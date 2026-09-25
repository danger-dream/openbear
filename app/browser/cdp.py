"""Small browser-root CDP channel. Never enables Target auto-attach."""

from __future__ import annotations

import asyncio
import contextlib
import json
from urllib.parse import urlsplit, urlunsplit

import aiohttp


class CDP:
    def __init__(self, endpoint: str, timeout: float = 10) -> None:
        self.endpoint, self.timeout = endpoint, timeout
        self.ws_url = ""
        self._http = None
        self._ws = None
        self._reader = None
        self._lock = asyncio.Lock()
        self._pending = {}
        self._seq = 0

    @property
    def connected(self):
        return bool(self._ws and not self._ws.closed and self._reader and not self._reader.done())

    async def connect(self):
        async with self._lock:
            if self.connected:
                return
            await self.close()
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=False
            )
            try:
                async with asyncio.timeout(self.timeout):
                    url = self.endpoint
                    if not url.startswith(("ws://", "wss://")):
                        p = urlsplit(url)
                        path = p.path.rstrip("/")
                        if not path.endswith("/json/version"):
                            path += "/json/version"
                        version_url = urlunsplit((p.scheme, p.netloc, path, p.query, ""))
                        async with self._http.get(version_url) as response:
                            response.raise_for_status()
                            raw = await response.content.read(65537)
                            if len(raw) > 65536:
                                raise RuntimeError("cdp_version_response_too_large")
                            url = json.loads(raw)["webSocketDebuggerUrl"]
                    self._ws = await self._http.ws_connect(
                        url, max_msg_size=4 * 1024 * 1024, heartbeat=20
                    )
                    self.ws_url = url
                    self._reader = asyncio.create_task(self._read(), name="browser-cdp-reader")
            except BaseException:
                await self.close()
                raise

    async def _read(self):
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                future = self._pending.pop(data.get("id"), None)
                if future and not future.done():
                    if "error" in data:
                        future.set_exception(
                            RuntimeError(
                                "cdp_command_failed: " + str(data["error"].get("message", ""))[:200]
                            )
                        )
                    else:
                        future.set_result(data.get("result", {}))
        except Exception:
            pass
        finally:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError("cdp_disconnected"))
            self._pending.clear()

    async def call(self, method, params=None, *, session_id=None, timeout=None):
        await self.connect()
        self._seq += 1
        mid = self._seq
        future = asyncio.get_running_loop().create_future()
        self._pending[mid] = future
        payload = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        try:
            async with asyncio.timeout(timeout or self.timeout):
                await self._ws.send_json(payload)
                return await future
        finally:
            self._pending.pop(mid, None)
            if not future.done():
                future.cancel()

    async def target_call(self, target_id, method, params=None, *, timeout=2):
        # A separate per-target attachment; unrelated pages are never initialized.
        session = None
        try:
            async with asyncio.timeout(timeout):
                attached = await self.call(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                    timeout=timeout,
                )
                session = attached["sessionId"]
                return await self.call(method, params, session_id=session, timeout=timeout)
        finally:
            if session and self.connected:
                with contextlib.suppress(Exception):
                    await self.call("Target.detachFromTarget", {"sessionId": session}, timeout=0.25)

    async def close(self):
        reader, self._reader = self._reader, None
        if reader and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._http:
            await self._http.close()
            self._http = None
