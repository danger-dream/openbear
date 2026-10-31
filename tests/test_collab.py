"""Cross-conversation requests and groups on the real Web controller host."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.collab.groups import GroupService
from app.collab.service import CollabService
from app.collab.store import many, one
from app.db.engine import DB
from app.llm.events import StreamEvent, ToolCall
from app.tools.base import ToolRegistry, current_tool_context
from app.tools.collab import register_collab_tools
from app.web_admin import WebAdminServer
from tests.test_web_admin import FakeBot, FakeRunFactory, _cfg


class RoutedBackend:
    """Fake model: one scripted handler per conversation, chosen by its system prompt marker."""
    protocol = "fake"

    def __init__(self):
        self.handlers = {}
        self.calls = []

    async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        name = next((key for key in self.handlers if f"[{key}]" in system), "lead")
        self.calls.append((name, messages, tools, system))
        step = sum(1 for call in self.calls if call[0] == name)
        events = await self.handlers[name](step, messages, tools, system)
        for event in events:
            yield event

    async def complete(self, messages, *, model, **k):
        raise AssertionError("not used")


def tool(name, args, call_id):
    return [StreamEvent(kind="tool_call", tool_calls=[ToolCall(call_id, name, json.dumps(args, ensure_ascii=False))]),
            StreamEvent(kind="finish", finish_reason="tool_calls")]


def say(text):
    return [StreamEvent(kind="content", text=text), StreamEvent(kind="finish", finish_reason="stop")]


def last_tool(messages):
    return json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])


def user_texts(messages):
    return "\n".join(str(m.get("content")) for m in messages if m["role"] == "user")


async def eventually(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.02)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBEAR_WEB_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    db = DB(str(tmp_path / "collab.sqlite"))
    await db.connect()
    server = WebAdminServer(_cfg(), db, FakeBot())
    server.workspace_dir = str(tmp_path)
    server.model_selection = SimpleNamespace(current="openai/gpt")
    backend = RoutedBackend()
    server.llm_factory = FakeRunFactory(backend, context_window=128000)
    service = CollabService(db, server)
    groups = GroupService(db, server, service)
    server.interactions.validators["group_proposal"] = groups.validate_answer
    tools = ToolRegistry()

    async def agent_wait(args):
        return await current_tool_context().agent_wait(args)

    tools.add("AgentWait", "wait", {"type": "object", "properties": {}}, agent_wait, visibility={"main"}, preserve_result=True)
    tools.add("UserInteraction", "ask", {"type": "object", "properties": {}}, lambda a: "x", visibility={"main"})
    tools.add("Cron", "cron", {"type": "object", "properties": {}}, lambda a: "x", visibility={"main"})
    register_collab_tools(tools, service, groups)
    server.tools = tools

    async def system(conversation_uuid=""):
        row = await one(db.conn, "SELECT title FROM web_conversations WHERE conversation_uuid=?", (conversation_uuid,))
        params = await server._prompt_template_params_live(conversation_uuid)
        return f"[{row['title'] if row else 'lead'}]\n{params.get('folderPrompt', '')}\nTOOLS:{','.join(params.get('toolNames', []))}"

    monkeypatch.setattr(server, "_build_system_prompt_for_chat", system)
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    await service.start()
    try:
        yield SimpleNamespace(db=db, server=server, service=service, groups=groups, backend=backend, client=client, tmp=tmp_path)
    finally:
        await service.stop()
        await server.runs.cancel_all_and_wait()
        await client.close()
        await db.close()


async def start(env, conversation, text):
    await env.server._start_or_steer_web_conversation(conversation, text, [], env.server._live_for(conversation))


async def idle(env, conversation):
    return not await env.service._busy(conversation)


async def final_assistant(env, conversation):
    row = await one(env.db.conn, """SELECT content FROM messages WHERE chat_id=? AND role='assistant' AND content!=''
        ORDER BY id DESC LIMIT 1""", (conversation["internal_chat_id"],))
    return row["content"] if row else ""


async def test_request_needs_input_supply_and_result(env):
    a = await env.server._create_web_conversation(123, title="A")
    b = await env.server._create_web_conversation(123, title="B")
    seen = {}

    async def lead(step, messages, tools, system):
        if step == 1:
            return tool("ConversationRequest", {"action": "send", "to": b["conversation_uuid"],
                                                "title": "审方案", "body": "请审 plan.md"}, "s1")
        if step == 2:
            assert last_tool(messages)["detached"] is True
            return tool("AgentWait", {"mode": "event_only"}, "w1")
        if step == 3:
            wait = last_tool(messages)
            note = json.dumps(wait, ensure_ascii=False)
            assert "需要补充" in note and "压测数据" in note
            seen["request"] = wait["notifications"][0]["taskUuid"].split(":", 1)[1]
            return tool("ConversationRequest", {"action": "supply", "requestId": seen["request"], "text": "QPS=1200"}, "p1")
        if step == 4:
            return tool("AgentWait", {"mode": "event_only"}, "w2")
        if step == 5:
            note = json.dumps(last_tool(messages), ensure_ascii=False)
            assert "已完成" in note and "方案可行" in note
            return say("B 认为可行")
        raise AssertionError(step)

    async def reviewer(step, messages, tools, system):
        names = {t["name"] for t in tools}
        assert "ConversationRequest" in names
        texts = user_texts(messages)
        assert "Cross-conversation request" in texts
        if step == 1:
            return tool("ConversationRequest", {"action": "respond", "outcome": "needs_input", "text": "缺压测数据"}, "r1")
        if step == 2:
            return say("需要压测数据才能判断")
        if step == 3:
            assert "QPS=1200" in texts and "对方补充的材料" in texts
            return tool("ConversationRequest", {"action": "respond", "outcome": "completed"}, "r2")
        if step == 4:
            return say("方案可行")
        raise AssertionError(step)

    env.backend.handlers.update(lead=lead, B=reviewer)
    await start(env, a, f"请让 [B](openbear://ref/chat/{b['conversation_uuid']}) 审一下")
    await eventually(lambda: idle(env, a))
    await eventually(lambda: idle(env, b))
    assert await final_assistant(env, a) == "B 认为可行"
    row = await one(env.db.conn, "SELECT * FROM collab_requests")
    assert row["state"] == "completed" and row["input_rounds"] == 1
    assert not {"kind", "verdict", "subject_json", "subject_changed", "related_request_uuid"} & set(row)
    kinds = [e["kind"] for e in await many(env.db.conn, "SELECT kind FROM collab_request_events WHERE request_uuid=? ORDER BY id", (row["request_uuid"],))]
    assert kinds == ["sent", "delivered", "needs_input", "input", "delivered", "result"]



async def test_receiver_reminded_once_to_declare_and_body_kept(env):
    a = await env.server._create_web_conversation(123, title="A")
    b = await env.server._create_web_conversation(123, title="B")

    async def lead(step, messages, tools, system):
        if step == 1:
            return tool("ConversationRequest", {"action": "send", "to": b["conversation_uuid"],
                                                "title": "审方案", "body": "看看"}, "s1")
        if step == 2:
            return tool("AgentWait", {"mode": "event_only"}, "w1")
        note = json.dumps(last_tool(messages), ensure_ascii=False)
        assert "结论：fail，TTL 太长" in note
        return say("收到")

    async def reviewer(step, messages, tools, system):
        if step == 1:
            return say("结论：fail，TTL 太长")
        if step == 2:
            assert "还没有声明结果" in user_texts(messages)
            return tool("ConversationRequest", {"action": "respond", "outcome": "completed", "text": "TTL 太长"}, "r1")
        if step == 3:
            return say("已声明")
        raise AssertionError(step)

    env.backend.handlers.update(lead=lead, B=reviewer)
    await start(env, a, f"请让 [B](openbear://ref/chat/{b['conversation_uuid']}) 审一下")
    await eventually(lambda: idle(env, a))
    await eventually(lambda: idle(env, b))
    row = await one(env.db.conn, "SELECT * FROM collab_requests")
    assert row["state"] == "completed"
    assert "结论：fail，TTL 太长" in row["result_text"]
    assert await final_assistant(env, a) == "收到"

async def test_any_conversation_is_reachable_but_wait_cycle_is_refused(env):
    a = await env.server._create_web_conversation(123, title="A")
    b = await env.server._create_web_conversation(123, title="B")
    results = {}

    async def lead(step, messages, tools, system):
        if step == 1:
            return tool("ConversationRequest", {"action": "send", "to": b["conversation_uuid"],
                                                "title": "x", "body": "y"}, "s1")
        results["sent"] = last_tool(messages)
        return say("done")

    env.backend.handlers.update(lead=lead)
    await start(env, a, "随便问问另一个会话")
    await eventually(lambda: idle(env, a))
    assert results["sent"].get("requestId")
    # A waits on B; B asking A back would deadlock and is refused.
    await env.service.send(sender_uuid=a["conversation_uuid"], root_turn_uuid="r", target_uuid=b["conversation_uuid"],
                           title="t", body="b")
    with pytest.raises(Exception) as caught:
        await env.service.send(sender_uuid=b["conversation_uuid"], root_turn_uuid="r2", target_uuid=a["conversation_uuid"],
                               title="t", body="b")
    assert getattr(caught.value, "code", "") == "wait_cycle"


async def test_group_proposal_edit_diff_member_rules_and_silence(env):
    lead_conv = await env.server._create_web_conversation(123, title="lead")
    proposal = {"name": "讨论组", "description": "评估方案", "prompt": {"goal": "评估缓存方案", "facts": "已有 Redis",
                "constraints": "不改代码", "deliverables": "结论+风险"},
                "members": [{"name": "审稿人", "model": "openai/gpt", "thinkingLevel": "high", "fast": False,
                             "prompt": "负责挑毛病，持续审查小组的方案和实测结果。"},
                            {"name": "实测员", "model": "openai/cheap", "thinkingLevel": "low", "fast": False,
                             "prompt": "负责写脚本实测，按需求补充实测数据。"}]}
    seen = {}

    async def lead(step, messages, tools, system):
        if step == 1:
            return tool("ConversationGroup", {"action": "propose", "group": proposal}, "g1")
        if step == 2:
            created = last_tool(messages)
            seen["created"] = created
            reviewer = created["group"]["members"][0]["conversationUuid"]
            return tool("ConversationRequest", {"action": "send", "to": reviewer, "title": "看看",
                                                "body": "评估一下"}, "s1")
        if step == 3:
            return tool("AgentWait", {"mode": "event_only"}, "w1")
        return say("组内结论已汇总")

    async def member(step, messages, tools, system):
        names = {t["name"] for t in tools}
        seen["member_tools"] = names
        seen["member_system"] = system
        return say("问题：缺少回滚")

    env.backend.handlers.update(lead=lead)
    env.backend.handlers["审稿人 · 讨论组"] = member
    await start(env, lead_conv, "建个小组讨论缓存方案")

    async def pending():
        return bool(env.server.interactions.pending_for(lead_conv["conversation_uuid"]))
    await eventually(pending)
    item = env.server.interactions.pending_for(lead_conv["conversation_uuid"])[0]
    assert item["action"] == "group_proposal" and item["models"]
    edited = json.loads(json.dumps(item["proposal"]))
    # Removing a member in the panel is refused; editing fields is accepted.
    dropped = {**edited, "members": edited["members"][:1]}
    refused = await env.server.interactions.submit(item["interactionId"], 123, {"decision": "approve", "proposal": dropped})
    assert refused["statusCode"] == 400
    edited["members"][0]["model"] = "openai/cheap"
    edited["members"][0]["thinkingLevel"] = "medium"
    edited["members"][0]["prompt"] = "负责挑毛病，尤其关注回滚。"
    accepted = await env.server.interactions.submit(item["interactionId"], 123, {"decision": "approve_with_comment",
                                                                                 "text": "严格一点", "proposal": edited})
    assert accepted["ok"], accepted
    await eventually(lambda: idle(env, lead_conv))
    created = seen["created"]
    assert created["humanEdited"] is True and created["comment"] == "严格一点"
    changes = "\n".join(created["humanChanges"])
    assert "openai/gpt" in changes and "openai/cheap" in changes and "回滚" in changes
    members = await many(env.db.conn, "SELECT * FROM collab_members ORDER BY sort")
    assert [m["name"] for m in members] == ["审稿人", "实测员"]
    reviewer_row = await one(env.db.conn, "SELECT model FROM web_conversations WHERE conversation_uuid=?", (members[0]["conversation_uuid"],))
    assert reviewer_row["model"] == "openai/cheap"
    # Member identity: no human-facing tools, fixed rules and group background in its prompt.
    assert "UserInteraction" not in seen["member_tools"] and "Cron" not in seen["member_tools"]
    assert "ConversationGroup" not in seen["member_tools"] and "ConversationRequest" in seen["member_tools"]
    assert "负责挑毛病，尤其关注回滚。" in seen["member_system"] and "小组存续期间你一直在" in seen["member_system"]
    assert "只掌握部分信息" in seen["member_system"] and "评估缓存方案" in seen["member_system"]
    assert "UserInteraction" not in seen["member_system"].split("TOOLS:")[1]
    assert await final_assistant(env, lead_conv) == "组内结论已汇总"
    # Members are listed under the lead, not as directory siblings, and never notify.
    response = await env.client.get("/api/conversation-tree/children", params={"systemNode": "temporary"})
    assert response.status in {200, 401}
    status = await env.server._tree_running_state(123)
    group_view = status["collabGroups"][lead_conv["conversation_uuid"]][0]
    assert group_view["memberCount"] == 2
    assert await env.service.silent_conversation(members[0]["conversation_uuid"], {})
    assert not await env.service.silent_conversation(lead_conv["conversation_uuid"], {})


async def test_human_edit_refreshes_member_and_notifies_lead(env):
    lead_conv = await env.server._create_web_conversation(123, title="lead")
    value, errors = env.groups.normalize({"name": "组", "prompt": {"goal": "g", "facts": "f", "constraints": "c", "deliverables": "d"},
                                          "members": [{"name": "甲", "model": "openai/gpt", "prompt": "r"}]})
    assert not errors
    group = await env.groups.create(lead_conv, value)
    member_uuid = group["members"][0]["conversationUuid"]
    member_conv = await env.service.conversation(member_uuid)
    await env.db.conn.execute("UPDATE sessions SET system_snapshot='old' WHERE chat_id=?", (member_conv["internal_chat_id"],))
    await env.db.conn.commit()
    current = await env.groups.current_value(group["groupId"])
    current["members"][0]["prompt"] = "新的设定"
    _result, changes = await env.groups.update(group["groupId"], current, actor="老大")
    assert any("提示词" in line and "新的设定" in line for line in changes)
    snapshot = await one(env.db.conn, "SELECT system_snapshot FROM sessions WHERE chat_id=?", (member_conv["internal_chat_id"],))
    assert snapshot["system_snapshot"] == ""
    lead_notes = await env.service.take_notes(lead_conv["conversation_uuid"])
    member_notes = await env.service.take_notes(member_uuid)
    assert "老大修改了小组" in lead_notes[0]["body"] and "被老大修改过" in member_notes[0]["body"]
    duplicated = {**current, "members": current["members"] + [{**current["members"][0], "memberKey": "extra"}]}
    with pytest.raises(Exception) as caught:
        await env.groups.update(group["groupId"], duplicated, actor="老大")
    assert getattr(caught.value, "code", "") == "membership_change_not_allowed"


async def test_stop_sender_cancels_receiver_and_late_result_does_not_wake(env):
    a = await env.server._create_web_conversation(123, title="A")
    b = await env.server._create_web_conversation(123, title="B")
    release = asyncio.Event()

    async def lead(step, messages, tools, system):
        if step == 1:
            return tool("ConversationRequest", {"action": "send", "to": b["conversation_uuid"],
                                                "title": "慢问题", "body": "慢慢想"}, "s1")
        return tool("AgentWait", {"mode": "event_only"}, f"w{step}")

    async def slow(step, messages, tools, system):
        await release.wait()
        return say("晚到的答案")

    env.backend.handlers.update(lead=lead, B=slow)
    await start(env, a, f"问问 openbear://ref/chat/{b['conversation_uuid']}")

    async def receiver_running():
        return b["conversation_uuid"] in env.service.active
    await eventually(receiver_running)
    await env.server._stop_web_conversation(a)
    release.set()
    await eventually(lambda: idle(env, b))
    row = await one(env.db.conn, "SELECT * FROM collab_requests")
    assert row["state"] == "cancelled"
    lead_calls = [c for c in env.backend.calls if c[0] == "lead"]
    await asyncio.sleep(0.3)
    assert len([c for c in env.backend.calls if c[0] == "lead"]) == len(lead_calls)


def record_statuses(env, conversation):
    statuses = []
    original = env.server._touch_web_conversation

    async def touch(conversation_uuid, **kw):
        if conversation_uuid == conversation["conversation_uuid"] and kw.get("current_status"):
            statuses.append(kw["current_status"])
        return await original(conversation_uuid, **kw)
    env.server._touch_web_conversation = touch
    return statuses


async def test_review_wait_names_the_conversation_it_waits_on(env):
    a = await env.server._create_web_conversation(123, title="A")
    b = await env.server._create_web_conversation(123, title="B")
    statuses = record_statuses(env, a)
    release = asyncio.Event()

    async def lead(step, messages, tools, system):
        if step == 1:
            return tool("ConversationRequest", {"action": "send", "to": b["conversation_uuid"], "title": "q", "body": "b"}, "s1")
        if step == 2:
            return tool("AgentWait", {"mode": "review_after", "reviewAfterSeconds": 1}, "w1")
        if step == 3:
            release.set()
            return tool("AgentWait", {"mode": "event_only"}, "w2")
        return say("好")

    async def answer(step, messages, tools, system):
        await release.wait()
        return say("答案")

    env.backend.handlers.update(lead=lead, B=answer)
    await start(env, a, f"问 openbear://ref/chat/{b['conversation_uuid']}")
    await eventually(lambda: idle(env, a))
    assert "等待「B」，计划 1 秒后统一复查" in statuses
    assert "等待「B」" in statuses
    texts = [op["payload"]["statusText"] for op in await env.server._web_operations(a["conversation_uuid"])
             if op["opType"] == "agent_supervision"]
    assert texts[0] == "统一复查时间已到"
    assert not [text for text in [*statuses, *texts] if "Agent" in text]


def test_returned_results_name_their_source():
    source = WebAdminServer._task_notification_source
    assert source([{"kind": "task-notification"}]) == "Agent "
    assert source([{"kind": "task-notification-collab"}]) == "会话请求"
    assert source([{"kind": "tool-notification"}]) == "后台任务"
    assert source([{"kind": "task-notification-collab"}, {}]) == "Agent、会话请求"


async def test_result_survives_sender_turn_end(env):
    """An interrupted sender turn (not a human stop) still receives the result in a new turn."""
    a = await env.server._create_web_conversation(123, title="A")
    b = await env.server._create_web_conversation(123, title="B")
    statuses = record_statuses(env, a)
    release = asyncio.Event()

    async def lead(step, messages, tools, system):
        if step == 1:
            return tool("ConversationRequest", {"action": "send", "to": b["conversation_uuid"],
                                                "title": "q", "body": "b"}, "s1")
        if step == 2:
            return tool("AgentWait", {"mode": "event_only"}, "w1")
        return say("收到回复：" + ("42" if "答案是 42" in user_texts(messages) else "?"))

    async def answer(step, messages, tools, system):
        await release.wait()
        return say("答案是 42")

    env.backend.handlers.update(lead=lead, B=answer)
    await start(env, a, f"问 openbear://ref/chat/{b['conversation_uuid']}")

    async def receiver_running():
        return b["conversation_uuid"] in env.service.active
    await eventually(receiver_running)
    assert await env.server.runs.cancel_and_wait(a["internal_chat_id"], timeout_s=5)
    assert (await one(env.db.conn, "SELECT state FROM collab_requests"))["state"] in {"delivered", "working"}
    release.set()

    async def answered():
        return (await final_assistant(env, a)).startswith("收到回复")
    await eventually(answered)
    assert await final_assistant(env, a) == "收到回复：42"
    assert (await one(env.db.conn, "SELECT state FROM collab_requests"))["state"] == "completed"
    assert "会话请求结果汇总中" in statuses and "Agent 结果汇总中" not in statuses
