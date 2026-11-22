"""Retention regression/performance tests. All databases live under tmp_path."""
from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.db.engine import DB
from app.web_console.event_frame_retention import delete_query, page_queries, prune_event_frames

_FRESH = 9_999_999_999_999
_OLD = 1
_INSERT = """
    INSERT INTO web_event_frames
      (conversation_uuid, op_id, frame_seq, revision, updated_at_ms,
       op_type, action, display_seq, payload_json, created_at_ms)
    VALUES (?,?,?,?,?,'stats','patch',10,'{}',1)
"""


@pytest.fixture
async def retention_env(tmp_path):
    db = DB(str(tmp_path / "retention.db"))
    await db.connect()
    async with db.write_transaction() as conn:
        await conn.execute("CREATE TABLE retention_test_writes (key TEXT PRIMARY KEY, value TEXT)")
    try:
        yield SimpleNamespace(db=db)
    finally:
        await db.close()


async def _seed(owner, records):
    async with owner.db.write_transaction() as conn:
        await conn.executemany(_INSERT, records)


async def _frames(owner):
    cur = await owner.db.conn.execute("SELECT * FROM web_event_frames ORDER BY id")
    try:
        return [dict(row) for row in await cur.fetchall()]
    finally:
        await cur.close()


async def _finish(owner, **kwargs):
    deleted = 0
    for _ in range(1000):
        deleted += await prune_event_frames(owner, **kwargs)
        if not owner._web_event_frame_retention_pending:
            return deleted
    pytest.fail("retention did not finish its sweep")


def _retained(rows, scope="", keep=100):
    maximum_seq = {}
    maximum_id = {}
    for row in rows:
        conv, op = row["conversation_uuid"], row["op_id"]
        maximum_seq[conv] = max(row["frame_seq"], maximum_seq.get(conv, 0))
        maximum_id[conv, op] = max(row["id"], maximum_id.get((conv, op), 0))
    cutoff = int((time.time() - 7 * 86400) * 1000)
    return [row for row in rows if (
        (scope and row["conversation_uuid"] != scope)
        or row["id"] == maximum_id[row["conversation_uuid"], row["op_id"]]
        or (row["updated_at_ms"] >= cutoff and row["frame_seq"] > maximum_seq[row["conversation_uuid"]] - keep)
    )]


@pytest.mark.parametrize("scope", ["", "b"])
async def test_retention_matches_rules_and_preserves_snapshots_across_scopes(retention_env, scope):
    owner = retention_env
    rng = random.Random(8)
    records = []
    for conv in ("a", "b", "c"):
        for seq in range(1, 351):
            # Deliberately non-monotone/duplicate revisions and timestamps.
            records.append((conv, f"op:{seq % 13}", seq, rng.randrange(8), _OLD if seq % 5 == 0 else _FRESH))
    rng.shuffle(records)  # Latest means id, NOT frame_seq or revision.
    await _seed(owner, records)
    async with owner.db.write_transaction() as conn:
        await conn.execute("""
            INSERT INTO web_operations (conversation_uuid,op_id,op_type,display_seq,revision,payload_json,created_at_ms,updated_at_ms)
            VALUES ('a','op:1','stats',10,99,'{"snapshot":"untouched"}',1,1)
        """)
    before = await _frames(owner)
    expected = _retained(before, scope)
    deleted = await _finish(owner, conversation_uuid=scope, keep_recent=100, max_scan_rows=71, batch_size=17)
    assert await _frames(owner) == expected
    assert deleted == len(before) - len(expected)
    cur = await owner.db.conn.execute("SELECT payload_json, revision FROM web_operations")
    assert tuple(await cur.fetchone()) == ('{"snapshot":"untouched"}', 99)
    await cur.close()
    assert not owner._web_event_frame_retention_sweeps


async def test_large_protected_prefix_advances_and_equal_revision_seek_is_bounded(retention_env):
    owner = retention_env
    # 30k same-revision rows exercise the implicit rowid index suffix, not just
    # the easy case where every revision happens to be unique.
    await _seed(owner, (("a", "protected", seq, 1, _FRESH) for seq in range(1, 30_001)))
    await _seed(owner, (("z", "expired", seq, 1, _OLD) for seq in range(1, 301)))
    assert await prune_event_frames(owner, keep_recent=100_000, max_scan_rows=1024) == 0
    state = owner._web_event_frame_retention_sweeps[("", 100_000, 7)]
    assert state.cursor == ("a", "protected", 1, 1024)
    assert owner._web_event_frame_retention_pending

    # SQLite VM instruction counts establish actual work, beyond a pretty plan.
    with sqlite3.connect(owner.db.path) as conn:
        counts = []
        for row_id in (1024, 29_000):
            sql, params = page_queries("", ("a", "protected", 1, row_id), 128)[0]
            plan = " ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
            assert "rowid>?" in plan and "SEARCH" in plan
            steps = [0]
            def step():
                steps[0] += 1
                return 0
            conn.set_progress_handler(step, 1)
            assert len(conn.execute(sql, params).fetchall()) == 128
            conn.set_progress_handler(None, 0)
            counts.append(steps[0])
        assert abs(counts[0] - counts[1]) <= 20  # Initial schema-version check is 10 opcodes.
        assert max(counts) < 3000
        for scope in ("", "a"):
            for sql, params in page_queries(scope, ("a", "protected", 1, 29000), 128):
                plan = " ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
                assert "SEARCH" in plan
                assert "TEMP B-TREE" not in plan
        sql, params = delete_query([(30001, 30300), (30002, 30300)], int(time.time() * 1000), 100000)
        plan = " ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
        assert "SEARCH f USING INTEGER PRIMARY KEY" in plan
        assert "SEARCH witness USING INTEGER PRIMARY KEY" in plan
        assert "COVERING INDEX ux_web_event_frames_conv_seq" in plan
        assert "SCAN web_event_frames" not in plan
    assert await _finish(owner, keep_recent=100_000, max_scan_rows=2048) == 299
    rows = await _frames(owner)
    assert len(rows) == 30001
    assert rows[-1]["id"] == 30300


async def test_a_batch_whose_commit_outlasts_the_write_bound_is_still_committed_and_counted(retention_env):
    owner = retention_env
    await _seed(owner, (("a", "op", seq, seq, _OLD) for seq in range(1, 201)))
    original_transaction = owner.db.write_transaction

    # SQLite finishes a commit on its own thread even when the awaiting task is cancelled;
    # this commit takes longer than the 0.1 s bound on the retention write.
    @asynccontextmanager
    async def transaction(**kwargs):
        if kwargs.get("label") != "web-event-frame-retention":
            async with original_transaction(**kwargs) as conn:
                yield conn
            return
        conn = None
        try:
            async with original_transaction(**kwargs) as conn:
                real_commit = conn.commit

                async def commit():
                    done = asyncio.ensure_future(real_commit())
                    await asyncio.sleep(0.25)
                    await done
                conn.commit = commit
                yield conn
        finally:
            if conn is not None:
                vars(conn).pop("commit", None)
    owner.db.write_transaction = transaction

    deleted = await _finish(owner, batch_size=32, max_scan_rows=4096)
    remaining = [row["frame_seq"] for row in await _frames(owner)]
    assert remaining == [200]
    assert deleted == 199


async def test_normal_reads_and_writes_interleave_between_committed_batches(retention_env):
    owner = retention_env
    await _seed(owner, (("a", "op", seq, seq, _OLD) for seq in range(1, 5001)))
    original_transaction = owner.db.write_transaction
    commits = []
    interactive_at = []
    @asynccontextmanager
    async def transaction(**kwargs):
        async with original_transaction(**kwargs) as conn:
            yield conn
        if kwargs.get("label") == "web-event-frame-retention":
            commits.append(time.monotonic())
    owner.db.write_transaction = transaction

    async def interactive():
        while not commits:
            await asyncio.sleep(0)
        for seq in range(5001, 5011):
            started = time.monotonic()
            async with original_transaction(label="interactive", wait_timeout_s=0.5) as conn:
                await conn.execute(_INSERT, ("a", "op", seq, seq, _FRESH))
            cur = await owner.db.conn.execute("SELECT MAX(frame_seq) FROM web_event_frames WHERE conversation_uuid='a'")
            assert (await cur.fetchone())[0] == seq
            await cur.close()
            interactive_at.append((len(commits), time.monotonic() - started))
            await asyncio.sleep(0)

    async def cleanup():
        return await _finish(owner, batch_size=32, max_scan_rows=4096)

    deleted, _ = await asyncio.gather(cleanup(), interactive())
    assert deleted == 4999  # Fixed sweep end leaves old latest until next sweep.
    assert len(commits) > 10
    assert min(count for count, _ in interactive_at) < len(commits)
    assert max(duration for _, duration in interactive_at) < 0.5
    rows = await _frames(owner)
    assert [row["frame_seq"] for row in rows] == list(range(5000, 5011))
    assert await _finish(owner) == 1  # A new sweep sees the new superseding frame.
    assert [row["frame_seq"] for row in await _frames(owner)] == list(range(5001, 5011))


async def test_busy_writer_skips_without_losing_cursor_or_touching_owner(retention_env):
    owner = retention_env
    await _seed(owner, (("a", "op", seq, seq, _OLD) for seq in range(1, 201)))
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold_writer():
        async with owner.db.write_transaction() as conn:
            await conn.execute("INSERT INTO retention_test_writes(key,value) VALUES ('retention-busy','uncommitted')")
            entered.set()
            await release.wait()
    task = asyncio.create_task(hold_writer())
    await asyncio.wait_for(entered.wait(), 1)
    try:
        started = time.monotonic()
        assert await prune_event_frames(owner) == 0
        assert time.monotonic() - started < 0.3
        assert owner._web_event_frame_retention_pending
        assert owner._web_event_frame_retention_sweeps[("", 5000, 7)].cursor is None
    finally:
        release.set()
        await task
    assert await _finish(owner) == 199
    cur = await owner.db.conn.execute("SELECT value FROM retention_test_writes WHERE key='retention-busy'")
    assert (await cur.fetchone())[0] == "uncommitted"
    await cur.close()


async def test_cancel_after_delete_rolls_back_and_releases_writer(retention_env):
    owner = retention_env
    await _seed(owner, (("a", "op", seq, seq, _OLD) for seq in range(1, 501)))
    before = await _frames(owner)
    original_transaction = owner.db.write_transaction
    deleted_uncommitted = asyncio.Event()
    @asynccontextmanager
    async def paused_transaction(**kwargs):
        async with original_transaction(**kwargs) as conn:
            yield conn
            if kwargs.get("label") == "web-event-frame-retention":
                deleted_uncommitted.set()
                await asyncio.Event().wait()
    owner.db.write_transaction = paused_transaction
    task = asyncio.create_task(prune_event_frames(owner, time_budget_s=10))
    await asyncio.wait_for(deleted_uncommitted.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    owner.db.write_transaction = original_transaction
    async with original_transaction(wait_timeout_s=0.1) as conn:
        await conn.execute("INSERT INTO retention_test_writes(key,value) VALUES ('after-cancel','ok')")
    assert await _frames(owner) == before
    assert owner._web_event_frame_retention_pending
    assert owner._web_event_frame_retention_sweeps[("", 5000, 7)].cursor is None
    assert await _finish(owner) == 499


async def test_selection_delete_race_rechecks_witness_and_eligibility(retention_env):
    owner = retention_env
    # Expired candidate id=1, witness id=2, both in reconnect window.
    await _seed(owner, [("a", "op", 1, 1, _OLD), ("a", "op", 2, 2, _OLD)])
    original_transaction = owner.db.write_transaction
    @asynccontextmanager
    async def refresh_before_delete(**kwargs):
        async with original_transaction(**kwargs) as conn:
            if kwargs.get("label") == "web-event-frame-retention":
                await conn.execute("UPDATE web_event_frames SET updated_at_ms=? WHERE id=1", (_FRESH,))
                await conn.execute(_INSERT, ("a", "op", 3, 3, _FRESH))
            yield conn
    owner.db.write_transaction = refresh_before_delete
    assert await _finish(owner) == 0
    assert [row["id"] for row in await _frames(owner)] == [1, 2, 3]
    owner.db.write_transaction = original_transaction
    async with original_transaction() as conn:
        await conn.execute("UPDATE web_event_frames SET updated_at_ms=1 WHERE id=1")
    @asynccontextmanager
    async def remove_witness(**kwargs):
        async with original_transaction(**kwargs) as conn:
            if kwargs.get("label") == "web-event-frame-retention":
                await conn.execute("DELETE FROM web_event_frames WHERE id=3")
            yield conn
    owner.db.write_transaction = remove_witness
    assert await _finish(owner) == 0
    # The selected witness disappeared: conservative skip, never delete latest.
    assert [row["id"] for row in await _frames(owner)] == [1, 2]
