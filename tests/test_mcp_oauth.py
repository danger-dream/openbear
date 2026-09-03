from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import Config, MCPServerConfig
from app.db.engine import DB
from app.mcp.errors import MCPAuthRequired
from app.mcp.manager import MCPManager
from app.mcp.oauth import MCPAuthManager, MCPOAuthError
from app.mcp.transports import StreamableHTTPTransport
from app.web_admin import WebAdminServer


def _server_config(**overrides) -> MCPServerConfig:
    data = {
        "enabled": True,
        "transport": "streamable_http",
        "url": "https://api.githubcopilot.com/mcp/",
        "oauth": {
            "enabled": True,
            "clientId": "client-123",
            "scopes": ["repo", "read:user"],
        },
    }
    data.update(overrides)
    return MCPServerConfig.model_validate(data)


def _cfg() -> Config:
    return Config.model_validate({
        "telegram": {"botToken": "t", "whitelistIds": [123]},
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://x",
                    "apiKey": "k",
                    "protocol": "chat",
                    "models": [{"id": "gpt"}],
                }
            },
            "primary": "openai/gpt",
        },
        "memory": {"provider": "builtin"},
        "web": {"enabled": True, "host": "127.0.0.1", "port": 18961, "customUrl": "https://openbear.example"},
        "mcp": {
            "enabled": True,
            "servers": {
                "github": _server_config().model_dump(by_alias=True),
            },
        },
    })


@pytest.fixture
async def db(tmp_path):
    engine = DB(str(tmp_path / "t.db"))
    await engine.connect()
    yield engine
    await engine.close()


def test_protected_resource_metadata_url_appends_mcp_path():
    url = MCPAuthManager._protected_resource_metadata_url(_server_config())
    assert url == "https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"


@pytest.mark.asyncio
async def test_begin_authorization_uses_github_login_oauth_and_pkce(monkeypatch):
    manager = MCPAuthManager(SimpleNamespace(path="/tmp/openbear.db", conn=object()))

    async def fake_discover(server_config):
        return (
            "https://github.com/login/oauth/authorize",
            "https://github.com/login/oauth/access_token",
            "https://github.com/login/oauth",
            tuple(server_config.oauth.scopes),
        )

    monkeypatch.setattr(manager, "_discover_endpoints", fake_discover)
    result = await manager.begin_authorization(
        "github",
        _server_config(),
        external_base_url="https://openbear.example",
    )
    parsed = urlparse(result["authorizationUrl"])
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "github.com"
    assert parsed.path == "/login/oauth/authorize"
    assert query["client_id"] == ["client-123"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://openbear.example/api/mcp/oauth/callback/github"]
    assert " ".join(result["scopes"]) == "repo read:user"
    assert list(manager._pending.values())[0].code_verifier
    assert "access_token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_complete_authorization_encrypts_token_and_never_returns_plaintext(db, monkeypatch):
    manager = MCPAuthManager(db)
    server = _server_config()
    started = await manager.begin_authorization(
        "github",
        server,
        external_base_url="https://openbear.example",
    )
    state = parse_qs(urlparse(started["authorizationUrl"]).query)["state"][0]
    pending = manager._pending[state]
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(parse_qs(request.content.decode()).items()))
        return httpx.Response(
            200,
            json={
                "access_token": "gho_secret_token",
                "refresh_token": "ghr_secret_refresh",
                "token_type": "bearer",
                "scope": "repo read:user",
                "expires_in": 3600,
            },
        )

    real_async_client = httpx.AsyncClient

    def fake_async_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(**kwargs)

    monkeypatch.setattr("app.mcp.oauth.httpx.AsyncClient", fake_async_client)
    result = await manager.complete_authorization("github", server, {"state": state, "code": "abc"})
    assert result["authorized"] is True
    assert "gho_secret_token" not in json.dumps(result)
    assert captured["grant_type"] == ["authorization_code"]
    assert captured["code_verifier"] == [pending.code_verifier]
    status = await manager.status("github", server)
    assert status["authorized"] is True
    assert status["hasRefreshToken"] is True
    assert status["scopes"] == ["repo", "read:user"]
    cur = await db.conn.execute("SELECT ciphertext FROM mcp_oauth_tokens WHERE server_key=?", ("github",))
    row = await cur.fetchone()
    assert row is not None
    assert "gho_secret_token" not in row["ciphertext"]
    token = await manager.get_access_token("github", server)
    assert token == "gho_secret_token"
    key_path = manager._key_path()
    assert key_path.exists()
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
async def test_get_access_token_refreshes_near_expiry(db, monkeypatch):
    manager = MCPAuthManager(db)
    await manager._save_token("github", {
        "access_token": "old-token",
        "refresh_token": "refresh-1",
        "token_type": "Bearer",
        "expires_at": 1,
        "scope": "repo",
        "token_endpoint": "https://github.com/login/oauth/access_token",
        "client_id": "client-123",
    })

    async def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["refresh-1"]
        return httpx.Response(200, json={"access_token": "new-token", "refresh_token": "refresh-2", "expires_in": 3600})

    real_async_client = httpx.AsyncClient

    def fake_async_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(**kwargs)

    monkeypatch.setattr("app.mcp.oauth.httpx.AsyncClient", fake_async_client)
    token = await manager.get_access_token("github", _server_config())
    assert token == "new-token"
    stored = await manager._load_token("github")
    assert stored["refresh_token"] == "refresh-2"


@pytest.mark.asyncio
async def test_revoke_deletes_encrypted_token(db):
    manager = MCPAuthManager(db)
    await manager._save_token("github", {"access_token": "secret", "scope": "repo"})
    assert await manager.revoke("github") is True
    assert await manager._load_token("github") is None
    status = await manager.status("github", _server_config())
    assert status["authorized"] is False


@pytest.mark.asyncio
async def test_streamable_http_injects_bearer_and_raises_auth_required_on_401():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(
            401,
            headers={"www-authenticate": 'Bearer error="invalid_request", resource_metadata="https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"'},
            text="unauthorized",
        )

    async def provider(force_refresh: bool) -> str:
        return "refreshed-token" if force_refresh else "initial-token"

    transport = StreamableHTTPTransport("github", _server_config(), token_provider=provider)
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MCPAuthRequired) as exc:
            await transport.request("initialize", {}, timeout_s=5)
        assert "oauth-protected-resource/mcp/" in exc.value.resource_metadata_url
        assert seen == ["Bearer initial-token", "Bearer refreshed-token"]
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_mcp_start_marks_auth_required_without_failing_required_server(db, monkeypatch):
    cfg = _cfg()
    cfg.mcp.servers["github"].required = True

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.instructions = ""

        def set_notification_handler(self, handler):
            return None

        async def connect(self):
            raise MCPAuthRequired("need auth")

        async def close(self):
            return None

    manager = MCPManager(cfg, db=db, oauth_manager=MCPAuthManager(db))
    monkeypatch.setattr("app.mcp.manager.MCPClient", FakeClient)
    await manager.start()
    state = manager.status_snapshot().servers[0]
    assert state.status == "auth_required"
    assert state.error == ""


def test_oauth_callback_page_posts_same_origin_message_without_token():
    response = WebAdminServer._mcp_oauth_callback_page(server="github", ok=True)
    body = response.text
    assert "openbear:mcp-oauth" in body
    assert "window.location.origin" in body
    assert "gho_" not in body
    assert "access_token" not in body


@pytest.mark.asyncio
async def test_oauth_callback_route_is_public_and_does_not_echo_code(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    await db.connect()
    oauth = MCPAuthManager(db)
    cfg = _cfg()
    manager = MCPManager(cfg, db=db, oauth_manager=oauth)
    bot = SimpleNamespace()
    web = WebAdminServer(cfg, db, bot)  # type: ignore[arg-type]
    web.mcp = manager
    client = TestClient(TestServer(web.make_app()))
    await client.start_server()
    try:
        resp = await client.get("/api/mcp/oauth/callback/github?code=leak-me&state=bad")
        text = await resp.text()
        assert resp.status == 200
        assert "leak-me" not in text
        assert "oauth_state_invalid_or_expired" in text or "授权未完成" in text
    finally:
        await client.close()
        await db.close()


def test_oauth_error_code_is_allowlisted():
    code = WebAdminServer._mcp_oauth_error_code(MCPOAuthError("oauth_token_exchange_rejected:invalid_grant"))
    assert code == "oauth_token_exchange_rejected"
    assert WebAdminServer._mcp_oauth_error_code(RuntimeError("boom /tmp/secret")) == "oauth_operation_failed"
