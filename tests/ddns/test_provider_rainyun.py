import json

import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.v2\.rainyun\.com/product/domain'
DOMAINS = BASE + '/'
DNS = BASE + '/55/dns/?'


def provider(api):
    return get('rainyun')({'key': 'k'}, client=api.client())


def ok(data):
    return {'code': 200, 'data': data}


def record(rid, host, value, rtype='A', **extra):
    return {'record_id': rid, 'host': host, 'value': value, 'type': rtype, 'ttl': 300, 'line': 'DEFAULT', 'is_pause': False, 'level': 10, **extra}


def with_domain(api):
    return api.on('GET', DOMAINS, json=ok({'TotalRecords': 1, 'Records': [{'id': 55, 'domain': 'Example.com'}]}))


def test_metadata():
    cls = get('rainyun')
    assert cls.tier == 'A' and cls.caps.by_id and not cls.options


async def test_zones_page_through_domain_list():
    def handler(req, m):
        options = json.loads(req.url.params['options'])
        page = options['page']
        items = [{'id': page * 1000 + i, 'domain': f'd{page}-{i}.com'} for i in range(100 if page == 1 else 3)]
        return ok({'TotalRecords': 103, 'Records': items})
    api = FakeAPI().on('GET', DOMAINS, handler)
    assert len((await provider(api).check())['zones']) == 103
    first = json.loads(api.calls()[0].url.params['options'])
    assert first == {'columnFilters': {'domains.Domain': ''}, 'sort': [], 'page': 1, 'perPage': 100}
    assert api.calls()[0].headers['x-api-key'] == 'k' and len(api.calls()) == 2


async def test_records_match_and_miss_with_pages():
    def handler(req, m):
        page = int(req.url.params['page_no'])
        items = [record(i, f'other{i}', '192.0.2.1') for i in range(100)] if page == 1 else [record(7, 'home', '198.51.100.1', line='LTEL')]
        return ok({'TotalRecords': 101, 'Records': items})
    api = with_domain(FakeAPI()).on('GET', DNS, handler)
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl, r.extra) for r in found] == [('7', '198.51.100.1', 300, {'line': 'LTEL', 'level': 10})]
    assert dict(api.calls()[1].url.params) == {'limit': '100', 'page_no': '1'}
    assert await provider(api).records('example.com', 'example.com', 'A') == []


async def test_unknown_zone():
    api = with_domain(FakeAPI())
    with pytest.raises(ProviderError) as err:
        await provider(api).records('other.com', 'home.other.com', 'A')
    assert err.value.code == 'zone_not_found'


async def test_create():
    api = with_domain(FakeAPI()).on('POST', BASE + '/55/dns', json=ok(141202856))
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {}) == '141202856'
    assert FakeAPI.body(api.calls('POST')[0]) == {'record_id': 0, 'host': 'home', 'type': 'A', 'value': '203.0.113.5', 'line': 'DEFAULT',
                                                  'level': 10, 'ttl': 600}


async def test_update_resends_line_level_and_ttl():
    api = with_domain(FakeAPI()).on('PATCH', BASE + '/55/dns', json=ok('ok'))
    rec = Record('7', 'example.com', 'A', '198.51.100.1', 300, {'line': 'LTEL', 'level': 20})
    await provider(api).update('example.com', rec, '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls('PATCH')[0]) == {'record_id': 7, 'host': '@', 'type': 'A', 'value': '203.0.113.9', 'line': 'LTEL',
                                                   'level': 20, 'ttl': 300}


async def test_update_reads_missing_attributes():
    api = (with_domain(FakeAPI()).on('GET', DNS, json=ok({'TotalRecords': 1, 'Records': [record(7, 'home', '198.51.100.1', line='LUNI', level=5)]}))
           .on('PATCH', BASE + '/55/dns', json=ok('ok')))
    await provider(api).update('example.com', Record('7', 'home.example.com', 'A', '198.51.100.1'), '203.0.113.9', 120, {})
    body = FakeAPI.body(api.calls('PATCH')[0])
    assert (body['line'], body['level'], body['ttl']) == ('LUNI', 5, 120)


async def test_delete():
    api = with_domain(FakeAPI()).on('DELETE', DNS, json=ok('ok'))
    await provider(api).delete('example.com', Record('7', 'home.example.com', 'A', '198.51.100.1'))
    call = api.calls('DELETE')[0]
    assert call.url.params['record_id'] == '7' and FakeAPI.body(call) == {'record_id': 7}


@pytest.mark.parametrize('status,body,expected', [
    (401, {'code': 401, 'message': 'api key invalid'}, 'auth_failed'),
    (200, {'code': 401, 'message': 'api key invalid'}, 'auth_failed'),
    (200, {'code': 30002, 'message': 'record exists'}, 'bad_request'),
    (500, {'code': 500, 'message': 'boom'}, 'server_error'),
])
async def test_error_mapping(status, body, expected):
    api = FakeAPI().on('GET', DOMAINS, status=status, json=body)
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == expected and err.value.message == body['message']
