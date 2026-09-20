"""MCP ToolRegistry adapter."""
from __future__ import annotations

from app.mcp.manager import MCPManager
from app.tools.base import ToolRegistry, current_tool_context


async def make_mcp_tool_call(manager: MCPManager, public_name: str, args: dict, identity: tuple[str, str] | None = None) -> str:
    ctx = current_tool_context()
    return await manager.call_tool(public_name, args or {}, ctx, expected_identity=identity)


def register_mcp_tools(registry: ToolRegistry, manager: MCPManager) -> int:
    registry.mcp_manager = manager
    count = 0
    for meta in manager.available_tools():
        identity = (meta.server_key, meta.original_tool_name)
        registry.mcp_tool_identities[meta.public_name] = identity
        registry.add(
            meta.public_name,
            meta.description,
            meta.input_schema,
            lambda args, name=meta.public_name, identity=identity: make_mcp_tool_call(manager, name, args, identity),
            source="mcp",
            visibility={"main", "runtime"} | ({"agent"} if not manager.agent_tool_unavailable_reason(meta.public_name) else set()),
        )
        count += 1
    return count
