"""配置加载 + 校验测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Config, MCPServerConfig, ModelDef, fast_request_mode, load_config


def _base_cfg() -> dict:
    return {
        "telegram": {"botToken": "t", "whitelistIds": [123]},
        "models": {
            "providers": {
                "openai": {"baseUrl": "http://x", "apiKey": "k", "protocol": "chat",
                           "models": [{"id": "deepseek", "name": "DS", "contextWindow": 1000000, "maxTokens": 8192}]},
                "anthropic": {"baseUrl": "http://x", "apiKey": "k", "protocol": "anthropic",
                              "models": [{"id": "claude", "name": "C"}]},
            },
            "primary": "openai/deepseek",
            "compressionModels": ["openai/deepseek"],
        },
        "memory": {"baseUrl": "http://m", "identity": "openbear", "accessKey": "ak"},
    }


def test_example_config_loads_and_matches_visible_defaults():
    cfg = load_config(Path(__file__).resolve().parents[1] / "openbear.json.example")
    assert cfg.ui.show_turn_stats is True
    assert cfg.agent.compact_timeout_s == 1800
    assert cfg.validate_for_startup() == []


def test_deprecated_settings_are_accepted_but_omitted_on_write_back():
    data = _base_cfg()
    data["agent"] = {"interruptOnNew": True}
    data["ui"] = {"showThinking": True, "showTurnStats": True}
    data["media"] = {"enabled": False, "keepDays": 30}
    cfg = Config.model_validate(data)
    assert cfg.agent.interrupt_on_new is True
    assert cfg.ui.show_thinking is True
    assert cfg.media.enabled is False
    assert cfg.media.keep_days == 30

    dumped = cfg.model_dump(mode="json", by_alias=True)
    assert "interruptOnNew" not in dumped["agent"]
    assert "showThinking" not in dumped["ui"]
    assert "enabled" not in dumped["media"]
    assert "keepDays" not in dumped["media"]


def test_load_and_validate_ok(tmp_path):
    p = tmp_path / "openbear.json"
    p.write_text(json.dumps(_base_cfg()), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.validate_for_startup() == []
    assert cfg.models.primary == "openai/deepseek"
    assert cfg.models.compression_models == ["openai/deepseek"]
    assert cfg.models.compression_model_candidates("anthropic/claude") == ["openai/deepseek", "anthropic/claude"]


def test_plan_limit_aliases_read_legacy_and_write_canonical_names():
    data = _base_cfg()
    data["rath"] = {
        "agentPlanMaxRevisionRounds": 4,
        "agentPlanMaxSteps": 31,
        "agentPlanMaxCriteriaPerStep": 11,
        "agentPlanMaxFinalOutputs": 21,
        "planDraftPrompt": "任务：{task}\n结构：{plan_schema}",
    }
    cfg = Config.model_validate(data)
    assert cfg.rath.agent_plan_max_revision_rounds == 4
    assert cfg.rath.agent_plan_max_steps == 31
    dumped = cfg.model_dump(mode="json", by_alias=True)["rath"]
    assert dumped["planMaxRevisionRounds"] == 4
    assert dumped["planMaxSteps"] == 31
    assert dumped["planMaxCriteriaPerStep"] == 11
    assert dumped["planMaxFinalOutputs"] == 21
    assert "agentPlanMaxRevisionRounds" not in dumped


def test_mcp_defaults_keep_legacy_config_disabled():
    cfg = Config.model_validate(_base_cfg())
    assert cfg.mcp.enabled is False
    assert cfg.mcp.install_dir == "./mcp-servers"
    assert cfg.mcp.default_approval == "ask"
    assert cfg.mcp.allow_tools == ["*"]
    assert cfg.mcp.servers == {}


def test_mcp_install_dir_alias_round_trips():
    data = _base_cfg()
    data["mcp"] = {"installDir": "./local-mcp"}
    cfg = Config.model_validate(data)
    assert cfg.mcp.install_dir == "./local-mcp"
    assert cfg.model_dump(mode="json", by_alias=True)["mcp"]["installDir"] == "./local-mcp"


def test_mcp_server_config_aliases_and_validation():
    server = MCPServerConfig.model_validate({
        "enabled": True,
        "transport": "streamable_http",
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer ${TOKEN}"},
        "connectTimeoutS": 3,
        "toolCallTimeoutS": 5,
        "approval": "ask",
        "tools": {"allow": ["read*"], "deny": ["delete*"]},
    })
    assert server.transport == "streamable_http"
    assert server.connect_timeout_s == 3
    assert server.tool_call_timeout_s == 5
    assert server.tools.allow == ["read*"]
    with pytest.raises(Exception):
        MCPServerConfig.model_validate({"transport": "sse"})
    with pytest.raises(Exception):
        MCPServerConfig.model_validate({"approval": "defaultPermission"})


def test_model_cost_tier_schema_rejects_lossy_or_unknown_values():
    invalid_costs = [
        {"tiers": [{"contextTokens": True, "input": 1}]},
        {"tiers": [{"contextTokens": 1.5, "input": 1}]},
        {"tiers": [{"contextTokens": 1, "input": 1, "legacy": 2}]},
        {"tiers": [{"contextTokens": 1}]},
        {"input": 1, "context_over_200k": 2},
    ]
    for cost in invalid_costs:
        with pytest.raises(ValueError):
            ModelDef(id="test", cost=cost)


def test_resolve_model():
    cfg = Config.model_validate(_base_cfg())
    r = cfg.models.resolve("openai/deepseek")
    assert r is not None
    prov, model = r
    assert prov.protocol == "chat"
    assert model.context_window == 1000000
    assert cfg.models.resolve("nope/x") is None


def test_model_fast_request_config_is_json_safe_and_header_validated():
    model = ModelDef(
        id="gpt",
        fastRequest={
            "body": {"service_tier": "priority", "nested": {"enabled": True}},
            "headers": {"anthropic-beta": "fast-mode-2026-02-01"},
        },
    )
    assert model.fast_request is not None
    assert model.fast_request.body["nested"] == {"enabled": True}
    assert model.fast_request.headers == {"anthropic-beta": "fast-mode-2026-02-01"}
    with pytest.raises(ValueError):
        ModelDef(id="bad", fastRequest={"body": []})
    with pytest.raises(ValueError):
        ModelDef(id="bad", fastRequest={"headers": {"bad\nheader": "x"}})


def test_fast_request_mode_is_protocol_specific():
    data = _base_cfg()
    data["models"]["providers"]["openai"]["models"][0]["supportsFast"] = True
    data["models"]["providers"]["anthropic"]["models"][0]["supportsFast"] = True
    data["models"]["providers"]["responses"] = {
        "baseUrl": "http://x",
        "apiKey": "k",
        "protocol": "responses",
        "models": [{"id": "gpt", "name": "GPT", "supportsFast": True}],
    }
    cfg = Config.model_validate(data)
    openai_provider, openai_model = cfg.models.resolve("openai/deepseek")
    anthropic_provider, anthropic_model = cfg.models.resolve("anthropic/claude")
    responses_provider, responses_model = cfg.models.resolve("responses/gpt")
    assert fast_request_mode(openai_provider, openai_model) == "priority"
    assert fast_request_mode(responses_provider, responses_model) == "priority"
    assert fast_request_mode(anthropic_provider, anthropic_model) == "fast"


def test_fast_request_mode_empty_when_model_not_fast():
    cfg = Config.model_validate(_base_cfg())
    provider, model = cfg.models.resolve("openai/deepseek")
    assert fast_request_mode(provider, model) == ""


def test_agent_renamed_fields_only():
    data = _base_cfg()
    data["agent"] = {
        "maxRunWallSeconds": 0,
        "keepRecentMessages": 6,
        "compactTimeoutS": 2400,
    }
    cfg = Config.model_validate(data)
    assert cfg.agent.max_run_wall_seconds == 0
    assert cfg.agent.keep_recent_messages == 6
    assert cfg.agent.compact_timeout_s == 2400


def test_removed_session_history_config_fields_are_rejected():
    cfg = Config.model_validate(_base_cfg())
    assert cfg.session.model_dump() == {}
    data = _base_cfg()
    data["session"] = {"idleArchiveMinutes": 5, "idleArchiveScanSeconds": 30}
    with pytest.raises(Exception):
        Config.model_validate(data)


def test_web_config_defaults_and_aliases():
    cfg = Config.model_validate(_base_cfg())
    assert cfg.web.enabled is True
    assert cfg.web.host == "0.0.0.0"
    assert cfg.web.port == 18961
    assert cfg.web.custom_url == ""
    assert cfg.web.session_days == 30
    data = _base_cfg()
    data["web"] = {"enabled": False, "host": "127.0.0.1", "port": 19999, "customUrl": "https://panel.example.com", "sessionDays": 7}
    cfg = Config.model_validate(data)
    assert cfg.web.enabled is False
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 19999
    assert cfg.web.custom_url == "https://panel.example.com"
    assert cfg.web.session_days == 7


@pytest.mark.parametrize("field", ["maxSessionTokens", "maxWallSeconds", "keepRecentTurns", "maxRunTokens"])
def test_agent_removed_field_names_are_rejected(field):
    data = _base_cfg()
    data["agent"] = {field: 12345}
    with pytest.raises(Exception):
        Config.model_validate(data)


def test_disabled_provider_hidden_from_default_resolution():
    data = _base_cfg()
    data["models"]["providers"]["anthropic"]["enabled"] = False
    cfg = Config.model_validate(data)
    assert cfg.models.resolve("anthropic/claude") is None
    assert cfg.models.resolve("anthropic/claude", include_disabled=True) is not None


def test_validate_primary_on_disabled_provider_fails():
    data = _base_cfg()
    data["models"]["providers"]["openai"]["enabled"] = False
    cfg = Config.model_validate(data)
    errors = cfg.validate_for_startup()
    assert any("primary" in e for e in errors)


def test_validate_compression_on_disabled_provider_fails():
    data = _base_cfg()
    data["models"]["providers"]["openai"]["enabled"] = False
    data["models"]["primary"] = "anthropic/claude"
    data["models"]["compressionModels"] = ["openai/deepseek"]
    cfg = Config.model_validate(data)
    errors = cfg.validate_for_startup()
    assert any("compressionModels" in e for e in errors)


def test_validate_primary_missing():
    bad = _base_cfg()
    bad["models"]["primary"] = "openai/nonexistent"
    cfg = Config.model_validate(bad)
    errors = cfg.validate_for_startup()
    assert any("primary" in e for e in errors)


def test_validate_bad_protocol():
    bad = _base_cfg()
    bad["models"]["providers"]["openai"]["protocol"] = "grpc"
    with pytest.raises(Exception):
        Config.model_validate(bad)


def test_validate_empty_whitelist():
    bad = _base_cfg()
    bad["telegram"]["whitelistIds"] = []
    cfg = Config.model_validate(bad)
    assert any("whitelist" in e.lower() for e in cfg.validate_for_startup())


def test_webhook_requires_host():
    bad = _base_cfg()
    bad["telegram"]["mode"] = "webhook"
    cfg = Config.model_validate(bad)
    errors = cfg.validate_for_startup()
    assert any("webhookHost" in e for e in errors)
