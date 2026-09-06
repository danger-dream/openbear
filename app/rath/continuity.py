"""Independent Agent identity and atomic ownership of successive task rounds.

Rath tasks remain immutable execution records. A session's context head points
at the existing private checkpoint store, never at a concatenation of presets.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.db.engine import now_ts
from app.rath.schemas import TERMINAL_TASK_STATUSES, RathAgentSession


class AgentContinuityError(ValueError):
    def __init__(self, code: str, message: str, *, task_uuid: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.task_uuid = task_uuid

    def public(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": str(self), "taskUuid": self.task_uuid}


def agent_session_public(session: RathAgentSession) -> dict[str, Any]:
    independent = session.session_kind == "independent"
    blocker = (
        "legacy_group_requires_explicit_task" if not independent else
        "instance_closed" if session.status != "active" else
        "instance_busy" if session.active_task_uuid else
        "context_unavailable" if not session.context_task_uuid else ""
    )
    return {
        "sessionUuid": session.session_uuid, "agentId": session.session_uuid,
        "openbearSessionUuid": session.openbear_session_uuid,
        "agentKey": session.agent_key, "sessionKind": session.session_kind,
        "status": session.status, "title": session.title, "summary": session.summary,
        "lastTaskUuid": session.last_task_uuid, "activeTaskUuid": session.active_task_uuid,
        "contextTaskUuid": session.context_task_uuid, "contextRevision": session.context_revision,
        "revision": session.revision, "turnCount": session.turn_count,
        "canContinue": not blocker, "continuationBlocker": blocker,
    }


class AgentContinuityDAO:
    """Persistence methods mixed into RathDAO; uses its single-writer transactions."""

    async def create_agent_instance(
        self, *, openbear_session_uuid: str, chat_id: int, workflow_uuid: str,
        agent_key: str, title: str = "", metadata: dict[str, Any] | None = None,
    ) -> RathAgentSession:
        sid = str(uuid.uuid4())
        ts = now_ts()
        await self._db.conn.execute(
            """INSERT INTO rath_agent_sessions
               (session_uuid, session_kind, openbear_session_uuid, chat_id, workflow_uuid,
                agent_key, status, title, metadata_json, created_at, updated_at)
               VALUES (?, 'independent', ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (sid, openbear_session_uuid, chat_id, workflow_uuid, agent_key, title or agent_key,
             json.dumps(metadata or {}, ensure_ascii=False), ts, ts),
        )
        await self._db.conn.commit()
        return await self.agent_session(sid)

    async def freeze_agent_instance(self, session_uuid: str, **values: Any) -> None:
        """Set missing immutable instance metadata without discarding prior fields."""
        async with self._db.write_transaction(label="agent_instance_snapshot") as conn:
            cur = await conn.execute("SELECT metadata_json FROM rath_agent_sessions WHERE session_uuid=?", (session_uuid,))
            row = await cur.fetchone()
            if row is None:
                raise AgentContinuityError("agent_instance_not_found", "Agent instance not found")
            metadata = json.loads(row[0] or "{}")
            for key, value in values.items():
                metadata.setdefault(key, value)
            await conn.execute("UPDATE rath_agent_sessions SET metadata_json=? WHERE session_uuid=?",
                               (json.dumps(metadata, ensure_ascii=False), session_uuid))

    async def adopt_legacy_agent_task(self, task_uuid: str, *, preset_tool_ceiling: list[str] | None = None) -> RathAgentSession:
        """Adopt exactly one terminal legacy task, retaining memory UUIDs and results."""
        async with self._db.write_transaction(label="agent_legacy_adopt") as conn:
            cur = await conn.execute("SELECT * FROM rath_tasks WHERE task_uuid=?", (task_uuid,))
            task = await cur.fetchone()
            if task is None:
                raise AgentContinuityError("agent_task_not_found", "Source task not found")
            cur = await conn.execute("SELECT * FROM rath_agent_sessions WHERE session_uuid=?", (task["agent_session_uuid"],))
            existing = self._agent_session_from_row(await cur.fetchone())
            if existing and existing.session_kind == "independent":
                return existing
            if task["status"] not in TERMINAL_TASK_STATUSES:
                raise AgentContinuityError("agent_instance_busy", "Finish or stop the original task before adopting it", task_uuid=task_uuid)
            cur = await conn.execute("SELECT * FROM rath_task_model_contexts WHERE task_uuid=?", (task_uuid,))
            checkpoint = await cur.fetchone()
            if checkpoint is None:
                raise AgentContinuityError("agent_context_unavailable", "No reliable private checkpoint exists; a report alone is not full continuation")
            sid = str(uuid.uuid4())
            ts = now_ts()
            data = json.loads(task["input_json"] or "{}")
            metadata = {
                "legacyAgentSessionUuid": task["agent_session_uuid"],
                "originTaskUuid": task_uuid, "agentSnapshot": data.get("agentSnapshot") or {},
                "llmSessionId": checkpoint["session_id"],
                "presetToolCeiling": list(preset_tool_ceiling if preset_tool_ceiling is not None else data.get("presetToolCeiling", [])),
            }
            # Old checkpoints did not freeze the base template; expose that fact.
            metadata["legacyTemplateUnpinned"] = True
            await conn.execute(
                """INSERT INTO rath_agent_sessions
                   (session_uuid,session_kind,openbear_session_uuid,chat_id,workflow_uuid,agent_key,
                    status,title,last_task_uuid,context_task_uuid,context_revision,turn_count,
                    revision,metadata_json,created_at,updated_at)
                   VALUES (?,'independent',?,?,?,?, 'active',?,?,?,?,1,1,?,?,?)""",
                (sid, task["parent_session_uuid"], task["chat_id"], task["workflow_uuid"],
                 task["current_agent_key"] or (data.get("agentSnapshot") or {}).get("agentKey", "general-purpose"),
                 task["title"], task_uuid, task_uuid, checkpoint["revision"],
                 json.dumps(metadata, ensure_ascii=False), ts, ts),
            )
            data.update(sessionKind="independent", sessionTurn=1, legacyAgentSessionUuid=task["agent_session_uuid"])
            await conn.execute("UPDATE rath_tasks SET agent_session_uuid=?,input_json=? WHERE task_uuid=?",
                               (sid, json.dumps(data, ensure_ascii=False), task_uuid))
            # The legacy task_uuid storage column is the scoped owner key for
            # agent_session rows. source_run_uuid retains the creating task.
            await conn.execute(
                """UPDATE conversation_task_memories SET scope_type='agent_session',task_uuid=?
                   WHERE conversation_uuid=? AND scope_type='agent_task' AND task_uuid=?""",
                (sid, task["parent_session_uuid"], task_uuid),
            )
        return await self.agent_session(sid)

    async def _create_instance_task(self, *, task_uuid: str, session_uuid: str, values: dict[str, Any]) -> str:
        async with self._db.write_transaction(label="agent_instance_task") as conn:
            cur = await conn.execute("SELECT * FROM rath_agent_sessions WHERE session_uuid=?", (session_uuid,))
            session = self._agent_session_from_row(await cur.fetchone())
            if session is None or session.status != "active":
                raise AgentContinuityError("agent_instance_closed", "The Agent instance is not active")
            data = dict(values["input"])
            request_id = str(data.get("continueRequestId") or "")
            if request_id:
                cur = await conn.execute(
                    """SELECT task_uuid,input_json FROM rath_tasks WHERE agent_session_uuid=?
                       AND json_extract(input_json,'$.continueRequestId')=? LIMIT 1""", (session_uuid, request_id),
                )
                replay = await cur.fetchone()
                if replay:
                    old = json.loads(replay["input_json"] or "{}")
                    for key in ("originalPrompt", "planMode", "requestedTools", "attachmentRefs"):
                        if old.get(key) != data.get(key):
                            raise AgentContinuityError("agent_continue_idempotency_conflict", "The request id already belongs to a different instruction")
                    raise AgentContinuityError("agent_continue_replayed", "The continuation request was already accepted", task_uuid=str(replay["task_uuid"]))
            if session.active_task_uuid:
                raise AgentContinuityError("agent_instance_busy", "This instance already owns an unfinished task", task_uuid=session.active_task_uuid)
            source = data.get("contextSource") or {}
            if session.turn_count:
                if source.get("taskUuid") != session.context_task_uuid or int(source.get("revision") or 0) != session.context_revision:
                    raise AgentContinuityError("agent_context_changed", "The instance context changed; inspect it before continuing")
                cur = await conn.execute("SELECT revision FROM rath_task_model_contexts WHERE task_uuid=?", (session.context_task_uuid,))
                checkpoint = await cur.fetchone()
                if not checkpoint or int(checkpoint[0]) != session.context_revision:
                    raise AgentContinuityError("agent_context_unavailable", "The authoritative continuation checkpoint is unavailable")
            data.update(sessionKind="independent", sessionTurn=session.turn_count + 1)
            ts = now_ts()
            await conn.execute(
                """INSERT INTO rath_tasks
                   (task_uuid,chat_id,parent_session_uuid,agent_session_uuid,caller_agent_session_uuid,
                    parent_task_uuid,workflow_uuid,title,status,input_json,output_json,started_at,updated_at,
                    turn_uuid,parent_turn_uuid,run_root_turn_uuid)
                   VALUES (?,?,?,?,?,?,?,?,?,?, '{}',?,?,?,?,?)""",
                (task_uuid, values["chat_id"], values["parent_session_uuid"], session_uuid,
                 values["caller_agent_session_uuid"], values["parent_task_uuid"], values["workflow_uuid"],
                 values["title"], values["status"], json.dumps(data, ensure_ascii=False),
                 ts if values["status"] == "running" else 0, ts, values["turn_uuid"],
                 values["parent_turn_uuid"], values["run_root_turn_uuid"]),
            )
            await conn.execute(
                """UPDATE rath_agent_sessions SET active_task_uuid=?,turn_count=turn_count+1,
                   revision=revision+1,updated_at=? WHERE session_uuid=?""", (task_uuid, ts, session_uuid),
            )
        await self.append_event(task_uuid, "task_created", summary=f"任务已创建：{values['title']}")
        return task_uuid

    async def agent_instance_tasks(self, session_uuid: str, *, limit: int = 200) -> list[Any]:
        cur = await self._db.conn.execute(
            "SELECT * FROM rath_tasks WHERE agent_session_uuid=? ORDER BY id ASC LIMIT ?",
            (session_uuid, max(1, min(limit, 1000))),
        )
        return [self._task_from_row(row) for row in await cur.fetchall()]

    async def release_agent_task(self, conn: Any, task_uuid: str, ts: int) -> None:
        await conn.execute(
            """UPDATE rath_agent_sessions SET active_task_uuid='',last_task_uuid=?,
               revision=revision+1,updated_at=? WHERE session_kind='independent' AND active_task_uuid=?""",
            (task_uuid, ts, task_uuid),
        )
