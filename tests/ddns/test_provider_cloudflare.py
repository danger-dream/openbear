import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.cloudflare\.com/client/v4'
ZONE = {'success': True, 'result': [{'id': 'z1', 'name': 'example.com'}], 'result_info': {'page': 1, 'total_pages': 1}}


def provider(api, **settings):
    return get('cloudflare')({'token': 'tok'}, settings, client=api.client())


def item(rid='r1', name='home.example.com', content='198.51.100.1', ttl=300, proxied=True):
    return {'id': rid, 'name': name, 'type': 'A', 'content': content, 'ttl': ttl, 'proxied': proxied, 'comment': 'kept'}


def test_metadata():
    cls = get('cloudflare')
    assert cls.tier == 'A' and cls.caps.ttl_auto and [o.key for o in cls.options] == ['proxied']
    assert cls.public()['docs'].startswith('https://developers.cloudflare.com/')


async def test_zones_follow_pages_and_send_bearer():
    pages = {1: ['b.com', 'a.com'], 2: ['c.com']}
    api = FakeAPI().on('GET', BASE + '/zones', lambda req, m: {
        'success': True, 'result': [{'id': n, 'name': n} for n in pages[int(req.url.params['page'])]],
        'result_info': {'page': int(req.url.params['page']), 'total_pages': 2}})
    out = await provider(api).check()
    assert out == {'zones': ['a.com', 'b.com', 'c.com'], 'writable': None}
    assert [r.url.params['page'] for r in api.calls()] == ['1', '2']
    assert all(r.headers['Authorization'] == 'Bearer tok' for r in api.calls())


async def test_records_match_exactly():
    api = (FakeAPI().on('GET', BASE + '/zones', json=ZONE)
           .on('GET', BASE + '/zones/z1/dns_records', json={'success': True, 'result': [item(), item('r2', 'x.home.example.com')],
                                                            'result_info': {'total_pages': 1}}))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl, r.extra) for r in found] == [('r1', '198.51.100.1', 300, {'proxied': True})]
    req = api.calls()[-1]
    assert req.url.params['type'] == 'A' and req.url.params['name'] == 'home.example.com'
    assert api.calls()[0].url.params['name'] == 'example.com'
    assert await provider(api).records('example.com', 'none.example.com', 'A') == []


async def test_create_uses_auto_ttl_by_default():
    api = (FakeAPI().on('GET', BASE + '/zones', json=ZONE)
           .on('POST', BASE + '/zones/z1/dns_records', lambda req, m: {'success': True, 'result': {**item(), **FakeAPI.body(req), 'id': 'new'}}))
    rec = await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert FakeAPI.body(api.calls('POST')[0]) == {'type': 'A', 'name': 'home.example.com', 'content': '203.0.113.5', 'ttl': 1}
    assert rec.id == 'new'
    await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 120, {'proxied': False})
    assert FakeAPI.body(api.calls('POST')[1]) == {'type': 'A', 'name': 'home.example.com', 'content': '203.0.113.5', 'ttl': 120, 'proxied': False}


async def test_update_patches_only_value_and_given_keys():
    api = (FakeAPI().on('GET', BASE + '/zones', json=ZONE)
           .on('PATCH', BASE + '/zones/z1/dns_records/r1', lambda req, m: {'success': True, 'result': {**item(), **FakeAPI.body(req)}}))
    old = Record('r1', 'home.example.com', 'A', '198.51.100.1', 300, {'proxied': True})
    rec = await provider(api).update('example.com', old, '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls('PATCH')[0]) == {'content': '203.0.113.9'}
    assert rec.extra == {'proxied': True} and rec.ttl == 300
    await provider(api).update('example.com', old, '203.0.113.9', 600, {'proxied': False})
    assert FakeAPI.body(api.calls('PATCH')[1]) == {'content': '203.0.113.9', 'ttl': 600, 'proxied': False}
    await provider(api).update('example.com', old, '203.0.113.9', 1, {})
    assert FakeAPI.body(api.calls('PATCH')[2]) == {'content': '203.0.113.9', 'ttl': 1}


async def test_delete():
    api = (FakeAPI().on('GET', BASE + '/zones', json=ZONE)
           .on('DELETE', BASE + '/zones/z1/dns_records/r1', json={'success': True, 'result': {'id': 'r1'}}))
    await provider(api).delete('example.com', Record('r1', 'home.example.com', 'A', '198.51.100.1'))
    assert len(api.calls('DELETE')) == 1


async def test_unknown_zone():
    api = FakeAPI().on('GET', BASE + '/zones', json={'success': True, 'result': [], 'result_info': {'total_pages': 1}})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == 'zone_not_found'


@pytest.mark.parametrize('status,body,code', [
    (400, {'success': False, 'errors': [{'code': 6003, 'message': 'Invalid request headers'}]}, 'auth_failed'),
    (403, {'success': False, 'errors': [{'code': 10000, 'message': 'Authentication error'}]}, 'forbidden'),
    (200, {'success': False, 'errors': [{'code': 81057, 'message': 'Record already exists.'}]}, 'bad_request'),
    (502, None, 'server_error'),
])
async def test_error_mapping(status, body, code):
    api = FakeAPI().on('GET', BASE + '/zones', status=status, json=body) if body else FakeAPI().on('GET', BASE + '/zones', status=status, text='<html>')
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == code
