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
- 同一 assistant 中完全相同的重复调用 → 仅在派生模型视图去重；原始消息不变。
- 同 ID 但调用内容冲突 → 拒绝构造视图；不能猜测哪个调用或结果有效。
- 完整配对 → 原样返回,零改动。
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
from typing import Any

Message = dict[str, Any]

# 占位结果文案:让模型明确这一步工具没有返回,而不是返回了空。
MISSING_TOOL_RESULT_TEXT = (
    "[openbear] 该工具调用未返回结果(可能被中止或上游中断),已插入占位以修复对话结构。可能已发生副作用；不得视为未执行或自动重放。"
)

# Non-empty placeholder so strict providers (Anthropic-style alternation)
# accept the sequence. Deliberately machine-marked: a natural phrase like
# "(继续)" gets imitated by the model as a final answer after enough
# repetitions in context (observed live, message 113539). A protocol marker
# is never mistaken for the assistant's own words.
ROLE_ALTERNATION_BRIDGE_TEXT = "[protocol: role-alternation bridge]"


def is_role_alternation_bridge(text: Any) -> bool:
    """True when model output is only the request-local bridge marker.

    The marker is inserted by OpenBear for strict role alternation. If a model
    echoes it as its whole answer, treat it like an empty response rather than
    a user-visible final answer.
    """
    return isinstance(text, str) and text.strip() == ROLE_ALTERNATION_BRIDGE_TEXT


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


class ConflictingToolCalls(ValueError):
    """Ambiguous historical batch; preserve both originals and fail before replay."""

    def __init__(self, call_id: str, calls: tuple[Any, ...], source_id: str = "") -> None:
        self.call_id = call_id
        self.calls = calls
        self.source_id = source_id
        # Do not include private arguments in exceptions/logs.
        super().__init__(f"conflicting_historical_tool_calls: id={call_id!r} source={source_id!r}")


def _call_payload(call: Any) -> Any:
    """Compare entire calls, including additional fields in legacy dict payloads."""
    return dict(call) if isinstance(call, dict) else asdict(call) if is_dataclass(call) else call


def _dedupe_assistant_calls(msg: Message, calls: list[Any]) -> tuple[Message, list[Any]]:
    """Derive a closed neutral replay unit without changing its archived source."""
    first_by_id: dict[str, Any] = {}
    unique: list[Any] = []
    duplicates = False
    for call in calls:
        cid = _call_id_of(call)
        if cid and cid in first_by_id:
            if _call_payload(call) != _call_payload(first_by_id[cid]):
                source = msg.get("openbear_context_source") or {}
                source_id = str(source.get("id") or "") if isinstance(source, dict) else ""
                raise ConflictingToolCalls(cid, (first_by_id[cid], call), source_id)
            duplicates = True
            continue
        unique.append(call)
        if cid:
            first_by_id[cid] = call
    if not duplicates:
        return msg, calls

    derived = {**msg, "tool_calls": unique}
    # A signed/opaque provider turn may bind the complete original call set;
    # never replay a partially rewritten native turn or its signature.
    for key in ("native_output_items", "reasoning", "signature"):
        derived.pop(key, None)
    source = msg.get("openbear_context_source")
    if isinstance(source, dict) and source.get("id"):
        # WindowStore fingerprints the neutral payload for each source ID. A
        # modified view must have its own stable ID, linked to the real source;
        # it must not masquerade as an unmodified reference-only DB row.
        payload = [_call_payload(call) for call in calls]
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           default=str).encode("utf-8")).hexdigest()
        derived["openbear_context_source"] = {
            **source, "id": f"{source['id']}/dedup:{digest}",
            "derived_from": source["id"], "message_id": 0, "reference_only": False,
        }
    return derived, unique


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

        # Only the model compatibility view is normalized. Do not alter durable
        # messages or execute/replay any historical tool calls.
        assistant, calls = _dedupe_assistant_calls(msg, calls)

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
        redundant_results: dict[str, list[int]] = {}
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
                elif assistant is not msg and rid in results_by_id:
                    source = nxt.get("openbear_context_source") or {}
                    source_id = str(source.get("id") or "") if isinstance(source, dict) else ""
                    row_id = source.get("message_id") if isinstance(source, dict) else None
                    if (isinstance(row_id, int) and row_id > 0 and source_id == f"message:{row_id}"):
                        redundant_results.setdefault(rid, []).append(row_id)
                # 不匹配(孤儿)或重复 → 丢弃；有来源的重复结果另留边界证明
            else:
                # 区间内夹杂的 user/system 等(正常流程几乎不出现),保留位置语义
                passthrough.append(nxt)
            j += 1

        # 先放 assistant 本身
        out.append(assistant)
        # 按 tool_calls 声明顺序补齐结果:有则用真实结果,无则补占位
        for cid in call_ids:
            r = results_by_id.get(cid)
            if r is not None and redundant_results.get(cid):
                original_source = r.get("openbear_context_source")
                if isinstance(original_source, dict) and original_source.get("id"):
                    # The original result is unchanged; its neutral view carries
                    # only a trace of extra raw rows omitted by first-result pairing.
                    r = {**r, "openbear_context_source": {
                        **original_source, "covered_duplicate_result_ids": redundant_results[cid],
                    }}
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
