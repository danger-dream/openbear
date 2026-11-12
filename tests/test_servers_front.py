"""前置机 (fronts): an exit's node serves its fronts' access, a front enrolls and stays connected
through it by the exit's certificate, and the controller reaches a front's SSH only through its exit."""
from __future__ import annotations

import asyncio
import base64
import os
import socket
import threading

import pytest

from app.instances import servers as instance_servers
from app.instances import ssh_bridge
from app.instances.contracts import InstanceError
from app.servers import front as fronts
from app.servers import install
from app.servers.profile import clean
from tests.test_servers import api, env, node_binary, run_node, shipped, start_node, wait_for  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

OWNER = 123


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_front_profile_rules():
    p = clean({'front': {'exits': [{'serverId': 'a', 'address': ' 10.0.10.246 '}, {'serverId': 'b', 'address': 'Exit.Example.com'}]}})
    assert p['front'] == {'exits': [{'serverId': 'a', 'address': '10.0.10.246'}, {'serverId': 'b', 'address': 'exit.example.com'}]}
    assert 'front' not in clean({'front': None}, p) and 'front' not in clean({'front': {'exits': []}}, p)
    one = {'serverId': 'a', 'address': '10.0.0.1'}
    for bad in ({'exits': [{'serverId': 'a', 'address': '1.2.3.4:22'}]}, {'exits': [one, one]}, {'exits': [one, {**one, 'serverId': 'b'}, {**one, 'serverId': 'c'}]},
                {'exits': [{'serverId': 'a'}]}, {'other': 1}):
        with pytest.raises(InstanceError):
            clean({'front': bad})
    assert fronts.access_url('fd00::1', 52002) == 'https://[fd00::1]:52002'


async def test_ssh_to_a_front_never_goes_direct():
    with pytest.raises(InstanceError) as raised:
        await instance_servers._ssh(None, {'host': '192.0.2.1', 'port': 22, 'username': 'root', 'via': ['exit']}, 'true')
    assert raised.value.error == 'front_exit_offline'
    windows = base64.b64decode(instance_servers.FRONT_WINDOWS_PROBE.split()[-1]).decode('utf-16-le')
    for probe in (instance_servers.FRONT_PROBE, windows):
        # Nothing beyond its exits: no echo service at all (they are out of reach there anyway).
        assert 'https://' not in probe and 'ip=' in probe


def test_front_install_arguments():
    args = install.arguments('https://10.0.10.246:52002', 'c0de', '', 'enroll', pin='a' * 64, backup_pin='b' * 64, front=True, binary='/tmp/n')
    assert '--pin ' + 'a' * 64 in args and '--front' in args and '--binary /tmp/n' in args
    # No backup address, no backup pin.
    assert '--backup-pin' not in args


async def test_front_enrolls_and_runs_through_its_exit(env, node_binary):
    e = env
    port = _free_port()
    await api(e, 'PUT', '/api/servers/settings', {'frontPort': port})
    exit_id = (await api(e, 'POST', '/api/servers', {'name': 'Relay-HK'}))['item']['id']
    await start_node(e, node_binary, exit_id)
    await wait_for(lambda: _capable(e, exit_id))

    # An exit must be another server, and not a front itself.
    with pytest.raises(InstanceError):
        await e.servers.front.check(OWNER, None, {'exits': [{'serverId': 'nope', 'address': '127.0.0.1'}]})
    front_profile = {'front': {'exits': [{'serverId': exit_id, 'address': '127.0.0.1'}]}}
    front_id = (await api(e, 'POST', '/api/servers', {'name': '广州', 'profile': front_profile}))['item']['id']

    async def ready():
        item = (await api(e, 'GET', f'/api/servers/{front_id}'))['item']
        return item['access']['primary'] if item['access']['primary']['ready'] else None
    point = await wait_for(ready)
    assert point['url'] == f'https://127.0.0.1:{port}' and point['serverId'] == exit_id
    access = await e.servers.onboarding.access(OWNER, front_id)
    assert access['front'] and access['url'] == point['url'] and len(access['pins'][0]) == 64
    # No install command to paste: a front is installed from the panel, over SSH through its exit.
    enrollment = await api(e, 'POST', f'/api/servers/{front_id}/enrollment')
    assert enrollment['command'] == '' and enrollment['windowsCommand'] == ''

    config = e.tmp / 'front.json'
    enrolled = await asyncio.create_subprocess_exec(str(node_binary), 'enroll', '--url', access['url'], '--pin', access['pins'][0], '--front',
                                                    '--code', enrollment['code'], '--config', str(config),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, errors = await asyncio.wait_for(enrolled.communicate(), 30)
    assert enrolled.returncode == 0, errors
    await run_node(e, node_binary, config)
    session = await wait_for(lambda: _session(e, front_id))
    assert session.front and session.endpoint == access['url']
    assert (await e.servers.exec(OWNER, front_id, 'echo through-the-exit'))['stdout'] == 'through-the-exit\n'
    # Nothing beyond its exits: no unlock or route checks run for it.
    assert await e.servers.unlock.domestic(front_id) and await e.servers.route.domestic(front_id)
    with pytest.raises(InstanceError) as raised:
        await e.servers.unlock.run_now(OWNER, front_id)
    assert raised.value.error == 'check_not_for_front'

    # The front's SSH: the controller's ssh reaches the server through a tunnel the exit's node makes.
    seen = asyncio.Event()

    async def fake_sshd(reader, writer):
        writer.write(b'SSH-2.0-OpenSSH_9.0 fake\r\n')
        await writer.drain()
        if (await reader.readline()).startswith(b'SSH-2.0-'):
            seen.set()
        writer.close()
    sshd = await asyncio.start_server(fake_sshd, '127.0.0.1', 0)
    sid_before = e.servers.hub.sessions[exit_id].next_sid
    target = {'host': '127.0.0.1', 'port': sshd.sockets[0].getsockname()[1], 'username': 'root', 'auth': 'password', 'password': 'x', 'via': [exit_id]}
    async with sshd:
        result = await instance_servers._ssh(e.db, target, 'true', timeout=20, tunnel=e.servers.front.opener([exit_id]))
    assert not result['ok'] and seen.is_set()
    assert e.servers.hub.sessions[exit_id].next_sid == sid_before + 1

    # No longer anyone's exit: the access stops.
    await api(e, 'PATCH', f'/api/servers/{front_id}', {'profile': {'front': None}, 'accessApply': 'later'})

    async def stopped():
        return not (await e.servers.front._status(exit_id))
    await wait_for(stopped)


async def test_front_install_sends_the_node_over_ssh_and_enrolls_by_the_exits_certificate(env, monkeypatch, shipped):
    from app.servers import onboarding
    from tests import fake_node
    from tests.test_servers import _arg, run_fake
    e = env
    monkeypatch.setattr(onboarding, 'ONLINE_WAIT', 15)
    exit_id = (await api(e, 'POST', '/api/servers', {'name': 'Relay-HK'}))['item']['id']
    front_id = (await api(e, 'POST', '/api/servers', {'name': '广州', 'profile': {'front': {'exits': [{'serverId': exit_id, 'address': '10.0.10.246'}]}}}))['item']['id']
    port = (await e.servers.settings())['frontPort']
    (shipped / 'openbear-node-linux-amd64').write_bytes(b'a node build')

    async def ready(owner, front):
        return None

    async def status(server_id):
        return {'port': port, 'fingerprint': 'ab' * 32, 'listening': True, 'error': ''} if server_id == exit_id else {}

    async def target(owner, identity, **kw):
        return {'id': identity, 'owner': owner, 'local': False, 'channel': 'ssh', 'host': '172.16.10.203', 'port': 2222,
                'username': 'root', 'auth': 'key', 'via': [exit_id]}

    async def platform(target):
        return 'linux'
    calls, config = [], e.tmp / 'front-node.json'

    async def fake_run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        stage = _arg(command, '--stage')
        calls.append((stage or command, command, stdin, root, target.get('via')))
        if stage == 'enroll':
            await fake_node.enroll(e.url, _arg(command, '--code'), config)
        if stage == 'service':
            await run_fake(e, config)
        out = 'arch=amd64 systemd=yes' if stage == 'check' else ''
        return {'ok': True, 'exitCode': 0, 'timedOut': False, 'stdout': f'@@step {stage}\n{out}', 'stderr': '', 'knownHosts': ''}
    monkeypatch.setattr(e.servers.front, 'ensure', ready)
    monkeypatch.setattr(e.servers.front, '_status', status)
    monkeypatch.setattr(e.instances.servers, 'target', target)
    monkeypatch.setattr(e.instances.servers, 'platform', platform)
    monkeypatch.setattr(e.instances.servers, 'run', fake_run)
    await api(e, 'POST', f'/api/servers/{front_id}/install')

    async def finished():
        state = (await api(e, 'GET', f'/api/servers/{front_id}'))['item']['install']
        return state if state['state'] != 'running' else None
    state = await wait_for(finished, 30)
    assert state['state'] == 'done', (state.get('error'), [s.get('detail') for s in state['steps']])
    stages = [c[0] for c in calls]
    assert stages[0] == 'check' and stages[1].startswith('umask 077 && cat > /tmp/openbear-node-') and stages[2:] == ['download', 'enroll', 'service']
    pushed = stages[1].rsplit(' ', 1)[1]
    assert calls[1][2] == b'a node build'
    for stage, command, stdin, root, via in calls[2:]:
        assert via == [exit_id] and root and _arg(command, '--binary') == pushed and '--front' in command.split()
        assert _arg(command, '--url') == f'https://10.0.10.246:{port}' and _arg(command, '--pin') == 'ab' * 32
    # The front is never told to fetch anything: the script it runs is the controller's, sent over SSH.
    assert all('curl' not in c[1] and 'wget' not in c[1] for c in calls)


async def test_front_port_rejects_offline_front_and_keeps_the_access_a_failed_move_leaves(env, monkeypatch):
    from tests.test_mesh_service import server, connect
    monkeypatch.setattr(env.servers.front, 'soon', lambda owner: None)
    exit_id = await server(env, 'exit', online=False)
    front_id = await server(env, 'front', online=False)
    entry = {'serverId': exit_id, 'address': '10.0.10.246'}
    await env.servers._write_profile(OWNER, front_id, {'front': {'exits': [entry]}})
    old = (await env.servers.settings())['frontPort']
    refused = await api(env, 'PUT', '/api/servers/settings', {'frontPort': old + 1}, status=409)
    assert refused['error'] == 'front_port_fronts_offline'
    assert (await env.servers.settings())['frontPort'] == old
    await connect(env, front_id)
    await connect(env, exit_id, features=('front',))
    status = {'port': old, 'fingerprint': 'ab' * 32, 'listening': True, 'error': ''}
    await env.servers._merge_node_info(exit_id, {'frontAccess': status})

    async def failed(*args):
        return {'ok': False, 'error': 'front_listen_failed'}
    monkeypatch.setattr(env.servers.hub, 'call', failed)
    await api(env, 'PUT', '/api/servers/settings', {'frontPort': old + 1})
    await env.servers.front.sync(OWNER)
    assert await env.servers.front._status(exit_id) == status
    assert not (await env.servers.front.point(entry, old + 1))['ready']


async def test_deleting_a_front_has_its_exits_access_worked_out_again(env, monkeypatch):
    from tests.test_mesh_service import server
    asked = []
    monkeypatch.setattr(env.servers.front, 'soon', asked.append)
    exit_id = await server(env, 'exit', online=False)
    front_id = await server(env, 'front', online=False)
    plain_id = await server(env, 'plain', online=False)
    await env.servers._write_profile(OWNER, front_id, {'front': {'exits': [{'serverId': exit_id, 'address': '10.0.10.246'}]}})
    asked.clear()
    await api(env, 'DELETE', f'/api/servers/{plain_id}')
    assert asked == []
    await api(env, 'DELETE', f'/api/servers/{front_id}')
    assert asked == [OWNER]


@pytest.mark.parametrize('failure', ['refused', 'disconnected'])
async def test_front_ssh_retries_failed_dial_without_closing_sink(failure):
    from types import SimpleNamespace
    from app.servers.hub import NodeHub
    tasks, attempts, closed, output = [], [], [], []

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    hub = NodeHub(SimpleNamespace(spawn=spawn))
    front = fronts.Fronts(SimpleNamespace(hub=hub))

    class Channel:
        def __init__(self, sid):
            self.sid = sid

        async def send_json(self, msg):
            if msg['t'] != 'term.open':
                return
            attempts.append(self.sid)
            session = hub.sessions[self.sid]
            if self.sid == 'primary':
                if failure == 'disconnected':
                    raise ConnectionError('disconnected')
                await hub._message(session, {'t': 'term.closed', 'sid': msg['sid'], 'reason': 'connect_failed'})
            else:
                await hub._message(session, {'t': 'term.opened', 'sid': msg['sid']})
                session.terminals[msg['sid']].feed(b'SSH greeting')

    class Sink:
        async def send_bytes(self, data):
            output.append(data)
        async def send_json(self, value):
            pass
        async def close(self):
            closed.append(True)
    for sid in ('primary', 'backup'):
        hub.sessions[sid] = SimpleNamespace(features={'front'}, caps={'exec': True}, incompatible='',
                                           next_sid=1, terminals={}, channel=Channel(sid))
    terminal = await front.opener(['primary', 'backup'])('10.0.0.1:22', Sink())
    await asyncio.sleep(0)
    assert attempts == ['primary', 'backup'] and not closed and output == [b'SSH greeting']
    assert not hub.sessions['primary'].terminals
    # Once established, closing/failing the SSH connection never redials another exit.
    await terminal.close()
    await asyncio.gather(*tasks)
    assert attempts == ['primary', 'backup'] and closed == [True]


async def test_front_public_exit_ip_follows_exit_public_ipv4_change(env, monkeypatch):
    from tests.test_mesh_service import server, connect
    reconciled = []
    monkeypatch.setattr(env.servers.front, 'soon', lambda owner: None)
    monkeypatch.setattr(env.servers.mesh, '_reconcile_soon', reconciled.append)
    exit_id = await server(env, 'exit', online=False, ipv4='45.32.100.244')
    front_id = await server(env, 'front', online=False)
    await env.servers._write_profile(OWNER, front_id, {
        'front': {'exits': [{'serverId': exit_id, 'address': '45.32.100.244'},
                            {'serverId': exit_id, 'address': '10.0.10.246'}][:1]}})
    port = (await env.servers.settings())['frontPort']
    await connect(env, exit_id, {'platform': 'linux', 'ipv4': '45.32.100.78'}, features=('front',))
    await env.servers._merge_node_info(exit_id, {'frontAccess': {'port': port, 'fingerprint': 'ab' * 32, 'listening': True, 'error': ''}})
    await env.servers.front.sync(OWNER)
    prof = (await env.servers._profiles(OWNER))[front_id]
    assert fronts.exits(prof) == [{'serverId': exit_id, 'address': '45.32.100.78'}]
    assert reconciled == [OWNER]
    # Private dedicated-line addresses stay untouched when public IP changes.
    await env.servers._write_profile(OWNER, front_id, {'front': {'exits': [{'serverId': exit_id, 'address': '10.0.10.246'}]}})
    await env.servers.front.sync(OWNER)
    prof = (await env.servers._profiles(OWNER))[front_id]
    assert fronts.exits(prof) == [{'serverId': exit_id, 'address': '10.0.10.246'}]


async def _capable(e, server_id):
    return e.servers.front.capable(server_id)


async def _session(e, server_id):
    return e.servers.hub.sessions.get(server_id)


async def test_a_sync_asked_for_while_one_runs_runs_for_every_owner_and_past_a_failure():
    """11-12 review: one waiting owner was kept, so a second one asked for in the meantime was lost,
    and a sync that failed stopped the ones after it."""
    gate, done = asyncio.Event(), []

    class Service:
        tasks = set()

        def spawn(self, coro):
            return asyncio.ensure_future(coro)

    f = fronts.Fronts(Service())

    async def sync(owner):
        if owner == 1 and not done:
            await gate.wait()
        done.append(owner)
        if owner == 2:
            raise RuntimeError('provider down')
    f.sync = sync
    f.soon(1)
    await asyncio.sleep(0)
    for owner in (2, 3, 1, 3):
        f.soon(owner)
    gate.set()
    await f.syncing
    assert done[0] == 1 and sorted(done[1:]) == [1, 2, 3] and not f.again


def test_the_ssh_bridge_hands_ssh_all_of_what_comes_when_a_pipe_takes_part(tmp_path, monkeypatch):
    path = str(tmp_path / 'exit.sock')
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    payload = os.urandom(200_000)

    def serve():
        c, _ = srv.accept()
        c.sendall(payload)
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    out, real_read, real_write = [], os.read, os.write

    def write(fd, data):
        if fd != 1:
            return real_write(fd, data)
        out.append(bytes(data[:1000]))
        return len(out[-1])
    monkeypatch.setattr(os, 'read', lambda fd, n: b'' if fd == 0 else real_read(fd, n))
    monkeypatch.setattr(os, 'write', write)
    ssh_bridge.main(path)
    monkeypatch.undo()
    srv.close()
    assert b''.join(out) == payload
