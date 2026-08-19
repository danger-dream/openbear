"""OpenBear 控制平面工具。

模型需要控制 OpenBear 自身时，应调用本工具，而不是用 Bash 直接 systemctl/改库。
高风险动作会通过当前交互通道确认，并在需要时排到当前回合结束后执行。
"""
from __future__ import annotations

import json
from typing import Any

from app.logging import get_logger
from app.models.thinking import (
    available_think_levels,
    clamp_think_level,
    default_think_level,
    normalize_think_level,
)
from app.tools import processes
from app.tools.base import ToolRegistry, current_tool_context

log = get_logger("tools.openbear_control")

_HIGH_RISK_ACTIONS = {"restart", "new", "stop"}
_INTERACTIVE_SOURCES = {"web", "chat"}
_ACTIONS = {
    "status",
    "restart",
    "new",
    "stop",
    "think",
    "models",
    "mcp_status",
    "skills_status",
    "skills_reload",
    "mcp_reload",
}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _mask_key(value: str) -> str:
    value = value or ""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def _current_model_parts(svc: Any) -> tuple[str, str, bool]:
    current = svc.selection.current
    resolved = svc.config.models.resolve(current)
    if not resolved:
        return "", current, False
    provider, model = resolved
    return provider.protocol, model.id, model.reasoning


async def _resolved_think_level(svc: Any, chat_id: int) -> str:
    protocol, model_id, reasoning = _current_model_parts(svc)
    stored = normalize_think_level(await svc.messages.get_thinking_level(chat_id))
    level = stored or default_think_level(protocol=protocol, model_id=model_id, reasoning=reasoning)
    return clamp_think_level(level, protocol, model_id)


async def _confirm_if_needed(
    svc: Any,
    *,
    action: str,
    reason: str,
    body: str,
    confirm_text: str,
) -> dict[str, Any]:
    del svc
    ctx = current_tool_context()
    if ctx.web_confirm is None:
        return {"status": "error", "confirmed": False, "error": "web_confirmation_not_available"}
    result = await ctx.web_confirm({
        "title": f"确认执行：{action}",
        "body": body + (f"\n\n原因：{reason}" if reason else ""),
        "type": "warning" if action in {"restart", "new", "stop"} else "info",
        "default": False,
        "confirmText": confirm_text,
        "cancelText": "取消",
        "timeoutSeconds": 600,
    })
    return result


async def _action_status(svc: Any, chat_id: int) -> str:
    session_uuid = await svc.messages.current_session_uuid(chat_id)
    think = await _resolved_think_level(svc, chat_id)
    data = {
        "status": "ok",
        "action": "status",
        "service": {
            "startedAt": int(svc.started_at),
            "runningRuns": svc.runs.count(),
            "runningThisChat": svc.runs.is_running(chat_id),
            "rathTasks": svc.rath.count() if getattr(svc, "rath", None) is not None else 0,
            "childProcesses": processes.count(),
            "postTurnActions": svc.control_actions.pending_count(chat_id),
        },
        "session": {
            "chatId": chat_id,
            "sessionUuid": session_uuid,
            "model": svc.selection.current,
            "thinkLevel": think,
        },
    }
    return _json(data)


async def _action_models(svc: Any) -> str:
    providers: dict[str, Any] = {}
    for name, provider in sorted(svc.config.models.providers.items()):
        providers[name] = {
            "enabled": provider.enabled,
            "protocol": provider.protocol,
            "baseUrl": provider.base_url,
            "apiKey": _mask_key(provider.api_key),
            "models": [
                {
                    "id": model.id,
                    "name": model.name,
                    "reasoning": model.reasoning,
                    "thinkingLevels": list(getattr(model, "thinking_levels", []) or []),
                    "defaultThinkingLevel": str(getattr(model, "default_thinking_level", "") or ""),
                    "supportsFast": bool(getattr(model, "supports_fast", False)),
                    "compactTriggerTokens": int(getattr(model, "compact_trigger_tokens", 0) or 0),
                    "input": model.input,
                    "contextWindow": model.context_window,
                    "maxTokens": model.max_tokens,
                }
                for model in provider.models
            ],
        }
    return _json({
        "status": "ok",
        "action": "models",
        "primary": svc.config.models.primary,
        "current": svc.selection.current,
        "compressionModels": list(getattr(svc.config.models, "compression_models", []) or []),
        "providers": providers,
    })


async def _action_mcp_status(svc: Any) -> str:
    manager = getattr(svc, "mcp", None)
    if manager is None:
        return _json({"status": "ok", "action": "mcp_status", "enabled": False, "servers": [], "tools": []})
    snapshot = manager.status_snapshot()
    tools = [
        {
            "publicName": meta.public_name,
            "serverKey": meta.server_key,
            "originalToolName": meta.original_tool_name,
            "risk": meta.risk,
            "approval": meta.approval,
            "visible": not meta.filtered,
            "filtered": meta.filtered,
            "filterReason": meta.filter_reason,
        }
        for meta in manager.all_tools_snapshot()
    ]
    return _json({
        "status": "ok",
        "action": "mcp_status",
        "enabled": snapshot.enabled,
        "servers": [server.model_dump() for server in snapshot.servers],
        "tools": tools,
        "note": "MCP 配置支持热重载；变更后会自动重连 server 并重建工具列表。",
    })


def _skill_item(skill: Any) -> dict[str, Any]:
    metadata = getattr(skill, "metadata", None)
    requires = getattr(metadata, "requires", None)
    return {
        "name": str(getattr(skill, "name", "") or ""),
        "description": str(getattr(skill, "description", "") or ""),
        "location": str(getattr(skill, "location", "") or ""),
        "baseDir": str(getattr(skill, "base_dir", "") or ""),
        "enabled": bool(getattr(skill, "enabled", True)),
        "always": bool(getattr(metadata, "always", False)) if metadata is not None else False,
        "emoji": str(getattr(metadata, "emoji", "") or "") if metadata is not None else "",
        "homepage": str(getattr(metadata, "homepage", "") or "") if metadata is not None else "",
        "skillKey": str(getattr(metadata, "skill_key", "") or "") if metadata is not None else "",
        "requires": {
            "bins": list(getattr(requires, "bins", []) or []) if requires is not None else [],
            "env": list(getattr(requires, "env", []) or []) if requires is not None else [],
        },
    }


async def _action_skills_status(svc: Any, args: dict[str, Any]) -> str:
    query = str(args.get("query") or args.get("q") or args.get("name") or "").strip().lower()
    skills = list(getattr(svc, "skills", []) or [])
    rows: list[dict[str, Any]] = []
    for skill in skills:
        item = _skill_item(skill)
        haystack = "\n".join([
            item.get("name", ""),
            item.get("description", ""),
            item.get("location", ""),
            item.get("skillKey", ""),
        ]).lower()
        if query and query not in haystack:
            continue
        rows.append(item)
    config = getattr(svc, "config", None)
    tools_config = getattr(config, "tools", None)
    disabled = list(getattr(tools_config, "disabled_skills", []) or []) if tools_config is not None else []
    return _json({
        "status": "ok",
        "action": "skills_status",
        "skillsDir": str(getattr(tools_config, "skills_dir", "") or "") if tools_config is not None else "",
        "loadedCount": len(skills),
        "matchedCount": len(rows),
        "disabledSkills": disabled,
        "query": query,
        "skills": rows,
        "effective": "current_context",
        "note": "返回的是当前运行时已加载的 skills；修改磁盘文件后请先执行 skills_reload，新回合会使用刷新后的 skills/context。",
    })


async def _action_skills_reload(svc: Any) -> str:
    reload_fn = getattr(svc, "reload_skills_from_disk", None)
    if not callable(reload_fn):
        return _json({"status": "error", "action": "skills_reload", "error": "skills_reload_not_available"})
    result = reload_fn()
    if hasattr(result, "__await__"):
        result = await result
    if not isinstance(result, dict):
        return _json({"status": "ok", "action": "skills_reload", "result": str(result), "effective": "next_turn"})
    result.setdefault("status", "ok" if result.get("ok", True) else "error")
    result.setdefault("action", "skills_reload")
    result.setdefault("effective", "next_turn")
    return _json(result)


async def _action_mcp_reload(svc: Any, reason: str) -> str:
    confirm = await _confirm_if_needed(
        svc,
        action="mcp_reload",
        reason=reason,
        body=(
            "这会从磁盘重新读取 MCP 配置，可能启动、重连或关闭配置中的 MCP server，"
            "并重建后续回合可用的 MCP 工具列表。"
        ),
        confirm_text="确认重载 MCP",
    )
    if not confirm.get("confirmed"):
        return _json({"status": "cancelled", "action": "mcp_reload", "confirmation": confirm})
    reload_fn = getattr(svc, "reload_mcp_from_disk", None)
    if not callable(reload_fn):
        return _json({"status": "error", "action": "mcp_reload", "error": "mcp_reload_not_available"})
    result = reload_fn()
    if hasattr(result, "__await__"):
        result = await result
    if not isinstance(result, dict):
        return _json({"status": "ok", "action": "mcp_reload", "result": str(result), "effective": "next_turn"})
    result.setdefault("status", "ok" if result.get("ok", True) else "error")
    result.setdefault("action", "mcp_reload")
    result.setdefault("effective", "next_turn")
    result.setdefault("note", "MCP reload may reconnect/start configured MCP servers; new turns use refreshed MCP tools/context.")
    return _json(result)


async def _action_think(svc: Any, chat_id: int, args: dict[str, Any]) -> str:
    requested_raw = str(args.get("level") or args.get("thinkLevel") or args.get("value") or "").strip()
    if not requested_raw:
        current = await _resolved_think_level(svc, chat_id)
        protocol, model_id, _reasoning = _current_model_parts(svc)
        return _json({
            "status": "ok",
            "action": "think",
            "current": current,
            "model": svc.selection.current,
            "available": available_think_levels(protocol, model_id),
        })
    protocol, model_id, reasoning = _current_model_parts(svc)
    requested = normalize_think_level(requested_raw)
    if requested is None:
        return _json({"status": "error", "action": "think", "error": "invalid_level", "received": requested_raw})
    available = available_think_levels(protocol, model_id)
    if requested not in available:
        return _json({
            "status": "error",
            "action": "think",
            "error": "unsupported_level_for_current_model",
            "received": requested,
            "model": svc.selection.current,
            "available": available,
        })
    await svc.messages.set_thinking_level(chat_id, requested)
    return _json({
        "status": "ok",
        "action": "think",
        "level": requested,
        "model": svc.selection.current,
        "modelReasoningFlag": reasoning,
    })


async def _action_restart(svc: Any, chat_id: int, args: dict[str, Any], reason: str) -> str:
    confirm = await _confirm_if_needed(
        svc,
        action="restart",
        reason=reason,
        body=(
            "这会重启 OpenBear 服务，当前回复会先完成收尾，随后由 systemd-run 延迟执行重启。\n"
            f"运行中 OpenBear 回合：{svc.runs.count()}\n"
            f"Rath 任务：{svc.rath.count() if getattr(svc, 'rath', None) is not None else 0}\n"
            f"子进程：{processes.count()}"
        ),
        confirm_text="确认重启",
    )
    if not confirm.get("confirmed"):
        return _json({"status": "cancelled", "action": "restart", "confirmation": confirm})
    svc.control_actions.enqueue_after_turn(
        chat_id,
        "restart",
        {"delayS": float(args.get("delayS") or args.get("delay_s") or 1.0)},
        reason=reason,
        requested_by="OpenBearControl",
    )
    return _json({"status": "scheduled", "action": "restart", "when": "after_current_turn"})


async def _action_new(svc: Any, chat_id: int, reason: str) -> str:
    confirm = await _confirm_if_needed(
        svc,
        action="new",
        reason=reason,
        body="这会在当前回复完成后清空当前会话；下一条消息会进入新会话。",
        confirm_text="确认新建会话",
    )
    if not confirm.get("confirmed"):
        return _json({"status": "cancelled", "action": "new", "confirmation": confirm})
    svc.control_actions.enqueue_after_turn(
        chat_id,
        "new",
        {},
        reason=reason,
        requested_by="OpenBearControl",
    )
    return _json({"status": "scheduled", "action": "new", "when": "after_current_turn"})


async def _action_stop(svc: Any, chat_id: int, args: dict[str, Any], reason: str) -> str:
    target = str(args.get("target") or "current_run").strip() or "current_run"
    if target in {"rath_task", "task", "agent_task", "agent", "all_tasks"}:
        return _json({
            "status": "error",
            "error": "use_AgentStop_for_agent_tasks",
            "action": "stop",
            "target": target,
            "message": "Rath/Agent task 停止请使用 AgentStop；OpenBearControl 只控制 OpenBear 主控轮/控制面。",
        })
    confirm = await _confirm_if_needed(
        svc,
        action="stop",
        reason=reason,
        body=f"这会停止目标：{target}。当前回复会尽量优雅收尾。",
        confirm_text="确认停止",
    )
    if not confirm.get("confirmed"):
        return _json({"status": "cancelled", "action": "stop", "target": target, "confirmation": confirm})
    stopped_run = False
    if target in {"current_run", "all"}:
        # 当前工具调用仍会返回给模型；设置软停止，Agent loop 在工具结果入上下文后优雅收尾。
        svc.control_actions.request_soft_stop(chat_id, reason or "OpenBearControl stop")
        stopped_run = True
    return _json({"status": "ok", "action": "stop", "target": target, "softStopCurrentRun": stopped_run, "rathStopped": 0})


async def make_openbear_control_tool(svc: Any, args: dict[str, Any]) -> str:
    ctx = current_tool_context()
    action = str(args.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        return _json({"status": "error", "error": "unknown_action", "allowed": sorted(_ACTIONS)})
    if ctx.chat_id == 0 and not ctx.session_uuid:
        return _json({"status": "error", "error": "missing_chat_context"})
    if action == "stop":
        action_args_preview = args.get("args") if isinstance(args.get("args"), dict) else {}
        target_preview = str(args.get("target") or action_args_preview.get("target") or "current_run").strip() or "current_run"
        if target_preview in {"rath_task", "task", "agent_task", "agent", "all_tasks"}:
            return _json({
                "status": "error",
                "error": "use_AgentStop_for_agent_tasks",
                "action": "stop",
                "target": target_preview,
                "message": "Rath/Agent task 停止请使用 AgentStop；OpenBearControl 只控制 OpenBear 主控轮/控制面。",
            })
    if ctx.source not in _INTERACTIVE_SOURCES:
        return _json({"status": "error", "error": "openbear_control_not_available_in_this_context", "source": ctx.source, "action": action})
    action_args = args.get("args") if isinstance(args.get("args"), dict) else {}
    reason = str(args.get("reason") or action_args.get("reason") or "").strip()
    chat_id = int(ctx.chat_id)
    if action == "status":
        return await _action_status(svc, chat_id)
    if action == "models":
        return await _action_models(svc)
    if action == "mcp_status":
        return await _action_mcp_status(svc)
    if action == "skills_status":
        return await _action_skills_status(svc, action_args)
    if action == "skills_reload":
        return await _action_skills_reload(svc)
    if action == "mcp_reload":
        return await _action_mcp_reload(svc, reason)
    if action == "think":
        return await _action_think(svc, chat_id, action_args)
    if action == "restart":
        return await _action_restart(svc, chat_id, action_args, reason)
    if action == "new":
        return await _action_new(svc, chat_id, reason)
    if action == "stop":
        return await _action_stop(svc, chat_id, action_args, reason)
    return _json({"status": "error", "error": "unhandled_action", "action": action})


def register_openbear_control_tool(reg: ToolRegistry, svc: Any) -> None:
    reg.add(
        "OpenBearControl",
        (
            "OpenBear control plane: status/models/mcp_status/skills_status/skills_reload/mcp_reload/think/restart/new/foreground-run stop. "
            "Do not use this tool for Rath/Agent task cancellation; use AgentStop. "
            "Risky actions ask channel confirmation when supported and run safely after the reply; "
            "do not restart/stop openbear.service via Bash."
        ),
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(_ACTIONS),
                    "description": "要执行的控制动作：status/models/mcp_status/skills_status/skills_reload/mcp_reload/think/restart/new/stop",
                },
                "args": {
                    "type": "object",
                    "description": "动作参数，例如 think 用 {level}, skills_status 用 {query}, stop 用 {target: current_run}, restart 用 {delayS}",
                    "additionalProperties": True,
                },
                "reason": {"type": "string", "description": "为什么需要执行该控制动作，会展示给用户确认。"},
            },
            "required": ["action"],
        },
        lambda tool_args: make_openbear_control_tool(svc, tool_args),
        visibility={"main"},
    )
