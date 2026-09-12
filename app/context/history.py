"""Read-only Controller execution view, with the same paging contract as Agents."""
from __future__ import annotations

from typing import Any

from app.agent.native_continuation import serialize_messages
from app.context.store import ContextHistoryUnavailable, ContextOwner, WindowStore
from app.context.window import neutral_context
from app.db.dao import MessageDAO
from app.db.engine import DB


class ControllerExecutionHistory:
    def __init__(self, db: DB, *, chat_id: int, session_uuid: str,
                 root_turn_uuid: str = "", exclude_turn_uuid: str = "") -> None:
        self.db = db
        self.chat_id = chat_id
        self.root_turn_uuid = root_turn_uuid
        self.exclude_turn_uuid = exclude_turn_uuid
        self.store = WindowStore(db, ContextOwner.controller(chat_id=chat_id, session_uuid=session_uuid))

    async def index(self, *, after: int = 0, high_water: int | None = None,
                    limit: int = 30, query: str = "") -> dict[str, Any]:
        cur = await self.db.conn.execute("SELECT COALESCE(MAX(id),0) AS high_water FROM messages WHERE chat_id=?", (self.chat_id,))
        bound = int((await cur.fetchone())["high_water"])
        if high_water is not None:
            if high_water < 0 or high_water > bound:
                raise ValueError("invalid_history_high_water")
            bound = high_water
        where = "chat_id=? AND id>? AND id<=?"
        params: list[Any] = [self.chat_id, after, bound]
        if self.root_turn_uuid:
            where += " AND (run_root_turn_uuid=? OR turn_uuid=?)"
            params += [self.root_turn_uuid, self.root_turn_uuid]
        if self.exclude_turn_uuid:
            where += " AND run_root_turn_uuid!=? AND turn_uuid!=?"
            params += [self.exclude_turn_uuid, self.exclude_turn_uuid]
        if query:
            where += """ AND (instr(content,?)>0 OR instr(tool_calls_json,?)>0 OR instr(name,?)>0
                OR EXISTS (SELECT 1 FROM json_each(COALESCE(NULLIF(tool_calls_json,''),'[]')) call
                           WHERE instr(json_extract(call.value,'$.arguments'),?)>0))"""
            params += [query, query, query, query]
        cur = await self.db.conn.execute(
            f"SELECT id AS seq,role AS kind,turn_uuid,run_root_turn_uuid,name,length(content) AS stored_chars,created_at FROM messages WHERE {where} ORDER BY id LIMIT ?",
            (*params, limit + 1),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        more = len(rows) > limit
        rows = rows[:limit]
        for row in rows:
            row["event_id"] = f"message:{row['seq']}"
            row["role"] = row["kind"]
            row["toolName"] = row["name"]
        return {"events": rows, "highWater": bound, "hasMore": more,
                "nextAfter": rows[-1]["seq"] if rows and more else None}

    async def event_payload(self, event_id: str) -> dict[str, Any]:
        if not event_id.startswith("message:"):
            raise ContextHistoryUnavailable("invalid_controller_event_id")
        try:
            message_id = int(event_id.split(":", 1)[1])
        except ValueError as exc:
            raise ContextHistoryUnavailable("invalid_controller_event_id") from exc
        cur = await self.db.conn.execute("SELECT * FROM messages WHERE id=? AND chat_id=?", (message_id, self.chat_id))
        raw = await cur.fetchone()
        if raw is None:
            raise ContextHistoryUnavailable("event_not_found_in_conversation")
        # Prefer the exact private model input/result when present; it may be
        # richer than the public transcript. No Agent instance is consulted.
        cur = await self.db.conn.execute(
            "SELECT 1 FROM context_execution_events WHERE owner_key=? AND event_id=?",
            (self.store.owner.key, event_id),
        )
        if await cur.fetchone() is not None:
            return await self.store.event_payload(event_id)
        row = MessageDAO(self.db)._row(raw)
        return {"event_id": event_id, "seq": message_id, "kind": row.role,
                "turn_uuid": row.turn_uuid, "created_at": row.created_at,
                "payload": serialize_messages(neutral_context([row.to_message()]))[0]}
