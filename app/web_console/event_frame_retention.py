"""Incremental retention using the existing (conversation, operation, revision) index.

SQLite's secondary index also contains rowid. Do *not* paginate with a four-
column tuple including id: SQLite only seeks the three explicit index columns
and rescans equal revisions. Instead finish the current revision by rowid seek,
then seek strictly past its three-column prefix. Every SELECT has a small LIMIT
before any eligibility filtering; an arbitrarily long protected run is cheap.

Rows are grouped by operation, but revision need not be monotone with id. Carry
only the largest id seen for the current operation between pages. That row is
never a delete candidate until a strictly larger id supplies a retention witness.
Thus a completed sweep removes exactly the eligible superseded rows, without a
GROUP BY, an operation-wide MAX scan, or an unbounded in-memory set of operations.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from itertools import groupby
from typing import Any

_INDEX = "idx_web_event_frames_conv_op_revision"
_COLUMNS = "id, conversation_uuid, op_id, revision, frame_seq, updated_at_ms"
_ORDER = "conversation_uuid, op_id, revision, id"


@dataclass
class RetentionSweep:
    # Fixed upper key prevents an active append-only tail from postponing wrap.
    end: tuple[str, str, int, int] | None = None
    cursor: tuple[str, str, int, int] | None = None
    latest: Any = None


def _key(row: Any) -> tuple[str, str, int, int]:
    return (row["conversation_uuid"], row["op_id"], row["revision"], row["id"])


async def _rows(conn: Any, sql: str, params: tuple = ()) -> list[Any]:
    # One call: a sweep must not hold the shared reader's snapshot while it yields.
    execute_fetchall = getattr(conn, "execute_fetchall", None)
    if execute_fetchall is not None:
        return list(await execute_fetchall(sql, params))
    cur = await conn.execute(sql, params)
    try:
        return await cur.fetchall()
    finally:
        await cur.close()


def page_queries(
    conversation_uuid: str, cursor: tuple[str, str, int, int] | None, limit: int,
) -> list[tuple[str, tuple]]:
    """At most two disjoint, bounded index ranges (also used by EXPLAIN tests)."""
    select = f"SELECT {_COLUMNS} FROM web_event_frames INDEXED BY {_INDEX}"
    if cursor is None:
        where, params = (" WHERE conversation_uuid=?", (conversation_uuid,)) if conversation_uuid else ("", ())
        return [(f"{select}{where} ORDER BY {_ORDER} LIMIT ?", (*params, limit))]
    conv, op, revision, row_id = cursor
    same = (
        f"{select} WHERE conversation_uuid=? AND op_id=? AND revision=? AND id>? ORDER BY id LIMIT ?",
        (conv, op, revision, row_id, limit),
    )
    if conversation_uuid:
        rest = (
            f"{select} WHERE conversation_uuid=? AND (op_id, revision)>(?,?) ORDER BY {_ORDER} LIMIT ?",
            (conversation_uuid, op, revision, limit),
        )
    else:
        rest = (
            f"{select} WHERE (conversation_uuid, op_id, revision)>(?,?,?) ORDER BY {_ORDER} LIMIT ?",
            (conv, op, revision, limit),
        )
    return [same, rest]


async def _page(conn: Any, scope: str, state: RetentionSweep, limit: int) -> tuple[list[Any], bool]:
    result: list[Any] = []
    for sql, params in page_queries(scope, state.cursor, limit):
        result.extend(await _rows(conn, sql, (*params[:-1], limit - len(result))))
        if len(result) == limit:
            break
    done = len(result) < limit
    for index, row in enumerate(result):
        if _key(row) >= state.end:
            done = True
            result = result[:index + (1 if _key(row) == state.end else 0)]
            break
    return result, done


async def _candidates(conn: Any, rows: list[Any], latest: Any, cutoff: int, keep: int) -> tuple[list[tuple[int, int]], Any]:
    candidates: list[tuple[int, int]] = []
    windows: dict[str, int] = {}
    for pair, group in groupby(rows, key=lambda row: (row["conversation_uuid"], row["op_id"])):
        members = list(group)
        if latest is not None and (latest["conversation_uuid"], latest["op_id"]) == pair:
            members.append(latest)
        latest = max(members, key=lambda row: row["id"])
        conv = pair[0]
        if conv not in windows:
            maximum = await _rows(conn, """
                SELECT frame_seq FROM web_event_frames INDEXED BY ux_web_event_frames_conv_seq
                WHERE conversation_uuid=? ORDER BY frame_seq DESC LIMIT 1
            """, (conv,))
            windows[conv] = (maximum[0]["frame_seq"] if maximum else 0) - keep
        candidates.extend(
            (row["id"], latest["id"]) for row in members
            if row["id"] != latest["id"]
            and (row["updated_at_ms"] < cutoff or row["frame_seq"] <= windows[conv])
        )
    return candidates, latest


def delete_query(candidates: list[tuple[int, int]], cutoff: int, keep: int) -> tuple[str, tuple]:
    # The CTE contains at most batch_size constants, NOT a scan of the log.
    # Revalidate under the writer: neither a removed witness nor changes between
    # selection and deletion may turn the latest/currently protected row into a
    # candidate. Both frame lookups are PK seeks; the window is a covering seek.
    values = ",".join("(?,?)" for _ in candidates)
    sql = f"""
        WITH candidates(id, witness_id) AS (VALUES {values})
        DELETE FROM web_event_frames AS f
        WHERE f.id IN (SELECT id FROM candidates)
          AND EXISTS (
            SELECT 1 FROM candidates AS c
            JOIN web_event_frames AS witness ON witness.id=c.witness_id
            WHERE c.id=f.id AND witness.id>f.id
              AND witness.conversation_uuid=f.conversation_uuid AND witness.op_id=f.op_id
          )
          AND (f.updated_at_ms < ? OR f.frame_seq <= (
            SELECT recent.frame_seq FROM web_event_frames AS recent
              INDEXED BY ux_web_event_frames_conv_seq
            WHERE recent.conversation_uuid=f.conversation_uuid
            ORDER BY recent.frame_seq DESC LIMIT 1
          ) - ?)
    """
    return sql, (*[value for pair in candidates for value in pair], cutoff, keep)


async def prune_event_frames(
    owner: Any,
    conversation_uuid: str = "",
    *,
    keep_recent: int = 5000,
    max_age_days: int = 7,
    batch_size: int = 128,
    max_scan_rows: int = 4096,
    time_budget_s: float = 1.0,
) -> int:
    """Return deletions in this bounded slice, not in the entire retention sweep.

    ``owner._web_event_frame_retention_pending`` tells the lifecycle scheduler to
    continue after a short delay (including zero-deletion slices and busy skips).
    A finished sweep resets its cursor for the next daily pass. State is in-memory
    and per scope/policy; restarting safely begins a fresh sweep. A single lock
    serializes global/scoped calls, but a concurrent caller never waits on it.
    """
    scope = str(conversation_uuid or "")
    keep = max(100, int(keep_recent))
    age = max(1, int(max_age_days))
    batch_size = min(128, max(1, int(batch_size)))
    max_scan_rows = max(1, int(max_scan_rows))
    budget = max(0.001, float(time_budget_s))
    cutoff = int((time.time() - age * 86_400) * 1000)
    if not hasattr(owner, "_web_event_frame_retention_lock"):
        owner._web_event_frame_retention_lock = asyncio.Lock()
        owner._web_event_frame_retention_sweeps = {}
    owner._web_event_frame_retention_pending = True
    lock = owner._web_event_frame_retention_lock
    if lock.locked():
        return 0
    async with lock:
        sweeps = owner._web_event_frame_retention_sweeps
        policy = (scope, keep, age)
        state = sweeps.setdefault(policy, RetentionSweep())
        deadline = time.monotonic() + budget
        deleted = scanned = 0
        try:
            if state.end is None:
                where, params = (" WHERE conversation_uuid=?", (scope,)) if scope else ("", ())
                async with asyncio.timeout_at(min(deadline, time.monotonic() + 0.25)):
                    end = await _rows(owner.db.conn, f"""
                        SELECT {_COLUMNS} FROM web_event_frames INDEXED BY {_INDEX}{where}
                        ORDER BY conversation_uuid DESC, op_id DESC, revision DESC, id DESC LIMIT 1
                    """, params)
                if not end:
                    sweeps.pop(policy)
                    owner._web_event_frame_retention_pending = False
                    return 0
                state.end = _key(end[0])
            while scanned < max_scan_rows and time.monotonic() < deadline:
                limit = min(batch_size, max_scan_rows - scanned)
                # DB's interruptible read API must cancel SQLite itself, not just
                # abandon an aiosqlite Future. No writer is held during selection.
                async with asyncio.timeout_at(min(deadline, time.monotonic() + 0.25)):
                    rows, done = await _page(owner.db.conn, scope, state, limit)
                    candidates, latest = await _candidates(owner.db.conn, rows, state.latest, cutoff, keep)
                count = 0
                if candidates:
                    async with asyncio.timeout_at(min(deadline, time.monotonic() + 0.1)) as bound:
                        async with owner.db.write_transaction(
                            label="web-event-frame-retention", wait_timeout_s=0.01,
                            sqlite_busy_timeout_ms=10,
                        ) as writer:
                            sql, params = delete_query(candidates, cutoff, keep)
                            cur = await writer.execute(sql, params)
                            await cur.close()
                            changes = await _rows(writer, "SELECT changes()")
                            count = int(changes[0][0])
                            # SQLite completes a commit even if its waiter is cancelled; a
                            # cancelled one would delete these rows without counting them.
                            bound.reschedule(None)
                # Only publish progress after commit. Timeout/cancellation/busy
                # retries this page, rather than silently losing candidates.
                deleted += count
                scanned += len(rows)
                if rows:
                    state.cursor = _key(rows[-1])
                    state.latest = latest
                if done:
                    sweeps.pop(policy)
                    owner._web_event_frame_retention_pending = False
                    break
                # Even all-protected pages release the reader cursor and yield.
                await asyncio.sleep(0)
        except TimeoutError:
            pass  # Optional maintenance must not join the business 30s queue.
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
        return deleted
