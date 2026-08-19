"""ContextBuilder —— 组装 system + history + 本轮 user。

[system] ← prompt-memory /system-prompt/build（传入 toolNames/skillsPrompt/workspaceDir 等参数）
[history] ← SQLite（safeguard 压缩见 compaction）
[user]    ← 本轮消息 + [⏰ 当前时间] 后缀
"""
from __future__ import annotations

import os
import platform
from typing import TYPE_CHECKING, Any

from app.agent.transcript_repair import (
    build_summary_prefixed_history,
    build_summary_prefixed_visible_history,
    repair_tool_pairing,
)
from app.db.dao import MessageDAO, SummaryDAO
from app.llm.base import Message
from app.logging import get_logger
from app.memory.client import MemoryClient
from app.rath.controller_projection import project_history_message_for_controller
from app.tools.base import ToolRegistry
from app.tools.skills import Skill, render_skills_block
from app.utils import now_cn

if TYPE_CHECKING:
    from app.rath.dao import RathDAO

log = get_logger("context.builder")

_FALLBACK_SYSTEM = (
    "你是 OpenBear，一个运行在私有 Web 控制台里的单人自用智能助理。"
    "你可以使用文件工具（Read/Write/Edit）、Bash 执行命令、记忆工具读写长期记忆。"
    "请用中文、简洁、专业地完成用户交代的任务。"
)


def _mcp_tool_groups(tool_names: list[str]) -> list[dict[str, Any]]:
    """Build a compact, template-friendly MCP catalog from public tool names."""
    grouped: dict[tuple[str, str], list[str]] = {}
    for raw_name in tool_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[0] and parts[1]:
            server = parts[1]
            namespace = f"{parts[0]}__{parts[1]}__"
        else:
            server = "mcp"
            namespace = ""
        grouped.setdefault((server, namespace), []).append(name)
    out: list[dict[str, Any]] = []
    for (server, namespace), names in grouped.items():
        count = len(names)
        out.append({
            "server": server,
            "toolCount": count,
            "count": count,
            "namespacePrefix": namespace if count > 1 else "",
            "exactToolName": names[0] if count == 1 else "",
        })
    return out


def build_system_prompt_params(
    *,
    tool_names: list[str] | None = None,
    tool_summaries: dict[str, str] | None = None,
    builtin_tool_names: list[str] | None = None,
    builtin_tool_summaries: dict[str, str] | None = None,
    mcp_tool_names: list[str] | None = None,
    mcp_tool_summaries: dict[str, str] | None = None,
    mcp_tool_groups: list[dict[str, Any]] | None = None,
    mcp_server_instructions: list[dict[str, str]] | None = None,
    skills_prompt: str = "",
    workspace_dir: str = "",
    current_model: str = "",
    default_think_level: str = "off",
    reasoning_level: str = "off",
    available_agents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mcp_names = list(mcp_tool_names or [])
    mcp_summaries = dict(mcp_tool_summaries or {})
    mcp_groups = [dict(item) for item in mcp_tool_groups] if mcp_tool_groups is not None else _mcp_tool_groups(mcp_names)
    builtin_names = list(builtin_tool_names) if builtin_tool_names is not None else list(tool_names or [])
    builtin_summaries = dict(builtin_tool_summaries) if builtin_tool_summaries is not None else dict(tool_summaries or {})
    server_instructions = [dict(item) for item in (mcp_server_instructions or []) if str(item.get("instructions") or "").strip()]
    agent_items = list(available_agents or [])
    all_tool_names = list(tool_names or builtin_names)
    all_tool_summaries = dict(tool_summaries or builtin_summaries)
    host = {
        "hostname": platform.node(),
        "os": platform.system(),
        "arch": platform.machine(),
        "platform": platform.platform(),
    }
    return {
        "toolNames": all_tool_names,
        "toolSummaries": all_tool_summaries,
        "tools": {
            "allowlist": all_tool_names,
            "summaries": all_tool_summaries,
            "builtin": {
                "allowlist": builtin_names,
                "summaries": builtin_summaries,
            },
            "mcp": {
                "allowlist": mcp_names,
                "summaries": mcp_summaries,
                "groups": mcp_groups,
                "serverInstructions": server_instructions,
            },
        },
        "builtinToolNames": builtin_names,
        "builtinToolSummaries": builtin_summaries,
        "mcpToolNames": mcp_names,
        "mcpToolSummaries": mcp_summaries,
        "mcpToolGroups": mcp_groups,
        "mcpServerInstructions": server_instructions,
        "availableAgents": agent_items,
        "agents": {
            "available": agent_items,
        },
        "skillsPrompt": skills_prompt,
        "workspaceDir": workspace_dir,
        "host": host,
        "runtimeInfo": {
            "channel": "web",
            "primaryInterface": "web_console",
            "outputFormat": "markdown",
            "model": current_model,
            "host": host["hostname"],
            "hostname": host["hostname"],
            "os": host["os"],
            "arch": host["arch"],
            "shell": os.environ.get("SHELL", ""),
            "web": {
                "primaryInterface": "web_console",
                "outputFormat": "markdown",
                "rendering": "browser_markdown",
            },
        },
        "templateEngine": {
            "name": "builtin-prompt-memory-compatible",
            "supportedSyntax": ["[[ expr ]]", "@if/@else/@endif", "@each/@endeach", "@raw/@endraw"],
        },
        "outputFormat": "markdown",
        "defaultThinkLevel": default_think_level,
        "reasoningLevel": reasoning_level,
    }


class ContextBuilder:
    def __init__(self, mem: MemoryClient, messages: MessageDAO, summaries: SummaryDAO,
                 skills: list[Skill], tools: ToolRegistry, workspace_dir: str,
                 rath_dao: RathDAO | None = None, mcp_manager: Any = None) -> None:
        self._mem = mem
        self._messages = messages
        self._summaries = summaries
        self._skills = skills
        self._tools = tools
        self._workspace_dir = workspace_dir
        self._rath_dao = rath_dao
        self._mcp_manager = mcp_manager

    async def _available_agents_for_prompt(self) -> list[dict[str, Any]]:
        if self._rath_dao is None:
            return []
        try:
            from app.rath.prompting import available_agent_prompt_items

            return await available_agent_prompt_items(self._rath_dao)
        except Exception as exc:
            log.warning("获取可用 Agent 提示词参数失败", 错误=str(exc)[:120])
            return []

    def _mcp_server_instructions_for_prompt(self) -> list[dict[str, str]]:
        manager = self._mcp_manager
        if manager is None or not hasattr(manager, "server_instructions_snapshot"):
            return []
        try:
            raw = manager.server_instructions_snapshot()
        except Exception as exc:
            log.warning("获取 MCP Server Instructions 失败", 错误=str(exc)[:120])
            return []
        out: list[dict[str, str]] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            server = str(item.get("server") or "").strip()
            instructions = str(item.get("instructions") or "").strip()
            if instructions:
                out.append({"server": server or "mcp-server", "instructions": instructions})
        return out

    async def build_system(self, *, current_model: str = "") -> str:
        """拉系统提示词（失败降级到最小兜底）。

        toolNames / skillsPrompt / workspaceDir 等全部作为参数传给模板引擎，
        由模板动态渲染，不在这里拼接。
        """
        try:
            params = build_system_prompt_params(
                tool_names=self._tools.names(scope="main"),
                tool_summaries=self._tools.summaries(scope="main"),
                builtin_tool_names=self._tools.names(scope="main", source="builtin"),
                builtin_tool_summaries=self._tools.summaries(scope="main", source="builtin"),
                mcp_tool_names=self._tools.names(scope="main", source="mcp"),
                mcp_tool_summaries=self._tools.summaries(scope="main", source="mcp"),
                mcp_server_instructions=self._mcp_server_instructions_for_prompt(),
                skills_prompt=render_skills_block(self._skills),
                workspace_dir=self._workspace_dir,
                current_model=current_model,
                available_agents=await self._available_agents_for_prompt(),
            )
            prompt = await self._mem.build_system_prompt(params)
            if not prompt.strip():
                raise ValueError("空提示词")
        except Exception as e:
            log.warning("拉取系统提示词失败，降级兜底", 错误=str(e)[:120])
            prompt = _FALLBACK_SYSTEM
        return prompt

    async def build_history(self, chat_id: int) -> list[Message]:
        """从 DB 构造历史消息；压缩后只回放可见文本 XML 尾部。"""
        summary = await self._summaries.latest(chat_id)
        summary_text = str((summary or {}).get("summary") or "")
        if summary_text:
            visible_rows = await self._messages.recent_visible_history(chat_id)
            return build_summary_prefixed_visible_history(summary_text, visible_rows)
        rows = await self._messages.recent(chat_id)
        recent = [project_history_message_for_controller(r.to_message()) for r in rows]
        history = build_summary_prefixed_history("", recent)
        # 发往上游前做工具配对净化:光杆 tool_call 补占位、孤儿/重复 tool 结果清理。
        # 这是所有 convo 的唯一收口,屏蔽存量脏历史 + 兜住任何中断残留(见 transcript_repair)。
        return repair_tool_pairing(history)

    def wrap_user(self, text) -> Message:
        if isinstance(text, list):
            blocks = [dict(b) if isinstance(b, dict) else b for b in text]
            now_block = {"type": "text", "text": f"[⏰ 当前时间: {now_cn()}]"}
            if blocks and isinstance(blocks[-1], dict) and blocks[-1].get("type") == "text":
                block = dict(blocks[-1])
                block["text"] = f"{block.get('text') or ''}\n\n{now_block['text']}"
                blocks[-1] = block
            else:
                # 多模态用户消息常以 image block 结尾；补一个末尾 text block，避免
                # Anthropic prompt-cache 断点被加到图片块上，并保持时间后缀可读。
                blocks.append(now_block)
            return {"role": "user", "content": blocks}
        return {"role": "user", "content": f"{text}\n\n[⏰ 当前时间: {now_cn()}]"}
