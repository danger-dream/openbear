"""Model/provider renames preserve ordered summary references and durable config."""
from __future__ import annotations

import copy
import json

import pytest

from app.admin.channels import update_model_mutator, update_provider_mutator
from app.config import ModelsConfig
from tests.test_web_settings_channels_api import admin_env as admin_fixture

admin_env = admin_fixture


def raw_config(key, candidates):
    return {"models": {"primary": "p/main", key: candidates, "providers": {
        "p": {"baseUrl": "http://not-called.invalid", "apiKey": "fixture", "protocol": "chat",
              "models": [{"id": "main"}, {"id": "cheap"}, {"id": "cheap-plus"}]},
        "other": {"baseUrl": "http://not-called.invalid", "apiKey": "fixture", "protocol": "chat",
                  "models": [{"id": "fallback"}]},
    }}}


@pytest.mark.parametrize("rename", ["provider", "model"])
@pytest.mark.parametrize("key", ["compressionModels", "compression_models"])
@pytest.mark.parametrize("as_text", [False, True])
def test_summary_rename_preserves_order_and_exact_matches(rename, key, as_text):
    ordered = ["other/fallback", "p/cheap", "p/cheap-plus", "p/main", "p/cheap"]
    raw = raw_config(key, ", ".join(ordered) if as_text else ordered)
    untouched = copy.deepcopy(raw["models"]["providers"]["other"])
    if rename == "provider":
        update_provider_mutator("p", {"name": "renamed"})(raw)
        expected = ["other/fallback", "renamed/cheap", "renamed/cheap-plus", "renamed/main"]
        assert raw["models"]["primary"] == "renamed/main"
    else:
        update_model_mutator("p", "cheap", {"id": "renamed"})(raw)
        expected = ["other/fallback", "p/renamed", "p/cheap-plus", "p/main"]
        assert raw["models"]["primary"] == "p/main"
    cfg = ModelsConfig.model_validate(raw["models"])
    assert raw["models"][key] == cfg.compression_models == expected
    assert all(cfg.resolve(value) is not None for value in expected)
    assert raw["models"]["providers"]["other"] == untouched


@pytest.mark.parametrize("rename", ["provider", "model"])
async def test_summary_rename_persists_and_hot_loads_same_order(admin_env, rename):
    expected = (["renamed/claude"] if rename == "provider" else ["anthropic/renamed"])
    path = "/api/channels/anthropic" if rename == "provider" else "/api/channels/anthropic/models/claude"
    patch = {"name": "renamed"} if rename == "provider" else {"id": "renamed"}
    response = await admin_env.client.patch(path, cookies=admin_env.cookie, json=patch)
    assert response.status == 200, await response.text()
    raw = json.loads(admin_env.cfg_path.read_text())
    assert raw["models"]["compressionModels"] == expected
    assert admin_env.server.config.models.compression_models == expected
    assert admin_env.server.config.models.resolve(expected[0]) is not None


def test_absent_summary_configuration_stays_absent_and_failed_rename_does_not_mutate():
    raw = raw_config("compressionModels", [])
    raw["models"].pop("compressionModels")
    update_model_mutator("p", "cheap", {"id": "renamed"})(raw)
    assert "compressionModels" not in raw["models"]
    before = copy.deepcopy(raw)
    with pytest.raises(ValueError, match="已存在"):
        update_provider_mutator("p", {"name": "other"})(raw)
    assert raw == before
