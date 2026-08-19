"""记忆工具测试 —— mock prompt-memory HTTP。"""
from __future__ import annotations

import json

import httpx

from app.memory.client import MemoryClient
from app.tools.base import ToolRegistry
from app.tools.memory import register_memory_tools


def _client(handler) -> MemoryClient:
    c = MemoryClient("http://m/api", "openbear", "ak")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


async def test_memory_entry_get():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        captured["identity"] = req.headers.get("X-Memory-Identity")
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True, "entry": {"title": "测试", "body": "内容"}})

    reg = ToolRegistry()
    register_memory_tools(reg, _client(handler))
    out = await reg.dispatch("Memory", json.dumps({"resource": "entry", "action": "get", "name": "测试"}))
    assert "内容" in out
    assert captured["url"].endswith("/tool/entry")
    assert captured["body"]["action"] == "get"
    assert "resource" not in captured["body"]
    assert captured["identity"] == "openbear"
    assert captured["auth"] == "Bearer ak"


async def test_memory_secret_plaintext():
    """secret 正常返回明文（不脱敏）。"""
    def handler(req):
        return httpx.Response(200, json={"ok": True, "secret": {"name": "gh", "fields": [{"key": "token", "value": "ghp_REAL"}]}})

    reg = ToolRegistry()
    register_memory_tools(reg, _client(handler))
    out = await reg.dispatch("Memory", json.dumps({"resource": "secret", "action": "get", "name": "gh"}))
    assert "ghp_REAL" in out


async def test_memory_doc_list():
    def handler(req):
        return httpx.Response(200, json={"ok": True, "items": [{"name": "doc1"}]})

    reg = ToolRegistry()
    register_memory_tools(reg, _client(handler))
    out = await reg.dispatch("Memory", json.dumps({"resource": "doc", "action": "list"}))
    assert "doc1" in out


async def test_memory_identity():
    def handler(req):
        assert str(req.url).endswith("/tool/identities")
        return httpx.Response(200, json={"ok": True, "items": ["openbear", "xiaoxi"]})

    reg = ToolRegistry()
    register_memory_tools(reg, _client(handler))
    out = await reg.dispatch("Memory", json.dumps({"resource": "identity", "action": "list"}))
    assert "openbear" in out


async def test_memory_identity_rejects_non_list_action():
    reg = ToolRegistry()
    register_memory_tools(reg, _client(lambda req: httpx.Response(200, json={})))
    out = await reg.dispatch("Memory", json.dumps({"resource": "identity", "action": "get"}))
    assert "only supports action=list" in out


async def test_memory_old_tools_are_removed():
    reg = ToolRegistry()
    register_memory_tools(reg, _client(lambda req: httpx.Response(200, json={})))
    assert "Memory" in reg.names()
    for name in ("mem_identity", "mem_secret", "mem_entry", "mem_doc"):
        assert name not in reg.names()
        out = await reg.dispatch(name, json.dumps({"action": "list"}))
        assert "未知工具" in out or "unknown tool" in out


async def test_memory_http_error():
    def handler(req):
        return httpx.Response(500, text="boom")

    reg = ToolRegistry()
    register_memory_tools(reg, _client(handler))
    out = await reg.dispatch("Memory", json.dumps({"resource": "entry", "action": "get", "name": "x"}))
    assert "HTTP 500" in out
