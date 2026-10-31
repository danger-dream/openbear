import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.vercel\.com'


def provider(api, team=''):
    return get('vercel')({'token': 'tok', 'team_id': team}, client=api.client())


def item(rid='rec_1', name='home', value='198.51.100.1'):
    return {'id': rid, 'slug': 's', 'name': name, 'type': 'A', 'value': value, 'ttl': 60, 'comment': 'kept'}


def test_metadata():
    cls = get('vercel')
    assert cls.tier == 'A' and cls.fields[1].optional


async def test_zones_follow_until_and_team():
    def handler(req, m):
        if 'until' not in req.url.params:
            return {'domains': [{'name': 'b.com'}], 'pagination': {'count': 1, 'next': 1540095775951, 'prev': None}}
        return {'domains': [{'name': 'a.com'}], 'pagination': {'count': 1, 'next': None, 'prev': 1}}
    api = FakeAPI().on('GET', BASE + '/v5/domains', handler)
    assert (await provider(api, 'team_x').check())['zones'] == ['a.com', 'b.com']
    first, second = api.calls()
    assert first.headers['Authorization'] == 'Bearer tok' and first.url.params['teamId'] == 'team_x'
    assert second.url.params['until'] == '1540095775951'


async def test_records_match_and_miss():
    api = FakeAPI().on('GET', BASE + '/v5/domains/example.com/records',
                       json={'records': [item(), item('rec_2', ''), item('rec_3', 'x.home')], 'pagination': {'next': None}})
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('rec_1', '198.51.100.1', 60)]
    assert [r.id for r in await provider(api).records('example.com', 'example.com', 'A')] == ['rec_2']
    assert await provider(api).records('example.com', 'none.example.com', 'A') == []
    assert 'teamId' not in api.calls()[0].url.params


async def test_create():
    api = FakeAPI().on('POST', BASE + '/v2/domains/example.com/records', json={'uid': 'rec_new', 'updated': 1})
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 300, {}) == 'rec_new'
    assert FakeAPI.body(api.calls()[0]) == {'name': 'home', 'type': 'A', 'value': '203.0.113.5', 'ttl': 300}


async def test_update_patches_value_only():
    api = FakeAPI().on('PATCH', BASE + '/v1/domains/records/rec_1', json=item())
    await provider(api).update('example.com', Record('rec_1', 'home.example.com', 'A', '198.51.100.1', 60), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls()[0]) == {'value': '203.0.113.9'}


async def test_delete():
    api = FakeAPI().on('DELETE', BASE + '/v2/domains/example.com/records/rec_1', json={})
    await provider(api).delete('example.com', Record('rec_1', 'home.example.com', 'A', 'x'))
    assert len(api.calls()) == 1


@pytest.mark.parametrize('status,error,expected', [
    (403, {'code': 'forbidden', 'message': 'Not authorized', 'invalidToken': True}, 'auth_failed'),
    (403, {'code': 'forbidden', 'message': 'Not authorized'}, 'forbidden'),
    (404, {'code': 'not_found', 'message': 'Domain not found'}, 'zone_not_found'),
    (429, {'code': 'rate_limited', 'message': 'slow down'}, 'rate_limited')])
async def test_error_mapping(status, error, expected):
    api = FakeAPI().on('GET', BASE + '/v5/domains/example.com/records', status=status, json={'error': error})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == expected
