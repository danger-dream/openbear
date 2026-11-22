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
from app.webhooks.repository import many
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

    # Held 30 minutes (holdS): watched from 5 minutes on, still waiting at 20, built at 35.
    state = None
    for t in (300_000, 1_200_000):
        res = mesh_autolinks.optimize(d, f, statuses, state=state, now_ms=t)
        assert not res['changed']
        assert res['suggestions'] and res['suggestions'][0]['heldMs'] == t - 300_000 and not res['suggestions'][0]['ready']
        state = res['state']

    res = mesh_autolinks.optimize(d, f, statuses, state=state, now_ms=2_100_000)
    assert res['changed']
    new_doc = res['doc']
    jc_after = next(m for m in new_doc['members'] if m['serverId'] == 'App-ES')
    auto_cands = [c for c in jc_after['entry']['candidates'] if c.get('origin') == 'auto']
    assert any(c['relay'] in ('Relay-SG1', 'Relay-SG2') for c in auto_cands)
    for c in auto_cands:
        assert 'App-ES' in c['reason'] and '新建门槛' in c['reason']
        assert c['since'] == 2_100_000

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
    nxt = mesh_autolinks.optimize(new_doc, f, statuses, state=res['state'], now_ms=2_400_000)
    stored = copy.deepcopy(nxt['doc'])
    stored['_autoLinksState'] = nxt['state']
    view = mesh_autolinks.view_of(stored, f, statuses, now_ms=2_400_000 + 1000)
    jc_gain = view['benefits'][mesh_autolinks.pair_key('App-ES', auto_cands[0]['relay'])]
    assert jc_gain['kind'] == 'entry' and jc_gain['count'] >= 1
    assert all('App-ES' in (g['src'], g['dst']) for g in jc_gain['pairs'])
    assert set(view['benefits']) == {mesh_autolinks.pair_key(x['a'], x['b']) for x in res['lastChange']['added']}
    assert (view['idleSince'], view['measuredAt'], view['holdS'], view['dropAfterS'], view['keepMs'], view['buildMs']) == ({}, 2_400_000, 1800, 21600, 10, 15)
    assert view['suggestions'] == nxt['suggestions']
    assert mesh_autolinks.view_of(stored, f, matrix_probes(relays), now_ms=2_400_000 + 5000) == view


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
                 limits=[limit_manual], auto_links={'holdS': 0})
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
                  limits=[limit_manual, limit_tcp], auto_links={'holdS': 0})
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
                  auto_links={'holdS': 0})
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
                 auto_links={'perNode': 1, 'holdS': 0})
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


# ----- 4. Damping: threshold jitter, held 30 minutes, measured worse than probed (24h), idle 6 hours, manual kept -----
def _sg_de_es():
    """Relay-SG1 <-> Relay-DE (manual) and App-ES entering at Relay-DE: (doc, facts)."""
    jc = make_member('App-ES', 'member', entry={'mode': 'upstreams', 'candidates': [
        {'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp'},
    ], 'autoReturn': True})
    pairs = [{'a': 'Relay-SG1', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0}]
    doc = make_doc(make_member('Relay-SG1', 'relay'), make_member('Relay-DE', 'relay'), jc, mode='custom', pairs=pairs)
    return doc, matrix_facts(['Relay-SG1', 'Relay-DE', 'App-ES'])


def _sg_de_es_status(probe_rtt, real_rtt=None, line_rtt=100.0, started=None):
    """Relay-SG1 -> App-ES costs 2 x line_rtt through Relay-DE; probe_rtt is what the two probe of each
    other, real_rtt what App-ES measures over an auto entry at Relay-SG1. started: App-ES's startedAt."""
    line = {'state': 'up', 'rttMs': line_rtt, 'jitterMs': 0.0, 'loss': 0.0, 'cost': line_rtt, 'penalty': 0}
    return {
        'Relay-SG1': {'status': {'probes': [{'peer': 'App-ES', 'rttMs': probe_rtt, 'jitterMs': 0.0, 'loss': 0.0, 'method': 'udp', 'at': 1}],
                                 'routing': {'mode': 'dynamic', 'links': [{'peer': 'Relay-DE', **line}], 'routes': []}}},
        'Relay-DE': {'status': {'probes': [], 'routing': {'mode': 'dynamic', 'links': [{'peer': 'Relay-SG1', **line}], 'routes': []}}},
        'App-ES': {'status': {**({'startedAt': started} if started else {}),
                              'probes': [{'peer': 'Relay-SG1', 'rttMs': probe_rtt, 'jitterMs': 0.0, 'loss': 0.0, 'method': 'udp', 'at': 1}],
                              'entry': {'candidate': 'c1', 'relay': 'Relay-DE', 'transport': 'udp', 'since': 1, 'switches': 0,
                                        'candidates': [{'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp', 'state': 'active', 'rttMs': line_rtt, 'timeoutRate': 0.0}]
                                        + ([{'id': 'c2', 'relay': 'Relay-SG1', 'transport': 'udp', 'state': 'ready', 'rttMs': real_rtt, 'timeoutRate': 0.0}] if real_rtt is not None else [])}}},
    }


def test_damping_near_threshold_disappointment_and_drop_after_six_hours():
    d, f = _sg_de_es()
    minute = 60_000
    # Baseline Relay-SG1 -> App-ES via Relay-DE is 200ms (100 + 100). A probe of 180ms cuts 20ms (builds: 15ms);
    # 187ms cuts 13ms, 6.5% (neither 15ms nor 10%).
    status_with = _sg_de_es_status

    # 180 and 187 by turns every 10 minutes never hold 30 minutes: never built.
    state = None
    for i, rtt in enumerate([180.0, 180.0, 187.0, 180.0, 180.0, 187.0]):
        res = mesh_autolinks.optimize(d, f, status_with(rtt), state=state, now_ms=(i + 1) * 10 * minute)
        assert not res['changed']
        state = res['state']

    # 180 from 70 minutes on: built at 100, once it has held 30 minutes.
    for t in (70, 80, 90):
        res = mesh_autolinks.optimize(d, f, status_with(180.0), state=state, now_ms=t * minute)
        assert not res['changed']
        state = res['state']
    res = mesh_autolinks.optimize(d, f, status_with(180.0), state=state, now_ms=100 * minute)
    assert res['changed'] and '快 20ms，新建门槛 15ms' in res['added'][0]['reason']
    state, built_doc = res['state'], res['doc']

    # Built, the link measures 195ms (5ms off) while the probe still says 180ms: after 30 minutes of it,
    # measured worse than probed, gone and kept out 24h.
    for t in (110, 125):
        res = mesh_autolinks.optimize(built_doc, f, status_with(180.0, real_rtt=195.0), state=state, now_ms=t * minute)
        assert not res['changed']
        state = res['state']
    dis_res = mesh_autolinks.optimize(built_doc, f, status_with(180.0, real_rtt=195.0), state=state, now_ms=140 * minute)
    assert dis_res['changed'] and dis_res['dropped'][0]['code'] == 'disappointing'
    assert '实测最好只快 5ms' in dis_res['dropped'][0]['why'] and '保留门槛 10ms' in dis_res['dropped'][0]['why']
    state = dis_res['state']
    for i in range(4):
        retry = mesh_autolinks.optimize(dis_res['doc'], f, status_with(120.0), state=state, now_ms=(150 + 10 * i) * minute)
        assert not retry['changed']
        state = retry['state']

    # After 24h it holds 30 minutes again and is built.
    after_24h = 140 * minute + mesh_autolinks.DISAPPOINT_TTL_MS + 1000
    for i in range(4):
        res2 = mesh_autolinks.optimize(dis_res['doc'], f, status_with(120.0), state=state, now_ms=after_24h + i * 10 * minute)
        state = res2['state']
    assert res2['changed']
    doc_with_auto = res2['doc']

    # The direct baseline falls to 125ms and the auto link measures 120ms: 5ms, under the 10ms it keeps for.
    def status_no_gain():
        st = status_with(120.0, real_rtt=120.0)
        for sid in ('Relay-SG1', 'Relay-DE'):
            st[sid]['status']['routing']['links'][0].update(rttMs=25.0, cost=25.0)
        return st

    t0 = after_24h + 60 * minute
    r_early = mesh_autolinks.optimize(doc_with_auto, f, status_no_gain(), state=state, now_ms=t0)
    assert not r_early['changed']
    r_mid = mesh_autolinks.optimize(doc_with_auto, f, status_no_gain(), state=r_early['state'], now_ms=t0 + 3 * 3600_000)
    assert not r_mid['changed'] and r_mid['state']['idleBest'][mesh_autolinks.pair_key('App-ES', 'Relay-SG1')] == 5.0
    # 6 hours: the auto link goes, while manual relay link Relay-SG1 <-> Relay-DE and manual upstream Relay-DE stay.
    r_drop = mesh_autolinks.optimize(doc_with_auto, f, status_no_gain(), state=r_mid['state'], now_ms=t0 + 6 * 3600_000)
    assert r_drop['changed'] and r_drop['dropped'][0]['code'] == 'idle'
    assert r_drop['dropped'][0]['why'] == '连续 6 小时最好只快 5ms，不到保留门槛 10ms'
    assert len(r_drop['doc']['relayLinks']['pairs']) == 1
    jc_cands = next(m for m in r_drop['doc']['members'] if m['serverId'] == 'App-ES')['entry']['candidates']
    assert [c['relay'] for c in jc_cands] == ['Relay-DE']


# ----- 4a. Settings of the older rules, smoothed costs, and the mesh settling -----
def test_settings_of_the_older_rules_move_to_the_new_thresholds():
    legacy = {'enabled': True, 'perNode': 3, 'gainRatio': 0.85, 'gainMs': 20, 'holdRounds': 3, 'dropAfterS': 3600}
    want = {**mesh_autolinks.DEFAULT_SETTINGS, 'perNode': 3}
    assert mesh_autolinks.settings_of({'settings': {'autoLinks': legacy}}) == want
    errors = model.Errors()
    assert mesh_autolinks.clean_settings(legacy, legacy, errors) == want and not errors
    # A drop time of its own stays; the new keys are taken as given.
    assert mesh_autolinks.clean_settings({**legacy, 'dropAfterS': 7200}, legacy, errors)['dropAfterS'] == 7200
    given = {'buildMs': 25, 'buildRatio': 0.2, 'keepMs': 12.5, 'holdS': 600, 'dropAfterS': 3600}
    assert mesh_autolinks.clean_settings(given, None, errors) == {**mesh_autolinks.DEFAULT_SETTINGS, **given} and not errors
    for bad in ({'keepMs': 20}, {'buildRatio': 1.5}, {'holdS': -1}, {'buildMs': 0}):
        errors = model.Errors()
        mesh_autolinks.clean_settings(bad, None, errors)
        assert errors, bad


def test_history_takes_the_median_and_keeps_a_line_down_a_few_rounds():
    h = mesh_autolinks.History()
    line, flaky = frozenset(('a', 'b')), frozenset(('a', 'c'))
    for i in range(30):
        real = {}
        if i not in (7, 8, 9):
            real[line] = 300.0 if i == 12 else 100.0 + i % 3
        if i % 3 == 0:
            real[flaky] = 50.0
        h.record(i * 60_000, real, {}, {line, flaky}, {}, {})
    # Down 3 rounds of 30 and once 300ms: in, at its median; up 10 of 30: out.
    assert h.smoothed('real') == {line: 101.0}
    h.record(29 * 60_000 + mesh_autolinks.SMOOTH_S * 1000, {}, {}, {line}, {}, {})
    assert h.smoothed('real') == {}

    live = {'a': {}, 'b': {}, 'c': {}}
    def links(state):
        return {'a': {'status': {'routing': {'links': [{'peer': 'b', 'state': state}]}}}}
    t = 10_000_000
    h.record(t, {}, {}, set(), links('up'), live)
    assert mesh_autolinks.settling(links('up'), live, t, h) == set()
    h.record(t + 60_000, {}, {}, set(), links('down'), live)
    assert mesh_autolinks.settling(links('down'), live, t + 60_000, h) == {'a', 'b'}
    assert mesh_autolinks.settling(links('down'), live, t + 60_000 + mesh_autolinks.SETTLE_S * 1000, h) == set()


def test_a_settling_end_or_a_new_revision_stops_the_clocks():
    d, f = _sg_de_es()
    minute = 60_000
    key = mesh_autolinks.pair_key('App-ES', 'Relay-SG1')
    state = None

    def step(t, status, doc=d, applied_ms=0):
        nonlocal state
        res = mesh_autolinks.optimize(doc, f, status, state=state, now_ms=t * minute, applied_ms=applied_ms * minute)
        state = res['state']
        return res

    # Watched from 10 minutes; App-ES restarts at 15, so the round at 20 stops the clock for those 10 minutes.
    assert step(10, _sg_de_es_status(180.0))['suggestions'][0]['heldMs'] == 0
    res = step(20, _sg_de_es_status(180.0, started=15 * minute))
    assert [(x['key'], x['heldMs'], x['paused']) for x in res['suggestions']] == [(key, 0, True)]
    assert step(30, _sg_de_es_status(180.0, started=15 * minute))['suggestions'][0]['heldMs'] == 10 * minute
    assert not step(40, _sg_de_es_status(180.0, started=15 * minute))['changed']
    # A revision applied at 45: the whole mesh settles until 55, the round at 50 builds nothing.
    res = step(50, _sg_de_es_status(180.0, started=15 * minute), applied_ms=45)
    assert res['paused'] and not res['changed'] and res['state']['paused']
    res = step(60, _sg_de_es_status(180.0, started=15 * minute), applied_ms=45)
    assert res['changed'] and res['added'][0]['key'] == key
    built = res['doc']

    # Built, it keeps no pair 10ms faster: idle from 70. App-ES restarting at 125 stops that clock for the round at
    # 130 (the hour since the last round), so it goes at 70 + 6h + 1h, not 70 + 6h.
    def no_gain(started=None):
        st = _sg_de_es_status(120.0, real_rtt=120.0, started=started)
        for sid in ('Relay-SG1', 'Relay-DE'):
            st[sid]['status']['routing']['links'][0].update(rttMs=25.0, cost=25.0)
        return st

    assert not step(70, no_gain(), built)['changed'] and state['idleSince'][key] == 70 * minute
    assert not step(130, no_gain(started=125 * minute), built)['changed'] and state['idleSince'][key] == 130 * minute
    assert not step(70 + 6 * 60, no_gain(), built)['changed']
    res = step(70 + 7 * 60, no_gain(), built)
    assert res['changed'] and res['dropped'][0]['code'] == 'idle' and res['dropped'][0]['why'].startswith('连续 6 小时最好只快 5ms')


# ----- 4b. A link down most rounds (RFCHost-JP2, 2026-11-16): gone in ten rounds, kept out 24h, then 72h -----
def _probe(peer, rtt, loss=0.0):
    return {'peer': peer, 'rttMs': rtt, 'jitterMs': 0.0 if rtt is not None else None, 'loss': loss, 'method': 'udp', 'at': 1}


def _link_state(statuses, a, b, state, rtt=60.0):
    for x, y in ((a, b), (b, a)):
        statuses[x]['status']['routing']['links'].append({'peer': y, 'state': state, 'rttMs': rtt, 'jitterMs': 0.0, 'loss': 0.0 if state == 'up' else 1.0})


def test_an_auto_link_down_most_rounds_goes_and_stays_out_longer_each_time():
    names = ['R1', 'R2', 'JP']
    d = make_doc(*(make_member(n, 'relay') for n in names), pairs=[_link('R1', 'R2'), _link('R2', 'JP')], auto_links={'holdS': 0})
    f = matrix_facts(names)
    key = mesh_autolinks.pair_key('R1', 'JP')

    def statuses(r1_jp):
        st = {}
        for a, b in (('R1', 'R2'), ('R2', 'JP')):
            _up(st, a, b, 100.0)
            _up(st, b, a, 100.0)
        # R1 keeps measuring JP fast from its side, as JP2's outbound UDP went through.
        st['R1']['status']['probes'].append(_probe('JP', 60.0))
        if r1_jp:
            _link_state(st, 'R1', 'JP', r1_jp)
        return st

    doc, state = d, None

    def run(t, r1_jp):
        nonlocal doc, state
        res = mesh_autolinks.optimize(doc, f, statuses(r1_jp), state=state, now_ms=t)
        doc, state = (res['doc'] if res['changed'] else doc), res['state']
        return res

    t0 = 60_000
    assert [x['key'] for x in run(t0, None)['added']] == [key]
    for i in range(1, 10):
        assert not run(t0 + i * 60_000, 'down')['changed']
    first = t0 + 10 * 60_000
    assert [(x['key'], x['why']) for x in run(first, 'down')['dropped']] == [(key, '实测连不上')]
    assert state['disappointed'][key] == first + mesh_autolinks.DISAPPOINT_TTL_MS
    # The probe still says it pays: it used to be built again three rounds on, and dropped an hour later.
    for i in range(1, 6):
        res = run(first + i * 60_000, None)
        assert not res['added'] and key not in [s['key'] for s in res['suggestions']]

    again = first + mesh_autolinks.DISAPPOINT_TTL_MS + 60_000
    assert [x['key'] for x in run(again, None)['added']] == [key]
    for i in range(1, 10):
        assert not run(again + i * 60_000, 'up' if i <= 2 else 'down')['changed']
    second = again + 10 * 60_000
    assert [(x['key'], x['why']) for x in run(second, 'down')['dropped']] == [(key, '实测不稳定')]
    assert state['disappointed'][key] == second + mesh_autolinks.REPEAT_DISAPPOINT_TTL_MS
    assert state['strikes'][key] == {'count': 2, 'at': second}
    assert mesh_autolinks.clean_state(state, second + mesh_autolinks.STRIKE_MEMORY_MS)['strikes'] == {}


def test_a_probe_pair_costs_what_its_worse_end_measures():
    d = make_doc(make_member('R1', 'relay'), make_member('R2', 'relay'))
    f = matrix_facts(['R1', 'R2'])
    pair = frozenset(('R1', 'R2'))
    statuses = {'R1': {'status': {'probes': [_probe('R2', 60.0)]}}, 'R2': {'status': {'probes': [_probe('R1', 60.0, 0.2)]}}}
    assert mesh_autolinks.collect_measurements(d, f, statuses)[1] == {pair: 260.0}
    statuses['R2']['status']['probes'] = [_probe('R1', None, 1.0)]
    assert mesh_autolinks.collect_measurements(d, f, statuses)[1] == {}


def test_a_relay_mostly_down_is_no_end_of_a_new_auto_link_until_it_holds_an_hour():
    names = ['R1', 'R2', 'R3', 'R4', 'JP']
    d = make_doc(*(make_member(n, 'relay') for n in names),
                 pairs=[_link('R1', 'R2'), _link('R2', 'R3'), _link('R2', 'R4'), _link('R2', 'JP'), _link('R3', 'JP'), _link('R4', 'JP')],
                 auto_links={'holdS': 86400})
    f = matrix_facts(names)
    key = mesh_autolinks.pair_key('R1', 'JP')

    def statuses(jp_up):
        st = {}
        for a, b in (('R1', 'R2'), ('R2', 'R3'), ('R2', 'R4'), ('R2', 'JP')):
            _up(st, a, b, 100.0)
            _up(st, b, a, 100.0)
        for peer in ('R3', 'R4'):
            _link_state(st, peer, 'JP', 'up' if jp_up else 'down', 100.0)
        st['R1']['status']['probes'].append(_probe('JP', 60.0))
        return st

    t, state = 0, None

    def watched(jp_up):
        nonlocal t, state
        t += 60_000
        res = mesh_autolinks.optimize(d, f, statuses(jp_up), state=state, now_ms=t)
        state = res['state']
        return key in [s['key'] for s in res['suggestions']]

    assert all([watched(False) for _ in range(9)])
    assert not [watched(False) for _ in range(11)][-1]
    assert state['nodeHealth']['JP']['until'] == t + mesh_autolinks.NODE_STABLE_MS
    # Its neighbours lose a link each to it, not their place.
    assert all('until' not in state['nodeHealth'][n] for n in ('R1', 'R2', 'R3', 'R4'))
    while state['nodeHealth']['JP']['share'] < mesh_autolinks.UNSTABLE_SHARE:
        assert not watched(True)
    until = state['nodeHealth']['JP']['until']
    while t + 60_000 < until:
        assert not watched(True)
    assert watched(True)


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
                 ks, jc, mode='custom', pairs=pairs, auto_links={'holdS': 0})

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
                     mode=mode, pairs=pairs, auto_links={'holdS': 0})
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
    doc['settings']['autoLinks'] = {'enabled': True, 'perNode': 4, 'buildMs': 15, 'buildRatio': 0.1, 'keepMs': 10, 'holdS': 0, 'dropAfterS': 21600}
    for m in doc['members']:
        if m['serverId'] in (ids['r1'], ids['r2'], ids['r3']):
            m['role'] = 'relay'
            m['entry']['candidates'] = []
        elif m['serverId'] == ids['m1']:
            m['entry'] = {'mode': 'upstreams', 'candidates': [{'id': 'c1', 'relay': ids['r3'], 'transport': 'udp'}], 'autoReturn': True}
        elif m['serverId'] == ids['m2']:
            m['entry'] = {'mode': 'upstreams', 'candidates': [{'id': 'c1', 'relay': ids['r2'], 'transport': 'udp'}], 'autoReturn': True}
    await api(e, 'PUT', '/api/servers/mesh/draft', {**doc, 'draftVersion': (await api(e, 'GET', '/api/servers/mesh'))['draftVersion']})
    await api(e, 'POST', '/api/servers/mesh/apply', {})
    await settled(e)

    # Now user edits the draft (changes mtu to 1360), without applying!
    view0 = await api(e, 'GET', '/api/servers/mesh')
    dirty_doc = {k: view0[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules', 'lineRules', 'exitGroups', 'limits')}
    dirty_doc['settings']['mtu'] = 1360
    pre_auto = await api(e, 'PUT', '/api/servers/mesh/draft', {**dirty_doc, 'draftVersion': (await api(e, 'GET', '/api/servers/mesh'))['draftVersion']})
    pre_auto_version = pre_auto['draftVersion']
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

    # 2b. A browser tab still holding the pre-autolink draftVersion saves cleanly without a false 409 conflict,
    # keeping both the user's edit and the background auto link; a stale token after a real user save still 409s.
    browser_doc = copy.deepcopy(dirty_doc)
    browser_doc['settings']['mtu'] = 1340
    merged = await api(e, 'PUT', '/api/servers/mesh/draft', {**browser_doc, 'draftVersion': pre_auto_version})
    assert merged['settings']['mtu'] == 1340
    m2_merged = next(m for m in merged['members'] if m['serverId'] == ids['m2'])
    assert any(c.get('origin') == 'auto' and c['relay'] == ids['r3'] for c in m2_merged['entry']['candidates'])
    await api(e, 'PUT', '/api/servers/mesh/draft', {**browser_doc, 'draftVersion': pre_auto_version}, status=409)
    # Any other write to the draft (adding, removing or leaving members) drops the bases: an older token conflicts
    # again instead of merging over that write.
    stale = (await api(e, 'GET', '/api/servers/mesh'))['draftVersion']
    applied_now = mesh._doc_without_internal(await mesh._load(OWNER, 'applied'))
    await mesh._save_draft(OWNER, await mesh._load(OWNER, 'draft'), auto_base=[{'version': stale, 'applied': applied_now}])
    assert (await mesh._load(OWNER, 'draft')).get('_autoBase')
    await mesh._save_draft(OWNER, await mesh._load(OWNER, 'draft'))
    assert '_autoBase' not in await mesh._load(OWNER, 'draft')
    await api(e, 'PUT', '/api/servers/mesh/draft', {**browser_doc, 'draftVersion': stale}, status=409)

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
                 pairs=[{'a': 'Relay-SG1', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0}], auto_links={'holdS': 0})
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


async def test_auto_bases_never_reach_a_revision_and_member_changes_always_name_a_field(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]})
    await api(e, 'POST', '/api/servers/mesh/apply', {})
    await settled(e)
    mesh = e.servers.mesh
    stale = (await api(e, 'GET', '/api/servers/mesh'))['draftVersion']
    draft = await mesh._load(OWNER, 'draft')
    draft['settings']['mtu'] = 1360
    await mesh._save_draft(OWNER, draft, auto_base=[{'version': stale, 'applied': mesh._doc_without_internal(await mesh._load(OWNER, 'applied'))}])
    await api(e, 'POST', '/api/servers/mesh/apply', {})
    await settled(e)
    rows = await many(mesh.db.conn, 'SELECT doc_json FROM mesh_revisions WHERE owner=?', (OWNER,))
    assert len(rows) >= 2 and not any('_autoBase' in r['doc_json'] for r in rows)
    assert '_autoBase' not in await mesh._load(OWNER, 'draft')
    # Fields without their own words still say what changed, never "修改 X 的" with nothing after it.
    applied = await mesh._load(OWNER, 'applied')
    changed = copy.deepcopy(applied)
    member = next(m for m in changed['members'] if m['serverId'] == ids['m1'])
    member['direct'] = False
    member['auto'] = []
    [change] = mesh._changes(applied, changed, await mesh._facts(OWNER))
    assert change['kind'] == 'member.update' and change['text'].endswith('的自动加入口、跟随自动的设置')


async def test_auto_links_off_takes_no_round_and_writes_nothing(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]})
    state = await api(e, 'GET', '/api/servers/mesh')
    await api(e, 'PUT', '/api/servers/mesh/draft', {'settings': {**state['settings'], 'autoLinks': {**state['settings']['autoLinks'], 'enabled': False}}, 'draftVersion': (await api(e, 'GET', '/api/servers/mesh'))['draftVersion']})
    await api(e, 'POST', '/api/servers/mesh/apply', {})
    await settled(e)
    mesh = e.servers.mesh
    rev_before = (await api(e, 'GET', '/api/servers/mesh/revisions'))['revisions']
    assert await mesh.check_auto_links(OWNER, now=1_000_000) is None
    assert (await api(e, 'GET', '/api/servers/mesh/revisions'))['revisions'] == rev_before


def test_added_edge_score_matches_full_dijkstra():
    names = ['App-SG', 'Relay-SG1', 'Relay-HK', 'Relay-DE', 'App-ES']
    r_sg1 = make_member('Relay-SG1', 'relay')
    r_hk = make_member('Relay-HK', 'relay')
    r_de = make_member('Relay-DE', 'relay')
    m_sg = make_member('App-SG', 'member', entry={'mode': 'upstreams', 'candidates': [{'id': 'c1', 'relay': 'Relay-SG1', 'transport': 'udp'}], 'autoReturn': True})
    m_es = make_member('App-ES', 'member', entry={'mode': 'upstreams', 'candidates': [{'id': 'c1', 'relay': 'Relay-DE', 'transport': 'udp'}], 'autoReturn': True})
    doc = make_doc(r_sg1, r_hk, r_de, m_sg, m_es, mode='custom', pairs=[
        {'a': 'Relay-SG1', 'b': 'Relay-HK', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
        {'a': 'Relay-HK', 'b': 'Relay-DE', 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0},
    ])
    facts = matrix_facts(names)
    statuses = matrix_probes(names)
    real_c, probe_c = mesh_autolinks.collect_measurements(doc, facts, statuses)
    relays = {'Relay-SG1', 'Relay-HK', 'Relay-DE'}
    base_g = mesh_autolinks.build_graph(doc, facts, real_c, probe_c)
    base_tbl = mesh_autolinks.shortest_table(base_g, relays)
    base_dists = mesh_autolinks._shortest_dists(base_g, relays)

    for a, b in (('Relay-SG1', 'Relay-DE'), ('App-ES', 'Relay-SG1'), ('App-SG', 'Relay-HK')):
        cst, _ = mesh_autolinks.edge_cost(a, b, doc, facts, real_c, probe_c)
        assert cst is not None
        trial_g = mesh_autolinks.build_graph(doc, facts, real_c, probe_c, extra=[(a, b)])
        for building in (True, False):
            meet = mesh_autolinks.meets(mesh_autolinks.DEFAULT_SETTINGS, building)
            expected, _ = mesh_autolinks.evaluate_gains(base_tbl, mesh_autolinks.shortest_table(trial_g, relays), meet)
            got = mesh_autolinks._added_edge_score(base_dists, relays, a, b, cst, meet)
            assert abs(got - expected) < 1e-9, (a, b, building, got, expected)


async def test_no_change_autolinks_updates_settings_only_and_routine_events_and_batched_refresh(env, monkeypatch):  # noqa: F811
    import asyncio
    e = env
    nodes, ids = await mesh_setup(e)
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['r2'], ids['m1']]})
    await api(e, 'POST', '/api/servers/mesh/apply', {})
    await settled(e)
    mesh = e.servers.mesh

    # 1. check_auto_links with no changes updates only applied mesh_settings (never calls _save to rewrite members/links/rules).
    draft = await mesh._load(OWNER, 'draft')
    stale = (await api(e, 'GET', '/api/servers/mesh'))['draftVersion']
    await mesh._save_draft(OWNER, draft, auto_base=[{'version': stale, 'applied': mesh._doc_without_internal(await mesh._load(OWNER, 'applied'))}])
    draft_before = await many(mesh.db.conn, "SELECT settings_json FROM mesh_settings WHERE owner=? AND stage='draft'", (OWNER,))

    full_saves = []
    orig_save = mesh._save

    async def spy_save(conn, owner, stage, doc, **kw):
        full_saves.append(stage)
        return await orig_save(conn, owner, stage, doc, **kw)

    to_thread_calls = []
    orig_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kw):
        to_thread_calls.append(fn)
        return await orig_to_thread(fn, *args, **kw)

    monkeypatch.setattr(mesh, '_save', spy_save)
    monkeypatch.setattr(asyncio, 'to_thread', spy_to_thread)
    res = await mesh.check_auto_links(OWNER, now=2_000_000)
    assert res and res['changed'] is False
    assert mesh_autolinks.optimize in to_thread_calls
    assert full_saves == []
    assert (await mesh._load(OWNER, 'applied'))['_autoLinksState']['measuredAt'] == 2_000_000
    draft_after = await many(mesh.db.conn, "SELECT settings_json FROM mesh_settings WHERE owner=? AND stage='draft'", (OWNER,))
    assert draft_after == draft_before

    # 2. Routine route.changed events stay in memory immediately without setting dirty for 10s fast save.
    sid = ids['r1']
    base_status = {'ready': True, 'appliedRevision': 1, 'startedAt': 1000, 'error': '', 'peers': [], 'events': []}
    await mesh.report(sid, base_status)
    mesh.status[sid]['saved'] = 10_000_000_000_000
    mesh.status[sid]['dirty'] = False
    await mesh.report(sid, {**base_status, 'events': [{'seq': 1, 'at': 1001, 'kind': 'route.changed', 'detail': 'a -> b (better)'}]})
    assert mesh.status[sid]['dirty'] is False
    assert any(ev['kind'] == 'route.changed' for ev in mesh.status[sid]['events'])
    await mesh.report(sid, {**base_status, 'events': [{'seq': 2, 'at': 1002, 'kind': 'entry.switch', 'detail': 'r1 -> r2'}]})
    assert mesh.status[sid]['dirty'] is True

    # 3. _refresh_facts batches multiple node updates into one DB write transaction.
    labels = []
    orig_tx = mesh.db.write_transaction

    def spy_tx(*args, **kw):
        labels.append(kw.get('label'))
        return orig_tx(*args, **kw)

    monkeypatch.setattr(mesh.db, 'write_transaction', spy_tx)
    await mesh._refresh_facts(OWNER)
    assert labels.count('mesh-nodes-refresh') == 1
    assert 'mesh-node' not in labels

    # 4. align_draft treats a draft differing only in _autoLinksState as unmodified and returns after_applied cleanly.
    applied_doc = await mesh._load(OWNER, 'applied')
    draft_stale_state = copy.deepcopy(applied_doc)
    draft_stale_state['_autoLinksState'] = {'measuredAt': 1}
    after_doc = copy.deepcopy(applied_doc)
    after_doc['_autoLinksState'] = {'measuredAt': 999}
    assert mesh_autolinks.align_draft(draft_stale_state, applied_doc, after_doc) == after_doc
