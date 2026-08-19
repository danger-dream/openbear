"""OpenAI Chat 协议 backend —— /v1/chat/completions。

出：choices[].delta.content / reasoning_content / tool_calls（分片按 index 拼）
入：role:tool + tool_call_id 回灌
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.llm.base import (
    AgentResult,
    LLMBackend,
    Message,
    apply_fast_request_body,
    apply_provider_billing,
    fast_request_parts,
    merge_fast_request_headers,
    provider_billing_details,
)
from app.llm.client import HTTPClient
from app.llm.error_payloads import error_event, read_error
from app.llm.events import StreamEvent, ToolCall, Usage
from app.llm.multimodal import text_from_content, to_openai_chat_content
from app.logging import get_logger
from app.models.thinking import api_effort, normalize_think_level

log = get_logger("llm.chat")


def _normalize_service_tier(value: Any) -> str:
    tier = str(value or "").strip().lower()
    return tier if tier in {"auto", "priority"} else ""


def _is_gpt_model(model: str) -> bool:
    return str(model or "").strip().lower().startswith("gpt-")


def _to_chat_messages(messages: list[Message], system: str) -> list[dict[str, Any]]:
    """中性消息 → OpenAI Chat messages。"""
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": text_from_content(m.get("content")) or None,
                "tool_calls": [
                    {
                        "id": tc.id or f"call_{i}",
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
                    }
                    for i, tc in enumerate(m["tool_calls"])
                ],
            })
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", ""),
                "content": m.get("content") or "",
            })
        else:
            content = m.get("content")
            out.append({"role": role, "content": to_openai_chat_content(content)})
    return out


def _to_chat_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """中性工具 schema → OpenAI function 格式。

    中性 schema: {"name","description","parameters"}
    """
    if not tools:
        return None
    return [{"type": "function", "function": t} for t in tools]


def _usage_from(u: dict[str, Any]) -> Usage:
    prompt_details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    cached = (
        prompt_details.get("cached_tokens")
        or prompt_details.get("cache_read_tokens")
        or u.get("cache_read_tokens")
        or u.get("cached_tokens")
        or 0
    )
    cache_write = (
        prompt_details.get("cache_creation_tokens")
        or prompt_details.get("cache_write_tokens")
        or u.get("cache_creation_tokens")
        or u.get("cache_write_tokens")
        or 0
    )
    prompt_total = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
    output = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
    # OpenAI-compatible usage totals include both cache reads and cache
    # creation when those detail fields are present. Keep Usage's input/cache
    # axes non-overlapping so context and cost do not count a cache write twice.
    input_uncached = max(0, prompt_total - cached - cache_write)
    return Usage(
        input_tokens=input_uncached,
        output_tokens=output,
        total_tokens=u.get("total_tokens", 0) or (input_uncached + output + cached + cache_write),
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
    )


class OpenAIChatBackend(LLMBackend):
    protocol = "chat"

    def __init__(self, client: HTTPClient, base_url: str, api_key: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._key = api_key

    def _headers(self, session_id: str | None = None, *, fast_headers: Any = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        if session_id:
            h["session-id"] = session_id
        return merge_fast_request_headers(h, fast_headers)

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
        payload: dict[str, Any] = {
            "model": model,
            "messages": _to_chat_messages(messages, system),
            "stream": stream,
            "max_tokens": max_tokens,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        level = normalize_think_level(opts.get("think_level"))
        effort = api_effort(level) if level else None
        if effort:
            payload["reasoning_effort"] = effort
        service_tier = _normalize_service_tier(opts.get("service_tier"))
        if service_tier:
            payload["service_tier"] = service_tier
        chat_tools = _to_chat_tools(tools)
        if chat_tools:
            payload["tools"] = chat_tools
            payload["tool_choice"] = "auto"
        if _is_gpt_model(model):
            # GPT family supports parallel function calling; request it explicitly
            # so providers/proxies do not default to serial tool calls.
            payload["parallel_tool_calls"] = True
        sid = opts.get("session_id")
        if sid:
            payload["user"] = sid
            payload["prompt_cache_key"] = sid
        fast_body, _ = fast_request_parts(opts.get("fast_request"))
        return apply_fast_request_body(payload, fast_body)

    async def stream(
        self, messages: list[Message], *, model: str, system: str = "",
        tools: list[dict[str, Any]] | None = None, max_tokens: int = 8192, **opts: Any,
    ) -> AsyncIterator[StreamEvent]:
        sid = opts.get("session_id")
        _, fast_headers = fast_request_parts(opts.get("fast_request"))
        payload = self.build_payload(
            messages,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            stream=True,
            think_level=opts.get("think_level"),
            service_tier=opts.get("service_tier"),
            session_id=sid,
            fast_request=opts.get("fast_request"),
        )

        pending: dict[int, ToolCall] = {}
        finish_reason = ""
        provider_billing: dict[str, Any] = {}
        url = f"{self._base}/chat/completions"

        async for ev_name, chunk in self._client.post_sse(
            url,
            self._headers(sid, fast_headers=fast_headers),
            payload,
            protocol="chat",
            first_byte_timeout_s=opts.get("first_byte_timeout_s"),
            total_timeout_s=opts.get("total_timeout_s"),
        ):
            if ev_name == "__openbear_metrics__":
                yield StreamEvent(kind="metrics", connect_ms=int(chunk.get("connect_ms") or 0))
                continue
            err = error_event(chunk, event_name=ev_name)
            if err:
                yield err
                return
            u = chunk.get("usage")
            chunk_billing = provider_billing_details(
                chunk.get("service_tier") or (u.get("service_tier") if isinstance(u, dict) else ""),
                u,
            )
            if chunk_billing:
                provider_billing.update(chunk_billing)
            if isinstance(u, dict) and u:
                yield StreamEvent(kind="usage", usage=_usage_from(u), details=dict(provider_billing))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                rc = delta.get("reasoning_content")
                if rc:
                    yield StreamEvent(kind="reasoning", text=rc)
                c = delta.get("content")
                if c:
                    yield StreamEvent(kind="content", text=c)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = pending.setdefault(idx, ToolCall())
                    if tc.get("id"):
                        slot.id = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot.name = fn["name"]
                    if fn.get("arguments"):
                        slot.arguments += fn["arguments"]
                fr = choice.get("finish_reason")
                if fr:
                    finish_reason = fr

        if pending:
            # Parrot may translate an OpenAI Responses upstream tool-call turn into
            # Chat SSE chunks whose deltas contain tool_calls but whose final
            # finish_reason is still "stop".  The Agent loop relies on the
            # normalized finish reason to decide whether to execute tools, so
            # treat the presence of complete pending tool calls as authoritative.
            calls = [pending[i] for i in sorted(pending)]
            yield StreamEvent(kind="tool_call", tool_calls=calls)
            finish_reason = "tool_calls"
        yield StreamEvent(
            kind="finish",
            finish_reason=finish_reason or "stop",
            details=dict(provider_billing),
        )

    async def complete(
        self, messages: list[Message], *, model: str, system: str = "",
        tools: list[dict[str, Any]] | None = None, max_tokens: int = 8192, **opts: Any,
    ) -> AgentResult:
        sid = opts.get("session_id")
        _, fast_headers = fast_request_parts(opts.get("fast_request"))
        payload = self.build_payload(
            messages,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            stream=False,
            think_level=opts.get("think_level"),
            service_tier=opts.get("service_tier"),
            session_id=sid,
            fast_request=opts.get("fast_request"),
        )
        url = f"{self._base}/chat/completions"
        data = await self._client.post_json(
            url,
            self._headers(sid, fast_headers=fast_headers),
            payload,
            protocol="chat",
            read_timeout_s=opts.get("read_timeout_s"),
        )
        parsed_error = read_error(data)
        if parsed_error:
            from app.llm.base import OpenBearLLMError
            error = OpenBearLLMError(
                parsed_error.message,
                **parsed_error.exception_kwargs(protocol="chat"),
            )
            u = data.get("usage") or {}
            error.usage = _usage_from(u)
            apply_provider_billing(
                error,
                provider_billing_details(data.get("service_tier") or u.get("service_tier"), u),
            )
            raise error
        result = AgentResult()
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            result.text = msg.get("content") or ""
            result.reasoning = msg.get("reasoning_content") or ""
            result.finish_reason = choices[0].get("finish_reason") or "stop"
            for i, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                result.tool_calls.append(ToolCall(
                    id=tc.get("id") or f"call_{i}",
                    name=fn.get("name", ""),
                    arguments=fn.get("arguments", "") or "{}",
                ))
            if result.tool_calls:
                result.finish_reason = "tool_calls"
        u = data.get("usage") or {}
        result.usage = _usage_from(u)
        apply_provider_billing(
            result,
            provider_billing_details(data.get("service_tier") or u.get("service_tier"), u),
        )
        return result
