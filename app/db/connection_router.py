"""Transaction-aware SQLite read/write routing.

All application writes share one physical writer connection and one task-owned
transaction lock.  Reads use a query-only connection unless the current task
already owns the writer transaction.  This avoids WAL writer collisions and the
SQLITE_BUSY_SNAPSHOT failure caused by upgrading a shared read connection after
another connection committed.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import aiosqlite

from app.logging import get_logger

log = get_logger("db.router")

_T = TypeVar("_T")
_FIRST_TOKEN_RE = re.compile(r"^\s*([A-Za-z]+)")
_CTE_WRITE_RE = re.compile(r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
_READ_TOKENS = {"SELECT", "EXPLAIN"}
_TRANSACTION_TOKENS = {"BEGIN", "SAVEPOINT", "RELEASE", "COMMIT", "END", "ROLLBACK"}
# Every other writer waits behind a held transaction; report holds this long.
_SLOW_HOLD_S = 5.0


def _strip_leading_comments(sql: str) -> str:
    value = str(sql or "")
    while True:
        stripped = value.lstrip()
        if stripped.startswith("--"):
            _, sep, tail = stripped.partition("\n")
            value = tail if sep else ""
            continue
        if stripped.startswith("/*"):
            end = stripped.find("*/", 2)
            value = stripped[end + 2 :] if end >= 0 else ""
            continue
        return stripped


def _statement_token(sql: str) -> str:
    match = _FIRST_TOKEN_RE.match(_strip_leading_comments(sql))
    return str(match.group(1) if match else "").upper()


def _is_read_statement(sql: str) -> bool:
    cleaned = _strip_leading_comments(sql)
    token = _statement_token(cleaned)
    if token in _READ_TOKENS:
        return True
    if token == "WITH":
        return _CTE_WRITE_RE.search(cleaned) is None
    return False


def _is_locked_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text


def _implicit_label(kind: str, sql: str) -> str:
    return f"{kind}:{' '.join(sql.split())[:80]}"


_INTERNAL_PATHS = (os.path.dirname(asyncio.__file__), os.path.dirname(aiosqlite.__file__), __file__)


def _suspended_at(task: asyncio.Task[Any] | None) -> str:
    """Innermost caller frame of the await chain, past asyncio/aiosqlite/router internals.

    Task.get_stack only reaches the task's entry coroutine.
    """
    frames = []
    awaitable: Any = task.get_coro() if task is not None else None
    while awaitable is not None:
        frame = (getattr(awaitable, "cr_frame", None) or getattr(awaitable, "gi_frame", None)
                 or getattr(awaitable, "ag_frame", None))
        if frame is None:
            break
        frames.append(frame)
        awaitable = (getattr(awaitable, "cr_await", None) or getattr(awaitable, "gi_yieldfrom", None)
                     or getattr(awaitable, "ag_await", None))
    caller = next((f for f in reversed(frames) if not f.f_code.co_filename.startswith(_INTERNAL_PATHS)),
                  frames[-1] if frames else None)
    return f"{caller.f_code.co_filename}:{caller.f_lineno}" if caller is not None else "unknown"


class SQLiteWriterTimeout(TimeoutError):
    """The application writer is busy; no SQLite operation was queued."""


async def drain_cleanup(operation: Awaitable[_T]) -> _T:
    """Finish resource cleanup even if shutdown is cancelled more than once."""
    cleanup = asyncio.ensure_future(operation)
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    result = cleanup.result()
    if cancelled:
        raise asyncio.CancelledError()
    return result


# A cancelled request's own statement is aborted from the progress handler,
# checked every this many VM instructions (well under a millisecond).
_ABORT_CHECK_INSTRUCTIONS = 10_000
# sqlite3_interrupt() stays set until no statement on the connection is active,
# so with another task's cursor open it fails unrelated queries. Use it only for
# a request the progress handler cannot reach (e.g. waiting on a lock).
_INTERRUPT_FALLBACK_S = 1.0


class InterruptibleConnection(aiosqlite.Connection):
    """Cancel only the caller's queued/running SQLite work, including cursor fetches.

    aiosqlite otherwise leaves cancelled statements running on its worker. The
    small worker-side identity guard keeps a cancellation on the shared reader
    from aborting a different request.
    """

    def __init__(self, path: str) -> None:
        super().__init__(lambda: self._open(path), iter_chunk_size=64)
        self._request_guard = threading.Lock()
        self._active_request: threading.Event | None = None
        self._requests: set[threading.Event] = set()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    def _open(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.set_progress_handler(self._abort_requested, _ABORT_CHECK_INSTRUCTIONS)
        return conn

    def _abort_requested(self) -> int:
        request = self._active_request
        return 1 if request is not None and request.is_set() else 0

    def _interrupt_request(self, request: threading.Event) -> None:
        with self._request_guard:
            if self._active_request is request:
                # sqlite3.interrupt is explicitly safe from a different thread.
                with contextlib.suppress(sqlite3.Error, ValueError):
                    self._conn.interrupt()

    async def _execute(self, fn, *args, **kwargs):
        # aiosqlite.close queues native close through this same hook.
        if self._closing:
            if fn == self._conn.close:
                return await super()._execute(fn, *args, **kwargs)
            raise ValueError("Connection closed")
        request = threading.Event()
        started = threading.Event()
        self._requests.add(request)

        def run():
            with self._request_guard:
                if request.is_set():
                    raise sqlite3.OperationalError("interrupted")
                self._active_request = request
                started.set()
            try:
                return fn(*args, **kwargs)
            finally:
                with self._request_guard:
                    self._active_request = None

        pending = asyncio.create_task(super()._execute(run))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            with self._request_guard:
                request.set()
                queued = not started.is_set()
            if queued:
                # This request will be skipped by the worker. Do not wait behind
                # (or interrupt) an unrelated long-running reader request.
                pending.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                raise
            # The progress handler aborts this request's statement; fall back to
            # interrupting only while THIS request is still active.
            fallback_at = time.monotonic() + _INTERRUPT_FALLBACK_S
            while not pending.done():
                if time.monotonic() >= fallback_at:
                    self._interrupt_request(request)
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({pending}, timeout=0.01)
            with contextlib.suppress(BaseException):
                result = pending.result()
                if isinstance(result, sqlite3.Cursor):
                    await drain_cleanup(super()._execute(result.close))
            raise
        finally:
            self._requests.discard(request)

    async def fetch_read(self, sql: str, parameters: Any) -> tuple[list[Any], Any]:
        """Run a read to its end in one worker call: its rows and description."""
        return await self._execute(self._fetch_read, sql, parameters)

    def _fetch_read(self, sql: str, parameters: Any) -> tuple[list[Any], Any]:
        cursor = self._conn.execute(sql, parameters)
        try:
            return cursor.fetchall(), cursor.description
        finally:
            cursor.close()

    async def interrupt(self) -> None:
        """Explicit emergency stop of currently queued/running work only."""
        for request in tuple(self._requests):
            request.set()
            self._interrupt_request(request)

    async def _close_connection(self) -> None:
        self._closing = True
        pending = asyncio.create_task(super().close())
        while not pending.done():
            await self.interrupt()
            await asyncio.wait({pending}, timeout=0.01)
        await pending

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_connection())
        await drain_cleanup(self._close_task)


async def open_sqlite_connection(path: str) -> InterruptibleConnection:
    """Retain a connecting worker until it can be closed, even on cancellation."""
    connection = InterruptibleConnection(path)
    pending = asyncio.ensure_future(connection)
    try:
        return await asyncio.shield(pending)
    except BaseException:
        with contextlib.suppress(BaseException):
            await drain_cleanup(pending)
        with contextlib.suppress(BaseException):
            await connection.close()
        raise


class _ReadCursor:
    """The rows of a finished read, served without touching the shared reader."""

    rowcount = -1

    def __init__(self, rows: list[Any], description: Any) -> None:
        self._rows: list[Any] | None = rows
        self._next = 0
        self.description = description
        self.arraysize = 1

    def _open_rows(self) -> list[Any]:
        if self._rows is None:
            raise sqlite3.ProgrammingError("Cannot operate on a closed cursor.")
        return self._rows

    async def fetchone(self) -> Any:
        rows = self._open_rows()
        if self._next >= len(rows):
            return None
        self._next += 1
        return rows[self._next - 1]

    async def fetchmany(self, size: int | None = None) -> list[Any]:
        rows, start = self._open_rows(), self._next
        self._next = min(len(rows), start + max(0, self.arraysize if size is None else size))
        return rows[start:self._next]

    async def fetchall(self) -> list[Any]:
        rows, start = self._open_rows(), self._next
        self._next = len(rows)
        return rows[start:]

    async def close(self) -> None:
        self._rows = None

    def __aiter__(self) -> _ReadCursor:
        return self

    async def __anext__(self) -> Any:
        row = await self.fetchone()
        if row is None:
            raise StopAsyncIteration
        return row

    async def __aenter__(self) -> _ReadCursor:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()


class SQLiteConnectionRouter:
    """Duck-typed subset of ``aiosqlite.Connection`` used by OpenBear.

    A normal write owns ``_writer_lock`` from its first mutating statement until
    commit/rollback.  Multi-statement DAO methods therefore become atomic without
    changing each call site, and unrelated tasks cannot commit one another's work.
    """

    def __init__(
        self,
        *,
        reader: InterruptibleConnection,
        writer: aiosqlite.Connection,
        lock_retries: int = 2,
        writer_wait_timeout_s: float = 30.0,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._writer_lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._owner_label = ""
        self._owner_since = 0.0
        self._owner_callback: Callable[[asyncio.Task[Any]], None] | None = None
        self._lock_retries = max(0, int(lock_retries))
        self._writer_wait_timeout_s = max(0.001, float(writer_wait_timeout_s))
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._transaction_depth = 0
        # Notifications only wake consumers; callbacks must not perform database
        # work or delay the owning transaction. No uncommitted data is broadcast.
        self.commit_listeners: set[Callable[[], None]] = set()

    def _notify_commit(self) -> None:
        for callback in tuple(self.commit_listeners):
            with contextlib.suppress(Exception):
                callback()

    @property
    def in_transaction(self) -> bool:
        return bool(self._writer.in_transaction)

    @property
    def total_changes(self) -> int:
        return int(self._writer.total_changes)

    def _current_task(self) -> asyncio.Task[Any]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("SQLite writer operation requires an asyncio task")
        return task

    async def _acquire_writer(self, *, wait_timeout_s: float | None = None,
                              label: str = "write") -> tuple[asyncio.Task[Any], bool]:
        task = self._current_task()
        if self._closed:
            raise RuntimeError("数据库已关闭")
        if self._owner is task:
            return task, False
        timeout = self._writer_wait_timeout_s if wait_timeout_s is None else max(0.001, float(wait_timeout_s))
        try:
            # asyncio.timeout preserves task identity: wait_for would acquire
            # in a child task and break transaction ownership/reentrancy.
            async with asyncio.timeout(timeout):
                await self._writer_lock.acquire()
        except TimeoutError as exc:
            owner = self._owner
            owner_name = owner.get_name() if owner is not None else "unknown"
            owner_label = self._owner_label if owner is not None else "unknown"
            held = time.monotonic() - self._owner_since if owner is not None else 0.0
            if wait_timeout_s is None:
                log.error("SQLite 写锁等待超时", 等待任务=task.get_name(), 等待事务=label,
                          持锁任务=owner_name, 持锁事务=owner_label, 已持锁秒=round(held, 1),
                          持锁位置=_suspended_at(owner), 等待秒=timeout)
            # A waiting caller must never commit/rollback/release another
            # task's transaction. Short optional writes can safely skip it.
            raise SQLiteWriterTimeout(
                f"SQLite writer wait timed out after {timeout:g}s "
                f"(owner={owner_name}, transaction={owner_label}, held={held:.1f}s)"
            ) from exc
        if self._closed:
            self._writer_lock.release()
            raise RuntimeError("数据库已关闭")
        self._owner = task
        self._owner_label = label
        self._owner_since = time.monotonic()
        loop = asyncio.get_running_loop()

        def _owner_done(done_task: asyncio.Task[Any]) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        self._recover_abandoned_owner(done_task),
                        name="sqlite-writer-owner-recovery",
                    )
                )

        self._owner_callback = _owner_done
        task.add_done_callback(_owner_done)
        return task, True

    def _release_writer(self, task: asyncio.Task[Any]) -> None:
        if self._owner is not task:
            return
        if self._owner_callback is not None:
            task.remove_done_callback(self._owner_callback)
            self._owner_callback = None
        held = time.monotonic() - self._owner_since
        if held >= _SLOW_HOLD_S:
            log.warning("SQLite 写事务持锁过久", 事务=self._owner_label, 任务=task.get_name(), 持锁秒=round(held, 1))
        self._owner = None
        if self._writer_lock.locked():
            self._writer_lock.release()

    async def _recover_abandoned_owner(self, task: asyncio.Task[Any]) -> None:
        if self._owner is not task:
            return
        try:
            await self._drain_cleanup(self._writer.rollback())
            log.warning("已回滚未正常结束的 SQLite 写事务", 任务=task.get_name())
        except BaseException as exc:  # pragma: no cover - catastrophic connection failure
            log.error("SQLite 写事务自动回滚失败", 错误=f"{type(exc).__name__}: {exc}")
        finally:
            self._release_writer(task)

    async def _drain_cleanup(self, operation: Awaitable[_T]) -> _T:
        """Keep ownership until SQLite has processed cleanup, even on repeated stop.

        Cancelling an aiosqlite await does not remove its queued SQL. A rollback
        must be queued unconditionally behind it, not gated on in_transaction.
        """
        return await drain_cleanup(operation)

    async def _retry_locked(self, operation: Callable[[], Awaitable[_T]], *, label: str) -> _T:
        for attempt in range(self._lock_retries + 1):
            try:
                return await operation()
            except BaseException as exc:
                if not _is_locked_error(exc) or attempt >= self._lock_retries:
                    raise
                delay = 0.05 * (2**attempt)
                log.warning(
                    "SQLite 外部写锁冲突，准备重试",
                    操作=label,
                    次数=attempt + 1,
                    等待秒=delay,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _execute_read(self, sql: str, parameters: Any) -> _ReadCursor:
        # An unfinished SELECT holds its WAL snapshot, and every other read on the
        # shared reader joins it: while one caller awaited between execute and
        # fetch, or was cancelled there, the others could not see later commits.
        # Executing and fetching in one worker call leaves no statement open across
        # an await, and the worker finishes it even if the caller is cancelled.
        return _ReadCursor(*await self._reader.fetch_read(sql, parameters))

    async def execute(self, sql: str, parameters: Any = None) -> aiosqlite.Cursor | _ReadCursor:
        if self._closed:
            raise RuntimeError("数据库已关闭")
        params = () if parameters is None else parameters
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            try:
                return await self._retry_locked(
                    lambda: self._writer.execute(sql, params),
                    label=_statement_token(sql) or "execute",
                )
            except BaseException:
                if not self._transaction_depth:
                    with contextlib.suppress(BaseException):
                        await self._drain_cleanup(self._writer.rollback())
                    self._release_writer(task)
                raise
        if _is_read_statement(sql):
            return await self._execute_read(sql, params)

        owner, acquired = await self._acquire_writer(label=_implicit_label("implicit", sql))
        try:
            cursor = await self._retry_locked(
                lambda: self._writer.execute(sql, params),
                label=_statement_token(sql) or "execute",
            )
            # PRAGMA and transaction-ending statements may leave no active
            # transaction.  Do not retain the writer lock in that case.
            if not self._writer.in_transaction:
                self._release_writer(owner)
                self._notify_commit()
            return cursor
        except BaseException:
            if acquired or self._owner is owner:
                with contextlib.suppress(BaseException):
                    await self._drain_cleanup(self._writer.rollback())
                self._release_writer(owner)
            raise

    async def execute_fetchall(self, sql: str, parameters: Any = None) -> list[Any]:
        """Run a statement and collect its rows; a read does both in one worker call.

        Between a separate ``execute`` and fetch, other tasks' reads on the shared
        reader see the snapshot the unfinished SELECT holds. Long-running
        maintenance scans read this way so they never pin it across awaits.
        """
        params = () if parameters is None else parameters
        task = asyncio.current_task()
        if not self._closed and _is_read_statement(sql) and not (task is not None and self._owner is task):
            return list(await self._reader.execute_fetchall(sql, params))
        cursor = await self.execute(sql, params)
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    async def executemany(self, sql: str, parameters: Any) -> aiosqlite.Cursor:
        if self._closed:
            raise RuntimeError("数据库已关闭")
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            try:
                return await self._retry_locked(
                    lambda: self._writer.executemany(sql, parameters),
                    label=f"executemany:{_statement_token(sql) or 'SQL'}",
                )
            except BaseException:
                if not self._transaction_depth:
                    with contextlib.suppress(BaseException):
                        await self._drain_cleanup(self._writer.rollback())
                    self._release_writer(task)
                raise
        if _is_read_statement(sql):
            return await self._reader.executemany(sql, parameters)
        owner, _ = await self._acquire_writer(label=_implicit_label("implicit-many", sql))
        try:
            cursor = await self._retry_locked(
                lambda: self._writer.executemany(sql, parameters),
                label=f"executemany:{_statement_token(sql) or 'SQL'}",
            )
            if not self._writer.in_transaction:
                self._release_writer(owner)
                self._notify_commit()
            return cursor
        except BaseException:
            with contextlib.suppress(BaseException):
                await self._drain_cleanup(self._writer.rollback())
            self._release_writer(owner)
            raise

    async def executescript(self, sql_script: str) -> aiosqlite.Cursor:
        owner, _ = await self._acquire_writer(label="executescript")
        try:
            cursor = await self._retry_locked(
                lambda: self._writer.executescript(sql_script),
                label="executescript",
            )
            if not self._writer.in_transaction:
                self._release_writer(owner)
                self._notify_commit()
            return cursor
        except BaseException:
            with contextlib.suppress(BaseException):
                await self._drain_cleanup(self._writer.rollback())
            self._release_writer(owner)
            raise

    async def commit(self) -> None:
        # DAO-level commits join an explicit transaction owned by this task.
        # Only its outer context manager can commit/notify; other tasks still wait.
        if self._owner is asyncio.current_task() and self._transaction_depth:
            return
        owner, _ = await self._acquire_writer(label="commit")
        try:
            await self._retry_locked(self._writer.commit, label="commit")
            self._notify_commit()
        except BaseException:
            with contextlib.suppress(BaseException):
                await self._drain_cleanup(self._writer.rollback())
            raise
        finally:
            self._release_writer(owner)

    async def rollback(self) -> None:
        owner, _ = await self._acquire_writer(label="rollback")
        try:
            await self._drain_cleanup(self._writer.rollback())
        finally:
            self._release_writer(owner)

    async def backup(self, target: sqlite3.Connection, **kwargs: Any) -> None:
        owner, _ = await self._acquire_writer(label="backup")
        try:
            if self._writer.in_transaction:
                await self._writer.commit()
                self._notify_commit()
            await self._writer.backup(target, **kwargs)
        finally:
            self._release_writer(owner)

    @asynccontextmanager
    async def transaction(self, *, label: str = "transaction", foreign_keys: bool = False,
                          wait_timeout_s: float | None = None,
                          sqlite_busy_timeout_ms: int | None = None) -> AsyncIterator[aiosqlite.Connection]:
        if sqlite_busy_timeout_ms is not None and sqlite_busy_timeout_ms < 0:
            raise ValueError("sqlite_busy_timeout_ms must be non-negative")
        owner, acquired = await self._acquire_writer(wait_timeout_s=wait_timeout_s, label=label)
        nested = bool(self._writer.in_transaction)
        savepoint = f"openbear_{uuid.uuid4().hex}"
        previous_fk: int | None = None
        previous_busy_timeout: int | None = None
        begun = False
        try:
            if sqlite_busy_timeout_ms is not None:
                async with self._writer.execute("PRAGMA busy_timeout") as cursor:
                    previous_busy_timeout = int((await cursor.fetchone())[0])
                await self._writer.execute(f"PRAGMA busy_timeout={int(sqlite_busy_timeout_ms)}")
            if foreign_keys:
                cursor = await self._writer.execute("PRAGMA foreign_keys")
                previous_fk = int((await cursor.fetchone())[0])
                await cursor.close()
                if nested and not previous_fk:
                    raise RuntimeError("Webhook transaction cannot nest inside an FK-disabled transaction")
                if not nested:
                    await self._writer.execute("PRAGMA foreign_keys=ON")
                    cursor = await self._writer.execute("PRAGMA foreign_keys")
                    active_fk = int((await cursor.fetchone())[0])
                    await cursor.close()
                    if active_fk != 1:
                        raise RuntimeError("Webhook foreign key enforcement unavailable")
            if nested:
                await self._writer.execute(f"SAVEPOINT {savepoint}")
            elif sqlite_busy_timeout_ms is not None:
                # Maintenance opts into one short native lock wait, not retries.
                await self._writer.execute("BEGIN IMMEDIATE")
            else:
                await self._retry_locked(
                    lambda: self._writer.execute("BEGIN IMMEDIATE"),
                    label=f"{label}:begin",
                )
            begun = True
            self._transaction_depth += 1
            try:
                yield self._writer
            finally:
                self._transaction_depth -= 1
            if nested:
                await self._writer.execute(f"RELEASE {savepoint}")
            else:
                if sqlite_busy_timeout_ms is not None:
                    await self._writer.commit()
                else:
                    await self._retry_locked(self._writer.commit, label=f"{label}:commit")
                self._notify_commit()
        except BaseException:
            with contextlib.suppress(BaseException):
                if nested and begun:
                    await self._drain_cleanup(self._writer.execute(f"ROLLBACK TO {savepoint}"))
                    await self._drain_cleanup(self._writer.execute(f"RELEASE {savepoint}"))
                elif not nested:
                    await self._drain_cleanup(self._writer.rollback())
            raise
        finally:
            try:
                try:
                    if foreign_keys and not nested and previous_fk is not None:
                        await self._drain_cleanup(self._writer.execute(f"PRAGMA foreign_keys={previous_fk}"))
                finally:
                    if previous_busy_timeout is not None:
                        await self._drain_cleanup(self._writer.execute(f"PRAGMA busy_timeout={previous_busy_timeout}"))
            finally:
                if acquired:
                    self._release_writer(owner)

    async def interrupt(self) -> None:
        """Emergency shutdown only: interrupt both connections, not on lock timeout."""
        for connection in (self._reader, self._writer):
            with contextlib.suppress(sqlite3.Error, ValueError):
                await connection.interrupt()

    async def _close_connections(self, caller: asyncio.Task[Any] | None) -> None:
        owner = self._owner
        if owner is not None and owner is not caller:
            # Ask the owner to unwind its own transaction. Never roll back another
            # live task's transaction or steal its writer lock.
            owner.cancel()
        await self.interrupt()
        reader_close = asyncio.create_task(self._reader.close())
        try:
            if owner is caller and owner is not None:
                try:
                    await self._writer.rollback()
                finally:
                    self._release_writer(owner)
            async with self._writer_lock:
                await self._writer.close()
        finally:
            await reader_close

    async def close(self) -> None:
        """Terminal shutdown; callers should stop producers before closing the DB."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_connections(asyncio.current_task()))
        await self._drain_cleanup(self._close_task)
