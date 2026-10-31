import httpx
import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.nsone\.net/v1'
RECORD = BASE + '/zones/example.com/home.example.com/A'


def provider(api):
    return get('ns1')({'key': 'k'}, client=api.client())


def record(*answers, ttl=3600):
    return {'zone': 'example.com', 'domain': 'home.example.com', 'type': 'A', 'ttl': ttl, 'filters': [{'filter': 'up'}],
            'answers': [{'id': f'a{i}', 'answer': [v], 'meta': {'up': True}} for i, v in enumerate(answers)]}


def test_metadata():
    cls = get('ns1')
    assert cls.tier == 'A' and not cls.caps.by_id and cls.caps.multi


async def test_zones_follow_link_pages():
    def handler(req, m):
        if 'after' not in req.url.params:
            return httpx.Response(200, json=[{'zone': 'b.com'}], headers={'Link': '<https://api.nsone.net/v1/zones?after=b.com>; rel="next"'})
        return [{'zone': 'a.com'}]
    api = FakeAPI().on('GET', BASE + '/zones', handler)
    assert (await provider(api).check())['zones'] == ['a.com', 'b.com']
    assert len(api.calls()) == 2 and api.calls()[0].headers['X-NSONE-Key'] == 'k'


async def test_records_match_and_miss():
    api = (FakeAPI().on('GET', RECORD, json=record('198.51.100.1', '198.51.100.2'))
           .on('GET', BASE + '/zones/example.com/example.com/A', status=404, json={'message': 'record not found'}))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('', '198.51.100.1', 3600), ('', '198.51.100.2', 3600)]
    assert await provider(api).records('example.com', 'example.com', 'A') == []


async def test_create_new_record():
    api = FakeAPI().on('GET', RECORD, status=404, json={'message': 'record not found'}).on('PUT', RECORD, json={})
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 300, {}) == ''
    assert FakeAPI.body(api.calls('PUT')[0]) == {'zone': 'example.com', 'domain': 'home.example.com', 'type': 'A',
                                                 'answers': [{'answer': ['203.0.113.5']}], 'ttl': 300}


async def test_create_appends_answer():
    api = FakeAPI().on('GET', RECORD, json=record('198.51.100.1')).on('POST', RECORD, json={})
    await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert FakeAPI.body(api.calls('POST')[0]) == {'answers': [{'id': 'a0', 'answer': ['198.51.100.1'], 'meta': {'up': True}},
                                                              {'answer': ['203.0.113.5']}]}


async def test_update_changes_one_answer_and_keeps_its_metadata():
    api = FakeAPI().on('GET', RECORD, json=record('198.51.100.1', '198.51.100.2')).on('POST', RECORD, json={})
    await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.2', 3600), '203.0.113.9', None, {})
    body = FakeAPI.body(api.calls('POST')[0])
    assert body == {'answers': [{'id': 'a0', 'answer': ['198.51.100.1'], 'meta': {'up': True}},
                                {'id': 'a1', 'answer': ['203.0.113.9'], 'meta': {'up': True}}]}


async def test_delete_last_answer_deletes_record():
    api = FakeAPI().on('GET', RECORD, json=record('198.51.100.1')).on('DELETE', RECORD, json={})
    await provider(api).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert [r.method for r in api.calls()] == ['GET', 'DELETE']


async def test_delete_one_of_several_answers():
    api = FakeAPI().on('GET', RECORD, json=record('198.51.100.1', '198.51.100.2')).on('POST', RECORD, json={})
    await provider(api).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert [a['answer'] for a in FakeAPI.body(api.calls('POST')[0])['answers']] == [['198.51.100.2']]


@pytest.mark.parametrize('status,message,expected', [
    (401, 'Unauthorized', 'auth_failed'), (403, 'insufficient permissions', 'forbidden'),
    (404, 'zone not found', 'zone_not_found'), (400, 'invalid answer', 'bad_request'), (429, 'rate limit exceeded', 'rate_limited'),
])
async def test_error_mapping(status, message, expected):
    api = FakeAPI().on('GET', RECORD, status=status, json={'message': message})
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == expected and err.value.message == message
