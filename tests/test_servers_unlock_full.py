"""The detail page's full unlock list: the Service Unlock program's report folded into one line per
service, when a node runs it, what it keeps, and a benchmark run's report taking its place."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from app.instances.contracts import InstanceError
from app.servers import unlock_full
from app.servers.unlock_full import fold, trim, unable
from tests.test_mesh_service import OWNER, connect, server
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

# A real report from a server in Spain (Europe's further checks), trimmed of timings.
SPAIN = json.loads((Path(__file__).parent / 'fixtures' / 'service_unlock_es.json').read_text())
LINUX = {'platform': 'linux', 'arch': 'amd64', 'ipv4': '45.32.100.1'}


def changed(report=SPAIN, **rows):
    """report with rows changed: {'s:google_captcha': {'value': 'yes'}, ...}."""
    out = copy.deepcopy(report)
    keys = {'r': 'results', 's': 'services', 'a': 'ai_endpoints', 'x': 'extra_checks'}
    for key, change in rows.items():
        row = next(r for r in out[keys[key[0]]] if r['id'] == key[2:])
        row.update(change)
    return out


def by_id(report):
    return {i['id']: i for i in fold(trim(report))['items']}


def test_the_spanish_report_folded():
    folded = fold(trim(SPAIN))
    assert (folded['region'], folded['country'], folded['family']) == ('eu', 'ES', 4)
    items = {i['id']: i for i in folded['items']}
    # The core's 35 rows make 23 services, Prime Video taking the further check's verdict; Europe adds 35 more.
    assert len(items) == 23 + 35 and 'MediaUnlockTest_PrimeVideo' not in items
    assert [i['id'] for i in folded['items'][:3]] == ['youtube', 'netflix', 'prime']
    assert items['youtube'] == {'id': 'youtube', 'name': 'YouTube', 'group': 'media', 'state': 'ok', 'region': 'ES', 'notes': []}
    assert items['gemini']['region'] == 'ES', 'three letters become two'
    assert items['openai']['region'] == 'ES' and items['tiktok']['region'] == 'ES', 'the region from the service row when the verdict has none'
    assert items['spotify']['state'] == 'ok' and items['spotify']['notes'] == ['不能注册新账号']
    assert items['deepseek']['group'] == 'ai' and items['ookla']['name'] == 'Speedtest'
    assert items['MediaUnlockTest_Dazn']['name'] == 'DAZN' and items['MediaUnlockTest_BBCiPLAYER']['name'] == 'BBC iPlayer'
    assert items['GameTest_MathsSpot']['group'] == items['WebTest_Wikipedia_Editable']['group'] == 'web'
    assert {i['group'] for i in folded['items']} == {'media', 'ai', 'web'}
    assert all(i['state'] in ('ok', 'limited', 'blocked', 'error') for i in folded['items'])


def test_what_narrows_or_turns_a_service_away():
    items = by_id(changed(**{'r:netflix_access': {'status': 'restricted', 'detail': 'originals only; licensed titles are withheld'},
                             's:google_captcha': {'value': 'yes'}, 's:youtube_premium': {'value': 'no'}, 'r:youtube_premium_access': {'status': 'blocked'},
                             'a:anthropic': {'status': 'blocked'}, 'r:chatgpt_web': {'status': 'blocked', 'region': None},
                             'x:MediaUnlockTest_HBOMax': {'status': 'restricted', 'detail': 'Oversea Only'},
                             'x:MediaUnlockTest_ZDF': {'status': 'error', 'region': None, 'detail': 'unexpected HTTP'}}))
    assert (items['netflix']['state'], items['netflix']['notes']) == ('limited', ['仅自制剧'])
    assert (items['google']['state'], items['google']['notes']) == ('ok', ['搜索要过人机验证'])
    assert items['youtube']['notes'] == ['Premium 不可用'], 'said once'
    assert (items['anthropic']['state'], items['anthropic']['notes']) == ('ok', ['API 拒绝该地区'])
    assert (items['openai']['state'], items['openai']['region']) == ('blocked', 'ES')
    assert (items['MediaUnlockTest_HBOMax']['state'], items['MediaUnlockTest_HBOMax']['notes']) == ('limited', ['仅海外版'])
    assert (items['MediaUnlockTest_ZDF']['state'], items['MediaUnlockTest_ZDF']['region'], items['MediaUnlockTest_ZDF']['notes']) == ('error', '', [])
    # Prime Video's further check could not tell: its region from the core's row.
    prime = by_id(changed(**{'x:MediaUnlockTest_PrimeVideo': {'status': 'error', 'region': None}}))['prime']
    assert (prime['state'], prime['region']) == ('ok', 'ES')
    unseen = by_id(changed(**{'s:twitch': {'status': 'error', 'value': None}}))['twitch']
    assert (unseen['state'], unseen['region']) == ('error', '')


def test_what_is_not_a_report_and_where_it_cannot_run():
    assert trim(None) is None and trim({'schema_version': 2, 'results': []}) is None and trim({'schema_version': 1}) is None
    assert unable({'platform': 'linux', 'arch': 'amd64'}, {'exec': True}) == ''
    assert unable({'platform': 'linux', 'arch': 'arm64'}, {}) == ''
    assert unable({'platform': 'windows', 'arch': 'amd64'}, {'exec': True}) == 'platform'
    assert unable({'platform': 'darwin', 'arch': 'arm64'}, {'exec': True}) == 'platform'
    assert unable({'platform': 'linux', 'arch': 'amd64'}, {'exec': False}) == 'exec'


async def test_a_run_kept_shown_and_replaced(env, monkeypatch):  # noqa: F811
    pushed, ran = [], []

    async def push(row, platform):
        pushed.append((row['server_id'], row['test'], platform))
    reply = {'exitCode': 1, 'stdout': json.dumps(SPAIN)}

    async def run(owner, server_id, command, **kw):
        ran.append((command, kw['source'], kw['log']))
        return {'ok': reply['exitCode'] == 0, 'timedOut': False, **reply}
    monkeypatch.setattr(env.servers.bench, '_push', push)
    monkeypatch.setattr(env.servers, 'exec', run)
    sid = await server(env, 'Mad', ipv4='45.32.100.1', online=False)
    await connect(env, sid, LINUX)
    path = f'/api/servers/{sid}/unlock-full'
    assert (await api(env, 'GET', path)) == {'ok': True, 'off': False, 'running': False, 'deferred': False, 'unable': '', 'full': None, 'error': None,
                                             'failedAt': None}
    full = unlock_full.FullUnlock
    stored = lambda: env.servers.unlock_full._stored(sid)  # noqa: E731
    assert env.servers.unlock_full.capable(sid) and env.servers.unlock_full.due(sid, await stored(), LINUX)

    # Exit 1 is a whole report with a service it could not tell.
    await env.servers.unlock_full.check(sid)
    assert pushed == [(sid, 'unlock', 'linux/amd64')]
    assert ran[0][0].endswith('exec ./bin/service-unlock --json') and ran[0][1:] == ('check', '解锁检测：运行 Service Unlock')
    shown = await api(env, 'GET', path)
    assert shown['full']['source'] == 'auto' and shown['full']['country'] == 'ES' and len(shown['full']['items']) == 58
    marker = (await stored())[full.KEY]
    assert marker['ips'] == ['45.32.100.1', ''] and marker['at'] == shown['full']['at'] and marker['v'] == unlock_full.VERSION
    assert not env.servers.unlock_full.due(sid, await stored(), LINUX)
    assert env.servers.unlock_full.due(sid, await stored(), {**LINUX, 'ipv4': '45.32.100.9'}), 'a new address'
    listed = (await api(env, 'GET', f'/api/servers/{sid}'))['item']['node']
    assert full.KEY not in listed, 'the list does not carry it'

    # A failed run keeps the last list and says why.
    reply.update(exitCode=2, stdout='')
    with pytest.raises(InstanceError, match='unlock_full_failed'):
        await env.servers.unlock_full.check(sid)
    failed = await api(env, 'GET', path)
    assert failed['full']['at'] == shown['full']['at'] and failed['error'] == 'unlock_full_failed' and failed['failedAt']
    assert not env.servers.unlock_full.due(sid, await stored(), LINUX), 'tried again with the cards’ next check'

    # While a benchmark runs there, it waits: nothing runs and it stays as it was.
    async with env.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO server_bench_runs(id,owner,server_id,test,state,created_ms) VALUES('b1',1,?,'netcheck','running',1)", (sid,))
    count = len(ran)
    assert await env.servers.unlock_full.check(sid) is None and len(ran) == count
    # The page is told it waits, so it keeps following it rather than showing the old list as current.
    assert (await api(env, 'GET', path))['deferred'] is True
    async with env.db.write_transaction(label='test') as conn:
        await conn.execute("DELETE FROM server_bench_runs WHERE id='b1'")

    # A Service Unlock benchmark's report takes its place when newer, and only then.
    hk = changed(**{'s:google': {'value': 'HK'}})
    await env.servers.unlock_full.adopt(OWNER, sid, hk, shown['full']['at'] - 1)
    assert by_id(SPAIN)['google']['region'] == 'ES' and (await api(env, 'GET', path))['full']['source'] == 'auto'
    await env.servers.unlock_full.adopt(OWNER, sid, hk, shown['full']['at'] + 1)
    after = await api(env, 'GET', path)
    assert after['full']['source'] == 'bench' and next(i for i in after['full']['items'] if i['id'] == 'google')['region'] == 'HK'
    assert after['error'] is None, 'a newer list clears the failure'

    # Switched off with the cards' unlock checks: nothing to show; deleted with its server.
    await api(env, 'PUT', '/api/servers/settings', {'checks': {'unlock': False}})
    assert (await api(env, 'GET', path))['off'] is True
    await api(env, 'DELETE', f'/api/servers/{sid}')
    assert not await env.servers.unlock_full._stored(sid)


async def test_nodes_it_cannot_run_on(env):  # noqa: F811
    win = await server(env, 'Win', platform='windows', ipv4='45.32.100.3')
    closed = await server(env, 'Closed', ipv4='45.32.100.4', exec_=False, online=False)
    await connect(env, closed, {**LINUX, 'ipv4': '45.32.100.4'}, exec_=False)
    assert (await api(env, 'GET', f'/api/servers/{win}/unlock-full'))['unable'] == 'platform'
    assert (await api(env, 'GET', f'/api/servers/{closed}/unlock-full'))['unable'] == 'exec'
    assert not env.servers.unlock_full.capable(win) and not env.servers.unlock_full.capable(closed)
    with pytest.raises(InstanceError, match='bench_platform_unsupported'):
        await env.servers.unlock_full.check(win)


async def test_it_follows_the_cards_checks(env, monkeypatch):  # noqa: F811
    from tests.test_servers_unlock import served
    checked = []

    async def call(server_id, method, params, timeout):
        return {'ok': True, 'result': {'family': params['family'], 'results': served('ES')}}

    async def check(server_id):
        checked.append(server_id)
    env.servers.hub.call = call
    monkeypatch.setattr(env.servers.unlock_full, 'check', check)
    sid = await server(env, 'Mad', ipv4='45.32.100.1', online=False)
    await connect(env, sid, LINUX, features=('mesh', 'net.http'))
    env.servers.unlock_full.automatic = True
    await api(env, 'POST', f'/api/servers/{sid}/unlock')
    for _ in range(100):
        if checked and not env.servers.unlock_full.running:
            break
        await asyncio.sleep(0.02)
    assert checked == [sid]
    # Not where it cannot run.
    win = await server(env, 'Win', platform='windows', ipv4='45.32.100.3', features=('mesh', 'net.http'))
    await api(env, 'POST', f'/api/servers/{win}/unlock')
    await asyncio.sleep(0.1)
    assert checked == [sid]


async def test_full_unlock_and_benchmark_share_slot_through_cleanup(env, monkeypatch):
    sid = await server(env, 'router', online=False)
    await connect(env, sid, {**LINUX, 'openwrt': True})
    full, bench = env.servers.unlock_full, env.servers.bench
    running, finish_run = asyncio.Event(), asyncio.Event()
    cleaning, finish_clean = asyncio.Event(), asyncio.Event()
    bench_entered, finish_bench = asyncio.Event(), asyncio.Event()
    events = []

    async def program(*args):
        events.append('full-run')
        running.set()
        await finish_run.wait()
        return {'exitCode': 0, 'stdout': json.dumps(SPAIN)}

    async def clean(*args, **kwargs):
        events.append(kwargs['source'] + '-clean')
        if kwargs['source'] == 'check':
            cleaning.set()
            await finish_clean.wait()
        return {'ok': True}

    async def advance(row):
        events.append('bench-run')
        bench_entered.set()
        await finish_bench.wait()
        await bench._clean(row, package=True)
        await bench._set(row['id'], state='done')
    monkeypatch.setattr(full, '_program', program)
    monkeypatch.setattr(env.servers, 'exec', clean)
    # The launch (which pushes the package) cannot begin before full unlock's cleanup.
    monkeypatch.setattr(bench, '_launch', advance)
    task = asyncio.create_task(full.check(sid))
    try:
        await asyncio.wait_for(running.wait(), 2)
        created = await bench.create(OWNER, {'serverId': sid, 'test': 'unlock'})
        worker = bench.workers[sid]
        await asyncio.sleep(0.02)
        assert not bench_entered.is_set()
        finish_run.set()
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not bench_entered.is_set()
        finish_clean.set()
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(bench_entered.wait(), 2)
        assert events == ['full-run', 'check-clean', 'bench-run']
        assert await full.check(sid) is None
        assert full.due(sid, await full._stored(sid), LINUX), 'a deferred refresh must remain due even with an existing result'
        finish_bench.set()
        await asyncio.wait_for(worker, 2)
        assert events[-1] == 'bench-clean'
        assert (await bench._get(created['runs'][0]['id']))['state'] == 'done'
    finally:
        finish_run.set()
        finish_clean.set()
        finish_bench.set()
        await asyncio.gather(task, return_exceptions=True)


def test_full_unlock_trims_untrusted_fields_and_collections():
    report = {'schema_version': 1, 'results': [{'id': 'netflix_access', 'status': [], 'region': {}, 'detail': {}}],
              'services': 42, 'ai_endpoints': {}, 'extra_checks': [{'id': 'x', 'name': 'n' * 1000, 'status': True}],
              'selection': {'region': [], 'detected_country': {}, 'extra_family': True}}
    kept = trim(report)
    assert kept['results'][0] == {'id': 'netflix_access', 'status': None, 'region': None, 'detail': None}
    assert kept['services'] == kept['ai'] == []
    assert kept['extra'][0]['status'] is None
    assert kept['selection'] == {'region': None, 'detected_country': None, 'extra_family': None}
    assert fold(kept)['items'][0]['state'] == 'error'


async def test_an_openwrt_router_keeps_no_package(env, monkeypatch):  # noqa: F811
    ran = []

    async def push(row, platform):
        pass

    async def run(owner, server_id, command, **kw):
        ran.append((command, kw['log']))
        # The program goes past a transparent proxy; the clean-up needs not.
        assert kw.get('bypass', False) is command.endswith('service-unlock --json')
        return {'ok': True, 'timedOut': False, 'exitCode': 0, 'stdout': json.dumps(SPAIN) if 'service-unlock' in command else ''}
    monkeypatch.setattr(env.servers.bench, '_push', push)
    monkeypatch.setattr(env.servers, 'exec', run)
    sid = await server(env, 'Kwrt', ipv4='45.32.100.5', online=False)
    await connect(env, sid, {**LINUX, 'ipv4': '45.32.100.5', 'openwrt': True})
    await env.servers.unlock_full.check(sid)
    # /var/tmp is a RAM disk on OpenWrt: the package is removed once it ran, failed or not.
    assert ran[-1] == ("rm -rf /var/tmp/openbear-bench/service-unlock-v0.1.0", '解锁检测：清理 Service Unlock')
    monkeypatch.setattr(env.servers, 'exec', lambda *a, **kw: run(*a, **kw) if 'rm -rf' in a[2] else asyncio.sleep(0, result={'exitCode': 2, 'stdout': ''}))
    with pytest.raises(InstanceError, match='unlock_full_failed'):
        await env.servers.unlock_full.check(sid)
    assert ran[-1][1] == '解锁检测：清理 Service Unlock' and len(ran) == 3
