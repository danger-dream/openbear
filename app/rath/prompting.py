"""Prompt helpers shared by OpenBear and Rath Agents."""
from __future__ import annotations

from typing import Any

from app.rath.dao import RathDAO
from app.rath.schemas import RathAgentDef
from app.tools.allowlist import sanitize_tool_allowlist


def agent_prompt_item(agent: RathAgentDef) -> dict[str, Any]:
    """Return the compact Agent descriptor exposed to prompt templates."""
    description = str(agent.description or "")
    configured_tools = [str(name) for name in (agent.tool_allowlist or []) if str(name).strip()]
    available_tools = sanitize_tool_allowlist(configured_tools)
    if available_tools:
        tools_text = ", ".join(available_tools)
    elif configured_tools:
        tools_text = "no currently available tools (configured preset tools are unavailable)"
    else:
        tools_text = "no additional preset restriction"
    return {
        "id": int(agent.id or 0),
        "key": agent.agent_key,
        "agentKey": agent.agent_key,
        "name": agent.name,
        "description": description,
        "scenario": description,
        "allowedTools": available_tools,
        "allowedToolsText": tools_text,
    }


async def available_agent_prompt_items(dao: RathDAO) -> list[dict[str, Any]]:
    agents = await dao.list_agents(include_disabled=False)
    return [agent_prompt_item(agent) for agent in agents if agent.enabled]
