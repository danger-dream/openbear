"""Page assistant conversations: ordinary Web conversations marked assistant=1.

The browser creates them in the virtual folder ``__assistant``; they are stored without a
folder and listed only in the conversation tree's 页面助手 group.
"""
from __future__ import annotations

import json
from typing import Any

from app.models.thinking import configured_default_think_level

FOLDER = "__assistant"
GROUP_LABEL = "页面助手"


async def is_assistant(db: Any, conversation_uuid: str) -> bool:
    if not conversation_uuid:
        return False
    cur = await db.conn.execute(
        "SELECT assistant FROM web_conversations WHERE conversation_uuid=?", (conversation_uuid,))
    row = await cur.fetchone()
    return bool(row and int(row["assistant"] or 0))


def scope_json(value: Any) -> str:
    """The page and object a conversation belongs to, as stored: {key, label, path, open?}."""
    if not isinstance(value, dict) or not str(value.get("key") or "").strip():
        return ""
    scope = {k: str(value.get(k) or "")[:limit] for k, limit in (("key", 300), ("label", 200), ("path", 500))}
    opener = value.get("open")
    if isinstance(opener, dict) and opener.get("kind") and opener.get("id") not in (None, ""):
        scope["open"] = {"kind": str(opener["kind"])[:40], "id": str(opener["id"])[:200]}
    return json.dumps(scope, ensure_ascii=False)


def scope_of(row: Any) -> dict[str, Any] | None:
    try:
        scope = json.loads(row["assistant_scope"] or "")
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return scope if isinstance(scope, dict) and scope.get("key") else None


async def latest_in_scope(db: Any, owner_chat_id: int, key: str) -> str:
    """The scope's most recently used conversation; a scope keeps one thread going until the user starts another."""
    cur = await db.conn.execute(
        """SELECT conversation_uuid FROM web_conversations
           WHERE owner_chat_id=? AND assistant=1 AND COALESCE(archived_at,0)=0
             AND json_extract(CASE WHEN json_valid(assistant_scope) THEN assistant_scope END, '$.key')=?
           ORDER BY MAX(COALESCE(last_interaction_at_ms,0), COALESCE(created_at,0)*1000) DESC, id DESC LIMIT 1""",
        (int(owner_chat_id), str(key)),
    )
    row = await cur.fetchone()
    return str(row["conversation_uuid"]) if row else ""


async def scope_by_uuid(db: Any, conversation_uuid: str) -> dict[str, Any] | None:
    cur = await db.conn.execute(
        "SELECT assistant_scope FROM web_conversations WHERE conversation_uuid=? AND assistant=1", (conversation_uuid,))
    row = await cur.fetchone()
    return scope_of(row) if row else None


def run_defaults(config: Any) -> dict[str, Any]:
    """The settings' run choices for a new assistant conversation, in folder run-default fields."""
    settings = config.assistant
    out: dict[str, Any] = {"mainFastMode": bool(settings.fast_mode)}
    resolved = config.models.resolve(settings.model) if settings.model else None
    if resolved:
        out["mainModel"] = settings.model
    if settings.thinking_level:
        out["mainThinkingLevel"] = settings.thinking_level
    elif resolved:
        out["mainThinkingLevel"] = configured_default_think_level(
            resolved[1].thinking_levels, resolved[1].default_thinking_level)
    return out


def extensions(config: Any, known: set[str]) -> dict[str, Any]:
    return {"enabled": [key for key in config.assistant.extensions if key in known], "autoAllow": False}
