"""Live 冒烟 —— 打真实 Parrot 上游（默认跳过，需 --run-live）。

证明三协议 backend 对真实上游可用（流式 + 非流式）。
"""
from __future__ import annotations

import os

import pytest

from app.llm.anthropic import AnthropicBackend
from app.llm.base import OpenBearLLMError, aggregate
from app.llm.client import HTTPClient
from app.llm.openai_chat import OpenAIChatBackend
from app.llm.openai_responses import OpenAIResponsesBackend

PARROT = "http://127.0.0.1:22122"
KEY = os.environ.get("OPENBEAR_PARROT_KEY", "")

pytestmark = pytest.mark.live


def _require_live_key() -> str:
    if not KEY:
        pytest.skip("set OPENBEAR_PARROT_KEY to run live Parrot tests")
    return KEY


async def _await_or_skip_unavailable(awaitable):
    """Live 冒烟只验证协议适配；上游别名暂不可用时不把环境容量算作代码失败。"""
    try:
        return await awaitable
    except OpenBearLLMError as e:
        if "No available upstream channels" in e.message:
            pytest.skip(e.message)
        raise


@pytest.fixture
async def client():
    c = HTTPClient()
    yield c
    await c.close()


async def test_live_chat_stream(client):
    key = _require_live_key()
    backend = OpenAIChatBackend(client, f"{PARROT}/v1", key)
    r = await _await_or_skip_unavailable(aggregate(backend.stream(
        [{"role": "user", "content": "只回复两个字:在的"}], model="deepseek", max_tokens=500)))
    assert r.text.strip()
    assert r.usage.total_tokens > 0


async def test_live_anthropic_stream(client):
    key = _require_live_key()
    backend = AnthropicBackend(client, f"{PARROT}/v1", key)
    r = await _await_or_skip_unavailable(aggregate(backend.stream(
        [{"role": "user", "content": "只回复两个字:在的"}], model="claude", max_tokens=50)))
    assert r.text.strip()


async def test_live_responses_stream(client):
    key = _require_live_key()
    backend = OpenAIResponsesBackend(client, f"{PARROT}/v1", key)
    r = await _await_or_skip_unavailable(aggregate(backend.stream(
        [{"role": "user", "content": "只回复两个字:在的"}], model="deepseek", max_tokens=2000)))
    assert r.text.strip()


async def test_live_chat_complete(client):
    key = _require_live_key()
    backend = OpenAIChatBackend(client, f"{PARROT}/v1", key)
    r = await _await_or_skip_unavailable(backend.complete(
        [{"role": "user", "content": "只回复两个字:在的"}], model="deepseek", max_tokens=50))
    assert r.text.strip()


async def test_live_chat_tool_call(client):
    """真实上游能否触发工具调用。"""
    key = _require_live_key()
    backend = OpenAIChatBackend(client, f"{PARROT}/v1", key)
    tools = [{
        "name": "get_time",
        "description": "获取当前时间",
        "parameters": {"type": "object", "properties": {}},
    }]
    r = await _await_or_skip_unavailable(aggregate(backend.stream(
        [{"role": "user", "content": "现在几点？必须调用 get_time 工具获取"}],
        model="deepseek", tools=tools, max_tokens=500)))
    # 工具调用不强制（模型行为有随机性），但若调用了，结构必须正确
    if r.tool_calls:
        assert r.tool_calls[0].name == "get_time"
