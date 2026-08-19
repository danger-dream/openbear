"""工具结果智能截断 —— head+tail 策略（移植 OpenClaw tool-result-truncation 思路）。

★ 在工具结果「产出当下」就地截断一次，存进 convo 后永不再改。
  不对历史 messages 做 guard —— 避免改写历史破坏模型 prompt cache、降低命中率、浪费 token。

策略：
- 尾部含重要内容（error/traceback/summary/JSON 闭合）→ 保留头+尾，中间标省略
- 否则 → 保留头部
- 在换行边界切，不硬切
"""
from __future__ import annotations

import re

_MIN_KEEP_CHARS = 2_000
_MIDDLE_MARKER = "\n\n⚠️ [... 中间内容已省略 — 仅显示头部与尾部 ...]\n\n"

# 尾部重要内容特征（保留尾部，不丢错误/结论）
_IMPORTANT_TAIL_RE = re.compile(
    r"\b(error|exception|failed|fatal|traceback|panic|stack trace|errno|exit code|"
    r"total|summary|result|complete|finished|done|错误|异常|失败|完成|总计)\b",
    re.IGNORECASE,
)


def _has_important_tail(text: str) -> bool:
    tail = text[-2000:]
    if _IMPORTANT_TAIL_RE.search(tail):
        return True
    # JSON 闭合：尾部是 } 或 ] 结尾，说明是结构化输出
    stripped = tail.rstrip()
    return stripped.endswith("}") or stripped.endswith("]")


def truncate_tool_result(text: str, max_chars: int) -> str:
    """把工具结果截断到 max_chars 以内。

    超限时优先 head+tail（尾部重要时），否则保留头部。换行边界切。
    """
    if len(text) <= max_chars:
        return text

    omitted = len(text) - max_chars

    def _suffix(n: int) -> str:
        return f"\n\n…[共 {len(text)} 字符，已截断约 {n} 字符]"

    budget = max(_MIN_KEEP_CHARS, max_chars - len(_suffix(omitted)))

    # 尾部重要 → head + middle marker + tail
    if _has_important_tail(text) and budget > _MIN_KEEP_CHARS * 2:
        tail_budget = min(int(budget * 0.3), 4_000)
        head_budget = budget - tail_budget - len(_MIDDLE_MARKER)
        if head_budget > _MIN_KEEP_CHARS:
            # 头部在换行边界切
            head_cut = head_budget
            nl = text.rfind("\n", 0, head_budget)
            if nl > head_budget * 0.8:
                head_cut = nl
            # 尾部在换行边界切
            tail_start = len(text) - tail_budget
            nl2 = text.find("\n", tail_start)
            if nl2 != -1 and nl2 < tail_start + tail_budget * 0.2:
                tail_start = nl2 + 1
            kept = text[:head_cut] + _MIDDLE_MARKER + text[tail_start:]
            return _bounded(kept, len(text), max_chars)

    # 默认：保留头部
    cut = budget
    nl = text.rfind("\n", 0, budget)
    if nl > budget * 0.8:
        cut = nl
    return _bounded(text[:cut], len(text), max_chars)


def _bounded(kept: str, original_len: int, max_chars: int) -> str:
    """拼上截断说明后再确保不超 max_chars。"""
    while True:
        suffix = f"\n\n…[共 {original_len} 字符，已截断约 {max(1, original_len - len(kept))} 字符]"
        final = kept + suffix
        if len(final) <= max_chars or not kept:
            return final if len(final) <= max_chars else final[:max_chars]
        overflow = len(final) - max_chars
        kept = kept[: max(0, len(kept) - overflow)]
