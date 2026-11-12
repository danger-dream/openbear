"""History tool and CLI helpers for visible Web conversation transcripts.

The first version is intentionally read-only and dependency-light.  It reads the
recoverable Web Operation v2 projection (`web_operations` + `web_conversations`)
instead of raw model/tool logs, so callers get the same user/assistant text that
is visible in the Web UI by default.
"""
from __future__ import annotations

import asyncio
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


SCOPE_WORDS = ("current", "others", "all")


def resolve_scope(args: dict[str, Any], default: str) -> tuple[str, str]:
    """Return (kind, conversationUuid): kind is current, others, all or conversation.

    An explicit conversationUuid always wins; any scope value that is not a
    scope word is taken as a conversation UUID.
    """
    conv_uuid = str(
        args.get("conversationUuid")
        or args.get("conversation_uuid")
        or args.get("convUuid")
        or args.get("conv_uuid")
        or ""
    ).strip()
    if conv_uuid:
        return "conversation", conv_uuid
    raw = str(args.get("scope") or "").strip()
    scope = raw.lower() or default
    if scope in SCOPE_WORDS:
        return scope, ""
    if scope in {"conversation", "explicit"}:
        return "conversation", ""
    return "conversation", raw


def _conversation_from_args(
    con: sqlite3.Connection,
    args: dict[str, Any],
    *,
    current_conversation_uuid: str = "",
) -> tuple[str, ConversationInfo | None, str]:
    kind, conv_uuid = resolve_scope(args, "current")
    if kind in {"others", "all"}:
        return "", None, f"error: scope={kind} is only for search; this action reads one conversation (scope=current or a conversationUuid)"
    if kind == "current":
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
    op_id: str | None = None,
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
          {"AND op_id=?" if op_id is not None else ""}
        ORDER BY display_seq ASC, id ASC
        """,
        (conversation_uuid, *op_types, *((op_id,) if op_id is not None else ())),
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
    offset_turns: int = 0,
    offset_from: str = "",
) -> str:
    lines = _render_conversation_header(info, action="read")
    lines.append(f"returnedTurns: {len(turns)} / {total_turns}")
    if offset_turns > 0:
        lines.append(f"offsetTurns: {offset_turns} (from {offset_from or 'end'})")
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
        if "opId" in args:
            op_id = args["opId"]
            if not isinstance(op_id, str) or not op_id.strip():
                return "error: invalid opId"
            # An explicit message locator is not a window over nearby turns.
            # Filter in SQL and retain the same owner/visibility boundary, even
            # when the selected message belongs to the active turn.
            items = _visible_history_items(con, conv_uuid, op_id=op_id)
            if not items:
                return f"error: message not found: {op_id}"
            return _render_turns(info, _group_turns(items), total_turns=1, max_chars=_max_chars(args))
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
        offset = max(0, _int_arg(args, "offset", default=0))
        from_start = direction in {"start", "first", "begin", "beginning"}
        if from_start:
            selected = turns[offset:offset + turn_count]
        else:
            end_index = max(0, len(turns) - offset)
            selected = turns[max(0, end_index - turn_count):end_index]
        return _render_turns(
            info,
            selected,
            total_turns=total_turns,
            max_chars=_max_chars(args),
            excluded_current_turn=excluded,
            offset_turns=offset,
            offset_from="start" if from_start else "end",
        )


def _split_terms(query: str) -> list[str]:
    """Space-separated words; a double-quoted phrase stays one term."""
    terms: list[str] = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', query.strip()):
        term = " ".join((phrase or word).split()).lower()
        if term and term not in terms:
            terms.append(term)
    return terms or [" ".join(query.split()).lower()]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def matched_terms(haystack: str, terms: list[str]) -> list[str]:
    folded = " ".join(str(haystack or "").split()).lower()
    return [term for term in terms if term in folded]


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


def scope_label(kind: str, conversation_uuid: str = "", *, has_current: bool = True) -> str:
    if kind == "current":
        return "current conversation"
    if kind == "others":
        return "other conversations (current conversation excluded)"
    if kind == "all":
        return "all conversations (current + others)" if has_current else "all conversations"
    return f"conversation {conversation_uuid}"


# Candidate rows ranked in SQL before the exact per-message check.
_SEARCH_CANDIDATES = 2000


def history_search(
    db_path: str,
    args: dict[str, Any],
    *,
    current_conversation_uuid: str = "",
    current_turn_uuid: str = "",
    current_root_turn_uuid: str = "",
) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "error: missing query"
    terms = _split_terms(query)
    limit = max(1, min(_int_arg(args, "limit", default=5), 20))
    max_snippet = max(120, min(_int_arg(args, "maxSnippetChars", "max_snippet_chars", default=500), 2000))
    include_archived = _bool_arg(args, "includeArchived", False) or _bool_arg(args, "include_archived", False)
    exclude_current = _bool_arg(args, "excludeCurrentTurn", True)
    if "exclude_current_turn" in args:
        exclude_current = _bool_arg(args, "exclude_current_turn", True)
    kind, conv_filter = resolve_scope(args, "all")
    if kind == "current":
        if not current_conversation_uuid:
            return "error: scope=current needs a current Web conversation; pass conversationUuid"
        conv_filter = current_conversation_uuid
    if kind == "others" and not current_conversation_uuid:
        kind = "all"
    if kind == "conversation" and not conv_filter:
        return "error: missing conversationUuid"
    current_turns = [turn for turn in dict.fromkeys((current_turn_uuid, current_root_turn_uuid)) if turn]
    with _connect_ro(db_path) as con:
        owner = _resolve_owner(con, current_conversation_uuid)
        if conv_filter:
            conv_info = _resolve_conversation(con, conv_filter)
            if conv_info is None:
                return f"error: conversation not found: {conv_filter}"
            if owner and conv_info.owner_chat_id and owner != conv_info.owner_chat_id:
                return "error: conversation belongs to a different owner"
        score_parts: list[str] = []
        params: list[Any] = []
        for term in terms:
            # payload_json may hold non-ASCII text as \uXXXX escapes; each case of it, as SQLite folds ASCII alone.
            cases = _case_forms(term)
            forms = list(dict.fromkeys(form for case in cases for form in (case, json.dumps(case)[1:-1])))
            tests = ["LOWER(COALESCE(c.title,'')) LIKE ? ESCAPE '\\'" for _ in cases] + ["LOWER(o.payload_json) LIKE ? ESCAPE '\\'" for _ in forms]
            score_parts.append("(" + " OR ".join(tests) + ")")
            params += [_like(case) for case in cases] + [_like(form) for form in forms]
        where = ["o.op_type IN ('user_message','assistant_message')", "COALESCE(o.internal,0)=0"]
        if conv_filter:
            where.append("o.conversation_uuid=?")
            params.append(conv_filter)
        elif kind == "others":
            where.append("o.conversation_uuid<>?")
            params.append(current_conversation_uuid)
        if owner:
            where.append("c.owner_chat_id=?")
            params.append(owner)
        if not include_archived:
            where.append("COALESCE(c.archived_at,0)=0")
        excluded_turn = bool(exclude_current and current_conversation_uuid and current_turns and kind != "others"
                             and (not conv_filter or conv_filter == current_conversation_uuid))
        if excluded_turn:
            marks = ",".join("?" for _ in current_turns)
            where.append(f"NOT (o.conversation_uuid=? AND (o.turn_uuid IN ({marks}) OR COALESCE(o.run_root_turn_uuid,'') IN ({marks})))")
            params += [current_conversation_uuid, *current_turns, *current_turns]
        rows = con.execute(
            f"""
            SELECT * FROM (
              SELECT c.conversation_uuid, c.title, c.updated_at, o.id, o.turn_uuid, o.op_type, o.display_seq,
                     o.payload_json, o.created_at_ms, ({' + '.join(score_parts)}) AS score
              FROM web_operations o
              JOIN web_conversations c ON c.conversation_uuid=o.conversation_uuid
              WHERE {' AND '.join(where)}
            ) WHERE score>0
            ORDER BY score DESC, created_at_ms DESC, id DESC
            LIMIT {_SEARCH_CANDIDATES}
            """,
            params,
        ).fetchall()
    hits: list[tuple[int, sqlite3.Row, str, str, list[str]]] = []
    for row in rows:
        payload = _json_dict(row["payload_json"])
        if payload.get("internal") or payload.get("hidden"):
            continue
        text = str(payload.get("text") or "")
        found = matched_terms(f"{row['title'] or ''}\n{text}", terms)
        if not found:
            continue
        role = "user" if str(row["op_type"] or "") == "user_message" else "assistant"
        hits.append((len(found), row, role, text, found))
    hits.sort(key=lambda hit: (-hit[0], -int(hit[1]["created_at_ms"] or 0), -int(hit[1]["id"] or 0)))
    hits = hits[:limit]
    full = sum(1 for hit in hits if hit[0] == len(terms))
    groups: dict[str, list[tuple[int, sqlite3.Row, str, str, list[str]]]] = {}
    for hit in hits:
        groups.setdefault(str(hit[1]["conversation_uuid"]), []).append(hit)
    lines = ["# History search results", "", f"Query: {query}"]
    if len(terms) > 1:
        lines.append(f"Terms: {', '.join(terms)}")
    lines.append(f"Scope: {scope_label(kind, conv_filter, has_current=bool(current_conversation_uuid))}; "
                 f"archived {'included' if include_archived else 'excluded'}")
    if excluded_turn:
        lines.append("currentTurnExcluded: true")
    lines.append(f"Returned: {len(hits)} (all terms: {full}, partial: {len(hits) - full}) in {len(groups)} conversation(s)")
    if hits and not full and len(terms) > 1:
        lines.append(f"Note: no message contains all {len(terms)} terms; these are the closest partial matches.")
    lines.append("Ranking: messages matching more terms first, then newest first.")
    lines.append("")
    index = 0
    for conv_uuid, group in groups.items():
        first = group[0][1]
        where_label = "Current conversation" if conv_uuid == current_conversation_uuid else "Other conversation"
        lines.append(f"## {where_label}: {first['title'] or '(untitled)'}")
        lines.append(f"conversationUuid: {conv_uuid}")
        lines.append(f"updatedAt: {_fmt_ts(int(first['updated_at'] or 0))}")
        lines.append("")
        for score, row, role, text, found in group:
            index += 1
            lines.append(f"### {index}. {role} {_fmt_ms(int(row['created_at_ms'] or 0))}".rstrip())
            lines.append(f"turnUuid: {row['turn_uuid']}")
            lines.append(f"displaySeq: {int(row['display_seq'] or 0)}")
            lines.append(f"role: {role}")
            lines.append(f"matchedTerms: {', '.join(found)} ({score}/{len(terms)})")
            lines.append("")
            lines.append(_snippet(text, found, max_snippet))
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
    current_root_turn_uuid: str = "",
) -> str:
    action = str(args.get("action") or "read").strip().lower()
    if "opId" in args and action != "read":
        return "error: opId requires action=read"
    if action == "read":
        return history_read(
            db_path,
            args,
            current_conversation_uuid=current_conversation_uuid,
            current_turn_uuid=current_turn_uuid,
        )
    if action == "search":
        return history_search(
            db_path,
            args,
            current_conversation_uuid=current_conversation_uuid,
            current_turn_uuid=current_turn_uuid,
            current_root_turn_uuid=current_root_turn_uuid,
        )
    if action == "list":
        return history_list(db_path, args, current_conversation_uuid=current_conversation_uuid)
    if action in {"read_turn", "read-turn", "turn"}:
        return history_read_turn(db_path, args, current_conversation_uuid=current_conversation_uuid)
    return f"error: unknown History action: {action}"


DEFAULT_EXECUTION_SEARCH_DAYS = 30
_TIME_UNITS = {"d": 86400, "h": 3600, "m": 60}


def parse_time_arg(value: Any, name: str) -> int | None:
    """Unix seconds from seconds/ms, a relative age (7d, 12h, 90m) or a UTC+8 date/time."""
    if value in (None, "") or value == 0:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid_{name}")
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        number = float(text)
        return int(number / 1000 if number > 1e11 else number)
    relative = re.fullmatch(r"(\d+)\s*([dhm])", text.lower())
    if relative:
        return int(datetime.now(CN_TZ).timestamp()) - int(relative.group(1)) * _TIME_UNITS[relative.group(2)]
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace("/", "-"))
    except ValueError as exc:
        raise ValueError(f"invalid_{name}: use YYYY-MM-DD[ HH:MM] (UTC+8), ISO time, unix seconds or 7d/12h") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CN_TZ)
    seconds = int(moment.timestamp())
    if name == "until" and re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text.replace("/", "-")):
        seconds += 86400
    return seconds


def _json_variants(term: str) -> tuple[list[str], list[str]]:
    """How a term can appear in stored content / tool_calls_json.

    Tool results may be JSON with escaped quotes or \\uXXXX; call arguments are a
    JSON string inside tool_calls_json, so they are escaped once more.
    """
    once = [json.dumps(term, ensure_ascii=False)[1:-1], json.dumps(term)[1:-1]]
    content = list(dict.fromkeys([term, *once]))
    twice = [json.dumps(item, ensure_ascii=False)[1:-1] for item in once]
    return content, list(dict.fromkeys([*content, *twice]))


def _first_id_since(con: sqlite3.Connection, since: int) -> int:
    """Lowest id of a message at or after `since`, so the scan starts there.

    Ids are not in time order: a copied conversation's messages keep their
    old times under new ids.
    """
    # +id: plain MIN(id) walks the table in id order instead of using idx_messages_created.
    row = con.execute("SELECT MIN(+id) FROM messages WHERE created_at>=?", (since,)).fetchone()
    if row[0] is not None:
        return int(row[0])
    return int(con.execute("SELECT COALESCE(MAX(id),0)+1 FROM messages").fetchone()[0])


def _case_forms(term: str) -> list[str]:
    """SQLite's LIKE folds only ASCII letters: a lower-case term in another script also goes upper and capitalised."""
    if term.isascii():
        return [term]
    return list(dict.fromkeys([term, term.upper(), term.capitalize(), term.title()]))


def _call_text(tool_calls_json: str | None) -> tuple[str, list[str]]:
    try:
        calls = json.loads(tool_calls_json or "[]")
    except (TypeError, ValueError):
        return str(tool_calls_json or ""), []
    parts, names = [], []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "")
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        names.append(name)
        parts.append(f"{name} {arguments}".strip())
    return "\n".join(parts), names


def search_execution_records(
    db_path: str,
    terms: list[str],
    *,
    owner_chat_id: int,
    chat_id: int = 0,
    exclude_chat_id: int = 0,
    current_chat_id: int = 0,
    include_archived: bool = False,
    root_turn_uuid: str = "",
    exclude_turn_uuid: str = "",
    since: int | None = None,
    until: int | None = None,
    after: int = 0,
    high_water: int | None = None,
    limit: int = 20,
    newest_first: bool = True,
    max_snippet: int = 300,
) -> dict[str, Any]:
    """Ranked search over Controller execution messages of one conversation or one owner's conversations."""
    with _connect_ro(db_path) as con:
        bound = int(con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0])
        if high_water is not None:
            if high_water < 0 or high_water > bound:
                raise ValueError("invalid_history_high_water")
            bound = high_water
        where = ["m.id>?", "m.id<=?"]
        params: list[Any] = [after, bound]
        if chat_id:
            where.append("m.chat_id=?")
            params.append(chat_id)
            conversations = 1
        else:
            owned = ["owner_chat_id=?"]
            owned_params: list[Any] = [owner_chat_id]
            if exclude_chat_id:
                owned.append("internal_chat_id<>?")
                owned_params.append(exclude_chat_id)
            if not include_archived:
                owned.append("COALESCE(archived_at,0)=0")
            owned_sql = " AND ".join(owned)
            where.append(f"m.chat_id IN (SELECT internal_chat_id FROM web_conversations WHERE {owned_sql})")
            params += owned_params
            conversations = int(con.execute(f"SELECT COUNT(*) FROM web_conversations WHERE {owned_sql}", owned_params).fetchone()[0])
        if since is not None:
            where.append("m.id>=?")
            params.append(_first_id_since(con, since))
            where.append("m.created_at>=?")
            params.append(since)
        if until is not None:
            where.append("m.created_at<?")
            params.append(until)
        if root_turn_uuid:
            where.append("(m.run_root_turn_uuid=? OR m.turn_uuid=?)")
            params += [root_turn_uuid, root_turn_uuid]
        if exclude_turn_uuid and current_chat_id:
            where.append("NOT (m.chat_id=? AND (m.run_root_turn_uuid=? OR m.turn_uuid=?))")
            params += [current_chat_id, exclude_turn_uuid, exclude_turn_uuid]
        score_parts: list[str] = []
        score_params: list[Any] = []
        variants: dict[str, list[str]] = {}
        for term in terms:
            content_forms, call_forms = [], []
            for form in _case_forms(term):
                content, calls = _json_variants(form)
                content_forms += [f for f in content if f not in content_forms]
                call_forms += [f for f in calls if f not in call_forms]
            variants[term] = call_forms
            # NULL columns (a tool row has no tool_calls_json) must not turn the whole score NULL.
            tests = ["COALESCE(m.content,'') LIKE ? ESCAPE '\\'" for _ in content_forms]
            tests += ["COALESCE(m.tool_calls_json,'') LIKE ? ESCAPE '\\'" for _ in call_forms]
            tests.append("COALESCE(m.name,'') LIKE ? ESCAPE '\\'")
            score_parts.append("(" + " OR ".join(tests) + ")")
            score_params += [_like(form) for form in content_forms] + [_like(form) for form in call_forms] + [_like(term)]
        direction = "DESC" if newest_first else "ASC"
        rows = con.execute(
            f"""
            SELECT * FROM (
              SELECT m.id, m.chat_id, m.role, m.name, m.turn_uuid, m.run_root_turn_uuid, m.created_at,
                     length(m.content) AS stored_chars, ({' + '.join(score_parts)}) AS score
              FROM messages m WHERE {' AND '.join(where)}
            ) WHERE score>0 ORDER BY score DESC, created_at {direction}, id {direction} LIMIT ?
            """,
            (*score_params, *params, limit + 1),
        ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        ids = [int(row["id"]) for row in rows]
        marks = ",".join("?" for _ in ids)
        bodies = {int(row["id"]): row for row in con.execute(
            f"SELECT id, content, tool_calls_json FROM messages WHERE id IN ({marks})", ids)} if ids else {}
        chats = sorted({int(row["chat_id"]) for row in rows})
        chat_marks = ",".join("?" for _ in chats)
        titles = {int(row["internal_chat_id"]): row for row in con.execute(
            f"SELECT internal_chat_id, conversation_uuid, title FROM web_conversations WHERE internal_chat_id IN ({chat_marks})",
            chats)} if chats else {}
    events: list[dict[str, Any]] = []
    for row in rows:
        body = bodies.get(int(row["id"]))
        content = str(body["content"] or "") if body else ""
        raw_calls = str(body["tool_calls_json"] or "") if body else ""
        call_text, call_names = _call_text(raw_calls)
        display = "\n".join(part for part in (content, call_text) if part)
        folded = f"{content}\n{raw_calls}\n{row['name'] or ''}".lower()
        found = [term for term in terms
                 if term in " ".join(display.split()).lower() or any(form in folded for form in variants[term])]
        conversation = titles.get(int(row["chat_id"]))
        tool_name = str(row["name"] or "") or ",".join(dict.fromkeys(name for name in call_names if name))
        events.append({
            "event_id": f"message:{row['id']}", "seq": int(row["id"]), "kind": row["role"], "role": row["role"],
            "toolName": tool_name, "conversationUuid": str(conversation["conversation_uuid"]) if conversation else "",
            "conversationTitle": str(conversation["title"] or "") if conversation else "",
            "currentConversation": bool(current_chat_id and int(row["chat_id"]) == current_chat_id),
            "turn_uuid": row["turn_uuid"], "run_root_turn_uuid": row["run_root_turn_uuid"],
            "created_at": row["created_at"], "time": _fmt_ts(int(row["created_at"] or 0)),
            "stored_chars": row["stored_chars"], "matchedTerms": found,
            "snippet": _snippet(display, found, max_snippet),
        })
    full = sum(1 for event in events if len(event["matchedTerms"]) == len(terms))
    out = {"events": events, "highWater": bound, "hasMore": more,
           "conversations": conversations, "allTermsMatched": full}
    if more:
        out["more"] = "search has no next page: raise limit (max 100), or narrow it with more terms, since/until or scope"
    return out


async def _execution_search(db: Any, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "missing_query"}
    terms = _split_terms(query)
    current_task = _bool_arg(args, "currentTask", False)
    kind, _ = resolve_scope(args, "current" if current_task else "all")
    include_archived = _bool_arg(args, "includeArchived", False)
    with _connect_ro(db.path) as con:
        current = _resolve_conversation(con, ctx.conversation_uuid) if ctx.conversation_uuid else None
        info = None
        if kind in {"current", "conversation"}:
            _, info, error = _conversation_from_args(con, args, current_conversation_uuid=ctx.conversation_uuid)
            if error or info is None:
                return {"ok": False, "error": error or "conversation_not_found"}
        elif current is None:
            return {"ok": False, "error": f"scope={kind} needs the current Web conversation"}
    current_chat = current.internal_chat_id if current else 0
    current_root = ctx.run_root_turn_uuid or ctx.turn_uuid
    root = str(args.get("rootTurnUuid") or "")
    if current_task:
        if info is None or info.conversation_uuid != ctx.conversation_uuid or not current_root:
            return {"ok": False, "error": "current_task_requires_current_conversation"}
        root = current_root
    exclude = _bool_arg(args, "excludeCurrentTurn", kind == "all" and not root)
    if exclude and root and root == current_root:
        return {"ok": False, "error": "current_task_cannot_exclude_itself"}
    in_scope = kind == "all" or (info is not None and info.conversation_uuid == ctx.conversation_uuid)
    excluded = bool(exclude and current_root and current_chat and in_scope)
    try:
        since = parse_time_arg(args.get("since"), "since")
        until = parse_time_arg(args.get("until"), "until")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if since is None and kind in {"all", "others"}:
        since = int(datetime.now(CN_TZ).timestamp()) - DEFAULT_EXECUTION_SEARCH_DAYS * 86400
    order = str(args.get("order") or "newest").strip().lower()
    if order not in {"newest", "oldest"}:
        return {"ok": False, "error": "order must be newest or oldest"}
    try:
        found = await asyncio.to_thread(
            search_execution_records, db.path, terms,
            owner_chat_id=current.owner_chat_id if current else 0,
            chat_id=info.internal_chat_id if info is not None else 0,
            exclude_chat_id=current_chat if kind == "others" else 0,
            current_chat_id=current_chat, include_archived=include_archived,
            root_turn_uuid=root, exclude_turn_uuid=current_root if excluded else "",
            since=since, until=until,
            after=max(0, _int_arg(args, "after", default=0)),
            high_water=_int_arg(args, "highWater", default=0) or None,
            limit=max(1, min(_int_arg(args, "limit", default=20), 100)),
            newest_first=order == "newest",
            max_snippet=max(120, min(_int_arg(args, "maxSnippetChars", default=300), 2000)),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    events = found.pop("events")
    searched = {"scope": scope_label(kind, info.conversation_uuid if info is not None else ""),
                "conversations": found.pop("conversations"),
                "since": _fmt_ts(since) if since else None, "until": _fmt_ts(until) if until else None,
                "currentTurnExcluded": excluded}
    if kind in {"all", "others"}:
        searched["archived"] = "included" if include_archived else "excluded"
    out: dict[str, Any] = {"ok": True, "source": "execution", "action": "search", "query": query, "terms": terms,
                           "searched": searched, "returned": len(events), **found,
                           "ranking": f"more matched terms first, then {order} first"}
    if events and not out["allTermsMatched"] and len(terms) > 1:
        out["note"] = f"no record contains all {len(terms)} terms; these are the closest partial matches"
    out["events"] = events
    if events:
        out["next"] = ("read_events with these eventIds for full text" +
                       (" (scope=all for records in other conversations)" if kind in {"all", "others"} else ""))
    return out


def register_history_tools(reg: ToolRegistry, db: Any) -> None:
    async def _history(args: dict[str, Any]) -> str:
        ctx = current_tool_context()
        if "opId" in args and str(args.get("source") or "visible") != "visible":
            return "error: opId requires source=visible"
        if str(args.get("source") or "visible") == "execution":
            from app.context.history import ControllerExecutionHistory
            from app.tools.agent_history import query_execution_history
            if ctx.task_uuid or str(ctx.source or "").startswith("agent:"):
                return json.dumps({"ok": False, "error": "use_own_AgentHistory"})
            action = str(args.get("action") or "index")
            if action == "search":
                return json.dumps(await _execution_search(db, args, ctx), ensure_ascii=False)
            if action in {"read", "read_event", "read_events"} and resolve_scope(args, "current")[0] in {"all", "others"}:
                from app.context.history import OwnerExecutionEvents
                kind = resolve_scope(args, "current")[0]
                with _connect_ro(db.path) as con:
                    current = _resolve_conversation(con, ctx.conversation_uuid) if ctx.conversation_uuid else None
                if current is None:
                    return json.dumps({"ok": False, "error": f"scope={kind} needs the current Web conversation"})
                store = OwnerExecutionEvents(db, owner_chat_id=current.owner_chat_id,
                                             exclude_chat_id=current.internal_chat_id if kind == "others" else 0)
                try:
                    result = await query_execution_history(store, args)
                except (RuntimeError, ValueError) as exc:
                    result = {"ok": False, "error": str(exc)}
                return json.dumps(result, ensure_ascii=False)
            with _connect_ro(db.path) as con:
                conv_uuid, info, error = _conversation_from_args(con, args, current_conversation_uuid=ctx.conversation_uuid)
                if error or info is None:
                    return json.dumps({"ok": False, "error": error or "conversation_not_found"})
                row = con.execute("SELECT session_uuid FROM sessions WHERE chat_id=?", (info.internal_chat_id,)).fetchone()
            session = str(row["session_uuid"] or "") if row else ""
            if not session:
                return json.dumps({"ok": False, "error": "conversation_session_unavailable"})
            current_root = ctx.run_root_turn_uuid or ctx.turn_uuid
            root = str(args.get("rootTurnUuid") or "")
            if _bool_arg(args, "currentTask", False):
                if conv_uuid != ctx.conversation_uuid or not current_root:
                    return json.dumps({"ok": False, "error": "current_task_requires_current_conversation"})
                root = current_root
            exclude = _bool_arg(args, "excludeCurrentTurn", False)
            if exclude and root and root == current_root:
                return json.dumps({"ok": False, "error": "current_task_cannot_exclude_itself"})
            store = ControllerExecutionHistory(db, chat_id=info.internal_chat_id, session_uuid=session,
                                               root_turn_uuid=root, exclude_turn_uuid=current_root if exclude and conv_uuid == ctx.conversation_uuid else "")
            try:
                result = await query_execution_history(store, args)
            except (RuntimeError, ValueError) as exc:
                result = {"ok": False, "error": str(exc)}
            return json.dumps(result, ensure_ascii=False)
        return run_history_action(
            db.path,
            args,
            current_conversation_uuid=ctx.conversation_uuid,
            current_turn_uuid=ctx.turn_uuid,
            current_root_turn_uuid=ctx.run_root_turn_uuid,
        )

    reg.add(
        "History",
        "Read original OpenBear records. source=visible (default) reads user/assistant dialogue; source=execution reads original tool arguments/results, including the current task. "
        "scope: current | others | all | a conversation UUID, always this owner's conversations. Defaults: search all (the active turn excluded); read, read_turn, index and read_event(s) current. "
        "search ranks results by how many terms matched, then by time, and reports matchedTerms; visible results are grouped by conversation, marked current or other. Execution search hits carry the conversation title/UUID, tool, role, time, event ID and a snippet; read_events (scope=all for other conversations) returns full text, follow nextOffset. "
        "To find where an object came from (background job, process, port, file, config) when the dialogue never mentioned it, search source=execution for its ID, command or path across conversations (last 30 days unless since/until). Hidden reasoning is never available.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "search", "list", "read_turn", "index", "read_event", "read_events"], "description": "Visible dialogue or execution history action"},
                "source": {"type": "string", "enum": ["visible", "execution"], "description": "Default visible. Execution includes original tool evidence and supports index/read_event/read_events."},
                "eventId": {"type": "string"},
                "eventIds": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "highWater": {"type": "integer", "description": "Execution index paging: omit or 0 to start; reuse the returned highWater (search: upper id bound)."},
                "rootTurnUuid": {"type": "string", "description": "Optional execution root-turn filter."},
                "currentTask": {"type": "boolean", "description": "Execution only: just the current root task (scope=current); not with excludeCurrentTurn=true."},
                "scope": {"type": "string", "description": "current | others | all | a conversation UUID; search defaults to all, the other actions to current (read_event(s) accept all/others for other conversations' events)."},
                "conversationUuid": {"type": "string", "description": "One Web conversation; overrides scope"},
                "turnUuid": {"type": "string", "description": "Turn UUID for read_turn"},
                "opId": {"type": "string", "description": "Exact message ID for action=read; returns only that message."},
                "query": {"type": "string", "description": "search: space-separated terms, \"a phrase\" in double quotes; case-insensitive; partial matches rank after full ones"},
                "since": {"type": "string", "description": "Execution search start: YYYY-MM-DD[ HH:MM] (UTC+8), ISO, unix seconds or an age like 7d; default 30d across conversations."},
                "until": {"type": "string", "description": "Execution search end, same formats (a bare date includes that day)."},
                "order": {"type": "string", "enum": ["newest", "oldest"], "description": "Execution search ties: newest (default) or oldest, which finds where an ID first appeared."},
                "from": {"type": "string", "description": "read position: start or end"},
                "turns": {"type": "integer", "description": "Number of turns to read"},
                "lastTurns": {"type": "integer", "description": "Alias for turns when reading from end"},
                "offset": {"type": "integer", "description": "Visible: turns to skip. Execution read_event: character offset from nextOffset."},
                "before": {"type": "integer", "description": "Turns before target for read_turn"},
                "after": {"type": "integer", "description": "read_turn: turns after target. Execution index: nextAfter cursor."},
                "limit": {"type": "integer", "description": "Results: visible search 5 (max 20); execution search 20, index 30 (max 100)"},
                "maxChars": {"type": "integer", "description": "Visible default 20000 (max 60000); execution page 1000–32000."},
                "maxSnippetChars": {"type": "integer", "description": "Search snippet size (visible default 500, execution 300)"},
                "excludeCurrentTurn": {"type": "boolean", "description": "Default true for visible read/search and cross-conversation execution search."},
                "includeNotices": {"type": "boolean", "description": "Include visible notice operations; default false"},
                "includeArchived": {"type": "boolean", "description": "Include archived conversations for list/search; default false"},
            },
            "required": ["action"],
        },
        _history,
        visibility={"main", "runtime"},
        preserve_result=True,
    )
