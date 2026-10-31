"""Server metrics in their own SQLite file, away from the main database's
writer lock. Live samples stay in memory; one averaged row per server per
minute is written, and hourly rows are rolled up from those."""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from pathlib import Path

import aiosqlite

MINUTE_RETENTION = 31 * 86400
HOUR_RETENTION = 400 * 86400
# Seconds a node's reported boot time may move without meaning a reboot
# (Windows derives it from the clock and the uptime, both truncated).
BOOT_SLACK = 60
COLUMNS = ('cpu', 'mem', 'mem_total', 'swap', 'disk', 'disk_total', 'load', 'tcp', 'udp', 'proc', 'steal', 'iowait', 'io_r', 'io_w')
# Columns added after the first schema; existing files gain them on start.
ADDED = {'steal': 'REAL', 'iowait': 'REAL', 'io_r': 'INTEGER', 'io_w': 'INTEGER'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS minute (
 server_id TEXT NOT NULL, ts INTEGER NOT NULL, samples INTEGER NOT NULL,
 cpu REAL, mem INTEGER, mem_total INTEGER, swap INTEGER, disk INTEGER, disk_total INTEGER,
 load REAL, tcp INTEGER, udp INTEGER, proc INTEGER, rx INTEGER NOT NULL, tx INTEGER NOT NULL,
 PRIMARY KEY(server_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hour (
 server_id TEXT NOT NULL, ts INTEGER NOT NULL, samples INTEGER NOT NULL,
 cpu REAL, mem INTEGER, mem_total INTEGER, swap INTEGER, disk INTEGER, disk_total INTEGER,
 load REAL, tcp INTEGER, udp INTEGER, proc INTEGER, rx INTEGER NOT NULL, tx INTEGER NOT NULL,
 PRIMARY KEY(server_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS counters (
 server_id TEXT PRIMARY KEY, boot INTEGER NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL, at INTEGER NOT NULL);
'''
# range -> (table, bucket seconds, span seconds)
RANGES = {'1h': ('minute', 60, 3600), '24h': ('minute', 300, 86400), '7d': ('hour', 3600, 7 * 86400),
          '30d': ('hour', 4 * 3600, 30 * 86400)}
# What a stored value may be (hub.SAMPLE_CAP): a minute's sums of these, an hour's of those and the
# weighted averages all stay within SQLite's 64-bit integers.
SAMPLE_CAP = 2 ** 53
MINUTE_TRAFFIC_CAP = SAMPLE_CAP
# Errors that come from one row's values: that row is dropped, the others are written.
BAD_ROW = (OverflowError, ValueError, TypeError, sqlite3.DataError, sqlite3.IntegrityError)


def _bad_row(exc):
    return isinstance(exc, BAD_ROW) or isinstance(exc, sqlite3.OperationalError) and 'overflow' in str(exc)


def _cap(value):
    return min(max(value, 0), SAMPLE_CAP)


def _sample_values(m):
    mem, swap, disk = m.get('mem') or {}, m.get('swap') or {}, m.get('disk') or {}
    load = m.get('load') or [0]
    values = {'cpu': float(m.get('cpu') or 0), 'mem': int(mem.get('used') or 0), 'mem_total': int(mem.get('total') or 0),
              'swap': int(swap.get('used') or 0), 'disk': int(disk.get('used') or 0), 'disk_total': int(disk.get('total') or 0),
              'load': float(load[0] if load else 0), 'tcp': int(m.get('tcp') or 0), 'udp': int(m.get('udp') or 0),
              'proc': int(m.get('processes') or 0), 'steal': float(m.get('steal') or 0), 'iowait': float(m.get('iowait') or 0),
              'io_r': int((m.get('io') or {}).get('readRate') or 0), 'io_w': int((m.get('io') or {}).get('writeRate') or 0)}
    return {k: _cap(v) for k, v in values.items()}


class Bucket:
    __slots__ = ('ts', 'samples', 'sums', 'rx', 'tx', 'counters')

    def __init__(self, ts):
        self.ts, self.samples, self.sums, self.rx, self.tx, self.counters = ts, 0, dict.fromkeys(COLUMNS, 0), 0, 0, None

    def row(self, server_id):
        n = max(self.samples, 1)
        avg = {k: v / n for k, v in self.sums.items()}
        return (server_id, self.ts, self.samples, round(avg['cpu'], 2), int(avg['mem']), int(avg['mem_total']), int(avg['swap']),
                int(avg['disk']), int(avg['disk_total']), round(avg['load'], 2), round(avg['tcp']), round(avg['udp']),
                round(avg['proc']), min(self.rx, MINUTE_TRAFFIC_CAP), min(self.tx, MINUTE_TRAFFIC_CAP), round(avg['steal'], 2), round(avg['iowait'], 2),
                int(avg['io_r']), int(avg['io_w']))


class MetricsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.conn = None
        self.lock = asyncio.Lock()
        self.buckets = {}       # server_id -> Bucket being filled
        self.pending = []       # (server_id, Bucket) completed, not yet written
        self.counters = {}      # server_id -> (boot, rx, tx) of the last sample seen
        self.flusher = None
        self.last_prune = 0

    async def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute('PRAGMA journal_mode=WAL')
        await self.conn.execute('PRAGMA synchronous=NORMAL')
        await self.conn.executescript(SCHEMA)
        for table in ('minute', 'hour'):
            async with self.conn.execute(f'PRAGMA table_info({table})') as cursor:
                present = {r['name'] async for r in cursor}
            for column, kind in ADDED.items():
                if column not in present:
                    await self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
        await self.conn.commit()
        async with self.conn.execute('SELECT server_id,boot,rx,tx FROM counters') as cursor:
            self.counters = {r['server_id']: (r['boot'], r['rx'], r['tx']) async for r in cursor}
        self.flusher = asyncio.create_task(self._flush_loop(), name='server-metrics-flush')

    async def close(self):
        if self.flusher:
            self.flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.flusher
            self.flusher = None
        if self.conn:
            for server_id, bucket in list(self.buckets.items()):
                if bucket.samples:
                    self.pending.append((server_id, bucket))
            self.buckets.clear()
            with contextlib.suppress(Exception):
                await self.flush()
            await self.conn.close()
            self.conn = None

    def observe(self, server_id, m, now=None):
        """Fold one live sample into the current minute and return traffic deltas."""
        now = int(now or time.time())
        ts = now - now % 60
        bucket = self.buckets.get(server_id)
        if bucket is None or bucket.ts != ts:
            if bucket is not None and bucket.samples:
                self.pending.append((server_id, bucket))
            bucket = self.buckets[server_id] = Bucket(ts)
        net = m.get('net') or {}
        boot, rx, tx = _cap(int(m.get('boot') or 0)), _cap(int(net.get('rx') or 0)), _cap(int(net.get('tx') or 0))
        last = self.counters.get(server_id)
        if last is None:
            drx = dtx = 0  # Traffic before the first sample predates monitoring.
        elif abs(boot - last[0]) > BOOT_SLACK:
            drx, dtx = rx, tx  # Rebooted: counting restarted from zero.
        else:
            # Boot times derived from the clock wobble by a second or so; a
            # counter that shrinks without a reboot lost an interface, which
            # must not bill everything it ever counted again.
            drx, dtx = max(rx - last[1], 0), max(tx - last[2], 0)
        self.counters[server_id] = (boot, rx, tx)
        for key, value in _sample_values(m).items():
            bucket.sums[key] += value
        bucket.samples += 1
        bucket.rx += drx
        bucket.tx += dtx
        bucket.counters = (boot, rx, tx)
        return drx, dtx

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(20)
            now = int(time.time())
            for server_id, bucket in list(self.buckets.items()):
                if bucket.ts + 60 <= now - 5:
                    del self.buckets[server_id]
                    if bucket.samples:
                        self.pending.append((server_id, bucket))
            try:
                await self.flush()
                if now - self.last_prune > 3600:
                    await self.prune(now)
                    self.last_prune = now
            except Exception:
                pass  # Keep pending rows; the next round retries.

    async def flush(self):
        """Row by row: one whose values cannot be stored is dropped on its own, so one node's bad
        sample never stops the others' minutes. Any other failure keeps the rest for the next round."""
        async with self.lock:
            if not self.pending or not self.conn:
                return
            batch, self.pending = self.pending, []
            for index, (server_id, bucket) in enumerate(batch):
                try:
                    await self.conn.execute(f'INSERT OR REPLACE INTO minute(server_id,ts,samples,{",".join(COLUMNS[:10])},rx,tx,{",".join(COLUMNS[10:])}) '
                                            f'VALUES({",".join("?" * (len(COLUMNS) + 5))})', bucket.row(server_id))
                    # The checkpoint matches exactly the traffic already written.
                    boot, rx, tx = bucket.counters
                    await self.conn.execute('INSERT OR REPLACE INTO counters VALUES(?,?,?,?,?)', (server_id, boot, rx, tx, bucket.ts))
                    await self._rollup(server_id, bucket.ts - bucket.ts % 3600)
                    await self.conn.commit()
                except Exception as exc:
                    await self.conn.rollback()
                    if not _bad_row(exc):
                        self.pending = batch[index:] + self.pending
                        raise

    async def _rollup(self, server_id, hour):
        # TOTAL never overflows where SUM of large values times their sample counts would.
        weighted = ','.join(f'TOTAL({c}*samples)/SUM(samples)' for c in COLUMNS)
        await self.conn.execute(f'INSERT OR REPLACE INTO hour(server_id,ts,samples,{",".join(COLUMNS)},rx,tx) SELECT server_id,?,SUM(samples),{weighted},SUM(rx),SUM(tx) '
                                'FROM minute WHERE server_id=? AND ts>=? AND ts<? GROUP BY server_id', (hour, server_id, hour, hour + 3600))

    async def prune(self, now=None):
        now = int(now or time.time())
        async with self.lock:
            await self.conn.execute('DELETE FROM minute WHERE ts<?', (now - MINUTE_RETENTION,))
            await self.conn.execute('DELETE FROM hour WHERE ts<?', (now - HOUR_RETENTION,))
            await self.conn.commit()

    async def rebase(self, server_id):
        """Another node now reports for this server (reinstalled, or a machine taking it over): its
        first sample starts the count, not taken for a reboot that counts all since its boot."""
        self.counters.pop(server_id, None)
        async with self.lock:
            await self.conn.execute('DELETE FROM counters WHERE server_id=?', (server_id,))
            await self.conn.commit()

    async def forget(self, server_id):
        self.buckets.pop(server_id, None)
        self.counters.pop(server_id, None)
        self.pending = [p for p in self.pending if p[0] != server_id]
        async with self.lock:
            for table in ('minute', 'hour', 'counters'):
                await self.conn.execute(f'DELETE FROM {table} WHERE server_id=?', (server_id,))
            await self.conn.commit()

    async def series(self, server_id, span, now=None):
        table, step, length = RANGES[span]
        now = int(now or time.time())
        start = now - length
        weighted = ','.join(f'TOTAL({c}*samples)/SUM(samples) AS {c}' for c in COLUMNS)
        sql = (f'SELECT ts/{step}*{step} AS t,{weighted},SUM(rx) AS rx,SUM(tx) AS tx FROM {table} '
               f'WHERE server_id=? AND ts>=? GROUP BY t ORDER BY t')
        points = {k: [] for k in ('t', 'cpu', 'steal', 'iowait', 'mem', 'swap', 'disk', 'load', 'rxRate', 'txRate', 'ioRead', 'ioWrite', 'tcp', 'udp', 'proc')}
        async with self.conn.execute(sql, (server_id, start)) as cursor:
            async for r in cursor:
                points['t'].append(r['t'])
                points['cpu'].append(round(r['cpu'] or 0, 2))
                points['steal'].append(round(r['steal'] or 0, 2))
                points['iowait'].append(round(r['iowait'] or 0, 2))
                points['ioRead'].append(int(r['io_r'] or 0))
                points['ioWrite'].append(int(r['io_w'] or 0))
                points['mem'].append(round((r['mem'] or 0) * 100 / r['mem_total'], 2) if r['mem_total'] else 0)
                points['swap'].append(int(r['swap'] or 0))
                points['disk'].append(round((r['disk'] or 0) * 100 / r['disk_total'], 2) if r['disk_total'] else 0)
                points['load'].append(round(r['load'] or 0, 2))
                points['rxRate'].append(int((r['rx'] or 0) / step))
                points['txRate'].append(int((r['tx'] or 0) / step))
                for key in ('tcp', 'udp', 'proc'):
                    points[key].append(round(r[key] or 0))
        return {'range': span, 'step': step, 'start': start, 'end': now, 'points': points}

    async def traffic_since(self, server_id, start):
        """Bytes received/sent since a unix time: whole hours from the hourly
        table, the rest from minutes, plus what is still in memory."""
        hour = start - start % 3600 + (3600 if start % 3600 else 0)
        rx = tx = 0
        # Summed as TOTAL: a month of a node's absurd counters must not overflow the listing.
        async with self.conn.execute('SELECT TOTAL(rx),TOTAL(tx) FROM minute WHERE server_id=? AND ts>=? AND ts<?', (server_id, start, hour)) as c:
            a, b = await c.fetchone()
            rx, tx = rx + int(a), tx + int(b)
        async with self.conn.execute('SELECT MAX(ts) FROM hour WHERE server_id=? AND ts>=?', (server_id, hour)) as c:
            (last_hour,) = await c.fetchone()
        if last_hour is not None:
            async with self.conn.execute('SELECT TOTAL(rx),TOTAL(tx) FROM hour WHERE server_id=? AND ts>=? AND ts<?', (server_id, hour, last_hour)) as c:
                a, b = await c.fetchone()
                rx, tx = rx + int(a), tx + int(b)
            hour = last_hour
        async with self.conn.execute('SELECT TOTAL(rx),TOTAL(tx) FROM minute WHERE server_id=? AND ts>=?', (server_id, max(hour, start))) as c:
            a, b = await c.fetchone()
            rx, tx = rx + int(a), tx + int(b)
        for sid, bucket in [*self.pending, *((k, v) for k, v in self.buckets.items())]:
            if sid == server_id and bucket.ts >= start:
                rx, tx = rx + bucket.rx, tx + bucket.tx
        return rx, tx
