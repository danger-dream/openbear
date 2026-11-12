"""The full unlock check for the detail page: the benchmarks' Service Unlock program, run on a Linux
node whose commands are on, each time the cards' own checks (unlock.py) run and once by itself for a
server that has no result yet or a new address. The program's report is kept as it came, trimmed,
in server_unlock_full and folded into one line per service only when read, so a change to CORE or
NAMES needs no new run. A Service Unlock benchmark run replaces it too. The node record holds a
marker, unlockFull: {v, at, ips, error?, failedAt?}, which says when it is due; the server list
does not carry the report (GET /api/servers/{id}/unlock-full does)."""
from __future__ import annotations

import json
import logging
import shlex
import time
import zlib

from app.instances.contracts import InstanceError
from app.servers import bench_catalog as catalog
from app.servers import bench_packages as packages
from app.servers.checks import ServerChecks
from app.servers.unlock import addresses, region
from app.webhooks.repository import one

log = logging.getLogger(__name__)

# Bump when what a run asks the program for changes: servers checked by an older one run again.
VERSION = 1
TEST = 'unlock'
RUN_S = 300
NODE_AWAY = {'node_offline', 'node_connection_lost'}
BENCH_ACTIVE = ('queued', 'preparing', 'running', 'collecting')

# The program's own rows folded into one line per service: (id, name, group, the rows its verdict
# comes from, first that says something; rows its region may come from; rows that narrow it and
# the note each adds when it says no). Rows: r results, s services, a ai_endpoints, x extra_checks.
# The ids of the cards' nine are theirs, so the page can put the cards' verdicts in their place.
CORE = (
    ('youtube', 'YouTube', 'media', ('s:youtube',), (), (('s:youtube_premium', 'Premium 不可用'), ('r:youtube_premium_access', 'Premium 不可用'),
                                                         ('s:youtube_music', 'Music 不可用'))),
    ('netflix', 'Netflix', 'media', ('r:netflix_access',), ('s:netflix',), ()),
    ('prime', 'Prime Video', 'media', ('x:MediaUnlockTest_PrimeVideo', 's:prime'), ('s:prime',), ()),
    ('spotify', 'Spotify', 'media', ('s:spotify',), (), (('s:spotify_signup', '不能注册新账号'),)),
    ('tiktok', 'TikTok', 'media', ('r:tiktok_access',), ('s:tiktok',), ()),
    ('twitch', 'Twitch', 'media', ('s:twitch',), (), ()),
    ('deezer', 'Deezer', 'media', ('s:deezer',), (), ()),
    ('openai', 'OpenAI', 'ai', ('r:chatgpt_web',), ('s:chatgpt',), (('a:openai', 'API 拒绝该地区'),)),
    ('anthropic', 'Anthropic', 'ai', ('r:claude_access',), (), (('a:anthropic', 'API 拒绝该地区'),)),
    ('gemini', 'Gemini', 'ai', ('r:gemini_access',), (), (('a:gemini', 'API 拒绝该地区'),)),
    ('deepseek', 'DeepSeek', 'ai', ('a:deepseek',), (), ()),
    ('qwen_intl', '通义千问 国际版', 'ai', ('a:qwen_intl',), (), ()),
    ('qwen_cn', '通义千问 国内版', 'ai', ('a:qwen_cn',), (), ()),
    ('moonshot', 'Kimi', 'ai', ('a:moonshot',), (), ()),
    ('zhipu', '智谱', 'ai', ('a:zhipu',), (), ()),
    ('google', 'Google', 'web', ('s:google',), (), (('s:google_captcha', '搜索要过人机验证'),)),
    ('reddit', 'Reddit', 'web', ('s:reddit',), (), (('s:reddit_guest', '不登录不能浏览'),)),
    ('bing', 'Bing', 'web', ('s:bing',), (), ()),
    ('apple', 'Apple', 'web', ('s:apple',), (), ()),
    ('steam', 'Steam', 'web', ('s:steam',), (), ()),
    ('playstation', 'PlayStation', 'web', ('s:playstation',), (), ()),
    ('jetbrains', 'JetBrains', 'web', ('s:jetbrains',), (), ()),
    ('ookla', 'Speedtest', 'web', ('s:ookla',), (), ()),
)
# The program's names where they read badly; the rest are its own.
NAMES = {
    'MediaUnlockTest_Dazn': 'DAZN', 'MediaUnlockTest_BBCiPLAYER': 'BBC iPlayer', 'MediaUnlockTest_FOD': 'FOD',
    'MediaUnlockTest_4GTV': '4GTV', 'MediaUnlockTest_LineTVTW': 'LINE TV', 'MediaUnlockTest_BilibiliTW': 'Bilibili 台湾',
    'MediaUnlockTest_BilibiliHKMCTW': 'Bilibili 港澳台', 'MediaUnlockTest_BGlobalSEA': 'Bilibili 东南亚', 'MediaUnlockTest_BGlobalTH': 'Bilibili 泰国',
    'MediaUnlockTest_BGlobalID': 'Bilibili 印尼', 'MediaUnlockTest_BGlobalVN': 'Bilibili 越南', 'MediaUnlockTest_ITVHUB': 'ITVX',
    'MediaUnlockTest_HotStar': 'Hotstar', 'MediaUnlockTest_SonyLiv': 'Sony LIV', 'MediaUnlockTest_Directv': 'DIRECTV Stream',
    'WebTest_Wikipedia_Editable': 'Wikipedia 编辑', 'GameTest_MathsSpot': 'MathsSpot（Roblox）', 'GameTest_SDGGGE': 'SD 高达 G 世纪 永恒',
}
# The program's words for a reduced offering, where they say what is missing.
DETAILS = {'originals only; licensed titles are withheld': '仅自制剧', 'Oversea Only': '仅海外版', 'Available Soon': '即将上线'}
STATES = {'available': 'ok', 'restricted': 'limited', 'blocked': 'blocked', 'reachable': 'ok'}


def now_ms():
    return int(time.time() * 1000)


def trim(report):
    """What is kept of the program's --json report, or None when it is not one."""
    if not isinstance(report, dict) or report.get('schema_version') != 1 or not isinstance(report.get('results'), list):
        return None

    def text(value):
        return value if isinstance(value, str) else None

    def rows(key, fields):
        values = report.get(key) if isinstance(report.get(key), list) else []
        return [{f: text(r.get(f)) for f in fields} for r in values if isinstance(r, dict) and isinstance(r.get('id'), str)]
    raw = report.get('selection') if isinstance(report.get('selection'), dict) else {}
    selection = {k: text(raw.get(k)) for k in ('region', 'detected_country')}
    selection['extra_family'] = raw.get('extra_family') if raw.get('extra_family') in (4, 6) else None
    return {'results': rows('results', ('id', 'status', 'region', 'detail')), 'services': rows('services', ('id', 'type', 'status', 'value')),
            'ai': rows('ai_endpoints', ('id', 'status')), 'extra': rows('extra_checks', ('id', 'name', 'status', 'region', 'detail')),
            'selection': {k: selection.get(k) for k in ('region', 'detected_country', 'extra_family')}}


def _verdict(kind, row):
    """(state, region) one of the program's rows says: ok, limited, blocked or error."""
    if kind == 's':
        value = str(row.get('value') or '')
        if row.get('status') != 'observed':
            return 'error', ''
        if row.get('type') == 'country':
            return ('ok', region(value)) if region(value) else ('error', '')
        if row.get('type') == 'availability':
            return {'yes': 'ok', 'no': 'blocked'}.get(value, 'error'), ''
        # blocked: yes is being turned away (a captcha, a wall).
        if row.get('type') == 'blocked':
            return {'no': 'ok', 'yes': 'blocked'}.get(value, 'error'), ''
        return 'error', ''
    return STATES.get(row.get('status'), 'error'), region(row.get('region')) if kind in 'rx' else ''


def _group(extra_id):
    return 'ai' if extra_id == 'WebTest_MetaAI' else 'web' if extra_id.startswith(('GameTest_', 'WebTest_')) else 'media'


def fold(report):
    """{region, country, family, items: [{id, name, group, state, region, notes}]}: the services in
    CORE's order, then the program's further ones in its own."""
    rows = {f'{k}:{r["id"]}': r for k, key in (('r', 'results'), ('s', 'services'), ('a', 'ai'), ('x', 'extra')) for r in report.get(key) or []}
    items, used = [], set()
    for sid, name, group, sources, regions, parts in CORE:
        present = [k for k in sources if k in rows]
        if not present:
            continue
        used.update(present)
        said = [(k, _verdict(k[0], rows[k])) for k in present]
        source, (state, where) = next(((k, v) for k, v in said if v[0] != 'error'), said[0])
        where = where or next((w for k in regions if k in rows for w in [_verdict(k[0], rows[k])[1]] if w), '')
        notes = [DETAILS[rows[source].get('detail')]] if rows[source].get('detail') in DETAILS else []
        for part, missing in parts:
            if part in rows and _verdict(part[0], rows[part])[0] == 'blocked' and missing not in notes:
                notes.append(missing)
        items.append({'id': sid, 'name': name, 'group': group, 'state': state, 'region': where, 'notes': notes})
    for row in report.get('extra') or []:
        if f'x:{row["id"]}' in used:
            continue
        state, where = _verdict('x', row)
        items.append({'id': row['id'], 'name': NAMES.get(row['id']) or str(row.get('name') or row['id']), 'group': _group(row['id']),
                      'state': state, 'region': where, 'notes': [DETAILS[row['detail']]] if row.get('detail') in DETAILS else []})
    selection = report.get('selection') or {}
    return {'region': str(selection.get('region') or ''), 'country': region(selection.get('detected_country')),
            'family': selection.get('extra_family') if selection.get('extra_family') in (4, 6) else None, 'items': items}


def unable(info, caps):
    """Why the program cannot run on this node: exec (its commands are off), platform (not Linux), or ''."""
    if (caps or {}).get('exec') is False:
        return 'exec'
    platform = f'{(info or {}).get("platform") or "linux"}/{(info or {}).get("arch") or ""}'
    tool = catalog.TESTS[TEST]['package']
    return '' if platform in packages.manifest(tool, catalog.PACKAGES[tool]).get('platforms', []) else 'platform'


class FullUnlock(ServerChecks):
    KEY = 'unlockFull'
    SWITCH = 'unlock'

    def __init__(self, service):
        super().__init__(service)
        self.deferred = set()

    def due(self, server_id, stored, info, zone=None):
        # When it is time again the cards' checks say so (UnlockChecks.check starts this one).
        last = stored.get(self.KEY) or {}
        return server_id in self.deferred or last.get('v') != VERSION or last.get('ips') != addresses(info)

    def capable(self, server_id):
        session = self.service.hub.sessions.get(server_id)
        return bool(session) and not unable(session.info, session.caps)

    async def check(self, server_id):
        """Run the program now; the folded result, or None while a benchmark runs there (one at a time,
        so neither disturbs the other's measurements; it stays due and is tried again)."""
        lock = self.service.bench.measurement_lock(server_id)
        if lock.locked():
            self.deferred.add(server_id)
            return None
        async with lock:
            self.deferred.discard(server_id)
            return await self._check_locked(server_id)

    async def _check_locked(self, server_id):
        row = await one(self.db.conn, 'SELECT owner, info_json FROM server_nodes WHERE server_id=?', (server_id,))
        if not row:
            raise InstanceError('node_not_enrolled', 404)
        session = self.service.hub.sessions.get(server_id)
        if not session:
            raise InstanceError('node_offline', 409)
        why = unable(session.info, session.caps)
        if why:
            raise InstanceError('bench_exec_disabled' if why == 'exec' else 'bench_platform_unsupported', 409)
        if await one(self.db.conn, f'SELECT 1 FROM server_bench_runs WHERE server_id=? AND state IN {BENCH_ACTIVE}', (server_id,)):
            self.deferred.add(server_id)
            return None
        ips = addresses(json.loads(row['info_json'] or '{}').get('info') or session.info or {})
        try:
            report = await self._measure(row['owner'], server_id, f'linux/{(session.info or {}).get("arch") or ""}')
        except InstanceError as exc:
            if exc.error in NODE_AWAY:
                raise
            await self._failed(server_id, ips, exc.error)
            raise
        except packages.PackageError as exc:
            await self._failed(server_id, ips, exc.code)
            raise InstanceError(exc.code, 502) from None
        at = now_ms()
        await self._keep(server_id, row['owner'], at, 'auto', report)
        await self._save(server_id, {'v': VERSION, 'at': at, 'ips': ips})
        return fold(report)

    async def _measure(self, owner, server_id, platform):
        """The trimmed report of one run; the package is pushed first unless the node has this one."""
        try:
            result = await self._program(owner, server_id, platform)
        finally:
            session = self.service.hub.sessions.get(server_id)
            if session and (session.info or {}).get('openwrt'):
                # Off the RAM disk again; it runs only when the addresses change.
                try:
                    await self.service.exec(owner, server_id, f'rm -rf {shlex.quote(catalog.package_dir(TEST))}', timeout=30, shell='sh',
                                            source='check', log='解锁检测：清理 Service Unlock')
                except InstanceError:
                    pass
        if result.get('error'):
            raise InstanceError(result['error'], 409)
        if result.get('timedOut'):
            raise InstanceError('unlock_full_timeout', 504)
        # 1: a full report with at least one service it could not tell.
        if result.get('exitCode') not in (0, 1):
            raise InstanceError('unlock_full_failed', 502)
        text = str(result.get('stdout') or '')
        try:
            report = json.JSONDecoder().raw_decode(text[text.find('{'):])[0] if '{' in text else None
        except ValueError:
            report = None
        kept = trim(report)
        if not kept:
            raise InstanceError('unlock_full_bad_report', 502)
        return kept

    async def _program(self, owner, server_id, platform):
        stub = {'owner': owner, 'server_id': server_id, 'test': TEST, 'options_json': '{}'}
        for attempt in (0, 1):
            await self.service.bench._push(stub, platform)
            result = await self.service.exec(owner, server_id, catalog.unlock_command(), timeout=RUN_S, shell='sh', source='check',
                                             log='解锁检测：运行 Service Unlock', bypass=True)
            if result.get('exitCode') == catalog.CHECKSUM_FAILED and not attempt:
                # The node's copy was changed or cleaned away: its file list goes, so the push sends it again.
                await self.service.exec(owner, server_id, f'rm -f {shlex.quote(catalog.package_dir(TEST) + "/" + catalog.LISTING)}', timeout=30, shell='sh',
                                        source='check', log='解锁检测：重新推送 Service Unlock')
                continue
            break
        return result

    async def _failed(self, server_id, ips, error):
        stored = await self._stored(server_id) or {}
        last = stored.get(self.KEY) or {}
        await self._save(server_id, {**last, 'v': VERSION, 'ips': ips, 'error': error[:60], 'failedAt': now_ms()})

    async def _keep(self, server_id, owner, at, source, report):
        async with self.db.write_transaction(label='unlock-full') as conn:
            await conn.execute('INSERT INTO server_unlock_full(server_id,owner,at_ms,source,report) VALUES(?,?,?,?,?) '
                               'ON CONFLICT(server_id) DO UPDATE SET owner=excluded.owner, at_ms=excluded.at_ms, source=excluded.source, report=excluded.report',
                               (server_id, owner, at, source, zlib.compress(json.dumps(report, ensure_ascii=False).encode(), 6)))

    async def adopt(self, owner, server_id, report, at):
        """A Service Unlock benchmark's report, when it is newer than the one kept."""
        kept = trim(report)
        if not kept:
            return
        held = await one(self.db.conn, 'SELECT at_ms FROM server_unlock_full WHERE server_id=?', (server_id,))
        if held and held['at_ms'] >= at:
            return
        stored = await self._stored(server_id) or {}
        await self._keep(server_id, owner, at, 'bench', kept)
        await self._save(server_id, {'v': VERSION, 'at': at, 'ips': addresses(stored.get('info') or {})})
        self.deferred.discard(server_id)

    async def view(self, owner, server_id):
        """The detail page's: the folded result, whether a run is under way, why it cannot run, and the last failure."""
        await self.service.registry.get(owner, server_id)
        stored = await self._stored(server_id)
        if stored is None:
            raise InstanceError('node_not_enrolled', 404)
        if not await self.enabled() or await self.domestic(server_id):
            return {'ok': True, 'off': True, 'full': None}
        session = self.service.hub.sessions.get(server_id)
        info, caps = (session.info, session.caps) if session else (stored.get('info') or {}, stored.get('caps'))
        held = await one(self.db.conn, 'SELECT at_ms, source, report FROM server_unlock_full WHERE server_id=? AND owner=?', (server_id, owner))
        marker = stored.get(self.KEY) or {}
        failed = marker.get('failedAt') and marker['failedAt'] > (held['at_ms'] if held else 0)
        # deferred: put off until the server's benchmarks are done (the page keeps following it meanwhile).
        return {'ok': True, 'off': False, 'running': server_id in self.running, 'deferred': server_id in self.deferred, 'unable': unable(info, caps),
                'full': {'at': held['at_ms'], 'source': held['source'], **fold(json.loads(zlib.decompress(held['report'])))} if held else None,
                'error': marker.get('error') if failed else None, 'failedAt': marker['failedAt'] if failed else None}
