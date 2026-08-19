"""MCP v0 structured errors."""
from __future__ import annotations


class MCPError(Exception):
    """Base class for MCP connector errors."""


class MCPConfigError(MCPError):
    """Invalid MCP configuration."""


class MCPConnectionError(MCPError):
    """MCP server connection failed."""


class MCPInitializeError(MCPConnectionError):
    """MCP initialize failed."""


class MCPToolListError(MCPConnectionError):
    """MCP tools/list failed."""


class MCPToolCallError(MCPError):
    """MCP tools/call failed."""


class MCPTimeoutError(MCPError):
    """MCP operation timed out."""


class MCPPermissionDenied(MCPError):
    """MCP tool call denied by OpenBear policy."""


class MCPServerExited(MCPConnectionError):
    """MCP server process exited unexpectedly."""
