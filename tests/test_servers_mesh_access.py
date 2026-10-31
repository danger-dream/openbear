"""Control connections over the mesh: the built-in access point ap-mesh, choosing it per
server, moving nodes when it comes or goes, the controller's port opened to the members,
gateway routes the controller's machine already has, and deliveries whose connection the
delivery itself cuts. Real Web app, isolated database, fake nodes."""
from __future__ import annotations

import asyncio
import ipaddress
import json

from app.instances.servers import LOCAL
from app.servers import mesh as mesh_module
from app.servers.mesh_config import config_hash
from app.webhooks.repository import one
from tests.test_mesh_service import (
    OWNER,
    READY,
    connect,
    mesh_setup,
    notifications,
    server,
    settled,
    until,
)
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

DIRECT, RELAY = 'https://node.example.com', 'https://relay.example.com'
# A node recent enough to take a mesh address; tests.test_mesh_service's default is an older one.
MESHED = ('mesh', 'access.mesh')


def route_line(dev, net):
    n = ipaddress.ip_network(net)
    hexed = [f'{int.from_bytes(a.packed, "little"):08X}' for a in (n.network_address, n.netmask)]
    return f'{dev}\t{hexed[0]}\t00000000\t0001\t0\t0\t0\t{hexed[1]}\t0\t0\t0'


ROUTES = [('eth0', '0.0.0.0/0'), ('eth0', '203.0.113.0/24'), ('wg9', '10.0.9.0/24'), ('wg9', '10.10.10.0/24'), ('wg9', '10.20.20.0/24'),
          ('docker0', '172.17.0.0/16'), ('br-1a2b', '172.18.0.0/16'), ('lo', '127.0.0.0/8'),
          # Routes the mesh put in itself never count against it.
          ('obmesh0', '100.96.0.0/16'), ('obmesh0', '10.10.10.0/24')]


def route_table(tmp_path, routes=ROUTES):
    path = tmp_path / 'route'
    path.write_text('Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n'
                    + '\n'.join(route_line(dev, net) for dev, net in routes) + '\n')
    return str(path)


class AccessNodes:
    """In front of the mesh fake: config.url, and mesh.apply / mesh.upgrade calls that lose the
    connection (the node then comes back, comes back on the old revision, or stays away)."""

    def __init__(self, nodes):
        self.nodes, self.e = nodes, nodes.e
        self.log, self.drops, self.status, self.refuse_url = [], {}, {}, set()

    async def call(self, sid, method, params, timeout):
        sessions = self.e.servers.hub.sessions
        if sid not in sessions:
            raise LookupError('node_offline')
        self.log.append((sid, method, params))
        if method == 'config.url':
            if sid in self.refuse_url:
                return {'ok': False, 'error': 'new_url_unreachable'}
            return {'ok': True, 'result': {'changed': True, 'backupReachable': True}}
        how = self.drops.get((sid, method))
        if how:
            del self.drops[(sid, method)]
            if method == 'mesh.apply':
                revision = params['config']['revision']
                if how != 'stale':
                    self.nodes.configs[sid] = params['config']
                self.status[sid] = {**READY, 'appliedRevision': revision if how != 'stale' else revision - 1}
            else:
                self.nodes.builds[sid] = sessions[sid].build
            session = sessions.pop(sid)
            if how in ('back', 'stale'):
                asyncio.get_running_loop().call_later(0.1, lambda: asyncio.ensure_future(
                    connect(self.e, sid, session.info, build=session.build, features=session.features)))
            raise ConnectionError('node_disconnected')
        reply = await self.nodes.call(sid, method, params, timeout)
        if method == 'mesh.status' and sid in self.status:
            reply['result']['status'] = self.status[sid]
        return reply

    def urls(self):
        return [(sid, p['url'], p['backup']) for sid, m, p in self.log if m == 'config.url']

    def index(self, sid, method):
        return next(i for i, (s, m, _) in enumerate(self.log) if s == sid and m == method)


async def enroll_local(e, nodes):
    info = {'platform': 'linux', 'ipv4': '198.51.100.9'}
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('INSERT INTO server_nodes(server_id,owner,public_key,info_json,enrolled_ms) VALUES(?,?,?,?,1)',
                           (LOCAL, OWNER, 'key-local', json.dumps({'info': info, 'caps': {'exec': True}})))
    await connect(e, LOCAL, info)
    # Interfaces only: the routes over wg9 are not among them.
    nodes.local[LOCAL] = ['10.0.9.0/24', '172.17.0.0/16']


async def mesh_access_setup(e, monkeypatch, tmp_path, *, with_local=True):
    monkeypatch.setattr(mesh_module, 'ROUTE_TABLE', route_table(tmp_path))
    monkeypatch.setattr(mesh_module, 'RECONNECT_POLL', 0.05)
    nodes, ids = await mesh_setup(e)
    fake = AccessNodes(nodes)
    e.servers.hub.call = fake.call
    for sid in ids.values():
        await connect(e, sid, features=MESHED)
    ids['home'] = await server(e, '家里', ipv4='192.0.2.117', features=MESHED)
    ids['solo'] = await server(e, 'solo', features=MESHED)
    await enroll_local(e, nodes)
    await api(e, 'PUT', '/api/servers/settings', {'accessPoints': [{'id': 'ap-direct', 'name': '海外', 'url': DIRECT},
                                                                    {'id': 'ap-relay', 'name': '中转', 'url': RELAY}], 'defaultAccess': 'ap-direct'})
    joining = [ids[k] for k in ('r1', 'r2', 'm1', 'm2', 'home')] + ([LOCAL] if with_local else [])
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': joining})
    relay = {'role': 'relay', 'listen': {'udp': 51999, 'tcp': 52000, 'tls': 0}}
    entry = {'mode': 'fixed', 'autoReturn': True, 'candidates': [{'relay': ids['r1'], 'transport': 'tcp'}]}
    members = [{'serverId': ids['r1'], **relay}, {'serverId': ids['r2'], **relay}, {'serverId': ids['m1'], 'entry': entry},
               {'serverId': ids['m2'], 'entry': entry}, {'serverId': ids['home'], 'entry': entry, 'gateway': {'subnets': ['10.10.10.0/24']}}]
    if with_local:
        members.append({'serverId': LOCAL, 'entry': entry})
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members, 'rules': [
        {'src': {'members': [LOCAL, ids['m1']] if with_local else [ids['m1']]}, 'dst': {'subnets': ['10.10.10.0/24']}}]})
    assert saved['errors'] == []
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    return fake, nodes, ids, state


def test_host_routes_read_the_main_table(tmp_path):
    assert mesh_module.host_routes(route_table(tmp_path)) == [
        {'net': '203.0.113.0/24', 'dev': 'eth0'}, {'net': '10.0.9.0/24', 'dev': 'wg9'}, {'net': '10.10.10.0/24', 'dev': 'wg9'},
        {'net': '10.20.20.0/24', 'dev': 'wg9'}, {'net': '172.17.0.0/16', 'dev': 'docker0'}, {'net': '172.18.0.0/16', 'dev': 'br-1a2b'}]
    assert mesh_module.host_routes(str(tmp_path / 'missing')) == []


async def test_mesh_access_point_follows_the_controller_in_the_mesh(env, monkeypatch, tmp_path):  # noqa: F811
    e = env
    port = e.servers.host.config.web.port
    fake, nodes, ids, state = await mesh_access_setup(e, monkeypatch, tmp_path, with_local=False)
    off = (await api(e, 'GET', '/api/servers/settings'))['settings']['meshAccess']
    assert off == {'id': 'ap-mesh', 'name': '组网', 'url': '', 'available': False, 'reason': 'controller_not_member'}
    # Members may choose it before it is available; it resolves to the default meanwhile.
    saved = await api(e, 'PATCH', f'/api/servers/{ids["m1"]}', {'profile': {'access': {'primary': 'ap-mesh', 'backup': 'ap-relay'}}})
    access = saved['item']['access']
    assert (access['primary']['id'], access['backup']['id'], access['meshEligible'], access['warnings']) == ('ap-direct', 'ap-relay', True, [])
    await api(e, 'PATCH', f'/api/servers/{ids["m2"]}', {'profile': {'access': {'primary': 'ap-direct', 'backup': 'ap-mesh'}}})
    assert (await api(e, 'GET', f'/api/servers/{ids["m2"]}'))['item']['access']['backup'] is None
    fake.log.clear()

    # The controller's machine joins: m1 and m2 move onto the mesh once the configs are out.
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [LOCAL]})
    draft = await api(e, 'GET', '/api/servers/mesh')
    draft['members'][-1]['entry']['candidates'] = [{'relay': ids['r1'], 'transport': 'tcp'}]
    draft['rules'][0]['src']['members'].append(LOCAL)
    await api(e, 'PUT', '/api/servers/mesh/draft', {k: draft[k] for k in ('members', 'rules')})
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    address = next(m['address'] for m in state['members'] if m['serverId'] == LOCAL)
    url = f'http://{address}:{port}'
    on = (await api(e, 'GET', '/api/servers/settings'))['settings']['meshAccess']
    assert on == {'id': 'ap-mesh', 'name': '组网', 'url': url, 'available': True, 'reason': ''}
    assert (await api(e, 'GET', '/api/servers'))['settings']['meshAccess'] == on
    assert sorted(fake.urls()) == sorted([(ids['m1'], url, RELAY), (ids['m2'], DIRECT, url)])
    last_apply = max(i for i, (_, m, _) in enumerate(fake.log) if m == 'mesh.apply')
    assert all(fake.index(sid, 'config.url') > last_apply for sid in (ids['m1'], ids['m2']))
    m1 = (await api(e, 'GET', f'/api/servers/{ids["m1"]}'))['item']['access']
    assert m1['primary'] == {'id': 'ap-mesh', 'name': '组网', 'url': url} and m1['pending'] is False and m1['warnings'] == []
    # Over the mesh without a backup: a hint, saved all the same.
    saved = await api(e, 'PATCH', f'/api/servers/{ids["m1"]}', {'profile': {'access': {'primary': 'ap-mesh', 'backup': ''}}})
    assert saved['item']['access']['warnings'] == ['mesh_no_backup'] and saved['accessPush']['ok']
    assert fake.urls()[-1] == (ids['m1'], url, '')
    assert (await api(e, 'GET', f'/api/servers/{ids["r1"]}'))['item']['access']['meshEligible'] is True
    local = (await api(e, 'GET', f'/api/servers/{LOCAL}'))['item']['access']
    assert local['meshEligible'] is False and local['primary'] is None

    # m1 leaves the mesh: it moves off before its configs go and it is uninstalled.
    fake.log.clear()
    await api(e, 'DELETE', f'/api/servers/mesh/members/{ids["m1"]}')
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert fake.urls() == [(ids['m1'], DIRECT, '')]
    assert fake.index(ids['m1'], 'config.url') < min(i for i, (_, m, _) in enumerate(fake.log) if m in ('mesh.apply', 'mesh.uninstall'))
    m1 = (await api(e, 'GET', f'/api/servers/{ids["m1"]}'))['item']['access']
    assert m1['primary']['id'] == 'ap-direct' and m1['meshEligible'] is False

    # The controller's machine leaves: m2 moves off first, while the mesh still carries it.
    fake.log.clear()
    await api(e, 'DELETE', f'/api/servers/mesh/members/{LOCAL}')
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert fake.urls() == [(ids['m2'], DIRECT, '')]
    assert fake.index(ids['m2'], 'config.url') < min(i for i, (_, m, _) in enumerate(fake.log) if m in ('mesh.apply', 'mesh.uninstall'))
    assert (await api(e, 'GET', '/api/servers/settings'))['settings']['meshAccess']['reason'] == 'controller_not_member'
    assert (await api(e, 'GET', f'/api/servers/{ids["m2"]}'))['item']['access']['backup'] is None


async def test_choosing_the_mesh_access_point_is_checked(env, monkeypatch, tmp_path):  # noqa: F811
    e = env
    fake, nodes, ids, state = await mesh_access_setup(e, monkeypatch, tmp_path)
    for sid in (ids['solo'], LOCAL):
        for access in ({'primary': 'ap-mesh'}, {'primary': 'ap-direct', 'backup': 'ap-mesh'}):
            err = await api(e, 'PATCH', f'/api/servers/{sid}', {'profile': {'access': access}}, status=409)
            assert err['error'] == 'mesh_access_requires_member'
    err = await api(e, 'POST', '/api/servers', {'name': 'new', 'profile': {'access': {'primary': 'ap-mesh'}}}, status=409)
    assert err['error'] == 'mesh_access_requires_member'
    err = await api(e, 'POST', '/api/servers/registration-codes', {'name': 'c', 'access': {'backup': 'ap-mesh'}}, status=409)
    assert err['error'] == 'mesh_access_requires_member'
    # Other profile changes of a server that left the mesh are not held up by an old choice.
    await api(e, 'PATCH', f'/api/servers/{ids["solo"]}', {'profile': {'remark': 'x'}})
    # The built-in point is never one of the user's, nor saved with them.
    err = await api(e, 'PUT', '/api/servers/settings', {'accessPoints': [{'id': 'ap-mesh', 'name': '组网', 'url': DIRECT}]}, status=400)
    assert err['error'] == 'invalid_access_points'
    meshed = (await api(e, 'GET', '/api/servers/settings'))['settings']
    err = await api(e, 'PUT', '/api/servers/settings', {'meshAccess': meshed['meshAccess']}, status=400)
    assert err['error'] == 'invalid_server_settings'
    saved = await api(e, 'PUT', '/api/servers/settings', {'timezone': 'Asia/Tokyo'})
    assert saved['settings']['meshAccess']['available'] is True
    stored = json.loads((await one(e.db.conn, 'SELECT settings_json FROM server_settings WHERE id=1'))['settings_json'])
    assert 'meshAccess' not in stored and [p['id'] for p in stored['accessPoints']] == ['ap-direct', 'ap-relay']
    # Chosen for a member while the node is online: it moves now.
    fake.log.clear()
    saved = await api(e, 'PATCH', f'/api/servers/{ids["m2"]}', {'profile': {'access': {'primary': 'ap-mesh', 'backup': 'ap-direct'}}})
    assert saved['accessPush']['ok'] and fake.urls() == [(ids['m2'], meshed['meshAccess']['url'], DIRECT)]


async def test_controller_port_open_and_its_own_routes_kept(env, monkeypatch, tmp_path):  # noqa: F811
    e = env
    port = e.servers.host.config.web.port
    fake, nodes, ids, state = await mesh_access_setup(e, monkeypatch, tmp_path)
    address = {m['serverId']: m['address'] for m in state['members']}
    local = nodes.configs[LOCAL]
    # Every member reaches the controller's port on its mesh address, whatever the user's rules.
    others = [f'{address[m["serverId"]]}/32' for m in state['members'] if m['serverId'] != LOCAL]
    assert local['rules'][0] == {'src': others, 'dst': [f'{address[LOCAL]}/32'], 'proto': 'tcp', 'ports': [[port, port]]}
    assert len(local['rules']) == 1
    assert all(not any(r['dst'] == [f'{address[LOCAL]}/32'] for r in nodes.configs[sid]['rules']) for sid in others if sid in nodes.configs)
    assert all(nodes.configs[ids[k]]['rules'] == [] for k in ('r1', 'r2', 'm1', 'm2'))
    # 10.10.10.0/24 goes over wg9 on the controller's machine: it is not routed into the mesh there.
    assert local['self']['gatewayRoutes'] == [] and nodes.configs[ids['m1']]['self']['gatewayRoutes'] == ['10.10.10.0/24']
    # The gateway still answers the controller's machine.
    assert sorted(nodes.configs[ids['home']]['rules'][0]['src']) == sorted([f'{address[LOCAL]}/32', f'{address[ids["m1"]]}/32'])
    index = [m['serverId'] for m in state['members']].index(LOCAL)
    warning = next(w for w in state['warnings'] if w['error'] == 'mesh_gateway_route_conflict')
    assert warning == {'path': f'members[{index}]', 'error': 'mesh_gateway_route_conflict',
                       'message': '本机 本机已有 10.10.10.0/24 的路由（经 wg9），和家里 的网关网段 10.10.10.0/24 重叠。组网不在 本机 上装 10.10.10.0/24 的路由，'
                                  '它访问这个网段仍走原来的路由，其他组网流量照常。'}
    assert state['errors'] == []
    exports = state['exports']
    assert exports == [{'gateway': ids['home'], 'subnet': '10.10.10.0/24', 'nodes': [ids['m1']]}]
    # A gateway subnet inside a bridge's network is held back the same way, from the interface list.
    draft = await api(e, 'GET', '/api/servers/mesh')
    draft['members'][[m['serverId'] for m in draft['members']].index(ids['home'])]['gateway'] = {'subnets': ['10.10.10.0/24', '172.17.5.0/24']}
    draft['rules'][0]['dst']['subnets'] = ['10.10.10.0/24', '172.17.5.0/24']
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {k: draft[k] for k in ('members', 'rules')})
    held = [w['message'] for w in saved['warnings'] if w['error'] == 'mesh_gateway_route_conflict']
    assert len(held) == 2 and '172.17.0.0/16 的路由（经 docker0）' in held[1]
    # Routes on other machines are not known: nothing is held back there.
    assert all(w['path'] == f'members[{index}]' for w in saved['warnings'] if w['error'] == 'mesh_gateway_route_conflict')
    # A route of the controller's machine into the mesh's own range blocks it, as an interface would.
    monkeypatch.setattr(mesh_module, 'ROUTE_TABLE', route_table(tmp_path, [*ROUTES, ('tailscale0', '100.96.8.0/24')]))
    err = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members']}, status=400)
    assert any(x['error'] == 'prefix_conflict' and '100.96.8.0/24' in x['message'] for x in err['errors'])


async def test_a_delivery_that_cuts_its_connection_waits_for_the_node(env, monkeypatch, tmp_path):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0)
    fake, nodes, ids, state = await mesh_access_setup(e, monkeypatch, tmp_path)
    m1, m2 = ids['m1'], ids['m2']

    async def reissue():
        fake.log.clear()
        draft = await api(e, 'GET', '/api/servers/mesh')
        for m in draft['members']:
            if m['serverId'] in (m1, m2):
                m['uploadLimitMbps'] = (m['uploadLimitMbps'] or 0) + 10
        await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members']})
        await api(e, 'POST', '/api/servers/mesh/apply')
        return await settled(e)

    # Back within the wait and running the revision: done, no failure, nothing sent again.
    fake.drops[(m1, 'mesh.apply')] = 'back'
    before = await notifications(e)
    state = await reissue()
    progress = {p['serverId']: p for p in state['apply']['progress']}
    assert progress[m1]['state'] == 'done' and progress[m2]['state'] == 'done' and state['apply']['revision'] == 2
    row = await one(e.db.conn, 'SELECT confirmed_revision,confirmed_hash,error FROM mesh_nodes WHERE server_id=?', (m1,))
    assert (row['confirmed_revision'], row['confirmed_hash'], row['error']) == (2, config_hash(nodes.configs[m1]), '')
    assert await notifications(e) == before
    assert sum(1 for s, m, _ in fake.log if s == m1 and m == 'mesh.apply') == 1
    revision = await one(e.db.conn, 'SELECT status FROM mesh_revisions WHERE owner=? AND revision=2', (OWNER,))
    assert revision['status'] == 'applied'

    # Back, but on the old revision when the wait runs out: failed, the rest not reached.
    monkeypatch.setattr(mesh_module, 'RECONNECT_WAIT', 0.6)
    fake.drops[(m1, 'mesh.apply')] = 'stale'
    state = await reissue()
    progress = {p['serverId']: p for p in state['apply']['progress']}
    assert progress[m2]['error'] == 'stopped'
    assert await notifications(e) == [*before, f'mesh-apply:{OWNER}:3:{m1}']
    assert (await one(e.db.conn, 'SELECT text FROM server_notifications WHERE id=?', (f'mesh-apply:{OWNER}:3:{m1}',)))['text'].endswith('「mesh_not_applied」。')
    revision = await one(e.db.conn, 'SELECT status FROM mesh_revisions WHERE owner=? AND revision=3', (OWNER,))
    assert revision['status'] == 'failed'
    statuses = [s for s, m, _ in fake.log if s == m1 and m == 'mesh.status']
    assert len(statuses) >= 5
    # Its reconnect then gets it the config, as for any node that comes back (the existing catch-up).
    assert [m for s, m, _ in fake.log if s == m1][-1] == 'mesh.apply' and progress[m1]['state'] == 'done'

    # Not back within the wait: offline as before, the rest still delivered, no failure reminder;
    # once back it gets the config through the usual catch-up.
    fake.drops[(m1, 'mesh.apply')] = 'gone'
    state = await reissue()
    progress = {p['serverId']: p for p in state['apply']['progress']}
    assert progress[m1]['state'] == 'offline' and (progress[m2]['state'], progress[m2]['error']) == ('done', '')
    assert await notifications(e) == [*before, f'mesh-apply:{OWNER}:3:{m1}']
    row = await one(e.db.conn, 'SELECT confirmed_revision,error FROM mesh_nodes WHERE server_id=?', (m1,))
    assert row['confirmed_revision'] == 3 and row['error'] == ''
    revision = await one(e.db.conn, 'SELECT status FROM mesh_revisions WHERE owner=? AND revision=4', (OWNER,))
    assert revision['status'] == 'partial'
    await connect(e, m1, features=MESHED)
    state = await settled(e)
    row = await one(e.db.conn, 'SELECT confirmed_revision FROM mesh_nodes WHERE server_id=?', (m1,))
    assert row['confirmed_revision'] == 4 and nodes.configs[m1]['revision'] == 4
    assert {p['serverId']: p['state'] for p in state['apply']['progress']}[m1] == 'done'


async def test_an_upgrade_that_cuts_its_connection_waits_for_the_node(env, monkeypatch, tmp_path):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'MEMBER_UPGRADE_SETTLE', 0.05)
    fake, nodes, ids, state = await mesh_access_setup(e, monkeypatch, tmp_path)
    m1 = ids['m1']
    old, new = 'a' * 64, 'b' * 64
    nodes.builds = {sid: old for sid in nodes.builds} | {m1: old}
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('UPDATE mesh_nodes SET data_build=?', (old,))
    fake.drops[(m1, 'mesh.upgrade')] = 'back'
    await connect(e, m1, build=new, features=MESHED)
    await until(lambda: (m1, 'mesh.upgrade') not in fake.drops)
    await until(lambda: m1 not in e.servers.mesh.upgrades)
    await settled(e)
    row = await one(e.db.conn, 'SELECT data_build,upgrade_failed_build,upgrade_error FROM mesh_nodes WHERE server_id=?', (m1,))
    assert (row['data_build'], row['upgrade_failed_build'], row['upgrade_error']) == (new, '', '')
    assert not [n for n in await notifications(e) if n.startswith('mesh-upgrade:')]

    # Not back within the wait: offline, no failure recorded or reminded; tried again when it reconnects.
    monkeypatch.setattr(mesh_module, 'RECONNECT_WAIT', 0.3)
    newer = 'c' * 64
    fake.drops[(m1, 'mesh.upgrade')] = 'gone'
    await connect(e, m1, build=newer, features=MESHED)
    await until(lambda: (m1, 'mesh.upgrade') not in fake.drops)
    await until(lambda: m1 not in e.servers.mesh.upgrades)
    await settled(e)
    row = await one(e.db.conn, 'SELECT data_build,upgrade_failed_build,upgrade_error FROM mesh_nodes WHERE server_id=?', (m1,))
    assert (row['upgrade_failed_build'], row['upgrade_error']) == ('', '')
    assert not [n for n in await notifications(e) if n.startswith('mesh-upgrade:')]
    # Back later: what it reports then is recorded (it did restart on the new build).
    await connect(e, m1, build=newer, features=MESHED)
    await until(lambda: m1 not in e.servers.mesh.upgrades)
    await settled(e)
    row = await one(e.db.conn, 'SELECT data_build,upgrade_error FROM mesh_nodes WHERE server_id=?', (m1,))
    assert (row['data_build'], row['upgrade_error']) == (newer, '')


async def test_an_older_node_never_gets_the_mesh_address(env, monkeypatch, tmp_path):  # noqa: F811
    e = env
    fake, nodes, ids, state = await mesh_access_setup(e, monkeypatch, tmp_path)
    url = (await api(e, 'GET', '/api/servers/settings'))['settings']['meshAccess']['url']
    assert url.startswith('http://100.96.')
    # The node takes plain http to the mesh's range (100.64.0.0/10), as an access point too.
    saved = await api(e, 'PUT', '/api/servers/settings', {'accessPoints': [
        {'id': 'ap-direct', 'name': '海外', 'url': DIRECT}, {'id': 'ap-relay', 'name': '中转', 'url': RELAY},
        {'id': 'ap-cgnat', 'name': 'CGNAT', 'url': 'http://100.64.1.2:18961'}], 'defaultAccess': 'ap-direct'})
    assert saved['settings']['accessPoints'][2]['url'] == 'http://100.64.1.2:18961'
    err = await api(e, 'PUT', '/api/servers/settings', {'accessPoints': [{'id': 'x', 'name': 'x', 'url': 'http://100.128.0.1:18961'}]}, status=400)
    assert err['error'] == 'node_url_requires_https'
    reasons = {sid: (await api(e, 'GET', f'/api/servers/{sid}'))['item']['access'] for sid in (ids['m1'], ids['solo'], LOCAL)}
    assert [(a['meshEligible'], a['meshReason']) for a in reasons.values()] == [(True, ''), (False, 'not_member'), (False, 'local')]
    listed = {i['id']: i['access'] for i in (await api(e, 'GET', '/api/servers'))['items']}
    assert listed[ids['m1']]['meshReason'] == '' and listed[ids['solo']]['meshReason'] == 'not_member'

    # m2 chose ap-mesh, then its node is replaced by an older one (no access.mesh).
    await api(e, 'PATCH', f'/api/servers/{ids["m2"]}', {'profile': {'access': {'primary': 'ap-mesh', 'backup': 'ap-relay'}}})
    fake.log.clear()
    await connect(e, ids['m2'], features=('mesh',))
    await settled(e)
    await until(lambda: ids['m2'] not in e.servers.access_applying)
    # It is moved off the mesh address (it would refuse it), to the default and the backup.
    assert fake.urls() == [(ids['m2'], DIRECT, RELAY)]
    access = (await api(e, 'GET', f'/api/servers/{ids["m2"]}'))['item']['access']
    assert (access['meshEligible'], access['meshReason'], access['primary']['id']) == (False, 'node_outdated', 'ap-direct')
    for chosen in ({'primary': 'ap-mesh'}, {'primary': 'ap-direct', 'backup': 'ap-mesh'}):
        err = await api(e, 'PATCH', f'/api/servers/{ids["m2"]}', {'profile': {'access': chosen}}, status=409)
        assert err['error'] == 'mesh_access_node_outdated'
    # Not a member wins over an old node: that one has to join first.
    await connect(e, ids['solo'], features=('mesh',))
    err = await api(e, 'PATCH', f'/api/servers/{ids["solo"]}', {'profile': {'access': {'primary': 'ap-mesh'}}}, status=409)
    assert err['error'] == 'mesh_access_requires_member'
    # Offline, judged by what it last reported.
    e.servers.hub.sessions.pop(ids['m2'])
    access = (await api(e, 'GET', f'/api/servers/{ids["m2"]}'))['item']['access']
    assert access['meshReason'] == 'node_outdated' and access['primary']['id'] == 'ap-direct'
    # Optional: an older node is not one that must be updated.
    from app.servers import releases
    assert 'access.mesh' not in releases.REQUIRED_FEATURES

    # A new revision and pushing everyone's addresses send it no mesh address either.
    fake.log.clear()
    await connect(e, ids['m2'], features=('mesh',))
    await settled(e)
    draft = await api(e, 'GET', '/api/servers/mesh')
    for m in draft['members']:
        if m['serverId'] == ids['m2']:
            m['uploadLimitMbps'] = 50
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members']})
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    await api(e, 'POST', '/api/servers/node-url', {})
    assert all(u != url and b != url for _, u, b in fake.urls())

    # Updated to a node with access.mesh: its chosen addresses go out by themselves.
    fake.log.clear()
    await connect(e, ids['m2'], features=MESHED)
    await settled(e)
    await until(lambda: ids['m2'] not in e.servers.access_applying and fake.urls())
    assert fake.urls() == [(ids['m2'], url, RELAY)]
    access = (await api(e, 'GET', f'/api/servers/{ids["m2"]}'))['item']['access']
    assert (access['meshEligible'], access['meshReason'], access['primary']['id'], access['pending']) == (True, '', 'ap-mesh', False)
    # Reconnecting with the same features sends nothing again.
    fake.log.clear()
    await connect(e, ids['m2'], features=MESHED)
    await settled(e)
    await asyncio.sleep(0.1)
    assert fake.urls() == []
