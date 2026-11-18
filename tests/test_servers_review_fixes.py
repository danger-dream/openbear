"""Controller review regressions: byte-accurate job pages, 50-job budgets,
explicit residual-job removal and front-command status. No real node jobs/installers.
"""
import codecs
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.instances.contracts import InstanceError
from app.servers import install, service as service_module
from app.servers.service import ServerService
from app.tools import servers as tools
from tests.test_servers import api, env, shipped, start_fake  # noqa: F401
from tests.test_servers_tool_integrity import caller  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


def node_log(service, content, *, first=0, legacy=False):
    """The existing node RPC only has decoded text and original-byte cursors.
    Model its complete-UTF-8 boundaries; invalid 0xff bytes become U+FFFD in Go JSON.
    No raw/base64 field or new feature flag is supplied, including for N-1.
    """
    receipts = []

    async def status(owner, sid, jid, offset=None, limit=None):
        size = first + len(content)
        offset = max(first, size - limit) if offset is None else min(size, max(first, offset))
        raw = content[offset - first:offset - first + limit]
        skipped = 0
        while raw and skipped < 3 and raw[0] & 0xc0 == 0x80:
            raw, offset, skipped = raw[1:], offset + 1, skipped + 1
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        text = decoder.decode(raw, final=False)
        consumed = len(raw) - len(decoder.getstate()[0])
        job = {'id': jid, 'command': '\\"' * 4000, 'cwd': '/' + 'x' * 4000, 'shell': 'sh', 'state': 'running',
               'exitCode': None, 'startedAtMs': 1794916800000, 'endedAtMs': None, 'durationMs': 1000000}
        if not legacy:
            job['canStop'] = True
        log = {'data': text, 'offset': offset, 'next': offset + consumed, 'size': size, 'first': first}
        receipts.append((sid, jid, dict(log)))
        return {'job': job, 'log': log}

    service.job_status.side_effect = status
    return receipts


def assert_node_page(row, receipts, sid, jid):
    # Not just a plausible re-encoding: the whole text/cursor pair must have come
    # from one actual node reply. This fails the old local slicing implementation.
    assert any((s, j) == (sid, jid) and (log['data'], log['offset'], log['next'], log['size']) ==
               (row['output'], row['outputOffset'], row['next'], row['outputSize']) for s, j, log in receipts)
    assert row['next'] <= row['outputSize']


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('content', [b'\xff' * 50000, (b'\xff' + '文🙂\ufffd\\"\n'.encode()) * 6000])
async def test_non_utf8_job_pages_use_node_byte_boundaries(caller, legacy, content):
    service, call = caller
    receipts = node_log(service, content, legacy=legacy)
    offset, text = 0, []
    for _ in range(100):
        row, _ = await call('job_status', serverId='srv', jobId='job123', offset=offset)
        assert row['ok'] and row['outputOffset'] == offset
        assert_node_page(row, receipts, 'srv', 'job123')
        text.append(row['output'])
        assert row['next'] > offset
        offset = row['next']
        if not row['hasMore']:
            break
    else:
        pytest.fail('log pagination did not finish')
    assert offset == len(content)
    assert ''.join(text) == content.decode(errors='replace')


async def test_oversize_tail_refetch_anchors_original_start_and_keeps_rotation_marker(caller):
    service, call = caller
    # JSON escaping makes this 8000-byte default tail exceed the output budget.
    content = b'\x00' * 30000
    first = 20000000
    receipts = node_log(service, content, first=first)
    row = await tools.ServersTool(service).do_job_status(123, 'srv', {'jobId': 'job123'}, None)
    # Direct handler read avoids the generic untrusted-data sanitizer stripping NULs.
    assert row['outputOffset'] == first + len(content) - tools.JOB_TAIL
    assert row['droppedBefore'] == first and row['hasMore']
    assert_node_page(row, receipts, 'srv', 'job123')
    assert len(receipts) > 1
    assert all(log['offset'] == receipts[0][2]['offset'] for _, _, log in receipts)
    assert tools._size(row) <= tools.OUTPUT_BUDGET - 1024


async def test_tiny_log_limit_still_delivers_one_complete_character(caller):
    service, call = caller
    receipts = node_log(service, '🙂文'.encode())
    row, _ = await call('job_status', serverId='srv', jobId='job123', offset=0, limit=1)
    assert row['output'] == '🙂' and row['next'] == 4 and row['hasMore']
    assert_node_page(row, receipts, 'srv', 'job123')
    node_log(service, b'ABCD')
    row, _ = await call('job_status', serverId='srv', jobId='job123', offset=0, limit=1)
    assert row['output'] == 'A' and row['next'] == 1


@pytest.mark.parametrize('name,first', [('a' * 60, 19000000), ('\\' * 60, 2**63 - 200000)])
async def test_fifty_real_metadata_jobs_have_independent_continuable_pages(caller, monkeypatch, name, first):
    service, call = caller
    content = (b'\xff' + '文🙂\\"\n'.encode()) * 8000
    receipts = node_log(service, content, first=first)
    refs = [{'serverId': f'00000000-0000-0000-0000-{i:012}', 'jobId': f'{i:040}', 'offset': first} for i in range(50)]
    monkeypatch.setattr(tools.ServersTool, 'server_names', AsyncMock(return_value={r['serverId']: name for r in refs}))
    prefixes = [''] * 50
    for _ in range(3):
        result, _ = await call('job_status', jobs=refs, limit=100000)
        assert result['ok'] and result['count'] == 50 and result['states'] == {'running': 50}
        assert tools._size(result) <= tools.OUTPUT_BUDGET
        for i, (ref, row) in enumerate(zip(refs, result['results'])):
            assert (row['serverId'], row['jobId']) == (ref['serverId'], ref['jobId'])
            assert row['outputOffset'] == ref['offset'] < row['next']
            assert_node_page(row, receipts, ref['serverId'], ref['jobId'])
            prefixes[i] += row['output']
            assert prefixes[i] == content[:row['next'] - first].decode(errors='replace')
            assert row['hasMore'] and row['jobSummary'] and row['droppedBefore'] == first
            ref['offset'] = row['next']
    # Narrowing back to one item restores the job identity/more details and resumes
    # the same original-byte stream (no logs skipped by batch summarization).
    ref = refs[0]
    row, _ = await call('job_status', serverId=ref['serverId'], jobId=ref['jobId'], offset=ref['offset'])
    assert row['job']['id'] == ref['jobId'] and row['outputOffset'] == ref['offset']
    assert prefixes[0] + row['output'] == content[:row['next'] - first].decode(errors='replace')


async def test_refetch_connection_failure_only_fails_that_batch_item(caller):
    service, call = caller
    node_log(service, b'\xff' * 90000)
    original = service.job_status.side_effect
    reads = {}

    async def failing(owner, sid, jid, **kw):
        reads[sid] = reads.get(sid, 0) + 1
        if sid == 'srv0' and reads[sid] > 1:
            raise InstanceError('node_connection_lost', 502)
        return await original(owner, sid, jid, **kw)

    service.job_status.side_effect = failing
    refs = [{'serverId': f'srv{i}', 'jobId': 'job123', 'offset': 0} for i in range(50)]
    result, _ = await call('job_status', jobs=refs, limit=100000)
    assert not result['ok'] and result['count'] == 50
    assert result['results'][0]['error'] == 'node_connection_lost'
    assert all(row['ok'] and row['next'] > 0 for row in result['results'][1:])
    assert tools._size(result) <= tools.OUTPUT_BUDGET


@pytest.fixture
def stopping_service(monkeypatch):
    monkeypatch.setattr(service_module, 'one', AsyncMock(return_value={'enrolled': 1}))
    return SimpleNamespace(db=SimpleNamespace(conn=object()),
                           hub=SimpleNamespace(sessions={'srv': SimpleNamespace(features={'jobs'})}),
                           bench=SimpleNamespace(stop_server=AsyncMock(return_value={'ok': True, 'runs': []})),
                           jobs_list=AsyncMock(), job_stop=AsyncMock(), job_status=AsyncMock())


async def test_only_explicit_residuals_and_legacy_running_jobs_are_stopped(stopping_service):
    service = stopping_service
    service.jobs_list.return_value = {'jobs': [
        {'id': 'orphan1', 'command': 'child', 'state': 'interrupted', 'canStop': True},
        {'id': 'orphan2', 'command': 'child', 'state': 'stopped', 'canStop': True},
        {'id': 'legacy1', 'command': 'running', 'state': 'running'},
        *[{'id': f'quiet{i}', 'command': '', 'state': 'interrupted', 'canStop': value}
          for i, value in enumerate((False, None, 'true', 1))],
    ]}
    service.job_stop.side_effect = [{'job': {'state': 'stopped', 'canStop': False}},
                                    {'job': {'state': 'interrupted', 'canStop': False}},
                                    {'job': {'state': 'exited'}}]
    result = await ServerService._stop_work(service, 123, 'srv')
    assert [c.args[2] for c in service.job_stop.await_args_list] == ['orphan1', 'orphan2', 'legacy1']
    assert [j['id'] for j in result['stopped']] == ['orphan1', 'orphan2', 'legacy1'] and not result['failed']
    service.job_status.assert_not_awaited()


@pytest.mark.parametrize('uninstall', [False, True])
async def test_removal_waits_for_residual_clear_before_forgetting_or_uninstalling(env, monkeypatch, uninstall):
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'residual-only-fixture'}))['item']['id']
    await start_fake(e, sid)
    cleared = False
    monkeypatch.setattr(e.servers, 'jobs_list', AsyncMock(return_value={'jobs': [
        {'id': 'orphan1', 'command': 'fixture, never executed', 'state': 'interrupted', 'canStop': True}]}))
    stop = AsyncMock(return_value={'job': {'state': 'stopped', 'canStop': True}})
    monkeypatch.setattr(e.servers, 'job_stop', stop)

    async def status(*args, **kwargs):
        nonlocal cleared
        cleared = True
        return {'job': {'state': 'stopped', 'canStop': False}}
    monkeypatch.setattr(e.servers, 'job_status', status)
    disconnect, node_call = e.servers.hub.disconnect, e.servers._node_call

    async def disconnecting(*args):
        assert cleared
        return await disconnect(*args)

    async def uninstalling(owner, server_id, method, params, **kwargs):
        if method == 'uninstall':
            assert cleared
            return {}
        return await node_call(owner, server_id, method, params, **kwargs)
    monkeypatch.setattr(e.servers.hub, 'disconnect', disconnecting)
    monkeypatch.setattr(e.servers, '_node_call', uninstalling)
    result = await api(e, 'DELETE', f'/api/servers/{sid}' + ('/node?uninstall=1' if uninstall else ''))
    assert result['ok'] and result['work']['stopped'][0]['id'] == 'orphan1'
    stop.assert_awaited_once()


@pytest.mark.parametrize('uninstall', [False, True])
async def test_unconfirmed_residual_keeps_registration_and_management(env, monkeypatch, uninstall):
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'unconfirmed-fixture'}))['item']['id']
    await start_fake(e, sid)
    monkeypatch.setattr(service_module, 'WORK_STOP_TIMEOUT', .02)
    monkeypatch.setattr(e.servers, 'jobs_list', AsyncMock(return_value={'jobs': [
        {'id': 'orphan1', 'command': 'fixture, never executed', 'state': 'interrupted', 'canStop': True}]}))
    monkeypatch.setattr(e.servers, 'job_stop', AsyncMock(return_value={'stopped': True, 'job': {'state': 'stopped', 'canStop': True}}))
    with monkeypatch.context() as patch:
        # Neither side effect may happen while a confirmed residual is still alive.
        patch.setattr(e.servers.hub, 'disconnect', AsyncMock(side_effect=AssertionError('must not disconnect')))
        patch.setattr(e.servers, '_node_call', AsyncMock(side_effect=AssertionError('must not uninstall')))
        result = await api(e, 'DELETE', f'/api/servers/{sid}' + ('/node?uninstall=1' if uninstall else ''), status=409)
    assert result['error'] == 'server_work_stop_failed' and result['errors'][0]['error'] == 'timeout'
    assert sid in e.servers.hub.sessions and sid not in e.servers.bench.paused
    assert (await api(e, 'GET', f'/api/servers/{sid}'))['item']['node'] is not None


@pytest.mark.parametrize('reported,error', [(True, 'timeout'), (None, 'job_stop_unconfirmed')])
async def test_terminal_state_is_not_proof_residual_process_is_gone(stopping_service, monkeypatch, reported, error):
    service = stopping_service
    monkeypatch.setattr(service_module, 'WORK_STOP_TIMEOUT', .02)
    service.jobs_list.return_value = {'jobs': [{'id': 'orphan1', 'command': 'child', 'state': 'interrupted', 'canStop': True}]}
    service.job_stop.return_value = {'stopped': True, 'job': {'state': 'stopped', 'canStop': reported}}
    with pytest.raises(InstanceError) as raised:
        await ServerService._stop_work(service, 123, 'srv')
    assert raised.value.error == 'server_work_stop_failed'
    assert raised.value.errors[0]['error'] == error


async def test_ordinary_node_update_does_not_stop_background_jobs():
    service = SimpleNamespace(registry=SimpleNamespace(get=AsyncMock()), node_updates=set(),
                              hub=SimpleNamespace(sessions={'srv': SimpleNamespace(caps={'exec': True})}),
                              _update_node=AsyncMock(return_value={'ok': True}), _stop_work=AsyncMock(), job_stop=AsyncMock())
    assert await ServerService.update_node(service, 123, 'srv') == {'ok': True}
    service._stop_work.assert_not_awaited()
    service.job_stop.assert_not_awaited()


@pytest.mark.parametrize('mode,expected', [('success', 0), ('installer', 7), ('download', 22), ('checksum', 1), ('mktemp', 73), ('cleanup', 7)])
@pytest.mark.parametrize('errexit', [False, True])
def test_front_command_preserves_failure_status_and_cleans_temp(monkeypatch, tmp_path, mode, expected, errexit):
    fixture = 'echo fixture-ran\nexit ' + ('7' if mode in ('installer', 'cleanup') else '0') + '\n'
    monkeypatch.setattr(install, 'script', lambda: fixture)
    command = install.front_command('https://unused.invalid', 'fixture-code', pin='ab' * 32)
    # curl is always shadowed: no test reaches the network or runs an installer.
    shell = ('curl() { '
             + ('return 22; ' if mode == 'download' else
                'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then printf "%s" "$FIXTURE" > "$2"; return 0; fi; shift; done; return 99; ')
             + '}; ' + ('mktemp() { return 73; }; ' if mode == 'mktemp' else '')
             + ('rm() { command rm "$@"; return 31; }; ' if mode == 'cleanup' else ''))
    env_vars = {**os.environ, 'TMPDIR': str(tmp_path), 'FIXTURE': 'echo tampered\n' if mode == 'checksum' else fixture}
    result = subprocess.run(['sh', '-c', shell + ('set -e; ' if errexit else '') + command], env=env_vars, cwd=tmp_path,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == expected
    assert ('fixture-ran' in result.stdout) is (mode in ('success', 'installer', 'cleanup'))
    assert not list(tmp_path.iterdir())
    # A pasted failure must not exit its caller or replace the caller's EXIT trap.
    result = subprocess.run(['sh', '-c', shell + "trap 'echo parent-trap' 0; " + command + '; r=$?; echo caller:$r'],
                            env=env_vars, cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert f'caller:{expected}' in result.stdout and result.stdout.endswith('parent-trap\n')
    assert not list(tmp_path.iterdir())
