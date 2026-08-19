"""白名单中间件 —— 仅放行配置用户的私聊（单人自用）。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import TelegramObject

from app.logging import get_logger

log = get_logger("bot.whitelist")


def _event_chat(event: TelegramObject, data: dict[str, Any]) -> Any:
    chat = data.get("event_chat")
    if chat is not None:
        return chat
    direct = getattr(event, "chat", None)
    if direct is not None:
        return direct
    msg = getattr(event, "message", None)
    return getattr(msg, "chat", None)


class WhitelistMiddleware(BaseMiddleware):
    def __init__(self, allowed_ids: list[int]) -> None:
        self._allowed = set(allowed_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        uid = getattr(user, "id", None)
        if uid is None or uid not in self._allowed:
            log.warning("非白名单访问被忽略", 用户=uid)
            return None

        chat = _event_chat(event, data)
        chat_type = getattr(chat, "type", None)
        if chat_type != ChatType.PRIVATE and str(chat_type) != "private":
            log.warning("非私聊访问被忽略", 用户=uid, 会话=getattr(chat, "id", None), 类型=chat_type)
            return None

        return await handler(event, data)
