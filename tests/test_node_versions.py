"""Which node build a node should run, how that is shown, and how nodes are brought to it:
the two-step judgement (same build by sha256, then direction and urgency), confirmed
updates, relays last, automatic updates and the update reminder, all only with 主机 on."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from app import versioning
from app.config import FeaturesConfig, UpdatesConfig
from app.servers import releases
from app.servers import service as service_module
from app.servers.hub import Session
from app.servers.service import node_facts, node_source, update_state
from app.tools.servers import _row
from app.webhooks.repository import many, one
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

NAME = 'openbear-node-linux-amd64'
SHA = 'a' * 64
ALL = frozenset(releases.REQUIRED_FEATURES)


def built(version='1.2.0', sha=SHA, source='github'):
    return {'version': version, 'source': source, 'binaries': {NAME: {'sha256': sha, 'bytes': 1}}}


def facts(version='1.2.0', build=SHA, features=ALL, protocol=(1, 1), arch='amd64'):
    return {'version': version, 'build': build, 'info': {'platform': 'linux', 'arch': arch}, 'features': features, 'protocol': protocol}


@pytest.mark.parametrize('node, target, withdrawn, state', [
    # Same build by sha256: current, whatever the versions say.
    (facts(), built(), (), 'current'),
    (facts(version='dev'), built(), (), 'current'),
    # Must update: a feature this controller needs, no common protocol, or a withdrawn version.
    (facts(version='1.1.0', build='b' * 64, features=ALL - {'mesh'}), built(), (), 'required'),
    (facts(version='1.3.0', build='b' * 64, features=frozenset()), built(), (), 'required'),
    (facts(version='1.1.0', build='b' * 64, protocol=(2, 2)), built(), (), 'required'),
    (facts(version='1.3.0', build='b' * 64), built(), ('1.3.0',), 'required'),
    # Can update: a newer target (dev < beta < rc < release, then N), or an unknown version.
    (facts(version='1.1.0', build='b' * 64), built(), (), 'available'),
    (facts(version='1.2.0-rc.1', build='b' * 64), built(), (), 'available'),
    (facts(version='1.2.0-dev.13', build='b' * 64), built(), (), 'available'),
    (facts(version='1.2.0-dev.13', build='b' * 64), built('1.2.0-dev.14', source='local'), (), 'available'),
    (facts(version='1.2.0-dev.13', build='b' * 64), built('1.2.0-beta.1'), (), 'available'),
    (facts(version='1.1.0', build='b' * 64), built('1.2.0-dev.1', source='local'), (), 'available'),
    (facts(version='dev', build='b' * 64), built(), (), 'available'),
    # Same version, another build: a mismatch, also with a local build.
    (facts(version='1.2.0', build='b' * 64), built(), (), 'mismatch'),
    (facts(version='1.3.0-dev.4', build='b' * 64), built('1.3.0-dev.4', source='local'), (), 'mismatch'),
    # A newer local development build is offered like any newer version.
    (facts(version='1.3.0-dev.4', build='b' * 64), built('1.3.0-dev.5', source='local'), (), 'available'),
    # Newer than the target: shown, never offered (it would be a downgrade), also a development
    # build against an older release or an older development build.
    (facts(version='1.4.0', build='b' * 64), built(), (), 'ahead'),
    (facts(version='1.3.0-dev.4', build='b' * 64), built(), (), 'ahead'),
    (facts(version='1.2.0-dev.14', build='b' * 64), built('1.2.0-dev.13', source='local'), (), 'ahead'),
    (facts(version='1.2.0', build='b' * 64), built('1.2.0-dev.13', source='local'), (), 'ahead'),
    # An older node that does not say its build: the target version counts as current.
    (facts(version='1.2.0', build=''), built(), (), 'current'),
    # Features not known (recorded before they were kept): judged by version only.
    (facts(version='1.1.0', build='b' * 64, features=None), built(), (), 'available'),
    # No build for its platform: nothing to offer.
    (facts(version='1.1.0', build='b' * 64, arch='riscv64'), built(), (), ''),
    (facts(version='1.1.0', build='b' * 64, arch='riscv64', features=ALL - {'net'}), built(), (), 'required'),
])
def test_update_state(node, target, withdrawn, state):
    result = update_state(target, node, frozenset(withdrawn))
    assert result['state'] == state, result
    downgrade = state != 'current' and node['info']['arch'] == 'amd64' and versioning.newer(node['version'], target['version'])
    assert result['downgrade'] == downgrade
    assert result['updatable'] == (state in ('required', 'available', 'mismatch') and node['info']['arch'] == 'amd64'
                                   and (not downgrade or node['version'] in withdrawn))
    if state == 'required' and node['features'] is not None and node['features'] != ALL:
        assert result['missing'] == [f for f in releases.REQUIRED_FEATURES if f not in node['features']]
    if result['target']:
        assert result['target'] == {'version': target['version'], 'source': target['source'], 'build': target['binaries'][NAME]['sha256'][:12]}


def test_a_node_is_never_offered_an_older_target_unless_its_version_was_withdrawn():
    # A development node against a GitHub release older than it: shown, not updatable.
    ahead = update_state(built('1.0.5'), facts(version='1.1.0-dev.13', build='b' * 64))
    assert (ahead['state'], ahead['updatable'], ahead['downgrade']) == ('ahead', False, True)
    # Needing what the controller requires does not make an older target an update either.
    lacking = update_state(built('1.2.0'), facts(version='1.3.0', build='b' * 64, features=frozenset()))
    assert (lacking['state'], lacking['updatable'], lacking['downgrade']) == ('required', False, True)
    # Off a withdrawn version the older target is still taken, and named a downgrade.
    pulled = update_state(built('1.2.0'), facts(version='1.3.0', build='b' * 64), frozenset({'1.3.0'}))
    assert (pulled['state'], pulled['updatable'], pulled['downgrade']) == ('required', True, True)
    # Same version, another build (a local rebuild): an update, not a downgrade.
    rebuilt = update_state(built('1.1.0-dev.13', source='local'), facts(version='1.1.0-dev.13', build='b' * 64))
    assert (rebuilt['state'], rebuilt['updatable'], rebuilt['downgrade']) == ('mismatch', True, False)


def test_node_source():
    assert node_source(built(source='local'), facts()) == 'local'
    assert node_source(built(), facts()) == 'release'
    assert node_source(built(), facts(version='1.3.0-beta.2', build='c' * 64)) == 'prerelease'
    assert node_source(built(), facts(version='1.3.0-dev.2', build='c' * 64)) == 'dev'
    assert node_source(built(), facts(version='dev', build='c' * 64)) == 'unknown'


def test_offline_nodes_are_judged_from_their_record():
    stored = {'version': '1.1.0', 'build': 'b' * 64, 'info': {'platform': 'linux', 'arch': 'amd64'}, 'features': ['fs.edit'], 'protocol': 1, 'protocolMin': 1}
    assert update_state(built(), node_facts(None, stored))['state'] == 'required'
    # A record from before builds and features were kept.
    assert node_facts(None, {'version': '1.1.0'})['features'] is None
    assert update_state(built(), node_facts(None, {'version': '1.1.0', 'info': {'platform': 'linux', 'arch': 'amd64'}}))['state'] == 'available'


def test_tool_rows_show_the_build_its_source_target_and_state():
    item = {'id': 's1', 'name': 'a', 'profile': {}, 'live': None,
            'node': {'version': '1.1.0', 'build': 'bbbbbbbbbbbb', 'source': 'release', 'updateAvailable': False, 'caps': {'exec': True}, 'info': {},
                     'update': {'state': 'required', 'missing': ['mesh'], 'target': {'version': '1.2.0', 'source': 'github', 'build': 'aaaaaaaaaaaa'}}}}
    node = _row(item, __import__('datetime').date(2026, 10, 28))['node']
    assert node['build'] == 'bbbbbbbbbbbb' and node['source'] == 'release' and node['updateState'] == 'required'
    assert node['missingFeatures'] == ['mesh'] and node['target'] == {'version': '1.2.0', 'source': 'github', 'build': 'aaaaaaaaaaaa'}


# ----- with a controller -----
def fake_session(sid, *, version='0.9.0', build='b' * 64, exec_=True, features=ALL):
    session = Session(sid, None, None, '')
    session.version, session.build, session.features = version, build, frozenset(features)
    session.caps = {'exec': exec_, 'terminal': True, 'browser': False}
    session.info = {'platform': 'linux', 'arch': 'amd64'}
    return session


async def add_node(e, name, **kwargs):
    sid = (await api(e, 'POST', '/api/servers', {'name': name}))['item']['id']
    async with e.db.write_transaction(label='test-node') as conn:
        await conn.execute('INSERT INTO server_nodes VALUES(?,?,?,?,?,?)', (sid, 123, base64.b64encode(sid.encode().ljust(32, b'k')[:32]).decode(), '{}', 1, 0))
    e.servers.hub.sessions[sid] = fake_session(sid, **kwargs)
    return sid


@pytest.fixture
def node_file(shipped):  # noqa: F811
    """The shipped amd64 build as a file, so updates get past fetching it."""
    (shipped / NAME).write_bytes(b'x')
    return json.loads((shipped / 'manifest.json').read_text())['binaries'][NAME]['sha256']


def settings(e, *, servers=True, prerelease=False, auto=False):
    e.server.config = e.server.config.model_copy(update={'features': FeaturesConfig(instances=True, servers=servers),
                                                         'updates': UpdatesConfig(prerelease=prerelease, auto_update_nodes=auto)})


async def test_an_update_is_reported_only_once_the_node_runs_the_new_build(env, node_file, monkeypatch):  # noqa: F811
    e = env
    sid = await add_node(e, 'confirm')
    sent = []

    async def node_call(owner, server_id, method, params, timeout):
        sent.append((method, params['sha256']))

        async def reconnect():
            await asyncio.sleep(0.3)
            e.servers.hub.sessions[server_id] = fake_session(server_id, version='1.0.0', build=params['sha256'])
        e.servers.spawn(reconnect())
        return {'changed': True, 'from': '0.9.0', 'to': '1.0.0'}
    monkeypatch.setattr(e.servers, '_node_call', node_call)
    result = await e.servers.update_node(123, sid)
    assert sent == [('update', node_file)]
    assert result['confirmed'] is True and result['state'] == 'updated' and result['build'] == node_file[:12]
    assert (await e.servers.get(123, sid))['execLog'][0]['command'] == '更新节点 0.9.0 → 1.0.0'
    # Once there, it is not updated again.
    assert (await e.servers.update_node(123, sid)) == {'ok': True, 'changed': False, 'state': 'current', 'from': '1.0.0', 'to': '1.0.0',
                                                        'build': node_file[:12]}


def test_an_update_waits_two_minutes_for_the_node_to_come_back():
    assert service_module.NODE_CONFIRM_S == 120


async def test_pages_follow_a_test_version_switch_only_with_servers_on(env, monkeypatch):  # noqa: F811
    """Listing, detail and the release status pick the version for a changed 接收测试版 first
    (waiting a bounded time); with 主机 off nothing is checked."""
    e = env
    sid = await add_node(e, 'follow')
    waits = []

    async def follow_switch(wait=None):
        waits.append(wait)
        return True
    monkeypatch.setattr(releases, 'follow_switch', follow_switch)
    settings(e)
    await api(e, 'GET', '/api/servers')
    await e.servers.get(123, sid)
    await e.servers.node_release()
    await e.servers.ensure_builds()
    assert waits == [service_module.SWITCH_WAIT_S] * 4
    settings(e, servers=False)
    await api(e, 'GET', '/api/servers')
    await e.servers.node_release()
    assert len(waits) == 4


async def test_a_node_that_does_not_come_back_on_the_new_build_is_not_reported_updated(env, node_file, monkeypatch):  # noqa: F811
    e = env
    sid = await add_node(e, 'silent')
    monkeypatch.setattr(service_module, 'NODE_CONFIRM_S', 0.6)

    async def node_call(owner, server_id, method, params, timeout):
        return {'changed': True, 'from': '0.9.0', 'to': '1.0.0'}
    monkeypatch.setattr(e.servers, '_node_call', node_call)
    result = await e.servers.update_node(123, sid)
    assert result['confirmed'] is False and result['state'] == 'unconfirmed' and '未确认运行' in result['note']
    log = (await e.servers.get(123, sid))['execLog'][0]
    assert log['command'] == '更新节点 0.9.0 → 1.0.0（文件已替换，未确认运行）' and log['outcome'] == 'unknown'


async def test_update_all_does_relays_last_one_at_a_time(env, node_file, monkeypatch):  # noqa: F811
    e = env
    plain = [await add_node(e, f'plain-{n}') for n in range(3)]
    relays = [await add_node(e, f'relay-{n}') for n in range(2)]
    await add_node(e, 'no-exec', exec_=False)
    await add_node(e, 'current', build=node_file, version='1.0.0')
    events, running = [], set()

    async def update_node(owner, sid, *, source='web', downgrade=False):
        running.add(sid)
        events.append(('start', sid, frozenset(running)))
        await asyncio.sleep(0.05)
        running.discard(sid)
        events.append(('end', sid))
        return {'ok': True, 'changed': True, 'confirmed': True}

    async def relay_ids(owner):
        return set(relays)
    monkeypatch.setattr(e.servers, 'update_node', update_node)
    monkeypatch.setattr(e.servers, '_relays', relay_ids)
    items = (await e.servers.update_nodes(123))['items']
    assert sorted(i['serverId'] for i in items) == sorted(plain + relays)
    assert [i['serverId'] for i in items][-2:] == relays and all(i.get('relay') for i in items[-2:])
    starts = [(sid, together) for kind, sid, *rest in events if kind == 'start' for together in rest]
    first_relay = next(n for n, (sid, _) in enumerate(starts) if sid in relays)
    # Every ordinary node is done before the first relay starts; a relay updates alone.
    assert all(sid in plain for sid, _ in starts[:first_relay]) and events.index(('end', plain[-1])) < events.index(('start', relays[0], frozenset({relays[0]})))
    assert all(together == {sid} for sid, together in starts[first_relay:])


async def test_automatic_updates_follow_the_switches(env, node_file, monkeypatch):  # noqa: F811
    e = env
    sid = await add_node(e, 'auto')
    calls = []

    async def update_node(owner, server_id, *, source='web', downgrade=False):
        calls.append((server_id, source))
        return {'ok': False, 'error': 'node_download_failed'} if len(calls) == 1 else {'ok': True, 'changed': True, 'confirmed': True}

    async def failing(owner, server_id, *, source='web', downgrade=False):
        calls.append((server_id, source))
        raise service_module.InstanceError('node_download_failed', 503)
    monkeypatch.setattr(e.servers, 'update_node', failing)
    settings(e, auto=False)
    assert await e.servers.auto_update() == [] and calls == []
    settings(e, auto=True, servers=False)  # 主机 off: nothing, even with the switch on
    assert await e.servers.auto_update() == [] and calls == []
    settings(e, auto=True)
    assert [i['serverId'] for i in await e.servers.auto_update()] == [sid] and calls == [(sid, 'auto')]
    # A build that failed to go onto a node is left alone for a while, not retried every pass.
    assert await e.servers.auto_update() == [] and len(calls) == 1
    e.servers.auto_failed = {k: v - service_module.AUTO_UPDATE_RETRY for k, v in e.servers.auto_failed.items()}
    monkeypatch.setattr(e.servers, 'update_node', update_node)
    assert len(await e.servers.auto_update()) == 1 and len(calls) == 2


async def test_a_node_coming_online_is_caught_up_when_automatic_updates_are_on(env, monkeypatch):  # noqa: F811
    e = env
    asked = []

    async def auto_update(server_ids=None):
        asked.append(server_ids)
        return []
    monkeypatch.setattr(e.servers, 'auto_update', auto_update)
    monkeypatch.setattr(service_module, 'AUTO_UPDATE_DELAY', 0)
    settings(e, auto=False)
    e.servers._auto_soon('s1')
    settings(e, auto=True)
    e.servers._auto_soon('s2')
    e.servers._auto_soon('s2')  # one catch-up per node at a time
    await asyncio.sleep(0.1)
    assert asked == [['s2']]


async def test_the_update_reminder_is_sent_once_per_version(env, monkeypatch):  # noqa: F811
    e = env
    settings(e)

    def listing(*states, source='github', version='1.2.0', exec_=True):
        items = [{'id': f's{n}', 'node': {'caps': {'exec': exec_}, 'update': {'state': s, 'updatable': s in ('required', 'available', 'mismatch'),
                                                                             'missing': ['mesh', 'net'] if s == 'required' else []}}}
                 for n, s in enumerate(states)]
        return {'items': items, 'node': {'available': True, 'source': source, 'version': version}}

    async def rows():
        return [(r['id'], r['text']) for r in await many(e.db.conn, "SELECT id,text FROM server_notifications WHERE kind='node_update' ORDER BY created_ms, id")]
    reminders = e.servers.reminders
    await reminders._node_updates(123, listing('available', 'current'))
    await reminders._node_updates(123, listing('available', 'available', 'current'))  # one more node behind: nothing new
    assert await rows() == [('node-update:123:1.2.0:optional', '有 1 台服务器的节点可以更新到 1.2.0。在服务器页点「全部更新」即可。')]
    await reminders._node_updates(123, listing('available', 'required', 'required'))
    texts = await rows()
    assert len(texts) == 2 and texts[1][1].startswith('有 3 台服务器的节点可以更新到 1.2.0，其中 2 台需要更新才能使用组网、网络测试')
    await reminders._node_updates(123, listing('available', 'required'))
    await reminders._node_updates(123, listing('available'))  # told they must; the rest needs no second word
    assert len(await rows()) == 2
    await reminders._node_updates(123, listing('available', version='1.3.0'))  # a new version: one more
    await reminders._node_updates(123, listing('available', source='local', version='1.4.0-dev.1'))  # local builds remind no one
    assert [r[0] for r in await rows()][-1] == 'node-update:123:1.3.0:optional' and len(await rows()) == 3
    # With automatic updates on, only nodes it cannot reach are counted.
    settings(e, auto=True)
    await reminders._node_updates(123, listing('available', version='1.5.0'))
    await reminders._node_updates(123, listing('available', version='1.6.0', exec_=False))
    assert [r[0] for r in await rows()][-1] == 'node-update:123:1.6.0:optional' and len(await rows()) == 4
    assert '关闭了命令执行' in (await rows())[-1][1]


async def test_nothing_about_node_versions_runs_with_the_servers_feature_off(env, monkeypatch):  # noqa: F811
    e = env
    sid = await add_node(e, 'quiet', version='0.1.0')
    called = []

    async def node_updates(owner, listing):
        called.append(owner)
    monkeypatch.setattr(e.servers.reminders, '_node_updates', node_updates)
    settings(e, servers=False)
    await e.servers.reminders.check()
    assert called == []
    settings(e)
    await e.servers.reminders.check()
    assert called == [123]
    # The daily GitHub check waits while 主机 is off.
    checks, sleeps = [], []

    async def check():
        checks.append(1)

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(releases, 'check', check)
    monkeypatch.setattr(releases, 'asyncio', SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await releases.loop(lambda: False)
    assert checks == [] and sleeps == [300, 300]
    assert sid


async def test_the_listing_carries_each_node_state_and_the_release_status(env):  # noqa: F811
    e = env
    settings(e, prerelease=True, auto=True)
    sid = await add_node(e, 'listed', features=ALL - {'net'})
    listing = await api(e, 'GET', '/api/servers')
    node = next(i for i in listing['items'] if i['id'] == sid)['node']
    assert node['update']['state'] == 'required' and node['update']['missing'] == ['net'] and node['updateAvailable']
    assert node['build'] == 'b' * 12 and node['source'] == 'release'
    release = listing['node']
    assert release['available'] and release['prerelease'] is True and release['autoUpdate'] is True and release['localProblem'] == ''
    assert [f['name'] for f in release['required']] == list(releases.REQUIRED_FEATURES)
    row = await one(e.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (sid,))
    assert json.loads(row['info_json']) == {}  # a fake session never said hello; the record is written on hello


async def test_an_older_target_goes_on_a_node_only_when_asked_and_is_named_a_downgrade(env, node_file, monkeypatch):  # noqa: F811
    """The shipped target is 1.0.0: a development node past it is shown, left alone by 全部更新 and
    自动更新, and downgraded only when that is asked for, through the page or the tool."""
    from app.tools.base import ToolRegistry, ToolRuntimeContext
    from app.tools.servers import register_server_tools
    e = env
    ahead = await add_node(e, 'ahead', version='1.1.0-dev.13')
    behind = await add_node(e, 'behind', version='1.0.0-dev.9')
    sent = []

    async def node_call(owner, server_id, method, params, timeout):
        sent.append(server_id)
        version = e.servers.hub.sessions[server_id].version
        e.servers.hub.sessions[server_id] = fake_session(server_id, version='1.0.0', build=params['sha256'])
        return {'changed': True, 'from': version, 'to': '1.0.0'}

    async def confirmed(server_id, before, sha256):
        return True
    monkeypatch.setattr(e.servers, '_node_call', node_call)
    monkeypatch.setattr(e.servers, '_confirm_build', confirmed)
    listing = {i['id']: i['node'] for i in (await api(e, 'GET', '/api/servers'))['items'] if i['node']}
    assert listing[ahead]['update']['state'] == 'ahead' and listing[ahead]['update']['downgrade'] and not listing[ahead]['updateAvailable']
    assert listing[behind]['update']['state'] == 'available' and not listing[behind]['update']['downgrade'] and listing[behind]['updateAvailable']
    # Automatic updates and 全部更新 take the development node behind the target, not the one past it.
    settings(e, auto=True)
    assert [i['serverId'] for i in await e.servers.auto_update()] == [behind] and sent == [behind]
    assert (await api(e, 'POST', '/api/servers/node-update'))['items'] == [] and sent == [behind]
    # Updating it without asking for a downgrade changes nothing and says why.
    result = await api(e, 'POST', f'/api/servers/{ahead}/node/update')
    assert result['changed'] is False and result['state'] == 'ahead' and result['downgrade'] is True and sent == [behind]
    # Asked for: it goes on, and the result and the log call it a downgrade.
    result = await api(e, 'POST', f'/api/servers/{ahead}/node/update', {'downgrade': True})
    assert result['changed'] and result['confirmed'] and result['downgrade'] is True and sent == [behind, ahead]
    log = [x['command'] for x in (await e.servers.get(123, ahead))['execLog']]
    assert log[0] == '降级节点 1.1.0-dev.13 → 1.0.0'
    assert (await e.servers.get(123, behind))['execLog'][0]['command'] == '更新节点 1.0.0-dev.9 → 1.0.0'

    # The tool: a downgrade names its servers, and the list says which nodes it would be.
    registry = ToolRegistry()
    register_server_tools(registry, e.servers)
    person = ToolRuntimeContext(chat_id=780, conversation_uuid='conv-d')
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-d',780,123,'t')")

    async def call(action, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=person))
    again = await add_node(e, 'again', version='1.2.0')
    row = next(r for r in (await call('list'))['items'] if r['id'] == again)['node']
    assert row['updateState'] == 'ahead' and row['downgrade'] is True and row['updateAvailable'] is False
    assert (await call('update_node', all=True, downgrade=True))['error'] == 'invalid_downgrade'
    assert (await call('update_node', group='x', downgrade=True))['error'] == 'invalid_downgrade'
    assert (await call('update_node', serverIds=[again], downgrade='yes'))['error'] == 'invalid_downgrade'
    assert (await call('update_node', serverIds=[again]))['items'] == [] and again not in sent
    items = (await call('update_node', serverIds=[again], downgrade=True))['items']
    assert [(i['serverId'], i['downgrade'], i['from'], i['to']) for i in items] == [(again, True, '1.2.0', '1.0.0')]
