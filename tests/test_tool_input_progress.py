from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.loop import Agent
from app.db.engine import DB
from app.llm.anthropic import AnthropicBackend
from app.llm.events import StreamEvent, ToolCall
from app.llm.openai_chat import OpenAIChatBackend
from app.llm.openai_responses import OpenAIResponsesBackend
from app.llm.tool_input import ToolInputTracker
from app.web_console.live_stream import _WebLiveStream, _WebStreamRenderer
from app.web_operations import web_event_operation_specs
from tests.test_agent_loop import _echo_registry
from tests.test_web_operations_schema import _OperationStore


def test_byte_count_throttle_and_full_snapshot_no_double_count(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr('app.llm.tool_input.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('app.llm.tool_input.time.time', lambda: 1000 + clock[0])
    tracker = ToolInputTracker()
    first = tracker.update('one', name='Write')
    assert first.details['receivedBytes'] == 0
    assert tracker.update('one', delta='中文') is None
    clock[0] += 1
    next_event = tracker.update('one', delta='!')
    assert next_event.details['receivedBytes'] == 7
    assert next_event.details['startedAtMs'] == first.details['startedAtMs']
    assert next_event.details['elapsedMs'] == 1000
    done = tracker.update('one', arguments='中文!', done=True)
    assert done.details['receivedBytes'] == 7
    assert done.details['phase'] == 'ready'
    assert not done.tool_calls and not done.text
    assert '中文' not in json.dumps(done.details, ensure_ascii=False)
    tracker.update('two', name='Edit', delta='abc')
    final = tracker.update('two', arguments='abc', done=True)
    assert final.details['receivedBytes'] == 10
    assert final.details['callCount'] == 2


class EventClient:
    def __init__(self, events):
        self.events = events

    async def post_sse(self, *args, **kwargs):
        for event in self.events:
            yield event.get('type', ''), event


@pytest.mark.parametrize('protocol', ['responses', 'chat', 'anthropic'])
async def test_each_protocol_exposes_progress_before_complete_call(protocol):
    args = '{"content":"中文' + 'x' * 2000 + '"}'
    split = 13
    if protocol == 'responses':
        item = {'id': 'fc1', 'type': 'function_call', 'call_id': 'call1', 'name': 'Write', 'arguments': ''}
        events = [
            {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
            *[{'type': 'response.function_call_arguments.delta', 'item_id': 'fc1', 'output_index': 0, 'delta': chunk} for chunk in (args[:split], args[split:])],
            {'type': 'response.function_call_arguments.done', 'item_id': 'fc1', 'output_index': 0, 'arguments': args},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': {**item, 'arguments': args, 'status': 'completed'}},
            {'type': 'response.completed', 'response': {'status': 'completed'}},
        ]
        backend = OpenAIResponsesBackend(EventClient(events), 'https://test/v1', 'k')
    elif protocol == 'chat':
        events = [
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call1', 'function': {'name': 'Write', 'arguments': args[:split]}}]}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': args[split:]}}]}, 'finish_reason': 'tool_calls'}]},
        ]
        backend = OpenAIChatBackend(EventClient(events), 'https://test/v1', 'k')
    else:
        events = [
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'call1', 'name': 'Write', 'input': {}}},
            *[{'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': chunk}} for chunk in (args[:split], args[split:])],
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}},
            {'type': 'message_stop'},
        ]
        backend = AnthropicBackend(EventClient(events), 'https://test/v1', 'k')
    output = [event async for event in backend.stream([{'role': 'user', 'content': 'write'}], model='m')]
    progress = [event for event in output if event.kind == 'tool_input']
    assert progress and progress[0].details['toolNames'] == ['Write']
    assert progress[-1].details['receivedBytes'] == len(args.encode())
    assert output.index(progress[0]) < next(i for i, event in enumerate(output) if event.kind == 'tool_call')
    calls = next(event.tool_calls for event in output if event.kind == 'tool_call')
    assert calls[0].arguments == args


@pytest.mark.parametrize('terminal', ['complete', 'error', 'cancel'])
async def test_controller_live_progress_without_premature_execution_and_cleanup(terminal):
    emitted = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def sink(event):
        emitted.append(event)
        return event

    live = _WebLiveStream('conv', 1, event_sink=sink)
    await live.publish({'type': 'accepted', 'turnUuid': 'turn', 'runUuid': 'run'})
    renderer = _WebStreamRenderer(live)

    class Backend:
        protocol = 'fake'
        calls = 0

        async def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                yield StreamEvent(kind='content', text='done')
                yield StreamEvent(kind='finish', finish_reason='stop')
                return
            yield StreamEvent(kind='tool_input', details={
                'toolNames': ['echo'], 'receivedBytes': 12010, 'startedAtMs': 1000,
                'updatedAtMs': 2000, 'elapsedMs': 1000, 'phase': 'generating'})
            entered.set()
            await release.wait()
            if terminal == 'error':
                yield StreamEvent(kind='error', error='broken', retryable=False)
                return
            yield StreamEvent(kind='tool_call', tool_calls=[ToolCall(id='c1', name='echo', arguments='{"x":"hi"}')])
            yield StreamEvent(kind='finish', finish_reason='tool_calls')

    reg = _echo_registry()
    # Tool start is the execution boundary; the live event is sufficient evidence.
    task = asyncio.create_task(Agent(Backend(), reg, max_retries=0).run(
        [{'role': 'user', 'content': 'test'}], renderer, model='m'))
    await asyncio.wait_for(entered.wait(), 2)
    progress_events = [e for e in emitted if e.get('modelOutput')]
    assert progress_events[-1]['modelOutput']['receivedBytes'] == 12010
    assert not any(e['type'] == 'tool_start' for e in emitted)
    spec = web_event_operation_specs(progress_events[-1])[0]
    assert spec['payload']['modelOutput']['receivedBytes'] == 12010
    assert 'arguments' not in spec['payload']['modelOutput']
    if terminal == 'cancel':
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await task
    statuses = [e for e in emitted if e['type'] == 'status']
    assert statuses[-1].get('modelOutput') is None
    if terminal == 'complete':
        assert any(e['type'] == 'tool_start' for e in emitted)
    else:
        assert not any(e['type'] == 'tool_start' for e in emitted)
    await renderer.close()


@pytest.mark.parametrize('clear_event', [
    {'type': 'status', 'status': '模型参数输出已结束', 'modelOutput': None},
    {'type': 'status', 'status': '正在思考 …'},
])
async def test_clear_progress_survives_real_database_snapshot_and_frame(tmp_path, clear_event):
    db = DB(str(tmp_path / 'tool-input-clear.db'))
    await db.connect()
    store = _OperationStore(db)
    base = {'conversationUuid': 'conv-progress', 'chatId': -1,
            'turnUuid': 'turn-progress', 'runUuid': 'run-progress'}
    try:
        await store._publish_native_operations({**base, 'type': 'status', 'status': '生成中',
            'modelOutput': {'toolNames': ['Write'], 'receivedBytes': 12010, 'phase': 'generating'}})
        await store._publish_native_operations({**base, **clear_event})
        row = await (await db.conn.execute(
            "SELECT payload_json FROM web_operations WHERE conversation_uuid=? AND op_type='status'",
            ('conv-progress',))).fetchone()
        snapshot = json.loads(row['payload_json'])
        assert snapshot['modelOutput'] is False
        row = await (await db.conn.execute(
            "SELECT payload_json FROM web_event_frames WHERE conversation_uuid=? AND op_type='status' ORDER BY frame_seq DESC LIMIT 1",
            ('conv-progress',))).fetchone()
        assert json.loads(row['payload_json'])['modelOutput'] is False
    finally:
        await db.close()


async def test_progress_cannot_reopen_externally_stopped_run():
    live = _WebLiveStream('conv', 1)
    await live.publish({'type': 'accepted', 'turnUuid': 'turn'})
    renderer = _WebStreamRenderer(live)
    await live.publish({'type': 'stopped', 'status': '已停止'})
    await renderer.on_model_output_progress(None)
    assert live.current_status == '已停止'
