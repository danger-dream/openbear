"""工具配对修复测试 —— 覆盖光杆 / 孤儿 / 重复 / 完整 / 多并行 等场景。"""
from __future__ import annotations

import copy

import pytest

from app.agent.native_continuation import validate_model_context
from app.agent.transcript_repair import (
    ConflictingToolCalls,
    MISSING_TOOL_RESULT_TEXT,
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


def test_identical_duplicate_calls_are_only_removed_from_derived_model_view():
    original = _asst_with_calls(
        ToolCall("same", "Read", '{"path":"a"}'),
        ToolCall("other", "History", "{}"),
        ToolCall("same", "Read", '{"path":"a"}'),
    )
    original["openbear_context_source"] = {"id": "message:42", "kind": "execution",
                                            "message_id": 42, "reference_only": True}
    messages = [original, _tool_result("same", content="actual"), _tool_result("other")]
    snapshot = copy.deepcopy(messages)
    result = repair_tool_pairing(messages)
    assert messages == snapshot
    assert result[0] is not original
    assert [c.id for c in result[0]["tool_calls"]] == ["same", "other"]
    assert result[0]["tool_calls"][0] is original["tool_calls"][0]
    assert [m["content"] for m in result[1:]] == ["actual", "结果"]
    assert validate_model_context(result)
    source = result[0]["openbear_context_source"]
    assert source["id"].startswith("message:42/dedup:")
    assert source["derived_from"] == "message:42"
    assert source["message_id"] == 0 and source["reference_only"] is False
    assert original["openbear_context_source"] == snapshot[0]["openbear_context_source"]
    assert repair_tool_pairing(messages)[0]["openbear_context_source"]["id"] == source["id"]
    assert repair_tool_pairing(result) == result  # already closed and idempotent


def test_duplicate_calls_without_results_get_one_uncertain_placeholder():
    messages = [_asst_with_calls(ToolCall("same", "Bash", "{}"), ToolCall("same", "Bash", "{}"))]
    result = repair_tool_pairing(messages)
    assert len(result[0]["tool_calls"]) == 1
    assert result[1]["content"] == MISSING_TOOL_RESULT_TEXT
    assert len(result) == 2 and validate_model_context(result)
    assert len(messages[0]["tool_calls"]) == 2


@pytest.mark.parametrize("calls", [
    [ToolCall("same", "Read", '{"path":"a"}'), ToolCall("same", "Read", '{"path":"b"}')],
    [ToolCall("same", "Read", "{}"), ToolCall("same", "Bash", "{}")],
    [{"id": "same", "name": "Read", "arguments": "{}", "legacy": "a"},
     {"id": "same", "name": "Read", "arguments": "{}", "legacy": "b"}],
])
def test_conflicting_calls_fail_closed_with_originals_accessible(calls):
    original = {"role": "assistant", "content": "", "tool_calls": calls,
                "openbear_context_source": {"id": "message:99", "kind": "execution"}}
    snapshot = copy.deepcopy(original)
    with pytest.raises(ConflictingToolCalls) as raised:
        repair_tool_pairing([original, _tool_result("same", content="unknown owner")])
    assert raised.value.call_id == "same" and raised.value.source_id == "message:99"
    assert raised.value.calls == tuple(calls)
    assert "path" not in str(raised.value) and "unknown owner" not in str(raised.value)
    assert original == snapshot  # no arbitrary winner, no fabricated successful result


def test_edited_turn_drops_native_and_signed_state_but_complete_turn_preserves_it():
    call = ToolCall("same", "Read", "{}")
    native = [{"type": "function_call", "call_id": "same", "name": "Read", "arguments": "{}"}]
    original = _asst_with_calls(call, copy.deepcopy(call))
    original.update(native_output_items=native, reasoning="old reasoning", signature="old signature")
    result = repair_tool_pairing([original, _tool_result("same")])
    assert validate_model_context(result)
    assert not any(k in result[0] for k in ("native_output_items", "reasoning", "signature"))
    assert original["native_output_items"] == native and original["reasoning"] == "old reasoning"
    complete = _asst_with_calls(call)
    complete["native_output_items"] = native
    full = [complete, _tool_result("same")]
    assert repair_tool_pairing(full)[0] is complete
    assert validate_model_context(full)


async def test_derived_source_archives_payload_not_a_changed_original(tmp_path):
    from app.context.store import ContextOwner, WindowStore
    from app.db.engine import DB
    db = DB(str(tmp_path / "source-proof.db"))
    await db.connect()
    try:
        store = WindowStore(db, ContextOwner.controller(chat_id=42, session_uuid="test"))
        original = _asst_with_calls(ToolCall("same", "Read", "{}"), ToolCall("same", "Read", "{}"))
        original["openbear_context_source"] = {"id": "message:42", "kind": "execution",
                                               "message_id": 42, "reference_only": True}
        tool = _tool_result("same")
        tool["openbear_context_source"] = {"id": "message:43", "kind": "execution",
                                          "message_id": 0, "reference_only": False}
        result = repair_tool_pairing([original, tool])
        await store.archive(result)
        derived_id = result[0]["openbear_context_source"]["id"]
        event = await store.event_payload(derived_id)
        assert event["payload"]["tool_calls"] == [{"id": "same", "name": "Read", "arguments": "{}"}]
        assert original["tool_calls"] != result[0]["tool_calls"]
        assert event["message_id"] is None
        assert await store.archive(result) == {"revision": 0, "sourceRevision": 2, "highWater": 2, "added": 0}
    finally:
        await db.close()


# ── 角色交替规整:repair_role_alternation ─────────────────────────





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
    assert out[1]["content"] == "[protocol: role-alternation bridge]"
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
    assert out[2]["content"] == "[protocol: role-alternation bridge]"


def test_alternation_empty_input():
    assert repair_role_alternation([]) == []


def test_is_role_alternation_bridge_matches_only_exact_marker():
    from app.agent.transcript_repair import is_role_alternation_bridge

    assert is_role_alternation_bridge("[protocol: role-alternation bridge]")
    assert is_role_alternation_bridge("  [protocol: role-alternation bridge]\n")
    assert not is_role_alternation_bridge("说明 [protocol: role-alternation bridge]")
    assert not is_role_alternation_bridge("")
    assert not is_role_alternation_bridge(None)
    assert not is_role_alternation_bridge([{"type": "text"}])
