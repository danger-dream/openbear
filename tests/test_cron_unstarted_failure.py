"""A scheduled run that fails before anything ran outside is tried again after a backoff; a run
still going is never joined by the next occurrence."""
import asyncio

from app.llm.events import StreamEvent, ToolCall
from app.webhooks.receipts import UNSTARTED_RETRY_S
from app.webhooks.repository import many, one
from tests.test_cron import cron_env, create, run  # noqa: F401
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend
from tests.test_webhooks_backend import env as base_env  # noqa: F401

REFUSED = [StreamEvent(kind='error', error='HTTP 402 balance_exhausted', status=402, reason='billing', retryable=False)]
HOUR = {'kind': 'every', 'everySeconds': 3600}


async def due(e, job, at):
    await e.db.conn.execute('UPDATE cron_jobs SET next_run_at_ms=? WHERE job_id=?', (at, job['id']))
    await e.db.conn.commit()


async def tick(e):
    await e.cron.tick()
    await asyncio.gather(*list(e.cron.tasks.values()))


async def test_a_scheduled_run_that_did_nothing_is_retried_after_a_backoff_then_left_failed(cron_env):
    e = cron_env
    now = [e.cron.clock()]
    e.cron.clock = lambda: now[0]
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([REFUSED]), context_window=128000)
    job = await create(e, schedule=HOUR)
    await due(e, job, now[0] - 1)
    for pause in UNSTARTED_RETRY_S:
        await tick(e)
        row = await one(e.db.conn, 'SELECT next_run_at_ms, schedule_state FROM cron_jobs WHERE job_id=?', (job['id'],))
        assert (row['next_run_at_ms'], row['schedule_state']) == (now[0] + pause * 1000, 'retrying')
        last = await one(e.db.conn, 'SELECT status, error, notification_state FROM cron_runs ORDER BY started_at_ms DESC, rowid DESC LIMIT 1')
        assert last['status'] == 'failed' and f'{pause} 秒后自动重试' in last['error'] and last['notification_state'] == 'suppressed'
        now[0] += pause * 1000
    await tick(e)
    runs = await many(e.db.conn, 'SELECT trigger_kind, status, error FROM cron_runs ORDER BY started_at_ms, rowid')
    assert [r['trigger_kind'] for r in runs] == ['scheduled'] + ['retry'] * len(UNSTARTED_RETRY_S)
    assert all(r['status'] == 'failed' for r in runs) and '自动重试' not in runs[-1]['error']
    # Back to the job's own schedule.
    row = await one(e.db.conn, 'SELECT next_run_at_ms, schedule_state FROM cron_jobs WHERE job_id=?', (job['id'],))
    assert row['schedule_state'] != 'retrying' and row['next_run_at_ms'] > now[0] + UNSTARTED_RETRY_S[-1] * 1000


async def test_a_run_that_used_a_tool_or_was_started_by_hand_is_not_retried(cron_env):
    e = cron_env

    async def lookup(args):
        return 'looked up'
    e.server.tools.add('Lookup', 'A read', {'type': 'object'}, lookup)
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([
        [StreamEvent(kind='tool_call', tool_calls=[ToolCall('t1', 'Lookup', '{}')]), StreamEvent(kind='finish', finish_reason='tool_calls')],
        REFUSED]), context_window=128000)
    job = await create(e, schedule=HOUR)
    await due(e, job, e.cron.clock() - 1)
    await tick(e)
    assert (await one(e.db.conn, 'SELECT schedule_state FROM cron_jobs WHERE job_id=?', (job['id'],)))['schedule_state'] != 'retrying'
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([REFUSED]), context_window=128000)
    manual = await run(e, job)
    assert manual['status'] == 'failed'
    assert (await one(e.db.conn, 'SELECT schedule_state FROM cron_jobs WHERE job_id=?', (job['id'],)))['schedule_state'] != 'retrying'


async def test_an_occurrence_due_while_the_last_run_is_going_is_skipped(cron_env):
    e = cron_env
    job = await create(e, schedule=HOUR, pre={'enabled': True, 'code': 'import time;time.sleep(.3)'})
    await due(e, job, e.cron.clock() - 1)
    await e.cron.tick()
    assert len(e.cron.tasks) == 1
    await due(e, job, e.cron.clock() - 1)
    await e.cron.tick()
    assert len(e.cron.tasks) == 1
    await asyncio.gather(*list(e.cron.tasks.values()))
    runs = await many(e.db.conn, 'SELECT status, error FROM cron_runs ORDER BY started_at_ms, rowid')
    assert sorted(r['status'] for r in runs) == ['completed', 'skipped']
    assert next(r for r in runs if r['status'] == 'skipped')['error'] == '上一次执行还没结束，本次跳过'
