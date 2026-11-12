"""三网回程: the line read from a trace's hops, the daily slot, a check through the node's own trace,
and the settings switches that stop both automatic checks and hide their results."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.servers import route
from tests.test_mesh_service import server
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

OVH = [['203.0.113.254', 16276, 'OVH SAS', 'SG'], ['10.133.2.82', None, '', '']]


def hops(*rows, reached=True):
    """Hops from [ip, asn, org, country] rows; the last one is the target when reached."""
    out = [{'ttl': i + 1, 'ip': r[0], 'asn': r[1], 'org': r[2], 'country': r[3], 'rttMs': 10.0 + i, 'reached': False} for i, r in enumerate(rows)]
    if reached:
        out[-1]['reached'] = True
    return out


# Traces from a server at OVH Singapore, 2026-11-05.
TATA_TO_BEIJING_UNICOM = hops(*OVH, ['80.231.165.25', 6453, 'TATA COMMUNICATIONS (AMERICA) INC', 'GB'],
                              ['125.33.187.66', 4808, 'China Unicom Beijing Province Network', 'CN'],
                              ['61.149.213.38', 4808, 'China Unicom Beijing Province Network', 'CN'], reached=False)
ONLY_THE_TARGET = hops(*OVH, ['195.219.68.127', 6453, 'TATA COMMUNICATIONS (AMERICA) INC', 'DE'],
                       ['58.246.163.108', 140979, 'China Unicom Shanghai FuTe IDC network', 'CN'])
NTT_TO_CMI = hops(*OVH, ['129.250.2.183', 2914, 'NTT America, Inc.', 'US'], ['223.120.2.117', 58453, 'China Mobile International Limited', 'CN'],
                  ['221.183.92.21', 9808, 'China Mobile Communications Group Co., Ltd.', 'CN'],
                  ['120.198.26.254', 56040, 'China Mobile communications corporation', 'CN'], reached=False)
NTT_TO_163 = hops(*OVH, ['129.250.6.25', 2914, 'NTT America, Inc.', 'US'], ['202.97.67.221', 4134, 'Chinanet', 'CN'],
                  ['101.226.101.195', 4811, 'China Telecom (Group)', 'CN'])


def test_lines_from_real_traces():
    assert route.classify(NTT_TO_163) == '163'
    assert route.classify(TATA_TO_BEIJING_UNICOM) == '4837', 'a provincial Unicom network is the ordinary line'
    assert route.classify(NTT_TO_CMI) == 'CMI'
    assert route.classify(ONLY_THE_TARGET) == '', 'only the target answered from China: the route is hidden'
    assert route.classify(hops(*OVH, ['129.250.2.183', 2914, 'NTT America, Inc.', 'US'], reached=False)) == ''
    assert route.classify([]) == ''


@pytest.mark.parametrize('path, line', [
    # CN2 all the way back, its addresses unknown to GeoIP.
    ([['59.43.182.101', None, '', 'CN'], ['59.43.246.230', None, '', 'CN'], ['220.181.0.1', 4847, 'China Networks Inter-Exchange', 'CN']], 'CN2 GIA'),
    # CN2 abroad, then the 163 backbone.
    ([['59.43.187.118', None, '', 'CN'], ['202.97.12.33', 4134, 'Chinanet', 'CN'], ['219.141.1.1', 4847, '', 'CN']], 'CN2 GT'),
    ([['218.105.2.1', 9929, 'China Netcom Corp.', 'CN'], ['219.158.3.1', 4837, 'China Unicom', 'CN']], '9929'),
    ([['43.252.86.1', 10099, 'China Unicom Global', 'HK'], ['219.158.3.1', 4837, 'China Unicom', 'CN']], '10099'),
    ([['223.120.128.1', 58807, 'China Mobile International Limited', 'HK'], ['221.183.1.1', 9808, '', 'CN']], 'CMIN2'),
    ([['69.194.171.1', 23764, 'CTGNet', 'HK'], ['202.97.1.1', 4134, 'Chinanet', 'CN']], 'CTG'),
    ([['221.183.55.1', 9808, 'China Mobile Communications Group Co., Ltd.', 'CN'], ['211.136.1.1', 56048, '', 'CN']], 'CMNET'),
    ([['101.4.1.1', 4538, 'China Education and Research Network Center', 'CN'], ['166.111.1.1', 4538, '', 'CN']], 'CERNET'),
])
def test_lines_by_the_networks_crossed(path, line):
    assert route.classify(hops(*OVH, *path)) == line
    assert route.TIERS[line] == {'CN2 GIA': 'gold', 'CTG': 'gold', '9929': 'gold', 'CMIN2': 'gold'}.get(line, route.TIERS[line])


def test_the_daily_slot_is_each_servers_own_minute_after_four():
    zone = ZoneInfo('Asia/Shanghai')
    at = lambda *t: int(datetime(*t, tzinfo=zone).timestamp() * 1000)  # noqa: E731
    minute = datetime.fromtimestamp(route.slot('srv-a', at(2026, 11, 5, 12, 0), zone) / 1000, zone)
    assert (minute.date().day, minute.hour) == (5, 4)
    before = datetime.fromtimestamp(route.slot('srv-a', at(2026, 11, 5, 3, 59), zone) / 1000, zone)
    assert (before.day, before.hour, before.minute) == (4, 4, minute.minute), 'before four the last slot was yesterday'
    assert len({route.slot(f's{i}', at(2026, 11, 5, 12), zone) for i in range(40)}) > 10, 'servers spread over the hour'


async def test_a_check_traces_nine_targets_and_the_switches_stop_and_hide_it(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(route, 'POLL_S', 0.01)
    started, polls = [], {}
    paths = {'ct': NTT_TO_163, 'cu': TATA_TO_BEIJING_UNICOM, 'cm': NTT_TO_CMI}

    async def call(server_id, method, params, timeout):
        if method == 'net.probe.start':
            assert {k: v for k, v in params.items() if k != 'target'} == route.PROBE
            started.append(params['target'])
            if params['target'].startswith('sh-cm') and 'busy' not in polls:
                polls['busy'] = 1
                return {'ok': False, 'error': 'probe_busy'}
            return {'ok': True, 'result': {'id': params['target']}}
        assert method == 'net.probe.status'
        target = params['id']
        polls[target] = polls.get(target, 0) + 1
        if polls[target] < 2:
            return {'ok': True, 'result': {'state': 'running', 'hops': []}}
        # The node forgot this one (it restarted, say): only that target fails.
        if target.startswith('gd-cu'):
            return {'ok': False, 'error': 'not_found'}
        trace = paths[target.split('-')[1]]
        return {'ok': True, 'result': {'state': 'done', 'reached': trace[-1]['reached'], 'hops': [
            {'ttl': h['ttl'], 'addrs': [{'ip': h['ip'], 'name': ''}], 'avgMs': h['rttMs'], 'reached': h['reached']} for h in trace]}}
    env.servers.hub.call = call
    networks = {h['ip']: {'country': h['country'], 'countryName': '', 'asn': h['asn'], 'org': h['org']} for t in paths.values() for h in t if h['asn']}

    async def fetch(ips):
        return {ip: networks[ip] for ip in ips if ip in networks}
    env.servers.net.networks.fetch = fetch
    monkeypatch.setattr(route.asyncio, 'sleep', _fast(asyncio.sleep))
    sid = await server(env, 'Fra', ipv4='45.32.100.1', features=('mesh', 'net'))
    result = (await api(env, 'POST', f'/api/servers/{sid}/route'))['route']
    assert sorted(started) == sorted([t for _, t in route.TARGETS] + ['sh-cm-v4.ip.zstaticcdn.com'])
    assert set(result['results']) == {k for k, _ in route.TARGETS} and result['ip'] == '45.32.100.1'
    bj = result['results']
    assert (bj['bj-ct']['line'], bj['bj-ct']['tier'], bj['bj-cu']['line'], bj['gd-cm']['line'], bj['gd-cm']['tier']) == ('163', 'plain', '4837', 'CMI', 'plain')
    assert bj['gd-cu'] == {'line': '', 'tier': '', 'rttMs': None, 'reached': False, 'error': 'not_found', 'hops': []}
    assert bj['sh-cu']['line'] == '4837'
    assert bj['bj-ct']['rttMs'] == NTT_TO_163[-1]['rttMs'] and bj['bj-ct']['reached'] and not bj['bj-cu']['reached']
    assert bj['bj-cu']['hops'][-1] == {'ttl': 5, 'ip': '61.149.213.38', 'rttMs': 14.0, 'reached': False, 'asn': 4808,
                                       'org': 'China Unicom Beijing Province Network', 'country': 'CN'}
    shown = (await api(env, 'GET', f'/api/servers/{sid}'))['item']['node']
    assert shown['route']['results']['sh-ct']['line'] == '163' and 'v' not in shown['route']
    # Checked before CMI counted as ordinary: shown as it would be today, the stored result as it was.
    old = {**result, 'v': route.VERSION, 'results': {**result['results'], 'gd-cm': {**result['results']['gd-cm'], 'tier': 'silver'},
                                                      'bj-cu': {**result['results']['bj-cu'], 'line': 'AS65000', 'tier': 'silver'}}}
    await env.servers._merge_node_info(sid, {'route': old})
    shown = (await api(env, 'GET', f'/api/servers/{sid}'))['item']['node']['route']['results']
    assert shown['gd-cm']['tier'] == 'plain' and shown['bj-cu']['tier'] == 'silver', 'a line the table does not know keeps its tier'
    assert route.retier(None) is None and route.retier({'at': 1}) == {'at': 1}

    # Due again tomorrow after the server's slot, or at once with a new address.
    stored = {'route': {**result, 'v': route.VERSION}}
    zone = ZoneInfo('Asia/Shanghai')
    assert not env.servers.route.due(sid, stored, {'ipv4': '45.32.100.1'}, zone)
    assert env.servers.route.due(sid, stored, {'ipv4': '45.32.100.2'}, zone)
    assert env.servers.route.due(sid, {'route': {**stored['route'], 'at': result['at'] - 86400000}}, {'ipv4': '45.32.100.1'}, zone)
    assert not env.servers.route.due(sid, {}, {'ipv6': '2a0e::1'}, zone), 'only IPv4 is traced'

    # Off: not shown, not run, the result kept for when it is on again.
    saved = (await api(env, 'PUT', '/api/servers/settings', {'checks': {'route': False}}))['settings']
    assert saved['checks'] == {'unlock': True, 'route': False, 'ipcheck': True}
    shown = (await api(env, 'GET', f'/api/servers/{sid}'))['item']['node']
    assert shown['route'] is None
    assert (await api(env, 'POST', f'/api/servers/{sid}/route', status=409))['error'] == 'route_check_disabled'
    env.servers.route.automatic = True
    count = len(started)
    env.servers.route.soon(sid)
    await asyncio.sleep(0.05)
    assert len(started) == count and not env.servers.route.running
    await api(env, 'PUT', '/api/servers/settings', {'checks': {'route': True}})
    assert (await api(env, 'GET', f'/api/servers/{sid}'))['item']['node']['route']['results']['gd-cm']['line'] == 'CMI'
    await api(env, 'PUT', '/api/servers/settings', {'checks': {'unlock': False}})
    assert (await api(env, 'GET', '/api/servers/settings'))['settings']['checks'] == {'unlock': False, 'route': True, 'ipcheck': True}
    assert (await api(env, 'POST', f'/api/servers/{sid}/unlock', status=409))['error'] == 'unlock_check_disabled'
    for bad in ({'unlock': 'yes'}, {'other': True}, ['route']):
        await api(env, 'PUT', '/api/servers/settings', {'checks': bad}, status=400)


async def test_a_node_without_ipv4_or_offline(env):  # noqa: F811
    v6 = await server(env, 'V6', features=('mesh', 'net'))
    assert (await api(env, 'POST', f'/api/servers/{v6}/route', status=409))['error'] == 'route_no_ipv4'
    away = await server(env, 'Away', online=False)
    assert (await api(env, 'POST', f'/api/servers/{away}/route', status=409))['error'] == 'node_offline'


def _fast(sleep):
    """The busy retry waits 5 s; the test does not."""
    async def fast(seconds, *args):
        return await sleep(min(seconds, 0.01), *args)
    return fast


async def test_neither_check_is_for_a_server_in_mainland_china(env, monkeypatch):  # noqa: F811
    asked = []

    async def call(server_id, method, params, timeout):
        asked.append((server_id, method))
        return {'ok': True, 'result': {'id': 'p', 'state': 'done', 'hops': [], 'results': []}}
    env.servers.hub.call = call
    set_cn = await server(env, 'Kunming', ipv4='198.51.100.244', features=('mesh', 'net', 'net.http'))
    seen_cn = await server(env, 'Chengdu', ipv4='198.51.100.30', features=('mesh', 'net', 'net.http'))
    moved_de = await server(env, 'Berlin', ipv4='203.0.113.203', features=('mesh', 'net', 'net.http'))
    await api(env, 'PATCH', f'/api/servers/{set_cn}', {'profile': {'region': 'CN'}})
    await api(env, 'PATCH', f'/api/servers/{moved_de}', {'profile': {'region': 'DE'}})
    for sid, country in ((seen_cn, 'CN'), (moved_de, 'CN'), (set_cn, 'SG')):
        await env.servers._merge_node_info(sid, {'geo': {'ip': 'x', 'country': country},
                                                'unlock': {'v': 1, 'at': 1, 'ips': ['', ''], 'v4': {}}, 'route': {'v': 1, 'at': 1, 'ip': '', 'results': {}}})
    checks = (env.servers.unlock, env.servers.route)
    # What the user set wins over GeoIP, both ways.
    assert [await c.domestic(s) for c in checks for s in (set_cn, seen_cn, moved_de)] == [True, True, False] * 2
    items = {i['id']: i for i in (await api(env, 'GET', '/api/servers'))['items']}
    assert (items[set_cn]['region'], items[seen_cn]['region'], items[moved_de]['region']) == ('CN', 'CN', 'DE')
    for sid in (set_cn, seen_cn):
        assert items[sid]['node']['unlock'] is None and items[sid]['node']['route'] is None, 'kept, not shown'
        for path in ('unlock', 'route'):
            assert (await api(env, 'POST', f'/api/servers/{sid}/{path}', status=409))['error'] == 'check_not_for_china'
    assert items[moved_de]['node']['unlock'] is not None and items[moved_de]['node']['route'] is not None
    # Nothing runs on its own either.
    for c in checks:
        c.automatic = True
        c.soon(set_cn)
        c.soon(seen_cn)
    await asyncio.sleep(0.05)
    assert not [a for a in asked if a[0] in (set_cn, seen_cn)]


async def test_a_trace_the_node_could_not_run_keeps_its_reason(env, monkeypatch):  # noqa: F811
    """The node ends a trace whose target did not resolve, or that could not open ICMP, as state error: that
    target's result says why rather than an unknown line with no error."""
    monkeypatch.setattr(route, 'POLL_S', 0.01)

    async def call(server_id, method, params, timeout):
        if method == 'net.probe.start':
            return {'ok': True, 'result': {'id': params['target']}}
        target = params['id']
        if target.startswith('bj-'):
            return {'ok': True, 'result': {'state': 'error', 'error': f'dns_failed: lookup {target}: no such host', 'hops': []}}
        return {'ok': True, 'result': {'state': 'error', 'error': 'icmp_unavailable: operation not permitted', 'hops': []}}
    env.servers.hub.call = call
    sid = await server(env, 'NoDNS', ipv4='45.32.100.1', features=('mesh', 'net'))
    results = (await api(env, 'POST', f'/api/servers/{sid}/route'))['route']['results']
    assert results['bj-ct'] == {'line': '', 'tier': '', 'rttMs': None, 'reached': False, 'hops': [],
                                'error': 'dns_failed: lookup bj-ct-v4.ip.zstaticcdn.com: no such host'}
    assert {r['error'] for k, r in results.items() if not k.startswith('bj-')} == {'icmp_unavailable: operation not permitted'}


async def test_checking_again_while_a_check_runs_joins_it(env, monkeypatch):  # noqa: F811
    """重新检测 on two pages, beside the automatic check, of one server: one set of traces on the node and one
    result for all of them; another server's check is its own."""
    monkeypatch.setattr(route, 'POLL_S', 0.01)
    started, release = [], asyncio.Event()

    async def call(server_id, method, params, timeout):
        if method == 'net.probe.start':
            started.append((server_id, params['target']))
            return {'ok': True, 'result': {'id': params['target']}}
        await release.wait()
        return {'ok': True, 'result': {'state': 'done', 'hops': []}}
    env.servers.hub.call = call
    sid = await server(env, 'Twice', ipv4='45.32.100.1', features=('mesh', 'net'))
    other = await server(env, 'Other', ipv4='45.32.100.2', features=('mesh', 'net'))
    checks = env.servers.route
    checks.automatic = True
    checks.soon(sid)
    pages = [asyncio.create_task(api(env, 'POST', f'/api/servers/{s}/route')) for s in (sid, sid, other)]
    for _ in range(100):
        if len(started) >= 2 * route.TRACES_AT_ONCE:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert sorted(s for s, _ in started) == sorted([other] * route.TRACES_AT_ONCE + [sid] * route.TRACES_AT_ONCE), 'one check of each server under way'
    release.set()
    first, second, third = [r['route'] for r in await asyncio.gather(*pages)]
    assert first == second and first['ip'] == '45.32.100.1' and third['ip'] == '45.32.100.2'
    assert len(started) == 2 * len(route.TARGETS)
    for _ in range(100):
        if not checks.running and not checks.inflight:
            break
        await asyncio.sleep(0.01)
    assert not checks.running and not checks.inflight
    # Once it has ended, the next 重新检测 is a check of its own.
    await api(env, 'POST', f'/api/servers/{sid}/route')
    assert len(started) == 3 * len(route.TARGETS)
