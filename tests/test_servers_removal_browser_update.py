"""Removal stops work before disconnecting; Chrome updates are explicit and feature-gated.
All nodes and downloads here are local fakes, never installed servers or browsers.
"""
import asyncio
import time

import pytest

from app.instances.contracts import InstanceError
from app.servers import service as service_module
from app.webhooks.repository import one
from tests.test_servers import api, env, shipped, start_fake  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


async def server_with_node(e, browser=False):
    sid = (await api(e, 'POST', '/api/servers', {'name': 'test-only'}))['item']['id']
    node, *_ = await start_fake(e, sid, caps={'exec': True, 'browser': browser})
    return sid, node


@pytest.mark.parametrize('uninstall', [False, True])
async def test_removal_stops_jobs_and_benchmarks_before_disconnect(env, monkeypatch, uninstall):
    e = env
    sid, _ = await server_with_node(e)
    events = []
    async with e.db.write_transaction(label='test-runs') as conn:
        await conn.execute("INSERT INTO server_bench_runs(id,owner,server_id,test,state,created_ms) VALUES('queued',123,?,'netcheck','queued',1)", (sid,))
    stop_bench = e.servers.bench.stop_server

    async def stopping_bench(owner, rid):
        assert sid in e.servers.hub.sessions
        events.append(('bench', rid))
        return await stop_bench(owner, rid)

    async def listing(owner, server_id):
        assert sid in e.servers.hub.sessions
        events.append(('list', server_id))
        return {'jobs': [{'id': 'running1', 'state': 'running', 'command': 'test task'},
                         {'id': 'finished1', 'state': 'exited', 'command': 'already done'}]}

    async def stopping(owner, server_id, job_id, **kw):
        assert sid in e.servers.hub.sessions
        events.append(('stop', job_id))
        return {'stopped': True, 'job': {'state': 'exited'}}

    call = e.servers._node_call

    async def node_call(owner, server_id, method, params, **kw):
        if method == 'uninstall':
            events.append(('uninstall', server_id))
            return {}
        return await call(owner, server_id, method, params, **kw)

    disconnect = e.servers.hub.disconnect

    async def disconnecting(server_id, *args):
        events.append(('disconnect', server_id))
        return await disconnect(server_id, *args)

    monkeypatch.setattr(e.servers.bench, 'stop_server', stopping_bench)
    monkeypatch.setattr(e.servers, 'jobs_list', listing)
    monkeypatch.setattr(e.servers, 'job_stop', stopping)
    monkeypatch.setattr(e.servers, '_node_call', node_call)
    monkeypatch.setattr(e.servers.hub, 'disconnect', disconnecting)
    suffix = '/node?uninstall=1' if uninstall else ''
    out = await api(e, 'DELETE', f'/api/servers/{sid}{suffix}')
    assert out['ok'] and not out['work']['failed']
    assert {(x['kind'], x['id']) for x in out['work']['stopped']} == {('job', 'running1'), ('bench', 'queued')}
    assert events == [('bench', sid), ('list', sid), ('stop', 'running1'), *([('uninstall', sid)] if uninstall else []), ('disconnect', sid)]
    assert await one(e.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=?', (sid,)) is None
    assert (sid in e.servers.bench.paused) is not uninstall


@pytest.mark.parametrize('uninstall', [False, True])
@pytest.mark.parametrize('bench_failure', [False, True])
async def test_offline_removal_reports_work_could_not_be_stopped(env, monkeypatch, uninstall, bench_failure):
    e = env
    sid, _ = await server_with_node(e)
    # Hide the session without reconnecting it; removal still revokes the stored registration.
    session = e.servers.hub.sessions.pop(sid)
    if bench_failure:
        async def offline_stop(owner, server_id):
            e.servers.bench.paused.add(server_id)
            raise InstanceError('node_offline', 409)
        monkeypatch.setattr(e.servers.bench, 'stop_server', offline_stop)
    try:
        out = await api(e, 'DELETE', f'/api/servers/{sid}' + ('/node?uninstall=1' if uninstall else ''))
        assert out['work']['offline']
        assert out['work']['note'] == '节点离线，没能停止它上面仍在运行的任务'
        assert await one(e.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=?', (sid,)) is None
    finally:
        # Keep fixture cleanup responsible for the fake connection.
        e.servers.hub.sessions[sid] = session


async def test_one_shared_deadline_and_online_failures_preserve_management(env, monkeypatch):
    e = env
    sid, _ = await server_with_node(e)
    monkeypatch.setattr(service_module, 'WORK_STOP_TIMEOUT', .2)

    async def listing(*args):
        return {'jobs': [{'id': x, 'state': 'running', 'command': x} for x in ['broken1', 'slowjob', 'later01']]}

    async def stopping(owner, server_id, job_id, **kw):
        if job_id == 'broken1':
            raise InstanceError('permission_denied')
        await asyncio.sleep(60)

    monkeypatch.setattr(e.servers, 'jobs_list', listing)
    monkeypatch.setattr(e.servers, 'job_stop', stopping)
    before = time.monotonic()
    out = await api(e, 'DELETE', f'/api/servers/{sid}', status=409)
    assert out['error'] == 'server_work_stop_failed'
    assert sid in e.servers.hub.sessions
    assert sid not in e.servers.bench.paused
    assert time.monotonic() - before < 3
    assert {x['id']: x['error'] for x in out['errors']} == {'broken1': 'permission_denied', 'slowjob': 'timeout', 'later01': 'timeout'}
    assert await one(e.db.conn, 'SELECT 1 FROM instance_servers WHERE id=?', (sid,)) is not None


async def test_refused_deletion_does_not_stop_work(env, monkeypatch):
    e = env
    sid, _ = await server_with_node(e)

    async def usage(owner):
        return {sid: ['in use']}

    async def forbidden(*args):
        pytest.fail('stopped work for a server that cannot be deleted')

    monkeypatch.setattr(e.servers.registry, 'usage', usage)
    monkeypatch.setattr(e.servers, '_stop_work', forbidden)
    out = await api(e, 'DELETE', f'/api/servers/{sid}', status=409)
    assert out['error'] == 'server_in_use'


async def test_browser_check_update_status_and_old_node(env):
    e = env
    sid, node = await server_with_node(e, browser=True)
    node.browser.chrome_version = '130.0.6723.116'
    path = f'/api/servers/{sid}/browser'
    state = await api(e, 'GET', path)
    assert state['browser']['chromeVersion'] == '130.0.6723.116'
    checked = await api(e, 'POST', path + '/update', {'check': True})
    assert checked['update'] == {'current': '130.0.6723.116', 'latest': node.browser.chrome_latest, 'upToDate': False, 'started': False}
    assert node.browser.chrome_version == '130.0.6723.116'
    updated = await api(e, 'POST', path + '/update', {'restart': False})
    assert updated['update']['started']
    assert (await api(e, 'GET', path))['browser']['update']['state'] == 'done'
    assert (await api(e, 'POST', path + '/update', {'check': True}))['update']['upToDate']
    session = e.servers.hub.sessions[sid]
    session.features = session.features - {'browser.update'}
    assert (await api(e, 'POST', path + '/update', {}, status=409))['error'] == 'node_update_required'


@pytest.mark.parametrize('body', [{'restart': 'yes'}, {'check': 1}, {'unexpected': True}])
async def test_browser_update_rejects_invalid_options(env, body):
    e = env
    sid, _ = await server_with_node(e, browser=True)
    out = await api(e, 'POST', f'/api/servers/{sid}/browser/update', body, status=400)
    assert out['error'] == 'invalid_browser_update'


async def test_browser_update_reports_no_download_and_offline(env):
    e = env
    sid, _ = await server_with_node(e, browser=True)
    path = f'/api/servers/{sid}/browser/update'
    assert (await api(e, 'POST', path, {}, status=409))['error'] == 'browser_not_downloaded'
    session = e.servers.hub.sessions.pop(sid)
    try:
        assert (await api(e, 'POST', path, {}, status=409))['error'] == 'node_offline'
    finally:
        e.servers.hub.sessions[sid] = session


async def test_disconnect_without_uninstall_preserves_work(env, monkeypatch):
    e = env
    sid, _ = await server_with_node(e)

    async def forbidden(*args):
        pytest.fail('disconnect-only must not stop independent tasks')

    monkeypatch.setattr(e.servers, '_stop_work', forbidden)
    out = await api(e, 'DELETE', f'/api/servers/{sid}/node')
    assert out['ok'] and 'work' not in out


@pytest.mark.parametrize('uninstall', [False, True])
@pytest.mark.parametrize('failure', ['unconfirmed', 'timeout'])
async def test_benchmark_fence_failure_blocks_removal_and_reopens_scheduling(env, monkeypatch, uninstall, failure):
    e = env
    sid, _ = await server_with_node(e)
    monkeypatch.setattr(service_module, 'WORK_STOP_TIMEOUT', .2)

    async def stop_server(owner, server_id):
        e.servers.bench.paused.add(server_id)
        if failure == 'timeout':
            await asyncio.sleep(60)
        raise InstanceError('bench_run_active', 409)

    async def listing(*args):
        return {'jobs': []}

    async def forbidden(*args, **kwargs):
        pytest.fail('must not uninstall or disconnect before stop is confirmed')

    monkeypatch.setattr(e.servers.bench, 'stop_server', stop_server)
    monkeypatch.setattr(e.servers, 'jobs_list', listing)
    with monkeypatch.context() as patch:
        patch.setattr(e.servers, '_node_call', forbidden)
        patch.setattr(e.servers.hub, 'disconnect', forbidden)
        out = await api(e, 'DELETE', f'/api/servers/{sid}' + ('/node?uninstall=1' if uninstall else ''), status=409)
    assert out['error'] == 'server_work_stop_failed'
    assert out['errors'][0]['error'] == ('timeout' if failure == 'timeout' else 'bench_run_active')
    assert sid not in e.servers.bench.paused
    assert sid in e.servers.hub.sessions
    assert await one(e.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=?', (sid,)) is not None


async def test_job_stop_reply_must_confirm_a_terminal_state(env, monkeypatch):
    e = env
    sid, _ = await server_with_node(e)

    async def listing(*args):
        return {'jobs': [{'id': 'running1', 'state': 'running', 'command': 'still running'}]}

    async def stopping(*args, **kw):
        return {'stopped': True, 'job': {'state': 'running'}}

    monkeypatch.setattr(e.servers, 'jobs_list', listing)
    monkeypatch.setattr(e.servers, 'job_stop', stopping)
    out = await api(e, 'DELETE', f'/api/servers/{sid}/node?uninstall=1', status=409)
    assert out['errors'][0]['error'] == 'job_stop_unconfirmed'
    assert sid in e.servers.hub.sessions and sid not in e.servers.bench.paused


async def test_removal_waits_for_benchmark_start_receipts_before_listing_jobs(env, monkeypatch):
    e = env
    sid, _ = await server_with_node(e)
    entered, receipt = asyncio.Event(), asyncio.Event()
    events = []

    async def stop_server(owner, server_id):
        e.servers.bench.paused.add(server_id)
        entered.set()
        await receipt.wait()
        events.append('bench-stopped')
        return {'ok': True, 'runs': ['inflight']}

    async def listing(*args):
        events.append('list')
        return {'jobs': []}

    monkeypatch.setattr(e.servers.bench, 'stop_server', stop_server)
    monkeypatch.setattr(e.servers, 'jobs_list', listing)
    task = asyncio.create_task(e.servers.delete(123, sid))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not task.done() and not events
        assert sid in e.servers.hub.sessions
        assert await one(e.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=?', (sid,)) is not None
        receipt.set()
        assert (await task)['ok']
        assert events == ['bench-stopped', 'list']
    finally:
        receipt.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_aborted_database_removal_releases_the_benchmark_fence(env, monkeypatch):
    e = env
    sid, _ = await server_with_node(e)

    async def listing(*args):
        return {'jobs': []}

    async def failed(*args):
        raise InstanceError('server_in_use', 409)

    monkeypatch.setattr(e.servers, 'jobs_list', listing)
    monkeypatch.setattr(e.servers.registry, 'delete', failed)
    assert (await api(e, 'DELETE', f'/api/servers/{sid}', status=409))['error'] == 'server_in_use'
    assert sid not in e.servers.bench.paused
    assert sid in e.servers.hub.sessions


async def test_rejecting_a_duplicate_registration_does_not_stop_original_machine_jobs(env, monkeypatch):
    import json
    e = env
    sid, _ = await server_with_node(e)
    async with e.db.write_transaction(label='test-duplicate-registration') as conn:
        row = await (await conn.execute('SELECT info_json FROM server_nodes WHERE server_id=?', (sid,))).fetchone()
        stored = json.loads(row['info_json'])
        stored['joinPending'] = {'serverId': 'original', 'name': 'original'}
        await conn.execute('UPDATE server_nodes SET info_json=? WHERE server_id=?', (json.dumps(stored), sid))

    async def forbidden(*args):
        pytest.fail('rejecting a duplicate registration must not stop the original machine tasks')

    monkeypatch.setattr(e.servers, '_stop_work', forbidden)
    assert (await api(e, 'POST', f'/api/servers/{sid}/join', {'action': 'reject'}))['ok']


async def test_revoking_duplicate_with_uninstall_does_not_touch_original_machine(env, monkeypatch):
    import json
    e = env
    sid, _ = await server_with_node(e)
    async with e.db.write_transaction(label='test-duplicate-revoke') as conn:
        row = await (await conn.execute('SELECT info_json FROM server_nodes WHERE server_id=?', (sid,))).fetchone()
        stored = json.loads(row['info_json'])
        stored['joinPending'] = {'serverId': 'original', 'name': 'original'}
        await conn.execute('UPDATE server_nodes SET info_json=? WHERE server_id=?', (json.dumps(stored), sid))

    async def forbidden(*args, **kwargs):
        pytest.fail('must not stop or uninstall the original machine through its duplicate')

    monkeypatch.setattr(e.servers, '_stop_work', forbidden)
    monkeypatch.setattr(e.servers, '_node_call', forbidden)
    out = await api(e, 'DELETE', f'/api/servers/{sid}/node?uninstall=1')
    assert out['ok'] and not out['uninstalled'] and 'work' not in out
    assert '重复登记仅注销' in out['note']
    assert await one(e.db.conn, 'SELECT 1 FROM instance_servers WHERE id=?', (sid,)) is not None


@pytest.mark.parametrize('uninstall', [False, True])
async def test_pre_jobs_node_can_be_removed_without_requiring_an_upgrade(env, monkeypatch, uninstall):
    e = env
    sid, _ = await server_with_node(e)
    e.servers.hub.sessions[sid].features -= {'jobs'}

    async def forbidden(*args, **kwargs):
        pytest.fail('a pre-jobs node has no independent jobs API to enumerate')

    monkeypatch.setattr(e.servers, 'jobs_list', forbidden)
    suffix = '/node?uninstall=1' if uninstall else ''
    out = await api(e, 'DELETE', f'/api/servers/{sid}{suffix}')
    assert out['ok'] and not out['work']['failed']
    assert await one(e.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=?', (sid,)) is None


@pytest.mark.parametrize('uninstall', [False, True])
async def test_advertised_jobs_api_failure_is_not_treated_as_a_pre_jobs_node(env, monkeypatch, uninstall):
    e = env
    sid, _ = await server_with_node(e)
    assert 'jobs' in e.servers.hub.sessions[sid].features
    original = e.servers._node_call

    async def unavailable(owner, server_id, method, params, **kwargs):
        if method == 'job.list':
            raise InstanceError('unsupported_method', 409)
        if method == 'uninstall':
            pytest.fail('must not uninstall after an advertised jobs API failed')
        return await original(owner, server_id, method, params, **kwargs)

    monkeypatch.setattr(e.servers, '_node_call', unavailable)
    suffix = '/node?uninstall=1' if uninstall else ''
    out = await api(e, 'DELETE', f'/api/servers/{sid}{suffix}', status=409)
    assert out['error'] == 'server_work_stop_failed'
    assert out['errors'] == [{'kind': 'jobs', 'id': '', 'label': '列出后台任务', 'error': 'node_update_required'}]
    assert sid in e.servers.hub.sessions and sid not in e.servers.bench.paused
    assert await one(e.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=?', (sid,)) is not None


async def test_missing_jobs_feature_does_not_bypass_unconfirmed_benchmark_stop(env, monkeypatch):
    e = env
    sid, _ = await server_with_node(e)
    e.servers.hub.sessions[sid].features -= {'jobs'}

    async def unconfirmed(owner, server_id):
        e.servers.bench.paused.add(server_id)
        raise InstanceError('bench_run_active', 409)

    monkeypatch.setattr(e.servers.bench, 'stop_server', unconfirmed)
    out = await api(e, 'DELETE', f'/api/servers/{sid}', status=409)
    assert out['error'] == 'server_work_stop_failed'
    assert out['errors'][0]['kind'] == 'bench'
    assert sid in e.servers.hub.sessions and sid not in e.servers.bench.paused
