import copy
import json

import pytest

from app.db.dao import MessageDAO
from app.llm.anthropic import AnthropicBackend
from app.llm.events import ToolCall
from app.llm.openai_responses import OpenAIResponsesBackend
from app.web_admin import _WebLiveStream, _WebStreamRenderer
from tests.test_context_editor import setup
from tests.test_context_editor import web_env as web_env


@pytest.mark.parametrize('protocol', ['anthropic', 'responses'])
@pytest.mark.parametrize('destination', ['branch', 'original'])
async def test_preview_and_both_save_targets_use_repaired_context_without_executing_history(web_env, protocol, destination):
    env = web_env
    row, url, _, backend, executed = await setup(env)
    dao = MessageDAO(env.db)
    chat, conv = row['internal_chat_id'], row['conversation_uuid']
    await dao.add(chat, 'assistant', '', tool_calls=[ToolCall('historic', 'Read', '{}')])
    await dao.add(chat, 'tool', 'historic output', tool_call_id='historic', name='Read')
    env.server.config.models.providers['openai'].protocol = protocol
    real = (AnthropicBackend if protocol == 'anthropic' else OpenAIResponsesBackend)(None, 'https://isolated.invalid', 'fixture')
    backend.protocol = protocol
    backend.build_payload = real.build_payload
    data = await (await env.client.get(url)).json()
    history = [entry['message'] for entry in data['baseline']['entries']]
    for index, message in enumerate(history):
        if message['role'] != 'assistant':
            continue
        calls = message.get('tool_calls') or []
        message['reasoning'] = 'old thinking'
        if protocol == 'anthropic':
            message['signature'] = f'signature-{index}'
            native = [{'type': 'thinking', 'thinking': 'old thinking', 'signature': f'signature-{index}'}]
            if message.get('content'):
                native.append({'type': 'text', 'text': message['content']})
            native.extend({'type': 'tool_use', 'id': call['id'], 'name': call['name'], 'input': json.loads(call['arguments'])} for call in calls)
        else:
            native = [{'type': 'reasoning', 'id': f'r{index}', 'encrypted_content': f'opaque-{index}'}]
            if message.get('content'):
                native.append({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': message['content']}]})
            native.extend({'type': 'function_call', 'call_id': call['id'], 'name': call['name'], 'arguments': call['arguments']} for call in calls)
        message['native_output_items'] = native
    await dao.save_controller_model_context(chat, conversation_uuid=conv,
        session_id=await dao.current_session_uuid(chat), protocol=protocol, model='gpt', model_label='openai/gpt', state={'messages': history})
    data = await (await env.client.get(url)).json()
    body = {'baseline': data['baseline'], 'working': copy.deepcopy(data['baseline']), 'mode': 'compatible',
            'targetModel': 'openai/gpt', 'snapshotToken': data['snapshotToken'], 'revision': 0}
    body['working']['entries'][0]['message']['content'] = 'edited question'
    raw_working = copy.deepcopy(body['working'])
    preview_response = await env.client.post(url + '/preview', json=body)
    assert preview_response.status == 200, await preview_response.text()
    preview = await preview_response.json()
    assert not [issue for issue in preview['issues'] if issue['severity'] == 'error']
    assert len([issue for issue in preview['issues'] if issue['code'] == 'reasoning_cleanup']) == 1
    assert '2 组' in next(i['text'] for i in preview['issues'] if i['code'] == 'reasoning_cleanup')
    payload = str(preview['payload'])
    assert 'old thinking' not in payload and 'signature-' not in payload and 'opaque-' not in payload
    assert 'historic' in payload and 'historic output' in payload and 'long response' in payload
    assert backend.calls == 0 and executed == []
    # Raw preview still keeps the full original material and cannot be applied.
    raw_preview = await (await env.client.post(url + '/preview', json={**body, 'mode': 'raw'})).json()
    assert 'signature-' in str(raw_preview['document']) if protocol == 'anthropic' else 'opaque-' in str(raw_preview['document'])
    assert body['working'] == raw_working
    result = await env.client.post(url + '/branch', json=body) if destination == 'branch' else await env.client.put(url, json=body)
    assert result.status == 200, await result.text()
    target = (await result.json())['conversation']['conversationUuid']
    reopened = await (await env.client.get(f'/api/conversations/{target}/context-editor')).json()
    assert [e['message']['content'] for e in reopened['baseline']['entries']] == [e['message']['content'] for e in preview['document']['entries']]
    for entry in reopened['baseline']['entries']:
        message = entry['message']
        assert not message.get('signature') and not message.get('reasoning')
        assert not any(n['type'] in {'reasoning', 'thinking', 'redacted_thinking'} for n in message.get('native_output_items') or [])
    package = json.loads((await (await env.db.conn.execute('SELECT document_json FROM context_editor_branches WHERE conversation_uuid=?', (target,))).fetchone())[0])
    assert package['working'] == raw_working
    current = await env.server._conversation_row(123, target)
    live = _WebLiveStream(target, current['internal_chat_id'])
    await live.publish({'type': 'accepted', 'turnUuid': 'after-context-repair'})
    assert await env.server._run_web_turn(current['internal_chat_id'], 'continue', _WebStreamRenderer(live), conversation=current, root_turn_uuid='after-context-repair'), live.snapshot()
    sent = str(backend.seen_convos[-1])
    assert 'old thinking' not in sent and 'signature-' not in sent and 'opaque-' not in sent
    assert 'historic output' in sent and 'long response' in sent
    assert executed == []
