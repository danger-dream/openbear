"""Confirmed changes use real controller/storage paths and isolated node RPCs only."""
from __future__ import annotations

import asyncio
import copy

import pytest

from app.instances.servers import LOCAL
from app.servers import mesh_change
from app.webhooks.repository import one
from tests.test_mesh_service import OWNER, connect, settled
from tests.test_servers_mesh_access import MESHED, mesh_access_setup
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


class ConfirmedNodes:
    def __init__(self, fake):
        self.fake, self.e, self.nodes = fake, fake.e, fake.nodes
        self.changes, self.log, self.fail, self.absent = {}, [], {}, set()
        self.status_gate = None

    async def call(self, sid, method, params, timeout):
        self.log.append((sid, method, copy.deepcopy(params)))
        if (sid, method) in self.fail:
            return {'ok': False, 'error': self.fail[sid, method]}
        if method == 'mesh.prepare':
            self.changes[sid] = {'id': params['id'], 'state': 'prepared', 'revision': params['config']['revision'],
                                 'config': params['config'], 'previous': copy.deepcopy(self.nodes.configs.get(sid))}
            return {'ok': True, 'result': {'state': 'prepared'}}
        if method == 'mesh.activate':
            c = self.changes[sid]
            c['state'] = 'active'
            self.nodes.configs[sid] = c['config']
            return {'ok': True, 'result': {'state': 'scheduled'}}
        if method == 'mesh.status' and sid in self.changes:
            if self.status_gate:
                await self.status_gate.wait()
            c = self.changes[sid]
            return {'ok': True, 'result': {'change': c, 'status': {'appliedRevision': c['revision'], 'ready': sid not in self.absent, 'error': ''}}}
        if method == 'mesh.confirm':
            assert all(c['state'] in ('active', 'confirmed') for c in self.changes.values())
            assert not self.absent
            self.changes[sid]['state'] = 'confirmed'
            return {'ok': True, 'result': {'state': 'confirmed'}}
        if method == 'mesh.abort':
            if sid in self.changes:
                c = self.changes[sid]
                self.nodes.configs[sid] = c['previous']
                c['state'] = 'aborted'
            return {'ok': True, 'result': {'state': 'aborted'}}
        return await self.fake.call(sid, method, params, timeout)


async def setup(e, monkeypatch, tmp_path):
    fake, nodes, ids, state = await mesh_access_setup(e, monkeypatch, tmp_path)
    rpc = ConfirmedNodes(fake)
    e.servers.hub.call = rpc.call
    for sid in nodes.configs:
        e.servers.hub.sessions[sid].features = frozenset((*MESHED, mesh_change.FEATURE))
    await api(e, 'PATCH', f'/api/servers/{ids["m1"]}', {'profile': {'access': {'primary': 'ap-mesh', 'backup': ''}}})
    monkeypatch.setattr(mesh_change, 'POLL_SECONDS', .01)
    monkeypatch.setattr(mesh_change, 'READY_SECONDS', .08)
    return rpc, ids, state


async def readdress(e):
    state = await api(e, 'GET', '/api/servers/mesh')
    await api(e, 'PUT', '/api/servers/mesh/draft', {'settings': {**state['settings'], 'prefix': '100.97.0.0/16'}})


async def test_refused_or_offline_exit_keeps_applied_and_cleanup(env, monkeypatch, tmp_path):
    e = env
    fake, nodes, ids, state = await mesh_access_setup(e, monkeypatch, tmp_path)
    sid = ids['m1']
    await api(e, 'PATCH', f'/api/servers/{sid}', {'profile': {'access': {'primary': 'ap-mesh', 'backup': ''}}})
    before = await e.servers.mesh._load(OWNER, 'applied')
    fake.refuse_url.add(sid)
    fake.log.clear()
    await api(e, 'DELETE', f'/api/servers/mesh/members/{sid}')
    error = await api(e, 'POST', '/api/servers/mesh/apply', status=409)
    assert error['error'] == 'mesh_access_move_failed'
    assert not any(method in ('mesh.apply', 'mesh.uninstall') for _, method, _ in fake.log)
    assert await e.servers.mesh._load(OWNER, 'applied') == before
    row = await one(e.db.conn, 'SELECT state FROM mesh_nodes WHERE server_id=?', (sid,))
    assert row['state'] != 'cleanup'
    await e.servers.mesh._catch_up(OWNER, sid)
    assert not any(method == 'mesh.uninstall' for _, method, _ in fake.log)
    session = e.servers.hub.sessions.pop(sid)
    await api(e, 'POST', '/api/servers/mesh/apply', status=409)
    e.servers.hub.sessions[sid] = session


async def test_confirmed_readdress_prepares_all_relays_and_new_url_before_switch(env, monkeypatch, tmp_path):
    e = env
    rpc, ids, before = await setup(e, monkeypatch, tmp_path)
    await readdress(e)
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['applied']['revision'] > before['applied']['revision'] and not state['draft']['dirty']
    assert all(p['state'] == 'done' for p in state['apply']['progress'])
    log = rpc.log
    prepared = [(sid, p) for sid, method, p in log if method == 'mesh.prepare']
    assert {sid for sid, _ in prepared} == set(rpc.nodes.configs)
    assert {ids['r1'], ids['r2'], LOCAL} <= {sid for sid, _ in prepared}
    assert next(p['url'] for sid, p in prepared if sid == ids['m1']).startswith('http://100.97.')
    last_prepare = max(i for i, (_, m, _) in enumerate(log) if m == 'mesh.prepare')
    first_activate = next(i for i, (_, m, _) in enumerate(log) if m == 'mesh.activate')
    first_confirm = next(i for i, (_, m, _) in enumerate(log) if m == 'mesh.confirm')
    assert last_prepare < first_activate < first_confirm
    assert {sid for sid, m, _ in log[first_activate:first_confirm] if m == 'mesh.status'} == set(rpc.nodes.configs)
    assert not await mesh_change.load(e.servers.mesh, OWNER)


@pytest.mark.parametrize('failure', ['not_ready', 'confirm', 'prepare'])
async def test_unconfirmed_change_restores_whole_network_and_retries(env, monkeypatch, tmp_path, failure):
    e = env
    rpc, ids, before = await setup(e, monkeypatch, tmp_path)
    old_doc, old_configs = await e.servers.mesh._load(OWNER, 'applied'), copy.deepcopy(rpc.nodes.configs)
    if failure == 'not_ready':
        rpc.absent.add(ids['m1'])
    else:
        rpc.fail[ids['m1'], 'mesh.' + failure] = 'injected_failure'
    await readdress(e)
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert state['applied']['revision'] == before['applied']['revision'] and state['draft']['dirty']
    assert await e.servers.mesh._load(OWNER, 'applied') == old_doc
    assert rpc.nodes.configs == old_configs
    assert all(c['state'] == 'aborted' for c in rpc.changes.values())
    assert any(p['state'] == 'failed' for p in state['apply']['progress'])
    rpc.fail.clear(); rpc.absent.clear()
    await api(e, 'POST', '/api/servers/mesh/apply')
    retried = await settled(e)
    assert not retried['draft']['dirty']


async def test_old_nodes_keep_ordinary_apply_but_unsafe_change_requires_update(env, monkeypatch, tmp_path):
    e = env
    rpc, ids, before = await setup(e, monkeypatch, tmp_path)
    e.servers.hub.sessions[ids['r1']].features = frozenset(MESHED)
    await readdress(e)
    rpc.log.clear()
    error = await api(e, 'POST', '/api/servers/mesh/apply', status=409)
    assert error['error'] == 'mesh_change_requires_online_nodes'
    assert not any(m.startswith(('mesh.prepare', 'mesh.activate', 'mesh.apply')) for _, m, _ in rpc.log)
    assert not await mesh_change.load(e.servers.mesh, OWNER)
    await api(e, 'POST', '/api/servers/mesh/discard')
    draft = await api(e, 'GET', '/api/servers/mesh')
    next(m for m in draft['members'] if m['serverId'] == ids['r1'])['uploadLimitMbps'] = 30
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members']})
    await api(e, 'POST', '/api/servers/mesh/apply')
    assert all(p['state'] == 'done' for p in (await settled(e))['apply']['progress'])


async def test_controller_recovery_aborts_durable_change_without_catchup(env, monkeypatch, tmp_path):
    e = env
    rpc, ids, before = await setup(e, monkeypatch, tmp_path)
    await readdress(e)
    mesh = e.servers.mesh
    draft, facts = await mesh._load(OWNER, 'draft'), await mesh._facts(OWNER)
    async with mesh._lock(OWNER):
        plan = await mesh._issue(OWNER, draft, facts, reason='apply', save_draft=True)
    assert await mesh_change.load(mesh, OWNER)
    # A new controller process sees the durable journal, not an in-memory rollout.
    replacement = type(mesh)(e.servers)
    assert not await mesh_change.recover(replacement, OWNER)
    assert (await replacement._network(OWNER))['applied_revision'] == before['applied']['revision']
    assert not any(m == 'mesh.apply' for _, m, _ in rpc.log if m.startswith('mesh.'))
    assert (await replacement._load(OWNER, 'draft'))['settings']['prefix'] == '100.97.0.0/16'


async def test_reserved_prefix_keeps_data_plane_but_disables_http_access(env, monkeypatch, tmp_path):
    e = env
    rpc, ids, before = await setup(e, monkeypatch, tmp_path)
    mesh = e.servers.mesh
    doc = await mesh._load(OWNER, 'applied')
    local = next(m for m in doc['members'] if m['serverId'] == LOCAL)
    local['address'] = '198.18.0.1'
    access, _, _ = mesh.access_of(doc, frozenset(ids.values()))
    assert access == {'id': 'ap-mesh', 'name': '组网', 'url': '', 'available': False, 'reason': 'controller_url_requires_https'}


async def test_pending_change_does_not_overwrite_later_draft_and_blocks_catchup(env, monkeypatch, tmp_path):
    e = env
    rpc, ids, before = await setup(e, monkeypatch, tmp_path)
    rpc.status_gate = asyncio.Event()
    await readdress(e)
    await api(e, 'POST', '/api/servers/mesh/apply')
    for _ in range(100):
        if any(method == 'mesh.status' and sid in rpc.changes for sid, method, _ in rpc.log):
            break
        await asyncio.sleep(.01)
    draft = await api(e, 'GET', '/api/servers/mesh')
    next(m for m in draft['members'] if m['serverId'] == ids['r1'])['uploadLimitMbps'] = 30
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members']})
    await e.servers.mesh._catch_up(OWNER, ids['m1'])
    assert not any(method == 'mesh.apply' for _, method, _ in rpc.log)
    rpc.status_gate.set()
    state = await settled(e)
    assert state['draft']['dirty'] and state['applied']['revision'] > before['applied']['revision']
    assert next(m for m in state['members'] if m['serverId'] == ids['r1'])['uploadLimitMbps'] == 30


async def test_partial_confirmation_recovery_keeps_offline_node_pending(env, monkeypatch, tmp_path):
    e = env
    rpc, ids, before = await setup(e, monkeypatch, tmp_path)
    mesh = e.servers.mesh
    await readdress(e)
    async with mesh._lock(OWNER):
        plan = await mesh._issue(OWNER, await mesh._load(OWNER, 'draft'), await mesh._facts(OWNER), reason='apply')
    change = plan['change']
    change['commitStarted'], change['phase'], change['deadline'] = True, 'confirming', 1
    await mesh_change.save(mesh, OWNER, change)
    session = e.servers.hub.sessions.pop(ids['m1'])
    assert await mesh_change.recover(mesh, OWNER)
    journal = await mesh_change.load(mesh, OWNER)
    assert journal['phase'] == 'aborting' and journal['remaining'] == [ids['m1']]
    assert (await api(e, 'GET', '/api/servers/mesh'))['apply']['recovery'] == 'restoring'
    # Deadline passing does not make an unobserved restoration successful.
    await api(e, 'POST', '/api/servers/mesh/apply', status=409)
    e.servers.hub.sessions[ids['m1']] = session
    assert not await mesh_change.recover(mesh, OWNER)
    assert not await mesh_change.load(mesh, OWNER)


async def test_exit_uses_existing_reachable_backup_when_default_refuses(env, monkeypatch, tmp_path):
    from tests.test_servers_mesh_access import DIRECT, RELAY
    import json
    e = env
    fake, nodes, ids, state = await mesh_access_setup(e, monkeypatch, tmp_path)
    sid = ids['m1']
    await api(e, 'PATCH', f'/api/servers/{sid}', {'profile': {'access': {'primary': 'ap-mesh', 'backup': 'ap-relay'}}})
    attempts = []
    async def call(target, method, params, timeout):
        if target == sid and method == 'config.url':
            attempts.append((params['url'], params['backup']))
            if params['url'] == DIRECT:
                return {'ok': False, 'error': 'new_url_unreachable'}
        return await fake.call(target, method, params, timeout)
    e.servers.hub.call = call
    await api(e, 'DELETE', f'/api/servers/mesh/members/{sid}')
    await api(e, 'POST', '/api/servers/mesh/apply')
    state = await settled(e)
    assert attempts == [(DIRECT, RELAY), (RELAY, DIRECT)]
    assert all(p['state'] == 'done' for p in state['apply']['progress'])
    assert sid not in {m['serverId'] for m in (await e.servers.mesh._load(OWNER, 'applied'))['members']}
    row = await one(e.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (sid,))
    assert json.loads(row['info_json'])['accessPending']
