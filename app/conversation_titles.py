"""Conversation-title normalization and bounded transcript selection."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

DEFAULT_NAMING_PROMPT = (
    "Generate a concise, single-line task title of at most 16 characters and under five words where possible. "
    "Start with an imperative verb. "
    "Capitalize only the first word unless the user's language, proper nouns, acronyms, or code terms require otherwise. "
    "Preserve ticket references exactly. Write in the user's language. "
    "Do not use quotes, markdown, or trailing punctuation. Do not answer the request."
)
TITLE_MAX_DISPLAY_UNITS = 12  # Initial first-message placeholder only.
GENERATED_TITLE_MAX_DISPLAY_UNITS = 24
_ASSISTANT_TRUNCATION_MARKER = "…[已截断]…"


def _is_grapheme_extend(character: str) -> bool:
    codepoint = ord(character)
    return bool(
        unicodedata.combining(character)
        or character in {"\ufe0e", "\ufe0f", "\u20e3"}
        or 0x1F3FB <= codepoint <= 0x1F3FF
    )


def iter_graphemes(value: str):
    """Yield practical Unicode graphemes without splitting emoji/combining text."""
    text = str(value or "")
    index = 0
    while index < len(text):
        start = index
        first = ord(text[index])
        index += 1
        if 0x1F1E6 <= first <= 0x1F1FF and index < len(text) and 0x1F1E6 <= ord(text[index]) <= 0x1F1FF:
            index += 1
        while index < len(text) and _is_grapheme_extend(text[index]):
            index += 1
        while index < len(text) and text[index] == "\u200d" and index + 1 < len(text):
            index += 2
            while index < len(text) and _is_grapheme_extend(text[index]):
                index += 1
        yield text[start:index]


def _grapheme_half_units(grapheme: str) -> int:
    """Return display width in half-CJK units (ASCII base=1, other base=2)."""
    if not grapheme:
        return 0
    base = next((character for character in grapheme if character != "\u200d" and not _is_grapheme_extend(character)), grapheme[0])
    return 1 if ord(base) <= 0x7F else 2


def display_half_units(value: str) -> int:
    return sum(_grapheme_half_units(grapheme) for grapheme in iter_graphemes(value))


def truncate_display_title(value: str, max_units: int = TITLE_MAX_DISPLAY_UNITS) -> str:
    limit = max(0, int(max_units)) * 2
    output: list[str] = []
    used = 0
    for grapheme in iter_graphemes(value):
        width = _grapheme_half_units(grapheme)
        if width and used + width > limit:
            break
        output.append(grapheme)
        used += width
    return "".join(output).rstrip()


def normalize_title_source(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def initial_conversation_title(value: str) -> str:
    return truncate_display_title(normalize_title_source(value)) or "新会话"


def clean_generated_title(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    text = re.sub(r"^(?:会话)?标题\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip(" \t\r\n\"'“”‘’`")
    text = normalize_title_source(text)
    return truncate_display_title(text, GENERATED_TITLE_MAX_DISPLAY_UNITS)


def _truncate_head_tail(text: str, limit: int) -> str:
    value = str(text or "")
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    marker = _ASSISTANT_TRUNCATION_MARKER
    if limit <= len(marker):
        return value[:limit]
    body = limit - len(marker)
    head = max(1, int(body * 0.7))
    tail = max(0, body - head)
    return value[:head] + marker + (value[-tail:] if tail else "")


def _fair_assistant_limits(lengths: list[int], budget: int) -> list[int]:
    """Water-fill a shared assistant budget without starving short replies."""
    limits = [0] * len(lengths)
    remaining = max(0, int(budget))
    active = {index for index, length in enumerate(lengths) if length > 0}
    while active and remaining > 0:
        share, extra = divmod(remaining, len(active))
        if share <= 0:
            for index in sorted(active)[:extra]:
                limits[index] += 1
            break
        completed = []
        spent = 0
        for index in sorted(active):
            need = lengths[index] - limits[index]
            grant = min(need, share + (1 if extra > 0 else 0))
            if extra > 0:
                extra -= 1
            limits[index] += grant
            spent += grant
            if limits[index] >= lengths[index]:
                completed.append(index)
        remaining -= spent
        for index in completed:
            active.discard(index)
        if not spent:
            break
    return limits


def select_naming_turns(
    turns: list[dict[str, Any]], *, max_turns: int = 8, max_chars: int = 50_000
) -> list[dict[str, str]]:
    """Select recent completed turns and trim assistant text before user text.

    The character ceiling is intentionally soft when user text alone exceeds it:
    user messages are never truncated, while assistant text receives the remaining
    shared budget.
    """
    normalized: list[dict[str, str]] = []
    for item in turns or []:
        user = str(item.get("user") or "")
        assistant = str(item.get("assistant") or "")
        if user and assistant:
            normalized.append({"user": user, "assistant": assistant})
    count = max(0, int(max_turns or 0))
    if count:
        normalized = normalized[-count:]
    if not normalized:
        return []
    ceiling = max(0, int(max_chars or 0))
    user_chars = sum(len(item["user"]) for item in normalized)
    assistant_budget = max(0, ceiling - user_chars)
    assistant_lengths = [len(item["assistant"]) for item in normalized]
    limits = _fair_assistant_limits(assistant_lengths, assistant_budget)
    return [
        {
            "user": item["user"],
            "assistant": _truncate_head_tail(item["assistant"], limit),
        }
        for item, limit in zip(normalized, limits, strict=True)
    ]


def naming_attempt_models(candidates: list[str], max_retries: int) -> list[str]:
    """Return first call plus configured retries, rotating candidates in order."""
    usable = [str(candidate or "").strip() for candidate in candidates if str(candidate or "").strip()]
    if not usable:
        return []
    return [usable[index % len(usable)] for index in range(1 + max(0, int(max_retries or 0)))]


def render_naming_transcript(turns: list[dict[str, str]]) -> str:
    blocks: list[str] = []
    for index, item in enumerate(turns, start=1):
        parts = [f"第 {index} 轮", f"用户：{item['user']}"]
        if item.get("assistant"):
            parts.append(f"助手：{item['assistant']}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)
