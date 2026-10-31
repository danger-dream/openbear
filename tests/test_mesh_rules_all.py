"""Rules on all members (src.all / dst.all) and node events folded when they repeat: the
document and config side as pure functions, then through the real Web app with fake nodes."""
from __future__ import annotations

import copy
import json

import pytest

from app.servers import mesh as mesh_module
from app.servers import mesh_model as model
from app.servers import mesh_status
from app.servers.mesh_config import build, config_hash, exports, gateway_routes
from app.webhooks.repository import one
from tests.test_mesh_config import doc, facts, member, own_keys
from tests.test_mesh_service import mesh_setup, settled
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


def rule(src=None, dst=None, proto='any', note=''):
    return {'id': '', 'src': {'all': False, 'allSubnets': False, 'members': [], 'tags': [], 'prefixes': [], **(src or {})},
            'dst': {'all': False, 'allSubnets': False, 'members': [], 'tags': [], 'subnets': [], **(dst or {})}, 'proto': proto, 'ports': [],
            'note': note}


def mesh(*rules, extra=()):
    """r1 100.96.0.1, m1 .2 (tag 公司), gw .3 announcing 192.168.1.0/24, m2 .4 announcing 10.20.20.0/24, r2 .5, then extra."""
    d = doc(member('r1', 'relay'), member('m1', tags=['公司']), member('gw', gateway={'subnets': ['192.168.1.0/24'], 'masquerade': True}),
            member('m2', gateway={'subnets': ['10.20.20.0/24'], 'masquerade': True}), member('r2', 'relay'), *extra, rules=list(rules))
    d['rules'] = [{**r, 'id': f'r{i + 1}'} for i, r in enumerate(d['rules'])]
    return d


def everyone(d):
    return [f"{m['address']}/32" for m in model.active(d)]


def clean_rules(rules):
    return model.clean_doc({'rules': rules}, model.empty_doc())['rules']


# ----- shape -----
def test_all_is_cleaned_with_members_and_tags_dropped_and_nets_kept():
    [r] = clean_rules([{'src': {'all': True, 'members': ['m1'], 'tags': ['公司'], 'prefixes': ['192.168.1.0/24']},
                        'dst': {'all': True, 'tags': ['x'], 'subnets': ['10.20.20.0/24']}, 'proto': 'icmp'}])
    assert r['src'] == {'all': True, 'allSubnets': False, 'members': [], 'tags': [], 'prefixes': ['192.168.1.0/24']}
    assert r['dst'] == {'all': True, 'allSubnets': False, 'members': [], 'tags': [], 'subnets': ['10.20.20.0/24']}
    # Default false; an older side without it reads as false.
    [r] = clean_rules([{'src': {'members': ['m1']}, 'dst': {'tags': ['公司']}}])
    assert r['src']['all'] is False and r['dst']['all'] is False and r['src']['members'] == ['m1']
    with pytest.raises(model.MeshInvalid) as bad:
        clean_rules([{'src': {'all': 'yes'}, 'dst': {'all': 1}}])
    assert {(e['path'], e['error']) for e in bad.value.errors} == {('rules[0].src.all', 'invalid_rule_side'), ('rules[0].dst.all', 'invalid_rule_side')}


def test_all_counts_as_a_side_and_needs_no_known_members_or_tags():
    d = mesh(rule({'all': True}, {'all': True}, proto='icmp'))
    assert not [e for e in model.validate(d, facts()) if e['path'].startswith('rules')]
    # Members or tags given with all are dropped on save, so unknown ones are no error either.
    d['rules'] = clean_rules([{'src': {'all': True, 'members': ['gone'], 'tags': ['nobody']}, 'dst': {'all': True}}])
    assert not [e for e in model.validate(d, facts()) if e['path'].startswith('rules')]
    d['rules'] = clean_rules([{'src': {'all': False}, 'dst': {'all': False}}])
    assert {(e['path'], e['error']) for e in model.validate(d, facts())} >= {('rules[0].src', 'rule_src_empty'), ('rules[0].dst', 'rule_dst_empty')}
    # dst.subnets next to all are still checked against the gateways.
    d['rules'] = clean_rules([{'src': {'all': True}, 'dst': {'all': True, 'subnets': ['172.31.0.0/24']}}])
    assert ('rules[0].dst.subnets[0]', 'rule_subnet_not_gateway') in {(e['path'], e['error']) for e in model.validate(d, facts())}


def test_an_older_stored_rule_reads_unchanged():
    d = mesh(rule({'members': ['m1']}, {'tags': ['公司']}))
    configs = build(d, facts(), 'net', 1)
    stored = copy.deepcopy(d)
    for r in stored['rules']:
        for side in ('src', 'dst'):
            del r[side]['all'], r[side]['allSubnets']
    upgraded = model.upgrade(stored)
    assert upgraded == d
    assert {k: config_hash(v) for k, v in build(upgraded, facts(), 'net', 1).items()} == {k: config_hash(v) for k, v in configs.items()}


# ----- expansion -----
def test_all_expands_to_every_member_and_follows_joins_and_leaves():
    d = mesh(rule({'all': True}, {'all': True}, proto='icmp'))
    configs = build(d, facts(), 'net', 1)
    for sid, c in configs.items():
        me = next(m for m in d['members'] if m['serverId'] == sid)
        # Every member's own mesh address only: dst.all is not the network behind a gateway.
        assert c['rules'] == [{'src': everyone(d), 'dst': [f"{me['address']}/32"], 'proto': 'icmp', 'ports': []}]
    # A member joining later is covered with nothing to edit, local too.
    joined = mesh(rule({'all': True}, {'all': True}, proto='icmp'), extra=(member('m3'), member('local')))
    configs = build(joined, {**facts(), 'm3': {**facts()['m1'], **own_keys('m3')}, 'local': {**facts()['m2'], **own_keys('local')}}, 'net', 1)
    assert set(configs) == {'r1', 'm1', 'gw', 'm2', 'r2', 'm3', 'local'}
    assert configs['local']['rules'][0]['dst'] == ['100.96.0.7/32'] and '100.96.0.7/32' in configs['m1']['rules'][0]['src']
    assert '100.96.0.6/32' in configs['local']['rules'][0]['src']
    # One on its way out is no longer in it, and nothing has to be cleaned out of the rule.
    joined['members'][5]['removing'] = True
    stripped = model.strip(copy.deepcopy(joined), 'm3')
    assert stripped['rules'] == joined['rules']
    configs = build(stripped, {**facts(), 'm3': facts()['m1'], 'local': facts()['m2']}, 'net', 1)
    assert 'm3' not in configs and all('100.96.0.6/32' not in r['src'] for c in configs.values() for r in c['rules'])


def test_all_is_a_union_with_members_tags_prefixes_and_subnets():
    # src.all with a prefix: the members plus the network behind gw; dst.all with a subnet: gw's subnet besides each member.
    d = mesh(rule({'all': True, 'prefixes': ['192.168.1.0/24']}, {'members': ['m1']}),
             rule({'members': ['r2']}, {'all': True, 'subnets': ['192.168.1.0/24']}),
             rule({'tags': ['公司']}, {'all': True}))
    configs = build(d, facts(), 'net', 1)
    assert configs['m1']['rules'][0] == {'src': [*everyone(d), '192.168.1.0/24'], 'dst': ['100.96.0.2/32'], 'proto': 'any', 'ports': []}
    assert configs['gw']['rules'][0] == {'src': ['100.96.0.5/32'], 'dst': ['100.96.0.3/32', '192.168.1.0/24'], 'proto': 'any', 'ports': []}
    assert configs['m2']['rules'][0] == {'src': ['100.96.0.5/32'], 'dst': ['100.96.0.4/32'], 'proto': 'any', 'ports': []}
    assert all(c['rules'][-1] == {'src': ['100.96.0.2/32'], 'dst': [f"{m['address']}/32"], 'proto': 'any', 'ports': []}
               for m in d['members'] for sid, c in configs.items() if sid == m['serverId'])


# ----- gateway routes -----
def test_gateway_routes_and_exports_with_all():
    # dst.all alone opens no gateway subnet: nobody gets a route.
    d = mesh(rule({'all': True}, {'all': True}))
    assert gateway_routes(d) == {sid: [] for sid in ('r1', 'm1', 'gw', 'm2', 'r2')}
    assert all(not e['nodes'] for e in exports(d))
    # src.all to a subnet: every member but its own gateway gets the route.
    d = mesh(rule({'all': True}, {'subnets': ['192.168.1.0/24']}))
    assert gateway_routes(d) == {'r1': ['192.168.1.0/24'], 'm1': ['192.168.1.0/24'], 'gw': [], 'm2': ['192.168.1.0/24'], 'r2': ['192.168.1.0/24']}
    assert exports(d)[0] == {'gateway': 'gw', 'subnet': '192.168.1.0/24', 'nodes': ['r1', 'm1', 'm2', 'r2']}
    # The network behind gw to all members: every one needs the route back to answer it.
    d = mesh(rule({'prefixes': ['192.168.1.0/24']}, {'all': True}))
    assert exports(d) == [{'gateway': 'gw', 'subnet': '192.168.1.0/24', 'nodes': ['r1', 'm1', 'm2', 'r2']},
                          {'gateway': 'm2', 'subnet': '10.20.20.0/24', 'nodes': []}]
    # A member joining later gets it too; one leaving does not.
    d = mesh(rule({'all': True}, {'subnets': ['10.20.20.0/24']}), extra=(member('m3'),))
    assert exports(d)[1]['nodes'] == ['r1', 'm1', 'gw', 'r2', 'm3']
    d['members'][5]['removing'] = True
    assert exports(d)[1]['nodes'] == ['r1', 'm1', 'gw', 'r2']
    # A machine that already routes the subnet elsewhere (local over a VPN) is still left out.
    held = {**facts(), 'm1': {**facts()['m1'], 'hostRoutes': [{'net': '10.20.20.0/24', 'dev': 'wg9'}]}}
    assert gateway_routes(d, held)['m1'] == []


# ----- events -----
def ev(seq, at, kind='path.ready', detail='c3'):
    return {'seq': seq, 'at': at, 'kind': kind, 'detail': detail}


def test_repeated_events_fold_and_the_limit_counts_folded_ones():
    out = mesh_status.merge_events([ev(1, 10), ev(2, 20)], [ev(3, 30), ev(4, 40, 'peer.down', 'r1'), ev(5, 50), ev(6, 60, detail='c2')])
    assert out == [{'seq': 3, 'at': 30, 'kind': 'path.ready', 'detail': 'c3', 'firstAt': 10, 'repeat': 3},
                   ev(4, 40, 'peer.down', 'r1'), ev(5, 50), ev(6, 60, detail='c2')]
    # A folded one folds on.
    again = mesh_status.merge_events(out, [ev(7, 70, detail='c2')])
    assert again[-1] == {**ev(7, 70, detail='c2'), 'firstAt': 60, 'repeat': 2}
    assert mesh_status.merge_events(again, [])[-1]['repeat'] == 2
    # A minute's repeats for hours stay one event; the limit counts folded events.
    kept = [ev(i, i) if i % 2 else ev(i, i, 'peer.up', str(i)) for i in range(150)]
    noisy = mesh_status.merge_events(kept, [ev(1000 + i, 1000 + i) for i in range(500)])
    assert len(noisy) == mesh_status.EVENTS_KEPT and noisy[-1]['repeat'] == 501 and noisy[-1]['firstAt'] == 149
    assert noisy[0] == ev(50, 50, 'peer.up', '50')


async def test_rules_on_all_say_so_in_changes(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]})
    draft = await api(e, 'GET', '/api/servers/mesh')
    draft['members'][0]['role'] = 'relay'
    rules = [{'src': {'all': True, 'members': [ids['m1']]}, 'dst': {'all': True}, 'proto': 'icmp', 'note': 'ping'},
             {'src': {'members': [ids['m1']]}, 'dst': {'members': [ids['r1']]}}]
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members'], 'rules': rules})
    assert saved['errors'] == [] and saved['rules'][0]['src'] == {'all': True, 'allSubnets': False, 'members': [], 'tags': [], 'prefixes': []}
    assert [c['text'] for c in saved['draft']['changes'] if c['kind'] == 'member.add'] == [
        '新增成员 Berlin，规则 ping 会自动包含它', '新增成员 公司-Win，规则 ping 会自动包含它']
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert nodes.configs[ids['m1']]['rules'][0]['src'] == ['100.96.0.1/32', '100.96.0.2/32']
    # Joining later: covered with nothing to edit, and said so.
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['m2']]})
    assert [c['text'] for c in added['draft']['changes']] == ['新增成员 家-Mac，规则 ping 会自动包含它']
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert nodes.configs[ids['m1']]['rules'][0]['src'] == ['100.96.0.1/32', '100.96.0.2/32', '100.96.0.3/32']
    assert nodes.configs[ids['m2']]['rules'][0] == {'src': ['100.96.0.1/32', '100.96.0.2/32', '100.96.0.3/32'], 'dst': ['100.96.0.3/32'],
                                                     'proto': 'icmp', 'ports': []}
    # Leaving: the rule needs no cleaning and stops covering it.
    left = await api(e, 'DELETE', f'/api/servers/mesh/members/{ids["m2"]}')
    assert left['rules'][0] == saved['rules'][0]
    assert [c['text'] for c in left['draft']['changes'] if c['kind'] == 'member.remove'] == ['移除成员 家-Mac，规则 ping 不再包含它']
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    assert nodes.configs[ids['m1']]['rules'][0]['src'] == ['100.96.0.1/32', '100.96.0.2/32']


async def test_repeated_node_events_are_folded_stored_and_returned(env, monkeypatch):  # noqa: F811
    e = env
    # Every report saved, as if far enough apart (see test_join_edit_apply_in_order_and_report_status).
    monkeypatch.setattr(mesh_module, 'STATUS_SAVE_MIN_MS', 0)
    nodes, ids = await mesh_setup(e)
    sid = ids['m1']
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], sid]})
    draft = await api(e, 'GET', '/api/servers/mesh')
    draft['members'][0]['role'] = 'relay'
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members']})
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    report = {'schema': 1, 'startedAt': 5, 'appliedRevision': 1, 'ready': True, 'iface': 'obmesh0', 'error': '', 'peers': [], 'entry': None,
              'resources': {}}
    await e.servers.mesh.report(sid, {**report, 'events': [ev(1, 1000, 'peer.up', 'r1'), ev(2, 60_000)]})
    for i in range(3, 300):
        await e.servers.mesh.report(sid, {**report, 'events': [ev(i, i * 60_000)]})
    await e.servers.mesh.report(sid, {**report, 'events': [ev(300, 300 * 60_000, 'peer.down', 'r1')]})
    detail = await api(e, 'GET', f'/api/servers/mesh/members/{sid}')
    assert detail['events'] == [ev(300, 300 * 60_000, 'peer.down', 'r1'),
                                {**ev(299, 299 * 60_000), 'firstAt': 60_000, 'repeat': 298}, ev(1, 1000, 'peer.up', 'r1')]
    stored = json.loads((await one(e.db.conn, 'SELECT events_json FROM mesh_nodes WHERE server_id=?', (sid,)))['events_json'])
    assert stored == list(reversed(detail['events']))
    # A node's next events still come through by seq.
    await e.servers.mesh.report(sid, {**report, 'events': [ev(300, 1, 'peer.down', 'r1'), ev(301, 301 * 60_000, 'peer.up', 'r1')]})
    assert [x['seq'] for x in (await api(e, 'GET', f'/api/servers/mesh/members/{sid}'))['events']] == [301, 300, 299, 1]

    # History stored before folding is folded when read back, and stored so on the next write.
    old = [ev(i, i * 60_000) for i in range(1, 101)]
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('UPDATE mesh_nodes SET events_json=? WHERE server_id=?', (json.dumps(old), sid))
    e.servers.mesh.status.clear()
    await e.servers.mesh.start()
    detail = await api(e, 'GET', f'/api/servers/mesh/members/{sid}')
    assert detail['events'] == [{**ev(100, 100 * 60_000), 'firstAt': 60_000, 'repeat': 100}]
    await e.servers.mesh.report(sid, {**report, 'startedAt': 6, 'events': [ev(1, 101 * 60_000, 'peer.up', 'r1')]})
    stored = json.loads((await one(e.db.conn, 'SELECT events_json FROM mesh_nodes WHERE server_id=?', (sid,)))['events_json'])
    assert stored == [{**ev(100, 100 * 60_000), 'firstAt': 60_000, 'repeat': 100}, ev(1, 101 * 60_000, 'peer.up', 'r1')]
    # The tool passes them on.
    from app.tools.servers import _mesh_member
    assert _mesh_member(detail)['events'][0]['repeat'] == 100
