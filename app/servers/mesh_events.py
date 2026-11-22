"""Every event the mesh nodes report, kept RETENTION_S in the metrics database beside the line
trends: the controller holds a node's latest EVENTS_KEPT in memory, and a link flapping fills
those within half an hour."""
from __future__ import annotations

import time

RETENTION_S = 30 * 86400
PRUNE_EVERY_S = 3600
PAGE_DEFAULT, PAGE_MAX = 100, 500

# A node's own event is stored once, by its data plane's start and seq, however often reports
# repeat it; the controller's own (started NULL) each time.
SCHEMA = ('CREATE TABLE IF NOT EXISTS mesh_event_node(id INTEGER PRIMARY KEY, server_id TEXT NOT NULL UNIQUE); '
          'CREATE TABLE IF NOT EXISTS mesh_event(node INTEGER NOT NULL, at INTEGER NOT NULL, started INTEGER, seq INTEGER, '
          'kind TEXT NOT NULL, detail TEXT NOT NULL); '
          'CREATE INDEX IF NOT EXISTS mesh_event_at ON mesh_event(node, at, seq); '
          'CREATE UNIQUE INDEX IF NOT EXISTS mesh_event_seq ON mesh_event(node, started, seq) WHERE started IS NOT NULL;')


class EventLog:
    def __init__(self, metrics):
        self.metrics = metrics
        self.keys = {}  # server id -> its mesh_event_node.id
        self.last_prune = 0

    async def start(self):
        async with self.metrics.lock:
            await self.metrics.conn.executescript(SCHEMA)
            await self.metrics.conn.commit()

    async def _key(self, server_id, *, create=False):
        """The integer key a node's rows carry; None for one never stored unless create (under the
        metrics lock, in the writing transaction)."""
        key = self.keys.get(server_id)
        if key is not None:
            return key
        conn = self.metrics.conn
        if create:
            await conn.execute('INSERT OR IGNORE INTO mesh_event_node(server_id) VALUES(?)', (server_id,))
        async with conn.execute('SELECT id FROM mesh_event_node WHERE server_id=?', (server_id,)) as cursor:
            row = await cursor.fetchone()
        if row:
            self.keys[server_id] = row['id']
        return row['id'] if row else None

    async def add(self, server_id, events, started=None, now=None):
        """Store a node's events ({seq, at, kind, detail}) of the data plane started at started, or
        with started None the controller's own; the expired go once an hour."""
        rows = [(e['at'], started, e.get('seq') if started else None, e['kind'], e['detail']) for e in events]
        if not rows:
            return
        now = int(time.time()) if now is None else now
        conn = self.metrics.conn
        async with self.metrics.lock:
            try:
                key = await self._key(server_id, create=True)
                await conn.executemany('INSERT OR IGNORE INTO mesh_event(node,at,started,seq,kind,detail) VALUES(?,?,?,?,?,?)',
                                       [(key, *row) for row in rows])
                if now - self.last_prune >= PRUNE_EVERY_S:
                    self.last_prune = now
                    await self._prune(now)
                await conn.commit()
            except Exception:
                # A key given out in the rolled back transaction is not stored.
                self.keys.clear()
                await conn.rollback()
                raise

    async def _prune(self, now):
        """Rows older than RETENTION_S, node by node along the index."""
        conn = self.metrics.conn
        async with conn.execute('SELECT id FROM mesh_event_node') as cursor:
            nodes = [row['id'] for row in await cursor.fetchall()]
        for node in nodes:
            await conn.execute('DELETE FROM mesh_event WHERE node=? AND at<?', (node, (now - RETENTION_S) * 1000))

    async def page(self, server_id, before=None, before_seq=None, limit=PAGE_DEFAULT):
        """A node's events newest first, those before (at, seq) when given, and whether there are more."""
        limit = max(1, min(PAGE_MAX, int(limit)))
        async with self.metrics.lock:
            key = await self._key(server_id)
            if key is None:
                return {'events': [], 'more': False}
            sql, args = 'SELECT at, seq, kind, detail FROM mesh_event WHERE node=?', [key]
            if before is not None and before_seq is not None:
                sql += ' AND (at<? OR (at=? AND (seq<? OR seq IS NULL)))'
                args += [before, before, before_seq]
            elif before is not None:
                sql += ' AND at<?'
                args.append(before)
            async with self.metrics.conn.execute(f'{sql} ORDER BY at DESC, seq DESC, rowid DESC LIMIT ?', (*args, limit + 1)) as cursor:
                rows = list(await cursor.fetchall())
            more = len(rows) > limit
            picked = rows[:limit]
            # Controller events share seq=NULL, so a page ending on one must not split same-ms NULL rows across pages.
            if more and picked and picked[-1]['seq'] is None and rows[limit]['seq'] is None and rows[limit]['at'] == picked[-1]['at']:
                tail_at = picked[-1]['at']
                async with self.metrics.conn.execute(
                    f'{sql} AND at=? AND seq IS NULL ORDER BY rowid DESC', (*args, tail_at)
                ) as cursor:
                    same_ms = list(await cursor.fetchall())
                picked = [r for r in picked if not (r['at'] == tail_at and r['seq'] is None)] + same_ms
                async with self.metrics.conn.execute(
                    'SELECT 1 FROM mesh_event WHERE node=? AND at<? LIMIT 1', (key, tail_at)
                ) as cursor:
                    more = await cursor.fetchone() is not None
        events = [{'seq': row['seq'], 'at': row['at'], 'kind': row['kind'], 'detail': row['detail']} for row in picked]
        return {'events': events, 'more': more}

    async def forget(self, server_id):
        """A member gone from the mesh: its events and key."""
        conn = self.metrics.conn
        async with self.metrics.lock:
            try:
                key = await self._key(server_id)
                if key is None:
                    return
                await conn.execute('DELETE FROM mesh_event WHERE node=?', (key,))
                await conn.execute('DELETE FROM mesh_event_node WHERE id=?', (key,))
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
            finally:
                self.keys.pop(server_id, None)
