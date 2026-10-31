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
    assert set(described['contracts']) == {'upload_file', 'download_file', 'transfer_status', 'transfer_cancel'}
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
    started = await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(target), overwrite=True)
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
    started = await e.transfer_tool('download_file', remotePath=str(source), localPath=str(target))
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
    started = await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(dest))
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
    started = await e.transfer_tool('upload_file', localPath=str(source), remotePath=str(target))
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
