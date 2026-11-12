"""The Servers tool's ddns_* actions: a smoke run through the registry, as the AI calls them."""
import json

from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.servers import ACTIONS, ServersTool, register_server_tools
from tests.ddns import fakeprovider
from tests.ddns.harness import add_node, base_env, env, probe_result  # noqa: F401

DDNS_ACTIONS = ['ddns_list', 'ddns_providers', 'ddns_account_add', 'ddns_account_update', 'ddns_account_check', 'ddns_account_delete',
                'ddns_preview', 'ddns_add', 'ddns_update', 'ddns_delete', 'ddns_sync', 'ddns_events', 'ddns_diagnose', 'ddns_import']


async def test_ddns_actions(env):  # noqa: F811
    e = env
    registry = ToolRegistry()
    register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-d',778,123,'t')")
    ctx = ToolRuntimeContext(chat_id=778, conversation_uuid='conv-d')

    async def call(action, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=ctx))

    assert all(a in ACTIONS for a in DDNS_ACTIONS)
    # Both describes fit the tool output cap, so neither reaches the AI cut short.
    described = await call('describe')
    assert 'ddns' in described['topics'] and set(DDNS_ACTIONS) <= set(described['actions'])
    topic = await call('describe', topic='ddns')
    assert topic['actions'] == DDNS_ACTIONS and set(topic['contracts']) == set(DDNS_ACTIONS) and 'confirmTakeover' in topic['guide']
    for topic in (None, 'ddns'):
        assert len(json.dumps(ServersTool.describe(topic), ensure_ascii=False)) < 32000
    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    providers = await call('ddns_providers')
    assert any(p['type'] == 'fake' and p['fields'] == ['token'] for p in providers['providers'])
    assert (await call('ddns_providers', type='fake'))['provider']['caps']['read'] is True
    checked = await call('ddns_account_check', type='fake', credentials={'token': 'tok'})
    assert checked['zones'] == ['example.com']
    account = (await call('ddns_account_add', type='fake', alias='AI', credentials={'token': 'tok'}))['account']
    assert (await call('ddns_account_update', accountId=account['id'], alias='AI 2'))['account']['alias'] == 'AI 2'
    assert (await call('ddns_account_check', accountId=account['id']))['zones'] == ['example.com']
    preview = await call('ddns_preview', serverId=sid, names=['ai.example.com'], source={'method': 'url'})
    assert preview['rows'][0]['action'] == 'new'
    # The node named inside source, as everywhere else, by its name too.
    by_name = await call('ddns_preview', names=['ai.example.com'], source={'kind': 'node', 'serverId': '家', 'method': 'url'})
    assert by_name['rows'][0]['action'] == 'new' and by_name['detected']['v4']['value'] == '198.51.100.7'
    added = await call('ddns_add', serverId=sid, source={'method': 'url'}, rows=[{'name': 'ai.example.com', 'type': 'A', 'accountId': account['id']}])
    rid = added['records'][0]['id']
    await e.ddns.idle()
    listing = await call('ddns_list')
    assert 'providers' not in listing and listing['records'][0]['state'] == 'ok'
    assert (await call('ddns_update', recordId=rid, ttl=600))['record']['ttl'] == 600
    assert (await call('ddns_sync', recordId=rid))['record']['state'] == 'ok'
    assert (await call('ddns_events', recordId=rid, limit=1))['events'][0]['kind']
    assert (await call('ddns_diagnose', serverId=sid, family='v4'))['recommend'] == 'url'
    yaml = 'dnsconf:\n  - ipv4: {enable: true, domains: [x.example.com]}\n    dns: {name: aliesa, id: a, secret: b}\n'
    assert (await call('ddns_import', yaml=yaml, serverId=sid))['unsupported']
    missing = await call('ddns_events', recordId='r_none')
    assert missing == {'ok': False, 'error': 'record_not_found', 'message': '找不到这条解析配置'}
    refused = await call('ddns_account_delete', accountId=account['id'])
    assert refused['error'] == 'account_in_use'
    assert (await call('ddns_delete', recordId=rid))['ok']
    assert (await call('ddns_account_delete', accountId=account['id']))['ok']
    assert 'tok' not in json.dumps(listing) and fakeprovider.store('tok').values('ai.example.com') == ['198.51.100.7']


async def test_a_command_preview_runs_only_where_servers_may_be_changed(env):  # noqa: F811
    """A Cron run not allowed to change servers previews a command source: the command would run on the node."""
    e = env
    registry = ToolRegistry()
    register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-d',778,123,'t')")
    person = ToolRuntimeContext(chat_id=778, conversation_uuid='conv-d')
    cron = ToolRuntimeContext(chat_id=778, conversation_uuid='conv-d', source='cron')

    async def call(ctx, action, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=ctx))

    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(cmd='198.51.100.7', url='198.51.100.7')
    await call(person, 'ddns_account_add', type='fake', credentials={'token': 'tok'})
    cmd = {'method': 'cmd', 'params': {'cmd': 'curl -s ip.sb'}}
    probed = lambda: [c[2]['methods'] for c in e.nodes.calls if c[1] == 'ddns.probe']  # noqa: E731
    refused = await call(cron, 'ddns_preview', serverId=sid, names=['ai.example.com'], source=cmd)
    assert refused['error'] == 'automatic_mutation_forbidden' and probed() == []
    assert (await call(cron, 'ddns_add', serverId=sid, source=cmd, rows=[]))['error'] == 'automatic_mutation_forbidden'
    # Other sources only read: the preview stays open to it.
    assert (await call(cron, 'ddns_preview', serverId=sid, names=['ai.example.com'], source={'method': 'url'}))['rows'][0]['action'] == 'new'
    assert probed() == [['url']]
    assert (await call(person, 'ddns_preview', serverId=sid, names=['ai.example.com'], source=cmd))['detected']['v4']['value'] == '198.51.100.7'
    assert probed()[-1] == ['cmd']


async def test_the_tool_preview_reads_the_line_it_is_given(env):  # noqa: F811
    e = env
    registry = ToolRegistry()
    register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-d',778,123,'t')")
    ctx = ToolRuntimeContext(chat_id=778, conversation_uuid='conv-d')

    async def call(action, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=ctx))

    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    await call('ddns_account_add', type='fake', credentials={'token': 'tok'})
    fakeprovider.store('tok').add('ai.example.com', 'A', '192.0.2.9', line='telecom')
    rows = {'serverId': sid, 'names': ['ai.example.com'], 'source': {'method': 'url'}}
    assert (await call('ddns_preview', **rows))['rows'][0]['action'] == 'new', 'nothing on the default line'
    row = (await call('ddns_preview', **rows, options={'line': 'telecom'}))['rows'][0]
    assert (row['action'], row['remote']) == ('takeover', '192.0.2.9')
    assert 'options' in ServersTool.describe('ddns_preview')['contracts']['ddns_preview']
