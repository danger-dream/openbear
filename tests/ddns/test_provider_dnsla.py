import base64

import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.dns\.la/api'
AUTH = 'Basic ' + base64.b64encode(b'id1:sec').decode()


def provider(api):
    return get('dnsla')({'api_id': 'id1', 'secret': 'sec'}, client=api.client())


def ok(data):
    return {'code': 200, 'msg': '', 'data': data}


def item(rid, host, value, rtype=1, **extra):
    return {'id': rid, 'domainId': 'D1', 'groupId': '', 'host': host, 'type': rtype, 'lineId': '', 'data': value, 'ttl': 600,
            'weight': 1, 'preference': 1, 'system': False, 'disable': False, **extra}


def with_domain(api):
    return api.on('GET', BASE + '/domainList', json=ok({'total': 1, 'results': [{'id': 'D1', 'domain': 'example.com.'}]}))


def test_metadata():
    cls = get('dnsla')
    assert cls.tier == 'A' and cls.caps.by_id and [f.key for f in cls.fields] == ['api_id', 'secret']


async def test_zones_page_through_domain_list():
    def handler(req, m):
        page = int(req.url.params['pageIndex'])
        items = [{'id': f'{page}-{i}', 'domain': f'd{page}-{i}.com.'} for i in range(100 if page == 1 else 2)]
        return ok({'total': 102, 'results': items})
    api = FakeAPI().on('GET', BASE + '/domainList', handler)
    zones = (await provider(api).check())['zones']
    assert len(zones) == 102 and 'd2-1.com' in zones
    assert dict(api.calls()[0].url.params) == {'pageIndex': '1', 'pageSize': '100'}
    assert api.calls()[0].headers['Authorization'] == AUTH


async def test_records_match_and_miss():
    api = with_domain(FakeAPI()).on('GET', BASE + '/recordList', json=ok({'total': 2, 'results': [
        item('R1', 'home', '198.51.100.1', lineId='L9'), item('R2', 'home2', '198.51.100.2')]}))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl, r.extra['lineId']) for r in found] == [('R1', '198.51.100.1', 600, 'L9')]
    assert dict(api.calls()[1].url.params) == {'pageIndex': '1', 'pageSize': '100', 'domainId': 'D1', 'host': 'home', 'type': '1'}
    assert await provider(api).records('example.com', 'example.com', 'AAAA') == []
    assert api.calls()[-1].url.params['host'] == '@' and api.calls()[-1].url.params['type'] == '28'


async def test_records_of_other_lines_are_not_the_default_lines():
    api = with_domain(FakeAPI()).on('GET', BASE + '/recordList', json=ok({'total': 3, 'results': [
        item('R1', 'home', '198.51.100.1', lineId='L9', lineCode='ct'), item('R2', 'home', '198.51.100.1'),
        item('R3', 'home', '198.51.100.1', lineId='L1', lineCode='default')]}))
    p = provider(api)
    found = await p.records('example.com', 'home.example.com', 'A')
    assert [r.id for r in p.on_line(found, {})] == ['R2', 'R3'] and [r.id for r in p.on_line(found, {'line': 'L9'})] == ['R1']
    api.on('POST', BASE + '/record', json=ok({'id': 'R9'}))
    await p.create('example.com', 'home.example.com', 'A', '198.51.100.1', None, {'line': 'L9'})
    assert FakeAPI.body(api.calls('POST')[-1])['lineId'] == 'L9'


async def test_create():
    api = with_domain(FakeAPI()).on('POST', BASE + '/record', json=ok({'id': 'R9'}))
    assert await provider(api).create('example.com', 'home.example.com', 'AAAA', '2001:db8::5', None, {}) == 'R9'
    assert FakeAPI.body(api.calls('POST')[0]) == {'domainId': 'D1', 'type': 28, 'host': 'home', 'data': '2001:db8::5', 'ttl': 600}


async def test_update_keeps_line_group_and_weight():
    api = FakeAPI().on('PUT', BASE + '/record', json=ok(None))
    rec = Record('R1', 'home.example.com', 'A', '198.51.100.1', 3600, {'lineId': 'L9', 'groupId': 'G1', 'weight': 5})
    await provider(api).update('example.com', rec, '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls()[0]) == {'id': 'R1', 'type': 1, 'host': 'home', 'data': '203.0.113.9', 'ttl': 3600,
                                            'lineId': 'L9', 'groupId': 'G1', 'weight': 5}


async def test_update_reads_missing_attributes():
    api = (with_domain(FakeAPI()).on('GET', BASE + '/recordList', json=ok({'total': 1, 'results': [item('R1', 'home', '198.51.100.1', lineId='L2', ttl=900)]}))
           .on('PUT', BASE + '/record', json=ok(None)))
    await provider(api).update('example.com', Record('R1', 'home.example.com', 'A', '198.51.100.1'), '203.0.113.9', None, {})
    body = FakeAPI.body(api.calls('PUT')[0])
    assert (body['lineId'], body['ttl'], body['weight']) == ('L2', 900, 1)


async def test_delete():
    api = FakeAPI().on('DELETE', BASE + '/record', json=ok(None))
    await provider(api).delete('example.com', Record('R1', 'home.example.com', 'A', '198.51.100.1'))
    assert dict(api.calls()[0].url.params) == {'id': 'R1'}


async def test_unknown_zone():
    with pytest.raises(ProviderError) as err:
        await provider(with_domain(FakeAPI())).records('other.com', 'home.other.com', 'A')
    assert err.value.code == 'zone_not_found'


async def test_http_401():
    api = FakeAPI().on('GET', BASE + '/domainList', status=401, text='unauthorized')
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == 'auth_failed'


@pytest.mark.parametrize('code,expected', [(400, 'bad_request'), (612, 'bad_request'), (500, 'server_error')])
async def test_business_code_in_http_200(code, expected):
    api = FakeAPI().on('GET', BASE + '/domainList', json={'code': code, 'msg': 'failed', 'data': None})
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == expected and 'failed' in err.value.message
