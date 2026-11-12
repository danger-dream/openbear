"""Tool 抽象 + 注册表。

工具用中性 schema 定义：{"name","description","parameters"}（JSON Schema）。
backend 各自把中性 schema 渲染成 OpenAI function / Anthropic input_schema。
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from app.interaction_data import redact_result
from app.logging import get_logger
from app.runtime.lifecycle import current_session
from app.runtime.tool_result import ToolOutcome
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
    from app.instances.security import audit
    arguments = audit(arguments)
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
    for key in ("title", "body", "description", "defaultValue", "text"):
        if key in payload:
            payload[key] = _SENSITIVE_REDACTION
    for key in ("options", "questions", "defaultValues", "defaultIndexes"):
        if key in payload:
            payload[key] = []
    payload["sensitiveRedacted"] = True
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def redact_tool_result_for_audit(name: str, result: str, arguments: str = "") -> str:
    """Redact sensitive UserInteraction prompt values without changing raw results."""
    from app.instances.security import audit
    result = audit(result)
    if str(name or "") != "UserInteraction":
        # Shared approval consumers can return a user's sensitive feedback inside
        # their result. Keep raw text for the model, but not for audit surfaces.
        try:
            outer = json.loads(result)
            confirmation = outer.get("confirmation") if isinstance(outer, dict) else None
            if isinstance(confirmation, dict) and confirmation.get("sensitive"):
                outer["confirmation"] = redact_result(confirmation)
                return json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
        except (ValueError, TypeError):
            pass
        return result
    if not _user_interaction_sensitive(arguments):
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
    return json.dumps(redact_result(payload), ensure_ascii=False, separators=(",", ":"))

# 工具处理函数：(arguments_dict) -> 结果字符串或结构化结果
ToolHandler = Callable[[dict[str, Any]], Awaitable[str | ToolOutcome]]


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
    # Transient structured media; never serialize base64 into tool text/audit/history.
    tool_images: list[dict[str, Any]] = field(default_factory=list)
    # Set only by the trusted interaction callback when a user submitted text.
    # A denied shared approval must not truncate that text with ordinary output.
    preserve_user_answer: bool = False
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
    execution_id: str = ""
    run_id: str = ""
    # Host-injected external-event authority; never model arguments.
    webhook_assignment_id: str = ""
    webhook_owner_id: int = 0
    webhook_start_cursor: int | None = None
    webhook_automatic: bool = False
    receipt_repair: bool = False
    receipt_repair_calls: int = 0
    receipt_repair_started: float = 0.0
    execution_started: Callable[[Any], Awaitable[None]] | None = None
    finish_policy: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    terminal_policy: Callable[[str], Awaitable[None]] | None = None
    # Last dispatch result, including cancellation when the await is interrupted.
    tool_outcome: ToolOutcome | None = None


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
    read_only: bool = False
    concurrent_safe: bool = False
    effect_scope: str = "unknown"
    # False: callable, but left out of names, summaries and schemas (an earlier tool behind a merged one).
    listed: bool = True

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class MergedTool:
    actions: dict[str, Tool]
    default: str | None
    inner: dict[str, str]

    def target(self, args: dict[str, Any]) -> tuple[str, Tool | None]:
        action = str(args.get("action") or self.default or "")
        return action, self.actions.get(action)

    def inner_arguments(self, tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in args.items() if k != "action"}
        field = self.inner.get(tool.name)
        if field and field in out:
            out["action"] = out.pop(field)
        return out

    def resolve(self, name: str, args: dict[str, Any]) -> tuple[Tool, dict[str, Any], list[str]] | str:
        """The selected tool, its own arguments and the fields it does not take, or an unknown-action error."""
        action, tool = self.target(args)
        if tool is None:
            return json.dumps({"ok": False, "error": "unknown_action", "tool": name, "action": action,
                               "actions": list(self.actions)}, ensure_ascii=False)
        inner = self.inner_arguments(tool, args)
        # The merged schema offers every action's fields, so another action's field is dropped rather than
        # refused; missing fields are left to the selected tool, which names them in its own errors.
        accepted = set(tool.parameters.get("properties", {}))
        ignored = sorted(set(inner) - accepted)
        return tool, {k: v for k, v in inner.items() if k in accepted}, ignored


def with_ignored_fields(content: str, ignored: list[str]) -> str:
    """A result that says which given fields its action did not take."""
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        value = None
    if isinstance(value, dict):
        return json.dumps({**value, "ignoredFields": ignored}, ensure_ascii=False)
    return f"{content}\n[ignoredFields: {', '.join(ignored)}]"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # File tools register their actual injected cache, not necessarily the
        # process-global fallback. Window owners invalidate only their scope.
        self.file_state: Any = None
        # Trusted wiring only; MCP eligibility is resolved against the live manager.
        self.mcp_manager: Any = None
        self.mcp_tool_identities: dict[str, tuple[str, str]] = {}
        self.current_registry: Callable[[], ToolRegistry] | None = None
        self._merged: dict[str, MergedTool] = {}

    def register(self, tool: Tool) -> None:
        from app.tools.confirmation import with_amended_confirmation
        self._tools[tool.name] = with_amended_confirmation(tool, self.mcp_tool_identities.get(tool.name))

    def add(self, name: str, description: str, parameters: dict[str, Any],
            handler: ToolHandler, *, visibility: set[str] | None = None,
            source: str = "builtin", preserve_result: bool = False,
            read_only: bool = False, concurrent_safe: bool = False,
            effect_scope: str = "unknown") -> None:
        self.register(Tool(name=name, description=description,
                           parameters=parameters, handler=handler,
                           visibility=set(visibility or {"main", "agent", "runtime"}),
                           source=source, preserve_result=bool(preserve_result),
                           read_only=read_only, concurrent_safe=concurrent_safe,
                           effect_scope=effect_scope))

    def merge(self, name: str, description: str, actions: dict[str, str], *, default: str | None = None,
              inner: dict[str, str] | None = None, fields: dict[str, dict[str, Any]] | None = None) -> None:
        """One tool whose `action` selects one of these registered tools; they stay callable but unlisted.

        inner names the field that carries a selected tool's own `action`; fields replaces the
        merged definition of a field. Field definitions are shared, not copied, with the tools.
        """
        inner = inner or {}
        chosen = {action: self._tools[target] for action, target in actions.items()}
        properties: dict[str, Any] = {"action": {"type": "string", "enum": list(actions)}}
        for action, tool in chosen.items():
            for key, spec in tool.parameters.get("properties", {}).items():
                properties.setdefault(inner.get(tool.name, key) if key == "action" else key, spec)
        properties.update(fields or {})
        for tool in chosen.values():
            if tool.name != name:
                self._tools[tool.name] = replace(tool, listed=False)
        base = chosen[default] if default else next(iter(chosen.values()))
        self._merged[name] = MergedTool(chosen, default, inner)
        self._tools[name] = replace(base, name=name, description=description, listed=True, parameters={
            "type": "object", "properties": properties, "required": [] if default else ["action"], "additionalProperties": False})

    def merged(self, name: str) -> MergedTool | None:
        return self._merged.get(name)

    def _visible_tools(self, scope: str | None = None, source: str | None = None) -> list[Tool]:
        listed = [t for t in self._tools.values() if t.listed]
        if not scope:
            tools = listed
        else:
            tools = [t for t in listed if scope in t.visibility]
        if source is not None:
            tools = [t for t in tools if t.source == source]
        return tools

    def names(self, scope: str | None = None, source: str | None = None) -> list[str]:
        return [t.name for t in self._visible_tools(scope, source)]

    def summaries(self, scope: str | None = None, source: str | None = None) -> dict[str, str]:
        """返回 {工具名: 描述} 字典，供模板引擎生成工具列表。"""
        return {t.name: t.description for t in self._visible_tools(scope, source)}

    def schemas(self, scope: str | None = None) -> list[dict[str, Any]]:
        if scope == "agent":
            from app.tools.confirmation import without_approval_argument
            return [without_approval_argument(t.schema()) for t in self._visible_tools(scope)]
        return [t.schema() for t in self._visible_tools(scope)]

    async def dispatch(
        self,
        name: str,
        arguments: str,
        *,
        max_chars: int = _HARD_CAP_CHARS,
        context: ToolRuntimeContext | None = None,
    ) -> str:
        """兼容模型工具结果文本；完整执行状态由 dispatch_outcome 提供。"""
        return (await self.dispatch_outcome(
            name, arguments, max_chars=max_chars, context=context,
        )).content

    async def dispatch_outcome(
        self,
        name: str,
        arguments: str,
        *,
        max_chars: int = _HARD_CAP_CHARS,
        context: ToolRuntimeContext | None = None,
    ) -> ToolOutcome:
        """执行工具并返回完整状态；仅结果正文遵循原有智能截断规则。"""
        execution_context = context if context is not None else ToolRuntimeContext()
        execution_context.tool_outcome = None
        session = current_session()
        if session:
            execution_context.execution_id = session.tool_action_id
            execution_context.run_id = session.run_id
        if execution_context.receipt_repair and name not in {"Webhook", "History"}:
            outcome = ToolOutcome("error: receipt_repair_read_only: business execution is forbidden", "failed", "not_started")
            execution_context.tool_outcome = outcome
            return outcome
        tool = self._tools.get(name)
        if tool is None:
            outcome = ToolOutcome(f"error: 未知工具: {name}", "failed", "not_started")
            execution_context.tool_outcome = outcome
            return outcome
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
                    outcome = ToolOutcome(f"error: 工具参数不是合法 JSON: {e}", "failed", "not_started")
                    execution_context.tool_outcome = outcome
                    return outcome
                log.info("工具参数 JSON 容错修复成功", 工具=name)
                args = repaired
        if not isinstance(args, dict):
            outcome = ToolOutcome("error: 工具参数必须是 JSON 对象", "failed", "not_started")
            execution_context.tool_outcome = outcome
            return outcome
        ignored: list[str] = []
        merged = self._merged.get(name)
        if merged is not None:
            resolved = merged.resolve(name, args)
            if isinstance(resolved, str):
                outcome = ToolOutcome(resolved, "failed", "not_started")
                execution_context.tool_outcome = outcome
                return outcome
            tool, args, ignored = resolved
        execution_context.preserve_user_answer = False
        image_start = len(execution_context.tool_images)
        token = _TOOL_CONTEXT.set(execution_context)
        try:
            try:
                result = await tool.handler(args)
            except asyncio.CancelledError:
                execution_context.tool_outcome = ToolOutcome(
                    "", "cancelled", "unknown",
                    images=list(execution_context.tool_images[image_start:]),
                )
                raise
            except Exception as e:
                log.exception("工具执行异常", 工具=name)
                outcome = ToolOutcome(
                    f"error: 工具 {name} 执行失败: {type(e).__name__}: {e}",
                    "failed", "unknown",
                    images=list(execution_context.tool_images[image_start:]),
                )
                execution_context.tool_outcome = outcome
                return outcome
        finally:
            _TOOL_CONTEXT.reset(token)
        if isinstance(result, ToolOutcome):
            outcome = result
        else:
            # A legacy string may contain arbitrary error-looking text or JSON;
            # neither provides evidence of business success or effect safety.
            outcome = ToolOutcome(result, "completed", "reported")
        content = with_ignored_fields(outcome.content, ignored) if ignored else outcome.content
        if len(content) > max_chars and not tool.preserve_result and not execution_context.preserve_user_answer:
            log.info("工具结果智能截断", 工具=name, 原长=len(content), 上限=max_chars)
            content = truncate_tool_result(content, max_chars)
        outcome = replace(
            outcome, content=content,
            images=[*outcome.images, *execution_context.tool_images[image_start:]],
        )
        execution_context.tool_outcome = outcome
        return outcome
