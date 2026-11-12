"""BGP 上游: AS names as people know them, the graph kept of RIS's paths, and the page's view of a server's
network with a day's cache, a refresh, both families, and what is said when there is nothing to draw."""
from __future__ import annotations

import asyncio
import json
import unittest.mock

from app.servers import bgp
from tests.test_mesh_service import OWNER, server
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


def test_short_names():
    assert [bgp.short_name(h) for h in ('HURRICANE - Hurricane Electric LLC', 'RETN-AS RETN Limited', 'OVH OVH SAS', 'GOOGLE - Google LLC',
                                         'CLOUDFLARENET - Cloudflare, Inc.', 'F5 F5 Networks SARL', 'AS40676 - Psychz Networks', 'SOLO', '')] == \
        ['Hurricane Electric', 'RETN', 'OVH', 'Google', 'Cloudflare', 'F5 Networks', 'Psychz Networks', 'SOLO', '']


def route(peer, *path):
    """A RIS route as bgp-state gives it: the collector peer's path, the origin last."""
    return {'source_id': f'rrc00-{peer}', 'path': list(path)}


def test_the_graph_kept():
    routes = [route('a', 3356, 64500), route('b', 2914, 6939, 64500), route('c', 2914, 6939, 64500, 64500),
              route('d', 9999, 64500),                       # a peer at an exchange: no Tier 1, left out
              route('e', 64501, 3356, 6453, 64500),          # beyond its first Tier 1 only Tier 1s count
              route('f', 701, 1299, 64500), route('g', 701, 6453, 64500), route('h', 701, 6453, 64500), route('i', 174, 6453, 64500)]
    drawn = bgp.graph(routes, [64500])
    assert drawn['origins'] == [64500] and drawn['routes'] == 9 and drawn['via'] == 8
    assert {(e['from'], e['to']): e['peers'] for e in drawn['edges']} == {
        (64500, 3356): 1, (64500, 6939): 2, (6939, 2914): 2, (64500, 6453): 4, (64500, 1299): 1,
        # Met only behind another Tier 1: by the edge most peers came by (6453 twice, 1299 once).
        (6453, 701): 2, (6453, 174): 1}
    assert drawn['nodes'] == [174, 701, 1299, 2914, 3356, 6453, 6939, 64500]
    assert bgp.graph([route('a', 9999, 64500)], [64500]) == {'origins': [64500], 'nodes': [], 'edges': [], 'routes': 1, 'via': 0}


class Reply:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return {'status': 'ok', 'data': self.data}


class Stat:
    """RIPEstat as the answers say, by (call, resource); every request kept."""
    def __init__(self, answers, down=False):
        self.answers, self.down, self.calls = answers, down, []

    def get(self, url):
        self.calls.append(url)
        if self.down:
            raise OSError('stat.ripe.net unreachable')
        call, resource = url.split('/data/')[1].split('/')[0], url.split('resource=')[1].split('&')[0]
        return Reply(self.answers[(call, resource)])


ANSWERS = {('network-info', '45.32.100.1'): {'prefix': '45.32.96.0/20', 'asns': ['20473']},
           ('bgp-state', '45.32.96.0/20'): {'bgp_state': [route('a', 3356, 20473), route('b', 2914, 6939, 20473), route('c', 1299, 20473)]},
           ('network-info', '2001:19f0::1'): {'prefix': '2001:19f0::/38', 'asns': ['20473']},
           ('bgp-state', '2001:19f0::/38'): {'bgp_state': [route('a', 6939, 20473), route('b', 6939, 20473), route('c', 1299, 6939, 20473)]},
           ('network-info', '45.33.0.9'): {'prefix': '', 'asns': []},
           ('as-overview', 'AS20473'): {'holder': 'AS-CHOOPA - The Constant Company, LLC'},
           ('as-overview', 'AS6939'): {'holder': 'should not be asked: a known name'}}


async def test_a_servers_network_kept_a_day_refreshed_both_families_and_why_not(env):
    e = env
    sid = await server(e, 'Vultr', ipv4='45.32.100.1', online=False)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('UPDATE server_nodes SET info_json=? WHERE server_id=?', (json.dumps({'info': {'ipv4': '45.32.100.1', 'ipv6': '2001:19f0::1'}}), sid))
    stat = Stat(ANSWERS)
    with unittest.mock.patch.object(e.servers, 'http', stat):
        view = await api(e, 'GET', f'/api/servers/{sid}/bgp?family=4')
        assert (view['state'], view['ip'], view['prefix'], view['families'], view['origins'], view['error']) == ('ok', '45.32.100.1', '45.32.96.0/20', [4, 6], [20473], '')
        assert {(n['asn'], n['name'], n['tier1'], n['origin']) for n in view['nodes']} == {
            (20473, 'The Constant Company', False, True), (3356, 'Lumen', True, False), (2914, 'NTT', True, False), (1299, 'Arelion', True, False),
            (6939, 'Hurricane Electric', False, False)}
        assert {(x['from'], x['to'], x['peers']) for x in view['edges']} == {(20473, 3356, 1), (20473, 6939, 1), (6939, 2914, 1), (20473, 1299, 1)}
        assert not any('AS6939' in u or 'AS3356' in u for u in stat.calls), 'Tier 1 and known names are not asked for'
        # A day's cache: nothing asked again; 刷新 asks again.
        asked = len(stat.calls)
        assert (await api(e, 'GET', f'/api/servers/{sid}/bgp?family=4'))['atMs'] == view['atMs']
        assert len(stat.calls) == asked
        fresh = await api(e, 'POST', f'/api/servers/{sid}/bgp/refresh', {'family': 4})
        assert fresh['state'] == 'ok' and any('bgp-state' in u for u in stat.calls[asked:])
        six = await api(e, 'GET', f'/api/servers/{sid}/bgp?family=6')
        assert (six['state'], six['prefix'], six['ip']) == ('ok', '2001:19f0::/38', '2001:19f0::1')
        # Two peers reach it through HE alone, no Tier 1: only the third path is drawn.
        assert {(x['from'], x['to'], x['peers']) for x in six['edges']} == {(20473, 6939, 1), (6939, 1299, 1)} and six['via'] == 1
    # RIPEstat out of reach: what was drawn last, with why.
    with unittest.mock.patch.object(e.servers, 'http', Stat(ANSWERS, down=True)):
        down = await api(e, 'POST', f'/api/servers/{sid}/bgp/refresh', {'family': 4})
        assert down['state'] == 'ok' and down['prefix'] == '45.32.96.0/20' and down['error'] == 'OSError: stat.ripe.net unreachable'
        never = await server(e, 'Never', ipv4='45.32.100.9', online=False)
        assert (await api(e, 'GET', f'/api/servers/{never}/bgp?family=4'))['state'] == 'unavailable'
    # No public address of that family; an address no one announces.
    with unittest.mock.patch.object(e.servers, 'http', Stat(ANSWERS)):
        assert (await api(e, 'GET', f'/api/servers/{never}/bgp?family=6'))['state'] == 'no_address'
        quiet = await server(e, 'Quiet', ipv4='45.33.0.9', online=False)
        assert (await api(e, 'GET', f'/api/servers/{quiet}/bgp?family=4'))['state'] == 'not_routed'
        assert (await api(e, 'GET', f'/api/servers/{quiet}/bgp?family=5', status=400))['error'] == 'invalid_family'


async def test_one_fetch_serves_every_request_and_outlives_one_that_goes(env):
    """A request that goes away does not take the fetch from the others; a refresh does not wait on
    a fetch that may answer from the cache, and nothing is left pending (11-12 review)."""
    e = env
    sid = await server(e, 'Vultr', ipv4='45.32.100.1', online=False)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('UPDATE server_nodes SET info_json=? WHERE server_id=?', (json.dumps({'info': {'ipv4': '45.32.100.1'}}), sid))
    b, owner = e.servers.bgp, OWNER
    gates, fetched = {}, []

    async def fetch(ip, fresh):
        fetched.append(fresh)
        gates[fresh] = asyncio.Event()
        await gates[fresh].wait()
        return {'state': 'ok', 'prefix': '45.32.96.0/20', 'fresh': fresh}

    with unittest.mock.patch.object(b, '_fetch', fetch):
        first = asyncio.ensure_future(b.view(owner, sid, 4))
        second = asyncio.ensure_future(b.view(owner, sid, 4))
        await asyncio.sleep(0.05)
        refresh = asyncio.ensure_future(b.view(owner, sid, 4, fresh=True))
        await asyncio.sleep(0.05)
        assert fetched == [False, True]
        first.cancel()
        await asyncio.sleep(0.05)
        gates[False].set()
        assert (await second)['fresh'] is False
        gates[True].set()
        assert (await refresh)['fresh'] is True
        # A request while a refresh runs takes its answer.
        gates.clear()
        late_refresh = asyncio.ensure_future(b.view(owner, sid, 4, fresh=True))
        await asyncio.sleep(0.05)
        plain = asyncio.ensure_future(b.view(owner, sid, 4))
        await asyncio.sleep(0.05)
        assert fetched == [False, True, True]
        gates[True].set()
        assert (await plain)['fresh'] is True and (await late_refresh)['fresh'] is True
    assert b.pending == {}
