from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import replace

import pytest

from app.agent.compaction import CompressionCandidate
from app.agent.transcript_repair import repair_role_alternation
from app.db.engine import DB
from app.llm.base import AgentResult, OpenBearLLMError
from app.llm.events import StreamEvent, ToolCall, Usage
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.dao import RathDAO
from app.rath.schemas import RathAgentDef
from app.rath.single_agent import (
    SingleAgentWorkflowRunner,
    agent_to_snapshot,
    safe_agent_llm_session_id,
)
from app.task_memory import (
    SCOPE_AGENT_TASK,
    SCOPE_CONVERSATION,
    TaskMemoryDAO,
    is_task_memory_runtime_message,
)
from app.tools.base import ToolRegistry
from app.tools.task_memory import register_task_memory_tool


@pytest.fixture
async def env(tmp_path):
    db = DB(str(tmp_path / "single-agent.db"))
    await db.connect()
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    agent = RathAgentDef(
        workflow_uuid=workflow_uuid,
        agent_key="code-reader",
        name="代码阅读员",
        description="读取代码并总结",
        system_prompt="你是代码阅读员",
        model="openai/gpt",
        think_level="high",
        tool_allowlist=["Read"],
        enabled=True,
        id=7,
    )
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="阅读项目",
        input_data={"instruction": "读取 README"},
        parent_session_uuid="session-1",
    )
    try:
        yield dao, task_uuid, agent
    finally:
        await db.close()


class _FlakyBackend:
    protocol = "chat"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict]] = []
        self.seen_systems: list[str] = []
        self.seen_tools: list[list[dict]] = []

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        self.seen_messages.append(copy.deepcopy(messages))
        self.seen_systems.append(system)
        self.seen_tools.append(copy.deepcopy(tools or []))
        self.calls += 1
        if self.calls == 1:
            raise OpenBearLLMError("temporary upstream error", status=503, retryable=True)
        return AgentResult(text="重试后成功", usage=Usage(input_tokens=1, output_tokens=1))


class _Backend:
    protocol = "chat"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        self.calls += 1
        usage = Usage(
            input_tokens=12,
            output_tokens=6,
            cache_read_tokens=2,
            cache_write_tokens=1,
            total_tokens=21,
        )
        if self.calls == 1 and tools:
            return AgentResult(
                tool_calls=[ToolCall(id="read", name="Read", arguments='{"path":"README.md"}')],
                usage=usage,
                finish_reason="tool_calls",
            )
        tool_content = "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "tool"
        )
        return AgentResult(text=f"结论：已读取。{tool_content}", usage=usage, finish_reason="stop")


_GOOD_COMPACTION_SUMMARY = (
    "## Primary Request and Intent\n- Test Agent compaction.\n"
    "## Key Technical Concepts\n- Context compaction.\n"
    "## Files and Code Sections\n- None\n"
    "## Errors and Fixes\n- None\n"
    "## Problem Solving\n- None\n"
    "## All User Messages\n- None\n"
    "## Pending Tasks\n- None\n"
    "## Current Work\n- None\n"
    "## Optional Next Step\n- None\n"
    "## Critical Identifiers\n- None\n"
)


class _MultiStepBackend:
    protocol = "chat"

    def __init__(self) -> None:
        self.tools_seen: list[bool] = []

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        self.tools_seen.append(bool(tools))
        usage = Usage(input_tokens=3, output_tokens=2)
        tool_count = sum(1 for m in messages if m.get("role") == "tool")
        if tool_count == 0:
            return AgentResult(
                tool_calls=[ToolCall(id="read1", name="Read", arguments='{"path":"a.md"}')],
                usage=usage,
                finish_reason="tool_calls",
            )
        if tool_count == 1:
            if not tools:
                return AgentResult(text="第二轮工具 schema 丢失", usage=usage, finish_reason="stop")
            return AgentResult(
                tool_calls=[ToolCall(id="read2", name="Read", arguments='{"path":"b.md"}')],
                usage=usage,
                finish_reason="tool_calls",
            )
        return AgentResult(text="结论：连续读取完成", usage=usage, finish_reason="stop")


async def test_single_agent_runner_prepends_base_system_prompt(env):
    dao, task_uuid, agent = env

    class CaptureSystemBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.systems: list[str] = []

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.systems.append(system)
            return AgentResult(text="ok", usage=Usage(input_tokens=1, output_tokens=1))

    backend = CaptureSystemBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        base_system_prompt="基础规则\n基础记忆",
    )

    output = await runner.run()

    assert output["summary"] == "ok"
    assert backend.systems == ["基础规则\n基础记忆\n\n你是代码阅读员"]


async def test_single_agent_runner_forwards_fast_request_to_every_model_call(env):
    dao, task_uuid, agent = env

    class CaptureFastBackend:
        protocol = "responses"

        def __init__(self) -> None:
            self.opts: list[dict] = []

        async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.opts.append(copy.deepcopy(opts))
            return AgentResult(
                text="ok",
                usage=Usage(input_tokens=1_000_000, output_tokens=1),
                service_tier="default",
                provider_cost_usd=0.123,
            )

    backend = CaptureFastBackend()
    calls: list[dict] = []
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        fast_request={
            "body": {"service_tier": "priority"},
            "headers": {"x-fast-mode": "enabled"},
        },
        cost={"input": 4, "output": 12},
        base_cost={"input": 2, "output": 6},
        fast_cost={"input": 4, "output": 12},
        fast_requested=True,
        on_model_call=lambda detail: calls.append(detail),
    )

    await runner.run()

    assert backend.opts
    assert backend.opts[0]["fast_request"] == {
        "body": {"service_tier": "priority"},
        "headers": {"x-fast-mode": "enabled"},
    }
    assert calls[0]["serviceTier"] == "default"
    assert calls[0]["providerCostUsd"] == pytest.approx(0.123)
    assert calls[0]["costUsd"] == pytest.approx(0.123)


async def test_plan_system_prompt_restores_inherited_facts_without_marking_new_progress(env):
    dao, task_uuid, agent = env
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        plan_protocol_enabled=True,
    )
    runner._task_instruction = "continue the remaining work"
    runner._plan_runtime = {"phase": "drafting", "activePlanVersion": 0}
    runner._inherited_plan_context = {
        "sourceTask": {"taskUuid": "source-1", "status": "interrupted"},
        "completedSteps": [{"stepId": "s1", "result": "verified"}],
        "evidence": [{"evidenceUuid": "ev-1", "reference": "pytest: passed"}],
    }

    system = runner._system_prompt()
    messages = []
    appended = await runner._append_plan_runtime_update(messages, force_full=True)

    assert appended is True
    assert "source-1" not in system
    assert '"phase":"drafting"' not in system
    assert "<agent-plan-runtime" in messages[-1]["content"]
    assert "source-1" in messages[-1]["content"]
    assert "pytest: passed" in messages[-1]["content"]
    assert "只读" in messages[-1]["content"]
    assert "完整 Plan" in messages[-1]["content"]
    assert runner._system_prompt() == system


async def test_plan_runtime_is_append_only_and_execution_tool_schema_is_frozen(env):
    dao, task_uuid, agent = env
    reg = ToolRegistry()

    async def noop(_args):
        return "ok"

    for name in (
        "Read",
        "Bash",
        "TaskMemory",
        "AgentPlanProgress",
        "AgentPlanReplan",
        "AgentControlAck",
    ):
        reg.add(
            name,
            f"{name} schema",
            {"type": "object", "properties": {"marker": {"type": "string"}}},
            noop,
            visibility={"agent", "runtime"},
        )
    await dao.db.conn.execute(
        """
        INSERT INTO rath_task_plan_state
          (task_uuid, phase, active_plan_version, approved_tools_json, row_revision, updated_at)
        VALUES (?, 'executing', 1, '[\"Read\",\"Bash\",\"TaskMemory\"]', 1, 1)
        """,
        (task_uuid,),
    )
    await dao.db.conn.commit()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=reg,
        plan_protocol_enabled=True,
    )
    runner._task_instruction = "execute approved plan"
    messages = []
    first_schemas = await runner._allowed_tool_schemas()
    await runner._append_plan_runtime_update(messages)
    first_outbound = repair_role_alternation(messages)
    stable_system = runner._system_prompt()

    assert {item["name"] for item in first_schemas} == {
        "Read",
        "Bash",
        "TaskMemory",
        "AgentPlanProgress",
        "AgentPlanReplan",
        "AgentControlAck",
    }
    assert 'mode="full"' in messages[-1]["content"]

    await dao.db.conn.execute(
        "UPDATE rath_task_plan_state SET current_step_id='s1', row_revision=row_revision+1 WHERE task_uuid=?",
        (task_uuid,),
    )
    await dao.db.conn.commit()
    second_schemas = await runner._allowed_tool_schemas()
    await runner._append_plan_runtime_update(messages)
    second_outbound = repair_role_alternation(messages)
    assert second_outbound[: len(first_outbound)] == first_outbound
    assert second_schemas == first_schemas
    assert 'mode="delta"' in messages[-1]["content"]
    assert "deltaSemantics" in messages[-1]["content"]
    assert '"operation":"upsert"' in messages[-1]["content"]
    assert '"omittedFields":"unchanged"' in messages[-1]["content"]
    assert "按 (planVersion, stepId)" in stable_system
    assert "pendingControls 每次出现时整数组替换" in stable_system
    assert runner._system_prompt() == stable_system

    runner.steers.append(
        {
            "controlUuid": "control-append-only",
            "requestedBy": "main-controller",
            "message": "继续当前步骤，不要重做",
            "metadata": {"reasonCode": "progress_review"},
        }
    )
    assert runner._append_pending_steers(messages) is True
    control_outbound = repair_role_alternation(messages)
    assert control_outbound[: len(second_outbound)] == second_outbound
    messages.append({"role": "user", "content": "plan completion correction"})
    correction_outbound = repair_role_alternation(messages)
    assert correction_outbound[: len(control_outbound)] == control_outbound

    runner._last_plan_runtime_snapshot = {
        **runner._last_plan_runtime_snapshot,
        "pendingControls": [{"control_uuid": "control-1"}],
    }
    mode, control_delta = runner._plan_runtime_payload(
        {
        **runner._last_plan_runtime_snapshot,
        "pendingControls": [],
        }
    )
    assert mode == "delta"
    assert control_delta["pendingControls"] == []
    assert control_delta["deltaSemantics"]["arrays"]["pendingControls"] == {"operation": "replace"}

    await dao.db.conn.execute(
        "UPDATE rath_task_plan_state SET phase='finalizing', row_revision=row_revision+1 WHERE task_uuid=?",
        (task_uuid,),
    )
    await dao.db.conn.commit()
    assert await runner._allowed_tool_schemas() == first_schemas

    await dao.db.conn.execute(
        "UPDATE rath_task_plan_state SET phase='executing', approved_tools_json='[\"Read\"]', row_revision=row_revision+1 WHERE task_uuid=?",
        (task_uuid,),
    )
    await dao.db.conn.commit()
    with pytest.raises(RuntimeError, match="tool set changed"):
        await runner._allowed_tool_schemas()


def test_full_plan_runtime_does_not_repeat_full_state_in_instruction_prompts(env):
    dao, task_uuid, agent = env
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        plan_protocol_enabled=True,
    )
    runner._task_instruction = "执行当前批准计划"
    large_evidence = "EVIDENCE_BODY_SENTINEL-" + ("x" * 20_000)
    runtime = {
        "phase": "executing",
        "activePlanVersion": 1,
        "pendingPlanVersion": 0,
        "currentStepId": "s1",
        "approvalCycle": 1,
        "revisionCount": 0,
        "rowRevision": 1,
        "approvedTools": ["Read"],
        "pendingControls": [],
        "controllerGuidance": "只完成当前步骤。",
        "plan": {"title": "测试计划", "steps": [{"id": "s1", "title": "读取"}]},
        "steps": [{"plan_version": 1, "step_id": "s1", "status": "running"}],
        "completedHistory": [],
        "evidence": [{
            "evidence_uuid": "ev-1",
            "plan_version": 1,
            "step_id": "s1",
            "criterion_id": "c1",
            "reference": "tests/example.py:1",
            "summary": large_evidence,
            "metadata": {"raw": large_evidence},
        }],
    }

    mode, payload = runner._plan_runtime_payload(runtime, force_full=True)

    assert mode == "full"
    assert payload["evidence"][0]["summary"] == large_evidence
    assert "EVIDENCE_BODY_SENTINEL" not in payload["phaseInstructions"]
    assert len(payload["restoreInstructions"]) < 10_000
    assert "完整且权威的 plan" in payload["phaseInstructions"]


async def test_single_agent_runner_uses_registered_agent_config(env):
    dao, task_uuid, agent = env
    reg = ToolRegistry()

    async def read(_args):
        return "README 内容"

    reg.add("Read", "read file", {"type": "object", "properties": {}}, read)
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=reg,
        model_label="openai/gpt",
        think_level="high",
        cost={"input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 0.2},
    )

    output = await runner.run()

    assert "已读取" in output["summary"]
    assert output["agent"]["agentKey"] == "code-reader"
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "completed"
    assert task.model_call_count == 2
    assert task.tool_call_count == 1
    assert task.input_tokens == 24
    assert task.output_tokens == 12
    assert task.cache_read_tokens == 4
    assert task.cache_write_tokens == 2
    private_context = await dao.task_model_context(task_uuid)
    assert private_context is not None and private_context["protocol"] == "chat"
    events = await dao.events(task_uuid)
    started = [e for e in events if e.kind == "model_call_started"]
    assert started[-1].detail["modelLabel"] == "openai/gpt"
    assert started[-1].detail["thinkLevel"] == "high"


async def test_tool_call_started_persists_complete_long_arguments(env):
    dao, task_uuid, agent = env
    reg = ToolRegistry()
    long_arguments = json.dumps({"path": f"/tmp/{'x' * 1400}.txt"})

    async def read(_args):
        return "ok"

    reg.add("Read", "read file", {"type": "object", "properties": {}}, read)

    class LongArgumentsBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.calls += 1
            if self.calls == 1:
                return AgentResult(
                    tool_calls=[ToolCall(id="long-read", name="Read", arguments=long_arguments)],
                    usage=Usage(input_tokens=1, output_tokens=1),
                    finish_reason="tool_calls",
                )
            return AgentResult(
                text="done", usage=Usage(input_tokens=1, output_tokens=1), finish_reason="stop"
            )

    await SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=LongArgumentsBackend(),
        model="gpt",
        max_tokens=2048,
        tools=reg,
    ).run()

    events = await dao.events(task_uuid)
    started = next(event for event in events if event.kind == "tool_call_started")
    assert len(started.detail["arguments"]) > 1000
    assert started.detail["arguments"] == long_arguments
    assert json.loads(started.detail["arguments"])["path"].endswith(".txt")


async def test_responses_agent_persists_and_replays_native_continuation(env):
    dao, task_uuid, agent = env
    reg = ToolRegistry()

    async def read(_args):
        return "README 内容"

    reg.add("Read", "read file", {"type": "object", "properties": {}}, read)

    class NativeResponsesBackend:
        protocol = "responses"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.calls += 1
            assert opts.get("native_continuation") is True
            if self.calls == 1:
                yield StreamEvent(kind="reasoning", text="先读取文件")
                yield StreamEvent(
                    kind="native_output_item",
                    native_output_items=[
                        {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "先读取文件"}],
                    "encrypted_content": "opaque-round-1",
                        }
                    ],
                )
                yield StreamEvent(
                    kind="native_output_item",
                    native_output_items=[
                        {
                    "type": "function_call",
                    "id": "fc_item_1",
                    "call_id": "read_1",
                    "name": "Read",
                    "arguments": '{"path":"README.md"}',
                    "status": "completed",
                        }
                    ],
                )
                yield StreamEvent(
                    kind="tool_call",
                    tool_calls=[
                        ToolCall(id="read_1", name="Read", arguments='{"path":"README.md"}')
                    ],
                )
                yield StreamEvent(kind="usage", usage=Usage(input_tokens=100, output_tokens=10))
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
                return

            assistant = next(message for message in messages if message.get("native_output_items"))
            assert assistant["reasoning"] == "先读取文件"
            assert [item["type"] for item in assistant["native_output_items"]] == [
                "reasoning",
                "function_call",
            ]
            assert assistant["native_output_items"][0]["encrypted_content"] == "opaque-round-1"
            assert any(
                message.get("role") == "tool" and message.get("tool_call_id") == "read_1"
                for message in messages
            )
            yield StreamEvent(kind="reasoning", text="已取得结果")
            yield StreamEvent(kind="content", text="结论：已完成")
            yield StreamEvent(
                kind="native_output_item",
                native_output_items=[
                    {
                "type": "reasoning",
                "id": "rs_2",
                "summary": [{"type": "summary_text", "text": "已取得结果"}],
                "encrypted_content": "opaque-round-2",
                    }
                ],
            )
            yield StreamEvent(
                kind="native_output_item",
                native_output_items=[
                    {
                "type": "message",
                "id": "msg_2",
                "role": "assistant",
                "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "结论：已完成", "annotations": []}
                        ],
                    }
                ],
            )
            yield StreamEvent(
                kind="usage", usage=Usage(input_tokens=5, cache_read_tokens=95, output_tokens=5)
            )
            yield StreamEvent(kind="finish", finish_reason="stop")

    backend = NativeResponsesBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
        model_label="openai/gpt",
    )

    output = await runner.run()

    assert output["summary"] == "结论：已完成"
    assert backend.calls == 2
    checkpoint = await dao.task_model_context(task_uuid)
    assert checkpoint is not None
    assert checkpoint["protocol"] == "responses"
    assert checkpoint["model"] == "gpt"
    assert checkpoint["sessionId"] == runner.session_id
    assert checkpoint["revision"] >= 4
    state = checkpoint["state"]
    assert state["stage"] == "completed"
    native_messages = [
        message for message in state["messages"] if message.get("native_output_items")
    ]
    assert len(native_messages) == 2
    assert native_messages[0]["native_output_items"][0]["encrypted_content"] == "opaque-round-1"
    assert native_messages[1]["native_output_items"][0]["encrypted_content"] == "opaque-round-2"
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert "opaque-round" not in str(task.output)
    deleted = await dao.delete_task_records([task_uuid])
    await dao.db.conn.commit()
    assert deleted["modelContexts"] == 1
    assert await dao.task_model_context(task_uuid) is None


async def test_responses_interrupted_stream_executes_complete_native_tool_call_once(env):
    dao, task_uuid, agent = env
    reg = ToolRegistry()
    executions: list[str] = []

    async def read(_args):
        executions.append("read_1")
        return "once-only-result"

    reg.add("Read", "read", {"type": "object", "properties": {}}, read)

    class InterruptedResponsesBackend:
        protocol = "responses"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(kind="reasoning", text="完整调用已生成")
                yield StreamEvent(
                    kind="native_output_item",
                    native_output_items=[
                        {
                    "type": "reasoning",
                    "id": "rs_interrupted",
                    "encrypted_content": "opaque-interrupted",
                        }
                    ],
                )
                yield StreamEvent(
                    kind="native_output_item",
                    native_output_items=[
                        {
                    "type": "function_call",
                    "id": "fc_interrupted",
                    "call_id": "read_1",
                    "name": "Read",
                    "arguments": "{}",
                    "status": "completed",
                        }
                    ],
                )
                yield StreamEvent(
                    kind="tool_call",
                    tool_calls=[ToolCall(id="read_1", name="Read", arguments="{}")],
                )
                raise OpenBearLLMError("stream disconnected", status=502, retryable=True)

            assistant = next(message for message in messages if message.get("native_output_items"))
            assert assistant["native_output_items"][0]["encrypted_content"] == "opaque-interrupted"
            assert any(
                message.get("role") == "tool" and message.get("tool_call_id") == "read_1"
                for message in messages
            )
            yield StreamEvent(kind="content", text="中断恢复完成")
            yield StreamEvent(
                kind="native_output_item",
                native_output_items=[
                    {
                "type": "message",
                "id": "msg_interrupted_recovered",
                "role": "assistant",
                "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "中断恢复完成", "annotations": []}
                        ],
                    }
                ],
            )
            yield StreamEvent(kind="finish", finish_reason="stop")

    backend = InterruptedResponsesBackend()
    output = await SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
        max_retries=2,
        retry_backoff_s=0,
    ).run()

    assert output["summary"] == "中断恢复完成"
    assert backend.calls == 2
    assert executions == ["read_1"]
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.model_call_count == 2
    assert "opaque-interrupted" not in str(task.output)
    events = await dao.events(task_uuid)
    assert any(event.kind == "model_stream_recovered_tool_calls" for event in events)


async def test_responses_native_context_resumes_after_db_reconnect(env):
    dao, task_uuid, agent = env
    reg = ToolRegistry()

    async def read(_args):
        return "durable-result"

    reg.add("Read", "read", {"type": "object", "properties": {}}, read)

    class FirstProcessBackend:
        protocol = "responses"

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            assert opts.get("native_continuation") is True
            return AgentResult(
                reasoning="先读取",
                tool_calls=[ToolCall(id="durable_1", name="Read", arguments="{}")],
                native_output_items=[
                    {
                        "type": "reasoning",
                        "id": "rs_durable",
                        "encrypted_content": "opaque-durable",
                    },
                    {
                        "type": "function_call",
                        "id": "fc_durable",
                        "call_id": "durable_1",
                        "name": "Read",
                        "arguments": "{}",
                        "status": "completed",
                    },
                ],
                finish_reason="tool_calls",
            )

    first_runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=FirstProcessBackend(),
        model="gpt",
        max_tokens=2048,
        tools=reg,
        model_call_limit=1,
    )
    paused = await first_runner.run()
    assert paused["status"] == "needs_openbear_control"
    public_artifacts = [
        artifact
        for artifact in await dao.artifacts(task_uuid)
        if artifact.kind == "agent_continuation_state"
    ]
    assert public_artifacts
    assert "opaque-durable" in public_artifacts[-1].content
    private_checkpoint = await dao.task_model_context(task_uuid)
    assert private_checkpoint is not None
    assert "opaque-durable" in str(private_checkpoint["state"])
    merged_state = await first_runner._latest_continuation_state()
    assert merged_state is not None
    assert "opaque-durable" in str(merged_state["messages"])

    # Force continuation to use the private model-context checkpoint rather than
    # the public control artifact, then reconnect SQLite like a real restart.
    await dao.db.conn.execute(
        "DELETE FROM rath_task_artifacts WHERE task_uuid=? AND kind='agent_continuation_state'",
        (task_uuid,),
    )
    await dao.db.conn.commit()
    await dao.db.close()
    await dao.db.connect()
    resumed_dao = RathDAO(dao.db)

    class SecondProcessBackend:
        protocol = "responses"

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            assistant = next(message for message in messages if message.get("native_output_items"))
            assert assistant["native_output_items"][0]["encrypted_content"] == "opaque-durable"
            assert any(
                message.get("role") == "tool" and message.get("tool_call_id") == "durable_1"
                for message in messages
            )
            return AgentResult(
                text="重启恢复完成",
                native_output_items=[
                    {
                    "type": "message",
                    "id": "msg_resumed",
                    "role": "assistant",
                    "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "重启恢复完成", "annotations": []}
                        ],
                    }
                ],
            )

    second_runner = SingleAgentWorkflowRunner(
        resumed_dao,
        task_uuid,
        agent=agent,
        backend=SecondProcessBackend(),
        model="gpt",
        max_tokens=2048,
        tools=reg,
        model_call_limit=2,
        session_id=first_runner.session_id,
    )
    continued = await second_runner.run_continue("从持久化检查点继续")

    assert continued["summary"] == "重启恢复完成"
    assert "opaque-durable" not in str(continued)
    checkpoint = await resumed_dao.task_model_context(task_uuid)
    assert checkpoint is not None
    assert checkpoint["state"]["stage"] == "completed"


async def test_resumed_agent_restores_provider_snapshot_before_first_model_request(env, monkeypatch):
    """A paused Agent must retain an unconsumed real prompt snapshot across processes."""
    dao, task_uuid, agent = env
    agent.tool_allowlist = ["Read"]
    reg = ToolRegistry()

    async def read(_args):
        return "durable tool result"

    reg.add("Read", "read", {"type": "object", "properties": {}}, read)

    class FirstProcessBackend:
        protocol = "responses"

        async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            assert opts.get("native_continuation") is True
            return AgentResult(
                tool_calls=[ToolCall(id="resume-read", name="Read", arguments="{}")],
                native_output_items=[
                    {"type": "reasoning", "id": "resume-r", "encrypted_content": "opaque-resume"},
                    {
                        "type": "function_call",
                        "id": "resume-fc",
                        "call_id": "resume-read",
                        "name": "Read",
                        "arguments": "{}",
                    },
                ],
                usage=Usage(input_tokens=782, cache_read_tokens=271_616, output_tokens=1),
                finish_reason="tool_calls",
            )

    first_runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=FirstProcessBackend(),
        model="resume-model",
        max_tokens=2048,
        tools=reg,
        model_call_limit=1,
        context_compact_trigger_tokens=272_000,
    )
    paused = await first_runner.run()
    assert paused["status"] == "needs_openbear_control"
    state = await first_runner._latest_continuation_state()
    assert state is not None
    assert state["providerPromptSnapshot"] == {
        "tokens": 272_398,
        "usageGeneration": 1,
        "compactedUsageGeneration": -1,
    }

    class SecondProcessBackend:
        protocol = "responses"

        def __init__(self):
            self.messages: list[dict] = []

        async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.messages = copy.deepcopy(messages)
            assert opts.get("native_continuation") is True
            return AgentResult(text="resumed after compaction", usage=Usage(input_tokens=20, output_tokens=2))

    second_backend = SecondProcessBackend()
    second_runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=second_backend,
        model="resume-model",
        max_tokens=2048,
        tools=reg,
        model_call_limit=2,
        context_compact_trigger_tokens=272_000,
        session_id=first_runner.session_id,
    )

    async def summarize(_old_messages, **_kwargs):
        return _GOOD_COMPACTION_SUMMARY.strip()

    monkeypatch.setattr(second_runner, "_summarize_context_with_llm", summarize)
    continued = await second_runner.run_continue("continue safely")

    assert continued["summary"] == "resumed after compaction"
    assert any(
        "【Rath Agent 上下文压缩摘要】" in str(message.get("content") or "")
        for message in second_backend.messages
    )
    assert not any(message.get("native_output_items") for message in second_backend.messages)
    events = await dao.events(task_uuid)
    detail = [event for event in events if event.kind == "model_context_pre_compacted"][-1].detail
    assert detail["providerPromptTokensBefore"] == 272_398
    assert detail["tokenSource"] == "provider_usage"


async def test_native_context_sanitizer_drops_unpaired_function_calls(env):
    dao, task_uuid, agent = env
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
    )
    messages = [
        {
        "role": "assistant",
        "content": "",
        "reasoning": "已规划两个调用",
        "tool_calls": [
            ToolCall(id="keep", name="Read", arguments="{}"),
            ToolCall(id="drop", name="Read", arguments="{}"),
        ],
        "native_output_items": [
            {"type": "reasoning", "encrypted_content": "opaque"},
            {"type": "function_call", "call_id": "keep", "name": "Read", "arguments": "{}"},
            {"type": "function_call", "call_id": "drop", "name": "Read", "arguments": "{}"},
        ],
        },
        {
        "role": "tool",
        "tool_call_id": "keep",
        "name": "Read",
        "content": "ok",
        },
    ]

    sanitized, dropped = runner._sanitize_paired_messages(messages)

    assert dropped == 1
    assert [call.id for call in sanitized[0]["tool_calls"]] == ["keep"]
    # A partially edited encrypted reasoning turn is unsafe to replay. Keep the
    # readable paired transcript and start a fresh native cache segment instead.
    assert "native_output_items" not in sanitized[0]
    assert sanitized[1]["tool_call_id"] == "keep"


async def test_single_agent_runner_keeps_tool_schemas_across_rounds(env):
    dao, task_uuid, agent = env
    reg = ToolRegistry()

    async def read(args):
        return f"内容:{args.get('path')}"

    reg.add(
        "Read",
        "read file",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        read,
        visibility={"agent"},
    )
    backend = _MultiStepBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
    )

    output = await runner.run()

    assert output["summary"] == "结论：连续读取完成"
    assert backend.tools_seen == [True, True, True]
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.tool_call_count == 2
    assert task.model_call_count == 3


async def test_single_agent_runner_empty_allowlist_exposes_no_agent_tools(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = []
    reg = ToolRegistry()

    async def read(args):
        return f"内容:{args.get('path')}"

    reg.add(
        "Read",
        "read file",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        read,
        visibility={"agent"},
    )

    class CaptureToolsBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.tools_seen = None

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.tools_seen = tools
            return AgentResult(text="无工具完成")

    backend = CaptureToolsBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
    )

    output = await runner.run()

    assert output["summary"] == "无工具完成"
    assert backend.tools_seen is None
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.tool_call_count == 0


async def test_single_agent_runner_empty_allowlist_denies_unsolicited_tool_calls(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = []
    reg = ToolRegistry()

    async def read(_args):
        return "should-not-run"

    reg.add("Read", "read file", {"type": "object", "properties": {}}, read, visibility={"agent"})

    class UnsolicitedToolBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.calls += 1
            if self.calls == 1:
                return AgentResult(
                    tool_calls=[ToolCall(id="read", name="Read", arguments="{}")],
                    finish_reason="tool_calls",
                )
            tool_text = "\n".join(
                str(m.get("content") or "") for m in messages if m.get("role") == "tool"
            )
            return AgentResult(text=f"收到：{tool_text}")

    backend = UnsolicitedToolBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
    )

    output = await runner.run()

    assert "未授权" in output["summary"]
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.tool_call_count == 0
    events = await dao.events(task_uuid)
    assert any(e.kind == "tool_call_denied" for e in events)


async def test_single_agent_runner_hard_blocks_memory_even_if_allowlisted(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = ["Memory"]
    reg = ToolRegistry()

    async def memory(_args):
        return "secret"

    reg.add("Memory", "memory", {"type": "object", "properties": {}}, memory, visibility={"agent"})

    class CaptureToolsBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.tools_seen = None

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.tools_seen = tools
            return AgentResult(text="没有拿到 Memory")

    backend = CaptureToolsBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
    )

    output = await runner.run()

    assert output["summary"] == "没有拿到 Memory"
    assert backend.tools_seen is None
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.tool_call_count == 0


async def test_single_agent_runner_denies_unsolicited_memory_tool_call(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = ["Memory"]
    reg = ToolRegistry()

    async def memory(_args):
        return "secret"

    reg.add("Memory", "memory", {"type": "object", "properties": {}}, memory, visibility={"agent"})

    class UnsolicitedMemoryBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.calls += 1
            if self.calls == 1:
                return AgentResult(
                    tool_calls=[
                        ToolCall(
                            id="mem",
                            name="Memory",
                            arguments='{"resource":"secret","action":"get","name":"gh"}',
                        )
                    ],
                    finish_reason="tool_calls",
                )
            tool_text = "\n".join(
                str(m.get("content") or "") for m in messages if m.get("role") == "tool"
            )
            return AgentResult(text=f"收到：{tool_text}")

    backend = UnsolicitedMemoryBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
    )

    output = await runner.run()

    assert "未授权" in output["summary"]
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.tool_call_count == 0
    events = await dao.events(task_uuid)
    assert any(e.kind == "tool_call_denied" for e in events)


async def test_single_agent_runner_retries_retryable_model_error(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = []
    backend = _FlakyBackend()
    calls = []
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        on_model_call=lambda detail: calls.append(detail),
    )

    output = await runner.run()

    assert output["summary"] == "重试后成功"
    assert backend.calls == 2
    assert [call["status"] for call in calls] == ["error", "ok"]
    assert calls[0]["inputTokens"] == 0
    assert calls[0]["outputTokens"] == 0
    assert calls[1]["inputTokens"] == 1
    assert calls[1]["outputTokens"] == 1
    task = await dao.get_task(task_uuid)
    assert task is not None and task.model_call_count == 2
    events = await dao.events(task_uuid)
    assert any(e.kind == "model_call_retry" for e in events)
    started = [event for event in events if event.kind == "model_call_started"]
    assert len(started) == 2
    assert started[-1].detail["attempt"] == 1
    assert "重试 1/10" in started[-1].summary


async def test_single_agent_physical_retry_deduplicates_task_memory_and_keeps_prefix(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = []
    memories = TaskMemoryDAO(dao.db)
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_CONVERSATION,
        name="retry state",
        description="stable",
        visible_to_agents=True,
    )
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=task_uuid,
        name="private retry state",
        description="stable",
    )
    backend = _FlakyBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        retry_backoff_s=0.001,
        retry_max_delay_s=0.001,
        retry_jitter_ratio=0,
        plan_protocol_enabled=False,
    )

    output = await runner.run()

    assert output["summary"] == "重试后成功"
    assert len(backend.seen_messages) == 2
    first, second = backend.seen_messages
    assert second[: len(first)] == first
    assert len([message for message in first if is_task_memory_runtime_message(message)]) == 1
    assert len([message for message in second if is_task_memory_runtime_message(message)]) == 1
    assert backend.seen_systems[0].encode() == backend.seen_systems[1].encode()
    assert backend.seen_tools[0] == backend.seen_tools[1]


async def test_single_agent_runner_retry_wait_can_be_cancelled(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = []
    backend = _FlakyBackend()
    calls = []
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        retry_backoff_s=30,
        retry_cancel_check=lambda: True,
        on_model_call=lambda detail: calls.append(detail),
    )

    with pytest.raises(asyncio.CancelledError, match="model retry cancelled by user"):
        await runner.run()

    assert backend.calls == 1
    assert len(calls) == 1
    assert calls[0]["status"] == "error"
    assert calls[0]["inputTokens"] == 0
    assert calls[0]["outputTokens"] == 0
    task = await dao.get_task(task_uuid)
    assert task is not None
    retry_state = task.output.get("retry") if isinstance(task.output, dict) else {}
    assert retry_state.get("active") is False
    events = await dao.events(task_uuid)
    assert any(event.kind == "model_call_retry_cancelled" for event in events)


async def test_single_agent_uses_streaming_transport_for_long_final_output(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = []

    class StreamingOnlyBackend:
        protocol = "responses"

        def __init__(self) -> None:
            self.stream_calls = 0
            self.complete_calls = 0

        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.stream_calls += 1
            yield StreamEvent(kind="reasoning", text="已完成调查，正在组织长文")
            yield StreamEvent(kind="content", text="第一部分。")
            yield StreamEvent(kind="content", text="第二部分。")
            yield StreamEvent(kind="usage", usage=Usage(input_tokens=100, output_tokens=20))
            yield StreamEvent(kind="finish", finish_reason="stop")

        async def complete(self, *args, **kwargs):
            self.complete_calls += 1
            raise AssertionError("Rath production path must not use non-streaming complete()")

    backend = StreamingOnlyBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=128000,
        tools=ToolRegistry(),
    )

    output = await runner.run()

    assert output["summary"] == "第一部分。第二部分。"
    assert backend.stream_calls == 1
    assert backend.complete_calls == 0
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "completed"
    assert task.input_tokens == 100
    assert task.output_tokens == 20
    events = await dao.events(task_uuid)
    finished = [event for event in events if event.kind == "model_call_finished"]
    assert finished[-1].detail["status"] == "ok"


async def test_single_agent_compaction_timeout_is_configured_per_attempt(env, monkeypatch):
    """Agent 压缩不再受 30/75 秒硬编码限制，且每个重试独立使用配置值。"""
    dao, task_uuid, agent = env
    timeouts = []

    async def fake_collect(_backend, _messages, *, timeout_s, **kwargs):
        timeouts.append(
            {
            "outer": timeout_s,
            "firstByte": kwargs.get("first_byte_timeout_s"),
            "total": kwargs.get("total_timeout_s"),
            "read": kwargs.get("read_timeout_s"),
            "connect": kwargs.get("connect_timeout_s"),
            "idle": kwargs.get("idle_timeout_s"),
            }
        )
        if len(timeouts) == 1:
            raise TimeoutError
        return AgentResult(text=_GOOD_COMPACTION_SUMMARY), False, ""

    monkeypatch.setattr("app.rath.single_agent.collect_backend_result", fake_collect)
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        context_compact_backend=_Backend(),
        context_compact_model="compact-model",
        context_compact_max_retries=1,
        context_compact_timeout_s=2345,
    )

    summary = await runner._summarize_context_with_llm(
        [{"role": "user", "content": "old context"}],
        attempt=1,
        reason="test",
    )

    assert summary == _GOOD_COMPACTION_SUMMARY.strip()
    assert timeouts == [
        {
            "outer": 2345,
            "firstByte": 2345,
            "total": 2345,
            "read": 2345,
            "connect": None,
            "idle": None,
        },
        {
            "outer": 2345,
            "firstByte": 2345,
            "total": 2345,
            "read": 2345,
            "connect": None,
            "idle": None,
        },
    ]


async def test_single_agent_compaction_deduplicates_primary_fallback_candidate(env, monkeypatch):
    """主模型已在有序候选中时，不应再因 label/model ID 不同而重复追加。"""
    dao, task_uuid, agent = env
    compression_backend = _Backend()
    fallback_backend = _Backend()
    calls = []

    async def fake_collect(backend, _messages, *, model, **_kwargs):
        calls.append((backend, model))
        raise RuntimeError("compaction unavailable")

    monkeypatch.setattr("app.rath.single_agent.collect_backend_result", fake_collect)
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        context_compact_backend=compression_backend,
        context_compact_model="compact-model",
        context_compact_label="OpenAI/compact-model",
        context_compact_extra_candidates=[
            CompressionCandidate(
                fallback_backend,
                "primary-model",
                "primary-fallback",
                "OpenAI/primary-model",
            )
        ],
        context_compact_fallback_backend=fallback_backend,
        context_compact_fallback_model="primary-model",
        context_compact_max_retries=0,
    )

    summary = await runner._summarize_context_with_llm(
        [{"role": "user", "content": "old context"}],
        attempt=1,
        reason="test",
    )

    assert summary is None
    assert calls == [
        (compression_backend, "compact-model"),
        (fallback_backend, "primary-model"),
    ]


async def test_single_agent_compacts_context_overflow_from_large_tool_result(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = ["Read"]
    await TaskMemoryDAO(dao.db).create(
        conversation_uuid="session-1",
        scope_type=SCOPE_CONVERSATION,
        name="overflow runtime",
        description="must be rebuilt, never summarized",
        visible_to_agents=True,
    )

    class OverflowAfterToolBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.calls = 0
            self.messages_after_compaction = None

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.calls += 1
            tool_messages = [m for m in messages if m.get("role") == "tool"]
            history_rebuilt = any(
                "<agent-history_messages>" in str(message.get("content") or "")
                for message in messages
            )
            if history_rebuilt:
                self.messages_after_compaction = messages
                return AgentResult(text="压缩后成功", usage=Usage(input_tokens=20, output_tokens=3))
            if not tool_messages:
                return AgentResult(
                    tool_calls=[ToolCall(id="read", name="Read", arguments='{"path":"huge.log"}')],
                    finish_reason="tool_calls",
                    usage=Usage(input_tokens=10, output_tokens=1),
                )
            raise OpenBearLLMError(
                "Prompt is too long: context_length_exceeded: Your input exceeds the context window of this model.",
                status=400,
                retryable=False,
            )

    reg = ToolRegistry()

    async def read(_args):
        return "TOO_BIG_START\n" + ("x" * 20_000) + "\nTOO_BIG_END"

    reg.add("Read", "read", {"type": "object", "properties": {}}, read)
    backend = OverflowAfterToolBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
        tool_result_max_chars=32_000,
    )

    output = await runner.run()

    assert output["summary"] == "压缩后成功"
    assert backend.calls == 3
    assert backend.messages_after_compaction is not None
    overflow_states = [
        message
        for message in backend.messages_after_compaction
        if is_task_memory_runtime_message(message)
    ]
    assert len(overflow_states) == 1
    assert overflow_states[0]["_openbear_runtime"]["epoch"] == 1
    compacted_tool_text = "\n".join(
        str(m.get("content") or "")
        for m in backend.messages_after_compaction
        if m.get("role") == "tool"
    )
    assert compacted_tool_text == ""
    rebuilt_context = "\n".join(
        str(m.get("content") or "") for m in backend.messages_after_compaction
    )
    assert "<agent-history_messages>" in rebuilt_context
    # The deterministic fallback may retain a short summary preview when the
    # compression model is unavailable, but never replays the 20k raw tool body.
    assert rebuilt_context.count("x") < 2_000
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "completed"
    events = await dao.events(task_uuid)
    compacted = [e for e in events if e.kind == "model_context_overflow_compacted"][-1]
    detail = compacted.detail
    assert detail["final"] is True
    assert detail["scope"] == "agent"
    assert detail["status"] == "overflow_compacted"
    assert detail["beforeTokens"] == detail["estimatedTokensBefore"]
    assert detail["afterTokens"] == detail["estimatedTokensAfter"]
    assert detail["summaryChars"] == len(detail["compactedOutput"])
    assert detail["outputAvailable"] is True
    assert detail["keepRecentMode"] == "semantic_xml"
    assert detail["rawMessagesKept"] == 0
    assert "openbear-task-memory-state" not in detail["compactedOutput"]
    assert detail["compactionId"] == detail["summaryId"]
    assert detail["compactionId"].startswith(f"agent-compaction:{task_uuid}:overflow_compacted:")
    assert any(
        str(message.get("content") or "") == detail["compactedOutput"]
        for message in backend.messages_after_compaction
    )


async def test_single_agent_pre_compaction_final_event_persists_actual_injected_output(
    env, monkeypatch
):
    dao, task_uuid, agent = env
    memories = TaskMemoryDAO(dao.db)
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_CONVERSATION,
        name="compaction runtime",
        description="must not enter summary",
        visible_to_agents=True,
    )
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        context_window=100,
        context_compact_trigger_tokens=5,
        context_compact_keep_recent=1,
    )
    runner.chat_id = 123
    runner.conversation_uuid = "session-1"
    summarized_messages: list[dict] = []

    async def summarize(old_messages, **_kwargs):
        summarized_messages.extend(copy.deepcopy(old_messages))
        return _GOOD_COMPACTION_SUMMARY.strip()

    monkeypatch.setattr(runner, "_summarize_context_with_llm", summarize)
    messages = [
        {"role": "user", "content": "old-a" * 200},
        {"role": "assistant", "content": "old-b" * 200},
        {"role": "user", "content": "latest"},
    ]
    await runner._reconcile_task_memory_context(messages)
    assert len([message for message in messages if is_task_memory_runtime_message(message)]) == 1

    assert await runner._pre_compact_context_if_needed(messages) is True
    assert runner._task_memory_epoch == 1
    rebuilt_states = [message for message in messages if is_task_memory_runtime_message(message)]
    assert len(rebuilt_states) == 1
    assert rebuilt_states[0]["_openbear_runtime"]["epoch"] == 1
    assert "openbear-task-memory-state" not in json.dumps(summarized_messages, ensure_ascii=False)
    events = await dao.events(task_uuid)
    compacted = [e for e in events if e.kind == "model_context_pre_compacted"][-1]
    detail = compacted.detail
    assert detail["final"] is True
    assert detail["scope"] == "agent"
    assert detail["source"] == "pre_model_request"
    assert detail["status"] == "pre_compacted"
    assert detail["outputAvailable"] is True
    assert detail["summaryChars"] == len(detail["compactedOutput"])
    assert messages[0]["content"] == detail["compactedOutput"]
    assert "openbear-task-memory-state" not in detail["compactedOutput"]
    assert detail["compactionId"] == detail["summaryId"]
    assert detail["compactionId"].startswith(f"agent-compaction:{task_uuid}:pre_compacted:")
    await runner._reconcile_task_memory_context(messages)
    rebuilt_states = [message for message in messages if is_task_memory_runtime_message(message)]
    assert len(rebuilt_states) == 1
    assert rebuilt_states[0]["_openbear_runtime"]["epoch"] == 1
    assert await runner._reconcile_task_memory_context(messages) is False


@pytest.mark.parametrize(
    ("protocol", "usage"),
    [
        ("chat", Usage(input_tokens=272_398, output_tokens=1)),
        ("anthropic", Usage(input_tokens=782, cache_read_tokens=271_616, output_tokens=1)),
        ("responses", Usage(input_tokens=782, cache_read_tokens=271_616, output_tokens=1)),
    ],
)
async def test_single_agent_pre_compaction_uses_provider_prompt_snapshot_for_all_protocols(
    env, monkeypatch, protocol, usage
):
    """The common Rath path must prefer normalized provider usage for every protocol."""
    dao, task_uuid, agent = env

    class PassiveBackend:
        def __init__(self, protocol_name):
            self.protocol = protocol_name

    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=PassiveBackend(protocol),
        model="model-under-test",
        max_tokens=2048,
        tools=ToolRegistry(),
        context_compact_trigger_tokens=272_000,
        context_compact_keep_recent=1,
    )
    runner.chat_id = 123
    runner._record_provider_prompt_usage(usage)

    async def summarize(_old_messages, **_kwargs):
        return _GOOD_COMPACTION_SUMMARY.strip()

    monkeypatch.setattr(runner, "_summarize_context_with_llm", summarize)
    messages = [
        {"role": "user", "content": "short prior request"},
        {"role": "assistant", "content": "short prior answer"},
        {"role": "user", "content": "continue"},
    ]

    assert await runner._pre_compact_context_if_needed(messages) is True
    events = await dao.events(task_uuid)
    detail = [event for event in events if event.kind == "model_context_pre_compacted"][-1].detail
    assert detail["beforeTokens"] == 272_398
    assert detail["providerPromptTokensBefore"] == 272_398
    assert detail["estimatedTokensBefore"] < 272_000
    assert detail["tokenSource"] == "provider_usage"
    # The old snapshot is consumed by the fold and must not compact the rebuilt
    # context once more before a fresh provider response arrives.
    assert runner._provider_prompt_tokens_for_compaction() == 0
    assert await runner._pre_compact_context_if_needed(messages) is False


async def test_single_agent_provider_snapshot_round_trips_generation_zero(env):
    """A prior local fold at generation zero must not consume the next real snapshot."""
    dao, task_uuid, agent = env
    first = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="model-under-test",
        max_tokens=2048,
        tools=ToolRegistry(),
    )
    first._consume_provider_prompt_usage_for_compaction()
    first._record_provider_prompt_usage(
        Usage(input_tokens=782, cache_read_tokens=271_616, output_tokens=1)
    )
    snapshot = first._provider_prompt_snapshot_state()
    assert snapshot == {
        "tokens": 272_398,
        "usageGeneration": 1,
        "compactedUsageGeneration": 0,
    }

    restored = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="model-under-test",
        max_tokens=2048,
        tools=ToolRegistry(),
    )
    restored._restore_provider_prompt_snapshot(snapshot)
    assert restored._provider_prompt_tokens_for_compaction() == 272_398


async def test_responses_agent_precompacts_from_provider_usage_after_tool_batch(env, monkeypatch):
    """Responses native context must compact before the next tool-loop request."""
    dao, task_uuid, agent = env
    agent.tool_allowlist = ["Read"]
    reg = ToolRegistry()

    async def read(_args):
        return "read result"

    reg.add("Read", "read", {"type": "object", "properties": {}}, read)

    class ResponsesBackend:
        protocol = "responses"

        def __init__(self):
            self.calls = 0
            self.second_messages: list[dict] = []

        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.calls += 1
            assert opts.get("native_continuation") is True
            if self.calls == 1:
                yield StreamEvent(
                    kind="native_output_item",
                    native_output_items=[
                        {"type": "reasoning", "id": "r1", "encrypted_content": "opaque-reasoning"}
                    ],
                )
                yield StreamEvent(
                    kind="native_output_item",
                    native_output_items=[
                        {
                            "type": "function_call",
                            "id": "fc1",
                            "call_id": "read-1",
                            "name": "Read",
                            "arguments": '{"path":"README.md"}',
                        }
                    ],
                )
                yield StreamEvent(
                    kind="tool_call",
                    tool_calls=[ToolCall(id="read-1", name="Read", arguments='{"path":"README.md"}')],
                )
                yield StreamEvent(
                    kind="usage",
                    usage=Usage(input_tokens=782, cache_read_tokens=271_616, output_tokens=1),
                )
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
                return

            self.second_messages = copy.deepcopy(messages)
            yield StreamEvent(kind="content", text="compacted continuation completed")
            yield StreamEvent(kind="usage", usage=Usage(input_tokens=20, output_tokens=2))
            yield StreamEvent(kind="finish", finish_reason="stop")

    backend = ResponsesBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="responses-model",
        max_tokens=2048,
        tools=reg,
        context_compact_trigger_tokens=272_000,
        context_compact_keep_recent=1,
    )

    async def summarize(_old_messages, **_kwargs):
        return _GOOD_COMPACTION_SUMMARY.strip()

    monkeypatch.setattr(runner, "_summarize_context_with_llm", summarize)
    output = await runner.run()

    assert output["summary"] == "compacted continuation completed"
    assert backend.calls == 2
    assert any(
        "【Rath Agent 上下文压缩摘要】" in str(message.get("content") or "")
        for message in backend.second_messages
    )
    assert not any(message.get("native_output_items") for message in backend.second_messages)
    events = await dao.events(task_uuid)
    detail = [event for event in events if event.kind == "model_context_pre_compacted"][-1].detail
    assert detail["beforeTokens"] == 272_398
    assert detail["providerPromptTokensBefore"] == 272_398
    assert detail["estimatedTokensBefore"] < 272_000
    assert detail["tokenSource"] == "provider_usage"


async def test_single_agent_compaction_rebuilds_semantic_xml_and_single_fresh_plan_runtime(
    env, monkeypatch
):
    dao, task_uuid, agent = env
    memories = TaskMemoryDAO(dao.db)
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_CONVERSATION,
        name="fresh runtime",
        description="must be injected after compaction",
        visible_to_agents=True,
    )
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        plan_protocol_enabled=True,
        context_compact_trigger_tokens=1,
        context_compact_keep_recent=10,
    )
    runner.chat_id = 123
    runner.conversation_uuid = "session-1"
    runner._task_instruction = "检查 Agent 压缩边界"
    summarized_messages: list[dict] = []

    async def summarize(old_messages, **_kwargs):
        summarized_messages.extend(copy.deepcopy(old_messages))
        return _GOOD_COMPACTION_SUMMARY.strip()

    monkeypatch.setattr(runner, "_summarize_context_with_llm", summarize)
    messages = [
        {"role": "user", "content": "原始任务：检查压缩"},
        {
            "role": "user",
            "content": (
                '<agent-plan-runtime revision="old" mode="full">\n'
                "<state-json>OLD_PLAN_SENTINEL</state-json>\n"
                "</agent-plan-runtime>\n"
                "这是系统追加的权威运行时状态，不是新的用户任务。按最新 revision 继续。"
            ),
            "_openbear_runtime": {"kind": "rath_agent_plan_runtime", "version": 1},
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [ToolCall(id="read-old", name="Read", arguments='{"path":"old.txt"}')],
        },
        {
            "role": "tool",
            "tool_call_id": "read-old",
            "name": "Read",
            "content": "RAW_READ_SENTINEL",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [ToolCall(id="tm-old", name="TaskMemory", arguments="{}")],
        },
        {
            "role": "tool",
            "tool_call_id": "tm-old",
            "name": "TaskMemory",
            "content": "TASK_MEMORY_RECEIPT_SENTINEL",
        },
        {
            "role": "assistant",
            "content": "中间结论：已定位主要问题。",
            "reasoning": "internal reasoning",
        },
        {"role": "user", "content": "主控制器补充：只处理当前范围。"},
    ]
    await runner._reconcile_task_memory_context(messages)

    assert await runner._pre_compact_context_if_needed(messages) is True

    # The compacted canonical transcript has one semantic summary/HTML-safe XML
    # tail, one newly generated full Plan state, and one fresh Task Memory state.
    assert len(messages) == 3
    compacted_text = str(messages[0]["content"])
    assert "<agent-history_messages>" in compacted_text
    assert "原始任务：检查压缩" in compacted_text
    assert "中间结论：已定位主要问题。" in compacted_text
    assert "主控制器补充：只处理当前范围。" in compacted_text
    assert "RAW_READ_SENTINEL" not in compacted_text
    assert "TASK_MEMORY_RECEIPT_SENTINEL" not in compacted_text
    assert "OLD_PLAN_SENTINEL" not in compacted_text
    assert "internal reasoning" not in compacted_text
    assert messages[0]["_openbear_runtime"]["kind"] == "rath_agent_context_summary"

    plan_messages = [
        message
        for message in messages
        if "<agent-plan-runtime" in str(message.get("content") or "")
    ]
    assert len(plan_messages) == 1
    assert 'mode="full"' in plan_messages[0]["content"]
    assert "OLD_PLAN_SENTINEL" not in plan_messages[0]["content"]
    assert plan_messages[0]["_openbear_runtime"]["kind"] == "rath_agent_plan_runtime"
    runtime_messages = [message for message in messages if is_task_memory_runtime_message(message)]
    assert len(runtime_messages) == 1
    assert runtime_messages[0]["_openbear_runtime"]["epoch"] == 1

    summarized_text = json.dumps(summarized_messages, ensure_ascii=False, default=str)
    assert "RAW_READ_SENTINEL" in summarized_text
    assert "TASK_MEMORY_RECEIPT_SENTINEL" in summarized_text
    assert "OLD_PLAN_SENTINEL" not in summarized_text
    assert "openbear-task-memory-state" not in summarized_text


async def test_single_agent_pre_compaction_failure_emits_final_unavailable_event(env, monkeypatch):
    dao, task_uuid, agent = env
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        context_window=100,
        context_compact_trigger_tokens=5,
        context_compact_keep_recent=1,
    )
    runner.chat_id = 123

    async def unavailable(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner, "_summarize_context_with_llm", unavailable)
    messages = [
        {"role": "user", "content": "old-a" * 200},
        {"role": "assistant", "content": "old-b" * 200},
        {"role": "user", "content": "latest"},
    ]

    assert await runner._pre_compact_context_if_needed(messages) is False
    events = await dao.events(task_uuid)
    failed = [
        e
        for e in events
        if e.kind == "model_context_compaction_failed" and e.detail.get("final") is True
    ][-1]
    assert failed.detail["scope"] == "agent"
    assert failed.detail["source"] == "pre_model_request"
    assert failed.detail["status"] == "failed"
    assert failed.detail["outputAvailable"] is False
    assert failed.detail["outputUnavailable"] == "summary_not_available"
    assert "compactedOutput" not in failed.detail


async def test_single_agent_context_overflow_pauses_when_nothing_to_compact(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = []

    class AlwaysOverflowBackend:
        protocol = "chat"

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            raise OpenBearLLMError(
                "HTTP 400: context_length_exceeded: Your input exceeds the context window of this model.",
                status=400,
                retryable=False,
            )

    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=AlwaysOverflowBackend(),
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
    )

    output = await runner.run()

    assert output["status"] == "needs_openbear_control"
    assert output["reason"] == "agent_context_overflow_unrecoverable"
    assert output["continuable"] is False
    assert "continueTool" not in output
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "needs_openbear_control"
    assert task.control_state == "waiting_openbear_control"
    events = await dao.events(task_uuid)
    assert any(e.kind == "needs_openbear_control" for e in events)


def test_agent_snapshot_contains_runtime_config(env):
    _dao, _task_uuid, agent = env
    snap = agent_to_snapshot(agent)
    assert snap["name"] == "代码阅读员"
    assert snap["model"] == "openai/gpt"
    assert snap["thinkLevel"] == "high"
    assert snap["toolAllowlist"] == ["Read"]


async def test_single_agent_session_reuses_own_summary_and_artifacts(tmp_path):
    """同一 Rath Agent Session 后续 Task 会续接自身摘要/产物；不同 session 仍隔离。"""
    from app.db.engine import DB
    from app.llm.base import AgentResult
    from app.rath.dao import RathDAO
    from app.rath.schemas import RathAgentDef
    from app.rath.single_agent import SingleAgentWorkflowRunner

    class CaptureBackend:
        protocol = "chat"

        def __init__(self):
            self.calls = []

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.calls.append(
                {
                "messages": messages,
                "system": system,
                "session_id": opts.get("session_id"),
                }
            )
            return AgentResult(
                text="模块 A 结论：核心细节已记录" if len(self.calls) == 1 else "模块 B 结论"
            )

    db = DB(str(tmp_path / "rath-history.db"))
    await db.connect()
    dao = RathDAO(db)
    wf_uuid = await dao.upsert_workflow(slug="single-agent", name="Single", kind="single-agent")
    agent = RathAgentDef(
        workflow_uuid=wf_uuid,
        agent_key="reader",
        name="源码阅读员",
        system_prompt="你是源码阅读员",
        tool_allowlist=[],
    )
    backend = CaptureBackend()
    agent_session = await dao.get_or_create_agent_session(
        openbear_session_uuid="openbear-1",
        chat_id=1,
        workflow_uuid=wf_uuid,
        agent_key="reader",
        title="源码阅读员",
    )

    first_uuid = await dao.create_task(
        chat_id=1,
        workflow_uuid=wf_uuid,
        title="first",
        input_data={"instruction": "先研究源码，记住模块 A 的细节。"},
        parent_session_uuid="openbear-1",
        agent_session_uuid=agent_session.session_uuid,
    )
    await SingleAgentWorkflowRunner(
        dao,
        first_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=1000,
        openbear_session_uuid="openbear-1",
        agent_session_uuid=agent_session.session_uuid,
    ).run()

    second_uuid = await dao.create_task(
        chat_id=1,
        workflow_uuid=wf_uuid,
        title="second",
        input_data={"instruction": "再说模块 B 的细节。"},
        parent_session_uuid="openbear-1",
        agent_session_uuid=agent_session.session_uuid,
    )
    await SingleAgentWorkflowRunner(
        dao,
        second_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=1000,
        openbear_session_uuid="openbear-1",
        agent_session_uuid=agent_session.session_uuid,
    ).run()

    assert len(backend.calls) == 2
    second_prompt = backend.calls[1]["messages"][0]["content"]
    assert "再说模块 B 的细节" in second_prompt
    assert "当前 Agent Session 历史" in second_prompt
    assert "模块 A 结论" in second_prompt
    first_session_id = safe_agent_llm_session_id(agent_session.session_uuid, first_uuid, "reader")
    second_session_id = safe_agent_llm_session_id(agent_session.session_uuid, second_uuid, "reader")
    # Durable Agent history is carried explicitly in the second prompt.  Hidden
    # upstream reasoning/cache state must remain isolated per task.
    assert backend.calls[0]["session_id"] == first_session_id
    assert backend.calls[1]["session_id"] == second_session_id
    assert backend.calls[0]["session_id"] != backend.calls[1]["session_id"]
    assert backend.calls[1]["session_id"].isascii()
    await db.close()


def test_safe_agent_llm_session_id_is_ascii_for_chinese_agent_key():
    value = safe_agent_llm_session_id("session-uuid", "task-uuid", "项目开发专家")
    assert value.startswith("rath:session-uuid:task-")
    assert ":agent-" in value
    assert value.isascii()
    assert "项目开发专家" not in value


def test_safe_agent_llm_session_id_isolates_parallel_tasks_but_stays_stable_for_continuation():
    first = safe_agent_llm_session_id("shared-agent-session", "task-a", "general-purpose")
    second = safe_agent_llm_session_id("shared-agent-session", "task-b", "general-purpose")
    assert first != second
    assert first == safe_agent_llm_session_id("shared-agent-session", "task-a", "general-purpose")


async def test_single_agent_model_call_hook_runs_before_task_completes(env):
    dao, task_uuid, agent = env
    observed = []

    class UsageBackend:
        protocol = "responses"

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            return AgentResult(
                text="done", usage=Usage(input_tokens=11, output_tokens=3), finish_reason="stop"
            )

    async def on_call(detail):
        current = await dao.get_task(task_uuid)
        observed.append((detail, current.status, current.model_call_count, current.input_tokens))

    output = await SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=UsageBackend(),
        model="gpt",
        max_tokens=2048,
        on_model_call=on_call,
    ).run()

    assert output["summary"] == "done"
    assert len(observed) == 1
    detail, status, calls, input_tokens = observed[0]
    assert detail["inputTokens"] == 11
    assert detail["outputTokens"] == 3
    assert status == "running"
    assert calls == 1
    assert input_tokens == 11


async def test_single_agent_tool_round_persists_reasoning_progress(env):
    dao, task_uuid, agent = env
    reg = ToolRegistry()
    captured: list[list[dict]] = []

    async def read(_args):
        return "evidence"

    class ReasoningBackend:
        protocol = "responses"

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            captured.append([dict(message) for message in messages])
            if len(captured) == 1:
                return AgentResult(
                    reasoning="已检查认证；下一步只验证授权后即可收口。",
                    tool_calls=[
                        ToolCall(id="read-authz", name="Read", arguments='{"path":"auth.py"}')
                    ],
                    usage=Usage(input_tokens=1, output_tokens=1),
                    finish_reason="tool_calls",
                )
            return AgentResult(
                text="最终报告", usage=Usage(input_tokens=1, output_tokens=1), finish_reason="stop"
            )

    reg.add("Read", "read", {"type": "object", "properties": {}}, read)
    output = await SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=ReasoningBackend(),
        model="gpt",
        max_tokens=2048,
        tools=reg,
    ).run()

    assert output["summary"] == "最终报告"
    tool_turn = next(
        message
        for message in captured[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert tool_turn["reasoning"] == "已检查认证；下一步只验证授权后即可收口。"
    assert "Task-local reasoning/progress" in tool_turn["content"]
    assert "下一步只验证授权后即可收口" in tool_turn["content"]


async def test_single_agent_budget_pause_can_continue_same_task(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = []
    memories = TaskMemoryDAO(dao.db)
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_CONVERSATION,
        name="continuation shared",
        description="stable",
        visible_to_agents=True,
    )
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=task_uuid,
        name="continuation private",
        description="stable",
    )

    class LoopBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.calls = 0
            self.messages_at_continue = None
            self.seen_messages: list[list[dict]] = []

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.seen_messages.append(copy.deepcopy(messages))
            self.calls += 1
            if self.calls <= 2:
                return AgentResult(
                    tool_calls=[ToolCall(id=f"noop{self.calls}", name="Noop", arguments="{}")],
                    text=f"第 {self.calls} 轮",
                    finish_reason="tool_calls",
                )
            self.messages_at_continue = messages
            return AgentResult(text="结论：继续后完成")

    reg = ToolRegistry()

    async def noop(_args):
        return "ok"

    reg.add("Noop", "noop", {"type": "object", "properties": {}}, noop)
    agent.tool_allowlist = ["Noop"]
    backend = LoopBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
        model_call_limit=2,
        tool_call_limit=10,
    )

    output = await runner.run()

    assert backend.calls == 2
    assert output["status"] == "needs_openbear_control"
    assert output["reason"] == "agent_task_budget_exceeded"
    assert output["detail"]["continueTool"]["name"] == "AgentMessage"
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "needs_openbear_control"
    artifacts = await dao.artifacts(task_uuid)
    assert any(a.kind == "agent_continuation_state" for a in artifacts)

    continued = await runner.run_continue("继续，只补剩余检查点")

    assert continued["continued"] is True
    assert continued["summary"] == "结论：继续后完成"
    assert backend.calls == 3
    assert backend.messages_at_continue is not None
    assert backend.seen_messages[2][: len(backend.seen_messages[1])] == backend.seen_messages[1]
    assert (
        len(
            [
                message
                for message in backend.messages_at_continue
                if is_task_memory_runtime_message(message)
            ]
        )
        == 1
    )
    assert "继续，只补剩余检查点" in str(backend.messages_at_continue[-1]["content"])
    private_checkpoint = await dao.task_model_context(task_uuid)
    assert private_checkpoint is not None and private_checkpoint["protocol"] == "chat"
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "completed"
    events = await dao.events(task_uuid)
    assert any(e.kind == "agent_budget_exhausted" for e in events)
    assert any(e.kind == "agent_task_continued" for e in events)


async def test_single_agent_user_prompt_uses_evidence_based_convergence_without_budget_salience(
    env,
):
    dao, task_uuid, agent = env
    agent.tool_allowlist = []

    class CaptureBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.first_user = ""

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.first_user = str(messages[0]["content"])
            return AgentResult(text="结论：完成")

    backend = CaptureBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        model_call_limit=3,
        tool_call_limit=5,
    )

    await runner.run()

    assert "Rath Agent Session" in backend.first_user
    assert "最多 3 次" not in backend.first_user
    assert "最多 5 次" not in backend.first_user
    assert "AgentMessage" not in backend.first_user
    assert "输出中文 Markdown" not in backend.first_user
    assert "输出语言以 task brief 明确要求为准" in backend.first_user
    assert "未指定时跟随任务的主要语言" in backend.first_user
    assert "批量搜索、批量读取相关片段" in backend.first_user
    assert "读一个文件 → 重新推理 → 再读一个文件" in backend.first_user
    assert "仍未满足的 Plan criterion" in backend.first_user
    assert "立即冻结证据并成稿" in backend.first_user
    assert "任务方向、真实阻塞、风险边界" in backend.first_user


async def test_tool_budget_pause_persists_only_paired_tool_calls(env):
    dao, task_uuid, agent = env
    agent.tool_allowlist = ["Read"]

    class MultiToolBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.calls = 0
            self.messages_at_continue = None

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.calls += 1
            if self.calls == 1:
                return AgentResult(
                    text="需要读取两个来源",
                    tool_calls=[
                        ToolCall(id="a", name="Read", arguments='{"n":1}'),
                        ToolCall(id="b", name="Read", arguments='{"n":2}'),
                    ],
                    finish_reason="tool_calls",
                )
            self.messages_at_continue = messages
            assistant_calls = [c.id for m in messages for c in (m.get("tool_calls") or [])]
            tool_outputs = [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]
            assert assistant_calls == tool_outputs
            assert "b" in assistant_calls
            return AgentResult(text="结论：预算暂停的待执行工具已完成后收口")

    reg = ToolRegistry()
    noop_calls: list[int] = []

    async def noop(args):
        noop_calls.append(int(args.get("n")))
        return f"ok-{args.get('n')}"

    reg.add(
        "Read",
        "read",
        {"type": "object", "properties": {"n": {"type": "number"}}},
        noop,
        visibility={"agent"},
    )
    backend = MultiToolBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=reg,
        model_call_limit=10,
        tool_call_limit=1,
    )

    output = await runner.run()

    assert output["status"] == "needs_openbear_control"
    assert output["detail"]["budgetKind"] == "tool"
    assert output["detail"]["pendingToolCalls"][0]["id"] == "b"
    artifacts = await dao.artifacts(task_uuid)
    state_artifact = [a for a in artifacts if a.kind == "agent_continuation_state"][-1]
    import json

    state = json.loads(state_artifact.content)
    assistant_calls = [c["id"] for m in state["messages"] for c in (m.get("tool_calls") or [])]
    tool_outputs = [m.get("tool_call_id") for m in state["messages"] if m.get("role") == "tool"]
    assert assistant_calls == ["a"]
    assert tool_outputs == ["a"]

    continued = await runner.run_continue("继续完成暂停点剩余工作后总结")

    assert continued["summary"] == "结论：预算暂停的待执行工具已完成后收口"
    assert backend.messages_at_continue is not None
    assert noop_calls == [1, 2]


async def test_task_memory_requires_explicit_agent_tool_grant(env):
    dao, task_uuid, agent = env
    registry = ToolRegistry()
    register_task_memory_tool(registry, TaskMemoryDAO(dao.db))
    without_grant = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=registry,
        plan_protocol_enabled=False,
    )
    assert "TaskMemory" not in {
        schema["name"] for schema in await without_grant._allowed_tool_schemas()
    }

    with_grant = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=replace(agent, tool_allowlist=["TaskMemory"]),
        backend=_Backend(),
        model="gpt",
        max_tokens=2048,
        tools=registry,
        plan_protocol_enabled=False,
    )
    assert "TaskMemory" in {schema["name"] for schema in await with_grant._allowed_tool_schemas()}


async def test_agent_task_memory_state_keeps_cross_second_tool_round_provider_prefix(env):
    dao, task_uuid, agent = env
    memories = TaskMemoryDAO(dao.db)
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_CONVERSATION,
        name="shared",
        description="conversation state",
        visible_to_agents=True,
    )
    own, _ = await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=task_uuid,
        name="own",
        description="initial task state",
    )
    calls: list[list[dict]] = []
    systems: list[str] = []
    schemas: list[list[dict]] = []
    physical_seconds: list[int] = []

    class PrefixBackend:
        protocol = "chat"

        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            calls.append(copy.deepcopy(messages))
            systems.append(system)
            schemas.append(copy.deepcopy(tools or []))
            physical_seconds.append(int(time.time()))
            if len(calls) == 1:
                yield StreamEvent(
                    kind="tool_call",
                    tool_calls=[ToolCall(id="read-1", name="Read", arguments="{}")],
                )
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
            else:
                yield StreamEvent(kind="content", text="prefix stable")
                yield StreamEvent(kind="finish", finish_reason="stop")

    async def mutate_catalog(_args):
        nonlocal own
        await asyncio.sleep(1.05)
        own = await memories.update(
            own["memoryUuid"],
            conversation_uuid="session-1",
            scope_type=SCOPE_AGENT_TASK,
            task_uuid=task_uuid,
            expected_revision=own["revision"],
            changes={"description": "intermediate task state"},
        )
        own = await memories.update(
            own["memoryUuid"],
            conversation_uuid="session-1",
            scope_type=SCOPE_AGENT_TASK,
            task_uuid=task_uuid,
            expected_revision=own["revision"],
            changes={"description": "latest task state"},
        )
        return "catalog mutated twice"

    registry = ToolRegistry()
    registry.add("Read", "mutate catalog", {"type": "object", "properties": {}}, mutate_catalog)
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=PrefixBackend(),
        model="gpt",
        max_tokens=2048,
        tools=registry,
        plan_protocol_enabled=False,
    )
    output = await runner.run()

    assert output["summary"] == "prefix stable"
    assert len(calls) == 2
    assert physical_seconds[1] > physical_seconds[0]
    assert calls[1][: len(calls[0])] == calls[0]
    assert systems[0].encode() == systems[1].encode()
    assert schemas[0] == schemas[1]
    first_states = [message for message in calls[0] if is_task_memory_runtime_message(message)]
    second_states = [message for message in calls[1] if is_task_memory_runtime_message(message)]
    assert len(first_states) == 1
    assert len(second_states) == 2
    assert second_states[0] == first_states[0]
    assert "latest task state" in second_states[-1]["content"]
    assert "intermediate task state" not in second_states[-1]["content"]
    assert "[⏰ 当前时间:" not in json.dumps(calls, ensure_ascii=False, default=str)

    checkpoint = await dao.task_model_context(task_uuid)
    assert checkpoint is not None and checkpoint["protocol"] == "chat"
    checkpoint_messages = checkpoint["state"]["messages"]
    assert (
        len([message for message in checkpoint_messages if is_task_memory_runtime_message(message)])
        == 2
    )


async def test_agent_compaction_rebuild_injects_latest_task_memory_runtime_only(env):
    dao, task_uuid, agent = env
    memories = TaskMemoryDAO(dao.db)
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_CONVERSATION,
        name="shared visible",
        description="from conversation",
        body="shared-secret-body",
        visible_to_agents=True,
    )
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_CONVERSATION,
        name="shared hidden",
        description="must stay hidden",
        body="hidden-secret-body",
        visible_to_agents=False,
    )
    await memories.create(
        conversation_uuid="session-1",
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=task_uuid,
        name="own task",
        description="from current task",
        body="own-secret-body",
    )

    class CaptureBackend:
        protocol = "chat"

        def __init__(self):
            self.messages = []
            self.system = ""

        async def complete(
            self, messages, *, model, system="", tools=None, max_tokens=8192, **opts
        ):
            self.messages = messages
            self.system = system
            return AgentResult(text="catalog rebuilt", usage=Usage(input_tokens=1, output_tokens=1))

    backend = CaptureBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=2048,
        tools=ToolRegistry(),
        plan_protocol_enabled=False,
    )

    compacted_messages = []

    async def simulate_compaction(messages):
        messages[:] = [{"role": "user", "content": "compacted task prompt"}]
        compacted_messages[:] = messages
        return True

    runner._pre_compact_context_if_needed = simulate_compaction
    output = await runner.run()
    assert output["summary"] == "catalog rebuilt"
    user_text = str(backend.messages[-1]["content"])
    assert "<conversation-memory revision=" in user_text
    assert "shared visible" in user_text
    assert "shared hidden" not in user_text
    assert "<agent-task-memory revision=" in user_text
    assert "own task" in user_text
    assert "shared-secret-body" not in user_text
    assert "hidden-secret-body" not in user_text
    assert "own-secret-body" not in user_text
    assert "[⏰ 当前时间:" not in user_text
    assert "不是新的用户任务" in user_text
    assert "conversation-memory" not in backend.system
    assert "agent-task-memory" not in backend.system

    durable_state = json.dumps(compacted_messages, ensure_ascii=False)
    assert "conversation-memory" not in durable_state
    assert "agent-task-memory" not in durable_state
    assert "shared-secret-body" not in durable_state
    assert "own-secret-body" not in durable_state
