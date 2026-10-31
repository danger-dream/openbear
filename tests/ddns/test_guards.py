"""What keeps the service from writing the wrong record or the wrong value: records without ids
told apart by value, lines, a change during a sync, whole-zone providers, unexpected adapter
errors, a source coming back, refused probes, reports and writes kept in bounds, restarts."""
import json

import pytest

from app.ddns import service as ddns_module
from app.ddns.model import DdnsError
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.servers import register_server_tools
from app.webhooks.repository import one
from tests.ddns import fakeprovider
from tests.ddns.harness import (  # noqa: F401
    OWNER,
    add_account,
    add_node,
    advance,
    alerts,
    base_env,
    env,
    events,
    key_of,
    probe_result,
    record,
    report,
    sync,
    url_record,
)


def holding(api, value):
    return next(r for r in api.records.values() if r.value == value)


async def test_a_record_without_ids_is_only_the_one_holding_its_value(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e, token='nm', kind='fakename')
    api = fakeprovider.store('nm')
    api.add('n.example.com', 'A', '192.0.2.1')
    api.add('n.example.com', 'A', '192.0.2.2')
    rid = (await url_record(e, sid, account, name='n.example.com', row={'confirmTakeover': True}))['records'][0]['id']
    # The one taken over is written, found again by the value written.
    assert api.values('n.example.com') == ['192.0.2.2', '198.51.100.7']
    advance(e, 600)
    await sync(e, rid)
    assert (await record(e, rid))['state'] == 'ok' and api.values('n.example.com') == ['192.0.2.2', '198.51.100.7']
    # Changed from outside: neither holds a value it knows, so neither is written, and it says so once.
    mine = holding(api, '198.51.100.7')
    mine.value = '192.0.2.9'
    writes = len(api.writes())
    for _ in range(2):
        advance(e, 600)
        await sync(e, rid)
    rec = await record(e, rid)
    assert rec['state'] == 'ambiguous' and '认不出' in rec['stateText'] and len(api.writes()) == writes
    assert api.values('n.example.com') == ['192.0.2.2', '192.0.2.9']
    assert [a['kind'] for a in await alerts(e)] == ['ddns_ambiguous'] and (await events(e, rid)).count('ambiguous') == 1
    with pytest.raises(DdnsError) as err:
        await e.ddns.delete_record(OWNER, rid, delete_remote=True)
    assert err.value.error == 'record_ambiguous' and api.values('n.example.com') == ['192.0.2.2', '192.0.2.9']
    # Put back by hand it is its own again; deleting it leaves the other alone.
    mine.value = '198.51.100.7'
    await sync(e, rid)
    assert (await record(e, rid))['state'] == 'ok'
    await e.ddns.delete_record(OWNER, rid, delete_remote=True)
    assert api.values('n.example.com') == ['192.0.2.2']
    # A lone record holding something else is reverted by a sync, but not deleted unrecognised.
    rid = (await url_record(e, sid, account, name='l.example.com'))['records'][0]['id']
    holding(api, '198.51.100.7').value = '192.0.2.5'
    with pytest.raises(DdnsError) as err:
        await e.ddns.delete_record(OWNER, rid, delete_remote=True)
    assert err.value.error == 'record_ambiguous' and api.values('l.example.com') == ['192.0.2.5']
    await sync(e, rid, 'manual')
    assert api.values('l.example.com') == ['198.51.100.7']


async def test_the_one_left_of_several_is_not_taken_for_its_own(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e, token='nm', kind='fakename')
    api = fakeprovider.store('nm')
    managed = api.add('home.example.com', 'A', '192.0.2.1')
    users = api.add('home.example.com', 'A', '192.0.2.200')
    rid = (await url_record(e, sid, account, row={'confirmTakeover': True}))['records'][0]['id']
    assert api.records[managed].value == '198.51.100.7' and api.records[users].value == '192.0.2.200'
    # The managed one deleted outside: the user's own is all that is left, and it stays as it is.
    del api.records[managed]
    writes = len(api.writes())
    for _ in range(2):
        advance(e, ddns_module.CHECK_S)
        await sync(e, rid)
    rec = await record(e, rid)
    assert rec['state'] == 'ambiguous' and len(api.writes()) == writes and list(api.records) == [users]
    assert api.records[users].value == '192.0.2.200'
    assert [a['kind'] for a in await alerts(e)] == ['ddns_ambiguous'] and '只剩 1 条' in (await alerts(e))[0]['text']
    assert (await events(e, rid)).count('ambiguous') == 1 and 'drift' not in await events(e, rid)
    # Set to the desired value by hand it is its own again.
    api.records[users].value = '198.51.100.7'
    await sync(e, rid)
    assert (await record(e, rid))['state'] == 'ok'
    # Several seen on a later check counts too, not only at the takeover.
    rid = (await url_record(e, sid, account, name='w.example.com'))['records'][0]['id']
    other = api.add('w.example.com', 'A', '192.0.2.50')
    advance(e, ddns_module.CHECK_S)
    await sync(e, rid)
    assert (await one(e.db.conn, 'SELECT several_seen FROM ddns_records WHERE id=?', (rid,)))['several_seen'] == 1
    del api.records[holding_id(api, 'w.example.com', '198.51.100.7')]
    advance(e, ddns_module.CHECK_S)
    await sync(e, rid)
    assert (await record(e, rid))['state'] == 'ambiguous' and api.records[other].value == '192.0.2.50'
    # A line chosen again starts the count again.
    await e.ddns.update_record(OWNER, rid, {'options': {'line': 'default'}})
    assert (await one(e.db.conn, 'SELECT several_seen FROM ddns_records WHERE id=?', (rid,)))['several_seen'] == 0


def holding_id(api, name, value):
    return next(k for k, r in api.records.items() if (r.name, r.value) == (name, value))


async def test_records_from_before_several_seen_count_as_never_several(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e, token='nm', kind='fakename')
    rid = (await url_record(e, sid, account))['records'][0]['id']
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('ALTER TABLE ddns_records DROP COLUMN several_seen')
    await ddns_module.migrate(e.db)
    assert (await one(e.db.conn, 'SELECT several_seen FROM ddns_records WHERE id=?', (rid,)))['several_seen'] == 0
    await ddns_module.migrate(e.db)
    # A lone record changed outside is still reverted.
    api = fakeprovider.store('nm')
    holding(api, '198.51.100.7').value = '192.0.2.5'
    advance(e, ddns_module.CHECK_S)
    await sync(e, rid, 'manual')
    assert api.values('home.example.com') == ['198.51.100.7'] and (await record(e, rid))['state'] == 'ok'


async def test_records_on_other_lines_are_left_alone(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    api = fakeprovider.store('tok')
    telecom = api.add('l.example.com', 'A', '192.0.2.1', line='telecom')
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    rows = (await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['l.example.com'], 'source': {'method': 'url'}}))['rows']
    assert rows[0]['action'] == 'new'
    rows = (await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['l.example.com'], 'source': {'method': 'url'}, 'options': {'line': 'telecom'}}))['rows']
    assert rows[0]['action'] == 'takeover' and rows[0]['remote'] == '192.0.2.1'
    # The default line's record is a new one beside the 电信 one, which no write touches.
    rid = (await url_record(e, sid, account, name='l.example.com'))['records'][0]['id']
    await report(e, sid, key_of(e, sid), '198.51.100.8')
    advance(e, 600)
    await sync(e, rid)
    assert (await record(e, rid))['state'] == 'ok' and api.records[telecom].value == '192.0.2.1'
    assert api.values('l.example.com') == ['192.0.2.1', '198.51.100.8']
    # One for the 电信 line takes over that line's record only.
    default = api.add('t.example.com', 'A', '192.0.2.3')
    telecom = api.add('t.example.com', 'A', '192.0.2.4', line='telecom')
    rid = (await url_record(e, sid, account, name='t.example.com', options={'line': 'telecom'}, row={'confirmTakeover': True}))['records'][0]['id']
    assert (await record(e, rid))['providerRecordId'] == telecom
    assert api.records[telecom].value == '198.51.100.8' and api.records[default].value == '192.0.2.3'


async def test_an_address_changing_during_a_sync_is_written_next(env, monkeypatch):  # noqa: F811
    e = env
    sid = await add_node(e)
    session = e.nodes.connect(sid)
    account = await add_account(e)
    # Alert only: an outside change is not written over, a new address of its own is.
    rid = (await url_record(e, sid, account, policy={'onDrift': 'alert'}))['records'][0]['id']
    key = key_of(e, sid)
    update = fakeprovider.FakeProvider.update

    async def racing(self, zone, record, value, ttl, opts):
        await update(self, zone, record, value, ttl, opts)
        if value == '198.51.100.8':
            advance(e, 1)
            await e.servers.hub._message(session, {'t': 'ddns', 'obs': [{'key': key, 'value': '198.51.100.9'}]})
    monkeypatch.setattr(fakeprovider.FakeProvider, 'update', racing)
    await report(e, sid, key, '198.51.100.8')
    rec = await record(e, rid)
    assert fakeprovider.store('tok').values('home.example.com') == ['198.51.100.9']
    assert rec['state'] == 'ok' and rec['desired'] == rec['remote'] == '198.51.100.9' and 'drift' not in await events(e, rid)
    # An outside change after it is still one.
    holding(fakeprovider.store('tok'), '198.51.100.9').value = '192.0.2.10'
    advance(e, 600)
    await sync(e, rid)
    assert (await record(e, rid))['state'] == 'drift'


async def test_whole_zone_writes_to_one_zone_take_turns(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e, token='zn', kind='fakezone')
    api = fakeprovider.store('zn')
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    names = ['a.example.com', 'b.example.com', 'c.example.com']
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': n, 'type': 'A', 'accountId': account['id']} for n in names]})
    await e.ddns.idle()
    assert [api.values(n) for n in names] == [['198.51.100.7']] * 3
    # One report changes all three at once: each rewrite sees the ones before it.
    await report(e, sid, key_of(e, sid), '198.51.100.8')
    assert [api.values(n) for n in names] == [['198.51.100.8']] * 3
    assert [(await record(e, r['id']))['state'] for r in result['records']] == ['ok'] * 3


async def test_an_unexpected_adapter_error_backs_off_and_alerts(env, monkeypatch):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    update = fakeprovider.FakeProvider.update

    async def broken(self, zone, record, value, ttl, opts):
        raise KeyError('rrset')
    monkeypatch.setattr(fakeprovider.FakeProvider, 'update', broken)
    await report(e, sid, key_of(e, sid), '198.51.100.8')
    row = await one(e.db.conn, 'SELECT state,fail_count,next_ms,error_code FROM ddns_records WHERE id=?', (rid,))
    assert row['state'] == 'pending' and row['fail_count'] == 1 and row['error_code'] == 'internal_error'
    assert row['next_ms'] == e.clock[0] + ddns_module.RETRY_MIN_S * 1000
    assert [a['kind'] for a in await alerts(e)] == ['ddns_fail'] and 'KeyError' in (await record(e, rid))['error']
    monkeypatch.setattr(fakeprovider.FakeProvider, 'update', update)
    e.clock[0] = row['next_ms']
    await e.ddns.tick()
    await e.ddns.idle()
    assert (await record(e, rid))['state'] == 'ok'


async def test_a_socks_proxy_saved_before_stops_syncing_with_a_reason(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    with pytest.raises(DdnsError) as err:
        await e.ddns.update_account(OWNER, account['id'], {'settings': {'proxy': 'socks5://127.0.0.1:1080'}})
    assert err.value.error == 'invalid_proxy'
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('UPDATE ddns_accounts SET settings_json=? WHERE id=?',
                           (json.dumps({'endpoint': '', 'proxy': 'socks5://u:pw@127.0.0.1:1080', 'ttl': None}), account['id']))
    await sync(e, rid, 'manual')
    rec = await record(e, rid)
    assert rec['state'] == 'failed' and rec['errorCode'] == 'invalid_proxy' and '只支持 http 和 https' in rec['error'] and 'pw' not in rec['error']
    view = await e.ddns.overview(OWNER)
    assert view['accounts'][0]['settings']['proxy'].startswith('socks5://') and view['accounts'][0]['state'] == 'error'
    # Cleared on the page, it syncs again.
    await e.ddns.update_account(OWNER, account['id'], {'settings': {'proxy': ''}})
    await e.ddns.idle()
    assert (await record(e, rid))['state'] == 'ok'


async def test_a_source_back_online_switches_back_only_on_a_new_report(env):  # noqa: F811
    e = env
    home, office = await add_node(e, '家'), await add_node(e, '公司')
    e.nodes.connect(home)
    e.nodes.connect(office)
    account = await add_account(e)
    e.nodes.probe[home] = probe_result(url='198.51.100.7')
    rid = (await e.ddns.create_records(OWNER, {'serverId': home, 'source': {'method': 'url'}, 'backup': {'serverId': office, 'afterS': 300},
                                               'rows': [{'name': 'home.example.com', 'type': 'A', 'accountId': account['id']}]}))['records'][0]['id']
    await e.ddns.idle()
    key = key_of(e, home)
    await report(e, home, key, '198.51.100.7')
    await report(e, office, key, '198.51.100.50')
    e.nodes.drop(home)
    advance(e, 301)
    await e.ddns.tick()
    await e.ddns.idle()
    api = fakeprovider.store('tok')
    assert (await record(e, rid))['activeSource'] == 'backup' and api.values('home.example.com') == ['198.51.100.50']
    # Reconnected: what it saw before going away is not written back.
    e.nodes.connect(home)
    await e.servers.node_hello(home, {'platform': 'linux'}, {}, '1.0.0')
    await e.ddns.idle()
    await e.ddns.tick()
    await e.ddns.idle()
    rec = await record(e, rid)
    assert rec['activeSource'] == 'backup' and api.values('home.example.com') == ['198.51.100.50'] and 'failback' not in await events(e, rid)
    # A failing report says no more; a fresh address switches back.
    await report(e, home, key, '', error='timeout', fails=3)
    assert (await record(e, rid))['activeSource'] == 'backup'
    await report(e, home, key, '198.51.100.7')
    rec = await record(e, rid)
    assert rec['activeSource'] == 'primary' and api.values('home.example.com') == ['198.51.100.7']


async def test_probes_a_node_refuses_are_shown(env, monkeypatch):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    key = key_of(e, sid)
    call = e.nodes.call

    async def refusing(server_id, method, params, timeout):
        if method == 'ddns.watch' and any(p['method'] == 'stun' for p in params['probes']):
            return {'ok': False, 'error': 'invalid_params'}
        return await call(server_id, method, params, timeout)
    monkeypatch.setattr(e.servers.hub, 'call', refusing)
    e.nodes.probe[sid] = probe_result(stun='198.51.100.9')
    added = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'stun'}, 'rows': [
        {'name': 's.example.com', 'type': 'A', 'accountId': account['id']}]})
    await e.ddns.idle()
    other = added['records'][0]['id']
    await e.ddns.tick()
    await e.ddns.idle()
    rec = await record(e, other)
    assert rec['state'] == 'probe_refused' and 'invalid_params' in rec['stateText']
    # The probe it already ran keeps going.
    await report(e, sid, key, '198.51.100.8')
    assert (await record(e, rid))['desired'] == '198.51.100.8'
    # Accepted once the node takes the list again.
    monkeypatch.setattr(e.servers.hub, 'call', call)
    await e.ddns.push(sid)
    await e.ddns.tick()
    await e.ddns.idle()
    assert (await record(e, other))['state'] != 'probe_refused'


async def test_reports_only_for_probes_given_are_kept(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    await url_record(e, sid, account)
    key = key_of(e, sid)
    session = e.servers.hub.sessions[sid]
    await e.servers.hub._message(session, {'t': 'ddns', 'obs': [{'key': f'k{n}', 'value': '192.0.2.1'} for n in range(64)]})
    await report(e, sid, key, '198.51.100.8')
    assert set(e.ddns.obs[sid]) == {key}


async def test_a_flapping_source_writes_at_most_write_max_an_hour(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    key = key_of(e, sid)
    api = fakeprovider.store('tok')
    start = len(api.writes())
    for n in range(30):
        advance(e, 10)
        await report(e, sid, key, '198.51.100.8' if n % 2 == 0 else '198.51.100.9')
    # The record's own creation counts too.
    assert len(api.writes()) - start == ddns_module.WRITE_MAX - 1
    rec = await record(e, rid)
    assert rec['state'] == 'pending' and '太频繁' in rec['stateText'] and rec['desired'] == '198.51.100.9'
    assert (await events(e, rid)).count('hold') == 1
    # A sync asked for is not held back; the hour over, the latest address is written.
    await sync(e, rid, 'manual')
    assert api.values('home.example.com') == ['198.51.100.9']
    await report(e, sid, key, '198.51.100.8')
    assert api.values('home.example.com') == ['198.51.100.9']
    nxt = (await one(e.db.conn, 'SELECT next_ms FROM ddns_records WHERE id=?', (rid,)))['next_ms']
    e.clock[0] = nxt
    await e.ddns.tick()
    await e.ddns.idle()
    assert api.values('home.example.com') == ['198.51.100.8'] and (await record(e, rid))['state'] == 'ok'


async def test_an_update_only_provider_writes_after_a_restart(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e, token='push', kind='fakepush')
    rid = (await url_record(e, sid, account))['records'][0]['id']
    api = fakeprovider.store('push')
    # Stopped mid-write: the new address is wanted, the provider still holds the old one.
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("UPDATE ddns_records SET desired='198.51.100.8', changed_ms=changed_ms+1, state='syncing' WHERE id=?", (rid,))
    nxt = (await one(e.db.conn, 'SELECT next_ms FROM ddns_records WHERE id=?', (rid,)))['next_ms']
    assert nxt - e.clock[0] == ddns_module.REWRITE_S * 1000
    advance(e, 60)
    await e.ddns.start()
    await e.ddns.tick()
    await e.ddns.idle()
    assert api.calls[-1] == ('update', 'home.example.com', '198.51.100.8') and (await record(e, rid))['state'] == 'ok'
    # Nothing left to write: the daily rewrite is a day after this write, not sooner.
    advance(e, 60)
    await e.ddns.start()
    await e.ddns.tick()
    await e.ddns.idle()
    assert [c for c in api.calls if c[0] == 'update'][-1] == ('update', 'home.example.com', '198.51.100.8')
    assert len([c for c in api.calls if c[0] == 'update']) == 2


async def test_secret_refs_and_endpoints_through_the_tool(env):  # noqa: F811
    e = env
    own = await add_account(e)
    mine = await e.ddns._memory().tool_call('secret', {'action': 'set', 'name': 'cf-personal', 'note': '自己的',
                                                       'kvJson': json.dumps([{'key': 'token', 'value': 'tok'}])})
    with pytest.raises(DdnsError) as err:
        await e.ddns.create_account(OWNER, {'type': 'fake', 'secretRef': {'secretId': mine['item']['id']}})
    assert err.value.error == 'secret_not_ddns'
    with pytest.raises(DdnsError) as err:
        await e.ddns.test_account(OWNER, {'type': 'fake', 'secretRef': {'secretId': mine['item']['id']}})
    assert err.value.error == 'secret_not_ddns'
    shared = await e.ddns.create_account(OWNER, {'type': 'fake', 'alias': '同一凭据', 'secretRef': {'secretId': own['secret']['id']}})
    assert shared['account']['state'] == 'ok'
    # The page sets an endpoint; the tool keeps it and cannot change it.
    await e.ddns.update_account(OWNER, own['id'], {'settings': {'endpoint': 'https://mirror.example.net/api'}})
    registry = ToolRegistry()
    register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-g',779,123,'t')")
    ctx = ToolRuntimeContext(chat_id=779, conversation_uuid='conv-g')

    async def call(action, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=ctx))
    for action, params in (('ddns_account_add', {'type': 'fake', 'credentials': {'token': 'tok'}, 'settings': {'endpoint': 'https://evil.test'}}),
                           ('ddns_account_check', {'type': 'fake', 'credentials': {'token': 'tok'}, 'settings': {'endpoint': 'https://evil.test'}}),
                           ('ddns_account_update', {'accountId': own['id'], 'settings': {'endpoint': 'https://evil.test'}}),
                           ('ddns_account_update', {'accountId': own['id'], 'settings': {'endpoint': ''}})):
        result = await call(action, **params)
        assert result['ok'] is False and result['error'] == 'endpoint_web_only', (action, result)
    result = await call('ddns_account_update', accountId=own['id'], alias='改名', settings={'ttl': 600, 'endpoint': 'https://mirror.example.net/api'})
    assert result['ok'] is True and result['account']['settings'] == {'endpoint': 'https://mirror.example.net/api', 'proxy': '', 'ttl': 600}
    result = await call('ddns_account_add', type='fake', alias='AI', credentials={'token': 'tok'}, settings={'ttl': 600})
    assert result['ok'] is True and result['account']['settings']['endpoint'] == ''


async def test_the_tool_cannot_carry_another_accounts_secret_to_a_callback(env):  # noqa: F811
    import httpx

    from app.ddns import providers
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    token = 'VICTIM-TOKEN-0123456789abcdef'
    victim = await add_account(e, token=token)
    secret_id = victim['secret']['id']
    sent = []

    def handler(req):
        sent.append(str(req.url) + req.content.decode())
        return httpx.Response(200, text='ok')
    real_make = ddns_module.Ddns._make
    e.ddns._make = lambda kind, creds, settings: (providers.get(kind)(creds, settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                                                  if kind == 'callback' else real_make(e.ddns, kind, creds, settings))
    registry = ToolRegistry()
    register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-c',782,123,'t')")
    ctx = ToolRuntimeContext(chat_id=782, conversation_uuid='conv-c')

    async def call(action, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=ctx))
    ref = {'secretId': secret_id, 'map': {'url': 'token'}}
    result = await call('ddns_account_add', type='callback', alias='hook', secretRef=ref)
    assert result['ok'] is False and result['error'] == 'secret_ref_web_only'
    result = await call('ddns_account_check', type='callback', secretRef=ref)
    assert result['ok'] is False and result['error'] == 'secret_ref_web_only'
    # One the page pointed at the other account's secret: the tool writes into a secret of its own, never into that one.
    hook = (await e.ddns.create_account(OWNER, {'type': 'callback', 'alias': 'hook', 'secretRef': ref}))['account']
    result = await call('ddns_account_update', accountId=hook['id'], credentials={'url': 'https://collector.attacker.example/c'})
    own = result['account']['secret']['id']
    assert result['ok'] is True and own != secret_id
    assert [i['key'] for i in (await e.ddns._secret_kv(secret_id))[1]] == ['token']
    result = await call('ddns_account_update', accountId=hook['id'], secretRef={'secretId': secret_id, 'map': {'url': 'url', 'body': 'token'}})
    assert result['ok'] is False and result['error'] == 'secret_ref_web_only'
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    result = await call('ddns_add', serverId=sid, source={'method': 'url'}, rows=[{'name': 'x.example.com', 'type': 'A', 'accountId': hook['id']}])
    await e.ddns.idle()
    assert sent and not any(token in s for s in sent)
    assert (await e.ddns.check_account(OWNER, victim['id']))['ok'] is True
    # A secret only the account reads is still written in place through the tool, and the page keeps writing a shared one.
    result = await call('ddns_account_update', accountId=hook['id'], credentials={'url': 'https://hook.example/u?ip={ip}'})
    assert result['ok'] is True and result['account']['secret']['id'] == own
    assert [i['value'] for i in (await e.ddns._secret_kv(own))[1] if i['key'] == 'url'] == ['https://hook.example/u?ip={ip}']
    shared = (await e.ddns.create_account(OWNER, {'type': 'fake', 'alias': '同一凭据', 'secretRef': {'secretId': secret_id}}))['account']
    await e.ddns.update_account(OWNER, shared['id'], {'credentials': {'token': token}})
    assert (await e.ddns._account(OWNER, shared['id']))['secret_id'] == secret_id


def test_scrub_masks_before_cutting_and_catches_pieces():
    creds = {'token': 'tok-0123456789abcdef', 'headers': 'X-Auth-Token: hdr-9f2kq4242\nAccept: application/json'}
    text = ddns_module.scrub('denied: Bearer tok-0123456789ab... (cut by the provider); header hdr-9f2kq4242 refused', creds)
    assert 'tok-0123456789' not in text and 'hdr-9f2kq4242' not in text and text.startswith('denied: Bearer ***')
    # Masked first, then cut: no part of a credential at the end.
    long = 'x' * 495 + ' tok-0123456789abcdef'
    assert ddns_module.scrub(long, creds) == ('x' * 495 + ' ***')[:500]
    assert 'tok-' not in ddns_module.scrub('x' * 490 + ' tok-0123456789abcdef', creds)
    assert ddns_module.scrub('nothing secret here', creds) == 'nothing secret here'
