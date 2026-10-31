from urllib.parse import parse_qsl

import pytest

from app.ddns.providers import _sign, get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

CREDS = {'access_key_id': 'HWAKTEST', 'secret_access_key': 'hwsecret'}
BASE = r'https://dns\.myhuaweicloud\.com'
ZONE = {'id': 'zid-1', 'name': 'example.com.', 'zone_type': 'public'}
RRSET = {'id': 'rs-1', 'name': 'home.example.com.', 'type': 'A', 'records': ['198.51.100.1'], 'ttl': 300, 'line': 'Dianxin',
         'weight': 5, 'description': 'kept'}


def query(request):
    return dict(parse_qsl(request.url.query.decode()))


def signed(handler):
    def wrapper(request, match):
        h = request.headers
        expected = _sign.huawei('HWAKTEST', 'hwsecret', method=request.method, path=request.url.path, query=query(request),
                                headers={'Content-Type': h['Content-Type'], 'Host': h['Host'], 'X-Sdk-Date': h['X-Sdk-Date']},
                                body=request.content, stamp=h['X-Sdk-Date'])
        assert h['Authorization'] == expected
        assert expected.startswith('SDK-HMAC-SHA256 Access=HWAKTEST, SignedHeaders=content-type;host;x-sdk-date, ')
        return handler(request, match)
    return wrapper


def fake():
    api = FakeAPI()
    api.on('GET', BASE + '/v2/zones', signed(lambda r, m: {'zones': [ZONE, {'id': 'zid-2', 'name': 'example.net.'}]}))
    return api


def provider(api):
    return get('huawei')(CREDS, client=api.client())


async def test_check_lists_public_zones():
    api = fake()
    assert await provider(api).check() == {'zones': ['example.com', 'example.net'], 'writable': None}
    assert query(api.requests[0]) == {'type': 'public', 'limit': '500', 'offset': '0'}


async def test_zone_lookup_by_exact_name():
    api = FakeAPI().on('GET', BASE + '/v2/zones', signed(lambda r, m: {'zones': []}))
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.org', 'home.example.org', 'A')
    assert err.value.code == 'zone_not_found'
    assert query(api.requests[0])['name'] == 'example.org' and query(api.requests[0])['search_mode'] == 'equal'


async def test_records_returns_record_sets():
    api = fake().on('GET', BASE + '/v2.1/zones/zid-1/recordsets',
                    signed(lambda r, m: {'recordsets': [RRSET, dict(RRSET, id='rs-2', name='x.home.example.com.')]}))
    records = await provider(api).records('example.com', 'home.example.com', 'A')
    assert records == [Record('rs-1', 'home.example.com', 'A', '198.51.100.1', 300, {'line': 'Dianxin'})]
    q = query(api.requests[-1])
    assert (q['name'], q['type'], q['search_mode']) == ('home.example.com.', 'A', 'equal')


async def test_create_with_line():
    api = fake().on('POST', BASE + '/v2.1/zones/zid-1/recordsets',
                    signed(lambda r, m: (202, dict(FakeAPI.body(r), id='rs-9', status='PENDING_CREATE'))))
    record = await provider(api).create('example.com', 'example.com', 'AAAA', '2001:db8::1', 600, {'line': 'Liantong'})
    assert FakeAPI.body(api.requests[-1]) == {'name': 'example.com.', 'type': 'AAAA', 'records': ['2001:db8::1'], 'line': 'Liantong',
                                              'ttl': 600}
    assert record == Record('rs-9', 'example.com', 'AAAA', '2001:db8::1', 600, {'line': 'Liantong'})


async def test_update_only_sends_records_so_other_attributes_stay():
    api = fake().on('PUT', BASE + '/v2.1/zones/zid-1/recordsets/rs-1',
                    signed(lambda r, m: (202, dict(RRSET, records=['198.51.100.2'], status='PENDING_UPDATE'))))
    current = Record('rs-1', 'home.example.com', 'A', '198.51.100.1', 300, {'line': 'Dianxin'})
    updated = await provider(api).update('example.com', current, '198.51.100.2', None, {})
    assert FakeAPI.body(api.requests[-1]) == {'name': 'home.example.com.', 'type': 'A', 'records': ['198.51.100.2']}
    assert updated == Record('rs-1', 'home.example.com', 'A', '198.51.100.2', 300, {'line': 'Dianxin'})


async def test_delete():
    api = fake().on('DELETE', BASE + '/v2.1/zones/zid-1/recordsets/rs-1', signed(lambda r, m: (202, RRSET)))
    await provider(api).delete('example.com', Record('rs-1', 'home.example.com', 'A', '198.51.100.1'))
    assert api.requests[-1].method == 'DELETE' and api.requests[-1].content == b''


@pytest.mark.parametrize('status,body,expected', [
    (401, {'error_code': 'APIGW.0301', 'error_msg': 'Incorrect IAM authentication information'}, 'auth_failed'),
    (403, {'code': 'DNS.0030', 'message': 'Policy doesn\'t allow dns:recordset:list'}, 'forbidden'),
    (400, {'code': 'DNS.0308', 'message': 'Attribute records is invalid'}, 'bad_request'),
    (429, {'error_code': 'APIGW.0308', 'error_msg': 'The throttling threshold has been reached'}, 'rate_limited'),
    (500, {'code': 'DNS.0001', 'message': 'internal error'}, 'server_error'),
])
async def test_http_errors(status, body, expected):
    api = FakeAPI().on('GET', BASE + '/v2/zones', status=status, json=body)
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == expected and (body.get('code') or body.get('error_code')) in err.value.message


async def test_http_200_without_id_is_invalid_response():
    api = fake().on('POST', BASE + '/v2.1/zones/zid-1/recordsets', signed(lambda r, m: {'message': 'accepted'}))
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.1', None, {})
    assert err.value.code == 'invalid_response'
