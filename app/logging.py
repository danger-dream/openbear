"""中文结构化日志：时间 | 级别 | [模块] | 事件 | 键=值 | 键=值。"""
from __future__ import annotations

import logging
import sys
from typing import Any

_LEVELS = {
    "DEBUG": logging.DEBUG, "INFO": logging.INFO,
    "WARNING": logging.WARNING, "ERROR": logging.ERROR,
}


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        base = f"{ts} | {record.levelname:<5} | [{record.name}] | {record.getMessage()}"
        kv = getattr(record, "kv", None)
        if kv:
            pairs = " | ".join(f"{k}={v}" for k, v in kv.items())
            base = f"{base} | {pairs}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


class _Logger:
    """轻量包装：log.info("事件", 键=值) 风格。"""

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, event: str, **kv: Any) -> None:
        self._log.log(level, event, extra={"kv": kv} if kv else {})

    def info(self, event: str, **kv: Any) -> None:
        self._emit(logging.INFO, event, **kv)

    def warning(self, event: str, **kv: Any) -> None:
        self._emit(logging.WARNING, event, **kv)

    def error(self, event: str, **kv: Any) -> None:
        self._emit(logging.ERROR, event, **kv)

    def exception(self, event: str, **kv: Any) -> None:
        self._log.log(logging.ERROR, event, extra={"kv": kv} if kv else {}, exc_info=True)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(_LEVELS.get(level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter())
    root.addHandler(handler)
    # 降噪第三方库
    for noisy in ("httpx", "httpcore", "aiosqlite", "asyncio", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> _Logger:
    return _Logger(name)
