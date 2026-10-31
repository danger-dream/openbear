"""Durable request/group records. Pure SQL helpers shared by service, tools and API."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

OPEN_STATES = ("queued", "delivered", "working", "needs_input")
TERMINAL_STATES = ("completed", "rejected", "failed", "cancelled", "limit_reached")
STATE_LABELS = {
    "queued": "排队中", "delivered": "已送达", "working": "处理中", "needs_input": "需要补充",
    "completed": "已完成", "rejected": "已拒绝", "failed": "已失败", "cancelled": "已取消",
    "limit_reached": "达到上限",
}
MAX_INPUT_ROUNDS = 3
MAX_REQUESTS_PER_TARGET = 6
MAX_CHAIN_DEPTH = 6


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return str(uuid.uuid4())


def loads(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


async def one(conn, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    cur = await conn.execute(sql, params)
    try:
        row = await cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        await cur.close()


async def many(conn, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = await conn.execute(sql, params)
    try:
        return [dict(row) for row in await cur.fetchall()]
    finally:
        await cur.close()


def public_request(row: dict[str, Any], *, titles: dict[str, str] | None = None) -> dict[str, Any]:
    titles = titles or {}
    return {
        "requestId": row["request_uuid"],
        "title": row["title"],
        "state": row["state"],
        "stateLabel": STATE_LABELS.get(row["state"], row["state"]),
        "from": {"conversationUuid": row["from_conversation_uuid"], "title": titles.get(row["from_conversation_uuid"], "")},
        "to": {"conversationUuid": row["to_conversation_uuid"], "title": titles.get(row["to_conversation_uuid"], "")},
        "groupId": row.get("group_uuid") or "",
        "toolCallId": row.get("from_tool_call_id") or "",
        "rootTurnId": row.get("from_root_turn_uuid") or "",
        "inputRounds": int(row.get("input_rounds") or 0),
        "question": row.get("question") or "",
        "createdAtMs": int(row.get("created_at_ms") or 0),
        "updatedAtMs": int(row.get("updated_at_ms") or 0),
        "finishedAtMs": int(row.get("finished_at_ms") or 0),
    }


async def add_event(conn, request_uuid: str, kind: str, body: str = "") -> None:
    await conn.execute(
        "INSERT INTO collab_request_events(request_uuid,kind,body,created_at_ms) VALUES(?,?,?,?)",
        (request_uuid, kind, body, now_ms()),
    )


async def conversation_titles(conn, uuids: list[str]) -> dict[str, str]:
    """Display names: a group member by its member name, others by conversation title."""
    ids = sorted({value for value in uuids if value})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = await many(conn, f"""SELECT w.conversation_uuid,w.title,m.name AS member_name FROM web_conversations w
        LEFT JOIN collab_members m USING(conversation_uuid) WHERE w.conversation_uuid IN ({marks})""", tuple(ids))
    return {row["conversation_uuid"]: str(row["member_name"] or row["title"] or "新会话") for row in rows}


async def member_row(conn, conversation_uuid: str) -> dict[str, Any] | None:
    return await one(conn, """SELECT m.*,g.lead_conversation_uuid,g.name AS group_name,g.owner_chat_id
        FROM collab_members m JOIN collab_groups g USING(group_uuid)
        WHERE m.conversation_uuid=? AND m.status='active'""", (conversation_uuid,))
