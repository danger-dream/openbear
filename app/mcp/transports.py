"""JSON-RPC transports for MCP.

stdio supports both newline-delimited JSON-RPC and the MCP/LSP-style
``Content-Length`` framing commonly used by stdio MCP servers. streamable_http
keeps a persistent HTTP session, preserves ``Mcp-Session-Id``, accepts JSON or
SSE responses, and returns the response whose JSON-RPC id matches the request.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import signal
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import MCPServerConfig
from app.logging import get_logger
from app.mcp.errors import MCPConnectionError, MCPServerExited, MCPTimeoutError, MCPToolCallError
from app.mcp.output import expand_header_env, redact_headers, redact_text_secrets
from app.tools import processes

log = get_logger("mcp.transport")

_JSONRPC_VERSION = "2.0"
_STDERR_LIMIT = 16_000
_SESSION_HEADER = "Mcp-Session-Id"
_PROTOCOL_HEADER = "MCP-Protocol-Version"


NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class MCPTransport(ABC):
    def set_notification_handler(self, handler: NotificationHandler | None) -> None:
        self._notification_handler = handler

    def _dispatch_notification(self, method: str, params: Any) -> None:
        handler = getattr(self, "_notification_handler", None)
        if handler is None or not method:
            return
        try:
            result = handler(method, params if isinstance(params, dict) else {})
            if inspect.isawaitable(result):
                asyncio.create_task(result, name=f"mcp-notification-{method.rsplit('/', 1)[-1]}")
        except Exception as exc:
            log.warning("mcp.notification.dispatch_failed", method=method, error=redact_text_secrets(str(exc))[:300])

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float) -> Any: ...

    @abstractmethod
    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class StdioJSONRPCTransport(MCPTransport):
    def __init__(self, server_key: str, config: MCPServerConfig) -> None:
        self.server_key = server_key
        self.config = config
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = ""
        self._closed = False
        self._stdio_mode = str(getattr(config, "stdio_mode", "auto") or "auto").strip().lower()
        if self._stdio_mode not in {"auto", "framed", "newline"}:
            self._stdio_mode = "auto"
        self._write_framed = self._stdio_mode == "framed"

    async def connect(self) -> None:
        if not self.config.command.strip():
            raise MCPConnectionError(f"MCP server {self.server_key} missing command")
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (self.config.env or {}).items()})
        cwd = self.config.cwd.strip() or None
        try:
            self.proc = await asyncio.create_subprocess_exec(
                self.config.command,
                *list(self.config.args or []),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except Exception as exc:
            raise MCPConnectionError(f"failed to start stdio MCP server {self.server_key}: {type(exc).__name__}: {exc}") from exc
        processes.register(
            self.proc.pid,
            command=f"mcp:{self.server_key}:{self.config.command}",
            cwd=cwd or os.getcwd(),
            blocks_restart=False,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(), name=f"mcp-stdio-{self.server_key}-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"mcp-stdio-{self.server_key}-stderr")

    async def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while not self._closed:
                msg = await self._read_stdio_message()
                if msg is None:
                    break
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id")
                if mid is None:
                    self._dispatch_notification(str(msg.get("method") or ""), msg.get("params"))
                    continue
                fut = None
                if isinstance(mid, int):
                    fut = self._pending.pop(mid, None)
                elif isinstance(mid, str) and mid.isdigit():
                    fut = self._pending.pop(int(mid), None)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    fut.set_exception(MCPToolCallError(_rpc_error_text(msg.get("error"))))
                else:
                    fut.set_result(msg.get("result"))
        except Exception as exc:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(MCPConnectionError(f"stdio read failed: {type(exc).__name__}: {exc}"))
            self._pending.clear()
        finally:
            rc = self.proc.returncode if self.proc is not None else None
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(MCPServerExited(f"MCP server {self.server_key} exited" + (f" rc={rc}" if rc is not None else "")))
            self._pending.clear()

    async def _read_stdio_message(self) -> dict[str, Any] | None:
        assert self.proc is not None and self.proc.stdout is not None
        first = await self.proc.stdout.readline()
        if not first:
            return None
        text = first.decode("utf-8", "replace")
        if text.lower().startswith("content-length:"):
            self._write_framed = True
            length_text = text.split(":", 1)[1].strip()
            try:
                length = int(length_text)
            except ValueError as exc:
                raise MCPConnectionError(f"invalid stdio Content-Length header: {length_text!r}") from exc
            while True:
                header = await self.proc.stdout.readline()
                if not header:
                    return None
                htext = header.decode("utf-8", "replace").strip()
                if not htext:
                    break
                if htext.lower().startswith("content-length:"):
                    # Some broken servers may repeat the header; use the last one.
                    with contextlib.suppress(ValueError):
                        length = int(htext.split(":", 1)[1].strip())
            body = await self.proc.stdout.readexactly(length)
            try:
                parsed = json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError as exc:
                raise MCPConnectionError("invalid framed stdio JSON-RPC message") from exc
            return parsed if isinstance(parsed, dict) else {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            log.warning("mcp.stdio.invalid_json", server=self.server_key, preview=redact_text_secrets(text[:200]))
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        with contextlib.suppress(Exception):
            while not self._closed:
                chunk = await self.proc.stderr.read(1024)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                self._stderr_tail = (self._stderr_tail + text)[-_STDERR_LIMIT:]

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float) -> Any:
        if self.proc is None or self.proc.stdin is None:
            raise MCPConnectionError(f"MCP server {self.server_key} is not connected")
        if self.proc.returncode is not None:
            raise MCPServerExited(f"MCP server {self.server_key} exited rc={self.proc.returncode}: {self._stderr_preview()}")
        self._next_id += 1
        mid = self._next_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[mid] = fut
        payload = {"jsonrpc": _JSONRPC_VERSION, "id": mid, "method": method, "params": params or {}}
        try:
            self.proc.stdin.write(self._encode_stdio_payload(payload))
            await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=max(1.0, float(timeout_s or 1)))
        except TimeoutError as exc:
            self._pending.pop(mid, None)
            if self._stdio_mode == "auto" and not self._write_framed and method == "initialize":
                # Some MCP servers are strict about Content-Length from the very
                # first initialize request.  Retry once with framed output while
                # keeping newline-json as the auto first try for compatibility
                # with Serena-style newline stdio servers.
                self._write_framed = True
                log.info("mcp.stdio.retry_framed", server=self.server_key)
                return await self.request(method, params, timeout_s=timeout_s)
            raise MCPTimeoutError(f"MCP {self.server_key} {method} timeout after {timeout_s}s") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(mid, None)
            raise MCPServerExited(f"MCP server {self.server_key} pipe closed: {self._stderr_preview()}") from exc

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.returncode is not None:
            return
        payload = {"jsonrpc": _JSONRPC_VERSION, "method": method, "params": params or {}}
        with contextlib.suppress(Exception):
            self.proc.stdin.write(self._encode_stdio_payload(payload))
            await self.proc.stdin.drain()

    def _encode_stdio_payload(self, payload: dict[str, Any]) -> bytes:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self._write_framed:
            return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        return body + b"\n"

    def _stderr_preview(self) -> str:
        text = self._stderr_tail.strip().replace("\n", " ")
        return text[-800:]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if proc is not None:
            with contextlib.suppress(Exception):
                await self.notify("notifications/cancelled", {"reason": "client_shutdown"})
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            await self._terminate_process(proc)
            processes.unregister(proc.pid)
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task

    async def _terminate_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        pgid = None
        with contextlib.suppress(Exception):
            pgid = os.getpgid(proc.pid)
        for sig, wait_s in ((signal.SIGINT, 1.5), (signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
            if proc.returncode is not None:
                return
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    proc.send_signal(sig)
            except ProcessLookupError:
                return
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait_s)
                return
            except TimeoutError:
                continue


class StreamableHTTPTransport(MCPTransport):
    def __init__(self, server_key: str, config: MCPServerConfig) -> None:
        self.server_key = server_key
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._next_id = 0
        self._base_headers = expand_header_env(config.headers)
        self._session_id = ""
        self._protocol_version = ""

    async def connect(self) -> None:
        parsed = urlparse(self.config.url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MCPConnectionError(f"MCP server {self.server_key} invalid streamable_http url")
        self._client = httpx.AsyncClient(
            base_url="",
            headers=self._base_headers,
            timeout=httpx.Timeout(float(self.config.connect_timeout_s or 20)),
        )
        log.info("mcp.http.connected", server=self.server_key, url=self._safe_url(), headers=redact_headers(self._base_headers))

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float) -> Any:
        if self._client is None:
            raise MCPConnectionError(f"MCP server {self.server_key} is not connected")
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": _JSONRPC_VERSION, "id": request_id, "method": method, "params": params or {}}
        try:
            resp = await self._client.post(
                self.config.url,
                json=payload,
                headers=self._request_headers(),
                timeout=float(timeout_s or self.config.tool_call_timeout_s or 120),
            )
        except TimeoutError as exc:
            raise MCPTimeoutError(f"MCP {self.server_key} {method} timeout after {timeout_s}s") from exc
        except httpx.TimeoutException as exc:
            raise MCPTimeoutError(f"MCP {self.server_key} {method} timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise MCPConnectionError(f"MCP {self.server_key} HTTP error: {type(exc).__name__}: {exc}") from exc
        self._capture_session_headers(resp)
        if resp.status_code >= 400:
            detail = redact_text_secrets(resp.text[:500])
            raise MCPConnectionError(f"MCP {self.server_key} HTTP {resp.status_code}: {detail}")
        messages = _decode_http_rpc_messages(resp)
        for item in messages:
            if item.get("id") is None:
                self._dispatch_notification(str(item.get("method") or ""), item.get("params"))
        msg = _select_rpc_response(messages, request_id=request_id)
        if "error" in msg:
            raise MCPToolCallError(_rpc_error_text(msg.get("error")))
        return msg.get("result")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._client is None:
            return
        payload = {"jsonrpc": _JSONRPC_VERSION, "method": method, "params": params or {}}
        with contextlib.suppress(Exception):
            resp = await self._client.post(self.config.url, json=payload, headers=self._request_headers(), timeout=5.0)
            self._capture_session_headers(resp)

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session_id:
            headers[_SESSION_HEADER] = self._session_id
        if self._protocol_version:
            headers[_PROTOCOL_HEADER] = self._protocol_version
        return headers

    def _capture_session_headers(self, resp: httpx.Response) -> None:
        session_id = resp.headers.get(_SESSION_HEADER) or resp.headers.get(_SESSION_HEADER.lower())
        if session_id:
            self._session_id = session_id.strip()
        protocol_version = resp.headers.get(_PROTOCOL_HEADER) or resp.headers.get(_PROTOCOL_HEADER.lower())
        if protocol_version:
            self._protocol_version = protocol_version.strip()

    def _safe_url(self) -> str:
        parsed = urlparse(self.config.url.strip())
        if not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _decode_http_rpc_response(resp: httpx.Response, *, request_id: int | str | None = None) -> dict[str, Any]:
    return _select_rpc_response(_decode_http_rpc_messages(resp), request_id=request_id)


def _decode_http_rpc_messages(resp: httpx.Response) -> list[dict[str, Any]]:
    ctype = resp.headers.get("content-type", "").lower()
    text = resp.text
    messages: list[dict[str, Any]] = []
    if "text/event-stream" in ctype or text.lstrip().startswith("event:") or "\ndata:" in text or text.lstrip().startswith("data:"):
        for item in _sse_data_items(text):
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(item)
                if isinstance(parsed, dict):
                    messages.append(parsed)
                elif isinstance(parsed, list):
                    messages.extend(x for x in parsed if isinstance(x, dict))
        if not messages:
            raise MCPConnectionError("invalid event-stream JSON-RPC response")
    else:
        try:
            parsed = resp.json()
        except Exception as exc:
            raise MCPConnectionError(f"invalid JSON-RPC response: {type(exc).__name__}") from exc
        if isinstance(parsed, list):
            messages = [x for x in parsed if isinstance(x, dict)]
        elif isinstance(parsed, dict):
            messages = [parsed]
        else:
            raise MCPConnectionError("invalid JSON-RPC response shape")
    return messages


def _sse_data_items(text: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if current:
                joined = "\n".join(current).strip()
                if joined and joined != "[DONE]":
                    items.append(joined)
                current = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            current.append(line[5:].lstrip())
    if current:
        joined = "\n".join(current).strip()
        if joined and joined != "[DONE]":
            items.append(joined)
    return items


def _select_rpc_response(messages: list[dict[str, Any]], *, request_id: int | str | None = None) -> dict[str, Any]:
    if not messages:
        raise MCPConnectionError("empty JSON-RPC response")
    if request_id is not None:
        wanted = str(request_id)
        for msg in messages:
            mid = msg.get("id")
            if mid is not None and str(mid) == wanted:
                return msg
        # Some servers send progress notifications before the final response; missing
        # matching id is safer to treat as protocol error than returning a notification.
        raise MCPConnectionError(f"JSON-RPC response id mismatch: expected {request_id}")
    for msg in reversed(messages):
        if msg.get("id") is not None:
            return msg
    return messages[-1]


def _rpc_error_text(error: Any) -> str:
    if isinstance(error, dict):
        code = error.get("code", "")
        message = error.get("message", "")
        data = error.get("data", "")
        rendered = f"JSON-RPC error {code}: {message}".strip()
        if data not in (None, ""):
            rendered += f" ({str(data)[:500]})"
        return rendered
    return str(error or "JSON-RPC error")


def make_transport(server_key: str, config: MCPServerConfig) -> MCPTransport:
    transport = str(config.transport or "stdio").strip().lower()
    if transport == "stdio":
        return StdioJSONRPCTransport(server_key, config)
    if transport == "streamable_http":
        return StreamableHTTPTransport(server_key, config)
    raise MCPConnectionError(f"unsupported MCP transport: {transport}")
