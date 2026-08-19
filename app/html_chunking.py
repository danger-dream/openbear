"""HTML 文本安全分块工具。

用于把较长的 HTML 片段拆成长度受限的多个块，同时保证跨块时标签自动闭合并在下一块重新打开，避免代码块、引用、粗体等标签被截断后破坏渲染。
"""
from __future__ import annotations

import re

_TAG_RE = re.compile(r"(</?)([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*?>", re.DOTALL)
_SELF_CLOSING = {"br"}


def _open_prefix(open_tags: list[tuple[str, str]]) -> str:
    return "".join(raw for (_name, raw) in open_tags)


def _close_suffix(open_tags: list[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for (name, _raw) in reversed(open_tags))


def _close_suffix_len(open_tags: list[tuple[str, str]]) -> int:
    return sum(len(f"</{name}>") for (name, _raw) in open_tags)


def _find_safe_split(text: str, max_len: int) -> int:
    """在 max_len 以内找安全切点，避免切断 HTML entity。"""
    if len(text) <= max_len:
        return len(text)
    limit = max(1, max_len)
    amp = text.rfind("&", 0, limit)
    if amp == -1:
        return limit
    semi = text.rfind(";", 0, limit)
    if amp < semi:
        return limit
    end = text.find(";", amp)
    if end == -1 or end >= limit:
        return amp
    return limit


def _pop_tag(open_tags: list[tuple[str, str]], name: str) -> None:
    for i in range(len(open_tags) - 1, -1, -1):
        if open_tags[i][0] == name:
            open_tags.pop(i)
            return


def split_html_chunks(html: str, limit: int = 4000) -> list[str]:
    """把 HTML 文本分成每块 ≤ limit 的若干块，跨块标签自动闭合+重开。"""
    if not html:
        return []
    limit = max(1, int(limit))
    if len(html) <= limit:
        return [html]

    chunks: list[str] = []
    open_tags: list[tuple[str, str]] = []
    current = ""
    has_payload = False

    def reset_current() -> None:
        nonlocal current, has_payload
        current = _open_prefix(open_tags)
        has_payload = False

    def flush() -> None:
        nonlocal current, has_payload
        if not has_payload:
            return
        chunks.append(current + _close_suffix(open_tags))
        reset_current()

    def append_text(segment: str) -> None:
        nonlocal current, has_payload
        remaining = segment
        while remaining:
            avail = limit - len(current) - _close_suffix_len(open_tags)
            if avail <= 0:
                if not has_payload:
                    current += remaining[:1]
                    remaining = remaining[1:]
                    has_payload = True
                    continue
                flush()
                continue
            if len(remaining) <= avail:
                current += remaining
                has_payload = True
                break
            cut = _find_safe_split(remaining, avail)
            if cut <= 0:
                if not has_payload:
                    cut = 1
                else:
                    flush()
                    continue
            current += remaining[:cut]
            has_payload = True
            remaining = remaining[cut:]
            flush()

    reset_current()
    last = 0
    for m in _TAG_RE.finditer(html):
        append_text(html[last:m.start()])
        raw = m.group(0)
        is_closing = m.group(1) == "</"
        name = m.group(2).lower()
        is_self = (not is_closing) and (name in _SELF_CLOSING or raw.rstrip().endswith("/>"))

        next_close = 0 if (is_closing or is_self) else len(f"</{name}>")
        if not is_closing and has_payload and (len(current) + len(raw) + _close_suffix_len(open_tags) + next_close) > limit:
            flush()
        current += raw
        if is_self:
            has_payload = True
        elif is_closing:
            _pop_tag(open_tags, name)
        else:
            open_tags.append((name, raw))
        last = m.end()

    append_text(html[last:])
    flush()
    return chunks if chunks else [html]
