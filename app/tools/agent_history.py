"""An Agent's own durable execution history, never parent/sibling History."""
from __future__ import annotations

import json
from typing import Any

from app.context.store import ContextHistoryUnavailable, ContextOwner, StaleWindow, WindowStore
from app.db.engine import DB
from app.tools.base import ToolRegistry, current_tool_context

_OWNER_ARGUMENTS = frozenset({"owner", "ownerId", "ownerKey", "conversationUuid", "agentId", "agentSessionUuid", "taskUuid", "to", "scope"})


def _int(args: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = args.get(name, default)
    return min(high, max(low, int(default if value is None else value)))


async def query_execution_history(store: WindowStore, args: dict[str, Any]) -> dict[str, Any]:
    """Shared bounded query body; callers resolve/authorize the owner first."""
    action = str(args.get("action") or "index")
    if action in {"index", "search"}:
        result = await store.index(
            after=_int(args, "after", 0, 0, 2**63 - 1),
            # Strict-schema providers often fill optional integer fields with 0.
            # Treat that documented sentinel as a fresh index, not a frozen empty
            # history. Positive cursors retain exact snapshot paging semantics.
            high_water=int(args.get("highWater") or 0) or None,
            limit=_int(args, "limit", 30, 1, 100),
            query=str(args.get("query") or "") if action == "search" else "",
        )
        return {"ok": True, "source": "execution", **result}
    if action not in {"read", "read_event", "read_events"}:
        return {"ok": False, "error": "unknown_history_action"}
    requested = args.get("eventIds") or ([args["eventId"]] if args.get("eventId") else [])
    if not isinstance(requested, list) or not requested or len(requested) > 20:
        return {"ok": False, "error": "provide_1_to_20_event_ids"}
    offset = _int(args, "offset", 0, 0, 2**63 - 1)
    budget = _int(args, "maxChars", 16_000, 1_000, 32_000)
    if len(requested) > 1 and offset:
        return {"ok": False, "error": "offset_requires_one_event"}
    events = []
    unread = []
    for index, event_id in enumerate(requested):
        if budget <= 0:
            unread = requested[index:]
            break
        event = await store.event_payload(str(event_id))
        payload = json.dumps(event.pop("payload"), ensure_ascii=False, separators=(",", ":"))
        text = payload[offset:offset + budget]
        end = offset + len(text)
        more = end < len(payload)
        # A page is an exact substring, not a fabricated JSON object or a
        # head/tail preview. The next offset covers every omitted character.
        events.append({**event, "text": text, "format": "json_text_fragment",
                       "offset": offset, "totalChars": len(payload),
                       "hasMore": more, "nextOffset": end if more else None})
        budget -= len(text)
        if more:
            unread = requested[index + 1:]
            break
    return {"ok": True, "source": "execution", "events": events, "unreadEventIds": unread}


def register_agent_history_tool(registry: ToolRegistry, db: DB) -> None:
    async def handler(args: dict[str, Any]) -> str:
        context = current_tool_context()
        if not context.task_uuid or not str(context.source or "").startswith("agent:"):
            return json.dumps({"ok": False, "error": "agent_history_requires_own_running_instance"})
        if any(args.get(key) not in (None, "") for key in _OWNER_ARGUMENTS):
            return json.dumps({"ok": False, "error": "agent_history_owner_is_runtime_bound"})
        cur = await db.conn.execute("SELECT session_kind FROM rath_agent_sessions WHERE session_uuid=?", (context.agent_session_uuid,))
        session = await cur.fetchone()
        independent_id = context.agent_session_uuid if session and session["session_kind"] == "independent" else ""
        owner = ContextOwner.agent(
            task_uuid=context.task_uuid, agent_session_uuid=independent_id,
            conversation_uuid=context.conversation_uuid, session_uuid=context.session_uuid,
            chat_id=context.chat_id,
        )
        try:
            result = await query_execution_history(WindowStore(db, owner), args)
        except (ContextHistoryUnavailable, StaleWindow, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    registry.add(
        "AgentHistory",
        "Read your OWN instance's task/control and original tool execution history, including earlier rounds explicitly continued into this instance. Use only for a concrete missing fact outside your active context window, not mechanically after every window rotation. These are original records, not current task progress or Plan authority; use the current runtime/Plan state for execution. Other Agents and parent conversations are inaccessible. index/search return event IDs, role and toolName: role=tool is a result, role=assistant is a call. read_events can retrieve multiple result IDs with one total maxChars budget (up to 32000); unreadEventIds and nextOffset identify the rest. Returned text is an exact JSON fragment, not a summary. No opaque provider state or hidden reasoning is exposed.",
        {
            "type": "object", "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["index", "search", "read", "read_event", "read_events"]},
                "query": {"type": "string", "description": "Exact text within your archived execution records."},
                "after": {"type": "integer", "description": "Stable index cursor: use nextAfter."},
                "highWater": {"type": "integer", "description": "Use 0 (or omit) for a fresh index; then reuse the returned positive highWater while paging."},
                "limit": {"type": "integer", "description": "Index page size, 1–100."},
                "eventId": {"type": "string"},
                "eventIds": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "offset": {"type": "integer", "description": "Exact character offset within one event; use nextOffset."},
                "maxChars": {"type": "integer", "description": "Total body character budget, 1000–32000. No middle truncation."},
            },
            "required": ["action"],
        }, handler, visibility={"agent"}, preserve_result=True,
    )
