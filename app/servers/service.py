"""Servers page: one registry for SSH targets and monitored nodes, the node
hub and its metrics. SSH connection material stays in the instance registry."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import datetime as dt
import ipaddress
import json
import os
import re
import secrets
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from aiohttp import WSMsgType, web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import versioning
from app.ddns.service import Ddns
from app.instances.contracts import InstanceError
from app.instances.servers import LOCAL, _ssh
from app.servers import compat, geo, install, releases, remote
from app.servers import files as file_ops
from app.servers import profile as profiles
from app.servers import reminders as reminder_rules
from app.servers.bench import Benchmarks
from app.servers.browser import NodeBrowsers
from app.servers.hub import ADDRESS_FIELDS, REPORT_INTERVAL, NodeHub, clean_ip
from app.servers.mesh import PUBLIC_IPS, Mesh
from app.servers.metrics import RANGES, MetricsStore
from app.servers.net import NetTools
from app.servers.onboarding import (
    MESH_ACCESS,
    MESH_ACCESS_FEATURE,
    Onboarding,
    clean_access,
    resolve,
    uses_mesh,
)
from app.servers.reminders import Reminders
from app.servers.schema import migrate
from app.servers.transfers import Transfers
from app.webhooks.repository import many, one

DEFAULT_TIMEZONE = 'Asia/Shanghai'
TERMINAL_IDLE_S = 30 * 60
TERMINAL_POLL_S = 60  # how often an idle terminal is looked at
EXEC_LOG_KEEP = 200
BATCH_PARALLEL = 8
FILE_CHUNK = 1024 * 1024
EDIT_LIMIT = 4 * 1024 * 1024
MAX_EDITS = 100
JOB_WAIT_MAX = 300
JOB_COMMAND_MAX = 8000
JOB_STDIN_MAX = 1024 * 1024
# Output kept in memory while job_wait looks for a pattern; a match must fit in it.
JOB_MATCH_WINDOW = 256 * 1024
# job_wait asks the node at most this often, even while every answer brings more output.
JOB_WAIT_ROUND_S = 0.5
# How long one look for the pattern may take. It runs in its own process: re holds the GIL, so a
# pattern that backtracks without end would stop every thread of the controller, not only its own.
JOB_MATCH_TIMEOUT_S = 2
_MATCHER = ('import json,re,sys\n'
            'd=json.load(sys.stdin)\n'
            'm=re.compile(d["p"],re.M).search(d["s"])\n'
            'print(json.dumps(m and [m.start(),m.end()]))')
# A node's location is looked up at most this often, however often its address changes.
GEO_EVERY_S = 600
# How long an update waits for the node to come back running the build it was sent.
NODE_CONFIRM_S = 120
# How long a listing waits for the check 接收测试版 changing starts, before answering with the
# pick it has (the check goes on and the next listing has it).
SWITCH_WAIT_S = 15
# Automatic node updates: how often every node is looked at, how long after a node comes
# online it is, and how long a build that failed to go onto a node is left alone.
AUTO_UPDATE_EVERY = 600
AUTO_UPDATE_DELAY = 30
AUTO_UPDATE_RETRY = 6 * 3600
# Update states a node can be brought out of by installing the target build.
UPDATABLE = frozenset({'required', 'available', 'mismatch'})


def short_build(build):
    """The first 12 hex digits of a build's sha256, as shown to people and the model."""
    return str(build or '')[:12]


def node_facts(session=None, stored=None):
    """What deciding about a node's version needs: from its live session, else as last recorded.
    features is None when it is not known which the node has (recorded before they were kept)."""
    if session is not None:
        top = getattr(session, 'protocol', 1)
        return {'version': session.version, 'build': session.build, 'info': session.info, 'features': session.features,
                'protocol': (getattr(session, 'protocol_min', top), top)}
    stored = stored or {}
    features = stored.get('features')
    top = stored.get('protocol', 1)
    return {'version': stored.get('version', ''), 'build': stored.get('build', ''), 'info': stored.get('info') or {},
            'features': frozenset(features) if isinstance(features, list) else None, 'protocol': (stored.get('protocolMin', top), top)}


def mesh_reason(sid, access):
    """Why a server cannot choose ap-mesh ('' when it can), by mesh.access(): local, not_member
    (not in the applied mesh) or node_outdated (a member whose node lacks MESH_ACCESS_FEATURE)."""
    _, members, eligible = access
    return 'local' if sid == LOCAL else '' if sid in eligible else 'node_outdated' if sid in members else 'not_member'


def update_state(built, facts, withdrawn=frozenset()):
    """Where a node stands against the build this controller would put on it.

    First whether it runs exactly that build (by sha256); only when it does not, the version,
    features and protocol say which way it is off and how urgent that is:
      current   runs the target build (or, not saying its build, the target version)
      required  lacks a feature this controller needs, speaks no protocol in common with it,
                or runs a withdrawn version
      available the target is newer (by app.versioning: a development build moves on to a
                later development build or release), or the node runs an unknown version
      mismatch  same version, another build
      ahead     newer than the target
      ''        no target build for its platform, and nothing it must have
    downgrade: the target is older than the node. That is never updatable (so never automatic)
    except off a withdrawn version; only asked for explicitly (update_node downgrade=True).
    """
    info = facts.get('info') or {}
    binary = ((built or {}).get('binaries') or {}).get(install.binary_name(install.platform_of(info), info.get('arch', '')))
    version, build = str(facts.get('version') or ''), str(facts.get('build') or '')
    target, source = (built or {}).get('version', ''), (built or {}).get('source', '')
    missing = releases.missing_features(facts.get('features'))
    protocol_ok = compat.common(compat.PROTOCOL, facts.get('protocol') or (1, 1)) is not None
    pulled = version in withdrawn
    downgrade = bool(binary) and versioning.valid(target) and versioning.newer(version, target)
    if binary and build and binary['sha256'] == build:
        state = 'current'
    elif not protocol_ok or missing or pulled:
        state = 'required'
    elif not binary:
        state = ''
    elif versioning.newer(target, version):
        state = 'available'
    elif downgrade:
        state = 'ahead'
    elif not build:
        state = 'current'
    else:
        state = 'mismatch'
    return {'state': state, 'updatable': state in UPDATABLE and bool(binary) and (not downgrade or pulled), 'downgrade': state != 'current' and downgrade,
            'missing': missing, 'protocolOk': protocol_ok, 'withdrawn': pulled, 'target': {'version': target, 'source': source, 'build': short_build(binary['sha256'])} if binary else None}


def node_source(built, facts):
    """Where the build a node runs comes from: local (this controller's node/bin), release,
    prerelease (beta/rc), dev, or unknown. Judged by the target's sha256 when it matches, else
    by the version the node reports."""
    info = facts.get('info') or {}
    binary = ((built or {}).get('binaries') or {}).get(install.binary_name(install.platform_of(info), info.get('arch', '')))
    if binary and facts.get('build') and binary['sha256'] == facts['build'] and built.get('source') == 'local':
        return 'local'
    version = versioning.parse(facts.get('version'))
    if version is None:
        return 'unknown'
    return {'dev': 'dev', 'beta': 'prerelease', 'rc': 'prerelease'}.get(version.stage, 'release')


def now_ms():
    return int(time.time() * 1000)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


# Command output cut to fit keeps its start and, larger, its end, with this mark where the
# middle was: the same one new nodes put in output over their own cap.
CLIP_MARK = '\n…（省略 {} 字节）…\n'


def clip_output(text, limit):
    """(text, cut): text within about limit characters, its first fifth and the rest from the
    end around a mark naming how many bytes (UTF-8) were left out."""
    text = text or ''
    if len(text) <= limit:
        return text, False
    head = limit // 5
    tail = max(1, limit - head - len(CLIP_MARK) - 8)
    omitted = len(text[head:len(text) - tail].encode('utf-8', errors='replace'))
    return text[:head] + CLIP_MARK.format(omitted) + text[len(text) - tail:], True


# What the node's Go net.IP IsLoopback/IsPrivate accept; Python's is_private
# also covers link-local, documentation and reserved ranges the node refuses.
# As the node allows: loopback, private networks and 100.64.0.0/10 (the mesh's addresses).
PLAIN_HTTP_NETWORKS = tuple(ipaddress.ip_network(n) for n in ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10',
                                                               '::1/128', 'fc00::/7'))


def node_url(raw):
    """Same policy as the node: https, or http only to loopback/private literals."""
    value = str(raw or '').strip().rstrip('/')
    if not value:
        return ''
    # The URL goes into install commands and the node's own parser; neither
    # takes whitespace, control or quoting characters.
    if any(ord(c) <= 32 or ord(c) == 127 or c in '"\'<>\\^`{|}' for c in value):
        raise InstanceError('invalid_node_url')
    parts = urlsplit(value)
    try:
        parts.port
    except ValueError:
        raise InstanceError('invalid_node_url') from None
    if parts.scheme not in {'http', 'https'} or not parts.hostname or '@' in parts.netloc or parts.query or parts.fragment:
        raise InstanceError('invalid_node_url')
    if parts.scheme == 'http' and parts.hostname != 'localhost':
        try:
            ip = ipaddress.ip_address(parts.hostname)
        except ValueError:
            raise InstanceError('node_url_requires_https') from None
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not any(ip in network for network in PLAIN_HTTP_NETWORKS if network.version == ip.version):
            raise InstanceError('node_url_requires_https')
    return value


class ServerService:
    def __init__(self, db, host, instances):
        self.db, self.host, self.instances = db, host, instances
        self.hub = NodeHub(self)
        self.metrics = MetricsStore(Path(db.path).expanduser().parent / 'server-metrics.db')
        self.installs = {}
        self.tasks = set()
        self.geo_pending = set()
        self.geo_at = {}  # server id -> monotonic time of its last location lookup
        self.access_applying = set()
        self.http = None
        self.identity = None
        self.jobs = {}
        self.file_jobs = {}
        self.transfers = Transfers(self)
        self.reminders = Reminders(self)
        self.onboarding = Onboarding(self)
        self.browsers = NodeBrowsers(self)
        self.mesh = Mesh(self)
        self.ddns = Ddns(self)
        self.net = NetTools(self)
        self.bench = Benchmarks(self)
        # Node updates under way, relays updating one at a time, and builds that would not
        # go onto a node automatically: (server id, sha256) -> when it last failed.
        self.node_updates = set()
        self.relay_updates = asyncio.Lock()
        self.auto_failed = {}
        self.auto_pending = set()

    @property
    def registry(self):
        return self.instances.servers

    async def start(self):
        self.instances.require_controller()
        await self.registry.migrate()
        await migrate(self.db)
        await self._load_identity()
        await self.metrics.start()
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)
        self.reminders.start()
        await self.browsers.refresh()
        await self.mesh.start()
        await self.ddns.start()
        await self.bench.start()
        releases.configure(Path(self.db.path).expanduser().parent / 'node-bin', prerelease=self.prerelease)
        if not os.environ.get('PYTEST_CURRENT_TEST') and os.environ.get('OPENBEAR_DISABLE_UPDATE_CHECK') != '1':
            self.spawn(releases.loop(self.servers_enabled))
            self.spawn(self._auto_update_loop())

    # ----- switches (system settings) -----
    def _config(self, section):
        return getattr(getattr(self.host, 'config', None), section, None)

    def servers_enabled(self):
        """The 主机 feature is on. Node version checks, update reminders and automatic updates
        only run then; node connections, the mesh and billing reminders run either way."""
        features = self._config('features')
        return features.enabled('servers') if features is not None else True

    def prerelease(self):
        return getattr(self._config('updates'), 'prerelease', False) is True

    def auto_update_enabled(self):
        return self.servers_enabled() and getattr(self._config('updates'), 'auto_update_nodes', False) is True

    # ----- node versions -----
    @staticmethod
    def update_state(built, facts, withdrawn=frozenset()):
        return update_state(built, facts, withdrawn)

    def outdated(self, built, session):
        """The node can be brought to the target build (see update_state)."""
        return bool(session) and update_state(built, node_facts(session), releases.withdrawn())['updatable']

    async def ensure_builds(self):
        """A node build to install: with no local or cached one, ask GitHub now."""
        await self.follow_prerelease()
        if not install.manifest().get('binaries'):
            await releases.check()
        return install.manifest()

    async def follow_prerelease(self):
        """After 接收测试版 changed, pick the node version for the new setting now, so pages and
        updates never see the old pick dropped with nothing in its place (主机 on only)."""
        if self.servers_enabled():
            await releases.follow_switch(SWITCH_WAIT_S)

    async def node_release(self, *, check=False, follow=True):
        if check:
            await releases.check()
        elif follow:
            await self.follow_prerelease()
        built, last = install.manifest(), releases.state()
        return {'ok': True, 'release': {'version': built.get('version', ''), 'source': built.get('source', ''), 'available': bool(built.get('binaries')),
                                        'latest': last.get('latest', ''), 'incompatible': last.get('incompatible', ''), 'lacking': last.get('lacking', ''),
                                        'checkedAtMs': last.get('checkedAtMs', 0), 'error': last.get('error', ''),
                                        'prerelease': self.prerelease(), 'autoUpdate': self.auto_update_enabled(), 'localProblem': install.local_problem(),
                                        'required': [{'name': f, 'label': releases.FEATURE_LABELS.get(f, f)} for f in releases.REQUIRED_FEATURES]}}

    async def _load_identity(self):
        """The key nodes pin at enrollment; stored sealed like other credentials."""
        row = await one(self.db.conn, 'SELECT sealed FROM server_identity WHERE id=1')
        if not row:
            sealed = self.instances.vault.seal({'seed': base64.b64encode(secrets.token_bytes(32)).decode()})
            async with self.db.write_transaction(label='server-identity') as conn:
                await conn.execute('INSERT OR IGNORE INTO server_identity VALUES(1,?)', (sealed,))
            row = await one(self.db.conn, 'SELECT sealed FROM server_identity WHERE id=1')
        seed = base64.b64decode(self.instances.vault.open(row['sealed'])['seed'])
        self.identity = Ed25519PrivateKey.from_private_bytes(seed)
        self.identity_public = base64.b64encode(self.identity.public_key().public_bytes_raw()).decode()

    async def close(self):
        await self.transfers.close()
        await self.hub.close()
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.metrics.close()
        if self.http:
            await self.http.close()
            self.http = None

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # ----- settings -----
    async def settings(self):
        row = await one(self.db.conn, 'SELECT settings_json FROM server_settings WHERE id=1')
        value = json.loads(row['settings_json']) if row else {}
        points = value.get('accessPoints')
        if points is None:
            # Before access points there was one address for every node.
            points = [{'id': 'default', 'name': '默认', 'url': value['nodeUrl']}] if value.get('nodeUrl') else []
        default = value.get('defaultAccess') or (points[0]['id'] if points else '')
        return {'accessPoints': points, 'defaultAccess': default, 'timezone': value.get('timezone') or DEFAULT_TIMEZONE,
                'reminders': reminder_rules.clean({}, value.get('reminders'))}

    async def settings_view(self, owner):
        """settings with meshAccess, the built-in access point over the mesh (worked out, not stored)."""
        return {**await self.settings(), 'meshAccess': (await self.mesh.access(owner))[0]}

    async def mesh_point(self, owner, server_id):
        """The mesh access point as resolve() takes it for one server, or None when it cannot use it now."""
        return self.mesh.access_point(await self.mesh.access(owner), server_id)

    async def _check_mesh_access(self, owner, server_id, patch):
        """ap-mesh may be chosen only for a server in the applied mesh, the controller's own machine
        aside, whose node takes a mesh address (an older one has to be updated first)."""
        if 'access' in patch and uses_mesh(patch):
            reason = mesh_reason(server_id, await self.mesh.access(owner)) if server_id else 'not_member'
            if reason == 'node_outdated':
                raise InstanceError('mesh_access_node_outdated', 409)
            if reason:
                raise InstanceError('mesh_access_requires_member', 409)

    async def save_settings(self, data, owner=None):
        if not isinstance(data, dict) or set(data) - {'accessPoints', 'defaultAccess', 'timezone', 'reminders'}:
            raise InstanceError('invalid_server_settings')
        value = await self.settings()
        before = copy.deepcopy(value)
        if 'accessPoints' in data or 'defaultAccess' in data:
            value['accessPoints'], value['defaultAccess'] = clean_access(data.get('accessPoints', value['accessPoints']),
                                                                         data.get('defaultAccess', value['defaultAccess']), node_url)
        if 'reminders' in data:
            value['reminders'] = reminder_rules.clean(data['reminders'], value['reminders'])
        if 'timezone' in data:
            try:
                ZoneInfo(str(data['timezone']))
            except (ZoneInfoNotFoundError, ValueError):
                raise InstanceError('invalid_timezone') from None
            value['timezone'] = str(data['timezone'])
        async with self.db.write_transaction(label='server-settings') as conn:
            await conn.execute('INSERT OR REPLACE INTO server_settings VALUES(1,?)', (dumps(value),))
        if owner is not None and (before['accessPoints'], before['defaultAccess']) != (value['accessPoints'], value['defaultAccess']):
            await self._access_moved(owner, before, value)
        return {'ok': True, 'settings': {**value, 'meshAccess': (await self.mesh.access(owner))[0]} if owner is not None else value}

    async def _access_moved(self, owner, before, after):
        """Nodes whose addresses new access points change are pending, as after a change on the
        server itself: moved by the page's push, or on their next connection."""
        access, prof = await self.mesh.access(owner), await self._profiles(owner)

        def urls(settings, sid):
            return tuple(p['url'] if p else '' for p in resolve(settings, prof.get(sid, {}), self.mesh.access_point(access, sid)))
        for sid in await self._nodes(owner):
            old, new = urls(before, sid), urls(after, sid)
            if sid == LOCAL or old == new:
                continue
            # No access point before: it uses the address it enrolled with, known only while online.
            live = self.hub.sessions.get(sid)
            if not old[0] and (live is None or live.endpoint in new):
                continue
            await self.mark_access_pending(sid, True)

    def local_url(self):
        return f'http://127.0.0.1:{self.host.config.web.port}'

    async def zone(self):
        return ZoneInfo((await self.settings())['timezone'])

    # ----- listing -----
    async def _profiles(self, owner):
        return {r['server_id']: json.loads(r['profile_json']) for r in await many(self.db.conn, 'SELECT * FROM server_profiles WHERE owner=?', (owner,))}

    async def _nodes(self, owner):
        return {r['server_id']: r for r in await many(self.db.conn, 'SELECT * FROM server_nodes WHERE owner=?', (owner,))}

    async def _traffic(self, server_id, prof, zone):
        t = prof.get('traffic') or {}
        billing = prof.get('billing') or {}
        mode = profiles.traffic_mode(t)
        anchor = billing.get('expiresAt') or billing.get('startedAt')
        day = billing.get('billingDay') if billing.get('expiresAt') else None
        reset = t.get('resetDay') if mode == 'fixed' and t.get('resetDay') else (day or (dt.date.fromisoformat(anchor).day if anchor else 1))
        today = dt.datetime.now(zone).date()
        start = profiles.period_start(reset, today)
        start_ts = int(dt.datetime.combine(start, dt.time(), zone).timestamp())
        rx, tx = await self.metrics.traffic_since(server_id, start_ts)
        kind = t.get('type') or 'max'
        used = profiles.traffic_used(kind, rx, tx)
        price = (t.get('price') or 0) if mode == 'metered' else 0
        return {'periodStart': start.isoformat(), 'resetDay': reset, 'rx': rx, 'tx': tx, 'type': kind, 'mode': mode, 'used': used,
                'limit': (t.get('limit') or 0) if mode in ('billing', 'fixed') else 0,
                **({'price': price, 'cost': round(used / 1024 ** 3 * price, 2)} if mode == 'metered' else {})}

    async def _item(self, server, prof, node, zone, settings, access, built=None, withdrawn=None):
        sid = server['id']
        local = sid == LOCAL
        ssh = None
        if not local and server.get('auth') != 'none':
            ssh = {k: server.get(k) for k in ('host', 'port', 'username', 'auth', 'keyLabel', 'status', 'checkedAtMs', 'error', 'os', 'docker', 'publicIp',
                                              'platform')}
        stored = json.loads(node['info_json']) if node else {}
        live = self.hub.live(sid)
        view = None
        if node:
            version = (live or {}).get('version') or stored.get('version', '')
            built = install.manifest() if built is None else built
            latest = built.get('version', '')
            session = self.hub.sessions.get(sid)
            facts = node_facts(session if live else None, stored)
            update = update_state(built, facts, releases.withdrawn() if withdrawn is None else withdrawn)
            outdated = bool(live and update['updatable'])
            view = {'enrolledAtMs': node['enrolled_ms'], 'lastSeenAtMs': node['seen_ms'], 'online': bool(live),
                    'version': version, 'latest': latest, 'updateAvailable': outdated,
                    'newBuild': outdated and latest == version,
                    'build': short_build(facts['build']), 'source': node_source(built, facts), 'update': update,
                    'caps': (live or {}).get('caps') or stored.get('caps', {}),
                    'info': stored.get('info', {}), 'geo': stored.get('geo'),
                    'endpoint': session.endpoint if session else '', 'onBackup': bool(session and session.on_backup),
                    'joinPending': await self._join_view(node['owner'], stored.get('joinPending'))}
        primary, backup = (None, None) if local else resolve(settings, prof, self.mesh.access_point(access, sid))
        # Only a hint: a lost mesh path then leaves the node no other way to reach the controller.
        warnings = ['mesh_no_backup'] if primary and primary['id'] == MESH_ACCESS and not backup else []
        return {'id': sid, 'name': (prof.get('alias') or '本机') if local else server['name'], 'local': local, 'ssh': ssh,
                'usedBy': server.get('usedBy', []), 'profile': prof, 'node': view,
                'region': prof.get('region') or (stored.get('geo') or {}).get('country', ''),
                'traffic': await self._traffic(sid, prof, zone) if node else None,
                'access': {'primary': primary, 'backup': backup, 'pending': bool(stored.get('accessPending')),
                           'meshEligible': not mesh_reason(sid, access), 'meshReason': mesh_reason(sid, access), 'warnings': warnings},
                'live': live, 'install': self.installs.get(sid)}

    async def _join_view(self, owner, pending):
        """A server that joined looking like another one, until the owner settles it (onboarding.settle_join)."""
        if not pending:
            return None
        try:
            name = (await self.registry.get(owner, pending['serverId']))['name']
        except InstanceError:
            name = ''
        return {'serverId': pending['serverId'], 'name': name, 'code': pending.get('code', ''), 'atMs': pending.get('atMs', 0)}

    async def list(self, owner):
        servers = await self.registry.all(owner)
        await self.follow_prerelease()
        prof, nodes, zone, settings = await self._profiles(owner), await self._nodes(owner), await self.zone(), await self.settings()
        built, withdrawn = install.manifest(), releases.withdrawn()
        access = await self.mesh.access(owner)
        items = [await self._item(s, prof.get(s['id'], {}), nodes.get(s['id']), zone, settings, access, built, withdrawn) for s in servers]
        # Servers without a node always come after those with one.
        items.sort(key=lambda i: (i['node'] is None, not i['local'] and 'sort' not in i['profile'], i['profile'].get('sort', 0), i['name'].lower()))
        return {'ok': True, 'items': items, 'settings': {**settings, 'meshAccess': access[0]}, 'node': (await self.node_release(follow=False))['release']}

    async def get(self, owner, server_id):
        server = await self.registry.get(owner, server_id)
        prof = (await self._profiles(owner)).get(server_id, {})
        node = await one(self.db.conn, 'SELECT * FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))
        await self.follow_prerelease()
        item = await self._item(server, prof, node, await self.zone(), await self.settings(), await self.mesh.access(owner))
        rows = await many(self.db.conn, 'SELECT started_ms,ended_ms,ip,reason FROM server_terminal_log WHERE server_id=? AND owner=? ORDER BY started_ms DESC LIMIT 10', (server_id, owner))
        item['terminalLog'] = [{'startedAtMs': r['started_ms'], 'endedAtMs': r['ended_ms'], 'ip': r['ip'], 'reason': r['reason']} for r in rows]
        rows = await many(self.db.conn, 'SELECT source,command,started_ms,duration_ms,exit_code,outcome FROM server_exec_log WHERE server_id=? AND owner=? ORDER BY id DESC LIMIT 20', (server_id, owner))
        item['execLog'] = [{'source': r['source'], 'command': r['command'], 'startedAtMs': r['started_ms'], 'durationMs': r['duration_ms'],
                            'exitCode': r['exit_code'], 'outcome': r['outcome']} for r in rows]
        return item

    async def live(self, owner):
        owned = {r['server_id'] for r in await many(self.db.conn, 'SELECT server_id FROM server_nodes WHERE owner=?', (owner,))}
        return {'ok': True, 'at': now_ms(), 'items': {sid: self.hub.live(sid) for sid in list(self.hub.sessions) if sid in owned}}

    async def series(self, owner, server_id, span):
        if span != 'live' and span not in RANGES:
            raise InstanceError('invalid_range')
        await self.registry.get(owner, server_id)
        if span == 'live':
            return {'ok': True, **self._recent(server_id)}
        return {'ok': True, **await self.metrics.series(server_id, span)}

    def _recent(self, server_id):
        """The last ten minutes of live samples, kept in memory by the hub."""
        session = self.hub.sessions.get(server_id)
        samples = list(session.recent) if session else []
        pct = lambda u: round(u['used'] * 100 / u['total'], 2) if u['total'] else 0  # noqa: E731
        points = {'t': [t for t, _ in samples]}
        for key, pick in (('cpu', lambda m: m['cpu']), ('steal', lambda m: m['steal']), ('iowait', lambda m: m['iowait']),
                          ('mem', lambda m: pct(m['mem'])), ('swap', lambda m: m['swap']['used']), ('disk', lambda m: pct(m['disk'])),
                          ('load', lambda m: m['load'][0]), ('rxRate', lambda m: m['net']['rxRate']), ('txRate', lambda m: m['net']['txRate']),
                          ('ioRead', lambda m: m['io']['readRate']), ('ioWrite', lambda m: m['io']['writeRate']),
                          ('tcp', lambda m: m['tcp']), ('udp', lambda m: m['udp']), ('proc', lambda m: m['processes'])):
            points[key] = [pick(m) for _, m in samples]
        end = int(time.time())
        return {'range': 'live', 'step': REPORT_INTERVAL, 'start': end - 600, 'end': end, 'points': points}

    # ----- registry changes -----
    async def _save_profile(self, owner, server_id, data):
        row = await one(self.db.conn, 'SELECT profile_json FROM server_profiles WHERE server_id=? AND owner=?', (server_id, owner))
        value = profiles.clean(data, json.loads(row['profile_json']) if row else {})
        await self._write_profile(owner, server_id, value)
        return value

    async def _write_profile(self, owner, server_id, value):
        async with self.db.write_transaction(label='server-profile') as conn:
            await conn.execute('INSERT OR REPLACE INTO server_profiles VALUES(?,?,?,?)', (server_id, owner, dumps(value), now_ms()))

    async def create(self, owner, data):
        if not isinstance(data, dict) or set(data) - {'name', 'profile', 'ssh'}:
            raise InstanceError('invalid_server')
        ssh = data.get('ssh')
        if ssh is not None and not isinstance(ssh, dict):
            raise InstanceError('invalid_server')
        if data.get('profile'):
            # A server being added is not in the mesh yet.
            await self._check_mesh_access(owner, None, profiles.clean(data['profile']))
        result = await self.registry.save(owner, {**(ssh or {'auth': 'none'}), 'name': data.get('name')})
        server_id = result['item']['id']
        if data.get('profile'):
            await self._save_profile(owner, server_id, data['profile'])
        return {'ok': True, 'item': await self.get(owner, server_id)}

    async def update(self, owner, server_id, data):
        """accessApply now (default) moves an online node first and saves nothing when it
        refuses; later saves the addresses for the node's next connection."""
        if not isinstance(data, dict) or set(data) - {'name', 'profile', 'accessApply'}:
            raise InstanceError('invalid_server')
        apply = data.get('accessApply', 'now')
        if apply not in ('now', 'later'):
            raise InstanceError('invalid_access_apply')
        await self.registry.get(owner, server_id)
        patch = dict(data.get('profile') or {})
        if 'name' in data:
            if server_id == LOCAL:
                patch['alias'] = data['name']
            elif not isinstance(data['name'], str) or not 1 <= len(data['name'].strip()) <= 60:
                raise InstanceError('server_name_required')
        current = (await self._profiles(owner)).get(server_id, {})
        merged = profiles.clean(patch, current) if patch else current
        if 'access' in patch:
            await self._check_mesh_access(owner, server_id, {'access': merged['access']})
        result, pending = {'ok': True}, None
        enrolled = server_id != LOCAL and await one(self.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))
        settings = await self.settings() if enrolled else None
        mesh = await self.mesh_point(owner, server_id) if enrolled else None
        primary, backup = resolve(settings, merged, mesh) if enrolled else (None, None)
        if enrolled and (primary, backup) != resolve(settings, current, mesh):
            pending = True
            if apply == 'now' and server_id in self.hub.sessions:
                if not primary:
                    raise InstanceError('node_url_required', 409)
                push = await self.onboarding.push_one(owner, server_id, (primary['url'], backup['url'] if backup else ''))
                # A lost connection may have moved the node; it is settled on reconnect.
                if not push['ok'] and push['error'] != 'node_connection_lost':
                    raise InstanceError(push['error'], 409)
                result['accessPush'], pending = push, not push['ok']
        if 'name' in data and server_id != LOCAL:
            await self.registry.save(owner, {'name': data['name']}, server_id)
        if patch:
            await self._write_profile(owner, server_id, merged)
        if pending is not None:
            await self.mark_access_pending(server_id, pending)
            result['accessPending'] = pending
            if pending and apply == 'now' and server_id in self.hub.sessions:
                self._apply_access_soon(server_id)
        return {**result, 'item': await self.get(owner, server_id)}

    async def mark_access_pending(self, server_id, pending):
        row = await one(self.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))
        if row and bool(json.loads(row['info_json']).get('accessPending')) != pending:
            await self._merge_node_info(server_id, {'accessPending': pending})

    def _apply_access_soon(self, server_id):
        if server_id in self.access_applying:
            return
        self.access_applying.add(server_id)

        async def apply():
            try:
                row = await one(self.db.conn, 'SELECT owner FROM server_nodes WHERE server_id=?', (server_id,))
                if row:
                    await self.onboarding.push(row['owner'], [server_id])
            finally:
                self.access_applying.discard(server_id)
        self.spawn(apply())

    async def order(self, owner, data):
        ids = data.get('ids') if isinstance(data, dict) else None
        owned = {s['id'] for s in await self.registry.all(owner)}
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or not all(isinstance(i, str) and i in owned for i in ids):
            raise InstanceError('invalid_server_order')
        current = await self._profiles(owner)
        for index, server_id in enumerate(ids):
            if current.get(server_id, {}).get('sort') != index:
                await self._write_profile(owner, server_id, profiles.clean({'sort': index}, current.get(server_id, {})))
        return await self.list(owner)

    async def delete(self, owner, server_id):
        """Cut off and forgotten first; the mesh drops it afterwards, without waiting on its node."""
        if server_id == LOCAL:
            raise InstanceError('local_server_fixed', 409)
        await self.registry.delete(owner, server_id)
        async with self.db.write_transaction(label='server-forget') as conn:
            for table in ('server_profiles', 'server_nodes', 'server_enrollments', 'server_terminal_log', 'server_exec_log', 'mesh_layout'):
                await conn.execute(f'DELETE FROM {table} WHERE server_id=? AND owner=?', (server_id, owner))
        await self.hub.disconnect(server_id)
        self.spawn(self.mesh.leave(owner, server_id))
        await self.bench.forget_server(owner, server_id)
        await self.metrics.forget(server_id)
        self.installs.pop(server_id, None)
        return {'ok': True}

    # ----- enrollment (see onboarding.py) -----
    async def enrollment(self, owner, server_id, *, browser=False):
        return await self.onboarding.enrollment(owner, server_id, browser=browser)

    async def enroll(self, data):
        return await self.onboarding.enroll(data)

    async def settle_join(self, owner, server_id, data):
        return await self.onboarding.settle_join(owner, server_id, data)

    async def install(self, owner, server_id, *, browser=False, force=False):
        return await self.onboarding.install(owner, server_id, browser=browser, force=force)

    async def revoke(self, owner, server_id, *, uninstall=False):
        """The node is cut off and forgotten first; the mesh drops it afterwards, without waiting on it."""
        await self.registry.get(owner, server_id)
        uninstalled = False
        if uninstall and server_id in self.hub.sessions:
            try:
                await self._node_call(owner, server_id, 'uninstall', {}, timeout=20)
                uninstalled = True
            except InstanceError:
                pass  # Revoking still cuts the node off; its files stay behind.
        async with self.db.write_transaction(label='server-node-revoke') as conn:
            await conn.execute('DELETE FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))
            await conn.execute('DELETE FROM server_enrollments WHERE server_id=? AND owner=?', (server_id, owner))
        await self.hub.disconnect(server_id)
        self.spawn(self.mesh.leave(owner, server_id))
        return {'ok': True, 'uninstalled': uninstalled, 'item': await self.get(owner, server_id)}

    # ----- billing -----
    async def set_expiry(self, owner, server_id, date, day):
        """A renewal's new expiry, keeping the billing day (profile.billing_day)."""
        await self._save_profile(owner, server_id, {'billing': {'expiresAt': date, 'billingDay': day}})

    async def renew(self, owner, server_id):
        """Advance the expiry by one billing cycle, from the current expiry date."""
        await self.registry.get(owner, server_id)
        billing = (await self._profiles(owner)).get(server_id, {}).get('billing') or {}
        if not billing.get('cycle') or billing['cycle'] <= 0:
            raise InstanceError('billing_cycle_required', 409)
        base = dt.date.fromisoformat(billing['expiresAt']) if billing.get('expiresAt') else dt.datetime.now(await self.zone()).date()
        day = billing.get('billingDay') or base.day
        await self.set_expiry(owner, server_id, profiles.add_cycle(base, billing['cycle'], day).isoformat(), day)
        return {'ok': True, 'item': await self.get(owner, server_id)}

    # ----- node maintenance -----
    async def update_node(self, owner, server_id, *, source='web', downgrade=False):
        """Install this controller's node build on one node; it restarts itself. Reported as
        updated only once the node is back running that build (confirmed); a node that does
        not come back on it in time is reported as "file replaced, not confirmed running".
        A target older than the node goes on only with downgrade=True."""
        await self.registry.get(owner, server_id)
        session = self.hub.sessions.get(server_id)
        if not session:
            raise InstanceError('node_offline', 409)
        if not session.caps.get('exec'):
            raise InstanceError('node_update_needs_exec', 409)
        if server_id in self.node_updates:
            raise InstanceError('node_update_running', 409)
        self.node_updates.add(server_id)
        try:
            return await self._update_node(owner, server_id, session, source, downgrade)
        finally:
            self.node_updates.discard(server_id)

    async def _update_node(self, owner, server_id, session, source, downgrade=False):
        built = await self.ensure_builds()
        name = install.binary_name(install.platform_of(session.info), session.info.get('arch', ''))
        binary = (built.get('binaries') or {}).get(name)
        if not binary:
            raise InstanceError('node_binary_missing', 503)
        status = update_state(built, node_facts(session), releases.withdrawn())
        if not self._wanted(status, downgrade):
            return {'ok': True, 'changed': False, 'state': status['state'], 'from': session.version, 'to': built['version'], 'build': short_build(session.build),
                    **({'downgrade': True} if status['downgrade'] else {})}
        # The node downloads it from here; a release binary is fetched from GitHub first.
        if not await install.fetch_binary(name):
            raise InstanceError('node_download_failed', 503)
        started = now_ms()
        result = await self._node_call(owner, server_id, 'update', {'version': built['version'], 'name': name, 'sha256': binary['sha256'],
                                                                    'size': binary['bytes']}, timeout=330)
        changed = bool(result.get('changed'))
        confirmed = await self._confirm_build(server_id, session, binary['sha256']) if changed else True
        what = f"{result.get('from', '')} → {result.get('to', '')}" if result.get('from') != result.get('to') else f"{result.get('to', '')} 的新构建"
        await self._log(owner, server_id, source, f"{'降级' if status['downgrade'] else '更新'}节点 {what}" + ('' if confirmed else '（文件已替换，未确认运行）'), started,
                        {'exitCode': 0 if confirmed else None, 'outcome': 'ok' if confirmed else 'unknown', 'durationMs': now_ms() - started})
        return {'ok': True, 'changed': changed, 'confirmed': confirmed, 'state': 'updated' if confirmed else 'unconfirmed',
                'from': result.get('from', ''), 'to': result.get('to', ''), 'build': short_build(binary['sha256']),
                **({'downgrade': True} if status['downgrade'] else {}),
                **({} if confirmed else {'note': '文件已替换，未确认运行：节点没有在限定时间内用新程序重新连上'})}

    async def _confirm_build(self, server_id, before, sha256):
        """The node reconnected (a new session, after the one the update went to) and says it runs sha256.
        Its own word: authenticated as this node's, but nothing here can check what a node runs,
        so pages and tools give it as what the node reports."""
        deadline = time.monotonic() + NODE_CONFIRM_S
        while time.monotonic() < deadline:
            session = self.hub.sessions.get(server_id)
            if session is not None and session is not before and session.build == sha256:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _relays(self, owner):
        """Servers that relay for the applied mesh: they update last, one at a time."""
        try:
            doc = await self.mesh._load(owner, 'applied')
        except Exception:  # noqa: BLE001 - without a mesh every node is an ordinary one
            return set()
        return {m.get('serverId') for m in doc.get('members') or [] if m.get('role') == 'relay'}

    async def update_nodes(self, owner, server_ids=None, *, source='web', auto=False, downgrade=False):
        """Update every online node that can be (or those of server_ids): ordinary nodes first,
        a few at a time, then mesh relays one after another, each back on its new build before
        the next goes, so the mesh never loses more than one relay at once. downgrade=True also
        takes the nodes the target is older than (only ever asked for explicitly)."""
        owned = {r['server_id'] for r in await many(self.db.conn, 'SELECT server_id FROM server_nodes WHERE owner=?', (owner,))}
        built = await self.ensure_builds()
        gone = releases.withdrawn()
        # A node that refuses commands cannot update itself; it is reinstalled where it runs.
        targets = [sid for sid, s in list(self.hub.sessions.items())
                   if sid in owned and (server_ids is None or sid in server_ids) and s.caps.get('exec') and sid not in self.node_updates
                   and self._wanted(update_state(built, node_facts(s), gone), downgrade and not auto) and (not auto or self._auto_due(sid, s, built))]
        relays = await self._relays(owner)
        gate = asyncio.Semaphore(4)

        async def one_node(sid):
            session = self.hub.sessions.get(sid)
            try:
                result = {'serverId': sid, **await self.update_node(owner, sid, source=source, downgrade=downgrade and not auto)}
            except InstanceError as exc:
                result = {'serverId': sid, 'ok': False, 'error': exc.error}
            if auto:
                self._auto_record(sid, session, built, result)
            return result

        async def ordinary(sid):
            async with gate:
                return await one_node(sid)
        items = list(await asyncio.gather(*(ordinary(sid) for sid in targets if sid not in relays)))
        for sid in (sid for sid in targets if sid in relays):
            async with self.relay_updates:
                items.append({**await one_node(sid), 'relay': True})
        return {'ok': True, 'items': items}

    @staticmethod
    def _wanted(status, downgrade):
        return status['updatable'] or downgrade and status['downgrade']

    # ----- automatic node updates (自动更新节点) -----
    @staticmethod
    def _target_sha(built, session):
        binary = (built.get('binaries') or {}).get(install.binary_name(install.platform_of(session.info), session.info.get('arch', '')))
        return binary['sha256'] if binary else ''

    def _auto_due(self, sid, session, built):
        """Not a build that already failed to go onto this node in the last hours."""
        failed = self.auto_failed.get((sid, self._target_sha(built, session)))
        return failed is None or time.time() - failed >= AUTO_UPDATE_RETRY

    def _auto_record(self, sid, session, built, result):
        key = (sid, self._target_sha(built, session)) if session else None
        if key and (not result.get('ok') or result.get('confirmed') is False):
            self.auto_failed[key] = time.time()
        elif key:
            self.auto_failed.pop(key, None)

    async def auto_update(self, server_ids=None):
        """With 自动更新节点 on (and 主机 on): bring every online node that can be to the target build."""
        if not self.auto_update_enabled():
            return []
        results = []
        for row in await many(self.db.conn, 'SELECT DISTINCT owner FROM server_nodes'):
            results += (await self.update_nodes(row['owner'], server_ids, source='auto', auto=True))['items']
        return results

    async def _auto_update_loop(self):
        await asyncio.sleep(AUTO_UPDATE_DELAY * 2)
        while True:
            with contextlib.suppress(Exception):
                await self.auto_update()
            await asyncio.sleep(AUTO_UPDATE_EVERY)

    def _auto_soon(self, server_id):
        """A node that just came online is caught up shortly after, once its hello has settled."""
        if server_id in self.auto_pending or server_id in self.node_updates or not self.auto_update_enabled():
            return
        self.auto_pending.add(server_id)

        async def later():
            try:
                await asyncio.sleep(AUTO_UPDATE_DELAY)
                await self.auto_update([server_id])
            except Exception:  # noqa: BLE001 - the periodic pass tries again
                pass
            finally:
                self.auto_pending.discard(server_id)
        self.spawn(later())

    async def push_node_url(self, owner):
        """Move online nodes to their configured access points; each proves the new address first."""
        return await self.onboarding.push(owner)

    # ----- node callbacks -----
    async def node_seen(self, server_id, *, connected):
        async with self.db.write_transaction(label='server-node-seen') as conn:
            await conn.execute('UPDATE server_nodes SET seen_ms=? WHERE server_id=?', (now_ms(), server_id))
        self.ddns.node_seen(server_id, connected)

    async def node_hello(self, server_id, info, caps, version):
        row = await one(self.db.conn, 'SELECT owner,info_json FROM server_nodes WHERE server_id=?', (server_id,))
        if not row:
            return
        stored = json.loads(row['info_json'])
        # Hello carries no machine id; keep the one recorded at enrollment.
        machine = (stored.get('info') or {}).get('machineId')
        if machine:
            info = {**info, 'machineId': machine}
        # A node process just started says hello before it has looked up its public addresses
        # (it sends them in a later hello, and never blanks a known one): keep the last known
        # ones until then. Configs dialing it are built from this one.
        prior = stored.get('info') or {}
        # A value recorded before addresses were checked on arrival is kept only when it is one.
        known = {k: prior[k] for k in PUBLIC_IPS if not info.get(k) and clean_ip(prior.get(k), ADDRESS_FIELDS[k])}
        # The exit services abroad saw belongs with the IPv4 it was looked up with.
        if 'ipv4' in known and clean_ip(prior.get('ipv4Proxy'), 4):
            known['ipv4Proxy'] = prior['ipv4Proxy']
        elif info.get('ipv4Proxy'):
            # The node takes the address services in China see for its own; for a server the
            # user puts outside China, the one services abroad see is (a proxy back home).
            region = ((await self._profiles(row['owner'])).get(server_id) or {}).get('region') or ''
            if region and region.upper() != 'CN':
                info = {**info, 'ipv4': info['ipv4Proxy'], 'ipv4Proxy': info['ipv4']}
        if known:
            info = {**info, **known}
        # What reads the live session sees the addresses the record keeps.
        if (live := self.hub.sessions.get(server_id)) is not None:
            live.info = info
        # The build (sha256 of its executable), features and protocol go into the record too, so
        # a node's version can be judged and shown while it is offline.
        session = self.hub.sessions.get(server_id)
        identity = {}
        if session is not None:
            top = getattr(session, 'protocol', 1)
            identity = {'build': session.build, 'features': sorted(session.features), 'protocol': top, 'protocolMin': getattr(session, 'protocol_min', top)}
        value = {**stored, 'info': info, 'caps': caps, 'version': version, **identity}
        if value != stored:
            await self._merge_node_info(server_id, {'info': info, 'caps': caps, 'version': version, **identity})
        if caps.get('browser') != (stored.get('caps') or {}).get('browser'):
            await self.browsers.refresh()
        # A node updated to (or back from) one that takes a mesh address: one that chose ap-mesh
        # gets its addresses again.
        if session is not None and server_id != LOCAL and \
                (MESH_ACCESS_FEATURE in session.features) != (MESH_ACCESS_FEATURE in (stored.get('features') or ())) and \
                uses_mesh((await self._profiles(row['owner'])).get(server_id)):
            await self.mark_access_pending(server_id, True)
            stored['accessPending'] = True
        if stored.get('accessPending'):
            self._apply_access_soon(server_id)
        ip = info.get('ipv4') or info.get('ipv6')
        if ip and (stored.get('geo') or {}).get('ip') != ip and server_id not in self.geo_pending:
            self.geo_pending.add(server_id)
            self.spawn(self._locate(server_id))
        self.mesh.node_hello(server_id)
        # Its probes live only in the node's memory: sent again on every hello.
        self.ddns.node_hello(server_id)
        # 自动更新节点: one that was offline when its update came catches up now.
        self._auto_soon(server_id)

    async def _merge_node_info(self, server_id, patch):
        async with self.db.write_transaction(label='server-node-info') as conn:
            row = await (await conn.execute('SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))).fetchone()
            if row:
                await conn.execute('UPDATE server_nodes SET info_json=? WHERE server_id=?', (dumps({**json.loads(row['info_json']), **patch}), server_id))

    async def _locate(self, server_id):
        """Once per GEO_EVERY_S per node: one whose address keeps changing is looked up again
        when the time is up, at the address it has then."""
        try:
            wait = self.geo_at.get(server_id, -GEO_EVERY_S) + GEO_EVERY_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            row = await one(self.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))
            stored = json.loads(row['info_json']) if row else {}
            info = stored.get('info') or {}
            ip = info.get('ipv4') or info.get('ipv6')
            if not ip or (stored.get('geo') or {}).get('ip') == ip:
                return
            self.geo_at[server_id] = time.monotonic()
            found = await geo.lookup(self.http, ip)
            if found:
                await self._merge_node_info(server_id, {'geo': found})
        finally:
            self.geo_pending.discard(server_id)

    # ----- web terminal -----
    async def terminal(self, request, owner, server_id):
        """Relay one browser terminal; the shell lives exactly as long as this socket."""
        node = await one(self.db.conn, 'SELECT server_id FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=1024 * 1024)
        await ws.prepare(request)

        async def refuse(reason):
            if not ws.closed:
                await ws.send_json({'type': 'status', 'state': 'closed', 'reason': reason})
                await ws.close()
            return ws
        if not node:
            return await refuse('node_not_enrolled')
        try:
            cols = max(10, min(500, int(request.query.get('cols', 80))))
            rows = max(5, min(300, int(request.query.get('rows', 24))))
        except ValueError:
            cols, rows = 80, 24
        try:
            terminal = await self.hub.open_terminal(server_id, ws, cols, rows)
        except (LookupError, PermissionError) as exc:
            return await refuse(str(exc))
        await ws.send_json({'type': 'status', 'state': 'open'})
        terminal.ready.set()
        async with self.db.write_transaction(label='server-terminal-open') as conn:
            cursor = await conn.execute('INSERT INTO server_terminal_log(owner,server_id,started_ms,ip) VALUES(?,?,?,?)', (owner, server_id, now_ms(), (request.remote or '')[:64]))
            log_id = cursor.lastrowid
        last_input, reason = time.monotonic(), ''
        try:
            while not terminal.closed:
                try:
                    message = await ws.receive(timeout=TERMINAL_POLL_S)
                except TimeoutError:
                    # Idle: neither typed into nor printing (a build or a tail -f keeps it open).
                    if time.monotonic() - max(last_input, getattr(terminal, 'output_at', 0)) > TERMINAL_IDLE_S:
                        reason = 'idle'
                        break
                    continue
                if message.type == WSMsgType.BINARY:
                    last_input = time.monotonic()
                    await terminal.input(message.data)
                elif message.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and data.get('type') == 'resize' and all(isinstance(data.get(k), int) for k in ('cols', 'rows')):
                        await terminal.resize(max(10, min(500, data['cols'])), max(5, min(300, data['rows'])))
                else:
                    break
        except (ConnectionError, RuntimeError):
            pass
        finally:
            # aiohttp cancels this handler when the browser disconnects; the
            # shell must still be stopped and the session logged.
            async def end():
                await terminal.close(reason or 'closed')
                async with self.db.write_transaction(label='server-terminal-close') as conn:
                    await conn.execute('UPDATE server_terminal_log SET ended_ms=?,reason=? WHERE id=?', (now_ms(), terminal.reason, log_id))
                await refuse(terminal.reason)
            await asyncio.shield(self.spawn(end()))
        return ws

    # ----- saved terminal commands -----
    async def commands(self, owner):
        rows = await many(self.db.conn, 'SELECT id,name,command,sort FROM server_commands WHERE owner=? ORDER BY sort,id', (owner,))
        return {'ok': True, 'items': [dict(r) for r in rows]}

    async def save_command(self, owner, data, command_id=None):
        if not isinstance(data, dict) or set(data) - {'name', 'command'}:
            raise InstanceError('invalid_command')
        name, text = data.get('name'), data.get('command')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise InstanceError('command_name_required')
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise InstanceError('command_text_required')
        async with self.db.write_transaction(label='server-command') as conn:
            if command_id is None:
                last = await (await conn.execute('SELECT COALESCE(MAX(sort),0) FROM server_commands WHERE owner=?', (owner,))).fetchone()
                await conn.execute('INSERT INTO server_commands(owner,name,command,sort,updated_ms) VALUES(?,?,?,?,?)', (owner, name.strip(), text, last[0] + 1, now_ms()))
            else:
                cursor = await conn.execute('UPDATE server_commands SET name=?,command=?,updated_ms=? WHERE id=? AND owner=?', (name.strip(), text, now_ms(), command_id, owner))
                if not cursor.rowcount:
                    raise InstanceError('command_not_found', 404)
        return await self.commands(owner)

    async def delete_command(self, owner, command_id):
        async with self.db.write_transaction(label='server-command-delete') as conn:
            await conn.execute('DELETE FROM server_commands WHERE id=? AND owner=?', (command_id, owner))
        return await self.commands(owner)

    async def order_commands(self, owner, data):
        ids = data.get('ids') if isinstance(data, dict) else None
        if not isinstance(ids, list) or not all(type(i) is int for i in ids):
            raise InstanceError('invalid_command_order')
        async with self.db.write_transaction(label='server-command-order') as conn:
            for index, command_id in enumerate(ids):
                await conn.execute('UPDATE server_commands SET sort=? WHERE id=? AND owner=?', (index, command_id, owner))
        return await self.commands(owner)

    # ----- calls through a node -----
    async def _node_call(self, owner, server_id, method, params, *, timeout=60):
        node = await one(self.db.conn, 'SELECT server_id FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))
        if not node:
            raise InstanceError('node_not_enrolled', 404)
        try:
            reply = await self.hub.call(server_id, method, params, timeout)
        except LookupError:
            raise InstanceError('node_offline', 409) from None
        except (ConnectionError, TimeoutError):
            error = InstanceError('node_connection_lost', 502)
            error.effect_state = 'unknown'
            raise error from None
        if not reply.get('ok'):
            error = str(reply.get('error') or 'node_error')[:60]
            raise InstanceError(error, {'not_found': 404, 'permission_denied': 403, 'capability_disabled': 403}.get(error, 409))
        return reply.get('result') if isinstance(reply.get('result'), dict) else {}

    async def _log(self, owner, server_id, source, command, started, result):
        async with self.db.write_transaction(label='server-exec-log') as conn:
            await conn.execute('INSERT INTO server_exec_log(owner,server_id,source,command,started_ms,duration_ms,exit_code,outcome) VALUES(?,?,?,?,?,?,?,?)',
                               (owner, server_id, source, command[:2000], started, int(result.get('durationMs') or now_ms() - started),
                                result.get('exitCode'), result.get('outcome', '')))
            await conn.execute('DELETE FROM server_exec_log WHERE server_id=? AND id NOT IN (SELECT id FROM server_exec_log WHERE server_id=? ORDER BY id DESC LIMIT ?)',
                               (server_id, server_id, EXEC_LOG_KEEP))

    async def exec(self, owner, server_id, command, *, timeout=120, cwd='', stdin='', source='web', log=None, shell='', ssh_fallback=False):
        """Run one command; every attempt is logged (command text, never output). shell='sh'
        keeps the controller's own machine-read commands on plain /bin/sh. With ssh_fallback a
        server whose node is not connected runs it over its saved SSH login instead."""
        node = await one(self.db.conn, 'SELECT server_id FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))

        async def over_ssh(error, status):
            target = await self._ssh_target(owner, server_id) if ssh_fallback else None
            if not target:
                raise InstanceError(error, status)
            return await self._exec_ssh(owner, server_id, target, command, timeout=timeout, cwd=cwd, stdin=stdin, source=source, log=log)
        if not node:
            return await over_ssh('node_not_enrolled', 404)
        if server_id not in self.hub.sessions:
            return await over_ssh('node_offline', 409)
        started = now_ms()
        params = {'command': command, 'timeout': timeout, 'cwd': cwd, 'stdin': stdin, **({'shell': shell} if shell else {})}
        try:
            reply = await self.hub.call(server_id, 'exec', params, timeout + 15)
        except LookupError:
            # Gone before the call was sent: nothing ran.
            return await over_ssh('node_offline', 409)
        except (ConnectionError, TimeoutError):
            result = {'ok': False, 'error': 'node_connection_lost', 'effectState': 'unknown', 'outcome': 'unknown'}
        else:
            if not reply.get('ok'):
                result = {'ok': False, 'error': str(reply.get('error', 'node_error'))[:80], 'effectState': 'not_started', 'outcome': 'refused'}
            else:
                value = reply.get('result') if isinstance(reply.get('result'), dict) else {}
                timed_out = bool(value.get('timedOut'))
                result = {'ok': value.get('exitCode') == 0 and not timed_out, 'effectState': 'unknown' if timed_out else 'observed',
                          'outcome': 'timeout' if timed_out else 'ok' if value.get('exitCode') == 0 else 'failed',
                          **{k: value.get(k) for k in ('exitCode', 'stdout', 'stderr', 'truncated', 'timedOut', 'durationMs')},
                          **({'shell': value['shell']} if isinstance(value.get('shell'), str) and value['shell'] else {})}
        await self._log(owner, server_id, source, log or command, started, result)
        return result

    async def _ssh_target(self, owner, server_id):
        """The saved SSH login's connection material, or None without one."""
        if server_id == LOCAL:
            return None
        try:
            return await self.registry.target(owner, server_id)
        except InstanceError as exc:
            if exc.error == 'server_ssh_unavailable':
                return None
            raise

    async def _exec_ssh(self, owner, server_id, target, command, *, timeout, cwd, stdin, source, log):
        """One run over SSH, never retried: after a drop or a timeout the result is unknown."""
        if stdin:
            # The script itself travels on the remote shell's stdin.
            raise InstanceError('ssh_fallback_stdin_unsupported')
        platform = await self.registry.platform(target)
        line, body = remote.script(command, platform, cwd)
        started = now_ms()
        raw = await _ssh(self.db, target, line, stdin=body, timeout=timeout)
        await self.registry.remember_host_key(server_id, raw.get('knownHosts', ''))
        result = {**remote.result(raw, platform), 'durationMs': now_ms() - started}
        await self._log(owner, server_id, f'{source}-ssh', log or command, started, result)
        return result

    # ----- the node's browser -----
    async def browser_status(self, owner, server_id):
        session = self.hub.sessions.get(server_id)
        if not session or not session.caps.get('browser'):
            await self.registry.get(owner, server_id)
            return {'ok': True, 'browser': {'enabled': bool(session and session.caps.get('browser')), 'online': bool(session)}}
        return {'ok': True, 'browser': {'enabled': True, 'online': True, **await self._node_call(owner, server_id, 'browser.status', {}, timeout=15)}}

    async def set_browser(self, owner, server_id, data):
        """Turn the node's browser on or off. The node allows this only when it runs commands,
        which already gives full control of the machine; otherwise it is set where it runs."""
        if (not isinstance(data, dict) or set(data) - {'enabled', 'endpoint', 'download'} or type(data.get('enabled')) is not bool
                or type(data.get('download', False)) is not bool):
            raise InstanceError('invalid_browser_settings')
        endpoint, download = data.get('endpoint', ''), data.get('download', False) and not data.get('endpoint')
        if not isinstance(endpoint, str) or len(endpoint) > 300 or endpoint and urlsplit(endpoint).scheme not in {'ws', 'wss', 'http', 'https'}:
            raise InstanceError('invalid_browser_endpoint')
        if data['enabled'] and server_id == LOCAL:
            raise InstanceError('browser_local_default', 409)
        result = await self._node_call(owner, server_id, 'browser.config', {'enabled': data['enabled'], 'endpoint': endpoint, 'download': download}, timeout=30)
        how = f'（连接 {endpoint}）' if endpoint else '（测试版 Chrome）' if download else ''
        await self._log(owner, server_id, 'web', f"{'开启' if data['enabled'] else '关闭'}浏览器{how if data['enabled'] else ''}",
                        now_ms(), {'exitCode': 0, 'outcome': 'ok'})
        return {'ok': True, 'browser': result}

    async def restart_browser(self, owner, server_id):
        return {'ok': True, 'browser': await self._node_call(owner, server_id, 'browser.restart', {}, timeout=60)}

    # ----- the node as the instances' and gateway's command channel -----
    async def node_channels(self, owner, server_id=None):
        """Enrolled Linux nodes; ready ones run instance and gateway commands instead of SSH."""
        sql, args = 'SELECT server_id, info_json FROM server_nodes WHERE owner=?', (owner,)
        if server_id:
            sql, args = sql + ' AND server_id=?', (owner, server_id)
        out = {}
        for row in await many(self.db.conn, sql, args):
            stored = json.loads(row['info_json'])
            session = self.hub.sessions.get(row['server_id'])
            info = session.info if session else stored.get('info', {})
            if install.platform_of(info) != 'linux':
                continue
            caps = session.caps if session else stored.get('caps', {})
            out[row['server_id']] = {'online': bool(session), 'exec': caps.get('exec') is True,
                                     'address': self._node_address(session, info), 'publicIp': clean_ip(info.get('ipv4'), 4)}
        return out

    @staticmethod
    def _node_address(session, info):
        """Where the controller reaches the server: the source of a direct private connection
        (a WG access point); behind a proxied access point only the node's public IPv4 is known."""
        try:
            ip = ipaddress.ip_address(session.remote if session else '')
        except ValueError:
            return clean_ip(info.get('ipv4'), 4)
        return session.remote if ip.is_private and not ip.is_loopback else clean_ip(info.get('ipv4'), 4)

    async def run_command(self, owner, server_id, command, *, stdin=b'', timeout=120, source='instance'):
        """Same result shape as the instance registry's SSH runs. Node exec input is text,
        theirs may be a binary archive, so input goes through a private file."""
        if not stdin:
            result = await self.exec(owner, server_id, command, timeout=timeout, source=source, shell='sh')
        else:
            path, upload = f'/tmp/.openbear-input-{secrets.token_hex(8)}', secrets.token_hex(12)
            for offset in range(0, len(stdin), FILE_CHUNK):
                await self._write_chunk(owner, server_id, path, stdin[offset:offset + FILE_CHUNK], upload, offset,
                                        offset + FILE_CHUNK >= len(stdin), '600', False)
            # The name goes before the command starts; the opened file stays readable,
            # so a killed command leaves nothing behind.
            wrapped = f'{{ rm -f {path}; sh -c {shlex.quote(command)}; }} < {path}'
            result = await self.exec(owner, server_id, wrapped, timeout=timeout, source=source, log=command, shell='sh')
        return {'ok': bool(result.get('ok')), 'exitCode': result.get('exitCode'), 'timedOut': bool(result.get('timedOut')),
                'effectState': result.get('effectState', 'unknown'), 'stdout': result.get('stdout') or '',
                'stderr': result.get('stderr') or '', 'truncated': bool(result.get('truncated')), 'knownHosts': '',
                **({'error': result['error']} if result.get('error') else {})}

    async def exec_batch(self, owner, data):
        if not isinstance(data, dict) or set(data) - {'serverIds', 'command', 'timeout'}:
            raise InstanceError('invalid_exec')
        ids, command, timeout = data.get('serverIds'), data.get('command'), data.get('timeout', 120)
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise InstanceError('invalid_exec_targets')
        if not isinstance(command, str) or not command.strip() or len(command) > 8000:
            raise InstanceError('command_text_required')
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise InstanceError('invalid_exec_timeout')
        for sid in ids:
            await self.registry.get(owner, sid)
        cutoff = now_ms() - 3600000
        for job_id in [k for k, j in self.jobs.items() if j['createdAtMs'] < cutoff][:]:
            self.jobs.pop(job_id, None)
        while len(self.jobs) >= 30:
            self.jobs.pop(next(iter(self.jobs)))
        job = {'id': secrets.token_hex(8), 'owner': owner, 'command': command, 'timeout': timeout, 'createdAtMs': now_ms(),
               'items': {sid: {'state': 'running'} for sid in ids}}
        self.jobs[job['id']] = job
        gate = asyncio.Semaphore(BATCH_PARALLEL)

        async def run_one(sid):
            async with gate:
                try:
                    result = await self.exec(owner, sid, command, timeout=timeout, source='batch')
                except InstanceError as exc:
                    result = {'ok': False, 'error': exc.error, 'effectState': 'not_started'}
                except Exception:
                    result = {'ok': False, 'error': 'exec_failed', 'effectState': 'unknown'}
            for key in ('stdout', 'stderr'):
                if isinstance(result.get(key), str) and len(result[key]) > 65536:
                    result[key], result['truncated'] = clip_output(result[key], 65536)[0], True
            job['items'][sid] = {'state': 'done', **result}
        for sid in ids:
            self.spawn(run_one(sid))
        return {'ok': True, 'job': self._job(job)}

    @staticmethod
    def _job(job):
        items = [{'serverId': sid, **value} for sid, value in job['items'].items()]
        return {'id': job['id'], 'command': job['command'], 'timeout': job['timeout'], 'createdAtMs': job['createdAtMs'],
                'done': all(i['state'] == 'done' for i in items), 'items': items}

    async def exec_job(self, owner, job_id):
        job = self.jobs.get(job_id)
        if not job or job['owner'] != owner:
            raise InstanceError('exec_job_not_found', 404)
        return {'ok': True, 'job': self._job(job)}

    # ----- files through a node -----
    async def list_files(self, owner, server_id, path):
        return {'ok': True, **await self._node_call(owner, server_id, 'fs.list', {'path': path or '/'})}

    async def file_op(self, owner, server_id, data):
        """Start one file operation; long ones outlive a proxy's read timeout, so the page polls."""
        if not isinstance(data, dict) or not isinstance(data.get('op'), str):
            raise InstanceError('invalid_file_op')
        await self.registry.get(owner, server_id)
        session = self.hub.sessions.get(server_id)
        command, timeout = file_ops.command(data['op'], data, install.platform_of(session.info if session else {}))
        cutoff = now_ms() - 3600000
        for job_id in [k for k, j in self.file_jobs.items() if j['createdAtMs'] < cutoff]:
            self.file_jobs.pop(job_id, None)
        while len(self.file_jobs) >= 50:
            self.file_jobs.pop(next(iter(self.file_jobs)))
        job = {'id': secrets.token_hex(8), 'owner': owner, 'serverId': server_id, 'op': data['op'], 'createdAtMs': now_ms(), 'state': 'running'}
        self.file_jobs[job['id']] = job

        async def run():
            try:
                failed = file_ops.outcome(await self.exec(owner, server_id, command, timeout=timeout, source='files', shell='sh'))
            except InstanceError as exc:
                failed = (exc.error, '')
            except Exception:
                failed = ('file_op_failed', '')
            job.update(state='failed' if failed else 'done', error=failed[0] if failed else '', detail=failed[1] if failed else '')
        self.spawn(run())
        return {'ok': True, 'job': self._file_job(job)}

    @staticmethod
    def _file_job(job):
        return {k: job[k] for k in ('id', 'op', 'state', 'error', 'detail', 'createdAtMs') if k in job}

    async def file_op_job(self, owner, server_id, job_id):
        job = self.file_jobs.get(job_id)
        if not job or job['owner'] != owner or job['serverId'] != server_id:
            raise InstanceError('file_job_not_found', 404)
        return {'ok': True, 'job': self._file_job(job)}

    async def stat_file(self, owner, server_id, path):
        return await self._node_call(owner, server_id, 'fs.stat', {'path': path})

    async def read_file(self, owner, server_id, path, offset=0, limit=FILE_CHUNK):
        result = await self._node_call(owner, server_id, 'fs.read', {'path': path, 'offset': offset, 'limit': limit})
        result['data'] = base64.b64decode(result.get('data') or '')
        return result

    async def write_file(self, owner, server_id, path, chunks, *, mode='', mkdirs=False, source='web', label='写入文件'):
        """Write an async iterable of byte chunks; the target changes only after the last one."""
        started, upload, offset = now_ms(), secrets.token_hex(12), 0
        pending = None
        async for chunk in chunks:
            if pending is not None:
                await self._write_chunk(owner, server_id, path, pending, upload, offset, False, mode, mkdirs)
                offset += len(pending)
            pending = chunk
        await self._write_chunk(owner, server_id, path, pending or b'', upload, offset, True, mode, mkdirs)
        size = offset + len(pending or b'')
        await self._log(owner, server_id, source, f'{label} {path}（{size} 字节）', started, {'exitCode': 0, 'outcome': 'ok'})
        return {'ok': True, 'path': path, 'size': size}

    # ----- node methods newer than protocol 1 -----
    async def _call_newer(self, owner, server_id, feature, method, params, *, timeout=60):
        """A node call that an older node build lacks: refused up front, never emulated."""
        session = self.hub.sessions.get(server_id)
        if session and session.caps.get('exec') and feature not in session.features:
            raise InstanceError('node_update_required', 409)
        try:
            return await self._node_call(owner, server_id, method, params, timeout=timeout)
        except InstanceError as exc:
            if exc.error == 'unsupported_method':
                raise InstanceError('node_update_required', 409) from None
            raise

    async def edit_file(self, owner, server_id, path, edits, *, source='web', label='修改文件'):
        """Exact replacements made by the node between one read and one atomic write:
        all of them, or none when any edit is missing, ambiguous or overlaps another."""
        if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
            raise InstanceError('invalid_edits')
        clean, size = [], 0
        for e in edits:
            if (not isinstance(e, dict) or set(e) - {'old', 'new', 'replaceAll'} or not isinstance(e.get('old'), str) or not e['old']
                    or not isinstance(e.get('new'), str) or type(e.get('replaceAll', False)) is not bool):
                raise InstanceError('invalid_edits')
            size += len(e['old'].encode()) + len(e['new'].encode())
            clean.append({'old': e['old'], 'new': e['new'], 'replaceAll': e.get('replaceAll', False)})
        if size > EDIT_LIMIT:
            raise InstanceError('invalid_edits')
        await self.registry.get(owner, server_id)
        session = self.hub.sessions.get(server_id)
        platform = install.platform_of(session.info if session else {})
        target = file_ops.unprotected([file_ops.path(path, platform)], platform)[0]
        started, text = now_ms(), f'{label} {target}（{len(clean)} 处）'
        try:
            result = await self._call_newer(owner, server_id, 'fs.edit', 'fs.edit', {'path': target, 'edits': clean})
        except InstanceError as exc:
            if exc.error not in {'node_offline', 'node_not_enrolled', 'node_update_required', 'node_connection_lost'}:
                await self._log(owner, server_id, source, text, started, {'outcome': 'refused'})
            raise
        if result.get('failures'):
            await self._log(owner, server_id, source, text, started, {'outcome': 'refused'})
            return {'ok': False, 'error': 'edit_not_applied', 'path': target, 'failures': result['failures'][:MAX_EDITS]}
        await self._log(owner, server_id, source, text, started, {'exitCode': 0, 'outcome': 'ok'})
        return {'ok': True, 'path': target, **{k: result[k] for k in ('changed', 'replacements', 'size', 'diff', 'diffTruncated', 'lineEndings') if k in result}}

    # ----- background jobs on a node -----
    @staticmethod
    def _job_view(job):
        job = job if isinstance(job, dict) else {}
        return {'id': str(job.get('id') or ''), 'command': str(job.get('command') or ''), 'cwd': str(job.get('cwd') or ''),
                'shell': str(job.get('shell') or ''), 'state': str(job.get('state') or ''), 'exitCode': job.get('exitCode'),
                **({'signal': str(job['signal'])} if job.get('signal') else {}),
                'startedAtMs': job.get('startedAt') or 0, 'endedAtMs': job.get('endedAt') or None, 'durationMs': job.get('durationMs')}

    @staticmethod
    def _job_log(log):
        log = log if isinstance(log, dict) else {}
        return {'data': str(log.get('data') or ''), **{k: int(log.get(k) or 0) for k in ('offset', 'next', 'size', 'first')}}

    async def job_start(self, owner, server_id, command, *, cwd='', stdin='', source='web', log=None):
        """Start a command that runs on apart from this connection; returns at once. log: what the
        command record says instead of the command itself."""
        if not isinstance(command, str) or not command.strip() or len(command) > JOB_COMMAND_MAX:
            raise InstanceError('command_required')
        if not isinstance(cwd, str) or len(cwd) > 4096 or not isinstance(stdin, str) or len(stdin.encode()) > JOB_STDIN_MAX:
            raise InstanceError('invalid_job')
        await self.registry.get(owner, server_id)
        started = now_ms()
        try:
            result = await self._call_newer(owner, server_id, 'jobs', 'job.start', {'command': command, 'cwd': cwd, 'stdin': stdin}, timeout=30)
        except InstanceError as exc:
            if exc.error not in {'node_offline', 'node_not_enrolled', 'node_update_required'}:
                await self._log(owner, server_id, source, log or f'后台任务：{command}', started,
                                {'outcome': 'unknown' if exc.error == 'node_connection_lost' else 'refused'})
            raise
        job = self._job_view(result)
        await self._log(owner, server_id, source, f'{log}（后台任务 {job["id"]}）' if log else f'后台任务 {job["id"]}：{command}', started,
                        {'outcome': 'started'})
        return {'ok': True, 'job': job}

    async def jobs_list(self, owner, server_id):
        await self.registry.get(owner, server_id)
        result = await self._call_newer(owner, server_id, 'jobs', 'job.list', {}, timeout=30)
        return {'ok': True, 'jobs': [self._job_view(j) for j in result.get('jobs') or []]}

    async def job_status(self, owner, server_id, job_id, *, offset=None, limit=None):
        """The job and part of its output: the tail, or from offset (bytes the job ever wrote)."""
        if not isinstance(job_id, str) or not re.fullmatch(r'[a-z0-9]{6,40}', job_id):
            raise InstanceError('job_not_found', 404)
        if offset is not None and (type(offset) is not int or offset < 0) or limit is not None and (type(limit) is not int or limit < 1):
            raise InstanceError('invalid_offset')
        params = {'id': job_id, **({'offset': offset} if offset is not None else {}), **({'limit': limit} if limit else {})}
        result = await self._call_newer(owner, server_id, 'jobs', 'job.status', params, timeout=30)
        return {'ok': True, 'job': self._job_view(result.get('job')), 'log': self._job_log(result.get('log'))}

    async def job_stop(self, owner, server_id, job_id, *, source='web', log=None):
        """End the job's whole process group (Windows: process tree)."""
        if not isinstance(job_id, str) or not re.fullmatch(r'[a-z0-9]{6,40}', job_id):
            raise InstanceError('job_not_found', 404)
        started = now_ms()
        result = await self._call_newer(owner, server_id, 'jobs', 'job.stop', {'id': job_id}, timeout=45)
        job = self._job_view(result.get('job'))
        if result.get('stopped'):
            await self._log(owner, server_id, source, log or f'停止后台任务 {job_id}：{job["command"]}', started, {'exitCode': job['exitCode'], 'outcome': 'ok'})
        return {'ok': True, 'stopped': bool(result.get('stopped')), 'job': job}

    async def job_wait(self, owner, server_id, job_id, *, until=None, offset=0, timeout=60, tail=8000):
        """Wait until the job ends, its output from offset matches until, or timeout seconds
        pass; reason says which. next is where newer output starts, for another wait."""
        if type(timeout) is not int or not 1 <= timeout <= JOB_WAIT_MAX:
            raise InstanceError('invalid_timeout')
        if type(offset) is not int or offset < 0:
            raise InstanceError('invalid_offset')
        pattern = None
        if until is not None:
            if not isinstance(until, str) or not until or len(until) > 500:
                raise InstanceError('invalid_pattern')
            try:
                pattern = re.compile(until, re.M)
            except re.error:
                raise InstanceError('invalid_pattern') from None
        deadline, position, window, looked = time.monotonic() + timeout, offset, '', False
        while True:
            round_start = time.monotonic()
            status = await self.job_status(owner, server_id, job_id, offset=position, limit=JOB_MATCH_WINDOW)
            job, log = status['job'], status['log']
            if log['offset'] > position:
                window = ''  # older output was dropped by the node's log limit
            if pattern:
                window += log['data']
                # Output already looked at holds no match: only new output is looked for again.
                found = await self._search(until, window) if log['data'] or not looked else None
                looked = True
                if found:
                    # Resume after the match: the output past it has not been looked at by the caller.
                    after = log['next'] - len(window[found[1]:].encode())
                    return {'ok': True, 'reason': 'matched', 'match': window[found[0]:found[1]][:500], 'next': after, 'job': job}
                window = window[-JOB_MATCH_WINDOW:]
            progressed = log['next'] > position
            position = max(position, log['next'])
            if progressed and position < log['size'] and time.monotonic() < deadline:
                await asyncio.sleep(max(0.0, round_start + JOB_WAIT_ROUND_S - time.monotonic()))
                continue
            if job['state'] != 'running':
                reason = 'exited'
                break
            if time.monotonic() >= deadline:
                reason = 'timeout'
                break
            await asyncio.sleep(1)
        last = await self.job_status(owner, server_id, job_id, limit=tail) if tail else {'log': {'data': ''}}
        return {'ok': True, 'reason': reason, 'next': position, 'job': last.get('job', job), 'tail': last['log']['data']}

    @staticmethod
    async def _search(pattern, text):
        """(start, end) of pattern's first match in text, or None; refused as pattern_timeout when
        looking takes longer than JOB_MATCH_TIMEOUT_S."""
        proc = await asyncio.create_subprocess_exec(sys.executable, '-I', '-S', '-c', _MATCHER, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(json.dumps({'p': pattern, 's': text}).encode()), JOB_MATCH_TIMEOUT_S)
        except TimeoutError:
            raise InstanceError('pattern_timeout') from None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        found = json.loads(out or b'null') if proc.returncode == 0 else None
        return tuple(found) if found else None

    async def _write_chunk(self, owner, server_id, path, data, upload, offset, done, mode, mkdirs):
        await self._node_call(owner, server_id, 'fs.write', {'path': path, 'data': base64.b64encode(data).decode(), 'upload': upload,
                                                            'offset': offset, 'done': done, 'mode': mode, 'mkdirs': mkdirs}, timeout=120)
