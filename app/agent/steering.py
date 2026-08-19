"""运行中插话队列（steering）—— 长任务执行到一半追加指导/提醒给模型。

场景：模型正在执行一个多轮长任务，老大中途想起还有要补充的指导或要改目标，
再发一条消息。这条消息不打断当前轮，而是排进队列；loop 在**轮与轮的边界**
（一轮刚结束、下一次请求模型之前）取出，作为 user 消息插入 convo，模型下一轮即可见。

单用户单 run，进程内队列即可；按 chat_id 隔离。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

_LOCK = threading.Lock()
_QUEUE: dict[int, list[dict[str, Any]]] = {}


def _normalize_item(item: Any, **metadata: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            return None
        base = dict(item)
    else:
        text = str(item or "").strip()
        if not text:
            return None
        base = {"text": text}
    base.update({k: v for k, v in metadata.items() if v is not None and v != ""})
    base["text"] = text
    base.setdefault("id", str(uuid.uuid4()))
    base.setdefault("submittedAtMs", int(time.time() * 1000))
    return base


def enqueue(chat_id: int, text: Any, **metadata: Any) -> dict[str, Any] | None:
    item = _normalize_item(text, **metadata)
    if item is None:
        return None
    with _LOCK:
        _QUEUE.setdefault(chat_id, []).append(item)
    return dict(item)


def pending_items(chat_id: int) -> list[dict[str, Any]]:
    """查看待注入插话队列（不清空），用于 Web composer 队列恢复。"""
    with _LOCK:
        return [dict(item) for item in _QUEUE.get(chat_id, [])]


def drain_items(chat_id: int) -> list[dict[str, Any]]:
    """取出并清空某会话排队的插话记录（按到达顺序）。"""
    with _LOCK:
        items = _QUEUE.pop(chat_id, [])
    return [dict(item) for item in items]


def drain(chat_id: int) -> list[str]:
    """兼容旧调用方：取出队列并只返回文本。"""
    return [str(item.get("text") or "") for item in drain_items(chat_id) if str(item.get("text") or "")]


def has_pending(chat_id: int) -> bool:
    with _LOCK:
        return bool(_QUEUE.get(chat_id))


def clear(chat_id: int) -> None:
    with _LOCK:
        _QUEUE.pop(chat_id, None)
