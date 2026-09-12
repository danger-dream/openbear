"""Real Controller loop, SQLite history, and native checkpoint integration."""
from __future__ import annotations

import json

import pytest

from app.agent.loop import Agent
from app.context.builder import build_controller_history
from app.context.runtime import WindowRuntime
from app.context.store import ContextOwner, WindowStore
from app.context.window import WindowPolicy, mark_source
from app.db.dao import MessageDAO, SummaryDAO
from app.db.engine import DB
from app.llm.events import StreamEvent, ToolCall, Usage
from app.tools.base import ToolRegistry
from app.web_console.live_stream import _WebDBPersister
from tests.test_agent_loop import RecordRenderer


@pytest.mark.asyncio
async def test_controller_finishes_effects_once_restores_window_and_originals(tmp_path):
    db = DB(str(tmp_path / "controller.db"))
    await db.connect()
    try:
        dao = MessageDAO(db)
        session = await dao.get_or_create_session_uuid(321)
        user_id = await dao.add(321, "user", "Append exactly six lines. No deployment.")
        messages = [mark_source({"role": "user", "content": "Append exactly six lines. No deployment."},
                                kind="human", source_id=f"message:{user_id}", message_id=user_id)]
        owner = ContextOwner.controller(chat_id=321, session_uuid=session, conversation_uuid="window-test")
        store = WindowStore(db, owner)
        reg = ToolRegistry()
        target = tmp_path / "output.txt"
        executed = []

        async def write(args):
            n = int(args["line"])
            assert n not in executed
            executed.append(n)
            with target.open("a") as stream:
                stream.write(f"{n}\n")
            return f"raw-step-{n}: " + "long exact execution evidence. " * 200

        reg.add("Write", "Append one line", {"type": "object", "properties": {"line": {"type": "integer"}}}, write)

        class Backend:
            protocol = "chat"
            calls = 0

            async def stream(self, convo, **kwargs):
                self.calls += 1
                assert any("No deployment." in str(m.get("content")) for m in convo)
                if self.calls <= 6:
                    yield StreamEvent(kind="tool_call", tool_calls=[ToolCall(f"c{self.calls}", "Write", json.dumps({"line": self.calls}))])
                    yield StreamEvent(kind="usage", usage=Usage(output_tokens=10))
                    yield StreamEvent(kind="finish", finish_reason="tool_calls")
                else:
                    assert all("raw-step-1:" not in str(m.get("content")) for m in convo)
                    yield StreamEvent(kind="content", text="Six lines completed.")
                    yield StreamEvent(kind="usage", usage=Usage(output_tokens=10))
                    yield StreamEvent(kind="finish", finish_reason="stop")

        backend = Backend()
        runtime = WindowRuntime(store, WindowPolicy(context_window=50000, trigger_tokens=6000), backend=backend, model="test")
        persister = _WebDBPersister(dao, 321, session_uuid=session, conversation_uuid="window-test", protocol="chat", model="test", model_label="test")
        persister.window_runtime = runtime
        result = await Agent(backend, reg).run(messages, RecordRenderer(), model="test", persister=persister, window_runtime=runtime)
        assert result.text == "Six lines completed."
        assert target.read_text() == "1\n2\n3\n4\n5\n6\n"
        state = await store.load()
        assert state["window_version"] >= 3
        restored = await build_controller_history(dao, 321)
        assert all("raw-step-1:" not in str(m.get("content")) for m in restored)
        found = await store.index(query="raw-step-1:")
        assert found["events"]
        original = await store.event_payload(found["events"][0]["event_id"])
        assert len(original["payload"]["content"]) > 1000
        assert await SummaryDAO(db).latest(321) is None
        # A model switch discards only opaque replay, never the selection.
        assert await dao.load_controller_model_context(321, conversation_uuid="window-test", session_id=session, protocol="responses", model="new-model", model_label="new-model") is None
        assert await build_controller_history(dao, 321) == restored
        await dao.add(321, "user", "Now stop.")
        after = await build_controller_history(dao, 321)
        assert after[-1]["content"] == "Now stop."
        assert all("raw-step-1:" not in str(m.get("content")) for m in after)
    finally:
        await db.close()
