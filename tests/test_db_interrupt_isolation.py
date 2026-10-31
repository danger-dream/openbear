from __future__ import annotations

import asyncio
import time

import pytest

from app.db.connection_router import open_sqlite_connection
from app.db.engine import DB

_SLOW = "WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM c WHERE i < 200000000) SELECT count(*) FROM c"


async def _seeded(path) -> None:
    conn = await open_sqlite_connection(str(path))
    await conn.execute("CREATE TABLE t(x INTEGER)")
    await conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5000)])
    await conn.commit()
    await conn.close()


async def test_cancelled_query_leaves_other_cursors_and_later_queries_working(tmp_path):
    await _seeded(tmp_path / "db.sqlite")
    conn = await open_sqlite_connection(str(tmp_path / "db.sqlite"))
    try:
        # Another task's read that has started but not been fully fetched.
        other = await conn.execute("SELECT x FROM t ORDER BY x")
        assert (await other.fetchone())[0] == 0
        slow = asyncio.create_task(conn.execute_fetchall(_SLOW))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        # Aborted through the progress handler, not the connection-wide fallback.
        assert time.monotonic() - started < 0.5
        assert (await other.fetchone())[0] == 1
        await other.close()
        assert list(await conn.execute_fetchall("SELECT count(*) FROM t")) == [(5000,)]
    finally:
        await conn.close()


async def _router_with_rows(path, count: int) -> DB:
    db = DB(str(path))
    await db.connect()
    await db.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    await db.conn.commit()
    async with db.write_transaction(label="seed") as conn:
        await conn.executemany("INSERT INTO t VALUES (?, 'old')", [(i,) for i in range(count)])
    return db


async def test_a_read_not_yet_fetched_does_not_hide_later_commits(tmp_path):
    db = await _router_with_rows(tmp_path / "router.db", 2)
    try:
        # One task has run a read and is suspended before fetching it.
        waiting = await db.conn.execute("SELECT v FROM t ORDER BY id")
        async with db.write_transaction(label="update") as conn:
            await conn.execute("UPDATE t SET v='new'")
        later = await db.conn.execute("SELECT v FROM t ORDER BY id")
        assert [row[0] for row in await later.fetchall()] == ["new", "new"]
        assert [row[0] for row in await waiting.fetchall()] == ["old", "old"]
    finally:
        await db.close()


async def test_a_cancelled_read_whose_cursor_stays_reachable_does_not_hide_later_commits(tmp_path):
    db = await _router_with_rows(tmp_path / "router.db", 50)
    kept, parked = [], asyncio.Event()

    async def reading():
        cursor = await db.conn.execute("SELECT v FROM t ORDER BY id")
        # As when the cancelled frame sits in a reference cycle no collection has freed yet.
        kept.append(cursor)
        await cursor.fetchone()
        parked.set()
        await asyncio.Event().wait()

    try:
        task = asyncio.create_task(reading())
        await asyncio.wait_for(parked.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with db.write_transaction(label="update") as conn:
            await conn.execute("UPDATE t SET v='new'")
        later = await db.conn.execute("SELECT DISTINCT v FROM t")
        assert [row[0] for row in await later.fetchall()] == ["new"]
    finally:
        await db.close()


async def test_router_reads_rows_in_one_call_and_still_routes_owner_writes(tmp_path):
    db = DB(str(tmp_path / "router.db"))
    await db.connect()
    try:
        await db.conn.execute("CREATE TABLE IF NOT EXISTS probe(v INTEGER)")
        await db.conn.commit()
        async with db.write_transaction(label="probe") as conn:
            await conn.execute("INSERT INTO probe VALUES (1)")
            # Inside its own transaction the owner reads its uncommitted write.
            assert [tuple(row) for row in await db.conn.execute_fetchall("SELECT v FROM probe")] == [(1,)]
        assert [tuple(row) for row in await db.conn.execute_fetchall("SELECT v FROM probe")] == [(1,)]
    finally:
        await db.close()
