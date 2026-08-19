"""Agent 主循环 —— 无轮次上限 + 防失控软约束 + tool-calling。

流程：backend.stream → 累积渲染 → finish=tool_calls 则执行工具回灌 → 续写 → 直到 stop。
软约束（非硬轮次上限）：
  - 单会话总 token 预算
  - 单轮墙钟时长上限
  - 无进展打转检测（连续 N 轮重复工具调用且无新文本）
"""
from __future__ import annotations

import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

from app.agent.compaction import ContextCompactionGate
from app.agent.context_overflow import is_context_overflow_error
from app.agent.native_continuation import native_items_for_tool_calls
from app.agent.result import RunResult
from app.agent.tool_call_hooks import normalize_tool_calls
from app.agent.transcript_repair import MISSING_TOOL_RESULT_TEXT, repair_role_alternation
from app.llm.base import LLMBackend, Message, OpenBearLLMError
from app.llm.events import ToolCall, Usage
from app.llm.retry import RetryCancelledError, RetryPolicy, retry_wait_payload, wait_for_retry
from app.logging import get_logger
from app.rath.controller_projection import project_agent_tool_result_for_controller
from app.stream.tool_progress import (
    format_tool_line,
    format_tool_result_line,
    format_tool_running_status_line,
    format_user_interaction_result_line,
    format_user_interaction_wait_line,
    is_user_interaction_tool,
)
from app.tools.base import (
    ToolRegistry,
    ToolRuntimeContext,
    redact_tool_arguments_for_audit,
    redact_tool_result_for_audit,
)

log = get_logger("agent.loop")

_AGENT_USAGE_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted", "partial", "needs_openbear_control"}
_AGENT_START_TOOLS = {"Agent"}
_AGENT_LONG_TOOLS = _AGENT_START_TOOLS | {"AgentMessage"}


def _json_obj(value: str) -> dict:
    try:
        data = json.loads(value) if str(value or "").strip() else {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _agent_tool_tasks(tool_name: str, payload: dict) -> list[dict]:
    if tool_name in {"Agent", "AgentMessage"}:
        task = payload.get("task")
        return [task] if isinstance(task, dict) else []
    return []


def _apply_agent_tool_usage(result: RunResult, tool_name: str, tool_result: str) -> None:
    """Merge completed Rath Agent task usage into the visible turn result once.

    We intentionally do not count active tasks here: the user-facing total should
    only include an Agent task after that execution has reached a stable terminal
    or waiting-control boundary.
    """
    if tool_name not in _AGENT_LONG_TOOLS:
        return
    payload = _json_obj(tool_result)
    for task in _agent_tool_tasks(tool_name, payload):
        status = str(task.get("status") or "")
        if status not in _AGENT_USAGE_TERMINAL_STATUSES:
            continue
        task_uuid = str(task.get("taskUuid") or task.get("task_uuid") or "")
        if task_uuid and task_uuid in result.expert_accounted_task_uuids:
            continue
        if task_uuid:
            result.expert_accounted_task_uuids.add(task_uuid)
        raw_tokens = task.get("tokens")
        tokens: dict[str, Any] = raw_tokens if isinstance(raw_tokens, dict) else {}
        result.expert_tasks += 1
        result.expert_model_calls += int(task.get("modelCalls") or 0)
        result.expert_tool_calls += int(task.get("toolCalls") or 0)
        public_input = int(tokens.get("input") or 0)
        public_cache = int(tokens.get("cache") or 0)
        # Agent task public payload exposes `input` as prompt total
        # (non-cache input + cache read/write). Convert back to the normal Usage
        # shape so display code does not double count cache tokens.
        output_tokens = int(tokens.get("output") or 0)
        result.expert_usage.input_tokens += max(0, public_input - public_cache)
        result.expert_usage.output_tokens += output_tokens
        result.expert_usage.total_tokens += public_input + output_tokens
        # Exact cache read/write split is not available in this public payload;
        # attribute the combined cache amount to cache_read for aggregate display.
        result.expert_usage.cache_read_tokens += public_cache
        try:
            result.expert_cost_usd += float(task.get("costUsd") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            result.expert_duration_ms = max(int(result.expert_duration_ms or 0), int(task.get("durationMs") or 0))
        except (TypeError, ValueError):
            pass


def _detached_agent_tool_payload(tool_name: str, tool_result: str) -> dict:
    if tool_name not in _AGENT_LONG_TOOLS:
        return {}
    payload = _json_obj(tool_result)
    if bool(payload.get("detached")) or str(payload.get("status") or "") == "running":
        return payload
    for task in _agent_tool_tasks(tool_name, payload):
        if bool(task.get("detached")) or str(task.get("status") or "") in {"running", "queued", "resuming"}:
            return payload
    return {}


_AGENT_ORCHESTRATION_TOOLS = {
    "Agent",
    "AgentMessage",
    "AgentStop",
    "AgentWait",
}

def _agent_tool_terminal_payload(tool_name: str, tool_result: str) -> dict:
    """Return a terminal Agent payload already delivered to the controller.

    A terminal delegated package is an input to the root task, not a terminal
    state for the main controller.  This signal is used only to suppress a
    redundant AgentWait until a newly running Agent starts another supervision
    generation; it must never restrict ordinary controller tools.
    """
    if tool_name not in _AGENT_LONG_TOOLS:
        return {}
    payload = _json_obj(tool_result)
    if bool(payload.get("detached")) or str(payload.get("status") or "") == "running":
        return {}
    tasks = _agent_tool_tasks(tool_name, payload)
    if not tasks:
        return {}
    for task in tasks:
        status = str(task.get("status") or "")
        if bool(task.get("detached")) or status not in _AGENT_USAGE_TERMINAL_STATUSES:
            return {}
    return payload


def _redundant_agent_wait_result() -> str:
    """Return a hidden no-op after this root turn already consumed terminal Agent state."""
    return json.dumps({
        "ok": True,
        "skipped": True,
        "alreadyTerminal": True,
        "wakeReason": "all_terminal",
        "summary": {"running": 0, "waitingControl": 0},
        "message": (
            "All scoped Agents are already terminal and their completion was already consumed. "
            "Do not call AgentWait again; integrate the results and continue the root task as needed."
        ),
    }, ensure_ascii=False)


def _agent_result_delivery_budget(tool_name: str, tool_result: str) -> tuple[int, int]:
    """Return exact output tokens/count for newly delivered terminal Agent results.

    AgentWait snapshots may contain older terminal tasks for status visibility, so
    only the explicit batch fields (derived from newly claimed notifications) are
    authoritative. Direct Agent/AgentMessage results have one terminal payload and
    may fall back to the task's final-call usage for compatibility.
    """
    if tool_name not in {"Agent", "AgentMessage", "AgentWait"}:
        return 0, 0
    payload = _json_obj(tool_result)
    if not payload or bool(payload.get("skipped")) or bool(payload.get("alreadyTerminal")):
        return 0, 0
    try:
        explicit_tokens = max(0, int(payload.get("resultOutputTokens") or 0))
        explicit_count = max(0, int(payload.get("resultCount") or 0))
    except (TypeError, ValueError):
        explicit_tokens = 0
        explicit_count = 0
    if explicit_tokens > 0 or explicit_count > 0:
        return explicit_tokens, explicit_count
    if tool_name == "AgentWait":
        return 0, 0
    if bool(payload.get("detached")) or str(payload.get("status") or "") != "completed":
        return 0, 0
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    last_usage = task.get("lastUsage") if isinstance(task.get("lastUsage"), dict) else {}
    try:
        fallback = max(0, int(last_usage.get("outputTokens") or 0))
    except (TypeError, ValueError):
        fallback = 0
    return fallback, 1


class Renderer(Protocol):
    """渲染抽象（Web 实现见 web_console/live_stream）。"""
    async def on_status(self, status: str) -> None: ...
    async def on_tool(self, tool_line: str) -> None: ...
    async def on_tool_update(self, tool_line: str, *, tool_call_id: str = "", name: str = "", arguments: str = "") -> None: ...
    async def on_delta(self, full_text: str, reasoning: str = "") -> None: ...
    async def finalize(self, full_text: str, reasoning: str = "") -> None: ...
    async def finalize_notice(self, note: str) -> None: ...
    async def fail(self, error_text: str) -> None: ...
    def set_footer(self, footer_html: str) -> None: ...
    async def cut(self) -> None: ...  # 软分段：封口当前消息，之后另起一条


class Persister(Protocol):
    """逐单元持久化抽象。loop 每产出一个 assistant 轮次 / 一条工具结果就实时落库，
    保证工具调用、工具结果、思考、中间轮 assistant 文本都进 DB——既供「查看对话」
    完整展示，也保证多轮历史回放时模型能看到自己之前的工具上下文。"""
    async def save_assistant(self, *, content: str, reasoning: str, signature: str,
                             tool_calls: list[ToolCall],
                             native_output_items: list[dict[str, Any]] | None = None) -> None: ...
    async def save_tool_result(self, *, tool_call_id: str, name: str, content: str,
                               duration_ms: int = 0) -> None: ...
    async def save_user(self, *, content: str, metadata: dict[str, Any] | None = None) -> None: ...
    async def save_native_context(self, *, messages: list[Message]) -> None: ...


class EmergencyCompactor(Protocol):
    """应急压缩抽象：识别到「上下文超限」错误时强制压缩历史并重建会话。

    返回压缩后应当用于重试的完整 convo（system 之外的消息列表）。返回 None 表示
    无法再压缩（已到底/压缩失败），调用方应放弃重试、如实报错。convo 用于 Web
    多模态/运行时注入消息的内存态尾部合并，避免只从 DB 重建后丢附件/image blocks。
    """
    async def compact_and_rebuild(self, convo: list[Message] | None = None) -> list[Message] | None: ...


def _calls_signature(calls: list[ToolCall]) -> str:
    return "|".join(f"{c.name}:{c.arguments}" for c in calls)


def _assistant_message(
    *,
    content: str,
    reasoning: str,
    signature: str,
    tool_calls: list[ToolCall],
    native_output_items: list[dict[str, Any]],
) -> tuple[Message, bool]:
    """Build one readable assistant message plus one complete native turn."""
    message: Message = {
        "role": "assistant",
        "content": content or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning:
        message["reasoning"] = reasoning
    if signature:
        message["signature"] = signature
    native_items = native_items_for_tool_calls(
        native_output_items,
        tool_calls,
        tool_calls,
        has_content=bool(content),
        has_reasoning=bool(reasoning),
    )
    if native_items:
        message["native_output_items"] = native_items
    return message, not native_output_items or bool(native_items)


class Agent:
    def __init__(self, backend: LLMBackend, tools: ToolRegistry, *,
                 max_run_wall_seconds: float = 0.0,
                 no_progress_rounds: int = 8,
                 tool_result_max_chars: int = 16_000,
                 max_retries: int = 10,
                 retry_backoff_s: float = 0.5,
                 retry_max_delay_s: float = 32.0,
                 retry_jitter_ratio: float = 0.25,
                 max_overflow_retries: int = 3,
                 empty_response_retry_limit: int = 1,
                 reasoning_only_retry_limit: int = 2) -> None:
        self._backend = backend
        self._tools = tools
        self._max_wall = max_run_wall_seconds
        self._no_progress = no_progress_rounds
        self._tool_result_max_chars = tool_result_max_chars
        self._max_retries = max(0, int(max_retries or 0))
        self._retry_policy = RetryPolicy(
            max_retries=self._max_retries,
            base_delay_s=retry_backoff_s,
            max_delay_s=retry_max_delay_s,
            jitter_ratio=retry_jitter_ratio,
        )
        self._max_overflow_retries = max_overflow_retries
        # 模型「调用成功但没产出正文」时的补救重试上限(与 max_retries 的「调用失败重试」正交)。
        # empty:既无正文也无思考;reasoning_only:有思考但没正文(常见于推理模型卡在思考阶段)。
        self._empty_response_retry_limit = empty_response_retry_limit
        self._reasoning_only_retry_limit = reasoning_only_retry_limit

    async def run(
        self,
        messages: list[Message],
        renderer: Renderer,
        *,
        model: str,
        system: str = "",
        max_tokens: int = 8192,
        show_thinking: bool = False,
        think_level: str = "off",
        session_id: str = "",
        service_tier: str = "",
        fast_request: dict[str, Any] | None = None,
        persister: Persister | None = None,
        emergency_compactor: EmergencyCompactor | None = None,
        context_compactor: ContextCompactionGate | None = None,
        steer_drain: Callable[[], list[str]] | None = None,
        model_request_refresher: Callable[[list[Message]], Awaitable[list[Message]]] | None = None,
        model_request_overlay: Callable[[list[Message]], Awaitable[list[Message]]] | None = None,
        model_call_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        footer_provider: Callable[[RunResult], str] | None = None,
        result: RunResult | None = None,
        tool_context: ToolRuntimeContext | None = None,
        retry_cancel_check: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> RunResult:
        # result 可由调用方传入并持有同一引用：被停止时 run 直接抛 CancelledError、
        # 来不及 return,调用方仍能从这个共享对象读到 loop 一路累加的真实统计(耗时/调用
        # 次数/token),而不是一个全 0 的占位对象。
        if result is None:
            result = RunResult()
        assert result is not None

        def _apply_footer() -> None:
            """在每个收尾出口前把本轮统计页脚交给 renderer，使其拼进最后一条消息。"""
            if footer_provider is None:
                return
            try:
                renderer.set_footer(footer_provider(result))
            except Exception:
                pass
        convo = list(messages)
        tool_schemas = self._tools.schemas(scope="main")
        t0 = time.monotonic()
        result.start_monotonic = t0  # 供取消兜底现算 total_time_ms
        repeat_sigs: list[str] = []
        agent_terminal_snapshot_consumed = False
        async_agent_tool_result_seen = False
        agent_wait_reminder_sent = False
        agent_wait_required = False
        last_agent_wait_delay_s = 0.0
        round_no = 0
        emergency_overflow_tries = 0  # 本轮 run 已触发的应急压缩次数（防无限压缩）
        empty_retry = 0           # 已触发的「空响应」补救重试次数
        reasoning_only_retry = 0  # 已触发的「只思考无正文」补救重试次数
        open_rendered = False         # 当前 renderer 草稿里是否已有可封口内容（正文/工具行）
        native_continuation = str(getattr(self._backend, "protocol", "") or "").lower() == "responses"
        # The provider's last successful prompt-usage snapshot is authoritative for
        # compaction.  A local estimate cannot faithfully include Responses native
        # items, encrypted reasoning, function-call payloads, or tool schemas.
        # Track which snapshot a successful compaction consumed so an old oversized
        # value does not trigger a second compaction before the next model request.
        provider_prompt_usage_generation = 0
        compacted_provider_prompt_usage_generation = -1

        async def _persist_assistant(
            *,
            content: str,
            reasoning: str,
            signature: str,
            tool_calls: list[ToolCall],
            native_output_items: list[dict[str, Any]],
        ) -> None:
            if persister is None:
                return
            save_assistant = persister.save_assistant
            kwargs: dict[str, Any] = {
                "content": content,
                "reasoning": reasoning,
                "signature": signature,
                "tool_calls": tool_calls,
            }
            try:
                params = inspect.signature(save_assistant).parameters
                supports_native = "native_output_items" in params or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in params.values()
                )
            except Exception:
                supports_native = False
            if supports_native:
                kwargs["native_output_items"] = native_output_items
            await save_assistant(**kwargs)

        async def _checkpoint_native_context(context: list[Message], *, replayable: bool) -> None:
            if persister is None or (native_continuation and not replayable):
                return
            checkpoint = getattr(persister, "save_native_context", None)
            if not callable(checkpoint):
                return
            maybe = checkpoint(messages=list(context))
            if inspect.isawaitable(maybe):
                await maybe

        def _steer_text(item: Any) -> str:
            if isinstance(item, dict):
                return str(item.get("text") or item.get("content") or "").strip()
            return str(item or "").strip()

        def _merge_steers_for_injection(steers: list[Any]) -> list[str]:
            texts = [_steer_text(item) for item in steers]
            texts = [text for text in texts if text]
            if not texts:
                return []
            # Web 运行中插话在 composer 队列里可以多条排队；到 loop 边界时作为一个
            # 真实 user message 注入，保持“取出队列 → 一条插话消息 → 模型处理”的 UI/上下文一致性。
            if any(isinstance(item, dict) and str(item.get("source") or "") == "web" for item in steers):
                return ["\n\n".join(texts)]
            return texts

        async def _call_steer_hook(steers: list[Any], injected_texts: list[str], *, cut: bool) -> None:
            steer_hook = getattr(renderer, "on_steers_injected", None)
            if not callable(steer_hook):
                return
            try:
                params = inspect.signature(steer_hook).parameters
            except Exception:
                params = {}
            if not params:
                maybe = steer_hook()
            else:
                maybe = steer_hook(steers, injected_texts=injected_texts, cut=cut)
            if inspect.isawaitable(maybe):
                await maybe

        def _latest_provider_prompt_tokens() -> int:
            usage = result.last_usage
            return max(0, (
                int(usage.input_tokens or 0)
                + int(usage.cache_read_tokens or 0)
                + int(usage.cache_write_tokens or 0)
            ))

        async def _run_context_compaction_gate(source: str, prompt_tokens: int | None = None) -> bool:
            """Run normal compaction only at protocol-safe boundaries.

            The last provider usage is the exact size of the preceding outbound
            prompt.  Reuse it at the next safe boundary so a long tool loop cannot
            silently stay below the threshold merely because the local estimator
            omits opaque/native protocol state.  Explicit preflight projections
            remain additive and win when they are larger.
            """
            nonlocal convo, compacted_provider_prompt_usage_generation
            if context_compactor is None:
                return False
            gate_prompt_tokens = max(0, int(prompt_tokens or 0))
            if provider_prompt_usage_generation > compacted_provider_prompt_usage_generation:
                gate_prompt_tokens = max(gate_prompt_tokens, _latest_provider_prompt_tokens())
            new_convo = await context_compactor.maybe_compact_and_rebuild(
                source=source,
                prompt_tokens=gate_prompt_tokens or None,
                convo=convo,
            )
            if new_convo is None:
                return False
            # A rebuilt context begins a new cache epoch. Do not repeatedly apply
            # the old oversized snapshot; the next provider response supplies a
            # fresh authoritative snapshot before another normal compaction.
            compacted_provider_prompt_usage_generation = provider_prompt_usage_generation
            convo = list(new_convo)
            return True

        async def _close_unexecuted_tool_calls(calls: list[ToolCall], *, reason: str) -> None:
            """Close assistant-emitted tool calls that will not be dispatched.

            Once an assistant message with tool_calls is persisted, every declared
            call must have a matching tool result before the run returns or the next
            request/rebuild can violate strict provider protocols. This helper is
            only used at early-exit boundaries after the assistant tool_call has
            been accepted but before all tools were executed.
            """
            if not calls:
                return
            content = f"{MISSING_TOOL_RESULT_TEXT}\n\n[openbear] {reason}"
            for call in calls:
                call_id = str(getattr(call, "id", "") or "")
                name = str(getattr(call, "name", "") or "")
                if not call_id:
                    continue
                convo.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": content,
                })
                if persister is not None:
                    await persister.save_tool_result(
                        tool_call_id=call_id,
                        name=name,
                        content=content,
                        duration_ms=0,
                    )

        async def _inject_steers(steers: list[Any], *, cut: bool) -> bool:
            """把已排队插话注入 convo。返回是否确实注入。

            cut=True 时先软分段当前 Telegram 草稿；首次模型调用前如果已经有插话排队，
            此时草稿还只有状态文案，不应额外发送一个空消息。
            """
            nonlocal open_rendered
            if not steers:
                return False
            injected_texts = _merge_steers_for_injection(steers)
            if not injected_texts:
                return False
            if cut:
                await renderer.cut()
                open_rendered = False
            await _call_steer_hook(steers, injected_texts, cut=cut)
            for index, steer_text in enumerate(injected_texts):
                convo.append({"role": "user", "content": steer_text})
                if persister is not None:
                    source_items = steers if len(injected_texts) == 1 else [steers[index]]
                    message_uuids = [
                        str(item.get("messageUuid") or item.get("message_uuid") or "").strip()
                        for item in source_items if isinstance(item, dict)
                    ]
                    message_uuids = [item for item in message_uuids if item]
                    turn_uuids = [
                        str(item.get("turnUuid") or item.get("turn_uuid") or "").strip()
                        for item in source_items if isinstance(item, dict)
                    ]
                    turn_uuids = [item for item in turn_uuids if item]
                    metadata = {
                        "opIds": [f"msg:{item}" for item in message_uuids],
                        "sourceMessageUuids": message_uuids,
                        "turnUuid": turn_uuids[-1] if turn_uuids else "",
                    }
                    save_user = persister.save_user
                    try:
                        supports_metadata = "metadata" in inspect.signature(save_user).parameters
                    except Exception:
                        supports_metadata = False
                    if supports_metadata:
                        await save_user(content=steer_text, metadata=metadata)
                    else:  # Compatibility for third-party/test persisters.
                        await save_user(content=steer_text)
            result.steered += len([item for item in steers if _steer_text(item)])
            return True

        def _drain_steers() -> list[str]:
            if steer_drain is None:
                return []
            return steer_drain()

        while True:
            # 轮间注入：取出运行中排队的插话消息，作为 user 消息插入会话。
            # 放在每轮请求模型之前，模型下一轮即可见老大的补充指导/改目标。
            await _inject_steers(_drain_steers(), cut=open_rendered)

            # 正常上下文压缩 gate：只在请求模型前运行，避开 streaming 中间和
            # assistant tool_calls 已产生但 tool result 未完整落库的非法窗口。
            await _run_context_compaction_gate("pre_model_request")

            round_no += 1
            result.rounds = round_no
            await renderer.on_status("正在思考 …")

            full_text = ""
            reasoning_text = ""
            signature = ""
            pending: list[ToolCall] = []
            finish = ""
            produced_any = False
            retry_context: list[Message] = []
            native_output_items: list[dict[str, Any]] = []
            native_round_replayable = True

            async def _publish_retry_state(state: dict[str, Any]) -> None:
                retry_hook = getattr(renderer, "on_retry_state", None)
                if callable(retry_hook):
                    maybe = retry_hook(state)
                    if inspect.isawaitable(maybe):
                        await maybe
                if state.get("active"):
                    delay_display = round(int(state.get("delayMs") or 0) / 1000, 1)
                    readable_reason = str(state.get("summary") or "").strip()
                    await renderer.on_status(
                        f"模型调用失败{f'：{readable_reason}' if readable_reason else ''}，"
                        f"{delay_display:g} 秒后重试 "
                        f"（{state.get('attempt')}/{state.get('maxRetries')}）…"
                    )

            attempt = 0
            primary_model_error: OpenBearLLMError | None = None
            while True:
                call_t0 = time.monotonic()
                call_connect_ms = 0
                call_first_token_ms = 0
                call_output_tokens = 0
                call_usage = Usage()
                call_prompt_usage_reported = False
                call_service_tier = ""
                call_provider_cost_usd: float | None = None
                call_reasoning_t0 = 0.0   # 本次调用首个 reasoning 增量时刻
                call_reasoning_ms = 0     # 本次调用思考时长（首个 reasoning → 首个 content）
                result.model_calls += 1
                # Reconcile application runtime state into the actual private model
                # context before composing request-local retry recovery. Retaining the
                # returned context is what makes digest deduplication and cross-turn
                # checkpoints append-only; role repair then adds deterministic bridges
                # without merging an already emitted user unit.
                if model_request_refresher is not None:
                    convo = list(await model_request_refresher(convo))
                request_prefix = list(convo)
                if model_request_overlay is not None:
                    # Request-local overlays target the durable conversation prefix.
                    # A transient retry recovery user message is protocol machinery,
                    # not the user's latest real message.
                    request_prefix = list(await model_request_overlay(request_prefix))
                request_messages = list(request_prefix + retry_context)
                outbound = repair_role_alternation(request_messages)
                # produced_any 只描述当前 physical attempt。此前 attempt 已产生的
                # partial 会作为恢复上下文保留，但不会阻止后续可重试错误继续恢复。
                produced_any = False
                attempt_native_output_items: list[dict[str, Any]] = []
                stream_options: dict[str, Any] = {
                    "model": model,
                    "system": system,
                    "tools": tool_schemas,
                    "max_tokens": max_tokens,
                    "think_level": think_level,
                    "session_id": session_id,
                    "service_tier": service_tier,
                    "fast_request": dict(fast_request or {}),
                }
                if native_continuation:
                    stream_options["native_continuation"] = True
                try:
                    async for ev in self._backend.stream(outbound, **stream_options):
                        billing = ev.details if isinstance(ev.details, dict) else {}
                        reported_tier = str(billing.get("serviceTier") or "").strip().lower()
                        if reported_tier:
                            call_service_tier = reported_tier
                        if "providerCostUsd" in billing:
                            call_provider_cost_usd = billing.get("providerCostUsd")
                        if ev.kind == "error":
                            raise OpenBearLLMError.from_stream_event(
                                ev,
                                protocol=str(getattr(self._backend, "protocol", "") or ""),
                            )
                        if ev.kind == "metrics":
                            if ev.connect_ms:
                                call_connect_ms = ev.connect_ms
                                if not result.connect_ms:
                                    result.connect_ms = ev.connect_ms
                            continue
                        produced_any = True
                        if ev.kind in {"content", "reasoning", "tool_call"}:
                            if not result.first_token_ms:
                                result.first_token_ms = int((time.monotonic() - t0) * 1000)
                            if not call_first_token_ms:
                                call_first_token_ms = int((time.monotonic() - call_t0) * 1000)
                        if ev.kind == "content":
                            # 首个正文增量 = 思考结束：定格本次调用的思考时长。
                            if call_reasoning_t0 and not call_reasoning_ms:
                                call_reasoning_ms = int((time.monotonic() - call_reasoning_t0) * 1000)
                            full_text += ev.text
                            open_rendered = True
                            await renderer.on_delta(full_text, reasoning_text if show_thinking else "")
                        elif ev.kind == "reasoning":
                            # 首个 reasoning 增量：记下思考起点（含纯思考无正文的情况）。
                            if not call_reasoning_t0 and ev.text:
                                call_reasoning_t0 = time.monotonic()
                            reasoning_text += ev.text
                            if ev.signature:
                                signature = ev.signature
                            # Web 的实时态也要能看到“纯思考 -> 工具调用”的思考过程。
                            # 之前只在 content delta 时带上 reasoning，遇到模型先 reasoning 后直接
                            # tool_call 的轮次，live 阶段就只能看到“正在思考”占位，最终态才出现
                            # reasoning，造成两套时间线。
                            if show_thinking and reasoning_text:
                                await renderer.on_delta(full_text, reasoning_text)
                        elif ev.kind == "tool_call":
                            pending = ev.tool_calls
                        elif ev.kind == "native_output_item":
                            attempt_native_output_items.extend(
                                dict(item) for item in (ev.native_output_items or []) if isinstance(item, dict)
                            )
                        elif ev.kind == "usage" and ev.usage:
                            # 累加进总账（计费）；同时记下这一次 API 调用的快照。
                            # 最后一次调用的 prompt(input+cache) = 模型实际看到的整个上下文体积，
                            # 是上下文占用 / 压缩阈值判定唯一正确的依据。
                            result.usage.merge(ev.usage)
                            call_usage.merge(ev.usage)
                            # A usage event that only reports output is still useful
                            # for billing, but it does not report prompt/context usage.
                            if (call_usage.input_tokens + call_usage.cache_read_tokens
                                    + call_usage.cache_write_tokens) > 0:
                                call_prompt_usage_reported = True
                                result.last_usage = call_usage
                                provider_prompt_usage_generation += 1
                            call_output_tokens = ev.usage.output_tokens
                        elif ev.kind == "finish":
                            finish = ev.finish_reason
                    # 流正常结束：本次调用成功，累计指标用于求会话平均；同时记录最后一次调用快照。
                    call_time_ms = int((time.monotonic() - call_t0) * 1000)
                    # 思考收尾：若这次调用全程只有 reasoning、没等到 content（如纯思考后
                    # 直接发起工具调用），用「首个 reasoning → 流结束」作为思考时长。
                    if call_reasoning_t0 and not call_reasoning_ms:
                        call_reasoning_ms = int((time.monotonic() - call_reasoning_t0) * 1000)
                    result.model_ok += 1
                    result.connect_ms_sum += call_connect_ms
                    result.first_token_ms_sum += call_first_token_ms
                    result.call_time_ms_sum += call_time_ms
                    result.output_tokens_sum += call_output_tokens
                    if call_time_ms > 0 and call_output_tokens > 0:
                        call_tps = call_output_tokens * 1000.0 / call_time_ms
                        result.peak_tps = max(result.peak_tps, call_tps)
                        result.min_tps = call_tps if result.min_tps <= 0 else min(result.min_tps, call_tps)
                    result.reasoning_ms_sum += call_reasoning_ms
                    result.last_call_connect_ms = call_connect_ms
                    result.last_call_first_token_ms = call_first_token_ms
                    result.last_call_time_ms = call_time_ms
                    result.last_prompt_usage_reported = call_prompt_usage_reported
                    if call_prompt_usage_reported:
                        result.last_usage = call_usage
                    if model_call_hook is not None:
                        try:
                            await model_call_hook({
                                "status": "ok",
                                "usage": call_usage,
                                "promptUsageReported": call_prompt_usage_reported,
                                "connectMs": call_connect_ms,
                                "firstTokenMs": call_first_token_ms,
                                "totalTimeMs": call_time_ms,
                                "reasoningMs": call_reasoning_ms,
                                "outputTokens": call_output_tokens,
                                "retry": attempt > 0,
                                "round": round_no,
                                "serviceTier": call_service_tier,
                                "providerCostUsd": call_provider_cost_usd,
                            })
                        except Exception:
                            log.exception("模型调用即时记账失败，终止当前运行以避免继续产生未入账请求", 轮次=round_no)
                            raise
                    native_output_items = (
                        list(attempt_native_output_items) if native_round_replayable else []
                    )
                    break  # 流正常结束
                except OpenBearLLMError as e:
                    if primary_model_error is None and e.structured:
                        primary_model_error = e
                    # Every physical upstream attempt gets its own durable row,
                    # including retryable/overflow failures.  Do this before any
                    # retry/compaction branch can issue the next request. RunResult's
                    # model_fail still means terminal failures, so it is incremented
                    # only after all recovery/retry branches are exhausted below.
                    if model_call_hook is not None:
                        try:
                            await model_call_hook({
                                "status": "error",
                                "usage": call_usage,
                                "promptUsageReported": call_prompt_usage_reported,
                                "connectMs": call_connect_ms,
                                "firstTokenMs": call_first_token_ms,
                                "totalTimeMs": int((time.monotonic() - call_t0) * 1000),
                                "reasoningMs": call_reasoning_ms,
                                "outputTokens": call_output_tokens,
                                "retry": attempt > 0,
                                "round": round_no,
                                "errorType": e.reason or str(e.status or "") or type(e).__name__,
                                "serviceTier": call_service_tier,
                                "providerCostUsd": call_provider_cost_usd,
                            })
                        except Exception:
                            log.exception("失败模型调用即时记账失败，终止当前运行以避免继续产生未入账请求", 轮次=round_no)
                            raise
                    # 上下文超限自救：靠错误文本识别（不依赖 usage，覆盖不报 usage 的模型）。
                    # 即使已经流出少量正文，也应压缩后重试；Responses/代理层可能先吐出
                    # partial output，随后才以 context_length_exceeded/incomplete 结束。
                    if (emergency_compactor is not None
                            and emergency_overflow_tries < self._max_overflow_retries
                            and is_context_overflow_error(e.message)):
                        emergency_overflow_tries += 1
                        log.warning("上下文超限，触发应急压缩后重试", 轮次=round_no,
                                    应急次数=emergency_overflow_tries, 错误=e.message[:120])
                        await renderer.on_status("上下文偏长，正在压缩历史后重试 …")
                        try:
                            new_convo = await emergency_compactor.compact_and_rebuild(convo=convo)
                        except TypeError:
                            # Backward compatibility for existing test/fallback compactor shims.
                            new_convo = await emergency_compactor.compact_and_rebuild()
                        if new_convo is not None:
                            result.model_retry += 1
                            if full_text or reasoning_text or pending or attempt_native_output_items:
                                native_round_replayable = False
                            native_output_items = []
                            convo = new_convo
                            full_text = reasoning_text = signature = ""
                            pending = []
                            finish = ""
                            produced_any = False
                            continue
                        # 压不动了（已到底/失败）→ 落到下面如实报错
                    # 完整 tool call 已经到达时不重放请求，直接执行该工具，避免副作用重复。
                    # content/reasoning partial 则作为 recovery context 交给下一 attempt 续写；
                    # 这与 Agent 的职责一致：一次传输中断不能直接杀掉整个任务。
                    if e.retryable and pending:
                        result.model_retry += 1  # usable partial recovery; keeps physical-call counters balanced
                        finish = "tool_calls"
                        native_output_items = (
                            list(attempt_native_output_items) if native_round_replayable else []
                        )
                        log.warning("流中断但工具调用已完整到达，继续执行工具", 轮次=round_no, 工具数=len(pending))
                        break
                    if e.retryable and attempt < self._max_retries:
                        attempt += 1
                        # The recovery instruction is request-local and is not part
                        # of the durable neutral transcript. This physical round may
                        # continue, but its opaque output must not be checkpointed.
                        native_round_replayable = False
                        native_output_items = []
                        result.model_retry += 1
                        wait = self._retry_policy.delay(
                            attempt,
                            retry_after_s=float(getattr(e, "retry_after_s", 0.0) or 0.0),
                        )
                        # 只把可公开正文作为 assistant partial 回灌；未签名 reasoning 不伪装成
                        # Anthropic signed thinking。下一条 user recovery 指令要求从中断点继续。
                        retry_context = []
                        if full_text.strip():
                            retry_context.append({"role": "assistant", "content": full_text})
                        retry_context.append({
                            "role": "user",
                            "content": (
                                "The previous model response was interrupted by a transient upstream error. "
                                "Continue the same task from the exact interruption point. Do not repeat completed "
                                "text or completed work; preserve all prior tool results and instructions."
                            ),
                        })
                        # 多 attempt 的 signed thinking 不能拼成一个有效签名；保留可见 reasoning，
                        # 但清空签名，避免后续把错误签名回传给 Anthropic。
                        signature = ""
                        state = retry_wait_payload(
                            retry_number=attempt,
                            max_retries=self._max_retries,
                            delay_s=wait,
                            reason=e.reason,
                            error=e.message,
                            summary=e.user_message(),
                            transport_status=e.transport_status,
                            upstream_status=e.upstream_status or e.status,
                            root_cause=e.root_cause,
                            attempts=e.attempts,
                            details=e.details,
                        )
                        log.warning("上游可重试错误，指数退避后恢复", 轮次=round_no, 尝试=attempt,
                                    等待秒=round(wait, 3), 已产出=produced_any, 错误=e.message[:120])
                        try:
                            await wait_for_retry(
                                wait,
                                state=state,
                                cancel_check=retry_cancel_check,
                                on_update=_publish_retry_state,
                            )
                        except RetryCancelledError:
                            result.model_fail += 1
                            result.halted_reason = "retry_cancelled"
                            result.total_time_ms = int((time.monotonic() - t0) * 1000)
                            if full_text.strip():
                                await _persist_assistant(
                                    content=full_text,
                                    reasoning=reasoning_text,
                                    signature="",
                                    tool_calls=[],
                                    native_output_items=[],
                                )
                            result.text = full_text
                            _apply_footer()
                            await renderer.finalize_notice("（已取消模型重试；之前完成的内容已保留）")
                            return result
                        pending = []
                        finish = ""
                        continue
                    result.model_fail += 1
                    # A retry can fail secondarily while reconstructing provider
                    # format. Preserve the first structured upstream cause for the
                    # terminal explanation without turning this into an error ledger.
                    terminal_error = (
                        primary_model_error
                        if primary_model_error is not None and primary_model_error is not e and e.reason == "format"
                        else e
                    )
                    log.error("Agent上游错误", 轮次=round_no, 错误=terminal_error.message,
                              已重试=attempt, 已产出=produced_any)
                    # 本轮若已产出部分正文，落库避免上下文丢失（与是否覆盖渲染无关）。
                    if full_text.strip():
                        await _persist_assistant(
                            content=full_text,
                            reasoning=reasoning_text,
                            signature=signature,
                            tool_calls=[],
                            native_output_items=[],
                        )
                        result.text = full_text
                    # 统一走 fail()：它会把错误**追加**到 renderer 已有的整条时间线
                    # 末尾（前面几十轮的正文/工具行原样保留），绝不覆盖。不能再用
                    # 「本轮 full_text 是否为空」来决定——503 常发生在工具后续写轮，
                    # 本轮 full_text 为空但前面早有大量输出。
                    result.total_time_ms = int((time.monotonic() - t0) * 1000)
                    _apply_footer()
                    await renderer.fail(terminal_error.user_message())
                    return result

            result.reasoning += reasoning_text

            # 工具调用分支
            if finish == "tool_calls" and pending:
                pending = normalize_tool_calls(pending)
                batch_pin = getattr(renderer, "pin_tool_batch_turn", None)
                if callable(batch_pin):
                    maybe = batch_pin([tc.id for tc in pending if tc.id])
                    if inspect.isawaitable(maybe):
                        await maybe
                # 把 assistant 消息（可读字段 + 一份完整原生 turn）入会话。
                assistant_message, native_turn_complete = _assistant_message(
                    content=full_text,
                    reasoning=reasoning_text,
                    signature=signature,
                    tool_calls=pending,
                    native_output_items=native_output_items,
                )
                native_round_replayable = native_round_replayable and native_turn_complete
                convo.append(assistant_message)
                # 普通消息表只保存可读字段；原生 items 由完整边界 checkpoint 私有保存。
                await _persist_assistant(
                    content=full_text,
                    reasoning=reasoning_text,
                    signature=signature,
                    tool_calls=pending,
                    native_output_items=list(assistant_message.get("native_output_items") or []),
                )
                # 无进展打转检测
                sig = _calls_signature(pending)
                if not full_text.strip():
                    repeat_sigs.append(sig)
                    if len(repeat_sigs) >= self._no_progress and len(set(repeat_sigs[-self._no_progress:])) == 1:
                        log.warning("检测到工具调用打转，强制收尾", 轮次=round_no)
                        result.halted_reason = "no_progress"
                        result.total_time_ms = int((time.monotonic() - t0) * 1000)
                        # assistant tool_calls 已落库但本批工具不再执行；先补占位结果闭口，
                        # 避免 DB/build_history 留下光杆 tool_call。
                        await _close_unexecuted_tool_calls(pending, reason="检测到重复工具调用，已中止执行。")
                        # 追加中止提示，保留前面已渲染的所有正文/工具行（不覆盖）
                        _apply_footer()
                        await renderer.finalize_notice("（检测到重复调用，已中止）")
                        return result
                else:
                    repeat_sigs.clear()

                detached_agent_count = 0
                delivered_agent_output_tokens = 0
                delivered_agent_result_count = 0
                for tc in pending:
                    result.tools_used.append(tc.name)
                    is_interaction = is_user_interaction_tool(tc.name)
                    # 普通工具直接显示调用意图；用户交互工具会弹独立卡片，
                    # 完成后再把干净结果写回主消息，避免聊天里留下两份杂乱信息。
                    audit_arguments = redact_tool_arguments_for_audit(tc.name, tc.arguments)
                    tool_line = format_tool_line(tc.name, audit_arguments)
                    web_tool_start = getattr(renderer, "on_tool_start", None)
                    if callable(web_tool_start):
                        maybe = web_tool_start(tc.id, tc.name, audit_arguments, tool_line)
                        if inspect.isawaitable(maybe):
                            await maybe
                    else:
                        await renderer.on_tool(tool_line)
                    open_rendered = True
                    if is_interaction:
                        await renderer.on_status(format_user_interaction_wait_line(tc.name, audit_arguments))
                    elif running_status := format_tool_running_status_line(tc.name, audit_arguments):
                        await renderer.on_status(running_status)
                    log.info("执行工具", 轮次=round_no, 工具=tc.name, 参数=audit_arguments[:200])
                    tool_t0 = time.monotonic()
                    structured_progress = getattr(renderer, "on_tool_progress", None)
                    text_progress = getattr(renderer, "on_tool_update", None)
                    # Detached tools may retain these callbacks after dispatch
                    # returns and after this loop advances to a later ToolCall.
                    # Bind the current call metadata/callbacks now; closing over
                    # the mutable `tc` loop variable attributes late AgentMessage
                    # progress to a subsequent AgentWait and creates zombie Web
                    # agent_control operations.
                    progress_tool_call_id = tc.id
                    progress_tool_name = tc.name
                    progress_tool_arguments = audit_arguments

                    async def _structured_progress(
                        payload: dict[str, Any],
                        *,
                        _callback: Any = structured_progress,
                        _tool_call_id: str = progress_tool_call_id,
                        _tool_name: str = progress_tool_name,
                        _tool_arguments: str = progress_tool_arguments,
                    ) -> None:
                        if callable(_callback):
                            maybe = _callback(_tool_call_id, _tool_name, _tool_arguments, payload)
                            if inspect.isawaitable(maybe):
                                await maybe

                    async def _text_progress(
                        line: str,
                        *,
                        _callback: Any = text_progress,
                        _tool_call_id: str = progress_tool_call_id,
                        _tool_name: str = progress_tool_name,
                        _tool_arguments: str = progress_tool_arguments,
                    ) -> None:
                        if not callable(_callback):
                            return
                        try:
                            maybe = _callback(
                                line,
                                tool_call_id=_tool_call_id,
                                name=_tool_name,
                                arguments=_tool_arguments,
                            )
                        except TypeError:
                            # Legacy renderers only accept the display line. Keep
                            # text-compatible renderers working while Web
                            # gets durable toolCallId attribution.
                            maybe = _callback(line)
                        if inspect.isawaitable(maybe):
                            await maybe

                    dispatch_context = replace(
                        tool_context or ToolRuntimeContext(),
                        tool_call_id=tc.id,
                        progress_update=_text_progress if callable(text_progress) else None,
                        progress_update_payload=_structured_progress if callable(structured_progress) else None,
                    )
                    # Agent terminal results never lock the main controller's tools:
                    # they are intermediate inputs to the root task. AgentWait still
                    # has precise batch/redundancy guards because waiting is valid only
                    # when no direct foreground work remains.
                    has_foreground_tool_in_batch = any(
                        other.name not in _AGENT_ORCHESTRATION_TOOLS
                        for other in pending
                        if other.id != tc.id
                    )
                    if tc.name == "AgentWait" and has_foreground_tool_in_batch:
                        tool_result = json.dumps({
                            "ok": False,
                            "error": "foreground_work_in_same_batch",
                            "message": "Finish the ordinary foreground tools first; call AgentWait only in a later model step when no direct user work remains.",
                        }, ensure_ascii=False)
                    elif tc.name == "AgentWait" and agent_terminal_snapshot_consumed and not agent_wait_required:
                        # The model may ask to "confirm" completion repeatedly. Once
                        # this root turn consumed an all-terminal snapshot, do not
                        # dispatch another wait cycle or create another visible Web
                        # supervision operation.
                        tool_result = _redundant_agent_wait_result()
                    else:
                        tool_result = await self._tools.dispatch(
                            tc.name,
                            tc.arguments,
                            max_chars=self._tool_result_max_chars,
                            context=dispatch_context,
                        )
                    tool_duration_ms = int((time.monotonic() - tool_t0) * 1000)
                    _apply_agent_tool_usage(result, tc.name, tool_result)
                    delivery_tokens, delivery_count = _agent_result_delivery_budget(tc.name, tool_result)
                    delivered_agent_output_tokens += delivery_tokens
                    delivered_agent_result_count += delivery_count
                    audit_tool_result = redact_tool_result_for_audit(tc.name, tool_result, tc.arguments)
                    web_tool_result = getattr(renderer, "on_tool_result", None)
                    if callable(web_tool_result):
                        maybe = web_tool_result(tc.id, tc.name, audit_arguments, audit_tool_result, tool_duration_ms)
                        if inspect.isawaitable(maybe):
                            await maybe
                        open_rendered = True
                    elif is_interaction:
                        await renderer.on_tool(format_user_interaction_result_line(tc.name, audit_arguments, audit_tool_result))
                        open_rendered = True
                    elif result_line := format_tool_result_line(tc.name, audit_arguments, audit_tool_result, tool_duration_ms):
                        await renderer.on_tool_update(result_line)
                        open_rendered = True
                    # The raw payload remains available to accounting and
                    # controller context. Renderer/audit projection uses only the
                    # redacted copy above; controller transcript remains unchanged.
                    controller_tool_result = project_agent_tool_result_for_controller(tc.name, tool_result)
                    convo.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": controller_tool_result,
                    })
                    # Persist exactly what future controller turns may replay,
                    # never the internal Agent telemetry envelope.
                    if persister is not None:
                        await persister.save_tool_result(
                            tool_call_id=tc.id, name=tc.name, content=controller_tool_result,
                            duration_ms=tool_duration_ms)
                    if _agent_tool_terminal_payload(tc.name, tool_result):
                        agent_terminal_snapshot_consumed = True
                    detached_payload = _detached_agent_tool_payload(tc.name, tool_result)
                    if detached_payload:
                        detached_agent_count += 1
                        async_agent_tool_result_seen = True
                        agent_wait_required = True
                        # A newly running/resumed Agent starts a fresh supervision
                        # generation and unlocks AgentWait for this same root turn.
                        agent_terminal_snapshot_consumed = False
                    if tc.name == "AgentWait":
                        wait_payload = _json_obj(tool_result)
                        if bool(wait_payload.get("ok")):
                            try:
                                parsed_wait_args = json.loads(tc.arguments or "{}")
                            except Exception:
                                parsed_wait_args = {}
                            last_agent_wait_delay_s = float((parsed_wait_args if isinstance(parsed_wait_args, dict) else {}).get("reviewAfterSeconds") or 0.0)
                            summary = wait_payload.get("summary") if isinstance(wait_payload.get("summary"), dict) else {}
                            remaining = int(summary.get("running") or 0) + int(summary.get("waitingControl") or 0)
                            # Each AgentWait arms exactly one wait. After it wakes,
                            # the model must explicitly choose a fresh wait if scoped
                            # Agents remain; an old review plan never carries forward.
                            agent_wait_required = remaining > 0
                            if remaining == 0:
                                # Hard terminal latch for AgentWait only: future waits
                                # in this root turn are hidden no-ops until a new Agent
                                # starts. Ordinary controller tools remain available.
                                agent_terminal_snapshot_consumed = True
                            agent_wait_reminder_sent = False
                    soft_stop_reason = ""
                    if dispatch_context.soft_stop_check is not None:
                        try:
                            soft_stop_reason = dispatch_context.soft_stop_check() or ""
                        except Exception:
                            soft_stop_reason = ""
                    if soft_stop_reason:
                        result.halted_reason = "soft_stop"
                        result.total_time_ms = int((time.monotonic() - t0) * 1000)
                        remaining = pending[pending.index(tc) + 1:] if tc in pending else []
                        await _close_unexecuted_tool_calls(
                            remaining,
                            reason=f"运行被停止，后续工具未执行：{soft_stop_reason}",
                        )
                        _apply_footer()
                        await renderer.finalize_notice(f"⏹ 已停止（{soft_stop_reason}）")
                        return result

                # Agent detached 只是异步工具结果；继续主控循环，由模型基于返回的
                # task id/status 决定是否继续调度、向用户说明等待，或用 AgentMessage/AgentStop 控制后台任务。

                # 工具批次已全部回灌并落库，此时才允许正常压缩；压缩成功后替换
                # convo，下一次 continue 请求模型会看到新摘要窗口而非旧长历史。
                # Agent terminal results use their provider-reported final output
                # tokens in addition to the latest real controller prompt. The Web
                # gate also compares the fully assembled convo estimate so JSON/tool
                # envelopes cannot make this preflight under-count.
                if delivered_agent_result_count > 0:
                    controller_prompt_tokens = (
                        int(result.last_usage.input_tokens or 0)
                        + int(result.last_usage.cache_read_tokens or 0)
                        + int(result.last_usage.cache_write_tokens or 0)
                    )
                    envelope_reserve = max(256, 64 * delivered_agent_result_count)
                    projected_tokens = controller_prompt_tokens + delivered_agent_output_tokens + envelope_reserve
                    await _run_context_compaction_gate(
                        "agent_result_preflight",
                        prompt_tokens=projected_tokens,
                    )
                else:
                    await _run_context_compaction_gate("tool_batch")

                # Only a fully persisted assistant/tool batch may advance the
                # private model chain. If compaction rebuilt convo, checkpoint the
                # rebuilt neutral context rather than stale opaque items.
                await _checkpoint_native_context(
                    convo,
                    replayable=native_round_replayable,
                )

                # 软约束检查
                if self._max_wall > 0 and time.monotonic() - t0 > self._max_wall:
                    result.halted_reason = "wall_time"
                    log.warning("单轮墙钟超时，收尾", 耗时=round(time.monotonic() - t0))
                    result.total_time_ms = int((time.monotonic() - t0) * 1000)
                    # 追加超时提示，保留前面已渲染的所有正文/工具行（不覆盖）
                    _apply_footer()
                    await renderer.finalize_notice("（已达时长上限，停止）")
                    return result
                continue

            # 正常结束
            # 最终模型调用期间也可能收到插话。此时如果直接 finalize 并 return，
            # on_message 已经把新消息排入 steering 队列且不再另起 run，就会形成静默吞消息。
            # 所以在定稿前最后 drain 一次：先把当前 assistant 作为上一段落库/回灌，
            # 软分段后把插话作为 user 注入，再继续请求模型。
            final_steers = _drain_steers()
            if final_steers:
                if full_text or reasoning_text:
                    assistant_message, native_turn_complete = _assistant_message(
                        content=full_text,
                        reasoning=reasoning_text,
                        signature=signature,
                        tool_calls=[],
                        native_output_items=native_output_items,
                    )
                    native_round_replayable = native_round_replayable and native_turn_complete
                    convo.append(assistant_message)
                    await _persist_assistant(
                        content=full_text,
                        reasoning=reasoning_text,
                        signature=signature,
                        tool_calls=[],
                        native_output_items=list(assistant_message.get("native_output_items") or []),
                    )
                    await _checkpoint_native_context(
                        convo,
                        replayable=native_round_replayable,
                    )
                await _inject_steers(final_steers, cut=open_rendered)
                continue

            # Detached Agents remain owned by this root turn, but waiting is now an
            # explicit model decision through AgentWait. If foreground work remains,
            # the controller simply keeps using normal tools. If no work remains and
            # the model forgets AgentWait, remind it once; a second miss falls back to
            # runtime event-only waiting without a periodic model timer.
            if async_agent_tool_result_seen and agent_wait_required:
                wait_callback = getattr(tool_context, "agent_wait", None) if tool_context is not None else None
                if callable(wait_callback):
                    if full_text or reasoning_text:
                        assistant_message, native_turn_complete = _assistant_message(
                            content=full_text,
                            reasoning=reasoning_text,
                            signature=signature,
                            tool_calls=[],
                            native_output_items=native_output_items,
                        )
                        native_round_replayable = native_round_replayable and native_turn_complete
                        convo.append(assistant_message)
                        await _persist_assistant(
                            content=full_text,
                            reasoning=reasoning_text,
                            signature=signature,
                            tool_calls=[],
                            native_output_items=list(assistant_message.get("native_output_items") or []),
                        )
                        await _checkpoint_native_context(
                            convo,
                            replayable=native_round_replayable,
                        )
                        await renderer.cut()
                        open_rendered = False
                    if not agent_wait_reminder_sent:
                        agent_wait_reminder_sent = True
                        convo.append({
                            "role": "user",
                            "content": (
                                "<controller-agent-wait-protocol internal=\"true\">\n"
                                "当前 root turn 仍有后台 Agent。若还有直接用户工作，继续完成它；"
                                "若已无前台工作，请调用 AgentWait，选择 event_only 或由你决定的 review_after。"
                                "统一复查全部 scoped Agents；健康推进时优先比上次等待更久，介入、停滞或临近预算时缩短/重置。"
                                f"上次 review_after 秒数：{last_agent_wait_delay_s:g}。"
                                "不要结束 root turn，也不要用 Process/Bash sleep 监控 Agent。\n"
                                "</controller-agent-wait-protocol>"
                            ),
                        })
                        continue
                    fallback = await wait_callback({
                        "mode": "event_only",
                        "reviewAfterSeconds": 0.0,
                        "reason": "模型未选择 AgentWait，运行时退化为仅事件唤醒",
                        "fallback": True,
                    })
                    fallback_payload = _json_obj(fallback)
                    fallback_summary = fallback_payload.get("summary") if isinstance(fallback_payload.get("summary"), dict) else {}
                    agent_wait_required = (
                        int(fallback_summary.get("running") or 0)
                        + int(fallback_summary.get("waitingControl") or 0)
                    ) > 0
                    agent_wait_reminder_sent = False
                    if fallback.strip():
                        convo.append({"role": "user", "content": fallback})
                        continue

            # After an explicit AgentWait has returned, the resulting aggregate
            # snapshot was already fed back as a tool result. A stop response here
            # either synthesizes terminal results or deliberately closes after no
            # scoped Agent remains.

            # #11 空响应 / 只思考补救重试：模型这轮调用成功(finish=stop)但没吐正文。
            # 偶发于上游波动或推理模型卡在思考阶段。注入一句引导后重发本轮,大概率恢复;
            # 用独立计数上限封死,避免模型持续不出正文导致死循环。
            if not full_text.strip():
                only_reasoning = bool(reasoning_text.strip())
                if only_reasoning and reasoning_only_retry < self._reasoning_only_retry_limit:
                    reasoning_only_retry += 1
                    result.model_retry += 1
                    log.warning("模型只思考无正文，补救重试", 轮次=round_no, 次数=reasoning_only_retry)
                    await renderer.on_status("重新组织回答 …")
                    convo.append({"role": "user",
                                  "content": "你刚才只有思考、没有输出正文回复。请基于已有信息直接给出最终回答。"})
                    continue
                if not only_reasoning and empty_retry < self._empty_response_retry_limit:
                    empty_retry += 1
                    result.model_retry += 1
                    log.warning("模型空响应，补救重试", 轮次=round_no, 次数=empty_retry)
                    await renderer.on_status("重新组织回答 …")
                    convo.append({"role": "user",
                                  "content": "你刚才没有输出任何回复。请基于已有信息直接给出最终回答。"})
                    continue

            # #14 stop reason 兜底：length 截断给用户明确提示;未知 finish_reason 记日志便于排障。
            if finish == "length":
                log.warning("回复被长度上限截断", 轮次=round_no, 长度=len(full_text))
            elif finish and finish not in ("stop", "tool_calls", ""):
                log.info("未知 finish_reason", 轮次=round_no, finish_reason=finish)

            result.text = full_text
            # 实时落库普通字段；完整 model-visible context 只进私有 checkpoint。
            if full_text or reasoning_text or native_output_items:
                assistant_message, native_turn_complete = _assistant_message(
                    content=full_text,
                    reasoning=reasoning_text,
                    signature=signature,
                    tool_calls=[],
                    native_output_items=native_output_items,
                )
                native_round_replayable = native_round_replayable and native_turn_complete
                await _persist_assistant(
                    content=full_text,
                    reasoning=reasoning_text,
                    signature=signature,
                    tool_calls=[],
                    native_output_items=list(assistant_message.get("native_output_items") or []),
                )
                await _checkpoint_native_context(
                    convo + [assistant_message],
                    replayable=native_round_replayable,
                )
            result.total_time_ms = int((time.monotonic() - t0) * 1000)
            # 统计页脚拼进这条最终消息末尾（与正文同属一条消息）
            _apply_footer()
            final_body = full_text or "（空回复）"
            if finish == "length":
                # 被模型 max_tokens 截断:给老大明确提示,免得以为回复莫名其妙断在半截。
                final_body = f"{final_body}\n\n⚠️ 回复达到长度上限被截断。"
            await renderer.finalize(final_body,
                                    reasoning_text if show_thinking else "")
            log.info("Agent完成", 轮次=round_no, 工具=result.tools_used or "无",
                     回复长度=len(full_text), Token=result.usage.total_tokens)
            return result
