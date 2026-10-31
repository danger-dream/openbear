import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

UPDATE = r'https://dynamicdns\.park-your-domain\.com/update'


def provider(api, domain='Example.com'):
    return get('namecheap')({'domain': domain, 'password': 'ddnspw'}, client=api.client())


def reply(*errors, ip='203.0.113.5'):
    items = ''.join(f'<Err{i}>{e}</Err{i}>' for i, e in enumerate(errors, 1))
    return ('<?xml version="1.0"?><interface-response><Command>SETDNSHOST</Command><Language>eng</Language>'
            f'<IP>{ip}</IP><ErrCount>{len(errors)}</ErrCount><errors>{items}</errors><ResponseCount>{len(errors)}</ResponseCount>'
            '<Done>true</Done><debug><![CDATA[]]></debug></interface-response>')


def api_with(*errors):
    return FakeAPI().on('GET', UPDATE, text=reply(*errors))


def test_metadata():
    caps = get('namecheap').caps
    assert (caps.read, caps.zones, caps.by_id, caps.delete, caps.types) == (False, False, False, False, ('A',))


async def test_check_reports_configured_domain_without_calls():
    api = api_with()
    assert await provider(api).check() == {'zones': ['example.com'], 'writable': None}
    assert api.calls() == []


async def test_records_unsupported():
    with pytest.raises(ProviderError) as err:
        await provider(api_with()).records('example.com', 'home.example.com', 'A')
    assert err.value.code == 'unsupported'


async def test_create_sends_domain_case_as_entered():
    api = api_with()
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 300, {}) == ''
    assert dict(api.calls()[0].url.params) == {'host': 'home', 'domain': 'Example.com', 'password': 'ddnspw', 'ip': '203.0.113.5'}


async def test_update_apex():
    api = api_with()
    await provider(api).update('example.com', Record('', 'example.com', 'A', '198.51.100.1'), '203.0.113.9', None, {})
    assert api.calls()[0].url.params['host'] == '@' and api.calls()[0].url.params['ip'] == '203.0.113.9'


async def test_rejects_aaaa_and_other_domains():
    api = api_with()
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'AAAA', '2001:db8::1', None, {})
    assert err.value.code == 'unsupported'
    with pytest.raises(ProviderError) as err:
        await provider(api).create('other.com', 'home.other.com', 'A', '203.0.113.5', None, {})
    assert err.value.code == 'bad_request' and api.calls() == []


@pytest.mark.parametrize('message,expected', [
    ('Passwords do not match', 'auth_failed'),
    ('Domain name not found', 'zone_not_found'),
    ('No Records updated. A record not Found;', 'bad_request'),
])
async def test_errors_in_http_200(message, expected):
    with pytest.raises(ProviderError) as err:
        await provider(api_with(message)).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert err.value.code == expected and err.value.message == message


async def test_non_xml_answer():
    api = FakeAPI().on('GET', UPDATE, text='<html>maintenance')
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert err.value.code == 'invalid_response'


async def test_delete_unsupported():
    with pytest.raises(ProviderError) as err:
        await provider(api_with()).delete('example.com', Record('', 'home.example.com', 'A', '203.0.113.5'))
    assert err.value.code == 'unsupported'
