"""Anthropic 协议 backend —— /v1/messages。

出：content_block_start/delta（text_delta / thinking_delta / input_json_delta）
    + message_delta(usage, stop_reason) + message_start(usage)
入：assistant 的 tool_use 块 + 顶层 system + role:user 里的 tool_result 块

★ thinking 块多轮回传：assistant 的 thinking 块必须带 signature 原样回传，
  否则开启 extended thinking 时 /v1/messages 报错。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.llm.base import (
    AgentResult,
    LLMBackend,
    Message,
    apply_fast_request_body,
    fast_request_parts,
    merge_fast_request_headers,
)
from app.llm.client import HTTPClient
from app.llm.error_payloads import error_event, read_error
from app.llm.events import StreamEvent, ToolCall, Usage
from app.llm.multimodal import text_from_content, to_anthropic_content
from app.logging import get_logger
from app.models.thinking import api_effort, normalize_think_level

log = get_logger("llm.anthropic")

_CACHE_EPHEMERAL = {"type": "ephemeral"}
FAST_MODE_BETA = "fast-mode-2026-02-01"


def _is_fast_mode(value: Any) -> bool:
    return str(value or "").strip().lower() == "fast"


def _norm_stop(stop_reason: str) -> str:
    """Anthropic stop_reason 归一到跨协议统一值。

    tool_use → tool_calls；end_turn/stop_sequence → stop；max_tokens → length。
    """
    return {
        "tool_use": "tool_calls",
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "": "stop",
    }.get(stop_reason, stop_reason)


def _to_anthropic(messages: list[Message], *, include_thinking: bool = True) -> list[dict[str, Any]]:
    """中性消息 → Anthropic messages（system 单独抽出，不在这里）。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue  # system 顶层传，跳过
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            # thinking 块（含 signature）必须排在最前；/think off 时不回放历史 thinking。
            if include_thinking and m.get("reasoning") and m.get("signature"):
                blocks.append({
                    "type": "thinking",
                    "thinking": m["reasoning"],
                    "signature": m["signature"],
                })
            if m.get("content"):
                blocks.append({"type": "text", "text": text_from_content(m.get("content"))})
            for i, tc in enumerate(m.get("tool_calls") or []):
                try:
                    inp = json.loads(tc.arguments) if tc.arguments else {}
                except json.JSONDecodeError:
                    inp = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.id or f"call_{i}",
                    "name": tc.name,
                    "input": inp,
                })
            out.append({"role": "assistant", "content": blocks or ""})
        elif role == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content") or "",
                }],
            })
        else:  # user
            out.append({"role": "user", "content": to_anthropic_content(m.get("content"))})

    # A neutral assistant may emit several tool results in one batch. Anthropic
    # represents every result as role=user, so combine only those already-adjacent
    # provider units. Later real/runtime users are separated by
    # repair_role_alternation's deterministic assistant bridge; an earlier cached
    # user unit is therefore never reopened and rewritten.
    normalized: list[dict[str, Any]] = []
    for message in out:
        if normalized and normalized[-1].get("role") == "user" and message.get("role") == "user":
            previous = dict(normalized[-1])
            previous_content = previous.get("content")
            current_content = message.get("content")
            previous_blocks = (
                list(previous_content)
                if isinstance(previous_content, list)
                else ([{"type": "text", "text": previous_content}] if isinstance(previous_content, str) and previous_content else [])
            )
            current_blocks = (
                list(current_content)
                if isinstance(current_content, list)
                else ([{"type": "text", "text": current_content}] if isinstance(current_content, str) and current_content else [])
            )
            previous["content"] = [*previous_blocks, *current_blocks]
            normalized[-1] = previous
            continue
        normalized.append(message)
    return normalized


def _to_anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """中性 schema {"name","description","parameters"} → Anthropic input_schema。"""
    if not tools:
        return None
    return [
        {"name": t["name"], "description": t.get("description", ""),
         "input_schema": t.get("parameters", {"type": "object", "properties": {}})}
        for t in tools
    ]


# ── Anthropic Prompt Cache 断点注入(沿用 parrot 已验证策略) ───────
# 4 个 ephemeral 断点(Anthropic 上限):system 末 block / tools 末 block /
# 最早两个 user turn。消息断点按不可变的前向位置固定，后续 append 不会把
# cache_control 从历史 unit 移走；经过 parrot 时会被 strip 重打(无害)。

def _inject_cache_on_block(block: dict) -> dict:
    """给单个 content block 打 ephemeral 断点(浅拷贝,不改原对象)。"""
    return {**block, "cache_control": _CACHE_EPHEMERAL}


def _inject_cache_on_msg(msg: dict) -> dict:
    """给一条 message 的 content 末 block 打断点。"""
    msg = dict(msg)
    content = msg.get("content")
    if isinstance(content, list) and content:
        content = list(content)
        content[-1] = _inject_cache_on_block(dict(content[-1]))
        msg["content"] = content
    elif isinstance(content, str):
        msg["content"] = [{"type": "text", "text": content, "cache_control": _CACHE_EPHEMERAL}]
    return msg


def _apply_cache_breakpoints(payload: dict) -> None:
    """原地注入 prompt cache 断点到 payload 的 system/tools/messages。"""
    # system:字符串 → list[{type:text, text:..., cache_control}];list → 末 block 打断点
    system = payload.get("system")
    if isinstance(system, str) and system:
        payload["system"] = [{"type": "text", "text": system, "cache_control": _CACHE_EPHEMERAL}]
    elif isinstance(system, list) and system:
        blocks = [dict(b) if isinstance(b, dict) else b for b in system]
        if isinstance(blocks[-1], dict):
            blocks[-1] = _inject_cache_on_block(blocks[-1])
        payload["system"] = blocks

    # tools:末 block 打断点
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        tools = [dict(t) for t in tools]
        tools[-1] = {**tools[-1], "cache_control": _CACHE_EPHEMERAL}
        payload["tools"] = tools

    # messages:pin the earliest two user turns. A rolling "last message" marker
    # rewrites an already emitted provider unit on every append, defeating literal
    # prompt-prefix stability. Fixed forward positions stay valid indefinitely and,
    # together with system/tools, remain within Anthropic's four-breakpoint limit.
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        messages = list(messages)
        pinned = 0
        for index, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                messages[index] = _inject_cache_on_msg(message)
                pinned += 1
                if pinned == 2:
                    break
        payload["messages"] = messages


class AnthropicBackend(LLMBackend):
    protocol = "anthropic"

    def __init__(self, client: HTTPClient, base_url: str, api_key: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._key = api_key

    def _headers(
        self,
        session_id: str | None = None,
        *,
        fast_mode: bool = False,
        fast_headers: Any = None,
    ) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._key}",
            "x-api-key": self._key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if fast_mode:
            # Legacy/manual Fast fallback. Confirmed models.dev Fast configs are
            # merged below and can replace this safe beta header when published.
            h["anthropic-beta"] = FAST_MODE_BETA
        if session_id:
            # 与 body.metadata.user_id.session_id 同源（对齐 Claude Code / parrot 约定）
            h["X-Claude-Code-Session-Id"] = session_id
        return merge_fast_request_headers(h, fast_headers)

    def _payload(self, messages, system, tools, max_tokens, stream,
                 think_level: str | None = None,
                 session_id: str | None = None,
                 service_tier: str | None = None,
                 fast_request: Any = None) -> dict[str, Any]:
        level = normalize_think_level(think_level) if think_level else None
        payload: dict[str, Any] = {
            "model": None,  # 调用方填
            "max_tokens": max_tokens,
            "messages": _to_anthropic(messages, include_thinking=level != "off"),
            "stream": stream,
        }
        if level == "off":
            payload["thinking"] = {"type": "disabled"}
        elif level:
            payload["thinking"] = {"type": "adaptive"}
            effort = api_effort(level)
            if effort:
                payload["output_config"] = {"effort": effort}
        if _is_fast_mode(service_tier):
            payload["speed"] = "fast"
        if system:
            payload["system"] = system
        atools = _to_anthropic_tools(tools)
        if atools:
            payload["tools"] = atools
        if session_id:
            # 稳定会话 id 内嵌 metadata.user_id，让上游识别同源轮次并保持 prompt cache 亲和
            payload["metadata"] = {"user_id": json.dumps(
                {"session_id": session_id}, separators=(",", ":"))}
        # Prompt Cache:在 system/tools/messages 上注入 ephemeral 断点。
        # 经过 parrot 时被 strip 重打(无害);直连裸 Anthropic 时自己生效(有用)。
        _apply_cache_breakpoints(payload)
        fast_body, _ = fast_request_parts(fast_request)
        return apply_fast_request_body(payload, fast_body)

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
        payload = self._payload(
            messages,
            system,
            tools,
            max_tokens,
            stream,
            opts.get("think_level"),
            opts.get("session_id"),
            opts.get("service_tier"),
            opts.get("fast_request"),
        )
        payload["model"] = model
        return payload

    async def stream(
        self, messages: list[Message], *, model: str, system: str = "",
        tools: list[dict[str, Any]] | None = None, max_tokens: int = 8192, **opts: Any,
    ) -> AsyncIterator[StreamEvent]:
        sid = opts.get("session_id")
        service_tier = opts.get("service_tier")
        fast_request = opts.get("fast_request")
        _, fast_headers = fast_request_parts(fast_request)
        payload = self._payload(messages, system, tools, max_tokens, True,
                                opts.get("think_level"), sid, service_tier, fast_request)
        payload["model"] = model
        url = f"{self._base}/messages"

        # content_block 索引 → 累积状态
        blocks: dict[int, dict[str, Any]] = {}
        in_usage = Usage()
        stop_reason = ""

        async for ev_name, data in self._client.post_sse(
            url,
            self._headers(sid, fast_mode=_is_fast_mode(service_tier), fast_headers=fast_headers),
            payload,
            protocol="anthropic",
            first_byte_timeout_s=opts.get("first_byte_timeout_s"),
            total_timeout_s=opts.get("total_timeout_s"),
        ):
            if ev_name == "__openbear_metrics__":
                yield StreamEvent(kind="metrics", connect_ms=int(data.get("connect_ms") or 0))
                continue
            err = error_event(data, event_name=ev_name)
            if err:
                yield err
                return
            t = data.get("type") or ev_name
            if t == "message_start":
                msg = data.get("message") or {}
                u = msg.get("usage") or {}
                in_usage.input_tokens = u.get("input_tokens", 0)
                in_usage.cache_read_tokens = u.get("cache_read_input_tokens", 0)
                in_usage.cache_write_tokens = u.get("cache_creation_input_tokens", 0)
            elif t == "content_block_start":
                idx = data.get("index", 0)
                cb = data.get("content_block") or {}
                blocks[idx] = {"type": cb.get("type"), "id": cb.get("id", ""),
                               "name": cb.get("name", ""), "args": "", "sig": ""}
            elif t == "content_block_delta":
                idx = data.get("index", 0)
                d = data.get("delta") or {}
                dt = d.get("type")
                if dt == "text_delta":
                    yield StreamEvent(kind="content", text=d.get("text", ""))
                elif dt == "thinking_delta":
                    yield StreamEvent(kind="reasoning", text=d.get("thinking", ""))
                elif dt == "signature_delta":
                    blocks.setdefault(idx, {}).setdefault("sig", "")
                    blocks[idx]["sig"] += d.get("signature", "")
                elif dt == "input_json_delta":
                    blocks.setdefault(idx, {"args": ""})
                    blocks[idx]["args"] = blocks[idx].get("args", "") + d.get("partial_json", "")
            elif t == "content_block_stop":
                idx = data.get("index", 0)
                blk = blocks.get(idx) or {}
                if blk.get("type") == "thinking" and blk.get("sig"):
                    # 把 signature 作为 reasoning 事件补发（聚合层会记下）
                    yield StreamEvent(kind="reasoning", text="", signature=blk["sig"])
            elif t == "message_delta":
                d = data.get("delta") or {}
                if d.get("stop_reason"):
                    stop_reason = d["stop_reason"]
                u = data.get("usage") or {}
                if u.get("output_tokens"):
                    in_usage.output_tokens = u.get("output_tokens", 0)
            elif t == "message_stop":
                pass

        # 收尾：tool_use 块 → tool_call 事件
        calls: list[ToolCall] = []
        for idx in sorted(blocks):
            blk = blocks[idx]
            if blk.get("type") == "tool_use":
                calls.append(ToolCall(id=blk.get("id", ""), name=blk.get("name", ""),
                                      arguments=blk.get("args") or "{}"))
        in_usage.total_tokens = (
            in_usage.input_tokens + in_usage.output_tokens
            + in_usage.cache_read_tokens + in_usage.cache_write_tokens
        )
        yield StreamEvent(kind="usage", usage=in_usage)
        if calls:
            yield StreamEvent(kind="tool_call", tool_calls=calls)
        yield StreamEvent(kind="finish", finish_reason=_norm_stop(stop_reason))

    async def complete(
        self, messages: list[Message], *, model: str, system: str = "",
        tools: list[dict[str, Any]] | None = None, max_tokens: int = 8192, **opts: Any,
    ) -> AgentResult:
        sid = opts.get("session_id")
        service_tier = opts.get("service_tier")
        fast_request = opts.get("fast_request")
        _, fast_headers = fast_request_parts(fast_request)
        payload = self._payload(messages, system, tools, max_tokens, False,
                                opts.get("think_level"), sid, service_tier, fast_request)
        payload["model"] = model
        url = f"{self._base}/messages"
        data = await self._client.post_json(
            url,
            self._headers(sid, fast_mode=_is_fast_mode(service_tier), fast_headers=fast_headers),
            payload,
            protocol="anthropic",
            read_timeout_s=opts.get("read_timeout_s"),
        )
        parsed_error = read_error(data)
        if parsed_error:
            from app.llm.base import OpenBearLLMError
            raise OpenBearLLMError(
                parsed_error.message,
                **parsed_error.exception_kwargs(protocol="anthropic"),
            )
        result = AgentResult()
        for blk in data.get("content") or []:
            bt = blk.get("type")
            if bt == "text":
                result.text += blk.get("text", "")
            elif bt == "thinking":
                result.reasoning += blk.get("thinking", "")
                if blk.get("signature"):
                    result.signature = blk["signature"]
            elif bt == "tool_use":
                result.tool_calls.append(ToolCall(
                    id=blk.get("id", ""), name=blk.get("name", ""),
                    arguments=json.dumps(blk.get("input") or {}, ensure_ascii=False),
                ))
        u = data.get("usage") or {}
        input_tokens = u.get("input_tokens", 0)
        output_tokens = u.get("output_tokens", 0)
        cache_read = u.get("cache_read_input_tokens", 0)
        cache_write = u.get("cache_creation_input_tokens", 0)
        result.usage = Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens + cache_read + cache_write,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        result.finish_reason = _norm_stop(data.get("stop_reason") or "")
        return result
