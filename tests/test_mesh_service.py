"""Mesh through the real Web app and an isolated database; nodes are fakes that
answer the mesh.* calls the way the contract describes."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.instances.contracts import InstanceError
from app.notice_names import telegram_html
from app.servers import compat
from app.servers import hub as hub_module
from app.servers import mesh as mesh_module
from app.servers import mesh_config as config_module
from app.servers import schema as schema_module
from app.servers.mesh_config import config_hash
from app.webhooks.repository import many, one
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

OWNER = 123


class FakeWS:
    closed = False

    async def close(self, **kw):
        self.closed = True


class Nodes:
    """Stands in for hub.call: records every call, applies configs, fails on request."""

    def __init__(self, e):
        self.e, self.calls, self.configs, self.fail, self.refuse, self.tls, self.local = e, [], {}, {}, {}, {}, {}
        self.builds, self.upgrade_errors, self.upgrades, self.gate = {}, {}, [], None
        self.install_gates, self.install_errors = {}, {}
        self.addresses = {}  # localAddresses per node; a node left out sends none, as an older one would
        self.schemas = {}  # configSchemas per node in mesh.status; a node left out says this controller's, as a current one would

    async def call(self, server_id, method, params, timeout):
        if server_id not in self.e.servers.hub.sessions:
            raise LookupError('node_offline')
        self.calls.append((server_id, method, params))
        if method == 'mesh.install':
            if server_id in self.install_gates:
                await self.install_gates[server_id].wait()
            if server_id in self.install_errors:
                return {'ok': False, 'error': self.install_errors[server_id]}
            platform = self.e.servers.hub.sessions[server_id].info['platform']
            return {'ok': True, 'result': {'publicKey': (server_id[:8] + 'wg').ljust(43, 'A') + '=', 'controlKey': (server_id[:8] + 'c').ljust(43, 'B') + '=',
                                           **self._security_keys(server_id), 'platform': platform, 'dataVersion': '1.0.0', 'localPrefixes': self.local.get(server_id, []),
                                           **self._addresses(server_id)}}
        if method == 'mesh.status':
            return {'ok': True, 'result': {'installed': True, 'service': 'running', 'localPrefixes': self.local.get(server_id, []),
                                           'dataVersion': '1.0.0', 'dataBuild': self.builds.get(server_id, ''), **self._addresses(server_id),
                                           **self._security_keys(server_id), 'configSchemas': self.schemas.get(server_id, list(compat.CONFIG_SCHEMA))}}
        if method == 'mesh.upgrade':
            self.upgrades.append(('start', server_id))
            await asyncio.sleep(0.05)
            self.upgrades.append(('end', server_id))
            if server_id in self.upgrade_errors:
                return {'ok': False, 'error': self.upgrade_errors[server_id]}
            target = self.e.servers.hub.sessions[server_id].build
            changed, self.builds[server_id] = self.builds.get(server_id) != target, target
            return {'ok': True, 'result': {'from': '1.0.0', 'to': '1.1.0' if changed else '1.0.0', 'changed': changed}}
        if method == 'mesh.apply':
            revision = params['config']['revision']
            if self.gate:
                await self.gate.wait()
            if server_id in self.refuse:
                return {'ok': False, 'error': self.refuse[server_id]}
            if server_id in self.fail:
                return {'ok': True, 'result': {'appliedRevision': revision - 1, 'ready': False, 'error': self.fail[server_id]}}
            self.configs[server_id] = params['config']
            return {'ok': True, 'result': {'appliedRevision': revision, 'ready': True, 'error': ''}}
        if method == 'mesh.tls':
            state = self.tls[server_id]
            if params.get('activate'):
                assert params['activate'] == state['pending']['id']
                state['active'], state['pending'] = state['pending'], None
            return {'ok': True, 'result': state}
        if method == 'mesh.uninstall':
            return {'ok': True, 'result': {'removed': True}}
        return {'ok': False, 'error': 'unsupported_method'}

    @staticmethod
    def _security_keys(server_id):
        return {'signKey': (server_id[:8] + 'sg').ljust(43, 'C') + '=', 'e2eKey': (server_id[:8] + 'ee').ljust(43, 'D') + '='}

    def _addresses(self, server_id):
        return {'localAddresses': self.addresses[server_id]} if server_id in self.addresses else {}

    def sent(self, method):
        return [sid for sid, m, _ in self.calls if m == method]


async def server(e, name, *, platform='linux', ipv4='', exec_=True, features=('mesh',), online=True):
    sid = (await api(e, 'POST', '/api/servers', {'name': name}))['item']['id']
    info = {'platform': platform, 'ipv4': ipv4}
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('INSERT INTO server_nodes(server_id,owner,public_key,info_json,enrolled_ms) VALUES(?,?,?,?,1)',
                           (sid, OWNER, f'key-{sid}', json.dumps({'info': info, 'caps': {'exec': exec_}})))
    if online:
        await connect(e, sid, info, exec_=exec_, features=features)
    return sid


async def connect(e, sid, info=None, *, exec_=True, features=('mesh',), build=''):
    row = await one(e.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (sid,))
    info = info or json.loads(row['info_json'])['info']
    session = hub_module.Session(sid, FakeWS(), None, '127.0.0.1')
    session.caps, session.features, session.info = {'exec': exec_}, frozenset(features), dict(info)
    session.build = build
    e.servers.hub.sessions[sid] = session
    await e.servers.node_hello(sid, session.info, session.caps, 'test')
    return session


async def settled(e):
    for _ in range(200):
        await asyncio.sleep(0.02)
        tasks = [t for t in e.servers.tasks if t.get_coro().__qualname__.startswith('Mesh.') and 'tls_loop' not in t.get_coro().__qualname__]
        if not tasks and not any(r.running for r in e.servers.mesh.rollouts.values()):
            return await api(e, 'GET', '/api/servers/mesh')
    raise AssertionError('mesh work did not settle')


async def notifications(e):
    return [r['id'] for r in await many(e.db.conn, "SELECT id FROM server_notifications WHERE kind='mesh' ORDER BY created_ms")]


async def mesh_setup(e):
    nodes = Nodes(e)
    e.servers.hub.call = nodes.call
    ids = {
        'r1': await server(e, 'Berlin', ipv4='45.32.100.1'),
        'r2': await server(e, 'LAX', ipv4='45.32.100.2'),
        'm1': await server(e, '公司-Win', platform='windows', ipv4='45.32.100.3'),
        'm2': await server(e, '家-Mac', platform='darwin', ipv4='45.32.100.4'),
    }
    return nodes, ids


async def test_join_edit_apply_in_order_and_report_status(env, monkeypatch):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    old = await server(e, 'old', features=())
    no_exec = await server(e, 'noexec', exec_=False)
    offline = await server(e, 'away', online=False)
    first = await api(e, 'GET', '/api/servers/mesh')
    reasons = {i['serverId']: i['reason'] for i in first['eligible']}
    assert reasons[old] == '节点版本过旧，需要先更新' and reasons[no_exec] == '未开启执行命令' and reasons[offline] == '节点离线'
    assert reasons['local'] == '未安装节点' and reasons[ids['r1']] == '' and first['settings']['prefix'] == '100.96.0.0/16'

    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['r2'], ids['m1'], ids['m2'], old]})
    assert [i['ok'] for i in added['items']] == [True, True, True, True, False] and added['items'][4]['error'] == 'node_update_required'
    assert [(m['address'], m['controlAddress']) for m in added['members']] == [(f'100.96.0.{i}', f'100.96.255.{i}') for i in range(1, 5)]
    assert added['draft']['dirty'] and [c['kind'] for c in added['draft']['changes']] == ['member.add'] * 4
    assert set(nodes.sent('mesh.install')) == set(ids.values())

    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][1]['role'] = 'relay'
    draft['members'][2]['role'] = 'relay'  # Windows
    bad = await api(e, 'PUT', '/api/servers/mesh/draft', draft, status=400)
    assert bad['error'] == 'mesh_invalid' and {'path': 'members[2].role', 'error': 'relay_requires_linux'}.items() <= bad['errors'][0].items()
    draft['members'][2]['role'] = 'member'
    for i in (0, 1):
        draft['members'][i].update(role='relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0})
    draft['members'][2]['entry'] = {'mode': 'fixed', 'candidates': [{'relay': ids['r1'], 'transport': 'tcp'}, {'relay': ids['r2'], 'transport': 'udp'}],
                                    'autoReturn': True}
    draft['members'][3]['entry'] = {'mode': 'fixed', 'candidates': [{'relay': ids['r2'], 'transport': 'udp'}], 'autoReturn': True}
    draft['rules'] = [{'src': {'prefixes': ['100.96.0.0/16']}, 'dst': {'members': [ids['m2']]}, 'proto': 'icmp'}]
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    assert saved['members'][2]['entry']['candidates'][0]['id'] == 'c1' and saved['errors'] == []

    # Relays first, then the members; an offline one is marked and the rest go on.
    del e.servers.hub.sessions[ids['m2']]
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert nodes.sent('mesh.apply') == [ids['r1'], ids['r2'], ids['m1']]
    assert [(p['serverId'], p['state']) for p in state['apply']['progress']] == [
        (ids['r1'], 'done'), (ids['r2'], 'done'), (ids['m1'], 'done'), (ids['m2'], 'offline')]
    assert state['applied']['revision'] == 1 and not state['draft']['dirty']
    assert state['nodes'][ids['m2']]['state'] == 'offline' and state['nodes'][ids['m1']]['state'] == 'degraded'
    m1 = nodes.configs[ids['m1']]
    assert m1['entry']['candidates'][0]['endpoint'] == 'tcp://45.32.100.1:52000' and m1['nodes'][0]['publicKey']
    assert nodes.configs[ids['r1']]['nextHops'] == {ids['r2']: ids['r2']}

    # Offline: it catches up when its node says hello again.
    await connect(e, ids['m2'])
    state = await settled(e)
    assert nodes.sent('mesh.apply')[-1] == ids['m2'] and nodes.configs[ids['m2']]['revision'] == 1
    assert state['apply']['progress'][-1] == {'serverId': ids['m2'], 'state': 'done', 'error': ''}
    assert nodes.configs[ids['m2']]['rules'] == [{'src': ['100.96.0.0/16'], 'dst': ['100.96.0.4/32'], 'proto': 'icmp', 'ports': []}]

    # Status from the node's report, through the hub.
    session = e.servers.hub.sessions[ids['m1']]
    report = {'schema': 1, 'startedAt': 5, 'appliedRevision': 1, 'ready': True, 'iface': 'obmesh0', 'error': '',
              'peers': [{'id': ids['r1'], 'kind': 'entry', 'active': True, 'transport': 'tcp', 'rttMs': 151.2}],
              'entry': {'candidate': 'c1', 'relay': ids['r1'], 'transport': 'tcp', 'since': 1, 'switches': 0},
              'resources': {'rssBytes': 50 * 1024 ** 2}, 'events': [{'seq': 1, 'at': 10, 'kind': 'peer.up', 'detail': 'r1'}]}
    await e.servers.hub._message(session, {'t': 'report', 'm': {}, 'mesh': report})
    await e.servers.hub._message(session, {'t': 'report', 'm': {}, 'mesh': {**report, 'events': [{'seq': 1, 'at': 10, 'kind': 'peer.up'},
                                                                                                  {'seq': 2, 'at': 11, 'kind': 'peer.down'}]}})
    state = await api(e, 'GET', '/api/servers/mesh')
    assert state['nodes'][ids['m1']]['state'] == 'ready'
    assert state['nodes'][ids['m1']]['summary'] == {'entry': report['entry'], 'ready': True, 'peers': 1, 'rttMs': 151.2,
                                                   'memory': {'rssBytes': 50 * 1024 ** 2, 'limitBytes': 160 * 1024 ** 2, 'level': ''},
                                                   'direct': {}, 'reachability': None, 'entryHealth': [], 'routing': None}
    detail = await api(e, 'GET', f'/api/servers/mesh/members/{ids["m1"]}')
    assert [ev['seq'] for ev in detail['events']] == [2, 1] and detail['status']['iface'] == 'obmesh0'
    assert detail['config']['revision'] == 1 and detail['member']['name'] == '公司-Win'
    # Saved with the first report; the change right after waits STATUS_SAVE_MIN_MS, then goes with the next report.
    stored = await one(e.db.conn, 'SELECT events_json FROM mesh_nodes WHERE server_id=?', (ids['m1'],))
    assert len(json.loads(stored['events_json'])) == 1
    e.servers.mesh.status[ids['m1']]['saved'] -= mesh_module.STATUS_SAVE_MIN_MS
    await e.servers.hub._message(session, {'t': 'report', 'm': {}, 'mesh': {**report, 'events': []}})
    stored = await one(e.db.conn, 'SELECT events_json FROM mesh_nodes WHERE server_id=?', (ids['m1'],))
    assert len(json.loads(stored['events_json'])) == 2
    # Punched direct peers are in the summary, so the page needs no member detail for them.
    await e.servers.mesh.report(ids['m2'], {**report, 'events': [], 'peers': [
        *report['peers'],
        {'id': ids['m1'], 'kind': 'direct', 'active': True, 'transport': 'udp', 'rttMs': 12.5,
         'direct': {'state': 'direct', 'via': 'lan', 'since': 7, 'attempts': 1, 'lastError': ''}},
        {'id': 'gw', 'kind': 'direct', 'active': False, 'direct': {'state': 'punching', 'via': 'wan', 'attempts': 4, 'lastError': 'timeout'}},
        {'id': 'nat', 'kind': 'direct', 'direct': {'state': 'unknown'}}]})
    summary = (await api(e, 'GET', '/api/servers/mesh'))['nodes'][ids['m2']]['summary']
    assert summary['direct'] == {ids['m1']: {'state': 'direct', 'via': 'lan', 'rttMs': 12.5}, 'gw': {'state': 'punching', 'via': '', 'rttMs': None}}

    # The path from the latest reports: m1 is on r1, which it reports a round trip to.
    path = await api(e, 'GET', f'/api/servers/mesh/path?src={ids["m1"]}&dst={ids["r1"]}')
    assert path == {'ok': True, 'hops': [{'node': ids['m1'], 'via': '', 'rttMs': None}, {'node': ids['r1'], 'via': 'entry', 'rttMs': 151.2}],
                    'rttMs': 151.2, 'stale': False}
    # r1 reports no routes, so its configured next hop is taken; nobody reports the r1-r2 round trip.
    path = await api(e, 'GET', f'/api/servers/mesh/path?src={ids["m1"]}&dst={ids["r2"]}')
    assert [h['node'] for h in path['hops']] == [ids['m1'], ids['r1'], ids['r2']] and path['stale'] and path['rttMs'] is None
    assert (await api(e, 'GET', f'/api/servers/mesh/path?src={ids["m1"]}&dst=nope', status=404))['error'] == 'mesh_member_not_found'
    assert (await api(e, 'GET', f'/api/servers/mesh/path?src={ids["m1"]}&dst={ids["m1"]}', status=400))['error'] == 'invalid_mesh_path'

    # Documents stored before relayMode, bias and reachability read as their defaults: nothing to apply.
    async with e.db.write_transaction(label='test') as conn:
        for table, column in (('mesh_members', 'member_json'), ('mesh_links', 'link_json'), ('mesh_settings', 'settings_json')):
            for row in await many(conn, f'SELECT rowid, {column} FROM {table}'):
                value = json.loads(row[column])
                for key in ('relayMode', 'bias', 'reachability'):
                    value.pop(key, None)
                await conn.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (json.dumps(value), row['rowid']))
    state = await api(e, 'GET', '/api/servers/mesh')
    assert not state['draft']['dirty'] and {m['relayMode'] for m in state['members']} == {'public'}
    assert state['settings']['reachability'] == {'stun': []} and state['warnings'] == []

    # Alerts: memory over the limit, three switches in five minutes, no entry for a minute.
    now = mesh_module.now_ms()
    switches = [{'seq': 10 + i, 'at': now - 1000 * i, 'kind': 'entry.switch', 'detail': 'c1 -> c2'} for i in range(3)]
    await e.servers.mesh.report(ids['m1'], {**report, 'resources': {'rssBytes': 200 * 1024 ** 2}, 'events': switches})
    lost = {**report, 'entry': None, 'peers': [], 'ready': False, 'events': []}
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: now)
    await e.servers.mesh.report(ids['m1'], lost)
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: now + 61_000)
    await e.servers.mesh.report(ids['m1'], lost)
    await e.servers.mesh.report(ids['m1'], lost)
    kinds = [n.split(':')[0] for n in await notifications(e)]
    assert sorted(kinds) == ['mesh-entry', 'mesh-flap', 'mesh-memory']


async def test_failure_stops_the_rollout_and_retry_and_rollback(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = 'relay'
    draft['members'][1]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    draft['members'][2]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert nodes.sent('mesh.apply') == [ids['r1'], ids['m1'], ids['m2']]

    # Revision 2 moves the relay's UDP port; the relay refuses it, nobody else gets it.
    nodes.calls.clear()
    draft['members'][0]['listen'] = {'udp': 51000}
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    nodes.fail[ids['r1']] = 'listen udp :51000: address already in use'
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert nodes.sent('mesh.apply') == [ids['r1']]
    assert [p['state'] for p in state['apply']['progress']] == ['failed', 'pending', 'pending']
    assert state['apply']['progress'][1]['error'] == 'stopped' and state['nodes'][ids['r1']]['state'] == 'failed'
    assert state['nodes'][ids['m1']]['state'] == 'degraded' and state['nodes'][ids['m1']]['error'] == ''
    assert nodes.configs[ids['m1']]['revision'] == 1  # still the last working config
    revision = await one(e.db.conn, 'SELECT status FROM mesh_revisions WHERE owner=? AND revision=2', (OWNER,))
    assert revision['status'] == 'failed' and await notifications(e) == [f'mesh-apply:{OWNER}:2:{ids["r1"]}']

    # Retry: same revision, only the nodes that still need it.
    del nodes.fail[ids['r1']]
    nodes.calls.clear()
    state = await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['applied']['revision'] == 2 and nodes.sent('mesh.apply') == [ids['r1'], ids['m1'], ids['m2']]
    assert nodes.configs[ids['m1']]['entry']['candidates'][0]['endpoint'] == 'udp://45.32.100.1:51000'

    # Rollback: revision 1 becomes the draft and goes out as revision 3.
    await api(e, 'POST', '/api/servers/mesh/rollback')
    state = await settled(e)
    assert state['applied']['revision'] == 3 and state['members'][0]['listen']['udp'] == 51999
    assert nodes.configs[ids['m1']]['entry']['candidates'][0]['endpoint'] == 'udp://45.32.100.1:51999'
    # Retrying with nothing to send changes nothing.
    nodes.calls.clear()
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert nodes.calls == [] and state['applied']['revision'] == 3

    # A config the node refuses to write keeps the node's whole reason; a data plane timeout is a failure too.
    reason = 'invalid_config: entry.candidates[0].endpoint: tls endpoint needs at least one pin of 64 hex digits'
    nodes.refuse[ids['m1']] = reason
    draft = {k: state[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][1].update(tags=['公司'], uploadLimitMbps=10)
    draft['rules'] = [{'src': {'tags': ['公司']}, 'dst': {'members': [ids['m2']]}, 'proto': 'tcp', 'ports': [[22, 22]]}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['apply']['progress'][0] == {'serverId': ids['m1'], 'state': 'failed', 'error': reason}
    assert state['nodes'][ids['m1']] == {**state['nodes'][ids['m1']], 'state': 'failed', 'error': reason}
    del nodes.refuse[ids['m1']]
    nodes.fail[ids['m1']] = 'apply_timeout'
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['apply']['progress'][0]['error'] == 'apply_timeout' and state['apply']['progress'][1]['error'] == 'stopped'


async def test_removal_revokes_first_then_uninstalls_and_delete_leaves(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['r2'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = draft['members'][1]['role'] = 'relay'
    draft['members'][2]['entry']['candidates'] = [{'relay': ids['r2'], 'transport': 'udp'}, {'relay': ids['r1'], 'transport': 'udp'}]
    draft['members'][3]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    draft['rules'] = [{'src': {'members': [ids['m1']]}, 'dst': {'members': [ids['m2']]}, 'proto': 'any'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert nodes.configs[ids['m2']]['nodes'][0]['id'] in ids.values()

    # Removing r2 takes it out of m1's candidates in the draft; it is uninstalled after everyone dropped it.
    removed = await api(e, 'DELETE', f'/api/servers/mesh/members/{ids["r2"]}')
    assert removed['members'][1]['removing'] and removed['members'][2]['entry']['candidates'] == [{'id': 'c2', 'relay': ids['r1'], 'transport': 'udp'}]
    assert {'kind': 'member.remove', 'serverId': ids['r2'], 'text': '移除成员 LAX'} in removed['draft']['changes']
    nodes.calls.clear()
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert [(sid, m) for sid, m, _ in nodes.calls] == [(ids['r1'], 'mesh.apply'), (ids['m1'], 'mesh.apply'), (ids['m2'], 'mesh.apply'),
                                                       (ids['r2'], 'mesh.uninstall')]
    assert all(ids['r2'] not in json.dumps(nodes.configs[s]) for s in (ids['r1'], ids['m1'], ids['m2']))
    assert ids['r2'] not in state['nodes'] and not await one(e.db.conn, 'SELECT 1 FROM mesh_nodes WHERE server_id=?', (ids['r2'],))
    assert ids['r2'] in {i['serverId'] for i in state['eligible']}

    # An offline member being removed stays to be cleaned up and is uninstalled when it returns.
    await api(e, 'DELETE', f'/api/servers/mesh/members/{ids["m1"]}')
    del e.servers.hub.sessions[ids['m1']]
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['nodes'][ids['m1']]['state'] == 'cleanup' and state['apply']['progress'][-1]['state'] == 'offline'
    await connect(e, ids['m1'])
    state = await settled(e)
    assert nodes.calls[-1][:2] == (ids['m1'], 'mesh.uninstall') and ids['m1'] not in state['nodes']
    assert nodes.configs[ids['m2']]['rules'] == []

    # Deleting a server cuts its node off first; the others drop it afterwards, without waiting on it.
    nodes.calls.clear()
    await api(e, 'DELETE', f'/api/servers/{ids["m2"]}')
    assert ids['m2'] not in e.servers.hub.sessions
    state = await settled(e)
    assert [(sid, m) for sid, m, _ in nodes.calls] == [(ids['r1'], 'mesh.apply')]
    assert nodes.configs[ids['r1']]['nodes'] == []
    assert [m['serverId'] for m in state['members']] == [ids['r1']] and list(state['nodes']) == [ids['r1']]


async def test_automatic_address_changes_and_tls_rotation_reissue(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0)
    nodes, ids = await mesh_setup(e)
    nodes.tls[ids['r1']] = {'active': {'id': 'cert-1', 'fingerprint': 'aa' * 32, 'notBefore': 1793000000000, 'notAfter': 1800776000000}, 'pending': None}
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0].update(role='relay', listen={'udp': 51999, 'tcp': 0, 'tls': 52001})
    draft['members'][1]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'tls'}]
    draft['members'][2]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)

    # The relay must be online to give its certificate when the change is applied.
    del e.servers.hub.sessions[ids['r1']]
    refused = await api(e, 'POST', '/api/servers/mesh/apply', status=400)
    assert refused['errors'][0]['path'] == 'members[1].entry.candidates[0]' and refused['errors'][0]['error'] == 'tls_fingerprint_missing'
    await connect(e, ids['r1'])
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert nodes.configs[ids['m1']]['entry']['candidates'][0] == {'id': 'c1', 'relay': ids['r1'], 'endpoint': 'tls://45.32.100.1:52001',
                                                                  'tlsPins': ['aa' * 32]}

    # The relay's reported address changes: a new revision reaches the members on its own.
    nodes.calls.clear()
    await connect(e, ids['r1'], {'platform': 'linux', 'ipv4': '45.32.100.9'})
    state = await settled(e)
    assert state['applied']['revision'] == 2 and sorted(nodes.sent('mesh.apply')) == sorted([ids['m1'], ids['m2']])
    assert nodes.configs[ids['m2']]['entry']['candidates'][0]['endpoint'] == 'udp://45.32.100.9:51999'
    assert not state['draft']['dirty']

    # Daily check: a pending certificate is pinned next to the active one, then activated.
    nodes.calls.clear()
    nodes.tls[ids['r1']]['pending'] = {'id': 'cert-2', 'fingerprint': 'bb' * 32, 'notBefore': 1798000000000, 'notAfter': 1805776000000}
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('UPDATE mesh_nodes SET tls_checked_ms=0')
    await e.servers.mesh.check_tls(OWNER)
    await settled(e)
    assert nodes.configs[ids['m1']]['entry']['candidates'][0]['tlsPins'] == ['aa' * 32, 'bb' * 32]
    assert [(sid, m, p) for sid, m, p in nodes.calls if m == 'mesh.tls'] == [(ids['r1'], 'mesh.tls', {}), (ids['r1'], 'mesh.tls', {'activate': 'cert-2'})]
    row = await one(e.db.conn, 'SELECT tls_json FROM mesh_nodes WHERE server_id=?', (ids['r1'],))
    assert json.loads(row['tls_json']) == {'active': nodes.tls[ids['r1']]['active'], 'pending': None}
    assert json.loads(row['tls_json'])['active'] == {'id': 'cert-2', 'fingerprint': 'bb' * 32, 'notBefore': 1798000000000, 'notAfter': 1805776000000}
    # The old pin goes with the next delivery.
    await e.servers.mesh.reconcile(OWNER)
    await settled(e)
    assert nodes.configs[ids['m1']]['entry']['candidates'][0]['tlsPins'] == ['bb' * 32]
    configs = (await one(e.db.conn, 'SELECT configs_json FROM mesh_revisions WHERE owner=? ORDER BY revision DESC LIMIT 1', (OWNER,)))['configs_json']
    rows = {r['server_id']: r['confirmed_hash'] for r in await many(e.db.conn, 'SELECT server_id, confirmed_hash FROM mesh_nodes')}
    assert all(rows[sid] == config_hash(c) for sid, c in json.loads(configs).items())



async def test_a_punched_link_follows_the_public_ip_a_node_verified(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0)
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0].update(role='relay', listen={'udp': 51999, 'tcp': 0, 'tls': 0})
    for m in draft['members'][1:]:
        m['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    draft['directLinks'] = [{'a': ids['m1'], 'b': ids['m2'], 'transport': 'udp', 'dialer': 'a'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    # The panel's IPv4 of either end (45.32.100.3/4) is not an endpoint.
    assert nodes.configs[ids['m2']]['links'] == [{'peer': ids['m1'], 'kind': 'direct', 'dial': False, 'punch': True}]
    assert nodes.configs[ids['m1']]['links'] == [{'peer': ids['m2'], 'kind': 'direct', 'dial': True, 'punch': True}]

    # m1's own check finds it public, every observer agreeing: m2 gets it, at m1's UDP port, in a new revision.
    def report(**reach):
        return {'startedAt': 1, 'ready': True, 'appliedRevision': 1, 'configSchemas': list(compat.CONFIG_SCHEMA), 'reachability': {
            'verdict': 'public', 'publicAddr': '45.32.100.30:40000', 'consistent': True, 'checkedAt': 1, **reach}}
    # Said public at an address that is not a global one: nobody is sent there.
    for private in ('10.1.2.3', '192.168.1.20', '100.64.0.9'):
        await e.servers.mesh.report(ids['m1'], report(publicAddr=f'{private}:40000'))
        assert (await settled(e))['applied']['revision'] == 1 and 'endpoint' not in nodes.configs[ids['m2']]['links'][0]
    await e.servers.mesh.report(ids['m1'], report())
    state = await settled(e)
    assert state['applied']['revision'] == 2 and not state['draft']['dirty']
    assert nodes.configs[ids['m2']]['links'][0]['endpoint'] == 'udp://45.32.100.30:51999'
    assert 'endpoint' not in nodes.configs[ids['m1']]['links'][0]
    # The same again changes nothing; observers disagreeing takes it back.
    await e.servers.mesh.report(ids['m1'], report())
    assert (await settled(e))['applied']['revision'] == 2
    await e.servers.mesh.report(ids['m1'], report(consistent=False))
    assert (await settled(e))['applied']['revision'] == 3 and 'endpoint' not in nodes.configs[ids['m2']]['links'][0]


async def test_a_public_address_that_keeps_changing_is_reissued_once_it_holds(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0.3)
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0].update(role='relay', listen={'udp': 51999, 'tcp': 0, 'tls': 0})
    for m in draft['members'][1:]:
        m['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    draft['directLinks'] = [{'a': ids['m1'], 'b': ids['m2'], 'transport': 'udp', 'dialer': 'a'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)

    def report(addr):
        return {'startedAt': 1, 'ready': True, 'appliedRevision': 1, 'configSchemas': list(compat.CONFIG_SCHEMA), 'reachability': {
            'verdict': 'public', 'publicAddr': f'{addr}:40000', 'consistent': True, 'checkedAt': 1}}
    # Three addresses in a row, each within the wait: one reissue, for the last one.
    for addr in ('45.32.100.30', '45.32.100.31', '45.32.100.32'):
        await e.servers.mesh.report(ids['m1'], report(addr))
        await asyncio.sleep(0.1)
    assert (await api(e, 'GET', '/api/servers/mesh'))['applied']['revision'] == 1
    state = await settled(e)
    assert state['applied']['revision'] == 2
    assert nodes.configs[ids['m2']]['links'][0]['endpoint'] == 'udp://45.32.100.32:51999'


async def test_a_public_address_flipping_between_hellos_reissues_nothing_until_one_holds(env, monkeypatch):  # noqa: F811
    """A hello reissues with the facts it reads: the public address read there must have held too,
    or one switched back and forth between hellos reissues to those punching towards it each time."""
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0.5)
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0].update(role='relay', listen={'udp': 51999, 'tcp': 0, 'tls': 0})
    for m in draft['members'][1:]:
        m['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    draft['directLinks'] = [{'a': ids['m1'], 'b': ids['m2'], 'transport': 'udp', 'dialer': 'a'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)

    # What it runs, as its reports say: a report behind the latest would have it sent again.
    def report(addr, revision=2):
        return {'startedAt': 1, 'ready': True, 'appliedRevision': revision, 'configSchemas': list(compat.CONFIG_SCHEMA), 'reachability': {
            'verdict': 'public', 'publicAddr': f'{addr}:40000', 'consistent': True, 'checkedAt': 1}}
    await e.servers.mesh.report(ids['m1'], report('45.32.100.30', 1))
    assert (await settled(e))['applied']['revision'] == 2
    before = list(nodes.sent('mesh.apply'))
    for i in range(6):
        await e.servers.mesh.report(ids['m1'], report('45.32.100.31' if i % 2 == 0 else '45.32.100.30'))
        await connect(e, ids['m1'])
        await asyncio.sleep(0.05)
    state = await settled(e)
    assert state['applied']['revision'] == 2 and nodes.sent('mesh.apply') == before
    assert nodes.configs[ids['m2']]['links'][0]['endpoint'] == 'udp://45.32.100.30:51999'
    # One that holds goes out once, even with a hello in the wait.
    await e.servers.mesh.report(ids['m1'], report('45.32.100.31'))
    await connect(e, ids['m1'])
    assert (await api(e, 'GET', '/api/servers/mesh'))['applied']['revision'] == 2
    state = await settled(e)
    assert state['applied']['revision'] == 3 and nodes.sent('mesh.apply')[len(before):] == [ids['m2']]
    assert nodes.configs[ids['m2']]['links'][0]['endpoint'] == 'udp://45.32.100.31:51999'


async def test_discard_uninstalls_members_never_applied_and_tracks_local_prefixes(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    nodes.local[ids['m1']] = ['10.0.0.0/24']
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = 'relay'
    draft['members'][1]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    row = await one(e.db.conn, 'SELECT local_json FROM mesh_nodes WHERE server_id=?', (ids['m1'],))
    assert json.loads(row['local_json']) == ['10.0.0.0/24']

    # Two members installed for a draft that is thrown away: the online one is uninstalled now,
    # the offline one is left to clean up and uninstalled when it reconnects. Applied members stay.
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['m2'], ids['r2']]})
    del e.servers.hub.sessions[ids['r2']]
    nodes.calls.clear()
    state = await api(e, 'POST', '/api/servers/mesh/discard')
    assert not state['draft']['dirty'] and [m['serverId'] for m in state['members']] == [ids['r1'], ids['m1']]
    state = await settled(e)
    assert [(sid, m) for sid, m, _ in nodes.calls] == [(ids['m2'], 'mesh.uninstall')]
    assert ids['m2'] not in state['nodes'] and state['nodes'][ids['r2']]['state'] == 'cleanup'
    assert not await one(e.db.conn, 'SELECT 1 FROM mesh_nodes WHERE server_id=?', (ids['m2'],))
    assert {ids['m2'], ids['r2']} <= {i['serverId'] for i in state['eligible']}
    await connect(e, ids['r2'])
    state = await settled(e)
    assert nodes.calls[-1][:2] == (ids['r2'], 'mesh.uninstall') and ids['r2'] not in state['nodes']
    assert set(state['nodes']) == {ids['r1'], ids['m1']} and state['applied']['revision'] == 1

    # A reconnecting member reports its interfaces through mesh.status; a conflict shows on the draft.
    nodes.local[ids['m1']] = ['100.96.7.0/24', 'not-a-prefix', 'fe80::/64', 7]
    await connect(e, ids['m1'])
    state = await settled(e)
    assert (ids['m1'], 'mesh.status', {}) in nodes.calls
    row = await one(e.db.conn, 'SELECT local_json FROM mesh_nodes WHERE server_id=?', (ids['m1'],))
    assert json.loads(row['local_json']) == ['100.96.7.0/24']
    # What an applied member says of its own machine warns; it does not hold up edits that change neither it nor the prefix.
    assert state['errors'] == [] and [(x['path'], x['error']) for x in state['warnings'] if x['error'] == 'mesh_prefix_overlap'] == [('members[1]', 'mesh_prefix_overlap')]
    await api(e, 'PUT', '/api/servers/mesh/draft', {'settings': {'mtu': 1300}})
    err = await api(e, 'PUT', '/api/servers/mesh/draft', {'settings': {'prefix': '100.96.0.0/20'}}, status=400)
    assert [(x['path'], x['error']) for x in err['errors']] == [('settings.prefix', 'prefix_conflict')]


async def test_a_failed_delivery_shows_until_the_node_runs_the_latest_config(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = 'relay'
    draft['members'][1]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    report = {'schema': 1, 'startedAt': 5, 'appliedRevision': 1, 'ready': True, 'iface': 'obmesh0', 'error': '', 'peers': [], 'entry': None,
              'resources': {}, 'events': [], 'configSchemas': list(compat.CONFIG_SCHEMA)}
    await e.servers.mesh.report(ids['m1'], report)
    assert (await api(e, 'GET', '/api/servers/mesh'))['nodes'][ids['m1']]['state'] == 'ready'

    # Only m1's config changes and it is refused; its own report still says ready on the old config.
    original = json.loads(json.dumps(draft))
    draft['members'][1]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    nodes.fail[ids['m1']] = 'shaper: tc not found'
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    await e.servers.mesh.report(ids['m1'], report)
    state = await api(e, 'GET', '/api/servers/mesh')
    assert state['apply']['progress'] == [{'serverId': ids['m1'], 'state': 'failed', 'error': 'shaper: tc not found'}]
    assert state['nodes'][ids['m1']]['state'] == 'failed' and state['nodes'][ids['m1']]['error'] == 'shaper: tc not found'
    assert state['nodes'][ids['r1']]['state'] != 'failed'
    del e.servers.hub.sessions[ids['m1']]
    assert (await api(e, 'GET', '/api/servers/mesh'))['nodes'][ids['m1']]['state'] == 'failed'

    # Reverting makes the latest config the one it already runs: nothing is sent and it is ready again.
    await connect(e, ids['m1'])
    await settled(e)
    await api(e, 'PUT', '/api/servers/mesh/draft', original)
    nodes.calls.clear()
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['applied']['revision'] == 3 and nodes.sent('mesh.apply') == []
    assert state['nodes'][ids['m1']] == {**state['nodes'][ids['m1']], 'state': 'ready', 'error': ''}

    # A failure, then a successful delivery.
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    assert (await settled(e))['nodes'][ids['m1']]['state'] == 'failed'
    del nodes.fail[ids['m1']]
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    await e.servers.mesh.report(ids['m1'], {**report, 'appliedRevision': 4})
    state = await api(e, 'GET', '/api/servers/mesh')
    assert state['nodes'][ids['m1']]['state'] == 'ready' and state['nodes'][ids['m1']]['error'] == ''


OLD, NEW, NEWER = 'a' * 64, 'b' * 64, 'c' * 64
READY = {'schema': 1, 'startedAt': 9, 'appliedRevision': 1, 'ready': True, 'iface': 'obmesh0', 'error': '', 'peers': [], 'entry': None,
         'resources': {}, 'events': [], 'configSchemas': list(compat.CONFIG_SCHEMA)}


async def until(check):
    for _ in range(250):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError('condition not reached')


async def ready_after_upgrade(e, sid):
    await until(lambda: sid in e.servers.mesh.ready_waits)
    await e.servers.mesh.report(sid, READY)


async def upgrade_setup(e):
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['r2'], ids['m1']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = draft['members'][1]['role'] = 'relay'
    draft['members'][2]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    nodes.builds = {ids[k]: OLD for k in ('r1', 'r2', 'm1')}
    nodes.calls.clear()
    return nodes, ids, draft


async def test_relays_upgrade_one_at_a_time_after_a_node_update(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'UPGRADE_READY_WAIT', 10)
    monkeypatch.setattr(mesh_module, 'MEMBER_UPGRADE_SETTLE', 0.05)
    nodes, ids, _ = await upgrade_setup(e)
    relays = {ids['r1'], ids['r2']}
    for key in ('r1', 'r2', 'm1'):
        await connect(e, ids[key], build=NEW)
    await until(lambda: any(s == 'end' and sid in relays for s, sid in nodes.upgrades))
    await asyncio.sleep(0.2)
    first = next(sid for s, sid in nodes.upgrades if s == 'start' and sid in relays)
    second = (relays - {first}).pop()
    # The second relay waits until the first reports ready, the member until both are done: its
    # entry relay restarting under it would keep it from getting ready.
    assert ('start', second) not in nodes.upgrades and ('start', ids['m1']) not in nodes.upgrades
    nodes_view = (await api(e, 'GET', '/api/servers/mesh'))['nodes']
    assert nodes_view[first]['upgrade']['state'] == 'upgrading' and nodes_view[second]['upgrade']['state'] == 'waiting'
    assert nodes_view[ids['m1']]['upgrade']['state'] == 'waiting'
    await ready_after_upgrade(e, first)
    await ready_after_upgrade(e, second)
    await until(lambda: ('end', ids['m1']) in nodes.upgrades)
    state = await settled(e)
    assert nodes.upgrades.index(('end', first)) < nodes.upgrades.index(('start', second))
    assert nodes.upgrades.index(('end', second)) < nodes.upgrades.index(('start', ids['m1']))
    assert all(state['nodes'][ids[k]]['dataVersion'] == '1.1.0' and state['nodes'][ids[k]]['upgrade']['state'] == '' for k in ('r1', 'r2', 'm1'))
    rows = await many(e.db.conn, 'SELECT data_build FROM mesh_nodes')
    assert {r['data_build'] for r in rows} == {NEW}

    # Same build as the copy: nothing to do on the next reconnect.
    nodes.upgrades.clear()
    for key in ('r1', 'm1'):
        await connect(e, ids[key], build=NEW)
    await settled(e)
    assert nodes.upgrades == []


async def test_upgrade_waits_for_a_rollout_and_skips_the_same_build(env):  # noqa: F811
    e = env
    nodes, ids, draft = await upgrade_setup(e)
    # The copy already has the node's build (known from mesh.status): no upgrade.
    nodes.builds[ids['m1']] = NEW
    await connect(e, ids['m1'], build=NEW)
    await settled(e)
    assert nodes.sent('mesh.upgrade') == [] and nodes.sent('mesh.status') == [ids['m1']]

    nodes.gate = asyncio.Event()
    draft['members'][2]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await until(lambda: ids['m1'] in nodes.sent('mesh.apply'))
    await connect(e, ids['r2'], build=NEW)
    await asyncio.sleep(0.3)
    assert nodes.sent('mesh.upgrade') == []
    nodes.gate.set()
    await ready_after_upgrade(e, ids['r2'])
    await settled(e)
    methods = [(sid, m) for sid, m, _ in nodes.calls]
    assert methods.index((ids['r2'], 'mesh.upgrade')) > methods.index((ids['m1'], 'mesh.apply'))
    assert nodes.builds[ids['r2']] == NEW


async def test_an_upgrade_that_rolled_back_alerts_once_and_waits_for_a_new_build(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'MEMBER_UPGRADE_SETTLE', 0)
    monkeypatch.setattr(mesh_module, 'UPGRADE_RETRY_DELAY', 0.05)
    nodes, ids, _ = await upgrade_setup(e)
    nodes.upgrade_errors[ids['m1']] = 'upgrade_not_ready'
    await connect(e, ids['m1'], build=NEW)
    state = await settled(e)
    # Tried once more before it is given up.
    assert nodes.sent('mesh.upgrade') == [ids['m1'], ids['m1']]
    assert state['nodes'][ids['m1']]['upgrade']['error'] == 'upgrade_not_ready' and nodes.builds[ids['m1']] == OLD
    assert await notifications(e) == [f"mesh-upgrade:{ids['m1']}:{NEW}"]
    row = await one(e.db.conn, 'SELECT text FROM server_notifications WHERE id=?', (f"mesh-upgrade:{ids['m1']}:{NEW}",))
    assert row['text'] == '组网：「公司-Win」的组网服务升级失败，已回退到原版本。'
    events = (await api(e, 'GET', f"/api/servers/mesh/members/{ids['m1']}"))['events']
    assert events[0]['kind'] == 'upgrade.failed'

    # The same build is not tried again, and the alert is not repeated.
    await connect(e, ids['m1'], build=NEW)
    await settled(e)
    assert nodes.sent('mesh.upgrade') == [ids['m1'], ids['m1']] and len(await notifications(e)) == 1

    # Other errors are tried again on the next reconnect.
    nodes.upgrade_errors[ids['r1']] = 'executable_unknown'
    await connect(e, ids['r1'], build=NEW)
    state = await settled(e)
    assert state['nodes'][ids['r1']]['upgrade']['error'] == 'executable_unknown'
    del nodes.upgrade_errors[ids['r1']]
    await connect(e, ids['r1'], build=NEW)
    await ready_after_upgrade(e, ids['r1'])
    state = await settled(e)
    assert nodes.sent('mesh.upgrade') == [ids['m1'], ids['m1'], ids['r1'], ids['r1']] and state['nodes'][ids['r1']]['upgrade']['error'] == ''

    # A newer build is tried, and this one comes up.
    del nodes.upgrade_errors[ids['m1']]
    await connect(e, ids['m1'], build=NEWER)
    state = await settled(e)
    assert nodes.sent('mesh.upgrade')[-1] == ids['m1'] and nodes.builds[ids['m1']] == NEWER
    assert state['nodes'][ids['m1']]['upgrade']['error'] == '' and len(await notifications(e)) == 1


async def test_an_upgrade_that_did_not_get_ready_once_is_tried_again(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'MEMBER_UPGRADE_SETTLE', 0)
    monkeypatch.setattr(mesh_module, 'UPGRADE_RETRY_DELAY', 0.3)
    nodes, ids, _ = await upgrade_setup(e)
    nodes.upgrade_errors[ids['m1']] = 'upgrade_not_ready'
    await connect(e, ids['m1'], build=NEW)
    await until(lambda: ('end', ids['m1']) in nodes.upgrades)
    # Its entry relay back by then, the second try comes up: no alert, nothing given up.
    del nodes.upgrade_errors[ids['m1']]
    state = await settled(e)
    assert nodes.sent('mesh.upgrade') == [ids['m1'], ids['m1']] and nodes.builds[ids['m1']] == NEW
    assert state['nodes'][ids['m1']]['upgrade']['error'] == '' and await notifications(e) == []
    row = await one(e.db.conn, 'SELECT upgrade_failed_build FROM mesh_nodes WHERE server_id=?', (ids['m1'],))
    assert row['upgrade_failed_build'] == ''


async def test_trend_samples_each_minute_and_answers_three_ranges(env, monkeypatch):  # noqa: F811
    e = env
    nodes, ids, _ = await upgrade_setup(e)
    hour = 1793001600  # a multiple of two hours, so every range's buckets line up with it
    clock = [hour * 1000]
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: clock[0])

    def report(at, *, started=5, rtt=100.0, timeout=0.0, reconnects=2, rx=1000, tx=500, switches=1, cpu=1.5):
        clock[0] = (hour + at) * 1000
        return e.servers.mesh.report(ids['m1'], {
            'schema': 1, 'startedAt': started, 'appliedRevision': 1, 'ready': True, 'iface': 'obmesh0', 'error': '', 'events': [],
            'peers': [{'id': ids['r1'], 'kind': 'entry', 'active': True, 'transport': 'udp', 'rttMs': rtt, 'timeoutRate': timeout,
                       'reconnects': reconnects, 'rxBytes': rx, 'txBytes': tx},
                      {'id': ids['r2'], 'kind': 'relay', 'active': False, 'rttMs': 900.0, 'timeoutRate': 0.5}],
            'entry': {'candidate': 'c1', 'relay': ids['r1'], 'transport': 'udp', 'since': 1, 'switches': switches},
            'resources': {'rssBytes': 50 * 1024 ** 2, 'cpuPercent': cpu}})
    await report(0)                                                              # first report: no increments yet
    await report(30, rtt=120.0, reconnects=3, rx=3000, tx=1500, switches=2, cpu=2.5)
    await report(60, rtt=None, timeout=1.0, reconnects=3, rx=3000, tx=1500, switches=2)  # minute 0 is written
    await report(7200, started=9, reconnects=1, rx=100, tx=50, switches=0)       # data plane restarted: counted from zero
    await report(7260, started=9, reconnects=1, rx=100, tx=50, switches=0)
    minute0 = {'at': hour * 1000, 'rttMs': 110.0, 'timeoutRate': 0.0, 'switches': 1, 'reconnects': 1, 'rssBytes': 50 * 1024 ** 2,
               'cpuPercent': 2.0, 'rxBytes': 2000, 'txBytes': 1000}
    later = {'at': (hour + 7200) * 1000, 'rttMs': 100.0, 'timeoutRate': 0.0, 'switches': 0, 'reconnects': 1, 'rssBytes': 50 * 1024 ** 2,
             'cpuPercent': 1.5, 'rxBytes': 100, 'txBytes': 50}
    # The first two minutes in one 12-minute (24h) or 2-hour (7d) point: rtt from the samples that had one, the timeout rate from all.
    merged = {**minute0, 'timeoutRate': 0.333, 'cpuPercent': 1.8}
    clock[0] = (hour + 7300) * 1000
    path = f"/api/servers/mesh/members/{ids['m1']}/trend"
    # rtt says what rttMs is the round trip to: the entry relay the member used.
    basis = {'kind': 'entry', 'peers': [ids['r1']]}
    assert await api(e, 'GET', path) == {'ok': True, 'range': '1h', 'points': [later], 'rtt': basis}
    assert await api(e, 'GET', path + '?range=24h') == {'ok': True, 'range': '24h', 'points': [merged, later], 'rtt': basis}
    assert await api(e, 'GET', path + '?range=7d') == {'ok': True, 'range': '7d', 'points': [merged, later], 'rtt': basis}
    # Eight days on, the 7-day range no longer reaches them.
    clock[0] = (hour + 8 * 86400) * 1000
    assert (await api(e, 'GET', path + '?range=7d'))['points'] == []

    # A relay is measured by its active links to other relays, not by the members it carries (they report 0 or nothing).
    for at in (0, 60):
        clock[0] = (hour + at) * 1000
        await e.servers.mesh.report(ids['r1'], {'startedAt': 3, 'peers': [{'id': ids['r2'], 'kind': 'relay', 'active': True, 'rttMs': 10.0},
                                                                          {'id': ids['m1'], 'kind': 'member', 'active': True, 'rttMs': 30.0}]})
    clock[0] = (hour + 120) * 1000
    relay = await api(e, 'GET', f"/api/servers/mesh/members/{ids['r1']}/trend")
    assert [p['rttMs'] for p in relay['points']] == [10.0] and relay['rtt'] == {'kind': 'links', 'peers': [ids['r2']]}

    assert (await api(e, 'GET', path + '?range=30d', status=400))['error'] == 'invalid_range'
    assert (await api(e, 'GET', f"/api/servers/mesh/members/{ids['m2']}/trend", status=404))['error'] == 'mesh_member_not_found'


async def test_layout_merges_deletes_checks_and_stays_out_of_the_draft(env):  # noqa: F811
    e = env
    nodes, ids, _ = await upgrade_setup(e)
    before = await api(e, 'GET', '/api/servers/mesh')
    assert before['layout'] == {} and not before['draft']['dirty']
    saved = await api(e, 'PUT', '/api/servers/mesh/layout', {'positions': {ids['r1']: {'x': 10, 'y': -20.5}, ids['m1']: {'x': 1, 'y': 2}}})
    assert saved == {'ok': True, 'layout': {ids['r1']: {'x': 10, 'y': -20.5}, ids['m1']: {'x': 1, 'y': 2}}}
    saved = await api(e, 'PUT', '/api/servers/mesh/layout', {'positions': {ids['m1']: None, ids['m2']: {'x': 100000, 'y': -3.25}}})
    assert saved['layout'] == {ids['r1']: {'x': 10, 'y': -20.5}, ids['m2']: {'x': 100000, 'y': -3.25}}
    after = await api(e, 'GET', '/api/servers/mesh')
    assert after['layout'] == saved['layout']
    assert after['draft'] == {'dirty': False, 'changes': []} and after['applied'] == before['applied']
    assert (await one(e.db.conn, 'SELECT revision FROM mesh_network WHERE owner=?', (OWNER,)))['revision'] == 1

    for bad in ({'positions': {ids['r1']: {'x': 100001, 'y': 0}}}, {'positions': {ids['r1']: {'x': True, 'y': 0}}},
                {'positions': {ids['r1']: {'x': '1', 'y': 0}}}, {'positions': {ids['r1']: {'x': 1}}},
                {'positions': {ids['r1']: {'x': 1, 'y': 2, 'z': 3}}}, {'positions': []}, {'positions': {}, 'other': 1},
                {'positions': {f'id-{i}': None for i in range(501)}}):
        assert (await api(e, 'PUT', '/api/servers/mesh/layout', bad, status=400))['error'] == 'invalid_mesh_layout'
    assert (await api(e, 'PUT', '/api/servers/mesh/layout', {'positions': {'nobody': {'x': 1, 'y': 1}}}, status=404))['error'] == 'server_not_found'
    assert (await api(e, 'GET', '/api/servers/mesh'))['layout'] == saved['layout']

    await api(e, 'DELETE', f"/api/servers/{ids['m2']}")
    assert (await api(e, 'GET', '/api/servers/mesh'))['layout'] == {ids['r1']: {'x': 10, 'y': -20.5}}


async def test_servers_being_installed_are_listed_until_they_join_the_draft(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    nodes.install_gates[ids['r1']] = asyncio.Event()
    nodes.install_errors[ids['m1']] = 'install_failed'
    adding = asyncio.create_task(api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]}))
    await until(lambda: ids['r1'] in nodes.sent('mesh.install') and ids['m1'] in nodes.sent('mesh.install'))
    state = await api(e, 'GET', '/api/servers/mesh')
    # The failed one is gone at once; the other is listed, and not yet a node.
    assert [{k: v for k, v in i.items() if k != 'startedAt'} for i in state['installing']] == [{'serverId': ids['r1'], 'name': 'Berlin', 'platform': 'linux'}]
    assert isinstance(state['installing'][0]['startedAt'], int) and state['installing'][0]['startedAt'] > 0
    assert ids['r1'] not in state['nodes'] and state['members'] == []
    nodes.install_gates[ids['r1']].set()
    added = await adding
    assert [(i['serverId'], i['ok']) for i in added['items']] == [(ids['r1'], True), (ids['m1'], False)]
    assert added['installing'] == [] and [m['serverId'] for m in added['members']] == [ids['r1']] and ids['r1'] in added['nodes']


async def test_migration_adds_columns_to_an_older_table_and_keeps_its_rows(env):  # noqa: F811
    e = env
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('DROP TABLE mesh_nodes')
        await conn.execute('DROP TABLE mesh_layout')
        await conn.execute("""CREATE TABLE mesh_nodes (
 server_id TEXT PRIMARY KEY, owner INTEGER NOT NULL, public_key TEXT NOT NULL, control_key TEXT NOT NULL,
 platform TEXT NOT NULL DEFAULT '', data_version TEXT NOT NULL DEFAULT '', local_json TEXT NOT NULL DEFAULT '[]',
 tls_json TEXT NOT NULL DEFAULT '', tls_checked_ms INTEGER NOT NULL DEFAULT 0,
 confirmed_revision INTEGER NOT NULL DEFAULT 0, confirmed_hash TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 status_json TEXT NOT NULL DEFAULT '', events_json TEXT NOT NULL DEFAULT '[]', updated_ms INTEGER NOT NULL DEFAULT 0)""")
        await conn.execute("INSERT INTO mesh_nodes(server_id,owner,public_key,control_key,data_version,confirmed_revision) VALUES('s1',?,'pk','ck','1.0.0',7)", (OWNER,))
    for _ in range(2):  # a second start finds nothing to add
        await schema_module.migrate(e.db)
    row = await one(e.db.conn, 'SELECT * FROM mesh_nodes WHERE server_id=?', ('s1',))
    assert (row['public_key'], row['data_version'], row['confirmed_revision']) == ('pk', '1.0.0', 7)
    assert (row['data_build'], row['upgrade_failed_build'], row['upgrade_error'], row['upgrade_ms']) == ('', '', '', 0)
    assert await many(e.db.conn, 'SELECT * FROM mesh_layout') == []


async def test_servers_tool_runs_the_mesh_tab_in_process(env):  # noqa: F811
    """The AI's Servers tool does what the Mesh tab does, through the same service and without asking."""
    from app.tools import servers as tool_module
    from app.tools.base import ToolRegistry, ToolRuntimeContext
    e = env
    nodes, ids = await mesh_setup(e)
    registry = ToolRegistry()
    tool_module.register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-m',779,?,'t')", (OWNER,))
    asked = []

    async def confirm(payload):
        asked.append(payload)
        return False
    ctx = ToolRuntimeContext(chat_id=779, conversation_uuid='conv-m', web_confirm=confirm)

    async def call(action, context=ctx, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=context))

    described = await call('describe')
    mesh_actions = {a for a in tool_module.ACTIONS if a.startswith('mesh')}
    assert mesh_actions <= set(described['contracts']) and 'mesh_' in described['mesh']
    draft_contract = json.dumps(described['contracts']['mesh_save_draft'])
    assert all(word in draft_contract for word in ('relayMode', 'bias', 'stun', 'candidates', 'directLinks', 'rules', 'No revision or lock'))
    assert tool_module.MESH_MUTATING <= tool_module.MUTATING and not {'mesh', 'mesh_draft', 'mesh_member', 'mesh_trend', 'mesh_path'} & tool_module.MUTATING

    # Joining installs the mesh service at once: no confirmation, no confirm parameter.
    joined = await call('mesh_join', serverIds=[ids['r1'], ids['r2'], ids['m1'], ids['m2']])
    assert [i['ok'] for i in joined['items']] == [True] * 4 and set(nodes.sent('mesh.install')) == set(ids.values())
    assert joined['draft']['dirty'] and 'layout' not in joined and len(joined['members']) == 4

    draft = await call('mesh_draft')
    assert set(draft) >= {'settings', 'members', 'relayLinks', 'directLinks', 'rules', 'errors', 'warnings', 'dirty'} and 'truncated' not in draft
    members = [{'serverId': m['serverId']} for m in draft['members']]
    # Checks fail: nothing is saved and every problem comes back as the page gets it.
    members[2]['role'] = 'relay'  # Windows
    bad = await call('mesh_save_draft', members=members)
    assert bad['ok'] is False and bad['error'] == 'mesh_invalid'
    assert {'path': 'members[2].role', 'error': 'relay_requires_linux', 'message': '中转只能是 Linux'} in bad['errors']
    refused = await call('mesh_save_draft', member=members)
    assert refused['error'] == 'invalid_mesh_draft' and 'not: member' in refused['errors'][0]['message']
    assert (await call('mesh_save_draft'))['error'] == 'invalid_mesh_draft'
    members[2] = {'serverId': ids['m1'], 'entry': {'mode': 'fixed', 'candidates': [{'relay': ids['r1'], 'transport': 'udp'}], 'autoReturn': True}}
    members[3] = {'serverId': ids['m2'], 'entry': {'mode': 'fixed', 'candidates': [{'relay': ids['r2'], 'transport': 'udp'}], 'autoReturn': True}}
    for i in (0, 1):
        members[i].update(role='relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0})
    saved = await call('mesh_save_draft', members=members, relayLinks={'mode': 'custom', 'pairs': [{'a': ids['r1'], 'b': ids['r2'], 'bias': -5}]},
                       settings={'reachability': {'stun': ['stun.example.com:3478']}})
    assert saved['ok'] and saved['errors'] == [] and saved['relayLinks']['pairs'][0]['bias'] == -5
    assert saved['settings']['reachability'] == {'stun': ['stun.example.com:3478']}

    # Apply waits for the rollout and returns its outcome.
    applied = await call('mesh_apply')
    assert applied['apply']['running'] is False and applied['applied']['revision'] == 1 and not applied['draft']['dirty']
    assert {p['state'] for p in applied['apply']['progress']} == {'done'} and nodes.configs[ids['r1']]['routing']['bias'] == {ids['r2']: -5}

    # Reads: overview, member detail, trend and path.
    session = e.servers.hub.sessions[ids['m1']]
    report = {**READY, 'peers': [{'id': ids['r1'], 'kind': 'entry', 'active': True, 'transport': 'udp', 'rttMs': 20.5}],
              'entry': {'candidate': 'c1', 'relay': ids['r1'], 'transport': 'udp', 'since': 1, 'switches': 0},
              'events': [{'seq': 1, 'at': 10, 'kind': 'peer.up', 'detail': 'r1'}]}
    await e.servers.hub._message(session, {'t': 'report', 'm': {}, 'mesh': report})
    overview = await call('mesh')
    assert overview['nodes'][ids['m1']]['state'] == 'ready' and overview['nodes'][ids['m1']]['summary']['rttMs'] == 20.5 and 'truncated' not in overview
    detail = await call('mesh_member', serverId=ids['m1'])
    assert detail['status']['iface'] == 'obmesh0' and detail['events'][0]['kind'] == 'peer.up' and detail['config']['revision'] == 1
    assert (await call('mesh_member'))['error'] == 'server_id_required'
    assert (await call('mesh_member', serverId='nope'))['error'] == 'mesh_member_not_found'
    trend = await call('mesh_trend', serverId=ids['m1'], range='24h')
    assert trend['ok'] and trend['range'] == '24h' and set(trend['summary']) >= {'rttMs', 'switches'}
    assert (await call('mesh_trend', serverId=ids['m1'], range='live'))['error'] == 'invalid_range'
    path = await call('mesh_path', src=ids['m1'], dst=ids['r1'])
    assert path['hops'][-1] == {'node': ids['r1'], 'via': 'entry', 'rttMs': 20.5} and not path['stale']
    # Without dst: every route from src, as GET /mesh/routes.
    table = await call('mesh_path', src=ids['m1'])
    assert [r['dst'] for r in table['routes']] == [ids['r1'], ids['r2'], ids['m2']] and table['subnets'] == []
    assert table['routes'][0] == {'dst': ids['r1'], 'hops': path['hops'], 'rttMs': 20.5, 'stale': False}
    assert (await call('mesh_path'))['error'] == 'invalid_mesh_path'
    assert (await call('mesh_path', src=ids['m1'], dst=5))['error'] == 'invalid_mesh_path'
    assert (await call('mesh_path', src=ids['m1'], dst='nope'))['error'] == 'mesh_member_not_found'

    # Discard drops an unapplied edit.
    await call('mesh_save_draft', settings={'mtu': 1380})
    assert (await call('mesh_discard'))['draft']['dirty'] is False

    # Leaving, then applying, uninstalls the mesh service without asking; rollback brings back the previous revision.
    left = await call('mesh_leave', serverId=ids['m2'])
    assert next(m for m in left['members'] if m['serverId'] == ids['m2'])['removing'] is True
    applied = await call('mesh_apply')
    assert applied['applied']['revision'] == 2 and nodes.sent('mesh.uninstall') == [ids['m2']]
    await call('mesh_save_draft', settings={'mtu': 1380})
    assert (await call('mesh_apply'))['applied']['revision'] == 3 and nodes.configs[ids['m1']]['network']['mtu'] == 1380
    rolled = await call('mesh_rollback')
    assert rolled['applied']['revision'] == 4 and rolled['apply']['running'] is False and rolled['settings']['mtu'] == 1400
    assert nodes.configs[ids['m1']]['network']['mtu'] == 1400
    assert not asked

    # Automatic (webhook) turns read the mesh but never change it.
    hook = ToolRuntimeContext(chat_id=779, conversation_uuid='conv-m', webhook_automatic=True)
    assert (await call('mesh', context=hook))['ok']
    for action in tool_module.MESH_MUTATING:
        assert (await call(action, context=hook))['error'] == 'automatic_mutation_forbidden'


def test_servers_tool_cuts_large_mesh_output_and_says_so():
    from app.tools import servers as tool_module
    budget = tool_module.OUTPUT_BUDGET
    member = {'serverId': 'm', 'name': 'M', 'role': 'member', 'relayMode': 'public', 'address': '100.96.0.1', 'controlAddress': '100.96.255.1',
              'publicEndpoint': '', 'publicEndpointAuto': '', 'listen': {'udp': 51999, 'tcp': 0, 'tls': 0}, 'tags': ['t' * 40] * 20,
              'gateway': None, 'uploadLimitMbps': None, 'entry': {'mode': 'fixed', 'candidates': [], 'autoReturn': True}, 'removing': False}
    routing = {'mode': 'dynamic', 'links': [{'peer': f'r{i}', 'state': 'up', 'rttMs': 1.0} for i in range(30)],
               'routes': [{'dest': f'r{i}', 'nextHop': 'r0', 'path': ['x', f'r{i}']} for i in range(64)]}
    summary = {'entry': {}, 'ready': True, 'peers': 1, 'rttMs': 1.0, 'memory': {}, 'direct': {}, 'reachability': None,
               'entryHealth': [], 'routing': routing}
    view = {'ok': True, 'settings': {'prefix': '100.96.0.0/16', 'health': {}}, 'members': [{**member, 'serverId': f'm{i}'} for i in range(200)],
            'relayLinks': {'mode': 'full', 'pairs': []}, 'directLinks': [], 'rules': [], 'applied': {'revision': 1, 'at': 1},
            'draft': {'dirty': False, 'changes': []}, 'nodes': {f'm{i}': {'state': 'ready', 'appliedRevision': 1, 'error': '', 'summary': summary,
                                                                          'dataVersion': '1', 'upgrade': {}} for i in range(200)},
            'installing': [], 'layout': {'m0': {'x': 1, 'y': 2}}, 'eligible': [], 'apply': {'running': False, 'revision': 1, 'progress': []},
            'errors': [], 'warnings': []}
    out = tool_module._mesh_overview(json.loads(json.dumps(view)))
    assert len(json.dumps(out, ensure_ascii=False)) <= budget and 'layout' not in out
    assert out['truncated']['omitted'][0] == 'nodes.summary routing and entryHealth detail' and out['members'][-1].get('more')
    small = tool_module._mesh_overview({**json.loads(json.dumps(view)), 'members': view['members'][:2], 'nodes': {}})
    assert 'truncated' not in small and small['members'] == view['members'][:2]

    # The draft is never cut in its parts, so it can be sent back as it is.
    draft = tool_module._mesh_draft(json.loads(json.dumps(view)))
    assert len(draft['members']) == 200 and 'name' not in draft['members'][0] and draft['truncated']['omitted']

    status = {'peers': [{'id': f'p{i}', 'kind': 'member', 'active': True, 'endpoint': 'x' * 100} for i in range(300)],
              'owners': {f'm{i}': {'relay': 'r', 'seq': i, 'leaseMs': 30000} for i in range(1000)}, 'routing': routing,
              'reachability': {'verdict': 'public', 'observers': [{'id': 'o'}] * 32}}
    detail = {'ok': True, 'member': member, 'node': None, 'status': status, 'config': {'nodes': [{'id': f'n{i}', 'publicKey': 'k' * 44} for i in range(250)],
                                                                                       'rules': [{'src': ['a'] * 50}] * 50},
              'events': [{'seq': i, 'at': i, 'kind': 'peer.up', 'detail': 'd' * 80} for i in range(100)]}
    cut = tool_module._mesh_member(json.loads(json.dumps(detail)))
    assert len(json.dumps(cut, ensure_ascii=False)) <= budget and cut['truncated']['omitted'][:2] == ['events beyond the newest 30', 'status.owners (count only)']
    assert cut['status']['owners'] == {'count': 1000} and cut['events'][-1] == {'more': 90}
    points = [{'at': i, 'rttMs': 1.0, 'timeoutRate': 0.0, 'switches': 1, 'reconnects': 0, 'rssBytes': 1, 'cpuPercent': 1.0, 'rxBytes': 9, 'txBytes': 9}
              for i in range(400)]
    trend = tool_module._mesh_trend({'range': '24h', 'points': points})
    assert trend['summary']['switches'] == 400 and len(trend['series']) < 60 and trend['truncated']['omitted'] == ['series thinned to about 48 points']



async def test_join_picks_a_udp_port_free_behind_the_same_nat_and_warns_on_shared_ones(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    lan = await server(e, 'NAS', ipv4='45.32.100.5')
    nodes.local[ids['m1']] = nodes.local[ids['m2']] = ['10.20.20.0/24', '203.0.113.0/24']
    nodes.local[ids['r1']] = nodes.local[lan] = ['192.168.8.0/24']

    def shared(view):
        return [w for w in view['warnings'] if w['error'] == 'mesh_udp_port_shared']

    def ports(view):
        return {m['serverId']: m['listen']['udp'] for m in view['members']}
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['r2'], ids['m1']]})
    assert set(ports(added).values()) == {51999}
    # m2 shares m1's private network: it takes the next port down, and nothing is left to warn about.
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['m2']]})
    assert ports(added)[ids['m2']] == 51998 and ports(added)[ids['m1']] == 51999 and shared(added) == []

    # A public relay behind the same private network takes no part.
    members = [{'serverId': sid} for sid in ports(added)]
    for i in (0, 1):
        members[i].update(role='relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0})
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members})
    assert saved['errors'] == [] and shared(saved) == []
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [lan]})
    assert ports(added)[lan] == 51999 and shared(added) == []

    # The Mac and Windows case: both on 51999, no common network, seen at one IP by their checks.
    members = [{'serverId': sid} for sid in ports(added)]
    members[3]['listen'] = {'udp': 51999}
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("UPDATE mesh_nodes SET local_json='[]' WHERE server_id IN (?,?)", (ids['m1'], ids['m2']))
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members})
    assert ports(saved)[ids['m2']] == 51999 and shared(saved) == []
    for sid, port, verdict in ((ids['m1'], 51999, 'punchable'), (ids['m2'], 1026, 'no_udp')):
        reach = {'checkedAt': 1, 'verdict': verdict, 'publicAddr': f'192.0.2.117:{port}', 'consistent': True, 'filtering': 'unknown', 'observers': []}
        await e.servers.hub._message(e.servers.hub.sessions[sid], {'t': 'report', 'm': {}, 'mesh': {**READY, 'appliedRevision': 0, 'reachability': reach}})
    view = await api(e, 'GET', '/api/servers/mesh')
    assert shared(view) == [{'path': 'members[3].listen.udp', 'error': 'mesh_udp_port_shared',
                             'message': '公司-Win 和 家-Mac 在同一个 NAT 后，都用 UDP 51999。部分路由器（尤其带代理的旁路由）会因此丢包，建议把 家-Mac 改成 51998。'}]
    # Only a warning: the draft applies as it is and no port is changed.
    assert view['errors'] == []
    applied = await api(e, 'POST', '/api/servers/mesh/apply')
    assert applied['ok'] and ports(applied)[ids['m2']] == 51999 and len(shared(applied)) == 1


async def test_gateway_routes_lan_entry_and_the_route_table(env):  # noqa: F811
    """Home announces 10.10.10.0/24 for LAX only; the company PC sits on TOY's LAN and enters it there."""
    e = env
    nodes, ids = await mesh_setup(e)
    toy, home = await server(e, 'TOY', ipv4='192.0.2.42'), await server(e, '家里', ipv4='192.0.2.117')
    nodes.addresses = {toy: ['10.20.20.7/24'], ids['m1']: ['10.20.20.9/24', '192.168.56.1/24', '203.0.113.5/24', 'bad', 7], home: []}
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['r2'], toy, ids['m1'], home]})
    assert all(i['ok'] for i in added['items'])
    stored = {r['server_id']: json.loads(r['addresses_json']) for r in await many(e.db.conn, 'SELECT server_id,addresses_json FROM mesh_nodes')}
    assert stored[toy] == ['10.20.20.7/24'] and stored[ids['m1']] == ['10.20.20.9/24', '192.168.56.1/24'] and stored[home] == []
    assert stored[ids['r1']] == []  # sent nothing: nothing stored

    relay = {'role': 'relay', 'listen': {'udp': 51999, 'tcp': 52000, 'tls': 0}}
    members = [{'serverId': ids['r1'], **relay}, {'serverId': ids['r2'], **relay},
               {'serverId': toy, 'role': 'relay', 'relayMode': 'site', 'listen': {'udp': 51999, 'tcp': 0, 'tls': 0}},
               {'serverId': ids['m1'], 'entry': {'mode': 'failover', 'autoReturn': True, 'candidates': [
                   {'relay': toy, 'transport': 'udp'}, {'relay': ids['r1'], 'transport': 'tcp'}]}},
               {'serverId': home, 'gateway': {'subnets': ['10.10.10.0/24']},
                'entry': {'mode': 'fixed', 'autoReturn': True, 'candidates': [{'relay': ids['r1'], 'transport': 'tcp'}]}}]
    rules = [{'src': {'members': [ids['r2']]}, 'dst': {'subnets': ['10.10.10.0/24']}}]
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members, 'rules': rules})
    assert saved['errors'] == []
    # By the draft: only LAX gets the home subnet.
    assert saved['exports'] == [{'gateway': home, 'subnet': '10.10.10.0/24', 'nodes': [ids['r2']]}]
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert {sid: c['self']['gatewayRoutes'] for sid, c in nodes.configs.items()} == {
        ids['r1']: [], ids['r2']: ['10.10.10.0/24'], toy: [], ids['m1']: [], home: []}
    assert nodes.configs[ids['m1']]['entry']['candidates'][0] == {'id': 'c1', 'relay': toy, 'punch': True, 'endpoint': 'udp://10.20.20.7:51999'}

    # A new LAN address after a reconnect is stored and sent out in a new revision.
    nodes.addresses[toy] = ['10.20.20.8/24']
    await connect(e, toy)
    await settled(e)
    assert nodes.configs[ids['m1']]['revision'] == 2
    assert nodes.configs[ids['m1']]['entry']['candidates'][0]['endpoint'] == 'udp://10.20.20.8:51999'
    # A node that sends no localAddresses keeps what was stored.
    del nodes.addresses[toy]
    await connect(e, toy)
    await settled(e)
    row = await one(e.db.conn, 'SELECT addresses_json FROM mesh_nodes WHERE server_id=?', (toy,))
    assert json.loads(row['addresses_json']) == ['10.20.20.8/24'] and nodes.configs[ids['m1']]['revision'] == 2

    # Reports: m1 entered TOY over the LAN; LAX reaches Berlin; home sits on Berlin.
    await e.servers.mesh.report(ids['m1'], {**READY, 'appliedRevision': 2, 'peers': [{'id': toy, 'kind': 'entry', 'active': True, 'rttMs': 0.8}],
                                            'entry': {'candidate': 'c1', 'relay': toy, 'transport': 'udp', 'since': 1, 'switches': 0, 'candidates': [
                                                {'id': 'c1', 'relay': toy, 'transport': 'udp', 'state': 'active', 'via': 'lan'},
                                                {'id': 'c2', 'relay': ids['r1'], 'transport': 'tcp', 'state': 'ready'}]}})
    await e.servers.mesh.report(ids['r2'], {**READY, 'appliedRevision': 2, 'peers': [
        {'id': ids['r1'], 'kind': 'relay', 'active': True, 'routed': True, 'rttMs': 140},
        {'id': toy, 'kind': 'relay', 'active': True, 'routed': False, 'rttMs': 180}]})
    await e.servers.mesh.report(home, {**READY, 'appliedRevision': 2, 'peers': [{'id': ids['r1'], 'kind': 'entry', 'active': True, 'rttMs': 30}],
                                       'entry': {'candidate': 'c1', 'relay': ids['r1'], 'transport': 'tcp', 'since': 1, 'switches': 0}})
    view = await api(e, 'GET', '/api/servers/mesh')
    assert [(c['id'], c['via']) for c in view['nodes'][ids['m1']]['summary']['entryHealth']] == [('c1', 'lan'), ('c2', '')]
    # A relay peer that is no next hop still counts as in use.
    assert view['nodes'][ids['r2']]['summary']['peers'] == 2

    table = await api(e, 'GET', f'/api/servers/mesh/routes?src={ids["r2"]}')
    assert [r['dst'] for r in table['routes']] == [ids['r1'], toy, ids['m1'], home]
    to_home = {'hops': [{'node': ids['r2'], 'via': '', 'rttMs': None}, {'node': ids['r1'], 'via': 'relay', 'rttMs': 140.0},
                        {'node': home, 'via': 'entry', 'rttMs': 30.0}], 'rttMs': 170.0, 'stale': False}
    assert table['routes'][3] == {'dst': home, **to_home}
    assert table['subnets'] == [{'subnet': '10.10.10.0/24', 'gateway': home, **to_home}]
    # The same as /mesh/path for each destination.
    for route in table['routes']:
        path = await api(e, 'GET', f'/api/servers/mesh/path?src={ids["r2"]}&dst={route["dst"]}')
        assert {k: path[k] for k in ('hops', 'rttMs', 'stale')} == {k: route[k] for k in ('hops', 'rttMs', 'stale')}
    assert (await api(e, 'GET', f'/api/servers/mesh/routes?src={ids["m1"]}'))['subnets'] == []
    assert (await api(e, 'GET', '/api/servers/mesh/routes', status=400))['error'] == 'invalid_mesh_path'
    assert (await api(e, 'GET', '/api/servers/mesh/routes?src=nope', status=404))['error'] == 'mesh_member_not_found'


# ----- concurrency, rollback targets, restarts -----
async def applied_base(e, members=('r1', 'm1', 'm2')):
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids[k] for k in members]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = 'relay'
    for m in draft['members'][1:]:
        m['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    return nodes, ids, draft


async def test_a_second_apply_or_a_rollback_during_a_rollout_is_refused_and_keeps_the_draft(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    draft['members'][1]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    nodes.gate = asyncio.Event()
    # Two applies sent together (a double click): one runs, the other is refused at once.
    async def apply():
        response = await e.app.request('POST', '/api/servers/mesh/apply', cookies=e.cookies, headers=e.origin)
        return await response.json()
    first, second = await asyncio.wait_for(asyncio.gather(apply(), apply()), 5)
    assert sorted([first.get('error', 'ok'), second.get('error', 'ok')]) == ['mesh_apply_running', 'ok']
    # A rollback while it runs is refused before it touches the draft.
    draft['members'][2]['uploadLimitMbps'] = 7
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    assert (await api(e, 'POST', '/api/servers/mesh/rollback', status=409))['error'] == 'mesh_apply_running'
    nodes.gate.set()
    state = await settled(e)
    assert state['applied']['revision'] == 2 and state['members'][2]['uploadLimitMbps'] == 7 and state['draft']['dirty']


async def test_rollback_skips_reissues_and_takes_revisions_some_nodes_missed(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0)
    nodes, ids, draft = await applied_base(e)
    # Revision 2 changes m1 while m2 is offline: partly delivered, still the one in effect.
    del e.servers.hub.sessions[ids['m2']]
    draft['members'][1]['uploadLimitMbps'] = draft['members'][2]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    await connect(e, ids['m2'])
    await settled(e)
    # Revision 3 changes m1 again; revision 4 only follows a fact (r1's public address).
    draft['members'][1]['uploadLimitMbps'] = 9
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('UPDATE server_nodes SET info_json=? WHERE server_id=?',
                           (json.dumps({'info': {'platform': 'linux', 'ipv4': '45.32.100.11'}, 'caps': {'exec': True}}), ids['r1']))
    e.servers.hub.sessions[ids['r1']].info['ipv4'] = '45.32.100.11'
    await e.servers.mesh.reconcile(OWNER)
    statuses = {r['revision']: (r['status'], r['reason']) for r in await many(e.db.conn, 'SELECT revision,status,reason FROM mesh_revisions')}
    assert statuses == {1: ('applied', 'apply'), 2: ('partial', 'apply'), 3: ('applied', 'apply'), 4: ('applied', 'facts')}
    # Back to what revision 2 set (5), not to revision 4's identical document nor past 2 to revision 1.
    await api(e, 'POST', '/api/servers/mesh/rollback')
    state = await settled(e)
    assert state['applied']['revision'] == 5 and [m['uploadLimitMbps'] for m in state['members']] == [None, 5, 5]
    assert nodes.configs[ids['m1']]['self']['uploadLimitMbps'] == 5
    # Every earlier document the same as the current one: nothing to go back to.
    e.servers.hub.sessions[ids['r1']].info['ipv4'] = '45.32.100.12'
    await e.servers.mesh.reconcile(OWNER)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('DELETE FROM mesh_revisions WHERE revision<5')
    assert (await api(e, 'POST', '/api/servers/mesh/rollback', status=409))['error'] == 'mesh_no_previous_revision'


async def test_a_rollout_cut_by_a_restart_counts_as_partly_applied(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("UPDATE mesh_revisions SET status='applying' WHERE revision=1")
    restarted = mesh_module.Mesh(e.servers)
    await restarted.start()
    row = await one(e.db.conn, 'SELECT status FROM mesh_revisions WHERE revision=1')
    assert row['status'] == 'partial'


async def test_a_member_rolled_back_in_while_waiting_for_cleanup_is_not_uninstalled(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    # m2 is removed while offline: it waits to be uninstalled when it reconnects.
    await api(e, 'DELETE', f'/api/servers/mesh/members/{ids["m2"]}')
    del e.servers.hub.sessions[ids['m2']]
    await api(e, 'POST', '/api/servers/mesh/apply')
    assert (await settled(e))['nodes'][ids['m2']]['state'] == 'cleanup'
    # Rolling back brings it back as a member before it ever returned.
    await api(e, 'POST', '/api/servers/mesh/rollback')
    state = await settled(e)
    assert ids['m2'] in {m['serverId'] for m in state['members'] if not m['removing']}
    nodes.calls.clear()
    await connect(e, ids['m2'])
    state = await settled(e)
    assert 'mesh.uninstall' not in [m for _, m, _ in nodes.calls]
    row = await one(e.db.conn, 'SELECT state,confirmed_hash FROM mesh_nodes WHERE server_id=?', (ids['m2'],))
    assert row['state'] == '' and row['confirmed_hash'] == config_hash(nodes.configs[ids['m2']])
    assert state['nodes'][ids['m2']]['state'] != 'cleanup'


async def test_alerts_for_lasting_conditions_are_not_sent_again_after_a_controller_restart(env, monkeypatch):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    over = {**READY, 'resources': {'rssBytes': 200 * 1024 ** 2},
            'reachability': {'verdict': 'punchable', 'publicAddr': '203.0.113.5:4000', 'consistent': False}}
    now = mesh_module.now_ms()
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: now)
    await e.servers.mesh.report(ids['m1'], over)
    sent = await notifications(e)
    assert sorted(n.split(':')[0] for n in sent) == ['mesh-memory', 'mesh_exit_inconsistent']
    over['entry'] = {'candidate': 'c1', 'relay': ids['r1'], 'transport': 'udp'}
    over['peers'] = [{'id': ids['r1'], 'kind': 'entry', 'active': True, 'transport': 'udp', 'rttMs': 20}]
    # The controller restarts; the node keeps reporting the same: nothing new.
    restarted = e.servers.mesh = mesh_module.Mesh(e.servers)
    await restarted.start()
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: now + 120_000)
    await restarted.report(ids['m1'], over)
    assert await notifications(e) == sent
    # Once it recovers and it happens again, it is sent again.
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: now + 180_000)
    await restarted.report(ids['m1'], {**READY, 'reachability': {**over['reachability'], 'consistent': True}})
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: now + 240_000)
    await restarted.report(ids['m1'], over)
    kinds = [n.split(':')[0] for n in await notifications(e) if not n.startswith('mesh-entry')]
    assert sorted(kinds) == ['mesh-memory', 'mesh-memory', 'mesh_exit_inconsistent', 'mesh_exit_inconsistent']


async def test_reissues_for_a_changing_address_do_not_push_out_what_rollback_needs(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0)
    nodes, ids, draft = await applied_base(e)
    draft['members'][1]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    # r1's address changes a dozen times: a reissue each.
    for i in range(12):
        e.servers.hub.sessions[ids['r1']].info['ipv4'] = f'45.32.100.{50 + i}'
        await e.servers.mesh.reconcile(OWNER)
    revisions = [r['revision'] for r in await many(e.db.conn, 'SELECT revision FROM mesh_revisions ORDER BY revision')]
    assert revisions[:2] == [1, 2] and revisions[-1] == 14 and len(revisions) == 12
    await api(e, 'POST', '/api/servers/mesh/rollback')
    state = await settled(e)
    assert state['members'][1]['uploadLimitMbps'] is None and nodes.configs[ids['m1']]['self']['uploadLimitMbps'] is None


# ----- the node's own report corrects what the controller thinks it runs -----
async def revision_config(e, revision, sid):
    row = await one(e.db.conn, 'SELECT configs_json FROM mesh_revisions WHERE revision=?', (revision,))
    return json.loads(row['configs_json'])[sid]


async def test_a_node_that_applies_a_failed_revision_itself_is_ready_and_later_changes_reach_it(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    win = ids['m1']
    # Revision 2 opens TCP 52020 on the Windows member; the port is taken and it keeps revision 1.
    draft['members'][1]['listen']['tcp'] = 52020
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    nodes.fail[win] = 'revision 2 not applied: listen tcp :52020: bind: address in use'
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['nodes'][win]['state'] == 'failed' and len(await notifications(e)) == 1
    # Its own retries fail too: its reports say so, and the controller does not send the same revision again.
    nodes.calls.clear()
    for started in (9, 9, 10):
        await e.servers.mesh.report(win, {**READY, 'startedAt': started, 'error': nodes.fail[win]})
        state = await settled(e)
    assert nodes.sent('mesh.apply') == [] and state['nodes'][win]['state'] == 'failed'

    # The port is freed and its data plane applies revision 2 by itself.
    del nodes.fail[win]
    await e.servers.mesh.report(win, {**READY, 'startedAt': 10, 'appliedRevision': 2})
    state = await settled(e)
    assert state['nodes'][win] == {**state['nodes'][win], 'state': 'ready', 'appliedRevision': 2, 'error': ''}
    row = await one(e.db.conn, 'SELECT confirmed_revision,confirmed_hash,error FROM mesh_nodes WHERE server_id=?', (win,))
    assert (row['confirmed_revision'], row['confirmed_hash'], row['error']) == (2, config_hash(await revision_config(e, 2, win)), '')
    assert nodes.sent('mesh.apply') == [] and len(await notifications(e)) == 1

    # Revision 3 closes the port again: the same config as revision 1, but not what it runs, so it is sent.
    draft['members'][1]['listen']['tcp'] = 0
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert config_hash(await revision_config(e, 3, win)) == config_hash(await revision_config(e, 1, win))
    assert win in nodes.sent('mesh.apply') and nodes.configs[win]['revision'] == 3
    assert {p['serverId']: p['state'] for p in state['apply']['progress']}[win] == 'done'


async def test_a_node_back_on_an_older_revision_gets_the_latest_once(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    win = ids['m1']
    draft['members'][1]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    await e.servers.mesh.report(win, {**READY, 'appliedRevision': 2})
    sent = len(await notifications(e))

    # Its data plane restarts, cannot apply config.json and runs revision 1 from applied.json.
    # The controller sends revision 2 once; it fails again and is left to the data plane's retries.
    nodes.fail[win] = 'revision 2 not applied: shaper: tc not found'
    nodes.calls.clear()
    fallback = {**READY, 'startedAt': 20, 'appliedRevision': 1, 'error': nodes.fail[win]}
    await e.servers.mesh.report(win, fallback)
    state = await settled(e)
    assert nodes.sent('mesh.apply') == [win] and state['nodes'][win]['state'] == 'failed'
    row = await one(e.db.conn, 'SELECT confirmed_revision,confirmed_hash FROM mesh_nodes WHERE server_id=?', (win,))
    assert (row['confirmed_revision'], row['confirmed_hash']) == (1, config_hash(await revision_config(e, 1, win)))
    for _ in range(3):
        await e.servers.mesh.report(win, fallback)
        await settled(e)
    assert nodes.sent('mesh.apply') == [win] and len(await notifications(e)) == sent

    # It applies revision 2 by itself; after another restart onto revision 1 the controller sends it again, and it takes.
    del nodes.fail[win]
    await e.servers.mesh.report(win, {**fallback, 'appliedRevision': 2, 'error': ''})
    assert (await settled(e))['nodes'][win]['state'] == 'ready'
    nodes.calls.clear()
    await e.servers.mesh.report(win, {**fallback, 'startedAt': 30, 'error': 'config.json: invalid'})
    await settled(e)
    assert nodes.sent('mesh.apply') == [win] and nodes.configs[win]['revision'] == 2
    await e.servers.mesh.report(win, {**READY, 'startedAt': 30, 'appliedRevision': 2})
    state = await settled(e)
    assert state['nodes'][win]['state'] == 'ready' and len(await notifications(e)) == sent

    # Back on a revision the controller no longer keeps: it cannot tell the config, so it sends the latest.
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('DELETE FROM mesh_revisions WHERE revision=1')
    nodes.calls.clear()
    await e.servers.mesh.report(win, {**fallback, 'startedAt': 40, 'error': ''})
    await settled(e)
    assert nodes.sent('mesh.apply') == [win]


async def test_reports_during_a_rollout_leave_the_record_to_the_delivery(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    win = ids['m1']
    draft['members'][1]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    nodes.gate = asyncio.Event()
    await api(e, 'POST', '/api/servers/mesh/apply')
    # A report sent before the new config took: still revision 1, while revision 2 goes out.
    await e.servers.mesh.report(win, {**READY, 'appliedRevision': 1})
    assert win not in e.servers.mesh.resending
    nodes.gate.set()
    await settled(e)
    row = await one(e.db.conn, 'SELECT confirmed_revision FROM mesh_nodes WHERE server_id=?', (win,))
    assert row['confirmed_revision'] == 2 and nodes.sent('mesh.apply').count(win) == 2


# ----- a node's first hello after its process restarts has no public address yet -----
def dialed(nodes):
    return {(sid, x.get('peer') or x.get('relay')): x.get('endpoint', '') for sid, c in nodes.configs.items()
            for x in [*c['links'], *((c['entry'] or {}).get('candidates') or [])]}


async def test_a_relay_reconnecting_before_its_address_is_known_keeps_its_last_one(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0)
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['r2'], ids['m1']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = draft['members'][1]['role'] = 'relay'
    draft['members'][1]['relayMode'] = 'site'
    draft['members'][2]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert dialed(nodes)[(ids['r2'], ids['r1'])].startswith('udp://45.32.100.1:')
    assert dialed(nodes)[(ids['m1'], ids['r1'])].startswith('udp://45.32.100.1:')

    # r1's node process restarts: its first hello has no public address yet. Nothing is reissued.
    nodes.calls.clear()
    await connect(e, ids['r1'], {'platform': 'linux', 'ipv4': '', 'ipv6': ''})
    state = await settled(e)
    assert state['applied']['revision'] == 1 and nodes.sent('mesh.apply') == []
    row = await one(e.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (ids['r1'],))
    assert json.loads(row['info_json'])['info']['ipv4'] == '45.32.100.1'
    # A change applied in that window still aims at the address last known.
    draft['members'][2]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['applied']['revision'] == 2 and not state['errors']
    assert dialed(nodes)[(ids['r2'], ids['r1'])].startswith('udp://45.32.100.1:')
    assert dialed(nodes)[(ids['m1'], ids['r1'])].startswith('udp://45.32.100.1:')

    # Its lookup then finds the address, now a new one: the configs follow it.
    await connect(e, ids['r1'], {'platform': 'linux', 'ipv4': '45.32.100.21', 'ipv6': ''})
    state = await settled(e)
    assert state['applied']['revision'] == 3
    assert dialed(nodes)[(ids['r2'], ids['r1'])].startswith('udp://45.32.100.21:')
    assert dialed(nodes)[(ids['m1'], ids['r1'])].startswith('udp://45.32.100.21:')


async def test_new_signing_or_end_to_end_keys_go_to_every_node(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = 'relay'
    for m in draft['members'][1:]:
        m['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    keys_of = lambda sid: {n['id']: (n['signKey'], n['e2eKey']) for n in nodes.configs[sid]['nodes']}  # noqa: E731
    assert keys_of(ids['m2'])[ids['m1']] == tuple(Nodes._security_keys(ids['m1']).values())

    # m1's keys were made again (its keys directory restored or replaced): when it reports them, every config carries them.
    new = {'signKey': 'N' * 43 + '=', 'e2eKey': 'E' * 43 + '='}
    nodes._security_keys = lambda sid: new if sid == ids['m1'] else Nodes._security_keys(sid)
    await connect(e, ids['m1'])
    state = await settled(e)
    assert state['applied']['revision'] == 2
    assert all(keys_of(sid)[ids['m1']] == (new['signKey'], new['e2eKey']) for sid in (ids['r1'], ids['m2']))


async def keys_mesh(e):
    """r1 a relay; m1 and m2 entering by it, linked directly, a rule naming m2; applied."""
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = 'relay'
    for m in draft['members'][1:]:
        m['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    draft['directLinks'] = [{'a': ids['m1'], 'b': ids['m2'], 'transport': 'udp', 'dialer': 'a'}]
    draft['rules'] = [{'src': {'members': [ids['m1'], ids['m2']]}, 'dst': {'members': [ids['m1'], ids['m2']]}, 'proto': 'icmp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['applied']['revision'] == 1
    return nodes, ids


async def test_a_member_without_keys_is_left_out_until_it_reports_them(env):  # noqa: F811
    e = env
    nodes, ids = await keys_mesh(e)
    # m2's keys are not known (a node from before them, or one that fell back): no data plane takes a
    # config naming a node without them, so only m2 is cut off, and the others get configs without it.
    nodes._security_keys = lambda sid: {} if sid == ids['m2'] else Nodes._security_keys(sid)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("UPDATE mesh_nodes SET sign_key='',e2e_key='' WHERE server_id=?", (ids['m2'],))
    await e.servers.mesh.reconcile(OWNER)
    state = await settled(e)
    assert state['applied']['revision'] == 2
    for sid in (ids['r1'], ids['m1']):
        config = nodes.configs[sid]
        assert config['revision'] == 2 and ids['m2'] not in json.dumps(config), sid
        assert all(n['signKey'] and n['e2eKey'] for n in config['nodes'])
    # Its own config names only the members with keys, as before: nothing new to send it, and nobody answers it.
    row = await one(e.db.conn, 'SELECT configs_json FROM mesh_revisions WHERE owner=? AND revision=2', (OWNER,))
    own = json.loads(row['configs_json'])[ids['m2']]
    assert sorted(n['id'] for n in own['nodes']) == sorted([ids['r1'], ids['m1']])
    assert all(n['signKey'] and n['e2eKey'] for n in own['nodes'])

    # Once it reports them, it is back in every config.
    nodes._security_keys = Nodes._security_keys
    await connect(e, ids['m2'])
    state = await settled(e)
    assert state['applied']['revision'] == 3
    for sid in (ids['r1'], ids['m1']):
        assert {n['id']: n['signKey'] for n in nodes.configs[sid]['nodes']}[ids['m2']] == Nodes._security_keys(ids['m2'])['signKey']
    assert [link['peer'] for link in nodes.configs[ids['m1']]['links']] == [ids['m2']]


async def test_a_relay_that_cannot_read_the_configs_does_not_hold_back_the_others(env):  # noqa: F811
    e = env
    nodes, ids = await keys_mesh(e)
    # r1's data plane fell back to one that reads only schema 1; the rollout skips it, relays first,
    # and goes on to the members.
    await e.servers.mesh.report(ids['r1'], ready([1, 1]))
    await settled(e)
    nodes.calls.clear()
    new = {'signKey': 'N' * 43 + '=', 'e2eKey': 'E' * 43 + '='}
    nodes._security_keys = lambda sid: new if sid == ids['m1'] else Nodes._security_keys(sid)
    await connect(e, ids['m1'])
    state = await settled(e)
    assert state['applied']['revision'] == 2
    assert ids['r1'] not in nodes.sent('mesh.apply') and nodes.configs[ids['m2']]['revision'] == 2
    progress = {p['serverId']: p['state'] for p in state['apply']['progress']}
    assert progress[ids['r1']] == 'outdated' and progress[ids['m2']] == 'done', progress
    assert state['nodes'][ids['r1']]['error'].startswith('config_schema_unsupported:')
    assert await notifications(e) == []

    # Its data plane updated again, its first report brings it the config it was skipped for.
    await e.servers.mesh.report(ids['r1'], ready(list(compat.CONFIG_SCHEMA)))
    await until(lambda: nodes.configs[ids['r1']]['revision'] == 2)
    assert nodes.configs[ids['r1']] == json.loads((await one(e.db.conn, 'SELECT configs_json FROM mesh_revisions WHERE owner=? AND revision=2', (OWNER,)))['configs_json'])[ids['r1']]


# ----- config schema and protocol compatibility -----
def schemas_sent(nodes):
    return {sid: c['schema'] for sid, c in nodes.configs.items()}


async def schema_mesh(e):
    nodes, ids = await mesh_setup(e)
    # What mesh.status says after the install; m2's data plane reads only 1.
    nodes.schemas = {ids['r1']: [1, 2], ids['m1']: [1, 2], ids['m2']: [1, 1]}
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = 'relay'
    for m in draft['members'][1:]:
        m['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    return nodes, ids, draft


def ready(schemas=None, revision=1):
    # None: a status.json from before configSchemas.
    return {**{k: v for k, v in READY.items() if k != 'configSchemas'}, 'appliedRevision': revision, **({'configSchemas': schemas} if schemas else {})}


async def test_configs_follow_the_newest_schema_every_member_reads(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(config_module, 'SCHEMA', (1, 2))
    nodes, ids, _ = await schema_mesh(e)
    rows = {r['server_id']: r['config_schemas'] for r in await many(e.db.conn, 'SELECT server_id,config_schemas FROM mesh_nodes')}
    assert rows == {ids['r1']: '[1,2]', ids['m1']: '[1,2]', ids['m2']: '[1,1]'}
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['applied']['revision'] == 1 and set(schemas_sent(nodes).values()) == {1}

    # m2's data plane is upgraded: its report says 1..2, and every config goes to 2.
    await e.servers.mesh.report(ids['m2'], ready([1, 2]))
    state = await settled(e)
    assert state['applied']['revision'] == 2 and set(schemas_sent(nodes).values()) == {2}
    # Reports that say the same reissue nothing.
    nodes.calls.clear()
    for sid in ids.values():
        await e.servers.mesh.report(sid, ready([1, 2], 2))
    state = await settled(e)
    assert state['applied']['revision'] == 2 and nodes.sent('mesh.apply') == []

    # m2 falls back to an older copy, whose status.json has no configSchemas: back to 1.
    await e.servers.mesh.report(ids['m2'], ready(None, 2))
    state = await settled(e)
    assert state['applied']['revision'] == 3 and set(schemas_sent(nodes).values()) == {1}


async def test_a_config_a_member_cannot_read_is_not_sent(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(config_module, 'SCHEMA', (1, 2))
    nodes, ids, draft = await schema_mesh(e)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    # r1 comes back reading only 2: no schema in common with m2.
    nodes.calls.clear()
    await e.servers.mesh.report(ids['r1'], ready([2, 2]))
    state = await settled(e)
    assert ids['m2'] not in nodes.sent('mesh.apply')
    assert state['nodes'][ids['m2']]['state'] == 'failed'
    assert state['nodes'][ids['m2']]['error'].startswith('config_schema_unsupported:') and '需要先更新节点' in state['nodes'][ids['m2']]['error']
    # The draft check names it, and apply is refused.
    assert [(x['path'], x['error']) for x in state['errors']] == [('members[2]', 'config_schema_mismatch')]
    draft['members'][1]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    nodes.calls.clear()
    await api(e, 'POST', '/api/servers/mesh/apply', status=400)
    assert nodes.sent('mesh.apply') == []


async def test_a_node_whose_protocol_does_not_overlap_is_not_called(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    newer = await server(e, 'newer', ipv4='45.32.100.9')
    e.servers.hub.sessions[newer].set_protocol(2, 3)
    state = await api(e, 'GET', '/api/servers/mesh')
    assert [(i['ok'], i['reason']) for i in state['eligible'] if i['serverId'] == newer] == [(False, mesh_module.REFUSALS['controller_protocol_outdated'])]
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [newer]})
    assert added['items'] == [{'serverId': newer, 'ok': False, 'error': 'controller_protocol_outdated',
                               'reason': mesh_module.REFUSALS['controller_protocol_outdated']}]
    assert nodes.calls == []


async def test_an_install_with_another_members_key_is_refused(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]})
    r1 = await one(e.db.conn, 'SELECT public_key, control_key FROM mesh_nodes WHERE server_id=?', (ids['r1'],))
    fresh = {'publicKey': 'm2wg'.ljust(43, 'C') + '=', 'controlKey': 'm2c'.ljust(43, 'D') + '='}
    for clash in ({**fresh, 'publicKey': r1['public_key']}, {**fresh, 'controlKey': r1['control_key']},
                  {**fresh, 'controlKey': r1['public_key']}):
        with pytest.raises(InstanceError) as raised:
            await e.servers.mesh._record_install(OWNER, ids['m2'], clash, {'platform': 'darwin'})
        assert raised.value.error == 'mesh_key_in_use' and raised.value.status == 409
    assert not await one(e.db.conn, 'SELECT 1 FROM mesh_nodes WHERE server_id=?', (ids['m2'],))
    # Said on the machine's events, naming the member whose key it is.
    said = e.servers.mesh.status[ids['m2']]['events'][-1]
    assert said['kind'] == 'mesh.key_conflict' and '没有安装' in said['detail'] and e.servers.mesh._name(ids['r1']) in said['detail']
    # Nor a signing or end-to-end key another member has, of any kind, however spelled.
    held = await one(e.db.conn, 'SELECT sign_key, e2e_key FROM mesh_nodes WHERE server_id=?', (ids['r1'],))
    for clash in ({**fresh, 'e2eKey': held['e2e_key']}, {**fresh, 'signKey': held['sign_key']}, {**fresh, 'e2eKey': r1['public_key']},
                  {**fresh, 'publicKey': r1['public_key'][:42] + 'D='}):
        with pytest.raises(InstanceError) as raised:
            await e.servers.mesh._record_install(OWNER, ids['m2'], clash, {'platform': 'darwin'})
        assert raised.value.error == 'mesh_key_in_use'
    assert not await one(e.db.conn, 'SELECT 1 FROM mesh_nodes WHERE server_id=?', (ids['m2'],))
    # Its own keys again (a reinstall that kept them), or new unique ones, are fine.
    await e.servers.mesh._record_install(OWNER, ids['m2'], fresh, {'platform': 'darwin'})
    await e.servers.mesh._record_install(OWNER, ids['m2'], fresh, {'platform': 'darwin'})
    await e.servers.mesh._record_install(OWNER, ids['r1'], {'publicKey': r1['public_key'], 'controlKey': r1['control_key']}, {'platform': 'linux'})
    # A reinstall reporting another's key: refused, its stored keys kept, the event kept with them.
    with pytest.raises(InstanceError):
        await e.servers.mesh._record_install(OWNER, ids['r1'], {'publicKey': fresh['publicKey'], 'controlKey': r1['control_key']}, {'platform': 'linux'})
    row = await one(e.db.conn, 'SELECT public_key, events_json FROM mesh_nodes WHERE server_id=?', (ids['r1'],))
    assert row['public_key'] == r1['public_key'] and json.loads(row['events_json'])[-1]['kind'] == 'mesh.key_conflict'


async def test_a_member_reporting_another_members_keys_keeps_its_own_and_is_named(env, monkeypatch):  # noqa: F811
    """From the 10-30 recheck: a member reporting the only relay's e2eKey in mesh.status (every config
    carries it) had both left out of every config, again on each reconnect: the whole mesh down."""
    e = env
    monkeypatch.setattr(mesh_module, 'MEMBER_UPGRADE_SETTLE', 0)
    nodes, ids, _ = await applied_base(e)
    r1, m1, evil = ids['r1'], ids['m1'], ids['m2']
    copies = {'mesh.status': {}, 'mesh.upgrade': {}}
    real = nodes.call

    async def call(server_id, method, params, timeout):
        reply = await real(server_id, method, params, timeout)
        if server_id == evil and method in copies and reply.get('ok'):
            reply['result'].update(copies[method])
        return reply
    e.servers.hub.call = call
    own = await one(e.db.conn, 'SELECT sign_key,e2e_key FROM mesh_nodes WHERE server_id=?', (evil,))

    async def conflicts():
        member = await api(e, 'GET', f'/api/servers/mesh/members/{evil}')
        state = await api(e, 'GET', '/api/servers/mesh')
        return ([(x['detail'], x.get('repeat', 1)) for x in member['events'] if x['kind'] == 'mesh.key_conflict'],
                [w['message'] for w in state['warnings'] if w['error'] == 'mesh_key_conflict'])

    copies['mesh.status'] = {'e2eKey': Nodes._security_keys(r1)['e2eKey']}
    for _ in range(2):
        await connect(e, evil)
        state = await settled(e)
    # Refused: its record keeps its own key, no revision, and the relay is in every config as before.
    assert await one(e.db.conn, 'SELECT sign_key,e2e_key FROM mesh_nodes WHERE server_id=?', (evil,)) == own
    assert state['applied']['revision'] == 1
    assert {n['id'] for n in nodes.configs[m1]['nodes']} == {r1, evil} and [c['relay'] for c in nodes.configs[m1]['entry']['candidates']] == [r1]
    # Named on the member that reported it: an event each time (folded), and a warning while it lasts.
    assert await conflicts() == ([('上报的端到端密钥和「Berlin」的相同，没有采用。' + mesh_module.KEY_CLONE_ADVICE, 2)],
                                 ['家-Mac 上报的端到端密钥和 Berlin 的相同，主控没有采用。它多半是从 Berlin 克隆出来的，'
                                  '请在 家-Mac 上删掉组网目录里的 keys 后重新安装组网服务；Berlin 和其他成员照常。'])
    # The relay itself reporting its keys is no conflict.
    await connect(e, r1)
    await settled(e)
    assert r1 not in e.servers.mesh.key_conflicts

    # Through an upgrade of its data plane too: a signing key of the other member's is not taken either.
    copies['mesh.status'], copies['mesh.upgrade'] = {}, {'signKey': Nodes._security_keys(m1)['signKey']}
    await connect(e, evil, build='b' * 64)
    state = await settled(e)
    assert evil in nodes.sent('mesh.upgrade') and state['applied']['revision'] == 1
    assert await one(e.db.conn, 'SELECT sign_key,e2e_key FROM mesh_nodes WHERE server_id=?', (evil,)) == own
    assert (await conflicts())[1] == ['家-Mac 上报的签名密钥和 公司-Win 的相同，主控没有采用。它多半是从 公司-Win 克隆出来的，'
                                      '请在 家-Mac 上删掉组网目录里的 keys 后重新安装组网服务；公司-Win 和其他成员照常。']
    # Reporting keys of its own again ends the warning.
    copies['mesh.upgrade'] = {}
    await connect(e, evil, build='b' * 64)
    await settled(e)
    assert (await conflicts())[1] == []


async def test_a_relay_ipv4_flipping_back_and_forth_reissues_nothing_until_one_holds(env, monkeypatch):  # noqa: F811
    """From the 10-30 recheck: every member's config has the relay's IPv4 among its reflectors, and
    6 hellos switching it back and forth sent each member 6 new configs."""
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0.5)
    nodes, ids, _ = await applied_base(e)
    r1 = ids['r1']
    before = list(nodes.sent('mesh.apply'))
    for i in range(6):
        await connect(e, r1, {'platform': 'linux', 'ipv4': '45.32.100.11' if i % 2 == 0 else '45.32.100.1'})
        await asyncio.sleep(0.05)
    state = await settled(e)
    assert state['applied']['revision'] == 1 and nodes.sent('mesh.apply') == before
    # One that holds goes out once, to each member it changes.
    await connect(e, r1, {'platform': 'linux', 'ipv4': '45.32.100.11'})
    assert (await api(e, 'GET', '/api/servers/mesh'))['applied']['revision'] == 1
    state = await settled(e)
    assert state['applied']['revision'] == 2 and sorted(nodes.sent('mesh.apply')[len(before):]) == sorted([ids['m1'], ids['m2']])
    assert nodes.configs[ids['m1']]['entry']['candidates'][0]['endpoint'] == 'udp://45.32.100.11:51999'


async def test_a_failed_delivery_quotes_the_nodes_error_like_a_name(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    nodes.refuse[ids['m1']] = 'invalid_config: see https://evil.example/x'
    draft['members'][1]['uploadLimitMbps'] = 5
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    texts = [r['text'] for r in await many(e.db.conn, "SELECT text FROM server_notifications WHERE id LIKE 'mesh-apply:%'")]
    assert texts == ['组网：第 2 版配置下发到「公司-Win」失败：「invalid_config: see https://evil.example/x」。']
    # Telegram gets it as code, not as a link.
    assert '「<code>invalid_config: see https://evil.example/x</code>」' in telegram_html(texts[0])


# ----- instances on a mesh address: the gateway's server reaches their ports -----
async def instance_mesh(e):
    """r1 relay; m1 runs the gateway, m2 the instances; applied as revision 1."""
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['members'][0]['role'] = 'relay'
    for m in draft['members'][1:]:
        m['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'udp'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['applied']['revision'] == 1
    addr = {m['serverId']: m['address'] for m in state['members']}
    await e.instances.gateway.store({'enabled': True, 'serverId': ids['m1'], 'port': 18962, 'status': 'connected'}, {})
    return nodes, ids, addr


async def put_instance(e, iid, sid, port, bind, *, status='ready', name=None):
    item = {'id': iid, 'name': name or iid, 'serverId': sid, 'port': port, 'managementUrl': f'http://{bind[-1]}:{port}', 'network': {'bind': bind}}
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('INSERT INTO managed_instances(id,owner,public_json,protected,report_hash,authority,status,auth_mode) VALUES(?,?,?,?,?,?,?,?) '
                           'ON CONFLICT(id) DO UPDATE SET public_json=excluded.public_json,status=excluded.status',
                           (iid, OWNER, json.dumps(item), '{}', 'hash-' + iid, 'x', status, 'gateway'))


async def changed(e):
    """As the instance service says after a change; the configs follow."""
    e.instances._mesh_changed(OWNER)
    return await settled(e)


async def test_instances_on_a_mesh_address_are_open_to_the_gateway_server_alone(env):  # noqa: F811
    e = env
    nodes, ids, addr = await instance_mesh(e)
    m1, m2 = ids['m1'], ids['m2']
    assert nodes.configs[m2]['rules'] == []

    await put_instance(e, 'edgehost', m2, 18970, ['127.0.0.1', addr[m2]], name='Edge-Fra')
    await put_instance(e, 'loopback', m2, 18971, ['127.0.0.1'])  # not on the mesh address: nothing
    state = await changed(e)
    assert state['applied']['revision'] == 2
    rule = {'src': [f'{addr[m1]}/32'], 'dst': [f'{addr[m2]}/32'], 'proto': 'tcp', 'ports': [[18970, 18970]]}
    assert nodes.configs[m2]['rules'] == [rule] and nodes.configs[m2]['revision'] == 2
    assert nodes.configs[m1]['rules'] == [] and nodes.configs[ids['r1']]['rules'] == []
    assert state['rules'] == []  # the user's rules stay theirs
    assert state['autoRules'] == [{'kind': 'instance', 'proto': 'tcp', 'serverId': m2, 'gateway': m1, 'instanceId': 'edgehost', 'name': 'Edge-Fra',
                                   'port': 18970, 'src': f'{addr[m1]}/32', 'dst': f'{addr[m2]}/32'}]
    # Nothing changed: no new revision.
    assert (await changed(e))['applied']['revision'] == 2
    # Placed on an address m2 no longer has (or never had on the mesh): nothing for it.
    await put_instance(e, 'stale', m2, 18972, ['127.0.0.1', '100.96.9.9'])
    assert (await changed(e))['applied']['revision'] == 2 and nodes.configs[m2]['rules'] == [rule]

    # Another port.
    await put_instance(e, 'edgehost', m2, 18975, ['127.0.0.1', addr[m2]], name='Edge-Fra')
    await changed(e)
    assert nodes.configs[m2]['rules'] == [{**rule, 'ports': [[18975, 18975]]}]

    # The gateway moves to r1: from there instead.
    await e.instances.gateway.store({'enabled': True, 'serverId': ids['r1'], 'port': 18962, 'status': 'connected'}, {})
    await changed(e)
    assert nodes.configs[m2]['rules'] == [{**rule, 'src': [f"{addr[ids['r1']]}/32"], 'ports': [[18975, 18975]]}]

    # Disabled: no rule. Enabled again, then the instance archived: none either.
    await e.instances.gateway.store({'enabled': False, 'serverId': ids['r1'], 'port': 18962, 'status': 'disabled'}, {})
    state = await changed(e)
    assert nodes.configs[m2]['rules'] == [] and state['autoRules'] == []
    await e.instances.gateway.store({'enabled': True, 'serverId': m1, 'port': 18962, 'status': 'connected'}, {})
    await changed(e)
    assert nodes.configs[m2]['rules'] == [{**rule, 'ports': [[18975, 18975]]}]
    await put_instance(e, 'edgehost', m2, 18975, ['127.0.0.1', addr[m2]], status='archived')
    state = await changed(e)
    assert nodes.configs[m2]['rules'] == [] and state['autoRules'] == []
    # Every change was a revision of its own, delivered to m2.
    assert state['applied']['revision'] == 7


async def test_the_mesh_tool_shows_the_automatic_rules(env):  # noqa: F811
    e = env
    nodes, ids, addr = await instance_mesh(e)
    await put_instance(e, 'edgehost', ids['m2'], 18970, ['127.0.0.1', addr[ids['m2']]], name='Edge-Fra')
    await changed(e)
    from app.tools.servers import _mesh_overview
    view = await e.servers.mesh.view(OWNER)
    out = _mesh_overview(view)
    assert [(a['name'], a['port'], a['src'], a['dst']) for a in out['autoRules']] == [('Edge-Fra', 18970, f"{addr[ids['m1']]}/32", f"{addr[ids['m2']]}/32")]


async def test_an_install_with_a_key_that_is_not_32_bytes_is_refused(env):  # noqa: F811
    """From the 10-30 check: 33 bytes passed the old pattern, and every data plane then refused the
    whole config. Such an install is not recorded; such a signing key is not taken."""
    import base64
    e = env
    nodes, ids = await mesh_setup(e)
    real, long = nodes.call, base64.b64encode(bytes(range(33))).decode()

    async def call(server_id, method, params, timeout):
        reply = await real(server_id, method, params, timeout)
        if server_id == ids['m1'] and method == 'mesh.install' and reply.get('ok'):
            reply['result']['publicKey'] = long
        return reply
    e.servers.hub.call = call
    joined = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['m1']]})
    assert [(i['ok'], i['error']) for i in joined['items']] == [(False, 'mesh_install_invalid')]
    assert not await one(e.db.conn, 'SELECT 1 FROM mesh_nodes WHERE server_id=?', (ids['m1'],))
    assert mesh_module._security_keys({'signKey': long, 'e2eKey': Nodes._security_keys('x')['e2eKey']}) == {'e2e_key': Nodes._security_keys('x')['e2eKey']}


async def test_a_stopped_rollout_is_not_finished_by_the_nodes_next_reports(env):  # noqa: F811
    """From the 10-30 check: after the relay failed and the rollout stopped, a member's next report
    (or reconnect) sent it the new revision anyway, aimed at a relay port the relay never took."""
    e = env
    nodes, ids, draft = await applied_base(e)
    nodes.calls.clear()
    draft['members'][0]['listen'] = {'udp': 51000}
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    nodes.fail[ids['r1']] = 'listen udp :51000: address already in use'
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    report = {'schema': 1, 'startedAt': 5, 'appliedRevision': 1, 'ready': True, 'error': '', 'peers': [], 'entry': None, 'events': [],
              'configSchemas': list(compat.CONFIG_SCHEMA)}
    await e.servers.mesh.report(ids['m1'], report)
    await connect(e, ids['m2'])
    state = await settled(e)
    assert nodes.sent('mesh.apply') == [ids['r1']]
    assert nodes.configs[ids['m1']]['revision'] == nodes.configs[ids['m2']]['revision'] == 1
    assert [p['state'] for p in state['apply']['progress']] == ['failed', 'pending', 'pending']
    # Applied again once the relay can take it: the members follow.
    nodes.fail.pop(ids['r1'])
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert [p['state'] for p in state['apply']['progress']] == ['done', 'done', 'done']
    assert nodes.configs[ids['m1']]['revision'] == 2


async def test_undoing_a_removal_brings_back_its_entries_links_and_rules(env):  # noqa: F811
    """From the 10-30 check: undoing a removal brought the member back without the entry
    candidates, links and rule references removing it had taken out."""
    e = env
    nodes, ids, draft = await applied_base(e)
    draft['rules'] = [{'src': {'members': [ids['m1']]}, 'dst': {'members': [ids['r1']]}, 'proto': 'tcp', 'ports': [[22, 22]]}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    await api(e, 'POST', '/api/servers/mesh/apply')
    before = await settled(e)
    removed = await api(e, 'DELETE', f'/api/servers/mesh/members/{ids["r1"]}')
    assert [m['entry']['candidates'] for m in removed['members'][1:]] == [[], []] and removed['rules'] == []
    back = {k: removed[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    back['members'][0]['removing'] = False
    restored = await api(e, 'PUT', '/api/servers/mesh/draft', back)
    assert [m['entry']['candidates'] for m in restored['members'][1:]] == [m['entry']['candidates'] for m in before['members'][1:]]
    assert [(r['src'], r['dst']) for r in restored['rules']] == [(r['src'], r['dst']) for r in before['rules']]
    assert restored['draft'] == {'dirty': False, 'changes': []}
    # Undone once: removing it again and undoing works the same.
    removed = await api(e, 'DELETE', f'/api/servers/mesh/members/{ids["r1"]}')
    back = {k: removed[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    back['members'][0]['removing'] = False
    assert (await api(e, 'PUT', '/api/servers/mesh/draft', back))['draft']['changes'] == []


async def test_removing_a_member_never_applied_takes_it_out_of_the_draft(env):  # noqa: F811
    """From the 10-30 check: it stayed as a removal, showing 有更改未应用 with nothing listed."""
    e = env
    nodes, ids, _ = await applied_base(e)
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r2']]})
    state = await api(e, 'DELETE', f'/api/servers/mesh/members/{ids["r2"]}')
    assert state['draft'] == {'dirty': False, 'changes': []}
    assert ids['r2'] not in {m['serverId'] for m in state['members']}
    await settled(e)
    assert ids['r2'] in nodes.sent('mesh.uninstall')
    assert not await one(e.db.conn, 'SELECT 1 FROM mesh_nodes WHERE server_id=?', (ids['r2'],))


async def test_a_new_member_overlapping_the_prefix_holds_only_the_apply(env):  # noqa: F811
    """From the 10-30 check: it joined silently, then every save was refused, pointing at the prefix."""
    e = env
    nodes, ids, _ = await applied_base(e)
    newcomer = ids['r2']
    real = nodes.call

    async def call(server_id, method, params, timeout):
        reply = await real(server_id, method, params, timeout)
        if server_id == newcomer and method in ('mesh.install', 'mesh.status') and reply.get('ok'):
            reply['result']['localPrefixes'] = ['100.96.8.0/24']
        return reply
    e.servers.hub.call = call
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [newcomer]})
    index = next(i for i, m in enumerate(added['members']) if m['serverId'] == newcomer)
    assert [(x['path'], x['error']) for x in added['warnings'] if x['error'] == 'mesh_prefix_overlap'] == [(f'members[{index}]', 'mesh_prefix_overlap')]
    draft = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    draft['rules'] = [{'src': {'all': True}, 'dst': {'all': True}, 'proto': 'icmp', 'ports': [], 'note': 'ping'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    refused = await api(e, 'POST', '/api/servers/mesh/apply', status=400)
    assert [(x['path'], x['error']) for x in refused['errors']] == [(f'members[{index}]', 'prefix_conflict')]


async def test_a_clone_of_a_member_is_refused_with_a_reason_one_can_read(env):  # noqa: F811
    """From the 10-30 check: only the code mesh_key_in_use showed, and the event went nowhere."""
    e = env
    nodes, ids, _ = await applied_base(e)
    clone = ids['r2']
    real = nodes.call

    async def call(server_id, method, params, timeout):
        reply = await real(server_id, method, params, timeout)
        if server_id == clone and method == 'mesh.install' and reply.get('ok'):
            reply['result'].update(publicKey=(ids['r1'][:8] + 'wg').ljust(43, 'A') + '=', controlKey=(ids['r1'][:8] + 'c').ljust(43, 'B') + '=',
                                   **Nodes._security_keys(ids['r1']))
        return reply
    e.servers.hub.call = call
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [clone]})
    item = added['items'][0]
    assert item['error'] == 'mesh_key_in_use' and '克隆' in item['reason'] and '/var/lib/openbear-node/mesh/keys' in item['reason']
    reminder = await one(e.db.conn, "SELECT text FROM server_notifications WHERE kind='mesh' AND server_id=?", (clone,))
    assert reminder and '克隆' in reminder['text'] and '入侵' not in reminder['text']
