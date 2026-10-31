from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent import steering
from app.context.prompt_policy import PromptToolRegistry, policy, read_json, snapshot_key, write_json
from app.db.dao import MessageDAO
from app.llm.events import StreamEvent, ToolCall
from app.task_memory import SCOPE_CONVERSATION, TaskMemoryDAO, is_task_memory_runtime_message
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.web_console.live_stream import _WebLiveStream, _WebStreamRenderer
from tests.test_builtin_template_import import _login_cookie
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, web_env


@pytest.fixture
async def env(web_env):  # noqa: F811 - imported pytest fixture
    e = web_env
    await _login_cookie(e)
    e.server.model_selection = SimpleNamespace(current='openai/gpt')
    e.server.tools = ToolRegistry()
    e.effects = []

    async def echo(args):
        e.effects.append('echo')
        return 'echo done'

    async def other(args):
        e.effects.append('other')
        return 'other done'

    e.server.tools.add('echo', 'Echo tool', {'type': 'object', 'properties': {}}, echo)
    e.server.tools.add('other', 'Other tool', {'type': 'object', 'properties': {}}, other)
    await e.db.conn.execute('INSERT INTO memory_templates(name,content,is_active,updated_at) VALUES (?,?,1,1)',
        ('global', 'GLOBAL [[ folderWorkspaceDir ]] [[ folderPrompt ]]'))
    await e.db.conn.commit()
    return e


async def request(e, method, url, body=None):
    response = await getattr(e.client, method)(url, **({'json': body} if body is not None else {}))
    assert response.status == 200, await response.text()
    return await response.json()


async def folder(e, name, parent=''):
    data = await request(e, 'post', '/api/conversation-folders', {'name': name, 'parentId': parent})
    return data['folder']['folderId']


async def save_folder(e, fid, text, **flags):
    return await request(e, 'put', f'/api/conversation-folders/{fid}/properties', {
        'promptMarkdown': text, 'promptPolicy': flags.pop('promptPolicy', {}), **flags})


async def create(e, fid=''):
    return await e.server._create_web_conversation(123, title='Policy test', model='openai/gpt', folder_uuid=fid)


async def save(e, row, text, **flags):
    return await request(e, 'put', f"/api/conversations/{row['conversation_uuid']}/properties", {'contextText': text, **flags})


async def run(e, row, text='human input', *, backend=None, turn='turn-1', **kwargs):
    backend = backend or FakeStreamBackend()
    backend.protocol = 'chat'
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    live = _WebLiveStream(row['conversation_uuid'], row['internal_chat_id'])
    await live.publish({'type': 'accepted', 'turnUuid': turn})
    assert await e.server._run_web_turn(row['internal_chat_id'], text, _WebStreamRenderer(live),
        conversation=row, root_turn_uuid=turn, **kwargs)
    return backend


async def test_whole_policy_nearest_nonempty_inheritance_and_temporary_isolation(env):
    e = env
    a = await folder(e, 'A')
    b = await folder(e, 'B', a)
    custom = {'overrideSystemPrompt': True, 'toolsEnabled': True, 'toolNames': ['echo'], 'userMessageTemplateEnabled': True}
    await save_folder(e, a, 'parent [[ conversation.title ]]', promptPolicy=custom)
    await save_folder(e, b, '   ', promptPolicy={'toolsEnabled': False})
    row = await create(e, b)
    values = (await request(e, 'get', f"/api/conversations/{row['conversation_uuid']}/properties"))['properties']
    assert values['contextText'] == ''
    assert values['promptPolicy']['effective'] == policy({**custom, 'text': 'parent [[ conversation.title ]]'})
    assert values['promptPolicy']['sourceFolderId'] == a
    assert await e.server._build_system_prompt_for_chat(row['conversation_uuid']) == 'parent Policy test'
    # Nonempty child ordinary Markdown masks ALL parent flags, not only its text.
    await save(e, row, 'child [[ unrendered.markdown ]]')
    selected = await e.server._resolved_prompt_policy(row['conversation_uuid'])
    assert not selected['overrideSystemPrompt'] and not selected['toolsEnabled']
    assert 'GLOBAL' in await e.server._build_system_prompt_for_chat(row['conversation_uuid'])
    assert 'child [[ unrendered.markdown ]]' in await e.server._build_system_prompt_for_chat(row['conversation_uuid'])
    impact = await request(e, 'post', f'/api/conversation-folders/{a}/properties/impact', {
        'promptMarkdown': 'changed parent', 'promptPolicy': custom})
    assert impact['affectedCount'] == 0
    await save(e, row, '')
    assert (await e.server._resolved_prompt_policy(row['conversation_uuid']))['toolsEnabled']
    await save_folder(e, '__temporary', 'temporary', promptPolicy={'overrideSystemPrompt': True})
    temp = await create(e)
    assert (await e.server._resolved_prompt_policy(temp['conversation_uuid']))['text'] == 'temporary'
    root = await create(e, await folder(e, 'independent root'))
    assert (await e.server._resolved_prompt_policy(root['conversation_uuid']))['text'] == ''


async def test_save_only_then_apply_freezes_system_tools_and_preserves_data(env):
    e = env
    row = await create(e)
    await run(e, row)
    before = await MessageDAO(e.db).get_system_snapshot(row['internal_chat_id'])
    cid = row['conversation_uuid']
    mem, _created = await TaskMemoryDAO(e.db).create(conversation_uuid=cid, scope_type=SCOPE_CONVERSATION,
        name='preserve', description='preserved note', body='real note')
    await save(e, row, 'CUSTOM', overrideSystemPrompt=True, toolsEnabled=True, toolNames=['echo'])
    assert await MessageDAO(e.db).get_system_snapshot(row['internal_chat_id']) == before
    old = await run(e, row, turn='turn-2')
    assert old.seen_systems[0] == before
    assert len(old.seen_tools[0]) == 2
    impact = await request(e, 'post', f'/api/conversations/{cid}/properties/impact', {
        'contextText': 'CUSTOM', 'overrideSystemPrompt': True, 'toolsEnabled': True, 'toolNames': ['echo']})
    assert impact['affectedCount'] == impact['updatableCount'] == 1
    result = await save(e, row, 'CUSTOM', overrideSystemPrompt=True, toolsEnabled=True, toolNames=['echo'], updateSnapshots=True)
    assert result['updatedCount'] == 1
    new = await run(e, row, turn='turn-3')
    assert new.seen_systems == ['CUSTOM']
    assert [t['name'] for t in new.seen_tools[0]] == ['echo']
    assert not any(is_task_memory_runtime_message(m) for m in new.seen_convos[0])
    assert '[⏰' not in str(new.seen_convos[0][-1])
    assert len([m for m in await MessageDAO(e.db).recent(row['internal_chat_id']) if m.role == 'user']) == 3
    # The original note is still in its data store.
    cur = await e.db.conn.execute('SELECT body FROM conversation_task_memories WHERE memory_uuid=?', (mem['memoryUuid'],))
    assert (await cur.fetchone())['body'] == 'real note'


async def test_running_target_is_skipped_and_not_implicitly_updated_after_run(env):
    row = await create(env)
    await run(env, row)
    before = await MessageDAO(env.db).get_system_snapshot(row['internal_chat_id'])
    async with env.server.operation_locks.chat(row['internal_chat_id'], 'busy-test'):
        result = await save(env, row, 'NEXT', overrideSystemPrompt=True, updateSnapshots=True)
        assert result['updatedCount'] == 0 and result['skippedRunningCount'] == 1
    assert await MessageDAO(env.db).get_system_snapshot(row['internal_chat_id']) == before
    assert not (await env.server._active_prompt_policy(row['conversation_uuid']))['overrideSystemPrompt']


@pytest.mark.parametrize('override,inject,global_enabled', [(False, False, True), (False, False, False), (True, False, True), (True, True, True), (True, True, False)])
async def test_actual_requests_suffix_memory_and_retry_stability(env, override, inject, global_enabled):
    e = env
    row = await create(e)
    await save(e, row, 'LOCAL', overrideSystemPrompt=override, userMessageTemplateEnabled=inject)
    e.server.config.user_message_template.enabled = global_enabled
    e.server.config.user_message_template.template = 'SUFFIX [[ message.source ]] [[ message.id ]] [[ time.iso ]]'
    e.server.config.agent.retry_backoff_s = 0
    await TaskMemoryDAO(e.db).create(conversation_uuid=row['conversation_uuid'], scope_type=SCOPE_CONVERSATION,
        name='memory', description='note', body='KEEP MEMORY')
    backend = FakeStreamBackend([[StreamEvent(kind='error', error='temporary', retryable=True)],
        [StreamEvent(kind='content', text='done'), StreamEvent(kind='finish', finish_reason='stop')]])
    await run(e, row, backend=backend, user_op_id='msg:message-1', user_message_source='telegram')
    assert len(backend.seen_convos) == 2
    first, retry = backend.seen_convos
    assert retry[:len(first)] == first
    human = next(m for m in first if 'human input' in str(m.get('content')))
    expected = global_enabled and (not override or inject)
    assert ('SUFFIX telegram message-1' in human['content']) == expected
    assert human['content'].count('SUFFIX') == int(expected)
    assert any(is_task_memory_runtime_message(m) for m in first) == (not override)
    assert 'Context window runtime' not in backend.seen_systems[0]
    assert ('GLOBAL' in backend.seen_systems[0]) == (not override)
    saved = await MessageDAO(e.db).recent(row['internal_chat_id'])
    assert next(m for m in saved if m.role == 'user').content == 'human input'


async def test_steers_get_suffix_without_changing_transcript(env):
    e = env
    row = await create(e)
    e.server.config.user_message_template.template = 'SUFFIX [[ message.source ]]'
    steering.enqueue(row['internal_chat_id'], 'human steer', source='telegram', messageUuid='steer-1', turnUuid='turn-1')
    backend = await run(e, row)
    matching = [m for m in backend.seen_convos[0] if 'human steer' in str(m.get('content'))]
    assert matching[0]['content'] == 'human steer\n\nSUFFIX telegram'
    saved = await MessageDAO(e.db).recent(row['internal_chat_id'])
    assert any(m.content == 'human steer' for m in saved)
    assert all('SUFFIX' not in m.content for m in saved)


async def test_folder_flags_can_apply_without_text_change_and_copy_keeps_policy(env):
    e = env
    fid = await folder(e, 'frozen')
    flags = {'overrideSystemPrompt': True}
    await save_folder(e, fid, 'SAME SYSTEM', promptPolicy=flags)
    row = await create(e, fid)
    await run(e, row)
    flags.update(toolsEnabled=True, toolNames=['echo'])
    await save_folder(e, fid, 'SAME SYSTEM', promptPolicy=flags)
    assert not (await e.server._active_prompt_policy(row['conversation_uuid']))['toolsEnabled']
    data = await save_folder(e, fid, 'SAME SYSTEM', promptPolicy=flags, updateSnapshots=True)
    assert data['updatedCount'] == 1
    assert (await e.server._active_prompt_policy(row['conversation_uuid']))['toolsEnabled']
    # Conversation-local overrides and the frozen state must survive duplication.
    await save(e, row, 'LOCAL COPY', overrideSystemPrompt=True, updateSnapshots=True)
    duplicate = await request(e, 'post', f"/api/conversations/{row['conversation_uuid']}/duplicate")
    copy_id = duplicate['conversation']['conversationUuid']
    assert (await e.server._local_conversation_prompt_policy(copy_id))['text'] == 'LOCAL COPY'
    assert (await e.server._active_prompt_policy(copy_id))['overrideSystemPrompt']


async def test_internal_notification_does_not_receive_user_template(env):
    row = await create(env)
    await run(env, row)
    env.server.config.user_message_template.template = 'UNWANTED SUFFIX'
    backend = await run(env, row, text='agent notification', task_notification=True)
    assert 'UNWANTED SUFFIX' not in str(backend.seen_convos)


async def test_tool_dispatch_enforced_but_existing_handlers_keep_their_context(env):
    ctx = ToolRuntimeContext()
    filtered = PromptToolRegistry(env.server.tools, ['echo'])
    assert [t['name'] for t in filtered.schemas('main')] == ['echo']
    assert '允许列表' in await filtered.dispatch('other', '{}', context=ctx)
    assert ctx.tool_outcome.effect_state == 'not_started'
    assert env.effects == []
    assert await filtered.dispatch('echo', '{}', context=ctx) == 'echo done'
    assert env.effects == ['echo']


async def test_preview_tool_variables_unknown_tool_retention_and_invalid_template(env):
    e = env
    row = await create(e)
    cid = row['conversation_uuid']
    data = await request(e, 'get', '/api/prompt-tools')
    assert {t['name'] for t in data['items']} == {'echo', 'other'}
    draft = {'conversationId': cid, 'text': '[[ conversation.title ]] [[ helpers.join(toolNames, ",") ]]',
        'overrideSystemPrompt': True, 'toolsEnabled': True, 'toolNames': ['echo']}
    preview = await request(e, 'post', '/api/prompt-template/preview', draft)
    assert preview['prompt'] == 'Policy test echo'
    assert preview['params']['tools']['allowlist'] == ['echo']
    assert await read_json(e.db.conn, 'conversation_context:' + cid) is None
    await save(e, row, 'CUSTOM', overrideSystemPrompt=True, toolsEnabled=True, toolNames=['echo'])
    e.server.tools = ToolRegistry()
    await save(e, row, 'CUSTOM 2', overrideSystemPrompt=True, toolsEnabled=True, toolNames=['echo'])
    bad = await e.client.put(f'/api/conversations/{cid}/properties', json={'contextText': 'x', 'toolNames': ['new-missing']})
    assert bad.status == 422
    bad = await e.client.put(f'/api/conversations/{cid}/properties', json={'contextText': '[[ unknown.value ]]', 'overrideSystemPrompt': True})
    assert bad.status == 422
    assert (await e.server._resolved_prompt_policy(cid))['text'] == 'CUSTOM 2'


PROMPT_HASH_KEYS = ('beforeHash', 'afterHash', 'toolsBeforeHash', 'toolsAfterHash')


async def edited_branch(e, row, tools):
    cid = row['conversation_uuid']
    system = await MessageDAO(e.db).get_system_snapshot(row['internal_chat_id'])
    await e.db.conn.execute('INSERT INTO context_editor_branches VALUES(?,?,?,?,?)',
        (cid, cid, json.dumps({'system': system, 'tools': tools, 'frozen': True}), '{}', 1))
    await e.db.conn.commit()


def stale_tools():
    schema = {'type': 'object', 'properties': {}}
    return [{'name': 'echo', 'description': 'Old echo', 'parameters': schema},
            {'name': 'retired', 'description': 'Removed since the edit', 'parameters': schema}]


async def branch_tools(e, row):
    cur = await e.db.conn.execute('SELECT settings_json FROM context_editor_branches WHERE conversation_uuid=?', (row['conversation_uuid'],))
    return json.loads((await cur.fetchone())['settings_json'])['tools']


async def test_prompt_refresh_replaces_edited_branch_tools_with_current_definitions(env):
    e = env
    row = await create(e)
    await run(e, row)
    await edited_branch(e, row, stale_tools())
    assert [t['name'] for t in (await run(e, row, turn='turn-2')).seen_tools[0]] == ['echo', 'retired']
    url = f"/api/conversations/{row['conversation_uuid']}/system-prompt"
    preview = await request(e, 'post', url + '/preview')
    assert preview['changed'] and preview['toolsChanged'] and preview['toolsFrozen'] and not preview['systemChanged']
    assert (preview['toolsBefore'], preview['toolsAfter']) == (2, 2)
    assert preview['toolChanges'] == {'added': ['other'], 'removed': ['retired'], 'changed': ['echo'], 'reordered': False}

    result = await request(e, 'put', url, {'confirmed': True, **{key: preview[key] for key in PROMPT_HASH_KEYS}})
    assert result['updated'] is True
    seen = (await run(e, row, turn='turn-3')).seen_tools[0]
    assert [t['name'] for t in seen] == ['echo', 'other']
    assert seen[0]['description'] == 'Echo tool'
    assert not (await request(e, 'post', url + '/preview'))['changed']


async def test_prompt_refresh_rejects_tools_changed_after_review(env):
    e = env
    row = await create(e)
    await run(e, row)
    await edited_branch(e, row, stale_tools())
    url = f"/api/conversations/{row['conversation_uuid']}/system-prompt"
    preview = await request(e, 'post', url + '/preview')

    async def third(args):
        return 'third done'
    e.server.tools.add('third', 'Unreviewed tool', {'type': 'object', 'properties': {}}, third)
    response = await e.client.put(url, json={'confirmed': True, **{key: preview[key] for key in PROMPT_HASH_KEYS}})
    assert response.status == 409 and (await response.json())['error'] == 'prompt_preview_changed'
    assert await branch_tools(e, row) == stale_tools()


async def test_property_snapshot_update_also_refreshes_edited_branch_tools(env):
    e = env
    row = await create(e)
    await run(e, row)
    await edited_branch(e, row, stale_tools())
    await save(e, row, 'CUSTOM', overrideSystemPrompt=True, toolsEnabled=True, toolNames=['echo'], updateSnapshots=True)
    seen = (await run(e, row, turn='turn-2')).seen_tools[0]
    assert [(t['name'], t['description']) for t in seen] == [('echo', 'Echo tool')]
