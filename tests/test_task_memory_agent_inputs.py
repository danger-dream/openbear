"""Agent input/grant boundaries with temporary stores and recording fake models."""
from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from app.context.strategies import ModelSummaryStrategy
from app.context.window import WindowPolicy
from app.llm.base import AgentResult
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.task_memory import (
    SCOPE_AGENT_SESSION,
    SCOPE_AGENT_TASK,
    SCOPE_CONVERSATION,
    TaskMemoryDAO,
    is_task_memory_runtime_message,
)
from app.tools.base import ToolRegistry
from app.tools.task_memory import register_task_memory_tool
from tests.test_agent_continuity import call
from tests.test_agent_continuity import env as shared_continuity_env
from tests.test_context_strategies import env as shared_strategy_env
from tests.test_context_window import batch
from tests.test_rath_single_agent import env as shared_runner_env

continuity_env = shared_continuity_env
strategy_env = shared_strategy_env
runner_env = shared_runner_env


def states(messages):
    return [m for m in messages if is_task_memory_runtime_message(m)]


async def test_actual_instance_continue_grants_shared_input_and_private_isolation(continuity_env):
    dao, reg, backend, ctx, _ = continuity_env
    memories = TaskMemoryDAO(dao.db)
    await memories.create(conversation_uuid=ctx.session_uuid, scope_type=SCOPE_CONVERSATION,
                          name="Shared preference", body="Keep the macOS appearance.", visible_to_agents=True)
    await memories.create(conversation_uuid=ctx.session_uuid, scope_type=SCOPE_CONVERSATION,
                          name="Long reference", body="LONG-BODY-MUST-BE-REQUESTED-" * 30, visible_to_agents=True)
    await memories.create(conversation_uuid=ctx.session_uuid, scope_type=SCOPE_CONVERSATION,
                          name="Hidden reference", body="HIDDEN-CONTROLLER-ONLY", visible_to_agents=False)
    first = await call(reg, "Agent", {"prompt": "first", "tools": []}, ctx)
    assert first["status"] == "completed"
    a = first["agentSession"]["sessionUuid"]
    first_call = backend.calls[-1]
    assert "TaskMemory" not in {s["name"] for s in first_call["tools"] or []}
    assert "<body>Keep the macOS appearance.</body>" in str(first_call["messages"])
    assert "Long reference" in str(first_call["messages"])
    assert "LONG-BODY-MUST-BE-REQUESTED-" not in str(first_call["messages"])
    assert "主控提供必要原文" in str(first_call["messages"])
    assert "HIDDEN-CONTROLLER-ONLY" not in str(first_call["messages"])

    await memories.create(conversation_uuid=ctx.session_uuid, scope_type=SCOPE_AGENT_SESSION,
                          task_uuid=a, name="Only instance A", body="INSTANCE-A-PRIVATE-PREFERENCE")
    second = await call(reg, "Agent", {"prompt": "second", "tools": ["TaskMemory"]}, ctx)
    b = second["agentSession"]["sessionUuid"]
    assert b != a
    assert "INSTANCE-A-PRIVATE-PREFERENCE" not in str(backend.calls[-1]["messages"])
    await memories.create(conversation_uuid=ctx.session_uuid, scope_type=SCOPE_AGENT_SESSION,
                          task_uuid=b, name="Only instance B", body="INSTANCE-B-PRIVATE-PREFERENCE")
    for instance, grants, own, other in (
        (a, [], "INSTANCE-A-PRIVATE-PREFERENCE", "INSTANCE-B-PRIVATE-PREFERENCE"),
        (a, ["TaskMemory"], "INSTANCE-A-PRIVATE-PREFERENCE", "INSTANCE-B-PRIVATE-PREFERENCE"),
        (b, ["TaskMemory"], "INSTANCE-B-PRIVATE-PREFERENCE", "INSTANCE-A-PRIVATE-PREFERENCE"),
        (a, [], "INSTANCE-A-PRIVATE-PREFERENCE", "INSTANCE-B-PRIVATE-PREFERENCE"),
    ):
        result = await call(reg, "AgentContinue", {"to": instance, "prompt": "next", "tools": grants}, ctx)
        assert result["status"] == "completed", result
        request = backend.calls[-1]
        text = str(request["messages"])
        assert "<body>Keep the macOS appearance.</body>" in text
        assert (own in text) is bool(grants)
        assert other not in text and "HIDDEN-CONTROLLER-ONLY" not in text
        assert "LONG-BODY-MUST-BE-REQUESTED-" not in text
        assert len(states(request["messages"])) == 1
        note = str(states(request["messages"])[0]["content"])
        assert ("使用 TaskMemory list/search" in note) is bool(grants)
        assert ("主控提供必要原文" in note) is not bool(grants)


class RecordingBackend:
    protocol = "chat"

    def __init__(self):
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        return AgentResult(text="done")


async def test_managed_phase_gate_uses_actual_schemas_and_appends_only(runner_env):
    dao, task_uuid, agent = runner_env
    memories = TaskMemoryDAO(dao.db)
    for scope, name, body in ((SCOPE_CONVERSATION, "shared", "SHARED-INPUT"),
                              (SCOPE_AGENT_TASK, "private", "PRIVATE-INPUT")):
        await memories.create(conversation_uuid="session-1", scope_type=scope,
                              task_uuid=task_uuid if scope == SCOPE_AGENT_TASK else "",
                              name=name, body=body, visible_to_agents=True)
    registry = ToolRegistry()
    register_task_memory_tool(registry, memories)
    backend = RecordingBackend()
    runner = SingleAgentWorkflowRunner(dao, task_uuid, agent=replace(agent, tool_allowlist=["TaskMemory"]),
                                       backend=backend, model="gpt", max_tokens=2048,
                                       tools=registry, plan_protocol_enabled=True)
    # Isolate the actual physical request path without pretending a fake Plan was
    # approved by the model. The temporary durable gate is controlled by the test.
    runner.conversation_uuid = runner.openbear_session_uuid = "session-1"
    runner.chat_id = 123
    messages = [{"role": "user", "content": "bounded request"}]
    schemas = await runner._allowed_tool_schemas()
    assert "TaskMemory" not in {s["name"] for s in schemas}
    await runner._call_model(messages, schemas)
    first = backend.calls[-1][0]
    assert "SHARED-INPUT" in str(first) and "PRIVATE-INPUT" not in str(first)
    await dao.db.conn.execute(
        """INSERT INTO rath_task_plan_state
           (task_uuid, phase, active_plan_version, approved_tools_json, row_revision, updated_at)
           VALUES (?, 'executing', 1, '["TaskMemory"]', 1, 1)""", (task_uuid,),
    )
    await dao.db.conn.commit()
    schemas = await runner._allowed_tool_schemas()
    assert "TaskMemory" in {s["name"] for s in schemas}
    await runner._call_model(messages, schemas)
    second = backend.calls[-1][0]
    assert second[:len(first)] == first
    assert len(states(second)) == 2
    assert "PRIVATE-INPUT" in states(second)[-1]["content"]


@pytest.mark.parametrize("granted", [False, True])
@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_actual_agent_windows_reinject_latest_complete_notes(runner_env, strategy_env, strategy, granted):
    dao, task_uuid, agent = runner_env
    memories = TaskMemoryDAO(dao.db)
    shared, _ = await memories.create(conversation_uuid="session-1", scope_type=SCOPE_CONVERSATION,
                                      name="shared", body="old-shared-body", visible_to_agents=True)
    await memories.create(conversation_uuid="session-1", scope_type=SCOPE_AGENT_TASK,
                          task_uuid=task_uuid, name="private", body="OWN-PRIVATE-BODY")
    registry = ToolRegistry()
    register_task_memory_tool(registry, memories)
    backend = RecordingBackend()
    runner = SingleAgentWorkflowRunner(dao, task_uuid,
                                       agent=replace(agent, tool_allowlist=["TaskMemory"] if granted else []),
                                       backend=backend, model="gpt", max_tokens=2048,
                                       tools=registry, plan_protocol_enabled=False)
    assert (await runner.run())["summary"] == "done"
    first = backend.calls[-1][0]
    assert "<body>old-shared-body</body>" in str(first)
    assert ("OWN-PRIVATE-BODY" in str(first)) is granted
    window = runner._get_window_runtime()
    window.policy = WindowPolicy(128000, trigger_tokens=8000)

    async def selected_strategy():
        return strategy

    window.strategy_resolver = selected_strategy
    window.strategies["model_summary"] = ModelSummaryStrategy(strategy_env.cfg, strategy_env.factory, "p/main")
    messages = copy.deepcopy(first)
    for index in range(2):
        body = f"macOS appearance preference, revision {index}."
        shared = await memories.update(shared["memoryUuid"], conversation_uuid="session-1", scope_type=SCOPE_CONVERSATION,
                                       expected_revision=shared["revision"], changes={"body": body})
        messages += batch(80 + index, text="evictable original tool result " * 1000) + batch(90 + index) + batch(100 + index)
        schemas = await runner._allowed_tool_schemas()
        assert await runner._prepare_context_window(messages, schemas, force=True)
        await runner._call_model(messages, schemas)
        sent = backend.calls[-1][0]
        assert len(states(sent)) == 1
        assert f"<body>{body}</body>" in states(sent)[0]["content"]
        assert "old-shared-body" not in str(sent)
        assert ("OWN-PRIVATE-BODY" in str(sent)) is granted
        assert window.active_strategy == strategy
