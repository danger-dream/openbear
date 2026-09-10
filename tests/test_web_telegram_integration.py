from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agent import steering
from app.agent.runs import RunRegistry
from app.db.dao import MessageDAO
from app.llm.events import StreamEvent
from app.tools.base import ToolRegistry
from app.web_telegram_replies import bind_message
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend
from tests.test_web_admin import web_env as _shared_web_env
from tests.test_web_task_telegram import NotificationBot
from tests.test_web_telegram_replies import Incoming

web_env = _shared_web_env


async def configure(server, monkeypatch, backend):
    server.runs = RunRegistry()
    server.llm_factory = FakeRunFactory(backend, context_window=128000)
    server.model_selection = SimpleNamespace(current="openai/gpt")
    server.tools = ToolRegistry()

    async def system():
        return "Use the original conversation history."

    monkeypatch.setattr(server, "_build_system_prompt_for_chat", system)


async def incoming(server, message):
    bridge = server.telegram_replies
    record = await bridge.match_reply(message)
    assert record is not None
    await bridge.handle_reply(message, record)
    assert await bridge.process_one()


async def test_tg_reply_uses_real_web_runtime_history_current_model_and_returns_each_short_answer(web_env, monkeypatch):
    server = web_env.server
    backend = FakeStreamBackend([
        [StreamEvent(kind="content", text="第一轮结论"), StreamEvent(kind="finish", finish_reason="stop")],
        [StreamEvent(kind="content", text="已继续原来的工作"), StreamEvent(kind="finish", finish_reason="stop")],
        [StreamEvent(kind="content", text="后续补充已完成"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    await configure(server, monkeypatch, backend)
    row = await server._create_web_conversation(123, title="原始任务", model="openai/gpt")
    chat_id, conv = int(row["internal_chat_id"]), str(row["conversation_uuid"])
    live = server._live_for(row)
    # No WebSocket/browser subscriber is opened throughout this test.
    await server._start_or_steer_web_conversation(row, "记住这个任务的上下文", [], live)
    await asyncio.wait_for(server.runs.task(chat_id), timeout=5)
    assert await server.web_task_telegram._claim_due() is None  # ordinary short Web run stays silent
    await bind_message(web_env.db, 123, 100, conv, live.current_turn_uuid)
    await web_env.db.conn.execute("UPDATE web_conversations SET model='openai/cheap' WHERE conversation_uuid=?", (conv,))
    await web_env.db.conn.commit()
    models = []
    original_backend_for = server.llm_factory.backend_for

    def backend_for(label):
        models.append(label)
        return original_backend_for(label)

    monkeypatch.setattr(server.llm_factory, "backend_for", backend_for)
    bot = NotificationBot()
    server.web_task_telegram.bot = bot
    server.telegram_replies.bot = bot
    for message, expected in [(Incoming(text="按刚才结论继续"), "已继续原来的工作"), (Incoming(501, 901, "再做补充"), "后续补充已完成")]:
        await incoming(server, message)
        assert not await server.telegram_replies.deliver_receipt()
        await asyncio.wait_for(server.runs.task(chat_id), timeout=5)
        delivery = await server.web_task_telegram._claim_due()
        assert delivery is not None and delivery.event_type == "result" and delivery.payload["combined"]
        await server.web_task_telegram._deliver(delivery)
        assert expected in bot.sent[-1]["body"]
        assert await server.web_task_telegram._claim_due() is None
    assert len(bot.sent) == 2
    assert models and all(model == "openai/cheap" for model in models)
    second_prompt = str(backend.seen_convos[1])
    assert "记住这个任务的上下文" in second_prompt and "第一轮结论" in second_prompt and "按刚才结论继续" in second_prompt
    ops = await server._web_operations(conv)
    tg_users = [op for op in ops if op["opType"] == "user_message" and op["source"] == "telegram"]
    assert [op["payload"]["text"] for op in tg_users] == ["按刚才结论继续", "再做补充"]
    assert (await (await web_env.db.conn.execute("SELECT COUNT(*) AS n FROM web_conversations")).fetchone())["n"] == 1
    assert {int(r["chat_id"]) for r in await (await web_env.db.conn.execute("SELECT DISTINCT chat_id FROM messages")).fetchall()} == {chat_id}


async def test_tg_replies_during_run_wake_same_controller_and_merge_at_safe_boundary(web_env, monkeypatch):
    ready, release = asyncio.Event(), asyncio.Event()

    class GateBackend(FakeStreamBackend):
        async def stream(self, *args, **kwargs):
            if self.calls == 0:
                ready.set()
                await release.wait()
            async for event in super().stream(*args, **kwargs):
                yield event

    backend = GateBackend([
        [StreamEvent(kind="content", text="阶段结论"), StreamEvent(kind="finish", finish_reason="stop")],
        [StreamEvent(kind="content", text="已按补充完成"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    server = web_env.server
    await configure(server, monkeypatch, backend)
    row = await server._create_web_conversation(123, title="运行中的任务", model="openai/gpt")
    chat_id, conv = int(row["internal_chat_id"]), str(row["conversation_uuid"])
    live = server._live_for(row)
    await server._start_or_steer_web_conversation(row, "开始", [], live)
    task = server.runs.task(chat_id)
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        root = live.current_turn_uuid
        await bind_message(web_env.db, 123, 100, conv, root)
        await web_env.db.conn.commit()
        await incoming(server, Incoming(text="新增要求一"))
        await incoming(server, Incoming(501, 100, "新增要求二"))
        assert not await server.telegram_replies.deliver_receipt()
        assert server.runs.task(chat_id) is task
        pending = steering.pending_items(chat_id)
        assert [item["source"] for item in pending] == ["telegram", "telegram"]
        assert {item["rootTurnUuid"] for item in pending} == {root}
        release.set()
        await asyncio.wait_for(task, timeout=5)
        assert len(backend.seen_convos) == 2
        injected = [m for m in backend.seen_convos[1] if m.get("role") == "user" and "新增要求一" in str(m.get("content"))]
        assert len(injected) == 1 and "新增要求二" in injected[0]["content"]
        operations = await server._web_operations(conv)
        assert any(op["opType"] == "user_message" and op["source"] == "telegram" and "新增要求一" in op["payload"]["text"] for op in operations)
        delivery = await server.web_task_telegram._claim_due()
        assert delivery.event_type == "result" and delivery.root_turn_uuid == root
        assert "已按补充完成" in delivery.payload["text"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        steering.clear(chat_id)


async def test_tg_ingress_rechecks_archive_under_shared_operation_lock(web_env):
    server = web_env.server
    row = await server._create_web_conversation(123, title="已归档")
    await web_env.db.conn.execute("UPDATE web_conversations SET archived_at=1 WHERE conversation_uuid=?", (row["conversation_uuid"],))
    await web_env.db.conn.commit()
    result = await server._submit_telegram_reply(row, "继续", submission_id=1)
    assert result == {"ok": False, "error": "conversation_unavailable"}
    assert await server._web_operations(row["conversation_uuid"]) == []


async def test_tg_ingress_respects_manual_compaction_lock(web_env):
    server = web_env.server
    row = await server._create_web_conversation(123, title="正在压缩")
    async with server.operation_locks.chat(int(row["internal_chat_id"]), "web_manual_compact"):
        result = await server._submit_telegram_reply(row, "继续", submission_id=1)
    assert result == {"ok": False, "error": "busy"}
