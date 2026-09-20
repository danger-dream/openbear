"""Deterministic Controller boundary races; temporary SQLite and scripted models only."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agent.loop import Agent
from app.context.runtime import ContextManager
from app.context.store import ContextOwner, WindowStore
from app.context.window import WindowPolicy, mark_source
from app.db.dao import MessageDAO
from app.db.engine import DB
from app.llm.events import StreamEvent, ToolCall
from app.tools.base import ToolRegistry
from app.web_console.live_stream import _WebDBPersister
from tests.test_agent_loop import FakeBackend, RecordRenderer


@pytest.fixture
async def env(tmp_path):
    db = DB(str(tmp_path / "boundary.db"))
    await db.connect()
    dao = MessageDAO(db)
    chat = 7
    session = await dao.get_or_create_session_uuid(chat)
    user_id = await dao.add(chat, "user", "Use the tool once, then answer.")
    messages = [mark_source({"role": "user", "content": "Use the tool once, then answer."},
                            kind="human", source_id=f"message:{user_id}", message_id=user_id)]
    store = WindowStore(db, ContextOwner.controller(chat_id=chat, session_uuid=session))
    persister = _WebDBPersister(dao, chat, session_uuid=session)
    yield SimpleNamespace(db=db, dao=dao, chat=chat, store=store, messages=messages,
                          persister=persister, user_id=user_id)
    await db.close()


def manager(env, backend, strategy):
    async def resolve():
        return strategy
    runtime = ContextManager(env.store, WindowPolicy(128000, trigger_tokens=32000),
                             backend=backend, model="test", strategy_resolver=resolve)
    env.persister.window_runtime = runtime
    return runtime


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_completed_reply_with_pinned_reader_does_not_repeat_execution(env, strategy):
    # An unrelated multi-row query pins the SHARED query-only connection. This
    # is deliberately kept open across the final synchronous assistant write.
    await env.dao.add(8, "user", "unrelated A")
    await env.dao.add(8, "assistant", "unrelated B")
    held = []
    effects = []
    registry = ToolRegistry()

    async def effect(args):
        effects.append("one effect")
        return "one durable result"
    registry.add("effect", "test effect", {"type": "object", "properties": {}}, effect)

    class Backend(FakeBackend):
        async def stream(self, messages, **kwargs):
            if self._round == 1:
                cursor = await env.db.conn.execute("SELECT * FROM messages ORDER BY id")
                await cursor.fetchone()
                held.append(cursor)
            async for event in super().stream(messages, **kwargs):
                yield event

    backend = Backend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall("t1", "effect", "{}")]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="The complete final answer."),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])
    runtime = manager(env, backend, strategy)
    renderer = RecordRenderer()
    try:
        result = await Agent(backend, registry).run(
            env.messages, renderer, model="test", persister=env.persister, window_runtime=runtime,
        )
        assert result.text == "The complete final answer."
        assert renderer.final == "🔧 effect …\n\nThe complete final answer."
        assert backend._round == result.model_calls == 2
        assert effects == ["one effect"]
        # Check the authoritative writer, not the intentionally old reader.
        async with env.db.conn.transaction(label="test-verify-final") as conn:
            cur = await conn.execute("SELECT role,content FROM messages WHERE chat_id=? ORDER BY id", (env.chat,))
            rows = [tuple(row) for row in await cur.fetchall()]
            assert rows.count(("assistant", result.text)) == 1
            assert rows.count(("tool", "one durable result")) == 1
            assert len(rows) == 4
            assert result.text in str(await env.store.restore_messages())
    finally:
        for cursor in held:
            await cursor.close()


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_persisted_user_arriving_during_prepare_is_in_first_request(env, strategy, monkeypatch):
    backend = FakeBackend([[StreamEvent(kind="content", text="Following the new instruction."),
                            StreamEvent(kind="finish", finish_reason="stop")]])
    runtime = manager(env, backend, strategy)
    original_save = env.store.save
    arrivals = []

    async def save(*args, **kwargs):
        if not arrivals and kwargs.get("expected_message_high_water") is not None:
            arrivals.append(await env.dao.add(env.chat, "user", "Do not write any files. New original input."))
        return await original_save(*args, **kwargs)
    monkeypatch.setattr(env.store, "save", save)
    result = await Agent(backend, ToolRegistry()).run(
        copy.deepcopy(env.messages), RecordRenderer(), model="test", persister=env.persister,
        window_runtime=runtime,
    )
    assert result.model_calls == backend._round == 1
    assert "Do not write any files. New original input." in str(backend.seen_convos[0])
    assert arrivals
    assert "Do not write any files. New original input." in str(await env.store.restore_messages())
    cur = await env.db.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND role='user'", (env.chat,))
    assert (await cur.fetchone())[0] == 2


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_final_maintenance_append_continues_only_for_new_input(env, strategy, monkeypatch):
    backend = FakeBackend([
        [StreamEvent(kind="content", text="Already completed answer."), StreamEvent(kind="finish", finish_reason="stop")],
        [StreamEvent(kind="content", text="Answer to the new input."), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    runtime = manager(env, backend, strategy)
    save_original = env.store.save
    arrivals, observations = [], []

    async def save(*args, **kwargs):
        boundary = kwargs.get("controller_boundary")
        if boundary is not None and boundary.phase == "post_model_response" and not arrivals:
            # Prove the final answer AND its first checkpoint are already durable.
            observations.append(await env.store.restore_messages())
            arrivals.append(await env.dao.add(env.chat, "user", "One genuinely new follow-up."))
        return await save_original(*args, **kwargs)
    monkeypatch.setattr(env.store, "save", save)
    renderer = RecordRenderer()
    result = await Agent(backend, ToolRegistry()).run(
        env.messages, renderer, model="test", persister=env.persister, window_runtime=runtime,
    )
    assert renderer.cuts == 1  # new input, not repeated finalization of old output
    assert result.text == "Answer to the new input."
    assert result.model_calls == backend._round == 2  # NOT a retry of the first answer
    assert "Already completed answer." in str(observations[0])
    assert "One genuinely new follow-up." in str(backend.seen_convos[1])
    cur = await env.db.conn.execute("SELECT role,content FROM messages WHERE chat_id=? ORDER BY id", (env.chat,))
    assert [tuple(row) for row in await cur.fetchall()] == [
        ("user", "Use the tool once, then answer."), ("assistant", "Already completed answer."),
        ("user", "One genuinely new follow-up."), ("assistant", "Answer to the new input."),
    ]


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
@pytest.mark.parametrize("mutation", ["edit", "delete", "invalidate", "window", "session", "foreign_execution"])
async def test_non_append_conflicts_are_not_retried(env, strategy, mutation, monkeypatch):
    from app.context.store import StaleWindow

    backend = FakeBackend([[StreamEvent(kind="content", text="must not be called")]])
    runtime = manager(env, backend, strategy)
    save_original = env.store.save
    attempts = []

    async def save(*args, **kwargs):
        if kwargs.get("controller_boundary") is not None:
            attempts.append(1)
            async with env.db.conn.transaction(label="test-conflicting-edit") as conn:
                if mutation == "edit":
                    await conn.execute("UPDATE messages SET content='edited original' WHERE id=?", (env.user_id,))
                elif mutation == "delete":
                    await conn.execute("DELETE FROM messages WHERE id=?", (env.user_id,))
                elif mutation == "invalidate":
                    await conn.execute("DELETE FROM context_windows WHERE owner_key=?", (env.store.owner.key,))
                elif mutation == "window":
                    await conn.execute("UPDATE context_windows SET revision=revision+1,state_json='{}' WHERE owner_key=?", (env.store.owner.key,))
                elif mutation == "session":
                    await conn.execute("UPDATE sessions SET session_uuid='replacement-session' WHERE chat_id=?", (env.chat,))
            # An accompanying append must NOT camouflage the edit/deletion/new
            # owner as a recoverable max(id) increment.
            await env.dao.add(env.chat, "assistant" if mutation == "foreign_execution" else "user", "concurrent append")
        return await save_original(*args, **kwargs)
    monkeypatch.setattr(env.store, "save", save)
    with pytest.raises(StaleWindow) as caught:
        await Agent(backend, ToolRegistry()).run(
            env.messages, RecordRenderer(), model="test", persister=env.persister, window_runtime=runtime,
        )
    assert type(caught.value) is StaleWindow
    assert backend._round == 0
    assert attempts == [1]
    assert caught.value.metadata["owner"] == env.store.owner.key
    assert caught.value.metadata["phase"] == "pre_model_request"
    assert caught.value.metadata["actualHighWater"] > caught.value.metadata["expectedHighWater"]
    if mutation == "invalidate":
        assert await env.store.load() is None  # no automatic resurrection


async def test_edit_after_append_failure_before_refresh_still_rejected(env, monkeypatch):
    from app.context.store import ControllerMessagesAppended, StaleWindow

    backend = FakeBackend([[StreamEvent(kind="content", text="must not run")]])
    runtime = manager(env, backend, "sliding_window")
    original_save = env.store.save
    attempts = []

    async def save(*args, **kwargs):
        attempts.append(1)
        await env.dao.add(env.chat, "user", "new input")
        try:
            return await original_save(*args, **kwargs)
        except ControllerMessagesAppended:
            await env.db.conn.execute("UPDATE messages SET content='edited between attempts' WHERE id=?", (env.user_id,))
            await env.db.conn.commit()
            raise
    monkeypatch.setattr(env.store, "save", save)
    with pytest.raises(StaleWindow, match="controller_history_changed"):
        await Agent(backend, ToolRegistry()).run(
            env.messages, RecordRenderer(), model="test", persister=env.persister, window_runtime=runtime,
        )
    assert backend._round == 0 and attempts == [1]


async def test_continuous_arrival_is_bounded_and_originals_remain_durable(env, monkeypatch):
    from app.context.store import StaleWindow

    backend = FakeBackend([[StreamEvent(kind="content", text="must not run")]])
    runtime = manager(env, backend, "sliding_window")
    original_save = env.store.save
    arrivals = []

    async def save(*args, **kwargs):
        arrivals.append(await env.dao.add(env.chat, "user", f"original arrival {len(arrivals)}"))
        return await original_save(*args, **kwargs)
    monkeypatch.setattr(env.store, "save", save)
    with pytest.raises(StaleWindow, match="controller_inputs_changed_repeatedly_before_request"):
        await Agent(backend, ToolRegistry()).run(
            env.messages, RecordRenderer(), model="test", persister=env.persister, window_runtime=runtime,
        )
    assert len(arrivals) == 3 and backend._round == 0
    cur = await env.db.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND role='user'", (env.chat,))
    assert (await cur.fetchone())[0] == 4


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_input_before_boundary_capture_is_not_hidden_by_own_final_checkpoint(env, strategy, monkeypatch):
    backend = FakeBackend([
        [StreamEvent(kind="content", text="Initial answer."), StreamEvent(kind="finish", finish_reason="stop")],
        [StreamEvent(kind="content", text="New answer."), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    runtime = manager(env, backend, strategy)
    original_persist = env.persister.save_assistant
    arrivals = []

    async def persist(**kwargs):
        if not arrivals:
            arrivals.append(await env.dao.add(env.chat, "user", "Input just before the final row."))
        await original_persist(**kwargs)
    monkeypatch.setattr(env.persister, "save_assistant", persist)
    result = await Agent(backend, ToolRegistry()).run(
        env.messages, RecordRenderer(), model="test", persister=env.persister, window_runtime=runtime,
    )
    assert result.text == "New answer."
    assert backend._round == 2
    assert "Input just before the final row." in str(backend.seen_convos[1])
    assert "Input just before the final row." in str(await env.store.restore_messages())


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_selection_race_rebuilds_originals_overlay_and_queued_control(env, strategy, monkeypatch):
    import asyncio
    import json

    from app.config import Config
    from app.context.strategies import ModelSummaryStrategy
    from app.references import ReferenceMaterials
    from tests.test_context_strategies import Backend as SummaryBackend
    from tests.test_context_window import batch

    backend = FakeBackend([[StreamEvent(kind="content", text="New instructions and frozen reference respected."),
                            StreamEvent(kind="finish", finish_reason="stop")]])
    runtime = manager(env, backend, strategy)
    runtime.policy = WindowPolicy(128000, trigger_tokens=2400)  # isolated test budget, no config mutation
    cfg = Config.model_validate({"telegram": {"botToken": "test"},
        "models": {"primary": "p/m", "providers": {"p": {"baseUrl": "http://not-called.invalid", "apiKey": "test",
            "protocol": "chat", "models": [{"id": "m"}]}}},
        "agent": {"keepRecentMessages": 2, "compactMaxRetries": 0}, "memory": {}})
    summary_backend = SummaryBackend()
    runtime.strategies["model_summary"] = ModelSummaryStrategy(
        cfg, SimpleNamespace(backend_for=lambda label: (summary_backend, "m", 8192)), "p/m")
    originals = [*env.messages, *batch(1, text="old evidence " * 1800),
                 *batch(2, text="more old evidence " * 1800), *batch(3), *batch(4)]
    runtime.active_run_root_turn_uuid = "run"
    for message in originals:
        message["openbear_context_source"]["run_root_turn_uuid"] = "run"
    selected_event, appended_event = asyncio.Event(), asyncio.Event()
    selected_inputs, queued, logs = [], [], []
    compress = runtime.strategies[strategy].compress

    async def gated_compress(request):
        selected_inputs.append(copy.deepcopy(request.messages))
        result = await compress(request)
        if len(selected_inputs) == 1:
            selected_event.set()
            await appended_event.wait()
        return result
    monkeypatch.setattr(runtime.strategies[strategy], "compress", gated_compress)
    monkeypatch.setattr("app.context.store.log.warning", lambda event, **kw: logs.append({"event": event, **kw}))

    async def arriving_user():
        await selected_event.wait()
        # Real concurrent task commits the original and its frozen binding in a
        # single transaction while the old selection is awaiting completion.
        async with env.db.conn.transaction(label="test-new-input-and-reference") as conn:
            message_id = await env.dao.add(env.chat, "user", "New input [Evidence](openbear://ref/doc/test)", run_root_turn_uuid="run", commit=False)
            await conn.execute("INSERT INTO web_operation_messages(conversation_uuid,op_id,message_id,created_at_ms) VALUES('c','op',?,1)", (message_id,))
            await conn.execute("INSERT INTO web_reference_bundles(bundle_uuid,conversation_uuid,op_id,manifest_json,material_json,created_at) VALUES('bundle','c','op','[]',?,1)",
                               (json.dumps([{"label": "Evidence", "text": "FROZEN ORIGINAL CONTENT", "kind": "doc"}]),))
        queued.append("A queued control: do not deploy.")
        appended_event.set()

    def drain():
        out, queued[:] = list(queued), []
        return out

    references = ReferenceMaterials(env.db)
    async def overlay(messages):
        return await references.overlay(messages, conversation_uuid="c")

    incoming = asyncio.create_task(arriving_user())
    try:
        result = await Agent(backend, ToolRegistry()).run(
            originals, RecordRenderer(), model="test", persister=env.persister, window_runtime=runtime,
            steer_drain=drain, model_request_overlay=overlay,
        )
        await incoming
    finally:
        if not incoming.done():
            incoming.cancel()
            await asyncio.gather(incoming, return_exceptions=True)
    assert result.model_calls == backend._round == 1
    assert len(selected_inputs) == 2
    assert "old evidence " * 1800 in str(selected_inputs[1])  # retry from originals, NOT discarded selection
    assert "New input" in str(selected_inputs[1])
    assert "FROZEN ORIGINAL CONTENT" in str(backend.seen_convos[0])
    assert "A queued control: do not deploy." in str(backend.seen_convos[0])
    assert len(summary_backend.calls) == (2 if strategy == "model_summary" else 0)
    restored = await env.store.restore_messages()
    assert "bundle" in str(restored)
    assert "FROZEN ORIGINAL CONTENT" not in str(restored)  # request-only expansion
    conflicts = [row for row in logs if row["event"] == "context.window_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["owner"] == env.store.owner.key
    assert conflicts[0]["phase"] == "pre_model_request"
    assert conflicts[0]["expectedHighWater"] == env.user_id
    assert conflicts[0]["actualHighWater"] > env.user_id
    assert not any(text in str(conflicts) for text in ("old evidence", "New input", "FROZEN ORIGINAL CONTENT", "test-secret"))


async def test_final_nonrecoverable_conflict_keeps_completed_reply_without_reexecution(env, monkeypatch):
    from app.context.store import StaleWindow

    backend = FakeBackend([[StreamEvent(kind="content", text="Completed before invalidation."),
                            StreamEvent(kind="finish", finish_reason="stop")]])
    runtime = manager(env, backend, "sliding_window")
    original_save = env.store.save

    async def save(*args, **kwargs):
        boundary = kwargs.get("controller_boundary")
        if boundary and boundary.phase == "post_model_response":
            await env.db.conn.execute("UPDATE context_windows SET revision=revision+1 WHERE owner_key=?", (env.store.owner.key,))
            await env.db.conn.commit()
        return await original_save(*args, **kwargs)
    monkeypatch.setattr(env.store, "save", save)
    with pytest.raises(StaleWindow, match="context_source_or_window_changed") as caught:
        await Agent(backend, ToolRegistry()).run(
            env.messages, RecordRenderer(), model="test", persister=env.persister, window_runtime=runtime,
        )
    assert backend._round == 1
    assert caught.value.metadata["phase"] == "post_model_response"
    assert caught.value.metadata["expectedHighWater"] == caught.value.metadata["actualHighWater"]
    cur = await env.db.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND content='Completed before invalidation.'", (env.chat,))
    assert (await cur.fetchone())[0] == 1
    assert "Completed before invalidation." in str(await env.store.restore_messages())


@pytest.mark.parametrize("change", ["none", "new_user", "new_execution", "edit", "delete"])
async def test_legacy_private_anchor_proves_only_its_exact_covered_prefix(env, change):
    from app.context.store import StaleWindow
    from app.db.dao import SummaryDAO

    # Legacy sidecars legitimately project a compacted transcript without a
    # one-to-one source ID on every private message. Obtain real DAO evidence,
    # rather than making up a permissive high-water for the boundary guard.
    await SummaryDAO(env.db).add(env.chat, "existing summary", env.user_id, 2)
    await env.dao.mark_compacted(env.chat, env.user_id)
    covered_id = await env.dao.add(env.chat, "assistant", "covered visible execution")
    identity = dict(conversation_uuid="c", session_id=env.store.owner.session_uuid,
                    protocol="fake", model="test", model_label="p/test")
    private = [{"role": "user", "content": "private summary projection"},
               {"role": "assistant", "content": "private acknowledgment"},
               {"role": "user", "content": "private continuation input"}]
    await env.dao.save_controller_model_context(env.chat, **identity, state={"messages": private})
    restored = await env.dao.load_controller_model_context(env.chat, **identity)
    assert restored and restored["messages"] == private
    assert restored["anchor"]["sourceHighWater"] == covered_id

    if change in {"new_user", "new_execution"}:
        await env.dao.add(env.chat, "user" if change == "new_user" else "assistant", "unconsumed new original")
    elif change == "edit":
        await env.db.conn.execute("UPDATE messages SET content='edited covered execution' WHERE id=?", (covered_id,))
        await env.db.conn.commit()
    elif change == "delete":
        await env.db.conn.execute("DELETE FROM messages WHERE id=?", (env.user_id,))
        await env.db.conn.commit()

    kwargs = dict(since=None, phase="pre_model_request", restored_anchor=restored["anchor"])
    if change in {"new_execution", "edit", "delete"}:
        reason = "controller_unrecognized_execution_append" if change == "new_execution" else "restored_controller_history_changed"
        with pytest.raises(StaleWindow, match=reason):
            await env.store.controller_boundary(private, **kwargs)
        assert await env.store.load() is None
        return

    boundary, additions = await env.store.controller_boundary(private, **kwargs)
    assert [m["content"] for m in additions] == (["unconsumed new original"] if change == "new_user" else [])
    assert env.user_id in boundary.rows and covered_id in boundary.rows
    runtime = manager(env, FakeBackend([]), "sliding_window")
    prepared = await runtime.prepare(copy.deepcopy(private) + additions, system="", tools=[], controller_boundary=boundary,
                                     expected_message_high_water=boundary.high_water)
    assert [{k: v for k, v in m.items() if k != "openbear_context_source"} for m in prepared[:3]] == private
    assert (await env.store.load())["state"]["sourceMessageHighWater"] == boundary.high_water
    # After lazy migration the independent window carries the proof's coverage,
    # even if no new public message supplied an archive message_id at all.
    _, next_additions = await env.store.controller_boundary(prepared, since=None, phase="next_request")
    assert next_additions == []


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
@pytest.mark.parametrize("execution", ["none", "complete", "incomplete"])
async def test_stale_history_backfills_before_latest_input_without_forgetting_execution(env, strategy, execution):
    """Real history loader on a pinned reader, then writer-backed recovery.

    A previous run committed originals but stopped before checkpointing. The
    next run must retain their order/effects, not treat older rows as new steers
    or as discardable execution just because the next user has a larger ID.
    """
    from app.agent.native_continuation import validate_model_context
    from app.context.builder import build_controller_history
    from app.context.store import StaleWindow
    from app.context.window import source_of

    initial = manager(env, FakeBackend([]), strategy)
    await initial.prepare(copy.deepcopy(env.messages), system="", tools=[])
    before = await env.store.load(fresh=True)
    for index in range(4):
        await env.dao.add(8, "user", f"unrelated snapshot row {index}")
    held = await env.db.conn.execute("SELECT * FROM messages ORDER BY id")
    await held.fetchone()
    try:
        old_id = await env.dao.add(env.chat, "user", "OLD instruction: perform the effect once.", turn_uuid="old-turn")
        execution_ids = []
        if execution != "none":
            execution_ids.append(await env.dao.add(env.chat, "assistant", "", tool_calls=[
                ToolCall("already-done", "effect", "{}")], turn_uuid="old-turn"))
            if execution == "complete":
                execution_ids.append(await env.dao.add(env.chat, "tool", "EFFECT_ALREADY_COMPLETED",
                    tool_call_id="already-done", name="effect", turn_uuid="old-turn"))
        # This is the production loader, not a hand-picked [followup] context.
        history = await build_controller_history(env.dao, env.chat)
        assert [source_of(m).get("message_id") for m in history] == [env.user_id]
        latest_text = "LATEST instruction: cancel; do not repeat the effect."
        latest_id = await env.dao.add(env.chat, "user", latest_text, turn_uuid="new-turn")
        latest = mark_source({"role": "user", "content": latest_text}, kind="human",
                             source_id=f"message:{latest_id}", message_id=latest_id, turn_uuid="new-turn")
        latest_copy = copy.deepcopy(latest)
        backend = FakeBackend([[StreamEvent(kind="content", text="Acknowledged without another effect."),
                                StreamEvent(kind="finish", finish_reason="stop")]])
        runtime = manager(env, backend, strategy)
        effects = []
        registry = ToolRegistry()

        async def effect(args):
            effects.append("unexpected replay")
            return "must not execute"
        registry.add("effect", "test effect", {"type": "object", "properties": {}}, effect)
        renderer = RecordRenderer()
        run = Agent(backend, registry).run(history + [latest], renderer, model="test",
                                         persister=env.persister, window_runtime=runtime)
        if execution == "incomplete":
            with pytest.raises(StaleWindow, match="controller_recovery_requires_closed_tool_batch"):
                await run
            assert backend._round == 0 and not effects
            assert await env.store.load(fresh=True) == before
            return
        result = await run
        assert result.model_calls == backend._round == 1
        assert result.steered == 1  # restored assistant/tool rows are not user steers
        assert not effects and renderer.cuts == 0
        request = backend.seen_convos[0]
        assert validate_model_context(request)
        source_ids = [source_of(m).get("message_id") for m in request if source_of(m).get("message_id")]
        assert source_ids == [env.user_id, old_id, *execution_ids, latest_id]
        assert [m["content"] for m in request if m.get("role") == "user"] == [
            env.messages[0]["content"], "OLD instruction: perform the effect once.", latest_text]
        assert latest == latest_copy
        async with env.db.conn.transaction(label="verify-recovered-historical-tail") as conn:
            restored = await env.store.restore_messages()
            assert restored and validate_model_context(restored)
            assert [source_of(m).get("message_id") for m in restored[:-1]] == source_ids
            if execution == "complete":
                assert [m["content"] for m in restored if m.get("role") == "tool"] == ["EFFECT_ALREADY_COMPLETED"]
            cur = await conn.execute("SELECT message_id FROM context_execution_events WHERE owner_key=? ORDER BY seq",
                                     (env.store.owner.key,))
            archived = [row[0] for row in await cur.fetchall()]
            assert all(row_id in archived for row_id in [old_id, *execution_ids])
            cur = await conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (env.chat,))
            assert (await cur.fetchone())[0] == 4 + len(execution_ids)
        # A later normal reload observes the committed selection. While held is
        # open the loader intentionally still sees the old reader snapshot.
        await held.close()
        held = None
        assert await build_controller_history(env.dao, env.chat) == restored
    finally:
        if held is not None:
            await held.close()


async def test_backfilled_user_keeps_existing_tool_batch_closed(env):
    from app.agent.native_continuation import validate_model_context
    from app.context.store import merge_controller_additions
    from app.context.window import source_of

    # The old input was persisted between the assistant and its tool result.
    # Backfilling it must not split the batch or overwrite a rich current input.
    assistant_id = await env.dao.add(env.chat, "assistant", "", tool_calls=[ToolCall("done", "effect", "{}")])
    old_id = await env.dao.add(env.chat, "user", "older queued input")
    tool_id = await env.dao.add(env.chat, "tool", "done", tool_call_id="done", name="effect")
    latest_id = await env.dao.add(env.chat, "user", "latest cancellation")
    messages = [*env.messages,
        mark_source({"role": "assistant", "content": "", "tool_calls": [ToolCall("done", "effect", "{}")]},
                    kind="execution", source_id=f"message:{assistant_id}", message_id=assistant_id),
        mark_source({"role": "tool", "content": "done", "tool_call_id": "done", "name": "effect"},
                    kind="execution", source_id=f"message:{tool_id}", message_id=tool_id),
        mark_source({"role": "user", "content": [{"type": "text", "text": "latest cancellation"}]},
                    kind="human", source_id=f"message:{latest_id}", message_id=latest_id)]
    original = copy.deepcopy(messages)
    _, additions = await env.store.controller_boundary(messages, since=None, phase="pre_model_request")
    merged = merge_controller_additions(messages, additions)
    assert validate_model_context(merged)
    assert [source_of(m).get("message_id") for m in merged] == [env.user_id, assistant_id, tool_id, old_id, latest_id]
    assert messages == original and merged[-1] is messages[-1]
