"""Access gateway lifecycle. The controller installs the gateway on a chosen
server, keeps its control key private, and pushes account/route changes
automatically. Passwords exist only in pending delivery.

Where the controller reaches the server's loopback (the local machine, or a node
that carries HTTP), the control API listens on 127.0.0.1 only; servers reached
only over SSH keep it on the public port, authenticated by the control key."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import platform
import re
import secrets
import shlex
import time
from pathlib import Path, PurePosixPath

from app.instances import network
from app.instances.contracts import InstanceError, origin
from app.logging import get_logger
from app.webhooks.repository import many, one

log = get_logger('instance_gateway')

DEFAULT_PORT = 18962
# Standard system locations: unlike /opt (often 0750 root), every distribution
# lets an unprivileged service user traverse these.
BIN = '/usr/local/lib/openbear-gateway/openbear-gateway'
CONFIG_DIR = '/etc/openbear-gateway'
STATE_DIR = '/var/lib/openbear-gateway'
SERVICE = 'openbear-gateway.service'
# Checked before anything is written, so an unsupported host is reported as such.
PREFLIGHT = '''[ "$(id -u)" = 0 ] || { echo "gateway-needs-root" >&2; exit 3; }
[ -d /run/systemd/system ] || { echo "gateway-needs-systemd" >&2; exit 3; }
command -v useradd >/dev/null 2>&1 || { echo "gateway-needs-useradd" >&2; exit 3; }'''
ROOT = Path(__file__).resolve().parents[2]


def now():
    return int(time.time()*1000)


def prepare(item, protected, *, enabled=True, account=None):
    """Caller holds the instance lock; never recreate an already ACKed password."""
    state = item.setdefault('gateway', {})
    state['mayBeEnabled'] = bool(state.get('mayBeEnabled') or state.get('appliedEnabled') or protected.get('gatewayPending', {}).get('enabled'))
    state.update(status='pending_sync', revision=int(state.get('revision', 0))+1,
                 appliedRevision=int(state.get('appliedRevision', 0)), enabled=enabled, error='')
    previous = protected.pop('gatewayPending', None)
    if previous and any(k in previous for k in ('username', 'password')):
        # An ACK may have been lost after the user changed the account. Resolve
        # this exact revision before the next one; never replay its account fields
        # under a NEW revision, including on route updates or revocation.
        protected['gatewayUnacked'] = previous
        protected.pop('gatewayAccount', None)
    if account:
        protected.setdefault('gatewayAccount', {}).update(account)
    protected.setdefault('businessKey', secrets.token_urlsafe(48))
    item['authMode'] = protected['authMode'] = 'gateway'


# Go's netip.Addr.IsPrivate: what gateways before explicit proxy trust accepted.
LEGACY_PRIVATE_PROXIES = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))

ARCHES = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}
UNMANAGED_CONFIG = ('webRoot', 'tlsCert', 'tlsKey', 'clientIp')
_BUILDS = {}
# Plain words for an automatic update's failure notice; other codes are shown as they are.
UPDATE_ERRORS = {
    'gateway_install_needs_root': '安装需要 root 权限或免密 sudo', 'gateway_needs_systemd': '服务器没有 systemd',
    'gateway_needs_useradd': '服务器缺少 useradd', 'gateway_start_failed': '新版本的网关服务启动失败',
    'gateway_install_failed': '安装命令执行失败', 'gateway_binary_missing': '主控缺少网关程序',
    'gateway_update_interrupted': '主控关闭中断了更新，远端结果待确认，恢复后请检查网关状态',
    'gateway_architecture_unsupported': '服务器的系统架构不受支持', 'gateway_unreachable': '更新后主控连不上网关',
    'gateway_control_port_unavailable': '找不到给网关管理用的空闲端口', 'gateway_address_unknown': '无法确定服务器的地址',
    'node_offline': '网关所在服务器的节点离线', 'server_ssh_unavailable': '连不上网关所在的服务器',
    'gateway_node_unavailable': '网关所在服务器的节点暂时连不上，管理接口只能经节点更新',
    'node_exec_disabled': '网关所在服务器的节点关闭了命令执行',
    'gateway_node_upgrade_required': '网关所在服务器的节点版本太旧，管理接口只能经节点更新',
    'gateway_node_exec_disabled': '网关所在服务器的节点关闭了命令执行，管理接口只能经节点更新'}
# Why the node of a gateway whose control API listens on loopback cannot carry the update.
GATEWAY_NODE_BLOCKERS = {'node_upgrade_required': 'gateway_node_upgrade_required', 'node_exec_disabled': 'gateway_node_exec_disabled'}


def bundled_builds():
    """{arch: sha256} of the gateway programs this controller installs, re-read only when a file changes."""
    builds = {}
    for arch in sorted(set(ARCHES.values())):
        path = ROOT / 'gateway/bin' / f'openbear-gateway-linux-{arch}'
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (stat.st_mtime_ns, stat.st_size)
        cached = _BUILDS.get(arch)
        if not cached or cached[0] != key:
            try:
                cached = _BUILDS[arch] = (key, hashlib.sha256(path.read_bytes()).hexdigest())
            except OSError:
                continue
        builds[arch] = cached[1]
    return builds


def _unit(user):
    return f"""[Unit]
Description=OpenBear access gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={STATE_DIR}
ExecStart={BIN} --config {CONFIG_DIR}/gateway.json
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
# systemd creates it, hands it to User= and makes it writable at every start.
StateDirectory={PurePosixPath(STATE_DIR).name}
StateDirectoryMode=0700

[Install]
WantedBy=multi-user.target
"""


def _install_error(result):
    err = result['stderr']
    for marker, code in (('gateway-needs-root', 'gateway_install_needs_root'), ('gateway-needs-systemd', 'gateway_needs_systemd'),
                         ('gateway-needs-useradd', 'gateway_needs_useradd'), ('gateway-start-failed', 'gateway_start_failed')):
        if marker in err:
            if code == 'gateway_start_failed':
                log.warning('网关服务启动失败', 输出=err[-800:])
            return code
    # A non-root user whose sudo is missing or not passwordless.
    if 'sudo' in err and any(m in err for m in ('password', 'sudoers', 'a terminal is required', 'not found', 'not allowed')):
        return 'gateway_install_needs_root'
    log.warning('网关安装失败', 输出=err[-800:])
    return 'gateway_install_failed'


class Gateway:
    def __init__(self, service):
        self.service = service
        self.sync_task = None
        self.update_task = None
        # Target builds already tried since this controller started: a failed one is
        # not retried automatically until the next start or a manual update.
        self.update_tried = set()

    @property
    def servers(self):
        return self.service.servers

    async def config(self):
        row = await one(self.service.db.conn, 'SELECT * FROM instance_gateway WHERE id=1')
        value = json.loads(row['public_json']) if row else {}
        for key, default in (('enabled', False), ('publicUrl', ''), ('serverId', ''), ('port', DEFAULT_PORT),
                             ('status', 'disabled'), ('error', ''), ('checkedAtMs', None), ('eventSeq', 0),
                             ('clientIp', {'source': 'auto', 'trustedProxies': []}), ('observed', None), ('version', ''),
                             # Installs from before automatic updates count as opted in.
                             ('build', ''), ('updateAvailable', False), ('updateReasons', []), ('autoUpdate', True), ('lastUpdate', None)):
            value.setdefault(key, default)
        return value, self.service.vault.open(row['protected']) if row else {}

    async def store(self, value, private):
        async with self.service.db.write_transaction(label='instance-gateway-config') as conn:
            await conn.execute('INSERT INTO instance_gateway VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET public_json=excluded.public_json,protected=excluded.protected',
                               (json.dumps(value), self.service.vault.seal(private)))

    @staticmethod
    def control_route(value, private):
        """Where the controller manages the gateway, never through the user-facing address
        (which may be a domain behind someone else's proxy): its loopback control port,
        directly or through the server's node, else server address + public port."""
        port = private.get('controlPort')
        if port:
            server = value['serverId'] or 'local'
            if server == 'local':
                return {'url': f'http://127.0.0.1:{port}'}
            return {'owner': private.get('owner'), 'serverId': server, 'port': port}
        return {'url': private['controlUrl']} if private.get('controlUrl') else None

    async def call(self, value, private, method, path, body=None):
        route = self.control_route(value, private)
        if not private.get('controlKey') or not route:
            raise InstanceError('gateway_not_configured', 409)
        try:
            status, raw = await network.fetch(self.service, route, method, path, body=body, timeout=10,
                                              headers={'Authorization': 'Bearer '+private['controlKey']})
        except network.TransportError as exc:
            raise InstanceError('gateway_response_too_large' if exc.kind == 'too_large' else 'gateway_unreachable', 502) from None
        if status != 200:
            # The gateway names its refusals (username_unavailable, instance_limit, ...).
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                named = network.loads(raw).get('error')
                if isinstance(named, str) and re.fullmatch(r'[a-z_]{1,40}', named):
                    raise InstanceError('gateway_' + named, 502)
            raise InstanceError('gateway_http_'+str(status), 502)
        try:
            data = network.loads(raw)
        except ValueError:
            raise InstanceError('gateway_invalid_response', 502) from None
        if not isinstance(data, dict) or data.get('ok') is not True:
            raise InstanceError('gateway_invalid_response', 502)
        return data

    # ----- public API -----
    @staticmethod
    def public_config(value, private):
        return {'ok': True, **{k: v for k, v in value.items() if not k.startswith('_') and k != 'eventSeq'},
                'connected': value['enabled'] and value['status'] == 'connected',
                'updateAvailable': bool(value.get('enabled') and value.get('updateAvailable'))}

    async def get(self, *, refresh=True):
        self.service.require_controller()
        async with self.service.lock('gateway-config'):
            value, private = await self.config()
            if refresh and value['enabled']:
                await self._refresh(value, private)
                await self.store(value, private)
                self._maybe_auto_update(value)
            return {**self.public_config(value, private), 'updating': self.updating}

    async def _refresh(self, value, private):
        try:
            result = await self.call(value, private, 'GET', '/control/v1/status')
            previous = value.get('observed')
            value.update(status='connected', error='', checkedAtMs=now(), observed=result.get('clientIpObserved'),
                         clientIp=result.get('clientIp') or value['clientIp'])
            for observed in (previous, value['observed']):
                await self._keep_legacy_proxy(value, private, observed)
            await self._check_build(value, private, result)
            await self._account_names(result)
            return result
        except InstanceError as exc:
            value.update(status='unreachable', error=exc.error, checkedAtMs=now())
            return None

    async def _keep_legacy_proxy(self, value, private, observed):
        """Earlier gateways trusted every private-network proxy without asking. One they
        reported in use is written to the trusted list once, so upgrading keeps that
        deployment's real client IPs. Current gateways mark observations 'trusted'; an
        untrusted one is only offered to the owner for confirmation."""
        if not isinstance(observed, dict) or 'trusted' in observed or value['clientIp'].get('source') == 'socket':
            return
        try:
            peer = ipaddress.ip_address(observed.get('peer') if isinstance(observed.get('peer'), str) else '')
            listed = [ipaddress.ip_network(entry, strict=False) for entry in value['clientIp'].get('trustedProxies') or []]
        except ValueError:
            return
        # Loopback and Cloudflare edges remain trusted without a list entry.
        migrated = value.setdefault('_migratedProxies', [])
        if (not any(peer in network for network in LEGACY_PRIVATE_PROXIES) or str(peer) in migrated
                or any(peer in network for network in listed)):
            return
        body = {'source': 'header', 'trustedProxies': [*(value['clientIp'].get('trustedProxies') or []), str(peer)]}
        try:
            ack = await self.call(value, private, 'PUT', '/control/v1/client-ip', body)
        except InstanceError:
            return  # Retried at the next status refresh.
        value['clientIp'] = ack.get('clientIp') or body
        migrated.append(str(peer))
        log.info('网关代理已加入信任列表', 代理=str(peer))

    async def _account_names(self, result):
        # The gateway owns current account names, including self-renames.
        entries = result.get('instances') if isinstance(result.get('instances'), list) else []
        # Locks only for instances this controller has: the gateway names any id it likes.
        known = {r['id'] for r in await many(self.service.db.conn, 'SELECT id FROM managed_instances')}
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not isinstance(entry.get('username'), str)
                    or entry['id'] not in known):
                continue
            lock = self.service.lock(entry['id'])
            if lock.locked():
                continue
            async with lock:
                row = await one(self.service.db.conn, 'SELECT * FROM managed_instances WHERE id=?', (entry['id'],))
                if not row:
                    continue
                item = json.loads(row['public_json'])
                state = item.get('gateway', {})
                if state.get('appliedRevision') != entry.get('revision') or item.get('account', {}).get('username') == entry['username']:
                    continue
                item['account'] = {'username': entry['username']}
                async with self.service.db.write_transaction(label='gateway-current-account') as conn:
                    await conn.execute('UPDATE managed_instances SET public_json=? WHERE id=?', (json.dumps(item), row['id']))

    async def check_port(self, owner, server_id, port):
        value, _ = await self.config()
        if value['enabled'] and value['serverId'] == server_id and value['port'] == port:
            return {'ok': True, 'free': True, 'current': True}
        return await self.servers.port(owner, server_id, port)

    async def suggest(self, owner, server_id, port):
        ip = await self.servers.public_ip(owner, server_id)
        return {'ok': True, 'publicIp': ip, 'publicUrl': f'http://{ip}:{port}' if ip else ''}

    async def _control_port(self, owner, server_id, port):
        """A free port for the loopback control listener, next to the public one."""
        taken = {port}
        for row in await many(self.service.db.conn, "SELECT public_json FROM managed_instances WHERE status!='archived'"):
            item = json.loads(row['public_json'])
            if item.get('serverId') == server_id and item.get('port'):
                taken.add(item['port'])
        for candidate in range(port + 1, min(port + 101, 65536)):
            if candidate not in taken and (await self.servers.port(owner, server_id, candidate)).get('free') is not False:
                return candidate
        raise InstanceError('gateway_control_port_unavailable', 409)

    async def enable(self, owner, data, *, rotate=False):
        """Install or update the gateway on a server and start it. Auto key; rotate=True
        replaces it. Saving again also moves an older install's control API to loopback."""
        self.service.require_controller()
        if set(data) - {'serverId', 'port', 'publicUrl'}:
            raise InstanceError('invalid_gateway_configuration')
        server_id, port, public_url = data.get('serverId'), data.get('port'), data.get('publicUrl')
        if not isinstance(server_id, str) or type(port) is not int or not 1 <= port <= 65535 or not isinstance(public_url, str):
            raise InstanceError('invalid_gateway_configuration')
        public_url = origin(public_url)
        async with self.service.lock('gateway-config'):
            value, private = await self.config()
            await self._apply(owner, value, private, server_id, port, public_url, rotate)
        await self.sync_all()
        return self.public_config(*(await self.config()))

    async def reinstall(self, owner, *, rotate=False):
        """enable() with the settings as they are when the lock is held, so a save made
        meanwhile is kept, not put back."""
        self.service.require_controller()
        async with self.service.lock('gateway-config'):
            value, private = await self.config()
            if not value['enabled']:
                raise InstanceError('gateway_not_enabled', 409)
            await self._apply(owner, value, private, value['serverId'], value['port'], value['publicUrl'], rotate)
        await self.sync_all()
        return self.public_config(*(await self.config()))

    async def _apply(self, owner, value, private, server_id, port, public_url, rotate):
        moving = value['enabled'] and (value['serverId'] != server_id or value['port'] != port)
        if moving and await self._has_instances():
            raise InstanceError('gateway_move_with_instances', 409)
        if not (value['enabled'] and value['serverId'] == server_id and value['port'] == port):
            check = await self.servers.port(owner, server_id, port)
            if check.get('free') is False:
                raise InstanceError('gateway_port_in_use', 409)
        target = await self.servers.target(owner, server_id, nodes=True)
        if target.get('local') or await self.servers.http_ready(owner, server_id):
            same = not moving and value['enabled'] and private.get('controlPort')
            control_port, control = (private['controlPort'] if same else await self._control_port(owner, server_id, port)), ''
        elif not moving and value['enabled'] and private.get('controlPort'):
            # Its control API listens on loopback and is reached through the node: reinstalling
            # over SSH now would put it, and the key, on the public port. Wait for the node, said
            # as what keeps it: offline, too old or not running commands.
            why = await self.servers.http_blocker(owner, server_id)
            raise InstanceError(GATEWAY_NODE_BLOCKERS.get(why, 'gateway_node_unavailable'), 409)
        else:
            host = target.get('host') or target.get('nodeAddress', '')
            if not host:
                raise InstanceError('gateway_address_unknown', 409)
            control_port, control = None, origin(f'http://{host}:{port}' if ':' not in host else f'http://[{host}]:{port}')
        key = private.get('controlKey') if not moving and not rotate and private.get('controlKey') else secrets.token_urlsafe(48)
        await self._install(target, port, public_url, key, control_port)
        value.update(enabled=True, serverId=server_id, port=port, publicUrl=public_url, status='starting', error='', checkedAtMs=now())
        private.update(controlKey=key, controlUrl=control, controlPort=control_port, owner=owner)
        await self.store(value, private)
        # Instances on a mesh address are reached from the gateway's server.
        self.service._mesh_changed()
        for _ in range(20):
            if await self._refresh(value, private):
                break
            await asyncio.sleep(0.5)
        await self.store(value, private)
        if value['status'] != 'connected':
            raise InstanceError(value['error'] or 'gateway_unreachable', 502)

    async def _has_instances(self):
        return bool(await one(self.service.db.conn, "SELECT 1 FROM managed_instances WHERE auth_mode='gateway' AND status!='archived' LIMIT 1"))

    async def rotate_control_key(self, owner):
        """A new control key: the gateway is reinstalled with it. Login sessions stay."""
        return await self.reinstall(owner, rotate=True)

    async def rotate_all(self, owner):
        """Control key now; every instance's business and management keys now or, for an
        instance that cannot be reached, as soon as it can (see InstanceService.rotate_keys)."""
        self.service.require_controller()
        await self.rotate_control_key(owner)
        rows = await many(self.service.db.conn, "SELECT id FROM managed_instances WHERE owner=? AND auth_mode='gateway' AND status!='archived'", (owner,))
        rotated = pending = 0
        for row in rows:
            try:
                result = await self.service.rotate_keys(owner, row['id'], queue_if_busy=True)
            except InstanceError:
                result = {'rotated': False}
            rotated, pending = rotated + bool(result.get('rotated')), pending + (not result.get('rotated'))
        return {**self.public_config(*(await self.config())), 'rotatedInstances': rotated, 'pendingInstances': pending}

    # ----- program updates -----
    async def _check_build(self, value, private, status):
        """From a fresh status: the build the gateway runs and why reinstalling it through
        enable() would change something (updateReasons, empty when it is current)."""
        build = status.get('build')
        value['build'] = build if isinstance(build, str) else ''
        reasons, builds = [], bundled_builds()
        if builds:  # Without bundled programs there is nothing to install.
            if not value['build']:
                reasons.append('build_unreported')  # Gateways from before build reporting.
            elif value['build'] not in builds.values():
                reasons.append('build_outdated')
        if not private.get('controlPort') and await self._loopback_ready(value, private):
            reasons.append('control_not_loopback')
        value.update(updateReasons=reasons, updateAvailable=bool(reasons))

    async def _loopback_ready(self, value, private):
        """enable()'s condition for giving the control API its own loopback listener."""
        server = value.get('serverId') or 'local'
        if server == 'local':
            return True
        try:
            owner = await self._owner(value, private)
            return owner is not None and await self.servers.http_ready(owner, server)
        except Exception:
            return False

    async def _owner(self, value, private):
        """Whose server runs the gateway: recorded by enable(); for older installs the
        server's owner, or the controller's owner when it runs locally."""
        if private.get('owner') is not None:
            return private['owner']
        server = value.get('serverId') or 'local'
        if server != 'local':
            row = await one(self.service.db.conn, 'SELECT owner FROM instance_servers WHERE id=?', (server,))
            if row:
                return row['owner']
        ids = list(getattr(getattr(getattr(self.service.host, 'config', None), 'telegram', None), 'whitelist_ids', None) or [])
        return int(ids[0]) if ids else None

    @property
    def updating(self):
        return bool(self.update_task and not self.update_task.done())

    @staticmethod
    def _update_target():
        return ','.join(sorted(bundled_builds().values()))

    async def set_auto_update(self, data):
        self.service.require_controller()
        if set(data) != {'autoUpdate'} or not isinstance(data['autoUpdate'], bool):
            raise InstanceError('invalid_gateway_auto_update')
        async with self.service.lock('gateway-config'):
            value, private = await self.config()
            if not value['enabled']:
                raise InstanceError('gateway_not_enabled', 409)
            value['autoUpdate'] = data['autoUpdate']
            await self.store(value, private)
            self._maybe_auto_update(value)
            return {**self.public_config(value, private), 'updating': self.updating}

    async def update(self, owner, *, auto=False):
        """Reinstall the bundled program with the current server, port, address and control
        key: the same path as saving the settings again. State stays; only the gateway restarts."""
        self.service.require_controller()
        value, _ = await self.config()
        if not value['enabled']:
            raise InstanceError('gateway_not_enabled', 409)
        if not auto and self.updating:
            raise InstanceError('gateway_update_running', 409)
        # A manual attempt also counts: its failure is not retried automatically.
        self.update_tried.add(self._update_target())
        try:
            await self.reinstall(owner)
        except asyncio.CancelledError:
            await self._finish_update(owner, auto, 'gateway_update_interrupted')
            raise
        except InstanceError as exc:
            await self._finish_update(owner, auto, exc.error)
            raise
        except Exception:
            await self._finish_update(owner, auto, 'gateway_update_failed')
            raise
        return await self._finish_update(owner, auto, '')

    async def _finish_update(self, owner, auto, error):
        async with self.service.lock('gateway-config'):
            value, private = await self.config()
            value['lastUpdate'] = {'atMs': now(), 'ok': not error, 'auto': auto, 'error': error, 'build': value['build']}
            await self.store(value, private)
        if auto:
            if error:
                log.warning('网关自动更新失败', 错误=error)
            else:
                log.info('网关已自动更新', 构建=value['build'][:12])
            await self._notify_update(owner, value, error)
        return {**self.public_config(value, private), 'updating': False}

    async def _notify_update(self, owner, value, error):
        from app.instances.notifications import GATEWAY
        if error == 'gateway_update_interrupted':
            text = '主控关闭中断了访问网关自动更新，远端结果待确认；恢复后请检查网关状态。'
        elif error:
            text = (f'访问网关自动更新失败：{UPDATE_ERRORS.get(error, error)}（{error}）。不会自动重试；'
                    '可在「实例」→「网关设置」点「更新网关」重试，主控下次启动时也会再试一次。')
        else:
            build = f'（构建 {value["build"][:12]}）' if value.get('build') else ''
            text = f'访问网关已自动更新{build}，账号和登录状态都保留。'
        try:
            async with self.service.db.write_transaction(label='gateway-update-notice') as conn:
                await self.service._enqueue(conn, 'gateway-update:'+secrets.token_hex(8), {'id': GATEWAY, 'owner': owner}, text)
        except Exception:
            log.warning('网关更新通知写入失败')

    def _maybe_auto_update(self, value):
        """Once per target build after the controller starts, only from a fresh connected status."""
        if (not value['enabled'] or not value.get('autoUpdate') or not value.get('updateAvailable') or value['status'] != 'connected'
                or self.updating or getattr(self.service, 'closing', False) or self._update_target() in self.update_tried):
            return
        self.update_task = asyncio.create_task(self._auto_update(), name='gateway-auto-update')

    async def _auto_update(self):
        value, private = await self.config()
        owner = await self._owner(value, private)
        if owner is None or not await self._server_reachable(owner, value['serverId'] or 'local'):
            return  # Offline or unreachable: tried again at a later status refresh.
        if self.service.closing:
            return
        try:
            await self.update(owner, auto=True)
        except Exception:
            pass  # Recorded in lastUpdate and notified.

    async def _server_reachable(self, owner, server_id):
        try:
            target = await self.servers.target(owner, server_id, nodes=True)
            if target.get('channel') != 'ssh':
                return True  # This machine, or an online node that runs commands.
            return bool((await self.servers.run(target, 'true', timeout=30, source='gateway')).get('ok'))
        except Exception:
            return False

    async def _install(self, target, port, public_url, key, control_port=None):
        if target.get('local'):
            arch = ARCHES.get(platform.machine().lower())
        else:
            arch = ARCHES.get((await self.servers.run(target, 'uname -m', timeout=30, source='gateway'))['stdout'].strip())
        if not arch:
            raise InstanceError('gateway_architecture_unsupported', 409)
        binary = ROOT / 'gateway/bin' / f'openbear-gateway-linux-{arch}'
        if not binary.is_file():
            raise InstanceError('gateway_binary_missing', 409)
        # Settings the controller does not manage (TLS added by hand, webRoot, clientIp) survive a reinstall.
        current = await self.servers.run(target, f'cat {CONFIG_DIR}/gateway.json 2>/dev/null || true', timeout=30, root=True, source='gateway')
        try:
            existing = json.loads(current.get('stdout') or '{}')
        except ValueError:
            existing = {}
        kept = {k: v for k, v in existing.items() if k in UNMANAGED_CONFIG} if isinstance(existing, dict) else {}
        config = {**kept, 'listen': f'0.0.0.0:{port}', 'publicUrl': public_url, 'stateDir': STATE_DIR, 'controlKey': key,
                  **({'controlListen': f'127.0.0.1:{control_port}'} if control_port else {})}
        files = {'openbear-gateway': binary.read_bytes(), 'gateway.json': json.dumps(config, indent=2).encode(),
                 SERVICE: _unit('openbear-gateway').encode()}
        encoded = {name: base64.b64encode(content).decode() for name, content in files.items()}
        # One idempotent script: keeps existing state (accounts, sessions), replaces
        # binary/config/unit, restarts only the gateway service.
        script = f'''set -e
umask 077
{PREFLIGHT}
nologin=$(command -v nologin || echo /bin/false)
id openbear-gateway >/dev/null 2>&1 || useradd --system --user-group --home-dir {STATE_DIR} --no-create-home --shell "$nologin" openbear-gateway
install -d -m 0755 $(dirname {BIN})
install -d -m 0750 -o root -g openbear-gateway {CONFIG_DIR}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
'''
        for name, data in encoded.items():
            script += f"printf '%s' {shlex.quote(data)} | base64 -d > \"$tmp/{name}\"\n"
        script += f'''install -m 0755 "$tmp/openbear-gateway" {BIN}
install -m 0600 -o openbear-gateway -g openbear-gateway "$tmp/gateway.json" {CONFIG_DIR}/gateway.json
install -m 0644 "$tmp/{SERVICE}" /etc/systemd/system/{SERVICE}
systemctl daemon-reload
systemctl enable {SERVICE} >/dev/null 2>&1
systemctl restart {SERVICE}
# A unit that cannot start keeps restarting; report it instead of a later
# connection failure that points at ports and firewalls.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 0.5
  [ "$(systemctl show -p NRestarts --value {SERVICE})" = "0" ] && systemctl is-active --quiet {SERVICE} && [ $_ -ge 4 ] && exit 0
done
echo "gateway-start-failed" >&2
journalctl -u {SERVICE} -n 5 --no-pager >&2 || true
exit 3
'''
        result = await self.servers.run(target, 'sh -s', stdin=script.encode(), timeout=180, root=True, source='gateway')
        if not result['ok']:
            raise InstanceError(_install_error(result), 502)

    async def disable(self, owner):
        """Stop the gateway service. Accounts and sessions stay in its state directory."""
        self.service.require_controller()
        async with self.service.lock('gateway-config'):
            value, private = await self.config()
            if not value['enabled']:
                return self.public_config(value, private)
            target = await self.servers.target(owner, value['serverId'], nodes=True)
            result = await self.servers.run(target, f'systemctl disable --now {SERVICE}', timeout=60, root=True, source='gateway')
            if not result['ok']:
                raise InstanceError('gateway_stop_failed', 502)
            value.update(enabled=False, status='disabled', error='', checkedAtMs=now())
            await self.store(value, private)
            self.service._mesh_changed()
            return self.public_config(value, private)

    async def set_public_url(self, owner, public_url):
        value, _ = await self.config()
        if not value['enabled']:
            raise InstanceError('gateway_not_enabled', 409)
        return await self.enable(owner, {'serverId': value['serverId'], 'port': value['port'], 'publicUrl': public_url})

    async def set_client_ip(self, data):
        if set(data) - {'trustedProxies'} or not isinstance(data.get('trustedProxies'), list):
            raise InstanceError('invalid_gateway_client_ip')
        body = {'source': 'header' if data['trustedProxies'] else 'auto', 'trustedProxies': data['trustedProxies']}
        async with self.service.lock('gateway-config'):
            value, private = await self.config()
            ack = await self.call(value, private, 'PUT', '/control/v1/client-ip', body)
            value['clientIp'] = ack.get('clientIp') or body
            if 'clientIpObserved' in ack:  # A just-trusted proxy is no longer offered.
                value['observed'] = ack['clientIpObserved']
            await self.store(value, private)
            return self.public_config(value, private)

    # ----- instance sync -----
    def schedule_sync(self):
        """Account/route changes reach the gateway without a manual sync button."""
        if self.sync_task and not self.sync_task.done():
            return
        self.sync_task = asyncio.create_task(self.sync_all(), name='gateway-sync')

    async def sync_all(self):
        rows = await many(self.service.db.conn, "SELECT id,owner FROM managed_instances WHERE auth_mode='gateway'")
        for row in rows:
            try:
                await self.sync(row['owner'], row['id'])
            except Exception:
                pass  # The pending state is kept and retried by the monitor.

    async def pull_events(self):
        from app.instances.events import ingest_gateway
        async with self.service.lock('gateway-events'):
            value, private = await self.config()
            if not value['enabled']:
                return
            result = await self.call(value, private, 'GET', f'/control/v1/events?after={int(value["eventSeq"])}')
            handled = await ingest_gateway(self.service, result.get('items') or [])
            if handled > value['eventSeq']:
                async with self.service.lock('gateway-config'):
                    value, private = await self.config()
                    value['eventSeq'] = max(value['eventSeq'], handled)
                    await self.store(value, private)

    async def persist(self, row, item, protected, *, archived=False):
        async with self.service.db.write_transaction(label='instance-gateway-state') as conn:
            await conn.execute("UPDATE managed_instances SET public_json=?,protected=?,auth_mode=?,revision=revision+1,status=CASE WHEN ? THEN ? ELSE status END WHERE id=?",
                (json.dumps(item), self.service.vault.seal(protected), 'gateway', archived, 'archived', row['id']))
        if archived:
            self.service._mesh_changed(row['owner'])

    async def sync(self, owner, identity, *, expected_revision=None):
        self.service.require_controller()
        async with self.service.lock('gateway-config'):
            value, private = await self.config()
        return await self._sync(owner, identity, value, private, expected_revision=expected_revision)

    async def apply(self, value, private, identity, body):
        ack = await self.call(value, private, 'PUT', '/control/v1/instances/'+identity, body)
        if ack.get('id') != identity or ack.get('revision') != body['revision'] or ack.get('enabled') is not body['enabled'] or not isinstance(ack.get('username'), str) or not ack['username']:
            raise InstanceError('gateway_ack_mismatch', 502)
        return ack

    async def _sync(self, owner, identity, value, private, *, expected_revision=None):
        async with self.service.lock(identity):
            row = await self.service.row(owner, identity, active=False)
            if expected_revision is not None and row['revision'] != expected_revision:
                raise InstanceError('instance_changed_after_confirmation', 409)
            item, protected = json.loads(row['public_json']), self.service.vault.open(row['protected'])
            state = item.setdefault('gateway', {})
            if state.get('status') == 'synced' and state.get('appliedRevision') == state.get('revision'):
                return {'ok': True, 'synced': True, **await self.service.get(owner, identity)}
            try:
                if not value['enabled']:
                    raise InstanceError('gateway_not_enabled', 409)
                if not item.get('managementUrl'):
                    raise InstanceError('instance_address_pending', 409)
                unresolved = protected.get('gatewayUnacked')
                if unresolved:
                    ack = await self.apply(value, private, identity, unresolved)
                    state.update(appliedRevision=ack['revision'], appliedEnabled=ack['enabled'], mayBeEnabled=ack['enabled'])
                    item['account'] = {'username': ack['username']}
                    protected.pop('gatewayUnacked', None)
                    await self.persist(row, item, protected)
                body = protected.get('gatewayPending')
                if body is None:
                    body = {'revision': state['revision'], 'upstream': item['managementUrl'], 'businessKey': protected['businessKey'],
                            'enabled': state['enabled'], **protected.get('gatewayAccount', {})}
                    protected['gatewayPending'] = body
                if state.get('appliedRevision', 0) == 0 and (not body.get('username') or not body.get('password')):
                    raise InstanceError('gateway_initial_account_required', 409)
                # Persist exact revision/body before I/O. A lost ACK retries the same
                # payload; after ACK account fields are never replayed on later revisions.
                state['mayBeEnabled'] = bool(state.get('mayBeEnabled') or body['enabled'])
                await self.persist(row, item, protected)
                ack = await self.apply(value, private, identity, body)
                state.update(status='synced', appliedRevision=body['revision'], appliedEnabled=body['enabled'], mayBeEnabled=body['enabled'], error='', syncedAtMs=now())
                item['account'] = {'username': ack['username']}
                protected.pop('gatewayPending', None)
                protected.pop('gatewayAccount', None)
                archived = bool(item.get('archivePending') and not ack['enabled'])
                if archived:
                    item.pop('archivePending', None)
                await self.persist(row, item, protected, archived=archived)
                if archived:
                    async with self.service.db.write_transaction(label='gateway-archive-notifications') as conn:
                        await conn.execute("UPDATE instance_notifications SET status='suppressed',updated_ms=? WHERE instance_id=? AND status='pending'", (now(), identity))
                return {'ok': True, 'synced': True, **await self.service.get(owner, identity)}
            except InstanceError as exc:
                state.update(status='pending_sync', error=exc.error)
                await self.persist(row, item, protected)
                return {'ok': True, 'synced': False, 'error': exc.error, **await self.service.get(owner, identity)}

    async def probe(self, row):
        """Ask the gateway to reach the instance the way a browser request would."""
        value, private = await self.config()
        item = json.loads(row['public_json'])
        state = item.get('gateway', {})
        if not value['enabled'] or state.get('status') != 'synced' or not state.get('appliedEnabled'):
            return {'ok': False, 'error': 'gateway_sync_required'}
        try:
            data = await self.call(value, private, 'GET', '/control/v1/instances/'+row['id']+'/probe')
        except InstanceError as exc:
            return {'ok': False, 'error': exc.error}
        ok = data.get('id') == row['id'] and data.get('revision') == state.get('appliedRevision') and data.get('reachable') is True
        return {'ok': ok, 'error': '' if ok else 'gateway_route_unverified', 'status': network.clean_status(data.get('status'))}
