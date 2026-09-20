"""Upstream models sometimes emit duplicate tool_call ids in one batch.

Observed live on Cursor/claude-fable-5-1 (2026-09-15): one assistant response
contained two complete tool_calls sharing a single toolu_ id. OpenBear executed
both, persisted two identical tool rows, and then:

1. ``validate_model_context`` rejected the window checkpoint
   (ValueError: context_window_has_incomplete_tool_batch), and
2. the next controller turn could not adopt the failed turn's durable rows
   (StaleWindow: controller_unrecognized_execution_append), wedging the chat.

These tests pin normalization and fail-closed handling of malformed old batches.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.loop import Agent
from app.agent.tool_call_hooks import normalize_tool_calls
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
    db = DB(str(tmp_path / "dup.db"))
    await db.connect()
    dao = MessageDAO(db)
    chat = 9
    session = await dao.get_or_create_session_uuid(chat)
    user_id = await dao.add(chat, "user", "Run the search.")
    messages = [mark_source({"role": "user", "content": "Run the search."},
                            kind="human", source_id=f"message:{user_id}", message_id=user_id)]
    store = WindowStore(db, ContextOwner.controller(chat_id=chat, session_uuid=session))
    persister = _WebDBPersister(dao, chat, session_uuid=session)
    yield SimpleNamespace(db=db, dao=dao, chat=chat, store=store, messages=messages,
                          persister=persister, user_id=user_id)
    await db.close()


def manager(env, backend, strategy="sliding_window"):
    async def resolve():
        return strategy
    runtime = ContextManager(env.store, WindowPolicy(128000, trigger_tokens=32000),
                             backend=backend, model="test", strategy_resolver=resolve)
    env.persister.window_runtime = runtime
    return runtime


# ---------------------------------------------------------------- normalization


def test_normalize_drops_duplicate_call_ids():
    calls = [ToolCall("dup", "History", '{"action": "search"}'),
             ToolCall("dup", "History", '{"action": "search"}'),
             ToolCall("other", "Read", '{}')]
    assert normalize_tool_calls(calls) == [calls[0], calls[2]]


def test_normalize_dedupes_idless_calls_by_name_and_arguments():
    calls = [ToolCall("", "Bash", '{"command": "ls"}'),
             ToolCall("", "Bash", '{"command": "ls"}'),
             ToolCall("", "Bash", '{"command": "pwd"}')]
    assert normalize_tool_calls(calls) == [calls[0], calls[2]]


def test_normalize_keeps_same_id_with_different_arguments():
    # Provider guarantees one id == one call; if arguments disagree the batch is
    # ambiguous but the FIRST emission stays authoritative and executable.
    calls = [ToolCall("dup", "Read", '{"path": "a"}'),
             ToolCall("dup", "Read", '{"path": "b"}')]
    assert normalize_tool_calls(calls) == [calls[0]]


def test_normalize_long_agent_ordering_preserved_with_dedup():
    calls = [ToolCall("a", "Agent", '{}'), ToolCall("b", "Read", '{}'),
             ToolCall("a", "Agent", '{}'), ToolCall("c", "Read", '{}')]
    assert normalize_tool_calls(calls) == [calls[1], calls[3], calls[0]]


# ------------------------------------------------------- end-to-end regression


async def test_duplicate_upstream_call_ids_execute_once_and_checkpoint(env):
    """The live failure: one batch, same toolu_ id twice, complete arguments."""
    effects: list[str] = []
    registry = ToolRegistry()

    async def search(args):
        effects.append("search")
        return "durable search result"
    registry.add("History", "test history", {"type": "object", "properties": {}}, search)

    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[
            ToolCall("toolu_dup", "History", '{"action": "search"}'),
            ToolCall("toolu_dup", "History", '{"action": "search"}'),
         ]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="Done after one search."), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    runtime = manager(env, backend)
    renderer = RecordRenderer()
    result = await Agent(backend, registry).run(
        env.messages, renderer, model="test", persister=env.persister, window_runtime=runtime,
    )
    assert effects == ["search"]  # executed exactly once, not twice
    assert result.text == "Done after one search."
    # Exactly one durable tool row; the batch persisted is protocol-valid.
    cur = await env.db.conn.execute(
        "SELECT role, tool_call_id FROM messages WHERE chat_id=? AND role='tool' ORDER BY id", (env.chat,))
    tool_rows = [tuple(row) for row in await cur.fetchall()]
    assert tool_rows == [("tool", "toolu_dup")]
    cur = await env.db.conn.execute(
        "SELECT tool_calls_json FROM messages WHERE chat_id=? AND role='assistant' ORDER BY id", (env.chat,))
    import json as _json
    persisted_calls = [c for row in await cur.fetchall() for c in _json.loads(row[0] or "[]")]
    assert [c["id"] for c in persisted_calls] == ["toolu_dup"]
    from app.agent.native_continuation import validate_model_context
    restored = await env.store.restore_messages()
    assert restored and validate_model_context(restored)
    assert [m["tool_call_id"] for m in restored if m.get("role") == "tool"] == ["toolu_dup"]


async def test_next_turn_does_not_silently_skip_malformed_execution_rows(env):
    """Malformed old execution cannot be marked consumed without its evidence.

    Repairing legacy duplicate transcripts is separate from recovering verified
    complete batches; stop explicitly rather than forgetting actual execution.
    """
    registry = ToolRegistry()
    backend = FakeBackend([
        [StreamEvent(kind="content", text="Recovered turn answer."), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    runtime = manager(env, backend)

    # Simulate the failed turn's durable rows (as _WebDBPersister would write
    # them): assistant with a tool_call + two identical tool results.
    assistant_id = await env.dao.add(env.chat, "assistant", "", tool_calls=[
        ToolCall("toolu_dup", "History", '{"action": "search"}')])
    await env.dao.add(env.chat, "tool", "durable search result", tool_call_id="toolu_dup", name="History")
    await env.dao.add(env.chat, "tool", "durable search result", tool_call_id="toolu_dup", name="History")
    # New user steering arrives for the next turn.
    followup_id = await env.dao.add(env.chat, "user", "Keep going.")
    followup = mark_source({"role": "user", "content": "Keep going."},
                           kind="human", source_id=f"message:{followup_id}", message_id=followup_id)

    from app.context.store import StaleWindow
    with pytest.raises(StaleWindow, match="controller_recovery_requires_closed_tool_batch"):
        await Agent(backend, registry).run(
            [followup], RecordRenderer(), model="test", persister=env.persister, window_runtime=runtime,
        )
    assert backend._round == 0
    assert await env.store.load() is None  # boundary transaction did not advance coverage
    # The execution remains durable, without inventing a recovered answer.
    cur = await env.db.conn.execute("SELECT role FROM messages WHERE chat_id=? ORDER BY id", (env.chat,))
    roles = [row[0] for row in await cur.fetchall()]
    assert roles == ["user", "assistant", "tool", "tool", "user"]
    assert assistant_id < followup_id


async def test_foreign_execution_rows_still_conflict(env):
    """task/agent-owned rows are another owner's execution: still fatal."""
    backend = FakeBackend([
        [StreamEvent(kind="content", text="answer"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    runtime = manager(env, backend)
    await env.dao.add(env.chat, "tool", "agent-owned", tool_call_id="x", name="Bash", task_uuid="task-1")
    followup_id = await env.dao.add(env.chat, "user", "Keep going.")
    followup = mark_source({"role": "user", "content": "Keep going."},
                           kind="human", source_id=f"message:{followup_id}", message_id=followup_id)
    from app.context.store import StaleWindow
    with pytest.raises(StaleWindow):
        await Agent(backend, ToolRegistry()).run(
            [followup], RecordRenderer(), model="test", persister=env.persister, window_runtime=runtime,
        )
