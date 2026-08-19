from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Config
from app.db.engine import DB
from app.web_task_telegram import Delivery, WebTaskTelegramNotifier, markdown_to_telegram_blocks


def _config(*, events=None, include_result=True, owner=123) -> Config:
    return Config.model_validate({
        "telegram": {"botToken": "test", "whitelistIds": [owner]},
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://example.test/v1",
                    "apiKey": "key",
                    "protocol": "chat",
                    "models": [{"id": "gpt", "name": "GPT"}],
                },
            },
            "primary": "openai/gpt",
        },
        "memory": {"provider": "builtin"},
        "web": {
            "taskNotifications": {
                "enabled": True,
                "includeResult": include_result,
                "thresholdMinutes": 3,
                "events": events if events is not None else ["task_completed", "task_failed"],
            },
        },
    })


async def _db(tmp_path) -> DB:
    db = DB(str(tmp_path / "notify.db"))
    await db.connect()
    now = 1000
    await db.conn.execute(
        """
        INSERT INTO web_conversations (
          conversation_uuid, owner_chat_id, internal_chat_id, title, model,
          status, current_status, created_at, updated_at
        ) VALUES ('conv-1', 123, -1, '长任务', 'openai/gpt', 'running', '运行中', ?, ?)
        """,
        (now, now),
    )
    await db.conn.commit()
    return db


async def _register(notifier: WebTaskTelegramNotifier, started_at=1000) -> None:
    await notifier.register(
        {"type": "accepted", "runUuid": "turn-1", "turnUuid": "turn-1", "conversationUuid": "conv-1", "ts": started_at * 1000},
        owner_chat_id=123,
        internal_chat_id=-1,
        title="长任务",
        model="openai/gpt",
    )


@pytest.mark.asyncio
async def test_accepted_without_timestamp_uses_current_time(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr("app.web_task_telegram._now", lambda: 5000)
    notifier = WebTaskTelegramNotifier(_config(events=[], include_result=False), db, SimpleNamespace())
    try:
        await notifier.register(
            {"type": "accepted", "runUuid": "turn-1", "conversationUuid": "conv-1"},
            owner_chat_id=123,
            internal_chat_id=-1,
            title="长任务",
            model="openai/gpt",
        )
        run = await notifier._run("turn-1")
        assert run and run["started_at"] == 5000 and run["threshold_at"] == 5180
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_short_task_cancels_buffered_notifications(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    notifier = WebTaskTelegramNotifier(_config(events=["task_started", "task_completed"]), db, SimpleNamespace())
    try:
        await _register(notifier)
        clock["now"] = 1100
        await notifier.finish("turn-1", "completed")
        run = await notifier._run("turn-1")
        assert run and run["status"] == "short"
        cur = await db.conn.execute("SELECT state FROM web_tg_notification_outbox WHERE root_turn_uuid='turn-1'")
        assert [row["state"] for row in await cur.fetchall()] == ["cancelled"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_long_task_queues_completion_before_result(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    notifier = WebTaskTelegramNotifier(_config(events=[]), db, SimpleNamespace())
    try:
        await _register(notifier)
        await notifier.observe(
            {"type": "final", "runUuid": "turn-1", "turnUuid": "turn-1", "text": "## 最终结论\n完成。"},
            owner_chat_id=123,
            internal_chat_id=-1,
        )
        clock["now"] = 1200
        await notifier.finish("turn-1", "completed")
        cur = await db.conn.execute(
            "SELECT event_type, state FROM web_tg_notification_outbox WHERE root_turn_uuid='turn-1' ORDER BY id"
        )
        assert [(row["event_type"], row["state"]) for row in await cur.fetchall()] == [
            ("result_ready", "pending"),
            ("result", "pending"),
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_running_task_keeps_accepted_time_config_snapshot(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    notifier = WebTaskTelegramNotifier(_config(events=["task_completed"], include_result=False), db, SimpleNamespace())
    try:
        await _register(notifier)
        notifier.apply_config(_config(events=[], include_result=True))
        clock["now"] = 1200
        await notifier.finish("turn-1", "completed")
        cur = await db.conn.execute(
            "SELECT event_type FROM web_tg_notification_outbox WHERE root_turn_uuid='turn-1' ORDER BY id"
        )
        assert [row["event_type"] for row in await cur.fetchall()] == ["task_completed"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_start_recovers_stale_running_task_as_interrupted(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    notifier = WebTaskTelegramNotifier(_config(events=["task_interrupted"], include_result=False), db, SimpleNamespace())
    wait_forever = asyncio.Event()

    async def idle_worker():
        await wait_forever.wait()

    monkeypatch.setattr(notifier, "_worker_loop", idle_worker)
    try:
        await _register(notifier)
        clock["now"] = 1200
        await notifier.start()
        run = await notifier._run("turn-1")
        assert run and run["status"] == "interrupted"
        cur = await db.conn.execute(
            "SELECT event_type, state FROM web_tg_notification_outbox WHERE root_turn_uuid='turn-1' ORDER BY id"
        )
        assert [(row["event_type"], row["state"]) for row in await cur.fetchall()] == [
            ("task_interrupted", "pending")
        ]
    finally:
        await notifier.stop()
        await db.close()


@pytest.mark.asyncio
async def test_selected_agent_and_retry_events_are_deduplicated_and_delayed(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    notifier = WebTaskTelegramNotifier(_config(events=["agent_started", "agent_finished", "retrying"], include_result=False), db, SimpleNamespace())
    try:
        await _register(notifier)
        progress = {
            "type": "tool_progress",
            "runUuid": "turn-1",
            "name": "Agent",
            "payload": {"toolName": "Agent", "status": "running", "task": {"taskUuid": "agent-1", "status": "running", "title": "审查代码"}},
        }
        await notifier.observe(progress, owner_chat_id=123, internal_chat_id=-1)
        await notifier.observe(progress, owner_chat_id=123, internal_chat_id=-1)
        await notifier.observe(
            {"type": "retry_wait", "runUuid": "turn-1", "retry": {"active": True, "attempt": 2, "delaySeconds": 8}},
            owner_chat_id=123,
            internal_chat_id=-1,
        )
        await notifier.observe(
            {"type": "task_notification", "runUuid": "turn-1", "taskUuid": "agent-1", "status": "completed", "title": "审查代码"},
            owner_chat_id=123,
            internal_chat_id=-1,
        )
        cur = await db.conn.execute(
            "SELECT event_type, deliver_after FROM web_tg_notification_outbox WHERE root_turn_uuid='turn-1' ORDER BY id"
        )
        rows = await cur.fetchall()
        assert [row["event_type"] for row in rows] == ["agent_started", "retrying", "agent_finished"]
        assert {row["deliver_after"] for row in rows} == {1180}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_threshold_coalesces_buffered_events_into_one_summary(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    notifier = WebTaskTelegramNotifier(_config(events=["task_started", "agent_started", "retrying"], include_result=False), db, SimpleNamespace())
    try:
        await _register(notifier)
        await notifier.observe(
            {"type": "tool_progress", "runUuid": "turn-1", "name": "Agent", "payload": {"status": "running", "task": {"taskUuid": "agent-1", "status": "running"}}},
            owner_chat_id=123,
            internal_chat_id=-1,
        )
        await notifier.observe(
            {"type": "retry_wait", "runUuid": "turn-1", "retry": {"active": True, "attempt": 1}},
            owner_chat_id=123,
            internal_chat_id=-1,
        )
        clock["now"] = 1181
        delivery = await notifier._claim_due()
        assert delivery and delivery.event_type == "threshold_summary"
        assert [item["eventType"] for item in delivery.payload["events"]] == [
            "task_started", "agent_started", "retrying",
        ]
        cur = await db.conn.execute(
            "SELECT state FROM web_tg_notification_outbox WHERE root_turn_uuid='turn-1' ORDER BY id"
        )
        assert [row["state"] for row in await cur.fetchall()] == ["processing", "cancelled", "cancelled"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivery_order_blocks_result_behind_retrying_notice(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    notifier = WebTaskTelegramNotifier(_config(events=[]), db, SimpleNamespace())
    try:
        await _register(notifier)
        await notifier._queue_event("turn-1", "result_ready", "notice", {}, deliver_after=1000)
        await notifier._queue_event("turn-1", "result", "result", {"text": "ok"}, deliver_after=1000)
        first = await notifier._claim_due()
        assert first and first.event_type == "result_ready"
        await notifier._retry(first, "temporary", delay=30)
        assert await notifier._claim_due() is None
        clock["now"] = 1031
        first_again = await notifier._claim_due()
        assert first_again and first_again.event_type == "result_ready"
        await notifier._mark(first_again, "failed", "permanent")
        second = await notifier._claim_due()
        assert second and second.event_type == "result"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_result_uses_rich_progressive_edits(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    notifier = WebTaskTelegramNotifier(_config(), db, SimpleNamespace())
    sent: list[str] = []
    edited: list[str] = []

    async def fake_send(_bot, _owner, body, **_kwargs):
        sent.append(body)
        return SimpleNamespace(message_id=900 + len(sent))

    async def fake_edit(_bot, _owner, _message_id, body, **_kwargs):
        edited.append(body)
        return True

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.web_task_telegram.send_rich", fake_send)
    monkeypatch.setattr("app.web_task_telegram.edit_rich", fake_edit)
    monkeypatch.setattr("app.web_task_telegram.asyncio.sleep", no_sleep)
    try:
        text = "# 结论\n\n" + ("这是安全的最终回答。" * 260)
        delivery = Delivery(id=1, root_turn_uuid="turn-1", event_type="result", payload={}, attempts=1, message_ids=())
        ids = await notifier._stream_result(delivery, 123, text)
        assert ids == [901]
        assert sent and "<b>📄 最终回答</b>" in sent[0]
        assert edited and "这是安全的最终回答" in edited[-1]
        assert len(edited[-1]) > len(sent[0])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_result_retry_reuses_checkpointed_telegram_message(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    notifier = WebTaskTelegramNotifier(_config(), db, SimpleNamespace())
    edits = []

    async def must_not_send(*_args, **_kwargs):
        raise AssertionError("checkpointed page must be edited instead of sent again")

    async def fake_edit(_bot, _owner, message_id, body, **_kwargs):
        edits.append((message_id, body))
        return True

    monkeypatch.setattr("app.web_task_telegram.send_rich", must_not_send)
    monkeypatch.setattr("app.web_task_telegram.edit_rich", fake_edit)
    try:
        delivery = Delivery(id=1, root_turn_uuid="turn-1", event_type="result", payload={}, attempts=2, message_ids=(901,))
        ids = await notifier._stream_result(delivery, 123, "# 结论\n\n已完成。")
        assert ids == [901]
        assert edits and edits[0][0] == 901
        assert "已完成" in edits[0][1]
    finally:
        await db.close()


def test_markdown_converter_escapes_raw_html_and_unsafe_links():
    rendered = "\n".join(markdown_to_telegram_blocks(
        "# 标题\n<script>alert(1)</script>\n[安全](https://example.com?a=1&b=2) [危险](javascript:alert(1))\n`<token>`"
    ))
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert 'href="https://example.com?a=1&amp;b=2"' in rendered
    assert "javascript:" not in rendered
    assert "<code>&lt;token&gt;</code>" in rendered
