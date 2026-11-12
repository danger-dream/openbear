"""Through the Web app: the kept revisions and going back to any of them, measuring the relays
before a server joins, and what a newcomer leaves to the mesh."""
from __future__ import annotations

from app.servers import mesh as mesh_module
from tests.test_mesh_service import applied_base, mesh_setup, server, settled
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


async def test_revisions_are_listed_and_any_one_comes_back_as_a_new_revision(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    for limit in (5, 7):
        draft['members'][1]['uploadLimitMbps'] = limit
        await api(e, 'PUT', '/api/servers/mesh/draft', draft)
        await api(e, 'POST', '/api/servers/mesh/apply')
        await settled(e)
    listed = (await api(e, 'GET', '/api/servers/mesh/revisions'))['revisions']
    assert [r['revision'] for r in listed] == [3, 2, 1] and [r['current'] for r in listed] == [True, False, False]
    assert all(r['status'] == 'applied' and r['reason'] and r['at'] for r in listed)
    # What each changed from the one before it.
    assert listed[0]['changes'] == listed[1]['changes'] == ['修改 公司-Win 的发送速率'] and listed[2]['changes'] == []

    await api(e, 'POST', '/api/servers/mesh/rollback', {'revision': 1})
    state = await settled(e)
    assert state['applied']['revision'] == 4 and state['members'][1]['uploadLimitMbps'] is None
    assert nodes.configs[ids['m1']]['revision'] == 4 and nodes.configs[ids['m1']]['self']['uploadLimitMbps'] is None
    # The applied one, or one not kept, is refused and changes nothing.
    current = await api(e, 'POST', '/api/servers/mesh/rollback', {'revision': 4}, status=409)
    missing = await api(e, 'POST', '/api/servers/mesh/rollback', {'revision': 99}, status=404)
    assert (current['error'], missing['error']) == ('mesh_revision_current', 'mesh_revision_not_found')
    assert (await api(e, 'POST', '/api/servers/mesh/rollback', {'revision': '2'}, status=400))['error'] == 'invalid_mesh_revision'
    assert (await api(e, 'GET', '/api/servers/mesh'))['applied']['revision'] == 4


async def test_a_server_about_to_join_measures_every_relay(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    newcomer = await server(e, 'Edge-SG', ipv4='45.32.100.9')
    asked = []
    real = nodes.call

    async def call(sid, method, params, timeout):
        if method == 'mesh.probe':
            asked.append((sid, params))
            return {'ok': True, 'result': {'results': [{'id': t['id'], 'rttMs': 12.5, 'loss': 0.25, 'udp': 'ok', 'tcp': 'refused', 'tls': 'ok'}
                                                       for t in params['targets']]}}
        return await real(sid, method, params, timeout)
    e.servers.hub.call = call
    got = await api(e, 'POST', '/api/servers/mesh/probe', {'serverId': newcomer})
    # Berlin listens on UDP and TCP (a relay left to the mesh): TLS it does not, whatever the node says.
    assert got['results'] == [{'relay': ids['r1'], 'rttMs': 12.5, 'loss': 0.25, 'udp': 'ok', 'tcp': 'refused', 'tls': 'closed'}]
    target = asked[0][1]['targets'][0]
    assert asked[0][0] == newcomer and target['id'] == ids['r1'] and target['host'] == '45.32.100.1'
    assert (target['udp'], target['tcp'], target['tls']) == (51999, 52000, 0)
    # A node too old for it says so.
    e.servers.hub.call = real
    assert (await api(e, 'POST', '/api/servers/mesh/probe', {'serverId': newcomer}, status=409))['error'] == 'node_update_required'
    assert (await api(e, 'POST', '/api/servers/mesh/probe', {'serverId': 'nope'}, status=404))['error'] == 'server_not_found'


async def test_the_route_editor_measures_each_link_now(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    asked = []
    real = nodes.call

    async def call(sid, method, params, timeout):
        if method == 'mesh.probe':
            asked.append((sid, [t['id'] for t in params['targets']]))
            return {'ok': True, 'result': {'results': [{'id': t['id'], 'rttMs': 30.04, 'loss': 0.25, 'udp': 'ok', 'tcp': 'ok', 'tls': 'closed'}
                                                       for t in params['targets']]}}
        return await real(sid, method, params, timeout)
    e.servers.hub.call = call
    # 公司-Win reports its entry Berlin; 家-Mac reports nothing, so its link is measured from it now.
    await e.servers.mesh.report(ids['m1'], {'schema': 1, 'startedAt': 5, 'appliedRevision': 1, 'ready': True, 'iface': 'obmesh0', 'error': '',
                                            'peers': [{'id': ids['r1'], 'kind': 'entry', 'active': True, 'transport': 'udp', 'rttMs': 151.2}],
                                            'entry': {'candidate': 'c1', 'relay': ids['r1'], 'transport': 'udp', 'since': 1, 'switches': 0}, 'events': []})
    route = [ids['m1'], ids['r1'], ids['m2']]
    got = await api(e, 'POST', '/api/servers/mesh/path/measure', {'nodes': route})
    assert got['segments'] == [{'a': ids['m1'], 'b': ids['r1'], 'exists': True, 'rttMs': 151.2, 'loss': 0, 'how': 'report'},
                               {'a': ids['r1'], 'b': ids['m2'], 'exists': False, 'rttMs': 30.0, 'loss': 0.25, 'how': 'probe'}]
    assert got['rttMs'] == 181.2 and asked == [(ids['m2'], [ids['r1']])]
    # Measured a moment ago: not again.
    assert (await api(e, 'POST', '/api/servers/mesh/path/measure', {'nodes': route}))['rttMs'] == 181.2 and len(asked) == 1
    # Two members are no link of a route; the reason says so.
    seg = (await api(e, 'POST', '/api/servers/mesh/path/measure', {'nodes': [ids['m1'], ids['m2']]}))['segments'][0]
    assert seg['how'] == 'none' and seg['rttMs'] is None and '都不是中转' in seg['why']
    # A relay on TLS alone is timed by TCP connects to its TLS port; what joins measures it as it listens.
    tls_only = {'serverId': 'x', 'role': 'relay', 'relayMode': 'public', 'publicEndpoint': '203.0.113.9', 'listen': {'udp': 0, 'tcp': 0, 'tls': 52001}}
    assert [(t['udp'], t['tcp'], t['tls']) for t in (mesh_module._probe_target(tls_only, {}, timed=True), mesh_module._probe_target(tls_only, {}))] == [(0, 52001, 52001), (0, 0, 52001)]
    for bad, status, code in (([ids['m1']], 400, 'invalid_mesh_path'), ([ids['m1'], ids['m1']], 400, 'invalid_mesh_path'), ([ids['m1'], 'nope'], 404, 'mesh_member_not_found')):
        assert (await api(e, 'POST', '/api/servers/mesh/path/measure', {'nodes': bad}, status=status))['error'] == code


async def test_a_newcomer_leaves_its_ports_and_relay_type_to_the_mesh(env):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]})
    assert [m['auto'] for m in added['members']] == [['listen', 'relayMode']] * 2
    assert added['members'][1]['entry']['candidates'] == []
    members = added['members']
    members[0]['role'] = 'relay'
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members})
    assert saved['members'][0]['listen'] == {'udp': 51999, 'tcp': 52000, 'tls': 0}
    assert saved['auto']['listen'][ids['r1']]['reason'] == '公网中转常开 UDP 和 TCP，新节点接入时用'
    # Set by hand: out of auto, kept as sent.
    members = saved['members']
    members[0].update(listen={'udp': 51999, 'tcp': 0, 'tls': 0}, auto=['relayMode'])
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members})
    assert saved['members'][0]['listen'] == {'udp': 51999, 'tcp': 0, 'tls': 0} and saved['members'][0]['auto'] == ['relayMode']


async def test_line_rules_and_exit_groups_are_kept_and_a_discard_brings_them_back(env):  # noqa: F811
    e = env
    nodes, ids, draft = await applied_base(e)
    exit_id = draft['members'][0]['serverId']
    # An exit needs a node that runs the proxy.
    session = e.servers.hub.sessions[exit_id]
    session.features = frozenset({*session.features, 'mesh.proxy.entries'})
    draft['members'][0]['proxy'] = {'exit': {'user': 'us'}, 'ports': []}
    draft['exitGroups'] = [{'name': 'US', 'members': [exit_id]}]
    draft['lineRules'] = [{'name': 'tls', 'on': True, 'applies': 'lines', 'a': {'kind': 'any'}, 'b': {'kind': 'any'}, 'order': ['tls'], 'bias': None, 'dialer': None}]
    await api(e, 'PUT', '/api/servers/mesh/draft', draft)
    # Read back from the database, not from what was sent.
    saved = await api(e, 'GET', '/api/servers/mesh')
    assert [(g['id'], g['name']) for g in saved['exitGroups']] == [('g1', 'US')] and [r['name'] for r in saved['lineRules']] == ['tls']
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert not state['draft']['dirty'] and [g['name'] for g in state['exitGroups']] == ['US']
    # A change to them, then discarded: back to the applied ones, nothing left over.
    state['exitGroups'][0]['name'] = 'USA'
    state['lineRules'] = []
    await api(e, 'PUT', '/api/servers/mesh/draft', {k: state[k] for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules', 'lineRules', 'exitGroups')})
    assert (await api(e, 'GET', '/api/servers/mesh'))['draft']['dirty']
    await api(e, 'POST', '/api/servers/mesh/discard')
    back = await api(e, 'GET', '/api/servers/mesh')
    assert not back['draft']['dirty'] and [g['name'] for g in back['exitGroups']] == ['US'] and [r['name'] for r in back['lineRules']] == ['tls']
