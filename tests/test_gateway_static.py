"""Gateway-authenticated node frontend: real routes and FileResponse, temp dist only."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from yarl import URL

from app.managed.http import GATEWAY_APP_PAGES, gateway_frontend_route_allowed
from app.web_console.auth_api import PWA_PUBLIC_FILES
from tests.test_managed_gateway import gateway  # noqa: F401
from tests.test_managed_runtime import runtime  # noqa: F401


@pytest.fixture
async def node(gateway, monkeypatch):  # noqa: F811
    r = gateway
    dist = r.root / 'test-dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'icons').mkdir()
    r.index = '<!doctype html><html><body>node-owned frontend<script type="module" src="/assets/app-test.js"></script></body></html>'
    r.javascript = 'globalThis.nodeFrontendVersion = "isolated-version-A";'
    (dist / 'index.html').write_text(r.index)
    (dist / 'assets/app-test.js').write_text(r.javascript)
    (dist / 'assets/app-test.css').write_text('body { color: #123; }')
    r.build = {'schema': 1, 'version': 'isolated-test', 'buildId': '0123456789abcdef'}
    (dist / 'build-info.json').write_text(json.dumps(r.build))
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/Q3sAAAAASUVORK5CYII=')
    for path, (relative, content_type) in PWA_PUBLIC_FILES.items():
        content = (png if content_type == 'image/png' else b'{"name":"node manifest","start_url":"/"}'
                   if content_type == 'application/manifest+json' else b'self.addEventListener("push", () => {});')
        (dist / relative).write_bytes(content)
    r.dist = dist
    monkeypatch.setattr(r.svc.web_admin, '_web_dist_dir', lambda: dist)
    yield r


async def test_gateway_page_allowlists_follow_frontend_and_installed_routes(node):
    root = Path(__file__).resolve().parents[1]
    frontend = (root / 'web/src/App.vue').read_text()
    incoming = re.search(r'const pathToPage\s*=\s*\{(.*?)\n\};', frontend, re.S)
    outgoing = re.search(r'const pageToPath\s*=\s*\{(.*?)\n\};', frontend, re.S)
    assert incoming and outgoing, 'frontend route tables not found'
    paths = set(re.findall(r'''["'](/[^"']*)["']\s*:''', incoming[1]))
    links = set(re.findall(r''':\s*["'](/[^"']*)["']''', outgoing[1]))
    # /ddns is an incoming alias only: it opens the servers page on its 动态域名 tab.
    assert paths == links | {'/', '/ddns'}
    # Controller-only pages are never exposed through the access gateway.
    controller_only = {'/instances', '/servers', '/ddns'}
    expected = paths - controller_only
    assert GATEWAY_APP_PAGES == expected
    go = (root / 'gateway/frontend.go').read_text()
    table = re.search(r'var pagePaths\s*=\s*map\[string\]bool\{(.*?)\}', go, re.S)
    assert table, 'gateway page allowlist not found'
    assert set(re.findall(r'"([^"]+)"\s*:\s*true', table[1])) == expected
    host = node.svc.web_admin
    installed = {route.resource.canonical for route in node.client.server.app.router.routes()
                 if route.method == 'GET' and route.handler == host.handle_index}
    assert installed - {'/login'} - controller_only == expected


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_gateway_serves_only_actual_node_pages_and_compiled_files(node, method):
    r = node
    for path in sorted(GATEWAY_APP_PAGES):
        response = await r.client.request(method, path, headers=r.auth, allow_redirects=False)
        assert response.status == 200, path
        assert response.headers['Content-Type'].startswith('text/html')
        assert response.headers['Cache-Control'] == 'no-store'
        assert await response.read() == (r.index.encode() if method == 'GET' else b'')
    for path, expected, mime in (
        ('/assets/app-test.js', r.javascript.encode(), 'javascript'),
        ('/assets/app-test.css', b'body { color: #123; }', 'text/css'),
        *((path, (r.dist / relative).read_bytes(), content_type) for path, (relative, content_type) in PWA_PUBLIC_FILES.items()),
    ):
        response = await r.client.request(method, path, headers=r.auth)
        assert response.status == 200, path
        assert mime in response.headers['Content-Type']
        assert await response.read() == (expected if method == 'GET' else b'')
        if path in PWA_PUBLIC_FILES:
            assert response.headers['X-Content-Type-Options'] == 'nosniff'
            assert 'must-revalidate' in response.headers['Cache-Control']
    # Existing build-info interface is this protected API, not a static wildcard.
    response = await r.client.get('/api/system/version', headers=r.auth)
    assert response.status == 200 and (await response.json())['frontend'] == r.build
    assert (await r.client.get('/api/system/version')).status == 401
    assert (await r.client.get('/build-info.json', headers=r.auth)).status == 403


@pytest.mark.parametrize('path', ['/', '/chat', '/skills', '/assets/app-test.js', '/manifest.webmanifest', '/openbear-push-sw.js', '/icons/openbear-192.png'])
async def test_node_frontend_requires_correct_gateway_key_instance_and_session(node, path):
    r = node
    for headers in ({}, {**r.auth, 'X-OpenBear-Gateway-Key': 'invalid'},
                    {**r.auth, 'X-OpenBear-Instance': 'wrong-instance'},
                    {k: v for k, v in r.auth.items() if k != 'X-OpenBear-Gateway-Session'}):
        response = await r.client.get(path, headers=headers, allow_redirects=False)
        assert response.status == 401
    # Management authority is not implicitly extended to frontend bootstrap.
    assert (await r.client.get(path, headers=r.management, allow_redirects=False)).status == 403


async def test_static_allowance_never_opens_login_management_or_other_resources(node):
    r = node
    for method, path in (
        ('GET', '/login'), ('GET', '/instances'), ('GET', '/api/instances'),
        ('GET', '/api/managed/v1/status'), ('GET', '/api/managed/v1/config'),
        ('POST', '/api/system/restart'), ('POST', '/api/system/update'),
        ('GET', '/api/auth/session'), ('POST', '/api/auth/password/login'),
        ('GET', '/index.html'), ('GET', '/static/app.js'), ('GET', '/icons/arbitrary.png'),
        ('GET', '/settings/new-page'), ('GET', '/assets-extra/app-test.js'),
        ('POST', '/'), ('POST', '/assets/app-test.js'), ('POST', '/manifest.webmanifest'),
    ):
        assert (await r.client.request(method, path, headers=r.auth, allow_redirects=False)).status == 403, (method, path)
    for path in ('/api/conversations', '/api/managed/v1/status'):
        assert (await r.client.get(path)).status == 401
    assert (await r.client.get('/api/managed/v1/status', headers=r.management)).status == 200
    assert (await r.client.get('/api/conversations', headers=r.auth)).status == 200


async def test_gateway_frontend_preserves_existing_path_and_symlink_safety(node):
    r = node
    secret = r.root / 'not-public.txt'
    secret.write_text('outside-public-root')
    (r.dist / 'assets/escape.js').symlink_to(secret)
    worker = r.dist / 'openbear-push-sw.js'
    worker.unlink()
    worker.symlink_to(secret)
    for path in ('/assets/escape.js', '/openbear-push-sw.js'):
        response = await r.client.get(path, headers=r.auth)
        assert response.status == 404
        assert 'outside-public-root' not in await response.text()
    for path in ('/assets/../index.html', '/assets/%2e%2e/index.html', '/assets/%252e%252e/index.html',
                 '/assets/%5c..%5cnot-public.txt', '/assets//app-test.js', '/icons/../not-public.txt'):
        url = URL(str(r.client.make_url('/')).rstrip('/') + path, encoded=True)
        response = await r.client.session.get(url, headers=r.auth, allow_redirects=False)
        assert response.status in {403, 404}
        assert 'outside-public-root' not in await response.text()
    assert (await r.client.get('/assets/missing.js', headers=r.auth)).status == 404
    assert (await r.client.get('/assets/', headers=r.auth)).status == 404


def test_frontend_policy_checks_actual_bound_handler():
    host = SimpleNamespace(handle_index=object(), handle_asset=object(), handle_pwa_asset=object())
    for path, handler in (('/', host.handle_index), ('/assets/app.js', host.handle_asset),
                          ('/manifest.webmanifest', host.handle_pwa_asset)):
        request = SimpleNamespace(method='GET', path=path, match_info=SimpleNamespace(handler=handler))
        assert gateway_frontend_route_allowed(request, host)
        request.match_info.handler = object()
        assert not gateway_frontend_route_allowed(request, host)
    request = SimpleNamespace(method='GET', path='/new-admin-page', match_info=SimpleNamespace(handler=host.handle_index))
    assert not gateway_frontend_route_allowed(request, host)


async def test_legacy_local_login_assets_stay_public(runtime, monkeypatch):  # noqa: F811
    r = runtime
    dist = r.root / 'local-dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<html>legacy local login</html>')
    (dist / 'assets/app.js').write_text('globalThis.legacyLocal = true;')
    (dist / 'manifest.webmanifest').write_text('{"name":"legacy"}')
    monkeypatch.setattr(r.svc.web_admin, '_web_dist_dir', lambda: dist)
    assert (await r.client.get('/', allow_redirects=False)).status == 302
    for path in ('/login', '/assets/app.js', '/manifest.webmanifest'):
        assert (await r.client.get(path, allow_redirects=False)).status == 200


INDEX_WITH_ASSETS = (
    '<link rel="manifest" href="/manifest.webmanifest" />'
    '<link rel="icon" type="image/x-icon" href="/assets/brand/favicon-2888ecac0deb.ico" />'
    '<script>window.__OPENBEAR_AUTH_MODE__ = "[[controller]]";\n'
    'window.__OPENBEAR_ASSET_BASE__ = "[[asset-base]]";</script>'
    '<script type="module" crossorigin src="/assets/index-yI1fbXhm.js"></script>'
    '<link rel="modulepreload" crossorigin href="/assets/vendor-CvLKSbGU.js">'
    '<link rel="stylesheet" crossorigin href="/assets/index-BUDn83CX.css">')


async def test_gateway_page_loads_its_build_below_its_own_prefix(node):
    r = node
    base = f"/_i/{r.values['OPENBEAR_INSTANCE_ID']}/"
    (r.dist / 'index.html').write_text(INDEX_WITH_ASSETS)
    page = await (await r.client.get('/chat', headers={**r.auth, 'X-OpenBear-Gateway-Assets': base})).text()
    assert f'window.__OPENBEAR_ASSET_BASE__ = "{base}";' in page and '"/assets/' not in page
    assert page.count(f'="{base}assets/') == 4 and 'href="/manifest.webmanifest"' in page
    assert 'window.__OPENBEAR_AUTH_MODE__ = "gateway"' in page
    # A gateway that does not route prefixes (older release), or a prefix other
    # than this instance's own, keeps the root-relative page.
    for headers in (r.auth, {**r.auth, 'X-OpenBear-Gateway-Assets': '/_i/other-instance/'}):
        page = await (await r.client.get('/chat', headers=headers)).text()
        assert page == INDEX_WITH_ASSETS.replace('"[[controller]]"', '"gateway"')


async def test_local_page_ignores_asset_prefix_announcement(runtime, monkeypatch):  # noqa: F811
    r = runtime
    dist = r.root / 'local-dist'
    dist.mkdir()
    (dist / 'index.html').write_text(INDEX_WITH_ASSETS)
    monkeypatch.setattr(r.svc.web_admin, '_web_dist_dir', lambda: dist)
    page = await (await r.client.get('/login', headers={'X-OpenBear-Gateway-Assets': '/_i/x/'})).text()
    assert page == INDEX_WITH_ASSETS.replace('"[[controller]]"', '"managed"')


def test_gateway_notification_worker_is_the_frontend_worker():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'gateway/openbear-push-sw.js').read_bytes() == (root / 'web/public/openbear-push-sw.js').read_bytes()
