"""前置机 (fronts) in the mesh: a front dials its exit at the address of the provider's line, asks
no other relay and no STUN server about its reachability, and full mode links it only to its exits
and to the site relays that dial it."""
from __future__ import annotations

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
