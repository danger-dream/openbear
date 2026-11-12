import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app.instances.contracts import InstanceError
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.servers import register_server_tools
from tests.test_servers import (api, base_env, env, node_binary, shipped, start_node, wait_for)  # noqa: F401


@pytest.fixture
async def transfer_env(env, node_binary):
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'transfer-node'}))['item']['id']
    await start_node(e, node_binary, sid)
    await wait_for(lambda: _ready(e, sid))
    async with e.db.write_transaction() as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-transfer',779,123,'t')")
    registry = ToolRegistry()
    register_server_tools(registry, e.servers)
    context = ToolRuntimeContext(chat_id=779, conversation_uuid='conv-transfer')

    async def tool(action, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': {'serverId': sid, **params}}), context=context))

    async def done(value):
        return (await tool('transfer_status', transferId=value['transfer']['id'], waitSeconds=60))['transfer']

    e.transfer_sid, e.transfer_tool, e.transfer_done = sid, tool, done
    e.transfer_registry, e.transfer_context = registry, context
    return e


async def _ready(e, sid):
    node = e.servers.hub.sessions.get(sid)
    return node is not None and 'fs.transfer' in node.features


async def test_node_transfer_tool_large_round_trip_overwrite_and_artifact(transfer_env):
    e = transfer_env
    described = await e.transfer_tool('describe', topic='files')
    assert set(described['contracts']) == {'upload_file', 'download_file', 'transfer_status', 'transfer_cancel', 'transfers'}
    assert 'localPath' in described['contracts']['upload_file']
    source, remote = e.tmp / 'source.bin', e.tmp / 'node' / 'target.bin'
    content = bytes(range(256)) * (5 * 4096 + 7)
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    uploaded = await e.transfer_done(await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(remote), mkdirs=True))
    assert uploaded['state'] == 'done' and uploaded['verified'] and uploaded['sha256'] == digest
    assert uploaded['bytes'] == uploaded['total'] == len(content) and remote.read_bytes() == content
    denied = await e.transfer_done(await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(remote)))
    assert denied['state'] == 'failed' and denied['error'] == 'file_exists'
    assert remote.read_bytes() == content
    source.write_bytes(b'replacement\0')
    replaced = await e.transfer_done(await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(remote), overwrite=True))
    assert replaced['state'] == 'done' and remote.read_bytes() == source.read_bytes()
    # A local receipt is linkable, but neither binary payload nor Base64 enters the tool result.
    e.servers.host.workspace_dir = e.tmp / 'workspace'
    dest = e.tmp / 'workspace' / 'artifacts' / 'downloads' / 'test.bin'
    downloaded = await e.transfer_done(await e.transfer_tool('download_file', remotePath=str(remote), localPath=str(dest), mkdirs=True))
    assert downloaded['state'] == 'done' and downloaded['verified']
    assert dest.read_bytes() == source.read_bytes() and downloaded['artifact'] == 'workspace/artifacts/downloads/test.bin'
    assert not {'data', 'content', 'contentBase64'} & downloaded.keys()
    refused = await e.transfer_tool('download_file', remotePath=str(remote), localPath=str(dest))
    assert refused['error'] == 'file_exists'
    dest.chmod(0o640)
    remote.write_bytes(content)
    downloaded = await e.transfer_done(await e.transfer_tool('download_file', remotePath=str(remote), localPath=str(dest), overwrite=True))
    assert downloaded['state'] == 'done' and dest.read_bytes() == content and dest.stat().st_mode & 0o777 == 0o640
    source.write_bytes(b'')
    assert (await e.transfer_done(await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(remote), overwrite=True)))['state'] == 'done'
    assert (await e.transfer_done(await e.transfer_tool('download_file', remotePath=str(remote), localPath=str(dest), overwrite=True)))['state'] == 'done'
    assert dest.read_bytes() == b'' and not list(e.tmp.rglob('*.obupload-*')) and not list(e.tmp.rglob('*.obtransfer-*'))


@pytest.mark.parametrize('failure', ['digest', 'cancel', 'lost_commit', 'source_changed', 'cleanup_lost'])
async def test_upload_failure_cancel_and_unknown_commit(transfer_env, monkeypatch, failure):
    e = transfer_env
    source, target = e.tmp / 'source.dat', e.tmp / 'target.dat'
    source.write_bytes(b'x' * (2 * 1024 * 1024 + 3))
    target.write_bytes(b'keep')
    original = e.servers._call_newer
    entered, resume = asyncio.Event(), asyncio.Event()

    async def call(owner, sid, feature, method, params, **kw):
        if method == 'fs.cancel' and failure == 'cleanup_lost':
            raise InstanceError('node_offline', 409)
        if method == 'fs.transfer':
            params = dict(params)
            if params.get('done') and failure == 'digest':
                params['sha256'] = '0' * 64
            result = await original(owner, sid, feature, method, params, **kw)
            if params.get('done') and failure == 'lost_commit':
                error = InstanceError('node_connection_lost', 502)
                error.effect_state = 'unknown'
                raise error
            if not params.get('done') and params['offset'] == 0:
                if failure in {'cancel', 'cleanup_lost'}:
                    entered.set()
                    await resume.wait()
                if failure == 'source_changed':
                    source.write_bytes(b'changed')
            return result
        return await original(owner, sid, feature, method, params, **kw)

    monkeypatch.setattr(e.servers, '_call_newer', call)
    started = await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(target), overwrite=True, waitSeconds=0)
    if failure in {'cancel', 'cleanup_lost'}:
        await asyncio.wait_for(entered.wait(), 10)
        cancelled = await e.transfer_tool('transfer_cancel', transferId=started['transfer']['id'])
        assert cancelled['cancelRequested'] and cancelled['transfer']['state'] == 'cancelling'
        resume.set()
    result = await e.transfer_done(started)
    if failure == 'lost_commit':
        assert result['state'] == 'unknown' and result['effectState'] == 'unknown' and not result['verified']
        assert target.read_bytes() == source.read_bytes()
        outcome = await e.transfer_registry.dispatch_outcome('Servers', json.dumps({'action': 'transfer_status', 'params': {
            'serverId': e.transfer_sid, 'transferId': started['transfer']['id']}}), context=e.transfer_context)
        assert outcome.status == 'unknown' and outcome.effect_state == 'unknown'
    else:
        assert result['state'] == ('cancelled' if failure in {'cancel', 'cleanup_lost'} else 'failed')
        assert target.read_bytes() == b'keep' and not result['verified']
    assert result['cleanupPending'] == (failure == 'cleanup_lost')
    if failure == 'cleanup_lost':
        await original(123, e.transfer_sid, 'fs.transfer', 'fs.cancel', {'path': str(target), 'upload': started['transfer']['id']})
    assert not list(e.tmp.glob('*.obupload-*'))


@pytest.mark.parametrize('failure', ['corrupt', 'race', 'cancel', 'read_lost'])
async def test_download_failure_preserves_target_and_cleans_temporary(transfer_env, monkeypatch, failure):
    e = transfer_env
    source, target = e.tmp / 'remote.bin', e.tmp / 'local.bin'
    source.write_bytes(b'r' * (2 * 1024 * 1024 + 5))
    original = e.servers.read_file
    entered, resume = asyncio.Event(), asyncio.Event()

    async def read(owner, sid, path, offset=0, limit=1024 * 1024):
        part = await original(owner, sid, path, offset, limit)
        if offset == 0:
            if failure == 'corrupt':
                part['data'] = b'z' + part['data'][1:]
            if failure == 'race':
                target.write_bytes(b'racing target')
            if failure == 'read_lost':
                error = InstanceError('node_connection_lost', 502)
                error.effect_state = 'unknown'
                raise error
            if failure == 'cancel':
                entered.set()
                await resume.wait()
        return part

    monkeypatch.setattr(e.servers, 'read_file', read)
    started = await e.transfer_tool('download_file', remotePath=str(source), localPath=str(target), waitSeconds=0)
    if failure == 'cancel':
        await asyncio.wait_for(entered.wait(), 10)
        await e.transfer_tool('transfer_cancel', transferId=started['transfer']['id'])
        resume.set()
    result = await e.transfer_done(started)
    assert result['state'] == ('cancelled' if failure == 'cancel' else 'failed')
    assert result['effectState'] == 'not_committed' and not result['verified']
    assert not list(e.tmp.glob('*.obtransfer-*'))
    if failure == 'race':
        assert target.read_bytes() == b'racing target'
    else:
        assert not target.exists()


async def test_transfer_gates_owner_paths_old_node_and_commit_cancellation(transfer_env, monkeypatch):
    e = transfer_env
    source, dest = e.tmp / 'source', e.tmp / 'dest'
    source.write_bytes(b'abc')
    assert (await e.transfer_tool('upload_file', localPath='relative', remotePath=str(dest)))['error'] == 'absolute_local_path_required'
    assert (await e.transfer_tool('upload_file', localPath=str(source), remotePath='relative'))['error'] == 'absolute_remote_path_required'
    node = e.servers.hub.sessions[e.transfer_sid]
    features = node.features
    node.features = frozenset()
    assert (await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(dest)))['error'] == 'node_update_required'
    node.features = features
    original = e.servers._call_newer
    entered, resume = asyncio.Event(), asyncio.Event()

    async def call(owner, sid, feature, method, params, **kw):
        if method == 'fs.transfer' and params.get('done'):
            entered.set()
            await resume.wait()
        return await original(owner, sid, feature, method, params, **kw)

    monkeypatch.setattr(e.servers, '_call_newer', call)
    started = await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(dest), waitSeconds=0)
    await asyncio.wait_for(entered.wait(), 10)
    key = started['transfer']['id']
    with pytest.raises(InstanceError, match='transfer_not_found'):
        await e.servers.transfers.status(999, e.transfer_sid, key)
    with pytest.raises(InstanceError, match='transfer_not_found'):
        await e.servers.transfers.status(123, 'other-server', key)
    refused = await e.transfer_tool('transfer_cancel', transferId=key)
    assert not refused['cancelRequested'] and refused['transfer']['state'] == 'committing'
    resume.set()
    assert (await e.transfer_done(started))['state'] == 'done'


async def test_transfer_progress_shutdown_and_automatic_permissions(transfer_env, monkeypatch):
    e = transfer_env
    source, target = e.tmp / 'large.dat', e.tmp / 'incomplete.dat'
    source.write_bytes(b'p' * (3 * 1024 * 1024))
    original = e.servers._call_newer
    entered, resume = asyncio.Event(), asyncio.Event()

    async def call(owner, sid, feature, method, params, **kw):
        if method == 'fs.transfer' and params.get('offset') == 1024 * 1024:
            entered.set()
            await resume.wait()
        return await original(owner, sid, feature, method, params, **kw)

    monkeypatch.setattr(e.servers, '_call_newer', call)
    started = await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(target), waitSeconds=0)
    await asyncio.wait_for(entered.wait(), 10)
    key = started['transfer']['id']
    progress = (await e.transfer_tool('transfer_status', transferId=key))['transfer']
    assert progress['state'] == 'transferring' and progress['bytes'] == 1024 * 1024
    assert progress['total'] == 3 * 1024 * 1024 and not progress['verified'] and not target.exists()
    automatic = ToolRuntimeContext(chat_id=779, conversation_uuid='conv-transfer', source='cron')
    for action in ('upload_file', 'download_file', 'transfer_cancel'):
        refused = json.loads(await e.transfer_registry.dispatch('Servers', json.dumps({'action': action, 'params': {
            'serverId': e.transfer_sid, 'localPath': str(source), 'remotePath': str(target), 'transferId': key}}), context=automatic))
        assert refused['error'] == 'automatic_mutation_forbidden'
    closing = asyncio.create_task(e.servers.transfers.close())
    await asyncio.sleep(0)
    resume.set()
    await closing
    assert (await e.transfer_done(started))['state'] == 'cancelled'
    assert not target.exists() and not list(e.tmp.glob('*.obupload-*'))


async def test_batch_transfers_over_servers_and_files_with_one_answer(transfer_env, node_binary):
    e = transfer_env
    first = e.transfer_sid
    second = (await api(e, 'POST', '/api/servers', {'name': 'transfer node/2'}))['item']['id']
    await start_node(e, node_binary, second)
    await wait_for(lambda: _ready(e, second))
    one, two = e.tmp / 'one.txt', e.tmp / 'two.txt'
    one.write_bytes(b'one')
    two.write_bytes(b'two!')
    spread = e.tmp / 'spread'
    files = [{'localPath': str(one), 'remotePath': str(spread / '{serverId}' / 'one.txt')}, {'localPath': str(two), 'remotePath': str(spread / '{serverId}' / 'two.txt')}]
    # Waits for the result by default: finished transfers come back finished.
    sent = await e.transfer_tool('upload_file', serverIds=[first, second], files=files, mkdirs=True)
    assert sent['ok'] and sent['batchId'] and sent['untrustedData'], sent
    assert sent['summary'] == {'count': 4, 'states': {'done': 4}, 'finished': 4, 'succeeded': 4, 'bytes': 14, 'totalBytes': 14, 'percent': 100.0}
    assert [(i['serverId'], i['remotePath']) for i in sent['items']] == [
        (first, str(spread / first / 'one.txt')), (second, str(spread / second / 'one.txt')),
        (first, str(spread / first / 'two.txt')), (second, str(spread / second / 'two.txt'))]
    assert all(i['transferId'] and i['startedBy']['current'] and i['startedBy']['conversationUuid'] == 'conv-transfer' for i in sent['items'])
    assert (spread / second / 'two.txt').read_bytes() == b'two!'

    # The same path from both servers lands in one local file each.
    got = e.tmp / 'got'
    assert (await e.transfer_tool('download_file', serverIds=[first, second], remotePath=str(spread / first / 'one.txt'),
                                  localPath=str(got / 'one.txt')))['error'] == 'server_placeholder_required'
    fetched = await e.transfer_tool('download_file', serverIds=[first, second], files=[
        {'remotePath': str(spread / '{serverId}' / 'one.txt'), 'localPath': str(got / '{server}-one.txt')}], mkdirs=True)
    assert fetched['summary']['succeeded'] == 2, fetched
    assert [i['localPath'] for i in fetched['items']] == [str(got / 'transfer-node-one.txt'), str(got / 'transfer_node_2-one.txt')]
    assert (got / 'transfer_node_2-one.txt').read_bytes() == b'one'
    assert (await e.transfer_tool('upload_file', files=[{'localPath': str(one), 'remotePath': '/tmp/x'}, {'localPath': str(two), 'remotePath': '/tmp/x'}]))[
        'error'] == 'duplicate_destination'

    batch = await e.transfer_tool('transfer_status', serverId=None, batchId=sent['batchId'])
    assert batch['selection'] == 'batch' and batch['summary']['succeeded'] == 4
    picked = await e.transfer_tool('transfer_status', serverId=None, transferIds=[sent['items'][0]['transferId'], 'missing'])
    assert not picked['ok'] and picked['notFound'] == ['missing'] and len(picked['items']) == 1
    nothing = await e.transfer_tool('transfer_status', serverId=None)
    assert nothing['selection'] == 'unfinished_in_this_conversation' and nothing['items'] == [] and nothing['note']
    listed = await e.transfer_tool('transfers', serverId=None, direction='download', startedBy='current')
    assert listed['count'] == 2 and {i['serverId'] for i in listed['items']} == {first, second}
    assert (await e.transfer_tool('transfers', serverId=None, serverIds=[second], state='done'))['count'] == 3
    assert (await e.transfer_tool('transfers', serverId=None, state='bogus'))['error'] == 'invalid_state'
    log = (await api(e, 'GET', f'/api/servers/{second}'))['item']['execLog']
    assert log[0]['command'].startswith('文件传输 download') and log[0]['conversationUuid'] == 'conv-transfer'


async def test_batch_beyond_the_running_limit_queues_and_cancels_together(transfer_env, monkeypatch):
    from app.servers import transfers as transfers_module
    e = transfer_env
    monkeypatch.setattr(transfers_module, 'RUNNING_MAX', 1)
    original = e.servers._call_newer
    entered, resume = asyncio.Event(), asyncio.Event()

    async def call(owner, sid, feature, method, params, **kw):
        if method == 'fs.transfer' and params.get('done'):
            entered.set()
            await resume.wait()
        return await original(owner, sid, feature, method, params, **kw)

    monkeypatch.setattr(e.servers, '_call_newer', call)
    files = []
    for n in range(3):
        (e.tmp / f'q{n}').write_bytes(b'q' * (n + 1))
        files.append({'localPath': str(e.tmp / f'q{n}'), 'remotePath': str(e.tmp / 'queued' / f'q{n}')})
    sent = await e.transfer_tool('upload_file', files=files, mkdirs=True, waitSeconds=0)
    assert sent['ok'] and [i.get('waitingForSlot', False) for i in sent['items']] == [False, True, True], sent
    assert sent['items'][2]['state'] == 'queued'
    # One alone past the limit is refused, as before.
    assert (await e.transfer_tool('upload_file', localPath=str(e.tmp / 'q0'), remotePath=str(e.tmp / 'alone')))['error'] == 'too_many_transfers'
    await asyncio.wait_for(entered.wait(), 10)
    assert len((await e.transfer_tool('transfer_status', serverId=None))['items']) == 3
    cancelled = await e.transfer_tool('transfer_cancel', serverId=None, transferIds=[sent['items'][2]['transferId']])
    assert cancelled['cancelRequested'] == 1 and cancelled['items'][0]['cancelRequested']
    resume.set()
    ended = await e.transfer_tool('transfer_status', serverId=None, batchId=sent['batchId'], waitSeconds=60)
    assert ended['summary']['states'] == {'done': 2, 'cancelled': 1} and ended['summary']['finished'] == 3, ended
    assert not (e.tmp / 'queued' / 'q2').exists() and (e.tmp / 'queued' / 'q1').read_bytes() == b'qq'


async def test_receipts_and_their_origin_survive_a_controller_restart(transfer_env):
    e = transfer_env
    transfers = e.servers.transfers
    source, remote = e.tmp / 'kept.bin', e.tmp / 'node' / 'kept.bin'
    source.write_bytes(b'kept across a restart')
    done = await e.transfer_done(await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(remote), mkdirs=True))
    assert done['state'] == 'done'
    # One still moving when the controller went away without a clean shutdown: nothing recorded its end.
    await transfers.save({**transfers.jobs[done['id']], 'id': 'cut-off', 'state': 'transferring', 'endedAtMs': None})
    # A new controller process: nothing in memory, the receipts come back from the database.
    transfers.jobs.clear()
    await transfers.load()
    kept = (await e.transfer_tool('transfer_status', transferId=done['id']))['transfer']
    assert kept['state'] == 'done' and kept['verified'] and kept['sha256'] == done['sha256']
    assert kept['startedBy']['kind'] == 'conversation' and kept['startedBy']['conversationUuid'] == 'conv-transfer'
    cut = (await e.transfer_tool('transfer_status', transferId='cut-off'))['transfer']
    assert cut['state'] == 'unknown' and cut['error'] == 'controller_restarted' and cut['effectState'] == 'unknown'
    listed = await e.transfer_tool('transfers')
    assert {i['transferId'] for i in listed['items']} == {done['id'], 'cut-off'}
    # The unknown verdict is stored too: a second restart reads the same.
    transfers.jobs.clear()
    await transfers.load()
    assert transfers.jobs['cut-off']['state'] == 'unknown' and transfers.jobs['cut-off']['endedAtMs'] == cut['endedAtMs']
