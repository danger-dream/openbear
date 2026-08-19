"""Anthropic 协议归一化测试 —— 含 thinking signature 回传(关键坑)。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.llm.anthropic import (
    AnthropicBackend,
    _apply_cache_breakpoints,
    _to_anthropic,
    _to_anthropic_tools,
)
from app.llm.base import OpenBearLLMError, aggregate
from app.llm.events import ToolCall
from app.tools.base import ToolRegistry
from app.tools.files import register_file_tools
from tests.conftest import make_client, sse_response


def _ev(name: str, d: dict) -> list[str]:
    return [f"event: {name}", f"data: {json.dumps(d)}", ""]


async def test_anthropic_stream_content():
    lines = []
    lines += _ev("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 20}}})
    lines += _ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    lines += _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你好"}})
    lines += _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
    lines += _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}})
    lines += _ev("message_stop", {"type": "message_stop"})
    backend = AnthropicBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="claude"))
    assert result.text == "你好"
    assert result.finish_reason == "stop"
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 5


async def test_anthropic_stream_thinking_with_signature():
    """thinking_delta + signature_delta → reasoning + signature 落到 result。"""
    lines = []
    lines += _ev("content_block_start", {"index": 0, "content_block": {"type": "thinking"}})
    lines += _ev("content_block_delta", {"index": 0, "delta": {"type": "thinking_delta", "thinking": "我想想"}})
    lines += _ev("content_block_delta", {"index": 0, "delta": {"type": "signature_delta", "signature": "SIGabc"}})
    lines += _ev("content_block_stop", {"index": 0})
    lines += _ev("content_block_start", {"index": 1, "content_block": {"type": "text"}})
    lines += _ev("content_block_delta", {"index": 1, "delta": {"type": "text_delta", "text": "答案"}})
    lines += _ev("content_block_stop", {"index": 1})
    lines += _ev("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}})
    backend = AnthropicBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="claude"))
    assert result.reasoning == "我想想"
    assert result.signature == "SIGabc"
    assert result.text == "答案"


async def test_anthropic_stream_tool_use():
    lines = []
    lines += _ev("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash"}})
    lines += _ev("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"command":'}})
    lines += _ev("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '"ls"}'}})
    lines += _ev("content_block_stop", {"index": 0})
    lines += _ev("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 8}})
    backend = AnthropicBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="claude"))
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "tu_1" and tc.name == "Bash"
    assert json.loads(tc.arguments) == {"command": "ls"}


async def test_anthropic_stream_edit_array_arguments():
    payload = {"path": "app/x.py", "edits": [{"old_string": "a", "new_string": "b"}]}
    encoded = json.dumps(payload)
    lines = []
    lines += _ev("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": "tu_edit", "name": "Edit"}})
    lines += _ev("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": encoded[:20]}})
    lines += _ev("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": encoded[20:]}})
    lines += _ev("content_block_stop", {"index": 0})
    lines += _ev("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {}})
    backend = AnthropicBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")

    result = await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="claude"))

    assert result.tool_calls[0].name == "Edit"
    assert json.loads(result.tool_calls[0].arguments) == payload


async def test_anthropic_thinking_block_回传_with_signature():
    """中性 assistant 消息含 reasoning+signature → thinking 块带 signature 排最前。"""
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "reasoning": "think", "signature": "SIG",
         "tool_calls": [ToolCall(id="t1", name="Bash", arguments='{"command":"ls"}')]},
        {"role": "tool", "tool_call_id": "t1", "name": "Bash", "content": "out"},
    ]
    out = _to_anthropic(messages)
    asst = out[1]
    assert asst["role"] == "assistant"
    blocks = asst["content"]
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["signature"] == "SIG"
    assert blocks[0]["thinking"] == "think"
    # tool_use 块存在
    assert any(b["type"] == "tool_use" and b["id"] == "t1" for b in blocks)
    # tool 结果 → user + tool_result
    assert out[2]["role"] == "user"
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[2]["content"][0]["tool_use_id"] == "t1"


async def test_anthropic_tools_preserve_separate_edit_contracts():
    registry = ToolRegistry()
    register_file_tools(registry)
    edit_tools = [tool for tool in registry.schemas() if tool["name"] in {"Edit", "EditBatch"}]

    out = _to_anthropic_tools(edit_tools)

    by_name = {item["name"]: item["input_schema"] for item in out}
    assert set(by_name["Edit"]["properties"]) == {"path", "old_string", "new_string", "replace_all"}
    assert by_name["Edit"]["required"] == ["path", "old_string", "new_string"]
    assert set(by_name["EditBatch"]["properties"]) == {"path", "edits"}
    assert by_name["EditBatch"]["required"] == ["path", "edits"]
    edits = by_name["EditBatch"]["properties"]["edits"]
    assert edits["items"]["properties"]["replace_all"]["type"] == "boolean"
    assert edits["items"]["required"] == ["old_string", "new_string"]


async def test_anthropic_stream_error_event_raises():
    lines = _ev("error", {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    backend = AnthropicBackend(make_client(lambda r: sse_response(lines)), "https://x/v1", "k")
    with pytest.raises(OpenBearLLMError) as ei:
        await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="claude"))
    assert "busy" in ei.value.message
    assert ei.value.retryable


async def test_anthropic_complete_nonstream():
    def handler(req):
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "在的"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })
    backend = AnthropicBackend(make_client(handler), "https://x/v1", "k")
    result = await backend.complete([{"role": "user", "content": "hi"}], model="claude")
    assert result.text == "在的"
    assert result.usage.input_tokens == 10

async def test_anthropic_payload_uses_adaptive_effort_for_think_level():
    captured = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        return sse_response(_ev("message_delta", {"delta": {"stop_reason": "end_turn"}}))

    backend = AnthropicBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="claude", think_level="max"))
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"] == {"effort": "max"}


async def test_anthropic_payload_disables_and_drops_history_thinking():
    captured = {}
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "reasoning": "think", "signature": "SIG"},
    ]

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        return sse_response(_ev("message_delta", {"delta": {"stop_reason": "end_turn"}}))

    backend = AnthropicBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream(messages, model="claude", think_level="off"))
    assert captured["thinking"] == {"type": "disabled"}
    assert all(
        not (isinstance(block, dict) and block.get("type") == "thinking")
        for msg in captured["messages"] if isinstance(msg.get("content"), list)
        for block in msg["content"]
    )


async def test_anthropic_fast_mode_uses_beta_header_and_speed():
    captured = {}
    headers = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        headers.update(dict(req.headers))
        return sse_response(_ev("message_delta", {"delta": {"stop_reason": "end_turn"}}))

    backend = AnthropicBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="claude", service_tier="fast"))
    assert captured["speed"] == "fast"
    assert "service_tier" not in captured
    assert headers.get("anthropic-beta") == "fast-mode-2026-02-01"


async def test_anthropic_fast_request_uses_source_body_and_beta_header_without_legacy_hint():
    captured = {}
    headers = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        headers.update(dict(req.headers))
        return sse_response(_ev("message_delta", {"delta": {"stop_reason": "end_turn"}}))

    backend = AnthropicBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream(
        [{"role": "user", "content": "hi"}],
        model="claude-opus",
        fast_request={
            "body": {"speed": "fast"},
            "headers": {"anthropic-beta": "fast-mode-2026-02-01"},
        },
    ))
    assert captured["speed"] == "fast"
    assert headers.get("anthropic-beta") == "fast-mode-2026-02-01"
    assert headers.get("authorization") == "Bearer k"


async def test_anthropic_injects_session_id():
    """session_id → body.metadata.user_id + header X-Claude-Code-Session-Id 同源。"""
    captured = {}
    headers = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        headers.update(dict(req.headers))
        return sse_response(_ev("message_delta", {"delta": {"stop_reason": "end_turn"}}))

    backend = AnthropicBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}],
                                   model="claude", session_id="sess-123"))
    assert "metadata" in captured
    assert "sess-123" in captured["metadata"]["user_id"]
    assert headers.get("x-claude-code-session-id") == "sess-123"


async def test_anthropic_no_session_id_no_metadata():
    """不传 session_id 时不应注入 metadata/header（保持原行为）。"""
    captured = {}
    headers = {}

    def handler(req):
        captured.update(json.loads(req.content.decode()))
        headers.update(dict(req.headers))
        return sse_response(_ev("message_delta", {"delta": {"stop_reason": "end_turn"}}))

    backend = AnthropicBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="claude"))
    assert "metadata" not in captured
    assert "x-claude-code-session-id" not in headers


# ── Prompt Cache 断点注入 ─────────────────────────────────────────

def test_cache_breakpoints_system_str():
    """system 为字符串 → 转成 list[{type:text, cache_control:ephemeral}]。"""
    payload = {"system": "你是助手", "messages": [{"role": "user", "content": "hi"}]}
    _apply_cache_breakpoints(payload)
    assert isinstance(payload["system"], list)
    assert payload["system"][0]["type"] == "text"
    assert payload["system"][0]["text"] == "你是助手"
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_cache_breakpoints_system_list():
    """system 为 list → 末 block 打断点,前面不动。"""
    payload = {
        "system": [{"type": "text", "text": "p1"}, {"type": "text", "text": "p2"}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    _apply_cache_breakpoints(payload)
    assert "cache_control" not in payload["system"][0]
    assert payload["system"][1]["cache_control"] == {"type": "ephemeral"}


def test_cache_breakpoints_tools():
    """tools 末 block 打断点。"""
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}],
    }
    _apply_cache_breakpoints(payload)
    assert "cache_control" not in payload["tools"][0]
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_cache_breakpoints_pin_first_two_user_turns_for_append_only_prefix():
    """messages:最早两个 user turn固定打断点，append不迁移历史标记。"""
    payload = {
        "messages": [
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": [{"type": "text", "text": "r1"}]},
            {"role": "user", "content": [{"type": "text", "text": "第二轮"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "r2"}]},
            {"role": "user", "content": "第三轮"},
        ],
    }
    _apply_cache_breakpoints(payload)
    msgs = payload["messages"]
    # 第一、第二个 user turn固定带断点；后来追加的第三个 user不回写旧unit。
    assert msgs[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert msgs[2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in msgs[4]["content"]
    assert "cache_control" not in msgs[1]["content"][-1]


def test_cache_breakpoints_short_messages_no_crash():
    """消息数 < 4 时不打倒数第二个 user(不越界)。"""
    payload = {"messages": [{"role": "user", "content": "solo"}]}
    _apply_cache_breakpoints(payload)
    assert isinstance(payload["messages"][0]["content"], list)
    assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


async def test_cache_breakpoints_end_to_end_payload():
    """_payload 完整构建 → 验证 cache_control 已注入。"""
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(req.content)
        return sse_response(
            _ev("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 5}}})
            + _ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
            + _ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}})
            + _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})
        )

    backend = AnthropicBackend(make_client(handler), "https://x/v1", "k")
    await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="claude", system="你是助手"))
    p = captured["payload"]
    # system 已转成 list 带断点
    assert isinstance(p["system"], list)
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    # 单个 user既是最早固定断点，也保持端到端cache_control。
    assert p["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
