"""★ 三协议一致性测试 —— 同一逻辑输入 → 三 backend 产出语义一致的归一化结果。

这是协议层防回归的核心：不管底层是 chat/responses/anthropic，
Agent 看到的 AgentResult（text/reasoning/tool_calls/finish_reason）必须等价。
"""
from __future__ import annotations

import json

from app.llm.anthropic import AnthropicBackend
from app.llm.base import aggregate
from app.llm.openai_chat import OpenAIChatBackend
from app.llm.openai_responses import OpenAIResponsesBackend
from app.task_memory import inject_runtime_block_into_latest_user
from tests.conftest import make_client, sse_response


def _chat_lines(content, reasoning, tool, finish):
    out = []
    if reasoning:
        out += [f'data: {json.dumps({"choices":[{"delta":{"reasoning_content":reasoning}}]})}', ""]
    if content:
        out += [f'data: {json.dumps({"choices":[{"delta":{"content":content}}]})}', ""]
    if tool:
        out += [f'data: {json.dumps({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":tool[0],"arguments":tool[1]}}]}}]})}', ""]
    out += [f'data: {json.dumps({"choices":[{"delta":{},"finish_reason":finish}]})}', ""]
    out += [f'data: {json.dumps({"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}})}', ""]
    return out


def _ev(name, d):
    return [f"event: {name}", f"data: {json.dumps(d)}", ""]


def _anthropic_lines(content, reasoning, tool, stop):
    out = []
    out += _ev("message_start", {"message": {"usage": {"input_tokens": 10}}})
    idx = 0
    if reasoning:
        out += _ev("content_block_start", {"index": idx, "content_block": {"type": "thinking"}})
        out += _ev("content_block_delta", {"index": idx, "delta": {"type": "thinking_delta", "thinking": reasoning}})
        out += _ev("content_block_delta", {"index": idx, "delta": {"type": "signature_delta", "signature": "SIG"}})
        out += _ev("content_block_stop", {"index": idx})
        idx += 1
    if content:
        out += _ev("content_block_start", {"index": idx, "content_block": {"type": "text"}})
        out += _ev("content_block_delta", {"index": idx, "delta": {"type": "text_delta", "text": content}})
        out += _ev("content_block_stop", {"index": idx})
        idx += 1
    if tool:
        out += _ev("content_block_start", {"index": idx, "content_block": {"type": "tool_use", "id": "c1", "name": tool[0]}})
        out += _ev("content_block_delta", {"index": idx, "delta": {"type": "input_json_delta", "partial_json": tool[1]}})
        out += _ev("content_block_stop", {"index": idx})
    out += _ev("message_delta", {"delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}})
    return out


def _responses_lines(content, reasoning, tool):
    out = _ev("response.created", {})
    if reasoning:
        out += _ev("response.reasoning_summary_text.delta", {"delta": reasoning})
    if content:
        out += _ev("response.output_text.delta", {"delta": content})
    if tool:
        out += _ev("response.output_item.done", {"item": {"type": "function_call", "call_id": "c1", "name": tool[0], "arguments": tool[1]}})
    out += _ev("response.completed", {"response": {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}})
    return out


async def _run(backend, lines):
    backend._client = make_client(lambda r: sse_response(lines))
    return await aggregate(backend.stream([{"role": "user", "content": "hi"}], model="m"))


async def test_parity_plain_text():
    chat = OpenAIChatBackend(None, "https://x/v1", "k")
    anth = AnthropicBackend(None, "https://x/v1", "k")
    resp = OpenAIResponsesBackend(None, "https://x/v1", "k")

    r_chat = await _run(chat, _chat_lines("你好", "", None, "stop"))
    r_anth = await _run(anth, _anthropic_lines("你好", "", None, "end_turn"))
    r_resp = await _run(resp, _responses_lines("你好", "", None))

    for r in (r_chat, r_anth, r_resp):
        assert r.text == "你好"
        assert r.finish_reason == "stop"
        assert not r.tool_calls
    assert r_chat.usage.total_tokens == r_anth.usage.total_tokens == r_resp.usage.total_tokens == 15


async def test_parity_with_reasoning():
    chat = OpenAIChatBackend(None, "https://x/v1", "k")
    anth = AnthropicBackend(None, "https://x/v1", "k")
    resp = OpenAIResponsesBackend(None, "https://x/v1", "k")

    r_chat = await _run(chat, _chat_lines("答", "想", None, "stop"))
    r_anth = await _run(anth, _anthropic_lines("答", "想", None, "end_turn"))
    r_resp = await _run(resp, _responses_lines("答", "想", None))

    for r in (r_chat, r_anth, r_resp):
        assert r.text == "答"
        assert r.reasoning == "想"


def test_request_local_xml_user_overlay_reaches_all_three_protocol_payloads():
    xml = '<openbear-memory-checkpoint version="1"><instructions>save state</instructions></openbear-memory-checkpoint>'
    canonical = [{"role": "user", "content": "latest user request"}]
    outbound = inject_runtime_block_into_latest_user(canonical, xml)
    backends = (
        OpenAIChatBackend(None, "https://x/v1", "k"),
        AnthropicBackend(None, "https://x/v1", "k"),
        OpenAIResponsesBackend(None, "https://x/v1", "k"),
    )

    for backend in backends:
        payload = backend.build_payload(outbound, model="m", system="stable system")
        assert xml in str(payload)
        assert xml not in str(payload.get("system") or payload.get("instructions") or "")
    assert canonical == [{"role": "user", "content": "latest user request"}]


async def test_parity_tool_call():
    chat = OpenAIChatBackend(None, "https://x/v1", "k")
    anth = AnthropicBackend(None, "https://x/v1", "k")
    resp = OpenAIResponsesBackend(None, "https://x/v1", "k")

    tool = ("Bash", '{"command":"ls"}')
    r_chat = await _run(chat, _chat_lines("", "", tool, "tool_calls"))
    r_anth = await _run(anth, _anthropic_lines("", "", tool, "tool_use"))
    r_resp = await _run(resp, _responses_lines("", "", tool))

    for r in (r_chat, r_anth, r_resp):
        assert r.finish_reason == "tool_calls"
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].name == "Bash"
        assert r.tool_calls[0].id == "c1"
        assert json.loads(r.tool_calls[0].arguments) == {"command": "ls"}
