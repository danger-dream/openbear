"""tests/conftest.py opens each new database as a copy of one migrated per test process.
These keep that shortcut equal to DB._connect, so the rest of the suite tests the real thing."""
from __future__ import annotations

import sqlite3

import pytest

from app.db.engine import DB
from tests.conftest import fast_db_connect

pytestmark = pytest.mark.real_db


def _schema(path):
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = [name for kind, name, _table, _sql in rows if kind == "table"]
        counts = {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}
    return sorted(rows, key=repr), counts


async def _pragmas(db):
    async def read(conn, name):
        async with conn.execute(f"PRAGMA {name}") as cur:
            return (await cur.fetchone())[0]
    writer, reader = db.conn._writer, db.conn._reader
    return ({name: await read(writer, name) for name in ("journal_mode", "synchronous", "busy_timeout", "query_only")},
            {name: await read(reader, name) for name in ("journal_mode", "busy_timeout", "query_only")},
            type(writer.row_factory), type(reader.row_factory), type(db.conn))


async def test_a_new_database_from_the_template_matches_a_migrated_one(tmp_path):
    real, fast = DB(str(tmp_path / "real.db")), DB(str(tmp_path / "fast.db"))
    await real.connect()
    await fast_db_connect(fast)
    try:
        assert await _pragmas(fast) == await _pragmas(real)
        await fast.conn.execute("INSERT INTO app_state(key, value, updated_at) VALUES ('probe', 'x', 1)")
        await fast.conn.commit()
    finally:
        await real.close()
        await fast.close()
    real_schema, real_counts = _schema(real.path)
    fast_schema, fast_counts = _schema(fast.path)
    assert fast_schema == real_schema
    assert fast_counts == {**real_counts, "app_state": real_counts["app_state"] + 1}


async def test_an_existing_database_still_migrates(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE app_state(key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)")
    db = DB(str(path))
    await fast_db_connect(db)
    await db.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'messages'").fetchone()[0] == 1
