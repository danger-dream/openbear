"""Servers that host the gateway and instances. The local host runs commands
directly; remote servers use their node when it is online, else SSH with a
password or a vault key reference."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import getpass
import json
import os
import re
import signal
import tempfile
import time
import uuid
from pathlib import Path

from app.instances import keys
from app.instances.contracts import InstanceError
from app.instances.security import Scrubber
from app.webhooks.repository import many, one

LOCAL = 'local'
# The node method that makes HTTP requests on its own loopback.
HTTP_FEATURE = 'http.local'
PUBLIC_IP_SOURCES = ('https://api.ip.sb/ip', 'https://4.icanhazip.com', 'https://ipv4.icanhazip.com')
_IPV4 = re.compile(r'(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}')
_HOST = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9.:-]*')
_USER = re.compile(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]*')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS instance_servers (
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL, public_json TEXT NOT NULL, protected TEXT NOT NULL,
 created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
)'''
# One probe reports what the UI needs: OS, Docker, public IP and listening ports.
PROBE = r'''
. /etc/os-release 2>/dev/null
[ -z "${PRETTY_NAME:-}" ] && command -v sw_vers >/dev/null 2>&1 && PRETTY_NAME="macOS $(sw_vers -productVersion)"
echo "os=${PRETTY_NAME:-$(uname -s)}"
echo "system=$(uname -s)"
echo "docker=$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
for u in https://api.ip.sb/ip https://4.icanhazip.com; do
  ip=$( (curl -4fsS --max-time 6 "$u" || wget -4qO- -T 6 "$u") 2>/dev/null | tr -d '[:space:]'); [ -n "$ip" ] && break
done; echo "ip=$ip"
'''
# Windows OpenSSH runs commands with cmd (or PowerShell when configured); an
# encoded PowerShell command reads the same under both.
_WINDOWS_PROBE = r'''
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
"os=" + (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').ProductName
"system=Windows"
$docker = if (Get-Command docker -ErrorAction SilentlyContinue) { docker version --format '{{.Server.Version}}' 2>$null }
"docker=$docker"
$ip = ''
foreach ($u in 'https://api.ip.sb/ip', 'https://4.icanhazip.com') {
  try { $ip = ([string](Invoke-RestMethod -UseBasicParsing -TimeoutSec 6 $u)).Trim(); if ($ip) { break } } catch {}
}
"ip=$ip"
'''
WINDOWS_PROBE = ('powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand '
                 + base64.b64encode(_WINDOWS_PROBE.encode('utf-16-le')).decode())
SYSTEMS = {'Linux': 'linux', 'Darwin': 'darwin', 'Windows': 'windows'}
PORT_CHECK = r'''
p=%d
if command -v ss >/dev/null 2>&1; then
  line=$(ss -Hltnp "sport = :$p" 2>/dev/null | head -1)
  [ -z "$line" ] && { echo free; exit 0; }
  name=$(printf '%%s' "$line" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p'); echo "used=${name:-unknown}"
elif command -v netstat >/dev/null 2>&1; then
  netstat -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$p\$" && echo used=unknown || echo free
else echo unknown; fi
'''


def now():
    return int(time.time() * 1000)


def _parse_probe(text):
    values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    ip = values.get('ip', '').strip()
    return {'os': values.get('os', '')[:120], 'docker': values.get('docker', '')[:40], 'publicIp': ip if _IPV4.fullmatch(ip) else '',
            'platform': SYSTEMS.get(values.get('system', '').strip(), '')}


class Servers:
    def __init__(self, service):
        self.service = service
        self.local_ip = None
        self.local_ip_at = 0

    @property
    def db(self):
        return self.service.db

    async def migrate(self):
        async with self.db.write_transaction(label='instance-servers-schema') as conn:
            await conn.execute(SCHEMA)

    # ----- registry -----
    async def _rows(self, owner):
        return await many(self.db.conn, 'SELECT * FROM instance_servers WHERE owner=? ORDER BY created_ms', (owner,))

    async def usage(self, owner):
        """Which gateway/instances run on each server, for display and deletion guards."""
        used = {}
        value, _ = await self.service.gateway.config()
        if value.get('enabled') and value.get('serverId'):
            used.setdefault(value['serverId'], []).append('访问网关')
        for row in await many(self.db.conn, "SELECT public_json FROM managed_instances WHERE owner=? AND status!='archived'", (owner,)):
            item = json.loads(row['public_json'])
            if item.get('serverId'):
                used.setdefault(item['serverId'], []).append(item.get('name', ''))
        return used

    def public(self, row, used):
        item = json.loads(row['public_json'])
        return {**item, 'id': row['id'], 'usedBy': used.get(row['id'], [])}

    async def _local_ip(self, *, force=False):
        # Failures are retried after a while instead of being cached forever.
        if force or self.local_ip is None or not self.local_ip and time.time() - self.local_ip_at > 600:
            self.local_ip, self.local_ip_at = await self._fetch_local_ip(), time.time()
        return self.local_ip

    async def local(self, used):
        await self._local_ip()
        return {'id': LOCAL, 'name': '本机', 'local': True, 'host': '', 'port': 0, 'username': '', 'auth': 'local',
                'publicIp': self.local_ip or '', 'usedBy': used.get(LOCAL, []), 'status': 'ok'}

    async def list(self, owner):
        """Servers that can host the gateway or instances: local, SSH and Linux node ones."""
        used = await self.usage(owner)
        channels = await self._channels(owner)
        items = [await self.local(used)] + [self.public(r, used) for r in await self._rows(owner)]
        for item in items:
            if item['id'] in channels:
                item['node'] = channels[item['id']]
        return {'ok': True, 'items': [i for i in items if i.get('auth') != 'none' or i.get('node')]}

    async def _channels(self, owner, identity=None):
        nodes = getattr(self.service.host, 'servers', None)
        return await nodes.node_channels(owner, identity) if nodes else {}

    async def all(self, owner):
        used = await self.usage(owner)
        return [await self.local(used)] + [self.public(r, used) for r in await self._rows(owner)]

    async def get(self, owner, identity):
        if identity == LOCAL:
            return await self.local(await self.usage(owner))
        row = await one(self.db.conn, 'SELECT * FROM instance_servers WHERE id=? AND owner=?', (identity, owner))
        if not row:
            raise InstanceError('server_not_found', 404)
        item = self.public(row, await self.usage(owner))
        if channel := (await self._channels(owner, identity)).get(identity):
            item['node'] = channel
        return item

    def _validate(self, data, prior):
        if set(data) - {'name', 'host', 'port', 'username', 'auth', 'password', 'privateKey', 'keyRef', 'saveKey'}:
            raise InstanceError('invalid_server')
        merged = {**prior, **{k: v for k, v in data.items() if k not in {'password', 'privateKey', 'keyRef', 'saveKey'}}}
        name, host, username = merged.get('name', ''), merged.get('host', ''), merged.get('username', '')
        port = merged.get('port', 22)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise InstanceError('server_name_required')
        if merged.get('auth') == 'none':
            # Monitored through its node only; no SSH connection is recorded.
            return {'name': name.strip(), 'host': '', 'port': 0, 'username': '', 'auth': 'none'}
        if not isinstance(host, str) or not _HOST.fullmatch(host) or '..' in host or len(host) > 253:
            raise InstanceError('invalid_server_host')
        if type(port) is not int or not 1 <= port <= 65535:
            raise InstanceError('invalid_server_port')
        if not isinstance(username, str) or not _USER.fullmatch(username) or len(username) > 64:
            raise InstanceError('invalid_server_user')
        if merged.get('auth') not in {'key', 'password', 'upload'}:
            raise InstanceError('invalid_server_auth')
        return {'name': name.strip(), 'host': host, 'port': port, 'username': username, 'auth': merged['auth']}

    async def save(self, owner, data, identity=None):
        self.service.require_controller()
        if identity == LOCAL:
            raise InstanceError('local_server_fixed', 409)
        row = None
        if identity:
            row = await one(self.db.conn, 'SELECT * FROM instance_servers WHERE id=? AND owner=?', (identity, owner))
            if not row:
                raise InstanceError('server_not_found', 404)
        prior = json.loads(row['public_json']) if row else {}
        secret = self.service.vault.open(row['protected']) if row else {}
        item = self._validate(data, prior)
        auth = item['auth']
        if auth == 'none':
            if row and prior.get('auth') != 'none' and (await self.usage(owner)).get(identity):
                raise InstanceError('server_in_use', 409)
            identity = identity or str(uuid.uuid4())
            item['keyLabel'] = ''
            async with self.db.write_transaction(label='instance-server-save') as conn:
                if row:
                    await conn.execute('UPDATE instance_servers SET public_json=?,protected=?,updated_ms=? WHERE id=?', (json.dumps(item, ensure_ascii=False), self.service.vault.seal({}), now(), identity))
                else:
                    await conn.execute('INSERT INTO instance_servers VALUES(?,?,?,?,?,?)', (identity, owner, json.dumps(item, ensure_ascii=False), self.service.vault.seal({}), now(), now()))
            return {'ok': True, 'item': await self.get(owner, identity)}
        if auth == 'key' and ('keyRef' in data or not secret.get('keyRef')):
            ref = data.get('keyRef')
            if not isinstance(ref, dict) or set(ref) - {'secret', 'field', 'passphraseField'}:
                raise InstanceError('ssh_key_reference_required')
            await keys.resolve(self.db, ref)
            secret = {'keyRef': {k: ref[k] for k in ('secret', 'field', 'passphraseField') if ref.get(k)}}
        elif auth == 'password' and ('password' in data or not secret.get('password')):
            if not isinstance(data.get('password'), str) or not data['password'] or len(data['password']) > 8192:
                raise InstanceError('ssh_password_required')
            secret = {'password': data['password']}
        elif auth == 'upload' and ('privateKey' in data or not secret.get('privateKey')):
            pem = keys.normalize(data.get('privateKey'))
            if not pem or not (await keys.inspect(pem)).get('unlocked'):
                raise InstanceError('ssh_private_key_invalid')
            if data.get('saveKey') is True:
                ref = await self._store_uploaded_key(item['name'], pem)
                item['auth'], secret = 'key', {'keyRef': ref}
            else:
                secret = {'privateKey': pem}
        stored = self.service.vault.open(row['protected']) if row else {}
        if row and all(prior.get(k) == item[k] for k in ('host', 'port')) and stored.get('knownHosts'):
            secret.setdefault('knownHosts', stored['knownHosts'])  # Keep the pinned host key.
        item['keyLabel'] = self._key_label(secret)
        item.update({k: prior[k] for k in ('publicIp', 'os', 'docker', 'status', 'checkedAtMs', 'error') if k in prior})
        target = {**item, **secret}
        if not row or any(prior.get(k) != item.get(k) for k in ('host', 'port', 'username', 'auth')) or secret != stored:
            # Saving always proves the connection once, as the UI promises.
            result = await self.test_target(target, secret.get('knownHosts', ''))
            item.update(result['facts'])
            secret['knownHosts'] = result['knownHosts']
        identity = identity or str(uuid.uuid4())
        sealed = self.service.vault.seal(secret)
        async with self.db.write_transaction(label='instance-server-save') as conn:
            if row:
                await conn.execute('UPDATE instance_servers SET public_json=?,protected=?,updated_ms=? WHERE id=?', (json.dumps(item, ensure_ascii=False), sealed, now(), identity))
            else:
                await conn.execute('INSERT INTO instance_servers VALUES(?,?,?,?,?,?)', (identity, owner, json.dumps(item, ensure_ascii=False), sealed, now(), now()))
        return {'ok': True, 'item': await self.get(owner, identity)}

    async def secret_for_test(self, data):
        """Connection material for an unsaved form; nothing is stored."""
        auth = data.get('auth')
        if auth == 'key':
            ref = data.get('keyRef')
            if not isinstance(ref, dict):
                raise InstanceError('ssh_key_reference_required')
            await keys.resolve(self.db, ref)
            return {'keyRef': ref}
        if auth == 'password' and isinstance(data.get('password'), str) and data['password']:
            return {'password': data['password']}
        if auth == 'upload':
            pem = keys.normalize(data.get('privateKey'))
            if pem:
                return {'privateKey': pem}
        raise InstanceError('server_credential_required')

    @staticmethod
    def _key_label(secret):
        if secret.get('keyRef'):
            return f"{secret['keyRef']['secret']} / {secret['keyRef']['field']}"
        return '上传的私钥' if secret.get('privateKey') else '密码'

    async def _store_uploaded_key(self, server_name, pem):
        from app.memory.builtin import BuiltinMemoryClient
        name = 'ssh-' + re.sub(r'[^a-zA-Z0-9_-]+', '-', server_name).strip('-').lower()[:40] or 'ssh-key'
        client = BuiltinMemoryClient(self.db, identity=self.service.host.config.memory.identity)
        taken = {i['name'] for i in (await client.tool_call('secret', {'action': 'list', 'includeArchived': True})).get('items', [])}
        base, n = name, 2
        while name in taken:
            name, n = f'{base}-{n}', n + 1
        result = await client.tool_call('secret', {'action': 'set', 'name': name, 'note': f'SSH 私钥（服务器「{server_name}」）',
                                                   'kvJson': json.dumps([{'key': '私钥', 'value': pem}], ensure_ascii=False)})
        if not result.get('ok'):
            raise InstanceError('ssh_key_vault_save_failed', 503)
        return {'secret': name, 'field': '私钥'}

    async def delete(self, owner, identity):
        if identity == LOCAL:
            raise InstanceError('local_server_fixed', 409)
        await self.get(owner, identity)
        if (await self.usage(owner)).get(identity):
            raise InstanceError('server_in_use', 409)
        async with self.db.write_transaction(label='instance-server-delete') as conn:
            await conn.execute('DELETE FROM instance_servers WHERE id=? AND owner=?', (identity, owner))
        return {'ok': True}

    # ----- execution -----
    async def target(self, owner, identity, *, nodes=False):
        """Fully resolved connection material, read at use time (vault keys may change).
        nodes=True (instances, gateway): an online Linux node with command execution is
        used before SSH. Installing the node itself always goes over SSH."""
        if identity == LOCAL:
            return {'id': LOCAL, 'local': True, 'channel': 'local'}
        row = await one(self.db.conn, 'SELECT * FROM instance_servers WHERE id=? AND owner=?', (identity, owner))
        if not row:
            raise InstanceError('server_not_found', 404)
        item, secret = json.loads(row['public_json']), self.service.vault.open(row['protected'])
        channel = (await self._channels(owner, identity)).get(identity) if nodes else None
        if channel and channel['online'] and channel['exec']:
            return {'id': identity, 'owner': owner, 'local': False, 'channel': 'node', **item, 'nodeAddress': channel['address']}
        if item.get('auth') == 'none':
            if channel:
                raise InstanceError('node_exec_disabled' if channel['online'] else 'node_offline', 409)
            raise InstanceError('server_ssh_unavailable', 409)
        return {'id': identity, 'local': False, 'channel': 'ssh', **item, **secret}

    async def channel(self, owner, identity):
        """How instance and gateway commands reach the server right now, and as whom."""
        try:
            target = await self.target(owner, identity, nodes=True)
        except InstanceError as exc:
            return {'channel': 'unavailable', 'error': exc.error}
        if target['channel'] == 'local':
            return {'channel': 'local', 'runAs': getpass.getuser()}
        return {'channel': target['channel'], 'runAs': 'root' if target['channel'] == 'node' else target['username']}

    # ----- HTTP on a server's loopback, through its node -----
    async def http_ready(self, owner, identity):
        """Whether the controller reaches services on this server's 127.0.0.1 through its node:
        enrolled, online, running commands (the same consent) and new enough for http.local."""
        return not await self.http_blocker(owner, identity)

    async def http_blocker(self, owner, identity):
        """'' when http_ready, else why not: node_missing (no enrolled node: SSH only),
        node_offline, node_exec_disabled or node_upgrade_required."""
        nodes = getattr(self.service.host, 'servers', None)
        if identity == LOCAL or not nodes or identity not in await self._channels(owner, identity):
            return 'node_missing'
        session = nodes.hub.sessions.get(identity)
        if not session:
            # As last recorded: an offline node known to be too old needs upgrading either way.
            row = await one(self.db.conn, 'SELECT info_json FROM server_nodes WHERE owner=? AND server_id=?', (owner, identity))
            features = json.loads(row['info_json']).get('features') if row else None
            return 'node_upgrade_required' if isinstance(features, list) and HTTP_FEATURE not in features else 'node_offline'
        if not session.caps.get('exec'):
            return 'node_exec_disabled'
        if session.incompatible or HTTP_FEATURE not in session.features:
            return 'node_upgrade_required'
        return ''

    async def local_http(self, owner, identity, method, port, path, *, headers, body=b'', timeout=20):
        """(status, body) of one request the node makes to 127.0.0.1:port on its own machine."""
        params = {'method': method, 'port': port, 'path': path, 'headers': headers,
                  'body': base64.b64encode(body).decode(), 'timeoutMs': int(timeout * 1000)}
        result = await self.service.host.servers._call_newer(owner, identity, HTTP_FEATURE, 'http.local', params, timeout=timeout + 10)
        try:
            status, raw = result['status'], base64.b64decode(result.get('body') or '', validate=True)
        except (KeyError, TypeError, ValueError):
            raise InstanceError('node_invalid_reply', 502) from None
        if type(status) is not int:
            raise InstanceError('node_invalid_reply', 502)
        return status, raw

    async def mesh_address(self, owner, identity):
        """The server's address in the applied mesh, '' when it is not an active member."""
        mesh = getattr(getattr(self.service.host, 'servers', None), 'mesh', None)
        if not mesh:
            return ''
        from app.servers import mesh_model
        doc = await mesh._load(owner, 'applied')
        return next((m['address'] for m in mesh_model.active(doc) if m['serverId'] == identity and m.get('address')), '')

    async def remember_host_key(self, identity, known):
        if identity == LOCAL or not known:
            return
        row = await one(self.db.conn, 'SELECT protected FROM instance_servers WHERE id=?', (identity,))
        if not row:
            return
        secret = self.service.vault.open(row['protected'])
        if secret.get('knownHosts') == known:
            return
        secret['knownHosts'] = known
        async with self.db.write_transaction(label='instance-server-host-key') as conn:
            await conn.execute('UPDATE instance_servers SET protected=? WHERE id=?', (self.service.vault.seal(secret), identity))

    async def run(self, target, command, *, stdin=b'', timeout=120, sudo=False, root=False, source='instance'):
        """Run on the target. Returns the same result shape for local, node and SSH.
        root=True runs through non-interactive sudo for a non-root SSH user."""
        if target.get('channel') == 'node':
            # The node runs as root: sudo and root need no wrapping.
            return await self.service.host.servers.run_command(target['owner'], target['id'], command, stdin=stdin,
                                                               timeout=timeout, source=source)
        if root and (os.geteuid() != 0 if target.get('local') else target.get('username') != 'root'):
            command = 'sudo -n -- sh -c ' + _quote(command)
        if target.get('local'):
            result = await _process(['/bin/sh', '-c', command], stdin=stdin, timeout=timeout, env=_local_env())
            result['knownHosts'] = ''
            return result
        if sudo:
            if not target.get('password') or '\n' in target['password']:
                raise InstanceError('sudo_password_unavailable')
            stdin = (target['password'] + '\n').encode()
            command = "sudo -S -p '' -- sh -c " + _quote(command)
        result = await _ssh(self.db, target, command, stdin=stdin, timeout=timeout)
        await self.remember_host_key(target['id'], result['knownHosts'])
        return result

    async def test_target(self, target, known=''):
        result = await _ssh(self.db, {**target, 'knownHosts': known or target.get('knownHosts', '')}, PROBE, timeout=40)
        if not (result['ok'] and 'system=' in result['stdout']):
            windows = await _ssh(self.db, {**target, 'knownHosts': result['knownHosts']}, WINDOWS_PROBE, timeout=60)
            if windows['ok'] and 'system=Windows' in windows['stdout']:
                result = windows
        if not result['ok']:
            raise InstanceError('server_connection_failed' if not result['timedOut'] else 'server_connection_timeout', 502)
        facts = _parse_probe(result['stdout'])
        return {'facts': {**facts, 'status': 'ok', 'checkedAtMs': now(), 'error': ''}, 'knownHosts': result['knownHosts']}

    async def platform(self, target):
        """linux, darwin or windows: the probed fact, else asked now (logins saved before it was recorded)."""
        if target.get('local'):
            return 'linux'
        if target.get('platform') in SYSTEMS.values():
            return target['platform']
        result = await self.run(target, 'uname -s', timeout=60)
        if result['ok'] and result['stdout'].strip() in SYSTEMS:
            return SYSTEMS[result['stdout'].strip()]
        result = await self.run(target, WINDOWS_PROBE, timeout=60)
        return 'windows' if result['ok'] and 'system=Windows' in result['stdout'] else 'linux'

    async def check(self, owner, identity):
        """Refresh connection facts (OS, Docker, public IP)."""
        if identity == LOCAL:
            await self._local_ip(force=True)
            result = await _process(['/bin/sh', '-c', PROBE], timeout=40, env=_local_env())
            return {'ok': True, 'item': {**await self.local(await self.usage(owner)), **{k: v for k, v in _parse_probe(result['stdout']).items() if k != 'publicIp'}}}
        row = await one(self.db.conn, 'SELECT * FROM instance_servers WHERE id=? AND owner=?', (identity, owner))
        if not row:
            raise InstanceError('server_not_found', 404)
        item, secret = json.loads(row['public_json']), self.service.vault.open(row['protected'])
        if item.get('auth') == 'none':
            raise InstanceError('server_ssh_unavailable', 409)
        try:
            result = await self.test_target({**item, **secret})
            item.update(result['facts'])
            secret['knownHosts'] = result['knownHosts']
        except InstanceError as exc:
            item.update(status='unreachable', error=exc.error, checkedAtMs=now())
        async with self.db.write_transaction(label='instance-server-check') as conn:
            await conn.execute('UPDATE instance_servers SET public_json=?,protected=? WHERE id=?', (json.dumps(item, ensure_ascii=False), self.service.vault.seal(secret), identity))
        return {'ok': True, 'item': await self.get(owner, identity)}

    async def port(self, owner, identity, port):
        """Whether a TCP port is free on the target, as the gateway/instance will need it."""
        if type(port) is not int or not 1 <= port <= 65535:
            raise InstanceError('invalid_port')
        target = await self.target(owner, identity, nodes=True)
        result = await self.run(target, PORT_CHECK % port, timeout=30)
        if not result['ok']:
            raise InstanceError('server_connection_failed', 502)
        line = result['stdout'].strip().splitlines()[-1:] or ['unknown']
        if line[0] == 'free':
            return {'ok': True, 'free': True}
        if line[0].startswith('used='):
            return {'ok': True, 'free': False, 'usedBy': line[0][5:][:60]}
        return {'ok': True, 'free': None}

    async def public_ip(self, owner, identity):
        if identity == LOCAL:
            return await self._local_ip() or ''
        item = await self.get(owner, identity)
        return item.get('publicIp') or (item.get('node') or {}).get('publicIp', '')

    async def _fetch_local_ip(self):
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as http:
                for url in PUBLIC_IP_SOURCES:
                    try:
                        async with http.get(url, headers={'User-Agent': 'curl/8.4.0'}) as response:
                            text = (await response.text()).strip()
                            if response.status == 200 and _IPV4.fullmatch(text):
                                return text
                    except (aiohttp.ClientError, TimeoutError, UnicodeError):
                        continue
        except Exception:
            pass
        return ''


def _quote(value):
    import shlex
    return shlex.quote(value)


def _local_env():
    from app.managed.settings import sanitized_env
    return {**sanitized_env(), 'PATH': os.environ.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')}


async def _process(args, *, stdin=b'', timeout=120, env=None):
    try:
        process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True)
    except OSError:
        raise InstanceError('command_unavailable', 503) from None

    async def read(stream):
        output, truncated = bytearray(), False
        while chunk := await stream.read(65536):
            if len(output) < 131072:
                output.extend(chunk[:131072 - len(output)])
            else:
                truncated = True
        return output.decode('utf-8', errors='replace'), truncated

    async def feed():
        try:
            process.stdin.write(stdin)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()
    readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr)), asyncio.create_task(feed())]
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout)
    except TimeoutError:
        timed_out = True
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
    values = await asyncio.gather(*readers, return_exceptions=True)
    stdout, stderr = values[0], values[1]
    return {'ok': process.returncode == 0 and not timed_out, 'exitCode': process.returncode, 'timedOut': timed_out,
            'effectState': 'unknown' if timed_out else 'observed', 'stdout': stdout[0], 'stderr': stderr[0],
            'truncated': stdout[1] or stderr[1]}


async def _ssh(db, target, command, *, stdin=b'', timeout=120):
    """Fixed target only: no user SSH config, agent, forwarding or proxy command."""
    secrets_seen = [target.get('password', ''), target.get('privateKey', '')]
    with tempfile.TemporaryDirectory(prefix='openbear-ssh-') as directory:
        root = Path(directory)
        known = root / 'known_hosts'
        known.write_text(target.get('knownHosts', ''))
        known.chmod(0o600)
        askpass = root / 'askpass'
        askpass.write_text('#!/bin/sh\nexec /usr/bin/printenv OPENBEAR_SSH_SECRET\n')
        askpass.chmod(0o700)
        args = ['ssh', '-F', '/dev/null', '-T', '-p', str(target['port']), '-l', target['username'],
                '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile=' + str(known),
                '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=10',
                '-o', 'ServerAliveCountMax=2', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                '-o', 'PermitLocalCommand=no', '-o', 'ProxyCommand=none', '-o', 'ClearAllForwardings=yes',
                '-o', 'NumberOfPasswordPrompts=1']
        secret = target.get('password', '')
        pem = target.get('privateKey', '')
        if target.get('keyRef'):
            pem, secret, _ = await keys.resolve(db, target['keyRef'])
            secrets_seen += [pem, secret]
        if pem:
            key = root / 'identity'
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(pem)
            args += ['-i', str(key), '-o', 'PasswordAuthentication=no']
        else:
            args += ['-o', 'PubkeyAuthentication=no']
        args += ['--', target['host'], command]
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(root), 'DISPLAY': ':0', 'SSH_ASKPASS': str(askpass),
               'SSH_ASKPASS_REQUIRE': 'force', 'OPENBEAR_SSH_SECRET': secret}
        result = await _process(args, stdin=stdin, timeout=timeout, env=env)
        result['knownHosts'] = known.read_text()
    scrub = Scrubber([s for s in secrets_seen if s])
    for field in ('stdout', 'stderr'):
        result[field] = scrub(result[field])
    return result
