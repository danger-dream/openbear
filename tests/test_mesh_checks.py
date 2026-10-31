"""Draft checks: addresses, ports and gateway subnets; listen ports checked on the machine with
net.ports (warned on save, refused on apply); gateway subnet suggestions; and rules on all
gateway subnets (allSubnets). Pure functions first, then the real Web app with fake nodes."""
from __future__ import annotations

import asyncio
import copy
import time

import pytest

from app.servers import mesh as mesh_module
from app.servers import mesh_model as model
from app.servers.mesh_config import build, config_hash, exports, gateway_routes
from tests.test_mesh_config import facts, member, own_keys
from tests.test_mesh_rules_all import clean_rules, everyone, mesh, rule
from tests.test_mesh_service import Nodes, connect, server, settled
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


# ----- allSubnets -----
def test_all_subnets_is_cleaned_counts_as_a_side_and_reads_old_rules_unchanged():
    [r] = clean_rules([{'src': {'allSubnets': True, 'prefixes': ['172.16.0.0/24']}, 'dst': {'allSubnets': True, 'all': True, 'members': ['m1']}}])
    assert r['src'] == {'all': False, 'allSubnets': True, 'members': [], 'tags': [], 'prefixes': ['172.16.0.0/24']}
    assert r['dst'] == {'all': True, 'allSubnets': True, 'members': [], 'tags': [], 'subnets': []}
    with pytest.raises(model.MeshInvalid) as bad:
        clean_rules([{'src': {'allSubnets': 'yes'}, 'dst': {'all': True}}])
    assert [(e['path'], e['error']) for e in bad.value.errors] == [('rules[0].src.allSubnets', 'invalid_rule_side')]
    d = mesh(rule({'all': True}, {'allSubnets': True}))
    assert not [e for e in model.validate(d, facts()) if e['path'].startswith('rules')]
    # A stored rule from before the field: the same document and the same configs.
    d = mesh(rule({'members': ['m1']}, {'subnets': ['192.168.1.0/24']}))
    stored = copy.deepcopy(d)
    for r in stored['rules']:
        for side in ('src', 'dst'):
            del r[side]['allSubnets']
    assert model.upgrade(stored) == d
    assert {k: config_hash(v) for k, v in build(model.upgrade(stored), facts(), 'net', 1).items()} == \
        {k: config_hash(v) for k, v in build(d, facts(), 'net', 1).items()}
    # mesh_leave has nothing to clean from it.
    d = mesh(rule({'members': ['m1']}, {'allSubnets': True}))
    assert model.strip(copy.deepcopy(d), 'm1')['rules'] == []
    d = mesh(rule({'all': True}, {'allSubnets': True}))
    assert model.strip(copy.deepcopy(d), 'gw')['rules'] == d['rules']


def test_all_subnets_expands_to_every_gateway_subnet_and_follows_them():
    # Every member to every LAN: each gateway takes its own subnet in, everyone else gets routes to both.
    d = mesh(rule({'all': True}, {'allSubnets': True}))
    configs = build(d, facts(), 'net', 1)
    assert configs['gw']['rules'] == [{'src': everyone(d), 'dst': ['192.168.1.0/24'], 'proto': 'any', 'ports': []}]
    assert configs['m2']['rules'] == [{'src': everyone(d), 'dst': ['10.20.20.0/24'], 'proto': 'any', 'ports': []}]
    assert configs['m1']['rules'] == []  # dst.allSubnets is not the members' own addresses
    routes = gateway_routes(d)
    assert routes == {'r1': ['10.20.20.0/24', '192.168.1.0/24'], 'm1': ['10.20.20.0/24', '192.168.1.0/24'], 'gw': ['10.20.20.0/24'],
                      'm2': ['192.168.1.0/24'], 'r2': ['10.20.20.0/24', '192.168.1.0/24']}
    assert exports(d) == [{'gateway': 'gw', 'subnet': '192.168.1.0/24', 'nodes': ['r1', 'm1', 'm2', 'r2']},
                          {'gateway': 'm2', 'subnet': '10.20.20.0/24', 'nodes': ['r1', 'm1', 'gw', 'r2']}]
    # A subnet announced later is in it with nothing to edit; one dropped is out.
    d['members'][1]['gateway'] = {'subnets': ['172.20.0.0/24'], 'masquerade': True}
    assert gateway_routes(d)['r1'] == ['10.20.20.0/24', '172.20.0.0/24', '192.168.1.0/24']
    d['members'][3]['removing'] = True
    assert gateway_routes(d)['r1'] == ['172.20.0.0/24', '192.168.1.0/24']
    # src.allSubnets: the LANs as sources (like listing them in prefixes); the members they reach route back to all of them.
    d = mesh(rule({'allSubnets': True}, {'members': ['m1']}))
    assert build(d, facts(), 'net', 1)['m1']['rules'] == [{'src': ['192.168.1.0/24', '10.20.20.0/24'], 'dst': ['100.96.0.2/32'], 'proto': 'any',
                                                           'ports': []}]
    assert gateway_routes(d)['m1'] == ['10.20.20.0/24', '192.168.1.0/24'] and gateway_routes(d)['r1'] == []
    # LAN to LAN: each gateway is a source for the other's subnet, never routed to its own.
    d = mesh(rule({'allSubnets': True}, {'allSubnets': True}))
    assert gateway_routes(d) == {'r1': [], 'm1': [], 'gw': ['10.20.20.0/24'], 'm2': ['192.168.1.0/24'], 'r2': []}
    assert build(d, facts(), 'net', 1)['gw']['rules'][0]['src'] == ['192.168.1.0/24', '10.20.20.0/24']
    # A union with members, tags, prefixes and subnets.
    d = mesh(rule({'tags': ['公司'], 'prefixes': ['172.16.5.0/24'], 'allSubnets': True}, {'members': ['r2'], 'allSubnets': True,
                                                                                         'subnets': ['192.168.1.0/25']}))
    configs = build(d, facts(), 'net', 1)
    assert configs['r2']['rules'][0]['src'] == ['100.96.0.2/32', '172.16.5.0/24', '192.168.1.0/24', '10.20.20.0/24']
    assert configs['gw']['rules'][0]['dst'] == ['192.168.1.0/25', '192.168.1.0/24']
    # local already routing a subnet elsewhere is left without it, and warned.
    d = mesh(rule({'all': True}, {'allSubnets': True}), extra=(member('local'),))
    held = {**facts(), 'local': {**facts()['m1'], **own_keys('local'), 'hostRoutes': [{'net': '10.20.0.0/16', 'dev': 'wg9'}]}}
    assert gateway_routes(d, held)['local'] == ['192.168.1.0/24']
    assert [w['error'] for w in model.hints(d, held)] == ['mesh_gateway_route_conflict']


async def test_all_subnets_in_changes_and_the_network_test(env):  # noqa: F811
    e = env
    nodes = Nodes(e)
    e.servers.hub.call = nodes.call
    r1, gw, m1 = await server(e, 'Berlin', ipv4='198.51.100.1'), await server(e, 'GW', ipv4='198.51.100.2'), await server(e, 'M', ipv4='198.51.100.3')
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [r1, gw, m1]})
    members = added['members']
    members[0]['role'] = 'relay'
    members[1]['gateway'] = {'subnets': ['192.168.1.0/24']}
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members, 'rules': [
        {'src': {'all': True}, 'dst': {'allSubnets': True}, 'note': 'lan'}]})
    assert [c['text'] for c in saved['draft']['changes'] if c['kind'] == 'subnet.add'] == ['GW 新增网关网段 192.168.1.0/24，规则 lan 会自动包含它']
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert nodes.configs[m1]['self']['gatewayRoutes'] == ['192.168.1.0/24']
    target = e.servers.net._target(await e.servers.mesh._load(123, 'applied'), {m['serverId']: m for m in members}, m1, '192.168.1.20',
                                   await e.servers.mesh._facts(123))
    assert target == {'kind': 'gateway', 'dst': gw, 'subnet': '192.168.1.0/24'}
    draft = await api(e, 'GET', '/api/servers/mesh')
    draft['members'][1]['gateway'] = {'subnets': ['192.168.2.0/24']}
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members']})
    assert [c['text'] for c in saved['draft']['changes'] if c['kind'].startswith('subnet.')] == [
        'GW 新增网关网段 192.168.2.0/24，规则 lan 会自动包含它', 'GW 移除网关网段 192.168.1.0/24，规则 lan 不再包含它']
    assert saved['exports'] == [{'gateway': gw, 'subnet': '192.168.2.0/24', 'nodes': [r1, m1]}]


# ----- addresses, ports, subnets -----
def test_addresses_ports_and_gateway_subnets_are_checked():
    d = mesh()

    def errors(**changes):
        """The problems saving the draft with m1 (members[1]) changed."""
        sent = [{'serverId': m['serverId'], **(changes if m['serverId'] == 'm1' else {})} for m in d['members']]
        try:
            doc = model.allocate(model.clean_doc({'members': sent}, d))
        except model.MeshInvalid as bad:
            return [(x['path'], x['error'], x['message']) for x in bad.errors]
        return [(x['path'], x['error'], x['message']) for x in model.validate(doc, facts())]
    assert errors(address='abc')[0][:2] == ('members[1].address', 'invalid_address')
    assert errors(address='100.96.0.0') == [('members[1].address', 'address_outside', '不能用 100.96.0.0/16 的网络地址或广播地址')]
    assert errors(address='100.96.255.255')[0][2] == '不能用 100.96.0.0/16 的网络地址或广播地址'
    assert errors(address='10.1.2.3')[0][1:] == ('address_outside', '地址须在 100.96.0.0/16 内，且不在控制地址段 100.96.255.0/24 内')
    assert errors(address='100.96.0.1') == [('members[1].address', 'address_duplicate', '地址和 R1 重复')]
    assert errors(controlAddress='100.96.255.0')[0][1:] == ('address_outside', '不能用 100.96.255.0/24 的网络地址或广播地址')
    for port in (519988, -1, '51999', 1.5):
        assert errors(listen={'udp': port}) == [('members[1].listen.udp', 'invalid_port', '端口须在 1～65535 之间，0 表示不监听')]
    for subnet, message in (('abc', '须是 IPv4 网段（网络地址/前缀长度），例如 192.168.1.0/24'),
                            ('192.168.1.1/24', '须是 IPv4 网段（网络地址/前缀长度），例如 192.168.1.0/24'),
                            ('10.0.0.0/33', '须是 IPv4 网段（网络地址/前缀长度），例如 192.168.1.0/24'),
                            ('0.0.0.0/0', '网段太大，前缀长度至少 8，例如 192.168.1.0/24'),
                            ('127.0.0.0/8', '不能是本机回环、链路本地（169.254.0.0/16）、组播或保留地址段'),
                            ('169.254.0.0/16', '不能是本机回环、链路本地（169.254.0.0/16）、组播或保留地址段'),
                            ('224.0.0.0/24', '不能是本机回环、链路本地（169.254.0.0/16）、组播或保留地址段')):
        assert errors(gateway={'subnets': [subnet]}) == [('members[1].gateway.subnets[0]', 'invalid_subnet', message)], subnet
    assert errors(gateway={'subnets': ['100.96.8.0/24']}) == [('members[1].gateway.subnets[0]', 'gateway_overlaps_mesh',
                                                               '网关网段不能和网格地址段 100.96.0.0/16 重叠')]
    assert errors(gateway={'subnets': ['192.168.1.0/25']}) == [('members[2].gateway.subnets[0]', 'gateway_overlap', '和 M1 的网段 192.168.1.0/25 重叠')]
    assert errors(gateway={'subnets': ['172.30.0.0/24']}) == []


def test_gateway_subnet_suggestions_come_from_the_machines_lans():
    f = {'localPrefixes': ['192.168.1.0/24', '172.17.0.0/16', '10.0.0.5/32', '100.96.3.0/24', '203.0.113.0/24', '10.8.0.0/16', 'bad']}
    assert model.subnet_suggestions(f, '100.96.0.0/16') == ['192.168.1.0/24', '10.8.0.0/16']
    assert model.subnet_suggestions({}, '100.96.0.0/16') == []


# ----- listen ports on the machine -----
def ports_nodes(e, held):
    """Nodes answering net.ports from held {(sid, proto, port): (process, pid, ours)}."""
    nodes = Nodes(e)
    calls, real = [], nodes.call

    async def call(sid, method, params, timeout):
        if method == 'net.ports':
            if sid not in e.servers.hub.sessions:
                raise LookupError('node_offline')
            calls.append((sid, params))
            out = {}
            for proto in ('tcp', 'udp'):
                out[proto] = {}
                for port in params.get(proto, []):
                    found = held.get((sid, proto, port))
                    out[proto][str(port)] = ({'free': False, 'ours': found[2], 'process': found[0], 'pid': found[1]} if found
                                             else {'free': True, 'ours': False, 'process': '', 'pid': 0})
            return {'ok': True, 'result': out}
        return await real(sid, method, params, timeout)
    e.servers.hub.call = call
    return nodes, calls


async def test_listen_ports_in_use_warn_on_save_and_refuse_apply(env):  # noqa: F811
    e = env
    held = {}
    nodes, calls = ports_nodes(e, held)
    features = ('mesh', 'net.ports')
    r1 = await server(e, 'Berlin', ipv4='198.51.100.1', features=features)
    win = await server(e, '公司-Windows', platform='windows', ipv4='198.51.100.3', features=features)
    old = await server(e, 'old', ipv4='198.51.100.4')
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [r1, win]})
    members = added['members']
    members[0]['role'] = 'relay'
    members[1]['listen'] = {'udp': 52020}
    held[(win, 'udp', 52020)] = ('svchost.exe', 4242, False)
    held[(r1, 'udp', 51999)] = ('openbear-mesh', 77, True)  # its own mesh service: fine

    # Adding them already asked about their default ports (the overview it returned); r1's answer is kept.
    assert sorted(calls, key=str) == sorted([(r1, {'tcp': [], 'udp': [51999]}), (win, {'tcp': [], 'udp': [51999]})], key=str)
    calls.clear()
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members})
    ports = [w for w in saved['warnings'] if w['error'].startswith('mesh_port')]
    assert ports == [{'path': 'members[1].listen.udp', 'error': 'mesh_port_in_use',
                      'message': '公司-Windows 的 UDP 端口 52020 已被 svchost.exe（PID 4242） 占用，组网服务无法监听它。请换一个端口，或先停掉占用它的程序'}]
    assert calls == [(win, {'tcp': [], 'udp': [52020]})]
    # Kept for a while: saving again or reading the overview asks nothing.
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members})
    await api(e, 'GET', '/api/servers/mesh')
    assert len(calls) == 1
    # Applying checks again, now: still held, so refused as an error and nothing goes out.
    refused = await api(e, 'POST', '/api/servers/mesh/apply', status=400)
    assert refused['error'] == 'mesh_invalid' and [x['error'] for x in refused['errors']] == ['mesh_port_in_use'] and len(calls) == 3
    assert not nodes.sent('mesh.apply')
    # Freed (or moved to a free port): it goes out.
    del held[(win, 'udp', 52020)]
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert set(nodes.sent('mesh.apply')) == {r1, win}
    # Ports as applied are not asked again; a changed one is.
    calls.clear()
    draft = await api(e, 'GET', '/api/servers/mesh')
    assert not [w for w in draft['warnings'] if w['error'].startswith('mesh_port')] and calls == []
    draft['members'][0]['listen'] = {'udp': 51999, 'tcp': 443}
    held[(r1, 'tcp', 443)] = ('nginx', 0, False)
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members']})
    assert calls == [(r1, {'tcp': [443], 'udp': []})]
    assert [w['message'] for w in saved['warnings'] if w['error'] == 'mesh_port_in_use'] == [
        'Berlin 的 TCP 端口 443 已被 nginx 占用，组网服务无法监听它。请换一个端口，或先停掉占用它的程序']
    # A node without net.ports, or offline: not checked, and said so.
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [old]})
    assert [w for w in added['warnings'] if w['error'] == 'mesh_port_unchecked'] == [
        {'path': 'members[2].listen', 'error': 'mesh_port_unchecked', 'message': 'old 的节点版本不支持端口检查，没有检查端口 UDP 51999 是否被占用'}]
    e.servers.hub.sessions.pop(r1)
    view = await api(e, 'GET', '/api/servers/mesh')
    assert {w['message'] for w in view['warnings'] if w['error'] == 'mesh_port_unchecked'} == {
        'old 的节点版本不支持端口检查，没有检查端口 UDP 51999 是否被占用', 'Berlin 离线，没有检查端口 TCP 443 是否被占用'}
    await connect(e, r1, features=features)


async def test_a_silent_node_never_holds_up_the_overview(env, monkeypatch):  # noqa: F811
    """Connected but not answering net.ports: the overview answers at once from what is known and
    leaves one check running; that no answer is kept a while and said as such; saving waits for the
    check under way instead of asking again; applying asks again, now."""
    e = env
    clock = [1_793_001_600_000]
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: clock[0])
    monkeypatch.setattr(mesh_module, 'PORT_CHECK_TIMEOUT', 0.6)
    nodes = Nodes(e)
    calls, silent, held = [], [True], {}

    async def call(sid, method, params, timeout):
        if method == 'net.ports':
            calls.append(sid)
            if silent[0]:
                await asyncio.sleep(timeout)
                raise TimeoutError
            return {'ok': True, 'result': {proto: {str(port): ({'free': False, 'ours': False, 'process': held[(sid, proto, port)], 'pid': 0}
                                                               if (sid, proto, port) in held else {'free': True})
                                                    for port in params.get(proto, [])} for proto in ('tcp', 'udp')}}
        return await nodes.call(sid, method, params, timeout)
    e.servers.hub.call = call
    a = await server(e, 'A', ipv4='198.51.100.1', features=('mesh', 'net.ports'))
    silence = 'A 的节点没有答复，没有检查端口 UDP 51999 是否被占用'

    def unchecked(view):
        return [w['message'] for w in view['warnings'] if w['error'] == 'mesh_port_unchecked']

    async def overview():
        started = time.monotonic()
        view = await api(e, 'GET', '/api/servers/mesh')
        return view, time.monotonic() - started

    # Adding waits for the check: no answer in time, said as such (not as offline).
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [a]})
    assert unchecked(added) == [silence] and calls == [a]
    # Kept: the overview neither waits nor asks again.
    view, took = await overview()
    assert unchecked(view) == [silence] and took < 0.3 and calls == [a]
    # Once that has run out, reading the overview starts one check in the background and answers
    # at once; reads meanwhile share it.
    clock[0] += mesh_module.PORT_NO_ANSWER_MS
    started = time.monotonic()
    views = await asyncio.gather(*(api(e, 'GET', '/api/servers/mesh') for _ in range(3)))
    assert time.monotonic() - started < 0.3 and all(unchecked(v) == [] for v in views) and calls == [a, a]
    # Saving meanwhile waits for that same check rather than asking a second time.
    members = views[0]['members']
    members[0]['role'] = 'relay'
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members})
    assert unchecked(saved) == [silence] and calls == [a, a]
    view, took = await overview()
    assert unchecked(view) == [silence] and took < 0.3 and calls == [a, a]
    # Applying never goes by what is kept: it asks again, and a port another program holds refuses it.
    silent[0] = False
    held[(a, 'udp', 51999)] = 'nginx'
    refused = await api(e, 'POST', '/api/servers/mesh/apply', status=400)
    assert [x['error'] for x in refused['errors']] == ['mesh_port_in_use'] and calls == [a, a, a]
    assert not nodes.sent('mesh.apply')

async def test_the_overview_suggests_gateway_subnets(env):  # noqa: F811
    e = env
    nodes = Nodes(e)
    e.servers.hub.call = nodes.call
    gw = await server(e, 'GW', ipv4='198.51.100.2')
    nodes.local[gw] = ['192.168.50.0/24', '172.17.0.0/16']
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [gw]})
    assert added['members'][0]['subnetSuggestions'] == ['192.168.50.0/24']
    # Sent back with the draft, it is accepted and not stored.
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': added['members']})
    assert saved['members'][0]['subnetSuggestions'] == ['192.168.50.0/24']
    assert 'subnetSuggestions' not in (await e.servers.mesh._load(123, 'draft'))['members'][0]
    assert (await api(e, 'GET', f'/api/servers/mesh/members/{gw}'))['member']['subnetSuggestions'] == ['192.168.50.0/24']
