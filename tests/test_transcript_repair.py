"""工具配对修复测试 —— 覆盖光杆 / 孤儿 / 重复 / 完整 / 多并行 等场景。"""
from __future__ import annotations

from types import SimpleNamespace

from app.agent.transcript_repair import (
    MISSING_TOOL_RESULT_TEXT,
    build_summary_prefixed_history,
    build_summary_prefixed_visible_history,
    build_visible_history_xml,
    repair_role_alternation,
    repair_tool_pairing,
)
from app.llm.events import ToolCall


def _asst_with_calls(*calls: ToolCall) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def _tool_result(call_id: str, name: str = "Read", content: str = "结果") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def test_complete_pairing_unchanged():
    """完整配对的历史 → 原样返回(逐条相等)。"""
    msgs = [
        {"role": "user", "content": "读文件"},
        _asst_with_calls(ToolCall(id="c1", name="Read", arguments="{}")),
        _tool_result("c1"),
        {"role": "assistant", "content": "读完了"},
    ]
    out = repair_tool_pairing(msgs)
    assert out == msgs


def test_single_dangling_tool_call_gets_placeholder():
    """单个光杆 tool_call → 补一条 isError 占位结果。"""
    msgs = [
        {"role": "user", "content": "搜一下"},
        _asst_with_calls(ToolCall(id="c1", name="Grep", arguments="{}")),
        {"role": "assistant", "content": "⏹ 已停止"},  # 中断:c1 没有结果
    ]
    out = repair_tool_pairing(msgs)
    # assistant(带calls) 之后必须紧跟一条 tool_call_id=c1 的占位
    assert out[1]["tool_calls"][0].id == "c1"
    assert out[2]["role"] == "tool"
    assert out[2]["tool_call_id"] == "c1"
    assert out[2]["content"] == MISSING_TOOL_RESULT_TEXT
    assert out[2]["name"] == "Grep"
    # 原 "⏹ 已停止" assistant 仍在,顺序在占位之后
    assert out[3]["content"] == "⏹ 已停止"


def test_three_parallel_all_dangling_repro_id923():
    """复现线上 id=923:一条 assistant 三个并行 tool_call,被停止时全无结果。"""
    msgs = [
        {"role": "user", "content": "查样式"},
        _asst_with_calls(
            ToolCall(id="call_7uCRc68", name="Grep", arguments="{}"),
            ToolCall(id="call_i90zhi", name="Grep", arguments="{}"),
            ToolCall(id="call_WvUcMr", name="Glob", arguments="{}"),
        ),
        {"role": "assistant", "content": "⏹ 已停止"},
    ]
    out = repair_tool_pairing(msgs)
    # 三个 call 各补一条占位,顺序与声明一致
    assert [m["tool_call_id"] for m in out[2:5]] == ["call_7uCRc68", "call_i90zhi", "call_WvUcMr"]
    assert all(m["role"] == "tool" and m["content"] == MISSING_TOOL_RESULT_TEXT for m in out[2:5])
    assert out[5]["content"] == "⏹ 已停止"


def test_partial_pairing_only_fills_missing():
    """3 个 call,2 个有结果、1 个缺 → 只补缺的那个,真实结果保留。"""
    msgs = [
        _asst_with_calls(
            ToolCall(id="a", name="Read", arguments="{}"),
            ToolCall(id="b", name="Read", arguments="{}"),
            ToolCall(id="c", name="Read", arguments="{}"),
        ),
        _tool_result("a", content="A内容"),
        _tool_result("c", content="C内容"),  # b 缺失
    ]
    out = repair_tool_pairing(msgs)
    results = out[1:4]
    assert [r["tool_call_id"] for r in results] == ["a", "b", "c"]  # 顺序按 calls 声明
    assert results[0]["content"] == "A内容"
    assert results[1]["content"] == MISSING_TOOL_RESULT_TEXT  # b 补占位
    assert results[2]["content"] == "C内容"


def test_orphan_tool_result_dropped():
    """游离 tool 结果(无匹配 assistant tool_call)→ 丢弃。"""
    msgs = [
        {"role": "user", "content": "hi"},
        _tool_result("ghost"),  # 没有任何 assistant 发起过 ghost
        {"role": "assistant", "content": "你好"},
    ]
    out = repair_tool_pairing(msgs)
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]


def test_duplicate_tool_result_deduped():
    """同一 call_id 的重复 tool 结果 → 只留第一条。"""
    msgs = [
        _asst_with_calls(ToolCall(id="c1", name="Read", arguments="{}")),
        _tool_result("c1", content="第一份"),
        _tool_result("c1", content="第二份"),  # 重复
    ]
    out = repair_tool_pairing(msgs)
    tools = [m for m in out if m["role"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["content"] == "第一份"


def test_no_tool_calls_passthrough():
    """普通多轮对话(无任何工具)→ 原样返回。"""
    msgs = [
        {"role": "user", "content": "1"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "3"},
        {"role": "assistant", "content": "4"},
    ]
    out = repair_tool_pairing(msgs)
    assert out == msgs


def test_empty_input():
    assert repair_tool_pairing([]) == []


# ── 角色交替规整:repair_role_alternation ─────────────────────────

def test_summary_prefix_omits_ack_when_kept_starts_with_assistant_tool_call():
    recent = [
        _asst_with_calls(ToolCall(id="c1", name="Read", arguments="{}")),
        _tool_result("c1"),
        {"role": "user", "content": "继续"},
    ]
    history = build_summary_prefixed_history("summary", recent)
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "user"]
    assert "此前对话摘要" in history[0]["content"]
    assert history[1]["tool_calls"][0].id == "c1"
    assert repair_tool_pairing(history) == history


def test_summary_prefix_keeps_ack_when_kept_starts_with_user():
    history = build_summary_prefixed_history("summary", [{"role": "user", "content": "继续"}])
    assert [m["role"] for m in history] == ["user", "assistant", "user"]


def test_alternation_already_ok_unchanged():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    assert repair_role_alternation(msgs) == msgs


def test_alternation_bridges_two_text_users_without_rewriting_first_unit():
    """A later retry/steer is appended after a deterministic assistant bridge."""
    msgs = [
        {"role": "user", "content": "老大的问题"},
        {"role": "user", "content": "你刚才没有输出任何回复。请直接给出最终回答。"},
    ]
    first_request = repair_role_alternation(msgs[:1])
    out = repair_role_alternation(msgs)
    assert out[:len(first_request)] == first_request
    assert [message["role"] for message in out] == ["user", "assistant", "user"]
    assert out[0] == msgs[0]
    assert out[1]["content"] == "(继续)"
    assert out[2] == msgs[1]


def test_alternation_three_consecutive_users_remains_append_only():
    msgs = [
        {"role": "user", "content": "x"},
        {"role": "user", "content": "y"},
        {"role": "user", "content": "z"},
    ]
    first = repair_role_alternation(msgs[:2])
    out = repair_role_alternation(msgs)
    assert out[:len(first)] == first
    assert [message["role"] for message in out] == ["user", "assistant", "user", "assistant", "user"]
    assert [message["content"] for message in out[::2]] == ["x", "y", "z"]


def test_alternation_does_not_rewrite_existing_user_assistant_user_prefix():
    msgs = [
        {"role": "user", "content": "1"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "3"},
        {"role": "user", "content": "4"},
    ]
    first = repair_role_alternation(msgs[:3])
    out = repair_role_alternation(msgs)
    assert out[:len(first)] == first
    assert [message["role"] for message in out] == ["user", "assistant", "user", "assistant", "user"]
    assert out[-1]["content"] == "4"


def test_alternation_non_str_content_inserts_placeholder():
    """content 非 str时同样插入bridge，不文本化多模态内容。"""
    blocks = [{"type": "image", "url": "x"}]
    msgs = [
        {"role": "user", "content": "文字"},
        {"role": "user", "content": blocks},
    ]
    out = repair_role_alternation(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[0]["content"] == "文字"
    assert out[2]["content"] == blocks


def test_alternation_tool_to_user_inserts_anthropic_safe_append_only_bridge():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [ToolCall(id="c1", name="Read", arguments="{}")]},
        {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": "r"},
        {"role": "user", "content": "接着说"},
    ]
    first = repair_role_alternation(msgs[:2])
    out = repair_role_alternation(msgs)
    assert out[:len(first)] == first
    assert [message["role"] for message in out] == ["assistant", "tool", "assistant", "user"]
    assert out[2]["content"] == "(继续)"


def test_alternation_empty_input():
    assert repair_role_alternation([]) == []


def test_visible_history_xml_keeps_only_user_and_final_assistant_text():
    rows = [
        SimpleNamespace(role="user", content="我说 <保留>", created_at=1, tool_calls=[]),
        SimpleNamespace(
            role="assistant", content="正在调用工具", created_at=2,
            tool_calls=[ToolCall(id="call-1", name="Read", arguments="{}")],
        ),
        SimpleNamespace(role="tool", content="巨大 AgentWait / Plan JSON", created_at=3, tool_calls=[]),
        SimpleNamespace(role="assistant", content="最终 <回复>", created_at=4, tool_calls=[]),
    ]

    xml = build_visible_history_xml(rows)
    history = build_summary_prefixed_visible_history("摘要", rows)

    assert xml.startswith("<history_messages>")
    assert '<user time="1970-01-01 08:00:01">' in xml
    assert "我说 &lt;保留&gt;" in xml
    assert "最终 &lt;回复&gt;" in xml
    assert "正在调用工具" not in xml
    assert "AgentWait" not in xml
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert "[此前对话摘要]\n摘要" in history[0]["content"]
    assert xml in history[0]["content"]
