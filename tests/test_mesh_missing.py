"""A member whose mesh service is gone (the node was uninstalled or installed again over it):
found on reconnect or by a refused delivery, said once, skipped by rollouts and upgrades, and
put back by mesh_reinstall with a new revision of the applied document. Real Web app,
isolated database, fake nodes."""
from __future__ import annotations

from app.servers import mesh as mesh_module
from app.servers import mesh_status
from app.webhooks.repository import many, one
from tests.test_mesh_service import connect, mesh_setup, notifications, settled
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


def gone_nodes(nodes):
    """Nodes in gone answer as one without its mesh service; mesh.install puts it back with new keys."""
    gone, real = set(), nodes.call

    async def call(server_id, method, params, timeout):
        if server_id in gone and server_id in nodes.e.servers.hub.sessions:
            if method == 'mesh.status':
                nodes.calls.append((server_id, method, params))
                return {'ok': True, 'result': {'installed': False, 'service': 'absent', 'localPrefixes': [], 'dataVersion': '', 'dataBuild': ''}}
            if method in ('mesh.apply', 'mesh.upgrade', 'mesh.tls'):
                nodes.calls.append((server_id, method, params))
                return {'ok': False, 'error': 'mesh_not_installed'}
            if method == 'mesh.install':
                gone.discard(server_id)
                reply = await real(server_id, method, params, timeout)
                reply['result'].update(publicKey=(server_id[:8] + 'new').ljust(43, 'A') + '=', controlKey=(server_id[:8] + 'nc').ljust(43, 'B') + '=')
                return reply
        return await real(server_id, method, params, timeout)
    nodes.e.servers.hub.call = call
    return gone


async def meshed(e):
    nodes, ids = await mesh_setup(e)
    gone = gone_nodes(nodes)
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1'], ids['m2']]})
    members = added['members']
    members[0].update(role='relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0})
    rules = [{'src': {'all': True}, 'dst': {'all': True}, 'proto': 'icmp', 'note': 'ping'}]
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members, 'rules': rules})
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['applied']['revision'] == 1 and all(p['state'] == 'done' for p in state['apply']['progress'])
    return nodes, ids, gone


async def texts(e):
    return [r['text'] for r in await many(e.db.conn, "SELECT text FROM server_notifications WHERE kind='mesh' ORDER BY created_ms")]


async def test_a_lost_mesh_service_is_found_on_reconnect_said_once_and_skipped(env):  # noqa: F811
    e = env
    nodes, ids, gone = await meshed(e)
    m1 = ids['m1']
    # The node was installed again over it (as another server, then back): it reconnects without the service.
    gone.add(m1)
    await connect(e, m1)
    state = await settled(e)
    assert state['nodes'][m1]['state'] == 'missing' and state['nodes'][m1]['error'] == 'mesh_service_missing'
    assert await texts(e) == ['组网：「公司-Win」上的组网服务不见了（可能被卸载或重装过），点「重新安装组网服务」恢复。']
    member = await api(e, 'GET', f'/api/servers/mesh/members/{m1}')
    assert member['node']['state'] == 'missing' and member['events'][0]['kind'] == 'mesh.missing'
    # Reconnecting again says nothing more.
    await connect(e, m1)
    await settled(e)
    assert len(await notifications(e)) == 1

    # Nothing goes to it: a rollout marks it missing and goes on, with no failure and no reminder.
    nodes.calls.clear()
    draft = await api(e, 'GET', '/api/servers/mesh')
    draft['rules'].append({'src': {'all': True}, 'dst': {'all': True}, 'proto': 'udp'})
    await api(e, 'PUT', '/api/servers/mesh/draft', {'rules': draft['rules']})
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    progress = {p['serverId']: (p['state'], p['error']) for p in state['apply']['progress']}
    assert progress[m1] == ('missing', 'mesh_service_missing') and progress[ids['r1']] == ('done', '') and progress[ids['m2']] == ('done', '')
    assert m1 not in nodes.sent('mesh.apply') and len(await notifications(e)) == 1
    assert (await one(e.db.conn, 'SELECT status FROM mesh_revisions WHERE revision=2'))['status'] == 'partial'
    # Nor an upgrade of its data plane when its node comes back on a new build.
    await connect(e, m1, build='b' * 64)
    await settled(e)
    assert m1 not in nodes.sent('mesh.upgrade') and m1 not in nodes.sent('mesh.apply') and len(await notifications(e)) == 1


async def test_a_service_said_gone_and_back_by_turns_alerts_once_per_cooldown(env, monkeypatch):  # noqa: F811
    """From the 10-30 recheck: 20 hellos answering installed false / true by turns sent 10 alerts."""
    e = env
    nodes, ids, gone = await meshed(e)
    m1 = ids['m1']
    clock = [mesh_module.now_ms()]
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: clock[0])
    keys = await one(e.db.conn, 'SELECT public_key,control_key FROM mesh_nodes WHERE server_id=?', (m1,))
    gone_call = e.servers.hub.call

    async def call(server_id, method, params, timeout):
        reply = await gone_call(server_id, method, params, timeout)
        if server_id == m1 and method == 'mesh.status' and reply['result'].get('installed'):
            # Back with the keys on record, as one put back by hand.
            reply['result'].update(publicKey=keys['public_key'], controlKey=keys['control_key'])
        return reply
    e.servers.hub.call = call

    async def hello(missing):
        (gone.add if missing else gone.discard)(m1)
        await connect(e, m1)
        state = await settled(e)
        assert (state['nodes'][m1]['state'] == 'missing') == missing
        clock[0] += 20_000
    for i in range(20):
        await hello(i % 2 == 0)
    assert len(await texts(e)) == 1
    # Once the cooldown is over, the next time it goes is said again.
    clock[0] += mesh_status.ALERT_COOLDOWN_MS
    await hello(True)
    assert len(await texts(e)) == 2
    # Gone again within it: held back, and said once it is still gone after it; only once.
    await hello(False)
    await hello(True)
    assert len(await texts(e)) == 2
    clock[0] += mesh_status.ALERT_COOLDOWN_MS
    await hello(True)
    await hello(True)
    assert len(await texts(e)) == 3


async def test_a_delivery_refused_for_a_missing_service_marks_it_once(env):  # noqa: F811
    e = env
    nodes, ids, gone = await meshed(e)
    m2 = ids['m2']
    # Gone while it stayed connected: the next delivery is the first to hear of it.
    gone.add(m2)
    draft = await api(e, 'GET', '/api/servers/mesh')
    await api(e, 'PUT', '/api/servers/mesh/draft', {'rules': [*draft['rules'], {'src': {'all': True}, 'dst': {'all': True}, 'proto': 'udp'}]})
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    progress = {p['serverId']: p['state'] for p in state['apply']['progress']}
    assert progress == {ids['r1']: 'done', ids['m1']: 'done', m2: 'missing'}
    assert state['nodes'][m2]['state'] == 'missing' and len(await texts(e)) == 1
    calls = nodes.sent('mesh.apply').count(m2)
    await api(e, 'POST', '/api/servers/mesh/rollback')
    await settled(e)
    assert nodes.sent('mesh.apply').count(m2) == calls and len(await texts(e)) == 1


async def test_reinstall_puts_the_service_back_and_issues_a_revision_keeping_the_draft(env):  # noqa: F811
    e = env
    nodes, ids, gone = await meshed(e)
    m1, r1 = ids['m1'], ids['r1']
    old_key = (await one(e.db.conn, 'SELECT public_key FROM mesh_nodes WHERE server_id=?', (m1,)))['public_key']
    gone.add(m1)
    await connect(e, m1)
    await settled(e)
    # An unapplied change waits in the draft.
    draft = await api(e, 'GET', '/api/servers/mesh')
    rules = [*draft['rules'], {'src': {'members': [ids['m2']]}, 'dst': {'members': [r1]}, 'proto': 'tcp', 'ports': [[22, 22]]}]
    saved = await api(e, 'PUT', '/api/servers/mesh/draft', {'rules': rules})
    assert saved['draft']['dirty']

    # Refused: not an applied member, or offline.
    assert (await api(e, 'POST', f'/api/servers/mesh/members/{ids["r2"]}/reinstall', status=404))['error'] == 'mesh_member_not_found'
    session = e.servers.hub.sessions.pop(m1)
    assert (await api(e, 'POST', f'/api/servers/mesh/members/{m1}/reinstall', status=409))['error'] == 'node_offline'
    e.servers.hub.sessions[m1] = session

    nodes.calls.clear()
    await api(e, 'POST', f'/api/servers/mesh/members/{m1}/reinstall')
    state = await settled(e)
    assert nodes.sent('mesh.install') == [m1]
    new_key = (await one(e.db.conn, 'SELECT public_key FROM mesh_nodes WHERE server_id=?', (m1,)))['public_key']
    assert new_key != old_key and new_key.startswith(m1[:8] + 'new')
    # A new revision of the applied document: every member gets the new key, the reinstalled one its config.
    assert state['applied']['revision'] == 2 and state['nodes'][m1]['state'] != 'missing' and state['nodes'][m1]['error'] == ''
    assert {p['serverId']: p['state'] for p in state['apply']['progress']} == {r1: 'done', m1: 'done', ids['m2']: 'done'}
    assert nodes.configs[m1]['revision'] == 2
    peers = {n['id']: n['publicKey'] for n in nodes.configs[r1]['nodes']}
    assert peers[m1] == new_key
    # The draft keeps its unapplied change; the applied rules are the old ones.
    assert state['draft']['dirty'] and state['rules'] == saved['rules']
    assert len(nodes.configs[r1]['rules']) == len(draft['rules'])
    assert len(await notifications(e)) == 1
    # Back to normal: the next reconnect finds the service and says nothing.
    await connect(e, m1)
    state = await settled(e)
    assert state['nodes'][m1]['state'] != 'missing' and len(await notifications(e)) == 1


def test_the_tool_has_mesh_reinstall():
    from app.tools.servers import ACTIONS, MESH_CONTRACTS, MESH_MUTATING, NEEDS_SERVER
    assert 'mesh_reinstall' in ACTIONS and 'mesh_reinstall' in MESH_MUTATING and 'mesh_reinstall' in NEEDS_SERVER
    assert 'serverId' in MESH_CONTRACTS['mesh_reinstall']
