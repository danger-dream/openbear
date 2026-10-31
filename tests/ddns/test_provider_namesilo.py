import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://www\.namesilo\.com/api'


def provider(api):
    return get('namesilo')({'key': 'secret-key'}, client=api.client())


def reply(op, **body):
    return {'request': {'operation': op, 'ip': '55.555.55.55'}, 'reply': {'code': 300, 'detail': 'success', **body}}


def params(request):
    return dict(request.url.params)


def test_metadata():
    cls = get('namesilo')
    assert cls.tier == 'A' and not cls.caps.by_id and cls.caps.ttl_min == 3600


async def test_zones_follow_pager():
    def handler(req, m):
        page = int(req.url.params['page'])
        names = [f'd{i}.com' for i in range(100)] if page == 1 else ['zz.com']
        return reply('listDomains', domains=[{'domain': n} for n in names], pager={'page': page, 'pageSize': '100', 'total': '101'})
    api = FakeAPI().on('GET', BASE + '/listDomains', handler)
    zones = (await provider(api).check())['zones']
    assert len(zones) == 101
    assert params(api.calls()[0]) == {'version': '1', 'type': 'json', 'key': 'secret-key', 'page': '1', 'pageSize': '100'}
    assert params(api.calls()[1])['page'] == '2'


async def test_zones_without_pager():
    api = FakeAPI().on('GET', BASE + '/listDomains', json=reply('listDomains', domains={'domain': 'only.com'}))
    assert await provider(api).zones() == ['only.com']


async def test_records_match_and_miss():
    items = [{'record_id': 'a1', 'type': 'A', 'host': 'home.example.com', 'value': '198.51.100.1', 'ttl': '7207', 'distance': '0'},
             {'record_id': 'a2', 'type': 'AAAA', 'host': 'home.example.com', 'value': '2001:db8::1', 'ttl': '3600', 'distance': '0'}]
    api = FakeAPI().on('GET', BASE + '/dnsListRecords', json=reply('dnsListRecords', resource_record=items))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('a1', '198.51.100.1', 7207)]
    assert params(api.calls()[0])['domain'] == 'example.com'
    assert await provider(api).records('example.com', 'other.example.com', 'A') == []


async def test_create():
    api = FakeAPI().on('GET', BASE + '/dnsAddRecord', json=reply('dnsAddRecord', record_id='new1'))
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 600, {}) == 'new1'
    assert params(api.calls()[0]) == {'version': '1', 'type': 'json', 'key': 'secret-key', 'domain': 'example.com', 'rrtype': 'A',
                                      'rrhost': 'home', 'rrvalue': '203.0.113.5', 'rrttl': '3600'}


async def test_update_keeps_ttl():
    api = FakeAPI().on('GET', BASE + '/dnsUpdateRecord', json=reply('dnsUpdateRecord', record_id='new2'))
    assert await provider(api).update('example.com', Record('a1', 'example.com', 'A', '198.51.100.1', 7207), '203.0.113.9', None, {}) == 'new2'
    assert params(api.calls()[0]) == {'version': '1', 'type': 'json', 'key': 'secret-key', 'domain': 'example.com', 'rrid': 'a1',
                                      'rrhost': '', 'rrvalue': '203.0.113.9', 'rrttl': '7207'}
    await provider(api).update('example.com', Record('a1', 'home.example.com', 'A', '198.51.100.1', 7207), '203.0.113.9', 86400, {})
    assert params(api.calls()[1])['rrttl'] == '86400'


async def test_delete():
    api = FakeAPI().on('GET', BASE + '/dnsDeleteRecord', json=reply('dnsDeleteRecord'))
    await provider(api).delete('example.com', Record('a1', 'home.example.com', 'A', 'x'))
    assert params(api.calls()[0])['rrid'] == 'a1'


@pytest.mark.parametrize('code,expected', [(110, 'auth_failed'), (113, 'forbidden'), (200, 'zone_not_found'), (280, 'bad_request')])
async def test_reply_codes_in_http_200(code, expected):
    api = FakeAPI().on('GET', BASE + '/dnsListRecords', json={'reply': {'code': code, 'detail': 'x'}})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == expected
