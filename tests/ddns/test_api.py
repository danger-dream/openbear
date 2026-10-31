"""The 动态域名 REST API through the real web app: shapes the page relies on, errors and
the owner check; no answer carries a credential value."""
from app.webhooks.repository import one
from tests.ddns import fakeprovider
from tests.ddns.harness import (  # noqa: F401
    OWNER,
    add_account,
    add_node,
    base_env,
    env,
    probe_result,
)

ACCOUNT_KEYS = {'id', 'type', 'alias', 'zones', 'zonesAt', 'state', 'error', 'errorCode', 'secret', 'settings', 'records', 'issues'}
RECORD_KEYS = {'id', 'accountId', 'zone', 'name', 'type', 'source', 'backup', 'ttl', 'options', 'policy', 'enabled', 'desired', 'remote',
               'remoteAt', 'providerRecordId', 'state', 'stateText', 'error', 'errorCode', 'failCount', 'changedAt', 'syncedAt', 'checkedAt',
               'activeSource', 'readable'}
SOURCE_KEYS = {'kind', 'serverId', 'method', 'methodAuto', 'resolvedMethod', 'params', 'fixed'}
NODE_KEYS = {'serverId', 'name', 'region', 'online', 'platform', 'ddns', 'exec', 'v4', 'v6'}


async def call(e, method, path, body=None, status=200):
    response = await e.app.request(method, path, json=body, cookies=e.cookies, headers=e.origin)
    text = await response.text()
    assert response.status == status, text
    return await response.json(), text


async def test_accounts_records_and_their_shapes(env):  # noqa: F811
    e = env
    sid = await add_node(e, region='SG')
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7', nic='', stun='198.51.100.7')
    token = 'tok-api-secret-9876'
    data, _ = await call(e, 'POST', '/api/ddns/accounts/test', {'type': 'fake', 'credentials': {'token': token}})
    assert data == {'ok': True, 'zones': ['example.com'], 'writable': True}
    assert fakeprovider.store(token).calls and not (await call(e, 'GET', '/api/ddns'))[0]['accounts']
    data, text = await call(e, 'POST', '/api/ddns/accounts', {'type': 'fake', 'alias': '主账号', 'credentials': {'token': token},
                                                              'settings': {'ttl': 600}})
    account = data['account']
    assert set(account) == ACCOUNT_KEYS and token not in text
    assert account['secret']['name'] == 'ddns/Fake-主账号' and account['settings'] == {'endpoint': '', 'proxy': '', 'ttl': 600}
    data, _ = await call(e, 'PATCH', f'/api/ddns/accounts/{account["id"]}', {'alias': '备用'})
    assert data['account']['alias'] == '备用'
    data, _ = await call(e, 'POST', f'/api/ddns/accounts/{account["id"]}/check')
    assert data == {'ok': True, 'zones': ['example.com'], 'writable': True}
    data, _ = await call(e, 'POST', '/api/ddns/preview', {'serverId': sid, 'names': ['home.example.com'], 'types': ['A', 'AAAA'],
                                                         'source': {'method': 'auto', 'params': {}}, 'accountIds': {}})
    assert set(data) == {'ok', 'detected', 'rows'} and set(data['detected']['v4']) == {'value', 'method', 'auto', 'notes'}
    assert [(r['type'], r['action']) for r in data['rows']] == [('A', 'new'), ('AAAA', 'new')]
    assert set(data['rows'][0]) == {'name', 'type', 'zone', 'accounts', 'accountId', 'action', 'remote', 'remoteCount', 'value', 'error', 'errorCode'}
    data, _ = await call(e, 'POST', '/api/ddns/records', {'serverId': sid, 'source': {'method': 'url'}, 'backup': None, 'ttl': None, 'options': {},
                                                         'policy': {'onDrift': 'revert'}, 'rows': [
                                                             {'name': 'home.example.com', 'type': 'A', 'accountId': account['id'], 'confirmTakeover': False}]})
    assert data['skipped'] == [] and set(data['records'][0]) == RECORD_KEYS and set(data['records'][0]['source']) == SOURCE_KEYS
    rid = data['records'][0]['id']
    await e.ddns.idle()
    data, text = await call(e, 'GET', '/api/ddns')
    assert set(data) == {'ok', 'accounts', 'records', 'providers', 'nodes', 'issues'} and token not in text
    assert data['records'][0]['state'] == 'ok' and data['issues'] == 0 and any(p['type'] == 'fake' for p in data['providers'])
    assert set(data['nodes'][0]) == NODE_KEYS and data['nodes'][0]['region'] == 'SG'
    data, _ = await call(e, 'PATCH', f'/api/ddns/records/{rid}', {'ttl': 300, 'options': {'proxied': True}})
    assert data['record']['ttl'] == 300 and data['record']['options'] == {'proxied': True}
    data, _ = await call(e, 'POST', f'/api/ddns/records/{rid}/sync')
    assert data['record']['state'] == 'ok' and data['record']['syncedAt'] > 0
    data, _ = await call(e, 'GET', f'/api/ddns/records/{rid}/events?limit=2')
    assert len(data['events']) == 2 and set(data['events'][0]) == {'id', 'at', 'kind', 'text', 'detail'}
    data, _ = await call(e, 'POST', '/api/ddns/diagnose', {'serverId': sid, 'family': 'v4'})
    assert set(data) == {'ok', 'results', 'interfaces', 'recommend', 'notes'} and data['recommend'] == 'url'


async def test_errors_deletes_and_import(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    e.app.session.cookie_jar.clear()
    anonymous = await e.app.get('/api/ddns')
    assert anonymous.status == 401
    data, _ = await call(e, 'POST', '/api/ddns/accounts', {'type': 'nope', 'credentials': {}}, status=400)
    assert data == {'ok': False, 'error': 'provider_unknown', 'message': '不支持这个服务商'}
    data, _ = await call(e, 'POST', '/api/ddns/accounts', {'type': 'fake', 'credentials': {'token': ''}}, status=400)
    assert data['error'] == 'credential_missing' and 'Token' in data['message']
    data, _ = await call(e, 'GET', '/api/ddns/records/r_missing/events', status=404)
    assert data['error'] == 'record_not_found' and data['message']
    data, _ = await call(e, 'POST', '/api/ddns/diagnose', {'serverId': sid, 'family': 'v5'}, status=400)
    assert data['error'] == 'invalid_family'
    account = (await call(e, 'POST', '/api/ddns/accounts', {'type': 'fake', 'credentials': {'token': 'tok'}}))[0]['account']
    rows = [{'name': n, 'type': 'A', 'accountId': account['id']} for n in ('a.example.com', 'b.example.com')]
    records = (await call(e, 'POST', '/api/ddns/records', {'serverId': sid, 'source': {'method': 'url'}, 'rows': rows}))[0]['records']
    await e.ddns.idle()
    api = fakeprovider.store('tok')
    data, _ = await call(e, 'DELETE', f'/api/ddns/records/{records[0]["id"]}')
    assert data['ok'] and api.values('a.example.com') == ['198.51.100.7']
    data, _ = await call(e, 'DELETE', f'/api/ddns/records/{records[1]["id"]}?deleteRemote=1')
    assert data['ok'] and api.values('b.example.com') == []
    records = (await call(e, 'POST', '/api/ddns/records', {'serverId': sid, 'source': {'method': 'url'}, 'rows': rows[1:]}))[0]['records']
    await e.ddns.idle()
    data, _ = await call(e, 'DELETE', f'/api/ddns/accounts/{account["id"]}', status=409)
    assert data['error'] == 'account_in_use' and data['message']
    data, _ = await call(e, 'DELETE', f'/api/ddns/accounts/{account["id"]}?force=1')
    assert data['ok'] and (await call(e, 'GET', '/api/ddns'))[0]['records'] == [] and api.values('b.example.com') == ['198.51.100.7']
    data, _ = await call(e, 'POST', '/api/ddns/import/preview', {'yaml': 'dnsconf: 3', 'serverId': sid}, status=400)
    assert data['error'] == 'invalid_yaml'
    yaml = 'dnsconf:\n  - ipv4: {enable: true, gettype: url, domains: [c.example.com]}\n    dns: {name: aliesa, id: a, secret: b}\n'
    data, _ = await call(e, 'POST', '/api/ddns/import/preview', {'yaml': yaml, 'serverId': sid})
    assert set(data) == {'ok', 'accounts', 'records', 'unsupported', 'warnings'} and data['unsupported'][0]['reason'] == '暂不支持这个服务商'
    data, _ = await call(e, 'POST', '/api/ddns/import', {'yaml': yaml, 'serverId': sid, 'confirmTakeover': False})
    assert data == {'ok': True, 'accounts': [], 'records': [], 'skipped': []}


async def test_the_page_route_is_registered(env):  # noqa: F811
    assert any(r.canonical == '/ddns' for r in env.app.server.app.router.resources())


async def test_checking_a_saved_account_tries_the_settings_being_edited(env):  # noqa: F811
    """From the 10-30 check: 测试连接 on an edited account tried the settings as saved."""
    e = env
    account = await add_account(e)
    seen = []
    real = e.ddns._check

    async def check(account_id, kind, creds, settings):
        seen.append(settings.get('endpoint'))
        return await real(account_id, kind, creds, settings)
    e.ddns._check = check
    await e.ddns.check_account(OWNER, account['id'], {'settings': {'endpoint': 'https://api.example.net'}})
    assert seen == ['https://api.example.net']
    stored = await one(e.db.conn, 'SELECT settings_json FROM ddns_accounts WHERE id=?', (account['id'],))
    assert 'api.example.net' not in stored['settings_json']
