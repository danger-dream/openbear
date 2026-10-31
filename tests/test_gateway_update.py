"""Gateway program updates: when one is due, the one-click and automatic paths, notices and the tool."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from app.instances.contracts import InstanceError
from app.instances.notifications import GATEWAY
from app.tools.base import ToolRuntimeContext
from app.tools.instances import dispatch
from app.webhooks.repository import many
from tests.test_instances import env  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

BUILDS = {'amd64': 'a' * 64, 'arm64': 'b' * 64}
OLD = 'c' * 64
OWNER = {'X-Test-Owner': '123'}


@pytest.fixture
async def gw(env, monkeypatch):  # noqa: F811
    """The fixture gateway runs OLD; installing it makes it report the bundled amd64 build."""
    e, gateway = env, env.instances.gateway
    # The instance monitor's first pass refreshes the gateway (get()) once, right after start;
    # under load it can land in the middle of a test and write back the config it read before.
    # Let it finish first: the next one is a minute away.
    if e.instances.monitor_task is not None:
        async with asyncio.timeout(10):
            while (await gateway.config())[0]['checkedAtMs'] is None and not e.instances.monitor_task.done():
                await asyncio.sleep(0.01)
        async with gateway.service.lock('gateway-config'):
            pass
    monkeypatch.setattr('app.instances.gateway.bundled_builds', lambda: dict(BUILDS))
    state = SimpleNamespace(build=OLD, installs=[], fail='')
    original = gateway.call

    async def call(value, private, method, path, body=None):
        data = await original(value, private, method, path, body)
        if path == '/control/v1/status' and state.build is not None:
            data['build'] = state.build
        return data

    async def install(target, port, public_url, key, control_port=None):
        state.installs.append({'local': bool(target.get('local')), 'port': port, 'publicUrl': public_url, 'key': key, 'controlPort': control_port})
        if state.fail:
            raise InstanceError(state.fail, 502)
        state.build = BUILDS['amd64']

    async def control_port(owner, server_id, port):
        return urlparse(e.gateway.url).port  # The fixture gateway answers on loopback.
    monkeypatch.setattr(gateway, 'call', call)
    monkeypatch.setattr(gateway, '_install', install)
    monkeypatch.setattr(gateway, '_control_port', control_port)
    state.gateway = gateway
    return state


async def set_auto(gateway, on):
    # The same lock as get(): the monitor's first pass refreshes the gateway right after start,
    # and a refresh that read the config before this store would write autoUpdate back.
    async with gateway.service.lock('gateway-config'):
        value, private = await gateway.config()
        await gateway.store({**value, 'autoUpdate': on}, private)


async def notices(e):
    return [r['text'] for r in await many(e.db.conn, 'SELECT text FROM instance_notifications WHERE instance_id=? ORDER BY rowid', (GATEWAY,))]


async def test_when_an_update_is_due(env, monkeypatch):  # noqa: F811
    gateway = env.instances.gateway
    monkeypatch.setattr('app.instances.gateway.bundled_builds', lambda: dict(BUILDS))
    ready = SimpleNamespace(value=False)

    async def http_ready(owner, identity):
        return ready.value
    monkeypatch.setattr(gateway.servers, 'http_ready', http_ready)

    async def check(status, private, server='local'):
        value = {'serverId': server}
        await gateway._check_build(value, private, {'ok': True, **status})
        return value['build'], value['updateReasons'], value['updateAvailable']
    loopback = {'controlPort': 18963, 'owner': 123}
    assert await check({'build': BUILDS['arm64']}, loopback) == (BUILDS['arm64'], [], False)
    assert await check({'build': OLD}, loopback) == (OLD, ['build_outdated'], True)
    # Gateways from before build reporting: missing or not a string.
    assert await check({}, loopback) == ('', ['build_unreported'], True)
    assert await check({'build': 7}, loopback) == ('', ['build_unreported'], True)
    # Control API still on the public port where enable() would move it to loopback.
    public = {'controlUrl': 'http://198.51.100.4:18962', 'owner': 123}
    assert await check({'build': BUILDS['amd64']}, public) == (BUILDS['amd64'], ['control_not_loopback'], True)
    assert (await check({'build': OLD}, public))[1] == ['build_outdated', 'control_not_loopback']
    assert await check({'build': BUILDS['amd64']}, public, 'srv-ssh') == (BUILDS['amd64'], [], False)  # SSH only: stays
    ready.value = True
    assert (await check({'build': BUILDS['amd64']}, public, 'srv-node'))[1] == ['control_not_loopback']
    # Without bundled programs there is nothing to install.
    monkeypatch.setattr('app.instances.gateway.bundled_builds', lambda: {})
    assert await check({'build': OLD}, loopback) == (OLD, [], False)


async def test_bundled_builds_are_the_shipped_programs(tmp_path, monkeypatch):
    import hashlib

    from app.instances import gateway as module
    (tmp_path / 'gateway/bin').mkdir(parents=True)
    program = tmp_path / 'gateway/bin/openbear-gateway-linux-arm64'
    program.write_bytes(b'one')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, '_BUILDS', {})
    assert module.bundled_builds() == {'arm64': hashlib.sha256(b'one').hexdigest()}
    program.write_bytes(b'second')  # A rebuilt program is hashed again.
    assert module.bundled_builds() == {'arm64': hashlib.sha256(b'second').hexdigest()}


async def test_one_click_update_reuses_the_current_settings_and_key(env, gw):  # noqa: F811
    e = env
    value, private = await gw.gateway.config()
    assert value['autoUpdate'] is False  # The fixture opted out; see below for the default.
    status = await (await e.http.get('/api/instance-gateway', headers=OWNER)).json()
    assert status['build'] == OLD and status['updateAvailable'] is True
    assert status['updateReasons'] == ['build_outdated', 'control_not_loopback'] and gw.gateway.update_task is None
    response = await e.http.post('/api/instance-gateway/update', headers=OWNER)
    data = await response.json()
    assert response.status == 200, data
    port = urlparse(e.gateway.url).port
    assert gw.installs == [{'local': True, 'port': 18962, 'publicUrl': e.gateway.public, 'key': private['controlKey'], 'controlPort': port}]
    assert data['build'] == BUILDS['amd64'] and data['updateAvailable'] is False and data['updateReasons'] == []
    assert data['lastUpdate']['ok'] is True and data['lastUpdate']['auto'] is False and data['connected']
    _, after = await gw.gateway.config()
    assert after['controlKey'] == private['controlKey'] and after['controlPort'] == port
    assert private['controlKey'] not in str(data)
    assert await notices(e) == []  # The person who clicked sees the result.


async def test_existing_installs_update_automatically_by_default_and_it_can_be_turned_off(env, gw):  # noqa: F811
    e = env
    async with gw.gateway.service.lock('gateway-config'):  # see set_auto
        value, private = await gw.gateway.config()
        await gw.gateway.store({k: v for k, v in value.items() if k != 'autoUpdate'}, private)
    assert (await gw.gateway.config())[0]['autoUpdate'] is True
    response = await e.http.put('/api/instance-gateway/auto-update', json={'autoUpdate': 'no'}, headers=OWNER)
    assert response.status == 400 and (await response.json())['error'] == 'invalid_gateway_auto_update'
    response = await e.http.put('/api/instance-gateway/auto-update', json={'autoUpdate': False}, headers=OWNER)
    assert (await response.json())['autoUpdate'] is False
    result = await gw.gateway.get()
    assert result['updateAvailable'] is True and gw.gateway.update_task is None and gw.installs == []


async def test_automatic_update_runs_once_and_notifies(env, gw):  # noqa: F811
    e = env
    await set_auto(gw.gateway, True)
    result = await gw.gateway.get()
    assert result['updateAvailable'] is True and result['updating'] is True
    await gw.gateway.update_task
    assert len(gw.installs) == 1
    value, _ = await gw.gateway.config()
    assert value['updateAvailable'] is False and value['lastUpdate']['ok'] is True and value['lastUpdate']['auto'] is True
    assert await notices(e) == [f'访问网关已自动更新（构建 {BUILDS["amd64"][:12]}），账号和登录状态都保留。']
    task = gw.gateway.update_task
    await gw.gateway.get()
    assert gw.gateway.update_task is task and len(gw.installs) == 1


async def test_failed_automatic_update_says_why_and_waits_for_restart_or_a_click(env, gw):  # noqa: F811
    e = env
    await set_auto(gw.gateway, True)
    gw.fail = 'gateway_start_failed'
    await gw.gateway.get()
    await gw.gateway.update_task
    value, _ = await gw.gateway.config()
    assert value['lastUpdate'] == {**value['lastUpdate'], 'ok': False, 'auto': True, 'error': 'gateway_start_failed'}
    assert value['updateAvailable'] is True
    texts = await notices(e)
    assert len(texts) == 1 and '自动更新失败' in texts[0] and '新版本的网关服务启动失败' in texts[0] and 'gateway_start_failed' in texts[0]
    # The same target build is not retried in a loop.
    for _ in range(3):
        await gw.gateway.get()
    assert len(gw.installs) == 1 and not gw.gateway.updating
    # A manual update still works and reports through its own response.
    gw.fail = ''
    response = await e.http.post('/api/instance-gateway/update', headers=OWNER)
    assert response.status == 200 and len(gw.installs) == 2 and len(await notices(e)) == 1
    # A failed manual update answers with its error.
    gw.fail, gw.build = 'gateway_install_failed', OLD
    response = await e.http.post('/api/instance-gateway/update', headers=OWNER)
    assert response.status == 502 and (await response.json())['error'] == 'gateway_install_failed'
    assert (await gw.gateway.config())[0]['lastUpdate']['error'] == 'gateway_install_failed'


async def test_a_restarted_controller_tries_again(env, gw):  # noqa: F811
    await set_auto(gw.gateway, True)
    gw.fail = 'gateway_start_failed'
    await gw.gateway.get()
    await gw.gateway.update_task
    gw.gateway.update_tried.clear()  # What a new controller process starts with.
    gw.fail = ''
    await gw.gateway.get()
    await gw.gateway.update_task
    assert len(gw.installs) == 2 and (await gw.gateway.config())[0]['lastUpdate']['ok'] is True


async def test_unreachable_server_is_skipped_and_tried_later(env, gw, monkeypatch):  # noqa: F811
    e = env
    await set_auto(gw.gateway, True)
    reachable = SimpleNamespace(value=False)

    async def server_reachable(owner, server_id):
        return reachable.value
    monkeypatch.setattr(gw.gateway, '_server_reachable', server_reachable)
    await gw.gateway.get()
    await gw.gateway.update_task
    assert gw.installs == [] and await notices(e) == [] and gw.gateway.update_tried == set()
    reachable.value = True
    await gw.gateway.get()
    await gw.gateway.update_task
    assert len(gw.installs) == 1


async def test_gateway_status_failure_does_not_update(env, gw):  # noqa: F811
    await set_auto(gw.gateway, True)
    env.gateway.fail = True
    result = await gw.gateway.get()
    assert result['status'] == 'unreachable' and gw.gateway.update_task is None


async def test_server_reachability(env, monkeypatch):  # noqa: F811
    gateway = env.instances.gateway
    replies = {}

    async def target(owner, identity, *, nodes=False):
        if identity == 'offline':
            raise InstanceError('node_offline', 409)
        return {'id': identity, 'channel': replies[identity][0]}

    async def run(target, command, **kwargs):
        assert command == 'true' and kwargs['source'] == 'gateway'
        return {'ok': replies[target['id']][1]}
    monkeypatch.setattr(gateway.servers, 'target', target)
    monkeypatch.setattr(gateway.servers, 'run', run)
    replies.update(up=('ssh', True), down=('ssh', False), node=('node', None), here=('local', None))
    assert [await gateway._server_reachable(123, s) for s in ('up', 'down', 'node', 'here', 'offline')] == [True, False, True, True, False]


async def test_gateway_notice_reaches_telegram(env, gw):  # noqa: F811
    from app.web_task_telegram import WebTaskTelegramNotifier
    from tests.test_web_admin import FakeBot
    e = env
    await set_auto(gw.gateway, True)
    gw.fail = 'gateway_binary_missing'
    await gw.gateway.get()
    await gw.gateway.update_task
    e.server.web_task_telegram = WebTaskTelegramNotifier(e.server.config, e.db, FakeBot())
    await e.instances.notify_pending()
    rows = await many(e.db.conn, 'SELECT status FROM instance_notifications WHERE instance_id=?', (GATEWAY,))
    assert [r['status'] for r in rows] == ['enqueued']
    deliveries = await many(e.db.conn, "SELECT * FROM web_tg_notification_outbox WHERE root_turn_uuid LIKE 'instance-notification:gateway-update:%'")
    assert len(deliveries) == 1


async def test_instance_control_reports_and_updates_the_gateway(env, gw):  # noqa: F811
    e = env
    conversation = await e.server._create_web_conversation(123)
    ctx = ToolRuntimeContext(chat_id=conversation['internal_chat_id'], conversation_uuid=conversation['conversation_uuid'])
    status = await dispatch(e.instances, 'gateway_status', {}, ctx)
    assert status['build'] == OLD and status['updateAvailable'] is True and status['autoUpdate'] is False
    assert 'gateway_update' in (await dispatch(e.instances, 'describe', {}, ctx))['contracts']
    with pytest.raises(InstanceError, match='gateway_update_takes_no_params'):
        await dispatch(e.instances, 'gateway_update', {'serverId': 'elsewhere'}, ctx)
    cron = ToolRuntimeContext(chat_id=conversation['internal_chat_id'], conversation_uuid=conversation['conversation_uuid'], source='cron')
    with pytest.raises(InstanceError, match='automatic_mutation_forbidden'):
        await dispatch(e.instances, 'gateway_update', {}, cron)
    result = await dispatch(e.instances, 'gateway_update', {}, ctx)
    assert result['updateAvailable'] is False and result['lastUpdate']['ok'] is True and len(gw.installs) == 1


async def test_a_loopback_control_api_is_never_moved_public_while_the_node_is_away(env, gw, monkeypatch):  # noqa: F811
    gateway = env.instances.gateway
    async with gateway.service.lock('gateway-config'):  # see set_auto
        value, private = await gateway.config()
        await gateway.store({**value, 'serverId': 'srv-remote'}, {**private, 'controlPort': 18963, 'owner': 123})
    before = await gateway.config()

    async def target(owner, server_id, nodes=False):
        return {'id': server_id, 'host': '198.51.100.7'}  # Reached over SSH: the node is offline.

    async def http_ready(owner, identity):
        return False
    blocker = SimpleNamespace(value='node_offline')

    async def http_blocker(owner, identity):
        return blocker.value
    monkeypatch.setattr(gateway.servers, 'target', target)
    monkeypatch.setattr(gateway.servers, 'http_ready', http_ready)
    monkeypatch.setattr(gateway.servers, 'http_blocker', http_blocker)
    for attempt in (gateway.update(123), gateway.rotate_control_key(123)):
        with pytest.raises(InstanceError) as raised:
            await attempt
        assert raised.value.error == 'gateway_node_unavailable'
    # Said as what keeps the node from it, when waiting would not help.
    for blocker.value, error in (('node_upgrade_required', 'gateway_node_upgrade_required'), ('node_exec_disabled', 'gateway_node_exec_disabled')):
        with pytest.raises(InstanceError, match=error):
            await gateway.rotate_control_key(123)
    assert gw.installs == []
    value, private = await gateway.config()
    assert private == before[1] and {k: value[k] for k in ('serverId', 'port', 'publicUrl')} == {k: before[0][k] for k in ('serverId', 'port', 'publicUrl')}
    assert value['lastUpdate']['error'] == 'gateway_node_unavailable'


async def test_an_update_uses_the_settings_saved_while_it_waited(env, gw):  # noqa: F811
    import asyncio
    gateway = env.instances.gateway
    async with gateway.service.lock('gateway-config'):
        task = asyncio.create_task(gateway.update(123))
        await asyncio.sleep(0.05)
        value, private = await gateway.config()
        await gateway.store({**value, 'publicUrl': 'https://later.example'}, private)
    await task
    assert gw.installs[-1]['publicUrl'] == 'https://later.example'
    assert (await gateway.config())[0]['publicUrl'] == 'https://later.example'


async def test_a_reinstall_keeps_settings_the_controller_does_not_manage(env, monkeypatch):  # noqa: F811
    import base64
    import json
    import re

    from app.instances import gateway as gateway_module
    gateway, seen = env.instances.gateway, []
    if not (gateway_module.ROOT / 'gateway/bin/openbear-gateway-linux-amd64').is_file():
        pytest.skip('needs the built gateway program')
    current = {'listen': '0.0.0.0:1', 'controlKey': 'old', 'tlsCert': '/etc/ssl/gw.pem', 'tlsKey': '/etc/ssl/gw.key',
               'webRoot': '/srv/web', 'clientIp': {'trusted': ['10.0.0.1/32']}}

    async def run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False, source='instance'):
        seen.append(command)
        if command == 'uname -m':
            return {'ok': True, 'stdout': 'x86_64\n'}
        if command.startswith('cat '):
            return {'ok': True, 'stdout': json.dumps(current)}
        seen.append(stdin.decode())
        return {'ok': True, 'stdout': '', 'stderr': '', 'exitCode': 0}
    monkeypatch.setattr(gateway.servers, 'run', run)
    await gateway._install({'id': 'srv'}, 18962, 'https://gw.example', 'new-key', 18963)
    data = re.search(r"printf '%s' '?([A-Za-z0-9+/=]+)'? \| base64 -d > \"\$tmp/gateway.json\"", seen[-1]).group(1)
    written = json.loads(base64.b64decode(data))
    assert {k: written[k] for k in ('tlsCert', 'tlsKey', 'webRoot', 'clientIp')} == {k: current[k] for k in ('tlsCert', 'tlsKey', 'webRoot', 'clientIp')}
    assert (written['listen'], written['controlKey'], written['controlListen']) == ('0.0.0.0:18962', 'new-key', '127.0.0.1:18963')
    current = '{not json'
    await gateway._install({'id': 'srv'}, 18962, 'https://gw.example', 'new-key', 18963)
    data = re.search(r"printf '%s' '?([A-Za-z0-9+/=]+)'? \| base64 -d > \"\$tmp/gateway.json\"", seen[-1]).group(1)
    assert 'tlsCert' not in json.loads(base64.b64decode(data))
