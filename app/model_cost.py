"""Shared model usage cost calculation."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

_RATE_KEYS = ("input", "output", "cacheRead", "cacheWrite")
_ALLOWED_TIER_KEYS = frozenset(("contextTokens", *_RATE_KEYS))
_PROVIDER_COST_TICKS_PER_USD = 10_000_000_000


def _validated_tiers(cost: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if "tiers" not in cost:
        return []

    tiers = cost["tiers"]
    if not isinstance(tiers, list):
        raise ValueError("cost.tiers must be an array")

    validated: list[Mapping[str, Any]] = []
    seen_thresholds: set[int] = set()
    for index, tier in enumerate(tiers):
        if not isinstance(tier, Mapping):
            raise ValueError(f"cost.tiers[{index}] must be an object")

        unknown_keys = set(tier) - _ALLOWED_TIER_KEYS
        if unknown_keys:
            rendered = ", ".join(sorted(str(key) for key in unknown_keys))
            raise ValueError(f"cost.tiers[{index}] has unsupported fields: {rendered}")

        context_tokens = tier.get("contextTokens")
        if isinstance(context_tokens, bool) or not isinstance(context_tokens, int) or context_tokens <= 0:
            raise ValueError(f"cost.tiers[{index}].contextTokens must be a positive integer")
        if context_tokens in seen_thresholds:
            raise ValueError(f"cost.tiers[{index}].contextTokens must be unique")
        seen_thresholds.add(context_tokens)
        if not any(key in tier for key in _RATE_KEYS):
            raise ValueError(f"cost.tiers[{index}] must contain at least one rate")

        for key in _RATE_KEYS:
            if key not in tier:
                continue
            rate = tier[key]
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or rate < 0
                or (isinstance(rate, float) and not math.isfinite(rate))
            ):
                raise ValueError(f"cost.tiers[{index}].{key} must be a non-negative number")
        validated.append(tier)
    return validated


def _rate(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise ValueError(f"cost.{field} must be a non-negative number")
    return float(value)


def provider_cost_usd_from_ticks(value: Any) -> float | None:
    """Convert xAI's authoritative ``cost_in_usd_ticks`` value to USD.

    Missing or malformed values return ``None`` so callers can fall back to the
    local price table.  A reported zero is valid and must not be treated as
    missing.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        ticks = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if ticks < 0 or not math.isfinite(ticks):
        return None
    return ticks / _PROVIDER_COST_TICKS_PER_USD


def resolved_usage_cost_usd(
    base_cost: Mapping[str, Any],
    usage: Any,
    *,
    fast_cost: Mapping[str, Any] | None = None,
    fast_requested: bool = False,
    actual_service_tier: Any = "",
    provider_cost_usd: Any = None,
) -> float:
    """Resolve one physical request's charge from actual response facts first.

    Priority order:
    1. Provider-reported final USD amount (for example xAI usage ticks).
    2. Explicit response tier: ``priority`` selects Fast, ``default`` selects base.
    3. If the response omits a tier, preserve the requested Fast/base behavior.

    A missing Fast table falls back to the base table rather than zero.
    """
    if not isinstance(provider_cost_usd, bool) and provider_cost_usd is not None:
        try:
            reported = float(provider_cost_usd)
        except (TypeError, ValueError, OverflowError):
            reported = -1.0
        if reported >= 0 and math.isfinite(reported):
            return reported

    tier = str(actual_service_tier or "").strip().lower()
    if tier == "default":
        selected = base_cost
    elif tier == "priority":
        selected = fast_cost if fast_cost else base_cost
    elif fast_requested and fast_cost:
        selected = fast_cost
    else:
        selected = base_cost
    return usage_cost_usd(selected, usage)


def usage_cost_usd(cost: Mapping[str, Any], usage: Any) -> float:
    """Calculate one model call's USD cost using its prompt-size tier, if any.

    Tier selection uses ``input + cache read + cache write`` tokens.  The tier
    with the highest ``contextTokens`` threshold *strictly below* that prompt
    size applies, matching models.dev's ``context_over_200k`` (``> 200K``)
    semantics. Any omitted tier rate falls back to the base cost.
    """
    if not cost:
        return 0.0

    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    cache_read_tokens = usage.cache_read_tokens
    cache_write_tokens = usage.cache_write_tokens
    prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens

    selected: Mapping[str, Any] | None = None
    selected_threshold = 0
    for tier in _validated_tiers(cost):
        threshold = tier["contextTokens"]
        if threshold < prompt_tokens and threshold >= selected_threshold:
            selected = tier
            selected_threshold = threshold

    def rate(key: str) -> float:
        if selected is not None and key in selected:
            return _rate(selected[key], f"tiers.{key}")
        return _rate(cost.get(key, 0), key)

    return (
        input_tokens / 1_000_000 * rate("input")
        + output_tokens / 1_000_000 * rate("output")
        + cache_read_tokens / 1_000_000 * rate("cacheRead")
        + cache_write_tokens / 1_000_000 * rate("cacheWrite")
    )
