"""Page/service wiring, read permissions and bounded tool reports; no real nodes or network."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.instances.contracts import InstanceError
from app.servers.browser import NodeBrowsers
from app.servers.service import ServerService
from app.tools import servers as tools
from app.tools.base import ToolRuntimeContext


@pytest.fixture
def wired(monkeypatch):
    service = SimpleNamespace(db=SimpleNamespace(conn=object()))
    tool = tools.ServersTool(service)
    tool.owner = AsyncMock(return_value=123)
    tool.server_name = AsyncMock(return_value='fake host')
    tool.origin = AsyncMock(return_value={'conversationUuid': 'conv', 'turnUuid': 'turn'})
    allowed = AsyncMock(return_value=False)
    monkeypatch.setattr(tools, 'automatic_changes_allowed', allowed)
    return service, tool, allowed, ToolRuntimeContext(source='cron')


CASES = [
    ('processes', 'processes', {'sort': 'mem', 'limit': 3}, ('mem',),
     {'ok': True, 'processes': [{'pid': i, 'name': 'proc'} for i in range(10)], 'count': 100}),
    ('host_details', 'host_details', {}, (), {'ok': True, 'details': {'os': 'fake', 'mounts': list(range(400))}}),
    ('addresses', 'net.addresses', {}, (), {'ok': True, 'interfaces': [{'name': str(i), 'addrs': []} for i in range(400)]}),
    ('ip_intel', 'intel.report', {}, (), {'ok': True, 'ip': '192.0.2.1', 'network': {'note': 'x' * 10000}}),
    ('ip_ports', 'intel.report', {}, (), {'ok': True, 'ip': '192.0.2.1', 'ports': {'open': list(range(400))}}),
    ('bgp', 'bgp.view', {'family': 6}, (6,), {'ok': True, 'nodes': list(range(400)), 'edges': []}),
    ('traffic_history', 'traffic_stats', {'unit': 'hour', 'limit': 3, 'start': 2, 'end': 7}, ('hour',),
     {'ok': True, 'unit': 'hour', 'points': [{'t': i, 'rx': i * 2, 'tx': i * 3} for i in range(10)]}),
    ('disk_history', 'disks', {'range': '30d', 'limit': 3}, ('30d',),
     {'ok': True, 'range': '30d', 'step': 100, 'partitions': [{'path': '/'}], 't': list(range(100)), 'series': {'/': list(range(100))}}),
    ('route_result', 'get', {}, (), {'node': {'route': {'results': list(range(400))}}}),
    ('unlock_result', 'get', {}, (), {'node': {'unlock': {'services': list(range(400))}}}),
]


@pytest.mark.parametrize('action,path,params,extra,report', CASES, ids=[c[0] for c in CASES])
async def test_reads_use_the_page_service_without_mutation_permission(wired, action, path, params, extra, report):
    service, tool, allowed, ctx = wired
    target = service
    parts = path.split('.')
    for part in parts[:-1]:
        setattr(target, part, SimpleNamespace())
        target = getattr(target, part)
    call = AsyncMock(return_value=copy.deepcopy(report))
    setattr(target, parts[-1], call)
    service.unlock_full = SimpleNamespace(view=AsyncMock(return_value={'ok': True, 'full': {'report': 'x' * 10000}}))
    result = await tool.dispatch(action, {'serverId': 'srv', **params}, ctx)
    call.assert_awaited_once_with(123, 'srv', *extra)
    allowed.assert_not_awaited()
    assert action not in tools.MUTATING and action in tools.NEEDS_SERVER
    assert 'untrustedData' in result and len(json.dumps(result, ensure_ascii=False)) < tools.OUTPUT_BUDGET + 300
    assert result['outputLimit']['maxChars'] == tools.OUTPUT_BUDGET
    assert call.return_value == report, 'limiting must not mutate a shared service/cache result'
    if action == 'processes':
        assert len(result['processes']) == result['returned'] == 3 and result['truncated']
    elif action == 'traffic_history':
        assert [p['t'] for p in result['points']] == [5, 6, 7]
        assert result['totalPoints'] == 6 and result['truncated']
    elif action == 'disk_history':
        assert result['t'] == result['series']['/'] == [0, 49, 99] and result['sampled']
    else:
        assert result['outputLimit']['truncated']
    if action == 'unlock_result':
        service.unlock_full.view.assert_awaited_once_with(123, 'srv')


@pytest.mark.parametrize('action', tools.READ_ACTIONS)
async def test_reads_still_require_an_authenticated_owner_and_server(wired, action):
    _, tool, _, ctx = wired
    with pytest.raises(InstanceError, match='server_id_required'):
        await tool.act(action, {}, ctx)
    tool.owner.side_effect = InstanceError('owner_required', 403)
    with pytest.raises(InstanceError, match='owner_required'):
        await tool.act(action, {'serverId': 'srv'}, ctx)


@pytest.mark.parametrize('action,params,error', [
    ('processes', {'limit': 11}, 'invalid_limit'), ('processes', {'limit': True}, 'invalid_limit'),
    ('disk_history', {'limit': 0}, 'invalid_limit'), ('traffic_history', {'limit': 201}, 'invalid_limit'),
    ('traffic_history', {'start': 9, 'end': 3}, 'invalid_range'), ('traffic_history', {'start': True}, 'invalid_range'),
    ('bgp', {'family': '6'}, 'invalid_family'), ('bgp', {'family': True}, 'invalid_family'),
])
async def test_invalid_read_parameters_never_reach_services(wired, action, params, error):
    _, tool, _, ctx = wired
    with pytest.raises(InstanceError, match=error):
        await tool.act(action, {'serverId': 'srv', **params}, ctx)


def test_nested_reports_have_a_total_budget_and_do_not_modify_inputs():
    report = {'ok': True, 'details': {'rows': [{'command': '文\n' * 10000, 'nested': list(range(500))} for _ in range(500)]}}
    before = copy.deepcopy(report)
    out = tools._bounded_read(report)
    assert len(json.dumps(out, ensure_ascii=False)) <= tools.OUTPUT_BUDGET
    assert out['outputLimit']['truncated'] and report == before


async def test_disk_history_reduces_whole_samples_and_partitions(wired):
    service, tool, _, ctx = wired
    service.disks = AsyncMock(return_value={'ok': True, 't': list(range(400)), 'step': 7200,
        'partitions': [{'path': f'/disk{i}'} for i in range(30)],
        'series': {f'/disk{i}': [12.3456789] * 400 for i in range(30)}})
    result = await tool.act('disk_history', {'serverId': 'srv', 'limit': 200}, ctx)
    assert result['sampled'] and result['truncated'] and result['totalPartitions'] == 30
    assert result['t'][0] == 0 and result['t'][-1] == 399
    assert len(result['partitions']) == len(result['series']) <= 20
    assert all(len(v) == len(result['t']) for v in result['series'].values())
    assert not result['outputLimit']['truncated']
    assert len(json.dumps(result, ensure_ascii=False)) <= tools.OUTPUT_BUDGET


async def test_browser_check_is_read_only_but_update_needs_permission(wired):
    service, tool, allowed, ctx = wired
    service.update_browser = AsyncMock(return_value={'ok': True, 'update': {'current': '130', 'latest': '131', 'upToDate': False, 'started': False}})
    checked = await tool.act('update_browser', {'serverId': 'srv', 'check': True}, ctx)
    assert checked['update']['started'] is False
    allowed.assert_not_awaited()
    service.update_browser.assert_awaited_once_with(123, 'srv', {'check': True, 'restart': False}, source='ai', origin=tool.origin.return_value)
    for params in ({}, {'restart': True}, {'check': 'true'}, {'check': 1}):
        with pytest.raises(InstanceError, match='automatic_mutation_forbidden'):
            await tool.act('update_browser', {'serverId': 'srv', **params}, ctx)
    assert service.update_browser.await_count == 1
    allowed.return_value = True
    for params in ({'check': 'true'}, {'restart': 1}):
        with pytest.raises(InstanceError, match='invalid_browser_update'):
            await tool.act('update_browser', {'serverId': 'srv', **params}, ctx)
    service.update_browser.return_value['update']['started'] = True
    result = await tool.act('update_browser', {'serverId': 'srv', 'restart': True}, ctx)
    assert result['update']['started']
    service.update_browser.assert_awaited_with(123, 'srv', {'check': False, 'restart': True}, source='ai', origin=tool.origin.return_value)


async def test_browser_update_keeps_the_service_old_node_gate(wired):
    service, tool, _, ctx = wired
    service.registry = SimpleNamespace(get=AsyncMock())
    service.hub = SimpleNamespace(sessions={'srv': SimpleNamespace(caps={'exec': True}, features={'host'})})
    service._node_call = AsyncMock()
    service._call_newer = ServerService._call_newer.__get__(service)
    service.browsers = NodeBrowsers(service)
    service.update_browser = ServerService.update_browser.__get__(service)
    with pytest.raises(InstanceError, match='node_update_required'):
        await tool.dispatch('update_browser', {'serverId': 'srv', 'check': True}, ctx)
    service._node_call.assert_not_awaited()
    service.hub.sessions['srv'].features.add('browser.update')
    service._node_call.return_value = {'current': '130', 'latest': '131', 'started': False, 'upToDate': False}
    result = await tool.dispatch('update_browser', {'serverId': 'srv', 'check': True}, ctx)
    assert result['update']['latest'] == '131'
    service._node_call.assert_awaited_once_with(123, 'srv', 'browser.update', {'check': True, 'restart': False}, timeout=90)


def test_schema_describe_groups_and_retention_are_aligned():
    class Registry:
        def add(self, name, description, schema, handler, **kwargs):
            self.description, self.schema = description, schema
    registry = Registry()
    tools.register_server_tools(registry, object())
    assert registry.schema['properties']['action']['enum'] == tools.ACTIONS
    assert len(tools.ACTIONS) == len(set(tools.ACTIONS))
    for action in (*tools.READ_ACTIONS, 'update_browser'):
        assert action in tools.SUMMARIES and action in tools.describe(action)['contracts']
        assert action in registry.schema['properties']['action']['enum']
    assert tools.describe('diagnostics')['actions'] == list(tools.READ_ACTIONS)
    assert 'update_browser' in tools.describe('browser_all')['actions']
    assert 'everything the Servers page does' not in registry.description
    assert 'retention_hours_too_short' in tools.describe('save_settings')['contracts']['save_settings']['retention']
