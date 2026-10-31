import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.godaddy\.com/v1/domains'
PATH = BASE + '/example.com/records/A/home'


def provider(api):
    return get('godaddy')({'key': 'k', 'secret': 's'}, client=api.client())


def test_metadata():
    cls = get('godaddy')
    assert cls.tier == 'A' and not cls.caps.by_id and '403' in cls.fields[1].hint


async def test_zones_page_with_marker_and_send_sso_key():
    def handler(req, m):
        marker = req.url.params.get('marker')
        return [{'domain': f'd{i:04}.com'} for i in range(1000)] if not marker else [{'domain': 'zz.com'}]
    api = FakeAPI().on('GET', BASE, handler)
    zones = (await provider(api).check())['zones']
    assert len(zones) == 1001 and zones[-1] == 'zz.com'
    first, second = api.calls()
    assert first.headers['Authorization'] == 'sso-key k:s' and first.url.params['statuses'] == 'ACTIVE'
    assert 'marker' not in first.url.params and second.url.params['marker'] == 'd0999.com'


async def test_records_match_and_miss():
    api = (FakeAPI().on('GET', PATH, json=[{'data': '198.51.100.1', 'name': 'home', 'ttl': 600, 'type': 'A'}])
           .on('GET', BASE + '/example.com/records/A/@', json=[]))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.name, r.value, r.ttl) for r in found] == [('', 'home.example.com', '198.51.100.1', 600)]
    assert await provider(api).records('example.com', 'example.com', 'A') == []


async def test_create_adds_with_patch():
    api = FakeAPI().on('PATCH', BASE + '/example.com/records', status=200, text='')
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 60, {}) == ''
    assert FakeAPI.body(api.calls()[0]) == [{'type': 'A', 'name': 'home', 'data': '203.0.113.5', 'ttl': 600}]


async def test_update_replaces_one_value_and_keeps_siblings():
    current = [{'data': '198.51.100.1', 'name': 'home', 'ttl': 3600, 'type': 'A'},
               {'data': '198.51.100.2', 'name': 'home', 'ttl': 1800, 'type': 'A'}]
    api = FakeAPI().on('GET', PATH, json=current).on('PUT', PATH, status=200, text='')
    await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.2', 1800), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls('PUT')[0]) == [{'data': '198.51.100.1', 'ttl': 3600}, {'data': '203.0.113.9', 'ttl': 1800}]
    await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.1', 3600), '203.0.113.9', 900, {})
    assert FakeAPI.body(api.calls('PUT')[1])[0] == {'data': '203.0.113.9', 'ttl': 900}


async def test_delete_keeps_siblings_or_removes_the_set():
    two = [{'data': '198.51.100.1', 'ttl': 600}, {'data': '198.51.100.2', 'ttl': 600}]
    api = FakeAPI().on('GET', PATH, json=two).on('PUT', PATH, status=200, text='')
    await provider(api).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert FakeAPI.body(api.calls('PUT')[0]) == [{'data': '198.51.100.2', 'ttl': 600}]
    one = FakeAPI().on('GET', PATH, json=two[:1]).on('DELETE', PATH, status=204)
    await provider(one).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert len(one.calls('DELETE')) == 1


@pytest.mark.parametrize('status,code,expected', [(401, 'UNABLE_TO_AUTHENTICATE', 'auth_failed'), (403, 'ACCESS_DENIED', 'forbidden'),
                                                  (404, 'UNKNOWN_DOMAIN', 'zone_not_found'), (422, 'INVALID_BODY', 'bad_request'),
                                                  (429, 'TOO_MANY_REQUESTS', 'rate_limited')])
async def test_error_mapping(status, code, expected):
    api = FakeAPI().on('GET', PATH, status=status, json={'code': code, 'message': 'm'})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == expected
