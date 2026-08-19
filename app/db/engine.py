"""SQLite 引擎 —— aiosqlite + WAL。"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.db.connection_router import SQLiteConnectionRouter
from app.db.schema_migrations import (
    backfill_web_operation_terminal_times,
    dedupe_active_rath_agent_sessions,
    reconcile_web_operation_snapshot_frames,
    remove_removed_tools_from_agent_allowlists,
)
from app.logging import get_logger

log = get_logger("db.engine")

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


class DB:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | SQLiteConnectionRouter | None = None

    async def connect(self) -> None:
        db_file = Path(self._path).expanduser()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        # schema.sql 是新库的唯一结构来源；这里保留少量兼容性补丁，
        # 让开发期既有 SQLite 也能安全补上 Rath 新字段。注意旧库在
        # executescript 里建新索引前必须先补列，否则 SQLite 会报 no such column。
        await self._pre_migrate_existing_session_schema()
        await self._pre_migrate_existing_web_schema()
        await self._pre_migrate_existing_rath_schema()
        await self._pre_migrate_existing_model_call_schema()
        await self._pre_migrate_memory_assets_schema()
        await self._conn.executescript(_SCHEMA)
        await self._remove_structural_memory_categories()
        backfilled_terminal_times = await backfill_web_operation_terminal_times(self._conn)
        if backfilled_terminal_times:
            log.info("已固化历史 Web Operation 首次终结时间", 数量=backfilled_terminal_times)
        repaired_web_frames = await reconcile_web_operation_snapshot_frames(self._conn)
        if repaired_web_frames:
            log.info("已补齐历史 Web Operation 快照帧", 数量=repaired_web_frames)
        await self._migrate_rath_schema()
        await self._conn.commit()

        # From this point onward every application write is routed to the one
        # physical writer above.  Concurrent reads use a query-only connection,
        # so a stale read snapshot can never be upgraded into a writer after a
        # different connection committed (SQLITE_BUSY_SNAPSHOT).
        writer = self._conn
        reader = await aiosqlite.connect(self._path)
        reader.row_factory = aiosqlite.Row
        await reader.execute("PRAGMA query_only=ON")
        await reader.execute("PRAGMA busy_timeout=5000")
        self._conn = SQLiteConnectionRouter(reader=reader, writer=writer)
        log.info("数据库已连接", 路径=self._path, 写入模式="single-writer")

    async def _table_exists(self, table: str) -> bool:
        if self._conn is None:
            return False
        cur = await self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        )
        return await cur.fetchone() is not None

    async def _columns(self, table: str) -> set[str]:
        if self._conn is None:
            return set()
        cur = await self._conn.execute(f"PRAGMA table_info({table})")
        return {str(row[1]) for row in await cur.fetchall()}

    async def _add_column_if_missing(self, table: str, name: str, ddl: str) -> None:
        if self._conn is None or not await self._table_exists(table):
            return
        if name not in await self._columns(table):
            await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    async def _pre_migrate_existing_session_schema(self) -> None:
        await self._add_column_if_missing("sessions", "fast_mode", "fast_mode INTEGER DEFAULT 0")

    async def _remove_structural_memory_categories(self) -> None:
        """身份、人格、行为准则已归属提示词，旧分类与条目不再保留。"""
        if self._conn is None or not await self._table_exists("memory_categories"):
            return
        placeholders = ",".join("?" for _ in ("identity", "persona", "rule"))
        cur = await self._conn.execute(
            f"SELECT id FROM memory_categories WHERE key IN ({placeholders})",
            ("identity", "persona", "rule"),
        )
        category_ids = [int(row["id"]) for row in await cur.fetchall()]
        if category_ids:
            id_placeholders = ",".join("?" for _ in category_ids)
            await self._conn.execute(
                f"DELETE FROM memory_entries WHERE category_id IN ({id_placeholders})",
                tuple(category_ids),
            )
            await self._conn.execute(
                f"DELETE FROM memory_categories WHERE id IN ({id_placeholders})",
                tuple(category_ids),
            )

    async def _pre_migrate_memory_assets_schema(self) -> None:
        """Add grouping/order/timestamp columns without inventing historical creation times."""
        await self._add_column_if_missing("memory_entries", "expanded", "expanded INTEGER DEFAULT 0")
        await self._add_column_if_missing("memory_entries", "created_at", "created_at INTEGER DEFAULT 0")
        await self._add_column_if_missing("memory_secrets", "grp", "grp TEXT DEFAULT ''")
        await self._add_column_if_missing("memory_secrets", "created_at", "created_at INTEGER DEFAULT 0")
        await self._add_column_if_missing("memory_docs", "grp", "grp TEXT DEFAULT ''")
        await self._add_column_if_missing("memory_docs", "sort", "sort INTEGER DEFAULT 0")
        await self._add_column_if_missing("memory_docs", "created_at", "created_at INTEGER DEFAULT 0")
        if self._conn is None or not await self._table_exists("memory_docs"):
            return
        cur = await self._conn.execute("SELECT id FROM memory_docs ORDER BY importance DESC, id")
        rows = await cur.fetchall()
        for index, row in enumerate(rows, start=1):
            await self._conn.execute(
                "UPDATE memory_docs SET sort=? WHERE id=? AND COALESCE(sort, 0)=0",
                (index * 10, int(row[0])),
            )

    async def _pre_migrate_existing_web_schema(self) -> None:
        has_web_conversations = await self._table_exists("web_conversations")
        had_display_order = has_web_conversations and "display_order" in await self._columns("web_conversations")
        await self._add_column_if_missing("web_conversations", "pinned_at", "pinned_at INTEGER DEFAULT 0")
        await self._add_column_if_missing("web_conversations", "display_order", "display_order REAL")
        if has_web_conversations and not had_display_order:
            await self._backfill_web_conversation_display_order()
        await self._add_column_if_missing("web_conversations", "agent_model", "agent_model TEXT DEFAULT ''")
        await self._add_column_if_missing("web_conversations", "agent_think_level", "agent_think_level TEXT DEFAULT ''")
        await self._add_column_if_missing("web_conversations", "agent_fast_mode", "agent_fast_mode INTEGER DEFAULT -1")
        await self._add_column_if_missing("web_login_requests", "nonce_hash", "nonce_hash TEXT DEFAULT ''")
        await self._add_column_if_missing("web_controller_context_snapshots", "session_uuid", "session_uuid TEXT NOT NULL DEFAULT ''")
        await self._add_column_if_missing("web_controller_context_snapshots", "known", "known INTEGER NOT NULL DEFAULT 1")
        await self._add_column_if_missing("web_memory_reminders", "session_uuid", "session_uuid TEXT NOT NULL DEFAULT ''")
        await self._add_column_if_missing("memory_templates", "is_agent_active", "is_agent_active INTEGER DEFAULT 0")
        await self._migrate_agent_prompt_template_flag()
        await self._pre_migrate_existing_messages_schema()
        await self._pre_migrate_existing_web_artifact_schema()
        await self._pre_migrate_existing_web_operation_schema()

    async def _backfill_web_conversation_display_order(self) -> None:
        """Seed legacy rows from the exact pre-feature sidebar ordering."""
        if self._conn is None or not await self._table_exists("web_conversations"):
            return
        cur = await self._conn.execute(
            """
            SELECT id, owner_chat_id, pinned_at
            FROM web_conversations
            ORDER BY owner_chat_id ASC,
                     CASE WHEN COALESCE(pinned_at, 0) > 0 THEN 0 ELSE 1 END ASC,
                     COALESCE(created_at, 0) DESC,
                     id DESC
            """
        )
        ordinal_by_group: dict[tuple[int, bool], int] = {}
        updates: list[tuple[float, int]] = []
        for row in await cur.fetchall():
            group = (int(row["owner_chat_id"] or 0), int(row["pinned_at"] or 0) > 0)
            ordinal = ordinal_by_group.get(group, 0) + 1
            ordinal_by_group[group] = ordinal
            updates.append((float(ordinal * 1024), int(row["id"])))
        if updates:
            await self._conn.executemany(
                "UPDATE web_conversations SET display_order=? WHERE id=?",
                updates,
            )

    async def _migrate_agent_prompt_template_flag(self) -> None:
        if self._conn is None or not await self._table_exists("memory_templates"):
            return
        columns = await self._columns("memory_templates")
        if "is_agent_active" not in columns:
            return
        cur = await self._conn.execute("SELECT 1 FROM memory_templates WHERE is_agent_active=1 LIMIT 1")
        if await cur.fetchone() is not None:
            return
        cur = await self._conn.execute(
            "SELECT id FROM memory_templates WHERE name=? ORDER BY id DESC LIMIT 1",
            ("Agent基础提示词",),
        )
        row = await cur.fetchone()
        if row is not None:
            await self._conn.execute("UPDATE memory_templates SET is_agent_active=0")
            await self._conn.execute(
                "UPDATE memory_templates SET is_agent_active=1 WHERE id=?",
                (int(row[0]),),
            )

    async def _pre_migrate_existing_messages_schema(self) -> None:
        # Ownership metadata for UI op <-> DB message binding. Keep these ALTERs
        # before schema.sql creates indexes that reference the columns.
        for name, ddl in (
            ("conversation_uuid", "conversation_uuid TEXT DEFAULT ''"),
            ("turn_uuid", "turn_uuid TEXT DEFAULT ''"),
            ("parent_turn_uuid", "parent_turn_uuid TEXT DEFAULT ''"),
            ("run_root_turn_uuid", "run_root_turn_uuid TEXT DEFAULT ''"),
            ("task_uuid", "task_uuid TEXT DEFAULT ''"),
            ("agent_session_uuid", "agent_session_uuid TEXT DEFAULT ''"),
        ):
            await self._add_column_if_missing("messages", name, ddl)

    async def _pre_migrate_existing_web_artifact_schema(self) -> None:
        # Development databases may have been created from an intermediate
        # artifact table while iterating.  Keep these ALTERs before schema.sql
        # creates indexes that reference the columns.
        for name, ddl in (
            ("artifact_uuid", "artifact_uuid TEXT NOT NULL DEFAULT ''"),
            ("conversation_uuid", "conversation_uuid TEXT NOT NULL DEFAULT ''"),
            ("owner_chat_id", "owner_chat_id INTEGER NOT NULL DEFAULT 0"),
            ("internal_chat_id", "internal_chat_id INTEGER NOT NULL DEFAULT 0"),
            ("turn_uuid", "turn_uuid TEXT DEFAULT ''"),
            ("message_id", "message_id INTEGER DEFAULT 0"),
            ("op_id", "op_id TEXT DEFAULT ''"),
            ("file_name", "file_name TEXT NOT NULL DEFAULT ''"),
            ("mime_type", "mime_type TEXT DEFAULT 'application/octet-stream'"),
            ("size_bytes", "size_bytes INTEGER DEFAULT 0"),
            ("sha256", "sha256 TEXT NOT NULL DEFAULT ''"),
            ("storage_path", "storage_path TEXT NOT NULL DEFAULT ''"),
            ("source_path", "source_path TEXT DEFAULT ''"),
            ("source_url", "source_url TEXT DEFAULT ''"),
            ("created_at", "created_at INTEGER DEFAULT 0"),
            ("deleted_at", "deleted_at INTEGER DEFAULT 0"),
        ):
            await self._add_column_if_missing("web_artifacts", name, ddl)

    async def _pre_migrate_existing_web_operation_schema(self) -> None:
        # Web Event / Operation v2 tables are new, but keep this compatible with
        # development databases that may have been created from an intermediate
        # schema during the multi-round refactor.  These ALTERs must run before
        # schema.sql creates indexes that reference the new columns.
        for name, ddl in (
            ("internal_chat_id", "internal_chat_id INTEGER DEFAULT 0"),
            ("owner_chat_id", "owner_chat_id INTEGER DEFAULT 0"),
            ("frame_seq", "frame_seq INTEGER NOT NULL DEFAULT 0"),
            ("op_id", "op_id TEXT NOT NULL DEFAULT ''"),
            ("op_type", "op_type TEXT NOT NULL DEFAULT ''"),
            ("action", "action TEXT NOT NULL DEFAULT ''"),
            ("turn_uuid", "turn_uuid TEXT DEFAULT ''"),
            ("parent_turn_uuid", "parent_turn_uuid TEXT DEFAULT ''"),
            ("run_root_turn_uuid", "run_root_turn_uuid TEXT DEFAULT ''"),
            ("target_type", "target_type TEXT DEFAULT ''"),
            ("target_id", "target_id TEXT DEFAULT ''"),
            ("task_uuid", "task_uuid TEXT DEFAULT ''"),
            ("run_id", "run_id TEXT DEFAULT ''"),
            ("revision", "revision INTEGER NOT NULL DEFAULT 0"),
            ("display_seq", "display_seq INTEGER NOT NULL DEFAULT 0"),
            ("payload_json", "payload_json TEXT NOT NULL DEFAULT '{}'"),
            ("debug_json", "debug_json TEXT DEFAULT '{}'"),
            ("created_at_ms", "created_at_ms INTEGER NOT NULL DEFAULT 0"),
            ("updated_at_ms", "updated_at_ms INTEGER NOT NULL DEFAULT 0"),
        ):
            await self._add_column_if_missing("web_event_frames", name, ddl)

        for name, ddl in (
            ("internal_chat_id", "internal_chat_id INTEGER DEFAULT 0"),
            ("op_id", "op_id TEXT NOT NULL DEFAULT ''"),
            ("op_type", "op_type TEXT NOT NULL DEFAULT ''"),
            ("turn_uuid", "turn_uuid TEXT DEFAULT ''"),
            ("parent_turn_uuid", "parent_turn_uuid TEXT DEFAULT ''"),
            ("run_root_turn_uuid", "run_root_turn_uuid TEXT DEFAULT ''"),
            ("target_type", "target_type TEXT DEFAULT ''"),
            ("target_id", "target_id TEXT DEFAULT ''"),
            ("task_uuid", "task_uuid TEXT DEFAULT ''"),
            ("run_id", "run_id TEXT DEFAULT ''"),
            ("display_seq", "display_seq INTEGER NOT NULL DEFAULT 0"),
            ("status", "status TEXT DEFAULT ''"),
            ("lifecycle", "lifecycle TEXT DEFAULT ''"),
            ("internal", "internal INTEGER DEFAULT 0"),
            ("source", "source TEXT DEFAULT ''"),
            ("transcript_message_ids_json", "transcript_message_ids_json TEXT DEFAULT '[]'"),
            ("revision", "revision INTEGER NOT NULL DEFAULT 0"),
            ("payload_json", "payload_json TEXT NOT NULL DEFAULT '{}'"),
            ("created_at_ms", "created_at_ms INTEGER NOT NULL DEFAULT 0"),
            ("updated_at_ms", "updated_at_ms INTEGER NOT NULL DEFAULT 0"),
        ):
            await self._add_column_if_missing("web_operations", name, ddl)

    async def _pre_migrate_existing_rath_schema(self) -> None:
        if self._conn is not None and await self._table_exists("rath_agents"):
            columns = await self._columns("rath_agents")
            old_agent_columns = {"workflow_uuid", "base_template_id", "context_policy_json"}
            if old_agent_columns & columns:
                await self._conn.execute("DROP TABLE IF EXISTS rath_agents")
        await self._add_column_if_missing("rath_tasks", "agent_session_uuid", "agent_session_uuid TEXT DEFAULT ''")
        await self._add_column_if_missing("rath_tasks", "caller_agent_session_uuid", "caller_agent_session_uuid TEXT DEFAULT ''")
        await self._add_column_if_missing("rath_tasks", "work_tool_call_count", "work_tool_call_count INTEGER DEFAULT 0")
        await self._add_column_if_missing("rath_tasks", "plan_tool_call_count", "plan_tool_call_count INTEGER DEFAULT 0")
        await self._add_column_if_missing("rath_tasks", "last_input_tokens", "last_input_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("rath_tasks", "last_output_tokens", "last_output_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("rath_tasks", "last_cache_read_tokens", "last_cache_read_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("rath_tasks", "last_cache_write_tokens", "last_cache_write_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("rath_tasks", "parent_task_uuid", "parent_task_uuid TEXT DEFAULT ''")
        await self._add_column_if_missing("rath_tasks", "turn_uuid", "turn_uuid TEXT DEFAULT ''")
        await self._add_column_if_missing("rath_tasks", "parent_turn_uuid", "parent_turn_uuid TEXT DEFAULT ''")
        await self._add_column_if_missing("rath_tasks", "run_root_turn_uuid", "run_root_turn_uuid TEXT DEFAULT ''")
        await self._add_column_if_missing(
            "rath_task_plan_state", "approved_tools_json", "approved_tools_json TEXT NOT NULL DEFAULT '[]'"
        )
        await self._add_column_if_missing(
            "rath_task_plan_decisions", "granted_tools_json", "granted_tools_json TEXT NOT NULL DEFAULT '[]'"
        )
        await self._add_column_if_missing(
            "rath_task_plan_requests", "request_fingerprint", "request_fingerprint TEXT NOT NULL DEFAULT ''"
        )
        for name, ddl in (
            ("metadata_json", "metadata_json TEXT NOT NULL DEFAULT '{}'"),
            ("response_status", "response_status TEXT DEFAULT ''"),
            ("response_reason", "response_reason TEXT DEFAULT ''"),
            ("response_plan_impact", "response_plan_impact TEXT DEFAULT ''"),
            ("response_next_action", "response_next_action TEXT DEFAULT ''"),
            ("responded_at", "responded_at INTEGER DEFAULT 0"),
        ):
            await self._add_column_if_missing("rath_task_controls", name, ddl)

    async def _pre_migrate_existing_model_call_schema(self) -> None:
        await self._add_column_if_missing("model_calls", "call_kind", "call_kind TEXT DEFAULT ''")
        await self._add_column_if_missing("model_calls", "last_input_tokens", "last_input_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("model_calls", "last_output_tokens", "last_output_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("model_calls", "last_cache_read_tokens", "last_cache_read_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("model_calls", "last_cache_write_tokens", "last_cache_write_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("model_calls", "expert_input_tokens", "expert_input_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("model_calls", "expert_output_tokens", "expert_output_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("model_calls", "expert_cache_read_tokens", "expert_cache_read_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("model_calls", "expert_cache_write_tokens", "expert_cache_write_tokens INTEGER DEFAULT 0")
        await self._add_column_if_missing("model_calls", "expert_tool_calls", "expert_tool_calls INTEGER DEFAULT 0")
        if self._conn is not None and await self._table_exists("model_calls"):
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_calls_kind_time ON model_calls(chat_id, call_kind, created_at DESC, id DESC)"
            )

    async def _migrate_rath_schema(self) -> None:
        if self._conn is None:
            return

        await self._pre_migrate_existing_rath_schema()
        removed_tool_rows = await remove_removed_tools_from_agent_allowlists(self._conn)
        if removed_tool_rows:
            log.info("已清理 Agent 工具白名单中的废弃工具", 数量=removed_tool_rows)
        await dedupe_active_rath_agent_sessions(self._conn)
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_rath_agent_sessions_active_openbear_agent ON rath_agent_sessions(openbear_session_uuid, workflow_uuid, agent_key) WHERE status='active'"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rath_tasks_agent_session ON rath_tasks(agent_session_uuid, updated_at DESC)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rath_tasks_caller_agent_session ON rath_tasks(caller_agent_session_uuid, updated_at DESC)"
        )
        repaired_revised_steps = await self._conn.execute(
            """
            UPDATE rath_task_plan_step_runs
            SET status='superseded', updated_at=MAX(updated_at, ?), row_revision=row_revision+1
            WHERE status IN ('pending','running','blocked')
              AND EXISTS (
                SELECT 1
                FROM rath_task_plan_versions version
                WHERE version.task_uuid=rath_task_plan_step_runs.task_uuid
                  AND version.version=rath_task_plan_step_runs.plan_version
                  AND version.status='revise_requested'
              )
            """,
            (int(time.time()),),
        )
        if repaired_revised_steps.rowcount:
            log.info("已归档历史 revise Plan 的未完成步骤", 数量=repaired_revised_steps.rowcount)

    def _router(self) -> SQLiteConnectionRouter:
        if not isinstance(self._conn, SQLiteConnectionRouter):
            raise RuntimeError("数据库尚未完成连接")
        return self._conn

    @asynccontextmanager
    async def write_transaction(self, *, label: str = "write") -> AsyncIterator[aiosqlite.Connection]:
        """Run one atomic mutation unit on the global SQLite writer."""
        async with self._router().transaction(label=label) as conn:
            yield conn

    @asynccontextmanager
    async def web_operation_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Publish one Web Operation snapshot/frame unit on the global writer."""
        async with self._router().transaction(label="web_operation") as conn:
            yield conn

    @asynccontextmanager
    async def accounting_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Persist one complete billing ledger update on the global writer."""
        async with self._router().transaction(label="accounting") as conn:
            yield conn

    @asynccontextmanager
    async def plan_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize one Agent Plan state-machine mutation on the global writer."""
        async with self._router().transaction(label="agent_plan") as conn:
            yield conn

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def conn(self) -> aiosqlite.Connection | SQLiteConnectionRouter:
        if self._conn is None:
            raise RuntimeError("DB 未连接")
        return self._conn



def now_ts() -> int:
    return int(time.time())
