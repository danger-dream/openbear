"""tool-call 参数 JSON 容错修复(#13)。

模型偶尔会把工具参数写成「带 markdown 包裹 / 前后有解释文字 / 末尾多杂质」的形式,
导致 json.loads 直接失败、整轮工具调用作废。这里做一层保守的「括号配平提取」:
从首个 '{'(或 '[')起按嵌套深度找到与之匹配的闭合括号,正确跳过字符串字面量内部的
括号与转义引号,把这一段切出来再交给标准 json.loads。

原则:只在标准解析失败时兜底;只提取「第一个完整的 JSON 值」;无法配平就返回 None,
让调用方回退到原始报错。绝不猜测/补全缺失字段,避免把错的参数蒙混成对的。
"""
from __future__ import annotations

import json
from typing import Any


def _find_balanced_span(text: str, open_ch: str, close_ch: str) -> str | None:
    """从首个 open_ch 起,按深度找到匹配的 close_ch,返回该闭合子串;失败返回 None。"""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_balanced_json(raw: str) -> Any | None:
    """尽力从一段含杂质的文本里提取出第一个合法 JSON 对象/数组并解析。

    返回解析后的 Python 值;无法可靠提取/解析时返回 None(调用方回退原始报错)。
    """
    if not raw:
        return None
    # 去掉常见的 ```json ... ``` / ``` ... ``` markdown 围栏
    s = raw.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()

    # 对象优先(工具参数绝大多数是对象);找不到对象再退而求其次试数组。
    obj_start = s.find("{")
    arr_start = s.find("[")
    candidates: list[str] = []
    if obj_start != -1 and (arr_start == -1 or obj_start < arr_start):
        span = _find_balanced_span(s, "{", "}")
        if span is not None:
            candidates.append(span)
        span_arr = _find_balanced_span(s, "[", "]")
        if span_arr is not None:
            candidates.append(span_arr)
    else:
        span_arr = _find_balanced_span(s, "[", "]")
        if span_arr is not None:
            candidates.append(span_arr)
        span = _find_balanced_span(s, "{", "}")
        if span is not None:
            candidates.append(span)

    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None
