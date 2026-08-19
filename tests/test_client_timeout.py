"""HTTPClient.post_sse 四段超时(#12)测试。

不打真实网络:用一个可编排「每行到达延迟」的假 httpx stream 替换 client._http.stream,
精确验证 first_byte / idle / total 三段超时与正常慢流不误杀。
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.llm.base import OpenBearLLMError
from app.llm.client import HTTPClient


class _FakeStreamResponse:
    """模拟 httpx 流式响应:status_code + aiter_lines(按编排延迟逐行吐)。"""

    def __init__(self, lines_with_delay: list[tuple[float, str]], status_code: int = 200):
        self._items = lines_with_delay
        self.status_code = status_code

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        for delay, line in self._items:
            if delay > 0:
                await asyncio.sleep(delay)
            yield line


class _FakeStreamCtx:
    def __init__(self, resp: _FakeStreamResponse):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _patch_stream(client: HTTPClient, resp: _FakeStreamResponse, captured: dict | None = None):
    def _stream(method, url, **kw):
        if captured is not None:
            captured.update(kw)
        return _FakeStreamCtx(resp)
    client._http.stream = _stream  # type: ignore[assignment]


async def _drain(client: HTTPClient):
    out = []
    async for ev_name, data in client.post_sse("http://x", {}, {}, protocol="chat"):
        if ev_name == "__openbear_metrics__":
            continue
        out.append((ev_name, data))
    return out


async def test_sse_read_preserves_external_cancel_when_line_completes_same_turn():
    """A stop must win even when one SSE line becomes readable in the same loop turn."""
    client = HTTPClient(first_byte_timeout_s=60.0, idle_timeout_s=60.0)
    ready = asyncio.Event()

    class RaceLines:
        def __init__(self):
            self.first = True
            self.pending: asyncio.Future[str] | None = None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.first:
                raise StopAsyncIteration
            self.first = False
            self.pending = asyncio.get_running_loop().create_future()
            ready.set()
            return await self.pending

    lines = RaceLines()

    class RaceResponse:
        status_code = 200

        async def aread(self) -> bytes:
            return b""

        def aiter_lines(self):
            return lines

    client._http.stream = lambda *args, **kwargs: _FakeStreamCtx(RaceResponse())  # type: ignore[assignment,arg-type]
    task = asyncio.create_task(_drain(client))
    try:
        await asyncio.wait_for(ready.wait(), timeout=1.0)
        assert lines.pending is not None
        # Ordering is deliberate: Python 3.11 wait_for() used to return the
        # completed inner read and swallow the cancellation in this race.
        lines.pending.set_result('data: {"k":1}')
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await client.close()


async def test_first_byte_timeout_retryable():
    """建流后迟迟没有首个 data 块 → first_byte 超时,且 retryable=True。"""
    client = HTTPClient(first_byte_timeout_s=0.2, idle_timeout_s=5.0)
    # 只发心跳注释行,永远不出 data
    resp = _FakeStreamResponse([(0.1, ": hb"), (0.1, ": hb"), (0.1, ": hb"), (0.5, ": hb")])
    _patch_stream(client, resp)
    with pytest.raises(OpenBearLLMError) as ei:
        await _drain(client)
    assert ei.value.retryable is True
    assert "首字节" in ei.value.message
    await client.close()


async def test_heartbeat_does_not_reset_first_byte_deadline():
    """SSE 心跳/注释行不应刷新 first_byte 截止点(否则上游只发心跳能永久续命)。"""
    client = HTTPClient(first_byte_timeout_s=0.3, idle_timeout_s=5.0)
    # 每 0.1s 一个心跳,0.3s 截止点必然在出 data 前触发
    resp = _FakeStreamResponse([(0.1, ": hb")] * 10 + [(0.0, 'data: {"k":1}')])
    _patch_stream(client, resp)
    with pytest.raises(OpenBearLLMError) as ei:
        await _drain(client)
    assert "首字节" in ei.value.message
    await client.close()


async def test_buffered_heartbeats_after_deadline_cannot_bypass_first_byte_timeout():
    """deadline 已过后即使底层已有大量立即可读心跳行，也必须先判超时。"""
    client = HTTPClient(first_byte_timeout_s=0.03, idle_timeout_s=5.0)
    resp = _FakeStreamResponse([(0.05, ": hb")] + [(0.0, ": hb") for _ in range(50)] + [(0.0, 'data: {"k":1}')])
    _patch_stream(client, resp)
    with pytest.raises(OpenBearLLMError) as ei:
        await _drain(client)
    assert "首字节" in ei.value.message
    await client.close()


async def test_buffered_heartbeats_after_deadline_cannot_bypass_idle_timeout():
    """首个 data 后 deadline 已过，即使心跳行已缓冲也必须空闲超时。"""
    client = HTTPClient(first_byte_timeout_s=1.0, idle_timeout_s=0.03)
    resp = _FakeStreamResponse([
        (0.0, 'data: {"k":1}'),
        (0.05, ": hb"),
        *[(0.0, ": hb") for _ in range(50)],
        (0.0, 'data: {"k":2}'),
    ])
    _patch_stream(client, resp)
    got = []
    with pytest.raises(OpenBearLLMError) as ei:
        async for ev_name, data in client.post_sse("http://x", {}, {}, protocol="chat"):
            if ev_name != "__openbear_metrics__":
                got.append(data)
    assert got == [{"k": 1}]
    assert "空闲" in ei.value.message
    await client.close()


async def test_idle_timeout_after_first_data_is_retryable_by_agent_caller():
    """首个 data 后 idle 超时仍标为可恢复，由 Agent 携带 partial 续写。"""
    client = HTTPClient(first_byte_timeout_s=5.0, idle_timeout_s=0.2)
    resp = _FakeStreamResponse([
        (0.05, 'data: {"k":1}'),   # 首块很快
        (1.0, 'data: {"k":2}'),    # 之后空闲 1s >> idle 0.2s
    ])
    _patch_stream(client, resp)
    got = []
    with pytest.raises(OpenBearLLMError) as ei:
        async for ev_name, data in client.post_sse("http://x", {}, {}, protocol="chat"):
            if ev_name != "__openbear_metrics__":
                got.append(data)
    assert got == [{"k": 1}]          # 首块已成功产出
    assert ei.value.retryable is True
    assert "空闲" in ei.value.message
    await client.close()


async def test_heartbeat_does_not_reset_idle_deadline_after_first_data():
    """首个 data 后只收到 SSE 心跳/注释,也必须按 data 空闲超时,不能被 heartbeat 永久续命。"""
    client = HTTPClient(first_byte_timeout_s=5.0, idle_timeout_s=0.25)
    resp = _FakeStreamResponse([
        (0.01, 'data: {"k":1}'),
        (0.1, ": hb"),
        (0.1, ": hb"),
        (0.1, ": hb"),
        (0.1, 'data: {"k":2}'),
    ])
    _patch_stream(client, resp)
    got = []
    with pytest.raises(OpenBearLLMError) as ei:
        async for ev_name, data in client.post_sse("http://x", {}, {}, protocol="chat"):
            if ev_name != "__openbear_metrics__":
                got.append(data)
    assert got == [{"k": 1}]
    assert ei.value.retryable is True
    assert "空闲" in ei.value.message
    await client.close()


async def test_first_byte_and_total_overrides_leave_idle_unchanged():
    """压缩请求可放宽首字/总时长，但首字后的空闲仍使用正常 idle 配置。"""
    client = HTTPClient(
        connect_timeout_s=0.01,
        first_byte_timeout_s=0.02,
        idle_timeout_s=0.03,
        total_timeout_s=0.04,
    )
    resp = _FakeStreamResponse([
        (0.05, 'data: {"k":1}'),
        (0.05, 'data: {"k":2}'),
    ])
    captured = {}
    _patch_stream(client, resp, captured)
    got = []

    with pytest.raises(OpenBearLLMError) as raised:
        async for ev_name, data in client.post_sse(
            "http://x", {}, {}, protocol="chat",
            first_byte_timeout_s=0.2,
            total_timeout_s=0.3,
        ):
            if ev_name != "__openbear_metrics__":
                got.append(data)

    assert got == [{"k": 1}]
    assert captured["timeout"].connect == 0.01
    assert "空闲超时" in raised.value.message
    assert "首字节" not in raised.value.message
    assert "总时长" not in raised.value.message
    await client.close()


async def test_non_stream_read_override_preserves_connect_write_and_pool():
    """非流式压缩只覆盖 read，不能把 connect/write/pool 一起拉长。"""
    client = HTTPClient(timeout_s=300, connect_timeout_s=10)
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True}

    async def fake_post(_url, **kwargs):
        captured.update(kwargs)
        return Response()

    client._http.post = fake_post  # type: ignore[method-assign]
    assert await client.post_json("http://x", {}, {}, read_timeout_s=1800) == {"ok": True}

    timeout = captured["timeout"]
    assert timeout.connect == 10
    assert timeout.read == 1800
    assert timeout.write == 300
    assert timeout.pool == 300
    await client.close()


async def test_total_timeout():
    """整条流总时长超 total → total 超时。"""
    client = HTTPClient(first_byte_timeout_s=5.0, idle_timeout_s=5.0, total_timeout_s=0.3)
    resp = _FakeStreamResponse([
        (0.05, 'data: {"k":1}'),
        (0.1, 'data: {"k":2}'),
        (0.1, 'data: {"k":3}'),
        (0.5, 'data: {"k":4}'),   # 累计超过 0.3s total
    ])
    _patch_stream(client, resp)
    got = []
    with pytest.raises(OpenBearLLMError) as ei:
        async for ev_name, data in client.post_sse("http://x", {}, {}, protocol="chat"):
            if ev_name != "__openbear_metrics__":
                got.append(data)
    assert "总时长" in ei.value.message
    assert len(got) >= 1
    await client.close()


async def test_normal_slow_stream_not_killed():
    """正常慢流(每块间隔 < idle)应完整读完,不误杀。"""
    client = HTTPClient(first_byte_timeout_s=1.0, idle_timeout_s=0.5, total_timeout_s=0.0)
    resp = _FakeStreamResponse([
        (0.1, 'data: {"k":1}'),
        (0.1, 'data: {"k":2}'),
        (0.1, 'data: {"k":3}'),
        (0.0, "data: [DONE]"),
    ])
    _patch_stream(client, resp)
    got = await _drain(client)
    assert [d for _, d in got] == [{"k": 1}, {"k": 2}, {"k": 3}]
    await client.close()


async def test_total_disabled_allows_long_stream():
    """total=0 禁用总时长上限:只要每块间隔不超 idle,长流也不被砍。"""
    client = HTTPClient(first_byte_timeout_s=1.0, idle_timeout_s=0.5, total_timeout_s=0.0)
    resp = _FakeStreamResponse([(0.2, f'data: {{"k":{i}}}') for i in range(6)] + [(0.0, "data: [DONE]")])
    _patch_stream(client, resp)
    got = await _drain(client)
    assert len(got) == 6
    await client.close()
