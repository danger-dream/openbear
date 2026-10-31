"""Offline terminal-text and complete Claude-native continuation regressions."""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from app.agent.native_continuation import (deserialize_messages, native_items_for_tool_calls,
                                           sanitize_paired_messages, serialize_messages, validate_model_context)
from app.context.editor import compile_document, document
from app.llm.anthropic import AnthropicBackend, _to_anthropic
from app.llm.base import aggregate
from app.llm.events import ToolCall
from app.llm.openai_responses import OpenAIResponsesBackend, _to_responses_input
from tests.conftest import make_client, sse_response


def message(item_id, text):
    return {"type": "message", "id": item_id, "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text}]}


def frame(typ, **fields):
    return [f"event: {typ}", f"data: {json.dumps({'type': typ, **fields})}", ""]


@pytest.mark.parametrize("source", ["terminal", "done", "prefix_delta", "full_delta"])
async def test_responses_visible_text_fallback_matches_native_order_without_duplicates(source):
    output = [message('m0', 'AB'), {"type": "function_call", "id": "fc_a", "call_id": "a",
                                    "name": "lookup", "arguments": "{}"}, message('m2', 'C')]
    lines = []
    if source in {'prefix_delta', 'full_delta'}:
        lines += frame('response.output_item.added', output_index=0, item={'type': 'message', 'id': 'm0'})
        lines += frame('response.output_text.delta', item_id='m0', output_index=0,
                       content_index=0, delta='A' if source == 'prefix_delta' else 'AB')
        if source == 'full_delta':
            lines += frame('response.output_text.delta', item_id='m2', output_index=2,
                           content_index=0, delta='C')
    if source in {'done', 'prefix_delta', 'full_delta'}:
        lines += frame('response.output_item.done', output_index=2, item=output[2])
        lines += frame('response.output_item.done', output_index=0, item=output[0])
        lines += frame('response.output_item.done', output_index=1, item=output[1])
    lines += frame('response.completed', response={'status': 'completed', 'output': output})
    client = make_client(lambda request: sse_response(lines))
    try:
        backend = OpenAIResponsesBackend(client, 'https://unused.test/v1', 'offline')
        result = await aggregate(backend.stream([{'role': 'user', 'content': 'q'}], model='gpt-5',
                                                native_continuation=True))
        assert result.text == 'ABC'
        assert result.native_output_items == output
        assert [call.id for call in result.tool_calls] == ['a']
        assistant = {'role': 'assistant', 'content': result.text, 'tool_calls': result.tool_calls,
                     'native_output_items': native_items_for_tool_calls(result.native_output_items,
                                                                       result.tool_calls, result.tool_calls,
                                                                       has_content=True)}
        assert _to_responses_input([assistant, {'role': 'tool', 'tool_call_id': 'a', 'content': 'ok'}])[:3] == output
    finally:
        await client.close()


@pytest.mark.parametrize('early', ['done', 'delta_then_late_done', 'complete_delta'])
async def test_responses_mixed_snapshot_and_later_delta_keep_native_order(early):
    first = message('m0', 'AA' if early == 'delta_then_late_done' else 'A')
    second = message('m1', 'B')
    frames = []
    if early == 'done':
        frames.append(('response.output_item.done', {'output_index': 0, 'item': first}))
    else:
        frames.append(('response.output_item.added', {'output_index': 0, 'item': {'type': 'message', 'id': 'm0'}}))
        frames.append(('response.output_text.delta', {'output_index': 0, 'item_id': 'm0', 'content_index': 0,
                                                       'delta': 'A'}))
    frames.append(('response.output_text.delta', {'output_index': 1, 'item_id': 'm1', 'content_index': 0,
                                                   'delta': 'B'}))
    if early != 'done':
        frames.append(('response.output_item.done', {'output_index': 0, 'item': first}))
    frames.append(('response.completed', {'response': {'status': 'completed', 'output': [first, second]}}))

    class Offline:
        async def post_sse(self, *_args, **_kwargs):
            for typ, body in frames:
                yield typ, {'type': typ, **body}

    backend = OpenAIResponsesBackend(Offline(), 'http://offline.invalid', 'offline')
    result = await aggregate(backend.stream([{'role': 'user', 'content': 'go'}], model='x', native_continuation=True))
    assert result.text == ('AAB' if early == 'delta_then_late_done' else 'AB')
    assert result.native_output_items == [first, second]
    assert _to_responses_input([{'role': 'assistant', 'content': result.text,
                                 'native_output_items': result.native_output_items}]) == [first, second]
    if early == 'complete_delta':
        stream = backend.stream([{'role': 'user', 'content': 'go'}], model='x')
        assert (await anext(stream)).text == 'A'  # First delta is not held until terminal.
        await stream.aclose()


@pytest.mark.parametrize('first_delta', [False, True])
async def test_responses_two_content_parts_wait_for_earlier_tail(first_delta):
    output = {'type': 'message', 'id': 'm0', 'role': 'assistant', 'status': 'completed',
              'content': [{'type': 'output_text', 'text': 'AA'}, {'type': 'output_text', 'text': 'B'}]}
    events = [('response.output_item.added', {'output_index': 0, 'item': {'type': 'message', 'id': 'm0'}})]
    if first_delta:
        events.append(('response.output_text.delta', {'output_index': 0, 'item_id': 'm0',
                                                      'content_index': 0, 'delta': 'A'}))
    events.append(('response.output_text.delta', {'output_index': 0, 'item_id': 'm0',
                                                  'content_index': 1, 'delta': 'B'}))
    if first_delta:
        events.append(('response.output_text.done', {'output_index': 0, 'item_id': 'm0',
                                                     'content_index': 0, 'text': 'AA'}))
    events += [('response.output_item.done', {'output_index': 0, 'item': output}),
               ('response.completed', {'response': {'status': 'completed', 'output': [output]}})]

    class Offline:
        async def post_sse(self, *_args, **_kwargs):
            for typ, body in events:
                yield typ, {'type': typ, **body}

    result = await aggregate(OpenAIResponsesBackend(Offline(), 'http://offline.invalid', 'offline').stream(
        [{'role': 'user', 'content': 'go'}], model='x', native_continuation=True))
    assert result.text == 'AAB'
    assert result.native_output_items == [output]


@pytest.mark.parametrize('stream', [False, True])
async def test_claude_full_blocks_round_trip_tool_filter_and_context_editor(stream):
    blocks = [
        {'type': 'thinking', 'thinking': 'first', 'signature': 's1'},
        {'type': 'text', 'text': 'A'},
        {'type': 'thinking', 'thinking': 'second', 'signature': 's2'},
        {'type': 'text', 'text': 'B'},
        {'type': 'tool_use', 'id': 'a', 'name': 'lookup', 'input': {'n': 1}},
        {'type': 'tool_use', 'id': 'b', 'name': 'lookup', 'input': {'n': 2}},
    ]
    if stream:
        lines = []
        for idx, block in enumerate(blocks):
            initial = {**block}
            if block['type'] in {'text', 'thinking'}:
                initial['text' if block['type'] == 'text' else 'thinking'] = ''
            if block['type'] == 'tool_use':
                initial['input'] = {}
            lines += frame('content_block_start', index=idx, content_block=initial)
            if block['type'] == 'text':
                lines += frame('content_block_delta', index=idx, delta={'type': 'text_delta', 'text': block['text']})
            elif block['type'] == 'thinking':
                lines += frame('content_block_delta', index=idx, delta={'type': 'thinking_delta', 'thinking': block['thinking']})
            elif block['type'] == 'tool_use':
                lines += frame('content_block_delta', index=idx, delta={
                    'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])})
            lines += frame('content_block_stop', index=idx)
        lines += frame('message_delta', delta={'stop_reason': 'tool_use'}, usage={})
        client = make_client(lambda request: sse_response(lines))
    else:
        client = make_client(lambda request: httpx.Response(200, json={
            'content': blocks, 'stop_reason': 'tool_use', 'usage': {}}))
    try:
        backend = AnthropicBackend(client, 'https://unused.test/v1', 'offline')
        user = {'role': 'user', 'content': 'q'}
        result = (await aggregate(backend.stream([user], model='claude')) if stream else
                  await backend.complete([user], model='claude'))
        assert result.text == 'AB' and result.reasoning == 'firstsecond'
        assert result.native_output_items == blocks
        assert [call.id for call in result.tool_calls] == ['a', 'b']
        assert native_items_for_tool_calls(blocks, result.tool_calls, result.tool_calls[:1],
                                           has_content=True, has_reasoning=True) == []
        assistant = {'role': 'assistant', 'content': result.text, 'reasoning': result.reasoning,
                     'signature': result.signature, 'tool_calls': result.tool_calls,
                     'native_output_items': native_items_for_tool_calls(blocks, result.tool_calls,
                                                                        result.tool_calls, has_content=True,
                                                                        has_reasoning=True)}
        tool_results = [{'role': 'tool', 'tool_call_id': call.id, 'content': call.id}
                        for call in result.tool_calls]
        messages = deserialize_messages(serialize_messages([user, assistant, *tool_results]))
        assert validate_model_context(messages)
        assert _to_anthropic(messages)[1]['content'] == blocks
        assert _to_anthropic(messages, include_thinking=False)[1]['content'] == blocks  # active tool round
        safe, dropped = sanitize_paired_messages(messages[:-1])
        assert dropped == 1 and 'native_output_items' not in safe[1]
        doc = document('s', [{'name': 'lookup', 'parameters': {'type': 'object'}}], messages,
                       origin={'protocol': 'anthropic', 'modelLabel': 'p/claude'})
        compiled, issues = compile_document(doc, copy.deepcopy(doc), protocol='anthropic',
                                             model_label='p/claude', available_tools=['lookup'])
        assert not [issue for issue in issues if issue['severity'] == 'error']
        assert compiled['entries'][1]['message']['native_output_items'] == blocks
        edit = copy.deepcopy(doc)
        edit['entries'][1]['message']['content'] = 'changed'
        _, issues = compile_document(doc, edit, protocol='anthropic', model_label='p/claude',
                                      available_tools=['lookup'])
        assert 'reasoning_cleanup' in {issue['code'] for issue in issues}
        assert 'signed_prefix_changed' not in {issue['code'] for issue in issues}
        assert 'native_conflict' in {issue['code'] for issue in issues}
    finally:
        await client.close()


def test_unsigned_claude_native_can_project_to_chat_only_after_safe_validation():
    native = [{'type': 'text', 'text': 'A'},
              {'type': 'tool_use', 'id': 'a', 'name': 'lookup', 'input': {'n': 1}},
              {'type': 'text', 'text': 'B'}]
    messages = [{'role': 'assistant', 'content': 'AB',
                 'tool_calls': [{'id': 'a', 'name': 'lookup', 'arguments': '{"n":1}'}],
                 'native_output_items': native},
                {'role': 'tool', 'tool_call_id': 'a', 'content': 'ok'}]
    base = document('s', [{'name': 'lookup', 'parameters': {'type': 'object'}}], messages,
                    origin={'protocol': 'anthropic', 'modelLabel': 'p/claude'})
    converted, issues = compile_document(base, copy.deepcopy(base), protocol='chat',
                                         available_tools=['lookup'])
    assert not [i for i in issues if i['severity'] == 'error']
    assert 'native_output_items' not in converted['entries'][0]['message']
    assert converted['entries'][0]['message']['content'] == 'AB'
    assert base['entries'][0]['message']['native_output_items'] == native
    edit = copy.deepcopy(base)
    edit['entries'][0]['message']['native_output_items'][0]['text'] = 'X'
    converted, issues = compile_document(base, edit, protocol='anthropic', available_tools=['lookup'])
    assert not [i for i in issues if i['severity'] == 'error']
    assert converted['entries'][0]['message']['content'] == 'XB'
    edit['entries'][0]['message']['content'] = 'different'
    _, issues = compile_document(base, edit, protocol='anthropic', available_tools=['lookup'])
    assert 'native_conflict' in {i['code'] for i in issues}
    malformed = copy.deepcopy(base)
    malformed['entries'][0]['message']['native_output_items'][0]['text'] = {'bad': 'text'}
    _, issues = compile_document(base, malformed, protocol='anthropic', available_tools=['lookup'])
    assert 'native_shape' in {i['code'] for i in issues}
