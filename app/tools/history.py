"""History tool and CLI helpers for visible Web conversation transcripts.

The first version is intentionally read-only and dependency-light.  It reads the
recoverable Web Operation v2 projection (`web_operations` + `web_conversations`)
instead of raw model/tool logs, so callers get the same user/assistant text that
is visible in the Web UI by default.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.tools.base import ToolRegistry, current_tool_context

CN_TZ = timezone(timedelta(hours=8))
VISIBLE_OP_TYPES = {"user_message", "assistant_message"}
DEFAULT_MAX_CHARS = 20_000
HARD_MAX_CHARS = 60_000


@dataclass(slots=True)
class ConversationInfo:
    conversation_uuid: str
    owner_chat_id: int = 0
    internal_chat_id: int = 0
    title: str = ""
    model: str = ""
    status: str = ""
    created_at: int = 0
    updated_at: int = 0
    archived_at: int = 0


@dataclass(slots=True)
class HistoryItem:
    role: str
    text: str
    op_type: str
    op_id: str
    turn_uuid: str
    display_seq: int
    created_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass(slots=True)
class HistoryTurn:
    turn_uuid: str
    items: list[HistoryItem] = field(default_factory=list)

    @property
    def first_seq(self) -> int:
        return min((item.display_seq for item in self.items), default=0)

    @property
    def last_seq(self) -> int:
        return max((item.display_seq for item in self.items), default=0)


def _connect_ro(db_path: str) -> sqlite3.Connection:
    path = str(Path(db_path).expanduser())
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _int_arg(args: dict[str, Any], *names: str, default: int = 0) -> int:
    for name in names:
        if name in args and args.get(name) not in (None, ""):
            try:
                return int(args.get(name) or 0)
            except (TypeError, ValueError):
                return default
    return default


def _bool_arg(args: dict[str, Any], name: str, default: bool = False) -> bool:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _max_chars(args: dict[str, Any]) -> int:
    raw = _int_arg(args, "maxChars", "max_chars", default=DEFAULT_MAX_CHARS)
    if raw <= 0:
        raw = DEFAULT_MAX_CHARS
    return max(1_000, min(raw, HARD_MAX_CHARS))


def _fmt_ts(seconds: int) -> str:
    if not seconds:
        return ""
    return datetime.fromtimestamp(seconds, CN_TZ).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def _fmt_ms(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, CN_TZ).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def _truncate_middle(text: str, max_chars: int) -> tuple[str, bool]:
    text = str(text or "")
    if len(text) <= max_chars:
        return text, False
    if max_chars < 80:
        return text[:max_chars], True
    head = max_chars // 2
    tail = max_chars - head - 36
    return text[:head].rstrip() + "\n…[history output truncated]…\n" + text[-tail:].lstrip(), True


def _resolve_conversation(con: sqlite3.Connection, conversation_uuid: str) -> ConversationInfo | None:
    row = con.execute(
        """
        SELECT conversation_uuid, owner_chat_id, internal_chat_id, title, model,
               status, created_at, updated_at, archived_at
        FROM web_conversations
        WHERE conversation_uuid=?
        LIMIT 1
        """,
        (conversation_uuid,),
    ).fetchone()
    if row is None:
        return None
    return ConversationInfo(
        conversation_uuid=str(row["conversation_uuid"] or ""),
        owner_chat_id=int(row["owner_chat_id"] or 0),
        internal_chat_id=int(row["internal_chat_id"] or 0),
        title=str(row["title"] or ""),
        model=str(row["model"] or ""),
        status=str(row["status"] or ""),
        created_at=int(row["created_at"] or 0),
        updated_at=int(row["updated_at"] or 0),
        archived_at=int(row["archived_at"] or 0),
    )


def _resolve_owner(con: sqlite3.Connection, current_conversation_uuid: str = "") -> int:
    if not current_conversation_uuid:
        return 0
    info = _resolve_conversation(con, current_conversation_uuid)
    return int(info.owner_chat_id) if info else 0


def _conversation_from_args(
    con: sqlite3.Connection,
    args: dict[str, Any],
    *,
    current_conversation_uuid: str = "",
) -> tuple[str, ConversationInfo | None, str]:
    conv_uuid = str(
        args.get("conversationUuid")
        or args.get("conversation_uuid")
        or args.get("convUuid")
        or args.get("conv_uuid")
        or ""
    ).strip()
    scope = str(args.get("scope") or "").strip().lower()
    if not conv_uuid and (scope in {"", "current"}):
        conv_uuid = current_conversation_uuid
    if not conv_uuid:
        return "", None, "error: missing conversationUuid; 当前工具上下文也没有 Web conversation UUID"
    info = _resolve_conversation(con, conv_uuid)
    if info is None:
        return conv_uuid, None, f"error: conversation not found: {conv_uuid}"
    owner = _resolve_owner(con, current_conversation_uuid)
    if owner and info.owner_chat_id and owner != info.owner_chat_id:
        return conv_uuid, None, "error: conversation belongs to a different owner"
    return conv_uuid, info, ""


def _visible_history_items(
    con: sqlite3.Connection,
    conversation_uuid: str,
    *,
    include_notices: bool = False,
) -> list[HistoryItem]:
    op_types = sorted(VISIBLE_OP_TYPES | ({"notice"} if include_notices else set()))
    placeholders = ",".join("?" for _ in op_types)
    rows = con.execute(
        f"""
        SELECT op_type, op_id, turn_uuid, display_seq, internal, payload_json,
               created_at_ms, updated_at_ms
        FROM web_operations
        WHERE conversation_uuid=?
          AND op_type IN ({placeholders})
        ORDER BY display_seq ASC, id ASC
        """,
        (conversation_uuid, *op_types),
    ).fetchall()
    items: list[HistoryItem] = []
    for row in rows:
        payload = _json_dict(row["payload_json"])
        if int(row["internal"] or 0) or payload.get("internal") or payload.get("hidden"):
            continue
        op_type = str(row["op_type"] or "")
        text = str(payload.get("text") or payload.get("summary") or "")
        if not text.strip():
            continue
        role = "notice"
        if op_type == "user_message":
            role = "user"
        elif op_type == "assistant_message":
            role = "assistant"
        items.append(
            HistoryItem(
                role=role,
                text=text,
                op_type=op_type,
                op_id=str(row["op_id"] or ""),
                turn_uuid=str(row["turn_uuid"] or ""),
                display_seq=int(row["display_seq"] or 0),
                created_at_ms=int(row["created_at_ms"] or 0),
                updated_at_ms=int(row["updated_at_ms"] or 0),
            )
        )
    return items


def _group_turns(items: list[HistoryItem]) -> list[HistoryTurn]:
    turns: list[HistoryTurn] = []
    by_turn: dict[str, HistoryTurn] = {}
    for item in items:
        key = item.turn_uuid or f"seq:{item.display_seq}"
        turn = by_turn.get(key)
        if turn is None:
            turn = HistoryTurn(turn_uuid=item.turn_uuid or key)
            by_turn[key] = turn
            turns.append(turn)
        turn.items.append(item)
    return turns


def _render_conversation_header(info: ConversationInfo, *, action: str) -> list[str]:
    lines = [f"# History {action} result", ""]
    lines.append(f"Conversation: {info.title or '(untitled)'}")
    lines.append(f"conversationUuid: {info.conversation_uuid}")
    if info.model:
        lines.append(f"model: {info.model}")
    if info.updated_at:
        lines.append(f"updatedAt: {_fmt_ts(info.updated_at)}")
    lines.append("source: web_operations visible transcript")
    return lines


def _render_turns(
    info: ConversationInfo,
    turns: list[HistoryTurn],
    *,
    total_turns: int,
    max_chars: int,
    excluded_current_turn: bool = False,
) -> str:
    lines = _render_conversation_header(info, action="read")
    lines.append(f"returnedTurns: {len(turns)} / {total_turns}")
    if excluded_current_turn:
        lines.append("currentTurnExcluded: true")
    lines.append("included: user_message, assistant_message")
    lines.append("")
    for index, turn in enumerate(turns, 1):
        lines.append(f"## Turn {index}")
        lines.append(f"turnUuid: {turn.turn_uuid}")
        lines.append(f"displaySeq: {turn.first_seq}-{turn.last_seq}")
        for item in turn.items:
            label = item.role
            if item.created_at_ms:
                lines.append(f"\n[{label}] {_fmt_ms(item.created_at_ms)}")
            else:
                lines.append(f"\n[{label}]")
            lines.append(item.text.rstrip())
        lines.append("")
    rendered = "\n".join(lines).rstrip() + "\n"
    rendered, truncated = _truncate_middle(rendered, max_chars)
    if truncated:
        rendered += f"\n\nTruncated: true; maxChars={max_chars}\n"
    else:
        rendered += "\nTruncated: false\n"
    return rendered


def history_read(
    db_path: str,
    args: dict[str, Any],
    *,
    current_conversation_uuid: str = "",
    current_turn_uuid: str = "",
) -> str:
    with _connect_ro(db_path) as con:
        conv_uuid, info, err = _conversation_from_args(con, args, current_conversation_uuid=current_conversation_uuid)
        if err:
            return err
        assert info is not None
        include_notices = _bool_arg(args, "includeNotices", False) or _bool_arg(args, "include_notices", False)
        items = _visible_history_items(con, conv_uuid, include_notices=include_notices)
        turns = _group_turns(items)
        exclude_default = bool(current_turn_uuid and conv_uuid == current_conversation_uuid)
        exclude_current = _bool_arg(args, "excludeCurrentTurn", exclude_default)
        if "exclude_current_turn" in args:
            exclude_current = _bool_arg(args, "exclude_current_turn", exclude_default)
        excluded = False
        if exclude_current and current_turn_uuid:
            before = len(turns)
            turns = [turn for turn in turns if turn.turn_uuid != current_turn_uuid]
            excluded = len(turns) != before
        total_turns = len(turns)
        direction = str(args.get("from") or args.get("position") or "end").strip().lower()
        turn_count = _int_arg(args, "turns", "lastTurns", "last_turns", "limitTurns", "limit_turns", default=5)
        turn_count = max(1, min(turn_count, 50))
        if direction in {"start", "first", "begin", "beginning"}:
            selected = turns[:turn_count]
        else:
            selected = turns[-turn_count:]
        return _render_turns(info, selected, total_turns=total_turns, max_chars=_max_chars(args), excluded_current_turn=excluded)


def _split_terms(query: str) -> list[str]:
    terms = [part.strip().lower() for part in re.split(r"\s+", query.strip()) if part.strip()]
    return terms or [query.strip().lower()]


def _snippet(text: str, terms: list[str], max_chars: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= max_chars:
        return clean
    lower = clean.lower()
    pos = -1
    for term in terms:
        pos = lower.find(term)
        if pos >= 0:
            break
    if pos < 0:
        return clean[:max_chars].rstrip() + "…"
    start = max(0, pos - max_chars // 3)
    end = min(len(clean), start + max_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(clean) else ""
    return prefix + clean[start:end].strip() + suffix


def history_search(
    db_path: str,
    args: dict[str, Any],
    *,
    current_conversation_uuid: str = "",
) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "error: missing query"
    terms = _split_terms(query)
    limit = max(1, min(_int_arg(args, "limit", default=5), 20))
    max_snippet = max(120, min(_int_arg(args, "maxSnippetChars", "max_snippet_chars", default=500), 2000))
    include_archived = _bool_arg(args, "includeArchived", False) or _bool_arg(args, "include_archived", False)
    with _connect_ro(db_path) as con:
        owner = _resolve_owner(con, current_conversation_uuid)
        first = f"%{terms[0]}%"
        params: list[Any] = [first, first]
        owner_sql = ""
        archived_sql = ""
        if owner:
            owner_sql = " AND c.owner_chat_id=?"
            params.append(owner)
        if not include_archived:
            archived_sql = " AND COALESCE(c.archived_at,0)=0"
        rows = con.execute(
            f"""
            SELECT c.conversation_uuid, c.title, c.updated_at, c.owner_chat_id,
                   o.turn_uuid, o.op_type, o.display_seq, o.payload_json, o.internal
            FROM web_operations o
            JOIN web_conversations c ON c.conversation_uuid=o.conversation_uuid
            WHERE o.op_type IN ('user_message','assistant_message')
              AND COALESCE(o.internal,0)=0
              AND (LOWER(c.title) LIKE ? OR LOWER(o.payload_json) LIKE ?)
              {owner_sql}
              {archived_sql}
            ORDER BY c.updated_at DESC, o.display_seq ASC, o.id ASC
            LIMIT 1000
            """,
            params,
        ).fetchall()
        matches: list[tuple[sqlite3.Row, str, str]] = []
        for row in rows:
            payload = _json_dict(row["payload_json"])
            if payload.get("internal") or payload.get("hidden"):
                continue
            text = str(payload.get("text") or "")
            title = str(row["title"] or "")
            haystack = f"{title}\n{text}".lower()
            if all(term in haystack for term in terms):
                role = "user" if str(row["op_type"] or "") == "user_message" else "assistant"
                matches.append((row, role, text))
                if len(matches) >= limit:
                    break
    lines = ["# History search results", "", f"Query: {query}", f"Returned: {len(matches)}", ""]
    for index, (row, role, text) in enumerate(matches, 1):
        lines.append(f"## {index}. {row['title'] or '(untitled)'}")
        lines.append(f"conversationUuid: {row['conversation_uuid']}")
        lines.append(f"updatedAt: {_fmt_ts(int(row['updated_at'] or 0))}")
        lines.append(f"turnUuid: {row['turn_uuid']}")
        lines.append(f"displaySeq: {int(row['display_seq'] or 0)}")
        lines.append(f"role: {role}")
        lines.append("")
        lines.append(_snippet(text, terms, max_snippet))
        lines.append("")
    rendered = "\n".join(lines).rstrip() + "\n"
    rendered, truncated = _truncate_middle(rendered, _max_chars(args))
    if truncated:
        rendered += f"\n\nTruncated: true; maxChars={_max_chars(args)}\n"
    return rendered


def history_list(
    db_path: str,
    args: dict[str, Any],
    *,
    current_conversation_uuid: str = "",
) -> str:
    limit = max(1, min(_int_arg(args, "limit", default=10), 50))
    include_archived = _bool_arg(args, "includeArchived", False) or _bool_arg(args, "include_archived", False)
    with _connect_ro(db_path) as con:
        owner = _resolve_owner(con, current_conversation_uuid)
        params: list[Any] = []
        where = ["1=1"]
        if owner:
            where.append("owner_chat_id=?")
            params.append(owner)
        if not include_archived:
            where.append("COALESCE(archived_at,0)=0")
        params.append(limit)
        rows = con.execute(
            f"""
            SELECT conversation_uuid, title, model, status, created_at, updated_at, archived_at
            FROM web_conversations
            WHERE {' AND '.join(where)}
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        lines = ["# History conversation list", "", f"Returned: {len(rows)}", ""]
        for index, row in enumerate(rows, 1):
            conv_uuid = str(row["conversation_uuid"] or "")
            preview_rows = con.execute(
                """
                SELECT op_type, payload_json
                FROM web_operations
                WHERE conversation_uuid=?
                  AND op_type IN ('user_message','assistant_message')
                  AND COALESCE(internal,0)=0
                ORDER BY display_seq DESC, id DESC
                LIMIT 2
                """,
                (conv_uuid,),
            ).fetchall()
            previews: list[str] = []
            for p_row in reversed(preview_rows):
                payload = _json_dict(p_row["payload_json"])
                if payload.get("internal") or payload.get("hidden"):
                    continue
                role = "user" if str(p_row["op_type"] or "") == "user_message" else "assistant"
                text = _snippet(str(payload.get("text") or ""), [], 180)
                if text:
                    previews.append(f"[{role}] {text}")
            lines.append(f"## {index}. {row['title'] or '(untitled)'}")
            lines.append(f"conversationUuid: {conv_uuid}")
            lines.append(f"updatedAt: {_fmt_ts(int(row['updated_at'] or 0))}")
            if row["model"]:
                lines.append(f"model: {row['model']}")
            if previews:
                lines.append("preview:")
                lines.extend(f"- {item}" for item in previews)
            lines.append("")
    rendered = "\n".join(lines).rstrip() + "\n"
    rendered, truncated = _truncate_middle(rendered, _max_chars(args))
    if truncated:
        rendered += f"\n\nTruncated: true; maxChars={_max_chars(args)}\n"
    return rendered


def history_read_turn(
    db_path: str,
    args: dict[str, Any],
    *,
    current_conversation_uuid: str = "",
) -> str:
    turn_uuid = str(args.get("turnUuid") or args.get("turn_uuid") or "").strip()
    if not turn_uuid:
        return "error: missing turnUuid"
    before = max(0, min(_int_arg(args, "before", default=2), 20))
    after = max(0, min(_int_arg(args, "after", default=2), 20))
    with _connect_ro(db_path) as con:
        conv_uuid, info, err = _conversation_from_args(con, args, current_conversation_uuid=current_conversation_uuid)
        if err:
            return err
        assert info is not None
        items = _visible_history_items(con, conv_uuid)
        turns = _group_turns(items)
        idx = next((i for i, turn in enumerate(turns) if turn.turn_uuid == turn_uuid), -1)
        if idx < 0:
            return f"error: turn not found: {turn_uuid}"
        start = max(0, idx - before)
        end = min(len(turns), idx + after + 1)
        selected = turns[start:end]
        rendered = _render_turns(info, selected, total_turns=len(turns), max_chars=_max_chars(args))
        return rendered


def run_history_action(
    db_path: str,
    args: dict[str, Any],
    *,
    current_conversation_uuid: str = "",
    current_turn_uuid: str = "",
) -> str:
    action = str(args.get("action") or "read").strip().lower()
    if action == "read":
        return history_read(
            db_path,
            args,
            current_conversation_uuid=current_conversation_uuid,
            current_turn_uuid=current_turn_uuid,
        )
    if action == "search":
        return history_search(db_path, args, current_conversation_uuid=current_conversation_uuid)
    if action == "list":
        return history_list(db_path, args, current_conversation_uuid=current_conversation_uuid)
    if action in {"read_turn", "read-turn", "turn"}:
        return history_read_turn(db_path, args, current_conversation_uuid=current_conversation_uuid)
    return f"error: unknown History action: {action}"


def register_history_tools(reg: ToolRegistry, db: Any) -> None:
    async def _history(args: dict[str, Any]) -> str:
        ctx = current_tool_context()
        return run_history_action(
            db.path,
            args,
            current_conversation_uuid=ctx.conversation_uuid,
            current_turn_uuid=ctx.turn_uuid,
        )

    reg.add(
        "History",
        "Read/search OpenBear Web conversation history as visible transcript. Use when the user asks 看之前/上一轮/上次/接着之前/还有工作没做完/查看某会话结论. Read-only; defaults to user+assistant visible text from web_operations and excludes tools, reasoning, stats, raw events.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "search", "list", "read_turn"], "description": "History action"},
                "scope": {"type": "string", "description": "current or explicit conversation"},
                "conversationUuid": {"type": "string", "description": "Web conversation UUID; optional for scope=current"},
                "turnUuid": {"type": "string", "description": "Turn UUID for read_turn"},
                "query": {"type": "string", "description": "Search query for action=search"},
                "from": {"type": "string", "description": "read position: start or end"},
                "turns": {"type": "integer", "description": "Number of turns to read"},
                "lastTurns": {"type": "integer", "description": "Alias for turns when reading from end"},
                "before": {"type": "integer", "description": "Turns before target for read_turn"},
                "after": {"type": "integer", "description": "Turns after target for read_turn"},
                "limit": {"type": "integer", "description": "Result limit for list/search"},
                "maxChars": {"type": "integer", "description": "Maximum output chars, default 20000, hard cap 60000"},
                "maxSnippetChars": {"type": "integer", "description": "Search snippet size"},
                "excludeCurrentTurn": {"type": "boolean", "description": "For current conversation reads, exclude the active user turn by default"},
                "includeNotices": {"type": "boolean", "description": "Include visible notice operations; default false"},
                "includeArchived": {"type": "boolean", "description": "Include archived conversations for list/search; default false"},
            },
            "required": ["action"],
        },
        _history,
        visibility={"main", "runtime"},
    )
