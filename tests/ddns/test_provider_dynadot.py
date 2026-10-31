import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

API = r'https://api\.dynadot\.com/api3\.json'


def provider(api):
    return get('dynadot')({'key': 'k'}, client=api.client())


def dns(*sub, main=({'RecordType': 'MX', 'Value': 'mx.example.net', 'Value2': '10'},)):
    return {'GetDnsResponse': {'ResponseCode': 0, 'Status': 'success', 'GetDns': {'NameServerSettings': {
        'Type': 'Dynadot DNS', 'MainDomains': list(main), 'SubDomains': list(sub), 'TTL': '3600'}}}}


def router(get_dns, seen):
    def handler(req, m):
        params = dict(req.url.params)
        seen.append(params)
        if params['command'] == 'get_dns':
            return get_dns
        return {'SetDnsResponse': {'ResponseCode': 0, 'Status': 'success'}}
    return FakeAPI().on('GET', API, handler)


HOME = {'Subhost': 'home', 'RecordType': 'A', 'Value': '198.51.100.1'}


def test_metadata():
    cls = get('dynadot')
    assert cls.tier == 'A' and not cls.caps.by_id


async def test_zones():
    api = FakeAPI().on('GET', API, json={'ListDomainInfoResponse': {'ResponseCode': 0, 'Status': 'success',
                                                                    'MainDomains': [{'Name': 'b.com'}, {'Name': 'a.com'}]}})
    assert (await provider(api).check())['zones'] == ['a.com', 'b.com']
    assert dict(api.calls()[0].url.params) == {'key': 'k', 'command': 'list_domain'}


async def test_records_match_and_miss():
    seen = []
    api = router(dns(HOME, {'Subhost': 'home', 'RecordType': 'AAAA', 'Value': '2001:db8::1'}), seen)
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.value, r.ttl) for r in found] == [('198.51.100.1', 3600)]
    assert seen[0] == {'key': 'k', 'command': 'get_dns', 'domain': 'example.com'}
    assert await provider(api).records('example.com', 'example.com', 'A') == []


async def test_create_appends():
    seen = []
    await provider(router(dns(), seen)).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert seen[0] == {'key': 'k', 'command': 'set_dns2', 'domain': 'example.com', 'add_dns_to_current_setting': '1',
                       'subdomain0': 'home', 'sub_record_type0': 'a', 'sub_record0': '203.0.113.5'}


async def test_update_rewrites_every_record_unchanged_but_one():
    seen = []
    api = router(dns(HOME, {'Subhost': 'www', 'RecordType': 'CNAME', 'Value': 'example.com'}), seen)
    await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.1', 3600), '203.0.113.9', None, {})
    assert seen[1] == {'key': 'k', 'command': 'set_dns2', 'domain': 'example.com', 'ttl': '3600',
                       'main_record_type0': 'mx', 'main_record0': 'mx.example.net', 'main_recordx0': '10',
                       'subdomain0': 'home', 'sub_record_type0': 'a', 'sub_record0': '203.0.113.9',
                       'subdomain1': 'www', 'sub_record_type1': 'cname', 'sub_record1': 'example.com'}


async def test_update_refuses_records_it_cannot_round_trip():
    seen = []
    api = router(dns(HOME, {'Subhost': 'go', 'RecordType': 'Forward', 'Value': 'https://x', 'Value2': '301'}), seen)
    with pytest.raises(ProviderError) as err:
        await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'), '203.0.113.9', None, {})
    assert err.value.code == 'unsupported' and len(seen) == 1


async def test_delete():
    seen = []
    await provider(router(dns(HOME), seen)).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert 'sub_record0' not in seen[1] and seen[1]['main_record0'] == 'mx.example.net'


@pytest.mark.parametrize('error,expected', [('invalid key', 'auth_failed'), ('domain not found', 'zone_not_found'), ('bad record', 'bad_request')])
async def test_errors_in_http_200(error, expected):
    api = FakeAPI().on('GET', API, json={'GetDnsResponse': {'ResponseCode': -1, 'Status': 'error', 'Error': error}})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == expected
