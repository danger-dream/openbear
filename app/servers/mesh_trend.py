"""Data plane trend per mesh member (plan §12.4), kept like server metrics: in the
metrics file, one row per member per minute folded from its reports, hourly rows
rolled up from those, and a series answered in at most about 120 points.

Averages: rttMs, timeoutRate, rssBytes, cpuPercent. Counts for the point's span:
switches (of entry), routeSwitches (of its routes' next hops), reconnects, rxBytes,
txBytes, taken as increments of the data plane's counters (which start from zero when
it restarts), and tcpPressures, the kernel's TCP memory pressure count (from zero when
the machine boots)."""
from __future__ import annotations

from collections import deque

from app.servers.mesh_status import entry_rtt

MINUTE_RETENTION = 2 * 86400
HOUR_RETENTION = 7 * 86400
# range -> (table, bucket seconds, span seconds)
RANGES = {'1h': ('mesh_minute', 60, 3600), '24h': ('mesh_minute', 720, 86400), '7d': ('mesh_hour', 7200, 7 * 86400)}
COLUMNS = ('samples', 'rtt', 'rtt_n', 'timeout_rate', 'timeout_n', 'rss', 'cpu', 'switches', 'reconnects', 'rx', 'tx', 'route_switches', 'pressures', 'route_flips')
_TABLE = ('(server_id TEXT NOT NULL, ts INTEGER NOT NULL, samples INTEGER NOT NULL, rtt REAL, rtt_n INTEGER NOT NULL, '
          'timeout_rate REAL, timeout_n INTEGER NOT NULL, rss INTEGER NOT NULL, cpu REAL NOT NULL, switches INTEGER NOT NULL, '
          'reconnects INTEGER NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL, route_switches INTEGER NOT NULL DEFAULT 0, '
          'pressures INTEGER NOT NULL DEFAULT 0, route_flips INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(server_id, ts)) WITHOUT ROWID')
SCHEMA = f'CREATE TABLE IF NOT EXISTS mesh_minute {_TABLE}; CREATE TABLE IF NOT EXISTS mesh_hour {_TABLE};'
# Columns added after the tables were first made; existing files gain them on start.
ADDED = ('route_switches', 'pressures', 'route_flips')
# The weighted aggregate of rows, for an hour's rollup and for a series' buckets.
_AGGREGATE = ('SUM(samples) AS samples, SUM(rtt*rtt_n)/NULLIF(SUM(rtt_n),0) AS rtt, SUM(rtt_n) AS rtt_n, '
              'SUM(timeout_rate*timeout_n)/NULLIF(SUM(timeout_n),0) AS timeout_rate, SUM(timeout_n) AS timeout_n, '
              'SUM(rss*samples)/SUM(samples) AS rss, SUM(cpu*samples)/SUM(samples) AS cpu, SUM(switches) AS switches, '
              'SUM(reconnects) AS reconnects, SUM(rx) AS rx, SUM(tx) AS tx, SUM(route_switches) AS route_switches, '
              'SUM(pressures) AS pressures, SUM(route_flips) AS route_flips')
PRESSURE_SPAN_S = 86400


def measured(status, role):
    """The peers one report measures the line by: a member's current entry, or a relay's active
    links to other relays (it does not measure the members it carries: they report 0)."""
    active = [p for p in status['peers'] if p['active']]
    if role == 'relay':
        return [p for p in active if p['kind'] == 'relay']
    relay = (status['entry'] or {}).get('relay')
    peers = [p for p in active if p['kind'] == 'entry' and p['id'] == relay][:1] if relay else []
    # The round trip as the member measures its entry, like the page and paths take it.
    return [{**peers[0], 'rttMs': entry_rtt(status, relay) or 0}] if peers else []


def rtt_basis(status, role):
    """What a trend's rttMs is the round trip to, by one report: {kind entry|links, peers}."""
    if role == 'relay':
        return {'kind': 'links', 'peers': [p['id'] for p in measured(status, role)] if status else []}
    relay = ((status or {}).get('entry') or {}).get('relay')
    return {'kind': 'entry', 'peers': [relay] if relay else []}


class Bucket:
    __slots__ = ('ts', *COLUMNS)

    def __init__(self, ts):
        self.ts = ts
        for key in COLUMNS:
            setattr(self, key, 0)

    def row(self, server_id):
        n = max(self.samples, 1)
        return (server_id, self.ts, self.samples, self.rtt / self.rtt_n if self.rtt_n else None, self.rtt_n,
                self.timeout_rate / self.timeout_n if self.timeout_n else None, self.timeout_n,
                int(self.rss / n), round(self.cpu / n, 2), self.switches, self.reconnects, self.rx, self.tx,
                self.route_switches, self.pressures, self.route_flips)


class MeshTrend:
    def __init__(self, metrics):
        self.metrics = metrics  # MetricsStore: its connection and lock to the metrics file
        self.buckets = {}       # server_id -> Bucket of the current minute
        self.counters = {}      # server_id -> counters of the last report, for increments
        self.pressure_minutes = {}  # server_id -> deque of (minute, TCP memory pressures) written in the last PRESSURE_SPAN_S
        self.last_prune = 0

    async def start(self):
        conn = self.metrics.conn
        async with self.metrics.lock:
            await conn.executescript(SCHEMA)
            for table in ('mesh_minute', 'mesh_hour'):
                async with conn.execute(f'PRAGMA table_info({table})') as cursor:
                    have = {r[1] async for r in cursor}
                for column in ADDED:
                    if column not in have:
                        await conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
            await conn.commit()
            async with conn.execute('SELECT server_id,ts,pressures FROM mesh_minute WHERE pressures>0 ORDER BY ts') as cursor:
                async for r in cursor:
                    self.pressure_minutes.setdefault(r[0], deque()).append((r[1], r[2]))

    def _increments(self, server_id, status):
        peers = {(p['id'], p['kind'], p['transport']): (p['reconnects'], p['rxBytes'], p['txBytes']) for p in status['peers']}
        routing_routes = (status.get('routing') or {}).get('routes', [])
        routes = {r['dest']: r['switches'] for r in routing_routes}
        flips = {r['dest']: r.get('flips', 0) for r in routing_routes}
        pressures = (((status.get('fast') or {}).get('tcpMem')) or {}).get('pressuresSinceBoot')
        now = {'startedAt': status['startedAt'], 'switches': (status['entry'] or {}).get('switches', 0), 'peers': peers,
               'routes': routes, 'flips': flips, 'pressures': pressures}
        last, self.counters[server_id] = self.counters.get(server_id), now
        if last is None:
            return 0, 0, 0, 0, 0, 0, 0  # What was counted before the first report predates the trend.
        restarted = last['startedAt'] != now['startedAt']

        def step(value, before):
            # A counter that is new, restarted or went down (its peer was set up again) counted from zero.
            return value if restarted or before is None or value < before else value - before
        sums = [0, 0, 0]
        for key, values in peers.items():
            before = last['peers'].get(key)
            for i in range(3):
                sums[i] += step(values[i], before[i] if before else None)
        route_switches = sum(step(n, last['routes'].get(dest)) for dest, n in routes.items())
        route_flips = sum(step(n, (last.get('flips') or {}).get(dest)) for dest, n in flips.items())
        # The kernel's count goes on through a data plane restart; one that went down is a machine that booted.
        before = last['pressures']
        pressure = 0 if pressures is None or before is None else pressures - before if pressures >= before else pressures
        return (step(now['switches'], last['switches']), route_switches, route_flips, *sums, pressure)

    def pressures_24h(self, server_id, now):
        """The TCP memory pressures the member's machine counted over the last PRESSURE_SPAN_S (now in
        seconds), as far as the trend has them."""
        minutes = self.pressure_minutes.get(server_id)
        while minutes and minutes[0][0] < now - PRESSURE_SPAN_S:
            minutes.popleft()
        bucket = self.buckets.get(server_id)
        return sum(n for _, n in minutes or ()) + (bucket.pressures if bucket else 0)

    async def observe(self, server_id, status, role, now_ms):
        """Fold one report into the member's minute; a finished minute is written."""
        ts = now_ms // 1000 - now_ms // 1000 % 60
        bucket = self.buckets.get(server_id)
        finished = None
        if bucket is None or bucket.ts != ts:
            finished = bucket if bucket is not None and bucket.samples else None
            bucket = self.buckets[server_id] = Bucket(ts)
        switches, route_switches, route_flips, reconnects, rx, tx, pressures = self._increments(server_id, status)
        peers = measured(status, role)
        rtts = [p['rttMs'] for p in peers if p['rttMs']]  # 0: not measured
        bucket.samples += 1
        if rtts:
            bucket.rtt, bucket.rtt_n = bucket.rtt + sum(rtts) / len(rtts), bucket.rtt_n + 1
        if peers:
            bucket.timeout_rate = bucket.timeout_rate + sum(p['timeoutRate'] for p in peers) / len(peers)
            bucket.timeout_n += 1
        bucket.rss += status['resources']['rssBytes']
        bucket.cpu += status['resources']['cpuPercent']
        bucket.switches, bucket.reconnects, bucket.rx, bucket.tx = (bucket.switches + switches, bucket.reconnects + reconnects,
                                                                    bucket.rx + rx, bucket.tx + tx)
        bucket.route_switches += route_switches
        bucket.route_flips += route_flips
        bucket.pressures += pressures
        if finished:
            await self._write(server_id, finished, now_ms // 1000)

    async def _write(self, server_id, bucket, now):
        conn = self.metrics.conn
        hour = bucket.ts - bucket.ts % 3600
        async with self.metrics.lock:
            try:
                await conn.execute(f'INSERT OR REPLACE INTO mesh_minute(server_id,ts,{",".join(COLUMNS)}) VALUES({",".join("?" * (len(COLUMNS) + 2))})',
                                   bucket.row(server_id))
                await conn.execute(f'INSERT OR REPLACE INTO mesh_hour(server_id,ts,{",".join(COLUMNS)}) SELECT server_id,?,{_AGGREGATE} '
                                   'FROM mesh_minute WHERE server_id=? AND ts>=? AND ts<? GROUP BY server_id', (hour, server_id, hour, hour + 3600))
                if now - self.last_prune > 3600:
                    self.last_prune = now
                    await conn.execute('DELETE FROM mesh_minute WHERE ts<?', (now - MINUTE_RETENTION,))
                    await conn.execute('DELETE FROM mesh_hour WHERE ts<?', (now - HOUR_RETENTION - 3600,))
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        if bucket.pressures:
            minutes = self.pressure_minutes.setdefault(server_id, deque())
            minutes.append((bucket.ts, bucket.pressures))
            while minutes and minutes[0][0] < now - PRESSURE_SPAN_S:
                minutes.popleft()

    async def forget(self, server_id):
        self.buckets.pop(server_id, None)
        self.counters.pop(server_id, None)
        self.pressure_minutes.pop(server_id, None)
        async with self.metrics.lock:
            for table in ('mesh_minute', 'mesh_hour'):
                await self.metrics.conn.execute(f'DELETE FROM {table} WHERE server_id=?', (server_id,))
            await self.metrics.conn.commit()

    async def series(self, server_id, span, now_ms):
        table, step, length = RANGES[span]
        now = now_ms // 1000
        start = (now - length) // step * step + step
        sql = f'SELECT ts/{step}*{step} AS t,{_AGGREGATE} FROM {table} WHERE server_id=? AND ts>=? GROUP BY t ORDER BY t'
        points = []
        async with self.metrics.conn.execute(sql, (server_id, start)) as cursor:
            async for r in cursor:
                points.append({'at': r['t'] * 1000, 'rttMs': round(r['rtt'], 1) if r['rtt'] is not None else None,
                               'timeoutRate': round(r['timeout_rate'], 3) if r['timeout_rate'] is not None else None,
                               'switches': int(r['switches']), 'reconnects': int(r['reconnects']), 'rssBytes': int(r['rss']),
                               'cpuPercent': round(r['cpu'], 1), 'rxBytes': int(r['rx']), 'txBytes': int(r['tx']),
                               'routeSwitches': int(r['route_switches']), 'routeFlips': int(r['route_flips']), 'tcpPressures': int(r['pressures'])})
        return points
