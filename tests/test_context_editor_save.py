from __future__ import annotations

import copy
import json

import pytest

from app.db.dao import MessageDAO
from app.web_admin import _WebLiveStream, _WebStreamRenderer
from tests.test_context_editor import setup
from tests.test_context_editor import web_env as web_env


async def test_save_original_preserves_transcript_and_continues_after_reload(web_env):
    env = web_env
    row, url, body, backend, executed = await setup(env)
    chat, conv = row['internal_chat_id'], row['conversation_uuid']
    original_rows = [dict(r) for r in await (await env.db.conn.execute('SELECT * FROM messages WHERE chat_id=? ORDER BY id', (chat,))).fetchall()]
    body['working']['system'] = ''
    body['working']['tools'][0]['description'] = 'edited tool'
    body['working']['entries'] = body['working']['entries'][:1]
    body['working']['entries'][0]['message']['content'] = 'edited question'
    result = await env.client.put(url, json=body)
    assert result.status == 200, await result.text()
    saved = await result.json()
    assert saved['conversation']['conversationUuid'] == conv
    assert saved['draft']['revision'] == 1
    assert saved['snapshotToken'] != body['snapshotToken']
    assert saved['baseline']['system'] == ''
    assert [e['message']['content'] for e in saved['baseline']['entries']] == ['edited question']
    assert executed == [] and backend.calls == 0
    assert [dict(r) for r in await (await env.db.conn.execute('SELECT * FROM messages WHERE chat_id=? ORDER BY id', (chat,))).fetchall()] == original_rows
    assert (await (await env.db.conn.execute('SELECT COUNT(*) FROM web_conversations')).fetchone())[0] == 1
    assert (await env.client.put(url, json=body)).status == 409
    from app.db.engine import DB
    reloaded = DB(env.db.path)
    await reloaded.connect()
    try:
        dao = MessageDAO(reloaded)
        state = await dao.load_controller_model_context(chat, conversation_uuid=conv,
            session_id=await dao.current_session_uuid(chat), protocol='chat', model='gpt', model_label='openai/gpt')
        assert state['messages'][0]['content'] == 'edited question'
    finally:
        await reloaded.close()
    for index in range(2):
        if index:
            await env.db.conn.execute('UPDATE web_conversations SET model=? WHERE conversation_uuid=?', ('openai/cheap', conv))
            await env.db.conn.commit()
        current = await env.server._conversation_row(123, conv)
        live = _WebLiveStream(conv, chat)
        await live.publish({'type': 'accepted', 'turnUuid': f'edited-{index}'})
        assert await env.server._run_web_turn(chat, 'continue', _WebStreamRenderer(live), conversation=current, root_turn_uuid=f'edited-{index}'), live.snapshot()
        sent = str(backend.seen_convos[-1])
        assert 'edited question' in sent
        assert 'original question' not in sent and 'long response' not in sent
        assert backend.seen_systems[-1] == ''
        assert backend.seen_tools[-1][0]['description'] == 'edited tool'


@pytest.mark.parametrize('mode', ['empty', 'repeated'])
async def test_save_original_empty_or_repeated_edits_do_not_resurrect_removed_messages(web_env, mode):
    row, url, body, backend, _ = await setup(web_env)
    body['working']['entries'] = []
    result = await web_env.client.put(url, json=body)
    assert result.status == 200, await result.text()
    if mode == 'repeated':
        data = await result.json()
        body = {k: data['draft'][k] for k in ('baseline', 'working', 'snapshotToken', 'mode', 'targetModel', 'revision')}
        body['working']['entries'] = [{'entryId': 'new', 'message': {'role': 'user', 'content': 'replacement'}}]
        result = await web_env.client.put(url, json=body)
        assert result.status == 200, await result.text()
    reopened = await (await web_env.client.get(url)).json()
    assert reopened['draft']['snapshotToken'] == reopened['snapshotToken']
    current = await web_env.server._conversation_row(123, row['conversation_uuid'])
    live = _WebLiveStream(row['conversation_uuid'], row['internal_chat_id'])
    await live.publish({'type': 'accepted', 'turnUuid': mode})
    assert await web_env.server._run_web_turn(row['internal_chat_id'], 'continue', _WebStreamRenderer(live), conversation=current, root_turn_uuid=mode), live.snapshot()
    assert 'original question' not in str(backend.seen_convos[-1])
    assert 'long response' not in str(backend.seen_convos[-1])
    if mode == 'repeated':
        assert 'replacement' in str(backend.seen_convos[-1])


async def test_save_original_conflicts_validation_authorization_and_rollback(web_env, monkeypatch):
    env = web_env
    row, url, body, _, _ = await setup(env)
    assert (await env.client.put(url, json={**body, 'mode': 'raw'})).status == 400
    invalid = copy.deepcopy(body)
    invalid['working']['entries'].append({'entryId': 'orphan', 'message': {'role': 'tool', 'tool_call_id': 'missing', 'content': 'bad'}})
    assert (await env.client.put(url, json=invalid)).status == 400
    env.server._web_starting_turns[row['conversation_uuid']] = True
    assert (await env.client.put(url, json=body)).status == 409
    env.server._web_starting_turns.clear()
    async with env.server.operation_locks.try_chat(row['internal_chat_id'], 'test') as acquired:
        assert acquired
        assert (await env.client.put(url, json=body)).status == 409
    foreign = await env.server._create_web_conversation(456)
    assert (await env.client.put(f"/api/conversations/{foreign['conversation_uuid']}/context-editor", json=body)).status == 404
    assert (await env.client.put(url + '/draft', json=body)).status == 200
    assert (await env.client.put(url, json=body)).status == 409
    body['revision'] = 1
    from app.context.store import WindowStore
    real_save = WindowStore.save
    async def fail(self, *args, **kwargs):
        await real_save(self, *args, **kwargs)
        raise ValueError('injected_failure')
    monkeypatch.setattr(WindowStore, 'save', fail)
    result = await env.client.put(url, json=body)
    assert result.status == 400, await result.text()
    assert (await (await env.client.get(url)).json())['snapshotToken'] == body['snapshotToken']
    assert (await (await env.db.conn.execute('SELECT COUNT(*) FROM context_editor_branches')).fetchone())[0] == 0
    assert (await (await env.db.conn.execute('SELECT COUNT(*) FROM context_windows')).fetchone())[0] == 0
    assert json.loads((await (await env.db.conn.execute('SELECT document_json FROM context_editor_drafts')).fetchone())[0])['working'] == body['working']
