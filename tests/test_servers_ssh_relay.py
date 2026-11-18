"""SSH through another server's node (a login's relay), a front's install command fetched through
its exit, a front installed over a login that needs sudo's password, and the install script's
check that a front's exit answers."""
from __future__ import annotations

import hashlib
import http.server
import os
import subprocess
import threading

import pytest

from app.instances import servers as instance_servers
from app.instances.contracts import InstanceError
from app.instances.servers import LOCAL
from app.servers import install, onboarding
from app.webhooks.repository import one
from tests.test_servers import _arg, api, env, shipped, wait_for  # noqa: F401
from tests.test_servers_machine_guard import as_root, script_at  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

OWNER = 123
LINUX = 'os=Debian GNU/Linux 13\nsystem=Linux\ndocker=\nip=203.0.113.7\n'


async def _servers(e):
    relay = (await api(e, 'POST', '/api/servers', {'name': '家'}))['item']['id']
    exit_id = (await api(e, 'POST', '/api/servers', {'name': 'Relay-HK'}))['item']['id']
    front = (await api(e, 'POST', '/api/servers', {'name': '广州', 'profile': {'front': {'exits': [{'serverId': exit_id, 'address': '10.0.10.246'}]}}}))['item']['id']
    return relay, exit_id, front


def _probes(monkeypatch):
    seen = []

    async def fake_ssh(db, target, command, *, stdin=b'', timeout=120, tunnel=None):
        seen.append({'via': target.get('via'), 'front': target.get('front'), 'tunnel': tunnel, 'command': command})
        return {'ok': True, 'exitCode': 0, 'timedOut': False, 'stdout': LINUX, 'stderr': '', 'knownHosts': ''}
    monkeypatch.setattr(instance_servers, '_ssh', fake_ssh)
    return seen


async def test_a_login_goes_through_its_relay(env, monkeypatch):
    e = env
    registry = e.instances.servers
    relay, exit_id, front = await _servers(e)
    # Another of the owner's servers, not this one, not the controller and not a front.
    await registry._check_relay(OWNER, None, relay)
    for identity, bad in ((relay, relay), (None, LOCAL), (None, 'nope'), (None, front)):
        with pytest.raises(InstanceError) as raised:
            await registry._check_relay(OWNER, identity, bad)
        assert raised.value.error == 'invalid_ssh_relay'
    login = {'name': 'S', 'host': '10.0.0.5', 'port': 22, 'username': 'root', 'auth': 'key'}
    assert registry._validate({**login, 'relay': relay}, {})['relay'] == relay
    assert registry._validate({'host': '10.0.0.6'}, {**login, 'relay': relay})['relay'] == relay
    assert 'relay' not in registry._validate({'relay': ''}, {**login, 'relay': relay})
    # Its relay when it names one, else a front's exits; a front probes no echo service either way.
    assert await registry._route(OWNER, None, {'relay': relay}) == {'via': [relay]}
    assert await registry._route(OWNER, front, {}) == {'via': [exit_id], 'front': True}
    assert await registry._route(OWNER, front, {'relay': relay}) == {'via': [relay], 'front': True}
    assert await registry._route(OWNER, relay, {}) == {}

    seen = _probes(monkeypatch)
    saved = (await api(e, 'POST', '/api/servers', {'name': 'NAS', 'ssh': {**login, 'auth': 'password', 'password': 'x', 'relay': relay}}))['item']
    assert seen[-1]['via'] == [relay] and seen[-1]['tunnel'] and 'https://' in seen[-1]['command']
    assert saved['ssh']['relay'] == relay and saved['ssh']['publicIp'] == '203.0.113.7'
    await api(e, 'PATCH', f'/api/instance-servers/{front}', {'name': '广州', **login, 'auth': 'password', 'password': 'x', 'relay': relay})
    assert seen[-1]['via'] == [relay] and seen[-1]['front'] and 'https://' not in seen[-1]['command']
    # An unsaved login is tested as it will be used; a saved one keeps its relay when the form does not offer one.
    await api(e, 'POST', '/api/instance-servers/test', {**login, 'auth': 'password', 'password': 'x', 'serverId': saved['id']})
    assert seen[-1]['via'] == [relay]
    refused = await api(e, 'POST', '/api/instance-servers/test', {**login, 'auth': 'password', 'password': 'x', 'relay': front}, status=400)
    assert refused['error'] == 'invalid_ssh_relay'

    # Its node offline: said as the relay's, not an exit's.
    for target, error in (({'via': [relay], 'relay': relay}, 'ssh_relay_offline'), ({'via': [exit_id], 'front': True}, 'front_exit_offline')):
        with pytest.raises(InstanceError) as raised:
            await registry._tunnel(target)['tunnel']('10.0.0.5:22', None)
        assert raised.value.error == error


async def test_a_front_is_given_a_command_that_fetches_through_its_exit(env, monkeypatch):
    e = env
    _, exit_id, front = await _servers(e)
    port = (await e.servers.settings())['frontPort']
    asked = []

    async def ensure(owner, profile):
        asked.append(profile)

    async def status(server_id):
        return {'port': port, 'fingerprint': 'ab' * 32, 'listening': True, 'error': ''} if server_id == exit_id else {}
    monkeypatch.setattr(e.servers.front, 'ensure', ensure)
    monkeypatch.setattr(e.servers.front, '_status', status)
    enrollment = await api(e, 'POST', f'/api/servers/{front}/enrollment')
    url = f'https://10.0.10.246:{port}'
    assert asked == [{'exits': [{'serverId': exit_id, 'address': '10.0.10.246'}]}]
    command, windows = enrollment['command'], enrollment['windowsCommand']
    assert f'{url}/api/server-nodes/v1/install.sh' in command and hashlib.sha256(install.script().encode()).hexdigest() in command
    assert _arg(command, '--pin') == 'ab' * 32 and '--front' in command.split() and _arg(command, '--code') == enrollment['code']
    assert f"'{url}/api/server-nodes/v1/install.ps1'" in windows and hashlib.sha256(install.windows_script().encode()).hexdigest() in windows
    assert "-Pin '" + 'ab' * 32 + "'" in windows and ' -Front' in windows and '-SkipCertificateCheck' in windows

    # The primary exit offline: refused before any code is made.
    async def offline(owner, profile):
        raise InstanceError('front_exit_offline', 409)
    monkeypatch.setattr(e.servers.front, 'ensure', offline)
    before = (await one(e.db.conn, 'SELECT COUNT(*) AS n FROM server_enrollments'))['n']
    assert (await api(e, 'POST', f'/api/servers/{front}/enrollment', status=409))['error'] == 'front_exit_offline'
    assert (await one(e.db.conn, 'SELECT COUNT(*) AS n FROM server_enrollments'))['n'] == before


class _Script(http.server.BaseHTTPRequestHandler):
    body = b''

    def do_GET(self):  # noqa: N802
        self.send_response(200 if self.path == '/api/server-nodes/v1/install.sh' else 404)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args):
        pass


def test_the_front_command_runs_only_the_script_it_was_made_for(monkeypatch, tmp_path):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Script)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    monkeypatch.setattr(install, 'script', lambda: 'echo ran "$@"\n')
    command = install.front_command(base, 'c0de', node='srv', pin='ab' * 32)

    def run(cmd):
        return subprocess.run(['sh', '-c', cmd], capture_output=True, text=True, timeout=60, cwd=tmp_path)
    try:
        _Script.body = b'echo ran "$@"\n'
        done = run(command)
        assert done.stdout.startswith('ran --url ' + base) and '--front' in done.stdout.split(), done.stderr
        _Script.body = b'echo tampered\n'
        done = run(command)
        assert 'tampered' not in done.stdout and 'install.sh checksum mismatch' in done.stderr
    finally:
        server.shutdown()
        server.server_close()
    done = run(install.front_command(base, 'c0de', pin='ab' * 32))
    assert 'ran' not in done.stdout and 'cannot fetch install.sh through the exit' in done.stderr and 'firewall' in done.stderr


def _check(config, url, *args):
    return subprocess.run(['sh', '-s', '--', '--url', url, '--code', 'c0de', '--node', 'srv', '--stage', 'check', *args], input=script_at(config),
                          capture_output=True, text=True, timeout=60)


def test_a_fronts_check_says_when_its_exit_does_not_answer(tmp_path, shipped, as_root):  # noqa: F811
    config = tmp_path / 'config.json'
    with socpair() as closed:
        done = _check(config, f'http://127.0.0.1:{closed}', '--front')
        assert done.returncode == 1 and f'@@unreachable http://127.0.0.1:{closed}' in done.stdout.splitlines()
        assert "cannot reach the exit's front access" in done.stderr and 'firewall' in done.stderr
        # Not a front: nothing to test here.
        assert _check(config, f'http://127.0.0.1:{closed}').returncode == 0
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Script)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        done = _check(config, f'http://127.0.0.1:{server.server_address[1]}', '--front')
        assert done.returncode == 0 and '@@unreachable' not in done.stdout, done.stderr
    finally:
        server.shutdown()
        server.server_close()


class socpair:
    """A local port nothing listens on, for as long as the block runs."""

    def __enter__(self):
        import socket
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        return self.sock.getsockname()[1]

    def __exit__(self, *exc):
        self.sock.close()


def test_a_front_fetches_its_node_through_the_exit_without_its_certificate_but_with_the_checksum(tmp_path, shipped):  # noqa: F811
    bin_dir, log = tmp_path / 'bin', tmp_path / 'curl.log'
    bin_dir.mkdir()
    (bin_dir / 'curl').write_text(f'#!/bin/sh\necho "$@" >> {log}\n'
                                  'while [ $# -gt 0 ]; do [ "$1" = -o ] && echo junk > "$2"; shift; done\n')
    (bin_dir / 'curl').chmod(0o755)
    env = {**os.environ, 'PATH': f'{bin_dir}:/usr/bin:/bin'}
    for front, insecure in ((['--front'], True), ([], False)):
        log.write_text('')
        done = subprocess.run(['sh', '-s', '--', '--url', 'https://10.0.10.246:52002', '--code', 'c0de', '--stage', 'download', *front],
                              input=script_at(tmp_path / 'config.json'), capture_output=True, text=True, timeout=60, env=env)
        assert done.returncode == 1 and 'download checksum mismatch' in done.stderr, done.stderr
        assert ('-k' in log.read_text().split()) is insecure


def _front_install(e, monkeypatch, exit_id, port, login, check_out=''):
    from tests import fake_node
    from tests.test_servers import run_fake
    monkeypatch.setattr(onboarding, 'ONLINE_WAIT', 15)
    calls, config = [], e.tmp / 'front-node.json'

    async def ready(owner, front):
        return None

    async def status(server_id):
        return {'port': port, 'fingerprint': 'ab' * 32, 'listening': True, 'error': ''} if server_id == exit_id else {}

    async def target(owner, identity, **kw):
        return {'id': identity, 'owner': owner, 'local': False, 'channel': 'ssh', 'host': '113.98.207.105', 'port': 27920, **login, 'via': ['relay'], 'front': True}

    async def platform(target):
        return 'linux'

    async def fake_run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        stage = _arg(command, '--stage')
        calls.append({'stage': stage, 'command': command, 'stdin': stdin, 'root': root, 'sudo': sudo})
        if stage == 'check' and check_out:
            return {'ok': False, 'exitCode': 1, 'timedOut': False, 'stdout': check_out, 'stderr': 'cannot reach', 'knownHosts': ''}
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
    return calls


async def _installed(e, server_id):
    await api(e, 'POST', f'/api/servers/{server_id}/install')

    async def finished():
        state = (await api(e, 'GET', f'/api/servers/{server_id}'))['item']['install']
        return state if state['state'] != 'running' else None
    return await wait_for(finished, 30)


async def test_a_front_installs_over_a_login_that_needs_sudos_password(env, monkeypatch, shipped):  # noqa: F811
    e = env
    _, exit_id, front = await _servers(e)
    port = (await e.servers.settings())['frontPort']
    (shipped / 'openbear-node-linux-amd64').write_bytes(b'a node build')
    calls = _front_install(e, monkeypatch, exit_id, port, {'username': 'debian', 'auth': 'password', 'password': 'pw'})
    state = await _installed(e, front)
    assert state['state'] == 'done', (state.get('error'), [s.get('detail') for s in state['steps']])
    # The script is sent first, as the login; each stage then runs it with sudo, the password on stdin.
    sent = calls[0]
    assert sent['command'].startswith('umask 077 && cat > /tmp/openbear-install-') and sent['stdin'] == install.script().encode()
    assert not sent['sudo'] and not sent['root']
    script = sent['command'].rsplit(' ', 1)[1]
    stages = [c for c in calls if c['stage']]
    assert [c['stage'] for c in stages] == ['check', 'download', 'enroll', 'service']
    for c in stages:
        assert c['command'].startswith(f'sh {script} ') and c['sudo'] and '--front' in c['command'].split()
        assert _arg(c['command'], '--url') == f'https://10.0.10.246:{port}' and _arg(c['command'], '--pin') == 'ab' * 32
    pushed = next(c for c in calls if c['command'].startswith('umask 077 && cat > /tmp/openbear-node-'))
    assert pushed['stdin'] == b'a node build' and not pushed['root'] and not pushed['sudo']
    assert all(_arg(c['command'], '--binary') == pushed['command'].rsplit(' ', 1)[1] for c in stages[1:])
    assert calls[-1]['command'] == f'rm -f {script}'


async def test_a_front_install_says_when_its_exit_does_not_answer(env, monkeypatch, shipped):  # noqa: F811
    e = env
    _, exit_id, front = await _servers(e)
    port = (await e.servers.settings())['frontPort']
    _front_install(e, monkeypatch, exit_id, port, {'username': 'root', 'auth': 'key'},
                   check_out=f'@@step check\narch=amd64 systemd=yes\n@@unreachable https://10.0.10.246:{port}\n')
    state = await _installed(e, front)
    check = next(s for s in state['steps'] if s['key'] == 'check')
    assert state['state'] == 'failed' and state['error'] == 'front_exit_unreachable'
    assert check['state'] == 'failed' and check['detail'] == f'https://10.0.10.246:{port}'
