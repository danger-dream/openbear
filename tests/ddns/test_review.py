from __future__ import annotations

import asyncio
import json

import pytest

from app.ddns import model
from app.ddns.providers.base import ProviderError
from app.webhooks.repository import many, one
from tests.ddns import fakeprovider
from tests.ddns.harness import OWNER, add_account, add_node, base_env, env, probe_result, url_record  # noqa: F401
from tests.ddns.test_import import cloudflare  # noqa: F401
from tests.ddns.test_provider_route53 import BASE, XMLNS, fake, provider, rrset, xml


@pytest.mark.parametrize('operation,kind', [('pause', 'fake'), ('delete', 'fake'), ('pause', 'fakepush'), ('forget', 'fakepush')])
async def test_stop_or_delete_waits_for_sync_and_prevents_late_writes(env, monkeypatch, operation, kind):
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e, kind=kind)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    await e.ddns._set(rid, desired='198.51.100.8', changed_ms=e.clock[0] + 1)
    entered, release, asked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = e.ddns._call
    blocked = False

    async def call(aid, fn, *args):
        nonlocal blocked
        if not blocked and fn.__name__ == ('records' if kind == 'fake' else 'update'):
            blocked = True
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
        return await original(aid, fn, *args)

    monkeypatch.setattr(e.ddns, '_call', call)
    sync = e.ddns.kick(rid, 'manual')
    await asyncio.wait_for(entered.wait(), 5)

    async def stop():
        asked.set()
        if operation == 'pause':
            return await e.ddns.update_record(OWNER, rid, {'enabled': False})
        return await e.ddns.delete_record(OWNER, rid, delete_remote=operation == 'delete')

    task = asyncio.create_task(stop())
    await asked.wait()
    try:
        assert not task.done()
        e.ddns.kick(rid, 'config')
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(sync, task), 5)
    api = fakeprovider.store('tok')
    writes = list(api.writes())
    await e.ddns.kick(rid, 'manual')
    await e.ddns.idle()
    assert api.writes() == writes
    row = await one(e.db.conn, 'SELECT * FROM ddns_records WHERE id=?', (rid,))
    if operation == 'pause':
        assert row['enabled'] == 0 and row['state'] == 'paused'
        assert await e.ddns._set(rid, _enabled_only=True, state='pending') is False
    else:
        assert row is None
    if operation == 'delete':
        assert api.values('home.example.com') == []


async def test_simultaneous_create_rechecks_duplicate_inside_write_transaction(env, monkeypatch):
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    account = await add_account(e)
    ready, count = asyncio.Event(), 0
    original = e.ddns._remote

    async def remote(*args, **kwargs):
        nonlocal count
        result = await original(*args, **kwargs)
        count += 1
        if count == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        return result

    monkeypatch.setattr(e.ddns, '_remote', remote)
    body = {'serverId': sid, 'source': {'method': 'url'}, 'rows': [{'name': 'home.example.com', 'type': 'A', 'accountId': account['id']}]}
    results = await asyncio.wait_for(asyncio.gather(*(e.ddns.create_records(OWNER, body) for _ in range(2))), 5)
    await e.ddns.idle()
    assert sum(len(r['records']) for r in results) == 1
    assert [r['reason'] for result in results for r in result['skipped']] == ['exists']
    assert len(await many(e.db.conn, 'SELECT * FROM ddns_records')) == 1


@pytest.mark.parametrize('existing_ttl', [None, 900])
async def test_import_preserves_group_ttl_without_pinning_default(env, cloudflare, existing_ttl):
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    if existing_ttl:
        await e.ddns.create_account(OWNER, {'type': 'cloudflare', 'credentials': {'token': 'same-token'}, 'settings': {'ttl': existing_ttl}})
    config = {'dnsconf': [{'dns': {'name': 'cloudflare', 'secret': 'same-token'}, 'ttl': str(ttl),
                          'ipv4': {'enable': True, 'gettype': 'url', 'url': 'https://api4.ipify.org', 'domains': [f'{name}.example.com']}}
                         for name, ttl in [('first', 300), ('second', 600), ('third', existing_ttl or 300)]]}
    result = await e.ddns.import_apply(OWNER, {'yaml': json.dumps(config), 'serverId': sid})
    await e.ddns.idle()
    assert result['skipped'] == []
    expected = {'first.example.com': 300 if existing_ttl else None, 'second.example.com': 600, 'third.example.com': None}
    assert {r['name']: r['ttl'] for r in result['records']} == expected


@pytest.mark.parametrize('extra', ['<SetIdentifier>east</SetIdentifier><Weight>50</Weight>', '<AliasTarget><DNSName>target.example.com.</DNSName></AliasTarget>'])
async def test_route53_rejects_complex_or_alias_records_without_writing(extra):
    api = fake()
    record = rrset('home.example.com.', 'A', ['198.51.100.7']).replace('</ResourceRecordSet>', extra + '</ResourceRecordSet>')
    api.on('GET', BASE + '/hostedzone/Z1/rrset', lambda r, m: xml(200, f'<ListResourceRecordSetsResponse xmlns="{XMLNS}">{record}</ListResourceRecordSetsResponse>'))
    with pytest.raises(ProviderError) as error:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert error.value.code == 'unsupported' and error.value.permanent
    assert not [request for request in api.calls() if request.method != 'GET']


@pytest.mark.parametrize('path,payload', [('/api/ddns/accounts/test', {'type': 'fake', 'credentials': {'token': 'fragmented'}}), ('/api/servers', {'name': 'fragmented'})])
async def test_json_body_reads_all_chunks_and_limits_total(env, path, payload):
    async def fragments(raw):
        yield raw[:2]
        await asyncio.sleep(0.01)
        yield raw[2:]

    headers = {**env.origin, 'Content-Type': 'application/json'}
    response = await env.app.post(path, data=fragments(json.dumps(payload).encode()), headers=headers, cookies=env.cookies)
    assert response.status == 200, await response.text()
    response = await env.app.post(path, data=fragments(b'{' + b' ' * (256 * 1024)), headers=headers, cookies=env.cookies)
    assert response.status == 413, await response.text()
    assert (await response.json())['error'] == 'body_too_large'


def test_recommend_uses_actual_default_interface_result():
    interfaces = [{'name': 'eth0', 'default': True, 'v4': ['10.0.0.1']}, {'name': 'eth1', 'default': False, 'v4': ['198.51.100.2']}]
    results = [{'method': 'nic', 'value': '10.0.0.1'}, {'method': 'url', 'value': '198.51.100.2'}]
    assert model.recommend('v4', results, interfaces)[0] == 'url'
    results[0]['value'] = '198.51.100.2'
    assert model.recommend('v4', results, interfaces)[0] == 'nic'


@pytest.mark.parametrize('address,family', [('224.0.0.1', 'v4'), ('239.1.2.3', 'v4'), ('ff02::1', 'v6')])
def test_multicast_is_never_a_host_address(address, family):
    assert model.check_address(address, family, True) == ('', 'reserved')


@pytest.mark.parametrize('address', ['10.0.0.1', '198.18.0.1', '240.0.0.1'])
def test_private_and_reserved_uses_remain_available(address):
    assert model.check_address(address, 'v4', True) == (address, '')
