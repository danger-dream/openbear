"""Mesh lines: which there are, how each looks now from both ends' reports, their long-term
record in the metrics file (minutes 2 days, hours 30 days) and the path's round trips taken
from what members measure themselves. Pure functions first, then the real Web app."""
from __future__ import annotations

import aiosqlite
import pytest

from app.instances.contracts import InstanceError
from app.servers import compat, mesh_lines, mesh_path, mesh_status
from app.servers import mesh as mesh_module
from app.servers.mesh_config import build
from app.servers.metrics import MetricsStore
from tests.test_mesh_config import doc, facts, member
from tests.test_mesh_service import mesh_setup, settled
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

NOW = 1_793_001_600_000  # a whole hour, in ms


def layout():
    """r1, r2 relays (r2 also on tcp); m1 enters r1/udp, r2/udp, r2/tcp in failover; m2 r1/udp fixed; m1-m2 punched direct."""
    d = doc(member('r1', 'relay'), member('r2', 'relay', listen={'tcp': 52000}),
            member('m1', entry={'mode': 'failover', 'autoReturn': True, 'candidates': [
                {'id': 'c1', 'relay': 'r1', 'transport': 'udp'}, {'id': 'c2', 'relay': 'r2', 'transport': 'udp'},
                {'id': 'c3', 'relay': 'r2', 'transport': 'tcp'}]}),
            member('m2', entry={'mode': 'fixed', 'autoReturn': True, 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}]}),
            direct=[{'a': 'm1', 'b': 'm2', 'transport': 'udp', 'dialer': 'a'}])
    return d, build(d, facts(), 'net', 1)


def reports(*, m1_state='active', c2='ready', link='up', at=NOW):
    st = {
        'r1': {'peers': [{'id': 'r2', 'kind': 'relay', 'active': True, 'transport': 'udp', 'endpoint': '203.0.113.2:51999',
                          'localAddr': '10.0.0.1:51999', 'rttMs': 31},
                         {'id': 'm1', 'kind': 'member', 'active': True, 'transport': 'udp', 'endpoint': '198.18.0.9:40000',
                          'localAddr': '10.0.0.1:51999', 'rttMs': 0}],
               'routing': {'mode': 'dynamic', 'links': [{'peer': 'r2', 'state': link, 'rttMs': 30.0, 'loss': 0.02}], 'routes': []},
               'reachability': {'verdict': 'public', 'publicAddr': '203.0.113.1:51999', 'consistent': True}},
        'm1': {'entry': {'candidate': 'c1', 'relay': 'r1', 'transport': 'udp', 'candidates': [
                   {'id': 'c1', 'relay': 'r1', 'transport': 'udp', 'state': m1_state, 'rttMs': 40.0, 'timeoutRate': 0.0},
                   {'id': 'c2', 'relay': 'r2', 'transport': 'udp', 'state': c2, 'rttMs': 80.0},
                   {'id': 'c3', 'relay': 'r2', 'transport': 'tcp', 'state': 'untested'}]},
               'peers': [{'id': 'r1', 'kind': 'entry', 'active': True, 'transport': 'udp', 'endpoint': '203.0.113.1:51999',
                          'localAddr': '192.168.1.5:51999', 'rttMs': 41.0},
                         {'id': 'm2', 'kind': 'direct', 'active': True, 'transport': 'udp', 'endpoint': '198.18.0.20:51999',
                          'localAddr': '192.168.1.5:51999', 'rttMs': 5.0, 'direct': {'state': 'direct', 'via': 'public'}}],
               'reachability': {'verdict': 'punchable', 'publicAddr': '198.18.0.9:40000', 'consistent': True}},
    }
    return {sid: {'status': mesh_status.clean_status(raw), 'at': at} for sid, raw in st.items()}


def by_id(lines):
    return {line['id']: line for line in lines}


def test_the_lines_of_a_mesh_and_how_they_look_from_both_ends():
    d, configs = layout()
    lines = mesh_lines.catalogue(d, configs)
    assert [line['id'] for line in lines] == ['relay:r1|r2', 'entry:m1|r1|udp', 'entry:m1|r2|udp', 'entry:m1|r2|tcp', 'entry:m2|r1|udp',
                                              'direct:m1|m2']
    assert all(mesh_lines.parse_id(line['id'])[0] == line['kind'] for line in lines)
    assert mesh_lines.parse_id('relay:r2|r1') is None and mesh_lines.parse_id('entry:m1|r1|c1') is None
    view = by_id(mesh_lines.view(lines, reports(), NOW))
    relay = view['relay:r1|r2']
    assert (relay['state'], relay['rttMs'], relay['lossRate'], relay['transport']) == ('up', 30.0, 0.02, 'udp')
    # r1 reported, r2 did not: its end is null.
    assert relay['ends'] == {'a': {'local': '10.0.0.1:51999', 'public': '203.0.113.1:51999', 'remote': '203.0.113.2:51999', 'dialer': True},
                             'b': None}
    # The active entry by the member's own measurement, both ends; the relay's 0 for the member does not count.
    active = view['entry:m1|r1|udp']
    assert (active['state'], active['role'], active['rttMs']) == ('up', 'active', 40.0)
    assert active['ends'] == {'a': {'local': '192.168.1.5:51999', 'public': '198.18.0.9:40000', 'remote': '203.0.113.1:51999', 'dialer': True},
                              'b': {'local': '10.0.0.1:51999', 'public': '203.0.113.1:51999', 'remote': '198.18.0.9:40000', 'dialer': False}}
    # The warm standby and a cold one: the member's configured endpoint, no relay end.
    standby, cold = view['entry:m1|r2|udp'], view['entry:m1|r2|tcp']
    assert (standby['state'], standby['rttMs']) == ('standby', 80.0)
    assert standby['ends'] == {'a': {'local': '', 'public': '198.18.0.9:40000', 'remote': '203.0.113.2:51999', 'dialer': True}, 'b': None}
    assert (cold['state'], cold['role'], cold['rttMs']) == ('cold', 'cold', None) and cold['ends']['a']['remote'] == '203.0.113.2:52000'
    assert cold['ends']['a']['public'] == ''  # a public mapping is a udp one
    # m2 never reported: unknown, both ends null.
    assert view['entry:m2|r1|udp']['state'] == 'unknown' and view['entry:m2|r1|udp']['ends'] == {'a': None, 'b': None}
    direct = view['direct:m1|m2']
    assert (direct['state'], direct['rttMs'], direct['punch']) == ('up', 5.0, True)
    assert direct['ends']['a'] == {'local': '192.168.1.5:51999', 'public': '198.18.0.9:40000', 'remote': '198.18.0.20:51999', 'dialer': True}
    # A failing standby is down, the next one becomes the warm one; reports past 150 s say nothing.
    view = by_id(mesh_lines.view(lines, reports(c2='failing', link='down'), NOW))
    assert view['entry:m1|r2|udp']['state'] == 'down' and view['entry:m1|r2|tcp']['state'] == 'standby'
    assert view['relay:r1|r2']['state'] == 'down'
    stale = by_id(mesh_lines.view(lines, reports(at=NOW - 200_000), NOW))
    assert {line['state'] for line in stale.values()} == {'unknown'}
    # The node's own role wins over the inference.
    st = reports()
    st['m1']['status']['entry']['candidates'][2]['role'] = 'standby'
    st['m1']['status']['entry']['candidates'][1]['role'] = 'cold'
    view = by_id(mesh_lines.view(lines, st, NOW))
    assert view['entry:m1|r2|udp']['state'] == 'cold' and view['entry:m1|r2|tcp']['state'] == 'standby'
    # The address in use (remoteAddr) wins over the endpoint, which may be a configured host name.
    raw = {'peers': [{'id': 'r2', 'kind': 'relay', 'active': True, 'transport': 'tcp', 'endpoint': 'r2.example.net:52000',
                      'remoteAddr': '203.0.113.2:52000', 'localAddr': '10.0.0.1:41000'}]}
    st = reports()
    st['r1']['status']['peers'][0] = mesh_status.clean_status(raw)['peers'][0]
    assert by_id(mesh_lines.view(lines, st, NOW))['relay:r1|r2']['ends']['a']['remote'] == '203.0.113.2:52000'
    st['r1']['status']['peers'][0]['remoteAddr'] = ''
    assert by_id(mesh_lines.view(lines, st, NOW))['relay:r1|r2']['ends']['a']['remote'] == 'r2.example.net:52000'


def test_a_fixed_member_uses_only_its_first_candidate():
    d = doc(member('r1', 'relay'), member('r2', 'relay'),
            member('m2', entry={'mode': 'fixed', 'autoReturn': True, 'candidates': [
                {'id': 'c1', 'relay': 'r1', 'transport': 'udp'}, {'id': 'c2', 'relay': 'r2', 'transport': 'udp'}]}))
    lines = mesh_lines.catalogue(d, build(d, facts(), 'net', 1))
    raw = {'entry': {'candidate': 'c1', 'relay': 'r1', 'transport': 'udp', 'candidates': [
               {'id': 'c1', 'relay': 'r1', 'transport': 'udp', 'state': 'active', 'rttMs': 12.0}]},
           'peers': [{'id': 'r1', 'kind': 'entry', 'active': True, 'transport': 'udp', 'endpoint': '203.0.113.1:51999',
                      'localAddr': '192.168.1.6:51999', 'rttMs': 12.0}]}
    st = {'m2': {'status': mesh_status.clean_status(raw), 'at': NOW}}
    view = by_id(mesh_lines.view(lines, st, NOW))
    assert (view['entry:m2|r1|udp']['state'], view['entry:m2|r1|udp']['role']) == ('up', 'active')
    unused = view['entry:m2|r2|udp']
    assert (unused['state'], unused['role'], unused['rttMs']) == ('unused', 'unused', None)


async def test_minutes_hours_ranges_summary_and_forgetting(tmp_path):
    d, configs = layout()
    lines = mesh_lines.catalogue(d, configs)
    metrics = MetricsStore(tmp_path / 'metrics.db')
    await metrics.start()
    store = mesh_lines.LineTrend(metrics)
    await store.start()

    async def feed(minute, **kw):
        # Twenty reports a minute, as nodes send them every 3 s; the relay link from both ends.
        for i in range(20):
            at = NOW + minute * 60_000 + i * 3000
            st = reports(at=at, **kw)
            for reporter in ('m1', 'r1'):
                await store.observe(lines, reporter, st, at)
    await feed(0)
    await feed(1, link='down', c2='failing')  # the relay link and the standby go down for a minute
    await feed(2)
    for minute in range(3, 61):
        await feed(minute)
    await feed(61)  # writes minute 60
    summary, points = await store.series('relay:r1|r2', '1h', NOW + 61 * 60_000)
    # The minute under way is not written yet: its point is null.
    assert len(points) == 60 and all(p['upRatio'] == 1.0 and p['rttMs'] == 30.0 for p in points[:-1]) and points[-1]['rttMs'] is None
    summary, points = await store.series('relay:r1|r2', '24h', NOW + 61 * 60_000)
    assert len(points) == 120 and [p for p in points if p['rttMs'] is not None][0]['upRatio'] == round(11 / 12, 3)
    assert summary == {'availability': round(100 * 60 * 60 / (61 * 60), 2), 'rttMs': {'avg': 30.0, 'min': 30.0, 'max': 30.0, 'p95': 30.0},
                       'lossRate': 0.02, 'downs': 1, 'downSeconds': 60, 'knownSeconds': 61 * 60}
    # Only one end sampled it: twenty samples a minute, not forty.
    async with metrics.conn.execute("SELECT samples FROM mesh_line_minute m JOIN mesh_line l ON l.id=m.line WHERE l.line='relay:r1|r2' AND ts=?",
                                    (NOW // 1000,)) as cursor:
        assert (await cursor.fetchone())['samples'] == 20
    # The standby by its own checks; the cold one has no round trip: its points are null, its state still known.
    summary, _ = await store.series('entry:m1|r2|udp', '24h', NOW + 61 * 60_000)
    assert summary['downs'] == 1 and summary['rttMs']['avg'] == 80.0
    summary, points = await store.series('entry:m1|r2|tcp', '24h', NOW + 61 * 60_000)
    assert summary['rttMs'] == {'avg': None, 'min': None, 'max': None, 'p95': None} and summary['availability'] is None
    assert all(p['rttMs'] is None for p in points)
    # Past two days the hours carry on: 3d, 7d, 15d and 30d in at most about 120 points.
    later = NOW + 3 * 86400_000
    await store.observe(lines, 'm1', reports(at=later), later)
    await store.flush(later // 1000 + 3600, later // 1000 + 3600)
    for span, count in (('3d', 72), ('7d', 84), ('15d', 90), ('30d', 120)):
        summary, points = await store.series('entry:m1|r1|udp', span, later + 60_000)
        assert len(points) in (count, count + 1) and summary['rttMs']['avg'] == 40.0, span
    async with metrics.conn.execute('SELECT COUNT(*) AS n FROM mesh_line_minute WHERE ts<?', (NOW // 1000 + 3600,)) as cursor:
        assert (await cursor.fetchone())['n'] == 0  # minutes older than two days pruned
    assert await store.peers('entry', 'm1', NOW // 1000, role=0) == ['r1']
    assert await store.peers('relay', 'r2', NOW // 1000) == ['r1']
    await store.forget('m1')
    assert not await store.recorded('entry:m1|r1|udp', later) and await store.recorded('relay:r1|r2', later)
    await metrics.close()


def test_a_path_through_relays_takes_the_members_own_entry_round_trip():
    """SG -> home -> TOY -> Windows: TOY reports 0 for the members it carries; Windows' entry decides."""
    roles = {'sg': 'member', 'home': 'relay', 'toy': 'relay', 'win': 'member'}

    def st(raw):
        return {'status': mesh_status.clean_status(raw), 'at': NOW}
    statuses = {
        'sg': st({'entry': {'relay': 'home', 'transport': 'udp', 'candidates': [{'id': 'c1', 'relay': 'home', 'transport': 'udp', 'state': 'active'}]},
                  'peers': [{'id': 'home', 'kind': 'entry', 'active': True, 'rttMs': 12.0}]}),
        'home': st({'peers': [{'id': 'toy', 'kind': 'relay', 'active': True, 'rttMs': 0}],
                    'routing': {'mode': 'dynamic', 'links': [{'peer': 'toy', 'state': 'up', 'rttMs': 20.0}],
                                'routes': [{'dest': 'toy', 'nextHop': 'toy', 'path': ['home', 'toy']}]},
                    'owners': {'win': {'relay': 'toy'}}}),
        'toy': st({'peers': [{'id': 'win', 'kind': 'member', 'active': True, 'rttMs': 0.0}, {'id': 'home', 'kind': 'relay', 'active': True}]}),
        # Its own record for TOY says 0 too (an inactive one first); its entry candidate has the measurement.
        'win': st({'entry': {'relay': 'toy', 'transport': 'tcp', 'candidates': [
                       {'id': 'c2', 'relay': 'toy', 'transport': 'udp', 'state': 'ready', 'rttMs': 90.0},
                       {'id': 'c1', 'relay': 'toy', 'transport': 'tcp', 'state': 'active', 'rttMs': 33.0}]},
                   'peers': [{'id': 'toy', 'kind': 'entry', 'active': True, 'rttMs': 0.0}]}),
    }
    path = mesh_path.find('sg', 'win', roles, statuses, {}, NOW)
    assert [(h['node'], h['via'], h['rttMs']) for h in path['hops']] == [('sg', '', None), ('home', 'entry', 12.0), ('toy', 'relay', 20.0),
                                                                          ('win', 'entry', 33.0)]
    assert path == {**path, 'stale': False, 'rttMs': 65.0}
    # A node that gives no per-candidate measurement: its own peer record, an active one before an inactive.
    statuses['win'] = st({'entry': {'relay': 'toy', 'transport': 'tcp'},
                          'peers': [{'id': 'toy', 'kind': 'relay', 'active': False, 'rttMs': 0},
                                    {'id': 'toy', 'kind': 'entry', 'active': True, 'rttMs': 35.0}]})
    assert mesh_path.find('sg', 'win', roles, statuses, {}, NOW)['rttMs'] == 67.0
    # The other way the member is the source: the same segment, the same measurement.
    back = mesh_path.find('win', 'sg', roles, statuses, {}, NOW)
    assert back['hops'][1] == {'node': 'toy', 'via': 'entry', 'rttMs': 35.0}


def _st(raw):
    return mesh_status.clean_status(raw)


def test_a_round_trip_of_0_is_not_a_measurement():
    st = _st
    # A relay: its measured links to other relays; the 0 it reports for the members it carries (and a
    # link not measured yet) count for nothing.
    relay = st({'peers': [{'id': 'm1', 'kind': 'member', 'active': True, 'rttMs': 0}, {'id': 'r2', 'kind': 'relay', 'active': True, 'rttMs': 0}],
                'routing': {'mode': 'dynamic', 'links': [{'peer': 'r2', 'state': 'up', 'rttMs': 0}, {'peer': 'r3', 'state': 'up', 'rttMs': 12.5},
                                                         {'peer': 'r4', 'state': 'up', 'rttMs': 30.0}]}})
    assert mesh_status.summary(relay, 'relay')['rttMs'] == 12.5
    relay['routing']['links'] = relay['routing']['links'][:1]
    assert mesh_status.summary(relay, 'relay')['rttMs'] is None
    # A member: its entry as it measures it, not a 0 record nor another peer's.
    member = st({'entry': {'relay': 'r1', 'transport': 'udp', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp', 'state': 'active', 'rttMs': 33.0}]},
                 'peers': [{'id': 'r1', 'kind': 'entry', 'active': True, 'rttMs': 0}, {'id': 'm2', 'kind': 'direct', 'active': True, 'rttMs': 4.0}]})
    assert mesh_status.summary(member, 'member')['rttMs'] == 33.0
    member['entry']['candidates'][0]['rttMs'] = None
    assert mesh_status.summary(member, 'member')['rttMs'] is None and mesh_status.summary(member, None)['rttMs'] is None


def test_one_transport_does_not_speak_for_another():
    st = _st
    # The candidate in use on tcp has no round trip yet; the cold udp check to the same relay is not
    # its measurement: the member's own record for the relay is.
    win = st({'entry': {'relay': 'toy', 'transport': 'tcp', 'candidates': [
                  {'id': 'c2', 'relay': 'toy', 'transport': 'udp', 'state': 'ready', 'rttMs': 90.0},
                  {'id': 'c1', 'relay': 'toy', 'transport': 'tcp', 'state': 'active'}]},
              'peers': [{'id': 'toy', 'kind': 'entry', 'active': True, 'rttMs': 35.0}]})
    statuses = {'win': {'status': win, 'at': NOW},
                'toy': {'status': st({'peers': [{'id': 'win', 'kind': 'member', 'active': True, 'rttMs': 0}]}), 'at': NOW}}
    path = mesh_path.find('win', 'toy', {'win': 'member', 'toy': 'relay'}, statuses, {}, NOW)
    assert path['hops'][1] == {'node': 'toy', 'via': 'entry', 'rttMs': 35.0}
    assert mesh_status.entry_rtt(win, 'toy') == 35.0 and mesh_status.summary(win)['rttMs'] == 35.0
    # No record either: unknown, not the other transport's 90.
    win['peers'] = []
    assert mesh_path.find('win', 'toy', {'win': 'member', 'toy': 'relay'}, statuses, {}, NOW)['rttMs'] is None
    assert mesh_status.entry_rtt(win, 'toy') is None


async def test_lines_are_stored_by_integer_keys_and_found_without_scans(tmp_path):
    path = tmp_path / 'metrics.db'
    # The first layout, keyed by the line id's text: dropped and made again on start.
    async with aiosqlite.connect(path) as conn:
        await conn.executescript("CREATE TABLE mesh_line_minute (line TEXT NOT NULL, ts INTEGER NOT NULL, samples INTEGER NOT NULL, "
                                 "PRIMARY KEY(line, ts)) WITHOUT ROWID; CREATE TABLE mesh_line_hour (line TEXT NOT NULL, ts INTEGER NOT NULL, "
                                 "samples INTEGER NOT NULL, PRIMARY KEY(line, ts)) WITHOUT ROWID; "
                                 "INSERT INTO mesh_line_hour VALUES('relay:r1|r2', 1793001600, 20);")
    metrics = MetricsStore(path)
    await metrics.start()
    store = mesh_lines.LineTrend(metrics)
    await store.start()
    for table in ('mesh_line_minute', 'mesh_line_hour'):
        async with metrics.conn.execute(f"SELECT type FROM pragma_table_info('{table}') WHERE name='line'") as cursor:
            assert (await cursor.fetchone())['type'] == 'INTEGER'
        async with metrics.conn.execute(f'SELECT COUNT(*) AS n FROM {table}') as cursor:
            assert (await cursor.fetchone())['n'] == 0
    await store.start()  # the new layout stays

    d, configs = layout()
    lines = mesh_lines.catalogue(d, configs)
    for minute in range(3):
        at = NOW + minute * 60_000
        for reporter in ('m1', 'r1'):
            await store.observe(lines, reporter, reports(at=at), at)
    async with metrics.conn.execute('SELECT line,kind,a,b FROM mesh_line') as cursor:
        stored = {tuple(r) for r in await cursor.fetchall()}
    # m2 never reported: its entry was not sampled, so it has no key.
    assert stored == {('relay:r1|r2', 'relay', 'r1', 'r2'), ('entry:m1|r1|udp', 'entry', 'm1', 'r1'), ('entry:m1|r2|udp', 'entry', 'm1', 'r2'),
                      ('entry:m1|r2|tcp', 'entry', 'm1', 'r2'), ('direct:m1|m2', 'direct', 'm1', 'm2')}
    async with metrics.conn.execute('SELECT DISTINCT typeof(line) AS t FROM mesh_line_minute') as cursor:
        assert [r['t'] for r in await cursor.fetchall()] == ['integer']

    # What peers() and forget() ask, as SQLite plans it: every table by its key or an index.
    statements = []
    await metrics.conn.set_trace_callback(statements.append)
    assert await store.peers('relay', 'r2', NOW // 1000) == ['r1']
    assert await store.peers('entry', 'm1', NOW // 1000, role=0) == ['r1']
    assert await store.peers('direct', 'm2', NOW // 1000) == ['m1']
    await store.forget('m2')
    await metrics.conn.set_trace_callback(None)
    asked = [q for q in statements if q.lstrip().upper().startswith(('SELECT', 'DELETE'))]
    assert len(asked) >= 8
    for sql in asked:
        async with metrics.conn.execute('EXPLAIN QUERY PLAN ' + sql) as cursor:
            plan = [r['detail'] for r in await cursor.fetchall()]
        assert not [step for step in plan if step.startswith('SCAN')], (sql, plan)
    # forget() takes the lines' keys with their rows.
    async with metrics.conn.execute("SELECT COUNT(*) AS n FROM mesh_line WHERE line='direct:m1|m2'") as cursor:
        assert (await cursor.fetchone())['n'] == 0
    assert await store.peers('direct', 'm1', NOW // 1000) == []
    summary, points = await store.series('direct:m1|m2', '1h', NOW + 3 * 60_000)
    assert summary['knownSeconds'] == 0 and all(p['rttMs'] is None for p in points)
    # A line never stored answers like one without rows.
    assert not await store.recorded('relay:x|y', NOW) and (await store.series('relay:x|y', '30d', NOW))[0]['availability'] is None
    await metrics.close()


async def test_a_past_line_is_only_its_owners(env, monkeypatch):  # noqa: F811
    e = env
    clock = [NOW]
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: clock[0])
    nodes, ids = await mesh_setup(e)
    r1, m1 = ids['r1'], ids['m1']
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [r1, m1]})
    members = added['members']
    members[0]['role'] = 'relay'
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': [members[0], {'serverId': m1, 'entry': {
        'mode': 'fixed', 'candidates': [{'relay': r1, 'transport': 'udp'}]}}]})
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    # m1 once entered r1 over tcp: no longer a line, still on record.
    past = f'entry:{m1}|{r1}|tcp'
    bucket = mesh_lines.Bucket(NOW // 1000)
    bucket.samples = bucket.known = bucket.up = 1
    store = e.servers.mesh.line_store
    store.buckets[past] = bucket
    await store.flush(NOW // 1000 + 60, NOW // 1000 + 60)
    clock[0] = NOW + 120_000
    trend = await api(e, 'GET', f'/api/servers/mesh/lines/{past.replace("|", "%7C")}/trend')
    assert trend['line'] == {'id': past, 'kind': 'entry', 'a': m1, 'b': r1, 'transport': 'tcp', 'current': False}
    assert trend['summary']['availability'] == 100.0
    # Another owner who knows the ids gets nothing, past or current.
    for line_id in (past, f'entry:{m1}|{r1}|udp'):
        with pytest.raises(InstanceError) as caught:
            await e.servers.mesh.line_trend(456, line_id, '24h')
        assert (caught.value.error, caught.value.status) == ('mesh_line_not_found', 404)

async def test_reports_read_the_applied_revision_once_until_another_is_issued(env, monkeypatch):  # noqa: F811
    e = env
    nodes, ids = await mesh_setup(e)
    r1, m1 = ids['r1'], ids['m1']
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [r1, m1]})
    members = added['members']
    members[0]['role'] = 'relay'
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': [members[0], {'serverId': m1, 'entry': {
        'mode': 'fixed', 'candidates': [{'relay': r1, 'transport': 'udp'}]}}]})
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    mesh = e.servers.mesh
    statements, reads = [], {name: getattr(mesh_module, name) for name in ('one', 'many')}
    for name, read in reads.items():
        def asked(conn, sql, args=(), *, read=read):
            statements.append(sql)
            return read(conn, sql, args)
        monkeypatch.setattr(mesh_module, name, asked)
    for _ in range(3):
        await mesh.report(m1, {'startedAt': 1, 'appliedRevision': 1, 'ready': True, 'configSchemas': list(compat.CONFIG_SCHEMA)})
    for name, read in reads.items():
        monkeypatch.setattr(mesh_module, name, read)
    assert statements  # the reads were seen
    # No revision, document or member read again for a report: they are kept.
    assert not [q for q in statements if 'mesh_revisions' in q or 'mesh_members' in q or 'mesh_network' in q]
    assert (await mesh._applied(123))['revision'] == 1
    # Another revision is read once, by the next one asking.
    view = await api(e, 'GET', '/api/servers/mesh')
    view['members'][1]['entry']['candidates'][0]['transport'] = 'tcp'
    view['members'][0]['listen']['tcp'] = 52000
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': view['members']})
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    applied = await mesh._applied(123)
    assert applied['revision'] == 2 and [line['id'] for line in applied['lines'] if line['kind'] == 'entry'] == [f'entry:{m1}|{r1}|tcp']
    assert applied['members'][m1]['entry']['candidates'][0]['transport'] == 'tcp'
    assert (await mesh._lines(123)) is applied['lines']

async def test_the_overview_lists_lines_and_the_trend_answers_by_line(env, monkeypatch):  # noqa: F811
    e = env
    clock = [NOW]
    monkeypatch.setattr(mesh_module, 'now_ms', lambda: clock[0])
    nodes, ids = await mesh_setup(e)
    r1, m1 = ids['r1'], ids['m1']
    added = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [r1, m1]})
    members = added['members']
    members[0]['role'] = 'relay'
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': members})
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': [members[0], {'serverId': m1, 'entry': {
        'mode': 'fixed', 'candidates': [{'relay': r1, 'transport': 'udp'}]}}]})
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    line_id = f'entry:{m1}|{r1}|udp'
    report = {'startedAt': 1, 'appliedRevision': 1, 'ready': True, 'configSchemas': list(compat.CONFIG_SCHEMA),
              'entry': {'candidate': 'c1', 'relay': r1, 'transport': 'udp',
                        'candidates': [{'id': 'c1', 'relay': r1, 'transport': 'udp', 'state': 'active', 'rttMs': 44.0}]},
              'peers': [{'id': r1, 'kind': 'entry', 'active': True, 'transport': 'udp', 'endpoint': '198.51.100.1:51999',
                         'localAddr': '10.1.1.2:51999', 'rttMs': 45.0}]}
    for minute in range(3):
        clock[0] = NOW + minute * 60_000
        await e.servers.mesh.report(m1, report)
    overview = await api(e, 'GET', '/api/servers/mesh')
    [line] = [x for x in overview['lines'] if x['id'] == line_id]
    assert (line['kind'], line['a'], line['b'], line['state'], line['rttMs']) == ('entry', m1, r1, 'up', 44.0)
    # The relay never reported: its end is null.
    assert line['ends'] == {'a': {'local': '10.1.1.2:51999', 'public': '', 'remote': '198.51.100.1:51999', 'dialer': True}, 'b': None}
    path = f'/api/servers/mesh/lines/{line_id.replace("|", "%7C")}/trend'
    trend = await api(e, 'GET', path)
    assert trend['line'] == {'id': line_id, 'kind': 'entry', 'a': m1, 'b': r1, 'transport': 'udp', 'current': True}
    assert trend['range'] == '24h' and trend['summary']['availability'] == 100.0 and trend['summary']['rttMs']['avg'] == 44.0
    assert len(trend['series']) <= 121 and sum(p['rttMs'] is not None for p in trend['series']) == 1
    assert (await api(e, 'GET', path + '?range=90d', status=400))['error'] == 'invalid_range'
    assert (await api(e, 'GET', '/api/servers/mesh/lines/bogus/trend', status=400))['error'] == 'invalid_mesh_line'
    a, b = sorted((m1, r1))
    assert (await api(e, 'GET', f'/api/servers/mesh/lines/direct:{a}%7C{b}/trend', status=404))['error'] == 'mesh_line_not_found'
    # The member's trend says what its rttMs is to.
    member = await api(e, 'GET', f'/api/servers/mesh/members/{m1}/trend?range=24h')
    assert member['rtt'] == {'kind': 'entry', 'peers': [r1]}
    # The tool.
    from app.tools.servers import ACTIONS, MESH_CONTRACTS, _mesh_line_trend
    assert 'mesh_line_trend' in ACTIONS and 'lineId' in MESH_CONTRACTS['mesh_line_trend']
    told = _mesh_line_trend(await e.servers.mesh.line_trend(123, line_id, '1h'))
    assert told['sampled'] == 2 and told['points'] == 60 and told['summary']['downs'] == 0


def test_a_dialed_tcp_direct_link_shows_up_and_paths_take_it():
    """From the 10-30 check: a TCP direct link up and measured at both ends listed as unknown,
    recorded no trend and paths went round it through the entry relay."""
    from app.servers import mesh_path
    from app.servers.mesh_status import clean_status

    def status(peers, entry=None):
        return clean_status({'schema': 1, 'startedAt': 1, 'appliedRevision': 3, 'ready': True, 'peers': peers, 'entry': entry})
    a, b, r = 'aaaa', 'bbbb', 'rrrr'
    doc = {'members': [{'serverId': a, 'role': 'member', 'removing': False, 'entry': {'mode': 'fixed', 'candidates': [], 'autoReturn': True}},
                       {'serverId': b, 'role': 'member', 'removing': False, 'entry': {'mode': 'fixed', 'candidates': [], 'autoReturn': True}}],
           'directLinks': [{'a': a, 'b': b, 'transport': 'tcp', 'dialer': 'a'}]}
    configs = {a: {'links': [{'peer': b, 'kind': 'direct', 'dial': True, 'endpoint': 'tcp://45.32.100.4:52000'}]},
               b: {'links': [{'peer': a, 'kind': 'direct', 'dial': False}]}}
    lines = mesh_lines.catalogue(doc, configs)
    now = 10_000_000
    statuses = {a: {'status': status([{'id': b, 'kind': 'direct', 'active': True, 'transport': 'tcp', 'endpoint': 'tcp://45.32.100.4:52000',
                                       'remoteAddr': '45.32.100.4:52000', 'rttMs': 12.5}], {'relay': r, 'transport': 'udp', 'candidates': []}), 'at': now},
                b: {'status': status([{'id': a, 'kind': 'direct', 'active': True, 'transport': 'tcp', 'rttMs': 12.4}],
                                     {'relay': r, 'transport': 'udp', 'candidates': []}), 'at': now},
                r: {'status': status([]), 'at': now}}
    [line] = mesh_lines.view(lines, statuses, now)
    assert line['state'] == 'up' and line['rttMs'] == 12.5
    sample = mesh_lines.measure(lines[0], statuses, now, mesh_lines.SAMPLE_FRESH_MS)
    assert sample['up'] is True and sample['rttMs'] == 12.5
    path = mesh_path.find(a, b, {a: 'member', b: 'member', r: 'relay'}, statuses, {}, now)
    assert [h['node'] for h in path['hops']] == [a, b] and path['rttMs'] == 12.5
    # Not in use: down, and the path goes through the relay again.
    statuses[a]['status']['peers'][0]['active'] = statuses[b]['status']['peers'][0]['active'] = False
    assert mesh_lines.view(lines, statuses, now)[0]['state'] == 'down'
