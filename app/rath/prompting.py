"""Prompt helpers shared by OpenBear and Rath Agents."""
from __future__ import annotations

from typing import Any

from app.rath.dao import RathDAO
from app.rath.schemas import RathAgentDef


def agent_prompt_item(agent: RathAgentDef) -> dict[str, Any]:
    """Return the compact Agent descriptor exposed to prompt templates."""
    description = str(agent.description or "")
    return {
        "id": int(agent.id or 0),
        "key": agent.agent_key,
        "agentKey": agent.agent_key,
        "name": agent.name,
        "description": description,
        "scenario": description,
        "allowedTools": [str(name) for name in (agent.tool_allowlist or []) if str(name).strip()],
        "allowedToolsText": ", ".join(str(name) for name in (agent.tool_allowlist or []) if str(name).strip()) or "no additional preset restriction",
    }


async def available_agent_prompt_items(dao: RathDAO) -> list[dict[str, Any]]:
    agents = await dao.list_agents(include_disabled=False)
    return [agent_prompt_item(agent) for agent in agents if agent.enabled]
