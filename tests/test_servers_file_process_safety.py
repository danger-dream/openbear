"""File/process safety contracts; all operations use in-memory fakes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.instances.contracts import InstanceError
from app.servers import service as module
from app.servers.service import ServerService
from tests.test_servers import api, base_env, env, shipped  # noqa: F401


def service(features=('host', 'host.processIdentity', 'fs.write.noReplace')):
    s = object.__new__(ServerService)
    s.instances = SimpleNamespace(servers=SimpleNamespace(get=AsyncMock(return_value={})))
    s.hub = SimpleNamespace(sessions={'node': SimpleNamespace(features=set(features), caps={'exec': True}, info={})})
    s.file_jobs = {}
    s._log = AsyncMock()
    s._node_call = AsyncMock(return_value={})
    return s


async def pieces():
    yield b'first'
    yield b'second'


async def test_no_replace_reaches_every_chunk_and_final_safe_rpc():
    s = service()
    await s.write_file(1, 'node', '/tmp/target', pieces(), no_replace=True)
    calls = s._node_call.await_args_list
    assert len(calls) == 2
    assert [c.args[2] for c in calls] == ['fs.writeSafe', 'fs.writeSafe']
    assert [c.args[3]['noReplace'] for c in calls] == [True, True]
    assert [c.args[3]['done'] for c in calls] == [False, True]
    assert [c.args[3]['offset'] for c in calls] == [0, 5]
    assert calls[0].args[3]['upload'] == calls[1].args[3]['upload']


async def test_no_replace_old_node_refused_before_consuming_body_and_no_unsafe_fallback():
    s = service(features=('host',))
    consumed = False

    async def body():
        nonlocal consumed
        consumed = True
        yield b'data'

    with pytest.raises(InstanceError, match='node_update_required'):
        await s.write_file(1, 'node', '/tmp/target', body(), no_replace=True)
    assert not consumed
    s._node_call.assert_not_awaited()
    # Explicit overwrite remains available with N-1 nodes.
    await s.write_file(1, 'node', '/tmp/target', pieces())
    assert all(c.args[2] == 'fs.write' for c in s._node_call.await_args_list)
    # A reconnect to an older node between the feature check and dispatch must
    # reject the new method, never retry the legacy method that ignores flags.
    s = service()
    s._node_call.side_effect = InstanceError('unsupported_method')
    with pytest.raises(InstanceError, match='node_update_required'):
        await s.write_file(1, 'node', '/tmp/target', pieces(), no_replace=True)
    assert s._node_call.await_count == 1
    assert s._node_call.await_args.args[2] == 'fs.writeSafe'


async def test_process_identity_forwarded_for_confirm_and_force_and_old_nodes_refused():
    s = service()
    s._node_call.return_value = {'exited': False}
    for force in (False, True):
        result = await s.kill_process(1, 'node', {'pid': 42, 'name': 'same-name', 'identity': 'epoch:42:start', 'force': force})
        assert result['identity'] == 'epoch:42:start'
        assert s._node_call.await_args.args[2:] == ('host.killIdentity', {'pid': 42, 'force': force, 'identity': 'epoch:42:start'})
    with pytest.raises(InstanceError, match='process_identity_required'):
        await s.kill_process(1, 'node', {'pid': 42})
    old = service(features=('host',))
    with pytest.raises(InstanceError, match='node_update_required'):
        await old.kill_process(1, 'node', {'pid': 42, 'identity': 'epoch:42:start'})
    old._node_call.assert_not_awaited()
    s._node_call.reset_mock()
    s._node_call.side_effect = InstanceError('unsupported_method')
    with pytest.raises(InstanceError, match='node_update_required'):
        await s.kill_process(1, 'node', {'pid': 42, 'identity': 'epoch:42:start'})
    assert s._node_call.await_count == 1
    assert s._node_call.await_args.args[2] == 'host.killIdentity'


def receipt(i, state='running', created=0):
    return {'id': str(i), 'state': state, 'createdAtMs': created, 'owner': 1, 'serverId': 'node', 'op': 'compress'}


async def test_file_receipts_age_and_capacity_evict_only_terminal(monkeypatch):
    s = service()
    monkeypatch.setattr(module.file_ops, 'command', lambda *args: ('fake-command', 60))
    monkeypatch.setattr(module, 'now_ms', lambda: 10_000_000)
    spawned = []
    s.spawn = lambda coro: (spawned.append(True), coro.close())
    s.file_jobs = {str(i): receipt(i) for i in range(50)}
    with pytest.raises(InstanceError, match='file_jobs_busy'):
        await s.file_op(1, 'node', {'op': 'compress'})
    assert not spawned and len(s.file_jobs) == 50
    assert (await s.file_op_job(1, 'node', '0'))['job']['state'] == 'running'
    # A single long operation survives many newer completed receipts too.
    for i in range(1, 50):
        s.file_jobs[str(i)] = receipt(i, 'done', 9_999_999)
    s.file_jobs['2']['createdAtMs'] = 0  # age eviction, not just capacity
    await s.file_op(1, 'node', {'op': 'compress'})
    assert '0' in s.file_jobs and '2' not in s.file_jobs and len(s.file_jobs) == 50
    await s.file_op(1, 'node', {'op': 'compress'})
    assert '0' in s.file_jobs and '1' not in s.file_jobs and len(s.file_jobs) == 50
    assert len(spawned) == 2


async def test_upload_http_default_no_replace_and_conflict_response(env, monkeypatch):
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'file-route-fake'}))['item']['id']
    calls = []

    async def stat(*args):
        raise InstanceError('not_found', 404)

    async def write(owner, server, path, chunks, **kw):
        data = b''.join([part async for part in chunks])
        calls.append((server, path, data, kw['no_replace']))
        if kw['no_replace']:
            raise InstanceError('file_exists', 409)
        return {'ok': True}

    monkeypatch.setattr(e.servers, 'stat_file', stat)
    monkeypatch.setattr(e.servers, 'write_file', write)
    for query, status in (('', 409), ('&overwrite=1', 200)):
        response = await e.app.post(f'/api/servers/{sid}/files/upload?path=/tmp/target{query}', data=b'bytes', cookies=e.cookies, headers=e.origin)
        assert response.status == status
        result = await response.json()
        assert result == ({'ok': False, 'error': 'file_exists'} if status == 409 else {'ok': True})
    assert calls == [(sid, '/tmp/target', b'bytes', True), (sid, '/tmp/target', b'bytes', False)]
