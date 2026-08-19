from __future__ import annotations

import json

from app.stream.tool_progress import (
    format_agent_batch_progress_card,
    format_tool_line,
    format_tool_result_line,
    format_tool_running_status_line,
    format_user_interaction_result_line,
    format_user_interaction_wait_line,
)


def test_agent_line_shows_worker_and_task():
    args = json.dumps({
        "workerType": "code-reviewer",
        "description": "只读审查实现侧证据",
        "prompt": "完整审查 brief",
    }, ensure_ascii=False)

    line = format_tool_line("Agent", args)
    status = format_tool_running_status_line("Agent", args)

    assert "<blockquote" in line
    assert "Agent" in line
    assert "code-reviewer" in line
    assert "只读审查实现侧证据" in line
    assert status == "Agent 执行中：code-reviewer · 只读审查实现侧证据"


def test_agent_card_escapes_dynamic_text():
    args = json.dumps(
        {"workerType": "<b>坏专家</b>", "description": "查 </blockquote><b>逃逸</b>"},
        ensure_ascii=False,
    )

    line = format_tool_line("Agent", args)

    assert "<blockquote" in line
    assert "&lt;b&gt;坏专家&lt;/b&gt;" in line
    assert "&lt;/blockquote&gt;" in line
    assert "<b>坏专家</b>" not in line


def test_agent_progress_card_includes_three_recent_child_events():
    card = format_agent_batch_progress_card([
        {
            "agent": "项目开发专家",
            "title": "只读梳理 OpenBear 代码设计",
            "status": "running",
            "currentStatus": "模型调用中",
            "durationMs": 185_000,
            "task": {"modelCalls": 1, "toolCalls": 2},
            "recentEvents": [
                {"kind": "task_started", "summary": "项目开发专家 已启动", "elapsedMs": 1000},
                {"kind": "tool_call_started", "summary": "调用工具 Read", "detail": {"name": "Read", "arguments": "{\"path\":\"app/tools/agents.py\"}"}, "elapsedMs": 10_000},
                {"kind": "tool_call_finished", "summary": "工具 Read 调用完成", "detail": {"name": "Read", "durationMs": 1200}, "elapsedMs": 12_000},
                {"kind": "model_call_started", "summary": "模型调用开始", "detail": {"modelLabel": "openai/gpt"}, "elapsedMs": 20_000},
            ],
        }
    ], duration_ms=185_000)

    assert "↳ 当前：模型调用中" in card
    assert "项目开发专家 已启动" not in card
    assert "✅ 工具完成：Read · 1.2s" in card
    assert "🔄 模型调用开始：openai/gpt · 已 2m45s" in card
    assert card.count("↳") == 4  # 当前状态 + 最近 3 条动态



def test_agent_result_line_includes_usage():
    result = json.dumps({
        "agentSession": {"title": "项目开发专家"},
        "task": {
            "status": "completed",
            "modelCalls": 2,
            "toolCalls": 1,
            "tokens": {"input": 1000, "output": 234, "cache": 500},
            "costUsd": 0.0101,
        },
    }, ensure_ascii=False)

    line = format_tool_result_line("Agent", "{}", result, 1234)

    assert "<blockquote" in line
    assert "项目开发专家" in line
    assert "Agent 完成：项目开发专家" in line
    assert "🔄 模型调用 2 次" in line
    assert "🛠 工具调用 1 次" in line
    assert "📊 Tokens：1.2k · ↑1.0k · ↓234 · 缓存 500(50.0%)" in line
    assert "💰 $0.0101" in line


def test_agent_result_line_warns_on_failure():
    result = json.dumps({
        "agentSession": {"title": "测试验证专家"},
        "task": {"status": "failed", "modelCalls": 1, "toolCalls": 2},
    }, ensure_ascii=False)

    line = format_tool_result_line("Agent", "{}", result, 1200)

    assert "❌ Agent 失败：测试验证专家" in line
    assert "✅ Agent 完成" not in line


def test_user_select_result_line_is_human_readable():
    line = format_user_interaction_result_line(
        "UserInteraction",
        json.dumps({"action": "select", "title": "选择方向"}, ensure_ascii=False),
        json.dumps({"status": "answered", "selectedLabels": ["提示词调优专家"]}, ensure_ascii=False),
    )
    assert line == "✅ 用户已选择：提示词调优专家"


def test_user_prompt_result_line_respects_sensitive_flag():
    line = format_user_interaction_result_line(
        "UserInteraction",
        json.dumps({"action": "prompt", "title": "输入密钥", "sensitive": True}, ensure_ascii=False),
        json.dumps({"status": "answered", "value": "secret-value"}, ensure_ascii=False),
    )
    assert line == "✍️ 用户已输入文本（已隐藏）。"


def test_user_confirm_cancel_result_line():
    line = format_user_interaction_result_line(
        "UserInteraction",
        json.dumps({"action": "confirm", "title": "确认修改"}, ensure_ascii=False),
        json.dumps({"status": "answered", "confirmed": False, "label": "取消"}, ensure_ascii=False),
    )
    assert line == "🚫 用户已取消：取消"


def test_wait_line_uses_title():
    assert format_user_interaction_wait_line("UserInteraction", json.dumps({"action": "prompt", "title": "补充说明"}, ensure_ascii=False)) == "等待用户输入：补充说明"


def test_questionnaire_progress_covers_wait_success_cancel_timeout_error_without_answer_leak():
    arguments = json.dumps({"action": "questionnaire", "title": "核心需求澄清"}, ensure_ascii=False)
    assert format_user_interaction_wait_line("UserInteraction", arguments) == "等待用户填写需求问卷：核心需求澄清"
    long_secret = "绝不能出现在进度里" * 100
    success = format_user_interaction_result_line(
        "UserInteraction", arguments,
        json.dumps({"status": "answered", "answers": [{"questionId": "q", "text": long_secret}]}, ensure_ascii=False),
    )
    assert success == "✅ 用户已提交需求问卷（1 题）。"
    assert long_secret not in success
    assert format_user_interaction_result_line(
        "UserInteraction", arguments, json.dumps({"status": "cancelled", "cancelled": True, "answers": []}),
    ) == "🚫 用户已取消需求问卷。"
    timeout = format_user_interaction_result_line(
        "UserInteraction", arguments, json.dumps({"status": "timeout", "cancelled": True, "answers": []}),
    )
    assert timeout == "⌛ 需求问卷已超时，未采用任何推荐或默认答案。"
    assert "默认值继续" not in timeout
    assert format_user_interaction_result_line(
        "UserInteraction", arguments, json.dumps({"status": "error", "error": "invalid_questionnaire"}),
    ) == "❌ 需求问卷创建失败。"


def test_agent_result_line_for_control_state_includes_recent_events():
    result = json.dumps({
        "ok": False,
        "status": "needs_openbear_control",
        "agentSession": {"title": "资料调研专家"},
        "task": {
            "status": "needs_openbear_control",
            "modelCalls": 15,
            "toolCalls": 80,
            "tokens": {"input": 1_100_000, "output": 9_400, "cache": 901_100},
            "costUsd": 1.1906,
        },
        "recentEvents": [
            {"kind": "tool_call_started", "summary": "调用工具 WebExtract", "detail": {"name": "WebExtract", "arguments": "{\"url\":\"https://example.com/reddit\"}"}},
            {"kind": "tool_call_finished", "summary": "工具 WebExtract 调用完成", "detail": {"name": "WebExtract", "durationMs": 1500}},
            {"kind": "agent_budget_exhausted", "summary": "工具调用达到安全预算，等待 OpenBear 裁决", "detail": {"kind": "tool", "used": 80, "limit": 80}},
            {"kind": "needs_openbear_control", "summary": "需要 OpenBear 裁决：agent_task_budget_exceeded", "detail": {"reason": "agent_task_budget_exceeded"}},
        ],
    }, ensure_ascii=False)

    line = format_tool_result_line("Agent", "{}", result, 6_021_000)

    assert "Agent 等待 OpenBear 裁决：资料调研专家" in line
    assert "状态 等待 OpenBear 裁决" in line
    assert "Agent 完成" not in line
    assert "最近动态：" in line
    assert "工具调用达到安全预算，等待 OpenBear 裁决" in line
    assert "需要 OpenBear 裁决：agent_task_budget_exceeded" in line


def test_task_memory_tool_line_names_action_and_target():
    assert format_tool_line("TaskMemory", json.dumps({"action": "list"}, ensure_ascii=False)) == (
        "🔧 TaskMemory: 获取记忆列表"
    )
    assert format_tool_line("TaskMemory", json.dumps({
        "action": "create",
        "name": "技术验证",
        "description": "用于验证工具调用会完整显示动作、对象名称、描述和正文。",
    }, ensure_ascii=False)) == (
        "🔧 TaskMemory: 创建记忆： 用于验证工具调用会完整显示动作、对象名称、描述和正文。"
    )
