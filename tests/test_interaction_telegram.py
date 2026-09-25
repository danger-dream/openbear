from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.bot.interactions import RegisteredInteractionReply, on_interaction_reply
from app.db.engine import DB
from app.interaction_telegram import InteractionTelegram
from app.user_interactions import InteractionService

OWNER = 123


class FakeInteractions:
    def __init__(self, *items: dict[str, Any]) -> None:
        self.items = {str(item["interactionId"]): copy.deepcopy(item) for item in items}
        self.submissions: list[tuple[str, int, dict[str, Any], str]] = []
        self._lock = asyncio.Lock()

    async def get(self, interaction_id: str, *, owner_chat_id: int | None = None) -> dict[str, Any] | None:
        item = self.items.get(interaction_id)
        if item is None or (owner_chat_id is not None and int(item["ownerChatId"]) != int(owner_chat_id)):
            return None
        return copy.deepcopy(item)

    async def submit(
        self,
        interaction_id: str,
        owner_chat_id: int,
        answer: dict[str, Any],
        *,
        source: str = "telegram",
    ) -> dict[str, Any]:
        async with self._lock:
            item = self.items.get(interaction_id)
            if item is None or int(item["ownerChatId"]) != int(owner_chat_id):
                return {"ok": False, "statusCode": 403, "error": "confirmation_owner_mismatch"}
            if int(item.get("expiresAtMs") or 0) <= int(time.time() * 1000):
                item["status"] = "timeout"
                return {"ok": False, "statusCode": 409, "error": "confirmation_expired"}
            if item["status"] != "pending":
                return {"ok": False, "statusCode": 409, "error": "confirmation_already_resolved", "result": item.get("result")}
            canonical = copy.deepcopy(answer)
            canonical["source"] = source
            item["status"] = "cancelled" if answer.get("cancelled") else "answered"
            item["result"] = canonical
            self.submissions.append((interaction_id, owner_chat_id, copy.deepcopy(answer), source))
            return {"ok": True, "statusCode": 200, "result": canonical}


class FakeQuery:
    def __init__(self, data: str, message: Any, user_id: int = OWNER) -> None:
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[Any, bool]] = []

    async def answer(self, text: Any = None, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class FakeIncoming:
    def __init__(
        self,
        bot: Any,
        *,
        text: str,
        message_id: int,
        reply_to: int | None,
        user_id: int = OWNER,
        chat_id: int = OWNER,
    ) -> None:
        self.bot = bot
        self.text = text
        self.caption = None
        self.message_id = message_id
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.reply_to_message = SimpleNamespace(message_id=reply_to) if reply_to else None
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


class TelegramCapture:
    def __init__(self) -> None:
        self.next_id = 100
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.deleted: list[tuple[int, int]] = []
        self.fail_sends = 0
        self.fail_edits = 0
        self.fail_deletes = 0
        self.bot = SimpleNamespace(delete_message=self.delete)

    async def send(self, bot: Any, chat_id: int, body: str, **kwargs: Any) -> Any:
        if self.fail_sends:
            self.fail_sends -= 1
            raise RuntimeError("network down: do not log payload")
        self.next_id += 1
        message = SimpleNamespace(
            message_id=self.next_id,
            chat=SimpleNamespace(id=chat_id, type="private"),
            bot=bot,
            text=body,
        )
        self.sent.append({"chat_id": chat_id, "body": body, "message": message, **kwargs})
        return message

    async def edit(self, bot: Any, chat_id: int, message_id: int, body: str, **kwargs: Any) -> bool:
        if self.fail_edits:
            self.fail_edits -= 1
            raise RuntimeError("temporary edit failure")
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "body": body, **kwargs})
        return True

    async def delete(self, chat_id: int, message_id: int) -> bool:
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise RuntimeError("temporary delete failure")
        self.deleted.append((chat_id, message_id))
        return True


@pytest.fixture
async def db(tmp_path) -> DB:
    database = DB(str(tmp_path / "interaction-tg.db"))
    await database.connect()
    for schema_path in ("app/db/user_interactions.sql", "app/db/interaction_telegram.sql"):
        await database.conn.executescript(Path(schema_path).read_text(encoding="utf-8"))
    await database.conn.commit()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def config() -> Any:
    return SimpleNamespace(
        telegram=SimpleNamespace(whitelist_ids=[OWNER]),
        web=SimpleNamespace(
            custom_url="https://bear.example.test",
            interaction_notifications=SimpleNamespace(enabled=True, allow_reply=True),
        ),
    )


@pytest.fixture
def capture(monkeypatch) -> TelegramCapture:
    value = TelegramCapture()
    monkeypatch.setattr("app.interaction_telegram.send_rich", value.send)
    monkeypatch.setattr("app.interaction_telegram.edit_rich", value.edit)
    return value


def item(interaction_id: str, action: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "interactionId": interaction_id,
        "confirmationId": interaction_id,
        "ownerChatId": OWNER,
        "conversationUuid": "conv-1",
        "conversationTitle": "测试会话",
        "turnUuid": "turn-1",
        "toolCallId": "tool-1",
        "action": action,
        "title": f"{action} 标题",
        "body": f"{action} 正文",
        "type": "warning",
        "options": [],
        "questions": [],
        "multiple": False,
        "sensitive": False,
        "requiresAuthorization": False,
        "revision": 1,
        "expiresAtMs": int(time.time() * 1000) + 600_000,
        "status": "pending",
    }
    value.update(overrides)
    return value


async def drain(transport: InteractionTelegram, *, limit: int = 50) -> None:
    for _ in range(limit):
        delivery = await transport._claim_due()
        if delivery is None:
            return
        await transport._deliver(delivery)
    raise AssertionError("outbox did not drain")


async def wait_for_pending(service: InteractionService, *, timeout: float = 1.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if service.pending:
            return next(iter(service.pending))
        await asyncio.sleep(0.001)
    raise AssertionError("InteractionService request did not enter pending")


async def wait_for_outbox(db: DB, interaction_id: str, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = await (await db.conn.execute(
            "SELECT 1 FROM interaction_tg_outbox WHERE interaction_id=? LIMIT 1",
            (interaction_id,),
        )).fetchone()
        if row is not None:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("InteractionTelegram listener did not enqueue delivery")


async def root_context(transport: InteractionTelegram, interaction_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    draft = await transport._draft(interaction_id)
    assert draft is not None
    cur = await transport.db.conn.execute(
        "SELECT * FROM interaction_tg_messages WHERE interaction_id=? AND role='root' ORDER BY id DESC LIMIT 1",
        (interaction_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return draft, dict(row)


def outgoing(capture: TelegramCapture, message_id: int) -> Any:
    return next(entry["message"] for entry in capture.sent if entry["message"].message_id == message_id)


async def click(
    transport: InteractionTelegram,
    capture: TelegramCapture,
    token: str,
    command: str,
    message_id: int,
    *,
    user_id: int = OWNER,
) -> FakeQuery:
    query = FakeQuery(f"ui:{token}:{command}", outgoing(capture, message_id), user_id=user_id)
    await transport.handle_callback(query)
    assert query.answers
    return query


@pytest.mark.asyncio
async def test_created_is_immediate_and_network_failure_retries_without_touching_interaction(db, config, capture):
    current = item("immediate", "confirm")
    interactions = FakeInteractions(current)
    transport = InteractionTelegram(config, db, capture.bot, interactions)
    capture.fail_sends = 1

    before = int(time.time())
    await transport.on_interaction("created", current)
    delivery = await transport._claim_due()
    assert delivery is not None and delivery.event_type == "created"
    await transport._deliver(delivery)

    row = await (await db.conn.execute(
        "SELECT state, deliver_after, attempts, last_error FROM interaction_tg_outbox WHERE interaction_id='immediate'"
    )).fetchone()
    assert row["state"] == "pending"
    assert int(row["deliver_after"]) >= before + 1
    assert row["attempts"] == 1
    assert row["last_error"] == "RuntimeError"
    assert interactions.items["immediate"]["status"] == "pending"

    await db.conn.execute("UPDATE interaction_tg_outbox SET deliver_after=0 WHERE interaction_id='immediate'")
    await db.conn.commit()
    await drain(transport)
    assert len(capture.sent) == 1
    assert "有效期至" in capture.sent[0]["body"]


@pytest.mark.asyncio
async def test_confirm_text_is_feedback_and_never_boolean_authorization(db, config, capture):
    current = item("confirm-1", "confirm")
    interactions = FakeInteractions(current)
    transport = InteractionTelegram(config, db, capture.bot, interactions)
    await transport.on_interaction("created", current)
    await drain(transport)
    draft, root = await root_context(transport, "confirm-1")

    await click(transport, capture, draft["callback_token"], "sd:c", root["telegram_message_id"])
    reply = FakeIncoming(
        transport.bot,
        text=" 先备份，\n不要立刻重启。 ",
        message_id=501,
        reply_to=root["telegram_message_id"],
    )
    record = await transport.match_reply(reply)
    assert record is not None
    sent_before_reply = len(capture.sent)
    await transport.handle_reply(reply, record)
    assert len(capture.sent) == sent_before_reply
    await drain(transport)
    reply_edit = capture.edited[-1]
    assert reply_edit["message_id"] == root["telegram_message_id"]
    assert "当前决定：<b>确认</b>" in reply_edit["body"]
    assert "先备份，\n不要立刻重启。" in reply_edit["body"]
    draft = await transport._draft("confirm-1")
    assert draft and draft["text_value"] == " 先备份，\n不要立刻重启。 "

    await click(transport, capture, draft["callback_token"], "submit", root["telegram_message_id"])
    submitted = interactions.submissions[0][2]
    assert submitted == {
        "revision": 1,
        "decision": "feedback",
        "selectedDecision": "confirm",
        "text": " 先备份，\n不要立刻重启。 ",
        "confirmed": False,
    }


@pytest.mark.asyncio
async def test_authorization_select_keeps_choice_and_text_and_labels_submit_as_feedback(db, config, capture):
    current = item(
        "select-auth",
        "select",
        requiresAuthorization=True,
        options=[{"label": "允许一次", "value": "once"}, {"label": "拒绝", "value": "deny"}],
    )
    interactions = FakeInteractions(current)
    transport = InteractionTelegram(config, db, capture.bot, interactions)
    await transport.on_interaction("created", current)
    await drain(transport)
    draft, root = await root_context(transport, "select-auth")

    await click(transport, capture, draft["callback_token"], "so:0", root["telegram_message_id"])
    reply = FakeIncoming(transport.bot, text="先缩小权限范围", message_id=502, reply_to=root["telegram_message_id"])
    sent_before_reply = len(capture.sent)
    await transport.handle_reply(reply, (await transport.match_reply(reply)) or {})
    assert len(capture.sent) == sent_before_reply
    await drain(transport)
    reply_edit = capture.edited[-1]
    assert reply_edit["message_id"] == root["telegram_message_id"]
    assert "已选：允许一次" in reply_edit["body"] and "先缩小权限范围" in reply_edit["body"]
    draft = await transport._draft("select-auth")
    assert draft
    markup = transport._root_keyboard(current, draft, allow_reply=True)
    button_texts = [button.text for row in markup.inline_keyboard for button in row]
    assert "提交意见（不执行原操作）" in button_texts

    await click(transport, capture, draft["callback_token"], "submit", root["telegram_message_id"])
    submitted = interactions.submissions[0][2]
    assert submitted["selectedValues"] == ["once"]
    assert submitted["selectedIndexes"] == [0]
    assert submitted["text"] == "先缩小权限范围"


@pytest.mark.asyncio
async def test_prompt_reply_is_saved_then_explicitly_submitted(db, config, capture):
    current = item("prompt-1", "prompt")
    interactions = FakeInteractions(current)
    transport = InteractionTelegram(config, db, capture.bot, interactions)
    await transport.on_interaction("created", current)
    await drain(transport)
    draft, root = await root_context(transport, "prompt-1")

    reply = FakeIncoming(transport.bot, text="  保留首尾空格  ", message_id=503, reply_to=root["telegram_message_id"])
    sent_before_reply = len(capture.sent)
    capture.fail_edits = 1
    await transport.handle_reply(reply, (await transport.match_reply(reply)) or {})
    assert len(capture.sent) == sent_before_reply
    assert interactions.submissions == []
    await drain(transport)
    retry_row = await (await db.conn.execute(
        "SELECT state, attempts FROM interaction_tg_outbox WHERE interaction_id='prompt-1' AND event_type='refresh'"
    )).fetchone()
    assert retry_row["state"] == "pending" and retry_row["attempts"] == 1
    assert len(capture.sent) == sent_before_reply
    await db.conn.execute(
        "UPDATE interaction_tg_outbox SET deliver_after=0 WHERE interaction_id='prompt-1' AND event_type='refresh'"
    )
    await db.conn.commit()
    await drain(transport)
    reply_edit = capture.edited[-1]
    assert reply_edit["message_id"] == root["telegram_message_id"]
    assert "保留首尾空格" in reply_edit["body"]
    assert len(capture.sent) == sent_before_reply
    draft = await transport._draft("prompt-1")
    assert draft and draft["text_value"] == "  保留首尾空格  "

    await click(transport, capture, draft["callback_token"], "submit", root["telegram_message_id"])
    assert interactions.submissions[0][2] == {"revision": 1, "value": "  保留首尾空格  "}


@pytest.mark.asyncio
async def test_questionnaire_uses_distinct_prompts_supports_text_and_return_edit(db, config, capture):
    current = item(
        "survey-1",
        "questionnaire",
        questions=[
            {
                "id": "q1",
                "type": "choice",
                "question": "选择方案",
                "required": True,
                "multiple": True,
                "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
            },
            {"id": "q2", "type": "open", "question": "补充信息", "required": True},
        ],
    )
    interactions = FakeInteractions(current)
    transport = InteractionTelegram(config, db, capture.bot, interactions)
    await transport.on_interaction("created", current)
    await drain(transport)
    draft, root = await root_context(transport, "survey-1")
    token = draft["callback_token"]

    await click(transport, capture, token, "start", root["telegram_message_id"])
    await drain(transport)
    qrows = await (await db.conn.execute(
        "SELECT * FROM interaction_tg_messages WHERE interaction_id='survey-1' AND role='question' ORDER BY id"
    )).fetchall()
    q0 = dict(qrows[-1])
    assert q0["question_index"] == 0 and q0["telegram_message_id"] != root["telegram_message_id"]

    await click(transport, capture, token, "q:0:o:0", q0["telegram_message_id"])
    q0_reply = FakeIncoming(transport.bot, text="以 A 为基础", message_id=504, reply_to=q0["telegram_message_id"])
    sent_before_q0_reply = len(capture.sent)
    await transport.handle_reply(q0_reply, (await transport.match_reply(q0_reply)) or {})
    assert len(capture.sent) == sent_before_q0_reply
    await drain(transport)
    q0_edit = capture.edited[-1]
    assert q0_edit["message_id"] == q0["telegram_message_id"]
    assert "已选：A" in q0_edit["body"] and "以 A 为基础" in q0_edit["body"]
    q0_buttons = [button.text for row in q0_edit["reply_markup"].inline_keyboard for button in row]
    assert "下一题 ▶" in q0_buttons
    await click(transport, capture, token, "q:0:next", q0["telegram_message_id"])
    await drain(transport)
    qrows = await (await db.conn.execute(
        "SELECT * FROM interaction_tg_messages WHERE interaction_id='survey-1' AND role='question' ORDER BY id"
    )).fetchall()
    q1 = dict(qrows[-1])
    assert q1["question_index"] == 1 and q1["telegram_message_id"] != q0["telegram_message_id"]

    # A delayed reply to the old per-question prompt is consumed but cannot alter it.
    late = FakeIncoming(transport.bot, text="迟到内容", message_id=505, reply_to=q0["telegram_message_id"])
    late_record = await transport.match_reply(late)
    assert late_record is not None
    await transport.handle_reply(late, late_record)
    draft = await transport._draft("survey-1")
    assert draft and draft["questionnaire"]["q1"]["text"] == "以 A 为基础"

    q1_reply = FakeIncoming(transport.bot, text="第二题原文", message_id=506, reply_to=q1["telegram_message_id"])
    sent_before_final_reply = len(capture.sent)
    await transport.handle_reply(q1_reply, (await transport.match_reply(q1_reply)) or {})
    assert len(capture.sent) == sent_before_final_reply
    assert interactions.submissions == []
    await drain(transport)
    summary_edit = capture.edited[-1]
    assert summary_edit["message_id"] == q1["telegram_message_id"]
    assert "问卷回答汇总" in summary_edit["body"]
    assert "以 A 为基础" in summary_edit["body"] and "第二题原文" in summary_edit["body"]
    summary_buttons = [button.text for row in summary_edit["reply_markup"].inline_keyboard for button in row]
    assert "✅ 最终提交问卷" in summary_buttons
    summary = q1

    await click(transport, capture, token, "edit:0", summary["telegram_message_id"])
    await drain(transport)
    edited_q0_row = await (await db.conn.execute(
        "SELECT * FROM interaction_tg_messages WHERE interaction_id='survey-1' AND role='question' AND active=1 ORDER BY id DESC LIMIT 1"
    )).fetchone()
    edited_q0 = dict(edited_q0_row)
    assert edited_q0["question_index"] == 0 and edited_q0["telegram_message_id"] != q0["telegram_message_id"]
    replacement = FakeIncoming(transport.bot, text="修改后的第一题说明", message_id=507, reply_to=edited_q0["telegram_message_id"])
    await transport.handle_reply(replacement, (await transport.match_reply(replacement)) or {})
    await click(transport, capture, token, "q:0:next", edited_q0["telegram_message_id"])
    await drain(transport)
    current_q1_row = await (await db.conn.execute(
        "SELECT * FROM interaction_tg_messages WHERE interaction_id='survey-1' AND role='question' AND active=1 ORDER BY id DESC LIMIT 1"
    )).fetchone()
    current_q1 = dict(current_q1_row)
    sent_before_summary = len(capture.sent)
    await click(transport, capture, token, "q:1:next", current_q1["telegram_message_id"])
    await drain(transport)
    assert len(capture.sent) == sent_before_summary
    final_summary_edit = capture.edited[-1]
    assert final_summary_edit["message_id"] == current_q1["telegram_message_id"]
    assert "问卷回答汇总" in final_summary_edit["body"]
    await click(transport, capture, token, "qsubmit", current_q1["telegram_message_id"])

    submitted = interactions.submissions[0][2]
    assert submitted == {
        "revision": 1,
        "answers": [
            {"questionId": "q1", "selectedValues": ["a"], "text": "修改后的第一题说明"},
            {"questionId": "q2", "selectedValues": [], "text": "第二题原文"},
        ],
    }
    await drain(transport)
    prompt_rows = await (await db.conn.execute(
        "SELECT telegram_message_id FROM interaction_tg_messages WHERE interaction_id='survey-1'"
    )).fetchall()
    prompt_ids = {int(row["telegram_message_id"]) for row in prompt_rows}
    assert {message_id for chat_id, message_id in capture.deleted if chat_id == OWNER} == prompt_ids


@pytest.mark.asyncio
async def test_sensitive_notification_contains_no_payload_and_rejects_reply(db, config, capture):
    secret = "TOP-SECRET-parameter"
    current = item(
        "sensitive-1",
        "select",
        title=secret,
        body=f"approve {secret}",
        sensitive=True,
        requiresAuthorization=True,
        options=[{"label": secret, "value": secret}],
        defaultValue=secret,
    )
    interactions = FakeInteractions(current)
    transport = InteractionTelegram(config, db, capture.bot, interactions)
    await transport.on_interaction("created", current)
    await drain(transport)

    assert len(capture.sent) == 1
    assert secret not in capture.sent[0]["body"]
    markup = capture.sent[0]["reply_markup"]
    assert all(button.callback_data is None for row in markup.inline_keyboard for button in row)
    assert await transport._draft("sensitive-1") is None

    root_row = await (await db.conn.execute(
        "SELECT * FROM interaction_tg_messages WHERE interaction_id='sensitive-1'"
    )).fetchone()
    reply = FakeIncoming(transport.bot, text=secret, message_id=508, reply_to=int(root_row["telegram_message_id"]))
    record = await transport.match_reply(reply)
    assert record is not None
    await transport.handle_reply(reply, record)
    assert interactions.submissions == []
    assert await transport._draft("sensitive-1") is None
    dump = " ".join(str(value) for row in await (await db.conn.execute(
        "SELECT * FROM interaction_tg_outbox JOIN interaction_tg_messages USING(interaction_id) WHERE interaction_id='sensitive-1'"
    )).fetchall() for value in row)
    assert secret not in dump


@pytest.mark.asyncio
async def test_identity_expiry_concurrency_and_reply_isolation(db, config, capture):
    current = item("race-1", "confirm")
    interactions = FakeInteractions(current)
    transport = InteractionTelegram(config, db, capture.bot, interactions)
    await transport.on_interaction("created", current)
    await drain(transport)
    draft, root = await root_context(transport, "race-1")
    token = draft["callback_token"]

    wrong = await click(transport, capture, token, "sd:c", root["telegram_message_id"], user_id=999)
    assert wrong.answers[-1][1] is True
    assert (await transport._draft("race-1"))["confirm_decision"] == ""

    await click(transport, capture, token, "sd:c", root["telegram_message_id"])
    q1 = FakeQuery(f"ui:{token}:submit", outgoing(capture, root["telegram_message_id"]))
    q2 = FakeQuery(f"ui:{token}:submit", outgoing(capture, root["telegram_message_id"]))
    await asyncio.gather(transport.handle_callback(q1), transport.handle_callback(q2))
    assert len(interactions.submissions) == 1
    assert any(answer[1] is True for answer in q2.answers + q1.answers)

    unrelated = FakeIncoming(transport.bot, text="普通管理文本", message_id=600, reply_to=None)
    assert await transport.match_reply(unrelated) is None
    command_reply = FakeIncoming(transport.bot, text="/status", message_id=601, reply_to=root["telegram_message_id"])
    assert await transport.match_reply(command_reply) is None
    old_reply = FakeIncoming(transport.bot, text="旧提示回复", message_id=602, reply_to=root["telegram_message_id"])
    assert await transport.match_reply(old_reply) is not None

    filter_ = RegisteredInteractionReply()
    svc = SimpleNamespace(web_admin=SimpleNamespace(interaction_telegram=transport))
    assert await filter_(unrelated, svc) is False
    assert await filter_(old_reply, svc)

    expired = item("expired-1", "select", options=["A"], expiresAtMs=int(time.time() * 1000) - 1)
    interactions.items["expired-1"] = expired
    # Insert local data to model a button whose canonical deadline passed.
    expired["expiresAtMs"] = int(time.time() * 1000) + 60_000
    await transport.on_interaction("created", expired)
    await drain(transport)
    expired_draft, expired_root = await root_context(transport, "expired-1")
    interactions.items["expired-1"]["expiresAtMs"] = int(time.time() * 1000) - 1
    stale = await click(transport, capture, expired_draft["callback_token"], "so:0", expired_root["telegram_message_id"])
    assert stale.answers[-1][1] is True
    assert not any(entry[0] == "expired-1" for entry in interactions.submissions)


@pytest.mark.asyncio
async def test_reply_lookup_failure_fails_closed_only_for_valid_reply_candidate():
    class BrokenLookupTransport:
        def __init__(self) -> None:
            self.handled = False

        async def match_reply(self, _message):
            raise RuntimeError("temporary database failure")

        async def handle_reply(self, _message, _record):
            self.handled = True

    transport = BrokenLookupTransport()
    svc = SimpleNamespace(web_admin=SimpleNamespace(interaction_telegram=transport))
    filter_ = RegisteredInteractionReply()
    candidate = FakeIncoming(SimpleNamespace(), text="待保存的交互答案", message_id=640, reply_to=100)

    matched = await filter_(candidate, svc)
    assert matched == {"interaction_tg_record": {}, "interaction_tg_lookup_failed": True}
    await on_interaction_reply(candidate, svc, **matched)
    assert candidate.answers == ["交互回复暂未保存，请稍后重试或前往 Web。"]
    assert transport.handled is False

    non_reply = FakeIncoming(SimpleNamespace(), text="普通文本", message_id=641, reply_to=None)
    command = FakeIncoming(SimpleNamespace(), text="/status", message_id=642, reply_to=100)
    wrong_identity = FakeIncoming(
        SimpleNamespace(),
        text="身份不匹配",
        message_id=643,
        reply_to=100,
        user_id=999,
        chat_id=OWNER,
    )
    assert await filter_(non_reply, svc) is False
    assert await filter_(command, svc) is False
    assert await filter_(wrong_identity, svc) is False


@pytest.mark.asyncio
async def test_concurrent_multi_select_and_reply_merge_without_lost_draft(db, config, capture):
    current = item(
        "draft-race",
        "select",
        multiple=True,
        options=[{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
    )
    interactions = FakeInteractions(current)
    transport = InteractionTelegram(config, db, capture.bot, interactions)
    await transport.on_interaction("created", current)
    await drain(transport)
    draft, root = await root_context(transport, "draft-race")
    token = draft["callback_token"]
    reply = FakeIncoming(transport.bot, text="并发补充原文", message_id=650, reply_to=root["telegram_message_id"])
    record = await transport.match_reply(reply)
    assert record is not None
    qa = FakeQuery(f"ui:{token}:so:0", outgoing(capture, root["telegram_message_id"]))
    qb = FakeQuery(f"ui:{token}:so:1", outgoing(capture, root["telegram_message_id"]))

    await asyncio.gather(
        transport.handle_callback(qa),
        transport.handle_callback(qb),
        transport.handle_reply(reply, record),
    )
    merged = await transport._draft("draft-race")
    assert merged
    assert merged["selected_values"] == ["a", "b"]
    assert merged["text_value"] == "并发补充原文"


@pytest.mark.asyncio
async def test_restart_invalidates_interrupted_draft_and_deletes_old_prompts(db, config, capture):
    current = item("restart-1", "prompt")
    interactions = FakeInteractions(current)
    transport = InteractionTelegram(config, db, capture.bot, interactions)
    await transport.on_interaction("created", current)
    await drain(transport)
    _draft, root = await root_context(transport, "restart-1")

    interactions.items["restart-1"]["status"] = "interrupted"
    capture.fail_deletes = 1
    await transport._reconcile_startup()
    draft = await transport._draft("restart-1")
    assert draft and draft["active"] == 0
    row = await (await db.conn.execute(
        "SELECT active FROM interaction_tg_messages WHERE interaction_id='restart-1' AND role='root'"
    )).fetchone()
    assert row["active"] == 0
    terminal = await transport._claim_due()
    assert terminal is not None and terminal.event_type == "resolved"
    await transport._deliver(terminal)
    retry_row = await (await db.conn.execute(
        "SELECT state, attempts FROM interaction_tg_outbox WHERE interaction_id='restart-1' AND event_type='resolved'"
    )).fetchone()
    assert retry_row["state"] == "pending" and retry_row["attempts"] == 1
    await db.conn.execute(
        "UPDATE interaction_tg_outbox SET deliver_after=0 WHERE interaction_id='restart-1' AND event_type='resolved'"
    )
    await db.conn.commit()
    await drain(transport)
    assert (OWNER, root["telegram_message_id"]) in capture.deleted
    assert not any("该交互已不能再通过此消息作答" in edit["body"] for edit in capture.edited)

    old = FakeIncoming(transport.bot, text="停机后的迟到回复", message_id=700, reply_to=root["telegram_message_id"])
    record = await transport.match_reply(old)
    assert record is not None
    await transport.handle_reply(old, record)
    assert interactions.submissions == []


# Real broker integration ----------------------------------------------------


@pytest.mark.asyncio
async def test_real_service_select_wakes_request_with_lossless_telegram_answer(db, config, capture):
    service = InteractionService(db)
    transport = InteractionTelegram(config, db, capture.bot, service)
    service.add_listener(transport.on_interaction)
    await service.start()
    request_task = asyncio.create_task(service.request(
        {
            "action": "select",
            "title": "选择方案",
            "body": "请选择并可补充原文",
            "options": [
                {"label": "方案 A", "value": "a"},
                {"label": "方案 B", "value": "b"},
            ],
            "timeoutSeconds": 10,
        },
        owner_chat_id=OWNER,
        conversation_uuid="conv-real-select",
        conversation_title="真实协议",
    ))
    interaction_id = await wait_for_pending(service)
    await wait_for_outbox(db, interaction_id)
    await drain(transport)
    draft, root = await root_context(transport, interaction_id)

    await click(transport, capture, draft["callback_token"], "so:0", root["telegram_message_id"])
    reply = FakeIncoming(
        transport.bot,
        text=" 以 A 为基础，\n但先不要部署。 ",
        message_id=801,
        reply_to=root["telegram_message_id"],
    )
    reply_record = await transport.match_reply(reply)
    assert reply_record is not None
    await transport.handle_reply(reply, reply_record)
    draft = await transport._draft(interaction_id)
    assert draft is not None
    await click(transport, capture, draft["callback_token"], "submit", root["telegram_message_id"])

    result = await asyncio.wait_for(request_task, timeout=1)
    assert result["status"] == "answered"
    assert result["source"] == "telegram"
    assert result["selectedIndexes"] == [0]
    assert result["selectedValues"] == ["a"]
    assert result["selectedLabels"] == ["方案 A"]
    assert result["text"] == " 以 A 为基础，\n但先不要部署。 "
    assert result["answerMode"] == "options_with_text"
    stored = await service.get(interaction_id, owner_chat_id=OWNER)
    assert stored and stored["status"] == "answered" and stored["revision"] == 2
    await drain(transport)
    assert (OWNER, root["telegram_message_id"]) in capture.deleted
    await service.stop()


@pytest.mark.asyncio
async def test_real_service_text_only_confirm_is_valid_feedback_without_null_choice(db, config, capture):
    service = InteractionService(db)
    transport = InteractionTelegram(config, db, capture.bot, service)
    service.add_listener(transport.on_interaction)
    await service.start()
    request_task = asyncio.create_task(service.request(
        {
            "action": "confirm",
            "title": "请确认",
            "body": "也可以只提交意见",
            "timeoutSeconds": 10,
        },
        owner_chat_id=OWNER,
        conversation_uuid="conv-real-feedback",
    ))
    interaction_id = await wait_for_pending(service)
    await wait_for_outbox(db, interaction_id)
    await drain(transport)
    draft, root = await root_context(transport, interaction_id)
    reply = FakeIncoming(
        transport.bot,
        text="先补充检查，不执行原操作",
        message_id=806,
        reply_to=root["telegram_message_id"],
    )
    await transport.handle_reply(reply, (await transport.match_reply(reply)) or {})
    draft = await transport._draft(interaction_id)
    assert draft is not None
    await click(transport, capture, draft["callback_token"], "submit", root["telegram_message_id"])

    result = await asyncio.wait_for(request_task, timeout=1)
    assert result["source"] == "telegram"
    assert result["decision"] == "feedback"
    assert result["selectedDecision"] == "feedback"
    assert result["text"] == "先补充检查，不执行原操作"
    assert result["confirmed"] is False
    await service.stop()


@pytest.mark.asyncio
async def test_real_service_web_wins_and_stale_telegram_inputs_cannot_overwrite(db, config, capture):
    service = InteractionService(db)
    transport = InteractionTelegram(config, db, capture.bot, service)
    service.add_listener(transport.on_interaction)
    await service.start()
    request_task = asyncio.create_task(service.request(
        {
            "action": "confirm",
            "title": "确认操作",
            "body": "是否继续",
            "timeoutSeconds": 10,
        },
        owner_chat_id=OWNER,
        conversation_uuid="conv-web-wins",
    ))
    interaction_id = await wait_for_pending(service)
    await wait_for_outbox(db, interaction_id)
    await drain(transport)
    draft, root = await root_context(transport, interaction_id)

    web_response = await service.submit(
        interaction_id,
        OWNER,
        {"revision": 1, "decision": "confirm", "confirmed": True, "text": ""},
        source="web",
    )
    assert web_response["ok"] is True
    web_result = await asyncio.wait_for(request_task, timeout=1)
    assert web_result["source"] == "web" and web_result["confirmed"] is True
    await drain(transport)
    assert (OWNER, root["telegram_message_id"]) in capture.deleted

    stale_click = await click(
        transport,
        capture,
        draft["callback_token"],
        "sd:r",
        root["telegram_message_id"],
    )
    assert stale_click.answers[-1][1] is True
    late = FakeIncoming(
        transport.bot,
        text="迟到的 Telegram 反对意见",
        message_id=802,
        reply_to=root["telegram_message_id"],
    )
    record = await transport.match_reply(late)
    assert record is not None
    await transport.handle_reply(late, record)

    canonical = await service.get(interaction_id, owner_chat_id=OWNER)
    assert canonical and canonical["revision"] == 2
    assert canonical["result"] == web_result
    assert canonical["result"]["source"] == "web"
    assert canonical["result"]["confirmed"] is True
    await service.stop()


@pytest.mark.asyncio
async def test_real_service_questionnaire_validates_text_and_options_with_text(db, config, capture):
    service = InteractionService(db)
    transport = InteractionTelegram(config, db, capture.bot, service)
    service.add_listener(transport.on_interaction)
    await service.start()
    request_task = asyncio.create_task(service.request(
        {
            "action": "questionnaire",
            "title": "TG 问卷体验测试（3 题）",
            "body": "这是一份纯测试问卷，不会修改任何设置。请在 Telegram Bot 里逐题作答，试试选择、补充文字、返回修改，最后提交整份问卷。选择题也可以不选选项，直接回复自己的答案。",
            "type": "info",
            "sensitive": False,
            "timeoutSeconds": 600,
            "questions": [
                {
                    "id": "color",
                    "type": "choice",
                    "question": "测试单选：你更喜欢哪种配色？",
                    "required": True,
                    "multiple": False,
                    "options": [
                        {"label": "蓝灰配色", "value": "blue_gray"},
                        {"label": "深色配色", "value": "dark"},
                        {"label": "都可以", "value": "either"},
                    ],
                },
                {
                    "id": "features",
                    "type": "choice",
                    "question": "测试多选：你想检查哪些功能？",
                    "required": True,
                    "multiple": True,
                    "options": [
                        {"label": "选项按钮", "value": "buttons"},
                        {"label": "补充文字", "value": "text"},
                        {"label": "返回修改", "value": "back"},
                    ],
                },
                {
                    "id": "comment",
                    "type": "open",
                    "question": "测试文本：随便写一句话",
                    "description": "例如：我正在 TG 里测试问卷。这题也可以跳过。",
                    "required": False,
                },
            ],
        },
        owner_chat_id=OWNER,
        conversation_uuid="conv-real-survey",
    ))
    interaction_id = await wait_for_pending(service)
    await wait_for_outbox(db, interaction_id)
    await drain(transport)
    draft, root = await root_context(transport, interaction_id)
    token = draft["callback_token"]

    await click(transport, capture, token, "start", root["telegram_message_id"])
    await drain(transport)
    q0 = dict(await (await db.conn.execute(
        "SELECT * FROM interaction_tg_messages WHERE interaction_id=? AND role='question' AND active=1",
        (interaction_id,),
    )).fetchone())
    await click(transport, capture, token, "q:0:o:0", q0["telegram_message_id"])
    await click(transport, capture, token, "q:0:next", q0["telegram_message_id"])
    await drain(transport)

    q1 = dict(await (await db.conn.execute(
        "SELECT * FROM interaction_tg_messages WHERE interaction_id=? AND role='question' AND active=1",
        (interaction_id,),
    )).fetchone())
    await click(transport, capture, token, "q:1:o:0", q1["telegram_message_id"])
    middle = FakeIncoming(transport.bot, text="中间题补充", message_id=804, reply_to=q1["telegram_message_id"])
    sent_before_middle = len(capture.sent)
    await transport.handle_reply(middle, (await transport.match_reply(middle)) or {})
    assert len(capture.sent) == sent_before_middle == 3
    await drain(transport)
    middle_edit = capture.edited[-1]
    assert middle_edit["message_id"] == q1["telegram_message_id"]
    assert "已选：选项按钮" in middle_edit["body"] and "中间题补充" in middle_edit["body"]
    assert "下一题 ▶" in [button.text for row in middle_edit["reply_markup"].inline_keyboard for button in row]
    await click(transport, capture, token, "q:1:next", q1["telegram_message_id"])
    await drain(transport)

    q2 = dict(await (await db.conn.execute(
        "SELECT * FROM interaction_tg_messages WHERE interaction_id=? AND role='question' AND active=1",
        (interaction_id,),
    )).fetchone())
    assert len(capture.sent) == 4
    opened = FakeIncoming(transport.bot, text="测试一下回复", message_id=805, reply_to=q2["telegram_message_id"])
    sent_before_final = len(capture.sent)
    await transport.handle_reply(opened, (await transport.match_reply(opened)) or {})
    assert len(capture.sent) == sent_before_final
    assert service.pending[interaction_id]["status"] == "pending"
    await drain(transport)
    assert len(capture.sent) == sent_before_final
    final_edit = capture.edited[-1]
    assert final_edit["message_id"] == q2["telegram_message_id"]
    assert "问卷回答汇总" in final_edit["body"]
    assert "已选：蓝灰配色" in final_edit["body"]
    assert "已选：选项按钮" in final_edit["body"] and "中间题补充" in final_edit["body"]
    assert "测试一下回复" in final_edit["body"]
    final_buttons = [button.text for row in final_edit["reply_markup"].inline_keyboard for button in row]
    assert "✅ 最终提交问卷" in final_buttons
    summary_count = await (await db.conn.execute(
        "SELECT COUNT(*) FROM interaction_tg_messages WHERE interaction_id=? AND role='summary'",
        (interaction_id,),
    )).fetchone()
    assert int(summary_count[0]) == 0
    await click(transport, capture, token, "qsubmit", q2["telegram_message_id"])

    result = await asyncio.wait_for(request_task, timeout=1)
    assert result["source"] == "telegram"
    assert [answer["answerMode"] for answer in result["answers"]] == ["options", "options_with_text", "text"]
    assert result["answers"][0]["selectedValues"] == ["blue_gray"]
    assert result["answers"][0]["selectedLabels"] == ["蓝灰配色"]
    assert result["answers"][0]["text"] == ""
    assert result["answers"][1]["selectedValues"] == ["buttons"]
    assert result["answers"][1]["selectedLabels"] == ["选项按钮"]
    assert result["answers"][1]["text"] == "中间题补充"
    assert result["answers"][2]["text"] == "测试一下回复"
    await service.stop()


@pytest.mark.asyncio
async def test_real_service_identity_expiry_and_sensitive_projection_keep_notifications_safe(db, config, capture):
    service = InteractionService(db)
    transport = InteractionTelegram(config, db, capture.bot, service)
    service.add_listener(transport.on_interaction)
    await service.start()
    secret = "REAL-SENSITIVE-VALUE"
    sensitive_task = asyncio.create_task(service.request(
        {
            "action": "prompt",
            "title": secret,
            "body": f"请输入 {secret}",
            "defaultValue": secret,
            "sensitive": True,
            "timeoutSeconds": 10,
        },
        owner_chat_id=OWNER,
        conversation_uuid="conv-sensitive-real",
        conversation_title=secret,
    ))
    sensitive_id = await wait_for_pending(service)
    await wait_for_outbox(db, sensitive_id)
    projected = await service.get(sensitive_id, owner_chat_id=OWNER)
    assert projected
    assert projected["sensitive"] is True
    assert projected["title"] == "敏感交互"
    assert projected["body"] == "" and projected["conversationTitle"] == ""
    assert projected["options"] == [] and projected["questions"] == [] and projected["defaultValue"] == ""
    assert await service.get(sensitive_id, owner_chat_id=999) is None
    wrong_owner = await service.submit(sensitive_id, 999, {"value": "x"}, source="telegram")
    assert wrong_owner == {"ok": False, "statusCode": 404, "error": "confirmation_not_found"}
    web_only = await service.submit(sensitive_id, OWNER, {"value": "x"}, source="telegram")
    assert web_only["statusCode"] == 403 and web_only["error"] == "sensitive_interaction_web_only"

    await drain(transport)
    sensitive_messages = [entry["body"] for entry in capture.sent]
    assert sensitive_messages and all(secret not in body for body in sensitive_messages)
    assert await transport._draft(sensitive_id) is None
    await service.terminate(sensitive_id, "cancelled")
    sensitive_result = await asyncio.wait_for(sensitive_task, timeout=1)
    assert sensitive_result["status"] == "cancelled"
    await drain(transport)
    assert (OWNER, capture.sent[0]["message"].message_id) in capture.deleted

    expiring_task = asyncio.create_task(service.request(
        {
            "action": "select",
            "title": "即将过期",
            "body": "不要接受迟到回答",
            "options": [{"label": "A", "value": "a"}],
            "timeoutSeconds": 0.05,
        },
        owner_chat_id=OWNER,
        conversation_uuid="conv-expiry-real",
    ))
    expiring_id = await wait_for_pending(service)
    await wait_for_outbox(db, expiring_id)
    await drain(transport)
    expiring_draft, expiring_root = await root_context(transport, expiring_id)
    expired_result = await asyncio.wait_for(expiring_task, timeout=1)
    assert expired_result["status"] == "timeout" and expired_result["source"] == "system"
    stale = await click(
        transport,
        capture,
        expiring_draft["callback_token"],
        "so:0",
        expiring_root["telegram_message_id"],
    )
    assert stale.answers[-1][1] is True
    stored = await service.get(expiring_id, owner_chat_id=OWNER)
    assert stored and stored["status"] == "timeout" and stored["revision"] == 2
    await drain(transport)
    assert (OWNER, expiring_root["telegram_message_id"]) in capture.deleted
    await service.stop()
