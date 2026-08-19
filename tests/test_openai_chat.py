"""OpenAI Chat 协议归一化测试。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.llm.base import OpenBearLLMError, aggregate
from app.llm.events import ToolCall
from app.llm.openai_chat import OpenAIChatBackend, _to_chat_messages, _to_chat_tools
from app.tools.base import ToolRegistry
from app.tools.files import register_file_tools
from tests.conftest import make_client, sse_response


def _chunk(d: dict) -> str:
    return f"data: {json.dumps(d)}"


async def test_chat_stream_content_and_usage():
    lines = [
        _chunk({"choices": [{"index": 0, "delta": {"content": "你"}}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {"content": "好"}}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        "",
        _chunk({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}),
        "",
        "data: [DONE]",
    ]

    def handler(req):
        return sse_response(lines)

    backend = OpenAIChatBackend(make_client(handler), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="deepseek"))
    assert result.text == "你好"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 12


async def test_chat_stream_preserves_actual_service_tier_and_provider_cost():
    lines = [
        _chunk({"choices": [{"index": 0, "delta": {"content": "ok"}}], "service_tier": "default"}),
        "",
        _chunk({
            "choices": [],
            "service_tier": "default",
            "usage": {
                "prompt_tokens": 221,
                "completion_tokens": 19,
                "total_tokens": 240,
                "cost_in_usd_ticks": 3_384_000,
            },
        }),
        "",
        "data: [DONE]",
    ]
    backend = OpenAIChatBackend(make_client(lambda _r: sse_response(lines)), "https://x/v1", "k")

    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="grok-4.5"))

    assert result.service_tier == "default"
    assert result.provider_cost_usd == pytest.approx(0.0003384)


async def test_chat_stream_reasoning():
    lines = [
        _chunk({"choices": [{"index": 0, "delta": {"reasoning_content": "想"}}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {"content": "答"}}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
    ]
    backend = OpenAIChatBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert result.reasoning == "想"
    assert result.text == "答"


async def test_chat_stream_tool_calls_fragmented():
    """工具调用分片按 index 拼装。"""
    lines = [
        _chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "Bash", "arguments": '{"comm'}}]}}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'and": "ls"}'}}]}}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
    ]
    backend = OpenAIChatBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "Bash"
    assert json.loads(tc.arguments) == {"command": "ls"}


async def test_chat_stream_edit_array_arguments():
    payload = {"path": "app/x.py", "edits": [{"old_string": "a", "new_string": "b"}]}
    encoded = json.dumps(payload)
    lines = [
        _chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_edit", "function": {"name": "Edit", "arguments": encoded[:18]}}]}}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": encoded[18:]}}]}}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
    ]
    backend = OpenAIChatBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")

    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))

    assert result.tool_calls[0].name == "Edit"
    assert json.loads(result.tool_calls[0].arguments) == payload


async def test_chat_stream_tool_calls_with_stop_finish_are_normalized():
    """Parrot 可能把 Responses 上游工具调用转成 Chat SSE，但 finish_reason 仍是 stop。"""
    lines = [
        _chunk({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function", "function": {"name": "Read", "arguments": ""}}]},
            "finish_reason": None}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"path":"a.txt"}'}}]},
            "finish_reason": None}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 1, "id": "call_b", "type": "function", "function": {"name": "Grep", "arguments": "{}"}}]},
            "finish_reason": None}]}),
        "",
        _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
    ]
    backend = OpenAIChatBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert result.finish_reason == "tool_calls"
    assert [tc.name for tc in result.tool_calls] == ["Read", "Grep"]
    assert json.loads(result.tool_calls[0].arguments) == {"path": "a.txt"}
    assert result.tool_calls[1].arguments == "{}"


async def test_chat_complete_tool_calls_normalize_finish_reason():
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "call_1", "function": {"name": "Bash", "arguments": '{"command":"pwd"}'}}
            ]}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        })
    backend = OpenAIChatBackend(make_client(handler), "https://x/v1", "k")
    result = await backend.complete([{"role": "user", "content": "hi"}], model="m")
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0].name == "Bash"


async def test_chat_messages_tool_roundtrip():
    """中性消息（含 tool_calls + tool 结果）→ chat 格式正确。"""
    messages = [
        {"role": "user", "content": "跑下 ls"},
        {"role": "assistant", "content": "好", "tool_calls": [
            ToolCall(id="c1", name="Bash", arguments='{"command":"ls"}')]},
        {"role": "tool", "tool_call_id": "c1", "name": "Bash", "content": "a.txt"},
    ]
    out = _to_chat_messages(messages, "你是助手")
    assert out[0] == {"role": "system", "content": "你是助手"}
    assert out[2]["role"] == "assistant"
    assert out[2]["tool_calls"][0]["id"] == "c1"
    assert out[2]["tool_calls"][0]["function"]["name"] == "Bash"
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "a.txt"}


async def test_chat_messages_ignores_cross_protocol_reasoning():
    """跨协议切换安全性：历史里带 reasoning/signature（来自其他协议轮次）
    喂给 chat backend 时必须被安全忽略，不泄漏不兼容字段、不报错。"""
    import json
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "答", "reasoning": "思考过程",
         "signature": "SIG-x"},
    ]
    out = _to_chat_messages(messages, "")
    dumped = json.dumps(out, ensure_ascii=False)
    assert "思考过程" not in dumped
    assert "SIG-x" not in dumped
    assert "signature" not in dumped
    # assistant 文本仍在
    assert any(o.get("role") == "assistant" and o.get("content") == "答" for o in out)


async def test_chat_tools_preserve_separate_edit_contracts():
    registry = ToolRegistry()
    register_file_tools(registry)
    edit_tools = [tool for tool in registry.schemas() if tool["name"] in {"Edit", "EditBatch"}]

    out = _to_chat_tools(edit_tools)

    assert all(item["type"] == "function" for item in out)
    by_name = {item["function"]["name"]: item["function"]["parameters"] for item in out}
    assert set(by_name["Edit"]["properties"]) == {"path", "old_string", "new_string", "replace_all"}
    assert by_name["Edit"]["required"] == ["path", "old_string", "new_string"]
    assert set(by_name["EditBatch"]["properties"]) == {"path", "edits"}
    assert by_name["EditBatch"]["required"] == ["path", "edits"]
    edits = by_name["EditBatch"]["properties"]["edits"]
    assert edits["items"]["properties"]["replace_all"]["type"] == "boolean"
    assert edits["items"]["required"] == ["old_string", "new_string"]


async def test_chat_stream_error_frame_raises_retryable():
    lines = [_chunk({"error": {"message": "rate limit", "type": "rate_limit_exceeded"}})]
    backend = OpenAIChatBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    with pytest.raises(OpenBearLLMError) as ei:
        await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert "rate limit" in ei.value.message
    assert ei.value.retryable


async def test_chat_complete_nonstream():
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "在的"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        })
    backend = OpenAIChatBackend(make_client(handler), "https://x/v1", "k")
    result = await backend.complete([{"role": "user", "content": "hi"}], model="m")
    assert result.text == "在的"
    assert result.usage.total_tokens == 7

async def test_chat_complete_preserves_actual_service_tier_and_provider_cost():
    def handler(_req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "service_tier": "priority",
            "usage": {
                "prompt_tokens": 221,
                "completion_tokens": 23,
                "total_tokens": 244,
                "cost_in_usd_ticks": 7_248_000,
            },
        })

    backend = OpenAIChatBackend(make_client(handler), "https://x/v1", "k")
    result = await backend.complete([{"role": "user", "content": "hi"}], model="grok-4.5")

    assert result.service_tier == "priority"
    assert result.provider_cost_usd == pytest.approx(0.0007248)


async def test_chat_payload_adds_reasoning_effort_from_think_level():
    captured = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        return sse_response([_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})])

    backend = OpenAIChatBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="deepseek", think_level="max"))
    assert captured["reasoning_effort"] == "max"


async def test_chat_payload_preserves_priority_service_tier():
    captured = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        return sse_response([_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})])

    backend = OpenAIChatBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="gpt", service_tier="priority"))
    assert captured["service_tier"] == "priority"

async def test_chat_fast_request_adds_source_body_and_headers_without_overriding_auth_or_model():
    captured = {}
    headers = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        headers.update(dict(req.headers))
        return sse_response([_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})])

    backend = OpenAIChatBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream(
        [{"role": "user", "content": "hi"}],
        model="gpt-5.6-sol",
        fast_request={
            "body": {"service_tier": "priority", "route_hint": "fast", "model": "must-not-replace"},
            "headers": {"x-fast-mode": "enabled", "Authorization": "must-not-replace"},
        },
    ))
    assert captured["service_tier"] == "priority"
    assert captured["route_hint"] == "fast"
    assert captured["model"] == "gpt-5.6-sol"
    assert headers.get("x-fast-mode") == "enabled"
    assert headers.get("authorization") == "Bearer k"


async def test_chat_usage_parses_cache_tokens():
    lines = [
        _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        "",
        _chunk({"choices": [], "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "prompt_tokens_details": {"cached_tokens": 60, "cache_creation_tokens": 7},
        }}),
    ]
    backend = OpenAIChatBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    # prompt_tokens is inclusive: 100 - 60 cache read - 7 cache creation.
    assert result.usage.input_tokens == 33
    assert result.usage.output_tokens == 5
    assert result.usage.total_tokens == 105
    assert result.usage.cache_read_tokens == 60
    assert result.usage.cache_write_tokens == 7


async def test_chat_injects_session_id():
    """session_id → body.user + body.prompt_cache_key + header session-id。"""
    captured = {}
    headers = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        headers.update(dict(req.headers))
        return sse_response([_chunk({"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}]})])

    backend = OpenAIChatBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}],
                                   model="m", session_id="sess-abc"))
    assert captured.get("user") == "sess-abc"
    assert captured.get("prompt_cache_key") == "sess-abc"
    assert headers.get("session-id") == "sess-abc"


async def test_chat_backend_forwards_only_explicit_timeout_overrides(monkeypatch):
    client = make_client(lambda _req: sse_response([]))
    captured = {}

    async def fake_post_json(_url, _headers, _payload, **kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(client, "post_json", fake_post_json)
    backend = OpenAIChatBackend(client, "https://x/v1", "k")
    result = await backend.complete(
        [{"role": "user", "content": "hi"}],
        model="m",
        read_timeout_s=1800,
    )

    assert result.text == "ok"
    assert captured == {"protocol": "chat", "read_timeout_s": 1800}


async def test_chat_stream_forwards_first_byte_and_total_without_connect_or_idle(monkeypatch):
    client = make_client(lambda _req: sse_response([]))
    captured = {}

    async def fake_post_sse(_url, _headers, _payload, **kwargs):
        captured.update(kwargs)
        if False:
            yield None

    monkeypatch.setattr(client, "post_sse", fake_post_sse)
    backend = OpenAIChatBackend(client, "https://x/v1", "k")

    events = [event async for event in backend.stream(
        [{"role": "user", "content": "hi"}],
        model="m",
        first_byte_timeout_s=1800,
        total_timeout_s=1800,
    )]
    assert events[-1].kind == "finish"
    assert captured == {
        "protocol": "chat",
        "first_byte_timeout_s": 1800,
        "total_timeout_s": 1800,
    }


async def test_chat_no_session_id_clean():
    captured = {}
    headers = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        headers.update(dict(req.headers))
        return sse_response([_chunk({"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}]})])

    backend = OpenAIChatBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))
    assert "user" not in captured and "prompt_cache_key" not in captured
    assert "session-id" not in headers


def test_chat_build_payload_sets_parallel_tool_calls_for_gpt_models():
    backend = OpenAIChatBackend(make_client(lambda _req: sse_response([])), "https://x/v1", "k")
    tools = [{"name": "Read", "description": "read", "parameters": {"type": "object", "properties": {}}}]
    gpt = backend.build_payload([{"role": "user", "content": "hi"}], model="gpt-5.4", tools=tools)
    other = backend.build_payload([{"role": "user", "content": "hi"}], model="deepseek-chat", tools=tools)
    assert gpt["parallel_tool_calls"] is True
    assert "parallel_tool_calls" not in other
