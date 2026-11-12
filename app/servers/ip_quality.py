"""IP 质量 on the detail page: the latest IP.Check.Place run of a server (评测), summed up — where the
address is, what kind of address the databases take it for, each database's risk score on one scale,
which risk factors how many of them flag, the platforms it unlocks, and our own 纯净分 and 场景评估 from
those — and what changed since the run before."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import zlib

from app.instances.contracts import InstanceError
from app.servers.checks import ServerChecks
from app.webhooks.repository import many, one

log = logging.getLogger(__name__)

TEST = 'ipcheck'
# A run in one of these is still to finish.
UNDER_WAY = ('queued', 'preparing', 'starting', 'running', 'collecting')
# How often a server is checked again by itself, in days, as the settings offer it.
EVERY_DAYS = (1, 7, 15, 30)
# Risk on 0-100 per database; up to LOW is 低, up to MID 中, above 高.
LOW, MID = 25, 60
FACTORS = (('Proxy', '代理'), ('VPN', 'VPN'), ('Tor', 'Tor'), ('Server', '机房'), ('Abuser', '滥用'), ('Robot', '机器人'))
MEDIA = (('Netflix', 'Netflix'), ('DisneyPlus', 'Disney+'), ('Youtube', 'YouTube'), ('AmazonPrimeVideo', 'Prime Video'),
         ('TikTok', 'TikTok'), ('Reddit', 'Reddit'), ('ChatGPT', 'ChatGPT'))
# 纯净分, 0-100: 100 less what the report holds against the address. The databases' risk scores, the worst
# weighing most (half the worst and a fifth of their mean); each risk factor by the share of the databases that
# flag it, times its weight; 2 a mail blacklist listing it (at most 10) and 0.2 one marking it (at most 5); 3
# for an address not native to where it is. Named by the bands below.
VOTE_WEIGHTS = {'Proxy': 25, 'VPN': 20, 'Tor': 30, 'Abuser': 25, 'Robot': 10, 'Server': 8}
BANDS = ((90, '极度纯净'), (75, '纯净'), (60, '一般'), (40, '存疑'), (0, '高危'))
# 场景评估, 0-10: the purity over ten, 1 more for an address most databases call residential (家宽, 移动) and
# 1 less for a data centre's; 社媒运营 a further 3 times the share calling it a proxy or VPN; at most 3 (AI 2)
# when its platform's check did not unlock.
SCENES = (('tiktok', 'TikTok', 'TikTok'), ('social', '社媒运营', 'Reddit'), ('ai', 'AI 应用', 'ChatGPT'))
RESIDENTIAL = ('家宽', '移动')
# A terminal colour code left in a value, whole (ESC [31m) or without its bracket (ESC 31m, as in Youtube's Region).
ANSI = re.compile(r'\x1b(?:\[[0-9;?]*[A-Za-z]|[0-9;]*[A-Za-z]?)')


def _number(value):
    """'12', '0.35%', 12.5 -> a score on 0-100; 'null', '', None and anything else -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(%?)\s*', str(value))
    return float(match.group(1)) if match else None


def level(score):
    return None if score is None else 'low' if score <= LOW else 'mid' if score <= MID else 'high'


def _text(value, limit=80):
    """A value as text, without colour codes; '' for what the script could not find, which it writes as 'null'."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    text = ANSI.sub('', str(value)).strip()
    return '' if text.lower() == 'null' else text[:limit]


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if -180 <= number <= 180 else None


def summarize(report):
    """One IP.Check.Place JSON report as the card shows it; None when it is not one."""
    if not isinstance(report, dict) or not isinstance(report.get('Info'), dict):
        return None
    info = report['Info']
    city, region = info.get('City') if isinstance(info.get('City'), dict) else {}, info.get('Region') if isinstance(info.get('Region'), dict) else {}
    registered = info.get('RegisteredRegion') if isinstance(info.get('RegisteredRegion'), dict) else {}
    lat, lon = _float(info.get('Latitude')), _float(info.get('Longitude'))
    kinds = report.get('Type') if isinstance(report.get('Type'), dict) else {}
    usage = {_text(k, 30): _text(v, 20) for k, v in (kinds.get('Usage') or {}).items() if _text(v)} if isinstance(kinds.get('Usage'), dict) else {}
    company = {_text(k, 30): _text(v, 20) for k, v in (kinds.get('Company') or {}).items() if _text(v)} if isinstance(kinds.get('Company'), dict) else {}
    scores = []
    for name, raw in (report.get('Score') or {}).items() if isinstance(report.get('Score'), dict) else ():
        value = _number(raw)
        scores.append({'source': _text(name, 30), 'value': None if value is None else min(round(value, 2), 100), 'level': level(value)})
    known = [s['value'] for s in scores if s['value'] is not None]
    factors = []
    raw_factors = report.get('Factor') if isinstance(report.get('Factor'), dict) else {}
    for key, name in FACTORS:
        votes = raw_factors.get(key) if isinstance(raw_factors.get(key), dict) else {}
        flagged = sorted(_text(k, 30) for k, v in votes.items() if v is True)
        factors.append({'key': key, 'name': name, 'flagged': len(flagged), 'total': sum(1 for v in votes.values() if isinstance(v, bool)), 'by': flagged})
    media = []
    raw_media = report.get('Media') if isinstance(report.get('Media'), dict) else {}
    for key, name in MEDIA:
        entry = raw_media.get(key) if isinstance(raw_media.get(key), dict) else None
        if entry:
            media.append({'key': key, 'name': name, 'status': _text(entry.get('Status'), 20), 'region': _text(entry.get('Region'), 8), 'type': _text(entry.get('Type'), 20)})
    mail = report.get('Mail') if isinstance(report.get('Mail'), dict) else {}
    blacklist = mail.get('DNSBlacklist') if isinstance(mail.get('DNSBlacklist'), dict) else {}
    summary = {
        'location': {'lat': lat if lat is not None and -90 <= lat <= 90 else None, 'lon': lon, 'city': _text(city.get('Name'), 60),
                     'subdivision': _text(city.get('Subdivisions'), 60), 'country': _text(region.get('Code'), 4).upper(), 'countryName': _text(region.get('Name'), 40),
                     'registered': _text(registered.get('Code'), 4).upper(), 'registeredName': _text(registered.get('Name'), 40), 'timezone': _text(info.get('TimeZone'), 40)},
        'asn': _text(info.get('ASN'), 12), 'org': _text(info.get('Organization'), 120), 'kind': _text(info.get('Type'), 20),
        'usage': usage, 'company': company, 'scores': scores,
        'risk': {'value': max(known) if known else None, 'level': level(max(known)) if known else None, 'sources': len(known)},
        'factors': factors, 'media': media,
        'mail': {'port25': mail.get('Port25') is True, 'listed': int(blacklist.get('Blacklisted') or 0), 'marked': int(blacklist.get('Marked') or 0),
                 'checked': int(blacklist.get('Total') or 0)},
    }
    summary['purity'] = purity(summary)
    summary['scenes'] = scenes(summary, summary['purity']['value'])
    return summary


def purity(summary):
    known = [s['value'] for s in summary['scores'] if s['value'] is not None]
    # No database scored it or voted on any factor (their lookups failed or were refused): nothing to rate, not 100.
    if not known and not any(f['total'] for f in summary['factors']):
        return {'value': None, 'label': '无法评估'}
    off = 0.5 * max(known) + 0.2 * sum(known) / len(known) if known else 0
    off += sum(VOTE_WEIGHTS[f['key']] * f['flagged'] / f['total'] for f in summary['factors'] if f['total'])
    off += min(summary['mail']['listed'] * 2, 10) + min(summary['mail']['marked'] * 0.2, 5)
    off += 3 if summary['kind'] and '原生' not in summary['kind'] else 0
    value = max(0, min(100, round(100 - off)))
    return {'value': value, 'label': next(name for floor, name in BANDS if value >= floor)}


def scenes(summary, score):
    if score is None:
        return []
    usage = list(summary['usage'].values())
    common = max(set(usage), key=usage.count) if usage else ''
    lean = 1 if common in RESIDENTIAL else -1 if common == '机房' else 0
    shares = {f['key']: f['flagged'] / f['total'] for f in summary['factors'] if f['total']}
    unlocked = {m['key']: m['status'] for m in summary['media']}
    out = []
    for key, name, platform in SCENES:
        value = score / 10 + lean - (3 * max(shares.get('Proxy', 0), shares.get('VPN', 0)) if key == 'social' else 0)
        status = unlocked.get(platform, '')
        if status and status != '解锁':
            value = min(value, 2 if key == 'ai' else 3)
        value = max(0, min(10, round(value)))
        out.append({'key': key, 'name': name, 'value': value, 'label': '极佳' if value >= 9 else '良好' if value >= 7 else '一般' if value >= 5 else '较差',
                    'platform': platform, 'status': status})
    return out


def changes(now, before):
    """What moved between two summaries: the overall risk, factors flagged by more or fewer, platforms whose status changed."""
    if not now or not before:
        return []
    out = []
    if now['purity']['value'] != before['purity']['value'] and None not in (now['purity']['value'], before['purity']['value']):
        out.append({'kind': 'purity', 'name': '纯净分', 'from': before['purity']['value'], 'to': now['purity']['value']})
    if now['risk']['value'] != before['risk']['value'] and None not in (now['risk']['value'], before['risk']['value']):
        out.append({'kind': 'risk', 'name': '综合风险', 'from': before['risk']['value'], 'to': now['risk']['value']})
    old = {f['key']: f for f in before['factors']}
    for f in now['factors']:
        if f['key'] in old and old[f['key']]['flagged'] != f['flagged']:
            out.append({'kind': 'factor', 'name': f['name'], 'from': old[f['key']]['flagged'], 'to': f['flagged']})
    old_media = {m['key']: m for m in before['media']}
    for m in now['media']:
        prior = old_media.get(m['key'])
        if prior and (prior['status'], prior['region']) != (m['status'], m['region']):
            out.append({'kind': 'media', 'name': m['name'], 'from': ' '.join(filter(None, (prior['status'], prior['region']))),
                        'to': ' '.join(filter(None, (m['status'], m['region'])))})
    return out


def _reports(sections_blob):
    """The sections of a stored run that carry an IP.Check.Place report, each with its label (IPv4/IPv6)."""
    try:
        sections = json.loads(zlib.decompress(sections_blob)) if sections_blob else []
    except (zlib.error, ValueError):
        return []
    return [(section['json'], _text(section.get('label'), 10)) for section in (sections if isinstance(sections, list) else [])
            if isinstance(section, dict) and isinstance(section.get('json'), dict) and section['json'].get('Info')]


def _report(sections_blob, family=None):
    """The first of them (of that family when given), and its label."""
    return next(((r, label) for r, label in _reports(sections_blob) if family is None or label == family), (None, ''))


def _same_address(a, b):
    """Whether two addresses may be the same one; either may be masked as IP.Check.Place masks it ('203.0.*.*').
    One not known is taken to be."""
    if not a or not b:
        return True
    if '*' not in a + b:
        return a == b
    a, b = a.split('*', 1)[0], b.split('*', 1)[0]
    return a.startswith(b) or b.startswith(a)


async def latest(conn, owner, server_id, ips=None):
    """The newest finished IP.Check.Place report, what changed since the one before it for the same address, and a
    run under way if there is one (the page follows it, then reads this again). The card is the server's IPv4 one
    unless it has only IPv6 reports. ips: the address each run IpChecks started was for, by run id; for another the
    report's masked one is all there is. moved: the report is not for the server's address now."""
    going = await many(conn, f"SELECT id, state, created_ms, started_ms FROM server_bench_runs WHERE owner=? AND server_id=? AND test=? "
                             f"AND state IN ({','.join('?' * len(UNDER_WAY))}) ORDER BY created_ms DESC LIMIT 1", (owner, server_id, TEST, *UNDER_WAY))
    pending = {'runId': going[0]['id'], 'state': going[0]['state'], 'startedMs': going[0]['started_ms'] or going[0]['created_ms']} if going else None
    rows = await many(conn, "SELECT id, ended_ms, sections FROM server_bench_runs WHERE owner=? AND server_id=? AND test=? AND state='done' "
                            'ORDER BY ended_ms DESC LIMIT 10', (owner, server_id, TEST))
    found = []
    for row in rows:
        for report, family in _reports(row['sections']):
            summary = summarize(report)
            if summary:
                ip = (ips or {}).get(row['id']) if family == 'IPv4' else None
                found.append({'runId': row['id'], 'atMs': row['ended_ms'], 'family': family,
                              'ip': ip or _text(((report.get('Head') or {}) if isinstance(report.get('Head'), dict) else {}).get('IP'), 64), **summary})
    if not found:
        return {'ok': True, 'report': None, 'changes': [], 'previousAtMs': 0, 'pending': pending}
    family = 'IPv4' if any(f['family'] == 'IPv4' for f in found) else found[0]['family']
    now, *older = [f for f in found if f['family'] == family]
    before = next((f for f in older if _same_address(now['ip'], f['ip'])), None)
    node = await one(conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))
    info = (json.loads(node['info_json'] or '{}').get('info') or {}) if node else {}
    current = str(info.get('ipv6' if family == 'IPv6' else 'ipv4') or '')
    now['moved'] = not _same_address(now['ip'], current)
    return {'ok': True, 'report': now, 'changes': changes(now, before), 'previousAtMs': before['atMs'] if before else 0, 'pending': pending}


def _masked_net(report):
    """The leading part of the address a report was for, as IP.Check.Place masks it ('203.0.*.*' -> '203.0.')."""
    head = str(((report or {}).get('Head') or {}).get('IP') or '')
    return head.split('*', 1)[0] if '*' in head else ''


class IpChecks(ServerChecks):
    """IP 质量 by itself: an IP.Check.Place run (评测, app/servers/bench.py) for a server never checked, one whose
    public IPv4 is not the one its last run was for, and one whose last run is ipcheckDays old. At most AT_ONCE
    under way of all servers'; a server whose own run did not end well is left FAILED_S. Only an online Linux node
    that runs commands and has a public IPv4; neither a front nor a server in mainland China (ServerChecks)."""
    KEY = 'ipcheck'
    TICK_S = 120
    AT_ONCE = 2
    FAILED_S = 6 * 3600

    def __init__(self, service):
        super().__init__(service)
        # Room is counted and taken under it: servers coming online together would each see the same room.
        self.starting = asyncio.Lock()

    def capable(self, server_id):
        session = self.service.hub.sessions.get(server_id)
        info = (session.info or {}) if session else {}
        return bool(session) and info.get('platform') == 'linux' and (session.caps or {}).get('exec') is not False and bool(info.get('ipv4'))

    def due(self, server_id, stored, info, zone):
        # Worked out from its runs (why), which the hello's quick look cannot: its check() asks.
        return True

    async def why(self, server_id, ip, days, now=None):
        """'never', 'moved', 'old', or '' when not due (or one under way, or left after a failed one)."""
        now = now or int(time.time() * 1000)
        rows = await many(self.db.conn, 'SELECT id, state, created_ms, ended_ms FROM server_bench_runs WHERE server_id=? AND test=? '
                                        'ORDER BY created_ms DESC LIMIT 10', (server_id, TEST))
        if any(r['state'] in UNDER_WAY for r in rows):
            return ''
        kept = await self._kept(server_id)
        recent = now - kept.get('atMs', 0) < self.FAILED_S * 1000
        # Its own last one did not end well, or could not be started at all (a platform 评测 has no build for).
        if recent and (kept.get('error') or rows and kept.get('runId') == rows[0]['id'] and rows[0]['state'] != 'done'):
            return ''
        for row in rows:
            if row['state'] != 'done':
                continue
            blob = await one(self.db.conn, 'SELECT sections FROM server_bench_runs WHERE id=?', (row['id'],))
            # Its IPv4 report: an IPv6 one (a run of the user's) says nothing of the address checked here.
            report, _ = _report(blob['sections'] if blob else None, 'IPv4')
            if report is None:
                continue
            if row['id'] in (kept.get('ips') or {}):
                moved = kept['ips'][row['id']] != ip
            else:
                net = _masked_net(report)
                moved = bool(net) and not ip.startswith(net)
            return 'moved' if moved else 'old' if now - (row['ended_ms'] or 0) >= days * 86400000 else ''
        return 'never'

    async def _kept(self, server_id):
        try:
            return json.loads(await self.service.metrics.meta(f'ipcheck:{server_id}') or '{}')
        except ValueError:
            return {}

    async def _room(self):
        row = await one(self.db.conn, f"SELECT COUNT(*) AS n FROM server_bench_runs WHERE test=? AND state IN ({','.join('?' * len(UNDER_WAY))})",
                        (TEST, *UNDER_WAY))
        return self.AT_ONCE - row['n']

    async def _start(self, server_id, ip):
        node = await one(self.db.conn, 'SELECT owner FROM server_nodes WHERE server_id=?', (server_id,))
        if not node:
            return False
        # The address each of its last runs was for, and how the last start went.
        ips = dict(list(((await self._kept(server_id)).get('ips') or {}).items())[-4:])
        kept = {'atMs': int(time.time() * 1000), 'ips': ips}
        try:
            kept['runId'] = (await self.service.bench.create(node['owner'], {'serverId': server_id, 'test': TEST, 'options': {'family': '4'}}))['runs'][0]['id']
            ips[kept['runId']] = ip
        except InstanceError as exc:
            kept['error'] = str(exc)
        await self.service.metrics.set_meta(f'ipcheck:{server_id}', json.dumps(kept))
        return 'runId' in kept

    async def _ip(self, server_id):
        session = self.service.hub.sessions.get(server_id)
        return str(((session.info or {}) if session else {}).get('ipv4') or '')

    async def check(self, server_id):
        """One server, on its hello: started when due and there is room."""
        days = (await self.service.settings())['ipcheckDays']
        ip = await self._ip(server_id)
        async with self.starting:
            if self.capable(server_id) and await self._room() > 0 and await self.why(server_id, ip, days):
                await self._start(server_id, ip)

    async def tick(self):
        """Every server due, those never checked or whose address moved first, while there is room."""
        if not await self.enabled():
            return
        async with self.starting:
            await self._tick()

    async def _tick(self):
        room = await self._room()
        if room <= 0:
            return
        days, order = (await self.service.settings())['ipcheckDays'], {'never': 0, 'moved': 1, 'old': 2}
        due = []
        for row in await many(self.db.conn, 'SELECT server_id FROM server_nodes'):
            server_id = row['server_id']
            if not self.capable(server_id) or await self.domestic(server_id):
                continue
            ip = await self._ip(server_id)
            reason = await self.why(server_id, ip, days)
            if reason:
                due.append((order[reason], server_id, ip))
        for _, server_id, ip in sorted(due):
            if room <= 0:
                break
            room -= await self._start(server_id, ip)

    async def loop(self):
        while True:
            await asyncio.sleep(self.TICK_S)
            try:
                await self.tick()
            except Exception:
                log.exception('ip quality checks')
