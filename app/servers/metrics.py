"""Server metrics in their own SQLite file, away from the main database's
writer lock. Live samples stay in memory; one averaged row per server per
minute is written, and hourly rows are rolled up from those, daily traffic
from the hours. Partitions are sampled hourly; latency comes per minute."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

# Days kept (0: for ever). Minute-level rows (metrics, latency) and hour-level ones (metrics, latency,
# partitions) each have their own; daily traffic is never dropped.
RETENTION = {'minuteDays': 31, 'hourDays': 400}
HOUR_DAYS_MIN = 31
MINUTE_TABLES = ('minute', 'ping_minute')
HOUR_TABLES = ('hour', 'ping_hour', 'disk')
# Latency is the bulk of the file: its rows name the server by a small number and keep tenths of a
# millisecond as integers.
PING_TABLES = ('ping_minute', 'ping_hour')
# meta: what other modules keep of one server, gone with it; and their lookups kept by what they are
# about (bgp.py, ip_intel.py), with when they were fetched, dropped CACHE_KEEP_S after that.
SERVER_META = ('ipcheck:', 'intel:ports:')
CACHE_KEEP_S = 30 * 86400
CACHE_META = ((('bgp:', 'bgp-ip:', 'asname:'), 'atMs', lambda s: s * 1000), (('intel:',), 'at', lambda s: s))
# Pages handed back to the file system per step, between which other writers get the lock.
VACUUM_STEP = 2048
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
CREATE TABLE IF NOT EXISTS day (
 server_id TEXT NOT NULL, day TEXT NOT NULL, ts INTEGER NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL,
 PRIMARY KEY(server_id, day)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS disk (
 server_id TEXT NOT NULL, ts INTEGER NOT NULL, mount TEXT NOT NULL, fs TEXT NOT NULL, total INTEGER NOT NULL, used INTEGER NOT NULL,
 PRIMARY KEY(server_id, ts, mount)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ping_ids (sid INTEGER PRIMARY KEY, server_id TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS ping_minute (
 sid INTEGER NOT NULL, ts INTEGER NOT NULL, target TEXT NOT NULL, sent INTEGER NOT NULL, lost INTEGER NOT NULL,
 avg INTEGER, min INTEGER, max INTEGER, PRIMARY KEY(sid, ts, target)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ping_hour (
 sid INTEGER NOT NULL, ts INTEGER NOT NULL, target TEXT NOT NULL, sent INTEGER NOT NULL, lost INTEGER NOT NULL,
 avg INTEGER, min INTEGER, max INTEGER, PRIMARY KEY(sid, ts, target)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''
# range -> (table, bucket seconds, span seconds); every range comes back as a few hundred points at most.
RANGES = {'1h': ('minute', 60, 3600), '24h': ('minute', 300, 86400), '7d': ('hour', 3600, 7 * 86400),
          '30d': ('hour', 4 * 3600, 30 * 86400), '90d': ('hour', 12 * 3600, 90 * 86400), '1y': ('hour', 2 * 86400, 365 * 86400)}
# Partition usage over time: hourly samples, bucketed the same way; all of it in at most ~400 points.
DISK_RANGES = {'7d': (3600 * 2, 7 * 86400), '30d': (4 * 3600, 30 * 86400), '90d': (12 * 3600, 90 * 86400), 'all': (0, 0)}
DISK_POINTS = 400
# Traffic by hour, day and month: how many of each, the current one counting so far.
TRAFFIC_UNITS = {'hour': 48, 'day': 31, 'month': 12}
# What a stored value may be (hub.SAMPLE_CAP): a minute's sums of these, an hour's of those and the
# weighted averages all stay within SQLite's 64-bit integers.
SAMPLE_CAP = 2 ** 53
MINUTE_TRAFFIC_CAP = SAMPLE_CAP
# Errors that come from one row's values: that row is dropped, the others are written.
BAD_ROW = (OverflowError, ValueError, TypeError, sqlite3.DataError, sqlite3.IntegrityError)
# Closing allows 10 seconds for the current writer, 30 for draining, then 10 for closing SQLite.
CLOSE_WAIT = 10
CLOSE_DRAIN = 30
# How Bucket.row rounds each average, for merging a bucket into a minute already written.
ROUNDED = {'cpu': 'ROUND({},2)', 'load': 'ROUND({},2)', 'steal': 'ROUND({},2)', 'iowait': 'ROUND({},2)',
           'tcp': 'CAST(ROUND({}) AS INTEGER)', 'udp': 'CAST(ROUND({}) AS INTEGER)', 'proc': 'CAST(ROUND({}) AS INTEGER)'}
# A bucket holds only traffic counted since the one before it, so one for a minute already written (the controller
# restarted within it) adds to that minute; each bucket is committed once, a failed one rolled back and retried.
WRITE_MINUTE = (f'INSERT INTO minute(server_id,ts,samples,{",".join(COLUMNS[:10])},rx,tx,{",".join(COLUMNS[10:])}) '
                f'VALUES({",".join("?" * (len(COLUMNS) + 5))}) ON CONFLICT(server_id,ts) DO UPDATE SET samples=samples+excluded.samples,'
                + ','.join(f'{c}=' + ROUNDED.get(c, 'CAST({} AS INTEGER)').format(f'(1.0*{c}*samples+1.0*excluded.{c}*excluded.samples)/(samples+excluded.samples)')
                           for c in COLUMNS)
                + f',rx=MIN(rx+excluded.rx,{MINUTE_TRAFFIC_CAP}),tx=MIN(tx+excluded.tx,{MINUTE_TRAFFIC_CAP})')


def _bad_row(exc):
    return isinstance(exc, BAD_ROW) or isinstance(exc, sqlite3.OperationalError) and 'overflow' in str(exc)


def _cap(value):
    return min(max(value, 0), SAMPLE_CAP)


def clean_retention(data, prior=None):
    """{minuteDays, hourDays}: whole days, 0 for ever. Minute-level rows are kept 7 days at least; hour-level
    ones 31 at least and never shorter than the minutes (both for ever, or the hours at least as long)."""
    value = {**RETENTION, **(prior or {})}
    # A period's traffic is summed from the hours (traffic_since); a shorter one saved before this limit counts as it.
    if type(value['hourDays']) is int and 0 < value['hourDays'] < HOUR_DAYS_MIN:
        value['hourDays'] = HOUR_DAYS_MIN
    if not isinstance(data, dict) or set(data) - set(RETENTION):
        raise ValueError('invalid_retention')
    for key in data:
        days = data[key]
        if type(days) is not int or days < 0 or days > 36500:
            raise ValueError('invalid_retention')
        if key == 'hourDays' and 0 < days < HOUR_DAYS_MIN:
            raise ValueError('retention_hours_too_short')
        if 0 < days < 7:
            raise ValueError('invalid_retention')
        value[key] = days
    minute, hour = value['minuteDays'], value['hourDays']
    if hour and (not minute or hour < minute):
        raise ValueError('retention_hours_shorter')
    return value


def _month_add(first, months):
    """The first day of the month `months` after the one `first` (a month's first day) is in."""
    index = first.year * 12 + first.month - 1 + months
    return dt.date(index // 12, index % 12 + 1, 1)


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
        # Set by the service from its settings: the day boundaries daily traffic uses, and the days kept.
        self.zone = ZoneInfo('UTC')
        self.retention = dict(RETENTION)
        self.maintaining = None
        self.pruning = asyncio.Lock()

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
        self.maintaining = asyncio.create_task(self._upgrade(), name='server-metrics-upgrade')

    async def _upgrade(self):
        """Once per file, in the background: daily traffic from the hours kept before it existed, and the file
        switched to giving freed pages back in steps (one VACUUM, holding writers for its length)."""
        with contextlib.suppress(Exception):
            if not await self.meta('days'):
                await self._backfill_days()
                await self.set_meta('days', '1')
            async with self.lock:
                async with self.conn.execute('PRAGMA auto_vacuum') as cursor:
                    mode = (await cursor.fetchone())[0]
                if mode != 2:
                    await self.conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
                    await self.conn.execute('VACUUM')

    async def meta(self, key):
        async with self.conn.execute('SELECT value FROM meta WHERE key=?', (key,)) as cursor:
            row = await cursor.fetchone()
        return row['value'] if row else None

    async def set_meta(self, key, value):
        async with self.lock:
            await self.conn.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, value))
            await self.conn.commit()

    async def _backfill_days(self):
        async with self.conn.execute('SELECT server_id, ts FROM hour') as cursor:
            hours = [(r['server_id'], r['ts']) async for r in cursor]
        days = {}
        for server_id, ts in hours:
            days.setdefault((server_id, self._day(ts)[0]), ts)
        for (server_id, _), ts in days.items():
            async with self.lock:
                await self._rollup_day(server_id, ts)
                await self.conn.commit()
            await asyncio.sleep(0)

    async def close(self):
        draining = asyncio.create_task(self._drain())
        try:
            done, _ = await asyncio.wait({draining}, timeout=CLOSE_DRAIN)
            if done:
                await draining
            else:
                logging.getLogger(__name__).warning('Timed out draining server metrics; unwritten samples may remain')
        finally:
            if not draining.done():
                draining.cancel()
            for name in ('flusher', 'maintaining'):
                task = getattr(self, name)
                if task:
                    task.cancel()
            if self.conn:
                conn, self.conn = self.conn, None
                try:
                    await asyncio.wait_for(conn.close(), CLOSE_WAIT)
                except TimeoutError:
                    logging.getLogger(__name__).warning('Timed out closing server metrics SQLite connection')

    async def _drain(self):
        # Cancel between writers so a batch already removed from pending finishes first.
        locked = False
        with contextlib.suppress(asyncio.TimeoutError):
            locked = await asyncio.wait_for(self.lock.acquire(), CLOSE_WAIT)
        try:
            for name in ('flusher', 'maintaining'):
                task = getattr(self, name)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    setattr(self, name, None)
        finally:
            if locked:
                self.lock.release()
        if self.conn:
            for server_id, bucket in list(self.buckets.items()):
                if bucket.samples:
                    self.pending.append((server_id, bucket))
            self.buckets.clear()
            with contextlib.suppress(Exception):
                await self.flush()

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
                committing = None
                try:
                    await self.conn.execute(WRITE_MINUTE, bucket.row(server_id))
                    # The checkpoint matches exactly the traffic already written.
                    boot, rx, tx = bucket.counters
                    await self.conn.execute('INSERT OR REPLACE INTO counters VALUES(?,?,?,?,?)', (server_id, boot, rx, tx, bucket.ts))
                    await self._rollup(server_id, bucket.ts - bucket.ts % 3600)
                    await self._rollup_day(server_id, bucket.ts)
                    committing = asyncio.create_task(self.conn.commit())
                    await asyncio.shield(committing)
                except asyncio.CancelledError:
                    # Cancelling the await does not stop SQLite's worker. A commit already queued must be
                    # resolved before deciding whether this minute belongs in the retry batch.
                    if committing is not None:
                        try:
                            await committing
                        except Exception:
                            pass
                        else:
                            index += 1
                    self.pending = batch[index:] + self.pending
                    with contextlib.suppress(Exception):
                        await self.conn.rollback()
                    raise
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

    def _day(self, ts):
        """The local day an hour falls in: its date and its bounds as unix times."""
        # A fractional-offset midnight belongs to its whole UTC hour. Use that hour's
        # end to choose its date too, so minutes before midnight cannot count it twice.
        hour = ts - ts % 3600
        date = dt.datetime.fromtimestamp(hour + 3599, self.zone).date()
        start = int(dt.datetime.combine(date, dt.time(), self.zone).timestamp())
        end = int(dt.datetime.combine(date + dt.timedelta(days=1), dt.time(), self.zone).timestamp())
        return date.isoformat(), start - start % 3600, end - end % 3600

    async def _rollup_day(self, server_id, ts):
        # Only the day being written is summed again: one that has ended keeps its total.
        day, start, end = self._day(ts)
        await self.conn.execute('INSERT OR REPLACE INTO day(server_id,day,ts,rx,tx) SELECT ?,?,?,CAST(TOTAL(rx) AS INTEGER),CAST(TOTAL(tx) AS INTEGER) '
                                'FROM hour WHERE server_id=? AND ts>=? AND ts<?', (server_id, day, start, server_id, start, end))

    def prune_soon(self):
        """After the days kept changed: the next round of the flush loop prunes."""
        self.last_prune = 0

    async def prune(self, now=None):
        """Rows past their days, a server and a day of rows per statement, the lock let go between them so
        writes are never held up for long; then the freed pages go back to the file system."""
        async with self.pruning:
            await self._prune(now)

    async def _prune(self, now):
        now = int(now or time.time())
        keep = {**{t: self.retention['minuteDays'] for t in MINUTE_TABLES}, **{t: self.retention['hourDays'] for t in HOUR_TABLES}}
        async with self.conn.execute('SELECT server_id FROM counters UNION SELECT DISTINCT server_id FROM hour') as cursor:
            servers = [r[0] async for r in cursor]
        async with self.conn.execute('SELECT sid FROM ping_ids') as cursor:
            sids = [r[0] async for r in cursor]
        for table, days in keep.items():
            if not days:
                continue
            cutoff = now - days * 86400
            column, keys = ('sid', sids) if table in PING_TABLES else ('server_id', servers)
            for key in keys:
                async with self.conn.execute(f'SELECT MIN(ts) FROM {table} WHERE {column}=?', (key,)) as cursor:
                    oldest = (await cursor.fetchone())[0]
                while oldest is not None and oldest < cutoff:
                    upto = min(cutoff, oldest + 86400)
                    async with self.lock:
                        await self.conn.execute(f'DELETE FROM {table} WHERE {column}=? AND ts<?', (key, upto))
                        await self.conn.commit()
                    oldest = upto if upto < cutoff else None
                    await asyncio.sleep(0)
        async with self.lock:
            for keys, field, cutoff in CACHE_META:
                like = ' OR '.join('key LIKE ?' for _ in keys)
                await self.conn.execute(f"DELETE FROM meta WHERE ({like}) AND key NOT LIKE 'intel:ports:%' "
                                        f"AND COALESCE(CASE WHEN json_valid(value) THEN json_extract(value, '$.{field}') END, 0) < ?",
                                        [k + '%' for k in keys] + [cutoff(now - CACHE_KEEP_S)])
            await self.conn.commit()
        await self.compact()

    async def compact(self):
        """Hand the file's free pages back, a step at a time."""
        while True:
            async with self.lock:
                async with self.conn.execute('PRAGMA auto_vacuum') as cursor:
                    if (await cursor.fetchone())[0] != 2:
                        return
                async with self.conn.execute('PRAGMA freelist_count') as cursor:
                    free = (await cursor.fetchone())[0]
                if not free:
                    return
                # Each page freed is a row of the pragma's result: it runs only as far as it is read.
                async with self.conn.execute(f'PRAGMA incremental_vacuum({min(free, VACUUM_STEP)})') as cursor:
                    await cursor.fetchall()
                await self.conn.commit()
            await asyncio.sleep(0)

    async def storage(self):
        """The file's size and, per kind of data, its rows, bytes and the days it spans now."""
        async with self.conn.execute('PRAGMA page_size') as cursor:
            page = (await cursor.fetchone())[0]
        async with self.conn.execute('PRAGMA page_count') as cursor:
            pages = (await cursor.fetchone())[0]
        async with self.conn.execute('PRAGMA freelist_count') as cursor:
            free = (await cursor.fetchone())[0]
        sizes = {}
        with contextlib.suppress(sqlite3.Error):
            async with self.conn.execute('SELECT name, SUM(pgsize) FROM dbstat GROUP BY name') as cursor:
                sizes = {r[0]: r[1] async for r in cursor}
        tables = {}
        for table in (*MINUTE_TABLES, *HOUR_TABLES, 'day'):
            async with self.conn.execute(f'SELECT COUNT(*), MIN(ts), MAX(ts) FROM {table}') as cursor:
                rows, first, last = await cursor.fetchone()
            tables[table] = {'rows': rows, 'bytes': sizes.get(table, 0), 'days': round((last - first) / 86400, 2) if rows else 0}
        return {'bytes': page * pages, 'freeBytes': page * free, 'tables': tables}

    async def write_disks(self, server_id, partitions, now=None):
        """One hourly sample of a server's partitions ({path, fs, total, used})."""
        now = int(now or time.time())
        ts = now - now % 3600
        rows = [(server_id, ts, str(p['path'])[:255], str(p.get('fs') or '')[:32], _cap(int(p['total'])), _cap(int(p['used'])))
                for p in partitions if isinstance(p, dict) and p.get('path') and int(p.get('total') or 0) > 0]
        async with self.lock:
            await self.conn.execute('DELETE FROM disk WHERE server_id=? AND ts=?', (server_id, ts))
            await self.conn.executemany('INSERT OR REPLACE INTO disk VALUES(?,?,?,?,?,?)', rows)
            await self.conn.commit()

    async def disk_sampled(self, server_id):
        async with self.conn.execute('SELECT MAX(ts) FROM disk WHERE server_id=?', (server_id,)) as cursor:
            return (await cursor.fetchone())[0]

    async def disks(self, server_id, span, now=None):
        """The latest partitions and each one's used share over a span, bucketed to a few hundred points."""
        step, length = DISK_RANGES[span]
        now = int(now or time.time())
        start = now - length if length else 0
        latest = await self.disk_sampled(server_id)
        current = []
        if latest is not None:
            async with self.conn.execute('SELECT mount, fs, total, used FROM disk WHERE server_id=? AND ts=? ORDER BY mount', (server_id, latest)) as cursor:
                current = [{'path': r['mount'], 'fs': r['fs'], 'total': r['total'], 'used': r['used']} async for r in cursor]
        if not step:
            async with self.conn.execute('SELECT MIN(ts) FROM disk WHERE server_id=?', (server_id,)) as cursor:
                first = (await cursor.fetchone())[0] or now
            step = max(3600, ((now - first) // DISK_POINTS + 3599) // 3600 * 3600)
            start = first
        t, series = [], {p['path']: [] for p in current}
        sql = (f'SELECT ts/{step}*{step} AS t, mount, AVG(used*100.0/total) AS pct FROM disk WHERE server_id=? AND ts>=? '
               'GROUP BY t, mount ORDER BY t')
        index = {}
        async with self.conn.execute(sql, (server_id, start)) as cursor:
            async for r in cursor:
                if r['mount'] not in series:
                    continue
                if r['t'] not in index:
                    index[r['t']] = len(t)
                    t.append(r['t'])
                    for values in series.values():
                        values.append(None)
                series[r['mount']][index[r['t']]] = round(r['pct'], 2)
        return {'range': span, 'step': step, 'sampledAt': latest, 'partitions': current, 't': t, 'series': series}

    async def traffic_stats(self, server_id, unit, now=None):
        """Traffic per hour (last 48), day (last 31) or month (last 12), oldest first; the last one is the
        current one so far. Hours come from the hourly table, days and months from the daily one."""
        count = TRAFFIC_UNITS[unit]
        now = int(now or time.time())
        points = []
        if unit == 'hour':
            start = now - now % 3600 - (count - 1) * 3600
            async with self.conn.execute('SELECT ts, rx, tx FROM hour WHERE server_id=? AND ts>=? ORDER BY ts', (server_id, start)) as cursor:
                stored = {r['ts']: (r['rx'], r['tx']) async for r in cursor}
            for i in range(count):
                ts = start + i * 3600
                rx, tx = stored.get(ts, (0, 0))
                points.append({'t': ts, 'label': '', 'rx': rx, 'tx': tx})
        else:
            today = dt.date.fromisoformat(self._day(now)[0])
            first = today - dt.timedelta(days=count - 1) if unit == 'day' else _month_add(today.replace(day=1), -(count - 1))
            async with self.conn.execute('SELECT day, rx, tx FROM day WHERE server_id=? AND day>=? ORDER BY day', (server_id, first.isoformat())) as cursor:
                rows = [(r['day'], r['rx'], r['tx']) async for r in cursor]
            keys = [(first + dt.timedelta(days=i)).isoformat() for i in range(count)] if unit == 'day' \
                else [_month_add(first, i).isoformat()[:7] for i in range(count)]
            sums = {k: [0, 0] for k in keys}
            for day, rx, tx in rows:
                key = day if unit == 'day' else day[:7]
                if key in sums:
                    sums[key][0] += rx
                    sums[key][1] += tx
            for key in keys:
                date = dt.date.fromisoformat(key if unit == 'day' else key + '-01')
                boundary = int(dt.datetime.combine(date, dt.time(), self.zone).timestamp())
                points.append({'t': boundary - boundary % 3600, 'label': key, 'rx': sums[key][0], 'tx': sums[key][1]})
        # Still in memory: the minute being filled and minutes not yet written.
        current = points[-1]
        for sid, bucket in [*self.pending, *((k, v) for k, v in self.buckets.items())]:
            if sid == server_id and bucket.ts >= current['t']:
                current['rx'] += bucket.rx
                current['tx'] += bucket.tx
        return {'unit': unit, 'points': points}

    async def _sid(self, server_id, create=False):
        """The small number latency rows name a server by; None when it has none and create is off."""
        async with self.conn.execute('SELECT sid FROM ping_ids WHERE server_id=?', (server_id,)) as cursor:
            row = await cursor.fetchone()
        if row or not create:
            return row[0] if row else None
        async with self.conn.execute('INSERT INTO ping_ids(server_id) VALUES(?)', (server_id,)) as cursor:
            return cursor.lastrowid

    async def write_pings(self, server_id, rows):
        """A node's minutes of latency: {ts, target, sent, lost, avg, min, max} (ms), rolled up into their hours."""
        clean = []
        for r in rows:
            try:
                sent, lost = int(r['sent']), int(r['lost'])
                # 20 probes a minute at the shortest interval; a node claiming far more would overflow the sums.
                if not 0 < sent <= 120 or not 0 <= lost <= sent:
                    continue
                stat = [None if r.get(k) is None or lost == sent else round(min(max(float(r[k]), 0), 600000) * 10) for k in ('avg', 'min', 'max')]
                ts = int(r['ts'])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            clean.append((ts - ts % 60, str(r['target'])[:64], sent, lost, *stat))
        if not clean:
            return
        async with self.lock:
            try:
                sid = await self._sid(server_id, create=True)
                await self.conn.executemany('INSERT OR REPLACE INTO ping_minute VALUES(?,?,?,?,?,?,?,?)', [(sid, *row) for row in clean])
                for hour, target in {(row[0] - row[0] % 3600, row[1]) for row in clean}:
                    await self.conn.execute(
                        'INSERT OR REPLACE INTO ping_hour SELECT sid,?,target,SUM(sent),SUM(lost),'
                        'ROUND(TOTAL(avg*(sent-lost))/NULLIF(SUM(CASE WHEN avg IS NULL THEN 0 ELSE sent-lost END),0)),MIN(min),MAX(max) '
                        'FROM ping_minute WHERE sid=? AND target=? AND ts>=? AND ts<? GROUP BY sid,target',
                        (hour, sid, target, hour, hour + 3600))
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def pings(self, server_id, span, now=None):
        """Each target's latency (ms) and loss (%) over a range, bucketed as the metric ranges are."""
        table, step, length = RANGES[span]
        table = 'ping_minute' if table == 'minute' else 'ping_hour'
        now = int(now or time.time())
        start = now - length
        sql = (f'SELECT ts/{step}*{step} AS t, target, SUM(sent) AS sent, SUM(lost) AS lost, '
               'TOTAL(avg*(sent-lost))/NULLIF(SUM(CASE WHEN avg IS NULL THEN 0 ELSE sent-lost END),0)/10 AS avg '
               f'FROM {table} WHERE sid=? AND ts>=? GROUP BY t, target ORDER BY t')
        t, index, targets = [], {}, {}
        async with self.conn.execute(sql, (await self._sid(server_id), start)) as cursor:
            async for r in cursor:
                if r['t'] not in index:
                    index[r['t']] = len(t)
                    t.append(r['t'])
                    for v in targets.values():
                        v['avg'].append(None)
                        v['loss'].append(None)
                if r['target'] not in targets:
                    targets[r['target']] = {'avg': [None] * len(t), 'loss': [None] * len(t)}
                i = index[r['t']]
                targets[r['target']]['avg'][i] = round(r['avg'], 1) if r['avg'] is not None else None
                targets[r['target']]['loss'][i] = round(r['lost'] * 100 / r['sent'], 1) if r['sent'] else None
        return {'range': span, 'step': step, 'start': start, 'end': now, 't': t, 'targets': targets}

    async def ping_summary(self, server_ids, minutes=30, now=None, wanted=None):
        """Per server, its last `minutes` minutes over its targets (those wanted names for it, else all): one
        cell per minute (average latency, loss share), and the last ten minutes' average and loss for the card."""
        now = int(now or time.time())
        end = now - now % 60
        start = end - minutes * 60
        out, wanted = {}, wanted or {}
        owned = set(server_ids)
        async with self.conn.execute('SELECT sid, server_id FROM ping_ids') as cursor:
            names = {r['sid']: r['server_id'] async for r in cursor if r['server_id'] in owned}
        ids = list(names)
        # (server, minute) -> [sent, lost, latency × answered, answered with a latency]
        sums = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            sql = (f'SELECT sid, ts, target, sent, lost, avg FROM ping_minute WHERE sid IN ({",".join("?" * len(chunk))}) AND ts>=? AND ts<?')
            async with self.conn.execute(sql, (*chunk, start, end)) as cursor:
                async for r in cursor:
                    server_id = names[r['sid']]
                    if server_id in wanted and r['target'] not in wanted[server_id]:
                        continue
                    s = sums.setdefault((server_id, r['ts']), [0, 0, 0.0, 0])
                    s[0] += r['sent']
                    s[1] += r['lost']
                    if r['avg'] is not None:
                        s[2] += r['avg'] * (r['sent'] - r['lost'])
                        s[3] += r['sent'] - r['lost']
        for (server_id, ts), (sent, lost, total, answered) in sums.items():
            cells = out.setdefault(server_id, {'cells': [None] * minutes})['cells']
            cells[(ts - start) // 60] = [round(total / answered / 10, 1) if answered else None, round(lost * 100 / sent, 1) if sent else None, sent, lost]
        for summary in out.values():
            recent = [c for c in summary['cells'][-10:] if c]
            sent, lost = sum(c[2] for c in recent), sum(c[3] for c in recent)
            got = [(c[0], c[2] - c[3]) for c in recent if c[0] is not None]
            weight = sum(n for _, n in got)
            summary['avg'] = round(sum(a * n for a, n in got) / weight, 1) if weight else None
            summary['loss'] = round(lost * 100 / sent, 1) if sent else None
            summary['cells'] = [c[:2] if c else None for c in summary['cells']]
        return {'start': start, 'step': 60, 'items': out}

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
            for table in ('minute', 'hour', 'counters', 'day', 'disk'):
                await self.conn.execute(f'DELETE FROM {table} WHERE server_id=?', (server_id,))
            await self.conn.execute(f'DELETE FROM meta WHERE key IN ({",".join("?" * len(SERVER_META))})', [p + server_id for p in SERVER_META])
            sid = await self._sid(server_id)
            if sid is not None:
                for table in PING_TABLES:
                    await self.conn.execute(f'DELETE FROM {table} WHERE sid=?', (sid,))
                await self.conn.execute('DELETE FROM ping_ids WHERE sid=?', (sid,))
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
        """Bytes received/sent since a unix time: the hours from the hourly table (each one rolled up
        with every minute written, the current one too, and kept at least as long as the minutes), the
        minutes only before the first whole hour, plus what is still in memory."""
        hour = start - start % 3600 + (3600 if start % 3600 else 0)
        rx = tx = 0
        # Summed as TOTAL: a month of a node's absurd counters must not overflow the listing.
        if hour > start:
            async with self.conn.execute('SELECT TOTAL(rx),TOTAL(tx) FROM minute WHERE server_id=? AND ts>=? AND ts<?', (server_id, start, hour)) as c:
                a, b = await c.fetchone()
                rx, tx = rx + int(a), tx + int(b)
        async with self.conn.execute('SELECT TOTAL(rx),TOTAL(tx) FROM hour WHERE server_id=? AND ts>=?', (server_id, hour)) as c:
            a, b = await c.fetchone()
            rx, tx = rx + int(a), tx + int(b)
        for sid, bucket in [*self.pending, *((k, v) for k, v in self.buckets.items())]:
            if sid == server_id and bucket.ts >= start:
                rx, tx = rx + bucket.rx, tx + bucket.tx
        return rx, tx
