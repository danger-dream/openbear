"""Tell open console pages that a tool changed an object they may be showing.

Only metadata travels: the kind of object, its identity and what happened. A page reloads
the object itself through its usual API.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.logging import get_logger

log = get_logger("assistant")

Listener = Callable[[dict[str, Any]], Awaitable[None]]
_listeners: list[Listener] = []


def on_change(listener: Listener) -> Callable[[], None]:
    _listeners.append(listener)
    return lambda: _listeners.remove(listener) if listener in _listeners else None


async def publish(kind: str, ident: Any = "", action: str = "update", **extra: Any) -> None:
    packet = {"type": "resource-changed", "kind": kind, "id": str(ident or ""), "action": action,
              **{k: v for k, v in extra.items() if v not in (None, "")}}
    for listener in list(_listeners):
        try:
            await listener(packet)
        except Exception as exc:
            log.warning("变更通知发送失败", 错误=str(exc)[:160])
