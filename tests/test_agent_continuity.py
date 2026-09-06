from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.db.engine import DB
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.continuity import AgentContinuityError
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.task_memory import TaskMemoryDAO, task_memory_catalog_snapshot
from app.tools.agents import register_agent_tools
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.task_memory import register_task_memory_tool
from tests.test_tools_agent_orchestration import _FakeConfig, _FakeFactory, _FakeSelection, _RecordingBackend


@pytest.fixture
async def env(tmp_path):
    db = DB(str(tmp_path / "continuity.db"))
    await db.connect()
    dao = RathDAO(db)
    await ensure_builtin_workflows(dao)
    registry = ToolRegistry()
    backend = _RecordingBackend()
    manager = RathTaskManager(dao)
    register_task_memory_tool(registry, TaskMemoryDAO(db))
    async def noop(args):
        return "authorized"
    for name in ("Read", "Write", "Edit", "EditBatch", "Bash"):
        registry.add(name, name, {"type": "object", "properties": {}}, noop)
    register_agent_tools(registry, config=_FakeConfig(), dao=dao, manager=manager,
                         llm_factory=_FakeFactory(backend), model_selection=_FakeSelection(), workspace_dir=str(tmp_path))
    context = ToolRuntimeContext(chat_id=123, session_uuid="conversation-a", conversation_uuid="conversation-a",
                                 source="web", turn_uuid="turn-1", run_root_turn_uuid="turn-1")
    try:
        yield dao, registry, backend, context, manager
    finally:
        await db.close()


async def call(reg, name, args, ctx):
    return json.loads(await reg.dispatch(name, json.dumps(args), context=ctx))


async def test_completed_round_continues_same_instance_actual_context_new_tools_and_turn(env):
    dao, reg, backend, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "调查标记：already-understood", "tools": ["Read", "TaskMemory"]}, ctx)
    assert first["status"] == "completed", first
    a = first["agentSession"]["sessionUuid"]
    t1 = first["task"]["taskUuid"]
    assert first["agentSession"]["canContinue"]
    memory_ctx = ToolRuntimeContext(chat_id=123, session_uuid=ctx.session_uuid, conversation_uuid=ctx.session_uuid,
                                    source="agent:general-purpose", task_uuid=t1, agent_session_uuid=a)
    memory = await call(reg, "TaskMemory", {"action": "create", "name": "confirmed", "body": "keep-me", "idempotencyKey": "confirmed-1"}, memory_ctx)
    assert memory["memory"]["agentSessionUuid"] == a
    next_ctx = ToolRuntimeContext(chat_id=123, session_uuid=ctx.session_uuid, conversation_uuid=ctx.session_uuid,
                                  source="web", turn_uuid="turn-2", run_root_turn_uuid="turn-2")
    second = await call(reg, "AgentContinue", {"to": a, "prompt": "现在修改并自测；保留约定边界", "tools": ["Edit", "TaskMemory"], "requestId": "next-1"}, next_ctx)
    assert second["status"] == "completed", second
    t2 = second["task"]["taskUuid"]
    assert second["agentSession"]["sessionUuid"] == a and t2 != t1
    assert second["task"]["sessionTurn"] == 2
    assert second["task"]["runRootTurnUuid"] == "turn-2"
    assert (await dao.get_task(t1)).status == "completed"
    assert (await dao.get_task(t1)).output == first["result"]
    request = backend.calls[-1]
    assert "already-understood" in str(request["messages"])
    assert "输出 1" in str(request["messages"])
    names = {item["name"] for item in request["tools"]}
    assert {"Edit", "EditBatch", "TaskMemory"} <= names and "Read" not in names
    assert backend.calls[0]["session_id"] == request["session_id"]
    memory_ctx.task_uuid = t2
    read = await call(reg, "TaskMemory", {"action": "get", "memoryUuid": memory["memory"]["memoryUuid"]}, memory_ctx)
    assert read["memory"]["body"] == "keep-me"
    snapshot = await task_memory_catalog_snapshot(TaskMemoryDAO(dao.db), conversation_uuid=ctx.session_uuid, task_uuid=t2, for_agent=True)
    assert "confirmed" in str(snapshot)
    replay = await call(reg, "AgentContinue", {"to": a, "prompt": "现在修改并自测；保留约定边界", "tools": ["Edit", "TaskMemory"], "requestId": "next-1"}, next_ctx)
    assert replay["replayed"] and replay["taskUuid"] == t2
    assert len(backend.calls) == 2


async def test_same_preset_isolated_and_no_cross_scope_tools(env):
    dao, reg, backend, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "A-only", "tools": ["TaskMemory"]}, ctx)
    second = await call(reg, "Agent", {"prompt": "B-only", "tools": ["TaskMemory"]}, ctx)
    assert first["agentSession"]["sessionUuid"] != second["agentSession"]["sessionUuid"]
    assert "A-only" not in str(backend.calls[-1]["messages"])
    bad = ToolRuntimeContext(chat_id=456, session_uuid="other", source="web")
    refused = await call(reg, "AgentContinue", {"to": first["agentSession"]["sessionUuid"], "prompt": "wrong", "tools": []}, bad)
    assert not refused["ok"]
    for tool in ("AgentContinue", "AgentInfo"):
        denied = await call(reg, tool, {"action": "list", "to": first["task"]["taskUuid"], "prompt": "nested", "tools": []},
                            ToolRuntimeContext(chat_id=123, session_uuid=ctx.session_uuid, source="agent:general-purpose"))
        assert not denied["ok"]


async def test_instance_cas_single_owner_and_stale_checkpoint_rejected(env):
    dao, reg, _, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "first", "tools": []}, ctx)
    sid = first["agentSession"]["sessionUuid"]
    session = await dao.agent_session(sid)
    source = {"taskUuid": session.context_task_uuid, "revision": session.context_revision}
    kwargs = dict(chat_id=123, workflow_uuid=session.workflow_uuid, parent_session_uuid=ctx.session_uuid,
                  agent_session_uuid=sid, title="next", input_data={"contextSource": source})
    results = await asyncio.gather(dao.create_task(**kwargs), dao.create_task(**kwargs), return_exceptions=True)
    assert sum(isinstance(item, str) for item in results) == 1
    assert any(isinstance(item, AgentContinuityError) and item.code == "agent_instance_busy" for item in results)
    with pytest.raises(RuntimeError, match="stale runner"):
        await dao.save_task_model_context(first["task"]["taskUuid"], protocol="chat", model="gpt", session_id="old", state={})


async def test_old_pause_artifact_cannot_override_completed_checkpoint(env):
    from app.rath.single_agent import SingleAgentWorkflowRunner
    dao, reg, _, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "latest", "tools": []}, ctx)
    tid = first["task"]["taskUuid"]
    await dao.create_artifact(tid, kind="agent_continuation_state", name="old pause", content='{"roundNo":0,"messages":[],"kind":"old"}')
    runner = object.__new__(SingleAgentWorkflowRunner)
    runner.dao, runner.task_uuid = dao, tid
    state = await runner._latest_continuation_state()
    assert state["stage"] == "completed"
    assert "latest" in str(state["messages"])


async def test_legacy_adoption_preserves_only_selected_task_and_memory_uuid(env):
    dao, reg, _, ctx, _ = env
    wf = await ensure_builtin_workflows(dao)
    legacy = await dao.get_or_create_agent_session(openbear_session_uuid=ctx.session_uuid, chat_id=123,
                                                   workflow_uuid=wf, agent_key="general-purpose")
    tid = await dao.create_task(chat_id=123, workflow_uuid=wf, title="old", parent_session_uuid=ctx.session_uuid,
                                agent_session_uuid=legacy.session_uuid, input_data={"agentSnapshot": {"agentKey": "general-purpose"}})
    await dao.update_task(tid, status="running")
    await dao.save_task_model_context(tid, protocol="chat", model="gpt", session_id="legacy-transport",
                                      state={"stage": "completed", "messages": [{"role": "user", "content": "legacy-only"}], "sessionId": "legacy-transport"})
    await dao.update_task(tid, status="completed")
    mem, _ = await TaskMemoryDAO(dao.db).create(conversation_uuid=ctx.session_uuid, scope_type="agent_task", task_uuid=tid,
                                              name="legacy-fact", body="private")
    refusal = await call(reg, "AgentContinue", {"to": legacy.session_uuid, "prompt": "no group merge", "tools": []}, ctx)
    assert refusal["error"] == "legacy_group_requires_explicit_task"
    adopted = await dao.adopt_legacy_agent_task(tid)
    assert adopted.session_kind == "independent" and adopted.session_uuid != legacy.session_uuid
    item = await TaskMemoryDAO(dao.db).get(mem["memoryUuid"], conversation_uuid=ctx.session_uuid, scope_type="agent_session", task_uuid=adopted.session_uuid)
    assert item["body"] == "private"
    assert (await dao.agent_session(legacy.session_uuid)).status == "active"


async def test_schema_reconnect_does_not_merge_independent_instances(tmp_path):
    path = str(tmp_path / "reconnect.db")
    db = DB(path)
    await db.connect()
    dao = RathDAO(db)
    args = dict(openbear_session_uuid="c", chat_id=1, workflow_uuid="w", agent_key="same")
    one = await dao.create_agent_instance(**args)
    two = await dao.create_agent_instance(**args)
    await db.close()
    await db.connect()
    assert (await dao.agent_session(one.session_uuid)).status == "active"
    assert (await dao.agent_session(two.session_uuid)).status == "active"
    await db.close()
