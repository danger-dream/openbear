"""Automatic link building (design 2.3), lightweight probe targets (3.2),
settings.autoLinks (4.1), damping/suppression, and automatic revisions."""
from __future__ import annotations

import base64
import copy
import json

import pytest

from app.servers import compat, mesh_autolinks, mesh_limits, mesh_status
from app.servers import mesh_model as model
from app.servers.mesh_config import build, relay_pairs
from tests.test_mesh_service import (
    OWNER,
    applied_base,
    mesh_setup,
    server,
    settled,
)
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

MATRIX = {
    'nodes': {
        'App-SG': ['App-SG', '203.0.113.11', 'SG'],
        'Relay-SG1': ['Relay-SG1', '203.0.113.12', 'SG'],
        'Relay-SG2': ['Relay-SG2', '203.0.113.13', 'SG'],
        'Relay-SG3': ['Relay-SG3', '203.0.113.14', 'SG'],
        'Relay-HK': ['Relay-HK', '203.0.113.15', 'HK'],
        'Relay-DE': ['Relay-DE', '203.0.113.16', 'DE'],
        'App-ES': ['App-ES', '203.0.113.17', 'ES'],
    },
    'pairs': {
        'Relay-HK|Relay-SG1': {'rtt': 33.2, 'loss': 0.0, 'jitter': 0.4},
        'Relay-HK|Relay-SG2': {'rtt': 31.3, 'loss': 0.0, 'jitter': 0.0},
        'Relay-HK|Relay-SG3': {'rtt': 40.3, 'loss': 0.0, 'jitter': 0.0},
        'Relay-HK|Relay-DE': {'rtt': 188.6, 'loss': 0.0, 'jitter': 0.0},
        'Relay-HK|App-SG': {'rtt': 32.9, 'loss': 0.0, 'jitter': 0.2},
        'Relay-HK|App-ES': {'rtt': 194.0, 'loss': 0.0, 'jitter': 0.1},
        'Relay-SG1|Relay-SG2': {'rtt': 1.6, 'loss': 0.0, 'jitter': 0.1},
        'Relay-SG1|Relay-SG3': {'rtt': 1.3, 'loss': 0.0, 'jitter': 0.2},
        'Relay-SG1|Relay-DE': {'rtt': 162.7, 'loss': 0.0, 'jitter': 0.1},
        'Relay-SG1|App-SG': {'rtt': 1.1, 'loss': 0.0, 'jitter': 0.1},
        'Relay-SG1|App-ES': {'rtt': 169.8, 'loss': 0.0, 'jitter': 0.2},
        'Relay-SG2|Relay-SG3': {'rtt': 0.7, 'loss': 0.0, 'jitter': 0.0},
        'Relay-SG2|Relay-DE': {'rtt': 163.7, 'loss': 0.0, 'jitter': 0.3},
        'Relay-SG2|App-SG': {'rtt': 0.5, 'loss': 0.0, 'jitter': 0.0},
        'Relay-SG2|App-ES': {'rtt': 169.4, 'loss': 0.0, 'jitter': 0.1},
        'Relay-SG3|Relay-DE': {'rtt': 187.6, 'loss': 0.0, 'jitter': 0.0},
        'Relay-SG3|App-SG': {'rtt': 0.7, 'loss': 0.0, 'jitter': 0.1},
        'Relay-SG3|App-ES': {'rtt': 188.5, 'loss': 0.0, 'jitter': 0.1},
        'Relay-DE|App-SG': {'rtt': 184.4, 'loss': 0.0, 'jitter': 0.1},
        'Relay-DE|App-ES': {'rtt': 47.2, 'loss': 0.0, 'jitter': 0.2},
        'App-SG|App-ES': {'rtt': 250.2, 'loss': 0.0, 'jitter': 0.1},
    },
}


def _keys(sid):
    return {
        'publicKey': f'{sid[:8]}wg'.ljust(43, 'A') + '=',
        'controlKey': f'{sid[:8]}ct'.ljust(43, 'B') + '=',
        'signKey': f'{sid[:8]}sg'.ljust(43, 'C') + '=',
        'e2eKey': f'{sid[:8]}ee'.ljust(43, 'D') + '=',
    }


def make_member(sid, role='member', **changes):
    m = model.new_member(sid, model.default_settings())
    m['role'] = role
    if role == 'relay':
        m['entry']['candidates'] = []
    for k, v in changes.items():
        m[k] = {**m[k], **v} if isinstance(m.get(k), dict) and isinstance(v, dict) else v
    return m


def make_doc(*members, mode='custom', pairs=(), direct=(), limits=(), auto_links=None):
    d = model.empty_doc()
    d['members'] = [copy.deepcopy(m) for m in members]
    d['relayLinks'] = {'mode': mode, 'pairs': [copy.deepcopy(p) for p in pairs]}
    d['directLinks'] = [copy.deepcopy(x) for x in direct]
    d['limits'] = [copy.deepcopy(l) for l in limits]
    if auto_links:
        d['settings']['autoLinks'].update(auto_links)
    return model.allocate(d)


def matrix_facts(names, *, regions=None, fronts=None):
    regions = regions or {}
    fronts = fronts or {}
    nodes = MATRIX['nodes']
    out = {}
    for i, name in enumerate(names):
        info = nodes.get(name, [name, f'203.0.113.{i + 1}', 'SG'])
        ip = info[1] if len(info) > 1 and info[1] else f'203.0.113.{i + 1}'
        reg = regions.get(name, info[2] if len(info) > 2 else 'SG')
        out[name] = {
            'name': name,
            'platform': 'linux',
            'ipv4': ip,
            'region': reg,
            'localPrefixes': [],
            'front': [{'serverId': ex} for ex in fronts.get(name, ())],
            'tls': {'active': {'fingerprint': 'ab' * 32}},
            'configSchemas': compat.CONFIG_SCHEMA,
            **_keys(name),
        }
    return out


def matrix_probes(names):
    """Build node status map containing probe measurements from MATRIX for the given node names."""
    pairs = MATRIX['pairs']
    statuses = {n: {'status': {'probes': [], 'routing': {'mode': 'dynamic', 'links': [], 'routes': []}}} for n in names}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            p = pairs.get(f'{a}|{b}') or pairs.get(f'{b}|{a}')
            if not p:
                continue
            statuses[a]['status']['probes'].append({
                'peer': b,
                'rttMs': p['rtt'],
                'jitterMs': p['jitter'],
                'loss': p['loss'],
                'method': 'udp',
                'at': 1700000000000,
            })
    return statuses


def _link(a, b, *, auto=False, bias=0):
    return {'a': a, 'b': b, 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': bias, **({'origin': 'auto'} if auto else {})}


def _up(statuses, a, b, rtt, bias=0):
    statuses.setdefault(a, {'status': {'probes': [], 'routing': {'mode': 'dynamic', 'links': [], 'routes': []}}})
    # What the node reports: its cost has the link's bias in.
    statuses[a]['status']['routing']['links'].append({'peer': b, 'state': 'up', 'rttMs': rtt, 'jitterMs': 0.0, 'loss': 0.0, 'cost': rtt + bias})


def test_a_measured_link_counts_its_bias_once():
    d = make_doc(make_member('R1', 'relay'), make_member('R2', 'relay'), pairs=[_link('R1', 'R2', bias=40)])
    f = matrix_facts(['R1', 'R2'])
    statuses = {}
    _up(statuses, 'R1', 'R2', 100.0, bias=40)
    real, _ = mesh_autolinks.collect_measurements(d, f, statuses)
    assert mesh_autolinks.edge_cost('R1', 'R2', d, f, real, {}) == (140.0, 'real')


def test_an_auto_link_dropped_this_round_is_out_of_the_next_ones_check():
    """R1-R3 (auto, deleted by hand) goes this round; R2-R3 (auto) is then what keeps R2 and R4 off
    the slow R4-R3, and is no longer idle."""
    names = ['R1', 'R2', 'R3', 'R4']
    d = make_doc(*(make_member(n, 'relay') for n in names),
                 pairs=[_link('R1', 'R2'), _link('R2', 'R4'), _link('R3', 'R4'), _link('R1', 'R3', auto=True), _link('R2', 'R3', auto=True)])
    f = matrix_facts(names)
    statuses = {}
    for a, b, rtt in (('R1', 'R2', 1.0), ('R2', 'R4', 1.0), ('R4', 'R3', 300.0), ('R1', 'R3', 50.0), ('R2', 'R3', 50.0)):
        _up(statuses, a, b, rtt)
    now = 1_800_000_000_000
    state = {'suppressed': {'R1|R3': now + 86_400_000}, 'idleSince': {'R2|R3': now - 60_000}}
    out = mesh_autolinks.optimize(d, f, statuses, state=state, now_ms=now)
    assert [x['key'] for x in out['dropped']] == ['R1|R3']
    assert 'R2|R3' not in out['state']['idleSince']


# ----- 1. App-ES with only a distant upstream (Relay-DE) gets a nearby SG relay -----
def test_remote_member_with_only_distant_upstream_gets_nearby_sg_relay():
    relays = ['Relay-HK', 'Relay-SG1', 'Relay-SG2', 'Relay-DE']
    members = ['App-SG', 'App-ES']
    all_nodes = relays + members
    f = matrix_facts(all_nodes)
    statuses = matrix_probes(all_nodes)

    # App-SG connects to Relay-HK; App-ES only lists Relay-DE (far away in Europe).
    ks = make_member('App-SG', 'member', entry={'mode': 'upstreams', 'candidates': [
        {'id': 'c1', 'relay': 'Relay-HK', 'transport': 'udp'},
    ], 'autoReturn': True})
    jc = make_member('App-ES', 'member', entry={'mode': 'upstreams', 'candidates': [
        {'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp'},
    ], 'autoReturn': True})
    pairs = [
        {'a': 'Relay-HK', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        {'a': 'Relay-SG1', 'b': 'Relay-HK', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        {'a': 'Relay-SG1', 'b': 'Relay-SG2', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        {'a': 'Relay-SG2', 'b': 'Relay-HK', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
    ]
    d = make_doc(*(make_member(r, 'relay') for r in relays), ks, jc, mode='custom', pairs=pairs)

    real_costs, probe_costs = mesh_autolinks.collect_measurements(d, f, statuses)
    g_before = mesh_autolinks.build_graph(d, f, real_costs, probe_costs)
    t_before = mesh_autolinks.shortest_table(g_before, set(relays))
    before_ks_jc = t_before[('App-SG', 'App-ES')][0]
    assert before_ks_jc > 260.0

    # Hold rounds = 3: first two rounds only build streak, 3rd round applies.
    state = None
    for round_no in (1, 2):
        res = mesh_autolinks.optimize(d, f, statuses, state=state, now_ms=round_no * 300_000)
        assert not res['changed']
        assert res['suggestions'] and res['suggestions'][0]['rounds'] == round_no
        state = res['state']

    res = mesh_autolinks.optimize(d, f, statuses, state=state, now_ms=3 * 300_000)
    assert res['changed']
    new_doc = res['doc']
    jc_after = next(m for m in new_doc['members'] if m['serverId'] == 'App-ES')
    auto_cands = [c for c in jc_after['entry']['candidates'] if c.get('origin') == 'auto']
    assert any(c['relay'] in ('Relay-SG1', 'Relay-SG2') for c in auto_cands)
    for c in auto_cands:
        assert 'App-ES' in c['reason'] and 'ms' in c['reason']
        assert c['since'] == 3 * 300_000

    g_after = mesh_autolinks.build_graph(new_doc, f, real_costs, probe_costs)
    t_after = mesh_autolinks.shortest_table(g_after, set(relays))
    after_ks_jc = t_after[('App-SG', 'App-ES')][0]
    assert after_ks_jc < 180.0
    assert not model.validate(new_doc, f, applying=True, applied=d)

    # Each change lists the node pairs it makes faster, the most first, each pair once whichever way.
    for item in res['lastChange']['added']:
        gains = item['pairs']
        assert item['count'] == len(gains) >= 1
        assert all(g['after'] < g['before'] for g in gains)
        cuts = [g['before'] - g['after'] for g in gains]
        assert all(x >= y - 0.1 for x, y in zip(cuts, cuts[1:]))
        assert len({frozenset((g['src'], g['dst'])) for g in gains}) == len(gains)
        assert item['reason'].endswith(f'等 {item["count"]} 对') if item['count'] > 1 else '等' not in item['reason']
    # The next round records what each automatic link gives; the lines page reads that until the round after,
    # whatever the measurements do in between.
    nxt = mesh_autolinks.optimize(new_doc, f, statuses, state=res['state'], now_ms=4 * 300_000)
    stored = copy.deepcopy(nxt['doc'])
    stored['_autoLinksState'] = nxt['state']
    view = mesh_autolinks.view_of(stored, f, statuses, now_ms=4 * 300_000 + 1000)
    jc_gain = view['benefits'][mesh_autolinks.pair_key('App-ES', auto_cands[0]['relay'])]
    assert jc_gain['kind'] == 'entry' and jc_gain['count'] >= 1
    assert all('App-ES' in (g['src'], g['dst']) for g in jc_gain['pairs'])
    assert set(view['benefits']) == {mesh_autolinks.pair_key(x['a'], x['b']) for x in res['lastChange']['added']}
    assert (view['idleSince'], view['measuredAt'], view['holdRounds'], view['dropAfterS']) == ({}, 4 * 300_000, 3, 3600)
    assert view['suggestions'] == nxt['suggestions']
    assert mesh_autolinks.view_of(stored, f, matrix_probes(relays), now_ms=4 * 300_000 + 5000) == view


# ----- 2. Connection limits, fronts, site relays and member.direct=False -----
def test_connection_limits_and_front_rules_never_violated():
    nodes = ['Relay-HK', 'Relay-SG1', 'Relay-DE', 'App-ES']
    f = matrix_facts(nodes, regions={'Relay-HK': 'CN', 'Relay-SG1': 'SG', 'Relay-DE': 'DE', 'App-ES': 'US'})
    statuses = matrix_probes(nodes)

    jc = make_member('App-ES', 'member', entry={'mode': 'upstreams', 'candidates': [
        {'id': 'c1', 'relay': 'Relay-DE', 'transport': 'tcp'},
    ], 'autoReturn': True})
    r_hk = make_member('Relay-HK', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0})
    r_sg = make_member('Relay-SG1', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0})
    r_de = make_member('Relay-DE', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0})

    # Limit 1: CN <-> non-CN only manual -> Relay-HK must never get any automatic cross-border link.
    limit_manual = {
        'id': 'lim1', 'name': 'CN跨界仅手动', 'on': True,
        'a': {'kind': 'region', 'regions': ['CN']},
        'b': {'kind': 'region', 'regions': ['CN'], 'not': True},
        'relayEnd': True, 'transports': ['tcp'], 'via': None, 'manualOnly': True,
    }
    base_pairs = [
        {'a': 'Relay-HK', 'b': 'Relay-DE', 'transport': 'tcp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        {'a': 'Relay-SG1', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
    ]
    d = make_doc(r_hk, r_sg, r_de, jc, mode='custom',
                 pairs=base_pairs,
                 limits=[limit_manual], auto_links={'holdRounds': 1})
    res = mesh_autolinks.optimize(d, f, statuses, now_ms=1000)
    for item in res['added']:
        assert 'Relay-HK' not in (item['a'], item['b'])

    # Limit 2: SG <-> US allows automatic, but only over TCP -> auto upstream to Relay-SG1 must pick TCP, not UDP.
    limit_tcp = {
        'id': 'lim2', 'name': 'SG到US仅TCP', 'on': True,
        'a': {'kind': 'region', 'regions': ['SG']},
        'b': {'kind': 'region', 'regions': ['US']},
        'relayEnd': True, 'transports': ['tcp'], 'via': None, 'manualOnly': False,
    }
    d2 = make_doc(r_hk, r_sg, r_de, jc, mode='custom',
                  pairs=base_pairs,
                  limits=[limit_manual, limit_tcp], auto_links={'holdRounds': 1})
    res2 = mesh_autolinks.optimize(d2, f, statuses, now_ms=2000)
    assert res2['changed']
    jc_cands = next(m for m in res2['doc']['members'] if m['serverId'] == 'App-ES')['entry']['candidates']
    auto_sg = next(c for c in jc_cands if c['relay'] == 'Relay-SG1')
    assert auto_sg['origin'] == 'auto' and auto_sg['transport'] == 'tcp'
    assert not model.validate(res2['doc'], f, applying=True, applied=d2)

    # Front machine only links to its own exits; member with direct=False never gets auto upstreams.
    f_front = matrix_facts(nodes, fronts={'Relay-HK': ['Relay-SG1']})
    jc_nodirect = copy.deepcopy(jc)
    jc_nodirect['direct'] = False
    d3 = make_doc(r_hk, r_sg, r_de, jc_nodirect, mode='custom',
                  pairs=[{'a': 'Relay-SG1', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0}],
                  auto_links={'holdRounds': 1})
    assert mesh_autolinks.candidate_spec('Relay-HK', 'Relay-DE', d3, {m['serverId']: m for m in d3['members']}, f_front) is None
    assert mesh_autolinks.candidate_spec('App-ES', 'Relay-SG1', d3, {m['serverId']: m for m in d3['members']}, f_front) is None


# ----- 3. Per-node budget (perNode) -----
def test_per_node_budget_caps_auto_links_on_each_node():
    nodes = ['Relay-SG1', 'Relay-SG2', 'Relay-SG3', 'Relay-HK', 'Relay-DE', 'App-ES']
    f = matrix_facts(nodes)
    statuses = matrix_probes(nodes)
    relays = ['Relay-SG1', 'Relay-SG2', 'Relay-SG3', 'Relay-HK', 'Relay-DE']
    # Linear chain Relay-DE - Relay-HK - Relay-SG3 - Relay-SG2 - Relay-SG1
    pairs = [
        {'a': 'Relay-HK', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        {'a': 'Relay-SG3', 'b': 'Relay-HK', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        {'a': 'Relay-SG2', 'b': 'Relay-SG3', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        {'a': 'Relay-SG1', 'b': 'Relay-SG2', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
    ]
    jc = make_member('App-ES', 'member', entry={'mode': 'upstreams', 'candidates': [
        {'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp'},
    ], 'autoReturn': True})
    d = make_doc(*(make_member(r, 'relay') for r in relays), jc, mode='custom', pairs=pairs,
                 auto_links={'perNode': 1, 'holdRounds': 1})
    res = mesh_autolinks.optimize(d, f, statuses, now_ms=1000)
    assert res['changed']
    auto_count = {}
    for p in res['doc']['relayLinks']['pairs']:
        if p.get('origin') == 'auto':
            auto_count[p['a']] = auto_count.get(p['a'], 0) + 1
            auto_count[p['b']] = auto_count.get(p['b'], 0) + 1
    for m in res['doc']['members']:
        if m['role'] == 'member':
            for c in m['entry']['candidates']:
                if c.get('origin') == 'auto':
                    auto_count[m['serverId']] = auto_count.get(m['serverId'], 0) + 1
                    auto_count[c['relay']] = auto_count.get(c['relay'], 0) + 1
    assert all(cnt <= 1 for cnt in auto_count.values())
    assert auto_count.get('App-ES', 0) == 1


# ----- 4. Damping: threshold jitter (acceptance case 5), disappointment (24h), drop after 1h, manual kept -----
def test_damping_near_threshold_disappointment_and_drop_after_hour():
    nodes = ['Relay-SG1', 'Relay-DE', 'App-ES']
    f = matrix_facts(nodes)
    jc = make_member('App-ES', 'member', entry={'mode': 'upstreams', 'candidates': [
        {'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp'},
    ], 'autoReturn': True})
    pairs = [{'a': 'Relay-SG1', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0}]
    d = make_doc(make_member('Relay-SG1', 'relay'), make_member('Relay-DE', 'relay'), jc, mode='custom', pairs=pairs)

    # Baseline Relay-SG1 -> App-ES via Relay-DE is 200ms (100 + 100).
    # 85% of 200ms is 170ms (30ms improvement). Probe fluctuates between 165ms (good) and 175ms (not enough: >170ms).
    def status_with(probe_rtt, real_rtt=None):
        st = {
            'Relay-SG1': {'status': {'probes': [{'peer': 'App-ES', 'rttMs': probe_rtt, 'jitterMs': 0.0, 'loss': 0.0, 'method': 'udp', 'at': 1}],
                                     'routing': {'mode': 'dynamic', 'links': [{'peer': 'Relay-DE', 'state': 'up', 'rttMs': 100.0, 'jitterMs': 0.0, 'loss': 0.0, 'cost': 100.0, 'penalty': 0}], 'routes': []}}},
            'Relay-DE': {'status': {'probes': [], 'routing': {'mode': 'dynamic', 'links': [{'peer': 'Relay-SG1', 'state': 'up', 'rttMs': 100.0, 'jitterMs': 0.0, 'loss': 0.0, 'cost': 100.0, 'penalty': 0}], 'routes': []}}},
            'App-ES': {'status': {'probes': [{'peer': 'Relay-SG1', 'rttMs': probe_rtt, 'jitterMs': 0.0, 'loss': 0.0, 'method': 'udp', 'at': 1}],
                                  'entry': {'candidate': 'c1', 'relay': 'Relay-DE', 'transport': 'udp', 'since': 1, 'switches': 0,
                                            'candidates': [{'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp', 'state': 'active', 'rttMs': 100.0, 'timeoutRate': 0.0}]
                                            + ([{'id': 'c2', 'relay': 'Relay-SG1', 'transport': 'udp', 'state': 'ready', 'rttMs': real_rtt, 'timeoutRate': 0.0}] if real_rtt is not None else [])}}},
        }
        return st

    # Fluctuating 165, 165, 175, 165, 165, 175 must never build the link (streak resets on 175).
    state = None
    for i, rtt in enumerate([165.0, 165.0, 175.0, 165.0, 165.0, 175.0]):
        res = mesh_autolinks.optimize(d, f, status_with(rtt), state=state, now_ms=(i + 1) * 60_000)
        assert not res['changed']
        state = res['state']

    # Now 3 consecutive good rounds (165ms) -> link is built on the 3rd round.
    for i in range(3):
        res = mesh_autolinks.optimize(d, f, status_with(165.0), state=state, now_ms=(10 + i) * 60_000)
        state = res['state']
    assert res['changed']
    built_doc = res['doc']

    # Once built, real measurement reports 190ms (worse than 85% threshold) while probe still says 165ms:
    # marked disappointed for 24h and dropped immediately.
    dis_res = mesh_autolinks.optimize(built_doc, f, status_with(165.0, real_rtt=190.0), state=state, now_ms=20 * 60_000)
    assert dis_res['changed']
    assert dis_res['dropped'][0]['why'] == '实测不划算'
    state = dis_res['state']
    # Within 24h, even 3 good probe rounds do not rebuild it.
    for i in range(3):
        retry = mesh_autolinks.optimize(dis_res['doc'], f, status_with(120.0), state=state, now_ms=30 * 60_000 + i * 60_000)
        assert not retry['changed']
        state = retry['state']

    # Test 1-hour idle drop & manual links never dropped:
    # Build again after 24h + 1ms.
    after_24h = 20 * 60_000 + mesh_autolinks.DISAPPOINT_TTL_MS + 1000
    for i in range(3):
        res2 = mesh_autolinks.optimize(dis_res['doc'], f, status_with(120.0), state=state, now_ms=after_24h + i * 60_000)
        state = res2['state']
    assert res2['changed']
    doc_with_auto = res2['doc']

    # Real measurement is 120ms, and suppose direct baseline also becomes 125ms so auto link has 0 gain (both probe & real say 120ms vs 125ms):
    def status_no_gain(t_ms):
        st = status_with(120.0, real_rtt=120.0)
        for sid in ('Relay-SG1', 'Relay-DE'):
            st[sid]['status']['routing']['links'][0].update(rttMs=25.0, cost=25.0)
        return st

    t0 = after_24h + 10 * 60_000
    # At t0 and t0 + 30 min (< 3600s): not dropped yet.
    r_early = mesh_autolinks.optimize(doc_with_auto, f, status_no_gain(t0), state=state, now_ms=t0)
    assert not r_early['changed']
    r_mid = mesh_autolinks.optimize(doc_with_auto, f, status_no_gain(t0 + 1800_000), state=r_early['state'], now_ms=t0 + 1800_000)
    assert not r_mid['changed']
    # At t0 + 3600s: auto link is dropped, while manual relay link Relay-SG1 <-> Relay-DE and manual upstream Relay-DE stay.
    r_drop = mesh_autolinks.optimize(doc_with_auto, f, status_no_gain(t0 + 3600_000), state=r_mid['state'], now_ms=t0 + 3600_000)
    assert r_drop['changed']
    assert r_drop['dropped'][0]['why'] == '连续无改善'
    assert len(r_drop['doc']['relayLinks']['pairs']) == 1
    jc_cands = next(m for m in r_drop['doc']['members'] if m['serverId'] == 'App-ES')['entry']['candidates']
    assert [c['relay'] for c in jc_cands] == ['Relay-DE']


# ----- 5. User deletion suppresses rebuilding for 7 days; enabled=False only returns suggestions -----
def test_user_deleted_auto_link_suppressed_for_7_days_and_disabled_mode():
    nodes = ['Relay-HK', 'Relay-SG1', 'Relay-DE', 'App-SG', 'App-ES']
    f = matrix_facts(nodes)
    statuses = matrix_probes(nodes)
    ks = make_member('App-SG', 'member', entry={'mode': 'upstreams', 'candidates': [
        {'id': 'c1', 'relay': 'Relay-SG1', 'transport': 'udp'},
    ], 'autoReturn': True})
    jc = make_member('App-ES', 'member', entry={'mode': 'upstreams', 'candidates': [
        {'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp'},
    ], 'autoReturn': True})
    pairs = [
        {'a': 'Relay-HK', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        {'a': 'Relay-SG1', 'b': 'Relay-HK', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
    ]
    d = make_doc(make_member('Relay-HK', 'relay'), make_member('Relay-SG1', 'relay'), make_member('Relay-DE', 'relay'),
                 ks, jc, mode='custom', pairs=pairs, auto_links={'holdRounds': 1})

    # enabled=False: calculates suggestions, does not modify doc.
    d_disabled = copy.deepcopy(d)
    d_disabled['settings']['autoLinks']['enabled'] = False
    res_off = mesh_autolinks.optimize(d_disabled, f, statuses, now_ms=1000)
    assert not res_off['changed']
    assert len(res_off['suggestions']) >= 1
    assert any(s['a'] == 'Relay-SG1' and s['b'] == 'App-ES' or s['a'] == 'App-ES' and s['b'] == 'Relay-SG1' for s in res_off['suggestions'])

    # enabled=True: builds Relay-SG1 <-> App-ES.
    res_on = mesh_autolinks.optimize(d, f, statuses, now_ms=2000)
    assert res_on['changed']
    with_auto = res_on['doc']

    # User deletes the auto candidate on App-ES.
    user_edited = copy.deepcopy(with_auto)
    for m in user_edited['members']:
        if m['serverId'] == 'App-ES':
            m['entry']['candidates'] = [c for c in m['entry']['candidates'] if c.get('origin') != 'auto']
    st = mesh_autolinks.record_user_deletions(with_auto, user_edited, with_auto.get('_autoLinksState'), now_ms=10_000)
    key = mesh_autolinks.pair_key('Relay-SG1', 'App-ES')
    assert key in st['suppressed']

    # 6 days later: still suppressed.
    six_days = 10_000 + 6 * 86400_000
    res_6d = mesh_autolinks.optimize(user_edited, f, statuses, state=st, now_ms=six_days)
    assert not any(x['key'] == key for x in res_6d['added'])

    # 7 days + 1s later: suppression expired, can be built again.
    after_7d = 10_000 + 7 * 86400_000 + 1000
    res_7d = mesh_autolinks.optimize(user_edited, f, statuses, state=st, now_ms=after_7d)
    assert any(x['key'] == key for x in res_7d['added'])


# ----- 6. Works in all three relayLinks modes (full / custom / auto) and generates probe targets -----
def test_works_in_full_custom_and_auto_modes_and_generates_probe_targets():
    nodes = ['Relay-SG1', 'Relay-DE', 'App-ES']
    f = matrix_facts(nodes)
    statuses = matrix_probes(nodes)
    for mode in ('full', 'custom', 'auto'):
        jc = make_member('App-ES', 'member', entry={'mode': 'upstreams', 'candidates': [
            {'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp'},
        ], 'autoReturn': True})
        pairs = [{'a': 'Relay-SG1', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0}] if mode == 'custom' else []
        d = make_doc(make_member('Relay-SG1', 'relay'), make_member('Relay-DE', 'relay'), jc,
                     mode=mode, pairs=pairs, auto_links={'holdRounds': 1})
        targets = mesh_autolinks.probe_targets(d, f)
        assert any(t['peer'] == 'Relay-SG1' for t in targets['App-ES']['targets'])
        res = mesh_autolinks.optimize(d, f, statuses, now_ms=5000)
        assert res['changed'], mode
        # Even in full/auto relayLinks mode, relay_pairs includes both base relay pairs and any auto pairs.
        rp = relay_pairs(res['doc'], f)
        assert any({p['a'], p['b']} == {'Relay-SG1', 'Relay-DE'} for p in rp)


# ----- 7. Coordinator integration: auto revisions, draft alignment without leaking draft, 10-min rate limit -----
async def test_auto_revision_does_not_leak_draft_preserves_dirty_draft_and_rate_limits(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    r3 = await server(e, 'Singapore', ipv4='203.0.113.5')
    ids['r3'] = r3
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['r2'], ids['r3'], ids['m1'], ids['m2']]})
    doc = {k: added[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules')}
    doc['relayLinks'] = {
        'mode': 'custom',
        'pairs': [
            {'a': ids['r1'], 'b': ids['r2'], 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
            {'a': ids['r1'], 'b': ids['r3'], 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        ],
    }
    doc['settings']['autoLinks'] = {'enabled': True, 'perNode': 4, 'gainRatio': 0.85, 'gainMs': 20, 'holdRounds': 1, 'dropAfterS': 3600}
    for m in doc['members']:
        if m['serverId'] in (ids['r1'], ids['r2'], ids['r3']):
            m['role'] = 'relay'
            m['entry']['candidates'] = []
        elif m['serverId'] == ids['m1']:
            m['entry'] = {'mode': 'upstreams', 'candidates': [{'id': 'c1', 'relay': ids['r3'], 'transport': 'udp'}], 'autoReturn': True}
        elif m['serverId'] == ids['m2']:
            m['entry'] = {'mode': 'upstreams', 'candidates': [{'id': 'c1', 'relay': ids['r2'], 'transport': 'udp'}], 'autoReturn': True}
    await api(e, 'PUT', '/api/servers/mesh/draft', doc)
    await api(e, 'POST', '/api/servers/mesh/apply', {})
    await settled(e)

    # Now user edits the draft (changes mtu to 1360), without applying!
    view0 = await api(e, 'GET', '/api/servers/mesh')
    dirty_doc = {k: view0[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules', 'lineRules', 'exitGroups', 'limits')}
    dirty_doc['settings']['mtu'] = 1360
    await api(e, 'PUT', '/api/servers/mesh/draft', dirty_doc)
    await settled(e)

    # Report high latency on r1<->r2 (260ms) and fast probe on r3<->m2 (25ms).
    mesh = e.servers.mesh
    await mesh.report(ids['r1'], {
        'startedAt': 1, 'appliedRevision': 1, 'ready': True, 'configSchemas': list(compat.CONFIG_SCHEMA),
        'routing': {'mode': 'dynamic', 'links': [
            {'peer': ids['r2'], 'state': 'up', 'rttMs': 260.0, 'jitterMs': 0.0, 'loss': 0.0, 'cost': 260.0, 'penalty': 0},
            {'peer': ids['r3'], 'state': 'up', 'rttMs': 30.0, 'jitterMs': 0.0, 'loss': 0.0, 'cost': 30.0, 'penalty': 0},
        ], 'routes': []},
    })
    await mesh.report(ids['m1'], {
        'startedAt': 1, 'appliedRevision': 1, 'ready': True, 'configSchemas': list(compat.CONFIG_SCHEMA),
        'entry': {'candidate': 'c1', 'relay': ids['r3'], 'transport': 'udp', 'since': 1, 'switches': 0,
                  'candidates': [{'id': 'c1', 'relay': ids['r3'], 'transport': 'udp', 'state': 'active', 'rttMs': 15.0, 'timeoutRate': 0.0}]},
    })
    await mesh.report(ids['m2'], {
        'startedAt': 1, 'appliedRevision': 1, 'ready': True, 'configSchemas': list(compat.CONFIG_SCHEMA),
        'entry': {'candidate': 'c1', 'relay': ids['r2'], 'transport': 'udp', 'since': 1, 'switches': 0,
                  'candidates': [{'id': 'c1', 'relay': ids['r2'], 'transport': 'udp', 'state': 'active', 'rttMs': 20.0, 'timeoutRate': 0.0}]},
        'probes': [{'peer': ids['r3'], 'rttMs': 25.0, 'jitterMs': 1.0, 'loss': 0.0, 'method': 'udp', 'at': 1000}],
    })
    await settled(e)

    # Trigger automatic optimization at t = 1_000_000.
    res1 = await mesh.check_auto_links(OWNER, now=1_000_000)
    assert res1 and res1['changed']
    await settled(e)

    # Verify:
    # 1. Applied document got the new auto link, but did NOT leak draft's mtu=1360!
    applied_after = await mesh._load(OWNER, 'applied')
    assert applied_after['settings']['mtu'] != 1360
    m2_applied = next(m for m in applied_after['members'] if m['serverId'] == ids['m2'])
    assert any(c.get('origin') == 'auto' and c['relay'] == ids['r3'] for c in m2_applied['entry']['candidates'])

    # 2. Draft still preserves user's unapplied mtu=1360 AND has the new auto link aligned into it!
    view1 = await api(e, 'GET', '/api/servers/mesh')
    assert view1['settings']['mtu'] == 1360
    assert view1['draft']['dirty'] is True
    m2_draft = next(m for m in view1['members'] if m['serverId'] == ids['m2'])
    assert any(c.get('origin') == 'auto' and c['relay'] == ids['r3'] for c in m2_draft['entry']['candidates'])
    assert view1['autoLinks']['lastChange']['reason'].startswith('自动：')

    # 3. Revision history records "自动：" and the reason.
    revs = (await api(e, 'GET', '/api/servers/mesh/revisions'))['revisions']
    assert revs[0]['reason'].startswith('自动：')

    # 4. Rate limit: within 10 minutes (e.g. +5 minutes), another candidate ready to add is rate-limited.
    await mesh.report(ids['r2'], {
        'startedAt': 1, 'appliedRevision': 2, 'ready': True, 'configSchemas': list(compat.CONFIG_SCHEMA),
        'probes': [{'peer': ids['r3'], 'rttMs': 20.0, 'jitterMs': 0.0, 'loss': 0.0, 'method': 'udp', 'at': 2000}],
    })
    await settled(e)
    res_limited = await mesh.check_auto_links(OWNER, now=1_000_000 + 5 * 60_000)
    assert res_limited and res_limited.get('rateLimited') is True and not res_limited['changed']

    # After 10 minutes, the automatic revision is allowed to proceed.
    res_allowed = await mesh.check_auto_links(OWNER, now=1_000_000 + 10 * 60_000 + 1000)
    assert res_allowed and res_allowed['changed']

    # 5. If validation fails (e.g. outdated configSchemas), check_auto_links does not issue a revision and records the reason.
    await mesh.report(ids['r1'], {
        'startedAt': 1, 'appliedRevision': 3, 'ready': True, 'configSchemas': [1, 1],
        'routing': {'mode': 'dynamic', 'links': [
            {'peer': ids['r2'], 'state': 'up', 'rttMs': 260.0, 'jitterMs': 0.0, 'loss': 0.0, 'cost': 260.0, 'penalty': 0},
            {'peer': ids['r3'], 'state': 'up', 'rttMs': 30.0, 'jitterMs': 0.0, 'loss': 0.0, 'cost': 30.0, 'penalty': 0},
        ], 'routes': []},
        'probes': [{'peer': ids['m1'], 'rttMs': 5.0, 'jitterMs': 0.0, 'loss': 0.0, 'method': 'udp', 'at': 3000}],
    })
    await settled(e)
    res_invalid = await mesh.check_auto_links(OWNER, now=1_000_000 + 25 * 60_000)
    assert res_invalid and not res_invalid['changed'] and res_invalid.get('errors')
    assert '自动版本校验未通过' in res_invalid['lastChange']['reason']


# ----- 8. A mesh from before auto links: nothing changes by itself -----
def test_a_stored_mesh_keeps_its_links_its_entries_and_its_configs():
    nodes = ['Relay-SG1', 'Relay-DE', 'App-ES']
    f = matrix_facts(nodes)
    for i, sid in enumerate(nodes):
        f[sid]['features'] = frozenset({'mesh.routing.members'})
        f[sid].update({k: base64.b64encode(bytes([i * 4 + j + 1]) * 32).decode() for j, k in enumerate(('publicKey', 'controlKey', 'signKey', 'e2eKey'))})
    statuses = matrix_probes(nodes)
    jc = make_member('App-ES', 'member', entry={'mode': 'upstreams', 'candidates': [
        {'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp'}], 'autoReturn': True})
    d = make_doc(make_member('Relay-SG1', 'relay'), make_member('Relay-DE', 'relay'), jc,
                 pairs=[{'a': 'Relay-SG1', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0}], auto_links={'holdRounds': 1})
    stored = copy.deepcopy(d)
    del stored['settings']['autoLinks']
    stored = model.upgrade(stored)
    assert stored['settings']['autoLinks']['enabled'] is False and not mesh_autolinks.enabled(stored)
    assert not mesh_autolinks.optimize(stored, f, statuses, now_ms=5000)['changed']
    assert all('probe' not in c for c in build(stored, f, 'net', 1).values())
    # Turned on: probes and links.
    assert mesh_autolinks.enabled(d) and any('probe' in c for c in build(d, f, 'net', 1).values())
    assert mesh_autolinks.optimize(d, f, statuses, now_ms=5000)['changed']
    # A member still on one entry is no candidate: another upstream would carry nothing.
    for mode in ('fixed', 'failover'):
        d['members'][2]['entry']['mode'] = mode
        res = mesh_autolinks.optimize(d, f, statuses, now_ms=5000)
        assert not [a for a in res['added'] if 'App-ES' in (a['a'], a['b'])], mode
        assert all(t['peer'] not in ('Relay-SG1',) for t in mesh_autolinks.probe_targets(d, f).get('App-ES', {}).get('targets', []))


async def test_auto_links_off_takes_no_round_and_writes_nothing(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]})
    state = await api(e, 'GET', '/api/servers/mesh')
    await api(e, 'PUT', '/api/servers/mesh/draft', {'settings': {**state['settings'], 'autoLinks': {**state['settings']['autoLinks'], 'enabled': False}}})
    await api(e, 'POST', '/api/servers/mesh/apply', {})
    await settled(e)
    mesh = e.servers.mesh
    rev_before = (await api(e, 'GET', '/api/servers/mesh/revisions'))['revisions']
    assert await mesh.check_auto_links(OWNER, now=1_000_000) is None
    assert (await api(e, 'GET', '/api/servers/mesh/revisions'))['revisions'] == rev_before
