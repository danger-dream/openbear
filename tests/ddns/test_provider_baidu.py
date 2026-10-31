import calendar
import time
from urllib.parse import parse_qsl

import httpx
import pytest

from app.ddns.providers import _sign, get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

CREDS = {'access_key_id': 'bdak', 'secret_access_key': 'bdsk'}
BASE = r'https://dns\.baidubce\.com/v1/dns/zone'
ITEM = {'id': '1234', 'rr': 'home', 'status': 'running', 'type': 'A', 'value': '198.51.100.1', 'ttl': 300, 'line': 'ct',
        'description': 'office'}


def query(request):
    return dict(parse_qsl(request.url.query.decode()))


def signed(handler):
    def wrapper(request, match):
        h = request.headers
        headers = {'Host': h['Host'], 'x-bce-date': h['x-bce-date']}
        if request.content:
            headers['Content-Type'] = h['Content-Type']
        stamp = calendar.timegm(time.strptime(h['x-bce-date'], '%Y-%m-%dT%H:%M:%SZ'))
        assert h['Authorization'] == _sign.bce('bdak', 'bdsk', method=request.method, path=request.url.path, query=query(request),
                                               headers=headers, timestamp=stamp)
        if request.method != 'GET':
            assert query(request)['clientToken']
        return handler(request, match)
    return wrapper


def provider(api):
    return get('baidu')(CREDS, client=api.client())


async def test_check_lists_zones_with_paging():
    pages = {None: {'zones': [{'name': 'example.com.'}], 'isTruncated': True, 'nextMarker': 'm2'},
             'm2': {'zones': [{'name': 'example.net'}], 'isTruncated': False}}
    api = FakeAPI().on('GET', BASE, signed(lambda r, m: pages[query(r).get('marker')]))
    assert await provider(api).check() == {'zones': ['example.com', 'example.net'], 'writable': None}
    assert [query(r).get('marker') for r in api.requests] == [None, 'm2']


async def test_records_filter_by_name_and_type():
    api = FakeAPI().on('GET', BASE + '/example.com/record', signed(lambda r, m: {
        'records': [ITEM, dict(ITEM, id='1235', type='AAAA', value='2001:db8::1')], 'isTruncated': False}))
    records = await provider(api).records('example.com', 'home.example.com', 'A')
    assert records == [Record('1234', 'home.example.com', 'A', '198.51.100.1', 300, {'line': 'ct', 'description': 'office'})]
    assert query(api.requests[0]) == {'rr': 'home', 'maxKeys': '1000'}


async def test_create_reads_back_the_new_id():
    api = FakeAPI()
    api.on('POST', BASE + '/example.com/record', signed(lambda r, m: httpx.Response(200)))
    api.on('GET', BASE + '/example.com/record', signed(lambda r, m: {
        'records': [dict(ITEM, id='1', rr='@', value='198.51.100.7', line='default'), dict(ITEM, id='2', rr='@', value='198.51.100.9', line='default')]}))
    record = await provider(api).create('example.com', 'example.com', 'A', '198.51.100.9', 600, {})
    assert FakeAPI.body(api.requests[0]) == {'rr': '@', 'type': 'A', 'value': '198.51.100.9', 'line': 'default', 'ttl': 600}
    assert record.id == '2' and record.value == '198.51.100.9'


async def test_records_of_other_lines_are_not_the_default_lines():
    """From the 10-30 check: without a line option a name's 电信 record could be taken over as its default one."""
    api = FakeAPI()
    api.on('POST', BASE + '/example.com/record', signed(lambda r, m: httpx.Response(200)))
    api.on('GET', BASE + '/example.com/record', signed(lambda r, m: {
        'records': [dict(ITEM, id='1', rr='@', value='198.51.100.9', line='ct'), dict(ITEM, id='2', rr='@', value='198.51.100.9', line='default')]}))
    p = provider(api)
    found = await p.records('example.com', 'example.com', 'A')
    assert [r.id for r in p.on_line(found, {})] == ['2'] and [r.id for r in p.on_line(found, {'line': 'ct'})] == ['1']
    record = await p.create('example.com', 'example.com', 'A', '198.51.100.9', 600, {'line': 'ct'})
    assert FakeAPI.body(api.requests[-2])['line'] == 'ct' and record.id == '1'


async def test_update_sends_back_ttl_and_description():
    api = FakeAPI().on('PUT', BASE + '/example.com/record/1234', signed(lambda r, m: httpx.Response(200)))
    current = Record('1234', 'home.example.com', 'A', '198.51.100.1', 300, {'line': 'ct', 'description': 'office'})
    updated = await provider(api).update('example.com', current, '198.51.100.2', None, {})
    assert FakeAPI.body(api.requests[0]) == {'rr': 'home', 'type': 'A', 'value': '198.51.100.2', 'ttl': 300, 'description': 'office'}
    assert updated == Record('1234', 'home.example.com', 'A', '198.51.100.2', 300, {'line': 'ct', 'description': 'office'})


async def test_delete():
    api = FakeAPI().on('DELETE', BASE + '/example.com/record/1234', signed(lambda r, m: httpx.Response(200)))
    await provider(api).delete('example.com', Record('1234', 'home.example.com', 'A', '198.51.100.1'))
    assert len(api.requests) == 1


@pytest.mark.parametrize('status,code,expected', [
    (403, 'SignatureDoesNotMatch', 'auth_failed'),
    (403, 'InvalidAccessKeyId', 'auth_failed'),
    (403, 'AccessDenied', 'forbidden'),
    (400, 'BadRequest', 'bad_request'),
    (404, 'NoSuchZone', 'bad_request'),
    (500, 'InternalError', 'server_error'),
])
async def test_http_errors(status, code, expected):
    api = FakeAPI().on('GET', BASE, status=status, json={'code': code, 'message': 'm', 'requestId': 'r'})
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == expected and code in err.value.message


async def test_business_failure_in_http_200():
    api = FakeAPI().on('PUT', BASE + '/example.com/record/1234', json={'code': 'RecordConflict', 'message': 'conflict'})
    with pytest.raises(ProviderError) as err:
        await provider(api).update('example.com', Record('1234', 'home.example.com', 'A', '1.1.1.1'), '198.51.100.2', None, {})
    assert err.value.code == 'bad_request' and 'RecordConflict' in err.value.message


async def test_created_record_missing_from_list_is_invalid():
    api = FakeAPI()
    api.on('POST', BASE + '/example.com/record', lambda r, m: httpx.Response(200))
    api.on('GET', BASE + '/example.com/record', json={'records': []})
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.9', None, {})
    assert err.value.code == 'invalid_response'
