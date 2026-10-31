import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.digitalocean\.com/v2'
RECORDS = BASE + '/domains/example.com/records'


def provider(api):
    return get('digitalocean')({'token': 'tok'}, client=api.client())


def rec(rid, name, data, rtype='A'):
    return {'id': rid, 'type': rtype, 'name': name, 'data': data, 'priority': None, 'port': None, 'ttl': 1800, 'weight': None}


def test_metadata():
    cls = get('digitalocean')
    assert cls.tier == 'A' and cls.caps.ttl_min == 30 and cls.caps.by_id


async def test_zones_follow_next_links():
    def handler(req, m):
        if req.url.params.get('page') == '2':
            return {'domains': [{'name': 'a.com'}], 'links': {}, 'meta': {'total': 2}}
        return {'domains': [{'name': 'b.com'}], 'links': {'pages': {'next': 'https://api.digitalocean.com/v2/domains?page=2&per_page=200'}},
                'meta': {'total': 2}}
    api = FakeAPI().on('GET', BASE + '/domains', handler)
    assert (await provider(api).check())['zones'] == ['a.com', 'b.com']
    first, second = api.calls()
    assert dict(first.url.params) == {'per_page': '200'} and dict(second.url.params) == {'page': '2', 'per_page': '200'}
    assert first.headers['Authorization'] == 'Bearer tok'


async def test_records_match_and_miss():
    api = FakeAPI().on('GET', RECORDS, json={'domain_records': [rec(1, 'home', '198.51.100.1'), rec(2, '@', '198.51.100.2')],
                                             'links': {}, 'meta': {'total': 2}})
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('1', '198.51.100.1', 1800)]
    assert dict(api.calls()[0].url.params) == {'per_page': '200', 'name': 'home.example.com', 'type': 'A'}
    assert [r.id for r in await provider(api).records('example.com', 'example.com', 'A')] == ['2']
    assert await provider(api).records('example.com', 'none.example.com', 'A') == []


async def test_missing_zone():
    api = FakeAPI().on('GET', RECORDS, status=404, json={'id': 'not_found', 'message': 'The resource you were accessing could not be found.'})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == 'zone_not_found'


async def test_create():
    api = FakeAPI().on('POST', RECORDS, status=201, json={'domain_record': rec(9, 'home', '203.0.113.5')})
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 10, {}) == '9'
    assert FakeAPI.body(api.calls()[0]) == {'type': 'A', 'name': 'home', 'data': '203.0.113.5', 'ttl': 30}


async def test_update_patches_only_value():
    api = FakeAPI().on('PATCH', RECORDS + '/9', json={'domain_record': rec(9, '@', '203.0.113.9')})
    await provider(api).update('example.com', Record('9', 'example.com', 'A', '198.51.100.1', 1800), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls()[0]) == {'type': 'A', 'data': '203.0.113.9'}
    await provider(api).update('example.com', Record('9', 'example.com', 'A', '198.51.100.1', 1800), '203.0.113.9', 300, {})
    assert FakeAPI.body(api.calls()[1]) == {'type': 'A', 'data': '203.0.113.9', 'ttl': 300}


async def test_delete():
    api = FakeAPI().on('DELETE', RECORDS + '/9', status=204)
    await provider(api).delete('example.com', Record('9', 'home.example.com', 'A', '198.51.100.1'))
    assert api.calls()[0].method == 'DELETE'


@pytest.mark.parametrize('status,expected', [(401, 'auth_failed'), (403, 'forbidden'), (422, 'bad_request'), (429, 'rate_limited')])
async def test_error_mapping(status, expected):
    api = FakeAPI().on('POST', RECORDS, status=status, json={'id': 'unauthorized', 'message': 'Unable to authenticate you.'})
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert err.value.code == expected and 'Unable to authenticate you.' in err.value.message
