"""Runtime types for OpenBear MCP v0."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MCPServerState(BaseModel):
    key: str
    transport: str
    status: str
    tool_count: int = 0
    required: bool = False
    error: str = ""
    last_connected_at: float | None = None
    last_failed_at: float | None = None
    approval: str = "ask"


class MCPManagerState(BaseModel):
    enabled: bool = False
    servers: list[MCPServerState] = Field(default_factory=list)


class MCPRawTool(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)


class MCPContentBlock(BaseModel):
    type: str = "text"
    text: str = ""
    data: str = ""
    mime_type: str = Field(default="", alias="mimeType")
    resource: dict[str, Any] | None = None

    model_config = {"populate_by_name": True, "extra": "allow"}


class MCPRawResult(BaseModel):
    content: list[Any] = Field(default_factory=list)
    structured_content: Any = Field(default=None, alias="structuredContent")
    is_error: bool = Field(default=False, alias="isError")
    raw: Any = None

    model_config = {"populate_by_name": True, "extra": "allow"}


class MCPToolMeta(BaseModel):
    public_name: str
    server_key: str
    original_tool_name: str
    normalized_tool_name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    approval: str = "ask"
    risk: str = "unknown"
    filtered: bool = False
    filter_reason: str = ""
