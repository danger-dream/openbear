"""What the discovery loop does when there is nothing to do: one wake per loop, one look per
blocked endpoint, sleeping until the next deadline, and nothing left behind by a deleted endpoint."""
import asyncio
import json

from app.webhooks import routing
from app.webhooks.repository import many, one
from app.webhooks.service import Wake
from tests.test_webhooks_backend import env, create, accept  # noqa: F401


async def sealed(env, n):
    clock = [1_000_000]
    env.s.clock = lambda: clock[0]
    c = await create(env, batching={'enabled': True, 'idleSeconds': 1, 'maxWaitSeconds': 2, 'maxEvents': 1})
    for i in range(n):
        await accept(env, c, '{}', f'e{i}')
    await routing.tick(env.s)
    clock[0] += 5000
    await routing.tick(env.s)
    rows = await many(env.db.conn, "SELECT batch_id FROM webhook_batches WHERE state='sealed'")
    assert len(rows) == n
    return c, clock


async def pause_model(env, c):
    e = (await env.s.get(123, c['endpoint']['id']))['endpoint']
    await env.s.control(123, e['id'], {'requestId': 'hold', 'expectedControlRevision': e['controlRevision'], 'action': 'pause', 'scope': 'model'})


async def test_a_loop_clearing_its_wake_leaves_the_others_woken():
    wake = Wake()
    worker, waiting = wake.listen(), wake.listen()
    wake.set()
    waiting.clear()
    assert worker.is_set() and wake.is_set()
    wake.forget(worker)
    worker.clear(); wake.set()
    assert not worker.is_set() and waiting.is_set()


async def test_a_blocked_endpoint_is_looked_up_once_a_round(env, monkeypatch):
    c, _ = await sealed(env, 3)
    await pause_model(env, c)
    looked = []
    row = env.s.endpoint_row

    async def counted(*args, **kwargs):
        looked.append(args[1])
        return await row(*args, **kwargs)
    monkeypatch.setattr(env.s, 'endpoint_row', counted)
    await env.bridge.dispatch()
    assert looked == [c['endpoint']['id']]
    assert len(await many(env.db.conn, "SELECT 1 FROM webhook_batches WHERE state='sealed'")) == 3


async def test_deleting_an_endpoint_discards_its_batches_and_review_holds(env):
    c, _ = await sealed(env, 2)
    eid = c['endpoint']['id']
    event = (await one(env.db.conn, 'SELECT event_id FROM webhook_events WHERE endpoint_id=? LIMIT 1', (eid,)))['event_id']
    # A finished run whose result asked for review, as receipts.finalize leaves it.
    await env.db.conn.execute("UPDATE webhook_events SET route_state='terminal',terminal_at_ms=1,review_required=1 WHERE event_id=?", (event,))
    await env.db.conn.execute('UPDATE webhook_endpoints SET model_blockers_json=? WHERE endpoint_id=?',
                              (json.dumps({'review:a1': {'source': 'review', 'assignmentId': 'a1'}, 'manual:model': {'source': 'manual'}}), eid))
    await env.db.conn.commit()
    e = (await env.s.get(123, eid))['endpoint']
    grant = await env.s.prepare(123, eid, 'delete', {'requestId': 'delete', 'expectedControlRevision': e['controlRevision']})
    await env.s.control(123, eid, {'requestId': 'delete', 'confirmationToken': grant['confirmationToken']}, action='delete')
    assert [r['state'] for r in await many(env.db.conn, 'SELECT state FROM webhook_batches WHERE endpoint_id=?', (eid,))] == ['empty', 'empty']
    assert not await one(env.db.conn, 'SELECT 1 FROM webhook_events WHERE endpoint_id=? AND review_required=1', (eid,))
    blockers = json.loads((await one(env.db.conn, 'SELECT model_blockers_json b FROM webhook_endpoints WHERE endpoint_id=?', (eid,)))['b'])
    assert list(blockers) == ['manual:model']
    assert (await env.s.get(123, eid))['endpoint']['pendingEvents'] == 0


async def test_startup_discards_what_an_earlier_delete_left(env):
    c, _ = await sealed(env, 1)
    eid = c['endpoint']['id']
    await env.db.conn.execute('UPDATE webhook_endpoints SET deleted_at_ms=1,enabled=0,model_blockers_json=? WHERE endpoint_id=?',
                              (json.dumps({'review:a1': {'source': 'review'}}), eid))
    await env.db.conn.execute("UPDATE webhook_events SET route_state='terminal',terminal_at_ms=1,review_required=1 WHERE endpoint_id=?", (eid,))
    await env.db.conn.commit()
    await env.worker.recover()
    assert (await one(env.db.conn, 'SELECT state FROM webhook_batches WHERE endpoint_id=?', (eid,)))['state'] == 'empty'
    assert not await one(env.db.conn, 'SELECT 1 FROM webhook_events WHERE review_required=1')
    assert (await one(env.db.conn, 'SELECT model_blockers_json b FROM webhook_endpoints WHERE endpoint_id=?', (eid,)))['b'] == '{}'


async def test_the_loop_sleeps_until_the_next_deadline(env):
    clock = [1_000_000]
    env.s.clock = lambda: clock[0]
    env.worker.last_prune = clock[0]
    assert await env.worker.pause() == 1.0
    c = await create(env, batching={'enabled': True, 'idleSeconds': 0.3, 'maxWaitSeconds': 2})
    await accept(env, c)
    await routing.tick(env.s)
    assert abs(await env.worker.pause() - 0.3) < 0.01
    clock[0] += 290
    assert abs(await env.worker.pause() - 0.01) < 0.001


async def test_an_event_wakes_the_running_loop_at_once(env):
    env.worker.last_prune = env.s.clock()
    ticks = []
    tick = env.worker.tick

    async def counted():
        ticks.append(1)
        await tick()
    env.worker.tick = counted
    task = asyncio.create_task(env.worker.run())
    try:
        while not ticks:
            await asyncio.sleep(.01)
        await asyncio.sleep(.05)
        before = len(ticks)
        c = await create(env)
        await accept(env, c)
        await asyncio.sleep(.1)
        assert len(ticks) > before
        assert not await one(env.db.conn, "SELECT 1 FROM webhook_events WHERE route_state='eligible'")
    finally:
        env.worker.closing = True
        env.s.wake.set()
        await asyncio.wait_for(task, 2)
