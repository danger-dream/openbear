"""Mesh lines: relay-to-relay links, each entry candidate of a member (active, standby and cold
alike) and punched or dialed direct links between members. What each looks like now, pieced
together from both ends' reports, and its long-term stability, kept like mesh_trend in the
metrics file: one row per line per minute (2 days), hourly rows rolled up from those (30 days).

Line ids, shared with the page and the nodes' agents:
  relay:<smaller serverId>|<larger serverId>
  entry:<member serverId>|<relay serverId>|<transport>
  direct:<smaller serverId>|<larger serverId>
Candidate ids (c1 ...) are not used: they are reused when candidates change.

Only what nodes measure anyway is recorded: a relay link by its ends' routing links, an entry
candidate by its member (a cold one by its periodic checks, nothing new is probed), a direct
link by its ends' peer records. One end samples a line, so nothing is counted twice: the member
for its entries, else the smaller id while its reports are fresh, the other end otherwise."""
from __future__ import annotations

import re

from app.servers.mesh_status import direct_up, entry_rtt, fresh_status, records

MINUTE_RETENTION = 2 * 86400
HOUR_RETENTION = 30 * 86400
# range -> (table, bucket seconds, span seconds); at most about 120 points.
RANGES = {'1h': ('mesh_line_minute', 60, 3600), '24h': ('mesh_line_minute', 720, 86400),
          '3d': ('mesh_line_hour', 3600, 3 * 86400), '7d': ('mesh_line_hour', 7200, 7 * 86400),
          '15d': ('mesh_line_hour', 14400, 15 * 86400), '30d': ('mesh_line_hour', 21600, 30 * 86400)}
ROLES = ('active', 'standby', 'cold')
UNKNOWN_ROLE = 3
# A sampling end's reports must be this recent for the other end to leave the line to it.
SAMPLE_FRESH_MS = 30_000
# The page's view of a line: reports older than this no longer describe it.
VIEW_FRESH_MS = 150_000
TRANSPORTS = ('udp', 'tcp', 'tls')
_SID = r'[A-Za-z0-9._-]{1,64}'
LINE_ID = re.compile(rf'(relay|direct):({_SID})\|({_SID})|entry:({_SID})\|({_SID})\|(udp|tcp|tls)')
COLUMNS = ('samples', 'rtt', 'rtt_n', 'rtt_min', 'rtt_max', 'loss', 'loss_n', 'up_s', 'known_s', 'downs', 'role')
# A line id holds two server ids: it is stored once in mesh_line, the rows carry its integer key
# (mesh_line.id); a, b and kind there find a server's lines without reading the rows.
_TABLE = ('(line INTEGER NOT NULL, ts INTEGER NOT NULL, samples INTEGER NOT NULL, rtt REAL, rtt_n INTEGER NOT NULL, rtt_min REAL, '
          'rtt_max REAL, loss REAL, loss_n INTEGER NOT NULL, up_s INTEGER NOT NULL, known_s INTEGER NOT NULL, downs INTEGER NOT NULL, '
          'role INTEGER NOT NULL, PRIMARY KEY(line, ts)) WITHOUT ROWID')
SCHEMA = ('CREATE TABLE IF NOT EXISTS mesh_line(id INTEGER PRIMARY KEY, line TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, a TEXT NOT NULL, '
          'b TEXT NOT NULL); CREATE INDEX IF NOT EXISTS mesh_line_a ON mesh_line(a, kind); CREATE INDEX IF NOT EXISTS mesh_line_b ON mesh_line(b, kind); '
          f'CREATE TABLE IF NOT EXISTS mesh_line_minute {_TABLE}; CREATE TABLE IF NOT EXISTS mesh_line_hour {_TABLE};')
# Tables of the first layout, keyed by the line id's text: dropped, not converted.
_OLD_LAYOUT = "SELECT 1 FROM pragma_table_info('mesh_line_minute') WHERE name='line' AND upper(type)!='INTEGER'"
_DROP = 'DROP TABLE IF EXISTS mesh_line_minute; DROP TABLE IF EXISTS mesh_line_hour; DROP TABLE IF EXISTS mesh_line;'
_AGGREGATE = ('SUM(samples) AS samples, SUM(rtt*rtt_n)/NULLIF(SUM(rtt_n),0) AS rtt, SUM(rtt_n) AS rtt_n, MIN(rtt_min) AS rtt_min, '
              'MAX(rtt_max) AS rtt_max, SUM(loss*loss_n)/NULLIF(SUM(loss_n),0) AS loss, SUM(loss_n) AS loss_n, SUM(up_s) AS up_s, '
              'SUM(known_s) AS known_s, SUM(downs) AS downs, MIN(role) AS role')


def parse_id(line_id):
    """(kind, a, b, transport) of a well-formed line id, else None. transport is '' for relay and direct ids."""
    match = LINE_ID.fullmatch(line_id or '')
    if not match:
        return None
    if match.group(1):
        kind, a, b = match.group(1), match.group(2), match.group(3)
        return (kind, a, b, '') if a < b else None
    return 'entry', match.group(4), match.group(5), match.group(6)


def _parts(line_id):
    """(kind, a, b) of a line id as stored in mesh_line."""
    parsed = parse_id(line_id)
    if parsed:
        return parsed[:3]
    kind, _, rest = line_id.partition(':')
    a, _, rest = rest.partition('|')
    return kind, a, rest.partition('|')[0]


def pair_id(kind, x, y):
    a, b = sorted((x, y))
    return f'{kind}:{a}|{b}'


def _scheme(endpoint):
    scheme, sep, _ = (endpoint or '').partition('://')
    return scheme if sep and scheme in TRANSPORTS else ''


def _strip(endpoint):
    return (endpoint or '').partition('://')[2] or (endpoint or '')


# ----- which lines there are -----
def catalogue(doc, configs):
    """[line] of the applied document as its configs run it, in a stable order: relay links,
    then each member's entry candidates in its order, then direct links. A line is {id, kind,
    a, b, transport, punch, dial {a, b}, endpoint {a, b}: what each end dials ('' when it
    waits)}; an entry also has candidate (its id now), mode and order (its place)."""
    live = {m['serverId']: m for m in doc['members'] if not m['removing']}
    out = {}

    def links(kind):
        for sid in sorted(configs):
            if sid not in live:
                continue
            for link in configs[sid].get('links', []):
                if link.get('kind') == kind and link['peer'] in live:
                    yield sid, link
    for kind in ('relay', 'direct'):
        found = {}
        for sid, link in links(kind):
            line_id = pair_id(kind, sid, link['peer'])
            a, b = sorted((sid, link['peer']))
            line = found.setdefault(line_id, {'id': line_id, 'kind': kind, 'a': a, 'b': b, 'transport': '', 'punch': False,
                                              'dial': {'a': False, 'b': False}, 'endpoint': {'a': '', 'b': ''}})
            end = 'a' if sid == a else 'b'
            line['dial'][end] = bool(link.get('dial'))
            line['endpoint'][end] = _strip(link.get('endpoint', ''))
            line['punch'] = line['punch'] or bool(link.get('punch'))
            line['transport'] = line['transport'] or _scheme(link.get('endpoint', ''))
        if kind == 'direct':
            # A direct link's transport is the document's; a relay link without any endpoint yet is punched or waits on udp.
            for pair in doc['directLinks']:
                line = found.get(pair_id('direct', pair['a'], pair['b']))
                if line:
                    line['transport'] = pair['transport']
        for line in found.values():
            line['transport'] = line['transport'] or 'udp'
            out[line['id']] = line
        if kind == 'relay':
            for m in live.values():
                if m['role'] != 'member':
                    continue
                config = configs.get(m['serverId']) or {}
                written = {c['id']: c for c in (config.get('entry') or {}).get('candidates', [])}
                for order, c in enumerate(m['entry']['candidates']):
                    if live.get(c['relay'], {}).get('role') != 'relay':
                        continue
                    line_id = f"entry:{m['serverId']}|{c['relay']}|{c['transport']}"
                    sent = written.get(c['id']) or {}
                    out.setdefault(line_id, {'id': line_id, 'kind': 'entry', 'a': m['serverId'], 'b': c['relay'], 'transport': c['transport'],
                                             'punch': bool(sent.get('punch')), 'dial': {'a': True, 'b': False},
                                             'endpoint': {'a': _strip(sent.get('endpoint', '')), 'b': ''},
                                             'candidate': c['id'], 'mode': m['entry']['mode'], 'order': order})
    return list(out.values())


# ----- what a line looks like in one report -----
def _entry_roles(status, lines):
    """{line id: role} for one member's entry lines by its report: the role the node gives, else
    active for its active candidate, standby for the one a failover member keeps warm (the first
    other one not failing, as the node picks it) and cold for the rest. A fixed member uses only its
    first candidate; the others are unused."""
    entry = (status or {}).get('entry') or {}
    by_key = {(c['relay'], c['transport']): c for c in entry.get('candidates', [])}
    by_id = {c['id']: c for c in entry.get('candidates', [])}
    out, warm = {}, False
    for line in sorted(lines, key=lambda line: line['order']):
        if line['mode'] == 'fixed' and line['order'] > 0:
            out[line['id']] = 'unused'
            continue
        c = by_key.get((line['b'], line['transport'])) or by_id.get(line['candidate'])
        if c and c.get('role'):
            out[line['id']] = c['role']
            warm = warm or c['role'] == 'standby'
            continue
        active = (c and c['state'] == 'active') or (entry.get('relay') == line['b'] and entry.get('transport') == line['transport']
                                                    and not entry.get('candidates'))
        if active:
            out[line['id']] = 'active'
        elif line['mode'] == 'failover' and not warm and c and c['state'] != 'failing':
            out[line['id']], warm = 'standby', True
        else:
            out[line['id']] = 'cold'
    return out


def measure(line, statuses, now, fresh_ms, roles=None):
    """{reporter, up (True|False|None unknown), rttMs, lossRate, role} from the end that samples
    the line, or None when neither end's report is fresh enough. An entry's also has seen:
    whether the member's report says anything of that candidate."""
    def fresh(sid):
        return fresh_status(statuses, sid, now, fresh_ms)
    if line['kind'] == 'entry':
        status = fresh(line['a'])
        if not status:
            return None
        role = (roles or _entry_roles(status, [line])).get(line['id'], 'cold')
        entry = status.get('entry') or {}
        c = next((c for c in entry.get('candidates', []) if c['relay'] == line['b'] and c['transport'] == line['transport']), None) \
            or next((c for c in entry.get('candidates', []) if c['id'] == line['candidate']), None)
        up = None
        rtt = loss = None
        if c:
            up = True if c['state'] in ('active', 'ready') else False if c['state'] == 'failing' else None
            rtt, loss = c.get('rttMs') or None, c.get('timeoutRate')
        if role == 'active':
            peer = next((p for p in records(status, line['b']) if p['kind'] == 'entry'), None)
            if peer:
                up = peer['active'] if up is None else up
                loss = peer['timeoutRate'] if loss is None else loss
            # Without the candidate's own measurement, the member's entry there as it measures it.
            rtt = rtt or entry_rtt(status, line['b'])
        return {'reporter': line['a'], 'up': up, 'rttMs': rtt, 'lossRate': loss, 'role': role, 'seen': bool(c) or role == 'active'}
    for x, y in ((line['a'], line['b']), (line['b'], line['a'])):
        status = fresh(x)
        if not status:
            continue
        if line['kind'] == 'relay':
            link = next((link for link in ((status.get('routing') or {}).get('links') or []) if link['peer'] == y), None)
            if link:
                return {'reporter': x, 'up': link['state'] == 'up', 'rttMs': link['rttMs'] or None, 'lossRate': link['loss'], 'role': 'active'}
            peer = next((p for p in records(status, y) if p['kind'] == 'relay'), None)
            if peer:
                return {'reporter': x, 'up': peer['active'], 'rttMs': peer['rttMs'] or None if peer['active'] else None,
                        'lossRate': peer['timeoutRate'] if peer['active'] else None, 'role': 'active'}
            return {'reporter': x, 'up': None, 'rttMs': None, 'lossRate': None, 'role': 'active'}
        peer = next((p for p in records(status, y) if p.get('direct') or p['kind'] == 'direct'), None)
        if not peer:
            return {'reporter': x, 'up': None, 'rttMs': None, 'lossRate': None, 'role': 'active'}
        up = direct_up(peer)
        return {'reporter': x, 'up': up, 'rttMs': peer['rttMs'] or None if up else None, 'lossRate': peer['timeoutRate'] if up else None,
                'role': 'active'}
    return None


def _end(line, end, statuses, now, role):
    """One end of a line as its own report shows it: local (the address it is bound to), public
    (its public mapping as STUN or a relay saw it, for udp), remote (where it sends to the other
    end) and dialer (whether it opens the connection). None when it has no fresh report."""
    me, other = (line['a'], line['b']) if end == 'a' else (line['b'], line['a'])
    status = fresh_status(statuses, me, now, VIEW_FRESH_MS)
    if not status:
        return None
    public = ((status.get('reachability') or {}).get('publicAddr') or '') if line['transport'] == 'udp' or line['punch'] else ''
    configured = line['endpoint'][end]
    if line['kind'] == 'entry' and role != 'active':
        # The member's record for the relay is its active path, not this candidate's; the relay has none of it.
        return {'local': '', 'public': public, 'remote': configured, 'dialer': True} if end == 'a' else None
    want = {'relay': ('relay',), 'direct': ('direct', 'member', 'relay'), 'entry': ('entry',) if end == 'a' else ('member',)}[line['kind']]
    found = records(status, other)
    peer = next((p for p in found if p['kind'] in want and p['endpoint']), None) or next((p for p in found if p['endpoint']), None)
    if line['kind'] == 'direct' and peer and not direct_up(peer):
        peer = None  # The record's path is through a relay, not this link.
    # remoteAddr is the address actually in use; endpoint may still be the configured host name.
    remote = (peer.get('remoteAddr') or peer['endpoint']) if peer else configured
    return {'local': peer['localAddr'] if peer else '', 'public': public, 'remote': remote,
            'dialer': line['dial'][end] or line['punch']}


def view(lines, statuses, now):
    """The lines as GET /mesh lists them: {id, kind, a, b, transport, punch, state up|down|standby|cold|unused|unknown,
    role, rttMs, lossRate, ends {a, b}}."""
    out = []
    by_member = {}
    for line in lines:
        if line['kind'] == 'entry':
            by_member.setdefault(line['a'], []).append(line)
    roles = {}
    for sid, mine in by_member.items():
        status = fresh_status(statuses, sid, now, VIEW_FRESH_MS)
        roles.update(_entry_roles(status, mine) if status else {})
    for line in lines:
        sample = measure(line, statuses, now, VIEW_FRESH_MS, roles)
        role = sample['role'] if sample else roles.get(line['id'], '')
        if role == 'unused':
            state = 'unused'
        elif not sample or sample['up'] is None and (line['kind'] != 'entry' or not sample.get('seen')):
            state = 'unknown'
        elif sample['up'] is False:
            state = 'down'
        elif line['kind'] != 'entry' or role == 'active':
            state = 'up' if sample['up'] else 'unknown'
        else:
            state = role
        out.append({'id': line['id'], 'kind': line['kind'], 'a': line['a'], 'b': line['b'], 'transport': line['transport'],
                    'punch': line['punch'], 'state': state, 'role': role if line['kind'] == 'entry' else 'active',
                    'rttMs': sample['rttMs'] if sample else None, 'lossRate': sample['lossRate'] if sample else None,
                    'ends': {end: _end(line, end, statuses, now, role) for end in ('a', 'b')}})
    return out


# ----- the long-term record -----
class Bucket:
    __slots__ = ('ts', 'samples', 'rtt', 'rtt_n', 'rtt_min', 'rtt_max', 'loss', 'loss_n', 'up', 'known', 'downs', 'role')

    def __init__(self, ts):
        self.ts, self.samples, self.rtt, self.rtt_n, self.rtt_min, self.rtt_max = ts, 0, 0.0, 0, None, None
        self.loss, self.loss_n, self.up, self.known, self.downs, self.role = 0.0, 0, 0, 0, 0, UNKNOWN_ROLE

    def row(self, key):
        # A minute where the line's state was known counts whole; up seconds by the share of samples up.
        known = 60 if self.known else 0
        return (key, self.ts, self.samples, self.rtt / self.rtt_n if self.rtt_n else None, self.rtt_n, self.rtt_min, self.rtt_max,
                self.loss / self.loss_n if self.loss_n else None, self.loss_n, round(60 * self.up / self.known) if self.known else 0,
                known, self.downs, self.role)


class LineTrend:
    def __init__(self, metrics):
        self.metrics = metrics
        self.buckets = {}  # line id -> Bucket of its current minute
        self.last_up = {}  # line id -> whether it was up at its last known sample
        self.keys = {}     # line id -> its mesh_line.id, as stored
        self.last_prune = 0

    async def start(self):
        async with self.metrics.lock:
            async with self.metrics.conn.execute(_OLD_LAYOUT) as cursor:
                old = await cursor.fetchone()
            if old:
                await self.metrics.conn.executescript(_DROP)
            await self.metrics.conn.executescript(SCHEMA)
            await self.metrics.conn.commit()

    async def _key(self, line_id, *, create=False):
        """The integer key a line's rows carry; None for a line never stored unless create (under the
        metrics lock, in the writing transaction)."""
        key = self.keys.get(line_id)
        if key is not None:
            return key
        conn = self.metrics.conn
        if create:
            await conn.execute('INSERT OR IGNORE INTO mesh_line(line,kind,a,b) VALUES(?,?,?,?)', (line_id, *_parts(line_id)))
        async with conn.execute('SELECT id FROM mesh_line WHERE line=?', (line_id,)) as cursor:
            row = await cursor.fetchone()
        if row:
            self.keys[line_id] = row['id']
        return row['id'] if row else None

    def samplers(self, lines, reporter, statuses, now):
        """The lines one report samples: a member's entries; a relay or direct link by its
        smaller end while that end's reports are fresh, else by the other."""
        out = []
        for line in lines:
            if reporter not in (line['a'], line['b']):
                continue
            if line['kind'] == 'entry':
                if reporter == line['a']:
                    out.append(line)
                continue
            first = statuses.get(line['a']) or {}
            if reporter == line['a'] or now - first.get('at', 0) > SAMPLE_FRESH_MS:
                out.append(line)
        return out

    async def observe(self, lines, reporter, statuses, now_ms):
        """Fold one report into the minutes of the lines it samples; finished minutes are written."""
        ts = now_ms // 1000 - now_ms // 1000 % 60
        # Finished minutes first, of every line: a line no longer sampled still gets its last one written.
        await self.flush(ts, now_ms // 1000)
        mine = self.samplers(lines, reporter, statuses, now_ms)
        status = (statuses.get(reporter) or {}).get('status')
        roles = _entry_roles(status, [line for line in mine if line['kind'] == 'entry']) if status else {}
        for line in mine:
            sample = measure(line, {reporter: statuses.get(reporter) or {}}, now_ms, SAMPLE_FRESH_MS, roles)
            if not sample or line['kind'] == 'entry' and not sample['seen']:
                continue
            bucket = self.buckets.get(line['id'])
            if bucket is None or bucket.ts != ts:
                if bucket is not None and bucket.ts > ts:
                    continue  # a clock step back: the newer minute stays
                bucket = self.buckets[line['id']] = Bucket(ts)
            bucket.samples += 1
            bucket.role = min(bucket.role, ROLES.index(sample['role']) if sample['role'] in ROLES else UNKNOWN_ROLE)
            if sample['rttMs']:
                bucket.rtt, bucket.rtt_n = bucket.rtt + sample['rttMs'], bucket.rtt_n + 1
                bucket.rtt_min = sample['rttMs'] if bucket.rtt_min is None else min(bucket.rtt_min, sample['rttMs'])
                bucket.rtt_max = sample['rttMs'] if bucket.rtt_max is None else max(bucket.rtt_max, sample['rttMs'])
            if sample['lossRate'] is not None:
                bucket.loss, bucket.loss_n = bucket.loss + sample['lossRate'], bucket.loss_n + 1
            if sample['up'] is not None:
                bucket.known += 1
                bucket.up += bool(sample['up'])
                if self.last_up.get(line['id']) and not sample['up']:
                    bucket.downs += 1
                self.last_up[line['id']] = sample['up']

    async def flush(self, ts, now):
        """Write every minute before ts, of any line."""
        done = [(line_id, b) for line_id, b in self.buckets.items() if b.ts < ts]
        if not done:
            return
        for line_id, _ in done:
            del self.buckets[line_id]
        conn = self.metrics.conn
        async with self.metrics.lock:
            try:
                for line_id, bucket in done:
                    if not bucket.samples:
                        continue
                    key = await self._key(line_id, create=True)
                    hour = bucket.ts - bucket.ts % 3600
                    await conn.execute(f'INSERT OR REPLACE INTO mesh_line_minute(line,ts,{",".join(COLUMNS)}) '
                                       f'VALUES({",".join("?" * (len(COLUMNS) + 2))})', bucket.row(key))
                    await conn.execute(f'INSERT OR REPLACE INTO mesh_line_hour(line,ts,{",".join(COLUMNS)}) SELECT line,?,{_AGGREGATE} '
                                       'FROM mesh_line_minute WHERE line=? AND ts>=? AND ts<? GROUP BY line', (hour, key, hour, hour + 3600))
                if now - self.last_prune > 3600:
                    self.last_prune = now
                    await conn.execute('DELETE FROM mesh_line_minute WHERE ts<?', (now - MINUTE_RETENTION,))
                    await conn.execute('DELETE FROM mesh_line_hour WHERE ts<?', (now - HOUR_RETENTION - 3600,))
                await conn.commit()
            except Exception:
                # A key given out in the rolled back transaction is not stored.
                self.keys.clear()
                await conn.rollback()
                raise

    async def forget(self, server_id):
        """A member gone from the mesh: the lines with it at either end, their rows and keys."""
        for line_id in [line_id for line_id in self.buckets if server_id in _parts(line_id)[1:]]:
            self.buckets.pop(line_id, None)
            self.last_up.pop(line_id, None)
        conn = self.metrics.conn
        async with self.metrics.lock:
            try:
                async with conn.execute('SELECT id,line FROM mesh_line WHERE a=? UNION SELECT id,line FROM mesh_line WHERE b=?',
                                        (server_id, server_id)) as cursor:
                    gone = await cursor.fetchall()
                for r in gone:
                    for table in ('mesh_line_minute', 'mesh_line_hour'):
                        await conn.execute(f'DELETE FROM {table} WHERE line=?', (r['id'],))
                    await conn.execute('DELETE FROM mesh_line WHERE id=?', (r['id'],))
                    self.keys.pop(r['line'], None)
                    self.last_up.pop(r['line'], None)
                await conn.commit()
            except Exception:
                self.keys.clear()
                await conn.rollback()
                raise

    async def recorded(self, line_id, now_ms):
        key = await self._key(line_id)
        if key is not None:
            async with self.metrics.conn.execute('SELECT 1 FROM mesh_line_hour WHERE line=? AND ts>=? LIMIT 1',
                                                 (key, now_ms // 1000 - HOUR_RETENTION - 3600)) as cursor:
                if await cursor.fetchone():
                    return True
        return line_id in self.buckets

    async def peers(self, kind, server_id, start, role=None):
        """The other ends of server_id's lines of one kind (for entry, its own entries) with a
        round trip measured since start; role: only while the line had that role (0 active)."""
        out = []
        for table in ('mesh_line_minute', 'mesh_line_hour'):
            measured = (f'EXISTS(SELECT 1 FROM {table} WHERE line=mesh_line.id AND ts>=? AND rtt_n>0'
                        + (' AND role=?' if role is not None else '') + ')')
            args = (start, *(() if role is None else (role,)))
            # An entry line is the member's (a); a relay or direct line is either end's.
            sql = f'SELECT line,a,b FROM mesh_line WHERE a=? AND kind=? AND {measured}'
            params = (server_id, kind, *args)
            if kind != 'entry':
                sql += f' UNION SELECT line,a,b FROM mesh_line WHERE b=? AND kind=? AND {measured}'
                params += (server_id, kind, *args)
            async with self.metrics.conn.execute(sql + ' ORDER BY line', params) as cursor:
                for r in await cursor.fetchall():
                    out.append(r['b'] if r['a'] == server_id else r['a'])
        return list(dict.fromkeys(out))

    async def series(self, line_id, span, now_ms):
        """(summary, points) over the range: a point per bucket, its values null where nothing was sampled."""
        table, step, length = RANGES[span]
        now = now_ms // 1000
        start = (now - length) // step * step + step
        rows = {}
        key = await self._key(line_id)
        sql = f'SELECT ts/{step}*{step} AS t,{_AGGREGATE} FROM {table} WHERE line=? AND ts>=? GROUP BY t ORDER BY t'
        async with self.metrics.conn.execute(sql, (key, start)) as cursor:
            async for r in cursor:
                rows[r['t']] = dict(r)
        points = []
        for t in range(start, now // step * step + 1, step):
            r = rows.get(t)
            if not r:
                points.append({'at': t * 1000, 'rttMs': None, 'lossRate': None, 'upRatio': None, 'downs': None, 'role': None})
                continue
            points.append({'at': t * 1000, 'rttMs': round(r['rtt'], 1) if r['rtt'] is not None else None,
                           'lossRate': round(r['loss'], 3) if r['loss'] is not None else None,
                           'upRatio': round(r['up_s'] / r['known_s'], 3) if r['known_s'] else None, 'downs': int(r['downs']),
                           'role': ROLES[r['role']] if r['role'] < len(ROLES) else None})
        # The summary from the stored rows themselves (minutes, or hours past two days); P95 of their averages.
        sql = f'SELECT rtt,rtt_n,rtt_min,rtt_max,loss,loss_n,up_s,known_s,downs FROM {table} WHERE line=? AND ts>=?'
        async with self.metrics.conn.execute(sql, (key, start)) as cursor:
            stored = [dict(r) for r in await cursor.fetchall()]
        return summarize(stored), points


def summarize(rows):
    known = sum(r['known_s'] for r in rows)
    up = sum(r['up_s'] for r in rows)
    weights = sum(r['rtt_n'] for r in rows)
    averages = sorted(r['rtt'] for r in rows if r['rtt'] is not None)
    losses = sum(r['loss_n'] for r in rows)
    p95 = averages[min(len(averages) - 1, max(0, -(-95 * len(averages) // 100) - 1))] if averages else None
    return {'availability': round(100 * up / known, 2) if known else None,
            'rttMs': {'avg': round(sum(r['rtt'] * r['rtt_n'] for r in rows if r['rtt'] is not None) / weights, 1) if weights else None,
                      'min': round(min(r['rtt_min'] for r in rows if r['rtt_min'] is not None), 1) if weights else None,
                      'max': round(max(r['rtt_max'] for r in rows if r['rtt_max'] is not None), 1) if weights else None,
                      'p95': round(p95, 1) if p95 is not None else None},
            'lossRate': round(sum(r['loss'] * r['loss_n'] for r in rows if r['loss'] is not None) / losses, 3) if losses else None,
            'downs': sum(r['downs'] for r in rows), 'downSeconds': known - up, 'knownSeconds': known}
