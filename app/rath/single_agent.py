"""Single custom Agent Rath runner.

This runner executes one Web-registered Rath Agent under OpenBear control.  It is
used by the selector path for requests like "帮我使用 深度调研员 调研 ...".
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import inspect
import json
import re
import time
import uuid
from typing import Any

from app.agent.context_overflow import is_context_overflow_error
from app.agent.native_continuation import (
    deserialize_messages,
    native_items_for_tool_calls,
    serialize_messages,
)
from app.agent.transcript_repair import repair_role_alternation
from app.context.configuration import conversation_strategy
from app.context.prompts import effective_context_prompt
from app.context.runtime import ContextManager as WindowRuntime
from app.context.store import ContextOwner, WindowStore
from app.context.strategies import ContextCompressionError, ModelSummaryStrategy
from app.context.window import (
    RequiredContextTooLarge,
    WindowPolicy,
    mark_source,
    neutral_context,
    sanitize_window_checkpoint,
    source_of,
)
from app.llm.base import (
    AgentResult,
    LLMBackend,
    Message,
    OpenBearLLMError,
    apply_provider_billing,
)
from app.llm.events import ToolCall, Usage
from app.llm.retry import (
    RetryCancelledError,
    RetryPolicy,
    retry_delay_label,
    retry_wait_payload,
    wait_for_retry,
)
from app.model_cost import resolved_usage_cost_usd as _resolved_usage_cost_usd
from app.rath.plan import PLAN_TOOL_NAMES
from app.rath.prompts import render_plan_prompt
from app.rath.runner import RathNeedsOpenBearControl, RathWorkflowRunner
from app.rath.schemas import RathAgentDef
from app.task_memory import (
    TaskMemoryDAO,
    reconcile_task_memory_runtime_state,
    task_memory_runtime_epoch,
    without_task_memory_runtime_messages,
)
from app.tools.allowlist import (
    AGENT_DELEGATION_TOOL_NAMES,
    agent_phase_tool_names,
    agent_tool_capability,
    sanitize_tool_allowlist,
)
from app.tools.base import (
    ToolRegistry,
    ToolRuntimeContext,
    redact_tool_arguments_for_audit,
    redact_tool_result_for_audit,
)
from app.tools.file_state import clear_read_file_state
from app.utils import estimate_tokens, now_cn

# Private metadata is retained in Rath checkpoints but ignored by all provider
# adapters. It lets window rotation distinguish application-generated runtime units
# from task text that merely happens to look like XML.
_AGENT_RUNTIME_METADATA_KEY = "_openbear_runtime"
_AGENT_PLAN_RUNTIME_KIND = "rath_agent_plan_runtime"
_AGENT_CONTEXT_SUMMARY_KIND = "rath_agent_context_summary"
_AGENT_RUNTIME_VERSION = 1
_AGENT_PLAN_RUNTIME_LEGACY_SUFFIX = "这是系统追加的权威运行时状态，不是新的用户任务。按最新 revision 继续。"
_AGENT_HISTORY_INTRO = (
    "以下是压缩后保留的 Agent 任务文本，仅用于恢复任务语义。"
    "工具调用及结果、TaskMemory 工具回执、旧 Plan runtime 和 reasoning 均未原样回放；"
    "权威 Plan 与 Task Memory 会以新的 runtime state 单独注入。"
)


def safe_agent_llm_session_id(agent_session_uuid: str, task_uuid: str, agent_key: str) -> str:
    """Return an ASCII-only, task-isolated model session id.

    ``agent_session_uuid`` identifies OpenBear's durable Agent persona/history,
    but it is not a safe transport/reasoning scope for concurrent tasks.  Parrot
    uses ``session-id`` / ``prompt_cache_key`` to replay opaque model reasoning;
    therefore every Rath task must get a distinct scope while continuation of
    the *same* task must keep that scope stable.
    """
    base = str(agent_session_uuid or "rath").strip() or "rath"
    safe_base = re.sub(r"[^A-Za-z0-9_.:-]", "-", base)[:64] or "rath"
    task_digest = hashlib.sha1(str(task_uuid or "task").encode("utf-8")).hexdigest()[:16]
    agent_digest = hashlib.sha1(str(agent_key or "agent").encode("utf-8")).hexdigest()[:12]
    return f"rath:{safe_base}:task-{task_digest}:agent-{agent_digest}"


def agent_to_snapshot(agent: RathAgentDef, *, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = {
        "id": agent.id,
        "agentKey": agent.agent_key,
        "name": agent.name,
        "description": agent.description,
        "systemPrompt": agent.system_prompt,
        "model": agent.model,
        "thinkLevel": agent.think_level,
        "toolAllowlist": sanitize_tool_allowlist(agent.tool_allowlist),
        "enabled": agent.enabled,
    }
    if isinstance(runtime, dict) and runtime:
        # Runtime resolution freezes effective model/think/fast onto the task
        # snapshot so continue/resume does not drift with later session changes.
        from app.models.agent_runtime import agent_runtime_snapshot_fields

        snapshot.update(agent_runtime_snapshot_fields(runtime))
    return snapshot


def _usage_detail(usage: Usage, cost_usd: float) -> dict[str, float | int]:
    return {
        "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens,
        "cacheReadTokens": usage.cache_read_tokens,
        "cacheWriteTokens": usage.cache_write_tokens,
        "costUsd": cost_usd,
    }

def _loads_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _agent_runtime_kind(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    metadata = message.get(_AGENT_RUNTIME_METADATA_KEY)
    if not isinstance(metadata, dict):
        return ""
    if int(metadata.get("version") or 0) != _AGENT_RUNTIME_VERSION:
        return ""
    return str(metadata.get("kind") or "")


def _is_agent_plan_runtime_message(message: Any) -> bool:
    if _agent_runtime_kind(message) == _AGENT_PLAN_RUNTIME_KIND:
        return True
    if not isinstance(message, dict) or str(message.get("role") or "") != "user":
        return False
    # Continuation checkpoints written before the metadata marker existed still
    # need to be removed on their next window rotation. Match the whole generated
    # envelope rather than a tag that a task author could legitimately quote.
    content = _safe_text(message.get("content")).strip()
    return (
        content.startswith('<agent-plan-runtime revision="')
        and content.endswith(_AGENT_PLAN_RUNTIME_LEGACY_SUFFIX)
    )


def _is_agent_context_summary_message(message: Any) -> bool:
    return _agent_runtime_kind(message) == _AGENT_CONTEXT_SUMMARY_KIND


def _middle_ellipsis(text: str, limit: int) -> str:
    limit = max(0, int(limit or 0))
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= 32:
        return text[:limit]
    head = max(16, int(limit * 0.65))
    tail = max(8, limit - head - 28)
    omitted = max(0, len(text) - head - tail)
    return f"{text[:head]}\n…[已压缩省略 {omitted} 字符]…\n{text[-tail:]}"


def _tool_call_preview(call: Any, *, limit: int = 360) -> str:
    if isinstance(call, ToolCall):
        call_id = call.id
        name = call.name
        arguments = call.arguments
    elif isinstance(call, dict):
        call_id = str(call.get("id") or "")
        name = str(call.get("name") or "")
        arguments = _safe_text(call.get("arguments") or "")
    else:
        return _middle_ellipsis(_safe_text(call), limit)
    args = _middle_ellipsis(arguments or "", limit)
    return f"- id={call_id or '无'} name={name or '未知'} args={args}"


def _message_token_estimate(message: Message) -> int:
    total = estimate_tokens(_safe_text(message.get("content") or ""))
    total += estimate_tokens(_safe_text(message.get("reasoning") or ""))
    for call in message.get("tool_calls") or []:
        total += estimate_tokens(_tool_call_preview(call, limit=800))
    return total


def _native_items_for_tool_calls(
    result: AgentResult,
    tool_calls: list[ToolCall] | None,
) -> list[dict[str, Any]]:
    """Keep one canonical Responses turn without persisting dangling calls.

    ``tool_calls=None`` means the caller deliberately did not accept any calls
    (for example at a budget boundary). A normal final response has no calls and
    keeps every native item. If only a subset may run, drop the *whole* opaque
    turn and fall back to the readable neutral transcript: encrypted reasoning
    may reference every function call, so partially editing it is not safe.
    """
    return native_items_for_tool_calls(
        result.native_output_items,
        result.tool_calls,
        tool_calls,
        has_content=bool(result.text),
        has_reasoning=bool(result.reasoning),
    )


def _assistant_result_message(result: AgentResult, tool_calls: list[ToolCall] | None = None) -> Message:
    """Persist readable output and the provider-native continuation turn."""
    message: Message = {"role": "assistant", "content": result.text or ""}
    if result.reasoning:
        message["reasoning"] = result.reasoning
    if result.signature:
        message["signature"] = result.signature
    if tool_calls:
        message["tool_calls"] = tool_calls
    native_items = _native_items_for_tool_calls(result, tool_calls)
    if native_items:
        message["native_output_items"] = native_items
    return message


def _messages_with_task_progress(messages: list[Message], *, protocol: str) -> list[Message]:
    """Make persisted reasoning summaries visible on the next model request.

    Anthropic can natively replay a signed thinking block.  OpenAI Responses and
    Chat serializers intentionally ignore the neutral ``reasoning`` field, so
    without this adapter the task plan exists in local state but the next model
    round cannot see it.  Add the summary to a request-local assistant content
    copy; do not mutate the durable transcript or duplicate signed Anthropic
    thinking.
    """
    out: list[Message] = []
    native_anthropic = str(protocol or "").lower() == "anthropic"
    for message in messages:
        reasoning = _safe_text(message.get("reasoning") or "").strip()
        has_native_thinking = native_anthropic and bool(message.get("signature"))
        if message.get("role") != "assistant" or not reasoning or has_native_thinking:
            out.append(message)
            continue
        item: Message = dict(message)
        content = _safe_text(item.get("content") or "").strip()
        progress = "[Task-local reasoning/progress from this assistant turn]\n" + reasoning
        item["content"] = f"{content}\n\n{progress}".strip() if content else progress
        out.append(item)
    return out


class SingleAgentWorkflowRunner(RathWorkflowRunner):
    def __init__(
        self,
        dao,
        task_uuid: str,
        *,
        agent: RathAgentDef,
        backend: LLMBackend,
        model: str,
        max_tokens: int,
        tools: ToolRegistry | None = None,
        model_label: str = "",
        think_level: str = "off",
        service_tier: str = "",
        fast_request: dict[str, Any] | None = None,
        session_id: str = "",
        openbear_session_uuid: str = "",
        agent_session_uuid: str = "",
        caller_agent_session_uuid: str = "",
        cost: dict[str, Any] | None = None,
        base_cost: dict[str, Any] | None = None,
        fast_cost: dict[str, Any] | None = None,
        fast_requested: bool = False,
        base_system_prompt: str = "",
        tool_result_max_chars: int = 32_000,
        max_retries: int = 10,
        retry_backoff_s: float = 3.0,
        retry_max_delay_s: float = 600.0,
        retry_jitter_ratio: float = 0.0,
        retry_cancel_check=None,
        model_call_limit: int = 40,
        tool_call_limit: int = 80,
        plan_control_call_limit: int = 200,
        poll_interval_s: float = 0.5,
        context_window: int = 0,
        rollover_trigger_tokens: int = 0,
        window_trigger_ratio: float = 0.7,
        window_retain_ratio: float = 0.15,
        context_config: Any = None,
        context_llm_factory: Any = None,
        on_model_call=None,
        on_event=None,
        task_notification=None,
        conversation_event=None,
        plan_protocol_enabled: bool | None = None,
        plan_prompts: dict[str, str] | None = None,
    ) -> None:
        super().__init__(dao, task_uuid, poll_interval_s=poll_interval_s, on_event=on_event)
        self.agent = agent
        self.backend = backend
        self.model = model
        self.max_tokens = max_tokens
        self.tools = tools
        self.model_label = model_label or model
        self.think_level = think_level
        self.service_tier = service_tier
        self.fast_request = dict(fast_request or {})
        self.openbear_session_uuid = openbear_session_uuid
        self.agent_session_uuid = agent_session_uuid
        self.caller_agent_session_uuid = caller_agent_session_uuid
        self.session_id = session_id or safe_agent_llm_session_id(agent_session_uuid, task_uuid, agent.agent_key)
        self._request_options = copy.deepcopy({
            "max_tokens": max_tokens, "think_level": think_level,
            "service_tier": service_tier, "fast_request": self.fast_request,
            "session_id": self.session_id,
            "native_continuation": str(getattr(backend, "protocol", "") or "").lower() == "responses",
        })
        self.cost = dict(cost or {})
        self.base_cost = dict(base_cost) if base_cost is not None else dict(self.cost)
        self.fast_cost = dict(fast_cost or {})
        self.fast_requested = bool(fast_requested)
        self.base_system_prompt = str(base_system_prompt or "").strip()
        self.tool_result_max_chars = max(1, int(tool_result_max_chars or 32_000))
        self.retry_policy = RetryPolicy(
            max_retries=max(0, int(max_retries or 0)),
            base_delay_s=retry_backoff_s,
            max_delay_s=retry_max_delay_s,
            jitter_ratio=retry_jitter_ratio,
        )
        self.retry_cancel_check = retry_cancel_check
        self.model_call_limit = max(0, int(model_call_limit or 0))
        self.tool_call_limit = max(0, int(tool_call_limit or 0))
        self.plan_control_call_limit = max(1, int(plan_control_call_limit or 200))
        self.context_window = max(0, int(context_window or 0))
        self.window_policy = WindowPolicy(
            self.context_window, trigger_tokens=max(0, int(rollover_trigger_tokens or 0)),
            trigger_ratio=window_trigger_ratio, retain_ratio=window_retain_ratio,
            max_output_tokens=self.max_tokens,
        )
        self.context_config = context_config
        self.context_llm_factory = context_llm_factory
        self._window_runtime: WindowRuntime | None = None
        self._last_provider_prompt_tokens = 0
        self._provider_prompt_usage_generation = 0
        self.on_model_call = on_model_call
        self.task_notification = task_notification
        self.conversation_event = conversation_event
        self._model_calls_made = 0
        self._tool_calls_made = 0
        self._work_tool_calls_made = 0
        self._plan_tool_calls_made = 0
        self.chat_id = 0
        self.conversation_uuid = ""
        self.turn_uuid = ""
        self.run_root_turn_uuid = ""
        if plan_protocol_enabled is None:
            registered = set(tools.names(scope="agent")) if tools is not None else set()
            plan_protocol_enabled = {
                "AgentPlanSubmit",
                "AgentPlanProgress",
                "AgentPlanReplan",
            }.issubset(registered)
        self.plan_protocol_enabled = bool(plan_protocol_enabled)
        self.plan_prompts = dict(plan_prompts or {})
        self._task_instruction = ""
        self._inherited_plan_context: dict[str, Any] = {}
        self._plan_runtime: dict[str, Any] = {"phase": "drafting"}
        self._plan_runtime_digest = ""
        self._last_plan_runtime_snapshot: dict[str, Any] | None = None
        self._frozen_execution_tools: tuple[str, ...] | None = None
        self._frozen_execution_tool_schemas: list[dict[str, Any]] | None = None
        self._pending_control_acks: set[str] = set()
        self._plan_protocol_corrections = 0
        self._task_memory_epoch = 0

    def _request_cost(self, usage: Usage, result: Any) -> float:
        return _resolved_usage_cost_usd(
            self.base_cost,
            usage,
            fast_cost=self.fast_cost,
            fast_requested=self.fast_requested,
            actual_service_tier=getattr(result, "service_tier", ""),
            provider_cost_usd=getattr(result, "provider_cost_usd", None),
        )

    async def run(self) -> dict[str, Any]:
        task = await self.dao.get_task(self.task_uuid)
        if task is None:
            raise RuntimeError(f"Rath task not found: {self.task_uuid}")
        instruction = str(task.input.get("instruction") or task.input.get("question") or task.title or "").strip()
        self._task_instruction = instruction
        self._round_input = dict(task.input or {})
        inherited = task.input.get("inheritedPlanContext") if isinstance(task.input, dict) else {}
        self._inherited_plan_context = inherited if isinstance(inherited, dict) else {}
        self.chat_id = int(task.chat_id or 0)
        self.openbear_session_uuid = self.openbear_session_uuid or task.parent_session_uuid
        self.agent_session_uuid = self.agent_session_uuid or task.agent_session_uuid
        self.caller_agent_session_uuid = self.caller_agent_session_uuid or task.caller_agent_session_uuid
        self._plan_tool_calls_made = max(
            self._plan_tool_calls_made,
            int(getattr(task, "plan_tool_call_count", 0) or 0),
        )
        self.conversation_uuid = str(task.parent_session_uuid or self.openbear_session_uuid or "")
        self.turn_uuid = str(getattr(task, "turn_uuid", "") or "")
        self.run_root_turn_uuid = str(getattr(task, "run_root_turn_uuid", "") or self.turn_uuid)
        if self.agent_session_uuid and self.session_id == safe_agent_llm_session_id("", self.task_uuid, self.agent.agent_key):
            self.session_id = safe_agent_llm_session_id(self.agent_session_uuid, self.task_uuid, self.agent.agent_key)
        started = await self.dao.update_task(
            self.task_uuid,
            status="running",
            current_agent_key=self.agent.agent_key,
            current_status="准备中",
            expected_statuses=("queued",),
        )
        if not started:
            raise asyncio.CancelledError
        await self.emit(
            "task_started",
            agent_key=self.agent.agent_key,
            summary=f"{self.agent.name} 已启动",
            detail={
                "agent": agent_to_snapshot(self.agent),
                "modelLabel": self.model_label,
                "thinkLevel": self.think_level,
                "agentSessionUuid": self.agent_session_uuid,
                "openbearSessionUuid": self.openbear_session_uuid,
                "callerAgentSessionUuid": self.caller_agent_session_uuid,
                "modelCallLimit": self.model_call_limit,
                "toolCallLimit": self.tool_call_limit,
                "planControlCallLimit": self.plan_control_call_limit,
                "baseSystemPromptEnabled": bool(self.base_system_prompt),
            },
        )
        try:
            result = await self.run_step(
                self.agent.agent_key,
                f"{self.agent.name} 执行任务",
                lambda: self._run_agent(instruction),
            )
        except RathNeedsOpenBearControl as exc:
            output = await self._pause_for_openbear_control(exc.payload)
            return output
        output_text = str(result or "").strip() or "任务已完成，但 Agent 未返回正文。"
        artifact_summary = output_text.split("\n", 1)[0][:240]
        artifact_uuid = await self.dao.create_artifact(
            self.task_uuid,
            kind="agent_output",
            name=f"{self.agent.name} 输出",
            content=output_text,
            agent_key=self.agent.agent_key,
            summary=artifact_summary,
            content_type="text/markdown",
        )
        if self.agent_session_uuid:
            await self.dao.update_agent_session_after_task(
                self.agent_session_uuid,
                task_uuid=self.task_uuid,
                summary_delta=f"任务 {self.task_uuid[:8]}：{artifact_summary or '已完成'}",
                metadata={"lastArtifactUuid": artifact_uuid, "lastAgentName": self.agent.name},
            )
        output = {
            "summary": output_text,
            "taskUuid": self.task_uuid,
            "agent": agent_to_snapshot(self.agent),
            "model": self.model_label,
            "artifactUuid": artifact_uuid,
        }
        await self.complete(output)
        return output

    def _budget_exhausted(self, kind: str) -> bool:
        if kind == "model":
            return self.model_call_limit > 0 and self._model_calls_made >= self.model_call_limit
        if kind in {"tool", "work_tool"}:
            return self.tool_call_limit > 0 and self._work_tool_calls_made >= self.tool_call_limit
        if kind == "plan_tool":
            return self._plan_tool_calls_made >= self.plan_control_call_limit
        return False

    @staticmethod
    def _tool_budget_kind(name: str) -> str:
        return "plan_tool" if str(name or "") in (PLAN_TOOL_NAMES | {"AgentControlAck"}) else "tool"

    def _select_tool_calls_with_budget(
        self,
        calls: list[ToolCall],
    ) -> tuple[list[ToolCall], list[ToolCall], str]:
        selected: list[ToolCall] = []
        selected_work = 0
        selected_plan = 0
        for index, call in enumerate(calls):
            kind = self._tool_budget_kind(call.name)
            if kind == "plan_tool":
                exhausted = self._plan_tool_calls_made + selected_plan >= self.plan_control_call_limit
            else:
                exhausted = (
                    self.tool_call_limit > 0
                    and self._work_tool_calls_made + selected_work >= self.tool_call_limit
                )
            if exhausted:
                return selected, calls[index:], kind
            selected.append(call)
            if kind == "plan_tool":
                selected_plan += 1
            else:
                selected_work += 1
        return selected, [], ""

    def _record_local_tool_call(self, name: str) -> None:
        self._tool_calls_made += 1
        if self._tool_budget_kind(name) == "plan_tool":
            self._plan_tool_calls_made += 1
        else:
            self._work_tool_calls_made += 1

    async def _reconcile_task_memory_context(self, messages: list[Message]) -> bool:
        # Shared preferences are inputs even without a tool grant. Private notes
        # and long-reference guidance follow the current phase's real schemas.
        task_memory_available = any(
            schema.get("name") == "TaskMemory" for schema in await self._allowed_tool_schemas()
        )
        reconciled = await reconcile_task_memory_runtime_state(
            messages,
            TaskMemoryDAO(self.dao.db),
            conversation_uuid=self.conversation_uuid,
            task_uuid=self.task_uuid,
            for_agent=True,
            task_memory_available=task_memory_available,
            epoch=self._task_memory_epoch,
        )
        if len(reconciled) == len(messages):
            return False
        messages[:] = reconciled
        return True

    def _replace_context_in_new_task_memory_epoch(
        self,
        messages: list[Message],
        replacement: list[Message],
    ) -> None:
        self._task_memory_epoch = task_memory_runtime_epoch(
            messages,
            default=self._task_memory_epoch,
        ) + 1
        messages[:] = replacement

    def _serialize_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        return serialize_messages(messages)

    def _deserialize_messages(self, payloads: list[dict[str, Any]]) -> list[Message]:
        return deserialize_messages(payloads)

    async def _checkpoint_model_context(
        self,
        messages: list[Message],
        *,
        round_no: int,
        stage: str,
        last_text: str = "",
        extra_state: dict[str, Any] | None = None,
    ) -> int:
        """Persist the private model context without exposing it to UI/summary."""
        protocol = str(getattr(self.backend, "protocol", "") or "").lower()
        for message in messages:
            if _is_agent_context_summary_message(message) and not source_of(message).get("id"):
                mark_source(message, kind="legacy_handoff")
        async with self.dao._db.write_transaction(label="agent-window-checkpoint") as conn:
            window = self._get_window_runtime()
            window_state = await window.checkpoint(messages, extra_state={"stage": stage, **(extra_state or {})})
            safe_messages, dropped = self._sanitize_paired_messages(messages)
            state = {
                "version": 1,
                "windowVersion": window_state["windowVersion"],
                "windowRevision": window_state["revision"],
                "stage": str(stage or "checkpoint"),
                "roundNo": max(0, int(round_no or 0)),
                "lastText": str(last_text or ""),
                "pendingControlUuids": sorted(self._pending_control_acks),
                "providerPromptSnapshot": self._provider_prompt_snapshot_state(),
                "messages": self._serialize_messages(safe_messages),
                "droppedDanglingToolCallsOrOutputs": int(dropped or 0),
                "agentKey": self.agent.agent_key,
                "protocol": protocol,
                "model": self.model,
                "sessionId": self.session_id,
                "agentSessionUuid": self.agent_session_uuid,
                "openbearSessionUuid": self.openbear_session_uuid,
            }
            state.update(extra_state or {})
            # Consumption and the checkpoint carrying the original/ack set form
            # one transaction. Queuing in memory alone is not durable delivery.
            for control_uuid in self._pending_control_acks:
                control = await self.dao.control(control_uuid)
                if control and control.status == "pending":
                    await conn.execute(
                        "UPDATE rath_task_controls SET status='applied',applied_at=?,result=? WHERE control_uuid=? AND status='pending'",
                        (int(time.time()), "delivered in model checkpoint", control_uuid),
                    )
                    await self.emit("steer_applied", agent_key=self.agent.agent_key,
                                    summary="追加指导已加入任务上下文",
                                    detail={"message": control.message, "stage": stage})
            return await self.dao.save_task_model_context(
                self.task_uuid,
                protocol=protocol,
                model=self.model,
                session_id=self.session_id,
                state=state,
            )

    def _tool_call_id(self, call: ToolCall, index: int = 0) -> str:
        return call.id or f"call_{index}_{call.name}"

    def _sanitize_paired_messages(self, messages: list[Message]) -> tuple[list[Message], int]:
        """Drop dangling pairs using the shared Controller/Rath safety rule."""
        return sanitize_window_checkpoint(messages)

    async def _latest_continuation_state(self) -> dict[str, Any] | None:
        checkpoint = await self.dao.task_model_context(self.task_uuid)
        if checkpoint and isinstance(checkpoint.get("state"), dict):
            state = dict(checkpoint["state"])
            if state.get("kind") or not str(state.get("stage") or "").startswith(("budget_boundary:", "control_boundary:")):
                return state
            # Old boundaries kept pending calls only in an artifact. Merge its
            # metadata only at the same boundary, never over a newer checkpoint.
            for artifact in reversed(await self.dao.artifacts(self.task_uuid)):
                if artifact.kind == "agent_continuation_state":
                    try:
                        old = json.loads(artifact.content or "{}")
                    except json.JSONDecodeError:
                        continue
                    if isinstance(old, dict) and old.get("roundNo") == state.get("roundNo"):
                        for key in ("kind", "pendingToolCalls", "used", "limit"):
                            if key in old:
                                state[key] = old[key]
                        break
            return state
        artifacts = await self.dao.artifacts(self.task_uuid)
        for artifact in reversed(artifacts):
            if artifact.kind != "agent_continuation_state":
                continue
            try:
                state = json.loads(artifact.content or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(state, dict):
                return state
        return None

    async def _budget_control_payload(
        self,
        kind: str,
        *,
        messages: list[Message],
        round_no: int,
        last_text: str = "",
        pending_tool_calls: list[ToolCall] | None = None,
    ) -> dict[str, Any]:
        if kind == "model":
            limit = self.model_call_limit
            used = self._model_calls_made
        elif kind == "plan_tool":
            limit = self.plan_control_call_limit
            used = self._plan_tool_calls_made
        else:
            limit = self.tool_call_limit
            used = self._work_tool_calls_made
        safe_messages, dropped_pairs = self._sanitize_paired_messages(messages)
        pending = [
            {"id": self._tool_call_id(call, i), "name": call.name, "arguments": call.arguments}
            for i, call in enumerate(pending_tool_calls or [])
        ]
        state = {
            "kind": kind,
            "used": used,
            "limit": limit,
            "roundNo": round_no,
            "lastText": last_text,
            "providerPromptSnapshot": self._provider_prompt_snapshot_state(),
            "messages": self._serialize_messages(safe_messages),
            "droppedDanglingToolCalls": dropped_pairs,
            "pendingToolCalls": pending,
            "toolCalls": self._tool_calls_made,
            "workToolCalls": self._work_tool_calls_made,
            "planToolCalls": self._plan_tool_calls_made,
            "protocol": str(getattr(self.backend, "protocol", "") or "").lower(),
            "model": self.model,
            "sessionId": self.session_id,
            "agentSessionUuid": self.agent_session_uuid,
            "openbearSessionUuid": self.openbear_session_uuid,
        }
        state["checkpointRevision"] = await self._checkpoint_model_context(
            safe_messages, round_no=round_no, stage=f"budget_boundary:{kind}",
            last_text=last_text, extra_state=state,
        )
        state_uuid = await self.dao.create_artifact(
            self.task_uuid,
            kind="agent_continuation_state",
            name=f"{self.agent.name} 运行安全边界续跑状态",
            content=json.dumps(state, ensure_ascii=False, indent=2),
            agent_key=self.agent.agent_key,
            summary="Agent 已在硬运行安全边界暂停，等待 OpenBear 判断是否继续。",
            content_type="application/json",
        )
        await self.emit(
            "agent_budget_exhausted",
            agent_key=self.agent.agent_key,
            summary="Agent 已在硬运行安全边界暂停，等待 OpenBear 裁决",
            detail={
                "kind": kind,
                "used": used,
                "limit": limit,
                "continuationStateArtifactUuid": state_uuid,
                "droppedDanglingToolCalls": dropped_pairs,
                "pendingToolCalls": pending[:10],
            },
        )
        return {
            "ok": False,
            "status": "needs_openbear_control",
            "reason": "agent_task_budget_exceeded",
            "budgetKind": kind,
            "message": (
                f"{self.agent.name} 已在硬运行安全边界暂停。"
                "请 OpenBear 只根据已批准 Plan、durable evidence、真实 blocker、scope 与安全状态判断是否继续。"
                "如果仍有必要工作，请调用 AgentMessage 给出窄化指导；"
                "续跑会复用已保存的上下文与 Plan 状态，不会重头开始。"
            ),
            "progressPreview": last_text[:1200],
            "taskUuid": self.task_uuid,
            "agentSessionUuid": self.agent_session_uuid,
            "continuationStateArtifactUuid": state_uuid,
            "droppedDanglingToolCalls": dropped_pairs,
            "pendingToolCalls": pending[:10],
            "continueTool": {
                "name": "AgentMessage",
                "arguments": {
                    "to": self.task_uuid,
                    "message": "说明是否继续、继续时只补哪些缺口；不要要求从头重查。",
                },
            },
        }

    async def _raise_budget_control(
        self,
        kind: str,
        *,
        messages: list[Message],
        round_no: int,
        last_text: str = "",
        pending_tool_calls: list[ToolCall] | None = None,
    ) -> None:
        raise RathNeedsOpenBearControl(await self._budget_control_payload(
            kind,
            messages=messages,
            round_no=round_no,
            last_text=last_text,
            pending_tool_calls=pending_tool_calls,
        ))

    async def _run_agent(self, instruction: str) -> str:
        messages = await self._round_context_messages()
        messages.append(mark_source(
            {"role": "user", "content": self._user_prompt(instruction)}, kind="task",
            task_uuid=self.task_uuid, turn_uuid=self.turn_uuid, run_root_turn_uuid=self.run_root_turn_uuid,
        ))
        tools = await self._allowed_tool_schemas()
        await self._append_plan_runtime_update(messages)
        await self._reconcile_task_memory_context(messages)
        if self._budget_exhausted("model"):
            await self._raise_budget_control("model", messages=messages, round_no=0, last_text="")
        return await self._run_agent_loop(messages, tools, round_no=0, last_text="")

    async def _round_context_messages(self) -> list[Message]:
        source = getattr(self, "_round_input", {}).get("contextSource") or {}
        if not source.get("taskUuid"):
            return []
        checkpoint = await self.dao.task_model_context(str(source["taskUuid"]))
        if not checkpoint or int(checkpoint["revision"]) != int(source.get("revision") or 0):
            raise RuntimeError("The requested Agent context checkpoint is missing or changed")
        state = checkpoint["state"]
        window = self._get_window_runtime()
        saved_window = await window.store.load()
        same_window = not saved_window or (
            int(state.get("windowVersion") or 0) == int(saved_window["window_version"])
            and int(state.get("windowRevision") or 0) == int(saved_window["revision"])
        )
        # A half-executed batch may use the atomically paired execution
        # checkpoint, which records the uncertain tool explicitly. Never use an
        # unpaired generic window as proof that that tool did not run.
        reliable_partial = same_window and bool(saved_window and saved_window["state"].get("incompleteBatch"))
        retained = await window.store.restore_messages() if saved_window and not reliable_partial else None
        compatible = same_window and (
            state.get("protocol") == str(getattr(self.backend, "protocol", "") or "").lower()
            and state.get("model") == self.model and state.get("sessionId") == self.session_id
        )
        if compatible:
            messages = self._deserialize_messages(list(state.get("messages") or []))
            self._restore_provider_prompt_snapshot(state.get("providerPromptSnapshot"))
        else:
            messages = retained if retained is not None else neutral_context(self._deserialize_messages(list(state.get("messages") or [])))
            await self.emit("agent_native_context_reset", agent_key=self.agent.agent_key,
                            summary="保留独立窗口，不兼容的原生模型状态已移除")
        await window.restore_source_scopes(messages, origin_task_uuid=str(source["taskUuid"]))
        messages = [
            message for message in without_task_memory_runtime_messages(messages)
            if not _is_agent_plan_runtime_message(message)
            and (message.get(_AGENT_RUNTIME_METADATA_KEY) or {}).get("kind") not in {"agent_capabilities", "agent_completion_gate"}
        ]
        for message in messages:
            if message.get("role") == "user" and "<agent-control " in str(message.get("content") or ""):
                historical = str(message["content"]).split("\n这些是 OpenBear 主控制器", 1)[0]
                message["content"] = (
                    "上轮历史指导（保留事实和约束，不继承回执要求或执行授权）：\n"
                    + historical.replace("<agent-control ", "<historical-agent-control ").replace("</agent-control>", "</historical-agent-control>")
                )
                message.pop(_AGENT_RUNTIME_METADATA_KEY, None)
                previous_source = source_of(message).get("id", "")
                mark_source(message, kind="control", source_id=str(uuid.uuid4()), derived_from=previous_source)
        messages, _ = self._sanitize_paired_messages(messages)
        self._task_memory_epoch = task_memory_runtime_epoch(messages)
        if state.get("inflightTool") or source.get("state") == "partial":
            messages.append({"role": "user", "content": (
                "上一轮非正常结束，恢复的是最后可靠检查点。部分工具副作用可能已经发生但未确认；"
                "不要自动重放。仅按本轮指令核实相关实际对象，并说明不能确定的结果。"
            )})
        await self.emit("agent_context_continued", agent_key=self.agent.agent_key,
                        summary="新一轮已接入原 Agent 实例上下文", detail={
                            "sourceTaskUuid": source["taskUuid"], "sourceRevision": source["revision"],
                            "nativeCompatible": compatible, "messageCount": len(messages),
                        })
        return messages

    def _append_capability_state(self, messages: list[Message], tools: list[dict[str, Any]]) -> None:
        names = sorted(str(tool.get("name") or "") for tool in tools)
        payload = {"agentId": self.agent_session_uuid, "taskId": self.task_uuid,
                   "effectiveTools": names, "planMode": "managed" if self.plan_protocol_enabled else "direct"}
        signature = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        latest = next(((message.get(_AGENT_RUNTIME_METADATA_KEY) or {}) for message in reversed(messages)
                       if (message.get(_AGENT_RUNTIME_METADATA_KEY) or {}).get("kind") == "agent_capabilities"), {})
        if latest.get("capabilitySignature") == signature:
            return
        messages.append({"role": "user", "content": (
            "当前 Agent 运行事实（替代旧轮次和启动模板中的工具清单；不扩大用户授权）：\n" + signature +
            "\n旧轮次的指令、Plan和回执是历史依据，不是本轮授权；以本轮指令及实际schema为准。"
            "私有TaskMemory由运行时绑定当前独立实例；父会话历史不会自动提供。"
        ), _AGENT_RUNTIME_METADATA_KEY: {"kind": "agent_capabilities", "capabilitySignature": signature}})

    def _append_pending_steers(self, messages: list[Message]) -> bool:
        if not self.steers:
            return False
        controls = [item for item in self.steers if isinstance(item, dict) and str(item.get("message") or "").strip()]
        self.steers.clear()
        if not controls:
            return False
        blocks: list[str] = []
        for control in controls:
            control_uuid = str(control.get("controlUuid") or "").strip()
            if control_uuid:
                self._pending_control_acks.add(control_uuid)
            metadata = control.get("metadata") if isinstance(control.get("metadata"), dict) else {}
            blocks.append(
                f'<agent-control id="{html.escape(control_uuid, quote=True)}">\n'
                f'<requested-by>{html.escape(str(control.get("requestedBy") or "main-controller"))}</requested-by>\n'
                f'<reason-code>{html.escape(str(metadata.get("reasonCode") or "unspecified"))}</reason-code>\n'
                f'<reason>{html.escape(str(metadata.get("reason") or ""))}</reason>\n'
                f'<evidence>{html.escape(_json_compact(metadata.get("evidence") or []), quote=False)}</evidence>\n'
                f'<criterion-ids>{html.escape(_json_compact(metadata.get("criterionIds") or []), quote=False)}</criterion-ids>\n'
                f'<instruction>{html.escape(str(control.get("message") or ""))}</instruction>\n'
                '</agent-control>'
            )
        messages.append({
            "role": "user",
            "content": (
                "\n".join(blocks)
                + "\n这些是 OpenBear 主控制器的结构化干预。先对每个 id 调用 AgentControlAck，"
                "明确 accepted、rejected、appeal 或 needs_clarification；全部回执完成前不得调用其他工具。"
            ),
            _AGENT_RUNTIME_METADATA_KEY: {"kind": "agent_control"},
        })
        mark_source(messages[-1], kind="control", task_uuid=self.task_uuid,
                    turn_uuid=self.turn_uuid, run_root_turn_uuid=self.run_root_turn_uuid)
        return True

    async def _queue_steer(self, control: Any, stage: str, *, agent_key: str = "", paused: bool = False) -> None:
        # Keep the durable inbox pending until its original text and ack gate
        # have both entered a checkpoint, including controls received while paused.
        if control.control_uuid in self._pending_control_acks or any(
            item.get("controlUuid") == control.control_uuid for item in self.steers
        ):
            return
        self.steers.append({"controlUuid": control.control_uuid, "message": control.message,
                            "requestedBy": control.requested_by, "metadata": control.metadata})

    async def _save_context_boundary(self, messages: list[Message], stage: str) -> None:
        previous = (await self.dao.task_model_context(self.task_uuid) or {}).get("state") or {}
        final_response = previous.get("finalResponse")
        extra = {"finalResponse": final_response} if final_response and any(
            source_of(m).get("id") == final_response.get("eventId") for m in messages
        ) else {}
        await self._checkpoint_model_context(messages, round_no=int(previous.get("roundNo") or 0),
            stage=stage, last_text=str(previous.get("lastText") or ""), extra_state=extra)

    async def _checkpoint_and_append_steers(self, stage: str, messages: list[Message]) -> bool:
        await self.checkpoint(stage, agent_key=self.agent.agent_key)
        changed = self._append_pending_steers(messages)
        if changed:
            await self._save_context_boundary(messages, stage)
        return changed

    async def _run_agent_loop(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        round_no: int = 0,
        last_text: str = "",
    ) -> str:
        while True:
            await self._checkpoint_and_append_steers("before_model", messages)
            if self._budget_exhausted("model"):
                await self._raise_budget_control("model", messages=messages, round_no=round_no, last_text=last_text)
            tools = await self._allowed_tool_schemas()
            self._append_capability_state(messages, tools)
            await self._append_plan_runtime_update(messages)
            await self._reconcile_task_memory_context(messages)
            await self._checkpoint_model_context(
                messages,
                round_no=round_no,
                stage="before_model",
                last_text=last_text,
            )
            result = await self._call_model(messages, tools, round_no=round_no)
            self._model_calls_made += 1
            last_text = (result.text or result.reasoning or last_text or "").strip()

            while result.tool_calls:
                normalized_calls = [
                    call
                    if call.id
                    else ToolCall(
                        id=f"call_{round_no}_{index}_{call.name}",
                        name=call.name,
                        arguments=call.arguments,
                    )
                    for index, call in enumerate(result.tool_calls)
                ]
                calls_to_run, pending_calls, exhausted_kind = self._select_tool_calls_with_budget(
                    normalized_calls
                )
                if not calls_to_run:
                    if result.text or result.reasoning:
                        messages.append(_assistant_result_message(result))
                    await self._raise_budget_control(
                        exhausted_kind or "tool",
                        messages=messages,
                        round_no=round_no,
                        last_text=last_text,
                        pending_tool_calls=pending_calls,
                    )
                messages.append(_assistant_result_message(result, calls_to_run))
                for call in calls_to_run:
                    await self._checkpoint_model_context(
                        messages, round_no=round_no, stage="before_tool_call", last_text=last_text,
                        extra_state={"inflightTool": {"id": call.id, "name": call.name}},
                    )
                    tool_result = await self._dispatch_tool(
                        call.name,
                        call.arguments,
                        round_no=round_no,
                        tool_call_id=call.id,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": tool_result,
                    })
                    await self._checkpoint_model_context(
                        messages, round_no=round_no, stage="after_tool_result", last_text=last_text,
                    )
                    await self._raise_if_needs_openbear_control(
                        tool_result,
                        messages=messages,
                        round_no=round_no,
                        last_text=last_text,
                    )
                await self._checkpoint_and_append_steers("after_tool_call", messages)
                if self._get_window_runtime().pending:
                    await self._prepare_context_window(messages, tools)
                await self._checkpoint_model_context(
                    messages,
                    round_no=round_no,
                    stage="after_tool_call",
                    last_text=last_text,
                )
                if pending_calls:
                    await self._raise_budget_control(
                        exhausted_kind or "tool",
                        messages=messages,
                        round_no=round_no,
                        last_text=last_text,
                        pending_tool_calls=pending_calls,
                    )
                round_no += 1
                if self._budget_exhausted("model"):
                    await self._raise_budget_control("model", messages=messages, round_no=round_no, last_text=last_text)
                await self._checkpoint_and_append_steers("before_model", messages)
                tools = await self._allowed_tool_schemas()
                self._append_capability_state(messages, tools)
                await self._append_plan_runtime_update(messages)
                await self._reconcile_task_memory_context(messages)
                await self._checkpoint_model_context(
                    messages,
                    round_no=round_no,
                    stage="before_model",
                    last_text=last_text,
                )
                result = await self._call_model(messages, tools, round_no=round_no)
                self._model_calls_made += 1
                last_text = (result.text or result.reasoning or last_text or "").strip()

            await self.checkpoint("after_model", agent_key=self.agent.agent_key)
            if not self.steers:
                correction = await self._completion_correction()
                if not correction:
                    final_messages = messages + [_assistant_result_message(result)]
                    window = self._get_window_runtime()
                    window.bind_sources(final_messages)
                    final_response = {"text": (result.text or result.reasoning or "").strip(),
                                      "eventId": source_of(final_messages[-1])["id"]}
                    revision = await self._checkpoint_model_context(
                        final_messages, round_no=round_no, stage="final_response_received", last_text=last_text,
                        extra_state={"finalResponse": final_response},
                    )
                    try:
                        await self._prepare_context_window(final_messages, tools)
                    except RathNeedsOpenBearControl as exc:
                        # Preserve the existing completion gate/status decision,
                        # but expose the already received response and exact ref.
                        exc.payload["finalResponse"] = {**final_response, "checkpointRevision": revision}
                        raise
                    # A control accepted during summary generation must be handled
                    # before the final completion gate, not silently left behind.
                    await self.checkpoint("after_final_compression", agent_key=self.agent.agent_key)
                    if self.steers:
                        messages[:] = final_messages
                        self._append_pending_steers(messages)
                        continue
                    await self._checkpoint_model_context(
                        final_messages,
                        round_no=round_no,
                        stage="completed",
                        last_text=last_text,
                    )
                    return (result.text or result.reasoning or "").strip()
                messages.append(_assistant_result_message(result))
                messages.append({"role": "user", "content": correction,
                                 _AGENT_RUNTIME_METADATA_KEY: {"kind": "agent_completion_gate"}})
                await self._checkpoint_model_context(
                    messages,
                    round_no=round_no,
                    stage="plan_completion_correction",
                    last_text=last_text,
                )
                self._plan_protocol_corrections += 1
                await self.emit(
                    "agent_plan_protocol_corrected",
                    agent_key=self.agent.agent_key,
                    summary="Agent 尚未通过 Plan 完成门禁，已要求继续",
                    detail={
                        "phase": str(self._plan_runtime.get("phase") or "drafting"),
                        "correctionCount": self._plan_protocol_corrections,
                    },
                )
                round_no += 1
                continue
            messages.append(_assistant_result_message(result))
            self._append_pending_steers(messages)
            await self._checkpoint_model_context(
                messages,
                round_no=round_no,
                stage="after_control_append",
                last_text=last_text,
            )
            round_no += 1

    async def run_continue(self, guidance: str = "") -> dict[str, Any]:
        task = await self.dao.get_task(self.task_uuid)
        if task is None:
            raise RuntimeError(f"Rath task not found: {self.task_uuid}")
        output = task.output if isinstance(task.output, dict) else {}
        detail = output.get("detail") if isinstance(output.get("detail"), dict) else {}
        if output.get("reason") == "agent_context_overflow_unrecoverable" or detail.get("continuable") is False:
            raise RuntimeError("agent_task_not_continuable: existing control decision forbids internal resume")
        self.chat_id = int(task.chat_id or 0)
        self._round_input = dict(task.input or {})
        self._task_instruction = str(task.input.get("instruction") or task.title or "")
        self.openbear_session_uuid = self.openbear_session_uuid or task.parent_session_uuid
        self.agent_session_uuid = self.agent_session_uuid or task.agent_session_uuid
        self.caller_agent_session_uuid = self.caller_agent_session_uuid or task.caller_agent_session_uuid
        self.conversation_uuid = str(task.parent_session_uuid or self.openbear_session_uuid or "")
        self.turn_uuid = str(getattr(task, "turn_uuid", "") or "")
        self.run_root_turn_uuid = str(getattr(task, "run_root_turn_uuid", "") or self.turn_uuid)
        self._plan_tool_calls_made = max(
            self._plan_tool_calls_made,
            int(getattr(task, "plan_tool_call_count", 0) or 0),
        )
        state = await self._latest_continuation_state()
        if not state:
            raise RuntimeError("agent continuation state not found")
        messages = self._deserialize_messages(list(state.get("messages") or []))
        saved_window = await self._get_window_runtime().store.load()
        same_window = not saved_window or (
            int(state.get("windowVersion") or 0) == int(saved_window["window_version"])
            and int(state.get("windowRevision") or 0) == int(saved_window["revision"])
        )
        if not same_window:
            if (int(saved_window["revision"]) < int(state.get("windowRevision") or 0)
                    or int(saved_window["window_version"]) < int(state.get("windowVersion") or 0)):
                raise RuntimeError("agent_continuation_checkpoint_mismatch: window predates execution checkpoint")
            if state.get("inflightTool") or state.get("pendingToolCalls") or saved_window["state"].get("incompleteBatch"):
                raise RuntimeError("agent_continuation_checkpoint_mismatch: uncertain tool boundary")
            messages = await self._get_window_runtime().store.restore_messages()
            if messages is None:
                raise RuntimeError("agent_continuation_checkpoint_mismatch: reliable window unavailable")
        for control_uuid in state.get("pendingControlUuids") or []:
            control = await self.dao.control(str(control_uuid))
            if control and control.status == "applied" and not control.responded_at:
                self._pending_control_acks.add(str(control_uuid))
        has_native_items = any(bool(message.get("native_output_items")) for message in messages)
        expected_protocol = str(getattr(self.backend, "protocol", "") or "").lower()
        context_identity_matches = (
            same_window and str(state.get("protocol") or "").lower() == expected_protocol
            and str(state.get("model") or "") == self.model
            and str(state.get("sessionId") or "") == self.session_id
        )
        if has_native_items and not context_identity_matches:
            for message in messages:
                message.pop("native_output_items", None)
            await self.emit(
                "agent_native_context_reset",
                agent_key=self.agent.agent_key,
                summary="模型身份发生变化，已安全重建 Agent 上下文",
                detail={
                    "savedProtocol": str(state.get("protocol") or ""),
                    "currentProtocol": expected_protocol,
                    "savedModel": str(state.get("model") or ""),
                    "currentModel": self.model,
                    "sessionChanged": str(state.get("sessionId") or "") != self.session_id,
                },
            )
        if context_identity_matches:
            # A resumed task begins in a new runner process. Restore the exact
            # previous provider snapshot when its protocol/model/session context is
            # still valid, so its first resumed request can pre-compact correctly.
            fallback_prompt_tokens = (
                int(getattr(task, "last_input_tokens", 0) or 0)
                + int(getattr(task, "last_cache_read_tokens", 0) or 0)
                + int(getattr(task, "last_cache_write_tokens", 0) or 0)
            )
            self._restore_provider_prompt_snapshot(
                state.get("providerPromptSnapshot"),
                fallback_tokens=fallback_prompt_tokens,
            )
        # Repair legacy applied-but-uncheckpointed controls from the durable
        # inbox. An old runtime snapshot cannot prove an acknowledgement.
        cur = await self.dao.db.conn.execute(
            "SELECT control_uuid FROM rath_task_controls WHERE task_uuid=? AND action='steer' AND status='applied' AND responded_at=0 ORDER BY id",
            (self.task_uuid,),
        )
        for row in await cur.fetchall():
            control = await self.dao.control(str(row["control_uuid"]))
            marker = f'<agent-control id="{html.escape(control.control_uuid, quote=True)}">'
            if not any(marker in str(m.get("content") or "") for m in messages
                       if (m.get(_AGENT_RUNTIME_METADATA_KEY) or {}).get("kind") == "agent_control"):
                self.steers.append({"controlUuid": control.control_uuid, "message": control.message,
                                    "requestedBy": control.requested_by, "metadata": control.metadata})
            self._pending_control_acks.add(control.control_uuid)
        await self._get_window_runtime().restore_source_scopes(messages, origin_task_uuid=self.task_uuid)
        messages, dropped_pairs = self._sanitize_paired_messages(messages)
        self._task_memory_epoch = task_memory_runtime_epoch(messages)
        if dropped_pairs:
            await self.emit(
                "agent_continuation_state_repaired",
                agent_key=self.agent.agent_key,
                summary="续跑前已修复不完整的工具调用上下文",
                detail={"droppedDanglingToolCallsOrOutputs": dropped_pairs},
            )
        round_no = int(state.get("roundNo") or 0)
        last_text = str(state.get("lastText") or "")
        pending_tool_calls: list[ToolCall] = []
        for index, raw_call in enumerate(state.get("pendingToolCalls") or []):
            if not isinstance(raw_call, dict):
                continue
            name = str(raw_call.get("name") or "").strip()
            if not name:
                continue
            pending_tool_calls.append(ToolCall(
                id=str(raw_call.get("id") or f"call_{round_no}_{index}_{name}"),
                name=name,
                arguments=str(raw_call.get("arguments") or "{}"),
            ))
        self._model_calls_made = 0
        self._tool_calls_made = 0
        self._work_tool_calls_made = 0
        claimed = await self.dao.update_task(
            self.task_uuid,
            status="running",
            control_state="",
            current_agent_key=self.agent.agent_key,
            current_status="按 OpenBear 指导继续执行",
            expected_statuses=("resuming", "needs_openbear_control"),
        )
        if not claimed:
            raise asyncio.CancelledError
        await self.emit(
            "agent_task_continued",
            agent_key=self.agent.agent_key,
            summary="OpenBear 已要求继续同一任务",
            detail={"guidance": guidance[:2000], "previousBoundaryKind": state.get("kind")},
        )
        await self._checkpoint_and_append_steers("before_continue", messages)
        if self._pending_control_acks and pending_tool_calls:
            messages.append({
                "role": "user",
                "content": (
                    "暂停边界前尚未执行的工具调用已撤销。请先完成 AgentControlAck，"
                    "再根据最新控制意见与 Plan 状态判断是否需要重新发起这些工具。"
                ),
            })
            pending_tool_calls = []
        try:
            if pending_tool_calls:
                calls_to_run, still_pending, exhausted_kind = self._select_tool_calls_with_budget(
                    pending_tool_calls
                )
                if calls_to_run:
                    messages.append({"role": "assistant", "content": None, "tool_calls": calls_to_run})
                    for call in calls_to_run:
                        await self._checkpoint_model_context(
                            messages, round_no=round_no, stage="before_tool_call", last_text=last_text,
                            extra_state={"inflightTool": {"id": call.id, "name": call.name}},
                        )
                        tool_result = await self._dispatch_tool(
                            call.name,
                            call.arguments,
                            round_no=round_no,
                            tool_call_id=call.id,
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": tool_result,
                        })
                        await self._checkpoint_model_context(
                            messages, round_no=round_no, stage="after_tool_result", last_text=last_text,
                        )
                        await self._raise_if_needs_openbear_control(
                            tool_result,
                            messages=messages,
                            round_no=round_no,
                            last_text=last_text,
                        )
                    round_no += 1
                if still_pending:
                    await self._raise_budget_control(
                        exhausted_kind or "tool",
                        messages=messages,
                        round_no=round_no,
                        last_text=last_text,
                        pending_tool_calls=still_pending,
                    )
            if guidance.strip():
                messages.append({
                    "role": "user",
                    "content": (
                        "OpenBear 对运行安全暂停点的结构化指导：\n"
                        f"{guidance.strip()}\n\n"
                        "请从已保存的位置继续同一个 task，不要从头重查；只补 OpenBear 指出的缺口。"
                    ),
                })
                mark_source(messages[-1], kind="control", task_uuid=self.task_uuid,
                            turn_uuid=self.turn_uuid, run_root_turn_uuid=self.run_root_turn_uuid)
            result = await self._run_agent_loop(
                messages,
                await self._allowed_tool_schemas(),
                round_no=round_no,
                last_text=last_text,
            )
        except RathNeedsOpenBearControl as exc:
            output = await self._pause_for_openbear_control(exc.payload)
            return output
        output_text = str(result or "").strip() or "任务已继续执行，但 Agent 未返回正文。"
        artifact_summary = output_text.split("\n", 1)[0][:240]
        artifact_uuid = await self.dao.create_artifact(
            self.task_uuid,
            kind="agent_output",
            name=f"{self.agent.name} 续跑输出",
            content=output_text,
            agent_key=self.agent.agent_key,
            summary=artifact_summary,
            content_type="text/markdown",
        )
        if self.agent_session_uuid:
            await self.dao.update_agent_session_after_task(
                self.agent_session_uuid,
                task_uuid=self.task_uuid,
                summary_delta=f"任务 {self.task_uuid[:8]} 续跑：{artifact_summary or '已完成'}",
                metadata={"lastArtifactUuid": artifact_uuid, "lastAgentName": self.agent.name},
            )
        output = {
            "summary": output_text,
            "taskUuid": self.task_uuid,
            "agent": agent_to_snapshot(self.agent),
            "model": self.model_label,
            "artifactUuid": artifact_uuid,
            "continued": True,
        }
        await self.complete(output)
        return output

    def _user_prompt(self, instruction: str) -> str:
        """Wrap the controller's brief with runtime facts only.

        Working method, investigation depth, and handoff shape are governed by
        the task brief and the Agent base system prompt. This wrapper must not
        impose a generic workflow or report format on every delegated task.
        """
        has_source = bool((getattr(self, "_round_input", {}).get("contextSource") or {}).get("taskUuid"))
        context_note = (
            "本轮接续本实例前面轮次的实际检查点上下文（不是其他 Agent 或父会话的历史）。"
            "复用已保留的理解，只补会改变本轮行动的未知信息；上面的新指令替代的阶段约束按新指令执行，其余保留约束继续有效。"
            if has_source
            else "本 task 自包含：从独立上下文开始，不自动注入父会话或同角色其他任务的历史。"
        )
        return f"""
用户任务：
{instruction}

运行事实：
- 你是 Agent「{self.agent.name}」（{self.agent.agent_key}）。描述：{self.agent.description or '（无）'}
- 本轮任务身份：{self.task_uuid}；Agent 实例：{self.agent_session_uuid or 'legacy-task'}；本轮开始时间：{now_cn()}。
- {context_note}
- 本轮工具授权以实际提供的 schema 为准；历史工具权限、旧 Plan 完成态和旧控制回执不是本轮授权。

按上面的任务和你的 system prompt 完成工作，结果交给 OpenBear 主控。任务明确要求的输出语言优先；未指定时跟随任务的主要语言。使用 Markdown。
""".strip()

    async def _raise_if_needs_openbear_control(
        self,
        tool_result: str,
        *,
        messages: list[Message],
        round_no: int,
        last_text: str = "",
    ) -> None:
        try:
            payload = json.loads(tool_result)
        except Exception:
            return
        if not isinstance(payload, dict) or payload.get("status") != "needs_openbear_control":
            return
        safe_messages, dropped_pairs = self._sanitize_paired_messages(messages)
        reason = str(payload.get("reason") or "tool_requested_control")
        await self._checkpoint_model_context(
            safe_messages,
            round_no=round_no + 1,
            stage=f"control_boundary:{reason}",
            last_text=last_text,
            extra_state={"kind": reason, "pendingToolCalls": []},
        )
        state = {
            "kind": reason,
            "used": self._work_tool_calls_made,
            "limit": self.tool_call_limit,
            "roundNo": round_no + 1,
            "lastText": last_text,
            "messages": self._serialize_messages(safe_messages),
            "droppedDanglingToolCalls": dropped_pairs,
            "pendingToolCalls": [],
            "toolCalls": self._tool_calls_made,
            "workToolCalls": self._work_tool_calls_made,
            "planToolCalls": self._plan_tool_calls_made,
            "agentSessionUuid": self.agent_session_uuid,
            "openbearSessionUuid": self.openbear_session_uuid,
        }
        state_uuid = await self.dao.create_artifact(
            self.task_uuid,
            kind="agent_continuation_state",
            name=f"{self.agent.name} 控制暂停续跑状态",
            content=json.dumps(state, ensure_ascii=False, indent=2),
            agent_key=self.agent.agent_key,
            summary=f"Agent 在 {reason} 控制边界暂停，可由主控制器恢复。",
            content_type="application/json",
        )
        payload["continuationStateArtifactUuid"] = state_uuid
        payload["droppedDanglingToolCalls"] = dropped_pairs
        payload["continuable"] = bool(payload.get("continuable", True))
        await self.emit(
            "agent_control_continuation_saved",
            agent_key=self.agent.agent_key,
            summary="已保存 Agent 控制边界续跑状态",
            detail={
                "reason": reason,
                "continuationStateArtifactUuid": state_uuid,
                "droppedDanglingToolCalls": dropped_pairs,
            },
        )
        raise RathNeedsOpenBearControl(payload)

    async def _pause_for_openbear_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        reason = str(payload.get("reason") or "needs_openbear_control")
        message = str(payload.get("message") or "子 Agent 需要 OpenBear 裁决。")
        summary = f"需要 OpenBear 裁决：{reason}"
        changed = await self.dao.update_task(
            self.task_uuid,
            status="needs_openbear_control",
            control_state="waiting_openbear_control",
            current_agent_key=self.agent.agent_key,
            current_status="等待 OpenBear 裁决",
            output={
                "status": "needs_openbear_control",
                "reason": reason,
                "message": message,
                "detail": payload,
                "taskUuid": self.task_uuid,
                "agent": agent_to_snapshot(self.agent),
                "agentSessionUuid": self.agent_session_uuid,
            },
            finish=True,
            expected_statuses=("running",),
        )
        if not changed:
            current = await self.dao.get_task(self.task_uuid)
            return {
                "status": str(current.status if current is not None else "interrupted"),
                "summary": str(current.current_status if current is not None else "任务已终止"),
                "taskUuid": self.task_uuid,
                "agent": agent_to_snapshot(self.agent),
                "agentSessionUuid": self.agent_session_uuid,
            }
        await self.emit(
            "needs_openbear_control",
            agent_key=self.agent.agent_key,
            summary=summary,
            detail=payload,
        )
        if reason == "agent_task_budget_exceeded":
            output_text = (
                f"{summary}\n\n{message}\n\n"
                "本次 Rath Agent task 已在硬运行安全边界暂停。OpenBear 只能根据 Plan、证据、真实阻塞、范围与安全状态决定是否调用 AgentMessage 继续；续跑会恢复已保存状态，不会从头开始。"
            )
        elif reason == "agent_context_overflow_unrecoverable":
            output_text = (
                f"{summary}\n\n{message}\n\n"
                "本次 Rath Agent task 已在上下文超限处暂停。OpenBear 应缩小任务、减少输入/工具结果，或关闭该 Agent Session 后重新派发更窄任务。"
            )
        else:
            output_text = (
                f"{summary}\n\n{message}\n\n"
                "本次 Rath Agent Session 已暂停，等待 OpenBear 总负责人裁决。"
            )
        artifact_uuid = await self.dao.create_artifact(
            self.task_uuid,
            kind="openbear_control_request",
            name=f"{self.agent.name} 控制请求",
            content=output_text,
            agent_key=self.agent.agent_key,
            summary=summary,
            content_type="text/markdown",
            source_refs=[payload],
        )
        return {
            "status": "needs_openbear_control",
            "reason": reason,
            "continuable": bool(payload.get("continuable", reason == "agent_task_budget_exceeded")),
            "next": str(payload.get("next") or ""),
            "summary": output_text,
            "taskUuid": self.task_uuid,
            "agent": agent_to_snapshot(self.agent),
            "agentSessionUuid": self.agent_session_uuid,
            "artifactUuid": artifact_uuid,
            "detail": payload,
        }

    @staticmethod
    def _provider_prompt_tokens_from_usage(usage: Usage | None) -> int:
        if usage is None:
            return 0
        return max(0, (
            int(usage.input_tokens or 0)
            + int(usage.cache_read_tokens or 0)
            + int(usage.cache_write_tokens or 0)
        ))

    def _record_provider_prompt_usage(self, usage: Usage | None) -> None:
        tokens = self._provider_prompt_tokens_from_usage(usage)
        if tokens <= 0:
            return
        self._last_provider_prompt_tokens = tokens
        self._provider_prompt_usage_generation += 1

    def _provider_prompt_snapshot_state(self) -> dict[str, int]:
        return {
            "tokens": max(0, int(self._last_provider_prompt_tokens or 0)),
            "usageGeneration": max(0, int(self._provider_prompt_usage_generation or 0)),
            "windowVersion": self._window_runtime.window_version if self._window_runtime else 0,
        }

    def _restore_provider_prompt_snapshot(self, snapshot: Any, *, fallback_tokens: int = 0) -> None:
        # Legacy snapshots remain readable, but are not promoted into the new
        # window's authoritative usage. A real new request supplies that value.
        data = snapshot if isinstance(snapshot, dict) else {}
        self._last_provider_prompt_tokens = max(0, int(data.get("tokens") or fallback_tokens or 0))
        self._provider_prompt_usage_generation = max(0, int(data.get("usageGeneration") or 0))

    def _context_overflow_max_retries(self) -> int:
        return 3

    def _get_window_runtime(self) -> WindowRuntime:
        if self._window_runtime is None:
            owner = ContextOwner.agent(
                task_uuid=self.task_uuid,
                agent_session_uuid=self.agent_session_uuid if getattr(self, "_round_input", {}).get("sessionKind") == "independent" else "",
                conversation_uuid=self.conversation_uuid, session_uuid=self.openbear_session_uuid,
                chat_id=self.chat_id,
            )
            self._window_runtime = WindowRuntime(
                WindowStore(self.dao._db, owner), self.window_policy,
                backend=self.backend, model=self.model, model_label=self.model_label,
                on_rotated=self._on_window_rotated, on_state=self._on_compression_state,
                active_run_root_turn_uuid=self.run_root_turn_uuid,
                strategy_resolver=(lambda: conversation_strategy(self.dao._db, self.conversation_uuid,
                    self.context_config.context_management.default_strategy, chat_id=self.chat_id)) if self.context_config else None,
                strategies={"model_summary": ModelSummaryStrategy(self.context_config, self.context_llm_factory,
                    self.model_label, on_model_call=self._on_summary_model_call)} if self.context_config else None,
            )
        return self._window_runtime

    async def _on_compression_state(self, detail: dict[str, Any]) -> None:
        active = detail.get("status") == "running"
        await self.emit("model_context_compaction_started" if active else "model_context_compaction_failed",
            agent_key=self.agent.agent_key, summary="正在压缩上下文" if active else "上下文压缩未完成", detail=detail)

    async def _on_summary_model_call(self, call: dict[str, Any]) -> None:
        usage = call.get("usage") if isinstance(call.get("usage"), Usage) else Usage()
        label = str(call.get("model") or self.model_label)
        resolved = self.context_config.models.resolve(label)
        cost = _resolved_usage_cost_usd(resolved[1].cost if resolved else {}, usage,
            actual_service_tier=call.get("serviceTier"), provider_cost_usd=call.get("providerCostUsd"))
        detail = {**_usage_detail(usage, cost), "modelLabel": label, "model": label,
            "protocol": call.get("protocol", ""), "thinkLevel": "off", "callKind": "context_compaction",
            "durationMs": int(call.get("totalTimeMs") or 0), "status": call.get("status", "ok"),
            "errorType": call.get("errorType", ""), "taskUuid": self.task_uuid}
        if self.on_model_call:
            await self.on_model_call(detail)
        await self.dao.update_task(self.task_uuid, model_call_delta=1,
            input_tokens_delta=usage.input_tokens, output_tokens_delta=usage.output_tokens,
            cache_read_tokens_delta=usage.cache_read_tokens, cache_write_tokens_delta=usage.cache_write_tokens,
            cost_usd_delta=cost)

    async def _on_window_rotated(self, detail: dict[str, Any]) -> None:
        cleared = clear_read_file_state(
            chat_id=self.chat_id, session_uuid=self.openbear_session_uuid,
            agent_session_uuid=self.agent_session_uuid,
            task_uuid=None if self.agent_session_uuid else self.task_uuid,
            store=getattr(self.tools, "file_state", None),
        )
        await self.emit(
            "model_context_compaction_completed", agent_key=self.agent.agent_key,
            summary="上下文压缩完成 · " + ("模型摘要" if detail.get("strategy") == "model_summary" else "滑动窗口"),
            detail={**detail, "clearedReadStates": cleared},
        )

    async def _fresh_window_runtime_state(self, messages: list[Message]) -> list[Message]:
        had_completion_gate = any((message.get(_AGENT_RUNTIME_METADATA_KEY) or {}).get("kind") == "agent_completion_gate"
                                  for message in messages)
        refreshed = [message for message in without_task_memory_runtime_messages(messages)
                     if not _is_agent_plan_runtime_message(message)
                     and (message.get(_AGENT_RUNTIME_METADATA_KEY) or {}).get("kind") not in {"agent_capabilities", "agent_completion_gate"}]
        self._task_memory_epoch += 1
        self._append_capability_state(refreshed, await self._allowed_tool_schemas())
        if had_completion_gate:
            correction = await self._completion_correction()
            if correction:
                refreshed.append({"role": "user", "content": correction,
                                  _AGENT_RUNTIME_METADATA_KEY: {"kind": "agent_completion_gate"}})
        if self.plan_protocol_enabled:
            self._plan_runtime_digest = ""
            self._last_plan_runtime_snapshot = None
            await self._append_plan_runtime_update(refreshed, force_full=True)
        await self._reconcile_task_memory_context(refreshed)
        return refreshed

    def _context_request_view(self, messages: list[Message], retry_tail: list[Message] | None = None) -> list[Message]:
        """One request-local view for both budgeting and actual transmission."""
        return repair_role_alternation(_messages_with_task_progress(
            messages + list(retry_tail or []), protocol=str(getattr(self.backend, "protocol", "") or ""),
        ))

    async def _prepare_context_window(
        self, messages: list[Message], tools: list[dict[str, Any]], *,
        force: bool = False, attempt: int = 0, retry_tail: list[Message] | None = None,
    ) -> bool:
        window = self._get_window_runtime()
        previous = window.window_version
        try:
            prepared = await window.prepare(
                messages, system=self._system_prompt(), tools=tools, force=force, attempt=attempt,
                refresh_after_rotation=self._fresh_window_runtime_state,
                request_view=lambda selected: self._context_request_view(selected, retry_tail),
                request_options={**self._request_options, "stream": callable(getattr(self.backend, "stream", None))},
            )
        except (RequiredContextTooLarge, ContextCompressionError) as exc:
            # A blocked retry must not lose the text that already streamed. Keep
            # it in the private execution archive, not in the active prompt or a
            # synthetic user instruction. The control handoff names exact refs.
            partials = [mark_source(dict(item), kind="execution") for item in (retry_tail or [])
                        if item.get("role") == "assistant" and item.get("content")]
            if partials:
                await window.store.archive(partials)
            raise RathNeedsOpenBearControl({
                "ok": False, "status": "needs_openbear_control",
                "reason": "agent_required_context_too_large" if isinstance(exc, RequiredContextTooLarge) else "agent_context_compression_failed",
                "message": str(exc), "taskUuid": self.task_uuid, "agentSessionUuid": self.agent_session_uuid,
                "continuable": False, "instructionsPreserved": True,
                "partialOutputEventIds": [source_of(item)["id"] for item in partials],
            }) from exc
        messages[:] = prepared
        await self._save_context_boundary(messages, "window_prepared")
        return window.window_version != previous

    def _context_overflow_control_payload(self, error: OpenBearLLMError, *, attempts: int) -> dict[str, Any]:
        return {
            "ok": False, "status": "needs_openbear_control", "reason": "agent_context_overflow_unrecoverable",
            "message": f"{self.agent.name} 已尝试上下文压缩 {attempts} 次仍无法在配置阈值内组成合法输入；已停止继续请求，未删除任务要求，也未重复工具。",
            "taskUuid": self.task_uuid, "agentSessionUuid": self.agent_session_uuid,
            "agent": agent_to_snapshot(self.agent), "error": error.message[:2000], "continuable": False,
        }

    def _system_prompt(self) -> str:
        """Return the stable Agent protocol prefix.

        Mutable Plan state is deliberately excluded from system instructions.
        It is appended to the conversation as revisioned XML user-state
        messages so provider prompt caches can keep this prefix and the tool
        schema stable throughout an execution phase.
        """
        own = (self.agent.system_prompt or f"你是 {self.agent.name}。{self.agent.description}").strip()
        identity = own if not self.base_system_prompt else f"{self.base_system_prompt.rstrip()}\n\n{own.lstrip()}"
        identity = effective_context_prompt(identity, self._window_runtime.active_strategy if self._window_runtime else "sliding_window")
        if not self.plan_protocol_enabled:
            return identity
        whitelist = ", ".join(sorted(AGENT_DELEGATION_TOOL_NAMES))
        whitelist_help = (
            "Read=读取文件；Write=创建或覆盖文件；Edit=精确修改文件；Bash=执行命令与测试；"
            "WebSearch=检索互联网；WebExtract=读取网页正文；Process=管理已启动的后台进程；"
            "TaskMemory=按当前 conversation/task 身份访问专用任务记忆。"
        )
        return f"""{identity}

【Agent Plan 强制执行协议（稳定前缀）】
1. 初次工作先提交完整 AgentPlanSubmit；审批前不得调用业务工具或宣称已执行。
2. Plan 可用 toolRequests 申请启动时未授权的基础工具。每项必须包含 name、reason、neededForSteps；可申请白名单：{whitelist}。能力说明：{whitelist_help} 不要申请启动时已授权工具。
3. 只有主控制器在初始 AgentPlanDecision(action=approve) 中明确列入 grantedTools 的申请才会生效。首次审批后工具集合冻结；Replan 不得扩权。
4. 执行已批准 Plan 时，先用 AgentPlanProgress(action=start) 进入步骤，再调用业务工具；update/complete 必须记录真实 durable evidence，全部必做步骤完成后调用 finalize。
5. 收到 <agent-control> 后，先调用 AgentControlAck，明确 accepted、rejected、appeal 或 needs_clarification，再做任何其他工具调用。不得把“消息已进入上下文”冒充 Agent 已同意。
6. 最新动态状态由追加式 <agent-plan-runtime> XML 提供。较新 revision 覆盖较旧字段；full 是完整快照。delta 只修改列出的字段，其余状态沿用之前 revision；其中 steps/completedHistory 按 (planVersion, stepId)、evidence 按 evidenceUuid 做 upsert，pendingControls 每次出现时整数组替换。未列出的条目或字段保持不变。
7. 不得伪造证据，不得把准备执行写成已完成，不得在 Plan 门禁未通过时宣布任务结束。
""".strip()

    def _plan_runtime_instruction_state(self, runtime: dict[str, Any]) -> dict[str, Any]:
        """Provide prompt templates a small locator, not a duplicate runtime tree."""
        return {
            "phase": str(runtime.get("phase") or "drafting"),
            "activePlanVersion": int(runtime.get("activePlanVersion") or 0),
            "pendingPlanVersion": int(runtime.get("pendingPlanVersion") or 0),
            "currentStepId": str(runtime.get("currentStepId") or ""),
            "approvedTools": list(runtime.get("approvedTools") or []),
            "pendingControlIds": [
                str(item.get("control_uuid") or "")
                for item in runtime.get("pendingControls") or []
                if isinstance(item, dict) and str(item.get("control_uuid") or "")
            ],
            "controllerGuidance": _middle_ellipsis(str(runtime.get("controllerGuidance") or ""), 1_200),
            "fullStateLocation": (
                "完整且权威的 plan、steps、completedHistory、evidence 与 pendingControls "
                "位于同一 <agent-plan-runtime> 的 state-json 同级字段；不要在本提示词中重复它们。"
            ),
        }

    @staticmethod
    def _plan_runtime_instruction_current_step(runtime: dict[str, Any]) -> dict[str, Any]:
        current_step_id = str(runtime.get("currentStepId") or "")
        matching = next(
            (
                item for item in runtime.get("steps") or []
                if isinstance(item, dict)
                and str(item.get("step_id") or item.get("stepId") or "") == current_step_id
            ),
            {},
        )
        if not isinstance(matching, dict):
            matching = {}
        return {
            "planVersion": matching.get("plan_version") or matching.get("planVersion") or runtime.get("activePlanVersion") or 0,
            "stepId": current_step_id,
            "status": str(matching.get("status") or ""),
            "fullStepLocation": "详见同一 state-json 的 plan 与 steps 字段。",
        }

    @staticmethod
    def _plan_runtime_instruction_evidence(runtime: dict[str, Any]) -> list[dict[str, Any]]:
        """Keep restore guidance useful without duplicating every evidence blob."""
        items: list[dict[str, Any]] = []
        for raw in reversed(list(runtime.get("evidence") or [])):
            if not isinstance(raw, dict):
                continue
            items.append({
                "evidenceUuid": str(raw.get("evidence_uuid") or raw.get("evidenceUuid") or ""),
                "stepId": str(raw.get("step_id") or raw.get("stepId") or ""),
                "criterionId": str(raw.get("criterion_id") or raw.get("criterionId") or ""),
                "reference": _middle_ellipsis(str(raw.get("reference") or ""), 480),
                "summary": _middle_ellipsis(str(raw.get("summary") or ""), 640),
            })
            if len(items) >= 8:
                break
        return list(reversed(items))

    def _plan_runtime_payload(self, runtime: dict[str, Any], *, force_full: bool = False) -> tuple[str, dict[str, Any]]:
        previous = getattr(self, "_last_plan_runtime_snapshot", None)
        full = force_full or not isinstance(previous, dict)
        if full:
            payload = dict(runtime)
            mode = "full"
        else:
            payload: dict[str, Any] = {
                "deltaSemantics": {
                    "objectFields": "replace-listed-fields-only",
                    "omittedFields": "unchanged",
                    "arrays": {
                        "steps": {"operation": "upsert", "identityAliases": [["plan_version", "planVersion"], ["step_id", "stepId"]]},
                        "completedHistory": {"operation": "upsert", "identityAliases": [["plan_version", "planVersion"], ["step_id", "stepId"]]},
                        "evidence": {"operation": "upsert", "identityAliases": [["evidence_uuid", "evidenceUuid"]]},
                        "pendingControls": {"operation": "replace"},
                    },
                },
            }
            scalar_keys = (
                "phase", "activePlanVersion", "pendingPlanVersion", "currentStepId",
                "approvalCycle", "revisionCount", "controllerGuidance", "approvedTools", "rowRevision",
            )
            for key in scalar_keys:
                if runtime.get(key) != previous.get(key):
                    payload[key] = runtime.get(key)
            if runtime.get("plan") != previous.get("plan"):
                payload["plan"] = runtime.get("plan")
            if runtime.get("pendingControls") != previous.get("pendingControls"):
                payload["pendingControls"] = runtime.get("pendingControls") or []
            for key in ("steps", "completedHistory"):
                old_items = {
                    (str(item.get("plan_version") or item.get("planVersion") or ""), str(item.get("step_id") or item.get("stepId") or "")): item
                    for item in previous.get(key) or [] if isinstance(item, dict)
                }
                changed = [
                    item for item in runtime.get(key) or []
                    if isinstance(item, dict)
                    and old_items.get((str(item.get("plan_version") or item.get("planVersion") or ""), str(item.get("step_id") or item.get("stepId") or ""))) != item
                ]
                if changed:
                    payload[key] = changed
            old_evidence = {
                str(item.get("evidence_uuid") or item.get("evidenceUuid") or ""): item
                for item in previous.get("evidence") or [] if isinstance(item, dict)
            }
            changed_evidence = [
                item for item in runtime.get("evidence") or []
                if isinstance(item, dict)
                and old_evidence.get(str(item.get("evidence_uuid") or item.get("evidenceUuid") or "")) != item
            ]
            if changed_evidence:
                payload["evidence"] = changed_evidence
            mode = "delta"
        phase = str(runtime.get("phase") or "drafting")
        if full or phase != str((previous or {}).get("phase") or ""):
            instruction_state = self._plan_runtime_instruction_state(runtime)
            common = {
                "task": self._task_instruction,
                "plan_state": instruction_state,
                "controller_guidance": str(instruction_state.get("controllerGuidance") or ""),
                "current_step": self._plan_runtime_instruction_current_step(runtime),
                "evidence": self._plan_runtime_instruction_evidence(runtime),
                "plan_schema": {
                    "title": "string", "objective": "string",
                    "scope": {"included": ["string"], "excluded": ["string"]},
                    "assumptions": ["string"],
                    "steps": [{
                        "id": "string", "title": "string", "objective": "string", "method": "string",
                        "dependsOn": ["step id"], "required": True,
                        "criteria": [{"id": "string", "description": "string", "required": True}],
                        "expectedEvidence": ["string"],
                    }],
                    "finalOutputs": [{
                        "id": "string", "title": "string", "description": "string", "supportedBy": ["source id"]
                    }],
                    "risks": ["string"],
                    "toolRequests": [{"name": "string", "reason": "string", "neededForSteps": ["step id"]}],
                },
            }
            if phase == "drafting":
                phase_path = "rath.planDraftPrompt"
            elif phase in {"revising", "replan_required"}:
                phase_path = "rath.planRevisionPrompt"
            else:
                phase_path = "rath.planExecutionPrompt"
            payload["phaseInstructions"] = render_plan_prompt(
                phase_path, self.plan_prompts.get(phase_path, ""), common
            )
            if full:
                payload["restoreInstructions"] = render_plan_prompt(
                    "rath.planContextRestorePrompt",
                    self.plan_prompts.get("rath.planContextRestorePrompt", ""),
                    common,
                )
                if self._inherited_plan_context:
                    payload["inheritedPlanContext"] = self._inherited_plan_context
                    payload["inheritedContextPolicy"] = (
                        "继承内容是只读历史事实，只用于避免重复工作；它不代表当前新 task 已完成任何步骤。"
                        "必须为全部剩余工作提交新的完整 Plan，并用当前 task 的 durable evidence 证明新进度。"
                    )
        return mode, payload

    async def _append_plan_runtime_update(self, messages: list[Message], *, force_full: bool = False) -> bool:
        if not self.plan_protocol_enabled:
            return False
        runtime = await self._refresh_plan_runtime()
        canonical = _json_compact(runtime)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if not force_full and digest == self._plan_runtime_digest:
            return False
        mode, payload = self._plan_runtime_payload(runtime, force_full=force_full)
        revision = f"{int(runtime.get('rowRevision') or 0)}-{digest[:12]}"
        encoded = html.escape(_json_compact(payload), quote=False)
        messages.append({
            "role": "user",
            "content": (
                f'<agent-plan-runtime revision="{revision}" mode="{mode}">\n'
                f'<state-json>{encoded}</state-json>\n'
                '</agent-plan-runtime>\n'
                '这是系统追加的权威运行时状态，不是新的用户任务。按最新 revision 继续。'
            ),
            _AGENT_RUNTIME_METADATA_KEY: {
                "kind": _AGENT_PLAN_RUNTIME_KIND,
                "version": _AGENT_RUNTIME_VERSION,
                "revision": revision,
            },
        })
        self._plan_runtime_digest = digest
        self._last_plan_runtime_snapshot = json.loads(canonical)
        return True

    async def _call_model(self, messages: list[Message], tool_schemas: list[dict[str, Any]], *, round_no: int = 0) -> AgentResult:
        label = f"{self.agent.name} 模型调用"
        await self.dao.update_task(
            self.task_uuid,
            current_agent_key=self.agent.agent_key,
            current_status="模型调用中",
        )
        await self.emit(
            "model_call_started",
            agent_key=self.agent.agent_key,
            summary=f"{label}开始",
            detail={
                "model": self.model,
                "modelLabel": self.model_label,
                "thinkLevel": self.think_level,
                "maxTokens": self.max_tokens,
            },
        )
        retry_max = self.retry_policy.max_retries
        attempt = 0
        retry_context: list[Message] = []
        accumulated_text = ""
        primary_model_error: OpenBearLLMError | None = None
        overflow_attempt = 0
        overflow_max = self._context_overflow_max_retries()
        while True:
            started = time.monotonic()
            result = AgentResult(text=accumulated_text)
            used_stream = False
            last_partial_persist_at = started
            last_partial_chars = 0

            async def _persist_partial(*, force: bool = False) -> None:
                nonlocal last_partial_persist_at, last_partial_chars
                text = str(result.text or "")
                reasoning = str(result.reasoning or "")
                chars = len(text) + len(reasoning)
                now = time.monotonic()
                if chars <= 0:
                    return
                if not force and (now - last_partial_persist_at < 5.0 or chars - last_partial_chars < 512):
                    return
                await self.dao.update_task(
                    self.task_uuid,
                    output={
                        "partial": True,
                        "streaming": True,
                        "summary": text,
                        "reasoningPreview": reasoning[-4000:],
                        "taskUuid": self.task_uuid,
                        "agent": agent_to_snapshot(self.agent),
                        "model": self.model_label,
                        "round": round_no,
                        "attempt": attempt,
                    },
                    current_status=f"模型流式输出中 · {len(text)} 字",
                )
                await self.emit(
                    "model_stream_progress",
                    agent_key=self.agent.agent_key,
                    summary=f"模型流式输出中 · {len(text)} 字",
                    detail={"round": round_no, "attempt": attempt, "textChars": len(text), "reasoningChars": len(reasoning)},
                )
                last_partial_persist_at = now
                last_partial_chars = chars

            try:
                # Reconcile into the actual private model context on every physical
                # boundary. Unchanged retries are no-ops; a mutation during retry is
                # appended once and every previously emitted unit remains untouched.
                await self._reconcile_task_memory_context(messages)
                for revision_attempt in range(3):
                    await self._prepare_context_window(messages, tool_schemas, retry_tail=retry_context)
                    await self.checkpoint("window_pre_model", agent_key=self.agent.agent_key)
                    if not self.steers:
                        break
                    # Window selection awaits persistence. An intervention accepted
                    # meanwhile must enter both context and current tool gates before
                    # the request, not wait for another completed model response.
                    await self._checkpoint_and_append_steers("window_pre_model", messages)
                    tool_schemas = await self._allowed_tool_schemas()
                    self._append_capability_state(messages, tool_schemas)
                    await self._append_plan_runtime_update(messages)
                else:
                    raise RuntimeError("agent_controls_changed_repeatedly_before_request; instructions_preserved")
                window_ticket = await self._get_window_runtime().begin_request()
                outbound = self._context_request_view(messages, retry_context)
                stream_call = getattr(self.backend, "stream", None)
                if callable(stream_call):
                    used_stream = True
                    async for event in stream_call(
                        outbound,
                        model=self.model,
                        system=self._get_window_runtime().system or self._system_prompt(),
                        tools=tool_schemas or None,
                        **copy.deepcopy(self._request_options),
                    ):
                        apply_provider_billing(result, event.details)
                        if event.kind == "error":
                            raise OpenBearLLMError.from_stream_event(
                                event,
                                protocol=str(getattr(self.backend, "protocol", "") or ""),
                            )
                        if event.kind == "content":
                            result.text += event.text
                        elif event.kind == "reasoning":
                            result.reasoning += event.text
                            if event.signature:
                                result.signature = event.signature
                        elif event.kind == "tool_call":
                            result.tool_calls = list(event.tool_calls or [])
                        elif event.kind == "native_output_item":
                            result.native_output_items.extend(event.native_output_items or [])
                        elif event.kind == "usage" and event.usage:
                            result.usage.merge(event.usage)
                        elif event.kind == "finish":
                            result.finish_reason = event.finish_reason
                        if event.kind in {"content", "reasoning"}:
                            await _persist_partial()
                else:
                    # Compatibility fallback for unit-test backends. Production
                    # OpenAI/Anthropic backends all use the streaming path.
                    result = await self.backend.complete(
                        outbound,
                        model=self.model,
                        system=self._get_window_runtime().system or self._system_prompt(),
                        tools=tool_schemas or None,
                        **copy.deepcopy(self._request_options),
                    )
                break
            except asyncio.CancelledError:
                if used_stream:
                    await asyncio.shield(_persist_partial(force=True))
                raise
            except OpenBearLLMError as exc:
                if primary_model_error is None and exc.structured:
                    primary_model_error = exc
                if used_stream:
                    await _persist_partial(force=True)
                duration_ms = int((time.monotonic() - started) * 1000)
                failure_usage = result.usage
                self._record_provider_prompt_usage(failure_usage)
                await self._get_window_runtime().observe_usage(failure_usage, ticket=window_ticket)
                failure_cost_usd = self._request_cost(failure_usage, result)
                failure_detail = {
                    **_usage_detail(failure_usage, failure_cost_usd),
                    "model": self.model,
                    "modelLabel": self.model_label,
                    "thinkLevel": self.think_level,
                    "durationMs": duration_ms,
                    "tps": 0.0,
                    "status": "error",
                    "errorType": exc.reason or str(exc.status or "") or type(exc).__name__,
                    "taskUuid": self.task_uuid,
                    "serviceTier": result.service_tier,
                    "providerCostUsd": result.provider_cost_usd,
                }
                # Count and persist every physical request attempt before any
                # retry/overflow recovery starts the next upstream call.
                await self.dao.update_task(
                    self.task_uuid,
                    model_call_delta=1,
                    input_tokens_delta=failure_usage.input_tokens,
                    output_tokens_delta=failure_usage.output_tokens,
                    cache_read_tokens_delta=failure_usage.cache_read_tokens,
                    cache_write_tokens_delta=failure_usage.cache_write_tokens,
                    last_input_tokens=failure_usage.input_tokens,
                    last_output_tokens=failure_usage.output_tokens,
                    last_cache_read_tokens=failure_usage.cache_read_tokens,
                    last_cache_write_tokens=failure_usage.cache_write_tokens,
                    cost_usd_delta=failure_cost_usd,
                    current_status="模型调用失败",
                )
                if self.on_model_call is not None:
                    try:
                        maybe = self.on_model_call(failure_detail)
                        if inspect.isawaitable(maybe):
                            await maybe
                    except Exception as accounting_exc:
                        await self.emit(
                            "model_call_accounting_failed",
                            agent_key=self.agent.agent_key,
                            summary="失败模型调用即时记账失败",
                            detail={"error": f"{type(accounting_exc).__name__}: {accounting_exc}"},
                        )
                        raise
                if is_context_overflow_error(exc.message):
                    if overflow_attempt < overflow_max:
                        overflow_attempt += 1
                        rotated = await self._prepare_context_window(
                            messages, tool_schemas, force=True, attempt=overflow_attempt - 1,
                            retry_tail=retry_context,
                        )
                        if rotated:
                            retry_context = []
                            accumulated_text = ""
                            # Compression recovery must not reset the failed-call retry budget.
                            continue
                    raise RathNeedsOpenBearControl(
                        self._context_overflow_control_payload(exc, attempts=overflow_attempt)
                    )
                if result.text or result.reasoning or result.tool_calls:
                    await self.emit(
                        "model_stream_interrupted",
                        agent_key=self.agent.agent_key,
                        summary="模型流中断，保留部分输出并进入恢复流程",
                        detail={
                            "reason": exc.reason,
                            "status": exc.status,
                            "durationMs": duration_ms,
                            "textChars": len(result.text),
                            "reasoningChars": len(result.reasoning),
                            "toolCallCount": len(result.tool_calls),
                        },
                    )
                if exc.retryable and result.tool_calls:
                    # Responses emits tool_call only after a complete
                    # response.output_item.done. Replaying the failed request at
                    # this point can produce the same side-effecting call again;
                    # accept the complete turn, preserve its native items, and
                    # let the normal dispatcher execute it exactly once. The
                    # physical request was already accounted above as an error,
                    # so return directly instead of running success accounting a
                    # second time.
                    result.finish_reason = result.finish_reason or "tool_calls"
                    await self.dao.update_task(
                        self.task_uuid,
                        current_status="模型流中断，但完整工具调用已恢复",
                    )
                    await self.emit(
                        "model_stream_recovered_tool_calls",
                        agent_key=self.agent.agent_key,
                        summary="模型流中断，但完整工具调用已保留并继续执行",
                        detail={
                            "round": round_no,
                            "toolCallCount": len(result.tool_calls),
                            "nativeOutputItemCount": len(result.native_output_items),
                        },
                    )
                    return result
                if attempt >= retry_max or not exc.retryable:
                    if (
                        primary_model_error is not None
                        and primary_model_error is not exc
                        and exc.reason == "format"
                    ):
                        raise primary_model_error from exc
                    raise
                attempt += 1
                accumulated_text = str(result.text or accumulated_text)
                retry_context = []
                if accumulated_text.strip():
                    retry_context.append({"role": "assistant", "content": accumulated_text})
                retry_context.append({
                    "role": "user",
                    "content": (
                        "The previous model response was interrupted by a transient upstream error. "
                        "Continue the same task from the exact interruption point. Do not repeat completed "
                        "text or completed work; preserve all prior tool results and instructions."
                    ),
                })
                wait = self.retry_policy.delay(
                    attempt,
                    retry_after_s=float(getattr(exc, "retry_after_s", 0.0) or 0.0),
                )
                state = retry_wait_payload(
                    retry_number=attempt,
                    max_retries=retry_max,
                    delay_s=wait,
                    reason=exc.reason,
                    error=exc.message,
                    summary=exc.user_message(),
                    transport_status=exc.transport_status,
                    upstream_status=exc.upstream_status or exc.status,
                    root_cause=exc.root_cause,
                    attempts=exc.attempts,
                    details=exc.details,
                    scope="rath_agent_model_call",
                    task_uuid=self.task_uuid,
                )

                async def _publish_retry_state(payload: dict[str, Any]) -> None:
                    task = await self.dao.get_task(self.task_uuid)
                    output = dict(task.output or {}) if task is not None else {}
                    output["retry"] = dict(payload)
                    output.setdefault("taskUuid", self.task_uuid)
                    output.setdefault("agent", agent_to_snapshot(self.agent))
                    await self.dao.update_task(
                        self.task_uuid,
                        output=output,
                        control_state="retry_wait" if payload.get("active") else "",
                        current_agent_key=self.agent.agent_key,
                        current_status=(
                            f"模型调用失败：{payload.get('summary') or payload.get('reason') or '上游错误'}，"
                            f"等待重试 {attempt}/{retry_max}"
                            if payload.get("active") else f"模型调用中（重试 {attempt}/{retry_max}）"
                        ),
                    )
                    await self.emit(
                        "model_call_retry_wait" if payload.get("active") else "model_call_retry_resumed",
                        agent_key=self.agent.agent_key,
                        summary=(
                            f"模型调用失败：{payload.get('summary') or payload.get('reason') or '上游错误'}，"
                            f"{retry_delay_label(wait)}后重试 {attempt}/{retry_max}"
                            if payload.get("active") else f"模型重试等待结束 {attempt}/{retry_max}"
                        ),
                        detail={"retry": dict(payload), "round": round_no},
                    )

                await self.emit(
                    "model_call_retry",
                    agent_key=self.agent.agent_key,
                    summary=f"模型调用失败：{exc.user_message()}，准备重试 {attempt}/{retry_max}",
                    detail={
                        "reason": exc.reason,
                        "summary": exc.user_message(),
                        "status": exc.status,
                        "transportStatus": exc.transport_status,
                        "upstreamStatus": exc.upstream_status or exc.status,
                        "rootCause": dict(exc.root_cause),
                        "attempts": list(exc.attempts),
                        "durationMs": duration_ms,
                        "delayMs": int(wait * 1000),
                    },
                )
                try:
                    await wait_for_retry(
                        wait,
                        state=state,
                        cancel_check=self.retry_cancel_check,
                        on_update=_publish_retry_state,
                    )
                except RetryCancelledError:
                    await self.emit(
                        "model_call_retry_cancelled",
                        agent_key=self.agent.agent_key,
                        summary="用户已取消模型重试",
                        detail={"attempt": attempt, "retryMax": retry_max, "reason": exc.reason},
                    )
                    raise asyncio.CancelledError("model retry cancelled by user") from exc
                await self.dao.update_task(
                    self.task_uuid,
                    current_agent_key=self.agent.agent_key,
                    current_status=f"模型调用中（重试 {attempt}/{retry_max}）",
                )
                await self.emit(
                    "model_call_started",
                    agent_key=self.agent.agent_key,
                    summary=f"{label}开始（重试 {attempt}/{retry_max}）",
                    detail={
                        "model": self.model,
                        "modelLabel": self.model_label,
                        "thinkLevel": self.think_level,
                        "maxTokens": self.max_tokens,
                        "attempt": attempt,
                        "retryMax": retry_max,
                        "round": round_no,
                    },
                )
        duration_ms = int((time.monotonic() - started) * 1000)
        usage = result.usage
        self._record_provider_prompt_usage(usage)
        await self._get_window_runtime().observe_usage(usage, ticket=window_ticket)
        cost_usd = self._request_cost(usage, result)
        tps = usage.output_tokens * 1000 / duration_ms if duration_ms > 0 and usage.output_tokens > 0 else 0.0
        await self.dao.update_task(
            self.task_uuid,
            model_call_delta=1,
            input_tokens_delta=usage.input_tokens,
            output_tokens_delta=usage.output_tokens,
            cache_read_tokens_delta=usage.cache_read_tokens,
            cache_write_tokens_delta=usage.cache_write_tokens,
            last_input_tokens=usage.input_tokens,
            last_output_tokens=usage.output_tokens,
            last_cache_read_tokens=usage.cache_read_tokens,
            last_cache_write_tokens=usage.cache_write_tokens,
            cost_usd_delta=cost_usd,
            current_status="模型调用完成",
        )
        call_detail = {
            **_usage_detail(usage, cost_usd),
            "model": self.model,
            "modelLabel": self.model_label,
            "thinkLevel": self.think_level,
            "durationMs": duration_ms,
            "tps": tps,
            "status": "ok",
            "taskUuid": self.task_uuid,
            "serviceTier": result.service_tier,
            "providerCostUsd": result.provider_cost_usd,
        }
        # Persist provider/model billing before the Agent can issue its next
        # tool/model request. rath_tasks remains the task progress aggregate;
        # model_calls is the authoritative per-request accounting ledger.
        if self.on_model_call is not None:
            try:
                maybe = self.on_model_call(call_detail)
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception as exc:
                await self.emit(
                    "model_call_accounting_failed",
                    agent_key=self.agent.agent_key,
                    summary="模型调用即时记账失败",
                    detail={"error": f"{type(exc).__name__}: {exc}"},
                )
                raise
        await self.emit(
            "model_call_finished",
            agent_key=self.agent.agent_key,
            summary=f"{label}完成",
            detail=call_detail,
        )
        return result

    async def _refresh_plan_runtime(self) -> dict[str, Any]:
        """Read the durable Plan gate before every model/tool transition."""
        conn = self.dao.db.conn
        cur = await conn.execute(
            "SELECT * FROM rath_task_plan_state WHERE task_uuid=?",
            (self.task_uuid,),
        )
        state_row = await cur.fetchone()
        if state_row is None:
            runtime: dict[str, Any] = {
                "phase": "drafting",
                "activePlanVersion": 0,
                "pendingPlanVersion": 0,
                "currentStepId": "",
                "revisionCount": 0,
                "rowRevision": 0,
                "approvedTools": sanitize_tool_allowlist(self.agent.tool_allowlist or []),
                "pendingControls": [],
                "plan": None,
                "steps": [],
                "completedHistory": [],
                "evidence": [],
            }
        else:
            state = dict(state_row)
            active_version = int(state.get("active_plan_version") or 0)
            pending_version = int(state.get("pending_plan_version") or 0)
            phase = str(state.get("phase") or "drafting")
            visible_version = pending_version or active_version
            if phase in {"revising", "replan_required"}:
                cur = await conn.execute(
                    """
                    SELECT version FROM rath_task_plan_versions
                    WHERE task_uuid=? AND status='revise_requested'
                    ORDER BY version DESC LIMIT 1
                    """,
                    (self.task_uuid,),
                )
                revised_row = await cur.fetchone()
                if revised_row is not None:
                    visible_version = int(revised_row["version"] or visible_version)
            plan: dict[str, Any] | None = None
            if visible_version:
                cur = await conn.execute(
                    """
                    SELECT plan_json, plan_type, status FROM rath_task_plan_versions
                    WHERE task_uuid=? AND version=?
                    """,
                    (self.task_uuid, visible_version),
                )
                version_row = await cur.fetchone()
                if version_row is not None:
                    plan = _loads_json(str(version_row["plan_json"] or "{}"), {})
                    if isinstance(plan, dict):
                        plan = {
                            **plan,
                            "version": visible_version,
                            "planType": str(version_row["plan_type"] or "initial"),
                            "status": str(version_row["status"] or ""),
                        }
            cur = await conn.execute(
                """
                SELECT plan_version, step_id, status, result, criteria_state_json, blocker_json
                FROM rath_task_plan_step_runs
                WHERE task_uuid=? AND plan_version=? ORDER BY id ASC
                """,
                (self.task_uuid, active_version),
            )
            steps = []
            for row in await cur.fetchall():
                item = dict(row)
                item["criteriaState"] = _loads_json(item.pop("criteria_state_json"), {})
                item["blocker"] = _loads_json(item.pop("blocker_json"), {})
                steps.append(item)
            cur = await conn.execute(
                """
                SELECT plan_version, step_id, status, result, criteria_state_json
                FROM rath_task_plan_step_runs
                WHERE task_uuid=? AND status='completed' AND plan_version<>?
                ORDER BY id ASC
                """,
                (self.task_uuid, active_version),
            )
            completed_history = []
            for row in await cur.fetchall():
                item = dict(row)
                item["criteriaState"] = _loads_json(item.pop("criteria_state_json"), {})
                completed_history.append(item)
            cur = await conn.execute(
                """
                SELECT evidence_uuid, plan_version, step_id, criterion_id,
                       evidence_type, reference, summary, metadata_json
                FROM rath_task_plan_evidence
                WHERE task_uuid=? ORDER BY id ASC
                """,
                (self.task_uuid,),
            )
            evidence = []
            for row in await cur.fetchall():
                item = dict(row)
                item["metadata"] = _loads_json(item.pop("metadata_json"), {})
                evidence.append(item)
            cur = await conn.execute(
                """
                SELECT control_uuid, message, requested_by, metadata_json
                FROM rath_task_controls
                WHERE task_uuid=? AND action='steer' AND status='applied' AND responded_at=0
                ORDER BY id ASC
                """,
                (self.task_uuid,),
            )
            pending_controls = []
            for row in await cur.fetchall():
                item = dict(row)
                item["metadata"] = _loads_json(item.pop("metadata_json"), {})
                pending_controls.append(item)
            approved_tools = sanitize_tool_allowlist(
                _loads_json(str(state.get("approved_tools_json") or "[]"), [])
            )
            if active_version and not approved_tools:
                # Compatibility for Plans approved before approved_tools_json existed.
                approved_tools = sanitize_tool_allowlist(self.agent.tool_allowlist or [])
            runtime = {
                "phase": phase,
                "activePlanVersion": active_version,
                "pendingPlanVersion": pending_version,
                "currentStepId": str(state.get("current_step_id") or ""),
                "approvalCycle": int(state.get("approval_cycle") or 0),
                "revisionCount": int(state.get("revision_count") or 0),
                "rowRevision": int(state.get("row_revision") or 0),
                "approvedTools": approved_tools,
                "pendingControls": pending_controls,
                "controllerGuidance": str(state.get("last_controller_guidance") or ""),
                "plan": plan,
                "steps": steps,
                "completedHistory": completed_history,
                "evidence": evidence,
            }
        self._plan_runtime = runtime
        self._pending_control_acks.update(
            str(item.get("control_uuid") or "")
            for item in runtime.get("pendingControls") or []
            if str(item.get("control_uuid") or "")
        )
        return runtime

    async def _allowed_tool_schemas(self) -> list[dict[str, Any]]:
        if self.tools is None:
            self._plan_runtime = {"phase": "drafting"}
            return []
        runtime = await self._refresh_plan_runtime() if self.plan_protocol_enabled else {}
        phase = str(runtime.get("phase") or "drafting")
        approved = tuple(sorted(set(sanitize_tool_allowlist(runtime.get("approvedTools") or [])) & AGENT_DELEGATION_TOOL_NAMES))
        if self.plan_protocol_enabled and phase in {"executing", "finalizing"}:
            if self._frozen_execution_tools is None:
                self._frozen_execution_tools = approved
            elif approved != self._frozen_execution_tools:
                raise RuntimeError(f"approved Agent tool set changed after execution started: {self._frozen_execution_tools!r} -> {approved!r}")
        allowed = agent_phase_tool_names(
            self.agent.tool_allowlist or [], managed=self.plan_protocol_enabled, phase=phase,
            approved=approved, pending_control=bool(self.steers or self._pending_control_acks),
            ceiling=getattr(self, "_round_input", {}).get("presetToolCeiling") or [],
        )
        schemas = [
            schema
            for schema in self.tools.schemas(scope="agent")
            if str(schema.get("name") or "") in allowed
        ]
        if self.plan_protocol_enabled and phase in {"executing", "finalizing"}:
            if self._frozen_execution_tool_schemas is None:
                self._frozen_execution_tool_schemas = json.loads(json.dumps(schemas, ensure_ascii=False))
            return json.loads(json.dumps(self._frozen_execution_tool_schemas, ensure_ascii=False))
        return schemas

    async def _completion_correction(self) -> str:
        if self._pending_control_acks:
            return "先对所有待回执控制调用 AgentControlAck；尚未完成回执，不能结束本轮。"
        return await self._plan_completion_correction()

    async def _plan_completion_correction(self) -> str:
        if not self.plan_protocol_enabled:
            return ""
        runtime = await self._refresh_plan_runtime()
        phase = str(runtime.get("phase") or "drafting")
        if phase == "finalizing":
            return ""
        if phase == "drafting":
            return (
                "你尚未提交初始 Plan，当前不能结束任务。请先分析任务并调用 AgentPlanSubmit；"
                "批准前不得调用业务工具或声称已经执行。"
            )
        if phase == "revising":
            return (
                "主控制器要求修改 Plan，当前不能结束任务。请按 controllerGuidance 修订完整 Plan，"
                "然后再次调用 AgentPlanSubmit。"
            )
        if phase == "replan_required":
            return (
                "当前 Plan 已被要求重规划，不能继续旧方案或结束任务。请调用 AgentPlanReplan，"
                "提交覆盖全部剩余工作的完整替代 Plan。"
            )
        if phase == "executing":
            return (
                "任务尚未通过 Plan 完成门禁。请继续执行已批准 Plan：先用 AgentPlanProgress start 标记步骤，"
                "完成时提交 criteria 与 evidence；全部必需步骤完成后调用 finalize。只有 finalize 成功后才能输出最终报告。"
            )
        return f"Plan 当前处于 {phase}，尚不能输出最终报告；请等待或完成当前 Plan 控制流程。"

    async def _dispatch_tool(
        self,
        name: str,
        arguments: str,
        *,
        round_no: int,
        tool_call_id: str = "",
    ) -> str:
        if self.tools is None:
            await self.emit("tool_call_denied", agent_key=self.agent.agent_key, summary=f"工具不可用：{name}")
            return f"error: 工具不可用：{name}"
        schemas = await self._allowed_tool_schemas()
        allowed = {str(schema.get("name") or "") for schema in schemas}
        await self.checkpoint("before_tool_call", agent_key=self.agent.agent_key)
        if self.steers and name != "AgentControlAck":
            return _json_compact({
                "ok": False,
                "error": "control_ack_required",
                "message": "A new controller intervention is pending delivery; acknowledge it before other tools.",
            })
        if self._pending_control_acks and name != "AgentControlAck":
            return _json_compact({
                "ok": False,
                "error": "control_ack_required",
                "message": "Agent must acknowledge all applied controller interventions before other tools.",
                "pendingControlUuids": sorted(self._pending_control_acks),
            })
        if name not in allowed:
            await self.emit(
                "tool_call_denied",
                agent_key=self.agent.agent_key,
                summary=f"Agent {self.agent.name} 当前阶段未授权工具 {name}",
                detail={
                    "name": name,
                    "round": round_no,
                    "phase": str(self._plan_runtime.get("phase") or "drafting"),
                    "allowed": sorted(allowed),
                    "configuredAllowlist": self.agent.tool_allowlist,
                },
            )
            return _json_compact({
                "ok": False,
                "error": "tool_denied_by_plan_phase",
                "message": "Agent 当前阶段未授权调用该工具",
                "tool": name,
                "phase": str(self._plan_runtime.get("phase") or "drafting"),
                "allowed": sorted(allowed),
            })
        capability_name = agent_tool_capability(name)
        if str(self._plan_runtime.get("phase") or "") == "finalizing" and name != "AgentControlAck":
            return _json_compact({
                "ok": False,
                "error": "plan_already_finalized",
                "message": "Plan finalization already passed; return the final report without more tools.",
            })
        if (
            capability_name in AGENT_DELEGATION_TOOL_NAMES
            and str(self._plan_runtime.get("phase") or "") == "executing"
            and not str(self._plan_runtime.get("currentStepId") or "")
        ):
            return _json_compact({
                "ok": False,
                "error": "plan_step_not_started",
                "message": "Call AgentPlanProgress(action=start) before using an execution tool.",
                "tool": name,
            })
        await self.dao.update_task(
            self.task_uuid,
            current_agent_key=self.agent.agent_key,
            current_status=f"工具调用中：{name}",
        )
        audit_arguments = redact_tool_arguments_for_audit(name, arguments)
        await self.emit(
            "tool_call_started",
            agent_key=self.agent.agent_key,
            summary=f"调用工具 {name}",
            detail={
                "name": name,
                "round": round_no,
                "planPhase": str(self._plan_runtime.get("phase") or "drafting"),
                "arguments": audit_arguments,
            },
        )
        started = time.monotonic()
        result = await self.tools.dispatch(
            name,
            arguments,
            max_chars=self.tool_result_max_chars,
            context=ToolRuntimeContext(
                chat_id=self.chat_id,
                session_uuid=self.openbear_session_uuid,
                conversation_uuid=self.conversation_uuid,
                source=f"agent:{self.agent.agent_key}",
                agent_session_uuid=self.agent_session_uuid,
                task_uuid=self.task_uuid,
                agent_key=self.agent.agent_key,
                turn_uuid=self.turn_uuid,
                run_root_turn_uuid=self.run_root_turn_uuid,
                tool_call_id=tool_call_id,
                task_notification=self.task_notification,
                conversation_event=self.conversation_event,
            ),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        if name == "AgentControlAck":
            try:
                response_payload = json.loads(result)
            except Exception:
                response_payload = {}
            if isinstance(response_payload, dict) and response_payload.get("ok"):
                acknowledged = str(response_payload.get("controlUuid") or "")
                self._pending_control_acks.discard(acknowledged)
        is_plan_tool = name in PLAN_TOOL_NAMES or name == "AgentControlAck"
        self._record_local_tool_call(name)
        await self.dao.update_task(
            self.task_uuid,
            tool_call_delta=1,
            work_tool_call_delta=0 if is_plan_tool else 1,
            plan_tool_call_delta=1 if is_plan_tool else 0,
            current_status=f"工具调用完成：{name}",
        )
        await self.emit(
            "tool_call_finished",
            agent_key=self.agent.agent_key,
            summary=f"工具 {name} 调用完成",
            detail={
                "name": name,
                "round": round_no,
                "planPhase": str(self._plan_runtime.get("phase") or "drafting"),
                "durationMs": duration_ms,
                "resultPreview": redact_tool_result_for_audit(name, result, arguments)[:1000],
            },
        )
        return result
