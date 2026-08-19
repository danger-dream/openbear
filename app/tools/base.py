"""Tool 抽象 + 注册表。

工具用中性 schema 定义：{"name","description","parameters"}（JSON Schema）。
backend 各自把中性 schema 渲染成 OpenAI function / Anthropic input_schema。
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.logging import get_logger
from app.tools.json_repair import extract_balanced_json
from app.tools.truncate import truncate_tool_result

log = get_logger("tools")

# 单个工具结果占 context window 的最大比例 + 硬上限（移植 OpenClaw）
_TOOL_RESULT_CONTEXT_SHARE = 0.3
_HARD_CAP_CHARS = 32_000


def max_tool_result_chars(context_window_tokens: int, hard_cap_chars: int = _HARD_CAP_CHARS) -> int:
    """按模型 context window 动态算单个工具结果的字符上限（~4 字符/token）。"""
    by_ctx = int(context_window_tokens * _TOOL_RESULT_CONTEXT_SHARE) * 4
    return max(1, min(by_ctx, max(1, hard_cap_chars)))


_SENSITIVE_REDACTION = "[敏感内容已隐藏]"
_SENSITIVE_FLAG_RE = re.compile(r'"(?:sensitive|secret)"\s*:\s*true\b', re.IGNORECASE)


def _user_interaction_sensitive(arguments: str) -> bool:
    if not _SENSITIVE_FLAG_RE.search(str(arguments or "")):
        return False
    try:
        payload = json.loads(arguments or "{}")
    except Exception:
        # An audit projection must fail closed when the model emitted malformed
        # JSON that still contains an unambiguous sensitive marker.
        return True
    if not isinstance(payload, dict):
        return True
    return bool(payload.get("sensitive") or payload.get("secret"))


def redact_tool_arguments_for_audit(name: str, arguments: str) -> str:
    """Redact sensitive UserInteraction prompt defaults at the audit boundary."""
    if str(name or "") != "UserInteraction" or not _user_interaction_sensitive(arguments):
        return arguments
    try:
        payload = json.loads(arguments or "{}")
    except Exception:
        return json.dumps({
            "sensitive": True,
            "defaultValue": _SENSITIVE_REDACTION,
            "sensitiveRedacted": True,
        }, ensure_ascii=False, separators=(",", ":"))
    if not isinstance(payload, dict):
        return json.dumps({
            "sensitive": True,
            "defaultValue": _SENSITIVE_REDACTION,
            "sensitiveRedacted": True,
        }, ensure_ascii=False, separators=(",", ":"))
    if "defaultValue" in payload:
        payload["defaultValue"] = _SENSITIVE_REDACTION
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def redact_tool_result_for_audit(name: str, result: str, arguments: str = "") -> str:
    """Redact sensitive UserInteraction prompt values without changing raw results."""
    if str(name or "") != "UserInteraction" or not _user_interaction_sensitive(arguments):
        return result
    try:
        payload = json.loads(result or "{}")
    except Exception:
        # Malformed content cannot safely be enriched in place. Return a valid,
        # conservative audit record rather than risk retaining an unusual value.
        return json.dumps({
            "value": _SENSITIVE_REDACTION,
            "sensitiveRedacted": True,
        }, ensure_ascii=False, separators=(",", ":"))
    if not isinstance(payload, dict):
        return json.dumps({
            "value": _SENSITIVE_REDACTION,
            "sensitiveRedacted": True,
        }, ensure_ascii=False, separators=(",", ":"))
    if "value" in payload:
        payload["value"] = _SENSITIVE_REDACTION
    payload["sensitiveRedacted"] = True
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

# 工具处理函数：(arguments_dict) -> 结果字符串
ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(slots=True)
class ToolRuntimeContext:
    """Runtime-only metadata for a tool call.

    The schema passed to the model stays clean: the model should not fill chat
    ids or session ids. OpenBear injects this context right before dispatch so
    orchestration tools can attach Rath tasks to the current conversation.
    """

    chat_id: int = 0
    # OpenBear 主会话 uuid。Rath Agent Session 会挂在它下面。
    session_uuid: str = ""
    # Web conversation UUID, when running inside the Web console.  This is
    # separate from session_uuid so raw audit can link Rath/Agent calls back to
    # the drawer without changing Agent session ownership semantics.
    conversation_uuid: str = ""
    source: str = "chat"
    # Rath 子 Agent 工具调用时注入；普通 OpenBear 主循环留空。
    agent_session_uuid: str = ""
    task_uuid: str = ""
    agent_key: str = ""
    # Web round ownership. Backend assigns every tool/process/notification to
    # the user-message root turn that spawned it; frontend only renders this fact.
    turn_uuid: str = ""
    run_root_turn_uuid: str = ""
    tool_call_id: str = ""
    # Optional renderer callback for long-running tools to update their inline
    # progress block while the tool is still executing.
    progress_update: Callable[[str], Awaitable[None]] | None = None
    # Optional structured progress callback for Web live timelines.  This is
    # intentionally separate from progress_update so Web can receive structured
    # task/session/event payloads while text renderers keep a compact line.
    progress_update_payload: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    # Optional callback checked by Agent loop after a tool returns. Used by
    # OpenBearControl(action=stop) to stop the current run gracefully after the
    # tool result is recorded, instead of cancelling mid-tool.
    soft_stop_check: Callable[[], str] | None = None
    # Optional callback for Claude-Code-style background Agent completion. Agent
    # tools can launch a Rath task, return immediately, and later deliver a
    # task-notification back to the owning session so the main OpenBear loop can
    # summarize in a new turn.
    task_notification: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    # Optional callback into the owning conversation's existing live event stream.
    # Domain tools publish metadata-only events here; it is not a second transport.
    conversation_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    # Optional Web/browser confirmation callback. Used for interactive approvals
    # in Web console contexts.
    web_confirm: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # Main-controller-only Agent supervision primitive. AgentWait delegates to
    # this runtime callback so the model chooses event-only vs delayed review,
    # while Web owns reliable wake-up, aggregation, and same-root-turn routing.
    agent_wait: Callable[[dict[str, Any]], Awaitable[str]] | None = None


_TOOL_CONTEXT: ContextVar[ToolRuntimeContext] = ContextVar(
    "openbear_tool_context", default=ToolRuntimeContext())


def current_tool_context() -> ToolRuntimeContext:
    return _TOOL_CONTEXT.get()


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    visibility: set[str]
    source: str = "builtin"
    preserve_result: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def add(self, name: str, description: str, parameters: dict[str, Any],
            handler: ToolHandler, *, visibility: set[str] | None = None,
            source: str = "builtin", preserve_result: bool = False) -> None:
        self.register(Tool(name=name, description=description,
                           parameters=parameters, handler=handler,
                           visibility=set(visibility or {"main", "agent", "runtime"}),
                           source=source, preserve_result=bool(preserve_result)))

    def _visible_tools(self, scope: str | None = None, source: str | None = None) -> list[Tool]:
        if not scope:
            tools = list(self._tools.values())
        else:
            tools = [t for t in self._tools.values() if scope in t.visibility]
        if source is not None:
            tools = [t for t in tools if t.source == source]
        return tools

    def names(self, scope: str | None = None, source: str | None = None) -> list[str]:
        return [t.name for t in self._visible_tools(scope, source)]

    def summaries(self, scope: str | None = None, source: str | None = None) -> dict[str, str]:
        """返回 {工具名: 描述} 字典，供模板引擎生成工具列表。"""
        return {t.name: t.description for t in self._visible_tools(scope, source)}

    def schemas(self, scope: str | None = None) -> list[dict[str, Any]]:
        return [t.schema() for t in self._visible_tools(scope)]

    async def dispatch(
        self,
        name: str,
        arguments: str,
        *,
        max_chars: int = _HARD_CAP_CHARS,
        context: ToolRuntimeContext | None = None,
    ) -> str:
        """执行工具。arguments 是 JSON 字符串。返回结果字符串（回灌给模型）。

        结果在产出当下就地智能截断到 max_chars 以内（head+tail），
        存进 convo 后不再改 —— 历史字节级稳定，保护 prompt cache。
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"error: 未知工具: {name}"
        if not arguments.strip():
            args: Any = {}
        else:
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError as e:
                # #13 容错:模型偶尔把参数包成 markdown / 带前后缀杂质,标准解析
                # 失败时试一次括号配平提取;仍失败才报错。
                repaired = extract_balanced_json(arguments)
                if repaired is None:
                    return f"error: 工具参数不是合法 JSON: {e}"
                log.info("工具参数 JSON 容错修复成功", 工具=name)
                args = repaired
        if not isinstance(args, dict):
            return "error: 工具参数必须是 JSON 对象"
        token = _TOOL_CONTEXT.set(context or ToolRuntimeContext())
        try:
            try:
                result = await tool.handler(args)
            except Exception as e:
                log.exception("工具执行异常", 工具=name)
                return f"error: 工具 {name} 执行失败: {type(e).__name__}: {e}"
        finally:
            _TOOL_CONTEXT.reset(token)
        if isinstance(result, str) and len(result) > max_chars and not tool.preserve_result:
            log.info("工具结果智能截断", 工具=name, 原长=len(result), 上限=max_chars)
            return truncate_tool_result(result, max_chars)
        return result
