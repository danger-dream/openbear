import json

import pytest

from app.ddns.providers import _sign, get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

CREDS = {'secret_id': 'AKIDtest', 'secret_key': 'secret'}
URL = r'https://dnspod\.tencentcloudapi\.com/'


def fake(responses):
    """responses: X-TC-Action -> Response body (wrapped in {'Response': ...}) or (status, raw body)."""
    api = FakeAPI()

    def handler(request, match):
        h = request.headers
        action = h['X-TC-Action']
        expected = _sign.tc3('AKIDtest', 'secret', service='dnspod', host='dnspod.tencentcloudapi.com', action=action,
                             payload=request.content.decode(), timestamp=int(h['X-TC-Timestamp']))
        assert h['Authorization'] == expected['Authorization']
        assert h['X-TC-Version'] == '2021-03-23' and h['Content-Type'] == 'application/json; charset=utf-8'
        result = responses[action]
        if isinstance(result, tuple):
            return result
        return {'Response': dict(result, RequestId='req')}
    api.on('POST', URL, handler)
    return api


def provider(api):
    return get('tencent')(CREDS, client=api.client())


def sent(api, action):
    return [json.loads(r.content) for r in api.requests if r.headers['X-TC-Action'] == action]


def error(code, message='m'):
    return {'Error': {'Code': code, 'Message': message}}


ITEM = {'RecordId': 1001, 'Name': 'home', 'Type': 'A', 'Value': '198.51.100.1', 'TTL': 600, 'Line': '电信',
        'Status': 'DISABLE', 'Weight': 10, 'MX': 0}


async def test_check_lists_zones():
    api = fake({'DescribeDomainList': {'DomainList': [{'Name': 'example.com', 'Punycode': 'example.com'},
                                                      {'Name': '例子.中国', 'Punycode': 'xn--fsqu00a.xn--fiqs8s'}]}})
    assert await provider(api).check() == {'zones': ['example.com', 'xn--fsqu00a.xn--fiqs8s'], 'writable': None}
    assert sent(api, 'DescribeDomainList')[0] == {'Offset': 0, 'Limit': 3000}


async def test_zones_empty_account():
    api = fake({'DescribeDomainList': error('ResourceNotFound.NoDataOfDomain')})
    assert await provider(api).zones() == []


async def test_records_filter_and_keep_attributes():
    api = fake({'DescribeRecordList': {'RecordList': [ITEM, dict(ITEM, RecordId=1002, Name='home.lab')]}})
    records = await provider(api).records('example.com', 'home.example.com', 'A')
    assert records == [Record('1001', 'home.example.com', 'A', '198.51.100.1', 600, {'line': '电信', 'status': 'DISABLE', 'weight': 10})]
    body = sent(api, 'DescribeRecordList')[0]
    assert (body['Domain'], body['SubDomain'], body['RecordType'], body['ErrorOnEmpty']) == ('example.com', 'home', 'A', 'no')


async def test_create_defaults_line():
    api = fake({'CreateRecord': {'RecordId': 2001}})
    record = await provider(api).create('example.com', 'example.com', 'AAAA', '2001:db8::1', None, {})
    assert sent(api, 'CreateRecord')[0] == {'Domain': 'example.com', 'SubDomain': '@', 'RecordType': 'AAAA', 'RecordLine': '默认',
                                            'Value': '2001:db8::1'}
    assert record.id == '2001' and record.extra == {'line': '默认'}


async def test_create_with_line_and_ttl():
    api = fake({'CreateRecord': {'RecordId': 2001}})
    await provider(api).create('example.com', 'www.example.com', 'A', '203.0.113.1', 120, {'line': '联通'})
    body = sent(api, 'CreateRecord')[0]
    assert (body['RecordLine'], body['TTL']) == ('联通', 120)


async def test_update_sends_back_line_ttl_status_weight():
    api = fake({'ModifyRecord': {'RecordId': 1001}})
    current = Record('1001', 'home.example.com', 'A', '198.51.100.1', 600, {'line': '电信', 'status': 'DISABLE', 'weight': 10})
    updated = await provider(api).update('example.com', current, '198.51.100.2', None, {})
    assert sent(api, 'ModifyRecord')[0] == {'Domain': 'example.com', 'RecordId': 1001, 'SubDomain': 'home', 'RecordType': 'A',
                                            'RecordLine': '电信', 'Value': '198.51.100.2', 'TTL': 600, 'Status': 'DISABLE', 'Weight': 10}
    assert updated.value == '198.51.100.2' and updated.extra['line'] == '电信'


async def test_update_without_known_line_uses_default():
    api = fake({'ModifyRecord': {'RecordId': 1001}})
    await provider(api).update('example.com', Record('1001', 'home.example.com', 'A', '198.51.100.1'), '198.51.100.2', 60, {})
    body = sent(api, 'ModifyRecord')[0]
    assert (body['RecordLine'], body['TTL']) == ('默认', 60) and 'Status' not in body and 'Weight' not in body


async def test_delete():
    api = fake({'DeleteRecord': {}})
    await provider(api).delete('example.com', Record('1001', 'home.example.com', 'A', '198.51.100.1'))
    assert sent(api, 'DeleteRecord')[0] == {'Domain': 'example.com', 'RecordId': 1001}


@pytest.mark.parametrize('code,expected', [
    ('AuthFailure.SignatureFailure', 'auth_failed'),
    ('AuthFailure.SecretIdNotFound', 'auth_failed'),
    ('UnauthorizedOperation', 'forbidden'),
    ('OperationDenied.AccessDenied', 'forbidden'),
    ('InvalidParameterValue.DomainNotExists', 'zone_not_found'),
    ('InvalidParameter.DomainRecordExist', 'record_conflict'),
    ('RequestLimitExceeded', 'rate_limited'),
    ('InternalError', 'server_error'),
    ('InvalidParameter.RecordLineInvalid', 'bad_request'),
])
async def test_business_errors_in_http_200(code, expected):
    api = fake({'CreateRecord': error(code)})
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.1', None, {})
    assert err.value.code == expected and code in err.value.message


async def test_non_api_body_is_invalid_response():
    api = fake({'CreateRecord': (200, {'unexpected': True})})
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.1', None, {})
    assert err.value.code == 'invalid_response'
