"""Window/history lifecycle operations inside the caller's existing transaction."""
from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.native_continuation import deserialize_messages, serialize_messages
from app.context.store import ContextOwner, WindowStore
from app.context.window import mark_source, source_of


async def delete_windows(connection: Any, *, where: str, params: tuple[Any, ...]) -> None:
    # `where` is supplied only by application code, never a model/tool argument.
    for table in ("context_execution_events", "context_window_rotations"):
        await connection.execute(f"DELETE FROM {table} WHERE owner_key IN (SELECT owner_key FROM context_windows WHERE {where})", params)
    await connection.execute(f"DELETE FROM context_windows WHERE {where}", params)


async def delete_controller_windows(connection: Any, chat_id: int) -> None:
    await delete_windows(connection, where="owner_kind='controller' AND owner_key LIKE ?", params=(f"controller:{int(chat_id)}:%",))


@dataclass
class DuplicatedWindow:
    """One transaction-local lineage map, shared by history, selection and checkpoint."""

    source: dict[str, Any]
    message_map: dict[int, int]
    summary_map: dict[int, int]
    rewrite: Callable[[Any], Any]
    saved: dict[str, Any] = field(default_factory=dict)
    event_map: dict[str, str] = field(default_factory=dict)

    def event_id(self, old_id: str) -> str:
        if not old_id:
            return str(uuid.uuid4())
        if old_id not in self.event_map:
            # message_id is also used as a coverage boundary. Only the exact
            # original event namespace denotes a transcript message identity.
            match = re.fullmatch(r"(message|legacy-summary):(\d+)", old_id)
            if match:
                namespace, number = match.groups()
                mapping = self.message_map if namespace == "message" else self.summary_map
                mapped = mapping.get(int(number))
                new_id = f"{namespace}:{mapped}" if mapped else str(uuid.uuid4())
            else:
                new_id = str(uuid.uuid4())
            self.event_map[old_id] = new_id
        return self.event_map[old_id]

    def remap(self, message: dict[str, Any]) -> dict[str, Any]:
        source = source_of(message)
        # Archives/windows may carry ToolCall objects, while private checkpoints
        # carry JSON dictionaries. Rewrite exactly the same semantic representation
        # in all three carriers before binding their shared event identity.
        result = self.rewrite(copy.deepcopy(serialize_messages([message])[0]))
        metadata = {"message_id": self.message_map.get(int(source.get("message_id") or 0), 0)}
        if "summary_id" in source:
            metadata["summary_id"] = self.summary_map.get(int(source["summary_id"] or 0), 0)
        if source.get("derived_from"):
            metadata["derived_from"] = self.event_id(str(source["derived_from"]))
        mark_source(result, kind=str(source.get("kind") or "execution"),
                    source_id=self.event_id(str(source.get("id") or "")), **metadata)
        return result

    def paired(self, task_uuid: str, state: dict[str, Any]) -> bool:
        return (
            self.source["task_uuid"] == task_uuid
            and state.get("windowVersion") == self.source["window_version"]
            and state.get("windowRevision") == self.source["revision"]
        )


async def duplicate_windows(db: Any, *, old_chat: int, new_chat: int, old_conversation: str,
                            new_conversation: str, new_session: str, message_map: dict[int, int],
                            agent_map: dict[str, str], task_map: dict[str, str],
                            rewrite: Callable[[Any], Any],
                            summary_map: dict[int, int] | None = None) -> dict[str, DuplicatedWindow]:
    copies: dict[str, DuplicatedWindow] = {}
    cur = await db.conn.execute("SELECT * FROM context_windows WHERE conversation_uuid=? OR owner_key LIKE ?", (old_conversation, f"controller:{old_chat}:%"))
    for raw in await cur.fetchall():
        old = dict(raw)
        if old["owner_kind"] == "controller":
            owner = ContextOwner.controller(chat_id=new_chat, session_uuid=new_session, conversation_uuid=new_conversation)
        else:
            agent_id = agent_map.get(old["agent_session_uuid"], "")
            task_id = task_map.get(old["task_uuid"], "")
            if not task_id or (old["agent_session_uuid"] and not agent_id):
                continue
            owner = ContextOwner.agent(chat_id=new_chat, session_uuid=new_session, conversation_uuid=new_conversation,
                                       task_uuid=task_id, agent_session_uuid=agent_id)
        new_store = WindowStore(db, owner)
        old_owner = ContextOwner(old["owner_kind"], old["owner_key"], chat_id=old_chat)
        old_store = WindowStore(db, old_owner)
        copied = DuplicatedWindow(old, message_map, summary_map or {}, rewrite)
        copies[old["owner_key"]] = copied

        # Incremental full originals must follow the instance, not only the active
        # selection. Bounded reads avoid collecting an entire Agent lifetime.
        after = 0
        while True:
            cur = await db.conn.execute("SELECT seq,event_id,kind,task_uuid,turn_uuid,message_id FROM context_execution_events WHERE owner_key=? AND seq>? ORDER BY seq LIMIT 100", (old["owner_key"], after))
            events = await cur.fetchall()
            if not events:
                break
            originals = []
            for event in events:
                payload = (await old_store.event_payload(event["event_id"]))["payload"]
                message = deserialize_messages([payload])[0]
                mark_source(message, kind=event["kind"], source_id=event["event_id"], message_id=event["message_id"],
                            task_uuid=event["task_uuid"], turn_uuid=event["turn_uuid"])
                originals.append(copied.remap(message))
                after = event["seq"]
            await new_store.archive(originals)
        state = json.loads(old["state_json"])
        selected = [copied.remap(message) for message in deserialize_messages(state.get("messages") or [])]
        archived = await new_store.archive(selected)
        extra = rewrite({key: value for key, value in state.items() if key not in {"messages", "sourceMessageHighWater", "calibration"}})
        high_water = None
        if old["owner_kind"] == "controller" and "sourceMessageHighWater" in state:
            old_high_water = int(state["sourceMessageHighWater"] or 0)
            high_water = max((new_id for old_id, new_id in message_map.items() if old_id <= old_high_water), default=0)
        copied.saved = await new_store.save(selected, expected_revision=archived["revision"], expected_source_revision=archived["sourceRevision"],
                                           route="", extra_state=extra, source_message_high_water=high_water)
        # No old request is authoritative for the copied window or provider cache.
        await db.conn.execute("UPDATE context_windows SET usage_known=0,usage_tokens=0 WHERE owner_key=?", (owner.key,))
    return copies
