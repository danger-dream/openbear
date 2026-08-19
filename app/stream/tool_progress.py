"""工具调用可见化 —— 把每次工具调用格式化成一行内联进流式消息。

参考 OpenClaw 的 tool-progress 方案：emoji + 工具名 + 参数预览，
内联进答案时间线，让老大随时看到「正在执行什么」。
"""
from __future__ import annotations

import html
import json
from typing import Any

_TOOL_EMOJI = {
    "Read": "📖",
    "Write": "✏️",
    "Edit": "✏️",
    "EditBatch": "✏️",
    "Bash": "💻",
    "Agent": "🧑‍💻",
    "AgentMessage": "▶️",
    "AgentStop": "🛑",
    "Memory": "🧠",
    "UserInteraction": "👤",
}

# 各工具优先展示哪个参数作为预览（pattern/command 等"意图"参数优先于 path）
_PREVIEW_KEYS = [
    "command", "pattern", "query", "title", "body", "name", "ref", "path",
    "old_string", "content", "action", "text", "prompt", "workerType",
]


def _pick_emoji(tool_name: str) -> str:
    return _TOOL_EMOJI.get(tool_name, "🔧")


def _load_json_obj(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value) if str(value or "").strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _short_text(value: str, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_preview(arguments: str) -> str:
    """从工具参数 JSON 里抽一个简短可读预览。"""
    args = _load_json_obj(arguments)
    if not args:
        return ""
    for k in _PREVIEW_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())
    for v in args.values():
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())
    return ""


def _task_memory_preview(args: dict[str, Any]) -> str:
    action = str(args.get("action") or "").strip().lower()
    action_label = {
        "list": "获取记忆列表",
        "search": "搜索记忆",
        "get": "读取记忆",
        "create": "创建记忆",
        "update": "更新记忆",
        "delete": "删除记忆",
        "restore": "恢复记忆",
    }.get(action, action or "操作记忆")
    if action == "list":
        return action_label
    if action == "search":
        query = _short_text(str(args.get("query") or ""), 60)
        return f"{action_label}： {query}" if query else action_label
    memory_label = _short_text(str(args.get("description") or args.get("name") or ""), 60)
    if memory_label:
        return f"{action_label}： {memory_label}"
    memory_uuid = _short_text(str(args.get("memoryUuid") or ""), 24)
    return f"{action_label}： {memory_uuid}" if memory_uuid else action_label


def _agent_items(arguments: str) -> list[dict[str, Any]]:
    args = _load_json_obj(arguments)
    if isinstance(args.get("items"), list):
        return [x for x in args["items"] if isinstance(x, dict)]
    if args:
        return [args]
    return []


def _agent_item_preview(item: dict[str, Any], *, limit: int = 72) -> str:
    agent = _short_text(str(item.get("workerType") or item.get("subagent_type") or item.get("agent") or item.get("agentName") or item.get("agentKey") or "general-purpose"), 24)
    task = str(item.get("description") or item.get("title") or item.get("prompt") or item.get("instruction") or item.get("task") or item.get("message") or "").strip()
    task = _short_text(task, limit)
    return f"{agent}：{task}" if task else agent


def _duration_text(duration_ms: int) -> str:
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    seconds = duration_ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rest = int(seconds % 60)
    return f"{minutes}m{rest:02d}s"


def _agent_card(lines: list[str], *, expandable: bool = False) -> str:
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    # Only the wrapper HTML is intentional. Agent names/titles/instructions can
    # contain <, >, &, or even </blockquote>; escape every line before rendering.
    return tag + "\n".join(html.escape(line) for line in lines) + "</blockquote>"


def _agent_tool_start_line(tool_name: str, arguments: str) -> str | None:
    items = _agent_items(arguments)
    if tool_name == "Agent" and items:
        return _agent_card(["🧑‍💻 Agent", f"🚧 {_agent_item_preview(items[0], limit=90)}"])
    if tool_name == "AgentMessage" and items:
        target = _short_text(str(items[0].get("to") or items[0].get("taskUuid") or items[0].get("task_uuid") or "Agent task"), 16)
        message = _short_text(str(items[0].get("message") or items[0].get("prompt") or items[0].get("guidance") or ""), 80)
        return _agent_card(["▶️ AgentMessage", f"🚧 {target}" + (f"：{message}" if message else "")])
    return None


def format_tool_running_status_line(tool_name: str, arguments: str) -> str | None:
    """Return a dynamic status label while a long-running Agent tool is executing."""
    items = _agent_items(arguments)
    if tool_name == "Agent" and items:
        agent = str(items[0].get("workerType") or items[0].get("subagent_type") or "general-purpose")
        task = _short_text(str(items[0].get("description") or items[0].get("title") or items[0].get("prompt") or ""), 60)
        return f"Agent 执行中：{agent}" + (f" · {task}" if task else "")
    if tool_name == "AgentMessage" and items:
        target = _short_text(str(items[0].get("to") or items[0].get("taskUuid") or items[0].get("task_uuid") or "Agent task"), 16)
        return f"Agent 消息处理中：{target}"
    return None


def _format_count(value: int | float) -> str:
    n = float(value or 0)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def _format_cost(value: Any) -> str:
    try:
        cost = float(value or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost <= 0:
        return ""
    return f"💰 ${cost:.4f}"


def _cache_pct(cache: int, input_tokens: int) -> str:
    if input_tokens <= 0:
        return "—"
    return f"{cache * 100 / input_tokens:.1f}%"


def _task_tokens(task: dict[str, Any]) -> dict[str, int]:
    tokens = task.get("tokens") if isinstance(task.get("tokens"), dict) else {}
    input_tokens = int(tokens.get("input") or 0)
    output_tokens = int(tokens.get("output") or 0)
    cache_tokens = int(tokens.get("cache") or 0)
    return {"input": input_tokens, "output": output_tokens, "cache": cache_tokens}


def _task_stats(task: dict[str, Any]) -> str:
    model_calls = task.get("modelCalls")
    tool_calls = task.get("toolCalls")
    parts: list[str] = []
    if isinstance(model_calls, int):
        parts.append(f"🔄 模型调用 {model_calls} 次")
    if isinstance(tool_calls, int):
        parts.append(f"🛠 工具调用 {tool_calls} 次")
    return " · ".join(parts)


def _token_line(*, input_tokens: int, output_tokens: int, cache_tokens: int) -> str:
    total = input_tokens + output_tokens
    if total <= 0 and cache_tokens <= 0:
        return ""
    return (
        f"📊 Tokens：{_format_count(total)} · ↑{_format_count(input_tokens)} · "
        f"↓{_format_count(output_tokens)} · 缓存 {_format_count(cache_tokens)}({_cache_pct(cache_tokens, input_tokens)})"
    )


def _tool_arg_preview(raw: object, *, limit: int = 82) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        for key in ("query", "url", "path", "command", "task", "instruction"):
            value = data.get(key)
            if value:
                text = f"{key}={value}"
                break
    text = " ".join(str(text).split())
    return _short_text(text, limit)


def _event_progress_label(event: dict[str, Any], *, now_ms: int | None = None) -> str:
    kind = str(event.get("kind") or "event")
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    summary = str(event.get("summary") or "")

    def running_suffix() -> str:
        if now_ms is None:
            return ""
        try:
            event_ms = int(event.get("elapsedMs") or 0)
        except (TypeError, ValueError):
            event_ms = 0
        if event_ms <= 0 or now_ms <= event_ms:
            return ""
        return f" · 已 {_duration_text(now_ms - event_ms)}"

    if kind == "model_call_started":
        model = str(detail.get("modelLabel") or detail.get("model") or "模型")
        return f"🔄 模型调用开始：{model}{running_suffix()}"
    if kind == "model_call_finished":
        out = int(detail.get("outputTokens") or 0)
        duration = int(detail.get("durationMs") or 0)
        suffix = f" · ↓{_format_count(out)}" if out else ""
        if duration > 0:
            suffix += f" · {_duration_text(duration)}"
        return f"✅ 模型调用完成{suffix}"
    if kind == "tool_call_started":
        name = str(detail.get("name") or summary or "工具")
        preview = _tool_arg_preview(detail.get("arguments"))
        return f"🛠 工具调用中：{name}" + (f" · {preview}" if preview else "") + running_suffix()
    if kind == "tool_call_finished":
        name = str(detail.get("name") or summary or "工具")
        duration = int(detail.get("durationMs") or 0)
        suffix = f" · {_duration_text(duration)}" if duration > 0 else ""
        return f"✅ 工具完成：{name}{suffix}"
    return summary or kind


def _event_progress_lines(events: list[dict[str, Any]], *, limit: int = 4, now_ms: int | None = None) -> list[str]:
    if not events:
        return []
    useful = [
        e for e in events
        if str(e.get("kind") or "") in {
            "model_call_started", "model_call_finished",
            "tool_call_started",
            "agent_budget_exhausted", "needs_openbear_control",
            "agent_continue_failed", "task_failed", "task_cancelled",
        } or e.get("summary")
    ]
    def is_active_started(idx: int, event: dict[str, Any]) -> bool:
        kind = str(event.get("kind") or "")
        if kind not in {"model_call_started", "tool_call_started"}:
            return False
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        name = str(detail.get("name") or "")
        round_no = detail.get("round")
        for later in useful[idx + 1:]:
            later_kind = str(later.get("kind") or "")
            later_detail = later.get("detail") if isinstance(later.get("detail"), dict) else {}
            if kind == "model_call_started" and later_kind in {"model_call_started", "model_call_finished"}:
                return False
            if kind == "tool_call_started" and later_kind in {"tool_call_started", "tool_call_finished"}:
                later_name = str(later_detail.get("name") or "")
                later_round = later_detail.get("round")
                if (not name or not later_name or later_name == name) and (round_no is None or later_round is None or later_round == round_no):
                    return False
        return True

    start = max(0, len(useful) - limit)
    lines: list[str] = []
    for idx, event in enumerate(useful[start:], start=start):
        label_now_ms = now_ms if is_active_started(idx, event) else None
        lines.append(f"• {_event_progress_label(event, now_ms=label_now_ms)}")
    return lines


def format_agent_batch_progress_card(items: list[dict[str, Any]], *, duration_ms: int = 0) -> str:
    """Render live aggregate progress for multiple Agent tasks."""
    total = len(items)
    done_statuses = {"completed", "failed", "cancelled", "interrupted", "needs_openbear_control"}
    done = sum(1 for item in items if str(item.get("status") or "") in done_statuses)
    running = sum(1 for item in items if str(item.get("status") or "") in {"running", "resuming", "pausing", "stopping"})
    elapsed = _duration_text(duration_ms) if duration_ms > 0 else ""
    lines = [
        "👥 Agent",
        " · ".join(x for x in [f"🚧 {total} 个 Agent", f"完成 {done}/{total}", f"运行 {running}" if running else "", f"⏱ {elapsed}" if elapsed else ""] if x),
    ]

    model_total = 0
    tool_total = 0
    input_total = 0
    output_total = 0
    cache_total = 0
    cost_total = 0.0
    for item in items:
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        if isinstance(task.get("modelCalls"), int):
            model_total += int(task["modelCalls"])
        if isinstance(task.get("toolCalls"), int):
            tool_total += int(task["toolCalls"])
        tokens = _task_tokens(task)
        input_total += tokens["input"]
        output_total += tokens["output"]
        cache_total += tokens["cache"]
        try:
            cost_total += float(task.get("costUsd") or 0.0)
        except (TypeError, ValueError):
            pass
    stats = " · ".join(x for x in [
        f"🔄 模型调用 {model_total} 次" if model_total else "",
        f"🛠 工具调用 {tool_total} 次" if tool_total else "",
    ] if x)
    if stats:
        lines.append(stats)
    if token_line := _token_line(input_tokens=input_total, output_tokens=output_total, cache_tokens=cache_total):
        lines.append(token_line)
    if cost := _format_cost(cost_total):
        lines.append(cost)

    shown = items[:6]
    for item in shown:
        status = str(item.get("status") or "queued")
        icon, _label = _agent_status_meta(status, tool_name="Agent")
        agent = _short_text(str(item.get("agent") or "Agent"), 18)
        title = _short_text(str(item.get("title") or ""), 54)
        current = _short_text(str(item.get("currentStatus") or _status_label(status)), 72)
        line = f"• {icon} {agent}" + (f"：{title}" if title else "")
        lines.append(line)
        if current:
            lines.append(f"↳ 当前：{current}")
        events = item.get("recentEvents") if isinstance(item.get("recentEvents"), list) else []
        item_duration_ms = int(item.get("durationMs") or duration_ms or 0)
        for event_line in _event_progress_lines(events, limit=3, now_ms=item_duration_ms):
            lines.append(f"↳ {event_line[2:] if event_line.startswith('• ') else event_line}")
    if total > len(shown):
        lines.append(f"• …另 {total - len(shown)} 个")
    return _agent_card([line for line in lines if line], expandable=len(lines) > 8)


def format_agent_task_progress_card(
    tool_name: str,
    arguments: dict[str, Any] | str,
    task: dict[str, Any],
    *,
    events: list[dict[str, Any]] | None = None,
    duration_ms: int = 0,
) -> str:
    """Render live Rath task stats into the inline Agent tool block."""
    args = _load_json_obj(arguments) if isinstance(arguments, str) else dict(arguments or {})
    items = _agent_items(json.dumps(args, ensure_ascii=False)) if args else []
    item = items[0] if items else args
    agent = str(
        (task.get("agentSession") if isinstance(task.get("agentSession"), dict) else {}).get("title")
        or item.get("agent") or item.get("agentName") or item.get("agentKey")
        or task.get("currentAgent") or "Agent"
    )
    task_preview = _short_text(str(item.get("title") or item.get("instruction") or item.get("task") or task.get("title") or ""), 86)
    status = str(task.get("status") or "running")
    current = str(task.get("currentStatus") or "执行中")
    icon = _pick_emoji(tool_name)
    label = tool_name
    elapsed = _duration_text(duration_ms) if duration_ms > 0 else ""
    lines = [
        f"{icon} {label}",
        f"🚧 {agent}" + (f"：{task_preview}" if task_preview else ""),
        " · ".join(x for x in [f"状态 {status}", f"⏱ {elapsed}" if elapsed else ""] if x),
        f"当前：{current}",
    ]
    lines.extend(_task_usage_lines(task))
    recent = _event_progress_lines(events or [], now_ms=duration_ms)
    if recent:
        lines.append("最近动态：")
        lines.extend(recent)
    return _agent_card([line for line in lines if line])


def _task_usage_lines(task: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if stats := _task_stats(task):
        lines.append(stats)
    tokens = _task_tokens(task)
    if token_line := _token_line(
        input_tokens=tokens["input"], output_tokens=tokens["output"], cache_tokens=tokens["cache"]
    ):
        lines.append(token_line)
    if cost := _format_cost(task.get("costUsd")):
        lines.append(cost)
    return lines


def _agent_status_meta(status: str, *, tool_name: str) -> tuple[str, str]:
    base = tool_name
    return {
        "completed": ("✅", f"{base} 完成"),
        "needs_openbear_control": ("🧭", f"{base} 等待 OpenBear 裁决"),
        "failed": ("❌", f"{base} 失败"),
        "cancelled": ("⏹", f"{base} 已取消"),
        "interrupted": ("⚠️", f"{base} 已中断"),
        "running": ("🚧", f"{base} 运行中"),
        "queued": ("⏳", f"{base} 排队中"),
    }.get(status or "", ("⚠️", f"{base} {status or '状态未知'}"))


def _status_label(status: str) -> str:
    return {
        "completed": "已完成",
        "needs_openbear_control": "等待 OpenBear 裁决",
        "failed": "失败",
        "cancelled": "已取消",
        "interrupted": "已中断",
        "running": "运行中",
        "queued": "排队中",
    }.get(status or "", status or "未知")


def _payload_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get("recentEvents")
    if isinstance(events, list):
        return [e for e in events if isinstance(e, dict)]
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    events = task.get("recentEvents") if isinstance(task, dict) else None
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def format_tool_result_line(tool_name: str, arguments: str, result: str, duration_ms: int) -> str:
    """Human-friendly completion line for long Agent tools; empty for ordinary tools."""
    if tool_name not in {"Agent", "AgentMessage", "AgentStop"}:
        return ""
    payload = _load_json_obj(result)
    elapsed = _duration_text(duration_ms)
    if not payload:
        return f"✅ {tool_name} 完成 · {elapsed}"

    if tool_name in {"Agent", "AgentMessage", "AgentStop"}:
        agent = payload.get("agentSession") if isinstance(payload.get("agentSession"), dict) else {}
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        name = agent.get("title") or agent.get("agentKey") or "Agent"
        status = str(task.get("status") or payload.get("status") or "completed")
        icon, label = _agent_status_meta(status, tool_name=tool_name)
        lines = [" · ".join(x for x in [f"{icon} {label}：{name}", f"状态 {_status_label(status)}", f"⏱ {elapsed}"] if x)]
        lines.extend(_task_usage_lines(task))
        recent = _event_progress_lines(_payload_events(payload), limit=5)
        if recent:
            lines.append("最近动态：")
            lines.extend(recent)
        return _agent_card(lines)
    return ""


def format_tool_line(tool_name: str, arguments: str, preview_len: int = 60) -> str:
    """格式化一行工具调用，如：💻 Bash: hostname && uptime"""
    agent_line = _agent_tool_start_line(tool_name, arguments)
    if agent_line:
        return agent_line
    emoji = _pick_emoji(tool_name)
    args = _load_json_obj(arguments)
    if tool_name == "EditBatch" and isinstance(args.get("edits"), list):
        path = " ".join(str(args.get("path") or "").split())
        preview = f"{path} · {len(args['edits'])} 段" if path else f"{len(args['edits'])} 段"
    elif tool_name == "TaskMemory":
        preview = _task_memory_preview(args)
    else:
        preview = _extract_preview(arguments)
    if not preview:
        return f"{emoji} {tool_name} …"
    if len(preview) > preview_len:
        preview = preview[: preview_len - 1] + "…"
    return f"{emoji} {tool_name}: {preview}"


USER_INTERACTION_TOOLS = {"UserInteraction", "OpenBearControl"}


def is_user_interaction_tool(tool_name: str) -> bool:
    return tool_name in USER_INTERACTION_TOOLS


def format_user_interaction_wait_line(tool_name: str, arguments: str) -> str:
    args = _load_json_obj(arguments)
    title = _short_text(str(args.get("title") or args.get("body") or "等待用户操作"), 60)
    if tool_name == "UserInteraction":
        action = str(args.get("action") or "confirm")
        if action == "select":
            return f"等待用户选择：{title}"
        if action == "prompt":
            return f"等待用户输入：{title}"
        if action == "questionnaire":
            return f"等待用户填写需求问卷：{title}"
        return f"等待用户确认：{title}"
    if tool_name == "OpenBearControl":
        action = _short_text(str(args.get("action") or "控制动作"), 40)
        return f"等待 OpenBear 控制确认：{action}"
    return f"等待用户操作：{title}"


def format_user_interaction_result_line(tool_name: str, arguments: str, result: str) -> str:
    args = _load_json_obj(arguments)
    data = _load_json_obj(result)
    status = str(data.get("status") or "")
    action = str(args.get("action") or "confirm") if tool_name == "UserInteraction" else ""
    if action == "questionnaire":
        if status == "error":
            return "❌ 需求问卷创建失败。"
        if status == "timeout":
            return "⌛ 需求问卷已超时，未采用任何推荐或默认答案。"
        if data.get("cancelled") is True or status == "cancelled":
            return "🚫 用户已取消需求问卷。"
        answers = data.get("answers")
        answer_count = len(answers) if isinstance(answers, list) else 0
        return f"✅ 用户已提交需求问卷（{answer_count} 题）。"
    if status == "timeout":
        return "⌛ 用户未响应，已按默认值继续。"
    if data.get("cancelled") is True:
        return "🚫 用户已取消。"

    if tool_name == "UserInteraction":
        if action == "select":
            labels = data.get("selectedLabels")
            if isinstance(labels, list) and labels:
                rendered = "、".join(_short_text(str(x), 40) for x in labels)
                return f"✅ 用户已选择：{rendered}"
            return "✅ 用户已确认选择。"
        if action == "prompt":
            sensitive = bool(args.get("sensitive") or args.get("secret"))
            value = str(data.get("value") or "")
            if sensitive:
                return "✍️ 用户已输入文本（已隐藏）。"
            return f"✍️ 用户输入：{_short_text(value)}" if value else "✍️ 用户已输入文本。"
        label = _short_text(str(data.get("label") or data.get("choice") or ""), 60)
        if data.get("confirmed") is True:
            return f"✅ 用户已确认：{label}" if label else "✅ 用户已确认。"
        return f"🚫 用户已取消：{label}" if label else "🚫 用户已取消。"

    if tool_name == "OpenBearControl":
        action = str(data.get("action") or args.get("action") or "控制动作")
        status = str(data.get("status") or "")
        if status == "scheduled":
            return f"✅ 控制动作已安排：{action}"
        if status == "cancelled":
            return f"🚫 控制动作已取消：{action}"
        if status == "ok":
            return f"✅ 控制动作完成：{action}"
        if status == "error":
            return f"❌ 控制动作失败：{action}"
        return f"✅ 控制动作返回：{action}"

    return "✅ 用户交互已完成。"
