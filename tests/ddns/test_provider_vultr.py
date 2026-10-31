import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.vultr\.com/v2'
RECORDS = BASE + '/domains/example.com/records'
REC_ID = 'cb676a46-66fd-4dfb-b839-443f2e6c0b60'


def provider(api):
    return get('vultr')({'key': 'k'}, client=api.client())


def rec(rid, name, data, rtype='A'):
    return {'id': rid, 'type': rtype, 'name': name, 'data': data, 'priority': -1, 'ttl': 300}


def listing(key, items, next_cursor=''):
    return {key: items, 'meta': {'total': len(items), 'links': {'next': next_cursor, 'prev': ''}}}


def test_metadata():
    cls = get('vultr')
    assert cls.tier == 'A' and cls.caps.by_id


async def test_zones_follow_cursor():
    def handler(req, m):
        if req.url.params.get('cursor') == 'c2':
            return listing('domains', [{'domain': 'a.com'}])
        return listing('domains', [{'domain': 'b.com'}], 'c2')
    api = FakeAPI().on('GET', BASE + '/domains', handler)
    assert (await provider(api).check())['zones'] == ['a.com', 'b.com']
    assert [dict(r.url.params) for r in api.calls()] == [{'per_page': '500'}, {'per_page': '500', 'cursor': 'c2'}]
    assert api.calls()[0].headers['Authorization'] == 'Bearer k'


async def test_records_match_and_miss():
    api = FakeAPI().on('GET', RECORDS, json=listing('records', [rec(REC_ID, 'home', '198.51.100.1'), rec('r2', '', '198.51.100.2'),
                                                                rec('r3', 'home', '2001:db8::1', 'AAAA')]))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [(REC_ID, '198.51.100.1', 300)]
    assert [r.id for r in await provider(api).records('example.com', 'example.com', 'A')] == ['r2']
    assert await provider(api).records('example.com', 'none.example.com', 'A') == []


async def test_missing_zone():
    api = FakeAPI().on('GET', RECORDS, status=404, json={'error': 'Domain not found', 'status': 404})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == 'zone_not_found'


async def test_create():
    api = FakeAPI().on('POST', RECORDS, status=201, json={'record': rec(REC_ID, 'home', '203.0.113.5')})
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 120, {}) == REC_ID
    assert FakeAPI.body(api.calls()[0]) == {'name': 'home', 'type': 'A', 'data': '203.0.113.5', 'ttl': 120}


async def test_update_patches_only_data_and_given_ttl():
    api = FakeAPI().on('PATCH', RECORDS + '/' + REC_ID, status=204)
    await provider(api).update('example.com', Record(REC_ID, 'home.example.com', 'A', '198.51.100.1', 300), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls()[0]) == {'data': '203.0.113.9'}
    await provider(api).update('example.com', Record(REC_ID, 'home.example.com', 'A', '198.51.100.1', 300), '203.0.113.9', 600, {})
    assert FakeAPI.body(api.calls()[1]) == {'data': '203.0.113.9', 'ttl': 600}


async def test_delete():
    api = FakeAPI().on('DELETE', RECORDS + '/' + REC_ID, status=204)
    await provider(api).delete('example.com', Record(REC_ID, 'home.example.com', 'A', '198.51.100.1'))
    assert len(api.calls()) == 1


@pytest.mark.parametrize('status,expected', [(401, 'auth_failed'), (403, 'forbidden'), (400, 'bad_request'), (503, 'server_error')])
async def test_error_mapping(status, expected):
    api = FakeAPI().on('POST', RECORDS, status=status, json={'error': 'Invalid API token.', 'status': status})
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert err.value.code == expected and err.value.message == 'Invalid API token.'
