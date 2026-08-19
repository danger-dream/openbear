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


async def test_responses_stream_edit_array_arguments():
    payload = {"path": "app/x.py", "edits": [{"old_string": "a", "new_string": "b"}]}
    lines = []
    lines += _ev("response.output_item.done", {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "fc_edit", "name": "Edit", "arguments": json.dumps(payload)}})
    lines += _ev("response.completed", {"type": "response.completed", "response": {"status": "completed", "usage": {}}})
    backend = OpenAIResponsesBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")

    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))

    assert result.tool_calls[0].name == "Edit"
    assert json.loads(result.tool_calls[0].arguments) == payload


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
    by_name = {item["name"]: item["parameters"] for item in out}
    assert set(by_name["Edit"]["properties"]) == {"path", "old_string", "new_string", "replace_all"}
    assert by_name["Edit"]["required"] == ["path", "old_string", "new_string"]
    assert set(by_name["EditBatch"]["properties"]) == {"path", "edits"}
    assert by_name["EditBatch"]["required"] == ["path", "edits"]
    edits = by_name["EditBatch"]["properties"]["edits"]
    assert edits["items"]["properties"]["replace_all"]["type"] == "boolean"
    assert edits["items"]["required"] == ["old_string", "new_string"]


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
