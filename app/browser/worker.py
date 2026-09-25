"""Recoverable Python worker, bounded length-prefixed IPC; never owns main Chrome."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import struct
import sys

MAX_FRAME = 20 * 1024 * 1024


class WorkerError(RuntimeError):
    def __init__(self, code, outcome="unknown", *, details=None, responded=False):
        super().__init__(code)
        self.outcome = outcome
        self.details = details or {}
        # A structured operation failure is not an IPC/process failure.
        self.responded = responded


class Worker:
    def __init__(self, python=None):
        self.python = python or sys.executable
        self.proc = None
        self.reader = None
        self.pending = {}
        self.seq = 0
        self.generation = 0
        self.lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.fatal = ""

    @property
    def connected(self):
        return bool(
            self.proc
            and self.proc.returncode is None
            and self.reader
            and not self.reader.done()
            and not self.fatal
        )

    async def start(self, init):
        async with self.lock:
            if self.connected:
                return
            await self.close()
            self.fatal = ""
            self.proc = await asyncio.create_subprocess_exec(
                self.python,
                "-m",
                "app.browser.pyworker.main",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self.generation += 1
            self.reader = asyncio.create_task(self._read(), name="browser-worker-reader")
            try:
                await self.call("initialize", init, timeout=init["connectTimeoutS"] + 1)
            except BaseException:
                await self.close()
                raise

    def _fail(self, error):
        self.fatal = str(error)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(WorkerError(self.fatal))
        self.pending.clear()

    async def _read(self):
        try:
            while True:
                size = struct.unpack(">I", await self.proc.stdout.readexactly(4))[0]
                if not 0 < size <= MAX_FRAME:
                    raise WorkerError("worker_frame_too_large")
                payload = json.loads(await self.proc.stdout.readexactly(size))
                future = self.pending.pop(payload.get("id"), None)
                if future and not future.done():
                    if "error" in payload:
                        future.set_exception(
                            WorkerError(
                                payload["error"],
                                payload.get("outcome", "unknown"),
                                details=payload.get("details", {}),
                                responded=True,
                            )
                        )
                    else:
                        future.set_result(payload.get("result", {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(
                "worker_disconnected" if isinstance(exc, asyncio.IncompleteReadError) else str(exc)
            )

    async def call(self, action, params, *, timeout):
        if not self.connected:
            raise WorkerError("worker_unavailable", "not_started")
        self.seq += 1
        mid = self.seq
        future = asyncio.get_running_loop().create_future()
        self.pending[mid] = future
        body = json.dumps(
            {"id": mid, "action": action, "params": params}, ensure_ascii=False
        ).encode()
        if len(body) > MAX_FRAME:
            self.pending.pop(mid, None)
            raise WorkerError("worker_request_too_large", "not_started")
        try:
            async with asyncio.timeout(timeout):
                async with self.write_lock:
                    self.proc.stdin.write(struct.pack(">I", len(body)) + body)
                    await self.proc.stdin.drain()
                return await future
        except (TimeoutError, asyncio.CancelledError):
            # Cancellation does not prove browser-side effects stopped. Never replay.
            self._fail("worker_request_interrupted")
            await self.close()
            raise
        finally:
            self.pending.pop(mid, None)
            if not future.done():
                future.cancel()

    async def close(self):
        self._fail("worker_closed")
        proc, self.proc = self.proc, None
        reader, self.reader = self.reader, None
        if reader and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if proc and proc.returncode is None:
            for sig, wait in ((signal.SIGTERM, 1), (signal.SIGKILL, 1)):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, sig)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), wait)
                if proc.returncode is not None:
                    break
