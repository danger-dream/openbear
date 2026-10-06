from __future__ import annotations
import copy
from types import SimpleNamespace
import pytest
from app.context.editor import document, compile_document, branch_settings
from app.db.dao import MessageDAO
from app.llm.openai_chat import OpenAIChatBackend
from app.llm.events import StreamEvent, ToolCall
from app.tools.base import ToolRegistry
from app.web_admin import _WebLiveStream, _WebStreamRenderer
from tests.test_web_admin import web_env as shared_web_env, _login_cookie, FakeStreamBackend

web_env = shared_web_env


def fixture_document():
    return document('original system', [{'name': 'Read', 'description': 'read', 'parameters': {'type': 'object'}}], [
        {'role': 'user', 'content': 'question', 'extension': {'unknown': True}},
        {'role': 'assistant', 'content': 'long answer', 'tool_calls': [{'id': 'c1', 'name': 'Read', 'arguments': '{"path":"old"}'}],
         'native_output_items': [{'type': 'reasoning', 'encrypted_content': 'opaque', 'vendor': 1}, {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'long answer'}]}, {'type': 'function_call', 'call_id': 'c1', 'name': 'Read', 'arguments': '{"path":"old"}'}]},
        {'role': 'tool', 'content': 'result', 'name': 'Read', 'tool_call_id': 'c1'}], origin={'protocol': 'responses', 'modelLabel': 'p/m'})


def test_bidirectional_native_edits_keep_unknown_and_opaque_fields():
    base = fixture_document()
    edit = copy.deepcopy(base)
    edit['entries'][1]['message']['content'] = 'short'
    compiled, issues = compile_document(base, edit, protocol='responses', available_tools=['Read'])
    assert not any(i['severity'] == 'error' for i in issues)
    assert compiled['entries'][1]['message']['native_output_items'][1]['content'][0]['text'] == 'short'
    assert compiled['entries'][1]['message']['native_output_items'][0] == base['entries'][1]['message']['native_output_items'][0]
    assert base['entries'][1]['message']['content'] == 'long answer'
    edit = copy.deepcopy(base)
    edit['entries'][1]['message']['native_output_items'][1]['content'][0]['text'] = 'from native'
    compiled, issues = compile_document(base, edit, protocol='responses', available_tools=['Read'])
    assert compiled['entries'][1]['message']['content'] == 'from native'
    assert not any(i['severity'] == 'error' for i in issues)


def test_conflicts_dangling_and_unbound_tools_do_not_autofix():
    base = fixture_document()
    edit = copy.deepcopy(base)
    edit['entries'][1]['message']['content'] = 'new'
    edit['entries'][1]['message']['native_output_items'][1]['content'][0]['text'] = 'different'
    edit['entries'].pop()
    _, issues = compile_document(base, edit, protocol='responses')
    assert {'native_conflict', 'missing_tool_results', 'unbound_tool'} <= {i['code'] for i in issues}
    assert len(edit['entries']) == 2


def test_anthropic_thinking_only_is_not_a_second_body_and_prefix_change_is_blocked():
    base = document('s', [], [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'answer', 'reasoning': '', 'signature': 'sig', 'native_output_items': [{'type': 'thinking', 'thinking': '', 'signature': 'sig'}, {'type': 'redacted_thinking', 'data': 'opaque'}]}], origin={'protocol': 'anthropic', 'modelLabel': 'p/a'})
    compiled, issues = compile_document(base, base, protocol='anthropic', model_label='p/a')
    assert compiled['entries'][1]['message']['content'] == 'answer'
    assert all(x['severity'] == 'warning' for x in issues)
    edit = copy.deepcopy(base)
    edit['entries'][0]['message']['content'] = 'edited prefix'
    _, issues = compile_document(base, edit, protocol='anthropic', model_label='p/a')
    assert 'signed_prefix_changed' in {i['code'] for i in issues}


class Backend(FakeStreamBackend):
    protocol = 'chat'
    build_payload = OpenAIChatBackend.build_payload


async def setup(env):
    await _login_cookie(env)
    backend = Backend()
    env.server.llm_factory = SimpleNamespace(backend_for=lambda label: (backend, label.split('/')[1], 8192), context_window=lambda _: 128000)
    env.server.model_selection = SimpleNamespace(current='openai/gpt')
    env.server.tools = ToolRegistry()
    executed = []
    async def handler(args):
        executed.append(args)
        return 'fresh result'
    env.server.tools.add('Read', 'read', {'type': 'object'}, handler)
    row = await env.server._create_web_conversation(123, model='openai/gpt')
    dao = MessageDAO(env.db)
    await dao.get_or_set_system_snapshot(row['internal_chat_id'], 'source system')
    await dao.add(row['internal_chat_id'], 'user', 'original question')
    await dao.add(row['internal_chat_id'], 'assistant', 'long response')
    url = f"/api/conversations/{row['conversation_uuid']}/context-editor"
    res = await env.client.get(url)
    assert res.status == 200, await res.text()
    data = await res.json()
    body = {'baseline': data['baseline'], 'working': copy.deepcopy(data['baseline']), 'snapshotToken': data['snapshotToken'], 'mode': 'compatible', 'targetModel': 'openai/gpt', 'revision': 0}
    return row, url, body, backend, executed


async def test_http_preview_save_cas_stale_and_raw(web_env):
    env = web_env
    row, url, body, backend, _ = await setup(env)
    body['working']['entries'][-1]['message']['content'] = 'short response'
    res = await env.client.post(url + '/preview', json=body)
    assert res.status == 200, await res.text()
    preview = await res.json()
    assert not preview['issues']
    assert preview['payload']['messages'][-1]['content'] == 'short response'
    assert backend.calls == 0
    saved = await env.client.put(url + '/draft', json=body)
    assert saved.status == 200
    assert (await saved.json())['draft']['revision'] == 1
    assert (await env.client.put(url + '/draft', json=body)).status == 409
    assert (await (await env.client.get(url)).json())['draft']['working'] == body['working']
    raw = {**body, 'mode': 'raw'}
    assert (await env.client.post(url + '/branch', json=raw)).status == 400
    await MessageDAO(env.db).add(row['internal_chat_id'], 'user', 'source advanced')
    assert (await env.client.post(url + '/branch', json=body)).status == 409


async def test_branch_real_runtime_reload_two_turns_and_model_change(web_env):
    env = web_env
    source, url, body, backend, executed = await setup(env)
    body['working']['system'] = ''  # Empty system is a deliberate edit, not a fallback.
    body['working']['tools'][0]['description'] = 'edited tool description'
    body['working']['entries'][0]['message']['vendor_extension'] = {'nested': [1, 2]}
    body['working']['entries'][1]['message']['content'] = 'short response'
    body['working']['entries'].extend([
        {'entryId': 'historic-call', 'message': {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'old-call', 'name': 'Read', 'arguments': '{}'}]}},
        {'entryId': 'historic-result', 'message': {'role': 'tool', 'content': 'edited historical result', 'tool_call_id': 'old-call', 'name': 'Read'}},
    ])
    res = await env.client.post(url + '/branch', json=body)
    assert res.status == 200, await res.text()
    bid = (await res.json())['conversation']['conversationUuid']
    branch = await env.server._conversation_row(123, bid)
    assert bid != source['conversation_uuid']
    assert executed == []
    source_rows = await MessageDAO(env.db).recent(source['internal_chat_id'], limit=20)
    assert source_rows[-1].content == 'long response'
    assert (await branch_settings(env.db, bid))['system'] == ''
    from app.db.engine import DB
    reloaded = DB(env.db.path)
    await reloaded.connect()
    try:
        assert await branch_settings(reloaded, bid) == await branch_settings(env.db, bid)
        state = await MessageDAO(reloaded).load_controller_model_context(branch['internal_chat_id'], conversation_uuid=bid,
            session_id=await MessageDAO(reloaded).current_session_uuid(branch['internal_chat_id']), protocol='chat', model='gpt', model_label='openai/gpt')
        assert state['messages'][0]['vendor_extension'] == {'nested': [1, 2]}
    finally:
        await reloaded.close()
    backend.scripts = [[StreamEvent(kind='tool_call', tool_calls=[ToolCall('new-call', 'Read', '{}')]), StreamEvent(kind='finish', finish_reason='tool_calls')], [StreamEvent(kind='content', text='done'), StreamEvent(kind='finish', finish_reason='stop')]]
    for i in range(2):
        if i:
            await env.db.conn.execute('UPDATE web_conversations SET model=? WHERE conversation_uuid=?', ('openai/cheap', bid))
            await env.db.conn.commit()
            branch = await env.server._conversation_row(123, bid)
        live = _WebLiveStream(bid, branch['internal_chat_id'])
        await live.publish({'type': 'accepted', 'turnUuid': f'new-{i}'})
        ok = await env.server._run_web_turn(branch['internal_chat_id'], 'continue', _WebStreamRenderer(live), conversation=branch, root_turn_uuid=f'new-{i}')
        assert ok, live.snapshot()
        sent = backend.seen_convos[-1]
        assert 'short response' in str(sent)
        assert 'long response' not in str(sent)
        assert 'edited historical result' in str(sent)
        assert backend.seen_systems[-1] == ''
        assert backend.seen_tools[-1][0]['description'] == 'edited tool description'
        assert any(m.get('vendor_extension') == {'nested': [1, 2]} for m in sent)
    assert len(executed) == 1  # Only the new call, never the historical pair.
    operations = await env.server._web_operations(bid)
    assert any(op['opType'] == 'assistant_message' and op['payload'].get('text') == 'short response' for op in operations)
    assert any(op['opType'] == 'tool' and op['payload'].get('resultText') == 'edited historical result' for op in operations)
    duplicate = await env.server._duplicate_web_conversation_data(branch)
    assert await branch_settings(env.db, duplicate['conversation_uuid']) == await branch_settings(env.db, bid)


def test_responses_reasoning_summary_sync_is_explicit_and_opaque_bytes_unchanged():
    base = document('s', [], [{'role': 'assistant', 'content': '', 'reasoning': 'old summary',
        'native_output_items': [{'type': 'reasoning', 'encrypted_content': 'opaque-bytes', 'summary': [{'type': 'summary_text', 'text': 'old summary', 'vendor': 7}]}]}])
    edit = copy.deepcopy(base)
    edit['entries'][0]['message']['reasoning'] = 'new summary'
    doc, issues = compile_document(base, edit, protocol='responses')
    native = doc['entries'][0]['message']['native_output_items'][0]
    assert native['summary'] == [{'type': 'summary_text', 'text': 'new summary', 'vendor': 7}]
    assert native['encrypted_content'] == 'opaque-bytes'
    assert any(i['code'] == 'opaque_reasoning_unverified' for i in issues)
    assert not any(i['severity'] == 'error' for i in issues)
    edit['entries'][0]['message']['native_output_items'][0]['summary'][0]['text'] = 'conflict'
    assert any(i['code'] == 'reasoning_conflict' for i in compile_document(base, edit, protocol='responses')[1])


async def test_busy_read_and_branch_preserve_draft(web_env):
    env = web_env
    row, url, body, _, _ = await setup(env)
    env.server._web_starting_turns[row['conversation_uuid']] = True
    assert (await env.client.get(url)).status == 409
    assert (await env.client.post(url + '/branch', json=body)).status == 409
    assert (await env.client.put(url + '/draft', json=body)).status == 200


@pytest.mark.parametrize('protocol', ['chat', 'responses', 'anthropic'])
async def test_actual_protocol_preview_and_private_branch_fields(web_env, protocol):
    from app.llm.anthropic import AnthropicBackend
    from app.llm.openai_responses import OpenAIResponsesBackend
    env = web_env
    row, url, body, _, _ = await setup(env)
    cls = {'chat': OpenAIChatBackend, 'responses': OpenAIResponsesBackend, 'anthropic': AnthropicBackend}[protocol]
    real = cls(None, 'https://unused.invalid', 'not-a-secret')
    env.server.config.models.providers['openai'].protocol = protocol
    env.server.llm_factory = SimpleNamespace(backend_for=lambda _: (real, 'gpt', 8192), context_window=lambda _: 128000)
    first = await (await env.client.get(url)).json()
    body.update(baseline=first['baseline'], working=copy.deepcopy(first['baseline']), snapshotToken=first['snapshotToken'])
    original = body['baseline']['entries'][1]['message']
    if protocol == 'responses':
        original['native_output_items'] = [{'type': 'message', 'role': 'assistant', 'vendor': {'retained': True}, 'content': [{'type': 'output_text', 'text': 'long response'}]}]
    elif protocol == 'anthropic':
        original['reasoning'] = ''
        original['signature'] = 'signature'
        original['native_output_items'] = [{'type': 'thinking', 'thinking': '', 'signature': 'signature', 'vendor': {'retained': True}}, {'type': 'text', 'text': 'long '}, {'type': 'redacted_thinking', 'data': 'opaque'}, {'type': 'text', 'text': 'response'}]
    body['working'] = copy.deepcopy(body['baseline'])
    result = await env.client.post(url + '/preview', json=body)
    assert result.status == 200, await result.text()
    preview = await result.json()
    assert preview['payload']['model'] == 'gpt'
    if protocol == 'anthropic':
        content = preview['payload']['messages'][1]['content']
        assert [block['type'] for block in content] == [block['type'] for block in original['native_output_items']]
        assert ''.join(block['text'] for block in content if block['type'] == 'text') == 'long response'
        # Preview may add a cache breakpoint to a copy of the final block.
        assert [{k: v for k, v in block.items() if k != 'cache_control'} for block in content] == original['native_output_items']
    else:
        assert 'long response' in str(preview['payload'])
    assert not any(i['severity'] == 'error' for i in preview['issues'])
    created = await env.client.post(url + '/branch', json=body)
    assert created.status == 200, await created.text()
    bid = (await created.json())['conversation']['conversationUuid']
    branch_view = await (await env.client.get(f'/api/conversations/{bid}/context-editor')).json()
    # Reopen from saved window + anchored private sidecar, not the creation DTO.
    edited = branch_view['baseline']['entries'][1]['message']
    assert edited.get('native_output_items') == original.get('native_output_items')
    if protocol == 'anthropic':
        branch_row = await env.server._conversation_row(123, bid)
        restored = await MessageDAO(env.db).load_controller_model_context(
            branch_row['internal_chat_id'], conversation_uuid=bid,
            session_id=await MessageDAO(env.db).current_session_uuid(branch_row['internal_chat_id']),
            protocol='anthropic', model='gpt', model_label='openai/gpt')
        from app.llm.anthropic import _to_anthropic
        assert _to_anthropic(restored['messages'])[1]['content'] == original['native_output_items']


def test_readable_responses_projection_requires_explicit_thinking_removal():
    base = fixture_document()
    edit = copy.deepcopy(base)
    _, issues = compile_document(base, edit, protocol='chat', available_tools=['Read'])
    assert any(i['code'] == 'native_protocol_mismatch' for i in issues)
    edit['entries'][1]['message']['native_output_items'].pop(0)
    doc, issues = compile_document(base, edit, protocol='chat', available_tools=['Read'])
    assert not any(i['severity'] == 'error' for i in issues)
    assert any(i['code'] == 'readable_native_projection' for i in issues)
    assert 'native_output_items' not in doc['entries'][1]['message']
    assert edit['entries'][1]['message']['native_output_items']


async def test_large_draft_rollback_and_authorization(web_env, monkeypatch):
    env = web_env
    row, url, body, _, _ = await setup(env)
    body['working']['entries'][0]['message']['content'] = '大' * 500000
    assert (await env.client.put(url + '/draft', json=body)).status == 200
    assert (await (await env.client.get(url)).json())['draft']['working'] == body['working']
    assert (await env.client.post(url + '/preview', data='{"broken"')).status == 400
    foreign = await env.server._create_web_conversation(456)
    assert (await env.client.get(f"/api/conversations/{foreign['conversation_uuid']}/context-editor")).status == 404
    from app.context.runtime import ContextManager
    async def fail(*args, **kwargs):
        raise ValueError('injected_checkpoint_failure')
    monkeypatch.setattr(ContextManager, 'checkpoint', fail)
    before = (await (await env.db.conn.execute('SELECT COUNT(*) n FROM web_conversations')).fetchone())['n']
    failed = await env.client.post(url + '/branch', json=body)
    assert failed.status == 400, await failed.text()
    after = (await (await env.db.conn.execute('SELECT COUNT(*) n FROM web_conversations')).fetchone())['n']
    assert after == before
    assert (await (await env.db.conn.execute('SELECT COUNT(*) n FROM context_editor_branches')).fetchone())['n'] == 0


async def test_task_memory_snapshot_token_stable_and_frozen_in_branch(web_env):
    from app.task_memory import TaskMemoryDAO, SCOPE_CONVERSATION
    env = web_env
    row, url, _, backend, _ = await setup(env)
    await TaskMemoryDAO(env.db).create(conversation_uuid=row['conversation_uuid'], scope_type=SCOPE_CONVERSATION,
        name='Preference', body='original preference')
    first = await (await env.client.get(url)).json()
    second = await (await env.client.get(url)).json()
    assert first['snapshotToken'] == second['snapshotToken']
    body = {'baseline': first['baseline'], 'working': copy.deepcopy(first['baseline']), 'snapshotToken': first['snapshotToken'], 'mode': 'compatible', 'targetModel': 'openai/gpt'}
    # Explicitly remove the runtime memory message; do not regenerate it later.
    body['working']['entries'] = [e for e in body['working']['entries'] if 'original preference' not in str(e)]
    result = await env.client.post(url + '/branch', json=body)
    assert result.status == 200, await result.text()
    bid = (await result.json())['conversation']['conversationUuid']
    branch = await env.server._conversation_row(123, bid)
    await TaskMemoryDAO(env.db).create(conversation_uuid=bid, scope_type=SCOPE_CONVERSATION, name='New preference', body='do not silently inject this')
    live = _WebLiveStream(bid, branch['internal_chat_id'])
    await live.publish({'type': 'accepted', 'turnUuid': 'frozen-memory'})
    assert await env.server._run_web_turn(branch['internal_chat_id'], 'continue', _WebStreamRenderer(live), conversation=branch, root_turn_uuid='frozen-memory')
    assert 'original preference' not in str(backend.seen_convos[-1])
    assert 'do not silently inject this' not in str(backend.seen_convos[-1])


@pytest.mark.parametrize('inherited,explicit', [(False, False), (True, False), (False, True)])
async def test_context_branch_preserves_or_explicitly_establishes_title_lock(web_env, inherited, explicit):
    source, url, body, _backend, _executed = await setup(web_env)
    await web_env.db.conn.execute('UPDATE web_conversations SET title_manual=? WHERE conversation_uuid=?', (int(inherited), source['conversation_uuid']))
    await web_env.db.conn.commit()
    if explicit:
        body['title'] = '用户选择的分支名称'
    response = await web_env.client.post(url + '/branch', json=body)
    assert response.status == 200, await response.text()
    public = (await response.json())['conversation']
    branch = await web_env.server._conversation_row(123, public['conversationUuid'])
    assert bool(branch['title_manual']) is (inherited or explicit)
    assert public['titleManual'] is (inherited or explicit)
