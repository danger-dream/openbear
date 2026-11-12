"""三网回程: which line a server's traffic back to China takes, to China Telecom, Unicom and Mobile in
Beijing, Shanghai and Guangdong, over IPv4. The node only runs its usual trace (net.probe); the
targets and the reading of the hops live here, so they change without a node update."""
from __future__ import annotations

import asyncio
import json
import time
import zlib
from datetime import datetime, timedelta

from app.instances.contracts import InstanceError
from app.servers import geo
from app.servers.checks import ServerChecks
from app.webhooks.repository import one

# Bump when the targets or the reading change: results made by an older one are made again.
VERSION = 1
FEATURE = 'net'
CITIES = (('bj', '北京'), ('sh', '上海'), ('gd', '广东'))
CARRIERS = (('ct', '电信'), ('cu', '联通'), ('cm', '移动'))
TARGETS = [(f'{city}-{carrier}', f'{city}-{carrier}-v4.ip.zstaticcdn.com') for city, _ in CITIES for carrier, _ in CARRIERS]
PROBE = {'mode': 'trace', 'family': 4, 'count': 2, 'intervalMs': 500, 'timeoutMs': 1500, 'maxHops': 30}
# A node runs at most four tests at a time: one is left for the page's own.
TRACES_AT_ONCE = 3
POLL_S, TRACE_LIMIT_S = 2, 90
NODE_GONE = {'node_offline', 'node_connection_lost', 'node_not_enrolled'}
# Once a day, each server at its own minute between four and five in the morning.
DAILY_HOUR = 4

# The lines and how good they are: gold the premium ones, silver the ones between. CMI is ordinary:
# crowded, its latency far too high to count as optimised.
TIERS = {'CN2 GIA': 'gold', 'CTG': 'gold', '9929': 'gold', 'CMIN2': 'gold',
         'CN2 GT': 'silver', '10099': 'silver',
         'CMI': 'plain', '163': 'plain', '4837': 'plain', 'CMNET': 'plain', 'CERNET': 'plain', 'CSTNET': 'plain'}
RANK = {'gold': 3, 'silver': 2, 'plain': 1}
# China Mobile's provincial networks, which a return over CMNET enters through.
CM_PROVINCES = {9808, 24400, 24444, 24445, 24547, 38019, 56040, 56041, 56042, 56044, 56046, 56047, 56048, 132525}
CHINA = {4134, 4809, 23764, 4837, 9929, 10099, 58453, 58807, 4538, 7497} | CM_PROVINCES


def asn_of(hop):
    """The hop's AS; CN2's 59.43.0.0/16 is not in the GeoIP services' data."""
    ip = hop.get('ip') or ''
    if ip.startswith('59.43.'):
        return 4809
    if 'ctgnet' in (hop.get('org') or '').lower():
        return 23764
    return hop.get('asn')


def classify(hops):
    """The line a trace's hops show, or '' when they do not show one (the route is hidden, or
    nothing in China answered)."""
    answered = [h for h in hops if h.get('ip')]
    asns = [asn_of(h) for h in answered]
    seen = set(asns)
    if 58807 in seen:
        return 'CMIN2'
    if 9929 in seen:
        return '9929'
    if 4809 in seen:
        # CN2 all the way is GIA; handed on to the 163 backbone (202.97.0.0/16) it is GT.
        return 'CN2 GT' if any(h['ip'].startswith('202.97.') for h in answered) else 'CN2 GIA'
    if 23764 in seen:
        return 'CTG'
    if 10099 in seen:
        return '10099'
    if 58453 in seen:
        return 'CMI'
    # Otherwise by where the route enters China; only the target answering from there proves nothing.
    entry = next((i for i, h in enumerate(answered) if asns[i] in CHINA or h['ip'].startswith('202.97.')
                  or (h.get('country') == 'CN' and asns[i])), None)
    if entry is None or (entry == len(answered) - 1 and len(answered) > 1 and answered[entry].get('reached')):
        return ''
    return carrier_line(answered[entry], asns[entry])


def carrier_line(hop, asn):
    """The ordinary line of the carrier a hop belongs to: provincial networks have ASes of their
    own (Beijing Unicom 4808, Shanghai Telecom 4812, ...), known by their operator's name."""
    if asn == 4134 or hop['ip'].startswith('202.97.'):
        return '163'
    line = {4837: '4837', 4538: 'CERNET', 7497: 'CSTNET'}.get(asn) or ('CMNET' if asn in CM_PROVINCES else '')
    if line:
        return line
    org = (hop.get('org') or '').lower()
    if 'unicom' in org or 'china169' in org or 'cncgroup' in org:
        return '4837'
    if 'chinanet' in org or 'china telecom' in org:
        return '163'
    if 'china mobile' in org and 'international' not in org:
        return 'CMNET'
    return ''


def hop_rows(probe):
    """A trace's hops as kept: ttl, the first address that answered (or ''), its average round trip."""
    rows = []
    for hop in probe.get('hops') if isinstance(probe.get('hops'), list) else []:
        if not isinstance(hop, dict):
            continue
        addrs = [a for a in hop.get('addrs') or [] if isinstance(a, dict) and isinstance(a.get('ip'), str)]
        rtt = hop.get('avgMs')
        rows.append({'ttl': hop.get('ttl') if isinstance(hop.get('ttl'), int) else len(rows) + 1, 'ip': addrs[0]['ip'][:64] if addrs else '',
                     'rttMs': round(rtt, 1) if isinstance(rtt, (int, float)) else None, 'reached': hop.get('reached') is True})
    return rows[:64]


def slot(server_id, now, zone):
    """The start of the server's most recent daily slot, in ms."""
    local = datetime.fromtimestamp(now / 1000, zone)
    minute = zlib.crc32(server_id.encode()) % 60
    today = local.replace(hour=DAILY_HOUR, minute=minute, second=0, microsecond=0)
    if local < today:
        today -= timedelta(days=1)
    return int(today.timestamp() * 1000)


def now_ms():
    return int(time.time() * 1000)


def retier(route):
    """A stored result with each line's tier as the table has it now: a result checked before a line moved
    tier reads as it would today. A line the table does not know keeps what it was given."""
    if not route or not isinstance(route.get('results'), dict):
        return route
    return {**route, 'results': {k: {**r, 'tier': TIERS.get(r.get('line') or '', r.get('tier') or '')} if isinstance(r, dict) else r
                                 for k, r in route['results'].items()}}


class RouteChecks(ServerChecks):
    """Keeps each server's newest result in its node record, under route:
    {v, at, ip, results: {bj-ct: {line, tier, rttMs, reached, error, hops}, ...}}."""
    KEY = 'route'
    AT_ONCE = 3

    def due(self, server_id, stored, info, zone):
        last = stored.get('route') or {}
        if not info.get('ipv4'):
            return False
        return last.get('v') != VERSION or last.get('ip') != info.get('ipv4') or (last.get('at') or 0) < slot(server_id, now_ms(), zone)

    def capable(self, server_id):
        session = self.service.hub.sessions.get(server_id)
        return bool(session and FEATURE in session.features and (session.info or {}).get('ipv4'))

    async def _trace(self, owner, server_id, target):
        """One trace's hops, or (None, error)."""
        for _ in range(10):
            try:
                started = await self.service._node_call(owner, server_id, 'net.probe.start', {**PROBE, 'target': target}, timeout=15)
                break
            except InstanceError as exc:
                if exc.error != 'probe_busy':
                    raise
            await asyncio.sleep(5)
        else:
            return None, 'busy'
        pid = started.get('id')
        if not isinstance(pid, str) or not pid:
            return None, 'node_error'
        deadline = time.monotonic() + TRACE_LIMIT_S
        while True:
            await asyncio.sleep(POLL_S)
            status = await self.service._node_call(owner, server_id, 'net.probe.status', {'id': pid}, timeout=15)
            if status.get('state') != 'running':
                break
            if time.monotonic() > deadline:
                await self.service._node_call(owner, server_id, 'net.probe.stop', {'id': pid}, timeout=15)
                break
        # The node ends a trace it could not run (dns_failed, icmp_unavailable) as error.
        if status.get('state') in ('error', 'failed') and not status.get('hops'):
            return None, str(status.get('error') or 'failed')[:120]
        return hop_rows(status), ''

    async def _networks(self, server_id, ips):
        """GeoIP of the hops, through the controller's shared cache of hop lookups."""
        lookups = self.service.net.networks
        lookups.want(ips, server_id)
        for _ in range(60):
            if not any(ip in lookups.pending for ip in ips):
                break
            await asyncio.sleep(0.5)
        return {ip: lookups.get(ip) for ip in ips}

    async def check(self, server_id):
        session = self.service.hub.sessions.get(server_id)
        if session is None:
            raise InstanceError('node_offline', 409)
        if FEATURE not in session.features:
            raise InstanceError('node_update_required', 409)
        row = await one(self.db.conn, 'SELECT owner, info_json FROM server_nodes WHERE server_id=?', (server_id,))
        if not row:
            raise InstanceError('node_not_enrolled', 404)
        ip = (json.loads(row['info_json'] or '{}').get('info') or {}).get('ipv4') or ''
        if not ip:
            raise InstanceError('route_no_ipv4', 409)
        gate = asyncio.Semaphore(TRACES_AT_ONCE)

        async def one_trace(key, target):
            async with gate:
                try:
                    return key, *await self._trace(row['owner'], server_id, target)
                except InstanceError as exc:
                    # A node gone ends the whole check; one target that failed is that target's result.
                    if exc.error in NODE_GONE:
                        raise
                    return key, None, exc.error
        traced = await asyncio.gather(*(one_trace(k, t) for k, t in TARGETS))
        public = list(dict.fromkeys(h['ip'] for _, hops, _ in traced for h in hops or [] if h['ip'] and geo.public_address(h['ip'])))
        found = await self._networks(server_id, public) if public else {}
        results = {}
        for key, hops, error in traced:
            if hops is None:
                results[key] = {'line': '', 'tier': '', 'rttMs': None, 'reached': False, 'error': error, 'hops': []}
                continue
            for h in hops:
                info = found.get(h['ip']) or {}
                h.update(asn=info.get('asn'), org=info.get('org') or '', country=info.get('country') or '')
            line = classify(hops)
            last = next((h for h in reversed(hops) if h['ip'] and h['rttMs'] is not None), None)
            results[key] = {'line': line, 'tier': TIERS.get(line, ''), 'rttMs': last['rttMs'] if last else None,
                            'reached': any(h['reached'] for h in hops), 'error': '', 'hops': hops}
        summary = {'v': VERSION, 'at': now_ms(), 'ip': ip, 'results': results}
        await self._save(server_id, summary)
        # Which of the three carriers it measures follows the lines found (延迟监测).
        self.service.latency.soon(server_id)
        return summary
