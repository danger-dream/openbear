"""Telegram notification and reply transport for unified UserInteraction records.

The canonical lifecycle and authorization decision belong to InteractionService.
This module stores only Telegram delivery metadata and non-sensitive local drafts.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.logging import get_logger
from app.telegram_ui import edit_rich, send_rich

log = get_logger("interaction_telegram")

_MAX_DELIVERY_ATTEMPTS = 5
_TZ = timezone(timedelta(hours=8))
_TERMINAL = {"answered", "cancelled", "timeout", "interrupted"}


@dataclass(slots=True)
class InteractionDelivery:
    id: int
    interaction_id: str
    owner_chat_id: int
    event_type: str
    question_index: int
    draft_version: int
    attempts: int


def _now() -> int:
    return int(time.time())


def _interaction_id(item: dict[str, Any]) -> str:
    return str(item.get("interactionId") or item.get("confirmationId") or "").strip()


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _integer(value: Any, default: int = 0) -> int:
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def _trim(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _status(item: dict[str, Any] | None) -> str:
    return str((item or {}).get("status") or "").strip().lower()


def _expired(item: dict[str, Any]) -> bool:
    expires = int(item.get("expiresAtMs") or 0)
    return bool(expires and expires <= int(time.time() * 1000))


def _pending(item: dict[str, Any] | None) -> bool:
    return bool(item) and _status(item) == "pending" and not _expired(item or {})


def _option(option: Any, index: int) -> tuple[str, str]:
    if isinstance(option, dict):
        label = str(option.get("label") or option.get("value") or f"选项 {index + 1}")
        value = str(option.get("value") or option.get("label") or index)
        return label, value
    text = str(option)
    return text, text


def _options(item: dict[str, Any]) -> list[tuple[str, str]]:
    raw = item.get("options") if isinstance(item.get("options"), list) else []
    return [_option(value, index) for index, value in enumerate(raw)]


def _questions(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("questions") if isinstance(item.get("questions"), list) else []
    return [dict(value) for value in raw if isinstance(value, dict)]


def _question_options(question: dict[str, Any]) -> list[tuple[str, str]]:
    raw = question.get("options") if isinstance(question.get("options"), list) else []
    return [_option(value, index) for index, value in enumerate(raw)]


def _private_message(message: Any) -> bool:
    chat = getattr(message, "chat", None)
    chat_type = getattr(chat, "type", None)
    return chat_type == ChatType.PRIVATE or str(chat_type) == "private"


def _markup(rows: list[list[InlineKeyboardButton]], *, force_reply: bool = False) -> InlineKeyboardMarkup:
    # Bot API 10.3 permits force_reply on an inline keyboard. aiogram accepts
    # forward-compatible fields even when generated model metadata lags behind.
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row], force_reply=force_reply)


class InteractionTelegram:
    """Restart-safe Telegram delivery plus process-local interaction handling."""

    def __init__(self, config: Any, db: Any, bot: Bot, interactions: Any) -> None:
        self.config = config
        self.db = db
        self.bot = bot
        self.interactions = interactions
        self._worker: asyncio.Task[Any] | None = None
        self._wake = asyncio.Event()
        self._locks: dict[str, asyncio.Lock] = {}

    def apply_config(self, config: Any) -> None:
        self.config = config
        self._wake.set()

    def _settings(self) -> tuple[bool, bool]:
        web = getattr(self.config, "web", None)
        cfg = getattr(web, "interaction_notifications", None)
        if cfg is None and web is not None:
            cfg = getattr(web, "interactionNotifications", None)
        enabled = bool(getattr(cfg, "enabled", True)) if cfg is not None else True
        allow_reply = getattr(cfg, "allow_reply", None) if cfg is not None else None
        if allow_reply is None and cfg is not None:
            allow_reply = getattr(cfg, "allowReply", True)
        return enabled, bool(True if allow_reply is None else allow_reply)

    def _whitelist(self) -> set[int]:
        telegram = getattr(self.config, "telegram", None)
        return {int(value) for value in (getattr(telegram, "whitelist_ids", None) or []) if int(value) > 0}

    def _web_url(self, conversation_uuid: str) -> str:
        web = getattr(self.config, "web", None)
        base = str(getattr(web, "custom_url", "") or "").strip()
        parsed = urlparse(base)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            return ""
        return f"{base.rstrip('/')}/chat?id={quote(str(conversation_uuid or ''), safe='')}"

    def _lock(self, interaction_id: str) -> asyncio.Lock:
        lock = self._locks.get(interaction_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[interaction_id] = lock
        return lock

    async def start(self) -> None:
        now = _now()
        await self.db.conn.execute(
            "UPDATE interaction_tg_outbox SET state='pending', updated_at=? WHERE state='processing'",
            (now,),
        )
        await self.db.conn.commit()
        await self._reconcile_startup()
        self._worker = asyncio.create_task(self._worker_loop(), name="interaction-telegram-worker")
        self._wake.set()

    async def stop(self) -> None:
        task = self._worker
        self._worker = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _reconcile_startup(self) -> None:
        """Invalidate local state whose canonical interaction was interrupted."""
        cur = await self.db.conn.execute(
            """
            SELECT interaction_id, owner_chat_id FROM interaction_tg_drafts
            WHERE active=1
            UNION
            SELECT interaction_id, owner_chat_id FROM interaction_tg_messages
            WHERE active=1
            """
        )
        for row in await cur.fetchall():
            interaction_id = str(row["interaction_id"] or "")
            owner = int(row["owner_chat_id"] or 0)
            if not interaction_id:
                continue
            try:
                item = await self.interactions.get(interaction_id, owner_chat_id=owner)
            except Exception:
                log.exception("检查重启后的 TG 交互状态失败", 交互=interaction_id)
                continue
            if not _pending(item):
                await self._invalidate_local(interaction_id, queue_terminal=True, item=item)

    async def on_interaction(self, event: str, item: dict[str, Any]) -> None:
        """InteractionService lifecycle listener; never propagates TG failures."""
        try:
            interaction_id = _interaction_id(item)
            owner = int(item.get("ownerChatId") or 0)
            if not interaction_id or owner <= 0:
                return
            if event == "created" and _pending(item):
                enabled, _allow_reply = self._settings()
                if enabled and owner in self._whitelist():
                    await self._queue(
                        interaction_id,
                        owner,
                        "created",
                        key="created",
                        deliver_after=_now(),
                    )
            elif event == "resolved" or _status(item) in _TERMINAL:
                await self._invalidate_local(interaction_id, queue_terminal=True, item=item)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not include the item: it may contain sensitive prompt/result data.
            log.exception("记录 UserInteraction Telegram 生命周期失败", 事件=str(event)[:40])

    async def _queue(
        self,
        interaction_id: str,
        owner: int,
        event_type: str,
        *,
        key: str,
        deliver_after: int,
        question_index: int = -1,
        draft_version: int = 0,
    ) -> None:
        now = _now()
        await self.db.conn.execute(
            """
            INSERT OR IGNORE INTO interaction_tg_outbox (
              delivery_key, interaction_id, owner_chat_id, event_type,
              question_index, draft_version, state, deliver_after, attempts,
              telegram_message_id, last_error, created_at, updated_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 0, 0, '', ?, ?, 0)
            """,
            (
                f"{interaction_id}:{key}",
                interaction_id,
                int(owner),
                event_type,
                int(question_index),
                int(draft_version),
                int(deliver_after),
                now,
                now,
            ),
        )
        await self.db.conn.commit()
        self._wake.set()

    async def _queue_refresh(self, draft: dict[str, Any]) -> None:
        version = int(draft.get("draft_version") or 0)
        await self._queue(
            str(draft["interaction_id"]),
            int(draft["owner_chat_id"]),
            "refresh",
            key=f"refresh:{version}",
            deliver_after=_now(),
            draft_version=version,
        )

    async def _queue_view(self, draft: dict[str, Any], view: str, question_index: int = -1) -> None:
        version = int(draft.get("draft_version") or 0)
        await self._queue(
            str(draft["interaction_id"]),
            int(draft["owner_chat_id"]),
            view,
            key=f"{view}:{question_index}:{version}",
            deliver_after=_now(),
            question_index=question_index,
            draft_version=version,
        )

    async def _invalidate_local(
        self,
        interaction_id: str,
        *,
        queue_terminal: bool,
        item: dict[str, Any] | None,
    ) -> None:
        now = _now()
        await self.db.conn.execute(
            "UPDATE interaction_tg_outbox SET state='cancelled', updated_at=? WHERE interaction_id=? AND event_type!='resolved' AND state IN ('pending','processing')",
            (now, interaction_id),
        )
        await self.db.conn.execute(
            "UPDATE interaction_tg_drafts SET active=0, updated_at=? WHERE interaction_id=? AND active=1",
            (now, interaction_id),
        )
        await self.db.conn.execute(
            "UPDATE interaction_tg_messages SET active=0, updated_at=? WHERE interaction_id=? AND active=1",
            (now, interaction_id),
        )
        await self.db.conn.commit()
        if queue_terminal:
            owner = int((item or {}).get("ownerChatId") or 0)
            if owner <= 0:
                cur = await self.db.conn.execute(
                    "SELECT owner_chat_id FROM interaction_tg_messages WHERE interaction_id=? ORDER BY id LIMIT 1",
                    (interaction_id,),
                )
                row = await cur.fetchone()
                owner = int(row["owner_chat_id"] or 0) if row else 0
            if owner > 0:
                state = _status(item) or "interrupted"
                revision = int((item or {}).get("revision") or 0)
                await self._queue(
                    interaction_id,
                    owner,
                    "resolved",
                    key=f"resolved:{state}:{revision}",
                    deliver_after=_now(),
                )

    async def _worker_loop(self) -> None:
        while True:
            try:
                delivery = await self._claim_due()
                if delivery is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                    except TimeoutError:
                        pass
                    continue
                await self._deliver(delivery)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("UserInteraction Telegram worker 异常")
                await asyncio.sleep(1)

    async def _claim_due(self) -> InteractionDelivery | None:
        now = _now()
        cur = await self.db.conn.execute(
            "SELECT * FROM interaction_tg_outbox WHERE state='pending' AND deliver_after<=? ORDER BY id LIMIT 1",
            (now,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        delivery_id = int(row["id"])
        result = await self.db.conn.execute(
            "UPDATE interaction_tg_outbox SET state='processing', attempts=attempts+1, updated_at=? WHERE id=? AND state='pending'",
            (now, delivery_id),
        )
        await self.db.conn.commit()
        if int(result.rowcount or 0) != 1:
            return None
        return InteractionDelivery(
            id=delivery_id,
            interaction_id=str(row["interaction_id"] or ""),
            owner_chat_id=int(row["owner_chat_id"] or 0),
            event_type=str(row["event_type"] or ""),
            question_index=_integer(row["question_index"], -1),
            draft_version=int(row["draft_version"] or 0),
            attempts=int(row["attempts"] or 0) + 1,
        )

    async def _get_item(self, interaction_id: str, owner: int) -> dict[str, Any] | None:
        return await self.interactions.get(interaction_id, owner_chat_id=owner)

    async def _deliver(self, delivery: InteractionDelivery) -> None:
        try:
            item = await self._get_item(delivery.interaction_id, delivery.owner_chat_id)
            if delivery.event_type == "resolved":
                message_id = await self._deliver_terminal(delivery, item)
            else:
                enabled, allow_reply = self._settings()
                if (
                    not enabled
                    or delivery.owner_chat_id not in self._whitelist()
                    or not _pending(item)
                ):
                    await self._mark(delivery, "cancelled", "interaction_not_deliverable")
                    return
                assert item is not None
                if delivery.event_type == "created":
                    message_id = await self._deliver_created(delivery, item, allow_reply)
                elif not allow_reply or bool(item.get("sensitive")):
                    await self._mark(delivery, "cancelled", "direct_reply_disabled")
                    return
                elif delivery.event_type == "refresh":
                    message_id = await self._deliver_refresh(delivery, item)
                elif delivery.event_type == "question":
                    message_id = await self._deliver_question(delivery, item)
                elif delivery.event_type == "summary":
                    message_id = await self._deliver_summary(delivery, item)
                else:
                    await self._mark(delivery, "failed", "unknown_event_type")
                    return
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            await self._retry(delivery, "rate_limited", max(1, int(exc.retry_after)))
            return
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            await self._mark(delivery, "failed", type(exc).__name__)
            return
        except Exception as exc:
            if delivery.attempts >= _MAX_DELIVERY_ATTEMPTS:
                await self._mark(delivery, "failed", type(exc).__name__)
            else:
                await self._retry(delivery, type(exc).__name__, min(300, 2 ** delivery.attempts))
            return
        now = _now()
        await self.db.conn.execute(
            "UPDATE interaction_tg_outbox SET state='sent', telegram_message_id=?, last_error='', delivered_at=?, updated_at=? WHERE id=? AND state='processing'",
            (int(message_id or 0), now, now, delivery.id),
        )
        await self.db.conn.commit()

    async def _retry(self, delivery: InteractionDelivery, error: str, delay: int) -> None:
        now = _now()
        await self.db.conn.execute(
            "UPDATE interaction_tg_outbox SET state='pending', deliver_after=?, last_error=?, updated_at=? WHERE id=? AND state='processing'",
            (now + max(1, int(delay)), _trim(error, 120), now, delivery.id),
        )
        await self.db.conn.commit()
        self._wake.set()

    async def _mark(self, delivery: InteractionDelivery, state: str, error: str = "") -> None:
        await self.db.conn.execute(
            "UPDATE interaction_tg_outbox SET state=?, last_error=?, updated_at=? WHERE id=? AND state='processing'",
            (state, _trim(error, 120), _now(), delivery.id),
        )
        await self.db.conn.commit()

    async def _message_for_delivery(self, delivery_id: int) -> dict[str, Any] | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM interaction_tg_messages WHERE delivery_id=? LIMIT 1",
            (delivery_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _record_message(
        self,
        delivery: InteractionDelivery,
        message_id: int,
        *,
        role: str,
        revision: int,
        question_index: int = -1,
    ) -> None:
        now = _now()
        await self.db.conn.execute(
            """
            INSERT OR IGNORE INTO interaction_tg_messages (
              delivery_id, interaction_id, owner_chat_id, telegram_message_id,
              role, question_index, interaction_revision, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                delivery.id,
                delivery.interaction_id,
                delivery.owner_chat_id,
                int(message_id),
                role,
                int(question_index),
                int(revision),
                now,
                now,
            ),
        )
        await self.db.conn.commit()

    async def _ensure_draft(self, item: dict[str, Any]) -> dict[str, Any]:
        interaction_id = _interaction_id(item)
        owner = int(item.get("ownerChatId") or 0)
        revision = int(item.get("revision") or 1)
        action = str(item.get("action") or "").strip().lower()
        now = _now()
        for _ in range(5):
            token = secrets.token_urlsafe(9)
            try:
                await self.db.conn.execute(
                    """
                    INSERT OR IGNORE INTO interaction_tg_drafts (
                      interaction_id, owner_chat_id, callback_token,
                      interaction_revision, action, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (interaction_id, owner, token, revision, action, now, now),
                )
                await self.db.conn.commit()
                break
            except Exception:
                continue
        draft = await self._draft(interaction_id)
        if draft is None:
            raise RuntimeError("interaction_draft_unavailable")
        return draft

    async def _draft(self, interaction_id: str) -> dict[str, Any] | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM interaction_tg_drafts WHERE interaction_id=? LIMIT 1",
            (interaction_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        data = dict(row)
        data["selected_values"] = _json(data.get("selected_values_json"), [])
        data["selected_indexes"] = _json(data.get("selected_indexes_json"), [])
        data["questionnaire"] = _json(data.get("questionnaire_json"), {})
        return data

    async def _draft_by_token(self, token: str) -> dict[str, Any] | None:
        cur = await self.db.conn.execute(
            "SELECT interaction_id FROM interaction_tg_drafts WHERE callback_token=? LIMIT 1",
            (token,),
        )
        row = await cur.fetchone()
        return await self._draft(str(row["interaction_id"])) if row else None

    async def _save_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        previous = int(draft.get("draft_version") or 0)
        next_version = previous + 1
        result = await self.db.conn.execute(
            """
            UPDATE interaction_tg_drafts SET
              selected_values_json=?, selected_indexes_json=?, text_value=?,
              confirm_decision=?, questionnaire_json=?, current_question_index=?,
              view=?, draft_version=?, updated_at=?
            WHERE interaction_id=? AND active=1 AND draft_version=?
            """,
            (
                _compact_json(draft.get("selected_values") or []),
                _compact_json(draft.get("selected_indexes") or []),
                str(draft.get("text_value") or ""),
                str(draft.get("confirm_decision") or ""),
                _compact_json(draft.get("questionnaire") or {}),
                _integer(draft.get("current_question_index"), -1),
                str(draft.get("view") or "root"),
                next_version,
                _now(),
                str(draft["interaction_id"]),
                previous,
            ),
        )
        await self.db.conn.commit()
        if int(result.rowcount or 0) != 1:
            raise RuntimeError("interaction_draft_conflict")
        draft["draft_version"] = next_version
        return draft

    def _web_button(self, item: dict[str, Any]) -> InlineKeyboardButton | None:
        url = self._web_url(str(item.get("conversationUuid") or ""))
        return InlineKeyboardButton(text="🌐 打开网页处理", url=url) if url else None

    @staticmethod
    def _callback(token: str, command: str) -> str:
        return f"ui:{token}:{command}"

    def _expiry_line(self, item: dict[str, Any]) -> str:
        expires = int(item.get("expiresAtMs") or 0)
        if not expires:
            return ""
        shown = datetime.fromtimestamp(expires / 1000, tz=_TZ).strftime("%m-%d %H:%M:%S")
        return f"有效期至：<code>{shown}</code>"

    def _root_html(self, item: dict[str, Any], draft: dict[str, Any] | None) -> str:
        if bool(item.get("sensitive")):
            lines = [
                "<b>🔐 有一项敏感交互待处理</b>",
                "",
                "为保护隐私，问题内容和可选项不会发送到 Telegram。请前往 Web 完成。",
            ]
            expiry = self._expiry_line(item)
            if expiry:
                lines.extend(["", expiry])
            return "\n".join(lines)

        action = str(item.get("action") or "").lower()
        labels = {
            "confirm": "需要确认",
            "select": "需要选择",
            "prompt": "需要输入",
            "questionnaire": "需要填写问卷",
        }
        title = _trim(item.get("title") or labels.get(action) or "待处理交互", 200)
        body = _trim(item.get("body") or "", 1600)
        conversation = _trim(item.get("conversationTitle") or "", 160)
        lines = [f"<b>🔔 {html.escape(title)}</b>"]
        if conversation:
            lines.extend(["", f"会话：{html.escape(conversation)}"])
        if body:
            lines.extend(["", html.escape(body)])
        expiry = self._expiry_line(item)
        if expiry:
            lines.extend(["", expiry])
        if draft:
            selected = list(draft.get("selected_values") or [])
            decision = str(draft.get("confirm_decision") or "")
            text_value = str(draft.get("text_value") or "")
            if decision:
                lines.append(f"当前决定：<b>{'确认' if decision == 'confirm' else '拒绝'}</b>")
            elif selected:
                labels_by_value = {value: label for label, value in _options(item)}
                shown = "、".join(html.escape(_trim(labels_by_value.get(value, value), 80)) for value in selected)
                lines.append(f"已选：{shown}")
            if text_value:
                preview = html.escape(_trim(text_value, 600), quote=False)
                lines.extend([
                    f"✍️ 已保存文字（{len(text_value)} 字，完整原文将在最终提交时逐字保留）",
                    f"<blockquote>{preview}</blockquote>",
                ])
            if action == "questionnaire":
                questions = _questions(item)
                lines.append(f"问卷：共 {len(questions)} 题；逐题保存，最后统一提交。")
            else:
                lines.extend(["", "可直接回复本消息填写文字；点击最终提交后才会结束交互。"])
                if bool(item.get("requiresAuthorization")):
                    lines.append("⚠️ 这是授权型选择：有文字时只提交意见，不执行原操作。")
        return "\n".join(lines)

    def _root_keyboard(
        self,
        item: dict[str, Any],
        draft: dict[str, Any] | None,
        *,
        allow_reply: bool,
    ) -> InlineKeyboardMarkup | None:
        rows: list[list[InlineKeyboardButton]] = []
        action = str(item.get("action") or "").lower()
        if draft and allow_reply and not bool(item.get("sensitive")):
            token = str(draft["callback_token"])
            if action == "confirm":
                current = str(draft.get("confirm_decision") or "")
                rows.append([
                    InlineKeyboardButton(text=("✓ " if current == "confirm" else "") + "确认", callback_data=self._callback(token, "sd:c")),
                    InlineKeyboardButton(text=("✓ " if current == "reject" else "") + "拒绝", callback_data=self._callback(token, "sd:r")),
                ])
            elif action == "select":
                selected = set(draft.get("selected_values") or [])
                for index, (label, value) in enumerate(_options(item)):
                    rows.append([InlineKeyboardButton(
                        text=("✓ " if value in selected else "") + _trim(label, 48),
                        callback_data=self._callback(token, f"so:{index}"),
                    )])
            elif action == "questionnaire":
                questions = _questions(item)
                if questions:
                    rows.append([InlineKeyboardButton(text="开始 / 继续在 TG 作答", callback_data=self._callback(token, "start"))])
            if action != "questionnaire":
                rows.append([InlineKeyboardButton(text="🧹 清除草稿", callback_data=self._callback(token, "clear"))])
                has_text = bool(str(draft.get("text_value") or "").strip())
                authorization_feedback = bool(item.get("requiresAuthorization")) and has_text
                confirm_feedback = action == "confirm" and has_text
                submit_text = "提交意见（不执行原操作）" if authorization_feedback or confirm_feedback else "✅ 最终提交"
                rows.append([InlineKeyboardButton(text=submit_text, callback_data=self._callback(token, "submit"))])
            rows.append([InlineKeyboardButton(text="放弃回答", callback_data=self._callback(token, "cancel"))])
        web_button = self._web_button(item)
        if web_button:
            rows.append([web_button])
        if not rows:
            return None
        force = bool(draft and allow_reply and action != "questionnaire" and not item.get("sensitive"))
        return _markup(rows, force_reply=force)

    async def _deliver_created(self, delivery: InteractionDelivery, item: dict[str, Any], allow_reply: bool) -> int:
        existing = await self._message_for_delivery(delivery.id)
        if existing:
            return int(existing["telegram_message_id"] or 0)
        draft: dict[str, Any] | None = None
        if allow_reply and not bool(item.get("sensitive")):
            draft = await self._ensure_draft(item)
        message = await send_rich(
            self.bot,
            delivery.owner_chat_id,
            self._root_html(item, draft),
            reply_markup=self._root_keyboard(item, draft, allow_reply=allow_reply),
            disable_notification=False,
        )
        message_id = int(message.message_id)
        await self._record_message(
            delivery,
            message_id,
            role="root",
            revision=int(item.get("revision") or 1),
        )
        return message_id

    def _question_answer(self, draft: dict[str, Any], question: dict[str, Any]) -> dict[str, Any]:
        qid = str(question.get("id") or "")
        answers = draft.get("questionnaire") if isinstance(draft.get("questionnaire"), dict) else {}
        answer = answers.get(qid) if isinstance(answers.get(qid), dict) else {}
        return {"selectedValues": list(answer.get("selectedValues") or []), "text": str(answer.get("text") or "")}

    def _question_html(self, item: dict[str, Any], draft: dict[str, Any], index: int) -> str:
        questions = _questions(item)
        question = questions[index]
        answer = self._question_answer(draft, question)
        qtext = _trim(question.get("question") or f"问题 {index + 1}", 1200)
        desc = _trim(question.get("description") or "", 800)
        lines = [f"<b>📝 问卷 {index + 1}/{len(questions)}</b>", "", html.escape(qtext)]
        if desc:
            lines.extend(["", html.escape(desc)])
        lines.append("必答" if bool(question.get("required", True)) else "选答")
        selected = list(answer.get("selectedValues") or [])
        if selected:
            labels = {value: label for label, value in _question_options(question)}
            shown = "、".join(html.escape(_trim(labels.get(value, value), 80)) for value in selected)
            lines.append(f"已选：{shown}")
        text_value = str(answer.get("text") or "")
        if text_value:
            preview = html.escape(_trim(text_value, 600), quote=False)
            lines.extend([
                f"✍️ 已保存本题文字（{len(text_value)} 字，完整原文将在最终提交时逐字保留）",
                f"<blockquote>{preview}</blockquote>",
            ])
        lines.extend(["", "可直接回复本题消息填写或替换文字；选项与原文会一同提交。"])
        return "\n".join(lines)

    def _question_keyboard(self, item: dict[str, Any], draft: dict[str, Any], index: int) -> InlineKeyboardMarkup:
        questions = _questions(item)
        question = questions[index]
        token = str(draft["callback_token"])
        answer = self._question_answer(draft, question)
        selected = set(answer.get("selectedValues") or [])
        rows: list[list[InlineKeyboardButton]] = []
        if str(question.get("type") or "choice") == "choice":
            for option_index, (label, value) in enumerate(_question_options(question)):
                rows.append([InlineKeyboardButton(
                    text=("✓ " if value in selected else "") + _trim(label, 48),
                    callback_data=self._callback(token, f"q:{index}:o:{option_index}"),
                )])
        rows.append([InlineKeyboardButton(text="🧹 清除本题", callback_data=self._callback(token, f"q:{index}:clear"))])
        nav: list[InlineKeyboardButton] = []
        if index > 0:
            nav.append(InlineKeyboardButton(text="◀ 上一题", callback_data=self._callback(token, f"q:{index}:prev")))
        nav.append(InlineKeyboardButton(
            text="查看汇总" if index == len(questions) - 1 else "下一题 ▶",
            callback_data=self._callback(token, f"q:{index}:next"),
        ))
        rows.append(nav)
        rows.append([InlineKeyboardButton(text="放弃回答", callback_data=self._callback(token, "cancel"))])
        web_button = self._web_button(item)
        if web_button:
            rows.append([web_button])
        return _markup(rows, force_reply=True)

    def _summary_html(self, item: dict[str, Any], draft: dict[str, Any]) -> str:
        questions = _questions(item)
        lines = ["<b>📋 问卷回答汇总</b>", ""]
        preview_limit = max(80, min(400, 8_000 // max(1, len(questions))))
        for index, question in enumerate(questions):
            answer = self._question_answer(draft, question)
            selected = list(answer.get("selectedValues") or [])
            text_value = str(answer.get("text") or "")
            labels = {value: label for label, value in _question_options(question)}
            lines.append(f"{index + 1}. {html.escape(_trim(question.get('question') or '', 80))}")
            if selected:
                shown = "、".join(html.escape(_trim(labels.get(value, value), 50)) for value in selected)
                lines.append(f"   已选：{shown}")
            if text_value:
                preview = html.escape(_trim(text_value, preview_limit), quote=False)
                lines.append(f"   原文（{len(text_value)} 字，提交时保留完整内容）：")
                lines.append(f"<blockquote>{preview}</blockquote>")
            if not selected and not text_value:
                lines.append("   未填写")
        lines.extend(["", "可返回任一题修改；只有点击最终提交才会结束交互。"])
        return "\n".join(lines)

    def _summary_keyboard(
        self,
        item: dict[str, Any],
        draft: dict[str, Any],
        *,
        force_reply: bool = False,
    ) -> InlineKeyboardMarkup:
        token = str(draft["callback_token"])
        rows = [
            [InlineKeyboardButton(text=f"修改第 {index + 1} 题", callback_data=self._callback(token, f"edit:{index}"))]
            for index, _question in enumerate(_questions(item))
        ]
        rows.append([InlineKeyboardButton(text="✅ 最终提交问卷", callback_data=self._callback(token, "qsubmit"))])
        rows.append([InlineKeyboardButton(text="放弃回答", callback_data=self._callback(token, "cancel"))])
        web_button = self._web_button(item)
        if web_button:
            rows.append([web_button])
        return _markup(rows, force_reply=force_reply)

    async def _draft_for_delivery(self, delivery: InteractionDelivery) -> dict[str, Any] | None:
        draft = await self._draft(delivery.interaction_id)
        if (
            not draft
            or not int(draft.get("active") or 0)
            or int(draft.get("draft_version") or 0) != delivery.draft_version
        ):
            return None
        return draft

    async def _deliver_question(self, delivery: InteractionDelivery, item: dict[str, Any]) -> int:
        existing = await self._message_for_delivery(delivery.id)
        if existing:
            return int(existing["telegram_message_id"] or 0)
        draft = await self._draft_for_delivery(delivery)
        questions = _questions(item)
        index = delivery.question_index
        if not draft or index < 0 or index >= len(questions) or _integer(draft.get("current_question_index"), -1) != index:
            await self._mark(delivery, "cancelled", "stale_question")
            return 0
        message = await send_rich(
            self.bot,
            delivery.owner_chat_id,
            self._question_html(item, draft, index),
            reply_markup=self._question_keyboard(item, draft, index),
        )
        message_id = int(message.message_id)
        await self._record_message(
            delivery,
            message_id,
            role="question",
            question_index=index,
            revision=int(item.get("revision") or 1),
        )
        return message_id

    async def _deliver_summary(self, delivery: InteractionDelivery, item: dict[str, Any]) -> int:
        existing = await self._message_for_delivery(delivery.id)
        if existing:
            return int(existing["telegram_message_id"] or 0)
        draft = await self._draft_for_delivery(delivery)
        if not draft or str(draft.get("view") or "") != "summary":
            await self._mark(delivery, "cancelled", "stale_summary")
            return 0
        message = await send_rich(
            self.bot,
            delivery.owner_chat_id,
            self._summary_html(item, draft),
            reply_markup=self._summary_keyboard(item, draft),
        )
        message_id = int(message.message_id)
        await self._record_message(
            delivery,
            message_id,
            role="summary",
            revision=int(item.get("revision") or 1),
        )
        return message_id

    async def _deliver_refresh(self, delivery: InteractionDelivery, item: dict[str, Any]) -> int:
        draft = await self._draft_for_delivery(delivery)
        if not draft:
            await self._mark(delivery, "cancelled", "stale_refresh")
            return 0
        cur = await self.db.conn.execute(
            "SELECT * FROM interaction_tg_messages WHERE interaction_id=? AND active=1 ORDER BY id",
            (delivery.interaction_id,),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        last = 0
        for row in rows:
            role = str(row.get("role") or "")
            message_id = int(row.get("telegram_message_id") or 0)
            action = str(item.get("action") or "").lower()
            view = str(draft.get("view") or "")
            current_index = _integer(draft.get("current_question_index"), -1)
            if action != "questionnaire" and role == "root":
                body = self._root_html(item, draft)
                markup = self._root_keyboard(item, draft, allow_reply=True)
            elif action == "questionnaire" and role == "question":
                index = _integer(row.get("question_index"), -1)
                if index != current_index:
                    continue
                if view == "summary":
                    # Keep the message bound to its original question ID while
                    # replacing only its presentation with the final summary.
                    body = self._summary_html(item, draft)
                    # Bot API does not allow force_reply to change during an
                    # inline-keyboard edit. This is still the same question message.
                    markup = self._summary_keyboard(item, draft, force_reply=True)
                else:
                    body = self._question_html(item, draft, index)
                    markup = self._question_keyboard(item, draft, index)
            elif action == "questionnaire" and role == "summary" and view == "summary":
                # Compatibility for an already queued summary from an older process.
                body = self._summary_html(item, draft)
                markup = self._summary_keyboard(item, draft)
            else:
                continue
            try:
                await edit_rich(self.bot, delivery.owner_chat_id, message_id, body, reply_markup=markup)
            except (TelegramBadRequest, TelegramForbiddenError):
                # A deleted/inaccessible old message must not prevent newer prompts
                # from being refreshed. Transient/network errors still escape and retry.
                continue
            last = message_id
        return last

    async def _deliver_terminal(self, delivery: InteractionDelivery, item: dict[str, Any] | None) -> int:
        # Every registered prompt belongs to this interaction, including older
        # questionnaire questions. A completed interaction must leave no TG panel.
        cur = await self.db.conn.execute(
            "SELECT telegram_message_id FROM interaction_tg_messages WHERE interaction_id=? ORDER BY id",
            (delivery.interaction_id,),
        )
        last = 0
        for row in await cur.fetchall():
            message_id = int(row["telegram_message_id"] or 0)
            try:
                await self.bot.delete_message(delivery.owner_chat_id, message_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                # Already deleted or no longer deletable: continue cleaning the
                # other messages. Transient failures escape for outbox retry.
                continue
            last = message_id
        return last

    async def _message_record(self, owner: int, message_id: int) -> dict[str, Any] | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM interaction_tg_messages WHERE owner_chat_id=? AND telegram_message_id=? LIMIT 1",
            (int(owner), int(message_id)),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def match_reply(self, message: Message) -> dict[str, Any] | None:
        text = message.text if message.text is not None else message.caption
        if text is None or str(text).startswith("/") or not _private_message(message):
            return None
        user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        owner = int(getattr(user, "id", 0) or 0)
        if owner <= 0 or owner != int(getattr(chat, "id", 0) or 0):
            return None
        reply = getattr(message, "reply_to_message", None)
        reply_id = int(getattr(reply, "message_id", 0) or 0)
        if reply_id <= 0:
            return None
        return await self._message_record(owner, reply_id)

    async def _reply_notice(self, message: Message, body: str) -> None:
        with contextlib.suppress(Exception):
            await send_rich(
                message.bot,
                message.chat.id,
                body,
                reply_to_message_id=message.message_id,
            )

    async def handle_reply(self, message: Message, record: dict[str, Any]) -> None:
        interaction_id = str(record.get("interaction_id") or "")
        owner = int(record.get("owner_chat_id") or 0)
        text = message.text if message.text is not None else (message.caption or "")
        if not interaction_id or owner <= 0:
            return
        async with self._lock(interaction_id):
            item = await self._get_item(interaction_id, owner)
            if not _pending(item):
                await self._reply_notice(message, "该交互已处理、过期或中断，不能再修改。")
                return
            assert item is not None
            if bool(item.get("sensitive")):
                await self._reply_notice(message, "这是敏感交互，请在 Web 页面中填写。Telegram 未保存你的回复。")
                return
            if (
                int(getattr(message.from_user, "id", 0) or 0) != owner
                or int(message.chat.id) != owner
                or int(record.get("interaction_revision") or 0) != int(item.get("revision") or 1)
                or not int(record.get("active") or 0)
            ):
                await self._reply_notice(message, "这条提示已经失效，请使用最新的交互消息。")
                return
            draft = await self._draft(interaction_id)
            if not draft or not int(draft.get("active") or 0):
                await self._reply_notice(message, "这条提示已经失效，请使用最新的交互消息。")
                return
            role = str(record.get("role") or "")
            action = str(item.get("action") or "").lower()
            if action == "questionnaire":
                index = _integer(record.get("question_index"), -1)
                questions = _questions(item)
                if role != "question" or index < 0 or index >= len(questions) or index != _integer(draft.get("current_question_index"), -1):
                    await self._reply_notice(message, "这不是当前问卷题目，请回复最新的一题。")
                    return
                question = questions[index]
                answers = draft.get("questionnaire") if isinstance(draft.get("questionnaire"), dict) else {}
                answer = self._question_answer(draft, question)
                answer["text"] = str(text)
                answers[str(question.get("id") or "")] = answer
                draft["questionnaire"] = answers
            elif role == "root" and action in {"confirm", "select", "prompt"}:
                draft["text_value"] = str(text)
            else:
                await self._reply_notice(message, "请按消息中的按钮开始或继续作答。")
                return
            if (
                action == "questionnaire"
                and index == len(questions) - 1
                and self._question_valid(question, answer)
                and self._first_invalid_question(item, draft) is None
            ):
                # The final question message becomes the summary in place. Keep
                # current_question_index and the message's question role so a later
                # reply remains unambiguously attached to this same question.
                draft["view"] = "summary"
            draft = await self._save_draft(draft)
            await self._queue_refresh(draft)
        # Successful replies are acknowledged by editing the registered panel.
        # Error/stale/sensitive paths above still send an explicit recovery notice.

    async def _safe_callback_answer(self, query: CallbackQuery, text: str = "", *, alert: bool = False) -> None:
        with contextlib.suppress(Exception):
            await query.answer(text or None, show_alert=alert)

    async def handle_callback(self, query: CallbackQuery) -> None:
        data = str(query.data or "")
        parts = data.split(":")
        if len(parts) < 3 or parts[0] != "ui":
            await self._safe_callback_answer(query, "请求格式错误", alert=True)
            return
        token = parts[1]
        command = ":".join(parts[2:])
        draft = await self._draft_by_token(token)
        message = query.message
        if not draft or message is None:
            await self._safe_callback_answer(query, "该交互已失效", alert=True)
            return
        owner = int(draft.get("owner_chat_id") or 0)
        chat = getattr(message, "chat", None)
        if (
            not _private_message(message)
            or int(getattr(query.from_user, "id", 0) or 0) != owner
            or int(getattr(chat, "id", 0) or 0) != owner
        ):
            await self._safe_callback_answer(query, "无权处理该交互", alert=True)
            return
        message_id = int(getattr(message, "message_id", 0) or 0)
        record = await self._message_record(owner, message_id)
        if not record or str(record.get("interaction_id") or "") != str(draft.get("interaction_id") or ""):
            await self._safe_callback_answer(query, "消息与交互不匹配", alert=True)
            return

        interaction_id = str(draft["interaction_id"])
        async with self._lock(interaction_id):
            # Token/message lookup happens before the lock so invalid callbacks can
            # fail quickly. Reload both rows inside the lock to serialize concurrent
            # toggles and avoid applying an older draft version after another click.
            current_draft = await self._draft(interaction_id)
            current_record = await self._message_record(owner, message_id)
            if (
                not current_draft
                or str(current_draft.get("callback_token") or "") != token
                or not current_record
                or str(current_record.get("interaction_id") or "") != interaction_id
            ):
                await self._safe_callback_answer(query, "这条按钮已经失效", alert=True)
                return
            draft = current_draft
            record = current_record
            item = await self._get_item(interaction_id, owner)
            enabled, allow_reply = self._settings()
            if not enabled or not allow_reply:
                await self._safe_callback_answer(query, "Telegram 直接作答已关闭，请前往 Web", alert=True)
                return
            if not _pending(item):
                await self._safe_callback_answer(query, "该交互已处理、过期或中断", alert=True)
                if item:
                    await self._invalidate_local(interaction_id, queue_terminal=True, item=item)
                return
            assert item is not None
            if bool(item.get("sensitive")):
                await self._safe_callback_answer(query, "敏感交互只能在 Web 处理", alert=True)
                return
            if (
                not int(draft.get("active") or 0)
                or not int(record.get("active") or 0)
                or int(draft.get("interaction_revision") or 0) != int(item.get("revision") or 1)
                or int(record.get("interaction_revision") or 0) != int(item.get("revision") or 1)
            ):
                await self._safe_callback_answer(query, "这条按钮已经失效", alert=True)
                return

            action = str(item.get("action") or "").lower()
            role = str(record.get("role") or "")
            summary_context = (
                action == "questionnaire"
                and str(draft.get("view") or "") == "summary"
                and (
                    role == "summary"
                    or (
                        role == "question"
                        and _integer(record.get("question_index"), -1)
                        == _integer(draft.get("current_question_index"), -1)
                    )
                )
            )
            if command == "cancel":
                await self._safe_callback_answer(query, "正在放弃…")
                await self._submit_from_callback(
                    query,
                    item,
                    draft,
                    {"revision": int(item.get("revision") or 1), "cancelled": True},
                )
                return
            if command == "start" and action == "questionnaire" and role == "root":
                questions = _questions(item)
                if not questions:
                    await self._safe_callback_answer(query, "问卷没有可填写的问题", alert=True)
                    return
                await self._safe_callback_answer(query, "已打开第 1 题")
                await self._change_view(draft, "question", 0)
                return
            if command.startswith("edit:") and summary_context:
                try:
                    index = int(command.split(":", 1)[1])
                except ValueError:
                    index = -1
                if index < 0 or index >= len(_questions(item)):
                    await self._safe_callback_answer(query, "问题不存在", alert=True)
                    return
                await self._safe_callback_answer(query, f"返回第 {index + 1} 题")
                await self._change_view(draft, "question", index)
                return
            if command == "qsubmit" and summary_context:
                invalid = self._first_invalid_question(item, draft)
                if invalid is not None:
                    await self._safe_callback_answer(query, f"第 {invalid + 1} 题尚未填写", alert=True)
                    return
                await self._safe_callback_answer(query, "正在提交…")
                await self._submit_from_callback(query, item, draft, self._questionnaire_payload(item, draft))
                return
            if (
                command.startswith("q:")
                and action == "questionnaire"
                and role == "question"
                and str(draft.get("view") or "") == "question"
            ):
                await self._handle_question_callback(query, command, item, draft, record)
                return
            if role != "root" or action not in {"confirm", "select", "prompt"}:
                await self._safe_callback_answer(query, "请使用最新的交互消息", alert=True)
                return
            if command.startswith("sd:") and action == "confirm":
                value = command.split(":", 1)[1]
                if value not in {"c", "r"}:
                    await self._safe_callback_answer(query, "未知决定", alert=True)
                    return
                draft["confirm_decision"] = "confirm" if value == "c" else "reject"
                draft = await self._save_draft(draft)
                await self._queue_refresh(draft)
                await self._safe_callback_answer(query, "已暂存决定；请最终提交")
                return
            if command.startswith("so:") and action == "select":
                try:
                    index = int(command.split(":", 1)[1])
                except ValueError:
                    index = -1
                options = _options(item)
                if index < 0 or index >= len(options):
                    await self._safe_callback_answer(query, "选项不存在", alert=True)
                    return
                value = options[index][1]
                selected = list(draft.get("selected_values") or [])
                indexes = list(draft.get("selected_indexes") or [])
                if value in selected:
                    selected = [entry for entry in selected if entry != value]
                    indexes = [entry for entry in indexes if int(entry) != index]
                elif bool(item.get("multiple")):
                    selected.append(value)
                    indexes.append(index)
                else:
                    selected, indexes = [value], [index]
                ordered = sorted(zip(indexes, selected), key=lambda pair: pair[0])
                draft["selected_indexes"] = [pair[0] for pair in ordered]
                draft["selected_values"] = [pair[1] for pair in ordered]
                draft = await self._save_draft(draft)
                await self._queue_refresh(draft)
                await self._safe_callback_answer(query, "已更新选择；请最终提交")
                return
            if command == "clear":
                draft["selected_values"] = []
                draft["selected_indexes"] = []
                draft["text_value"] = ""
                draft["confirm_decision"] = ""
                draft = await self._save_draft(draft)
                await self._queue_refresh(draft)
                await self._safe_callback_answer(query, "草稿已清除")
                return
            if command == "submit":
                payload, error = self._simple_payload(item, draft)
                if error:
                    await self._safe_callback_answer(query, error, alert=True)
                    return
                await self._safe_callback_answer(query, "正在提交…")
                await self._submit_from_callback(query, item, draft, payload)
                return
            await self._safe_callback_answer(query, "未知操作", alert=True)

    async def _change_view(self, draft: dict[str, Any], view: str, index: int) -> None:
        await self.db.conn.execute(
            "UPDATE interaction_tg_messages SET active=0, updated_at=? WHERE interaction_id=? AND role IN ('question','summary') AND active=1",
            (_now(), str(draft["interaction_id"])),
        )
        await self.db.conn.commit()
        draft["view"] = view
        draft["current_question_index"] = index if view == "question" else -1
        draft = await self._save_draft(draft)
        await self._queue_view(draft, view, index if view == "question" else -1)

    async def _handle_question_callback(
        self,
        query: CallbackQuery,
        command: str,
        item: dict[str, Any],
        draft: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        parts = command.split(":")
        if len(parts) < 3:
            await self._safe_callback_answer(query, "请求格式错误", alert=True)
            return
        try:
            index = int(parts[1])
        except ValueError:
            index = -1
        questions = _questions(item)
        if (
            index < 0
            or index >= len(questions)
            or index != _integer(record.get("question_index"), -1)
            or index != _integer(draft.get("current_question_index"), -1)
        ):
            await self._safe_callback_answer(query, "这不是当前题目", alert=True)
            return
        question = questions[index]
        qid = str(question.get("id") or "")
        answers = draft.get("questionnaire") if isinstance(draft.get("questionnaire"), dict) else {}
        answer = self._question_answer(draft, question)
        op = parts[2]
        if op == "o" and len(parts) == 4:
            try:
                option_index = int(parts[3])
            except ValueError:
                option_index = -1
            options = _question_options(question)
            if option_index < 0 or option_index >= len(options):
                await self._safe_callback_answer(query, "选项不存在", alert=True)
                return
            value = options[option_index][1]
            selected = list(answer.get("selectedValues") or [])
            if value in selected:
                selected = [entry for entry in selected if entry != value]
            elif bool(question.get("multiple")):
                selected.append(value)
            else:
                selected = [value]
            order = {option_value: position for position, (_label, option_value) in enumerate(options)}
            answer["selectedValues"] = sorted(selected, key=lambda entry: order.get(entry, 10**9))
            answers[qid] = answer
            draft["questionnaire"] = answers
            draft = await self._save_draft(draft)
            await self._queue_refresh(draft)
            await self._safe_callback_answer(query, "已更新选择")
            return
        if op == "clear":
            answers[qid] = {"selectedValues": [], "text": ""}
            draft["questionnaire"] = answers
            draft = await self._save_draft(draft)
            await self._queue_refresh(draft)
            await self._safe_callback_answer(query, "本题已清除")
            return
        if op == "prev":
            if index <= 0:
                await self._safe_callback_answer(query, "已经是第一题")
                return
            await self._safe_callback_answer(query, f"返回第 {index} 题")
            await self._change_view(draft, "question", index - 1)
            return
        if op == "next":
            if not self._question_valid(question, answer):
                await self._safe_callback_answer(query, "此题必答：请选择或填写文字", alert=True)
                return
            if index == len(questions) - 1:
                await self._safe_callback_answer(query, "正在生成汇总…")
                # Do not send a new menu: transform this question's own panel so
                # its message ID remains bound to the same question for late replies.
                draft["view"] = "summary"
                draft = await self._save_draft(draft)
                await self._queue_refresh(draft)
            else:
                await self._safe_callback_answer(query, f"进入第 {index + 2} 题")
                await self._change_view(draft, "question", index + 1)
            return
        await self._safe_callback_answer(query, "未知操作", alert=True)

    @staticmethod
    def _question_valid(question: dict[str, Any], answer: dict[str, Any]) -> bool:
        if not bool(question.get("required", True)):
            return True
        return bool(list(answer.get("selectedValues") or [])) or bool(str(answer.get("text") or "").strip())

    def _first_invalid_question(self, item: dict[str, Any], draft: dict[str, Any]) -> int | None:
        for index, question in enumerate(_questions(item)):
            if not self._question_valid(question, self._question_answer(draft, question)):
                return index
        return None

    def _questionnaire_payload(self, item: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
        answers = []
        for question in _questions(item):
            answer = self._question_answer(draft, question)
            answers.append({
                "questionId": str(question.get("id") or ""),
                "selectedValues": list(answer.get("selectedValues") or []),
                "text": str(answer.get("text") or ""),
            })
        return {"revision": int(item.get("revision") or 1), "answers": answers}

    def _simple_payload(self, item: dict[str, Any], draft: dict[str, Any]) -> tuple[dict[str, Any], str]:
        action = str(item.get("action") or "").lower()
        revision = int(item.get("revision") or 1)
        text_value = str(draft.get("text_value") or "")
        has_text = bool(text_value.strip())
        if action == "prompt":
            if not has_text:
                return {}, "请先回复提示消息填写内容"
            return {"revision": revision, "value": text_value}, ""
        if action == "select":
            selected_values = list(draft.get("selected_values") or [])
            selected_indexes = [int(value) for value in (draft.get("selected_indexes") or [])]
            if not selected_values and not has_text:
                return {}, "请至少选择一项或填写自己的答案"
            return {
                "revision": revision,
                "selectedValues": selected_values,
                "selectedIndexes": selected_indexes,
                "text": text_value,
            }, ""
        if action == "confirm":
            selected = str(draft.get("confirm_decision") or "")
            if not selected and not has_text:
                return {}, "请选择确认/拒绝，或填写自己的意见"
            decision = "feedback" if has_text else selected
            payload: dict[str, Any] = {
                "revision": revision,
                "decision": decision,
                "text": text_value,
                "confirmed": bool(decision == "confirm" and not has_text),
            }
            # selectedDecision is optional in the canonical protocol. Sending an
            # explicit null for a text-only opinion is invalid; omit it so the
            # service derives "feedback" while preserving a real button choice.
            if selected:
                payload["selectedDecision"] = selected
            return payload, ""
        return {}, "不支持的交互类型"

    async def _submit_from_callback(
        self,
        query: CallbackQuery,
        item: dict[str, Any],
        draft: dict[str, Any],
        answer: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self._submit(item, draft, answer)
        if not bool(result.get("ok")) and int(result.get("statusCode") or 0) != 409:
            message = query.message
            if message is not None:
                with contextlib.suppress(Exception):
                    await send_rich(
                        query.bot,
                        int(message.chat.id),
                        "提交未成功，草稿仍保留。请重试或前往 Web 处理。",
                        reply_to_message_id=int(message.message_id),
                    )
        return result

    async def _submit(self, item: dict[str, Any], draft: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        interaction_id = _interaction_id(item)
        owner = int(item.get("ownerChatId") or 0)
        try:
            result = await self.interactions.submit(interaction_id, owner, answer, source="telegram")
        except Exception as exc:
            log.warning("TG 交互提交失败", 交互=interaction_id, 错误=type(exc).__name__)
            return {"ok": False, "statusCode": 503, "error": "interaction_submit_failed"}
        if bool(result.get("ok")) or int(result.get("statusCode") or 0) == 409:
            latest = await self._get_item(interaction_id, owner)
            await self._invalidate_local(interaction_id, queue_terminal=True, item=latest or item)
        else:
            # Keep the draft for validation/transient errors; do not echo answer text.
            refreshed = await self._draft(interaction_id)
            if refreshed:
                await self._queue_refresh(refreshed)
        return result


__all__ = ["InteractionDelivery", "InteractionTelegram"]
