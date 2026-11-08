"""OpenAI Responses 协议归一化测试。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agent.loop import Agent
from app.llm.base import OpenBearLLMError, aggregate
from app.llm.events import ToolCall
from app.llm.openai_responses import (
    OpenAIResponsesBackend,
    _to_responses_input,
    _to_responses_tools,
)
from app.tools.base import ToolRegistry
from app.tools.files import register_file_tools
from tests.conftest import make_client, sse_response


def _ev(name: str, d: dict) -> list[str]:
    return [f"event: {name}", f"data: {json.dumps(d)}", ""]


async def test_responses_stream_content_and_usage():
    lines = []
    lines += _ev("response.created", {"type": "response.created"})
    lines += _ev("response.output_text.delta", {"type": "response.output_text.delta", "delta": "你"})
    lines += _ev("response.output_text.delta", {"type": "response.output_text.delta", "delta": "好"})
    lines += _ev("response.completed", {"type": "response.completed", "response": {
        "status": "completed", "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}}})
    backend = OpenAIResponsesBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="deepseek"))
    assert result.text == "你好"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 10


async def test_responses_stream_preserves_actual_service_tier_and_provider_cost():
    lines = _ev("response.completed", {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "service_tier": "default",
            "usage": {
                "input_tokens": 221,
                "output_tokens": 19,
                "total_tokens": 240,
                "cost_in_usd_ticks": 3_384_000,
            },
        },
    })
    backend = OpenAIResponsesBackend(
        make_client(lambda _r: sse_response(lines)), "https://x/v1", "k",
    )

    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="grok-4.5"))

    assert result.service_tier == "default"
    assert result.provider_cost_usd == pytest.approx(0.0003384)


async def test_responses_stream_reasoning():
    lines = []
    lines += _ev("response.reasoning_summary_text.delta", {"type": "response.reasoning_summary_text.delta", "delta": "思考"})
    lines += _ev("response.output_text.delta", {"type": "response.output_text.delta", "delta": "答"})
    lines += _ev("response.completed", {"type": "response.completed", "response": {"status": "completed", "usage": {}}})
    backend = OpenAIResponsesBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert result.reasoning == "思考"
    assert result.text == "答"


@pytest.mark.parametrize("native_continuation", [False, True])
@pytest.mark.parametrize("with_ids", [False, True])
async def test_encrypted_reasoning_is_display_only_and_replaces_each_item(native_continuation, with_ids):
    lines = []
    final_items = []
    for index, (initial, final) in enumerate((("opaque-first-start", "opaque-first-final"),
                                            (None, "opaque-second-final"))):
        for phase, encrypted in (("added", initial), ("done", final)):
            item = {"type": "reasoning", "summary": [], "encrypted_content": encrypted}
            if with_ids:
                item["id"] = f"rs_{index}"
            lines += _ev(f"response.output_item.{phase}", {
                "type": f"response.output_item.{phase}", "output_index": index, "item": item,
            })
            if phase == "done":
                final_items.append(item)
    lines += _ev("response.output_text.delta", {"type": "response.output_text.delta", "delta": "答"})
    lines += _ev("response.completed", {"type": "response.completed", "response": {"status": "completed"}})
    backend = OpenAIResponsesBackend(make_client(lambda _r: sse_response(lines)), "https://x/v1", "k")
    options = {"model": "gpt-test", "native_continuation": native_continuation}
    events = [event async for event in backend.stream([{"role": "user", "content": "hi"}], **options)]
    assert [event.text for event in events if event.kind == "encrypted_reasoning"] == [
        "opaque-first-start", "opaque-first-final", "opaque-first-final\n\nopaque-second-final",
    ]
    first_display = next(i for i, event in enumerate(events) if event.kind == "encrypted_reasoning")
    first_text = next(i for i, event in enumerate(events) if event.kind == "content")
    assert first_display < first_text
    assert not any(event.kind == "reasoning" for event in events)
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], **options))
    assert result.text == "答"
    assert result.reasoning == ""
    assert result.native_output_items == (final_items if native_continuation else [])


async def test_responses_stream_function_call():
    lines = []
    lines += _ev("response.output_item.done", {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "fc_1", "name": "Bash", "arguments": '{"command":"ls"}'}})
    lines += _ev("response.completed", {"type": "response.completed", "response": {"status": "completed", "usage": {}}})
    backend = OpenAIResponsesBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "fc_1" and tc.name == "Bash"
    assert json.loads(tc.arguments) == {"command": "ls"}
    assert result.native_output_items == []


async def test_responses_empty_terminal_output_keeps_streamed_function_call():
    # Codex-style relays emit response.completed with "output": [] while the
    # streamed output_item.done frames carry the real items. The empty snapshot
    # must not discard the streamed function_call.
    lines = []
    lines += _ev("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {
        "type": "message", "id": "msg_1", "content": []}})
    lines += _ev("response.content_part.added", {"type": "response.content_part.added", "output_index": 0,
        "item_id": "msg_1", "content_index": 0, "part": {"type": "output_text", "text": ""}})
    lines += _ev("response.output_text.delta", {"type": "response.output_text.delta", "output_index": 0,
        "item_id": "msg_1", "content_index": 0, "delta": "我先查看。"})
    lines += _ev("response.output_text.done", {"type": "response.output_text.done", "output_index": 0,
        "item_id": "msg_1", "content_index": 0, "text": "我先查看。"})
    lines += _ev("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": {
        "type": "message", "id": "msg_1", "content": [{"type": "output_text", "text": "我先查看。"}]}})
    lines += _ev("response.output_item.added", {"type": "response.output_item.added", "output_index": 1, "item": {
        "type": "function_call", "id": "fc_9", "call_id": "call_9", "name": "Bash"}})
    lines += _ev("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
        "output_index": 1, "item_id": "fc_9", "delta": '{"command":"ls"}'})
    lines += _ev("response.output_item.done", {"type": "response.output_item.done", "output_index": 1, "item": {
        "type": "function_call", "id": "fc_9", "call_id": "call_9", "name": "Bash", "arguments": '{"command":"ls"}'}})
    lines += _ev("response.completed", {"type": "response.completed", "response": {
        "status": "completed", "usage": {}, "output": []}})
    backend = OpenAIResponsesBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "call_9" and tc.name == "Bash"
    assert json.loads(tc.arguments) == {"command": "ls"}


async def test_responses_native_continuation_captures_and_replays_output_items():
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "检查代码"}],
        "encrypted_content": "opaque-secret-state",
    }
    call_item = {
        "type": "function_call",
        "id": "fc_item_1",
        "call_id": "fc_1",
        "name": "Bash",
        "arguments": '{"command":"ls"}',
        "status": "completed",
    }
    lines = []
    lines += _ev("response.output_item.done", {"type": "response.output_item.done", "item": reasoning_item})
    lines += _ev("response.output_item.done", {"type": "response.output_item.done", "item": call_item})
    lines += _ev("response.completed", {"type": "response.completed", "response": {"status": "completed", "usage": {}}})
    captured = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        return sse_response(lines)

    backend = OpenAIResponsesBackend(make_client(handler), "https://x/v1", "k")
    result = await aggregate(backend.stream(
        [{"role": "user", "content": "hi"}],
        model="m",
        native_continuation=True,
    ))

    assert captured["store"] is False
    assert captured["include"] == ["reasoning.encrypted_content"]
    assert result.native_output_items == [reasoning_item, call_item]
    assert result.tool_calls[0].id == "fc_1"

    replayed = _to_responses_input([
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "这段可读文本不能重复序列化",
            "tool_calls": [ToolCall(id="fc_1", name="Bash", arguments='{"command":"ls"}')],
            "native_output_items": result.native_output_items,
        },
        {"role": "tool", "tool_call_id": "fc_1", "name": "Bash", "content": "a.txt"},
    ])
    assert replayed[1:3] == [reasoning_item, call_item]
    assert sum(1 for item in replayed if item.get("type") == "function_call") == 1
    assert "这段可读文本不能重复序列化" not in json.dumps(replayed, ensure_ascii=False)
    assert replayed[-1] == {"type": "function_call_output", "call_id": "fc_1", "output": "a.txt"}


@pytest.mark.parametrize("native_continuation", [False, True])
@pytest.mark.parametrize("ordering_source", ["done_index", "added_index", "terminal_output"])
async def test_responses_output_order_is_not_completion_order(native_continuation, ordering_source):
    items = [
        {"type": "reasoning", "id": "rs_order", "summary": [], "encrypted_content": "opaque"},
        {"type": "message", "id": "msg_before", "role": "assistant",
         "content": [{"type": "output_text", "text": "before"}]},
        {"type": "function_call", "id": "fc_first", "call_id": "call_first",
         "name": "First", "arguments": "{}"},
        {"type": "message", "id": "msg_between", "role": "assistant",
         "content": [{"type": "output_text", "text": "between"}]},
        {"type": "function_call", "id": "fc_second", "call_id": "call_second",
         "name": "Second", "arguments": "{}"},
    ]
    lines = []
    if ordering_source == "added_index":
        for index, item in enumerate(items):
            lines += _ev("response.output_item.added", {
                "type": "response.output_item.added", "output_index": index, "item": item,
            })
    for index in [0, 4, 2, 3, 1]:
        event = {"type": "response.output_item.done", "item": items[index]}
        if ordering_source == "done_index":
            event["output_index"] = index
        lines += _ev("response.output_item.done", event)
    response = {"status": "completed"}
    if ordering_source == "terminal_output":
        response["output"] = items
    lines += _ev("response.completed", {"type": "response.completed", "response": response})
    backend = OpenAIResponsesBackend(make_client(lambda _r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream(
        [{"role": "user", "content": "hi"}], model="m", native_continuation=native_continuation,
    ))
    assert [call.id for call in result.tool_calls] == ["call_first", "call_second"]
    assert result.native_output_items == (items if native_continuation else [])
    if native_continuation:
        replay = _to_responses_input([
            {"role": "assistant", "native_output_items": result.native_output_items},
            {"role": "tool", "tool_call_id": "call_first", "content": "first result"},
            {"role": "tool", "tool_call_id": "call_second", "content": "second result"},
        ])
        assert replay[:len(items)] == items
        assert [item["type"] for item in replay[-2:]] == ["function_call_output"] * 2


@pytest.mark.parametrize("terminal", ["completed", "incomplete"])
async def test_responses_terminal_output_completes_native_items_once(terminal):
    item = {"type": "reasoning", "id": "rs_final", "summary": [], "encrypted_content": "final"}
    call = {"type": "function_call", "id": "fc_final", "call_id": "call_final",
            "name": "Bash", "arguments": "{}"}
    lines = _ev("response.output_item.done", {
        "type": "response.output_item.done", "output_index": 0,
        "item": {**item, "encrypted_content": "earlier"},
    })
    # The terminal array is authoritative and includes a call whose done event was omitted.
    lines += _ev(f"response.{terminal}", {
        "type": f"response.{terminal}",
        "response": {"status": terminal, "output": [item, call]},
    })
    backend = OpenAIResponsesBackend(make_client(lambda _r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream(
        [{"role": "user", "content": "hi"}], model="m", native_continuation=True,
    ))
    assert result.native_output_items == [item, call]
    assert [value.id for value in result.tool_calls] == (["call_final"] if terminal == "completed" else [])
    assert result.finish_reason == ("tool_calls" if terminal == "completed" else "incomplete")


async def test_controller_agent_enables_native_responses_request_fields():
    captured = {}
    message_item = {
        "type": "message",
        "id": "controller-message-1",
        "content": [{"type": "output_text", "text": "controller ok"}],
    }
    lines = []
    lines += _ev("response.output_item.done", {
        "type": "response.output_item.done",
        "item": message_item,
    })
    lines += _ev("response.output_text.delta", {
        "type": "response.output_text.delta",
        "delta": "controller ok",
    })
    lines += _ev("response.completed", {
        "type": "response.completed",
        "response": {"status": "completed", "usage": {}},
    })

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        return sse_response(lines)

    class QuietRenderer:
        async def on_status(self, _status):
            return None
        async def on_tool(self, _line):
            return None
        async def on_tool_update(self, _line, **_kwargs):
            return None
        async def on_delta(self, _text, _reasoning=""):
            return None
        async def finalize(self, _text, _reasoning=""):
            return None
        async def finalize_notice(self, _note):
            return None
        async def fail(self, _error):
            return None
        def set_footer(self, _footer):
            return None
        async def cut(self):
            return None

    backend = OpenAIResponsesBackend(make_client(handler), "https://x/v1", "k")
    result = await Agent(backend, ToolRegistry()).run(
        [{"role": "user", "content": "hi"}],
        QuietRenderer(),
        model="gpt",
        session_id="controller-session",
    )

    assert result.text == "controller ok"
    assert captured["store"] is False
    assert captured["include"] == ["reasoning.encrypted_content"]
    assert captured["prompt_cache_key"] == "controller-session"


async def test_responses_input_tool_roundtrip():
    messages = [
        {"role": "user", "content": "跑 ls"},
        {"role": "assistant", "content": "好", "tool_calls": [
            ToolCall(id="c1", name="Bash", arguments='{"command":"ls"}')]},
        {"role": "tool", "tool_call_id": "c1", "name": "Bash", "content": "a.txt"},
    ]
    out = _to_responses_input(messages)
    # user
    assert out[0]["role"] == "user"
    assert out[0]["content"][0]["type"] == "input_text"
    # assistant text
    assert out[1]["role"] == "assistant"
    # function_call item
    fc = next(o for o in out if o.get("type") == "function_call")
    assert fc["call_id"] == "c1" and fc["name"] == "Bash"
    # function_call_output item
    fco = next(o for o in out if o.get("type") == "function_call_output")
    assert fco["call_id"] == "c1" and fco["output"] == "a.txt"


async def test_responses_input_ignores_cross_protocol_reasoning():
    """跨协议切换安全性：历史里带 reasoning/signature（来自 chat 或 anthropic 轮次）
    喂给 responses backend 时必须被安全忽略，不泄漏不兼容字段、不报错。"""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "答", "reasoning": "思考过程",
         "signature": "SIG-from-anthropic"},
    ]
    out = _to_responses_input(messages)
    # assistant 文本保留，但不能出现 reasoning/signature/thinking 残留
    assert any(o.get("role") == "assistant" for o in out)
    dumped = json.dumps(out, ensure_ascii=False)
    assert "思考过程" not in dumped
    assert "SIG-from-anthropic" not in dumped
    assert "signature" not in dumped


async def test_responses_tools_preserve_separate_edit_contracts():
    registry = ToolRegistry()
    register_file_tools(registry)
    edit_tools = [tool for tool in registry.schemas() if tool["name"] in {"Edit", "EditBatch"}]

    out = _to_responses_tools(edit_tools)

    assert all(item["type"] == "function" for item in out)
    assert all(item["strict"] is False for item in out)
    by_name = {item["name"]: item["parameters"] for item in out}
    assert set(by_name["Edit"]["properties"]) == {"path", "old_string", "new_string", "replace_all"}
    assert by_name["Edit"]["required"] == ["path", "old_string", "new_string"]
    assert set(by_name["EditBatch"]["properties"]) == {"path", "edits"}
    assert by_name["EditBatch"]["required"] == ["path", "edits"]
    edits = by_name["EditBatch"]["properties"]["edits"]
    assert edits["items"]["properties"]["replace_all"]["type"] == "boolean"
    assert edits["items"]["required"] == ["old_string", "new_string"]


async def test_responses_mcp_optional_parameters_remain_optional_on_wire():
    tool = {
        "name": "mcp__parrot__web_search",
        "description": "Search with an optional freshness filter.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "freshness": {"type": "string", "enum": ["day", "week", "month", "year"]},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    }
    original = json.dumps(tool, sort_keys=True)
    captured = {}
    lines = _ev("response.output_item.done", {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call", "call_id": "search_1",
            "name": tool["name"], "arguments": '{"query":"RFC 2606"}',
        },
    })
    lines += _ev("response.completed", {
        "type": "response.completed", "response": {"status": "completed", "usage": {}},
    })

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        return sse_response(lines)

    backend = OpenAIResponsesBackend(make_client(handler), "https://x/v1", "k")
    result = await aggregate(backend.stream(
        [{"role": "user", "content": "Search RFC 2606 without a freshness filter."}],
        model="gpt-test", tools=[tool],
    ))

    sent = captured["tools"][0]
    # Responses otherwise auto-normalizes omitted strict into all-fields-required.
    assert sent["strict"] is False
    assert sent["parameters"] == tool["parameters"]
    assert sent["parameters"]["required"] == ["query"]
    assert json.dumps(tool, sort_keys=True) == original
    assert json.loads(result.tool_calls[0].arguments) == {"query": "RFC 2606"}


@pytest.mark.parametrize("strict", [False, True])
def test_responses_tools_preserve_explicit_strict_choice(strict):
    tool = {
        "name": "ExplicitTool", "strict": strict,
        "parameters": {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        },
    }

    converted = _to_responses_tools([tool])[0]

    assert converted["strict"] is strict
    assert converted["parameters"] == tool["parameters"]


async def test_responses_stream_error_event_raises():
    lines = _ev("error", {"type": "error", "message": "server busy", "error_type": "server_error"})
    backend = OpenAIResponsesBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    with pytest.raises(OpenBearLLMError) as ei:
        await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert "server busy" in ei.value.message
    assert ei.value.retryable


async def test_responses_failed_emits_usage_before_error():
    lines = _ev("response.failed", {
        "type": "response.failed",
        "response": {
            "status": "failed",
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            "error": {
                "message": "upstream failed",
                "details": {
                    "summary": "上游请求频率过高，请稍后重试",
                    "root_cause": {
                        "status": 429,
                        "classification": "rate_limit",
                        "code": "rate_limit",
                        "message": "Too many requests",
                        "retryable": True,
                        "retry_scope": "account",
                    },
                    "attempts": [{"status": 429}],
                },
            },
        },
    })
    backend = OpenAIResponsesBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    events = [event async for event in backend.stream([{"role": "user", "content": "hi"}], model="m")]
    usage_index = next(i for i, event in enumerate(events) if event.kind == "usage")
    error_index = next(i for i, event in enumerate(events) if event.kind == "error")
    assert usage_index < error_index
    assert events[usage_index].usage is not None
    assert events[usage_index].usage.total_tokens == 15
    assert events[error_index].error == "rate_limit: Too many requests"
    assert events[error_index].status == events[error_index].upstream_status == 429
    assert events[error_index].reason == "rate_limit"
    assert events[error_index].retryable is True
    assert events[error_index].summary == "上游请求频率过高，请稍后重试"
    assert events[error_index].details["root_cause"]["status"] == 429


async def test_responses_complete_nonstream():
    def handler(req):
        return httpx.Response(200, json={
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "在的"}]}],
            "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        })
    backend = OpenAIResponsesBackend(make_client(handler), "https://x/v1", "k")
    result = await backend.complete(
        [{"role": "user", "content": "hi"}],
        model="m",
        native_continuation=True,
    )
    assert result.text == "在的"
    assert result.usage.total_tokens == 7
    assert result.native_output_items == [
        {"type": "message", "content": [{"type": "output_text", "text": "在的"}]}
    ]


async def test_responses_complete_preserves_actual_service_tier_and_provider_cost():
    def handler(_req):
        return httpx.Response(200, json={
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "service_tier": "priority",
            "usage": {
                "input_tokens": 221,
                "output_tokens": 23,
                "total_tokens": 244,
                "cost_in_usd_ticks": 7_248_000,
            },
        })

    backend = OpenAIResponsesBackend(make_client(handler), "https://x/v1", "k")
    result = await backend.complete([{"role": "user", "content": "hi"}], model="grok-4.5")

    assert result.service_tier == "priority"
    assert result.provider_cost_usd == pytest.approx(0.0007248)


async def test_responses_payload_adds_reasoning_from_think_level():
    captured = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        return sse_response(_ev("response.completed", {"type": "response.completed", "response": {"status": "completed", "usage": {}}}))

    backend = OpenAIResponsesBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="gpt", think_level="xhigh"))
    assert captured["reasoning"] == {"effort": "xhigh"}
    assert "summary" not in captured["reasoning"]


async def test_responses_payload_preserves_priority_service_tier():
    captured = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        return sse_response(_ev("response.completed", {"type": "response.completed", "response": {"status": "completed", "usage": {}}}))

    backend = OpenAIResponsesBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="gpt", service_tier="priority"))
    assert captured["service_tier"] == "priority"

async def test_responses_fast_request_adds_source_body_and_headers():
    captured = {}
    headers = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        headers.update(dict(req.headers))
        return sse_response(_ev("response.completed", {"type": "response.completed", "response": {"status": "completed", "usage": {}}}))

    backend = OpenAIResponsesBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream(
        [{"role": "user", "content": "hi"}],
        model="gpt-5.6-sol",
        fast_request={
            "body": {"service_tier": "priority", "reasoning": {"mode": "fast"}},
            "headers": {"x-fast-mode": "enabled"},
        },
    ))
    assert captured["service_tier"] == "priority"
    assert captured["reasoning"] == {"mode": "fast"}
    assert headers.get("x-fast-mode") == "enabled"


async def test_responses_usage_parses_cache_tokens():
    lines = []
    lines += _ev("response.completed", {"type": "response.completed", "response": {
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 5, "total_tokens": 105,
                  "input_tokens_details": {"cached_tokens": 60, "cache_creation_tokens": 7}},
    }})
    backend = OpenAIResponsesBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    # input_tokens is inclusive: 100 - 60 cache read - 7 cache creation.
    assert result.usage.input_tokens == 33
    assert result.usage.output_tokens == 5
    assert result.usage.total_tokens == 105
    assert result.usage.cache_read_tokens == 60
    assert result.usage.cache_write_tokens == 7


async def test_responses_injects_session_id():
    """session_id → body.prompt_cache_key + header session-id。"""
    captured = {}
    headers = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        headers.update(dict(req.headers))
        return sse_response(_ev("response.completed", {"type": "response.completed",
                                "response": {"status": "completed", "usage": {}}}))

    backend = OpenAIResponsesBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}],
                                   model="m", session_id="sess-r"))
    assert captured.get("prompt_cache_key") == "sess-r"
    assert headers.get("session-id") == "sess-r"


def test_responses_build_payload_sets_parallel_tool_calls_for_gpt_models():
    backend = OpenAIResponsesBackend(make_client(lambda _req: sse_response([])), "https://x/v1", "k")
    tools = [{"name": "Read", "description": "read", "parameters": {"type": "object", "properties": {}}}]
    gpt = backend.build_payload([{"role": "user", "content": "hi"}], model="GPT-5.4", tools=tools)
    other = backend.build_payload([{"role": "user", "content": "hi"}], model="o3", tools=tools)
    assert gpt["parallel_tool_calls"] is True
    assert "parallel_tool_calls" not in other


async def test_responses_incomplete_max_output_tokens_maps_to_length():
    """response.incomplete + incomplete_details.reason=max_output_tokens → finish_reason=length。

    回归：以前 response.incomplete 只被用来取 usage，finish 恒为 "stop"，
    Agent 层于是把「输出被截断」误判成「模型偶发没输出」，触发无效补救重试。
    """
    lines = []
    lines += _ev("response.reasoning_summary_text.delta", {
        "type": "response.reasoning_summary_text.delta", "delta": "想" * 50})
    lines += _ev("response.incomplete", {
        "type": "response.incomplete",
        "response": {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 10, "output_tokens": 31999, "total_tokens": 32009},
        },
    })
    backend = OpenAIResponsesBackend(make_client(lambda _r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="deepseek"))
    assert result.text == ""
    assert result.finish_reason == "length"
    assert result.usage.output_tokens == 31999


async def test_responses_incomplete_other_reason_passes_through():
    """其它 incomplete reason（如 content_filter）按原值传出，不伪装成 stop/length。"""
    lines = _ev("response.incomplete", {
        "type": "response.incomplete",
        "response": {
            "status": "incomplete",
            "incomplete_details": {"reason": "content_filter"},
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    })
    backend = OpenAIResponsesBackend(make_client(lambda _r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert result.finish_reason == "content_filter"
def test_responses_input_omits_role_alternation_bridge_placeholder():
    from app.agent.transcript_repair import ROLE_ALTERNATION_BRIDGE_TEXT, repair_role_alternation
    from app.llm.openai_responses import _to_responses_input

    messages = repair_role_alternation([
        {"role": "assistant", "content": "", "tool_calls": [ToolCall(id="c1", name="WorkItem", arguments="{}")]},
        {"role": "tool", "tool_call_id": "c1", "name": "WorkItem", "content": "{\"ok\":true}"},
        {"role": "user", "content": "<work-items>#1</work-items>"},
        {"role": "user", "content": "<work-item-focus>#1.1</work-item-focus>"},
    ])
    items = _to_responses_input(messages)
    assert all(ROLE_ALTERNATION_BRIDGE_TEXT not in json.dumps(item, ensure_ascii=False) for item in items)
    assert [item.get("type") or item.get("role") for item in items] == [
        "function_call",
        "function_call_output",
        "user",
        "user",
    ]
