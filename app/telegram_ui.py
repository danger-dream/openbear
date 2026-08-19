"""保留中的 Telegram 管理通道 UI 辅助函数。"""
from __future__ import annotations

import asyncio
import html as html_lib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters
from aiogram.types.input_rich_message import InputRichMessage

from app.html_chunking import split_html_chunks
from app.logging import get_logger

log = get_logger("telegram_ui")

# Telegram Bot API Rich Messages support up to 32768 UTF-8 characters in rich text.
USE_RICH_MESSAGES = True
RICH_TEXT_LIMIT = 32_768
RICH_PAGE_LIMIT = 32_000
LEGACY_HTML_LIMIT = 3_800
_PENDING_TTL_SECONDS = 600


@dataclass(slots=True)
class PendingInput:
    action: str
    message_id: int
    data: dict[str, Any]
    created_at: float


_pending_inputs: dict[int, PendingInput] = {}


def set_pending(chat_id: int, *, action: str, message_id: int, data: dict[str, Any]) -> None:
    _pending_inputs[int(chat_id)] = PendingInput(
        action=action,
        message_id=message_id,
        data=data,
        created_at=time.time(),
    )


def get_pending(chat_id: int) -> PendingInput | None:
    item = _pending_inputs.get(int(chat_id))
    if item is None:
        return None
    if time.time() - item.created_at > _PENDING_TTL_SECONDS:
        _pending_inputs.pop(int(chat_id), None)
        return None
    return item


def pop_pending(chat_id: int) -> PendingInput | None:
    item = get_pending(chat_id)
    _pending_inputs.pop(int(chat_id), None)
    return item


def clear_pending(chat_id: int) -> None:
    _pending_inputs.pop(int(chat_id), None)


def btn(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def kb(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


async def delete_trigger_message(message: Message) -> None:
    try:
        await message.bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass


def _rich(html: str) -> InputRichMessage:
    return InputRichMessage(html=_preserve_rich_line_breaks(html or "…"))


def _preserve_rich_line_breaks(value: str) -> str:
    """Rich HTML 会折叠原始换行；在非 pre/code 区域转成 <br>。"""
    if "\n" not in value:
        return value
    out: list[str] = []
    i = 0
    code_depth = 0
    lower = value.lower()
    while i < len(value):
        if lower.startswith("<pre", i) or lower.startswith("<code", i):
            end = value.find(">", i)
            if end == -1:
                out.append(value[i:])
                break
            raw = value[i:end + 1]
            out.append(raw)
            if not raw.rstrip().endswith("/>"):
                code_depth += 1
            i = end + 1
            continue
        if lower.startswith("</pre", i) or lower.startswith("</code", i):
            end = value.find(">", i)
            if end == -1:
                out.append(value[i:])
                break
            out.append(value[i:end + 1])
            code_depth = max(0, code_depth - 1)
            i = end + 1
            continue
        ch = value[i]
        if ch == "\n" and code_depth == 0:
            out.append("<br>")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def split_rich_html(html: str, limit: int = RICH_PAGE_LIMIT) -> list[str]:
    prepared = _preserve_rich_line_breaks(html or "…") if USE_RICH_MESSAGES else (html or "…")
    chunks = split_html_chunks(prepared, limit)
    return chunks or ["…"]


def _rich_html_to_legacy_html(value: str) -> str:
    return re.sub(r"<br\s*/?>", "\n", value or "…", flags=re.IGNORECASE)


async def _sleep_retry(e: TelegramRetryAfter) -> None:
    await asyncio.sleep(e.retry_after + 0.3)


async def _send_legacy_pages(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_notification: bool | None = None,
    plain: bool = False,
    reply_parameters: ReplyParameters | None = None,
) -> Message:
    chunks = split_html_chunks(text or "…", LEGACY_HTML_LIMIT) or [text or "…"]
    last: Message | None = None
    for i, chunk in enumerate(chunks):
        last = await bot.send_message(
            chat_id,
            html_lib.escape(chunk) if plain else chunk,
            parse_mode=None if plain else "HTML",
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
            disable_notification=disable_notification,
            reply_parameters=reply_parameters if i == 0 else None,
        )
    assert last is not None
    return last


async def _edit_legacy_pages(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    plain: bool = False,
) -> Message | bool:
    source = html_lib.escape(text or "…") if plain else (text or "…")
    chunks = split_html_chunks(source, LEGACY_HTML_LIMIT) or [source]
    last: Message | bool = await bot.edit_message_text(
        chunks[0],
        chat_id=chat_id,
        message_id=message_id,
        parse_mode=None if plain else "HTML",
        reply_markup=reply_markup if len(chunks) == 1 else None,
    )
    for i, chunk in enumerate(chunks[1:], start=1):
        last = await bot.send_message(
            chat_id,
            chunk,
            parse_mode=None if plain else "HTML",
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
        )
    return last


async def _send_legacy_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_notification: bool | None = None,
    reply_parameters: ReplyParameters | None = None,
    plain: bool = False,
) -> Message:
    legacy = _rich_html_to_legacy_html(text)
    try:
        return await _send_legacy_pages(
            bot,
            chat_id,
            legacy,
            reply_markup=reply_markup,
            disable_notification=disable_notification,
            plain=plain,
            reply_parameters=reply_parameters,
        )
    except TelegramBadRequest as e:
        log.warning("普通HTML兜底仍失败，改纯文本", 错误=str(e)[:160])
        return await _send_legacy_pages(
            bot,
            chat_id,
            legacy,
            reply_markup=reply_markup,
            disable_notification=disable_notification,
            plain=True,
            reply_parameters=reply_parameters,
        )


async def _edit_legacy_fallback(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    plain: bool = False,
) -> Message | bool:
    legacy = _rich_html_to_legacy_html(text)
    try:
        return await _edit_legacy_pages(
            bot,
            chat_id,
            message_id,
            legacy,
            reply_markup=reply_markup,
            plain=plain,
        )
    except TelegramBadRequest as e:
        log.warning("普通HTML编辑兜底仍失败，改纯文本", 错误=str(e)[:160])
        return await _edit_legacy_pages(
            bot,
            chat_id,
            message_id,
            legacy,
            reply_markup=reply_markup,
            plain=True,
        )


def _looks_parse_error(error: TelegramBadRequest) -> bool:
    msg = str(error).lower()
    return any(token in msg for token in ("parse", "entities", "entity", "tag"))


def _reply_params(reply_to_message_id: int | None) -> ReplyParameters | None:
    if not reply_to_message_id:
        return None
    return ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)


async def send_rich(
    bot: Bot,
    chat_id: int,
    html: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_notification: bool | None = None,
    reply_to_message_id: int | None = None,
) -> Message:
    text = html or "…"
    reply_parameters = _reply_params(reply_to_message_id)
    if not USE_RICH_MESSAGES:
        try:
            return await _send_legacy_pages(
                bot,
                chat_id,
                text,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
                reply_parameters=reply_parameters,
            )
        except TelegramRetryAfter as e:
            await _sleep_retry(e)
            return await _send_legacy_pages(
                bot,
                chat_id,
                text,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
                reply_parameters=reply_parameters,
            )
        except TelegramBadRequest as e:
            log.warning("HTML发送失败，改为纯文本", 错误=str(e)[:160])
            return await _send_legacy_pages(
                bot,
                chat_id,
                text,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
                plain=True,
                reply_parameters=reply_parameters,
            )

    for _ in range(3):
        try:
            return await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=_rich(text),
                reply_markup=reply_markup,
                disable_notification=disable_notification,
                reply_parameters=reply_parameters,
            )
        except TelegramRetryAfter as e:
            await _sleep_retry(e)
        except TelegramBadRequest as e:
            log.warning("Rich发送失败，降级普通HTML", 错误=str(e)[:160])
            return await _send_legacy_fallback(
                bot,
                chat_id,
                text,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
                plain=_looks_parse_error(e),
                reply_parameters=reply_parameters,
            )
        except AttributeError:
            log.warning("当前 Bot 对象不支持 Rich发送，降级普通HTML")
            return await _send_legacy_fallback(
                bot,
                chat_id,
                text,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
                reply_parameters=reply_parameters,
            )
    return await _send_legacy_fallback(
        bot,
        chat_id,
        text,
        reply_markup=reply_markup,
        plain=True,
        reply_parameters=reply_parameters,
    )


async def send_rich_pages(
    bot: Bot,
    chat_id: int,
    html: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> list[Message]:
    messages: list[Message] = []
    chunks = split_rich_html(html or "…")
    for i, chunk in enumerate(chunks):
        messages.append(
            await send_rich(
                bot,
                chat_id,
                chunk,
                reply_markup=reply_markup if i == len(chunks) - 1 else None,
            )
        )
    return messages


async def answer_rich(
    message: Message,
    html: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> list[Message]:
    return await send_rich_pages(message.bot, message.chat.id, html, reply_markup=reply_markup)


async def edit_rich(
    bot: Bot,
    chat_id: int,
    message_id: int,
    html: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | bool:
    text = html or "…"
    if not USE_RICH_MESSAGES:
        try:
            return await _edit_legacy_pages(bot, chat_id, message_id, text, reply_markup=reply_markup)
        except TelegramRetryAfter as e:
            await _sleep_retry(e)
            return await _edit_legacy_pages(bot, chat_id, message_id, text, reply_markup=reply_markup)
        except TelegramBadRequest as e:
            msg = str(e)
            if "not modified" in msg.lower():
                return True
            log.warning("HTML编辑失败，改为纯文本", 错误=msg[:160])
            return await _edit_legacy_pages(bot, chat_id, message_id, text, plain=True, reply_markup=reply_markup)

    for _ in range(3):
        try:
            return await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                rich_message=_rich(text),
                reply_markup=reply_markup,
            )
        except TelegramRetryAfter as e:
            await _sleep_retry(e)
        except TelegramBadRequest as e:
            msg = str(e)
            if "not modified" in msg.lower():
                return True
            log.warning("Rich编辑失败，降级普通HTML", 错误=msg[:160])
            return await _edit_legacy_fallback(
                bot,
                chat_id,
                message_id,
                text,
                plain=_looks_parse_error(e),
                reply_markup=reply_markup,
            )
    return await _edit_legacy_fallback(bot, chat_id, message_id, text, plain=True, reply_markup=reply_markup)
