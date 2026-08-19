from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from app.config import Config, MCPServerConfig
from app.mcp.client import MCPClient
from app.mcp.manager import MCPManager
from app.mcp.output import govern_text_output, redact_secrets, redact_text_secrets
from app.mcp.permissions import (
    build_tool_meta,
    can_call_without_prompt,
    classify_risk,
    make_public_tool_name,
)
from app.mcp.transports import StreamableHTTPTransport, _decode_http_rpc_response
from app.mcp.types import MCPRawResult, MCPRawTool, MCPServerState, MCPToolMeta
from app.services import Services
from app.tools.base import ToolRuntimeContext


def _cfg() -> Config:
    return Config.model_validate(_cfg_data())


def _cfg_data() -> dict:
    return {
        "telegram": {"botToken": "t", "whitelistIds": [123]},
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://x",
                    "apiKey": "k",
                    "protocol": "chat",
                    "models": [{"id": "m", "name": "M"}],
                }
            },
            "primary": "openai/m",
        },
        "memory": {"provider": "builtin"},
        "mcp": {
            "enabled": True,
            "allowTools": ["*"],
            "denyTools": ["*:delete*"],
            "servers": {
                "serena": {
                    "enabled": True,
                    "transport": "stdio",
                    "command": "serena",
                    "approval": "ask",
                    "tools": {"allow": ["find_*", "read*", "delete*"], "deny": ["write*"]},
                }
            },
        },
    }


def test_mcp_status_snapshot_uses_config_order_not_async_state_insertion_order():
    data = _cfg_data()
    base = data["mcp"]["servers"]["serena"]
    data["mcp"]["servers"] = {
        "first": {**base, "enabled": True},
        "disabled": {**base, "enabled": False},
        "last": {**base, "enabled": True},
    }
    manager = MCPManager(Config.model_validate(data))
    manager._states = {
        "disabled": MCPServerState(key="disabled", transport="stdio", status="disabled"),
        "last": MCPServerState(key="last", transport="stdio", status="connected"),
        "first": MCPServerState(key="first", transport="stdio", status="connected"),
    }
    assert [row.key for row in manager.status_snapshot().servers] == ["first", "disabled", "last"]


def test_mcp_public_name_sanitize_and_prefix():
    public, server, tool = make_public_tool_name("mcp", "中文 server", "find symbol!*")
    assert public.startswith("mcp__server__find_symbol")
    assert server == "server"
    assert tool == "find_symbol"


def test_mcp_public_name_never_exceeds_llm_tool_name_limit():
    public, server, tool = make_public_tool_name("p" * 80, "s" * 80, "t" * 80)
    assert len(public) <= 64
    assert public.startswith("ppp")
    assert "__" in public
    assert server
    assert tool


def test_mcp_filter_deny_before_register():
    cfg = _cfg()
    server = cfg.mcp.servers["serena"]
    used: set[str] = set()
    allowed = build_tool_meta(cfg.mcp, server, server_key="serena", raw_tool=MCPRawTool(name="find_symbol"), used_names=used)
    denied_global = build_tool_meta(cfg.mcp, server, server_key="serena", raw_tool=MCPRawTool(name="delete_symbol"), used_names=used)
    denied_server = build_tool_meta(cfg.mcp, server, server_key="serena", raw_tool=MCPRawTool(name="write_file"), used_names=used)
    assert allowed.filtered is False
    assert denied_global.filtered is True
    assert denied_global.filter_reason == "global_deny"
    assert denied_server.filtered is True
    assert denied_server.filter_reason == "server_deny"


def test_mcp_output_redacts_and_truncates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert redact_secrets({"token": "abcdef123456", "nested": {"password": "p"}}) == {
        "token": "[REDACTED:12 chars]",
        "nested": {"password": "[REDACTED]"},
    }
    meta = MCPToolMeta(
        public_name="mcp__s__t",
        server_key="s",
        original_tool_name="t",
        normalized_tool_name="t",
        description="",
    )
    rendered = govern_text_output("x" * 50, meta, inline_max_chars=10, output_max_chars=100, call_id="abc")
    assert "MCP output truncated" in rendered
    huge = govern_text_output("x" * 200, meta, inline_max_chars=10, output_max_chars=100, call_id="abc")
    assert "artifact_saved" in huge
    assert "artifactPath" in huge


def test_mcp_default_approval_is_inherited_by_server():
    data = _cfg().model_dump(by_alias=True)
    data["mcp"]["defaultApproval"] = "allow"
    data["mcp"]["servers"]["serena"].pop("approval", None)
    cfg = Config.model_validate(data)
    server = cfg.mcp.servers["serena"]
    meta = build_tool_meta(cfg.mcp, server, server_key="serena", raw_tool=MCPRawTool(name="find_symbol"), used_names=set())
    assert meta.approval == "allow"


def test_mcp_explicit_allow_is_authoritative_for_unknown_risk():
    data = _cfg().model_dump(by_alias=True)
    data["mcp"]["defaultApproval"] = "allow"
    data["mcp"]["servers"]["serena"].pop("approval", None)
    cfg = Config.model_validate(data)
    server = cfg.mcp.servers["serena"]
    meta = build_tool_meta(cfg.mcp, server, server_key="serena", raw_tool=MCPRawTool(name="opaque_tool"), used_names=set())
    assert meta.risk == "unknown"
    assert meta.approval == "allow"


def test_mcp_approval_policy_controls_runtime_in_every_context():
    context = ToolRuntimeContext(source="agent", agent_session_uuid="agent-1")
    destructive = MCPToolMeta(
        public_name="mcp__s__delete",
        server_key="s",
        original_tool_name="delete",
        normalized_tool_name="delete",
        description="",
        approval="allow",
        risk="destructive",
    )
    assert can_call_without_prompt(destructive, context) == (True, "allowed_by_policy")
    destructive.approval = "deny"
    assert can_call_without_prompt(destructive, context) == (False, "approval_deny")
    destructive.approval = "ask"
    assert can_call_without_prompt(destructive, context) == (False, "needs_openbear_control")

    read_meta = MCPToolMeta(
        public_name="mcp__s__read",
        server_key="s",
        original_tool_name="read",
        normalized_tool_name="read",
        description="",
        approval="ask",
        risk="read",
    )
    assert can_call_without_prompt(read_meta, context) == (True, "allowed_read_context")


def test_mcp_readonly_hint_cannot_downgrade_dangerous_tool_name():
    assert classify_risk("delete_all_files", annotations={"readOnlyHint": True}) == "destructive"
    assert classify_risk("send_email", annotations={"readOnlyHint": True}) == "external"
    assert classify_risk("read_file", annotations={"readOnlyHint": True}) == "read"


def test_mcp_risk_keywords_use_word_boundaries_and_readonly_beats_open_world():
    assert classify_risk("web_search_prime", "Search web information") == "read"
    assert classify_risk("browser_snapshot", annotations={"readOnlyHint": True, "openWorldHint": True}) == "read"
    assert classify_risk("rm_file") == "destructive"


def test_mcp_text_output_redacts_plain_text_secrets():
    redacted = redact_text_secrets(
        "Authorization: Bearer abcdef1234567890 token=secret123 password=hunter2 "
        '"api_key": "abc123456" cookie="cookiesecret" sk-abcdef1234567890'
    )
    assert "abcdef1234567890" not in redacted
    assert "secret123" not in redacted
    assert "hunter2" not in redacted
    assert "abc123456" not in redacted
    assert "cookiesecret" not in redacted
    assert "sk-abcdef1234567890" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_mcp_web_confirmation_allows_negative_internal_chat_id():
    cfg = _cfg()
    manager = MCPManager(cfg)
    meta = MCPToolMeta(
        public_name="mcp__s__t",
        server_key="s",
        original_tool_name="t",
        normalized_tool_name="t",
        description="",
        approval="ask",
        risk="read",
    )

    async def web_confirm(payload: dict) -> dict:
        assert payload["title"] == "确认执行 MCP 工具"
        assert payload["action"] == "select"
        assert [row["value"] for row in payload["options"]] == ["once", "conversation", "always"]
        return {"status": "answered", "cancelled": False, "selectedValues": ["conversation"]}

    context = ToolRuntimeContext(chat_id=-1, source="web", web_confirm=web_confirm)
    assert await manager._confirm_call(meta, {}, context) == "conversation"


@pytest.mark.asyncio
async def test_mcp_always_trust_runs_current_call_before_persist_and_grants_conversation():
    events: list[str] = []

    async def update_approval(server: str, approval: str) -> None:
        assert (server, approval) == ("serena", "allow")
        events.append("persist")

    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> MCPRawResult:
            assert name == "delete_symbol"
            assert arguments == {"name": "x"}
            events.append("call")
            return MCPRawResult(content=[{"type": "text", "text": "done"}])

    manager = MCPManager(_cfg(), approval_updater=update_approval)
    meta = MCPToolMeta(
        public_name="mcp__serena__delete_symbol",
        server_key="serena",
        original_tool_name="delete_symbol",
        normalized_tool_name="delete_symbol",
        description="",
        approval="ask",
        risk="destructive",
    )
    manager._tools[meta.public_name] = meta
    manager._clients[meta.server_key] = FakeClient()  # type: ignore[assignment]

    async def web_confirm(payload: dict) -> dict:
        assert payload["action"] == "select"
        return {"status": "answered", "cancelled": False, "selectedValues": ["always"]}

    context = ToolRuntimeContext(
        chat_id=-1,
        source="web",
        conversation_uuid="conv-1",
        web_confirm=web_confirm,
    )
    assert await manager.call_tool(meta.public_name, {"name": "x"}, context) == "done"
    assert events == ["call", "persist"]
    assert manager._has_conversation_grant(meta, context) is True

    async def should_not_confirm(_payload: dict) -> dict:
        raise AssertionError("conversation grant should suppress repeated confirmation")

    context.web_confirm = should_not_confirm
    assert await manager.call_tool(meta.public_name, {"name": "x"}, context) == "done"
    assert events == ["call", "persist", "call"]


@pytest.mark.asyncio
async def test_mcp_server_uninstall_reservation_does_not_interrupt_active_call():
    started = asyncio.Event()
    release = asyncio.Event()

    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> MCPRawResult:
            started.set()
            await release.wait()
            return MCPRawResult(content=[{"type": "text", "text": "done"}])

    manager = MCPManager(_cfg())
    meta = MCPToolMeta(
        public_name="mcp__serena__read_symbol",
        server_key="serena",
        original_tool_name="read_symbol",
        normalized_tool_name="read_symbol",
        description="",
        approval="allow",
        risk="read",
    )
    manager._tools[meta.public_name] = meta
    manager._clients[meta.server_key] = FakeClient()  # type: ignore[assignment]
    context = ToolRuntimeContext(source="agent")

    call = asyncio.create_task(manager.call_tool(meta.public_name, {}, context))
    await started.wait()
    assert await manager.begin_server_uninstall("serena") == (False, 1)
    release.set()
    assert await call == "done"

    assert await manager.begin_server_uninstall("serena") == (True, 0)
    draining = json.loads(await manager.call_tool(meta.public_name, {}, context))
    assert draining["error"] == "mcp_server_draining"
    await manager.end_server_uninstall("serena")


@pytest.mark.asyncio
async def test_mcp_conversation_grant_never_overrides_explicit_deny():
    calls: list[str] = []

    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> MCPRawResult:
            calls.append(name)
            return MCPRawResult(content=[{"type": "text", "text": "called"}])

    manager = MCPManager(_cfg())
    meta = MCPToolMeta(
        public_name="mcp__serena__delete_symbol",
        server_key="serena",
        original_tool_name="delete_symbol",
        normalized_tool_name="delete_symbol",
        description="",
        approval="ask",
        risk="destructive",
    )
    manager._tools[meta.public_name] = meta
    manager._clients[meta.server_key] = FakeClient()  # type: ignore[assignment]
    context = ToolRuntimeContext(source="web", conversation_uuid="conv-1")
    manager._grant_conversation(meta, context)
    meta.approval = "deny"

    result = json.loads(await manager.call_tool(meta.public_name, {}, context))
    assert result["status"] == "denied"
    assert result["reason"] == "approval_deny"
    assert calls == []


@pytest.mark.asyncio
async def test_mcp_conversation_grant_is_scoped_to_one_tool():
    calls: list[str] = []

    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> MCPRawResult:
            calls.append(name)
            return MCPRawResult(content=[{"type": "text", "text": "called"}])

    manager = MCPManager(_cfg())
    granted = MCPToolMeta(
        public_name="mcp__serena__write_one",
        server_key="serena",
        original_tool_name="write_one",
        normalized_tool_name="write_one",
        description="",
        approval="ask",
        risk="write",
    )
    other = granted.model_copy(update={
        "public_name": "mcp__serena__delete_two",
        "original_tool_name": "delete_two",
        "normalized_tool_name": "delete_two",
        "risk": "destructive",
    })
    manager._tools = {granted.public_name: granted, other.public_name: other}
    manager._clients[granted.server_key] = FakeClient()  # type: ignore[assignment]
    context = ToolRuntimeContext(source="agent", conversation_uuid="conv-1", agent_session_uuid="agent-1")
    manager._grant_conversation(granted, context)

    assert await manager.call_tool(granted.public_name, {}, context) == "called"
    result = json.loads(await manager.call_tool(other.public_name, {}, context))
    assert result["status"] == "needs_openbear_control"
    assert calls == ["write_one"]


@pytest.mark.asyncio
async def test_mcp_tool_errors_are_redacted_before_return():
    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> MCPRawResult:
            raise RuntimeError("upstream token=secret12345")

    manager = MCPManager(_cfg())
    meta = MCPToolMeta(
        public_name="mcp__serena__find_symbol",
        server_key="serena",
        original_tool_name="find_symbol",
        normalized_tool_name="find_symbol",
        description="",
        approval="allow",
        risk="read",
    )
    manager._tools[meta.public_name] = meta
    manager._clients[meta.server_key] = FakeClient()  # type: ignore[assignment]
    result = await manager.call_tool(meta.public_name, {}, ToolRuntimeContext(source="agent"))
    assert "secret12345" not in result
    assert "[REDACTED]" in result


@pytest.mark.asyncio
async def test_mcp_client_collects_all_tool_and_prompt_pages():
    class PagedTransport:
        def __init__(self) -> None:
            self.requests: list[tuple[str, dict]] = []

        async def connect(self) -> None:
            return None

        async def request(self, method: str, params: dict | None = None, *, timeout_s: float):
            params = params or {}
            self.requests.append((method, params))
            cursor = params.get("cursor")
            if method == "tools/list":
                if not cursor:
                    return {"tools": [{"name": "one"}], "nextCursor": "tools-2"}
                return {"tools": [{"name": "two"}, {"name": "one"}]}
            if method == "prompts/list":
                if not cursor:
                    return {"prompts": [{"name": "p1"}], "nextCursor": "prompts-2"}
                return {"prompts": [{"name": "p2"}]}
            raise AssertionError(method)

        async def notify(self, method: str, params: dict | None = None) -> None:
            return None

        async def close(self) -> None:
            return None

    transport = PagedTransport()
    client = MCPClient("serena", _cfg().mcp.servers["serena"], transport=transport)  # type: ignore[arg-type]
    assert [tool.name for tool in await client.list_tools()] == ["one", "two"]
    assert [prompt["name"] for prompt in await client.list_prompts()] == ["p1", "p2"]
    assert ("tools/list", {"cursor": "tools-2"}) in transport.requests
    assert ("prompts/list", {"cursor": "prompts-2"}) in transport.requests


@pytest.mark.asyncio
async def test_mcp_list_changed_refreshes_tools_and_publishes_callback():
    published: list[bool] = []

    async def publish() -> None:
        published.append(True)

    class FakeClient:
        async def list_tools(self) -> list[MCPRawTool]:
            return [MCPRawTool(name="find_new", annotations={"readOnlyHint": True})]

    cfg = _cfg()
    manager = MCPManager(cfg, tools_changed_callback=publish)
    old = build_tool_meta(
        cfg.mcp,
        cfg.mcp.servers["serena"],
        server_key="serena",
        raw_tool=MCPRawTool(name="find_old", annotations={"readOnlyHint": True}),
        used_names=set(),
    )
    manager._clients["serena"] = FakeClient()  # type: ignore[assignment]
    manager._tools = {old.public_name: old}
    manager._all_tools = [old]
    manager._states["serena"] = MCPServerState(
        key="serena", transport="stdio", status="connected", tool_count=1
    )

    await manager._handle_server_notification("serena", "notifications/tools/list_changed", {})
    assert [tool.original_tool_name for tool in manager.available_tools()] == ["find_new"]
    assert manager.status_snapshot().servers[0].tool_count == 1
    assert published == [True]


@pytest.mark.asyncio
async def test_mcp_http_sse_dispatches_embedded_notifications():
    seen: list[str] = []
    notified = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json_loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'event: message\n'
                'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed","params":{}}\n\n'
                f'event: message\ndata: {{"jsonrpc":"2.0","id":{body["id"]},"result":{{"ok":true}}}}\n\n'
            ),
        )

    async def on_notification(method: str, params: dict) -> None:
        seen.append(method)
        notified.set()

    server = _cfg().mcp.servers["serena"].model_copy(
        update={"transport": "streamable_http", "url": "https://mcp.example/rpc"}
    )
    transport = StreamableHTTPTransport("serena", server)
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport.set_notification_handler(on_notification)
    try:
        assert await transport.request("tools/list", {}, timeout_s=5) == {"ok": True}
        await asyncio.wait_for(notified.wait(), timeout=1)
        assert seen == ["notifications/tools/list_changed"]
    finally:
        await transport.close()


def test_mcp_http_sse_selects_matching_response_id():
    resp = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=(
            'event: message\n'
            'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"x":1}}\n\n'
            'event: message\n'
            'data: {"jsonrpc":"2.0","id":2,"result":{"wrong":true}}\n\n'
            'event: message\n'
            'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        ),
    )
    msg = _decode_http_rpc_response(resp, request_id=1)
    assert msg["result"] == {"ok": True}


def test_mcp_http_response_id_mismatch_is_error():
    resp = httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {}})
    with pytest.raises(Exception, match="id mismatch"):
        _decode_http_rpc_response(resp, request_id=1)


@pytest.mark.asyncio
async def test_streamable_http_preserves_session_header():
    seen_headers: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        body = json_loads(request.content)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "session-1"},
            json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}},
        )

    transport = httpx.MockTransport(handler)
    server = _cfg().mcp.servers["serena"].model_copy(update={"transport": "streamable_http", "url": "https://mcp.example/rpc"})
    client = StreamableHTTPTransport("serena", server)
    client._client = httpx.AsyncClient(transport=transport)
    assert await client.request("initialize", {}, timeout_s=5) == {"ok": True}
    assert await client.request("tools/list", {}, timeout_s=5) == {"ok": True}
    assert seen_headers[0].get("mcp-session-id") is None
    assert seen_headers[1].get("mcp-session-id") == "session-1"
    await client.close()


@pytest.mark.asyncio
async def test_stdio_auto_retries_framed_initialize_for_strict_servers(tmp_path):
    server_path = tmp_path / "strict_mcp.py"
    server_path.write_text(
        r'''
import json
import sys


def read_msg():
    line = sys.stdin.buffer.readline()
    if not line:
        sys.exit(0)
    if not line.lower().startswith(b"content-length:"):
        # Strict framed server: ignore newline JSON until the client retries using
        # Content-Length framing.
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                sys.exit(0)
            if line.lower().startswith(b"content-length:"):
                break
    length = int(line.split(b":", 1)[1].strip())
    while True:
        header = sys.stdin.buffer.readline()
        if header in (b"\r\n", b"\n", b""):
            break
    return json.loads(sys.stdin.buffer.read(length).decode())


def send(msg):
    body = json.dumps(msg, separators=(",", ":")).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()


while True:
    msg = read_msg()
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"protocolVersion": "2024-11-05", "capabilities": {}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": []}})
    elif msg.get("id") is not None:
        send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})
''',
        encoding="utf-8",
    )
    cfg = MCPServerConfig(command=sys.executable, args=[str(server_path)], connectTimeoutS=1)
    client = MCPClient("strict", cfg)
    try:
        await client.connect()
        assert await client.list_tools() == []
    finally:
        await client.close()


def _fake_stdio_mcp_server(tmp_path, *, tool_name: str, label: str = "ok", exit_immediately: bool = False, instructions: str = ""):
    server_path = tmp_path / f"mcp_{tool_name}.py"
    if exit_immediately:
        server_path.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
        return server_path
    server_path.write_text(
        f'''
import json
import sys

TOOL_NAME = {tool_name!r}
LABEL = {label!r}
INSTRUCTIONS = {instructions!r}


def send(mid, result):
    print(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": result}}, separators=(",", ":")), flush=True)


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        result = {{"protocolVersion": "2024-11-05", "capabilities": {{}}}}
        if INSTRUCTIONS:
            result["instructions"] = INSTRUCTIONS
        send(mid, result)
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send(mid, {{"tools": [{{"name": TOOL_NAME, "description": "Read fake", "inputSchema": {{"type": "object", "properties": {{}}}}, "annotations": {{"readOnlyHint": True}}}}]}})
    elif method == "tools/call":
        params = msg.get("params") or {{}}
        send(mid, {{"content": [{{"type": "text", "text": LABEL + ":" + str(params.get("name") or "")}}]}})
    elif mid is not None:
        send(mid, {{}})
''',
        encoding="utf-8",
    )
    return server_path


def _config_with_mcp_server(tmp_path, server_path, *, tool_name: str = "read_one", enabled: bool = True, required: bool = True) -> Config:
    data = _cfg_data()
    data["storage"] = {"dbPath": str(tmp_path / "openbear.db")}
    data["tools"] = {"skillsDir": str(tmp_path / "skills")}
    data["web"] = {"enabled": False}
    data["mcp"] = {
        "enabled": enabled,
        "allowTools": ["*"],
        "denyTools": [],
        "defaultApproval": "allow",
        "servers": {
            "fake": {
                "enabled": True,
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(server_path)],
                "connectTimeoutS": 1,
                "toolCallTimeoutS": 5,
                "required": required,
                "tools": {"allow": [tool_name, "read*"], "deny": []},
            }
        },
    }
    return Config.model_validate(data)


@pytest.mark.asyncio
async def test_mcp_manager_exposes_server_instructions_snapshot(tmp_path):
    server = _fake_stdio_mcp_server(
        tmp_path,
        tool_name="read_one",
        label="one",
        instructions="Prefer symbol lookup before raw text search.",
    )
    manager = MCPManager(_config_with_mcp_server(tmp_path, server, tool_name="read_one"))
    await manager.start()
    try:
        assert manager.server_instructions_snapshot() == [
            {"server": "fake", "instructions": "Prefer symbol lookup before raw text search."}
        ]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_hot_reload_swaps_tools_without_restart(tmp_path):
    first = _fake_stdio_mcp_server(tmp_path, tool_name="read_one", label="one")
    second = _fake_stdio_mcp_server(tmp_path, tool_name="read_two", label="two")
    manager = MCPManager(_config_with_mcp_server(tmp_path, first, tool_name="read_one"))
    await manager.start()
    try:
        assert [meta.original_tool_name for meta in manager.available_tools()] == ["read_one"]
        await manager.reload(_config_with_mcp_server(tmp_path, second, tool_name="read_two"))
        assert [meta.original_tool_name for meta in manager.available_tools()] == ["read_two"]
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [True, False])
async def test_mcp_manager_hot_reload_failure_keeps_previous_tools(tmp_path, required):
    first = _fake_stdio_mcp_server(tmp_path, tool_name="read_one", label="one")
    broken = _fake_stdio_mcp_server(tmp_path, tool_name="broken", exit_immediately=True)
    manager = MCPManager(_config_with_mcp_server(tmp_path, first, tool_name="read_one", required=required))
    await manager.start()
    try:
        assert [meta.original_tool_name for meta in manager.available_tools()] == ["read_one"]
        with pytest.raises(RuntimeError):
            await manager.reload(_config_with_mcp_server(tmp_path, broken, tool_name="read_two", required=required))
        assert [meta.original_tool_name for meta in manager.available_tools()] == ["read_one"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_mcp_successful_reload_clears_conversation_grants(tmp_path):
    first = _fake_stdio_mcp_server(tmp_path, tool_name="read_one", label="one")
    second = _fake_stdio_mcp_server(tmp_path, tool_name="read_one", label="two")
    manager = MCPManager(_config_with_mcp_server(tmp_path, first, tool_name="read_one"))
    await manager.start()
    try:
        meta = manager.available_tools()[0]
        context = ToolRuntimeContext(source="web", conversation_uuid="conv-1")
        manager._grant_conversation(meta, context)
        assert manager._has_conversation_grant(meta, context)
        await manager.reload(_config_with_mcp_server(tmp_path, second, tool_name="read_one"))
        reloaded_meta = manager.available_tools()[0]
        assert not manager._has_conversation_grant(reloaded_meta, context)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_services_apply_config_hot_reloads_mcp_tools(tmp_path, monkeypatch):
    first = _fake_stdio_mcp_server(tmp_path, tool_name="read_one", label="one")
    second = _fake_stdio_mcp_server(tmp_path, tool_name="read_two", label="two")
    (tmp_path / "skills").mkdir()
    config_path = tmp_path / "openbear.json"
    config_path.write_text(json.dumps(_cfg_data()), encoding="utf-8")
    monkeypatch.setenv("OPENBEAR_CONFIG", str(config_path))
    base_data = _cfg_data()
    base_data["storage"] = {"dbPath": str(tmp_path / "openbear.db")}
    base_data["tools"] = {"skillsDir": str(tmp_path / "skills")}
    base_data["web"] = {"enabled": False}
    base_data["mcp"] = {"enabled": False, "servers": {}}
    svc = Services(Config.model_validate(base_data), SimpleNamespace())  # type: ignore[arg-type]
    await svc.db.connect()
    try:
        cfg_one = _config_with_mcp_server(tmp_path, first, tool_name="read_one")
        svc.apply_config(cfg_one)
        await svc._mcp_reload_task
        assert "mcp__fake__read_one" in svc.tools.names()

        cfg_two = _config_with_mcp_server(tmp_path, second, tool_name="read_two")
        svc.apply_config(cfg_two)
        await svc._mcp_reload_task
        names = svc.tools.names()
        assert "mcp__fake__read_two" in names
        assert "mcp__fake__read_one" not in names

        cfg_off = cfg_two.model_copy(update={"mcp": cfg_two.mcp.model_copy(update={"enabled": False})})
        svc.apply_config(cfg_off)
        assert not any(name.startswith("mcp__") for name in svc.tools.names())
        await svc._mcp_reload_task
        assert not svc.mcp.available_tools()
    finally:
        await svc.shutdown()


def json_loads(raw: bytes) -> dict:
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {}
