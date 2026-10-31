"""Network addresses and network tests through the real Web app; nodes are fakes that
answer net.* the way the contract's node section describes."""
from __future__ import annotations

import asyncio
import contextlib

from app.servers import geo
from app.servers import mesh as mesh_module
from app.servers import net as net_module
from tests.test_mesh_service import READY, mesh_setup, server, settled
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


class NetNode:
    """net.* answers in front of the mesh fake; every other method goes to it."""

    def __init__(self, nodes):
        self.nodes, self.calls, self.addrs, self.status, self.errors, self.lost = nodes, [], {}, {}, {}, set()
        self.started = 0

    async def call(self, server_id, method, params, timeout):
        if not method.startswith('net.'):
            return await self.nodes.call(server_id, method, params, timeout)
        if server_id not in self.nodes.e.servers.hub.sessions:
            raise LookupError('node_offline')
        if server_id in self.lost:
            raise ConnectionError('gone')
        self.calls.append((server_id, method, params))
        if (server_id, method) in self.errors:
            return {'ok': False, 'error': self.errors[(server_id, method)]}
        if method == 'net.addrs':
            return {'ok': True, 'result': {'interfaces': self.addrs.get(server_id, [])}}
        if method == 'net.probe.start':
            self.started += 1
            pid = f'p{self.started}'
            self.status[(server_id, pid)] = {
                'id': pid, 'mode': params['mode'], 'state': 'running', 'error': '', 'target': params['target'], 'ip': params['target'],
                'family': 4, 'port': params.get('port', 0), 'startedAt': 1793115000000, 'endedAt': 0, 'results': [], 'summary': None,
                'hops': [], 'reached': False}
            return {'ok': True, 'result': {'id': pid}}
        state = self.status.get((server_id, params['id']))
        if state is None:
            return {'ok': False, 'error': 'not_found'}
        if method == 'net.probe.stop':
            if state['state'] == 'running':
                state.update(state='stopped', endedAt=1793115009000)
            return {'ok': True, 'result': {}}
        return {'ok': True, 'result': {**state, 'hops': [{**h, 'addrs': [dict(a) for a in h['addrs']]} for h in state['hops']]}}

    def last(self, method):
        return next(p for _, m, p in reversed(self.calls) if m == method)


def addr(ip, prefix, family=4):
    return {'ip': ip, 'prefixLen': prefix, 'family': family}


async def mesh_net(e):
    """Berlin and LAX relays, 公司-Win and 家-Mac members, 家里 announcing 10.10.10.0/24 for LAX only."""
    nodes, ids = await mesh_setup(e)
    fake = NetNode(nodes)
    e.servers.hub.call = fake.call
    ids['home'] = await server(e, '家里', ipv4='1.0.0.117')
    ids['solo'] = await server(e, 'solo', exec_=False)
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids[k] for k in ('r1', 'r2', 'm1', 'm2', 'home')]})
    relay = {'role': 'relay', 'listen': {'udp': 51999, 'tcp': 52000, 'tls': 0}}
    entry = {'mode': 'fixed', 'autoReturn': True, 'candidates': [{'relay': ids['r1'], 'transport': 'tcp'}]}
    members = [{'serverId': ids['r1'], **relay}, {'serverId': ids['r2'], **relay}, {'serverId': ids['m1'], 'entry': entry},
               {'serverId': ids['m2'], 'entry': entry}, {'serverId': ids['home'], 'entry': entry, 'gateway': {'subnets': ['10.10.10.0/24']}}]
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members, 'rules': [
        {'src': {'members': [ids['r2']]}, 'dst': {'subnets': ['10.10.10.0/24']}}]})
    assert saved['errors'] == []
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    address = {m['serverId']: (m['address'], m['controlAddress']) for m in state['members']}
    return fake, ids, address


def test_scope_order():
    nets = [net_module.ipaddress.ip_network('100.96.0.0/16'), net_module.ipaddress.ip_network('100.96.255.0/24')]
    cases = {'100.96.0.3': 'mesh', '100.96.255.3': 'mesh', '100.64.0.1': 'cgnat', '100.127.255.1': 'cgnat', '169.254.10.2': 'linklocal',
             '10.10.10.251': 'private', '172.20.0.1': 'private', '192.168.1.2': 'private', 'fd12::1': 'private', '1.0.0.117': 'public',
             '2408:8000::1': 'public', '::ffff:8.8.8.8': 'public', '2001:db8::1': 'other', '127.0.0.1': 'other', '224.0.0.1': 'other', 'ff0e::1': 'other', 'x': 'other'}
    assert {ip: net_module.scope(ip, nets) for ip in cases} == cases
    # With nobody in the mesh there is no mesh range: 100.96/16 is CGNAT space like the rest of 100.64/10.
    assert net_module.scope('100.96.0.3') == 'cgnat'


async def test_addresses_scope_egress_and_mesh_address(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(mesh_module, 'PUBLIC_ADDR_SETTLE_S', 0)
    fake, ids, address = await mesh_net(e)
    m1_mesh, m1_ctrl = address[ids['m1']]
    session = e.servers.hub.sessions[ids['m1']]
    session.info = {**session.info, 'ipv4': '1.0.0.117', 'ipv6': '2408:8000::1'}
    fake.addrs[ids['m1']] = [
        {'name': 'obmesh0', 'up': True, 'mac': '', 'kind': 'mesh', 'addrs': [addr(m1_mesh, 16), addr(m1_ctrl, 24)]},
        {'name': 'eth0', 'up': True, 'mac': '52:54:00:12:34:56', 'kind': 'ethernet',
         'addrs': [addr('10.20.20.5', 24), addr('1.0.0.117', 32), addr('2408:8000::1', 64, 6), addr('fd00::5', 64, 6)]},
        {'name': 'eth1', 'up': False, 'mac': '52:54:00:12:34:57', 'kind': 'ethernet', 'addrs': [addr('169.254.3.4', 16)]},
        {'name': 'ppp0', 'up': True, 'mac': '', 'kind': 'tunnel', 'addrs': [addr('100.64.3.4', 32), addr('2001:db8::1', 64, 6)]}]
    got = await api(e, 'GET', f'/api/servers/{ids["m1"]}/net/addresses')
    assert (got['publicIPv4'], got['publicIPv6'], got['meshAddress']) == ('1.0.0.117', '2408:8000::1', m1_mesh)
    assert [i['name'] for i in got['interfaces']] == ['obmesh0', 'eth0', 'eth1', 'ppp0'] and got['interfaces'][2]['up'] is False
    assert [(a['ip'], a['scope'], a['egress']) for i in got['interfaces'] for a in i['addrs']] == [
        (m1_mesh, 'mesh', False), (m1_ctrl, 'mesh', False), ('10.20.20.5', 'private', False), ('1.0.0.117', 'public', True),
        ('2408:8000::1', 'public', True), ('fd00::5', 'private', False), ('169.254.3.4', 'linklocal', False),
        ('100.64.3.4', 'cgnat', False), ('2001:db8::1', 'other', False)]
    assert got['interfaces'][1]['addrs'][0] == {'ip': '10.20.20.5', 'prefixLen': 24, 'family': 4, 'scope': 'private', 'egress': False}
    assert got['publicIPv4Proxy'] == ''
    assert fake.last('net.addrs') == {}
    # Behind a proxy that sends traffic abroad elsewhere: the exit services abroad saw comes along.
    session.info = {**session.info, 'ipv4Proxy': '1.0.0.92'}
    assert (await api(e, 'GET', f'/api/servers/{ids["m1"]}/net/addresses'))['publicIPv4Proxy'] == '1.0.0.92'
    # Not in the mesh, or on its way out: no mesh address. Commands off on the node: still answered.
    fake.addrs[ids['solo']] = [{'name': 'eth0', 'up': True, 'mac': '', 'kind': 'ethernet', 'addrs': [addr('198.18.0.2', 24)]}]
    solo = await api(e, 'GET', f'/api/servers/{ids["solo"]}/net/addresses')
    assert solo['meshAddress'] == '' and solo['publicIPv6'] == '' and solo['interfaces'][0]['addrs'][0]['scope'] == 'other'
    await api(e, 'DELETE', f'/api/servers/mesh/members/{ids["m2"]}')
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert (await api(e, 'GET', f'/api/servers/{ids["m2"]}/net/addresses'))['meshAddress'] == ''


async def test_errors_from_the_node_and_the_checks(env):  # noqa: F811
    e = env
    fake, ids, _ = await mesh_net(e)
    m1 = ids['m1']
    fake.errors[(m1, 'net.addrs')] = 'unsupported_method'
    assert (await api(e, 'GET', f'/api/servers/{m1}/net/addresses', status=409))['error'] == 'node_upgrade_required'
    for method in ('net.probe.start', 'net.probe.status', 'net.probe.stop'):
        fake.errors[(m1, method)] = 'unsupported_method'
    assert (await api(e, 'POST', f'/api/servers/{m1}/net/probes', {'mode': 'ping', 'target': '1.1.1.1'}, status=409))['error'] == 'node_upgrade_required'
    assert (await api(e, 'GET', f'/api/servers/{m1}/net/probes/p1', status=409))['error'] == 'node_upgrade_required'
    assert (await api(e, 'DELETE', f'/api/servers/{m1}/net/probes/p1', status=409))['error'] == 'node_upgrade_required'
    fake.errors.clear()
    fake.errors[(m1, 'net.probe.start')] = 'probe_busy'
    assert (await api(e, 'POST', f'/api/servers/{m1}/net/probes', {'mode': 'ping', 'target': '1.1.1.1'}, status=409))['error'] == 'probe_busy'
    fake.errors.clear()
    assert (await api(e, 'GET', f'/api/servers/{m1}/net/probes/nope', status=404))['error'] == 'not_found'
    assert (await api(e, 'DELETE', f'/api/servers/{m1}/net/probes/nope', status=404))['error'] == 'not_found'
    fake.lost.add(m1)
    assert (await api(e, 'GET', f'/api/servers/{m1}/net/addresses', status=502))['error'] == 'node_connection_lost'
    fake.lost.clear()
    del e.servers.hub.sessions[m1]
    assert (await api(e, 'GET', f'/api/servers/{m1}/net/addresses', status=409))['error'] == 'node_offline'
    bare = (await api(e, 'POST', '/api/servers', {'name': 'bare'}))['item']['id']
    assert (await api(e, 'GET', f'/api/servers/{bare}/net/addresses', status=404))['error'] == 'node_not_enrolled'
    assert (await api(e, 'POST', f'/api/servers/{bare}/net/probes', {'mode': 'ping', 'target': '1.1.1.1'}, status=404))['error'] == 'node_not_enrolled'

    # Checked before the node is asked; the field says which one is wrong.
    r2 = ids['r2']
    bad = [({'target': '1.1.1.1'}, 'mode'), ({'mode': 'mtr', 'target': '1.1.1.1'}, 'mode'), ({'mode': 'ping'}, 'target'),
           ({'mode': 'ping', 'target': ''}, 'target'), ({'mode': 'ping', 'target': 'a' * 254}, 'target'),
           ({'mode': 'ping', 'target': 'exa mple.com'}, 'target'), ({'mode': 'ping', 'target': 'https://example.com'}, 'target'),
           ({'mode': 'ping', 'target': 'example.com:443'}, 'target'), ({'mode': 'ping', 'target': '[::1]'}, 'target'),
           ({'mode': 'ping', 'target': 7}, 'target'), ({'mode': 'tcping', 'target': 'example.com'}, 'port'),
           ({'mode': 'tcping', 'target': 'example.com', 'port': 0}, 'port'), ({'mode': 'tcping', 'target': 'example.com', 'port': 65536}, 'port'),
           ({'mode': 'tcping', 'target': 'example.com', 'port': '443'}, 'port'), ({'mode': 'ping', 'target': '1.1.1.1', 'family': 5}, 'family'),
           ({'mode': 'ping', 'target': '1.1.1.1', 'family': 6}, 'family'), ({'mode': 'ping', 'target': '2001:db8::1', 'family': 4}, 'family'),
           ({'mode': 'ping', 'target': '1.1.1.1', 'count': 0}, 'count'), ({'mode': 'ping', 'target': '1.1.1.1', 'count': 101}, 'count'),
           ({'mode': 'trace', 'target': '1.1.1.1', 'count': 11}, 'count'), ({'mode': 'ping', 'target': '1.1.1.1', 'count': True}, 'count'),
           ({'mode': 'ping', 'target': '1.1.1.1', 'intervalMs': 199}, 'intervalMs'), ({'mode': 'ping', 'target': '1.1.1.1', 'timeoutMs': 10001}, 'timeoutMs'),
           ({'mode': 'trace', 'target': '1.1.1.1', 'maxHops': 65}, 'maxHops'), ({'mode': 'trace', 'target': '1.1.1.1', 'maxHops': 0}, 'maxHops')]
    before = len(fake.calls)
    for body, field in bad:
        got = await api(e, 'POST', f'/api/servers/{r2}/net/probes', body, status=400)
        assert (got['error'], got['field']) == ('invalid_probe', field), body
    assert len(fake.calls) == before
    # Defaults filled in; only what the mode uses is sent.
    for body, sent in [
        ({'mode': 'ping', 'target': 'example.com'},
         {'mode': 'ping', 'target': 'example.com', 'family': 0, 'count': 10, 'intervalMs': 1000, 'timeoutMs': 2000}),
        ({'mode': 'tcping', 'target': '2001:db8::1', 'port': 443, 'family': 6, 'count': 100, 'maxHops': 3},
         {'mode': 'tcping', 'target': '2001:db8::1', 'family': 6, 'count': 100, 'intervalMs': 1000, 'timeoutMs': 2000, 'port': 443}),
        ({'mode': 'trace', 'target': 'one.one.one.one.', 'port': 80, 'intervalMs': 200, 'timeoutMs': 10000},
         {'mode': 'trace', 'target': 'one.one.one.one.', 'family': 0, 'count': 3, 'intervalMs': 200, 'timeoutMs': 10000, 'maxHops': 30})]:
        assert (await api(e, 'POST', f'/api/servers/{r2}/net/probes', body))['id']
        assert fake.last('net.probe.start') == sent


async def test_mesh_targets_trace_as_ping_and_the_mesh_path(env):  # noqa: F811
    e = env
    fake, ids, address = await mesh_net(e)
    r2, home = ids['r2'], ids['home']

    async def start(src, **body):
        pid = (await api(e, 'POST', f'/api/servers/{src}/net/probes', body))['id']
        return pid, fake.last('net.probe.start'), e.servers.net.probes[(src, pid)]['mesh']

    # Another member's mesh or control address; a subnet behind a gateway src has a route to.
    _, sent, target = await start(r2, mode='trace', target=address[home][0])
    assert target == {'kind': 'member', 'dst': home, 'subnet': None}
    assert sent == {'mode': 'ping', 'target': address[home][0], 'family': 0, 'count': 10, 'intervalMs': 1000, 'timeoutMs': 2000}
    pid_ctrl, sent, target = await start(r2, mode='trace', target=address[home][1], count=4, maxHops=8)
    assert target['dst'] == home and sent['mode'] == 'ping' and sent['count'] == 4 and 'maxHops' not in sent
    pid_gw, sent, target = await start(r2, mode='trace', target='10.10.10.1')
    assert target == {'kind': 'gateway', 'dst': home, 'subnet': '10.10.10.0/24'} and sent['mode'] == 'ping'
    pid_tcp, sent, target = await start(r2, mode='tcping', target='10.10.10.1', port=22)
    assert target['kind'] == 'gateway' and sent['mode'] == 'tcping'
    # Not mesh targets: no route to the subnet, a hostname, the source's own address, a public address,
    # a source outside the mesh, or one on its way out.
    for src, target_ip in [(ids['m1'], '10.10.10.1'), (r2, 'home.example'), (r2, address[r2][0]), (r2, '8.8.8.8'), (ids['solo'], address[home][0])]:
        pid, sent, target = await start(src, mode='trace', target=target_ip)
        assert target is None and sent['mode'] == 'trace', target_ip
        assert (await api(e, 'GET', f'/api/servers/{src}/net/probes/{pid}'))['meshPath'] is None

    # Reports: LAX reaches Berlin, home sits on Berlin.
    await e.servers.mesh.report(r2, {**READY, 'peers': [{'id': ids['r1'], 'kind': 'relay', 'active': True, 'routed': True, 'rttMs': 140}]})
    await e.servers.mesh.report(home, {**READY, 'peers': [{'id': ids['r1'], 'kind': 'entry', 'active': True, 'rttMs': 30}],
                                       'entry': {'candidate': 'c1', 'relay': ids['r1'], 'transport': 'tcp', 'since': 1, 'switches': 0}})
    path = await api(e, 'GET', f'/api/servers/mesh/path?src={r2}&dst={home}')
    hops = [{'node': r2, 'via': '', 'rttMs': None}, {'node': ids['r1'], 'via': 'relay', 'rttMs': 140.0}, {'node': home, 'via': 'entry', 'rttMs': 30.0}]
    assert path['hops'] == hops
    got = await api(e, 'GET', f'/api/servers/{r2}/net/probes/{pid_ctrl}')
    assert (got['mode'], got['requestedMode']) == ('ping', 'trace')
    assert got['meshPath'] == {'kind': 'member', 'dst': home, 'subnet': None, 'hops': hops, 'rttMs': 170.0, 'stale': False}
    got = await api(e, 'GET', f'/api/servers/{r2}/net/probes/{pid_gw}')
    assert got['meshPath'] == {'kind': 'gateway', 'dst': home, 'subnet': '10.10.10.0/24', 'hops': hops, 'rttMs': 170.0, 'stale': False}
    # ping and tcping keep their mode and get the path too.
    got = await api(e, 'GET', f'/api/servers/{r2}/net/probes/{pid_tcp}')
    assert (got['mode'], got['requestedMode'], got['meshPath']['subnet']) == ('tcping', 'tcping', '10.10.10.0/24')
    # The record outlives the test by ten minutes, then the status is the node's alone.
    assert (await api(e, 'DELETE', f'/api/servers/{r2}/net/probes/{pid_tcp}')) == {'ok': True}
    record = e.servers.net.probes[(r2, pid_tcp)]
    assert record['ended'] is not None
    record['ended'] -= net_module.KEEP_S + 1
    got = await api(e, 'GET', f'/api/servers/{r2}/net/probes/{pid_tcp}')
    assert (r2, pid_tcp) not in e.servers.net.probes and got['state'] == 'stopped' and got['meshPath'] is None and got['requestedMode'] == 'tcping'


async def test_trace_hops_get_scope_and_a_cached_network_lookup(env):  # noqa: F811
    e = env
    fake, ids, address = await mesh_net(e)
    asked, gates = [], []

    async def fetch(ips):
        asked.append(list(ips))
        if gates:
            await gates[0].wait()
        return {'8.8.8.8': {'country': 'US', 'countryName': 'United States', 'asn': 15169, 'org': 'Google LLC'}}
    networks = e.servers.net.networks
    networks.fetch = fetch
    clock = [1000.0]
    networks.clock = lambda: clock[0]

    r2 = ids['r2']
    pid = (await api(e, 'POST', f'/api/servers/{r2}/net/probes', {'mode': 'trace', 'target': '8.8.8.8'}))['id']

    def hop(ttl, *ips, reached=False):
        return {'ttl': ttl, 'addrs': [{'ip': ip, 'name': ''} for ip in ips], 'sent': 3, 'received': 3 if ips else 0, 'lossPct': 0.0 if ips else 100.0,
                'lastMs': 1.0 if ips else None, 'minMs': 1.0 if ips else None, 'avgMs': 1.0 if ips else None, 'maxMs': 1.0 if ips else None, 'reached': reached}
    fake.status[(r2, pid)]['hops'] = [hop(1, '10.0.0.1'), hop(2, address[r2][0], '100.64.0.1'), hop(3), hop(4, '1.1.1.1', '8.8.8.8'),
                                      hop(5, '8.8.8.8', reached=True)]

    async def status():
        return await api(e, 'GET', f'/api/servers/{r2}/net/probes/{pid}')

    async def idle():
        for _ in range(100):
            if not networks.pending:
                return
            await asyncio.sleep(0.01)
        raise AssertionError('lookup did not finish')
    first = await status()
    assert first['requestedMode'] == 'trace' and first['meshPath'] is None and first['hops'][2]['addrs'] == []
    # Not waiting for the lookup: public addresses have no network yet, the rest never get one.
    assert [[(a['ip'], a['scope'], a['geo']) for a in h['addrs']] for h in first['hops']] == [
        [('10.0.0.1', 'private', None)], [(address[r2][0], 'mesh', None), ('100.64.0.1', 'cgnat', None)], [],
        [('1.1.1.1', 'public', None), ('8.8.8.8', 'public', None)], [('8.8.8.8', 'public', None)]]
    assert first['hops'][0]['addrs'][0] == {'ip': '10.0.0.1', 'name': '', 'scope': 'private', 'geo': None}
    await idle()
    # One batch for the public ones, each once.
    assert asked == [['1.1.1.1', '8.8.8.8']]
    second = await status()
    google = {'country': 'US', 'countryName': 'United States', 'asn': 15169, 'org': 'Google LLC'}
    assert second['hops'][3]['addrs'][1]['geo'] == google and second['hops'][4]['addrs'][0]['geo'] == google
    # 1.1.1.1 was not found: not asked again for ten minutes, then once more.
    assert second['hops'][3]['addrs'][0]['geo'] is None
    await status()
    await idle()
    assert asked == [['1.1.1.1', '8.8.8.8']]
    clock[0] += geo.NetworkLookups.FAIL_TTL + 1
    await status()
    await idle()
    assert asked == [['1.1.1.1', '8.8.8.8'], ['1.1.1.1']]
    # Found ones stay a day.
    clock[0] += geo.NetworkLookups.TTL - geo.NetworkLookups.FAIL_TTL - 10
    assert networks.get('8.8.8.8') == google
    clock[0] += 20
    assert networks.get('8.8.8.8') is None

    # While a lookup runs, the same address is not asked for again.
    gate = asyncio.Event()
    gates.append(gate)
    asked.clear()
    networks.want(['9.9.9.9', '8.8.8.8'])
    networks.want(['9.9.9.9', '8.8.4.4'])
    await asyncio.sleep(0.05)
    assert asked == [['9.9.9.9', '8.8.8.8'], ['8.8.4.4']]
    gate.set()
    await idle()
    # A failing service is a miss, cached like one; the cache keeps the newest LIMIT entries.
    gates.clear()

    async def broken(ips):
        raise TimeoutError
    networks.fetch = broken
    networks.want(['4.4.4.4'])
    await idle()
    assert '4.4.4.4' in networks.cache and networks.get('4.4.4.4') is None
    networks.fetch, networks.LIMIT = fetch, 3
    networks.want(['5.5.5.5', '6.6.6.6'])
    await idle()
    assert list(networks.cache) == ['4.4.4.4', '5.5.5.5', '6.6.6.6']


class FakeResponse:
    def __init__(self, status, data):
        self.status, self.data = status, data

    async def json(self, content_type=None):
        return self.data


class FakeHttp:
    def __init__(self, answers):
        self.answers, self.urls = answers, []

    @contextlib.asynccontextmanager
    async def get(self, url, **kw):
        self.urls.append(url)
        answer = next((v for k, v in self.answers.items() if url.startswith(k)), None)
        if isinstance(answer, Exception):
            raise answer
        yield FakeResponse(*(answer or (404, {})))


async def test_network_lookup_batches_geojs_then_falls_back():
    http = FakeHttp({
        'https://get.geojs.io/v1/ip/geo.json?ip=8.8.8.8,1.1.1.1,9.9.9.9': (200, [
            {'ip': '8.8.8.8', 'country_code': 'US', 'country': 'United States', 'asn': 15169, 'organization_name': 'Google LLC',
             'organization': 'AS15169 Google LLC'},
            {'ip': '1.1.1.1', 'country_code': 'nil', 'asn': None}]),
        'https://ipwho.is/1.1.1.1': (200, {'success': True, 'country_code': 'AU', 'country': 'Australia',
                                           'connection': {'asn': 13335, 'org': 'APNIC and Cloudflare DNS Resolver project', 'isp': 'Cloudflare, Inc.'}}),
        'https://ipwho.is/9.9.9.9': (200, {'success': False, 'message': 'rate limit'})})
    found = await geo.fetch_networks(http, ['8.8.8.8', '1.1.1.1', '9.9.9.9'])
    assert found == {'8.8.8.8': {'country': 'US', 'countryName': 'United States', 'asn': 15169, 'org': 'Google LLC'},
                     '1.1.1.1': {'country': 'AU', 'countryName': 'Australia', 'asn': 13335, 'org': 'APNIC and Cloudflare DNS Resolver project'}}
    assert http.urls[0] == 'https://get.geojs.io/v1/ip/geo.json?ip=8.8.8.8,1.1.1.1,9.9.9.9' and sorted(http.urls[1:]) == [
        'https://ipwho.is/1.1.1.1', 'https://ipwho.is/9.9.9.9']
    # geojs down: every address from the fallback.
    http = FakeHttp({'https://get.geojs.io': TimeoutError(), 'https://ipwho.is/8.8.8.8': (200, {
        'country_code': 'US', 'country': 'United States', 'connection': {'asn': 'AS15169', 'isp': 'Google LLC'}})})
    assert await geo.fetch_networks(http, ['8.8.8.8']) == {'8.8.8.8': {'country': 'US', 'countryName': 'United States', 'asn': 15169, 'org': 'Google LLC'}}
    assert geo.network_info('', '', None, '') is None and geo.network_info('', '', 64512, 'Unknown') is None
    assert geo.network_info('', '', 2914, 'NTT')['country'] == ''


async def test_network_lookup_asks_the_fallback_when_geojs_names_no_operator():
    # geojs answers a private AS and "Unknown" for CN2 and CTGNet hops.
    unknown = {'country_code': 'CN', 'country': 'China', 'asn': 64512, 'organization_name': 'Unknown', 'organization': 'AS64512 Unknown'}
    http = FakeHttp({
        'https://get.geojs.io/v1/ip/geo.json?ip=59.43.137.217,59.43.22.41': (200, [{'ip': '59.43.137.217', **unknown}, {'ip': '59.43.22.41', **unknown}]),
        'https://ipwho.is/59.43.137.217': (200, {'success': True, 'country_code': 'CN', 'country': 'China',
                                                 'connection': {'asn': 0, 'org': 'Chinatelecom Next Carrying Network backbone'}}),
        'https://ipwho.is/59.43.22.41': (200, {'success': False, 'message': 'rate limit'})})
    assert await geo.fetch_networks(http, ['59.43.137.217', '59.43.22.41']) == {
        '59.43.137.217': {'country': 'CN', 'countryName': 'China', 'asn': None, 'org': 'Chinatelecom Next Carrying Network backbone'},
        '59.43.22.41': {'country': 'CN', 'countryName': 'China', 'asn': None, 'org': ''}}


async def test_trace_status_is_cut_to_size_and_lookups_keep_to_a_quota_per_server(env, monkeypatch):  # noqa: F811
    e = env
    fake, ids, _ = await mesh_net(e)
    asked = []

    async def fetch(ips):
        asked.extend(ips)
        return {}
    networks = e.servers.net.networks
    networks.fetch = fetch
    monkeypatch.setattr(networks, 'QUOTA', 5)
    r1, r2 = ids['r1'], ids['r2']
    pid = (await api(e, 'POST', f'/api/servers/{r2}/net/probes', {'mode': 'trace', 'target': '8.8.8.8'}))['id']
    # A node answering far more than a trace can have: hops, addresses per hop, and long texts.
    hops = [{'ttl': t, 'addrs': [{'ip': f'8.{t}.{n}.1', 'name': 'x' * 5000} for n in range(40)], 'sent': 3, 'received': 3, 'lossPct': 0.0,
             'reached': False} for t in range(1, 201)]
    fake.status[(r2, pid)].update(hops=hops, error='e' * 100000, results=[{'seq': i} for i in range(5000)])

    async def idle():
        for _ in range(100):
            if not networks.pending:
                return
            await asyncio.sleep(0.01)
        raise AssertionError('lookup did not finish')
    got = await api(e, 'GET', f'/api/servers/{r2}/net/probes/{pid}')
    assert len(got['hops']) == net_module.MAX_HOPS and {len(h['addrs']) for h in got['hops']} == {net_module.MAX_HOP_ADDRS}
    assert len(got['hops'][0]['addrs'][0]['name']) == net_module.MAX_TEXT and len(got['error']) == net_module.MAX_TEXT
    assert len(got['results']) == net_module.MAX_RESULTS
    # Only QUOTA addresses go out for this server; the others say they were not looked up.
    await idle()
    addrs = [a for h in got['hops'] for a in h['addrs']]
    assert asked == [a['ip'] for a in addrs[:5]] and all('geoSkipped' not in a for a in addrs[:5])
    assert all(a['geoSkipped'] is True and a['geo'] is None for a in addrs[5:])
    await api(e, 'GET', f'/api/servers/{r2}/net/probes/{pid}')
    await idle()
    assert len(asked) == 5
    # Another server has its own quota, and the quota comes back after its window.
    pid1 = (await api(e, 'POST', f'/api/servers/{r1}/net/probes', {'mode': 'trace', 'target': '9.9.9.9'}))['id']
    fake.status[(r1, pid1)]['hops'] = [{'ttl': 1, 'addrs': [{'ip': '9.9.9.9', 'name': ''}], 'sent': 1, 'received': 1, 'lossPct': 0.0, 'reached': True}]
    assert (await api(e, 'GET', f'/api/servers/{r1}/net/probes/{pid1}'))['hops'][0]['addrs'][0].get('geoSkipped') is None
    await idle()
    assert asked[-1] == '9.9.9.9'
    networks.spent[r2] = (networks.spent[r2][0] - networks.QUOTA_WINDOW, networks.QUOTA)
    await api(e, 'GET', f'/api/servers/{r2}/net/probes/{pid}')
    await idle()
    assert len(asked) == 11
