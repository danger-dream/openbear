"""Confirmed mesh readdressing. The old applied document stays authoritative until
all participants, including relays, answer through the tentative configuration."""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from app.instances.contracts import InstanceError
from app.instances.servers import LOCAL
from app.servers.mesh_config import config_hash
from app.servers.onboarding import resolve
from app.webhooks.repository import many, one

FEATURE = 'mesh.confirmed'
LEASE_SECONDS = 180
READY_SECONDS = 100
POLL_SECONDS = 1
ACTIVATE_DELAY_MS = 2000


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def addressing(config):
    return (config.get('network', {}).get('prefix'), config.get('self', {}).get('address'), config.get('self', {}).get('controlAddress'))


def needed(previous, configs):
    return any(addressing(previous[sid]) != addressing(config) for sid, config in configs.items() if sid in previous)


async def load(mesh, owner):
    row = await one(mesh.db.conn, 'SELECT change_json FROM mesh_changes WHERE owner=?', (owner,))
    return json.loads(row['change_json']) if row else None


async def make(mesh, owner, net, previous, rows, configs):
    for sid in configs:
        session = mesh.hub.sessions.get(sid)
        if not session or FEATURE not in session.features:
            error = InstanceError('mesh_change_requires_online_nodes', 409)
            error.reason = f'{mesh._name(sid)}：重编号需要所有受影响节点在线并支持确认提交，请先连接或更新节点；旧网络未修改'
            raise error
    settings, profiles = await mesh.service.settings(), await mesh.service._profiles(owner)
    return {'id': uuid.uuid4().hex, 'phase': 'preparing', 'net': dict(net), 'previous': previous,
            'rows': {sid: {k: row[k] for k in ('state', 'confirmed_revision', 'confirmed_hash', 'error')} for sid, row in rows.items()},
            'targets': list(configs), 'remaining': list(configs), 'deadline': int(time.time() * 1000) + LEASE_SECONDS * 1000,
            'settings': settings, 'profiles': profiles, 'commitStarted': False}


async def save(mesh, owner, change):
    async with mesh.db.write_transaction(label='mesh-change') as conn:
        await conn.execute('INSERT INTO mesh_changes VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET change_json=excluded.change_json', (owner, dumps(change)))


async def rollback(mesh, owner, change):
    change['phase'] = 'aborting'
    net = change['net']
    async with mesh.db.write_transaction(label='mesh-change-rollback') as conn:
        await conn.execute('UPDATE mesh_network SET applied_revision=?,applied_ms=? WHERE owner=?', (net['applied_revision'], net['applied_ms'], owner))
        await mesh._save(conn, owner, 'applied', change['previous'])
        for sid, row in change['rows'].items():
            await conn.execute('UPDATE mesh_nodes SET state=?,confirmed_revision=?,confirmed_hash=?,error=? WHERE server_id=?',
                               (row['state'], row['confirmed_revision'], row['confirmed_hash'], row['error'], sid))
        await conn.execute("UPDATE mesh_revisions SET status='failed' WHERE owner=? AND revision=?", (owner, change['revision']))
        await conn.execute('UPDATE mesh_changes SET change_json=? WHERE owner=?', (dumps(change), owner))
    mesh._applied_changed(owner)


async def recover(mesh, owner):
    change = await load(mesh, owner)
    if not change:
        return False
    if change['phase'] != 'aborting':
        await rollback(mesh, owner, change)
    async def abort(sid):
        try:
            result = await mesh._call(owner, sid, 'mesh.abort', {'id': change['id']}, timeout=10)
            return sid if result.get('state') == 'aborted' else None
        except InstanceError:
            return None
    done = await asyncio.gather(*(abort(sid) for sid in change['remaining'] if sid in mesh.hub.sessions))
    change['remaining'] = [sid for sid in change['remaining'] if sid not in done]
    async with mesh.db.write_transaction(label='mesh-change-recovered') as conn:
        if change['remaining']:
            await conn.execute('UPDATE mesh_changes SET change_json=? WHERE owner=?', (dumps(change), owner))
        else:
            await conn.execute('DELETE FROM mesh_changes WHERE owner=?', (owner,))
    rollout = mesh.rollouts.get(owner)
    if rollout:
        rollout.recovery = 'restoring' if change['remaining'] else 'restored'
    if change['remaining'] and owner not in mesh.change_watching:
        mesh.change_watching.add(owner)
        mesh.service.spawn(watch(mesh, owner))
    return bool(change['remaining'])


async def watch(mesh, owner):
    try:
        while True:
            await asyncio.sleep(5)
            async with mesh._lock(owner):
                if not await recover(mesh, owner):
                    return
    finally:
        mesh.change_watching.discard(owner)


async def run(mesh, owner, plan):
    change, rollout = plan['change'], plan['rollout']
    deadline = time.monotonic() + READY_SECONDS
    committed = False
    try:
        access = mesh.access_of(plan['doc'], await mesh.mesh_capable(owner))
        async def prepare(sid):
            if sid == LOCAL:
                url, backup = await mesh.service.onboarding.addresses(owner, sid)
            else:
                points = resolve(change['settings'], change['profiles'].get(sid), mesh.access_point(access, sid))
                url, backup = (point['url'] if point else '' for point in points)
            result = await mesh._call(owner, sid, 'mesh.prepare', {'id': change['id'], 'config': plan['configs'][sid],
                                      'url': url, 'backup': backup, 'timeoutSeconds': LEASE_SECONDS}, timeout=20)
            if result.get('state') != 'prepared':
                raise InstanceError('mesh_change_not_prepared', 409)
        await parallel(prepare, change['targets'])
        change['phase'] = 'activating'
        # Include the last prepare's full lease when recovering after a controller restart.
        change['deadline'] = int(time.time() * 1000) + LEASE_SECONDS * 1000
        await save(mesh, owner, change)
        async def activate(sid):
            rollout.mark(sid, 'applying')
            result = await mesh._call(owner, sid, 'mesh.activate', {'id': change['id'], 'delayMs': ACTIVATE_DELAY_MS}, timeout=10)
            if result.get('state') not in ('scheduled', 'active'):
                raise InstanceError('mesh_change_not_prepared', 409)
        await parallel(activate, change['targets'])
        async def ready(sid):
            try:
                result = await mesh._call(owner, sid, 'mesh.status', {}, timeout=5)
            except InstanceError:
                return False
            node_change, status = result.get('change') or {}, result.get('status') or {}
            return (node_change.get('id') == change['id'] and node_change.get('state') == 'active'
                    and status.get('appliedRevision') == plan['revision'] and status.get('ready') is True and not status.get('error'))
        while True:
            states = await asyncio.gather(*(ready(sid) for sid in change['targets']))
            if all(states):
                break
            if time.monotonic() >= deadline:
                raise InstanceError('mesh_change_unconfirmed', 409)
            await asyncio.sleep(POLL_SECONDS)
        change['commitStarted'], change['phase'] = True, 'confirming'
        await save(mesh, owner, change)
        async def confirm(sid):
            result = await mesh._call(owner, sid, 'mesh.confirm', {'id': change['id']}, timeout=10)
            if result.get('state') != 'confirmed':
                raise InstanceError('mesh_change_unconfirmed', 409)
        await parallel(confirm, change['targets'])
        async with mesh.db.write_transaction(label='mesh-change-commit') as conn:
            await conn.execute('UPDATE mesh_network SET applied_revision=?,applied_ms=? WHERE owner=?', (plan['revision'], int(time.time() * 1000), owner))
            await mesh._save(conn, owner, 'applied', plan['doc'])
            # A draft edited while the rollout was in progress remains the user's next draft.
            draft = await mesh._load(owner, 'draft')
            if draft == change['draft']:
                await mesh._save(conn, owner, 'draft', plan['doc'])
                await mesh._drop_removed(conn, owner)
            for sid, config in plan['configs'].items():
                await conn.execute("UPDATE mesh_nodes SET state='',confirmed_revision=?,confirmed_hash=?,error='' WHERE server_id=?",
                                   (plan['revision'], config_hash(config), sid))
            for sid in rollout.cleanup:
                await conn.execute("UPDATE mesh_nodes SET state='cleanup' WHERE server_id=?", (sid,))
            await conn.execute("UPDATE mesh_revisions SET status='applied' WHERE owner=? AND revision=?", (owner, plan['revision']))
            await conn.execute('DELETE FROM mesh_changes WHERE owner=?', (owner,))
        committed = True
        mesh._applied_changed(owner)
        for sid in change['targets']:
            rollout.mark(sid, 'done')
            mesh.refused.pop(sid, None)
            await mesh.service.mark_access_pending(sid, False)
        for sid in rollout.cleanup:
            state, error = await mesh._uninstall(owner, sid)
            rollout.mark(sid, state, error)
        return 'applied' if all(item['state'] == 'done' for item in rollout.progress) else 'partial'
    except (Exception, asyncio.CancelledError) as exc:
        error = getattr(exc, 'error', 'mesh_change_interrupted')
        if committed:
            for item in rollout.progress:
                if item['state'] != 'done':
                    rollout.mark(item['serverId'], 'failed', error)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return 'partial'
        await rollback(mesh, owner, change)
        await recover(mesh, owner)
        for sid in change['targets']:
            rollout.mark(sid, 'failed', error)
        for sid in rollout.cleanup:
            rollout.mark(sid, 'pending', 'stopped')
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 'failed'
    finally:
        rollout.running = False


async def parallel(call, targets):
    # Finish every in-flight operation before compensation: none may activate after its abort.
    tasks = [asyncio.create_task(call(sid)) for sid in targets]
    group = asyncio.gather(*tasks, return_exceptions=True)
    try:
        results = await asyncio.shield(group)
    except asyncio.CancelledError:
        await group
        raise
    for result in results:
        if isinstance(result, BaseException):
            raise result
