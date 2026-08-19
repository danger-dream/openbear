"""Thinking level policy.

OpenBear keeps this intentionally simpler than OpenClaw: capability is derived
from the configured protocol plus the *model id* string only. No provider-specific
format flags are exposed in config.
"""
from __future__ import annotations

import re
from typing import Any, Literal

ThinkLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

_BASE_LEVELS: tuple[ThinkLevel, ...] = ("off", "minimal", "low", "medium", "high")
_RANK: dict[ThinkLevel, int] = {
    "off": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}


def normalize_think_level(raw: str | None) -> ThinkLevel | None:
    """Normalize user input to a canonical thinking level."""
    if raw is None:
        return None
    key = raw.strip().lower()
    if not key:
        return None
    collapsed = "".join(ch for ch in key if ch.isalnum())
    if collapsed in {"off", "false", "disable", "disabled", "no", "0"}:
        return "off"
    if collapsed in {"on", "enable", "enabled", "think"}:
        return "low"
    if collapsed in {"min", "minimal"}:
        return "minimal"
    if collapsed in {"low", "thinkhard"}:
        return "low"
    if collapsed in {"mid", "med", "medium", "thinkharder", "harder"}:
        return "medium"
    if collapsed in {"high", "ultra", "ultrathink", "thinkhardest", "highest"}:
        return "high"
    if collapsed in {"xhigh", "extrahigh"}:
        return "xhigh"
    if collapsed in {"max", "maximum", "maxeffort"}:
        return "max"
    return None


def normalize_think_levels(raw: Any) -> list[ThinkLevel]:
    """Normalize a configurable thinking-level list.

    Accepts either a list or a separator-delimited string. Separators are
    intentionally liberal so Web forms can accept: ``high,xhigh,max``,
    ``low;medium;high;xhigh`` or ``high，max``.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,;，；\n\r\t ]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        parts = [raw]
    out: list[ThinkLevel] = []
    seen: set[ThinkLevel] = set()
    for part in parts:
        level = normalize_think_level(str(part) if part is not None else "")
        if level and level not in seen:
            seen.add(level)
            out.append(level)
    return out


def configured_default_think_level(levels: list[str] | tuple[str, ...] | None, default: str | None = "") -> ThinkLevel:
    """Return the effective default from explicit model metadata.

    No configured list means the model exposes no selectable thinking strength.
    When a list exists and default is omitted/invalid, the last configured level
    is used, matching the Web form behavior 老大要求的“最后一档默认”。
    """
    normalized = normalize_think_levels(list(levels or []))
    if not normalized:
        return "off"
    wanted = normalize_think_level(default or "")
    return wanted if wanted in normalized else normalized[-1]



def thinking_ceiling(protocol: str, model_id: str) -> ThinkLevel:
    """Resolve the highest supported level from protocol + model id only.

    Policy requested by 老大:
    - OpenAI-style protocols: gpt => xhigh; glm/deepseek => max; otherwise high.
    - Anthropic protocol: claude/glm/deepseek => max; otherwise high.
    """
    proto = (protocol or "").strip().lower()
    model = (model_id or "").strip().lower()
    if proto == "anthropic":
        if any(token in model for token in ("claude", "glm", "deepseek")):
            return "max"
        return "high"
    if any(token in model for token in ("glm", "deepseek")):
        return "max"
    if "gpt" in model:
        return "xhigh"
    return "high"


def available_think_levels(protocol: str, model_id: str) -> list[ThinkLevel]:
    ceiling = thinking_ceiling(protocol, model_id)
    levels = list(_BASE_LEVELS)
    if _RANK[ceiling] >= _RANK["xhigh"]:
        levels.append("xhigh")
    if _RANK[ceiling] >= _RANK["max"]:
        levels.append("max")
    return levels


def clamp_think_level(level: ThinkLevel, protocol: str, model_id: str) -> ThinkLevel:
    if level == "off":
        return level
    ceiling = thinking_ceiling(protocol, model_id)
    return ceiling if _RANK[level] > _RANK[ceiling] else level


def default_think_level(*, protocol: str, model_id: str, reasoning: bool) -> ThinkLevel:
    """Default used when the session has no explicit think-level preference."""
    if not reasoning:
        return "off"
    return thinking_ceiling(protocol, model_id)


def api_effort(level: ThinkLevel) -> str | None:
    """Map OpenBear level to upstream effort value.

    `minimal` is normalized to `low` because Anthropic/OpenAI effort fields do
    not consistently accept a separate minimal value.
    """
    if level == "off":
        return None
    if level == "minimal":
        return "low"
    return level
