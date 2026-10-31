from urllib.parse import parse_qs

import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.cloudns\.net'
OK = {'status': 'Success', 'statusDescription': 'ok'}


def provider(api, **creds):
    return get('cloudns')({'auth_id': '123', 'password': 'pw', **creds}, client=api.client())


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}


def record(rid, host, value, rtype='A', ttl='3600'):
    return {'id': rid, 'type': rtype, 'host': host, 'record': value, 'ttl': ttl, 'status': 1}


def test_metadata():
    cls = get('cloudns')
    assert cls.tier == 'A' and cls.caps.by_id and cls.docs.startswith('https://www.cloudns.net/')


async def test_check_logs_in_and_pages_zones():
    def zones(req, m):
        page = int(form(req)['page'])
        if page == 1:
            return [{'name': f'z{i}.com', 'type': 'master'} for i in range(100)]
        return [{'name': 'a.com', 'type': 'master'}, {'name': 'b.com', 'type': 'slave'}]
    api = FakeAPI().on('POST', BASE + '/login/login.json', json=OK).on('POST', BASE + '/dns/list-zones.json', zones)
    result = await provider(api).check()
    assert len(result['zones']) == 101 and 'a.com' in result['zones'] and 'b.com' not in result['zones']
    login, first, second = api.calls()
    assert form(login) == {'auth-id': '123', 'auth-password': 'pw'}
    assert form(first)['rows-per-page'] == '100' and form(second)['page'] == '2'


async def test_sub_auth_user_kind():
    api = FakeAPI().on('POST', BASE + '/login/login.json', json=OK).on('POST', BASE + '/dns/list-zones.json', json=[])
    await provider(api, kind='sub-auth-user').check()
    assert form(api.calls()[0]) == {'sub-auth-user': '123', 'auth-password': 'pw'}


async def test_records_match_and_miss():
    items = {'1': record('1', 'home', '198.51.100.1'), '2': record('2', 'homelab', '198.51.100.2')}
    api = FakeAPI().on('POST', BASE + '/dns/records.json', json=items)
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('1', '198.51.100.1', 3600)]
    assert form(api.calls()[0]) == {'auth-id': '123', 'auth-password': 'pw', 'domain-name': 'example.com', 'host': 'home',
                                    'type': 'A', 'rows-per-page': '100', 'page': '1'}
    api = FakeAPI().on('POST', BASE + '/dns/records.json', json=[])
    assert await provider(api).records('example.com', 'example.com', 'A') == []
    assert form(api.calls()[0])['host'] == ''


async def test_create_rounds_ttl_to_allowed_value():
    api = FakeAPI().on('POST', BASE + '/dns/add-record.json', json={**OK, 'data': {'id': 42}})
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 600, {}) == '42'
    assert form(api.calls()[0]) == {'auth-id': '123', 'auth-password': 'pw', 'domain-name': 'example.com', 'record-type': 'A',
                                    'host': 'home', 'record': '203.0.113.5', 'ttl': '900'}


async def test_update_keeps_record_ttl():
    api = FakeAPI().on('POST', BASE + '/dns/mod-record.json', json=OK)
    await provider(api).update('example.com', Record('7', 'example.com', 'A', '198.51.100.1', 21600), '203.0.113.9', None, {})
    assert form(api.calls()[0]) == {'auth-id': '123', 'auth-password': 'pw', 'domain-name': 'example.com', 'record-id': '7',
                                    'host': '', 'record': '203.0.113.9', 'ttl': '21600'}


async def test_delete():
    api = FakeAPI().on('POST', BASE + '/dns/delete-record.json', json=OK)
    await provider(api).delete('example.com', Record('7', 'home.example.com', 'A', '198.51.100.1'))
    assert form(api.calls()[0]) == {'auth-id': '123', 'auth-password': 'pw', 'domain-name': 'example.com', 'record-id': '7'}


@pytest.mark.parametrize('description,expected', [
    ('Invalid authentication, incorrect auth-id or auth-password.', 'auth_failed'),
    ('Missing domain-name', 'zone_not_found'),
    ('Invalid record', 'bad_request'),
])
async def test_failed_status_in_http_200(description, expected):
    api = FakeAPI().on('POST', BASE + '/dns/records.json', json={'status': 'Failed', 'statusDescription': description})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == expected
