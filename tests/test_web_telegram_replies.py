from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.bot.task_replies import RegisteredTaskReply, on_task_reply
from app.config import Config
from app.db.engine import DB
from app.main import _drain_startup_backlog
from app.web_telegram_replies import (
    REPLY_CALLBACK,
    WebTelegramReplies,
    bind_message,
    reply_keyboard,
)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.updates = []
        self.offsets = []

    async def send_rich_message(self, *, chat_id, rich_message, **kwargs):
        if self.fail:
            raise RuntimeError("temporary network error")
        msg = SimpleNamespace(message_id=1000 + len(self.sent), chat=SimpleNamespace(id=chat_id, type="private"))
        self.sent.append({"message": msg, "html": rich_message.html, **kwargs})
        return msg

    async def get_updates(self, **kwargs):
        self.offsets.append(kwargs.get("offset"))
        return self.updates if len(self.offsets) == 1 else []


class Incoming:
    def __init__(self, message_id=500, reply_to=100, text="继续实施", *, owner=123, sender=123, kind="private"):
        self.message_id, self.text, self.caption = message_id, text, None
        self.reply_to_message = SimpleNamespace(message_id=reply_to) if reply_to else None
        self.chat = SimpleNamespace(id=owner, type=kind)
        self.from_user = SimpleNamespace(id=sender)
        self.answers = []

    async def answer(self, text):
        self.answers.append(text)


class Query:
    def __init__(self, message_id=100, owner=123, sender=123):
        self.data = REPLY_CALLBACK
        self.message = SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=owner, type="private"))
        self.from_user = SimpleNamespace(id=sender)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


@pytest.fixture
async def env(tmp_path):
    config = Config.model_validate({
        "telegram": {"botToken": "test", "whitelistIds": [123]},
        "models": {"primary": "p/m", "providers": {"p": {"baseUrl": "http://example.test", "apiKey": "test", "protocol": "chat", "models": [{"id": "m"}]}}},
        "memory": {"provider": "builtin"},
        "web": {"customUrl": "https://console.example.test"},
    })
    db = DB(str(tmp_path / "replies.db"))
    await db.connect()
    for index in (1, 2):
        await db.conn.execute(
            "INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title,created_at) VALUES (?,123,?,?,?)",
            (f"conv-{index}", -index, f"会话{index}", int(time.time())),
        )
        await bind_message(db, 123, 100 * index, f"conv-{index}", f"old-{index}")
    await db.conn.commit()
    submitted = []

    async def submit(row, text, **kwargs):
        submitted.append((row, text, kwargs))
        return {"ok": True, "queued": False, "rootTurnUuid": "next-turn"}

    bot = FakeBot()
    bridge = WebTelegramReplies(config, db, bot, submit)
    try:
        yield SimpleNamespace(config=config, db=db, bot=bot, bridge=bridge, submitted=submitted, submit=submit)
    finally:
        await bridge.stop()
        await db.close()


async def inbox(env):
    return [dict(row) for row in await (await env.db.conn.execute("SELECT * FROM web_tg_reply_inbox ORDER BY id")).fetchall()]


async def receive(env, message):
    record = await env.bridge.match_reply(message)
    assert record
    await env.bridge.handle_reply(message, record)


async def test_reply_targets_exact_conversation_and_redelivery_does_not_execute_twice(env):
    first, second = Incoming(), Incoming(501, 200, "接着第二个会话")
    await asyncio.gather(receive(env, first), receive(env, first), receive(env, second))
    await asyncio.gather(env.bridge.process_one(), env.bridge.process_one())
    while await env.bridge.process_one():
        pass
    assert len(env.submitted) == 2
    assert {(row["conversation_uuid"], text) for row, text, _ in env.submitted} == {("conv-1", "继续实施"), ("conv-2", "接着第二个会话")}
    restarted = WebTelegramReplies(env.config, env.db, env.bot, env.submit)
    await restarted.handle_reply(first, await restarted.match_reply(first))
    assert not await restarted.process_one()
    assert len(env.submitted) == 2
    assert {r["state"] for r in await inbox(env)} == {"submitted"}


async def test_button_only_opens_bound_force_reply_prompt(env):
    query = Query()
    await env.bridge.handle_callback(query)
    assert query.answers == [(None, {"show_alert": False})]
    assert env.submitted == [] and await inbox(env) == []
    sent = env.bot.sent[-1]
    markup = sent["reply_markup"]
    assert markup.model_dump()["force_reply"] is True
    assert markup.inline_keyboard[0][0].url == "https://console.example.test/chat?id=conv-1"
    assert "发送文字后才会开始执行" in sent["html"]
    restarted = WebTelegramReplies(env.config, env.db, env.bot, env.submit)
    reply = Incoming(reply_to=sent["message"].message_id)
    record = await restarted.match_reply(reply)
    assert record["role"] == "prompt" and record["conversation_uuid"] == "conv-1"
    await restarted.handle_reply(reply, record)
    await restarted.process_one()
    assert len(env.submitted) == 1


@pytest.mark.parametrize("message", [Incoming(reply_to=None), Incoming(reply_to=999), Incoming(text="/restart"), Incoming(owner=999), Incoming(sender=999), Incoming(kind="group")])
async def test_unregistered_commands_or_wrong_identity_are_not_agent_input(env, message):
    assert await env.bridge.match_reply(message) is None
    assert await RegisteredTaskReply()(message, SimpleNamespace(web_admin=SimpleNamespace(telegram_replies=env.bridge))) is False
    assert not await env.bridge.process_one()


@pytest.mark.parametrize("mutation,fragment", [
    ("UPDATE web_conversations SET archived_at=1 WHERE conversation_uuid='conv-1'", "归档"),
    ("DELETE FROM web_conversations WHERE conversation_uuid='conv-1'", "删除"),
    ("UPDATE web_conversations SET owner_chat_id=999 WHERE conversation_uuid='conv-1'", "不可访问"),
    ("UPDATE web_tg_messages SET created_at=1", "90 天"),
])
async def test_unavailable_old_target_is_explicitly_rejected_without_new_chat(env, mutation, fragment):
    await env.db.conn.execute(mutation)
    await env.db.conn.commit()
    await receive(env, Incoming())
    await env.bridge.process_one()
    assert env.submitted == []
    assert (await inbox(env))[0]["state"] == "rejected"
    assert fragment in (await inbox(env))[0]["response_text"]


async def test_revoked_owner_and_disabled_web_do_not_execute_pending_replies(env):
    await receive(env, Incoming())
    env.config.telegram.whitelist_ids = []
    await env.bridge.process_one()
    assert env.submitted == []
    assert (await inbox(env))[0]["state"] == "rejected"


async def test_media_caption_is_not_silently_used_as_instruction(env):
    message = Incoming(text=None)
    message.caption, message.photo = "处理附件", [object()]
    await receive(env, message)
    assert not await env.bridge.process_one()
    assert env.submitted == []
    assert "文字回复" in (await inbox(env))[0]["response_text"]


async def test_callback_checks_owner_and_message_binding(env):
    for query in (Query(sender=999), Query(message_id=999), Query(owner=999)):
        await env.bridge.handle_callback(query)
        assert query.answers[-1][1].get("show_alert")
    assert env.bot.sent == [] and env.submitted == []


async def test_receipt_retry_does_not_resubmit_and_receipt_is_replyable(env):
    await receive(env, Incoming())
    await env.bridge.process_one()
    env.bot.fail = True
    await env.bridge.deliver_receipt()
    assert len(env.submitted) == 1
    env.bot.fail = False
    await env.db.conn.execute("UPDATE web_tg_reply_inbox SET response_after=0")
    await env.db.conn.commit()
    await env.bridge.deliver_receipt()
    assert len(env.submitted) == 1
    assert (await inbox(env))[0]["response_sent"] == 1
    record = await env.bridge.match_reply(Incoming(501, env.bot.sent[-1]["message"].message_id))
    assert record["conversation_uuid"] == "conv-1" and record["role"] == "receipt"


async def test_restart_recovers_pending_but_never_replays_uncertain_dispatch(env, monkeypatch):
    await receive(env, Incoming())
    await receive(env, Incoming(501, 200))
    await env.db.conn.execute("UPDATE web_tg_reply_inbox SET state='dispatching' WHERE telegram_message_id=501")
    await env.db.conn.commit()

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(env.bridge, "_worker_loop", idle)
    await env.bridge.start()
    assert [r["state"] for r in await inbox(env)] == ["pending", "uncertain"]
    assert "未自动重复执行" in (await inbox(env))[1]["response_text"]
    await env.bridge.process_one()
    assert len(env.submitted) == 1 and env.submitted[0][0]["conversation_uuid"] == "conv-1"
    assert not await env.bridge.process_one()


async def test_submit_exception_after_side_effect_is_not_retried(env):
    async def uncertain(row, text, **kwargs):
        await env.submit(row, text, **kwargs)
        raise RuntimeError("receipt lost after start")

    env.bridge.submit = uncertain
    message = Incoming()
    await receive(env, message)
    await env.bridge.process_one()
    await receive(env, message)
    assert not await env.bridge.process_one()
    assert len(env.submitted) == 1 and (await inbox(env))[0]["state"] == "uncertain"


async def test_startup_backlog_persists_only_registered_replies_before_ack(env):
    messages = [Incoming(), Incoming(501, None, "普通文本"), Incoming(502, 100, "/restart"), Incoming(503, 999), Incoming(504, 100, sender=999), Incoming()]
    env.bot.updates = [SimpleNamespace(update_id=10 + i, message=m) for i, m in enumerate(messages)]
    await _drain_startup_backlog(env.bot, SimpleNamespace(web_admin=SimpleNamespace(telegram_replies=env.bridge)))
    assert env.bot.offsets == [None, 16]
    assert len(await inbox(env)) == 1
    await env.bridge.process_one()
    assert len(env.submitted) == 1


async def test_startup_storage_failure_does_not_advance_offset(env, monkeypatch):
    async def broken(_message):
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(env.bridge, "match_reply", broken)
    env.bot.updates = [SimpleNamespace(update_id=10, message=Incoming())]
    with pytest.raises(RuntimeError):
        await _drain_startup_backlog(env.bot, SimpleNamespace(web_admin=SimpleNamespace(telegram_replies=env.bridge)))
    assert env.bot.offsets == [None]


async def test_filter_failure_is_consumed_not_leaked_into_admin_settings(env, monkeypatch):
    async def broken(_message):
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(env.bridge, "match_reply", broken)
    svc = SimpleNamespace(web_admin=SimpleNamespace(telegram_replies=env.bridge))
    message = Incoming()
    matched = await RegisteredTaskReply()(message, svc)
    assert matched["task_reply_lookup_failed"] is True
    await on_task_reply(message, svc, **matched)
    assert message.answers and not env.submitted


def test_notification_keyboard_has_reply_and_web_buttons(env):
    markup = reply_keyboard(env.config, "conv-1")
    assert markup.inline_keyboard[0][0].callback_data == REPLY_CALLBACK
    assert markup.inline_keyboard[0][1].url.endswith("/chat?id=conv-1")
    assert markup.model_dump()["force_reply"] is False


async def test_disabled_web_replies_immediately_without_waiting_for_disabled_worker(env):
    env.config.web.enabled = False
    await receive(env, Incoming())
    assert await inbox(env) == [] and env.submitted == []
    assert "Web 服务已关闭" in env.bot.sent[-1]["html"]


async def test_prompt_without_public_web_url_uses_native_force_reply(env):
    from aiogram.types import ForceReply
    env.config.web.custom_url = ""
    await env.bridge.handle_callback(Query())
    assert isinstance(env.bot.sent[-1]["reply_markup"], ForceReply)


async def test_startup_only_replays_task_buttons_and_tolerates_expired_callback_ack(env):
    from aiogram.exceptions import TelegramBadRequest
    old = Query()

    async def expired(*_args, **_kwargs):
        raise TelegramBadRequest(method=SimpleNamespace(), message="query is too old")

    old.answer = expired
    unrelated = Query()
    unrelated.data = "ui:old:submit"
    env.bot.updates = [SimpleNamespace(update_id=10, callback_query=old), SimpleNamespace(update_id=11, callback_query=unrelated)]
    await _drain_startup_backlog(env.bot, SimpleNamespace(web_admin=SimpleNamespace(telegram_replies=env.bridge)))
    assert env.bot.offsets == [None, 12]
    assert len(env.bot.sent) == 1 and await inbox(env) == [] and env.submitted == []


async def test_terminal_inbox_keeps_dedup_key_not_an_extra_copy_of_user_text(env):
    await receive(env, Incoming(text="原始用户材料"))
    assert (await inbox(env))[0]["text"] == "原始用户材料"
    await env.bridge.process_one()
    assert (await inbox(env))[0]["text"] == ""
    assert (await inbox(env))[0]["telegram_message_id"] == 500


async def test_real_dispatcher_keeps_interactions_and_task_replies_ahead_of_admin(env):
    from aiogram import Bot
    from aiogram.types import Update

    from app import telegram_ui
    from app.main import build_dispatcher

    class NoNetworkBot(Bot):
        async def __call__(self, *_args, **_kwargs):
            raise AssertionError("This test must never send a real Telegram request")

    interaction_answers = []

    class InteractionStub:
        async def match_reply(self, message):
            reply = message.reply_to_message
            return {"interaction_id": "form"} if reply and reply.message_id == 600 else None

        async def handle_reply(self, message, record):
            interaction_answers.append(message.text)

    bot = NoNetworkBot("123456:TEST")
    svc = SimpleNamespace(config=env.config, web_admin=SimpleNamespace(telegram_replies=env.bridge, interaction_telegram=InteractionStub()))
    dispatcher = build_dispatcher(svc)
    telegram_ui.set_pending(123, action="admin_setting_edit", message_id=700, data={"path": "memory.identity", "panel": "memory"})
    try:
        for message_id, reply_id, text in [(500, 100, "继续原会话"), (501, 600, "表单答案")]:
            message = {
                "message_id": message_id, "date": int(time.time()), "text": text,
                "from": {"id": 123, "is_bot": False, "first_name": "Owner"},
                "chat": {"id": 123, "type": "private"},
                "reply_to_message": {"message_id": reply_id, "date": int(time.time()), "text": "prompt", "chat": {"id": 123, "type": "private"}, "from": {"id": 123456, "is_bot": True, "first_name": "Bot"}},
            }
            await dispatcher.feed_update(bot, Update.model_validate({"update_id": message_id, "message": message}))
        assert len(await inbox(env)) == 1 and (await inbox(env))[0]["text"] == "继续原会话"
        assert interaction_answers == ["表单答案"]
        assert telegram_ui.get_pending(123).message_id == 700
    finally:
        telegram_ui.clear_pending(123)
        await bot.session.close()
