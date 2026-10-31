import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.porkbun\.com/api/json/v3'
KEYS = {'apikey': 'pk1_a', 'secretapikey': 'sk1_b'}


def provider(api):
    return get('porkbun')({'api_key': 'pk1_a', 'secret_key': 'sk1_b'}, client=api.client())


def item(rid='11', name='home.example.com', content='198.51.100.1'):
    return {'id': rid, 'name': name, 'type': 'A', 'content': content, 'ttl': '600', 'prio': None, 'notes': 'kept'}


def test_metadata():
    cls = get('porkbun')
    assert cls.tier == 'A' and cls.options == () and cls.caps.ttl_min == 600


async def test_check_pages_domains_and_reads_scopes():
    def domains(req, m):
        start = FakeAPI.body(req)['start']
        return {'status': 'SUCCESS', 'domains': [{'domain': f'd{start + i}.com'} for i in range(1000 if start == 0 else 2)]}
    api = (FakeAPI().on('POST', BASE + '/ping', json={'status': 'SUCCESS', 'yourIp': '1.2.3.4', 'credentialsValid': True, 'scopes': ['read', 'dns']})
           .on('POST', BASE + '/domain/listAll', domains))
    out = await provider(api).check()
    assert len(out['zones']) == 1002 and out['writable'] is True
    assert FakeAPI.body(api.calls()[0]) == KEYS
    assert [FakeAPI.body(r)['start'] for r in api.calls()[1:]] == [0, 1000]
    read_only = FakeAPI().on('POST', BASE + '/ping', json={'status': 'SUCCESS', 'scopes': ['read']}).on(
        'POST', BASE + '/domain/listAll', json={'status': 'SUCCESS', 'domains': []})
    assert (await provider(read_only).check())['writable'] is False


async def test_records_by_name_and_type():
    api = (FakeAPI().on('POST', BASE + '/dns/retrieveByNameType/example.com/A/home', json={'status': 'SUCCESS', 'records': [item()]})
           .on('POST', BASE + '/dns/retrieveByNameType/example.com/A', json={'status': 'SUCCESS', 'records': []}))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('11', '198.51.100.1', 600)]
    assert FakeAPI.body(api.calls()[0]) == KEYS
    assert await provider(api).records('example.com', 'example.com', 'A') == []


async def test_create():
    api = FakeAPI().on('POST', BASE + '/dns/create/example.com', json={'status': 'SUCCESS', 'id': '123456789'})
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 300, {}) == '123456789'
    assert FakeAPI.body(api.calls()[0]) == {**KEYS, 'name': 'home', 'type': 'A', 'content': '203.0.113.5', 'ttl': 600}
    await provider(api).create('example.com', 'example.com', 'AAAA', '2001:db8::1', None, {})
    assert FakeAPI.body(api.calls()[1]) == {**KEYS, 'name': '', 'type': 'AAAA', 'content': '2001:db8::1'}


async def test_update_sends_only_changed_fields():
    api = FakeAPI().on('POST', BASE + '/dns/edit/example.com/11', json={'status': 'SUCCESS'})
    await provider(api).update('example.com', Record('11', 'home.example.com', 'A', '198.51.100.1', 600), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls()[0]) == {**KEYS, 'content': '203.0.113.9'}
    await provider(api).update('example.com', Record('11', 'home.example.com', 'A', '198.51.100.1', 600), '203.0.113.9', 1200, {})
    assert FakeAPI.body(api.calls()[1]) == {**KEYS, 'content': '203.0.113.9', 'ttl': 1200}


async def test_delete():
    api = FakeAPI().on('POST', BASE + '/dns/delete/example.com/11', json={'status': 'SUCCESS'})
    await provider(api).delete('example.com', Record('11', 'home.example.com', 'A', 'x'))
    assert len(api.calls()) == 1


@pytest.mark.parametrize('status,code,expected', [(400, 'INVALID_API_KEYS_001', 'auth_failed'), (403, 'INSUFFICIENT_SCOPE', 'forbidden'),
                                                  (400, 'DOMAIN_NOT_FOUND', 'zone_not_found'), (400, 'INVALID_INPUT', 'bad_request'),
                                                  (200, 'X', 'bad_request'), (429, 'RATE_LIMIT_EXCEEDED', 'rate_limited')])
async def test_error_mapping(status, code, expected):
    api = FakeAPI().on('POST', BASE + '/dns/create/example.com', status=status, json={'status': 'ERROR', 'code': code, 'message': 'm'})
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert err.value.code == expected
