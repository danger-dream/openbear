import pytest

from app.ddns.providers import get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

BASE = r'https://api\.hetzner\.cloud/v1'
RRSET = BASE + '/zones/example.com/rrsets/home/A'
DONE = {'action': {'id': 5, 'command': 'x', 'status': 'success', 'error': None}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(delay):
        return None
    monkeypatch.setattr('app.ddns.providers.hetzner.asyncio.sleep', instant)


def provider(api):
    return get('hetzner')({'token': 'tok'}, client=api.client())


def rrset(*values, ttl=None, name='home'):
    return {'rrset': {'id': f'{name}/A', 'name': name, 'type': 'A', 'ttl': ttl, 'labels': {}, 'protection': {'change': False}, 'zone': 42,
                      'records': [{'value': v, 'comment': f'c{i}'} for i, v in enumerate(values)]}}


def test_metadata():
    cls = get('hetzner')
    assert cls.tier == 'A' and not cls.caps.by_id and cls.endpoint == 'https://api.hetzner.cloud/v1'
    assert 'dns.hetzner.com' in cls.fields[0].hint


async def test_zones_page_primary_only():
    def handler(req, m):
        page = int(req.url.params['page'])
        return {'zones': [{'id': page, 'name': f'z{page}.com', 'mode': 'primary'}],
                'meta': {'pagination': {'page': page, 'per_page': 100, 'next_page': 2 if page == 1 else None, 'last_page': 2}}}
    api = FakeAPI().on('GET', BASE + '/zones', handler)
    assert (await provider(api).check())['zones'] == ['z1.com', 'z2.com']
    assert dict(api.calls()[0].url.params) == {'mode': 'primary', 'page': '1', 'per_page': '100'}
    assert api.calls()[0].headers['Authorization'] == 'Bearer tok'


async def test_records_match_and_miss():
    api = (FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1', '198.51.100.2', ttl=300))
           .on('GET', BASE + '/zones/example.com/rrsets/@/A', status=404, json={'error': {'code': 'not_found', 'message': 'rrset not found'}})
           .on('GET', BASE + '/zones/example.com', json={'zone': {'id': 42, 'name': 'example.com'}}))
    found = await provider(api).records('example.com', 'home.example.com', 'A')
    assert [(r.id, r.value, r.ttl) for r in found] == [('', '198.51.100.1', 300), ('', '198.51.100.2', 300)]
    assert await provider(api).records('example.com', 'example.com', 'A') == []


async def test_missing_zone():
    not_found = {'error': {'code': 'not_found', 'message': 'not found'}}
    api = FakeAPI().on('GET', RRSET, status=404, json=not_found).on('GET', BASE + '/zones/example.com', status=404, json=not_found)
    with pytest.raises(ProviderError) as err:
        await provider(api).records('example.com', 'home.example.com', 'A')
    assert err.value.code == 'zone_not_found'


async def test_create_adds_record_without_comment():
    api = FakeAPI().on('POST', RRSET + '/actions/add_records', status=201, json=DONE)
    assert await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', 120, {}) == ''
    assert FakeAPI.body(api.calls()[0]) == {'records': [{'value': '203.0.113.5'}], 'ttl': 120}


async def test_update_replaces_one_value_and_keeps_comments_and_ttl():
    api = FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1', '198.51.100.2', ttl=300)).on('POST', RRSET + '/actions/set_records', status=201, json=DONE)
    await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.2', 300), '203.0.113.9', None, {})
    assert FakeAPI.body(api.calls('POST')[0]) == {'records': [{'value': '198.51.100.1', 'comment': 'c0'}, {'value': '203.0.113.9', 'comment': 'c1'}]}
    assert len(api.calls('POST')) == 1


async def test_update_waits_for_running_action_before_changing_ttl():
    running = {'action': {'id': 7, 'status': 'running', 'error': None}}
    api = (FakeAPI().on('GET', RRSET, json=rrset('198.51.100.1'))
           .on('POST', RRSET + '/actions/set_records', status=201, json=running)
           .on('GET', BASE + '/zones/actions/7', json={'action': {'id': 7, 'status': 'success', 'error': None}})
           .on('POST', RRSET + '/actions/change_ttl', status=201, json=DONE))
    await provider(api).update('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'), '203.0.113.9', 600, {})
    assert [r.url.path.rsplit('/', 1)[-1] for r in api.calls()] == ['A', 'set_records', '7', 'change_ttl']
    assert FakeAPI.body(api.calls()[-1]) == {'ttl': 600}


async def test_delete_removes_value():
    api = FakeAPI().on('POST', RRSET + '/actions/remove_records', status=201, json=DONE)
    await provider(api).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1'))
    assert FakeAPI.body(api.calls()[0]) == {'records': [{'value': '198.51.100.1'}]}


@pytest.mark.parametrize('status,code,expected', [
    (401, 'unauthorized', 'auth_failed'), (403, 'forbidden', 'forbidden'), (423, 'locked', 'record_conflict'),
    (422, 'incorrect_zone_mode', 'unsupported'), (422, 'invalid_input', 'bad_request'), (429, 'rate_limit_exceeded', 'rate_limited'),
])
async def test_error_mapping(status, code, expected):
    api = FakeAPI().on('POST', RRSET + '/actions/add_records', status=status, json={'error': {'code': code, 'message': 'msg'}})
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert err.value.code == expected and err.value.message == f'{code}: msg'


async def test_failed_action():
    failed = {'action': {'id': 8, 'status': 'error', 'error': {'code': 'action_failed', 'message': 'invalid value'}}}
    api = FakeAPI().on('POST', RRSET + '/actions/add_records', status=201, json=failed)
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '203.0.113.5', None, {})
    assert err.value.code == 'bad_request' and 'invalid value' in err.value.message
