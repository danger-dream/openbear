"""What a node reports is untrusted: addresses, numbers, rates and sizes are bounded, and one node
going wrong does not stop the others or the controller."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
import types

import pytest

from app.instances import keys as keys_module
from app.instances.contracts import InstanceError
from app.servers import hub as hub_module
from app.servers import mesh as mesh_module
from app.servers import mesh_config
from app.servers import metrics as metrics_module
from app.servers import service as service_module
from app.servers.service import ServerService
from app.webhooks.repository import one
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


# ----- S2: addresses -----
def test_reported_addresses_are_ip_literals_and_ipv4_public():
    info = hub_module.clean_info({'ipv4': '45.32.100.1', 'ipv6': '2001:4860::1', 'ipv4Proxy': '1.0.0.42'})
    assert (info['ipv4'], info['ipv6'], info['ipv4Proxy']) == ('45.32.100.1', '2001:4860::1', '1.0.0.42')
    for bad in ('10.0.0.5', '192.168.1.1', '127.0.0.1', '100.64.0.1', '0.0.0.0', '198.51.100.1', '1.2.3.4:80', 'host.example',
                '1.2.3.4\n', '2001:4860::1', ' 45.32.100.1', 7, None, 'x' * 100):
        assert hub_module.clean_info({'ipv4': bad, 'ipv4Proxy': bad})['ipv4'] == '', bad
        assert hub_module.clean_info({'ipv4Proxy': bad})['ipv4Proxy'] == '', bad
    for bad in ('45.32.100.1', 'fe80::1%eth0', '::1]', 'nope'):
        assert hub_module.clean_info({'ipv6': bad})['ipv6'] == '', bad
    # Written out in canonical form.
    assert hub_module.clean_info({'ipv6': '2001:4860:0:0:0:0:0:1'})['ipv6'] == '2001:4860::1'


def test_an_unusable_endpoint_or_reflector_is_left_out_not_the_config():
    member = {'serverId': 'r1', 'role': 'relay', 'publicEndpoint': ''}
    for good in ('45.32.100.1', '2001:4860::1', 'relay.example.com'):
        assert mesh_config.endpoint_host(member, {'r1': {'ipv4': good}}) == good
        assert mesh_config.punch_host(member, {'r1': {'publicIp': good}}) == good
    for bad in ('0.0.0.0', 'bad host', 'a/b', '1.2.3.4:5', '', None, 'x' * 300):
        assert mesh_config.endpoint_host(member, {'r1': {'ipv4': bad}}) == ''
        assert mesh_config.punch_host(member, {'r1': {'publicIp': bad}}) == ''


async def test_a_bad_address_keeps_the_last_good_one(env):  # noqa: F811
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'n'}))['item']['id']
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('INSERT INTO server_nodes(server_id,owner,public_key,info_json,enrolled_ms) VALUES(?,?,?,?,1)',
                           (sid, 123, 'k', '{"info": {"ipv4": "45.32.100.7"}}'))
    await e.servers.node_hello(sid, hub_module.clean_info({'ipv4': 'garbage', 'hostname': 'n'}), {}, 'test')
    row = await one(e.db.conn, "SELECT json_extract(info_json,'$.info.ipv4') AS ip FROM server_nodes WHERE server_id=?", (sid,))
    assert row['ip'] == '45.32.100.7'
    assert (await e.servers.node_channels(123, sid))[sid]['address'] == '45.32.100.7'


# ----- S6: metrics -----
def test_sample_numbers_are_capped_below_2_53():
    sample = hub_module.clean_sample({'mem': {'total': 2 ** 63, 'used': 2 ** 53 - 1}, 'net': {'rx': 1e300, 'tx': -1}, 'uptime': True})
    assert sample['mem'] == {'total': 0, 'used': 2 ** 53 - 1} and sample['net']['rx'] == 0 and sample['net']['tx'] == 0 and sample['uptime'] == 0


async def test_one_bad_minute_is_dropped_and_the_others_are_written(tmp_path):
    store = metrics_module.MetricsStore(tmp_path / 'metrics.db')
    await store.start()
    try:
        now = int(time.time()) // 60 * 60
        bad, good = metrics_module.Bucket(now), metrics_module.Bucket(now)
        for bucket in (bad, good):
            bucket.samples, bucket.counters = 1, (0, 0, 0)
        bad.sums['mem'] = 2 ** 80  # past what SQLite stores
        good.sums['mem'] = 1024
        store.pending = [('bad', bad), ('good', good)]
        await store.flush()
        assert store.pending == []
        async with store.conn.execute('SELECT server_id, mem FROM minute') as cursor:
            assert [tuple(r) for r in await cursor.fetchall()] == [('good', 1024)]
        async with store.conn.execute('SELECT server_id FROM hour') as cursor:
            assert [r[0] for r in await cursor.fetchall()] == ['good']
    finally:
        await store.close()


# ----- S8: what one session may send -----
def test_a_session_budget_refuses_past_its_burst_and_recovers():
    rate = hub_module.Rate(10, 3)
    assert [rate.allow() for _ in range(4)] == [True, True, True, False]
    time.sleep(0.11)
    assert rate.allow() and not rate.allow()
    slow = hub_module.Rate(100, 100)
    assert slow.delay(100) == 0 and slow.delay(50) == pytest.approx(0.5, abs=0.05)
    session = hub_module.Session('n', None, None, '')
    assert sum(session.reports.allow() for _ in range(20)) == hub_module.REPORT_BURST


def _stub_mesh():
    mesh = mesh_module.Mesh.__new__(mesh_module.Mesh)
    tasks = []
    mesh.service = types.SimpleNamespace(spawn=lambda coro: tasks.append(asyncio.ensure_future(coro)) or tasks[-1])
    return mesh, tasks


async def test_reconciles_asked_for_while_one_waits_run_once():
    mesh, tasks = _stub_mesh()
    mesh.reconcile_waiting, runs = {}, []

    async def reconcile(owner):
        mesh.reconcile_waiting.pop(owner, None)
        runs.append(owner)
    mesh.reconcile = reconcile
    for _ in range(50):
        mesh._reconcile_soon(1)
    await asyncio.gather(*tasks)
    assert runs == [1]
    mesh._reconcile_soon(1)
    await asyncio.gather(*tasks)
    assert runs == [1, 1]


# ----- S3: one catch-up per node, never holding the mesh lock while it waits on the node -----
async def test_hellos_while_a_catch_up_runs_have_it_run_once_more():
    mesh, tasks = _stub_mesh()
    mesh.service.hub = types.SimpleNamespace(sessions={})
    mesh.owners, mesh.catching, mesh.catch_again = {'n': 1}, set(), set()
    gate, runs = asyncio.Event(), []

    async def catch_up(owner, sid):
        runs.append(sid)
        await gate.wait()
    mesh._catch_up = catch_up
    mesh.node_hello('n')
    await asyncio.sleep(0)
    for _ in range(20):
        mesh.node_hello('n')
    await asyncio.sleep(0)
    assert runs == ['n'] and len(tasks) == 1
    gate.set()
    await asyncio.gather(*tasks)
    assert runs == ['n', 'n'] and not mesh.catching


# ----- S10: localPrefixes -----
def test_local_prefixes_drop_the_default_route_and_other_nonsense():
    assert mesh_module.clean_local(['0.0.0.0/0', '10.0.0.0/24', '127.0.0.0/8', '224.0.0.0/4', '0.0.0.0/8', '1.0.0.0/7',
                                    '192.168.1.5/24', '10.0.0.0/24', 'fd00::/64', 'bad', 7]) == ['10.0.0.0/24', '192.168.1.0/24']
    assert mesh_module.clean_local('nope') is None


# ----- S11: registration failures count per source -----
async def test_failed_registrations_lock_out_only_their_source(env):  # noqa: F811
    e = env

    async def enroll(source):
        response = await e.app.post('/api/server-nodes/v1/enroll', json={'code': 'reg-wrong', 'publicKey': 'A' * 43 + '='},
                                    headers={'X-Forwarded-For': source})
        return response.status
    assert {await enroll('203.0.113.50') for _ in range(30)} == {403}
    assert await enroll('203.0.113.50') == 429
    assert await enroll('203.0.113.51') == 403


# ----- S12: location lookups -----
async def test_location_is_looked_up_at_most_once_per_interval_per_node(env, monkeypatch):  # noqa: F811
    e = env
    monkeypatch.setattr(service_module, 'GEO_EVERY_S', 0.4)
    looked = []

    async def lookup(http, ip):
        looked.append(ip)
        return {'ip': ip, 'country': 'DE'}
    monkeypatch.setattr(service_module.geo, 'lookup', lookup)
    sid = (await api(e, 'POST', '/api/servers', {'name': 'g'}))['item']['id']
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('INSERT INTO server_nodes(server_id,owner,public_key,info_json,enrolled_ms) VALUES(?,?,?,?,1)', (sid, 123, 'g', '{}'))
    for ip in ('45.32.100.1', '45.32.100.2', '45.32.100.3'):
        await e.servers.node_hello(sid, {'ipv4': ip}, {}, 'test')
        await asyncio.sleep(0.05)
    assert looked == ['45.32.100.1']
    await asyncio.sleep(0.6)
    # The next lookup waits out the interval, then takes the address the node has by then.
    assert looked == ['45.32.100.1', '45.32.100.3']


# ----- S14: job_wait -----
async def test_job_wait_asks_at_most_once_per_round_while_output_keeps_coming(monkeypatch):
    monkeypatch.setattr(service_module, 'JOB_WAIT_ROUND_S', 0.2)
    calls = []

    async def job_status(owner, server_id, job_id, *, offset=0, limit=0):
        calls.append(time.monotonic())
        # Always a little more, never all of it.
        return {'job': {'state': 'running'}, 'log': {'offset': offset, 'next': offset + 1, 'size': offset + 1000, 'data': 'x'}}
    stub = types.SimpleNamespace(job_status=job_status, _search=ServerService._search)
    result = await ServerService.job_wait(stub, 1, 's', 'j', timeout=1, tail=0)
    assert result['reason'] == 'timeout' and 4 <= len(calls) <= 7
    assert min(b - a for a, b in zip(calls, calls[1:])) >= 0.15


async def test_job_wait_patterns_are_matched_off_the_loop_and_time_limited(monkeypatch):
    assert await ServerService._search(r'^ready (\d+)$', 'starting\nready 42\n') == (9, 17)
    assert await ServerService._search('nothing', 'text') is None
    monkeypatch.setattr(service_module, 'JOB_MATCH_TIMEOUT_S', 0.5)
    ticks = []

    async def tick():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)
    ticker = asyncio.create_task(tick())
    started = time.monotonic()
    with pytest.raises(InstanceError) as raised:
        await ServerService._search(r'(a+)+$', 'a' * 64 + 'b')
    ticker.cancel()
    assert raised.value.error == 'pattern_timeout' and time.monotonic() - started < 2
    # The event loop kept running while the pattern backtracked.
    assert len(ticks) >= 5


# ----- I12: the key passphrase stays off the command line -----
@pytest.mark.skipif(not shutil.which('ssh-keygen'), reason='ssh-keygen is not available')
async def test_key_passphrase_is_not_passed_in_argv(tmp_path, monkeypatch):
    path = tmp_path / 'k'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', 'open sesame', '-f', str(path)], check=True)
    pem = path.read_text()
    seen = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        seen.append(args)
        return await real(*args, **kwargs)
    monkeypatch.setattr(keys_module.asyncio, 'create_subprocess_exec', spy)
    unlocked = await keys_module.inspect(pem, 'open sesame')
    assert unlocked['unlocked'] is True
    assert (await keys_module.inspect(pem, 'wrong'))['unlocked'] is False
    assert (await keys_module.inspect(pem))['unlocked'] is False
    assert seen and not any('open sesame' in str(a) or 'wrong' in str(a) for args in seen for a in args)
    plain = tmp_path / 'p'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(plain)], check=True)
    assert (await keys_module.inspect(plain.read_text()))['unlocked'] is True
