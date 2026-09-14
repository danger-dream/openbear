"""Old-root notification recovery: temporary SQLite and recording providers only."""
from __future__ import annotations

import copy

import pytest

from app.agent.native_continuation import validate_model_context
from app.context.builder import build_controller_history
from app.context.resume import restore_controller_run_inputs
from app.context.runtime import ContextManager
from app.context.store import ContextOwner, WindowStore
from app.context.window import WindowPolicy, source_of
from app.db.dao import MessageDAO
from app.llm.events import ToolCall
from app.web_console.live_stream import _WebStreamRenderer
from tests.test_conversation_restart import configure_web, run_web, seed_turn
from tests.test_web_admin import web_env as shared_web_env

web_env = shared_web_env


@pytest.fixture(autouse=True)
async def no_metadata_subscription(web_env):
    await web_env.server.global_realtime.close()


async def evicted_root(env, monkeypatch):
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123)
    chat, conv = row["internal_chat_id"], row["conversation_uuid"]
    dao = MessageDAO(env.db)
    await seed_turn(env, row, "preceding", "previous question", answer="A_PROPOSAL: inspect files without deployment")
    assert await run_web(env, row, "root-A", "A_ORIGINAL: inspect only; do not deploy [doc](openbear://ref/doc/1)",
                         ref="A_FROZEN_REFERENCE: authorized scope is only the requested files")
    await dao.add(chat, "assistant", "A_CONFIRM_QUESTION", tool_calls=[ToolCall("decision", "UserInteraction", "{}")],
                  turn_uuid="root-A", run_root_turn_uuid="root-A", conversation_uuid=conv)
    await dao.add(chat, "tool", '{"answer":"A_CONFIRMED: inspection only"}', name="UserInteraction", tool_call_id="decision",
                  turn_uuid="root-A", run_root_turn_uuid="root-A", conversation_uuid=conv)
    await dao.add(chat, "user", "A_FEEDBACK: keep the exact font size", turn_uuid="feedback-A", run_root_turn_uuid="root-A", conversation_uuid=conv)
    await seed_turn(env, row, "old-B", "unrelated old question", answer="OLD_OPTIONAL_EVIDENCE " * 5000)
    await seed_turn(env, row, "later-B", "another short exchange", answer="short answer")
    assert await run_web(env, row, "root-B", "B_NEW_TASK: " + "B optional " * 280)
    sid = await dao.get_or_create_session_uuid(chat)
    store = WindowStore(env.db, ContextOwner.controller(chat_id=chat, session_uuid=sid))
    assert "A_ORIGINAL" not in str(await store.restore_messages())
    assert "A_FEEDBACK" not in str(await store.restore_messages())
    return row, backend, store


async def notify(env, row, root, content):
    live = env.server._live_for(row)
    payload = {"content": content, "runRootTurnUuid": root}
    assert await env.server._root_turn_for_task_notification(row["conversation_uuid"], payload) == root
    await live.publish({"type": "accepted", "turnUuid": root, "runUuid": "notification-resume"})
    renderer = _WebStreamRenderer(live)
    try:
        return await env.server._run_web_turn(row["internal_chat_id"], content, renderer, conversation=row,
            root_turn_uuid=root, task_notification=True, task_notification_payload=payload)
    finally:
        await renderer.close()


@pytest.mark.parametrize("size", ["small", "large", "oversized"])
async def test_evicted_root_restores_originals_decisions_and_frozen_references(web_env, monkeypatch, size):
    env = web_env
    row, backend, store = await evicted_root(env, monkeypatch)
    before = await store.load()
    calls = backend.calls
    content = "A_BACKGROUND_RESULT " + "detail " * {"small": 1, "large": 4000, "oversized": 7000}[size]
    result = await notify(env, row, "root-A", content)
    if size == "oversized":
        assert not result and backend.calls == calls
        error = (await env.server._conversation_row(123, row["conversation_uuid"]))["last_error"]
        assert "required_context_too_large" in error and "active_execution_input_missing" not in error
        assert (await store.load())["state"] == before["state"]
        return
    assert result and backend.calls == calls + 1
    sent = backend.seen_convos[calls]
    assert validate_model_context(sent)
    for marker in ("A_ORIGINAL", "A_PROPOSAL", "A_FEEDBACK", "A_CONFIRM_QUESTION", "A_CONFIRMED", "A_FROZEN_REFERENCE", "A_BACKGROUND_RESULT"):
        assert marker in str(sent)
    assert "OLD_OPTIONAL_EVIDENCE" not in str(sent)
    notification = next(m for m in sent if "A_BACKGROUND_RESULT" in str(m.get("content")))
    assert source_of(notification)["kind"] == "notification"
    if size == "large":
        assert (await store.load())["window_version"] > before["window_version"]
        assert "B_NEW_TASK" not in str(sent)
    else:
        assert (await store.load())["window_version"] == before["window_version"]
    # The frozen overlay never becomes the semantic input archived/checkpointed.
    assert "A_FROZEN_REFERENCE" not in str((await store.load())["state"]["messages"])
    original = await store.event_payload(next(source_of(m)["id"] for m in sent if "A_ORIGINAL" in str(m.get("content"))))
    assert "A_FROZEN_REFERENCE" not in str(original)


@pytest.mark.parametrize("size", [1, 7000])
@pytest.mark.parametrize("missing", ["unknown_root", "deleted_input", "notification_only"])
async def test_missing_trusted_input_blocks_before_model_or_window_commit(web_env, monkeypatch, missing, size):
    env = web_env
    row, backend, store = await evicted_root(env, monkeypatch)
    dao = MessageDAO(env.db)
    chat = row["internal_chat_id"]
    root = "root-A" if missing == "deleted_input" else "unknown"
    if missing == "deleted_input":
        await env.db.conn.execute("DELETE FROM messages WHERE chat_id=? AND run_root_turn_uuid=? AND role='user'", (chat, root))
        await env.db.conn.commit()
    elif missing == "notification_only":
        # A role=user receipt with no input provenance is not user authorization.
        await dao.add(chat, "user", "background receipt", turn_uuid=root, run_root_turn_uuid=root)
    before = await store.load()
    calls = backend.calls
    assert not await notify(env, row, root, "background result " * size)
    assert backend.calls == calls
    after = await store.load()
    assert after["state"] == before["state"]
    assert (after["revision"], after["window_version"]) == (before["revision"], before["window_version"])
    assert "active_execution_input_missing" in (await env.server._conversation_row(123, row["conversation_uuid"]))["last_error"]


async def test_unarchived_input_requires_original_user_operation_and_keeps_history_unchanged(web_env, monkeypatch):
    env = web_env
    await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123)
    await seed_turn(env, row, "legacy-root", "ORIGINAL [doc](openbear://ref/doc/1)", ref="FROZEN_LEGACY")
    dao = MessageDAO(env.db)
    history = await build_controller_history(dao, row["internal_chat_id"])
    original = copy.deepcopy(history)
    restored = await restore_controller_run_inputs(dao, row["internal_chat_id"], history,
        session_uuid=await dao.get_or_create_session_uuid(row["internal_chat_id"]), run_root_turn_uuid="legacy-root",
        reference_store=env.server._reference_store())
    assert history == original
    assert any(m.get("openbear_reference_bundle") for m in restored)
    assert await notify(env, row, "legacy-root", "background completed")


async def test_legacy_reference_rehydration_keeps_archived_original_immutable(web_env, monkeypatch):
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123)
    await seed_turn(env, row, "legacy-root", "LEGACY [doc](openbear://ref/doc/1)", ref="LEGACY_FROZEN")
    dao = MessageDAO(env.db)
    chat = row["internal_chat_id"]
    sid = await dao.get_or_create_session_uuid(chat)
    store = WindowStore(env.db, ContextOwner.controller(chat_id=chat, session_uuid=sid))
    # Older windows archived the semantic input before bundle metadata existed.
    history = await build_controller_history(dao, chat)
    manager = ContextManager(store, WindowPolicy(128000, trigger_tokens=8000), backend=backend, model="fake-gpt")
    await manager.checkpoint(history)
    original_id = source_of(history[0])["id"]
    before = await store.event_payload(original_id)
    await manager.checkpoint([])
    assert await notify(env, row, "legacy-root", "background completed")
    assert "LEGACY_FROZEN" in str(backend.seen_convos[-1])
    assert await store.event_payload(original_id) == before
    restored = await store.restore_messages()
    original_view = next(m for m in restored if "LEGACY [doc]" in str(m.get("content")))
    assert source_of(original_view)["derived_from"] == original_id
    assert source_of(original_view)["id"] != original_id


async def test_retained_root_leaves_native_prefix_intact(web_env, monkeypatch):
    env = web_env
    await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123)
    assert await run_web(env, row, "retained", "USER_SCOPE")
    dao = MessageDAO(env.db)
    chat = row["internal_chat_id"]
    history = await build_controller_history(dao, chat, reference_store=env.server._reference_store())
    history[-1]["native_output_items"] = [{"type": "reasoning", "encrypted_content": "SIGNED_PREFIX"}]
    before = copy.deepcopy(history)
    result = await restore_controller_run_inputs(dao, chat, history,
        session_uuid=await dao.get_or_create_session_uuid(chat), run_root_turn_uuid="retained",
        reference_store=env.server._reference_store())
    assert result is history and result == before
