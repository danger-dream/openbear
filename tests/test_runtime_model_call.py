"""Deterministic fake-backend tests of the shared model-call boundary."""
import asyncio
import copy

import pytest

from app.llm.base import AgentResult, OpenBearLLMError
from app.llm.events import StreamEvent, ToolCall, Usage
from app.llm.retry import RetryCancelledError, RetryPolicy
from app.runtime.model_call import (AttemptOutcome, ModelCallDriver, PreparedRequest,
                                    execute_attempt)


class FakeStream:
    protocol = "fake"

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.seen = []
        self.complete_calls = 0

    async def stream(self, messages, **options):
        self.seen.append((copy.deepcopy(messages), copy.deepcopy(options)))
        events = self.rounds.pop(0)
        for item in events:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def complete(self, messages, **options):
        self.complete_calls += 1
        raise AssertionError("auto must prefer stream")


class FakeComplete:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def complete(self, messages, **options):
        self.calls += 1
        return self.result


def req(backend, messages=None, options=None):
    return PreparedRequest(backend, messages or [{"role": "user", "content": "question"}],
                           options or {"model": "fake", "system": "s", "tools": []})


def driver(max_retries=2):
    return ModelCallDriver(RetryPolicy(max_retries=max_retries, base_delay_s=0))


def fault(message="temporary", status=503, **kw):
    kw.setdefault("retryable", status >= 500)
    return StreamEvent("error", error=message, status=status, **kw)


@pytest.mark.asyncio
async def test_stream_success_events_usage_flags_and_timing():
    backend = FakeStream([[StreamEvent("metrics", connect_ms=7),
                           StreamEvent("reasoning", text="think", signature="sig"),
                           StreamEvent("content", text="Hi"), StreamEvent("content", text="!"),
                           StreamEvent("usage", usage=Usage(input_tokens=2, output_tokens=3)),
                           StreamEvent("finish", finish_reason="stop")]])
    settled = []
    seen = []

    async def on_event(event, logical, attempt):
        seen.append((event.kind, logical.text, logical.reasoning, attempt.attempt_id))

    result = await driver().call(lambda tail: req(backend), settled.append, on_event=on_event)
    assert result.response.text == "Hi!" and result.response.reasoning == "think"
    assert result.response.signature == "sig" and result.native_replayable
    assert result.response.usage == Usage()  # physically billed only by settle
    assert len(settled) == 1 and settled[0].status == "ok" and settled[0].error is None
    assert settled[0].usage_reported and settled[0].prompt_usage_reported
    assert settled[0].response.usage.input_tokens == 2
    assert settled[0].connect_ms == 7 and settled[0].first_token_ms > 0
    assert settled[0].total_time_ms >= 0 and settled[0].reasoning_ms > 0
    assert [v[1] for v in seen if v[0] == "content"] == ["Hi", "Hi!"]
    assert len({v[3] for v in seen}) == 1
    assert backend.complete_calls == 0


@pytest.mark.asyncio
async def test_complete_compat_explicit_override_and_request_snapshot():
    complete = FakeComplete(AgentResult(text="done", usage=Usage(output_tokens=4)))
    out = await execute_attempt(req(complete))
    assert out.status == "ok" and out.response.text == "done"
    assert out.connect_ms == out.first_token_ms == 0
    assert out.usage_reported and not out.prompt_usage_reported and complete.calls == 1
    hybrid = FakeStream([])
    hybrid.complete = complete.complete
    assert (await execute_attempt(req(hybrid), mode="complete")).response.text == "done"
    explicit = req(hybrid)
    explicit.metadata["mode"] = "complete"
    assert (await driver().call(lambda tail: explicit, lambda outcome: None)).response.text == "done"
    assert hybrid.complete_calls == 0 and complete.calls == 3
    options = {"model": "a", "tools": [{"name": "first"}]}
    messages = [{"role": "user", "content": ["first"]}]
    snapshot = req(complete, messages, options)
    options["tools"][0]["name"] = "changed"
    messages[0]["content"][0] = "changed"
    assert snapshot.options["tools"][0]["name"] == "first"
    assert snapshot.messages[0]["content"] == ["first"]


@pytest.mark.asyncio
async def test_error_event_partial_text_tail_and_no_native_mixing():
    backend = FakeStream([
        [StreamEvent("content", text="first "),
         StreamEvent("native_output_item", native_output_items=[{"id": "old"}]),
         StreamEvent("usage", usage=Usage(output_tokens=3)), fault()],
        [StreamEvent("content", text="second"),
         StreamEvent("native_output_item", native_output_items=[{"id": "new"}]),
         StreamEvent("finish", finish_reason="stop")],
    ])
    settled, tails, callbacks = [], [], []

    async def prepare(tail):
        tails.append(copy.deepcopy(tail))
        return req(backend, [{"role": "user", "content": "origin"}, *tail])

    async def on_event(ev, logical, attempt):
        if ev.kind == "content":
            callbacks.append(logical.text)

    result = await driver().call(prepare, settled.append, on_event=on_event)
    assert callbacks == ["first ", "first second"]
    assert result.response.text == "first second"
    assert result.response.native_output_items == [] and result.response.signature == ""
    assert not result.native_replayable and (result.attempts, result.retries) == (2, 1)
    assert len(tails) == 2 and tails[0] == []
    assert tails[1][0] == {"role": "assistant", "content": "first "}
    assert tails[1][1]["role"] == "user"
    assert [o.status for o in settled] == ["error", "ok"]
    assert settled[0].response.text == "first " and settled[1].response.text == "second"
    assert settled[0].attempt_id != settled[1].attempt_id
    assert backend.seen[0][0] == [{"role": "user", "content": "origin"}]


@pytest.mark.asyncio
async def test_reasoning_accumulation_switch_and_no_unsigned_tail():
    backend = FakeStream([
        [StreamEvent("reasoning", text="old", signature="old-sig"), fault()],
        [StreamEvent("reasoning", text="new", signature="new-sig"), StreamEvent("content", text="okay")],
    ])
    tails = []

    def prepare(tail):
        tails.append(tail)
        return req(backend)

    result = await driver().call(prepare, lambda outcome: None, accumulate_reasoning=False)
    assert result.response.reasoning == "new" and result.response.signature == ""
    assert result.response.native_output_items == [] and tails[1][0]["role"] == "user"


@pytest.mark.asyncio
async def test_completed_tool_call_on_retryable_failure_never_reissued_or_success_settled():
    call = ToolCall(id="id1", name="side_effect", arguments="{}")
    backend = FakeStream([[StreamEvent("content", text="before"),
                           StreamEvent("tool_call", tool_calls=[call]),
                           StreamEvent("native_output_item", native_output_items=[{"id": "native"}]),
                           fault()]])
    settled = []
    result = await driver().call(lambda tail: req(backend), settled.append)
    assert result.recovered_tool_calls and result.attempts == 1 and result.retries == 0
    assert result.response.tool_calls == [call]
    assert result.response.finish_reason == "tool_calls"
    assert result.response.native_output_items == [{"id": "native"}]
    assert len(backend.seen) == 1 and [o.status for o in settled] == ["error"]


@pytest.mark.asyncio
async def test_overflow_rotation_not_spending_retry_budget_and_discards_stale_partial():
    backend = FakeStream([[StreamEvent("content", text="stale"),
                           StreamEvent("native_output_item", native_output_items=[{"old": True}]),
                           fault("context length exceeded", status=413)],
                          [StreamEvent("content", text="fresh"),
                           StreamEvent("native_output_item", native_output_items=[{"fresh": True}])]])
    settled, tails, overflow = [], [], []

    def prepare(tail):
        tails.append(copy.deepcopy(tail))
        return req(backend)

    def recover(error, tail):
        overflow.append((error.reason, tail))
        return True

    result = await driver(max_retries=0).call(prepare, settled.append, recover_overflow=recover)
    assert overflow == [("context_overflow", [])]
    assert tails == [[], []] and result.response.text == "fresh"
    assert result.response.native_output_items == [{"fresh": True}]
    assert len(settled) == 2 and (result.attempts, result.retries) == (2, 1)


@pytest.mark.asyncio
async def test_cancel_wait_preserves_retry_cancelled_and_no_phantom_attempt():
    backend = FakeStream([[fault()]])
    settled, updates = [], []

    def control(wait_id):
        return "cancel"

    with pytest.raises(RetryCancelledError):
        await driver().call(lambda tail: req(backend), settled.append, on_retry=updates.append,
                            control_check=control, retry_scope="agent", task_uuid="task-1")
    assert len(settled) == len(backend.seen) == 1
    assert updates[0]["scope"] == "agent" and updates[0]["taskUuid"] == "task-1"
    assert updates[-1]["status"] == "cancelled" and not updates[-1]["active"]


@pytest.mark.asyncio
async def test_cancel_during_stream_settles_known_partial_usage_once():
    class CancelStream:
        async def stream(self, messages, **options):
            yield StreamEvent("content", text="kept")
            yield StreamEvent("usage", usage=Usage(output_tokens=5))
            await asyncio.sleep(100)

    settled = []
    task = asyncio.create_task(driver().call(lambda tail: req(CancelStream()), settled.append))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(settled) == 1
    assert settled[0].status == "cancelled" and settled[0].error is None
    assert settled[0].response.text == "kept" and settled[0].response.usage.output_tokens == 5
    assert settled[0].usage_reported and not settled[0].prompt_usage_reported


@pytest.mark.asyncio
async def test_cancel_cleanup_is_bounded():
    backend = FakeStream([[asyncio.CancelledError()]])
    blocker = asyncio.Event()
    settled = []

    async def settle(outcome):
        settled.append(outcome)
        await blocker.wait()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execute_attempt(req(backend), settle=settle,
                                                cancel_settle_timeout_s=0.01), timeout=1)
    assert len(settled) == 1 and settled[0].status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["settle", "on_event", "prepare"])
async def test_local_failures_never_retry(failure):
    backend = FakeStream([[StreamEvent("content", text="part"), fault()]])
    settled = []
    calls = []

    def prepare(tail):
        calls.append(tail)
        if failure == "prepare":
            raise ValueError("prepare broke")
        return req(backend)

    def settle(outcome):
        settled.append(outcome)
        if failure == "settle":
            raise ValueError("accounting broke")

    def on_event(event, logical, outcome):
        if failure == "on_event":
            raise ValueError("renderer broke")

    with pytest.raises(ValueError, match="broke"):
        await driver().call(prepare, settle, on_event=on_event)
    assert len(calls) == 1 and len(backend.seen) == len(settled) == (failure != "prepare")
    if settled:
        assert settled[0].status == "error"
        assert (settled[0].error is None) == (failure == "on_event")
        assert settled[0].response.text == "part"


@pytest.mark.asyncio
async def test_no_usage_is_unknown_even_when_output_and_complete_zero():
    stream = FakeStream([[StreamEvent("content", text="x"), StreamEvent("finish")]])
    a = await execute_attempt(req(stream))
    b = await execute_attempt(req(FakeComplete(AgentResult(text="x"))))
    assert not a.usage_reported and not b.usage_reported
    assert not a.prompt_usage_reported and not b.prompt_usage_reported


@pytest.mark.asyncio
async def test_first_structured_root_cause_not_overwritten_by_format_failure():
    original = fault("upstream overloaded", status=503,
                     root_cause={"classification": "overloaded", "code": "busy"},
                     details={"trace": "original"})
    backend = FakeStream([[original], [fault("bad format", status=400)]])
    settled = []
    with pytest.raises(OpenBearLLMError) as raised:
        await driver(max_retries=1).call(lambda tail: req(backend), settled.append)
    assert raised.value.root_cause["code"] == "busy" and raised.value.structured
    assert [o.error.reason for o in settled] == ["overloaded", "format"]


@pytest.mark.asyncio
async def test_settlement_completes_before_next_physical_call_and_preserves_error_usage():
    backend = FakeStream([
        [StreamEvent("usage", usage=Usage(output_tokens=6)), fault()],
        [StreamEvent("content", text="okay")],
    ])
    order = []

    async def settle(outcome):
        order.append((len(backend.seen), outcome.status, outcome.response.usage.output_tokens))
        await asyncio.sleep(0)

    def prepare(tail):
        assert len(order) == len(backend.seen)
        return req(backend)

    result = await driver().call(prepare, settle)
    assert result.response.text == "okay"
    assert order == [(1, "error", 6), (2, "ok", 0)]


@pytest.mark.asyncio
async def test_provider_exception_with_usage_is_preserved_on_physical_outcome():
    error = OpenBearLLMError("overloaded", status=503, retryable=True)
    error.usage = Usage(cache_read_tokens=3, output_tokens=1)
    backend = FakeStream([[error]])
    settled = []
    outcome = await execute_attempt(req(backend), settle=settled.append)
    assert outcome is settled[0]
    assert outcome.error is error and outcome.status == "error"
    assert outcome.usage_reported and outcome.prompt_usage_reported
    assert outcome.response.usage.cache_read_tokens == 3


@pytest.mark.asyncio
async def test_renderer_llm_error_is_not_provider_error_or_retried():
    backend = FakeStream([[StreamEvent("content", text="partial"), fault()]])
    settled = []
    injected = OpenBearLLMError("renderer", status=503, retryable=True)

    def on_event(event, logical, attempt):
        raise injected

    with pytest.raises(OpenBearLLMError) as caught:
        await driver().call(lambda tail: req(backend), settled.append, on_event=on_event)
    assert caught.value is injected
    assert len(backend.seen) == len(settled) == 1
    assert settled[0].status == "error" and settled[0].error is None


@pytest.mark.asyncio
async def test_on_start_awaits_before_stream_and_settles_same_attempt_after():
    backend = FakeStream([[StreamEvent("content", text="okay")]])
    gate = asyncio.Event()
    entered = asyncio.Event()
    order = []
    source_options = {"model": "frozen", "tools": [{"name": "t"}]}
    request = req(backend, options=source_options)

    async def on_start(outcome):
        assert isinstance(outcome, AttemptOutcome)
        assert outcome.request is request and outcome.attempt_id
        assert outcome.response == AgentResult() and outcome.status == "ok"
        assert outcome.request.options["tools"][0]["name"] == "t"
        order.append(("start", outcome))
        entered.set()
        await gate.wait()

    def on_event(event, logical, outcome):
        assert order[0][1] is outcome
        order.append(("event", outcome))

    def settle(outcome):
        order.append(("settle", outcome))

    task = asyncio.create_task(execute_attempt(request, on_start=on_start,
                                               on_event=on_event, settle=settle))
    await entered.wait()
    source_options["tools"][0]["name"] = "changed"
    assert not backend.seen and [item[0] for item in order] == ["start"]
    gate.set()
    outcome = await task
    assert outcome.response.text == "okay"
    assert [kind for kind, _ in order] == ["start", "event", "settle"]
    assert all(item is outcome for _, item in order)
    assert backend.seen[0][1]["tools"][0]["name"] == "t"


@pytest.mark.asyncio
async def test_driver_on_start_runs_per_actual_retry_before_backend_and_settle():
    backend = FakeStream([[fault()], [StreamEvent("content", text="yes")]])
    ordered = []

    async def on_start(outcome):
        assert len(backend.seen) == sum(1 for kind, _ in ordered if kind == "settle")
        ordered.append(("start", outcome))
        await asyncio.sleep(0)

    def settle(outcome):
        ordered.append(("settle", outcome))

    response = await driver().call(lambda tail: req(backend), settle, on_start=on_start)
    assert (response.attempts, response.retries) == (2, 1)
    assert [kind for kind, _ in ordered] == ["start", "settle", "start", "settle"]
    assert ordered[0][1] is ordered[1][1] and ordered[2][1] is ordered[3][1]
    assert ordered[0][1].attempt_id != ordered[2][1].attempt_id
    assert [item.status for kind, item in ordered if kind == "settle"] == ["error", "ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("use_driver", [False, True])
async def test_on_start_failure_never_calls_backend_or_settle(use_driver):
    backend = FakeStream([[StreamEvent("content", text="must not send")]])
    settled = []
    started = []
    failure = OpenBearLLMError("start persistence failed", status=503, retryable=True)

    async def on_start(outcome):
        started.append(outcome)
        await asyncio.sleep(0)
        raise failure

    with pytest.raises(OpenBearLLMError) as caught:
        if use_driver:
            await driver().call(lambda tail: req(backend), settled.append, on_start=on_start)
        else:
            await execute_attempt(req(backend), settle=settled.append, on_start=on_start)
    assert caught.value is failure
    assert len(started) == 1 and started[0].attempt_id
    assert not backend.seen and not settled


@pytest.mark.asyncio
async def test_complete_on_start_failure_prevents_complete_request():
    backend = FakeComplete(AgentResult(text="never"))
    with pytest.raises(ValueError, match="commit"):
        await execute_attempt(req(backend), on_start=lambda _: (_ for _ in ()).throw(ValueError("commit")))
    assert backend.calls == 0


@pytest.mark.asyncio
async def test_driver_cancel_check_only_applies_to_retry_wait():
    backend = FakeStream([[fault()]])
    started, settled = [], []
    with pytest.raises(RetryCancelledError):
        await driver().call(lambda tail: req(backend), settled.append,
                            on_start=started.append, cancel_check=lambda: True)
    assert len(started) == len(backend.seen) == len(settled) == 1
    assert started[0] is settled[0] and settled[0].status == "error"


@pytest.mark.asyncio
async def test_error_event_explicitly_closes_stream():
    class TrackedBackend:
        def __init__(self, broken_close=False):
            self.closed = 0
            self.broken_close = broken_close
            self.calls = 0

        def stream(self, messages, **options):
            backend = self

            class Events:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    backend.calls += 1
                    return fault()

                async def aclose(self):
                    backend.closed += 1
                    if backend.broken_close:
                        raise ValueError("close failed")

            return Events()

    backend = TrackedBackend()
    settled = []
    with pytest.raises(OpenBearLLMError):
        await driver(max_retries=0).call(lambda tail: req(backend), settled.append)
    assert backend.closed == backend.calls == len(settled) == 1
    broken = TrackedBackend(broken_close=True)
    broken_settled = []
    with pytest.raises(ValueError, match="close failed"):
        await driver(max_retries=3).call(lambda tail: req(broken), broken_settled.append)
    assert broken.closed == broken.calls == len(broken_settled) == 1


@pytest.mark.asyncio
async def test_stream_exception_keeps_event_billing_when_exception_has_no_new_billing():
    backend = FakeStream([[
        StreamEvent("usage", usage=Usage(input_tokens=2),
                    details={"serviceTier": "priority", "providerCostUsd": 0.25}),
        OpenBearLLMError("provider disconnected", status=503, retryable=True),
    ]])
    settled = []
    outcome = await execute_attempt(req(backend), settle=settled.append)
    assert outcome is settled[0] and outcome.status == "error"
    assert outcome.usage_reported and outcome.prompt_usage_reported
    assert outcome.response.usage.input_tokens == 2
    assert outcome.response.service_tier == "priority"
    assert outcome.response.provider_cost_usd == 0.25


@pytest.mark.asyncio
async def test_backend_receives_per_attempt_copied_options_and_messages():
    class MutatingBackend:
        async def stream(self, messages, **options):
            messages[0]["content"] = "provider changed"
            options["tools"][0]["name"] = "provider changed"
            yield StreamEvent("content", text="fine")

    source = {"model": "m", "tools": [{"name": "good"}], "max_tokens": 77}
    outbound = req(MutatingBackend(), options=source)
    result = await execute_attempt(outbound)
    assert result.response.text == "fine"
    assert outbound.options == source and outbound.messages[0]["content"] == "question"
    assert outbound.options["max_tokens"] == 77
