"""前置机 (fronts) in the mesh: a front dials its exit at the address of the provider's line, asks
no other relay and no STUN server about its reachability, and full mode links it only to its exits
and to the site relays that dial it."""
from __future__ import annotations

import copy

from app.servers import mesh_auto, mesh_model
from app.servers.mesh import Mesh
from app.servers.mesh_config import build, relay_pairs
from tests.test_mesh_config import doc, facts, member, pair

PIN = 'ab' * 32
TLS = {'active': {'fingerprint': PIN}}
LINE = '10.0.10.246'


def front_facts(**changes):
    f = facts(tls={'r1': TLS, 'm2': TLS})
    f['m2']['front'] = [{'serverId': 'r1', 'address': LINE}]
    for sid, value in changes.items():
        f[sid].update(value)
    return f


def mesh_doc(front_udp=0, relay_links=None, stun=()):
    exit_ = member('r1', 'relay', listen={'tls': 52001})
    other = member('r2', 'relay')
    home = member('m1', 'relay', relayMode='site')
    front = member('m2', 'relay', listen={'udp': front_udp, 'tcp': 0, 'tls': 52001})
    links = relay_links or {'mode': 'custom', 'pairs': [pair('m2', 'r1', transport='tls'), pair('r1', 'r2'), pair('m1', 'm2', transport='tls')]}
    d = doc(exit_, other, home, front, relay_links=links)
    d['settings']['reachability'] = {'stun': list(stun)}
    return d


def test_a_front_dials_its_exit_on_the_line_and_asks_nobody_else():
    f = front_facts()
    configs = build(mesh_doc(stun=['stun.example.com:3478']), f, 'net', 1)
    to_exit = next(link for link in configs['m2']['links'] if link['peer'] == 'r1')
    assert to_exit['dial'] and to_exit['endpoint'] == f'tls://{LINE}:52001' and to_exit['tlsPins'] == [PIN]
    # Without UDP of its own it opens no UDP socket: no reflectors, no STUN.
    assert configs['m2']['reflectors'] == [] and configs['m2']['reachability'] == {'stun': []}
    # The home relay dials the front at its public address, as before.
    to_front = next(link for link in configs['m1']['links'] if link['peer'] == 'm2')
    assert to_front['dial'] and to_front['endpoint'] == f"tls://{f['m2']['ipv4']}:52001"
    # Everyone else keeps their STUN servers.
    assert configs['r2']['reachability'] == {'stun': ['stun.example.com:3478']}


def test_a_front_with_udp_asks_only_its_exit_and_is_nobodys_reflector():
    f = front_facts()
    configs = build(mesh_doc(front_udp=51999), f, 'net', 1)
    assert configs['m2']['reflectors'] == [f'{LINE}:51999']
    front_address = f"{f['m2']['ipv4']}:51999"
    assert all(front_address not in c['reflectors'] for sid, c in configs.items() if sid != 'm2')


def test_full_mode_links_a_front_only_to_its_exit_and_site_relays():
    f = front_facts()
    d = mesh_doc(relay_links={'mode': 'full', 'pairs': []})
    d['members'].append(member('r3', 'relay'))
    pairs = {frozenset((p['a'], p['b'])): p for p in relay_pairs(d, f)}
    assert frozenset(('m2', 'r2')) not in pairs and frozenset(('m2', 'r3')) not in pairs
    to_exit, home = pairs[frozenset(('m2', 'r1'))], pairs[frozenset(('m1', 'm2'))]
    assert to_exit['transport'] == 'tls' and to_exit[to_exit['dialer']] == 'm2'
    assert home['transport'] == 'tls' and home[home['dialer']] == 'm1'
    # Other relays still link to one another.
    assert frozenset(('r1', 'r2')) in pairs and frozenset(('r2', 'r3')) in pairs


def test_a_front_member_enters_through_its_exit_on_the_line():
    f = front_facts()
    front = member('m2', listen={'udp': 0}, entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'tls'}], 'autoReturn': True})
    configs = build(doc(member('r1', 'relay', listen={'tls': 52001}), front), f, 'net', 1)
    assert configs['m2']['entry']['candidates'][0]['endpoint'] == f'tls://{LINE}:52001'
    assert configs['m2']['reflectors'] == []


def test_joining_links_a_front_to_its_exit_once():
    f = front_facts()
    d = mesh_doc(relay_links={'mode': 'custom', 'pairs': []})
    Mesh._link_fronts(d, f)
    assert d['relayLinks']['pairs'] == [{'a': 'm2', 'b': 'r1', 'transport': 'tls', 'cost': 1, 'dialer': 'a', 'bias': 0}]
    Mesh._link_fronts(d, f)
    assert len(d['relayLinks']['pairs']) == 1
    # A link the user took out stays out while neither end has just become a relay.
    d['relayLinks']['pairs'] = []
    Mesh._link_fronts(d, f, {'r2'})
    assert d['relayLinks']['pairs'] == []


def test_joining_front_with_auto_listen_exit_defaults_to_tls_and_opens_exit_tls():
    f = front_facts(m2={'ipv4': ''})
    exit_ = {**member('r1', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0}), 'auto': list(mesh_auto.MEMBER_AUTO)}
    front = {**member('m2', 'relay', listen={'udp': 0, 'tcp': 0, 'tls': 52001}), 'auto': list(mesh_auto.MEMBER_AUTO)}
    d = doc(exit_, front, relay_links={'mode': 'custom', 'pairs': []})
    Mesh._link_fronts(d, f)
    resolved = mesh_auto.resolve(d, f, links=True)
    assert resolved['relayLinks']['pairs'][0]['transport'] == 'tls'
    assert resolved['members'][0]['listen']['tls'] == 52001

    # Connecting a site relay to a front with no publicEndpoint/ipv4 explains how to fill the front's address.
    with_site = copy.deepcopy(resolved)
    with_site['members'].append(member('m1', 'relay', relayMode='site'))
    with_site['relayLinks']['pairs'].append(pair('m1', 'm2', transport='tls', dialer='a'))
    errs = mesh_model.validate(with_site, f)
    assert any(e['error'] == 'endpoint_missing' and '是前置机，还没有填对外地址' in e['message'] for e in errs)

    # Custom-mode relay link edits emit specific link changes with endpoints instead of duplicate generic relayLinks.
    # Applied before the front joined: the exit had no TLS port and the link dialed TCP.
    applied = copy.deepcopy(resolved)
    applied['members'][0]['listen']['tls'] = 0
    applied['relayLinks']['pairs'][0]['transport'] = 'tcp'
    changes = Mesh._changes(applied, resolved, f)
    assert [c['kind'] for c in changes] == ['member.update', 'link.change']
    assert changes[1]['a'] == 'm2' and changes[1]['b'] == 'r1' and 'TCP → TLS' in changes[1]['text']

    # Non-default link cost (e.g. cost=80) is read from applied pairs even when issued configs are passed.
    cost_doc = copy.deepcopy(resolved)
    cost_doc['relayLinks']['pairs'][0]['cost'] = 80
    issued = build(cost_doc, f, 'net', 1)
    assert Mesh._changes(cost_doc, copy.deepcopy(cost_doc), f, configs=issued) == []
