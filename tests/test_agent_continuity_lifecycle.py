from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.db.engine import DB
from app.llm.base import AgentResult
from app.llm.events import ToolCall
from app.rath.dao import RathDAO
from app.rath.plan import PlanError
from app.task_memory import TaskMemoryDAO
from app.tools.base import ToolRuntimeContext
from tests.test_agent_continuity import call, env
from tests.test_rath_plan import sample_plan
from tests.test_rath_web_api import web_env
from tests.test_tools_agent_orchestration import _FakeConfig


async def test_three_rounds_pin_template_and_keep_private_memory(env, monkeypatch):
    dao, reg, backend, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "first-fact", "tools": ["TaskMemory"]}, ctx)
    sid, t1 = first["agentSession"]["agentId"], first["task"]["taskId"]
    private_ctx = ToolRuntimeContext(chat_id=123, session_uuid=ctx.session_uuid,
                                    source="agent:general-purpose", task_uuid=t1, agent_session_uuid=sid)
    memory = await call(reg, "TaskMemory", {"action": "create", "name": "private-fact", "body": "retained",
                                            "idempotencyKey": "fact"}, private_ctx)
    mid = memory["memory"]["memoryUuid"]

    async def changed_template(*args, **kwargs):
        return "NEW-TEMPLATE-MUST-NOT-REPLACE-INSTANCE"
    monkeypatch.setattr("app.tools.agents.render_agent_base_system_prompt", changed_template)
    for index in (2, 3):
        result = await call(reg, "AgentContinue", {"to": sid, "prompt": f"round-{index}", "tools": ["TaskMemory"]}, ctx)
        assert result["status"] == "completed", result
        private_ctx.task_uuid = result["task"]["taskId"]
        assert result["task"]["sessionTurn"] == index
        assert "first-fact" in str(backend.calls[-1]["messages"])
        assert "NEW-TEMPLATE-MUST-NOT-REPLACE-INSTANCE" not in backend.calls[-1]["system"]
        assert (await call(reg, "TaskMemory", {"action": "get", "memoryUuid": mid}, private_ctx))["memory"]["body"] == "retained"
    assert "round-2" in str(backend.calls[-1]["messages"])
    assert len({item["session_id"] for item in backend.calls}) == 1
    assert (await dao.get_task(t1)).output == first["result"]
    sibling = await call(reg, "Agent", {"prompt": "independent", "tools": ["TaskMemory"]}, ctx)
    private_ctx.task_uuid = sibling["task"]["taskId"]
    private_ctx.agent_session_uuid = sibling["agentSession"]["agentId"]
    assert not (await call(reg, "TaskMemory", {"action": "get", "memoryUuid": mid}, private_ctx))["ok"]
    info = await call(reg, "AgentInfo", {"action": "get", "to": sid}, ctx)
    assert info["contextAvailable"] and info["context"]["templatePinned"]
    assert info["capabilities"]["grantedTools"] == ["TaskMemory"]
    assert info["capabilities"]["effectiveTools"] == []  # idle is not executing
    assert len(info["tasks"]) == 3
    assert "state_json" not in str(info) and "native_output_items" not in str(info)


async def test_cancel_mid_batch_keeps_completed_result_without_replaying_uncertain_tool(env, monkeypatch):
    dao, reg, backend, ctx, _ = env
    actual_calls = []

    async def read(args):
        actual_calls.append("Read")
        return "actual-source-result"

    async def uncertain(args):
        actual_calls.append("Bash")
        raise asyncio.CancelledError

    reg.add("Read", "Read", {"type": "object"}, read)
    reg.add("Bash", "Bash", {"type": "object"}, uncertain)
    original_complete = backend.complete

    async def first_request(messages, **kwargs):
        return AgentResult(tool_calls=[ToolCall(id="read", name="Read", arguments="{}"),
                                       ToolCall(id="write", name="Bash", arguments="{}")])
    monkeypatch.setattr(backend, "complete", first_request)
    first = await call(reg, "Agent", {"prompt": "do scoped work", "tools": ["Read", "Bash"]}, ctx)
    assert first["status"] == "cancelled", first
    sid = first["agentSession"]["agentId"]
    source = await dao.task_model_context(first["task"]["taskId"])
    assert source["state"]["inflightTool"]["name"] == "Bash"
    assert "actual-source-result" in str(source["state"]["messages"])
    monkeypatch.setattr(backend, "complete", original_complete)
    second = await call(reg, "AgentContinue", {"to": sid, "prompt": "report retained facts; do not rerun", "tools": []}, ctx)
    assert second["status"] == "completed", second
    request = str(backend.calls[-1]["messages"])
    assert "actual-source-result" in request and "不要自动重放" in request
    assert actual_calls == ["Read", "Bash"]
    assert (await dao.get_task(first["task"]["taskId"])).status == "cancelled"


async def test_new_round_keeps_control_facts_but_not_old_ack_or_native_state(env):
    dao, reg, backend, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "retained-facts", "tools": []}, ctx)
    tid = first["task"]["taskId"]
    saved = await dao.task_model_context(tid)
    state = saved["state"]
    state["messages"].extend([
        {"role": "user", "content": '<agent-control id="old"><instruction>production-untouched</instruction></agent-control>\n这些是 OpenBear 主控制器的结构化干预。先调用 AgentControlAck',
         "_openbear_runtime": {"kind": "agent_control"}},
        {"role": "user", "content": "old-finalize-gate", "_openbear_runtime": {"kind": "agent_completion_gate"}},
        {"role": "assistant", "content": "prior-final", "native_output_items": [{"type": "reasoning", "id": "opaque-old"}]},
    ])
    await dao.db.conn.execute("UPDATE rath_task_model_contexts SET state_json=? WHERE task_uuid=?", (json.dumps(state), tid))
    await dao.db.conn.commit()
    backend.protocol = "responses"
    second = await call(reg, "AgentContinue", {"to": first["agentSession"]["agentId"], "prompt": "new-work", "tools": []}, ctx)
    assert second["status"] == "completed", second
    request = str(backend.calls[-1]["messages"])
    assert "production-untouched" in request and "retained-facts" in request
    assert "<agent-control " not in request and "old-finalize-gate" not in request
    assert "opaque-old" not in request
    events = await dao.events(second["task"]["taskId"], limit=100)
    assert any(event.kind == "agent_native_context_reset" for event in events)


async def test_direct_to_managed_is_new_round_with_preapproval_gate(env, monkeypatch):
    dao, reg, backend, ctx, manager = env
    first = await call(reg, "Agent", {"prompt": "investigation", "tools": ["Read"]}, ctx)
    monkeypatch.setattr(_FakeConfig.rath, "agent_plan_enabled", True)
    monkeypatch.setattr(_FakeConfig.rath, "agent_model_call_limit", 1)

    async def notification(payload):
        pass

    async def wait(*args, **kwargs):
        pass

    managed_ctx = replace(ctx, task_notification=notification, agent_wait=wait, turn_uuid="managed-root", run_root_turn_uuid="managed-root")
    result = await call(reg, "AgentContinue", {"to": first["agentSession"]["agentId"], "prompt": "plan controlled work", "tools": ["Edit"], "planMode": "managed"}, managed_ctx)
    tid = result["task"]["taskId"]
    pending = manager.task(tid)
    if pending:
        await asyncio.wait_for(pending, timeout=3)
    task = await dao.get_task(tid)
    assert task.input["planMode"] == "managed"
    assert (await dao.get_task(first["task"]["taskId"])).input["planMode"] == "direct"
    assert {tool["name"] for tool in backend.calls[-1]["tools"]} == {"AgentPlanSubmit", "AgentControlAck"}
    assert "investigation" in str(backend.calls[-1]["messages"])
    assert task.run_root_turn_uuid == "managed-root"
    await manager.mark_cancelled(tid)


async def test_managed_initial_extra_tools_obey_instance_preset_ceiling(env):
    dao, reg, _, ctx, manager = env
    first = await call(reg, "Agent", {"prompt": "investigate", "tools": ["Read"]}, ctx)
    session = await dao.agent_session(first["agentSession"]["agentId"])
    tid = await dao.create_task(chat_id=123, workflow_uuid=session.workflow_uuid,
                               parent_session_uuid=ctx.session_uuid, agent_session_uuid=session.session_uuid,
                               title="managed next", status="running", input_data={
                                   "planMode": "managed", "presetToolCeiling": ["Read"],
                                   "agentSnapshot": {"toolAllowlist": ["Read"]},
                                   "contextSource": {"taskUuid": session.context_task_uuid, "revision": session.context_revision},
                               })
    plan = sample_plan()
    plan["toolRequests"] = [{"name": "Bash", "reason": "test ceiling", "neededForSteps": ["s1"]}]
    coordinator = manager.plan_coordinator
    submitted = await coordinator.submit_plan(tid, plan, request_id="submit", wait_for_decision=False)
    with pytest.raises(PlanError, match="ceiling"):
        await coordinator.decide(tid, expected_version=submitted["planVersion"], action="approve",
                                 request_id="reject-outside-ceiling", reason="extra tool", granted_tools=["Bash"])
    snapshot = await coordinator.snapshot(tid)
    assert snapshot["state"]["phase"] == "awaiting_plan_decision"


async def test_task_cleanup_keeps_instance_context_until_conversation_deleted(env):
    dao, reg, _, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "context-outlives-record", "tools": ["TaskMemory"]}, ctx)
    sid, tid = first["agentSession"]["agentId"], first["task"]["taskId"]
    item, _ = await TaskMemoryDAO(dao.db).create(conversation_uuid=ctx.session_uuid, scope_type="agent_session",
                                               task_uuid=sid, name="still-owned", body="private")
    await dao.delete_task_records([tid])
    await dao.db.conn.commit()
    assert await dao.get_task(tid) is None
    assert await dao.task_model_context(tid) is not None
    second = await call(reg, "AgentContinue", {"to": sid, "prompt": "continue after history cleanup", "tools": ["TaskMemory"]}, ctx)
    assert second["status"] == "completed", second
    await dao.delete_task_records_for_chat(123)
    await dao.db.conn.commit()
    assert await dao.task_model_context(tid) is None
    cur = await dao.db.conn.execute("SELECT COUNT(*) FROM conversation_task_memories WHERE memory_uuid=?", (item["memoryUuid"],))
    assert (await cur.fetchone())[0] == 0


async def test_startup_migrates_real_legacy_scope_without_losing_uuid_revision_or_index(tmp_path):
    schema = Path("app/db/schema.sql").read_text()
    memory_ddl = re.search(r"CREATE TABLE IF NOT EXISTS conversation_task_memories \(.*?\n\);", schema, re.S).group(0)
    memory_ddl = memory_ddl.replace("'conversation','agent_task','agent_session'", "'conversation','agent_task'")
    memory_ddl = memory_ddl.replace("scope_type IN ('agent_task','agent_session')", "scope_type='agent_task'")
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(memory_ddl)
        conn.execute("CREATE INDEX legacy_memory_name ON conversation_task_memories(name)")
        conn.execute("INSERT INTO conversation_task_memories(memory_uuid,conversation_uuid,scope_type,task_uuid,name,body,revision) VALUES('mem_old','c','agent_task','t','fact','original',7)")
    db = DB(path)
    await db.connect()
    try:
        row = await (await db.conn.execute("SELECT body,revision FROM conversation_task_memories WHERE memory_uuid='mem_old'")).fetchone()
        assert tuple(row) == ("original", 7)
        assert await (await db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_memory_name'")).fetchone()
        item, _ = await TaskMemoryDAO(db).create(conversation_uuid="c", scope_type="agent_session", task_uuid="a", name="new", body="supported")
        assert item["agentSessionUuid"] == "a"
    finally:
        await db.close()


async def test_instance_web_endpoint_separates_presets_and_never_exposes_checkpoint(web_env):
    dao = web_env.server.rath_dao
    wf = await dao.workflow_by_slug("single-agent")
    kwargs = dict(openbear_session_uuid="conversation-instance", chat_id=123, workflow_uuid=wf.workflow_uuid, agent_key="same")
    a = await dao.create_agent_instance(**kwargs)
    b = await dao.create_agent_instance(**kwargs)
    tids = []
    source = {}
    for index in (1, 2):
        tid = await dao.create_task(chat_id=123, workflow_uuid=wf.workflow_uuid, title=f"round-{index}",
                                   parent_session_uuid="conversation-instance", agent_session_uuid=a.session_uuid,
                                   input_data={"contextSource": source, "agentSnapshot": {"toolAllowlist": ["Read"]}})
        revision = await dao.save_task_model_context(tid, protocol="chat", model="gpt", session_id="private",
                                                     state={"stage": "completed", "messages": [{"role": "user", "content": "PRIVATE-MODEL-CONTEXT"}]})
        await dao.update_task(tid, status="completed", output={"summary": f"round-{index}"})
        source = {"taskUuid": tid, "revision": revision}
        tids.append(tid)
    await dao.create_task(chat_id=123, workflow_uuid=wf.workflow_uuid, title="other-instance",
                          parent_session_uuid="conversation-instance", agent_session_uuid=b.session_uuid)
    url = f"/api/conversations/conversation-instance/agents/{tids[0]}/instance"
    response = await web_env.client.get(url, cookies=web_env.cookie)
    data = await response.json()
    assert response.status == 200, data
    assert [task["taskId"] for task in data["tasks"]] == tids
    assert data["total"] == 2 and data["agentSession"]["canContinue"]
    assert "PRIVATE-MODEL-CONTEXT" not in json.dumps(data)
    wrong = await web_env.client.get(url.replace("conversation-instance", "wrong-conversation"), cookies=web_env.cookie)
    assert wrong.status == 404


async def test_compaction_rebuilds_same_instance_memory_and_reports_retained_summary(env, monkeypatch):
    from app.tools.agents import AgentTools
    from tests.test_rath_single_agent import _GOOD_COMPACTION_SUMMARY
    dao, reg, backend, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "investigated-first", "tools": ["TaskMemory"]}, ctx)
    sid = first["agentSession"]["agentId"]
    item, _ = await TaskMemoryDAO(dao.db).create(conversation_uuid=ctx.session_uuid, scope_type="agent_session",
                                               task_uuid=sid, name="persisted-before-compaction", body="exact-fact")

    class Compressor:
        protocol = "chat"
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            return AgentResult(text=_GOOD_COMPACTION_SUMMARY + "\n已有调查结论 retained-after-compression；当前仅进行只读复查。")

    compressor = Compressor()
    monkeypatch.setattr(AgentTools, "_context_compact_kwargs", lambda self, model: {
        "context_compact_trigger_tokens": 1000, "context_compact_backend": compressor,
        "context_compact_model": "summary", "context_compact_keep_recent": 1,
    })
    second = await call(reg, "AgentContinue", {"to": sid, "prompt": "只读复查。" + "已提供材料与保留约束。" * 1000,
                                               "tools": ["TaskMemory"]}, ctx)
    assert second["status"] == "completed", second
    assert compressor.calls > 0
    request = str(backend.calls[-1]["messages"])
    assert "retained-after-compression" in request and item["memoryUuid"] in request
    memory_ctx = ToolRuntimeContext(chat_id=123, session_uuid=ctx.session_uuid, source="agent:general-purpose",
                                    task_uuid=second["task"]["taskId"], agent_session_uuid=sid)
    assert (await call(reg, "TaskMemory", {"action": "get", "memoryUuid": item["memoryUuid"]}, memory_ctx))["memory"]["body"] == "exact-fact"
    info = await call(reg, "AgentInfo", {"action": "get", "to": sid}, ctx)
    assert info["context"]["compacted"] is True
    assert item["memoryUuid"] in info["memoryCatalog"]


@pytest.mark.parametrize("clean_task_history", [False, True])
async def test_duplicate_conversation_preserves_instance_head_and_rewrites_private_references(web_env, clean_task_history):
    dao = web_env.server.rath_dao
    source = await web_env.server._create_web_conversation(123, title="instance-copy")
    chat_id, conversation = int(source["internal_chat_id"]), source["conversation_uuid"]
    wf = await dao.workflow_by_slug("single-agent")
    session = await dao.create_agent_instance(openbear_session_uuid=conversation, chat_id=chat_id,
                                              workflow_uuid=wf.workflow_uuid, agent_key="same")
    tid = await dao.create_task(chat_id=chat_id, workflow_uuid=wf.workflow_uuid, parent_session_uuid=conversation,
                               title="source task", agent_session_uuid=session.session_uuid)
    item, _ = await TaskMemoryDAO(web_env.db).create(conversation_uuid=conversation, scope_type="agent_session",
                                                    task_uuid=session.session_uuid, name="private", body="original-fact")
    await dao.save_task_model_context(tid, protocol="chat", model="gpt", session_id="old-native",
                                      state={"stage": "completed", "messages": [{"role": "user", "content": item["memoryUuid"]}]})
    await dao.update_task(tid, status="completed")
    if clean_task_history:
        await dao.delete_task_records([tid])
        await web_env.db.conn.commit()
    response = await web_env.client.post(f"/api/conversations/{conversation}/duplicate", cookies=web_env.cookie)
    copied = await response.json()
    assert response.status == 200, copied
    new_conversation = copied["conversation"]["conversationUuid"]
    sessions = await dao.list_agent_sessions(openbear_session_uuid=new_conversation)
    assert len(sessions) == 1
    copied_session = sessions[0]
    assert copied_session.session_kind == "independent"
    assert copied_session.context_task_uuid and copied_session.context_task_uuid != tid
    context = await dao.task_model_context(copied_session.context_task_uuid)
    memories = await TaskMemoryDAO(web_env.db).list(conversation_uuid=new_conversation, scope_type="agent_session",
                                                   task_uuid=copied_session.session_uuid)
    assert memories["total"] == 1
    new_memory_uuid = memories["items"][0]["memoryUuid"]
    assert new_memory_uuid != item["memoryUuid"]
    assert new_memory_uuid in str(context["state"]["messages"])
    assert context["sessionId"] == ""
