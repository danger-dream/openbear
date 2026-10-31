"""Mesh networking between enrolled servers: the draft and applied documents,
revisions, installs, rollouts, catch-up after a node reconnects, TLS pins and
the status nodes report. One mesh per owner."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import ipaddress
import json
import math
import re
import time
import uuid

from app.instances.contracts import InstanceError
from app.instances.servers import LOCAL
from app.servers import mesh_apply, mesh_change, mesh_lines, mesh_path, mesh_status
from app.servers import mesh_model as model
from app.servers.mesh_config import (
    LEGACY_SCHEMAS,
    _key32,
    build,
    can_dial,
    clean_schemas,
    config_hash,
    exports,
    gateway_routes,
    instance_access,
    is_lan,
    key_bytes,
    routes_and_conflicts,
)
from app.servers.mesh_trend import RANGES as TREND_RANGES
from app.servers.mesh_trend import MeshTrend, rtt_basis
from app.servers.onboarding import (
    MESH_ACCESS,
    MESH_ACCESS_FEATURE,
    MESH_ACCESS_NAME,
    resolve,
    uses_mesh,
)
from app.servers.reminders import quoted
from app.webhooks.repository import many, one

REVISIONS_KEPT = 10
# Two machines with one mesh key: almost always a clone of a member's disk or template.
KEY_CLONE_ADVICE = ('多半是从那台机器的系统快照或模板克隆出来的。请在这台机器上删掉组网目录里的 keys（Linux 是 '
                    '/var/lib/openbear-node/mesh/keys，Windows 是 C:\\ProgramData\\OpenBear\\node\\mesh\\keys，macOS 是 '
                    '/etc/openbear-node/mesh/keys），重启它的 openbear-mesh 服务，再加入一次；它会生成自己的密钥')
# Stage of the draft as it was before a member was marked for removal (MeshService.remove).
REMOVED = "removed:"
INSTALL_TIMEOUT = 180
# The node waits up to 15 seconds for the data plane before it answers.
APPLY_TIMEOUT = 40
UNINSTALL_TIMEOUT = 60
TLS_CALL_TIMEOUT = 60
STATUS_CALL_TIMEOUT = 30
# The node stops the data plane, swaps the copy and waits up to 30 seconds for it to be ready.
UPGRADE_TIMEOUT = 120
# Reports come at most a minute apart.
UPGRADE_READY_WAIT = 120
# A member waits this long for relays reconnecting with it to queue their upgrades, then for them all.
MEMBER_UPGRADE_SETTLE = 15
MEMBER_UPGRADE_WAIT = 900
# A copy that did not get ready is tried once more: its entry relay restarting under it is the usual cause.
UPGRADE_RETRY_DELAY = 60
# A call cut by what it set off (a config or upgrade restarting the path to the node) waits this
# long for the node to come back before it counts as failed.
RECONNECT_WAIT = 180
RECONNECT_POLL = 3
TLS_CHECK_INTERVAL_S = 3600
TLS_REFRESH_MS = 86400000
STATUS_SAVE_MS = 60000
# A status that changed is saved at most this often; the one in memory is always the latest.
STATUS_SAVE_MIN_MS = 10000
# A member's public address (its reachability check) or its IPv4 must hold this long before the
# configs of those that punch towards it or dial it follow it: one that flips back and forth costs nothing.
PUBLIC_ADDR_SETTLE_S = 30
LAYOUT_KEPT = 500
LAYOUT_SPAN = 100000
# Looked up by the node after it connects: until then its hello has none, and the last known ones stand.
PUBLIC_IPS = ('ipv4', 'ipv6')
# A member whose node says its mesh service is gone (mesh_nodes.state 'missing').
MISSING_ERROR = 'mesh_service_missing'
NOT_INSTALLED = 'mesh_not_installed'
# net.ports: whether the listen ports a draft gives a member are free on its machine.
PORTS_FEATURE = 'net.ports'
PORT_CHECK_MS = 300_000
PORT_CHECK_TIMEOUT = 5
# A node that did not answer in time is not asked again for this long: reading the overview or
# saving does not wait out its silence every time.
PORT_NO_ANSWER_MS = 45_000
_KEY = re.compile(r'[A-Za-z0-9+/]{42,86}={0,2}')


def _valid_key(value):
    """A node's public key as its data plane takes it: base64 of exactly 32 bytes."""
    return isinstance(value, str) and bool(_KEY.fullmatch(value)) and _key32(value)
_FINGERPRINT = re.compile(r'[0-9a-f]{64}')
_BUILD = _FINGERPRINT
REFUSALS = {'node_not_enrolled': '未安装节点', 'node_offline': '节点离线', 'capability_disabled': '未开启执行命令',
            'node_update_required': '节点版本过旧，需要先更新', 'platform_unsupported': '平台不支持',
            'node_protocol_outdated': '节点版本过旧，和主控不兼容，需要先更新节点',
            'controller_protocol_outdated': '节点比主控新，和主控不兼容，需要先更新主控'}
KEY_COLUMNS = ('public_key', 'control_key', 'sign_key', 'e2e_key')
SECURITY_KEYS = (('sign_key', 'signKey'), ('e2e_key', 'e2eKey'))
INSTALL_KEY_NAMES = {'publicKey': '组网公钥', 'controlKey': '控制密钥'}
MEMBER_FIELDS = (('role', '角色'), ('relayMode', '中转类型'), ('address', '地址'), ('publicEndpoint', '对外地址'), ('listen', '监听端口'), ('entry', '入口'),
                 ('gateway', '网关'), ('tags', '标签'), ('uploadLimitMbps', '发送速率'), ('controlAddress', '控制地址'))


def now_ms():
    return int(time.time() * 1000)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _reported_schemas(result):
    """configSchemas from mesh.status: its own, else its status.json's; a status.json without
    one is from a data plane that reads only the first schema. None while nothing says."""
    status = result.get('status') if isinstance(result.get('status'), dict) else None
    return (clean_schemas(result.get('configSchemas')) or (status is not None and clean_schemas(status.get('configSchemas')))
            or (LEGACY_SCHEMAS if status is not None else None))


def _cert(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not 0 < len(raw['id']) <= 64:
        return None
    fingerprint = str(raw.get('fingerprint') or '').lower().replace(':', '')
    if not _FINGERPRINT.fullmatch(fingerprint):
        return None
    return {'id': raw['id'], 'fingerprint': fingerprint,
            **{k: raw[k] if type(raw.get(k)) is int and 0 <= raw[k] < 2 ** 53 else 0 for k in ('notBefore', 'notAfter')}}


# No interface is on these: one reported (0.0.0.0/0) would overlap every mesh prefix.
_NO_LAN = tuple(ipaddress.ip_network(n) for n in ('0.0.0.0/8', '127.0.0.0/8', '224.0.0.0/3'))


def clean_local(raw):
    """localPrefixes from mesh.install or mesh.status: IPv4 networks on the node's interfaces,
    without ones wider than /8 or in _NO_LAN. None when the node did not send the field, so a
    stored list is kept."""
    if not isinstance(raw, list):
        return None
    out = []
    for value in raw[:64]:
        with contextlib.suppress(TypeError, ValueError):
            network = ipaddress.ip_network(value if isinstance(value, str) else '', strict=False)
            if network.version == 4 and network.prefixlen >= 8 and not any(network.overlaps(n) for n in _NO_LAN):
                out.append(str(network))
    return list(dict.fromkeys(out))


def _security_keys(result):
    """{column: key}: the signing and end-to-end public keys in a node's answer, without malformed ones."""
    keys = {column: result.get(field) for column, field in SECURITY_KEYS}
    return {k: v for k, v in keys.items() if _valid_key(v)}


def _ipv4(value):
    """value when it is an IPv4 literal, else '' (a record from before addresses were checked)."""
    try:
        return value if isinstance(value, str) and ipaddress.ip_address(value).version == 4 else ''
    except ValueError:
        return ''


def clean_addresses(raw):
    """localAddresses from mesh.install or mesh.status: the node's private IPv4 addresses with
    their prefix length (10.20.20.7/24). None when the node did not send the field."""
    if not isinstance(raw, list):
        return None
    out = []
    for value in raw[:64]:
        with contextlib.suppress(TypeError, ValueError):
            iface = ipaddress.ip_interface(value if isinstance(value, str) else '')
            if is_lan(iface.ip):
                out.append(str(iface))
    return list(dict.fromkeys(out))


ROUTE_TABLE = '/proc/net/route'
MESH_INTERFACE = 'obmesh0'


def host_routes(path=None):
    """[{net, dev}]: the IPv4 routes of the controller's own machine (the main table), without the
    default route, loopback and the mesh's own interface. Its node reports only the networks on
    its interfaces, and a route through a VPN (10.10.10.0/24 over WireGuard) would be lost to a
    gateway subnet the mesh routes there."""
    try:
        with open(path or ROUTE_TABLE, encoding='ascii') as f:
            lines = f.read().splitlines()[1:]
    except (OSError, ValueError):
        return []
    out = []
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[0] in ('lo', MESH_INTERFACE):
            continue
        with contextlib.suppress(ValueError):
            dest, mask = (ipaddress.IPv4Address(int(v, 16).to_bytes(4, 'little')) for v in (fields[1], fields[7]))
            net = ipaddress.ip_network(f'{dest}/{mask}', strict=False)
            if net.prefixlen:
                out.append({'net': str(net), 'dev': fields[0][:32]})
    return list({(r['net'], r['dev']): r for r in out}.values())


def clean_tls(raw):
    """mesh.tls answer: the active certificate and, during a rotation, the pending one."""
    active = _cert((raw or {}).get('active'))
    return {'active': active, 'pending': _cert(raw.get('pending'))} if active else None


def dialed_pins(config, relay):
    """The TLS pins one node's config holds for a relay, one list per way it connects there."""
    for c in (config.get('entry') or {}).get('candidates', []):
        if c['relay'] == relay and 'tlsPins' in c:
            yield c['tlsPins']
    for link in config.get('links', []):
        if link['peer'] == relay and 'tlsPins' in link:
            yield link['tlsPins']


class Mesh:
    def __init__(self, service):
        self.service = service
        self.owners = {}
        self.status = {}
        self.watches = {}
        self.rollouts = {}
        self.locks = {}
        self.edits = {}
        self.installing = {}  # server_id -> (owner, started ms) while mesh.install runs
        self.names = {}
        self.trend_store = MeshTrend(service.metrics)
        self.line_store = mesh_lines.LineTrend(service.metrics)
        self.applied_cache = {}  # owner -> the applied revision as _applied gives it, until another is issued
        self.applied_issued = {}  # owner -> how many revisions were issued since the start, to drop a load that raced one
        self.port_checks = {}  # (server id, ports asked) -> (ms it holds until, net.ports answer)
        self.port_pending = {}  # (server id, ports asked) -> the check under way, shared by everyone asking
        self.upgrades = {}
        self.relay_upgrades = {}
        self.ready_waits = {}
        self.applying = set()  # owners with an apply or rollback from its start until its rollout ends
        self.change_watching = set()
        self.refused = {}  # server id -> hash of the latest config it refused; its data plane retries that one itself
        self.resending = set()
        self.schemas = {}  # server id -> (min, max) config schemas its data plane reads, as last stored
        self.catching = set()  # server ids with a catch-up running
        self.catch_again = set()  # of those, ones that said hello again meanwhile
        self.node_locks = {}  # server id -> held while a config or uninstall goes to that node
        self.reconcile_waiting = {}  # owner -> the reissue waiting for the lock, which every later change joins
        self.addr_settling = {}  # server id -> monotonic time its public address last changed
        self.ipv4s = {}  # server id -> (the IPv4 configs use, another it reports, monotonic time since it does)
        self.public_ips = {}  # server id -> the same for the public IP its reachability check verified
        self.key_conflicts = {}  # server id -> {signKey|e2eKey: the member whose key it reported}, from its last report of them
        self.alerted = {}  # server id -> {alert kind: ms one last went out}, for alerts not from its reports (mesh_status.Watch)
        self.missing_held = set()  # server ids missing whose alert waits out the cooldown

    @property
    def db(self):
        return self.service.db

    @property
    def hub(self):
        return self.service.hub

    def _lock(self, owner):
        """Held while configs go out: one rollout, catch-up or reissue at a time."""
        return self.locks.setdefault(owner, asyncio.Lock())

    def _edit(self, owner):
        """Held while the draft is read and written back. Taken after _lock, never before."""
        return self.edits.setdefault(owner, asyncio.Lock())

    def _node_lock(self, sid):
        """Held while one node is sent a config or uninstalled, so a catch-up and a rollout
        never cross on it. Taken after _lock when both are held, never before."""
        return self.node_locks.setdefault(sid, asyncio.Lock())

    def _sending(self, sid):
        lock = self.node_locks.get(sid)
        return bool(lock and lock.locked())

    async def _call(self, owner, sid, method, params, *, timeout):
        """service._call_newer for the mesh feature, keeping the node's whole error: a refused
        config says why ("invalid_config: ..."), well past the 60 characters an error code gets."""
        session = self.hub.sessions.get(sid)
        if session and session.incompatible:
            raise InstanceError(session.incompatible, 409)
        if session and session.caps.get('exec') and 'mesh' not in session.features:
            raise InstanceError('node_update_required', 409)
        if not await one(self.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=? AND owner=?', (sid, owner)):
            raise InstanceError('node_not_enrolled', 404)
        try:
            reply = await self.hub.call(sid, method, params, timeout)
        except LookupError:
            raise InstanceError('node_offline', 409) from None
        except (ConnectionError, TimeoutError):
            raise InstanceError('node_connection_lost', 502) from None
        if not reply.get('ok'):
            error = str(reply.get('error') or 'node_error')[:500]
            raise InstanceError('node_update_required' if error == 'unsupported_method' else error, 409)
        return reply.get('result') if isinstance(reply.get('result'), dict) else {}

    async def start(self):
        for row in await many(self.db.conn, 'SELECT server_id,owner,status_json,events_json,updated_ms FROM mesh_nodes'):
            self.owners[row['server_id']] = row['owner']
            if row['status_json']:
                # Cleaned again: a status saved before a field was added gains its default.
                self.status[row['server_id']] = {'status': mesh_status.clean_status(json.loads(row['status_json'])), 'at': row['updated_ms'], 'saved': row['updated_ms'],
                                                 # Folded again: a list stored before repeats were folded.
                                                 'events': mesh_status.merge_events(json.loads(row['events_json'] or '[]')),
                                                 # Alerts this status already raised are not raised again by the next report.
                                                 'restored': True}
        for row in await many(self.db.conn, 'SELECT owner FROM mesh_changes'):
            await mesh_change.recover(self, row['owner'])
        # A rollout cut short by a restart: the nodes it did not reach get it on reconnect.
        async with self.db.write_transaction(label='mesh-revision-state') as conn:
            await conn.execute("UPDATE mesh_revisions SET status='partial' WHERE status='applying'")
        await self.trend_store.start()
        await self.line_store.start()
        self.service.spawn(self._tls_loop())

    # ----- storage -----
    async def _load(self, owner, stage):
        row = await one(self.db.conn, 'SELECT settings_json,relay_mode FROM mesh_settings WHERE owner=? AND stage=?', (owner, stage))
        if not row:
            return model.empty_doc() if stage == 'applied' else await self._load(owner, 'applied')
        args = (owner, stage)
        members = [json.loads(r['member_json']) for r in await many(self.db.conn, 'SELECT member_json FROM mesh_members WHERE owner=? AND stage=? ORDER BY sort', args)]
        links = await many(self.db.conn, 'SELECT kind,link_json FROM mesh_links WHERE owner=? AND stage=? ORDER BY kind,sort', args)
        rules = [json.loads(r['rule_json']) for r in await many(self.db.conn, 'SELECT rule_json FROM mesh_rules WHERE owner=? AND stage=? ORDER BY sort', args)]
        return model.upgrade({'settings': json.loads(row['settings_json']), 'members': members,
                              'relayLinks': {'mode': row['relay_mode'], 'pairs': [json.loads(r['link_json']) for r in links if r['kind'] == 'relay']},
                              'directLinks': [json.loads(r['link_json']) for r in links if r['kind'] == 'direct'], 'rules': rules})

    @staticmethod
    async def _save(conn, owner, stage, doc):
        for table in ('mesh_settings', 'mesh_members', 'mesh_links', 'mesh_rules'):
            await conn.execute(f'DELETE FROM {table} WHERE owner=? AND stage=?', (owner, stage))
        await conn.execute('INSERT INTO mesh_settings VALUES(?,?,?,?)', (owner, stage, dumps(doc['settings']), doc['relayLinks']['mode']))
        for i, m in enumerate(doc['members']):
            await conn.execute('INSERT INTO mesh_members VALUES(?,?,?,?,?)', (owner, stage, m['serverId'], i, dumps(m)))
        for kind, items in (('relay', doc['relayLinks']['pairs']), ('direct', doc['directLinks'])):
            for i, link in enumerate(items):
                await conn.execute('INSERT INTO mesh_links VALUES(?,?,?,?,?)', (owner, stage, kind, i, dumps(link)))
        for i, rule in enumerate(doc['rules']):
            await conn.execute('INSERT INTO mesh_rules VALUES(?,?,?,?)', (owner, stage, i, dumps(rule)))

    async def _save_draft(self, owner, doc):
        async with self.db.write_transaction(label='mesh-draft') as conn:
            await self._save(conn, owner, 'draft', doc)

    async def _network(self, owner):
        row = await one(self.db.conn, 'SELECT * FROM mesh_network WHERE owner=?', (owner,))
        return row or {'owner': owner, 'network_id': '', 'revision': 0, 'applied_revision': 0, 'applied_ms': 0}

    async def _stopped(self, owner, net, sid):
        """The applied revision's rollout stopped at a failure while a relay does not run it: no
        other node gets it by catching up (it would dial a relay that never took it) until the
        relays do, or it is applied again or rolled back. Relays come first, as in a rollout."""
        row = await one(self.db.conn, 'SELECT status FROM mesh_revisions WHERE owner=? AND revision=?', (owner, net['applied_revision']))
        if not row or row['status'] != 'failed':
            return False
        applied = await self._applied(owner)
        relays = [m for m, member in applied['members'].items() if member['role'] == 'relay' and not member['removing']]
        if sid in relays:
            return False
        confirmed = {r['server_id']: r['confirmed_hash'] for r in await many(self.db.conn, 'SELECT server_id,confirmed_hash FROM mesh_nodes WHERE owner=?', (owner,))}
        return any(confirmed.get(r) != applied['hashes'].get(r) for r in relays)

    async def _latest(self, owner, net):
        row = await one(self.db.conn, 'SELECT configs_json FROM mesh_revisions WHERE owner=? AND revision=?', (owner, net['applied_revision']))
        return json.loads(row['configs_json']) if row else {}

    async def _applied(self, owner):
        """The applied revision, read and parsed once until another is issued: {revision, doc, configs,
        hashes {serverId: config_hash}, members {serverId: member}, lines}. Shared and never changed
        by callers; every report reads it."""
        cached = self.applied_cache.get(owner)
        if cached is not None:
            return cached
        issued = self.applied_issued.get(owner, 0)
        net = await self._network(owner)
        doc, configs = await self._load(owner, 'applied'), await self._latest(owner, net)
        value = {'revision': net['applied_revision'], 'doc': doc, 'configs': configs,
                 'hashes': {sid: config_hash(c) for sid, c in configs.items()},
                 'members': {m['serverId']: m for m in doc['members']}, 'lines': mesh_lines.catalogue(doc, configs)}
        # A revision issued while this was read: what was read may be the old one.
        if self.applied_issued.get(owner, 0) == issued:
            self.applied_cache[owner] = value
        return value

    def _applied_changed(self, owner):
        self.applied_issued[owner] = self.applied_issued.get(owner, 0) + 1
        self.applied_cache.pop(owner, None)

    async def _rows(self, owner):
        return {r['server_id']: r for r in await many(self.db.conn, 'SELECT * FROM mesh_nodes WHERE owner=?', (owner,))}

    async def _facts(self, owner):
        """What configs and checks need from outside the document: names, platforms, keys, addresses, TLS pins."""
        servers = await self.service.registry.all(owner)
        profiles = await self.service._profiles(owner)
        nodes = await self.service._nodes(owner)
        rows = await self._rows(owner)
        facts = {}
        for server in servers:
            sid = server['id']
            node = nodes.get(sid)
            stored = json.loads(node['info_json']) if node else {}
            session = self.hub.sessions.get(sid)
            known = stored.get('info') or {}
            info = (session.info if session else None) or known
            # Reconnected, the public address not looked up yet: the last known one, not none,
            # or every config dialing it would get an empty host.
            info = {**info, **{k: known[k] for k in PUBLIC_IPS if not info.get(k) and known.get(k)}}
            name = (profiles.get(sid, {}).get('alias') or '本机') if sid == LOCAL else server['name']
            facts[sid] = {'name': name, 'listed': True, 'enrolled': bool(node), 'online': bool(session),
                          'caps': session.caps if session else stored.get('caps') or {}, 'features': session.features if session else frozenset(),
                          'incompatible': session.incompatible if session else '',
                          'platform': info.get('platform', ''),
                          'ipv4': self._settled(self.ipv4s, owner, sid, _ipv4(info.get('ipv4'))) if sid in rows else _ipv4(info.get('ipv4'))}
        for sid, row in rows.items():
            f = facts.setdefault(sid, {'name': sid, 'listed': False, 'enrolled': False, 'online': False, 'caps': {}, 'features': frozenset(),
                                       'incompatible': '', 'platform': '', 'ipv4': ''})
            f.update(publicKey=row['public_key'], controlKey=row['control_key'], signKey=row['sign_key'], e2eKey=row['e2e_key'],
                     platform=row['platform'] or f['platform'],
                     dataVersion=row['data_version'], localPrefixes=clean_local(json.loads(row['local_json'] or '[]')) or [],
                     configSchemas=clean_schemas(json.loads(row['config_schemas'])) if row['config_schemas'] else None,
                     localAddresses=json.loads(row['addresses_json'] or '[]'),
                     tls=json.loads(row['tls_json']) if row['tls_json'] else None, keyConflict=self.key_conflicts.get(sid))
        if LOCAL in facts:
            # The controller's machine: its port, opened to the members over the mesh, and its routes.
            facts[LOCAL].update(controllerPort=self.service.host.config.web.port, hostRoutes=host_routes())
        # Instances listening on their server's mesh address: opened to the gateway's server alone.
        for sid, entries in (await self._instance_ports(owner)).items():
            if sid in facts:
                facts[sid]['instancePorts'] = entries
        for sid, f in facts.items():
            # What the node's last reachability check found: the verdict for warnings, and a
            # public IP every observer agreed on, the only address punched links may aim at
            # besides what the user entered.
            status = (self.status.get(sid) or {}).get('status')
            f['verdict'] = ((status or {}).get('reachability') or {}).get('verdict', '')
            public = mesh_status.public_ip(status)
            f['publicIp'] = self._settled(self.public_ips, owner, sid, public) if sid in rows else public
            # The IP it was seen at, whatever the verdict: nodes seen at one IP share a NAT.
            f['natIp'] = mesh_status.nat_ip(status)
        self.names.update((sid, f['name']) for sid, f in facts.items())
        return facts

    def _settled(self, store, owner, sid, value):
        """The address configs give for a member (store: ipv4s for its IPv4, public_ips for its
        verified public IP): another it reports once that has held PUBLIC_ADDR_SETTLE_S, whatever
        reads the facts meanwhile. Every config has a relay's IPv4 among its reflectors, and a
        hello reissues with the facts it reads, so one flipping back and forth would reissue them
        each time. The first one known (since the start) is taken at once."""
        known, now = store.get(sid), time.monotonic()
        if known is None or value == known[0]:
            store[sid] = (value, None, 0)
            return value
        used, pending, since = known
        if value != pending:
            pending, since = value, now
        if now - since >= PUBLIC_ADDR_SETTLE_S:
            store[sid] = (value, None, 0)
            return value
        if known[1] != pending:
            store[sid] = (used, pending, since)
            self._settle_addr(owner, sid)
        return used

    async def _instance_ports(self, owner):
        """{serverId: [{id, name, port, gateway, bind}]}: the owner's instances, not archived, whose port
        listens on an address besides loopback (bind: their server's mesh address when placed), while the gateway is enabled; gateway is the server it runs on,
        the one source mesh_config.instance_access lets through to them."""
        instances = getattr(self.service, 'instances', None)
        if instances is None:
            return {}
        from app.instances.remote import mesh_bind
        value, _ = await instances.gateway.config()
        if not value['enabled']:
            return {}
        gateway = value['serverId'] or LOCAL
        out = {}
        for row in await many(self.db.conn, "SELECT id,public_json FROM managed_instances WHERE owner=? AND status!='archived' ORDER BY id", (owner,)):
            item = json.loads(row['public_json'])
            port, sid = item.get('port'), item.get('serverId')
            bind = mesh_bind({'managementUrl': '', **item})
            if type(port) is int and 0 < port < 65536 and sid and sid != gateway and bind:
                out.setdefault(sid, []).append({'id': row['id'], 'name': str(item.get('name') or row['id'])[:80], 'port': port, 'gateway': gateway, 'bind': bind})
        return out

    def instances_changed(self, owner=None):
        """An instance placed, moved, given another port or archived, or the gateway enabled, moved or
        disabled: the automatic rules for instances on a mesh address follow, through reconcile (a new
        revision only when a config changed). owner None: every owner with an applied mesh, as the
        gateway serves them all."""
        async def run():
            if owner is not None:
                owners = [owner]
            else:
                owners = [r['owner'] for r in await many(self.db.conn, 'SELECT owner FROM mesh_network WHERE applied_revision>0')]
            for o in owners:
                self._reconcile_soon(o)
        self.service.spawn(run())

    # ----- the controller's access point over the mesh -----
    def access_of(self, doc, capable=frozenset()):
        """(meshAccess, members, eligible) by an applied document: the built-in access point,
        usable while the controller's own machine is a member with a mesh address; the other
        members; and those of them whose nodes take a mesh address (capable), the servers that
        may choose it. Its control connection is end-to-end encrypted, so plain http."""
        live = {m['serverId']: m for m in model.active(doc)}
        local = live.get(LOCAL)
        reason = 'controller_not_member' if not local else '' if local['address'] else 'controller_no_address'
        url = f"http://{local['address']}:{self.service.host.config.web.port}" if not reason else ''
        if url:
            from app.servers.service import node_url
            try:
                node_url(url)
            except InstanceError:
                reason, url = 'controller_url_requires_https', ''
        members = frozenset(sid for sid in live if sid != LOCAL)
        return ({'id': MESH_ACCESS, 'name': MESH_ACCESS_NAME, 'url': url, 'available': not reason, 'reason': reason},
                members, members & capable)

    async def access(self, owner):
        return self.access_of(await self._load(owner, 'applied'), await self.mesh_capable(owner))

    async def mesh_capable(self, owner):
        """Servers whose nodes have MESH_ACCESS_FEATURE: by the live session, else as last recorded."""
        out = set()
        for row in await many(self.db.conn, 'SELECT server_id,info_json FROM server_nodes WHERE owner=?', (owner,)):
            session = self.hub.sessions.get(row['server_id'])
            features = session.features if session is not None else json.loads(row['info_json']).get('features') or ()
            if MESH_ACCESS_FEATURE in features:
                out.add(row['server_id'])
        return frozenset(out)

    @staticmethod
    def access_point(access, sid):
        """The mesh access point as resolve() takes it for one server, or None when it cannot use it now."""
        view, _, eligible = access
        return {k: view[k] for k in ('id', 'name', 'url')} if view['available'] and sid in eligible else None

    async def _access_moves(self, owner, before, after):
        """Servers whose access addresses differ between two applied documents: (leaving, joining),
        those whose new addresses hold no mesh one and can move before the configs go out, and
        those moving onto the mesh, who can only once theirs are in place."""
        capable = await self.mesh_capable(owner)
        old, new = self.access_of(before, capable), self.access_of(after, capable)
        if old == new:
            return [], []
        settings, profiles = await self.service.settings(), await self.service._profiles(owner)
        enrolled = {r['server_id'] for r in await many(self.db.conn, 'SELECT server_id FROM server_nodes WHERE owner=?', (owner,))}
        leaving, joining = [], []
        for sid, profile in profiles.items():
            if sid == LOCAL or sid not in enrolled or not uses_mesh(profile):
                continue
            then = [p and p['url'] for p in resolve(settings, profile, self.access_point(old, sid))]
            now = [p and p['url'] for p in resolve(settings, profile, self.access_point(new, sid))]
            if then != now:
                (joining if new[0]['url'] and new[0]['url'] in now else leaving).append(sid)
        return leaving, joining

    async def _leaving_access(self, owner, before, after):
        leaving, _ = await self._access_moves(owner, before, after)
        if not leaving:
            return
        settings, profiles = await self.service.settings(), await self.service._profiles(owner)
        access = self.access_of(after, await self.mesh_capable(owner))
        moved = []
        for sid in leaving:
            points = resolve(settings, profiles.get(sid), self.access_point(access, sid))
            addresses = tuple(p['url'] if p else '' for p in points)
            result = await self.service.onboarding.push_one(owner, sid, addresses)
            if not result['ok'] and result.get('error') == 'new_url_unreachable' and addresses[1]:
                result = await self.service.onboarding.push_one(owner, sid, (addresses[1], addresses[0]))
                if result['ok']:
                    await self.service.mark_access_pending(sid, True)
            if not result['ok']:
                for pending in [*moved, sid]:
                    await self.service.mark_access_pending(pending, True)
                error = InstanceError('mesh_access_move_failed', 409)
                error.reason = f'{self._name(sid)}：{result.get("error") or "未确认迁出"}；保留旧网络和成员，尚未下发配置或卸载'
                raise error
            moved.append(sid)

    async def _move_access(self, owner, sids):
        """Send the servers their access addresses again; one not reached gets them when it reconnects."""
        for sid in sids:
            await self.service.mark_access_pending(sid, True)
        if sids:
            await self.service.onboarding.push(owner, sids)

    # ----- page -----
    async def view(self, owner, *, ports='cached'):
        """The overview. ports: how listen ports are checked, as _ports takes it: 'cached' for reading it
        (a check not answered yet is left to run), 'wait' after the draft changed."""
        draft, applied = await self._load(owner, 'draft'), await self._load(owner, 'applied')
        facts, rows, net = await self._facts(owner), await self._rows(owner), await self._network(owner)
        issued = await self._applied(owner)
        joined = {m['serverId'] for m in draft['members']} | {m['serverId'] for m in applied['members']}
        roles = self._roles(draft, applied)
        rollout = self.rollouts.get(owner)
        pending_change = await mesh_change.load(self, owner)
        routes, conflicts = routes_and_conflicts(draft, facts)
        warnings = [*model.hints(draft, facts, conflicts, applied), *await self._port_warnings(owner, draft, applied, facts, mode=ports)]
        return {'ok': True, 'settings': draft['settings'], 'members': [self._member_view(m, facts, draft['settings']['prefix']) for m in draft['members']],
                'relayLinks': draft['relayLinks'], 'directLinks': draft['directLinks'], 'rules': draft['rules'],
                # Not the user's to edit: the gateway's server reaching instances on a mesh address.
                'autoRules': [{'kind': 'instance', 'proto': 'tcp', **a} for a in instance_access(draft, facts)],
                'applied': {'revision': net['applied_revision'], 'at': net['applied_ms']},
                'draft': {'dirty': draft != applied, 'changes': self._changes(applied, draft, facts)},
                'nodes': {sid: self._node_view(sid, row, issued['hashes'], roles.get(sid)) for sid, row in rows.items() if sid in joined or row['state'] == 'cleanup'},
                'installing': [{'serverId': sid, 'name': facts.get(sid, {}).get('name') or sid, 'platform': facts.get(sid, {}).get('platform', ''),
                                'startedAt': started} for sid, (who, started) in self.installing.items() if who == owner],
                'layout': await self._layout(owner),
                # Who gets a route into each announced subnet, by the draft's rules.
                'exports': exports(draft, routes),
                'eligible': self._eligible(draft, facts),
                'apply': {**(rollout.view() if rollout else {'running': False, 'revision': net['applied_revision'], 'progress': []}),
                          **({'recovery': 'restoring'} if pending_change and pending_change['phase'] == 'aborting' else {})},
                # Each line of the applied mesh as both ends' latest reports show it.
                'lines': mesh_lines.view(issued['lines'], self.status, now_ms()),
                'errors': model.validate(draft, facts, applied=applied), 'warnings': warnings}

    async def _ports(self, sid, query, facts, *, mode='wait'):
        """net.ports for one member: (result, '') or (None, why it was not checked: offline, no_feature,
        no_answer, pending or the node's error). Answers are kept PORT_CHECK_MS per set of ports asked,
        no answer in time PORT_NO_ANSWER_MS. mode: 'wait' takes a kept answer, else the check, joining
        one under way; 'cached' takes a kept answer, else leaves the check running in the background
        and says pending; 'fresh' (apply) asks the node again whatever is kept."""
        f = facts.get(sid) or {}
        if not f.get('online') or sid not in self.hub.sessions:
            return None, 'offline'
        if PORTS_FEATURE not in f.get('features', ()):
            return None, 'no_feature'
        key = (sid, json.dumps(query, sort_keys=True))
        if mode == 'fresh':
            return await self._check_ports(key, sid, query)
        cached = self.port_checks.get(key)
        if cached and now_ms() < cached[0]:
            return cached[1]
        task = self.port_pending.get(key)
        if task is None:
            task = self.port_pending[key] = self.service.spawn(self._shared_port_check(key, sid, query))
        if mode == 'cached':
            return None, 'pending'
        # Shielded: a caller that goes away does not cancel the check others wait on.
        return await asyncio.shield(task)

    async def _shared_port_check(self, key, sid, query):
        try:
            return await self._check_ports(key, sid, query)
        except Exception:  # noqa: BLE001 - nobody may be waiting; the next one asks again
            return None, 'node_error'
        finally:
            self.port_pending.pop(key, None)

    async def _check_ports(self, key, sid, query):
        try:
            reply = await self.hub.call(sid, 'net.ports', query, PORT_CHECK_TIMEOUT)
        except TimeoutError:
            # Connected but silent: kept a while, so the next reads do not wait for it again.
            answer, keep = (None, 'no_answer'), PORT_NO_ANSWER_MS
        except (LookupError, ConnectionError):
            return None, 'offline'
        else:
            if not reply.get('ok') or not isinstance(reply.get('result'), dict):
                error = str(reply.get('error') or 'node_error')[:60]
                answer = (None, 'no_feature' if error == 'unsupported_method' else error)
            else:
                answer = ({proto: {str(k): v for k, v in (reply['result'].get(proto) or {}).items() if isinstance(v, dict)}
                           for proto in ('tcp', 'udp') if isinstance(reply['result'].get(proto), dict)}, '')
            keep = PORT_CHECK_MS
        now = now_ms()
        self.port_checks[key] = (now + keep, answer)
        for old in [k for k, (until, _) in self.port_checks.items() if now >= until]:
            del self.port_checks[old]
        return answer

    async def _port_warnings(self, owner, draft, applied, facts, *, mode='wait'):
        """Listen ports that differ from the applied document (a new member's all of them), checked
        on the member's machine with net.ports (mode as _ports takes it): mesh_port_in_use where another
        program holds one, mesh_port_unchecked where the node is offline, does not answer or cannot
        tell; nothing yet for a check still under way. Ports the mesh service or the node itself holds
        (ours) are fine."""
        before = {m['serverId']: m for m in model.active(applied)}
        index = {m['serverId']: i for i, m in enumerate(draft['members'])}
        asks = {}
        for m in model.active(draft):
            old = before.get(m['serverId'])
            changed = {t: v for t, v in m['listen'].items() if v and (not old or old['listen'].get(t) != v)}
            if changed:
                asks[m['serverId']] = changed
        if not asks:
            return []

        def query(changed):
            return {'tcp': sorted({v for t, v in changed.items() if t != 'udp'}), 'udp': sorted({v for t, v in changed.items() if t == 'udp'})}
        answers = await asyncio.gather(*(self._ports(sid, query(changed), facts, mode=mode) for sid, changed in asks.items()))
        out = model.Errors()
        for (sid, changed), (result, why) in zip(asks.items(), answers):
            p, name = f'members[{index[sid]}]', (facts.get(sid) or {}).get('name') or sid
            if why == 'pending':
                continue  # the next read has its answer
            if result is None:
                reason = {'offline': '离线', 'no_feature': '的节点版本不支持端口检查', 'no_answer': '的节点没有答复'}.get(why, f'的节点没有答复（{why}）')
                ports = '、'.join(f'{model.TRANSPORT_NAMES[t]} {v}' for t, v in changed.items())
                out.add(f'{p}.listen', 'mesh_port_unchecked', f'{name} {reason}，没有检查端口 {ports} 是否被占用')
                continue
            for t, v in changed.items():
                held = (result.get('udp' if t == 'udp' else 'tcp') or {}).get(str(v)) or {}
                if held.get('free') is False and held.get('ours') is not True:
                    process = str(held.get('process') or '')[:64] or '其他程序'
                    pid = held.get('pid') if type(held.get('pid')) is int and held['pid'] > 0 else None
                    out.add(f'{p}.listen.{t}', 'mesh_port_in_use',
                            f'{name} 的 {model.TRANSPORT_NAMES[t]} 端口 {v} 已被 {process}{f"（PID {pid}）" if pid else ""} 占用，'
                            f'组网服务无法监听它。请换一个端口，或先停掉占用它的程序')
        return out

    @staticmethod
    def _member_view(m, facts, prefix=None):
        f = facts.get(m['serverId']) or {}
        # A site relay has no public endpoint, so the panel's IPv4 is not offered as one.
        auto = '' if m['role'] == 'relay' and m.get('relayMode') == 'site' else f.get('ipv4', '')
        return {**m, 'name': f.get('name') or m['serverId'], 'platform': f.get('platform', ''), 'publicEndpointAuto': auto,
                'subnetSuggestions': model.subnet_suggestions(f, prefix or model.DEFAULT_PREFIX)}

    @staticmethod
    def _roles(draft, applied):
        """Each member's role as its data plane runs it: the applied one, else the draft's."""
        return {m['serverId']: m['role'] for doc in (draft, applied) for m in doc['members']}

    def _node_view(self, sid, row, hashes, role=None):
        """hashes: config_hash of each config of the applied revision, as _applied gives them."""
        status = (self.status.get(sid) or {}).get('status')
        current = sid in hashes and row['confirmed_hash'] == hashes[sid]
        failed = bool(row['error']) and not current
        missing = row['state'] == 'missing'
        state = mesh_status.node_state(cleanup=row['state'] == 'cleanup', installing=sid in self.installing, online=sid in self.hub.sessions,
                                       configured=bool(row['confirmed_hash']), current=current, failed=failed, status=status, missing=missing)
        return {'state': state, 'appliedRevision': status['appliedRevision'] if status else row['confirmed_revision'],
                'error': row['error'] if failed or missing else (status or {}).get('error', ''), 'summary': mesh_status.summary(status, role),
                'dataVersion': row['data_version'],
                'upgrade': {'state': self.upgrades.get(sid, ''), 'error': row['upgrade_error'], 'at': row['upgrade_ms']}}

    @staticmethod
    def _changes(applied, draft, facts):
        def name(sid):
            return (facts.get(sid) or {}).get('name') or sid
        out = []
        before = {m['serverId']: m for m in applied['members']}
        # Rules on all members follow members coming and going by themselves; say so.
        covering = '、'.join(r['note'] or r['id'] for r in draft['rules'] if r['src'].get('all') or r['dst'].get('all'))
        joins = f'，规则 {covering} 会自动包含它' if covering else ''
        leaves = f'，规则 {covering} 不再包含它' if covering else ''
        for m in draft['members']:
            sid, old = m['serverId'], before.get(m['serverId'])
            if m['removing']:
                if old:
                    out.append({'kind': 'member.remove', 'serverId': sid, 'text': f'移除成员 {name(sid)}{leaves}'})
            elif not old:
                out.append({'kind': 'member.add', 'serverId': sid, 'text': f'新增成员 {name(sid)}{joins}'})
            elif old != m:
                fields = '、'.join(label for key, label in MEMBER_FIELDS if old[key] != m[key])
                out.append({'kind': 'member.update', 'serverId': sid, 'text': f'修改 {name(sid)} 的{fields}'})
        # Rules on all gateway subnets follow subnets coming and going the same way.
        spanning = '、'.join(r['note'] or r['id'] for r in draft['rules'] if r['src'].get('allSubnets') or r['dst'].get('allSubnets'))
        if spanning:
            for m in draft['members']:
                sid, old = m['serverId'], before.get(m['serverId'])
                then = set((old['gateway'] or {}).get('subnets', [])) if old and not old['removing'] else set()
                now = [] if m['removing'] else (m['gateway'] or {}).get('subnets', [])
                for subnet in now:
                    if subnet not in then:
                        out.append({'kind': 'subnet.add', 'serverId': sid, 'subnet': subnet,
                                    'text': f'{name(sid)} 新增网关网段 {subnet}，规则 {spanning} 会自动包含它'})
                for subnet in sorted(then - set(now)):
                    out.append({'kind': 'subnet.remove', 'serverId': sid, 'subnet': subnet,
                                'text': f'{name(sid)} 移除网关网段 {subnet}，规则 {spanning} 不再包含它'})
        if [m['serverId'] for m in applied['members']] != [m['serverId'] for m in draft['members'] if m['serverId'] in before]:
            out.append({'kind': 'members.order', 'text': '调整成员顺序'})
        for key, kind, text in (('settings', 'settings', '修改网格设置'), ('relayLinks', 'relayLinks', '修改中转之间的连接'),
                                ('directLinks', 'directLinks', '修改直连'), ('rules', 'rules', '修改访问规则')):
            if applied[key] != draft[key]:
                out.append({'kind': kind, 'text': text})
        return out

    @staticmethod
    def _refusal(f):
        if not f['enrolled']:
            return 'node_not_enrolled'
        if not f['online']:
            return 'node_offline'
        if f.get('incompatible'):
            return f['incompatible']
        if not f['caps'].get('exec'):
            return 'capability_disabled'
        if 'mesh' not in f['features']:
            return 'node_update_required'
        if f['platform'] not in model.PLATFORMS:
            return 'platform_unsupported'
        return ''

    def _eligible(self, draft, facts):
        joined = {m['serverId'] for m in draft['members'] if not m['removing']}
        out = []
        for sid, f in facts.items():
            if sid in joined or not f['listed']:
                continue
            refusal = self._refusal(f)
            out.append({'serverId': sid, 'name': f['name'], 'ok': not refusal, 'reason': REFUSALS.get(refusal, '')})
        return out

    async def member(self, owner, server_id):
        draft, applied = await self._load(owner, 'draft'), await self._load(owner, 'applied')
        m = next((m for doc in (draft, applied) for m in doc['members'] if m['serverId'] == server_id), None)
        row = (await self._rows(owner)).get(server_id)
        if not m and not row:
            raise InstanceError('mesh_member_not_found', 404)
        facts, net = await self._facts(owner), await self._network(owner)
        latest = await self._latest(owner, net)
        live = self.status.get(server_id) or {}
        role = self._roles(draft, applied).get(server_id)
        hashes = (await self._applied(owner))['hashes']
        return {'ok': True, 'member': self._member_view(m, facts, draft['settings']['prefix']) if m else None, 'node': self._node_view(server_id, row, hashes, role) if row else None,
                'status': live.get('status'), 'events': list(reversed(live.get('events') or [])), 'config': latest.get(server_id)}

    async def trend(self, owner, server_id, span):
        if span not in TREND_RANGES:
            raise InstanceError('invalid_range')
        draft, applied = await self._load(owner, 'draft'), await self._load(owner, 'applied')
        if not any(m['serverId'] == server_id for doc in (draft, applied) for m in doc['members']) and server_id not in await self._rows(owner):
            raise InstanceError('mesh_member_not_found', 404)
        now = now_ms()
        role = self._roles(draft, applied).get(server_id, 'member')
        # What rttMs is the round trip to over the range: the entries the member used (by the line
        # record), or the relay's links; by its latest report where the record has none yet.
        start = now // 1000 - TREND_RANGES[span][2]
        if role == 'relay':
            peers = await self.line_store.peers('relay', server_id, start)
        else:
            peers = await self.line_store.peers('entry', server_id, start, role=0)
        basis = rtt_basis((self.status.get(server_id) or {}).get('status'), role)
        return {'ok': True, 'range': span, 'points': await self.trend_store.series(server_id, span, now),
                'rtt': {'kind': basis['kind'], 'peers': peers or basis['peers']}}

    async def _lines(self, owner):
        """The applied document's lines, kept until another revision is issued."""
        return (await self._applied(owner))['lines']

    async def line_trend(self, owner, line_id, span):
        if span not in mesh_lines.RANGES:
            raise InstanceError('invalid_range')
        parsed = mesh_lines.parse_id(line_id)
        if not parsed:
            raise InstanceError('invalid_mesh_line')
        now = now_ms()
        line = next((line for line in await self._lines(owner) if line['id'] == line_id), None)
        if not line:
            # A line of the past (its record is kept): both ends must be this owner's members, as for a member's trend.
            draft, applied = await self._load(owner, 'draft'), await self._load(owner, 'applied')
            owned = {m['serverId'] for doc in (draft, applied) for m in doc['members']} | set(await self._rows(owner))
            if not {parsed[1], parsed[2]} <= owned or not await self.line_store.recorded(line_id, now):
                raise InstanceError('mesh_line_not_found', 404)
        summary, series = await self.line_store.series(line_id, span, now)
        kind, a, b, transport = parsed
        return {'ok': True, 'line': {'id': line_id, 'kind': kind, 'a': a, 'b': b, 'transport': line['transport'] if line else transport,
                                     'current': bool(line)},
                'range': span, 'summary': summary, 'series': series}

    async def path(self, owner, src, dst):
        """The path between two applied members as their latest reports show it."""
        if not src or not dst or src == dst:
            raise InstanceError('invalid_mesh_path')
        applied = await self._load(owner, 'applied')
        roles = {m['serverId']: m['role'] for m in model.active(applied)}
        if src not in roles or dst not in roles:
            raise InstanceError('mesh_member_not_found', 404)
        latest = await self._latest(owner, await self._network(owner))
        return {'ok': True, **mesh_path.find(src, dst, roles, self.status, latest, now_ms())}

    async def routes(self, owner, src):
        """From one applied member: the path to every other one, as path() finds it, and to the
        gateway of each subnet it has a route to."""
        if not src:
            raise InstanceError('invalid_mesh_path')
        applied = await self._load(owner, 'applied')
        live = model.active(applied)
        roles = {m['serverId']: m['role'] for m in live}
        if src not in roles:
            raise InstanceError('mesh_member_not_found', 404)
        latest, now = await self._latest(owner, await self._network(owner)), now_ms()
        routes = [{'dst': m['serverId'], **mesh_path.find(src, m['serverId'], roles, self.status, latest, now)} for m in live if m['serverId'] != src]
        subnets = []
        for route in gateway_routes(applied, await self._facts(owner)).get(src, []):
            net = ipaddress.ip_network(route)
            gateway = next((m['serverId'] for m in live if m['serverId'] != src
                            and any(net.overlaps(ipaddress.ip_network(s)) for s in (m['gateway'] or {}).get('subnets', []))), None)
            if gateway:
                subnets.append({'subnet': route, 'gateway': gateway, **mesh_path.find(src, gateway, roles, self.status, latest, now)})
        return {'ok': True, 'routes': routes, 'subnets': subnets}

    # ----- canvas layout: where the editor draws each server; not part of the draft -----
    async def _layout(self, owner):
        rows = await many(self.db.conn, 'SELECT server_id,x,y FROM mesh_layout WHERE owner=? ORDER BY server_id', (owner,))
        return {r['server_id']: {'x': r['x'], 'y': r['y']} for r in rows}

    async def save_layout(self, owner, data):
        positions = data.get('positions') if isinstance(data, dict) and set(data) == {'positions'} else None
        if not isinstance(positions, dict) or len(positions) > LAYOUT_KEPT:
            raise InstanceError('invalid_mesh_layout')

        def coordinate(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and abs(v) <= LAYOUT_SPAN
        for sid, p in positions.items():
            if p is not None and not (isinstance(p, dict) and set(p) == {'x', 'y'} and coordinate(p['x']) and coordinate(p['y'])):
                raise InstanceError('invalid_mesh_layout')
        owned = {s['id'] for s in await self.service.registry.all(owner)}
        if any(p is not None and sid not in owned for sid, p in positions.items()):
            raise InstanceError('server_not_found', 404)
        async with self.db.write_transaction(label='mesh-layout') as conn:
            for sid, p in positions.items():
                if p is None:
                    await conn.execute('DELETE FROM mesh_layout WHERE owner=? AND server_id=?', (owner, sid))
                else:
                    await conn.execute('INSERT INTO mesh_layout VALUES(?,?,?,?) ON CONFLICT(owner,server_id) DO UPDATE SET x=excluded.x,y=excluded.y',
                                       (owner, sid, float(p['x']), float(p['y'])))
            async with conn.execute('SELECT COUNT(*) FROM mesh_layout WHERE owner=?', (owner,)) as cursor:
                (count,) = await cursor.fetchone()
            if count > LAYOUT_KEPT:
                raise InstanceError('invalid_mesh_layout')
        return {'ok': True, 'layout': await self._layout(owner)}

    # ----- draft -----
    async def save_draft(self, owner, data):
        async with self._edit(owner):
            prior = await self._load(owner, 'draft')
            doc = model.allocate(model.clean_doc(data, prior))
            facts, applied = await self._facts(owner), await self._load(owner, 'applied')

            def invalid(d):
                # A data plane that needs updating is not the draft's fault, and must not keep it from being edited.
                return [x for x in model.validate(d, facts, applied=applied) if x['error'] not in model.SCHEMA_ERRORS]
            # A removal undone: its entries, links and rules come back, where they still hold.
            kept = {m['serverId'] for m in doc['members'] if not m['removing']}
            undone = [m['serverId'] for m in prior['members'] if m['removing'] and m['serverId'] in kept]
            back = copy.deepcopy(doc)
            for sid in undone:
                if (before := await self._removed(owner, sid)) is not None:
                    model.unstrip(back, before, sid)
            errors = invalid(back) if undone else []
            if not errors and undone:
                doc = back
            elif errors := invalid(doc):
                raise model.MeshInvalid(errors)
            async with self.db.write_transaction(label='mesh-draft') as conn:
                await self._save(conn, owner, 'draft', doc)
                await self._drop_removed(conn, owner, undone)
        return await self.view(owner, ports='wait')

    async def add(self, owner, data):
        ids = data.get('serverIds') if isinstance(data, dict) and set(data) <= {'serverIds'} else None
        if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids) or not all(isinstance(i, str) for i in ids):
            raise InstanceError('invalid_mesh_members')
        facts = await self._facts(owner)
        if any(sid not in facts or not facts[sid]['listed'] for sid in ids):
            raise InstanceError('server_not_found', 404)

        started = []

        async def install(sid):
            refusal = self._refusal(facts[sid])
            if refusal:
                return {'serverId': sid, 'ok': False, 'error': refusal, 'reason': REFUSALS[refusal]}
            # Listed as installing until it is in the draft, so the page never loses it in between.
            self.installing[sid] = (owner, now_ms())
            started.append(sid)
            try:
                result = await self._call(owner, sid, 'mesh.install', {}, timeout=INSTALL_TIMEOUT)
                if not all(_valid_key(result.get(k)) for k in ('publicKey', 'controlKey')):
                    raise InstanceError('mesh_install_invalid', 502)
                await self._record_install(owner, sid, result, facts[sid])
            except InstanceError as exc:
                self.installing.pop(sid, None)
                return {'serverId': sid, 'ok': False, 'error': exc.error, 'reason': getattr(exc, 'reason', '') or REFUSALS.get(exc.error, '')}
            schemas = clean_schemas(result.get('configSchemas'))
            if schemas:
                await self._store_schemas(sid, schemas)
            else:
                # The config schemas its data plane reads, for the first config it gets.
                await self._refresh_node(owner, sid)
            return {'serverId': sid, 'ok': True, 'error': '', 'reason': ''}
        try:
            items = list(await asyncio.gather(*(install(sid) for sid in ids)))
            installed = [i['serverId'] for i in items if i['ok']]
            if installed:
                async with self._edit(owner):
                    draft, facts = await self._load(owner, 'draft'), await self._facts(owner)
                    present = {m['serverId']: m for m in draft['members']}
                    for sid in installed:
                        if sid in present:
                            present[sid]['removing'] = False
                            continue
                        member = model.new_member(sid, draft['settings'])
                        # Two machines behind one NAT on one UDP port can lose all traffic on
                        # some routers, so a newcomer takes the first port its NAT has free.
                        member['listen']['udp'] = model.free_udp_port(member, model.nat_udp_ports(draft, member, facts), draft['settings'])
                        member['entry']['candidates'] = self._first_entry(draft, facts)
                        draft['members'].append(member)
                    await self._save_draft(owner, model.allocate(draft))
        finally:
            for sid in started:
                self.installing.pop(sid, None)
        return {**await self.view(owner, ports='wait'), 'items': items}

    @staticmethod
    def _first_entry(draft, facts):
        for relay in model.active(draft):
            for transport in ('udp', 'tcp'):
                if relay['role'] == 'relay' and can_dial(relay, transport, facts):
                    return [{'id': 'c1', 'relay': relay['serverId'], 'transport': transport}]
        return []

    async def _record_install(self, owner, sid, result, f):
        """Refused (mesh_key_in_use) when any key it reports is one another member has on record
        (_held_keys): every member's config would then be refused whole by the nodes. Checked and
        stored in one transaction, so two installs at once cannot both take one key."""
        platform = result.get('platform') if result.get('platform') in model.PLATFORMS else f['platform']
        local = clean_local(result.get('localPrefixes')) or []
        addresses = clean_addresses(result.get('localAddresses')) or []
        keys = {'publicKey': result['publicKey'], 'controlKey': result['controlKey'],
                **{field: _security_keys(result)[column] for column, field in SECURITY_KEYS if column in _security_keys(result)}}
        taken = {}
        async with self.db.write_transaction(label='mesh-install') as conn:
            held = await self._held_keys(conn, owner, sid)
            taken = {field: held[key_bytes(k)] for field, k in keys.items() if key_bytes(k) in held}
            if not taken:
                await conn.execute('INSERT INTO mesh_nodes(server_id,owner,public_key,control_key,platform,data_version,local_json,addresses_json,updated_ms) '
                                   'VALUES(?,?,?,?,?,?,?,?,?) '
                                   'ON CONFLICT(server_id) DO UPDATE SET owner=excluded.owner,public_key=excluded.public_key,control_key=excluded.control_key,'
                                   'platform=excluded.platform,data_version=excluded.data_version,local_json=excluded.local_json,'
                                   'addresses_json=excluded.addresses_json,updated_ms=excluded.updated_ms,'
                                   # Installed again: no longer missing (SQLite reads the old state in both).
                                   "error=CASE WHEN mesh_nodes.state='missing' THEN '' ELSE mesh_nodes.error END,"
                                   "state=CASE WHEN mesh_nodes.state IN ('cleanup','missing') THEN '' ELSE mesh_nodes.state END",
                                   (sid, owner, result['publicKey'], result['controlKey'], platform, str(result.get('dataVersion') or '')[:40], dumps(local),
                                    dumps(addresses), now_ms()))
                await self._store_security_keys(conn, owner, sid, result)
        if taken:
            # Refused, nothing stored: said on the machine's events as a report of them would be.
            await self._facts(owner)
            by = {}
            for field, other in taken.items():
                by.setdefault(other, []).append(model.KEY_NAMES.get(field) or INSTALL_KEY_NAMES[field])
            said = '；'.join(f'安装时上报的{"、".join(names)}和{quoted(self._name(other))}的相同' for other, names in by.items())
            text = f'{said}，没有安装。{KEY_CLONE_ADVICE}'
            await self._event(sid, 'mesh.key_conflict', text)
            # A machine not yet a member keeps no events: the reminders list it, the page says it.
            await self.service.reminders._enqueue(owner, f'mesh-key-in-use:{sid}:{now_ms()}', sid, 'mesh', f'组网：{quoted(self._name(sid))}{text}')
            error = InstanceError('mesh_key_in_use', 409)
            error.reason = f'{quoted(self._name(sid))}{text}'
            raise error
        self.owners[sid] = owner

    @staticmethod
    async def _held_keys(conn, owner, sid):
        """{key: server id} of every key the owner's other members have on record, all four kinds
        together (a data plane refuses a config where a WireGuard and an end-to-end key repeat
        each other), as key_bytes gives them."""
        rows = await many(conn, f"SELECT server_id,{','.join(KEY_COLUMNS)} FROM mesh_nodes WHERE owner=? AND server_id!=?", (owner, sid))
        return {key_bytes(r[c]): r['server_id'] for r in rows for c in KEY_COLUMNS if r[c]}

    async def _store_security_keys(self, conn, owner, sid, result):
        """The signing and end-to-end public keys a node reports (mesh.install, mesh.upgrade,
        mesh.status); a node from before them reports none and keeps what is stored. One another
        member has on record is refused and the stored one kept: a member reporting a relay's
        end-to-end key (every config carries it) would otherwise have nodes refuse the two of them.
        Those refused go to key_conflicts until the node reports keys of its own (_key_conflict says
        so). True when a stored key changed: every config names them, so they are issued again."""
        keys = _security_keys(result)
        held = await self._held_keys(conn, owner, sid) if keys else {}
        taken = {field: held[key_bytes(keys[column])] for column, field in SECURITY_KEYS if column in keys and key_bytes(keys[column]) in held}
        if taken:
            self.key_conflicts[sid] = taken
        else:
            self.key_conflicts.pop(sid, None)
        keys = {column: key for column, key in keys.items() if dict(SECURITY_KEYS)[column] not in taken}
        if not keys:
            return False
        cursor = await conn.execute(f"UPDATE mesh_nodes SET {','.join(f'{k}=?' for k in keys)} WHERE server_id=? AND NOT "
                                    f"({' AND '.join(f'{k}=?' for k in keys)})", (*keys.values(), sid, *keys.values()))
        return bool(cursor.rowcount)

    async def _key_conflict(self, owner, sid):
        """An event on a member whose last report had a key another member has, naming that one;
        mesh_model.hints shows it among its warnings while it lasts."""
        taken = self.key_conflicts.get(sid)
        if not taken:
            return
        if any(x not in self.names for x in taken.values()):
            await self._facts(owner)
        by = {}
        for field, other in taken.items():
            by.setdefault(other, []).append(model.KEY_NAMES[field])
        said = '；'.join(f'上报的{"、".join(names)}和{quoted(self._name(other))}的相同，没有采用' for other, names in by.items())
        await self._event(sid, 'mesh.key_conflict', f'{said}。{KEY_CLONE_ADVICE}')

    async def remove(self, owner, server_id):
        async with self._edit(owner):
            draft, applied = await self._load(owner, 'draft'), await self._load(owner, 'applied')
            member = next((m for m in draft['members'] if m['serverId'] == server_id), None)
            if not member:
                raise InstanceError('mesh_member_not_found', 404)
            if member['removing']:
                return await self.view(owner)
            # Never applied: it goes from the draft at once and its node is uninstalled, as discard does.
            if server_id not in {m['serverId'] for m in applied['members']} and server_id not in self.installing:
                draft['members'] = [m for m in draft['members'] if m['serverId'] != server_id]
                rows = await self._rows(owner)
                async with self.db.write_transaction(label='mesh-remove-unapplied') as conn:
                    await self._save(conn, owner, 'draft', model.strip(draft, server_id))
                    if server_id in rows and rows[server_id]['state'] != 'cleanup':
                        await conn.execute("UPDATE mesh_nodes SET state='cleanup' WHERE server_id=?", (server_id,))
                if server_id in self.hub.sessions:
                    self.service.spawn(self._catch_up(owner, server_id))
                return await self.view(owner)
            # What strip takes out comes back if the removal is undone (save_draft).
            before = copy.deepcopy(draft)
            member['removing'] = True
            async with self.db.write_transaction(label='mesh-remove') as conn:
                await self._save(conn, owner, 'draft', model.strip(draft, server_id))
                await self._save(conn, owner, REMOVED + server_id, before)
        return await self.view(owner)

    async def _removed(self, owner, server_id):
        """The draft from before server_id was marked for removal, or None."""
        if not await one(self.db.conn, 'SELECT 1 FROM mesh_settings WHERE owner=? AND stage=?', (owner, REMOVED + server_id)):
            return None
        return await self._load(owner, REMOVED + server_id)

    @staticmethod
    async def _drop_removed(conn, owner, server_ids=None):
        for table in ('mesh_settings', 'mesh_members', 'mesh_links', 'mesh_rules'):
            if server_ids is None:
                await conn.execute(f"DELETE FROM {table} WHERE owner=? AND stage LIKE '{REMOVED}%'", (owner,))
            for sid in server_ids or ():
                await conn.execute(f'DELETE FROM {table} WHERE owner=? AND stage=?', (owner, REMOVED + sid))

    async def discard(self, owner):
        """Back to the applied document. Members installed for this draft and never applied are
        uninstalled like removed ones: now when online, otherwise when they reconnect."""
        async with self._edit(owner):
            draft, applied, rows = await self._load(owner, 'draft'), await self._load(owner, 'applied'), await self._rows(owner)
            kept = {m['serverId'] for m in applied['members']}
            unused = [m['serverId'] for m in draft['members'] if m['serverId'] not in kept and m['serverId'] not in self.installing
                      and m['serverId'] in rows and rows[m['serverId']]['state'] != 'cleanup']
            async with self.db.write_transaction(label='mesh-discard') as conn:
                await self._save(conn, owner, 'draft', applied)
                await self._drop_removed(conn, owner)
                for sid in unused:
                    await conn.execute("UPDATE mesh_nodes SET state='cleanup' WHERE server_id=?", (sid,))
        for sid in unused:
            if sid in self.hub.sessions:
                self.service.spawn(self._catch_up(owner, sid))
        return await self.view(owner)

    async def rollback(self, owner):
        """The previous applied revision becomes the draft and goes out. Members added since are removed again.
        Previous: the latest earlier document that differs from the applied one (a reissue for changed
        facts or a leaving server is skipped), preferring one that went out (applied, or partly when
        some nodes were offline) over one that failed."""
        self._reserve(owner)
        try:
            await self._rollback_draft(owner)
        except BaseException:
            self.applying.discard(owner)
            raise
        return await self.apply(owner, reserved=True)

    async def _rollback_draft(self, owner):
        net, current = await self._network(owner), await self._load(owner, 'applied')
        rows = await many(self.db.conn, 'SELECT doc_json,status FROM mesh_revisions WHERE owner=? AND revision<? ORDER BY revision DESC', (owner, net['applied_revision']))
        docs = [(model.upgrade(json.loads(r['doc_json'])), r['status']) for r in rows]
        docs = [(doc, status) for doc, status in docs if doc != current]
        doc = next((doc for doc, status in docs if status in ('applied', 'partial')), docs[0][0] if docs else None)
        if not doc:
            raise InstanceError('mesh_no_previous_revision', 409)
        async with self._edit(owner):
            installed, applied = await self._rows(owner), await self._load(owner, 'applied')
            for gone in [m['serverId'] for m in doc['members'] if m['serverId'] not in installed]:
                doc['members'] = [m for m in doc['members'] if m['serverId'] != gone]
                model.strip(doc, gone)
            kept = {m['serverId'] for m in doc['members']}
            doc['members'] += [{**copy.deepcopy(m), 'removing': True} for m in applied['members'] if m['serverId'] not in kept]
            await self._save_draft(owner, doc)

    # ----- revisions and rollouts -----
    def _reserve(self, owner):
        """One apply or rollback per owner from its start: a second one (a double click, the page and
        the AI at once) is refused at once instead of waiting out the first rollout and sending again."""
        rollout = self.rollouts.get(owner)
        if owner in self.applying or rollout and rollout.running:
            raise InstanceError('mesh_apply_running', 409)
        self.applying.add(owner)

    async def apply(self, owner, *, reserved=False):
        if not reserved:
            self._reserve(owner)
        lock = self._lock(owner)
        try:
            await lock.acquire()
        except BaseException:
            self.applying.discard(owner)
            raise
        try:
            async with self._edit(owner):
                draft, facts, applied = await self._load(owner, 'draft'), await self._facts(owner), await self._load(owner, 'applied')
                await self._refresh_tls(owner, draft, facts)
                errors = model.validate(draft, facts, applying=True, applied=applied)
                if not errors:
                    # A listen port another program holds would fail the delivery: checked again now, not from the cache.
                    errors = [w for w in await self._port_warnings(owner, draft, applied, facts, mode='fresh')
                              if w['error'] == 'mesh_port_in_use']
                if errors:
                    raise model.MeshInvalid(errors)
                plan = await self._issue(owner, draft, facts, reason='apply', save_draft=True)
        except BaseException:
            lock.release()
            self.applying.discard(owner)
            raise
        self.rollouts[owner] = plan['rollout']

        async def roll():
            try:
                await self._roll(owner, plan)
            finally:
                lock.release()
                self.applying.discard(owner)
        self.service.spawn(roll())
        return await self.view(owner)

    async def _issue(self, owner, doc, facts, *, reason, removed=(), save_draft=False):
        """A revision for doc (an applied document) with current facts. When neither the
        document nor any config changed, the current revision is sent again instead: a retry."""
        if await mesh_change.load(self, owner) and await mesh_change.recover(self, owner):
            raise InstanceError('mesh_change_recovering', 409)
        removed = [m['serverId'] for m in doc['members'] if m['removing']] + list(removed)
        doc = copy.deepcopy(doc)
        doc['members'] = [m for m in doc['members'] if not m['removing']]
        net, previous, rows = await self._network(owner), await self._load(owner, 'applied'), await self._rows(owner)
        latest = await self._latest(owner, net)
        network_id = net['network_id'] or str(uuid.uuid4())
        revision = net['applied_revision']
        configs = build(doc, facts, network_id, revision)
        fresh = not revision or doc != previous or {k: config_hash(v) for k, v in configs.items()} != {k: config_hash(v) for k, v in latest.items()}
        if fresh:
            revision = net['revision'] + 1
            configs = build(doc, facts, network_id, revision)
        else:
            configs = latest
        cleanup = list(dict.fromkeys([*removed, *sorted(sid for sid, r in rows.items() if r['state'] == 'cleanup')]))
        cleanup = [sid for sid in cleanup if sid in rows and sid not in configs]
        changed = {sid for sid, c in configs.items() if (rows.get(sid) or {}).get('confirmed_hash') != config_hash(c)}
        change = await mesh_change.make(self, owner, net, previous, rows, configs) if mesh_change.needed(latest, configs) else None
        await self._leaving_access(owner, previous, doc)
        if change:
            change.update(revision=revision, draft=await self._load(owner, 'draft'))
            async with self.db.write_transaction(label='mesh-change-prepare') as conn:
                await conn.execute('INSERT INTO mesh_revisions VALUES(?,?,?,?,?,?,?)', (owner, revision, now_ms(), reason, dumps(doc), dumps(configs), 'confirming'))
                await conn.execute('UPDATE mesh_network SET revision=? WHERE owner=?', (revision, owner))
                await conn.execute('INSERT INTO mesh_changes VALUES(?,?)', (owner, dumps(change)))
            return {'revision': revision, 'configs': configs, 'previous': previous, 'doc': doc, 'change': change,
                    'rollout': mesh_apply.Rollout(revision, mesh_apply.order(previous, doc, set(configs)), cleanup)}
        async with self.db.write_transaction(label='mesh-revision') as conn:
            if fresh:
                at = now_ms()
                await conn.execute('INSERT INTO mesh_revisions VALUES(?,?,?,?,?,?,?)', (owner, revision, at, reason, dumps(doc), dumps(configs), 'applying'))
                # Reissues for changed facts (a dynamic address) do not push out the documents rollback goes back to.
                await conn.execute('DELETE FROM mesh_revisions WHERE owner=? '
                                   'AND revision NOT IN (SELECT revision FROM mesh_revisions WHERE owner=? ORDER BY revision DESC LIMIT ?) '
                                   "AND revision NOT IN (SELECT revision FROM mesh_revisions WHERE owner=? AND reason!='facts' ORDER BY revision DESC LIMIT ?)",
                                   (owner, owner, REVISIONS_KEPT, owner, REVISIONS_KEPT))
                await conn.execute('INSERT INTO mesh_network VALUES(?,?,?,?,?) ON CONFLICT(owner) DO UPDATE SET network_id=excluded.network_id,'
                                   'revision=excluded.revision,applied_revision=excluded.applied_revision,applied_ms=excluded.applied_ms',
                                   (owner, network_id, revision, revision, at))
                await self._save(conn, owner, 'applied', doc)
            if save_draft:
                await self._save(conn, owner, 'draft', doc)
                await self._drop_removed(conn, owner)
            for sid in cleanup:
                await conn.execute("UPDATE mesh_nodes SET state='cleanup' WHERE server_id=?", (sid,))
            # A member back before its cleanup ran (a rollback past its removal) keeps its node.
            for sid in configs:
                if (rows.get(sid) or {}).get('state') == 'cleanup':
                    await conn.execute("UPDATE mesh_nodes SET state='' WHERE server_id=?", (sid,))
        if fresh:
            self._applied_changed(owner)
        return {'revision': revision, 'configs': configs, 'previous': previous, 'doc': doc,
                'rollout': mesh_apply.Rollout(revision, mesh_apply.order(previous, doc, changed), cleanup)}

    async def _roll(self, owner, plan):
        rollout = plan['rollout']
        self.rollouts[owner] = rollout
        if plan.get('change'):
            return await mesh_change.run(self, owner, plan)
        # Servers whose access over the mesh goes away move first, while it still works; those
        # moving onto it once the configs that carry them there are out.
        _, joining = await self._access_moves(owner, plan['previous'], plan['doc']) if 'doc' in plan else ([], [])
        outcome = await mesh_apply.run(rollout, plan['configs'], lambda sid, c: self._deliver(owner, sid, c), lambda sid: self._uninstall(owner, sid))
        with contextlib.suppress(InstanceError):
            await self._move_access(owner, joining)
        async with self.db.write_transaction(label='mesh-revision-state') as conn:
            await conn.execute('UPDATE mesh_revisions SET status=? WHERE owner=? AND revision=?', (outcome, owner, plan['revision']))
        for item in rollout.progress:
            if item['state'] == 'failed':
                name = self._name(item['serverId'])
                await self.service.reminders._enqueue(owner, f'mesh-apply:{owner}:{plan["revision"]}:{item["serverId"]}', item['serverId'], 'mesh',
                                                      f'组网：第 {plan["revision"]} 版配置下发到{quoted(name)}失败：{quoted(item["error"])}。')
        with contextlib.suppress(InstanceError):
            await self._activate_tls(owner)
        return outcome

    def _name(self, sid):
        return self.names.get(sid, sid)

    async def _deliver(self, owner, sid, config):
        async with self._node_lock(sid):
            return await self._send(owner, sid, config)

    async def _send(self, owner, sid, config):
        """_deliver with the node's lock already held."""
        row = await one(self.db.conn, 'SELECT config_schemas,state FROM mesh_nodes WHERE server_id=?', (sid,))
        if row and row['state'] == 'missing':
            # Not sent: it would only fail again. It gets the config once reinstalled.
            return 'missing', MISSING_ERROR
        reads = (clean_schemas(json.loads(row['config_schemas'])) if row and row['config_schemas'] else None) or LEGACY_SCHEMAS
        if not reads[0] <= config.get('schema', 1) <= reads[1]:
            # Not sent: the data plane would refuse the whole config. Nor a failure that stops the
            # rollout: one node left behind by an update (or fallen back) must not keep the others
            # from theirs. Its report once updated reissues the config (_store_schemas).
            side = '节点' if config.get('schema', 1) > reads[1] else '主控'
            error = (f'config_schema_unsupported: 这一版的配置格式是 {config.get("schema", 1)}，这台的组网数据面只读 '
                     f'{model.schema_span(reads)}，需要先更新{side}')
            async with self.db.write_transaction(label='mesh-failed') as conn:
                await conn.execute('UPDATE mesh_nodes SET error=? WHERE server_id=?', (error, sid))
            self.refused[sid] = config_hash(config)
            return 'outdated', error
        try:
            result = await self._call(owner, sid, 'mesh.apply', {'config': config}, timeout=APPLY_TIMEOUT)
        except InstanceError as exc:
            if exc.error == 'node_offline':
                return 'offline', exc.error
            if exc.error == 'node_connection_lost':
                # Applying a config can cut the connection that carried it: the node's own over the
                # mesh, or one through a relay or the controller's machine restarting. What it runs
                # once it is back decides, not the lost reply. One not back in time is offline as
                # before: the rollout goes on and it gets the config when it reconnects.
                seen = {}
                result = await self._after_lost(owner, sid, lambda r: self._applied_verdict(r, config['revision'], seen))
                if result is None and 'status' not in seen:
                    return 'offline', exc.error
                error = (seen['status'] or 'mesh_not_applied') if result is None else result
            else:
                error = exc.error
            if not error:
                await self._confirm(sid, config)
                return 'done', ''
        else:
            error = str(result.get('error') or '')[:500]
            if not error and result.get('appliedRevision') != config['revision']:
                error = 'mesh_not_applied'
            if not error:
                await self._confirm(sid, config)
                return 'done', ''
        if error == NOT_INSTALLED and await self._mark_missing(owner, sid):
            return 'missing', MISSING_ERROR
        async with self.db.write_transaction(label='mesh-failed') as conn:
            await conn.execute('UPDATE mesh_nodes SET error=? WHERE server_id=?', (error, sid))
        self.refused[sid] = config_hash(config)
        return 'failed', error

    async def _confirm(self, sid, config):
        async with self.db.write_transaction(label='mesh-confirmed') as conn:
            await conn.execute("UPDATE mesh_nodes SET confirmed_revision=?,confirmed_hash=?,error='' WHERE server_id=?",
                               (config['revision'], config_hash(config), sid))
        self.refused.pop(sid, None)

    @staticmethod
    def _applied_verdict(result, revision, seen):
        """From mesh.status after a lost apply: '' once the data plane runs the revision, else None
        to keep waiting. seen['status'] keeps the last error it showed ('' for none), for the
        failure when the wait runs out."""
        status = mesh_status.clean_status(result['status']) if isinstance(result.get('status'), dict) else None
        seen['status'] = status['error'] if status else ''
        return '' if status and status['appliedRevision'] == revision and not status['error'] else None

    async def _after_lost(self, owner, sid, verdict):
        """A call whose connection was lost: wait for the node to come back, from any address,
        and ask mesh.status until verdict(result) says how it went (not None). None when that
        does not happen within RECONNECT_WAIT seconds."""
        deadline = time.monotonic() + RECONNECT_WAIT
        while True:
            if sid in self.hub.sessions:
                try:
                    result = await self._call(owner, sid, 'mesh.status', {}, timeout=STATUS_CALL_TIMEOUT)
                except InstanceError:
                    result = None
                found = verdict(result) if result is not None else None
                if found is not None:
                    return found
            if time.monotonic() + RECONNECT_POLL > deadline:
                return None
            await asyncio.sleep(RECONNECT_POLL)

    async def _uninstall(self, owner, sid):
        async with self._node_lock(sid):
            return await self._remove(owner, sid)

    async def _remove(self, owner, sid, *, cleanup_only=False):
        """_uninstall with the node's lock already held. cleanup_only: forgotten only while still
        to be cleaned up; a rollback may have taken the member back meanwhile, and its next
        delivery then finds the service gone and says so."""
        try:
            await self._call(owner, sid, 'mesh.uninstall', {}, timeout=UNINSTALL_TIMEOUT)
        except InstanceError as exc:
            if exc.error in ('node_offline', 'node_connection_lost'):
                return 'offline', exc.error
            if exc.error != 'node_not_enrolled':
                async with self.db.write_transaction(label='mesh-failed') as conn:
                    await conn.execute('UPDATE mesh_nodes SET error=? WHERE server_id=?', (exc.error, sid))
                return 'failed', exc.error
        await self._forget(sid, cleanup_only=cleanup_only)
        return 'done', ''

    async def _forget(self, sid, *, cleanup_only=False):
        async with self.db.write_transaction(label='mesh-forget') as conn:
            cursor = await conn.execute("DELETE FROM mesh_nodes WHERE server_id=? AND (? OR state='cleanup')", (sid, not cleanup_only))
        if not cursor.rowcount and cleanup_only:
            return
        for cache in (self.owners, self.status, self.watches, self.refused, self.schemas, self.ipv4s, self.public_ips, self.key_conflicts,
                      self.alerted):
            cache.pop(sid, None)
        self.missing_held.discard(sid)
        await self.trend_store.forget(sid)
        with contextlib.suppress(Exception):
            await self.line_store.forget(sid)

    def node_hello(self, server_id):
        """A node (re)connected: send it what it missed, finish its cleanup, and reissue
        configs if what it reports (its address) changed what others need."""
        owner = self.owners.get(server_id)
        session = self.hub.sessions.get(server_id)
        # One that cannot work with this controller gets nothing until it or the controller is updated.
        if owner is None or session and session.incompatible:
            return
        # One catch-up per node at a time; hellos meanwhile have it run once more afterwards.
        if server_id in self.catching:
            self.catch_again.add(server_id)
            return
        self.catching.add(server_id)
        self.service.spawn(self._catching_up(owner, server_id))

    async def _catching_up(self, owner, sid):
        try:
            while True:
                self.catch_again.discard(sid)
                await self._catch_up(owner, sid)
                if sid not in self.catch_again:
                    return
        finally:
            self.catching.discard(sid)
            self.catch_again.discard(sid)

    async def _catch_up(self, owner, sid):
        """Never waits on the node holding the mesh lock: only the node's own lock is held while it
        is asked, and what is sent is read again under it, so a rollout to it meanwhile is not undone."""
        if await mesh_change.load(self, owner):
            if self._delivering(owner):
                return
            async with self._lock(owner):
                if await mesh_change.recover(self, owner):
                    return
        row = await one(self.db.conn, 'SELECT state FROM mesh_nodes WHERE server_id=?', (sid,))
        if not row:
            return
        state, error = None, ''
        if row['state'] != 'cleanup':
            await self._refresh_node(owner, sid)
        async with self._node_lock(sid):
            row = await one(self.db.conn, 'SELECT state,confirmed_hash FROM mesh_nodes WHERE server_id=?', (sid,))
            if not row:
                return
            if row['state'] == 'cleanup':
                state, error = await self._remove(owner, sid, cleanup_only=True)
            else:
                net = await self._network(owner)
                config = (await self._latest(owner, net)).get(sid)
                if config and row['confirmed_hash'] != config_hash(config) and not await self._stopped(owner, net, sid):
                    state, error = await self._send(owner, sid, config)
        # A rollout under way shows its own deliveries, and activates certificates itself once over.
        if state and owner in self.rollouts and not self.rollouts[owner].running:
            self.rollouts[owner].mark(sid, state, error)
        if state == 'done' and not self._delivering(owner):
            with contextlib.suppress(InstanceError):
                await self._activate_tls(owner)
        if state == 'done':
            self._access_after_config(sid)
        self._reconcile_soon(owner)
        await self._upgrade(owner, sid)

    def _access_after_config(self, sid):
        """A node that missed its move onto the mesh (it refuses an address it cannot reach yet)
        is moved again once its config is in place."""
        self.service.spawn(self._access_retry(sid))

    async def _access_retry(self, sid):
        row = await one(self.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (sid,))
        if row and json.loads(row['info_json']).get('accessPending'):
            self.service._apply_access_soon(sid)

    async def _refresh_node(self, owner, sid):
        """mesh.status after a reconnect: the interfaces for the prefix check and the build of the data plane's copy,
        and whether its mesh service is still there."""
        try:
            result = await self._call(owner, sid, 'mesh.status', {}, timeout=STATUS_CALL_TIMEOUT)
        except InstanceError:
            return
        if result.get('installed') is False or result.get('service') == 'absent':
            await self._mark_missing(owner, sid)
            return
        await self._found(sid, result)
        local, version, data_build = clean_local(result.get('localPrefixes')), result.get('dataVersion'), str(result.get('dataBuild') or '').lower()
        addresses = clean_addresses(result.get('localAddresses'))
        schemas = _reported_schemas(result)
        if schemas:
            await self._store_schemas(sid, schemas)
        fields = {'local_json': dumps(local) if local is not None else None,
                  'addresses_json': dumps(addresses) if addresses is not None else None,
                  'data_version': version[:40] if isinstance(version, str) and version else None,
                  'data_build': data_build if _BUILD.fullmatch(data_build) else None}
        fields = {k: v for k, v in fields.items() if v is not None}
        changed = False
        if fields or result.get('signKey') or result.get('e2eKey'):
            async with self.db.write_transaction(label='mesh-node') as conn:
                if fields:
                    await conn.execute(f"UPDATE mesh_nodes SET {','.join(f'{k}=?' for k in fields)} WHERE server_id=?", (*fields.values(), sid))
                changed = await self._store_security_keys(conn, owner, sid, result)
            await self._key_conflict(owner, sid)
        if changed:
            self._reconcile_soon(owner)

    async def _mark_missing(self, owner, sid):
        """The node says the mesh service of an applied member is gone (uninstalled, or the node
        installed again over it): mark it missing and say so once. Nothing is delivered to it until
        mesh_reinstall puts the service back. False when it is not an applied member."""
        if not any(m['serverId'] == sid for m in (await self._load(owner, 'applied'))['members']):
            return False
        async with self.db.write_transaction(label='mesh-missing') as conn:
            cursor = await conn.execute("UPDATE mesh_nodes SET state='missing',error=? WHERE server_id=? AND state=''", (MISSING_ERROR, sid))
            marked = bool(cursor.rowcount)
        if marked:
            self.refused.pop(sid, None)
            await self._event(sid, 'mesh.missing', '')
        missing = bool(marked or await one(self.db.conn, "SELECT 1 FROM mesh_nodes WHERE server_id=? AND state='missing'", (sid,)))
        # A node can say gone and back by turns: once per ALERT_COOLDOWN_MS, as mesh_status.Watch does.
        # One held back goes out when it is found missing again after that, unless it came back meanwhile.
        if missing and (marked or sid in self.missing_held):
            if self._due(sid, 'missing'):
                self.missing_held.discard(sid)
                await self.service.reminders._enqueue(owner, f'mesh-missing:{sid}:{now_ms()}', sid, 'mesh',
                                                      f'组网：{quoted(self._name(sid))}上的组网服务不见了（可能被卸载或重装过），点「重新安装组网服务」恢复。')
            else:
                self.missing_held.add(sid)
        return missing

    def _due(self, sid, kind):
        """mesh_status.Watch._due for an alert the controller raises itself about a node."""
        now, sent = now_ms(), self.alerted.setdefault(sid, {})
        if kind in sent and now - sent[kind] < mesh_status.ALERT_COOLDOWN_MS:
            return False
        sent[kind] = now
        return True

    async def _found(self, sid, result):
        """A missing member whose service is back with the keys on record (put back by hand) is no
        longer missing; with other keys the others cannot reach it, so it stays until reinstalled."""
        row = await one(self.db.conn, 'SELECT state,public_key,control_key FROM mesh_nodes WHERE server_id=?', (sid,))
        if (row and row['state'] == 'missing' and result.get('installed') is True
                and (result.get('publicKey'), result.get('controlKey')) == (row['public_key'], row['control_key'])):
            async with self.db.write_transaction(label='mesh-missing') as conn:
                await conn.execute("UPDATE mesh_nodes SET state='',error='' WHERE server_id=? AND state='missing'", (sid,))
            self.missing_held.discard(sid)

    async def reinstall(self, owner, sid):
        """Put the mesh service back on an applied member (mesh.install: new keys when the old ones
        are gone), then issue a revision of the applied document with them so every member gets its
        new public key. The draft is left as it is, unapplied changes included."""
        self._reserve(owner)
        lock = self._lock(owner)
        try:
            await lock.acquire()
        except BaseException:
            self.applying.discard(owner)
            raise
        try:
            applied, facts = await self._load(owner, 'applied'), await self._facts(owner)
            if (not any(m['serverId'] == sid for m in applied['members'])
                    or not await one(self.db.conn, 'SELECT 1 FROM mesh_nodes WHERE server_id=? AND owner=?', (sid, owner))):
                raise InstanceError('mesh_member_not_found', 404)
            refusal = self._refusal(facts[sid]) if sid in facts else 'node_not_enrolled'
            if refusal:
                raise InstanceError(refusal, 409)
            self.installing[sid] = (owner, now_ms())
            try:
                result = await self._call(owner, sid, 'mesh.install', {}, timeout=INSTALL_TIMEOUT)
                if not all(_valid_key(result.get(k)) for k in ('publicKey', 'controlKey')):
                    raise InstanceError('mesh_install_invalid', 502)
                await self._record_install(owner, sid, result, facts[sid])
                # Its data plane has no config now: it gets one whatever it was confirmed to run.
                async with self.db.write_transaction(label='mesh-reinstall') as conn:
                    await conn.execute("UPDATE mesh_nodes SET confirmed_revision=0,confirmed_hash='',error='',state='' WHERE server_id=?", (sid,))
                self.refused.pop(sid, None)
                schemas = clean_schemas(result.get('configSchemas'))
                if schemas:
                    await self._store_schemas(sid, schemas)
                await self._refresh_node(owner, sid)
            finally:
                self.installing.pop(sid, None)
            await self._event(sid, 'mesh.reinstall', '')
            async with self._edit(owner):
                # New keys are changed facts: the same document, reissued like one for a new address.
                plan = await self._issue(owner, await self._load(owner, 'applied'), await self._facts(owner), reason='facts')
        except BaseException:
            lock.release()
            self.applying.discard(owner)
            raise
        self.rollouts[owner] = plan['rollout']

        async def roll():
            try:
                await self._roll(owner, plan)
            finally:
                lock.release()
                self.applying.discard(owner)
        self.service.spawn(roll())
        return await self.view(owner)

    async def _store_schemas(self, sid, schemas):
        """Record the config schemas a member's data plane reads; True when that changes which ones
        count for it (none recorded counts as the first schema only)."""
        if self.schemas.get(sid) == schemas:
            return False
        row = await one(self.db.conn, 'SELECT config_schemas FROM mesh_nodes WHERE server_id=?', (sid,))
        if not row:
            return False
        self.schemas[sid] = schemas
        stored = clean_schemas(json.loads(row['config_schemas'])) if row['config_schemas'] else None
        if stored == schemas:
            return False
        async with self.db.write_transaction(label='mesh-schemas') as conn:
            await conn.execute('UPDATE mesh_nodes SET config_schemas=? WHERE server_id=?', (dumps(list(schemas)), sid))
        # A config held back or refused for the schemas it read is due again: the same config, when
        # nothing else changed, is resent on this report rather than waiting for the next revision.
        self.refused.pop(sid, None)
        return (stored or LEGACY_SCHEMAS) != schemas

    async def _upgrade(self, owner, sid, again=False):
        """Bring the data plane's copy to the node's own build after a node update. Relays go one at
        a time, each reporting ready before the next starts: a batch node update reconnects them all
        at once, and restarting every relay together would cut the whole mesh. Members go after
        the relays, as one whose entry relay restarts under it does not get ready. A build that did
        not come up twice is not tried again; the node has already gone back to the old copy."""
        session = self.hub.sessions.get(sid)
        target = session.build if session else ''
        row = await one(self.db.conn, 'SELECT state,data_build,upgrade_failed_build FROM mesh_nodes WHERE server_id=?', (sid,))
        if (not target or not row or row['state'] in ('cleanup', 'missing') or sid in self.installing or sid in self.upgrades
                or target in (row['data_build'], row['upgrade_failed_build'])):
            return
        relays = {m['serverId'] for m in (await self._load(owner, 'applied'))['members'] if m['role'] == 'relay'}
        relay = sid in relays
        self.upgrades[sid] = 'waiting'
        try:
            async with self.relay_upgrades.setdefault(owner, asyncio.Lock()) if relay else contextlib.nullcontext():
                if not relay:
                    await asyncio.sleep(MEMBER_UPGRADE_SETTLE)
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(MEMBER_UPGRADE_WAIT):
                            while relays & self.upgrades.keys():
                                await asyncio.sleep(1)
                # Not during a rollout, and not holding the lock through an upgrade that can take a minute.
                async with self._lock(owner):
                    pass
                if sid not in self.hub.sessions:
                    return
                self.upgrades[sid] = 'upgrading'
                try:
                    result = await self._call(owner, sid, 'mesh.upgrade', {}, timeout=UPGRADE_TIMEOUT)
                except InstanceError as exc:
                    # Restarting the data plane can cut the connection that asked for it; the
                    # build it runs once back decides.
                    lost = exc.error == 'node_connection_lost' and await self._after_lost(
                        owner, sid, lambda r: '' if str(r.get('dataBuild') or '').lower() == target else None) == ''
                    if not lost:
                        error = exc.error
                        if error == NOT_INSTALLED and await self._mark_missing(owner, sid):
                            return
                        if error == 'node_connection_lost':
                            # Not back in time: offline, it is tried again when it reconnects.
                            error = 'node_offline' if sid not in self.hub.sessions else 'mesh_upgrade_not_running'
                        await self._upgrade_failed(owner, sid, target, error, again)
                        return
                    result = {'changed': True}
                ready = self.ready_waits[sid] = asyncio.Event()
                version = result.get('to') if isinstance(result.get('to'), str) and result['to'] else None
                async with self.db.write_transaction(label='mesh-upgrade') as conn:
                    await conn.execute("UPDATE mesh_nodes SET data_build=?,upgrade_failed_build='',upgrade_error='',upgrade_ms=?,"
                                       'data_version=COALESCE(?,data_version) WHERE server_id=?', (target, now_ms(), version and version[:40], sid))
                    # This build signs and encrypts end to end with keys made by the upgrade.
                    keys_changed = await self._store_security_keys(conn, owner, sid, result)
                await self._key_conflict(owner, sid)
                if keys_changed:
                    self._reconcile_soon(owner)
                if relay and result.get('changed'):
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(ready.wait(), UPGRADE_READY_WAIT)
        finally:
            self.upgrades.pop(sid, None)
            self.ready_waits.pop(sid, None)

    async def _upgrade_failed(self, owner, sid, target, error, again):
        if error in ('node_offline', 'node_update_required'):
            return
        not_ready = error == 'upgrade_not_ready'
        given_up = not_ready and again
        async with self.db.write_transaction(label='mesh-upgrade') as conn:
            await conn.execute('UPDATE mesh_nodes SET upgrade_error=?,upgrade_ms=?,upgrade_failed_build=CASE WHEN ? THEN ? ELSE upgrade_failed_build END WHERE server_id=?',
                               (error, now_ms(), given_up, target, sid))
        if not_ready and not again:
            self.service.spawn(self._upgrade_again(owner, sid, target))
        if not given_up:
            return
        if sid not in self.names:
            await self._facts(owner)
        await self._event(sid, 'upgrade.failed', f'data plane upgrade to build {target[:12]} did not get ready; rolled back')
        await self.service.reminders._enqueue(owner, f'mesh-upgrade:{sid}:{target}', sid, 'mesh',
                                              f'组网：{quoted(self._name(sid))}的组网服务升级失败，已回退到原版本。')

    async def _upgrade_again(self, owner, sid, target):
        await asyncio.sleep(UPGRADE_RETRY_DELAY)
        session = self.hub.sessions.get(sid)
        if session and session.build == target:
            await self._upgrade(owner, sid, again=True)

    async def _event(self, sid, kind, detail):
        """An event the controller adds to a node's list. It takes the seq of the node's last
        event, so the node's next events, compared by seq, still come through."""
        live = self.status.setdefault(sid, {'status': None, 'events': [], 'at': 0, 'saved': 0})
        seq = live['events'][-1]['seq'] if live['events'] else -1
        live['events'] = mesh_status.merge_events(live['events'], [{'seq': seq, 'at': now_ms(), 'kind': kind, 'detail': detail}])
        async with self.db.write_transaction(label='mesh-event') as conn:
            await conn.execute('UPDATE mesh_nodes SET events_json=? WHERE server_id=?', (dumps(list(live['events'])), sid))

    def _reconcile_soon(self, owner):
        """reconcile, once for any number of changes that come while one waits for the lock: it
        reads the facts only once it has the lock."""
        if owner in self.reconcile_waiting:
            return
        mark = self.reconcile_waiting[owner] = object()

        async def run():
            try:
                await self.reconcile(owner)
            finally:
                if self.reconcile_waiting.get(owner) is mark:
                    del self.reconcile_waiting[owner]
        self.service.spawn(run())

    async def reconcile(self, owner):
        """A new revision of the applied document when facts changed the configs, e.g. an automatic public address."""
        async with self._lock(owner):
            # Changes from here on are not in what is read below: they wait for the next one.
            self.reconcile_waiting.pop(owner, None)
            if await mesh_change.load(self, owner) and await mesh_change.recover(self, owner):
                return None
            net = await self._network(owner)
            if not net['applied_revision']:
                return None
            doc, facts = await self._load(owner, 'applied'), await self._facts(owner)
            latest = await self._latest(owner, net)
            configs = build(doc, facts, net['network_id'], net['applied_revision'])
            if {k: config_hash(v) for k, v in configs.items()} == {k: config_hash(v) for k, v in latest.items()}:
                # Nothing new to issue. A node whose report started this (its data plane now reads the
                # schema it was skipped for) found the lock held and did not catch up: it does now.
                # Only those: nodes a stopped rollout did not reach wait for the next one, as before.
                rows = await self._rows(owner)
                for sid, config in latest.items():
                    row = rows.get(sid)
                    if not row or row['state'] in ('cleanup', 'missing') or not row['error'].startswith('config_schema_unsupported'):
                        continue
                    reads = (clean_schemas(json.loads(row['config_schemas'])) if row['config_schemas'] else None) or LEGACY_SCHEMAS
                    if (reads[0] <= config.get('schema', 1) <= reads[1] and row['confirmed_hash'] != config_hash(config)
                            and sid in self.hub.sessions and sid not in self.refused and sid not in self.resending):
                        self.resending.add(sid)
                        self.service.spawn(self._resend(owner, sid))
                return None
            return await self._roll(owner, await self._issue(owner, doc, facts, reason='facts'))

    async def leave(self, owner, server_id):
        """After a server is deleted or its node revoked: revoke it on every other member. Its node is
        cut off by then, so it is not waited on; its mesh service stays on the machine. Otherwise (a
        server leaving while still enrolled) it is uninstalled."""
        if not await one(self.db.conn, 'SELECT 1 FROM mesh_nodes WHERE server_id=? AND owner=?', (server_id, owner)):
            return
        async with self._lock(owner):
            async with self._edit(owner):
                draft = await self._load(owner, 'draft')
                draft['members'] = [m for m in draft['members'] if m['serverId'] != server_id]
                await self._save_draft(owner, model.strip(draft, server_id))
                applied = await self._load(owner, 'applied')
                plan = None
                if any(m['serverId'] == server_id for m in applied['members']):
                    applied['members'] = [m for m in applied['members'] if m['serverId'] != server_id]
                    plan = await self._issue(owner, model.strip(applied, server_id), await self._facts(owner), reason='leave', removed=[server_id])
            if plan:
                await self._roll(owner, plan)
            elif server_id in self.hub.sessions:
                await self._uninstall(owner, server_id)
            await self._forget(server_id)

    # ----- TLS pins -----
    async def _refresh_tls(self, owner, doc, facts, *, stale_only=False):
        """Ask relays that listen for TLS for their certificates; True when a pin changed."""
        rows, changed = await self._rows(owner), False
        for m in model.active(doc):
            sid = m['serverId']
            if m['role'] != 'relay' or not m['listen']['tls'] or sid not in self.hub.sessions or sid not in rows or rows[sid]['state'] == 'missing':
                continue
            if stale_only and now_ms() - rows[sid]['tls_checked_ms'] < TLS_REFRESH_MS:
                continue
            try:
                tls = clean_tls(await self._call(owner, sid, 'mesh.tls', {}, timeout=TLS_CALL_TIMEOUT))
            except InstanceError:
                continue
            if tls is None:
                continue
            changed = changed or tls != facts[sid].get('tls')
            facts[sid]['tls'] = tls
            await self._store_tls(sid, tls)
        return changed

    async def _store_tls(self, sid, tls):
        async with self.db.write_transaction(label='mesh-tls') as conn:
            await conn.execute('UPDATE mesh_nodes SET tls_json=?,tls_checked_ms=? WHERE server_id=?', (dumps(tls), now_ms(), sid))

    async def _activate_tls(self, owner):
        """Switch a relay to its pending certificate once every online node that connects
        to it over TLS runs a config holding the new pin; offline ones get it on catch-up."""
        rows, latest = await self._rows(owner), await self._latest(owner, await self._network(owner))
        for sid, row in rows.items():
            tls = json.loads(row['tls_json']) if row['tls_json'] else None
            pending = (tls or {}).get('pending')
            if not pending or sid not in self.hub.sessions:
                continue
            holders = {n: list(dialed_pins(c, sid)) for n, c in latest.items()}
            holders = {n: pins for n, pins in holders.items() if pins}
            if not all(pending['fingerprint'] in p for pins in holders.values() for p in pins):
                continue
            if any(n in self.hub.sessions and (rows.get(n) or {}).get('confirmed_hash') != config_hash(latest[n]) for n in holders):
                continue
            result = clean_tls(await self._call(owner, sid, 'mesh.tls', {'activate': pending['id']}, timeout=TLS_CALL_TIMEOUT))
            if result:
                await self._store_tls(sid, result)

    async def check_tls(self, owner):
        """Daily: relays may have made a pending certificate; it goes out as a pin, then gets activated."""
        async with self._lock(owner):
            net = await self._network(owner)
            if not net['applied_revision']:
                return
            doc, facts = await self._load(owner, 'applied'), await self._facts(owner)
            changed = await self._refresh_tls(owner, doc, facts, stale_only=True)
            with contextlib.suppress(InstanceError):
                await self._activate_tls(owner)
        if changed:
            await self.reconcile(owner)

    async def _tls_loop(self):
        while True:
            await asyncio.sleep(TLS_CHECK_INTERVAL_S)
            for owner in set(self.owners.values()):
                with contextlib.suppress(Exception):
                    await self.check_tls(owner)

    # ----- reports -----
    async def report(self, server_id, raw):
        """The mesh field of a node's report: status.json without events, plus the events since the last report."""
        owner = self.owners.get(server_id)
        if owner is None or not isinstance(raw, dict):
            return
        now = now_ms()
        status, events = mesh_status.clean_status(raw), mesh_status.clean_events(raw.get('events'))
        live = self.status.get(server_id)
        if live is None:
            live = self.status[server_id] = {'status': None, 'events': [], 'at': 0, 'saved': 0}
        before, restored = live['status'], live.pop('restored', False)
        restarted = bool(before) and before.get('startedAt') != status['startedAt']
        last = live['events'][-1]['seq'] if live['events'] and not restarted else -1
        new = [e for e in events if e['seq'] > last]
        live['status'], live['at'] = status, now
        live['events'] = mesh_status.merge_events(live['events'], new)
        if status['ready'] and server_id in self.ready_waits:
            self.ready_waits[server_id].set()

        def key(s):
            return s and (s['ready'], s['appliedRevision'], s['error'], (s['entry'] or {}).get('relay'), (s['entry'] or {}).get('transport'),
                          mesh_status.public_ip(s))
        try:
            if new or key(before) != key(status):
                live['dirty'] = True
            # A change is saved soon, not on every report: a node whose state flaps does not write each time.
            if now - live['saved'] >= (STATUS_SAVE_MIN_MS if live.get('dirty') else STATUS_SAVE_MS):
                live['saved'], live['dirty'] = now, False
                async with self.db.write_transaction(label='mesh-status') as conn:
                    await conn.execute('UPDATE mesh_nodes SET status_json=?,events_json=?,updated_ms=? WHERE server_id=?',
                                       (dumps(status), dumps(list(live['events'])), now, server_id))
            # The schemas the running data plane reads; one from before configSchemas reads the first only.
            if await self._store_schemas(server_id, clean_schemas(raw.get('configSchemas')) or LEGACY_SCHEMAS):
                # The schema every member reads may have changed: the configs follow it.
                self._reconcile_soon(owner)
            applied = await self._applied(owner)
            member = applied['members'].get(server_id)
            with contextlib.suppress(Exception):
                # The trend must not stop alerts; a lost minute is only a gap.
                await self.trend_store.observe(server_id, status, member['role'] if member else 'member', now)
            with contextlib.suppress(Exception):
                await self.line_store.observe(applied['lines'], server_id, self.status, now)
            if not member:
                return
            if mesh_status.public_ip(before) != mesh_status.public_ip(status):
                # Punched links aim at a verified public IP: others' configs follow it once it holds,
                # counted from this report.
                self.public_ips.setdefault(server_id, (mesh_status.public_ip(before), None, 0))
                self._settled(self.public_ips, owner, server_id, mesh_status.public_ip(status))
                self._settle_addr(owner, server_id)
            await self._sync_applied(owner, server_id, status, applied)
            if server_id not in self.names:
                await self._facts(owner)
            watch = self.watches.get(server_id)
            if watch is None:
                watch = self.watches[server_id] = mesh_status.Watch()
                if restored and before:
                    watch.resume(before, member['role'], now)
            for alert_key, kind, text in watch.observe(now, server_id, self._name(server_id), member['role'],
                                                       bool(member['entry']['candidates']), status, new, self._name):
                await self.service.reminders._enqueue(owner, alert_key, server_id, kind, text)
        except Exception:
            # A report must never break the node's session; the next one tries again.
            live['saved'] = 0

    def _settle_addr(self, owner, sid):
        """Reissue for a member's new public address once it has held PUBLIC_ADDR_SETTLE_S; each
        change starts the wait again."""
        waiting = sid in self.addr_settling
        self.addr_settling[sid] = time.monotonic()
        if waiting:
            return

        async def settle():
            try:
                while True:
                    wait = self.addr_settling[sid] + PUBLIC_ADDR_SETTLE_S - time.monotonic()
                    if wait <= 0:
                        break
                    await asyncio.sleep(wait)
            finally:
                self.addr_settling.pop(sid, None)
            self._reconcile_soon(owner)
        self.service.spawn(settle())

    def _delivering(self, owner):
        return self._lock(owner).locked() or owner in self.applying

    async def _sync_applied(self, owner, sid, status, applied):
        """What the node runs, from its report: a revision its data plane applied by itself after a
        delivery failed (its own retries), or an older one it fell back to on a restart. The record
        follows it, so the next delivery is not skipped as already there, and a node behind the latest
        config gets it once. Not while configs go out: the delivery's own reply decides then.
        applied: the applied revision as _applied gives it."""
        reported = status['appliedRevision']
        if not reported or self._delivering(owner) or self._sending(sid) or await mesh_change.load(self, owner):
            return
        row = await one(self.db.conn, 'SELECT confirmed_revision,confirmed_hash,error,state FROM mesh_nodes WHERE server_id=?', (sid,))
        if not row or row['state'] == 'cleanup':
            return
        latest, latest_hash = applied['configs'].get(sid), applied['hashes'].get(sid, '')
        confirmed = row['confirmed_hash']
        if reported != row['confirmed_revision']:
            ran = await one(self.db.conn, 'SELECT configs_json FROM mesh_revisions WHERE owner=? AND revision=?', (owner, reported))
            config = json.loads(ran['configs_json']).get(sid) if ran else None
            # A revision no longer kept: its config is unknown, so it never counts as the latest.
            confirmed = config_hash(config) if config else 'unknown'
        clear = confirmed == latest_hash and not status['error'] and row['error']
        if confirmed != row['confirmed_hash'] or clear:
            async with self.db.write_transaction(label='mesh-confirmed') as conn:
                await conn.execute('UPDATE mesh_nodes SET confirmed_revision=?,confirmed_hash=?,error=? WHERE server_id=?',
                                   (reported, confirmed, '' if clear else row['error'], sid))
        if confirmed == latest_hash:
            self.refused.pop(sid, None)
        elif latest and sid in self.hub.sessions and self.refused.get(sid) != latest_hash and sid not in self.resending:
            self.resending.add(sid)
            self.service.spawn(self._resend(owner, sid))

    async def _resend(self, owner, sid):
        """The latest config to a node whose report shows it runs another; once per config it refuses."""
        try:
            if self._delivering(owner):
                return
            # Like a catch-up: only the node's own lock is held while it is asked.
            async with self._node_lock(sid):
                row = await one(self.db.conn, 'SELECT confirmed_hash,state FROM mesh_nodes WHERE server_id=?', (sid,))
                net = await self._network(owner)
                config = (await self._latest(owner, net)).get(sid)
                if (self._delivering(owner) or not row or row['state'] == 'cleanup' or not config or sid not in self.hub.sessions
                        or row['confirmed_hash'] == config_hash(config) or self.refused.get(sid) == config_hash(config)
                        or await self._stopped(owner, net, sid)):
                    return
                state, error = await self._send(owner, sid, config)
            if owner in self.rollouts and not self.rollouts[owner].running:
                self.rollouts[owner].mark(sid, state, error)
            if state == 'done':
                if not self._delivering(owner):
                    with contextlib.suppress(InstanceError):
                        await self._activate_tls(owner)
                self._access_after_config(sid)
        finally:
            self.resending.discard(sid)
