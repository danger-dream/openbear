import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://dynv6\.com/api/v2'
ZONE = {'id': 7, 'name': 'example.dynv6.net', 'ipv4address': '198.51.100.1', 'ipv6prefix': ''}
RECORDS = [{'id': 1, 'zoneID': 7, 'name': 'home', 'type': 'A', 'data': '198.51.100.2'},
           {'id': 2, 'zoneID': 7, 'name': 'home', 'type': 'AAAA', 'data': '2001:db8::1'}]


def provider(api):
    return get('dynv6')({'token': 'tok'}, client=api.client())


def api_with_zone():
    return FakeAPI().on('GET', BASE + '/zones/by-name/example.dynv6.net', json=ZONE).on('GET', BASE + '/zones/7/records', json=RECORDS)


def test_metadata():
    cls = get('dynv6')
    assert cls.tier == 'A' and cls.docs == 'https://dynv6.com/keys'


async def test_zones_with_bearer():
    api = FakeAPI().on('GET', BASE + '/zones', json=[{'id': 2, 'name': 'b.dynv6.net'}, {'id': 1, 'name': 'a.v6.rocks'}])
    assert (await provider(api).check())['zones'] == ['a.v6.rocks', 'b.dynv6.net']
    assert api.calls()[0].headers['Authorization'] == 'Bearer tok'


async def test_records_match_and_miss():
    api = api_with_zone()
    found = await provider(api).records('example.dynv6.net', 'home.example.dynv6.net', 'A')
    assert [(r.id, r.value) for r in found] == [('1', '198.51.100.2')]
    assert await provider(api).records('example.dynv6.net', 'other.example.dynv6.net', 'A') == []


async def test_apex_is_the_zone_address():
    api = api_with_zone().on('PATCH', BASE + '/zones/7', json=ZONE)
    p = provider(api)
    assert [(r.id, r.value) for r in await p.records('example.dynv6.net', 'example.dynv6.net', 'A')] == [('', '198.51.100.1')]
    assert await p.records('example.dynv6.net', 'example.dynv6.net', 'AAAA') == []
    await p.update('example.dynv6.net', Record('', 'example.dynv6.net', 'A', '198.51.100.1'), '203.0.113.9', None, {})
    await p.create('example.dynv6.net', 'example.dynv6.net', 'AAAA', '2001:db8::9', None, {})
    assert [FakeAPI.body(r) for r in api.calls('PATCH')] == [{'ipv4address': '203.0.113.9'}, {'ipv6prefix': '2001:db8::9'}]


async def test_create():
    api = api_with_zone().on('POST', BASE + '/zones/7/records', json={'id': 9, 'zoneID': 7, 'name': 'home', 'type': 'A', 'data': '203.0.113.5'})
    assert await provider(api).create('example.dynv6.net', 'home.example.dynv6.net', 'A', '203.0.113.5', 300, {}) == '9'
    assert FakeAPI.body(api.calls('POST')[0]) == {'name': 'home', 'type': 'A', 'data': '203.0.113.5'}


async def test_update_patches_only_data():
    api = api_with_zone().on('PATCH', BASE + '/zones/7/records/1', json=RECORDS[0])
    await provider(api).update('example.dynv6.net', Record('1', 'home.example.dynv6.net', 'A', '198.51.100.2'), '203.0.113.9', 600, {})
    assert FakeAPI.body(api.calls('PATCH')[0]) == {'data': '203.0.113.9'}


async def test_delete():
    api = api_with_zone().on('DELETE', BASE + '/zones/7/records/1', status=204)
    await provider(api).delete('example.dynv6.net', Record('1', 'home.example.dynv6.net', 'A', 'x'))
    assert len(api.calls('DELETE')) == 1


@pytest.mark.parametrize('status,expected', [(401, 'auth_failed'), (403, 'forbidden'), (404, 'zone_not_found')])
async def test_error_mapping(status, expected):
    api = FakeAPI().on('GET', BASE + '/zones/by-name/example.dynv6.net', status=status, json={'error': 'x'})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.dynv6.net', 'home.example.dynv6.net', 'A')
    assert err.value.code == expected
