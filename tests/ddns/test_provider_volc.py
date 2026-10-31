import json
from urllib.parse import parse_qsl

import pytest

from app.ddns.providers import _sign, get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

CREDS = {'access_key_id': 'AKLTtest', 'secret_access_key': 'volcsecret'}
URL = r'https://dns\.volcengineapi\.com/'
ZONES = {'Zones': [{'ZID': 58846, 'ZoneName': 'example.com'}, {'ZID': 58847, 'ZoneName': 'example.net'}], 'Total': 2}
ITEM = {'RecordID': '9001', 'Host': 'home', 'Type': 'A', 'Value': '198.51.100.1', 'TTL': 600, 'Line': 'telecom', 'Weight': 1}


def fake(responses):
    """responses: Action -> Result, or (status, full body)."""
    api = FakeAPI()

    def handler(request, match):
        h = request.headers
        query = dict(parse_qsl(request.url.query.decode()))
        assert query['Version'] == '2018-08-01' and h['X-Content-Sha256'] == _sign.sha256_hex(request.content)
        expected = _sign.volc('AKLTtest', 'volcsecret', region='cn-beijing', service='dns', method='POST', path='/', query=query,
                              headers={k: h[k] for k in ('Content-Type', 'Host', 'X-Date', 'X-Content-Sha256')},
                              body=request.content, stamp=h['X-Date'])
        assert h['Authorization'] == expected
        assert '/cn-beijing/dns/request, SignedHeaders=content-type;host;x-content-sha256;x-date, ' in expected
        result = responses[query['Action']]
        if isinstance(result, tuple):
            return result
        return {'ResponseMetadata': {'RequestId': 'r', 'Action': query['Action']}, 'Result': result}
    api.on('POST', URL, handler)
    return api


def provider(api):
    return get('volc')(CREDS, client=api.client())


def sent(api, action):
    return [json.loads(r.content) for r in api.requests if f'Action={action}&' in r.url.query.decode()]


def error(status, code):
    return status, {'ResponseMetadata': {'RequestId': 'r', 'Error': {'Code': code, 'Message': 'm'}}}


async def test_check_lists_zones():
    api = fake({'ListZones': ZONES})
    assert await provider(api).check() == {'zones': ['example.com', 'example.net'], 'writable': None}
    assert sent(api, 'ListZones')[0] == {'PageNumber': 1, 'PageSize': 100}


async def test_records_exact_search():
    api = fake({'ListZones': {'Zones': [ZONES['Zones'][0]]},
                'ListRecords': {'Records': [ITEM, dict(ITEM, RecordID='9002', Host='home.lab')]}})
    records = await provider(api).records('example.com', 'home.example.com', 'A')
    assert records == [Record('9001', 'home.example.com', 'A', '198.51.100.1', 600, {'line': 'telecom'})]
    assert sent(api, 'ListZones')[0]['Key'] == 'example.com'
    body = sent(api, 'ListRecords')[0]
    assert (body['ZID'], body['Host'], body['Type'], body['SearchMode']) == (58846, 'home', 'A', 'exact')


async def test_unknown_zone():
    api = fake({'ListZones': {'Zones': []}})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.org', 'example.org', 'A')
    assert err.value.code == 'zone_not_found'


async def test_create():
    api = fake({'ListZones': ZONES, 'CreateRecord': {'RecordID': '9010', 'TTL': 600, 'Line': 'default'}})
    p = provider(api)
    await p.zones()
    record = await p.create('example.com', 'example.com', 'AAAA', '2001:db8::1', 600, {})
    assert sent(api, 'CreateRecord')[0] == {'ZID': 58846, 'Host': '@', 'Type': 'AAAA', 'Value': '2001:db8::1', 'Line': 'default', 'TTL': 600}
    assert record == Record('9010', 'example.com', 'AAAA', '2001:db8::1', 600, {'line': 'default'})
    await p.create('example.com', 'example.com', 'AAAA', '2001:db8::1', 600, {'line': 'oversea'})
    assert sent(api, 'CreateRecord')[1]['Line'] == 'oversea'


async def test_records_of_other_lines_are_not_the_default_lines():
    api = fake({'ListZones': {'Zones': [ZONES['Zones'][0]]},
                'ListRecords': {'Records': [ITEM, dict(ITEM, RecordID='9003', Line='default')]}})
    p = provider(api)
    found = await p.records('example.com', 'home.example.com', 'A')
    assert [r.id for r in p.on_line(found, {})] == ['9003'] and [r.id for r in p.on_line(found, {'line': 'telecom'})] == ['9001']


async def test_update_keeps_line_and_leaves_ttl_to_server():
    api = fake({'UpdateRecord': {'RecordID': '9001', 'TTL': 600, 'Line': 'telecom'}})
    current = Record('9001', 'home.example.com', 'A', '198.51.100.1', 600, {'line': 'telecom'})
    updated = await provider(api).update('example.com', current, '198.51.100.2', None, {})
    assert sent(api, 'UpdateRecord')[0] == {'RecordID': '9001', 'Host': 'home', 'Line': 'telecom', 'Type': 'A', 'Value': '198.51.100.2'}
    assert updated == Record('9001', 'home.example.com', 'A', '198.51.100.2', 600, {'line': 'telecom'})


async def test_delete():
    api = fake({'DeleteRecord': {}})
    await provider(api).delete('example.com', Record('9001', 'home.example.com', 'A', '198.51.100.1'))
    assert sent(api, 'DeleteRecord')[0] == {'RecordID': '9001'}


@pytest.mark.parametrize('status,code,expected', [
    (401, 'InvalidAccessKey', 'auth_failed'),
    (401, 'SignatureDoesNotMatch', 'auth_failed'),
    (403, 'AccessDenied', 'forbidden'),
    (429, 'FlowLimitExceeded', 'rate_limited'),
    (500, 'InternalError', 'server_error'),
    (400, 'InvalidParameter', 'bad_request'),
    (200, 'ErrZoneNotFound', 'zone_not_found'),
    (200, 'ErrRecordExists', 'bad_request'),
])
async def test_errors_including_http_200(status, code, expected):
    api = fake({'ListZones': error(status, code)})
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == expected and code in err.value.message


async def test_list_without_result_is_invalid():
    api = fake({'ListZones': (200, {'ResponseMetadata': {'RequestId': 'r'}})})
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == 'invalid_response'
