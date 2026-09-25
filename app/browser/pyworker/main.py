"""Length-prefixed Python worker entry point. Never owns or closes attached Chrome."""

from __future__ import annotations

import asyncio
import json
import signal
import struct
import sys

from .common import MAX_FRAME, BrowserError
from .engine import Engine


async def serve():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_FRAME)
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    write_transport, protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop), sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(write_transport, protocol, None, loop)
    engine = Engine()
    tasks = set()
    write_lock = asyncio.Lock()
    stopped = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)

    async def reply(value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_FRAME:
            body = json.dumps(
                {"id": value.get("id"), "error": "worker_response_too_large", "outcome": "unknown"}
            ).encode()
        async with write_lock:
            writer.write(struct.pack(">I", len(body)) + body)
            await writer.drain()

    async def dispatch(message):
        mid = message.get("id")
        try:
            if not engine.config and message.get("action") != "initialize":
                raise BrowserError("worker_not_initialized")
            result = await engine.execute(message["action"], message["params"])
            await reply({"id": mid, "result": result})
        except BrowserError as exc:
            await reply(
                {"id": mid, "error": exc.code, "outcome": exc.outcome, "details": exc.details}
            )
        except Exception:
            # No evaluated source, endpoint credentials, raw protocol data or stacks over IPC.
            await reply({"id": mid, "error": "browser_operation_failed", "outcome": "unknown"})

    async def read_messages():
        try:
            while True:
                size = struct.unpack(">I", await reader.readexactly(4))[0]
                if not 0 < size <= MAX_FRAME:
                    break
                message = json.loads(await reader.readexactly(size))
                if not isinstance(message, dict):
                    break
                if len(tasks) >= 64:
                    await reply(
                        {"id": message.get("id"), "error": "worker_busy", "outcome": "not_started"}
                    )
                    continue
                task = asyncio.create_task(dispatch(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            stopped.set()

    reading = asyncio.create_task(read_messages())
    try:
        await stopped.wait()
    finally:
        reading.cancel()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(reading, *tasks, return_exceptions=True)
        await engine.close()
        read_transport.close()
        writer.close()


def main():
    try:
        asyncio.run(serve())
    except (BrokenPipeError, ConnectionResetError):
        pass


if __name__ == "__main__":
    main()
