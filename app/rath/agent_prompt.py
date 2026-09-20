"""Rath Agent base system prompt rendering helpers."""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

from app.db.engine import DB
from app.memory.builtin import BuiltinMemoryClient
from app.tools.allowlist import (
    agent_delegation_names,
    expand_agent_tool_names,
    sanitize_tool_allowlist,
)
from app.tools.base import ToolRegistry


def allowed_agent_tool_names(registry: ToolRegistry | None, tool_allowlist: list[str] | tuple[str, ...] | None) -> list[str]:
    if registry is None:
        return []
    requested = set(sanitize_tool_allowlist(tool_allowlist or [])) & agent_delegation_names(registry)
    if not requested:
        return []
    requested = expand_agent_tool_names(requested)
    return [name for name in registry.names(scope="agent") if name in requested]


def agent_system_prompt_params(
    registry: ToolRegistry | None,
    *,
    tool_allowlist: list[str] | tuple[str, ...] | None,
    model_name: str = "",
    workspace_dir: str = "",
) -> dict[str, Any]:
    tool_names = allowed_agent_tool_names(registry, tool_allowlist)
    all_summaries = registry.summaries(scope="agent") if registry is not None else {}
    tool_summaries = {name: str(all_summaries.get(name) or "") for name in tool_names}
    builtin_names = [name for name in tool_names if name in registry.names(source="builtin")] if registry else []
    builtin_summaries = {name: tool_summaries[name] for name in builtin_names}
    mcp_names = [name for name in tool_names if name not in builtin_names]
    manager = getattr(registry, "mcp_manager", None)
    servers = {meta.server_key for meta in manager.agent_tools() if meta.public_name in mcp_names} if manager else set()
    instructions = [row for row in manager.server_instructions_snapshot() if row["server"] in servers] if manager else []
    host = {
        "hostname": platform.node(),
        "os": platform.system(),
        "arch": platform.machine(),
        "platform": platform.platform(),
    }
    workspace = str(workspace_dir or Path.cwd())
    return {
        "toolNames": tool_names,
        "toolSummaries": tool_summaries,
        "builtinToolNames": builtin_names,
        "builtinToolSummaries": builtin_summaries,
        "tools": {
            "allowlist": tool_names,
            "summaries": tool_summaries,
            "builtin": {"names": builtin_names, "summaries": builtin_summaries},
            "mcp": {"names": mcp_names, "summaries": {name: tool_summaries[name] for name in mcp_names}},
        },
        "mcpToolNames": mcp_names,
        "mcpToolSummaries": {name: tool_summaries[name] for name in mcp_names},
        "mcpServerInstructions": instructions,
        "workspaceDir": workspace,
        "host": host,
        "runtimeInfo": {
            "channel": "rath_agent",
            "primaryInterface": "rath_agent",
            "outputFormat": "markdown",
            "model": model_name,
            "host": host["hostname"],
            "hostname": host["hostname"],
            "os": host["os"],
            "arch": host["arch"],
            "shell": os.environ.get("SHELL", ""),
        },
        "templateEngine": {
            "name": "openbear-template-lite",
            "supportedSyntax": ["[[ expr ]]", "@if/@else/@endif", "@each/@endeach", "@raw/@endraw"],
        },
        "outputFormat": "markdown",
        "defaultThinkLevel": "off",
        "reasoningLevel": "off",
    }


async def render_agent_base_system_prompt(
    db: DB,
    *,
    identity: str,
    registry: ToolRegistry | None,
    tool_allowlist: list[str] | tuple[str, ...] | None,
    model_name: str = "",
    workspace_dir: str = "",
) -> str:
    mem = BuiltinMemoryClient(db, identity=identity)
    params = agent_system_prompt_params(
        registry,
        tool_allowlist=tool_allowlist,
        model_name=model_name,
        workspace_dir=workspace_dir,
    )
    return (await mem.render_agent_system_prompt(params)).strip()
