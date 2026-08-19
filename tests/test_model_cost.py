from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from app.llm.events import Usage
from app.model_cost import (
    provider_cost_usd_from_ticks,
    resolved_usage_cost_usd,
    usage_cost_usd,
)

_ROOT = Path(__file__).resolve().parents[1]


def _imports_shared_calculator(relative_path: str, name: str, alias_name: str) -> bool:
    module = ast.parse((_ROOT / relative_path).read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.model_cost"
        and any(alias.name == name and alias.asname == alias_name for alias in node.names)
        for node in ast.walk(module)
    )


def test_legacy_cost_and_private_callers_remain_compatible() -> None:
    # Keep this unit test independent of the optional async database package that
    # importing the full Rath/Web runtime requires.  Syntax compilation covers
    # import execution; this asserts both callers retain the single calculator.
    assert _imports_shared_calculator(
        "app/rath/single_agent.py", "resolved_usage_cost_usd", "_resolved_usage_cost_usd",
    )
    assert _imports_shared_calculator(
        "app/web_console/core.py", "usage_cost_usd", "_usage_cost_usd",
    )

    cost = {"input": 2, "output": 10, "cacheRead": 0.5, "cacheWrite": 3}
    usage = Usage(
        input_tokens=100_000,
        output_tokens=20_000,
        cache_read_tokens=50_000,
        cache_write_tokens=10_000,
    )

    assert usage_cost_usd(cost, usage) == pytest.approx(0.455)


def test_provider_ticks_and_actual_service_tier_choose_the_final_charge() -> None:
    usage = Usage(input_tokens=1_000_000)
    base = {"input": 2, "output": 6}
    fast = {"input": 4, "output": 12}

    assert provider_cost_usd_from_ticks(3_384_000) == pytest.approx(0.0003384)
    assert provider_cost_usd_from_ticks("3624000") == pytest.approx(0.0003624)
    assert provider_cost_usd_from_ticks(0) == 0.0
    assert provider_cost_usd_from_ticks(-1) is None
    assert provider_cost_usd_from_ticks(math.inf) is None
    assert provider_cost_usd_from_ticks(True) is None

    # An upstream-reported amount is authoritative, including an explicit zero.
    assert resolved_usage_cost_usd(
        base, usage, fast_cost=fast, fast_requested=True,
        actual_service_tier="priority", provider_cost_usd=0.125,
    ) == pytest.approx(0.125)
    assert resolved_usage_cost_usd(
        base, usage, fast_cost=fast, fast_requested=True,
        actual_service_tier="priority", provider_cost_usd=0,
    ) == 0.0
    # Explicit response tiers override what was requested.
    assert resolved_usage_cost_usd(
        base, usage, fast_cost=fast, fast_requested=True, actual_service_tier="default",
    ) == pytest.approx(2.0)
    assert resolved_usage_cost_usd(
        base, usage, fast_cost=fast, fast_requested=False, actual_service_tier="priority",
    ) == pytest.approx(4.0)
    # Missing response metadata preserves the existing request-mode fallback.
    assert resolved_usage_cost_usd(
        base, usage, fast_cost=fast, fast_requested=True,
    ) == pytest.approx(4.0)
    assert resolved_usage_cost_usd(
        base, usage, fast_cost={}, fast_requested=True, actual_service_tier="priority",
    ) == pytest.approx(2.0)


def test_tier_applies_only_above_context_threshold() -> None:
    # models.dev's legacy name is context_over_200k: 200,000 itself stays in
    # the base band; the higher price starts strictly above the boundary.
    cost = {
        "input": 1,
        "output": 2,
        "tiers": [{"contextTokens": 200_000, "input": 4, "output": 8}],
    }

    below = Usage(input_tokens=199_999, output_tokens=1_000_000)
    at_threshold = Usage(input_tokens=200_000, output_tokens=1_000_000)
    above_threshold = Usage(input_tokens=200_001, output_tokens=1_000_000)

    assert usage_cost_usd(cost, below) == pytest.approx(2.199999)
    assert usage_cost_usd(cost, at_threshold) == pytest.approx(2.2)
    assert usage_cost_usd(cost, above_threshold) == pytest.approx(8.800004)


def test_prompt_size_includes_cache_read_and_cache_write_tokens() -> None:
    cost = {
        "input": 1,
        "cacheRead": 2,
        "cacheWrite": 3,
        "tiers": [
            {
                "contextTokens": 200_000,
                "input": 10,
                "cacheRead": 20,
                "cacheWrite": 30,
            }
        ],
    }
    usage = Usage(
        input_tokens=100_001,
        cache_read_tokens=60_000,
        cache_write_tokens=40_000,
    )

    # The cache components participate in the inclusive prompt size. The total
    # is 200,001, so the >200K tier applies.
    assert usage_cost_usd(cost, usage) == pytest.approx(3.40001)


def test_normalized_cache_creation_is_counted_once_for_tier_selection() -> None:
    cost = {
        "input": 1,
        "cacheWrite": 1,
        "tiers": [{"contextTokens": 200_000, "input": 4, "cacheWrite": 4}],
    }
    # The upstream inclusive prompt total was 195K, including 10K cache
    # creation. Usage stores its non-overlapping representation, so this must
    # remain in the base band rather than becoming an erroneous 205K request.
    usage = Usage(input_tokens=185_000, cache_write_tokens=10_000)

    assert usage_cost_usd(cost, usage) == pytest.approx(0.195)


def test_highest_eligible_unsorted_tier_wins_and_missing_rates_fall_back() -> None:
    cost = {
        "input": 1,
        "output": 2,
        "cacheRead": 3,
        "cacheWrite": 4,
        "tiers": [
            {"contextTokens": 300_000, "input": 30},
            {"contextTokens": 100_000, "output": 20},
            {"contextTokens": 200_000, "input": 10, "cacheRead": 30},
        ],
    }
    usage = Usage(
        input_tokens=100_000,
        output_tokens=10_000,
        cache_read_tokens=100_000,
        cache_write_tokens=50_000,
    )

    assert usage_cost_usd(cost, usage) == pytest.approx(4.22)


def test_arbitrary_non_200k_thresholds_and_multiple_stages_apply() -> None:
    # models.dev currently contains context tiers such as 16K/32K, 256K and
    # 512K.  The calculator must use each supplied boundary, not treat 200K as
    # a special case.
    cost = {
        "input": 1,
        "tiers": [
            {"contextTokens": 16_000, "input": 2},
            {"contextTokens": 32_000, "input": 3},
            {"contextTokens": 256_000, "input": 4},
            {"contextTokens": 512_000, "input": 5},
        ],
    }

    assert usage_cost_usd(cost, Usage(input_tokens=16_000)) == pytest.approx(0.016)
    assert usage_cost_usd(cost, Usage(input_tokens=16_001)) == pytest.approx(0.032002)
    assert usage_cost_usd(cost, Usage(input_tokens=32_001)) == pytest.approx(0.096003)
    assert usage_cost_usd(cost, Usage(input_tokens=256_001)) == pytest.approx(1.024004)
    assert usage_cost_usd(cost, Usage(input_tokens=512_001)) == pytest.approx(2.560005)


@pytest.mark.parametrize(
    "tiers",
    [
        None,
        {},
        [None],
        [{}],
        [{"contextTokens": 0}],
        [{"contextTokens": True}],
        [{"contextTokens": 1.5}],
        [{"contextTokens": 1, "input": -0.1}],
        [{"contextTokens": 1, "output": True}],
        [{"contextTokens": 1, "cacheRead": math.inf}],
        [{"contextTokens": 1, "cacheWrite": "1"}],
        [{"contextTokens": 1, "extra": 0}],
        [{"contextTokens": 1}],
    ],
)
def test_invalid_tier_shapes_are_rejected(tiers: object) -> None:
    with pytest.raises(ValueError):
        usage_cost_usd({"tiers": tiers}, Usage())


def test_duplicate_tier_thresholds_and_invalid_base_rates_are_rejected() -> None:
    with pytest.raises(ValueError):
        usage_cost_usd(
            {"tiers": [{"contextTokens": 1, "input": 1}, {"contextTokens": 1, "input": 2}]},
            Usage(input_tokens=1),
        )
    with pytest.raises(ValueError):
        usage_cost_usd({"input": "1"}, Usage(input_tokens=1))
