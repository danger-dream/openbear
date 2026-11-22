"""Notes: the model reads and edits the user's knowledge-base notes (the 笔记 page)."""
from __future__ import annotations

import json
from typing import Any

from app.assistant import changes
from app.kb_notes import KBNoteError, KBNotesDAO
from app.runtime.tool_result import ToolOutcome
from app.tools.base import ToolRegistry, current_tool_context

ACTIONS = ("list", "search", "get", "create", "update")
_LIST_LIMIT = 300

DESCRIPTION = (
    "The user's knowledge-base notes (the 笔记 page), stored as Markdown in a tree. "
    "list: every note's id, title, path and summary. search: notes whose title, path or body contain all words of query, with a snippet. "
    "get: one note's full Markdown and its revision. create: a new note with title and content, under parent if given. "
    "update: change a note's title, or its body in one way: content with mode replace (the whole body), append or prepend; "
    "or find and replace (one exact, unique passage of the current body). Pass revision from get to refuse the change if the note changed meanwhile. "
    "A note locked read-only cannot be edited until the user unlocks it."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "id": {"type": "string", "description": "get/update: the note id (noteUuid)"},
        "query": {"type": "string", "description": "search: words that must all appear"},
        "title": {"type": "string"},
        "content": {"type": "string", "description": "create: the body; update: text applied by mode"},
        "mode": {"type": "string", "enum": ["replace", "append", "prepend"], "description": "update with content; default replace"},
        "find": {"type": "string", "description": "update: exact passage of the current body to replace"},
        "replace": {"type": "string", "description": "update: the passage that replaces find"},
        "parent": {"type": "string", "description": "create: id of the parent note; omit for the top level"},
        "revision": {"type": "integer", "description": "update: the revision get returned"},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _brief(note: dict[str, Any]) -> dict[str, Any]:
    return {k: note.get(k) for k in ("noteUuid", "title", "path", "summary", "charCount", "isReadonly", "revision", "updatedAtMs") if k in note}


async def _owner(dao: KBNotesDAO) -> int:
    ctx = current_tool_context()
    if ctx.conversation_uuid:
        cur = await dao.db.conn.execute(
            "SELECT owner_chat_id FROM web_conversations WHERE conversation_uuid=?", (ctx.conversation_uuid,))
        row = await cur.fetchone()
        if row:
            return int(row["owner_chat_id"])
    return int(ctx.chat_id or 0)


def _failed(error: str, message: str, **extra: Any) -> ToolOutcome:
    return ToolOutcome(json.dumps({"ok": False, "error": error, "message": message, **extra}, ensure_ascii=False),
                       "failed", "not_started")


async def run_notes(dao: KBNotesDAO, args: dict[str, Any]) -> Any:
    action = str(args.get("action") or "")
    owner = await _owner(dao)
    if not owner:
        return _failed("owner_unknown", "this run has no console owner to read notes for")
    note_id = str(args.get("id") or "").strip()
    try:
        if action == "list":
            items = (await dao.list_tree(owner))["items"]
            return json.dumps({"ok": True, "total": len(items), "items": [_brief(n) for n in items[:_LIST_LIMIT]],
                               **({"truncated": True} if len(items) > _LIST_LIMIT else {})}, ensure_ascii=False)
        if action == "search":
            query = str(args.get("query") or "").strip()
            if not query:
                return _failed("query_required", "search needs query")
            found = await dao.search_notes(owner, query, limit=40)
            return json.dumps({"ok": True, "items": [{**_brief(n), "snippet": n.get("snippet", "")} for n in found]}, ensure_ascii=False)
        if action == "get":
            note = await dao.get_note(owner, note_id)
            return json.dumps({"ok": True, "note": {**_brief(note), "content": note["content"]}}, ensure_ascii=False)
        if action == "create":
            note = await dao.create_note(owner, parent_uuid=str(args.get("parent") or ""), title=str(args.get("title") or "未命名笔记"),
                                         content=str(args.get("content") or ""))
            await changes.publish("note", note["noteUuid"], "create")
            return json.dumps({"ok": True, "note": _brief(note)}, ensure_ascii=False)
        if action == "update":
            return await _update(dao, owner, note_id, args)
    except KBNoteError as exc:
        current = exc.extra.get("currentNote")
        return _failed(exc.code, exc.message, **({"currentRevision": current.get("revision")} if isinstance(current, dict) else {}))
    return _failed("unknown_action", f"unknown action: {action}", actions=list(ACTIONS))


async def _update(dao: KBNotesDAO, owner: int, note_id: str, args: dict[str, Any]) -> Any:
    current = await dao.get_note(owner, note_id)
    body = str(current["content"] or "")
    change: dict[str, Any] = {}
    if "title" in args and str(args["title"] or "").strip():
        change["title"] = str(args["title"])
    if "find" in args:
        find = str(args.get("find") or "")
        count = body.count(find) if find else 0
        if count != 1:
            return _failed("find_not_unique" if count else "find_not_found",
                           "find must match exactly one passage of the current body" if count else "find does not occur in the current body",
                           matches=count)
        change["content"] = body.replace(find, str(args.get("replace") or ""), 1)
    elif "content" in args:
        text = str(args.get("content") or "")
        mode = str(args.get("mode") or "replace")
        if mode == "append":
            change["content"] = f"{body.rstrip()}\n\n{text.strip()}\n" if body.strip() else text
        elif mode == "prepend":
            change["content"] = f"{text.strip()}\n\n{body.lstrip()}" if body.strip() else text
        else:
            change["content"] = text
    if not change:
        return _failed("nothing_to_change", "update needs title, content or find/replace")
    revision = args.get("revision")
    if revision is not None:
        if isinstance(revision, bool) or not (isinstance(revision, int) or (isinstance(revision, str) and revision.strip().isdigit())):
            return _failed("invalid_revision", "revision must be a positive integer")
        expected_revision: int | None = int(revision)
    elif "find" in args or ("content" in args and str(args.get("mode") or "replace") in {"append", "prepend"}):
        expected_revision = int(current["revision"])
    else:
        expected_revision = None
    note = await dao.update_note(owner, note_id, change, expected_revision=expected_revision)
    await changes.publish("note", note["noteUuid"], "update")
    return json.dumps({"ok": True, "note": _brief(note)}, ensure_ascii=False)


def register_notes_tool(registry: ToolRegistry, db: Any) -> None:
    dao = KBNotesDAO(db)

    async def handler(args: dict[str, Any]) -> Any:
        return await run_notes(dao, args)

    registry.add("Notes", DESCRIPTION, SCHEMA, handler, visibility={"main"})
