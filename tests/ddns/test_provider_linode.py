import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.linode\.com/v4'
RECORDS = BASE + '/domains/77/records'


def provider(api):
    return get('linode')({'token': 'tok'}, client=api.client())


def page(data, number=1, pages=1):
    return {'data': data, 'page': number, 'pages': pages, 'results': len(data)}


def rec(rid, name, target, rtype='A', ttl=0):
    return {'id': rid, 'type': rtype, 'name': name, 'target': target, 'ttl_sec': ttl, 'priority': 0, 'weight': 0, 'port': 0, 'tag': None}


def with_domain(api):
    return api.on('GET', BASE + '/domains', json=page([{'id': 77, 'domain': 'example.com', 'type': 'master'}]))


def test_metadata():
    cls = get('linode')
    assert cls.tier == 'A' and (cls.caps.ttl_min, cls.caps.ttl_max) == (300, 2419200)


async def test_zones_page_and_skip_slaves():
    def handler(req, m):
        number = int(req.url.params['page'])
        if number == 1:
            return page([{'id': 1, 'domain': 'b.com', 'type': 'master'}, {'id': 2, 'domain': 'c.com', 'type': 'slave'}], 1, 2)
        return page([{'id': 3, 'domain': 'a.com', 'type': 'master'}], 2, 2)
    api = FakeAPI().on('GET', BASE + '/domains', handler)
    assert (await provider(api).check())['zones'] == ['a.com', 'b.com']
    assert [dict(r.url.params) for r in api.calls()] == [{'page': '1', 'page_size': '500'}, {'page': '2', 'page_size': '500'}]
    assert api.calls()[0].headers['Authorization'] == 'Bearer tok'


async def test_records_match_and_miss():
    api = with_domain(FakeAPI()).on('GET', RECORDS, json=page([rec(1, 'home', '198.51.100.1', ttl=3600), rec(2, '', '198.51.100.2'),
                                                               rec(3, 'home', '2001:db8::1', 'AAAA')]))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('1', '198.51.100.1', 3600)]
    assert [(r.id, r.ttl) for r in await provider(api).records('example.com', 'example.com', 'A')] == [('2', None)]
    assert await provider(api).records('example.com', 'none.example.com', 'A') == []


async def test_unknown_zone():
    with pytest.raises(ProviderError) as err:
        await provider(with_domain(FakeAPI())).records('other.com', 'home.other.com', 'A')
    assert err.value.code == 'zone_not_found'


async def test_create():
    api = with_domain(FakeAPI()).on('POST', RECORDS, json=rec(9, 'home', '203.0.113.5'))
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 60, {}) == '9'
    assert FakeAPI.body(api.calls('POST')[0]) == {'type': 'A', 'name': 'home', 'target': '203.0.113.5', 'ttl_sec': 300}


async def test_update_sends_only_target_and_given_ttl():
    api = with_domain(FakeAPI()).on('PUT', RECORDS + '/9', json=rec(9, '', '203.0.113.9'))
    await provider(api).update('example.com', Record('9', 'example.com', 'A', '198.51.100.1', 3600), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls('PUT')[0]) == {'target': '203.0.113.9'}
    await provider(api).update('example.com', Record('9', 'example.com', 'A', '198.51.100.1'), '203.0.113.9', 7200, {})
    assert FakeAPI.body(api.calls('PUT')[1]) == {'target': '203.0.113.9', 'ttl_sec': 7200}


async def test_delete():
    api = with_domain(FakeAPI()).on('DELETE', RECORDS + '/9', json={})
    await provider(api).delete('example.com', Record('9', 'home.example.com', 'A', '198.51.100.1'))
    assert api.calls('DELETE')[0].url.path == '/v4/domains/77/records/9'


@pytest.mark.parametrize('status,expected', [(401, 'auth_failed'), (403, 'forbidden'), (400, 'bad_request'), (429, 'rate_limited')])
async def test_error_mapping(status, expected):
    api = FakeAPI().on('GET', BASE + '/domains', status=status, json={'errors': [{'reason': 'Invalid Token'}]})
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == expected and err.value.message == 'Invalid Token'
