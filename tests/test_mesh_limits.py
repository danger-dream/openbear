"""Connection limits (mesh_limits): hard rules on connections across a boundary, which every listed
connection must keep to and the mesh's own links skip or adapt to."""
from __future__ import annotations

import pytest

from app.servers import mesh_auto, mesh_limits
from app.servers import mesh_model as model
from app.servers.mesh_config import relay_pairs
from tests.test_mesh_config import doc, errors, facts, member, pair

ALL = {'udp': 51999, 'tcp': 52000, 'tls': 52001}
CN = {'kind': 'region', 'regions': ['CN']}
ABROAD = {'kind': 'region', 'regions': ['CN'], 'not': True}


def limit(name='跨境', a=CN, b=ABROAD, kid='k1', **conditions):
    return {'id': kid, 'name': name, 'on': True, 'a': a, 'b': b, 'relayEnd': False, 'transports': None, 'via': None, 'manualOnly': False, **conditions}


def regions(**by):
    f = facts()
    for sid, region in by.items():
        f[sid]['region'] = region
    return f


def test_limits_are_cleaned_and_numbered():
    raw = [{'name': ' 跨境 ', 'a': {'kind': 'region', 'regions': ['cn']}, 'b': ABROAD, 'transports': ['tls', 'tcp'], 'relayEnd': True},
           {'name': '只能手动', 'manualOnly': True, 'on': False}]
    clean = model.clean_doc({'limits': raw}, doc(member('m1')))['limits']
    assert clean[0] == {'id': 'k1', 'name': '跨境', 'on': True, 'a': {'kind': 'region', 'regions': ['CN'], 'not': False}, 'b': ABROAD,
                        'relayEnd': True, 'transports': ['tcp', 'tls'], 'via': None, 'manualOnly': False}
    assert clean[1]['id'] == 'k2' and clean[1]['on'] is False


@pytest.mark.parametrize('raw, path', [
    ({'name': ''}, 'limits[0].name'),
    ({'transports': ['quic']}, 'limits[0].transports'),
    ({'via': []}, 'limits[0].via'),
    ({'relayEnd': 'yes'}, 'limits[0].relayEnd'),
    ({'relayEnd': False, 'transports': None, 'via': None, 'manualOnly': False}, 'limits[0]'),
])
def test_each_limit_check_names_its_field(raw, path):
    with pytest.raises(model.MeshInvalid) as bad:
        model.clean_doc({'limits': [{**limit(), **raw}]}, doc(member('m1')))
    assert path in {e['path'] for e in bad.value.errors}


def test_a_connection_within_one_side_is_never_limited():
    f = regions(r1='CN', r2='CN', r3='US')
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('r3', 'relay'))
    live = {m['serverId']: m for m in d['members']}
    limits = [limit(transports=['tcp'])]
    assert mesh_limits.allowed(limits, live['r1'], live['r2'], 'udp', f)
    assert not mesh_limits.allowed(limits, live['r1'], live['r3'], 'udp', f)
    assert mesh_limits.allowed(limits, live['r3'], live['r1'], 'tcp', f)
    # Switched off, it limits nothing.
    assert mesh_limits.allowed([{**limits[0], 'on': False}], live['r1'], live['r3'], 'udp', f)


def test_every_listed_connection_across_must_keep_to_the_limit():
    f = regions(r1='CN', r2='US', m1='CN', m2='US')
    d = doc(member('r1', 'relay', listen=ALL), member('r2', 'relay', listen=ALL),
            member('m1', entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r2', 'transport': 'udp'}], 'autoReturn': True}),
            member('m2', entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r2', 'transport': 'udp'}], 'autoReturn': True}),
            relay_links={'mode': 'custom', 'pairs': [{**pair('r1', 'r2'), 'bias': 0}]}, direct=[pair('m1', 'm2')])
    d['limits'] = [limit(relayEnd=True, transports=['tcp', 'tls'])]
    got = {(e['path'], e['error']): e['message'] for e in model.validate(d, f)}
    assert got[('relayLinks.pairs[0]', 'limit_violation')] == 'R1 和 R2 违反连接限制「跨境」：跨界只允许 TCP、TLS'
    assert ('members[2].entry.candidates[0]', 'limit_violation') in got
    assert got[('directLinks[0]', 'limit_violation')] == 'M1 和 M2 违反连接限制「跨境」：跨界连接至少一端须是中转'
    # The US member entering the US relay is within one side.
    assert ('members[3].entry.candidates[0]', 'limit_violation') not in got
    # Over TCP the relay link and the entry keep to it.
    d['relayLinks']['pairs'][0]['transport'] = 'tcp'
    d['members'][2]['entry']['candidates'][0]['transport'] = 'tcp'
    assert {p for p, e in errors(d, f) if e == 'limit_violation'} == {'directLinks[0]'}


def test_only_through_the_named_machines():
    f = regions(r1='CN', r2='US', r3='US')
    d = doc(member('r1', 'relay', listen=ALL), member('r2', 'relay', listen=ALL), member('r3', 'relay', listen=ALL),
            relay_links={'mode': 'custom', 'pairs': [{**pair('r1', 'r2', transport='tcp'), 'bias': 0}, {**pair('r1', 'r3', transport='tcp'), 'bias': 0}]})
    d['limits'] = [limit(via=['r2'])]
    got = {(e['path'], e['error']): e['message'] for e in model.validate(d, f)}
    assert got == {('relayLinks.pairs[1]', 'limit_violation'): 'R1 和 R3 违反连接限制「跨境」：跨界只能经过 R2'}


def test_full_mode_moves_a_link_across_to_an_allowed_transport_or_leaves_it_out():
    udp = {'udp': 51999, 'tcp': 0, 'tls': 0}
    f = regions(r1='CN', r2='US', r3='US')
    d = doc(member('r1', 'relay', listen=ALL), member('r2', 'relay', listen=ALL), member('r3', 'relay', listen=udp))
    d['limits'] = [limit(transports=['tcp', 'tls'])]
    pairs = {frozenset((p['a'], p['b'])): p for p in relay_pairs(d, f)}
    # r1 and r2 move to TCP; r3 opens only UDP, so it dials r1 over TCP.
    assert pairs[frozenset(('r1', 'r2'))]['transport'] == 'tcp'
    assert pairs[frozenset(('r1', 'r3'))]['transport'] == 'tcp' and pairs[frozenset(('r1', 'r3'))]['dialer'] == 'b'
    assert pairs[frozenset(('r2', 'r3'))]['transport'] == 'udp'
    assert not {e for _, e in errors(d, f) if e == 'limit_violation'}
    # With UDP all either end opens, the mesh does not link them at all.
    d['members'][0]['listen'] = udp
    assert frozenset(('r1', 'r3')) not in {frozenset((p['a'], p['b'])) for p in relay_pairs(d, f)}


def test_set_by_hand_only_keeps_the_mesh_from_making_links_across():
    f = regions(r1='CN', r2='US', r3='US')
    d = doc(member('r1', 'relay', listen=ALL), member('r2', 'relay', listen=ALL), member('r3', 'relay', listen=ALL))
    d['limits'] = [limit(manualOnly=True)]
    assert {frozenset((p['a'], p['b'])) for p in relay_pairs(d, f)} == {frozenset(('r2', 'r3'))}
    # Nothing then joins r1 to the rest, which the draft check says.
    assert ('relayLinks', 'relay_unreachable') in errors(d, f)
    # A link listed by hand is allowed across.
    d['relayLinks']['pairs'] = [{**pair('r1', 'r2', transport='tcp'), 'bias': 0}]
    assert not {e for _, e in errors(d, f) if e in ('limit_violation', 'relay_unreachable')}


def test_auto_mode_never_picks_a_link_the_limits_forbid():
    f = regions(r1='CN', r2='CN', r3='US', m1='US')
    ids = ['r1', 'r2', 'r3', 'm1']
    d = doc(*(member(sid, 'relay', listen=ALL) for sid in ids), relay_links={'mode': 'auto', 'pairs': []})
    d['limits'] = [limit(via=['r2'])]
    rtts = {frozenset(('r1', 'r3')): 5}
    got = {frozenset((p['a'], p['b'])) for p in mesh_auto.auto_pairs(d, f, rtts)}
    assert frozenset(('r1', 'r3')) not in got and frozenset(('r1', 'm1')) not in got
    assert frozenset(('r2', 'r3')) in got or frozenset(('r2', 'm1')) in got
    d['relayLinks']['pairs'] = mesh_auto.auto_pairs(d, f, rtts)
    assert not {e for _, e in errors(mesh_auto.resolve(d, f), f) if e in ('limit_violation', 'relay_unreachable')}


def test_an_auto_transport_moves_to_one_the_limit_allows():
    f = regions(r1='US', m1='CN', r2='CN', r3='US')
    p = {**pair('r2', 'r3'), 'bias': 0, 'auto': list(mesh_auto.PAIR_AUTO)}
    d = doc(member('r1', 'relay', listen=ALL), member('r2', 'relay', listen=ALL), member('r3', 'relay', listen=ALL),
            member('m1', entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp', 'auto': ['transport']}], 'autoReturn': True}),
            relay_links={'mode': 'custom', 'pairs': [p]})
    d['limits'] = [limit(transports=['tls'])]
    got = mesh_auto.resolve(d, f)
    assert got['relayLinks']['pairs'][0]['transport'] == 'tls'
    assert next(m for m in got['members'] if m['serverId'] == 'm1')['entry']['candidates'][0]['transport'] == 'tls'
    # Set by hand only as well: links listed by hand still move to an allowed transport, and the automatic
    # values shown say so, as the fields are set.
    d['limits'] = [limit(transports=['tcp', 'tls'], manualOnly=True)]
    got = mesh_auto.resolve(d, f)
    assert got['relayLinks']['pairs'][0]['transport'] == 'tcp'
    assert next(m for m in got['members'] if m['serverId'] == 'm1')['entry']['candidates'][0]['transport'] == 'tcp'
    shown = mesh_auto.explain(d, f)
    assert shown['pairs']['r2|r3']['transport'] == {'value': 'tcp', 'reason': '连接限制「跨境」不允许 UDP，用 TCP'}
    assert shown['candidates']['m1|r1']['transport']['value'] == 'tcp'
    assert not {e for _, e in errors(got, f) if e == 'limit_violation'}
