import json

import httpx
import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

HOOK = r'https://hook\.example\.net/update'


def provider(api, **creds):
    return get('callback')({'url': 'https://hook.example.net/update?host={domain}&ip={ip}', **creds}, client=api.client())


def test_caps():
    caps = get('callback').caps
    assert (caps.read, caps.zones, caps.by_id, caps.delete, caps.multi) == (False, False, False, False, False)


async def test_get_url_variables_are_url_encoded():
    api = FakeAPI().on('GET', HOOK, text='ok')
    p = provider(api, url='https://hook.example.net/update?host={domain}&ip={ip}&t={type}&ttl={ttl}&z={zone}')
    record = await p.create('example.com', 'home.example.com', 'AAAA', '2001:db8::1', 300, {})
    assert api.requests[0].url.query.decode() == 'host=home.example.com&ip=2001%3Adb8%3A%3A1&t=AAAA&ttl=300&z=example.com'
    assert api.requests[0].content == b''
    assert record == Record('', 'home.example.com', 'AAAA', '2001:db8::1', 300)


async def test_unset_ttl_renders_empty():
    api = FakeAPI().on('GET', HOOK, text='ok')
    await provider(api, url='https://hook.example.net/update?ttl={ttl}&x={unknown}').create('example.com', 'example.com', 'A',
                                                                                              '198.51.100.1', None, {})
    assert api.requests[0].url.query.decode() == 'ttl=&x={unknown}'


async def test_post_json_body_is_json_escaped_and_headers_sent():
    api = FakeAPI().on('POST', HOOK, status=201, json={})
    p = provider(api, method='post', url='https://hook.example.net/update',
                 headers='Authorization: Bearer abc:def\n\nX-Note:  "quoted"  ',
                 body='{"name": "{domain}", "content": "{ip}", "ttl": {ttl}, "note": "{zone}"}')
    await p.create('we"ird\\zone', 'home.we"ird\\zone', 'A', '198.51.100.1', 120, {})
    request = api.requests[0]
    assert request.method == 'POST'
    assert request.headers['Authorization'] == 'Bearer abc:def' and request.headers['X-Note'] == '"quoted"'
    assert request.headers['Content-Type'] == 'application/json'
    assert json.loads(request.content) == {'name': 'home.we"ird\\zone', 'content': '198.51.100.1', 'ttl': 120, 'note': 'we"ird\\zone'}


async def test_non_json_body_is_sent_verbatim_with_given_content_type():
    api = FakeAPI().on('PUT', HOOK, status=204)
    p = provider(api, method='PUT', url='https://hook.example.net/update', headers='Content-Type: application/x-www-form-urlencoded',
                 body='hostname={domain}&myip={ip}')
    await p.create('example.com', 'home.example.com', 'A', '198.51.100.1', None, {})
    assert api.requests[0].content == b'hostname=home.example.com&myip=198.51.100.1'
    assert api.requests[0].headers['Content-Type'] == 'application/x-www-form-urlencoded'


async def test_update_sends_the_same_request():
    api = FakeAPI().on('GET', HOOK, text='good')
    updated = await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.1', 60), '198.51.100.2', None, {})
    assert api.requests[0].url.query.decode() == 'host=home.example.com&ip=198.51.100.2'
    assert updated.value == '198.51.100.2'


async def test_zones_check_records_delete():
    api = FakeAPI()
    p = provider(api, zones='example.com, example.net')
    assert p.caps.zones is True and await p.zones() == ['example.com', 'example.net']
    assert await p.check() == {'zones': ['example.com', 'example.net'], 'writable': None}
    for call in (provider(api).zones(), p.records('example.com', 'example.com', 'A'),
                 p.delete('example.com', Record('', 'example.com', 'A', '1.1.1.1'))):
        with pytest.raises(ProviderError) as err:
            await call
        assert err.value.code == 'unsupported'
    assert api.requests == []


@pytest.mark.parametrize('status,expected', [(401, 'auth_failed'), (403, 'forbidden'), (404, 'bad_request'), (302, 'bad_request'),
                                             (429, 'rate_limited'), (502, 'server_error')])
async def test_non_2xx_fails(status, expected):
    api = FakeAPI().on('GET', HOOK, lambda r, m: httpx.Response(status, text='nope'))
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.1', None, {})
    assert err.value.code == expected


@pytest.mark.parametrize('creds,message', [({'method': 'DELETE'}, 'method'), ({'url': 'ftp://x/{ip}'}, 'http'),
                                           ({'headers': 'no colon here'}, 'header')])
def test_bad_configuration(creds, message):
    with pytest.raises(ProviderError) as err:
        get('callback')({'url': 'https://hook.example.net/{ip}', **creds})
    assert err.value.code == 'bad_request' and message in err.value.message
