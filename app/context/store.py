"""Private window persistence and incremental execution history.

No public Web projection reads these tables. Accessors require an owner derived
from the running Controller/Agent, never an owner supplied by a model tool call.
Old provider checkpoints and summary IDs do not define window validity.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.agent.native_continuation import (
    deserialize_messages,
    serialize_messages,
    validate_model_context,
)
from app.context.window import CONTEXT_META, mark_source, neutral_context, source_of
from app.db.engine import DB, now_ts
from app.llm.base import Message


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class StaleWindow(RuntimeError):
    pass


class ContextHistoryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ContextOwner:
    kind: str
    key: str
    conversation_uuid: str = ""
    session_uuid: str = ""
    agent_session_uuid: str = ""
    task_uuid: str = ""
    chat_id: int = 0

    @classmethod
    def controller(cls, *, chat_id: int, session_uuid: str, conversation_uuid: str = "") -> ContextOwner:
        if not session_uuid:
            raise ValueError("controller_context_requires_session")
        return cls("controller", f"controller:{chat_id}:{session_uuid}", conversation_uuid, session_uuid, chat_id=chat_id)

    @classmethod
    def agent(cls, *, task_uuid: str, agent_session_uuid: str = "", conversation_uuid: str = "", session_uuid: str = "", chat_id: int = 0) -> ContextOwner:
        if not task_uuid:
            raise ValueError("agent_context_requires_task")
        key = f"agent:{agent_session_uuid}" if agent_session_uuid else f"legacy-task:{task_uuid}"
        return cls("agent", key, conversation_uuid, session_uuid, agent_session_uuid, task_uuid, chat_id)


@dataclass(frozen=True)
class RequestTicket:
    owner_key: str
    window_version: int
    route: str
    sequence: int
    request_id: str
    estimate_tokens: int = 0


class WindowStore:
    def __init__(self, db: DB, owner: ContextOwner) -> None:
        self.db = db
        self.owner = owner

    async def _ensure(self, conn: Any) -> None:
        owner, ts = self.owner, now_ts()
        await conn.execute(
            """INSERT OR IGNORE INTO context_windows
               (owner_key,owner_kind,conversation_uuid,session_uuid,agent_session_uuid,task_uuid,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (owner.key, owner.kind, owner.conversation_uuid, owner.session_uuid, owner.agent_session_uuid, owner.task_uuid, ts, ts),
        )

    async def load(self) -> dict[str, Any] | None:
        cur = await self.db.conn.execute("SELECT * FROM context_windows WHERE owner_key=?", (self.owner.key,))
        row = await cur.fetchone()
        if row is None:
            return None
        data = dict(row)
        data["state"] = json.loads(data.pop("state_json"))
        return data

    async def archive(self, messages: list[Message]) -> dict[str, int]:
        """Append previously unseen semantic units before any window can evict them.

        Agent payloads are private originals, not 1000-character audit previews.
        A source message ID can point to the Controller's existing transcript.
        Runtime snapshots are regenerated, not accumulated as user instructions.
        """
        payloads: list[tuple[str, str, str, str, int | None, str, str]] = []
        empty_tool_alternates: dict[str, str] = {}
        for message in messages:
            meta = source_of(message)
            if meta.get("kind") == "runtime":
                continue
            if not meta.get("id"):
                mark_source(message, kind=str(meta.get("kind") or "execution"))
                meta = source_of(message)
            item = serialize_messages(neutral_context([message]))[0]
            item.pop(CONTEXT_META, None)
            # A pure tool-call assistant has no text in either representation.
            # Legacy live checkpoints used null, SQLite rows used an empty string.
            # Compare only this exact alternate; never relax source identity/CAS
            # for changed text, tool arguments, media or non-tool messages.
            if item.get("role") == "assistant" and item.get("tool_calls") and item.get("content") in (None, ""):
                alternate = {**item, "content": "" if item.get("content") is None else None}
                empty_tool_alternates[str(meta["id"])] = _hash(alternate)
            message_id = int(meta.get("message_id") or 0) or None
            # Reference-only records are opt-in: rich current media or overlays
            # must not be replaced by their plaintext public transcript.
            body = {} if message_id and meta.get("reference_only") else item
            payloads.append((str(meta["id"]), str(meta.get("kind") or "execution"),
                             str(meta.get("task_uuid") or self.owner.task_uuid),
                             str(meta.get("turn_uuid") or ""), message_id, _json(body), _hash(item)))
        async with self.db.conn.transaction(label="context-history-append") as conn:
            await self._ensure(conn)
            added = 0
            for event_id, kind, task_uuid, turn_uuid, message_id, body, fingerprint in payloads:
                cur = await conn.execute(
                    "SELECT fingerprint FROM context_execution_events WHERE owner_key=? AND event_id=?",
                    (self.owner.key, event_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    if row["fingerprint"] not in (fingerprint, empty_tool_alternates.get(event_id)):
                        raise StaleWindow("source_event_changed; invalidate the edited lineage before continuing")
                    continue
                await conn.execute(
                    """INSERT INTO context_execution_events
                       (owner_key,event_id,kind,task_uuid,turn_uuid,message_id,payload_json,fingerprint,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (self.owner.key, event_id, kind, task_uuid, turn_uuid, message_id, body, fingerprint, now_ts()),
                )
                added += 1
            cur = await conn.execute("SELECT COALESCE(MAX(seq),0) AS high_water FROM context_execution_events WHERE owner_key=?", (self.owner.key,))
            high_water = int((await cur.fetchone())["high_water"])
            if added:
                await conn.execute(
                    "UPDATE context_windows SET source_revision=source_revision+?,source_high_water=?,updated_at=? WHERE owner_key=?",
                    (added, high_water, now_ts(), self.owner.key),
                )
            cur = await conn.execute("SELECT revision,source_revision FROM context_windows WHERE owner_key=?", (self.owner.key,))
            row = await cur.fetchone()
            return {"revision": int(row["revision"]), "sourceRevision": int(row["source_revision"]), "highWater": high_water, "added": added}

    async def save(
        self, messages: list[Message], *, expected_revision: int,
        expected_source_revision: int, route: str, rotated: bool = False,
        reason: str = "checkpoint", detail: dict[str, Any] | None = None,
        expected_message_high_water: int | None = None,
        source_message_high_water: int | None = None,
        extra_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # The independent fallback contains neutral, protocol-closed content.
        # Provider opaque items live solely in their existing private checkpoint.
        clean = neutral_context(messages)
        if not validate_model_context(clean):
            raise ValueError("context_window_has_incomplete_tool_batch")
        state = {**(extra_state or {}), "version": 1, "messages": serialize_messages(clean),
                 "sourceMessageHighWater": max((int(source_of(message).get("message_id") or 0) for message in clean), default=0)}
        async with self.db.conn.transaction(label="context-window-save") as conn:
            await self._ensure(conn)
            cur = await conn.execute("SELECT * FROM context_windows WHERE owner_key=?", (self.owner.key,))
            old = dict(await cur.fetchone())
            if old["revision"] != expected_revision or old["source_revision"] != expected_source_revision:
                raise StaleWindow("context_source_or_window_changed")
            if expected_message_high_water is not None:
                cur = await conn.execute("SELECT COALESCE(MAX(id),0) AS high_water FROM messages WHERE chat_id=?", (self.owner.chat_id,))
                if int((await cur.fetchone())["high_water"]) != expected_message_high_water:
                    raise StaleWindow("new_controller_message_arrived")
            for message in clean:
                meta = source_of(message)
                if meta.get("kind") == "runtime":
                    continue
                if not meta.get("id"):
                    raise ContextHistoryUnavailable("unarchived_context_message")
                cur = await conn.execute(
                    "SELECT 1 FROM context_execution_events WHERE owner_key=? AND event_id=?",
                    (self.owner.key, str(meta["id"])),
                )
                if await cur.fetchone() is None:
                    raise ContextHistoryUnavailable("context_source_not_in_owner_history")
            cur = await conn.execute(
                "SELECT COALESCE(MAX(message_id),0) AS high_water FROM context_execution_events WHERE owner_key=?",
                (self.owner.key,))
            archived_high_water = int((await cur.fetchone())["high_water"])
            old_state = json.loads(old["state_json"])
            # Coverage is independent from the selected/archived tail after a
            # suffix rollback. Copy may explicitly rebind that trusted boundary;
            # newly copied raw rows beyond it have NOT necessarily been consumed.
            # Within an existing lineage, saving a smaller selection cannot undo
            # previously committed coverage (rollback replaces the window first).
            covered = archived_high_water if source_message_high_water is None else int(source_message_high_water)
            if covered < 0:
                raise ValueError("invalid_source_message_high_water")
            state["sourceMessageHighWater"] = max(
                int(old_state.get("sourceMessageHighWater") or 0), covered,
            )
            for key in ("calibration", "strategy"):
                if key in old_state:
                    state.setdefault(key, old_state[key])
            changed_route = old["route_fingerprint"] != route
            version = int(old["window_version"]) + int(rotated or changed_route)
            invalidate = rotated or changed_route
            await conn.execute(
                """UPDATE context_windows SET revision=revision+1,window_version=?,route_fingerprint=?,state_json=?,
                   task_uuid=?,usage_known=CASE WHEN ? THEN 0 ELSE usage_known END,
                   usage_tokens=CASE WHEN ? THEN 0 ELSE usage_tokens END,updated_at=? WHERE owner_key=?""",
                (version, route, _json(state), self.owner.task_uuid, invalidate, invalidate, now_ts(), self.owner.key),
            )
            if rotated:
                if self.owner.kind == "controller":
                    await conn.execute("DELETE FROM controller_model_contexts WHERE chat_id=?", (self.owner.chat_id,))
                if self.owner.kind == "controller" and detail and detail.get("strategy") == "model_summary":
                    summary_cur = await conn.execute(
                        "INSERT INTO summaries(chat_id,summary,up_to_message_id,tokens,created_at) VALUES(?,?,?,?,?)",
                        (self.owner.chat_id, detail["summary"], int(detail.get("upToMessageId") or 0),
                         int(detail.get("summaryTokens") or 0), now_ts()),
                    )
                    detail["summaryId"] = int(summary_cur.lastrowid)
                    detail["summaryRef"] = f"/api/conversations/{self.owner.conversation_uuid}/compactions/{summary_cur.lastrowid}"
                await conn.execute(
                    "INSERT INTO context_window_rotations(rotation_id,owner_key,window_version,reason,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), self.owner.key, version, reason, _json(detail or {}), now_ts()),
                )
        return {"windowVersion": version, "revision": expected_revision + 1, "sourceRevision": expected_source_revision, "usageInvalidated": invalidate}

    async def begin_request(self, *, route: str) -> RequestTicket:
        async with self.db.conn.transaction(label="context-request-begin") as conn:
            cur = await conn.execute("SELECT window_version,route_fingerprint,request_sequence,state_json FROM context_windows WHERE owner_key=?", (self.owner.key,))
            row = await cur.fetchone()
            if row is None or row["route_fingerprint"] != route:
                raise StaleWindow("request_context_route_changed")
            sequence = int(row["request_sequence"]) + 1
            await conn.execute("UPDATE context_windows SET request_sequence=? WHERE owner_key=?", (sequence, self.owner.key))
            state = json.loads(row["state_json"])
            estimate = int(state.get("rawEstimatedInputTokens") or 0) if not state.get("mediaTokensUnknown") else 0
            return RequestTicket(self.owner.key, int(row["window_version"]), route, sequence, str(uuid.uuid4()), estimate)

    async def observe_usage(self, ticket: RequestTicket, *, tokens: int | None) -> bool:
        if ticket.owner_key != self.owner.key:
            return False
        async with self.db.conn.transaction(label="context-request-usage") as conn:
            cur = await conn.execute(
                """UPDATE context_windows SET usage_known=?,usage_tokens=?,usage_request_id=?,usage_request_sequence=?,updated_at=?,
                   state_json=json_set(state_json,'$.calibration',json(?))
                   WHERE owner_key=? AND window_version=? AND route_fingerprint=?
                     AND usage_request_sequence<? AND request_sequence=?""",
                (tokens is not None, max(0, int(tokens or 0)), ticket.request_id, ticket.sequence, now_ts(),
                 _json({"tokens": tokens, "estimate": ticket.estimate_tokens, "route": ticket.route, "windowVersion": ticket.window_version}),
                 self.owner.key, ticket.window_version, ticket.route, ticket.sequence, ticket.sequence),
            )
            return cur.rowcount == 1

    async def restore_messages(self) -> list[Message] | None:
        saved = await self.load()
        if saved is None:
            return None
        if saved["state"].get("incompleteBatch"):
            raise ContextHistoryUnavailable("incomplete_tool_batch_requires_reliable_execution_checkpoint")
        payloads = saved["state"].get("messages")
        if not isinstance(payloads, list):
            return None
        messages = deserialize_messages(payloads)
        if not validate_model_context(messages):
            raise ContextHistoryUnavailable("invalid_saved_window")
        return messages

    async def event_payload(self, event_id: str) -> dict[str, Any]:
        cur = await self.db.conn.execute("SELECT * FROM context_execution_events WHERE owner_key=? AND event_id=?", (self.owner.key, event_id))
        row = await cur.fetchone()
        if row is None:
            raise ContextHistoryUnavailable("event_not_found_in_own_history")
        item = dict(row)
        payload = json.loads(item.pop("payload_json"))
        if not payload and item["message_id"]:
            # A deleted original stays deleted. Never resurrect it from a stale
            # public preview or from another conversation's identical text.
            from app.db.dao import MessageDAO
            cur = await self.db.conn.execute("SELECT * FROM messages WHERE id=? AND chat_id=?", (item["message_id"], self.owner.chat_id))
            source = await cur.fetchone()
            if source is None:
                raise ContextHistoryUnavailable("original_message_deleted")
            payload = serialize_messages(neutral_context([MessageDAO(self.db)._row(source).to_message()]))[0]
            if _hash(payload) != item["fingerprint"]:
                raise ContextHistoryUnavailable("original_message_changed")
        for key in (CONTEXT_META, "native_output_items", "reasoning", "signature"):
            payload.pop(key, None)
        item["payload"] = payload
        return item

    async def index(self, *, after: int = 0, high_water: int | None = None, limit: int = 30, query: str = "") -> dict[str, Any]:
        saved = await self.load()
        bound = int((saved or {}).get("source_high_water") or 0)
        if high_water is not None:
            if high_water < 0 or high_water > bound:
                raise StaleWindow("invalid_history_high_water")
            bound = high_water
        count = min(100, max(1, limit))
        # Index metadata is intentionally body-free. Search reads only this
        # owner's archived semantic payload, never private provider reasoning.
        where = "owner_key=? AND seq>? AND seq<=?"
        params: list[Any] = [self.owner.key, max(0, after), bound]
        if query:
            where += """ AND (instr(payload_json,?)>0 OR instr(kind,?)>0
                OR instr(COALESCE(json_extract(payload_json,'$.content'),''),?)>0
                OR EXISTS (SELECT 1 FROM json_each(payload_json,'$.tool_calls') call
                           WHERE instr(json_extract(call.value,'$.arguments'),?)>0)"""
            params += [query, query, query, query]
            if self.owner.kind == "controller":
                where += " OR EXISTS (SELECT 1 FROM messages m WHERE m.id=context_execution_events.message_id AND m.chat_id=? AND (instr(m.content,?)>0 OR instr(m.tool_calls_json,?)>0))"
                params += [self.owner.chat_id, query, query]
            where += ")"
        cur = await self.db.conn.execute(
            f"""SELECT seq,event_id,kind,task_uuid,turn_uuid,message_id,
                       json_extract(payload_json,'$.role') AS role,
                       COALESCE(json_extract(payload_json,'$.name'),json_extract(payload_json,'$.tool_calls[0].name'),'') AS toolName,
                       COALESCE(json_extract(payload_json,'$.tool_call_id'),json_extract(payload_json,'$.tool_calls[0].id'),'') AS toolCallId,
                       length(payload_json) AS stored_chars,created_at
                FROM context_execution_events WHERE {where} ORDER BY seq LIMIT ?""",
            (*params, count + 1),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        more = len(rows) > count
        rows = rows[:count]
        return {"events": rows, "highWater": bound, "hasMore": more, "nextAfter": int(rows[-1]["seq"]) if rows and more else None}
