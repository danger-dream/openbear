"""OpenBear MCP v0 client connector.

MCP servers are treated as untrusted external tool providers.  The modules in this
package keep protocol, lifecycle, permissions and output governance behind a
normal ToolRegistry adapter so the main Agent loop does not need MCP-specific
branches.
"""
from __future__ import annotations

from app.mcp.manager import MCPManager

__all__ = ["MCPManager"]
