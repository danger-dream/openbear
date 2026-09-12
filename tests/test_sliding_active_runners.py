"""Recording-model coverage of active source identity at real runner boundaries."""
from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from app.agent import steering
from app.context.runtime import ContextManager
from app.context.window import WindowPolicy, source_of
from app.db.dao import MessageDAO
from app.llm.base import AgentResult
from app.llm.events import StreamEvent, ToolCall
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.task_memory import TaskMemoryDAO, is_task_memory_runtime_message
from app.tools.base import ToolRegistry
from app.web_admin import _WebLiveStream, _WebStreamRenderer
from tests.test_agent_continuity import call
from tests.test_agent_continuity import env as shared_continuity_env
from tests.test_rath_single_agent import env as shared_agent_env
from tests.test_web_admin import FakeRunFactory
from tests.test_web_admin import web_env as shared_web_env

continuity_env = shared_continuity_env
agent_env = shared_agent_env
web_env = shared_web_env


async def test_web_current_root_steering_and_notification_resume_preserve_bounded_proposal(web_env, monkeypatch):
    env = web_env
    row = await env.server._create_web_conversation(123)
    chat, uuid = row["internal_chat_id"], row["conversation_uuid"]
    dao = MessageDAO(env.db)
    for index in range(20):
        await dao.add(chat, "user", f"OLD INPUT {index}: " + "old material " * 200,
                      turn_uuid=f"old-{index}", run_root_turn_uuid=f"old-{index}")
        await dao.add(chat, "assistant", f"OLD PROPOSAL {index}", turn_uuid=f"old-{index}", run_root_turn_uuid=f"old-{index}")
    await dao.add(chat, "assistant", "CURRENT PROPOSAL: change only the agreed files; do not deploy.",
                  turn_uuid="predecessor", run_root_turn_uuid="predecessor")
    await TaskMemoryDAO(env.db).create(conversation_uuid=uuid, scope_type="conversation",
                                      name="Style", body="Keep macOS style.")
    requests = []
    executions = []
    registry = ToolRegistry()

    async def read(args):
        executions.append(len(executions) + 1)
        if len(executions) == 1:
            steering.enqueue(chat, "CURRENT FEEDBACK: keep the font size.", source="web", turnUuid="feedback-turn")
        return "optional current execution output " * 180

    registry.add("Read", "test read", {"type": "object", "properties": {}}, read)
    env.server.tools = registry
    env.server.config.agent.max_retries = 0

    class Backend:
        protocol = "chat"

        async def stream(self, messages, **kwargs):
            requests.append(copy.deepcopy(messages))
            text = str(messages)
            assert "CURRENT PROPOSAL: change only the agreed files" in text
            assert "好，改吧" in text
            assert "OLD INPUT 0:" not in text
            assert "Keep macOS style." in text
            assert len([m for m in messages if is_task_memory_runtime_message(m)]) == 1
            if len(requests) > 1:
                assert "CURRENT FEEDBACK" in text
                feedback = next(m for m in messages if m.get("content") == "CURRENT FEEDBACK: keep the font size.")
                assert source_of(feedback)["run_root_turn_uuid"] == "active-root"
            if len(requests) <= 4:
                yield StreamEvent(kind="content", text="CURRENT QUESTION: preserve the same UI?")
                yield StreamEvent(kind="tool_call", tool_calls=[ToolCall(f"read-{len(requests)}", "Read", "{}")])
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
            else:
                yield StreamEvent(kind="content", text="Completed without deployment.")
                yield StreamEvent(kind="finish", finish_reason="stop")

    backend = Backend()
    env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    env.server.config.models.providers["openai"].models[0].rollover_trigger_tokens = 8000

    async def system():
        return "Stable test policy"

    monkeypatch.setattr(env.server, "_build_system_prompt_for_chat", system)
    prepare = ContextManager.prepare

    async def boundary(self, messages, **kwargs):
        assert self.active_run_root_turn_uuid == "active-root"
        kwargs["force"] = True
        return await prepare(self, messages, **kwargs)

    monkeypatch.setattr(ContextManager, "prepare", boundary)
    live = _WebLiveStream(uuid, chat)
    await live.publish({"type": "accepted", "turnUuid": "active-root"})
    try:
        assert await env.server._run_web_turn(chat, "好，改吧", _WebStreamRenderer(live), conversation=row,
                                              root_turn_uuid="active-root") is True
        assert executions == [1, 2, 3, 4]
        assert len(requests) == 5
        # A notification continuation has no newly invented human task. The
        # current root's original brief and feedback remain the required inputs.
        await live.publish({"type": "accepted", "turnUuid": "active-root"})
        assert await env.server._run_web_turn(chat, "Agent result is ready", _WebStreamRenderer(live), conversation=row,
                                              root_turn_uuid="active-root", task_notification=True,
                                              task_notification_payload={"content": "Agent result is ready"}) is True
        assert len(requests) == 6 and executions == [1, 2, 3, 4]
    finally:
        steering.clear(chat)


async def test_actual_agent_continue_new_task_evicts_old_inputs_not_current_brief(continuity_env, monkeypatch):
    dao, reg, backend, ctx, _ = continuity_env
    prepare = ContextManager.prepare

    async def force_window(self, messages, **kwargs):
        self.policy = WindowPolicy(128000, trigger_tokens=16000)
        kwargs["force"] = True
        return await prepare(self, messages, **kwargs)

    monkeypatch.setattr(ContextManager, "prepare", force_window)
    first = await call(reg, "Agent", {"prompt": "OLD COMPLETE BRIEF " + "prior standalone task " * 550, "tools": []}, ctx)
    assert first["status"] == "completed", first
    task1 = first["task"]["taskUuid"]
    instance = first["agentSession"]["sessionUuid"]
    second = await call(reg, "AgentContinue", {"to": instance, "prompt": "NEW COMPLETE BRIEF: edit only the specified source; no deployment.", "tools": []}, ctx)
    assert second["status"] == "completed", second
    task2 = second["task"]["taskUuid"]
    assert task1 != task2
    request = backend.calls[-1]["messages"]
    assert "NEW COMPLETE BRIEF" in str(request)
    assert "OLD COMPLETE BRIEF" not in str(request)
    current = next(m for m in request if "NEW COMPLETE BRIEF" in str(m.get("content")))
    assert source_of(current)["task_uuid"] == task2
    old_context = await dao.task_model_context(task1)
    assert old_context and "OLD COMPLETE BRIEF" in str(old_context)


async def test_agent_same_task_pause_resume_pins_instruction_and_current_controls(agent_env, monkeypatch):
    dao, task_uuid, agent = agent_env
    registry = ToolRegistry()
    executed = []

    async def read(args):
        executed.append(1)
        return "read once"

    registry.add("Read", "test", {"type": "object", "properties": {}}, read)

    class Backend:
        protocol = "chat"

        def __init__(self):
            self.calls = []

        async def complete(self, messages, **kwargs):
            self.calls.append(copy.deepcopy(messages))
            if len(self.calls) == 1:
                return AgentResult(tool_calls=[ToolCall("once", "Read", "{}")])
            return AgentResult(text="continued")

    backend = Backend()
    runner = SingleAgentWorkflowRunner(dao, task_uuid, agent=replace(agent, tool_allowlist=["Read"]),
                                       backend=backend, model="gpt", max_tokens=1024, tools=registry,
                                       model_call_limit=1, context_window=128000, rollover_trigger_tokens=8000,
                                       plan_protocol_enabled=False)
    output = await runner.run()
    assert output["status"] == "needs_openbear_control"
    runner.model_call_limit = 3
    prepare = ContextManager.prepare

    async def boundary(self, messages, **kwargs):
        kwargs["force"] = True
        return await prepare(self, messages, **kwargs)

    monkeypatch.setattr(ContextManager, "prepare", boundary)
    result = await runner.run_continue("CURRENT CONTROL: finish the remaining verification only.")
    assert result["summary"] == "continued"
    assert executed == [1]
    last = backend.calls[-1]
    instruction = next(m for m in last if "读取 README" in str(m.get("content")))
    feedback = next(m for m in last if "CURRENT CONTROL" in str(m.get("content")))
    assert source_of(instruction)["task_uuid"] == task_uuid
    assert source_of(feedback)["task_uuid"] == task_uuid and source_of(feedback)["kind"] == "control"
