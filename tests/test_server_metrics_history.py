"""The metrics file's longer history: daily traffic, partitions, latency, the days kept and the file giving space back."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from app.servers import metrics
from app.servers.metrics import MetricsStore, clean_retention

ZONE = ZoneInfo('Asia/Shanghai')
G = 1024 ** 3


def sample(rx, tx, boot=100):
    return {'cpu': 10.0, 'mem': {'total': 1000, 'used': 500}, 'disk': {'total': 100, 'used': 25}, 'load': [1, 0, 0],
            'net': {'rx': rx, 'tx': tx}, 'boot': boot, 'tcp': 3, 'udp': 1, 'processes': 50}


async def started(path):
    store = MetricsStore(path)
    store.zone = ZONE
    await store.start()
    await store.maintaining
    return store


async def minutes(store, server_id, rows):
    """Minutes of traffic, written as the flush loop writes them: [(ts, rx, tx)]."""
    for ts, rx, tx in rows:
        store.observe(server_id, sample(0, 0), ts)
        store.buckets[server_id].rx, store.buckets[server_id].tx = rx, tx
        store.pending.append((server_id, store.buckets.pop(server_id)))
    await store.flush()


def local(y, m, d, h=0, minute=0):
    return int(dt.datetime(y, m, d, h, minute, tzinfo=ZONE).timestamp())


async def test_daily_traffic_follows_the_local_day_and_keeps_ended_days(tmp_path):
    store = await started(tmp_path / 'm.db')
    # 23:30 and 00:10 Beijing time are two local days, though the same UTC day.
    await minutes(store, 's', [(local(2026, 11, 4, 23, 30), 100, 10), (local(2026, 11, 5, 0, 10), 200, 20), (local(2026, 11, 5, 12), 300, 30)])
    async with store.conn.execute('SELECT day, rx, tx FROM day WHERE server_id=? ORDER BY day', ('s',)) as cursor:
        assert [tuple(r) async for r in cursor] == [('2026-11-04', 100, 10), ('2026-11-05', 500, 50)]
    # An ended day keeps its total when the hours behind it are gone.
    await store.conn.execute('DELETE FROM hour WHERE ts<?', (local(2026, 11, 5),))
    await store.conn.commit()
    await minutes(store, 's', [(local(2026, 11, 5, 13), 1, 1)])
    days = await store.traffic_stats('s', 'day', now=local(2026, 11, 5, 14))
    assert len(days['points']) == 31 and days['points'][-1] == {'t': local(2026, 11, 5), 'label': '2026-11-05', 'rx': 501, 'tx': 51}
    assert days['points'][-2]['label'] == '2026-11-04' and days['points'][-2]['rx'] == 100
    months = await store.traffic_stats('s', 'month', now=local(2026, 11, 5, 14))
    assert [p['label'] for p in months['points']][-3:] == ['2026-09', '2026-10', '2026-11']
    assert months['points'][-1]['rx'] == 601 and months['points'][0]['label'] == '2025-12'
    hours = await store.traffic_stats('s', 'hour', now=local(2026, 11, 5, 13, 30))
    assert len(hours['points']) == 48 and hours['points'][-1]['t'] == local(2026, 11, 5, 13) and hours['points'][-1]['rx'] == 1
    # The minute still in memory counts in the current one.
    store.observe('s', sample(0, 0), local(2026, 11, 5, 13, 31))
    store.buckets['s'].rx = 7
    assert (await store.traffic_stats('s', 'hour', now=local(2026, 11, 5, 13, 32)))['points'][-1]['rx'] == 8
    await store.close()


async def test_days_are_worked_out_once_from_the_hours_kept_before(tmp_path):
    store = await started(tmp_path / 'm.db')
    await minutes(store, 's', [(local(2026, 10, 1, 8), 40, 4), (local(2026, 10, 2, 8), 50, 5)])
    await store.conn.execute('DELETE FROM day')
    await store.conn.execute('DELETE FROM meta')
    await store.conn.commit()
    await store.close()
    store = await started(tmp_path / 'm.db')
    async with store.conn.execute('SELECT day, rx FROM day ORDER BY day') as cursor:
        assert [tuple(r) async for r in cursor] == [('2026-10-01', 40), ('2026-10-02', 50)]
    assert await store.meta('days') == '1'
    await store.close()


def test_days_kept_rules():
    assert clean_retention({}) == {'minuteDays': 31, 'hourDays': 400}
    assert clean_retention({'minuteDays': 7}) == {'minuteDays': 7, 'hourDays': 400}
    assert clean_retention({'minuteDays': 0, 'hourDays': 0}) == {'minuteDays': 0, 'hourDays': 0}
    assert clean_retention({'hourDays': 0}, {'minuteDays': 31}) == {'minuteDays': 31, 'hourDays': 0}
    for bad in ({'minuteDays': 6}, {'minuteDays': -1}, {'minuteDays': 1.5}, {'minuteDays': True}, {'minuteDays': '31'}, {'other': 1}):
        with pytest.raises(ValueError, match='invalid_retention'):
            clean_retention(bad)
    # Hours never shorter than minutes, and kept for ever when minutes are.
    for bad in ({'minuteDays': 60, 'hourDays': 31}, {'minuteDays': 0, 'hourDays': 400}):
        with pytest.raises(ValueError, match='retention_hours_shorter'):
            clean_retention(bad)
    # From the 11-09 review: a period's traffic is summed from the hours, so they are kept a month at least,
    # as the settings page offers; one stored shorter before that counts as 31 days, read without an error.
    assert clean_retention({'hourDays': 31}, {'minuteDays': 7}) == {'minuteDays': 7, 'hourDays': 31}
    for days in range(1, 31):
        with pytest.raises(ValueError, match='retention_hours_too_short'):
            clean_retention({'hourDays': days}, {'minuteDays': 7})
        assert clean_retention({}, {'minuteDays': 7, 'hourDays': days}) == {'minuteDays': 7, 'hourDays': 31}
    assert clean_retention({}, {'minuteDays': 7, 'hourDays': 7}) == {'minuteDays': 7, 'hourDays': 31}
    assert clean_retention({'minuteDays': 14}, {'minuteDays': 7, 'hourDays': 10}) == {'minuteDays': 14, 'hourDays': 31}


async def test_prune_follows_the_days_kept_and_hands_space_back(tmp_path):
    store = await started(tmp_path / 'm.db')
    async with store.conn.execute('PRAGMA auto_vacuum') as cursor:
        assert (await cursor.fetchone())[0] == 2
    now = local(2026, 11, 5, 12)
    old = now - 20 * 86400
    # Twenty days ago and now, with enough rows that their pages are worth giving back.
    await minutes(store, 's', [(old + i * 60, 1, 1) for i in range(1500)] + [(now - 600, 5, 5)])
    await store.write_pings('s', [{'ts': old + i * 60, 'target': 'yn-ct', 'sent': 6, 'lost': 0, 'avg': 30, 'min': 29, 'max': 31} for i in range(1500)])
    before = (await store.storage())['bytes']
    store.retention = {'minuteDays': 7, 'hourDays': 0}
    await store.prune(now)
    async with store.conn.execute('SELECT MIN(ts), COUNT(*) FROM minute') as cursor:
        assert tuple(await cursor.fetchone()) == (now - 600, 1)
    async with store.conn.execute('SELECT COUNT(*) FROM ping_minute') as cursor:
        assert (await cursor.fetchone())[0] == 0
    # Hours kept for ever, days never dropped.
    async with store.conn.execute('SELECT COUNT(*) FROM hour WHERE ts<?', (now - 7 * 86400,)) as cursor:
        assert (await cursor.fetchone())[0] == 25
    async with store.conn.execute('SELECT COUNT(*) FROM ping_hour') as cursor:
        assert (await cursor.fetchone())[0] == 25
    after = await store.storage()
    assert after['freeBytes'] == 0 and after['bytes'] < before
    store.retention = {'minuteDays': 7, 'hourDays': 7}
    await store.prune(now)
    async with store.conn.execute('SELECT COUNT(*) FROM hour WHERE ts<?', (now - 7 * 86400,)) as cursor:
        assert (await cursor.fetchone())[0] == 0
    async with store.conn.execute('SELECT COUNT(*) FROM day') as cursor:
        assert (await cursor.fetchone())[0] == 3
    tables = (await store.storage())['tables']
    assert tables['minute']['rows'] == 1 and tables['day']['rows'] == 3 and set(tables) == {'minute', 'ping_minute', 'hour', 'ping_hour', 'disk', 'day'}
    await store.close()


async def test_meta_goes_with_its_server_and_lookups_a_month_old(tmp_path):
    """11-12 review: a deleted server's IP check and ports stayed, and lookups by address piled up for ever."""
    store = await started(tmp_path / 'm.db')
    now = local(2026, 11, 5, 12)
    fresh, stale = now - 86400, now - 40 * 86400
    kept = {'ipcheck:t': '{}', 'zstatic': '{}', 'days': '1', 'intel:ports:t': '{"at": 1}',
            'bgp:203.0.113.0/24': f'{{"atMs": {fresh * 1000}}}', 'intel:asn:203.0.113.9': f'{{"at": {fresh}}}'}
    # The server's own, lookups a month old, and one that cannot be read.
    gone = {'ipcheck:s': '{}', 'intel:ports:s': '{"at": 1}', 'bgp:198.51.100.0/24': f'{{"atMs": {stale * 1000}}}',
            'bgp-ip:198.51.100.9': f'{{"atMs": {stale * 1000}}}', 'asname:20473': f'{{"atMs": {stale * 1000}}}',
            'intel:rdns:198.51.100.9': f'{{"at": {stale}}}', 'asname:13335': 'not json'}
    for k, v in {**kept, **gone}.items():
        await store.set_meta(k, v)
    await store.prune(now)
    await store.forget('s')
    async with store.conn.execute('SELECT key FROM meta') as cursor:
        left = {r[0] async for r in cursor}
    assert left == set(kept)
    await store.close()


async def test_a_minute_written_at_shutdown_is_added_to_after_a_restart_within_it(tmp_path):
    # From the 11-09 review: closed and started again inside one minute, the controller wrote that minute
    # again from what it counted after starting, and the traffic before the restart was gone for good.
    store = await started(tmp_path / 'm.db')
    base = local(2026, 11, 5, 12, 30)
    store.observe('s', {**sample(0, 0), 'cpu': 10.0}, base - 30)
    store.observe('s', {**sample(1000, 100), 'cpu': 10.0}, base + 3)
    store.observe('s', {**sample(1600, 160), 'cpu': 10.0}, base + 24)
    await store.close()
    store = await started(tmp_path / 'm.db')
    assert store.observe('s', {**sample(2000, 200), 'cpu': 40.0}, base + 42) == (400, 40)
    assert await store.traffic_since('s', base) == (2000, 200)
    store.pending.append(('s', store.buckets.pop('s')))
    # A write that fails is rolled back and tried again: counted once.
    execute, failed = store.conn.execute, []

    async def failing(sql, *args):
        if sql.startswith('INSERT OR REPLACE INTO counters') and not failed:
            failed.append(sql)
            raise sqlite3.OperationalError('disk I/O error')
        return await execute(sql, *args)
    store.conn.execute = failing
    with pytest.raises(sqlite3.OperationalError):
        await store.flush()
    await store.flush()
    store.conn.execute = execute
    assert failed and not store.pending
    assert await store.traffic_since('s', base) == (2000, 200)
    async with store.conn.execute('SELECT samples, cpu, rx, tx FROM minute WHERE server_id=? AND ts=?', ('s', base)) as cursor:
        assert tuple(await cursor.fetchone()) == (3, 20.0, 2000, 200)
    async with store.conn.execute('SELECT rx FROM hour WHERE server_id=?', ('s',)) as cursor:
        assert [r[0] async for r in cursor] == [2000]
    days = await store.traffic_stats('s', 'day', now=base + 120)
    assert days['points'][-1]['rx'] == 2000
    await store.close()


async def test_a_periods_traffic_outlasts_its_minutes(tmp_path):
    # From the 11-09 review: minutes kept 7 days, a node offline since the 1st; its last hour was summed from
    # minutes already pruned, and this period's 1 GB came to nothing.
    store = await started(tmp_path / 'm.db')
    start = local(2026, 11, 1)
    await minutes(store, 's', [(start + 3600 * 5 + 60 * i, G // 4, 0) for i in range(4)])
    assert await store.traffic_since('s', start) == (G, 0)
    store.retention = {'minuteDays': 7, 'hourDays': 400}
    await store.prune(local(2026, 11, 11))
    async with store.conn.execute('SELECT COUNT(*) FROM minute') as cursor:
        assert (await cursor.fetchone())[0] == 0
    assert await store.traffic_since('s', start) == (G, 0)
    # Back online: the hours, then the minutes still in memory.
    await minutes(store, 's', [(local(2026, 11, 11, 9), 5, 0)])
    store.observe('s', sample(0, 0), local(2026, 11, 11, 9, 1))
    store.buckets['s'].rx = 7
    assert await store.traffic_since('s', start) == (G + 12, 0)
    # A start inside an hour: its minutes before the next whole hour, then the hours.
    assert await store.traffic_since('s', local(2026, 11, 11, 8, 30)) == (12, 0)
    await store.close()


async def test_closing_while_minutes_are_written_keeps_them(tmp_path, monkeypatch):
    # From the 11-09 review: closing cancelled the background write where it was, and the minutes it had taken
    # but not yet written were gone. Closing waits for it (a while), and one cut short keeps what it had not written.
    for wait, released in ((10, True), (0.2, False)):
        monkeypatch.setattr(metrics, 'CLOSE_WAIT', wait)
        path = tmp_path / f'{wait}.db'
        store = await started(path)
        base = local(2026, 11, 5, 12, 30)
        for i, sid in enumerate(('a', 'b', 'c')):
            store.observe(sid, sample(0, 0), base - 30)
            store.observe(sid, sample(100 * (i + 1), 0), base + 3)
            store.pending.append((sid, store.buckets.pop(sid)))
        execute, gate, held = store.conn.execute, asyncio.Event(), asyncio.Event()

        async def slow(sql, *args):
            # The second minute's write takes its time, once.
            if sql == metrics.WRITE_MINUTE and args[0][0] == 'b' and not held.is_set():
                held.set()
                await gate.wait()
            return await execute(sql, *args)
        store.conn.execute = slow
        store.flusher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await store.flusher
        store.flusher = asyncio.create_task(store.flush())
        closing = None
        try:
            await asyncio.wait_for(held.wait(), 3)
            closing = asyncio.create_task(store.close())
            await asyncio.sleep(0.3)
            if released:
                assert not closing.done(), 'waits for the write under way'
                gate.set()
            await asyncio.wait_for(asyncio.shield(closing), 5)
        finally:
            gate.set()
            if closing:
                await asyncio.wait_for(closing, 5)
            elif store.conn:
                await asyncio.wait_for(store.close(), 5)
        # A synchronous read also checks the committed data without leaving a worker open after a failed assertion.
        with sqlite3.connect(path) as conn:
            assert conn.execute('SELECT server_id,SUM(rx),SUM(tx) FROM minute WHERE ts>=? GROUP BY server_id', (base,)).fetchall() == [
                ('a', 100, 0), ('b', 200, 0), ('c', 300, 0)]
            assert conn.execute('SELECT server_id,rx FROM counters ORDER BY server_id').fetchall() == [('a', 100), ('b', 200), ('c', 300)]


async def test_closing_has_a_deadline_even_if_the_final_write_stalls(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(metrics, 'CLOSE_DRAIN', 0.1)
    store = await started(tmp_path / 'stalled.db')
    now = local(2026, 11, 5, 12, 30)
    store.observe('a', sample(0, 0), now)
    store.observe('a', sample(100, 0), now + 3)
    execute = store.conn.execute

    async def stalled(sql, *args):
        if sql == metrics.WRITE_MINUTE:
            await asyncio.Event().wait()
        return await execute(sql, *args)

    store.conn.execute = stalled
    try:
        await asyncio.wait_for(store.close(), 3)
        assert store.conn is None
        assert len(store.pending) == 1 and store.pending[0][1].rx == 100
        assert 'Timed out draining server metrics' in caplog.text
    finally:
        if store.conn:
            store.conn.execute = execute
            await asyncio.wait_for(store.close(), 3)


async def test_cancelling_a_commit_does_not_bill_that_minute_twice(tmp_path):
    path = tmp_path / 'commit.db'
    store = await started(path)
    now = local(2026, 11, 5, 12, 30)
    store.observe('a', sample(0, 0), now)
    store.observe('a', sample(100, 0), now + 3)
    store.pending.append(('a', store.buckets.pop('a')))
    commit, entered, release = store.conn.commit, asyncio.Event(), asyncio.Event()

    async def slow_commit():
        await commit()
        entered.set()
        await release.wait()

    store.conn.commit = slow_commit
    writer = asyncio.create_task(store.flush())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        writer.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(writer, 3)
        assert not store.pending
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(writer, 3)
        store.conn.commit = commit
        await asyncio.wait_for(store.close(), 3)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT SUM(rx) FROM minute').fetchone()[0] == 100


async def test_partitions_sampled_hourly(tmp_path):
    store = await started(tmp_path / 'm.db')
    now = local(2026, 11, 5, 12)
    for i in range(48):
        await store.write_disks('s', [{'path': '/', 'fs': 'ext4', 'total': 100 * G, 'used': (40 + i // 24) * G},
                                      {'path': '/data', 'fs': 'xfs', 'total': 200 * G, 'used': 50 * G}, {'path': '/empty', 'total': 0, 'used': 0}],
                               now - (47 - i) * 3600)
    # The same hour sampled again replaces its rows.
    await store.write_disks('s', [{'path': '/', 'fs': 'ext4', 'total': 100 * G, 'used': 42 * G}, {'path': '/data', 'fs': 'xfs', 'total': 200 * G, 'used': 50 * G}], now)
    week = await store.disks('s', '7d', now=now)
    assert week['partitions'] == [{'path': '/', 'fs': 'ext4', 'total': 100 * G, 'used': 42 * G}, {'path': '/data', 'fs': 'xfs', 'total': 200 * G, 'used': 50 * G}]
    assert week['step'] == 7200 and len(week['t']) == 25 and week['series']['/'][-1] == 42 and set(week['series']['/data']) == {25}
    everything = await store.disks('s', 'all', now=now)
    assert everything['step'] == 3600 and len(everything['t']) == 48
    assert (await store.disks('other', '7d', now=now)) == {'range': '7d', 'step': 7200, 'sampledAt': None, 'partitions': [], 't': [], 'series': {}}
    await store.close()


@pytest.mark.parametrize('zone', ['Asia/Kolkata', 'Asia/Kathmandu'])
async def test_daily_traffic_fractional_timezone_assigns_each_hour_once(tmp_path, zone):
    store = await started(tmp_path / 'm.db')
    store.zone = ZoneInfo(zone)
    try:
        midnight = int(dt.datetime(2026, 11, 7, tzinfo=store.zone).timestamp())
        boundary = midnight - midnight % 3600
        # Both sides of local midnight share a UTC hour: it belongs wholly to Nov 7.
        await minutes(store, 's', [(boundary - 60, 10, 1), (boundary + 60, 20, 2),
                                   (midnight + 60, 30, 3), (boundary + 3660, 40, 4)])
        async with store.conn.execute('SELECT day,rx,tx FROM day ORDER BY day') as cursor:
            assert [tuple(r) async for r in cursor] == [('2026-11-06', 10, 1), ('2026-11-07', 90, 9)]
        assert store._day(boundary + 60) == store._day(midnight + 60)
        assert store._day(midnight)[1] == boundary
        # Backfill uses the same assignment as the live minute writer.
        await store.conn.execute('DELETE FROM day')
        await store.conn.commit()
        await store._backfill_days()
        async with store.conn.execute('SELECT SUM(rx),SUM(tx) FROM day') as cursor:
            assert tuple(await cursor.fetchone()) == (100, 10)
        store.observe('pending', sample(0, 0), boundary + 60)
        store.buckets['pending'].rx = 7
        live = (await store.traffic_stats('pending', 'day', now=boundary + 120))['points'][-1]
        assert live['label'] == '2026-11-07' and live['t'] == boundary and live['rx'] == 7
    finally:
        await store.close()


async def test_latency_untrusted_records_are_dropped_individually(tmp_path):
    import time
    from types import SimpleNamespace
    from app.servers.latency import Latency
    store = await started(tmp_path / 'm.db')
    try:
        latency = Latency(SimpleNamespace(metrics=store))
        latency.sent['s'] = {'known': {}}
        now = int(time.time()) // 60 * 60
        good = {'ts': now - 120, 'target': 'known', 'sent': 20, 'lost': 1, 'avg': 10, 'min': 9, 'max': 11}
        bad = [{'sent': 2**62}, {'lost': -1}, {'lost': 21}, {'avg': float('nan')}, {'avg': 10**400}, {'min': {}},
               {'ts': float('inf')}, {'ts': 'soon'}, {'target': []}, {'target': 'not-issued'}]
        await latency.observe('s', [good, 'junk', *({**good, 'ts': now - 60, **patch} for patch in bad),
                                    {**good, 'ts': now, 'sent': 6}])
        async with store.conn.execute('SELECT ts,sent FROM ping_minute ORDER BY ts') as cursor:
            assert [tuple(r) async for r in cursor] == [(now - 120, 20), (now, 6)]
        assert not store.conn.in_transaction
        assert (await store.pings('s', '24h', now=now + 60))['targets']['known']['avg']
    finally:
        await store.close()


async def test_latency_rollup_failure_rolls_back_minutes_and_ids(tmp_path):
    import sqlite3
    store = await started(tmp_path / 'm.db')
    try:
        await store.conn.execute("CREATE TRIGGER reject_ping BEFORE INSERT ON ping_hour BEGIN SELECT RAISE(ABORT, 'rollup failed'); END")
        await store.conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match='rollup failed'):
            await store.write_pings('s', [{'ts': 1800000000, 'target': 'known', 'sent': 6, 'lost': 0, 'avg': 10}])
        assert not store.conn.in_transaction
        await store.set_meta('unrelated', 'must not commit failed minutes')
        for table in ('ping_ids', 'ping_minute', 'ping_hour'):
            async with store.conn.execute(f'SELECT COUNT(*) FROM {table}') as cursor:
                assert (await cursor.fetchone())[0] == 0
    finally:
        await store.close()


async def test_latency_minutes_hours_and_the_cards_summary(tmp_path):
    store = await started(tmp_path / 'm.db')
    now = local(2026, 11, 5, 12, 30)
    rows = []
    for i in range(40):
        ts = now - (40 - i) * 60
        rows.append({'ts': ts, 'target': 'yn-ct', 'sent': 6, 'lost': 0, 'avg': 30.0, 'min': 28, 'max': 35})
        rows.append({'ts': ts, 'target': 'yn-cu', 'sent': 6, 'lost': 3 if i >= 35 else 0, 'avg': 60.0, 'min': 50, 'max': 90})
    # All lost: no latency; nothing sent or more lost than sent: dropped.
    rows += [{'ts': now - 60, 'target': 'yn-cm', 'sent': 6, 'lost': 6, 'avg': 12.0}, {'ts': now - 60, 'target': 'x', 'sent': 0, 'lost': 0},
             {'ts': now - 60, 'target': 'y', 'sent': 2, 'lost': 3}]
    await store.write_pings('s', rows)
    async with store.conn.execute("SELECT avg FROM ping_minute WHERE target='yn-cm'") as cursor:
        assert (await cursor.fetchone())[0] is None
    async with store.conn.execute("SELECT COUNT(*) FROM ping_minute WHERE target IN ('x','y')") as cursor:
        assert (await cursor.fetchone())[0] == 0
    # Stored as tenths of a millisecond.
    async with store.conn.execute("SELECT sent, lost, avg, min, max FROM ping_hour WHERE target='yn-ct' ORDER BY ts DESC") as cursor:
        assert tuple(await cursor.fetchone()) == (6 * 30, 0, 300, 280, 350)
    hour = await store.pings('s', '1h', now=now)
    assert set(hour['targets']) == {'yn-ct', 'yn-cu', 'yn-cm'} and len(hour['t']) == 40
    assert hour['targets']['yn-ct']['avg'][-1] == 30 and hour['targets']['yn-cu']['loss'][-1] == 50 and hour['targets']['yn-cm']['avg'][-1] is None
    assert hour['targets']['yn-cm']['avg'][0] is None and hour['targets']['yn-cm']['loss'][0] is None
    summary = (await store.ping_summary(['s', 'none'], now=now))
    card = summary['items']['s']
    assert list(summary['items']) == ['s'] and len(card['cells']) == 30
    # The last minute: two answered targets weighted by replies, one target fully lost.
    assert card['cells'][-1] == [round((30 * 6 + 60 * 3) / 9, 1), round(9 * 100 / 18, 1)]
    assert card['loss'] == round((5 * 3 + 6) * 100 / (10 * 12 + 6), 1) and card['avg'] is not None
    await store.close()
