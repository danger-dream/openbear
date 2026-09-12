"""The system settings UI must persist ordered candidates and all retained summary options."""
from __future__ import annotations

import json

import pytest

from tests.test_web_settings_channels_api import admin_env as settings_fixture

admin_env = settings_fixture


async def test_summary_candidate_multiselect_roundtrip_order_fallback_and_invalid_rollback(admin_env):
    env = admin_env
    url = "/api/settings/models.compressionModels"
    for candidates in (["openai/gpt", "anthropic/claude"], ["anthropic/claude", "openai/gpt"], []):
        response = await env.client.patch(url, json={"value": candidates}, cookies=env.cookie)
        assert response.status == 200, await response.text()
        assert (await response.json())["value"] == candidates
        assert env.server.config.models.compression_models == candidates
        persisted = json.loads(env.cfg_path.read_text())
        assert persisted["models"]["compressionModels"] == candidates
        current = await (await env.client.get("/api/settings", cookies=env.cookie)).json()
        assert current["values"]["models.compressionModels"] == candidates
    assert env.server.config.models.compression_model_candidates("openai/gpt") == ["openai/gpt"]
    before = env.cfg_path.read_bytes()
    response = await env.client.patch(url, json={"value": ["missing/model"]}, cookies=env.cookie)
    assert response.status == 400
    assert env.cfg_path.read_bytes() == before
    assert env.server.config.models.compression_models == []


@pytest.mark.parametrize("path,value", [
    ("agent.compactRatio", .6), ("agent.keepRecentMessages", 17),
    ("agent.compactMaxTokens", 4096), ("agent.compactMaxRetries", 2),
    ("agent.compactTimeoutS", 120), ("agent.manualCompactMinPercent", 40),
    ("agent.compactPrompt", "Existing={existing}\nHistory={history}"),
    ("contextManagement.defaultStrategy", "model_summary"),
    ("contextManagement.retainRatio", .2),
])
async def test_all_retained_and_new_context_settings_persist_and_hot_reload(admin_env, path, value):
    env = admin_env
    response = await env.client.patch(f"/api/settings/{path}", json={"value": value}, cookies=env.cookie)
    assert response.status == 200, await response.text()
    assert (await response.json())["value"] == value
    section, key = path.split(".")
    assert env.server.config.model_dump(by_alias=True)[section][key] == value
    assert json.loads(env.cfg_path.read_text())[section][key] == value
    specs = await (await env.client.get("/api/settings/specs", cookies=env.cookie)).json()
    assert "agent.memoryReminderPercent" not in specs["specs"]
    assert "agent.memoryReminderPrompt" not in specs["specs"]
    before = env.cfg_path.read_bytes()
    reminder = await env.client.patch("/api/settings/agent.memoryReminderPercent", json={"value": 50}, cookies=env.cookie)
    assert reminder.status == 400 and env.cfg_path.read_bytes() == before
