"""UM-01: actual facts -> checks/hints/configs across node/data-plane upgrades.

Only synthetic rows, sessions and documents. Recovery exercises the real catch-up,
upgrade and reconcile wiring with the persistence and delivery boundaries mocked.
No database, service, node or network is opened.
"""
import base64
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.servers import mesh as mesh_module
from app.servers import mesh_config, mesh_fast, mesh_forward, mesh_model as model, mesh_proxy
from app.servers.mesh import Mesh

OLD, NEW, NEXT = 'a' * 64, 'b' * 64, 'c' * 64
OWNER = 1
IDS = ('r1', 'r2', 'r3', 'm1')
LEGACY = frozenset({mesh_config.STANDBY_FEATURE, mesh_proxy.ENTRIES_FEATURE, mesh_config.MEMBERS_ROUTING_FEATURE,
                    mesh_config.PATHS_FEATURE, mesh_config.PATHS_BACKUP_FEATURE, 'mesh.routing.paths.probe'})
ADDED = frozenset({mesh_config.NO_TRANSIT_FEATURE, mesh_config.TUNING_FEATURE, mesh_fast.FAST_FEATURE, mesh_forward.FEATURE})
WARNINGS = {'mesh_no_transit_outdated', 'mesh_tuning_outdated'}
PORT_ERRORS = {'fast_member_needs_update', 'forward_member_needs_update'}


def environment(manager=NEW, plane=OLD, recorded=False, online=True):
    """Mesh with actual _facts; all external observations are immutable in-memory inputs."""
    rows, stored, sessions = {}, {}, {}
    features = LEGACY | {'mesh', 'exec.shell'} | (ADDED if manager != OLD else set())
    for i, sid in enumerate(IDS):
        info = {'platform': 'linux', 'arch': 'amd64', 'ipv4': f'203.0.113.{i + 1}'}
        sessions[sid] = SimpleNamespace(features=features, info=info, caps={'exec': True}, incompatible='', build=manager)
        stored[sid] = {'info_json': json.dumps({'info': info, 'features': sorted(features), 'build': manager, 'caps': {'exec': True}})}
        keys = {key: base64.b64encode(bytes([4 * i + j + 1]) * 32).decode()
                for j, key in enumerate(('public_key', 'control_key', 'sign_key', 'e2e_key'))}
        reads = LEGACY | (ADDED if plane != OLD else set())
        rows[sid] = {'state': '', 'data_build': plane, 'confirmed_revision': 4, 'confirmed_hash': '', 'error': '',
                     'data_version': '1.3.0' if plane == OLD else '1.3.1', 'upgrade_failed_build': '',
                     'data_features': json.dumps(sorted(reads & mesh_module.DATA_PLANE_FEATURES)) if recorded else '',
                     **keys, 'platform': 'linux', 'local_json': '[]', 'config_schemas': '[2,2]',
                     'addresses_json': '[]', 'routes_json': '[]', 'tls_json': ''}
    # hub/db are read-only Mesh properties backed by the service. Use the real constructor
    # so caches, upgrade state and locks also match production; it performs no I/O.
    service = SimpleNamespace(registry=SimpleNamespace(all=AsyncMock(return_value=[{'id': sid, 'name': sid.upper()} for sid in IDS])),
                              _profiles=AsyncMock(return_value={}), _nodes=AsyncMock(return_value=stored),
                              hub=SimpleNamespace(sessions=sessions if online else {}), db=object(), metrics=object())
    mesh = Mesh(service)
    mesh._rows = AsyncMock(return_value=rows)
    mesh._instance_ports = AsyncMock(return_value={})
    return mesh, rows


def intent():
    d = model.empty_doc()
    for sid in IDS:
        m = model.new_member(sid, d['settings'])
        if sid.startswith('r'):
            m['role'] = 'relay'
        else:
            m['entry']['candidates'] = [{'id': sid, 'relay': sid, 'transport': 'udp'} for sid in ('r1', 'r3')]
        d['members'].append(m)
    d['members'][1]['noTransit'] = True
    d['settings']['routing']['switchMs'] = 50
    d['paths'] = [{'id': f'path-{r}', 'on': True, 'from': 'm1', 'to': 'r2', 'via': [r]} for r in ('r1', 'r3')]
    # An existing, unrelated user grant must survive both capability filtering and recovery.
    d['rules'] = [{'id': 'keep-access', 'src': {'all': False, 'allSubnets': False, 'members': ['r1'], 'tags': [], 'prefixes': []},
                   'dst': {'all': False, 'allSubnets': False, 'members': ['r2'], 'tags': [], 'subnets': []},
                   'proto': 'udp', 'ports': [[9000, 9000]], 'note': ''}]
    return model.allocate(d)


def with_ports(d):
    d = copy.deepcopy(d)
    d['fastPorts'] = [{'id': 'p', 'serverId': 'r2', 'port': 8443, 'proto': 'tcp', 'note': '',
                      'src': {'all': False, 'allSubnets': False, 'members': ['m1'], 'tags': [], 'prefixes': []}}]
    d['forwards'] = [{'id': 'fw', 'entry': 'm1', 'target': 'r2', 'listen': 443, 'port': 9443, 'host': '127.0.0.1',
                      'proto': 'tcp', 'line': 'fast', 'enabled': True, 'proxyProtocol': 0, 'note': '',
                      'allow': {'prefixes': ['198.51.100.0/24'], 'provinces': []}}]
    mesh_fast.ensure_secrets(d)
    return d


def warning_map(d, facts):
    return {h['error']: h['message'] for h in model.hints(d, facts) if h['error'] in WARNINGS}


def assert_routes_kept(configs):
    assert configs['m1']['self']['leaf'] is True and configs['m1']['entry'] is None
    assert {p['id'] for p in configs['m1']['paths']} == {'path-r1', 'path-r3'}
    assert {p['id'] for p in configs['r2']['paths']} == {'path-r1~back', 'path-r3~back'}
    for sid in IDS:
        assert configs[sid]['routing']['noTransit'] == ['r2']
        assert configs[sid]['routing']['tuning']['switchMs'] == 50
    for sid in ('r1', 'r3'):
        assert any(link['peer'] == 'm1' and link['kind'] == 'member' for link in configs[sid]['links'])


def assert_service_acl(d, baseline, configs, *, fast_members=(), forward=False):
    """Exact fixture grants, not production rule helpers: no omitted or additional ACLs.

    fastPorts explicitly grants m1 -> r2 TCP/8443 even over ordinary mesh, independent
    of fast capability. Only supported relay listeners get internal TCP/52003 grants;
    only a running forward adds m1 -> r2 TCP/51083. User UDP/9000 stays unchanged.
    """
    addresses = {m['serverId']: m['address'] + '/32' for m in d['members']}

    def rule(sources, target, port, proto='tcp'):
        return {'src': [addresses[s] for s in sources], 'dst': [addresses[target]], 'proto': proto, 'ports': [[port, port]]}

    assert {sid: c['rules'] for sid, c in baseline.items()} == {
        'r1': [], 'r2': [rule(['r1'], 'r2', 9000, 'udp')], 'r3': [], 'm1': []}
    extra = {sid: [] for sid in IDS}
    extra['r2'].append(rule(['m1'], 'r2', 8443))
    if forward:
        extra['r2'].append(rule(['m1'], 'r2', 51083))
    for sid in ('r1', 'r2', 'r3'):
        if sid in fast_members:
            extra[sid].append(rule([other for other in IDS if other != sid and other in fast_members], sid, 52003))
    assert {sid: c['rules'] for sid, c in configs.items()} == {
        sid: extra[sid] + baseline[sid]['rules'] for sid in IDS}


@pytest.mark.parametrize('manager,plane,recorded,supported', [
    pytest.param(OLD, OLD, True, False, id='old-node-and-mesh'),
    pytest.param(NEW, OLD, True, False, id='new-node-old-mesh-known'),
    pytest.param(NEW, OLD, False, False, id='new-node-old-mesh-unknown'),
    pytest.param(NEW, NEW, True, True, id='all-new'),
    pytest.param(NEW, NEW, False, True, id='same-build-confirmed-not-yet-recorded'),
    pytest.param(NEXT, NEW, True, True, id='later-node-update-keeps-confirmed-capabilities'),
])
@pytest.mark.parametrize('online', [True, False], ids=['online', 'offline-cached-node'])
async def test_upgrade_capability_matrix_uses_running_plane_without_erasing_intent(manager, plane, recorded, supported, online):
    mesh, rows = environment(manager, plane, recorded, online)
    original_rows = copy.deepcopy(rows)
    d = intent()
    original = copy.deepcopy(d)
    facts = await mesh._facts(OWNER)
    for f in facts.values():
        assert LEGACY <= f['features']  # Members, fixed paths and backups must not disappear.
        assert (ADDED <= f['features']) if supported else not (ADDED & f['features'])
    assert model.validate(d, facts, applying=True) == []  # Warnings, not deletion or forced rerouting.
    warnings = warning_map(d, facts)
    assert set(warnings) == (set() if supported else WARNINGS)
    for text in warnings.values():
        assert '组网数据面尚未确认支持' in text and '组网服务升级状态' in text
        assert '设置会保留' in text and '确认支持后自动生效' in text
        assert '的节点版本还不' not in text  # The management node may already be up to date.
    configs = mesh_config.build(d, facts, 'net', 4)
    assert_routes_kept(configs)
    assert all('fast' not in c and 'forward' not in c for c in configs.values())

    services = with_ports(d)
    original_services = copy.deepcopy(services)
    errors = {e['error'] for e in model.validate(services, facts, applying=True)}
    wired = mesh_config.build(services, facts, 'net', 4)
    assert_routes_kept(wired)
    if supported:
        assert not errors
        assert wired['m1']['forward']['forwards'][0]['allow'] == ['198.51.100.0/24']
        assert wired['r2']['forward']['targets'][0]['from'] == d['members'][-1]['address']
        target = next(t for t in wired['m1']['fast']['targets'] if t['port'] == 8443)
        assert target['src'] == [d['members'][-1]['address'] + '/32']
    else:
        assert errors == PORT_ERRORS
        assert all('fast' not in c and 'forward' not in c for c in wired.values())
    assert_service_acl(d, configs, wired, fast_members=IDS if supported else (), forward=supported)
    assert d == original and services == original_services and rows == original_rows


@pytest.mark.parametrize('discovery', ['upgrade', 'status'])
async def test_confirmed_plane_automatically_reconciles_preserved_intent(monkeypatch, discovery):
    """Only m1 lags; real _upgrade or _catch_up -> _store_data_build -> reconcile rebuilds it.

    Persistence is a dict updated at the SQL boundary; no real DB/transaction is used.
    _issue/_roll are delivery boundaries, not mocked versions of the capability logic.
    """
    mesh, rows = environment(NEW, NEW, True)
    rows['m1'].update(data_build=OLD, data_version='1.3.0', data_features='')
    d = with_ports(intent())
    original = copy.deepcopy(d)
    before_facts = await mesh._facts(OWNER)
    before = mesh_config.build(d, before_facts, 'net', 4)
    assert set(warning_map(d, before_facts)) == WARNINGS
    # r2 hosts the explicit fastPort and is already capable. Only its forward's entry m1
    # is still old; merely naming m1 as a fastPort source does not require fast capability.
    errors = model.validate(d, before_facts, applying=True)
    assert [(e['path'], e['error']) for e in errors] == [('forwards[0]', 'forward_member_needs_update')]
    assert 'M1' in errors[0]['message']
    assert ADDED <= before_facts['r2']['features'] and not (ADDED & before_facts['m1']['features'])
    assert_routes_kept(before)
    assert all('forward' not in c for c in before.values()) and 'fast' not in before['m1']
    baseline = mesh_config.build(intent(), before_facts, 'net', 4)
    assert_service_acl(d, baseline, before, fast_members=('r1', 'r2', 'r3'))
    for sid in rows:
        rows[sid]['confirmed_hash'] = mesh_config.config_hash(before[sid])

    scheduled = []
    mesh.service.spawn = scheduled.append  # Run the real scheduled reconcile only after its caller returns.
    mesh._load = AsyncMock(return_value=d)
    mesh._network = AsyncMock(return_value={'network_id': 'net', 'applied_revision': 4})
    mesh._latest = AsyncMock(return_value=before)
    mesh._found = AsyncMock()
    mesh._store_security_keys = AsyncMock(return_value=False)
    mesh._key_conflict = AsyncMock()
    mesh._send = AsyncMock(side_effect=AssertionError('unchanged confirmed configs must not be resent'))
    monkeypatch.setattr(mesh_module, 'MEMBER_UPGRADE_SETTLE', 0)
    monkeypatch.setattr(mesh_module.mesh_change, 'load', AsyncMock(return_value=None))
    monkeypatch.setattr(mesh_module, 'one', AsyncMock(side_effect=lambda conn, sql, args: rows[args[0]]))

    async def execute(sql, values):
        row = rows[values[-1]]
        if sql.startswith('UPDATE mesh_nodes SET data_build=?,data_features=?'):
            build, features, _, version, _ = values
            row.update(data_build=build, data_features=features, data_version=version)
        elif sql.startswith('UPDATE mesh_nodes SET data_features=CASE'):
            own, features, same, build, _ = values
            row['data_features'] = features if own else row['data_features'] if row['data_build'] == same else ''
            row['data_build'] = build
        elif sql == 'UPDATE mesh_nodes SET data_version=? WHERE server_id=?':
            row['data_version'] = values[0]
        else:
            raise AssertionError(('unexpected persistence', sql, values))
    conn = SimpleNamespace(execute=AsyncMock(side_effect=execute))

    @asynccontextmanager
    async def transaction(**kwargs):
        yield conn
    mesh.service.db = SimpleNamespace(conn=conn, write_transaction=transaction)
    if discovery == 'upgrade':
        mesh._call = AsyncMock(return_value={'changed': True, 'to': '1.3.1'})
    else:
        mesh._call = AsyncMock(return_value={'installed': True, 'dataBuild': NEW, 'dataVersion': '1.3.1'})

    async def issue(owner, doc, facts, *, reason):
        assert owner == OWNER and doc == original and reason == 'facts'
        return {'configs': mesh_config.build(doc, facts, 'net', 5)}
    mesh._issue = AsyncMock(side_effect=issue)
    mesh._roll = AsyncMock()
    if discovery == 'upgrade':
        await mesh._upgrade(OWNER, 'm1')
        assert mesh._call.await_args.args[2] == 'mesh.upgrade'
    else:
        await mesh._catch_up(OWNER, 'm1')
        # Status confirmed it was already upgraded: no unnecessary service restart.
        assert [c.args[2] for c in mesh._call.await_args_list] == ['mesh.status']
    assert rows['m1']['data_build'] == NEW
    assert ADDED <= set(json.loads(rows['m1']['data_features']))
    assert len(scheduled) == 1  # Recovery actually scheduled; not manually calling reconcile instead.
    await scheduled.pop()
    mesh._issue.assert_awaited_once()
    mesh._roll.assert_awaited_once()
    mesh._send.assert_not_awaited()
    after = mesh._roll.await_args.args[1]['configs']
    assert_routes_kept(after)
    assert after['m1']['forward']['forwards'][0]['allow'] == ['198.51.100.0/24']
    assert after['m1']['fast']['targets'] == [
        {'node': 'r2', 'port': port, 'proto': 'tcp', 'src': [d['members'][-1]['address'] + '/32']} for port in (8443, 51083)]
    assert after['r2']['forward']['targets'] == [
        {'from': d['members'][-1]['address'], 'host': '127.0.0.1', 'port': 9443, 'proto': 'tcp', 'fast': True}]
    assert_service_acl(d, baseline, after, fast_members=IDS, forward=True)
    facts = await mesh._facts(OWNER)
    assert not warning_map(d, facts) and not model.validate(d, facts, applying=True)
    assert not mesh.reconcile_waiting and not mesh.upgrades and not mesh.ready_waits
    assert d == original  # Including user tuning, noTransit, explicit ports, keys and source grants.
