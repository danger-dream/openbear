"""LLMBackend 抽象 + 中性消息格式 + 错误归一。

中性消息格式（Agent / 历史库统一用，backend 请求前转成各协议入参）：
  {"role": "system|user|assistant|tool",
   "content": str | list[block],
   "tool_calls": [ToolCall],       # assistant 发起的工具调用
   "tool_call_id": str,            # tool 结果回灌：对应的 call id
   "name": str,                    # tool 结果回灌：工具名
   "reasoning": str,               # assistant thinking（anthropic 多轮回传用）
   "signature": str,               # anthropic thinking 签名（原样回传）
   "native_output_items": [dict]}  # Responses 原生 output items（原样回放）
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.llm.events import StreamEvent, ToolCall, Usage
from app.model_cost import provider_cost_usd_from_ticks


class OpenBearLLMError(Exception):
    """协议无关的统一 LLM 错误。

    reason: 由 error_classify.classify_error 归类的错误类别(rate_limit/billing/auth/...)。
            构造时不传则惰性按 (message,status) 推导。retryable 默认也据此推导,但允许
            调用方显式覆盖(如流式「已产出内容后不可重试」的场景)。
    """

    def __init__(self, message: str, *, status: int = 0, retryable: bool | None = None,
                 protocol: str = "", payload: str = "", reason: str = "",
                 retry_after_s: float = 0.0, transport_status: int = 0,
                 upstream_status: int = 0, summary: str = "",
                 root_cause: dict[str, Any] | None = None,
                 attempts: list[Any] | None = None,
                 details: dict[str, Any] | None = None,
                 structured: bool = False) -> None:
        from app.llm.error_classify import (
            BILLING,
            classify_error,
            normalize_classification,
            retryable_from_contract,
        )
        from app.llm.error_payloads import sanitize_error_detail
        from app.llm.error_sanitize import redact_sensitive_text, sanitize_user_facing_text

        self.message = redact_sensitive_text(str(message or "upstream error"))
        self.transport_status = int(transport_status or 0)
        self.upstream_status = int(upstream_status or 0)
        self.status = int(status or self.upstream_status or self.transport_status or 0)
        self.protocol = protocol
        self.payload = redact_sensitive_text(str(payload or ""))[:4000]
        self.summary = sanitize_user_facing_text(summary) if summary else ""
        safe_root = sanitize_error_detail(root_cause or {})
        safe_attempts = sanitize_error_detail(attempts or [])
        safe_details = sanitize_error_detail(details or {})
        self.root_cause = dict(safe_root) if isinstance(safe_root, dict) else {}
        self.attempts = list(safe_attempts) if isinstance(safe_attempts, list) else []
        self.details = dict(safe_details) if isinstance(safe_details, dict) else {}
        self.structured = bool(structured or details)
        contract_status = self.upstream_status or self.status or self.transport_status
        raw_classification = str(self.root_cause.get("classification") or "")
        structured_reason = normalize_classification(raw_classification, contract_status)
        code_reason = normalize_classification(
            str(self.root_cause.get("code") or ""), contract_status,
        )
        fallback_reason = classify_error(self.summary or self.message, contract_status)
        if BILLING in {structured_reason, code_reason, fallback_reason}:
            self.reason = BILLING
        else:
            self.reason = reason or structured_reason or code_reason or fallback_reason
        self.retry_after_s = max(0.0, float(retry_after_s or 0.0))
        root_retryable = self.root_cause.get("retryable")
        advertised_retryable = root_retryable if isinstance(root_retryable, bool) else retryable
        self.retryable = retryable_from_contract(
            self.reason,
            classification=raw_classification or str(self.root_cause.get("code") or ""),
            status=contract_status,
            retryable=advertised_retryable,
            retry_scope=str(
                self.root_cause.get("retry_scope") or self.root_cause.get("retryScope") or ""
            ),
            structured=self.structured,
        )
        super().__init__(self.message)

    @classmethod
    def from_stream_event(cls, event: StreamEvent, *, protocol: str = "") -> OpenBearLLMError:
        return cls(
            event.error,
            status=event.status,
            retryable=event.retryable,
            protocol=protocol,
            reason=event.reason,
            summary=event.summary,
            transport_status=event.transport_status,
            upstream_status=event.upstream_status,
            root_cause=event.root_cause,
            attempts=event.attempts,
            details=event.details,
            structured=bool(event.details),
        )

    def user_message(self) -> str:
        from app.llm.error_classify import user_message_for
        from app.llm.error_sanitize import sanitize_user_facing_text
        if self.summary:
            return sanitize_user_facing_text(self.summary)
        return user_message_for(self.reason, self.message)

    def public_detail(self) -> dict[str, Any]:
        """Bounded structured fields suitable for retry/task persistence."""
        return {
            "summary": self.user_message(),
            "transportStatus": self.transport_status,
            "upstreamStatus": self.upstream_status or self.status,
            "reason": self.reason,
            "rootCause": dict(self.root_cause),
            "attempts": list(self.attempts),
            "details": dict(self.details),
        }


@dataclass(slots=True)
class AgentResult:
    """一轮 backend 调用的聚合结果（流式聚合 / 非流式同构）。"""
    text: str = ""
    reasoning: str = ""
    signature: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # 完整 provider output items，只在支持原生 continuation 的 backend 中使用。
    # 可读 transcript 仍由 text/reasoning/tool_calls 表示，二者不能互相替代。
    native_output_items: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    # Actual response billing facts for this one physical provider request.
    service_tier: str = ""
    provider_cost_usd: float | None = None


def provider_billing_details(service_tier: Any, usage: Any) -> dict[str, Any]:
    """Normalize provider response tier and authoritative xAI cost ticks."""
    details: dict[str, Any] = {}
    tier = str(service_tier or "").strip().lower()
    if tier:
        details["serviceTier"] = tier
    if isinstance(usage, Mapping):
        cost_usd = provider_cost_usd_from_ticks(usage.get("cost_in_usd_ticks"))
        if cost_usd is not None:
            details["providerCostUsd"] = cost_usd
    return details


def apply_provider_billing(target: Any, details: Any) -> None:
    """Apply normalized event billing facts to an ``AgentResult``-like target."""
    if not isinstance(details, Mapping):
        return
    tier = str(details.get("serviceTier") or "").strip().lower()
    if tier:
        target.service_tier = tier
    if "providerCostUsd" in details:
        target.provider_cost_usd = details.get("providerCostUsd")


# 中性消息类型别名（用 dict 表达，灵活）
Message = dict[str, Any]


# Fast mode is source-confirmed metadata, but it must never replace OpenBear's
# routed model, transcript, stream shape, session affinity, or authentication.
# models.dev Fast records currently use safe keys such as ``service_tier``,
# ``speed`` and ``anthropic-beta``; keeping this guard here makes the adapter
# boundary safe even for a stale frozen task snapshot or malformed config file.
_FAST_BODY_RESERVED_KEYS = frozenset({
    "model", "messages", "input", "stream", "max_tokens", "max_output_tokens",
    "system", "instructions", "tools", "tool_choice", "user", "prompt_cache_key",
    "metadata", "store", "include", "parallel_tool_calls", "stream_options",
})
_FAST_HEADER_RESERVED_KEYS = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "cookie", "host",
    "content-length", "transfer-encoding", "connection", "content-type",
    "session-id", "x-claude-code-session-id", "x-session-affinity",
})
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _deep_overlay(base: Any, overlay: Any) -> Any:
    """Merge nested Fast request objects without mutating runtime-owned values."""
    if not isinstance(base, Mapping) or not isinstance(overlay, Mapping):
        return overlay
    out = dict(base)
    for key, value in overlay.items():
        out[key] = _deep_overlay(out[key], value) if key in out else value
    return out


def apply_fast_request_body(payload: dict[str, Any], extra_body: Any) -> dict[str, Any]:
    """Apply a confirmed Fast-mode body overlay after normal payload construction.

    This mirrors OpenCode's mode-provider options for arbitrary mode keys while
    reserving request identity and transcript fields for OpenBear itself.
    """
    if not isinstance(extra_body, Mapping):
        return payload
    out = dict(payload)
    for raw_key, value in extra_body.items():
        key = str(raw_key or "").strip()
        if not key or key in _FAST_BODY_RESERVED_KEYS:
            continue
        out[key] = _deep_overlay(out[key], value) if key in out else value
    return out


def fast_request_parts(value: Any) -> tuple[Any, Any]:
    """Return Fast body/header parts from a config or frozen task snapshot."""
    if not isinstance(value, Mapping):
        return {}, {}
    return value.get("body"), value.get("headers")


def merge_fast_request_headers(headers: dict[str, str], extra_headers: Any) -> dict[str, str]:
    """Add Fast-mode headers without allowing public metadata to replace auth."""
    if not isinstance(extra_headers, Mapping):
        return dict(headers)
    out = dict(headers)
    for raw_name, raw_value in extra_headers.items():
        name = str(raw_name or "").strip()
        value = raw_value if isinstance(raw_value, str) else ""
        lower = name.lower()
        if (
            not name
            or not value
            or not _HEADER_NAME_RE.fullmatch(name)
            or "\r" in value
            or "\n" in value
            or lower in _FAST_HEADER_RESERVED_KEYS
        ):
            continue
        # HTTP field names are case-insensitive.  Replace an existing safe field
        # (for example ``anthropic-beta``) rather than emitting duplicate values.
        for existing in list(out):
            if existing.lower() == lower:
                out.pop(existing)
        out[name] = value
    return out


async def collect_backend_result(
    backend: Any,
    messages: list[Message],
    *,
    timeout_s: float,
    allow_partial: bool = True,
    **kwargs: Any,
) -> tuple[AgentResult, bool, str]:
    """Collect one bounded model call, preferring streaming when available.

    Returns ``(result, partial, partial_error)``. If a stream times out or emits
    an error after producing useful text, the partial text is returned instead
    of being discarded. Errors before content retain any usage already emitted.
    """
    result = AgentResult()
    stream_call = getattr(backend, "stream", None)
    try:
        async with asyncio.timeout(max(0.1, float(timeout_s))):
            if callable(stream_call):
                async for event in stream_call(messages, **kwargs):
                    apply_provider_billing(result, event.details)
                    if event.kind == "usage" and event.usage:
                        result.usage.merge(event.usage)
                    elif event.kind == "content":
                        result.text += event.text
                    elif event.kind == "reasoning":
                        result.reasoning += event.text
                        if event.signature:
                            result.signature = event.signature
                    elif event.kind == "tool_call":
                        result.tool_calls = list(event.tool_calls or [])
                    elif event.kind == "native_output_item":
                        result.native_output_items.extend(event.native_output_items or [])
                    elif event.kind == "finish":
                        result.finish_reason = event.finish_reason
                    elif event.kind == "error":
                        if allow_partial and (result.text or result.reasoning):
                            return result, True, event.error or "stream error"
                        exc = OpenBearLLMError.from_stream_event(
                            event,
                            protocol=str(getattr(backend, "protocol", "") or ""),
                        )
                        exc.usage = result.usage
                        exc.service_tier = result.service_tier
                        exc.provider_cost_usd = result.provider_cost_usd
                        raise exc
                return result, False, ""
            completed = await backend.complete(messages, **kwargs)
            return completed, False, ""
    except TimeoutError as exc:
        if allow_partial and (result.text or result.reasoning):
            return result, True, "summary timeout"
        exc.usage = result.usage
        exc.service_tier = result.service_tier
        exc.provider_cost_usd = result.provider_cost_usd
        raise


class PayloadInspectableBackend(Protocol):
    """Backend that can render the exact outbound request body without sending it."""

    protocol: str

    def build_payload(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8192,
        stream: bool = True,
        **opts: Any,
    ) -> dict[str, Any]:
        ...


class LLMBackend(Protocol):
    """协议后端抽象。每个协议实现一个。"""

    protocol: str

    def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8192,
        **opts: Any,
    ) -> AsyncIterator[StreamEvent]:
        """流式：产出归一化事件序列。"""
        ...

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8192,
        **opts: Any,
    ) -> AgentResult:
        """非流式：聚合成 AgentResult。"""
        ...


async def aggregate(stream: AsyncIterator[StreamEvent]) -> AgentResult:
    """把归一化事件流聚合成 AgentResult（complete 可复用 stream 实现）。"""
    result = AgentResult()
    async for ev in stream:
        apply_provider_billing(result, ev.details)
        if ev.kind == "content":
            result.text += ev.text
        elif ev.kind == "reasoning":
            result.reasoning += ev.text
            if ev.signature:
                result.signature = ev.signature
        elif ev.kind == "tool_call":
            result.tool_calls = ev.tool_calls
        elif ev.kind == "native_output_item":
            result.native_output_items.extend(ev.native_output_items or [])
        elif ev.kind == "usage" and ev.usage:
            result.usage.merge(ev.usage)
        elif ev.kind == "finish":
            result.finish_reason = ev.finish_reason
        elif ev.kind == "error":
            raise OpenBearLLMError.from_stream_event(ev)
    return result
