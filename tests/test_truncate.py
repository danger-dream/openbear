"""工具结果智能截断测试。"""
from __future__ import annotations

import json

from app.tools.base import ToolRegistry, max_tool_result_chars
from app.tools.truncate import truncate_tool_result


def test_under_limit_unchanged():
    assert truncate_tool_result("short", 1000) == "short"


def test_head_only_truncation():
    text = "A" * 50000
    out = truncate_tool_result(text, 5000)
    assert len(out) <= 5000
    assert out.startswith("A")
    assert "截断" in out


def test_head_tail_keeps_error_at_end():
    """尾部有 error → head+tail 保留尾部错误。"""
    body = "正常输出行\n" * 3000
    text = body + "\nTraceback (most recent call last):\n  RuntimeError: 关键错误"
    out = truncate_tool_result(text, 6000)
    assert len(out) <= 6000
    # 尾部错误必须保留
    assert "RuntimeError: 关键错误" in out
    assert "中间内容已省略" in out


def test_head_tail_keeps_summary():
    body = "data line\n" * 3000
    text = body + "\nTotal: 12345 files processed, done"
    out = truncate_tool_result(text, 6000)
    assert "Total: 12345" in out or "done" in out


def test_json_tail_preserved():
    body = '{"items": [\n' + ('  {"x": 1},\n' * 3000)
    text = body + '  {"last": true}\n]}'
    out = truncate_tool_result(text, 6000)
    assert len(out) <= 6000
    # JSON 闭合尾部保留
    assert "}" in out[-50:]


def test_max_chars_from_context_window():
    # 200K context → 30% = 60K tokens → 240K chars, 但默认硬顶 32K
    assert max_tool_result_chars(200000) == 32000
    # 小 context: 1000 tokens → 300 tokens → 1200 chars
    assert max_tool_result_chars(1000) == 1200
    # 硬顶可由配置下调/上调
    assert max_tool_result_chars(200000, hard_cap_chars=16000) == 16000


async def test_registry_applies_truncation():
    reg = ToolRegistry()

    async def _big(args):
        return "X" * 100000

    reg.add("big", "big", {"type": "object"}, _big)
    out = await reg.dispatch("big", "{}", max_chars=5000)
    assert len(out) <= 5000
    assert "截断" in out


async def test_registry_no_truncation_under_limit():
    reg = ToolRegistry()

    async def _small(args):
        return "tiny output"

    reg.add("small", "small", {"type": "object"}, _small)
    out = await reg.dispatch("small", "{}", max_chars=5000)
    assert out == "tiny output"
