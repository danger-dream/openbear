"""Real SQLite/worker regressions: a leaked thread must fail the subprocess timeout."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.db.engine import DB

# These test migrations, so every database here is built the real way.
pytestmark = pytest.mark.real_db

_CHILD = r'''
import asyncio
import json
import sqlite3
import sys
import threading
import time
from unittest.mock import patch

from app.db import engine
from app.db.connection_router import SQLiteWriterTimeout

LONG_READ = "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n"
LONG_WRITE = "INSERT INTO lifecycle SELECT (" + LONG_READ + ")"

async def main():
    mode, path = sys.argv[1:]
    baseline = set(threading.enumerate())
    database = engine.DB(path)
    opened = []
    original_open = engine.open_sqlite_connection

    async def tracked_open(path):
        connection = await original_open(path)
        opened.append(connection)
        if mode == 'reader_failure' and len(opened) == 2:
            original_execute = connection.execute
            def fail_reader(sql, *args, **kwargs):
                if sql == 'PRAGMA query_only=ON':
                    raise RuntimeError('reader setup failure')
                return original_execute(sql, *args, **kwargs)
            connection.execute = fail_reader
        return connection

    with patch.object(engine, 'open_sqlite_connection', tracked_open):
        if mode == 'opening_cancel':
            entered = asyncio.Event()
            release = threading.Event()
            loop = asyncio.get_running_loop()
            original_connect = sqlite3.connect
            def held_connect(*args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(2)
                return original_connect(*args, **kwargs)
            with patch.object(sqlite3, 'connect', held_connect):
                pending = asyncio.create_task(database.connect())
                await asyncio.wait_for(entered.wait(), 2)
                pending.cancel()
                await asyncio.sleep(0)
                pending.cancel()
                release.set()
                result = await asyncio.gather(pending, return_exceptions=True)
                assert isinstance(result[0], asyncio.CancelledError), result
            assert database._conn is None
        elif mode in ('schema_failure', 'schema_cancel', 'reader_failure'):
            async def fail_schema():
                await database.conn.execute('CREATE TABLE unfinished (value)')
                await database.conn.execute('INSERT INTO unfinished VALUES (1)')
                if mode == 'schema_cancel':
                    raise asyncio.CancelledError()
                raise RuntimeError('schema setup failure')
            if mode != 'reader_failure':
                database._pre_migrate_existing_session_schema = fail_schema
            try:
                await database.connect()
            except (RuntimeError, asyncio.CancelledError):
                pass
            else:
                raise AssertionError('startup must fail')
            assert database._conn is None
            assert all(conn._connection is None for conn in opened)
            if mode != 'reader_failure':
                with sqlite3.connect(path) as check:
                    assert check.execute('SELECT * FROM unfinished').fetchall() == []
        else:
            await database.connect()
            await database.conn.execute('CREATE TABLE lifecycle (value)')
            await database.conn.execute('INSERT INTO lifecycle VALUES (7)')
            await database.conn.commit()
            router = database._router()
            started = asyncio.Event()
            loop = asyncio.get_running_loop()
            raw = router._writer if 'write' in mode else router._reader
            await raw.set_trace_callback(lambda sql: loop.call_soon_threadsafe(started.set) if 'RECURSIVE' in sql else None)
            owner_released = asyncio.Event()

            async def query():
                if 'write' in mode:
                    try:
                        async with database.write_transaction() as conn:
                            await conn.execute('INSERT INTO lifecycle VALUES (8)')
                            await conn.execute(LONG_WRITE)
                    finally:
                        owner_released.set()
                elif mode == 'cancel_fetch':
                    cursor = await database.conn.execute('SELECT 1 UNION ALL SELECT (' + LONG_READ + ')')
                    try:
                        await cursor.fetchall()
                    finally:
                        await cursor.close()
                else:
                    await database.conn.execute(LONG_READ)

            task = asyncio.create_task(query())
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.sleep(.02)
            began = time.monotonic()
            if mode.startswith('close'):
                close = asyncio.create_task(database.close())
                await asyncio.sleep(0)
                if mode == 'close_cancel_write':
                    close.cancel()
                    await asyncio.sleep(0)
                    close.cancel()
                result = await asyncio.gather(close, return_exceptions=True)
                assert result[0] is None or isinstance(result[0], asyncio.CancelledError), result
                await asyncio.gather(task, return_exceptions=True)
                if 'write' in mode:
                    assert owner_released.is_set()
            elif mode == 'queued_reader_cancel':
                queued = asyncio.create_task(database.conn.execute('SELECT 42'))
                await asyncio.sleep(.01)
                queued.cancel()
                await asyncio.gather(queued, return_exceptions=True)
                assert not task.done(), 'cancelled reader interrupted a different request'
                assert time.monotonic() - began < .5
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            elif mode == 'writer_wait_timeout':
                try:
                    async with database.write_transaction(wait_timeout_s=.02):
                        raise AssertionError('waiter acquired active writer')
                except SQLiteWriterTimeout:
                    pass
                assert not task.done(), 'writer waiter interrupted active owner'
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            elif mode == 'interrupt_read':
                await database.interrupt()
                result = await asyncio.gather(task, return_exceptions=True)
                assert isinstance(result[0], sqlite3.OperationalError), result
            else:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                result = await asyncio.gather(task, return_exceptions=True)
                assert isinstance(result[0], asyncio.CancelledError), result
            elapsed = time.monotonic() - began
            assert elapsed < 2, elapsed
            if not mode.startswith('close'):
                await database.conn.execute('INSERT INTO lifecycle VALUES (9)')
                await database.conn.commit()
                async with await database.conn.execute('SELECT value FROM lifecycle ORDER BY value') as cursor:
                    assert [row[0] for row in await cursor.fetchall()] == [7, 9]
            await database.close()
            await database.close()
            with sqlite3.connect(path) as check:
                expected = [7] if mode.startswith('close') else [7, 9]
                assert [row[0] for row in check.execute('SELECT value FROM lifecycle ORDER BY value')] == expected
            assert all(conn._connection is None for conn in opened)
            print(json.dumps({'mode': mode, 'elapsed_s': elapsed}))
    for _ in range(100):
        if set(threading.enumerate()) <= baseline:
            break
        await asyncio.sleep(.01)
    assert set(threading.enumerate()) <= baseline, threading.enumerate()

asyncio.run(main())
'''


@pytest.mark.parametrize("mode", [
    "opening_cancel", "schema_failure", "schema_cancel", "reader_failure", "cancel_read", "cancel_fetch",
    "cancel_write", "close_read", "close_write", "close_cancel_write",
    "queued_reader_cancel", "writer_wait_timeout", "interrupt_read",
])
def test_sqlite_worker_lifecycle_in_real_process(tmp_path, mode):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_CHILD), mode, str(tmp_path / "lifecycle.db")],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=12,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_memory_order_migrates_once_without_rewriting_current_sort(tmp_path, monkeypatch):
    path = tmp_path / "ordering.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE memory_docs (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE, title TEXT DEFAULT '', summary TEXT DEFAULT '',
            project TEXT DEFAULT '', importance INTEGER DEFAULT 3, tags TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1, archived INTEGER DEFAULT 0, content TEXT DEFAULT '', updated_at INTEGER DEFAULT 0
        )""")
        conn.executemany("INSERT INTO memory_docs(id,name,importance) VALUES (?,?,?)", [(1, 'low', 1), (2, 'high', 9)])
    database = DB(str(path))
    await database.connect()
    await database.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id,sort,created_at,grp FROM memory_docs ORDER BY id").fetchall() == [
            (1, 20, 0, ''), (2, 10, 0, ''),
        ]
        conn.execute("UPDATE memory_docs SET sort=0 WHERE id=1")
        conn.execute("UPDATE memory_docs SET sort=123 WHERE id=2")

    from app.db.connection_router import InterruptibleConnection
    original_execute = InterruptibleConnection.execute
    def forbid_scan(self, sql, *args, **kwargs):
        if "SELECT id, sort FROM memory_docs" in sql or "UPDATE memory_docs SET sort" in sql:
            pytest.fail("completed ordering repair ran again")
        return original_execute(self, sql, *args, **kwargs)
    monkeypatch.setattr(InterruptibleConnection, "execute", forbid_scan)
    import app.db.engine as engine
    async def forbid_dedupe(_conn):
        pytest.fail("unique index already prevents duplicates")
    monkeypatch.setattr(engine, "dedupe_active_rath_agent_sessions", forbid_dedupe)
    reopened = DB(str(path))
    try:
        await reopened.connect()
    finally:
        await reopened.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT sort FROM memory_docs ORDER BY id").fetchall() == [(0,), (123,)]


async def test_memory_order_failure_does_not_record_completion(tmp_path, monkeypatch):
    path = tmp_path / "retry.db"
    database = DB(str(path))
    await database.connect()
    await database.conn.execute("INSERT INTO memory_docs(name,sort) VALUES ('legacy',0)")
    await database.conn.execute("DELETE FROM schema_data_migrations WHERE name='memory_document_order_v1'")
    await database.conn.commit()
    await database.close()

    from app.db.connection_router import InterruptibleConnection
    original_execute = InterruptibleConnection.execute
    def fail_marker(self, sql, parameters=()):
        if sql.startswith("INSERT INTO schema_data_migrations") and parameters[0] == 'memory_document_order_v1':
            raise RuntimeError("marker write failed")
        return original_execute(self, sql, parameters)
    with monkeypatch.context() as patch:
        patch.setattr(InterruptibleConnection, "execute", fail_marker)
        broken = DB(str(path))
        with pytest.raises(RuntimeError, match="marker write failed"):
            await broken.connect()
        assert broken._conn is None
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT sort FROM memory_docs").fetchone()[0] == 0
        assert conn.execute("SELECT 1 FROM schema_data_migrations WHERE name='memory_document_order_v1'").fetchone() is None
    recovered = DB(str(path))
    await recovered.connect()
    await recovered.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT sort FROM memory_docs").fetchone()[0] == 10


_MAINTENANCE_CHILD = r'''
import asyncio
import sqlite3
import sys
import threading
import time
from app.db.engine import DB

async def main():
    baseline = set(threading.enumerate())
    db = DB(sys.argv[1])
    await db.connect()
    await db.conn.execute('CREATE TABLE maintenance (value)')
    await db.conn.commit()
    writer = db._router()._writer
    external = sqlite3.connect(sys.argv[1])
    external.execute('BEGIN IMMEDIATE')
    began = time.monotonic()
    try:
        async with db.write_transaction(wait_timeout_s=.01, sqlite_busy_timeout_ms=10):
            raise AssertionError('external SQLite lock was not held')
    except sqlite3.OperationalError as exc:
        assert 'locked' in str(exc), str(exc)
    else:
        raise AssertionError('busy transaction must fail')
    finally:
        external.rollback()
        external.close()
    elapsed = time.monotonic() - began
    assert elapsed < .25, elapsed  # Not 5s busy waits plus retries.
    assert not db._router()._writer_lock.locked()
    async with writer.execute('PRAGMA busy_timeout') as cursor:
        assert (await cursor.fetchone())[0] == 5000

    async with db.write_transaction(sqlite_busy_timeout_ms=20) as conn:
        async with db.write_transaction(sqlite_busy_timeout_ms=0):
            async with conn.execute('PRAGMA busy_timeout') as cursor:
                assert (await cursor.fetchone())[0] == 0
        async with conn.execute('PRAGMA busy_timeout') as cursor:
            assert (await cursor.fetchone())[0] == 20

    began = time.monotonic()
    try:
        async with asyncio.timeout(.05):
            async with db.write_transaction(wait_timeout_s=.01, sqlite_busy_timeout_ms=10) as conn:
                await conn.execute('INSERT INTO maintenance VALUES (1)')
                await conn.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) INSERT INTO maintenance SELECT sum(x) FROM n")
    except TimeoutError:
        pass
    else:
        raise AssertionError('SQL deadline must cancel')
    assert time.monotonic() - began < 1
    assert not db._router()._writer_lock.locked()
    assert not writer.in_transaction
    async with writer.execute('PRAGMA busy_timeout') as cursor:
        assert (await cursor.fetchone())[0] == 5000
    async with await db.conn.execute('SELECT * FROM maintenance') as cursor:
        assert await cursor.fetchall() == []
    await db.conn.execute('INSERT INTO maintenance VALUES (2)')
    await db.conn.commit()
    await db.close()
    with sqlite3.connect(sys.argv[1]) as conn:
        assert conn.execute('SELECT value FROM maintenance').fetchall() == [(2,)]
    for _ in range(100):
        if set(threading.enumerate()) <= baseline:
            break
        await asyncio.sleep(.01)
    assert set(threading.enumerate()) <= baseline
    print('native_busy_elapsed_s=', elapsed)

asyncio.run(main())
'''


def test_maintenance_native_lock_timeout_and_execution_deadline(tmp_path):
    result = subprocess.run(
        [sys.executable, '-c', _MAINTENANCE_CHILD, str(tmp_path / 'maintenance.db')],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=12,
    )
    assert result.returncode == 0, result.stdout + result.stderr
