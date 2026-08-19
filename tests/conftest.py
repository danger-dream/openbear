"""测试公共夹具：用 httpx MockTransport 模拟各协议响应，不打真实 Parrot。"""
from __future__ import annotations

import httpx
import pytest

from app.llm.client import HTTPClient


def sse_response(lines: list[str]) -> httpx.Response:
    """构造一个 SSE 流式响应（lines 已是裸行，自动加换行）。"""
    body = "\n".join(lines) + "\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def make_client(handler) -> HTTPClient:
    """用 MockTransport 替换 HTTPClient 内部 httpx。"""
    client = HTTPClient()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False,
                     help="运行打真实 Parrot 的 live 测试")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="需 --run-live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
