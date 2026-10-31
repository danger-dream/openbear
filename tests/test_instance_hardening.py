"""Instance and gateway hardening: transport through nodes, placement, keys, limits and bounds."""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tarfile
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.instances import network, remote
from app.instances.contracts import Form, InstanceError, check_workspace
from app.instances.events import NOTICE_BURST, NOTICE_WINDOW_MS
from app.llm.events import StreamEvent
from app.webhooks.repository import many, one
from tests.test_instances import create, env, form, model, run  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

KEYS = {'managementKey': 'm' * 48, 'businessKey': 'b' * 48}


def now():
    return int(time.time() * 1000)


async def edit(e, identity, **public):
    """Change the stored record the way an older deployment left it."""
    row = await e.instances.row(123, identity, active=False)
    item = json.loads(row['public_json'])
    for key, value in public.items():
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value
    await e.db.conn.execute('UPDATE managed_instances SET public_json=? WHERE id=?', (json.dumps(item), identity))
    await e.db.conn.commit()


def delivered(e, identity):
    return e.db.conn.execute("UPDATE managed_instances SET status='ready' WHERE id=?", (identity,))


def files(stdin):
    with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive.getmembers()}


def node_host(e, monkeypatch, *, calls, reply=None, features=frozenset({'http.local'})):
    async def call_newer(owner, server_id, feature, method, params, *, timeout=60):
        calls.append((owner, server_id, feature, method, params))
        if isinstance(reply, Exception):
            raise reply
        return reply or {'status': 200, 'body': base64.b64encode(b'{"ok":true}').decode(), 'contentType': 'application/json'}

    async def node_channels(owner, identity=None):
        return {'srv-n': {}}
    sessions = {'srv-n': SimpleNamespace(incompatible='', caps={'exec': True}, features=features)}
    monkeypatch.setattr(e.server, 'servers', SimpleNamespace(_call_newer=call_newer, node_channels=node_channels,
                                                             hub=SimpleNamespace(sessions=sessions)), raising=False)


# ----- I1: node HTTP channel, placement, gateway control, migration -----
async def test_node_carries_http_for_instances_on_its_loopback(env, monkeypatch):
    e, calls = env, []
    node_host(e, monkeypatch, calls=calls)
    servers = e.instances.servers
    assert await servers.http_ready(123, 'srv-n') and not await servers.http_ready(123, 'local')
    assert not await servers.http_ready(123, 'srv-other')
    status, raw = await servers.local_http(123, 'srv-n', 'PUT', 18970, '/api/x', headers={'Authorization': 'Bearer k'}, body=b'{"a":1}')
    assert (status, raw) == (200, b'{"ok":true}')
    owner, server_id, feature, method, params = calls[-1]
    assert (owner, server_id, feature, method) == (123, 'srv-n', 'http.local', 'http.local')
    assert params['port'] == 18970 and params['path'] == '/api/x' and base64.b64decode(params['body']) == b'{"a":1}'
    assert params['headers'] == {'Authorization': 'Bearer k'} and params['timeoutMs'] == 20000
    row = {'owner': 123}
    # A loopback-placed instance only ever goes through its node; a legacy one does when the node can.
    placed = {'serverId': 'srv-n', 'port': 18970, 'managementUrl': 'http://127.0.0.1:18970', 'network': {'bind': ['127.0.0.1']}}
    assert await network.route(e.instances, row, placed) == {'owner': 123, 'serverId': 'srv-n', 'port': 18970}
    legacy = {'serverId': 'srv-n', 'port': 18970, 'managementUrl': 'http://198.51.100.4:18970'}
    assert (await network.route(e.instances, row, legacy))['serverId'] == 'srv-n'
    assert await network.route(e.instances, row, {**legacy, 'serverId': 'srv-ssh'}) == {'url': 'http://198.51.100.4:18970'}
    assert await network.route(e.instances, row, {**placed, 'serverId': 'srv-ssh'}) == {'owner': 123, 'serverId': 'srv-ssh', 'port': 18970}
    # An older node is not used; node failures are transport failures.
    node_host(e, monkeypatch, calls=calls, features=frozenset())
    assert not await servers.http_ready(123, 'srv-n')
    node_host(e, monkeypatch, calls=calls, reply=InstanceError('response_too_large', 502))
    with pytest.raises(network.TransportError, match='too_large'):
        await network.fetch(e.instances, {'owner': 123, 'serverId': 'srv-n', 'port': 1}, 'GET', '/')
    node_host(e, monkeypatch, calls=calls, reply={'status': '200', 'body': ''})
    with pytest.raises(network.TransportError, match='unreachable'):
        await network.fetch(e.instances, {'owner': 123, 'serverId': 'srv-n', 'port': 1}, 'GET', '/')


async def test_mesh_address_comes_from_the_applied_mesh(env, monkeypatch):
    e, stages = env, []
    doc = {'members': [{'serverId': 'srv-n', 'address': '10.88.0.2', 'removing': False},
                       {'serverId': 'srv-gone', 'address': '10.88.0.3', 'removing': True}]}

    async def load(owner, stage):
        stages.append(stage)
        return doc
    monkeypatch.setattr(e.server, 'servers', SimpleNamespace(mesh=SimpleNamespace(_load=load)), raising=False)
    servers = e.instances.servers
    assert await servers.mesh_address(123, 'srv-n') == '10.88.0.2'
    assert await servers.mesh_address(123, 'srv-gone') == '' and await servers.mesh_address(123, 'srv-x') == ''
    assert set(stages) == {'applied'}


async def test_new_instances_listen_on_loopback_or_the_mesh(env, monkeypatch):
    e = env
    svc, mesh = e.instances, {}

    async def http_ready(owner, identity):
        return identity == 'srv-n'

    async def mesh_address(owner, identity):
        return mesh.get(identity, '')

    async def get(owner, identity):
        return {'id': identity, 'name': identity, 'host': '198.51.100.4'}
    monkeypatch.setattr(svc.servers, 'http_ready', http_ready)
    monkeypatch.setattr(svc.servers, 'mesh_address', mesh_address)
    monkeypatch.setattr(svc.servers, 'get', get)
    assert await svc._placement(123, 'local', 18970) == ('http://127.0.0.1:18970', {'bind': ['127.0.0.1']})
    # Gateway elsewhere: both must be mesh members.
    with pytest.raises(InstanceError, match='instance_mesh_required'):
        await svc._placement(123, 'srv-n', 18970)
    mesh['srv-n'] = '10.88.0.2'
    with pytest.raises(InstanceError, match='instance_mesh_required'):
        await svc._placement(123, 'srv-n', 18970)
    mesh['local'] = '10.88.0.1'
    assert await svc._placement(123, 'srv-n', 18970) == ('http://10.88.0.2:18970', {'bind': ['127.0.0.1', '10.88.0.2']})
    # SSH-only servers keep server address + port.
    assert await svc._placement(123, 'srv-ssh', 18970) == ('http://198.51.100.4:18970', None)
    # Gateway on the instance's server: loopback only.
    value, private = await svc.gateway.config()
    await svc.gateway.store({**value, 'serverId': 'srv-n'}, private)
    assert await svc._placement(123, 'srv-n', 18971) == ('http://127.0.0.1:18971', {'bind': ['127.0.0.1']})
    await svc.gateway.store(value, private)
    # Deploying re-checks the mesh it was placed on.
    item = {'serverId': 'srv-n', 'network': {'bind': ['127.0.0.1', '10.88.0.2']}}
    await svc.check_placement(123, item)
    mesh['srv-n'] = '10.88.0.9'
    with pytest.raises(InstanceError, match='instance_mesh_required'):
        await svc.check_placement(123, item)


def test_compose_binds_limits_and_boot_unit():
    row, base = {'id': 'i'}, {'workspacePath': '/opt/openbear/instances/a', 'port': 18970}
    legacy = remote.compose(row, KEYS, {**base, 'managementUrl': 'http://203.0.113.5:18970'}, 'img')['services']['openbear']
    assert legacy['ports'] == ['0.0.0.0:18970:18961'] and legacy['pids_limit'] == remote.PIDS_LIMIT
    assert 'mem_limit' not in legacy and 'user' not in legacy
    item = {**base, 'managementUrl': 'http://10.88.0.2:18970', 'memoryLimitMb': 2048, 'network': {'bind': ['127.0.0.1', '10.88.0.2']}}
    placed = remote.compose(row, KEYS, item, 'img')['services']['openbear']
    assert placed['ports'] == ['127.0.0.1:18970:18961', '10.88.0.2:18970:18961'] and placed['mem_limit'] == '2048m'
    unit, command, text = remote.boot_unit(item)
    assert unit == 'openbear-instance-openbear-a.service' and f'/etc/systemd/system/{unit}' in command and 'systemctl enable' in command
    assert ' inet 10.88.0.2/' in text.decode() and 'docker start openbear-a' in text.decode() and 'WantedBy=multi-user.target' in text.decode()
    assert remote.mesh_bind({**base, 'managementUrl': 'http://127.0.0.1:1', 'network': {'bind': ['127.0.0.1']}}) == ''


def fake_docker(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    script = bin_dir / 'docker'
    script.write_text('#!/bin/sh\necho "$*" >> "$LOG"\n'
                      'case "$*" in *"--entrypoint test"*) exit "${STARTED:-0}";; *maintenance*) cat > "$CAPTURE"; exit "${MAINT:-0}";; esac\n'
                      'exit 0\n')
    script.chmod(0o755)
    return bin_dir


def test_reconfigure_maintains_the_stopped_volume_then_recreates(tmp_path):
    bin_dir = fake_docker(tmp_path)
    item = {'workspacePath': str(tmp_path / 'data' / 'openbear-a'), 'port': 18970, 'managementUrl': 'http://127.0.0.1:18970',
            'image': 'img:1', 'network': {'bind': ['127.0.0.1']}}
    payload = {'authMode': 'gateway', 'gatewayKey': 'n' * 48}
    command, stdin = remote.reconfigure({'id': 'i'}, KEYS, item, maintenance=[('set-auth', payload)])
    deploy = tmp_path / 'data' / '.deploy-openbear-a'

    def go(**extra):
        log = tmp_path / 'log'
        log.unlink(missing_ok=True)
        environ = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'LOG': str(log), 'CAPTURE': str(tmp_path / 'capture'), **extra}
        done = subprocess.run(['/bin/sh', '-c', command], input=stdin, env=environ, capture_output=True)
        return done, log.read_text().splitlines()
    done, log = go()
    assert done.returncode == 0, done.stderr
    assert [line.split()[0] for line in log] == ['info', 'run', 'stop', 'run', 'run', 'compose']
    assert 'set-auth' in log[3] and '--network none' in log[3] and log[5].endswith('compose.json up -d')
    assert json.loads((tmp_path / 'capture').read_text()) == payload
    compose = json.loads((deploy / 'compose.json').read_text())['services']['openbear']
    assert compose['ports'] == ['127.0.0.1:18970:18961'] and compose['environment']['OPENBEAR_GATEWAY_KEY'] == KEYS['businessKey']
    assert not [p for p in deploy.iterdir() if p.name.startswith('.reconfigure-')]
    # Failed maintenance: the old container starts again and its compose file stays.
    (deploy / 'compose.json').write_text('previous')
    done, log = go(MAINT='1')
    assert done.returncode == 3 and b'instance-maintenance-failed' in done.stderr
    assert log[-1].startswith('start openbear-a') and (deploy / 'compose.json').read_text() == 'previous'
    # A volume that never started has nothing to maintain.
    done, log = go(STARTED='1')
    assert done.returncode == 0 and [line.split()[0] for line in log] == ['info', 'run', 'run', 'inspect', 'compose']


async def test_gateway_control_moves_to_loopback_and_its_key_rotates(env, monkeypatch):
    e = env
    gw, installs = e.instances.gateway, []

    async def install(target, port, public_url, key, control_port=None):
        installs.append({'local': bool(target.get('local')), 'port': port, 'key': key, 'controlPort': control_port})

    async def refresh(value, private):
        value.update(status='connected', error='')
        return {'ok': True}
    monkeypatch.setattr(gw, '_install', install)
    monkeypatch.setattr(gw, '_refresh', refresh)
    value, private = await gw.config()
    old_key = private['controlKey']
    await gw.enable(123, {'serverId': 'local', 'port': 18962, 'publicUrl': e.gateway.public})
    value, private = await gw.config()
    assert installs[-1] == {'local': True, 'port': 18962, 'key': old_key, 'controlPort': 18963}
    assert private['controlPort'] == 18963 and private['controlUrl'] == '' and private['controlKey'] == old_key
    assert gw.control_route(value, private) == {'url': 'http://127.0.0.1:18963'}
    await gw.rotate_control_key(123)
    value, private = await gw.config()
    assert private['controlKey'] != old_key and installs[-1]['key'] == private['controlKey'] and private['controlPort'] == 18963
    # The control port is never handed to an instance.
    assert await e.instances._allocate_port(123, 'local', ()) == 18970
    await gw.store({**value, 'serverId': 'srv-n'}, private)
    assert gw.control_route(*(await gw.config())) == {'owner': 123, 'serverId': 'srv-n', 'port': 18963}

    # Only SSH: control stays on the public port at the server's address.
    async def target(owner, identity, *, nodes=False):
        return {'id': identity, 'channel': 'ssh', 'host': '198.51.100.4'}

    async def free(owner, identity, port):
        return {'ok': True, 'free': True}
    monkeypatch.setattr(gw.servers, 'target', target)
    monkeypatch.setattr(gw.servers, 'port', free)
    await gw.enable(123, {'serverId': 'srv-ssh', 'port': 18962, 'publicUrl': e.gateway.public})
    value, private = await gw.config()
    assert installs[-1]['controlPort'] is None and private['controlUrl'] == 'http://198.51.100.4:18962' and not private['controlPort']
    assert gw.control_route(value, private) == {'url': 'http://198.51.100.4:18962'}


async def test_gateway_install_config_separates_the_control_listener(env, monkeypatch):
    e, scripts = env, []

    async def run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False, source=''):
        if command == 'sh -s':
            scripts.append(stdin.decode())
        return {'ok': True, 'stdout': '', 'stderr': '', 'exitCode': 0}
    monkeypatch.setattr(e.instances.servers, 'run', run)
    if not (remote.ROOT / 'gateway/bin/openbear-gateway-linux-amd64').is_file():
        pytest.skip('gateway binary not built')
    monkeypatch.setattr('app.instances.gateway.platform.machine', lambda: 'x86_64')
    await e.instances.gateway._install({'local': True}, 18962, 'http://203.0.113.10:18962', 'k' * 48, 18963)
    await e.instances.gateway._install({'local': True}, 18962, 'http://203.0.113.10:18962', 'k' * 48)

    def config(script):
        line = next(x for x in script.splitlines() if x.endswith('"$tmp/gateway.json"') and x.startswith('printf'))
        return json.loads(base64.b64decode(line.split()[2].strip("'")))
    assert config(scripts[0]) == {'listen': '0.0.0.0:18962', 'controlListen': '127.0.0.1:18963', 'publicUrl': 'http://203.0.113.10:18962',
                                  'stateDir': '/var/lib/openbear-gateway', 'controlKey': 'k' * 48}
    assert 'controlListen' not in config(scripts[1])


async def test_migrate_network_rebinds_an_older_instance_and_resyncs(env, monkeypatch):
    e, runs, fail = env, [], {'on': True}
    submitted = form(e)
    submitted.pop('managementUrl')
    item = (await e.instances.save(123, submitted))['item']
    assert item['network'] == {'bind': ['127.0.0.1']} and item['managementUrl'] == 'http://127.0.0.1:18970'
    # As deployed before placement: server address and a 0.0.0.0 port.
    await edit(e, item['id'], network=None, managementUrl='http://203.0.113.10:18970', image='img:1')
    await delivered(e, item['id'])
    await e.db.conn.commit()

    async def run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        runs.append((command, stdin))
        return {'ok': not fail['on'], 'stdout': '', 'stderr': 'boom' if fail['on'] else '', 'exitCode': 3 if fail['on'] else 0}
    monkeypatch.setattr(e.instances.servers, 'run', run)
    with pytest.raises(InstanceError, match='instance_reconfigure_failed'):
        await e.instances.migrate_network(123, item['id'])
    pending = await e.instances.get(123, item['id'])
    assert pending['item']['network'] == {'bind': ['127.0.0.1'], 'pending': True}
    fail['on'] = False
    result = await e.instances.migrate_network(123, item['id'])
    assert result['migrated'] and result['synced']
    assert result['item']['network'] == {'bind': ['127.0.0.1']} and result['item']['managementUrl'] == 'http://127.0.0.1:18970'
    compose = json.loads(files(runs[-1][1])['compose.json'])['services']['openbear']
    assert compose['ports'] == ['127.0.0.1:18970:18961'] and 'maintenance' not in runs[-1][0]
    assert e.gateway.bodies[-1]['upstream'] == 'http://127.0.0.1:18970'
    assert not (await e.instances.migrate_network(123, item['id']))['migrated']
    # Edits after migration keep the placement.
    saved = await e.instances.save(123, {'notifications': {'health': False}}, item['id'])
    assert saved['item']['network'] == {'bind': ['127.0.0.1']} and saved['item']['managementUrl'] == 'http://127.0.0.1:18970'
    # Its port stays: the record would follow a new one, the running instance would not; on loopback
    # only it is not said to be on the mesh.
    with pytest.raises(InstanceError) as raised:
        await e.instances.save(123, {'port': 22}, item['id'])
    assert raised.value.error == 'instance_port_fixed'
    assert (await e.instances.save(123, {'port': 18970}, item['id']))['item']['port'] == 18970


async def legacy_on_node_server(e, monkeypatch, mesh):
    """An instance deployed before placement on the node server srv-n, gateway on this machine."""
    submitted = form(e)
    submitted.pop('managementUrl')
    item = (await e.instances.save(123, submitted))['item']
    await edit(e, item['id'], network=None, serverId='srv-n', managementUrl='http://198.51.100.4:18970', image='img:1')

    async def mesh_address(owner, identity):
        return mesh.get(identity, '')
    monkeypatch.setattr(e.instances.servers, 'mesh_address', mesh_address)
    return item['id']


async def migration(e, identity):
    detail = (await e.instances.get(123, identity))['item']['networkMigration']
    listed = next(i for i in (await e.instances.list(123))['items'] if i['id'] == identity)['networkMigration']
    assert listed == detail
    return detail


async def test_network_migration_is_offered_only_where_it_applies(env, monkeypatch):
    e, calls = env, []
    node_host(e, monkeypatch, calls=calls)
    submitted = form(e)
    submitted.pop('managementUrl')
    item = (await e.instances.save(123, submitted))['item']
    # Placed at creation: nothing to migrate.
    assert await migration(e, item['id']) is None
    # Deployed before placement on this machine, port open to all: can migrate now.
    await edit(e, item['id'], network=None, managementUrl='http://203.0.113.10:18970')
    assert await migration(e, item['id']) == {'pending': False, 'blockers': []}
    # Half done: finishing it is still offered.
    await edit(e, item['id'], network={'bind': ['127.0.0.1'], 'pending': True})
    assert await migration(e, item['id']) == {'pending': True, 'blockers': []}
    # An SSH-only server keeps server address + port; a fixed address is the user's.
    await edit(e, item['id'], network=None, serverId='srv-ssh', managementUrl='http://198.51.100.4:18970')
    assert await migration(e, item['id']) is None
    fixed = (await create(e, account={'username': 'second', 'password': 'another-fixture-pass'}))[0]
    await edit(e, fixed['id'], managementUrl='http://203.0.113.10:18971')
    assert await migration(e, fixed['id']) is None


async def test_network_migration_names_the_missing_mesh_before_it_is_tried(env, monkeypatch):
    # As found online: the gateway here (a mesh member), the instance's server outside the mesh.
    e, calls = env, []
    node_host(e, monkeypatch, calls=calls)
    identity = await legacy_on_node_server(e, monkeypatch, {'local': '10.88.0.1'})
    assert await migration(e, identity) == {'pending': False, 'blockers': ['server_mesh_required']}
    with pytest.raises(InstanceError, match='instance_mesh_required'):
        await e.instances.migrate_network(123, identity)
    assert not calls and (await e.instances.get(123, identity))['item'].get('network') is None


async def test_network_migration_asks_for_a_node_upgrade(env, monkeypatch):
    e, calls = env, []
    node_host(e, monkeypatch, calls=calls, features=frozenset())
    mesh = {'local': '10.88.0.1', 'srv-n': '10.88.0.2'}
    identity = await legacy_on_node_server(e, monkeypatch, mesh)
    assert await migration(e, identity) == {'pending': False, 'blockers': ['node_upgrade_required']}

    async def get(owner, identity):
        return {'id': identity, 'name': identity, 'host': '198.51.100.4'}
    monkeypatch.setattr(e.instances.servers, 'get', get)
    with pytest.raises(InstanceError, match='node_http_required'):
        await e.instances.migrate_network(123, identity)
    # Both at once are both said; an offline node last seen without http.local needs it too.
    mesh.clear()
    assert (await migration(e, identity))['blockers'] == ['node_upgrade_required', 'server_mesh_required', 'gateway_mesh_required']
    e.server.servers.hub.sessions.clear()
    from app.servers import schema
    await schema.migrate(e.db)
    await e.db.conn.execute('INSERT INTO server_nodes(server_id,owner,public_key,info_json,enrolled_ms) VALUES(?,?,?,?,?)',
                            ('srv-n', 123, 'fixture-key', json.dumps({'features': ['jobs']}), now()))
    assert (await e.instances.servers.http_blocker(123, 'srv-n')) == 'node_upgrade_required'
    await e.db.conn.execute('UPDATE server_nodes SET info_json=? WHERE server_id=?', (json.dumps({'features': ['jobs', 'http.local']}), 'srv-n'))
    assert (await e.instances.servers.http_blocker(123, 'srv-n')) == 'node_offline'


# ----- I2: provider copy -----
async def test_only_the_primary_models_provider_is_copied_by_default(env):
    e = env
    providers = e.server.config.models.providers
    providers['other'] = providers['openai'].model_copy()
    try:
        _, credentials = await create(e)
        assert list(credentials['initialConfig']['models']['providers']) == ['openai']
        _, credentials = await create(e, account={'username': 'second', 'password': 'another-fixture-pass'}, workspacePath=str(e.tmp / 'second'),
                                      models={'mode': 'copy', 'primary': 'openai/gpt', 'providers': ['openai', 'other']})
        assert sorted(credentials['initialConfig']['models']['providers']) == ['openai', 'other']
    finally:
        providers.pop('other')


# ----- I4: key rotation -----
async def test_rotation_reaches_instance_and_gateway_and_waits_for_an_offline_one(env, monkeypatch):
    e, runs, offline = env, [], {'on': True}
    item, old = await create(e)
    await edit(e, item['id'], image='img:1')
    await delivered(e, item['id'])
    await e.db.conn.commit()

    async def run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        if offline['on']:
            raise InstanceError('server_connection_timeout', 502)
        runs.append((command, stdin))
        return {'ok': True, 'stdout': '', 'stderr': '', 'exitCode': 0}
    monkeypatch.setattr(e.instances.servers, 'run', run)
    result = await e.instances.rotate_keys(123, item['id'])
    assert not result['rotated'] and result['error'] == 'server_connection_timeout'
    stored = e.instances.vault.open((await e.instances.row(123, item['id']))['protected'])
    assert {k: stored[k] for k in KEYS} == {k: old[k] for k in KEYS} and set(stored['rotation']) == set(KEYS)
    assert result['item']['rotation']['status'] == 'pending' and result['item']['rotation']['nextAtMs'] > now()
    assert old['businessKey'] not in json.dumps(result) and stored['rotation']['businessKey'] not in json.dumps(result)
    # Meanwhile everything keeps working with the old pair.
    assert e.gateway.bodies[-1]['businessKey'] == old['businessKey']
    assert (await e.instances.verify(123, item['id']))['verification']['ok']
    # Back online: the retry waits for its time, then completes.
    offline['on'] = False
    await e.instances.rotation_upkeep()
    assert not runs
    await edit(e, item['id'], rotation={**result['item']['rotation'], 'nextAtMs': 0})
    await e.instances.rotation_upkeep()
    new = stored['rotation']
    assert [list(files(stdin)) for _, stdin in runs] == [['compose.json', '0-set-auth.json'], ['compose.json', '0-rotate-identity.json']]
    assert json.loads(files(runs[0][1])['0-set-auth.json']) == {'authMode': 'gateway', 'gatewayKey': new['businessKey']}
    assert json.loads(files(runs[1][1])['0-rotate-identity.json']) == {'managementKey': new['managementKey']}
    environment = json.loads(files(runs[1][1])['compose.json'])['services']['openbear']['environment']
    assert environment['OPENBEAR_GATEWAY_KEY'] == new['businessKey'] and environment['OPENBEAR_MANAGEMENT_KEY'] == new['managementKey']
    stored = e.instances.vault.open((await e.instances.row(123, item['id']))['protected'])
    assert {k: stored[k] for k in KEYS} == new and 'rotation' not in stored
    current = (await e.instances.get(123, item['id']))['item']
    assert 'rotation' not in current and current['keysRotatedAtMs'] and current['gateway']['status'] == 'synced'
    assert e.gateway.bodies[-1]['businessKey'] == new['businessKey']
    e.child.key, e.child.business = new['managementKey'], new['businessKey']
    assert (await e.instances.verify(123, item['id']))['verification']['ok']


async def test_rotate_all_renews_the_control_key_and_every_instance(env, monkeypatch):
    e = env
    gw = e.instances.gateway

    async def install(target, port, public_url, key, control_port=None):
        e.gateway.key = key

    async def refresh(value, private):
        value.update(status='connected', error='')
        return {'ok': True}
    monkeypatch.setattr(gw, '_install', install)
    monkeypatch.setattr(gw, '_refresh', refresh)
    # The fixture gateway answers at its own address; keep reaching it there.
    monkeypatch.setattr(type(gw), 'control_route', staticmethod(lambda value, private: {'url': e.gateway.url}))
    item, old = await create(e)
    _, before = await gw.config()
    result = await gw.rotate_all(123)
    _, after = await gw.config()
    assert after['controlKey'] != before['controlKey'] and result['rotatedInstances'] == 1 and result['pendingInstances'] == 0
    stored = e.instances.vault.open((await e.instances.row(123, item['id']))['protected'])
    # Not delivered yet: nothing to change on a server, the first start uses the new pair.
    assert stored['businessKey'] != old['businessKey'] and stored['managementKey'] != old['managementKey']
    assert e.gateway.bodies[-1]['businessKey'] == stored['businessKey']


# ----- I5: hostile replies -----
async def test_hostile_child_json_is_bounded_and_the_operation_still_ends(env, monkeypatch):
    e = env
    item, _ = await create(e)
    deep = b'[' * 100000 + b']' * 100000
    for raw in (deep, b'[' * (network.MAX_DEPTH + 1) + b']' * (network.MAX_DEPTH + 1), b'{"a":' * 70 + b'1' + b'}' * 70, b'\xff'):
        with pytest.raises(ValueError):
            network.loads(raw)
    assert isinstance(network.loads(b'[' * network.MAX_DEPTH + b']' * network.MAX_DEPTH), list)

    async def fetch(service, route, method, path, **kwargs):
        return 200, deep
    monkeypatch.setattr(network, 'fetch', fetch)
    with pytest.raises(InstanceError, match='child_invalid_response'):
        await e.instances.request(123, item['id'], {'method': 'GET', 'path': '/api/settings'})
    check = await network.probe(e.instances, await e.instances.row(123, item['id']))
    assert not check['ok'] and check['error'] == 'child_invalid_response' and check['gateway']['error'] == 'gateway_invalid_response'
    # The terminal state is written before the probe; a probe that blows up cannot hold the instance.
    seen = []

    async def probe(service, row):
        op = await one(e.db.conn, 'SELECT status FROM instance_operations WHERE instance_id=? ORDER BY created_ms DESC', (row['id'],))
        seen.append(op['status'])
        raise RecursionError()
    monkeypatch.setattr(network, 'probe', probe)
    model(e, [[StreamEvent(kind='content', text='done'), StreamEvent(kind='finish', finish_reason='stop')]])
    _, op = await run(e, item)
    assert seen == ['needs_verification'] and op['status'] == 'needs_verification'
    assert json.loads(op['verification_json'])['error'] == 'verification_failed'
    assert (await e.instances.archive(123, item['id']))['ok']


# ----- I6: reported sizes -----
async def test_reported_text_is_cut_before_storage_and_tool_results(env):
    e = env
    item, _ = await create(e)
    e.child.health, e.child.issues = 'degraded' + 'x' * 5000, ['y' * 10000] * 100 + [{'nested': 'z'}]
    check = (await e.instances.verify(123, item['id']))['verification']
    for health in (check['health'], check['gateway']['status']['health']):
        assert len(health['status']) == 80 and len(health['issues']) == network.ISSUES_LIMIT
        assert all(issue == 'y' * network.TEXT_LIMIT for issue in health['issues'])
    row = await e.instances.row(123, item['id'])
    assert len(row['public_json']) < 30000
    cleaned = network.clean_status({'ok': True, 'version': 'v' * 10000, 'instanceId': 'i' * 1000, 'extra': 'e' * 1000,
                                    'persistence': {'root': 'r' * 5000, 'mounted': 'yes', 'dbPath': 1}})
    assert cleaned['version'] == 'v' * 80 and len(cleaned['instanceId']) == 128 and 'extra' not in cleaned
    assert cleaned['persistence'] == {'root': 'r' * 512, 'mounted': False, 'dbPath': '', 'workspacePath': ''}


# ----- I8: limits -----
async def test_memory_limit_is_optional_per_instance(env):
    e = env
    assert Form.model_validate({'name': 'x', 'serverId': 'local', 'account': {'username': 'a', 'password': 'p'}}).memoryLimitMb == 0
    with pytest.raises(ValidationError):
        Form.model_validate({'name': 'x', 'serverId': 'local', 'account': {'username': 'a', 'password': 'p'}, 'memoryLimitMb': 100})
    item, _ = await create(e, memoryLimitMb=1024)
    assert item['memoryLimitMb'] == 1024


# ----- I9: event time and notice rate -----
async def test_event_time_is_never_ahead_and_notices_are_rate_limited(env):
    e = env
    item, _ = await create(e)
    future = now() + 3600000
    e.gateway.events = [{'seq': i + 1, 'eventId': f'e{i}', 'instanceId': item['id'], 'type': 'login_failure', 'atMs': future, 'data': {}}
                        for i in range(NOTICE_BURST + 3)]
    await e.instances.gateway.pull_events()
    events = (await e.instances.events(123, item['id']))['items']
    assert len(events) == NOTICE_BURST + 3 and all(x['atMs'] <= now() for x in events)
    notes = await many(e.db.conn, "SELECT text FROM instance_notifications WHERE id LIKE 'event:%'")
    assert len(notes) == NOTICE_BURST
    # The next window says how many were left out.
    row = await e.instances.row(123, item['id'])
    window = json.loads(row['public_json'])['noticeWindow']
    await edit(e, item['id'], noticeWindow={**window, 'startMs': window['startMs'] - NOTICE_WINDOW_MS})
    past = now() - 1000
    e.gateway.events = [{'seq': 100, 'eventId': 'late', 'instanceId': item['id'], 'type': 'login_failure', 'atMs': past, 'data': {}}]
    await e.instances.gateway.pull_events()
    assert next(x for x in (await e.instances.events(123, item['id']))['items'] if x['id'] == 'gw:late')['atMs'] == past
    texts = [r['text'] for r in await many(e.db.conn, "SELECT text FROM instance_notifications WHERE id LIKE 'event:%' ORDER BY created_ms,rowid")]
    assert len(texts) == NOTICE_BURST + 1 and texts[-1] == '实例「实例丙」登录失败（期间另有 3 条通知未发送）'
    assert 'noticeWindow' not in (await e.instances.get(123, item['id']))['item']


# ----- I10: locks -----
async def test_gateway_status_only_locks_known_instances(env):
    e = env
    item, _ = await create(e)
    applied = (await e.instances.get(123, item['id']))['item']['gateway']['appliedRevision']
    entries = [{'id': f'ghost-{i}', 'username': 'x', 'revision': 1} for i in range(500)]
    await e.instances.gateway._account_names({'instances': entries + [{'id': item['id'], 'username': 'renamed', 'revision': applied}]})
    assert not [k for k in e.instances.locks if k.startswith('ghost-')]
    assert (await e.instances.get(123, item['id']))['item']['account'] == {'username': 'renamed'}
    await e.instances.gateway._account_names({'instances': 'not-a-list'})


# ----- I15: data directory -----
@pytest.mark.parametrize('path', ['/root/x', '/root', '/var/lib/docker/volumes/x', '/var/lib/containerd/x', '/home/alice', '/opt',
                                  '/etc/openbear', '/usr/local/openbear', '/opt/openbear', '/opt/openbear/instances', '/boot/x', '/tmp',
                                  '/var/lib/openbear-gateway', '/var/lib/openbear-gateway/instances', '/var/lib/openbear-node',
                                  '/var/lib/openbear-node/mesh'])
def test_workspace_rejects_system_and_shared_directories(path):
    with pytest.raises(InstanceError, match='dedicated_volume_required'):
        check_workspace(path)


def test_workspace_rejects_controller_directories_and_their_parents():
    protected = ['/data/openbear', '/data/openbear/data']
    for path in ('/data/openbear', '/data', '/data/openbear/data'):
        with pytest.raises(InstanceError, match='controller_directory_volume_forbidden'):
            check_workspace(path, protected)
    for path in ('/opt/openbear/instances/a', '/data/other', '/home/alice/instances/a', '/srv/openbear-a'):
        check_workspace(path, protected)


async def test_workspace_rules_apply_to_new_and_moved_directories_only(env, monkeypatch):
    e = env
    # The controller's own directory, placed where neither it nor its parent is a shared
    # directory: a checkout right under /tmp or /opt (or an install at /opt/openbear) would fail
    # the shared-directory rule first, whatever this test is about. Only the check reads it here;
    # the instance created further down uses the real tree again. It sits beside e.tmp (the
    # database and workspace directory), so e.tmp is still refused for being those.
    with monkeypatch.context() as m:
        controller = (e.tmp.parent / f'{e.tmp.name}-install' / 'openbear').resolve()
        (controller / 'data').mkdir(parents=True)
        m.setattr(remote, 'ROOT', controller)
        for path in (str(controller), str(controller / 'data'), str(controller.parent), str(e.tmp), str(e.tmp.parent)):
            with pytest.raises(InstanceError, match='controller_directory_volume_forbidden'):
                await create(e, workspacePath=path)
        link = e.tmp / 'looks-dedicated'
        link.symlink_to(controller)
        with pytest.raises(InstanceError, match='controller_directory_volume_forbidden'):
            await create(e, workspacePath=str(link))
    with pytest.raises(InstanceError, match='dedicated_volume_required'):
        await create(e, workspacePath='/root/instances/a')
    # An instance saved under the old rules keeps saving other settings.
    item, _ = await create(e)
    row = await e.instances.row(123, item['id'])
    protected = e.instances.vault.open(row['protected'])
    protected['form']['workspacePath'] = '/root/legacy'
    await e.db.conn.execute('UPDATE managed_instances SET protected=? WHERE id=?', (e.instances.vault.seal(protected), item['id']))
    await e.db.conn.commit()
    await edit(e, item['id'], workspacePath='/root/legacy')
    saved = await e.instances.save(123, {'notifications': {'health': False}}, item['id'])
    assert saved['item']['workspacePath'] == '/root/legacy' and saved['item']['notifications']['health'] is False
    with pytest.raises(InstanceError, match='dedicated_volume_required'):
        await e.instances.save(123, {'workspacePath': '/root/other'}, item['id'])


async def test_a_form_save_keeps_the_delivered_image_and_rotation_reaches_the_volume(env, monkeypatch):
    """From the 10-30 check: toggling a notice switch emptied the delivered image, and key rotation
    then changed only the controller's keys, leaving the instance unreachable."""
    from app.llm.events import StreamEvent
    from tests.test_instances import model, run, tool
    e = env
    item, _ = await create(e)
    model(e, [tool('deliver', {'revision': 1, 'source': False}),
              tool('report', {'revision': 1, 'outcome': 'completed', 'summary': 'ok'}),
              [StreamEvent(kind='content', text='done'), StreamEvent(kind='finish', finish_reason='stop')]])
    await run(e, item)
    delivered = (await e.instances.get(123, item['id']))['item']['image']
    saved = (await e.instances.save(123, {'notifications': {'health': False}}, item['id']))['item']
    assert delivered and saved['image'] == delivered
    runs = []

    async def record(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        runs.append(command)
        return {'ok': True, 'stdout': '', 'stderr': '', 'exitCode': 0}
    monkeypatch.setattr(e.instances.servers, 'run', record)
    assert (await e.instances.rotate_keys(123, item['id']))['rotated'] and any('rotate-identity' in c or 'set-auth' in c or 'docker' in c for c in runs)


async def test_a_record_emptied_before_takes_its_image_back_from_the_server(env, monkeypatch):
    e = env
    item, _ = await create(e)
    await edit(e, item['id'], image='')
    await e.db.conn.execute("UPDATE managed_instances SET status='ready' WHERE id=?", (item['id'],))
    await e.db.conn.commit()
    row = await e.instances.row(123, item['id'])
    composed = json.dumps({'services': {'openbear': {'image': 'openbear:0.14.0'}}})

    async def run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        return {'ok': True, 'stdout': composed if command.startswith('cat ') else '', 'stderr': '', 'exitCode': 0}
    monkeypatch.setattr(e.instances.servers, 'run', run)
    record = json.loads(row['public_json'])
    assert await e.instances._delivered_image(123, row, record) == 'openbear:0.14.0' and record['image'] == 'openbear:0.14.0'
    composed = ''
    record = json.loads(row['public_json'])
    with pytest.raises(InstanceError, match='instance_image_unknown'):
        await e.instances._delivered_image(123, row, record)


async def test_instances_on_one_server_never_share_a_data_directory(env):
    """From the 10-30 check: accounts in Chinese all defaulted to .../instance, and a new instance
    with an archived one's account took its directory and container."""
    from app.instances.remote import container_name
    e = env
    made = []
    for name, user in (('妈妈的实例', '妈妈'), ('爸爸的实例', '爸爸')):
        data = form(e, name=name, account={'username': user, 'password': 'pw-' + user})
        data.pop('workspacePath')
        made.append((await e.instances.save(123, data))['item'])
    assert made[0]['workspacePath'] == '/opt/openbear/instances/instance'
    assert made[1]['workspacePath'].startswith('/opt/openbear/instances/instance-') and container_name(made[0]) != container_name(made[1])
    # An archived instance's data stays apart from a new one with its account.
    await e.instances.archive(123, made[0]['id'])
    data = form(e, name='新实例', account={'username': '妈妈', 'password': 'pw-x'})
    data.pop('workspacePath')
    again = (await e.instances.save(123, data))['item']
    assert again['workspacePath'] not in {m['workspacePath'] for m in made}
    # A directory chosen by hand that an active instance has is refused, also one only differing in case.
    for path in (made[1]['workspacePath'], made[1]['workspacePath'].upper().replace('/OPT/OPENBEAR/INSTANCES', '/srv')):
        with pytest.raises(InstanceError, match='instance_workspace_in_use'):
            await e.instances.save(123, form(e, name='重复', account={'username': 'dup', 'password': 'pw-dup'}, workspacePath=path))


async def test_an_instance_on_a_mesh_address_its_server_lost_is_offered_the_new_one(env, monkeypatch):
    """From the 10-30 check: a new prefix or another address for its server cut the gateway off
    with nothing saying so; migrating the network moves it to the address the server has now."""
    e, runs = env, []
    node_host(e, monkeypatch, calls=[])
    mesh = {'srv-n': '10.88.0.2', 'local': '10.88.0.1'}
    identity = await legacy_on_node_server(e, monkeypatch, mesh)
    await edit(e, identity, network={'bind': ['127.0.0.1', '10.88.0.2']}, managementUrl='http://10.88.0.2:18970')
    await delivered(e, identity)
    assert await migration(e, identity) is None
    mesh['srv-n'] = '10.88.0.7'
    assert await migration(e, identity) == {'pending': False, 'blockers': [], 'moved': True}

    async def run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        runs.append((command, stdin))
        return {'ok': True, 'stdout': '', 'stderr': '', 'exitCode': 0}
    async def target(owner, identity, nodes=False):
        return {'id': identity, 'channel': 'node'}
    monkeypatch.setattr(e.instances.servers, 'run', run)
    monkeypatch.setattr(e.instances.servers, 'target', target)
    monkeypatch.setattr(e.instances.servers, 'http_ready', lambda owner, identity: _true())
    result = await e.instances.migrate_network(123, identity)
    assert result['migrated'] and result['item']['network'] == {'bind': ['127.0.0.1', '10.88.0.7']}
    assert result['item']['managementUrl'] == 'http://10.88.0.7:18970'
    compose = json.loads(files(runs[-1][1])['compose.json'])['services']['openbear']
    assert compose['ports'] == ['127.0.0.1:18970:18961', '10.88.0.7:18970:18961']
    assert await migration(e, identity) is None
    with pytest.raises(InstanceError) as raised:
        await e.instances.save(123, {'port': 22}, identity)
    assert raised.value.error == 'instance_port_fixed_on_mesh'


async def _true():
    return True


def test_the_draft_says_which_instances_lose_their_gateway():
    from app.servers import mesh_model as model
    s = model.default_settings()
    doc = model.empty_doc()
    doc['members'] = [model.new_member('gw', s), model.new_member('host', s)]
    model.allocate(doc)
    keys = {'publicKey': 'A' * 43 + '=', 'controlKey': 'B' * 43 + '=', 'signKey': 'C' * 43 + '=', 'e2eKey': 'D' * 43 + '='}
    facts = {'gw': {'name': 'gateway', 'platform': 'linux', **keys},
             'host': {'name': 'host', 'platform': 'linux', **{k: v.replace(v[0], v[0].lower(), 1) for k, v in keys.items()},
                      'instancePorts': [{'id': 'i1', 'name': 'bot', 'port': 18970, 'gateway': 'gw', 'bind': doc['members'][1]['address']}]}}
    applied = model.upgrade(json.loads(json.dumps(doc)))

    def said(d):
        return [x['message'] for x in model.hints(d, facts, applied=applied) if x['error'] == 'mesh_instance_cut']
    assert said(doc) == []
    doc['members'][1]['address'] = '100.96.0.20'
    assert said(doc) == ['host 的组网地址改了，它上面的实例「bot」（端口 18970）还监听在原地址，网关会访问不到它：应用后到实例页点「迁移网络」，让实例改到新地址。']
    doc['members'][1].update(address=applied['members'][1]['address'], removing=True)
    assert said(doc) == ['host 移出组网后，网关访问不到它上面的实例「bot」（端口 18970），实例会无法使用。']
    doc['members'][1]['removing'], doc['members'][0]['removing'] = False, True
    assert said(doc) == ['网关所在的 gateway 移出组网后，网关访问不到 host 上的实例「bot」（端口 18970）。']


async def test_a_question_after_a_deployment_leaves_it_completed(env):
    """From the 10-30 check: any message in the finished deployment's conversation reopened it,
    and with no report it ended needs_verification, demoting the instance and sending a notice."""
    import uuid

    from app.web_console.live_stream import _WebStreamRenderer
    from tests.test_instances import tool
    e = env
    item, _ = await create(e)
    model(e, [tool('report', {'revision': 1, 'outcome': 'completed', 'summary': 'ok'}),
              [StreamEvent(kind='content', text='部署完成'), StreamEvent(kind='finish', finish_reason='stop')]])
    accepted, op = await run(e, item)
    assert (await e.instances.get(123, item['id']))['item']['status'] == 'ready'
    notices = len(await many(e.db.conn, 'SELECT 1 FROM instance_notifications'))
    model(e, [[StreamEvent(kind='content', text='访问地址是网关地址。'), StreamEvent(kind='finish', finish_reason='stop')]])
    original = await one(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=?', (accepted['conversationUuid'],))
    live = e.server._live_for(original)
    root = str(uuid.uuid4())
    await live.publish({'type': 'accepted', 'turnUuid': root, 'runUuid': root})
    await e.server._run_web_turn(original['internal_chat_id'], '访问地址是什么？', _WebStreamRenderer(live=live), conversation=original, root_turn_uuid=root)
    op = await one(e.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (op['id'],))
    assert (op['status'], op['error'], op['resumed']) == ('completed', '', '')
    assert (await e.instances.get(123, item['id']))['item']['status'] == 'ready'
    assert len(await many(e.db.conn, 'SELECT 1 FROM instance_notifications')) == notices
    # A turn that does something on the server is maintenance again and has to report.
    model(e, [tool('remote', {'revision': op['revision'] + 1, 'command': 'true'}),
              [StreamEvent(kind='content', text='改好了'), StreamEvent(kind='finish', finish_reason='stop')]])
    root = str(uuid.uuid4())
    await live.publish({'type': 'accepted', 'turnUuid': root, 'runUuid': root})
    await e.server._run_web_turn(original['internal_chat_id'], '把日志级别调高', _WebStreamRenderer(live=live), conversation=original, root_turn_uuid=root)
    op = await one(e.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (op['id'],))
    assert (op['status'], op['error']) == ('needs_verification', 'no_report')


async def test_an_owners_check_survives_a_restart(env):
    """From the 10-30 check: after 立即检查 passed, a restart set it back to 检查发现问题 for good."""
    from app.instances import InstanceService
    from app.instances.monitor import once
    e = env
    item, _ = await create(e)
    _, op = await run(e, item)
    assert op['status'] == 'needs_verification'
    assert (await e.instances.verify(123, item['id']))['item']['status'] == 'ready'
    await e.instances.close()
    e.instances = InstanceService(e.db, e.server)
    e.server.instances = e.instances
    await e.instances.start()
    await e.instances.recovery_task
    await once(e.instances, force=True)
    assert (await e.instances.get(123, item['id']))['item']['status'] == 'ready'


async def test_archiving_with_the_gateway_stopped_archives_and_disables_the_account_later(env, monkeypatch):
    """From the 10-30 check: it answered ok, yet stayed pending until the gateway came back."""
    e = env

    async def record_only(target, command, **kw):
        return {'ok': True, 'stdout': '', 'stderr': '', 'exitCode': 0}
    monkeypatch.setattr(e.instances.servers, 'run', record_only)
    item, _ = await create(e)
    await e.instances.gateway.disable(123)
    result = await e.instances.archive(123, item['id'])
    assert result['item']['status'] == 'archived' and not result['item'].get('archivePending')
    # Enabled again: the account is disabled there with the next sync.
    value, private = await e.instances.gateway.config()
    await e.instances.gateway.store({**value, 'enabled': True}, private)
    await e.instances.gateway.sync_all()
    assert e.gateway.bodies[-1]['enabled'] is False


def test_rebuilding_fails_when_docker_cannot_be_used(tmp_path):
    """From the 10-30 check: a login user outside the docker group had every docker command
    refused, and the script still ended 0, as if the keys had reached the instance."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    docker = bin_dir / 'docker'
    docker.write_text('#!/bin/sh\necho "$*" >> "$LOG"\necho "permission denied while trying to connect to the Docker daemon socket" >&2\nexit 1\n')
    docker.chmod(0o755)
    item = {'workspacePath': str(tmp_path / 'data' / 'openbear-a'), 'port': 18970, 'managementUrl': 'http://127.0.0.1:18970',
            'image': 'openbear:0.14.0', 'network': {'bind': ['127.0.0.1']}}
    command, stdin = remote.reconfigure({'id': 'i'}, KEYS, item, maintenance=[('set-auth', {'authMode': 'gateway', 'gatewayKey': 'n' * 48})])
    log = tmp_path / 'log'
    done = subprocess.run(['/bin/sh', '-c', command], input=stdin, capture_output=True,
                          env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'LOG': str(log)})
    assert done.returncode == 4 and b'instance-docker-unavailable' in done.stderr
    assert log.read_text().split() == ['info']
