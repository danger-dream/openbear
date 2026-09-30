"""OpenAI Responses 协议 backend —— /v1/responses。

实测 SSE 事件名（Parrot 上游）：
  response.created / response.in_progress
  response.output_item.added / .done           ← function_call item 在这里
  response.content_part.added / .done
  response.output_text.delta / .done           ← 正文增量
  response.reasoning_summary_text.delta / .done ← 思考摘要增量
  response.completed                            ← usage 在这里
  response.incomplete                           ← usage + incomplete_details.reason（截断语义）

出：output_text.delta（正文）/ reasoning_summary_text.delta（思考）/ function_call item（工具）
入：input[] 数组；assistant 工具调用是 function_call item，工具结果是 function_call_output item
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.llm._response_text_order import TextOutputOrder
from app.llm.base import (
    AgentResult,
    LLMBackend,
    Message,
    OpenBearLLMError,
    apply_fast_request_body,
    apply_provider_billing,
    fast_request_parts,
    merge_fast_request_headers,
    provider_billing_details,
)
from app.llm.client import HTTPClient
from app.llm.error_payloads import error_event, read_error
from app.llm.events import StreamEvent, ToolCall, Usage
from app.llm.multimodal import to_openai_responses_content
from app.llm.tool_input import ToolInputTracker
from app.logging import get_logger
from app.models.thinking import api_effort, normalize_think_level

log = get_logger("llm.responses")


def _normalize_service_tier(value: Any) -> str:
    tier = str(value or "").strip().lower()
    return tier if tier in {"auto", "priority"} else ""


def _is_gpt_model(model: str) -> bool:
    return str(model or "").strip().lower().startswith("gpt-")


def _to_responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    """中性消息 → Responses input[]。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue  # 用 instructions 顶层传
        if role == "assistant":
            native_items = m.get("native_output_items") or []
            if native_items:
                # Responses output items are already canonical input items. Replaying
                # them verbatim preserves encrypted reasoning and the provider's
                # exact output order; serializing text/tool_calls again would create
                # duplicate assistant turns and break the continuation prefix.
                out.extend(dict(item) for item in native_items if isinstance(item, dict))
                continue
            if m.get("content"):
                out.append({
                    "role": "assistant",
                    "content": to_openai_responses_content(m.get("content"), output=True),
                })
            for i, tc in enumerate(m.get("tool_calls") or []):
                out.append({
                    "type": "function_call",
                    "call_id": tc.id or f"call_{i}",
                    "name": tc.name,
                    "arguments": tc.arguments or "{}",
                })
        elif role == "tool":
            out.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id", ""),
                "output": m.get("content") or "",
            })
        else:  # user
            out.append({
                "role": "user",
                "content": to_openai_responses_content(m.get("content")),
            })
    return out


def _to_responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """中性 schema → Responses flat function tools。

    Responses auto-normalizes an omitted strict flag into all-fields-required.
    Keep ordinary tool schemas non-strict so their optional parameters remain
    optional, while preserving an explicit strict choice from the definition.
    """
    if not tools:
        return None
    return [
        {"type": "function", "name": t["name"], "description": t.get("description", ""),
         "parameters": t.get("parameters", {"type": "object", "properties": {}}),
         "strict": t.get("strict", False)}
        for t in tools
    ]


def _usage_from(u: dict[str, Any]) -> Usage:
    input_details = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
    cached = (
        input_details.get("cached_tokens")
        or input_details.get("cache_read_tokens")
        or u.get("cache_read_tokens")
        or u.get("cached_tokens")
        or 0
    )
    cache_write = (
        input_details.get("cache_creation_tokens")
        or input_details.get("cache_write_tokens")
        or u.get("cache_creation_tokens")
        or u.get("cache_write_tokens")
        or 0
    )
    input_total = u.get("input_tokens", 0) or 0
    output = u.get("output_tokens", 0) or 0
    # OpenAI-compatible usage totals include both cache reads and cache
    # creation when those detail fields are present. Keep Usage's input/cache
    # axes non-overlapping so context and cost do not count a cache write twice.
    input_uncached = max(0, input_total - cached - cache_write)
    return Usage(
        input_tokens=input_uncached,
        output_tokens=output,
        total_tokens=u.get("total_tokens", 0) or (input_uncached + output + cached + cache_write),
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
    )


def _same_output_item(left: dict, right: dict) -> bool:
    if left.get("id") and right.get("id"):
        return left["id"] == right["id"]
    if left.get("type") == right.get("type") == "function_call" and left.get("call_id"):
        return left["call_id"] == right.get("call_id")
    return left == right


def _merge_output_items(streamed: list[dict], terminal: list[dict] | None) -> list[dict]:
    """Prefer final values/order without treating a relay's partial list as deletion.

    Match stable item identity, never a terminal array position against a stream
    output_index. Missing completed items retain their position between shared
    anchors. Thus empty/partial snapshots and sparse indices do not lose/duplicate
    tools, while a complete terminal snapshot remains authoritative.
    """
    result: list[dict] = []
    for item in terminal or []:
        match = next((i for i, old in enumerate(result) if _same_output_item(old, item)), None)
        if match is None:
            result.append(item)
        else:
            result[match] = item
    for pos, item in enumerate(streamed):
        if any(_same_output_item(item, old) for old in result):
            continue
        following = next((i for later in streamed[pos + 1:] for i, old in enumerate(result)
                          if _same_output_item(later, old)), None)
        if following is not None:
            result.insert(following, item)
        else:
            preceding = next((i for earlier in reversed(streamed[:pos]) for i, old in enumerate(result)
                              if _same_output_item(earlier, old)), None)
            result.insert(preceding + 1 if preceding is not None else len(result), item)
    return result


def _response_stop(response: dict, event_type: str = "") -> str:
    if response.get("status") == "incomplete" or event_type == "response.incomplete":
        details = response.get("incomplete_details") or {}
        reason = details.get("reason") if isinstance(details, dict) else None
        return "length" if reason == "max_output_tokens" else str(reason or "incomplete")
    return "stop"


async def _ordered_text_events(events: AsyncIterator[tuple[str, dict[str, Any]]]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    order = TextOutputOrder()
    terminal_seen = False
    async for event_name, data in events:
        # Match the backend's type resolution, including data-only SSE.
        name = data.get("type") or event_name
        if name in {"response.completed", "response.incomplete", "response.failed", "error"}:
            terminal_seen = True
        for ready in order.feed(name, data):
            yield ready
    if not terminal_seen and (order.open or order.deferred):
        # Do not discard queued text and then report a successful response.
        # Its ordering/full suffix is unknown until the predecessor completes.
        raise OpenBearLLMError(
            "Responses 流提前结束：未收到终态，仍有未完成的正文块或等待排序的内容",
            retryable=True, protocol="responses",
        )


class OpenAIResponsesBackend(LLMBackend):
    protocol = "responses"

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
        think_level = opts.get("think_level")
        session_id = opts.get("session_id")
        service_tier = opts.get("service_tier")
        payload: dict[str, Any] = {
            "model": model,
            "input": _to_responses_input(messages),
            "stream": stream,
            "max_output_tokens": max_tokens,
        }
        level = normalize_think_level(think_level) if think_level else None
        effort = api_effort(level) if level else None
        if effort:
            # Keep internal reasoning enabled without asking the provider to emit
            # fragmented, user-visible reasoning-summary deltas.
            payload["reasoning"] = {"effort": effort}
        normalized_service_tier = _normalize_service_tier(service_tier)
        if normalized_service_tier:
            payload["service_tier"] = normalized_service_tier
        if system:
            payload["instructions"] = system
        rtools = _to_responses_tools(tools)
        if rtools:
            payload["tools"] = rtools
            payload["tool_choice"] = "auto"
        if _is_gpt_model(model):
            # GPT family supports parallel function calling; request it explicitly
            # so providers/proxies do not default to serial tool calls.
            payload["parallel_tool_calls"] = True
        if session_id:
            # Responses 用 prompt_cache_key 做缓存亲和（对齐 parrot 约定）
            payload["prompt_cache_key"] = session_id
        if opts.get("native_continuation"):
            # Rath Agent keeps the durable transcript locally. Ask Responses for
            # opaque reasoning state so the next request can replay one canonical,
            # cacheable chain without relying on provider-side retention.
            payload["store"] = False
            payload["include"] = ["reasoning.encrypted_content"]
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
            session_id=sid,
            service_tier=opts.get("service_tier"),
            native_continuation=opts.get("native_continuation"),
            fast_request=opts.get("fast_request"),
        )
        url = f"{self._base}/responses"

        calls: list[ToolCall] = []
        tool_input = ToolInputTracker()
        indexed_items: dict[int, dict[str, Any]] = {}
        unindexed_items: dict[str, dict[str, Any]] = {}
        item_indices: dict[str, int] = {}
        index_ids: dict[int, str] = {}
        emitted_text: dict[tuple[str, int], str] = {}
        text_snapshots: dict[tuple[str, int], tuple[int, str]] = {}
        terminal_output: list[dict[str, Any]] | None = None
        encrypted_reasoning: dict[str, str] = {}
        final_usage: Usage | None = None
        provider_billing: dict[str, Any] = {}
        stop = "stop"
        terminal_seen = False

        def missing_suffix(key: tuple[str, int], index: int | None, full: str) -> str:
            already = emitted_text.get(key, emitted_text.get((f"index:{index}", key[1]), ""))
            if not already and len(text_snapshots) == 1:
                already = emitted_text.get(("index:None", key[1]), "")
            if full.startswith(already):
                emitted_text[key] = full
                return full[len(already):]
            return ""  # Contradictory full snapshots cannot be merged safely.

        async for ev_name, data in _ordered_text_events(self._client.post_sse(
            url,
            self._headers(sid, fast_headers=fast_headers),
            payload,
            protocol="responses",
            first_byte_timeout_s=opts.get("first_byte_timeout_s"),
            total_timeout_s=opts.get("total_timeout_s"),
        )):
            if ev_name == "__openbear_metrics__":
                yield StreamEvent(kind="metrics", connect_ms=int(data.get("connect_ms") or 0))
                continue
            t = data.get("type") or ev_name
            if t in ("response.completed", "response.incomplete", "response.failed"):
                terminal_seen = True
                resp = data.get("response") or {}
                stop = _response_stop(resp, t)
                u = resp.get("usage") or {}
                if isinstance(resp.get("output"), list):
                    terminal_output = [dict(item) for item in resp["output"] if isinstance(item, dict)]
                terminal_billing = provider_billing_details(
                    resp.get("service_tier") or u.get("service_tier"),
                    u,
                )
                if terminal_billing:
                    provider_billing.update(terminal_billing)
                if u:
                    final_usage = _usage_from(u)
                if t == "response.failed" or resp.get("status") == "failed":
                    # Failed Responses may still carry billable usage. Emit it
                    # before the normalized terminal error.
                    if final_usage:
                        yield StreamEvent(
                            kind="usage",
                            usage=final_usage,
                            details=dict(provider_billing),
                        )
                        final_usage = None
                    parsed = read_error(data, event_name="response.failed")
                    if parsed:
                        yield parsed.stream_event()
                    else:
                        yield StreamEvent(kind="error", error="responses failed")
                    return
            err = error_event(data, event_name=ev_name)
            if err:
                yield err
                return
            if t in {"response.output_text.delta", "response.output_text.done", "response.refusal.delta", "response.refusal.done", "response.content_part.done"}:
                part = data.get("part") if t == "response.content_part.done" else None
                if t == "response.content_part.done" and (not isinstance(part, dict) or part.get("type") not in {"output_text", "refusal"}):
                    continue
                oi = data.get("output_index")
                item_id = str(data.get("item_id") or (index_ids.get(oi) if type(oi) is int else "") or "")
                key = (item_id or f"index:{oi}", int(data.get("content_index", 0)))
                text = (data.get("delta") if t.endswith(".delta") else
                        part.get("refusal" if part.get("type") == "refusal" else "text") if part is not None else
                        data.get("refusal" if t == "response.refusal.done" else "text"))
                if isinstance(text, str):
                    if t.endswith(".delta"):
                        emitted_text[key] = emitted_text.get(key, "") + text
                        yield StreamEvent(kind="content", text=text)
                    else:
                        position = oi if type(oi) is int else item_indices.get(item_id)
                        text_snapshots[key] = (position if position is not None else 0, text)
                        if position is not None:
                            tail = missing_suffix(key, position, text)
                            if tail:
                                yield StreamEvent(kind="content", text=tail)
            elif t == "response.reasoning_summary_text.delta":
                yield StreamEvent(kind="reasoning", text=data.get("delta", ""))
            elif t in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
                oi = data.get("output_index")
                key = str(data.get("item_id") or index_ids.get(oi) or f"index:{oi}")
                progress = tool_input.update(
                    key, delta=str(data.get("delta") or ""),
                    arguments=data.get("arguments") if t.endswith(".done") else None,
                    done=t.endswith(".done"),
                )
                if progress is not None:
                    yield progress
            elif t in {"response.output_item.added", "response.output_item.done"}:
                item = data.get("item") or {}
                if isinstance(item, dict) and item:
                    item_id = str(item.get("id") or "")
                    output_index = data.get("output_index")
                    if type(output_index) is int and output_index >= 0:
                        if item_id:
                            item_indices[item_id] = output_index
                            index_ids[output_index] = item_id
                    else:
                        output_index = item_indices.get(item_id)
                    if item.get("type") == "function_call":
                        progress = tool_input.update(
                            item_id or f"index:{output_index}", name=str(item.get("name") or ""),
                            arguments=item.get("arguments") if isinstance(item.get("arguments"), str) else None,
                            done=t == "response.output_item.done",
                        )
                        if progress is not None:
                            yield progress
                    encrypted = item.get("encrypted_content")
                    if item.get("type") == "reasoning" and isinstance(encrypted, str) and encrypted:
                        # Added/done carry complete opaque values, not deltas.
                        # Keep each item's latest value; this display-only event
                        # must not enter readable reasoning or model history.
                        key = str(item.get("id") or data.get("output_index", 0))
                        encrypted_reasoning[key] = encrypted
                        yield StreamEvent(
                            kind="encrypted_reasoning",
                            text="\n\n".join(encrypted_reasoning.values()),
                        )
                    if t != "response.output_item.done":
                        continue
                    if item.get("type") == "message":
                        for content_index, part in enumerate(item.get("content") or []):
                            if not isinstance(part, dict) or part.get("type") not in {"output_text", "refusal"}:
                                continue
                            full = part.get("refusal" if part.get("type") == "refusal" else "text")
                            if isinstance(full, str):
                                key = (item_id or f"index:{output_index}", content_index)
                                text_snapshots[key] = (output_index if output_index is not None else 0, full)
                                if output_index is not None:
                                    tail = missing_suffix(key, output_index, full)
                                    if tail:
                                        yield StreamEvent(kind="content", text=tail)
                    if output_index is not None:
                        indexed_items[output_index] = dict(item)
                        if item_id:
                            unindexed_items.pop(item_id, None)
                    else:
                        # Legacy compatible streams may omit indices and even IDs.
                        # Keep their arrival order only when no canonical position exists.
                        unindexed_items[item_id or f"unindexed:{len(unindexed_items)}"] = dict(item)

        # Item completion order is not output order. Prefer the terminal snapshot;
        # otherwise reconstruct by output_index (including indices seen on added).
        # Emit once so downstream append-only collectors cannot retain a stale order
        # or duplicate an item when the terminal snapshot supplies its final value.
        # Codex-style relays emit response.completed with an empty output list while
        # the streamed output_item.done frames carry the real items (function_call
        # included), so an empty snapshot must fall back to the streamed collection
        # instead of discarding it.
        output_items = _merge_output_items([
            *[indexed_items[index] for index in sorted(indexed_items)],
            *unindexed_items.values(),
        ], terminal_output)
        # Reconcile only missing suffixes. Detailed deltas are already visible;
        # done/terminal snapshots may be the sole full-text carrier on proxies.
        total_text_parts = sum(
            1 for item in output_items if item.get("type") == "message"
            for part in (item.get("content") or [])
            if isinstance(part, dict) and part.get("type") in {"output_text", "refusal"}
        )
        for index, item in enumerate(output_items):
            if item.get("type") != "message":
                continue
            item_id = str(item.get("id") or index_ids.get(index) or f"index:{index}")
            for content_index, part in enumerate(item.get("content") or []):
                if not isinstance(part, dict) or part.get("type") not in {"output_text", "refusal"}:
                    continue
                full = part.get("refusal" if part.get("type") == "refusal" else "text")
                key = (item_id, content_index)
                if isinstance(full, str):
                    text_snapshots[key] = (index, full)
        for key, (_, full) in sorted(text_snapshots.items(), key=lambda entry: (entry[1][0], entry[0][1])):
            already = emitted_text.get(key, emitted_text.get((f"index:{text_snapshots[key][0]}", key[1]), ""))
            if not already and total_text_parts == 1:
                # Legacy streams sometimes omit both item_id and output_index.
                # Only a single output part makes their anonymous delta unambiguous.
                already = emitted_text.get(("index:None", key[1]), "")
            if full.startswith(already) and len(full) > len(already):
                yield StreamEvent(kind="content", text=full[len(already):])
        if opts.get("native_continuation") and output_items:
            yield StreamEvent(kind="native_output_item", native_output_items=output_items)
        for item in output_items:
            if (item.get("type") == "function_call" and stop == "stop"
                    and item.get("status") in (None, "completed")):
                calls.append(ToolCall(
                    id=item.get("call_id") or item.get("id", ""),
                    name=item.get("name", ""),
                    arguments=item.get("arguments", "") or "{}",
                ))

        if final_usage:
            yield StreamEvent(
                kind="usage",
                usage=final_usage,
                details=dict(provider_billing),
            )
        if calls:
            stop = "tool_calls"
            yield StreamEvent(kind="tool_call", tool_calls=calls)
        if not terminal_seen:
            # Completed tool items have already been exposed once for the existing
            # recovery driver. Unfinished calls were never emitted. Record EOF as
            # interruption, not a successful response, without reissuing those tools.
            raise OpenBearLLMError("Responses 流提前结束：未收到响应终态", retryable=True, protocol="responses")
        yield StreamEvent(kind="finish", finish_reason=stop, details=dict(provider_billing))

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
            session_id=sid,
            service_tier=opts.get("service_tier"),
            native_continuation=opts.get("native_continuation"),
            fast_request=opts.get("fast_request"),
        )
        url = f"{self._base}/responses"
        data = await self._client.post_json(
            url,
            self._headers(sid, fast_headers=fast_headers),
            payload,
            protocol="responses",
            read_timeout_s=opts.get("read_timeout_s"),
        )
        parsed_error = read_error(data)
        if parsed_error:
            from app.llm.base import OpenBearLLMError
            error = OpenBearLLMError(
                parsed_error.message,
                **parsed_error.exception_kwargs(protocol="responses"),
            )
            u = data.get("usage") or {}
            error.usage = _usage_from(u)
            apply_provider_billing(
                error,
                provider_billing_details(data.get("service_tier") or u.get("service_tier"), u),
            )
            raise error
        result = AgentResult()
        stop = _response_stop(data)
        output_items = [dict(item) for item in (data.get("output") or []) if isinstance(item, dict)]
        if opts.get("native_continuation"):
            result.native_output_items = output_items
        for item in output_items:
            it = item.get("type")
            if it == "message":
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text"):
                        result.text += part.get("text", "")
                    elif part.get("type") == "refusal":
                        result.text += part.get("refusal", "")
            elif it == "reasoning":
                for part in item.get("summary") or []:
                    if part.get("type") in ("summary_text", "text"):
                        result.reasoning += part.get("text", "")
            elif it == "function_call" and stop == "stop" and item.get("status") in (None, "completed"):
                result.tool_calls.append(ToolCall(
                    id=item.get("call_id") or item.get("id", ""),
                    name=item.get("name", ""),
                    arguments=item.get("arguments", "") or "{}",
                ))
        u = data.get("usage") or {}
        result.usage = _usage_from(u)
        apply_provider_billing(
            result,
            provider_billing_details(data.get("service_tier") or u.get("service_tier"), u),
        )
        result.finish_reason = "tool_calls" if result.tool_calls else stop
        return result
