import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://spaceship\.dev/api/v1'
RECORDS = BASE + '/dns/records/example.com'


def provider(api):
    return get('spaceship')({'key': 'k', 'secret': 's'}, client=api.client())


def item(name='home', address='198.51.100.1', rtype='A'):
    return {'type': rtype, 'name': name, 'address': address, 'ttl': 1800, 'group': {'type': 'custom'}}


def test_metadata():
    cls = get('spaceship')
    assert cls.tier == 'A' and not cls.caps.by_id and cls.caps.ttl_max == 3600


async def test_zones_page_with_take_and_skip():
    def handler(req, m):
        skip = int(req.url.params['skip'])
        return {'items': [{'name': f'd{skip + i}.com'} for i in range(100 if skip == 0 else 1)], 'total': 101}
    api = FakeAPI().on('GET', BASE + '/domains', handler)
    assert len((await provider(api).check())['zones']) == 101
    assert [r.url.params['skip'] for r in api.calls()] == ['0', '100']
    assert api.calls()[0].headers['X-API-Key'] == 'k' and api.calls()[0].headers['X-API-Secret'] == 's'


async def test_records_match_and_miss():
    api = FakeAPI().on('GET', RECORDS, json={'items': [item(), item('@'), item('home', '2001:db8::1', 'AAAA')], 'total': 3})
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('', '198.51.100.1', 1800)]
    assert api.calls()[0].url.params['take'] == '500'
    assert [r.value for r in await provider(api).records('example.com', 'example.com', 'A')] == ['198.51.100.1']
    assert await provider(api).records('example.com', 'none.example.com', 'A') == []


async def test_create():
    api = FakeAPI().on('PUT', RECORDS, status=204)
    await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 7200, {})
    assert FakeAPI.body(api.calls()[0]) == {'force': False, 'items': [{'type': 'A', 'name': 'home', 'address': '203.0.113.5', 'ttl': 3600}]}


async def test_update_saves_new_value_with_old_ttl_then_removes_old():
    api = FakeAPI().on('PUT', RECORDS, status=204).on('DELETE', RECORDS, status=204)
    await provider(api).update('example.com', Record('', 'example.com', 'A', '198.51.100.1', 1800), '203.0.113.9', None, {})
    put, delete = api.calls()
    assert put.method == 'PUT' and FakeAPI.body(put) == {'force': False, 'items': [{'type': 'A', 'name': '@', 'address': '203.0.113.9', 'ttl': 1800}]}
    assert delete.method == 'DELETE' and FakeAPI.body(delete) == [{'type': 'A', 'name': '@', 'address': '198.51.100.1'}]


async def test_delete():
    api = FakeAPI().on('DELETE', RECORDS, status=204)
    await provider(api).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert FakeAPI.body(api.calls()[0]) == [{'type': 'A', 'name': 'home', 'address': '198.51.100.1'}]


@pytest.mark.parametrize('status,expected', [(401, 'auth_failed'), (403, 'forbidden'), (404, 'zone_not_found'), (422, 'bad_request'),
                                             (429, 'rate_limited')])
async def test_error_mapping(status, expected):
    api = FakeAPI().on('GET', RECORDS, status=status, json={'detail': 'x'}, headers={'spaceship-error-code': 'business.x'})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == expected and 'business.x' in err.value.message
