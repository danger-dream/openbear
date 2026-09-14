from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from app.web_admin import WebAdminServer
from app.web_console.auth_api import PWA_PUBLIC_FILES

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def pwa_web(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    shutil.copy2(ROOT / "web/public/manifest.webmanifest", dist)
    shutil.copytree(ROOT / "web/public/icons", dist / "icons")
    (dist / "index.html").write_text("<html>test console</html>")
    (dist / "build-info.json").write_text('{"private-root-file":true}')
    (dist / "icons/private.txt").write_text("not public")
    # Use the REAL make_app routes, middleware, static handlers and origin checks.
    # Only realtime lifecycle and session lookup are replaced; no database or bot.
    server = WebAdminServer.__new__(WebAdminServer)
    server.config = SimpleNamespace(web=SimpleNamespace(custom_url=""))
    server._web_dist_dir = lambda: dist

    async def session(request):
        return SimpleNamespace(chat_id=1, expires_at=9999999999) if request.headers.get("Authorization") == "test-session" else None

    async def lifecycle(_app):
        yield

    async def shutdown(_app):
        pass

    async def protected(_request):
        return web.json_response({"ok": True})

    server.session_from_request = session
    server._realtime_context = lifecycle
    server._realtime_shutdown = shutdown
    server.handle_api_conversations = protected
    server.handle_api_conversation_create = protected
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    try:
        yield SimpleNamespace(client=client, dist=dist, server=server, tmp=tmp_path)
    finally:
        await client.close()


async def test_pwa_public_exact_resources_have_real_bytes_mime_and_revalidation(pwa_web):
    for url, (filename, mime) in PWA_PUBLIC_FILES.items():
        response = await pwa_web.client.get(url, allow_redirects=False)
        assert response.status == 200
        assert response.content_type == mime
        assert await response.read() == (pwa_web.dist / filename).read_bytes()
        assert response.headers["Cache-Control"] == "no-cache, max-age=0, must-revalidate"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "Set-Cookie" not in response.headers
        head = await pwa_web.client.head(url)
        assert head.status == 200
        assert head.content_type == mime
        assert int(head.headers["Content-Length"]) == (pwa_web.dist / filename).stat().st_size
        assert await head.read() == b""
        conditional = await pwa_web.client.get(url, headers={"If-None-Match": response.headers["ETag"]})
        assert conditional.status == 304
        assert "must-revalidate" in conditional.headers["Cache-Control"]
    manifest = await (await pwa_web.client.get("/manifest.webmanifest")).json()
    assert [manifest[key] for key in ("id", "start_url", "scope")] == ["./"] * 3


@pytest.mark.parametrize("url", list(PWA_PUBLIC_FILES))
async def test_pwa_missing_resource_returns_404_not_html(pwa_web, url):
    (pwa_web.dist / PWA_PUBLIC_FILES[url][0]).unlink()
    response = await pwa_web.client.get(url, allow_redirects=False)
    assert response.status == 404
    assert "text/html" not in response.headers["Content-Type"]


@pytest.mark.parametrize("url", [
    "/icons/private.txt", "/icons/", "/manifest.json", "/build-info.json", "/index.html", "/openbear.json",
    "/icons/../build-info.json", "/icons/%2e%2e/build-info.json", "/icons/%2e%2e%2fbuild-info.json",
    "/icons/openbear-192.png/../private.txt", "/manifest.webmanifest/private", "/icons/%252e%252e/private.txt",
])
async def test_pwa_does_not_open_directories_root_files_or_traversal(pwa_web, url):
    encoded = URL(url, encoded=True)
    anonymous = await pwa_web.client.get(encoded, allow_redirects=False)
    assert anonymous.status == 302
    assert anonymous.headers["Location"] == "/login"
    authenticated = await pwa_web.client.get(encoded, headers={"Authorization": "test-session"}, allow_redirects=False)
    assert authenticated.status == 404
    assert b"private-root-file" not in await authenticated.read()


@pytest.mark.parametrize("inside_dist", [False, True])
async def test_pwa_symlink_escape_is_rejected(pwa_web, inside_dist):
    outside = (pwa_web.dist if inside_dist else pwa_web.tmp) / "outside.png"
    outside.write_bytes(b"not public")
    icon = pwa_web.dist / "icons/openbear-192.png"
    icon.unlink()
    icon.symlink_to(outside)
    response = await pwa_web.client.get("/icons/openbear-192.png", allow_redirects=False)
    assert response.status == 404
    assert b"not public" not in await response.read()


async def test_pwa_does_not_weaken_api_auth_csrf_or_enable_static_writes(pwa_web):
    for url in ("/api/auth/session", "/api/conversations", "/api/settings", "/api/events/ws"):
        response = await pwa_web.client.get(url, allow_redirects=False)
        assert response.status == 401
        assert (await response.json())["error"] == "unauthorized"
    allowed = await pwa_web.client.get("/api/conversations", headers={"Authorization": "test-session"})
    assert allowed.status == 200
    cross_origin = await pwa_web.client.post("/api/conversations", headers={"Authorization": "test-session", "Origin": "https://untrusted.example"}, json={})
    assert cross_origin.status == 403
    assert (await cross_origin.json())["error"] == "csrf_origin_rejected"
    same_origin = await pwa_web.client.post("/api/conversations", headers={"Authorization": "test-session", "Origin": str(pwa_web.client.make_url("/")).rstrip("/")}, json={})
    assert same_origin.status == 200
    for url in PWA_PUBLIC_FILES:
        anonymous = await pwa_web.client.post(url, allow_redirects=False)
        assert anonymous.status == 302
        csrf = await pwa_web.client.post(url, headers={"Authorization": "test-session", "Origin": "https://untrusted.example"}, allow_redirects=False)
        assert csrf.status == 403
        authenticated = await pwa_web.client.post(url, headers={"Authorization": "test-session"}, allow_redirects=False)
        assert authenticated.status == 405
