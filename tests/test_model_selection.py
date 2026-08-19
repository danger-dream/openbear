"""模型切换运行态规则测试。"""
from __future__ import annotations

import json

from app.config import Config
from app.models.selection import ModelSelection
from tests.test_config import _base_cfg


def test_model_selection_reports_protocol(tmp_path):
    cfg_raw = _base_cfg()
    cfg_raw["models"]["providers"]["openai"]["models"].append({"id": "gpt", "name": "GPT"})
    p = tmp_path / "openbear.json"
    p.write_text(json.dumps(cfg_raw), encoding="utf-8")
    cfg = Config.model_validate(cfg_raw)
    sel = ModelSelection(cfg.models, p)

    assert sel.protocol_of("openai/gpt") == "chat"
    assert sel.protocol_of("anthropic/claude") == "anthropic"


def test_model_selection_family_rules(tmp_path):
    """parrot 只支持 openai 家族内 chat↔responses 互转，anthropic 跨家族禁止。"""
    cfg_raw = _base_cfg()
    # 同家族另一协议：responses
    cfg_raw["models"]["providers"]["oai_resp"] = {
        "baseUrl": "http://x", "apiKey": "k", "protocol": "responses",
        "models": [{"id": "o3", "name": "O3", "contextWindow": 1000000, "maxTokens": 8192}],
    }
    p = tmp_path / "openbear.json"
    p.write_text(json.dumps(cfg_raw), encoding="utf-8")
    cfg = Config.model_validate(cfg_raw)
    sel = ModelSelection(cfg.models, p)  # 当前 openai/deepseek (chat)

    # chat → responses：同 openai 家族，允许
    assert sel.family_of("oai_resp/o3") == "openai"
    assert sel.same_family_as_current("oai_resp/o3") is True
    # chat → anthropic：跨家族，禁止
    assert sel.family_of("anthropic/claude") == "anthropic"
    assert sel.same_family_as_current("anthropic/claude") is False
