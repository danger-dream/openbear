"""One physical model request and its optional logical recovery driver.

Only ``settle`` accounts for physical usage; ``LogicalResponse.response.usage`` is
intentionally empty. A summary caller can use ``execute_attempt`` directly and
retain its own generation/retry policy.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.llm.base import AgentResult, Message, OpenBearLLMError, apply_provider_billing
from app.llm.events import StreamEvent, Usage
from app.llm.retry import RetryPolicy, retry_wait_payload, wait_for_retry
from app.runtime.lifecycle import _consume_cleanup_result, current_session
from app.runtime.thinking_recovery import is_anthropic_prefix_mismatch


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    backend: Any
    messages: list[Message]
    options: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The prepared view is independent of the mutable context/config used to
        # assemble it. Backend receives yet another copy at the transport boundary.
        object.__setattr__(self, "messages", copy.deepcopy(self.messages))
        object.__setattr__(self, "options", copy.deepcopy(self.options))
        object.__setattr__(self, "metadata", copy.deepcopy(self.metadata))


@dataclass(slots=True)
class AttemptOutcome:
    request: PreparedRequest
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    response: AgentResult = field(default_factory=AgentResult)
    status: Literal["ok", "error", "cancelled"] = "ok"
    error: OpenBearLLMError | None = None
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    usage_reported: bool = False
    prompt_usage_reported: bool = False
    connect_ms: int = 0
    first_token_ms: int = 0
    total_time_ms: int = 0
    reasoning_ms: int = 0
    output_started: bool = False


@dataclass(slots=True)
class LogicalResponse:
    response: AgentResult
    attempts: int
    retries: int
    recovered_tool_calls: bool = False
    # True only if the response is one uninterrupted physical provider turn.
    native_replayable: bool = True


EventCallback = Callable[[StreamEvent, AgentResult, AttemptOutcome], Any]
SettleCallback = Callable[[AttemptOutcome], Any]
StartCallback = Callable[[AttemptOutcome], Any]


async def _invoke(callback: Callable[..., Any], *args: Any) -> Any:
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


async def _cancel_settle(settle: SettleCallback, outcome: AttemptOutcome,
                         timeout_s: float) -> None:
    """A cancelled transport still gets one bounded attempt at settlement."""
    task = asyncio.create_task(_invoke(settle, outcome))
    deadline = asyncio.get_running_loop().time() + max(0.01, timeout_s)
    while not task.done():
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated stop keeps the original deadline, not a new attempt or
            # permission to discard usage already received from the provider.
            continue
        except TimeoutError:
            task.cancel()
            task.add_done_callback(_consume_cleanup_result)
            break
        except Exception:
            break
    if task.done():
        _consume_cleanup_result(task)


async def execute_attempt(
    request: PreparedRequest, *,
    settle: SettleCallback | None = None,
    after_settle: SettleCallback | None = None,
    on_start: StartCallback | None = None,
    on_event: EventCallback | None = None,
    logical_partial: AgentResult | None = None,
    cancel_check: Callable[[], Any] | None = None,
    mode: Literal["auto", "stream", "complete"] = "auto",
    cancel_settle_timeout_s: float = 5.0,
) -> AttemptOutcome:
    """Execute exactly one request. Provider errors are returned in the outcome.

    Callback/control errors propagate *after* settling a started attempt; they
    are never converted to retryable provider errors. An interrupted callback
    produces status=error with error=None (not an upstream error). ``mode='complete'`` is for
    fake/explicit non-stream callers; auto prefers the stream whenever available.
    Cancellation propagates after bounded settlement with known usage/partial.
    ``on_start`` commits pre-request facts; failure prevents transport/settle.
    Its latency is excluded from the physical request timing.
    """
    # A composite backend may select another physical provider, but must execute
    # every request through this boundary (no synthetic outer attempt to bill).
    composite = getattr(request.backend, "execute_physical_attempts", None)
    if callable(composite):
        return await composite(request, settle=settle, after_settle=after_settle,
            on_start=on_start, on_event=on_event, logical_partial=logical_partial,
            cancel_check=cancel_check, mode=mode, cancel_settle_timeout_s=cancel_settle_timeout_s)
    from app.runtime.budget import current_budget
    budget = current_budget()
    if budget is not None:
        await budget.before_call()
    if mode not in {"auto", "stream", "complete"}:
        raise ValueError(f"unknown model call mode: {mode}")
    stream = getattr(request.backend, "stream", None)
    if mode == "stream" and not callable(stream):
        raise TypeError("backend does not support stream")
    if mode == "complete" or not callable(stream):
        if not callable(getattr(request.backend, "complete", None)):
            raise TypeError("backend does not support complete")
        use_stream = False
    else:
        use_stream = True
    if cancel_check is not None and await _invoke(cancel_check):
        raise asyncio.CancelledError()
    # Do not give a provider/callback access to the request's frozen snapshot.
    messages = copy.deepcopy(request.messages)
    options = copy.deepcopy(request.options)
    outcome = AttemptOutcome(request=request)
    session = current_session()
    if session is not None:
        await session.start_attempt(outcome)
    caller_settle = settle

    async def commit_attempt(value):
        if session is None:
            if caller_settle is not None:
                await _invoke(caller_settle, value)
        else:
            async with session.account_attempt(value) as first:
                if first and caller_settle is not None:
                    await _invoke(caller_settle, value)
                if first and budget is not None and callable(getattr(budget, "account_attempt", None)):
                    await budget.account_attempt(value)
        # Presentation may take a Web publish lock and wait for another writer.
        # It must never run inside the atomic ledger/action transaction.
        if after_settle is not None:
            await _invoke(after_settle, value)
        if budget is not None:
            await budget.after_call()

    settle = commit_attempt
    try:
        if on_start is not None:
            await _invoke(on_start, outcome)
        if budget is not None:
            # A preflight writer/presentation wait can cross midnight. Recheck
            # the current day and stamp immediately before entering transport.
            await budget.before_call()
        outcome.started_at_ms = int(time.time() * 1000)
        if budget is not None and callable(getattr(budget, "start_attempt", None)):
            await budget.start_attempt(outcome)
    except BaseException:
        # Preflight persistence/control failed before invoking the provider.
        if session is not None and session.store is not None:
            await session.store.finish_action(outcome.attempt_id, status="not_started",
                detail={"reason": "pre_request_callback_failed", "usageKnown": False})
        raise
    partial = logical_partial if logical_partial is not None else AgentResult()
    started = time.monotonic()
    reasoning_started: float | None = None

    def finish_reasoning(now: float) -> None:
        nonlocal reasoning_started
        if reasoning_started is not None:
            outcome.reasoning_ms += max(1, int((now - reasoning_started) * 1000))
            reasoning_started = None

    callback_failure: BaseException | None = None
    cancelled: asyncio.CancelledError | None = None
    events = None
    try:
        if use_stream:
            events = stream(messages, **options)
            async for event in events:
                now = time.monotonic()
                result = outcome.response
                apply_provider_billing(result, event.details)
                if (event.kind in {"output_started", "tool_input"}
                        or event.kind in {"content", "reasoning", "encrypted_reasoning"} and (event.text or event.signature)
                        or event.tool_calls or event.native_output_items):
                    outcome.output_started = True
                if event.kind == "error":
                    outcome.error = OpenBearLLMError.from_stream_event(
                        event, protocol=str(getattr(request.backend, "protocol", "") or ""),
                    )
                    outcome.status = "error"
                    # The error event itself is observable, but callbacks cannot
                    # change its provider classification or retry eligibility.
                elif event.kind == "metrics":
                    if event.connect_ms:
                        outcome.connect_ms = event.connect_ms
                elif event.kind == "content":
                    if event.text:
                        outcome.first_token_ms = outcome.first_token_ms or max(1, int((now-started)*1000))
                        finish_reasoning(now)
                    result.text += event.text
                    partial.text += event.text
                elif event.kind == "reasoning":
                    if event.text:
                        if reasoning_started is None:
                            reasoning_started = now
                        outcome.first_token_ms = outcome.first_token_ms or max(1, int((now-started)*1000))
                    result.reasoning += event.text
                    partial.reasoning += event.text
                    if event.signature:
                        result.signature = event.signature
                elif event.kind == "tool_input":
                    # A tool item starts the parameter phase even before its
                    # first argument byte. Only actual bytes prove first output.
                    finish_reasoning(now)
                    if event.details.get("receivedBytes", 0) > 0:
                        outcome.first_token_ms = outcome.first_token_ms or max(1, int((now-started)*1000))
                elif event.kind == "tool_call":
                    finish_reasoning(now)
                    outcome.first_token_ms = outcome.first_token_ms or max(1, int((now-started)*1000))
                    result.tool_calls = copy.deepcopy(event.tool_calls or [])
                    partial.tool_calls = copy.deepcopy(result.tool_calls)
                elif event.kind == "native_output_item":
                    result.native_output_items.extend(copy.deepcopy(event.native_output_items or []))
                elif event.kind == "usage" and event.usage is not None:
                    outcome.usage_reported = True
                    result.usage.merge(event.usage)
                    if (event.usage.input_tokens + event.usage.cache_read_tokens
                            + event.usage.cache_write_tokens) > 0:
                        outcome.prompt_usage_reported = True
                elif event.kind == "finish":
                    result.finish_reason = event.finish_reason
                    partial.finish_reason = event.finish_reason
                    result.finish_details = copy.deepcopy(event.details.get("stopDetails") or {})
                    partial.finish_details = copy.deepcopy(result.finish_details)
                if on_event is not None:
                    try:
                        await _invoke(on_event, event, partial, outcome)
                    except BaseException as exc:
                        callback_failure = exc
                        raise
                if outcome.error is not None:
                    break
        else:
            result = await request.backend.complete(messages, **options)
            if not isinstance(result, AgentResult):
                raise TypeError("backend.complete must return AgentResult")
            outcome.response = result
            outcome.output_started = bool(result.text or result.reasoning or result.signature
                                          or result.tool_calls or result.native_output_items)
            outcome.usage_reported = bool(getattr(result, "usage_reported", False)) or any(
                (result.usage.input_tokens, result.usage.output_tokens,
                 result.usage.total_tokens, result.usage.cache_read_tokens,
                 result.usage.cache_write_tokens)
            )
            u = result.usage
            outcome.prompt_usage_reported = (u.input_tokens + u.cache_read_tokens + u.cache_write_tokens) > 0
            partial.text += result.text
            partial.reasoning += result.reasoning
            partial.tool_calls = copy.deepcopy(result.tool_calls)
            partial.finish_reason = result.finish_reason
            partial.finish_details = copy.deepcopy(result.finish_details)
        if outcome.error is None and outcome.response.finish_reason == "refusal":
            message = ("上游拒绝生成（refusal），本轮未完成。已保留返回的正文，未自动重试。"
                       if outcome.response.text.strip() else
                       "上游拒绝生成（refusal），未返回正文，本轮未完成。未自动重试。")
            from app.llm.error_sanitize import sanitize_user_facing_text

            details = outcome.response.finish_details
            category = details.get("category")
            explanation = details.get("explanation")
            public = {}
            if isinstance(category, str) and category.strip():
                category = sanitize_user_facing_text(category, max_len=80)
                public["category"] = category
                label = {"cyber": "网络安全"}.get(category, category)
                message += f" 拒绝分类：{label}" + (f"（{category}）。" if label != category else "。")
            if isinstance(explanation, str) and explanation.strip():
                explanation = sanitize_user_facing_text(explanation, max_len=900)
                public["explanation"] = explanation
                message += f" 上游说明：{explanation}"
            outcome.status = "error"
            outcome.error = OpenBearLLMError(
                message, summary=message, reason="refusal", retryable=False,
                protocol=str(getattr(request.backend, "protocol", "") or ""),
                details={"stopDetails": public} if public else None,
            )
    except asyncio.CancelledError as exc:
        outcome.status = "cancelled"
        cancelled = exc
    except OpenBearLLMError as exc:
        if callback_failure is None:
            outcome.status = "error"
            outcome.error = exc
            reported_usage = getattr(exc, "usage", None)
            if isinstance(reported_usage, Usage):
                outcome.response.usage = copy.deepcopy(reported_usage)
                outcome.usage_reported = bool(getattr(exc, "usage_reported", True))
                outcome.prompt_usage_reported = (
                    reported_usage.input_tokens + reported_usage.cache_read_tokens
                    + reported_usage.cache_write_tokens
                ) > 0
            reported_tier = getattr(exc, "service_tier", None)
            if reported_tier:
                outcome.response.service_tier = str(reported_tier)
            reported_cost = getattr(exc, "provider_cost_usd", None)
            if reported_cost is not None:
                outcome.response.provider_cost_usd = reported_cost
        else:
            outcome.status = "error"
            raise
    except Exception as exc:
        if callback_failure is None:
            outcome.status = "error"
            outcome.error = OpenBearLLMError(
                str(exc), protocol=str(getattr(request.backend, "protocol", "") or ""),
            )
        else:
            outcome.status = "error"
            raise
    finally:
        try:
            close = getattr(events, "aclose", None)
            if callable(close):
                await _invoke(close)
        except asyncio.CancelledError as exc:
            outcome.status = "cancelled"
            cancelled = cancelled or exc
        except Exception:
            # Transport cleanup is local, never a request to retry the model.
            outcome.status = "error"
            raise
        finally:
            outcome.total_time_ms = max(0, int((time.monotonic()-started)*1000))
            finish_reasoning(time.monotonic())
            if settle is not None:
                if cancelled is not None:
                    await _cancel_settle(settle, outcome, cancel_settle_timeout_s)
                else:
                    await _invoke(settle, outcome)
    if cancelled is not None:
        raise cancelled
    if budget is not None:
        await budget.before_call()  # No tool execution or retry after budget exhaustion.
    return outcome


_RECOVERY_INSTRUCTION = (
    "The previous model response was interrupted by a transient upstream error. "
    "Continue the same task from the exact interruption point. Do not repeat completed "
    "text or completed work; preserve all prior tool results and instructions."
)


class ModelCallDriver:
    def __init__(self, retry_policy: RetryPolicy):
        self.retry_policy = retry_policy

    async def call(
        self, prepare: Callable[[list[Message]], Any],
        settle: SettleCallback, on_event: EventCallback | None = None,
        on_retry: Callable[[dict[str, Any]], Any] | None = None,
        recover_overflow: Callable[[OpenBearLLMError, list[Message]], Any] | None = None,
        cancel_check: Callable[[], Any] | None = None,
        control_check: Callable[[str], Any] | None = None,
        retry_scope: str = "model_call", task_uuid: str = "",
        accumulate_reasoning: bool = True,
        on_start: StartCallback | None = None,
        after_settle: SettleCallback | None = None,
        recover_thinking: Callable[[AttemptOutcome], Any] | None = None,
    ) -> LogicalResponse:
        """Retry provider failures only, never preparation, controls or accounting.

        ``recover_overflow`` owns its own bounded rotation policy. A successful
        rotation discards stale partial/native state without spending normal retries.
        ``recover_thinking`` persists an active-context derivative and announces
        the downgrade. It runs at most once, only for a pre-output Anthropic
        prefix-binding 400, after settlement; its failures are local, not retries.
        ``settle`` is atomic accounting only; presentation belongs in
        ``after_settle``, outside the writer transaction (including cancellation).
        """
        logical = AgentResult()
        attempts = retries = normal_retries = 0
        mixed = False
        thinking_recovery_used = False
        tail: list[Message] = []
        first_structured: OpenBearLLMError | None = None
        last_retry_state: dict[str, Any] | None = None

        async def settled(outcome):
            nonlocal last_retry_state
            if after_settle is not None:
                await _invoke(after_settle, outcome)
            if last_retry_state is not None and on_retry is not None:
                await _invoke(on_retry, {**last_retry_state, "active": False, "cancelable": False,
                    "retryAtMs": 0, "terminal": True,
                    "status": {"ok": "completed", "error": "failed", "cancelled": "cancelled"}[outcome.status]})
                last_retry_state = None

        while True:
            # Preparation is outside the physical provider failure boundary.
            request = await _invoke(prepare, copy.deepcopy(tail))
            if not isinstance(request, PreparedRequest):
                raise TypeError("prepare must return PreparedRequest")
            outcome = await execute_attempt(
                request, settle=settle, after_settle=settled, on_start=on_start, on_event=on_event,
                logical_partial=logical, mode=request.metadata.get("mode", "auto"),
            )
            attempts += 1
            attempt = outcome.response
            error = outcome.error
            if error is None:
                logical.tool_calls = copy.deepcopy(attempt.tool_calls)
                logical.finish_reason = attempt.finish_reason
                if not mixed:
                    logical.signature = attempt.signature
                    logical.native_output_items = copy.deepcopy(attempt.native_output_items)
                else:
                    logical.signature = ""
                    logical.native_output_items = []
                if not accumulate_reasoning and mixed:
                    logical.reasoning = attempt.reasoning
                return LogicalResponse(logical, attempts, retries, native_replayable=not mixed)
            if is_anthropic_prefix_mismatch(
                error, protocol=str(getattr(outcome.request.backend, "protocol", "") or ""),
            ):
                # A second binding failure is terminal even if a gateway calls
                # it retryable. Neither normal retries nor overflow reset this.
                if not thinking_recovery_used and not outcome.output_started and recover_thinking is not None:
                    thinking_recovery_used = True
                    if await _invoke(recover_thinking, outcome):
                        await wait_for_retry(0, state={"waitId": uuid.uuid4().hex},
                                             cancel_check=cancel_check, control_check=control_check)
                        retries += 1
                        # No output was contributed by this failed request.
                        # Retain any earlier normal-retry tail/mixed state.
                        continue
                raise error
            if first_structured is None and error.structured:
                first_structured = error
            if error.retryable and attempt.tool_calls:
                logical.tool_calls = copy.deepcopy(attempt.tool_calls)
                logical.finish_reason = attempt.finish_reason or "tool_calls"
                if not mixed:
                    logical.signature = attempt.signature
                    logical.native_output_items = copy.deepcopy(attempt.native_output_items)
                else:
                    logical.signature = ""
                    logical.native_output_items = []
                if not accumulate_reasoning and mixed:
                    logical.reasoning = attempt.reasoning
                return LogicalResponse(logical, attempts, retries, recovered_tool_calls=True,
                                       native_replayable=not mixed)
            if error.reason == "context_overflow" and recover_overflow is not None:
                if await _invoke(recover_overflow, error, copy.deepcopy(tail)):
                    logical = AgentResult()
                    tail = []
                    # Rotation discards the failed turn entirely; the next
                    # request may again be a complete native provider turn.
                    mixed = False
                    retries += 1
                    continue
            if not error.retryable or normal_retries >= self.retry_policy.max_retries:
                if first_structured is not None and first_structured is not error and error.reason == "format":
                    raise first_structured from error
                raise error
            normal_retries += 1
            delay = self.retry_policy.delay(normal_retries, retry_after_s=error.retry_after_s)
            state = retry_wait_payload(
                retry_number=normal_retries, max_retries=self.retry_policy.max_retries,
                delay_s=delay, reason=error.reason, error=error.message,
                summary=error.user_message(), transport_status=error.transport_status,
                upstream_status=error.upstream_status or error.status,
                root_cause=error.root_cause, attempts=error.attempts,
                details=error.details, scope=retry_scope, task_uuid=task_uuid,
                quiet=self.retry_policy.quiet(normal_retries, delay),
            )
            await wait_for_retry(delay, state=state, cancel_check=cancel_check,
                                 control_check=control_check, on_update=on_retry)
            last_retry_state = dict(state)
            # Only a retry which actually proceeds to a new physical request counts.
            retries += 1
            mixed = True
            logical.signature = ""
            logical.native_output_items = []
            logical.tool_calls = []
            if not accumulate_reasoning:
                logical.reasoning = ""
            tail = []
            if logical.text.strip():
                tail.append({"role": "assistant", "content": logical.text})
            tail.append({"role": "user", "content": _RECOVERY_INSTRUCTION})
