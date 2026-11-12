"""Line rules in full mode: the links no one listed (every pair of relays) follow the rules as listed and
auto links do, and without a rule that covers them the configs are what they were before rules reached
them; the draft's changes name the applied links a rule changes."""
from __future__ import annotations

import copy
import hashlib
import json

from app.servers import mesh_auto
from app.servers import mesh_model as model
from app.servers.mesh import Mesh
from app.servers.mesh_config import build, relay_pairs
from tests.test_mesh_config import SITE_IPV4, doc, facts, member, pair, site_doc
from tests.test_mesh_front import front_facts, mesh_doc

FULL = {'mode': 'full', 'pairs': []}


def auto(m):
    m['auto'] = list(mesh_auto.MEMBER_AUTO)
    return m


def samples():
    """(name, doc, facts) in full mode, from the shapes the other mesh tests use."""
    out = [('public relays', doc(member('r1', 'relay'), member('r2', 'relay'), member('r3', 'relay'), member('m1'), member('m2')), facts())]
    out.append(('all left to the mesh', doc(auto(member('r1', 'relay')), auto(member('r2', 'relay')), auto(member('r3', 'relay', relayMode='site')),
                                            auto(member('m1')), auto(member('m2'))), facts()))
    sites = site_doc()
    sites['relayLinks'] = copy.deepcopy(FULL)
    out.append(('site relays', sites, facts(ipv4=SITE_IPV4)))
    listed = site_doc()
    listed['relayLinks']['mode'] = 'full'
    out.append(('site relays, some links listed', listed, facts(ipv4=SITE_IPV4)))
    for udp in (0, 51999):
        front = mesh_doc(front_udp=udp, relay_links=copy.deepcopy(FULL))
        front['members'].append(member('r3', 'relay'))
        out.append((f'a front, udp {udp}', front, front_facts()))
    tls = {'active': {'fingerprint': 'cd' * 32}}
    out.append(('tcp only, tls, no address', doc(member('r1', 'relay', listen={'udp': 0, 'tcp': 52000}), member('r2', 'relay', listen={'tls': 52001}),
                                                 member('r3', 'relay'), member('m1')),
                facts(ipv4={'r3': ''}, tls={'r2': tls})))
    # Members as they were stored when the bytes were captured: fixed entries by default, nothing on direct links.
    for _, d, _ in out:
        for m in d['members']:
            if m['entry']['mode'] == 'upstreams':
                m['entry']['mode'] = 'fixed'
            m.pop('direct', None)
    return out


def dump(d, f):
    """All a full mode doc gives: the configs, the doc as worked out and the draft check."""
    resolved = mesh_auto.resolve(d, f)
    # Captured before documents had connection limits, fixed paths or autoLinks; with defaults they change nothing else.
    for key in ('limits', 'paths'):
        if resolved.get(key) == []:
            del resolved[key]
    if resolved.get('settings', {}).get('autoLinks') == model.default_settings()['autoLinks']:
        resolved['settings'] = {k: v for k, v in resolved['settings'].items() if k != 'autoLinks'}
    body = {'configs': build(d, f, 'net', 1), 'resolved': resolved, 'errors': model.validate(d, f), 'hints': model.hints(d, f)}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


# Captured before F4: canonical JSON bytes of configs, resolved documents, errors and hints.
BEFORE_BYTES = {'public relays': '4a8a54ceab3b1a7885fdfa15c8ea52785387411b777cd0ccbcb09b488e3e1eb3', 'all left to the mesh': 'f158126273f81edb41e076f2a347b41024c0adead29c2a43ee1c0b26e8ba4af1', 'site relays': 'f930715a51030357e8d99d990b21b3541357d9f4706cd85ca0556012d2f271d0', 'site relays, some links listed': '4e0ed0eb4d73b77729d30a50c75e7a56ec4fc0ceb25e88bb03db0bbc42da0058', 'a front, udp 0': 'c8912ee8d16e405498eeb34a776888082775483f160e5ab41be48c05fc6246d1', 'a front, udp 51999': 'e55d2df4ee3a5b88cdc7c45ea26a00ba3fb3aa90efdabcf61abd4021eb8e9d72', 'tcp only, tls, no address': 'b29668c534a611a1d4d312be2d88581749c48fe4affe62d18d9d2c6692d423ed'}


# What every sample's links and relays' listen ports were before line rules reached full mode's links.
BEFORE = {
    'public relays': [('r1', 'r2', 'udp', 'a', 0, False), ('r1', 'r3', 'udp', 'a', 0, False), ('r2', 'r3', 'udp', 'a', 0, False)],
    'all left to the mesh': [('r1', 'r2', 'udp', 'a', 0, False), ('r1', 'r3', 'udp', 'b', 0, False), ('r2', 'r3', 'udp', 'b', 0, False)],
    'site relays': [('gw', 'r1', 'udp', 'a', 0, False), ('gw', 'r2', 'udp', 'a', 0, False), ('gw', 'r3', 'udp', 'a', 0, True),
                    ('r1', 'r2', 'udp', 'a', 0, False), ('r1', 'r3', 'udp', 'b', 0, False), ('r2', 'r3', 'udp', 'b', 0, False)],
    'site relays, some links listed': [('gw', 'r1', 'udp', 'a', 0, False), ('gw', 'r2', 'tcp', 'a', 0, False), ('gw', 'r3', 'udp', 'b', -5, True),
                                       ('r1', 'r2', 'udp', 'a', 20, False), ('r2', 'r3', 'udp', 'b', 0, False), ('r3', 'r1', 'udp', 'a', 0, False)],
    'a front, udp 0': [('m1', 'm2', 'tls', 'a', 0, False), ('m1', 'r1', 'udp', 'a', 0, False), ('m1', 'r2', 'udp', 'a', 0, False),
                       ('m1', 'r3', 'udp', 'a', 0, False), ('m2', 'r1', 'tls', 'a', 0, False), ('r1', 'r2', 'udp', 'a', 0, False),
                       ('r1', 'r3', 'udp', 'a', 0, False), ('r2', 'r3', 'udp', 'a', 0, False)],
    'tcp only, tls, no address': [('r1', 'r2', 'udp', 'a', 0, False), ('r1', 'r3', 'tcp', 'b', 0, False), ('r2', 'r3', 'udp', 'b', 0, False)],
}
BEFORE['a front, udp 51999'] = BEFORE['a front, udp 0']
LISTEN_BEFORE = {
    'public relays': {'r1': [51999, 0, 0], 'r2': [51999, 0, 0], 'r3': [51999, 0, 0]},
    'all left to the mesh': {'r1': [51999, 52000, 0], 'r2': [51999, 52000, 0], 'r3': [51999, 0, 0]},
    'site relays': {'r1': [51999, 52000, 0], 'r2': [51999, 52000, 0], 'r3': [51999, 0, 0], 'gw': [51999, 0, 0]},
    'a front, udp 0': {'r1': [51999, 0, 52001], 'r2': [51999, 0, 0], 'm1': [51999, 0, 0], 'm2': [0, 0, 52001], 'r3': [51999, 0, 0]},
    'a front, udp 51999': {'r1': [51999, 0, 52001], 'r2': [51999, 0, 0], 'm1': [51999, 0, 0], 'm2': [51999, 0, 52001], 'r3': [51999, 0, 0]},
    'tcp only, tls, no address': {'r1': [0, 52000, 0], 'r2': [51999, 0, 52001], 'r3': [51999, 0, 0]},
}
LISTEN_BEFORE['site relays, some links listed'] = LISTEN_BEFORE['site relays']


def line_rule(name='规则', rid='l1', a=None, b=None, applies='lines', **sets):
    return {'id': rid, 'name': name, 'on': True, 'applies': applies, 'a': a or {'kind': 'any'}, 'b': b or {'kind': 'any'},
            'order': None, 'bias': None, 'dialer': None, **sets}


def runs(d, f):
    """The links as they run and the relays' listen ports, d worked out first as a build does."""
    r = mesh_auto.resolve(d, f)
    links = sorted((p['a'], p['b'], p['transport'], p['dialer'], p['bias'], bool(p.get('punch'))) for p in relay_pairs(r, f))
    return links, {m['serverId']: [m['listen'][t] for t in ('udp', 'tcp', 'tls')] for m in r['members'] if m['role'] == 'relay'}


def test_without_a_rule_on_them_full_modes_links_and_configs_are_as_before():
    covers_nobody = [line_rule('停用的', 'l1', order=['tls'], bias=100, dialer='b') | {'on': False},
                     line_rule('没人有的标签', 'l2', a={'kind': 'tag', 'tags': ['没有']}, order=['tls'], bias=100, dialer='b')]
    for name, d, f in samples():
        assert hashlib.sha256(dump(d, f).encode()).hexdigest() == BEFORE_BYTES[name], name
        assert runs(d, f) == (BEFORE[name], LISTEN_BEFORE[name]), name
        configs = json.dumps(build(d, f, 'net', 1), sort_keys=True)
        for rules in ([], covers_nobody):
            ruled = copy.deepcopy(d)
            ruled['lineRules'] = rules
            assert runs(ruled, f) == runs(d, f), name
            assert json.dumps(build(ruled, f, 'net', 1), sort_keys=True) == configs, name


def test_a_rule_steers_full_modes_links_as_it_does_auto_modes():
    # The case from the review: only TCP, the other end dials, avoided by 100 ms.
    rule = line_rule('只走 TCP', order=['tcp'], dialer='b', bias=100)
    d = doc(auto(member('r1', 'relay')), auto(member('r2', 'relay')), auto(member('m1')))
    d['lineRules'] = [rule]
    f = facts()
    assert runs(d, f)[0] == [('r1', 'r2', 'tcp', 'b', 100, False)]
    auto_mode = copy.deepcopy(d)
    auto_mode['relayLinks'] = {'mode': 'auto', 'pairs': []}
    [p] = mesh_auto.resolve(auto_mode, f, links=True)['relayLinks']['pairs']
    assert (p['transport'], p['dialer'], p['bias']) == ('tcp', 'b', 100)
    to_r1 = next(link for link in build(d, f, 'net', 1)['r2']['links'] if link['peer'] == 'r1')
    assert to_r1['dial'] and to_r1['endpoint'].startswith('tcp://')
    # TLS first: the relays left to the mesh open TLS for it, and the link takes it.
    pins = {'active': {'fingerprint': 'ef' * 32}}
    d['lineRules'] = [line_rule('TLS 优先', order=['tls', 'udp'])]
    f = facts(tls={'r1': pins, 'r2': pins})
    links, listen = runs(d, f)
    assert links == [('r1', 'r2', 'tls', 'a', 0, False)] and listen['r2'][2] and listen['r1'][2]
    assert not model.validate(mesh_auto.resolve(d, f), f)


def test_fronts_and_site_relays_keep_how_they_connect_and_take_the_bias():
    for name, d, f in samples():
        if not name.startswith(('a front', 'site relays')):
            continue
        before = {(a, b): rest for a, b, *rest in BEFORE[name]}
        d['lineRules'] = [line_rule('只走 TCP', order=['tcp'], bias=30)]
        for a, b, transport, dialer, bias, punch in runs(d, f)[0]:
            listed = any({a, b} == {p['a'], p['b']} for p in d['relayLinks']['pairs'])
            sides = {(x['serverId'], x['role'], x.get('relayMode')) for x in d['members'] if x['serverId'] in (a, b)}
            fixed = 'm2' in (a, b) and name.startswith('a front') or punch
            if listed:
                assert [transport, dialer, bias, punch] == before[(a, b)], (name, a, b)
            elif fixed:
                assert [transport, dialer, punch] == [before[(a, b)][0], before[(a, b)][1], punch] and bias == 30, (name, a, b, sides)
            else:
                assert transport == 'tcp' or not d['members'][0]['listen']['tcp'], (name, a, b)


def test_unusable_rules_fail_the_same_checks_as_listed_auto_links():
    _, d, f = next(s for s in samples() if s[0] == 'tcp only, tls, no address')
    d['lineRules'] = [line_rule('只走 TLS', a={'kind': 'machines', 'ids': ['r1']}, b={'kind': 'machines', 'ids': ['r3']}, order=['tls'])]
    implicit = next(p for p in relay_pairs(d, f) if p['a'] == 'r1' and p['b'] == 'r3')
    assert implicit['transport'] == 'tls'
    assert any(e['error'] == 'transport_not_listening' for e in model.validate(d, f))
    explicit = copy.deepcopy(d)
    explicit['relayLinks'] = {'mode': 'custom', 'pairs': [{**pair('r1', 'r3', dialer=implicit['dialer']), 'auto': list(mesh_auto.PAIR_AUTO)}]}
    [p] = mesh_auto.resolve(explicit, f)['relayLinks']['pairs']
    assert (p['transport'], p['dialer']) == (implicit['transport'], implicit['dialer'])


def test_explain_gives_full_modes_links_as_they_are_built():
    d = doc(member('r1', 'relay', listen={'tcp': 52000}), member('r2', 'relay', listen={'tcp': 52000}), member('r3', 'relay'))
    d['lineRules'] = [line_rule('r3 少走', a={'kind': 'machines', 'ids': ['r3']}, bias=50), line_rule('TCP', 'l2', order=['tcp', 'udp'])]
    f = facts()
    pairs = mesh_auto.explain(d, f)['pairs']
    assert set(pairs) == {'r1|r2', 'r1|r3', 'r2|r3'}
    for p in relay_pairs(d, f):
        got = pairs[f"{p['a']}|{p['b']}"]
        assert (got['transport']['value'], got['dialer']['value'], got['bias']['value']) == (p['transport'], p['dialer'], p['bias'])
    assert pairs['r1|r2']['transport'] == {'value': 'tcp', 'reason': '按规则「TCP」'} and pairs['r1|r2']['rules'] == ['l2']
    assert pairs['r1|r3']['bias'] == {'value': 50, 'reason': '按规则「r3 少走」'} and pairs['r1|r3']['rules'] == ['l1', 'l2']
    # r3 has no TCP: UDP, next in the rule's order.
    assert pairs['r2|r3']['transport']['value'] == 'udp'
    # Listed links are explained as before, without implicit.
    d['relayLinks'] = {'mode': 'full', 'pairs': [{**pair('r1', 'r2'), 'bias': 0}]}
    assert 'implicit' not in mesh_auto.explain(d, f)['pairs']['r1|r2']


def test_the_draft_names_applied_links_a_rule_changes_and_says_they_reconnect():
    applied = doc(member('r1', 'relay', listen={'tcp': 52000}), member('r2', 'relay', listen={'tcp': 52000}), member('r3', 'relay'))
    f = facts()
    draft = copy.deepcopy(applied)
    draft['lineRules'] = [line_rule('只走 TCP', a={'kind': 'machines', 'ids': ['r1']}, b={'kind': 'machines', 'ids': ['r2']}, order=['tcp'], dialer='b'),
                          line_rule('r3 少走', 'l2', a={'kind': 'machines', 'ids': ['r3']}, bias=100)]
    changes = Mesh._changes(applied, draft, f)
    assert [c['text'] for c in changes] == [
        '修改线路规则',
        '中转线路 R1 ⇄ R2：协议 UDP → TCP、主动连的一端 R1 → R2，应用后这条线路会重连',
        '中转线路 R1 ⇄ R3：偏好 0 → +100ms，应用后选路随之调整',
        '中转线路 R2 ⇄ R3：偏好 0 → +100ms，应用后选路随之调整']
    assert [(c['kind'], c.get('a'), c.get('b')) for c in changes][1] == ('link.change', 'r1', 'r2')
    # The same document on both sides: nothing, in any mode.
    for mode in ('full', 'custom'):
        same = copy.deepcopy(draft)
        same['relayLinks'] = {'mode': mode, 'pairs': [] if mode == 'full' else [{**pair('r1', 'r2'), 'bias': 0}]}
        assert Mesh._changes(same, copy.deepcopy(same), f) == []


def test_preview_compares_issued_bytes_not_a_reinterpretation_of_old_rules():
    d = doc(auto(member('r1', 'relay')), auto(member('r2', 'relay')))
    f = facts()
    old_configs = build(d, f, 'net', 1)
    # An old controller stored these rules but ignored them on implicit links.
    d['lineRules'] = [line_rule(order=['tcp'], dialer='b', bias=100)]
    changes = Mesh._changes(d, d, f, configs=old_configs)
    assert len(changes) == 1
    assert changes[0]['text'] == '中转线路 R1 ⇄ R2：协议 UDP → TCP、主动连的一端 R1 → R2、偏好 0 → +100ms，应用后这条线路会重连'
    assert Mesh._changes(d, d, f, configs=build(d, f, 'net', 2)) == []


def test_first_matching_rule_per_field_and_explicit_overrides():
    d = doc(auto(member('r1', 'relay')), auto(member('r2', 'relay')), auto(member('r3', 'relay')))
    f = facts()
    d['lineRules'] = [line_rule('反向', a={'kind': 'machines', 'ids': ['r2']}, b={'kind': 'machines', 'ids': ['r1']}, dialer='a'),
                      line_rule('TCP', 'l2', order=['tcp', 'udp'], bias=-30), line_rule('后面的', 'l3', order=['udp'], bias=100)]
    pairs = relay_pairs(mesh_auto.resolve(d, f), f)
    p = next(p for p in pairs if (p['a'], p['b']) == ('r1', 'r2'))
    assert (p['transport'], p['dialer'], p['bias']) == ('tcp', 'b', -30)
    pinned = {**pair('r2', 'r1', cost=5), 'bias': 20}
    d['relayLinks']['pairs'] = [pinned]
    assert next(p for p in relay_pairs(mesh_auto.resolve(d, f), f) if {p['a'], p['b']} == {'r1', 'r2'}) == pinned


def test_legacy_documents_keep_their_full_rules_semantics_until_explicit_apply():
    d = doc(auto(member('r1', 'relay')), auto(member('r2', 'relay')))
    f = facts(tls={sid: {'active': {'fingerprint': 'ab' * 32}} for sid in ('r1', 'r2')})
    before = json.dumps(build(d, f, 'net', 1), sort_keys=True)
    d['lineRules'] = [line_rule(order=['tls'], dialer='b', bias=100)]
    legacy = model.upgrade(copy.deepcopy(d))
    assert legacy['_fullLineRules'] is False
    assert json.dumps(build(legacy, f, 'net', 1), sort_keys=True) == before
    preview = mesh_auto.for_draft(legacy)
    assert preview['_fullLineRules'] is True and legacy['_fullLineRules'] is False
    assert runs(preview, f)[0] == [('r1', 'r2', 'tls', 'b', 100, False)]
    assert runs(legacy, f)[1]['r2'][2] == 0  # no TLS listener opened by automatic reconcile
    assert runs(preview, f)[1]['r1'][2] != 0 and runs(preview, f)[1]['r2'][2] == 0
    # The marker never appears in configs, and without rules either interpretation is byte-identical.
    assert '_fullLineRules' not in json.dumps(build(preview, f, 'net', 1))
    for name, sample, ff in samples():
        legacy_sample = {**sample, '_fullLineRules': False}
        assert json.dumps(build(legacy_sample, ff, 'net', 1), sort_keys=True) == json.dumps(build(sample, ff, 'net', 1), sort_keys=True), name
