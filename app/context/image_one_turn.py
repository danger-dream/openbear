"""The per-conversation "图片只看一轮" setting: images reach the model only in the
round they arrive; later rounds see their local file paths instead."""
from __future__ import annotations

import json
from typing import Any

DEFAULT = {"enabled": False, "includeUser": True, "agents": True}
KEYS = frozenset(DEFAULT)


def normalize(value: Any) -> dict[str, bool]:
    """A stored JSON string, a dict, or nothing; missing or non-bool keys take the default."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except ValueError:
            value = {}
    raw = value if isinstance(value, dict) else {}
    return {key: raw[key] if isinstance(raw.get(key), bool) else default for key, default in DEFAULT.items()}


def dumps(value: Any) -> str:
    return json.dumps(normalize(value), separators=(",", ":"), sort_keys=True)


def patch_error(value: Any) -> str:
    """'' when value is a valid partial update."""
    if not isinstance(value, dict) or not value or set(value) - KEYS:
        return "invalid_image_one_turn"
    if not all(isinstance(item, bool) for item in value.values()):
        return "invalid_image_one_turn"
    return ""


async def conversation_image_one_turn(db: Any, *, conversation_uuid: str = "", chat_id: int | None = None) -> dict[str, bool]:
    if not conversation_uuid and chat_id is None:
        return normalize(None)
    where, key = ("internal_chat_id", chat_id) if chat_id is not None else ("conversation_uuid", conversation_uuid)
    cur = await db.conn.execute(f"SELECT image_one_turn FROM web_conversations WHERE {where}=?", (key,))
    row = await cur.fetchone()
    return normalize(row["image_one_turn"] if row is not None else None)
