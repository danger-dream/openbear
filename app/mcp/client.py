"""MCP client protocol wrapper."""
from __future__ import annotations

from typing import Any

from app.config import MCPServerConfig
from app.mcp.errors import MCPInitializeError, MCPToolCallError, MCPToolListError
from app.mcp.transports import MCPTransport, NotificationHandler, make_transport
from app.mcp.types import MCPRawResult, MCPRawTool

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "openbear", "version": "0.1.0"}


class MCPClient:
    def __init__(self, server_key: str, config: MCPServerConfig, transport: MCPTransport | None = None) -> None:
        self.server_key = server_key
        self.config = config
        self.transport = transport or make_transport(server_key, config)
        self.initialized = False
        self.instructions = ""

    def set_notification_handler(self, handler: NotificationHandler | None) -> None:
        self.transport.set_notification_handler(handler)

    async def connect(self) -> None:
        await self.transport.connect()
        try:
            result = await self.transport.request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
                timeout_s=float(self.config.connect_timeout_s or 20),
            )
            if isinstance(result, dict):
                self.instructions = str(result.get("instructions") or "").strip()
            await self.transport.notify("notifications/initialized", {})
            self.initialized = True
        except Exception as exc:
            raise MCPInitializeError(f"MCP server {self.server_key} initialize failed: {type(exc).__name__}: {exc}") from exc

    async def list_tools(self) -> list[MCPRawTool]:
        try:
            raw_tools = await self._list_paginated("tools/list", "tools")
        except Exception as exc:
            raise MCPToolListError(f"MCP server {self.server_key} tools/list failed: {type(exc).__name__}: {exc}") from exc
        tools: list[MCPRawTool] = []
        seen_names: set[str] = set()
        for item in raw_tools:
            name = str(item.get("name") or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            schema = item.get("inputSchema") or item.get("input_schema") or {}
            annotations = item.get("annotations") or {}
            tools.append(MCPRawTool(
                name=name,
                description=str(item.get("description") or ""),
                input_schema=schema if isinstance(schema, dict) else {},
                annotations=annotations if isinstance(annotations, dict) else {},
            ))
        return tools

    async def list_prompts(self) -> list[dict[str, Any]]:
        return await self._list_paginated("prompts/list", "prompts")

    async def _list_paginated(self, method: str, item_key: str) -> list[dict[str, Any]]:
        """Collect every cursor page while rejecting loops and malformed responses."""
        timeout_s = float(self.config.connect_timeout_s or 20)
        cursor = ""
        seen_cursors: set[str] = set()
        items: list[dict[str, Any]] = []
        for _page in range(1000):
            params = {"cursor": cursor} if cursor else {}
            result = await self.transport.request(method, params, timeout_s=timeout_s)
            page_items = result.get(item_key) if isinstance(result, dict) else None
            if not isinstance(page_items, list):
                raise ValueError(f"{method} returned invalid shape")
            items.extend(item for item in page_items if isinstance(item, dict))
            next_cursor = str(result.get("nextCursor") or "").strip()
            if not next_cursor:
                return items
            if next_cursor in seen_cursors:
                raise ValueError(f"{method} returned repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise ValueError(f"{method} exceeded pagination limit")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPRawResult:
        try:
            result = await self.transport.request(
                "tools/call",
                {"name": name, "arguments": arguments or {}},
                timeout_s=float(self.config.tool_call_timeout_s or 120),
            )
        except Exception as exc:
            raise MCPToolCallError(f"MCP server {self.server_key} tool {name} failed: {type(exc).__name__}: {exc}") from exc
        if isinstance(result, dict):
            return MCPRawResult.model_validate({**result, "raw": result})
        return MCPRawResult(raw=result, content=[result])

    async def close(self) -> None:
        await self.transport.close()
