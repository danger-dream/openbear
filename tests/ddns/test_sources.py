"""Where desired values come from: probes sent to nodes, their reports, address filtering,
nodes going offline and the backup source, conn and auto sources."""
import asyncio

from app.ddns import service as ddns_module
from app.servers.hub import Session
from tests.ddns import fakeprovider
from tests.ddns.harness import (  # noqa: F401
    OWNER,
    add_account,
    add_node,
    advance,
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


async def test_probes_are_shared_and_sent_again_when_the_node_says_hello(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': n, 'type': 'A', 'accountId': account['id']} for n in ('a.example.com', 'b.example.com')]})
    await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url', 'params': {'intervalS': 30}}, 'rows': [
        {'name': 'c.example.com', 'type': 'A', 'accountId': account['id']}]})
    await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'nic'}, 'rows': [
        {'name': 'a.example.com', 'type': 'AAAA', 'accountId': account['id']}]})
    await e.ddns.idle()
    probes = e.nodes.watched(sid)[-1]
    assert sorted((p['family'], p['method'], p['intervalS']) for p in probes) == [('v4', 'url', 30), ('v6', 'nic', 15)]
    sent = len(e.nodes.watched(sid))
    # Unchanged probes are not sent again until the node reconnects; then after its hello they are.
    await e.ddns.push(sid)
    assert len(e.nodes.watched(sid)) == sent
    e.nodes.drop(sid)
    session = e.nodes.connect(sid)
    await e.servers.node_hello(sid, {'platform': 'linux'}, {'exec': True}, '1.0.0')
    await e.ddns.idle()
    assert len(e.nodes.watched(sid)) == sent + 1 and e.nodes.watched(sid)[-1] == probes
    # The node's report goes through the hub's message handling.
    v4 = next(p['key'] for p in probes if p['family'] == 'v4')
    await e.servers.hub._message(session, {'t': 'ddns', 'obs': [{'key': v4, 'value': '198.51.100.30', 'error': '', 'fails': 0}]})
    await e.ddns.idle()
    view = await e.ddns.overview(OWNER)
    assert sorted(r['desired'] for r in view['records'] if r['type'] == 'A') == ['198.51.100.30'] * 3
    # Pausing every record on it leaves the node with only what is left.
    for r in view['records']:
        if r['type'] == 'A':
            await e.ddns.update_record(OWNER, r['id'], {'enabled': False})
    assert [p['family'] for p in e.nodes.watched(sid)[-1]] == ['v6']


async def test_a_node_without_the_feature_is_outdated(env):  # noqa: F811
    e = env
    sid = await add_node(e, region='SG')
    e.nodes.connect(sid, features=())
    account = await add_account(e)
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': 'home.example.com', 'type': 'A', 'accountId': account['id']}]})
    await e.ddns.idle()
    rec = await record(e, result['records'][0]['id'])
    assert rec['state'] == 'node_outdated' and '更新节点' in rec['stateText'] and rec['desired'] == ''
    assert e.nodes.watched(sid) == []
    nodes = (await e.ddns.overview(OWNER))['nodes']
    assert nodes == [{'serverId': sid, 'name': '家', 'region': 'SG', 'online': True, 'platform': 'linux', 'ddns': False, 'exec': True,
                      'v4': '', 'v6': ''}]
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['x.example.com'], 'source': {'method': 'url'}})
    assert '节点版本过旧' in preview['detected']['v4']['notes'][0]['text']
    # Updated, it gets its probes after hello and the record converges.
    e.nodes.drop(sid)
    session = e.nodes.connect(sid)
    await e.servers.node_hello(sid, {'platform': 'linux'}, {'exec': True}, '1.1.0')
    await e.ddns.idle()
    key = key_of(e, sid)
    await e.servers.hub._message(session, {'t': 'ddns', 'obs': [{'key': key, 'value': '198.51.100.7'}]})
    await e.ddns.idle()
    rec = await record(e, rec['id'])
    assert rec['state'] == 'ok' and fakeprovider.store('tok').values('home.example.com') == ['198.51.100.7']


async def test_region_comes_from_the_geo_lookup_without_a_profile(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    nodes = (await e.ddns.overview(OWNER))['nodes']
    assert nodes[0]['region'] == 'JP' and nodes[0]['online'] is False and nodes[0]['serverId'] == sid


async def test_private_addresses_are_refused_unless_allowed(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    key = key_of(e, sid)
    api = fakeprovider.store('tok')
    for value in ('10.0.0.5', '100.64.1.1', '169.254.3.3', '224.0.0.9'):
        await report(e, sid, key, value)
        rec = await record(e, rid)
        assert rec['state'] == 'no_address' and value in rec['stateText'] and rec['desired'] == '198.51.100.7'
    assert api.values('home.example.com') == ['198.51.100.7']
    await e.ddns.update_record(OWNER, rid, {'policy': {'allowPrivate': True}})
    await e.ddns.idle()
    await report(e, sid, key, '10.0.0.5')
    rec = await record(e, rid)
    assert rec['state'] == 'ok' and api.values('home.example.com') == ['10.0.0.5']
    advance(e, ddns_module.DETECT_CACHE_S)
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'names': ['y.example.com'], 'source': {'method': 'url'}})
    assert '10.0.0.5' in preview['detected']['v4']['notes'][0]['text']


async def test_a_failing_probe_keeps_dns_as_it_is(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    key = key_of(e, sid)
    await report(e, sid, key, '198.51.100.7')
    await report(e, sid, key, '', error='timeout', fails=3)
    rec = await record(e, rid)
    assert rec['desired'] == '198.51.100.7' and rec['state'] == 'ok'
    assert (await events(e, rid))[-1] == 'address'
    assert '取 IP 失败（超时）' in (await e.ddns.events(OWNER, rid))['events'][0]['text']
    await report(e, sid, key, '198.51.100.8')
    assert (await record(e, rid))['desired'] == '198.51.100.8'
    # A probe that has seen nothing since the node connected: DNS stays, the record says why.
    e.nodes.drop(sid)
    e.nodes.connect(sid)
    e.ddns.obs.clear()
    await e.ddns.push(sid)
    await report(e, sid, key, '', error='iface_not_found', fails=3)
    rec = await record(e, rid)
    assert rec['state'] == 'no_address' and '找不到网卡' in rec['stateText'] and rec['desired'] == '198.51.100.8'


async def test_offline_source_keeps_dns_and_backup_takes_over(env):  # noqa: F811
    e = env
    home, office = await add_node(e, '家'), await add_node(e, '公司')
    e.nodes.connect(home)
    e.nodes.connect(office)
    account = await add_account(e)
    e.nodes.probe[home] = probe_result(url='198.51.100.7')
    result = await e.ddns.create_records(OWNER, {'serverId': home, 'source': {'method': 'url'}, 'backup': {'serverId': office, 'afterS': 300},
                                                 'rows': [{'name': 'home.example.com', 'type': 'A', 'accountId': account['id']}]})
    await e.ddns.idle()
    rid = result['records'][0]['id']
    assert result['records'][0]['backup'] == {'serverId': office, 'afterS': 300}
    key = key_of(e, home)
    assert key_of(e, office) == key
    await report(e, office, key, '198.51.100.50')
    api = fakeprovider.store('tok')
    e.nodes.drop(home)
    advance(e, 60)
    await e.ddns.tick()
    await e.ddns.idle()
    assert (await record(e, rid))['state'] == 'ok'
    advance(e, 61)
    await e.ddns.tick()
    await e.ddns.idle()
    rec = await record(e, rid)
    assert rec['state'] == 'offline' and rec['stateText'] == '来源节点离线，DNS 保持不变' and api.values('home.example.com') == ['198.51.100.7']
    advance(e, 180)
    await e.ddns.tick()
    await e.ddns.idle()
    rec = await record(e, rid)
    assert rec['activeSource'] == 'backup' and rec['desired'] == '198.51.100.50' and rec['state'] == 'ok'
    assert api.values('home.example.com') == ['198.51.100.50']
    # Back: the primary's report wins again.
    session = e.nodes.connect(home)
    await e.servers.node_hello(home, {'platform': 'linux'}, {}, '1.0.0')
    await e.ddns.idle()
    await e.servers.hub._message(session, {'t': 'ddns', 'obs': [{'key': key, 'value': '198.51.100.8'}]})
    await e.ddns.idle()
    rec = await record(e, rid)
    assert rec['activeSource'] == 'primary' and rec['desired'] == '198.51.100.8' and api.values('home.example.com') == ['198.51.100.8']
    kinds = await events(e, rid)
    assert kinds.index('offline') < kinds.index('failover') < kinds.index('failback')


async def test_offline_without_backup_comes_back_online(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    e.nodes.drop(sid)
    advance(e, ddns_module.OFFLINE_S)
    await e.ddns.tick()
    await e.ddns.idle()
    assert (await record(e, rid))['state'] == 'offline'
    writes = len(fakeprovider.store('tok').writes())
    advance(e, 3600)
    await e.ddns.tick()
    await e.ddns.idle()
    assert len(fakeprovider.store('tok').writes()) == writes
    e.nodes.connect(sid)
    await e.servers.node_hello(sid, {'platform': 'linux'}, {}, '1.0.0')
    await e.ddns.idle()
    await report(e, sid, key_of(e, sid), '198.51.100.7')
    rec = await record(e, rid)
    assert rec['state'] == 'ok'
    kinds = await events(e, rid)
    assert kinds.count('offline') == 1 and kinds.index('offline') < kinds.index('online')


async def test_conn_source_uses_the_connection_address(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    session = e.nodes.connect(sid, remote='203.0.113.77')
    account = await add_account(e)
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'conn'}, 'rows': [
        {'name': 'home.example.com', 'type': 'A', 'accountId': account['id']}]})
    await e.ddns.idle()
    rid = result['records'][0]['id']
    rec = await record(e, rid)
    assert rec['desired'] == '203.0.113.77' and rec['state'] == 'ok' and rec['source']['resolvedMethod'] == 'conn'
    assert e.nodes.watched(sid) == [] or e.nodes.watched(sid)[-1] == []
    session.forwarded = True
    await e.ddns.tick()
    rec = await record(e, rid)
    assert rec['state'] == 'conn_unavailable' and '反向代理' in rec['stateText']
    e.servers.hub.sessions[sid] = Session(sid, None, None, '10.1.1.1')
    e.servers.hub.sessions[sid].features = frozenset({'ddns'})
    await e.ddns.tick()
    rec = await record(e, rid)
    assert rec['state'] == 'conn_unavailable' and '10.1.1.1' in rec['stateText']


async def test_auto_method_is_chosen_by_diagnosis(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7', nic='', stun='198.51.100.99')
    diag = await e.ddns.diagnose(OWNER, sid, 'v4')
    assert diag['recommend'] == 'stun' and diag['interfaces'][0]['name'] == 'eth0' and diag['notes']
    nic = next(r for r in diag['results'] if r['method'] == 'nic')
    assert (nic['error'], nic['errorCode']) == ('没有地址', 'no_address'), 'the page shows the words, the code stays'
    probe = next(c for c in e.nodes.calls if c[1] == 'ddns.probe')[2]
    assert probe['family'] == 'v4' and probe['methods'] == ['url', 'nic', 'stun']
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'rows': [{'name': 'home.example.com', 'type': 'A', 'accountId': account['id']}]})
    await e.ddns.idle()
    rec = await record(e, result['records'][0]['id'])
    assert rec['source']['method'] == 'auto' and rec['source']['methodAuto'] is True and rec['source']['resolvedMethod'] == 'stun'
    assert rec['desired'] == '198.51.100.99' and e.nodes.watched(sid)[-1][0]['method'] == 'stun'
    e.nodes.probe[sid] = probe_result(url='198.51.100.7', nic='198.51.100.7')
    assert (await e.ddns.diagnose(OWNER, sid, 'v4'))['recommend'] == 'nic'
    e.nodes.probe[sid] = probe_result(url='198.51.100.7', stun='198.51.100.7')
    assert (await e.ddns.diagnose(OWNER, sid, 'v4'))['recommend'] == 'url'
    assert (await e.ddns.diagnose(OWNER, sid, 'v6'))['recommend'] == 'nic'


async def test_auto_behind_a_proxy_that_splits_china_and_abroad(env):  # noqa: F811
    from app.ddns import model
    from tests.ddns.harness import T0
    e = env
    line, vps = '198.51.100.17', '203.0.113.92'

    def answer(params):
        if params['urls'] == model.URL_GROUPS['cn']['v4']:
            return probe_result(url=line, nic='', stun=vps)
        return probe_result(url=vps)
    sid, abroad = await add_node(e, '公司'), await add_node(e, '公司 SG', region='SG')
    for s in (sid, abroad):
        e.nodes.connect(s)
        e.nodes.probe[s] = answer
    account = await add_account(e)
    diag = await e.ddns.diagnose(OWNER, sid, 'v4')
    assert [r['method'] for r in diag['results']] == ['url_cn', 'url_intl', 'nic', 'stun'] and diag['recommend'] == 'url_cn'
    urls = [c[2]['urls'] for c in e.nodes.calls if c[1] == 'ddns.probe']
    assert urls == [model.URL_GROUPS['cn']['v4'], model.URL_GROUPS['intl']['v4']]
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'rows': [{'name': 'office.example.com', 'type': 'A', 'accountId': account['id']}]})
    await e.ddns.idle()
    rec = await record(e, result['records'][0]['id'])
    assert rec['source']['resolvedMethod'] == 'url_cn' and rec['desired'] == line and rec['state'] == 'ok'
    probe = e.nodes.watched(sid)[-1][0]
    assert probe['method'] == 'url' and probe['urls'] == model.URL_GROUPS['cn']['v4']
    assert fakeprovider.store('tok').values('office.example.com') == [line]
    # The region the user gave: abroad, so the services there.
    assert (await e.ddns.diagnose(OWNER, abroad, 'v4'))['recommend'] == 'url_intl'
    # A mesh member on a punched link sees the node at the line: that wins over the region.
    peer = {'id': abroad, 'remoteAddr': f'{line}:51998', 'direct': {'state': 'direct', 'via': 'public'}}
    e.servers.mesh.status['home'] = {'status': {'peers': [peer]}, 'at': T0}
    # Only a member the applied configuration links to the node directly is believed.
    configs = {'home': {'links': [{'kind': 'relay', 'peer': abroad}]}, 'other': {'links': [{'kind': 'direct', 'peer': 'home'}]}}

    async def applied(owner):
        return {'configs': configs}
    e.servers.mesh._applied = applied
    assert (await e.ddns.diagnose(OWNER, abroad, 'v4'))['recommend'] == 'url_intl'
    configs['home']['links'].append({'kind': 'direct', 'peer': abroad})
    diag = await e.ddns.diagnose(OWNER, abroad, 'v4')
    assert diag['recommend'] == 'url_cn' and any('组网' in n['text'] for n in diag['notes'])
    # Too old a report, or a link that is not direct, says nothing.
    e.servers.mesh.status['home']['at'] = T0 - (ddns_module.MESH_SEEN_S + 1) * 1000
    assert (await e.ddns.diagnose(OWNER, abroad, 'v4'))['recommend'] == 'url_intl'
    e.servers.mesh.status['home'] = {'status': {'peers': [{**peer, 'direct': {'state': 'punching'}}]}, 'at': T0}
    assert (await e.ddns.diagnose(OWNER, abroad, 'v4'))['recommend'] == 'url_intl'


async def test_a_node_behind_a_proxy_reports_its_line_and_the_exit(env):  # noqa: F811
    e = env
    sid, abroad = await add_node(e, '公司'), await add_node(e, '海外', region='US')
    line, vps = '1.0.0.34', '1.0.0.92'

    async def hello(server_id, **info):
        session = e.servers.hub.sessions.get(server_id) or e.nodes.connect(server_id)
        session.info = {'platform': 'linux', **info}
        await e.servers.node_hello(server_id, session.info, {'exec': True}, 'test')
        await e.ddns.idle()
        node = next(n for n in await e.ddns.nodes(OWNER) if n['serverId'] == server_id)
        return node['v4'], session.info.get('ipv4Proxy', '')
    assert await hello(sid, ipv4=line, ipv4Proxy=vps) == (line, vps)
    # A node just started says hello before looking its addresses up: both are kept.
    assert await hello(sid) == (line, vps)
    # The proxy gone: so is its exit.
    assert await hello(sid, ipv4=line) == (line, '')
    # A server the user puts outside China: the address services abroad see is its own.
    assert await hello(abroad, ipv4=line, ipv4Proxy=vps) == (vps, line)
    assert await hello(abroad) == (vps, line), 'kept as they were, not swapped back'


async def test_rate_limit_per_account(env):  # noqa: F811
    e = env
    account = await add_account(e)
    running, peak = [0], [0]

    async def op():
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0)
        running[0] -= 1
        return e.mono[0]
    started = await asyncio.gather(*(e.ddns._call(account['id'], op) for _ in range(10)))
    assert peak[0] <= 2
    by_second = {}
    for at in started:
        by_second[int(at)] = by_second.get(int(at), 0) + 1
    assert max(by_second.values()) <= 4 and e.slept


async def test_ula_and_private_reports_are_filtered_here(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    e.nodes.probe[sid] = probe_result(url='2400:cb00::7')
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': 'six.example.com', 'type': 'AAAA', 'accountId': account['id']}]})
    await e.ddns.idle()
    rid = result['records'][0]['id']
    key = key_of(e, sid)
    await report(e, sid, key, 'fd00::5')
    rec = await record(e, rid)
    assert rec['state'] == 'no_address' and rec['desired'] == '2400:cb00::7'
    assert fakeprovider.store('tok').values('six.example.com', 'AAAA') == ['2400:cb00::7']


async def test_a_node_runs_at_most_64_probes(env, monkeypatch):  # noqa: F811
    e = env
    from app.ddns import model
    monkeypatch.setattr(model, 'MAX_PROBES', 2)
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7', stun='198.51.100.7', nic='198.51.100.7')
    for n, method in enumerate(('url', 'stun', 'nic')):
        result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': method}, 'rows': [
            {'name': f'p{n}.example.com', 'type': 'A', 'accountId': account['id']}]})
    assert result['records'] == [] and result['skipped'][0]['error'] == 'too_many_probes'
    # Sharing a probe already there is fine.
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': 'p9.example.com', 'type': 'A', 'accountId': account['id']}]})
    await e.ddns.idle()
    assert len(result['records']) == 1 and len(e.nodes.watched(sid)[-1]) == 2
