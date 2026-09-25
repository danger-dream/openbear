"""Connection checks use CDP protocol doubles, never a real browser."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.browser.config import BrowserConfig
from app.browser.connection import probe_connection
from app.browser.service import BrowserService
from app.config_store import ConfigStore
from app.tools.base import ToolRegistry
from app.tools.browser import register_browser_tool
from tests import test_web_settings_channels_api
from tests.test_browser_native import FakeCDP, config, ctx
from tests.test_config_store import _sample_config

admin_env = test_web_settings_channels_api.admin_env


@pytest.mark.parametrize("transport", ["http", "ws"])
async def test_probe_performs_real_protocol_handshake_without_page_operations(transport):
    calls = []
    sockets = []

    async def socket(request):
        ws = web.WebSocketResponse()
        sockets.append(ws)
        await ws.prepare(request)
        async for msg in ws:
            packet = json.loads(msg.data)
            calls.append(packet["method"])
            result = {"product": "Chromium/test"} if packet["method"] == "Browser.getVersion" else {"targetInfos": [{"targetId": "existing", "type": "page"}]}
            await ws.send_json({"id": packet["id"], "result": result})
        return ws

    async def version(request):
        return web.json_response({"webSocketDebuggerUrl": str(server.make_url('/browser')).replace('http:', 'ws:')})

    app = web.Application()
    app.router.add_get('/json/version', version)
    app.router.add_get('/browser', socket)
    server = TestServer(app)
    await server.start_server()
    try:
        endpoint = str(server.make_url('/')) if transport == 'http' else str(server.make_url('/browser')).replace('http:', 'ws:')
        result = await probe_connection(BrowserConfig(mainEndpoint=endpoint))
        assert result['ok'] and result['product'] == 'Chromium/test'
        assert calls == ['Browser.getVersion', 'Target.getTargets']
    finally:
        await server.close()
    assert all(ws.closed for ws in sockets)


async def test_probe_requires_address_and_never_starts_worker(monkeypatch):
    constructor = AsyncMock(side_effect=AssertionError('No browser connection without address'))
    monkeypatch.setattr('app.browser.connection.CDP', constructor)
    result = await probe_connection(BrowserConfig())
    assert result['code'] == 'browser_endpoint_required'
    constructor.assert_not_called()
    assert BrowserConfig().enabled is False


@pytest.mark.parametrize('error,code', [(TimeoutError('secret endpoint'), 'browser_connection_timeout'), (OSError('token=private'), 'browser_connection_failed')])
async def test_probe_failure_closes_transport_without_echoing_endpoint(monkeypatch, error, code):
    client = SimpleNamespace(connect=AsyncMock(side_effect=error), close=AsyncMock())
    monkeypatch.setattr('app.browser.connection.CDP', lambda *a: client)
    result = await probe_connection(BrowserConfig(mainEndpoint='http://browser.invalid/?token=private'))
    assert result['ok'] is False and result['code'] == code
    assert 'private' not in json.dumps(result) and 'secret' not in json.dumps(result)
    client.close.assert_awaited_once()


async def test_cancellation_closes_probe_and_propagates(monkeypatch):
    started = asyncio.Event()
    async def connect():
        started.set()
        await asyncio.Event().wait()
    client = SimpleNamespace(connect=connect, close=AsyncMock())
    monkeypatch.setattr('app.browser.connection.CDP', lambda *a: client)
    task = asyncio.create_task(probe_connection(BrowserConfig(mainEndpoint='http://browser.invalid')))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.close.assert_awaited_once()


async def test_config_enable_requires_success_and_preserves_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(_sample_config()))
    store = ConfigStore(path)
    original = path.read_bytes()
    with pytest.raises(ValueError, match='连接地址'):
        await store.update_path('browser.enabled', True)
    assert path.read_bytes() == original and store.revision == 0
    await store.update_path('browser.mainEndpoint', 'http://browser.invalid')
    before = path.read_bytes()
    probe = AsyncMock(return_value={'ok': False, 'error': '无法连接'})
    monkeypatch.setattr('app.browser.connection.probe_connection', probe)
    with pytest.raises(ValueError, match='无法连接'):
        await store.update_path('browser.enabled', True)
    assert path.read_bytes() == before and store.revision == 1
    probe.return_value = {'ok': True, 'product': 'Test/1'}
    enabled = await store.update_path('browser.enabled', True)
    assert enabled.browser.enabled and enabled.browser._connection_verified
    assert '_connection_verified' not in path.read_text()
    service = BrowserService(enabled, str(tmp_path / 'workspace'))
    assert service.available
    assert not enabled.browser._connection_verified  # handoff is consumed
    registry = ToolRegistry()
    register_browser_tool(registry, service)
    assert registry.names() == ['Browser']
    probe.reset_mock()
    await store.update_path('browser.actionTimeoutS', 25)
    probe.assert_not_called()  # unrelated limits do not reconnect
    await store.update_path('browser.enabled', False)
    probe.assert_not_called()
    await service.close()


async def test_enabled_endpoint_change_and_bulk_mutation_validate_before_write(tmp_path, monkeypatch):
    raw = _sample_config()
    raw['browser'] = {'enabled': True, 'mainEndpoint': 'http://working.invalid'}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(raw))
    store = ConfigStore(path)
    original = path.read_bytes()
    probe = AsyncMock(return_value={'ok': False, 'error': '无法连接'})
    monkeypatch.setattr('app.browser.connection.probe_connection', probe)
    with pytest.raises(ValueError, match='无法连接'):
        await store.update_path('browser.mainEndpoint', 'http://failed.invalid')
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match='无法连接'):
        await store.mutate(lambda data: data['browser'].update(mainEndpoint='http://failed.invalid'))
    assert path.read_bytes() == original and store.revision == 0


async def test_runtime_does_not_advertise_browser_until_verified_and_invalidates_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr('app.browser.connection.CDP', FakeCDP)
    service = BrowserService(config(tmp_path), str(tmp_path / 'workspace'))
    registry = ToolRegistry()
    register_browser_tool(registry, service)
    assert 'Browser' not in registry.names()
    assert (await service.call({'action': 'status'}, ctx()))['error'] == 'browser_connection_unverified'
    assert (await service.validate_connection())['ok']
    assert service.available and not service.pages and not service.instances
    other = service.config.model_copy(deep=True)
    other.browser.main_endpoint = 'http://another.invalid'
    service.configure(other)
    assert not service.available
    await service.close()


async def test_late_probe_cannot_reenable_disabled_or_changed_browser(tmp_path, monkeypatch):
    began, finish = asyncio.Event(), asyncio.Event()
    async def probe(_):
        began.set()
        await finish.wait()
        return {'ok': True}
    monkeypatch.setattr('app.browser.service.probe_connection', probe)
    service = BrowserService(config(tmp_path), str(tmp_path / 'workspace'))
    task = asyncio.create_task(service.validate_connection())
    await began.wait()
    changed = service.config.model_copy(deep=True)
    changed.browser.enabled = False
    service.configure(changed)
    finish.set()
    assert not (await task)['applied']
    assert not service.available
    await service.close()


async def test_unavailable_browser_does_not_block_startup_and_hot_config_rechecks(tmp_path, monkeypatch):
    from app.config import Config
    from app.services import Services
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'openbear.json'
    monkeypatch.setenv('OPENBEAR_CONFIG', str(path))
    raw = _sample_config()
    raw.update(memory={'provider': 'builtin'}, web={'enabled': False}, mcp={'enabled': False},
               storage={'dbPath': str(tmp_path / 'test.db')}, tools={'skillsDir': str(tmp_path / 'skills')},
               browser={'enabled': True, 'mainEndpoint': 'http://unavailable.invalid'})
    path.write_text(json.dumps(raw))
    probe = AsyncMock(return_value={'ok': False, 'code': 'browser_connection_failed', 'error': '无法连接'})
    monkeypatch.setattr('app.browser.service.probe_connection', probe)
    svc = Services(Config.model_validate(raw), SimpleNamespace())
    monkeypatch.setattr(svc.models_dev_catalog, 'start', AsyncMock())
    monkeypatch.setattr(svc.update, 'start', AsyncMock())
    try:
        await svc.startup()
        assert 'Browser' not in svc.tools.names() and 'Read' in svc.tools.names()
        assert json.loads(path.read_text())['browser']['enabled'] is True
        probe.return_value = {'ok': True}
        changed = svc.config.model_copy(deep=True)
        svc.apply_config(changed)
        await svc._browser_validation_task
        assert 'Browser' in svc.tools.names()
        disabled = changed.model_copy(deep=True)
        disabled.browser.enabled = False
        svc.apply_config(disabled)
        if svc._browser_validation_task:
            await svc._browser_validation_task
        assert 'Browser' not in svc.tools.names()
    finally:
        await svc.shutdown()


async def test_settings_probe_is_authenticated_read_only_and_accepts_unsaved_address(admin_env, monkeypatch):
    probe = AsyncMock(return_value={'ok': True, 'product': 'Test/1', 'elapsedMs': 3})
    monkeypatch.setattr('app.web_console.config_api.probe_connection', probe)
    before = admin_env.cfg_path.read_bytes()
    response = await admin_env.client.post('/api/settings/browser/test', cookies=admin_env.cookie, json={'endpoint': 'http://draft.invalid'})
    assert response.status == 200 and (await response.json())['ok']
    assert probe.await_args.args[0].main_endpoint == 'http://draft.invalid'
    assert admin_env.cfg_path.read_bytes() == before
    assert admin_env.server.config.browser.enabled is False
    invalid = await admin_env.client.post('/api/settings/browser/test', cookies=admin_env.cookie, json={'endpoint': 'file:///private'})
    assert invalid.status == 400
    probe.assert_awaited_once()
    # A separate client without the authenticated cookie must not invoke a probe.
    import aiohttp
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as anonymous:
        async with anonymous.post(admin_env.client.make_url('/api/settings/browser/test'), json={'endpoint': 'http://draft.invalid'}) as denied:
            assert denied.status == 401
            probe.assert_awaited_once()
