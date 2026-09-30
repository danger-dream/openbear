"""Display-only tool-input activity; never submits or stores partial arguments."""
from __future__ import annotations

import time

from app.llm.events import StreamEvent


class ToolInputTracker:
    """Count UTF-8 argument bytes and publish bounded, throttled metadata only."""

    def __init__(self, *, interval: float = 1.0) -> None:
        self.interval = interval
        self._calls: dict[str, dict] = {}
        self._started = time.monotonic()
        self._started_at_ms = 0
        self._last_emitted: float | None = None

    def update(
        self, key: str, *, name: str = "", delta: str = "",
        arguments: str | None = None, done: bool = False,
    ) -> StreamEvent | None:
        now = time.monotonic()
        is_new = key not in self._calls
        if not self._calls:
            self._started = now
            self._started_at_ms = int(time.time() * 1000)
        call = self._calls.setdefault(key, {"name": "", "bytes": 0, "done": False})
        if name:
            call["name"] = str(name)[:80]
        # done/terminal frames contain full snapshots. Replace, never add them
        # to the already counted deltas (Responses often sends both).
        if arguments is not None:
            call["bytes"] = len(arguments.encode("utf-8", errors="replace"))
        elif delta:
            call["bytes"] += len(delta.encode("utf-8", errors="replace"))
        call["done"] = done
        if not (is_new or done) and self._last_emitted is not None and now - self._last_emitted < self.interval:
            return None
        self._last_emitted = now
        names = list(dict.fromkeys(c["name"] for c in self._calls.values() if c["name"]))
        return StreamEvent(kind="tool_input", details={
            "toolNames": names[:3],
            "callCount": len(self._calls),
            "receivedBytes": sum(c["bytes"] for c in self._calls.values()),
            "startedAtMs": self._started_at_ms,
            "updatedAtMs": int(time.time() * 1000),
            "elapsedMs": max(0, int((now - self._started) * 1000)),
            "phase": "ready" if all(c["done"] for c in self._calls.values()) else "generating",
        })


def tool_input_status(progress: dict) -> str:
    names = progress.get("toolNames") or []
    if progress.get("phase") == "ready":
        label = "参数已接收，等待模型结束"
    elif names == ["Write"]:
        label = "正在生成文件内容"
    elif names and all(name in {"Edit", "EditBatch"} for name in names):
        label = "正在生成修改内容"
    else:
        label = "正在生成工具参数"
    return f"{label} · {', '.join(names)}" if names else label
