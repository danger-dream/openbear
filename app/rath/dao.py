"""SQLite persistence for Rath workflows, tasks, events, artifacts and controls."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

import aiosqlite

from app.db.engine import DB, now_ts
from app.rath.schemas import (
    ACTIVE_TASK_STATUSES,
    CONTROLLABLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    RathAgentDef,
    RathAgentSession,
    RathArtifact,
    RathControl,
    RathTask,
    RathTaskEvent,
    RathWorkflow,
)
from app.tools.allowlist import sanitize_tool_allowlist


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _json_loads(raw: str | None, fallback: Any) -> Any:
    if raw is None or raw == "":
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _row_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return {k: row[k] for k in row.keys()} if row is not None else None


class RathDAO:
    def __init__(self, db: DB) -> None:
        self._db = db

    @property
    def db(self) -> DB:
        return self._db

    # ---------------------------------------------------------------- workflows
    async def upsert_workflow(
        self,
        *,
        slug: str,
        name: str,
        description: str = "",
        kind: str = "",
        config: dict[str, Any] | None = None,
        enabled: bool = True,
        workflow_uuid: str | None = None,
    ) -> str:
        ts = now_ts()
        existing = await self.workflow_by_slug(slug, include_disabled=True)
        if existing is not None:
            await self._db.conn.execute(
                """
                UPDATE rath_workflows
                SET name=?, description=?, kind=?, enabled=?, config_json=?, updated_at=?
                WHERE slug=?
                """,
                (name, description, kind, 1 if enabled else 0, _json_dumps(config or {}), ts, slug),
            )
            await self._db.conn.commit()
            return existing.workflow_uuid
        wid = workflow_uuid or _new_uuid()
        await self._db.conn.execute(
            """
            INSERT INTO rath_workflows (
              workflow_uuid, slug, name, description, kind, enabled, config_json, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (wid, slug, name, description, kind, 1 if enabled else 0, _json_dumps(config or {}), ts, ts),
        )
        await self._db.conn.commit()
        return wid

    async def workflow_by_slug(self, slug: str, *, include_disabled: bool = False) -> RathWorkflow | None:
        sql = "SELECT * FROM rath_workflows WHERE slug=?"
        params: tuple[Any, ...] = (slug,)
        if not include_disabled:
            sql += " AND enabled=1"
        cur = await self._db.conn.execute(sql, params)
        return self._workflow_from_row(await cur.fetchone())

    async def workflow_by_uuid(self, workflow_uuid: str) -> RathWorkflow | None:
        cur = await self._db.conn.execute("SELECT * FROM rath_workflows WHERE workflow_uuid=?", (workflow_uuid,))
        return self._workflow_from_row(await cur.fetchone())

    async def list_workflows(self, *, include_disabled: bool = False) -> list[RathWorkflow]:
        sql = "SELECT * FROM rath_workflows"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY slug"
        cur = await self._db.conn.execute(sql)
        return [self._workflow_from_row(r) for r in await cur.fetchall() if r is not None]

    def _workflow_from_row(self, row: aiosqlite.Row | None) -> RathWorkflow | None:
        d = _row_dict(row)
        if d is None:
            return None
        return RathWorkflow(
            id=int(d["id"]),
            workflow_uuid=d["workflow_uuid"],
            slug=d["slug"],
            name=d["name"],
            description=d["description"] or "",
            kind=d["kind"] or "",
            enabled=bool(d["enabled"]),
            config=_json_loads(d["config_json"], {}),
            created_at=int(d["created_at"] or 0),
            updated_at=int(d["updated_at"] or 0),
        )

    # ---------------------------------------------------------------- agents
    async def create_agent(
        self,
        *,
        agent_key: str,
        name: str,
        description: str = "",
        system_prompt: str = "",
        model: str = "",
        think_level: str = "",
        tool_allowlist: list[str] | None = None,
        sort: int = 0,
        enabled: bool = True,
    ) -> int:
        ts = now_ts()
        cur = await self._db.conn.execute(
            """
            INSERT INTO rath_agents (
              agent_key, name, description, system_prompt, model,
              think_level, tool_allowlist_json, sort,
              enabled, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                agent_key,
                name,
                description,
                system_prompt,
                model,
                think_level,
                _json_dumps(sanitize_tool_allowlist(tool_allowlist or [])),
                int(sort or 0),
                1 if enabled else 0,
                ts,
                ts,
            ),
        )
        await self._db.conn.commit()
        return int(cur.lastrowid or 0)

    async def update_agent(
        self,
        agent_id: int,
        *,
        agent_key: str | None = None,
        name: str | None = None,
        description: str | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        think_level: str | None = None,
        tool_allowlist: list[str] | None = None,
        sort: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        fields: list[str] = ["updated_at=?"]
        params: list[Any] = [now_ts()]
        updates = {
            "agent_key": agent_key,
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "model": model,
            "think_level": think_level,
            "sort": sort,
        }
        for col, val in updates.items():
            if val is not None:
                fields.append(f"{col}=?")
                params.append(val)
        if tool_allowlist is not None:
            fields.append("tool_allowlist_json=?")
            params.append(_json_dumps(sanitize_tool_allowlist(tool_allowlist)))
        if enabled is not None:
            fields.append("enabled=?")
            params.append(1 if enabled else 0)
        params.append(int(agent_id))
        await self._db.conn.execute(
            f"UPDATE rath_agents SET {', '.join(fields)} WHERE id=?",
            tuple(params),
        )
        await self._db.conn.commit()

    async def delete_agent(self, agent_id: int) -> None:
        await self._db.conn.execute("DELETE FROM rath_agents WHERE id=?", (int(agent_id),))
        await self._db.conn.commit()

    async def agent_by_id(self, agent_id: int, *, include_disabled: bool = True) -> RathAgentDef | None:
        sql = "SELECT * FROM rath_agents WHERE id=?"
        params: tuple[Any, ...] = (int(agent_id),)
        if not include_disabled:
            sql += " AND enabled=1"
        cur = await self._db.conn.execute(sql, params)
        return self._agent_from_row(await cur.fetchone())

    async def agent_by_key(self, agent_key: str, *, include_disabled: bool = False) -> RathAgentDef | None:
        sql = "SELECT * FROM rath_agents WHERE agent_key=?"
        params: tuple[Any, ...] = (agent_key,)
        if not include_disabled:
            sql += " AND enabled=1"
        cur = await self._db.conn.execute(sql, params)
        return self._agent_from_row(await cur.fetchone())

    async def list_agents(
        self,
        *,
        include_disabled: bool = False,
    ) -> list[RathAgentDef]:
        where: list[str] = []
        params: list[Any] = []
        if not include_disabled:
            where.append("enabled=1")
        sql = "SELECT * FROM rath_agents"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY sort ASC, id ASC"
        cur = await self._db.conn.execute(sql, tuple(params))
        return [self._agent_from_row(r) for r in await cur.fetchall() if r is not None]

    def _agent_from_row(self, row: aiosqlite.Row | None) -> RathAgentDef | None:
        d = _row_dict(row)
        if d is None:
            return None
        return RathAgentDef(
            id=int(d["id"]),
            agent_key=d["agent_key"],
            name=d["name"],
            description=d["description"] or "",
            system_prompt=d["system_prompt"] or "",
            model=d["model"] or "",
            think_level=d["think_level"] or "",
            tool_allowlist=sanitize_tool_allowlist(_json_loads(d["tool_allowlist_json"], [])),
            sort=int(d["sort"] or 0),
            enabled=bool(d["enabled"]),
            created_at=int(d["created_at"] or 0),
            updated_at=int(d["updated_at"] or 0),
            workflow_uuid=d.get("workflow_uuid", "") or "",
        )

    # ---------------------------------------------------------------- agent sessions
    async def get_or_create_agent_session(
        self,
        *,
        openbear_session_uuid: str,
        chat_id: int,
        workflow_uuid: str,
        agent_key: str,
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> RathAgentSession:
        openbear_session_uuid = str(openbear_session_uuid or "").strip()
        workflow_uuid = str(workflow_uuid or "").strip()
        agent_key = str(agent_key or "").strip()
        cur = await self._db.conn.execute(
            """
            SELECT * FROM rath_agent_sessions
            WHERE openbear_session_uuid=? AND workflow_uuid=? AND agent_key=? AND status='active'
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (openbear_session_uuid, workflow_uuid, agent_key),
        )
        existing = self._agent_session_from_row(await cur.fetchone())
        if existing is not None:
            return existing
        ts = now_ts()
        sid = _new_uuid()
        try:
            await self._db.conn.execute(
                """
                INSERT INTO rath_agent_sessions (
                  session_uuid, openbear_session_uuid, chat_id, workflow_uuid, agent_key,
                  status, title, summary, last_task_uuid,
                  metadata_json, created_at, updated_at, closed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sid,
                    openbear_session_uuid,
                    int(chat_id or 0),
                    workflow_uuid,
                    agent_key,
                    "active",
                    title or agent_key,
                    "",
                    "",
                    _json_dumps(metadata or {}),
                    ts,
                    ts,
                    0,
                ),
            )
            await self._db.conn.commit()
        except sqlite3.IntegrityError:
            # Another concurrent task created the same active session between
            # our SELECT and INSERT.  Re-read and reuse it.
            cur = await self._db.conn.execute(
                """
                SELECT * FROM rath_agent_sessions
                WHERE openbear_session_uuid=? AND workflow_uuid=? AND agent_key=? AND status='active'
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (openbear_session_uuid, workflow_uuid, agent_key),
            )
            existing = self._agent_session_from_row(await cur.fetchone())
            if existing is not None:
                return existing
            raise
        got = await self.agent_session(sid)
        if got is None:
            raise RuntimeError("failed to create Rath agent session")
        return got

    async def agent_session(self, session_uuid: str) -> RathAgentSession | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM rath_agent_sessions WHERE session_uuid=?",
            (session_uuid,),
        )
        return self._agent_session_from_row(await cur.fetchone())

    async def list_agent_sessions(
        self,
        *,
        openbear_session_uuid: str = "",
        chat_id: int | None = None,
        status: str = "",
        limit: int = 50,
    ) -> list[RathAgentSession]:
        where: list[str] = []
        params: list[Any] = []
        if openbear_session_uuid:
            where.append("openbear_session_uuid=?")
            params.append(openbear_session_uuid)
        if chat_id is not None:
            where.append("chat_id=?")
            params.append(int(chat_id))
        if status:
            where.append("status=?")
            params.append(status)
        sql = "SELECT * FROM rath_agent_sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        cur = await self._db.conn.execute(sql, tuple(params))
        return [self._agent_session_from_row(r) for r in await cur.fetchall() if r is not None]

    async def update_agent_session_after_task(
        self,
        session_uuid: str,
        *,
        task_uuid: str,
        summary_delta: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session = await self.agent_session(session_uuid)
        if session is None:
            return
        ts = now_ts()
        next_summary = session.summary
        if summary_delta.strip():
            addition = summary_delta.strip()
            base = next_summary.strip()
            next_summary = (base + "\n" if base else "") + f"- {addition}"
            if len(next_summary) > 6000:
                next_summary = next_summary[-6000:]
        merged_meta = dict(session.metadata or {})
        if metadata:
            merged_meta.update(metadata)
        await self._db.conn.execute(
            """
            UPDATE rath_agent_sessions
            SET summary=?, last_task_uuid=?, metadata_json=?, updated_at=?
            WHERE session_uuid=?
            """,
            (next_summary, task_uuid, _json_dumps(merged_meta), ts, session_uuid),
        )
        await self._db.conn.commit()

    async def close_agent_session(self, session_uuid: str, *, reason: str = "") -> None:
        session = await self.agent_session(session_uuid)
        if session is None:
            return
        meta = dict(session.metadata or {})
        if reason:
            meta["closeReason"] = reason
        ts = now_ts()
        await self._db.conn.execute(
            """
            UPDATE rath_agent_sessions
            SET status='closed', metadata_json=?, updated_at=?, closed_at=?
            WHERE session_uuid=?
            """,
            (_json_dumps(meta), ts, ts, session_uuid),
        )
        await self._db.conn.commit()

    async def close_agent_sessions_for_openbear_session(self, openbear_session_uuid: str, *, reason: str = "") -> int:
        sessions = await self.list_agent_sessions(openbear_session_uuid=openbear_session_uuid, status="active", limit=500)
        if not sessions:
            return 0
        ts = now_ts()
        for session in sessions:
            meta = dict(session.metadata or {})
            if reason:
                meta["closeReason"] = reason
            await self._db.conn.execute(
                """
                UPDATE rath_agent_sessions
                SET status='closed', metadata_json=?, updated_at=?, closed_at=?
                WHERE session_uuid=? AND status='active'
                """,
                (_json_dumps(meta), ts, ts, session.session_uuid),
            )
        await self._db.conn.commit()
        return len(sessions)

    def _agent_session_from_row(self, row: aiosqlite.Row | None) -> RathAgentSession | None:
        d = _row_dict(row)
        if d is None:
            return None
        return RathAgentSession(
            id=int(d["id"]),
            session_uuid=d["session_uuid"],
            openbear_session_uuid=d["openbear_session_uuid"] or "",
            chat_id=int(d["chat_id"] or 0),
            workflow_uuid=d["workflow_uuid"] or "",
            agent_key=d["agent_key"] or "",
            status=d["status"] or "active",
            title=d["title"] or "",
            summary=d["summary"] or "",
            last_task_uuid=d["last_task_uuid"] or "",
            metadata=_json_loads(d["metadata_json"], {}),
            created_at=int(d["created_at"] or 0),
            updated_at=int(d["updated_at"] or 0),
            closed_at=int(d["closed_at"] or 0),
        )

    # ---------------------------------------------------------------- tasks
    async def create_task(
        self,
        *,
        chat_id: int,
        workflow_uuid: str,
        title: str,
        input_data: dict[str, Any] | None = None,
        parent_session_uuid: str = "",
        agent_session_uuid: str = "",
        caller_agent_session_uuid: str = "",
        parent_task_uuid: str = "",
        status: str = "queued",
        task_uuid: str | None = None,
        turn_uuid: str = "",
        parent_turn_uuid: str = "",
        run_root_turn_uuid: str = "",
    ) -> str:
        ts = now_ts()
        tid = task_uuid or _new_uuid()
        turn = str(turn_uuid or "").strip()
        root_turn = str(run_root_turn_uuid or turn or "").strip()
        await self._db.conn.execute(
            """
            INSERT INTO rath_tasks (
              task_uuid, chat_id, parent_session_uuid, agent_session_uuid, caller_agent_session_uuid,
              parent_task_uuid, workflow_uuid, title, status, input_json, output_json, started_at, updated_at,
              turn_uuid, parent_turn_uuid, run_root_turn_uuid
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (tid, chat_id, parent_session_uuid, agent_session_uuid, caller_agent_session_uuid,
             str(parent_task_uuid or "").strip(), workflow_uuid, title, status, _json_dumps(input_data or {}), "{}", ts if status == "running" else 0, ts,
             turn, str(parent_turn_uuid or "").strip(), root_turn),
        )
        await self._db.conn.commit()
        await self.append_event(tid, "task_created", summary=f"任务已创建：{title}")
        return tid

    async def get_task(self, task_uuid: str) -> RathTask | None:
        cur = await self._db.conn.execute("SELECT * FROM rath_tasks WHERE task_uuid=?", (task_uuid,))
        return self._task_from_row(await cur.fetchone())

    async def save_task_model_context(
        self,
        task_uuid: str,
        *,
        protocol: str,
        model: str,
        session_id: str,
        state: dict[str, Any],
    ) -> int:
        """Durably checkpoint one task's private provider continuation state."""
        ts = now_ts()
        await self._db.conn.execute(
            """
            INSERT INTO rath_task_model_contexts (
              task_uuid, protocol, model, session_id, state_json, revision, created_at, updated_at
            ) VALUES (?,?,?,?,?,1,?,?)
            ON CONFLICT(task_uuid) DO UPDATE SET
              protocol=excluded.protocol,
              model=excluded.model,
              session_id=excluded.session_id,
              state_json=excluded.state_json,
              revision=rath_task_model_contexts.revision+1,
              updated_at=excluded.updated_at
            """,
            (
                str(task_uuid or ""),
                str(protocol or ""),
                str(model or ""),
                str(session_id or ""),
                _json_dumps(state),
                ts,
                ts,
            ),
        )
        await self._db.conn.commit()
        cur = await self._db.conn.execute(
            "SELECT revision FROM rath_task_model_contexts WHERE task_uuid=?",
            (task_uuid,),
        )
        row = await cur.fetchone()
        return int(row["revision"] or 0) if row else 0

    async def task_model_context(self, task_uuid: str) -> dict[str, Any] | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM rath_task_model_contexts WHERE task_uuid=?",
            (task_uuid,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        data = dict(row)
        return {
            "taskUuid": str(data.get("task_uuid") or ""),
            "protocol": str(data.get("protocol") or ""),
            "model": str(data.get("model") or ""),
            "sessionId": str(data.get("session_id") or ""),
            "state": _json_loads(data.get("state_json"), {}),
            "revision": int(data.get("revision") or 0),
            "createdAt": int(data.get("created_at") or 0),
            "updatedAt": int(data.get("updated_at") or 0),
        }

    async def clear_task_model_context(self, task_uuid: str) -> bool:
        cur = await self._db.conn.execute(
            "DELETE FROM rath_task_model_contexts WHERE task_uuid=?",
            (task_uuid,),
        )
        await self._db.conn.commit()
        return bool(cur.rowcount)

    async def active_task_for_chat(self, chat_id: int) -> RathTask | None:
        tasks = await self.active_tasks_for_chat(chat_id, limit=1)
        return tasks[0] if tasks else None

    async def active_tasks_for_chat(self, chat_id: int, *, limit: int = 50, controllable: bool = False) -> list[RathTask]:
        statuses = CONTROLLABLE_TASK_STATUSES if controllable else ACTIVE_TASK_STATUSES
        placeholders = ",".join("?" for _ in statuses)
        cur = await self._db.conn.execute(
            f"""
            SELECT * FROM rath_tasks
            WHERE chat_id=? AND status IN ({placeholders})
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (chat_id, *statuses, max(1, int(limit))),
        )
        return [self._task_from_row(r) for r in await cur.fetchall() if r is not None]

    async def task_uuids_for_chat(self, chat_id: int) -> list[str]:
        cur = await self._db.conn.execute(
            "SELECT task_uuid FROM rath_tasks WHERE chat_id=? ORDER BY id ASC",
            (int(chat_id or 0),),
        )
        return [str(row["task_uuid"] or "") for row in await cur.fetchall() if str(row["task_uuid"] or "")]

    async def delete_task_records(self, task_uuids: list[str] | tuple[str, ...]) -> dict[str, int]:
        """Delete selected Rath tasks and every task-owned child row.

        Caller owns the surrounding transaction/commit so conversation suffix
        deletion can remove model/UI/Agent state atomically.
        """
        ids = list(dict.fromkeys(str(item or "").strip() for item in task_uuids if str(item or "").strip()))
        empty = {
            "tasks": 0,
            "taskMemories": 0,
            "modelContexts": 0,
            "events": 0,
            "artifacts": 0,
            "controls": 0,
            "planStates": 0,
            "planVersions": 0,
            "planDecisions": 0,
            "planSteps": 0,
            "planEvidence": 0,
            "planRequests": 0,
        }
        if not ids:
            return empty
        placeholders = ",".join("?" for _ in ids)
        params = tuple(ids)
        deleted: dict[str, int] = {}
        task_memories = await self._db.conn.execute(
            f"DELETE FROM conversation_task_memories WHERE scope_type='agent_task' AND task_uuid IN ({placeholders})",
            params,
        )
        deleted["taskMemories"] = int(task_memories.rowcount or 0)
        for key, table in (
            ("modelContexts", "rath_task_model_contexts"),
            ("planEvidence", "rath_task_plan_evidence"),
            ("planRequests", "rath_task_plan_requests"),
            ("planDecisions", "rath_task_plan_decisions"),
            ("planSteps", "rath_task_plan_step_runs"),
            ("planVersions", "rath_task_plan_versions"),
            ("planStates", "rath_task_plan_state"),
            ("controls", "rath_task_controls"),
            ("artifacts", "rath_task_artifacts"),
            ("events", "rath_task_events"),
        ):
            cur = await self._db.conn.execute(
                f"DELETE FROM {table} WHERE task_uuid IN ({placeholders})",
                params,
            )
            deleted[key] = int(cur.rowcount or 0)
        tasks = await self._db.conn.execute(
            f"DELETE FROM rath_tasks WHERE task_uuid IN ({placeholders})",
            params,
        )
        deleted["tasks"] = int(tasks.rowcount or 0)
        return {**empty, **deleted}

    async def delete_task_records_for_chat(self, chat_id: int) -> dict[str, int]:
        """Delete Rath tasks and all task-owned child rows for a chat."""
        return await self.delete_task_records(await self.task_uuids_for_chat(chat_id))

    async def task_usage_totals(
        self,
        *,
        chat_id: int,
        parent_session_uuid: str = "",
        terminal_only: bool = True,
    ) -> dict[str, Any]:
        """Aggregate Rath task usage for status display.

        Read-only by design: /status recomputes this instead of writing any
        accounting marker, so refreshing status can never duplicate usage.
        """
        where = ["chat_id=?"]
        params: list[Any] = [chat_id]
        if parent_session_uuid:
            where.append("parent_session_uuid=?")
            params.append(parent_session_uuid)
        if terminal_only:
            placeholders = ",".join("?" for _ in TERMINAL_TASK_STATUSES)
            where.append(f"status IN ({placeholders})")
            params.extend(TERMINAL_TASK_STATUSES)
        sql = f"""
            SELECT COUNT(*) AS task_count,
                   SUM(COALESCE(model_call_count, 0)) AS model_call_count,
                   SUM(COALESCE(tool_call_count, 0)) AS tool_call_count,
                   SUM(COALESCE(work_tool_call_count, 0)) AS work_tool_call_count,
                   SUM(COALESCE(plan_tool_call_count, 0)) AS plan_tool_call_count,
                   SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                   SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                   SUM(COALESCE(cache_read_tokens, 0)) AS cache_read_tokens,
                   SUM(COALESCE(cache_write_tokens, 0)) AS cache_write_tokens,
                   SUM(COALESCE(cost_usd, 0)) AS cost_usd,
                   SUM(CASE WHEN COALESCE(finished_at, 0) > COALESCE(started_at, 0)
                            THEN (finished_at - started_at) * 1000 ELSE 0 END) AS total_time_ms
            FROM rath_tasks
            WHERE {' AND '.join(where)}
        """
        cur = await self._db.conn.execute(sql, tuple(params))
        row = await cur.fetchone()
        return dict(row) if row else {}

    async def list_tasks(self, *, chat_id: int | None = None, status: str = "", limit: int = 50) -> list[RathTask]:
        where: list[str] = []
        params: list[Any] = []
        if chat_id is not None:
            where.append("chat_id=?")
            params.append(chat_id)
        if status:
            where.append("status=?")
            params.append(status)
        sql = "SELECT * FROM rath_tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        cur = await self._db.conn.execute(sql, tuple(params))
        return [self._task_from_row(r) for r in await cur.fetchall() if r is not None]

    async def finalize_task_plan_terminal(
        self,
        conn: aiosqlite.Connection,
        task_uuid: str,
        status: str,
        *,
        updated_at: int | None = None,
    ) -> dict[str, int]:
        """Apply the task→Plan terminal invariant inside the caller transaction.

        Only failed/cancelled/interrupted are failure terminals for an executing
        Plan.  Completed steps and every non-running state/evidence/gate remain
        immutable; the helper merely closes orphan running work and clears the
        active pointer.  Conditional updates make startup repair idempotent.
        """
        terminal_status = str(status or "").strip()
        if terminal_status not in {"failed", "cancelled", "interrupted"}:
            return {"steps": 0, "states": 0}
        ts = int(updated_at or now_ts())
        steps = await conn.execute(
            """
            UPDATE rath_task_plan_step_runs
            SET status=?, updated_at=?, row_revision=row_revision+1
            WHERE task_uuid=? AND status='running'
            """,
            (terminal_status, ts, task_uuid),
        )
        states = await conn.execute(
            """
            UPDATE rath_task_plan_state
            SET phase=?, current_step_id='', row_revision=row_revision+1, updated_at=?
            WHERE task_uuid=?
              AND (phase<>? OR COALESCE(current_step_id, '')<>'')
            """,
            (terminal_status, ts, task_uuid, terminal_status),
        )
        return {
            "steps": max(0, int(steps.rowcount or 0)),
            "states": max(0, int(states.rowcount or 0)),
        }

    async def update_task(
        self,
        task_uuid: str,
        *,
        status: str | None = None,
        expected_statuses: tuple[str, ...] | list[str] | set[str] | None = None,
        control_state: str | None = None,
        current_agent_key: str | None = None,
        current_status: str | None = None,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        model_call_delta: int = 0,
        tool_call_delta: int = 0,
        work_tool_call_delta: int = 0,
        plan_tool_call_delta: int = 0,
        input_tokens_delta: int = 0,
        output_tokens_delta: int = 0,
        cache_read_tokens_delta: int = 0,
        cache_write_tokens_delta: int = 0,
        last_input_tokens: int | None = None,
        last_output_tokens: int | None = None,
        last_cache_read_tokens: int | None = None,
        last_cache_write_tokens: int | None = None,
        cost_usd_delta: float = 0.0,
        finish: bool = False,
    ) -> bool:
        ts = now_ts()
        fields: list[str] = ["updated_at=?"]
        params: list[Any] = [ts]
        if status is not None:
            fields.append("status=?")
            params.append(status)
            if status == "running":
                fields.append("started_at=CASE WHEN started_at=0 THEN ? ELSE started_at END")
                params.append(ts)
        if control_state is not None:
            fields.append("control_state=?")
            params.append(control_state)
        if current_agent_key is not None:
            fields.append("current_agent_key=?")
            params.append(current_agent_key)
        if current_status is not None:
            fields.append("current_status=?")
            params.append(current_status)
        if output is not None:
            fields.append("output_json=?")
            params.append(_json_dumps(output))
        if error is not None:
            fields.append("error=?")
            params.append(error)
        counters = {
            "model_call_count": model_call_delta,
            "tool_call_count": tool_call_delta,
            "work_tool_call_count": work_tool_call_delta,
            "plan_tool_call_count": plan_tool_call_delta,
            "input_tokens": input_tokens_delta,
            "output_tokens": output_tokens_delta,
            "cache_read_tokens": cache_read_tokens_delta,
            "cache_write_tokens": cache_write_tokens_delta,
        }
        for col, delta in counters.items():
            if delta:
                fields.append(f"{col}={col}+?")
                params.append(int(delta))
        last_values = {
            "last_input_tokens": last_input_tokens,
            "last_output_tokens": last_output_tokens,
            "last_cache_read_tokens": last_cache_read_tokens,
            "last_cache_write_tokens": last_cache_write_tokens,
        }
        for col, value in last_values.items():
            if value is not None:
                fields.append(f"{col}=?")
                params.append(int(value or 0))
        if cost_usd_delta:
            fields.append("cost_usd=cost_usd+?")
            params.append(float(cost_usd_delta))
        if finish:
            fields.append("finished_at=?")
            params.append(ts)
        where = "task_uuid=?"
        params.append(task_uuid)
        if expected_statuses is not None:
            allowed = tuple(dict.fromkeys(str(item) for item in expected_statuses if str(item)))
            if not allowed:
                return False
            where += f" AND status IN ({','.join('?' for _ in allowed)})"
            params.extend(allowed)
        else:
            # Terminal Rath rows are immutable by default. Callers that truly
            # need an administrative same-state enrichment must opt in with an
            # explicit expected_statuses CAS; late runner progress can no longer
            # alter terminal output, counters, errors, or status accidentally.
            terminal = tuple(TERMINAL_TASK_STATUSES)
            where += f" AND status NOT IN ({','.join('?' for _ in terminal)})"
            params.extend(terminal)
        sql = f"UPDATE rath_tasks SET {', '.join(fields)} WHERE {where}"
        if str(status or "") in {"failed", "cancelled", "interrupted"}:
            # The task CAS and its Plan terminal projection are one commit unit.
            # Every manager/runner/Agent path already funnels through update_task,
            # so centralizing here also covers stale cleanup and stop controls.
            async with self._db.plan_transaction() as conn:
                cur = await conn.execute(sql, tuple(params))
                if cur.rowcount:
                    await self.finalize_task_plan_terminal(
                        conn,
                        task_uuid,
                        str(status or ""),
                        updated_at=ts,
                    )
                return bool(cur.rowcount)
        cur = await self._db.conn.execute(sql, tuple(params))
        await self._db.conn.commit()
        return bool(cur.rowcount)

    async def mark_interrupted_running(self) -> int:
        """Interrupt startup-active tasks and repair legacy terminal Plan orphans."""
        ts = now_ts()
        placeholders = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
        async with self._db.plan_transaction() as conn:
            cur = await conn.execute(
                f"""
                UPDATE rath_tasks
                SET status='interrupted', error=CASE
                  WHEN COALESCE(error, '') = '' THEN 'interrupted by OpenBear startup'
                  ELSE error
                END, updated_at=?, finished_at=?
                WHERE status IN ({placeholders})
                """,
                (ts, ts, *ACTIVE_TASK_STATUSES),
            )
            interrupted = int(cur.rowcount or 0)
            terminal_rows = await conn.execute(
                """
                SELECT task_uuid, status FROM rath_tasks
                WHERE status IN ('failed','cancelled','interrupted')
                """
            )
            for row in await terminal_rows.fetchall():
                await self.finalize_task_plan_terminal(
                    conn,
                    str(row["task_uuid"] or ""),
                    str(row["status"] or ""),
                    updated_at=ts,
                )
            return interrupted

    def _task_from_row(self, row: aiosqlite.Row | None) -> RathTask | None:
        d = _row_dict(row)
        if d is None:
            return None
        return RathTask(
            id=int(d["id"]),
            task_uuid=d["task_uuid"],
            chat_id=int(d["chat_id"] or 0),
            parent_session_uuid=d["parent_session_uuid"] or "",
            agent_session_uuid=d["agent_session_uuid"] or "",
            caller_agent_session_uuid=d["caller_agent_session_uuid"] or "",
            workflow_uuid=d["workflow_uuid"] or "",
            title=d["title"] or "",
            status=d["status"] or "queued",
            control_state=d["control_state"] or "",
            current_agent_key=d["current_agent_key"] or "",
            current_status=d["current_status"] or "",
            input=_json_loads(d["input_json"], {}),
            output=_json_loads(d["output_json"], {}),
            error=d["error"] or "",
            model_call_count=int(d["model_call_count"] or 0),
            tool_call_count=int(d["tool_call_count"] or 0),
            work_tool_call_count=int(d.get("work_tool_call_count") or 0),
            plan_tool_call_count=int(d.get("plan_tool_call_count") or 0),
            input_tokens=int(d["input_tokens"] or 0),
            output_tokens=int(d["output_tokens"] or 0),
            cache_read_tokens=int(d["cache_read_tokens"] or 0),
            cache_write_tokens=int(d["cache_write_tokens"] or 0),
            last_input_tokens=int(d["last_input_tokens"] or 0),
            last_output_tokens=int(d["last_output_tokens"] or 0),
            last_cache_read_tokens=int(d["last_cache_read_tokens"] or 0),
            last_cache_write_tokens=int(d["last_cache_write_tokens"] or 0),
            cost_usd=float(d["cost_usd"] or 0.0),
            started_at=int(d["started_at"] or 0),
            updated_at=int(d["updated_at"] or 0),
            finished_at=int(d["finished_at"] or 0),
            parent_task_uuid=str(d.get("parent_task_uuid") or ""),
            turn_uuid=str(d.get("turn_uuid") or ""),
            parent_turn_uuid=str(d.get("parent_turn_uuid") or ""),
            run_root_turn_uuid=str(d.get("run_root_turn_uuid") or ""),
        )

    # ---------------------------------------------------------------- events
    async def append_event(
        self,
        task_uuid: str,
        kind: str,
        *,
        agent_key: str = "",
        summary: str = "",
        detail: dict[str, Any] | None = None,
        elapsed_ms: int = 0,
    ) -> int:
        # Allocate and insert on the physical writer in one SQLite statement.
        # A SELECT routed through the shared reader can observe a pinned, stale WAL
        # snapshot and reuse an already committed seq, so MAX(seq) must not be read
        # separately from the INSERT.
        async with self._db.write_transaction(label="rath_event") as conn:
            cur = await conn.execute(
                """
                INSERT INTO rath_task_events (
                  task_uuid, seq, ts, kind, agent_key, summary, detail_json, elapsed_ms
                )
                SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?, ?
                FROM rath_task_events
                WHERE task_uuid=?
                RETURNING seq
                """,
                (
                    task_uuid,
                    now_ts(),
                    kind,
                    agent_key,
                    summary,
                    _json_dumps(detail or {}),
                    int(elapsed_ms or 0),
                    task_uuid,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError(f"Rath event insert returned no seq: {task_uuid}")
            return int(row["seq"])

    async def events(self, task_uuid: str, *, after_seq: int = 0, limit: int = 200) -> list[RathTaskEvent]:
        limit = max(1, int(limit))
        after_seq = int(after_seq)
        if after_seq > 0:
            # Incremental polling: preserve the original contract, all events
            # newer than after_seq in natural order.
            cur = await self._db.conn.execute(
                """
                SELECT * FROM rath_task_events
                WHERE task_uuid=? AND seq>?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (task_uuid, after_seq, limit),
            )
        else:
            # Status cards want the recent tail, not the first N events.  Fetch
            # latest N by descending seq, then re-sort ascending for readable
            # chronological display.
            cur = await self._db.conn.execute(
                """
                SELECT * FROM (
                  SELECT * FROM rath_task_events
                  WHERE task_uuid=?
                  ORDER BY seq DESC
                  LIMIT ?
                )
                ORDER BY seq ASC
                """,
                (task_uuid, limit),
            )
        return [self._event_from_row(r) for r in await cur.fetchall() if r is not None]

    async def events_before(
        self,
        task_uuid: str,
        *,
        before_seq: int = 0,
        limit: int = 20,
    ) -> tuple[list[RathTaskEvent], int, bool]:
        """Return one chronological page ending before ``before_seq``.

        ``before_seq=0`` selects the newest page.  The extra row is used only
        to determine whether an older page exists; it is never returned.
        """
        limit = max(1, min(100, int(limit)))
        before_seq = max(0, int(before_seq))
        cur = await self._db.conn.execute(
            "SELECT COUNT(*) AS total FROM rath_task_events WHERE task_uuid=?",
            (task_uuid,),
        )
        total_row = await cur.fetchone()
        total = int(total_row["total"] or 0) if total_row is not None else 0
        if before_seq > 0:
            cur = await self._db.conn.execute(
                """
                SELECT * FROM rath_task_events
                WHERE task_uuid=? AND seq<?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (task_uuid, before_seq, limit + 1),
            )
        else:
            cur = await self._db.conn.execute(
                """
                SELECT * FROM rath_task_events
                WHERE task_uuid=?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (task_uuid, limit + 1),
            )
        rows = await cur.fetchall()
        has_more = len(rows) > limit
        selected = list(reversed(rows[:limit]))
        events = [self._event_from_row(row) for row in selected]
        return [event for event in events if event is not None], total, has_more

    @staticmethod
    def _normalize_compaction_event_detail(kind: str, value: Any) -> dict[str, Any]:
        detail = dict(value) if isinstance(value, dict) else {}
        statuses = {
            "model_context_pre_compacted": "pre_compacted",
            "model_context_overflow_compacted": "overflow_compacted",
            "model_context_compaction_failed": "failed",
        }
        status = statuses.get(str(kind or ""))
        if status is None:
            return detail
        detail.setdefault("scope", "agent")
        detail.setdefault("source", "legacy_event")
        detail.setdefault("status", status)
        detail.setdefault("beforeTokens", int(detail.get("estimatedTokensBefore") or 0))
        detail.setdefault("afterTokens", int(detail.get("estimatedTokensAfter") or 0))
        output = detail.get("compactedOutput")
        if isinstance(output, str) and output:
            detail["outputAvailable"] = True
            detail.setdefault("summaryChars", len(output))
            detail.pop("outputUnavailable", None)
        else:
            detail.pop("compactedOutput", None)
            detail["outputAvailable"] = False
            detail.setdefault("summaryChars", 0)
            detail.setdefault(
                "outputUnavailable",
                "summary_not_available" if status == "failed" else "historical_summary_not_stored",
            )
        return detail

    def _event_from_row(self, row: aiosqlite.Row | None) -> RathTaskEvent | None:
        d = _row_dict(row)
        if d is None:
            return None
        kind = str(d["kind"] or "")
        detail = self._normalize_compaction_event_detail(kind, _json_loads(d["detail_json"], {}))
        return RathTaskEvent(
            id=int(d["id"]),
            task_uuid=d["task_uuid"],
            seq=int(d["seq"]),
            ts=int(d["ts"] or 0),
            kind=kind,
            agent_key=d["agent_key"] or "",
            summary=d["summary"] or "",
            detail=detail,
            elapsed_ms=int(d["elapsed_ms"] or 0),
        )

    # ---------------------------------------------------------------- artifacts
    async def create_artifact(
        self,
        task_uuid: str,
        *,
        kind: str,
        name: str,
        content: str,
        agent_key: str = "",
        summary: str = "",
        content_type: str = "text/plain",
        source_refs: list[Any] | None = None,
        artifact_uuid: str | None = None,
    ) -> str:
        aid = artifact_uuid or _new_uuid()
        payload = content or ""
        await self._db.conn.execute(
            """
            INSERT INTO rath_task_artifacts (
              artifact_uuid, task_uuid, agent_key, kind, name, summary, content,
              content_type, source_refs_json, size_bytes, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (aid, task_uuid, agent_key, kind, name, summary, payload, content_type,
             _json_dumps(source_refs or []), len(payload.encode("utf-8")), now_ts()),
        )
        await self._db.conn.commit()
        await self.append_event(
            task_uuid,
            "artifact_created",
            agent_key=agent_key,
            summary=summary or f"产物已创建：{name}",
            detail={"artifactUuid": aid, "kind": kind, "name": name},
        )
        return aid

    async def artifacts(self, task_uuid: str, *, kind: str = "") -> list[RathArtifact]:
        params: list[Any] = [task_uuid]
        sql = "SELECT * FROM rath_task_artifacts WHERE task_uuid=?"
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY id ASC"
        cur = await self._db.conn.execute(sql, tuple(params))
        return [self._artifact_from_row(r) for r in await cur.fetchall() if r is not None]

    def _artifact_from_row(self, row: aiosqlite.Row | None) -> RathArtifact | None:
        d = _row_dict(row)
        if d is None:
            return None
        return RathArtifact(
            id=int(d["id"]),
            artifact_uuid=d["artifact_uuid"],
            task_uuid=d["task_uuid"],
            agent_key=d["agent_key"] or "",
            kind=d["kind"] or "",
            name=d["name"] or "",
            summary=d["summary"] or "",
            content=d["content"] or "",
            content_type=d["content_type"] or "text/plain",
            source_refs=_json_loads(d["source_refs_json"], []),
            size_bytes=int(d["size_bytes"] or 0),
            created_at=int(d["created_at"] or 0),
        )

    # ---------------------------------------------------------------- controls
    async def add_control(
        self,
        task_uuid: str,
        action: str,
        *,
        message: str = "",
        requested_by: str = "",
        metadata: dict[str, Any] | None = None,
        control_uuid: str | None = None,
    ) -> str:
        cid = control_uuid or _new_uuid()
        await self._db.conn.execute(
            """
            INSERT INTO rath_task_controls (
              control_uuid, task_uuid, action, message, requested_by, status, created_at, metadata_json
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (cid, task_uuid, action, message, requested_by, "pending", now_ts(), _json_dumps(metadata or {})),
        )
        await self._db.conn.commit()
        await self.append_event(
            task_uuid,
            "control_requested",
            summary=f"控制请求：{action}",
            detail={
                "controlUuid": cid,
                "action": action,
                "message": message,
                "requestedBy": requested_by,
                "metadata": metadata or {},
            },
        )
        return cid

    async def pending_controls(self, task_uuid: str) -> list[RathControl]:
        cur = await self._db.conn.execute(
            """
            SELECT * FROM rath_task_controls
            WHERE task_uuid=? AND status='pending'
            ORDER BY id ASC
            """,
            (task_uuid,),
        )
        return [self._control_from_row(r) for r in await cur.fetchall() if r is not None]

    async def mark_control(self, control_uuid: str, status: str, *, result: str = "") -> None:
        await self._db.conn.execute(
            """
            UPDATE rath_task_controls
            SET status=?, applied_at=?, result=?
            WHERE control_uuid=?
            """,
            (status, now_ts(), result, control_uuid),
        )
        await self._db.conn.commit()

    async def control(self, control_uuid: str) -> RathControl | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM rath_task_controls WHERE control_uuid=?",
            (control_uuid,),
        )
        return self._control_from_row(await cur.fetchone())

    async def mark_control_response(
        self,
        control_uuid: str,
        *,
        response_status: str,
        reason: str = "",
        plan_impact: str = "",
        next_action: str = "",
    ) -> bool:
        cur = await self._db.conn.execute(
            """
            UPDATE rath_task_controls
            SET response_status=?, response_reason=?, response_plan_impact=?,
                response_next_action=?, responded_at=?
            WHERE control_uuid=? AND status='applied' AND responded_at=0
            """,
            (response_status, reason, plan_impact, next_action, now_ts(), control_uuid),
        )
        await self._db.conn.commit()
        return int(cur.rowcount or 0) == 1

    def _control_from_row(self, row: aiosqlite.Row | None) -> RathControl | None:
        d = _row_dict(row)
        if d is None:
            return None
        return RathControl(
            id=int(d["id"]),
            control_uuid=d["control_uuid"],
            task_uuid=d["task_uuid"],
            action=d["action"],
            message=d["message"] or "",
            requested_by=d["requested_by"] or "",
            status=d["status"] or "pending",
            created_at=int(d["created_at"] or 0),
            applied_at=int(d["applied_at"] or 0),
            result=d["result"] or "",
            metadata=_json_loads(d.get("metadata_json"), {}),
            response_status=d.get("response_status") or "",
            response_reason=d.get("response_reason") or "",
            response_plan_impact=d.get("response_plan_impact") or "",
            response_next_action=d.get("response_next_action") or "",
            responded_at=int(d.get("responded_at") or 0),
        )
