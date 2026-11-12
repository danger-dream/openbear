"""Webhook management contracts and argument routing, without production state."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.webhook import ACTIONS, register_webhook_tool
from app.webhooks.contracts import EndpointConfig, Scope, parse_config
from app.webhooks.repository import one
from tests.test_webhooks_backend import env


@pytest.fixture
def tool(monkeypatch):
    monkeypatch.setattr('app.tools.webhook.one', AsyncMock(return_value=None))
    service = SimpleNamespace(db=SimpleNamespace(conn=None),
        create=AsyncMock(return_value={'endpoint': {'id': 'fixture'}, 'credential': {'key': 'hidden'}}),
        update=AsyncMock(return_value={'endpoint': {'id': 'fixture'}}),
        prepare=AsyncMock(), control=AsyncMock())
    registry = ToolRegistry()
    register_webhook_tool(registry, service)
    ctx = ToolRuntimeContext(webhook_owner_id=123, conversation_uuid='fixture')
    return registry, service, ctx


async def invoke(registry, ctx, action, params=None, **extra):
    return await registry.dispatch_outcome('Webhook', json.dumps({'action': action, 'params': params or {}, **extra}), context=ctx)


@pytest.mark.parametrize('action', ['create', 'update'])
async def test_describe_exposes_live_wire_schemas_and_action_contracts(tool, action):
    registry, service, ctx = tool
    result = await invoke(registry, ctx, 'describe', {'action': action})
    data = json.loads(result.content)
    # The outline by default: every field by path, with its type, default and the rule its description starts with.
    assert 'configSchema' not in data and 'scopeSchema' not in data and len(result.content) < 12000
    assert 'target.mode: newConversation|fixedConversation = newConversation' in data['configFields']
    assert any(line.startswith('processing.allowServerChanges: boolean = False \u2014 Default false') for line in data['configFields'])
    assert any(line.startswith('statistics.dimensions[].name: string') for line in data['configFields'])
    assert data['scopeFields'] == ['type: folder|conversation', 'id: string']
    full = json.loads((await invoke(registry, ctx, 'describe', {'action': action, 'schema': True})).content)
    assert full['configSchema'] == EndpointConfig.model_json_schema(by_alias=True)
    assert full['scopeSchema'] == Scope.model_json_schema(by_alias=True)
    assert 'configFields' not in full and full['contracts'] == data['contracts']
    assert set(data['contracts']) == {action}
    for name, contract in data['contracts'].items():
        example = contract['example']
        assert example['action'] == name and set(example) == {'action', 'params'}
        assert set(contract['required']) <= example['params'].keys()
        EndpointConfig.model_validate(example['params']['config'])
    assert 'pre-execution script' in data['guidance'] and 'register a scoped wait' not in data['guidance']
    service.create.assert_not_awaited()
    service.update.assert_not_awaited()


async def test_describe_index_names_every_action_and_one_action_gives_only_its_part(tool):
    registry, _, ctx = tool
    index = json.loads((await invoke(registry, ctx, 'describe')).content)
    assert set(index['actions']) == set(ACTIONS) and 'configSchema' not in index and 'guidance' not in index
    wait = json.loads((await invoke(registry, ctx, 'describe', {'action': 'wait'})).content)
    assert 'configSchema' not in wait and wait['contract']['op'] == 'register | await | cancel'
    assert 'register a scoped wait' in wait['guidance'] and 'pre-execution script' not in wait['guidance']
    assert len((await invoke(registry, ctx, 'describe', {'action': 'wait'})).content) < 3000
    for action in ACTIONS:
        assert json.loads((await invoke(registry, ctx, 'describe', {'action': action})).content)['action'] == action
    unknown = await invoke(registry, ctx, 'describe', {'action': 'nope'})
    assert unknown.status == 'failed' and json.loads(unknown.content)['details']['actions'] == list(ACTIONS)


async def test_schema_remains_small(tool):
    registry, _, _ = tool
    schema = registry.schemas()[0]['parameters']
    assert set(schema['properties']) == {'action', 'params', '_openbearApprovalId'}
    assert 'beside config' in schema['properties']['params']['description']


@pytest.mark.parametrize('action,params', [
    ('create', {'requestId': 'create', 'scope': {'type': 'folder', 'id': 'folder'},
        'name': '中文名称', 'description': '说明', 'enabled': False,
        'config': {'processing': {'instructions': '指令', 'resultSchema': {'properties': {'scope': {'type': 'string'}}}},
                   'pre': {'enabled': False}, 'batching': {'idleSeconds': None}}}),
    ('update', {'endpointId': 'fixture', 'requestId': 'update', 'expectedRevision': 3,
        'expectedControlRevision': 0, 'enabled': False, 'name': '', 'description': '',
        'config': {'post': {'enabled': False}, 'target': {'mode': 'newConversation'}}}),
    ('create', {'requestId': 'create-defaults', 'scope': {'type': 'folder', 'id': 'folder'}}),
    ('update', {'endpointId': 'fixture', 'requestId': 'update-defaults', 'expectedRevision': 3}),
])
async def test_business_arguments_are_preserved(tool, action, params):
    registry, service, ctx = tool
    original = copy.deepcopy(params)
    result = await invoke(registry, ctx, action, params)
    assert result.status == 'completed'
    if action == 'create':
        service.create.assert_awaited_once_with(123, params)
        assert 'hidden' not in result.content and 'credential' not in json.loads(result.content)
    else:
        service.update.assert_awaited_once_with(123, params['endpointId'], {k: v for k, v in params.items() if k != 'endpointId'})
    assert params == original


@pytest.mark.parametrize('action,params,path', [
    ('create', {'requestId': 'create'}, 'params.scope'),
    ('create', {'scope': None, 'requestId': 'create'}, 'params.scope'),
    ('create', {'scope': {'type': 'folder', 'id': 'folder'}}, 'params.requestId'),
    ('update', {'requestId': 'update', 'expectedRevision': 1}, 'params.endpointId'),
    ('update', {'endpointId': 'fixture', 'expectedRevision': 1}, 'params.requestId'),
    ('update', {'endpointId': 'fixture', 'requestId': 'update'}, 'params.expectedRevision'),
    ('update', {'endpointId': 'fixture', 'requestId': 'update', 'expectedRevision': 1, 'enabled': False}, 'params.expectedControlRevision'),
])
async def test_missing_parameters_fail_before_service_mutation(tool, action, params, path):
    registry, service, ctx = tool
    result = await invoke(registry, ctx, action, params)
    data = json.loads(result.content)
    assert result.status == 'failed' and result.effect_state == 'not_started'
    assert data['code'] == 'missing_parameter' and data['details']['field'] == path
    assert path in data['message'] and 'describe' in data['message']
    service.create.assert_not_awaited()
    service.update.assert_not_awaited()


@pytest.mark.parametrize('action,key,value', [
    ('create', 'scope', {'type': 'folder', 'id': 'folder'}),
    ('create', 'requestId', 'create'),
    ('create', 'enabled', True),
    ('update', 'endpointId', 'fixture'),
    ('update', 'expectedRevision', 1),
    ('update', 'name', 'Name'),
])
async def test_nested_management_fields_report_correct_path_without_repair(tool, action, key, value):
    registry, service, ctx = tool
    params = {'config': {key: value, 'processing': {'instructions': 'fixture'}}}
    original = copy.deepcopy(params)
    result = await invoke(registry, ctx, action, params)
    data = json.loads(result.content)
    assert result.status == 'failed' and result.effect_state == 'not_started'
    assert data['code'] == 'invalid_parameter_location'
    assert data['details'] == {'field': f'params.config.{key}', 'expectedPath': f'params.{key}'}
    assert f'params.config.{key}' in data['message'] and f'params.{key}' in data['message']
    assert params == original
    service.create.assert_not_awaited()
    service.update.assert_not_awaited()


async def test_conflicting_scope_locations_are_not_guessed(tool):
    registry, service, ctx = tool
    result = await invoke(registry, ctx, 'create', {
        'requestId': 'conflict', 'scope': {'type': 'folder', 'id': 'one'},
        'config': {'scope': {'type': 'folder', 'id': 'two'}}})
    assert json.loads(result.content)['code'] == 'invalid_parameter_location'
    service.create.assert_not_awaited()


@pytest.mark.parametrize('action', ['create', 'update'])
async def test_tool_root_business_fields_are_not_silently_ignored(tool, action):
    registry, service, ctx = tool
    result = await invoke(registry, ctx, action, scope={'type': 'folder', 'id': 'folder'})
    data = json.loads(result.content)
    assert result.status == 'failed' and result.effect_state == 'not_started'
    assert data['details'] == {'field': 'scope', 'expectedPath': 'params.scope'}
    service.create.assert_not_awaited()
    service.update.assert_not_awaited()


async def test_rotation_still_uses_confirmation_without_config_revision(tool):
    registry, service, ctx = tool
    result = await invoke(registry, ctx, 'update', {
        'endpointId': 'fixture', 'requestId': 'rotate', 'operation': 'rotate_key', 'graceSeconds': 0})
    assert json.loads(result.content)['code'] == 'user_confirmation_unavailable'
    service.update.assert_not_awaited()
    service.control.assert_not_awaited()


async def test_describe_examples_create_and_update_through_registry_and_real_service(env):
    registry = env.server.tools
    ctx = ToolRuntimeContext(webhook_owner_id=123, conversation_uuid='fixture')
    contracts = {a: json.loads((await invoke(registry, ctx, 'describe', {'action': a})).content)['contracts'][a] for a in ('create', 'update')}
    params = contracts['create']['example']['params']
    params['scope']['id'] = 'folder'
    params['name'] = '中文触发器'
    params['config']['batching'] = {'enabled': False, 'idleSeconds': None}
    params['config']['pre'] = {'enabled': False, 'code': 'not executed'}
    created = await invoke(registry, ctx, 'create', params)
    assert created.status == 'completed'
    endpoint = json.loads(created.content)['endpoint']
    assert 'credential' not in json.loads(created.content)
    expected = parse_config(params['config'], Scope.model_validate(params['scope']), env.s.config, enabled=True).wire()
    assert endpoint['config'] == expected
    assert endpoint['name'] == params['name'] and endpoint['scope'] == params['scope']
    row = await one(env.db.conn, 'SELECT selection_json FROM webhook_control_operations WHERE request_id=?', (params['requestId'],))
    assert json.loads(row['selection_json']) == params

    update = contracts['update']['example']['params']
    update.update(endpointId=endpoint['id'], expectedRevision=endpoint['revision'],
        expectedControlRevision=endpoint['controlRevision'], enabled=False,
        name=endpoint['name'], description=endpoint['description'], config=endpoint['config'])
    update['config']['processing']['instructions'] = '更新后的指令'
    changed = await invoke(registry, ctx, 'update', update)
    assert changed.status == 'completed'
    saved = (await env.s.get(123, endpoint['id']))['endpoint']
    assert saved['config'] == update['config']
    assert saved['scope'] == params['scope'] and saved['name'] == params['name']
    assert saved['revision'] == 2 and saved['enabled'] is False
    row = await one(env.db.conn, 'SELECT selection_json FROM webhook_control_operations WHERE request_id=?', (update['requestId'],))
    assert json.loads(row['selection_json']) == {k: v for k, v in update.items() if k != 'endpointId'}


async def test_wrong_scope_nesting_creates_no_endpoint_or_operation(env):
    ctx = ToolRuntimeContext(webhook_owner_id=123, conversation_uuid='fixture')
    result = await invoke(env.server.tools, ctx, 'create', {'config': {
        'scope': {'type': 'folder', 'id': 'folder'}, 'requestId': 'wrong', 'enabled': True,
        'processing': {'instructions': 'must not execute'}}})
    assert result.status == 'failed' and result.effect_state == 'not_started'
    assert json.loads(result.content)['code'] == 'invalid_parameter_location'
    assert (await one(env.db.conn, 'SELECT COUNT(*) n FROM webhook_endpoints'))['n'] == 0
    assert (await one(env.db.conn, 'SELECT COUNT(*) n FROM webhook_control_operations'))['n'] == 0
