import httpx
import pytest

from app.ddns.providers import REGISTRY, catalog
from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record
from tests.ddns.fakeapi import FakeAPI


class Demo(Provider):
    type = 'demo'
    name = 'Demo'
    fields = (Field('token', 'Token', 'password'), Field('team', 'Team', optional=True))
    caps = Caps(ttl_min=60, ttl_max=3600)
    endpoint = 'https://api.demo.test/v1/'


def test_missing_credentials_are_a_bad_request():
    with pytest.raises(ProviderError) as err:
        Demo({'token': ''})
    assert err.value.code == 'bad_request' and err.value.permanent


@pytest.mark.parametrize('zone,name,rel', [('example.com', 'example.com', '@'), ('example.com', 'a.b.example.com', 'a.b'),
                                           ('Example.COM.', 'WWW.example.com.', 'www')])
def test_relative_and_fqdn(zone, name, rel):
    assert Provider.relative(zone, name) == rel
    assert Provider.fqdn(zone, rel) == name.lower().rstrip('.')


def test_relative_outside_zone_is_refused():
    with pytest.raises(ProviderError):
        Provider.relative('example.com', 'example.net')


def test_ttl_is_clamped_and_falls_back_to_account_default():
    p = Demo({'token': 't'}, {'ttl': 30}, client=FakeAPI().client())
    assert p.ttl_for(None) == 60 and p.ttl_for(7200) == 3600 and p.ttl_for(600) == 600
    assert Demo({'token': 't'}, client=FakeAPI().client()).ttl_for(None) is None


@pytest.mark.parametrize('status,code,permanent', [(401, 'auth_failed', True), (403, 'forbidden', True), (400, 'bad_request', True),
                                                   (429, 'rate_limited', False), (503, 'server_error', False)])
async def test_http_errors_are_mapped(status, code, permanent):
    api = FakeAPI().on('GET', r'https://api\.demo\.test/v1/x', status=status, json={}, headers={'Retry-After': '7'})
    p = Demo({'token': 't'}, client=api.client())
    with pytest.raises(ProviderError) as err:
        await p.request('GET', '/x')
    assert err.value.code == code and err.value.permanent is permanent
    if code == 'rate_limited':
        assert err.value.retry_after == 7.0


async def test_an_error_body_is_not_repeated():
    page = '<html>mirror login: session=abc123 internal 10.0.0.8</html>'
    api = FakeAPI().on('GET', r'https://api\.demo\.test/v1/x', status=502, text=page)
    with pytest.raises(ProviderError) as err:
        await Demo({'token': 't'}, client=api.client()).request('GET', '/x')
    assert err.value.message == 'HTTP 502' and 'abc123' not in str(err.value)


@pytest.mark.parametrize('kind,default', [('aliyun', 'default'), ('tencent', '默认'), ('dnspod', '默认'), ('huawei', 'default_view')])
def test_only_records_on_the_line_chosen_count(kind, default):
    cls = REGISTRY[kind]
    other = next(v for v in (str(o).split(':')[0] for o in next(f for f in cls.options if f.key == 'line').options) if v != default)
    found = [Record('1', 'a.example.com', 'A', '192.0.2.1', 600, {'line': default}), Record('2', 'a.example.com', 'A', '192.0.2.2', 600, {'line': other}),
             Record('3', 'a.example.com', 'A', '192.0.2.3', 600, {})]
    p = cls.__new__(cls)
    assert [r.id for r in p.on_line(found, {})] == ['1', '3']
    assert [r.id for r in p.on_line(found, {'line': default})] == ['1', '3']
    assert [r.id for r in p.on_line(found, {'line': other})] == ['2']


async def test_transport_failure_is_a_network_error():
    def boom(request):
        raise httpx.ConnectError('refused')
    p = Demo({'token': 't'}, client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with pytest.raises(ProviderError) as err:
        await p.request('GET', '/x')
    assert err.value.code == 'network' and not err.value.permanent


async def test_endpoint_override_and_relative_urls():
    api = FakeAPI().on('GET', r'https://dns\.internal/api/zones', json={'ok': True})
    p = Demo({'token': 't'}, {'endpoint': 'https://dns.internal/api/'}, client=api.client())
    assert (await p.request('GET', '/zones')).json() == {'ok': True}


def test_catalog_lists_public_fields_only():
    for item in catalog():
        assert set(item) == {'type', 'name', 'tier', 'fields', 'options', 'caps', 'docs'}
        assert item['tier'] in {'A', 'B', 'C'}
