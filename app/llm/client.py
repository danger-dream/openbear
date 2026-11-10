"""共享 HTTP 客户端 —— httpx 单例 + SSE 解析 + 错误归一。

所有 backend 共用：POST JSON / POST SSE。错误统一抛 OpenBearLLMError。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.llm.base import OpenBearLLMError
from app.llm.error_payloads import normalize_error_payload
from app.logging import get_logger

log = get_logger("llm.client")


async def _iter_sse_lines(response: httpx.Response) -> AsyncIterator[str]:
    """SSE has only CR/LF endings; Unicode separators are JSON string data.

    HTTPX's generic aiter_lines also splits U+0085/U+2028/U+2029. Decode complete
    byte lines instead so both these characters and split UTF-8 survive.
    """
    pending = b""
    skip_lf = False
    first_line = True
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        if skip_lf:
            chunk = chunk.removeprefix(b"\n")
            skip_lf = False
        pending += chunk
        while match := re.search(rb"\r\n|\r|\n", pending):
            raw = pending[:match.start()]
            skip_lf = match.group() == b"\r" and match.end() == len(pending)
            pending = pending[match.end():]
            line = raw.decode("utf-8", errors="replace")
            if first_line:
                line = line.removeprefix("\ufeff")
                first_line = False
            yield line
    if pending:
        yield pending.decode("utf-8", errors="replace")


def _retry_after_seconds(headers: httpx.Headers) -> float:
    raw = str(headers.get("retry-after") or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0.0, (target - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


class HTTPClient:
    """httpx 异步单例封装。"""

    def __init__(self, *, timeout_s: float = 300.0, max_connections: int = 50,
                 connect_timeout_s: float = 10.0, first_byte_timeout_s: float = 30.0,
                 idle_timeout_s: float = 120.0, total_timeout_s: float = 0.0) -> None:
        # 四段超时(对齐 parrot failover):connect 交给 httpx;first_byte/idle/total
        # 在 post_sse 内用 asyncio.wait_for 手动控制。total<=0 表示禁用总时长上限。
        self._default_timeout_s = timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._first_byte_timeout_s = first_byte_timeout_s
        self._idle_timeout_s = idle_timeout_s
        self._total_timeout_s = total_timeout_s
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            limits=httpx.Limits(max_connections=max_connections),
        )

    def configure_timeouts(
        self,
        *,
        connect_timeout_s: float | None = None,
        first_byte_timeout_s: float | None = None,
        idle_timeout_s: float | None = None,
        total_timeout_s: float | None = None,
    ) -> None:
        if connect_timeout_s is not None:
            self._connect_timeout_s = connect_timeout_s
            self._http.timeout = httpx.Timeout(self._default_timeout_s, connect=connect_timeout_s)
        if first_byte_timeout_s is not None:
            self._first_byte_timeout_s = first_byte_timeout_s
        if idle_timeout_s is not None:
            self._idle_timeout_s = idle_timeout_s
        if total_timeout_s is not None:
            self._total_timeout_s = total_timeout_s

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def raw(self) -> httpx.AsyncClient:
        return self._http

    async def post_json(self, url: str, headers: dict[str, str], payload: dict,
                        *, protocol: str = "", read_timeout_s: float | None = None) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {}
        if read_timeout_s is not None and float(read_timeout_s) > 0:
            # 长响应可单独放宽 read；connect/write/pool 仍沿用正常模型配置。
            request_kwargs["timeout"] = httpx.Timeout(
                self._default_timeout_s,
                connect=self._connect_timeout_s,
                read=float(read_timeout_s),
            )
        try:
            resp = await self._http.post(url, headers=headers, json=payload, **request_kwargs)
        except httpx.TimeoutException as e:
            raise OpenBearLLMError(f"请求超时: {e}", retryable=True, protocol=protocol) from e
        except httpx.HTTPError as e:
            raise OpenBearLLMError(f"网络错误: {e}", retryable=True, protocol=protocol) from e
        if resp.status_code >= 400:
            # Parse the complete body first. The normalizer bounds/redacts only the
            # retained payload, so details after byte 500 are not silently lost.
            normalized = normalize_error_payload(resp.text, transport_status=resp.status_code)
            raise OpenBearLLMError(
                normalized.message,
                **normalized.exception_kwargs(protocol=protocol),
                retry_after_s=_retry_after_seconds(resp.headers),
            )
        return resp.json()

    async def post_sse(self, url: str, headers: dict[str, str], payload: dict,
                       *, protocol: str = "", first_byte_timeout_s: float | None = None,
                       total_timeout_s: float | None = None,
                       emit_done: bool = False) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """流式 POST，逐条 yield (event_name, data_json)，带四段独立超时。

        - OpenAI Chat: 无 event 行，event_name="" ；data 为 JSON 或 "[DONE]"
        - Anthropic / Responses: 有 "event:" 行，data 为 JSON

        四段超时(对齐 parrot failover，防止上游卡死把单轮拖死):
          - connect:   建连超时,交给 httpx(self._connect_timeout_s)。
          - first_byte: 从流建立到收到「首个数据块」的固定截止点;期间 SSE 心跳/注释
                        行不重置该截止点 —— 上游只发心跳不出数据照样会超时。
          - idle:       收到首个数据块后,相邻数据块之间的最长空闲(每块刷新)。
          - total:      整条流的总时长上限(<=0 禁用)。
        调用方可仅覆盖 first_byte / total（用于长响应请求）；connect / idle 始终保留正常模型配置。
        传输层瞬时错误始终标为 retryable；是否已有 partial、如何携带 partial 续写，
        由调用者侧 Agent retry state machine 统一处理，避免传输层直接杀掉长任务。
        """
        first_data = False  # 是否已向上层 yield 过真正的 data 块
        req_start = time.monotonic()
        first_byte_to = (
            float(first_byte_timeout_s)
            if first_byte_timeout_s is not None and float(first_byte_timeout_s) > 0
            else self._first_byte_timeout_s
        )
        total_to = (
            float(total_timeout_s)
            if total_timeout_s is not None and float(total_timeout_s) > 0
            else self._total_timeout_s
        )
        total_deadline = (req_start + total_to) if total_to > 0 else None
        # 流式读取的 read 超时交给下面的四段逻辑全权控制(read=None),connect 仍由 httpx 把关。
        stream_timeout = httpx.Timeout(self._default_timeout_s, connect=self._connect_timeout_s, read=None)
        # Manual SSE deadlines only start after headers. Keep the existing normal
        # request timeout for opening/error-body reads, and honor total there too.
        def opening_budget() -> float:
            remaining = self._default_timeout_s
            if total_deadline is not None:
                remaining = min(remaining, total_deadline - time.monotonic())
            return max(0, remaining)
        try:
            async with AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(opening_budget()):
                        resp = await stack.enter_async_context(self._http.stream(
                            "POST", url, headers=headers, json=payload, timeout=stream_timeout))
                except TimeoutError:
                    raise OpenBearLLMError("流式响应头等待超时", retryable=True, protocol=protocol) from None
                if resp.status_code >= 400:
                    try:
                        async with asyncio.timeout(opening_budget()):
                            full_body = (await resp.aread()).decode("utf-8", "replace")
                    except TimeoutError:
                        raise OpenBearLLMError("流式错误响应体读取超时", retryable=True, protocol=protocol) from None
                    normalized = normalize_error_payload(full_body, transport_status=resp.status_code)
                    raise OpenBearLLMError(
                        normalized.message,
                        **normalized.exception_kwargs(protocol=protocol),
                        retry_after_s=_retry_after_seconds(resp.headers),
                    )
                yield "__openbear_metrics__", {"connect_ms": int((time.monotonic() - req_start) * 1000)}
                line_iter = _iter_sse_lines(resp)
                fb_deadline = time.monotonic() + first_byte_to
                data_deadline = fb_deadline
                event_name = ""
                data_lines: list[str] = []
                ended = False
                while not ended:
                    now = time.monotonic()
                    # 选段:未出首数据用 first_byte(固定截止),已出数据用 idle(真实 data 块刷新)。
                    # 注意:SSE 心跳/注释/空行只能证明 TCP 连接还活着,不能证明模型仍在产出;
                    # 它们不应刷新 data_deadline,否则上游一直发 heartbeat 会让单轮永久卡住。
                    seg_remaining = data_deadline - now
                    total_remaining = (total_deadline - now) if total_deadline is not None else None
                    if total_remaining is not None and total_remaining <= 0:
                        raise OpenBearLLMError(
                            f"上游流式总时长超时（{int(total_to)}s）",
                            retryable=True, protocol=protocol) from None
                    if seg_remaining <= 0:
                        if not first_data:
                            raise OpenBearLLMError(
                                f"上游首字节响应超时（{int(first_byte_to)}s 无响应）",
                                retryable=True, protocol=protocol) from None
                        raise OpenBearLLMError(
                            f"上游流式空闲超时（{int(self._idle_timeout_s)}s 无数据）",
                            retryable=True, protocol=protocol) from None
                    wait_s = seg_remaining
                    if total_remaining is not None:
                        wait_s = min(wait_s, total_remaining)
                    try:
                        # Python 3.11 asyncio.wait_for() can swallow an external
                        # Task.cancel() when the awaited SSE read completes in the
                        # same event-loop turn.  Keep timeout ownership in this task
                        # so a Web stop always propagates CancelledError.
                        async with asyncio.timeout(wait_s):
                            line = await line_iter.__anext__()
                    except StopAsyncIteration:
                        # Tolerate relays omitting the last blank separator. Only a
                        # complete JSON event is recovered; the backend still owns
                        # protocol-level completion/EOF validation.
                        ended = True
                        line = ""
                    except TimeoutError:
                        now2 = time.monotonic()
                        if total_deadline is not None and now2 >= total_deadline - 1e-3:
                            raise OpenBearLLMError(
                                f"上游流式总时长超时（{int(total_to)}s）",
                                retryable=True, protocol=protocol) from None
                        if not first_data:
                            raise OpenBearLLMError(
                                f"上游首字节响应超时（{int(first_byte_to)}s 无响应）",
                                retryable=True, protocol=protocol) from None
                        raise OpenBearLLMError(
                            f"上游流式空闲超时（{int(self._idle_timeout_s)}s 无数据）",
                            retryable=True, protocol=protocol) from None
                    if not line:
                        name, event_name = event_name, ""
                        data_str = "\n".join(data_lines)
                        data_lines.clear()
                        if data_str.strip() == "[DONE]":
                            if emit_done:
                                yield "__openbear_done__", {}
                            return
                        if not data_str:
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        first_data = True
                        yield name, data
                        data_deadline = time.monotonic() + self._idle_timeout_s
                        continue
                    if line.startswith(":"):
                        continue  # Heartbeats do not refresh data deadlines.
                    field, _, value = line.partition(":")
                    if value.startswith(" "):
                        value = value[1:]
                    if field == "event":
                        event_name = value
                    elif field == "data":
                        data_lines.append(value)
        except httpx.TimeoutException as e:
            raise OpenBearLLMError(f"流式连接超时: {e}", retryable=True, protocol=protocol) from e
        except httpx.HTTPError as e:
            raise OpenBearLLMError(f"流式网络错误: {e}", retryable=True, protocol=protocol) from e
