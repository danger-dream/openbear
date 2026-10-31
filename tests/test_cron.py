"""Cron acceptance on isolated SQLite, real Web runtime and real subprocesses."""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.cron.contracts import CronError, Schedule, timestamp
from app.cron.schedule import next_time, preview
from app.cron.service import CronService
from app.llm.events import StreamEvent, ToolCall, Usage
from app.tools.base import ToolRuntimeContext
from app.tools.cron import dispatch, register_cron_tool
from app.webhooks.contracts import WebhookError, iso
from app.webhooks.repository import many, one
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _login_cookie
from tests.test_webhooks_backend import env as base_env


@pytest.fixture
async def cron_env(base_env, monkeypatch):
    e = base_env
    e.cron = CronService(e.db, e.server)
    async def offline_fetch(session, url):
        raise OSError('isolated test: network unavailable')
    monkeypatch.setattr(e.cron.holidays, 'fetch', offline_fetch)
    e.server.cron = e.cron
    register_cron_tool(e.server.tools, e.cron)
    try:
        yield e
    finally:
        await e.cron.close()


async def create(e, **config):
    return (await e.cron.save(123, {'folderId': 'folder', 'name': '定时测试', 'enabled': True, 'requestId': uuid.uuid4().hex,
        'config': {'instructions': 'Only execute the isolated test.', 'schedule': {'kind': 'every', 'everySeconds': 60},
                   'notifications': {'mode': 'silent'}, **config}}))['job']


async def run(e, job):
    accepted = await e.cron.run(123, job['id'], {'expectedRevision': job['revision'], 'requestId': uuid.uuid4().hex})
    await asyncio.gather(*list(e.cron.tasks.values()))
    return (await e.cron.get_run(123, accepted['run']['id']))['run']


def test_schedule_calculator_and_preview():
    start = timestamp('2026-10-04T00:00:00Z')
    s = Schedule(kind='every', everySeconds=600, anchorAt=iso(start))
    assert next_time(s, start) == start + 600000
    assert next_time(s, start + 1234567) == start + 1800000
    assert next_time(Schedule(kind='at', at=iso(start)), start) is None
    assert preview(Schedule(expression='0 9 * * 1-5'), start, 2) == ['2026-10-05T01:00:00Z', '2026-10-06T01:00:00Z']
    with pytest.raises(ValueError): Schedule(expression='* * * * * *')
    with pytest.raises(ValueError): Schedule(timezone='missing/place')
    with pytest.raises(ValueError): Schedule(kind='at', at='2026-10-05T09:00:00')


def test_dst_missing_minute_skips_and_repeated_minute_once():
    s = Schedule(expression='30 2 * * *', timezone='America/New_York')
    assert preview(s, timestamp('2026-03-08T05:00:00Z'), 1) == ['2026-03-09T06:30:00Z']
    s = Schedule(expression='30 1 * * *', timezone='America/New_York')
    assert preview(s, timestamp('2026-11-01T04:00:00Z'), 2) == ['2026-11-01T05:30:00Z', '2026-11-02T06:30:00Z']


async def test_definition_crud_cas_and_one_folder_many(cron_env):
    e = cron_env
    a, b = await create(e), await create(e)
    assert a['id'] != b['id']
    assert (await e.cron.list(123, {}))['total'] == 2
    assert (await e.cron.list(999, {}))['total'] == 0
    assert (await e.cron.list(123, {'folderId': 'missing'}))['total'] == 0
    with pytest.raises(CronError):
        await e.cron.set_enabled(123, a['id'], {'enabled': False, 'expectedRevision': 0, 'requestId': 'bad'})
    body = {'enabled': False, 'expectedRevision': 1, 'requestId': 'disable'}
    first = await e.cron.set_enabled(123, a['id'], body)
    assert await e.cron.set_enabled(123, a['id'], body) == first
    assert first['job']['scheduleState'] == 'disabled'
    assert not await many(e.db.conn, 'SELECT * FROM webhook_endpoints')


async def test_real_turn_new_conversations_and_independent_origin(cron_env):
    e = cron_env; job = await create(e)
    first, second = await run(e, job), await run(e, job)
    assert first['status'] == second['status'] == 'completed', (first, second)
    assert first['conversationId'] != second['conversationId']
    assert first['finalText'] == 'ok'
    assert first['notificationState'] == 'suppressed'
    assert (await e.cron.get(123, job['id']))['job']['nextRunAt'] == job['nextRunAt']
    conversations = await many(e.db.conn, 'SELECT * FROM web_conversations')
    assert len(conversations) == 2 and all(c['folder_uuid'] == 'folder' and c['title'].startswith('[定时]') for c in conversations)
    runs = await many(e.db.conn, 'SELECT run_id, conversation_uuid FROM cron_runs')
    assert {(c['conversation_uuid'], c['origin_kind'], c['origin_ref']) for c in conversations} == {(r['conversation_uuid'], 'cron', r['run_id']) for r in runs}
    assert not await many(e.db.conn, 'SELECT * FROM webhook_assignments')
    assert any('Scheduled task (trusted runtime provenance)' in str(m.get('content')) for m in e.backend.seen_convos[0])
    assert (await one(e.db.conn, "SELECT COUNT(*) n FROM runtime_runs WHERE json_extract(metadata_json,'$.inputProvenance')='cron'"))['n'] == 2
    assert not await many(e.db.conn, "SELECT * FROM web_operations WHERE lifecycle='active'")


async def test_scripts_no_input_environment_logs_and_post_result(cron_env):
    e = cron_env
    pre = "import os,sys\nassert sys.stdin.read() == ''\nassert os.environ['OPENBEAR_CRON_RUN_ID']\nprint('plain preparation')"
    post = "import json,sys,os\nx=json.load(sys.stdin)\nassert x['outcome']=='completed'\nassert x['runId']==os.environ['OPENBEAR_CRON_RUN_ID']\nprint('cleaned')"
    job = await create(e, pre={'enabled': True, 'code': pre}, post={'enabled': True, 'code': post})
    result = await run(e, job)
    assert result['status'] == 'completed', result
    assert result['preAttempts'][0]['stdout'] == 'plain preparation\n'
    assert result['postAttempts'][0]['stdout'] == 'cleaned\n'
    assert len(e.backend.seen_convos) == 1


async def test_retry_stays_in_script_and_post_failure_preserves_outcome(cron_env):
    e = cron_env
    code = "from pathlib import Path\np=Path('retry-count')\nn=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\nraise SystemExit(1 if n==0 else 0)"
    job = await create(e, pre={'enabled': True, 'code': code, 'retry': {'maxAttempts': 2, 'backoffSeconds': [0]}},
        post={'enabled': True, 'code': 'raise SystemExit(2)'})
    result = await run(e, job)
    assert result['status'] == 'post_failed' and result['outcome'] == 'completed', result
    assert len(result['preAttempts']) == 2 and len(result['postAttempts']) == 1
    assert e.backend.calls == 1


async def test_pre_failure_can_cleanup_without_model(cron_env):
    e = cron_env
    job = await create(e, pre={'enabled': True, 'code': 'raise SystemExit(1)'},
        post={'enabled': True, 'code': "print('cleanup')", 'onOutcomes': ['failed']})
    result = await run(e, job)
    assert result['status'] == result['outcome'] == 'failed', result
    assert result['postAttempts'][0]['stdout'] == 'cleanup\n'
    assert e.backend.calls == 0
    assert not await many(e.db.conn, "SELECT * FROM web_operations WHERE lifecycle='active'")


async def test_timeout_runs_cleanup_outside_execution_deadline(cron_env):
    e = cron_env
    job = await create(e, timeoutSeconds=.15, pre={'enabled': True, 'code': 'import time;time.sleep(10)'},
        post={'enabled': True, 'code': "import time;time.sleep(.2);print('cleaned')", 'onOutcomes': ['timed_out']})
    result = await run(e, job)
    assert result['status'] == 'timed_out', result
    assert result['postAttempts'][0]['stdout'] == 'cleaned\n'
    assert e.backend.calls == 0


async def test_total_token_guard_stops_before_tool_and_cleans_up(cron_env):
    e = cron_env
    backend = FakeStreamBackend([[StreamEvent(kind='usage', usage=Usage(input_tokens=100, output_tokens=20)),
        StreamEvent(kind='tool_call', tool_calls=[ToolCall('x', 'ShouldNotRun', '{}')]), StreamEvent(kind='finish', finish_reason='tool_calls')]])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    job = await create(e, totalTokensBudget=100, post={'enabled': True, 'code': "print('limit cleanup')", 'onOutcomes': ['tokens_exceeded']})
    result = await run(e, job)
    assert result['status'] == 'tokens_exceeded', result
    assert result['totalTokens'] == 120 and result['modelCalls'] == 1
    assert backend.calls == 1
    assert result['postAttempts'][0]['stdout'] == 'limit cleanup\n'
    assert not await many(e.db.conn, "SELECT * FROM runtime_actions WHERE kind='tool'")


async def test_zero_cost_is_not_a_budget_error(cron_env):
    e = cron_env
    job = await create(e, costBudgetUsd=.0001)
    result = await run(e, job)
    assert result['status'] == 'completed' and result['costUsd'] == 0, result


async def test_parallel_ticks_and_manual_request_id(cron_env):
    e = cron_env; job = await create(e, pre={'enabled': True, 'code': 'import time;time.sleep(.15)'})
    now = e.cron.clock()
    await e.db.conn.execute('UPDATE cron_jobs SET next_run_at_ms=? WHERE job_id=?', (now-1, job['id'])); await e.db.conn.commit()
    await asyncio.gather(e.cron.tick(), e.cron.tick())
    assert len(e.cron.tasks) == 1
    # Same definition may launch again while its earlier occurrence is live.
    body = {'expectedRevision': job['revision'], 'requestId': 'manual-once'}
    first = await e.cron.run(123, job['id'], body)
    assert await e.cron.run(123, job['id'], body) == first
    assert len(e.cron.tasks) == 2
    await asyncio.gather(*list(e.cron.tasks.values()))
    runs = (await e.cron.runs(123, {}))['items']
    assert len(runs) == 2 and all(r['status'] == 'completed' for r in runs), json.dumps(runs, ensure_ascii=False, indent=2)
    assert len({r['conversationId'] for r in runs}) == 2


async def test_restart_skips_ten_days_without_catchup(cron_env):
    e = cron_env; job = await create(e)
    past = e.cron.clock()-10*86400000
    await e.db.conn.execute('UPDATE cron_jobs SET next_run_at_ms=? WHERE job_id=?', (past, job['id'])); await e.db.conn.commit()
    await e.cron.start()
    saved = (await e.cron.get(123, job['id']))['job']
    assert timestamp(saved['nextRunAt']) > e.cron.clock()
    assert not await many(e.db.conn, 'SELECT * FROM cron_runs')
    await e.cron.close()


async def test_one_shot_exhaustion_does_not_disable_execution(cron_env):
    e = cron_env; now = e.cron.clock()
    job = await create(e, schedule={'kind': 'at', 'at': iso(now+1000)})
    e.cron.clock = lambda: now+1001
    await e.cron.tick()
    await asyncio.gather(*list(e.cron.tasks.values()))
    current = (await e.cron.get(123, job['id']))['job']
    assert current['enabled'] and current['scheduleState'] == 'exhausted'
    assert current['lastRun']['status'] == 'completed', current
    await e.cron.tick()
    assert (await e.cron.runs(123, {}))['total'] == 1


async def test_delete_keeps_history_and_confirmation_cas(cron_env):
    e = cron_env; job = await create(e); result = await run(e, job)
    with pytest.raises(CronError): await e.cron.delete(123, job['id'], {'requestId': 'deny'})
    grant = await e.cron.delete_impact(123, job['id'], {'expectedRevision': job['revision']})
    body = {'confirmationToken': grant['confirmationToken'], 'requestId': 'delete'}
    deleted = await e.cron.delete(123, job['id'], body)
    assert await e.cron.delete(123, job['id'], body) == deleted
    assert (await e.cron.list(123, {}))['total'] == 0
    assert (await e.cron.get_run(123, result['id']))['run']['status'] == 'completed'
    assert await one(e.db.conn, 'SELECT 1 FROM web_conversations WHERE conversation_uuid=?', (result['conversationId'],))


async def test_http_auth_config_preview_and_statistics(cron_env):
    e = cron_env
    assert (await e.client.get('/api/cron/jobs')).status == 401
    cookies = {'openbear_web_session': await _login_cookie(e)}
    response = await e.client.post('/api/cron/jobs', cookies=cookies, json={'folderId': 'folder', 'name': 'HTTP', 'requestId': 'http', 'config': {'instructions': 'test'}})
    assert response.status == 200, await response.text()
    job = (await response.json())['job']
    assert not job['enabled']
    response = await e.client.post('/api/cron/preview-next', cookies=cookies, json={'schedule': {'kind': 'cron', 'expression': '0 9 * * *'}, 'count': 5})
    assert response.status == 200 and len((await response.json())['times']) == 5
    assert (await (await e.client.get('/api/cron/folders', cookies=cookies)).json())['items'][0]['id'] == 'folder'
    assert (await (await e.client.get('/api/cron/statistics', cookies=cookies)).json())['runs'] == 0
    bad = await e.client.patch('/api/cron/jobs/'+job['id'], cookies=cookies, json={'requestId': 'bad', 'expectedRevision': 0, 'name': 'bad'})
    assert bad.status == 409


@pytest.mark.parametrize('source', ['web'])
async def test_tool_runtime_identity_and_delete_feedback(cron_env, source):
    e = cron_env; job = await create(e); result = await run(e, job)
    row = await one(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=?', (result['conversationId'],))
    ctx = ToolRuntimeContext(conversation_uuid=row['conversation_uuid'], chat_id=row['internal_chat_id'],
        source=source, webhook_automatic=source == 'webhook')
    assert (await dispatch(e.cron, 'list', {}, ctx))['total'] == 1
    assert (await dispatch(e.cron, 'list', {'folders': True}, ctx))['items'][0]['id'] == 'folder'
    with pytest.raises(CronError, match='host_identity_not_a_parameter'):
        await dispatch(e.cron, 'create', {'owner': 123}, ctx)
    ctx.agent_session_uuid = 'agent'
    with pytest.raises(CronError, match='main_controller_only'):
        await dispatch(e.cron, 'create', {}, ctx)
    ctx.agent_session_uuid = ''
    ctx.chat_id = 999
    with pytest.raises(CronError, match='host_identity_required'):
        await dispatch(e.cron, 'create', {}, ctx)
    ctx.chat_id = row['internal_chat_id']
    params = {'jobId': job['id'], 'expectedRevision': 1, 'requestId': 'tool-delete'}
    with pytest.raises(CronError, match='user_confirmation_unavailable'):
        await dispatch(e.cron, 'delete', params, ctx)
    async def feedback(payload): return {'confirmed': True, 'text': '不要删除'}
    ctx.web_confirm = feedback
    answer = await dispatch(e.cron, 'delete', params, ctx)
    assert answer['cancelled']
    assert (await e.cron.get(123, job['id']))['job']['enabled']


@pytest.mark.parametrize('source', ['cron'])
async def test_tool_manages_schedules_from_automatic_runs(cron_env, source):
    e = cron_env
    row = await e.server._create_web_conversation(123, folder_uuid='folder')
    ctx = ToolRuntimeContext(conversation_uuid=row['conversation_uuid'], chat_id=row['internal_chat_id'],
        source=source, webhook_automatic=source == 'webhook')
    params = {'folderId': 'folder', 'name': '后续任务', 'requestId': 'follow-up',
        'config': {'instructions': 'Only execute the isolated follow-up.', 'notifications': {'mode': 'silent'},
                   'schedule': {'kind': 'at', 'at': iso(e.cron.clock()+60000)}}}
    created = await dispatch(e.cron, 'create', params, ctx)
    assert await dispatch(e.cron, 'create', params, ctx) == created
    job = created['job']
    updated = await dispatch(e.cron, 'update', {'jobId': job['id'], 'expectedRevision': job['revision'],
        'requestId': 'edit-follow-up', 'name': '调整后的任务', 'config': job['config']}, ctx)
    job = updated['job']
    assert job['name'] == '调整后的任务'
    for enabled in (True, False, True):
        job = (await dispatch(e.cron, 'set_enabled', {'jobId': job['id'], 'expectedRevision': job['revision'],
            'requestId': uuid.uuid4().hex, 'enabled': enabled}, ctx))['job']
        assert job['enabled'] is enabled
    accepted = await dispatch(e.cron, 'run', {'jobId': job['id'], 'expectedRevision': job['revision'],
        'requestId': 'run-follow-up'}, ctx)
    await asyncio.gather(*list(e.cron.tasks.values()))
    run_id = accepted['run']['id']
    assert (await e.cron.get_run(123, run_id))['run']['status'] == 'completed'
    await dispatch(e.cron, 'stop', {'runId': run_id, 'requestId': 'stop-follow-up'}, ctx)
    async def confirm(payload): return {'confirmed': True}
    ctx.web_confirm = confirm
    deleted = await dispatch(e.cron, 'delete', {'jobId': job['id'], 'expectedRevision': job['revision'],
        'requestId': 'delete-follow-up'}, ctx)
    assert deleted['job']['scheduleState'] == 'deleted'


async def test_server_changes_are_granted_by_people_never_by_automatic_runs(cron_env):
    from app.tools.webhook import dispatch as webhook_dispatch
    e = cron_env
    row = await e.server._create_web_conversation(123, folder_uuid='folder')
    person = ToolRuntimeContext(conversation_uuid=row['conversation_uuid'], chat_id=row['internal_chat_id'], webhook_owner_id=123)
    automatic = ToolRuntimeContext(conversation_uuid=row['conversation_uuid'], chat_id=row['internal_chat_id'], source='cron', webhook_owner_id=123)
    config = {'instructions': 'Check the servers.', 'notifications': {'mode': 'silent'}}
    plain = (await dispatch(e.cron, 'create', {'folderId': 'folder', 'name': '巡检', 'requestId': 'plain', 'config': config}, automatic))['job']
    assert plain['config']['allowServerChanges'] is False
    with pytest.raises(CronError) as refused:
        await dispatch(e.cron, 'create', {'folderId': 'folder', 'name': '改动', 'requestId': 'grant', 'config': {**config, 'allowServerChanges': True}}, automatic)
    assert refused.value.payload['code'] == 'automatic_permission_forbidden'
    with pytest.raises(CronError) as refused:
        await dispatch(e.cron, 'update', {'jobId': plain['id'], 'expectedRevision': plain['revision'], 'requestId': 'grant-update', 'name': '巡检',
                                          'config': {**plain['config'], 'allowServerChanges': True}}, automatic)
    assert refused.value.payload['code'] == 'automatic_permission_forbidden'
    granted = (await dispatch(e.cron, 'update', {'jobId': plain['id'], 'expectedRevision': plain['revision'], 'requestId': 'person', 'name': '巡检',
                                                 'config': {**plain['config'], 'allowServerChanges': True}}, person))['job']
    assert granted['config']['allowServerChanges'] is True
    # A run never edits a task the user allowed, even keeping the permission: it would run what it wrote with it.
    with pytest.raises(CronError) as refused:
        await dispatch(e.cron, 'update', {'jobId': granted['id'], 'expectedRevision': granted['revision'], 'requestId': 'keep', 'name': '巡检 2',
                                          'config': granted['config']}, automatic)
    assert refused.value.payload['code'] == 'automatic_privileged_task_forbidden'
    kept = (await dispatch(e.cron, 'update', {'jobId': granted['id'], 'expectedRevision': granted['revision'], 'requestId': 'keep-person',
                                              'name': '巡检 2', 'config': granted['config']}, person))['job']
    assert kept['name'] == '巡检 2' and kept['config']['allowServerChanges'] is True
    with pytest.raises(WebhookError) as refused:
        await webhook_dispatch(e.s, 'create', {'scope': {'type': 'folder', 'id': 'folder'}, 'requestId': 'hook',
                                               'config': {'processing': {'instructions': 'x', 'allowServerChanges': True}}}, automatic)
    assert refused.value.payload['code'] == 'automatic_permission_forbidden'
    assert not await many(e.db.conn, 'SELECT * FROM webhook_endpoints')
    hook = (await webhook_dispatch(e.s, 'create', {'scope': {'type': 'folder', 'id': 'folder'}, 'requestId': 'hook-person',
                                                   'config': {'processing': {'instructions': 'x', 'allowServerChanges': True}}}, person))['endpoint']
    assert hook['config']['processing']['allowServerChanges'] is True


async def test_scheduled_turn_creates_and_executes_one_time_follow_up(cron_env):
    e = cron_env; now = e.cron.clock()
    params = {'folderId': 'folder', 'name': '后续一次性任务', 'enabled': True, 'requestId': 'scheduled-follow-up',
        'config': {'instructions': 'Only execute the isolated follow-up.', 'notifications': {'mode': 'silent'},
                   'schedule': {'kind': 'at', 'at': iso(now+5000)}}}
    backend = FakeStreamBackend([
        [StreamEvent(kind='tool_call', tool_calls=[ToolCall('describe', 'Cron', json.dumps({'action': 'describe'}))]),
         StreamEvent(kind='finish', finish_reason='tool_calls')],
        [StreamEvent(kind='tool_call', tool_calls=[ToolCall('create', 'Cron', json.dumps({'action': 'create', 'params': params}))]),
         StreamEvent(kind='finish', finish_reason='tool_calls')],
        [StreamEvent(kind='content', text='ok'), StreamEvent(kind='finish', finish_reason='stop')],
    ])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    parent = await create(e, schedule={'kind': 'at', 'at': iso(now+1000)}, totalTokensBudget=1000000)
    e.cron.clock = lambda: now+1001
    await e.cron.tick()
    await asyncio.gather(*list(e.cron.tasks.values()))
    parent_runs = (await e.cron.runs(123, {'jobId': parent['id']}))['items']
    assert len(parent_runs) == 1 and parent_runs[0]['status'] == 'completed'
    jobs = (await e.cron.list(123, {}))['items']
    assert len(jobs) == 2, jobs
    child = next(job for job in jobs if job['id'] != parent['id'])
    assert child['enabled'] and child['config']['schedule']['kind'] == 'at'
    assert timestamp(child['nextRunAt']) == now+5000
    prompt = str(backend.seen_convos[0])
    assert 'The saved task instructions define the scope.' in prompt
    assert 'Existing tool confirmation gates and current human corrections apply.' in prompt
    assert 'Do not create, enable or modify schedules' not in prompt
    e.cron.clock = lambda: now+5001
    await e.cron.tick()
    await asyncio.gather(*list(e.cron.tasks.values()))
    child_runs = (await e.cron.runs(123, {'jobId': child['id']}))['items']
    assert len(child_runs) == 1 and child_runs[0]['status'] == 'completed'
    assert child_runs[0]['conversationId'] != parent_runs[0]['conversationId']
    assert (await e.cron.get(123, child['id']))['job']['scheduleState'] == 'exhausted'
    await e.cron.tick()
    assert (await e.cron.runs(123, {'jobId': child['id']}))['total'] == 1


@pytest.mark.parametrize('phase', ['pre', 'model'])
async def test_existing_conversation_stop_preserves_plan_and_runs_cleanup(cron_env, monkeypatch, phase):
    e = cron_env
    from app.cron import executor
    entered = asyncio.Event()
    original_stage = executor.stage
    async def stage(*args, **kwargs):
        if args[3] == 'pre': entered.set()
        return await original_stage(*args, **kwargs)
    monkeypatch.setattr(executor, 'stage', stage)
    class Backend(FakeStreamBackend):
        async def stream(self, messages, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield StreamEvent('finish', finish_reason='stop')
    e.server.llm_factory = FakeRunFactory(Backend(), context_window=128000)
    job = await create(e, pre={'enabled': phase == 'pre', 'code': 'import time;time.sleep(30)'},
        post={'enabled': True, 'code': "print('stopped cleanup')", 'onOutcomes': ['cancelled']})
    accepted = await e.cron.run(123, job['id'], {'expectedRevision': 1, 'requestId': 'run-stop'})
    await asyncio.wait_for(entered.wait(), 5)
    run_id = accepted['run']['id']
    request = {'requestId': 'stop-once'}
    first = await e.cron.stop(123, run_id, request)
    assert await e.cron.stop(123, run_id, request) == first
    result = (await e.cron.get_run(123, run_id))['run']
    assert result['status'] == 'cancelled', result
    assert result['postAttempts'][0]['stdout'] == 'stopped cleanup\n'
    assert (await e.cron.get(123, job['id']))['job']['enabled']
    assert not await many(e.db.conn, "SELECT * FROM web_operations WHERE lifecycle='active'")


@pytest.mark.parametrize('action', ['disable', 'edit', 'delete'])
async def test_definition_change_does_not_cancel_or_mutate_active_run(cron_env, action):
    e = cron_env; entered = asyncio.Event(); release = asyncio.Event()
    class Backend(FakeStreamBackend):
        async def stream(self, messages, **kwargs):
            entered.set(); await release.wait()
            yield StreamEvent('content', text='Original execution')
            yield StreamEvent('finish', finish_reason='stop')
    e.server.llm_factory = FakeRunFactory(Backend(), context_window=128000)
    job = await create(e)
    accepted = await e.cron.run(123, job['id'], {'expectedRevision': 1, 'requestId': 'start'})
    await asyncio.wait_for(entered.wait(), 5)
    if action == 'disable':
        await e.cron.set_enabled(123, job['id'], {'expectedRevision': 1, 'requestId': 'disable', 'enabled': False})
    elif action == 'edit':
        await e.cron.save(123, {'expectedRevision': 1, 'requestId': 'edit', 'config': {**job['config'], 'instructions': 'Changed future execution'}}, job['id'])
    else:
        grant = await e.cron.delete_impact(123, job['id'], {'expectedRevision': 1})
        await e.cron.delete(123, job['id'], {'requestId': 'delete', 'confirmationToken': grant['confirmationToken']})
    assert not e.cron.tasks[accepted['run']['id']].done()
    release.set(); await asyncio.gather(*list(e.cron.tasks.values()))
    result = (await e.cron.get_run(123, accepted['run']['id']))['run']
    assert result['status'] == 'completed'
    assert result['configSnapshot']['instructions'] == job['config']['instructions']


@pytest.mark.parametrize('budget,expected,detached', [({'totalTokensBudget': 700}, 'tokens_exceeded', False), ({'costBudgetUsd': .005}, 'cost_exceeded', False), ({}, 'completed', False), ({}, 'completed', True)])
async def test_real_child_agent_uses_same_root_budget_and_ledger(cron_env, monkeypatch, budget, expected, detached):
    from app.agents.dao import AgentDAO
    from app.agents.profiles import ensure_builtin_workflows
    from app.agents.schemas import AgentDefinition
    from app.agents.execution import AgentExecutor
    from app.tools.agents import AgentTools
    from app.tools.base import ToolRegistry, current_tool_context
    from app.llm.base import AgentResult
    from app.agents.control import AgentControlService
    e = cron_env; dao = AgentDAO(e.db); e.server.agent_dao = dao
    e.server.agents = AgentControlService(dao, scheduler=e.server.runs.scheduler)
    workflow = await ensure_builtin_workflows(dao)
    agent = AgentDefinition(workflow_uuid=workflow, agent_key='fixture', name='Fixture', model='openai/gpt', enabled=True, id=7)
    children = []; release = asyncio.Event(); waiting = asyncio.Event()
    from app.cron import executor
    original_bind = executor.bind_context
    def bind_context(service, ctx, run_id):
        original_bind(service, ctx, run_id)
        original_wait = ctx.agent_wait
        async def wait(request):
            waiting.set()
            return await original_wait(request)
        ctx.agent_wait = wait
    monkeypatch.setattr(executor, 'bind_context', bind_context)
    class Child:
        protocol = 'chat'
        async def complete(self, messages, **kwargs):
            if detached: await release.wait()
            return AgentResult(text='Child complete', usage=Usage(input_tokens=300, cache_read_tokens=100, output_tokens=40), provider_cost_usd=.004, finish_reason='stop')
    async def spawn(args):
        ctx = current_tool_context()
        tid = await dao.create_task(chat_id=ctx.chat_id, workflow_uuid=workflow, title='Child', input_data={'instruction': 'Isolated child'}, parent_session_uuid=ctx.conversation_uuid, run_root_turn_uuid=ctx.run_root_turn_uuid)
        async def account(detail):
            await AgentTools._persist_agent_model_call(SimpleNamespace(dao=dao), chat_id=ctx.chat_id, session_uuid=ctx.session_uuid, model_label='openai/gpt', protocol='chat', detail=detail)
        runner = AgentExecutor(dao, tid, agent=agent, backend=Child(), model='gpt', model_label='openai/gpt', max_tokens=1024, tools=ToolRegistry(), on_model_call=account)
        child = asyncio.create_task(runner.run()); children.append(child)
        e.server.agents.register(tid, ctx.chat_id, child, occupies_chat=False)
        child.add_done_callback(lambda _: e.server._web_controller_wake_events[ctx.conversation_uuid].set())
        if detached:
            return 'Child running independently'
        return json.dumps(await child)
    e.server.tools.add('SpawnOwned', 'Start isolated owned work', {'type': 'object'}, spawn)
    backend = FakeStreamBackend([
        [StreamEvent('usage', usage=Usage(input_tokens=100, cache_read_tokens=200, cache_write_tokens=50, output_tokens=20), details={'providerCostUsd': .003}),
         StreamEvent('tool_call', tool_calls=[ToolCall('spawn', 'SpawnOwned', '{}')]), StreamEvent('finish', finish_reason='tool_calls')],
        [StreamEvent('usage', usage=Usage(), details={'providerCostUsd': 0.0}), StreamEvent('content', text='All done'), StreamEvent('finish', finish_reason='stop')],
        [StreamEvent('usage', usage=Usage(), details={'providerCostUsd': 0.0}), StreamEvent('content', text='Child integrated'), StreamEvent('finish', finish_reason='stop')],
    ])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    try:
        running = asyncio.create_task(run(e, await create(e, **budget)))
        if detached:
            await asyncio.wait_for(waiting.wait(), 5)
            assert not running.done()
            assert (await one(e.db.conn, 'SELECT phase FROM cron_runs'))['phase'] == 'model'
            release.set()
        result = await asyncio.wait_for(running, 10)
        assert result['status'] == expected, result
        assert result['totalTokens'] == 810 and result['costUsd'] == pytest.approx(.007), result
        assert result['modelCalls'] == (3 if expected == 'completed' else 2) + int(detached)
        assert backend.calls == (2 if expected == 'completed' else 1) + int(detached)
    finally:
        release.set()
        await asyncio.gather(*children, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 5)


@pytest.mark.parametrize('mode,errors_only,expected', [('both', False, 'enqueued'), ('both', True, 'suppressed'), ('inherit', False, 'enqueued'), ('silent', False, 'suppressed')])
async def test_notification_channels_after_post_and_recovery_no_duplicates(cron_env, monkeypatch, mode, errors_only, expected):
    from app.web_task_telegram import WebTaskTelegramNotifier
    from tests.test_web_task_telegram import _config
    from app.web_push import BrowserPush
    from tests.test_web_push import add_device
    from app.cron import executor
    from app.cron.notifications import deliver
    e = cron_env
    e.server.web_task_telegram = WebTaskTelegramNotifier(_config(), e.db, SimpleNamespace())
    e.server.browser_push = BrowserPush(e.db)
    await add_device(e.db)
    from app.web_console.core import _sha256
    cookie = await _login_cookie(e)
    await e.db.conn.execute('UPDATE web_push_subscriptions SET session_token_hash=?', (_sha256(cookie),)); await e.db.conn.commit()
    original_stage = executor.stage
    async def stage(*args, **kwargs):
        if args[3] == 'post':
            assert not await many(e.db.conn, 'SELECT * FROM web_tg_notification_runs')
            assert not await many(e.db.conn, 'SELECT * FROM web_push_deliveries')
        return await original_stage(*args, **kwargs)
    monkeypatch.setattr(executor, 'stage', stage)
    result = await run(e, await create(e, notifications={'mode': mode, 'errorsOnly': errors_only}, post={'enabled': True, 'code': "print('notified later')"}))
    assert result['notificationState'] == expected, result
    assert len(await many(e.db.conn, 'SELECT * FROM web_push_deliveries')) == int(expected == 'enqueued')
    # inherit keeps TG's configured duration threshold; explicit mode bypasses it.
    expected_tg = int(expected == 'enqueued' and mode == 'both')
    assert len(await many(e.db.conn, 'SELECT * FROM web_tg_notification_outbox')) == expected_tg
    await e.db.conn.execute("UPDATE cron_runs SET notification_state='pending' WHERE run_id=?", (result['id'],)); await e.db.conn.commit()
    await deliver(e.cron, await one(e.db.conn, 'SELECT * FROM cron_runs WHERE run_id=?', (result['id'],)))
    assert len(await many(e.db.conn, 'SELECT * FROM web_push_deliveries')) == int(expected == 'enqueued')
    assert len(await many(e.db.conn, 'SELECT * FROM web_tg_notification_outbox')) == expected_tg


async def test_restart_marks_active_interrupted_and_past_once_missed(cron_env):
    e = cron_env; now = e.cron.clock()
    job = await create(e, schedule={'kind': 'at', 'at': iso(now + 1000)})
    async with e.db.write_transaction(label='interrupted-fixture') as conn:
        row = await e.cron.row(conn, 123, job['id'])
        old = await e.cron.accept_run(conn, row, now, 'manual', 'old')
    e.cron.clock = lambda: now + 10000
    await e.cron.start()
    assert (await e.cron.get(123, job['id']))['job']['scheduleState'] == 'missed'
    assert (await e.cron.get_run(123, old['run_id']))['run']['status'] == 'interrupted'
    assert len(await many(e.db.conn, 'SELECT * FROM cron_runs')) == 1


async def test_frontend_deep_link_and_editor_dependencies(cron_env, monkeypatch):
    e = cron_env
    # Route tests must not depend on a developer's untracked web/dist build.
    dist = e.tmp / 'dist'
    dist.mkdir()
    index_html = '<html><body>Cron route fixture</body></html>'
    (dist / 'index.html').write_text(index_html, encoding='utf-8')
    monkeypatch.setattr(e.server, '_web_dist_dir', lambda: dist)
    cookies = {'openbear_web_session': await _login_cookie(e)}
    response = await e.client.get('/cron?folder=folder', cookies=cookies)
    assert response.status == 200
    assert await response.text() == index_html
    response = await e.client.get('/api/cron/environment', cookies=cookies)
    assert response.status == 200
    assert next(r for r in (await response.json())['runtimes'] if r['runtime'] == 'python')['available']
