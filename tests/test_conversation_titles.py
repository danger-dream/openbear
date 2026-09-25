from app.conversation_titles import (
    DEFAULT_NAMING_PROMPT,
    GENERATED_TITLE_MAX_DISPLAY_UNITS,
    clean_generated_title,
    display_half_units,
    initial_conversation_title,
    naming_attempt_models,
    render_naming_transcript,
    select_naming_turns,
    truncate_display_title,
)


def test_title_width_and_truncation_preserve_graphemes():
    assert display_half_units("会话ABC123") == 10
    assert display_half_units("e\u0301") == 1
    assert display_half_units("👨‍👩‍👧‍👦") == 2
    assert truncate_display_title("一二三四五六七八九十十一十二十三") == "一二三四五六七八九十十一"
    assert truncate_display_title("abcdefghijklmnopqrstuvwxy") == "abcdefghijklmnopqrstuvwx"
    assert truncate_display_title("一二三四五六七八九十甲👨‍👩‍👧‍👦尾") == "一二三四五六七八九十甲👨‍👩‍👧‍👦"
    assert initial_conversation_title("  OpenBear   会话命名功能需要调整  ") == "OpenBear 会话命名功能需"


def test_generated_title_cleanup_is_bounded():
    assert clean_generated_title('标题："会话智能命名方案"') == "会话智能命名方案"
    assert clean_generated_title("```text\nQwen-Image-2.1部署调研\n```") == "Qwen-Image-2.1部署调研"
    assert display_half_units(clean_generated_title("标题：" + "会话" * 50)) <= GENERATED_TITLE_MAX_DISPLAY_UNITS * 2


def test_default_naming_prompt_requests_concise_task_titles():
    assert all(rule in DEFAULT_NAMING_PROMPT for rule in (
        "single-line task title of at most 16 characters and under five words where possible",
        "Start with an imperative verb.",
        "proper nouns, acronyms, or code terms",
        "Preserve ticket references exactly.",
        "Write in the user's language.",
        "Do not use quotes, markdown, or trailing punctuation.",
        "Do not answer the request.",
    ))


def test_title_cleanup_preserves_model_output():
    title = "分析 zcode parrot claude 使用问题"
    assert display_half_units(title) > 12 * 2
    assert clean_generated_title(f"标题：{title}") == title
    assert clean_generated_title("Claude 使用指南") == "Claude 使用指南"
    assert clean_generated_title("鹦鹉项目分析") == "鹦鹉项目分析"


def test_naming_turn_selection_uses_recent_turns_and_preserves_all_user_text():
    turns = [
        {"user": f"用户{i}" + ("U" * (i * 2)), "assistant": "A" * (20 + i)}
        for i in range(1, 6)
    ]
    selected = select_naming_turns(turns, max_turns=3, max_chars=40)
    assert [item["user"][:3] for item in selected] == ["用户3", "用户4", "用户5"]
    assert [item["user"] for item in selected] == [turn["user"] for turn in turns[-3:]]
    assert sum(len(item["assistant"]) for item in selected) <= max(
        0, 40 - sum(len(item["user"]) for item in selected)
    )
    assert "第 1 轮\n用户：用户3" in render_naming_transcript(selected)

    oversized = select_naming_turns(
        [{"user": "用户输入" * 20, "assistant": "助手输出" * 20}],
        max_turns=0,
        max_chars=10,
    )
    assert oversized[0]["user"] == "用户输入" * 20
    assert oversized[0]["assistant"] == ""


def test_naming_attempts_count_first_call_separately_and_rotate_candidates():
    assert naming_attempt_models(["A"], 3) == ["A", "A", "A", "A"]
    assert naming_attempt_models(["A", "B"], 3) == ["A", "B", "A", "B"]
    assert naming_attempt_models(["A", "B", "C"], 3) == ["A", "B", "C", "A"]
    assert naming_attempt_models(["A", "B", "C", "D", "E"], 3) == ["A", "B", "C", "D"]
    assert naming_attempt_models([], 3) == []
