"""Controller instance acceptance: real SQLite, Web turns, aiohttp gateway and instance peers."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import json
import shutil
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.instances import InstanceService, install, remote
from app.instances.contracts import Form, InstanceError, origin, relay_path
from app.instances.remote import deployment_archive
from app.instances.security import _AUDIT, _REFERENCE_AUTH, Scrubber
from app.llm.events import StreamEvent, ToolCall
from app.references import BUNDLE_FIELD
from app.tools.base import (
    ToolRegistry,
    ToolRuntimeContext,
    current_tool_context,
    redact_tool_arguments_for_audit,
)
from app.tools.instances import dispatch, register_instance_tools
from app.web_console.core import _WEB_SESSION_KEY
from app.web_console.live_stream import _WebStreamRenderer
from app.webhooks.repository import many, one
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _login_cookie
from tests.test_webhooks_backend import env as base_env


def fake_gateway():
    """Speaks the Go gateway's control API; proxies probes to the real instance peer."""
    g = SimpleNamespace(routes={}, bodies=[], events=[], fail=False, bad_ack=False, key='isolated-control-key-'+'x'*40, url='')

    async def handle(request):
        if request.headers.get('Authorization') != 'Bearer '+g.key:
            return web.json_response({'ok': False}, status=401)
        if g.fail:
            return web.json_response({'ok': False}, status=503)
        if request.path == '/control/v1/status':
            return web.json_response({'ok': True, 'version': 1, 'publicUrl': g.public, 'clientIp': {'source': 'auto', 'trustedProxies': []},
                'clientIpObserved': None, 'instances': [{'id': k, 'revision': v['body']['revision'], 'enabled': v['body']['enabled'], 'username': v['username']} for k, v in g.routes.items()]})
        if request.path == '/control/v1/events':
            after = int(request.query.get('after', '0'))
            g.events = [e for e in g.events if e['seq'] > after]
            return web.json_response({'ok': True, 'items': g.events})
        if request.path == '/control/v1/client-ip':
            return web.json_response({'ok': True, 'clientIp': await request.json()})
        identity = request.match_info['id']
        if request.method == 'PUT':
            body = await request.json()
            g.bodies.append(body.copy())
            previous = g.routes.get(identity)
            if previous and body['revision'] <= previous['body']['revision']:
                if body != previous['body']:
                    return web.json_response({'ok': False}, status=409)
            else:
                if not previous and not (body.get('username') and body.get('password')):
                    return web.json_response({'ok': False}, status=400)
                entry = dict(previous or {})
                entry['username'] = body.get('username', entry.get('username'))
                if 'password' in body:
                    entry['passwordHash'] = hashlib.sha256(body['password'].encode()).hexdigest()
                entry['body'] = body
                g.routes[identity] = entry
            entry = g.routes[identity]
            return web.json_response({'ok': True, 'id': identity, 'revision': body['revision']+(1 if g.bad_ack else 0), 'enabled': body['enabled'], 'username': entry['username']})
        entry = g.routes.get(identity)
        if not entry:
            return web.json_response({'ok': False}, status=404)
        body = entry['body']
        try:
            async with aiohttp.ClientSession() as http, http.get(body['upstream']+'/api/gateway/v1/probe', headers={
                    'X-OpenBear-Gateway-Key': body['businessKey'], 'X-OpenBear-Instance': identity}) as response:
                status, reachable = await response.json(), response.status == 200
        except aiohttp.ClientError:
            status, reachable = None, False
        return web.json_response({'ok': True, 'id': identity, 'revision': body['revision'], 'reachable': reachable, 'status': status})
    app = web.Application()
    app.router.add_get('/control/v1/status', handle)
    app.router.add_get('/control/v1/events', handle)
    app.router.add_put('/control/v1/client-ip', handle)
    app.router.add_put('/control/v1/instances/{id}', handle)
    app.router.add_get('/control/v1/instances/{id}/probe', handle)
    return g, app


@pytest.fixture
async def env(base_env, monkeypatch):  # noqa: F811
    e = base_env
    e.instances = InstanceService(e.db, e.server)
    e.server.instances = e.instances
    await e.instances.start()
    register_instance_tools(e.server.tools, e.instances)
    child = SimpleNamespace(identity='', key='', business='', ready=True, mounted=True, health='healthy', issues=[], redirect='', status=200, calls=[], reflect=False)

    async def handle(request):
        child.calls.append((request.method, request.path, dict(request.headers)))
        if request.path == '/api/gateway/v1/probe':
            ok = request.headers.get('X-OpenBear-Gateway-Key') == child.business and request.headers.get('X-OpenBear-Instance') == child.identity
        else:
            ok = request.headers.get('Authorization') == 'Bearer '+child.key
        if not ok:
            return web.json_response({'ok': False}, status=401)
        if child.redirect:
            raise web.HTTPFound(child.redirect)
        if request.path in {'/api/managed/v1/status', '/api/gateway/v1/probe'}:
            return web.json_response({'ok': True, 'instanceId': child.identity, 'ready': child.ready,
                'persistence': {'root': '/workspace', 'mounted': child.mounted, 'dbPath': '/workspace/data/db.sqlite', 'workspacePath': '/workspace/workspace'},
                'health': {'status': child.health, 'issues': child.issues}}, status=child.status)
        return web.json_response({'ok': True, 'reflected': request.headers.get('Authorization') if child.reflect else '', 'body': await request.json() if request.can_read_body else None})
    child_app = web.Application()
    child_app.router.add_route('*', '/{path:.*}', handle)
    e.child_server = TestServer(child_app)
    await e.child_server.start_server()
    e.child, e.child_url = child, str(e.child_server.make_url('')).rstrip('/')

    g, gateway_app = fake_gateway()
    e.gateway_server = TestServer(gateway_app)
    await e.gateway_server.start_server()
    g.url = str(e.gateway_server.make_url('')).rstrip('/')
    g.public = 'http://203.0.113.10:18962'
    e.gateway = g
    # The fixture gateway is already "installed": state as enable() would leave it. It reports
    # no build, so automatic updates (which would reinstall it) are off unless a test enables them.
    await e.instances.gateway.store({'enabled': True, 'publicUrl': g.public, 'serverId': 'local', 'port': 18962, 'status': 'connected', 'autoUpdate': False},
                                    {'controlKey': g.key, 'controlUrl': g.url})
    commands = []
    # Commands are only recorded: they would run as root on this machine. A test that needs a
    # delivery script to really write its files under e.tmp sets e.execute.
    e.execute = False

    async def fake_run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        commands.append((target.get('id'), command))
        if 'ss -Hltnp' in command or 'netstat' in command:
            return {'ok': True, 'stdout': 'free\n', 'stderr': '', 'exitCode': 0, 'knownHosts': ''}
        if not e.execute:
            return {'ok': True, 'exitCode': 0, 'stdout': '', 'stderr': '', 'knownHosts': ''}
        process = await asyncio.create_subprocess_exec('/bin/sh', '-c', command, cwd=e.tmp, stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate(stdin)
        return {'ok': process.returncode == 0, 'exitCode': process.returncode, 'stdout': out.decode(), 'stderr': err.decode(), 'knownHosts': ''}
    monkeypatch.setattr(e.instances.servers, 'run', fake_run)
    e.commands = commands
    e.instances.servers.local_ip = '203.0.113.10'

    @web.middleware
    async def auth(request, handler):
        if request.headers.get('X-Test-Owner'):
            request[_WEB_SESSION_KEY] = SimpleNamespace(chat_id=int(request.headers['X-Test-Owner']))
        return await handler(request)
    app = web.Application(middlewares=[auth])
    install(app, e.server, e.instances)
    e.http = TestClient(TestServer(app))
    await e.http.start_server()
    try:
        yield e
    finally:
        await e.instances.close()
        await e.http.close()
        await e.child_server.close()
        await e.gateway_server.close()


def form(e, **patch):
    return {'name': '实例丙', 'serverId': 'local', 'managementUrl': e.child_url, 'workspacePath': str(e.tmp/'child-workspace'),
            'account': {'username': 'family', 'password': 'account-fixture-password'},
            'models': {'mode': 'copy', 'primary': 'openai/gpt'},
            'notifications': {'loginSuccess': True, 'loginFailure': True, 'health': True}, **patch}


async def create(e, **patch):
    item = (await e.instances.save(123, form(e, **patch)))['item']
    row = await e.instances.row(123, item['id'])
    credentials = e.instances.vault.open(row['protected'])
    e.child.identity, e.child.key, e.child.business = item['id'], credentials['managementKey'], credentials['businessKey']
    return item, credentials


async def run(e, item, kind='deployment', **data):
    accepted = await e.instances.start_operation(123, item['id'], kind, data)
    await asyncio.gather(*list(e.instances.tasks.values()))
    op = await one(e.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (accepted['operation']['id'],))
    return accepted, op


def tool(action, params):
    return [StreamEvent(kind='tool_call', tool_calls=[ToolCall(uuid.uuid4().hex, 'InstanceControl', json.dumps({'action': action, 'params': params}))]), StreamEvent(kind='finish', finish_reason='tool_calls')]


def model(e, scripts):
    backend = FakeStreamBackend(scripts)
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    return backend


async def test_create_requires_enabled_gateway_and_owner(env):
    e = env
    assert (await e.http.post('/api/instances', json=form(e))).status == 401
    assert (await e.http.post('/api/instances', json=form(e), headers={'X-Test-Owner': '999'})).status == 403
    value, private = await e.instances.gateway.config()
    await e.instances.gateway.store({**value, 'enabled': False}, private)
    response = await e.http.post('/api/instances', json=form(e), headers={'X-Test-Owner': '123'})
    assert response.status == 409 and (await response.json())['error'] == 'gateway_not_enabled'
    await e.instances.gateway.store(value, private)
    e.server.config.managed.enabled = True
    assert (await e.http.get('/api/instances', headers={'X-Test-Owner': '123'})).status == 403
    registry = ToolRegistry()
    register_instance_tools(registry, e.instances)
    assert 'InstanceControl' not in registry.names()
    e.server.config.managed.enabled = False


async def test_create_and_deploy_in_one_step_with_automatic_values(env):
    e = env
    model(e, [[StreamEvent(kind='content', text='deploying'), StreamEvent(kind='finish', finish_reason='stop')]])
    submitted = form(e)
    submitted.pop('managementUrl')
    submitted.pop('workspacePath')
    response = await e.http.post('/api/instances', json=submitted, headers={'X-Test-Owner': '123'})
    assert response.status == 200, await response.text()
    result = await response.json()
    item = result['item']
    await asyncio.gather(*list(e.instances.tasks.values()))
    # Port, address and data directory are automatic.
    assert item['port'] == 18970 and item['managementUrl'] == 'http://127.0.0.1:18970'
    assert item['workspacePath'] == '/opt/openbear/instances/family'
    assert item['serverName'] == '本机' and item['models'] == {'mode': 'copy', 'primary': 'openai/gpt'}
    # Account reached the gateway immediately; password never stored afterwards.
    assert e.gateway.routes[item['id']]['username'] == 'family'
    stored = e.instances.vault.open((await e.instances.row(123, item['id']))['protected'])
    assert 'account-fixture-password' not in json.dumps(stored)
    # Controller providers (with keys) were copied for the instance's first start.
    assert stored['initialConfig']['models']['primary'] == 'openai/gpt'
    assert stored['initialConfig']['models']['providers']['openai']['apiKey'] == 'k'
    # A deployment session started in the dedicated folder.
    conversation = await one(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=?', (result['conversationUuid'],))
    folder = await one(e.db.conn, 'SELECT * FROM web_conversation_folders WHERE folder_uuid=?', (conversation['folder_uuid'],))
    assert folder['name'] == '实例部署' and conversation['title'] == '部署 · 实例丙' and conversation['origin_kind'] == 'deployment'
    raw = json.dumps(await many(e.db.conn, 'SELECT * FROM managed_instances'))
    for value in ('account-fixture-password', stored['managementKey'], stored['businessKey']):
        assert value not in raw and value not in json.dumps(item)
    second = form(e, name='实例甲', account={'username': 'alpha', 'password': 'another-fixture-pass'})
    second.pop('managementUrl')
    await e.instances.save(123, second)
    assert (await many(e.db.conn, "SELECT public_json FROM managed_instances WHERE public_json LIKE '%\"port\": 18971%' OR public_json LIKE '%\"port\":18971%'"))


async def test_model_choice_and_later(env):
    e = env
    item, credentials = await create(e, models={'mode': 'later'})
    assert credentials['initialConfig'] == {}
    with pytest.raises(InstanceError, match='primary_model_not_copied'):
        await e.instances.save(123, form(e, account={'username': 'x', 'password': 'another-fixture-pass'}, workspacePath=str(e.tmp / 'x'),
                                         models={'mode': 'copy', 'primary': 'missing/model'}))
    options = e.instances.model_options()
    assert options['primary'] == 'openai/gpt' and options['providers'][0]['models'] == ['gpt', 'cheap']
    assert 'apiKey' not in json.dumps(options)


async def test_folder_is_reused_and_recreated(env):
    e = env
    first = (await e.instances.folder(123))['folderUuid']
    assert (await e.instances.folder(123))['folderUuid'] == first
    await e.db.conn.execute('DELETE FROM web_conversation_folders WHERE folder_uuid=?', (first,))
    await e.db.conn.commit()
    second = (await e.instances.folder(123))['folderUuid']
    assert second != first
    assert (await one(e.db.conn, 'SELECT name FROM web_conversation_folders WHERE folder_uuid=?', (second,)))['name'] == '实例部署'


@pytest.mark.parametrize('value', ['https://u:p@example.com', 'https://a.example/path', 'https://a.example?x=1', 'http://169.254.169.254', 'http://0.0.0.0', 'https://a.example\nX:y', 'https://a.example;evil'])
def test_origin_rejects_unsafe_targets(value):
    with pytest.raises(ValueError):
        origin(value)


@pytest.mark.parametrize('path', ['/api/auth/session', '/api/auth/password/login', '/api/instances', '//evil/api/settings', 'https://evil/api/settings', '/api/settings/../../instances', '/api/settings/%2e%2e/instances', '/api/settings%3f@evil', '/api/settings\\x', '/api/system/restart', '/api/system/update', '/api/managed/v1/status?host=evil', '/api/managed/v1/status#x'])
def test_closed_relay_rejects_bypasses(path):
    with pytest.raises(InstanceError):
        relay_path('GET', path)


def test_form_defaults():
    # Short passwords are allowed; the UI only advises.
    value = Form.model_validate({'name': 'x', 'serverId': 'local', 'account': {'username': 'Family Name', 'password': 'abc'}}).model_dump()
    assert value['workspacePath'] == '/opt/openbear/instances/family-name' and value['port'] == 0 and value['managementUrl'] == ''


async def test_verify_uses_instance_and_gateway_route(env):
    e = env
    item, credentials = await create(e)
    verified = await e.instances.verify(123, item['id'])
    assert verified['verification']['ok'] and verified['verification']['publicRoute']
    assert any(call[2].get('Authorization') == 'Bearer '+credentials['managementKey'] for call in e.child.calls)
    assert any(call[1] == '/api/gateway/v1/probe' and call[2].get('X-OpenBear-Gateway-Key') == credentials['businessKey'] for call in e.child.calls)
    e.child.reflect = True
    relayed = await e.instances.request(123, item['id'], {'method': 'PATCH', 'path': '/api/managed/v1/config', 'body': {'agent': {'maxRetries': 2}}})
    assert credentials['managementKey'] not in json.dumps(relayed)
    e.child.redirect = 'https://never-request-this.example/'
    with pytest.raises(InstanceError, match='redirect'):
        await e.instances.request(123, item['id'], {'method': 'GET', 'path': '/api/settings'})
    e.child.redirect = ''
    e.child.mounted = False
    assert not (await e.instances.verify(123, item['id']))['verification']['ok']
    e.child.mounted, e.child.health = True, 'degraded'
    degraded = await e.instances.verify(123, item['id'])
    assert not degraded['verification']['ok'] and degraded['item']['healthStatus'] == 'degraded'
    e.gateway.fail = True
    e.child.health = 'healthy'
    assert not (await e.instances.verify(123, item['id']))['verification']['publicRoute']
    e.gateway.fail = False
    await e.child_server.close()
    value = await e.instances.verify(123, item['id'])
    assert not value['verification']['ok'] and value['item']['status'] == 'unreachable'


async def test_login_events_are_pulled_from_gateway(env):
    e = env
    item, _ = await create(e)
    now = __import__('time').time_ns()//1000000
    e.gateway.events = [{'seq': 1, 'eventId': 'a', 'instanceId': item['id'], 'type': 'login_failure', 'atMs': now, 'data': {'ip': '192.168.1.23'}},
                        {'seq': 2, 'eventId': 'b', 'instanceId': item['id'], 'type': 'login_success', 'atMs': now+1, 'data': {'ip': '<script>'}},
                        {'seq': 3, 'eventId': 'c', 'instanceId': 'unknown', 'type': 'login_success', 'atMs': now, 'data': {}}]
    await e.instances.gateway.pull_events()
    events = (await e.instances.events(123, item['id']))['items']
    assert [x['type'] for x in events] == ['login_success', 'login_failure']
    assert events[1]['data'] == {'ip': '192.168.1.23'} and events[0]['data'] == {}
    notes = [r['text'] for r in await many(e.db.conn, 'SELECT text FROM instance_notifications ORDER BY created_ms')]
    assert '实例「实例丙」登录失败（192.168.1.23）' in notes
    # Acknowledged sequence is not requested again; replays never duplicate.
    assert (await e.instances.gateway.config())[0]['eventSeq'] == 3
    e.gateway.events = [{'seq': 2, 'eventId': 'b', 'instanceId': item['id'], 'type': 'login_success', 'atMs': now+1, 'data': {}}]
    await e.instances.gateway.pull_events()
    assert len((await e.instances.events(123, item['id']))['items']) == 2


async def test_account_change_syncs_automatically_and_never_replays_password(env):
    e = env
    item, _ = await create(e)
    first = e.gateway.bodies[-1]
    assert first['password'] == 'account-fixture-password' and first['upstream'] == e.child_url
    assert 'reportUrl' not in first and 'reportToken' not in first
    await e.instances.save(123, {'account': {'password': 'changed-fixture-pass'}}, item['id'])
    assert e.gateway.bodies[-1]['password'] == 'changed-fixture-pass'
    await e.instances.save(123, {'name': '改名'}, item['id'])
    stored = e.instances.vault.open((await e.instances.row(123, item['id']))['protected'])
    assert 'changed-fixture-pass' not in json.dumps(stored)
    e.gateway.fail = True
    result = await e.instances.save(123, {'account': {'password': 'pending-fixture-pass'}}, item['id'])
    assert result['item']['gateway']['status'] == 'pending_sync'
    e.gateway.fail = False
    from app.instances.monitor import gateway_upkeep
    await gateway_upkeep(e.instances)
    assert e.gateway.bodies[-1]['password'] == 'pending-fixture-pass'
    assert (await e.instances.get(123, item['id']))['item']['gateway']['status'] == 'synced'


async def test_archive_waits_for_gateway_revocation(env):
    e = env
    item, _ = await create(e)
    e.gateway.fail = True
    result = await e.instances.archive(123, item['id'])
    assert result['item']['archivePending'] and result['item']['status'] != 'archived'
    e.gateway.fail = False
    from app.instances.monitor import gateway_upkeep
    await gateway_upkeep(e.instances)
    assert (await e.instances.get(123, item['id']))['item']['status'] == 'archived'
    assert e.gateway.routes[item['id']]['body']['enabled'] is False


async def test_actual_started_deployment_report_and_private_material(env):
    e = env
    item, credentials = await create(e)
    secret = credentials['managementKey']
    backend = model(e, [tool('status', {}), tool('report', {'revision': 1, 'outcome': 'completed', 'summary': 'authenticated child verified'}),
        [StreamEvent(kind='content', text='key='+secret[:20]), StreamEvent(kind='content', text=secret[20:]), StreamEvent(kind='finish', finish_reason='stop')]])
    accepted, op = await run(e, item, instructions='Execute only this isolated child fixture.')
    assert op['status'] == 'completed', op
    upstream = json.dumps(backend.seen_convos, default=str)
    assert secret in upstream and 'account-fixture-password' not in upstream
    # status gives the model its concrete target.
    assert '"managementUrl": "' + e.child_url in upstream.replace('\\"', '"') and 'child-workspace' in upstream
    for table in ('messages', 'web_operations', 'web_event_frames', 'managed_instances', 'instance_operations', 'web_reference_bundles'):
        public = json.dumps(await many(e.db.conn, 'SELECT * FROM '+table), default=str)
        assert secret not in public, table
    assert (await e.instances.get(123, item['id']))['item']['status'] == 'ready'


async def test_no_report_normal_and_aborted_are_not_success_or_redeployed(env):
    e = env
    item, _ = await create(e)
    accepted, op = await run(e, item)
    assert op['status'] == 'needs_verification' and op['error'] == 'no_report'
    calls = e.backend.calls
    await e.instances.close()
    e.instances = InstanceService(e.db, e.server)
    e.server.instances = e.instances
    await e.instances.start()
    assert e.backend.calls == calls and not e.instances.tasks
    await e.instances.recovery_task
    op = await one(e.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (op['id'],))
    assert op['status'] == 'needs_verification' and op['error'] == 'no_report'


async def test_revision_late_reports_and_fork_source_cannot_grant_capability(env):
    e = env
    item, _ = await create(e)
    gate, reached = asyncio.Event(), asyncio.Event()
    captured = []

    async def hold(args):
        captured.append(dataclasses.replace(current_tool_context()))
        reached.set()
        await gate.wait()
        return 'continued'
    e.server.tools.add('HoldFixture', 'fixture', {'type': 'object'}, hold)
    model(e, [[StreamEvent(kind='tool_call', tool_calls=[ToolCall('hold', 'HoldFixture', '{}')]), StreamEvent(kind='finish', finish_reason='tool_calls')],
              [StreamEvent(kind='content', text='done'), StreamEvent(kind='finish', finish_reason='stop')]])
    accepted = await e.instances.start_operation(123, item['id'], 'deployment', {})
    await asyncio.wait_for(reached.wait(), 5)
    ctx = captured[0]
    reports = await asyncio.gather(*(e.instances.report(ctx, {'revision': 1, 'outcome': 'completed'}) for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(x, InstanceError) for x in reports) == 1
    with pytest.raises(InstanceError):
        await e.instances.report(dataclasses.replace(ctx, agent_session_uuid='child'), {'revision': 1, 'outcome': 'completed'})
    gate.set()
    await asyncio.gather(*list(e.instances.tasks.values()))
    original = await one(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=?', (accepted['conversationUuid'],))
    copied = await e.server._create_web_conversation(123, origin_kind='deployment', origin_ref=accepted['operation']['id'])
    assert await e.instances.prepare_turn(copied, 'forged') is None
    model(e, [tool('report', {'revision': 2, 'outcome': 'completed'}), [StreamEvent(kind='content', text='repair complete'), StreamEvent(kind='finish', finish_reason='stop')]])
    live = e.server._live_for(original)
    root = str(uuid.uuid4())
    await live.publish({'type': 'accepted', 'turnUuid': root, 'runUuid': root})
    await e.server._run_web_turn(original['internal_chat_id'], '请仅重新核验并报告，不重新部署。', _WebStreamRenderer(live=live), conversation=original, root_turn_uuid=root)
    op = await one(e.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (accepted['operation']['id'],))
    assert op['revision'] == 2 and op['status'] == 'completed'


async def test_reference_copy_and_context_inspection_no_authority(env):
    e = env
    item, credentials = await create(e)
    accepted, op = await run(e, item)
    store = e.instances.vault.references
    message = {'role': 'user', 'content': 'material', BUNDLE_FIELD: [op['bundle']]}
    assert credentials['managementKey'] not in json.dumps(await store.overlay([message], conversation_uuid=op['conversation_uuid']))
    token = _REFERENCE_AUTH.set((op['conversation_uuid'], op['id'], asyncio.current_task(), op['bundle']))
    try:
        assert credentials['managementKey'] in json.dumps(await store.overlay([message], conversation_uuid=op['conversation_uuid']))
    finally:
        _REFERENCE_AUTH.reset(token)
    cookie = 'openbear_web_session='+await _login_cookie(e)
    response = await e.client.post('/api/conversations/'+op['conversation_uuid']+'/duplicate', headers={'Cookie': cookie}, json={})
    assert response.status == 200
    copy = (await many(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid!=? AND origin_kind!=?', (op['conversation_uuid'], 'user')) or
            await many(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid!=?', (op['conversation_uuid'],)))[0]
    assert await e.instances.prepare_turn(copy, str(uuid.uuid4())) is None


async def test_tool_changes_from_automatic_turns_follow_their_task(env):
    from tests.test_servers import automatic_turn
    e = env
    item, _ = await create(e)
    for index, kind in enumerate(('cron', 'webhook')):
        refused = await automatic_turn(e.db, 123, 9100 + index, kind, allowed=False)
        assert (await dispatch(e.instances, 'status', {'instanceId': item['id']}, refused))['item']['id'] == item['id']
        with pytest.raises(InstanceError, match='automatic_mutation_forbidden'):
            await dispatch(e.instances, 'archive', {'instanceId': item['id']}, refused)
    allowed = await automatic_turn(e.db, 123, 9110, 'webhook', allowed=True)
    assert (await dispatch(e.instances, 'archive', {'instanceId': item['id']}, allowed))['item']['status'] == 'archived'
    assert 'allowServerChanges' in (await dispatch(e.instances, 'describe', {}, allowed))['rules']


async def test_tool_runs_without_user_confirmation_and_secret_delivery_material(env, monkeypatch):
    e = env
    # The real source context's contents are under test, not gzip level 9:
    # write it with the fastest level (still a valid .tar.gz).
    original_open = tarfile.open

    def fast_gzip_open(*args, **kwargs):
        if kwargs.get('mode') == 'w:gz':
            kwargs.setdefault('compresslevel', 1)
        return original_open(*args, **kwargs)
    monkeypatch.setattr(tarfile, 'open', fast_gzip_open)
    item, credentials = await create(e)
    conversation = await e.server._create_web_conversation(123)
    asked = []

    async def ask(payload):
        asked.append(payload)
        return {'confirmed': False}
    ctx = ToolRuntimeContext(chat_id=conversation['internal_chat_id'], conversation_uuid=conversation['conversation_uuid'], web_confirm=ask)
    automatic = ToolRuntimeContext(chat_id=conversation['internal_chat_id'], conversation_uuid=conversation['conversation_uuid'], source='cron')
    with pytest.raises(InstanceError, match='automatic_mutation_forbidden'):
        await dispatch(e.instances, 'request', {'instanceId': item['id'], 'method': 'PATCH', 'path': '/api/settings', 'body': {}}, automatic)
    row = await e.instances.row(123, item['id'])
    # The user is never asked to judge instance operations.
    result = await dispatch(e.instances, 'archive', {'instanceId': item['id']}, ctx)
    assert result['item']['status'] == 'archived' and not asked
    rules = (await dispatch(e.instances, 'describe', {}, ctx))['rules']
    assert 'without asking the user' in rules and 'Never delete' in rules
    # Real sources with a built web/dist, plus private files that must never ship
    # (a source checkout has no web/dist; a dev tree has data and config).
    real, root = Path(remote.ROOT), e.tmp / 'source-root'
    for name in ('Dockerfile', 'pyproject.toml', 'uv.lock', 'README.md', 'scripts/container-entrypoint.sh'):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real / name, root / name)
    for name in ('app', 'prompts', 'benchmark-package'):
        shutil.copytree(real / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name, text in {'web/dist/index.html': '<!doctype html>', 'app/__pycache__/decoy.pyc': 'x', 'data/openbear.db': 'x',
                       'workspace/notes.md': 'x', 'openbear.json': '{}'}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    monkeypatch.setattr(remote, 'ROOT', root)
    command, payload, directory, image = deployment_archive(row, credentials, e.instances.public(row), {})
    assert credentials['managementKey'] not in command and image.startswith('openbear:')
    assert directory == str(e.tmp/'.deploy-child-workspace')
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        names = set(archive.getnames())
        compose = json.load(archive.extractfile('compose.json'))
        child = compose['services']['openbear']
        assert child['environment']['OPENBEAR_MANAGEMENT_KEY'] == credentials['managementKey']
        assert child['environment']['OPENBEAR_GATEWAY_KEY'] == credentials['businessKey']
        assert not {'OPENBEAR_REPORT_TOKEN', 'OPENBEAR_CONTROLLER_URL'} & set(child['environment'])
        assert child['ports'] == ['127.0.0.1:18970:18961']
        assert compose['services']['openbear']['container_name'] == 'openbear-child-workspace'
        assert child['volumes'] == [{'type': 'bind', 'source': str(e.tmp/'child-workspace'), 'target': '/workspace'}]
        assert 'docker.sock' not in json.dumps(compose)
        assert {'source.tar.gz', 'bootstrap.json'} <= names
        with tarfile.open(fileobj=archive.extractfile('source.tar.gz')) as source:
            files = set(source.getnames())
            assert 'Dockerfile' in files and any(f.startswith('web/dist/') for f in files)
            assert 'benchmark-package/ip.check.place/v0.1.0/main/ref/cookies.txt' in files
            assert 'benchmark-package/geekbench5/v0.1.0/manifest.json' in files
            assert not any(f.startswith(('data/', 'workspace/')) or 'openbear.json' in f or '__pycache__' in f for f in files)
    registry = e.server.tools
    assert 'InstanceControl' in registry.names(scope='main') and 'InstanceControl' not in registry.names(scope='agent')
    token = _AUDIT.set(Scrubber([credentials['managementKey']]))
    try:
        assert credentials['managementKey'] not in redact_tool_arguments_for_audit('Bash', credentials['managementKey'])
    finally:
        _AUDIT.reset(token)


async def test_delivery_preserves_existing_data_directory(env):
    e = env
    item, credentials = await create(e)
    row = await e.instances.row(123, item['id'])
    command, payload, _, _ = deployment_archive(row, credentials, e.instances.public(row), {'source': False})

    async def execute():
        process = await asyncio.create_subprocess_exec('/bin/sh', '-c', command, cwd=e.tmp, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate(payload)
        assert process.returncode == 0, (out, err)
    await execute()
    config = e.tmp/'child-workspace/openbear.json'
    content = json.loads(config.read_text())
    assert content['models']['providers']['openai']['apiKey'] == 'k'
    assert content['web'] == {'enabled': True, 'host': '0.0.0.0', 'port': 18961}
    assert config.stat().st_mode & 0o077 == 0
    config.write_text('{"existing":"must survive"}')
    await execute()
    assert json.loads(config.read_text()) == {'existing': 'must survive'}


async def test_model_delivers_executes_and_reports_actual_started_fixture(env, monkeypatch):
    e = env
    e.execute = True
    item, _ = await create(e)
    e.child.ready = False
    original = e.instances.servers.run

    async def starting(target, command, **kwargs):
        result = await original(target, command, **kwargs)
        if command == 'printf child-started':
            e.child.ready = True
        return result
    monkeypatch.setattr(e.instances.servers, 'run', starting)
    model(e, [tool('deliver', {'revision': 1, 'source': False}),
              tool('remote', {'revision': 1, 'command': 'printf child-started'}),
              tool('report', {'revision': 1, 'outcome': 'completed', 'summary': 'Executed fixture startup.'}),
              [StreamEvent(kind='content', text='fixture ready'), StreamEvent(kind='finish', finish_reason='stop')]])
    accepted, operation = await run(e, item)
    assert operation['status'] == 'completed'
    assert e.commands[-1] == ('local', 'printf child-started')
    assert (e.tmp/'child-workspace/openbear.json').exists()


async def test_health_transitions_notify_once(env):
    from app.instances.monitor import once
    e = env
    item, _ = await create(e)
    await e.instances.verify(123, item['id'])
    await once(e.instances, force=True)
    e.child.health = 'degraded'
    await once(e.instances, force=True)
    await once(e.instances, force=True)
    e.child.health = 'healthy'
    await once(e.instances, force=True)
    texts = [r['text'] for r in await many(e.db.conn, "SELECT text FROM instance_notifications WHERE id LIKE 'health:%' ORDER BY created_ms")]
    assert texts == ['实例「实例丙」运行异常', '实例「实例丙」已恢复正常']
    kinds = [x['type'] for x in (await e.instances.events(123, item['id']))['items']]
    assert kinds == ['recovery', 'health']


async def test_aborted_real_model_run_and_new_maintenance_supersedes_old(env):
    e = env
    item, _ = await create(e)
    started = asyncio.Event()

    class WaitingBackend(FakeStreamBackend):
        async def stream(self, messages, **options):
            self.calls += 1
            started.set()
            await asyncio.Event().wait()
            yield StreamEvent(kind='finish', finish_reason='stop')
    e.server.llm_factory = FakeRunFactory(WaitingBackend(), context_window=128000)
    accepted = await e.instances.start_operation(123, item['id'], 'deployment', {})
    await asyncio.wait_for(started.wait(), 5)
    task = list(e.instances.tasks.values())[0]
    task.cancel()
    await task
    op = await one(e.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (accepted['operation']['id'],))
    assert op['status'] == 'interrupted'
    with pytest.raises(InstanceError):
        await e.instances.start_operation(123, item['id'], 'maintenance', {})
    model(e, [tool('report', {'revision': 1, 'outcome': 'completed'}), [StreamEvent(kind='content', text='verified'), StreamEvent(kind='finish', finish_reason='stop')]])
    maintenance, second = await run(e, item, 'maintenance', instructions='Only recheck. No rebuild.')
    assert second['status'] == 'completed' and second['kind'] == 'maintenance'
    conversation = await one(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=?', (second['conversation_uuid'],))
    first = await one(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=?', (op['conversation_uuid'],))
    assert conversation['title'] == '维护 · 实例丙' and conversation['folder_uuid'] == first['folder_uuid']


async def test_real_telegram_outbox_deduplicates_domain_replay(env):
    from app.web_task_telegram import WebTaskTelegramNotifier
    from tests.test_web_admin import FakeBot
    e = env
    item, _ = await create(e)
    model(e, [tool('report', {'revision': 1, 'outcome': 'completed'}), [StreamEvent(kind='content', text='done'), StreamEvent(kind='finish', finish_reason='stop')]])
    await run(e, item)
    e.server.web_task_telegram = WebTaskTelegramNotifier(e.server.config, e.db, FakeBot())
    for _ in range(2):
        await e.db.conn.execute("UPDATE instance_notifications SET status='pending',next_ms=0")
        await e.db.conn.commit()
        await e.instances.notify_pending()
    deliveries = await many(e.db.conn, "SELECT * FROM web_tg_notification_outbox WHERE root_turn_uuid LIKE 'instance-notification:%'")
    assert len(deliveries) == 1


async def test_chunked_management_file_upload(env):
    e = env
    item, _ = await create(e)
    body = json.dumps({'method': 'PUT', 'path': '/api/managed/v1/files/skills/large/SKILL.md', 'body': {'content': 'x'*300000}}).encode()

    async def chunks():
        for offset in range(0, len(body), 10000):
            yield body[offset:offset+10000]
            await asyncio.sleep(0)
    response = await e.http.post('/api/instances/'+item['id']+'/request', headers={'X-Test-Owner': '123', 'Content-Type': 'application/json'}, data=chunks())
    assert response.status == 200
    assert len((await response.json())['data']['body']['content']) == 300000


async def test_encoded_opaque_model_id_keeps_one_aiohttp_route_parameter(env):
    e = env
    item, _ = await create(e)
    observed = []

    async def actual_router(request):
        observed.append((dict(request.match_info), request.raw_path, request.path))
        return web.json_response({'ok': True})
    app = web.Application()
    app.router.add_patch('/api/channels/{name}/models/{model_id}', actual_router)
    async with TestServer(app) as child:
        await e.instances.save(123, {'managementUrl': str(child.make_url('')).rstrip('/')}, item['id'])
        response = await e.http.post('/api/instances/'+item['id']+'/request', headers={'X-Test-Owner': '123'}, json={
            'method': 'PATCH', 'path': '/api/channels/中文/models/vendor%2f模型%3Av1%2Bquant', 'body': {'name': 'updated'}})
        assert response.status == 200 and (await response.json())['status'] == 200
    assert observed[0][0] == {'name': '中文', 'model_id': 'vendor/模型:v1+quant'}
    assert '%2F' in observed[0][1] and '%252F' not in observed[0][1]


async def test_turn_binding_uses_the_committed_revision_not_a_stale_read(env, monkeypatch):
    """The read connection may still show the pre-bind row; the binding must not."""
    e = env
    item, _ = await create(e)
    model(e, [tool('report', {'revision': 1, 'outcome': 'completed'}), [StreamEvent(kind='content', text='done'), StreamEvent(kind='finish', finish_reason='stop')]])
    import app.instances.service as service_module
    real_one = service_module.one

    async def lagging_one(conn, sql, args=()):
        row = await real_one(conn, sql, args)
        # The first running row read from the reader is the just-bound one; serve
        # it as the reader's older snapshot (revision before the bind).
        if conn is e.db.conn and row and row.get('status') == 'running' and sql.startswith('SELECT * FROM instance_operations WHERE id=?') and not lagging_one.done:
            lagging_one.done = True
            return {**row, 'revision': row['revision'] - 1}
        return row
    lagging_one.done = False
    monkeypatch.setattr(service_module, 'one', lagging_one)
    accepted, op = await run(e, item)
    monkeypatch.setattr(service_module, 'one', real_one)
    op = await one(e.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (op['id'],))
    assert op['status'] == 'completed', op
    conversation = await one(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=?', (accepted['conversationUuid'],))
    assert 'stale_operation' not in (conversation['last_error'] or '')


async def test_deployment_message_is_a_card_and_stays_out_of_recent(env):
    from app.instances.presentation import enrich_operations
    e = env
    item, _ = await create(e)
    model(e, [tool('report', {'revision': 1, 'outcome': 'completed'}), [StreamEvent(kind='content', text='done'), StreamEvent(kind='finish', finish_reason='stop')]])
    accepted, op = await run(e, item)
    stored = await one(e.db.conn, "SELECT * FROM web_operations WHERE conversation_uuid=? AND op_type='user_message'", (op['conversation_uuid'],))
    payload = json.loads(stored['payload_json'])
    assert stored['source'] == 'instance' and payload['eventTriggered'] is True
    assert payload['instanceCard']['name'] == '实例丙' and payload['instanceCard']['kind'] == 'deployment'
    assert payload['instanceCard']['workspacePath'] == str(e.tmp/'child-workspace')
    replies = await many(e.db.conn, "SELECT payload_json FROM web_operations WHERE conversation_uuid=? AND op_type='assistant_message'", (op['conversation_uuid'],))
    assert replies and all(json.loads(r['payload_json']).get('eventTriggered') for r in replies)
    conversation = await one(e.db.conn, 'SELECT last_interaction_at_ms FROM web_conversations WHERE conversation_uuid=?', (op['conversation_uuid'],))
    assert not conversation['last_interaction_at_ms']
    # A message stored before cards existed is presented as a card too.
    legacy = [{'opId': 'msg:' + op['id'], 'opType': 'user_message', 'turnId': op['root'], 'source': 'deployment', 'payload': {'text': 'x'}}]
    await enrich_operations(e.db.conn, legacy, op['conversation_uuid'])
    assert legacy[0]['source'] == 'instance' and legacy[0]['payload']['instanceCard']['operationId'] == op['id']
    other = [{'opId': 'msg:human', 'opType': 'user_message', 'turnId': op['root'], 'payload': {'text': 'x'}}]
    await enrich_operations(e.db.conn, other, op['conversation_uuid'])
    assert 'instanceCard' not in other[0]['payload']


async def test_instance_and_gateway_changes_bring_the_mesh_rules_up_to_date(env, monkeypatch):
    """Instances on a mesh address are let through by the mesh's automatic rules: saving one (placed,
    another port), archiving it and the gateway's enable or disable each tell the mesh, which reconciles."""
    e = env
    told = []
    monkeypatch.setattr(e.server, 'servers', SimpleNamespace(mesh=SimpleNamespace(instances_changed=told.append)), raising=False)
    item, _ = await create(e)
    assert told == [123]
    await e.instances.save(123, {'port': item['port'] + 1}, item['id'])
    assert told == [123, 123]
    await e.instances.archive(123, item['id'])  # through the gateway's revocation
    assert (await e.instances.get(123, item['id']))['item']['status'] == 'archived' and told == [123, 123, 123]
    ran = []

    async def no_run(target, command, **kw):
        ran.append(command)
        return {'ok': True, 'stdout': '', 'stderr': '', 'exitCode': 0, 'knownHosts': ''}
    monkeypatch.setattr(e.instances.servers, 'run', no_run)
    await e.instances.gateway.disable(123)
    assert told[-1] is None and len(ran) == 1 and 'systemctl disable --now' in ran[0]
