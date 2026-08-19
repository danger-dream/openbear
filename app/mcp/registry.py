"""MCP ToolRegistry adapter."""
from __future__ import annotations

from app.mcp.manager import MCPManager
from app.tools.base import ToolRegistry, current_tool_context


async def make_mcp_tool_call(manager: MCPManager, public_name: str, args: dict) -> str:
    ctx = current_tool_context()
    return await manager.call_tool(public_name, args or {}, ctx)


def register_mcp_tools(registry: ToolRegistry, manager: MCPManager) -> int:
    count = 0
    for meta in manager.available_tools():
        registry.add(
            meta.public_name,
            meta.description,
            meta.input_schema,
            lambda args, name=meta.public_name: make_mcp_tool_call(manager, name, args),
            source="mcp",
        )
        count += 1
    return count
