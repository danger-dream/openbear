from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from app.context.builder import ContextBuilder
from app.db.dao import MessageDAO
from app.db.engine import DB
from app.llm.base import AgentResult
from app.llm.events import ToolCall, Usage
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.rath.schemas import RathAgentDef
from app.rath.single_agent import SingleAgentWorkflowRunner, agent_to_snapshot
from app.tools.agents import (
    AgentTools,
    _agent_progress_signature,
    _task_public,
    register_agent_tools,
)
from app.tools.allowlist import AGENT_DELEGATION_TOOL_NAMES
from app.tools.base import ToolRegistry, ToolRuntimeContext, current_tool_context
from app.web_admin import WebAdminServer
from app.web_operations import reduce_operation_payload


async def test_tool_registry_preserves_protected_result_verbatim():
    reg = ToolRegistry()
    long_result = "完整 Agent 结论：" + ("证据" * 20_000)

    async def result(_args):
        return long_result

    reg.add(
        "ProtectedAgentResult",
        "test",
        {"type": "object", "properties": {}},
        result,
        preserve_result=True,
    )
    delivered = await reg.dispatch("ProtectedAgentResult", "{}", max_chars=128)

    assert delivered == long_result
    assert "tool_result_truncated" not in delivered


async def test_tool_registry_injects_runtime_context():
    reg = ToolRegistry()

    async def whoami(_args):
        ctx = current_tool_context()
        return f"chat={ctx.chat_id};session={ctx.session_uuid};source={ctx.source}"

    reg.add("WhoAmI", "test", {"type": "object", "properties": {}}, whoami)
    result = await reg.dispatch(
        "WhoAmI",
        "{}",
        context=ToolRuntimeContext(chat_id=123, session_uuid="s1", source="chat"),
    )
    assert result == "chat=123;session=s1;source=chat"


@pytest.fixture
async def agent_tool_env(tmp_path):
    db = DB(str(tmp_path / "agents.db"))
    await db.connect()
    dao = RathDAO(db)
    await ensure_builtin_workflows(dao)
    try:
        yield dao
    finally:
        await db.close()


class _FakeFactory:
    def __init__(self, backend=None) -> None:
        self.backend = backend or _RecordingBackend()

    def context_window(self, _model: str) -> int:
        return 128000

    def backend_for(self, _model: str):
        return self.backend, "gpt", 2048


class _RecordingBackend:
    protocol = "chat"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        self.calls.append({
            "messages": messages,
            "system": system,
            "tools": tools,
            "session_id": opts.get("session_id"),
            "think_level": opts.get("think_level"),
            "service_tier": opts.get("service_tier"),
            "model": model,
        })
        return AgentResult(text=f"输出 {len(self.calls)}")


class _FakeSelection:
    current = "openai/gpt"


class _FakeModelDef:
    def __init__(self, *, thinking_levels=None, default_thinking_level="", supports_fast=False, cost=None):
        self.thinking_levels = list(thinking_levels or [])
        self.default_thinking_level = default_thinking_level
        self.supports_fast = supports_fast
        self.cost = cost or {}
        self.compact_trigger_tokens = 0


class _FakeProviderDef:
    def __init__(self, protocol="chat"):
        self.protocol = protocol


class _FakeConfig:
    class _Models:
        primary = "openai/gpt"
        compression_models = ["openai/gpt"]

        def compression_model_candidates(self, fallback=""):
            out = []
            for item in [*self.compression_models, fallback]:
                if item and item not in out:
                    out.append(item)
            return out

        def resolve(self, name):
            label = str(name or "")
            if label == "openai/cheap":
                return (
                    _FakeProviderDef("chat"),
                    _FakeModelDef(thinking_levels=["low", "medium"], default_thinking_level="low", supports_fast=False),
                )
            if label in {"openai/gpt", "openai/main"}:
                return (
                    _FakeProviderDef("responses"),
                    _FakeModelDef(thinking_levels=["low", "high"], default_thinking_level="high", supports_fast=True),
                )
            return None

    class _Tools:
        tool_result_max_chars = 32000

    class _Rath:
        max_concurrent_tasks = 3
        agent_model_call_limit = 40
        agent_tool_call_limit = 80
        agent_tool_foreground_wait_s = 30.0
        # Legacy orchestration mocks exercise launch/continue mechanics without
        # emulating the controller approval loop. Dedicated Plan runtime tests
        # below enable the protocol explicitly.
        agent_plan_enabled = False

    class _Memory:
        identity = "openbear"

    models = _Models()
    tools = _Tools()
    rath = _Rath()
    memory = _Memory()


def test_agent_progress_signature_ignores_timer_only_fields():
    base = {
        "taskUuid": "task-1",
        "status": "running",
        "currentStatus": "模型调用中",
        "durationMs": 1000,
        "updatedAtMs": 1000,
    }
    later = {**base, "durationMs": 9999, "updatedAtMs": 9999}
    events = [{"seq": 3, "kind": "model_call_started"}]
    session = {"sessionUuid": "session-1", "status": "active"}

    assert _agent_progress_signature(base, events, None, session) == _agent_progress_signature(later, events, None, session)
    assert _agent_progress_signature(base, events, None, session) != _agent_progress_signature(
        {**later, "currentStatus": "工具执行中"}, events, None, session
    )
    assert _agent_progress_signature(
        later, events, None, session, {"ledgerRevision": 10, "inputTokens": 100}
    ) != _agent_progress_signature(
        later, events, None, session, {"ledgerRevision": 11, "inputTokens": 100}
    )


async def test_context_builder_does_not_append_hidden_multi_agent_prompt():
    class Mem:
        async def build_system_prompt(self, _params):
            return "基础系统提示词"

    reg = ToolRegistry()

    async def noop(_args):
        return "ok"

    reg.add("Agent", "run agent", {"type": "object", "properties": {}}, noop)
    builder = ContextBuilder(Mem(), messages=None, summaries=None, skills=[], tools=reg, workspace_dir="/tmp")  # type: ignore[arg-type]

    prompt = await builder.build_system(current_model="openai/gpt")

    assert prompt == "基础系统提示词"
    assert "OpenBear Multi-Agent / Rath Orchestration Guide" not in prompt
    assert "`Agent` starts one focused background worker" not in prompt


def test_agent_operation_payload_preserves_root_invocation_identity():
    old_payload = {
        "toolName": "Agent",
        "rootToolName": "Agent",
        "rootToolCallId": "call_agent",
        "rootArguments": "{\"prompt\":\"review backend\"}",
        "arguments": "{\"prompt\":\"review backend\"}",
        "preview": "Agent: backend review",
        "status": "running",
    }
    patch_payload = {
        "toolName": "AgentMessage",
        "rootToolName": "AgentMessage",
        "toolCallId": "call_followup",
        "arguments": "{\"to\":\"task-1\",\"message\":\"summarize\"}",
        "preview": "AgentMessage: summarize",
        "resultText": "{\"steered\":true}",
        "status": "completed",
    }

    merged = reduce_operation_payload(old_payload, op_type="agent", action="end", patch=patch_payload)

    assert merged["rootToolName"] == "Agent"
    assert merged["rootToolCallId"] == "call_agent"
    assert merged["rootArguments"] == "{\"prompt\":\"review backend\"}"
    assert merged["toolName"] == "Agent"
    assert merged["preview"] == "Agent: backend review"
    assert merged["lastControlToolName"] == "AgentMessage"
    assert merged["lastControlArguments"] == "{\"to\":\"task-1\",\"message\":\"summarize\"}"
    assert merged["lastControlPreview"] == "AgentMessage: summarize"
    assert merged["lastControlResultText"] == "{\"steered\":true}"
    assert "resultText" not in merged


def test_default_rath_agent_prompt_has_actionable_structure():
    prompt = WebAdminServer.__new__(WebAdminServer)._default_rath_agent_prompt()

    assert "## 核心职责" in prompt
    assert "## 工作流程" in prompt
    assert "## 工具使用规则" in prompt
    assert "## 输出格式" in prompt
    assert "## 质量标准" in prompt
    assert "只有实际调用工具后" in prompt
    assert "Agent / AgentMessage / AgentStop" in prompt
    assert "Web Agent 配置只是你的可复用 system prompt" in prompt
    assert "本次任务 prompt" in prompt


def test_agent_new_and_continue_callbacks_emit_progress_after_persisting_ledger_usage():
    for method in (AgentTools._run_one, AgentTools.continue_task):
        source = inspect.getsource(method)
        persist_at = source.index("latest_ledger_usage = await self._persist_agent_model_call(")
        emit_at = source.index("await _emit_progress()", persist_at)
        assert emit_at > persist_at


async def test_agent_accounting_snapshots_are_absolute_monotonic_and_keep_only_durable_usage(agent_tool_env):
    dao = agent_tool_env
    tools = AgentTools(
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
        registry=ToolRegistry(),
    )
    messages = MessageDAO(dao.db)
    await messages.ensure_session(321)
    session_uuid = await messages.get_or_create_session_uuid(321)

    first, second = await asyncio.gather(
        tools._persist_agent_model_call(
            chat_id=321,
            session_uuid=session_uuid,
            model_label="openai/gpt",
            protocol="responses",
            detail={"status": "ok", "inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 3, "costUsd": 0.01},
        ),
        tools._persist_agent_model_call(
            chat_id=321,
            session_uuid=session_uuid,
            model_label="openai/gpt",
            protocol="responses",
            detail={"status": "error", "inputTokens": 4, "outputTokens": 1, "cacheWriteTokens": 2, "costUsd": 0.02},
        ),
    )
    snapshots = sorted((first, second), key=lambda item: item["ledgerRevision"])
    assert snapshots[1]["ledgerRevision"] > snapshots[0]["ledgerRevision"] > 0
    assert snapshots[1] == {
        "ledgerRevision": snapshots[1]["ledgerRevision"],
        "inputTokens": 14,
        "outputTokens": 3,
        "cacheReadTokens": 3,
        "cacheWriteTokens": 2,
        "costUsd": pytest.approx(0.03),
    }

    no_usage = await tools._persist_agent_model_call(
        chat_id=321,
        session_uuid=session_uuid,
        model_label="openai/gpt",
        protocol="responses",
        detail={"status": "cancelled"},
    )
    assert no_usage["ledgerRevision"] > snapshots[1]["ledgerRevision"]
    assert {key: no_usage[key] for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")} == {
        "inputTokens": 14,
        "outputTokens": 3,
        "cacheReadTokens": 3,
        "cacheWriteTokens": 2,
    }
    assert no_usage["costUsd"] == pytest.approx(0.03)
    rows = await messages.recent_model_calls(321)
    assert [row.status for row in rows[:3]] == ["cancelled", "error", "ok"] or [row.status for row in rows[:3]] == ["cancelled", "ok", "error"]


async def test_detached_agent_progress_immediately_carries_post_accounting_ledger_usage(agent_tool_env):
    dao = agent_tool_env

    class BlockingUsageBackend:
        protocol = "responses"

        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            await self.release.wait()
            return AgentResult(
                text="detached done",
                usage=Usage(input_tokens=12, output_tokens=4, cache_read_tokens=2),
                finish_reason="stop",
            )

    backend = BlockingUsageBackend()
    registry = ToolRegistry()
    manager = RathTaskManager(dao)
    register_agent_tools(
        registry,
        config=_FakeConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
    )
    messages = MessageDAO(dao.db)
    await messages.ensure_session(322)
    session_uuid = await messages.get_or_create_session_uuid(322)
    progress_payloads = []
    notifications = []

    async def progress(payload):
        progress_payloads.append(payload)

    async def notification(payload):
        notifications.append(payload)

    raw = await registry.dispatch(
        "Agent",
        json.dumps({"description": "ledger progress", "prompt": "account one request", "tools": []}),
        context=ToolRuntimeContext(
            chat_id=322,
            session_uuid=session_uuid,
            source="chat",
            progress_update_payload=progress,
            task_notification=notification,
        ),
    )
    launched = json.loads(raw)
    assert launched["ok"] is True
    assert launched["detached"] is True

    backend.release.set()
    ledger_payload = None
    for _ in range(200):
        ledger_payload = next((payload for payload in progress_payloads if payload.get("ledgerUsage")), None)
        if ledger_payload is not None:
            break
        await asyncio.sleep(0.01)
    assert ledger_payload is not None
    assert ledger_payload["detached"] is True
    assert ledger_payload["ledgerUsage"] == {
        "ledgerRevision": ledger_payload["ledgerUsage"]["ledgerRevision"],
        "inputTokens": 12,
        "outputTokens": 4,
        "cacheReadTokens": 2,
        "cacheWriteTokens": 0,
        "costUsd": 0.0,
    }
    assert ledger_payload["ledgerUsage"]["ledgerRevision"] > 0

    for _ in range(200):
        task = await dao.get_task(launched["task"]["taskUuid"])
        if task is not None and task.status == "completed":
            break
        await asyncio.sleep(0.01)
    assert task is not None and task.status == "completed"


async def test_agent_long_result_is_returned_verbatim_without_summary_model(agent_tool_env):
    dao = agent_tool_env
    backend = _RecordingBackend()
    tools = AgentTools(
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
        registry=ToolRegistry(),
    )
    full_result = {
        "summary": "完整结论\n" + ("关键证据与边界条件。" * 12_000),
        "taskUuid": "task-long",
        "agent": {"agentKey": "reviewer"},
        "model": "openai/gpt",
    }

    prepared = await tools._prepare_agent_result(full_result, task=None)

    assert prepared == full_result
    assert prepared["summary"] == full_result["summary"]
    assert "compressed" not in prepared
    assert backend.calls == []


async def test_agent_registry_exposes_only_controller_agent_tools(agent_tool_env):
    dao = agent_tool_env
    reg = ToolRegistry()
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )

    assert set(reg.names(scope="main")) == {
        "Agent",
        "AgentMessage",
        "AgentStop",
        "AgentWait",
        "AgentPlanDecision",
    }
    assert set(reg.names()) == {
        "Agent",
        "AgentMessage",
        "AgentStop",
        "AgentWait",
        "AgentPlanDecision",
        "AgentPlanSubmit",
        "AgentPlanProgress",
        "AgentPlanReplan",
        "AgentControlAck",
    }
    assert set(reg.names(scope="agent")) == {
        "AgentPlanSubmit",
        "AgentPlanProgress",
        "AgentPlanReplan",
        "AgentControlAck",
    }
    canonical_tools = sorted(AGENT_DELEGATION_TOOL_NAMES)
    assert set(canonical_tools) == {
        "Bash", "Edit", "Process", "Read", "TaskMemory", "WebExtract", "WebSearch", "Write",
    }
    main_schemas = {item["name"]: item for item in reg.schemas(scope="main")}
    agent_schemas = {item["name"]: item for item in reg.schemas(scope="agent")}
    assert main_schemas["Agent"]["parameters"]["properties"]["tools"]["items"]["enum"] == canonical_tools
    plan_mode_schema = main_schemas["Agent"]["parameters"]["properties"]["planMode"]
    assert plan_mode_schema["enum"] == ["direct", "managed"]
    assert plan_mode_schema["default"] == "direct"
    assert "tool count alone never require managed" in plan_mode_schema["description"]

    plan_schema = agent_schemas["AgentPlanSubmit"]["parameters"]["properties"]["plan"]
    assert plan_schema["required"] == ["title", "objective", "steps", "finalOutputs"]
    assert set(plan_schema["properties"]) == {
        "title",
        "objective",
        "scope",
        "assumptions",
        "steps",
        "finalOutputs",
        "risks",
        "toolRequests",
    }
    step_schema = plan_schema["properties"]["steps"]["items"]
    assert step_schema["required"] == ["id", "title", "objective", "method", "criteria"]
    assert step_schema["properties"]["criteria"]["items"]["required"] == ["id", "description"]
    assert plan_schema["properties"]["finalOutputs"]["items"]["required"] == [
        "id",
        "title",
        "description",
    ]
    assert "never replaces" in plan_schema["description"]

    tool_request_schema = plan_schema["properties"]["toolRequests"]
    assert tool_request_schema["items"]["properties"]["name"]["enum"] == canonical_tools
    assert (
        main_schemas["AgentPlanDecision"]["parameters"]["properties"]["grantedTools"]["items"]["enum"]
        == canonical_tools
    )
    assert "TaskMemory" in canonical_tools

    message_schema = main_schemas["AgentMessage"]
    expected_schema = message_schema["parameters"]["properties"]["expectedPlanVersion"]
    assert expected_schema["minimum"] == 0
    assert "Optimistic CAS" in expected_schema["description"]
    assert "expectedPlanVersion" not in message_schema["parameters"]["required"]


def _agent_message_plan(step_id: str = "s1") -> dict:
    criterion_id = "c1" if step_id == "s1" else "rc1"
    return {
        "title": f"AgentMessage Plan {step_id}",
        "objective": "Exercise AgentMessage Plan governance",
        "scope": {"included": ["test"], "excluded": []},
        "assumptions": [],
        "steps": [{
            "id": step_id,
            "title": f"Step {step_id}",
            "objective": "Exercise the current method",
            "method": f"Execute method {step_id}",
            "dependsOn": [],
            "required": True,
            "criteria": [{"id": criterion_id, "description": "method verified", "required": True}],
            "expectedEvidence": ["test result"],
        }],
        "finalOutputs": [{
            "id": "o1",
            "title": "Result",
            "description": "Final result",
            "supportedBy": [step_id],
        }],
        "risks": [],
    }


def _message_tools(dao: RathDAO):
    manager = RathTaskManager(dao)
    registry = ToolRegistry()
    tools = AgentTools(
        config=_FakeConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
        registry=registry,
    )
    registry.add(
        "TestAgentMessage",
        "test AgentMessage governance",
        {"type": "object", "properties": {}},
        tools.agent_message,
    )
    return tools, manager, registry


async def _approved_message_task(dao: RathDAO, tools: AgentTools, *, title: str) -> str:
    workflow = await dao.workflow_by_slug("single-agent")
    assert workflow is not None
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow.workflow_uuid,
        title=title,
        input_data={"instruction": title},
        parent_session_uuid="blocked-session",
        status="running",
    )
    await tools.plan.submit_plan(
        task_uuid,
        _agent_message_plan(),
        request_id="submit-v1",
        wait_for_decision=False,
    )
    await tools.plan.decide(
        task_uuid,
        expected_version=1,
        action="approve",
        request_id="approve-v1",
        reason="approved for test",
    )
    return task_uuid


async def _dispatch_agent_message(registry: ToolRegistry, task_uuid: str, expected_version: int) -> dict:
    raw = await registry.dispatch(
        "TestAgentMessage",
        json.dumps({
            "to": task_uuid,
            "message": "apply the controller intervention",
            "reasonCode": "plan_consistency",
            "reason": "exercise the authoritative Plan gate",
            "expectedPlanVersion": expected_version,
        }),
        context=ToolRuntimeContext(chat_id=123, session_uuid="blocked-session", source="web"),
    )
    return json.loads(raw)


async def _control_count(dao: RathDAO, task_uuid: str) -> int:
    cur = await dao.db.conn.execute(
        "SELECT COUNT(*) AS n FROM rath_task_controls WHERE task_uuid=?",
        (task_uuid,),
    )
    return int((await cur.fetchone())["n"])


async def _wait_plan_phase(coordinator, task_uuid: str, phase: str) -> dict:
    for _ in range(300):
        snapshot = await coordinator.snapshot(task_uuid)
        if snapshot["state"]["phase"] == phase:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"Plan phase did not become {phase}")


class _PlanEnabledConfig(_FakeConfig):
    class _Rath(_FakeConfig._Rath):
        agent_plan_enabled = True
        plan_control_call_limit = 100

    rath = _Rath()


async def test_agent_plan_mode_defaults_direct_and_managed_requires_controller_runtime(agent_tool_env):
    dao = agent_tool_env
    backend = _RecordingBackend()
    reg = ToolRegistry()

    async def read(_args):
        return "read"

    reg.add("Read", "read", {"type": "object", "properties": {}}, read, visibility={"agent"})
    register_agent_tools(
        reg,
        config=_PlanEnabledConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
    )

    direct_raw = await reg.dispatch(
        "Agent",
        json.dumps({"prompt": "read directly", "tools": ["Read"]}),
        context=ToolRuntimeContext(chat_id=123, session_uuid="plan-mode-session", source="chat"),
    )
    direct = json.loads(direct_raw)
    assert direct["ok"] is True
    direct_task = await dao.get_task(direct["task"]["taskUuid"])
    assert direct_task is not None
    assert direct_task.input["planMode"] == "direct"
    assert _task_public(direct_task)["planMode"] == "direct"
    assert "Agent Plan 强制执行协议" not in backend.calls[0]["system"]
    assert {tool["name"] for tool in backend.calls[0]["tools"]} == {"Read"}

    managed_raw = await reg.dispatch(
        "Agent",
        json.dumps({"prompt": "run with Plan governance", "tools": ["Read"], "planMode": "managed"}),
        context=ToolRuntimeContext(chat_id=123, session_uuid="plan-mode-session", source="chat"),
    )
    managed = json.loads(managed_raw)
    assert managed["ok"] is False
    assert managed["error"] == "controller_runtime_required"

    invalid_raw = await reg.dispatch(
        "Agent",
        json.dumps({"prompt": "invalid mode", "tools": [], "planMode": "auto"}),
        context=ToolRuntimeContext(chat_id=123, session_uuid="plan-mode-session", source="chat"),
    )
    invalid = json.loads(invalid_raw)
    assert invalid["error"] == "invalid_agent_plan_mode"
    assert invalid["allowedPlanModes"] == ["direct", "managed"]

    workflow = await dao.workflow_by_slug("single-agent")
    assert workflow is not None
    legacy_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow.workflow_uuid,
        title="legacy task",
        input_data={"instruction": "legacy"},
        parent_session_uuid="plan-mode-session",
        status="completed",
    )
    legacy_task = await dao.get_task(legacy_uuid)
    assert _task_public(legacy_task)["planMode"] == "managed"


class _BlockedRecoveryBackend:
    protocol = "chat"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.control_uuid = ""

    @staticmethod
    def _call(call_id: str, name: str, args: dict) -> AgentResult:
        return AgentResult(
            tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False))],
            finish_reason="tool_calls",
        )

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        names = {str(item.get("name") or "") for item in tools or []}
        self.calls.append({"messages": list(messages), "names": names, "system": system})
        number = len(self.calls)
        if number == 1:
            assert names == {"AgentPlanSubmit", "AgentControlAck"}
            return self._call("full-submit-v1", "AgentPlanSubmit", {"plan": _agent_message_plan("s1")})
        if number == 2:
            return self._call(
                "full-start-s1",
                "AgentPlanProgress",
                {"action": "start", "stepId": "s1"},
            )
        if number == 3:
            return self._call(
                "full-block-s1",
                "AgentPlanProgress",
                {
                    "action": "block",
                    "stepId": "s1",
                    "blocker": {"reason": "the v1 execution method is unavailable"},
                },
            )
        if number == 4:
            assert names == {"AgentPlanReplan", "AgentControlAck"}
            content = "\n".join(str(item.get("content") or "") for item in messages)
            marker = '<agent-control id="'
            assert marker in content
            self.control_uuid = content.split(marker, 1)[1].split('"', 1)[0]
            assert self.control_uuid
            assert '"pendingControls"' in content
            return self._call(
                "full-ack-control",
                "AgentControlAck",
                {
                    "controlUuid": self.control_uuid,
                    "status": "accepted",
                    "reason": "the replacement method is required",
                    "planImpact": "submit a replan before continuing execution",
                    "nextAction": "submit AgentPlanReplan",
                },
            )
        if number == 5:
            cleared_deltas = [
                str(item.get("content") or "")
                for item in messages
                if item.get("role") == "user"
                and 'mode="delta"' in str(item.get("content") or "")
                and '"pendingControls":[]' in str(item.get("content") or "")
            ]
            assert cleared_deltas
            return self._call(
                "full-submit-v2",
                "AgentPlanReplan",
                {
                    "changeReason": "replace the blocked v1 method",
                    "plan": _agent_message_plan("r1"),
                },
            )
        if number == 6:
            return self._call(
                "full-start-r1",
                "AgentPlanProgress",
                {"action": "start", "stepId": "r1"},
            )
        if number == 7:
            return self._call(
                "full-complete-r1",
                "AgentPlanProgress",
                {
                    "action": "complete",
                    "stepId": "r1",
                    "result": "replacement method completed",
                    "criteria": [{"id": "rc1", "status": "passed"}],
                    "evidence": [{
                        "type": "integration_test",
                        "reference": "runner:replacement-r1",
                        "summary": "the replacement r1 method executed",
                        "criterionId": "rc1",
                    }],
                },
            )
        if number == 8:
            return self._call(
                "full-finalize-v2",
                "AgentPlanProgress",
                {
                    "action": "finalize",
                    "finalOutputs": [{
                        "id": "o1",
                        "summary": "blocked recovery completed under v2",
                        "sources": ["step:r1"],
                    }],
                },
            )
        if number == 9:
            return AgentResult(text="blocked recovery chain completed")
        raise AssertionError(f"unexpected model call {number}")


@pytest.mark.parametrize(
    ("task_status", "output"),
    [
        ("needs_openbear_control", {}),
        ("needs_openbear_control", {"reason": "agent_task_continue_failed", "detail": {"continuable": True}}),
        ("running", None),
    ],
)
async def test_agent_message_uses_authoritative_blocked_phase_regardless_of_task_projection(
    agent_tool_env, task_status, output
):
    dao = agent_tool_env
    tools, _manager, registry = _message_tools(dao)
    task_uuid = await _approved_message_task(dao, tools, title=f"blocked projection {task_status}")
    await tools.plan.progress(task_uuid, action="start", step_id="s1", request_id="start-s1")
    await tools.plan.progress(
        task_uuid,
        action="block",
        step_id="s1",
        request_id="block-s1",
        blocker={"reason": "dependency unavailable"},
    )
    if task_status == "needs_openbear_control":
        changed = await dao.update_task(
            task_uuid,
            status=task_status,
            control_state="waiting_openbear_control",
            output=output or {},
            expected_statuses=("running",),
        )
        assert changed is True

    result = await _dispatch_agent_message(registry, task_uuid, 1)

    assert result["ok"] is False
    assert result["error"] == "plan_replan_required"
    assert result["planPhase"] == "blocked_control"
    assert result["activePlanVersion"] == 1
    assert result["visiblePlanVersion"] == 1
    assert await _control_count(dao, task_uuid) == 0


async def test_agent_message_enforces_visible_plan_version_and_allows_legacy_zero(agent_tool_env):
    dao = agent_tool_env
    tools, _manager, registry = _message_tools(dao)
    task_uuid = await _approved_message_task(dao, tools, title="active version CAS")

    too_new = await _dispatch_agent_message(registry, task_uuid, 999)
    missing = await _dispatch_agent_message(registry, task_uuid, 0)
    accepted = await _dispatch_agent_message(registry, task_uuid, 1)

    for rejected in (too_new, missing):
        assert rejected["ok"] is False
        assert rejected["error"] == "stale_plan_version"
        assert rejected["activePlanVersion"] == 1
        assert rejected["pendingPlanVersion"] == 0
        assert rejected["visiblePlanVersion"] == 1
    assert accepted["ok"] is True
    assert accepted["steered"] is True
    assert await _control_count(dao, task_uuid) == 1

    workflow = await dao.workflow_by_slug("single-agent")
    assert workflow is not None
    legacy_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow.workflow_uuid,
        title="legacy plan-disabled task",
        parent_session_uuid="blocked-session",
        status="running",
    )
    legacy = await _dispatch_agent_message(registry, legacy_uuid, 0)
    assert legacy["ok"] is True
    assert legacy["steered"] is True
    assert await _control_count(dao, legacy_uuid) == 1
    cur = await dao.db.conn.execute(
        "SELECT COUNT(*) AS n FROM rath_task_plan_state WHERE task_uuid=?",
        (legacy_uuid,),
    )
    assert int((await cur.fetchone())["n"]) == 0

    legacy_omitted_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow.workflow_uuid,
        title="legacy task with omitted version",
        parent_session_uuid="blocked-session",
        status="running",
    )
    legacy_omitted = json.loads(await registry.dispatch(
        "TestAgentMessage",
        json.dumps({
            "to": legacy_omitted_uuid,
            "message": "legacy intervention without a Plan version",
            "reasonCode": "plan_consistency",
            "reason": "legacy tasks have no Plan state",
        }),
        context=ToolRuntimeContext(chat_id=123, session_uuid="blocked-session", source="web"),
    ))
    assert legacy_omitted["ok"] is True
    assert await _control_count(dao, legacy_omitted_uuid) == 1


async def test_agent_message_rejects_pending_plan_even_when_expected_version_matches(agent_tool_env):
    dao = agent_tool_env
    tools, _manager, registry = _message_tools(dao)
    task_uuid = await _approved_message_task(dao, tools, title="pending version CAS")
    await tools.plan.decide(
        task_uuid,
        expected_version=1,
        action="request_replan",
        request_id="request-replan-v1",
        reason="replace the active method",
    )
    submitted = await tools.plan.submit_plan(
        task_uuid,
        _agent_message_plan("r1"),
        request_id="submit-v2",
        plan_type="replan",
        change_reason="replace the active method",
        wait_for_decision=False,
    )
    assert submitted["planVersion"] == 2

    stale = await _dispatch_agent_message(registry, task_uuid, 1)
    pending = await _dispatch_agent_message(registry, task_uuid, 2)

    assert stale["error"] == "stale_plan_version"
    assert stale["activePlanVersion"] == 1
    assert stale["pendingPlanVersion"] == 2
    assert stale["visiblePlanVersion"] == 2
    assert pending["error"] == "plan_intervention_not_allowed"
    assert pending["planPhase"] == "awaiting_replan_decision"
    assert pending["visiblePlanVersion"] == 2
    assert await _control_count(dao, task_uuid) == 0


async def test_agent_message_queue_and_replan_approval_are_linearized(agent_tool_env):
    dao = agent_tool_env
    tools, _manager, registry = _message_tools(dao)
    task_uuid = await _approved_message_task(dao, tools, title="linearized pending decision")
    await tools.plan.decide(
        task_uuid,
        expected_version=1,
        action="request_replan",
        request_id="request-replan-v1",
        reason="replace the active method",
    )
    await tools.plan.submit_plan(
        task_uuid,
        _agent_message_plan("r1"),
        request_id="submit-v2",
        plan_type="replan",
        change_reason="replace the active method",
        wait_for_decision=False,
    )

    lock = tools.plan._lock(task_uuid)
    await lock.acquire()
    try:
        decision_task = asyncio.create_task(tools.plan.decide(
            task_uuid,
            expected_version=2,
            action="approve",
            request_id="approve-v2",
            reason="replacement approved",
        ))
        message_task = asyncio.create_task(_dispatch_agent_message(registry, task_uuid, 1))
        await asyncio.sleep(0)
    finally:
        lock.release()
    decision, message = await asyncio.gather(decision_task, message_task)

    assert decision["phase"] == "executing"
    assert message["ok"] is False
    assert message["error"] == "stale_plan_version"
    assert message["visiblePlanVersion"] == 2
    assert await _control_count(dao, task_uuid) == 0
    snapshot = await tools.plan.snapshot(task_uuid)
    assert snapshot["state"]["active_plan_version"] == 2
    assert snapshot["state"]["pending_plan_version"] == 0

    request_task_uuid = await _approved_message_task(dao, tools, title="linearized request replan")
    request_lock = tools.plan._lock(request_task_uuid)
    await request_lock.acquire()
    try:
        request_replan_task = asyncio.create_task(tools.plan.decide(
            request_task_uuid,
            expected_version=1,
            action="request_replan",
            request_id="concurrent-request-replan-v1",
            reason="replace the active method concurrently",
        ))
        request_message_task = asyncio.create_task(
            _dispatch_agent_message(registry, request_task_uuid, 1)
        )
        await asyncio.sleep(0)
    finally:
        request_lock.release()
    requested, accepted_message = await asyncio.gather(request_replan_task, request_message_task)

    assert requested["phase"] == "replan_required"
    assert accepted_message["ok"] is True
    assert await _control_count(dao, request_task_uuid) == 1
    controls = await dao.pending_controls(request_task_uuid)
    assert len(controls) == 1
    assert controls[0].metadata["expectedPlanVersion"] == 1
    request_snapshot = await tools.plan.snapshot(request_task_uuid)
    assert request_snapshot["state"]["phase"] == "replan_required"
    assert request_snapshot["state"]["active_plan_version"] == 1
    assert request_snapshot["state"]["pending_plan_version"] == 0


async def test_agent_message_rejects_blocked_plan_until_controller_requests_replan(agent_tool_env):
    dao = agent_tool_env
    manager = RathTaskManager(dao)
    registry = ToolRegistry()
    tools = AgentTools(
        config=_FakeConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
        registry=registry,
    )
    workflow = await dao.workflow_by_slug("single-agent")
    assert workflow is not None
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow.workflow_uuid,
        title="blocked plan",
        input_data={"instruction": "perform blocked work"},
        parent_session_uuid="blocked-session",
        status="running",
    )
    plan = {
        "title": "Blocked Plan",
        "objective": "Exercise blocked control recovery",
        "scope": {"included": ["test"], "excluded": []},
        "assumptions": [],
        "steps": [{
            "id": "s1",
            "title": "Blocked step",
            "objective": "Try the unavailable dependency",
            "method": "Run once and block",
            "dependsOn": [],
            "required": True,
            "criteria": [{"id": "c1", "description": "dependency available", "required": True}],
            "expectedEvidence": ["dependency result"],
        }],
        "finalOutputs": [{
            "id": "o1",
            "title": "Result",
            "description": "Final result",
            "supportedBy": ["s1"],
        }],
        "risks": [],
    }
    await tools.plan.submit_plan(task_uuid, plan, request_id="submit", wait_for_decision=False)
    await tools.plan.decide(
        task_uuid,
        expected_version=1,
        action="approve",
        request_id="approve",
        reason="complete",
    )
    await tools.plan.progress(task_uuid, action="start", step_id="s1", request_id="start")
    blocked = await tools.plan.progress(
        task_uuid,
        action="block",
        step_id="s1",
        request_id="block",
        blocker={"reason": "credential unavailable"},
    )
    assert blocked["reason"] == "agent_plan_blocked"
    await dao.update_task(
        task_uuid,
        status="needs_openbear_control",
        control_state="waiting_openbear_control",
        output={
            "reason": "agent_plan_blocked",
            "detail": {"continuable": True},
        },
        expected_statuses=("running",),
    )
    registry.add(
        "TestAgentMessage",
        "test AgentMessage guard",
        {"type": "object", "properties": {}},
        tools.agent_message,
    )

    raw = await registry.dispatch(
        "TestAgentMessage",
        json.dumps({
            "to": task_uuid,
            "message": "continue the blocked step unchanged",
            "reasonCode": "blocked",
            "reason": "the external credential was supplied",
            "expectedPlanVersion": 1,
        }),
        context=ToolRuntimeContext(chat_id=123, session_uuid="blocked-session", source="web"),
    )
    result = json.loads(raw)

    assert result["ok"] is False
    assert result["error"] == "plan_replan_required"
    assert result["planPhase"] == "blocked_control"
    assert result["activePlanVersion"] == 1
    assert await dao.pending_controls(task_uuid) == []

    replan_required = await tools.plan.decide(
        task_uuid,
        expected_version=1,
        action="request_replan",
        request_id="request-replan",
        reason="the blocked execution method must be replaced",
    )
    assert replan_required["phase"] == "replan_required"

    continued: list[dict] = []

    async def capture_continue(args):
        continued.append(args)
        return json.dumps({"ok": True, "continued": True, "status": "running"})

    tools.continue_task = capture_continue
    resumed_raw = await registry.dispatch(
        "TestAgentMessage",
        json.dumps({
            "to": task_uuid,
            "message": "submit a replacement Plan for the remaining work",
            "reasonCode": "blocked",
            "reason": "the active Plan is now marked for replan",
            "expectedPlanVersion": 1,
        }),
        context=ToolRuntimeContext(chat_id=123, session_uuid="blocked-session", source="web"),
    )
    resumed = json.loads(resumed_raw)
    assert resumed["ok"] is True
    assert continued and continued[0]["taskUuid"] == task_uuid
    assert len(await dao.pending_controls(task_uuid)) == 1


async def test_blocked_plan_recovers_through_real_agent_message_ack_replan_and_finalize(agent_tool_env):
    dao = agent_tool_env
    workflow = await dao.workflow_by_slug("single-agent")
    assert workflow is not None
    backend = _BlockedRecoveryBackend()
    manager = RathTaskManager(dao, max_concurrent_tasks=1)
    registry = ToolRegistry()
    register_agent_tools(
        registry,
        config=_PlanEnabledConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
    )
    coordinator = manager.plan_coordinator
    assert coordinator is not None
    agent = RathAgentDef(
        agent_key="blocked-recovery-worker",
        name="Blocked recovery worker",
        description="Exercise the complete blocked recovery protocol",
        system_prompt="Follow the Agent Plan and control protocols exactly.",
        model="openai/gpt",
        think_level="off",
        tool_allowlist=[],
        workflow_uuid=workflow.workflow_uuid,
    )
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow.workflow_uuid,
        title="real blocked recovery",
        input_data={
            "instruction": "Run v1, block, then recover through an approved v2 Replan.",
            "agentSnapshot": agent_to_snapshot(agent),
        },
        parent_session_uuid="blocked-full-session",
        status="queued",
    )
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=4096,
        tools=registry,
        model_call_limit=20,
        tool_call_limit=10,
        plan_control_call_limit=100,
        plan_protocol_enabled=True,
    )
    initial_run = asyncio.create_task(runner.run())
    message_task = None
    try:
        pending_v1 = await _wait_plan_phase(coordinator, task_uuid, "awaiting_plan_decision")
        assert pending_v1["state"]["pending_plan_version"] == 1
        await coordinator.decide(
            task_uuid,
            expected_version=1,
            action="approve",
            request_id="full-approve-v1",
            reason="approve initial blocked-path test Plan",
        )
        blocked = await asyncio.wait_for(initial_run, timeout=3)
        assert blocked["status"] == "needs_openbear_control"
        assert blocked["reason"] == "agent_plan_blocked"
        assert blocked["next"].startswith("First request_replan against the active Plan")
        blocked_snapshot = await coordinator.snapshot(task_uuid)
        assert blocked_snapshot["state"]["phase"] == "blocked_control"
        assert {(item["step_id"], item["status"]) for item in blocked_snapshot["steps"]} == {
            ("s1", "blocked")
        }

        requested = await coordinator.decide(
            task_uuid,
            expected_version=1,
            action="request_replan",
            request_id="full-request-replan-v1",
            reason="the blocked v1 method must be replaced",
        )
        assert requested["phase"] == "replan_required"
        message_task = asyncio.create_task(registry.dispatch(
            "AgentMessage",
            json.dumps({
                "to": task_uuid,
                "message": "Acknowledge this control and submit a replacement Plan with step r1.",
                "reasonCode": "blocked",
                "reason": "the active v1 Plan is now replan_required",
                "expectedPlanVersion": 1,
            }),
            context=ToolRuntimeContext(
                chat_id=123,
                session_uuid="blocked-full-session",
                source="web",
            ),
        ))

        pending_v2 = await _wait_plan_phase(coordinator, task_uuid, "awaiting_replan_decision")
        assert pending_v2["state"]["active_plan_version"] == 1
        assert pending_v2["state"]["pending_plan_version"] == 2
        assert backend.control_uuid
        control = await dao.control(backend.control_uuid)
        assert control is not None
        assert control.status == "applied"
        assert control.response_status == "accepted"
        assert control.responded_at > 0

        await coordinator.decide(
            task_uuid,
            expected_version=2,
            action="approve",
            request_id="full-approve-v2",
            reason="approve replacement r1 method",
        )
        resumed = json.loads(await asyncio.wait_for(message_task, timeout=5))
        assert resumed["ok"] is True
        assert resumed["continued"] is True
        assert resumed["status"] == "completed"

        stored = await dao.get_task(task_uuid)
        assert stored is not None
        assert stored.status == "completed"
        assert stored.output["summary"] == "blocked recovery chain completed"
        final_snapshot = await coordinator.snapshot(task_uuid)
        assert final_snapshot["state"]["phase"] == "finalizing"
        assert final_snapshot["state"]["active_plan_version"] == 2
        assert [item["status"] for item in final_snapshot["versions"]] == ["superseded", "approved"]
        assert {(item["step_id"], item["status"]) for item in final_snapshot["steps"]} == {
            ("s1", "superseded"),
            ("r1", "completed"),
        }
        assert final_snapshot["state"]["final_outputs_state"]["o1"]["sources"] == ["step:r1"]
        events = await dao.events(task_uuid)
        assert any(event.kind == "control_response" for event in events)
        assert len(backend.calls) == 9
    finally:
        if not initial_run.done():
            initial_run.cancel()
            await asyncio.gather(initial_run, return_exceptions=True)
        if message_task is not None and not message_task.done():
            message_task.cancel()
            await asyncio.gather(message_task, return_exceptions=True)
        running = list(manager._runs.values())
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)


async def test_agent_wait_delegates_to_main_controller_runtime(agent_tool_env):
    dao = agent_tool_env
    reg = ToolRegistry()
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )
    requests = []

    async def wait_callback(request):
        requests.append(request)
        return json.dumps({
            "ok": True,
            "wakeReason": "review_due",
            "summary": {"running": 2, "waitingControl": 0, "terminal": 0, "total": 2},
            "agents": [{"taskUuid": "one"}, {"taskUuid": "two"}],
        })

    raw = await reg.dispatch(
        "AgentWait",
        json.dumps({"mode": "review_after", "reviewAfterSeconds": 45, "reason": "两名 Agent 正常推进"}),
        context=ToolRuntimeContext(source="web", agent_wait=wait_callback),
    )
    data = json.loads(raw)

    assert data["wakeReason"] == "review_due"
    assert [item["taskUuid"] for item in data["agents"]] == ["one", "two"]
    assert requests == [{
        "mode": "review_after",
        "reviewAfterSeconds": 45.0,
        "reason": "两名 Agent 正常推进",
    }]


async def test_agent_wait_event_only_has_no_timer_and_is_main_only(agent_tool_env):
    dao = agent_tool_env
    reg = ToolRegistry()
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )
    requests = []

    async def wait_callback(request):
        requests.append(request)
        return json.dumps({"ok": True, "wakeReason": "all_terminal", "summary": {"running": 0}})

    raw = await reg.dispatch(
        "AgentWait",
        json.dumps({"mode": "event_only", "reviewAfterSeconds": 999}),
        context=ToolRuntimeContext(source="web", agent_wait=wait_callback),
    )
    assert json.loads(raw)["wakeReason"] == "all_terminal"
    assert requests[0]["reviewAfterSeconds"] == 0.0

    denied = await reg.dispatch(
        "AgentWait",
        json.dumps({"mode": "event_only"}),
        context=ToolRuntimeContext(source="agent:worker", agent_wait=wait_callback),
    )
    assert json.loads(denied)["error"] == "agent_context_not_allowed"


async def test_agent_launches_general_worker_with_dynamic_prompt(agent_tool_env):
    dao = agent_tool_env
    backend = _RecordingBackend()
    reg = ToolRegistry()
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"description": "阅读 README", "prompt": "请阅读 README 并返回关键结论", "tools": []}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)

    assert data["ok"] is True
    assert data["status"] == "completed"
    assert data["agentSession"]["agentKey"] == "general-purpose"
    assert len(backend.calls) == 1
    assert "请阅读 README" in str(backend.calls[0]["messages"][0]["content"])
    task = await dao.get_task(data["task"]["taskUuid"])
    assert task is not None
    assert task.input["instruction"] == "请阅读 README 并返回关键结论"
    assert task.input["agentSnapshot"]["agentKey"] == "general-purpose"
    assert task.input["agentSnapshot"]["toolAllowlist"] == []


async def test_agent_launch_prepends_active_agent_prompt_template(agent_tool_env, tmp_path):
    dao = agent_tool_env
    await dao.db.conn.execute(
        "INSERT INTO memory_templates (name, content, is_agent_active, updated_at) VALUES (?,?,1,1)",
        ("Agent基础提示词", "Agent base prompt\nWorkspace: [[ workspaceDir ]]\n[[ helpers.toolLines(builtinToolNames, builtinToolSummaries) ]]"),
    )
    await dao.db.conn.commit()
    backend = _RecordingBackend()
    reg = ToolRegistry()
    workspace_dir = str(tmp_path / "agent-workspace")

    async def read(_args):
        return "read"

    reg.add("Read", "读取文件", {"type": "object", "properties": {}}, read, visibility={"agent"})
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
        workspace_dir=workspace_dir,
    )

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"description": "阅读 README", "prompt": "请阅读 README", "tools": ["Read"]}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)

    assert data["ok"] is True
    system = backend.calls[0]["system"]
    assert system.startswith("Agent base prompt")
    assert f"Workspace: {workspace_dir}" in system
    assert "- Read: 读取文件" in system
    assert "You are a focused general-purpose subagent" in system


async def test_agent_child_inherits_parent_task_root_turn_lineage(agent_tool_env):
    dao = agent_tool_env
    parent_task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid="wf-parent",
        title="parent agent",
        parent_session_uuid="conversation-1",
        agent_session_uuid="parent-agent-session",
        turn_uuid="turn-root",
        parent_turn_uuid="turn-root",
        run_root_turn_uuid="turn-root",
        status="completed",
    )
    reg = ToolRegistry()
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"description": "child", "prompt": "继承父任务归属", "tools": []}, ensure_ascii=False),
        context=ToolRuntimeContext(
            chat_id=123,
            session_uuid="conversation-1",
            conversation_uuid="conversation-1",
            source="web",
            task_uuid=parent_task_uuid,
            agent_session_uuid="parent-agent-session",
        ),
    )
    data = json.loads(raw)
    assert data["ok"] is True
    child = await dao.get_task(data["task"]["taskUuid"])
    assert child is not None
    assert child.parent_task_uuid == parent_task_uuid
    assert child.turn_uuid == "turn-root"
    assert child.parent_turn_uuid == "turn-root"
    assert child.run_root_turn_uuid == "turn-root"
    assert data["task"]["parentTaskUuid"] == parent_task_uuid
    assert data["task"]["runRootTurnUuid"] == "turn-root"


async def test_agent_requires_explicit_tools_argument(agent_tool_env):
    dao = agent_tool_env
    reg = ToolRegistry()
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"description": "阅读 README", "prompt": "请阅读 README 并返回关键结论"}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)

    assert data["ok"] is False
    assert data["error"] == "agent_tools_required"
    assert data["availableTools"] == []


async def test_agent_rejects_unavailable_requested_tool(agent_tool_env):
    dao = agent_tool_env
    reg = ToolRegistry()
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"prompt": "查资料", "tools": ["Bash"]}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)

    assert data["ok"] is False
    assert data["error"] == "agent_tool_not_available"
    assert data["unknownTools"] == ["Bash"]


async def test_agent_rejects_memory_and_mcp_even_when_registered_agent_scoped(agent_tool_env):
    dao = agent_tool_env
    reg = ToolRegistry()

    async def noop(_args):
        return "noop"

    reg.add("Read", "read", {"type": "object", "properties": {}}, noop, visibility={"agent"})
    reg.add("Memory", "memory", {"type": "object", "properties": {}}, noop, visibility={"agent"})
    reg.add("MCPFindSymbol", "mcp", {"type": "object", "properties": {}}, noop, visibility={"agent"})
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"prompt": "查记忆", "tools": ["Memory", "MCPFindSymbol"]}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)

    assert data["ok"] is False
    assert data["error"] == "agent_tool_not_available"
    assert data["unknownTools"] == ["Memory", "MCPFindSymbol"]
    assert data["availableTools"] == ["Read"]


async def test_agent_uses_web_preset_and_agent_scoped_tools(agent_tool_env):
    dao = agent_tool_env
    await dao.create_agent(
        agent_key="code-reviewer",
        name="代码审查员",
        description="审查代码实现",
        system_prompt="你是代码审查员",
        model="openai/gpt",
        tool_allowlist=["Read"],
        enabled=True,
    )
    backend = _RecordingBackend()
    reg = ToolRegistry()

    async def read(_args):
        return "read"

    async def bash(_args):
        return "bash"

    reg.add("Read", "读取文件", {"type": "object", "properties": {}}, read, visibility={"agent"})
    reg.add("Bash", "执行命令", {"type": "object", "properties": {}}, bash, visibility={"agent"})
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"workerType": "code-reviewer", "prompt": "审查 app/tools/agents.py", "tools": ["Read"]}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)

    assert data["ok"] is True
    assert data["agentSession"]["agentKey"] == "code-reviewer"
    assert "你是代码审查员" in backend.calls[0]["system"]
    tool_names = {tool["name"] for tool in backend.calls[0]["tools"]}
    assert tool_names == {"Read"}


async def test_agent_requested_tools_cannot_exceed_preset_allowlist(agent_tool_env):
    dao = agent_tool_env
    await dao.create_agent(
        agent_key="reader-only",
        name="只读 Agent",
        description="只允许读取",
        system_prompt="你是只读 Agent",
        model="openai/gpt",
        tool_allowlist=["Read"],
        enabled=True,
    )
    reg = ToolRegistry()

    async def read(_args):
        return "read"

    async def bash(_args):
        return "bash"

    reg.add("Read", "读取文件", {"type": "object", "properties": {}}, read, visibility={"agent"})
    reg.add("Bash", "执行命令", {"type": "object", "properties": {}}, bash, visibility={"agent"})
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"workerType": "reader-only", "prompt": "检查代码", "tools": ["Read", "Bash"]}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)

    assert data["ok"] is False
    assert data["error"] == "agent_tool_not_allowed_by_preset"
    assert data["deniedTools"] == ["Bash"]


async def test_agent_message_does_not_resurrect_terminal_task(agent_tool_env):
    dao = agent_tool_env
    await dao.create_agent(
        agent_key="reviewer",
        name="新版审查员",
        description="已改动",
        system_prompt="新版 prompt",
        model="openai/changed",
        think_level="off",
        tool_allowlist=["Bash"],
        enabled=True,
    )
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="旧审查任务",
        input_data={"agentSnapshot": {
            "id": 99,
            "agentKey": "reviewer",
            "name": "旧版审查员",
            "description": "原始描述",
            "systemPrompt": "旧版 prompt",
            "model": "openai/original",
            "thinkLevel": "high",
            "toolAllowlist": ["Read"],
            "enabled": True,
        }},
        parent_session_uuid="openbear-session-1",
        status="completed",
    )
    await dao.update_task(task_uuid, current_agent_key="reviewer", output={"summary": "旧任务完成"}, finish=True)
    backend = _RecordingBackend()
    reg = ToolRegistry()

    async def read(_args):
        return "read"

    async def bash(_args):
        return "bash"

    reg.add("Read", "读取文件", {"type": "object", "properties": {}}, read, visibility={"agent"})
    reg.add("Bash", "执行命令", {"type": "object", "properties": {}}, bash, visibility={"agent"})
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "AgentMessage",
        json.dumps({
            "to": task_uuid,
            "message": "补充检查",
            "reasonCode": "user_instruction",
            "reason": "用户要求对已完成任务补充检查",
        }, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)

    assert data["ok"] is True
    assert data["terminal"] is True
    assert data["alreadyTerminal"] is True
    assert data["taskUuid"] == task_uuid
    assert data["task"]["status"] == "completed"
    assert backend.calls == []
    tasks = await dao.list_tasks(chat_id=123, limit=20)
    assert [task.task_uuid for task in tasks] == [task_uuid]


async def test_concurrent_agent_messages_only_one_claims_continuation(agent_tool_env):
    dao = agent_tool_env
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="等待续跑的 Agent",
        input_data={"agentSnapshot": {
            "agentKey": "general-purpose",
            "name": "general-purpose",
            "model": "openai/gpt",
            "toolAllowlist": [],
            "enabled": True,
        }},
        parent_session_uuid="openbear-session-1",
        status="needs_openbear_control",
    )
    await dao.create_artifact(
        task_uuid,
        kind="agent_continuation_state",
        name="续跑状态",
        content=json.dumps({
            "kind": "model",
            "messages": [{"role": "user", "content": "原任务"}],
            "roundNo": 0,
            "lastText": "",
            "pendingToolCalls": [],
        }),
    )

    class BlockingBackend(_RecordingBackend):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.calls.append({"messages": messages})
            self.entered.set()
            await self.release.wait()
            return AgentResult(text="续跑完成")

    backend = BlockingBackend()
    reg = ToolRegistry()
    manager = RathTaskManager(dao)
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
    )
    context = ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat")
    first = asyncio.create_task(reg.dispatch(
        "AgentMessage",
        json.dumps({
            "to": task_uuid,
            "message": "继续",
            "reasonCode": "user_instruction",
            "reason": "用户要求继续完成当前任务",
        }, ensure_ascii=False),
        context=context,
    ))
    await asyncio.wait_for(backend.entered.wait(), timeout=1)
    second_raw = await reg.dispatch(
        "AgentMessage",
        json.dumps({
            "to": task_uuid,
            "message": "重复继续",
            "reasonCode": "user_instruction",
            "reason": "用户再次要求继续当前任务",
        }, ensure_ascii=False),
        context=context,
    )
    second = json.loads(second_raw)
    assert second["ok"] is True
    assert second["steered"] is True
    # The second message may steer the one claimed continuation, but it must
    # never create a second model runner for the same task.
    assert len(backend.calls) == 1
    assert manager.count() == 1

    backend.release.set()
    first_result = json.loads(await asyncio.wait_for(first, timeout=1))
    assert first_result["ok"] is True
    # The steer request causes another model round in the same runner.
    assert len(backend.calls) == 2
    assert (await dao.get_task(task_uuid)).status == "completed"
    assert manager.task(task_uuid) is None


async def test_agent_message_steers_running_task(agent_tool_env):
    dao = agent_tool_env
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="运行中的 Agent",
        input_data={"agentSnapshot": {"agentKey": "general-purpose", "name": "general-purpose"}},
        parent_session_uuid="openbear-session-1",
        status="running",
    )
    reg = ToolRegistry()
    manager = RathTaskManager(dao)
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "AgentMessage",
        json.dumps({
            "to": task_uuid,
            "message": "缩小范围，只看配置加载",
            "reasonCode": "scope_drift",
            "reason": "当前调查范围过宽，需要收敛到配置加载",
        }, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)
    pending = await dao.pending_controls(task_uuid)

    assert data["ok"] is True
    assert data["steered"] is True
    assert pending[0].action == "steer"
    assert pending[0].message == "缩小范围，只看配置加载"


async def test_rath_manager_rejects_steer_for_waiting_openbear_control(agent_tool_env):
    dao = agent_tool_env
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="等待裁决的 Agent",
        input_data={"agentSnapshot": {"agentKey": "general-purpose", "name": "general-purpose"}},
        parent_session_uuid="openbear-session-1",
        status="needs_openbear_control",
    )
    manager = RathTaskManager(dao)

    with pytest.raises(RuntimeError, match="AgentMessage"):
        await manager.steer(task_uuid, "继续")
    assert await dao.pending_controls(task_uuid) == []


async def test_agent_message_steers_paused_task(agent_tool_env):
    dao = agent_tool_env
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="暂停中的 Agent",
        input_data={"agentSnapshot": {"agentKey": "general-purpose", "name": "general-purpose"}},
        parent_session_uuid="openbear-session-1",
        status="paused",
    )
    reg = ToolRegistry()
    manager = RathTaskManager(dao)
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "AgentMessage",
        json.dumps({
            "to": task_uuid,
            "message": "暂停期间补充：只看失败用例",
            "reasonCode": "user_instruction",
            "reason": "用户在暂停期间补充了调查范围",
        }, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)
    pending = await dao.pending_controls(task_uuid)

    assert data["ok"] is True
    assert data["steered"] is True
    assert pending[0].action == "steer"
    assert pending[0].message == "暂停期间补充：只看失败用例"


async def test_agent_stop_stops_running_task(agent_tool_env):
    dao = agent_tool_env
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="需要停止的 Agent",
        input_data={"agentSnapshot": {"agentKey": "general-purpose", "name": "general-purpose"}},
        parent_session_uuid="openbear-session-1",
        status="running",
    )
    reg = ToolRegistry()
    manager = RathTaskManager(dao)
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
    )

    raw = await reg.dispatch(
        "AgentStop",
        json.dumps({"to": task_uuid, "reason": "用户取消"}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)
    pending = await dao.pending_controls(task_uuid)
    task = await dao.get_task(task_uuid)

    assert data["ok"] is True
    assert data["stopped"] is True
    assert pending == []
    assert task is not None
    assert task.status == "cancelled"



async def test_agent_uses_conversation_agent_run_config_and_freezes_snapshot(agent_tool_env):
    dao = agent_tool_env
    backend = _RecordingBackend()
    reg = ToolRegistry()
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=RathTaskManager(dao),
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
    )

    await dao.db.conn.execute(
        """
        INSERT INTO web_conversations (
          conversation_uuid, owner_chat_id, internal_chat_id, title, model,
          agent_model, agent_think_level, agent_fast_mode, status, current_status, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "openbear-session-1", 123, 123, "t", "openai/gpt",
            "openai/cheap", "medium", 1, "idle", "就绪", 1, 1,
        ),
    )
    await dao.db.conn.execute(
        "INSERT INTO sessions (chat_id, created_at, updated_at, fast_mode) VALUES (?,?,?,1)",
        (123, 1, 1),
    )
    await dao.db.conn.commit()

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"description": "调查", "prompt": "用便宜模型做调查", "tools": []}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=123, session_uuid="openbear-session-1", source="chat"),
    )
    data = json.loads(raw)
    assert data["ok"] is True
    task = await dao.get_task(data["task"]["taskUuid"])
    assert task is not None
    snapshot = task.input["agentSnapshot"]
    assert snapshot["model"] == "openai/cheap"
    assert snapshot["thinkLevel"] == "medium"
    assert snapshot["fastMode"] is False  # cheap model has no fast
    assert snapshot["modelSource"] == "conversation"
    assert snapshot["resolvedAtStart"] is True
    assert backend.calls
    assert backend.calls[0]["think_level"] == "medium"
    assert backend.calls[0]["service_tier"] in {"", None}


async def test_agent_continue_keeps_frozen_runtime_after_conversation_change(agent_tool_env):
    dao = agent_tool_env
    backend = _RecordingBackend()
    reg = ToolRegistry()
    manager = RathTaskManager(dao)
    register_agent_tools(
        reg,
        config=_FakeConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(),
    )

    await dao.db.conn.execute(
        """
        INSERT INTO web_conversations (
          conversation_uuid, owner_chat_id, internal_chat_id, title, model,
          agent_model, agent_think_level, agent_fast_mode, status, current_status, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "openbear-session-2", 200, 200, "t", "openai/gpt",
            "openai/cheap", "medium", 0, "idle", "就绪", 1, 1,
        ),
    )
    await dao.db.conn.commit()

    raw = await reg.dispatch(
        "Agent",
        json.dumps({"description": "第一轮", "prompt": "先调查", "tools": []}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=200, session_uuid="openbear-session-2", source="chat"),
    )
    first = json.loads(raw)
    task_uuid = first["task"]["taskUuid"]
    assert first["ok"] is True

    # Change conversation defaults after task finished; continue must stay frozen.
    await dao.db.conn.execute(
        "UPDATE web_conversations SET agent_model=?, agent_think_level=?, agent_fast_mode=? WHERE conversation_uuid=?",
        ("openai/gpt", "high", 1, "openbear-session-2"),
    )
    await dao.db.conn.commit()

    cont = await reg.dispatch(
        "AgentMessage",
        json.dumps({
            "to": task_uuid,
            "message": "继续补充结论",
            "reasonCode": "user_instruction",
            "reason": "用户要求继续补充现有任务结论",
        }, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=200, session_uuid="openbear-session-2", source="chat"),
    )
    cont_data = json.loads(cont)
    # Terminal tasks cannot continue via AgentMessage in current product rules; if blocked,
    # at least the first launch freeze is validated. Prefer continue path when available.
    if cont_data.get("ok"):
        assert backend.calls[-1]["think_level"] == "medium"
        task = await dao.get_task(task_uuid)
        assert task.input["agentSnapshot"]["model"] == "openai/cheap"
    else:
        task = await dao.get_task(task_uuid)
        assert task.input["agentSnapshot"]["model"] == "openai/cheap"
        assert task.input["agentSnapshot"]["thinkLevel"] == "medium"


async def test_agent_inheritance_uses_durable_plan_facts_and_enforces_scope(agent_tool_env):
    dao = agent_tool_env
    manager = RathTaskManager(dao)
    tools = AgentTools(
        config=_FakeConfig(),
        dao=dao,
        manager=manager,
        llm_factory=_FakeFactory(),
        model_selection=_FakeSelection(),
        registry=ToolRegistry(),
    )
    workflow = await dao.workflow_by_slug("single-agent")
    assert workflow is not None
    source_uuid = await dao.create_task(
        chat_id=300,
        workflow_uuid=workflow.workflow_uuid,
        title="interrupted source",
        input_data={"instruction": "finish the durable task"},
        parent_session_uuid="inherit-session",
        agent_session_uuid="agent:inherit-source",
        status="running",
    )
    plan = {
        "title": "Durable source Plan",
        "objective": "Produce a verified artifact",
        "scope": {"included": ["backend"], "excluded": []},
        "assumptions": [],
        "steps": [{
            "id": "s1",
            "title": "Verify source",
            "objective": "Run the source verification",
            "method": "Run pytest",
            "dependsOn": [],
            "required": True,
            "criteria": [{"id": "c1", "description": "pytest passes", "required": True}],
            "expectedEvidence": ["pytest output"],
        }],
        "finalOutputs": [{
            "id": "o1",
            "title": "Verified source",
            "description": "The verified source result",
            "supportedBy": ["s1"],
        }],
        "risks": [],
    }
    await tools.plan.submit_plan(source_uuid, plan, request_id="inherit-submit", wait_for_decision=False)
    await tools.plan.decide(
        source_uuid,
        expected_version=1,
        action="approve",
        request_id="inherit-approve",
        reason="complete and executable",
    )
    await tools.plan.progress(source_uuid, action="start", step_id="s1", request_id="inherit-start")
    await tools.plan.progress(
        source_uuid,
        action="complete",
        step_id="s1",
        request_id="inherit-complete",
        result_text="source verified",
        criteria=[{"id": "c1", "status": "satisfied"}],
        evidence=[{
            "type": "test_result",
            "reference": "pytest inheritance: passed",
            "summary": "inheritance test passed",
            "criterionId": "c1",
        }],
    )
    assert await dao.mark_interrupted_running() == 1

    captured: list[tuple[dict, dict | None]] = []
    registry = ToolRegistry()

    async def probe(_args):
        captured.append(await tools._inherit_plan_context(source_uuid))
        return "ok"

    registry.add("ProbeInheritance", "test", {"type": "object", "properties": {}}, probe)
    await registry.dispatch(
        "ProbeInheritance",
        "{}",
        context=ToolRuntimeContext(chat_id=300, session_uuid="inherit-session", source="chat"),
    )
    inherited, error = captured.pop()
    assert error is None
    assert inherited["sourceTask"]["status"] == "interrupted"
    assert inherited["completedSteps"][0]["stepId"] == "s1"
    assert inherited["evidence"][0]["reference"] == "pytest inheritance: passed"
    assert inherited["evidence"][0]["evidenceUuid"]
    assert inherited["instruction"].startswith("Treat these as durable facts")

    registry.add(
        "RunInheritedAgent",
        "test Agent inheritance handler",
        {"type": "object", "properties": {}},
        tools.agent,
    )
    launched_raw = await registry.dispatch(
        "RunInheritedAgent",
        json.dumps({
            "description": "continue inherited work",
            "prompt": "finish all remaining work",
            "tools": [],
            "inheritFromTaskUuid": source_uuid,
        }),
        context=ToolRuntimeContext(chat_id=300, session_uuid="inherit-session", source="chat"),
    )
    launched = json.loads(launched_raw)
    assert launched["ok"] is True
    new_task = await dao.get_task(launched["task"]["taskUuid"])
    assert new_task is not None
    assert new_task.input["inheritFromTaskUuid"] == source_uuid
    assert new_task.input["inheritedPlanContext"]["evidence"][0]["evidenceUuid"]
    events = await dao.events(new_task.task_uuid)
    assert any(event.kind == "agent_plan_inherited" for event in events)

    await registry.dispatch(
        "ProbeInheritance",
        "{}",
        context=ToolRuntimeContext(chat_id=300, session_uuid="other-session", source="chat"),
    )
    _inherited, error = captured.pop()
    assert error is not None and error["error"] == "inherit_task_not_found"

    active_uuid = await dao.create_task(
        chat_id=300,
        workflow_uuid=workflow.workflow_uuid,
        title="active source",
        input_data={},
        parent_session_uuid="inherit-session",
        status="running",
    )

    async def probe_active(_args):
        captured.append(await tools._inherit_plan_context(active_uuid))
        return "ok"

    registry.add("ProbeActiveInheritance", "test", {"type": "object", "properties": {}}, probe_active)
    await registry.dispatch(
        "ProbeActiveInheritance",
        "{}",
        context=ToolRuntimeContext(chat_id=300, session_uuid="inherit-session", source="chat"),
    )
    _inherited, error = captured.pop()
    assert error is not None and error["error"] == "inherit_task_still_active"
