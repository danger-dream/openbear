"""Conversation-reference character policy; no model-dependent token gate.

Only visible user/assistant body Unicode code points count. Catalog readers
project sizes, not transcripts; authoritative send resolution never uses caches.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from app.tools.history import _group_turns, _render_turns, _resolve_conversation, _visible_history_items

CONVERSATION_CONTENT_LIMIT = 20_000
CONVERSATION_KINDS = {"chat", "turn", "message"}
CONVERSATION_MODE_REASONS = {"conversation_too_long", "conversation_total_limit"}


def _visible_chars(payload_json, internal):
    payload = json.loads(payload_json or "{}")
    if internal or not isinstance(payload, dict) or payload.get("hidden") or payload.get("internal"):
        return 0
    text = str(payload.get("text") or payload.get("summary") or "")
    return len(text) if text.strip() else 0


def history_sizes(conn, conversation_uuid):
    """SQLite returns only IDs/lengths. Match History visibility and text choice."""
    conn.create_function("reference_body_chars", 2, _visible_chars)
    rows = conn.execute(
        """SELECT op_id,COALESCE(NULLIF(turn_uuid,''),'seq:' || display_seq) AS turn_id,
                  reference_body_chars(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,internal) AS chars
           FROM web_operations WHERE conversation_uuid=?
             AND op_type IN ('user_message','assistant_message') ORDER BY display_seq,id""",
        (conversation_uuid,),
    )
    turns, messages = {}, {}
    for row in rows:
        if row["chars"]:
            messages[row["op_id"]] = row["chars"]
            turns[row["turn_id"]] = turns.get(row["turn_id"], 0) + row["chars"]
    return {"bodyChars": sum(turns.values()), "recentTurnChars": list(reversed(list(turns.values())))[:50],
            "turns": turns, "messages": messages}


def catalog_history_sizes(conn, conversation_uuid, cache=None):
    # Reuse the existing metadata hub cadence. A stream may update its revision
    # every frame; count its bodies at most once per two seconds, not per frame.
    now = time.monotonic()
    previous = (cache or {}).get(conversation_uuid)
    if previous and now - previous[0] < 2:
        return previous[2]
    signature = tuple(conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(revision),0),COALESCE(MAX(id),0),COALESCE(MAX(updated_at_ms),0) FROM web_operations WHERE conversation_uuid=?",
        (conversation_uuid,),
    ).fetchone())
    value = previous[2] if previous and previous[1] == signature else history_sizes(conn, conversation_uuid)
    if cache is not None:
        cache[conversation_uuid] = (now, signature, value)
    return value


def selected_chars(sizes, ref):
    if ref["kind"] == "turn":
        return sizes["turns"].get(ref["itemId"])
    if ref["kind"] == "message":
        return sizes["messages"].get(ref["itemId"])
    if ref.get("scope") == "recent":
        return sum(sizes["recentTurnChars"][:ref.get("turns", 20)])
    return sizes["bodyChars"]


def conversation_material(db_path, ref, *, owner, remaining, metadata_only=False):
    """Check and freeze within one SQLite snapshot; over-limit bodies never leave it."""
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        info = _resolve_conversation(conn, ref["id"])
        if info is None or info.owner_chat_id != owner:
            return None
        sizes = history_sizes(conn, ref["id"])
        chars = selected_chars(sizes, ref)
        if chars is None:
            return None
        result = {"bodyChars": chars, "contentLimit": CONVERSATION_CONTENT_LIMIT, "sourceLabel": info.title or ref["label"]}
        if metadata_only or chars > remaining:
            return result
        items = _visible_history_items(conn, ref["id"], op_id=ref.get("itemId") if ref["kind"] == "message" else None)
        if ref["kind"] == "message":
            result["content"] = items[0].text
        else:
            turns = _group_turns(items)
            if ref["kind"] == "turn":
                turns = [turn for turn in turns if turn.turn_uuid == ref["itemId"]]
            elif ref.get("scope") == "recent":
                turns = turns[-ref.get("turns", 20):]
            # Body policy excludes History headers. Do not truncate a permitted
            # selection merely because the readable envelope adds characters.
            result["content"] = _render_turns(info, turns, total_turns=len(turns), max_chars=chars + len(items) * 2000 + 10000)
        return result
