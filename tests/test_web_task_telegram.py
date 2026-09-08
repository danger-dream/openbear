from __future__ import annotations

import asyncio
import json
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
@pytest.mark.parametrize("events", [[], ["task_completed"]])
async def test_long_task_combines_completion_and_result(tmp_path, monkeypatch, events):
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    notifier = WebTaskTelegramNotifier(_config(events=events), db, SimpleNamespace())
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
            ("result", "pending"),
        ]
        delivery = await notifier._claim_due()
        assert delivery.payload["combined"] is True
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


def _agent_tool_event(name, kind, status="running", task_uuid="agent-1"):
    payload = {
        "status": status,
        "task": {"taskUuid": task_uuid, "status": status, "title": "审查代码"},
    }
    event = {"type": kind, "runUuid": "turn-1", "name": name}
    if kind == "tool_result":
        event["result"] = json.dumps(payload)
    else:
        event["payload"] = {"toolName": name, **payload}
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Agent", "AgentContinue"])
@pytest.mark.parametrize("kind", ["tool_progress", "tool_result"])
async def test_agent_creation_tools_register_start_once_with_original_threshold(tmp_path, monkeypatch, name, kind):
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    notifier = WebTaskTelegramNotifier(_config(events=["agent_started"], include_result=False), db, SimpleNamespace())
    try:
        await _register(notifier)
        event = _agent_tool_event(name, kind)
        await notifier.observe(event, owner_chat_id=123, internal_chat_id=-1)
        # Replayed progress/result after the threshold must not generate a new
        # start or move the original event's time to a later control interaction.
        clock["now"] = 1200
        await notifier.observe(event, owner_chat_id=123, internal_chat_id=-1)
        other_kind = "tool_result" if kind == "tool_progress" else "tool_progress"
        await notifier.observe(_agent_tool_event(name, other_kind), owner_chat_id=123, internal_chat_id=-1)
        cur = await db.conn.execute(
            "SELECT event_type, created_at, deliver_after, payload_json FROM web_tg_notification_outbox ORDER BY id"
        )
        rows = await cur.fetchall()
        assert [(row["event_type"], row["created_at"], row["deliver_after"]) for row in rows] == [
            ("agent_started", 1000, 1180),
        ]
        assert json.loads(rows[0]["payload_json"])["taskUuid"] == "agent-1"
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool_progress", "tool_result"])
@pytest.mark.parametrize("status", ["running", "completed"])
async def test_agent_message_snapshot_never_creates_lifecycle_notification(tmp_path, monkeypatch, kind, status):
    db = await _db(tmp_path)
    monkeypatch.setattr("app.web_task_telegram._now", lambda: 2067)
    notifier = WebTaskTelegramNotifier(
        _config(events=["agent_started", "agent_finished"], include_result=False), db, SimpleNamespace()
    )
    try:
        await _register(notifier)
        # No prior start in the outbox: deduplication must not mask an incorrect
        # AgentMessage classification (the observed production failure).
        event = _agent_tool_event("AgentMessage", kind, status)
        await notifier.observe(event, owner_chat_id=123, internal_chat_id=-1)
        await notifier.observe(event, owner_chat_id=123, internal_chat_id=-1)
        cur = await db.conn.execute("SELECT event_type FROM web_tg_notification_outbox")
        assert await cur.fetchall() == []
        # Ignoring a control receipt must not suppress a genuine terminal event.
        await notifier.observe(
            {"type": "task_notification", "runUuid": "turn-1", "taskUuid": "agent-1", "status": "completed"},
            owner_chat_id=123, internal_chat_id=-1,
        )
        cur = await db.conn.execute("SELECT event_type FROM web_tg_notification_outbox")
        assert [row["event_type"] for row in await cur.fetchall()] == ["agent_finished"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_continuation_new_task_has_its_own_start_notification(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr("app.web_task_telegram._now", lambda: 1000)
    notifier = WebTaskTelegramNotifier(_config(events=["agent_started"], include_result=False), db, SimpleNamespace())
    try:
        await _register(notifier)
        for name, task_uuid in [("Agent", "round-1"), ("AgentContinue", "round-2")]:
            await notifier.observe(
                _agent_tool_event(name, "tool_result", task_uuid=task_uuid), owner_chat_id=123, internal_chat_id=-1,
            )
        cur = await db.conn.execute("SELECT payload_json FROM web_tg_notification_outbox ORDER BY id")
        assert [json.loads(row["payload_json"])["taskUuid"] for row in await cur.fetchall()] == ["round-1", "round-2"]
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
async def test_result_uses_one_full_rich_message(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    bot = NotificationBot()
    notifier = WebTaskTelegramNotifier(_config(), db, bot)
    try:
        await _register(notifier)
        text = "# 结论\n\n" + ("这是安全的最终回答。" * 260)
        delivery = Delivery(id=1, root_turn_uuid="turn-1", event_type="result", payload={"combined": True}, attempts=1, message_ids=())
        ids = await notifier._stream_result(delivery, 123, text)
        assert ids == [901] and len(bot.sent) == 1
        assert "任务已完成" in bot.sent[0]["body"]
        assert "<b>📄 最终回答</b>" in bot.sent[0]["body"]
        assert bot.sent[0]["body"].count("这是安全的最终回答。") == 260
        assert bot.sent[0]["reply_markup"].inline_keyboard[0][0].callback_data == "wt:reply"
        bound = await (await db.conn.execute("SELECT * FROM web_tg_messages WHERE telegram_message_id=901")).fetchone()
        assert bound["conversation_uuid"] == "conv-1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_result_retry_reuses_checkpointed_telegram_message(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    bot = NotificationBot()
    notifier = WebTaskTelegramNotifier(_config(), db, bot)
    try:
        delivery = Delivery(id=1, root_turn_uuid="turn-1", event_type="result", payload={}, attempts=2, message_ids=(901,))
        ids = await notifier._stream_result(delivery, 123, "# 结论\n\n已完成。")
        assert ids == [901] and bot.sent == []
        assert bot.edited and bot.edited[0][0] == 901
        assert "已完成" in bot.edited[0][1]
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


class NotificationBot:
    def __init__(self, *, legacy=False):
        self.legacy = legacy
        self.sent = []
        self.edited = []
        self.fail_at = -1
        self.edit_fail_id = 0

    async def edit_message_text(self, text=None, *, chat_id, message_id, rich_message=None, **kwargs):
        if self.legacy and rich_message is not None:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=SimpleNamespace(), message="rich messages unsupported")
        if message_id == self.edit_fail_id:
            self.edit_fail_id = 0
            raise RuntimeError("temporary edit failure")
        self.edited.append((message_id, rich_message.html if rich_message is not None else text))
        return True

    async def send_rich_message(self, *, chat_id, rich_message, **kwargs):
        if self.legacy:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=SimpleNamespace(), message="rich messages unsupported")
        return self._record(chat_id, rich_message.html, **kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        return self._record(chat_id, text, **kwargs)

    def _record(self, chat_id, body, **kwargs):
        if self.fail_at == len(self.sent):
            self.fail_at = -1
            raise RuntimeError("temporary network failure")
        self.sent.append({"body": body, "chat_id": chat_id, **kwargs})
        return SimpleNamespace(message_id=900 + len(self.sent))


@pytest.mark.parametrize("status,expected", [("completed", "result"), ("failed", "task_failed"), ("interrupted", "task_interrupted")])
async def test_direct_tg_short_turn_always_returns_terminal_even_when_web_notifications_disabled(tmp_path, monkeypatch, status, expected):
    db = await _db(tmp_path)
    cfg = _config(events=[], include_result=False)
    cfg.web.task_notifications.enabled = False
    notifier = WebTaskTelegramNotifier(cfg, db, NotificationBot())
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    try:
        row = dict(await (await db.conn.execute("SELECT * FROM web_conversations")).fetchone())
        await notifier.enable_direct_reply(row, "tg-next")
        await notifier.observe({"type": "accepted", "runUuid": "tg-next", "source": "telegram", "conversationUuid": "conv-1"}, owner_chat_id=123, internal_chat_id=-1)
        await notifier.observe({"type": "final", "runUuid": "tg-next", "text": "很快完成"}, owner_chat_id=123, internal_chat_id=-1)
        clock["now"] = 1001
        await notifier.finish("tg-next", status)
        run = await notifier._run("tg-next")
        assert run["status"] == status and run["threshold_at"] == 1000
        delivery = await notifier._claim_due()
        assert delivery and delivery.event_type == expected
        await notifier._deliver(delivery)
        assert len(notifier.bot.sent) == 1
        assert await notifier._claim_due() is None
    finally:
        await db.close()


async def test_tg_steering_promotes_only_current_web_root_to_direct_response(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr("app.web_task_telegram._now", lambda: 1001)
    notifier = WebTaskTelegramNotifier(_config(events=[], include_result=False), db, NotificationBot())
    try:
        await _register(notifier)
        row = dict(await (await db.conn.execute("SELECT * FROM web_conversations")).fetchone())
        await notifier.enable_direct_reply(row, "turn-1")
        await notifier.observe({"type": "final", "runUuid": "turn-1", "text": "已处理补充"}, owner_chat_id=123, internal_chat_id=-1)
        await notifier.finish("turn-1", "completed")
        assert (await notifier._claim_due()).event_type == "result"
        assert notifier.config.web.task_notifications.include_result is False
        assert notifier.config.web.task_notifications.threshold_minutes == 3
    finally:
        await db.close()


@pytest.mark.parametrize("legacy", [False, True])
async def test_all_result_pages_bound_and_partial_delivery_retries_without_duplicate_pages(tmp_path, monkeypatch, legacy):
    import json
    db = await _db(tmp_path)
    clock = {"now": 1000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    bot = NotificationBot(legacy=legacy)
    notifier = WebTaskTelegramNotifier(_config(), db, bot)
    try:
        await _register(notifier)
        text = "# 结果\n\n" + "原始回答内容。" * 6500
        await notifier.observe({"type": "final", "runUuid": "turn-1", "text": text}, owner_chat_id=123, internal_chat_id=-1)
        clock["now"] = 1200
        await notifier.finish("turn-1", "completed")
        bot.fail_at = 1
        await notifier._deliver(await notifier._claim_due())
        assert len(bot.sent) == 1
        row = await (await db.conn.execute("SELECT * FROM web_tg_notification_outbox")).fetchone()
        assert row["state"] == "pending" and json.loads(row["telegram_message_ids_json"]) == [901]
        # Simulate a fresh notifier after restart; all state comes from SQLite.
        clock["now"] = 1250
        recovered = WebTaskTelegramNotifier(_config(), db, bot)
        await recovered._deliver(await recovered._claim_due())
        row = await (await db.conn.execute("SELECT * FROM web_tg_notification_outbox")).fetchone()
        assert row["state"] == "sent"
        ids = json.loads(row["telegram_message_ids_json"])
        assert ids == list(range(901, 901 + len(bot.sent)))
        assert len(ids) > 1
        bindings = await (await db.conn.execute("SELECT * FROM web_tg_messages ORDER BY telegram_message_id")).fetchall()
        assert [b["telegram_message_id"] for b in bindings] == ids
        assert {b["conversation_uuid"] for b in bindings} == {"conv-1"}
        assert all(item["reply_markup"].inline_keyboard[0][0].callback_data == "wt:reply" for item in bot.sent)
        from app.web_telegram_replies import WebTelegramReplies
        bridge = WebTelegramReplies(_config(), db, bot, None)
        assert all([await bridge.message_record(123, mid) for mid in ids])
    finally:
        await db.close()


async def test_pre_upgrade_message_id_still_matches_original_conversation(tmp_path):
    db = await _db(tmp_path)
    notifier = WebTaskTelegramNotifier(_config(), db, NotificationBot())
    try:
        await _register(notifier)
        await notifier._queue_event("turn-1", "task_completed", "old", {}, deliver_after=0)
        await db.conn.execute("UPDATE web_tg_notification_outbox SET state='sent',telegram_message_ids_json='[432,433]'")
        await db.conn.commit()
        from app.web_telegram_replies import WebTelegramReplies
        bridge = WebTelegramReplies(_config(), db, notifier.bot, None)
        assert (await bridge.message_record(123, 432))["conversation_uuid"] == "conv-1"
        assert (await bridge.message_record(123, 433))["conversation_uuid"] == "conv-1"
        assert await bridge.message_record(999, 432) is None
    finally:
        await db.close()


@pytest.mark.parametrize("legacy", [False, True])
async def test_upgrade_imports_all_old_checkpoints_before_retrying_an_edit(tmp_path, monkeypatch, legacy):
    import json
    db = await _db(tmp_path)
    clock = {"now": 2000}
    monkeypatch.setattr("app.web_task_telegram._now", lambda: clock["now"])
    bot = NotificationBot(legacy=legacy)
    notifier = WebTaskTelegramNotifier(_config(), db, bot)
    try:
        await _register(notifier)
        text = "完整正文。" * 10000
        await notifier._queue_event("turn-1", "result", "old-result", {"text": text}, deliver_after=0)
        await db.conn.execute("UPDATE web_tg_notification_outbox SET telegram_message_ids_json='[801,802]'")
        await db.conn.commit()
        bot.edit_fail_id = 802
        await notifier._deliver(await notifier._claim_due())
        saved = await (await db.conn.execute("SELECT * FROM web_tg_notification_outbox")).fetchone()
        assert saved["state"] == "pending"
        assert {801, 802}.issubset(json.loads(saved["telegram_message_ids_json"]))
        clock["now"] = 2100
        await notifier._deliver(await notifier._claim_due())
        saved = await (await db.conn.execute("SELECT * FROM web_tg_notification_outbox")).fetchone()
        assert saved["state"] == "sent"
        ids = json.loads(saved["telegram_message_ids_json"])
        assert len(ids) == len(set(ids)) == 2 + len(bot.sent)
        assert [mid for mid, _ in bot.edited] == [801, 802]
        bound_ids = {r["telegram_message_id"] for r in await (await db.conn.execute("SELECT * FROM web_tg_messages")).fetchall()}
        assert bound_ids == set(ids)
    finally:
        await db.close()


async def test_existing_database_gets_additive_reply_tables_without_losing_conversations(tmp_path):
    db = await _db(tmp_path)
    await db.conn.execute("DROP TABLE web_tg_messages")
    await db.conn.execute("DROP TABLE web_tg_reply_inbox")
    await db.conn.commit()
    await db.close()
    upgraded = DB(str(tmp_path / "notify.db"))
    await upgraded.connect()
    try:
        assert (await (await upgraded.conn.execute("SELECT title FROM web_conversations WHERE conversation_uuid='conv-1'")).fetchone())["title"] == "长任务"
        assert await (await upgraded.conn.execute("SELECT * FROM web_tg_reply_inbox")).fetchall() == []
        assert await (await upgraded.conn.execute("SELECT * FROM web_tg_messages")).fetchall() == []
    finally:
        await upgraded.close()
