from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from app.db.dao import MessageDAO
from app.llm.events import ToolCall
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.history import register_history_tools, run_history_action
from tests.test_web_admin import web_env as controller_web_env

web_env = controller_web_env


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

    # The message with both terms comes first; one with only "History" follows as a partial match.
    assert "Returned: 2 (all terms: 1, partial: 1)" in out
    assert "conv-a" in out
    assert out.index("第一轮结论：应该新增只读 History 工具") < out.index("第一个需求：请调研 History 工具")
    assert "matchedTerms: 只读, history (2/2)" in out and "matchedTerms: history (1/2)" in out
    assert "SECRET TOOL RESULT" not in out


def test_history_search_visible_text_in_another_case_outside_ascii(tmp_path: Path) -> None:
    """11-12 review: SQLite folds ASCII alone, so a lower-case term missed its capitalised word."""
    db = tmp_path / "openbear.db"
    _make_db(db)
    con = sqlite3.connect(db)
    for i, (text, ensure_ascii) in enumerate((("Ärger mit dem Δέλτα-Server", False), ("Ärger escaped", True))):
        payload = json.dumps({"role": "user", "text": text}, ensure_ascii=ensure_ascii)
        con.execute("""INSERT INTO web_operations (conversation_uuid, op_id, op_type, turn_uuid, display_seq, internal, payload_json,
                       revision, created_at_ms, updated_at_ms) VALUES ('conv-a',?,'user_message','turn-3',?,0,?,1,?,?)""",
                    (f"msg:u3-{i}", 80 + i, payload, 108000 + i, 108000 + i))
    con.commit()
    con.close()
    out = run_history_action(str(db), {"action": "search", "query": "ärger", "limit": 5})
    assert "Returned: 2" in out and "Ärger mit dem" in out and "Ärger escaped" in out
    assert "Δέλτα-Server" in run_history_action(str(db), {"action": "search", "query": "δέλτα", "limit": 5})


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


def _add_conversation(path: Path, conv: str, owner: int, chat: int, title: str, updated: int, rows: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO web_conversations (conversation_uuid, owner_chat_id, internal_chat_id, title, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (conv, owner, chat, title, updated, updated),
    )
    con.executemany(
        """INSERT INTO web_operations (conversation_uuid, op_id, op_type, turn_uuid, display_seq, internal, payload_json, revision,
           created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, 0, ?, 1, ?, ?)""",
        [(conv, op_id, op_type, turn, seq, json.dumps({"text": text}), ts, ts) for op_id, op_type, turn, seq, text, ts in rows],
    )
    con.commit()
    con.close()


def test_history_search_defaults_to_all_conversations_grouped_and_ranked(tmp_path: Path) -> None:
    db = tmp_path / "openbear.db"
    _make_db(db)
    # conv-b is updated last, yet older matches elsewhere still rank by matched terms, then time.
    _add_conversation(db, "conv-b", 1, -2, "地图迁移", 900, [
        ("b:u1", "user_message", "b-1", 10, "高德地图换成天地图", 300000),
        ("b:a1", "assistant_message", "b-1", 20, "只改了天地图的逆地址", 400000),
    ])
    _add_conversation(db, "conv-x", 2, -3, "别人的会话", 950, [("x:u1", "user_message", "x-1", 10, "高德地图 天地图", 500000)])
    con = sqlite3.connect(db)
    con.execute("""INSERT INTO web_operations (conversation_uuid, op_id, op_type, turn_uuid, display_seq, internal, payload_json,
                   revision, created_at_ms, updated_at_ms) VALUES ('conv-a','msg:now','user_message','turn-now',80,0,?,1,600000,600000)""",
                (json.dumps({"text": "帮我找高德地图换天地图的那次"}),))
    con.commit()
    con.close()

    out = run_history_action(str(db), {"action": "search", "query": "高德地图 天地图"},
                             current_conversation_uuid="conv-a", current_turn_uuid="turn-now")

    assert "Scope: all conversations (current + others); archived excluded" in out
    assert "currentTurnExcluded: true" in out
    assert "帮我找高德地图" not in out and "别人的会话" not in out
    assert "## Other conversation: 地图迁移" in out
    assert "matchedTerms: 高德地图, 天地图 (2/2)" in out and "matchedTerms: 天地图 (1/2)" in out
    assert out.index("高德地图换成天地图") < out.index("只改了天地图的逆地址")

    current = run_history_action(str(db), {"action": "search", "query": "History", "scope": "current"},
                                 current_conversation_uuid="conv-a", current_turn_uuid="turn-now")
    assert "Scope: current conversation" in current and "## Current conversation: 历史回看测试" in current
    others = run_history_action(str(db), {"action": "search", "query": "天地图", "scope": "others"},
                                current_conversation_uuid="conv-a", current_turn_uuid="turn-now")
    assert "Scope: other conversations (current conversation excluded)" in others
    assert "Current conversation" not in others and "地图迁移" in others

    partial = run_history_action(str(db), {"action": "search", "query": "天地图 不存在的词"},
                                 current_conversation_uuid="conv-a", current_turn_uuid="turn-now")
    assert "Note: no message contains all 2 terms; these are the closest partial matches." in partial
    assert "error: scope=all is only for search" in run_history_action(
        str(db), {"action": "read", "scope": "all"}, current_conversation_uuid="conv-a")


async def _dispatch(reg, args, ctx):
    return json.loads(await reg.dispatch("History", json.dumps(args), context=ctx))


async def test_execution_search_across_one_owners_conversations(web_env):
    db = web_env.db
    dao = MessageDAO(db)
    current = await web_env.server._create_web_conversation(123, title="排查后台任务")
    origin = await web_env.server._create_web_conversation(123, title="调研地图方案")
    foreign = await web_env.server._create_web_conversation(456, title="别人的会话")
    chat, origin_chat = int(current["internal_chat_id"]), int(origin["internal_chat_id"])
    session = await dao.get_or_create_session_uuid(chat)
    await dao.get_or_create_session_uuid(origin_chat)
    stale = await dao.add(origin_chat, "tool", '{"job": {"id": "jobzz9"}, "note": "old"}', name="Servers", tool_call_id="old")
    await db.conn.execute("UPDATE messages SET created_at=? WHERE id=?", (int(time.time()) - 60 * 86400, stale))
    call = await dao.add(origin_chat, "assistant", tool_calls=[ToolCall("start", "Servers",
                         '{"action": "job_start", "params": {"command": "node start-map-test.cjs"}}')], turn_uuid="t1", run_root_turn_uuid="t1")
    started = await dao.add(origin_chat, "tool", json.dumps({"ok": True, "job": {"id": "jobzz9", "command": "node start-map-test.cjs"}}),
                            name="Servers", tool_call_id="start", turn_uuid="t1", run_root_turn_uuid="t1")
    await dao.add(int(foreign["internal_chat_id"]), "tool", '{"job": {"id": "jobzz9"}}', name="Servers", tool_call_id="x")
    asked = await dao.add(chat, "user", "jobzz9 是谁启动的？", turn_uuid="now", run_root_turn_uuid="now")
    await db.conn.commit()
    reg = ToolRegistry()
    register_history_tools(reg, db)
    ctx = ToolRuntimeContext(chat_id=chat, conversation_uuid=current["conversation_uuid"], session_uuid=session,
                             source="web", turn_uuid="now", run_root_turn_uuid="now")

    found = await _dispatch(reg, {"source": "execution", "action": "search", "query": "jobzz9"}, ctx)
    assert found["ok"] and found["searched"]["scope"] == "all conversations (current + others)"
    assert found["searched"]["currentTurnExcluded"] is True and found["searched"]["since"]
    hits = {event["seq"]: event for event in found["events"]}
    assert set(hits) == {started}  # not the stale record, the foreign owner's, or this turn's question
    hit = hits[started]
    assert hit["conversationTitle"] == "调研地图方案" and hit["conversationUuid"] == origin["conversation_uuid"]
    assert hit["toolName"] == "Servers" and hit["role"] == "tool" and hit["event_id"] == f"message:{started}"
    assert hit["time"].endswith("UTC+8") and "jobzz9" in hit["snippet"] and hit["matchedTerms"] == ["jobzz9"]

    older = await _dispatch(reg, {"source": "execution", "action": "search", "query": "jobzz9", "since": "90d"}, ctx)
    assert stale in {event["seq"] for event in older["events"]}
    ranked = await _dispatch(reg, {"source": "execution", "action": "search", "query": "start-map-test job_start"}, ctx)
    assert ranked["events"][0]["seq"] == call and ranked["events"][0]["matchedTerms"] == ["start-map-test", "job_start"]
    assert ranked["events"][1]["matchedTerms"] == ["start-map-test"] and ranked["allTermsMatched"] == 1
    others = await _dispatch(reg, {"source": "execution", "action": "search", "query": "jobzz9", "scope": "others",
                                   "excludeCurrentTurn": False}, ctx)
    assert {event["seq"] for event in others["events"]} == {started}
    mine = await _dispatch(reg, {"source": "execution", "action": "search", "query": "jobzz9", "scope": "current"}, ctx)
    assert {event["seq"] for event in mine["events"]} == {asked}

    full = await _dispatch(reg, {"source": "execution", "action": "read_events", "scope": "all",
                                 "eventIds": [f"message:{started}"]}, ctx)
    assert full["ok"] and "start-map-test" in full["events"][0]["text"]
    assert full["events"][0]["conversationTitle"] == "调研地图方案"
    # read_events without scope keeps reading the current conversation only.
    assert not (await _dispatch(reg, {"source": "execution", "action": "read_events", "eventIds": [f"message:{started}"]}, ctx))["ok"]
    agent_ctx = ToolRuntimeContext(chat_id=chat, conversation_uuid=current["conversation_uuid"], session_uuid=session,
                                   source="agent:test", task_uuid="task-1")
    assert (await _dispatch(reg, {"source": "execution", "action": "search", "query": "jobzz9"}, agent_ctx))["error"] == "use_own_AgentHistory"


async def test_execution_search_goes_by_message_time_not_id(web_env):
    db = web_env.db
    dao = MessageDAO(db)
    current = await web_env.server._create_web_conversation(123, title="当前")
    other = await web_env.server._create_web_conversation(123, title="别处")
    chat, other_chat = int(current["internal_chat_id"]), int(other["internal_chat_id"])
    session = await dao.get_or_create_session_uuid(chat)
    await dao.get_or_create_session_uuid(other_chat)
    now = int(time.time())
    # A copied conversation's messages keep their old times under new ids, between newer ones.
    rows = [("old", now - 60 * 86400), ("recent FÖRDERUNG", now - 3 * 86400), ("copied", now - 59 * 86400),
            ("copied", now - 58 * 86400), ("latest", now - 86400)]
    ids = []
    for text, at in rows:
        ids.append(await dao.add(other_chat, "tool", f"marker77 {text}", name="Bash", tool_call_id=f"c{len(ids)}"))
        await db.conn.execute("UPDATE messages SET created_at=? WHERE id=?", (at, ids[-1]))
    await db.conn.commit()
    reg = ToolRegistry()
    register_history_tools(reg, db)
    ctx = ToolRuntimeContext(chat_id=chat, conversation_uuid=current["conversation_uuid"], session_uuid=session,
                             source="web", turn_uuid="now", run_root_turn_uuid="now")
    found = await _dispatch(reg, {"source": "execution", "action": "search", "query": "marker77", "since": "30d"}, ctx)
    assert [e["seq"] for e in found["events"]] == [ids[4], ids[1]] and not found["hasMore"]
    everything = await _dispatch(reg, {"source": "execution", "action": "search", "query": "marker77", "since": "90d", "limit": 2}, ctx)
    assert [e["seq"] for e in everything["events"]] == [ids[4], ids[1]] and everything["hasMore"] and "limit" in everything["more"]
    oldest = await _dispatch(reg, {"source": "execution", "action": "search", "query": "marker77", "since": "90d", "order": "oldest"}, ctx)
    assert [e["seq"] for e in oldest["events"]] == [ids[0], ids[2], ids[3], ids[1], ids[4]]
    folded = await _dispatch(reg, {"source": "execution", "action": "search", "query": "förderung"}, ctx)
    assert [e["seq"] for e in folded["events"]] == [ids[1]] and folded["events"][0]["matchedTerms"] == ["förderung"]
