"""Mesh node events kept 30 days in the metrics file: each stored once however often reports
repeat it, read back newest first page by page, pruned past the retention and forgotten with the
member; then the real Web app, its API and the Servers tool."""
from __future__ import annotations

import json
import time

from app.servers import mesh_events
from app.servers.metrics import MetricsStore
from tests.test_mesh_service import OWNER, mesh_setup, settled
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

DAY_MS = 86_400_000
NOW_S = 1_793_001_600


def ev(seq, at, kind='route.changed', detail='r2: r1 -> r3 (down)'):
    return {'seq': seq, 'at': at, 'kind': kind, 'detail': detail}


async def test_stored_once_paged_by_time_and_seq_pruned_and_forgotten(tmp_path):
    metrics = MetricsStore(tmp_path / 'metrics.db')
    await metrics.start()
    log = mesh_events.EventLog(metrics)
    await log.start()
    at = NOW_S * 1000
    # A burst in one millisecond, as a link going down gives, then the same report again.
    burst = [ev(i, at) for i in range(1, 31)]
    await log.add('n1', burst, started=5, now=NOW_S)
    await log.add('n1', burst + [ev(31, at + 1000, 'peer.up', 'r1')], started=5, now=NOW_S)
    # The data plane restarted: its seq starts over.
    await log.add('n1', [ev(1, at + 2000, 'start', '')], started=6, now=NOW_S)
    # The controller's own, without a seq of its own: kept each time.
    await log.add('n1', [{'at': at + 3000, 'kind': 'mesh.reinstall', 'detail': 'x'}] * 2, now=NOW_S)
    await log.add('n2', [ev(1, at)], started=1, now=NOW_S)

    first = await log.page('n1', limit=4)
    assert first['more'] and [(e['at'] - at, e['seq'], e['kind']) for e in first['events']] == [
        (3000, None, 'mesh.reinstall'), (3000, None, 'mesh.reinstall'), (2000, 1, 'start'), (1000, 31, 'peer.up')]
    # On from the last one shown, through the burst a page at a time: none twice, none missed.
    seen, cursor = [], first['events'][-1]
    while True:
        page = await log.page('n1', cursor['at'], cursor['seq'], limit=7)
        seen += [e['seq'] for e in page['events']]
        if not page['more']:
            break
        cursor = page['events'][-1]
    assert seen == list(range(30, 0, -1))
    assert (await log.page('n1', at + 1000))['events'][0]['seq'] == 30
    assert await log.page('nobody') == {'events': [], 'more': False}

    # Past 30 days, gone with the hourly prune; the other node's too.
    later = NOW_S + mesh_events.RETENTION_S + 3600
    await log.add('n1', [ev(2, later * 1000, 'stop', '')], started=6, now=later)
    assert [e['kind'] for e in (await log.page('n1'))['events']] == ['stop']
    assert (await log.page('n2'))['events'] == []

    await log.add('n2', [ev(2, later * 1000)], started=1, now=later)
    await log.forget('n2')
    assert await log.page('n2') == {'events': [], 'more': False}
    assert (await log.page('n1'))['events']
    await metrics.close()


async def test_reports_store_every_event_and_the_api_and_tool_page_them(env):  # noqa: F811
    from app.tools import servers as tool_module
    from app.tools.base import ToolRegistry, ToolRuntimeContext
    e = env
    nodes, ids = await mesh_setup(e)
    sid = ids['m1']
    await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], sid]})
    draft = await api(e, 'GET', '/api/servers/mesh')
    draft['members'][0]['role'] = 'relay'
    await api(e, 'PUT', '/api/servers/mesh/draft', {'members': draft['members'], 'draftVersion': (await api(e, 'GET', '/api/servers/mesh'))['draftVersion']})
    await api(e, 'POST', '/api/servers/mesh/apply')
    await settled(e)
    report = {'schema': 1, 'startedAt': 5, 'appliedRevision': 1, 'ready': True, 'iface': 'obmesh0', 'error': '', 'peers': [], 'entry': None,
              'resources': {}}
    # 250 events, more than the controller holds, each report with the node's latest hundred.
    base = int(time.time() * 1000) - 600_000
    sent = []
    for i in range(1, 251):
        sent.append(ev(i, base + i * 1000, 'route.changed', f'd{i}: r1 -> r2 (better)'))
        await e.servers.mesh.report(sid, {**report, 'events': sent[-100:]})
    held = await api(e, 'GET', f'/api/servers/mesh/members/{sid}')
    assert len(held['events']) == 100 and held['events'][-1]['seq'] == 151
    # The controller restarts and the node sends its hundred again: stored once.
    e.servers.mesh.status.clear()
    await e.servers.mesh.start()
    await e.servers.mesh.report(sid, {**report, 'events': sent[-100:]})

    stored, before, before_seq = [], None, None
    while True:
        query = '&'.join(f'{k}={v}' for k, v in (('before', before), ('beforeSeq', before_seq), ('limit', 120)) if v is not None)
        page = await api(e, 'GET', f'/api/servers/mesh/members/{sid}/events?{query}')
        stored += page['events']
        if not page['more']:
            break
        before, before_seq = page['events'][-1]['at'], page['events'][-1]['seq']
    assert [x['seq'] for x in stored] == list(range(250, 0, -1)) and stored[-1] == sent[0]
    assert (await api(e, 'GET', '/api/servers/mesh/members/nope/events', status=404))['error'] == 'mesh_member_not_found'

    # The tool reads the same history, a page at a time.
    registry = ToolRegistry()
    tool_module.register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-ev',780,?,'t')", (OWNER,))
    ctx = ToolRuntimeContext(chat_id=780, conversation_uuid='conv-ev')

    async def call(**params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': 'mesh_member', 'params': params}), context=ctx))
    got = await call(serverId=sid, include=['events'], eventsBefore='now', eventsLimit=200)
    assert [x['seq'] for x in got['events']][:2] == [250, 249] and len(got['events']) == 200
    assert got['pages']['events'] == {'more': True, 'eventsBefore': base + 51_000, 'eventsBeforeSeq': 51}
    rest = await call(serverId=sid, include=['events'], eventsBefore=base + 51_000, eventsBeforeSeq=51)
    assert [x['seq'] for x in rest['events']] == list(range(50, 0, -1)) and rest['pages']['events'] == {'more': False}
    assert (await call(serverId=sid, include=['events'], eventsBefore='yesterday'))['error'] == 'invalid_parameters'


async def test_paging_keeps_controller_events_with_null_seq_at_same_millisecond(tmp_path):
    metrics = MetricsStore(tmp_path / 'metrics.db')
    await metrics.start()
    log = mesh_events.EventLog(metrics)
    await log.start()
    at = NOW_S * 1000
    # A node event and a controller event (seq=NULL) at the same millisecond, plus an older event.
    await log.add('n1', [ev(4, at - 100, 'k', 'older'), ev(5, at, 'k', 'node')], started=5, now=NOW_S)
    await log.add('n1', [{'at': at, 'kind': 'relay.mode', 'detail': 'ctrl'}], now=NOW_S)
    seen, before, before_seq = [], None, None
    while True:
        page = await log.page('n1', before, before_seq, limit=1)
        seen.extend(e['detail'] for e in page['events'])
        if not page['more']:
            break
        before, before_seq = page['events'][-1]['at'], page['events'][-1]['seq']
    assert seen == ['node', 'ctrl', 'older']

    # Two controller events (both seq=NULL) at the exact same millisecond stay intact across pages.
    await log.add('n2', [{'at': at - 100, 'kind': 'k', 'detail': 'older'}], now=NOW_S)
    await log.add('n2', [{'at': at, 'kind': 'a', 'detail': 'ctrl1'}, {'at': at, 'kind': 'b', 'detail': 'ctrl2'}], now=NOW_S)
    seen2, before, before_seq = [], None, None
    while True:
        page = await log.page('n2', before, before_seq, limit=1)
        seen2.extend(e['detail'] for e in page['events'])
        if not page['more']:
            break
        before, before_seq = page['events'][-1]['at'], page['events'][-1]['seq']
    assert seen2 == ['ctrl2', 'ctrl1', 'older']
    await metrics.close()
