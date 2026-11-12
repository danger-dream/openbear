"""Extension tool groups: per-conversation tool lists, folder defaults and the model's request to turn one on."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.extensions import ExtensionService, catalog
from app.llm.events import StreamEvent, ToolCall
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.extensions import register_extension_tool, request_extension
from app.user_interactions import InteractionService
from app.web_console.live_stream import _WebLiveStream, _WebStreamRenderer
from tests.test_builtin_template_import import _login_cookie
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, web_env  # noqa: F401


@pytest.fixture
async def env(web_env):  # noqa: F811 - imported pytest fixture
    e = web_env
    await _login_cookie(e)
    e.server.model_selection = SimpleNamespace(current='openai/gpt')
    e.server.tools = ToolRegistry()
    e.calls = []
    for name in ('Read', 'Browser', 'Cron', 'Webhook', 'mcp__parrot__web_search', 'Agent', 'ConversationRequest'):
        async def handler(args, name=name):
            e.calls.append(name)
            return f'{name} done'
        e.server.tools.add(name, f'{name} tool', {'type': 'object', 'properties': {}}, handler)
    e.server.extensions = ExtensionService(e.db, e.server)
    e.server.extensions.names = lambda: e.server.tools.names(scope='main')
    register_extension_tool(e.server.tools, e.server.extensions)
    await e.db.conn.execute('INSERT INTO memory_templates(name,content,is_active,updated_at) VALUES (?,?,1,1)', ('global', 'GLOBAL'))
    await e.db.conn.commit()
    return e


async def request(e, method, url, body=None, status=200):
    response = await getattr(e.client, method)(url, **({'json': body} if body is not None else {}))
    assert response.status == status, await response.text()
    return await response.json()


async def folder(e, name, parent=''):
    return (await request(e, 'post', '/api/conversation-folders', {'name': name, 'parentId': parent}))['folder']['folderId']


async def created(e, title, fid=''):
    await request(e, 'post', '/api/conversations', {'title': title, 'folderId': fid})
    cur = await e.db.conn.execute('SELECT * FROM web_conversations WHERE title=?', (title,))
    return dict(await cur.fetchone())


async def run(e, row, scripts, turn='turn-1'):
    backend = FakeStreamBackend(scripts)
    backend.protocol = 'chat'
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    live = _WebLiveStream(row['conversation_uuid'], row['internal_chat_id'])
    await live.publish({'type': 'accepted', 'turnUuid': turn})
    assert await e.server._run_web_turn(row['internal_chat_id'], 'go', _WebStreamRenderer(live), conversation=row, root_turn_uuid=turn)
    return backend


def names(tools):
    return sorted(t.get('name') or t['function']['name'] for t in tools)


def call(name, args, call_id='c1'):
    return [StreamEvent(kind='tool_call', tool_calls=[ToolCall(call_id, name, json.dumps(args))]), StreamEvent(kind='finish', finish_reason='tool_calls')]


DONE = [StreamEvent(kind='content', text='ok'), StreamEvent(kind='finish', finish_reason='stop')]


def test_catalog_groups_builtin_tools_and_each_mcp_server():
    found = {e.key: e.tools for e in catalog(['Read', 'Browser', 'Cron', 'mcp__parrot__web_search', 'mcp__parrot__web_fetch', 'mcp__other__x'])}
    assert found == {'browser': ('Browser',), 'automation': ('Cron', 'Webhook'),
                     'mcp:parrot': ('mcp__parrot__web_search', 'mcp__parrot__web_fetch'), 'mcp:other': ('mcp__other__x',)}


async def test_new_conversations_take_the_nearest_folder_default_and_old_ones_keep_everything(env):
    e = env
    legacy = await e.server._create_web_conversation(123, title='legacy')
    assert all(x['enabled'] for x in (await request(e, 'get', f"/api/conversations/{legacy['conversation_uuid']}/extensions"))['extensions'])
    parent = await folder(e, 'parent')
    child = await folder(e, 'child', parent)
    saved = await request(e, 'put', f'/api/conversation-folders/{parent}/extensions', {'enabled': ['browser'], 'autoAllow': True})
    assert saved['default']['own'] and saved['default']['enabled'] == ['browser']
    inherited = await request(e, 'get', f'/api/conversation-folders/{child}/extensions')
    assert inherited['default']['from'] == parent and not inherited['default']['own']
    # Only a folder with a folder above it can inherit; a top-level one and the temporary node have their own setting only.
    assert inherited['canInherit'] is True and saved['canInherit'] is False
    assert (await request(e, 'get', '/api/conversation-folders/__temporary/extensions'))['canInherit'] is False
    row = await created(e, 'in child', child)
    state = await e.server.extensions.state(row['conversation_uuid'])
    assert state.enabled == {'browser'} and state.auto_allow
    plain = await created(e, 'no default', await folder(e, 'plain'))
    assert (await e.server.extensions.state(plain['conversation_uuid'])).enabled == frozenset()
    await request(e, 'put', f'/api/conversation-folders/{parent}/extensions', {'inherit': True})
    assert not (await request(e, 'get', f'/api/conversation-folders/{child}/extensions'))['default']['explicit']
    await request(e, 'put', f'/api/conversation-folders/{child}/extensions', {'enabled': ['nope']}, status=400)


async def test_tools_follow_the_extensions_from_the_next_request(env):
    e = env
    row = await created(e, 'auto')
    await request(e, 'put', f"/api/conversations/{row['conversation_uuid']}/extensions", {'autoAllow': True})
    backend = await run(e, row, [call('RequestExtension', {'extension': 'browser', 'reason': 'open the page'}), call('Browser', {}, 'c2'), DONE])
    assert names(backend.seen_tools[0]) == ['Read', 'RequestExtension']
    assert names(backend.seen_tools[1]) == ['Browser', 'Read', 'RequestExtension']
    assert e.calls == ['Browser']
    cur = await e.db.conn.execute("SELECT payload_json FROM web_operations WHERE conversation_uuid=? AND op_type='notice'", (row['conversation_uuid'],))
    notices = [json.loads(r['payload_json'])['text'] for r in await cur.fetchall()]
    assert any('开启浏览器' in text and '自动允许' in text for text in notices), notices


async def test_a_tool_of_an_extension_that_is_off_is_refused(env):
    e = env
    row = await created(e, 'off')
    backend = await run(e, row, [call('Cron', {}), DONE])
    assert e.calls == []
    result = next(m for m in backend.seen_convos[1] if m.get('role') == 'tool')
    assert 'extension_not_enabled' in str(result['content']) and 'automation' in str(result['content'])


async def test_the_model_asks_the_user_unless_the_conversation_allows_it(env):
    e = env
    row = await created(e, 'ask')
    asked = []

    async def confirm(payload):
        asked.append(payload)
        return answers.pop(0)
    answers = [{'confirmed': False}, {'confirmed': True, 'decision': 'confirm'}]
    ctx = ToolRuntimeContext(conversation_uuid=row['conversation_uuid'], web_confirm=confirm)
    service = e.server.extensions
    declined = await request_extension(service, {'extension': 'browser', 'reason': 'check a page'}, ctx)
    assert declined['status'] == 'declined' and 'browser' not in (await service.state(row['conversation_uuid'])).enabled
    enabled = await request_extension(service, {'extension': 'browser', 'reason': 'check a page'}, ctx)
    assert enabled['status'] == 'enabled' and asked[0]['title'] == '开启扩展：浏览器' and asked[0]['_requiresAuthorization']
    assert (await request_extension(service, {'extension': 'browser', 'reason': 'x'}, ctx))['status'] == 'already_enabled'
    unattended = ToolRuntimeContext(conversation_uuid=row['conversation_uuid'])
    with pytest.raises(Exception) as caught:
        await request_extension(service, {'extension': 'mcp:parrot', 'reason': 'search'}, unattended)
    assert caught.value.payload['error'] == 'user_confirmation_unavailable'
    with pytest.raises(Exception) as caught:
        await request_extension(service, {'extension': 'nope', 'reason': 'x'}, ctx)
    assert caught.value.payload['error'] == 'unknown_extension'


async def test_an_approval_with_conditions_turns_nothing_on_until_the_call_comes_again_with_its_receipt(env):
    e = env
    row = await created(e, 'amended')
    interactions = InteractionService(e.db)
    await interactions.start()
    shown = []

    async def answer(event, item):
        if event == 'created':
            shown.append(item)
            assert (await interactions.submit(item['interactionId'], 123, {'decision': 'confirm_with_changes', 'text': '只用来看这一页'}))['ok']
    interactions.add_listener(answer)
    ctx = ToolRuntimeContext(chat_id=123, source='web', conversation_uuid=row['conversation_uuid'], turn_uuid='turn')

    async def confirm(payload):
        return await interactions.request(payload, owner_chat_id=123, conversation_uuid=ctx.conversation_uuid, turn_uuid=ctx.turn_uuid)
    ctx.web_confirm = confirm
    try:
        args = {'extension': 'browser', 'reason': 'check a page'}
        first = json.loads(await e.server.tools.dispatch('RequestExtension', json.dumps(args), context=ctx))
        assert first['status'] == 'amendments_required' and first['confirmation']['text'] == '只用来看这一页'
        assert 'browser' not in (await e.server.extensions.state(row['conversation_uuid'])).enabled
        other = json.loads(await e.server.tools.dispatch('RequestExtension', json.dumps(
            {'extension': 'automation', 'reason': 'x', '_openbearApprovalId': first['approvalId']}), context=ctx))
        assert other['status'] == 'declined' and 'automation' not in (await e.server.extensions.state(row['conversation_uuid'])).enabled
        again = json.loads(await e.server.tools.dispatch('RequestExtension', json.dumps(
            {**args, '_openbearApprovalId': first['approvalId']}), context=ctx))
        assert again['status'] == 'enabled' and again['appliedConditions'] == '只用来看这一页' and len(shown) == 1
        assert 'browser' in (await e.server.extensions.state(row['conversation_uuid'])).enabled
    finally:
        await interactions.stop()


async def test_concurrent_updates_keep_each_change(env):
    e = env
    row = await created(e, 'concurrent')
    service = e.server.extensions
    await service.update(row['conversation_uuid'], enable=['browser'])
    gate = asyncio.Event()

    # Turning browser off waits here, after it has read the stored set and before it saves.
    async def in_use(conversation):
        await gate.wait()
        return ''
    service.in_use['browser'] = in_use
    turn_off = asyncio.create_task(service.update(row['conversation_uuid'], disable=['browser']))
    await asyncio.sleep(0.05)
    turn_on = asyncio.create_task(service.update(row['conversation_uuid'], enable=['automation']))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(turn_off, turn_on)
    assert (await service.state(row['conversation_uuid'])).enabled == {'automation'}


async def test_an_extension_in_use_cannot_be_turned_off(env):
    e = env
    row = await created(e, 'busy')
    await request(e, 'put', f"/api/conversations/{row['conversation_uuid']}/extensions", {'enable': ['browser']})

    async def busy(conversation):
        return 'a page is being driven'
    e.server.extensions.in_use['browser'] = busy
    refused = await request(e, 'put', f"/api/conversations/{row['conversation_uuid']}/extensions", {'disable': ['browser']}, status=409)
    assert refused['error'] == 'extension_in_use'
    assert 'browser' in (await e.server.extensions.state(row['conversation_uuid'])).enabled


async def test_unattended_runs_use_the_declared_set_then_an_explicit_folder_default_then_everything(env):
    e = env
    service = e.server.extensions
    fid = await folder(e, 'events')
    assert await service.for_unattended(123, fid, None) is None
    assert await service.for_unattended(123, fid, ['browser']) == {'enabled': ['browser'], 'autoAllow': False}
    await service.set_folder_default(123, fid, {'enabled': ['automation'], 'autoAllow': True})
    assert await service.for_unattended(123, fid, None) == {'enabled': ['automation'], 'autoAllow': True}
    row = await created(e, 'source', fid)
    assert await service.copy_of(row['conversation_uuid']) == {'enabled': ['automation'], 'autoAllow': True}


def test_usage_blocks_leave_the_system_prompt_and_join_the_first_tool_of_their_extension():
    from app.extensions import split_usage, with_usage
    system = ('BASE\n<!-- extension: workitems -->\n### Work items\nrecord work\n<!-- /extension -->\nMIDDLE\n'
              '<!-- extension: mcp -->\nshared mcp\n<!-- /extension -->\n<!-- extension: mcp:parrot -->\nparrot only\n<!-- /extension -->\nEND\n')
    base, usage = split_usage(system)
    assert base == 'BASE\nMIDDLE\nEND\n'
    assert usage == {'workitems': '### Work items\nrecord work', 'mcp': 'shared mcp', 'mcp:parrot': 'parrot only'}
    tools = [{'name': 'Read', 'description': 'r'}, {'name': 'WorkItem', 'description': 'w'},
             {'name': 'mcp__parrot__web_search', 'description': 's'}, {'name': 'mcp__parrot__web_fetch', 'description': 'f'}]
    out = {t['name']: t['description'] for t in with_usage(tools, usage)}
    assert out['Read'] == 'r' and out['WorkItem'].endswith('(workitems):\n### Work items\nrecord work')
    assert out['mcp__parrot__web_search'].endswith('(mcp:parrot):\nshared mcp\n\nparrot only') and out['mcp__parrot__web_fetch'] == 'f'
    assert with_usage(tools[:1], usage) == tools[:1]


async def test_a_run_sends_extension_usage_only_with_the_tools_that_are_on(env):
    e = env
    legacy = await created(e, 'legacy-template')
    legacy_backend = await run(e, legacy, [DONE])
    legacy_described = {t.get('name') or t['function']['name']: t.get('description') or t['function']['description']
                        for t in legacy_backend.seen_tools[0]}
    assert 'Available extensions' in legacy_described['RequestExtension']
    assert '- browser (浏览器):' in legacy_described['RequestExtension']
    assert '- mcp:parrot (parrot):' in legacy_described['RequestExtension']

    await e.db.conn.execute('UPDATE memory_templates SET content=? WHERE is_active=1', (
        'GLOBAL\n<!-- extension: browser -->\nBROWSER USAGE\n<!-- /extension -->\n<!-- extension: automation -->\nCRON USAGE\n<!-- /extension -->\n',))
    await e.db.conn.commit()
    row = await created(e, 'usage')
    await request(e, 'put', f"/api/conversations/{row['conversation_uuid']}/extensions", {'enable': ['browser']})
    backend = await run(e, row, [DONE])
    assert 'USAGE' not in backend.seen_systems[0] and backend.seen_systems[0].startswith('GLOBAL')
    described = {t.get('name') or t['function']['name']: t.get('description') or t['function']['description'] for t in backend.seen_tools[0]}
    assert 'BROWSER USAGE' in described['Browser'] and 'Cron' not in described
    assert 'Available extensions' not in described['RequestExtension']
    assert all('CRON USAGE' not in d for d in described.values())


async def test_a_new_conversation_can_be_created_with_the_extensions_chosen_in_its_draft(env):
    e = env
    fid = await folder(e, 'chosen')
    await request(e, 'put', f'/api/conversation-folders/{fid}/extensions', {'enabled': ['automation'], 'autoAllow': False})
    await request(e, 'post', '/api/conversations', {'title': 'chosen', 'folderId': fid, 'extensions': {'enabled': ['browser'], 'autoAllow': True}})
    cur = await e.db.conn.execute("SELECT conversation_uuid FROM web_conversations WHERE title='chosen'")
    state = await e.server.extensions.state((await cur.fetchone())['conversation_uuid'])
    assert state.enabled == {'browser'} and state.auto_allow
    # An extension gone since the draft was made does not stop the first message; a malformed choice is refused.
    await request(e, 'post', '/api/conversations', {'title': 'gone', 'extensions': {'enabled': ['mcp:removed', 'browser']}})
    cur = await e.db.conn.execute("SELECT conversation_uuid FROM web_conversations WHERE title='gone'")
    assert (await e.server.extensions.state((await cur.fetchone())['conversation_uuid'])).enabled == {'browser'}
    await request(e, 'post', '/api/conversations', {'title': 'bad', 'extensions': {'enabled': 'browser'}}, status=400)
    cur = await e.db.conn.execute("SELECT count(*) n FROM web_conversations WHERE title='bad'")
    assert (await cur.fetchone())['n'] == 0
    panel = await request(e, 'get', f'/api/conversation-folders/{fid}/extensions')
    assert {x['key']: x['enabled'] for x in panel['extensions']}['automation'] is True and all('tokens' in x for x in panel['extensions'])


@pytest.mark.parametrize('origin', ['cron', 'webhook'])
async def test_a_scheduled_or_event_run_never_asks_for_an_extension(env, origin):
    e = env
    row = await created(e, f'unattended {origin}')
    asked = []

    async def confirm(payload):
        asked.append(payload)
        return {'confirmed': True}
    # The contexts chat_runtime builds for these runs: their confirmation channel exists but nobody watches it.
    ctx = ToolRuntimeContext(conversation_uuid=row['conversation_uuid'], web_confirm=confirm, source=origin,
                             webhook_automatic=origin == 'webhook', webhook_assignment_id='assignment-1' if origin == 'webhook' else '')
    with pytest.raises(Exception) as caught:
        await request_extension(e.server.extensions, {'extension': 'browser', 'reason': 'x'}, ctx)
    assert caught.value.payload['error'] == 'user_confirmation_unavailable' and asked == []
    await e.server.extensions.update(row['conversation_uuid'], auto_allow=True)
    assert (await request_extension(e.server.extensions, {'extension': 'browser', 'reason': 'x'}, ctx))['approvedBy'] == 'auto_allow'


async def notices(e, row):
    cur = await e.db.conn.execute("SELECT payload_json FROM web_operations WHERE conversation_uuid=? AND op_type='notice' ORDER BY id", (row['conversation_uuid'],))
    return [json.loads(r['payload_json'])['text'] for r in await cur.fetchall()]


async def test_conversations_comes_with_agents_and_goes_with_them(env):
    e = env
    service = e.server.extensions
    row = await created(e, 'collab')
    await service.update(row['conversation_uuid'], enable=['conversations'])
    assert {'conversations', 'agents'} <= (await service.state(row['conversation_uuid'])).enabled
    await service.update(row['conversation_uuid'], disable=['agents'])
    assert not {'conversations', 'agents'} & (await service.state(row['conversation_uuid'])).enabled
    assert (await notices(e, row))[-2:] == ['扩展：开启Agent 协作、开启会话协作', '扩展：关闭Agent 协作、关闭会话协作']
    # A draft choice and a folder default are stored the same way.
    assert service.chosen({'enabled': ['conversations']})['enabled'] == ['agents', 'conversations']
    fid = await folder(e, 'collab folder')
    assert (await service.set_folder_default(123, fid, {'enabled': ['conversations']}))['enabled'] == ['agents', 'conversations']
    panel = {x['key']: x for x in service.panel(set())}
    assert panel['conversations']['requires'] == ['agents'] and panel['agents']['requires'] == []


async def test_agents_cannot_be_turned_off_while_a_conversation_request_awaits_its_reply(env):
    from types import SimpleNamespace
    from app.services import Services
    e = env
    row = await created(e, 'sender')
    host = SimpleNamespace(db=e.db, agent_dao=SimpleNamespace(active_tasks_for_chat=lambda chat_id, **kwargs: _none()))
    assert await Services._agents_in_use(host, row) == ''
    await e.db.conn.execute(
        "INSERT INTO collab_requests(request_uuid, owner_chat_id, from_conversation_uuid, to_conversation_uuid, title, body, state, created_at_ms, updated_at_ms) "
        "VALUES('req-1', 123, ?, 'other', 't', 'b', 'working', 1, 1)", (row['conversation_uuid'],))
    await e.db.conn.commit()
    assert '会话请求' in await Services._agents_in_use(host, row)


async def _none():
    return []


async def test_an_old_conversation_records_only_what_changed(env):
    e = env
    old = await e.server._create_web_conversation(123, title='old')
    await e.server.extensions.update(old['conversation_uuid'], disable=['browser'])
    assert (await notices(e, old))[-1] == '扩展：关闭浏览器'


async def test_a_conversation_whose_prompt_policy_lists_its_tools_is_not_filtered_by_extensions(env):
    e = env
    row = await created(e, 'policy')
    await request(e, 'put', f"/api/conversations/{row['conversation_uuid']}/properties",
                  {'contextText': 'CUSTOM', 'overrideSystemPrompt': True, 'toolsEnabled': True, 'toolNames': ['Read', 'Browser'], 'updateSnapshots': True})
    backend = await run(e, row, [DONE])
    assert names(backend.seen_tools[0]) == ['Browser', 'Read']  # the browser extension is off, the policy lists it
    system, tools = await e.server._conversation_request_inputs(row['conversation_uuid'], 'CUSTOM')
    assert 'Browser' in names(tools)


@pytest.mark.parametrize('status', ['running', 'paused', 'needs_openbear_control'])
async def test_agents_extension_cannot_disable_with_controllable_task(env, status):
    from app.agents.dao import AgentDAO
    from app.services import Services
    e = env
    row = await created(e, 'pending agent')
    dao = AgentDAO(e.db)
    host = SimpleNamespace(db=e.db, agent_dao=dao)
    e.server.extensions.in_use['agents'] = lambda row: Services._agents_in_use(host, row)
    await e.server.extensions.update(row['conversation_uuid'], enable=['agents'])
    tid = await dao.create_task(chat_id=row['internal_chat_id'], workflow_uuid='', title='pending', status=status)
    refused = await request(e, 'put', f"/api/conversations/{row['conversation_uuid']}/extensions", {'disable': ['agents']}, status=409)
    assert refused['error'] == 'extension_in_use'
    assert 'agents' in (await e.server.extensions.state(row['conversation_uuid'])).enabled
    await dao.update_task(tid, status='completed', finish=True)
    await request(e, 'put', f"/api/conversations/{row['conversation_uuid']}/extensions", {'disable': ['agents']})
    assert 'agents' not in (await e.server.extensions.state(row['conversation_uuid'])).enabled
