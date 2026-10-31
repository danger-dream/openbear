"""Dynamic DNS on the main controller: provider accounts, records and their events.

Every record has a desired value, worked out from its source (a node's probe, the address
its node connects from, or a fixed value), and is kept equal to it at the provider:
read, compare, write only when different, read back. Credentials stay in the vault
(memory_secrets); accounts hold only the secret's id and which of its keys fill which field."""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import random
import re
import secrets
import time
import weakref
from urllib.parse import urlsplit

from app.ddns import importer, model, providers
from app.ddns.model import TYPES, DdnsError, fail
from app.ddns.providers.base import ProviderError, Record
from app.ddns.schema import migrate
from app.instances.servers import LOCAL
from app.servers.reminders import quoted
from app.webhooks.repository import many, one

log = logging.getLogger(__name__)

TICK_S = 15
CHECK_S = 600
OFFLINE_S = 120
RETRY_MIN_S, RETRY_MAX_S = 30, 1800
REWRITE_S = 24 * 3600
ZONES_S = 3600
FIGHT_WINDOW_S = 3600
# Waits before reverting the 1st, 2nd, ... external change within FIGHT_WINDOW_S.
FIGHT_WAITS_S = (0, 60, 300, 900, 3600)
FAIL_ALERT_COUNT, FAIL_ALERT_S = 3, 600
DRIFT_ALERT_S = 3600
# At most WRITE_MAX writes of one record within WRITE_WINDOW_S, but on request: a source flapping
# between two addresses would otherwise write the provider on every report.
WRITE_MAX, WRITE_WINDOW_S = 10, 3600
EVENTS_KEEP_S, EVENTS_PER_RECORD = 30 * 86400, 200
ACCOUNT_PARALLEL, ACCOUNT_RATE = 2, 4
SYNC_WAIT_S = 20
PROBE_TIMEOUT_S, WATCH_TIMEOUT_S = 45, 15
DETECT_CACHE_S = 60
# How fresh a mesh member's report must be for what it sees of a peer to count.
MESH_SEEN_S = 600
MAX_ROWS = 100
SECRET_NOTE = '动态域名服务商凭据'
FORCED = ('manual', 'config')
# Errors of this module's own that a later attempt may get past.
RETRYABLE = frozenset({'verify_failed', 'node_offline', 'timeout'})
SOURCE_STATES = frozenset({'offline', 'no_address', 'node_outdated', 'conn_unavailable', 'probe_refused'})
SOFT_STATES = SOURCE_STATES | {'ok', 'pending', 'syncing'}
ISSUE_STATES = SOURCE_STATES | {'drift', 'fighting', 'failed', 'ambiguous'}
JSON_FIELDS = ('source', 'backup', 'options', 'policy', 'drift_times')
STATE_TEXT = {'ok': '正常', 'pending': '等待同步', 'syncing': '正在同步', 'drift': '被外部改动', 'fighting': '另一个系统也在写这条记录',
              'offline': '来源离线，DNS 保持不变', 'failed': '同步失败，处理后手动同步', 'paused': '已停用', 'no_address': '取不到可用地址',
              'node_outdated': '节点版本过旧，需要更新节点', 'conn_unavailable': '连接来源地址不可用',
              'probe_refused': '节点拒绝了取 IP 参数', 'ambiguous': '认不出服务商上哪条同名记录归 OpenBear 管理，没有写入'}
PROBE_ERRORS = {'timeout': '超时', 'no_address': '没有地址', 'http_error': '查询接口出错', 'parse_error': '无法解析结果',
                'capability_disabled': '节点关闭了命令执行', 'iface_not_found': '找不到网卡', 'unsupported': '节点不支持'}
METHOD_TEXT = {'url': '接口', 'url_cn': '接口（国内）', 'url_intl': '接口（国外）', 'nic': '网卡', 'stun': 'STUN', 'cmd': '命令', 'conn': '连接来源', 'fixed': '固定值'}


def now_ms():
    return int(time.time() * 1000)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


SCRUB_MIN, FRAGMENT_MIN = 4, 6
# Words of a message, split where a credential would end (quotes, separators of headers and queries).
_WORDS = re.compile(r'[^\s"\'`,;&?=:<>()\[\]{}|\\]+')


def _pieces(creds):
    """What of each credential value may turn up in a message, longest first: the value, each
    of its lines, and in a line what follows a "key:" or "key=" and its last word (a header's token)."""
    out = set()
    for value in (creds or {}).values():
        if not isinstance(value, str):
            continue
        out.add(value.strip())
        for line in value.splitlines():
            line = line.strip()
            out.update((line, line.partition(':')[2].strip(), line.partition('=')[2].strip(), (line.split() or [''])[-1]))
    return sorted((p for p in out if len(p) >= SCRUB_MIN), key=len, reverse=True)


def scrub(text, creds):
    """An error text without any credential in it: masked before it is cut, so cutting cannot leave
    part of one, and a word of FRAGMENT_MIN or more that occurs inside one (a credential the
    provider or a library cut) is masked too."""
    text = str(text or '')
    pieces = _pieces(creds)
    for piece in pieces:
        text = text.replace(piece, '***')
    if pieces:
        text = _WORDS.sub(lambda m: _fragment(m[0], pieces), text)
    return text[:500]


def _fragment(word, pieces):
    # A cut is often marked with dots or an ellipsis, which are not part of the credential.
    core = word.strip('.…')
    if len(core) >= FRAGMENT_MIN and any(core in p for p in pieces):
        return word.replace(core, '***', 1)
    return word


def decode(row):
    rec = dict(row)
    for field in JSON_FIELDS:
        rec[field] = json.loads(row[field + '_json'])
    rec['policy'] = {**model.DEFAULT_POLICY, **rec['policy']}
    rec['family'] = TYPES[row['rtype']]
    return rec


def decode_account(row):
    return {**row, 'settings': model.clean_settings(None, json.loads(row['settings_json'])), 'zones': json.loads(row['zones_json']),
            'field_map': json.loads(row['field_map_json'])}


def readable(kind):
    cls = providers.get(kind)
    return bool(cls and cls.caps.read)


def say(seconds):
    return f'{seconds // 60} 分钟' if seconds >= 60 else f'{seconds} 秒'


class Ddns:
    def __init__(self, service):
        self.service = service
        self.obs = {}       # server id -> {probe key: {value, error, detail, fails, at, stale}}
        self.sent = {}      # server id -> the probes it last accepted
        self.offered = {}   # server id -> keys of the ddns.watch under way (reports may come before its answer)
        self.refused = {}   # server id -> the error it refused its probes with
        self.writes = {}    # record id -> when it was written lately (WRITE_MAX)
        self.zone_locks = collections.defaultdict(asyncio.Lock)  # (account id, zone) of a whole-zone provider
        self.record_locks = weakref.WeakValueDictionary()
        self.account_locks = weakref.WeakValueDictionary()
        self.held = {}      # record id -> when a write _throttled put off is due
        self.left = {}      # server id -> when its node went offline
        self.problems = {}  # record id -> (state, text) its source is in
        self.running = {}   # record id -> the task syncing it
        self.again = {}     # record id -> why it syncs once more after the running one
        self.gates = {}     # account id -> (concurrency, times of recent calls)
        self.locks = collections.defaultdict(asyncio.Lock)
        self.detected = {}
        self.tasks = set()
        self.started_ms = 0
        self.pruned_ms = 0
        self.now = now_ms
        # The request rate is measured in real time, apart from the clock records are scheduled on.
        self.monotonic = time.monotonic
        self.sleep = asyncio.sleep
        self.jitter = lambda seconds: seconds * random.uniform(0.9, 1.1)

    @property
    def db(self):
        return self.service.db

    @property
    def hub(self):
        return self.service.hub

    async def start(self):
        await migrate(self.db)
        self.started_ms = now = self.now()
        async with self.db.write_transaction(label='ddns-start') as conn:
            await conn.execute("UPDATE ddns_records SET state='pending' WHERE state='syncing'")
            # A write the last run did not get to: a provider that cannot be read would otherwise
            # wait for its daily rewrite. Retry backoff and the wait before reverting an outside change stay.
            await conn.execute("UPDATE ddns_records SET next_ms=? WHERE enabled=1 AND fail_count=0 AND state NOT IN ('failed','drift','fighting') "
                               "AND desired!='' AND desired!=remote AND next_ms>?", (now, now))
        self.service.spawn(self._loop())

    async def _loop(self):
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the next tick tries again
                log.exception('ddns tick failed')

    def _spawn(self, coro):
        task = self.service.spawn(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def idle(self):
        """Wait until no evaluation or sync this service started is running."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    # ----- storage -----
    def _record_lock(self, rid):
        return self.record_locks.setdefault(rid, asyncio.Lock())

    def _account_lock(self, aid):
        return self.account_locks.setdefault(aid, asyncio.Lock())

    async def _set(self, rid, *, _enabled_only=False, **fields):
        cols = {(k + '_json' if k in JSON_FIELDS else k): (dumps(v) if k in JSON_FIELDS else v) for k, v in fields.items()}
        async with self.db.write_transaction(label='ddns-record') as conn:
            where = ' AND enabled=1' if _enabled_only else ''
            cursor = await conn.execute(f'UPDATE ddns_records SET {",".join(c + "=?" for c in cols)} WHERE id=?{where}', (*cols.values(), rid))
            changed = cursor.rowcount > 0
            await cursor.close()
            return changed

    async def _rec(self, owner, rid):
        row = await one(self.db.conn, 'SELECT * FROM ddns_records WHERE id=? AND owner=?', (rid, owner))
        if not row:
            fail('record_not_found', 404)
        return decode(row)

    async def _account(self, owner, aid):
        row = await one(self.db.conn, 'SELECT * FROM ddns_accounts WHERE id=? AND owner=?', (aid, owner))
        if not row:
            fail('account_not_found', 404)
        return decode_account(row)

    async def _accounts(self, owner):
        return [decode_account(r) for r in await many(self.db.conn, 'SELECT * FROM ddns_accounts WHERE owner=? ORDER BY sort,created_ms', (owner,))]

    async def _set_account(self, aid, **fields):
        async with self.db.write_transaction(label='ddns-account') as conn:
            await conn.execute(f'UPDATE ddns_accounts SET {",".join(c + "=?" for c in fields)} WHERE id=?', (*fields.values(), aid))

    async def _event(self, owner, rid, kind, text, detail=None):
        async with self.db.write_transaction(label='ddns-event') as conn:
            await conn.execute('INSERT INTO ddns_events(owner,record_id,at_ms,kind,text,detail_json) VALUES(?,?,?,?,?,?)',
                               (owner, rid, self.now(), kind, text, dumps(detail or {})))
            await conn.execute('DELETE FROM ddns_events WHERE record_id=? AND id NOT IN (SELECT id FROM ddns_events WHERE record_id=? ORDER BY id DESC LIMIT ?)',
                               (rid, rid, EVENTS_PER_RECORD))

    async def _alert(self, owner, key, kind, text, server_id=''):
        await self.service.reminders._enqueue(owner, key, server_id or '', kind, text)

    async def _own_node(self, owner, server_id):
        if not await one(self.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner)):
            fail('server_not_found', 404)

    # ----- credentials (the vault) -----
    def _memory(self):
        from app.memory.builtin import BuiltinMemoryClient
        identity = getattr(getattr(getattr(self.service.host, 'config', None), 'memory', None), 'identity', None) or 'openbear'
        return BuiltinMemoryClient(self.db, identity=identity)

    async def _secret_kv(self, secret_id):
        row = await one(self.db.conn, 'SELECT id,name,note,kv_json FROM memory_secrets WHERE id=? AND archived=0', (secret_id,))
        if not row:
            return None, []
        try:
            items = json.loads(row['kv_json'] or '[]')
        except ValueError:
            items = []
        return row, [i for i in items if isinstance(i, dict)]

    async def _creds(self, account):
        row, items = await self._secret_kv(account['secret_id'])
        if row is None:
            fail('secret_missing', 409)
        values = {str(i.get('key') or ''): i.get('value') for i in items}
        return {field: values[key] for field, key in account['field_map'].items() if isinstance(values.get(key), str) and values[key] != ''}

    @staticmethod
    def _own_secret(row):
        return bool(row) and row['note'] == SECRET_NOTE and row['name'].startswith('ddns/')

    async def _ref_creds(self, cls, ref):
        """Credentials of a secretRef: only a secret this module saved, never another vault entry."""
        secret_id, mapping = model.clean_secret_ref(cls, ref)
        row, items = await self._secret_kv(secret_id)
        if row is None:
            fail('secret_not_found', 404)
        if not self._own_secret(row):
            fail('secret_not_ddns', 403)
        keys = {str(i.get('key') or '') for i in items}
        mapping = {f: k for f, k in mapping.items() if k in keys}
        creds = await self._creds({'secret_id': secret_id, 'field_map': mapping})
        model.missing_fields(cls, creds)
        return secret_id, mapping, creds

    async def _secret_name(self, cls, alias):
        base = f'ddns/{cls.name}-{alias}'
        name, n = base, 1
        while await one(self.db.conn, 'SELECT 1 FROM memory_secrets WHERE name=?', (name,)):
            n += 1
            name = f'{base}-{n}'
        return name

    async def _save_secret(self, cls, alias, values, secret_id=0):
        """Write credential values to the vault through the credentials page's own writer: into the
        account's own secret when it has one (blank fields keep their value), else a new one."""
        row, items = await self._secret_kv(secret_id) if secret_id else (None, [])
        args = {'action': 'set'}
        if self._own_secret(row):
            known = {str(i.get('key') or ''): i for i in items}
            merged = {k: i.get('value') for k, i in known.items()}
            merged.update(values)
            model.missing_fields(cls, merged)
            items = [{**i, 'value': values[i.get('key')]} if i.get('key') in values else i for i in items]
            items += [{'key': k, 'value': v} for k, v in values.items() if k not in known]
            args.update(id=secret_id)
        else:
            model.missing_fields(cls, values)
            items = [{'key': k, 'value': v} for k, v in values.items()]
            args.update(name=await self._secret_name(cls, alias), note=SECRET_NOTE)
        result = await self._memory().tool_call('secret', {**args, 'kvJson': json.dumps(items, ensure_ascii=False)})
        if not result.get('ok'):
            fail('secret_save_failed', 500)
        return int(result['item']['id'])

    async def _input_creds(self, cls, data):
        if data.get('credentials') is not None:
            return model.clean_credentials(cls, data['credentials'])
        if data.get('secretRef') is not None:
            return (await self._ref_creds(cls, data['secretRef']))[2]
        fail('credentials_required')

    # ----- providers -----
    @staticmethod
    def _cls(kind):
        cls = providers.get(kind) if isinstance(kind, str) else None
        if cls is None:
            fail('provider_unknown')
        return cls

    def _make(self, kind, creds, settings):
        proxy = (settings or {}).get('proxy') or ''
        # Saved before socks proxies were refused; the URL is not repeated (it may hold a password).
        if proxy and urlsplit(proxy).scheme.lower() not in model.PROXY_SCHEMES:
            fail('invalid_proxy', message='账户设置里的代理不再支持（只支持 http 和 https 代理），请在服务商账户的高级选项里修改或清空')
        return self._cls(kind)(creds, settings)

    @staticmethod
    def _tool_settings(data, prior=None):
        """Settings given through the agent tool: the API endpoint is the web page's alone (credentials
        go wherever it points), so they may only repeat the account's current one."""
        settings = data.get('settings') if isinstance(data, dict) else None
        if isinstance(settings, dict) and 'endpoint' in settings and (settings['endpoint'] or '') != ((prior or {}).get('endpoint') or ''):
            fail('endpoint_web_only', 403)

    @staticmethod
    def _tool_ref(cls, data):
        """A secretRef given through the agent tool: a callback's URL is one of its credentials, so mapped
        from another account's secret it would send that account's values where the agent chose."""
        if cls.type == 'callback' and isinstance(data, dict) and data.get('secretRef') is not None:
            fail('secret_ref_web_only', 403)

    async def _shared(self, secret_id, aid):
        """Whether an account other than aid reads this secret (it got there by a secretRef)."""
        return bool(await one(self.db.conn, 'SELECT 1 FROM ddns_accounts WHERE secret_id=? AND id!=?', (secret_id, aid)))

    def _zone_lock(self, aid, zone, p):
        """Held around the writes of a provider that rewrites the whole zone each time: two at
        once would each write back the zone as it was before the other's change."""
        return self.zone_locks[(aid, zone)] if p.caps.whole_zone else contextlib.nullcontext()

    def _gate(self, aid):
        if aid not in self.gates:
            self.gates[aid] = (asyncio.Semaphore(ACCOUNT_PARALLEL), collections.deque())
        return self.gates[aid]

    async def _call(self, aid, fn, *args):
        """One provider operation: at most ACCOUNT_PARALLEL at once and ACCOUNT_RATE a second per account."""
        gate, calls = self._gate(aid)
        async with gate:
            while True:
                now = self.monotonic()
                while calls and now - calls[0] >= 1:
                    calls.popleft()
                if len(calls) < ACCOUNT_RATE:
                    calls.append(now)
                    break
                await self.sleep(1 - (now - calls[0]))
            return await fn(*args)

    async def _remote(self, account, zone, name, rtype, creds=None, opts=None):
        creds = creds if creds is not None else await self._creds(account)
        try:
            async with self._make(account['type'], creds, account['settings']) as p:
                return p.on_line(await self._call(account['id'], p.records, zone, name, rtype), opts)
        except ProviderError as exc:
            raise DdnsError(exc.code, 400, model.MESSAGES.get(exc.code, '') + '：' + scrub(exc.message, creds)) from None

    async def _check(self, account_id, kind, creds, settings):
        try:
            async with self._make(kind, creds, settings) as p:
                try:
                    result = await self._call(account_id, p.check)
                except ProviderError as exc:
                    # Capabilities of the instance: configured zones make some providers list them.
                    if exc.code != 'unsupported' or p.caps.zones:
                        raise
                    result = {'zones': [], 'writable': None}
        except ProviderError as exc:
            raise DdnsError(exc.code, 400, model.MESSAGES.get(exc.code, '') + '：' + scrub(exc.message, creds)) from None
        zones = sorted({str(z).lower().rstrip('.') for z in result.get('zones') or [] if isinstance(z, str) and z})
        writable = result.get('writable')
        return zones, writable if isinstance(writable, bool) else None

    async def _listable(self, account):
        """Whether the account can list zones: asked of an instance built with its credentials,
        since configured zones turn listing on for providers that otherwise cannot."""
        cls = providers.get(account['type'])
        if cls is None:
            return False
        if cls.caps.zones:
            return True
        try:
            async with self._make(account['type'], await self._creds(account), account['settings']) as p:
                return p.caps.zones
        except (DdnsError, ProviderError):
            return False

    async def _refresh_zones(self, account, *, strict=False):
        """Zones the account can manage, kept for an hour; with strict a failure is raised."""
        now = self.now()
        try:
            creds = await self._creds(account)
            zones, writable = await self._check(account['id'], account['type'], creds, account['settings'])
        except DdnsError as exc:
            await self._set_account(account['id'], zones_ms=now, state='error', error=exc.message[:300], error_code=exc.error)
            if strict:
                raise
            return None, None
        await self._set_account(account['id'], zones_json=dumps(zones), zones_ms=now, state='ok', error='', error_code='')
        return zones, writable

    # ----- views -----
    def _issue(self, rec):
        return rec['enabled'] and (rec['state'] in ISSUE_STATES or rec['state'] == 'pending' and rec['fail_count'] > 0)

    def _state_text(self, rec, can_read):
        state = rec['state']
        problem = self.problems.get(rec['id'])
        if problem and problem[0] == state:
            text = problem[1]
        elif state == 'pending' and rec['fail_count']:
            text = '同步出错，稍后自动重试'
        elif state == 'pending' and rec['id'] in self.held:
            text = f'地址变化太频繁，暂缓写入（{say(max(0, self.held[rec["id"]] - self.now()) // 1000)}后）'
        elif state == 'drift' and rec['policy']['onDrift'] == 'alert':
            text = '被外部改动（只告警，不改回）'
        elif state == 'fighting':
            text = '另一个系统也在写这条记录，OpenBear 会拉长间隔后改回'
        else:
            text = STATE_TEXT.get(state, state)
        if not can_read:
            text += '；服务商不支持读取，无法检测外部改动'
        return text

    def _view(self, rec, kind):
        src = rec['source']
        can_read = readable(kind)
        node = src['kind'] == 'node'
        return {'id': rec['id'], 'accountId': rec['account_id'], 'zone': rec['zone'], 'name': rec['name'], 'type': rec['rtype'],
                'source': {'kind': src['kind'], 'serverId': src['serverId'], 'method': src['method'] if node else '',
                           'methodAuto': node and src['method'] == 'auto', 'resolvedMethod': model.method_of(src, rec['family']),
                           'params': src['params'], 'fixed': src['fixed']},
                'backup': rec['backup'], 'ttl': rec['ttl'], 'options': rec['options'], 'policy': rec['policy'], 'enabled': bool(rec['enabled']),
                'desired': rec['desired'], 'remote': rec['remote'], 'remoteAt': rec['remote_ms'], 'providerRecordId': rec['provider_record_id'],
                'state': rec['state'], 'stateText': self._state_text(rec, can_read), 'error': rec['error'], 'errorCode': rec['error_code'],
                'failCount': rec['fail_count'], 'changedAt': rec['changed_ms'],
                # synced_ms is the change last brought to the provider, remote_ms when it was last read or written.
                'syncedAt': rec['remote_ms'] if rec['synced_ms'] else 0, 'checkedAt': rec['checked_ms'],
                'activeSource': rec['active_source'], 'readable': can_read}

    async def record_view(self, owner, rid):
        rec = await self._rec(owner, rid)
        account = await one(self.db.conn, 'SELECT type FROM ddns_accounts WHERE id=?', (rec['account_id'],))
        return self._view(rec, account['type'] if account else '')

    async def _secret_names(self, ids):
        ids = [i for i in ids if i]
        if not ids:
            return {}
        rows = await many(self.db.conn, f'SELECT id,name FROM memory_secrets WHERE id IN ({",".join("?" * len(ids))})', tuple(ids))
        return {r['id']: r['name'] for r in rows}

    def _account_view(self, account, names, records):
        mine = [r for r in records if r['account_id'] == account['id']]
        return {'id': account['id'], 'type': account['type'], 'alias': account['alias'], 'zones': account['zones'], 'zonesAt': account['zones_ms'],
                'state': account['state'], 'error': account['error'], 'errorCode': account['error_code'],
                'secret': {'id': account['secret_id'], 'name': names.get(account['secret_id'], '')},
                'settings': account['settings'], 'records': len(mine), 'issues': sum(1 for r in mine if self._issue(r))}

    async def account_view(self, owner, aid):
        account = await self._account(owner, aid)
        records = [decode(r) for r in await many(self.db.conn, 'SELECT * FROM ddns_records WHERE account_id=?', (aid,))]
        return self._account_view(account, await self._secret_names([account['secret_id']]), records)

    async def nodes(self, owner):
        rows = await many(self.db.conn, 'SELECT server_id,info_json FROM server_nodes WHERE owner=?', (owner,))
        names = {s['id']: s.get('name', '') for s in await self.service.registry.all(owner)}
        profiles = await self.service._profiles(owner)
        out = []
        for row in rows:
            sid, stored = row['server_id'], json.loads(row['info_json'] or '{}')
            session = self.hub.sessions.get(sid)
            info = (session.info if session else None) or stored.get('info') or {}
            features = session.features if session else stored.get('features') or ()
            caps = (session.caps if session else None) or stored.get('caps') or {}
            prof = profiles.get(sid, {})
            out.append({'serverId': sid, 'name': (prof.get('alias') or '本机') if sid == LOCAL else names.get(sid) or sid,
                        'region': prof.get('region') or (stored.get('geo') or {}).get('country', ''), 'online': session is not None,
                        'platform': info.get('platform', ''), 'ddns': 'ddns' in features, 'exec': caps.get('exec') is True,
                        'v4': info.get('ipv4', ''), 'v6': info.get('ipv6', '')})
        out.sort(key=lambda n: (not n['online'], n['name'].lower()))
        return out

    async def overview(self, owner):
        accounts = await self._accounts(owner)
        records = [decode(r) for r in await many(self.db.conn, 'SELECT * FROM ddns_records WHERE owner=? ORDER BY created_ms', (owner,))]
        kinds = {a['id']: a['type'] for a in accounts}
        names = await self._secret_names([a['secret_id'] for a in accounts])
        issues = sum(1 for r in records if self._issue(r)) + sum(1 for a in accounts if a['state'] == 'error')
        return {'ok': True, 'accounts': [self._account_view(a, names, records) for a in accounts],
                'records': [self._view(r, kinds.get(r['account_id'], '')) for r in records],
                'providers': providers.catalog(), 'nodes': await self.nodes(owner), 'issues': issues}

    async def events(self, owner, rid, limit=50):
        await self._rec(owner, rid)
        limit = max(1, min(int(limit), 200))
        rows = await many(self.db.conn, 'SELECT * FROM ddns_events WHERE record_id=? AND owner=? ORDER BY id DESC LIMIT ?', (rid, owner, limit))
        return {'ok': True, 'events': [{'id': r['id'], 'at': r['at_ms'], 'kind': r['kind'], 'text': r['text'], 'detail': json.loads(r['detail_json'])}
                                       for r in rows]}

    # ----- accounts -----
    async def test_account(self, owner, data, *, tool=False):
        """tool: given through the agent tool, which may not choose the API endpoint."""
        if not isinstance(data, dict) or set(data) - {'type', 'credentials', 'secretRef', 'settings'}:
            fail('invalid_parameters')
        if tool:
            self._tool_settings(data)
        cls = self._cls(data.get('type'))
        if tool:
            self._tool_ref(cls, data)
        settings = model.clean_settings(data.get('settings'))
        creds = await self._input_creds(cls, data)
        zones, writable = await self._check('test', cls.type, creds, settings)
        return {'ok': True, 'zones': zones, 'writable': writable}

    async def create_account(self, owner, data, *, tool=False):
        if not isinstance(data, dict) or set(data) - {'type', 'alias', 'credentials', 'secretRef', 'settings'}:
            fail('invalid_parameters')
        if tool:
            self._tool_settings(data)
        cls = self._cls(data.get('type'))
        if tool:
            self._tool_ref(cls, data)
        alias = model.clean_alias(data.get('alias'), cls.name)
        settings = model.clean_settings(data.get('settings'))
        if data.get('credentials') is not None:
            values = model.clean_credentials(cls, data['credentials'])
            secret_id, mapping = await self._save_secret(cls, alias, values), {k: k for k in values}
        elif data.get('secretRef') is not None:
            secret_id, mapping, _ = await self._ref_creds(cls, data['secretRef'])
        else:
            fail('credentials_required')
        aid, now = 'a_' + secrets.token_hex(5), self.now()
        async with self.db.write_transaction(label='ddns-account-create') as conn:
            last = await (await conn.execute('SELECT COALESCE(MAX(sort),0) FROM ddns_accounts WHERE owner=?', (owner,))).fetchone()
            await conn.execute('INSERT INTO ddns_accounts(id,owner,type,alias,secret_id,field_map_json,settings_json,sort,created_ms,updated_ms) '
                               'VALUES(?,?,?,?,?,?,?,?,?,?)', (aid, owner, cls.type, alias, secret_id, dumps(mapping), dumps(settings), last[0] + 1, now, now))
        await self._refresh_zones(await self._account(owner, aid))
        return {'ok': True, 'account': await self.account_view(owner, aid)}

    async def update_account(self, owner, aid, data, *, tool=False):
        account = await self._account(owner, aid)
        if not isinstance(data, dict) or set(data) - {'alias', 'credentials', 'secretRef', 'settings'}:
            fail('invalid_parameters')
        if tool:
            self._tool_settings(data, account['settings'])
        cls = self._cls(account['type'])
        if tool:
            self._tool_ref(cls, data)
        fields = {}
        if 'alias' in data:
            fields['alias'] = model.clean_alias(data['alias'], account['alias'])
        if data.get('settings') is not None:
            fields['settings_json'] = dumps(model.clean_settings(data['settings'], account['settings']))
        if data.get('credentials') is not None:
            values = model.clean_credentials(cls, data['credentials'], partial=True)
            row, _ = await self._secret_kv(account['secret_id'])
            own = self._own_secret(row)
            # Through the agent tool not a secret another account also reads either: its values would be the AI's to choose.
            if own and tool and await self._shared(account['secret_id'], aid):
                own = False
            if not own:
                # A secret of the user's own is never written into: the account gets one of its own.
                with contextlib.suppress(DdnsError):
                    values = {**await self._creds(account), **values}
            secret_id = await self._save_secret(cls, fields.get('alias', account['alias']), values, account['secret_id'] if own else 0)
            fields.update(secret_id=secret_id, field_map_json=dumps({**{k: k for k in values}, **(account['field_map'] if secret_id == account['secret_id'] else {})}))
        elif data.get('secretRef') is not None:
            secret_id, mapping, _ = await self._ref_creds(cls, data['secretRef'])
            fields.update(secret_id=secret_id, field_map_json=dumps(mapping))
        if not fields:
            return {'ok': True, 'account': await self.account_view(owner, aid)}
        await self._set_account(aid, **fields, updated_ms=self.now())
        if set(fields) - {'alias'}:
            await self._refresh_zones(await self._account(owner, aid))
            await self._retry_failed(owner, aid)
        return {'ok': True, 'account': await self.account_view(owner, aid)}

    async def _retry_failed(self, owner, aid):
        """Credentials or settings changed: records stopped by a permanent error try again."""
        rows = await many(self.db.conn, "SELECT id FROM ddns_records WHERE owner=? AND account_id=? AND state='failed' AND enabled=1", (owner, aid))
        for row in rows:
            await self._set(row['id'], state='pending', next_ms=self.now())
            self.kick(row['id'], 'config')

    async def check_account(self, owner, aid, data=None):
        """data {settings}: its stored credentials tried with settings being edited, nothing stored."""
        account = await self._account(owner, aid)
        if data and data.get('settings') is not None:
            if set(data) - {'settings'}:
                fail('invalid_parameters')
            zones, writable = await self._check('test', account['type'], await self._creds(account), model.clean_settings(data['settings']))
            return {'ok': True, 'zones': zones, 'writable': writable}
        zones, writable = await self._refresh_zones(account, strict=True)
        return {'ok': True, 'zones': zones, 'writable': writable}

    async def delete_account(self, owner, aid, *, force=False):
        async with self._account_lock(aid), contextlib.AsyncExitStack() as held:
            await self._account(owner, aid)
            rows = await many(self.db.conn, 'SELECT id FROM ddns_records WHERE account_id=? AND owner=? ORDER BY id', (aid, owner))
            if rows and not force:
                fail('account_in_use', 409)
            # Creation takes the account lock; sync, edits and remote deletion take record locks.
            for row in rows:
                await held.enter_async_context(self._record_lock(row['id']))
            rows = [decode(r) for r in await many(self.db.conn, 'SELECT * FROM ddns_records WHERE account_id=? AND owner=?', (aid, owner))]
            servers = {s for r in rows for s in self._servers_of(r)}
            async with self.db.write_transaction(label='ddns-account-delete') as conn:
                for r in rows:
                    await conn.execute('DELETE FROM ddns_events WHERE record_id=?', (r['id'],))
                await conn.execute('DELETE FROM ddns_records WHERE account_id=? AND owner=?', (aid, owner))
                await conn.execute('DELETE FROM ddns_accounts WHERE id=? AND owner=?', (aid, owner))
            for r in rows:
                for cache in (self.problems, self.writes, self.held, self.again):
                    cache.pop(r['id'], None)
            self.gates.pop(aid, None)
        for sid in servers:
            await self.push(sid)
        return {'ok': True, 'deleted': len(rows)}

    # ----- choosing an account and zone for a name -----
    @staticmethod
    def _zone_of(account, name, hint=''):
        zones = account['zones']
        if zones:
            if hint and hint in zones and model.in_zone(name, hint):
                return hint
            matched = [z for z in zones if model.in_zone(name, z)]
            return max(matched, key=len) if matched else ''
        if hint and model.in_zone(name, hint):
            return hint
        return '.'.join(name.split('.')[-2:])

    def _choose(self, name, hint, accounts, wanted=None):
        """(account or None, zone, candidates): the account whose zones hold the name by the
        longest suffix; accounts that cannot list zones only when no other one matches."""
        if wanted:
            account = next((a for a in accounts if a['id'] == wanted), None)
            if account is None:
                return None, '', [], 'account_not_found'
            zone = self._zone_of(account, name, hint)
            return (account, zone, [account], '') if zone else (None, '', [account], 'zone_not_found')
        best, hits = 0, []
        for account in accounts:
            zone = self._zone_of(account, name, hint) if account['zones'] else ''
            if zone and len(zone) > best:
                best, hits = len(zone), [(account, zone)]
            elif zone and len(zone) == best:
                hits.append((account, zone))
        if not hits:
            hits = [(a, self._zone_of(a, name, hint)) for a in accounts if not a['zones']]
        if not hits:
            return None, hint, [], 'no_account'
        if len(hits) > 1:
            return None, hits[0][1], [a for a, _ in hits], 'account_ambiguous'
        return hits[0][0], hits[0][1], [hits[0][0]], ''

    # ----- nodes: probes, diagnosis -----
    def _servers_of(self, rec):
        if rec['source']['kind'] != 'node':
            return set()
        return {rec['source']['serverId']} | ({rec['backup']['serverId']} if rec['backup'] else set())

    async def _records_for(self, server_id):
        rows = await many(self.db.conn, 'SELECT * FROM ddns_records WHERE enabled=1')
        return [r for r in map(decode, rows) if server_id in self._servers_of(r)]

    async def _fits(self, rec):
        """Fail too_many_probes when the record would leave a node with more probes than it runs."""
        if model.method_of(rec['source'], rec['family']) in ('', 'conn'):
            return
        spec = model.probe_spec(rec['source'], rec['family'], rec['policy']['allowPrivate'])
        for sid in self._servers_of(rec):
            specs = [model.probe_spec(r['source'], r['family'], r['policy']['allowPrivate']) for r in await self._records_for(sid)
                     if r['id'] != rec.get('id') and model.method_of(r['source'], r['family']) != 'conn']
            if len(model.merge_probes(specs + [spec])) > model.MAX_PROBES:
                fail('too_many_probes', 409)

    async def _probes(self, server_id):
        specs = [model.probe_spec(r['source'], r['family'], r['policy']['allowPrivate']) for r in await self._records_for(server_id)
                 if model.method_of(r['source'], r['family']) != 'conn']
        return model.merge_probes(specs)

    @staticmethod
    def _ddns_ready(session):
        return session is not None and 'ddns' in session.features and not getattr(session, 'incompatible', '')

    async def push(self, server_id, *, force=False):
        """Send the node every probe its records need (ddns.watch replaces the whole list)."""
        session = self.hub.sessions.get(server_id)
        if not self._ddns_ready(session):
            return
        async with self.locks[server_id]:
            probes = await self._probes(server_id)
            if not force and self.sent.get(server_id) == probes:
                return
            self.offered[server_id] = {p['key'] for p in probes}
            try:
                reply = await self.hub.call(server_id, 'ddns.watch', {'probes': probes}, WATCH_TIMEOUT_S)
            except (LookupError, ConnectionError, TimeoutError):
                return
            finally:
                self.offered.pop(server_id, None)
            if reply.get('ok'):
                self.sent[server_id] = probes
                self.refused.pop(server_id, None)
                keys = {p['key'] for p in probes}
                store = self.obs.get(server_id, {})
                for key in [k for k in store if k not in keys]:
                    del store[key]
            else:
                # The node keeps the probes it ran before, if any; the new ones do not run.
                self.refused[server_id] = str(reply.get('error') or 'node_error')[:60]
                log.warning('node %s refused its ddns probes: %s', server_id, self.refused[server_id])

    async def _probe(self, owner, server_id, params):
        await self._own_node(owner, server_id)
        session = self.hub.sessions.get(server_id)
        if session is None:
            fail('node_offline', 409)
        if not self._ddns_ready(session):
            fail('node_outdated', 409)
        try:
            reply = await self.hub.call(server_id, 'ddns.probe', params, PROBE_TIMEOUT_S)
        except LookupError:
            fail('node_offline', 409)
        except (ConnectionError, TimeoutError):
            fail('node_connection_lost', 502)
        if not reply.get('ok'):
            error = str(reply.get('error') or 'node_error')[:60]
            fail('node_outdated' if error == 'unsupported_method' else error, 409)
        return reply.get('result') if isinstance(reply.get('result'), dict) else {}

    @staticmethod
    def _clean_results(result):
        results = [{'method': str(r.get('method') or '')[:10], 'value': str(r.get('value') or '')[:64], 'error': str(r.get('error') or '')[:40],
                    'detail': str(r.get('detail') or '')[:300]} for r in (result.get('results') or [])[:10] if isinstance(r, dict)]
        interfaces = []
        for i in (result.get('interfaces') or [])[:64]:
            if not isinstance(i, dict):
                continue
            v6 = [{'addr': str(a.get('addr') or '')[:64], 'temporary': a.get('temporary') is True, 'deprecated': a.get('deprecated') is True,
                   'scope': str(a.get('scope') or '')[:16]} for a in (i.get('v6') or [])[:32] if isinstance(a, dict)]
            interfaces.append({'name': str(i.get('name') or '')[:64], 'default': i.get('default') is True,
                               'v4': [str(a)[:64] for a in (i.get('v4') or [])[:32] if isinstance(a, str)], 'v6': v6})
        return results, interfaces

    async def diagnose(self, owner, server_id, family):
        if family not in ('v4', 'v6'):
            fail('invalid_family')
        if not isinstance(server_id, str) or not server_id:
            fail('server_required')
        base = {'family': family, 'stun': model.DEFAULT_STUN, 'iface': '', 'bindIface': '', 'v6Select': model.DEFAULT_V6_SELECT}
        if family == 'v6':
            results, interfaces = self._clean_results(await self._probe(owner, server_id, {**base, 'methods': ['url', 'nic', 'stun'],
                                                                                         'urls': model.DEFAULT_URLS[family]}))
        else:
            # Services in China and abroad apart: a proxy that routes the two ways differently
            # shows up as two answers.
            home, abroad = await asyncio.gather(
                self._probe(owner, server_id, {**base, 'methods': ['url', 'nic', 'stun'], 'urls': model.URL_GROUPS['cn'][family]}),
                self._probe(owner, server_id, {**base, 'methods': ['url'], 'urls': model.URL_GROUPS['intl'][family]}), return_exceptions=True)
            if isinstance(home, BaseException):
                raise home
            if isinstance(abroad, BaseException) and not isinstance(abroad, DdnsError):
                raise abroad
            results, interfaces = self._clean_results(home)
            results = [{**r, 'method': 'url_cn'} if r['method'] == 'url' else r for r in results]
            if not isinstance(abroad, BaseException):
                results[1:1] = [{**r, 'method': 'url_intl'} for r in self._clean_results(abroad)[0] if r['method'] == 'url']
        region = ((await self.service._profiles(owner)).get(server_id) or {}).get('region') or ''
        method, notes = model.recommend(family, results, interfaces, await self._mesh_seen(owner, server_id, family), region)
        results = [{**r, 'error': PROBE_ERRORS.get(r['error'], r['error']), 'errorCode': r['error']} for r in results]
        return {'ok': True, 'results': results, 'interfaces': interfaces, 'recommend': method, 'notes': notes}

    async def _mesh_seen(self, owner, server_id, family):
        """Public addresses mesh members holding a punched direct link to the node see it at:
        what reaches it from outside, past any proxy on its way out. Only members the applied
        configuration links to the node directly count: any other could claim anything."""
        mesh = self.service.mesh
        configs = (await mesh._applied(owner))['configs']

        def linked(a, b):
            return any(link.get('kind') == 'direct' and link.get('peer') == b for link in (configs.get(a) or {}).get('links') or [])
        now, seen = self.now(), set()
        for sid, live in (getattr(mesh, 'status', None) or {}).items():
            if sid == server_id or now - (live.get('at') or 0) > MESH_SEEN_S * 1000:
                continue
            if not (linked(sid, server_id) or linked(server_id, sid)):
                continue
            for peer in (live.get('status') or {}).get('peers') or []:
                if peer.get('id') != server_id or (peer.get('direct') or {}).get('state') != 'direct':
                    continue
                host = (peer.get('remoteAddr') or peer.get('endpoint') or '').rpartition(':')[0].strip('[]')
                address, _ = model.check_address(host, family)
                if address:
                    seen.add(address)
        return seen

    def _conn(self, session, family):
        if getattr(session, 'forwarded', False):
            return '', ('conn_unavailable', '节点经反向代理连到主控，拿不到它的真实来源地址')
        address, _ = model.check_address(session.remote, family)
        if not address:
            return '', ('conn_unavailable', f'节点连接主控的来源地址（{session.remote or "未知"}）不是公网 {"IPv4" if family == "v4" else "IPv6"} 地址')
        return address, None

    async def _detect(self, owner, source, family):
        """{value, method, auto, notes}: what the source sees now, for previews and new records."""
        if source['kind'] == 'fixed':
            ok = not model.check_address(source['fixed'], family, True)[1]
            return {'value': source['fixed'] if ok else '', 'method': 'fixed', 'auto': False, 'notes': []}
        key = (owner, dumps(source), family)
        cached = self.detected.get(key)
        if cached and self.now() - cached[0] < DETECT_CACHE_S * 1000:
            return cached[1]
        sid, method = source['serverId'], source['method']
        auto = method == 'auto'
        out = {'value': '', 'method': model.method_of(source, family), 'auto': auto, 'notes': []}
        session = self.hub.sessions.get(sid)
        if method == 'conn':
            if session is None:
                out['notes'].append({'level': 'warn', 'text': '节点离线，暂时检测不到地址'})
            else:
                out['value'], problem = self._conn(session, family)
                if problem:
                    out['notes'].append({'level': 'warn', 'text': problem[1]})
            return out
        try:
            if auto:
                diag = await self.diagnose(owner, sid, family)
                out['method'], out['notes'] = diag['recommend'], diag['notes']
                # url: both groups agree, either answer is it.
                wanted = ('url', 'url_intl', 'url_cn') if diag['recommend'] == 'url' else (diag['recommend'],)
                found = next((r for r in diag['results'] if r['method'] in wanted and r['value']), {})
                out['value'] = found.get('value', '')
            else:
                spec = model.probe_spec(source, family, False)
                seen = self.obs.get(sid, {}).get(spec['key'])
                if seen and seen['value'] and not seen['stale']:
                    out['value'] = seen['value']
                else:
                    result = await self._probe(owner, sid, {'family': family, 'methods': [spec['method']], 'urls': spec['urls'], 'stun': spec['stun'],
                                                            'iface': spec['iface'], 'bindIface': spec['bindIface'], 'v6Select': spec['v6Select'],
                                                            'cmd': spec['cmd'], 'allowPrivate': spec['allowPrivate']})
                    found = next((r for r in self._clean_results(result)[0] if r['method'] == spec['method']), {})
                    out['value'] = found.get('value', '')
                    if found.get('error'):
                        out['notes'].append({'level': 'warn', 'text': f'{METHOD_TEXT.get(method, method)}方式取 IP 失败：'
                                                                       f'{PROBE_ERRORS.get(found["error"], found["error"])}'})
        except DdnsError as exc:
            text = {'node_offline': '节点离线，暂时检测不到地址'}.get(exc.error, exc.message or exc.error)
            out['notes'].append({'level': 'warn', 'text': text})
            return out
        if out['value']:
            _, reason = model.check_address(out['value'], family)
            if reason:
                out['notes'].append({'level': 'warn', 'text': f'检测到的地址 {out["value"]} 是{model.REASONS[reason]}，默认不会写入（高级选项里可以允许内网地址）'})
        self.detected[key] = (self.now(), out)
        return out

    # ----- node callbacks -----
    def node_seen(self, server_id, connected):
        if not connected:
            self.left[server_id] = self.now()
            self.sent.pop(server_id, None)
            # What it saw before going away no longer says where it is: only a report after it is
            # back may switch a record back to it (the value is kept for a probe still failing then).
            for seen in self.obs.get(server_id, {}).values():
                seen['stale'] = True

    def node_hello(self, server_id):
        self._spawn(self._hello(server_id))

    async def _hello(self, server_id):
        """A node (re)connected: it gets its probes again (it keeps them only in memory), auto
        sources not yet decided are decided now, and its records are looked at again."""
        await self.push(server_id, force=True)
        changed = False
        for rec in await self._records_for(server_id):
            src = rec['source']
            if src['serverId'] == server_id and src['method'] == 'auto' and not src['resolvedMethod'] and self._ddns_ready(self.hub.sessions.get(server_id)):
                found = await self._detect(rec['owner'], src, rec['family'])
                if found['method'] in model.RESOLVED and found['value']:
                    rec['source'] = {**src, 'resolvedMethod': found['method']}
                    await self._set(rec['id'], source=rec['source'])
                    changed = True
            await self._evaluate(rec)
        if changed:
            await self.push(server_id)

    async def observe(self, server_id, items):
        """A node's {t: ddns} report: what its probes see. Kept in memory; a failing probe keeps
        the last address it saw, so DNS stays as it is."""
        if not isinstance(items, list):
            return
        # Only probes it was given: anything else would only grow the store.
        known = {p['key'] for p in self.sent.get(server_id, ())} | self.offered.get(server_id, set())
        store = self.obs.setdefault(server_id, {})
        failing = set()
        for item in items[:64]:
            if not isinstance(item, dict) or item.get('key') not in known:
                continue
            key = item['key']
            prior = store.get(key) or {}
            value, error = str(item.get('value') or '')[:64], str(item.get('error') or '')[:40]
            fails = item.get('fails')
            store[key] = {'value': value or prior.get('value', ''), 'error': error, 'detail': str(item.get('detail') or '')[:300],
                          'fails': fails if type(fails) is int and fails >= 0 else 0, 'at': self.now(),
                          'stale': not value and prior.get('stale', False)}
            if error and not prior.get('error'):
                failing.add(key)
        self._spawn(self._observed(server_id, failing))

    async def _observed(self, server_id, failing):
        for rec in await self._records_for(server_id):
            if rec['desired'] and model.method_of(rec['source'], rec['family']) != 'conn':
                key = model.probe_spec(rec['source'], rec['family'], rec['policy']['allowPrivate'])['key']
                seen = self.obs.get(server_id, {}).get(key)
                if key in failing and seen:
                    await self._event(rec['owner'], rec['id'], 'address', f'取 IP 失败（{PROBE_ERRORS.get(seen["error"], seen["error"])}），DNS 保持 {rec["desired"]}',
                                      {'error': seen['error'], 'detail': seen['detail']})
            await self._evaluate(rec)

    # ----- the desired value -----
    def _down_ms(self, server_id, now):
        if server_id in self.hub.sessions:
            return 0
        return max(0, now - max(self.left.get(server_id, 0), self.started_ms))

    def _read(self, rec, server_id, now):
        """(value, problem) the source on server_id gives now. (None, None): nothing new to go
        on (no report yet, or offline for less than OFFLINE_S)."""
        src, family = rec['source'], rec['family']
        if src['kind'] == 'fixed':
            return src['fixed'], None
        session = self.hub.sessions.get(server_id)
        if session is None:
            if self._down_ms(server_id, now) < OFFLINE_S * 1000:
                return None, None
            return None, ('offline', '来源节点离线，DNS 保持不变')
        method = model.method_of(src, family)
        if method == 'conn':
            return self._conn(session, family)
        if not self._ddns_ready(session):
            return None, ('node_outdated', model.MESSAGES['node_outdated'])
        key = model.probe_spec(src, family, rec['policy']['allowPrivate'])['key']
        if server_id in self.refused and key not in {p['key'] for p in self.sent.get(server_id, ())}:
            return None, ('probe_refused', f'节点拒绝了下发的取 IP 参数（{self.refused[server_id]}），这台节点上新的探测都没有运行；'
                                                 '检查这台节点上各条解析的取 IP 参数，或更新节点')
        seen = self.obs.get(server_id, {}).get(key)
        if not seen or seen['stale']:
            return None, None
        if seen['value']:
            return seen['value'], None
        return None, ('no_address', f'{METHOD_TEXT.get(method, method)}方式取不到地址（{PROBE_ERRORS.get(seen["error"], seen["error"] or "没有地址")}）')

    async def _evaluate(self, rec, now=None):
        """Work out the record's desired value from its source (switching to the backup source and
        back), and the state its source puts it in; a new desired value starts a sync."""
        now = now or self.now()
        rid, owner = rec['id'], rec['owner']
        if not rec['enabled']:
            self.problems.pop(rid, None)
            return
        src, backup, name = rec['source'], rec['backup'] if rec['source']['kind'] == 'node' else None, rec['name']
        fields, events = {}, []
        active = rec['active_source']
        value, problem = self._read(rec, src['serverId'], now)
        if backup:
            down = self._down_ms(src['serverId'], now)
            if active == 'backup':
                if down == 0 and value:
                    active = 'primary'
                    events.append(('failback', '主来源恢复，切回主来源', {'serverId': src['serverId']}))
                else:
                    value, problem = self._read(rec, backup['serverId'], now)
            elif down >= backup['afterS'] * 1000:
                other, _ = self._read(rec, backup['serverId'], now)
                if other:
                    active, value, problem = 'backup', other, None
                    events.append(('failover', f'主来源离线超过 {say(backup["afterS"])}，改用备用来源', {'serverId': backup['serverId']}))
        if active != rec['active_source']:
            fields['active_source'] = active
        if value:
            address, reason = model.check_address(value, rec['family'], rec['policy']['allowPrivate'])
            if not address:
                problem = ('no_address', f'取到的地址 {value} 是{model.REASONS[reason]}，按策略不写入')
            elif address != rec['desired']:
                # Always later than the change the last sync brought (its synced_ms), even within one ms.
                fields.update(desired=address, desired_ms=now, changed_ms=max(now, rec['changed_ms'] + 1))
                events.append(('address', f'地址变为 {address}' + (f'（原来是 {rec["desired"]}）' if rec['desired'] else ''),
                               {'value': address, 'previous': rec['desired'], 'source': active}))
        if problem:
            self.problems[rid] = problem
        else:
            self.problems.pop(rid, None)
        state = rec['state']
        if rid not in self.running:
            target = problem[0] if problem and state in SOFT_STATES else 'pending' if not problem and state in SOURCE_STATES else state
            if target != state:
                fields['state'] = target
                if target == 'offline':
                    events.append(('offline', '来源节点离线超过 2 分钟，DNS 保持不变', {}))
                if state == 'offline':
                    events.append(('online', '来源节点恢复在线', {}))
        if fields and not await self._set(rid, _enabled_only=True, **fields):
            return
        for kind, text, detail in events:
            await self._event(owner, rid, kind, text, {'name': name, **detail})
        if (fields.get('desired') or fields.get('state') == 'pending') and state != 'failed':
            self.kick(rid, 'address')

    # ----- syncing -----
    def kick(self, rid, reason):
        """Sync the record now, or once more after the sync under way."""
        if rid in self.running:
            prior = self.again.get(rid)
            order = ('check', 'address', 'config', 'manual')
            if prior is None or order.index(reason) > order.index(prior):
                self.again[rid] = reason
            return self.running[rid]
        task = self._spawn(self._run(rid, reason))
        self.running[rid] = task
        return task

    async def _run(self, rid, reason):
        try:
            while reason:
                try:
                    await self._sync(rid, reason)
                except Exception:  # noqa: BLE001 - the periodic check tries again
                    log.exception('ddns sync of %s failed', rid)
                reason = self.again.pop(rid, None)
        finally:
            self.running.pop(rid, None)

    async def _sync(self, rid, reason):
        async with self._record_lock(rid):
            await self._sync_locked(rid, reason)

    async def _sync_locked(self, rid, reason):
        row = await one(self.db.conn, 'SELECT * FROM ddns_records WHERE id=?', (rid,))
        if not row or not row['enabled']:
            return
        rec = decode(row)
        if rec['state'] == 'failed' and reason not in FORCED:
            return
        if not rec['desired']:
            await self._set(rid, next_ms=self.now() + CHECK_S * 1000)
            return
        account = await one(self.db.conn, 'SELECT * FROM ddns_accounts WHERE id=?', (rec['account_id'],))
        if not account:
            return
        account = decode_account(account)
        await self._set(rid, state='syncing')
        creds = {}
        try:
            creds = await self._creds(account)
            async with self._make(account['type'], creds, account['settings']) as p:
                async with self._zone_lock(account['id'], rec['zone'], p):
                    outcome = await (self._converge if p.caps.read else self._push)(rec, account, p, reason)
        except ProviderError as exc:
            await self._failed(rec, account, exc.code, scrub(exc.message, creds), exc.permanent, exc.retry_after)
        except DdnsError as exc:
            await self._failed(rec, account, exc.error, scrub(exc.message, creds), exc.error not in RETRYABLE)
        except Exception as exc:  # noqa: BLE001 - an adapter's own fault: retried with backoff, and alerted
            log.exception('ddns sync of %s failed', rid)
            await self._failed(rec, account, 'internal_error', scrub(f'{type(exc).__name__}: {exc}', creds), False)
        else:
            await self._done(rec, account, outcome)

    def _mine(self, rec, found, p, *, strict=False):
        """The records at the provider this one manages: all of the name's with multi=all, else the
        one taken over (by id). Without an id, by name and type: the one holding the value OpenBear
        wants or last saw there, else a lone record, unless the name has had several (strict, as for
        deleting: not even that). None: none is recognisably it, and none may be touched."""
        if rec['policy']['multi'] == 'all':
            return list(found)
        if rec['provider_record_id'] and p.caps.by_id:
            return [r for r in found if r.id == rec['provider_record_id']]
        known = [r for r in found if r.value == rec['desired']] or [r for r in found if rec['remote'] and r.value == rec['remote']]
        if known:
            return known[:1]
        # Once there were several, the one left may be someone else's after OpenBear's was deleted.
        if not found or len(found) == 1 and not strict and not rec['several_seen']:
            return list(found)
        return None

    def _throttled(self, rid, reason, now):
        """When the record may be written next, 0 for now: at most WRITE_MAX writes within
        WRITE_WINDOW_S, but for a write asked for (manual, config)."""
        times = [t for t in self.writes.get(rid, ()) if now - t < WRITE_WINDOW_S * 1000]
        self.writes[rid] = times
        if reason in FORCED or len(times) < WRITE_MAX:
            return 0
        return times[-WRITE_MAX] + WRITE_WINDOW_S * 1000

    async def _hold(self, rec, due, out):
        """A write put off by _throttled: the latest desired value is written at due."""
        rid = rec['id']
        if rid not in self.held:
            await self._event(rec['owner'], rid, 'hold', f'{say(WRITE_WINDOW_S)}内已写入 {WRITE_MAX} 次，地址还在变，'
                                                         f'{say(max(0, due - self.now()) // 1000)}后再写入最新的地址', {'value': rec['desired'], 'until': due})
        self.held[rid] = due
        return {**out, 'state': 'pending', 'next_ms': due}

    def _wrote(self, rid):
        self.writes.setdefault(rid, []).append(self.now())
        self.held.pop(rid, None)

    @staticmethod
    def _attrs_differ(r, rec, p):
        ttl = p.ttl_for(rec['ttl']) if rec['ttl'] else None
        if ttl and r.ttl and r.ttl != ttl:
            return True
        return any(k in r.extra and r.extra[k] != v for k, v in rec['options'].items())

    async def _converge(self, rec, account, p, reason):
        aid, zone, name, rtype, desired = account['id'], rec['zone'], rec['name'], rec['rtype'], rec['desired']
        # Records of the name on another line (电信, 联通...) are not this one's.
        found = p.on_line(await self._call(aid, p.records, zone, name, rtype), rec['options'])
        await self._saw(rec, found)
        mine = self._mine(rec, found, p)
        now = self.now()
        times = [t for t in rec['drift_times'] if now - t < FIGHT_WINDOW_S * 1000]
        if mine is None:
            await self._ambiguous(rec, found)
            return {'remote': rec['remote'], 'pid': rec['provider_record_id'], 'times': times, 'wrote': '', 'synced': False, 'read': True,
                    'state': 'ambiguous', 'next_ms': now + int(self.jitter(CHECK_S) * 1000)}
        current = mine[0].value if mine else ''
        out = {'remote': current, 'pid': rec['provider_record_id'], 'times': times, 'wrote': '', 'synced': False, 'read': True}
        # A difference OpenBear did not cause: the desired value has not changed since the
        # provider last held it.
        expected = not rec['synced_ms'] or rec['changed_ms'] > rec['synced_ms']
        if (not mine or any(r.value != desired for r in mine)) and not expected:
            times = out['times']
            if current != rec['remote']:
                times.append(now)
                await self._drifted(rec, current, times)
            if rec['policy']['onDrift'] == 'alert':
                return {**out, 'state': 'drift', 'next_ms': now + int(self.jitter(CHECK_S) * 1000)}
            if times:
                due = times[-1] + FIGHT_WAITS_S[min(len(times), len(FIGHT_WAITS_S)) - 1] * 1000
                if now < due:
                    return {**out, 'state': 'fighting' if len(times) >= 2 else 'drift', 'next_ms': min(due, now + int(self.jitter(CHECK_S) * 1000))}
        attrs = reason in FORCED
        stale = [r for r in mine if r.value != desired or attrs and self._attrs_differ(r, rec, p)]
        if not mine or stale:
            due = self._throttled(rec['id'], reason, now)
            if due:
                return await self._hold(rec, due, out)
        if not mine:
            created = await self._call(aid, p.create, zone, name, rtype, desired, rec['ttl'], rec['options'])
            self._wrote(rec['id'])
            out['pid'] = created.id if isinstance(created, Record) else created if isinstance(created, str) else ''
            out['wrote'] = 'recreated' if rec['synced_ms'] and not expected else 'created'
        for r in stale:
            await self._call(aid, p.update, zone, r, desired, rec['ttl'], rec['options'])
            self._wrote(rec['id'])
            out['wrote'] = 'updated' if expected else 'reverted'
        if out['wrote']:
            again = p.on_line(await self._call(aid, p.records, zone, name, rtype), rec['options'])
            await self._saw(rec, again)
            check = self._mine({**rec, 'provider_record_id': out['pid']}, again, p) or []
            if out['wrote'] in ('created', 'recreated') and not (out['pid'] and p.caps.by_id):
                check = [r for r in again if r.value == desired][:1] or check
            if not check or any(r.value != desired for r in check):
                raise DdnsError('verify_failed', message=f'写入后回读到的是 {check[0].value if check else "（没有记录）"}，不是 {desired}')
            if not out['pid'] and p.caps.by_id and check[0].id and rec['policy']['multi'] != 'all':
                out['pid'] = check[0].id
        out.update(remote=desired, synced=True, state='fighting' if len(out['times']) >= 2 else 'ok',
                   next_ms=self.now() + int(self.jitter(CHECK_S) * 1000))
        return out

    async def _saw(self, rec, found):
        if len(found) > 1 and not rec['several_seen']:
            await self._set(rec['id'], several_seen=1)
            rec['several_seen'] = 1

    async def _ambiguous(self, rec, found):
        """Several records of the name, or one left of several, and none holding a value OpenBear wrote
        or wants: nothing is written until one does (or multi=all); an event and an alert when it starts."""
        if rec['state'] == 'ambiguous':
            return
        rid, owner, now = rec['id'], rec['owner'], self.now()
        values = '、'.join(r.value for r in found[:5]) + ('…' if len(found) > 5 else '')
        if len(found) == 1:
            text = (f'服务商上只剩 1 条同名记录（{values}），不是 OpenBear 写入的值；这个名字原先有多条，'
                    'OpenBear 管理的那条可能已被删除，剩下这条可能是别人的，没有写入')
        else:
            text = f'服务商上有 {len(found)} 条同名记录（{values}），都不是 OpenBear 写入的值，认不出哪条归它管理，没有写入'
        await self._event(owner, rid, 'ambiguous', text, {'values': [r.value for r in found[:20]], 'count': len(found)})
        await self._alert(owner, f'ddns-ambiguous:{rid}:{now}', 'ddns_ambiguous',
                          f'{quoted(rec["name"])}（{rec["rtype"]}）{text}。' + (
                              f'这条确实归 OpenBear 管理的话，把它改成 {rec["desired"]}；不是的话，停止管理这条解析，或换一个名字。' if len(found) == 1 else
                              f'删掉多余的记录，或把其中一条改成 {rec["desired"]}；也可以在这条解析的高级选项里改成“全部改成同一个值”。'),
                          rec['source'].get('serverId', ''))

    async def _drifted(self, rec, current, times):
        """An external change seen for the first time: kept before any revert is tried, so a
        failing revert does not count it again; an event, and an alert at most hourly."""
        rid, owner, now = rec['id'], rec['owner'], self.now()
        await self._set(rid, remote=current, remote_ms=now, checked_ms=now, drift_times=times)
        gone = not current
        text = '服务商上的记录被删除了' if gone else f'服务商上的值被改成了 {current}（期望 {rec["desired"]}）'
        await self._event(owner, rid, 'drift', text, {'value': current, 'previous': rec['remote'], 'times': len(times)})
        rec['remote'] = current
        # The key carries this service's clock, which the hourly limit is measured on.
        sent = [int(r['id'].rsplit(':', 1)[1]) for r in await many(self.db.conn, 'SELECT id FROM server_notifications WHERE id LIKE ?', (f'ddns-drift:{rid}:%',))]
        if now - max(sent or [0]) < DRIFT_ALERT_S * 1000:
            return
        then = ('按设置只告警，不改回。' if rec['policy']['onDrift'] == 'alert' else
                '另一个系统也在写这条记录（比如还在运行的 ddns-go），OpenBear 会拉长间隔后改回，请停用另一个系统。' if len(times) >= 2 else
                '会重新创建。' if gone else '会改回期望值。')
        await self._alert(owner, f'ddns-drift:{rid}:{now}', 'ddns_recreated' if gone else 'ddns_drift', f'{quoted(rec["name"])}（{rec["rtype"]}）{text}。{then}',
                          rec['source'].get('serverId', ''))

    async def _push(self, rec, account, p, reason):
        """A provider that cannot read: write when the desired value changed, on request, and
        every REWRITE_S; there is no way to see an external change."""
        now = self.now()
        out = {'remote': rec['remote'], 'pid': rec['provider_record_id'], 'times': [], 'wrote': '', 'synced': False, 'read': False}
        # remote_ms: when it was last written (synced_ms is the change that write brought).
        if reason in FORCED or rec['remote'] != rec['desired'] or now - rec['remote_ms'] >= REWRITE_S * 1000:
            due = self._throttled(rec['id'], reason, now)
            if due:
                return await self._hold(rec, due, out)
            current = Record(rec['provider_record_id'], rec['name'], rec['rtype'], rec['remote'], rec['ttl'], dict(rec['options']))
            await self._call(account['id'], p.update, rec['zone'], current, rec['desired'], rec['ttl'], rec['options'])
            self._wrote(rec['id'])
            out.update(remote=rec['desired'], wrote='updated', synced=True)
            now = self.now()
        out.update(state='ok', next_ms=(now if out['synced'] else rec['remote_ms']) + REWRITE_S * 1000)
        return out

    async def _done(self, rec, account, out):
        rid, owner, now = rec['id'], rec['owner'], self.now()
        label = f'{quoted(rec["name"])}（{rec["rtype"]}）'
        state = out['state']
        problem = self.problems.get(rid)
        if problem and state == 'ok':
            state = problem[0]
        fields = {'remote': out['remote'], 'state': state, 'error': '', 'error_code': '', 'fail_count': 0, 'first_fail_ms': 0,
                  'next_ms': out['next_ms'], 'drift_times': out['times'], 'provider_record_id': out['pid'] or ''}
        if out['read'] or out['synced']:
            fields['remote_ms'] = now
        if out['read']:
            fields['checked_ms'] = now
        if out['synced']:
            # The change this sync started from: one coming while it ran is still to be written,
            # not taken for an outside change.
            fields['synced_ms'] = rec['changed_ms'] or now
        await self._set(rid, **fields)
        server = rec['source'].get('serverId', '')
        if out['wrote'] == 'created':
            await self._event(owner, rid, 'write', f'在服务商新建记录：{rec["desired"]}', {'value': rec['desired']})
        elif out['wrote'] == 'recreated':
            await self._event(owner, rid, 'recreated', f'记录被删除，已重新创建：{rec["desired"]}', {'value': rec['desired']})
        elif out['wrote'] == 'reverted':
            await self._event(owner, rid, 'write', f'已改回 {rec["desired"]}', {'value': rec['desired'], 'previous': rec['remote']})
        elif out['wrote']:
            await self._event(owner, rid, 'write', f'已写入 {rec["desired"]}' + (f'（原来是 {rec["remote"]}）' if rec['remote'] and rec['remote'] != rec['desired'] else ''),
                              {'value': rec['desired'], 'previous': rec['remote']})
        if rec['first_fail_ms']:
            await self._event(owner, rid, 'recover', '同步恢复正常', {'failures': rec['fail_count']})
            if await one(self.db.conn, 'SELECT 1 FROM server_notifications WHERE id=?', (f'ddns-fail:{rid}:{rec["first_fail_ms"]}',)):
                await self._alert(owner, f'ddns-recover:{rid}:{rec["first_fail_ms"]}', 'ddns_recover', f'{label} 已恢复正常，当前值 {rec["desired"]}。', server)
        if account['state'] != 'ok':
            await self._set_account(account['id'], state='ok', error='', error_code='')

    async def _failed(self, rec, account, code, message, permanent, retry_after=None):
        rid, owner, now = rec['id'], rec['owner'], self.now()
        count, first = rec['fail_count'] + 1, rec['first_fail_ms'] or now
        text = model.MESSAGES.get(code, code)
        if message and message != code and message != text:
            text = f'{text}：{message}' if not message.startswith(text) else message
        if permanent:
            state, next_ms, delay = 'failed', 0, 0
        else:
            delay = min(RETRY_MIN_S * 2 ** (count - 1), RETRY_MAX_S)
            if retry_after:
                delay = max(delay, min(int(retry_after), RETRY_MAX_S))
            state, next_ms = 'pending', now + delay * 1000
        await self._set(rid, state=state, error=text[:300], error_code=code, fail_count=count, first_fail_ms=first, next_ms=next_ms)
        if count == 1 or permanent and rec['state'] != 'failed':
            await self._event(owner, rid, 'fail', f'同步失败：{text}' + ('，不会自动重试，处理后手动同步' if permanent else f'，{say(delay)}后重试'),
                              {'code': code, 'count': count})
        if permanent:
            await self._set_account(account['id'], state='error', error=text[:300], error_code=code)
        # An internal error is OpenBear's own fault: told at once, though retried.
        if permanent or code == 'internal_error' or count >= FAIL_ALERT_COUNT or now - first >= FAIL_ALERT_S * 1000:
            label = f'{quoted(rec["name"])}（{rec["rtype"]}）'
            await self._alert(owner, f'ddns-fail:{rid}:{first}', 'ddns_fail',
                              f'{label} 同步失败：{text}。' + ('需要检查凭据或配置后手动同步。' if permanent else f'已连续失败 {count} 次，会继续自动重试。'),
                              rec['source'].get('serverId', ''))

    async def tick(self):
        """Look at every record's source, run the syncs that are due, refresh zone lists an hour
        old and drop old events."""
        now = self.now()
        for row in await many(self.db.conn, 'SELECT * FROM ddns_records WHERE enabled=1'):
            await self._evaluate(decode(row), now)
        for row in await many(self.db.conn, "SELECT id FROM ddns_records WHERE enabled=1 AND state!='failed' AND next_ms>0 AND next_ms<=?", (now,)):
            if row['id'] not in self.running:
                self.kick(row['id'], 'check')
        for row in await many(self.db.conn, 'SELECT * FROM ddns_accounts WHERE zones_ms<=?', (now - ZONES_S * 1000,)):
            account = decode_account(row)
            if await self._listable(account):
                self._spawn(self._refresh_zones(account))
            else:
                await self._set_account(account['id'], zones_ms=now)
        if now - self.pruned_ms >= 3600 * 1000:
            self.pruned_ms = now
            async with self.db.write_transaction(label='ddns-events-prune') as conn:
                await conn.execute('DELETE FROM ddns_events WHERE at_ms<?', (now - EVENTS_KEEP_S * 1000,))

    # ----- records -----
    def _source_input(self, data, prior=None):
        raw = data.get('source') if data.get('source') is not None else {}
        if not isinstance(raw, dict):
            fail('invalid_source')
        if raw.get('kind', 'node') == 'node' and data.get('serverId'):
            raw = {**raw, 'serverId': data['serverId']}
        return model.clean_source(raw)

    async def _check_servers(self, owner, source, backup):
        if source['kind'] == 'node':
            await self._own_node(owner, source['serverId'])
        if backup:
            if source['kind'] != 'node':
                fail('invalid_backup')
            await self._own_node(owner, backup['serverId'])

    async def preview(self, owner, data):
        if not isinstance(data, dict) or set(data) - {'serverId', 'names', 'types', 'source', 'accountIds', 'backup', 'policy', 'ttl', 'options'}:
            fail('invalid_parameters')
        names, types = data.get('names'), data.get('types') or ['A']
        if not isinstance(names, list) or not 0 < len(names) <= MAX_ROWS:
            fail('invalid_rows')
        if not isinstance(types, list) or not types or set(types) - set(TYPES):
            fail('invalid_type')
        wanted = data.get('accountIds') or {}
        if not isinstance(wanted, dict):
            fail('invalid_parameters')
        source = self._source_input(data)
        await self._check_servers(owner, source, None)
        accounts = await self._accounts(owner)
        detected = {'v4': None, 'v6': None}
        for rtype in ('A', 'AAAA'):
            if rtype in types:
                detected[TYPES[rtype]] = await self._detect(owner, source, TYPES[rtype])
        rows = []
        for raw in names:
            try:
                name, hint = model.clean_name(raw)
            except DdnsError as exc:
                rows += [self._row(str(raw), t, '', [], None, None, error=exc.error) for t in types]
                continue
            account, zone, candidates, error = self._choose(name, hint, accounts, wanted.get(raw) or wanted.get(name))
            for rtype in types:
                rows.append(await self._preview_row(owner, name, rtype, zone, candidates, account, detected[TYPES[rtype]], error, data.get('options')))
        return {'ok': True, 'detected': detected, 'rows': rows}

    @staticmethod
    def _row(name, rtype, zone, candidates, account, detected, *, error='', action='error'):
        return {'name': name, 'type': rtype, 'zone': zone, 'accounts': [{'id': a['id'], 'alias': a['alias'], 'type': a['type']} for a in candidates],
                'accountId': account['id'] if account else '', 'action': action if not error else 'error', 'remote': None, 'remoteCount': 0,
                'value': (detected or {}).get('value', ''), 'error': model.MESSAGES.get(error, error) if error else '', 'errorCode': error}

    async def _preview_row(self, owner, name, rtype, zone, candidates, account, detected, error, options=None):
        row = self._row(name, rtype, zone, candidates, account, detected, error=error, action='new')
        if error:
            return row
        if rtype not in providers.get(account['type']).caps.types:
            return {**row, 'action': 'error', 'error': model.MESSAGES['type_unsupported'], 'errorCode': 'type_unsupported'}
        if await one(self.db.conn, 'SELECT 1 FROM ddns_records WHERE owner=? AND account_id=? AND name=? AND rtype=?', (owner, account['id'], name, rtype)):
            return {**row, 'action': 'exists'}
        if not readable(account['type']):
            return row
        try:
            opts = model.clean_options(providers.get(account['type']), options, strict=False)
            found = await self._remote(account, zone, name, rtype, opts=opts)
        except DdnsError as exc:
            return {**row, 'action': 'error', 'error': exc.message, 'errorCode': exc.error}
        if not found:
            return row
        value = row['value']
        match = next((r for r in found if value and r.value == value), None)
        return {**row, 'action': 'same' if match else 'takeover', 'remote': (match or found[0]).value, 'remoteCount': len(found)}

    async def create_records(self, owner, data):
        if not isinstance(data, dict) or set(data) - {'serverId', 'source', 'backup', 'ttl', 'options', 'policy', 'rows'}:
            fail('invalid_parameters')
        source = self._source_input(data)
        backup = model.clean_backup(data.get('backup'), source['serverId'])
        await self._check_servers(owner, source, backup)
        policy, ttl = model.clean_policy(data.get('policy')), model.clean_ttl(data.get('ttl'))
        rows = data.get('rows')
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_ROWS or not all(isinstance(r, dict) for r in rows):
            fail('invalid_rows')
        if any(set(r) - {'name', 'type', 'accountId', 'confirmTakeover'} for r in rows):
            fail('invalid_rows')
        records, skipped = await self._add(owner, source, backup, ttl, data.get('options'), policy, rows)
        return {'ok': True, 'records': records, 'skipped': skipped}

    async def _add(self, owner, source, backup, ttl, options, policy, rows):
        """Save new records. One that would take over a record already at the provider is saved
        only with confirmTakeover, unless that record already holds the value it would get."""
        accounts = {a['id']: a for a in await self._accounts(owner)}
        detected, added, skipped = {}, [], []
        for row in rows:
            label = {'name': str(row.get('name') or ''), 'type': str(row.get('type') or '')}
            try:
                name, hint = model.clean_name(row.get('name'))
                hint = row.get('zone') or hint
                label['name'] = name
                rtype = row.get('type')
                if rtype not in TYPES:
                    fail('invalid_type')
                family = TYPES[rtype]
                model.check_fixed(source, family)
                account = accounts.get(row.get('accountId'))
                if account is None:
                    fail('account_not_found', 404)
                zone = self._zone_of(account, name, hint)
                if not zone:
                    fail('zone_not_found')
                if rtype not in providers.get(account['type']).caps.types:
                    fail('type_unsupported')
                opts = model.clean_options(providers.get(account['type']), row['options'] if 'options' in row else options, strict=False)
            except DdnsError as exc:
                skipped.append({**label, 'reason': 'error', 'error': exc.error, 'message': exc.message})
                continue
            if await one(self.db.conn, 'SELECT 1 FROM ddns_records WHERE owner=? AND account_id=? AND name=? AND rtype=?', (owner, account['id'], name, rtype)):
                skipped.append({**label, 'reason': 'exists'})
                continue
            if family not in detected:
                detected[family] = await self._detect(owner, source, family)
            value = detected[family]['value']
            pid, taken = str(row.get('recordId') or ''), None
            if readable(account['type']):
                try:
                    # Only a record on the line this one is for may be taken over.
                    found = await self._remote(account, zone, name, rtype, opts=opts)
                except DdnsError as exc:
                    skipped.append({**label, 'reason': 'error', 'error': exc.error, 'message': exc.message})
                    continue
                if found:
                    taken = next((r for r in found if pid and r.id == pid), None) or next((r for r in found if value and r.value == value), None) or found[0]
                    same = bool(value) and all(r.value == value for r in (found if policy['multi'] == 'all' else [taken]))
                    if not same and row.get('confirmTakeover') is not True:
                        skipped.append({**label, 'reason': 'takeover_not_confirmed', 'remote': taken.value})
                        continue
                    pid = taken.id
            stored = {**source, 'resolvedMethod': detected[family]['method'] if source['method'] == 'auto' and detected[family]['method'] in model.RESOLVED else ''}
            try:
                await self._fits({'source': stored, 'backup': backup, 'policy': policy, 'family': family})
            except DdnsError as exc:
                skipped.append({**label, 'reason': 'error', 'error': exc.error, 'message': exc.message})
                continue
            desired = model.check_address(value, family, policy['allowPrivate'])[0] if value else ''
            rid, now = 'r_' + secrets.token_hex(5), self.now()
            # A record taken over starts out as read: without an id it is told apart by that value.
            remote = taken.value if taken is not None else ''
            async with self._account_lock(account['id']), self.db.write_transaction(label='ddns-record-create') as conn:
                if not await one(conn, 'SELECT 1 FROM ddns_accounts WHERE id=? AND owner=?', (account['id'], owner)):
                    skipped.append({**label, 'reason': 'error', 'error': 'account_not_found', 'message': model.MESSAGES['account_not_found']})
                    continue
                if await one(conn, 'SELECT 1 FROM ddns_records WHERE owner=? AND account_id=? AND name=? AND rtype=?', (owner, account['id'], name, rtype)):
                    skipped.append({**label, 'reason': 'exists'})
                    continue
                await conn.execute('INSERT INTO ddns_records(id,owner,account_id,zone,name,rtype,source_json,backup_json,ttl,options_json,policy_json,'
                                   'provider_record_id,desired,desired_ms,remote,remote_ms,changed_ms,state,next_ms,several_seen,created_ms,updated_ms) '
                                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (rid, owner, account['id'], zone, name, rtype, dumps(stored), dumps(backup), ttl, dumps(opts), dumps(policy), pid,
                                    desired, now if desired else 0, remote, now if remote else 0, now if desired else 0, 'pending', now,
                                    int(len(found) > 1) if taken is not None else 0, now, now))
            source_text = METHOD_TEXT.get(model.method_of(stored, family) or 'fixed', '')
            await self._event(owner, rid, 'create', f'开始管理 {name} 的 {rtype} 记录（来源：{source_text}）', {'name': name})
            if taken is not None:
                await self._event(owner, rid, 'takeover', f'接管服务商上已有的记录，当前值 {taken.value}', {'id': taken.id, 'value': taken.value})
            added.append(rid)
        servers = {source['serverId']} | ({backup['serverId']} if backup else set()) if source['kind'] == 'node' else set()
        for sid in servers:
            await self.push(sid)
        out = []
        for rid in added:
            rec = await self._rec(owner, rid)
            await self._evaluate(rec)
            self.kick(rid, 'config')
            out.append(await self.record_view(owner, rid))
        return out, skipped

    async def update_record(self, owner, rid, data):
        async with self._record_lock(rid):
            return await self._update_record(owner, rid, data)

    async def _update_record(self, owner, rid, data):
        rec = await self._rec(owner, rid)
        if not isinstance(data, dict) or not data or set(data) - {'source', 'backup', 'ttl', 'options', 'policy', 'enabled'}:
            fail('invalid_record')
        account = await self._account(owner, rec['account_id'])
        changes, old = {}, self._servers_of(rec)
        source = rec['source']
        if 'source' in data:
            raw = data['source']
            if not isinstance(raw, dict):
                fail('invalid_source')
            prior = {'kind': source['kind'], 'serverId': source['serverId'], 'method': source['method'] or 'auto', 'params': source['params'],
                     **({'fixed': source['fixed']} if source['fixed'] else {})}
            merged = {k: v for k, v in {**prior, **raw}.items() if k not in ('methodAuto', 'resolvedMethod')}
            new = model.clean_source(merged)
            model.check_fixed(new, rec['family'])
            if new['kind'] == 'node':
                await self._own_node(owner, new['serverId'])
            if new['method'] == 'auto':
                same = source['method'] == 'auto' and source['serverId'] == new['serverId'] and source['params'] == new['params']
                given = raw.get('resolvedMethod')
                if given in model.RESOLVED:
                    new['resolvedMethod'] = given
                elif same:
                    new['resolvedMethod'] = source['resolvedMethod']
                else:
                    found = await self._detect(owner, new, rec['family'])
                    new['resolvedMethod'] = found['method'] if found['method'] in model.RESOLVED else ''
            changes['source'] = source = new
        if 'backup' in data or 'source' in changes:
            backup = model.clean_backup(data['backup'] if 'backup' in data else rec['backup'], source['serverId'])
            await self._check_servers(owner, source, backup)
            changes['backup'] = backup
        if 'ttl' in data:
            changes['ttl'] = model.clean_ttl(data['ttl'])
        if 'options' in data:
            changes['options'] = model.clean_options(providers.get(account['type']), data['options'])
            if changes['options'] != rec['options']:
                # Counted on the line the options choose.
                changes['several_seen'] = 0
        if 'policy' in data:
            changes['policy'] = model.clean_policy(data['policy'], rec['policy'])
        if 'enabled' in data:
            if not isinstance(data['enabled'], bool):
                fail('invalid_record')
            changes['enabled'] = int(data['enabled'])
        enabled = changes.get('enabled', rec['enabled'])
        if enabled:
            await self._fits({**rec, **changes})
        now = self.now()
        if 'options' in changes and self._line(account, changes['options']) != self._line(account, rec['options']):
            # Another line is another record at the provider: the old line's one this record managed goes,
            # and the new line's is written as a change of its settings, not taken for one deleted outside.
            await self._leave_line(owner, rec, account)
            changes.update(provider_record_id='', remote='', changed_ms=max(now, rec['changed_ms'] + 1))
        state = {'state': 'paused', 'next_ms': 0} if not enabled else {'state': 'pending', 'next_ms': now}
        if 'source' in changes and changes['source'] != rec['source']:
            state['active_source'] = 'primary'
        await self._set(rid, **changes, **state, updated_ms=now)
        if not enabled:
            self.problems.pop(rid, None)
        for sid in old | self._servers_of({**rec, **changes}):
            await self.push(sid)
        rec = await self._rec(owner, rid)
        if enabled:
            await self._evaluate(rec)
            self.kick(rid, 'config')
        return {'ok': True, 'record': await self.record_view(owner, rid)}

    @staticmethod
    def _line(account, options):
        field = next((f for f in providers.get(account['type']).options if f.key == 'line'), None)
        return (options or {}).get('line') or field.default if field else ''

    async def _leave_line(self, owner, rec, account):
        """Deletes the record this one managed on the line it leaves; when that fails or it cannot be
        told apart, it stays there, said in an event."""
        if not rec['synced_ms'] and not rec['provider_record_id']:
            return
        creds = await self._creds(account)
        try:
            async with self._make(account['type'], creds, account['settings']) as p:
                if not p.caps.delete or not p.caps.read:
                    return
                async with self._zone_lock(account['id'], rec['zone'], p):
                    found = p.on_line(await self._call(account['id'], p.records, rec['zone'], rec['name'], rec['rtype']), rec['options'])
                    targets = self._mine(rec, found, p, strict=True)
                    if targets is None:
                        await self._event(owner, rec['id'], 'line_left', '换了线路；原线路上的记录认不出哪条归它管理，没有删除，可以到服务商那里处理')
                        return
                    for target in targets:
                        await self._call(account['id'], p.delete, rec['zone'], target)
                    if targets:
                        await self._event(owner, rec['id'], 'line_left', f'换了线路，已删除原线路上的记录（{"、".join(t.value for t in targets)}）')
        except (ProviderError, DdnsError) as exc:
            await self._event(owner, rec['id'], 'line_left', f'换了线路；删除原线路上的记录失败，它还在：{scrub(getattr(exc, "message", ""), creds)}')

    async def delete_record(self, owner, rid, *, delete_remote=False):
        async with self._record_lock(rid):
            return await self._delete_record(owner, rid, delete_remote=delete_remote)

    async def _delete_record(self, owner, rid, *, delete_remote=False):
        rec = await self._rec(owner, rid)
        if delete_remote:
            account = await self._account(owner, rec['account_id'])
            creds = await self._creds(account)
            try:
                async with self._make(account['type'], creds, account['settings']) as p:
                    if not p.caps.delete:
                        fail('delete_unsupported', 409)
                    async with self._zone_lock(account['id'], rec['zone'], p):
                        if p.caps.read:
                            found = p.on_line(await self._call(account['id'], p.records, rec['zone'], rec['name'], rec['rtype']), rec['options'])
                            targets = self._mine(rec, found, p, strict=True)
                            if targets is None:
                                fail('record_ambiguous', 409, f'服务商上 {rec["name"]} 的 {rec["rtype"]} 记录（'
                                                              f'{"、".join(r.value for r in found[:5])}）都不是 OpenBear 写入的值，'
                                                              '认不出哪条归它管理，没有删除；可以只停止管理，再到服务商那里删除')
                        else:
                            targets = [Record(rec['provider_record_id'], rec['name'], rec['rtype'], rec['remote'], rec['ttl'], dict(rec['options']))]
                        for target in targets:
                            await self._call(account['id'], p.delete, rec['zone'], target)
            except ProviderError as exc:
                raise DdnsError(exc.code, 400, model.MESSAGES.get(exc.code, '') + '：' + scrub(exc.message, creds)) from None
        async with self.db.write_transaction(label='ddns-record-delete') as conn:
            await conn.execute('DELETE FROM ddns_records WHERE id=? AND owner=?', (rid, owner))
            await conn.execute('DELETE FROM ddns_events WHERE record_id=?', (rid,))
        self.problems.pop(rid, None)
        self.writes.pop(rid, None)
        self.held.pop(rid, None)
        for sid in self._servers_of(rec):
            await self.push(sid)
        return {'ok': True, 'deletedRemote': bool(delete_remote)}

    async def sync_now(self, owner, rid):
        """Sync now, a failed record included; answers once done, or after SYNC_WAIT_S."""
        rec = await self._rec(owner, rid)
        if rec['enabled']:
            await self._evaluate(rec)
            task = self.kick(rid, 'manual')
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), SYNC_WAIT_S)
        return {'ok': True, 'record': await self.record_view(owner, rid)}

    # ----- importing a ddns-go config -----
    async def _merge_targets(self, owner, specs):
        """Existing accounts holding the same credentials as an imported one."""
        out = {}
        accounts = await self._accounts(owner)
        for spec in specs:
            for account in accounts:
                if account['type'] != spec['type'] or not spec['credentials']:
                    continue
                with contextlib.suppress(DdnsError):
                    creds = await self._creds(account)
                    if all(creds.get(k) == v for k, v in spec['credentials'].items()):
                        out[spec['key']] = account
                        break
        return out

    async def import_preview(self, owner, data):
        if not isinstance(data, dict) or set(data) - {'yaml', 'serverId'}:
            fail('invalid_parameters')
        parsed = importer.parse(data.get('yaml'))
        sid = data.get('serverId') or ''
        if sid:
            await self._own_node(owner, sid)
        merge = await self._merge_targets(owner, parsed['accounts'])
        zones, records = {}, []
        for r in parsed['records']:
            spec = parsed['accounts'][r['account']]
            target = merge.get(spec['key'])
            account = target or {'id': 'import', 'type': spec['type'], 'zones': [], 'settings': model.clean_settings({'ttl': spec['ttl']})}
            creds = None if target else spec['credentials']
            if spec['key'] not in zones:
                zones[spec['key']] = target['zones'] if target else []
                if not target:
                    with contextlib.suppress(DdnsError):
                        zones[spec['key']] = (await self._check('import', spec['type'], creds, account['settings']))[0]
            account = {**account, 'zones': zones[spec['key']]}
            item = {'name': r['name'], 'type': r['type'], 'method': r['method'], 'action': 'new', 'remote': None, 'value': ''}
            if sid:
                with contextlib.suppress(DdnsError):
                    source = model.clean_source({'kind': 'node', 'serverId': sid, 'method': r['method'], 'params': r['params']})
                    item['value'] = (await self._detect(owner, source, TYPES[r['type']]))['value']
            zone = self._zone_of(account, r['name'], r['zone'])
            if target and await one(self.db.conn, 'SELECT 1 FROM ddns_records WHERE owner=? AND account_id=? AND name=? AND rtype=?',
                                    (owner, target['id'], r['name'], r['type'])):
                item['action'] = 'exists'
            elif not zone:
                item['action'] = 'error'
            elif readable(spec['type']):
                try:
                    found = await self._remote(account, zone, r['name'], r['type'], creds, r['options'])
                except DdnsError:
                    item['action'] = 'error'
                else:
                    if found:
                        match = next((x for x in found if item['value'] and x.value == item['value']), None)
                        item.update(action='same' if match else 'takeover', remote=(match or found[0]).value)
            records.append(item)
        return {'ok': True, 'accounts': [{'type': a['type'], 'alias': a['alias'], 'mergeInto': merge[a['key']]['id'] if a['key'] in merge else None}
                                         for a in parsed['accounts']],
                'records': records, 'unsupported': parsed['unsupported'], 'warnings': parsed['warnings']}

    async def import_apply(self, owner, data):
        if not isinstance(data, dict) or set(data) - {'yaml', 'serverId', 'confirmTakeover'}:
            fail('invalid_parameters')
        parsed = importer.parse(data.get('yaml'))
        sid = data.get('serverId')
        if not isinstance(sid, str) or not sid:
            fail('server_required')
        await self._own_node(owner, sid)
        merge = await self._merge_targets(owner, parsed['accounts'])
        ids, defaults, accounts, records, skipped = {}, {}, [], [], []
        for spec in parsed['accounts']:
            if spec['key'] in merge:
                ids[spec['key']] = merge[spec['key']]['id']
            else:
                try:
                    created = await self.create_account(owner, {'type': spec['type'], 'alias': spec['alias'], 'credentials': spec['credentials'],
                                                                'settings': {'ttl': spec['ttl']}})
                except DdnsError as exc:
                    skipped += [{'name': r['name'], 'type': r['type'], 'reason': 'error', 'error': exc.error, 'message': exc.message}
                                for r in parsed['records'] if r['account'] == spec['key']]
                    continue
                ids[spec['key']] = created['account']['id']
            account = await self.account_view(owner, ids[spec['key']])
            accounts.append(account)
            defaults[spec['key']] = account['settings'].get('ttl')
        for r in parsed['records']:
            if r['account'] not in ids:
                continue
            try:
                source = model.clean_source({'kind': 'node', 'serverId': sid, 'method': r['method'], 'params': r['params']})
            except DdnsError as exc:
                skipped.append({'name': r['name'], 'type': r['type'], 'reason': 'error', 'error': exc.error, 'message': exc.message})
                continue
            row = {'name': r['name'], 'type': r['type'], 'accountId': ids[r['account']], 'confirmTakeover': data.get('confirmTakeover') is True,
                   'zone': r['zone'], 'recordId': r['recordId'], 'options': r['options']}
            ttl = r['ttl'] if r['ttl'] != defaults[r['account']] else None
            added, missed = await self._add(owner, source, None, ttl, None, model.clean_policy(None), [row])
            records += added
            skipped += missed
        return {'ok': True, 'accounts': accounts, 'records': records, 'skipped': skipped}
