"""11-17 controller review regressions. Pure documents/graphs, no services or networking."""
import copy
import random
from types import SimpleNamespace

import pytest

from app.servers import mesh_auto, mesh_fast, mesh_forward, mesh_model as model, mesh_ports
from app.servers.mesh import Mesh, _data_features, _own_data_features, _running_features, MEMBERS_ROUTING_FEATURE
from app.servers.mesh_config import build, node_rules
from app.servers.mesh_path import best_paths, tolled
from tests.test_mesh_config import doc, facts, member
from tests.test_mesh_forward import fwd


def fixture():
    d = doc(*(member(s, 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0}) for s in ('r1', 'r2', 'r3')))
    f = facts()
    for v in f.values():
        v['features'] = {'mesh', mesh_fast.FAST_FEATURE, mesh_forward.FEATURE}
    return d, f


def port(sid='r2', number=8443, id='p1', proto='tcp', src=None):
    return {'id': id, 'serverId': sid, 'port': number, 'proto': proto, 'note': '',
            'src': copy.deepcopy(src if src is not None else mesh_fast.ALL_SOURCES)}


def forward(**kw):
    return fwd(**{'entry': 'r1', 'target': 'r2', 'line': 'fast', **kw})


def codes(d, f):
    return {e['error'] for e in model.validate(d, f, applying=True)}


def test_auto_listen_keeps_fast_override_on_clean_preview_resolve_and_build():
    d, f = fixture()
    d['fastPorts'] = [port()]
    d['members'][0]['auto'] = ['listen']
    d = model.clean_doc({'members': [{'serverId': m['serverId'], **({'listen': {'fast': 53003}} if m['serverId'] == 'r1' else {})}
                                   for m in d['members']]}, d)
    assert not codes(d, f)
    for resolved in (d, mesh_auto.member_defaults(d, f), mesh_auto.resolve(d, f)):
        assert resolved['members'][0]['listen']['fast'] == 53003
        assert build(resolved, f, 'net', 1)['r1']['self']['listen']['fast'] == 53003


def test_fast_strip_and_undo_restore_hosts_sources_and_order_without_resurrecting_other_removals():
    d, f = fixture()
    only = {'all': False, 'allSubnets': False, 'members': ['r1', 'r3'], 'tags': [], 'prefixes': []}
    d['fastPorts'] = [port('r1'), port('r2', 9000, 'p2', src=only)]
    before = copy.deepcopy(d)
    d['members'][0]['removing'] = True
    model.strip(d, 'r1')
    assert [p['id'] for p in d['fastPorts']] == ['p2']
    assert d['fastPorts'][0]['src']['members'] == ['r3']
    assert not codes(d, f)
    d['members'][0]['removing'] = False
    model.unstrip(d, before, 'r1')
    assert d['fastPorts'] == before['fastPorts']
    # Source-only removal stays closed (not ALL_SOURCES), even after round-trip cleaning.
    d['members'][0]['removing'] = d['members'][2]['removing'] = True
    model.strip(d, 'r1'); model.strip(d, 'r3')
    d = model.clean_doc({}, d)
    assert not any(d['fastPorts'][0]['src'].values())
    assert not build(d, f, 'net', 2)['r2']['fast'].get('targets')
    d['members'][0]['removing'] = False
    model.unstrip(d, before, 'r1')
    assert d['fastPorts'][1]['src']['members'] == ['r1']
    assert 'r3' not in d['fastPorts'][1]['src']['members']


def test_internal_ports_avoid_user_ports_with_one_stable_preview_rules_and_wire_result():
    d, f = fixture()
    d['forwards'] = [forward(line='mesh'), forward(id='fw2', entry='r3', listen=8444)]
    d['members'][1]['listen']['tcp'] = 51082
    d['members'][0]['listen']['tcp'] = 52003
    d['fastPorts'] = [port(number=51083)]
    mesh_fast.ensure_secrets(d)
    before = copy.deepcopy(d)
    p = mesh_ports.plan(d, f)
    assert p['forward'] == {'port': 51084, 'fastPort': 51085}
    assert p['fast']['r1'] == 52004
    assert not codes(d, f)
    configs = build(d, f, 'net', 1)
    for sid, cfg in configs.items():
        assert cfg['self']['listen']['fast'] == cfg['fast']['port'] == p['fast'][sid]
        assert cfg['fast']['meshPorts']['r1'] == 52004
        assert cfg['forward']['port'] == p['forward']['port']
    assert configs['r3']['forward']['fastPort'] == configs['r2']['forward']['fastPort'] == 51085
    assert any(t['port'] == 51085 for t in configs['r3']['fast']['targets'])
    assert any(r['ports'] == [[51085, 51085]] for r in node_rules(d, 'r2', f))
    assert any(r['ports'] == [[51084, 51084]] for r in node_rules(d, 'r2', f))
    assert configs['r2']['fast']['next']['r1'].endswith(':52004')
    assert mesh_ports.plan(d, f) == p and d == before
    d['members'].reverse(); d['forwards'].reverse(); d['fastPorts'].reverse()
    assert mesh_ports.plan(d, f) == p


def test_fast_only_forward_avoids_effective_transport_and_never_changes_pinned_ports():
    d, f = fixture()
    d['forwards'] = [forward()]
    mesh_fast.ensure_secrets(d)
    m = d['members'][1]
    m['listen']['tcp'] = 52003
    assert not codes(d, f)
    cfg = build(d, f, 'net', 1)['r2']
    assert cfg['self']['listen']['tcp'] == 52003 and cfg['self']['listen']['fast'] == 52004
    for pin in ({'listen': {'fast': 52003}}, {'publicPorts': {'fast': 62003}}):
        pinned = copy.deepcopy(d)
        for key, value in pin.items():
            pinned['members'][1].setdefault(key, {}).update(value)
        before = copy.deepcopy(pinned)
        assert mesh_ports.plan(pinned, f)['fast']['r2'] == 52003
        assert 'fast_port_conflict' in codes(pinned, f)
        assert pinned == before  # Explicit conflict cannot be silently resolved by moving a mapping.
    m['listen']['fast'] = 53003
    m['publicPorts'] = {'fast': 63003}
    cfgs = build(d, f, 'net', 1)
    assert cfgs['r2']['fast']['port'] == 53003
    assert cfgs['r1']['fast']['next']['r2'].endswith(':63003')


def test_disabled_features_reserve_no_new_default_ports_on_upgrade():
    for n in (52003, 51082, 51083):
        d, f = fixture()
        d['settings']['ports']['tcp'] = n
        for m in d['members']:
            m['listen']['tcp'] = n
        for key in ('fast', 'forward'):
            d['settings'].pop(key)
        model.upgrade(d)
        assert not codes(d, f)
        assert mesh_ports.plan(d, f)['fast'] == {}
        assert all('fast' not in c and 'forward' not in c for c in build(d, f, 'net', 1).values())


async def test_dormant_fast_override_is_not_probed_as_a_real_listener():
    d, f = fixture()
    applied = copy.deepcopy(d)
    d['members'][0]['listen']['fast'] = 53003
    async def unexpected_probe(*args, **kwargs):
        raise AssertionError('dormant fast override is not an active listener')
    fake = SimpleNamespace(_ports=unexpected_probe)
    assert await Mesh._port_warnings(fake, 'owner', d, applied, f) == []


def test_targets_merge_equivalent_protocol_permissions_then_enforce_actual_node_capacity():
    d, f = fixture()
    src = {'all': False, 'allSubnets': False, 'members': ['r1'], 'tags': [], 'prefixes': []}
    d['fastPorts'] = [port(proto='any', src=src), port(id='p2', proto='tcp', src=src)]
    mesh_fast.ensure_secrets(d)
    assert not codes(d, f)
    targets = build(d, f, 'net', 1)['r1']['fast']['targets']
    assert targets == [{'node': 'r2', 'port': 8443, 'proto': 'any', 'src': [d['members'][0]['address'] + '/32']}]
    # A TCP grant to r3 must not become a UDP grant to r3 when coalescing.
    d['fastPorts'].append(port(id='p3', proto='tcp', src={**src, 'members': ['r3']}))
    cfg = build(d, f, 'net', 2)
    assert cfg['r3']['fast']['targets'] == [{'node': 'r2', 'port': 8443, 'proto': 'tcp', 'src': [d['members'][2]['address'] + '/32']}]
    d['forwards'] = [forward()]
    d['fastPorts'] = [port(number=10000+i, id=f'p{i}') for i in range(256)]
    assert 'fast_targets_too_many' in codes(d, f)
    with pytest.raises(model.MeshInvalid) as exc:
        build(d, f, 'net', 3)
    assert exc.value.errors[0]['error'] == 'fast_targets_too_many'
    # Same raw list length, but one equivalent entry: the real 256-target node capacity is enough.
    d['fastPorts'][-1]['port'] = d['fastPorts'][0]['port']
    assert not codes(d, f)
    assert len(build(d, f, 'net', 3)['r1']['fast']['targets']) == 256


def test_forwards_feature_follows_running_data_plane_not_new_node_build():
    features = {'mesh', MEMBERS_ROUTING_FEATURE, mesh_fast.FAST_FEATURE, mesh_forward.FEATURE}
    for raw in ('[]', '["mesh.routing.members"]', ''):
        reads = _data_features({'data_features': raw, 'data_version': '1.3.0'})
        actual = _running_features(features, reads)
        assert mesh_forward.FEATURE not in actual and mesh_fast.FAST_FEATURE not in actual
    # Unknown data-plane routing does not tear down existing routes during the upgrade.
    assert MEMBERS_ROUTING_FEATURE in _running_features(features, None)
    session = SimpleNamespace(build='new', features=features)
    assert mesh_forward.FEATURE in _own_data_features(session, 'new')
    assert _own_data_features(session, 'old') == ''
    assert _running_features(features, frozenset(features)) == features


def test_no_transit_first_entry_and_yen_spurs_keep_original_exemptions():
    roles = {'s': 'member', 'a': 'relay', 'b': 'relay', 't': 'relay'}
    graph = {s: {} for s in roles}
    for a, b, w in [('s', 'a', 7), ('a', 'b', 12), ('s', 'b', 22), ('b', 't', 18)]:
        graph[a][b] = graph[b][a] = (w, w, 0)
    assert best_paths(graph, roles, 's', 't', 2, {'b'}) == [['s', 'b', 't'], ['s', 'a', 'b', 't']]
    # Bounded exhaustive oracle also covers spurs whose root starts before a tolled relay.
    rng = random.Random(2718)
    ids = ['s', 'a', 'b', 'c', 'd', 't']
    for _ in range(180):
        roles = {n: 'relay' for n in ids}
        roles['s'], roles['t'] = rng.choice(['member', 'relay']), rng.choice(['member', 'relay'])
        no_transit = {n for n in ids[1:-1] if rng.randrange(3) == 0}
        graph = {n: {} for n in ids}
        for i, a in enumerate(ids):
            for b in ids[i+1:]:
                if rng.random() < .5:
                    w = rng.randrange(1, 25)
                    graph[a][b] = graph[b][a] = (w, w, 0)
        paths = []
        def walk(p):
            if p[-1] == 't':
                paths.append(p)
                return
            for v in graph[p[-1]]:
                if v not in p:
                    walk(p + [v])
        walk(['s'])
        def rank(p):
            return len(tolled(p, roles, no_transit)), sum(graph[a][b][0] for a, b in zip(p, p[1:]))
        got = best_paths(graph, roles, 's', 't', 10, no_transit)
        assert [rank(p) for p in got] == sorted(rank(p) for p in paths)[:10]
        assert all(len(p) == len(set(p)) for p in got)
