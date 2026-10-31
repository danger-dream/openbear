"""An in-process openbear-node for the controller's tests.

It speaks the node's side of the protocol over a real WebSocket to the test controller: the
signed handshake, the sealed channel, hello and reports, calls, terminals and enrollment. What
it does for a call is real where the controller's tests look at the effect (commands run in a
shell, files change on disk, background jobs outlive it, terminals are real PTYs); what only the
Go node can do (mesh, net, ddns, http.local) it refuses as an unknown method.

Every message and call the controller sends is checked against the shared protocol samples in
node/testdata/protocol.md (tests/node_protocol.py), and so is everything it sends back; a
mismatch is kept in FakeNode.problems and fails the test (see check_problems). The Go node's own
tests check the real node against the same samples (node/protocol_samples_test.go)."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import pwd
import re
import resource
import secrets
import shutil
import signal
import struct
import sys
import termios
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from app.servers import hub as hub_module
from tests import node_protocol as samples

PROTOCOL, PROTOCOL_MIN = 1, 1
FEATURES = samples.SAMPLES['msg:hello']['features']
REFUSED_WITHOUT_EXEC = samples.SAMPLES['refusal:capability_disabled']
# Calls whose inner params belong to the browser engine (held to the Go one by test_browser_engine_parity).
ENGINE_OPAQUE = {'browser.call', 'browser.cdp'}
# What only the Go node does: refused here as unknown, and not sampled.
GO_ONLY = ('mesh.', 'net.', 'ddns.', 'http.local')
ARCH = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(os.uname().machine, os.uname().machine)
BINARY = f'openbear-node-linux-{ARCH}'
# As the Go node: output kept per stream, a terminal's unacknowledged bytes, a file chunk.
EXEC_OUTPUT_LIMIT, EXEC_OUTPUT_HEAD = 256 << 10, 64 << 10
OUTPUT_WINDOW = hub_module.OUTPUT_WINDOW
FILE_CHUNK_MAX = 4 << 20
JOB_ID = re.compile(r'^[a-z0-9]{6,40}$')
UPLOAD_ID = re.compile(r'^[A-Za-z0-9]{8,40}$')
SHARED = ipaddress.ip_network('100.64.0.0/10')
# Retries after a dropped link; the Go node waits a second and more.
RETRY_S = 0.2


def b64(raw):
    return base64.b64encode(raw).decode()


def now_ms():
    return int(time.time() * 1000)


class Refused(Exception):
    """An error reply: the node's error code."""


class Revoked(Exception):
    pass


class Unauthorized(Exception):
    pass


class Reconnect(Exception):
    """The session ends after this reply and the node connects again (update, new address)."""


class Uninstalled(Exception):
    pass


class EnrollError(Exception):
    pass


def controller_url(raw):
    """ControllerURL in node/config.go: an https origin, or http for loopback, private, mesh."""
    raw = str(raw or '').strip().rstrip('/')
    u = urlsplit(raw)
    if not u.hostname or u.username or u.query or u.fragment or u.scheme not in {'http', 'https'}:
        raise ValueError('controller url must be an http(s) origin')
    if u.scheme == 'http' and u.hostname != 'localhost':
        try:
            ip = ipaddress.ip_address(u.hostname)
        except ValueError:
            raise ValueError('plain http is only allowed for loopback, private or mesh addresses') from None
        if not (ip.is_loopback or ip.is_private or ip in SHARED):
            raise ValueError('plain http is only allowed for loopback, private or mesh addresses')
    return raw


def _meminfo():
    values = {}
    with contextlib.suppress(OSError):
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, rest = line.partition(':')
            values[key] = int(rest.split()[0]) * 1024
    return values


def collect_info():
    """What the node says about its machine (node/protocol.go Info), read the same way."""
    release = ''
    with contextlib.suppress(OSError):
        release = next((line.split('=', 1)[1].strip().strip('"') for line in Path('/etc/os-release').read_text().splitlines()
                        if line.startswith('PRETTY_NAME=')), '')
    cpu = ''
    with contextlib.suppress(OSError):
        cpu = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name')), '')
    machine = ''
    with contextlib.suppress(OSError):
        machine = Path('/etc/machine-id').read_text().strip()
    mem = _meminfo()
    disk = os.statvfs('/')
    return {'hostname': os.uname().nodename, 'platform': 'linux', 'os': release, 'kernel': os.uname().release, 'arch': ARCH, 'cpu': cpu,
            'cores': os.cpu_count() or 1, 'virtualization': '', 'memTotal': mem.get('MemTotal', 0), 'swapTotal': mem.get('SwapTotal', 0),
            'diskTotal': disk.f_blocks * disk.f_frsize, 'ipv4': '', 'ipv6': '', 'machineId': machine}


def sample_metrics():
    """One report's measurements (node/protocol.go Sample), from /proc like the Go collector."""
    mem = _meminfo()
    disk = os.statvfs('/')
    load = [0.0, 0.0, 0.0]
    with contextlib.suppress(OSError):
        load = [float(v) for v in Path('/proc/loadavg').read_text().split()[:3]]
    uptime = 0
    with contextlib.suppress(OSError):
        uptime = int(float(Path('/proc/uptime').read_text().split()[0]))
    rx = tx = 0
    with contextlib.suppress(OSError):
        for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
            name, _, rest = line.partition(':')
            fields = rest.split()
            if name.strip() != 'lo' and len(fields) > 8:
                rx, tx = rx + int(fields[0]), tx + int(fields[8])
    total, available = mem.get('MemTotal', 0), mem.get('MemAvailable', 0)
    return {'cpu': 1.5, 'steal': 0.0, 'iowait': 0.0, 'mem': {'total': total, 'used': max(0, total - available)},
            'swap': {'total': mem.get('SwapTotal', 0), 'used': max(0, mem.get('SwapTotal', 0) - mem.get('SwapFree', 0))},
            'load': load, 'disk': {'total': disk.f_blocks * disk.f_frsize, 'used': (disk.f_blocks - disk.f_bfree) * disk.f_frsize},
            'io': {'readRate': 0, 'writeRate': 0}, 'net': {'rx': rx, 'tx': tx, 'rxRate': 0, 'txRate': 0},
            'tcp': 1, 'udp': 0, 'processes': len([p for p in os.listdir('/proc') if p.isdigit()]), 'uptime': uptime,
            'boot': int(time.time()) - uptime, 'nodeRss': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}


# ----- enrollment -----
async def enroll(url, code, config_path, *, backup='', fingerprint='', caps=None, info=None, version='dev', problems=None):
    """`openbear-node enroll`: register with a one-time or registration code and save the config.
    Raises EnrollError with the message the node would print."""
    problems = [] if problems is None else problems
    base = controller_url(url)
    spare = controller_url(backup) if backup else ''
    fingerprint = str(fingerprint or '').strip().lower()
    if fingerprint and not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise EnrollError('the controller fingerprint must be 64 hex digits')
    key = Ed25519PrivateKey.generate()
    body = {'code': code, 'publicKey': b64(key.public_key().public_bytes_raw()), 'info': {**collect_info(), **(info or {})},
            'version': version, 'protocol': PROTOCOL, 'protocolMin': PROTOCOL_MIN}
    _check(problems, 'enroll request', body, 'enroll:request')
    async with aiohttp.ClientSession() as http:
        try:
            async with http.post(base + '/api/server-nodes/v1/enroll', json=body, allow_redirects=False) as response:
                status, raw = response.status, await response.read()
        except aiohttp.ClientError:
            if not spare:
                raise
            async with http.post(spare + '/api/server-nodes/v1/enroll', json=body, allow_redirects=False) as response:
                status, raw = response.status, await response.read()
    try:
        result = json.loads(raw)
    except ValueError:
        raise EnrollError(f'enrollment failed: HTTP {status}') from None
    if not result.get('ok') or not result.get('nodeId'):
        _check(problems, 'enroll refusal', result, 'enroll:error')
        raise EnrollError(f"enrollment failed: {result.get('error')}")
    _check(problems, 'enroll response', result, 'enroll:response')
    controller = base64.b64decode(result['controllerKey'])
    actual = hashlib.sha256(controller).hexdigest()
    if fingerprint and actual != fingerprint:
        raise EnrollError(f'enrollment stopped: the controller answered with an identity key whose fingerprint is {actual}, not {fingerprint} '
                          'as in the install command; something on the way may be posing as it. Nothing was saved')
    config = {'url': base, **({'backup': spare} if spare else {}), 'nodeId': result['nodeId'], 'privateKey': b64(key.private_bytes_raw()),
              'controllerKey': result['controllerKey'], 'capabilities': {'exec': True, 'terminal': True, 'browser': False, **(caps or {})}}
    save_config(config_path, config)
    return config


def save_config(path, config):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(config, indent=2))
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _check(problems, label, value, name_or_pattern, *, loose=False):
    found = samples.problems(value, samples.SAMPLES[name_or_pattern] if isinstance(name_or_pattern, str) else name_or_pattern, loose=loose)
    if found:
        problems.append(f'{label} ({name_or_pattern if isinstance(name_or_pattern, str) else "pattern"}): ' + '; '.join(found[:6]))
    return not found


# ----- the node -----
class FakeNode:
    """One `openbear-node run --config PATH`. start() connects in the background; kill() drops
    it at once like a killed process; a new FakeNode on the same config is the node started again
    (its background jobs, kept next to the config, are still there)."""

    def __init__(self, config_path, *, version='dev', exe=None, environ=None, connect=True):
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text())
        self.version = version
        self.environ = dict(os.environ if environ is None else environ)
        # The program file: its sha256 is the build the node reports, and an update replaces it.
        self.exe = Path(exe) if exe else self.config_path.with_name(self.config_path.name + '.bin')
        if not self.exe.exists():
            self.exe.write_bytes(f'fake openbear-node {version} {secrets.token_hex(8)}\n'.encode())
        self.jobs_dir = self.config_path.with_name(self.config_path.name + '.jobs')
        self.on_backup = False
        self.log = []
        self.problems = []
        self.unsampled = set()
        self.calls = []
        self.returncode = None
        self.connected = asyncio.Event()
        self.task = None
        self.channel = None
        self.ws = None
        self.terminals = {}
        self.uploads = {}
        self.browser = FakeBrowserHost(self)
        self._finished = asyncio.Event()
        if connect:
            self.start()

    # -- life --
    @property
    def caps(self):
        return {'exec': False, 'terminal': False, 'browser': False, **self.config.get('capabilities', {})}

    @property
    def build(self):
        return hashlib.sha256(self.exe.read_bytes()).hexdigest()

    def start(self):
        self.task = asyncio.get_running_loop().create_task(self._run(), name=f'fake-node-{self.config.get("nodeId")}')

    async def kill(self):
        """The process dies: no goodbye, the connection just drops."""
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        if self.returncode is None:
            self.returncode = -9
        self._finished.set()

    terminate = kill

    async def wait(self):
        await self._finished.wait()
        return self.returncode

    def _say(self, line):
        self.log.append(line)

    async def _run(self):
        delay = RETRY_S
        try:
            while True:
                try:
                    await self._session()
                    delay = RETRY_S
                except Reconnect:
                    delay = RETRY_S
                    continue
                except Revoked:
                    self._say('controller revoked this node; re-enroll to connect again; retrying in 1h0m0s')
                    await asyncio.sleep(3600)
                except Unauthorized as exc:
                    self._say(f'{exc}; retrying in 5m0s')
                    await asyncio.sleep(300)
                except Uninstalled:
                    self.config_path.unlink(missing_ok=True)
                    self.returncode = 0
                    return
                except (aiohttp.ClientError, ConnectionError, OSError, TimeoutError, ValueError) as exc:
                    self._say(f'disconnected: {exc!r}')
                    # Both addresses are tried, as switchAddress does after failures.
                    if self.config.get('backup'):
                        self.on_backup = not self.on_backup
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2)
        finally:
            self.connected.clear()
            await self._end_terminals()
            await self.browser.stop()
            if self.returncode is not None:
                self._finished.set()

    def address(self):
        return self.config['backup'] if self.on_backup and self.config.get('backup') else self.config['url']

    # -- handshake --
    async def dial(self, http, url, *, probe=False):
        """Connect and authenticate; (ws, channel, first sealed message)."""
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(self.config['privateKey']))
        controller = Ed25519PublicKey.from_public_bytes(base64.b64decode(self.config['controllerKey']))
        node_id = self.config['nodeId']
        headers = {'User-Agent': f'openbear-node/{self.version}', 'X-OpenBear-Node': node_id, 'X-OpenBear-Nonce': secrets.token_hex(16)}
        ws = await http.ws_connect(url.rstrip('/') + '/api/server-nodes/v1/connect', headers=headers, max_msg_size=hub_module.MAX_MESSAGE,
                                   autoping=True, timeout=aiohttp.ClientWSTimeout(ws_close=5))
        try:
            challenge = await ws.receive_json(timeout=10)
            _check(self.problems, 'challenge', challenge, 'msg:challenge')
            eph = X25519PrivateKey.generate()
            mine = b64(eph.public_key().public_bytes_raw())
            base = hub_module.handshake_base(node_id, challenge['nonce'], challenge['eph'], mine)
            params = {'version': self.version, 'protocol': PROTOCOL, 'protocolMin': PROTOCOL_MIN, 'notices': True, **({'probe': True} if probe else {})}
            text = json.dumps(params, separators=(',', ':'))
            _check(self.problems, 'auth.params', params, 'msg:auth.params')
            auth = {'t': 'auth', 'node': node_id, 'eph': mine, 'sig': b64(key.sign(hub_module.signed('client', base))), 'params': text,
                    'paramsSig': b64(key.sign(hub_module.signed('client-params', hub_module.params_base(base, text)))), **params}
            _check(self.problems, 'auth', auth, 'msg:auth')
            await ws.send_json(auth)
            welcome = await ws.receive(timeout=10)
            if welcome.type != aiohttp.WSMsgType.TEXT:
                code = ws.close_code
                if code == hub_module.CLOSE_REVOKED:
                    raise Revoked()
                if code == hub_module.CLOSE_UNAUTHORIZED:
                    raise Unauthorized('the controller refused this node (unauthorized)')
                raise ConnectionError(f'handshake closed: {code}')
            welcome = json.loads(welcome.data)
            _check(self.problems, 'welcome', welcome, 'msg:welcome')
            try:
                controller.verify(base64.b64decode(welcome['sig']), hub_module.signed('server', base))
                controller.verify(base64.b64decode(welcome['paramsSig']), hub_module.signed('server-params', hub_module.params_base(base, text)))
            except (InvalidSignature, KeyError, ValueError):
                raise ConnectionError('the controller did not prove its identity') from None
            shared = eph.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(challenge['eph'])))
            to_controller, to_node = hub_module.session_keys(shared, base)
            channel = hub_module.Channel(ws, to_controller, to_node)
            message = await ws.receive(timeout=10)
            if message.type != aiohttp.WSMsgType.BINARY:
                if ws.close_code == hub_module.CLOSE_REVOKED:
                    raise Revoked()
                raise ConnectionError(f'no session config: {ws.close_code}')
            kind, payload = channel.open(message.data)
            first = json.loads(payload)
            if first.get('t') == 'close':
                _check(self.problems, 'close', first, 'msg:close')
                if first.get('code') == hub_module.CLOSE_REVOKED:
                    raise Revoked()
                raise ConnectionError(first.get('reason'))
            _check(self.problems, 'config', first, 'msg:config')
            if first.get('signedParams') is not True:
                raise ConnectionError('the handshake parameters were stripped on the way')
            return ws, channel, first
        except BaseException:
            await ws.close()
            raise

    async def probe(self, url):
        """Whether url reaches this same controller: a full handshake that ends at the config."""
        try:
            async with aiohttp.ClientSession() as http:
                ws, _, first = await asyncio.wait_for(self.dial(http, url, probe=True), 30)
                await ws.close()
                return first.get('t') == 'config' and first.get('probe') is True
        except (aiohttp.ClientError, ConnectionError, OSError, TimeoutError, ValueError, Revoked, Unauthorized):
            return False

    # -- session --
    async def send(self, value, sample):
        _check(self.problems, f"sent {value.get('t')}", value, sample)
        await self.channel.send_json(value)

    async def hello(self):
        await self.send({'t': 'hello', 'info': collect_info(), 'caps': self.caps, 'version': self.version, 'build': self.build,
                         'endpoint': self.address(), 'backup': self.on_backup, 'features': list(FEATURES),
                         'protocol': PROTOCOL, 'protocolMin': PROTOCOL_MIN}, 'msg:hello')

    async def _session(self):
        async with aiohttp.ClientSession() as http:
            ws, channel, config = await self.dial(http, self.address())
            self.ws, self.channel = ws, channel
            tasks = set()
            reporter = None
            try:
                await self.hello()
                reporter = asyncio.create_task(self._report(config.get('interval') or 3))
                self.connected.set()
                ending = asyncio.get_running_loop().create_future()
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.BINARY:
                        continue
                    kind, payload = channel.open(message.data)
                    if kind == hub_module.KIND_TERMINAL:
                        terminal = self.terminals.get(int.from_bytes(payload[:4], 'big')) if len(payload) > 4 else None
                        if terminal:
                            terminal.write(payload[4:])
                        continue
                    data = json.loads(payload)
                    kind = data.get('t')
                    if kind == 'call':
                        _check(self.problems, 'call', data, 'msg:call')
                        task = asyncio.create_task(self._call(data, ending))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif kind == 'close':
                        _check(self.problems, 'close', data, 'msg:close')
                        if data.get('code') == hub_module.CLOSE_REVOKED:
                            raise Revoked()
                    elif kind in {'term.open', 'term.resize', 'term.ack', 'term.close'}:
                        _check(self.problems, kind, data, f'msg:{kind}')
                        await self._terminal_message(data)
                    else:
                        self.problems.append(f'unexpected message from the controller: {data!r}'[:300])
                    if ending.done():
                        break
                    await asyncio.sleep(0)
                if ending.done():
                    # Let the reply that asked for it go out first.
                    raise ending.result()
                if ws.close_code == hub_module.CLOSE_REVOKED:
                    raise Revoked()
                raise ConnectionError(f'connection closed: {ws.close_code}')
            finally:
                self.connected.clear()
                if reporter:
                    reporter.cancel()
                for task in list(tasks):
                    task.cancel()
                await self._end_terminals()
                self.channel = None
                await ws.close()

    async def _report(self, interval):
        with contextlib.suppress(ConnectionError, RuntimeError):
            while True:
                await self.send({'t': 'report', 'm': sample_metrics()}, 'msg:report')
                await asyncio.sleep(interval)

    async def _call(self, data, ending):
        method, params = data['method'], data.get('params')
        self.calls.append((method, params))
        self._check_params(method, params)
        try:
            result = await self.dispatch(method, params if isinstance(params, dict) else {})
            reply = {'t': 'reply', 'id': data['id'], 'ok': True, 'result': result}
            then = None
        except Refused as exc:
            reply, then = {'t': 'reply', 'id': data['id'], 'ok': False, 'error': str(exc)}, None
        except (Reconnect, Uninstalled) as exc:
            reply, then = {'t': 'reply', 'id': data['id'], 'ok': True, 'result': exc.args[0]}, exc
        except Exception as exc:  # the Go node recovers a failing call as internal_error
            self.problems.append(f'{method} failed in the fake node: {exc!r}')
            reply, then = {'t': 'reply', 'id': data['id'], 'ok': False, 'error': 'internal_error'}, None
        self._check_result(method, reply)
        with contextlib.suppress(ConnectionError, RuntimeError):
            await self.send(reply, 'msg:reply' if reply['ok'] else 'msg:reply/error')
        if then is not None and not ending.done():
            ending.set_result(then)
            # Wake the reading loop, which waits for the next frame.
            with contextlib.suppress(Exception):
                await self.ws.close()

    def _variants(self, method):
        return [v for k, v in samples.SAMPLES.items() if k == f'call:{method}' or k.startswith(f'call:{method}/')]

    def _check_params(self, method, params):
        variants = self._variants(method)
        if not variants:
            self.unsampled.add(method)
            return
        if method in ENGINE_OPAQUE and isinstance(params, dict) and isinstance(params.get('params'), dict):
            # An engine action's or CDP method's own parameters are the engine's, not the link's.
            params = {**params, 'params': {}}
            variants = [{**v, 'params': {**v['params'], 'params': {}}} for v in variants]
        found = [samples.problems(params, v['params'], loose=True) for v in variants]
        if all(found):
            self.problems.append(f'{method} params match no sample: {params!r}'[:300] + ' — ' + '; '.join(min(found, key=len)[:4]))

    def _check_result(self, method, reply):
        variants = self._variants(method)
        if not variants:
            return
        if reply['ok']:
            patterns = [v['result'] for v in variants if 'result' in v]
            if patterns and all(samples.problems(reply['result'], p, loose=True) for p in patterns):
                found = min((samples.problems(reply['result'], p, loose=True) for p in patterns), key=len)
                self.problems.append(f'{method} result matches no sample: ' + '; '.join(found[:4]))

    # -- calls --
    async def dispatch(self, method, p):
        if method == 'ping':
            return {'version': self.version}
        if method == 'uninstall':
            raise Uninstalled({'version': self.version})
        if method == 'config.url':
            return await self.change_url(p)
        if method.startswith('browser.'):
            return await self.browser.call(method, p)
        if method.startswith(GO_ONLY):
            raise Refused('unsupported_method')
        if not self.caps['exec']:
            raise Refused('capability_disabled' if method in REFUSED_WITHOUT_EXEC else 'unsupported_method')
        if method == 'exec':
            return await run_command(p, self.environ)
        if method == 'update':
            return await self.update(p)
        if method in {'fs.list', 'fs.stat', 'fs.read', 'fs.write'}:
            return await asyncio.to_thread(self.files, method, p)
        if method == 'fs.edit':
            return await asyncio.to_thread(edit_file, p)
        if method in {'job.start', 'job.list', 'job.status', 'job.stop'}:
            return await JobStore(self.jobs_dir, self.environ).handle(method, p)
        raise Refused('unsupported_method')

    async def change_url(self, p):
        try:
            url = controller_url(p.get('url'))
            backup = controller_url(p['backup']) if p.get('backup') else ''
        except ValueError:
            raise Refused('invalid_url') from None
        if url == self.config['url'] and backup == self.config.get('backup', ''):
            return {'url': url, 'backup': backup, 'changed': False}
        if not await self.probe(url):
            raise Refused('new_url_unreachable')
        reachable = not backup or await self.probe(backup)
        self.config = {**self.config, 'url': url}
        self.config.pop('backup', None)
        if backup:
            self.config['backup'] = backup
        save_config(self.config_path, self.config)
        self.on_backup = False
        raise Reconnect({'url': url, 'backup': backup, 'backupReachable': reachable, 'changed': True})

    async def update(self, p):
        if not p.get('version') or not re.fullmatch(r'[0-9a-fA-F]{64}', str(p.get('sha256', ''))) or not 0 < int(p.get('size') or 0) <= 64 << 20:
            raise Refused('invalid_params')
        if p.get('name') != BINARY:
            raise Refused('wrong_platform')
        if p['sha256'].lower() == self.build:
            return {'from': self.version, 'to': self.version, 'changed': False}
        async with aiohttp.ClientSession() as http:
            try:
                async with http.get(self.address() + '/api/server-nodes/v1/download/' + p['name'], allow_redirects=False) as response:
                    if response.status != 200:
                        raise Refused('download_failed')
                    data = await response.content.read(p['size'] + 1)
            except aiohttp.ClientError:
                raise Refused('download_failed') from None
        if len(data) != p['size'] or hashlib.sha256(data).hexdigest() != p['sha256'].lower():
            raise Refused('checksum_mismatch')
        # The Go node also runs the new file once ("version") before it replaces itself.
        staging = self.exe.with_name(self.exe.name + '.new')
        staging.write_bytes(data)
        os.chmod(staging, 0o755)
        os.replace(staging, self.exe)
        before, self.version = self.version, p['version']
        raise Reconnect({'from': before, 'to': p['version'], 'changed': True})

    def files(self, method, p):
        path = p.get('path')
        if not isinstance(path, str) or '\0' in path or not os.path.isabs(path) or int(p.get('offset') or 0) < 0:
            raise Refused('invalid_params')
        path = os.path.normpath(path)
        if method == 'fs.stat':
            try:
                return describe(path, os.path.basename(path), os.lstat(path))
            except OSError as exc:
                raise Refused(file_error(exc)) from None
        if method == 'fs.list':
            try:
                names = os.listdir(path)
            except OSError as exc:
                raise Refused(file_error(exc)) from None
            entries = []
            for name in names[:5000]:
                with contextlib.suppress(OSError):
                    entries.append(describe(os.path.join(path, name), name, os.lstat(os.path.join(path, name))))
            entries.sort(key=lambda e: (not e.get('dir', False), e['name']))
            return {'path': path, 'entries': entries, 'truncated': len(names) > 5000}
        if method == 'fs.read':
            return read_file(path, int(p.get('offset') or 0), int(p.get('limit') or 0))
        return self.write(path, p)

    def write(self, path, p):
        try:
            data = base64.b64decode(p.get('data') or '', validate=True)
        except ValueError:
            raise Refused('invalid_params') from None
        if len(data) > FILE_CHUNK_MAX:
            raise Refused('invalid_params')
        try:
            if p.get('mkdirs'):
                os.makedirs(os.path.dirname(path), 0o755, exist_ok=True)
            if os.path.islink(path):
                path = os.path.realpath(path)
            prior = os.stat(path) if os.path.exists(path) else None
        except OSError as exc:
            raise Refused(file_error(exc)) from None
        if prior and os.path.isdir(path):
            raise Refused('is_directory')
        upload = p.get('upload') or ''
        if not upload:
            temp = os.path.join(os.path.dirname(path), f'.{os.path.basename(path)}.obtmp-{secrets.token_hex(6)}')
            _write(temp, data, 'xb')
            commit(temp, path, p.get('mode') or '', prior)
            return {'path': path, 'size': len(data), 'done': True}
        if not UPLOAD_ID.match(upload):
            raise Refused('invalid_params')
        offset = int(p.get('offset') or 0)
        state = self.uploads.get(upload)
        if offset == 0:
            if state:
                with contextlib.suppress(OSError):
                    os.remove(state['temp'])
            state = self.uploads[upload] = {'temp': os.path.join(os.path.dirname(path), f'.{os.path.basename(path)}.obupload-{upload}'),
                                            'target': path, 'size': 0}
        elif not state or state['target'] != path or state['size'] != offset:
            raise Refused('upload_out_of_order')
        _write(state['temp'], data, 'wb' if offset == 0 else 'ab')
        state['size'] += len(data)
        if not p.get('done'):
            return {'path': path, 'size': state['size'], 'done': False}
        del self.uploads[upload]
        commit(state['temp'], path, p.get('mode') or '', prior)
        return {'path': path, 'size': state['size'], 'done': True}

    # -- terminals --
    async def _terminal_message(self, data):
        sid = data.get('sid')
        kind = data['t']
        if kind == 'term.open':
            if not self.caps['terminal']:
                await self.send({'t': 'term.closed', 'sid': sid, 'reason': 'capability_disabled'}, 'msg:term.closed')
                return
            try:
                terminal = Terminal(self, sid, data.get('cols') or 80, data.get('rows') or 24)
                await terminal.start()
            except OSError:
                await self.send({'t': 'term.closed', 'sid': sid, 'reason': 'start_failed'}, 'msg:term.closed')
                return
            self.terminals[sid] = terminal
            await self.send({'t': 'term.opened', 'sid': sid}, 'msg:term.opened')
            # As termOpen in node/terminal.go: output is read only after term.opened went out.
            terminal._read_on()
        elif (terminal := self.terminals.get(sid)) is None:
            return
        elif kind == 'term.resize':
            terminal.resize(data.get('cols') or 80, data.get('rows') or 24)
        elif kind == 'term.ack':
            terminal.acked(int(data.get('n') or 0))
        elif kind == 'term.close':
            await terminal.close('closed')

    async def _end_terminals(self):
        for terminal in list(self.terminals.values()):
            await terminal.close('node_disconnected', tell=False)

    # -- after a test --
    async def stop_jobs(self):
        """Ends what this node's background jobs still run (a test leaves no processes behind)."""
        store = JobStore(self.jobs_dir, self.environ)
        for view in store.views():
            if view['state'] == 'running':
                with contextlib.suppress(Refused):
                    await store.handle('job.stop', {'id': view['id']})


def problems_of(nodes):
    """The protocol mismatches the fake nodes saw, and the methods the controller called that
    have no sample (a new call needs one, so the Go test checks the node answers it)."""
    found = [p for node in nodes for p in node.problems]
    unsampled = sorted({m for node in nodes for m in node.unsampled if not m.startswith(GO_ONLY)})
    return found + [f'{m}: the controller calls it and node/testdata/protocol.md has no call:{m} sample' for m in unsampled]


def check_problems(nodes):
    """Fails on any protocol mismatch the fake nodes saw (call from a fixture's teardown)."""
    found = problems_of(nodes)
    assert not found, 'protocol samples (node/testdata/protocol.md) and the controller disagree:\n' + '\n'.join(found[:20])


# ----- commands -----
def shell_env(environ, shell):
    """shellEnv in node/shell_unix.go: a UTF-8 locale, and HOME, USER, LOGNAME when missing."""
    env = {**environ, 'LANG': 'C.UTF-8'}
    env.setdefault('SHELL', shell)
    if not (environ.get('HOME') and environ.get('USER') and environ.get('LOGNAME')):
        account = pwd.getpwuid(os.getuid())
        for key, value in (('HOME', account.pw_dir), ('USER', account.pw_name), ('LOGNAME', account.pw_name)):
            if not environ.get(key):
                env[key] = value
    return env


def login_shell():
    return '/bin/bash' if os.access('/bin/bash', os.X_OK) else '/bin/sh'


def shell_argv(shell, command):
    return [shell, '-l', '-c', command] if shell.endswith('bash') else [shell, '-c', command]


def capped(data):
    if len(data) <= EXEC_OUTPUT_LIMIT:
        return data.decode(errors='replace'), False
    head, tail = data[:EXEC_OUTPUT_HEAD], data[-(EXEC_OUTPUT_LIMIT - EXEC_OUTPUT_HEAD):]
    head, tail = head.decode(errors='ignore'), tail.decode(errors='ignore')
    omitted = len(data) - len(head.encode()) - len(tail.encode())
    return f'{head}\n…（省略 {omitted} 字节）…\n{tail}', True


async def run_command(p, environ):
    """Exec in node/exec.go: bash as a login shell (plain sh when asked), its own process group,
    killed whole at the timeout; output still collected for a second after the shell ends."""
    command = p.get('command')
    if not isinstance(command, str) or not command.strip():
        raise Refused('invalid_params')
    timeout = min(int(p.get('timeout') or 0) or 120, 3600)
    shell = '/bin/sh' if p.get('shell') == 'sh' else login_shell()
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(*shell_argv(shell, command), cwd=p.get('cwd') or None, env=shell_env(environ, shell),
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    start_new_session=True)
    except OSError:
        raise Refused('start_failed') from None
    out, err = bytearray(), bytearray()

    async def pump(stream, into):
        while chunk := await stream.read(65536):
            into += chunk

    async def feed():
        with contextlib.suppress(ConnectionError, BrokenPipeError):
            proc.stdin.write(str(p.get('stdin') or '').encode())
            await proc.stdin.drain()
        with contextlib.suppress(ConnectionError, BrokenPipeError):
            proc.stdin.close()
    readers = [asyncio.create_task(pump(proc.stdout, out)), asyncio.create_task(pump(proc.stderr, err)), asyncio.create_task(feed())]
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
    await asyncio.wait(readers, timeout=1)
    for task in readers:
        task.cancel()
    stdout, cut_out = capped(bytes(out))
    stderr, cut_err = capped(bytes(err))
    return {'exitCode': -1 if timed_out or proc.returncode < 0 else proc.returncode, 'stdout': stdout, 'stderr': stderr,
            'truncated': cut_out or cut_err, 'timedOut': timed_out, 'durationMs': int((time.monotonic() - started) * 1000), 'shell': shell}


# ----- files -----
def file_error(exc):
    import errno
    return {errno.ENOENT: 'not_found', errno.EACCES: 'permission_denied', errno.EPERM: 'permission_denied', errno.EISDIR: 'is_directory',
            errno.ENOTDIR: 'not_a_directory', errno.ENOSPC: 'no_space', errno.EROFS: 'read_only_filesystem'}.get(exc.errno, 'io_error')


def describe(path, name, info):
    import stat as st
    entry = {'name': name, 'type': 'other', 'size': info.st_size, 'mode': f'{st.S_IMODE(info.st_mode) & 0o777:04o}', 'mtime': int(info.st_mtime)}
    if st.S_ISDIR(info.st_mode):
        entry.update(type='dir', dir=True)
    elif st.S_ISREG(info.st_mode):
        entry['type'] = 'file'
    elif st.S_ISLNK(info.st_mode):
        entry['type'] = 'link'
        with contextlib.suppress(OSError):
            entry['target'] = os.readlink(path)
        if os.path.isdir(path):
            entry['dir'] = True
    return {k: entry[k] for k in ('name', 'type', 'dir', 'size', 'mode', 'mtime', 'target') if k in entry}


def read_file(path, offset, limit):
    import stat as st
    try:
        with open(path, 'rb') as f:
            info = os.fstat(f.fileno())
            if st.S_ISDIR(info.st_mode):
                raise Refused('is_directory')
            if not st.S_ISREG(info.st_mode):
                raise Refused('not_regular_file')
            f.seek(offset)
            want = min(limit if limit > 0 else 1 << 20, FILE_CHUNK_MAX)
            data = f.read(want)
    except IsADirectoryError:
        raise Refused('is_directory') from None
    except OSError as exc:
        raise Refused(file_error(exc)) from None
    # As ReadAt: a short read is the end; a file that claims a size reads to it.
    eof = len(data) < want or info.st_size > 0 and offset + len(data) >= info.st_size
    return {'data': b64(data), 'size': info.st_size, 'offset': offset, 'read': len(data), 'eof': eof,
            'mode': f'{info.st_mode & 0o777:04o}', 'mtime': int(info.st_mtime)}


def _write(path, data, mode):
    try:
        with open(path, mode) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if 'x' in mode:
            os.chmod(path, 0o600)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(path)
        raise Refused(file_error(exc)) from None


def commit(temp, path, mode, prior):
    perm = (prior.st_mode & 0o7777) if prior else 0o644
    if mode:
        try:
            perm = int(mode, 8)
        except ValueError:
            perm = -1
        if not 0 <= perm <= 0o7777:
            os.remove(temp)
            raise Refused('invalid_mode')
    try:
        os.chmod(temp, perm)
        if prior:
            with contextlib.suppress(OSError):
                os.chown(temp, prior.st_uid, prior.st_gid)
        os.replace(temp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise Refused(file_error(exc)) from None


def edit_file(p):
    """uploads.edit in node/fs_edit.go: exact replacements, all or nothing, with the same failures
    and the same unified diff."""
    path, edits = p.get('path'), p.get('edits')
    if not isinstance(path, str) or '\0' in path or not isinstance(edits, list) or not 0 < len(edits) <= 100:
        raise Refused('invalid_params')
    if any(not isinstance(e, dict) or not e.get('old') for e in edits) or not os.path.isabs(path):
        raise Refused('invalid_params')
    path = os.path.normpath(path)
    if os.path.islink(path):
        path = os.path.realpath(path)
    try:
        prior = os.stat(path)
    except FileNotFoundError:
        raise Refused('not_found') from None
    except OSError as exc:
        raise Refused(file_error(exc)) from None
    import stat as st
    if st.S_ISDIR(prior.st_mode):
        raise Refused('is_directory')
    if not st.S_ISREG(prior.st_mode):
        raise Refused('not_regular_file')
    if prior.st_size > FILE_CHUNK_MAX:
        raise Refused('file_too_large')
    raw = Path(path).read_bytes()
    try:
        if b'\0' in raw:
            raise UnicodeDecodeError('utf-8', raw, 0, 1, 'nul')
        text = raw.decode()
    except UnicodeDecodeError:
        raise Refused('not_text') from None
    starts = [0] + [m.end() for m in re.finditer('\n', text)]
    crlf = '\r\n' in text
    found, failures, converted = [], [], False
    for i, e in enumerate(edits, 1):
        old, new, every = str(e['old']), str(e.get('new') or ''), e.get('replaceAll') is True
        at = _occurrences(text, old, every)
        if not at and crlf and '\n' in old:
            alt = old.replace('\r\n', '\n').replace('\n', '\r\n')
            if alt != old and (at := _occurrences(text, alt, every)):
                old, new, converted = alt, new.replace('\r\n', '\n').replace('\n', '\r\n'), True
        if len(at) > 100000:
            failures.append({'edit': i, 'reason': 'too_many', 'count': 100000})
        elif not at:
            failures.append({'edit': i, 'reason': 'not_found'})
        elif len(at) > 1 and not every:
            failures.append({'edit': i, 'reason': 'multiple', 'count': len(at), 'lines': [_line_of(starts, pos) for pos in at[:5]]})
        else:
            found += [(pos, pos + len(old), new, i) for pos in at]
    found.sort(key=lambda r: r[0])
    seen = set()
    for a, b in zip(found, found[1:]):
        if b[0] < a[1]:
            pair = (min(a[3], b[3]), max(a[3], b[3]))
            if pair not in seen:
                seen.add(pair)
                failures.append({'edit': pair[1], 'reason': 'overlap', 'with': pair[0]})
    if failures:
        failures.sort(key=lambda f: f['edit'])
        return {'path': path, 'changed': False, 'failures': failures}
    pieces, last = [], 0
    for start, end, new, _ in found:
        pieces += [text[last:start], new]
        last = end
    nxt = ''.join(pieces) + text[last:]
    if len(nxt.encode()) > FILE_CHUNK_MAX:
        raise Refused('file_too_large')
    result = {'path': path, 'replacements': len(found), 'size': len(nxt.encode())}
    if converted:
        result['lineEndings'] = 'crlf'
    if nxt == text:
        return {**result, 'changed': False, 'diff': ''}
    temp = os.path.join(os.path.dirname(path), f'.{os.path.basename(path)}.obtmp-{secrets.token_hex(6)}')
    _write(temp, nxt.encode(), 'xb')
    now = os.stat(path)
    if now.st_size != prior.st_size or now.st_mtime_ns != prior.st_mtime_ns:
        os.remove(temp)
        raise Refused('file_changed')
    commit(temp, path, '', prior)
    diff, cut = unified_diff(path, text, nxt, found)
    return {**result, 'changed': True, 'diff': diff, 'diffTruncated': cut}


def _occurrences(text, old, every):
    at, start, step = [], 0, len(old) if every else 1
    while start <= len(text) and len(at) <= 100000:
        i = text.find(old, start)
        if i < 0:
            break
        at.append(i)
        start = i + step
    return at


def _line_of(starts, pos):
    import bisect
    return bisect.bisect_right(starts, pos)


def _at_line_start(s, p):
    return p == 0 or p == len(s) or s[p - 1] == '\n'


def _line_count(s, p):
    n = s[:p].count('\n')
    return n + 1 if p == len(s) and p > 0 and s[p - 1] != '\n' else n


def _file_lines(s):
    lines = s.split('\n')
    if lines[-1] == '':
        lines.pop()
    return [line.removesuffix('\r') for line in lines]


def unified_diff(name, old, nxt, found, limit=12000, context=3):
    changes, delta, i = [], 0, 0
    while i < len(found):
        start = old.rfind('\n', 0, found[i][0]) + 1
        before = delta
        while True:
            r_start, r_end, text, _ = found[i]
            delta += len(text) - (r_end - r_start)
            end = r_end
            if not _at_line_start(old, end) or not _at_line_start(nxt, end + delta):
                j = old.find('\n', end)
                end = j + 1 if j >= 0 else len(old)
            i += 1
            if i < len(found) and old.rfind('\n', 0, found[i][0]) + 1 < end:
                continue
            break
        changes.append((_line_count(old, start), _line_count(old, end), _line_count(nxt, start + before), _line_count(nxt, end + delta)))
    old_lines, new_lines = _file_lines(old), _file_lines(nxt)
    out = [f'--- {name}\n+++ {name}\n']

    def hunk(frm, count):
        return f'{frm},0' if count == 0 else f'{frm + 1},{count}'
    i = 0
    while i < len(changes):
        j = i
        while j + 1 < len(changes) and changes[j + 1][0] - changes[j][1] <= 2 * context:
            j += 1
        frm, to = max(0, changes[i][0] - context), min(len(old_lines), changes[j][1] + context)
        new_from = frm + changes[i][2] - changes[i][0]
        new_count = to - frm + sum((c[3] - c[2]) - (c[1] - c[0]) for c in changes[i:j + 1])
        out.append(f'@@ -{hunk(frm, to - frm)} +{hunk(new_from, new_count)} @@\n')
        at = frm
        for a1, a2, b1, b2 in changes[i:j + 1]:
            out += [f' {line}\n' for line in old_lines[at:a1]]
            out += [f'-{line}\n' for line in old_lines[a1:a2]]
            out += [f'+{line}\n' for line in new_lines[b1:b2]]
            at = a2
        out += [f' {line}\n' for line in old_lines[at:to]]
        i = j + 1
    diff = ''.join(out)
    if len(diff) <= limit:
        return diff, False
    return diff[:diff.rfind('\n', 0, limit) + 1], True


# ----- background jobs -----
# The job runner: a process of its own (a new session), so it outlives the node as the Go
# node's "job-run" does. It keeps the same files: job.json, stdin, pid, child, log, exit, stop.
RUNNER = r'''
import json, os, signal, subprocess, sys, time
d = sys.argv[1]
spec = json.load(open(os.path.join(d, "job.json")))
def put(name, text):
    with open(os.path.join(d, name + ".tmp"), "w") as f:
        f.write(text)
    os.replace(os.path.join(d, name + ".tmp"), os.path.join(d, name))
put("pid", "%d x\n" % os.getpid())
env = json.load(open(os.path.join(d, "env.json")))
log = open(os.path.join(d, "log"), "ab", buffering=0)
stdin = open(os.path.join(d, "stdin"), "rb") if spec.get("stdin") else subprocess.DEVNULL
shell, command = spec["shell"], spec["command"]
argv = [shell, "-l", "-c", command] if shell.endswith("bash") else [shell, "-c", command]
try:
    child = subprocess.Popen(argv, cwd=spec.get("cwd") or None, stdin=stdin, stdout=log, stderr=log, env=env, start_new_session=True)
except OSError as exc:
    log.write(("openbear-node: cannot start the job: %s\n" % exc).encode())
    put("exit", json.dumps({"exitCode": -1, "endedAt": int(time.time() * 1000)}))
    sys.exit(0)
put("child", "%d x\n" % child.pid)
stopping = None
while child.poll() is None:
    if stopping is None and os.path.exists(os.path.join(d, "stop")):
        stopping = time.monotonic()
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif stopping is not None and time.monotonic() - stopping > 5:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.05)
if stopping is not None:
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
code, name = child.returncode, ""
if code < 0:
    name, code = signal.Signals(-code).name, 128 - code
put("exit", json.dumps({"exitCode": code, "signal": name, "endedAt": int(time.time() * 1000)} if name else
                       {"exitCode": code, "endedAt": int(time.time() * 1000)}))
'''

# Runner processes are children of the test process: reaped here so a finished one is not a zombie.
_runners = set()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


def _base36(n):
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or '0'


class JobStore:
    """jobStore in node/jobs.go, on the same files."""

    def __init__(self, root, environ):
        self.root, self.environ = Path(root), environ

    async def handle(self, method, p):
        if method == 'job.start':
            return await self.start(p)
        if method == 'job.list':
            return {'jobs': sorted(self.views(), key=lambda v: v['startedAt'], reverse=True)}
        job_id = p.get('id')
        if not isinstance(job_id, str) or not JOB_ID.match(job_id):
            raise Refused('invalid_params')
        folder = self.root / job_id
        view = self.read(folder)
        if method == 'job.status':
            limit = int(p.get('limit') or 0) or 64 << 10
            offset = p.get('offset')
            return {'job': view, 'log': self.log(folder, -1 if offset is None else max(0, int(offset)), min(limit, 256 << 10))}
        if method == 'job.stop':
            return await self.stop(folder, view)
        raise Refused('unsupported_method')

    async def start(self, p):
        command, cwd, stdin = p.get('command'), p.get('cwd') or '', p.get('stdin') or ''
        if not isinstance(command, str) or not command.strip() or len(command) > 64 << 10 or len(stdin) > 1 << 20:
            raise Refused('invalid_params')
        if cwd and (not os.path.isabs(cwd) or not os.path.isdir(cwd)):
            raise Refused('invalid_cwd')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sum(v['state'] == 'running' for v in self.views()) >= 20:
            raise Refused('too_many_jobs')
        started = now_ms()
        spec = {'id': _base36(started) + secrets.token_hex(4), 'command': command, **({'cwd': cwd} if cwd else {}), 'shell': login_shell(),
                'startedAt': started, 'logLimit': 20 << 20, **({'stdin': True} if stdin else {})}
        folder = self.root / spec['id']
        folder.mkdir(mode=0o700)
        (folder / 'job.json').write_text(json.dumps(spec))
        (folder / 'env.json').write_text(json.dumps(shell_env(self.environ, spec['shell'])))
        if stdin:
            (folder / 'stdin').write_text(stdin)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', RUNNER, str(folder), stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        task = asyncio.get_running_loop().create_task(proc.wait())
        _runners.add(task)
        task.add_done_callback(_runners.discard)
        return self.read(folder)

    def views(self):
        out = []
        if self.root.is_dir():
            for folder in self.root.iterdir():
                if folder.is_dir() and JOB_ID.match(folder.name):
                    with contextlib.suppress(Refused):
                        out.append(self.read(folder))
        return sorted(out, key=lambda v: v['startedAt'])

    @staticmethod
    def _pid(folder, name):
        try:
            pid = int((folder / name).read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return 0, False
        return pid, _alive(pid)

    def read(self, folder):
        try:
            spec = json.loads((folder / 'job.json').read_text())
        except FileNotFoundError:
            raise Refused('job_not_found') from None
        except (OSError, ValueError):
            raise Refused('job_unreadable') from None
        view = {'id': spec['id'], 'command': spec['command'], **({'cwd': spec['cwd']} if spec.get('cwd') else {}), 'shell': spec['shell'],
                'state': 'running', 'exitCode': None, 'startedAt': spec['startedAt']}
        stopped = (folder / 'stop').exists()

        def finish():
            try:
                done = json.loads((folder / 'exit').read_text())
            except (OSError, ValueError):
                return False
            view.update(state='stopped' if stopped else 'exited', exitCode=done['exitCode'], endedAt=done['endedAt'])
            if done.get('signal'):
                view['signal'] = done['signal']
            return True
        if not finish():
            pid, alive = self._pid(folder, 'pid')
            if pid:
                view['pid'] = pid
            if not (alive or not (folder / 'pid').exists() and now_ms() - spec['startedAt'] < 30000) and not finish():
                view['state'] = 'stopped' if stopped else 'interrupted'
        view['durationMs'] = (view['endedAt'] if 'endedAt' in view else now_ms()) - view['startedAt']
        keys = ('id', 'command', 'cwd', 'shell', 'state', 'exitCode', 'signal', 'startedAt', 'endedAt', 'durationMs', 'pid')
        return {k: view[k] for k in keys if k in view}

    @staticmethod
    def log(folder, offset, limit):
        try:
            data = (folder / 'log').read_bytes()
        except OSError:
            data = b''
        size, tail = len(data), offset < 0
        if tail:
            offset = max(0, size - limit)
        offset = min(offset, size)
        part = data[offset:offset + limit]
        if tail or offset > 0:
            skip = 0
            while skip < len(part) and skip < 3 and part[skip] & 0xC0 == 0x80:
                skip += 1
            part, offset = part[skip:], offset + skip
        text = part.decode(errors='ignore')
        part = text.encode()
        return {'data': text, 'offset': offset, 'next': offset + len(part), 'size': size, 'first': 0}

    async def stop(self, folder, view):
        _, orphan = self._pid(folder, 'child')
        if view['state'] != 'running' and not (view['exitCode'] is None and orphan):
            return {'job': view, 'stopped': False}
        (folder / 'stop').write_text('1\n')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.read(folder)['state'] != 'running':
                break
            await asyncio.sleep(0.05)
        view = self.read(folder)
        if view['exitCode'] is None:
            for name in ('child', 'pid'):
                pid, alive = self._pid(folder, name)
                if alive:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pid, signal.SIGKILL)
            view = self.read(folder)
        return {'job': view, 'stopped': True}


# ----- terminals -----
class Terminal:
    """One login shell on a PTY (startShell in node/pty_unix.go)."""

    def __init__(self, node, sid, cols, rows):
        self.node, self.sid, self.cols, self.rows = node, sid, cols, rows
        self.unacked = 0
        self.reading = False
        self.closed = False
        self.proc = None

    async def start(self):
        account = pwd.getpwuid(os.getuid())
        shell, home = account.pw_shell or '/bin/sh', account.pw_dir or '/'
        if not os.access(shell, os.X_OK):
            shell = '/bin/sh'
        self.master, slave = os.openpty()
        self.resize(self.cols, self.rows)
        env = {'TERM': 'xterm-256color', 'HOME': home, 'SHELL': shell,
               **{k: v for k, v in self.node.environ.items() if k not in {'TERM', 'HOME', 'SHELL', 'INVOCATION_ID', 'JOURNAL_STREAM'}}}
        env.setdefault('LANG', 'C.UTF-8')

        def controlling():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        try:
            self.proc = await asyncio.create_subprocess_exec(shell, '-l', cwd=home, env=env, stdin=slave, stdout=slave, stderr=slave,
                                                             start_new_session=True, preexec_fn=controlling)
        finally:
            os.close(slave)
        os.set_blocking(self.master, False)
        self.waiter = asyncio.get_running_loop().create_task(self._exited())

    def _read_on(self):
        if not self.reading and not self.closed:
            asyncio.get_running_loop().add_reader(self.master, self._readable)
            self.reading = True

    def _read_off(self):
        if self.reading:
            asyncio.get_running_loop().remove_reader(self.master)
            self.reading = False

    def _readable(self):
        try:
            data = os.read(self.master, 32 * 1024)
        except BlockingIOError:
            return
        except OSError:
            data = b''
        if not data:
            self._read_off()
            return
        self.unacked += len(data)
        if self.unacked >= OUTPUT_WINDOW:
            self._read_off()
        channel = self.node.channel
        if channel:
            asyncio.get_running_loop().create_task(channel.send(hub_module.KIND_TERMINAL, self.sid.to_bytes(4, 'big') + data))

    def acked(self, n):
        self.unacked = max(0, self.unacked - n)
        if self.unacked < OUTPUT_WINDOW:
            self._read_on()

    def write(self, data):
        if not self.closed:
            with contextlib.suppress(OSError):
                os.write(self.master, data)

    def resize(self, cols, rows):
        with contextlib.suppress(OSError):
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    async def _exited(self):
        await self.proc.wait()
        # What it printed last goes out before the close.
        await asyncio.sleep(0.05)
        self._readable_all()
        await self.close('exited')

    def _readable_all(self):
        while self.reading:
            before = self.unacked
            self._readable()
            if self.unacked == before:
                break

    async def close(self, reason, tell=True):
        if self.closed:
            return
        self.closed = True
        self._read_off()
        self.node.terminals.pop(self.sid, None)
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGHUP)
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                await self.proc.wait()
        with contextlib.suppress(OSError):
            os.close(self.master)
        if tell and self.node.channel:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self.node.send({'t': 'term.closed', 'sid': self.sid, 'reason': reason}, 'msg:term.closed')


# ----- the browser -----
class FakeBrowserHost:
    """browserHost in node/browser_host.go for a browser the node attaches to (an endpoint), run
    by the controller's own Python engine, which test_browser_engine_parity holds to the Go one."""

    CDP_METHODS = {'Target.getTargets', 'Target.createTarget', 'Target.closeTarget', 'Browser.getVersion'}
    TARGET_METHODS = {'Runtime.evaluate', 'Runtime.terminateExecution'}

    def __init__(self, node):
        self.node = node
        self.dir = node.config_path.with_name(node.config_path.name + '.browser')
        self.state, self.source, self.product, self.error, self.epoch = 'stopped', '', '', '', ''
        self.worker = self.cdp = None

    def settings(self):
        return self.node.config.get('browser') or {}

    def status(self):
        out = {'enabled': self.node.caps['browser'], 'state': self.state, 'source': self.source, 'product': self.product,
               'headless': False, 'epoch': self.epoch, 'endpoint': self.settings().get('endpoint', ''), 'download': bool(self.settings().get('download'))}
        if self.error:
            out['error'] = self.error
        return out

    async def call(self, method, p):
        if method == 'browser.config':
            if not self.node.caps['exec']:
                raise Refused('capability_disabled')
            return await self.configure(p)
        if method == 'browser.status':
            return self.status()
        if not self.node.caps['browser']:
            raise Refused('capability_disabled')
        if method == 'browser.start':
            return await self.ensure()
        if method == 'browser.restart':
            await self.stop()
            return await self.ensure()
        if method == 'browser.call':
            return await self.execute(p)
        if method == 'browser.cdp':
            return await self.raw(p)
        if method == 'browser.artifact':
            return self.artifact(p)
        if method == 'browser.upload':
            return self.upload(p)
        raise Refused('unsupported_method')

    async def configure(self, p):
        endpoint = str(p.get('endpoint') or '')
        if endpoint and urlsplit(endpoint).scheme not in {'ws', 'wss', 'http', 'https'}:
            raise Refused('invalid_browser_endpoint')
        before = self.settings()
        config = {**self.node.config, 'capabilities': {**self.node.caps, 'browser': p.get('enabled') is True}}
        config['browser'] = {**({'endpoint': endpoint} if endpoint else {}), **({'download': True} if p.get('download') else {})}
        if not config['browser']:
            config.pop('browser')
        save_config(self.node.config_path, config)
        self.node.config = config
        if config.get('browser', {}) != before or not p.get('enabled'):
            await self.stop()
            if self.state == 'failed':
                self.state, self.error = 'stopped', ''
        # The caps changed: the node says hello again.
        if self.node.channel:
            await self.node.hello()
        return self.status()

    async def ensure(self):
        if self.state == 'ready' and self.cdp and self.cdp.connected:
            return self.status()
        from app.browser.cdp import CDP
        from app.browser.worker import Worker, WorkerError
        endpoint = self.settings().get('endpoint')
        if not endpoint:
            self.state, self.error = 'failed', 'the fake node only attaches to a browser endpoint'
            return self.status()
        self.state, self.error = 'starting', ''
        for name in ('uploads', 'artifacts', 'downloads'):
            shutil.rmtree(self.dir / name, ignore_errors=True)
        (self.dir / 'downloads').mkdir(parents=True, exist_ok=True)
        try:
            self.cdp = CDP(endpoint, 15)
            await self.cdp.connect()
            self.worker = Worker(sys.executable)
            await self.worker.start({'connectTimeoutS': 15, 'snapshotMaxChars': 12000, 'maxEventEntries': 200, 'maxBodyBytes': 2 << 20,
                                     'maxArtifactBytes': 32 << 20, 'endpoint': self.cdp.ws_url, 'downloadHostPath': str(self.dir / 'downloads'),
                                     'downloadBrowserPath': str(self.dir / 'downloads')})
            version = await self.cdp.call('Browser.getVersion', {})
        except (OSError, aiohttp.ClientError, TimeoutError, WorkerError, RuntimeError) as exc:
            await self.stop()
            self.state, self.error = 'failed', str(exc) or type(exc).__name__
            return self.status()
        self.state, self.source, self.product, self.epoch = 'ready', 'endpoint', version.get('product', ''), secrets.token_hex(8)
        return self.status()

    async def stop(self):
        worker, cdp, self.worker, self.cdp = self.worker, self.cdp, None, None
        if worker:
            await worker.close()
        if cdp:
            await cdp.close()
        if self.state == 'ready':
            self.state = 'stopped'

    def _within(self, root, path):
        return isinstance(path, str) and os.path.realpath(path).startswith(str(root.resolve()) + os.sep)

    async def execute(self, p):
        from app.browser.worker import WorkerError
        action, params = p.get('action'), p.get('params')
        if not action or action == 'initialize':
            raise Refused('invalid_params')
        if not self.worker:
            raise Refused('browser_not_started')
        params = dict(params or {})
        if params.get('outputPath') == '*':
            (self.dir / 'artifacts').mkdir(parents=True, exist_ok=True)
            params['outputPath'] = str(self.dir / 'artifacts' / secrets.token_hex(12))
        elif 'outputPath' in params:
            raise Refused('invalid_params')
        for path in params.get('paths') or []:
            if not self._within(self.dir / 'uploads', path):
                raise Refused('upload_path_outside_node')
        try:
            result = await self.worker.call(action, params, timeout=float(params.get('timeoutMs') or 30000) / 1000 + 5)
        except WorkerError as exc:
            return {'error': exc.code, 'outcome': exc.outcome, 'details': exc.details or {}}
        return {'result': result}

    async def raw(self, p):
        method, params, target = p.get('method'), p.get('params') or {}, p.get('target') or ''
        if not target and method not in self.CDP_METHODS or target and method not in self.TARGET_METHODS:
            raise Refused('method_not_forwarded')
        if not self.cdp:
            raise Refused('browser_not_started')
        try:
            result = await (self.cdp.target_call(target, method, params, timeout=10) if target else self.cdp.call(method, params))
        except (ConnectionError, aiohttp.ClientError):
            return {'error': 'browser_connection_lost', 'epoch': self.epoch}
        except Exception as exc:
            return {'error': str(exc)[:200] or 'cdp_error', 'epoch': self.epoch}
        return {'result': result, 'epoch': self.epoch}

    def artifact(self, p):
        path, offset, limit = p.get('path'), int(p.get('offset') or 0), int(p.get('limit') or 1)
        if offset < 0 or not self._within(self.dir / 'artifacts', path):
            raise Refused('invalid_params')
        try:
            with open(path, 'rb') as f:
                size = os.fstat(f.fileno()).st_size
                f.seek(offset)
                data = f.read(min(max(limit, 1), 4 << 20))
        except OSError:
            raise Refused('not_found') from None
        eof = offset + len(data) >= size
        if eof:
            os.remove(path)
        return {'data': b64(data), 'eof': eof, 'size': size}

    def upload(self, p):
        upload, name, offset = str(p.get('upload') or ''), os.path.basename(str(p.get('name') or '').replace('\\', '/')), int(p.get('offset') or 0)
        if offset < 0 or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', upload) or name in {'', '.', '/'} or len(name) > 255:
            raise Refused('invalid_params')
        data = base64.b64decode(p.get('data') or '')
        folder = self.dir / 'uploads' / upload
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with open(path, 'r+b' if offset else 'wb') if path.exists() or not offset else open(path, 'wb') as f:
            if f.seek(0, 2) != offset:
                raise Refused('upload_offset_mismatch')
            f.write(data)
        return {'path': str(path)} if p.get('done') else {}
