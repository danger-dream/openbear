"""CORE-C1..C5: isolated metrics/registry databases and controlled async boundaries.
No live nodes, commands, credentials, DNS or external release service.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import aiosqlite
import httpx
import pytest

from app.instances.contracts import InstanceError
from app.instances.servers import SCHEMA as REGISTRY_SCHEMA, Servers
from app.servers import hub as hub_module, releases
from app.servers.metrics import Bucket, MetricsStore
from app.servers.service import ServerService


class Database:
    def __init__(self, conn):
        self.conn, self.lock = conn, asyncio.Lock()

    @contextlib.asynccontextmanager
    async def write_transaction(self, **_):
        async with self.lock:
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(':memory:')
    conn.row_factory = aiosqlite.Row
    await conn.executescript(REGISTRY_SCHEMA + ''';
        CREATE TABLE server_settings(id INTEGER PRIMARY KEY, settings_json TEXT);
        CREATE TABLE server_nodes(server_id TEXT, owner INTEGER);
        CREATE TABLE memory_secrets(name TEXT, kv_json TEXT, archived INTEGER);
    ''')
    yield Database(conn)
    await conn.close()


@pytest.fixture
async def store(tmp_path):
    value = MetricsStore(tmp_path / 'metrics.db')
    await value.start()
    await value.maintaining
    try:
        yield value
    finally:
        await value.close()


def stamp(text):
    return int(dt.datetime.fromisoformat(text).timestamp())


def pending(store, ts, rx, tx=0, sid='s'):
    bucket = Bucket(ts - ts % 60)
    bucket.samples, bucket.rx, bucket.tx = 1, rx, tx
    bucket.counters = (1, rx, tx)
    store.pending.append((sid, bucket))


async def write(store, rows):
    for ts, rx, tx in rows:
        pending(store, ts, rx, tx)
    await store.flush()


def settings_service(db, store):
    service = ServerService.__new__(ServerService)
    service.db, service.metrics = db, store
    return service


@pytest.mark.parametrize('zones', [
    ('UTC', 'Asia/Shanghai', 'America/Los_Angeles'),
    ('Asia/Kathmandu', 'America/St_Johns', 'UTC'),
    ('America/New_York', 'Pacific/Kiritimati', 'Asia/Kathmandu'),
])
async def test_c1_settings_rebuild_complete_hours_across_days_months_and_dst(db, store, zones):
    service = settings_service(db, store)
    await service.save_settings({'timezone': zones[0]})
    rows = [(stamp('2026-10-31T20:00:00+00:00'), 100, 11),
            (stamp('2026-11-01T01:00:00+00:00'), 200, 22),
            (stamp('2026-11-01T06:00:00+00:00'), 300, 33)]
    await write(store, rows)
    for zone in (*zones[1:], zones[0]):
        await service.save_settings({'timezone': zone})
        assert store.zone.key == zone
        expected = {}
        for ts, rx, tx in rows:
            key = store._day(ts)[0]
            value = expected.setdefault(key, [0, 0])
            value[0] += rx
            value[1] += tx
        async with store.conn.execute('SELECT day,rx,tx FROM day WHERE rx>0 ORDER BY day') as cursor:
            actual = {r['day']: [r['rx'], r['tx']] async for r in cursor}
        assert actual == expected
        stats = await store.traffic_stats('s', 'month', rows[-1][0] + 86400)
        assert sum(p['rx'] for p in stats['points']) == 600
        assert sum(p['tx'] for p in stats['points']) == 66
        assert stats['history'] == []
    # Pending minutes are assigned to their actual day, not all to the final point.
    pending(store, rows[0][0] + 60, 7, 3)
    stats = await store.traffic_stats('s', 'day', rows[-1][0] + 86400)
    assert sum(p['rx'] for p in stats['points']) == 607
    await store.flush()
    assert sum(p['rx'] for p in (await store.traffic_stats('s', 'day', rows[-1][0] + 86400))['points']) == 607


@pytest.mark.parametrize('legacy', [False, True])
async def test_c1_partial_history_keeps_basis_and_boundary_bytes_once_after_restart(store, legacy):
    a, b, c = map(stamp, ['2026-10-31T02:00:00+00:00', '2026-10-31T20:00:00+00:00', '2026-11-01T01:00:00+00:00'])
    await write(store, [(a, 100, 10), (b, 200, 20), (c, 300, 30)])
    # Only part of the first old day remains. The retained 200 must not enter
    # the rebuilt next month, but future increments in that same hour must.
    await store.conn.execute('DELETE FROM hour WHERE ts=?', (a,))
    if legacy:
        await store.conn.execute('DELETE FROM day_basis')
        await store.conn.execute("DELETE FROM meta WHERE key='day-zone'")
    await store.conn.commit()
    await store.change_timezone('Asia/Shanghai')
    stats = await store.traffic_stats('s', 'month', c + 3600)
    assert sum(p['rx'] for p in stats['points']) == 600
    assert stats['history'] == [{'day': '2026-10-31', 'timezone': None if legacy else 'UTC', 'rx': 300, 'tx': 30}]
    # Same-hour increment, flushed and pending, survives the accounted checkpoint.
    pending(store, b + 60, 9, 4)
    assert sum(p['rx'] for p in (await store.traffic_stats('s', 'month', c + 3600))['points']) == 609
    await store.flush()
    for zone in ('America/Los_Angeles', 'Asia/Kathmandu', 'UTC'):
        await store.change_timezone(zone)
        assert sum(p['rx'] for p in (await store.traffic_stats('s', 'month', c + 86400))['points']) == 609
    path = store.path
    await store.close()
    restarted = MetricsStore(path)
    restarted.zone = ZoneInfo('Asia/Shanghai')
    await restarted.start()
    await restarted.maintaining
    try:
        stats = await restarted.traffic_stats('s', 'month', c + 86400)
        assert sum(p['rx'] for p in stats['points']) == 609
        assert len(stats['history']) == 1
        await restarted.forget('s')
        assert (await restarted.traffic_stats('s', 'month', c + 86400))['history'] == []
    finally:
        await restarted.close()


async def test_c1_eastern_archived_label_remains_visible_after_westward_month_switch(store):
    ts = stamp('2026-10-31T20:00:00+00:00')
    await store.change_timezone('Asia/Shanghai')
    await write(store, [(ts, 100, 10)])
    await store.conn.execute('DELETE FROM hour')
    await store.conn.commit()
    await store.change_timezone('America/Los_Angeles')
    for unit in ('day', 'month'):
        stats = await store.traffic_stats('s', unit, ts + 60)
        assert sum(p['rx'] for p in stats['points']) == 100
        assert stats['history'][0]['timezone'] == 'Asia/Shanghai'
        assert stats['points'][-1]['label'].startswith('2026-11')
        assert stats['currentLabel'].startswith('2026-10')


async def test_c1_failed_rebuild_rolls_back_totals_and_basis(store, monkeypatch):
    ts = stamp('2026-11-10T20:00:00+00:00')
    await write(store, [(ts, 100, 10)])
    original = store._rollup_day
    monkeypatch.setattr(store, '_rollup_day', AsyncMock(side_effect=RuntimeError('injected')))
    with pytest.raises(RuntimeError, match='injected'):
        await store.change_timezone('Asia/Shanghai')
    assert store.zone.key == 'UTC'
    assert await store.meta('day-zone') == 'UTC'
    assert sum(p['rx'] for p in (await store.traffic_stats('s', 'month', ts))['points']) == 100
    monkeypatch.setattr(store, '_rollup_day', original)


@pytest.mark.parametrize('stage', ['_rollup', '_rollup_day', 'commit', 'rollback'])
@pytest.mark.parametrize('position', [1, 2])
async def test_c2_flush_snapshot_and_real_calibration_are_consistent(db, store, monkeypatch, stage, position):
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    ts = now - now % 3600
    pending(store, ts, 100, 10)
    pending(store, ts, 200, 20, 'other')
    if position == 2:
        store.pending.reverse()
    entered, release, reading = asyncio.Event(), asyncio.Event(), asyncio.Event()
    target, name = (store.conn, 'commit') if stage == 'commit' else (store, '_rollup_day' if stage == 'rollback' else stage)
    original = getattr(target, name)
    writes = 0

    async def gate(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == position:
            entered.set()
            await release.wait()
            if stage == 'rollback':
                raise RuntimeError('retry flush')
        return await original(*args, **kwargs)

    monkeypatch.setattr(target, name, gate)
    service = settings_service(db, store)
    service.instances = SimpleNamespace(servers=SimpleNamespace(get=AsyncMock(return_value={})))
    await db.conn.execute('INSERT INTO server_nodes VALUES(?,?)', ('s', 1))
    await db.conn.commit()
    profiles = {'s': {'traffic': {'type': 'down'}}}
    service._profiles = AsyncMock(side_effect=lambda _: profiles)
    service.zone = AsyncMock(return_value=ZoneInfo('UTC'))
    service.get = AsyncMock(return_value={})

    async def save_profile(_, sid, value):
        profiles[sid] = value
    service._write_profile = save_profile
    read = store.traffic_since

    async def tracked_read(*args):
        reading.set()
        return await read(*args)
    monkeypatch.setattr(store, 'traffic_since', tracked_read)
    flushing = asyncio.create_task(store.flush())
    await entered.wait()
    calibration = asyncio.create_task(service.calibrate_traffic(1, 's', {'used': 1000}))
    snapshot = asyncio.create_task(store.traffic_stats('s', 'hour', now))
    await reading.wait()
    await asyncio.sleep(0)
    assert not calibration.done() and not snapshot.done()
    release.set()
    if stage == 'rollback':
        with pytest.raises(RuntimeError, match='retry flush'):
            await flushing
    else:
        await flushing
    await calibration
    assert sum(p['rx'] for p in (await snapshot)['points']) == 100
    assert profiles['s']['trafficCalibration']['counted'] == [100, 10]
    await store.flush()
    traffic = await service._traffic('s', profiles['s'], ZoneInfo('UTC'))
    assert traffic['used'] == 1000
    assert (traffic['rx'], traffic['tx']) == (100, 10)
    assert await store.traffic_since('other', ts) == (200, 20)


class Socket:
    def __init__(self, *_, **__):
        self.closed, self.prepared = False, False

    async def prepare(self, _):
        self.prepared = True

    async def close(self, **_):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.parametrize('failure', ['previous-close', 'config', 'seen', 'cancel'])
@pytest.mark.parametrize('replaced', [False, True])
async def test_c3_every_post_registration_failure_cleans_only_its_session(monkeypatch, failure, replaced):
    service = SimpleNamespace(db=SimpleNamespace(conn=None), node_seen=AsyncMock(), spawn=asyncio.create_task)
    hub = hub_module.NodeHub(service)
    socket = Socket()
    replacement = SimpleNamespace(ws=Socket())
    calls, terminals = [], []

    async def fail(*_, **__):
        current = hub.sessions['s']
        future = asyncio.get_running_loop().create_future()
        current.calls[1] = future
        calls.append(future)
        terminal = SimpleNamespace(finish=lambda reason: terminals.append(reason))
        current.terminals[1] = terminal
        if replaced:
            hub.sessions['s'] = replacement
        if failure == 'cancel':
            raise asyncio.CancelledError
        raise ConnectionResetError('injected')

    channel = SimpleNamespace(send_json=AsyncMock())
    if failure == 'previous-close':
        hub.sessions['s'] = SimpleNamespace(ws=SimpleNamespace(close=fail))
    elif failure in ('config', 'cancel'):
        channel.send_json = fail
    else:
        async def seen(_, connected):
            if connected:
                await fail()
        service.node_seen = AsyncMock(side_effect=seen)
    monkeypatch.setattr(hub_module.web, 'WebSocketResponse', lambda **_: socket)
    monkeypatch.setattr(hub_module, 'one', AsyncMock(return_value={'server_id': 's', 'public_key': 'key', 'info_json': '{}'}))
    monkeypatch.setattr(hub, '_handshake', AsyncMock(return_value=(channel, {})))
    monkeypatch.setattr(hub, '_full_messages', lambda *_: None)
    request = SimpleNamespace(headers={'X-OpenBear-Node': 's'}, remote='127.0.0.1')
    error = asyncio.CancelledError if failure == 'cancel' else ConnectionResetError
    with pytest.raises(error):
        await hub.handle(request)
    assert socket.closed
    assert hub.sessions.get('s') is (replacement if replaced else None)
    assert terminals == ['node_disconnected']
    assert calls[0].done() and isinstance(calls[0].exception(), ConnectionError)
    offline = [c for c in service.node_seen.call_args_list if c.kwargs.get('connected') is False]
    assert len(offline) == (0 if replaced else 1)
    assert hub.pending == {} and hub.handshakes == 0


@pytest.fixture
async def registry(db):
    vault = SimpleNamespace(open=json.loads, seal=json.dumps)
    service = SimpleNamespace(db=db, vault=vault, require_controller=lambda: None)
    value = Servers(service)
    value._exits = AsyncMock(return_value=[])
    value.usage = AsyncMock(return_value={})

    async def get(_, identity):
        async with db.conn.execute('SELECT public_json FROM instance_servers WHERE id=?', (identity,)) as cursor:
            return json.loads((await cursor.fetchone())[0])
    value.get = get
    public = {'name': 'before', 'host': 'old.example', 'port': 22, 'username': 'root', 'auth': 'password', 'custom': 'keep'}
    secret = {'password': 'old-password', 'knownHosts': 'old-pin'}
    await db.conn.execute('INSERT INTO instance_servers VALUES(?,?,?,?,?,?)', ('s', 1, json.dumps(public), json.dumps(secret), 1, 1))
    await db.conn.commit()
    return value


@pytest.mark.parametrize('edit', ['host', 'credentials', 'remove', 'rename', 'pin', 'via', 'deleted', 'unchanged'])
@pytest.mark.parametrize('failed', [False, True])
async def test_c5_slow_check_cannot_restore_old_configuration(registry, db, edit, failed):
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(target, known=''):
        if target['host'] == 'old.example' and not entered.is_set():
            entered.set()
            await release.wait()
            if failed:
                raise InstanceError('server_connection_failed', 502)
            return {'facts': {'status': 'ok', 'os': 'checked', 'host': 'must-not-merge', 'name': 'must-not-merge'}, 'knownHosts': 'checked-pin'}
        return {'facts': {'status': 'ok', 'os': 'new'}, 'knownHosts': 'new-pin'}
    registry.test_target = probe
    checking = asyncio.create_task(registry.check(1, 's'))
    await entered.wait()
    if edit == 'host':
        await registry.save(1, {'host': 'new.example', 'password': 'new-password'}, 's')
    elif edit == 'credentials':
        await registry.save(1, {'password': 'new-password'}, 's')
    elif edit == 'remove':
        await registry.save(1, {'auth': 'none'}, 's')
    elif edit == 'rename':
        await registry.save(1, {'name': 'renamed'}, 's')
    elif edit == 'pin':
        await registry.remember_host_key('s', 'new-pin')
    elif edit == 'via':
        registry._exits = AsyncMock(return_value=['new-exit'])
    elif edit == 'deleted':
        await db.conn.execute('DELETE FROM instance_servers WHERE id=?', ('s',))
        await db.conn.commit()
    async with db.conn.execute('SELECT public_json,protected FROM instance_servers WHERE id=?', ('s',)) as cursor:
        saved = await cursor.fetchone()
    release.set()
    if edit == 'deleted':
        with pytest.raises(InstanceError, match='server_not_found'):
            await checking
        return
    result = await checking
    async with db.conn.execute('SELECT public_json,protected FROM instance_servers WHERE id=?', ('s',)) as cursor:
        after = await cursor.fetchone()
    if edit not in ('rename', 'unchanged'):
        assert result['stale'] is True
        assert tuple(after) == tuple(saved)
    else:
        assert result['stale'] is False
        public, secret = map(json.loads, after)
        assert public['name'] == ('renamed' if edit == 'rename' else 'before') and public['host'] == 'old.example'
        if edit == 'unchanged':
            assert public['custom'] == 'keep'
        assert public['status'] == ('unreachable' if failed else 'ok')
        assert secret['password'] == 'old-password'
        assert secret['knownHosts'] == ('old-pin' if failed else 'checked-pin')


async def test_c4_known_withdrawal_survives_newer_release_and_failed_recheck(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, '_cache', tmp_path)
    monkeypatch.setattr(releases, '_locks', {})
    monkeypatch.setattr(releases, '_prune', lambda _: None)
    releases._write(tmp_path / 'state.json', {'withdrawn': ['1.0.1'], 'latest': '1.0.0'})
    offered = ['1.0.0', '1.0.2']  # withdrawn tag may also have disappeared
    monkeypatch.setattr(releases, '_tags', AsyncMock(side_effect=lambda *_: offered))

    @contextlib.asynccontextmanager
    async def client(_):
        yield object()
    monkeypatch.setattr(releases, '_client', client)
    mode, visited = 'withdrawn', []

    async def usable(_, __, version, prerelease, result):
        visited.append(version)
        if version == '1.0.1':
            if mode == 'error':
                raise RuntimeError('github_http_503')
            if mode == 'withdrawn':
                result['withdrawn'].append(version)
                return None
        return {'binaries': {}}, {}
    monkeypatch.setattr(releases, '_usable', usable)
    for mode in ('withdrawn', 'error', 'restored'):
        visited.clear()
        result = await releases.check(api='https://unused.invalid', prerelease=False)
        assert result['ok'] and result['latest'] == '1.0.2'
        assert visited == ['1.0.2', '1.0.1']
        assert result['withdrawn'] == ([] if mode == 'restored' else ['1.0.1'])
        assert releases.withdrawn() == frozenset(result['withdrawn'])


@pytest.mark.parametrize('state', ['404', 'draft', '503', 'bad-manifest', 'restored', 'restored-incompatible'])
async def test_c4_release_evidence_is_required_to_clear_known_withdrawal(tmp_path, monkeypatch, state):
    monkeypatch.setattr(releases, '_cache', tmp_path)
    monkeypatch.setattr(releases, '_locks', {})
    releases._write(tmp_path / 'state.json', {'withdrawn': ['1.0.1'], 'latest': '1.0.0'})
    monkeypatch.setattr(releases, '_tags', AsyncMock(return_value=['1.0.1', '1.0.2']))
    name = releases.NAMES[0]

    def response(request):
        path = request.url.path
        version = '1.0.1' if '1.0.1' in path else '1.0.2'
        old = version == '1.0.1'
        if '/releases/tags/' in path:
            if old and state in ('404', '503'):
                return httpx.Response(int(state), json={})
            return httpx.Response(200, json={'draft': old and state == 'draft', 'assets': [
                {'name': n, 'browser_download_url': f'https://fake.invalid/assets/{version}/{n}'}
                for n in ('manifest.json', name)]})
        if path.endswith('/manifest.json'):
            manifest = {'schema': 1, 'version': version, 'protocol': releases.PROTOCOL[1],
                        'features': list(releases.REQUIRED_FEATURES),
                        'binaries': {name: {'sha256': 'a' * 64, 'bytes': 1}}}
            if old and state == 'restored-incompatible':
                manifest['protocol'] = releases.PROTOCOL[1] + 1
            return httpx.Response(200, json={} if old and state == 'bad-manifest' else manifest)
        raise AssertionError(f'unexpected request {path}')
    monkeypatch.setattr(releases, '_client', lambda _: httpx.AsyncClient(transport=httpx.MockTransport(response)))
    result = await releases.check(api='https://fake.invalid', prerelease=False)
    assert result['ok'] and result['latest'] == '1.0.2'
    assert releases.withdrawn() == (frozenset() if state.startswith('restored') else frozenset({'1.0.1'}))


async def test_c5_unchanged_key_reference_with_changed_vault_material_is_stale(registry, db):
    secret = {'keyRef': {'secret': 'test-key', 'field': 'pem'}, 'knownHosts': 'old-pin'}
    await db.conn.execute('UPDATE instance_servers SET protected=?,public_json=json_set(public_json,\'$.auth\',\'key\')', (json.dumps(secret),))
    await db.conn.execute('INSERT INTO memory_secrets VALUES(?,?,0)', ('test-key', 'old-test-material'))
    await db.conn.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(*_):
        entered.set()
        await release.wait()
        return {'facts': {'os': 'old-result'}, 'knownHosts': 'old-result-pin'}
    registry.test_target = probe
    checking = asyncio.create_task(registry.check(1, 's'))
    await entered.wait()
    await db.conn.execute('UPDATE memory_secrets SET kv_json=?', ('new-test-material',))
    await db.conn.commit()
    release.set()
    assert (await checking)['stale'] is True
    async with db.conn.execute('SELECT protected FROM instance_servers') as cursor:
        assert json.loads((await cursor.fetchone())[0]) == secret


async def test_c1_settings_failure_restores_persisted_timezone(db, store, monkeypatch):
    service = settings_service(db, store)
    await service.save_settings({'timezone': 'UTC'})
    monkeypatch.setattr(store, 'change_timezone', AsyncMock(side_effect=RuntimeError('migration failed')))
    with pytest.raises(RuntimeError, match='migration failed'):
        await service.save_settings({'timezone': 'Asia/Shanghai'})
    assert (await service.settings())['timezone'] == 'UTC'
    assert store.zone.key == 'UTC'
