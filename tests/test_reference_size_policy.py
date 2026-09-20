# ruff: noqa: F811
"""Normal conversation-reference size policy through real APIs, snapshots and windows."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.context.request_view import expanded_request_view
from app.context.window import RequiredContextTooLarge, estimate_request, mark_source, select_window
from app.memory.builtin import BuiltinMemoryClient
from app.reference_policy import CONVERSATION_CONTENT_LIMIT
from app.references import BUNDLE_FIELD, ReferenceError, effective_reference_text, parse_references
from app.agent.runs import RunRegistry
from app.web_console.reference_api import WebAdminReferenceMixin
from app.web_console.realtime import read_catalog
from tests.test_builtin_template_import import _login_cookie, web_env
from tests.test_reference_mentions import insert_op


def token(kind, conv, item='', query=''):
    return f'[选择来源](openbear://ref/{kind}/{conv}{"/"+item if item else ""}{"?"+query if query else ""})'


async def conversations(e):
    source = await e.server._create_web_conversation(123, title='昨日讨论', model='openai/gpt')
    current = await e.server._create_web_conversation(123, title='当前问题', model='openai/gpt')
    return source, current


@pytest.mark.parametrize('kind', ['chat', 'turn', 'message'])
@pytest.mark.parametrize('count', [20000, 20001])
async def test_unicode_boundary_counts_only_visible_body(web_env, kind, count):
    e = web_env
    source, current = await conversations(e)
    conv = source['conversation_uuid']
    body = '🙂' * count  # One Unicode character, not two UTF-16 units or four bytes.
    await insert_op(e, conv, 'msg:visible', 'turn-visible', 1, text=body)
    await insert_op(e, conv, 'tool', 'turn-visible', 2, text='PRIVATE-TOOL' * 5000, op_type='tool')
    await insert_op(e, conv, 'hidden', 'hidden', 3, payload={'text': 'PRIVATE-HIDDEN' * 5000, 'hidden': True})
    text = token(kind, conv, '' if kind == 'chat' else 'turn-visible' if kind == 'turn' else 'msg:visible')
    bundle, bindings = await e.server._prepare_reference_bundle(current, text, 'msg:selected')
    ref = bindings[0]
    assert ref['bodyChars'] == count and ref['contentLimit'] == CONVERSATION_CONTENT_LIMIT
    assert ref.get('mode', 'content') == ('mention' if count > 20000 else 'content')
    if count > 20000:
        assert ref['modeReason'] == 'conversation_too_long'
    frozen = await (await e.db.conn.execute('SELECT material_json FROM web_reference_bundles WHERE bundle_uuid=?', (bundle,))).fetchone()
    material = json.loads(frozen[0])[0]
    if count > 20000:
        assert 'content' not in material and body not in frozen[0]
    else:
        assert body in material['content']
    overlay = await e.server._reference_store().overlay([{'role': 'user', 'content': text, BUNDLE_FIELD: [bundle]}], conversation_uuid=current['conversation_uuid'])
    assert ('🙂' in overlay[0]['content']) == (count <= 20000)
    assert 'PRIVATE-' not in overlay[0]['content']


async def test_aggregate_dedup_modes_small_ranges_and_existing_priority(web_env):
    e = web_env
    source, current = await conversations(e)
    conv = source['conversation_uuid']
    for name, size, seq in [('old', 12000, 1), ('new', 8001, 2), ('fit', 8000, 3)]:
        await insert_op(e, conv, 'msg:'+name, 'turn-'+name, seq, text=name[0] * size)
    old, new, fit = [token('message', conv, 'msg:'+name) for name in ['old', 'new', 'fit']]
    store = e.server._reference_store()
    text = old+' '+old+' '+token('message', conv, 'msg:old', 'mode=mention')+' '+fit
    result = await store.resolve(text, owner=123, budget=1)
    assert result['bodyChars'] == 20000 and len(result['manifest']) == 3
    assert [r.get('mode', 'content') for r in result['bindings']] == ['content', 'content', 'mention', 'content']
    result = await store.resolve(old+' '+new+' '+new, owner=123)
    assert result['bodyChars'] == 12000
    assert [r.get('mode', 'content') for r in result['bindings']] == ['content', 'mention', 'mention']
    assert result['bindings'][1]['modeReason'] == 'conversation_total_limit'
    # A new ref inserted BEFORE an older one must not win just by text position.
    cookies = await _login_cookie(e)
    text = '原问题：\n'+new+' 普通正文 '+old+'\n保留尾文'
    response = await e.client.post('/api/references/preview', cookies=cookies, json={
        'text': text, 'conversationUuid': current['conversation_uuid'],
        'existingKeys': [parse_references(old)[0]['key'], parse_references(new)[0]['key']],
    })
    data = await response.json()
    assert response.status == 200
    assert [r.get('mode', 'content') for r in data['bindings']] == ['mention', 'content']
    effective = effective_reference_text(text, data['bindings'])
    assert effective.replace(token('message', conv, 'msg:new', 'mode=mention&reason=conversation_total_limit'), new) == text
    _, bound = await e.server._prepare_reference_bundle(current, text, 'msg:priority', existing_keys=[parse_references(old)[0]['key'], parse_references(new)[0]['key']])
    assert [r.get('mode', 'content') for r in bound] == ['mention', 'content']
    # Full conversation is too long; an independently selected short turn stays content.
    full = await store.resolve(token('chat', conv), owner=123)
    turn = await store.resolve(token('turn', conv, 'turn-fit'), owner=123, budget=1)
    recent = await store.resolve(token('chat', conv, query='scope=recent&turns=1'), owner=123, budget=1)
    assert full['manifest'][0]['modeReason'] == 'conversation_too_long'
    assert turn['bodyChars'] == recent['bodyChars'] == 8000
    assert all(r.get('mode') != 'mention' for r in turn['manifest'] + recent['manifest'])


async def test_catalog_lengths_are_metadata_only_cached_and_history_range_specific(web_env, monkeypatch):
    e = web_env
    source, _ = await conversations(e)
    conv = source['conversation_uuid']
    await insert_op(e, conv, 'q', 't', 1, text='😀' * 20000)
    await insert_op(e, conv, 'a', 't', 2, text='ANSWER', op_type='assistant_message')
    await insert_op(e, conv, 'secret-tool', 't', 3, text='TOOL-MUST-NOT-COUNT', op_type='tool')
    import app.reference_policy as policy
    original, calls = policy.history_sizes, []
    def counted(conn, uuid):
        calls.append(uuid)
        return original(conn, uuid)
    monkeypatch.setattr(policy, 'history_sizes', counted)
    cache = {}
    _, items = read_catalog(e.db.path, 123, length_cache=cache)
    item = items['chat:'+conv]
    assert item['bodyChars'] == 20006 and item['recentTurnChars'] == [20006]
    assert '😀' not in json.dumps(items) and 'ANSWER' not in json.dumps(items)
    previous = list(calls)
    read_catalog(e.db.path, 123, length_cache=cache)
    assert calls == previous  # No re-read of transcript bodies on each refresh.
    await e.db.conn.execute("UPDATE web_operations SET payload_json=?,revision=revision+1 WHERE conversation_uuid=? AND op_id='a'", (json.dumps({'text': 'GROWN-ANSWER'}), conv))
    await e.db.conn.commit()
    when, signature, value = cache[conv]
    cache[conv] = (when-3, signature, value)
    assert read_catalog(e.db.path, 123, length_cache=cache)[1]['chat:'+conv]['bodyChars'] == 20012
    cookies = await _login_cookie(e)
    catalog = await (await e.client.get('/api/reference-catalog', cookies=cookies)).json()
    assert catalog['conversationContentLimit'] == CONVERSATION_CONTENT_LIMIT
    history = await (await e.client.get(f'/api/reference-history/{conv}', cookies=cookies)).json()
    assert history['conversationContentLimit'] == CONVERSATION_CONTENT_LIMIT
    assert {row['itemId']: row['bodyChars'] for row in history['items']} == {'t': 20012, 'a': 12, 'q': 20000}


async def test_latest_send_growth_is_visible_and_preserves_question_and_copy(web_env, monkeypatch):
    e = web_env
    source, current = await conversations(e)
    conv = source['conversation_uuid']
    await insert_op(e, conv, 'msg:grow', 'growth', 1, text='原文' * 4000)
    old_source = await e.server._create_web_conversation(123, title='先选的来源', model='openai/gpt')
    await insert_op(e, old_source['conversation_uuid'], 'msg:old', 'old', 1, text='旧正文' * 4000)
    old_token = token('chat', old_source['conversation_uuid'])
    # The new selection is in FRONT of the old one; it grows after preview.
    text = '请核对这个问题，保留普通正文。\n'+token('chat', conv)+'\n结尾不许丢。 '+old_token
    order = [parse_references(old_token)[0]['key'], parse_references(token('chat', conv))[0]['key']]
    cookies = await _login_cookie(e)
    preview = await (await e.client.post('/api/references/preview', cookies=cookies, json={'text': text, 'existingKeys': order})).json()
    assert all(item.get('mode') != 'mention' for item in preview['items'])
    await e.db.conn.execute('UPDATE web_operations SET payload_json=?,revision=revision+1 WHERE conversation_uuid=?', (json.dumps({'text': '原文'*4000+'增'}), conv))
    await e.db.conn.commit()
    e.server.runs = RunRegistry()
    captured, completed = {}, asyncio.Event()
    async def fake_run(chat_id, effective, renderer, **kwargs):
        captured.update(text=effective, **kwargs)
        await renderer.finalize('fixture response')
        await renderer.close()
        completed.set()
    monkeypatch.setattr(e.server, '_run_web_turn', fake_run)
    async with e.client.ws_connect(f"/api/conversations/{current['conversation_uuid']}/ws?bootstrap=incremental", headers={'Cookie': '; '.join(f'{k}={v}' for k,v in cookies.items())}) as ws:
        await ws.send_json({'type': 'send', 'requestId': 'size-growth', 'text': text, 'files': [], 'referenceOrder': order})
        async with asyncio.timeout(10):
            while True:
                message = await ws.receive_json()
                if message.get('requestId') == 'size-growth':
                    assert message['type'] == 'ack', message
                    break
            await completed.wait()
    effective = captured['text']
    ref = parse_references(effective)[0]
    assert ref['mode'] == 'mention' and ref['modeReason'] == 'conversation_total_limit'
    assert effective.replace(token('chat', conv, query='mode=mention&reason=conversation_total_limit'), token('chat', conv)) == text
    assert parse_references(effective)[1].get('mode') != 'mention'
    operations = await e.server._web_operations(current['conversation_uuid'])
    user = next(op['payload'] for op in operations if op['opType'] == 'user_message')
    assert user['text'] == effective and user['references'][0]['mode'] == 'mention'
    assert user['references'][1].get('mode') != 'mention'
    inspected = await (await e.client.post('/api/references/inspect', cookies=cookies, json={'text': token('chat', conv, query='mode=mention&reason=conversation_total_limit'), 'bundleId': user['referenceBundleId']})).json()
    assert inspected['frozen'] and inspected['content'] == '' and inspected['item']['bodyChars'] == 8001
    copied = await e.server._duplicate_web_conversation_data(current)
    copy_ops = await e.server._web_operations(copied['conversation_uuid'])
    copy_user = next(op['payload'] for op in copy_ops if op['opType'] == 'user_message')
    assert parse_references(copy_user['text'])[0]['mode'] == 'mention'
    assert copy_user['references'][0]['modeReason'] == 'conversation_total_limit'


async def test_conversation_ignores_old_model_gate_but_memory_keeps_it(web_env):
    e = web_env
    source, current = await conversations(e)
    await insert_op(e, source['conversation_uuid'], 'one', 'one', 1, text='会话正文' * 3000)
    definition = SimpleNamespace(context_window=500000, max_tokens=500000)
    example = SimpleNamespace(config=SimpleNamespace(models=SimpleNamespace(primary='example', resolve=lambda _: ('p', definition))))
    budget = WebAdminReferenceMixin._reference_budget(example)
    assert budget == 1000
    resolved = await e.server._reference_store().resolve(token('chat', source['conversation_uuid']), owner=123, budget=budget)
    assert resolved['estimatedTokens'] > budget and resolved['materials'][0]['content']
    doc = (await BuiltinMemoryClient(e.db).tool_call('doc', {'action': 'set', 'name': 'same-size', 'content': '会话正文'*3000}))['item']
    with pytest.raises(ReferenceError, match='reference_budget_exceeded'):
        await e.server._reference_store().resolve(token('doc', doc['id']), owner=123, budget=budget)


async def test_frozen_content_and_current_question_survive_real_request_window(web_env):
    e = web_env
    source, current = await conversations(e)
    conv = source['conversation_uuid']
    await insert_op(e, conv, 'selected', 'selected', 1, text='冻结正文' * 4000)
    store = e.server._reference_store()
    old_text = token('chat', conv)
    old_bundle, _ = await e.server._prepare_reference_bundle(current, old_text, 'old')
    await e.db.conn.execute('UPDATE web_operations SET payload_json=?,revision=revision+1 WHERE conversation_uuid=?', (json.dumps({'text': '增长后正文' * 5000}), conv))
    await e.db.conn.commit()
    question = '当前用户问题不能被压掉。\n'+token('chat', conv)
    new_bundle, bindings = await e.server._prepare_reference_bundle(current, question, 'new')
    effective = effective_reference_text(question, bindings)
    old = mark_source({'role': 'user', 'content': old_text, BUNDLE_FIELD: [old_bundle]}, kind='human', source_id='old-input', run_root_turn_uuid='old')
    answer = mark_source({'role': 'assistant', 'content': '旧轮结束'}, source_id='old-answer', run_root_turn_uuid='old')
    now = mark_source({'role': 'user', 'content': effective, BUNDLE_FIELD: [new_bundle]}, kind='human', source_id='new-input', run_root_turn_uuid='new')
    messages = [old, answer, now]
    expanded = await store.overlay(messages, conversation_uuid=current['conversation_uuid'])
    assert '冻结正文' in expanded[0]['content'] and '增长后正文' not in expanded[0]['content']
    assert '增长后正文' not in expanded[-1]['content'] and '"tool": "History"' in expanded[-1]['content']
    view = expanded_request_view(expanded)
    estimate = lambda selected: estimate_request(view(selected), system='system', tools=[])
    selected = select_window(messages, estimate=estimate, target=1800, input_ceiling=2200, active_run_root_turn_uuid='new')
    assert now in selected.messages and old not in selected.messages
    actual_request = view(selected.messages)
    assert '当前用户问题不能被压掉。' in actual_request[-1]['content']
    assert conv in actual_request[-1]['content'] and '"action": "read"' in actual_request[-1]['content']
    # A genuinely too-small request window still fails explicitly, without
    # silently deleting the pinned question or changing an older frozen bundle.
    required = mark_source({'role': 'user', 'content': '保留当前问题 '+old_text, BUNDLE_FIELD: [old_bundle]}, kind='human', source_id='required', run_root_turn_uuid='new')
    required_view = expanded_request_view(await store.overlay([required], conversation_uuid=current['conversation_uuid']))
    with pytest.raises(RequiredContextTooLarge):
        select_window([required], estimate=lambda selected: estimate_request(required_view(selected), system='system', tools=[]), target=800, input_ceiling=1000, active_run_root_turn_uuid='new')
    assert required['content'].startswith('保留当前问题 ')
