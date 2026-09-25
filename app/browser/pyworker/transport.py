"""Independent worker CDP transport. No browser-wide target initialization."""

from __future__ import annotations

import asyncio
import contextlib
import json
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .common import ProtocolError, operation


class Connection:
    def __init__(self, endpoint, timeout=10, max_message=64 * 1024 * 1024):
        self.endpoint, self.timeout, self.max_message = endpoint, timeout, max_message
        self.http = self.ws = self.reader = None
        self.pending = {}
        self.seq = 0
        self.on_event = lambda method, params, session: None

    @property
    def connected(self):
        return bool(self.ws and not self.ws.closed and self.reader and not self.reader.done())

    async def connect(self):
        self.http = aiohttp.ClientSession(trust_env=False)
        try:
            async with asyncio.timeout(self.timeout):
                url = self.endpoint
                if not url.startswith(("ws://", "wss://")):
                    parsed = urlsplit(url)
                    path = parsed.path.rstrip("/")
                    if not path.endswith("/json/version"):
                        path += "/json/version"
                    url = urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
                    async with self.http.get(url) as response:
                        response.raise_for_status()
                        body = await response.content.read(65537)
                        if len(body) > 65536:
                            raise ProtocolError("cdp_version_too_large")
                        url = json.loads(body)["webSocketDebuggerUrl"]
                self.ws = await self.http.ws_connect(
                    url, max_msg_size=self.max_message, heartbeat=20
                )
                self.reader = asyncio.create_task(self._read())
        except BaseException:
            await self.close()
            raise

    async def _read(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                if "id" in data:
                    future = self.pending.pop(data["id"], None)
                    if future and not future.done():
                        if "error" in data:
                            future.set_exception(
                                ProtocolError(data["error"].get("message", "cdp_error"))
                            )
                        else:
                            future.set_result(data.get("result", {}))
                elif "method" in data:
                    # Handlers only update state/schedule work. Never await protocol calls here.
                    self.on_event(data["method"], data.get("params", {}), data.get("sessionId"))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("cdp_disconnected"))
            self.pending.clear()

    async def call(self, method, params=None, session=None, *, timeout=None):
        if not self.connected:
            raise ConnectionError("cdp_disconnected")
        op = operation.get()
        deadline = timeout if timeout is not None else (op.remaining() if op else self.timeout)
        self.seq += 1
        mid = self.seq
        future = asyncio.get_running_loop().create_future()
        self.pending[mid] = future
        payload = {"id": mid, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        try:
            async with asyncio.timeout(deadline):
                await self.ws.send_json(payload)
                return await future
        finally:
            self.pending.pop(mid, None)
            if not future.done():
                future.cancel()

    async def close(self):
        reader, self.reader = self.reader, None
        if reader:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if self.ws:
            await self.ws.close()
        if self.http:
            await self.http.close()
        self.ws = self.http = None
