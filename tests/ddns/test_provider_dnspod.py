from urllib.parse import parse_qs

import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://dnsapi\.cn'
OK = {'code': '1', 'message': 'Action completed successful'}


def provider(api):
    return get('dnspod')({'id': '12345', 'token': 'abcdef'}, client=api.client())


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def item(rid='16894439', name='home', line='电信', ttl='600', weight=None, enabled='1'):
    return {'id': rid, 'name': name, 'line': line, 'line_id': '10=0', 'type': 'A', 'ttl': ttl, 'value': '198.51.100.1',
            'weight': weight, 'mx': '0', 'enabled': enabled, 'remark': 'kept'}


def test_metadata():
    cls = get('dnspod')
    assert cls.tier == 'A' and [o.key for o in cls.options] == ['line'] and '默认:默认' in cls.options[0].options


async def test_zones_page_by_offset_and_send_login_token():
    def handler(req, m):
        offset = int(form(req)['offset'])
        domains = [{'id': 1, 'name': '我们.cn', 'punycode': 'xn--vnqp08b.cn'}, {'id': 2, 'name': 'b.com', 'punycode': 'b.com'}] if offset == 0 else \
            [{'id': 3, 'name': 'a.com', 'punycode': 'a.com'}]
        return {'status': OK, 'info': {'domain_total': 3}, 'domains': domains}
    api = FakeAPI().on('POST', BASE + '/Domain.List', handler)
    assert (await provider(api).check())['zones'] == ['a.com', 'b.com', 'xn--vnqp08b.cn']
    first = form(api.calls()[0])
    assert first['login_token'] == '12345,abcdef' and first['format'] == 'json' and first['type'] == 'all'
    assert [form(r)['offset'] for r in api.calls()] == ['0', '2']


async def test_zones_empty_account():
    api = FakeAPI().on('POST', BASE + '/Domain.List', json={'status': {'code': '9', 'message': '没有任何域名'}})
    assert await provider(api).zones() == []


async def test_records_match_and_miss():
    api = FakeAPI().on('POST', BASE + '/Record.List', json={'status': OK, 'info': {'record_total': '2'},
                                                            'records': [item(), item('2', 'home2')]})
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.name, r.value, r.ttl, r.extra) for r in found] == [
        ('16894439', 'home.example.com', '198.51.100.1', 600, {'line': '电信', 'status': 'enable'})]
    sent = form(api.calls()[0])
    assert sent['domain'] == 'example.com' and sent['sub_domain'] == 'home' and sent['record_type'] == 'A'
    empty = FakeAPI().on('POST', BASE + '/Record.List', json={'status': {'code': '10', 'message': '记录列表为空'}})
    assert await provider(empty).records('example.com', 'example.com', 'A') == []
    assert form(empty.calls()[0])['sub_domain'] == '@'


async def test_create_defaults_line():
    api = FakeAPI().on('POST', BASE + '/Record.Create', json={'status': OK, 'record': {'id': '99', 'name': 'home', 'status': 'enable'}})
    assert await provider(api).create('example.com', 'home.example.com', 'AAAA', '2001:db8::1', 120, {}) == '99'
    sent = form(api.calls()[0])
    assert {k: sent[k] for k in ('domain', 'sub_domain', 'record_type', 'value', 'record_line', 'ttl')} == {
        'domain': 'example.com', 'sub_domain': 'home', 'record_type': 'AAAA', 'value': '2001:db8::1', 'record_line': '默认', 'ttl': '600'}
    assert 'remark' not in sent


async def test_update_keeps_line_ttl_weight_and_status():
    api = FakeAPI().on('POST', BASE + '/Record.Modify', json={'status': OK, 'record': {'id': 1}})
    old = Record('16894439', 'home.example.com', 'A', '198.51.100.1', 900, {'line': '电信', 'status': 'disable', 'weight': 5})
    await provider(api).update('example.com', old, '203.0.113.9', None, {})
    sent = form(api.calls()[0])
    assert {k: v for k, v in sent.items() if k not in ('login_token', 'format', 'lang', 'error_on_empty')} == {
        'domain': 'example.com', 'record_id': '16894439', 'sub_domain': 'home', 'record_type': 'A', 'value': '203.0.113.9',
        'record_line': '电信', 'status': 'disable', 'ttl': '900', 'weight': '5'}
    await provider(api).update('example.com', Record('1', 'home.example.com', 'A', 'x'), '203.0.113.9', 3600, {'line': '联通'})
    sent = form(api.calls()[1])
    assert sent['record_line'] == '联通' and sent['ttl'] == '3600' and 'weight' not in sent
    await provider(api).update('example.com', Record('1', 'home.example.com', 'A', 'x'), '203.0.113.9', None, {})
    assert form(api.calls()[2])['record_line'] == '默认'


async def test_delete():
    api = FakeAPI().on('POST', BASE + '/Record.Remove', json={'status': OK})
    await provider(api).delete('example.com', Record('7', 'home.example.com', 'A', 'x'))
    assert form(api.calls()[0])['record_id'] == '7'


@pytest.mark.parametrize('code,expected', [('-1', 'auth_failed'), ('-7', 'forbidden'), ('-2', 'rate_limited'), ('7', 'zone_not_found'),
                                           ('17', 'bad_request')])
async def test_status_codes_in_http_200(code, expected):
    api = FakeAPI().on('POST', BASE + '/Record.List', json={'status': {'code': code, 'message': 'x'}})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == expected
