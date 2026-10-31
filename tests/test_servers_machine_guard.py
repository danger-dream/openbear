"""Installing a node on a machine that already runs one: the install scripts (run for real
with sh, the config path moved to a temporary file) and the SSH install that reads what the
check stage found before anything on the machine changes."""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from app.servers import install
from tests.test_servers import _arg, api, env, shipped, wait_for  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

CONFIG = '/etc/openbear-node/config.json'


def node_config(path, node_id, *, indent=2):
    # As the node writes it (json.MarshalIndent), or on one line.
    path.write_text(json.dumps({'url': 'https://c.example.com', 'nodeId': node_id, 'privateKey': 'secret', 'controllerKey': 'k'},
                               indent=indent) + '\n')


def script_at(config):
    text = install.script()
    assert text.count(CONFIG) >= 1
    return text.replace(CONFIG, str(config))


def run_script(config, *args):
    return subprocess.run(['sh', '-s', '--', '--url', 'http://127.0.0.1:9', '--code', 'c0de', *args], input=script_at(config),
                          capture_output=True, text=True, timeout=60)


@pytest.fixture
def as_root():
    import os
    if os.geteuid() != 0:
        pytest.skip('the check stage runs as root')


def test_the_script_stops_on_another_node_unless_forced(tmp_path, shipped, as_root):  # noqa: F811
    config = tmp_path / 'config.json'
    # No node here yet, or the same node again: on it goes.
    done = run_script(config, '--node', 'srv-a', '--stage', 'check')
    assert done.returncode == 0 and '@@node' not in done.stdout, done.stderr
    node_config(config, 'srv-a')
    done = run_script(config, '--node', 'srv-a', '--stage', 'check')
    assert done.returncode == 0 and '@@node srv-a' in done.stdout.splitlines()
    # Another node: stopped, saying why, before anything is changed.
    for indent in (2, None):
        node_config(config, 'srv-b', indent=indent)
        done = run_script(config, '--node', 'srv-a', '--stage', 'check')
        assert done.returncode == 3 and '@@node srv-b' in done.stdout.splitlines()
        assert 'already runs openbear-node as node srv-b' in done.stderr and '--force' in done.stderr
    # The whole install stops in its check stage: nothing is downloaded or stopped.
    done = run_script(config, '--node', 'srv-a')
    assert done.returncode == 3 and '@@step download' not in done.stdout
    # A command without a node id (a registration code) stops on any node found.
    done = run_script(config, '--stage', 'check')
    assert done.returncode == 3
    # --force replaces it.
    done = run_script(config, '--node', 'srv-a', '--force', '--stage', 'check')
    assert done.returncode == 0 and '@@node srv-b' in done.stdout
    assert json.loads(config.read_text())['nodeId'] == 'srv-b'  # the check itself changes nothing


def test_commands_carry_the_node_id_and_force():
    assert install.arguments('https://c.example.com/', 'code', node='srv-a', force=True).split() == [
        '--url', 'https://c.example.com', '--code', 'code', '--node', 'srv-a', '--force']
    assert '--node srv-a' in install.command('https://c.example.com', 'code', node='srv-a')
    assert '--force' not in install.command('https://c.example.com', 'code', node='srv-a')
    assert install.windows_arguments('https://c.example.com', 'code', node='srv-a', force=True).endswith("-Node 'srv-a' -Force")
    assert "-Node 'srv-a'" in install.windows_command('https://c.example.com', 'code', node='srv-a')
    assert install.existing_node('@@step check\n@@node srv-b\narch=amd64') == 'srv-b' and install.existing_node('fine') == ''
    # The Windows script checks its config the same way, in its check stage before the download.
    text = install.windows_script()
    assert "[string]$Node = ''" in text and '[switch]$Force' in text
    check, download = text.index('"@@node $old"'), text.index("'@@step download'")
    assert text.index("'@@step check'") < check < download and 'if ($old -ne $Node -and -not $Force)' in text


async def test_the_local_command_names_the_local_node(env):  # noqa: F811
    assert _arg((await api(env, 'POST', '/api/servers/local/enrollment'))['localCommand'], '--node') == 'local'


@pytest.mark.host_commands  # Only the check stage runs, which reads a config under tmp_path.
async def test_ssh_install_reads_the_machine_first(env, monkeypatch, tmp_path, shipped, as_root):  # noqa: F811
    """The local server installs through `sh -s` like an SSH one; the check stage is the real script."""
    e = env
    config = tmp_path / 'config.json'
    calls = []

    async def fake_run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        stage = _arg(command, '--stage')
        calls.append((stage, command))
        if stage == 'check':
            done = await asyncio.create_subprocess_exec('sh', '-c', command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            out, err = await done.communicate(stdin.decode().replace(CONFIG, str(config)).encode())
            return {'ok': done.returncode == 0, 'exitCode': done.returncode, 'timedOut': False, 'stdout': out.decode(), 'stderr': err.decode(),
                    'knownHosts': ''}
        if stage == 'enroll':
            # Far enough: the install went past its check.
            return {'ok': False, 'exitCode': 1, 'timedOut': False, 'stdout': '', 'stderr': 'stop here', 'knownHosts': ''}
        return {'ok': True, 'exitCode': 0, 'timedOut': False, 'stdout': f'@@step {stage}', 'stderr': '', 'knownHosts': ''}
    monkeypatch.setattr(e.instances.servers, 'run', fake_run)

    async def install(**body):
        calls.clear()
        await api(e, 'POST', '/api/servers/local/install', body)

        async def finished():
            state = (await api(e, 'GET', '/api/servers/local'))['item']['install']
            return state if state['state'] != 'running' else None
        return await wait_for(finished, 30)

    other = (await api(e, 'POST', '/api/servers', {'name': 'Edge-ES-9u'}))['item']['id']
    # The machine is already connected here as another server: refused, force or not, after the check only.
    node_config(config, other)
    for force in (False, True):
        state = await install(force=force)
        assert state['error'] == 'machine_has_node' and state['existing'] == {'nodeId': other, 'name': 'Edge-ES-9u'}
        assert state['message'] == '这台机器已经作为「Edge-ES-9u」接入，先删除那台或直接在它上面重新安装'
        assert [(s['key'], s['state']) for s in state['steps']][:2] == [('check', 'failed'), ('download', 'pending')]
        assert state['steps'][0]['detail'] == state['message'] and [c[0] for c in calls] == ['check']
        assert '@@node' not in state['output']
        assert _arg(calls[0][1], '--node') == 'local' and ('--force' in calls[0][1].split()) is force
    # A node this controller does not know: refused unless forced.
    node_config(config, 'elsewhere-1')
    state = await install()
    assert state['error'] == 'machine_has_other_node' and state['existing'] == {'nodeId': 'elsewhere-1', 'name': ''}
    assert 'elsewhere-1' in state['message'] and [c[0] for c in calls] == ['check']
    state = await install(force=True)
    assert state['error'] == 'install_enroll_failed' and [c[0] for c in calls] == ['check', 'download', 'enroll']
    # This server's own node: reinstalled as before; no node at all neither.
    node_config(config, 'local')
    state = await install()
    assert state['error'] == 'install_enroll_failed' and state['steps'][0]['state'] == 'done'
    config.unlink()
    state = await install()
    assert state['error'] == 'install_enroll_failed' and state['steps'][0]['state'] == 'done'
    # A force that is not a boolean is refused before anything starts.
    bad = await api(e, 'POST', '/api/servers/local/install', {'force': 'yes'}, status=400)
    assert bad['error'] == 'invalid_install'


async def test_batch_install_refuses_a_machine_known_as_another_server(env, monkeypatch):  # noqa: F811
    e = env
    taken = (await api(e, 'POST', '/api/servers', {'name': 'EdgeES'}))['item']['id']
    forced = []

    async def fake_run(target, command, *, stdin=b'', timeout=120, sudo=False, root=False):
        stage = _arg(command, '--stage')
        if stage == 'check':
            forced.append('--force' in command.split())
            return {'ok': True, 'exitCode': 0, 'timedOut': False, 'stdout': f'@@step check\n@@node {taken}', 'stderr': '', 'knownHosts': ''}
        return {'ok': False, 'exitCode': 1, 'timedOut': False, 'stdout': '', 'stderr': 'unexpected', 'knownHosts': ''}

    async def platform(target):
        return 'linux'
    async def target(owner, identity, **kw):
        return {'id': identity, 'local': False, 'channel': 'ssh', 'username': 'root', 'auth': 'key'}

    async def addresses(owner, server_id):
        return 'https://c.example.com', ''
    real_create = e.servers.create

    async def create(owner, data):
        # No SSH login is checked: the fake target stands for the saved one.
        return await real_create(owner, {'name': data['name']})
    monkeypatch.setattr(e.instances.servers, 'run', fake_run)
    monkeypatch.setattr(e.instances.servers, 'platform', platform)
    monkeypatch.setattr(e.instances.servers, 'target', target)
    monkeypatch.setattr(e.servers.onboarding, 'addresses', addresses)
    monkeypatch.setattr(e.servers, 'create', create)
    job = (await api(e, 'POST', '/api/servers/batch-install', {'servers': [{'host': '192.0.2.84'}], 'ssh': {'username': 'root'},
                                                               'force': True}))['job']

    async def done():
        j = (await api(e, 'GET', f'/api/servers/batch-install/{job["id"]}'))['job']
        return j if j['done'] else None
    item = (await wait_for(done, 30))['items'][0]
    assert item['state'] == 'failed' and item['error'] == 'machine_has_node' and item['install']['existing']['name'] == 'EdgeES', item
    assert forced == [True]
    assert (await api(e, 'POST', '/api/servers/batch-install', {'servers': [{'host': 'h'}], 'ssh': {}, 'force': 1}, status=400))['error'] == 'invalid_batch'
