"""What the mesh works out by itself (mesh_auto): auto fields from listen ports and reachability,
line rules steering them, auto mode's links, and the draft fields around them: announce, port
mappings and backup gateways."""
from __future__ import annotations

import pytest

from app.servers import mesh_auto
from app.servers import mesh_model as model
from app.servers.mesh_config import build, endpoint_host, exports
from tests.test_mesh_config import doc, errors, facts, member, pair

PAIR_AUTO = list(mesh_auto.PAIR_AUTO)


def auto(m, *fields):
    m['auto'] = list(fields or mesh_auto.MEMBER_AUTO)
    return m


def line_rule(name, a=None, b=None, rid='l1', **sets):
    return {'id': rid, 'name': name, 'on': True, 'applies': 'both', 'a': a or {'kind': 'any'}, 'b': b or {'kind': 'any'},
            'order': None, 'bias': None, 'dialer': None, **sets}


def auto_pair(a, b):
    return {**pair(a, b), 'bias': 0, 'auto': PAIR_AUTO}


def by_id(d):
    return {m['serverId']: m for m in d['members']}


# ----- auto fields -----
def test_listen_left_to_the_mesh_opens_what_others_dial_it_with():
    d = doc(auto(member('r1', 'relay')), auto(member('m1')), auto(member('m2', 'relay', relayMode='site')))
    got = by_id(mesh_auto.resolve(d, facts()))
    # Public relays UDP and TCP for newcomers, members and site relays UDP only (punching).
    assert got['r1']['listen'] == {'udp': 51999, 'tcp': 52000, 'tls': 0}
    assert got['m1']['listen'] == {'udp': 51999, 'tcp': 0, 'tls': 0}
    assert got['m2']['listen'] == {'udp': 51999, 'tcp': 0, 'tls': 0}
    # The port numbers are the member's; auto only decides which are open.
    by_id(d)['r1']['listen'] = {'udp': 41000, 'tcp': 0, 'tls': 0}
    assert by_id(mesh_auto.resolve(d, facts()))['r1']['listen'] == {'udp': 41000, 'tcp': 52000, 'tls': 0}


def test_a_link_set_to_tls_by_hand_opens_tls_on_the_end_it_dials():
    d = doc(auto(member('r1', 'relay')), member('r2', 'relay'), relay_links={'mode': 'custom', 'pairs': [pair('r2', 'r1', transport='tls')]})
    assert by_id(mesh_auto.resolve(d, facts()))['r1']['listen']['tls'] == 52001
    d['members'][0]['auto'] = []
    assert by_id(mesh_auto.resolve(d, facts()))['r1']['listen']['tls'] == 0


def test_fields_left_out_of_auto_keep_what_was_entered():
    d = doc(member('r1', 'relay', listen={'udp': 51999, 'tcp': 0, 'tls': 0}), member('m1'))
    assert mesh_auto.resolve(d, facts()) == d


def test_an_auto_link_takes_the_first_protocol_the_dialed_end_opens_and_says_why():
    d = doc(member('r1', 'relay', listen={'udp': 0, 'tcp': 52000, 'tls': 0}), member('r2', 'relay'),
            relay_links={'mode': 'custom', 'pairs': [auto_pair('r2', 'r1')]})
    d['relayLinks']['pairs'][0]['dialer'] = 'a'
    got = mesh_auto.resolve(d, facts())['relayLinks']['pairs'][0]
    assert got['transport'] == 'tcp'
    why = mesh_auto.explain(d, facts())['pairs']['r2|r1']['transport']
    assert why == {'value': 'tcp', 'reason': 'R1 没开 UDP，用 TCP'}


def test_a_site_relay_always_dials_and_is_punched_over_udp():
    d = doc(member('r1', 'relay'), member('h1', 'relay', relayMode='site'), relay_links={'mode': 'custom', 'pairs': [auto_pair('r1', 'h1')]})
    got = mesh_auto.resolve(d, facts())['relayLinks']['pairs'][0]
    assert got['dialer'] == 'b' and got['transport'] == 'udp'
    assert mesh_auto.explain(d, facts())['pairs']['r1|h1']['dialer']['reason'] == '站点中转在 NAT 后，只能由它主动连'


def test_the_end_others_can_reach_is_the_one_dialed():
    f = facts()
    f['r1']['verdict'], f['r2']['verdict'] = 'symmetric', 'public'
    d = doc(member('r1', 'relay'), member('r2', 'relay'), relay_links={'mode': 'custom', 'pairs': [auto_pair('r2', 'r1')]})
    assert mesh_auto.resolve(d, f)['relayLinks']['pairs'][0]['dialer'] == 'b'
    assert mesh_auto.explain(d, f)['pairs']['r2|r1']['dialer']['reason'] == '只有 R2 能被直接连到（可达性检测）'


def test_relay_type_left_to_the_mesh_follows_the_reachability_check():
    f = facts()
    d = doc(auto(member('r1', 'relay'), 'relayMode'))
    f['r1']['verdict'] = 'punchable'
    assert by_id(mesh_auto.resolve(d, f))['r1']['relayMode'] == 'site'
    f['r1']['verdict'] = 'public'
    assert by_id(mesh_auto.resolve(d, f))['r1']['relayMode'] == 'public'
    # An address entered by hand is a promise it can be reached.
    f['r1']['verdict'] = 'symmetric'
    d['members'][0]['publicEndpoint'] = 'r1.example.net'
    assert mesh_auto.explain(d, f)['relayMode']['r1'] == {'value': 'public', 'reason': '填了对外地址'}


def test_an_entry_candidate_left_to_the_mesh_follows_the_relays_listen_ports():
    d = doc(member('r1', 'relay', listen={'udp': 0, 'tcp': 0, 'tls': 52001}),
            member('m1', entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp', 'auto': ['transport']}], 'autoReturn': True}))
    assert by_id(mesh_auto.resolve(d, facts()))['m1']['entry']['candidates'][0]['transport'] == 'tls'


# ----- line rules -----
def regions(**by):
    f = facts()
    for sid, region in by.items():
        f[sid]['region'] = region
    return f


def test_a_line_rule_sets_the_protocol_order_between_the_sides_it_names():
    f = regions(r1='CN', r2='US', r3='US')
    d = doc(member('r1', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 52001}), member('r2', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 52001}),
            member('r3', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 52001}),
            relay_links={'mode': 'custom', 'pairs': [auto_pair('r1', 'r2'), auto_pair('r2', 'r3')]})
    d['lineRules'] = [line_rule('跨境走 TLS', {'kind': 'region', 'regions': ['CN']}, {'kind': 'region', 'regions': ['CN'], 'not': True}, order=['tls', 'tcp'])]
    pairs = mesh_auto.resolve(d, f)['relayLinks']['pairs']
    assert [p['transport'] for p in pairs] == ['tls', 'udp']
    ex = mesh_auto.explain(d, f)['pairs']
    assert ex['r1|r2']['transport']['reason'] == '按规则「跨境走 TLS」' and ex['r1|r2']['rules'] == ['l1'] and ex['r2|r3']['rules'] == []
    # Switched off, the pair is back to what reachability gives.
    d['lineRules'][0]['on'] = False
    assert [p['transport'] for p in mesh_auto.resolve(d, f)['relayLinks']['pairs']] == ['udp', 'udp']


def test_a_line_rule_never_touches_a_field_set_by_hand():
    f = regions(r1='CN', r2='US')
    p = {**auto_pair('r1', 'r2'), 'auto': ['dialer', 'bias'], 'transport': 'tcp'}
    d = doc(member('r1', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 52001}), member('r2', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 52001}),
            relay_links={'mode': 'custom', 'pairs': [p]})
    d['lineRules'] = [line_rule('全走 TLS', order=['tls'], bias=-20, dialer='b')]
    got = mesh_auto.resolve(d, f)['relayLinks']['pairs'][0]
    assert got['transport'] == 'tcp' and got['bias'] == -20 and got['dialer'] == 'b'


def test_a_line_rule_on_tags_or_machines_and_only_for_entries():
    d = doc(member('r1', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 52001}),
            member('m1', tags=['办公'], entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp', 'auto': ['transport']}], 'autoReturn': True}),
            member('m2', entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp', 'auto': ['transport']}], 'autoReturn': True}))
    d['lineRules'] = [{**line_rule('办公接入走 TCP', {'kind': 'tag', 'tags': ['办公']}, {'kind': 'machines', 'ids': ['r1']}, order=['tcp']), 'applies': 'entries'}]
    got = by_id(mesh_auto.resolve(d, facts()))
    assert got['m1']['entry']['candidates'][0]['transport'] == 'tcp' and got['m2']['entry']['candidates'][0]['transport'] == 'udp'


def test_a_machine_of_unknown_region_is_not_taken_as_outside_one():
    f = regions(r1='CN')
    sel = {'kind': 'region', 'regions': ['CN'], 'not': True}
    m = member('r2', 'relay')
    assert mesh_auto.matches(sel, m, f) is False
    f['r2']['region'] = 'JP'
    assert mesh_auto.matches(sel, m, f) is True


@pytest.mark.parametrize('rule, path', [
    ({'name': ''}, 'lineRules[0].name'),
    ({'applies': 'all'}, 'lineRules[0].applies'),
    ({'order': ['udp', 'udp']}, 'lineRules[0].order'),
    ({'order': ['quic']}, 'lineRules[0].order'),
    ({'bias': 1.5}, 'lineRules[0].bias'),
    ({'dialer': 'x'}, 'lineRules[0].dialer'),
    ({'a': {'kind': 'region', 'regions': ['China']}}, 'lineRules[0].a.regions'),
    ({'b': {'kind': 'everyone'}}, 'lineRules[0].b'),
])
def test_each_line_rule_check_names_its_field(rule, path):
    d = doc(member('m1'))
    with pytest.raises(model.MeshInvalid) as bad:
        model.clean_doc({'lineRules': [{**line_rule('x'), **rule}]}, d)
    assert path in {e['path'] for e in bad.value.errors}


def test_line_rules_are_cleaned_and_numbered():
    d = doc(member('m1'))
    raw = [{'name': ' 跨境 ', 'a': {'kind': 'region', 'regions': ['cn', 'CN']}, 'order': ['tls']}, {'name': '第二条', 'on': False}]
    clean = model.clean_doc({'lineRules': raw}, d)['lineRules']
    assert clean[0] == {'id': 'l1', 'name': '跨境', 'on': True, 'applies': 'both', 'a': {'kind': 'region', 'regions': ['CN'], 'not': False},
                        'b': {'kind': 'any'}, 'order': ['tls'], 'bias': None, 'dialer': None}
    assert clean[1]['id'] == 'l2' and clean[1]['on'] is False


# ----- auto mode's links -----
def test_auto_mode_links_each_relay_to_its_nearest_and_joins_the_rest():
    ids = ['r1', 'r2', 'r3', 'm1', 'm2', 'gw']
    d = doc(*(member(sid, 'relay') for sid in ids), relay_links={'mode': 'auto', 'pairs': []})
    # Two clusters a long way apart: r1 r2 r3 near each other, m1 m2 gw near each other.
    near = [('r1', 'r2'), ('r1', 'r3'), ('r2', 'r3'), ('m1', 'm2'), ('m1', 'gw'), ('m2', 'gw')]
    rtts = {frozenset(k): 5 for k in near} | {frozenset(('r3', 'm1')): 150}
    rtts |= {frozenset((a, b)): 300 for a in ids[:3] for b in ids[3:] if frozenset((a, b)) not in rtts}
    pairs = mesh_auto.auto_pairs(d, facts(), rtts)
    got = {frozenset((p['a'], p['b'])) for p in pairs}
    # Each takes its 3 nearest: its two neighbours and the nearest across; every pair is left to the mesh.
    assert {frozenset(k) for k in near} <= got and frozenset(('r3', 'm1')) in got
    assert all(p['auto'] == PAIR_AUTO for p in pairs)
    configs = build(mesh_auto.resolve({**d, 'relayLinks': {'mode': 'auto', 'pairs': pairs}}, facts()), facts(), 'net', 1)
    assert set(configs['r1']['nextHops']) == set(ids) - {'r1'}


def test_auto_mode_keeps_a_link_whose_fields_were_set_and_links_a_newcomer():
    d = doc(member('r1', 'relay'), member('r2', 'relay'), relay_links={'mode': 'auto', 'pairs': [{**pair('r1', 'r2', transport='tcp'), 'bias': 0}]})
    d['members'].append(member('r3', 'relay'))
    d = model.allocate(d)
    pairs = {frozenset((p['a'], p['b'])): p for p in mesh_auto.auto_pairs(d, facts())}
    assert pairs[frozenset(('r1', 'r2'))]['transport'] == 'tcp' and 'auto' not in pairs[frozenset(('r1', 'r2'))]
    assert frozenset(('r1', 'r3')) in pairs and frozenset(('r2', 'r3')) in pairs


def test_auto_mode_never_links_two_site_relays_and_a_front_only_to_its_exits():
    f = facts()
    f['m2']['front'] = [{'serverId': 'r1', 'address': '10.0.10.246'}]
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('m1', 'relay', relayMode='site'), member('gw', 'relay', relayMode='site'),
            member('m2', 'relay'), relay_links={'mode': 'auto', 'pairs': []})
    got = {frozenset((p['a'], p['b'])) for p in mesh_auto.auto_pairs(d, f)}
    assert frozenset(('m1', 'gw')) not in got
    assert {k for k in got if 'm2' in k} == {frozenset(('m2', 'r1'))}


def test_auto_mode_drops_a_set_link_of_a_relay_turned_member():
    """From the 11-09 review: a link whose bias was set kept its place, and resolve failed on its
    end that was no longer a relay (KeyError), so the draft did not save."""
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('m1', 'relay'),
            relay_links={'mode': 'auto', 'pairs': [{**pair('r1', 'r2'), 'bias': 100, 'auto': ['transport', 'dialer']}]})
    d['members'][1]['role'] = 'member'
    d['members'][1]['entry']['candidates'] = [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}]
    d = mesh_auto.resolve(model.clean_doc(d, d), facts(), links=True)
    assert [(p['a'], p['b']) for p in d['relayLinks']['pairs']] == [('m1', 'r1')]
    assert errors(d) == set()


def test_the_draft_takes_auto_mode_and_auto_lists():
    d = doc(member('r1', 'relay'), member('r2', 'relay'))
    clean = model.clean_doc({'relayLinks': {'mode': 'auto', 'pairs': [{**pair('r1', 'r2'), 'auto': ['bias', 'transport']}]},
                             'members': [{**d['members'][0], 'auto': ['relayMode', 'listen']}, d['members'][1]]}, d)
    assert clean['relayLinks']['pairs'][0]['auto'] == ['transport', 'bias'] and clean['members'][0]['auto'] == ['listen', 'relayMode']
    with pytest.raises(model.MeshInvalid) as bad:
        model.clean_doc({'members': [{**d['members'][0], 'auto': ['address']}, d['members'][1]]}, d)
    assert [e['path'] for e in bad.value.errors] == ['members[0].auto']


# ----- announce and port mappings -----
def test_a_member_not_announced_is_never_dialed():
    f = facts()
    d = doc(member('r1', 'relay'), member('r2', 'relay'))
    d['members'][0]['announce'] = False
    assert endpoint_host(d['members'][0], f) == ''
    links = {link['peer']: link for link in build(d, f, 'net', 1)['r2']['links']}
    assert links['r1']['dial'] is False


def test_a_port_mapping_is_what_others_dial():
    # r1 dials r2 (the smaller id), at the port r2's NAT maps to the one it listens on.
    d = doc(member('r1', 'relay'), member('r2', 'relay', publicEndpoint='r2.example.net'))
    d['members'][1]['publicPorts'] = {'udp': 41999}
    links = {link['peer']: link for link in build(d, facts(), 'net', 1)['r1']['links']}
    assert links['r2']['endpoint'] == 'udp://r2.example.net:41999'
    assert build(d, facts(), 'net', 1)['r2']['self']['listen']['udp'] == 51999


def test_the_draft_takes_announce_and_port_mappings():
    d = doc(member('r1', 'relay'))
    clean = model.clean_doc({'members': [{**d['members'][0], 'announce': False, 'publicPorts': {'udp': 41999, 'tcp': 0}}]}, d)
    assert clean['members'][0]['announce'] is False and clean['members'][0]['publicPorts'] == {'udp': 41999}
    clean = model.clean_doc({'members': [{**clean['members'][0], 'announce': True, 'publicPorts': {}}]}, clean)
    assert 'announce' not in clean['members'][0] and 'publicPorts' not in clean['members'][0]
    with pytest.raises(model.MeshInvalid) as bad:
        model.clean_doc({'members': [{**d['members'][0], 'publicEndpoint': '1.2.3.4:51999', 'publicPorts': {'udp': 70000}}]}, d)
    assert {e['path'] for e in bad.value.errors} == {'members[0].publicEndpoint', 'members[0].publicPorts'}


# ----- named LANs and backup gateways -----
LAN = ['192.168.1.0/24']


def lan_doc(backup_subnets=LAN):
    return doc(member('r1', 'relay'), member('m1', gateway={'subnets': LAN, 'masquerade': True, 'name': '家里'}),
               member('m2', gateway={'subnets': backup_subnets, 'masquerade': True, 'backup': True}),
               rules=[{'id': 'r1', 'src': {'members': ['r1'], 'tags': [], 'prefixes': []}, 'dst': {'members': [], 'tags': [], 'subnets': LAN},
                       'proto': 'any', 'ports': [], 'note': ''}])


def test_a_backup_gateway_stands_by_for_the_same_subnets():
    d = lan_doc()
    assert not {e for e in errors(d) if e[1].startswith('gateway')}
    configs = build(d, facts(), 'net', 1)
    nodes = {n['id']: n for n in configs['r1']['nodes']}
    assert nodes['m1']['subnets'] == LAN and 'standbySubnets' not in nodes['m1']
    assert nodes['m2']['subnets'] == [] and nodes['m2']['standbySubnets'] == LAN
    # The name is the page's: the gateway's own config has what it does.
    assert configs['m1']['self']['gateway'] == {'subnets': LAN, 'masquerade': True}
    assert [x['gateway'] for x in exports(d)] == ['m1']


def test_a_backup_gateway_needs_a_gateway_announcing_exactly_its_subnets():
    assert ('members[2].gateway.subnets[0]', 'gateway_backup_without_primary') in errors(lan_doc(['192.168.1.0/25']))


def test_a_backup_gateway_needs_a_node_that_knows_standby():
    f = facts()
    # Unreported features are permitted in drafts, refused when applying.
    assert ('members[2].gateway.subnets[0]', 'gateway_backup_needs_update') in {(e['path'], e['error']) for e in model.validate(lan_doc(), f, applying=True)}
    f['m2']['features'] = frozenset({'mesh'})
    assert ('members[2].gateway.subnets[0]', 'gateway_backup_needs_update') in errors(lan_doc(), f)
    # If an already-applied mesh encounters an older m2 build, m2's own config omits the overlapping backup gateway.
    degraded = build(lan_doc(), f, 'net', 1)
    assert degraded['m2']['self']['gateway'] is None
    assert 'standbySubnets' not in {n['id']: n for n in degraded['r1']['nodes']}['m2']
    f['m2']['features'] = frozenset({'mesh', model.STANDBY_FEATURE})
    assert not {e for e in errors(lan_doc(), f) if e[1].startswith('gateway')}
    assert not {e for e in {(e['path'], e['error']) for e in model.validate(lan_doc(), f, applying=True)} if e[1].startswith('gateway')}


def test_the_draft_takes_a_lan_name_and_backup():
    d = doc(member('m1'))
    clean = model.clean_doc({'members': [{**d['members'][0], 'gateway': {'subnets': LAN, 'name': ' 家里 ', 'backup': True}}]}, d)
    assert clean['members'][0]['gateway'] == {'subnets': LAN, 'masquerade': True, 'name': '家里', 'backup': True}
    with pytest.raises(model.MeshInvalid) as bad:
        model.clean_doc({'members': [{**d['members'][0], 'gateway': {'subnets': LAN, 'name': 'x' * 41}}]}, d)
    assert [e['path'] for e in bad.value.errors] == ['members[0].gateway.name']
