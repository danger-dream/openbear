"""WorkItem: the controller's tool for the conversation's task record.

The runtime supplies the conversation, turn and tool call; the
model supplies item numbers and content. Every source carrying the user's
authority is resolved from durable records by the DAO.
"""
from __future__ import annotations

import asyncio
import json
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.logging import get_logger
from app.tools.base import ToolRegistry, current_tool_context
from app.tools.file_state import normalize_path
from app.work_items import (
    MODEL_EVENT_LABELS,
    STATUS_LABELS,
    Actor,
    WorkItemConflict,
    WorkItemError,
    WorkItemNotFound,
    WorkItemsDAO,
    changed_event,
    configure_limits,
    in_subtree,
    model_actor_label,
    normalize_evidence,
    normalize_number,
    walk,
)

log = get_logger("work_items.tool")

_STATUS_ACTIONS = ("start", "complete", "block", "ask", "drop", "reopen")
_DIFF_LIMIT = 200_000
_DIFF_TIMEOUT_S = 10
_MAX_OPS = 30
_REFERENCE = re.compile(r"\$(\d+)((?:\.\d+)*)")
# While ops run, panel notifications are collected here and sent once at the end.
_PUBLISH_SINK: ContextVar[list[tuple[list[str], str]] | None] = ContextVar("work_item_publish_sink", default=None)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def _brief(node: dict[str, Any]) -> dict[str, Any]:
    out = {"number": node["number"], "title": node["title"], "status": node.get("effectiveLabel") or STATUS_LABELS[node["status"]],
           "revision": node["revision"]}
    if node.get("statusReason"):
        out["reason"] = node["statusReason"]
    if node.get("progress"):
        out["progress"] = f"{node['progress']['done']}/{node['progress']['total']}"
    return out


def _detail(node: dict[str, Any]) -> dict[str, Any]:
    out = _brief(node)
    out.update({key: node[key] for key in ("description", "criteria", "scopeMethod") if node.get(key)})
    if node.get("evidence"):
        out["evidence"] = [{k: v for k, v in item.items() if k != "diff"} for item in node["evidence"]]
    if node.get("ancestors"):
        out["within"] = [f"#{a['number']} {a['title']}" for a in node["ancestors"]]
    out["children"] = [_brief(child) for child in node["children"]]
    history = []
    for event in node.get("events") or []:
        entry = {"kind": MODEL_EVENT_LABELS.get(event["kind"], event["label"]), "by": model_actor_label(event), "at": event["createdAtMs"]}
        if event["summary"]:
            entry["summary"] = event["summary"]
        data = event["data"]
        for key in ("quote", "old", "new", "why", "reason", "from", "to", "questions", "added", "removed"):
            if data.get(key):
                entry[key] = data[key]
        source = event["source"]
        for key in ("turnUuid", "interactionId"):
            if source.get(key):
                entry[key] = source[key]
        history.append(entry)
    out["history"] = history
    return out


async def _git_root(path: Path) -> Path | None:
    for parent in [path.parent, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


async def _run(*argv: str, cwd: Path) -> str:
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_DIFF_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode("utf-8", "replace")


async def snapshot_diffs(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep what each listed file's uncommitted change was when the item was completed."""
    if any(item.get("kind") == "commit" for item in evidence):
        return evidence
    evidence_local = [item for item in evidence if not item.get("server")]
    budget = _DIFF_LIMIT
    for item in evidence_local:
        if item.get("kind") != "file" or not item.get("path") or item.get("diff") or budget <= 0:
            continue
        try:
            path = normalize_path(item["path"]).resolve()
            root = await _git_root(path) if path.is_file() else None
            if root is None:
                continue
            rel = str(path.relative_to(root))
            diff = await _run("git", "diff", "HEAD", "--", rel, cwd=root)
            if not diff.strip():
                tracked = await _run("git", "ls-files", "--", rel, cwd=root)
                if not tracked.strip():
                    diff = await _run("git", "diff", "--no-index", "--", "/dev/null", rel, cwd=root)
            if diff.strip():
                item["diff"] = diff[:budget]
                budget -= len(item["diff"])
        except Exception:
            continue
    return evidence


class WorkItemTool:
    def __init__(self, dao: WorkItemsDAO) -> None:
        self.dao = dao

    async def _actor(self, ctx: Any, conversation_uuid: str) -> Actor:
        turn = str(ctx.run_root_turn_uuid or ctx.turn_uuid or "")
        actor = Actor("controller", turn_uuid=turn, tool_call_id=str(ctx.tool_call_id or ""))
        source = await self.dao.turn_input(conversation_uuid, turn)
        if source is not None:
            actor.op_id = source.op_id
            if not source.human:
                actor.kind = source.actor if source.actor in {"cron", "webhook", "collab"} else "controller"
        return actor

    async def _publish(self, ctx: Any, conversation_uuid: str, numbers: list[str], action: str) -> None:
        sink = _PUBLISH_SINK.get()
        if sink is not None:
            sink.append((list(numbers), action))
            return
        callback = getattr(ctx, "conversation_event", None)
        if not callable(callback):
            return
        try:
            await callback(changed_event(conversation_uuid, numbers, action))
        except Exception:
            log.error("work_items.changed publish failed", conversationUuid=conversation_uuid, action=action)

    async def handle(self, args: dict[str, Any]) -> str:
        ctx = current_tool_context()
        conversation_uuid = str(ctx.conversation_uuid or ctx.session_uuid or "").strip()
        action = str(args.get("action") or "list").strip().lower()
        if not conversation_uuid:
            return _json({"ok": False, "error": "no_conversation", "message": "work items live in a Web conversation"})
        if action == "ops" or ("ops" in args and not args.get("action")):
            return _json(await self._ops(ctx, conversation_uuid, args.get("ops")))
        try:
            actor = await self._actor(ctx, conversation_uuid)
            result = await self._controller(actor, conversation_uuid, action, args, ctx)
            return _json({"ok": True, **result})
        except Exception as exc:
            return _json(_failure(exc, action))

    async def _ops(self, ctx: Any, conversation_uuid: str, ops: Any) -> dict[str, Any]:
        """Run steps in order with each action's own checks; stop at the first failure, no rollback."""
        if not isinstance(ops, list) or not ops:
            return {"ok": False, "error": "ops_required", "message": "ops must be a non-empty list of steps"}
        if len(ops) > _MAX_OPS:
            return {"ok": False, "error": "too_many_ops", "message": f"at most {_MAX_OPS} steps per call"}
        completed: list[dict[str, Any]] = []
        created: list[list[str]] = []
        failed: dict[str, Any] | None = None
        sink: list[tuple[list[str], str]] = []
        token = _PUBLISH_SINK.set(sink)
        try:
            actor = await self._actor(ctx, conversation_uuid)
            for index, raw in enumerate(ops, 1):
                action = str(raw.get("action") or "").strip().lower() if isinstance(raw, dict) else ""
                try:
                    if not action or action == "ops":
                        raise WorkItemError("invalid_step", "each step is an object with its own action (ops cannot nest)")
                    result = await self._controller(actor, conversation_uuid, action, _with_references(raw, created, index), ctx)
                except Exception as exc:
                    failed = {"step": index, "action": action, **_failure(exc, action)}
                    break
                created.append(list(result.get("created") or []))
                if result.get("errors"):
                    failed = {"step": index, "action": action, "ok": False, "error": result["errors"][0]["error"],
                              "message": result["errors"][0]["message"], **result}
                    break
                completed.append({"step": index, "action": action, **result})
        except Exception as exc:
            failed = {"step": 0, "action": "", **_failure(exc, "ops")}
        finally:
            _PUBLISH_SINK.reset(token)
        if sink:
            numbers = list(dict.fromkeys(number for changed, _ in sink for number in changed))
            actions = list(dict.fromkeys(action for _, action in sink))
            await self._publish(ctx, conversation_uuid, numbers, actions[0] if len(actions) == 1 else "ops")
        if failed is None:
            return {"ok": True, "completed": completed}
        return {"ok": False, "error": failed["error"], "message": failed.get("message", ""),
                "failedStep": {key: value for key, value in failed.items() if key != "ok"}, "completed": completed,
                "notRun": len(ops) - failed["step"],
                "note": "steps before failedStep were applied and stay applied; fix the failed step and send only it and the rest"}

    async def _controller(self, actor: Actor, conversation_uuid: str, action: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        dao = self.dao
        if action == "list":
            tree = await dao.tree(conversation_uuid)
            focus = await dao.focus(conversation_uuid, actor.turn_uuid)
            include_closed = bool(args.get("includeClosed"))
            tasks = []
            for node in tree:
                if not include_closed and node["status"] in {"done", "abandoned"}:
                    tasks.append({**_brief(node), "children": "closed"})
                    continue
                tasks.append({**_brief(node), "items": [_brief(item) for item in walk(node["children"])]})
            return {"tasks": tasks, "focus": focus["numbers"] if focus else []}
        if action == "get":
            return {"item": _detail(await dao.detail(conversation_uuid, normalize_number(args.get("number"))))}
        if action == "create":
            source = await dao.resolve_source(conversation_uuid, actor, args)
            spec = {key: args.get(key) for key in ("title", "description", "criteria", "scopeMethod", "children")}
            result = await dao.create(conversation_uuid, actor, spec, source=source)
            await self._publish(ctx, conversation_uuid, result["created"], "create")
            return {"number": result["number"], "created": result["created"],
                    "next": "Record findings with note, the user's decisions with decide. Mark the task decided (phase) once the plan is agreed."}
        if action == "add":
            parent = normalize_number(args.get("number"))
            created = await dao.add(conversation_uuid, actor, parent, args.get("children") or [], reason=str(args.get("reason") or ""))
            await self._publish(ctx, conversation_uuid, created, "add")
            return {"created": created}
        if action == "update":
            number = normalize_number(args.get("number"))
            changes = {key: args[key] for key in ("title", "description", "criteria", "scopeMethod") if key in args}
            item = await dao.update(conversation_uuid, actor, number, revision=int(args.get("revision") or 0), changes=changes,
                                    reason=str(args.get("reason") or ""))
            await self._publish(ctx, conversation_uuid, [number], "update")
            return {"item": _brief({**item, "children": []})}
        if action == "note":
            number = normalize_number(args.get("number"))
            files = normalize_evidence(args.get("evidence"))
            await dao.note(conversation_uuid, actor, number, kind=str(args.get("kind") or "note"),
                           summary=str(args.get("summary") or ""), files=files)
            await self._publish(ctx, conversation_uuid, [number], "note")
            return {"recorded": number}
        if action == "decide":
            source = await dao.resolve_source(conversation_uuid, actor, args, require_human=True)
            numbers, questions = [], {}
            for entry in args.get("decisions") or []:
                if not isinstance(entry, dict):
                    continue
                for raw in entry.get("items") or []:
                    number = normalize_number(raw)
                    numbers.append(number)
                    if entry.get("questionId"):
                        questions.setdefault(number, []).append(str(entry["questionId"]))
            numbers += [normalize_number(raw) for raw in args.get("items") or []]
            numbers = list(dict.fromkeys(numbers))
            if not numbers:
                raise WorkItemError("items_required", "say which items the decision is about")
            recorded = await dao.decide(conversation_uuid, actor, numbers, source, question_ids=questions,
                                        summary=str(args.get("summary") or ""))
            await self._publish(ctx, conversation_uuid, recorded, "decide")
            return {"recorded": recorded}
        if action == "correct":
            number = normalize_number(args.get("number"))
            source = await dao.resolve_source(conversation_uuid, actor, args, require_human=True)
            changes = {key: args[key] for key in ("title", "description", "criteria", "scopeMethod") if key in args}
            item = await dao.correct(conversation_uuid, actor, number, source, old=str(args.get("old") or ""),
                                     new=str(args.get("new") or ""), why=str(args.get("why") or args.get("reason") or ""),
                                     changes=changes)
            status = str(args.get("status") or "").strip()
            if status:
                await self._status(actor, conversation_uuid, status, {"number": number, "reason": args.get("why") or args.get("reason")}, source)
            await self._publish(ctx, conversation_uuid, [number], "correct")
            return {"item": _brief({**item, "children": []})}
        if action == "focus":
            numbers = [normalize_number(raw) for raw in (args.get("items") or ([args["number"]] if args.get("number") else []))]
            source = await dao.resolve_source(conversation_uuid, actor, args, require_human=True)
            basis = {**source.public(), "actor": source.actor}
            focus = await dao.set_focus(conversation_uuid, actor.turn_uuid, numbers, basis=basis, actor=actor)
            await self._publish(ctx, conversation_uuid, focus.get("numbers") or numbers, "focus")
            return {"focus": focus["numbers"],
                    "next": "The framework added this focus's briefing (the user's words and decisions) to your context. Read it, then start the first item."}
        if action == "phase":
            number = normalize_number(args.get("number"))
            phase = str(args.get("phase") or "").strip()
            source = await dao.resolve_source(conversation_uuid, actor, args, require_human=phase == "decided",
                                              require_quote=phase == "decided")
            item = await dao.transition(conversation_uuid, actor, number, "phase", phase=phase, source=source,
                                        summary=str(args.get("summary") or ""))
            await self._publish(ctx, conversation_uuid, [number], "phase")
            return {"item": _brief({**item, "children": []})}
        if action == "delete":
            number = normalize_number(args.get("number"))
            removed = await dao.delete(conversation_uuid, actor, number, reason=str(args.get("reason") or ""))
            await self._publish(ctx, conversation_uuid, removed, "delete")
            return {"deleted": removed}
        if action in _STATUS_ACTIONS:
            entries = args.get("entries")
            if not isinstance(entries, list) or not entries:
                entries = [{key: args[key] for key in ("number", "summary", "reason", "evidence", "revision", "quote",
                                                       "interactionId", "sourceTurnUuid") if key in args}]
            results, errors = [], []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    item = await self._status(actor, conversation_uuid, action, entry, None, args)
                    results.append(_brief({**item, "children": []}))
                except WorkItemError as exc:
                    errors.append({"number": entry.get("number"), "error": exc.code, "message": exc.message})
            await self._publish(ctx, conversation_uuid, [item["number"] for item in results], action)
            out: dict[str, Any] = {"items": results}
            if errors:
                out["errors"] = errors
            if action == "complete":
                out["remaining"] = await self._remaining(conversation_uuid, actor.turn_uuid)
            return out
        raise WorkItemError("unsupported_action", f"unknown action {action}")

    async def _status(self, actor: Actor, conversation_uuid: str, action: str, entry: dict[str, Any],
                      source: Any = None, args: dict[str, Any] | None = None) -> dict[str, Any]:
        dao = self.dao
        number = normalize_number(entry.get("number"))
        shared = {key: (args or {})[key] for key in ("quote", "interactionId", "sourceTurnUuid", "reason") if key in (args or {})}
        merged = {**shared, **entry}
        needs_user = action == "drop"
        if action == "reopen":
            row = await dao.get_row(conversation_uuid, number)
            needs_user = row["status"] in {"dropped", "abandoned"}
        if source is None and (needs_user or any(merged.get(key) for key in ("interactionId", "sourceTurnUuid", "quote"))):
            source = await dao.resolve_source(conversation_uuid, actor, merged, require_human=needs_user, require_quote=needs_user)
        if action in {"start", "complete"}:
            focus = await dao.focus(conversation_uuid, actor.turn_uuid)
            if focus is None:
                raise WorkItemError("focus_required", "set this turn's focus first (focus, citing the user's instruction to do it)")
            inside = any(in_subtree(number, root) for root in focus["numbers"])
            # A task or section above the focus closes once everything under it is closed.
            above = action == "complete" and any(in_subtree(root, number) for root in focus["numbers"])
            if not inside and not above:
                raise WorkItemError("outside_focus",
                                    f"#{number} is outside this turn's focus ({', '.join('#' + n for n in focus['numbers'])}); "
                                    + ("record a note or add an item instead of working on it" if action == "start"
                                       else "if it is already done, record that with note"))
        evidence = normalize_evidence(merged.get("evidence"))
        if action == "complete" and evidence:
            evidence = await snapshot_diffs(evidence)
        item = await dao.transition(conversation_uuid, actor, number, action, summary=str(merged.get("summary") or ""),
                                    reason=str(merged.get("reason") or ""), evidence=evidence, source=source,
                                    revision=int(merged.get("revision") or 0))
        if action == "start":
            await dao.set_current(conversation_uuid, actor.turn_uuid, number)
        return item

    async def _remaining(self, conversation_uuid: str, turn_uuid: str) -> list[str]:
        return [f"#{item['number']} {item['title']}" for item in await self.dao.unfinished(conversation_uuid, turn_uuid)][:20]


def _failure(exc: Exception, action: str) -> dict[str, Any]:
    if isinstance(exc, WorkItemConflict):
        return {"ok": False, "error": exc.code, "message": exc.message, "conflict": True}
    if isinstance(exc, (WorkItemNotFound, WorkItemError)):
        return {"ok": False, "error": exc.code, "message": exc.message}
    if isinstance(exc, (TypeError, ValueError)):
        return {"ok": False, "error": "invalid_arguments", "message": str(exc)[:300]}
    log.exception("work_items.tool_failed", action=action)
    return {"ok": False, "error": "work_item_error"}


def _with_references(step: dict[str, Any], created: list[list[str]], index: int) -> dict[str, Any]:
    """Replace $N / $N.k item references with numbers created by earlier steps."""
    def resolve(value: Any) -> Any:
        text = str(value or "").strip().lstrip("#")
        if not isinstance(value, str) or not text.startswith("$"):
            return value
        match = _REFERENCE.fullmatch(text)
        if not match:
            raise WorkItemError("invalid_reference", f"{value!r}: write $N or $N.k, N being an earlier create/add step")
        step_number = int(match.group(1))
        if not 1 <= step_number < index:
            raise WorkItemError("invalid_reference", f"{value!r}: step {step_number} is not an earlier step")
        numbers = created[step_number - 1]
        if not numbers:
            raise WorkItemError("invalid_reference", f"{value!r}: step {step_number} created no item; only create/add steps can be referenced")
        return numbers[0] + match.group(2)

    out = dict(step)
    if "number" in out:
        out["number"] = resolve(out["number"])
    if isinstance(out.get("items"), list):
        out["items"] = [resolve(item) for item in out["items"]]
    if isinstance(out.get("entries"), list):
        out["entries"] = [{**entry, "number": resolve(entry.get("number"))} if isinstance(entry, dict) and "number" in entry else entry
                          for entry in out["entries"]]
    if isinstance(out.get("decisions"), list):
        out["decisions"] = [{**entry, "items": [resolve(item) for item in entry["items"]]}
                            if isinstance(entry, dict) and isinstance(entry.get("items"), list) else entry
                            for entry in out["decisions"]]
    return out

_DESCRIPTION = (
    "The conversation's task record (the user's task panel): what the user raised, investigations and proposals, the "
    "user's decisions and corrections, and evidence of completion. Numbers #4 / #4.3 / #4.3.1 (max three levels) never "
    "change; every entry's source turn is recorded. "
    "Cite a user's decision as interactionId (answered questionnaire) or the user's message (this turn's, or "
    "sourceTurnUuid) with a quote of their exact words; drop, reopening dropped work and phase=decided require the quote. "
    "Feedback/rejection approves nothing. "
    "Statuses: task discussing→decided→in_progress→done|abandoned; item todo(待做)/doing(进行中)/blocked(受阻)/ask(等你定)/"
    "done(已完成)/dropped(按决定不做). "
    "Actions: create (task, optional nested children); add (children under number); update (title/description/criteria/"
    "scopeMethod, revision from get/list; changing criteria needs reason); note (kind, summary, optional evidence files); "
    "decide (the user's decision on items: interactionId with decisions, or their message, quote optional); correct (the "
    "user's correction from their message: old/new/why, optional field changes and status); focus (this turn's scope, "
    "only on the user's instruction); phase (decided needs the user's decision); start (only inside this turn's focus); "
    "complete (inside the focus, or a task/section above it once all under it is closed; summary and concrete evidence: "
    "file, report, test, command, commit, link or message reference); block/ask (reason; items without children); drop "
    "(the user's decision; also closes unfinished descendants); reopen (reason; dropped work needs the user's decision and "
    "gets back only the descendants that drop closed, never auto-started); delete (an item created by mistake, with "
    "reason); get; list. Status actions take number or entries[]. "
    "action=ops runs ops[] in order, each step checked exactly as alone; it stops at the first failure (earlier steps "
    "stay) and reports completed and failed steps. In steps \"$N\" is the item step N created (create: the task; add: its "
    "first new item), \"$N.2\" its 2nd child; e.g. create, decide [$1], focus [$1], start $1.1."
)

_EVIDENCE_SCHEMA = {
    "type": "array",
    "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["file", "report", "test", "command", "commit", "link", "message", "note"]},
        "path": {"type": "string", "description": "File or report path"},
        "server": {"type": "string", "description": "Managed server name, for a file not on this machine"},
        "text": {"type": "string", "description": "Test/command result or note"},
        "commit": {"type": "string"},
        "url": {"type": "string"},
        "label": {"type": "string"},
    }, "required": ["kind"]},
}

_CHILD_SCHEMA = {
    "type": "array",
    "items": {"anyOf": [{"type": "string"}, {"type": "object", "properties": {
        "title": {"type": "string"}, "description": {"type": "string"},
        "criteria": {"type": "array", "items": {"type": "string"}},
        "scopeMethod": {"type": "string"},
        "children": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
    }, "required": ["title"]}]},
}


def register_work_item_tool(registry: ToolRegistry, dao: WorkItemsDAO, *, limits: Any = None) -> None:
    if limits is not None:
        configure_limits(limits)
    handler = WorkItemTool(dao)
    registry.add(
        "WorkItem",
        _DESCRIPTION,
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": [
                    "create", "add", "update", "note", "decide", "correct", "focus", "phase", "start", "complete",
                    "block", "ask", "drop", "reopen", "delete", "get", "list", "ops"]},
                "ops": {"type": "array", "items": {"type": "object"}, "description": "Steps: {action, ...same fields as a single call}"},
                "number": {"type": "string", "description": "Item number, e.g. 4 or 4.3"},
                "items": {"type": "array", "items": {"type": "string"}, "description": "Item numbers (focus, decide)"},
                "entries": {"type": "array", "items": {"type": "object", "properties": {
                    "number": {"type": "string"}, "summary": {"type": "string"}, "reason": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "object"}, "description": "As evidence"},
                    "revision": {"type": "integer"}, "quote": {"type": "string"},
                }, "required": ["number"]}, "description": "Several status changes, each with its own summary/evidence/reason"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "criteria": {"type": "array", "items": {"type": "string"}, "description": "Acceptance criteria"},
                "scopeMethod": {"type": "string", "description": "For a decision covering a category: how the item list was made complete"},
                "children": _CHILD_SCHEMA,
                "kind": {"type": "string", "enum": ["investigation", "proposal", "note"]},
                "summary": {"type": "string"},
                "reason": {"type": "string"},
                "evidence": _EVIDENCE_SCHEMA,
                "phase": {"type": "string", "enum": ["discussing", "decided", "in_progress"]},
                "interactionId": {"type": "string", "description": "An answered UserInteraction carrying the decision"},
                "decisions": {"type": "array", "items": {"type": "object", "properties": {
                    "questionId": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
                    "required": ["items"]}, "description": "Which questionnaire answers concern which items"},
                "sourceTurnUuid": {"type": "string", "description": "An earlier turn whose user message is the source (from History)"},
                "quote": {"type": "string", "description": "The user's exact words in the cited message or answer"},
                "old": {"type": "string"}, "new": {"type": "string"}, "why": {"type": "string"},
                "status": {"type": "string", "description": "correct: also apply this status action (e.g. reopen)"},
                "revision": {"type": "integer", "minimum": 1},
                "includeClosed": {"type": "boolean"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler.handle,
        visibility={"main"},
    )
