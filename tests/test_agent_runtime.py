from __future__ import annotations

from types import SimpleNamespace

from app.models.agent_runtime import (
    agent_run_config_public,
    agent_runtime_snapshot_fields,
    resolve_agent_runtime_config,
)
from app.rath.schemas import RathAgentDef


class _Model:
    def __init__(self, *, thinking_levels=None, default_thinking_level="", supports_fast=False, cost=None,
                 fast_cost=None, fast_request=None):
        self.thinking_levels = list(thinking_levels or [])
        self.default_thinking_level = default_thinking_level
        self.supports_fast = supports_fast
        self.cost = cost or {"input": 1.0, "output": 2.0, "cacheRead": 0.1, "cacheWrite": 0.2}
        self.fast_cost = fast_cost or {}
        self.fast_request = fast_request


class _Provider:
    def __init__(self, protocol="responses"):
        self.protocol = protocol


class _Models:
    primary = "openai/main"

    def __init__(self):
        self._items = {
            "openai/main": (_Provider("responses"), _Model(thinking_levels=["low", "high"], default_thinking_level="high", supports_fast=True)),
            "openai/cheap": (_Provider("chat"), _Model(thinking_levels=["low", "medium"], default_thinking_level="low", supports_fast=False)),
            "openai/fastonly": (_Provider("responses"), _Model(thinking_levels=[], supports_fast=True)),
        }

    def resolve(self, name: str):
        return self._items.get(str(name or ""))


class _Config:
    def __init__(self):
        self.models = _Models()


def _agent(**kwargs):
    base = dict(
        id=1,
        agent_key="coder",
        name="coder",
        description="",
        system_prompt="x",
        model="",
        think_level="",
        tool_allowlist=["Read"],
        enabled=True,
    )
    base.update(kwargs)
    return RathAgentDef(**base)


def test_resolve_falls_back_to_main_model_and_default_think():
    resolved = resolve_agent_runtime_config(
        _agent(),
        config=_Config(),
        model_selection_current="openai/main",
        conversation={"model": "openai/main", "agent_model": "", "agent_think_level": "", "agent_fast_mode": -1},
        main_model="openai/main",
        main_fast_requested=False,
    )
    assert resolved["model"] == "openai/main"
    assert resolved["thinkLevel"] == "high"
    assert resolved["fastMode"] is False
    assert resolved["serviceTier"] == ""
    assert resolved["source"]["model"] == "main"
    assert resolved["source"]["thinkLevel"] == "model_default"
    assert resolved["source"]["fastMode"] == "main"


def test_resolve_prefers_conversation_defaults_over_main():
    resolved = resolve_agent_runtime_config(
        _agent(),
        config=_Config(),
        model_selection_current="openai/main",
        conversation={
            "model": "openai/main",
            "agent_model": "openai/cheap",
            "agent_think_level": "medium",
            "agent_fast_mode": 0,
        },
        main_model="openai/main",
        main_fast_requested=True,
    )
    assert resolved["model"] == "openai/cheap"
    assert resolved["thinkLevel"] == "medium"
    assert resolved["fastMode"] is False
    assert resolved["source"]["model"] == "conversation"
    assert resolved["source"]["thinkLevel"] == "conversation"
    assert resolved["source"]["fastMode"] == "conversation"


def test_resolve_prefers_preset_over_conversation():
    resolved = resolve_agent_runtime_config(
        _agent(model="openai/main", think_level="low"),
        config=_Config(),
        conversation={
            "model": "openai/main",
            "agent_model": "openai/cheap",
            "agent_think_level": "medium",
            "agent_fast_mode": 1,
        },
        main_model="openai/cheap",
        main_fast_requested=False,
    )
    assert resolved["model"] == "openai/main"
    assert resolved["thinkLevel"] == "low"
    assert resolved["source"]["model"] == "preset"
    assert resolved["source"]["thinkLevel"] == "preset"
    # conversation still controls fast when not frozen
    assert resolved["fastMode"] is True
    assert resolved["serviceTier"] == "priority"
    assert resolved["source"]["fastMode"] == "conversation"


def test_resolve_follow_main_fast_when_agent_fast_unset():
    resolved = resolve_agent_runtime_config(
        _agent(),
        config=_Config(),
        conversation={"model": "openai/main", "agent_model": "", "agent_think_level": "", "agent_fast_mode": -1},
        main_model="openai/main",
        main_fast_requested=True,
    )
    assert resolved["fastMode"] is True
    assert resolved["serviceTier"] == "priority"
    assert resolved["source"]["fastMode"] == "main"


def test_resolve_uses_confirmed_fast_request_and_effective_fast_cost():
    config = _Config()
    config.models._items["openai/main"] = (
        _Provider("responses"),
        _Model(
            thinking_levels=["low", "high"],
            default_thinking_level="high",
            supports_fast=True,
            cost={"input": 1, "output": 2, "tiers": [{"contextTokens": 200_000, "input": 4, "output": 8}]},
            fast_cost={"input": 3, "output": 6, "tiers": [{"contextTokens": 200_000, "input": 12, "output": 8}]},
            fast_request={
                "body": {"service_tier": "priority"},
                "headers": {"x-fast-mode": "enabled"},
            },
        ),
    )
    resolved = resolve_agent_runtime_config(
        _agent(),
        config=config,
        model_selection_current="openai/main",
        conversation={"model": "openai/main", "agent_model": "", "agent_think_level": "", "agent_fast_mode": -1},
        main_model="openai/main",
        main_fast_requested=True,
    )
    assert resolved["fastMode"] is True
    assert resolved["serviceTier"] == ""
    assert resolved["fastRequest"] == {
        "body": {"service_tier": "priority"},
        "headers": {"x-fast-mode": "enabled"},
    }
    assert resolved["cost"]["input"] == 3
    assert resolved["cost"]["tiers"][0]["input"] == 12
    assert resolved["baseCost"]["input"] == 1
    assert resolved["baseCost"]["tiers"][0]["input"] == 4
    assert resolved["fastCost"] == resolved["cost"]
    snapshot = agent_runtime_snapshot_fields(resolved)
    assert snapshot["fastRequest"] == resolved["fastRequest"]
    assert snapshot["fastRequestExplicit"] is True


def test_legacy_fast_snapshot_resume_preserves_service_tier():
    initial = resolve_agent_runtime_config(
        _agent(),
        config=_Config(),
        model_selection_current="openai/main",
        conversation={"model": "openai/main", "agent_model": "", "agent_think_level": "", "agent_fast_mode": -1},
        main_model="openai/main",
        main_fast_requested=True,
    )
    assert initial["fastMode"] is True
    assert initial["serviceTier"] == "priority"
    assert initial["fastRequestExplicit"] is False

    snapshot = agent_runtime_snapshot_fields(initial)
    assert "fastRequest" not in snapshot
    resumed = resolve_agent_runtime_config(
        _agent(),
        config=_Config(),
        main_model="openai/main",
        main_fast_requested=False,
        frozen=snapshot,
    )
    assert resumed["fastMode"] is True
    assert resumed["serviceTier"] == "priority"
    assert resumed["fastRequest"] == {"body": {}, "headers": {}}

    # Existing snapshots written by the broken implementation always contain an
    # empty fastRequest.  Without an explicit marker it must remain legacy Fast.
    old_snapshot = {**snapshot, "fastRequest": {"body": {}, "headers": {}}}
    resumed_old = resolve_agent_runtime_config(
        _agent(),
        config=_Config(),
        main_model="openai/main",
        frozen=old_snapshot,
    )
    assert resumed_old["fastMode"] is True
    assert resumed_old["serviceTier"] == "priority"


def test_explicit_empty_fast_request_remains_explicit_across_resume():
    config = _Config()
    config.models._items["openai/main"] = (
        _Provider("responses"),
        _Model(supports_fast=True, fast_request={"body": {}, "headers": {}}),
    )
    initial = resolve_agent_runtime_config(
        _agent(),
        config=config,
        model_selection_current="openai/main",
        main_model="openai/main",
        main_fast_requested=True,
    )
    snapshot = agent_runtime_snapshot_fields(initial)
    assert snapshot["fastRequestExplicit"] is True
    assert snapshot["fastRequest"] == {"body": {}, "headers": {}}

    resumed = resolve_agent_runtime_config(
        _agent(),
        config=config,
        main_model="openai/main",
        frozen=snapshot,
    )
    assert resumed["fastMode"] is True
    assert resumed["serviceTier"] == ""
    assert resumed["fastRequestExplicit"] is True


def test_resolve_frozen_snapshot_ignores_later_conversation_changes():
    frozen = {
        "model": "openai/cheap",
        "thinkLevel": "medium",
        "fastMode": False,
        "serviceTier": "",
        "modelSource": "conversation",
        "thinkSource": "conversation",
        "fastSource": "conversation",
    }
    resolved = resolve_agent_runtime_config(
        _agent(model="openai/main", think_level="high"),
        config=_Config(),
        conversation={
            "model": "openai/main",
            "agent_model": "openai/main",
            "agent_think_level": "high",
            "agent_fast_mode": 1,
        },
        main_model="openai/main",
        main_fast_requested=True,
        frozen=frozen,
    )
    assert resolved["model"] == "openai/cheap"
    assert resolved["thinkLevel"] == "medium"
    assert resolved["fastMode"] is False
    assert resolved["source"]["model"] == "conversation"
    assert resolved["source"]["thinkLevel"] == "conversation"
    assert resolved["source"]["fastMode"] == "conversation"


def test_snapshot_and_public_payload_shape():
    resolved = resolve_agent_runtime_config(
        _agent(),
        config=_Config(),
        conversation={
            "model": "openai/main",
            "agent_model": "openai/cheap",
            "agent_think_level": "low",
            "agent_fast_mode": 1,
        },
        main_model="openai/main",
        main_fast_requested=False,
    )
    snap = agent_runtime_snapshot_fields(resolved)
    assert snap["model"] == "openai/cheap"
    assert snap["thinkLevel"] == "low"
    assert snap["fastMode"] is False  # cheap does not support fast
    assert snap["resolvedAtStart"] is True
    public = agent_run_config_public(resolved)
    assert public["model"] == "openai/cheap"
    assert public["thinkLevel"] == "low"
    assert public["fastMode"] is True
    assert public["effective"]["model"] == "openai/cheap"
    assert public["effective"]["source"]["model"] == "conversation"
