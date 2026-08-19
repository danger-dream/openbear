"""Human-in-the-loop tools backed by the Web conversation UI."""
from __future__ import annotations

import json
from typing import Any

from app.tools.base import ToolRegistry, current_tool_context


class UserInteractionManager:
    """Compatibility holder for the Web-only interaction tool registration path."""

    def __init__(self, _transport: Any = None) -> None:
        self.transport = _transport


def register_user_interaction_tools(reg: ToolRegistry, manager: UserInteractionManager) -> None:
    del manager

    async def _user_interaction(args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").strip().lower()
        if not action:
            return json.dumps({"status": "error", "error": "missing_action"}, ensure_ascii=False, indent=2)
        ctx = current_tool_context()
        if action in {"confirm", "select", "prompt", "questionnaire"}:
            if ctx.web_confirm is None:
                return json.dumps({
                    "status": "error",
                    "confirmed": False,
                    "error": "user_interaction_not_available_in_this_context",
                }, ensure_ascii=False, indent=2)
            payload = dict(args)
            payload.setdefault("type", payload.get("tone") or "warning")
            result = await ctx.web_confirm(payload)
            return json.dumps(result, ensure_ascii=False, indent=2)
        return json.dumps({"status": "error", "error": f"unknown_action:{action}"}, ensure_ascii=False, indent=2)

    option_schema = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "选项展示文字"},
            "value": {"type": "string", "description": "选项稳定值（同题内唯一）"},
            "description": {"type": "string", "description": "选项补充说明"},
        },
        "required": ["label", "value"],
    }
    question_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "本轮问卷内唯一的问题 ID"},
            "type": {"type": "string", "enum": ["choice", "open"], "description": "choice 为选项题且用户始终可补充自由文字，open 为开放题"},
            "question": {"type": "string", "description": "问题正文"},
            "description": {"type": "string", "description": "问题补充说明"},
            "required": {"type": "boolean", "description": "是否必答，默认 true"},
            "multiple": {"type": "boolean", "description": "choice 是否允许选择多个选项，默认 false"},
            "options": {"type": "array", "items": option_schema, "description": "choice 的可选项"},
            "recommendation": {
                "type": "object",
                "properties": {
                    "values": {"type": "array", "items": {"type": "string"}, "description": "推荐的选项 value，仅作提示，不会自动作答"},
                    "reason": {"type": "string", "description": "推荐理由"},
                },
                "required": ["values", "reason"],
            },
        },
        "required": ["id", "type", "question"],
    }
    reg.add(
        "UserInteraction",
        "Ask the Web user for an interaction. Supports action=confirm, action=select, action=prompt, and action=questionnaire in the Web conversation UI.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["confirm", "select", "prompt", "questionnaire"], "description": "Interaction action"},
            "title": {"type": "string", "description": "标题"},
            "body": {"type": "string", "description": "正文说明"},
            "type": {"type": "string", "enum": ["none", "success", "info", "warning", "danger"], "description": "提示类型"},
            "tone": {"type": "string", "enum": ["none", "success", "info", "warning", "danger"], "description": "type 的别名"},
            "default": {"type": "boolean", "description": "confirm 超时时默认是否确认，默认 false"},
            "confirmText": {"type": "string", "description": "确认按钮文案，默认 确认"},
            "cancelText": {"type": "string", "description": "取消按钮文案，默认 取消"},
            "timeoutSeconds": {"type": "number", "description": "等待秒数，默认 600"},
            "options": {"type": "array", "items": {"oneOf": [
                {"type": "string"},
                {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}}, "required": ["label"]},
            ]}, "description": "select 的选项列表"},
            "multiple": {"type": "boolean", "description": "select 是否允许多选；questionnaire 请在每个 choice 问题中设置"},
            "sensitive": {"type": "boolean", "description": "prompt 输入是否按敏感文本处理"},
            "defaultValue": {"type": "string", "description": "prompt 超时时返回的默认输入值"},
            "questions": {"type": "array", "items": question_schema, "description": "questionnaire 的问题列表；choice 固有支持选项之外的自由文字补充"},
        }, "required": ["action", "title", "body"]},
        _user_interaction,
        visibility={"main"},
    )
