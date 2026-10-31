from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent.loop import Agent
from app.db.engine import DB
from app.llm.anthropic import AnthropicBackend
from app.llm.events import StreamEvent, ToolCall
from app.runtime import model_call
from app.runtime.model_call import PreparedRequest, execute_attempt
from app.web_console.live_stream import _WebLiveStream, _WebStreamRenderer
from tests.test_agent_loop import _echo_registry
from tests.test_tool_input_progress import EventClient
from tests.test_web_operations_schema import _OperationStore


@pytest.fixture
async def presentation(tmp_path):
    db = DB(str(tmp_path / 'progress.db'))
    await db.connect()
    store = _OperationStore(db)
    events = []

    async def sink(event):
        events.append(event)
        await store._publish_native_operations(event)
        return event

    live = _WebLiveStream('conv', -1, event_sink=sink)
    await live.publish({'type': 'accepted', 'turnUuid': 'turn', 'runUuid': 'run'})
    renderer = _WebStreamRenderer(live)
    try:
        yield renderer, db, events
    finally:
        await renderer.close()
        await db.close()


async def operations(db):
    rows = await (await db.conn.execute('SELECT * FROM web_operations ORDER BY display_seq')).fetchall()
    return {row['op_id']: {**dict(row), 'payload': json.loads(row['payload_json'])} for row in rows}


def progress(size=100, phase='generating'):
    return {'toolNames': ['echo'], 'receivedBytes': size, 'startedAtMs': 1000,
            'updatedAtMs': 2000, 'elapsedMs': 1000, 'phase': phase}


async def test_parameter_boundary_closes_thought_in_snapshot_and_frame(presentation):
    renderer, db, events = presentation
    await renderer.on_model_start()
    await renderer.on_delta('', 'first thought')
    await renderer.on_model_output_progress(progress())
    first = (await operations(db))['reasoning:turn:0']
    assert first['status'] == 'completed'
    assert first['payload']['complete'] is True
    ended = first['payload']['terminalAtMs']
    await renderer.on_model_output_progress(progress(200))
    after = (await operations(db))['reasoning:turn:0']
    assert after['payload']['terminalAtMs'] == ended
    assert after['payload']['text'] == 'first thought'
    frames = await (await db.conn.execute(
        "SELECT action,payload_json FROM web_event_frames WHERE op_id='reasoning:turn:0' ORDER BY frame_seq"
    )).fetchall()
    assert frames[-1]['action'] == 'end'
    assert json.loads(frames[-1]['payload_json'])['complete'] is True
    assert not any(e['type'] == 'tool_start' for e in events)


async def test_phase_suffixes_preserve_history_once_and_reset_for_next_model(presentation):
    renderer, db, _ = presentation
    await renderer.on_model_start()
    await renderer.on_delta('', 'thought A')
    await renderer.on_model_output_progress(progress())
    await renderer.on_model_output_progress(None)
    await renderer.on_delta('', 'thought Athought B')
    await renderer.on_delta('answer A', 'thought Athought B')
    await renderer.on_delta('answer A', 'thought Athought Bthought C')
    await renderer.on_delta('answer Aanswer B', 'thought Athought Bthought C')
    await renderer.finalize('answer Aanswer B', 'thought Athought Bthought C')
    rows = list((await operations(db)).values())
    assert [r['payload']['text'] for r in rows if r['op_type'] == 'reasoning'] == ['thought A', 'thought B', 'thought C']
    assert [r['payload']['text'] for r in rows if r['op_type'] == 'assistant_message'] == ['answer A', 'answer B']
    assert all(r['payload']['complete'] for r in rows if r['op_type'] in {'reasoning', 'assistant_message'})
    # A later model response may legitimately repeat a prefix. It is not a
    # continuation snapshot of the previous physical/logical response.
    await renderer.cut()
    await renderer.on_model_start()
    await renderer.on_delta('answer A', 'thought A')
    await renderer.finalize('answer A', 'thought A')
    rows = list((await operations(db)).values())
    assert [r['payload']['text'] for r in rows if r['op_type'] == 'assistant_message'] == ['answer A', 'answer B', 'answer A']


@pytest.mark.parametrize('next_kind', ['content', 'reasoning', 'encrypted_reasoning'])
async def test_controller_clears_parameter_activity_when_stream_changes_phase(presentation, next_kind):
    renderer, db, events = presentation
    entered, release = asyncio.Event(), asyncio.Event()

    class Backend:
        protocol = 'fake'

        async def stream(self, *args, **kwargs):
            yield StreamEvent('tool_input', details=progress(100, 'ready'))
            yield StreamEvent(next_kind, text='new phase')
            entered.set()
            await release.wait()
            yield StreamEvent('content', text='done')
            yield StreamEvent('finish', finish_reason='stop')

    task = asyncio.create_task(Agent(Backend(), _echo_registry()).run(
        [{'role': 'user', 'content': 'fixture'}], renderer, model='fixture', show_thinking=True))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await renderer._flush_delta(force_persist=True)
        assert (await operations(db))['status:run']['payload']['modelOutput'] is False
        assert not any(e['type'] == 'tool_start' for e in events)
    finally:
        release.set()
        await task


@pytest.mark.parametrize('visible', [False, True])
@pytest.mark.parametrize('kind', ['reasoning', 'encrypted_reasoning', 'content'])
async def test_retry_uses_new_display_segment_without_replaying_old_snapshot(presentation, kind, visible, monkeypatch):
    renderer, db, events = presentation
    if visible:
        monkeypatch.setattr('app.llm.retry.QUIET_RETRIES', 0)
    entered, release = asyncio.Event(), asyncio.Event()

    class Backend:
        protocol = 'fake'
        calls = 0

        async def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(kind, text='first attempt')
                yield StreamEvent('error', error='transient', retryable=True)
                return
            yield StreamEvent(kind, text='second attempt')
            entered.set()
            await release.wait()
            yield StreamEvent('content', text='done')
            yield StreamEvent('finish', finish_reason='stop')

    task = asyncio.create_task(Agent(Backend(), _echo_registry(), max_retries=1, retry_backoff_s=0).run(
        [{'role': 'user', 'content': 'fixture'}], renderer, model='fixture', show_thinking=True))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await renderer._flush_delta(force_persist=True)
        rows = list((await operations(db)).values())
        expected_type = 'assistant_message' if kind == 'content' else 'reasoning'
        output = [r for r in rows if r['op_type'] == expected_type]
        assert len(output) == 2
        assert output[0]['payload']['complete'] is True
        assert output[1]['payload']['complete'] is False
        assert 'first attempt' in output[0]['payload']['text']
        assert 'second attempt' in output[1]['payload']['text']
        assert 'first attempt' not in output[1]['payload']['text']
        retries = [r for r in rows if r['op_type'] == 'model_retry']
        if visible:
            assert output[0]['display_seq'] < retries[0]['display_seq'] < output[1]['display_seq']
        else:
            assert retries == []
            assert output[0]['display_seq'] < output[1]['display_seq']
    finally:
        release.set()
        await task
    assert len([e for e in events if e['type'] == 'cut']) >= 1


@pytest.mark.parametrize('with_reasoning', [False, True])
async def test_timing_counts_parameter_bytes_and_excludes_parameter_generation(monkeypatch, with_reasoning):
    clock = [100.0]
    wall_time = 1_800_000_000.0
    # Replace only model_call's clock view, not asyncio's monotonic clock.
    # Wall-clock request identity is independent of elapsed-time measurements.
    monkeypatch.setattr(model_call, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: wall_time))

    class Backend:
        async def stream(self, *args, **kwargs):
            if with_reasoning:
                clock[0] = 101.0
                yield StreamEvent('reasoning', text='thought')
            clock[0] = 101.5
            yield StreamEvent('tool_input', details=progress(0))
            clock[0] = 102.0
            yield StreamEvent('tool_input', details=progress(10))
            clock[0] = 162.0
            yield StreamEvent('tool_input', details=progress(40000, 'ready'))
            yield StreamEvent('tool_call', tool_calls=[ToolCall(id='c', name='echo', arguments='{}')])
            yield StreamEvent('finish', finish_reason='tool_calls')

    result = await execute_attempt(PreparedRequest(Backend(), [], {'model': 'fixture'}))
    assert result.started_at_ms == int(wall_time * 1000)
    assert result.first_token_ms == (1000 if with_reasoning else 2000)
    assert result.reasoning_ms == (500 if with_reasoning else 0)
    assert result.total_time_ms == 62000


async def test_timing_sums_separate_reasoning_phases_without_intervening_output(monkeypatch):
    clock = [100.0]
    wall_time = 1_800_000_000.0
    # Replace only model_call's clock view, not asyncio's monotonic clock.
    # Wall-clock request identity is independent of elapsed-time measurements.
    monkeypatch.setattr(model_call, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: wall_time))

    class Backend:
        async def stream(self, *args, **kwargs):
            for when, event in [(101, StreamEvent('reasoning', text='A')),
                                (102, StreamEvent('tool_input', details=progress())),
                                (162, StreamEvent('reasoning', text='B')),
                                (164, StreamEvent('content', text='answer')),
                                (200, StreamEvent('finish', finish_reason='stop'))]:
                clock[0] = when
                yield event

    result = await execute_attempt(PreparedRequest(Backend(), [], {'model': 'fixture'}))
    assert result.started_at_ms == int(wall_time * 1000)
    assert result.reasoning_ms == 3000
    assert result.total_time_ms == 100000


@pytest.mark.parametrize('input_value', [{}, {'path': '/tmp/example', 'content': '中文'}])
@pytest.mark.parametrize('with_delta', [False, True])
async def test_anthropic_initial_input_and_delta_use_same_arguments_as_complete_call(input_value, with_delta):
    final_input = {'content': 'incremental'} if with_delta else input_value
    arguments = json.dumps(final_input, ensure_ascii=False)
    events = [
        {'type': 'content_block_start', 'index': 0,
         'content_block': {'type': 'tool_use', 'id': 'c', 'name': 'Write', 'input': input_value}},
        *([{'type': 'content_block_delta', 'index': 0,
            'delta': {'type': 'input_json_delta', 'partial_json': arguments}}] if with_delta else []),
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}},
        {'type': 'message_stop'},
    ]
    output = [event async for event in AnthropicBackend(EventClient(events), 'https://fixture.invalid', 'fixture').stream(
        [{'role': 'user', 'content': 'fixture'}], model='fixture')]
    activity = [e.details for e in output if e.kind == 'tool_input']
    call = next(e.tool_calls[0] for e in output if e.kind == 'tool_call')
    assert json.loads(call.arguments) == final_input
    assert activity[-1]['receivedBytes'] == len(call.arguments.encode())
    if input_value:
        assert activity[0]['receivedBytes'] == len(json.dumps(input_value, ensure_ascii=False).encode())
