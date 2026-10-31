import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

UPDATE = r'https://www\.duckdns\.org/update'
TOKEN = 'a7c4d0ad-114e-40ef-ba1d-d217904a50f2'


def provider(api, token=TOKEN):
    return get('duckdns')({'token': token}, client=api.client())


def text(body):
    return FakeAPI().on('GET', UPDATE, text=body)


def test_metadata():
    caps = get('duckdns').caps
    assert (caps.read, caps.zones, caps.by_id, caps.multi, caps.delete) == (False, False, False, False, False)
    assert (caps.ttl_min, caps.ttl_max) == (60, 60)


async def test_check_validates_token_without_calls():
    api = text('OK')
    assert await provider(api).check() == {'zones': ['duckdns.org'], 'writable': None}
    with pytest.raises(ProviderError) as err:
        await provider(api, 'not-a-token').check()
    assert err.value.code == 'auth_failed' and api.calls() == []


async def test_records_unsupported():
    with pytest.raises(ProviderError) as err:
        await provider(text('OK')).records('duckdns.org', 'home.duckdns.org', 'A')
    assert err.value.code == 'unsupported'


async def test_create_ipv4():
    api = text('OK\n203.0.113.5\n\nUPDATED')
    assert await provider(api).create('duckdns.org', 'home.duckdns.org', 'A', '203.0.113.5', None, {}) == ''
    assert dict(api.calls()[0].url.params) == {'domains': 'home', 'token': TOKEN, 'ip': '203.0.113.5', 'verbose': 'true'}


async def test_update_ipv6_uses_ip_parameter_only():
    api = text('OK\n\n2001:db8::9\nUPDATED')
    await provider(api).update('duckdns.org', Record('', 'home.duckdns.org', 'AAAA', '2001:db8::1'), '2001:db8::9', None, {})
    assert dict(api.calls()[0].url.params) == {'domains': 'home', 'token': TOKEN, 'ip': '2001:db8::9', 'verbose': 'true'}


@pytest.mark.parametrize('zone,name', [('example.com', 'home.example.com'), ('duckdns.org', 'a.home.duckdns.org'),
                                       ('duckdns.org', 'duckdns.org')])
async def test_rejects_names_outside_one_label(zone, name):
    api = text('OK')
    with pytest.raises(ProviderError) as err:
        await provider(api).create(zone, name, 'A', '203.0.113.5', None, {})
    assert err.value.code == 'bad_request' and api.calls() == []


async def test_ko_is_auth_failure():
    with pytest.raises(ProviderError) as err:
        await provider(text('KO')).update('duckdns.org', Record('', 'home.duckdns.org', 'A', ''), '203.0.113.5', None, {})
    assert err.value.code == 'auth_failed'


async def test_unexpected_body_and_http_errors():
    with pytest.raises(ProviderError) as err:
        await provider(text('<html>')).create('duckdns.org', 'home.duckdns.org', 'A', '203.0.113.5', None, {})
    assert err.value.code == 'invalid_response'
    api = FakeAPI().on('GET', UPDATE, status=503, text='down')
    with pytest.raises(ProviderError) as err:
        await provider(api).create('duckdns.org', 'home.duckdns.org', 'A', '203.0.113.5', None, {})
    assert err.value.code == 'server_error'


async def test_delete_unsupported():
    with pytest.raises(ProviderError) as err:
        await provider(text('OK')).delete('duckdns.org', Record('', 'home.duckdns.org', 'A', '203.0.113.5'))
    assert err.value.code == 'unsupported'
