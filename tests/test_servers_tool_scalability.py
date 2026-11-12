"""Scalability, pagination, field projection, server-name resolution, incremental mesh draft patching,
revision config diffs, live mesh_member refresh and batched job_start for the Servers tool."""
from __future__ import annotations

import copy
import json

import pytest

from app.servers import mesh_config
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.servers import (
    DRAFT_BUDGET,
    MAX_BATCH_TRANSFERS,
    OUTPUT_BUDGET,
    _list_fit,
    _mesh_draft,
    _mesh_member,
    _mesh_overview,
    _mesh_routes,
    describe,
    register_server_tools,
)
from tests.test_mesh_service import (  # noqa: F401
    OWNER,
    Nodes,
    api,
    applied_base,
    base_env,
    env,
    mesh_setup,
    server,
    settled,
    shipped,
)
from tests.test_servers import _online, start_fake, wait_for

TOOL_MAX_CHARS = 32000


async def _caller(e, chat_id=910, conversation='conv-scale-1', title='组网工具测试', turn='turn-scale-1'):
    registry = ToolRegistry()
    register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute(
            'INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES(?,?,?,?)',
            (conversation, chat_id, OWNER, title),
        )
    ctx = ToolRuntimeContext(chat_id=chat_id, conversation_uuid=conversation, turn_uuid=turn)

    async def call(action, **params):
        raw = await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=ctx)
        assert len(raw) <= TOOL_MAX_CHARS, f'{action} output exceeded framework cap: {len(raw)}'
        return json.loads(raw)

    return call


def test_describe_topics_stay_within_tool_cap_and_expose_new_actions():
    idx = describe()
    assert 'mesh_revisions' in idx['actions']
    for topic in (None, 'diagnostics', 'browser_all', 'commands', 'files', 'mesh_all', 'ddns', 'bench'):
        encoded = json.dumps(describe(topic), ensure_ascii=False)
        assert len(encoded) <= TOOL_MAX_CHARS
    assert MAX_BATCH_TRANSFERS >= 200


def test_sections_too_large_for_the_output_are_cut_like_the_whole_view():
    """mesh section=["lines", "rules"] past the budget falls back to the overview's cuts, which
    found no nodes, settings or members in it (11-12 review: KeyError 'nodes')."""
    picked = {'ok': True, 'applied': {'revision': 3},
              'lines': [{'id': f'l{i}', 'kind': 'relay', 'state': 'up', 'ends': [{'address': '203.0.113.1:51820'}] * 4, 'rttMs': 12.5} for i in range(400)],
              'rules': [{'id': f'r{i}', 'from': {'kind': 'any'}, 'to': {'kind': 'any'}, 'ports': 'all', 'note': 'x' * 40} for i in range(300)],
              'autoRules': [{'id': f'a{i}'} for i in range(50)]}
    out = _mesh_overview(copy.deepcopy(picked))
    assert len(json.dumps(out, ensure_ascii=False)) <= OUTPUT_BUDGET and out['truncated']['omitted']
    nodes = {'ok': True, 'nodes': {f'n{i}': {'untrustedData': {'error': 'e' * 200}} for i in range(300)}}
    assert len(json.dumps(_mesh_overview(nodes), ensure_ascii=False)) <= OUTPUT_BUDGET


def test_unit_auto_pagination_at_150_plus_scale_loses_no_items():
    # 1. 180 servers through _list_fit: walks pages via nextOffset with every page <= OUTPUT_BUDGET
    sample_row = {
        'id': 'srv-0', 'name': 'Node-0', 'group': 'prod', 'tags': ['edge', 'hk'], 'region': 'HK',
        'provider': 'ExampleCloud', 'state': 'online', 'ipv4': '203.0.113.10', 'cpuPct': 12.5, 'memPct': 44.1,
        'diskPct': 31.0, 'rxBytesPerSec': 1024, 'txBytesPerSec': 2048, 'uptimeSec': 86400, 'latencyMs': 31.2,
        'traffic': {'usedGB': 120.4, 'limitGB': 1000.0, 'pct': 12.04, 'resetAt': '2026-12-01'},
        'billing': {'price': 15, 'currency': 'USD', 'cycle': 'month', 'expiresAt': '2026-12-01', 'daysLeft': 20},
        'node': {'version': '0.20.0', 'build': 'abcdef123456', 'source': 'release', 'updateState': 'current',
                 'target': {'version': '0.20.0', 'source': 'release', 'build': 'abcdef123456'}},
    }
    rows = [{**copy.deepcopy(sample_row), 'id': f'srv-{i:03d}', 'name': f'Server-{i:03d}'} for i in range(180)]
    collected, offset, pages = [], 0, 0
    while True:
        res = _list_fit({'ok': True, 'count': len(rows), 'items': rows}, offset=offset, explicit_page=(offset > 0))
        assert len(json.dumps(res, ensure_ascii=False)) <= OUTPUT_BUDGET + 100
        collected.extend(r['id'] for r in res['items'])
        pages += 1
        if not (res.get('page') or {}).get('hasMore'):
            break
        offset = res['page']['nextOffset']
    assert pages >= 2
    assert collected == [f'srv-{i:03d}' for i in range(180)]

    # 2. 140 mesh members + 400 lines + bloated auto/autoLinks through _mesh_overview & _mesh_draft
    members, nodes = [], {}
    for i in range(140):
        sid = f'm-{i:03d}'
        members.append({
            'serverId': sid, 'name': f'Mesh-{i:03d}', 'platform': 'linux', 'role': 'relay' if i < 15 else 'member',
            'relayMode': 'public', 'address': f'100.96.0.{(i % 250) + 1}', 'controlAddress': f'100.96.255.{(i % 250) + 1}',
            'publicEndpoint': '', 'publicEndpointAuto': f'198.51.100.{(i % 250) + 1}', 'direct': True, 'removing': False,
            'listen': {'udp': 51999, 'tcp': 52000, 'tls': 52001}, 'tags': [], 'gateway': None, 'proxy': None,
            'uploadLimitMbps': None, 'keepRoutes': [], 'auto': [],
            'entry': {'mode': 'upstreams', 'autoReturn': True,
                      'candidates': [{'id': 'c1', 'relay': 'm-000', 'transport': 'udp'}, {'id': 'c2', 'relay': 'm-001', 'transport': 'tcp'}]},
        })
        nodes[sid] = {
            'state': 'ready', 'appliedRevision': 9, 'dataVersion': '1.0.0', 'error': '',
            'summary': {'ready': True, 'peers': 30, 'rttMs': 25.0,
                        'routing': {'mode': 'dynamic', 'links': [{'peer': f'm-{k:03d}'} for k in range(12)],
                                    'routes': [{'dst': f'm-{k:03d}'} for k in range(80)]},
                        'entryHealth': [{'id': 'c1', 'relay': 'm-000', 'state': 'up', 'via': 'public', 'rttMs': 11.2}]},
        }
    big_view = {
        'ok': True, 'applied': {'revision': 9, 'at': 1000}, 'apply': {'running': False, 'revision': 9, 'progress': []},
        'draft': {'dirty': False, 'changes': []}, 'errors': [], 'warnings': [], 'installing': [], 'routeClashes': [],
        'settings': {'prefix': '100.96.0.0/16', 'mtu': 1380, 'health': {'leaseMs': 60000}},
        'members': members, 'nodes': nodes,
        'relayLinks': {'mode': 'custom', 'pairs': [{'a': f'm-{i:03d}', 'b': f'm-{i + 1:03d}', 'transport': 'udp', 'cost': 20} for i in range(45)]},
        'directLinks': [], 'rules': [{'id': f'r{i}', 'src': {'all': True}, 'dst': {'all': True}, 'proto': 'any', 'note': 'allow'} for i in range(25)],
        'autoRules': [], 'exports': [{'gateway': 'm-000', 'subnet': '10.10.0.0/24', 'nodes': [f'm-{i:03d}' for i in range(140)]}],
        'eligible': [], 'limits': [], 'lineRules': [], 'exitGroups': [], 'paths': [], 'pathsView': [],
        'lines': [{'id': f'ln-{i}', 'kind': 'entry', 'a': f'm-{(i % 120) + 15:03d}', 'b': f'm-{i % 15:03d}', 'state': 'up',
                   'ends': {'a': {'local': '0.0.0.0:51999', 'remote': '1.2.3.4:51999'}}} for i in range(400)],
        'autoLinks': {'watching': [{'id': f'w{i}', 'pairs': [[f'a{j}', f'b{j}', 99] for j in range(60)]} for i in range(5)], 'benefits': {}},
        'auto': {'members': {f'm-{i:03d}': {'explain': 'x' * 200} for i in range(140)}},
    }

    seen_ov, off = [], 0
    while True:
        ov = _mesh_overview(big_view, offset=off, explicit_page=(off > 0))
        assert len(json.dumps(ov, ensure_ascii=False)) <= OUTPUT_BUDGET
        assert list(ov.keys())[:4] == ['ok', 'applied', 'apply', 'draft']
        real_members = [m for m in ov['members'] if 'serverId' in m]
        assert set(ov['nodes']) == {m['serverId'] for m in real_members}
        seen_ov.extend(m['serverId'] for m in real_members)
        if not (ov.get('page') or {}).get('hasMore'):
            break
        off = ov['page']['nextOffset']
    assert seen_ov == [f'm-{i:03d}' for i in range(140)]

    seen_dr, off = [], 0
    while True:
        dr = _mesh_draft(big_view, offset=off, explicit_page=(off > 0))
        assert len(json.dumps(dr, ensure_ascii=False)) <= DRAFT_BUDGET + 200 <= TOOL_MAX_CHARS
        seen_dr.extend(m['serverId'] for m in dr['members'])
        if not (dr.get('page') or {}).get('hasMore'):
            break
        off = dr['page']['nextOffset']
    assert seen_dr == [f'm-{i:03d}' for i in range(140)]

    # 3. _mesh_routes pagination at 160 routes
    routes_table = {'ok': True, 'subnets': [], 'routes': [
        {'dst': f'm-{i:03d}', 'rttMs': 35.0, 'stale': False,
         'hops': [{'node': 'm-000', 'via': '', 'rttMs': 0}, {'node': 'm-001', 'via': 'relay', 'rttMs': 20}, {'node': f'm-{i:03d}', 'via': 'entry', 'rttMs': 15}]}
        for i in range(160)
    ]}
    r_p1 = _mesh_routes(routes_table, offset=0, limit=50, explicit_page=True)
    assert len(r_p1['routes']) == 50 and r_p1['page'] == {'target': 'routes', 'offset': 0, 'returned': 50, 'total': 160, 'hasMore': True, 'nextOffset': 50}

    # 4. _mesh_member shrinks large config.peers within budget
    huge_member = {
        'ok': True, 'member': members[0], 'node': nodes['m-000'],
        'events': [{'seq': i, 'at': i, 'kind': 'peer.up', 'detail': 'ok' * 30} for i in range(60)],
        'status': {'peers': [{'id': f'p-{i}', 'state': 'up', 'tx': 1000, 'rx': 2000} for i in range(60)],
                   'owners': {f'100.96.0.{i}': f'm-{i:03d}' for i in range(100)},
                   'routing': {'routes': [{'dst': f'm-{i:03d}', 'cost': 10} for i in range(60)]}},
        'config': {'revision': 9, 'nodes': [{'id': f'm-{i:03d}', 'name': f'N{i}', 'role': 'member', 'address': '100.96.0.1', 'pubKey': 'K' * 44} for i in range(80)],
                   'peers': [{'id': f'p-{i}', 'publicKey': 'P' * 44, 'allowedIPs': ['100.96.0.0/16']} for i in range(60)],
                   'routes': [{'dst': f'100.96.0.{i}/32'} for i in range(60)], 'rules': [{'id': f'r{i}'} for i in range(40)]},
    }
    shaped_m = _mesh_member(huge_member)
    assert len(json.dumps(shaped_m, ensure_ascii=False)) <= OUTPUT_BUDGET


async def test_name_resolution_filters_patch_revisions_and_live_member(env):  # noqa: F811
    e = env
    call = await _caller(e)
    nodes, ids, draft = await applied_base(e)
    # Give distinct human names to relays and members, plus a duplicate pair to test ambiguity guard
    await api(e, 'PATCH', f"/api/servers/{ids['r1']}", {'name': 'HK-Relay-1', 'profile': {'group': '中转', 'region': 'HK', 'tags': ['core']}})
    await api(e, 'PATCH', f"/api/servers/{ids['m1']}", {'name': 'GZ-Exit', 'profile': {'group': '出口', 'region': 'CN', 'tags': ['nat']}})
    await api(e, 'PATCH', f"/api/servers/{ids['m2']}", {'name': 'SG-App', 'profile': {'group': '业务', 'region': 'SG'}})
    dup1 = await server(e, 'Dup-Name', ipv4='198.51.100.91')
    dup2 = await server(e, 'Dup-Name', ipv4='198.51.100.92')
    assert dup1 != dup2

    # 1. list filtering, field projection and pagination
    hk_only = await call('list', region='hk', fields=['name', 'region', 'state', 'node.updateState'])
    assert hk_only['count'] == 1 and hk_only['items'][0]['id'] == ids['r1']
    assert set(hk_only['items'][0].keys()) <= {'id', 'name', 'region', 'state', 'node'}
    assert 'updateState' in hk_only['items'][0]['node'] and 'version' not in hk_only['items'][0]['node']

    paged_list = await call('list', offset=1, limit=2, compact=True)
    assert paged_list['count'] == 2 and paged_list['page']['offset'] == 1 and paged_list['page']['limit'] if 'limit' in paged_list['page'] else True
    assert paged_list['page']['returned'] == 2 and paged_list['page']['nextOffset'] == 3
    # By IP; compact rows keep it, and fields given beside compact are what come back.
    by_ip = await call('list', query='198.51.100.92', compact=True)
    assert [(r['id'], r['ip']) for r in by_ip['items']] == [(dup2, '198.51.100.92')]
    both = await call('list', query='198.51.100.92', compact=True, fields=['ip', 'billing.price'])
    assert both['items'] == [{'id': dup2, 'ip': '198.51.100.92', 'billing': {'price': 0}}]
    # total is every server whether paged or not; what matched, the page's.
    every = len((await call('list'))['items'])
    dups = await call('list', query='dup-name', compact=True, limit=1)
    assert (dups['total'], dups['page']['total'], by_ip['total']) == (every, 2, every)

    # 2. Ambiguous server name is rejected cleanly before hitting any node; unique name resolves case-insensitively
    amb = await call('get', serverId='Dup-Name')
    assert amb['ok'] is False and amb['error'] == 'server_name_ambiguous' and 'untrustedData' not in amb
    got = await call('get', serverId='gz-exit')
    assert got['ok'] is True and got['item']['id'] == ids['m1']

    # 3. Incremental mesh_save_draft patch using server names (without resending all members!)
    path_feats = ('mesh', mesh_config.MEMBERS_ROUTING_FEATURE, mesh_config.PATHS_FEATURE, mesh_config.PATHS_BACKUP_FEATURE)
    for sid in ids.values():
        if sid in e.servers.hub.sessions:
            e.servers.hub.sessions[sid].features |= frozenset(path_feats)
    r2_id = await server(e, 'JP-Relay-2', ipv4='45.32.200.2', features=path_feats)
    lnx_id = await server(e, 'SG-Linux', ipv4='45.32.200.3', features=path_feats)
    joined = await call('mesh_join', serverIds=['JP-Relay-2', 'SG-Linux'])
    assert joined['ok'] is True

    patched = await call(
        'mesh_save_draft',
        patch={
            'members': [
                {'serverId': 'JP-Relay-2', 'role': 'relay', 'listen': {'tcp': 52010}},
                {'serverId': 'GZ-Exit', 'uploadLimitMbps': 66,
                 'entry': {'mode': 'failover', 'addCandidates': [{'relay': 'JP-Relay-2', 'transport': 'tcp'}]}},
                {'serverId': 'SG-Linux',
                 'entry': {'mode': 'upstreams', 'candidates': [{'relay': 'HK-Relay-1', 'transport': 'udp'}, {'relay': 'JP-Relay-2', 'transport': 'tcp'}]}},
            ],
            'relayLinks': {'mode': 'custom', 'upsert': [{'a': 'HK-Relay-1', 'b': 'JP-Relay-2', 'transport': 'tcp', 'cost': 35, 'dialer': 'a'}]},
            'paths': {'upsert': [{'on': True, 'from': 'SG-Linux', 'to': 'HK-Relay-1', 'via': ['JP-Relay-2']}]},
            'rules': {'upsert': [{'src': {'members': ['GZ-Exit']}, 'dst': {'members': ['SG-App']}, 'proto': 'tcp', 'ports': [[443, 443]], 'note': 'https'}]},
        },
    )
    assert patched['ok'] is True and patched['draft']['dirty'] is True and not patched['errors']

    # Read filtered & projected draft for GZ-Exit and SG-Linux by name
    gz_draft = await call('mesh_draft', serverIds=['GZ-Exit', 'SG-Linux'], parts=['members', 'paths'], fields=['role', 'uploadLimitMbps', 'entry'])
    by_sid = {m['serverId']: m for m in gz_draft['members']}
    assert by_sid[ids['m1']]['uploadLimitMbps'] == 66
    assert {c['relay'] for c in by_sid[ids['m1']]['entry']['candidates']} == {ids['r1'], r2_id}
    assert len(gz_draft['paths']) == 1 and gz_draft['paths'][0]['via'] == [r2_id] and gz_draft['paths'][0]['from'] == lnx_id

    # Reject overlapping top-level and patch keys
    conflict = await call('mesh_save_draft', settings={'mtu': 1380}, patch={'settings': {'mtu': 1360}})
    assert conflict['ok'] is False and conflict['error'] == 'invalid_mesh_draft'

    # Apply and inspect mesh_revisions + per-node config diff by server name
    applied = await call('mesh_apply')
    assert applied['ok'] is True and applied['applied']['revision'] == 2
    await settled(e)

    rev_list = await call('mesh_revisions', limit=1, offset=0)
    assert rev_list['ok'] is True and len(rev_list['revisions']) == 1 and rev_list['page']['hasMore'] is True
    rev_diff = await call('mesh_revisions', revision=2, serverId='GZ-Exit', fields=['self', 'entry'])
    assert rev_diff['ok'] is True and rev_diff['comparedTo'] == 1
    assert ids['m1'] in rev_diff['changedNodes']
    assert 'before' in rev_diff['changedNodes'][ids['m1']] and 'after' in rev_diff['changedNodes'][ids['m1']]

    # 4. mesh section & pagination, and mesh_member live fresh=True + sub-collection pagination + batch serverIds
    sec_members = await call('mesh', section='members', offset=0, limit=2, fields=['name', 'role', 'address'])
    assert len(sec_members['members']) == 2 and sec_members['page']['nextOffset'] == 2

    # Inject a live status payload returned by mesh.status RPC on GZ-Exit
    live_status = {
        'appliedRevision': 2, 'ready': True, 'error': '',
        'entry': {'relay': r2_id, 'transport': 'tcp'},
        'peers': [{'id': f'peer-{k}', 'kind': 'entry', 'active': k == 0, 'rttMs': 12 + k} for k in range(15)],
        'routing': {'mode': 'dynamic', 'links': [], 'routes': [{'dst': f'100.96.0.{k}', 'cost': k} for k in range(12)]},
    }
    orig_call = nodes.call

    async def status_with_live(server_id, method, params=None, timeout=30):
        ans = await orig_call(server_id, method, params, timeout)
        if method == 'mesh.status' and server_id == ids['m1']:
            ans = copy.deepcopy(ans)
            ans['result']['status'] = live_status
        return ans

    e.servers.hub.call = status_with_live
    live_m = await call('mesh_member', serverId='GZ-Exit', fresh=True, include=['status'], peersOffset=5, peersLimit=4)
    assert live_m['ok'] is True and live_m['fresh'] is True
    assert [p['id'] for p in live_m['status']['peers']] == ['peer-5', 'peer-6', 'peer-7', 'peer-8']
    assert live_m['pages']['peers'] == {'target': 'status.peers', 'offset': 5, 'returned': 4, 'total': 15, 'hasMore': True, 'nextOffset': 9}

    batch_m = await call('mesh_member', serverIds=['HK-Relay-1', 'GZ-Exit', 'SG-App'])
    assert batch_m['ok'] is True and batch_m['count'] == 3


async def test_batched_job_start_by_names_and_exec_timeout_sec_alias(env):  # noqa: F811
    e = env
    call = await _caller(e, chat_id=911, conversation='conv-batch-job', title='批量启动任务')
    s1 = (await api(e, 'POST', '/api/servers', {'name': 'Worker-Alpha', 'profile': {'group': 'workers'}}))['item']['id']
    s2 = (await api(e, 'POST', '/api/servers', {'name': 'Worker-Beta', 'profile': {'group': 'workers'}}))['item']['id']
    fakes = []
    for sid in (s1, s2):
        fakes.append((await start_fake(e, sid))[0])
        await wait_for(lambda sid=sid: _online(e, sid))
    try:
        # exec accepts server names and timeoutSec alias
        ex = await call('exec', serverIds=['Worker-Alpha', 'Worker-Beta'], command='echo ready-ok', timeoutSec=15)
        assert ex['ok'] is True and [r['stdout'].strip() for r in ex['results']] == ['ready-ok', 'ready-ok']

        # job_start accepts serverIds (with names) and waits in one call
        started = await call('job_start', serverIds=['Worker-Alpha', 'Worker-Beta'], command='echo batch-job-done', until='batch-job-done', waitSeconds=10)
        assert started['ok'] is True and started['count'] == 2
        assert [r['wait']['reason'] for r in started['results']] == ['matched', 'matched']
    finally:
        for fn in fakes:
            await fn.kill()
