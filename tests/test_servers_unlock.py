"""The service checks behind the cards' unlock icons: verdicts from net.http answers, and when the
controller asks a node for them."""
from __future__ import annotations

import asyncio

import pytest

from app.servers import unlock
from tests.test_mesh_service import connect, server
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


def answer(rid, status=200, url=None, size=1000, error='', find=None, has=(), json=None):
    """One net.http result for request rid, by the names the check gives its patterns."""
    finds, texts, paths = unlock._NAMES[rid]
    request = next(r for r in unlock.REQUESTS if r['id'] == rid)
    return {'id': rid, 'status': status, 'url': url or request['url'], 'size': size, 'error': error,
            'find': [(find or {}).get(n, '') for n in finds], 'contains': [n in has for n in texts],
            'json': [(json or {}).get(n, '') for n in paths]}


def served(country='SG', **changes):
    """Every service serving country, then changes: {request id: answer kwargs or None to leave it out}."""
    base = {
        'google': {'find': {'locale': country}},
        'youtube': {'find': {'region': country}, 'has': ('offer',)},
        'openai': {}, 'openai.trace': {'find': {'loc': country}},
        'anthropic': {}, 'anthropic.trace': {'find': {'loc': country}}, 'anthropic.api': {'status': 401, 'json': {'error': 'authentication_error'}},
        'gemini': {'find': {'region': {'SG': 'SGP', 'US': 'USA', 'CN': 'CHN'}.get(country, country)}},
        'netflix': {'url': f'https://www.netflix.com/{country.lower()}/title/70143836'},
        'netflix.original': {'url': f'https://www.netflix.com/{country.lower()}/title/80197526'},
        'spotify': {'find': {'region': country}}, 'reddit': {}, 'reddit.region': {'find': {'region': country.lower()}},
        'tiktok': {}, 'tiktok.region': {'json': {'region': country}},
    }
    base.update(changes)
    return [answer(rid, **kw) for rid, kw in base.items() if kw is not None]


def verdicts(results):
    return {k: (v['state'], v['region'], v['note']) for k, v in unlock.judge(results).items()}


def test_every_service_served():
    assert verdicts(served('SG')) == {s: ('ok', 'SG', '') for s in unlock.SERVICES}
    assert list(unlock.judge([])) == list(unlock.SERVICES)
    assert all(v[0] == 'error' for v in verdicts([]).values()), 'no answers prove nothing'
    timeouts = served(**{rid: {'status': 0, 'error': 'timeout', 'size': 0} for rid in ('openai', 'tiktok')})
    assert verdicts(timeouts)['openai'] == ('error', '', 'timeout') and verdicts(timeouts)['tiktok'] == ('error', '', 'timeout')


def test_sent_to_china_and_refusals():
    # 送中: YouTube sends the visitor to google.cn; Google's country is then China whatever its homepage says.
    cn = verdicts(served('SG', youtube={'url': 'https://www.google.cn/', 'size': 300}))
    assert cn['google'] == ('ok', 'CN', '') and cn['youtube'] == ('blocked', 'CN', '')
    v = verdicts(served('HK', youtube={'find': {'region': 'HK'}, 'has': ('refused',)}, openai={'has': ('refused',)},
                        gemini={'find': {'region': 'CHN'}}, spotify={'find': {'region': 'CN'}}, tiktok={'has': ('refused',)}))
    assert v['youtube'] == ('blocked', 'HK', '') and v['openai'] == ('blocked', 'HK', '') and v['gemini'] == ('blocked', 'CN', '')
    assert v['spotify'] == ('blocked', 'CN', '') and v['tiktok'] == ('blocked', 'HK', '')
    assert verdicts(served('US'))['gemini'] == ('ok', 'US', ''), 'three letters become two'
    assert verdicts(served(gemini={'find': {}}))['gemini'] == ('error', '', 'unclear')
    assert verdicts(served(youtube={'find': {'region': 'SG'}}))['youtube'] == ('error', '', 'unclear'), 'neither offer nor refusal'


def test_claude_behind_cloudflare_falls_back_to_the_api():
    challenged = {'status': 403, 'has': ('challenge',)}
    assert verdicts(served(anthropic=challenged))['anthropic'] == ('ok', 'SG', 'api')
    assert verdicts(served(anthropic=challenged, **{'anthropic.api': {'status': 403, 'json': {'error': 'forbidden'}, 'has': ('refused',)}}))['anthropic'] \
        == ('blocked', 'SG', 'api')
    assert verdicts(served(anthropic=challenged, **{'anthropic.api': {'status': 0, 'error': 'timeout', 'size': 0}}))['anthropic'] == ('error', '', 'challenge')
    assert verdicts(served(anthropic={'has': ('refused',)}))['anthropic'] == ('blocked', 'SG', '')
    assert verdicts(served(anthropic={'url': 'https://claude.ai/app-unavailable-in-region'}))['anthropic'] == ('blocked', 'SG', '')
    # Refused without the region page: not put down to the country.
    assert verdicts(served(anthropic={'status': 403}))['anthropic'] == ('error', '', 'http')


def test_netflix_catalogue_and_reddit_datacenters():
    assert verdicts(served(netflix={'url': 'https://www.netflix.com/jp-en/title/70143836'}))['netflix'] == ('ok', 'JP', '')
    originals = served(netflix={'status': 404}, **{'netflix.original': {'url': 'https://www.netflix.com/title/80197526'}})
    assert verdicts(originals)['netflix'] == ('limited', 'US', 'originals'), 'a bare /title/ path is the US catalogue'
    assert verdicts(served(netflix={'status': 404}, **{'netflix.original': {'status': 404}}))['netflix'] == ('blocked', '', '')
    assert verdicts(served(netflix={'url': 'https://example.com/title/70143836'}))['netflix'] == ('error', '', 'unclear')
    assert verdicts(served(netflix={'status': 0, 'error': 'connect', 'size': 0}))['netflix'] == ('error', '', 'failed')
    blocked = served(reddit={'status': 403, 'has': ('blocked',)}, **{'reddit.region': {'status': 403}})
    assert verdicts(blocked)['reddit'] == ('blocked', '', 'datacenter')
    assert verdicts(served(reddit={'status': 403, 'has': ('challenge',)}))['reddit'] == ('error', '', 'challenge')


async def test_checks_run_per_family_on_hello_and_when_asked(env, monkeypatch):  # noqa: F811
    asked = []

    async def call(server_id, method, params, timeout):
        assert method == 'net.http' and params['requests'] == unlock.REQUESTS
        asked.append((server_id, params['family']))
        return {'ok': True, 'result': {'family': params['family'], 'results': served('DE' if params['family'] == 4 else 'NL')}}
    env.servers.hub.call = call
    env.servers.unlock.automatic = True
    sid = await server(env, 'Fra', ipv4='45.32.100.1', features=('mesh', 'net.http'))

    async def shown():
        return (await api(env, 'GET', f'/api/servers/{sid}'))['item']['node']['unlock']

    async def settle():
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not env.servers.unlock.running:
                return
        raise AssertionError('checks did not settle')
    await settle()
    first = await shown()
    assert asked == [(sid, 4)] and first['ips'] == ['45.32.100.1', ''] and 'v6' not in first
    assert first['v4']['gemini'] == {'state': 'ok', 'region': 'DE', 'note': ''}

    # A hello with nothing new does not check again; another address, or one more family, does.
    await connect(env, sid, {'platform': 'linux', 'ipv4': '45.32.100.1'}, features=('mesh', 'net.http'))
    await settle()
    assert len(asked) == 1
    await connect(env, sid, {'platform': 'linux', 'ipv4': '45.32.100.1', 'ipv6': '2a0e:6901::1'}, features=('mesh', 'net.http'))
    await settle()
    assert sorted(asked[1:]) == [(sid, 4), (sid, 6)]
    both = await shown()
    assert both['v6']['netflix']['region'] == 'NL' and both['ips'] == ['45.32.100.1', '2a0e:6901::1'] and both['at'] >= first['at']

    # Six hours on, the periodic look finds it due.
    stored = {'unlock': {**both, 'v': unlock.VERSION, 'at': both['at'] - unlock.EVERY_S * 1000}}
    assert env.servers.unlock.due(sid, stored, {'ipv4': '45.32.100.1', 'ipv6': '2a0e:6901::1'})
    assert not env.servers.unlock.due(sid, {'unlock': {**both, 'v': unlock.VERSION}}, {'ipv4': '45.32.100.1', 'ipv6': '2a0e:6901::1'})

    # 重新检测 from the detail page.
    again = await api(env, 'POST', f'/api/servers/{sid}/unlock')
    assert again['unlock']['v4']['google']['region'] == 'DE' and 'v' not in again['unlock'] and len(asked) == 5

    # An older node has no net.http: nothing is asked, and asking says it needs an update.
    old = await server(env, 'Old', ipv4='45.32.100.2', features=('mesh',))
    await settle()
    assert all(s != old for s, _ in asked)
    assert (await api(env, 'POST', f'/api/servers/{old}/unlock', status=409))['error'] == 'node_update_required'
    offline = await server(env, 'Away', online=False)
    assert (await api(env, 'POST', f'/api/servers/{offline}/unlock', status=409))['error'] == 'node_offline'


@pytest.mark.parametrize('value, code', [('usa', 'US'), ('GBR', 'GB'), ('de', 'DE'), ('XX', ''), ('T1', ''), ('ABCD', ''), (None, '')])
def test_country_codes(value, code):
    assert unlock.region(value) == code
