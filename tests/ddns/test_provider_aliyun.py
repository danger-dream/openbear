from urllib.parse import parse_qsl

import pytest

from app.ddns.providers import _sign, get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

CREDS = {'access_key_id': 'testid', 'access_key_secret': 'testsecret'}
URL = r'https://alidns\.aliyuncs\.com/'


def params(request):
    return dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))


def fake(responses):
    """responses: Action -> JSON body or (status, body)."""
    api = FakeAPI()

    def handler(request, match):
        p = params(request)
        signature = p.pop('Signature')
        assert signature == _sign.aliyun_rpc('GET', p, CREDS['access_key_secret'])
        assert p['AccessKeyId'] == 'testid' and p['Version'] == '2015-01-09' and p['Format'] == 'JSON'
        assert p['SignatureMethod'] == 'HMAC-SHA1' and p['SignatureVersion'] == '1.0' and p['SignatureNonce']
        return responses[p['Action']]
    api.on('GET', URL, handler)
    return api


def provider(api):
    return get('aliyun')(CREDS, client=api.client())


def calls(api, action):
    return [params(r) for r in api.requests if params(r)['Action'] == action]


RECORD = {'RecordId': '99001', 'RR': 'home', 'Type': 'A', 'Value': '198.51.100.1', 'TTL': 600, 'Line': 'telecom',
          'Status': 'Enable', 'DomainName': 'example.com'}


async def test_check_lists_zones_by_punycode():
    api = fake({'DescribeDomains': {'TotalCount': 2, 'Domains': {'Domain': [
        {'DomainName': 'example.com'}, {'DomainName': '例子.中国', 'PunyCode': 'xn--fsqu00a.xn--fiqs8s'}]}}})
    result = await provider(api).check()
    assert result == {'zones': ['example.com', 'xn--fsqu00a.xn--fiqs8s'], 'writable': None}
    assert calls(api, 'DescribeDomains')[0]['PageSize'] == '100'


async def test_records_match_name_and_type_only():
    api = fake({'DescribeSubDomainRecords': {'TotalCount': 3, 'DomainRecords': {'Record': [
        RECORD, dict(RECORD, RecordId='99002', RR='home.lab'), dict(RECORD, RecordId='99003', Type='AAAA')]}}})
    records = await provider(api).records('example.com', 'home.example.com', 'A')
    assert records == [Record('99001', 'home.example.com', 'A', '198.51.100.1', 600, {'line': 'telecom'})]
    sent = calls(api, 'DescribeSubDomainRecords')[0]
    assert (sent['DomainName'], sent['SubDomain'], sent['Type']) == ('example.com', 'home.example.com', 'A')


async def test_records_none():
    api = fake({'DescribeSubDomainRecords': {'TotalCount': 0, 'DomainRecords': {'Record': []}}})
    assert await provider(api).records('example.com', 'example.com', 'AAAA') == []


async def test_create_with_line_and_ttl():
    api = fake({'AddDomainRecord': {'RecordId': '99009', 'RequestId': 'r'}})
    record = await provider(api).create('example.com', 'example.com', 'A', '203.0.113.9', 600, {'line': 'unicom'})
    sent = calls(api, 'AddDomainRecord')[0]
    assert (sent['RR'], sent['Type'], sent['Value'], sent['TTL'], sent['Line']) == ('@', 'A', '203.0.113.9', '600', 'unicom')
    assert record.id == '99009' and record.extra == {'line': 'unicom'}


async def test_create_without_options_leaves_defaults_to_aliyun():
    api = fake({'AddDomainRecord': {'RecordId': '99009'}})
    await provider(api).create('example.com', 'www.example.com', 'AAAA', '2001:db8::9', None, {})
    sent = calls(api, 'AddDomainRecord')[0]
    assert 'TTL' not in sent and 'Line' not in sent and sent['RR'] == 'www'


async def test_update_keeps_line_and_ttl():
    api = fake({'UpdateDomainRecord': {'RecordId': '99001'}})
    current = Record('99001', 'home.example.com', 'A', '198.51.100.1', 120, {'line': 'telecom'})
    updated = await provider(api).update('example.com', current, '198.51.100.2', None, {})
    sent = calls(api, 'UpdateDomainRecord')[0]
    assert (sent['RecordId'], sent['RR'], sent['Value'], sent['TTL'], sent['Line']) == ('99001', 'home', '198.51.100.2', '120', 'telecom')
    assert updated.value == '198.51.100.2' and updated.extra['line'] == 'telecom'


async def test_update_changes_given_keys():
    api = fake({'UpdateDomainRecord': {'RecordId': '99001'}})
    current = Record('99001', 'home.example.com', 'A', '198.51.100.1', 120, {'line': 'telecom'})
    await provider(api).update('example.com', current, '198.51.100.2', 3600, {'line': 'mobile'})
    sent = calls(api, 'UpdateDomainRecord')[0]
    assert (sent['TTL'], sent['Line']) == ('3600', 'mobile')


async def test_delete():
    api = fake({'DeleteDomainRecord': {'RecordId': '99001'}})
    await provider(api).delete('example.com', Record('99001', 'home.example.com', 'A', '198.51.100.1'))
    assert calls(api, 'DeleteDomainRecord')[0]['RecordId'] == '99001'


@pytest.mark.parametrize('status,code,expected', [
    (404, 'InvalidAccessKeyId.NotFound', 'auth_failed'),
    (400, 'SignatureDoesNotMatch', 'auth_failed'),
    (403, 'Forbidden.RAM', 'forbidden'),
    (400, 'InvalidDomainName.NoExist', 'zone_not_found'),
    (400, 'DomainRecordDuplicate', 'record_conflict'),
    (400, 'Throttling.User', 'rate_limited'),
    (400, 'InvalidRR.Format', 'bad_request'),
    (503, 'ServiceUnavailable', 'server_error'),
])
async def test_error_codes(status, code, expected):
    api = fake({'DescribeDomains': (status, {'Code': code, 'Message': 'm', 'RequestId': 'r'})})
    with pytest.raises(ProviderError) as err:
        await provider(api).zones()
    assert err.value.code == expected and code in err.value.message


async def test_business_failure_in_http_200():
    api = fake({'AddDomainRecord': {'Code': 'LimitExceeded', 'Message': 'too many records'}})
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.1', None, {})
    assert err.value.code == 'bad_request' and 'LimitExceeded' in err.value.message


async def test_http_200_without_record_id_is_invalid():
    api = fake({'AddDomainRecord': {'RequestId': 'r'}})
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.1', None, {})
    assert err.value.code == 'invalid_response'
