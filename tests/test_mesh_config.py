"""Mesh intent to configs without a database: routes, the fixed config sample,
address allocation and each draft check."""
from __future__ import annotations

import copy
import json
import random

import pytest

from app.notice_names import telegram_html
from app.servers import compat, mesh_config, mesh_status
from app.servers import mesh_model as model
from app.servers.mesh_config import build, config_hash, instance_access, next_hops, relay_pairs
from app.servers.mesh_path import find

# Base64 of 32 bytes, as nodes report them: the configs leave out a member whose keys are not.
KEYS = {sid: (f'{sid}wg'.ljust(43, 'A') + '=', f'{sid}ctl'.ljust(43, 'B') + '=', f'{sid}sig'.ljust(43, 'C') + '=', f'{sid}e2e'.ljust(43, 'D') + '=')
        for sid in ('r1', 'r2', 'r3', 'm1', 'm2', 'gw')}


def own_keys(sid):
    """Keys of its own for a node whose facts copy another's: the same end-to-end key twice leaves both out."""
    return dict(zip(('publicKey', 'controlKey', 'signKey', 'e2eKey'),
                    (f'{sid}{k}'.ljust(43, c) + '=' for k, c in (('wg', 'A'), ('ctl', 'B'), ('sig', 'C'), ('e2e', 'D')))))


def member(sid, role='member', **changes):
    m = model.new_member(sid, model.default_settings())
    m['role'] = role
    for key, value in changes.items():
        m[key] = {**m[key], **value} if isinstance(m[key], dict) and isinstance(value, dict) else value
    return m


def doc(*members, relay_links=None, direct=(), rules=()):
    d = model.empty_doc()
    d['members'] = [copy.deepcopy(m) for m in members]
    if relay_links:
        d['relayLinks'] = relay_links
    d['directLinks'], d['rules'] = list(direct), list(rules)
    return model.allocate(d)


def facts(platforms=None, ipv4=None, tls=None):
    platforms, ipv4, tls = platforms or {}, ipv4 or {}, tls or {}
    return {sid: {'name': sid.upper(), 'platform': platforms.get(sid, 'linux'), 'ipv4': ipv4.get(sid, f'203.0.113.{i + 1}'),
                  'publicKey': KEYS[sid][0], 'controlKey': KEYS[sid][1], 'signKey': KEYS[sid][2], 'e2eKey': KEYS[sid][3],
                  'localPrefixes': [], 'tls': tls.get(sid), 'configSchemas': compat.CONFIG_SCHEMA}
            for i, sid in enumerate(KEYS)}


def pair(a, b, cost=1, transport='udp', dialer='a'):
    return {'a': a, 'b': b, 'transport': transport, 'cost': cost, 'dialer': dialer}


# ----- routes -----
def test_full_relay_mesh_links_every_pair_once_and_routes_directly():
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('r3', 'relay'))
    pairs = relay_pairs(d, facts())
    assert {(p['a'], p['b']) for p in pairs} == {('r1', 'r2'), ('r1', 'r3'), ('r2', 'r3')}
    hops = next_hops(['r1', 'r2', 'r3'], pairs)
    assert hops == {'r1': {'r2': 'r2', 'r3': 'r3'}, 'r2': {'r1': 'r1', 'r3': 'r3'}, 'r3': {'r1': 'r1', 'r2': 'r2'}}
    configs = build(d, facts(), 'net', 1)
    dials = {(sid, link['peer']): link['dial'] for sid, c in configs.items() for link in c['links']}
    # Each pair has exactly one dialer: the smaller id by default.
    assert dials == {('r1', 'r2'): True, ('r2', 'r1'): False, ('r1', 'r3'): True, ('r3', 'r1'): False, ('r2', 'r3'): True, ('r3', 'r2'): False}
    assert all(link['kind'] == 'relay' for c in configs.values() for link in c['links'])


def test_custom_relay_links_follow_the_cheapest_path():
    links = {'mode': 'custom', 'pairs': [pair('r1', 'r2'), pair('r2', 'r3'), pair('r1', 'r3', cost=5)]}
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('r3', 'relay'), relay_links=links)
    configs = build(d, facts(), 'net', 1)
    assert configs['r1']['nextHops'] == {'r2': 'r2', 'r3': 'r2'}
    assert configs['r3']['nextHops'] == {'r1': 'r2', 'r2': 'r2'}
    assert configs['r2']['nextHops'] == {'r1': 'r1', 'r3': 'r3'}
    # The expensive link still exists as a link, it just carries no routes.
    assert {link['peer'] for link in configs['r1']['links']} == {'r2', 'r3'}
    links['pairs'][2]['cost'] = 1
    assert build(d, facts(), 'net', 1)['r1']['nextHops'] == {'r2': 'r2', 'r3': 'r3'}


def test_a_relay_without_a_path_is_unreachable_and_refused():
    links = {'mode': 'custom', 'pairs': [pair('r1', 'r2')]}
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('r3', 'relay'), relay_links=links)
    configs = build(d, facts(), 'net', 1)
    assert configs['r1']['nextHops'] == {'r2': 'r2'} and configs['r3']['nextHops'] == {}
    assert [e['error'] for e in model.validate(d, facts())] == ['relay_unreachable']


def test_direct_links_only_join_their_two_members():
    d = doc(member('r1', 'relay'), member('m1'), member('m2', listen={'tcp': 52000}),
            direct=[{'a': 'm1', 'b': 'm2', 'transport': 'tcp', 'dialer': 'a'}])
    configs = build(d, facts(), 'net', 1)
    assert configs['m1']['links'] == [{'peer': 'm2', 'kind': 'direct', 'dial': True, 'endpoint': 'tcp://203.0.113.5:52000'}]
    assert configs['m2']['links'] == [{'peer': 'm1', 'kind': 'direct', 'dial': False}]
    assert configs['r1']['links'] == []


# ----- the fixed sample -----
def sample():
    return doc(
        member('r1', 'relay', publicEndpoint='berlin.example.net', listen={'tcp': 52000}),
        member('r2', 'relay', listen={'udp': 0, 'tls': 52001}),
        member('m1', tags=['公司'], entry={'mode': 'failover', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'tcp'},
                                                                             {'id': 'c2', 'relay': 'r2', 'transport': 'tls'}], 'autoReturn': True}),
        member('gw', gateway={'subnets': ['192.168.1.0/24'], 'masquerade': True}, uploadLimitMbps=45,
               entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}], 'autoReturn': True}),
        rules=[{'id': 'r1', 'src': {'members': [], 'tags': ['公司'], 'prefixes': []}, 'dst': {'members': ['gw'], 'tags': [], 'subnets': []},
                'proto': 'tcp', 'ports': [[22, 22]], 'note': ''},
               {'id': 'r2', 'src': {'members': ['m1'], 'tags': [], 'prefixes': ['10.9.0.0/16']},
                'dst': {'members': [], 'tags': [], 'subnets': ['192.168.1.0/25']}, 'proto': 'any', 'ports': [], 'note': ''}])


TLS = {'r2': {'active': {'id': 'a', 'fingerprint': 'aa' * 32}, 'pending': {'id': 'b', 'fingerprint': 'bb' * 32}}}


def test_config_sample_is_exact():
    configs = build(sample(), facts(tls=TLS), 'net-1', 7)
    health = configs['m1']['health']
    assert health['activeIntervalMs'] == 1000 and health['leaseRefreshMs'] == 5000
    m1 = {k: v for k, v in configs['m1'].items() if k != 'health'}
    assert m1 == {
        'schema': 2, 'revision': 7,
        'network': {'id': 'net-1', 'prefix': '100.96.0.0/16', 'controlPrefix': '100.96.255.0/24', 'mtu': 1400},
        'self': {'id': 'm1', 'name': 'M1', 'role': 'member', 'address': '100.96.0.3', 'controlAddress': '100.96.255.3',
                 'listen': {'udp': 51999, 'tcp': 0, 'tls': 0}, 'gateway': None, 'uploadLimitMbps': None, 'resources': 'member',
                 'gatewayRoutes': ['192.168.1.0/25']},
        'nodes': [
            {'id': 'gw', 'name': 'GW', 'role': 'member', 'address': '100.96.0.4', 'controlAddress': '100.96.255.4',
             'publicKey': KEYS['gw'][0], 'controlKey': KEYS['gw'][1], 'signKey': KEYS['gw'][2], 'e2eKey': KEYS['gw'][3],
             'subnets': ['192.168.1.0/24'], 'relays': ['r1']},
            {'id': 'r1', 'name': 'R1', 'role': 'relay', 'address': '100.96.0.1', 'controlAddress': '100.96.255.1',
             'publicKey': KEYS['r1'][0], 'controlKey': KEYS['r1'][1], 'signKey': KEYS['r1'][2], 'e2eKey': KEYS['r1'][3],
             'subnets': [], 'relays': []},
            {'id': 'r2', 'name': 'R2', 'role': 'relay', 'address': '100.96.0.2', 'controlAddress': '100.96.255.2',
             'publicKey': KEYS['r2'][0], 'controlKey': KEYS['r2'][1], 'signKey': KEYS['r2'][2], 'e2eKey': KEYS['r2'][3],
             'subnets': [], 'relays': []}],
        'links': [],
        'entry': {'mode': 'failover', 'autoReturn': True, 'candidates': [
            {'id': 'c1', 'relay': 'r1', 'endpoint': 'tcp://berlin.example.net:52000'},
            {'id': 'c2', 'relay': 'r2', 'endpoint': 'tls://203.0.113.2:52001', 'tlsPins': ['aa' * 32, 'bb' * 32]}]},
        'nextHops': {},
        'rules': [],
        # Every node gets reflectors: public relays listening on UDP, its entry relays first.
        'reflectors': ['berlin.example.net:51999'],
        'reachability': {'stun': []},
    }
    gw = configs['gw']
    assert gw['self']['gateway'] == {'subnets': ['192.168.1.0/24'], 'masquerade': True} and gw['self']['uploadLimitMbps'] == 45
    assert gw['rules'] == [{'src': ['100.96.0.3/32'], 'dst': ['100.96.0.4/32'], 'proto': 'tcp', 'ports': [[22, 22]]},
                           {'src': ['100.96.0.3/32', '10.9.0.0/16'], 'dst': ['192.168.1.0/25'], 'proto': 'any', 'ports': []}]
    r1 = configs['r1']
    assert r1['entry'] is None and r1['nextHops'] == {'r2': 'r2'} and r1['self']['resources'] == 'relay'
    assert r1['routing'] == {'mode': 'dynamic', 'bias': {'r2': 0}} and r1['reflectors'] == [] and 'routing' not in configs['m1']
    # By default relays prefer UDP: r2 has no UDP listener, so it dials r1's.
    assert r1['links'] == [{'peer': 'r2', 'kind': 'relay', 'dial': False}]
    assert configs['r2']['links'] == [{'peer': 'r1', 'kind': 'relay', 'dial': True, 'endpoint': 'udp://berlin.example.net:51999'}]
    d = sample()
    d['relayLinks']['pairs'] = [pair('r1', 'r2', transport='tls')]
    tls_link = build(d, facts(tls=TLS), 'net-1', 7)['r1']['links']
    assert tls_link == [{'peer': 'r2', 'kind': 'relay', 'dial': True, 'endpoint': 'tls://203.0.113.2:52001', 'tlsPins': ['aa' * 32, 'bb' * 32]}]
    assert [n['relays'] for n in r1['nodes']] == [['r1'], ['r1', 'r2'], []]


def test_configs_are_deterministic():
    d = sample()
    first = build(d, facts(tls=TLS), 'net-1', 7)
    shuffled = copy.deepcopy(d)
    random.Random(4).shuffle(shuffled['members'])
    again = build(shuffled, facts(tls=TLS), 'net-1', 7)
    assert first == again
    assert {k: config_hash(v) for k, v in build(d, facts(tls=TLS), 'net-1', 8).items()} == {k: config_hash(v) for k, v in first.items()}


def test_a_rule_for_a_gateway_machine_does_not_open_its_subnets():
    rule = {'id': 'r1', 'src': {'members': ['m1'], 'tags': [], 'prefixes': []}, 'dst': {'members': ['gw'], 'tags': [], 'subnets': []},
            'proto': 'any', 'ports': [], 'note': ''}
    d = doc(member('r1', 'relay'), member('m1'), member('gw', gateway={'subnets': ['192.168.1.0/24'], 'masquerade': True}), rules=[rule])
    assert build(d, facts(), 'net', 1)['gw']['rules'] == [{'src': ['100.96.0.2/32'], 'dst': ['100.96.0.3/32'], 'proto': 'any', 'ports': []}]
    # The subnet is reachable only when a rule names it, alone or next to the machine.
    rule['dst']['subnets'] = ['192.168.1.0/24']
    assert build(d, facts(), 'net', 1)['gw']['rules'][0]['dst'] == ['100.96.0.3/32', '192.168.1.0/24']
    rule['dst']['members'] = []
    assert build(d, facts(), 'net', 1)['gw']['rules'][0]['dst'] == ['192.168.1.0/24']
    assert all(r['dst'] for c in build(sample(), facts(tls=TLS), 'net-1', 7).values() for r in c['rules'])


def test_an_instance_on_a_mesh_address_is_open_to_the_gateway_server_alone():
    """r1 relay .1, m1 .2 (the gateway's server), m2 .3 (two instances on its mesh address), gw .4."""
    user = {'id': 'u1', 'src': {'members': ['gw'], 'tags': [], 'prefixes': []}, 'dst': {'members': ['m2'], 'tags': [], 'subnets': []},
            'proto': 'tcp', 'ports': [[22, 22]], 'note': ''}
    d = doc(member('r1', 'relay'), member('m1'), member('m2'), member('gw'), rules=[user])
    f = facts()
    f['m2']['instancePorts'] = [{'id': 'b', 'name': 'Edge-Fra', 'port': 18971, 'gateway': 'm1', 'bind': '100.96.0.3'},
                                {'id': 'a', 'name': 'Other', 'port': 18970, 'gateway': 'm1', 'bind': '100.96.0.3'},
                                {'id': 'c', 'name': 'Elsewhere', 'port': 18972, 'gateway': 'm1', 'bind': '203.0.113.3'}]
    configs = build(d, f, 'net', 1)
    # One rule from the gateway's server to this server, tcp, the instance ports only (not one listening on
    # another address); the user's rule as before.
    assert configs['m2']['rules'] == [{'src': ['100.96.0.2/32'], 'dst': ['100.96.0.3/32'], 'proto': 'tcp', 'ports': [[18970, 18970], [18971, 18971]]},
                                      {'src': ['100.96.0.4/32'], 'dst': ['100.96.0.3/32'], 'proto': 'tcp', 'ports': [[22, 22]]}]
    # Nowhere else, and no wider: not the gateway's own config, not other members.
    assert all(c['rules'] == [] for sid, c in configs.items() if sid != 'm2')
    assert [(a['instanceId'], a['name'], a['port'], a['src'], a['dst']) for a in instance_access(d, f)] == [
        ('a', 'Other', 18970, '100.96.0.2/32', '100.96.0.3/32'), ('b', 'Edge-Fra', 18971, '100.96.0.2/32', '100.96.0.3/32')]

    # The gateway's server leaving the mesh (or losing its address, or its keys) takes the rule with it.
    for gone in (doc(member('r1', 'relay'), member('m1', removing=True), member('m2'), member('gw'), rules=[user]),):
        assert build(gone, f, 'net', 1)['m2']['rules'] == [{'src': ['100.96.0.4/32'], 'dst': ['100.96.0.3/32'], 'proto': 'tcp', 'ports': [[22, 22]]}]
        assert instance_access(gone, f) == []
    keyless = copy.deepcopy(f)
    keyless['m1']['signKey'] = ''
    assert [r['ports'] for r in build(d, keyless, 'net', 1)['m2']['rules']] == [[[22, 22]]]
    # A gateway on the instance's own server reaches it on loopback: no rule.
    f['m2']['instancePorts'] = [{'id': 'a', 'name': 'Other', 'port': 18970, 'gateway': 'm2', 'bind': '100.96.0.3'}]
    assert build(d, f, 'net', 1)['m2']['rules'] == [{'src': ['100.96.0.4/32'], 'dst': ['100.96.0.3/32'], 'proto': 'tcp', 'ports': [[22, 22]]}]


def rule(src=None, dst=None, **sides):
    return {'id': '', 'src': {'members': [], 'tags': [], 'prefixes': [], **(src or {})},
            'dst': {'members': [], 'tags': [], 'subnets': [], **(dst or {})}, 'proto': 'any', 'ports': [], 'note': ''}


def gateway_doc(*rules):
    """r1 100.96.0.1, m1 .2 (tag 公司), gw .3 announcing 192.168.1.0/24, m2 .4 announcing 10.20.20.0/24, r2 .5 (tag edge)."""
    d = doc(member('r1', 'relay'), member('m1', tags=['公司']), member('gw', gateway={'subnets': ['192.168.1.0/24'], 'masquerade': True}),
            member('m2', gateway={'subnets': ['10.20.20.0/24'], 'masquerade': True}), member('r2', 'relay', tags=['edge']), rules=list(rules))
    d['rules'] = [{**r, 'id': f'r{i + 1}'} for i, r in enumerate(d['rules'])]
    return d


def test_gateway_routes_only_where_a_rule_lets_a_node_reach_or_answer_a_subnet():
    from app.servers.mesh_config import exports, gateway_routes
    # No rule: nobody gets a route, and every node is still sent the (empty) list.
    d = gateway_doc()
    assert gateway_routes(d) == {sid: [] for sid in ('r1', 'm1', 'gw', 'm2', 'r2')}
    assert all(c['self']['gatewayRoutes'] == [] for c in build(d, facts(), 'net', 1).values())
    assert exports(d) == [{'gateway': 'gw', 'subnet': '192.168.1.0/24', 'nodes': []}, {'gateway': 'm2', 'subnet': '10.20.20.0/24', 'nodes': []}]
    d = gateway_doc(
        # The node reaches subnets: src names it by member, tag or a prefix holding its address; a narrower one is merged away.
        rule({'members': ['m1']}, {'subnets': ['192.168.1.0/25']}),
        rule({'tags': ['公司']}, {'subnets': ['192.168.1.0/24']}),
        rule({'prefixes': ['100.96.0.5/32']}, {'subnets': ['10.20.20.0/24']}),
        # Subnets reach the node: its answers need the sources inside gateway subnets (a public prefix is not one).
        rule({'prefixes': ['192.168.1.128/25', '203.0.113.0/24']}, {'members': ['r1']}),
        rule({'prefixes': ['10.0.0.0/8']}, {'members': ['m1']}),
        # src names a gateway: only the machine itself, the network behind it is not a source.
        rule({'members': ['gw']}, {'tags': ['edge']}),
        # A gateway's own subnet is never in its list; site to site, each gateway gets the other side.
        rule({'members': ['gw']}, {'subnets': ['192.168.1.0/24']}),
        rule({'prefixes': ['192.168.1.0/24']}, {'subnets': ['10.20.20.0/24']}))
    routes = gateway_routes(d)
    assert routes == {'r1': ['192.168.1.128/25'], 'm1': ['10.20.20.0/24', '192.168.1.0/24'], 'gw': ['10.20.20.0/24'],
                      'm2': ['192.168.1.0/24'], 'r2': ['10.20.20.0/24']}
    assert {sid: c['self']['gatewayRoutes'] for sid, c in build(d, facts(), 'net', 1).items()} == routes
    assert exports(d) == [{'gateway': 'gw', 'subnet': '192.168.1.0/24', 'nodes': ['r1', 'm1', 'm2']},
                          {'gateway': 'm2', 'subnet': '10.20.20.0/24', 'nodes': ['m1', 'gw', 'r2']}]
    # Naming the network behind the gateway is what lets it reach the rule's destinations.
    d['rules'].append(rule({'prefixes': ['192.168.1.0/24']}, {'tags': ['edge']}))
    assert gateway_routes(d)['r2'] == ['10.20.20.0/24', '192.168.1.0/24']
    # A member on its way out gets nothing and is in no export.
    d['members'][1]['removing'] = True
    assert 'm1' not in gateway_routes(d) and all('m1' not in e['nodes'] for e in exports(d))


def test_the_home_subnet_goes_only_to_the_machine_the_rule_names():
    from app.servers.mesh_config import exports
    d = gateway_doc(rule({'members': ['r2']}, {'subnets': ['192.168.1.0/24']}))
    configs = build(d, facts(), 'net', 1)
    assert {sid: c['self']['gatewayRoutes'] for sid, c in configs.items()} == {
        'r1': [], 'm1': [], 'gw': [], 'm2': [], 'r2': ['192.168.1.0/24']}
    assert exports(d)[0] == {'gateway': 'gw', 'subnet': '192.168.1.0/24', 'nodes': ['r2']}
    # What the gateway enforces is unchanged: r2 may reach the subnet.
    assert configs['gw']['rules'] == [{'src': ['100.96.0.5/32'], 'dst': ['192.168.1.0/24'], 'proto': 'any', 'ports': []}]


def test_an_everyone_rule_does_not_open_the_network_behind_a_gateway():
    from app.servers.mesh_config import exports
    everyone = ['r1', 'm1', 'gw', 'm2', 'r2']
    d = gateway_doc(rule({'members': everyone}, {'members': everyone}), rule({'members': ['r2']}, {'subnets': ['192.168.1.0/24']}))
    configs = build(d, facts(), 'net', 1)
    assert exports(d)[0] == {'gateway': 'gw', 'subnet': '192.168.1.0/24', 'nodes': ['r2']}
    assert all('192.168.1.0/24' not in r['src'] for c in configs.values() for r in c['rules'])
    assert [r for r in configs['gw']['rules'] if '192.168.1.0/24' in r['dst']] == [
        {'src': ['100.96.0.5/32'], 'dst': ['192.168.1.0/24'], 'proto': 'any', 'ports': []}]


def lan_candidate(addresses, **r3):
    d = site_doc()
    d['members'][2].update(r3)
    f = facts(ipv4=SITE_IPV4)
    for sid, values in addresses.items():
        f[sid]['localAddresses'] = values
    return build(d, f, 'net', 1)['m1']['entry']['candidates'][0]


def test_a_member_on_its_site_relays_network_enters_it_at_the_lan_address():
    assert lan_candidate({}) == {'id': 'c1', 'relay': 'r3', 'punch': True}
    # Same network: the relay's LAN address and its UDP port.
    same = {'m1': ['10.20.20.9/24'], 'r3': ['10.20.20.7/24']}
    assert lan_candidate(same) == {'id': 'c1', 'relay': 'r3', 'punch': True, 'endpoint': 'udp://10.20.20.7:51999'}
    assert lan_candidate(same, listen={'udp': 51998, 'tcp': 0, 'tls': 0})['endpoint'] == 'udp://10.20.20.7:51998'
    # Several addresses: the relay's one on the member's network.
    assert lan_candidate({'m1': ['192.168.9.3/24', '10.20.20.9/24'], 'r3': ['172.20.0.1/16', '10.20.20.7/24']})['endpoint'] == 'udp://10.20.20.7:51999'
    assert lan_candidate({'m1': ['10.20.0.9/16'], 'r3': ['192.168.9.1/24', '10.20.20.7/24']})['endpoint'] == 'udp://10.20.20.7:51999'
    # Different networks, a shared default container bridge, public or broken addresses: none.
    assert 'endpoint' not in lan_candidate({'m1': ['10.30.0.9/24'], 'r3': ['10.20.20.7/24']})
    assert 'endpoint' not in lan_candidate({'m1': ['172.17.0.2/16', '10.88.0.3/16'], 'r3': ['172.17.0.1/16', '10.88.0.2/16']})
    assert 'endpoint' not in lan_candidate({'m1': ['203.0.113.9/24', 'bad', 7], 'r3': ['203.0.113.7/24']})
    # No UDP on the relay: nothing to aim at.
    assert 'endpoint' not in lan_candidate(same, listen={'udp': 0, 'tcp': 0, 'tls': 0})
    # The LAN address comes before an entered public one, which stays for members elsewhere.
    assert lan_candidate(same, publicEndpoint='toy.example.net')['endpoint'] == 'udp://10.20.20.7:51999'
    assert lan_candidate({'m1': ['10.30.0.9/24'], 'r3': ['10.20.20.7/24']}, publicEndpoint='toy.example.net')['endpoint'] == 'udp://toy.example.net:51999'
    # Only entry candidates: the relay links of the site relay stay as they were.
    d = site_doc()
    f = facts(ipv4=SITE_IPV4)
    f['m1']['localAddresses'] = f['gw']['localAddresses'] = ['10.20.20.9/24']
    f['r3']['localAddresses'] = ['10.20.20.7/24']
    assert all('endpoint' not in link for link in build(d, f, 'net', 1)['gw']['links'] if link['peer'] == 'r3')


# ----- addresses -----
def test_addresses_are_allocated_and_reallocated_with_a_new_prefix():
    d = doc(member('r1', 'relay'), member('m1'), member('m2', address='100.96.0.2'))
    assert [(m['address'], m['controlAddress']) for m in d['members']] == [
        ('100.96.0.1', '100.96.255.1'), ('100.96.0.3', '100.96.255.3'), ('100.96.0.2', '100.96.255.2')]
    data = {'settings': {'prefix': '100.100.0.0/22'}, 'members': [{'serverId': m['serverId']} for m in d['members']]}
    moved = model.allocate(model.clean_doc(data, d))
    assert moved['settings']['controlPrefix'] == '100.100.3.0/24'
    assert [m['address'] for m in moved['members']] == ['100.100.0.1', '100.100.0.2', '100.100.0.3']
    assert not model.validate(moved, facts())


# ----- each check -----
def errors(d, f=None, applying=False):
    return {(e['path'], e['error']) for e in model.validate(d, f or facts(), applying=applying)}


def base():
    return doc(member('r1', 'relay', listen={'tcp': 52000}), member('r2', 'relay'), member('m1'), member('m2'))


def test_a_valid_draft_has_no_errors():
    assert errors(base()) == set()


@pytest.mark.parametrize('change, f, expected', [
    (lambda d: d['members'][2].update(address='10.0.0.1'), None, ('members[2].address', 'address_outside')),
    (lambda d: d['members'][2].update(address='100.96.255.9'), None, ('members[2].address', 'address_outside')),
    (lambda d: d['members'][3].update(address='100.96.0.3'), None, ('members[3].address', 'address_duplicate')),
    (lambda d: d['members'][3].update(controlAddress='100.96.255.3'), None, ('members[3].controlAddress', 'address_duplicate')),
    (lambda d: None, {'m1': {'localPrefixes': ['100.96.8.0/24']}}, ('settings.prefix', 'prefix_conflict')),
    (lambda d: None, {'m2': {'ipv4': '100.96.3.3'}}, ('settings.prefix', 'prefix_conflict')),
    (lambda d: None, {'r2': {'platform': 'windows'}}, ('members[1].role', 'relay_requires_linux')),
    (lambda d: d['members'][2].update(gateway={'subnets': ['10.1.0.0/24'], 'masquerade': True}), {'m1': {'platform': 'darwin'}},
     ('members[2].gateway', 'gateway_requires_linux')),
    (lambda d: d['members'][2]['entry']['candidates'].append({'id': 'c1', 'relay': 'm2', 'transport': 'udp'}), None,
     ('members[2].entry.candidates[0]', 'candidate_not_relay')),
    (lambda d: d['members'][2]['entry']['candidates'].append({'id': 'c1', 'relay': 'r2', 'transport': 'tcp'}), None,
     ('members[2].entry.candidates[0]', 'transport_not_listening')),
    (lambda d: d['members'][2]['entry']['candidates'].append({'id': 'c1', 'relay': 'r1', 'transport': 'udp'}), {'r1': {'ipv4': ''}},
     ('members[2].entry.candidates[0]', 'endpoint_missing')),
    (lambda d: d['members'][2]['entry']['candidates'].extend([{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}, {'id': 'c2', 'relay': 'r1', 'transport': 'udp'}]),
     None, ('members[2].entry.candidates[1]', 'candidate_duplicate')),
    (lambda d: [d['members'][i].update(gateway={'subnets': ['192.168.0.0/16'], 'masquerade': True}) for i in (2, 3)] and
     d['members'][3].update(gateway={'subnets': ['192.168.1.0/24'], 'masquerade': False}), None, ('members[3].gateway.subnets[0]', 'gateway_overlap')),
    (lambda d: d['members'][2].update(gateway={'subnets': ['100.96.4.0/24'], 'masquerade': True}), None,
     ('members[2].gateway.subnets[0]', 'gateway_overlaps_mesh')),
    (lambda d: d['members'][0]['listen'].update(tcp=51999), None, ('members[0].listen.tcp', 'port_duplicate')),
    (lambda d: d['settings']['ports'].update(tcp=51999), None, ('settings.ports', 'port_duplicate')),
    (lambda d: d['members'][2]['listen'].update(tls=52001), None, ('members[2].listen.tls', 'tls_relay_only')),
    (lambda d: d['directLinks'].append({'a': 'm1', 'b': 'r1', 'transport': 'udp', 'dialer': 'a'}), None, ('directLinks[0]', 'direct_not_members')),
    (lambda d: d['directLinks'].append({'a': 'm1', 'b': 'm2', 'transport': 'tcp', 'dialer': 'a'}), None, ('directLinks[0]', 'transport_not_listening')),
    (lambda d: d['members'][2]['listen'].update(tcp=52000) or d['directLinks'].append({'a': 'm1', 'b': 'm2', 'transport': 'tcp', 'dialer': 'b'}),
     {'m1': {'ipv4': ''}}, ('directLinks[0]', 'endpoint_missing')),
    (lambda d: d['members'][3]['listen'].update(udp=0, tcp=52000) or d['directLinks'].append({'a': 'm1', 'b': 'm2', 'transport': 'udp', 'dialer': 'a'}),
     None, ('directLinks[0]', 'udp_not_listening')),
    (lambda d: d['directLinks'].append({'a': 'm1', 'b': 'm2', 'transport': 'udp', 'dialer': 'a'}),
     {sid: {'ipv4': ''} for sid in ('r1', 'r2', 'm1', 'm2')}, ('directLinks[0]', 'punch_no_reflector')),
    # A relay with a public address but no UDP listener cannot reflect.
    (lambda d: d['members'][1]['listen'].update(udp=0, tcp=52000) or d['directLinks'].append({'a': 'm1', 'b': 'm2', 'transport': 'udp', 'dialer': 'a'}),
     {sid: {'ipv4': ''} for sid in ('r1', 'm1', 'm2')}, ('directLinks[0]', 'punch_no_reflector')),
    (lambda d: d['rules'].append({'id': 'x', 'src': {'members': ['m9'], 'tags': [], 'prefixes': []}, 'dst': {'members': ['m1'], 'tags': [], 'subnets': []},
                                  'proto': 'any', 'ports': [], 'note': ''}), None, ('rules[0].src.members[0]', 'rule_member_unknown')),
    (lambda d: d['rules'].append({'id': 'x', 'src': {'members': ['m2'], 'tags': [], 'prefixes': []}, 'dst': {'members': [], 'tags': [], 'subnets': ['10.5.0.0/24']},
                                  'proto': 'any', 'ports': [], 'note': ''}), None, ('rules[0].dst.subnets[0]', 'rule_subnet_not_gateway')),
    (lambda d: d['rules'].append({'id': 'x', 'src': {'members': ['m2'], 'tags': [], 'prefixes': []}, 'dst': {'members': ['m1'], 'tags': [], 'subnets': []},
                                  'proto': 'icmp', 'ports': [[1, 2]], 'note': ''}), None, ('rules[0].ports', 'rule_ports_need_proto')),
])
def test_each_check_names_its_field(change, f, expected):
    d = base()
    change(d)
    merged = facts()
    for sid, patch in (f or {}).items():
        merged[sid].update(patch)
    assert expected in errors(d, merged)


def test_tls_pins_are_required_only_when_applying():
    d = base()
    d['members'][1]['listen']['tls'] = 52001
    d['members'][2]['entry']['candidates'] = [{'id': 'c1', 'relay': 'r2', 'transport': 'tls'}]
    assert errors(d) == set()
    assert errors(d, applying=True) == {('members[2].entry.candidates[0]', 'tls_fingerprint_missing')}
    assert errors(d, facts(tls=TLS), applying=True) == set()


def test_shape_errors_carry_paths_and_ids_are_filled():
    d = base()
    data = {'members': [{'serverId': 'r1'}, {'serverId': 'r2'}, {'serverId': 'm1', 'entry': {'candidates': [{'relay': 'r1', 'transport': 'quic'}]}},
                        {'serverId': 'm2', 'listen': {'udp': 70000}}],
            'rules': [{'src': {'members': ['m1']}, 'dst': {'members': ['m2']}, 'ports': [[30, 20]]}]}
    with pytest.raises(model.MeshInvalid) as raised:
        model.clean_doc(data, d)
    assert {e['path'] for e in raised.value.errors} == {'members[2].entry.candidates[0]', 'members[3].listen.udp', 'rules[0].ports[0]'}
    with pytest.raises(model.MeshInvalid) as raised:
        model.clean_doc({'members': [{'serverId': 'r1'}]}, d)
    assert raised.value.errors[0]['error'] == 'member_missing'
    data = {'members': [{'serverId': s} for s in ('r1', 'r2', 'm2')] + [{'serverId': 'm1', 'entry': {'candidates': [{'relay': 'r1', 'transport': 'tcp'}]}}],
            'rules': [{'src': {'tags': ['x']}, 'dst': {'members': ['m2']}}]}
    cleaned = model.clean_doc(data, d)
    assert cleaned['members'][3]['entry']['candidates'] == [{'id': 'c1', 'relay': 'r1', 'transport': 'tcp'}]
    assert cleaned['rules'][0]['id'] == 'r1' and cleaned['rules'][0]['proto'] == 'any'


@pytest.mark.parametrize('health, field', [
    ({'minTimeoutMs': 4000}, 'initialTimeoutMs'),
    ({'maxTimeoutMs': 2500}, 'initialTimeoutMs'),
    ({'leaseRefreshMs': 30000}, 'leaseRefreshMs'),
    ({'coldRetryMs': 300001}, 'coldRetryMs'),
    ({'consecutiveFailures': 17}, 'consecutiveFailures'),
    ({'readySuccesses': 4, 'activeIntervalMs': 2600}, 'activeIntervalMs'),
])
def test_health_relations_match_the_data_plane(health, field):
    d = base()
    members = [{'serverId': m['serverId']} for m in d['members']]
    with pytest.raises(model.MeshInvalid) as raised:
        model.clean_doc({'settings': {'health': health}, 'members': members}, d)
    assert {(e['path'], e['error']) for e in raised.value.errors} == {(f'settings.health.{field}', 'invalid_health')}
    model.clean_doc({'settings': {'health': {'readySuccesses': 4, 'activeIntervalMs': 2500}}, 'members': members}, d)


def test_new_status_fields_are_cleaned_and_default_for_an_older_data_plane():
    status = mesh_status.clean_status({
        'resources': {'rssBytes': 10, 'cpuPercent': 12.345}, 'hostFirewall': 'on',
        'peers': [{'id': 'r1', 'kind': 'entry', 'active': True, 'reconnects': 4}, {'id': 'r2', 'reconnects': 'x'}],
        'skippedRoutes': ['10.0.0.0/24', '10.0.0.0/24', '192.168.1.7/24', 'fe80::/64', 'nonsense', 7, None,
                          *[f'172.16.{i}.0/24' for i in range(70)]]})
    assert status['resources']['cpuPercent'] == 12.3 and isinstance(status['resources']['cpuPercent'], float)
    assert [p['reconnects'] for p in status['peers']] == [4, 0] and status['hostFirewall'] == 'on'
    # At most 64 items are read; only valid IPv4 networks stay, normalized and once.
    assert status['skippedRoutes'][:2] == ['10.0.0.0/24', '192.168.1.0/24'] and len(status['skippedRoutes']) == 2 + 57
    assert all(r.startswith(('10.', '192.', '172.16.')) for r in status['skippedRoutes'])
    old = mesh_status.clean_status({'resources': {'rssBytes': 10}, 'peers': [{'id': 'r1'}], 'hostFirewall': 'yes'})
    assert (old['resources']['cpuPercent'], old['peers'][0]['reconnects'], old['skippedRoutes'], old['hostFirewall']) == (0.0, 0, [], '')
    assert old['hostFirewallIssues'] == []
    assert mesh_status.clean_status(status) == status  # cleaning a saved status again keeps it
    # Linux: the host firewalls found and what they may not let through; unknown kinds and codes dropped.
    linux = mesh_status.clean_status({'hostFirewall': 'ufw,docker,bogus,docker', 'hostFirewallIssues': [
        {'code': 'firewalld_zone', 'detail': 'work'}, {'code': 'nftables_failed', 'detail': 'x' * 400}, {'code': 'made_up'}, 'junk',
        {'code': 'firewall_tool_missing', 'detail': 'firewalld: firewall-cmd'}]})
    assert linux['hostFirewall'] == 'ufw,docker'
    assert linux['hostFirewallIssues'] == [{'code': 'firewalld_zone', 'detail': 'work'}, {'code': 'nftables_failed', 'detail': 'x' * 300},
                                           {'code': 'firewall_tool_missing', 'detail': 'firewalld: firewall-cmd'}]
    assert mesh_status.clean_status(linux) == linux


def test_end_to_end_and_signature_status_is_cleaned():
    status = mesh_status.clean_status({
        'counters': {'droppedPlain': 3},
        'e2e': {'sessions': 7, 'limit': 64, 'innerMtu': 1348, 'created': 9, 'evicted': 2, 'sent': 100, 'received': 90, 'droppedExhausted': 4,
                'droppedQueue': 1, 'droppedSource': 'x', 'droppedBad': -1, 'peers': [{'id': 'm2'}]},
        'security': {'signing': {'verified': 120, 'rejected': 5, 'byHop': {'r1': 5, 'r2': 0, 'r3': 'x', **{f'h{i}': 1 for i in range(80)}}},
                     'claims': {'expired': 2}, 'mode': 'enforce'}})
    assert status['counters']['droppedPlain'] == 3
    assert status['e2e'] == {'sessions': 7, 'limit': 64, 'innerMtu': 1348, 'created': 9, 'evicted': 2, 'sent': 100, 'received': 90,
                             'droppedExhausted': 4, 'droppedQueue': 1, 'droppedSource': 0, 'droppedBad': 0}
    # Counts of zero or not numbers are left out; at most 64 peers are read.
    by_hop = status['security']['signing']['byHop']
    assert by_hop['r1'] == 5 and 'r2' not in by_hop and 'r3' not in by_hop and len(by_hop) == 64 - 3 + 1
    assert status['security']['signing']['verified'] == 120 and status['security']['claims'] == {'expired': 2} and 'mode' not in status['security']
    assert mesh_status.clean_status(status) == status
    old = mesh_status.clean_status({'e2e': 'x', 'security': []})
    assert old['e2e'] is None and old['security'] is None and old['counters']['droppedPlain'] == 0


def test_a_dialed_paths_redial_state_is_kept():
    redial = {'baselineMs': 74.04, 'level': 2, 'redials': 3, 'lastAt': 1793205000000, 'nextAt': 1793206200000, 'degradedMinutes': 1, 'extra': 'x'}
    status = mesh_status.clean_status({
        'peers': [{'id': 'S', 'kind': 'relay', 'redial': redial}, {'id': 'V', 'kind': 'relay'}],
        'entry': {'candidates': [{'id': 'c2', 'relay': 'S', 'transport': 'tcp', 'state': 'ready', 'redial': {**redial, 'baselineMs': None}}]}})
    kept = {k: v for k, v in redial.items() if k != 'extra'}
    assert status['peers'][0]['redial'] == {**kept, 'baselineMs': 74.0} and 'redial' not in status['peers'][1]
    assert status['entry']['candidates'][0]['redial'] == {**kept, 'baselineMs': None}
    assert mesh_status.clean_status(status) == status


@pytest.mark.parametrize('role, rss, level', [('member', 127 * 1024 ** 2, ''), ('member', 128 * 1024 ** 2, 'near'),
                                              ('member', 160 * 1024 ** 2, 'over'), ('relay', 160 * 1024 ** 2, ''),
                                              ('relay', 205 * 1024 ** 2, 'near'), ('relay', 300 * 1024 ** 2, 'over')])
def test_memory_level_against_the_role_limit(role, rss, level):
    memory = mesh_status.summary(mesh_status.clean_status({'resources': {'rssBytes': rss}}), role)['memory']
    assert memory == {'rssBytes': rss, 'limitBytes': (256 if role == 'relay' else 160) * 1024 ** 2, 'level': level}
    assert mesh_status.summary(None, role)['memory']['level'] == ''


# ----- punched UDP direct links -----
def punch_doc():
    return doc(
        member('r1', 'relay', publicEndpoint='berlin.example.net'),
        member('r2', 'relay', listen={'udp': 0, 'tcp': 52000}),
        member('r3', 'relay', listen={'udp': 51990, 'tcp': 52000}),
        member('m1', entry={'mode': 'failover', 'autoReturn': True, 'candidates': [
            {'id': 'c1', 'relay': 'r3', 'transport': 'udp'}, {'id': 'c2', 'relay': 'r2', 'transport': 'tcp'},
            {'id': 'c3', 'relay': 'r3', 'transport': 'tcp'}]}),
        member('m2', listen={'tcp': 52000}, entry={'mode': 'fixed', 'autoReturn': True, 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}]}),
        member('gw'),
        direct=[{'a': 'm1', 'b': 'm2', 'transport': 'udp', 'dialer': 'b'}, {'a': 'gw', 'b': 'm2', 'transport': 'tcp', 'dialer': 'a'}])


def test_udp_direct_links_are_punched_from_both_ends():
    f = facts(ipv4={'m1': ''})  # m1 is behind NAT with no address of its own
    d = punch_doc()
    assert errors(d, f) == set()
    configs = build(d, f, 'net', 1)
    # The panel's IPv4 of m2 is no endpoint for punching (behind a proxy it is the proxy's); the dialer is kept.
    m1_link = {'peer': 'm2', 'kind': 'direct', 'dial': False, 'punch': True}
    assert configs['m1']['links'] == [m1_link]
    assert configs['m2']['links'] == [{'peer': 'gw', 'kind': 'direct', 'dial': False},
                                      {'peer': 'm1', 'kind': 'direct', 'dial': True, 'punch': True}]
    # Its candidates' relays first, once each and only those with UDP and an address, then the others by id.
    assert configs['m1']['reflectors'] == ['203.0.113.3:51990', 'berlin.example.net:51999']
    assert configs['m2']['reflectors'] == ['berlin.example.net:51999', '203.0.113.3:51990']
    # TCP direct links are unchanged.
    assert configs['gw']['links'] == [{'peer': 'm2', 'kind': 'direct', 'dial': True, 'endpoint': 'tcp://203.0.113.5:52000'}]
    # Every node has reflectors now, never itself: by id without entry relays, linked relays first for a relay.
    assert configs['gw']['reflectors'] == ['berlin.example.net:51999', '203.0.113.3:51990']
    assert configs['r1']['reflectors'] == ['203.0.113.3:51990'] and configs['r3']['reflectors'] == ['berlin.example.net:51999']
    # An endpoint only from what the user entered or a public IP the node's own check agreed on, at its UDP port.
    known = copy.deepcopy(d)
    known['members'][4]['publicEndpoint'] = 'm2.example.net'
    trusted = facts(ipv4={'m1': '192.0.2.42'})
    trusted['m1']['publicIp'] = '198.51.100.7'
    both = build(known, trusted, 'net', 1)
    assert both['m1']['links'] == [{**m1_link, 'endpoint': 'udp://m2.example.net:51999'}]
    assert both['m2']['links'][1] == {'peer': 'm1', 'kind': 'direct', 'dial': True, 'punch': True, 'endpoint': 'udp://198.51.100.7:51999'}
    assert '192.0.2.42' not in json.dumps(both)
    # A relay's new address is a new config for the punching members.
    moved = build(d, facts(ipv4={'m1': '', 'r3': '198.51.100.9'}), 'net', 1)
    assert moved['m1']['reflectors'] == ['198.51.100.9:51990', 'berlin.example.net:51999']
    assert config_hash(moved['m1']) != config_hash(configs['m1']) and moved['gw']['reflectors'] == ['berlin.example.net:51999', '198.51.100.9:51990']


def test_a_punched_link_needs_no_address_when_a_relay_can_reflect():
    d = base()
    d['directLinks'].append({'a': 'm1', 'b': 'm2', 'transport': 'udp', 'dialer': 'b'})
    assert errors(d, facts(ipv4={'m1': '', 'm2': ''})) == set()
    assert build(d, facts(ipv4={'m1': '', 'm2': ''}), 'net', 1)['m1']['links'] == [{'peer': 'm2', 'kind': 'direct', 'dial': False, 'punch': True}]


def test_direct_state_of_a_peer_is_cleaned():
    def peer(direct):
        return mesh_status.clean_status({'peers': [{'id': 'm2', 'kind': 'direct', 'direct': direct}]})['peers'][0]
    good = {'state': 'punching', 'via': 'public', 'since': 1793001600000, 'attempts': 3, 'lastError': 'x' * 80, 'extra': 1}
    assert peer(good)['direct'] == {'state': 'punching', 'via': 'public', 'since': 1793001600000, 'attempts': 3, 'lastError': 'x' * 64}
    assert peer({'state': 'direct', 'via': 'wan', 'since': -5, 'attempts': '2', 'lastError': 7})['direct'] == {
        'state': 'direct', 'via': '', 'since': 0, 'attempts': 0, 'lastError': ''}
    assert peer({'state': 'relayed'})['direct'] == {'state': 'relayed', 'via': '', 'since': 0, 'attempts': 0, 'lastError': ''}
    for bad in (None, 'direct', {'state': 'up'}, {'via': 'lan'}):
        assert 'direct' not in peer(bad)
    status = mesh_status.clean_status({'peers': [{'id': 'm2', 'direct': good}]})
    assert mesh_status.clean_status(status) == status
    events = mesh_status.clean_events([{'seq': 1, 'at': 2, 'kind': k, 'detail': 'm2'} for k in ('direct.up', 'direct.down', 'direct.failed')])
    assert [e['kind'] for e in events] == ['direct.up', 'direct.down', 'direct.failed']


# ----- site relays (behind NAT), reachability and measured routing -----
# The panel has the proxy's address for r3 (TOY) and gw (home); neither may ever be an endpoint.
SITE_IPV4 = {'r3': '192.0.2.42', 'gw': '192.0.2.117', 'm1': '192.0.2.42', 'm2': '192.0.2.42'}


def site_doc(**links):
    """r1 (SG) and r2 (LAX) public, gw (home) and r3 (TOY) site relays; m1, m2 enter by TOY, then SG."""
    pairs = links.get('pairs', [pair('gw', 'r1', dialer='b'), pair('gw', 'r2', transport='tcp'), pair('r3', 'r1'),
                                 {**pair('gw', 'r3', dialer='b'), 'bias': -5}, {**pair('r1', 'r2'), 'bias': 20}])
    return doc(
        member('r1', 'relay', listen={'tcp': 52000}),
        member('r2', 'relay', listen={'tcp': 52000}),
        member('r3', 'relay', relayMode='site'),
        member('gw', 'relay', relayMode='site'),
        member('m1', entry={'mode': 'failover', 'autoReturn': True, 'candidates': [
            {'id': 'c1', 'relay': 'r3', 'transport': 'udp'}, {'id': 'c2', 'relay': 'r1', 'transport': 'tcp'}]}),
        member('m2', entry={'mode': 'failover', 'autoReturn': True, 'candidates': [
            {'id': 'c1', 'relay': 'r3', 'transport': 'udp'}, {'id': 'c2', 'relay': 'r1', 'transport': 'udp'}]}),
        relay_links={'mode': 'custom', 'pairs': pairs})


def test_site_relays_punch_each_other_dial_public_relays_and_are_entered_by_punching():
    f = facts(ipv4=SITE_IPV4)
    d = site_doc()
    assert errors(d, f) == set()
    configs = build(d, f, 'net', 1)
    # Site to site is punched without endpoints; site to public is dialed by the site relay, whatever the pair says.
    assert configs['gw']['links'] == [
        {'peer': 'r1', 'kind': 'relay', 'dial': True, 'endpoint': 'udp://203.0.113.1:51999'},
        {'peer': 'r2', 'kind': 'relay', 'dial': True, 'endpoint': 'tcp://203.0.113.2:52000'},
        {'peer': 'r3', 'kind': 'relay', 'dial': False, 'punch': True}]
    assert configs['r3']['links'] == [{'peer': 'gw', 'kind': 'relay', 'dial': True, 'punch': True},
                                      {'peer': 'r1', 'kind': 'relay', 'dial': True, 'endpoint': 'udp://203.0.113.1:51999'}]
    assert configs['r1']['links'] == [{'peer': 'gw', 'kind': 'relay', 'dial': False}, {'peer': 'r2', 'kind': 'relay', 'dial': True,
                                                                                         'endpoint': 'udp://203.0.113.2:51999'},
                                      {'peer': 'r3', 'kind': 'relay', 'dial': False}]
    # A candidate on a site relay is punched and has no endpoint.
    assert configs['m1']['entry']['candidates'] == [{'id': 'c1', 'relay': 'r3', 'punch': True},
                                                    {'id': 'c2', 'relay': 'r1', 'endpoint': 'tcp://203.0.113.1:52000'}]
    # Reflectors: public relays with UDP only, never the node itself; entry or linked relays first.
    assert configs['m1']['reflectors'] == ['203.0.113.1:51999', '203.0.113.2:51999']
    assert configs['r2']['reflectors'] == ['203.0.113.1:51999'] and configs['gw']['reflectors'] == ['203.0.113.1:51999', '203.0.113.2:51999']
    # Each end of a link has its bias; nextHops are still worked out as the start and the static mode.
    assert configs['gw']['routing'] == {'mode': 'dynamic', 'bias': {'r1': 0, 'r2': 0, 'r3': -5}}
    assert configs['r3']['routing'] == {'mode': 'dynamic', 'bias': {'gw': -5, 'r1': 0}}
    assert configs['r1']['routing'] == {'mode': 'dynamic', 'bias': {'gw': 0, 'r2': 20, 'r3': 0}}
    assert configs['gw']['nextHops'] == {'r1': 'r1', 'r2': 'r2', 'r3': 'r3'} and 'routing' not in configs['m1']
    assert all(c['reachability'] == {'stun': []} for c in configs.values())
    # The panel's addresses of the machines behind NAT appear nowhere.
    text = json.dumps(configs)
    assert '192.0.2.42' not in text and '192.0.2.117' not in text
    # The same machine as a public relay is dialed at what it has.
    public = site_doc()
    public['members'][2].update(relayMode='public', publicEndpoint='toy.example.net')
    assert build(public, f, 'net', 1)['m1']['entry']['candidates'][0] == {'id': 'c1', 'relay': 'r3', 'endpoint': 'udp://toy.example.net:51999'}
    # Full mode: the default link between the two site relays is punched, between site and public dialed by the site relay.
    full = site_doc(pairs=[])
    full['relayLinks']['mode'] = 'full'
    links = {link['peer']: link for link in build(full, f, 'net', 1)['gw']['links']}
    assert links['r3'] == {'peer': 'r3', 'kind': 'relay', 'dial': True, 'punch': True} and links['r1']['dial'] and links['r2']['dial']
    assert errors(full, f) == set()


def test_stun_servers_are_a_mesh_setting_sent_to_every_node():
    d = site_doc()
    data = {'settings': {'reachability': {'stun': ['stun.miwifi.com:3478', ' 1.2.3.4:3478', '[2001:db8::1]:3478', 'stun.miwifi.com:3478']}}}
    cleaned = model.clean_doc(data, d)
    assert cleaned['settings']['reachability'] == {'stun': ['stun.miwifi.com:3478', '1.2.3.4:3478', '[2001:db8::1]:3478']}
    configs = build(cleaned, facts(ipv4=SITE_IPV4), 'net', 1)
    assert all(c['reachability']['stun'] == cleaned['settings']['reachability']['stun'] for c in configs.values())
    for bad in (['stun.example.com'], ['a:0'], ['bad host:3478'], 'x', [f's{i}.example.com:3478' for i in range(9)]):
        with pytest.raises(model.MeshInvalid) as exc:
            model.clean_doc({'settings': {'reachability': {'stun': bad}}}, d)
        assert {e['error'] for e in exc.value.errors} == {'invalid_stun'}
    with pytest.raises(model.MeshInvalid) as exc:
        model.clean_doc({'members': [{'serverId': m['serverId'], **({'relayMode': 'nat'} if m['serverId'] == 'r3' else {})} for m in d['members']]}, d)
    assert [(e['path'], e['error']) for e in exc.value.errors] == [('members[2].relayMode', 'invalid_relay_mode')]
    with pytest.raises(model.MeshInvalid) as exc:
        model.clean_doc({'relayLinks': {'mode': 'custom', 'pairs': [{**pair('r1', 'r2'), 'bias': 1.5}]}}, d)
    assert [(e['path'], e['error']) for e in exc.value.errors] == [('relayLinks.pairs[0].bias', 'invalid_bias')]
    kept = model.clean_doc({'relayLinks': {'mode': 'custom', 'pairs': [{**pair('r1', 'r2'), 'bias': -30}]}}, d)
    assert kept['relayLinks']['pairs'] == [{'a': 'r1', 'b': 'r2', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': -30}]
    # A document stored before these fields reads as their defaults.
    old = copy.deepcopy(site_doc())
    del old['settings']['reachability']
    for m in old['members']:
        del m['relayMode']
    for p in old['relayLinks']['pairs']:
        p.pop('bias', None)
    assert model.upgrade(old)['settings']['reachability'] == {'stun': []} and {m['relayMode'] for m in old['members']} == {'public'}
    assert {p['bias'] for p in old['relayLinks']['pairs']} == {0}


@pytest.mark.parametrize('change, expected', [
    # Only the two site relays linked to each other: neither reaches a public relay.
    (lambda d: d['relayLinks'].update(pairs=[pair('gw', 'r3'), pair('r1', 'r2')]),
     {('members[2].relayMode', 'mesh_site_relay_needs_public_link'), ('members[3].relayMode', 'mesh_site_relay_needs_public_link'),
      ('relayLinks', 'relay_unreachable')}),
    (lambda d: d['members'][4]['entry'].update(candidates=[{'id': 'c1', 'relay': 'r3', 'transport': 'udp'}]),
     {('members[4].entry.candidates', 'mesh_entry_needs_bootstrap')}),
    (lambda d: d['members'][4]['entry']['candidates'][0].update(transport='tcp'), {('members[4].entry.candidates[0]', 'punch_needs_udp')}),
    (lambda d: d['relayLinks']['pairs'][3].update(transport='tcp'), {('relayLinks.pairs[3]', 'punch_needs_udp')}),
    (lambda d: d['members'][2]['listen'].update(udp=0, tcp=52000),
     {('relayLinks.pairs[3]', 'udp_not_listening'), ('members[4].entry.candidates[0]', 'transport_not_listening'),
      ('members[5].entry.candidates[0]', 'transport_not_listening')}),
    # The site relay dials whatever the pair says, so it is the public relay that must listen.
    (lambda d: d['relayLinks'].update(mode='custom', pairs=[pair('r1', 'r3', transport='tls'), *d['relayLinks']['pairs'][:2],
                                                            {**pair('gw', 'r3'), 'bias': 0}, {**pair('r1', 'r2'), 'bias': 0}]),
     {('relayLinks.pairs[0]', 'transport_not_listening')}),
])
def test_site_relay_checks(change, expected):
    d = site_doc()
    change(d)
    assert errors(d, facts(ipv4=SITE_IPV4)) == expected


def test_punching_between_two_symmetric_nats_is_only_a_warning():
    d = site_doc()
    d['directLinks'] = [{'a': 'm1', 'b': 'm2', 'transport': 'udp', 'dialer': 'a'}]
    f = facts(ipv4=SITE_IPV4)
    assert model.hints(d, f) == []
    for sid in ('m1', 'm2', 'r3', 'gw'):
        f[sid]['verdict'] = 'symmetric'
    assert errors(d, f) == set()
    hints = model.hints(d, f)
    assert {(h['path'], h['error']) for h in hints} == {
        ('directLinks[0]', 'mesh_punch_impossible'), ('relayLinks.pairs[3]', 'mesh_punch_impossible'),
        ('members[4].entry.candidates[0]', 'mesh_punch_impossible'), ('members[5].entry.candidates[0]', 'mesh_punch_impossible')}
    assert hints[0]['message'] == 'M1 和 M2 都在对称型 NAT 后面，打洞基本不会成功，这条直连会一直经中转'
    f['m2']['verdict'] = 'punchable'
    assert {h['path'] for h in model.hints(d, f)} == {'relayLinks.pairs[3]', 'members[4].entry.candidates[0]'}


def test_symmetric_nats_on_one_lan_are_not_warned():
    # Office machines behind one symmetric NAT reach each other and their site relay over
    # the LAN; only a public IP known to differ means the same numbers on two networks.
    d = site_doc()
    d['directLinks'] = [{'a': 'm1', 'b': 'm2', 'transport': 'udp', 'dialer': 'a'}]
    f = facts(ipv4=SITE_IPV4)
    for sid in ('m1', 'm2', 'r3', 'gw'):
        f[sid]['verdict'] = 'symmetric'
    for sid in ('m1', 'm2', 'r3'):
        f[sid]['localPrefixes'] = ['10.20.20.0/24']
        f[sid]['natIp'] = '192.0.2.34'
    f['gw']['localPrefixes'] = ['10.10.10.0/24']

    def punched():
        return {h['path'] for h in model.hints(d, f) if h['error'] == 'mesh_punch_impossible'}
    assert punched() == {'relayLinks.pairs[3]'}
    f['m2']['natIp'] = '203.0.113.9'
    assert punched() == {'relayLinks.pairs[3]', 'directLinks[0]', 'members[5].entry.candidates[0]'}


def shared(d, f):
    return [(h['path'], h['message']) for h in model.hints(d, f) if h['error'] == 'mesh_udp_port_shared']


def test_same_nat_by_public_ip_or_overlapping_private_networks():
    f = facts()
    assert not model.same_nat('m1', 'm2', f)
    # Seen at one public IP by their reachability checks (any verdict), whatever their networks.
    f['m1']['natIp'], f['m2']['natIp'] = '192.0.2.117', '192.0.2.117'
    assert model.same_nat('m1', 'm2', f) and model.same_nat('m2', 'm1', f)
    f['m2']['natIp'] = '42.243.99.118'
    assert not model.same_nat('m1', 'm2', f)
    f['m1']['natIp'] = f['m2']['natIp'] = ''
    assert not model.same_nat('m1', 'm2', f)
    # Overlapping private networks; public, CGNAT or link-local ones and broken entries do not count.
    f['m1']['localPrefixes'], f['m2']['localPrefixes'] = ['10.20.20.0/24', '100.64.0.0/24'], ['10.20.0.0/16']
    assert model.same_nat('m1', 'm2', f)
    f['m2']['localPrefixes'] = ['10.20.21.0/24', '100.64.0.0/24', '169.254.0.0/16', '203.0.113.0/24', 'bad', 7]
    f['m1']['localPrefixes'] += ['169.254.0.0/16', '203.0.113.0/24']
    assert not model.same_nat('m1', 'm2', f)
    f['m2']['localPrefixes'].append('192.168.1.0/24')
    f['m1']['localPrefixes'].append('192.168.1.7/24')
    assert model.same_nat('m1', 'm2', f)
    # The default docker0 / podman bridges say nothing: a common part only inside them does not count.
    f['m1']['localPrefixes'], f['m2']['localPrefixes'] = ['172.17.0.0/16', '10.88.0.0/16'], ['172.17.0.0/16', '10.88.0.0/16']
    assert not model.same_nat('m1', 'm2', f)
    f['m2']['localPrefixes'] = ['172.16.0.0/12', '10.0.0.0/8', '172.17.5.0/24']
    assert not model.same_nat('m1', 'm2', f)
    f['m1']['localPrefixes'].append('10.20.20.0/24')
    assert model.same_nat('m1', 'm2', f)
    f['m1']['localPrefixes'] = ['172.17.0.0/16', '10.20.20.0/24']
    f['m2']['localPrefixes'] = ['172.17.0.0/16', '10.20.20.0/24']
    assert model.same_nat('m1', 'm2', f)
    f['m1']['localPrefixes'], f['m2']['localPrefixes'] = ['172.18.0.0/16'], ['172.16.0.0/12']
    assert model.same_nat('m1', 'm2', f)
    # A public IP seen by both still decides, bridges or not.
    f['m1']['localPrefixes'] = f['m2']['localPrefixes'] = ['172.17.0.0/16']
    f['m1']['natIp'] = f['m2']['natIp'] = '192.0.2.117'
    assert model.same_nat('m1', 'm2', f)
    status = {'reachability': {'verdict': 'no_udp', 'publicAddr': '192.0.2.117:1026', 'consistent': False}}
    assert mesh_status.nat_ip(status) == '192.0.2.117' and mesh_status.public_ip(status) == ''
    assert mesh_status.nat_ip({'reachability': {'publicAddr': ''}}) == mesh_status.nat_ip(None) == ''


def test_members_behind_one_nat_on_one_udp_port_get_a_warning_only():
    d = doc(member('r1', 'relay', listen={'tcp': 52000}), member('m1'), member('m2'), member('gw'))
    f = facts()
    assert shared(d, f) == []
    for sid in ('m1', 'm2', 'gw'):
        f[sid]['natIp'] = '192.0.2.117'
    assert errors(d, f) == set()
    assert shared(d, f) == [
        ('members[2].listen.udp', 'M1 和 M2 在同一个 NAT 后，都用 UDP 51999。部分路由器（尤其带代理的旁路由）会因此丢包，建议把 M2 改成 51998。'),
        ('members[3].listen.udp', 'M1 和 GW 在同一个 NAT 后，都用 UDP 51999。部分路由器（尤其带代理的旁路由）会因此丢包，建议把 GW 改成 51997。')]
    # Following the suggestions clears them; a port taken by another member there is skipped.
    d['members'][2]['listen']['udp'], d['members'][3]['listen']['udp'] = 51998, 51997
    assert shared(d, f) == []
    d['members'][3]['listen']['udp'] = 51998
    assert shared(d, f) == [('members[3].listen.udp', 'M2 和 GW 在同一个 NAT 后，都用 UDP 51998。部分路由器（尤其带代理的旁路由）会因此丢包，'
                                                       '建议把 GW 改成 51997。')]
    # Different NATs, UDP off, or a member on its way out: no warning.
    d['members'][3]['listen']['udp'] = 0
    assert shared(d, f) == []
    d['members'][3]['listen']['udp'], d['members'][3]['removing'] = 51998, True
    assert shared(d, f) == []
    d['members'][3]['removing'] = False
    f['gw']['natIp'] = '198.51.100.9'
    assert shared(d, f) == []


def test_public_relays_take_no_part_in_the_shared_nat_check():
    d = doc(member('r1', 'relay'), member('r2', 'relay', relayMode='site'), member('m1'))
    f = facts()
    for sid in ('r1', 'r2', 'm1'):
        f[sid]['localPrefixes'] = ['10.20.20.0/24']
    # r1 is a public relay: dialed at its own address, so sharing its port is fine; the site relay is not.
    assert [p for p, _ in shared(d, f)] == ['members[2].listen.udp']
    assert '和 M1 在同一个 NAT 后' in shared(d, f)[0][1] and shared(d, f)[0][1].startswith('R2 ')
    assert model.nat_udp_ports(d, d['members'][2], f) == {51999}
    d['members'][1]['relayMode'] = 'public'
    assert shared(d, f) == [] and model.nat_udp_ports(d, d['members'][2], f) == set()


def test_a_new_member_takes_the_first_udp_port_free_behind_its_nat():
    settings = model.default_settings()
    m = model.new_member('m9', settings)
    assert model.free_udp_port(m, set(), settings) == 51999
    assert model.free_udp_port(m, {51999, 51998, 7}, settings) == 51997
    m['listen']['tcp'] = 51997
    assert model.free_udp_port(m, {51999, 51998}, settings) == 51996
    settings['ports']['udp'] = 2
    assert model.free_udp_port(model.new_member('m9', settings), {1, 2}, settings) == 3


def test_reachability_entry_health_and_routing_are_cleaned_and_summarised():
    raw = {
        'reachability': {'checkedAt': 1793000000000, 'verdict': 'punchable', 'publicAddr': '192.0.2.34:51999', 'consistent': True,
                         'mapping': 'stable', 'filtering': 'port', 'extra': 1,
                         'observers': [{'id': 'r1', 'kind': 'relay', 'addr': '192.0.2.34:51999', 'ok': True},
                                       {'id': 'stun.miwifi.com:3478', 'kind': 'stun', 'addr': '', 'ok': 'yes'}, 'bad']},
        'entry': {'candidate': 'c1', 'relay': 'r3', 'transport': 'udp', 'since': 1, 'switches': 0, 'candidates': [
            {'id': 'c1', 'relay': 'r3', 'transport': 'udp', 'state': 'active', 'since': 5, 'lastOkAt': 9, 'lastError': '', 'via': 'lan',
             'rttMs': 12.34, 'timeoutRate': 0.01, 'role': 'active'},
            {'id': 'c2', 'relay': 'r2', 'transport': 'tcp', 'state': 'failing', 'since': -1, 'lastOkAt': 'x', 'lastError': 'timeout' * 20,
             'via': 'wifi', 'rttMs': 0, 'lossRate': 2, 'role': 'hot'},
            {'id': 'c3', 'relay': 'r1', 'state': 'broken'}]},
        'routing': {'mode': 'dynamic',
                    'links': [{'peer': 'gw', 'state': 'up', 'rttMs': 2.54, 'jitterMs': 0.2, 'loss': 0.0, 'cost': -2.1, 'penalty': 0},
                              {'peer': 'r1', 'state': 'down', 'rttMs': None, 'jitterMs': -1, 'loss': 3, 'cost': None, 'penalty': 1000},
                              {'peer': 'r2', 'state': 'flapping'}],
                    'routes': [{'dest': 'gw', 'nextHop': 'gw', 'cost': 2.9, 'path': ['r3', 'gw'], 'since': 1793000000000, 'switches': 1},
                               {'dest': 'r1', 'nextHop': 'gw', 'cost': 'inf', 'path': 'r3', 'since': 0, 'switches': 0}]},
        'peers': [{'id': 'gw', 'kind': 'relay', 'active': True, 'rttMs': 2.5,
                   'direct': {'state': 'direct', 'via': 'public', 'since': 3, 'attempts': 2, 'lastError': ''}}],
    }
    status = mesh_status.clean_status(raw)
    assert status['reachability'] == {
        'checkedAt': 1793000000000, 'verdict': 'punchable', 'publicAddr': '192.0.2.34:51999', 'consistent': True, 'mapping': 'stable',
        'filtering': 'port', 'observers': [{'id': 'r1', 'kind': 'relay', 'addr': '192.0.2.34:51999', 'ok': True},
                                           {'id': 'stun.miwifi.com:3478', 'kind': 'stun', 'addr': '', 'ok': False}]}
    # via says how a candidate got through (lan, public, configured), '' when unknown.
    # rttMs and timeoutRate as the node measured a candidate (0 or out of range: not measured), role when it says.
    assert status['entry']['candidates'] == [
        {'id': 'c1', 'relay': 'r3', 'transport': 'udp', 'state': 'active', 'since': 5, 'lastOkAt': 9, 'lastError': '', 'via': 'lan',
         'rttMs': 12.3, 'timeoutRate': 0.01, 'role': 'active'},
        {'id': 'c2', 'relay': 'r2', 'transport': 'tcp', 'state': 'failing', 'since': 0, 'lastOkAt': 0, 'lastError': ('timeout' * 20)[:64], 'via': '',
         'rttMs': None, 'timeoutRate': None, 'role': ''}]
    assert status['routing'] == {'mode': 'dynamic', 'links': [
        {'peer': 'gw', 'state': 'up', 'rttMs': 2.5, 'jitterMs': 0.2, 'loss': 0.0, 'cost': -2.1, 'penalty': 0},
        {'peer': 'r1', 'state': 'down', 'rttMs': None, 'jitterMs': 0.0, 'loss': 0.0, 'cost': None, 'penalty': 1000}], 'routes': [
        {'dest': 'gw', 'nextHop': 'gw', 'cost': 2.9, 'path': ['r3', 'gw'], 'since': 1793000000000, 'switches': 1},
        {'dest': 'r1', 'nextHop': 'gw', 'cost': None, 'path': [], 'since': 0, 'switches': 0}]}
    # A punched relay link's state comes through like a direct link's.
    assert status['peers'][0]['direct'] == {'state': 'direct', 'via': 'public', 'since': 3, 'attempts': 2, 'lastError': ''}
    assert mesh_status.clean_status(status) == status
    # An old data plane has none of these.
    old = mesh_status.clean_status({'entry': {'relay': 'r1'}, 'reachability': 'x', 'routing': []})
    assert old['reachability'] is None and old['routing'] is None and old['entry']['candidates'] == []

    relay = mesh_status.summary(status, 'relay')
    assert relay['reachability'] == {'verdict': 'punchable', 'publicAddr': '192.0.2.34:51999', 'consistent': True, 'filtering': 'port',
                                     'checkedAt': 1793000000000}
    assert relay['routing'] == status['routing'] and relay['entryHealth'] == [] and relay['direct'] == {'gw': {'state': 'direct', 'via': 'public', 'rttMs': 2.5}}
    member_summary = mesh_status.summary(status, 'member')
    assert member_summary['entryHealth'] == status['entry']['candidates'] and member_summary['routing'] is None
    assert [c['via'] for c in member_summary['entryHealth']] == ['lan', '']
    assert 'candidates' not in member_summary['entry'] and member_summary['entry']['relay'] == 'r3'
    empty = mesh_status.summary(None, 'relay')
    assert (empty['reachability'], empty['entryHealth'], empty['routing']) == (None, [], None)


def test_relay_peers_count_by_a_usable_session_not_by_being_a_next_hop():
    from app.servers.mesh_trend import measured
    status = mesh_status.clean_status({'peers': [
        # Forwarding for others without being the next hop for anything here: in use.
        {'id': 'r2', 'kind': 'relay', 'active': True, 'routed': False, 'rttMs': 40},
        {'id': 'r3', 'kind': 'relay', 'active': True, 'routed': True, 'rttMs': 20},
        {'id': 'r4', 'kind': 'relay', 'active': False, 'routed': True, 'rttMs': 90},
        {'id': 'm1', 'kind': 'member', 'active': True, 'rttMs': None}]})
    assert [(p['id'], p['active'], p['routed']) for p in status['peers']] == [
        ('r2', True, False), ('r3', True, True), ('r4', False, True), ('m1', True, False)]
    assert mesh_status.summary(status, 'relay')['peers'] == 3
    # A relay's trend is its links to other relays: it does not measure the members it carries.
    assert [p['id'] for p in measured(status, 'relay')] == ['r2', 'r3']


def test_alerts_for_failing_standby_candidates_and_a_rewritten_exit():
    watch, now = mesh_status.Watch(), 1_793_000_000_000
    names = {'r2': 'LAX', 'r1': 'SG'}.get

    def report(states, consistent=True):
        return mesh_status.clean_status({
            'entry': {'relay': 'r3', 'candidates': [{'id': f'c{i}', 'relay': r, 'state': s, 'lastError': e} for i, (r, s, e) in enumerate(states)]},
            'peers': [{'id': 'r3', 'kind': 'entry', 'active': True}],
            'reachability': {'verdict': 'unknown', 'consistent': consistent}})

    def kinds(status, at):
        return [key.split(':')[0] for key, _, _ in watch.observe(at, 'm1', 'Mac', 'member', True, status, [], names)]
    failing = report([('r3', 'active', ''), ('r2', 'failing', 'timeout'), ('r1', 'failing', 'punch timeout')])
    assert kinds(failing, now) == []
    alerts = watch.observe(now + 60_000, 'm1', 'Mac', 'member', True, failing, [], names)
    assert [(k.split(':')[0], text) for k, _, text in alerts] == [
        ('mesh_standby_unhealthy', '组网：「Mac」的备用入口都连不上（「LAX」备用：超时；「SG」备用：打洞超时），当前入口断开时无法切换。')]
    assert kinds(failing, now + 120_000) == []
    # One standby recovering ends the episode; a later one alerts again.
    assert kinds(report([('r3', 'active', ''), ('r2', 'ready', ''), ('r1', 'failing', '')]), now + 130_000) == []
    # Within ALERT_COOLDOWN_MS of the last one it waits; it goes out then if the episode still lasts.
    assert kinds(failing, now + 140_000) == [] and kinds(failing, now + 200_000) == []
    later = now + 60_000 + mesh_status.ALERT_COOLDOWN_MS
    assert kinds(failing, later - 1) == [] and kinds(failing, later) == ['mesh_standby_unhealthy']
    # No standby at all, or a relay, is not this alert.
    assert kinds(report([('r3', 'active', '')]), now + 300_000) == []
    assert watch.observe(now + 400_000, 'r3', 'TOY', 'relay', False, failing, [], names) == []
    # An error the node words itself is quoted like a name: Telegram gets it as code, not as a link.
    worded = report([('r3', 'active', ''), ('r2', 'failing', 'see https://evil.example/x'), ('r1', 'failing', '')])
    worded_watch = mesh_status.Watch()
    assert worded_watch.observe(now, 'm1', 'Mac', 'member', True, worded, [], names) == []
    text = worded_watch.observe(now + 60_000, 'm1', 'Mac', 'member', True, worded, [], names)[0][2]
    assert '「LAX」备用：「see https://evil.example/x」；「SG」备用：失败' in text
    assert '「<code>see https://evil.example/x</code>」' in telegram_html(text)
    # The exit seen differently by observers: once per episode.
    exit_watch = mesh_status.Watch()
    split = report([('r3', 'active', '')], consistent=False)
    assert [k.split(':')[0] for k, _, _ in exit_watch.observe(now, 'm1', 'Mac', 'member', True, split, [])] == ['mesh_exit_inconsistent']
    assert exit_watch.observe(now + 1000, 'm1', 'Mac', 'member', True, split, []) == []
    # Coming and going does not send one each time: once per ALERT_COOLDOWN_MS.
    exit_watch.observe(now + 2000, 'm1', 'Mac', 'member', True, report([('r3', 'active', '')]), [])
    assert exit_watch.observe(now + 3000, 'm1', 'Mac', 'member', True, split, []) == []
    exit_watch.observe(now + 4000, 'm1', 'Mac', 'member', True, report([('r3', 'active', '')]), [])
    assert exit_watch.observe(now + 5000, 'm1', 'Mac', 'member', True, split, []) == []
    late = now + mesh_status.ALERT_COOLDOWN_MS
    assert [k.split(':')[0] for k, _, _ in exit_watch.observe(late, 'm1', 'Mac', 'member', True, split, [])] == ['mesh_exit_inconsistent']


# ----- the path between two nodes -----
NOW = 1_793_000_000_000


def reports(**raw):
    return {sid: {'status': mesh_status.clean_status(r), 'at': r.pop('_at', NOW)} for sid, r in raw.items()}


def hop(node, via='', rtt=None):
    return {'node': node, 'via': via, 'rttMs': rtt}


ROLES = {'home': 'relay', 'toy': 'relay', 'sg': 'relay', 'mac': 'member', 'win': 'member', 'pc': 'member'}


def lan(state='direct'):
    return {'state': state, 'via': 'lan', 'since': 1, 'attempts': 1, 'lastError': ''}


def test_path_over_a_direct_link():
    statuses = reports(mac={'peers': [{'id': 'win', 'kind': 'direct', 'active': True, 'rttMs': 3.0, 'direct': lan()}],
                            'entry': {'relay': 'toy'}})
    assert find('mac', 'win', ROLES, statuses, {}, NOW) == {'hops': [hop('mac'), hop('win', 'lan', 3.0)], 'rttMs': 3.0, 'stale': False}
    # Seen from the other end too; a direct link still punching is not the path.
    assert find('win', 'mac', ROLES, statuses, {}, NOW)['hops'] == [hop('win'), hop('mac', 'lan', 3.0)]
    punching = reports(mac={'peers': [{'id': 'win', 'kind': 'direct', 'rttMs': None, 'direct': lan('punching')}]})
    assert find('mac', 'win', ROLES, punching, {}, NOW) == {'hops': [hop('mac')], 'rttMs': None, 'stale': True}


def test_path_through_one_relay():
    statuses = reports(
        mac={'entry': {'relay': 'toy'}, 'peers': [{'id': 'toy', 'kind': 'entry', 'active': True, 'rttMs': 4.0, 'direct': lan()}]},
        win={'entry': {'relay': 'toy'}, 'peers': [{'id': 'toy', 'kind': 'entry', 'active': True, 'rttMs': 5.0}]},
        toy={'owners': {'win': {'relay': 'toy', 'seq': 1, 'leaseMs': 30000}}})
    assert find('mac', 'win', ROLES, statuses, {}, NOW) == {
        'hops': [hop('mac'), hop('toy', 'lan', 4.0), hop('win', 'entry', 5.0)], 'rttMs': 9.0, 'stale': False}
    # To the relay itself.
    assert find('mac', 'toy', ROLES, statuses, {}, NOW) == {'hops': [hop('mac'), hop('toy', 'lan', 4.0)], 'rttMs': 4.0, 'stale': False}
    # A relay reports 0 for members it does not measure: the member's own round trip counts.
    statuses['toy']['status']['peers'] = [{'id': 'win', 'kind': 'member', 'active': True, 'rttMs': 0.0, 'direct': None, 'endpoint': ''}]
    assert find('toy', 'win', ROLES, statuses, {}, NOW) == {'hops': [hop('toy'), hop('win', 'entry', 5.0)], 'rttMs': 5.0, 'stale': False}


def test_path_through_several_relays_follows_the_reported_route():
    statuses = reports(
        pc={'entry': {'relay': 'home'}, 'peers': [{'id': 'home', 'kind': 'entry', 'active': True, 'rttMs': 1.0}]},
        home={'owners': {'mac': {'relay': 'toy'}},
              'routing': {'mode': 'dynamic', 'links': [{'peer': 'sg', 'state': 'up', 'rttMs': 79.0}, {'peer': 'toy', 'state': 'down', 'rttMs': None}],
                          'routes': [{'dest': 'toy', 'nextHop': 'sg', 'cost': 160, 'path': ['home', 'sg', 'toy']}]}},
        sg={'routing': {'mode': 'dynamic', 'links': [{'peer': 'toy', 'state': 'up', 'rttMs': 75.0}], 'routes': []}},
        mac={'entry': {'relay': 'toy'}, 'peers': [{'id': 'toy', 'kind': 'entry', 'active': True, 'rttMs': 4.0, 'direct': lan()}]})
    assert find('pc', 'mac', ROLES, statuses, {}, NOW) == {
        'hops': [hop('pc'), hop('home', 'entry', 1.0), hop('sg', 'relay', 79.0), hop('toy', 'relay', 75.0), hop('mac', 'lan', 4.0)],
        'rttMs': 159.0, 'stale': False}
    # Without reported routes (static, or before any advertisement) the configured next hops are followed.
    static = copy.deepcopy(statuses)
    static['home']['status']['routing'] = None
    static['home']['status']['peers'] = [{'id': 'toy', 'kind': 'relay', 'active': True, 'rttMs': 2.5, 'direct': None, 'endpoint': ''}]
    configs = {'home': {'nextHops': {'toy': 'toy', 'sg': 'sg'}}}
    assert find('pc', 'mac', ROLES, static, configs, NOW) == {
        'hops': [hop('pc'), hop('home', 'entry', 1.0), hop('toy', 'relay', 2.5), hop('mac', 'lan', 4.0)], 'rttMs': 7.5, 'stale': False}
    # A dynamic route that does not reach the relay stops the path there.
    lost = copy.deepcopy(statuses)
    lost['home']['status']['routing']['routes'] = [{'dest': 'sg', 'nextHop': 'sg', 'cost': 80, 'path': ['home', 'sg'], 'since': 0, 'switches': 0}]
    assert find('pc', 'mac', ROLES, lost, {}, NOW) == {'hops': [hop('pc'), hop('home', 'entry', 1.0)], 'rttMs': None, 'stale': True}


def test_path_is_stale_when_a_report_is_missing_or_old():
    statuses = reports(
        mac={'entry': {'relay': 'toy'}, 'peers': [{'id': 'toy', 'kind': 'entry', 'active': True, 'rttMs': 4.0}]},
        win={'_at': NOW - 10 * 60_000, 'entry': {'relay': 'toy'}, 'peers': [{'id': 'toy', 'kind': 'entry', 'active': True, 'rttMs': 5.0}]},
        toy={'owners': {'win': {'relay': 'toy'}}})
    # win's report is old: the last segment has no round trip, but the path is still known from toy.
    assert find('mac', 'win', ROLES, statuses, {}, NOW) == {
        'hops': [hop('mac'), hop('toy', 'entry', 4.0), hop('win', 'entry', None)], 'rttMs': None, 'stale': True}
    # No report from the source: nothing beyond it is known.
    assert find('pc', 'win', ROLES, statuses, {}, NOW) == {'hops': [hop('pc')], 'rttMs': None, 'stale': True}
    # The destination attached nowhere anyone reports.
    assert find('mac', 'pc', ROLES, statuses, {}, NOW) == {'hops': [hop('mac'), hop('toy', 'entry', 4.0)], 'rttMs': None, 'stale': True}


def test_only_a_consistent_public_verdict_gives_a_punching_address():
    def ip(**reach):
        status = mesh_status.clean_status({'reachability': {'verdict': 'public', 'publicAddr': '45.32.100.7:40123', 'consistent': True, **reach}})
        return mesh_status.public_ip(status)
    # The observed port is the NAT's; the endpoint uses listen.udp, so only the IP is taken.
    assert ip() == '45.32.100.7' and ip(publicAddr='[2606:4700::7]:40123') == '2606:4700::7'
    assert ip(consistent=False) == '' and ip(verdict='punchable') == '' and ip(verdict='symmetric') == ''
    assert ip(publicAddr='') == '' and ip(publicAddr='host.example:1') == '' and ip(publicAddr='0.0.0.0:1') == ''
    assert mesh_status.public_ip(None) == '' and mesh_status.public_ip(mesh_status.clean_status({})) == ''
    # Others send to it: never an address that is not a global one, whatever the node says it is.
    for addr in ('10.1.2.3', '192.168.1.1', '172.16.0.9', '100.64.0.1', '127.0.0.1', '169.254.1.1', '198.51.100.7', '224.0.0.1',
                 '255.255.255.255', '[fd00::1]', '[fe80::1]', '[2001:db8::7]', '[ff02::1]', '[::1]'):
        assert ip(publicAddr=f'{addr}:40123') == '', addr
    # Seen behind one NAT is only compared, never sent to: a private address still groups.
    assert mesh_status.nat_ip(mesh_status.clean_status({'reachability': {'verdict': 'punchable', 'publicAddr': '10.1.2.3:4000'}})) == '10.1.2.3'


def test_punched_relay_links_and_candidates_aim_only_at_a_known_address():
    d = site_doc()
    f = facts(ipv4=SITE_IPV4)
    f['r3']['publicIp'] = '192.0.2.34'  # TOY found itself public and every observer agreed
    configs = build(d, f, 'net', 1)
    assert configs['gw']['links'][2] == {'peer': 'r3', 'kind': 'relay', 'dial': False, 'punch': True, 'endpoint': 'udp://192.0.2.34:51999'}
    assert configs['r3']['links'][0] == {'peer': 'gw', 'kind': 'relay', 'dial': True, 'punch': True}
    assert configs['m1']['entry']['candidates'][0] == {'id': 'c1', 'relay': 'r3', 'punch': True, 'endpoint': 'udp://192.0.2.34:51999'}
    # Entered by the user: that wins. Neither way does the panel's IPv4 appear, nor does a site relay become a reflector.
    d['members'][3]['publicEndpoint'] = 'home.example.net'
    configs = build(d, f, 'net', 1)
    assert configs['r3']['links'][0]['endpoint'] == 'udp://home.example.net:51999'
    assert '192.0.2.42' not in json.dumps(configs) and '192.0.2.117' not in json.dumps(configs)
    assert configs['m1']['reflectors'] == ['203.0.113.1:51999', '203.0.113.2:51999']


def test_rule_ids_must_be_unique_and_odd_input_is_a_field_error():
    d = base()
    members = [{'serverId': m['serverId']} for m in d['members']]
    rule = {'id': 'web', 'src': {'members': ['m1']}, 'dst': {'members': ['m2']}}
    with pytest.raises(model.MeshInvalid) as raised:
        model.clean_doc({'members': members, 'rules': [rule, {**rule, 'proto': 'tcp'}]}, d)
    assert [(e['path'], e['error']) for e in raised.value.errors] == [('rules', 'rule_id_duplicate')]
    # A server id that is not text is an unknown member, not a crash.
    with pytest.raises(model.MeshInvalid) as raised:
        model.clean_doc({'members': [*members, {'serverId': ['m1']}]}, d)
    assert ('members[4].serverId', 'member_not_installed') in {(e['path'], e['error']) for e in raised.value.errors}
    # A pasted address keeps no surrounding space.
    members[2]['publicEndpoint'] = ' 203.0.113.9\n'
    assert model.clean_doc({'members': members}, d)['members'][2]['publicEndpoint'] == '203.0.113.9'


def test_a_dial_target_without_an_address_is_left_out_not_sent_empty():
    """An address not known (yet) never becomes tcp://:port: the node would refuse the whole config."""
    d = base()
    d['relayLinks'] = {'mode': 'full', 'pairs': [pair('r1', 'r2', dialer='b')]}
    d['members'][1]['listen']['tls'] = 52001
    d['members'][2]['entry']['candidates'] = [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}, {'id': 'c2', 'relay': 'r2', 'transport': 'tls'},
                                              {'id': 'c3', 'relay': 'r2', 'transport': 'udp'}]
    d['members'][3]['listen']['tcp'] = 52010
    d['directLinks'] = [{'a': 'm1', 'b': 'm2', 'transport': 'tcp', 'dialer': 'a', 'cost': 1}]
    configs = build(d, facts(ipv4={'r1': '', 'm2': ''}), 'net', 1)
    endpoints = [x.get('endpoint', '') for c in configs.values() for x in [*c['links'], *((c['entry'] or {}).get('candidates') or [])]]
    assert endpoints and not any('://:' in ep or ep.startswith(':') for ep in endpoints)
    # A link keeps its place (next hops and bias still name it) but waits for the other end to dial.
    r2 = {link['peer']: link for link in configs['r2']['links']}
    assert r2['r1'] == {'peer': 'r1', 'kind': 'relay', 'dial': False}
    assert configs['r2']['nextHops'] == {'r1': 'r1'} and configs['r2']['routing']['bias'] == {'r1': 0}
    assert configs['m1']['links'] == [{'peer': 'm2', 'kind': 'direct', 'dial': False}]
    # A candidate without an address, or a TLS one without pins, is left out; the others keep their ids.
    assert [c['id'] for c in configs['m1']['entry']['candidates']] == ['c3']
    for c in configs.values():
        for link in c['links']:
            assert bool(link.get('endpoint')) == (link['dial'] and not link.get('punch')) or link.get('punch')
            assert link.get('tlsPins') is None or link['tlsPins']
    # The draft check names each one.
    found = errors(d, facts(ipv4={'r1': '', 'm2': ''}), applying=True)
    assert {('relayLinks.pairs[0]', 'endpoint_missing'), ('members[2].entry.candidates[0]', 'endpoint_missing'),
            ('members[2].entry.candidates[1]', 'tls_fingerprint_missing'), ('directLinks[0]', 'endpoint_missing')} <= found
    # With the addresses known nothing is left out.
    configs = build(d, facts(ipv4={}, tls=TLS), 'net', 1)
    assert [c['id'] for c in configs['m1']['entry']['candidates']] == ['c1', 'c2', 'c3']
    assert {link['peer']: link['endpoint'] for link in configs['r2']['links']}['r1'].startswith('udp://203.0.113.1:')


# ----- config schema: the newest one the controller and every member read -----
def with_schemas(**ranges):
    f = facts()
    for sid, r in ranges.items():
        # None: never said, as a data plane from before configSchemas.
        f[sid]['configSchemas'] = r
    return f


def schemas_built(d, f):
    return {c['schema'] for c in build(d, f, 'net', 1).values()}


def test_configs_use_the_newest_schema_every_member_reads(monkeypatch):
    from app.servers import mesh_config
    monkeypatch.setattr(mesh_config, 'SCHEMA', (1, 2))
    d = base()
    # A member that reads only 1 (or never said, as a data plane from before configSchemas) holds everyone at 1.
    assert schemas_built(d, with_schemas(r1=(1, 2), r2=(1, 2), m1=(1, 2), m2=(1, 1))) == {1}
    assert schemas_built(d, with_schemas(r1=(1, 2), r2=(1, 2), m1=(1, 2), m2=None)) == {1}
    assert errors(d, with_schemas(r1=(1, 2), r2=(1, 2), m1=(1, 2), m2=(1, 1))) == set()
    # All upgraded: 2.
    assert schemas_built(d, with_schemas(r1=(1, 2), r2=(1, 2), m1=(1, 2), m2=(1, 2))) == {2}
    # A member being removed does not count.
    d['members'][3]['removing'] = True
    assert schemas_built(d, with_schemas(r1=(1, 2), r2=(1, 2), m1=(1, 2), m2=(1, 1))) == {2}
    # The controller's own range bounds it: nodes reading up to 3 still get 2.
    d['members'][3]['removing'] = False
    assert schemas_built(d, with_schemas(r1=(1, 3), r2=(2, 3), m1=(1, 3), m2=(2, 3))) == {2}
    # Today's controller writes 1 whatever the nodes read.
    monkeypatch.setattr(mesh_config, 'SCHEMA', (1, 1))
    assert schemas_built(d, with_schemas(r1=(1, 2), r2=(1, 2), m1=(1, 2), m2=(1, 2))) == {1}


def test_a_member_without_a_common_schema_is_named_with_what_to_update(monkeypatch):
    from app.servers import mesh_config
    d = base()
    # Behind the controller: update the node.
    monkeypatch.setattr(mesh_config, 'SCHEMA', (2, 3))
    found = model.validate(d, with_schemas(r1=(2, 3), r2=(2, 3), m1=(2, 2), m2=(1, 1)))
    assert [(e['path'], e['error']) for e in found] == [('members[3]', 'config_schema_node_outdated')]
    assert 'M2' in found[0]['message'] and '更新这台的节点' in found[0]['message']
    # Ahead of the controller: update the controller.
    monkeypatch.setattr(mesh_config, 'SCHEMA', (1, 2))
    found = model.validate(d, with_schemas(r1=(3, 4), r2=(1, 2), m1=(1, 2), m2=(1, 2)))
    assert [(e['path'], e['error']) for e in found] == [('members[0]', 'config_schema_controller_outdated')]
    assert 'R1' in found[0]['message'] and '更新主控' in found[0]['message']
    # Each reads one the controller writes, but no common one: the oldest is named.
    monkeypatch.setattr(mesh_config, 'SCHEMA', (1, 3))
    found = model.validate(d, with_schemas(r1=(2, 3), r2=(1, 3), m1=(1, 3), m2=(1, 1)))
    assert [(e['path'], e['error']) for e in found] == [('members[3]', 'config_schema_mismatch')]
    assert 'M2' in found[0]['message'] and 'R1' in found[0]['message'] and '更新 M2 的节点' in found[0]['message']
    # Without a common one the configs still build; delivery skips the members that cannot read them.
    assert schemas_built(d, with_schemas(r1=(2, 3), r2=(1, 3), m1=(1, 3), m2=(1, 1))) == {3}


# ----- signing and end-to-end keys, relay links -----
def test_configs_carry_every_nodes_keys_and_relays_get_the_relay_links():
    links = {'mode': 'custom', 'pairs': [pair('r1', 'r2'), pair('r2', 'r3')]}
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('r3', 'relay'),
            member('m1', entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}], 'autoReturn': True}),
            relay_links=links)
    configs = build(d, facts(), 'net', 1)
    for sid, c in configs.items():
        for n in c['nodes']:
            assert (n['signKey'], n['e2eKey']) == (KEYS[n['id']][2], KEYS[n['id']][3])
        if c['self']['role'] == 'relay':
            # The whole mesh's relay links, not only this relay's: routing takes no other.
            assert c['relayLinks'] == [['r1', 'r2'], ['r2', 'r3']]
        else:
            assert 'relayLinks' not in c


def test_a_node_without_its_keys_is_a_warning_not_a_refusal():
    # The configs leave it out until it reports them: nothing else is held back.
    d = doc(member('r1', 'relay'), member('m1', entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}], 'autoReturn': True}))
    f = facts()
    f['m1'] = {**f['m1'], 'signKey': '', 'e2eKey': ''}
    assert not any(e['error'] == 'mesh_keys_missing' for e in model.validate(d, f, applying=True))
    assert [(w['path'], w['error']) for w in model.hints(d, f)] == [('members[1]', 'mesh_keys_missing')]
    assert model.hints(d, facts()) == []


# ----- a member reporting another's keys -----
def test_a_member_copying_a_relays_end_to_end_key_does_not_cut_the_relay_out():
    """From the 10-30 recheck: a member reporting its only relay's e2eKey (every config carries it)
    had both left out, and the other members lost every node and entry. Only the later one goes."""
    cand = {'entry': {'mode': 'fixed', 'autoReturn': True, 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}]}}
    d = doc(member('r1', 'relay'), member('m1', **cand), member('m2', **cand))
    f = facts()
    f['m2'] = {**f['m2'], 'e2eKey': f['r1']['e2eKey']}
    configs = build(d, f, 'net', 2)
    assert sorted(n['id'] for n in configs['m1']['nodes']) == ['r1'] and [c['relay'] for c in configs['m1']['entry']['candidates']] == ['r1']
    assert sorted(n['id'] for n in configs['r1']['nodes']) == ['m1']
    # Spelled another way (the unused bits of the last character cleared), it is the same key to a node.
    respelled = f['r1']['e2eKey'][:42] + 'A='
    assert respelled != f['r1']['e2eKey'] and mesh_config.key_bytes(respelled) == mesh_config.key_bytes(f['r1']['e2eKey'])
    f['m2']['e2eKey'] = respelled
    assert mesh_config.keyless(d['members'], f) == {'m2'}
    assert '"r1"' in json.dumps(build(d, f, 'net', 2)['m1'])


def test_keyless_leaves_out_the_later_holder_of_a_repeated_key_only():
    # The recheck's keyless_check: X reports V's public end-to-end key as its own.
    f = {sid: own_keys(sid) for sid in ('V', 'X', 'R')}
    f['X']['e2eKey'] = f['V']['e2eKey']
    assert mesh_config.keyless([{'serverId': s} for s in ('V', 'X', 'R')], f) == {'X'}
    # An end-to-end key that is a WireGuard key still leaves its holder out, wherever that sits.
    f['X']['e2eKey'], f['V']['e2eKey'] = own_keys('X')['e2eKey'], f['R']['publicKey']
    assert mesh_config.keyless([{'serverId': s} for s in ('V', 'X', 'R')], f) == {'V'}


def test_a_member_that_reported_anothers_keys_is_a_warning_naming_both():
    d = doc(member('r1', 'relay'), member('m1', entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}], 'autoReturn': True}))
    f = facts()
    f['m1'] = {**f['m1'], 'signKey': '', 'e2eKey': '', 'keyConflict': {'signKey': 'r1', 'e2eKey': 'r1'}}
    # Not also "keys not reported yet": that would say it fixes itself.
    assert model.hints(d, f) == [{'path': 'members[1]', 'error': 'mesh_key_conflict',
                                  'message': 'M1 上报的签名密钥、端到端密钥和 R1 的相同，主控没有采用。它多半是从 R1 克隆出来的，'
                                             '请在 M1 上删掉组网目录里的 keys 后重新安装组网服务；R1 和其他成员照常。'}]


# ----- members whose keys are not in -----
def test_a_relay_or_member_without_keys_is_left_out_of_every_other_config():
    """No data plane takes a config naming a node without signing and end-to-end keys: such a member
    (an old data plane, one fallen back, one not reported yet) is left out as if removed, with every
    candidate, link, next hop, relay link, rule and gateway route that pointed at it."""
    cand = lambda *relays: {'entry': {'mode': 'failover', 'autoReturn': True, 'candidates': [{'id': f'c{i}', 'relay': r, 'transport': 'udp'} for i, r in enumerate(relays)]}}  # noqa: E731
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('r3', 'relay'),
            member('m1', tags=['公司'], **cand('r1', 'r2')), member('m2', **cand('r2')),
            member('gw', gateway={'subnets': ['192.168.1.0/24'], 'masquerade': True}, **cand('r3')),
            direct=[pair('m1', 'gw'), pair('m1', 'm2')],
            rules=[rule({'members': ['m1']}, {'members': ['gw'], 'subnets': ['192.168.1.0/24']}), rule({'tags': ['公司']}, {'members': ['m2']})])
    d['rules'] = [{**r, 'id': f'x{i}'} for i, r in enumerate(d['rules'])]
    f = facts()
    for sid in ('r2', 'gw'):
        f[sid] = {**f[sid], 'signKey': '', 'e2eKey': None}
    configs = build(d, f, 'net', 3)
    assert set(configs) == {'r1', 'r2', 'r3', 'm1', 'm2', 'gw'}
    for sid in ('r1', 'r3', 'm1', 'm2'):
        text = json.dumps(configs[sid])
        assert '"r2"' not in text and '"gw"' not in text, (sid, text)
        # Its subnets are no one's: a rule naming them still routes them in, to be dropped, as after a removal.
        assert all('192.168.1.0/24' not in n['subnets'] for n in configs[sid]['nodes'])
        assert all(n['signKey'] and n['e2eKey'] for n in configs[sid]['nodes'])
    assert [c['relay'] for c in configs['m1']['entry']['candidates']] == ['r1']
    assert configs['m2']['entry']['candidates'] == []
    assert [link['peer'] for link in configs['m1']['links']] == ['m2']
    assert configs['r1']['relayLinks'] == [['r1', 'r3']] and configs['r1']['nextHops'] == {'r3': 'r3'}
    # Each one's own config names only the members with keys, and itself as before.
    for sid in ('r2', 'gw'):
        assert sorted(n['id'] for n in configs[sid]['nodes']) == ['m1', 'm2', 'r1', 'r3']
        assert all(n['signKey'] and n['e2eKey'] for n in configs[sid]['nodes'])
    assert [c['relay'] for c in configs['gw']['entry']['candidates']] == ['r3']
    assert configs['gw']['self']['gateway']['subnets'] == ['192.168.1.0/24']
    # With every key in, they are back in every config.
    assert '"r2"' in json.dumps(build(d, facts(), 'net', 3)['r1']) and '"gw"' in json.dumps(build(d, facts(), 'net', 3)['m1'])


def test_a_rule_only_about_a_member_without_keys_lets_nothing_through():
    """Leaving the member out drops the rule side that named only it, and with it the rule: never a
    rule with an empty side, which a data plane reads as every address. A side that named it among
    others narrows to the others; rules not about it stay as they were."""
    d = doc(member('r1', 'relay'), member('m1'), member('m2'), member('gw', tags=['t']),
            rules=[rule({'members': ['m2']}, {'members': ['m1']}),
                   rule({'members': ['m1']}, {'members': ['m2']}),
                   rule({'members': ['gw']}, {'members': ['m2']}),
                   rule({'members': ['m2', 'gw']}, {'members': ['m1']}),
                   rule({'tags': ['t']}, {'members': ['m1']})])
    for i, (r, proto, ports) in enumerate(zip(d['rules'], ('tcp', 'any', 'udp', 'icmp', 'udp'), ([[22, 22]], [], [], [], [[53, 53]])), 1):
        r.update(id=f'x{i}', proto=proto, ports=ports)
    at = {m['serverId']: f"{m['address']}/32" for m in d['members']}
    full = build(d, facts(), 'net', 1)
    f = facts()
    f['m2'] = {**f['m2'], 'signKey': '', 'e2eKey': ''}
    configs = build(d, f, 'net', 1)
    # With m2's keys in, what m1 lets in names m2 in two rules.
    assert full['m1']['rules'] == [
        {'src': [at['m2']], 'dst': [at['m1']], 'proto': 'tcp', 'ports': [[22, 22]]},
        {'src': [at['m2'], at['gw']], 'dst': [at['m1']], 'proto': 'icmp', 'ports': []},
        {'src': [at['gw']], 'dst': [at['m1']], 'proto': 'udp', 'ports': [[53, 53]]}]
    # Without: the rule from m2 alone is gone, the one from m2 and gw is gw's, the tag rule is unchanged.
    assert configs['m1']['rules'] == [
        {'src': [at['gw']], 'dst': [at['m1']], 'proto': 'icmp', 'ports': []},
        {'src': [at['gw']], 'dst': [at['m1']], 'proto': 'udp', 'ports': [[53, 53]]}]
    for sid in ('r1', 'm1', 'gw'):
        assert all(r['src'] and r['dst'] for r in configs[sid]['rules']), sid
        assert at['m2'] not in json.dumps(configs[sid]['rules']), sid
    # The rules to m2 (from m1 and from gw) went with it: nothing opens on gw or r1, as before.
    assert configs['gw']['rules'] == full['gw']['rules'] == [] and configs['r1']['rules'] == full['r1']['rules'] == []


def test_a_member_whose_wireguard_or_control_key_is_not_32_bytes_is_left_out_alone():
    """From the 10-30 check: a 33-byte publicKey passed the old pattern, went into every config, and
    every data plane refused the whole config. Only that member goes now, with a warning naming it."""
    import base64
    cand = {'entry': {'mode': 'fixed', 'autoReturn': True, 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}]}}
    d = doc(member('r1', 'relay'), member('m1', **cand), member('m2', **cand))
    for field in ('publicKey', 'controlKey'):
        f = facts()
        f['m2'] = {**f['m2'], field: base64.b64encode(bytes(33)).decode()}
        assert mesh_config.keyless(d['members'], f) == {'m2'}
        configs = build(d, f, 'net', 2)
        assert sorted(n['id'] for n in configs['m1']['nodes']) == ['r1'] and sorted(n['id'] for n in configs['r1']['nodes']) == ['m1']
        assert [(w['path'], w['error']) for w in model.hints(d, f)] == [('members[2]', 'mesh_keys_invalid')]
