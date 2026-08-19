"""tool-call 参数 JSON 容错修复(#13)测试。

覆盖 extract_balanced_json 纯函数 + ToolRegistry.dispatch 的容错接入。
"""
from __future__ import annotations

from app.tools.base import ToolRegistry
from app.tools.json_repair import extract_balanced_json

# ── 纯函数:extract_balanced_json ────────────────────────────────

def test_plain_object():
    assert extract_balanced_json('{"a": 1}') == {"a": 1}


def test_markdown_fenced_json():
    raw = '```json\n{"command": "ls -la"}\n```'
    assert extract_balanced_json(raw) == {"command": "ls -la"}


def test_markdown_fenced_no_lang():
    raw = '```\n{"x": true}\n```'
    assert extract_balanced_json(raw) == {"x": True}


def test_prefix_suffix_noise():
    raw = '好的,我来调用:{"path": "/tmp/a"} 这样就可以了'
    assert extract_balanced_json(raw) == {"path": "/tmp/a"}


def test_braces_inside_string_not_confused():
    """字符串字面量里的花括号不能干扰配平。"""
    raw = 'junk {"code": "if (x) { return {1}; }"} tail'
    assert extract_balanced_json(raw) == {"code": "if (x) { return {1}; }"}


def test_escaped_quote_inside_string():
    raw = r'{"q": "she said \"hi\" to me"} trailing'
    assert extract_balanced_json(raw) == {"q": 'she said "hi" to me'}


def test_nested_object():
    raw = 'x {"a": {"b": {"c": [1, 2, 3]}}} y'
    assert extract_balanced_json(raw) == {"a": {"b": {"c": [1, 2, 3]}}}


def test_array_payload():
    raw = '```json\n[1, 2, {"k": "v"}]\n```'
    assert extract_balanced_json(raw) == [1, 2, {"k": "v"}]


def test_unbalanced_returns_none():
    assert extract_balanced_json('{"a": 1') is None


def test_total_garbage_returns_none():
    assert extract_balanced_json("not json at all") is None


def test_empty_returns_none():
    assert extract_balanced_json("") is None


# ── 集成:dispatch 容错 ──────────────────────────────────────────

def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def echo(args):
        return f"got:{args.get('msg', '')}"

    reg.add("echo", "echo", {"type": "object"}, echo)
    return reg


async def test_dispatch_valid_json_unchanged():
    reg = _registry()
    out = await reg.dispatch("echo", '{"msg": "hi"}')
    assert out == "got:hi"


async def test_dispatch_repairs_markdown_wrapped():
    reg = _registry()
    out = await reg.dispatch("echo", '```json\n{"msg": "wrapped"}\n```')
    assert out == "got:wrapped"


async def test_dispatch_repairs_noisy_prefix():
    reg = _registry()
    out = await reg.dispatch("echo", 'sure: {"msg": "x"} done')
    assert out == "got:x"


async def test_dispatch_empty_args_ok():
    reg = _registry()
    out = await reg.dispatch("echo", "")
    assert out == "got:"


async def test_dispatch_irreparable_reports_error():
    reg = _registry()
    out = await reg.dispatch("echo", '{"msg": broken')
    assert out.startswith("error: 工具参数不是合法 JSON")


async def test_dispatch_non_object_after_repair_rejected():
    """修复出来是数组而非对象 → 仍按「必须是对象」拒绝。"""
    reg = _registry()
    out = await reg.dispatch("echo", "```json\n[1,2,3]\n```")
    assert out == "error: 工具参数必须是 JSON 对象"
