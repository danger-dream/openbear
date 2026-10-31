"""Adding servers: access points, one-time install commands, reusable
registration codes, and installs over SSH that report each step."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time

import aiohttp

from app.instances.contracts import InstanceError
from app.instances.security import Scrubber
from app.instances.servers import LOCAL
from app.servers import compat, install
from app.servers.hub import (
    CLOSE_REPLACED,
    CONTROLLER_OUTDATED,
    NODE_OUTDATED,
    clean_info,
    protocol_range,
)
from app.servers.reminders import quoted
from app.webhooks.repository import many, one

CODE_TTL_MS = 30 * 60 * 1000
ACCESS_LIMIT = 10
BATCH_LIMIT = 50
ONLINE_WAIT = 30
STEPS = (('check', '连接并检查系统'), ('download', '下载节点程序'), ('enroll', '注册节点'), ('service', '启动服务'), ('online', '等待上线'))
_ID = re.compile(r'[a-z0-9-]{1,40}')
# The built-in access point over the mesh: worked out by the controller, never stored with the user's ones.
MESH_ACCESS = 'ap-mesh'
MESH_ACCESS_NAME = '组网'
# Optional node feature: it takes a plain-http control address in 100.64.0.0/10, the mesh's.
# Older nodes refuse one (invalid_url), so they never get it.
MESH_ACCESS_FEATURE = 'access.mesh'


class MachineTaken(InstanceError):
    """The machine already runs a node other than the one being installed: nothing was changed there."""

    def __init__(self, error, node, name, message):
        super().__init__(error, 409)
        self.node, self.name, self.message = node, name, message


def now_ms():
    return int(time.time() * 1000)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def digest(code):
    return hashlib.sha256(code.encode()).hexdigest()


def clean_access(points, default, check):
    """Validate the access point list; `check` validates and normalises a URL."""
    if not isinstance(points, list) or len(points) > ACCESS_LIMIT:
        raise InstanceError('invalid_access_points')
    out, seen = [], set()
    for point in points:
        if not isinstance(point, dict) or set(point) - {'id', 'name', 'url'}:
            raise InstanceError('invalid_access_points')
        name = point.get('name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 30:
            raise InstanceError('access_point_name_required')
        url = check(point.get('url'))
        if not url:
            raise InstanceError('access_point_url_required')
        ident = point.get('id') or 'ap-' + secrets.token_hex(3)
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or ident in seen or ident == MESH_ACCESS:
            raise InstanceError('invalid_access_points')
        seen.add(ident)
        out.append({'id': ident, 'name': name.strip(), 'url': url})
    if default not in seen:
        default = out[0]['id'] if out else ''
    return out, default


def uses_mesh(profile):
    chosen = (profile or {}).get('access') or {}
    return MESH_ACCESS in (chosen.get('primary'), chosen.get('backup'))


def resolve(settings, profile, mesh=None):
    """(primary, backup) access points for a server; backup may be None. mesh is the mesh
    access point when this server can use it now (the controller and it both in the applied
    mesh, its node with MESH_ACCESS_FEATURE); otherwise a primary on it falls back to the default and a backup on it is none."""
    points = {p['id']: p for p in settings['accessPoints']}
    if mesh:
        points[MESH_ACCESS] = mesh
    chosen = (profile or {}).get('access') or {}
    primary = points.get(chosen.get('primary')) or points.get(settings['defaultAccess'])
    backup = points.get(chosen.get('backup'))
    if backup and primary and backup['id'] == primary['id']:
        backup = None
    return primary, backup


class Onboarding:
    def __init__(self, service):
        self.service = service
        self.batches = {}

    @property
    def db(self):
        return self.service.db

    @property
    def installs(self):
        return self.service.installs

    async def addresses(self, owner, server_id):
        """(url, backup url) a server's node uses; the local node always uses loopback."""
        if server_id == LOCAL:
            return self.service.local_url(), ''
        settings = await self.service.settings()
        profile = (await self.service._profiles(owner)).get(server_id, {})
        primary, backup = resolve(settings, profile, await self.service.mesh_point(owner, server_id))
        if not primary:
            raise InstanceError('node_url_required', 409)
        return primary['url'], backup['url'] if backup else ''

    # ----- access points -----
    def fingerprint(self):
        return hashlib.sha256(base64.b64decode(self.service.identity_public)).hexdigest()[:16]

    async def test_access(self, url):
        """From the controller, check that an address reaches this controller over valid TLS."""
        from app.servers.service import node_url
        base = node_url(url)
        if not base:
            raise InstanceError('access_point_url_required')
        started = time.monotonic()
        try:
            async with self.service.http.get(base + '/api/server-nodes/v1/hello', timeout=aiohttp.ClientTimeout(total=12),
                                             allow_redirects=False) as response:
                data = await response.json(content_type=None) if response.status == 200 else None
        except aiohttp.ClientConnectorCertificateError:
            return {'ok': False, 'error': 'access_tls_invalid'}
        except aiohttp.ClientSSLError:
            return {'ok': False, 'error': 'access_tls_invalid'}
        except (aiohttp.ClientError, TimeoutError):
            return {'ok': False, 'error': 'access_unreachable'}
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get('service') != 'openbear':
            return {'ok': False, 'error': 'access_path_not_forwarded'}
        if data.get('controller') != self.fingerprint():
            return {'ok': False, 'error': 'access_other_controller'}
        return {'ok': True, 'latencyMs': round((time.monotonic() - started) * 1000)}

    # ----- one-time install commands -----
    async def new_code(self, owner, server_id):
        code = secrets.token_urlsafe(18)
        async with self.db.write_transaction(label='server-enrollment') as conn:
            await conn.execute('DELETE FROM server_enrollments WHERE expires_ms<?', (now_ms(),))
            await conn.execute('INSERT INTO server_enrollments VALUES(?,?,?,?)', (digest(code), owner, server_id, now_ms() + CODE_TTL_MS))
        return code

    async def enrollment(self, owner, server_id, *, browser=False):
        await self.service.registry.get(owner, server_id)
        if browser and server_id == LOCAL:
            raise InstanceError('browser_local_default', 409)
        # A command for a controller with no node build would only fail on the machine it is run on.
        if not (await self.service.ensure_builds()).get('binaries'):
            raise InstanceError('node_binary_missing', 503)
        code = await self.new_code(owner, server_id)
        fingerprint = install.fingerprint(self.service.identity_public)
        # The node id lets the script refuse a machine already running another node; one that took
        # this server over (settle_join) runs under the id it joined with.
        took = await one(self.db.conn, "SELECT json_extract(info_json,'$.nodeId') AS node FROM server_nodes WHERE server_id=? AND owner=?", (server_id, owner))
        node = (took and took['node']) or server_id
        local = install.command(self.service.local_url(), code, browser=browser, node=node, fingerprint=fingerprint) if server_id == LOCAL else ''
        try:
            url, backup = await self.addresses(owner, server_id) if server_id != LOCAL else ('', '')
        except InstanceError:
            url = backup = ''
        return {'ok': True, 'code': code, 'expiresAtMs': now_ms() + CODE_TTL_MS, 'issuedAtMs': now_ms(),
                'command': install.command(url, code, backup=backup, browser=browser, node=node, fingerprint=fingerprint) if url else '', 'localCommand': local,
                'windowsCommand': install.windows_command(url, code, backup=backup, browser=browser, node=node, fingerprint=fingerprint) if url else ''}

    async def enroll(self, data):
        """Public endpoint: a one-time code or a registration code binds a node's public key."""
        if not isinstance(data, dict) or not isinstance(data.get('code'), str) or not isinstance(data.get('publicKey'), str):
            raise InstanceError('invalid_enrollment')
        try:
            key = base64.b64decode(data['publicKey'], validate=True)
        except ValueError:
            raise InstanceError('invalid_public_key') from None
        if len(key) != 32:
            raise InstanceError('invalid_public_key')
        # A node this controller could not work with is turned away before its code is spent;
        # one from before protocol declares none and speaks 1.
        low, high = protocol_range(data)
        if compat.common(compat.PROTOCOL, (low, high)) is None:
            raise InstanceError(NODE_OUTDATED if high < compat.PROTOCOL[0] else CONTROLLER_OUTDATED, 409)
        code_hash = digest(data['code'])
        info = clean_info(data.get('info'))
        machine = str((data.get('info') or {}).get('machineId') or '')[:64] if isinstance(data.get('info'), dict) else ''
        version = str(data.get('version', ''))[:40]
        async with self.db.write_transaction(label='server-enroll') as conn:
            row = await (await conn.execute('SELECT * FROM server_enrollments WHERE code_hash=? AND expires_ms>?', (code_hash, now_ms()))).fetchone()
            if row:
                await conn.execute('DELETE FROM server_enrollments WHERE code_hash=?', (code_hash,))
                server_id, owner = row['server_id'], row['owner']
                exists = server_id == LOCAL or await (await conn.execute('SELECT 1 FROM instance_servers WHERE id=? AND owner=?', (server_id, owner))).fetchone()
                if not exists:
                    raise InstanceError('server_not_found', 404)
                await self._bind(conn, owner, server_id, key, info, machine, version)
                joined = None
            else:
                joined = await self._claim(conn, code_hash)
                # Nodes generate a new key for every enrollment; a known one is refused
                # here, which also rolls the counted use back, before a server is created.
                if await (await conn.execute('SELECT 1 FROM server_nodes WHERE public_key=?', (base64.b64encode(key).decode(),))).fetchone():
                    raise InstanceError('public_key_in_use', 409)
        if joined:
            server_id, owner = await self._join(joined, key, info, machine, version)
        else:
            await self.service.metrics.rebase(server_id)
        await self.service.hub.disconnect(server_id, CLOSE_REPLACED)
        return {'ok': True, 'nodeId': server_id, 'controllerKey': self.service.identity_public}

    @staticmethod
    async def _bind(conn, owner, server_id, key, info, machine, version, extra=None):
        public = base64.b64encode(key).decode()
        # REPLACE would otherwise drop whichever other server holds this key.
        if await (await conn.execute('SELECT 1 FROM server_nodes WHERE public_key=? AND server_id!=?', (public, server_id))).fetchone():
            raise InstanceError('public_key_in_use', 409)
        prior = await (await conn.execute('SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))).fetchone()
        stored = json.loads(prior['info_json']) if prior else {}
        # The new node connects under this server's own id.
        stored.pop('nodeId', None)
        stored.update(info={**info, 'machineId': machine}, version=version, **(extra or {}))
        await conn.execute('INSERT OR REPLACE INTO server_nodes VALUES(?,?,?,?,?,?)',
                           (server_id, owner, public, dumps(stored), now_ms(), 0))

    # ----- reusable registration codes -----
    async def _claim(self, conn, code_hash):
        """Count one use of a registration code, or refuse it."""
        row = await (await conn.execute('SELECT * FROM server_registration_codes WHERE code_hash=?', (code_hash,))).fetchone()
        if (not row or row['revoked_ms'] or row['expires_ms'] and row['expires_ms'] < now_ms()
                or row['max_uses'] and row['uses'] >= row['max_uses']):
            raise InstanceError('invalid_enrollment_code', 403)
        await conn.execute('UPDATE server_registration_codes SET uses=uses+1,last_used_ms=? WHERE id=?', (now_ms(), row['id']))
        return dict(row)

    async def _join(self, code, key, info, machine, version):
        """A machine that registered itself becomes a new server named after the host. One that
        looks like a server already here (same machine id and host name) is not given that server:
        machines cloned from one template look alike too. It joins as a new server pending the
        owner's word (settle_join): take that server over, stay a server of its own, or go."""
        owner = code['owner']
        existing = None
        if machine and info.get('hostname'):
            existing = await one(self.db.conn, "SELECT server_id FROM server_nodes WHERE owner=? AND json_extract(info_json,'$.info.machineId')=? "
                                 "AND json_extract(info_json,'$.info.hostname')=? AND json_extract(info_json,'$.joinPending') IS NULL",
                                 (owner, machine, info['hostname']))
        taken = {s['name'] for s in await self.service.registry.all(owner)}
        base = (info.get('hostname') or 'server')[:50]
        name, n = base, 2
        while name in taken:
            name, n = f'{base} ({n})', n + 1
        server_id = (await self.service.registry.save(owner, {'name': name, 'auth': 'none'}))['item']['id']
        profile = json.loads(code['profile_json'])
        if profile:
            await self.service._save_profile(owner, server_id, profile)
        pending = {'joinPending': {'serverId': existing['server_id'], 'code': code['name'], 'atMs': now_ms()}} if existing else None
        async with self.db.write_transaction(label='server-join') as conn:
            await self._bind(conn, owner, server_id, key, info, machine, version, pending)
        host = info.get('hostname') or '未知'
        if existing:
            other = (await self.service.registry.get(owner, existing['server_id']))['name']
            text = (f'通过注册码{quoted(code["name"])}接入的机器（主机名 {quoted(host)}）和已有服务器{quoted(other)}像是同一台，'
                    f'暂作为新服务器{quoted(name)}加入。请在服务器页面确认：由它接管{quoted(other)}、作为新服务器保留，或者拒绝。')
        else:
            text = f'服务器{quoted(name)}通过注册码{quoted(code["name"])}已加入（主机名 {quoted(host)}）。'
        await self.service.reminders._enqueue(owner, f'joined:{server_id}:{now_ms()}', server_id, 'joined', text)
        self.service.spawn(self.service.reminders.deliver())
        return server_id, owner

    async def settle_join(self, owner, server_id, data):
        """The owner's word on a server pending after it joined (_join). takeover: the server it
        looked like gets its node (key and facts), the node keeps connecting under the id it was
        given and is found by that server's record (nodeId); the pending server goes. keep: it
        stays a server of its own. reject: it is deleted and its node cut off."""
        action = data.get('action') if isinstance(data, dict) and set(data) == {'action'} else None
        if action not in ('takeover', 'keep', 'reject'):
            raise InstanceError('invalid_join_action')
        await self.service.registry.get(owner, server_id)
        row = await one(self.db.conn, 'SELECT * FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))
        stored = json.loads(row['info_json']) if row else {}
        pending = stored.get('joinPending')
        if not pending:
            raise InstanceError('join_not_pending', 409)
        if action == 'reject':
            await self.service.delete(owner, server_id)
            return {'ok': True}
        if action == 'keep':
            async with self.db.write_transaction(label='server-join-keep') as conn:
                current = await (await conn.execute('SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))).fetchone()
                if current:
                    value = json.loads(current['info_json'])
                    value.pop('joinPending', None)
                    await conn.execute('UPDATE server_nodes SET info_json=? WHERE server_id=?', (dumps(value), server_id))
            return {'ok': True, 'item': await self.service.get(owner, server_id)}
        target = pending['serverId']
        await self.service.registry.get(owner, target)
        async with self.db.write_transaction(label='server-join-takeover') as conn:
            node = await (await conn.execute('SELECT public_key,info_json FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))).fetchone()
            if not node:
                raise InstanceError('join_not_pending', 409)
            joined = json.loads(node['info_json'])
            prior = await (await conn.execute('SELECT info_json FROM server_nodes WHERE server_id=?', (target,))).fetchone()
            value = json.loads(prior['info_json']) if prior else {}
            value.update({k: v for k, v in joined.items() if k not in ('joinPending', 'geo', 'accessPending')}, nodeId=server_id)
            await conn.execute('DELETE FROM server_nodes WHERE server_id=?', (server_id,))
            await conn.execute('INSERT OR REPLACE INTO server_nodes VALUES(?,?,?,?,?,?)', (target, owner, node['public_key'], dumps(value), now_ms(), 0))
        # The node now there was another machine's, or that machine before it was reinstalled. The
        # joined one reconnects at once, as the server it took over: not told it was revoked.
        await self.service.metrics.rebase(target)
        await self.service.hub.disconnect(target, CLOSE_REPLACED)
        await self.service.hub.disconnect(server_id, CLOSE_REPLACED)
        await self.service.delete(owner, server_id)
        return {'ok': True, 'serverId': target, 'item': await self.service.get(owner, target)}

    def _code_view(self, row, urls):
        code = self.service.instances.vault.open(row['sealed'])['code']
        profile = json.loads(row['profile_json'])
        expired = bool(row['expires_ms'] and row['expires_ms'] < now_ms())
        used_up = bool(row['max_uses'] and row['uses'] >= row['max_uses'])
        url, backup = urls(profile)
        fingerprint = install.fingerprint(self.service.identity_public)
        return {'id': row['id'], 'name': row['name'], 'group': profile.get('group', ''), 'access': profile.get('access') or {},
                'expiresAtMs': row['expires_ms'], 'maxUses': row['max_uses'], 'uses': row['uses'], 'createdAtMs': row['created_ms'],
                'lastUsedAtMs': row['last_used_ms'], 'revokedAtMs': row['revoked_ms'],
                'active': not (row['revoked_ms'] or expired or used_up),
                'command': install.command(url, code, backup=backup, fingerprint=fingerprint) if url and not row['revoked_ms'] else '',
                'windowsCommand': install.windows_command(url, code, backup=backup, fingerprint=fingerprint) if url and not row['revoked_ms'] else ''}

    async def _urls(self):
        settings = await self.service.settings()

        def urls(profile):
            primary, backup = resolve(settings, profile)
            return (primary['url'] if primary else ''), (backup['url'] if backup else '')
        return urls

    async def codes(self, owner):
        rows = await many(self.db.conn, 'SELECT * FROM server_registration_codes WHERE owner=? ORDER BY created_ms DESC', (owner,))
        urls = await self._urls()
        return {'ok': True, 'items': [self._code_view(r, urls) for r in rows]}

    async def create_code(self, owner, data):
        if not isinstance(data, dict) or set(data) - {'name', 'group', 'access', 'expiresInDays', 'maxUses'}:
            raise InstanceError('invalid_registration_code')
        name = data.get('name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise InstanceError('registration_code_name_required')
        days, uses = data.get('expiresInDays', 0), data.get('maxUses', 0)
        if type(days) is not int or not 0 <= days <= 3650 or type(uses) is not int or not 0 <= uses <= 10000:
            raise InstanceError('invalid_registration_code')
        from app.servers import profile as profiles
        profile = profiles.clean({k: data[k] for k in ('group', 'access') if data.get(k)})
        # A server that registers itself is new, so not in the mesh.
        if uses_mesh(profile):
            raise InstanceError('mesh_access_requires_member', 409)
        code = 'reg-' + secrets.token_urlsafe(24)
        ident = secrets.token_hex(6)
        async with self.db.write_transaction(label='server-registration-code') as conn:
            await conn.execute('INSERT INTO server_registration_codes(id,owner,name,code_hash,sealed,profile_json,expires_ms,max_uses,created_ms) VALUES(?,?,?,?,?,?,?,?,?)',
                               (ident, owner, name.strip(), digest(code), self.service.instances.vault.seal({'code': code}), dumps(profile),
                                now_ms() + days * 86400000 if days else 0, uses, now_ms()))
        row = await one(self.db.conn, 'SELECT * FROM server_registration_codes WHERE id=?', (ident,))
        return {'ok': True, 'item': self._code_view(row, await self._urls())}

    async def revoke_code(self, owner, ident):
        async with self.db.write_transaction(label='server-registration-revoke') as conn:
            cursor = await conn.execute('UPDATE server_registration_codes SET revoked_ms=? WHERE id=? AND owner=? AND revoked_ms=0', (now_ms(), ident, owner))
            if not cursor.rowcount and not await (await conn.execute('SELECT 1 FROM server_registration_codes WHERE id=? AND owner=?', (ident, owner))).fetchone():
                raise InstanceError('registration_code_not_found', 404)
        return await self.codes(owner)

    # ----- installation over SSH, one visible step at a time -----
    async def install(self, owner, server_id, *, browser=False, force=False):
        """force: replace a node of another controller found on the machine (machine_has_other_node);
        a machine already connected here as another server is never taken over (machine_has_node)."""
        if type(force) is not bool:
            raise InstanceError('invalid_install')
        if (self.installs.get(server_id) or {}).get('state') == 'running':
            raise InstanceError('node_install_running', 409)
        if browser and server_id == LOCAL:
            raise InstanceError('browser_local_default', 409)
        # Claimed before the first await, so a second request cannot start another run.
        previous = self.installs.get(server_id)
        state = self.installs[server_id] = {'state': 'running', 'startedAtMs': now_ms(), 'output': '',
                                            'steps': [{'key': k, 'label': label, 'state': 'pending'} for k, label in STEPS]}
        try:
            if not (await self.service.ensure_builds()).get('binaries'):
                raise InstanceError('node_binary_missing', 503)
            url, backup = await self.addresses(owner, server_id)
            target = await self.service.registry.target(owner, server_id)
            code = await self.new_code(owner, server_id)
        except BaseException:
            if self.installs.get(server_id) is state:
                if previous is None:
                    self.installs.pop(server_id, None)
                else:
                    self.installs[server_id] = previous
            raise
        self.service.spawn(self._install(state, owner, server_id, target, url, backup, code, browser, force))
        return {'ok': True, 'install': state}

    async def _machine_check(self, owner, server_id, existing, force):
        """After the check stage: the node the machine already runs, if any, decides whether to go on.
        True when it is this server's own under the id it registered with (settle_join): it is
        replaced without asking."""
        if not existing or existing == server_id:
            return False
        if await one(self.db.conn, "SELECT 1 FROM server_nodes WHERE server_id=? AND owner=? AND json_extract(info_json,'$.nodeId')=?",
                     (server_id, owner, existing)):
            return True
        if existing == LOCAL or await one(self.db.conn, 'SELECT 1 FROM instance_servers WHERE id=? AND owner=?', (existing, owner)):
            name = (await self.service.registry.get(owner, existing))['name']
            raise MachineTaken('machine_has_node', existing, name,
                               f'这台机器已经作为「{name}」接入，先删除那台或直接在它上面重新安装')
        if not force:
            raise MachineTaken('machine_has_other_node', existing, '',
                               f'这台机器上已经装有其他节点（{existing}，可能属于别的主控），确认要替换时用强制安装')
        return False

    async def _install(self, state, owner, server_id, target, url, backup, code, browser=False, force=False):
        scrub = Scrubber([code])
        sudo = not target.get('local') and target.get('username') != 'root' and target.get('auth') == 'password'
        current = None
        try:
            platform = await self.service.registry.platform(target)

            async def stage(key):
                # The check stage reads the node config already on the machine and changes nothing.
                opts = {'browser': browser, 'node': server_id, 'force': force, 'fingerprint': install.fingerprint(self.service.identity_public)}
                if platform == 'windows':
                    # An administrator's SSH session already runs elevated; there is no sudo.
                    return await self.service.registry.run(target, install.windows_ssh_command(url, code, backup=backup, stage=key, **opts), timeout=300)
                if sudo:
                    # The sudo password occupies stdin, so the script is fetched instead.
                    return await self.service.registry.run(target, install.command(url, code, backup=backup, stage=key, **opts), timeout=300, sudo=True)
                return await self.service.registry.run(target, 'sh -s -- ' + install.arguments(url, code, backup, key, **opts),
                                                       stdin=install.script().encode(), timeout=300, root=True)
            for key, _ in STEPS[:-1]:
                current = self._step(state, key, 'running')
                result = await stage(key)
                if key == 'check' and await self._machine_check(owner, server_id, install.existing_node(result['stdout']), force):
                    # This server's own node under the id it joined with: replaced, the check run again.
                    force = True
                    if not result['ok']:
                        result = await stage(key)
                text = scrub('\n'.join(line for line in (result['stdout'] + '\n' + result['stderr']).splitlines()
                                       if line.strip() and not line.startswith('@@')))
                state['output'] = (state['output'] + ('\n' if state['output'] and text else '') + text)[-3000:]
                if not result['ok']:
                    current['detail'] = text.strip().splitlines()[-1][:200] if text.strip() else ''
                    raise InstanceError('install_timeout' if result['timedOut'] else f'install_{key}_failed')
                self._step(state, key, 'done')
            current = self._step(state, 'online', 'running')
            deadline = time.monotonic() + ONLINE_WAIT
            while server_id not in self.service.hub.sessions:
                if time.monotonic() > deadline:
                    raise InstanceError('node_not_connected')
                await asyncio.sleep(0.5)
            self._step(state, 'online', 'done')
            state['state'] = 'done'
        except MachineTaken as exc:
            state.update(state='failed', error=exc.error, message=exc.message, existing={'nodeId': exc.node, 'name': exc.name})
            if current:
                current.update(state='failed', detail=exc.message)
        except InstanceError as exc:
            state.update(state='failed', error=exc.error)
            if current:
                current['state'] = 'failed'
        except Exception:
            state.update(state='failed', error='install_failed')
            if current:
                current['state'] = 'failed'
        state['finishedAtMs'] = now_ms()

    @staticmethod
    def _step(state, key, value):
        step = next(s for s in state['steps'] if s['key'] == key)
        step['state'] = value
        return step

    # ----- batch: many servers sharing one SSH login -----
    async def batch(self, owner, data):
        if (not isinstance(data, dict) or set(data) - {'servers', 'ssh', 'profile', 'browser', 'force'}
                or type(data.get('browser', False)) is not bool or type(data.get('force', False)) is not bool):
            raise InstanceError('invalid_batch')
        servers, ssh, profile = data.get('servers'), data.get('ssh'), data.get('profile') or {}
        browser, force = data.get('browser', False), data.get('force', False)
        if not isinstance(servers, list) or not 1 <= len(servers) <= BATCH_LIMIT or not isinstance(ssh, dict) or not isinstance(profile, dict):
            raise InstanceError('invalid_batch')
        entries = []
        for line in servers:
            if not isinstance(line, dict) or set(line) - {'name', 'host', 'port'} or not isinstance(line.get('host'), str) or not line['host'].strip():
                raise InstanceError('invalid_batch')
            port = line.get('port', 22)
            if type(port) is not int:
                raise InstanceError('invalid_server_port')
            entries.append({'name': str(line.get('name') or line['host']).strip()[:60], 'host': line['host'].strip(), 'port': port,
                            'state': 'pending', 'serverId': '', 'error': ''})
        cutoff = now_ms() - 6 * 3600000
        for ident in [k for k, j in self.batches.items() if j['createdAtMs'] < cutoff]:
            self.batches.pop(ident, None)
        job = {'id': secrets.token_hex(8), 'owner': owner, 'createdAtMs': now_ms(), 'items': entries}
        self.batches[job['id']] = job
        gate = asyncio.Semaphore(4)

        async def one_server(entry):
            async with gate:
                entry['state'] = 'connecting'
                try:
                    created = await self.service.create(owner, {'name': entry['name'], 'profile': profile,
                                                                'ssh': {**ssh, 'host': entry['host'], 'port': entry['port']}})
                    entry['serverId'] = created['item']['id']
                    await self.install(owner, entry['serverId'], browser=browser, force=force)
                    entry['state'] = 'installing'
                    while (self.installs.get(entry['serverId']) or {}).get('state') == 'running':
                        await asyncio.sleep(1)
                    entry['state'] = (self.installs.get(entry['serverId']) or {}).get('state', 'failed')
                    entry['error'] = (self.installs.get(entry['serverId']) or {}).get('error', '')
                except InstanceError as exc:
                    entry.update(state='failed', error=exc.error)
                except Exception:
                    entry.update(state='failed', error='install_failed')
        for entry in entries:
            self.service.spawn(one_server(entry))
        return {'ok': True, 'job': self._batch_view(job)}

    def _batch_view(self, job):
        items = [{**{k: e[k] for k in ('name', 'host', 'port', 'state', 'serverId', 'error')},
                  'install': self.installs.get(e['serverId']) if e['serverId'] else None} for e in job['items']]
        return {'id': job['id'], 'createdAtMs': job['createdAtMs'], 'done': all(i['state'] in {'done', 'failed'} for i in items), 'items': items}

    async def batch_job(self, owner, ident):
        job = self.batches.get(ident)
        if not job or job['owner'] != owner:
            raise InstanceError('batch_not_found', 404)
        return {'ok': True, 'job': self._batch_view(job)}

    # ----- moving online nodes to their configured addresses -----
    async def push(self, owner, server_ids=None):
        owned = {r['server_id'] for r in await many(self.db.conn, 'SELECT server_id FROM server_nodes WHERE owner=?', (owner,))}
        targets = [sid for sid in list(self.service.hub.sessions) if sid in owned and sid != LOCAL and (server_ids is None or sid in server_ids)]
        return {'ok': True, 'items': list(await asyncio.gather(*(self.push_one(owner, sid) for sid in targets)))}

    async def push_one(self, owner, sid, addresses=None):
        """Move one online node to its configured addresses, or to the given ones."""
        try:
            url, backup = addresses or await self.addresses(owner, sid)
            result = await self.service._node_call(owner, sid, 'config.url', {'url': url, 'backup': backup}, timeout=60)
        except InstanceError as exc:
            return {'serverId': sid, 'ok': False, 'error': exc.error}
        await self.service.mark_access_pending(sid, False)
        return {'serverId': sid, 'ok': True, 'changed': bool(result.get('changed')), 'backupReachable': result.get('backupReachable', True)}
