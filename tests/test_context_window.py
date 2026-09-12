"""Window selection and durable history tests. No network or live database."""
from __future__ import annotations

import copy

import pytest

from app.context.store import ContextHistoryUnavailable, ContextOwner, StaleWindow, WindowStore
from app.context.window import (
    CONTEXT_META,
    InputEstimate,
    RequiredContextTooLarge,
    UnsafeWindowBoundary,
    WindowPolicy,
    estimate_payload,
    estimate_request,
    mark_source,
    select_window,
)
from app.db.engine import DB
from app.llm.events import ToolCall


def human(text="task", source_id="u1"):
    return mark_source({"role": "user", "content": text}, kind="human", source_id=source_id)


def batch(index, *, name="Read", text="result"):
    return [
        mark_source({"role": "assistant", "content": None,
                     "tool_calls": [ToolCall(id=f"t{index}", name=name, arguments="{}")]}),
        mark_source({"role": "tool", "tool_call_id": f"t{index}", "name": name, "content": text}),
    ]


def units(messages):
    return InputEstimate(10 + len(messages) * 10)


def test_preserves_current_run_originals_and_antecedent_not_latest_n():
    messages = [mark_source(human("do not deploy"), kind="human", run_root_turn_uuid="active")]
    for i in range(12):
        messages += batch(i)
    proposal = mark_source({"role": "assistant", "content": "Only edit source; no deployment."})
    messages += [proposal, mark_source(human("yes", "u2"), kind="human", run_root_turn_uuid="active"), *batch(99)]
    before = copy.deepcopy(messages)
    picked = select_window(messages, estimate=units, target=90, latest_batches=1, active_run_root_turn_uuid="active")
    assert [m["content"] for m in picked.messages if m.get("role") == "user"] == ["do not deploy", "yes"]
    assert proposal in picked.messages
    assert picked.changed
    assert messages == before


def test_two_recent_batches_keep_contiguous_middle():
    middle = mark_source({"role": "assistant", "content": "checking both facts"})
    messages = [human(), *batch(0), *batch(1), middle, *batch(2)]
    selected = select_window(messages, estimate=units, target=20, latest_batches=2)
    assert selected.messages == [messages[0], *messages[3:]]
    assert selected.soft_target_exceeded


def test_decision_question_and_free_text_feedback_kept_as_complete_batch():
    messages = [human(), *batch(1, name="UserInteraction", text='{"status":"feedback","confirmed":false,"text":"stop deployment"}'), *batch(2), *batch(3)]
    selected = select_window(messages, estimate=units, target=50, latest_batches=1)
    assert messages[1:3] == selected.messages[1:3]
    assert '"confirmed":false' in selected.messages[2]["content"]


def test_provider_user_role_without_provenance_is_not_a_pin():
    synthetic = {"role": "user", "content": "runtime old message"}
    messages = [human(), synthetic, *batch(1), *batch(2)]
    picked = select_window(messages, estimate=units, target=40, latest_batches=1)
    assert synthetic not in picked.messages


def test_over_capacity_does_not_truncate_or_mutate_requirements():
    messages = [human(), *batch(1)]
    before = copy.deepcopy(messages)
    with pytest.raises(RequiredContextTooLarge):
        select_window(messages, estimate=units, target=20, input_ceiling=30)
    assert messages == before


def test_incomplete_multi_tool_batch_is_rejected():
    messages = [human(), {"role": "assistant", "tool_calls": [ToolCall("a", "Read", "{}"), ToolCall("b", "Read", "{}")], "content": None}, {"role": "tool", "tool_call_id": "a", "content": "ok"}]
    with pytest.raises(UnsafeWindowBoundary):
        select_window(messages, estimate=units, target=100)


def test_opaque_turns_are_dropped_whole_when_opening_new_prefix():
    messages = [human(), *batch(1), *batch(2)]
    messages[1]["native_output_items"] = [{"type": "reasoning", "encrypted_content": "opaque"}]
    messages[1]["reasoning"] = "old provider thought"
    picked = select_window(messages, estimate=units, target=40, latest_batches=1)
    assert all("native_output_items" not in m and "reasoning" not in m for m in picked.messages)
    assert messages[1]["native_output_items"][0]["encrypted_content"] == "opaque"


def test_media_base64_is_not_tokenized_as_text():
    small = estimate_payload({"content": [{"type": "input_image", "image_url": "data:image/png;base64,123"}]})
    large = estimate_payload({"content": [{"type": "input_image", "image_url": "data:image/png;base64," + "a" * 1_000_000}]})
    assert large == small
    assert large.media_unknown


def test_system_and_tools_count_inside_total_budget():
    messages = [human()]
    small = estimate_request(messages, system="s", tools=[])
    large = estimate_request(messages, system="s" * 1000, tools=[{"name": "Read", "description": "d" * 1000}])
    assert large.tokens > small.tokens + 400


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_actual_production_serializers_accept_selected_batches(protocol):
    from app.llm.anthropic import AnthropicBackend
    from app.llm.openai_chat import OpenAIChatBackend
    from app.llm.openai_responses import OpenAIResponsesBackend
    cls = {"chat": OpenAIChatBackend, "responses": OpenAIResponsesBackend, "anthropic": AnthropicBackend}[protocol]
    backend = cls(None, "https://isolated.invalid", "")
    def estimator(messages):
        return estimate_request(messages, system="system", tools=[], backend=backend, model="test", max_tokens=1024)

    picked = select_window([human(), *batch(1), *batch(2)], estimate=estimator, target=150, latest_batches=1)
    assert picked.estimate.tokens > 0
    assert CONTEXT_META not in str(backend.build_payload(picked.messages, model="test", system="s", tools=[], max_tokens=100, stream=False))


def test_estimation_accepts_union_type_tool_schemas_without_misreading_them_as_media():
    estimate = estimate_request([human()], system="system", tools=[{
        "name": "Progress", "parameters": {"type": "object", "properties": {
            "evidence": {"type": ["array", "object"], "description": "Preserve every Plan criterion"},
        }},
    }])
    assert estimate.tokens > 0
    assert not estimate.media_unknown


def test_policy_uses_explicit_trigger_without_output_or_metadata_cap():
    policy = WindowPolicy(100_000, trigger_tokens=300_000, max_output_tokens=10_000)
    assert policy.threshold == 300_000
    assert policy.target() == 45_000
    assert policy.target(attempt=1) < policy.target()


@pytest.fixture
async def db(tmp_path):
    instance = DB(str(tmp_path / "isolated.db"))
    await instance.connect()
    yield instance
    await instance.close()


def agent_store(db, identity="agent-one", task="task-one"):
    return WindowStore(db, ContextOwner.agent(task_uuid=task, agent_session_uuid=identity, conversation_uuid="conversation"))


async def save(store, messages, **kwargs):
    version = await store.archive(messages)
    return await store.save(messages, expected_revision=version["revision"], expected_source_revision=version["sourceRevision"], route="test-route", **kwargs)


@pytest.mark.asyncio
async def test_agent_history_outlives_window_and_task_checkpoint(db):
    store = agent_store(db)
    messages = [human(), *batch(1, text="x" * 70_000), *batch(2)]
    version = await store.archive(messages)
    event_id = messages[2][CONTEXT_META]["id"]
    picked = select_window(messages, estimate=units, target=40, latest_batches=1)
    await store.save(picked.messages, expected_revision=version["revision"], expected_source_revision=version["sourceRevision"], route="route", rotated=True)
    assert len((await store.event_payload(event_id))["payload"]["content"]) == 70_000
    continued = agent_store(db, task="task-two")
    assert await continued.restore_messages() == picked.messages
    assert len((await continued.event_payload(event_id))["payload"]["content"]) == 70_000


@pytest.mark.asyncio
async def test_private_history_rejects_another_instance(db):
    one, two = agent_store(db), agent_store(db, "agent-two")
    messages = [human()]
    await save(one, messages)
    with pytest.raises(ContextHistoryUnavailable):
        await two.event_payload("u1")
    assert (await two.index())["events"] == []


@pytest.mark.asyncio
async def test_repeated_checkpoints_do_not_duplicate_agent_history(db):
    store = agent_store(db)
    messages = [human(), *batch(1)]
    await save(store, messages)
    await save(store, messages)
    assert len((await store.index())["events"]) == 3


@pytest.mark.asyncio
async def test_late_usage_cannot_repopulate_a_rotated_window(db):
    store = agent_store(db)
    messages = [human(), *batch(1)]
    await save(store, messages)
    ticket = await store.begin_request(route="test-route")
    await save(store, messages, rotated=True)
    assert not await store.observe_usage(ticket, tokens=300_000)
    assert not (await store.load())["usage_known"]
    fresh = await store.begin_request(route="test-route")
    assert await store.observe_usage(fresh, tokens=30_000)
    assert (await store.load())["usage_tokens"] == 30_000


@pytest.mark.asyncio
async def test_same_window_older_request_cannot_override_later_request(db):
    store = agent_store(db)
    await save(store, [human()])
    old = await store.begin_request(route="test-route")
    new = await store.begin_request(route="test-route")
    assert await store.observe_usage(new, tokens=20_000)
    assert not await store.observe_usage(old, tokens=100)
    assert (await store.load())["usage_tokens"] == 20_000


@pytest.mark.asyncio
async def test_control_arriving_after_selection_rejects_window_commit(db):
    store = agent_store(db)
    messages = [human(), *batch(1)]
    selected_at = await store.archive(messages)
    control = mark_source({"role": "user", "content": "stop"}, kind="control")
    await store.archive([control])
    with pytest.raises(StaleWindow):
        await store.save(messages, expected_revision=selected_at["revision"], expected_source_revision=selected_at["sourceRevision"], route="route", rotated=True)
    assert (await store.load())["window_version"] == 0


@pytest.mark.asyncio
async def test_failed_save_transaction_does_not_publish_rotation(db):
    store = agent_store(db)
    messages = [human()]
    at = await store.archive(messages)
    never_archived = mark_source({"role": "assistant", "content": "missing"})
    with pytest.raises(ContextHistoryUnavailable):
        await store.save([*messages, never_archived], expected_revision=at["revision"], expected_source_revision=at["sourceRevision"], route="route", rotated=True)
    assert (await store.load())["window_version"] == 0
    cur = await db.conn.execute("SELECT COUNT(*) AS n FROM context_window_rotations")
    assert (await cur.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_history_snapshot_pagination_does_not_skip_new_appends(db):
    store = agent_store(db)
    messages = [human(), *batch(1), *batch(2)]
    await store.archive(messages)
    first = await store.index(limit=2)
    await store.archive(batch(3))
    next_page = await store.index(after=first["nextAfter"], high_water=first["highWater"], limit=10)
    assert len(next_page["events"]) == 3
    assert not next_page["hasMore"]
    assert len((await store.index())["events"]) == 7


@pytest.mark.asyncio
async def test_archive_source_mutation_is_rejected_without_partial_append(db):
    store = agent_store(db)
    messages = [human(), *batch(1)]
    await store.archive(messages)
    messages[0]["content"] = "silently changed user instruction"
    with pytest.raises(StaleWindow):
        await store.archive([mark_source({"role": "assistant", "content": "new"}), *messages])
    assert len((await store.index())["events"]) == 3


@pytest.mark.asyncio
async def test_history_index_distinguishes_calls_results_and_searches_unescaped_text(db):
    store = agent_store(db)
    records = batch(1, text='{"part": 1, "tag": "exact\\quoted"}')
    records[0]['tool_calls'][0].arguments = '{"part":1}'
    await store.archive(records)
    found = await store.index(query='"part"')
    assert [(item['role'], item['toolName']) for item in found['events']] == [
        ('assistant', 'Read'), ('tool', 'Read')]
    assert found['events'][0]['toolCallId'] == found['events'][1]['toolCallId'] == 't1'
    assert 'text' not in found['events'][0]


@pytest.mark.asyncio
async def test_same_model_id_on_another_provider_invalidates_usage_not_selection(db):
    from types import SimpleNamespace

    from app.context.runtime import WindowRuntime
    from app.llm.events import Usage

    store = agent_store(db)
    backend = SimpleNamespace(protocol='responses', _base='https://one.invalid')
    runtime = WindowRuntime(store, WindowPolicy(100000, trigger_tokens=90000), backend=backend, model='same-id')
    messages = await runtime.prepare([human(), *batch(1)], system='same system', tools=[])
    await runtime.begin_request()
    await runtime.observe_usage(Usage(input_tokens=4000))
    old_version = runtime.window_version
    old_route = runtime.route
    backend._base = 'https://two.invalid'
    changed = await runtime.prepare(messages, system='same system', tools=[])
    assert changed == messages
    assert runtime.route != old_route
    assert runtime.window_version > old_version
    assert not (await store.load())['usage_known']
    assert await store.restore_messages() == messages
