import base64
import copy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.instances.contracts import InstanceError
from app.tools import servers as tools
from app.tools.base import ToolRegistry, ToolRuntimeContext


@pytest.fixture
def caller(monkeypatch):
    item = {'id': 'created-server', 'name': '测试节点', 'profile': {}, 'ssh': {'host': '192.0.2.1'}}
    service = SimpleNamespace(db=SimpleNamespace(conn=object()), create=AsyncMock(return_value={'item': item}),
                              install=AsyncMock(), get=AsyncMock(return_value={'install': {'state': 'done'}}),
                              revoke=AsyncMock(), read_file=AsyncMock(), job_status=AsyncMock())
    monkeypatch.setattr(tools.ServersTool, 'owner', AsyncMock(return_value=123))
    monkeypatch.setattr(tools.ServersTool, 'today', AsyncMock(return_value=dt.date(2026, 11, 12)))
    monkeypatch.setattr(tools.ServersTool, 'server_name', AsyncMock(return_value='测试节点'))
    monkeypatch.setattr(tools.ServersTool, 'server_names', AsyncMock(return_value={}))
    monkeypatch.setattr(tools, 'automatic_changes_allowed', AsyncMock(return_value=True))
    registry = ToolRegistry()
    tools.register_server_tools(registry, service)
    ctx = ToolRuntimeContext(conversation_uuid='integrity-test')

    async def call(tool_action, **params):
        outcome = await registry.dispatch_outcome('Servers', json.dumps({'action': tool_action, 'params': params}), context=ctx)
        assert len(outcome.content) <= 32000
        return json.loads(outcome.content), outcome

    return service, call


@pytest.mark.parametrize('field,value', [('install', 'yes'), ('browser', 1), ('force', 'yes')])
async def test_add_validates_flags_before_creating(caller, field, value):
    service, call = caller
    result, outcome = await call('add', **{'name': 'test', 'ssh': {}, 'install': True, field: value})
    assert not result['ok'] and outcome.effect_state == 'not_started'
    service.create.assert_not_awaited()
    service.install.assert_not_awaited()


async def test_add_install_failure_keeps_created_identity(caller):
    service, call = caller
    service.install.side_effect = InstanceError('node_binary_missing', 503)
    result, outcome = await call('add', name='test', ssh={}, install=True)
    assert not result['ok'] and result['created']
    assert result['serverId'] == result['item']['id'] == 'created-server'
    assert result['install'] == {'state': 'not_started', 'error': 'node_binary_missing'}
    assert outcome.status == 'failed' and outcome.effect_state == 'reported'
    assert outcome.business_success is False
    service.create.assert_awaited_once()


async def test_add_install_status_failure_does_not_claim_not_started(caller):
    service, call = caller
    service.get.side_effect = InstanceError('node_connection_lost')
    result, outcome = await call('add', name='test', ssh={}, install=True)
    assert result['created'] and result['serverId'] == 'created-server'
    assert result['install']['state'] == 'unknown' and outcome.effect_state == 'reported'


async def test_add_finished_install_failure_is_not_success(caller):
    service, call = caller
    service.get.return_value = {'install': {'state': 'failed', 'error': 'ssh_failed'}}
    result, outcome = await call('add', name='test', ssh={}, install=True)
    assert not result['ok'] and result['created'] and result['install']['error'] == 'ssh_failed'
    assert outcome.business_success is False


@pytest.mark.parametrize('extra', [
    {'work': {'offline': True, 'stopped': [], 'failed': [], 'note': '节点离线，没能停止它上面仍在运行的任务'}},
    {'note': '仅注销，不停止或卸载原机器'},
])
async def test_remove_node_preserves_remote_work_and_registration_notes(caller, extra):
    service, call = caller
    service.revoke.return_value = {'ok': True, 'uninstalled': False, **extra}
    result, _ = await call('remove_node', serverId='srv', uninstall=True)
    assert not result['uninstalled']
    assert all(result[k] == v for k, v in extra.items())


def serve_file(service, content):
    async def read(owner, sid, path, offset, limit):
        data = content[offset:offset + limit]
        return {'data': data, 'size': len(content), 'read': len(data), 'eof': offset + len(data) >= len(content), 'mode': '0644'}
    service.read_file.side_effect = read


@pytest.mark.parametrize('content,encoding', [
    (b'A' * 39999 + b'CENTRAL_REQUIRED_LINE' + b'B' * 40000, None),
    (('文🙂\n\\\"\t' * 12000).encode(), None),
    (bytes(range(256)) * 400, 'base64'),
])
async def test_file_pages_reconstruct_every_byte_through_final_tool_budget(caller, content, encoding):
    service, call = caller
    serve_file(service, content)
    offset, delivered = 0, bytearray()
    for _ in range(100):
        result, _ = await call('read_file', serverId='srv', path='/fake/log', offset=offset, **({'encoding': encoding} if encoding else {}))
        part = base64.b64decode(result['contentBase64'], validate=True) if encoding else result['content'].encode()
        assert result['offset'] == offset and result['read'] == len(part)
        assert result['next'] == offset + len(part)
        delivered.extend(part)
        if result['eof']:
            break
        assert result['next'] > offset
        offset = result['next']
    else:
        pytest.fail('pagination failed to finish')
    assert bytes(delivered) == content


async def test_file_page_does_not_consume_incomplete_utf8_at_node_read_boundary(caller):
    service, call = caller
    content = ('A' * 199999 + '🙂尾').encode()
    serve_file(service, content)
    offset, delivered = 0, bytearray()
    while offset < len(content):
        result, _ = await call('read_file', serverId='srv', path='/fake/log', offset=offset)
        assert '\ufffd' not in result['content']
        delivered.extend(result['content'].encode())
        assert result['next'] > offset
        offset = result['next']
    assert delivered == content


async def test_tiny_text_limit_makes_progress_by_complete_codepoint(caller):
    service, call = caller
    serve_file(service, '🙂文'.encode())
    first, _ = await call('read_file', serverId='srv', path='/fake/log', limit=1)
    second, _ = await call('read_file', serverId='srv', path='/fake/log', offset=first['next'], limit=1)
    assert first['content'] + second['content'] == '🙂文'
    assert first['next'] == 4 and second['eof']


def serve_log(service, content, *, long_job=False):
    async def status(owner, sid, jid, offset=None, limit=None):
        offset = offset if offset is not None else max(0, len(content) - (limit or 8000))
        data = content[offset:offset + (limit or 8000)]
        while data:
            try:
                text = data.decode()
                break
            except UnicodeDecodeError:
                data = data[:-1]
        else:
            text = ''
        job = {'id': jid, 'state': 'running', 'command': '\\"' * 4000 if long_job else 'echo', 'cwd': '/' + 'x' * 4000 if long_job else '/', 'shell': 'sh'}
        return {'job': job, 'log': {'data': text, 'offset': offset, 'next': offset + len(data), 'size': len(content), 'first': 0}}
    service.job_status.side_effect = status


async def test_log_pages_reconstruct_utf8_and_json_escaping(caller):
    service, call = caller
    content = ('文🙂\n\\\"\t' * 15000).encode()
    serve_log(service, content, long_job=True)
    offset, delivered = 0, bytearray()
    for _ in range(100):
        result, _ = await call('job_status', serverId='srv', jobId='job123', offset=offset)
        part = result['output'].encode()
        assert result['outputOffset'] == offset
        assert result['next'] == offset + len(part)
        delivered.extend(part)
        if not result['hasMore']:
            break
        assert result['next'] > offset
        offset = result['next']
    else:
        pytest.fail('log pagination failed to finish')
    assert delivered == content


async def test_batch_log_budget_keeps_each_cursor_on_its_delivered_prefix(caller):
    service, call = caller
    content = ('日志🙂\\\"\n' * 8000).encode()
    serve_log(service, content, long_job=True)
    refs = [{'serverId': f'srv{i}', 'jobId': f'job{i:03}', 'offset': 0} for i in range(50)]
    result, _ = await call('job_status', jobs=refs, limit=100000)
    assert result['ok'] and result['count'] == 50
    for row in result['results']:
        assert row['next'] == len(row['output'].encode()) > 0
        assert content.startswith(row['output'].encode()) and row['hasMore']
        assert row['jobSummary']


@pytest.mark.parametrize('version,error', [(None, 'mesh_draft_version_required'), ('old', 'mesh_draft_conflict')])
async def test_tool_draft_patch_never_refreshes_stale_client_version(caller, version, error):
    service, call = caller
    service.mesh = SimpleNamespace(view=AsyncMock(return_value={'draftVersion': 'current'}), save_draft=AsyncMock())
    result, _ = await call('mesh_save_draft', patch={'settings': {'mtu': 1300}}, **({'draftVersion': version} if version else {}))
    assert result['error'] == error
    service.mesh.save_draft.assert_not_awaited()


async def test_tool_draft_patch_carries_original_version_to_conditional_write(caller):
    service, call = caller
    view = {'ok': True, 'draftVersion': 'current', 'settings': {'mtu': 1380}}
    service.mesh = SimpleNamespace(view=AsyncMock(return_value=view), save_draft=AsyncMock(return_value={**view, 'draftVersion': 'next'}))
    result, _ = await call('mesh_save_draft', draftVersion='current', patch={'settings': {'mtu': 1300}})
    assert result['draftVersion'] == 'next'
    service.mesh.save_draft.assert_awaited_once_with(123, {'settings': {'mtu': 1300}, 'draftVersion': 'current'})


@pytest.mark.parametrize('params', [{}, {'parts': ['rules']}, {'parts': ['members'], 'offset': 0, 'limit': 1}, {'fields': ['role']}])
async def test_tool_draft_projections_retain_version(caller, params):
    service, call = caller
    view = {'draftVersion': 'current', 'draft': {'dirty': False, 'changes': []}, 'errors': [], 'warnings': [], 'members': [], 'rules': []}
    service.mesh = SimpleNamespace(view=AsyncMock(return_value=view))
    result, _ = await call('mesh_draft', **params)
    assert result['draftVersion'] == 'current'


async def test_ssh_check_exposes_stale_result(caller):
    service, call = caller
    service.registry = SimpleNamespace(check=AsyncMock(return_value={'stale': True}))
    service.get.return_value = {'ssh': {'host': 'new.example.org', 'status': 'unknown'}}
    result, _ = await call('check_ssh', serverId='srv')
    assert result['stale'] is True and result['ssh']['host'] == 'new.example.org'
    assert '未由本次检查验证' in result['note']


async def test_traffic_history_preserves_basis_without_adding_it_twice(caller):
    service, call = caller
    report = {'unit': 'day', 'timezone': 'UTC', 'currentLabel': '2026-11-12',
              'history': [{'day': '2026-11-13', 'timezone': None, 'rx': 100, 'tx': 50}],
              'points': [{'t': 1794499200, 'label': '2026-11-13', 'rx': 100, 'tx': 50}]}
    service.traffic_stats = AsyncMock(return_value=report)
    result, _ = await call('traffic_history', serverId='srv')
    assert all(result[key] == report[key] for key in ('history', 'timezone', 'currentLabel', 'points'))


async def test_ddns_tool_preserves_zone_and_explicit_rrset_choice(caller):
    service, call = caller
    service.ddns = SimpleNamespace(create_records=AsyncMock(return_value={'ok': True}),
                                   update_record=AsyncMock(return_value={'ok': True}))
    rows = [{'name': 'home.sub.example.org', 'zone': 'example.org', 'type': 'A', 'accountId': 'account1',
             'confirmTakeover': True, 'confirmRrsetTakeover': True}]
    await call('ddns_add', source={'kind': 'fixed', 'fixed': '192.0.2.1'}, rows=rows)
    assert service.ddns.create_records.call_args.args[1]['rows'] == rows
    await call('ddns_update', recordId='record1', policy={'rrsetTakeover': True})
    service.ddns.update_record.assert_awaited_once_with(123, 'record1', {'policy': {'rrsetTakeover': True}})
    contract, _ = await call('describe', action='ddns_add')
    assert 'confirmRrsetTakeover' in json.dumps(contract)


def test_log_metadata_summary_leaves_service_results_and_node_cursors_unchanged():
    original = {'ok': True, 'job': {'id': 'job123', 'state': 'running', 'command': 'x' * 8000},
                'output': '\\"' * 50000, 'outputOffset': 7, 'next': 100007, 'outputSize': 100007}
    before = copy.deepcopy(original)
    result = tools._log_metadata(original, 1000)
    assert original == before
    assert result['jobSummary']
    assert result['output'] == original['output'] and result['next'] == original['next']
