"""The per-conversation system prompt template ("人设").

A conversation renders either the active main template ('') or one prompt
template marked as a persona, chosen when it is created. The choice is fixed
once the conversation has a system prompt snapshot.
"""
from __future__ import annotations

import re
from typing import Any


def normalize(value: Any) -> str:
    """'' for the active main template, otherwise a template id as text."""
    text = str(value if value is not None else "").strip()
    return text if re.fullmatch(r"[1-9][0-9]{0,18}", text) and int(text) <= 9223372036854775807 else ""


def patch_error(value: Any) -> str:
    if not isinstance(value, str) or (value.strip() and not normalize(value)):
        return "invalid_prompt_template"
    return ""


async def choices(db: Any) -> dict[str, Any]:
    """The active main template and the selectable persona templates.

    The Agent prompt is never a choice; the active template appears once, as the default.
    """
    cur = await db.conn.execute(
        "SELECT id, name, is_active, is_agent_active, is_persona, length(content) AS chars, updated_at"
        " FROM memory_templates ORDER BY id")
    rows = [dict(row) for row in await cur.fetchall()]
    active = next((row for row in reversed(rows) if row["is_active"]), None)
    items = [_choice(row) for row in rows if row["is_persona"] and not row["is_active"] and not row["is_agent_active"]]
    return {"active": _choice(active) if active else {"id": "", "name": "", "chars": 0, "updatedAt": 0}, "items": items, "existingIds": [str(row["id"]) for row in rows]}


def _choice(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row["id"]), "name": str(row["name"] or ""),
            "chars": int(row["chars"] or 0), "updatedAt": int(row["updated_at"] or 0)}


async def selectable_error(db: Any, value: str) -> str:
    """Validate existing selections; a deleted reference is retained and falls back at rebuild."""
    if not value:
        return ""
    available = await choices(db)
    # A stale menu/directory/draft can still carry a deleted persona ID. Retain
    # that intent and use the ordinary missing-template fallback, not a new
    # blocking error or a rewritten reference. Existing non-personas/Agent
    # templates remain unselectable.
    if value not in available["existingIds"]:
        return ""
    if value != available["active"]["id"] and value not in {item["id"] for item in available["items"]}:
        return "prompt_template_not_found"
    return ""


async def conversation_prompt_template(db: Any, conversation_uuid: str) -> str:
    if not conversation_uuid:
        return ""
    cur = await db.conn.execute("SELECT prompt_template FROM web_conversations WHERE conversation_uuid=?", (conversation_uuid,))
    row = await cur.fetchone()
    return normalize(row["prompt_template"] if row is not None else "")


async def missing(db: Any, value: Any) -> bool:
    """Existence, not selectability: unmarking a persona does not delete it."""
    template_id = normalize(value)
    if not template_id:
        return False
    cur = await db.conn.execute("SELECT 1 FROM memory_templates WHERE id=?", (int(template_id),))
    return await cur.fetchone() is None
