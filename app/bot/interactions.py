"""Narrow Telegram routes for unified UserInteraction callbacks and replies."""
from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

router = Router(name="user-interactions")


def _transport(svc: Any) -> Any | None:
    web_admin = getattr(svc, "web_admin", None)
    transport = getattr(web_admin, "interaction_telegram", None) if web_admin is not None else None
    # The fallback keeps isolated tests and gradual integration simple; production
    # wiring is svc.web_admin.interaction_telegram.
    return transport or getattr(svc, "interaction_telegram", None)


def _is_reply_lookup_candidate(message: Message) -> bool:
    """Identify the narrow envelope that can reach the interaction lookup."""
    text = message.text if message.text is not None else message.caption
    if text is None or str(text).startswith("/"):
        return False
    chat = getattr(message, "chat", None)
    user = getattr(message, "from_user", None)
    chat_type = getattr(chat, "type", None)
    owner = int(getattr(user, "id", 0) or 0)
    reply = getattr(message, "reply_to_message", None)
    return (
        (str(chat_type) == "private" or getattr(chat_type, "value", None) == "private")
        and owner > 0
        and owner == int(getattr(chat, "id", 0) or 0)
        and int(getattr(reply, "message_id", 0) or 0) > 0
    )


class RegisteredInteractionReply(Filter):
    """Match only a non-command reply to a prompt registered by this transport."""

    async def __call__(self, message: Message, svc: Any) -> dict[str, Any] | bool:
        transport = _transport(svc)
        if transport is None:
            return False
        try:
            record = await transport.match_reply(message)
        except Exception:
            # Once a valid private reply reaches the DB lookup, uncertainty must
            # fail closed: otherwise the broad admin setting handler can consume
            # an interaction answer as a configuration value.
            if _is_reply_lookup_candidate(message):
                return {"interaction_tg_record": {}, "interaction_tg_lookup_failed": True}
            return False
        return {"interaction_tg_record": record} if record else False


@router.callback_query(F.data.startswith("ui:"))
async def on_interaction_callback(query: CallbackQuery, svc: Any) -> None:
    transport = _transport(svc)
    if transport is None:
        with contextlib.suppress(Exception):
            await query.answer("交互服务暂不可用", show_alert=True)
        return
    try:
        await transport.handle_callback(query)
    except Exception:
        # Callback must stop Telegram's progress indicator even when local state is
        # temporarily unavailable. Do not include callback payload in logs/replies.
        with contextlib.suppress(Exception):
            await query.answer("处理失败，请稍后重试或前往 Web", show_alert=True)


@router.message(RegisteredInteractionReply())
async def on_interaction_reply(
    message: Message,
    svc: Any,
    interaction_tg_record: dict[str, Any],
    interaction_tg_lookup_failed: bool = False,
) -> None:
    if interaction_tg_lookup_failed:
        with contextlib.suppress(Exception):
            await message.answer("交互回复暂未保存，请稍后重试或前往 Web。")
        return
    transport = _transport(svc)
    if transport is None:
        return
    try:
        await transport.handle_reply(message, interaction_tg_record)
    except Exception:
        # This route has already positively matched an interaction prompt, so it
        # intentionally consumes the reply rather than leaking into admin editing.
        with contextlib.suppress(Exception):
            await message.answer("交互回复暂未保存，请重试或前往 Web。")


__all__ = ["RegisteredInteractionReply", "on_interaction_reply", "router"]
