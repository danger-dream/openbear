"""Only registered Web task notification replies can reach the Agent ingress."""
from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from app.web_telegram_replies import REPLY_CALLBACK, reply_candidate

router = Router(name="web-task-replies")


def transport(svc: Any) -> Any:
    return getattr(getattr(svc, "web_admin", None), "telegram_replies", None)


class RegisteredTaskReply(Filter):
    async def __call__(self, message: Message, svc: Any) -> dict[str, Any] | bool:
        bridge = transport(svc)
        if bridge is None:
            return False
        try:
            record = await bridge.match_reply(message)
        except Exception:
            # A DB outage must not turn a possible conversation reply into an
            # admin-setting value. No unverified instruction is dispatched.
            return {"task_reply_record": {}, "task_reply_lookup_failed": True} if reply_candidate(message) else False
        return {"task_reply_record": record} if record else False


@router.message(RegisteredTaskReply())
async def on_task_reply(message: Message, svc: Any, task_reply_record: dict[str, Any], task_reply_lookup_failed: bool = False) -> None:
    bridge = transport(svc)
    try:
        if task_reply_lookup_failed or bridge is None:
            raise RuntimeError("reply_lookup_unavailable")
        await bridge.handle_reply(message, task_reply_record)
    except Exception:
        with contextlib.suppress(Exception):
            await message.answer("回复暂未确认保存，请稍后重试或前往 Web 核实。")


@router.callback_query(F.data == REPLY_CALLBACK)
async def on_task_reply_callback(query: CallbackQuery, svc: Any) -> None:
    bridge = transport(svc)
    try:
        if bridge is None:
            raise RuntimeError("reply_service_unavailable")
        await bridge.handle_callback(query)
    except Exception:
        with contextlib.suppress(Exception):
            await query.answer("暂时无法继续，请稍后重试或前往 Web", show_alert=True)
