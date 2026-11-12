"""A run that fails before any tool ran changed nothing outside (2026-11-05: an exhausted
upstream account): its events are retried after a backoff, then fail; nothing waits for review."""
import asyncio
import json

from app.llm.events import StreamEvent, ToolCall
from app.webhooks.receipts import UNSTARTED_RETRY_S
from app.webhooks.repository import many, one
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend
from tests.test_webhooks_backend import env, create, accept  # noqa: F401

REFUSED = [StreamEvent(kind='error', error='HTTP 402 balance_exhausted', status=402, reason='billing', retryable=False)]


async def run(env):
    await env.worker.tick()
    await asyncio.gather(*env.server.runs.scheduler.tasks(kind='controller'))
    await env.worker.tick()


async def blockers(env):
    return json.loads((await one(env.db.conn, 'SELECT model_blockers_json b FROM webhook_endpoints'))['b'])


async def test_events_of_a_run_that_did_nothing_are_retried_after_a_backoff_then_fail(env):
    clock = [1_000_000]
    env.s.clock = lambda: clock[0]
    c = await create(env, batching={'enabled': False})
    event = (await accept(env, c))['eventId']
    env.server.llm_factory = FakeRunFactory(FakeStreamBackend([REFUSED]), context_window=128000)
    for tried, pause in enumerate(UNSTARTED_RETRY_S):
        await run(env)
        assert (await one(env.db.conn, 'SELECT COUNT(*) n FROM webhook_assignments'))['n'] == tried + 1
        row = await one(env.db.conn, 'SELECT route_state, review_required FROM webhook_events WHERE event_id=?', (event,))
        assert row['route_state'] != 'terminal' and not row['review_required']
        assert await blockers(env) == {'retry': {'source': 'retry', 'untilMs': clock[0] + pause * 1000}}
        assert (await env.s.get(123, c['endpoint']['id']))['endpoint']['pauseReasons'] == ['retry']
        # Held for the whole backoff, then dispatched again by itself.
        clock[0] += pause * 1000 - 1
        await run(env)
        assert (await one(env.db.conn, 'SELECT COUNT(*) n FROM webhook_assignments'))['n'] == tried + 1
        clock[0] += 1
        assert (await env.s.get(123, c['endpoint']['id']))['endpoint']['pauseReasons'] == []
    await run(env)
    row = await one(env.db.conn, 'SELECT route_state, review_required, terminal_reason FROM webhook_events WHERE event_id=?', (event,))
    assert (row['route_state'], row['review_required'], row['terminal_reason']) == ('terminal', 0, 'failed')
    assert [r['terminal_reason'] for r in await many(env.db.conn, 'SELECT terminal_reason FROM webhook_assignments ORDER BY created_at_ms')] == \
        ['retried'] * len(UNSTARTED_RETRY_S) + ['failed']
    assert not any(k.startswith('review:') for k in await blockers(env))
    # Only the run that settled the event has a result to announce.
    from app.webhooks.notifications import tick
    await tick(env.s)
    notified = await many(env.db.conn, 'SELECT notification_key FROM webhook_assignments WHERE notification_key IS NOT NULL')
    assert len(notified) == 1


async def test_a_run_that_used_a_tool_before_failing_still_waits_for_review_and_says_so(env):
    from app.webhooks.notifications import tick
    c = await create(env, batching={'enabled': False}, notifications={'policy': 'silent'})
    await accept(env, c)

    async def lookup(args):
        return 'looked up'
    env.server.tools.add('Lookup', 'A read', {'type': 'object'}, lookup)
    env.server.llm_factory = FakeRunFactory(FakeStreamBackend([
        [StreamEvent(kind='tool_call', tool_calls=[ToolCall('t1', 'Lookup', '{}')]), StreamEvent(kind='finish', finish_reason='tool_calls')],
        REFUSED]), context_window=128000)
    await run(env)
    a = await one(env.db.conn, 'SELECT * FROM webhook_assignments')
    assert a['terminal_reason'] != 'retried' and 'review:' + a['assignment_id'] in await blockers(env)
    assert (await one(env.db.conn, 'SELECT review_required FROM webhook_events'))['review_required'] == 1
    assert (await env.s.get(123, c['endpoint']['id']))['endpoint']['effectiveStatus'] == 'needs_review'
    # Silent results, but a held endpoint is announced.
    await tick(env.s)
    n = await one(env.db.conn, "SELECT state, payload_json FROM web_task_notifications WHERE kind='webhook-result'")
    assert n['state'] != 'suppressed' and '已暂停自动模型处理' in json.loads(n['payload_json'])['finalText']


async def test_resuming_the_model_ends_a_backoff(env):
    c = await create(env, batching={'enabled': False})
    await accept(env, c)
    env.server.llm_factory = FakeRunFactory(FakeStreamBackend([REFUSED]), context_window=128000)
    await run(env)
    assert 'retry' in await blockers(env)
    e = (await env.s.get(123, c['endpoint']['id']))['endpoint']
    await env.s.control(123, e['id'], {'requestId': 'hold', 'expectedControlRevision': e['controlRevision'], 'action': 'pause', 'scope': 'model'})
    e = (await env.s.get(123, c['endpoint']['id']))['endpoint']
    await env.s.control(123, e['id'], {'requestId': 'go', 'expectedControlRevision': e['controlRevision'], 'action': 'resume', 'scope': 'model'})
    assert await blockers(env) == {}
