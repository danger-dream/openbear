"""Rath internal workflow route definitions."""
from __future__ import annotations

from typing import Any

SINGLE_AGENT_WORKFLOW_SLUG = "single-agent"

SINGLE_AGENT_WORKFLOW_CONFIG: dict[str, Any] = {
    "version": 1,
    "mode": "single-agent",
    "description": "Web 控制台注册的用户自定义 Agent 默认挂载在这里。",
}


async def ensure_builtin_workflows(dao) -> str:
    """Ensure the single supported Rath workflow exists; return its workflow_uuid."""
    return await dao.upsert_workflow(
        slug=SINGLE_AGENT_WORKFLOW_SLUG,
        name="Single Agent Tasks",
        description="用户在 Web 控制台注册的自定义 Agent 默认工作流。",
        kind="single-agent",
        config=SINGLE_AGENT_WORKFLOW_CONFIG,
        enabled=True,
    )
