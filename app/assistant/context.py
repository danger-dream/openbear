"""The page snapshot a page assistant message carries, as the model reads it."""
from __future__ import annotations

import json
from typing import Any

_MAX_OVERLAYS = 6
_MAX_SUBJECTS = 12
_MAX_ACTIONS = 24
_SELECTION_CHARS = 6000


def _text(value: Any, limit: int) -> str:
    text = str(value if value is not None else "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _plain(value: Any, depth: int = 0) -> Any:
    """JSON-safe, bounded copy of declared details: no deep nesting, no long strings."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _text(value, 600)
    if depth >= 3:
        return _text(json.dumps(value, ensure_ascii=False, default=str), 300)
    if isinstance(value, dict):
        return {str(k)[:60]: _plain(v, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in list(value)[:30]]
    return _text(value, 300)


def normalize(value: Any, *, visible_chars: int) -> dict[str, Any] | None:
    """Keep only the known fields of a browser snapshot, each bounded; None when nothing usable remains."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    page = value.get("page")
    if isinstance(page, dict):
        out["page"] = {k: _text(page.get(k), 300) for k in ("key", "label", "path") if page.get(k)}
    overlays = [
        {k: _text(item.get(k), 200) for k in ("kind", "title") if item.get(k)}
        for item in (value.get("overlays") or [])[:_MAX_OVERLAYS] if isinstance(item, dict)
    ]
    if overlays:
        out["overlays"] = overlays
    subjects = []
    for item in (value.get("subjects") or [])[:_MAX_SUBJECTS]:
        if not isinstance(item, dict) or not (item.get("kind") or item.get("ref")):
            continue
        subject = {k: _text(item.get(k), 300) for k in ("kind", "id", "ref", "label", "role", "scope") if item.get(k)}
        if isinstance(item.get("details"), dict) and item["details"]:
            subject["details"] = _plain(item["details"])
        subjects.append(subject)
    if subjects:
        out["subjects"] = subjects
    selection = value.get("selection")
    if isinstance(selection, dict) and str(selection.get("text") or "").strip():
        out["selection"] = {"source": _text(selection.get("source") or "page", 40),
                            "text": _text(selection.get("text"), _SELECTION_CHARS)}
    visible = value.get("visible")
    if isinstance(visible, dict) and str(visible.get("text") or "").strip():
        out["visible"] = {k: _text(visible.get(k), 200) for k in ("of", "title") if visible.get(k)}
        out["visible"]["text"] = _text(visible.get("text"), visible_chars)
    actions = []
    for item in (value.get("actions") or [])[:_MAX_ACTIONS]:
        if isinstance(item, dict) and item.get("name"):
            action = {"name": _text(item["name"], 80), "description": _text(item.get("description"), 300)}
            if isinstance(item.get("params"), dict) and item["params"]:
                action["params"] = _plain(item["params"])
            actions.append(action)
    if actions:
        out["actions"] = actions
    if not out:
        return None
    label = _text(value.get("label"), 200)
    if label:
        out["label"] = label
    return out


def render(context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    lines = [f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
             for key, value in context.items() if key != "label"]
    return "<page-context>\n" + "\n".join(lines) + "\n</page-context>"


def with_context(text: str, context: dict[str, Any] | None) -> str:
    block = render(context)
    if not block:
        return text
    return f"{text.rstrip()}\n\n{block}" if str(text or "").strip() else block
