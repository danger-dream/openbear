"""Agent runtime model / thinking / fast resolution.

Priority (new task):
1. frozen task snapshot (continue / resume)
2. Agent preset explicit model/think
3. conversation Agent defaults
4. main conversation model / model default thinking / main fast
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import Config, fast_request_mode
from app.models.thinking import (
    configured_default_think_level,
    normalize_think_level,
    normalize_think_levels,
)
from app.rath.schemas import RathAgentDef


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _fast_request_payload(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize a stored Fast request config for one immutable run snapshot."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        return {"body": {}, "headers": {}}
    body = value.get("body")
    headers = value.get("headers")
    return {
        "body": dict(body) if isinstance(body, Mapping) else {},
        "headers": {
            str(name): header_value
            for name, header_value in headers.items()
            if isinstance(name, str) and isinstance(header_value, str)
        } if isinstance(headers, Mapping) else {},
    }


def _fast_request_has_values(value: Mapping[str, Any]) -> bool:
    return bool(value.get("body") or value.get("headers"))


def conversation_agent_defaults(conversation: dict[str, Any] | None) -> dict[str, Any]:
    row = conversation if isinstance(conversation, dict) else {}
    return {
        "model": str(row.get("agent_model") or "").strip(),
        "thinkLevel": str(row.get("agent_think_level") or "").strip(),
        "fastMode": _int_or(row.get("agent_fast_mode"), -1),
    }


def resolve_agent_runtime_config(
    agent: RathAgentDef | None = None,
    *,
    config: Config,
    model_selection_current: str = "",
    conversation: dict[str, Any] | None = None,
    main_model: str = "",
    main_fast_requested: bool = False,
    frozen: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve effective Agent model/think/fast for launch or continue."""
    agent = agent or RathAgentDef(
        id=0,
        agent_key="general-purpose",
        name="general-purpose",
        description="",
        system_prompt="",
        model="",
        think_level="",
        tool_allowlist=[],
        enabled=True,
    )
    conv = conversation if isinstance(conversation, dict) else {}
    defaults = conversation_agent_defaults(conv)
    primary = str(getattr(config.models, "primary", "") or "")
    main_model_label = (
        str(main_model or "").strip()
        or str(conv.get("model") or "").strip()
        or str(model_selection_current or "").strip()
        or primary
    )

    frozen_row = frozen if isinstance(frozen, dict) else {}
    frozen_model = str(frozen_row.get("model") or "").strip()
    if frozen_model:
        model_name = frozen_model
        model_source = str(frozen_row.get("modelSource") or frozen_row.get("source", {}).get("model") or "snapshot")
        think_requested = normalize_think_level(str(frozen_row.get("thinkLevel") or ""))
        think_source = str(frozen_row.get("thinkSource") or frozen_row.get("source", {}).get("thinkLevel") or "snapshot")
        if "fastMode" in frozen_row:
            fast_requested = bool(frozen_row.get("fastMode"))
            fast_source = str(frozen_row.get("fastSource") or frozen_row.get("source", {}).get("fastMode") or "snapshot")
        else:
            fast_requested = bool(main_fast_requested)
            fast_source = "main"
        service_tier_hint = str(frozen_row.get("serviceTier") or "")
        frozen_fast_request = _fast_request_payload(frozen_row.get("fastRequest"))
        explicit_marker = frozen_row.get("fastRequestExplicit")
        if isinstance(explicit_marker, bool):
            frozen_fast_request_present = explicit_marker
        else:
            # Compatibility with snapshots written before the explicit marker:
            # non-empty request additions are explicit, while the old always-
            # serialized empty object belongs to legacy serviceTier Fast mode.
            frozen_fast_request_present = _fast_request_has_values(frozen_fast_request)
    else:
        preset_model = str(agent.model or "").strip()
        conv_model = str(defaults.get("model") or "").strip()
        if preset_model:
            model_name, model_source = preset_model, "preset"
        elif conv_model:
            model_name, model_source = conv_model, "conversation"
        else:
            model_name, model_source = main_model_label, "main"

        think_requested = None
        think_source = "model_default"
        preset_think = normalize_think_level(str(agent.think_level or ""))
        conv_think = normalize_think_level(str(defaults.get("thinkLevel") or ""))
        if preset_think:
            think_requested = preset_think
            think_source = "preset"
        elif conv_think:
            think_requested = conv_think
            think_source = "conversation"

        agent_fast = _int_or(defaults.get("fastMode"), -1)
        if agent_fast == 1:
            fast_requested, fast_source = True, "conversation"
        elif agent_fast == 0:
            fast_requested, fast_source = False, "conversation"
        else:
            fast_requested, fast_source = bool(main_fast_requested), "main"
        service_tier_hint = ""
        frozen_fast_request_present = False
        frozen_fast_request = {}

    model_meta = config.models.resolve(model_name)
    cost: dict[str, Any] = {}
    base_cost: dict[str, Any] = {}
    fast_cost: dict[str, Any] = {}
    levels: list[str] = []
    think_level = "off"
    service_tier = ""
    fast_request: dict[str, dict[str, Any]] = {"body": {}, "headers": {}}
    fast_request_is_explicit = False
    supports_fast = False
    if model_meta:
        provider_def, model_def = model_meta
        levels = list(normalize_think_levels(model_def.thinking_levels))
        supports_fast = bool(getattr(model_def, "supports_fast", False))
        if think_requested and think_requested in levels:
            think_level = think_requested
        else:
            think_level = (
                configured_default_think_level(levels, model_def.default_thinking_level)
                if levels
                else "off"
            )
            if think_requested and think_requested not in levels:
                think_source = "model_default"
        if fast_requested:
            if frozen_fast_request_present:
                fast_request = frozen_fast_request
                fast_request_is_explicit = True
            elif getattr(model_def, "fast_request", None) is not None:
                fast_request = _fast_request_payload(model_def.fast_request)
                fast_request_is_explicit = True
            else:
                # Compatibility for existing manually marked Fast models.  New
                # models.dev-bound models use their confirmed request config above.
                service_tier = fast_request_mode(provider_def, model_def) or service_tier_hint
        else:
            service_tier = ""
    elif fast_requested and frozen_fast_request_present:
        fast_request = frozen_fast_request
        fast_request_is_explicit = True
    elif service_tier_hint and fast_requested:
        service_tier = service_tier_hint

    # An explicit empty Fast request is still a valid Fast mode: some public
    # records require no request additions.  Legacy Fast remains signalled by its
    # retained service-tier hint.
    effective_fast = bool(fast_requested and (supports_fast or fast_request_is_explicit or service_tier))
    if model_meta:
        _provider_def, model_def = model_meta
        base_cost = dict(getattr(model_def, "cost", None) or {})
        fast_cost = dict(getattr(model_def, "fast_cost", None) or {})
        selected_cost = fast_cost if effective_fast and fast_cost else base_cost
        cost = dict(selected_cost or {})

    thinking_levels = levels
    default_thinking = (
        configured_default_think_level(levels, model_meta[1].default_thinking_level)
        if model_meta and levels
        else ""
    )

    return {
        "model": model_name,
        "thinkLevel": think_level or "off",
        "fastMode": effective_fast,
        "fastRequested": bool(fast_requested),
        "fastSupported": supports_fast,
        "serviceTier": service_tier,
        "fastRequest": fast_request,
        "fastRequestExplicit": fast_request_is_explicit,
        "cost": cost,
        "baseCost": base_cost,
        "fastCost": fast_cost,
        "thinkingLevels": thinking_levels,
        "defaultThinkingLevel": default_thinking,
        "supportsThinking": bool(thinking_levels),
        "mainModel": main_model_label,
        "source": {
            "model": model_source,
            "thinkLevel": think_source,
            "fastMode": fast_source,
        },
        "stored": {
            "model": defaults.get("model") or "",
            "thinkLevel": defaults.get("thinkLevel") or "",
            "fastMode": _int_or(defaults.get("fastMode"), -1),
        },
    }


def agent_runtime_snapshot_fields(resolved: dict[str, Any]) -> dict[str, Any]:
    """Fields merged into agentSnapshot at task start so continue stays frozen."""
    source = resolved.get("source") if isinstance(resolved.get("source"), dict) else {}
    snapshot = {
        "model": str(resolved.get("model") or ""),
        "thinkLevel": str(resolved.get("thinkLevel") or "off"),
        "fastMode": bool(resolved.get("fastMode")),
        "serviceTier": str(resolved.get("serviceTier") or ""),
        "modelSource": str(source.get("model") or ""),
        "thinkSource": str(source.get("thinkLevel") or ""),
        "fastSource": str(source.get("fastMode") or ""),
        "resolvedAtStart": True,
    }
    if bool(resolved.get("fastRequestExplicit")):
        snapshot["fastRequest"] = _fast_request_payload(resolved.get("fastRequest"))
        snapshot["fastRequestExplicit"] = True
    return snapshot


def agent_run_config_public(resolved: dict[str, Any]) -> dict[str, Any]:
    """Web state payload for conversation Agent defaults + effective values."""
    stored = resolved.get("stored") if isinstance(resolved.get("stored"), dict) else {}
    source = resolved.get("source") if isinstance(resolved.get("source"), dict) else {}
    stored_fast = _int_or(stored.get("fastMode"), -1)
    return {
        "model": str(stored.get("model") or ""),
        "thinkLevel": str(stored.get("thinkLevel") or ""),
        "fastMode": None if stored_fast < 0 else bool(stored_fast),
        "fastModeRaw": stored_fast,
        "effective": {
            "model": str(resolved.get("model") or ""),
            "thinkLevel": str(resolved.get("thinkLevel") or "off"),
            "fastMode": bool(resolved.get("fastMode")),
            "fastSupported": bool(resolved.get("fastSupported")),
            "thinkingLevels": list(resolved.get("thinkingLevels") or []),
            "defaultThinkingLevel": str(resolved.get("defaultThinkingLevel") or ""),
            "supportsThinking": bool(resolved.get("supportsThinking")),
            "source": {
                "model": str(source.get("model") or ""),
                "thinkLevel": str(source.get("thinkLevel") or ""),
                "fastMode": str(source.get("fastMode") or ""),
            },
        },
    }
