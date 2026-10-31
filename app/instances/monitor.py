"""Bounded background observation; never launches deployment work."""
from __future__ import annotations

import asyncio
import json
import time

from app.instances import network
from app.instances.events import cleanup, health_notice
from app.lifecycle import wait_for_startup
from app.webhooks.repository import many, one

CONCURRENCY = 4
INTERVAL = 30
MAX_BACKOFF = 300


def now():
    return int(time.time()*1000)


async def recover(service, rows):
    await wait_for_startup(service)
    # The local-only startup snapshot predates every newly admitted run.
    sem = asyncio.Semaphore(CONCURRENCY)
    async def reconcile(op):
        async with sem:
            try:
                await service.finish(op['id'], op['revision'], op['root'], 'interrupted', recovering=True)
            except Exception:
                pass  # Durable record remains retryable; no model work is launched.
    await asyncio.gather(*(reconcile(op) for op in rows))


async def once(service, *, force=False):
    rows = await many(service.db.conn, "SELECT * FROM managed_instances WHERE status NOT IN ('archived','draft')")
    sem = asyncio.Semaphore(CONCURRENCY)
    async def observe(prior):
        async with service.lock(prior['id']):
            row = await one(service.db.conn, 'SELECT * FROM managed_instances WHERE id=?', (prior['id'],))
            item = json.loads(row['public_json'])
            old = item.get('monitor', {})
            op = await one(service.db.conn, 'SELECT status FROM instance_operations WHERE id=?', (row['active_operation'],))
            if row['status'] in {'archived', 'draft'} or op and op['status'] in {'pending', 'running'} or not force and old.get('nextCheckAtMs', 0) > now():
                return
            check = await network.probe(service, row)
            alive, health = check['identity'], check['health']
            state = 'unreachable' if not alive else 'degraded' if health['status'] == 'degraded' else 'healthy' if health['status'] == 'healthy' else 'unknown'
            failures = min(8, int(old.get('failures', 0))+1) if not check['ok'] else 0
            timestamp = now()
            item['monitor'] = {'checkedAtMs': timestamp, 'failures': failures,
                               'nextCheckAtMs': timestamp+1000*min(MAX_BACKOFF, INTERVAL*2**failures), 'routeReachable': check['publicRoute']}
            item['verification'] = check
            item.update(healthStatus=health['status'] if alive else 'unreachable', lastHealthAtMs=timestamp, lastError=check['error'])
            if alive:
                item['lastSeenAtMs'] = timestamp
            accepted = item.get('deploymentAccepted', row['status'] == 'ready')
            status = ('ready' if check['ok'] else 'unreachable' if not alive else 'needs_verification') if accepted else row['status']
            async with service.db.write_transaction(label='instance-health-monitor') as conn:
                await health_notice(service, conn, row, item, state)
                await conn.execute('UPDATE managed_instances SET public_json=?,status=? WHERE id=? AND revision=?', (json.dumps(item), status, row['id'], row['revision']))
    async def safe(row):
        try:
            async with sem:
                if service.lock(row['id']).locked():
                    return  # Long remote commands cannot occupy monitor slots.
                await observe(row)
        except Exception:
            pass  # One corrupt/unreachable instance must not stop all monitors.
    await asyncio.gather(*(safe(row) for row in rows))


async def gateway_upkeep(service):
    """Pull login events and retry account/route changes not yet applied."""
    try:
        await service.gateway.pull_events()
    except Exception:
        pass
    for row in await many(service.db.conn, "SELECT id,owner,public_json FROM managed_instances WHERE auth_mode='gateway' AND status!='archived'"):
        state = json.loads(row['public_json']).get('gateway', {})
        if state.get('status') == 'pending_sync':
            try:
                await service.gateway.sync(row['owner'], row['id'])
            except Exception:
                pass


async def monitor(service):
    await wait_for_startup(service)
    if service.recovery_task:
        await asyncio.shield(service.recovery_task)
    # The first pass already refreshes the gateway status: a due (automatic) gateway
    # update is found right after the controller starts.
    last_cleanup = 0
    while not service.closing:
        try:
            await once(service)
            await gateway_upkeep(service)
            await service.rotation_upkeep()
            if now()-last_cleanup >= 60000:
                await cleanup(service)
                # Refresh self-service account names without replaying cached passwords.
                await service.gateway.get()
                last_cleanup = now()
        except Exception:
            pass  # Retry bounded maintenance; never emit credentials in exception text.
        await asyncio.sleep(5)
