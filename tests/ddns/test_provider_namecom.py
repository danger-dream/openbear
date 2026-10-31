import base64

import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.name\.com/core/v1/domains'
AUTH = 'Basic ' + base64.b64encode(b'jane:tok').decode()


def provider(api):
    return get('namecom')({'username': 'jane', 'token': 'tok'}, client=api.client())


def item(rid=11, host='home', rtype='A', answer='198.51.100.1', ttl=600):
    fqdn = (host + '.' if host else '') + 'example.com.'
    return {'id': rid, 'domainName': 'example.com', 'host': host, 'fqdn': fqdn, 'type': rtype, 'answer': answer, 'ttl': ttl}


def test_metadata():
    cls = get('namecom')
    assert cls.tier == 'A' and cls.caps.ttl_min == 300 and cls.fields[0].key == 'username'


async def test_zones_follow_next_page_with_basic_auth():
    def handler(req, m):
        page = req.url.params['page']
        return {'domains': [{'domainName': 'b.com'}], 'nextPage': 2} if page == '1' else {'domains': [{'domainName': 'a.com'}]}
    api = FakeAPI().on('GET', BASE, handler)
    assert (await provider(api).check())['zones'] == ['a.com', 'b.com']
    assert [r.url.params['page'] for r in api.calls()] == ['1', '2']
    assert api.calls()[0].headers['Authorization'] == AUTH and api.calls()[0].url.params['perPage'] == '1000'


async def test_records_match_and_miss():
    api = FakeAPI().on('GET', BASE + '/example.com/records', json={'records': [item(), item(12, 'home', 'AAAA', '2001:db8::1'), item(13, '')]})
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.name, r.value, r.ttl) for r in found] == [('11', 'home.example.com', '198.51.100.1', 600)]
    assert [r.id for r in await provider(api).records('example.com', 'example.com', 'A')] == ['13']
    assert await provider(api).records('example.com', 'none.example.com', 'A') == []


async def test_create():
    api = FakeAPI().on('POST', BASE + '/example.com/records', lambda req, m: {**item(99), **FakeAPI.body(req)})
    rec = await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 60, {})
    assert FakeAPI.body(api.calls()[0]) == {'host': 'home', 'type': 'A', 'answer': '203.0.113.5', 'ttl': 300}
    assert rec.id == '99' and rec.value == '203.0.113.5'


async def test_update_sends_full_record_with_old_ttl():
    api = FakeAPI().on('PUT', BASE + '/example.com/records/11', lambda req, m: {**item(), **FakeAPI.body(req)})
    await provider(api).update('example.com', Record('11', 'example.com', 'A', '198.51.100.1', 3600), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls()[0]) == {'host': '', 'type': 'A', 'answer': '203.0.113.9', 'ttl': 3600}
    await provider(api).update('example.com', Record('11', 'home.example.com', 'A', '198.51.100.1', 3600), '203.0.113.9', 900, {})
    assert FakeAPI.body(api.calls()[1]) == {'host': 'home', 'type': 'A', 'answer': '203.0.113.9', 'ttl': 900}


async def test_delete():
    api = FakeAPI().on('DELETE', BASE + '/example.com/records/11', json={})
    await provider(api).delete('example.com', Record('11', 'home.example.com', 'A', 'x'))
    assert len(api.calls()) == 1


@pytest.mark.parametrize('status,expected', [(401, 'auth_failed'), (403, 'forbidden'), (404, 'zone_not_found'), (400, 'bad_request')])
async def test_error_mapping(status, expected):
    api = FakeAPI().on('GET', BASE + '/example.com/records', status=status, json={'message': 'Unauthenticated', 'details': 'x'})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == expected
