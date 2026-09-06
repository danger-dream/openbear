"""In-place compatibility for independent Agent sessions; run only at DB startup."""
from __future__ import annotations

import re

import aiosqlite


async def migrate_agent_continuity(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(rath_agent_sessions)")
    columns = {row[1] for row in await cursor.fetchall()}
    if columns:
        for name, ddl in (
            ("session_kind", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("active_task_uuid", "TEXT NOT NULL DEFAULT ''"),
            ("context_task_uuid", "TEXT NOT NULL DEFAULT ''"),
            ("context_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("revision", "INTEGER NOT NULL DEFAULT 0"),
            ("turn_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                await conn.execute(f"ALTER TABLE rath_agent_sessions ADD COLUMN {name} {ddl}")
        # Legacy role grouping remains unique; independent instances must never
        # be deduplicated by their preset, including on subsequent startups.
        cursor = await conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='ux_rath_agent_sessions_active_openbear_agent'"
        )
        index = await cursor.fetchone()
        if index and "session_kind" not in str(index[0] or ""):
            await conn.execute("DROP INDEX ux_rath_agent_sessions_active_openbear_agent")

    cursor = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_task_memories'"
    )
    row = await cursor.fetchone()
    if not row or "'agent_session'" in str(row[0]):
        return
    # SQLite cannot widen a CHECK constraint using ADD COLUMN. Retain every
    # existing column, UUID, body, revision and index while widening only scope.
    original = str(row[0])
    widened = original.replace("'conversation','agent_task'", "'conversation','agent_task','agent_session'")
    widened = widened.replace("scope_type='agent_task'", "scope_type IN ('agent_task','agent_session')")
    if widened == original or "'agent_session'" not in widened:
        raise RuntimeError("Unrecognized TaskMemory schema; refusing a lossy migration")
    temporary = re.sub(r"\bconversation_task_memories\b", "conversation_task_memories_v2", widened, count=1)
    cursor = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='conversation_task_memories' AND sql IS NOT NULL"
    )
    indexes = [str(item[0]) for item in await cursor.fetchall()]
    await conn.execute("SAVEPOINT agent_memory_scope_migration")
    try:
        await conn.execute(temporary)
        await conn.execute("INSERT INTO conversation_task_memories_v2 SELECT * FROM conversation_task_memories")
        await conn.execute("DROP TABLE conversation_task_memories")
        await conn.execute("ALTER TABLE conversation_task_memories_v2 RENAME TO conversation_task_memories")
        for sql in indexes:
            await conn.execute(sql)
        await conn.execute("RELEASE agent_memory_scope_migration")
    except BaseException:
        await conn.execute("ROLLBACK TO agent_memory_scope_migration")
        await conn.execute("RELEASE agent_memory_scope_migration")
        raise
