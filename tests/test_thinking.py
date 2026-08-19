from __future__ import annotations

from app.models.thinking import (
    api_effort,
    available_think_levels,
    clamp_think_level,
    default_think_level,
    normalize_think_level,
    thinking_ceiling,
)


def test_normalize_think_level_aliases():
    assert normalize_think_level("off") == "off"
    assert normalize_think_level("on") == "low"
    assert normalize_think_level("think-harder") == "medium"
    assert normalize_think_level("xhigh") == "xhigh"
    assert normalize_think_level("max") == "max"
    assert normalize_think_level("wat") is None


def test_openai_style_model_name_policy():
    assert thinking_ceiling("chat", "gpt") == "xhigh"
    assert thinking_ceiling("responses", "gpt-5") == "xhigh"
    assert thinking_ceiling("chat", "deepseek") == "max"
    assert thinking_ceiling("responses", "glm-4.6") == "max"
    assert thinking_ceiling("chat", "qwen") == "high"


def test_anthropic_model_name_policy():
    assert thinking_ceiling("anthropic", "claude") == "max"
    assert thinking_ceiling("anthropic", "glm") == "max"
    assert thinking_ceiling("anthropic", "deepseek") == "max"
    assert thinking_ceiling("anthropic", "qwen") == "high"


def test_available_and_clamp():
    assert available_think_levels("chat", "gpt")[-1] == "xhigh"
    assert available_think_levels("chat", "deepseek")[-1] == "max"
    assert available_think_levels("chat", "qwen")[-1] == "high"
    assert clamp_think_level("max", "chat", "gpt") == "xhigh"
    assert clamp_think_level("xhigh", "chat", "qwen") == "high"


def test_default_and_api_effort():
    assert default_think_level(protocol="chat", model_id="deepseek", reasoning=True) == "max"
    assert default_think_level(protocol="chat", model_id="deepseek", reasoning=False) == "off"
    assert api_effort("minimal") == "low"
    assert api_effort("max") == "max"
    assert api_effort("off") is None
