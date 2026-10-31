import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.gcore\.com/dns/v2'
RRSET = BASE + '/zones/example.com/home.example.com/A'


def provider(api):
    return get('gcore')({'token': 'tok'}, client=api.client())


def rrset(*values, ttl=300):
    return {'name': 'home.example.com', 'type': 'A', 'ttl': ttl, 'meta': {'geodns_link': 'cdn'}, 'filter_set_id': 1,
            'resource_records': [{'id': i, 'content': [v], 'enabled': i != 2, 'meta': {'weight': i}} for i, v in enumerate(values, 1)]}


def test_metadata():
    cls = get('gcore')
    assert cls.tier == 'A' and not cls.caps.by_id


async def test_zones_page_by_offset():
    def handler(req, m):
        offset = int(req.url.params['offset'])
        return {'zones': [{'name': 'b.com'}, {'name': 'a.com'}] if offset == 0 else [{'name': 'c.com'}], 'total_amount': 3}
    api = FakeAPI().on('GET', BASE + '/zones', handler)
    assert (await provider(api).check())['zones'] == ['a.com', 'b.com', 'c.com']
    assert [r.url.params['offset'] for r in api.calls()] == ['0', '2']
    assert api.calls()[0].headers['Authorization'] == 'APIKey tok'


async def test_records_match_and_miss():
    api = (FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1', '198.51.100.2'))
           .on('GET', BASE + '/zones/example.com/x.example.com/A', status=404, json={'error': 'rrset not found'})
           .on('GET', BASE + '/zones/example.com', json={'name': 'example.com'}))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.value, r.ttl) for r in found] == [('198.51.100.1', 300), ('198.51.100.2', 300)]
    assert await provider(api).records('example.com', 'x.example.com', 'A') == []


async def test_create_new_rrset():
    api = (FakeAPI().on('GET', RRSET, status=404, json={'error': 'not found'}).on('GET', BASE + '/zones/example.com', json={'name': 'example.com'})
           .on('POST', RRSET, json=rrset('203.0.113.5')))
    await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 120, {})
    assert FakeAPI.body(api.calls('POST')[0]) == {'resource_records': [{'content': ['203.0.113.5']}], 'ttl': 120}


async def test_update_keeps_meta_pickers_and_siblings():
    api = FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1', '198.51.100.2')).on('PUT', RRSET, json={})
    await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.2', 300), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls('PUT')[0]) == {
        'resource_records': [{'content': ['198.51.100.1'], 'enabled': True, 'meta': {'weight': 1}},
                             {'content': ['203.0.113.9'], 'enabled': False, 'meta': {'weight': 2}}],
        'meta': {'geodns_link': 'cdn'}, 'ttl': 300}


async def test_delete_value_or_whole_rrset():
    api = FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1', '198.51.100.2')).on('PUT', RRSET, json={})
    await provider(api).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert [r['content'] for r in FakeAPI.body(api.calls('PUT')[0])['resource_records']] == [['198.51.100.2']]
    single = FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1')).on('DELETE', RRSET, json={})
    await provider(single).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert len(single.calls('DELETE')) == 1


@pytest.mark.parametrize('status,expected', [(401, 'auth_failed'), (403, 'forbidden'), (400, 'bad_request'), (500, 'server_error')])
async def test_error_mapping(status, expected):
    api = FakeAPI().on('GET', BASE + '/zones', status=status, json={'error': 'Unauthorized'})
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == expected


async def test_missing_zone():
    api = FakeAPI().on('GET', RRSET, status=404, json={'error': 'x'}).on('GET', BASE + '/zones/example.com', status=404, json={'error': 'x'})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == 'zone_not_found'
