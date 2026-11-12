"""Own routes in the controller: the document, the draft check, the configs nodes get, the path query."""
from __future__ import annotations

import copy

import pytest

from app.servers import compat, mesh_autolinks, mesh_path, mesh_paths, mesh_status
from app.servers import mesh_model as model
from app.servers.mesh_config import MEMBERS_ROUTING_FEATURE, PATHS_BACKUP_FEATURE, PATHS_FEATURE, build

IDS = ('r1', 'r2', 'r3', 'm1', 'm2', 'm3')
KEYS = {sid: tuple(f'{sid}{k}'.ljust(43, c) + '=' for k, c in (('wg', 'A'), ('ctl', 'B'), ('sig', 'C'), ('e2e', 'D'))) for sid in IDS}
NEW = (MEMBERS_ROUTING_FEATURE, PATHS_FEATURE, PATHS_BACKUP_FEATURE)


def _member(sid, role='member', relays=(), mode='upstreams'):
    m = model.new_member(sid, model.default_settings())
    m['role'] = role
    m['listen'] = {**m['listen'], 'tcp': 52000}
    if relays:
        m['entry'] = {'mode': mode, 'candidates': [{'id': f'c{i + 1}', 'relay': r, 'transport': 'tcp'} for i, r in enumerate(relays)],
                      'autoReturn': True}
    return m


def _doc(paths=(), members=None, relay_mode='full', pairs=()):
    d = model.empty_doc()
    d['members'] = members or [_member('r1', 'relay'), _member('r2', 'relay'), _member('r3', 'relay'),
                               _member('m1', relays=('r1', 'r3')), _member('m2', relays=('r2',)), _member('m3', relays=('r3',))]
    d['relayLinks'] = {'mode': relay_mode, 'pairs': [{'a': a, 'b': b, 'transport': 'tcp', 'dialer': 'a', 'bias': 0} for a, b in pairs]}
    errors = model.Errors()
    d['paths'] = mesh_paths.clean(list(paths), errors)
    assert not errors
    return model.allocate(d)


def _facts(old=None):
    """old: {serverId: features} for the nodes that run an older data plane."""
    old = old or {}
    return {sid: {'name': sid.upper(), 'platform': 'linux', 'ipv4': f'203.0.113.{i + 1}', 'publicKey': KEYS[sid][0], 'controlKey': KEYS[sid][1],
                  'signKey': KEYS[sid][2], 'e2eKey': KEYS[sid][3], 'localPrefixes': [], 'configSchemas': compat.CONFIG_SCHEMA,
                  'features': list(old.get(sid, NEW))}
            for i, sid in enumerate(IDS)}


def _path(src, dst, via, **more):
    return {'from': src, 'to': dst, 'via': list(via), **more}


def _codes(errors):
    return [e['error'] for e in errors]


def _ids(configs, sid):
    return [p['id'] for p in configs[sid].get('paths', [])]


def test_clean_fills_defaults_and_refuses_bad_shapes():
    errors = model.Errors()
    out = mesh_paths.clean([_path('m1', 'm2', ['r1']), _path('m1', 'm3', ['r3'], id='p1', on=False)], errors)
    assert not errors
    assert out == [{'id': 'p2', 'on': True, 'from': 'm1', 'to': 'm2', 'via': ['r1']}, {'id': 'p1', 'on': False, 'from': 'm1', 'to': 'm3', 'via': ['r3']}]
    for bad, code in ((_path('m1', 'm2', []), 'invalid_path_via'), (_path('m1', 'm2', ['r1', 'r1']), 'path_via_repeated'),
                      (_path('m1', 'm2', [f'r{i}' for i in range(9)]), 'invalid_path_via'), (_path('', 'm2', ['r1']), 'path_side_empty'),
                      (_path('m1', 'm1', ['r1']), 'path_same_ends'), (_path('r1', 'm2', ['r1', 'r2']), 'path_end_on_via'),
                      ({**_path('m1', 'm2', ['r1']), 'back': 'auto'}, 'invalid_path'), (_path('m1', 'm2', ['r1'], id='a~back'), 'invalid_path'),
                      (_path({'kind': 'machines', 'ids': ['m1']}, 'm2', ['r1']), 'path_side_empty')):
        errors = model.Errors()
        mesh_paths.clean([bad], errors)
        assert code in _codes(errors), bad


def test_a_pair_takes_three_routes_either_way_round_and_no_repeat():
    routes = [_path('m1', 'm2', ['r1', 'r2']), _path('m2', 'm1', ['r3', 'r1']), _path('m1', 'm2', ['r3', 'r2']), _path('m1', 'm3', ['r3'])]
    errors = model.Errors()
    mesh_paths.clean(routes, errors)
    assert not errors
    errors = model.Errors()
    mesh_paths.clean([*routes, _path('m2', 'm1', ['r2', 'r3', 'r1'])], errors)
    assert [(e['path'], e['error']) for e in errors] == [('paths[4]', 'path_pair_full')]
    # The same route written the other way round.
    errors = model.Errors()
    mesh_paths.clean([routes[0], _path('m2', 'm1', ['r2', 'r1'])], errors)
    assert _codes(errors) == ['path_repeated']


def test_configs_go_to_the_nodes_on_the_route_with_the_same_way_back():
    doc = _doc([_path('m1', 'm2', ['r1', 'r2'])])
    configs = build(doc, _facts(), 'net', 1)
    assert configs['m1']['paths'] == [{'id': 'p1', 'from': ['m1'], 'to': ['m2'], 'via': ['r1', 'r2'], 'rank': 1}]
    assert configs['m2']['paths'] == [{'id': 'p1~back', 'from': ['m2'], 'to': ['m1'], 'via': ['r2', 'r1'], 'rank': 1}]
    for relay in ('r1', 'r2'):
        assert configs[relay]['paths'] == [{'id': 'p1', 'from': ['m1'], 'to': ['m2'], 'via': ['r1', 'r2'], 'rank': 1},
                                           {'id': 'p1~back', 'from': ['m2'], 'to': ['m1'], 'via': ['r2', 'r1'], 'rank': 1}]
    for other in ('r3', 'm3'):
        assert 'paths' not in configs[other]


def test_nodes_off_the_routes_get_the_configs_they_had():
    without = build(_doc(), _facts(), 'net', 1)
    with_paths = build(_doc([_path('m1', 'm2', ['r1', 'r2'])]), _facts(), 'net', 1)
    for sid in ('r3', 'm3'):
        assert with_paths[sid] == without[sid]
    for sid in ('m1', 'm2', 'r1', 'r2'):
        assert {k: v for k, v in with_paths[sid].items() if k != 'paths'} == without[sid]


def test_a_pairs_routes_reach_the_nodes_in_their_order():
    # The second is written from m2: m1 to m2 goes r1 r2 first, then r3 r2 (its way back); m2 to m1 the reverse ways.
    doc = _doc([_path('m1', 'm2', ['r1', 'r2']), _path('m2', 'm1', ['r2', 'r3'])])
    configs = build(doc, _facts(), 'net', 1)
    assert _ids(configs, 'm1') == ['p1', 'p2~back'] and _ids(configs, 'm2') == ['p1~back', 'p2']
    assert _ids(configs, 'r2') == ['p1', 'p1~back', 'p2', 'p2~back'] and _ids(configs, 'r3') == ['p2', 'p2~back']
    assert configs['m1']['paths'][1] == {'id': 'p2~back', 'from': ['m1'], 'to': ['m2'], 'via': ['r3', 'r2'], 'rank': 2}
    # A relay on the second alone still has it as the second: the source marks its packets with the rank.
    assert [p['rank'] for p in configs['r3']['paths']] == [2, 2]
    assert [e['rank'] for e in mesh_paths.plan(doc, _facts())] == [1, 2]
    # Turned the other way round, the second comes first.
    doc['paths'].reverse()
    assert _ids(build(doc, _facts(), 'net', 1), 'm1') == ['p2~back', 'p1']


def test_an_old_node_on_a_pairs_routes_leaves_it_the_first_alone():
    doc = _doc([_path('m1', 'm2', ['r1', 'r2']), _path('m1', 'm2', ['r3', 'r2'])])
    facts = _facts(old={'r3': (MEMBERS_ROUTING_FEATURE, PATHS_FEATURE)})
    configs = build(doc, facts, 'net', 1)
    assert _ids(configs, 'm1') == ['p1'] and 'paths' not in configs['r3']
    hint = next(h for h in model.hints(doc, facts) if h['error'] == 'path_backup_outdated')
    assert hint['path'] == 'paths[1]' and 'R3 的节点版本只用每对的第 1 条' in hint['message']
    assert not model.validate(doc, facts)
    # A pair with one route needs nothing more.
    doc['paths'].pop()
    assert not [h for h in model.hints(doc, facts) if h['error'] == 'path_backup_outdated']


def test_what_the_mesh_lacks_keeps_the_draft_and_stops_apply():
    facts = _facts()
    cases = [
        (_doc([_path('m1', 'm2', ['r1', 'r2'])], relay_mode='custom', pairs=[('r1', 'r3'), ('r2', 'r3')]), 'path_link_missing', 'R1 和 R2 之间没有线路'),
        (_doc([_path('m1', 'm2', ['m3', 'r2'])]), 'path_via_not_relay', 'M3 不是中转'),
        (_doc([_path('m3', 'm2', ['r1', 'r2'])]), 'path_no_source', '起点 M3 的入口中转里没有 R1'),
        (_doc([_path('m1', 'm2', ['r1', 'r3'])]), 'path_no_destination', '终点 M2 的入口中转里没有 R3'),
        (_doc([_path('r1', 'm2', ['r3', 'r2'])], relay_mode='custom', pairs=[('r2', 'r3')]), 'path_no_source', '起点 R1 和 R3 之间没有线路'),
    ]
    for doc, code, text in cases:
        errors = model.validate(doc, facts)
        found = [e for e in errors if e['error'] == code]
        assert found and text in found[0]['message'], (code, errors)
        assert code in model.APPLY_ERRORS
        assert all('paths' not in c for c in build(doc, facts, 'net', 1).values())
    # An old relay on the route, an old source.
    doc = _doc([_path('m1', 'm2', ['r1', 'r2'])])
    assert 'path_node_outdated' in _codes(model.validate(doc, _facts(old={'r2': (MEMBERS_ROUTING_FEATURE,)})))
    err = next(e for e in model.validate(doc, _facts(old={'m1': (MEMBERS_ROUTING_FEATURE,)})) if e['error'] == 'path_no_source')
    assert '节点版本不支持自建路线' in err['message']
    # A node from before the probes and marks neither answers a route's probes nor follows its source off it.
    before_probes = (MEMBERS_ROUTING_FEATURE, 'mesh.routing.paths', PATHS_BACKUP_FEATURE)
    assert 'path_node_outdated' in _codes(model.validate(doc, _facts(old={'r1': before_probes})))
    # A member still on a single entry is no end of a route.
    doc['members'][3]['entry']['mode'] = 'failover'
    err = next(e for e in model.validate(doc, facts) if e['error'] == 'path_no_source')
    assert '要改成「同时连上」' in err['message']
    # If m1 and its upstreams (r1, r3) are updated, but another relay in the mesh (r2) is still old,
    # the message points to the un-updated relay in the mesh rather than blaming m1's own upstreams.
    doc2 = _doc([_path('m1', 'r3', ['r1'])])
    err = next(e for e in model.validate(doc2, _facts(old={'r2': ()})) if e['error'] == 'path_no_source')
    assert '组网里的中转节点需全部更新' in err['message']


def test_an_old_destination_takes_the_route_but_answers_by_the_routes():
    doc = _doc([_path('m1', 'm2', ['r1', 'r2'])])
    facts = _facts(old={'m2': (MEMBERS_ROUTING_FEATURE,)})
    configs = build(doc, facts, 'net', 1)
    assert 'paths' not in configs['m2'] and _ids(configs, 'r1') == ['p1']
    # It does not answer the source's probes either: the source goes by the links.
    assert [p.get('noProbe') for p in configs['m1']['paths']] == [True]
    assert 'path_end_skipped' in _codes(model.hints(doc, facts))
    assert not model.validate(doc, facts)
    configs = build(doc, _facts(), 'net', 1)
    assert not any(p.get('noProbe') for p in configs['m1']['paths'])


def test_a_disabled_route_keeps_its_place_and_does_nothing():
    doc = _doc([_path('m1', 'm2', ['r1', 'r2'], on=False), _path('m1', 'm2', ['r3', 'r2'])])
    configs = build(doc, _facts(), 'net', 1)
    assert _ids(configs, 'm1') == ['p2'] and 'paths' not in configs['r1']
    assert configs['m1']['paths'][0]['rank'] == 2
    plan = mesh_paths.plan(doc, _facts())
    assert [(e['rank'], e['used']) for e in plan] == [(1, False), (2, True)]
    assert not model.validate(doc, _facts())


def test_removing_a_machine_takes_its_routes_and_undoing_brings_them_back():
    doc = _doc([_path('m1', 'm2', ['r1', 'r2']), _path('m1', 'm3', ['r3']), _path('m1', 'm2', ['r3', 'r2'])])
    before = copy.deepcopy(doc)
    members = {m['serverId']: m for m in doc['members']}
    model.strip(doc, 'm2')
    members['m2']['removing'] = True
    assert [p['id'] for p in doc['paths']] == ['p2']
    model.strip(doc, 'r3')
    assert doc['paths'] == []
    # m2 is still being removed: the route through r3 to m2 stays out.
    model.unstrip(doc, before, 'r3')
    assert [p['id'] for p in doc['paths']] == ['p2']
    members['m2']['removing'] = False
    model.unstrip(doc, before, 'm2')
    assert doc['paths'] == before['paths']


def test_the_auto_links_keep_a_link_a_route_uses():
    doc = _doc([_path('m1', 'm2', ['r1', 'r2'])], relay_mode='custom', pairs=[('r1', 'r3'), ('r2', 'r3')])
    doc['relayLinks']['pairs'].append({'a': 'r1', 'b': 'r2', 'transport': 'tcp', 'dialer': 'a', 'bias': 0, 'origin': 'auto', 'reason': 'x', 'since': 1})
    assert frozenset(('r1', 'r2')) in mesh_paths.in_use(doc, _facts())
    doc['settings']['autoLinks'] = {**doc['settings']['autoLinks'], 'enabled': True, 'dropAfterS': 0}
    state = {'idleSince': {mesh_autolinks.pair_key('r1', 'r2'): 1}}
    out = mesh_autolinks.optimize(doc, _facts(), {}, {}, state=state, now_ms=10_000_000)
    assert not [d for d in out['dropped'] if {d['a'], d['b']} == {'r1', 'r2'}]


ROLES = {sid: 'relay' if sid.startswith('r') else 'member' for sid in IDS}
NOW = 1_800_000_000_000


def _status(paths):
    return {'m1': {'status': {'peers': [], 'paths': paths, 'routing': {'mode': 'dynamic', 'routes': [{'dest': 'm2', 'path': ['m1', 'r3', 'r2', 'm2']}],
                                                                     'links': []}}, 'at': NOW}}


@pytest.mark.parametrize('state', ['pinned', 'fallback'])
def test_the_path_query_shows_an_own_route(state):
    report = {'id': 'p1', 'nodes': ['m1', 'r1', 'r2', 'm2'], 'state': state, 'since': NOW - 5000, 'rank': 1}
    found = mesh_path.find('m1', 'm2', ROLES, _status([report]), {}, NOW)
    assert found['fixed']['state'] == state
    want = ['m1', 'r1', 'r2', 'm2'] if state == 'pinned' else ['m1', 'r3', 'r2', 'm2']
    assert [h['node'] for h in found['hops']] == want
    assert 'fixed' not in mesh_path.find('m1', 'm3', ROLES, _status([report]), {}, NOW)


def test_the_path_query_takes_the_route_in_use_else_the_first():
    first = {'id': 'p1', 'nodes': ['m1', 'r1', 'r2', 'm2'], 'state': 'fallback', 'since': 1, 'rank': 1, 'down': ['r1', 'r2']}
    second = {'id': 'p2', 'nodes': ['m1', 'r3', 'r1', 'r2', 'm2'], 'state': 'pinned', 'since': 2, 'rank': 2}
    found = mesh_path.find('m1', 'm2', ROLES, _status([first, second]), {}, NOW)
    assert found['fixed']['id'] == 'p2' and [h['node'] for h in found['hops']] == second['nodes']
    second = {**second, 'state': 'fallback'}
    found = mesh_path.find('m1', 'm2', ROLES, _status([second, first]), {}, NOW)
    assert found['fixed']['id'] == 'p1' and found['fixed']['down'] == ['r1', 'r2']


def test_a_node_report_keeps_its_routes_through_the_cleaning():
    raw = {'ready': True, 'peers': [], 'paths': [
        {'id': 'p1', 'nodes': ['m1', 'r1', 'r2', 'm2'], 'state': 'waiting', 'since': 5, 'until': 9, 'rank': 1, 'down': ['r1', 'r2'], 'extra': 'x'},
        {'id': 'p2', 'nodes': ['m1', 'r3', 'm2'], 'state': 'standby', 'since': 6, 'rank': 'two', 'down': ['r9', 'm2']},
        {'id': 'p1', 'nodes': ['m1', 'm2'], 'state': 'pinned'}, {'id': 'p3', 'nodes': ['m1', 'r1', 'm3'], 'state': 'odd'}, 'junk',
        {'id': 'p4', 'nodes': ['m1', 'r1', 'm3'], 'state': 'fallback', 'since': 7, 'rank': 1, 'silent': True},
        {'id': 'p5', 'nodes': ['m1', 'r2', 'm3'], 'state': 'pinned', 'since': 8, 'rank': 2, 'silent': True}]}
    status = mesh_status.clean_status(raw)
    assert status['paths'] == [{'id': 'p1', 'nodes': ['m1', 'r1', 'r2', 'm2'], 'state': 'waiting', 'since': 5, 'until': 9, 'rank': 1, 'down': ['r1', 'r2']},
                               {'id': 'p2', 'nodes': ['m1', 'r3', 'm2'], 'state': 'standby', 'since': 6},
                               {'id': 'p4', 'nodes': ['m1', 'r1', 'm3'], 'state': 'fallback', 'since': 7, 'rank': 1, 'silent': True},
                               {'id': 'p5', 'nodes': ['m1', 'r2', 'm3'], 'state': 'pinned', 'since': 8, 'rank': 2}]
    found = mesh_path.find('m1', 'm2', ROLES, {'m1': {'status': status, 'at': NOW}}, {}, NOW)
    assert found['fixed']['state'] == 'waiting' and found['fixed']['until'] == 9


def test_the_view_shows_each_link_and_where_the_route_is_each_way():
    doc = _doc([_path('m1', 'm2', ['r1', 'r2'])], relay_mode='custom', pairs=[('r1', 'r3'), ('r2', 'r3')])
    statuses = {'r1': {'status': {'routing': {'links': [{'peer': 'r3', 'rttMs': 12.5}]}}, 'at': NOW},
                'm1': {'status': {'paths': [{'id': 'p1', 'nodes': ['m1', 'r1', 'r2', 'm2'], 'state': 'pinned', 'since': NOW - 1, 'rank': 1}],
                                  'routing': {'links': [{'peer': 'r1', 'rttMs': 3.0}]}}, 'at': NOW}}
    v = mesh_paths.view(doc, _facts(), statuses, NOW)[0]
    assert v['segments'] == [{'a': 'm1', 'b': 'r1', 'kind': 'upstream', 'exists': True, 'member': 'm1', 'rttMs': 3.0},
                             {'a': 'r1', 'b': 'r2', 'kind': 'relay', 'exists': False, 'rttMs': None},
                             {'a': 'r2', 'b': 'm2', 'kind': 'upstream', 'exists': True, 'member': 'm2', 'rttMs': None}]
    assert [p['error'] for p in v['problems']] == ['path_link_missing'] and not v['used'] and 'states' not in v
    assert v['label'] == 'M1 → R1 → R2 → M2' and v['rank'] == 1
    doc['relayLinks']['pairs'].append({'a': 'r1', 'b': 'r2', 'transport': 'tcp', 'dialer': 'a', 'bias': 0})
    v = mesh_paths.view(doc, _facts(), statuses, NOW)[0]
    assert v['problems'] == [] and v['used']
    assert v['states'] == {'to': {'state': 'pinned', 'since': NOW - 1}, 'back': {'state': 'unknown'}}
    statuses['m1']['status']['paths'][0].update(state='fallback', silent=True)
    assert mesh_paths.view(doc, _facts(), statuses, NOW)[0]['states']['to'] == {'state': 'fallback', 'since': NOW - 1, 'silent': True}
    # A destination not entering by the last relay: shown, with the member to add it to.
    doc['paths'][0]['to'] = 'm3'
    v = mesh_paths.view(doc, _facts(), statuses, NOW)[0]
    assert v['segments'][-1] == {'a': 'r2', 'b': 'm3', 'kind': 'upstream', 'exists': False, 'member': 'm3', 'rttMs': None}
    assert [p['error'] for p in v['problems']] == ['path_no_destination']


def _links(*links):
    """Reports where each (a, b, cost) is up as both ends see it; rtt is the cost less a tenth."""
    statuses = {}
    for a, b, cost in links:
        for x, y in ((a, b), (b, a)):
            statuses.setdefault(x, {'status': {'peers': [], 'routing': {'links': []}}, 'at': NOW})
            statuses[x]['status']['routing']['links'].append({'peer': y, 'state': 'up', 'cost': cost, 'rttMs': round(cost - 0.1, 1)})
    return statuses


def test_the_best_routes_go_by_cost_through_relays_alone():
    # m1 enters by r1 and r3, m2 by r2 and m3 by r3; m3 is also linked to r2, and m1 to m3: members forward nothing.
    statuses = _links(('m1', 'r1', 2), ('m1', 'r3', 5), ('r1', 'r2', 10), ('r1', 'r3', 1), ('r3', 'r2', 20), ('r2', 'm2', 3),
                      ('m3', 'r3', 1), ('m3', 'r2', 1), ('m1', 'm3', 1))
    out = mesh_path.options('m1', 'm2', ROLES, statuses, NOW, 10)
    assert [r['nodes'] for r in out['routes']] == [['m1', 'r1', 'r2', 'm2'], ['m1', 'r3', 'r1', 'r2', 'm2'], ['m1', 'r1', 'r3', 'r2', 'm2'],
                                                 ['m1', 'r3', 'r2', 'm2']]
    first = out['routes'][0]
    assert first['rttMs'] == 14.7 and first['own'] is None and out['own'] == []
    assert first['segments'][0] == {'a': 'm1', 'b': 'r1', 'exists': True, 'rttMs': 1.9}
    assert [r['nodes'] for r in mesh_path.options('m1', 'm2', ROLES, statuses, NOW, 2)['routes']] == [r['nodes'] for r in out['routes'][:2]]
    # A link one end reports down is no link: r2 says so of r1.
    statuses['r2']['status']['routing']['links'][0]['state'] = 'down'
    assert [r['nodes'] for r in mesh_path.options('m1', 'm2', ROLES, statuses, NOW, 10)['routes']] == [['m1', 'r1', 'r3', 'r2', 'm2'], ['m1', 'r3', 'r2', 'm2']]


def test_the_best_routes_show_the_pairs_own_routes():
    statuses = _links(('m1', 'r1', 2), ('r1', 'r2', 10), ('r2', 'm2', 3), ('r1', 'r3', 1), ('r3', 'r2', 1))
    own = [{'id': 'p1', 'rank': 1, 'on': True, 'nodes': ['m1', 'r1', 'r2', 'm2']},
           {'id': 'p2', 'rank': 2, 'on': True, 'nodes': ['m1', 'r3', 'r2', 'm2']}]
    out = mesh_path.options('m1', 'm2', ROLES, statuses, NOW, 10, own)
    assert [(r['nodes'], r['own']) for r in out['routes']] == [(['m1', 'r1', 'r3', 'r2', 'm2'], None), (['m1', 'r1', 'r2', 'm2'], 1)]
    assert out['own'][0]['place'] == 2 and out['own'][0]['rttMs'] == 14.7
    # The loss either end measures shows on the segment.
    statuses['r2']['status']['routing']['links'][0]['loss'] = 0.02
    assert mesh_path.options('m1', 'm2', ROLES, statuses, NOW, 10, own)['own'][0]['segments'][1] == {'a': 'r1', 'b': 'r2', 'exists': True, 'rttMs': 9.9, 'loss': 0.02}
    # m1 to r3 is not linked yet: the route waits for it, out of the ranking.
    assert out['own'][1]['place'] is None and out['own'][1]['rttMs'] is None
    assert [s['exists'] for s in out['own'][1]['segments']] == [False, True, True]


def test_a_member_on_a_single_entry_and_a_direct_link_are_routes_too():
    statuses = _links(('r1', 'r2', 10), ('r2', 'm2', 3))
    statuses['m1'] = {'status': {'peers': [{'id': 'r1', 'kind': 'entry', 'active': True, 'rttMs': 4.0},
                                           {'id': 'm2', 'kind': 'direct', 'active': True, 'rttMs': 30.0, 'direct': {'state': 'direct'}}],
                                 'entry': {'relay': 'r1', 'candidates': []}}, 'at': NOW}
    out = mesh_path.options('m1', 'm2', ROLES, statuses, NOW, 10)
    assert [(r['nodes'], r['rttMs']) for r in out['routes']] == [(['m1', 'r1', 'r2', 'm2'], 16.8), (['m1', 'm2'], 30.0)]
