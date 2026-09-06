from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db.engine import DB
from app.web_admin import _LOGIN_NONCE_COOKIE, WebAdminServer
from tests.test_web_admin import FakeBot, _cfg, _login_cookie


@pytest.fixture
async def login_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBEAR_WEB_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    db = DB(str(tmp_path / "login.db"))
    await db.connect()
    bot = FakeBot()
    server = WebAdminServer(_cfg(), db, bot)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div>test shell</div>", encoding="utf-8")
    server._web_dist_dir = lambda: dist
    await server.ensure_secret_key()
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    try:
        yield SimpleNamespace(db=db, bot=bot, server=server, client=client, dist=dist)
    finally:
        await client.close()
        await db.close()


async def test_https_config_redirects_http_login_before_secret_entry(login_env):
    env = login_env
    env.server.config.web.custom_url = "https://console.example:8443"
    response = await env.client.get("/login?ignored=1", headers={"Host": "other.invalid"}, allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "https://console.example:8443/login"
    assert response.headers["Cache-Control"] == "no-store"
    assert not response.cookies
    assert not env.bot.sent
    assert (await env.client.get("/health")).status == 200


@pytest.mark.parametrize("path,method", [
    ("/api/auth/login/start", "post"),
    ("/api/auth/login/consume/example", "post"),
    ("/api/auth/login/status/example", "get"),
])
async def test_http_auth_request_rejected_before_any_nonce_or_notification(login_env, path, method):
    env = login_env
    env.server.config.web.custom_url = "https://console.example"
    response = await getattr(env.client, method)(path, json={"secret": await env.server.get_secret_key()})
    assert response.status == 400
    assert (await response.json())["error"] == "https_required"
    assert not response.cookies
    assert not env.bot.sent
    count = await (await env.db.conn.execute("SELECT COUNT(*) FROM web_login_requests")).fetchone()
    assert count[0] == 0


async def test_https_proxy_login_keeps_secure_cookie_and_nonce_validation(login_env):
    env = login_env
    env.server.config.web.custom_url = "https://console.example"
    headers = {"X-Forwarded-Proto": "https"}
    assert (await env.client.get("/login", headers=headers)).status == 200
    start = await env.client.post("/api/auth/login/start", headers=headers, json={"secret": await env.server.get_secret_key()})
    payload = await start.json()
    assert payload["expiresIn"] == 300
    nonce = start.cookies[_LOGIN_NONCE_COOKIE]
    assert nonce["secure"] and nonce["httponly"] and nonce["samesite"] == "Lax"
    request = payload["requestUuid"]
    await env.server.decide_login_request(request, approved=True, decided_by=123)
    missing = await env.client.post(f"/api/auth/login/consume/{request}", headers=headers)
    assert missing.status == 403
    assert await env.server.login_request_status(request) == "approved"
    accepted = await env.client.post(f"/api/auth/login/consume/{request}", headers=headers,
                                    cookies={_LOGIN_NONCE_COOKIE: nonce.value})
    assert accepted.status == 200
    assert accepted.cookies["openbear_web_session"]["secure"]


async def test_login_status_and_session_not_cached(login_env):
    env = login_env
    missing = await env.client.get("/api/auth/login/status/not-present")
    assert (await missing.json())["status"] == "missing"
    assert missing.headers["Cache-Control"] == "no-store"
    cookie = await _login_cookie(env)
    session = await env.client.get("/api/auth/session", cookies={"openbear_web_session": cookie})
    assert session.status == 200
    assert session.headers["Cache-Control"] == "no-store"


async def test_build_handshake_is_authenticated_uncached_and_reads_new_dist(login_env):
    env = login_env
    assert (await env.client.get("/api/system/version")).status == 401
    cookie = await _login_cookie(env)
    headers = {"Cookie": f"openbear_web_session={cookie}"}
    legacy = await env.client.get("/api/system/version", headers=headers)
    assert (await legacy.json())["frontend"] is None
    for build_id in ["1" * 16, "2" * 16]:
        info = {"schema": 1, "version": "0.1.2", "buildId": build_id}
        (env.dist / "build-info.json").write_text(json.dumps(info), encoding="utf-8")
        response = await env.client.get("/api/system/version", headers=headers)
        assert response.headers["Cache-Control"] == "no-store"
        assert (await response.json())["frontend"] == info
    (env.dist / "build-info.json").write_text("[]", encoding="utf-8")
    response = await env.client.get("/api/system/version", headers=headers)
    assert (await response.json())["frontend"] is None
