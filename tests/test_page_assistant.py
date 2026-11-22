"""The page assistant: its conversations, page bridge, page snapshot, opt-in tools and credential usage."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.assistant import context as page_context
from app.assistant import conversations as assistant_conversations
from app.assistant.page import PageBridge, page_registry
from app.config import Config
from app.extensions import ExtensionService, hidden_tools
from app.memory import secret_usage
from app.memory.builtin import BuiltinMemoryClient
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.memory import register_memory_tools
from app.tools.notes import register_notes_tool
from app.tools.system_config import register_system_config_tool
from tests.test_builtin_template_import import _login_cookie
from tests.test_reference_mentions import sources  # noqa: F401
from tests.test_web_admin import web_env  # noqa: F401


@pytest.fixture
async def env(web_env):  # noqa: F811 - imported pytest fixture
    await _login_cookie(web_env)
    return web_env


async def call(e, method, url, body=None, status=200):
    response = await getattr(e.client, method)(url, **({'json': body} if body is not None else {}))
    assert response.status == status, await response.text()
    return await response.json()


def outcome(result) -> dict:
    return json.loads(getattr(result, 'content', result))


# Page bridge


async def test_a_page_request_without_an_open_page_fails_at_once():
    bridge = PageBridge(lambda: 30)
    started = time.monotonic()
    result = await bridge.request('conv', 'read', {})
    assert result['error'] == 'page_left'
    assert time.monotonic() - started < 0.5


async def test_a_page_that_does_not_answer_times_out_and_a_detached_page_is_gone():
    bridge = PageBridge(lambda: 1)
    sent = []

    async def silent(packet):
        sent.append(packet)

    bridge.attach('conv', 'tab', silent)
    result = await bridge.request('conv', 'fill', {'label': '名称', 'value': 'x'})
    assert result['error'] == 'page_timeout'
    assert sent and sent[0]['type'] == 'page_request' and sent[0]['action'] == 'fill'
    bridge.detach('conv', 'tab')
    assert (await bridge.request('conv', 'read', {}))['error'] == 'page_left'


async def test_the_latest_page_of_a_conversation_answers_and_answers_reach_only_their_request():
    bridge = PageBridge(lambda: 5)
    older, newer = [], []

    async def remember(store, packet):
        store.append(packet)

    bridge.attach('conv', 'old', lambda p: remember(older, p))
    bridge.attach('conv', 'new', lambda p: remember(newer, p))
    task = asyncio.create_task(bridge.request('conv', 'read', {}))
    while not newer:
        await asyncio.sleep(0)
    assert not older
    request_id = newer[0]['requestId']
    assert not bridge.resolve('other-conv', request_id, {'ok': True})
    assert bridge.resolve('conv', request_id, {'ok': True, 'context': {'page': {'key': 'notes'}}})
    assert (await task)['context']['page']['key'] == 'notes'


async def test_a_page_request_names_its_conversations_page_and_says_where_the_user_left():
    async def scope_of(uuid):
        return {'key': 'servers|server:a', 'label': '服务器 · A'} if uuid == 'conv' else None

    bridge = PageBridge(lambda: 5, scope_of)
    left = await bridge.request('conv', 'read', {})
    assert left['error'] == 'page_left' and '「服务器 · A」' in left['message'] and '后台工具' in left['message']
    sent = []

    async def capture(packet):
        sent.append(packet)

    bridge.attach('conv', 'tab', capture)
    task = asyncio.create_task(bridge.request('conv', 'fill', {'label': '名称', 'value': 'x'}))
    while not sent:
        await asyncio.sleep(0)
    assert sent[0]['scope'] == {'key': 'servers|server:a', 'label': '服务器 · A'}
    bridge.resolve('conv', sent[0]['requestId'], {'ok': False, 'error': 'page_left', 'message': 'elsewhere'})
    assert (await task)['error'] == 'page_left'


async def test_the_page_tool_checks_its_arguments_before_asking_the_page():
    bridge = PageBridge(lambda: 5)
    registry = page_registry(bridge)
    ctx = ToolRuntimeContext(conversation_uuid='conv')
    bad = await registry.dispatch('Page', json.dumps({'action': 'fill', 'label': '名称'}), context=ctx)
    assert outcome(bad)['error'] == 'invalid_arguments'
    missing = await registry.dispatch('Page', json.dumps({'action': 'locate', 'target': '保存'}), context=ctx)
    assert outcome(missing)['error'] == 'page_left'
    assert 'click' not in json.dumps(registry.schemas(scope='main'))


# Page snapshot


def test_a_page_snapshot_keeps_known_bounded_fields_and_renders_as_one_block():
    raw = {
        'page': {'key': 'secrets', 'label': '凭证库', 'path': '/secrets', 'extra': 'x'},
        'overlays': [{'kind': 'dialog', 'title': '新建凭证', 'html': '<div>'}],
        'subjects': [{'kind': 'secret', 'id': '3', 'ref': 'openbear://ref/secret/3', 'label': 'github',
                      'details': {'tags': ['a'], 'deep': {'x': {'y': {'z': 1}}}}}, 'junk'],
        'selection': {'source': 'terminal', 'text': 'root  1  /usr/sbin/sshd'},
        'visible': {'of': 'overlay', 'title': '新建凭证', 'text': '字' * 900},
        'actions': [{'name': 'terminal.run', 'description': '输入命令', 'params': {'command': 'text'}}],
        'label': '凭证库 › 新建凭证',
        'unknown': 'dropped',
    }
    snapshot = page_context.normalize(raw, visible_chars=500)
    assert set(snapshot) == {'page', 'overlays', 'subjects', 'selection', 'visible', 'actions', 'label'}
    assert snapshot['page'] == {'key': 'secrets', 'label': '凭证库', 'path': '/secrets'}
    assert snapshot['overlays'] == [{'kind': 'dialog', 'title': '新建凭证'}]
    assert len(snapshot['subjects']) == 1 and snapshot['subjects'][0]['ref'] == 'openbear://ref/secret/3'
    assert len(snapshot['visible']['text']) == 501
    text = page_context.with_context('给它打标签', snapshot)
    assert text.startswith('给它打标签\n\n<page-context>\n') and text.endswith('</page-context>')
    assert 'label:' not in text
    assert page_context.with_context('原文', page_context.normalize({'nothing': 1}, visible_chars=100)) == '原文'


# Conversations


def _assistant_config(**assistant) -> Config:
    return Config.model_validate({
        'telegram': {'botToken': 't', 'whitelistIds': [1]},
        'models': {
            'providers': {'p': {'baseUrl': 'http://x.invalid', 'apiKey': 'k', 'protocol': 'chat', 'models': [
                {'id': 'm', 'thinkingLevels': ['low', 'medium', 'high'], 'defaultThinkingLevel': 'medium'},
            ]}},
            'primary': 'p/m',
        },
        'memory': {'baseUrl': 'http://x.invalid', 'identity': 'openbear', 'accessKey': 'ak'},
        'assistant': assistant,
    })


def test_assistant_run_defaults_come_from_its_settings():
    assert assistant_conversations.run_defaults(_assistant_config()) == {'mainFastMode': False}
    chosen = assistant_conversations.run_defaults(_assistant_config(model='p/m', fastMode=True))
    assert chosen == {'mainFastMode': True, 'mainModel': 'p/m', 'mainThinkingLevel': 'medium'}
    assert assistant_conversations.run_defaults(_assistant_config(model='p/m', thinkingLevel='high'))['mainThinkingLevel'] == 'high'
    assert 'mainModel' not in assistant_conversations.run_defaults(_assistant_config(model='gone/model'))


async def test_notes_and_system_config_are_opt_in_extensions(env):
    config = _assistant_config()
    assert config.assistant.extensions == ['servers']
    names = ['Read', 'Notes', 'SystemConfig', 'Cron']
    assert hidden_tools(names, None) == {'Notes', 'SystemConfig'}
    service = ExtensionService(env.db, env.server)
    service.names = lambda: names
    assert service.effective(None) == ['automation']
    assert assistant_conversations.extensions(_assistant_config(extensions=['notes', 'gone']), {'notes', 'system'}) == {
        'enabled': ['notes'], 'autoAllow': False}


async def test_assistant_conversations_are_kept_out_of_the_ordinary_lists(env):
    env.server.extensions = ExtensionService(env.db, env.server)
    env.server.extensions.names = lambda: ['Read', 'Notes', 'SystemConfig']
    env.server.config.assistant.extensions = ['notes']
    created = await call(env, 'post', '/api/conversations', {'title': '助手', 'folderId': assistant_conversations.FOLDER})
    uuid = created['conversation']['conversationUuid']
    await call(env, 'post', '/api/conversations', {'title': '普通'})
    cur = await env.db.conn.execute('SELECT assistant, folder_uuid FROM web_conversations WHERE conversation_uuid=?', (uuid,))
    assert tuple(await cur.fetchone()) == (1, '')
    state = await env.server.extensions.state(uuid)
    assert sorted(state.enabled) == ['notes']

    tree = await call(env, 'get', '/api/conversation-tree/bootstrap')
    assert (tree['assistantCount'], tree['temporaryCount'], tree['activeCount']) == (1, 1, 1)
    group = await call(env, 'get', '/api/conversation-tree/children?systemNode=assistant')
    assert [item['conversationUuid'] for item in group['items']] == [uuid]
    assert group['items'][0]['assistant'] is True
    temporary = await call(env, 'get', '/api/conversation-tree/children?systemNode=temporary')
    assert uuid not in [item.get('conversationUuid') for item in temporary['items']]


async def test_an_assistant_conversation_keeps_its_page_and_each_page_finds_its_latest(env):
    env.server.extensions = ExtensionService(env.db, env.server)
    env.server.extensions.names = lambda: ['Read']
    scope_a = {'key': 'servers|server:a', 'label': '服务器 · A', 'path': '/servers', 'open': {'kind': 'server', 'id': 'a'}}
    first = await call(env, 'post', '/api/conversations', {'title': 'A1', 'folderId': assistant_conversations.FOLDER, 'assistantScope': scope_a})
    second = await call(env, 'post', '/api/conversations', {'title': 'A2', 'folderId': assistant_conversations.FOLDER, 'assistantScope': scope_a})
    other = await call(env, 'post', '/api/conversations', {'title': 'B', 'folderId': assistant_conversations.FOLDER,
                                                           'assistantScope': {'key': 'servers|server:b', 'label': '服务器 · B', 'path': '/servers'}})
    plain = await call(env, 'post', '/api/conversations', {'title': '普通', 'assistantScope': scope_a})
    def uuid(created):
        return created['conversation']['conversationUuid']

    await env.db.conn.execute('UPDATE web_conversations SET last_interaction_at_ms=? WHERE conversation_uuid=?', (9_000_000_000_000, uuid(first)))
    await env.db.conn.commit()

    listed = await call(env, 'get', '/api/assistant/conversations?scope=servers%7Cserver%3Aa')
    assert listed['latest'] == uuid(first)
    assert [item['conversationUuid'] for item in listed['items']][0] == uuid(first)
    assert {item['conversationUuid'] for item in listed['items']} == {uuid(first), uuid(second), uuid(other)}
    by_uuid = {item['conversationUuid']: item for item in listed['items']}
    assert by_uuid[uuid(first)]['assistantScope'] == scope_a
    assert by_uuid[uuid(other)]['assistantScope']['label'] == '服务器 · B'
    assert (await call(env, 'get', '/api/assistant/conversations?scope=notes'))['latest'] == ''
    cur = await env.db.conn.execute('SELECT assistant_scope FROM web_conversations WHERE conversation_uuid=?', (uuid(plain),))
    assert (await cur.fetchone())[0] == ''
    assert (await assistant_conversations.scope_by_uuid(env.db, uuid(second)))['key'] == 'servers|server:a'
    assert assistant_conversations.scope_json({'label': 'no key'}) == ''


# Opt-in tools


async def test_notes_tool_lists_searches_reads_and_edits_notes(env):
    registry = ToolRegistry()
    register_notes_tool(registry, env.db)
    ctx = ToolRuntimeContext(chat_id=77)

    async def notes(**args):
        return outcome(await registry.dispatch('Notes', json.dumps(args), context=ctx))

    note = (await notes(action='create', title='周报', content='本周完成了开发。'))['note']
    assert (await notes(action='search', query='完成 开发'))['items'][0]['noteUuid'] == note['noteUuid']
    appended = await notes(action='update', id=note['noteUuid'], content='下周测试。', mode='append')
    replaced = await notes(action='update', id=note['noteUuid'], find='本周', replace='这周')
    assert appended['ok'] and replaced['ok']
    body = (await notes(action='get', id=note['noteUuid']))['note']['content']
    assert body == '这周完成了开发。\n\n下周测试。\n'
    stale = await notes(action='update', id=note['noteUuid'], content='x', revision=1)
    assert stale['ok'] is False and stale['currentRevision'] > 1
    assert (await notes(action='update', id=note['noteUuid'], find='没有这段', replace='x'))['error'] == 'find_not_found'
    assert [n['title'] for n in (await notes(action='list'))['items']] == ['周报']


async def test_system_config_explains_settings_and_edits_templates(env):
    registry = ToolRegistry()
    register_system_config_tool(registry, env.server)
    ctx = ToolRuntimeContext(chat_id=123)

    async def system(**args):
        return outcome(await registry.dispatch('SystemConfig', json.dumps(args), context=ctx))

    found = await system(resource='setting', action='list', query='页面助手 默认模型')
    assert 'assistant.model' in [item['path'] for item in found['items']]
    detail = (await system(resource='setting', action='get', paths=['assistant.enabled']))['items'][0]
    assert detail['title'] == '启用页面助手' and detail['value'] is True

    created = (await system(resource='template', action='create', name='试用', content='@if no_such_variable\nHello\n@endif'))
    assert created['ok'] is False and created['error'] == 'template_invalid'
    template = (await system(resource='template', action='create', name='试用', content='你是页面助手。'))['template']
    assert (await system(resource='template', action='update', id=str(template['id']), find='页面助手', replace='助手'))['ok']
    assert (await system(resource='template', action='activate', id=str(template['id']), role='assistant'))['ok']
    active = (await system(resource='template', action='get', role='assistant'))['template']
    assert active['content'] == '你是助手。' and active['roles'] == ['assistant']
    persona = await system(resource='template', action='activate', id=str(template['id']), role='persona')
    assert persona['error'] == 'persona_unavailable'
    listed = (await system(resource='model', action='list'))
    assert listed['primary'] and listed['providers'][0]['models']


# Credentials


async def test_secret_tags_filter_the_list_and_reading_values_records_the_use(env):
    mem = BuiltinMemoryClient(env.db, identity='openbear')
    registry = ToolRegistry()
    register_memory_tools(registry, mem)
    ctx = ToolRuntimeContext(chat_id=123)

    async def memory(**args):
        return outcome(await registry.dispatch('Memory', json.dumps(args), context=ctx))

    await memory(resource='secret', action='set', name='gh', kvJson='[{"key":"token","value":"v"}]', tags='开发, 代码，开发')
    await memory(resource='secret', action='set', name='vps', kvJson='[{"key":"pw","value":"p"}]', tags='服务器')
    items = (await memory(resource='secret', action='list'))['items']
    assert {i['name']: i['tags'] for i in items} == {'gh': '开发,代码', 'vps': '服务器'}
    assert all(i['last_used_at'] == 0 for i in items)
    assert [i['name'] for i in (await memory(resource='secret', action='list', tags='代码'))['items']] == ['gh']
    await memory(resource='secret', action='get', name='gh')
    used = {i['name']: i['last_used_at'] for i in (await memory(resource='secret', action='list'))['items']}
    assert used['gh'] > 0 and used['vps'] == 0
    await memory(resource='secret', action='set', name='gh', note='改备注')
    assert (await memory(resource='secret', action='list', tags='开发'))['items'][0]['note'] == '改备注'


async def test_sending_a_credential_reference_with_its_values_records_the_use_and_a_mention_does_not(env, sources):  # noqa: F811
    store = env.server._reference_store()

    async def last_used():
        cur = await env.db.conn.execute('SELECT last_used_at FROM memory_secrets WHERE id=?', (int(sources['secret']),))
        return (await cur.fetchone())[0]

    mention = await store.resolve(f"[Credential](openbear://ref/secret/{sources['secret']}?mode=mention)", owner=123)
    await store.save(mention, conversation_uuid=sources['chat'], op_id='send:mention')
    assert await last_used() == 0
    content = await store.resolve(f"[Credential](openbear://ref/secret/{sources['secret']})", owner=123)
    await store.save(content, conversation_uuid=sources['chat'], op_id='send:content')
    assert await last_used() > 0


async def test_credential_use_before_the_column_existed_is_filled_once_from_tool_history(env):
    mem = BuiltinMemoryClient(env.db, identity='openbear')
    await mem.tool_call('secret', {'action': 'set', 'name': 'old', 'kvJson': '[]'})
    payload = {'toolName': 'Memory', 'args': json.dumps({'resource': 'secret', 'action': 'get', 'name': '@secret/old'})}
    await env.db.conn.execute(
        'INSERT INTO web_operations (conversation_uuid, op_id, op_type, display_seq, revision, payload_json, created_at_ms, updated_at_ms)'
        ' VALUES (?,?,?,?,?,?,?,?)', ('c', 'op', 'tool', 1, 1, json.dumps(payload, separators=(',', ':')), 1_700_000_000_000, 1_700_000_000_000))
    await env.db.conn.commit()
    await secret_usage.backfill(env.db)
    cur = await env.db.conn.execute("SELECT last_used_at FROM memory_secrets WHERE name='old'")
    assert (await cur.fetchone())[0] == 1_700_000_000
    await env.db.conn.execute("UPDATE memory_secrets SET last_used_at=0")
    await env.db.conn.commit()
    await secret_usage.backfill(env.db)
    cur = await env.db.conn.execute("SELECT last_used_at FROM memory_secrets WHERE name='old'")
    assert (await cur.fetchone())[0] == 0


async def test_detaching_a_page_host_ends_in_flight_requests_at_once():
    bridge = PageBridge(lambda: 30, scope_of=lambda _uuid: asyncio.sleep(0, result={'key': 'servers', 'label': '服务器'}))

    async def silent(_packet):
        pass

    bridge.attach('conv', 'tab', silent)
    started = time.monotonic()
    pending = asyncio.create_task(bridge.request('conv', 'read', {}))
    await asyncio.sleep(0.02)
    bridge.detach('conv', 'tab')
    result = await pending
    assert result['error'] == 'page_left'
    assert '服务器' in result['message']
    assert time.monotonic() - started < 0.5


async def test_notes_update_validates_revision_and_guards_read_modify_write(env):
    from app.kb_notes import KBNotesDAO

    registry = ToolRegistry()
    register_notes_tool(registry, env.db)
    ctx = ToolRuntimeContext(chat_id=77)

    async def notes(**args):
        return outcome(await registry.dispatch('Notes', json.dumps(args), context=ctx))

    note = (await notes(action='create', title='清单', content='第一行\n第二行'))['note']
    bad_rev = await registry.dispatch_outcome('Notes', json.dumps({'action': 'update', 'id': note['noteUuid'], 'content': 'x', 'revision': 'abc'}), context=ctx)
    assert bad_rev.effect_state == 'not_started' and outcome(bad_rev)['error'] == 'invalid_revision'
    bool_rev = await registry.dispatch_outcome('Notes', json.dumps({'action': 'update', 'id': note['noteUuid'], 'content': 'x', 'revision': True}), context=ctx)
    assert bool_rev.effect_state == 'not_started' and outcome(bool_rev)['error'] == 'invalid_revision'

    # Simulate a concurrent edit landing between get_note and update_note when revision is omitted on find/replace.
    real_update = KBNotesDAO.update_note

    async def race_update(self, owner, note_uuid, patch, *, expected_revision=None):
        await real_update(self, owner, note_uuid, {'content': '用户刚刚改过的内容'})
        return await real_update(self, owner, note_uuid, patch, expected_revision=expected_revision)

    KBNotesDAO.update_note = race_update
    try:
        raced = await notes(action='update', id=note['noteUuid'], find='第一行', replace='改动')
    finally:
        KBNotesDAO.update_note = real_update
    assert raced['ok'] is False and raced['error'] == 'kb_note_revision_conflict'
    latest = (await notes(action='get', id=note['noteUuid']))['note']
    assert latest['content'] == '用户刚刚改过的内容'


async def test_system_config_template_respects_memory_lock_and_validates_main_activation(env):
    registry = ToolRegistry()
    register_system_config_tool(registry, env.server)
    ctx = ToolRuntimeContext(chat_id=123)

    async def system(**args):
        return outcome(await registry.dispatch('SystemConfig', json.dumps(args), context=ctx))

    # A template valid only for Agent (unknown @if variable for main) cannot be activated as main or assistant.
    cur = await env.db.conn.execute(
        'INSERT INTO memory_templates (name, content, is_active, is_agent_active, is_persona, is_assistant_active, updated_at) VALUES (?,?,0,1,0,0,1)',
        ('仅限Agent', '@if no_such_main_var\nhello\n@endif'),
    )
    await env.db.conn.commit()
    agent_tpl_id = str(cur.lastrowid)
    for role in ('main', 'assistant'):
        res = await system(resource='template', action='activate', id=agent_tpl_id, role=role)
        assert res['ok'] is False and res['error'] == 'template_invalid'

    # While a memory operation is running, template mutations are refused instead of racing it.
    await env.db.conn.execute(
        "INSERT INTO operations (operation_uuid, chat_id, kind, status, started_at) VALUES ('op-1', 123, 'memory_import', 'running', 1)"
    )
    await env.db.conn.commit()
    try:
        busy_create = await system(resource='template', action='create', name='冲突', content='hello')
        assert busy_create['ok'] is False and busy_create['error'] == 'memory_busy'
        busy_update = await system(resource='template', action='update', id=agent_tpl_id, name='新名')
        assert busy_update['ok'] is False and busy_update['error'] == 'memory_busy'
        busy_activate = await system(resource='template', action='activate', id=agent_tpl_id, role='agent')
        assert busy_activate['ok'] is False and busy_activate['error'] == 'memory_busy'
    finally:
        await env.db.conn.execute("DELETE FROM operations WHERE operation_uuid='op-1'")
        await env.db.conn.commit()


async def test_assistant_conversations_cannot_move_into_folders_and_archive_stays_in_own_group(env):
    folder = (await call(env, 'post', '/api/conversation-folders', {'name': '工作'}))['folder']['folderId']
    temp1 = (await call(env, 'post', '/api/conversations', {'title': '临时一'}))['conversation']['conversationUuid']
    asst1 = (await call(env, 'post', '/api/conversations', {
        'title': '助手一', 'folderId': assistant_conversations.FOLDER,
        'assistantScope': {'key': 'servers', 'label': '服务器', 'path': '/servers'},
    }))['conversation']['conversationUuid']
    asst2 = (await call(env, 'post', '/api/conversations', {
        'title': '助手二', 'folderId': assistant_conversations.FOLDER,
        'assistantScope': {'key': 'settings', 'label': '系统设置', 'path': '/settings'},
    }))['conversation']['conversationUuid']

    # Moving an assistant conversation into a regular folder is refused.
    res = await env.client.post('/api/conversation-tree/move', json={'kind': 'conversation', 'id': asst1, 'targetFolderId': folder})
    assert res.status == 400
    assert 'assistant_conversation_cannot_move_to_folder' in await res.text()

    # Archiving the only temporary conversation does not pick an assistant conversation as its successor.
    archived_temp = await call(env, 'patch', f'/api/conversations/{temp1}', {'archived': True})
    assert archived_temp.get('nextConversation') is None

    # Archiving an assistant conversation picks its assistant sibling, never a temporary conversation.
    temp2 = (await call(env, 'post', '/api/conversations', {'title': '临时二'}))['conversation']['conversationUuid']
    archived_asst = await call(env, 'patch', f'/api/conversations/{asst1}', {'archived': True})
    assert archived_asst['nextConversation']['conversationUuid'] == asst2
    assert archived_asst['nextConversation']['conversationUuid'] != temp2
