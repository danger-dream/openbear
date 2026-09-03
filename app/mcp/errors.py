"""MCP v0 structured errors."""
from __future__ import annotations


class MCPError(Exception):
    """Base class for MCP connector errors."""


class MCPConfigError(MCPError):
    """Invalid MCP configuration."""


class MCPConnectionError(MCPError):
    """MCP server connection failed."""


class MCPAuthRequired(MCPConnectionError):
    """The remote MCP resource requires an OAuth bearer token."""

    def __init__(self, message: str = "MCP OAuth authorization required", *, resource_metadata_url: str = "") -> None:
        super().__init__(message)
        self.resource_metadata_url = resource_metadata_url


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
