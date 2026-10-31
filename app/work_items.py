"""Work items: the controller's user-visible record of a piece of work.

A work item keeps what was raised, investigated, proposed, decided, corrected,
delegated and completed, each entry with a recoverable source. Sources that
carry the user's authority (a decision to drop work, abandon a task, mark it
decided, or set the scope of a turn) are resolved here from durable records,
never from the model's description of them.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.db.engine import DB
from app.logging import get_logger

log = get_logger("work_items")

TOP_PHASES = ("discussing", "decided", "in_progress", "done", "abandoned")
ITEM_STATUSES = ("todo", "doing", "blocked", "ask", "done", "dropped")
OPEN_TOP = frozenset({"discussing", "decided", "in_progress", "blocked", "ask"})
CLOSED = frozenset({"done", "dropped", "abandoned"})
# Leaves the end-of-turn check still expects work on.
UNFINISHED = frozenset({"todo", "doing", "in_progress"})

STATUS_LABELS = {
    "discussing": "讨论中", "decided": "已定", "in_progress": "进行中", "done": "已完成", "abandoned": "已放弃",
    "todo": "待做", "doing": "进行中", "blocked": "受阻", "ask": "等你定", "dropped": "按决定不做",
}
EVENT_LABELS = {
    "raise": "提出", "investigation": "调查", "proposal": "方案", "note": "备注", "decision": "决策",
    "correction": "纠正", "scope": "范围调整", "state": "状态", "done": "完成",
    "user_edit": "你的修改", "focus": "执行范围", "finish_check": "收尾检查", "delete": "删除",
    "edit": "修改",
}
ACTOR_LABELS = {
    "user": "你", "controller": "主会话", "cron": "定时任务", "webhook": "Webhook", "collab": "会话请求",
    "system": "OpenBear",
}
# What the model reads: the panel addresses the user as 你, which the model would take for itself.
MODEL_EVENT_LABELS = {**EVENT_LABELS, "user_edit": "用户的修改"}


def model_actor_label(event: dict[str, Any]) -> str:
    return "用户" if event.get("actor") == "user" else str(event.get("actorLabel") or "")


# web_operations.source values written for the user's own messages.
HUMAN_SOURCES = frozenset({"user", "telegram", "queued_steering"})
_AUTOMATED_SOURCES = {"webhook": "webhook", "cron": "cron", "collab": "collab"}

TEXT_MAX = 8000
MAX_DEPTH = 3
FINISH_CHECK_LIMIT = 2

RUNTIME_KEY = "_openbear_runtime"
OUTLINE_KIND = "work_items_state"
BRIEFING_KIND = "work_items_focus"
RUNTIME_KINDS = frozenset({OUTLINE_KIND, BRIEFING_KIND})


class WorkItemError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class WorkItemNotFound(WorkItemError):
    pass


class WorkItemConflict(WorkItemError):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class WorkItemLimits:
    """Set from config.workItems whenever the tools are built; read at each use."""

    title_chars: int = 300
    quote_chars: int = 2000
    max_criteria: int = 20
    max_evidence: int = 30
    outline_chars: int = 6000
    briefing_chars: int = 24_000
    recent_edits: int = 5
    recent_edit_days: int = 7


LIMITS = WorkItemLimits()


def configure_limits(config: Any) -> None:
    for name in WorkItemLimits.__dataclass_fields__:
        setattr(LIMITS, name, int(getattr(config, name)))


def _text(value: Any, limit: int = TEXT_MAX) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _loads(value: Any, fallback: Any) -> Any:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return fallback
    return data if isinstance(data, type(fallback)) else fallback


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def normalize_number(value: Any) -> str:
    raw = str(value or "").strip().lstrip("#").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,4}(\.[1-9][0-9]{0,4}){0,2}", raw):
        raise WorkItemError("invalid_number", f"invalid work item number: {value!r}")
    return raw


def parent_of(number: str) -> str:
    return number.rsplit(".", 1)[0] if "." in number else ""


def top_of(number: str) -> str:
    return number.split(".", 1)[0]


def in_subtree(number: str, root: str) -> bool:
    return number == root or number.startswith(root + ".")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _criteria(value: Any) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
    out = [_text(item, 1000) for item in items if _text(item, 1000)]
    if len(out) > LIMITS.max_criteria:
        raise WorkItemError("too_many_criteria", f"at most {LIMITS.max_criteria} acceptance criteria")
    return out


EVIDENCE_KINDS = ("file", "report", "test", "command", "commit", "link", "message", "note")


def normalize_evidence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkItemError("invalid_evidence", "evidence must be a list")
    out: list[dict[str, Any]] = []
    for raw in value[:LIMITS.max_evidence]:
        if isinstance(raw, str):
            raw = {"kind": "note", "text": raw}
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "note").strip().lower()
        if kind not in EVIDENCE_KINDS:
            kind = "note"
        entry: dict[str, Any] = {"kind": kind}
        for key, limit in (("path", 1000), ("server", 200), ("text", 2000), ("commit", 80), ("url", 2000), ("label", 300), ("turnUuid", 80), ("diff", 400_000)):
            text = _text(raw.get(key), limit)
            if text:
                entry[key] = text
        if kind == "commit" and not entry.get("commit") and entry.get("text"):
            entry["commit"] = entry.pop("text")[:80]
        if kind == "link" and not entry.get("url") and entry.get("text", "").startswith(("http://", "https://")):
            entry["url"] = entry.pop("text")
        if any(entry.get(key) for key in ("path", "text", "commit", "url", "turnUuid")):
            out.append(entry)
    return out


def concrete_evidence(evidence: list[dict[str, Any]]) -> bool:
    return any(item.get("kind") != "note" for item in evidence)


@dataclass
class Actor:
    """Who performs a mutation. Built by the runtime, never from model arguments."""

    kind: str  # user|controller|cron|webhook|collab|system
    label: str = ""
    turn_uuid: str = ""
    op_id: str = ""
    tool_call_id: str = ""

    @property
    def display(self) -> str:
        return self.label or ACTOR_LABELS.get(self.kind, self.kind)

    def link(self) -> dict[str, Any]:
        out = {key: value for key, value in (("turnUuid", self.turn_uuid), ("opId", self.op_id),
                                             ("toolOpId", f"tool:{self.tool_call_id}" if self.tool_call_id else "")) if value}
        return out


@dataclass
class Source:
    """A resolved, durable source carrying (or not carrying) the user's authority."""

    type: str  # message|interaction|panel|turn
    human: bool
    actor: str
    turn_uuid: str = ""
    op_id: str = ""
    text: str = ""
    interaction_id: str = ""
    created_at_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        out = {"type": self.type, "human": self.human}
        for key, value in (("turnUuid", self.turn_uuid), ("opId", self.op_id), ("interactionId", self.interaction_id),
                           ("createdAtMs", self.created_at_ms)):
            if value:
                out[key] = value
        out.update(self.extra)
        return out


def require_decision(source: Source | None, message: str) -> None:
    if source is None or not source.human:
        raise WorkItemError("source_not_user", message)
    result = source.extra.get("interaction", {}).get("result", {})
    if result.get("decision") in {"feedback", "reject"}:
        raise WorkItemError("source_not_authorized", "feedback or rejection does not approve this action")


def _row(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "itemUuid": data["item_uuid"],
        "number": data["number"],
        "parent": data["parent_number"],
        "seq": int(data["seq"]),
        "order": int(data["sort_order"]),
        "title": data["title"],
        "description": data["description"],
        "criteria": _loads(data["criteria_json"], []),
        "scopeMethod": data["scope_method"],
        "status": data["status"],
        "statusLabel": STATUS_LABELS.get(data["status"], data["status"]),
        "statusReason": data["status_reason"],
        "evidence": _loads(data["evidence_json"], []),
        "revision": int(data["revision"]),
        "top": not data["parent_number"],
        "createdAtMs": int(data["created_at_ms"]),
        "updatedAtMs": int(data["updated_at_ms"]),
        "deletedAtMs": int(data["deleted_at_ms"]),
    }


def _event_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "eventUuid": data["event_uuid"],
        "id": int(data["id"]),
        "itemUuid": data["item_uuid"],
        "number": data["number"],
        "kind": data["kind"],
        "label": EVENT_LABELS.get(data["kind"], data["kind"]),
        "actor": data["actor"],
        "actorLabel": data["actor_label"] or ACTOR_LABELS.get(data["actor"], data["actor"]),
        "summary": data["summary"],
        "data": _loads(data["data_json"], {}),
        "source": _loads(data["source_json"], {}),
        "turnUuid": data["turn_uuid"],
        "createdAtMs": int(data["created_at_ms"]),
    }


def derived_status(item: dict[str, Any], children: list[dict[str, Any]]) -> str:
    """Sections (non-top items with children) take their status from their leaves."""
    if item["top"] or not children or item["status"] in CLOSED:
        return item["status"]
    statuses = [child.get("effectiveStatus", child["status"]) for child in children]
    if all(status == "dropped" for status in statuses):
        return "dropped"
    if all(status in CLOSED for status in statuses):
        return "done"
    if any(status in {"doing", "done"} for status in statuses):
        return "doing"
    return "todo"


class WorkItemsDAO:
    def __init__(self, db: DB) -> None:
        self.db = db

    # ---------- reads ----------

    async def rows(self, conversation_uuid: str, *, include_deleted: bool = False, conn: Any = None) -> list[dict[str, Any]]:
        conn = conn or self.db.conn
        cur = await conn.execute(
            "SELECT * FROM work_items WHERE conversation_uuid=?" + ("" if include_deleted else " AND deleted_at_ms=0")
            + " ORDER BY parent_number, sort_order, seq",
            (conversation_uuid,),
        )
        return [_row(row) for row in await cur.fetchall()]

    async def get_row(self, conversation_uuid: str, number: str, *, conn: Any = None) -> dict[str, Any]:
        conn = conn or self.db.conn
        cur = await conn.execute(
            "SELECT * FROM work_items WHERE conversation_uuid=? AND number=? AND deleted_at_ms=0",
            (conversation_uuid, normalize_number(number)),
        )
        row = await cur.fetchone()
        if row is None:
            raise WorkItemNotFound("work_item_not_found", f"#{number} does not exist in this conversation")
        return _row(row)

    async def events(self, conversation_uuid: str, item_uuid: str) -> list[dict[str, Any]]:
        cur = await self.db.conn.execute(
            "SELECT * FROM work_item_events WHERE conversation_uuid=? AND item_uuid=? ORDER BY id",
            (conversation_uuid, item_uuid),
        )
        return [_event_row(row) for row in await cur.fetchall()]

    async def tree(self, conversation_uuid: str) -> list[dict[str, Any]]:
        """Nested items with derived section status and leaf progress."""
        rows = await self.rows(conversation_uuid)
        by_parent: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_parent.setdefault(row["parent"], []).append(row)

        def build(item: dict[str, Any]) -> dict[str, Any]:
            kids = [build(child) for child in by_parent.get(item["number"], [])]
            node = {**item, "children": kids}
            node["effectiveStatus"] = derived_status(item, kids)
            node["effectiveLabel"] = STATUS_LABELS.get(node["effectiveStatus"], node["effectiveStatus"])
            leaves = _leaves(node)
            live = [leaf for leaf in leaves if leaf["status"] != "dropped"]
            node["progress"] = {
                "done": sum(1 for leaf in live if leaf["status"] == "done"),
                "total": len(live),
                "dropped": len(leaves) - len(live),
                "ask": sum(1 for leaf in live if leaf["status"] == "ask"),
                "blocked": sum(1 for leaf in live if leaf["status"] == "blocked"),
            } if kids else None
            return node

        return [build(item) for item in by_parent.get("", [])]

    async def find(self, conversation_uuid: str, number: str) -> dict[str, Any]:
        number = normalize_number(number)
        for node in _walk(await self.tree(conversation_uuid)):
            if node["number"] == number:
                return node
        raise WorkItemNotFound("work_item_not_found", f"#{number} does not exist in this conversation")

    async def detail(self, conversation_uuid: str, number: str) -> dict[str, Any]:
        node = await self.find(conversation_uuid, number)
        node = {**node, "events": await self.events(conversation_uuid, node["itemUuid"])}
        ancestors = []
        parent = node["parent"]
        while parent:
            row = await self.get_row(conversation_uuid, parent)
            ancestors.insert(0, {"number": row["number"], "title": row["title"]})
            parent = row["parent"]
        node["ancestors"] = ancestors
        return node

    # ---------- sources ----------

    async def turn_input(self, conversation_uuid: str, root_turn_uuid: str) -> Source | None:
        """The latest input message of a root turn, whatever its origin."""
        if not root_turn_uuid:
            return None
        cur = await self.db.conn.execute(
            """SELECT op_id, turn_uuid, run_root_turn_uuid, source, payload_json, created_at_ms FROM web_operations
               WHERE conversation_uuid=? AND op_type='user_message' AND (run_root_turn_uuid=? OR turn_uuid=?)
               ORDER BY display_seq DESC LIMIT 1""",
            (conversation_uuid, root_turn_uuid, root_turn_uuid),
        )
        row = await cur.fetchone()
        return _message_source(row) if row is not None else None

    async def human_message(self, conversation_uuid: str, turn_uuid: str) -> Source:
        """The user's own latest message in a turn (root or steering turn id)."""
        cur = await self.db.conn.execute(
            f"""SELECT op_id, turn_uuid, run_root_turn_uuid, source, payload_json, created_at_ms FROM web_operations
               WHERE conversation_uuid=? AND op_type='user_message' AND (run_root_turn_uuid=? OR turn_uuid=?)
                 AND source IN ({','.join('?' for _ in HUMAN_SOURCES)})
               ORDER BY display_seq DESC LIMIT 1""",
            (conversation_uuid, turn_uuid, turn_uuid, *sorted(HUMAN_SOURCES)),
        )
        row = await cur.fetchone()
        if row is None:
            raise WorkItemError("source_not_user", f"turn {turn_uuid} has no message from the user")
        return _message_source(row)

    async def interaction(self, conversation_uuid: str, interaction_id: str) -> dict[str, Any]:
        cur = await self.db.conn.execute(
            "SELECT * FROM user_interactions WHERE interaction_id=? AND conversation_uuid=?",
            (str(interaction_id or "").strip(), conversation_uuid),
        )
        row = await cur.fetchone()
        if row is None:
            raise WorkItemError("interaction_not_found", f"no interaction {interaction_id} in this conversation")
        if str(row["status"] or "") != "answered":
            raise WorkItemError("interaction_not_answered", f"interaction {interaction_id} was not answered by the user")
        payload = _loads(row["payload_json"], {})
        result = _loads(row["result_json"], {})
        if result.get("cancelled"):
            raise WorkItemError("interaction_not_answered", f"interaction {interaction_id} was cancelled")
        return {"row": dict(row), "payload": payload, "result": result}

    async def resolve_source(self, conversation_uuid: str, actor: Actor, args: dict[str, Any], *,
                             require_human: bool = False, require_quote: bool = False) -> Source:
        """Resolve the source named by tool arguments against durable records.

        - interactionId: an answered UserInteraction in this conversation;
        - sourceTurnUuid: the user's message in that turn;
        - otherwise the current turn's input.
        A quote, when given (or required), must appear in the resolved message.
        """
        interaction_id = _text(args.get("interactionId"), 200)
        if interaction_id:
            data = await self.interaction(conversation_uuid, interaction_id)
            row = data["row"]
            result = data["result"]
            if require_quote and require_human and result.get("decision") in {"feedback", "reject"}:
                raise WorkItemError("source_not_authorized", "feedback or rejection does not approve this action; cite the user's decision")
            source = Source("interaction", True, "user", turn_uuid=str(row.get("turn_uuid") or ""),
                            op_id=f"tool:{row['tool_call_id']}" if row.get("tool_call_id") else "",
                            text=interaction_quote_text(data), interaction_id=interaction_id,
                            created_at_ms=int(row.get("resolved_at_ms") or 0), extra={"interaction": data})
        elif turn := _text(args.get("sourceTurnUuid"), 200):
            source = await self.human_message(conversation_uuid, turn)
        elif actor.kind == "user":
            return Source("panel", True, "user", turn_uuid=actor.turn_uuid, op_id=actor.op_id, created_at_ms=now_ms())
        else:
            found = await self.turn_input(conversation_uuid, actor.turn_uuid)
            if found is None:
                if require_human:
                    raise WorkItemError("source_not_user", "this needs the user's own decision: cite interactionId or sourceTurnUuid")
                return Source("turn", False, actor.kind, turn_uuid=actor.turn_uuid)
            source = found
        if require_human and not source.human:
            raise WorkItemError(
                "source_not_user",
                "this needs the user's own decision; the current turn was started by "
                f"{ACTOR_LABELS.get(source.actor, source.actor)}. Cite interactionId or the sourceTurnUuid of the user's message.",
            )
        quote = _text(args.get("quote"), LIMITS.quote_chars)
        if source.type in {"message", "interaction"} and (quote or (require_quote and require_human)):
            if not quote:
                raise WorkItemError("quote_required", "quote the user's words that make this decision")
            if _squash(quote) not in _squash(source.text):
                raise WorkItemError("quote_not_found", "the quote does not appear in the cited user message or answer")
            source.extra["quote"] = quote
        return source

    # ---------- writes ----------

    async def _event(self, conn: Any, conversation_uuid: str, item: dict[str, Any], kind: str, actor_kind: str,
                     actor_label: str, *, summary: str = "", data: dict[str, Any] | None = None,
                     source: dict[str, Any] | None = None, turn_uuid: str = "", at: int = 0) -> dict[str, Any]:
        event_uuid = f"wie_{uuid.uuid4().hex}"
        await conn.execute(
            """INSERT INTO work_item_events (event_uuid, conversation_uuid, item_uuid, number, kind, actor, actor_label,
               summary, data_json, source_json, turn_uuid, created_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_uuid, conversation_uuid, item["itemUuid"], item["number"], kind, actor_kind, actor_label,
             _text(summary), _dumps(data or {}), _dumps(source or {}), turn_uuid, at or now_ms()),
        )
        return {"eventUuid": event_uuid, "kind": kind}

    async def _model_event(self, conn: Any, conversation_uuid: str, item: dict[str, Any], kind: str, actor: Actor, **kw: Any) -> None:
        source = {**actor.link(), **(kw.pop("source", None) or {})}
        if actor.kind == "user":
            source.setdefault("type", "panel")
        await self._event(conn, conversation_uuid, item, kind, actor.kind, actor.display,
                          source=source, turn_uuid=actor.turn_uuid, **kw)

    async def _source_event(self, conn: Any, conversation_uuid: str, item: dict[str, Any], kind: str, actor: Actor,
                            source: Source, **kw: Any) -> None:
        """An event attributed to whoever the source belongs to (usually the user)."""
        kind_actor = "user" if source.human else (source.actor or actor.kind)
        label = ACTOR_LABELS.get(kind_actor, actor.display) if kind_actor != actor.kind else actor.display
        public = source.public()
        interaction = public.pop("interaction", None)
        if interaction:
            data = dict(kw.pop("data", None) or {})
            recorded = interaction_decision(interaction)
            kw["data"] = {**recorded, **data}
            if source.extra.get("quote"):
                kw["data"].setdefault("quote", source.extra["quote"])
        if actor.tool_call_id:
            public.setdefault("toolOpId", f"tool:{actor.tool_call_id}")
        await self._event(conn, conversation_uuid, item, kind, kind_actor, label, source=public,
                          turn_uuid=source.turn_uuid or actor.turn_uuid, **kw)

    async def _next_seq(self, conn: Any, conversation_uuid: str, parent: str) -> int:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM work_items WHERE conversation_uuid=? AND parent_number=?",
            (conversation_uuid, parent),
        )
        return int((await cur.fetchone())[0] or 0) + 1

    async def _insert(self, conn: Any, conversation_uuid: str, parent: str, spec: dict[str, Any], actor: Actor) -> dict[str, Any]:
        depth = parent.count(".") + 2 if parent else 1
        if depth > MAX_DEPTH:
            raise WorkItemError("too_deep", f"work items nest at most {MAX_DEPTH} levels")
        title = _text(spec.get("title"), LIMITS.title_chars)
        if not title:
            raise WorkItemError("title_required", "every work item needs a title")
        seq = await self._next_seq(conn, conversation_uuid, parent)
        number = f"{parent}.{seq}" if parent else str(seq)
        status = "discussing" if not parent else "todo"
        if not parent and spec.get("status") in OPEN_TOP:
            status = spec["status"]
        ts = now_ms()
        item_uuid = f"wi_{uuid.uuid4().hex}"
        await conn.execute(
            """INSERT INTO work_items (item_uuid, conversation_uuid, number, parent_number, seq, sort_order, title,
               description, criteria_json, scope_method, status, created_turn_uuid,
               created_at_ms, updated_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_uuid, conversation_uuid, number, parent, seq, seq, title, _text(spec.get("description")),
             _dumps(_criteria(spec.get("criteria"))), _text(spec.get("scopeMethod"), 2000), status,
             actor.turn_uuid, ts, ts),
        )
        return await self.get_row(conversation_uuid, number, conn=conn)

    async def create(self, conversation_uuid: str, actor: Actor, spec: dict[str, Any], *, source: Source | None = None) -> dict[str, Any]:
        """A new top-level task, optionally with children; records who raised it and in what words."""
        children = spec.get("children") or []
        if not isinstance(children, list):
            raise WorkItemError("invalid_children", "children must be a list")
        async with self.db.write_transaction(label="work_item_create") as conn:
            item = await self._insert(conn, conversation_uuid, "", spec, actor)
            src = source or Source("turn", False, actor.kind, turn_uuid=actor.turn_uuid)
            data = {"quote": src.text[:LIMITS.quote_chars]} if src.text else {}
            await self._source_event(conn, conversation_uuid, item, "raise", actor, src, data=data)
            created = [item["number"]]
            created += await self._add_children(conn, conversation_uuid, item, children, actor)
            if len(created) > 1:
                await self._model_event(conn, conversation_uuid, item, "scope", actor,
                                        summary=f"加了 {len(created) - 1} 项", data={"added": created[1:]})
        return {"number": item["number"], "created": created}

    async def _add_children(self, conn: Any, conversation_uuid: str, parent: dict[str, Any], specs: list[Any], actor: Actor) -> list[str]:
        created: list[str] = []
        for spec in specs:
            spec = {"title": spec} if isinstance(spec, str) else spec
            if not isinstance(spec, dict):
                raise WorkItemError("invalid_children", "each child is a title or an object")
            if parent["status"] in CLOSED:
                raise WorkItemError("parent_closed", f"#{parent['number']} is {STATUS_LABELS[parent['status']]}; reopen it first")
            child = await self._insert(conn, conversation_uuid, parent["number"], spec, actor)
            created.append(child["number"])
            created += await self._add_children(conn, conversation_uuid, child, spec.get("children") or [], actor)
        return created

    async def add(self, conversation_uuid: str, actor: Actor, parent: str, specs: list[Any], *, reason: str = "") -> list[str]:
        if not specs:
            raise WorkItemError("children_required", "give at least one item to add")
        async with self.db.write_transaction(label="work_item_add") as conn:
            parent_row = await self.get_row(conversation_uuid, parent, conn=conn)
            await self._open_ancestors(conn, conversation_uuid, parent_row)
            if not parent_row["top"] and parent_row["status"] in CLOSED:
                raise WorkItemError("parent_closed", f"#{parent_row['number']} is closed; reopen it first")
            created = await self._add_children(conn, conversation_uuid, parent_row, specs, actor)
            kind = "user_edit" if actor.kind == "user" else "scope"
            summary = reason or ("加了子项" if actor.kind == "user" else f"加了 {len(created)} 项")
            await self._model_event(conn, conversation_uuid, parent_row, kind, actor, summary=summary, data={"added": created})
            await self._touch(conn, parent_row)
        return created

    async def _touch(self, conn: Any, item: dict[str, Any], **changes: Any) -> None:
        sets = ["revision=revision+1", "updated_at_ms=?"]
        values: list[Any] = [now_ms()]
        for column, value in changes.items():
            sets.append(f"{column}=?")
            values.append(value)
        await conn.execute(f"UPDATE work_items SET {', '.join(sets)} WHERE item_uuid=?", (*values, item["itemUuid"]))

    async def update(self, conversation_uuid: str, actor: Actor, number: str, *, revision: int,
                     changes: dict[str, Any], reason: str = "") -> dict[str, Any]:
        columns = {"title": ("title", lambda v: _text(v, LIMITS.title_chars)), "description": ("description", _text),
                   "criteria": ("criteria_json", lambda v: _dumps(_criteria(v))),
                   "scopeMethod": ("scope_method", lambda v: _text(v, 2000))}
        applied = {key: value for key, value in changes.items() if key in columns}
        if not applied:
            raise WorkItemError("nothing_to_update", "give title, description, criteria or scopeMethod")
        if "title" in applied and not _text(applied["title"], LIMITS.title_chars):
            raise WorkItemError("title_required", "the title cannot be empty")
        if "criteria" in applied and actor.kind != "user" and not reason:
            raise WorkItemError("reason_required", "changing acceptance criteria needs a reason")
        async with self.db.write_transaction(label="work_item_update") as conn:
            item = await self.get_row(conversation_uuid, number, conn=conn)
            if int(revision or 0) != item["revision"]:
                raise WorkItemConflict(
                    "revision_conflict",
                    f"#{item['number']} changed (revision {item['revision']}); get it again"
                    + ("; the user edited it in the panel" if await self._last_user_edit_after(conn, item, revision) else ""),
                )
            before = {key: item[key] for key in applied}
            await self._touch(conn, item, **{columns[key][0]: columns[key][1](value) for key, value in applied.items()})
            await self._model_event(conn, conversation_uuid, item, "user_edit" if actor.kind == "user" else "edit", actor,
                                    summary=reason or "改了" + "、".join(_FIELD_LABELS[key] for key in applied),
                                    data={"before": before, "after": {key: applied[key] for key in applied}})
            return await self.get_row(conversation_uuid, item["number"], conn=conn)

    async def _last_user_edit_after(self, conn: Any, item: dict[str, Any], revision: int) -> bool:
        cur = await conn.execute("SELECT actor, kind FROM work_item_events WHERE item_uuid=? ORDER BY id DESC LIMIT 1",
                                 (item["itemUuid"],))
        row = await cur.fetchone()
        return row is not None and row["actor"] == "user"

    async def note(self, conversation_uuid: str, actor: Actor, number: str, *, kind: str, summary: str,
                   files: list[dict[str, Any]] | None = None) -> None:
        if kind not in {"investigation", "proposal", "note"}:
            kind = "note"
        if not _text(summary):
            raise WorkItemError("summary_required", "write what was found or proposed")
        async with self.db.write_transaction(label="work_item_note") as conn:
            item = await self.get_row(conversation_uuid, number, conn=conn)
            await self._model_event(conn, conversation_uuid, item, kind, actor, summary=summary,
                                    data={"files": files} if files else {})
            await self._touch(conn, item)

    async def decide(self, conversation_uuid: str, actor: Actor, numbers: list[str], source: Source, *,
                     question_ids: dict[str, list[str]] | None = None, summary: str = "") -> list[str]:
        """Attach the user's decision, in its original form, to each item it concerns."""
        recorded: list[str] = []
        async with self.db.write_transaction(label="work_item_decide") as conn:
            for number in numbers:
                item = await self.get_row(conversation_uuid, number, conn=conn)
                data: dict[str, Any] = {}
                if source.type == "interaction":
                    wanted = (question_ids or {}).get(item["number"])
                    data = interaction_decision(source.extra["interaction"], wanted)
                    if wanted and not data.get("questions"):
                        raise WorkItemError("question_not_found", f"no answered question {wanted} in {source.interaction_id}")
                elif source.extra.get("quote"):
                    data = {"quote": source.extra["quote"]}
                elif source.text:
                    data = {"quote": source.text[:LIMITS.quote_chars]}
                await self._source_event(conn, conversation_uuid, item, "decision", actor, source, summary=summary, data=data)
                await self._touch(conn, item)
                recorded.append(item["number"])
        return recorded

    async def correct(self, conversation_uuid: str, actor: Actor, number: str, source: Source, *, old: str, new: str,
                      why: str, changes: dict[str, Any] | None = None) -> dict[str, Any]:
        if not (_text(old) or _text(new)):
            raise WorkItemError("correction_required", "say what was wrong and what it is now")
        async with self.db.write_transaction(label="work_item_correct") as conn:
            item = await self.get_row(conversation_uuid, number, conn=conn)
            data = {"old": _text(old, 2000), "new": _text(new, 2000), "why": _text(why, 2000)}
            if source.extra.get("quote"):
                data["quote"] = source.extra["quote"]
            elif source.text:
                data["quote"] = source.text[:LIMITS.quote_chars]
            await self._source_event(conn, conversation_uuid, item, "correction", actor, source, data=data)
            columns = {"title": ("title", lambda v: _text(v, LIMITS.title_chars)), "description": ("description", _text),
                       "criteria": ("criteria_json", lambda v: _dumps(_criteria(v))),
                       "scopeMethod": ("scope_method", lambda v: _text(v, 2000))}
            applied = {columns[key][0]: columns[key][1](value) for key, value in (changes or {}).items() if key in columns}
            await self._touch(conn, item, **applied)
            return await self.get_row(conversation_uuid, item["number"], conn=conn)

    async def transition(self, conversation_uuid: str, actor: Actor, number: str, action: str, *,
                         summary: str = "", reason: str = "", evidence: list[dict[str, Any]] | None = None,
                         source: Source | None = None, revision: int = 0, phase: str = "") -> dict[str, Any]:
        """One status change, validated against the state rules."""
        async with self.db.write_transaction(label=f"work_item_{action}") as conn:
            item = await self.get_row(conversation_uuid, number, conn=conn)
            if revision and int(revision) != item["revision"]:
                raise WorkItemConflict("revision_conflict", f"#{item['number']} changed (revision {item['revision']}); get it again")
            await self._open_ancestors(conn, conversation_uuid, item)
            kids = await self._children(conn, conversation_uuid, item["number"])
            before = item["status"]
            after, kind, data, evidence_out = self._next_state(item, kids, action, summary=summary, reason=reason,
                                                               evidence=evidence, source=source, phase=phase, actor=actor)
            if action in {"drop", "reopen"}:
                data.update(await self._cascade(conn, conversation_uuid, item, action, actor, source))
            changes: dict[str, Any] = {"status": after}
            if action in {"block", "ask"}:
                changes["status_reason"] = _text(reason, 2000)
            elif after not in {"blocked", "ask"}:
                changes["status_reason"] = ""
            if evidence_out is not None:
                changes["evidence_json"] = _dumps(evidence_out)
            await self._touch(conn, item, **changes)
            data = {"from": before, "to": after, **data}
            if source is not None and source.human and action in {"drop", "reopen", "phase"}:
                await self._source_event(conn, conversation_uuid, item, kind, actor, source, summary=summary or reason, data=data)
            else:
                await self._model_event(conn, conversation_uuid, item, kind, actor, summary=summary or reason, data=data)
            if after in {"doing", "in_progress"} or action == "start":
                await self._promote_top(conn, conversation_uuid, item, actor)
            return await self.get_row(conversation_uuid, item["number"], conn=conn)

    def _next_state(self, item: dict[str, Any], kids: list[dict[str, Any]], action: str, *, summary: str, reason: str,
                    evidence: list[dict[str, Any]] | None, source: Source | None, phase: str,
                    actor: Actor) -> tuple[str, str, dict[str, Any], list[dict[str, Any]] | None]:
        status, top, number = item["status"], item["top"], item["number"]
        open_kids = [kid for kid in kids if kid["status"] not in CLOSED]
        label = STATUS_LABELS.get(status, status)
        if action == "start":
            if top:
                if status in CLOSED:
                    raise WorkItemError("closed", f"#{number} is {label}; reopen it first")
                return "in_progress", "state", {}, None
            if status in CLOSED:
                raise WorkItemError("closed", f"#{number} is {label}; reopen it first")
            if kids:
                raise WorkItemError("has_children", f"#{number} has children; start one of them")
            return "doing", "state", {}, None
        if action == "complete":
            if status in CLOSED:
                raise WorkItemError("closed", f"#{number} is already {label}")
            if open_kids:
                raise WorkItemError(
                    "children_open",
                    f"#{number} still has open children: " + ", ".join(f"#{kid['number']} {STATUS_LABELS[kid['status']]}" for kid in open_kids[:10]),
                )
            if top and status == "discussing":
                raise WorkItemError("still_discussing", f"#{number} is still being discussed")
            if not _text(summary):
                raise WorkItemError("summary_required", "say what was done")
            items = normalize_evidence(evidence)
            if not kids and not concrete_evidence(items):
                raise WorkItemError("evidence_required",
                                    "completion needs concrete evidence: a file, report, test, command, commit, link or message reference")
            return "done", "done", {"evidence": items}, items or item["evidence"]
        if action in {"block", "ask"}:
            if status in CLOSED:
                raise WorkItemError("closed", f"#{number} is {label}")
            if not _text(reason):
                raise WorkItemError("reason_required", "say why" if action == "block" else "say what the user needs to decide")
            if kids:
                raise WorkItemError("has_children", f"mark the specific child of #{number} instead")
            return ("blocked" if action == "block" else "ask"), "state", {"reason": _text(reason, 2000)}, None
        if action == "drop":
            if status in CLOSED:
                raise WorkItemError("closed", f"#{number} is already {label}")
            require_decision(source, "only the user's decision can drop work")
            return ("abandoned" if top else "dropped"), "state", {"reason": _text(reason or summary, 2000)}, None
        if action == "reopen":
            if status not in CLOSED:
                raise WorkItemError("not_closed", f"#{number} is {label}, not closed")
            if not _text(reason):
                raise WorkItemError("reason_required", "say why it is reopened")
            if status in {"dropped", "abandoned"}:
                require_decision(source, "reopening work the user dropped needs the user's own decision")
            return ("in_progress" if top else "todo"), "state", {"reason": _text(reason, 2000)}, None
        if action == "phase":
            if not top:
                raise WorkItemError("not_top_level", "phase applies to top-level tasks")
            if phase not in {"discussing", "decided", "in_progress"}:
                raise WorkItemError("invalid_phase", "phase is discussing, decided or in_progress (use complete / drop to close)")
            if status in CLOSED:
                raise WorkItemError("closed", f"#{number} is {label}; reopen it first")
            if phase == "decided":
                require_decision(source, "a task becomes decided on the user's decision: cite it")
            return phase, "state", {}, None
        raise WorkItemError("unsupported_action", action)

    async def _open_ancestors(self, conn: Any, conversation_uuid: str, item: dict[str, Any]) -> None:
        parent = item["parent"]
        while parent:
            row = await self.get_row(conversation_uuid, parent, conn=conn)
            if row["status"] in CLOSED:
                raise WorkItemError("parent_closed", f"#{parent} is closed; reopen it first")
            parent = row["parent"]

    async def _cascade(self, conn: Any, conversation_uuid: str, item: dict[str, Any], action: str,
                       actor: Actor, source: Source | None) -> dict[str, Any]:
        rows = [row for row in await self.rows(conversation_uuid, conn=conn)
                if row["number"].startswith(item["number"] + ".")]
        if not rows:
            return {}
        latest = {}
        if action == "reopen":
            cur = await conn.execute(
                "SELECT number, data_json FROM work_item_events WHERE conversation_uuid=? AND "
                "(number=? OR number LIKE ?) AND kind IN ('state','done') ORDER BY id DESC",
                (conversation_uuid, item["number"], item["number"] + ".%"),
            )
            for event in await cur.fetchall():
                latest.setdefault(event["number"], _loads(event["data_json"], {}))
            group = latest.get(item["number"], {}).get("cascade")
            if not group:
                return {}
        else:
            group = uuid.uuid4().hex
        closed = [row["number"] for row in rows if row["status"] in CLOSED]
        changed = []
        for row in rows:
            if action == "drop":
                if any(in_subtree(row["number"], number) for number in closed):
                    continue
                after, reason = "dropped", ""
            else:
                previous = latest.get(row["number"], {})
                if row["status"] != "dropped" or previous.get("cascade") != group:
                    continue
                after = previous.get("from", "todo")
                if after == "doing":
                    after = "todo"
                reason = previous.get("fromReason", "") if after in {"blocked", "ask"} else ""
            await self._touch(conn, row, status=after, status_reason=reason)
            data = {"from": row["status"], "to": after, "fromReason": row["statusReason"],
                    "cascade": group, "cascadeRoot": item["number"]}
            await self._source_event(conn, conversation_uuid, row, "state", actor, source,
                                     summary=f"随 #{item['number']} " + ("标为不做" if action == "drop" else "重新打开"), data=data)
            changed.append(row["number"])
        return {"cascade": group, "cascadeItems": changed}

    async def _children(self, conn: Any, conversation_uuid: str, number: str) -> list[dict[str, Any]]:
        cur = await conn.execute(
            "SELECT * FROM work_items WHERE conversation_uuid=? AND deleted_at_ms=0 AND (number LIKE ?) ORDER BY number",
            (conversation_uuid, number + ".%"),
        )
        rows = [_row(row) for row in await cur.fetchall()]
        # Sections carry no status of their own; judge them by their leaves.
        numbers = {row["number"] for row in rows}
        return [row for row in rows if not any(other.startswith(row["number"] + ".") for other in numbers)]

    async def _promote_top(self, conn: Any, conversation_uuid: str, item: dict[str, Any], actor: Actor) -> None:
        top = await self.get_row(conversation_uuid, top_of(item["number"]), conn=conn)
        if top["number"] != item["number"] and top["status"] in {"discussing", "decided"}:
            await self._touch(conn, top, status="in_progress")
            await self._model_event(conn, conversation_uuid, top, "state", actor,
                                    data={"from": top["status"], "to": "in_progress"})

    async def delete(self, conversation_uuid: str, actor: Actor, number: str, *, reason: str) -> list[str]:
        if not _text(reason):
            raise WorkItemError("reason_required", "say why this item was created by mistake")
        async with self.db.write_transaction(label="work_item_delete") as conn:
            item = await self.get_row(conversation_uuid, number, conn=conn)
            cur = await conn.execute(
                "SELECT number FROM work_items WHERE conversation_uuid=? AND deleted_at_ms=0 AND (number=? OR number LIKE ?)",
                (conversation_uuid, item["number"], item["number"] + ".%"),
            )
            removed = [str(row[0]) for row in await cur.fetchall()]
            ts = now_ms()
            await conn.execute(
                "UPDATE work_items SET deleted_at_ms=?, updated_at_ms=?, revision=revision+1 WHERE conversation_uuid=? AND (number=? OR number LIKE ?) AND deleted_at_ms=0",
                (ts, ts, conversation_uuid, item["number"], item["number"] + ".%"),
            )
            target = await self.get_row(conversation_uuid, item["parent"], conn=conn) if item["parent"] else item
            await self._model_event(conn, conversation_uuid, target, "delete", actor, summary=_text(reason, 2000),
                                    data={"removed": removed, "title": item["title"]})
        return removed

    async def record(self, conversation_uuid: str, number: str, kind: str, actor: Actor, *, summary: str = "",
                     data: dict[str, Any] | None = None) -> None:
        async with self.db.write_transaction(label="work_item_record") as conn:
            item = await self.get_row(conversation_uuid, number, conn=conn)
            await self._model_event(conn, conversation_uuid, item, kind, actor, summary=summary, data=data or {})

    # ---------- focus ----------

    async def set_focus(self, conversation_uuid: str, root_turn_uuid: str, numbers: list[str], *, basis: dict[str, Any],
                        actor: Actor) -> dict[str, Any]:
        if not root_turn_uuid:
            raise WorkItemError("no_turn", "focus belongs to a turn")
        clean = list(dict.fromkeys(normalize_number(number) for number in numbers))
        if not clean:
            raise WorkItemError("numbers_required", "name the items to work on")
        ts = now_ms()
        async with self.db.write_transaction(label="work_item_focus") as conn:
            rows = [await self.get_row(conversation_uuid, number, conn=conn) for number in clean]
            for row in rows:
                await self._open_ancestors(conn, conversation_uuid, row)
                if row["status"] in CLOSED:
                    raise WorkItemError("closed", f"#{row['number']} is {STATUS_LABELS[row['status']]}; reopen it first")
            # A section inside another focused section is already covered.
            clean = [n for n in clean if not any(n != other and in_subtree(n, other) for other in clean)]
            await conn.execute(
                """INSERT INTO work_item_focus (conversation_uuid, root_turn_uuid, numbers_json, current_number, basis_json,
                   created_at_ms, updated_at_ms) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(conversation_uuid, root_turn_uuid) DO UPDATE SET numbers_json=excluded.numbers_json,
                   basis_json=excluded.basis_json, updated_at_ms=excluded.updated_at_ms""",
                (conversation_uuid, root_turn_uuid, _dumps(clean), "", _dumps(basis), ts, ts),
            )
            for row in rows:
                if row["number"] not in clean:
                    continue
                await self._event(conn, conversation_uuid, row, "focus", "user" if basis.get("human") else actor.kind,
                                  "你" if basis.get("human") else actor.display,
                                  summary=f"这一轮做 #{row['number']}", data={"numbers": clean},
                                  source={k: v for k, v in basis.items() if k != "human"}, turn_uuid=root_turn_uuid, at=ts)
                top = await self.get_row(conversation_uuid, top_of(row["number"]), conn=conn)
                if top["status"] in {"discussing", "decided"}:
                    await self._touch(conn, top, status="in_progress")
                    await self._model_event(conn, conversation_uuid, top, "state", actor,
                                            data={"from": top["status"], "to": "in_progress"})
        return await self.focus(conversation_uuid, root_turn_uuid) or {}

    async def focus(self, conversation_uuid: str, root_turn_uuid: str) -> dict[str, Any] | None:
        if not root_turn_uuid:
            return None
        cur = await self.db.conn.execute(
            "SELECT * FROM work_item_focus WHERE conversation_uuid=? AND root_turn_uuid=?",
            (conversation_uuid, root_turn_uuid),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {"rootTurnUuid": row["root_turn_uuid"], "numbers": _loads(row["numbers_json"], []),
                "current": row["current_number"], "basis": _loads(row["basis_json"], {}),
                "createdAtMs": int(row["created_at_ms"]),
                "updatedAtMs": int(row["updated_at_ms"])}

    async def latest_focus(self, conversation_uuid: str) -> dict[str, Any] | None:
        cur = await self.db.conn.execute(
            "SELECT root_turn_uuid FROM work_item_focus WHERE conversation_uuid=? ORDER BY updated_at_ms DESC LIMIT 1",
            (conversation_uuid,),
        )
        row = await cur.fetchone()
        return await self.focus(conversation_uuid, str(row[0])) if row else None

    async def set_current(self, conversation_uuid: str, root_turn_uuid: str, number: str) -> None:
        await self.db.conn.execute(
            "UPDATE work_item_focus SET current_number=?, updated_at_ms=? WHERE conversation_uuid=? AND root_turn_uuid=?",
            (number, now_ms(), conversation_uuid, root_turn_uuid),
        )
        await self.db.conn.commit()

    async def unfinished(self, conversation_uuid: str, root_turn_uuid: str) -> list[dict[str, Any]]:
        """Items this turn still owes work on: what the end-of-turn check asks about."""
        focus = await self.focus(conversation_uuid, root_turn_uuid)
        if not focus:
            return []
        roots = list(focus["numbers"])
        nodes = {node["number"]: node for node in _walk(await self.tree(conversation_uuid))}
        out: dict[str, dict[str, Any]] = {}
        for root in roots:
            node = nodes.get(root)
            if node is None or any(nodes.get(number, {}).get("status") in CLOSED
                                   for number in nodes if in_subtree(root, number)):
                continue
            for leaf in _leaves(node):
                if leaf["status"] not in UNFINISHED:
                    continue
                out[leaf["number"]] = leaf
            if node["top"] and node["status"] == "in_progress" and node["children"] and not any(
                    leaf["status"] not in CLOSED for leaf in _leaves(node)):
                out[node["number"]] = {**node, "status": "in_progress", "closeTask": True}
        return [out[key] for key in sorted(out, key=_number_key)]

    async def turn_status_lines(self, conversation_uuid: str, root_turn_uuid: str) -> list[str]:
        """One plain line per task this turn worked on, like the status line after the reply in the console."""
        if not conversation_uuid or not root_turn_uuid:
            return []
        cur = await self.db.conn.execute(
            "SELECT DISTINCT number FROM work_item_events WHERE conversation_uuid=? AND turn_uuid=?",
            (conversation_uuid, root_turn_uuid),
        )
        tops = {top_of(str(row[0])) for row in await cur.fetchall()}
        focus = await self.focus(conversation_uuid, root_turn_uuid)
        tops |= {top_of(number) for number in (focus or {}).get("numbers") or []}
        if not tops:
            return []
        tasks = {node["number"]: node for node in await self.tree(conversation_uuid)}
        left = [item["number"] for item in await self.unfinished(conversation_uuid, root_turn_uuid)]
        lines = []
        for number in sorted(tops, key=_number_key):
            node = tasks.get(number)
            if node is None:
                continue
            live = [leaf for leaf in _leaves(node) if leaf["status"] != "dropped"]
            parts = [f"{sum(1 for leaf in live if leaf['status'] == 'done')}/{len(live)} 完成"]
            remaining = sum(1 for n in left if top_of(n) == number)
            if remaining:
                parts.append(f"本轮范围还剩 {remaining} 项没收尾")
            asks = sum(1 for leaf in live if leaf["status"] == "ask")
            if asks:
                parts.append(f"{asks} 项等你定")
            lines.append(f"任务 #{number} {node['title']}：" + " · ".join(parts))
        return lines

    async def recent_user_edits(self, conversation_uuid: str, limit: int | None = None) -> list[dict[str, Any]]:
        """What the user changed directly in the panel, newest last."""
        limit = LIMITS.recent_edits if limit is None else limit
        cur = await self.db.conn.execute(
            """SELECT * FROM work_item_events WHERE conversation_uuid=? AND actor='user'
               AND json_extract(source_json, '$.type')='panel' ORDER BY id DESC LIMIT ?""",
            (conversation_uuid, limit),
        )
        return [_event_row(row) for row in await cur.fetchall()][::-1]

    async def last_user_edit_id(self, conversation_uuid: str) -> int:
        cur = await self.db.conn.execute(
            """SELECT COALESCE(MAX(id),0) FROM work_item_events WHERE conversation_uuid=? AND actor='user'
               AND json_extract(source_json, '$.type')='panel'""",
            (conversation_uuid,),
        )
        return int((await cur.fetchone())[0] or 0)

    async def turn_focuses(self, conversation_uuid: str) -> list[dict[str, Any]]:
        cur = await self.db.conn.execute(
            "SELECT root_turn_uuid FROM work_item_focus WHERE conversation_uuid=? ORDER BY created_at_ms",
            (conversation_uuid,),
        )
        return [focus for row in await cur.fetchall() if (focus := await self.focus(conversation_uuid, str(row[0])))]

    # ---------- conversation lifecycle ----------

    async def hard_delete_conversation(self, conversation_uuid: str, *, conn: Any = None) -> int:
        conn = conn or self.db.conn
        cur = await conn.execute("DELETE FROM work_items WHERE conversation_uuid=?", (conversation_uuid,))
        removed = int(cur.rowcount or 0)
        await conn.execute("DELETE FROM work_item_events WHERE conversation_uuid=?", (conversation_uuid,))
        await conn.execute("DELETE FROM work_item_focus WHERE conversation_uuid=?", (conversation_uuid,))
        return removed

    async def duplicate_conversation(self, old_uuid: str, new_uuid: str, *, conn: Any = None,
                                     rewrite_text: Any = None) -> int:
        """Copy items and their history; focus belongs to the original turns and is not copied.

        rewrite_text maps identifiers that the duplicate re-allocates (Agent tasks,
        artifacts) inside stored text and JSON.
        """
        conn = conn or self.db.conn
        rewrite = rewrite_text or (lambda value: value)
        cur = await conn.execute("SELECT * FROM work_items WHERE conversation_uuid=? ORDER BY id", (old_uuid,))
        items = [dict(row) for row in await cur.fetchall()]
        mapping: dict[str, str] = {}
        for item in items:
            new_item = f"wi_{uuid.uuid4().hex}"
            mapping[item["item_uuid"]] = new_item
            item.update(item_uuid=new_item, conversation_uuid=new_uuid,
                        evidence_json=rewrite(item["evidence_json"]), created_turn_uuid=rewrite(item["created_turn_uuid"]))
            item.pop("id", None)
            columns = list(item)
            await conn.execute(
                f"INSERT INTO work_items ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [item[column] for column in columns],
            )
        cur = await conn.execute("SELECT * FROM work_item_events WHERE conversation_uuid=? ORDER BY id", (old_uuid,))
        for event in [dict(row) for row in await cur.fetchall()]:
            event.pop("id", None)
            event.update(event_uuid=f"wie_{uuid.uuid4().hex}", conversation_uuid=new_uuid,
                         item_uuid=mapping.get(event["item_uuid"], event["item_uuid"]),
                         source_json=rewrite(event["source_json"]), data_json=rewrite(event["data_json"]),
                         turn_uuid=rewrite(event["turn_uuid"]))
            columns = list(event)
            await conn.execute(
                f"INSERT INTO work_item_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [event[column] for column in columns],
            )
        return len(items)


_FIELD_LABELS = {"title": "标题", "description": "说明", "criteria": "完成标准", "scopeMethod": "范围说明"}


def _message_source(row: Any) -> Source:
    payload = _loads(row["payload_json"], {})
    origin = str(row["source"] or "")
    human = origin in HUMAN_SOURCES
    return Source("message", human, "user" if human else _AUTOMATED_SOURCES.get(origin, "system"),
                  turn_uuid=str(row["run_root_turn_uuid"] or row["turn_uuid"] or ""), op_id=str(row["op_id"] or ""),
                  text=str(payload.get("text") or ""), created_at_ms=int(row["created_at_ms"] or 0))


def interaction_quote_text(data: dict[str, Any]) -> str:
    payload, result = data["payload"], data["result"]
    action = payload.get("action") or result.get("action")
    if action == "confirm":
        text = str(result.get("text") or "")
        if result.get("decision") in {"confirm", "confirm_with_changes"}:
            return "\n".join(str(value or "") for value in (payload.get("title"), payload.get("body"), text))
        return text
    answers = result.get("answers") or [result]
    return "\n".join(str(value) for answer in answers if isinstance(answer, dict)
                     for value in [*(answer.get("selectedLabels") or []), answer.get("text") or answer.get("value") or ""])


def interaction_decision(data: dict[str, Any], question_ids: list[str] | None = None) -> dict[str, Any]:
    """The questions, options and the user's answers, as recorded by the interaction."""
    payload, result = data.get("payload") or {}, data.get("result") or {}
    out: dict[str, Any] = {"interactionId": payload.get("interactionId") or data.get("row", {}).get("interaction_id"),
                           "title": _text(payload.get("title"), 300), "action": payload.get("action") or ""}
    if (payload.get("action") or result.get("action")) == "questionnaire":
        questions = {str(q.get("id")): q for q in payload.get("questions") or [] if isinstance(q, dict)}
        rows = []
        for answer in result.get("answers") or []:
            if not isinstance(answer, dict):
                continue
            qid = str(answer.get("questionId") or "")
            if question_ids and qid not in question_ids:
                continue
            question = questions.get(qid, {})
            rows.append({
                "id": qid,
                "question": _text(answer.get("question") or question.get("question"), 1000),
                "options": [_text(opt.get("label"), 500) for opt in question.get("options") or [] if isinstance(opt, dict)],
                "selected": [_text(label, 500) for label in answer.get("selectedLabels") or []],
                "text": _text(answer.get("text"), LIMITS.quote_chars),
            })
        out["questions"] = rows
        return out
    out["questions"] = [{
        "id": "",
        "question": _text(payload.get("body") or payload.get("title"), 1000),
        "options": [_text(opt.get("label") if isinstance(opt, dict) else opt, 500) for opt in payload.get("options") or []],
        "selected": [_text(label, 500) for label in (result.get("selectedLabels") or ([result.get("decision")] if result.get("decision") else []))],
        "text": _text(result.get("text") or result.get("value"), LIMITS.quote_chars),
    }]
    return out


def _walk(nodes: list[dict[str, Any]]):
    for node in nodes:
        yield node
        yield from _walk(node["children"])


def _leaves(node: dict[str, Any]) -> list[dict[str, Any]]:
    if not node["children"]:
        return [node]
    return [leaf for child in node["children"] for leaf in _leaves(child)]


def _number_key(number: str) -> tuple[int, ...]:
    return tuple(int(part) for part in number.split("."))


def walk(nodes: list[dict[str, Any]]):
    return _walk(nodes)


def leaves(node: dict[str, Any]) -> list[dict[str, Any]]:
    return _leaves(node)


# ---------- model-visible state ----------

_TZ = ZoneInfo("Asia/Shanghai")


def _fmt_time(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, _TZ).strftime("%m-%d %H:%M")


def render_outline(tree: list[dict[str, Any]], *, focus: dict[str, Any] | None = None,
                   recent: list[dict[str, Any]] | None = None, max_chars: int = 0) -> str:
    """Open tasks with their unfinished items; closed items only as counts."""
    max_chars = max_chars or LIMITS.outline_chars
    opening, closing = "<work-items>\n", "\n</work-items>"
    budget = max(0, max_chars - len(opening) - len(closing))
    reserve = min(80, budget)
    remaining = budget - reserve
    lines: list[str] = []
    omitted = omitted_tasks = 0
    shortened = False

    def add(line: str) -> bool:
        nonlocal remaining
        cost = len(line) + bool(lines)
        if cost > remaining:
            return False
        lines.append(line)
        remaining -= cost
        return True

    focus_line = ""
    if focus and focus.get("numbers"):
        basis = focus.get("basis") or {}
        who = "用户" if basis.get("human") else ACTOR_LABELS.get(str(basis.get("actor") or ""), "本轮输入")
        focus_line = "本轮执行范围：" + "、".join(f"#{n}" for n in focus["numbers"])
        focus_line += f"（依据：{who} {_fmt_time(int(basis.get('createdAtMs') or focus.get('createdAtMs') or 0))} 的消息）"
        cap = max(0, min(len(focus_line), remaining // 3))
        if len(focus_line) > cap:
            focus_line = focus_line[:max(0, cap - 1)] + ("…" if cap else "")
            shortened = True
        remaining -= len(focus_line) + bool(focus_line)
    active = [node for node in tree if node["status"] in OPEN_TOP]
    for node in active:
        progress = node.get("progress")
        suffix = f" [{STATUS_LABELS[node['status']]}]"
        if progress:
            suffix += f" {progress['done']}/{progress['total']}"
            if progress["dropped"]:
                suffix += f"，另有 {progress['dropped']} 项按决定不做"
        elif node.get("statusReason"):
            suffix += f"：{node['statusReason'][:120]}"
        title = node["title"]
        cap = max(0, min(len(title), remaining - len(suffix) - len(node['number']) - 3))
        if cap < len(title):
            title = title[:max(0, cap - 1)] + ("…" if cap else "")
            shortened = True
        live = [leaf for leaf in _leaves(node) if leaf is not node and leaf["status"] not in CLOSED]
        if not add(f"#{node['number']} {title}{suffix}"):
            omitted_tasks += 1
            omitted += len(live) or 1
            continue
        for leaf in live:
            line = f"  #{leaf['number']} {leaf['title']} [{STATUS_LABELS[leaf['status']]}]"
            if leaf["status"] in {"blocked", "ask"} and leaf.get("statusReason"):
                line += f"：{leaf['statusReason'][:120]}"
            if not add(line):
                omitted += 1
    if focus_line:
        remaining += len(focus_line) + 1
        add(focus_line)
    if recent:
        line = "最近改动：" + "；".join(
            f"{_fmt_time(event['createdAtMs'])} 用户 #{event['number']} {event['summary']}"[:200] for event in recent)
        if not add(line):
            shortened = True
    remaining += reserve
    if omitted or shortened:
        add(f"……省略 {omitted_tasks} 个任务、{omitted} 项未完成" + ("，部分文字已省略" if shortened else "")
            + "；用 WorkItem get 查看。")
    if not lines:
        return ""
    return opening + "\n".join(lines) + closing


def render_briefing(nodes: list[dict[str, Any]], events_by_item: dict[str, list[dict[str, Any]]],
                    ancestors: dict[str, list[dict[str, Any]]], focus: dict[str, Any],
                    max_chars: int = 0, *, header: list[str] | None = None,
                    tag: str = "work-item-focus") -> str:
    """What the user asked, decided and corrected for the focused items, in their own words."""
    max_chars = max_chars or LIMITS.briefing_chars
    numbers = "、".join(f"#{n}" for n in focus.get("numbers") or [])
    parts = [f"<{tag}>", *(header or [
        f"本轮执行范围：{numbers}。只做这些项；范围外发现的问题记成 note 或新加一项，不顺手改。",
        "下面是这些项的完成标准、用户的原话和决策原文。引用的讨论如果已不在上下文里，先用 History 按 turn 读原文再动手。",
    ])]
    for node in nodes:
        chain = " › ".join(f"#{a['number']} {a['title']}" for a in ancestors.get(node["number"], []))
        parts.append("")
        parts.append(f"## #{node['number']} {node['title']}" + (f"（属于 {chain}）" if chain else ""))
        if node.get("description"):
            parts.append(f"说明：{node['description']}")
        if node.get("criteria"):
            parts.append("完成标准：" + "；".join(node["criteria"]))
        if node.get("scopeMethod"):
            parts.append(f"范围怎么找全的：{node['scopeMethod']}")
        rows = [leaf for leaf in _leaves(node) if leaf is not node]
        if rows:
            parts.append("子项：")
            for leaf in rows:
                line = f"  #{leaf['number']} {leaf['title']} [{STATUS_LABELS[leaf['status']]}]"
                if leaf.get("criteria"):
                    line += " 完成标准：" + "；".join(leaf["criteria"])
                parts.append(line)
        seen: set[str] = set()
        for number in [*(a["number"] for a in ancestors.get(node["number"], [])), node["number"],
                       *(leaf["number"] for leaf in rows)]:
            for event in events_by_item.get(number, []):
                if event["eventUuid"] in seen or event["kind"] not in {"raise", "decision", "correction", "investigation", "proposal", "user_edit"}:
                    continue
                seen.add(event["eventUuid"])
                parts.extend(_briefing_event(event))
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 120] + "\n……过长已截断，其余按编号用 get 读取。"
    return text + f"\n</{tag}>"


def _briefing_event(event: dict[str, Any]) -> list[str]:
    src = event.get("source") or {}
    where = " ".join(f"{key}={src[key]}" for key in ("turnUuid", "interactionId") if src.get(key))
    label = MODEL_EVENT_LABELS.get(event["kind"], event["label"])
    head = f"- [{label}] #{event['number']} {_fmt_time(event['createdAtMs'])} {model_actor_label(event)}" + (f"（{where}）" if where else "")
    out = [head]
    data = event.get("data") or {}
    if event.get("summary"):
        out.append(f"  {event['summary'][:1500]}")
    if data.get("quote"):
        out.append(f"  原话：{data['quote'][:1500]}")
    for question in data.get("questions") or []:
        out.append(f"  问：{question.get('question', '')}")
        if question.get("options"):
            out.append("  选项：" + " | ".join(question["options"]))
        if question.get("selected"):
            out.append("  用户选：" + " | ".join(question["selected"]))
        if question.get("text"):
            out.append(f"  用户补充：{question['text']}")
    if data.get("old") or data.get("new"):
        out.append(f"  原来：{data.get('old', '')}\n  改为：{data.get('new', '')}" + (f"\n  原因：{data['why']}" if data.get("why") else ""))
    if event["kind"] == "user_edit" and data.get("after"):
        out.append("  改成：" + _dumps(data["after"])[:600])
    return out


def runtime_metadata(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    meta = message.get(RUNTIME_KEY)
    if isinstance(meta, dict) and meta.get("kind") in RUNTIME_KINDS:
        return meta
    return None


def is_work_items_runtime_message(message: Any) -> bool:
    return runtime_metadata(message) is not None


def without_work_items_runtime_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if not is_work_items_runtime_message(message)]


class WorkItemsRuntime:
    """Keeps the outline and the focus briefing present in one controller run's requests.

    The outline is appended when the run starts (if it differs from the last one
    the model saw), when the user edits items in the panel, and when the focus
    changes. The model's own changes are not re-announced inside the same run:
    it saw them in the tool results. A window rotation drops older states; the
    next request carries a fresh outline and, if a focus is set, its briefing.
    """

    def __init__(self, dao: WorkItemsDAO, conversation_uuid: str, root_turn_uuid: str) -> None:
        self.dao = dao
        self.conversation_uuid = conversation_uuid
        self.root_turn_uuid = root_turn_uuid
        self._first = True
        self._marked = False
        self.finish_checks = 0

    async def outline(self) -> tuple[str, dict[str, Any] | None, int]:
        tree = await self.dao.tree(self.conversation_uuid)
        focus = await self.dao.focus(self.conversation_uuid, self.root_turn_uuid)
        if focus and not focus["basis"].get("createdAtMs"):
            focus["basis"]["createdAtMs"] = focus["createdAtMs"]
        recent = await self.dao.recent_user_edits(self.conversation_uuid)
        recent = [event for event in recent if now_ms() - event["createdAtMs"] < LIMITS.recent_edit_days * 86400_000]
        return render_outline(tree, focus=focus, recent=recent), focus, await self.dao.last_user_edit_id(self.conversation_uuid)

    async def briefing(self, focus: dict[str, Any], **render: Any) -> str:
        nodes, events, ancestors = [], {}, {}
        for number in focus["numbers"]:
            try:
                detail = await self.dao.detail(self.conversation_uuid, number)
            except WorkItemNotFound:
                continue
            nodes.append(detail)
            ancestors[number] = detail["ancestors"]
            for ancestor in detail["ancestors"]:
                row = await self.dao.get_row(self.conversation_uuid, ancestor["number"])
                events.setdefault(row["number"], await self.dao.events(self.conversation_uuid, row["itemUuid"]))
            for node in _walk([detail]):
                events.setdefault(node["number"], await self.dao.events(self.conversation_uuid, node["itemUuid"]))
        return render_briefing(nodes, events, ancestors, focus, **render) if nodes else ""

    async def reconcile(self, messages: list[dict[str, Any]], *, epoch: int) -> list[dict[str, Any]]:
        out = list(messages)
        try:
            text, focus, user_seq = await self.outline()
        except Exception:
            log.exception("work_items.outline_failed", conversation_uuid=self.conversation_uuid)
            return out
        focus_key = _dumps(focus["numbers"]) if focus else ""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        latest = next((meta for message in reversed(out) if (meta := runtime_metadata(message)) is not None
                       and meta.get("kind") == OUTLINE_KIND and int(meta.get("epoch") or 0) == epoch), None)
        first, self._first = self._first, False
        if text or latest is not None:
            if latest is None:
                append = bool(text)
            elif first:
                append = latest.get("digest") != digest
            else:
                append = latest.get("userSeq") != user_seq or latest.get("focusKey") != focus_key
            if append:
                out.append({"role": "user", "content": text or "<work-items empty=\"true\" />",
                            RUNTIME_KEY: {"kind": OUTLINE_KIND, "epoch": epoch, "digest": digest,
                                          "userSeq": user_seq, "focusKey": focus_key}})
        if focus:
            briefed = any((meta := runtime_metadata(message)) is not None and meta.get("kind") == BRIEFING_KIND
                          and int(meta.get("epoch") or 0) == epoch and meta.get("focusKey") == focus_key
                          and meta.get("rootTurnUuid") == self.root_turn_uuid for message in out)
            if not briefed:
                briefing = await self.briefing(focus)
                if briefing:
                    out.append({"role": "user", "content": briefing,
                                RUNTIME_KEY: {"kind": BRIEFING_KIND, "epoch": epoch, "focusKey": focus_key,
                                              "rootTurnUuid": self.root_turn_uuid}})
        return out

    async def finish_check(self) -> dict[str, Any] | None:
        """None when the turn may end; otherwise the reminder to send back.

        After FINISH_CHECK_LIMIT reminders in one run the turn ends anyway and
        each item still open is marked in its history for the user to see.
        """
        open_items = await self.dao.unfinished(self.conversation_uuid, self.root_turn_uuid)
        if not open_items:
            return None
        if self.finish_checks >= FINISH_CHECK_LIMIT:
            if self._marked:
                return None
            self._marked = True
            actor = Actor("system", turn_uuid=self.root_turn_uuid)
            for item in open_items:
                with_close = "任务本身没有收尾" if item.get("closeTask") else f"仍是{STATUS_LABELS[item['status']]}"
                try:
                    await self.dao.record(self.conversation_uuid, item["number"], "finish_check", actor,
                                          summary=f"这一轮结束时{with_close}，也没有说明原因")
                except WorkItemError:
                    continue
            return None
        self.finish_checks += 1
        checks = self.finish_checks
        lines = []
        for item in open_items[:30]:
            if item.get("closeTask"):
                lines.append(f"- #{item['number']} {item['title']}：子项都已结束，任务本身还没收尾（complete）")
            else:
                lines.append(f"- #{item['number']} {item['title']} [{STATUS_LABELS[item['status']]}]")
        return {
            "items": [item["number"] for item in open_items],
            "message": (
                "<work-items-finish-check internal=\"true\">\n"
                "这一轮的执行范围里还有没收尾的项：\n" + "\n".join(lines) + "\n"
                "继续把它们做完；做不下去的逐项用 WorkItem block 写明原因，需要用户定的用 ask 写清要用户定什么。"
                "只在回复里解释不算，框架看的是任务状态。"
                + (f"这是第 {checks} 次提醒，再结束时将直接结束，并把这些项标给用户看。" if checks >= FINISH_CHECK_LIMIT else "")
                + "\n</work-items-finish-check>"
            ),
        }


def bind_work_items_finish(ctx: Any, runtime: WorkItemsRuntime, *, on_status: Any = None) -> None:
    """Chain the end-of-turn check after any finish policy already bound to the run."""
    previous = ctx.finish_policy

    async def finish(text: str) -> dict[str, Any]:
        decision = await previous(text) if previous else {"action": "close"}
        if decision.get("action") in {"repair", "continue"}:
            return decision
        try:
            reminder = await runtime.finish_check()
        except Exception:
            log.exception("work_items.finish_check_failed", conversation_uuid=runtime.conversation_uuid)
            return decision
        if reminder is None:
            return decision
        if on_status is not None:
            await on_status("任务还有没收尾的项，继续处理 …")
        return {"action": "continue", "message": reminder["message"]}

    ctx.finish_policy = finish


def reset_work_items_runtime_epoch(messages: list[dict[str, Any]], *, current_epoch: int = 0) -> int:
    epochs = [int((runtime_metadata(message) or {}).get("epoch") or 0) for message in messages if runtime_metadata(message)]
    messages[:] = without_work_items_runtime_messages(messages)
    return max([current_epoch, *epochs]) + 1


def work_items_runtime_epoch(messages: list[dict[str, Any]]) -> int:
    return max([0, *(int((runtime_metadata(message) or {}).get("epoch") or 0) for message in messages if runtime_metadata(message))])


def changed_event(conversation_uuid: str, numbers: list[str], action: str) -> dict[str, Any]:
    return {"type": "work_items.changed", "conversationUuid": conversation_uuid,
            "numbers": [str(number) for number in numbers][:100], "action": str(action)[:40]}
