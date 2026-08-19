"""工具调用配对修复 —— 借鉴 OpenClaw repairToolUseResultPairing。

为什么需要它
============
严格的上游(OpenAI Responses API / Anthropic / MiniMax 等)要求:assistant 发起的
每一个 tool_call,必须紧跟一条 tool_call_id 匹配的 tool 结果;否则整条请求 400
(典型报错:`No tool output found for function call ...`)。

会话历史里出现「光杆 tool_call」(有调用、无结果)的根因:
  - 停止或新消息打断:工具串行执行,被取消时后续 tool_call 连执行都没开始,
    但 assistant 那条(含全部 tool_calls)早已落库 → 留下 1~N 个无结果的调用。
  - 进程崩溃 / 网络中断:工具结果尚未落库。

build_history 是「发往上游的所有 convo」的唯一收口,在这里做一次配对净化,既能
屏蔽存量脏历史,也能兜住未来任何来源产生的残缺,一处生效、全场景覆盖。

修复策略(与 OpenClaw 对齐)
==========================
- 光杆 tool_call(无配对结果)→ 合成一条 isError 占位结果补上(保留轮次结构,
  比直接删整条更稳,且让模型知道「这步没成功」)。
- 游离 / 孤儿 tool 结果(找不到对应的 assistant tool_call)→ 丢弃。
- 重复 tool 结果(同一 call_id 出现多次)→ 只留第一条。
- 完整配对 → 原样返回,零改动。
"""
from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

Message = dict[str, Any]

# 占位结果文案:让模型明确这一步工具没有返回,而不是返回了空。
MISSING_TOOL_RESULT_TEXT = (
    "[openbear] 该工具调用未返回结果(可能被中止或上游中断),已插入占位以修复对话结构。"
)

SUMMARY_ACK_TEXT = "好的，我已了解此前的上下文。"
ROLE_ALTERNATION_BRIDGE_TEXT = "(继续)"
VISIBLE_HISTORY_INTRO = (
    "以下是压缩后保留的最近可见对话文本；仅包含用户消息和助手最终可见回复，"
    "工具结果、Agent 内部状态、Plan/通知 JSON、调试信息和 reasoning 均已排除。"
)


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") in {"text", "input_text", "output_text"}:
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") in {"image", "input_image"}:
                    label = str(
                        item.get("name") or item.get("path") or item.get("url") or "image"
                    ).strip()
                    parts.append(f"[图片: {label}]")
            elif item:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(value or "")


def _row_time_text(row: Any) -> str:
    try:
        ts = int(_row_value(row, "created_at", 0) or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return ""
    beijing = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(ts, tz=UTC).astimezone(beijing).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def build_visible_history_xml(rows: list[Any], *, max_messages: int = 100) -> str:
    """Render the compacted history tail as user-visible XML text only.

    The retained root-context tail exists to remind the model of the recent human
    conversation. It intentionally excludes tool outputs, AgentWait/Plan runtime
    payloads, reasoning, and assistant turns that only issued tool calls.
    """
    items: list[tuple[str, str, str]] = []
    for row in rows or []:
        role = str(_row_value(row, "role", "") or "")
        if role not in {"user", "assistant"}:
            continue
        if role == "assistant" and _row_value(row, "tool_calls", None):
            # Tool-call turns are execution scaffolding, not final visible replies.
            continue
        content = _message_text(_row_value(row, "content", "")).strip()
        if not content:
            continue
        items.append((role, _row_time_text(row), content))
    try:
        limit = max(0, int(max_messages or 0))
    except (TypeError, ValueError):
        limit = 100
    if limit > 0:
        items = items[-limit:]
    if not items:
        return ""

    lines = ["<history_messages>"]
    for role, time_text, content in items:
        time_attr = f' time="{html.escape(time_text, quote=True)}"' if time_text else ""
        lines.append(f"<{role}{time_attr}>\n{html.escape(content, quote=False)}\n</{role}>")
    lines.append("</history_messages>")
    return "\n".join(lines)


def build_summary_prefixed_visible_history(
    summary_text: str,
    recent_rows: list[Any],
    *,
    max_messages: int = 100,
) -> list[Message]:
    """Build root context as one summary unit plus an XML visible-history tail."""
    blocks: list[str] = []
    text = str(summary_text or "").strip()
    if text:
        blocks.append(f"[此前对话摘要]\n{text}")
    xml = build_visible_history_xml(recent_rows, max_messages=max_messages)
    if xml:
        blocks.append(f"{VISIBLE_HISTORY_INTRO}\n{xml}")
    if not blocks:
        return []
    return [
        {"role": "user", "content": "\n\n".join(blocks)},
        {"role": "assistant", "content": SUMMARY_ACK_TEXT},
    ]


def build_summary_prefixed_history(summary_text: str, recent_messages: list[Message]) -> list[Message]:
    """Prepend the compacted-history summary without breaking role alternation.

    A compacted transcript is represented as a synthetic user summary.  The old
    bridge also appended a synthetic assistant acknowledgement unconditionally;
    that is only safe when the first uncompressed message is a user.  If the kept
    window starts with an assistant (for example an assistant tool_call kept with
    its tool results), adding the ack would produce consecutive assistant roles
    and strict providers will reject the request before tool pairing is checked.
    """
    history: list[Message] = []
    text = str(summary_text or "").strip()
    if text:
        history.append({"role": "user", "content": f"[此前对话摘要]\n{text}"})
        first_role = ""
        for msg in recent_messages or []:
            if isinstance(msg, dict):
                first_role = str(msg.get("role") or "")
                break
        if first_role != "assistant":
            history.append({"role": "assistant", "content": SUMMARY_ACK_TEXT})
    history.extend(recent_messages or [])
    return history


def _tool_calls_of(msg: Message) -> list[dict]:
    """取出 assistant 消息里的 tool_calls 列表(可能是 ToolCall 对象或 dict)。"""
    tc = msg.get("tool_calls")
    return tc if isinstance(tc, list) else []


def _call_id_of(call: Any) -> str:
    """从一个 tool_call(ToolCall dataclass 或 dict)里取 id。"""
    if isinstance(call, dict):
        return str(call.get("id") or "")
    return str(getattr(call, "id", "") or "")


def _call_name_of(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("name") or "")
    return str(getattr(call, "name", "") or "")


def _make_missing_result(call_id: str, name: str) -> Message:
    """合成一条占位 tool 结果,字段与正常 tool 消息同构(见 base.py 中性格式)。"""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": MISSING_TOOL_RESULT_TEXT,
    }


def repair_tool_pairing(messages: list[Message]) -> list[Message]:
    """返回工具配对修复后的新列表(不修改入参对象)。

    遍历时,遇到带 tool_calls 的 assistant,就向后收集「直到下一个 assistant 之前」
    的所有 tool 结果,按 call_id 配对:配上的按 tool_calls 顺序排好,缺的补占位,
    多余/重复/孤儿的丢弃。非工具相关消息原样保留。
    """
    out: list[Message] = []
    n = len(messages)
    i = 0
    while i < n:
        msg = messages[i]
        role = msg.get("role") if isinstance(msg, dict) else None

        # 顶层游离 tool 结果(不在任何 assistant tool_calls 区间内)→ 孤儿,丢弃。
        if role == "tool":
            i += 1
            continue

        if role != "assistant":
            out.append(msg)
            i += 1
            continue

        calls = _tool_calls_of(msg)
        if not calls:
            # 普通 assistant(纯文本,无工具调用)→ 原样。
            out.append(msg)
            i += 1
            continue

        # 这是一个带工具调用的 assistant 轮:收集它之后、下一个 assistant 之前的所有消息。
        call_ids: list[str] = []
        seen_call_ids: set[str] = set()
        for c in calls:
            cid = _call_id_of(c)
            # 同一轮里理论上不会有重复 call_id,保险起见去重。
            if cid and cid not in seen_call_ids:
                call_ids.append(cid)
                seen_call_ids.add(cid)
        name_by_id = {_call_id_of(c): _call_name_of(c) for c in calls}

        results_by_id: dict[str, Message] = {}
        passthrough: list[Message] = []  # 区间内夹杂的非 tool 消息(罕见),保留
        j = i + 1
        while j < n:
            nxt = messages[j]
            nxt_role = nxt.get("role") if isinstance(nxt, dict) else None
            if nxt_role == "assistant":
                break  # 下一个 assistant 轮开始,本区间结束
            if nxt_role == "tool":
                rid = str(nxt.get("tool_call_id") or "")
                if rid in seen_call_ids and rid not in results_by_id:
                    results_by_id[rid] = nxt  # 配对成功,留第一条
                # 不匹配(孤儿)或重复 → 丢弃
            else:
                # 区间内夹杂的 user/system 等(正常流程几乎不出现),保留位置语义
                passthrough.append(nxt)
            j += 1

        # 先放 assistant 本身
        out.append(msg)
        # 按 tool_calls 声明顺序补齐结果:有则用真实结果,无则补占位
        for cid in call_ids:
            r = results_by_id.get(cid)
            out.append(r if r is not None else _make_missing_result(cid, name_by_id.get(cid, "")))
        # 夹杂的非工具消息接在后面
        out.extend(passthrough)

        i = j
    return out


def repair_role_alternation(messages: list[Message]) -> list[Message]:
    """Normalize strict-provider roles without rewriting an emitted prompt unit.

    Consecutive neutral ``user`` messages used to be merged into the earlier
    message. Once that earlier unit had reached a provider, a later Plan/control/
    retry/runtime append therefore rewrote the cached prefix. Insert a deterministic
    request-local assistant bridge instead. Neutral ``tool`` maps to Anthropic's
    user role, so a following user needs the same bridge. The original messages are
    retained byte-for-byte and each later message remains a true append.
    """
    out: list[Message] = []
    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue
        previous_role = (
            str(out[-1].get("role") or "")
            if out and isinstance(out[-1], dict)
            else ""
        )
        role = str(message.get("role") or "")
        if role == "user" and previous_role in {"user", "tool"}:
            out.append({"role": "assistant", "content": ROLE_ALTERNATION_BRIDGE_TEXT})
        out.append(message)
    return out
