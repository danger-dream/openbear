#!/usr/bin/env python3
"""Query OpenBear visible Web conversation history.

Standalone test harness for the History tool semantics.  It reads only the Web UI
visible transcript projection by default: user messages + assistant final text.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tools.history import run_history_action  # noqa: E402


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=str(ROOT / "data" / "openbear.db"), help="SQLite DB path")
    parser.add_argument("--current-conversation", default="", help="Current Web conversation UUID for scope=current / owner filtering")
    parser.add_argument("--current-turn", default="", help="Current turn UUID to exclude by default for current reads")
    parser.add_argument("--max-chars", type=int, default=20_000, help="Maximum output chars")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenBear visible conversation history query")
    _add_common(parser)
    sub = parser.add_subparsers(dest="action", required=True)

    read = sub.add_parser("read", help="Read visible turns from a conversation")
    _add_common(read)
    read.add_argument("--conversation", "--conversation-uuid", dest="conversationUuid", default="")
    read.add_argument("--scope", default="")
    read.add_argument("--from", dest="from_", default="end", choices=["start", "end"])
    read.add_argument("--turns", "--last-turns", dest="turns", type=int, default=5)
    read.add_argument("--include-notices", action="store_true")
    read.add_argument("--no-exclude-current-turn", action="store_true")

    search = sub.add_parser("search", help="Search visible user/assistant text across conversations")
    _add_common(search)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--max-snippet-chars", type=int, default=500)
    search.add_argument("--include-archived", action="store_true")

    list_cmd = sub.add_parser("list", help="List recent Web conversations")
    _add_common(list_cmd)
    list_cmd.add_argument("--limit", type=int, default=10)
    list_cmd.add_argument("--include-archived", action="store_true")

    turn = sub.add_parser("read-turn", help="Read a target turn and nearby turns")
    _add_common(turn)
    turn.add_argument("--conversation", "--conversation-uuid", dest="conversationUuid", required=True)
    turn.add_argument("--turn", "--turn-uuid", dest="turnUuid", required=True)
    turn.add_argument("--before", type=int, default=2)
    turn.add_argument("--after", type=int, default=2)

    raw = sub.add_parser("json", help="Run with a raw JSON argument object")
    _add_common(raw)
    raw.add_argument("args_json")
    return parser


def args_to_payload(ns: argparse.Namespace) -> dict:
    if ns.action == "json":
        data = json.loads(ns.args_json)
        if not isinstance(data, dict):
            raise SystemExit("args_json must decode to an object")
        data.setdefault("maxChars", ns.max_chars)
        return data
    payload: dict = {"action": "read_turn" if ns.action == "read-turn" else ns.action, "maxChars": ns.max_chars}
    if ns.action == "read":
        payload.update({
            "conversationUuid": ns.conversationUuid,
            "scope": ns.scope,
            "from": ns.from_,
            "turns": ns.turns,
            "includeNotices": bool(ns.include_notices),
            "excludeCurrentTurn": not bool(ns.no_exclude_current_turn),
        })
    elif ns.action == "search":
        payload.update({
            "query": ns.query,
            "limit": ns.limit,
            "maxSnippetChars": ns.max_snippet_chars,
            "includeArchived": bool(ns.include_archived),
        })
    elif ns.action == "list":
        payload.update({"limit": ns.limit, "includeArchived": bool(ns.include_archived)})
    elif ns.action == "read-turn":
        payload.update({
            "conversationUuid": ns.conversationUuid,
            "turnUuid": ns.turnUuid,
            "before": ns.before,
            "after": ns.after,
        })
    return payload


def main() -> int:
    parser = build_parser()
    ns = parser.parse_args()
    payload = args_to_payload(ns)
    result = run_history_action(
        ns.db,
        payload,
        current_conversation_uuid=ns.current_conversation,
        current_turn_uuid=ns.current_turn,
    )
    sys.stdout.write(result)
    if not result.endswith("\n"):
        sys.stdout.write("\n")
    return 1 if result.startswith("error:") else 0


if __name__ == "__main__":
    raise SystemExit(main())
