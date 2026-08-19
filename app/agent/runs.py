"""运行注册表 —— 支持停止当前 run、新消息打断旧 run。"""
from __future__ import annotations

import asyncio

from app.logging import get_logger

log = get_logger("agent.runs")


class RunRegistry:
    """按 chat_id 跟踪正在跑的 Agent task。"""

    def __init__(self) -> None:
        self._runs: dict[int, asyncio.Task] = {}

    def register(self, chat_id: int, task: asyncio.Task) -> None:
        self._runs[chat_id] = task
        task.add_done_callback(lambda t, c=chat_id: self._clear(c, t))

    def _clear(self, chat_id: int, task: asyncio.Task) -> None:
        if self._runs.get(chat_id) is task:
            self._runs.pop(chat_id, None)

    def task(self, chat_id: int) -> asyncio.Task | None:
        t = self._runs.get(chat_id)
        return t if t is not None and not t.done() else None

    def is_running(self, chat_id: int) -> bool:
        return self.task(chat_id) is not None

    def cancel(self, chat_id: int) -> bool:
        """取消该 chat 当前 run。返回是否确实取消了。"""
        t = self.task(chat_id)
        if t is not None:
            t.cancel()
            log.info("已请求取消 run", 会话=chat_id)
            return True
        return False

    async def cancel_and_wait(self, chat_id: int, *, timeout_s: float = 10.0) -> bool:
        """取消并等待该 run 完成清理，避免新会话清库后旧 run 又写回。"""
        t = self.task(chat_id)
        if t is None:
            return False
        t.cancel()
        log.info("已请求取消 run 并等待", 会话=chat_id)
        try:
            await asyncio.wait_for(asyncio.shield(t), timeout=timeout_s)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            log.warning("等待 run 取消超时", 会话=chat_id, 超时=timeout_s)
            return False
        except Exception as e:
            log.warning("等待 run 结束时捕获异常", 会话=chat_id, 错误=str(e)[:120])
        return True

    async def cancel_all_and_wait(self, *, timeout_s: float | None = None) -> int:
        """Cancel every registered run and wait for its cleanup before DB close.

        Service shutdown used to close SQLite while Web controller tasks were
        still unwinding.  Their CancelledError handlers then attempted to persist
        terminal frames through a disconnected DB, leaving durable zombie runs.
        """
        tasks = [task for task in self._runs.values() if not task.done()]
        if not tasks:
            return 0
        for task in tasks:
            task.cancel()
        log.info("已请求取消全部 run", 数量=len(tasks))
        done, pending = await asyncio.wait(
            tasks,
            timeout=None if timeout_s is None else max(0.0, float(timeout_s)),
        )
        for task in done:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
        if pending:
            log.warning("等待全部 run 取消超时", 剩余=len(pending), 超时=timeout_s)
        return len(tasks) - len(pending)

    def count(self) -> int:
        return sum(1 for t in self._runs.values() if not t.done())
