import httpx
import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://desec\.io/api/v1'
RRSET = BASE + '/domains/example.com/rrsets/home/A/'
DOMAIN = BASE + '/domains/example.com/'


def provider(api):
    return get('desec')({'token': 'tok'}, client=api.client())


def rrset(*values, ttl=3600):
    return {'domain': 'example.com', 'subname': 'home', 'name': 'home.example.com.', 'type': 'A', 'records': list(values), 'ttl': ttl}


def test_metadata():
    cls = get('desec')
    assert cls.tier == 'A' and not cls.caps.by_id and cls.caps.multi


async def test_zones_follow_cursor_links():
    def handler(req, m):
        if req.url.params['cursor'] == '':
            return httpx.Response(200, json=[{'name': 'b.com'}], headers={'Link': '<https://desec.io/api/v1/domains/?cursor=abc>; rel="next"'})
        return [{'name': 'a.com'}]
    api = FakeAPI().on('GET', BASE + '/domains/', handler)
    assert (await provider(api).check())['zones'] == ['a.com', 'b.com']
    assert [r.url.params['cursor'] for r in api.calls()] == ['', 'abc']
    assert api.calls()[0].headers['Authorization'] == 'Token tok'


async def test_records_match_and_miss():
    api = (FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1', '198.51.100.2'))
           .on('GET', BASE + '/domains/example.com/rrsets/@/A/', status=404, json={'detail': 'Not found.'})
           .on('GET', DOMAIN, json={'name': 'example.com', 'minimum_ttl': 3600}))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('', '198.51.100.1', 3600), ('', '198.51.100.2', 3600)]
    assert await provider(api).records('example.com', 'example.com', 'A') == []


async def test_missing_zone():
    api = FakeAPI().on('GET', RRSET, status=404, json={'detail': 'Not found.'}).on('GET', DOMAIN, status=404, json={'detail': 'Not found.'})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == 'zone_not_found'


async def test_create_new_rrset_respects_domain_minimum_ttl():
    api = (FakeAPI().on('GET', RRSET, status=404, json={'detail': 'Not found.'})
           .on('GET', DOMAIN, json={'name': 'example.com', 'minimum_ttl': 3600})
           .on('POST', BASE + '/domains/example.com/rrsets/', status=201, json=rrset('203.0.113.5')))
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 60, {}) == ''
    assert FakeAPI.body(api.calls('POST')[0]) == {'subname': 'home', 'type': 'A', 'records': ['203.0.113.5'], 'ttl': 3600}


async def test_create_adds_to_existing_rrset():
    api = FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1')).on('PATCH', RRSET, json=rrset())
    await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert FakeAPI.body(api.calls('PATCH')[0]) == {'records': ['198.51.100.1', '203.0.113.5']}


async def test_update_replaces_one_value_and_keeps_ttl():
    api = (FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1', '198.51.100.2'))
           .on('GET', DOMAIN, json={'name': 'example.com', 'minimum_ttl': 60}).on('PATCH', RRSET, json=rrset()))
    await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.2', 3600), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls('PATCH')[0]) == {'records': ['198.51.100.1', '203.0.113.9']}
    await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.1', 3600), '203.0.113.9', 60, {})
    assert FakeAPI.body(api.calls('PATCH')[1]) == {'records': ['203.0.113.9', '198.51.100.2'], 'ttl': 60}


async def test_delete_removes_value_and_empty_set_deletes():
    api = FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1')).on('PATCH', RRSET, status=204)
    await provider(api).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert FakeAPI.body(api.calls('PATCH')[0]) == {'records': []}


@pytest.mark.parametrize('status,expected', [(401, 'auth_failed'), (403, 'forbidden'), (429, 'rate_limited'), (400, 'bad_request')])
async def test_error_mapping(status, expected):
    api = FakeAPI().on('GET', BASE + '/domains/', status=status, json={'detail': 'Invalid token.'}, headers={'Retry-After': '3'})
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == expected
