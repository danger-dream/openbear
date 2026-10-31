"""Servers: what the 2026-10-30 check found, fixed."""
from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path

from app.servers.profile import add_cycle, billing_day, renew
from app.webhooks.repository import one
from tests.test_servers import (  # noqa: F401
    _online,
    api,
    env,
    shipped,
    start_fake,
    wait_for,
)
from tests.test_webhooks_backend import env as base_env  # noqa: F401


async def _node_row(e, server_id, info=None):
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO server_nodes VALUES(?,?,?,?,?,?)",
                           (server_id, 123, base64.b64encode(server_id.encode()[:32].ljust(32, b'k')).decode(), json.dumps(info or {}), 1, 0))


async def test_an_access_point_changed_in_settings_reaches_nodes_offline_then(env, monkeypatch):
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'offline-node'}))['item']['id']
    await _node_row(e, sid)
    await api(e, 'PUT', '/api/servers/settings', {'accessPoints': [{'id': 'ap-a', 'name': '直连', 'url': 'https://old.example.com'}]})
    # The first access point: an offline node keeps the address it enrolled with until it is online.
    assert (await api(e, 'GET', f'/api/servers/{sid}'))['item']['access']['pending'] is False
    await api(e, 'PUT', '/api/servers/settings', {'accessPoints': [{'id': 'ap-a', 'name': '直连', 'url': 'https://new.example.com'}]})
    item = (await api(e, 'GET', f'/api/servers/{sid}'))['item']
    assert item['access']['primary']['url'] == 'https://new.example.com' and item['access']['pending'] is True
    applied = []
    monkeypatch.setattr(e.servers, '_apply_access_soon', lambda server_id: applied.append(server_id))
    await e.servers.node_hello(sid, {'hostname': 'h', 'platform': 'linux'}, {'exec': True}, '1.1.0')
    assert applied == [sid]
    # Settings that leave its addresses alone (the timezone, another access point's name) mark nothing.
    await e.servers.mark_access_pending(sid, False)
    await api(e, 'PUT', '/api/servers/settings', {'timezone': 'Europe/Berlin'})
    await api(e, 'PUT', '/api/servers/settings', {'accessPoints': [{'id': 'ap-a', 'name': '直连', 'url': 'https://new.example.com'},
                                                                   {'id': 'ap-b', 'name': '中转', 'url': 'https://relay.example.com'}]})
    assert (await api(e, 'GET', f'/api/servers/{sid}'))['item']['access']['pending'] is False


async def test_automatic_renewal_goes_on_with_reminders_off_and_a_date_set_back_is_expired(env):
    e = env
    zone = await e.servers.zone()
    today = dt.datetime.now(zone).date()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    sid = (await api(e, 'POST', '/api/servers', {'name': '自动续', 'profile': {'billing': {'price': 5, 'cycle': 30, 'autoRenew': True,
                                                                                         'expiresAt': yesterday}}}))['item']['id']
    await api(e, 'PUT', '/api/servers/settings', {'reminders': {'enabled': False}})
    noon = dt.datetime.combine(today, dt.time(11, 0), zone)
    await e.servers.reminders.check(now=noon)
    renewed = (await api(e, 'GET', f'/api/servers/{sid}'))['item']['profile']['billing']['expiresAt']
    assert renewed > today.isoformat()
    sent = await one(e.db.conn, "SELECT status FROM server_notifications WHERE kind='renewed'")
    assert sent['status'] == 'off'
    # Not renewed after all: the owner sets the date back. Reminders on, it is reported expired, not renewed again.
    await api(e, 'PATCH', f'/api/servers/{sid}', {'profile': {'billing': {'expiresAt': yesterday}}})
    await api(e, 'PUT', '/api/servers/settings', {'reminders': {'enabled': True}})
    await e.servers.reminders.check(now=noon)
    assert (await api(e, 'GET', f'/api/servers/{sid}'))['item']['profile']['billing']['expiresAt'] == yesterday
    assert await one(e.db.conn, "SELECT 1 FROM server_notifications WHERE kind='expired' AND status='pending'")


def test_monthly_renewals_keep_the_billing_day_through_a_short_month():
    billing = {'cycle': 30}
    chain = [dt.date(2027, 1, 31)]
    for _ in range(3):
        chain.append(add_cycle(chain[-1], 30, 31))
    assert chain == [dt.date(2027, 1, 31), dt.date(2027, 2, 28), dt.date(2027, 3, 31), dt.date(2027, 4, 30)]
    assert renew(billing, dt.date(2027, 1, 31), dt.date(2027, 3, 1)) == dt.date(2027, 3, 31)
    assert billing_day(31, dt.date(2027, 2, 28)) == 31 and billing_day(31, dt.date(2027, 3, 15)) == 15


async def test_renewing_keeps_the_billing_day_and_the_traffic_reset_day(env):
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'month-end', 'profile': {
        'billing': {'price': 9.9, 'cycle': 30, 'expiresAt': '2027-01-31'}, 'traffic': {'mode': 'billing', 'limit': 100 * 1024 ** 3}}}))['item']['id']
    await _node_row(e, sid)
    await api(e, 'POST', f'/api/servers/{sid}/renew')
    item = (await api(e, 'GET', f'/api/servers/{sid}'))['item']
    assert item['profile']['billing']['expiresAt'] == '2027-02-28' and item['traffic']['resetDay'] == 31
    await api(e, 'POST', f'/api/servers/{sid}/renew')
    item = (await api(e, 'GET', f'/api/servers/{sid}'))['item']
    assert item['profile']['billing']['expiresAt'] == '2027-03-31' and item['traffic']['resetDay'] == 31


async def test_a_new_node_for_a_server_starts_its_traffic_count(env):
    """Another machine taking the server over (boot time differs) was taken for a reboot,
    counting all its traffic since its boot into this period."""
    metrics = env.servers.metrics
    metrics.observe('s1', {'boot': 1000, 'net': {'rx': 5000, 'tx': 5000}}, now=60)
    assert metrics.observe('s1', {'boot': 1000, 'net': {'rx': 6000, 'tx': 6000}}, now=120) == (1000, 1000)
    await metrics.rebase('s1')
    assert metrics.observe('s1', {'boot': 99999, 'net': {'rx': 10 ** 12, 'tx': 10 ** 12}}, now=180) == (0, 0)
    assert metrics.observe('s1', {'boot': 99999, 'net': {'rx': 10 ** 12 + 10, 'tx': 10 ** 12}}, now=240) == (10, 0)


async def test_a_sysfs_file_downloads_whole(env):
    """/sys files claim 4096 bytes and hold a few: the download was cut at its claimed length."""
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'dl'}))['item']['id']
    await start_fake(e, sid)
    await wait_for(lambda: _online(e, sid))
    regular = e.tmp / 'plain.txt'
    regular.write_bytes(b'x' * 3_000_000)
    got = {}
    for path in (str(regular), '/proc/self/status', '/sys/class/net/lo/address'):
        response = await e.app.get(f'/api/servers/{sid}/files/download', params={'path': path}, cookies=e.cookies, headers=e.origin)
        got[path] = (response.status, response.headers.get('Content-Length'), await response.read())
    assert got[str(regular)][:2] == (200, '3000000') and len(got[str(regular)][2]) == 3_000_000
    assert got['/proc/self/status'][0] == 200 and b'Name:' in got['/proc/self/status'][2]
    assert got['/sys/class/net/lo/address'] == (200, None, Path('/sys/class/net/lo/address').read_bytes())


async def test_reinstalling_a_server_that_took_over_a_joined_machine_goes_past_the_check(env, monkeypatch):
    """From the 10-30 check: the check stopped at the machine's node (the one it took over) and
    the install failed with no reason one could see."""
    from tests.test_servers import _arg
    e = env
    joined = 'joined-node-id'
    await _node_row(e, 'local', {'nodeId': joined, 'info': {'platform': 'linux'}})
    calls = []

    async def fake_run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        stage = _arg(command, '--stage')
        calls.append((stage, '--force' in command.split()))
        if stage == 'check' and '--force' not in command.split():
            return {'ok': False, 'exitCode': 3, 'timedOut': False, 'stdout': f'@@step check\n@@node {joined}\n',
                    'stderr': f'this machine already runs openbear-node as node {joined}', 'knownHosts': ''}
        if stage == 'enroll':
            return {'ok': False, 'exitCode': 1, 'timedOut': False, 'stdout': '', 'stderr': 'stop here', 'knownHosts': ''}
        return {'ok': True, 'exitCode': 0, 'timedOut': False, 'stdout': f'@@step {stage}\n@@node {joined}\n', 'stderr': '', 'knownHosts': ''}
    monkeypatch.setattr(e.instances.servers, 'run', fake_run)
    await api(e, 'POST', '/api/servers/local/install', {})

    async def finished():
        state = (await api(e, 'GET', '/api/servers/local'))['item']['install']
        return state if state['state'] != 'running' else None
    state = await wait_for(finished, 30)
    assert calls[:2] == [('check', False), ('check', True)] and ('download', True) in calls
    assert state['error'] == 'install_enroll_failed'


async def test_the_install_command_of_a_server_that_took_over_names_its_node(env):
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'taken'}))['item']['id']
    await _node_row(e, sid, {'nodeId': 'joined-node-id', 'info': {'platform': 'linux'}})
    await api(e, 'PUT', '/api/servers/settings', {'accessPoints': [{'id': 'ap-a', 'name': '直连', 'url': 'https://ctl.example.com'}]})
    enrollment = await api(e, 'POST', f'/api/servers/{sid}/enrollment', {})
    assert '--node joined-node-id' in enrollment['command'] and sid not in enrollment['command']


async def test_a_terminal_printing_is_not_idle(env, monkeypatch):
    """From the 10-30 check: 30 minutes without a key closed it, killing a build printing all along."""
    import asyncio

    import aiohttp

    from app.servers import service as service_module
    monkeypatch.setattr(service_module, 'TERMINAL_IDLE_S', 2)
    monkeypatch.setattr(service_module, 'TERMINAL_POLL_S', 0.3)
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'tty'}))['item']['id']
    await start_fake(e, sid)
    await wait_for(lambda: _online(e, sid))
    async with aiohttp.ClientSession(cookies=e.cookies) as http:
        async with http.ws_connect(f'{e.url}/api/servers/{sid}/terminal', headers=e.origin) as ws:
            assert (await ws.receive_json())['state'] == 'open'
            await ws.send_bytes(b'while true; do echo tick; sleep 0.4; done\r')

            async def closed_within(seconds):
                end = asyncio.get_running_loop().time() + seconds
                while (left := end - asyncio.get_running_loop().time()) > 0:
                    try:
                        message = await asyncio.wait_for(ws.receive(), left)
                    except TimeoutError:
                        return None
                    if message.type == aiohttp.WSMsgType.TEXT:
                        return json.loads(message.data)
                return None
            assert await closed_within(4) is None
            await ws.send_bytes(b'\x03')
            assert await closed_within(6) == {'type': 'status', 'state': 'closed', 'reason': 'idle'}


def test_a_rename_changing_only_the_case_goes_through_a_temporary_name(tmp_path):
    """From the 10-30 check: on Windows and macOS the target "exists" (it is the source): file_exists."""
    import subprocess

    from app.servers import files
    mac, _ = files.command('rename', {'path': '/Users/a/readme.md', 'name': 'README.md'}, 'darwin')
    assert '.README.md.obrename' in mac and 'exit 17' in mac and mac.endswith('/Users/a/README.md')
    win, _ = files.command('rename', {'path': 'C:/Users/a/readme.md', 'name': 'README.md'}, 'windows')
    assert win.count('Rename-Item') == 2 and "-NewName 'README.md'" in win
    # Linux tells the two names apart: an existing README.md is still refused.
    linux, _ = files.command('rename', {'path': '/home/a/readme.md', 'name': 'README.md'}, 'linux')
    assert 'obrename' not in linux and '[ -e /home/a/README.md ]' in linux
    # The two steps, run here on a directory (mv would otherwise move it inside itself).
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs' / 'a.txt').write_text('x')
    command, _ = files.command('rename', {'path': str(tmp_path / 'docs'), 'name': 'Docs'}, 'darwin')
    assert subprocess.run(['/bin/sh', '-c', command]).returncode == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ['Docs'] and (tmp_path / 'Docs' / 'a.txt').read_text() == 'x'


async def test_the_ai_tool_runs_a_command_on_several_servers_at_once():
    """From the 10-30 check: one after another, 29 nodes took the sum of their run times."""
    import asyncio
    import time
    from types import SimpleNamespace

    from app.tools.servers import ServersTool
    running, peak = 0, 0

    async def exec(owner, server_id, command, **kw):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.2)
        running -= 1
        return {'ok': True, 'exitCode': 0, 'stdout': server_id}
    servers = [{'id': f's{i}', 'name': f'n{i}', 'profile': {}, 'live': True} for i in range(12)]

    async def listing(owner):
        return {'items': servers}
    tool = ServersTool(SimpleNamespace(exec=exec, list=listing))
    started = time.monotonic()
    result = await tool.exec(123, {'command': 'true', 'all': True}, None)
    assert [r['serverId'] for r in result['results']] == [s['id'] for s in servers] and result['ok']
    assert peak == 8 and time.monotonic() - started < 1.0


def test_metrics_series_are_at_most_48_means():
    """From the 10-30 check: 61 points came back, sampled one in N, so a burst between samples vanished."""
    from app.tools.servers import SERIES_POINTS, _averaged
    n = 1440
    points = {'t': list(range(n)), 'cpu': [1.0] * n, 'mem': [None] * n, 'rxRate': [0.0] * n, 'txRate': [0.0] * n}
    points['rxRate'][31] = 3000.0
    size = -(-n // SERIES_POINTS)
    out = _averaged(points, ('cpu', 'mem', 'rxRate', 'txRate'), size)
    assert len(out['t']) == SERIES_POINTS and out['cpu'] == [1.0] * SERIES_POINTS and out['mem'] == [None] * SERIES_POINTS
    assert out['rxRate'][1] == 100.0 and sum(out['rxRate']) == 100.0
