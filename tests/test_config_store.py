from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config_store import ConfigConflictError, ConfigStore


def _sample_config() -> dict:
    return {
        "telegram": {"botToken": "123:abc", "whitelistIds": [5352767013]},
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://127.0.0.1:22122",
                    "apiKey": "key",
                    "protocol": "chat",
                    "models": [{"id": "gpt", "name": "GPT", "reasoning": True}],
                }
            },
            "primary": "openai/gpt",
        },
        "memory": {
            "baseUrl": "http://127.0.0.1:8899/api",
            "identity": "openbear",
            "accessKey": "mem-key",
        },
        "ui": {"showTurnStats": True, "editThrottleMs": 1000},
    }


@pytest.mark.asyncio
async def test_config_store_load_keeps_builtin_prompt_as_unmaterialized_default(tmp_path: Path):
    path = tmp_path / "openbear.json"
    original = _sample_config()
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(path)

    cfg = await store.load_config()

    assert cfg.agent.compact_prompt == ""
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "agent" not in raw
    assert store.revision == 0


@pytest.mark.asyncio
async def test_config_store_migrates_legacy_plan_limit_keys_only(tmp_path: Path):
    path = tmp_path / "openbear.json"
    original = _sample_config()
    original["rath"] = {"agentPlanMaxRevisionRounds": 4, "planMaxSteps": 40}
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(path)

    cfg = await store.load_config()

    assert cfg.rath.agent_plan_max_revision_rounds == 4
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["rath"]["planMaxRevisionRounds"] == 4
    assert raw["rath"]["planMaxSteps"] == 40
    assert "agentPlanMaxRevisionRounds" not in raw["rath"]
    assert "agent" not in raw
    assert store.revision == 1


@pytest.mark.asyncio
async def test_config_store_update_path_writes_and_backs_up(tmp_path: Path):
    path = tmp_path / "openbear.json"
    path.write_text(json.dumps(_sample_config(), ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(path)

    cfg = await store.update_path("ui.showTurnStats", False)

    assert cfg.ui.show_turn_stats is False
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["ui"]["showTurnStats"] is False
    backup = tmp_path / "openbear.json.bak.1"
    assert backup.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600
    assert store.revision == 1


@pytest.mark.asyncio
async def test_config_store_snapshot_can_rollback_when_unchanged(tmp_path: Path):
    path = tmp_path / "openbear.json"
    original = _sample_config()
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(path)

    snapshot = await store.mutate_with_snapshot(lambda raw: raw["ui"].update({"showTurnStats": False}))
    restored = await store.restore_snapshot(snapshot)

    assert restored.ui.show_turn_stats is True
    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert store.revision == 2


async def test_config_store_snapshot_refuses_to_overwrite_newer_change(tmp_path: Path):
    path = tmp_path / "openbear.json"
    path.write_text(json.dumps(_sample_config(), ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(path)

    snapshot = await store.mutate_with_snapshot(lambda raw: raw["ui"].update({"showTurnStats": False}))
    await store.update_path("ui.editThrottleMs", 2200)

    with pytest.raises(ConfigConflictError, match="config_changed_since_mutation"):
        await store.restore_snapshot(snapshot)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["ui"]["showTurnStats"] is False
    assert raw["ui"]["editThrottleMs"] == 2200


async def test_config_store_validation_failure_keeps_original_file(tmp_path: Path):
    path = tmp_path / "openbear.json"
    original = _sample_config()
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(path)

    with pytest.raises(Exception):
        await store.update_path("models.primary", "missing/model")

    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert store.revision == 0
