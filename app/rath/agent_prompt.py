"""Rath Agent base system prompt rendering helpers."""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

from app.db.engine import DB
from app.memory.builtin import BuiltinMemoryClient
from app.tools.allowlist import AGENT_DELEGATION_TOOL_NAMES, sanitize_tool_allowlist
from app.tools.base import ToolRegistry


def allowed_agent_tool_names(registry: ToolRegistry | None, tool_allowlist: list[str] | tuple[str, ...] | None) -> list[str]:
    if registry is None:
        return []
    requested = set(sanitize_tool_allowlist(tool_allowlist or [])) & set(AGENT_DELEGATION_TOOL_NAMES)
    if not requested:
        return []
    available = set(registry.names(scope="agent")) & set(AGENT_DELEGATION_TOOL_NAMES)
    return [name for name in registry.names(scope="agent") if name in requested and name in available]


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
        "builtinToolNames": tool_names,
        "builtinToolSummaries": tool_summaries,
        "tools": {
            "allowlist": tool_names,
            "summaries": tool_summaries,
            "builtin": {"names": tool_names, "summaries": tool_summaries},
        },
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
