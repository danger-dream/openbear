import copy
import json

import pytest

from app.context.editor import compile_document, document


def history_document(protocol='anthropic', groups=3):
    messages = []
    for index in range(groups):
        call = {'id': f'c{index}', 'name': 'Read', 'arguments': json.dumps({'path': str(index)})}
        if protocol == 'anthropic':
            native = [
                {'type': 'thinking', 'thinking': f'thinking-{index}', 'signature': f'sig-{index}', 'vendor': index},
                {'type': 'redacted_thinking', 'data': f'opaque-{index}'},
                {'type': 'text', 'text': f'answer-{index}', 'citations': [{'vendor': index}]},
                {'type': 'tool_use', 'id': call['id'], 'name': call['name'], 'input': {'path': str(index)}, 'vendor': index},
            ]
            protected = {'reasoning': f'thinking-{index}', 'signature': f'sig-{index}'}
        else:
            native = [
                {'type': 'reasoning', 'id': f'r{index}', 'encrypted_content': f'opaque-{index}', 'summary': [{'type': 'summary_text', 'text': f'thinking-{index}'}]},
                {'type': 'message', 'id': f'm{index}', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': f'answer-{index}', 'annotations': [{'vendor': index}]}]},
                {'type': 'function_call', 'call_id': call['id'], 'name': call['name'], 'arguments': call['arguments'], 'vendor': index},
            ]
            protected = {'reasoning': f'thinking-{index}'}
        messages.extend([
            {'role': 'user', 'content': f'question-{index}'},
            {'role': 'assistant', 'content': f'answer-{index}', 'tool_calls': [call], 'native_output_items': native, **protected},
            {'role': 'tool', 'content': f'result-{index}', 'name': 'Read', 'tool_call_id': call['id']},
        ])
    return document('system', [{'name': 'Read', 'parameters': {'type': 'object'}}], messages,
                    origin={'protocol': protocol, 'modelLabel': 'p/model'})


def compile(base, edit, protocol=None, **kwargs):
    return compile_document(base, edit, protocol=protocol or base['origin']['protocol'],
                            model_label=kwargs.pop('model_label', 'p/model'), available_tools=['Read'], **kwargs)


def assert_no_errors(issues):
    assert not [issue for issue in issues if issue['severity'] == 'error'], issues


@pytest.mark.parametrize('protocol', ['anthropic', 'responses'])
@pytest.mark.parametrize('change', ['prefix', 'system', 'tools', 'model', 'protocol', 'delete', 'reorder'])
def test_compatibility_automatically_resets_only_affected_reasoning_and_preserves_tools(protocol, change):
    base = history_document(protocol)
    original = copy.deepcopy(base)
    edit = copy.deepcopy(base)
    target_protocol, target_model = protocol, 'p/model'
    if change == 'prefix':
        edit['entries'][0]['message']['content'] = 'edited question'
    elif change == 'system':
        edit['system'] = 'edited system'
    elif change == 'tools':
        edit['tools'][0]['description'] = 'edited description'
    elif change == 'model':
        target_model = 'p/new-model'
    elif change == 'protocol':
        target_protocol = 'chat'
    elif change == 'delete':
        edit['entries'].pop(0)
    else:
        edit['entries'] = edit['entries'][3:] + edit['entries'][:3]
    before = copy.deepcopy(edit)
    compiled, issues = compile(base, edit, target_protocol, model_label=target_model)
    assert_no_errors(issues)
    notices = [issue for issue in issues if issue['code'] == 'reasoning_cleanup']
    assert len(notices) == 1 and '3 组' in notices[0]['text']
    assert edit == before and base == original
    assert [e['message'].get('tool_calls') for e in compiled['entries']] == [e['message'].get('tool_calls') for e in edit['entries']]
    assert [(e['message'].get('tool_call_id'), e['message'].get('content')) for e in compiled['entries']] == [(e['message'].get('tool_call_id'), e['message'].get('content')) for e in edit['entries']]
    for entry in compiled['entries']:
        message = entry['message']
        assert 'signature' not in message and 'reasoning' not in message
        native = message.get('native_output_items') or []
        assert not any(item['type'] in {'thinking', 'redacted_thinking', 'reasoning'} for item in native)
        if native:
            original = next(e['message']['native_output_items'] for e in edit['entries'] if e['entryId'] == entry['entryId'])
            kept = [item for item in original if item['type'] not in {'thinking', 'redacted_thinking', 'reasoning'}]
            if protocol == 'responses':
                kept = [{k: v for k, v in item.items() if k != 'id'} for item in kept]
            assert native == kept


def test_responses_items_lose_ids_only_where_their_reasoning_was_dropped():
    from app.agent.native_continuation import deserialize_messages
    from app.llm.openai_responses import _to_responses_input
    base = history_document('responses', groups=3)
    for index, entry in enumerate(base['entries'][1::3]):
        entry['message']['native_output_items'][2]['id'] = f'fc{index}'
    # The last turn never had reasoning: it is affected by the edit, yet nothing is dropped.
    last = base['entries'][7]['message']
    last.pop('reasoning')
    last['native_output_items'] = last['native_output_items'][1:]
    edit = copy.deepcopy(base)
    edit['entries'][3]['message']['content'] = 'edited second question'
    compiled, issues = compile(base, edit)
    assert_no_errors(issues)
    untouched, cleaned, plain = (compiled['entries'][i]['message']['native_output_items'] for i in (1, 4, 7))
    assert [item.get('id') for item in untouched] == ['r0', 'm0', 'fc0']
    assert [item['type'] for item in cleaned] == ['message', 'function_call']
    assert not any('id' in item for item in cleaned)
    assert cleaned[1]['call_id'] == 'c1' and cleaned[1]['vendor'] == 1
    assert cleaned[0]['content'] == edit['entries'][4]['message']['native_output_items'][1]['content']
    assert [item.get('id') for item in plain] == ['m2', 'fc2']
    assert edit['entries'][4]['message']['native_output_items'][1]['id'] == 'm1', 'the editor document keeps its ids'
    wire = _to_responses_input(deserialize_messages([e['message'] for e in compiled['entries']]))
    calls = [item['call_id'] for item in wire if item.get('type') == 'function_call']
    outputs = [item['call_id'] for item in wire if item.get('type') == 'function_call_output']
    assert calls == outputs == ['c0', 'c1', 'c2']


@pytest.mark.parametrize('protocol', ['anthropic', 'responses'])
def test_earlier_untouched_reasoning_stays_exact_and_notifications_are_aggregated(protocol):
    base = history_document(protocol, groups=20)
    edit = copy.deepcopy(base)
    edit['entries'][3]['message']['content'] = 'change only from second turn'
    compiled, issues = compile(base, edit)
    assert_no_errors(issues)
    assert compiled['entries'][:3] == base['entries'][:3]
    assert len([issue for issue in issues if issue['code'] == 'reasoning_cleanup']) == 1
    assert '19 组' in next(i['text'] for i in issues if i['code'] == 'reasoning_cleanup')
    assert len([issue for issue in issues if issue['code'] == 'signature_unverified']) <= 1
    assert not any(issue['code'] in {'signed_prefix_changed', 'signed_model_change'} for issue in issues)
    assert len(issues) <= 2


@pytest.mark.parametrize('protocol', ['anthropic', 'responses'])
def test_new_user_suffix_does_not_invalidate_prior_signed_turns(protocol):
    base = history_document(protocol, groups=20)
    edit = copy.deepcopy(base)
    edit['entries'].append({'entryId': 'new', 'message': {'role': 'user', 'content': 'continue'}})
    compiled, issues = compile(base, edit)
    assert_no_errors(issues)
    assert compiled == edit
    assert not any(issue['code'] == 'reasoning_cleanup' for issue in issues)
    assert len(issues) <= 1


@pytest.mark.parametrize('protocol', ['anthropic', 'responses'])
def test_thinking_only_empty_turn_is_omitted_from_request_but_kept_in_editor(protocol):
    base = history_document(protocol, groups=1)
    assistant = base['entries'][1]['message']
    assistant['content'] = ''
    assistant.pop('tool_calls')
    assistant['native_output_items'] = assistant['native_output_items'][:1]
    base['entries'].pop()
    edit = copy.deepcopy(base)
    edit['system'] = 'edited'
    compiled, issues = compile(base, edit)
    assert_no_errors(issues)
    assert len(compiled['entries']) == 1 and compiled['entries'][0]['message']['role'] == 'user'
    assert len(edit['entries']) == 2 and edit['entries'][1]['message']['native_output_items']


@pytest.mark.parametrize('protocol', ['anthropic', 'responses'])
def test_raw_preview_keeps_opaque_state_and_structural_errors_are_not_fabricated_away(protocol):
    base = history_document(protocol)
    edit = copy.deepcopy(base)
    edit['system'] = 'edited'
    compiled, issues = compile(base, edit, repair_reasoning=False)
    assert compiled == edit
    assert not any(issue['code'] == 'reasoning_cleanup' for issue in issues)
    if protocol == 'anthropic':
        assert any(issue['code'] == 'signed_prefix_changed' for issue in issues)
    edit['entries'] = [e for e in edit['entries'] if e['message'].get('tool_call_id') != 'c0']
    _, issues = compile(base, edit)
    assert any(issue['code'] == 'missing_tool_results' for issue in issues)
