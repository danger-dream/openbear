"""Narrow, durable Telegram ingress into existing Web conversations.

Only replies to registered bot messages are input. Telegram message IDs are
idempotency keys; no "current Telegram conversation" or separate Agent loop exists.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlparse

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Config
from app.db.engine import DB
from app.logging import get_logger
from app.telegram_ui import send_rich

log = get_logger("web_telegram_replies")
REPLY_CALLBACK = "wt:reply"
_BINDING_TTL_S = 90 * 86_400


def private_owner(message: Any) -> int:
    chat = getattr(message, "chat", None)
    kind = getattr(chat, "type", None)
    owner = int(getattr(chat, "id", 0) or 0)
    return owner if owner > 0 and (str(kind) == "private" or getattr(kind, "value", None) == "private") else 0


def reply_candidate(message: Any) -> bool:
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    return bool(
        private_owner(message)
        and int(getattr(getattr(message, "from_user", None), "id", 0) or 0) == private_owner(message)
        and int(getattr(getattr(message, "reply_to_message", None), "message_id", 0) or 0) > 0
        and not str(text).lstrip().startswith("/")
    )


def reply_keyboard(config: Config, conversation_uuid: str) -> InlineKeyboardMarkup | None:
    # Every delivered result is already bound to its Web conversation. Native
    # Telegram replies need neither a continuation button nor a ForceReply prompt.
    base = str(config.web.custom_url or "").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    button = InlineKeyboardButton(text="打开 Web 会话", url=f"{base}/chat?id={quote(conversation_uuid, safe='')}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]], force_reply=False)


async def bind_message(db: DB, owner: int, message_id: int, conversation_uuid: str, root: str = "", *, role: str = "notification") -> None:
    await db.conn.execute(
        """INSERT OR IGNORE INTO web_tg_messages
           (owner_chat_id, telegram_message_id, conversation_uuid, root_turn_uuid, role, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (owner, message_id, conversation_uuid, root, role, int(time.time())),
    )
    # The caller commits with its delivery checkpoint when applicable.


class WebTelegramReplies:
    def __init__(self, config: Config, db: DB, bot: Any, submit: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self.config, self.db, self.bot, self.submit = config, db, bot, submit
        self._worker: asyncio.Task[Any] | None = None
        self._wake = asyncio.Event()

    def apply_config(self, config: Config) -> None:
        self.config = config
        self._wake.set()

    async def start(self) -> None:
        # A crash can occur after starting a run and before saving its receipt.
        # Never silently replay that instruction (including queued steering).
        await self.db.conn.execute(
            """UPDATE web_tg_reply_inbox SET state='uncertain', text='', response_text=?, updated_at=?
               WHERE state='dispatching'""",
            ("重启前这条回复的提交结果未能确认，未自动重复执行。请先在 Web 核实；如仍需执行，请重新回复通知。", int(time.time())),
        )
        # Retire success acknowledgements queued by older versions. They must
        # not appear after an upgrade; only rejection/uncertain receipts are sent.
        await self.db.conn.execute(
            """UPDATE web_tg_reply_inbox SET response_sent=1, response_text='', updated_at=?
               WHERE state='submitted' AND response_sent=0""",
            (int(time.time()),),
        )
        await self.db.conn.commit()
        self._worker = asyncio.create_task(self._worker_loop(), name="web-tg-reply-worker")
        self._wake.set()

    async def stop(self) -> None:
        task, self._worker = self._worker, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def message_record(self, owner: int, message_id: int) -> dict[str, Any] | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM web_tg_messages WHERE owner_chat_id=? AND telegram_message_id=?",
            (owner, message_id),
        )
        row = await cur.fetchone()
        if row:
            return dict(row)
        # Upgrade compatibility: notifications sent before this feature already
        # carry persisted IDs. Never infer a target from a title or latest chat.
        cur = await self.db.conn.execute(
            """SELECT runs.owner_chat_id, runs.conversation_uuid, runs.root_turn_uuid,
                      outbox.created_at, 'notification' AS role
               FROM web_tg_notification_outbox AS outbox
               JOIN web_tg_notification_runs AS runs USING(root_turn_uuid)
               JOIN json_each(outbox.telegram_message_ids_json) AS ids
               WHERE runs.owner_chat_id=? AND CAST(ids.value AS INTEGER)=? LIMIT 1""",
            (owner, message_id),
        )
        row = await cur.fetchone()
        return {**dict(row), "telegram_message_id": message_id} if row else None

    async def match_reply(self, message: Message) -> dict[str, Any] | None:
        if not reply_candidate(message):
            return None
        return await self.message_record(private_owner(message), int(message.reply_to_message.message_id))

    async def _conversation(self, owner: int, conversation_uuid: str) -> tuple[dict[str, Any] | None, str]:
        if not self.config.web.enabled or owner not in self.config.telegram.whitelist_ids:
            return None, "当前不能通过 Telegram 继续此会话。"
        cur = await self.db.conn.execute(
            "SELECT * FROM web_conversations WHERE conversation_uuid=? AND owner_chat_id=?",
            (conversation_uuid, owner),
        )
        row = await cur.fetchone()
        if not row:
            return None, "原会话已删除或不可访问，未创建新会话。"
        if int(row["archived_at"] or 0):
            return None, "原会话已归档，请前往 Web 处理。"
        return dict(row), ""

    async def handle_reply(self, message: Message, record: dict[str, Any]) -> None:
        if (
            not reply_candidate(message)
            or private_owner(message) != int(record.get("owner_chat_id") or 0)
            or int(message.reply_to_message.message_id) != int(record.get("telegram_message_id") or 0)
        ):
            return
        if private_owner(message) not in self.config.telegram.whitelist_ids:
            return
        if not self.config.web.enabled:
            await send_rich(self.bot, private_owner(message), "Web 服务已关闭，暂不能继续原会话。", reply_to_message_id=int(message.message_id))
            return
        # Media is not silently reduced to its caption; this first ingress is text-only.
        media = any(getattr(message, name, None) for name in ("photo", "document", "voice", "audio", "video", "video_note", "sticker", "animation"))
        text = str(getattr(message, "text", None) or "").strip()
        error = ""
        if media or not text:
            error = "目前请用文字回复通知；图片、语音和文件请在 Web 会话中发送。"
        elif int(record.get("created_at") or 0) < int(time.time()) - _BINDING_TTL_S:
            error = "这条通知已超过 90 天，请在 Web 会话中继续。"
        now = int(time.time())
        await self.db.conn.execute(
            """INSERT OR IGNORE INTO web_tg_reply_inbox
               (owner_chat_id, telegram_message_id, reply_to_message_id, conversation_uuid,
                text, state, response_text, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (private_owner(message), int(message.message_id), int(message.reply_to_message.message_id),
             str(record["conversation_uuid"]), "" if error else text, "rejected" if error else "pending", error, now, now),
        )
        await self.db.conn.commit()
        self._wake.set()

    @staticmethod
    async def _answer_callback(query: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
        with contextlib.suppress(TelegramBadRequest):
            await query.answer(text, show_alert=alert)

    async def handle_callback(self, query: CallbackQuery) -> None:
        message = query.message
        owner = private_owner(message)
        if query.data != REPLY_CALLBACK or not owner or int(query.from_user.id) != owner:
            await self._answer_callback(query, "无权处理此通知", alert=True)
            return
        record = await self.message_record(owner, int(message.message_id))
        if not record or int(record.get("created_at") or 0) < int(time.time()) - _BINDING_TTL_S:
            await self._answer_callback(query, "通知已失效，请前往 Web", alert=True)
            return
        row, error = await self._conversation(owner, str(record["conversation_uuid"]))
        if error or row is None:
            await self._answer_callback(query, error, alert=True)
            return
        # Compatibility for buttons on already-delivered messages. A transient
        # callback hint is enough; never send another bound prompt into the chat.
        await self._answer_callback(query, "请直接回复这条结果消息继续。")

    async def process_one(self) -> bool:
        cur = await self.db.conn.execute("SELECT * FROM web_tg_reply_inbox WHERE state='pending' ORDER BY id LIMIT 1")
        row = await cur.fetchone()
        if row is None:
            return False
        item = dict(row)
        claim = await self.db.conn.execute(
            "UPDATE web_tg_reply_inbox SET state='dispatching', updated_at=? WHERE id=? AND state='pending'",
            (int(time.time()), item["id"]),
        )
        await self.db.conn.commit()
        if int(claim.rowcount or 0) != 1:
            return True
        owner = int(item["owner_chat_id"])
        state, root = "rejected", ""
        try:
            conversation, error = await self._conversation(owner, str(item["conversation_uuid"]))
            if not error and conversation is not None:
                result = await self.submit(conversation, str(item["text"]), submission_id=int(item["id"]))
                if result.get("ok"):
                    state = "submitted"
                    root = str(result.get("rootTurnUuid") or "")
                    error = ""  # Success is silent; the actual result returns via Telegram.
                else:
                    labels = {"busy": "会话正在压缩上下文，请稍后重新回复。", "conversation_unavailable": "原会话已删除或归档，请前往 Web。"}
                    error = labels.get(str(result.get("error") or ""), "回复未提交，请稍后重新回复或前往 Web。")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not retry the side effect. The ordinary receipt delivery may retry.
            state = "uncertain"
            error = "这条回复的提交结果未能确认，未自动重复执行。请先在 Web 核实；如仍需执行，请重新回复通知。"
            log.exception("提交 TG 会话回复失败", inbox_id=item["id"])
        await self.db.conn.execute(
            "UPDATE web_tg_reply_inbox SET state=?, text='', root_turn_uuid=?, response_text=?, response_sent=?, updated_at=? WHERE id=?",
            (state, root, error, 1 if state == "submitted" else 0, int(time.time()), item["id"]),
        )
        await self.db.conn.commit()
        return True

    async def deliver_receipt(self) -> bool:
        now = int(time.time())
        cur = await self.db.conn.execute(
            """SELECT * FROM web_tg_reply_inbox WHERE state IN ('rejected', 'uncertain')
               AND response_sent=0 AND response_text<>'' AND response_after<=? ORDER BY id LIMIT 1""", (now,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        item = dict(row)
        try:
            if int(item["owner_chat_id"]) not in self.config.telegram.whitelist_ids:
                raise PermissionError("owner_not_whitelisted")
            receipt = await send_rich(
                self.bot, int(item["owner_chat_id"]), html.escape(str(item["response_text"])),
                reply_to_message_id=int(item["telegram_message_id"]),
            )
            await bind_message(self.db, int(item["owner_chat_id"]), int(receipt.message_id), str(item["conversation_uuid"]), str(item["root_turn_uuid"]), role="receipt")
            await self.db.conn.execute("UPDATE web_tg_reply_inbox SET response_sent=1, updated_at=? WHERE id=?", (now, item["id"]))
            await self.db.conn.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            attempts = int(item["response_attempts"] or 0) + 1
            await self.db.conn.execute(
                "UPDATE web_tg_reply_inbox SET response_attempts=?, response_after=?, response_sent=?, updated_at=? WHERE id=?",
                (attempts, now + min(300, 2 ** attempts), -1 if attempts >= 5 else 0, now, item["id"]),
            )
            await self.db.conn.commit()
        return True

    async def _worker_loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                worked = await self.process_one()
                worked = await self.deliver_receipt() or worked
                if not worked:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TG 会话回复 worker 异常")
                await asyncio.sleep(1)
