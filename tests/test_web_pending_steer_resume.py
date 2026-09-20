"""A failed Web run's queued input must precede the next user's instruction."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from app.agent import steering
from app.agent.runs import RunRegistry
from app.context.builder import build_controller_history
from app.context.window import source_of
from app.db.dao import MessageDAO
from app.llm.base import OpenBearLLMError
from app.llm.events import StreamEvent
from app.tools.base import ToolRegistry
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _cfg, web_env


async def test_failed_run_restores_pending_steer_before_new_input_without_changing_turn(web_env, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    class Backend(FakeStreamBackend):
        async def stream(self, messages, **kwargs):
            self.seen_convos.append(copy.deepcopy(messages))
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
                raise OpenBearLLMError("upstream request rejected", status=400, retryable=False)
            yield StreamEvent(kind="content", text=f"FOLLOWUP ANSWER {self.calls}")
            yield StreamEvent(kind="finish", finish_reason="stop")

    backend = Backend()
    server = web_env.server
    server.config = _cfg()
    server.llm_factory = FakeRunFactory(backend, context_window=128000)
    server.model_selection = SimpleNamespace(current="openai/gpt")
    server.tools = ToolRegistry()
    server.runs = RunRegistry()

    async def system():
        return "sys"

    monkeypatch.setattr(server, "_build_system_prompt_for_chat", system)
    row = await server._create_web_conversation(123, title="failed run pending steer", model="openai/gpt")
    chat = int(row["internal_chat_id"])
    conv_uuid = str(row["conversation_uuid"])
    live = server._live_for(row)
    steering.clear(chat)
    initial = "INITIAL request"
    old_input = "OLD instruction: perform the effect"
    latest_input = "LATEST instruction: cancel; do not perform the effect"

    async def send(text):
        result = await server._start_or_steer_web_conversation(row, text, [], live)
        return result, server.runs.task(chat)

    try:
        _, first = await send(initial)
        await asyncio.wait_for(entered.wait(), 2)
        old_root = live._agent_turn_uuid
        queued, _ = await send(old_input)
        assert queued["queued"] is True
        queued_item = steering.pending_items(chat)[0]
        source_op_id = f"msg:{queued_item['messageUuid']}"
        release.set()
        assert await asyncio.wait_for(first, 3) is False
        assert steering.has_pending(chat)

        accepted, second = await send(latest_input)
        assert accepted["queued"] is False
        new_root = live._agent_turn_uuid
        assert new_root != old_root
        assert await asyncio.wait_for(second, 3) is True
        assert backend.calls == 2
        assert not steering.has_pending(chat)
        user_contents = [m["content"] for m in backend.seen_convos[1] if m["role"] == "user"]
        assert [str(text).split("\n\n[⏰", 1)[0] for text in user_contents] == [initial, old_input, latest_input]

        operations = await server._web_operations(conv_uuid)
        answer = next(o for o in operations if o["opType"] == "assistant_message" and o["payload"].get("text") == "FOLLOWUP ANSWER 2")
        assert (answer["turnUuid"], answer["runRootTurnId"], answer["runId"]) == (new_root,) * 3
        stats = next(o for o in operations if o["opId"] == f"stats:{new_root}")
        assert (stats["turnUuid"], stats["runRootTurnId"], stats["runId"]) == (new_root,) * 3
        assert stats["payload"]["live"] is False
        source_op = next(o for o in operations if o["opId"] == source_op_id)
        assert source_op["turnUuid"] == source_op["runRootTurnId"] == old_root
        assert source_op["payload"]["queued"] is False
        assert source_op["payload"]["text"] == old_input

        dao = MessageDAO(web_env.db)
        restored = await build_controller_history(dao, chat)
        restored_old = next(m for m in restored if m.get("content") == old_input)
        assert source_of(restored_old)["turn_uuid"] == old_root
        assert source_of(restored_old)["run_root_turn_uuid"] == old_root
        cur = await web_env.db.conn.execute("SELECT id,role,content,turn_uuid,run_root_turn_uuid FROM messages WHERE chat_id=? ORDER BY id", (chat,))
        rows = [dict(r) for r in await cur.fetchall()]
        assert [r["content"] for r in rows if r["role"] == "user"] == [initial, old_input, latest_input]
        old_row = next(r for r in rows if r["content"] == old_input)
        assert old_row["turn_uuid"] == old_row["run_root_turn_uuid"] == old_root
        assert source_op["transcriptMessageIds"] == [old_row["id"]]

        # A later normal request must neither drain nor persist the old item twice.
        _, third = await send("NEXT ordinary input")
        assert await asyncio.wait_for(third, 3) is True
        assert sum(m.get("content") == old_input for m in backend.seen_convos[-1]) == 1
        cur = await web_env.db.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND content=?", (chat, old_input))
        assert (await cur.fetchone())[0] == 1
    finally:
        release.set()
        await server.runs.cancel_all_and_wait()
        steering.clear(chat)


def test_restored_item_drain_keeps_new_same_run_steering():
    chat = 728194
    steering.clear(chat)
    try:
        old = steering.enqueue(chat, "old original", source="web", turnUuid="old")
        new = steering.enqueue(chat, "new arrival during persistence", source="web", turnUuid="new")
        assert steering.drain_items(chat, item_ids={old["id"]}) == [old]
        assert steering.pending_items(chat) == [new]
        assert steering.drain_items(chat, item_ids={old["id"]}) == []
        assert steering.drain_items(chat) == [new]
    finally:
        steering.clear(chat)
