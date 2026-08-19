from __future__ import annotations

import sqlite3
from pathlib import Path

from app.tools.history import run_history_action


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE web_conversations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          conversation_uuid TEXT NOT NULL UNIQUE,
          owner_chat_id INTEGER NOT NULL,
          internal_chat_id INTEGER NOT NULL UNIQUE,
          title TEXT DEFAULT '',
          model TEXT DEFAULT '',
          status TEXT DEFAULT 'idle',
          current_status TEXT DEFAULT '',
          last_error TEXT DEFAULT '',
          created_at INTEGER,
          updated_at INTEGER,
          archived_at INTEGER DEFAULT 0
        );
        CREATE TABLE web_operations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          conversation_uuid TEXT NOT NULL,
          internal_chat_id INTEGER DEFAULT 0,
          op_id TEXT NOT NULL,
          op_type TEXT NOT NULL,
          turn_uuid TEXT DEFAULT '',
          parent_turn_uuid TEXT DEFAULT '',
          run_root_turn_uuid TEXT DEFAULT '',
          target_type TEXT DEFAULT '',
          target_id TEXT DEFAULT '',
          task_uuid TEXT DEFAULT '',
          run_id TEXT DEFAULT '',
          display_seq INTEGER NOT NULL,
          status TEXT DEFAULT '',
          lifecycle TEXT DEFAULT '',
          internal INTEGER DEFAULT 0,
          source TEXT DEFAULT '',
          transcript_message_ids_json TEXT DEFAULT '[]',
          revision INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          created_at_ms INTEGER NOT NULL,
          updated_at_ms INTEGER NOT NULL
        );
        """
    )
    con.execute(
        """
        INSERT INTO web_conversations
          (conversation_uuid, owner_chat_id, internal_chat_id, title, model, status, created_at, updated_at)
        VALUES ('conv-a', 1, -1, '历史回看测试', 'test-model', 'idle', 100, 200)
        """
    )
    rows = [
        ("conv-a", "msg:u1", "user_message", "turn-1", 10, 0, '{"role":"user","text":"第一个需求：请调研 History 工具"}', 101000),
        ("conv-a", "tool:t1", "tool", "turn-1", 20, 0, '{"result":"SECRET TOOL RESULT SHOULD NOT APPEAR"}', 102000),
        ("conv-a", "reasoning:r1", "reasoning", "turn-1", 30, 0, '{"text":"hidden reasoning should not appear"}', 103000),
        ("conv-a", "assistant:a1", "assistant_message", "turn-1", 40, 0, '{"text":"第一轮结论：应该新增只读 History 工具","complete":true}', 104000),
        ("conv-a", "msg:u2", "user_message", "turn-2", 50, 0, '{"role":"user","text":"第二轮：继续"}', 105000),
        ("conv-a", "assistant:a2", "assistant_message", "turn-2", 60, 0, '{"text":"第二轮回复","complete":true}', 106000),
        ("conv-a", "notice:n1", "notice", "turn-2", 70, 1, '{"text":"internal notice","internal":true}', 107000),
    ]
    con.executemany(
        """
        INSERT INTO web_operations
          (conversation_uuid, op_id, op_type, turn_uuid, display_seq, internal, payload_json, revision, created_at_ms, updated_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        [(conv, op_id, op_type, turn, seq, internal, payload, ts, ts) for conv, op_id, op_type, turn, seq, internal, payload, ts in rows],
    )
    con.commit()
    con.close()


def test_history_read_visible_first_turn(tmp_path: Path) -> None:
    db = tmp_path / "openbear.db"
    _make_db(db)

    out = run_history_action(
        str(db),
        {"action": "read", "conversationUuid": "conv-a", "from": "start", "turns": 1, "maxChars": 8000},
    )

    assert "第一个需求：请调研 History 工具" in out
    assert "第一轮结论：应该新增只读 History 工具" in out
    assert "SECRET TOOL RESULT" not in out
    assert "hidden reasoning" not in out
    assert "internal notice" not in out


def test_history_read_turn_nearby(tmp_path: Path) -> None:
    db = tmp_path / "openbear.db"
    _make_db(db)

    out = run_history_action(
        str(db),
        {"action": "read_turn", "conversationUuid": "conv-a", "turnUuid": "turn-2", "before": 1, "after": 0, "maxChars": 8000},
    )

    assert "turnUuid: turn-1" in out
    assert "turnUuid: turn-2" in out
    assert "第二轮回复" in out


def test_history_search_visible_text(tmp_path: Path) -> None:
    db = tmp_path / "openbear.db"
    _make_db(db)

    out = run_history_action(str(db), {"action": "search", "query": "只读 History", "limit": 5})

    assert "Returned: 1" in out
    assert "conv-a" in out
    assert "第一轮结论：应该新增只读 History 工具" in out
    assert "SECRET TOOL RESULT" not in out


def test_history_current_scope_excludes_current_turn(tmp_path: Path) -> None:
    db = tmp_path / "openbear.db"
    _make_db(db)

    out = run_history_action(
        str(db),
        {"action": "read", "scope": "current", "turns": 5, "maxChars": 8000},
        current_conversation_uuid="conv-a",
        current_turn_uuid="turn-2",
    )

    assert "currentTurnExcluded: true" in out
    assert "turnUuid: turn-1" in out
    assert "turnUuid: turn-2" not in out
    assert "第二轮回复" not in out
