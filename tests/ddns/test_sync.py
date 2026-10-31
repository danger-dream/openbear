"""Converging records at the fake provider: creation and takeover, the periodic check,
outside changes, deletion, update-only providers, errors and retries, alerts."""
import json

from app.ddns import service as ddns_module
from app.webhooks.repository import many, one
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


async def test_create_converges_and_the_periodic_check_writes_nothing(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    assert account['zones'] == ['example.com'] and account['state'] == 'ok'
    result = await url_record(e, sid, account)
    assert result['skipped'] == []
    rid = result['records'][0]['id']
    api = fakeprovider.store('tok')
    assert api.values('home.example.com') == ['198.51.100.7']
    rec = await record(e, rid)
    assert rec['state'] == 'ok' and rec['remote'] == rec['desired'] == '198.51.100.7' and rec['providerRecordId']
    assert rec['readable'] is True and rec['syncedAt'] == rec['checkedAt'] > 0
    # Read, write, read back.
    assert [c[0] for c in api.calls[-3:]] == ['records', 'create', 'records']
    assert await events(e, rid) == ['create', 'write']
    # Ten minutes on, the check reads and writes nothing.
    stored = await one(e.db.conn, 'SELECT next_ms FROM ddns_records WHERE id=?', (rid,))
    assert stored['next_ms'] == e.clock[0] + ddns_module.CHECK_S * 1000
    writes = len(api.writes())
    advance(e, ddns_module.CHECK_S)
    await e.ddns.tick()
    await e.ddns.idle()
    assert len(api.writes()) == writes and api.calls[-1][0] == 'records'
    assert (await record(e, rid))['checkedAt'] == e.clock[0]


async def test_a_new_address_is_written_once_and_read_back(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    key = key_of(e, sid)
    advance(e, 30)
    await report(e, sid, key, '198.51.100.8')
    api = fakeprovider.store('tok')
    assert api.values('home.example.com') == ['198.51.100.8']
    rec = await record(e, rid)
    assert rec['desired'] == rec['remote'] == '198.51.100.8' and rec['changedAt'] == e.clock[0] and rec['state'] == 'ok'
    assert (await events(e, rid))[-2:] == ['address', 'write']
    # The same value again changes nothing.
    calls = len(api.calls)
    await report(e, sid, key, '198.51.100.8')
    assert len(api.calls) == calls


async def test_takeover_needs_confirmation_and_unconfirmed_rows_are_not_saved(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    api = fakeprovider.store('tok')
    old = api.add('www.example.com', 'A', '192.0.2.1')
    api.add('same.example.com', 'A', '198.51.100.7')
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['www.example.com', 'same.example.com', 'new.example.com', 'x.other.org'],
                                           'source': {'method': 'url'}})
    assert preview['detected']['v4']['value'] == '198.51.100.7' and preview['detected']['v6'] is None
    rows = {r['name']: r for r in preview['rows']}
    assert rows['www.example.com']['action'] == 'takeover' and rows['www.example.com']['remote'] == '192.0.2.1'
    assert rows['www.example.com']['remoteCount'] == 1 and rows['www.example.com']['zone'] == 'example.com'
    assert rows['www.example.com']['accounts'] == [{'id': account['id'], 'alias': '主账号', 'type': 'fake'}]
    assert rows['same.example.com']['action'] == 'same' and rows['new.example.com']['action'] == 'new'
    assert rows['x.other.org']['action'] == 'error' and rows['x.other.org']['errorCode'] == 'no_account'
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': n, 'type': 'A', 'accountId': account['id']} for n in ('www.example.com', 'same.example.com')]})
    assert result['skipped'] == [{'name': 'www.example.com', 'type': 'A', 'reason': 'takeover_not_confirmed', 'remote': '192.0.2.1'}]
    assert [r['name'] for r in result['records']] == ['same.example.com']
    assert not await one(e.db.conn, "SELECT 1 FROM ddns_records WHERE name='www.example.com'")
    await e.ddns.idle()
    assert api.values('www.example.com') == ['192.0.2.1']
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': 'www.example.com', 'type': 'A', 'accountId': account['id'], 'confirmTakeover': True}]})
    await e.ddns.idle()
    rid = result['records'][0]['id']
    rec = await record(e, rid)
    assert rec['providerRecordId'] == old and rec['remote'] == '198.51.100.7' and api.values('www.example.com') == ['198.51.100.7']
    assert await events(e, rid) == ['create', 'takeover', 'write']
    again = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': 'www.example.com', 'type': 'A', 'accountId': account['id'], 'confirmTakeover': True}]})
    assert again['skipped'][0]['reason'] == 'exists'


async def test_only_the_taken_over_record_is_changed_unless_multi_all(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    api = fakeprovider.store('tok')
    first = api.add('m.example.com', 'A', '192.0.2.1')
    api.add('m.example.com', 'A', '192.0.2.2')
    rid = (await url_record(e, sid, account, name='m.example.com', row={'confirmTakeover': True}))['records'][0]['id']
    assert (await record(e, rid))['providerRecordId'] == first
    assert api.values('m.example.com') == ['192.0.2.2', '198.51.100.7']
    await e.ddns.update_record(OWNER, rid, {'policy': {'multi': 'all'}})
    await e.ddns.idle()
    assert api.values('m.example.com') == ['198.51.100.7', '198.51.100.7']


async def test_outside_changes_are_reverted_with_growing_waits(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    api = fakeprovider.store('tok')
    pid = (await record(e, rid))['providerRecordId']
    waits = []
    for n, value in enumerate(('192.0.2.10', '192.0.2.11', '192.0.2.12', '192.0.2.13'), 1):
        advance(e, 30)
        api.records[pid].value = value
        await sync(e, rid)
        rec = await record(e, rid)
        start = e.clock[0]
        # Wait out the revert; checks before it is due change nothing.
        while api.records[pid].value != '198.51.100.7':
            assert rec['state'] == ('drift' if n == 1 else 'fighting') and rec['remote'] == value
            nxt = (await one(e.db.conn, 'SELECT next_ms FROM ddns_records WHERE id=?', (rid,)))['next_ms']
            e.clock[0] = nxt
            await e.ddns.tick()
            await e.ddns.idle()
            rec = await record(e, rid)
        waits.append((e.clock[0] - start) // 1000)
        assert rec['state'] == ('ok' if n == 1 else 'fighting') and rec['remote'] == '198.51.100.7'
    assert waits == [0, 60, 300, 900]
    kinds = await events(e, rid)
    assert kinds.count('drift') == 4 and kinds.count('write') == 5
    # One alert an hour for the record; it was sent at the first change.
    sent = await alerts(e)
    assert [a['kind'] for a in sent] == ['ddns_drift'] and '192.0.2.10' in sent[0]['text']
    assert (await record(e, rid))['stateText'].startswith('另一个系统也在写这条记录')
    # An hour after the last change it is quiet again.
    advance(e, 3600)
    api.records[pid].value = '192.0.2.20'
    await sync(e, rid)
    assert api.records[pid].value == '198.51.100.7' and (await record(e, rid))['state'] == 'ok'
    sent = await alerts(e)
    assert len(sent) == 2 and '192.0.2.20' in sent[1]['text']


async def test_on_drift_alert_only_does_not_revert(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account, policy={'onDrift': 'alert'}))['records'][0]['id']
    api = fakeprovider.store('tok')
    pid = (await record(e, rid))['providerRecordId']
    api.records[pid].value = '192.0.2.10'
    await sync(e, rid)
    advance(e, 3600)
    await sync(e, rid)
    rec = await record(e, rid)
    assert api.records[pid].value == '192.0.2.10' and rec['state'] == 'drift' and '只告警' in rec['stateText']
    assert '按设置只告警' in (await alerts(e))[0]['text']


async def test_a_deleted_record_is_created_again(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    api = fakeprovider.store('tok')
    old = (await record(e, rid))['providerRecordId']
    api.records.clear()
    advance(e, 600)
    await sync(e, rid)
    rec = await record(e, rid)
    assert api.values('home.example.com') == ['198.51.100.7'] and rec['state'] == 'ok'
    assert rec['providerRecordId'] and rec['providerRecordId'] != old
    assert (await events(e, rid))[-2:] == ['drift', 'recreated']
    assert [a['kind'] for a in await alerts(e)] == ['ddns_recreated']


async def test_update_only_provider(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e, token='push', kind='fakepush')
    assert account['zones'] == [] and account['state'] == 'ok'
    rid = (await url_record(e, sid, account))['records'][0]['id']
    api = fakeprovider.store('push')
    rec = await record(e, rid)
    assert rec['zone'] == 'example.com' and rec['readable'] is False and rec['state'] == 'ok'
    assert rec['stateText'] == '正常；服务商不支持读取，无法检测外部改动'
    assert [c[0] for c in api.calls] == ['check', 'update'] and api.values('home.example.com') == ['198.51.100.7']
    # Nothing to read: a check before a day has passed writes nothing, a day later it writes again.
    nxt = (await one(e.db.conn, 'SELECT next_ms FROM ddns_records WHERE id=?', (rid,)))['next_ms']
    assert nxt == e.clock[0] + ddns_module.REWRITE_S * 1000
    advance(e, 3600)
    await sync(e, rid)
    assert [c[0] for c in api.calls] == ['check', 'update']
    advance(e, ddns_module.REWRITE_S)
    await e.ddns.tick()
    await e.ddns.idle()
    assert [c[0] for c in api.calls] == ['check', 'update', 'update']
    # A new address is written straight away.
    await report(e, sid, key_of(e, sid), '198.51.100.9')
    assert api.calls[-1] == ('update', 'home.example.com', '198.51.100.9')
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['other.example.com'], 'source': {'method': 'url'}})
    assert preview['rows'][0]['action'] == 'new' and preview['rows'][0]['remote'] is None


async def test_permanent_errors_stop_until_a_manual_sync(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    api = fakeprovider.store('tok')
    api.fail('auth_failed')
    advance(e, 600)
    await sync(e, rid)
    rec = await record(e, rid)
    assert rec['state'] == 'failed' and rec['errorCode'] == 'auth_failed' and rec['failCount'] == 1
    view = (await e.ddns.overview(OWNER))['accounts'][0]
    assert view['state'] == 'error' and view['errorCode'] == 'auth_failed' and view['issues'] == 1
    assert [a['kind'] for a in await alerts(e)] == ['ddns_fail']
    calls = len(api.calls)
    advance(e, 3600)
    await e.ddns.tick()
    await sync(e, rid)
    # Only the hourly zone refresh reaches the provider; the record is not tried again.
    assert [c[0] for c in api.calls[calls:]] == ['check'] and (await record(e, rid))['state'] == 'failed'
    result = await e.ddns.sync_now(OWNER, rid)
    assert result['record']['state'] == 'ok' and result['record']['failCount'] == 0
    assert (await e.ddns.overview(OWNER))['accounts'][0]['state'] == 'ok'
    assert [a['kind'] for a in await alerts(e)] == ['ddns_fail', 'ddns_recover']
    assert (await events(e, rid))[-2:] == ['fail', 'recover']


async def test_retryable_errors_back_off_and_alert_after_three(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    api = fakeprovider.store('tok')
    api.fail('server_error', times=3)
    advance(e, 600)
    delays = []
    for n in range(1, 4):
        await sync(e, rid)
        row = await one(e.db.conn, 'SELECT state,fail_count,next_ms FROM ddns_records WHERE id=?', (rid,))
        assert row['state'] == 'pending' and row['fail_count'] == n
        delays.append((row['next_ms'] - e.clock[0]) // 1000)
        assert len(await alerts(e)) == (1 if n == 3 else 0)
        e.clock[0] = row['next_ms']
    assert delays == [30, 60, 120]
    assert (await e.ddns.overview(OWNER))['accounts'][0]['state'] == 'ok'
    await sync(e, rid)
    assert (await record(e, rid))['state'] == 'ok'
    assert [a['kind'] for a in await alerts(e)] == ['ddns_fail', 'ddns_recover']
    assert (await events(e, rid)).count('fail') == 1


async def test_retry_waits_are_capped_and_follow_retry_after(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    api = fakeprovider.store('tok')
    api.fail('rate_limited', retry_after=300)
    await sync(e, rid, 'manual')
    row = await one(e.db.conn, 'SELECT next_ms FROM ddns_records WHERE id=?', (rid,))
    assert row['next_ms'] - e.clock[0] == 300_000
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('UPDATE ddns_records SET fail_count=20 WHERE id=?', (rid,))
    api.fail('network')
    await sync(e, rid, 'manual')
    row = await one(e.db.conn, 'SELECT next_ms FROM ddns_records WHERE id=?', (rid,))
    assert row['next_ms'] - e.clock[0] == ddns_module.RETRY_MAX_S * 1000


async def test_failures_alert_after_ten_minutes_even_before_three(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    api = fakeprovider.store('tok')
    api.fail('server_error', times=2)
    await sync(e, rid, 'manual')
    advance(e, 600)
    await sync(e, rid, 'manual')
    assert [a['kind'] for a in await alerts(e)] == ['ddns_fail']


async def test_alerts_go_out_titled_for_dynamic_dns(env):  # noqa: F811
    e = env
    sent = []

    class Telegram:
        async def send_notice(self, root, **kwargs):
            sent.append(kwargs)
            return True
    e.server.web_task_telegram = Telegram()
    await e.servers.reminders._enqueue(OWNER, 'ddns-fail:r_x:1', '', 'ddns_fail', 'home 同步失败')
    await e.servers.reminders._enqueue(OWNER, 'test:1', '', 'test', '测试')
    await e.servers.reminders.deliver()
    by = {s['text']: s for s in sent}
    assert by['home 同步失败']['title'] == '🌐 动态域名' and by['home 同步失败']['link'] == ('/ddns', '打开动态域名')
    assert by['测试']['title'] == '🔔 服务器提醒' and by['测试']['link'] == ('/servers', '打开服务器')


async def test_credentials_never_leave_the_vault(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    token = 'bad-secret-token-123456'
    account = await add_account(e, token=token)
    assert account['state'] == 'error' and token not in json.dumps(account) and '***' in account['error']
    await e.ddns.update_account(OWNER, account['id'], {'credentials': {'token': 'tok-good-654321'}})
    account = (await e.ddns.overview(OWNER))['accounts'][0]
    assert account['state'] == 'ok' and account['secret']['name'] == 'ddns/Fake-主账号'
    secret = await one(e.db.conn, 'SELECT name,note,kv_json FROM memory_secrets WHERE id=?', (account['secret']['id'],))
    assert secret['note'] == '动态域名服务商凭据' and json.loads(secret['kv_json']) == [{'key': 'token', 'value': 'tok-good-654321'}]
    api = fakeprovider.store('tok-good-654321')
    rid = (await url_record(e, sid, account))['records'][0]['id']
    api.fail('auth_failed')
    api.errors[0].message = 'denied for tok-good-654321'
    await sync(e, rid, 'manual')
    dump = json.dumps([await e.ddns.overview(OWNER), await e.ddns.events(OWNER, rid), await alerts(e),
                       [dict(r) for r in await many(e.db.conn, 'SELECT * FROM ddns_accounts')],
                       [dict(r) for r in await many(e.db.conn, 'SELECT * FROM ddns_records')]], ensure_ascii=False)
    assert 'tok-good-654321' not in dump and token not in dump and 'denied for ***' in dump


async def test_zones_follow_the_instance_capabilities(env):  # noqa: F811
    e = env
    plain = await add_account(e, token='p1', kind='fakepush')
    zoned = (await e.ddns.create_account(OWNER, {'type': 'fakepush', 'alias': '有域', 'credentials': {'token': 'p2', 'zones': 'example.net'}}))['account']
    assert plain['zones'] == [] and plain['state'] == 'ok'
    assert zoned['zones'] == ['example.net'] and zoned['state'] == 'ok'
    assert (await e.ddns.check_account(OWNER, zoned['id']))['zones'] == ['example.net']
    # The hourly refresh asks only the instance that can list zones.
    advance(e, ddns_module.ZONES_S)
    await e.ddns.tick()
    await e.ddns.idle()
    assert [c[0] for c in fakeprovider.store('p1').calls] == ['check']
    assert [c[0] for c in fakeprovider.store('p2').calls] == ['check', 'check', 'check']
    # Names are matched to the account holding their zone.
    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['h.example.net'], 'source': {'method': 'url'}})
    assert preview['rows'][0]['accountId'] == zoned['id'] and preview['rows'][0]['zone'] == 'example.net'


async def test_events_are_kept_30_days_and_200_per_record(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    for n in range(ddns_module.EVENTS_PER_RECORD + 5):
        await e.ddns._event(OWNER, rid, 'check', f'第 {n} 条')
    rows = await many(e.db.conn, 'SELECT text FROM ddns_events WHERE record_id=? ORDER BY id', (rid,))
    assert len(rows) == ddns_module.EVENTS_PER_RECORD and rows[-1]['text'] == f'第 {ddns_module.EVENTS_PER_RECORD + 4} 条'
    advance(e, ddns_module.EVENTS_KEEP_S + 1)
    await e.ddns._event(OWNER, rid, 'check', '新的')
    await e.ddns.tick()
    await e.ddns.idle()
    texts = [r['text'] for r in await many(e.db.conn, 'SELECT text FROM ddns_events WHERE record_id=? ORDER BY id', (rid,))]
    assert texts[0] == '新的'


async def test_changing_the_line_moves_the_record_without_a_drift(env):  # noqa: F811
    """From the 10-30 check: the old line's record was left, and the new one taken for a deletion
    outside (or never written with onDrift=alert)."""
    from tests.ddns.harness import alerts, url_record
    for on_drift in ('revert', 'alert'):
        e = env
        sid = await add_node(e)
        e.nodes.connect(sid)
        account = await add_account(e)
        api = fakeprovider.store('tok')
        rid = (await url_record(e, sid, account, policy={'onDrift': on_drift}))['records'][0]['id']
        await e.ddns.update_record(OWNER, rid, {'options': {'line': 'telecom'}})
        await e.ddns.idle()
        assert [(r.value, r.extra.get('line')) for r in api.records.values() if r.name.startswith('home')] == [('198.51.100.7', 'telecom')]
        kinds = await events(e, rid)
        assert 'drift' not in kinds and 'recreated' not in kinds and 'line_left' in kinds
        assert not [a for a in await alerts(e) if '删除' in a['text']]
        assert (await record(e, rid))['state'] == 'ok'
        await e.ddns.delete_record(OWNER, rid, delete_remote=True)


async def test_a_line_chosen_goes_with_the_preview(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    await add_account(e)
    api = fakeprovider.store('tok')
    api.add('home.example.com', 'A', '192.0.2.10', line='telecom')
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['home.example.com'], 'types': ['A'], 'source': {'method': 'url'},
                                          'options': {'line': 'telecom'}})
    assert preview['rows'][0]['action'] == 'takeover' and preview['rows'][0]['remote'] == '192.0.2.10'
    assert (await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['home.example.com'], 'types': ['A'], 'source': {'method': 'url'}}))['rows'][0]['action'] == 'new'


async def test_a_type_the_provider_cannot_update_is_refused_before_it_is_saved(env):  # noqa: F811
    import httpx

    from app.ddns import providers
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = lambda p: {'results': [{'method': 'url', 'value': '198.51.100.7' if p['family'] == 'v4' else '2001:db8::7',
                                                 'error': '', 'detail': ''}], 'interfaces': []}

    def handler(req):
        return httpx.Response(200, text='<?xml version="1.0"?><interface-response><ErrCount>0</ErrCount></interface-response>')
    e.ddns._make = lambda kind, creds, settings: providers.get(kind)(creds, settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    acct = (await e.ddns.create_account(OWNER, {'type': 'namecheap', 'credentials': {'domain': 'example.com', 'password': 'pw'}}))['account']
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['home.example.com'], 'types': ['A', 'AAAA'], 'source': {'method': 'url'}})
    assert [(r['type'], r['action']) for r in preview['rows']] == [('A', 'new'), ('AAAA', 'error')]
    out = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': 'home.example.com', 'type': t, 'accountId': acct['id']} for t in ('A', 'AAAA')]})
    assert [r['type'] for r in out['records']] == ['A'] and [(s['type'], s['error']) for s in out['skipped']] == [('AAAA', 'type_unsupported')]
    await e.ddns.idle()
    assert (await e.ddns.account_view(OWNER, acct['id']))['state'] != 'error'
