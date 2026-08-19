from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.llm.base import OpenBearLLMError, collect_backend_result
from app.llm.events import StreamEvent, Usage


class _PartialTimeoutBackend:
    protocol = "fake-stream"

    async def stream(self, messages, **kwargs) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(kind="content", text="可用的 partial 摘要")
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=12, output_tokens=4))
        await asyncio.sleep(30)


class _FailedUsageBackend:
    protocol = "fake-stream"

    async def stream(self, messages, **kwargs) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=20, output_tokens=2))
        yield StreamEvent(kind="error", error="upstream failed", status=500, retryable=True)


async def test_collect_backend_result_keeps_partial_on_timeout():
    result, partial, error = await collect_backend_result(
        _PartialTimeoutBackend(),
        [{"role": "user", "content": "summarize"}],
        timeout_s=0.01,
        model="m",
    )
    assert partial is True
    assert error == "summary timeout"
    assert result.text == "可用的 partial 摘要"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 4


async def test_collect_backend_result_attaches_usage_to_stream_error():
    with pytest.raises(OpenBearLLMError) as raised:
        await collect_backend_result(
            _FailedUsageBackend(),
            [{"role": "user", "content": "summarize"}],
            timeout_s=1,
            model="m",
        )
    assert raised.value.usage.input_tokens == 20
    assert raised.value.usage.output_tokens == 2
