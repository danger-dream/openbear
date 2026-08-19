"""Per-chat operation locks for serializing high-risk session operations."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ChatOperationLocks:
    """按 chat 串行化新会话、compact、stop 等高风险操作。"""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[int, asyncio.Lock] = {}
        self._operations: dict[int, str] = {}
        self._waiting: dict[int, int] = {}

    async def _get(self, chat_id: int) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(chat_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[chat_id] = lock
            return lock

    async def _acquire(
        self,
        chat_id: int,
        operation: str,
        *,
        try_only: bool = False,
        reject_operation: str = "",
    ) -> tuple[asyncio.Lock, bool]:
        """Reserve one FIFO lock slot without a check/acquire race."""
        lock = await self._get(chat_id)
        operation_name = str(operation or "")
        async with self._guard:
            current = str(self._operations.get(chat_id) or "")
            waiting = max(0, int(self._waiting.get(chat_id) or 0))
            if reject_operation and current == reject_operation:
                return lock, False
            if try_only and (lock.locked() or waiting > 0):
                return lock, False
            if not lock.locked() and waiting <= 0:
                # With no owner or queued waiter, asyncio.Lock.acquire completes
                # immediately; keeping the guard here makes owner publication atomic.
                await lock.acquire()
                self._operations[chat_id] = operation_name
                return lock, True
            self._waiting[chat_id] = waiting + 1

        try:
            await lock.acquire()
        except BaseException:
            async with self._guard:
                remaining = max(0, int(self._waiting.get(chat_id) or 0) - 1)
                if remaining:
                    self._waiting[chat_id] = remaining
                else:
                    self._waiting.pop(chat_id, None)
            raise
        async with self._guard:
            remaining = max(0, int(self._waiting.get(chat_id) or 0) - 1)
            if remaining:
                self._waiting[chat_id] = remaining
            else:
                self._waiting.pop(chat_id, None)
            self._operations[chat_id] = operation_name
        return lock, True

    async def _release(self, chat_id: int, lock: asyncio.Lock) -> None:
        async with self._guard:
            self._operations.pop(chat_id, None)
            lock.release()

    @asynccontextmanager
    async def chat(self, chat_id: int, operation: str = "") -> AsyncIterator[None]:
        lock, _ = await self._acquire(chat_id, operation)
        try:
            yield
        finally:
            await self._release(chat_id, lock)

    @asynccontextmanager
    async def try_chat(self, chat_id: int, operation: str = "") -> AsyncIterator[bool]:
        lock, acquired = await self._acquire(chat_id, operation, try_only=True)
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            await self._release(chat_id, lock)

    @asynccontextmanager
    async def chat_unless(self, chat_id: int, operation: str, *, reject_operation: str) -> AsyncIterator[bool]:
        """Acquire while atomically refusing to queue behind a named operation."""
        lock, acquired = await self._acquire(
            chat_id,
            operation,
            reject_operation=str(reject_operation or ""),
        )
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            await self._release(chat_id, lock)

    async def current_operation(self, chat_id: int) -> str:
        async with self._guard:
            return str(self._operations.get(chat_id) or "")
