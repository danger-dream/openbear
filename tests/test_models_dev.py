from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.admin import channels as channel_admin
from app.config import ModelDef, ModelsConfig
from app.models_dev import (
    ModelsDevCatalog,
    models_dev_metadata_fingerprint,
    models_dev_metadata_to_openbear,
)


def _catalog_payload() -> dict:
    return {
        "models": {"acme/demo": {"id": "acme/demo", "name": "Canonical Demo"}},
        "providers": {
            "acme": {
                "id": "acme",
                "name": "Acme Cloud",
                "models": {
                    "demo/v1": {
                        "id": "demo/v1",
                        "name": "Demo V1",
                        "reasoning": True,
                        "reasoning_options": [{"type": "effort", "values": ["none", "low", "high"]}],
                        "modalities": {"input": ["text", "image"], "output": ["text"]},
                        "limit": {"context": 1_000_000, "output": 32_768},
                        "cost": {
                            "input": 1.25,
                            "output": 10,
                            "cache_read": 0.125,
                            "cache_write": 1.5,
                            "tiers": [
                                {
                                    "tier": {"type": "context", "size": 200_000},
                                    "input": 2.5,
                                    "output": 15,
                                    "cache_read": 0.25,
                                    "cache_write": 3,
                                },
                                {
                                    "tier": {"type": "context", "size": 500_000},
                                    "input": 5,
                                    "output": 20,
                                },
                            ],
                        },
                        "experimental": {
                            "modes": {
                                "fast": {
                                    "cost": {
                                        "input": 2.5,
                                        "output": 20,
                                        "tiers": [{
                                            "tier": {"type": "context", "size": 200_000},
                                            "input": 4,
                                            "output": 30,
                                        }],
                                    },
                                    "provider": {
                                        "body": {"service_tier": "priority"},
                                        "headers": {"x-fast-mode": "enabled"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
    }


def _catalog_from_cache(tmp_path, payload: dict | None = None) -> ModelsDevCatalog:
    cache_dir = tmp_path / "models-dev"
    cache_dir.mkdir()
    (cache_dir / "catalog.json").write_text(json.dumps(payload or _catalog_payload()), encoding="utf-8")
    return ModelsDevCatalog(cache_dir)


def test_catalog_cache_projects_tiers_and_reasoning_options(tmp_path) -> None:
    catalog = _catalog_from_cache(tmp_path)

    assert catalog.status()["available"] is True
    assert catalog.list_providers() == [{"id": "acme", "name": "Acme Cloud", "modelCount": 1}]
    assert catalog.list_provider_models("acme")[0]["id"] == "demo/v1"

    projected = models_dev_metadata_to_openbear(catalog.get_model("acme", "demo/v1") or {})
    assert projected["name"] == "Demo V1"
    assert projected["input"] == ["text", "image"]
    assert projected["contextWindow"] == 1_000_000
    assert projected["maxTokens"] == 32_768
    assert projected["thinkingLevels"] == ["off", "low", "high"]
    assert projected["compactTriggerTokens"] == 200_000
    assert projected["cost"] == {
        "input": 1.25,
        "output": 10.0,
        "cacheRead": 0.125,
        "cacheWrite": 1.5,
        "tiers": [
            {"contextTokens": 200_000, "input": 2.5, "output": 15.0, "cacheRead": 0.25, "cacheWrite": 3.0},
            {"contextTokens": 500_000, "input": 5.0, "output": 20.0, "cacheRead": 0.0, "cacheWrite": 0.0},
        ],
    }
    # OpenCode normalizes omitted cache prices to zero.  Fast replaces the
    # matching base/tier rates while retaining unrelated context tiers.
    assert projected["supportsFast"] is True
    assert projected["fastRequest"] == {
        "body": {"service_tier": "priority"},
        "headers": {"x-fast-mode": "enabled"},
    }
    assert projected["fastCost"] == {
        "input": 2.5,
        "output": 20.0,
        "cacheRead": 0.0,
        "cacheWrite": 0.0,
        "tiers": [
            {"contextTokens": 200_000, "input": 4.0, "output": 30.0, "cacheRead": 0.0, "cacheWrite": 0.0},
            {"contextTokens": 500_000, "input": 5.0, "output": 20.0, "cacheRead": 0.0, "cacheWrite": 0.0},
        ],
    }


def test_bundled_grok_45_override_is_applied_after_cache_without_mutating_snapshot(tmp_path) -> None:
    payload = {
        "models": {"xai/grok-4.5": {"id": "xai/grok-4.5", "name": "Grok 4.5"}},
        "providers": {
            "xai": {
                "id": "xai",
                "name": "xAI",
                "models": {
                    "grok-4.5": {
                        "id": "grok-4.5",
                        "name": "Grok 4.5",
                        "cost": {
                            "input": 2,
                            "output": 6,
                            "cache_read": 0.3,
                            "tiers": [{
                                "tier": {"type": "context", "size": 200_000},
                                "input": 4,
                                "output": 12,
                                "cache_read": 1,
                            }],
                        },
                    }
                },
            }
        },
    }
    cache_dir = tmp_path / "models-dev"
    cache_dir.mkdir()
    cache_path = cache_dir / "catalog.json"
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    original_bytes = cache_path.read_bytes()

    catalog = ModelsDevCatalog(cache_dir)
    projected = models_dev_metadata_to_openbear(catalog.get_model("xai", "grok-4.5") or {})

    assert catalog.status()["localOverrideCount"] == 1
    assert projected["supportsFast"] is True
    assert projected["fastRequest"] == {
        "body": {"service_tier": "priority"},
        "headers": {},
    }
    assert projected["cost"]["tiers"] == [{
        "contextTokens": 200_000,
        "input": 4.0,
        "output": 12.0,
        "cacheRead": 0.6,
        "cacheWrite": 0.0,
    }]
    assert projected["fastCost"] == {
        "input": 4.0,
        "output": 12.0,
        "cacheRead": 0.6,
        "cacheWrite": 0.0,
        "tiers": [{
            "contextTokens": 200_000,
            "input": 8.0,
            "output": 24.0,
            "cacheRead": 1.2,
            "cacheWrite": 0.0,
        }],
    }
    # Local correction is an effective read overlay, not a rewrite of upstream cache.
    assert cache_path.read_bytes() == original_bytes
    assert json.loads(cache_path.read_text(encoding="utf-8"))["providers"]["xai"]["models"]["grok-4.5"]["cost"]["tiers"][0]["cache_read"] == 1


def test_preview_and_sync_maps_first_price_tier_to_compact_trigger(tmp_path) -> None:
    catalog = _catalog_from_cache(tmp_path)
    raw = {
        "models": {
            "providers": {
                "proxy": {
                    "baseUrl": "https://proxy.example/v1",
                    "apiKey": "secret",
                    "protocol": "chat",
                    "modelsDevProviderId": "acme",
                    "models": [{
                        "id": "private-demo",
                        "name": "Proxy Demo",
                        "reasoning": False,
                        "contextWindow": 128_000,
                        "maxTokens": 8_192,
                        "cost": {"input": 9, "output": 9},
                        "supportsFast": True,
                        "compactTriggerTokens": 77_777,
                    }],
                }
            },
            "primary": "proxy/private-demo",
        }
    }
    models = ModelsConfig.model_validate(raw["models"])

    preview = channel_admin.models_dev_sync_preview(
        models,
        "proxy",
        "private-demo",
        catalog,
        {"providerId": "acme", "modelId": "demo/v1"},
    )
    changed = {item["field"] for item in preview["changes"]}
    assert {"contextWindow", "maxTokens", "compactTriggerTokens", "cost", "reasoning"} <= changed
    assert preview["metadataSha256"] == models_dev_metadata_fingerprint(preview["metadata"])

    channel_admin.sync_model_from_models_dev_mutator(
        "proxy",
        "private-demo",
        source=preview["source"],
        metadata=preview["metadata"],
        catalog_sha256="catalog-sha",
        synced_at=1_700_000_000,
    )(raw)
    synced = ModelsConfig.model_validate(raw["models"]).resolve("proxy/private-demo")
    assert synced is not None
    _, model = synced
    assert model.models_dev is not None
    assert model.models_dev.provider_id == "acme"
    assert model.models_dev.model_id == "demo/v1"
    assert model.models_dev.catalog_sha256 == "catalog-sha"
    assert model.models_dev.metadata_sha256 == models_dev_metadata_fingerprint(preview["metadata"])
    assert model.context_window == 1_000_000
    assert model.max_tokens == 32_768
    assert model.cost["tiers"][0]["contextTokens"] == 200_000
    # Fast request additions and the effective merged Fast price table are
    # persisted only after this confirmed source sync.
    assert model.supports_fast is True
    assert model.fast_request is not None
    assert model.fast_request.body == {"service_tier": "priority"}
    assert model.fast_request.headers == {"x-fast-mode": "enabled"}
    assert model.fast_cost["output"] == 20.0
    assert model.fast_cost["cacheRead"] == 0.0
    assert model.fast_cost["cacheWrite"] == 0.0
    assert model.fast_cost["tiers"][0]["input"] == 4.0
    assert model.fast_cost["tiers"][0]["output"] == 30.0
    assert model.fast_cost["tiers"][0]["cacheRead"] == 0.0
    assert model.fast_cost["tiers"][0]["cacheWrite"] == 0.0
    assert model.compact_trigger_tokens == 200_000


def test_sync_without_published_fast_clears_stale_fast_request(tmp_path) -> None:
    payload = _catalog_payload()
    payload["providers"]["acme"]["models"]["demo/v1"].pop("experimental")
    catalog = _catalog_from_cache(tmp_path, payload)
    raw = {
        "models": {
            "providers": {
                "proxy": {
                    "baseUrl": "https://proxy.example/v1",
                    "apiKey": "secret",
                    "protocol": "chat",
                    "models": [{
                        "id": "private-demo",
                        "supportsFast": True,
                        "fastCost": {"input": 99},
                        "fastRequest": {
                            "body": {"service_tier": "priority"},
                            "headers": {"x-fast-mode": "old"},
                        },
                    }],
                }
            },
            "primary": "proxy/private-demo",
        }
    }
    models = ModelsConfig.model_validate(raw["models"])
    preview = channel_admin.models_dev_sync_preview(
        models, "proxy", "private-demo", catalog, {"providerId": "acme", "modelId": "demo/v1"},
    )
    assert preview["metadata"]["supportsFast"] is False
    assert preview["metadata"]["fastRequest"] is None

    channel_admin.sync_model_from_models_dev_mutator(
        "proxy", "private-demo", source=preview["source"], metadata=preview["metadata"], synced_at=1,
    )(raw)
    synced = raw["models"]["providers"]["proxy"]["models"][0]
    assert synced["supportsFast"] is False
    assert synced["fastCost"] == {}
    assert "fastRequest" not in synced


def test_unbound_model_sync_never_infers_source_from_channel_or_local_model_id(tmp_path) -> None:
    catalog = _catalog_from_cache(tmp_path)
    raw = {
        "models": {
            "providers": {
                "proxy": {
                    "baseUrl": "https://proxy.example/v1",
                    "apiKey": "secret",
                    "protocol": "chat",
                    # This remains a picker default only, not a model binding.
                    "modelsDevProviderId": "acme",
                    "models": [{"id": "private-demo"}],
                }
            },
            "primary": "proxy/private-demo",
        }
    }
    models = ModelsConfig.model_validate(raw["models"])

    with pytest.raises(ValueError, match="请先选择元数据提供者和模型 ID"):
        channel_admin.models_dev_sync_preview(models, "proxy", "private-demo", catalog)
    with pytest.raises(ValueError, match="必须同时填写"):
        channel_admin.models_dev_sync_preview(
            models,
            "proxy",
            "private-demo",
            catalog,
            {"providerId": "acme"},
        )

    raw["models"]["providers"]["proxy"]["models"][0]["modelsDev"] = {
        "providerId": "acme",
        "modelId": "demo/v1",
    }
    explicitly_bound = ModelsConfig.model_validate(raw["models"])
    preview = channel_admin.models_dev_sync_preview(explicitly_bound, "proxy", "private-demo", catalog)
    assert preview["source"]["providerId"] == "acme"
    assert preview["source"]["modelId"] == "demo/v1"


def test_explicit_no_reasoning_clears_stale_public_options() -> None:
    raw = {
        "models": {
            "providers": {
                "proxy": {
                    "models": [{
                        "id": "demo",
                        "reasoning": True,
                        "reasoningOptions": [{"type": "effort", "values": ["high"]}],
                        "thinkingLevels": ["high"],
                        "defaultThinkingLevel": "high",
                    }]
                }
            }
        }
    }
    channel_admin.sync_model_from_models_dev_mutator(
        "proxy",
        "demo",
        source={"providerId": "acme", "modelId": "demo/v1"},
        metadata={"reasoning": False},
        synced_at=1_700_000_000,
    )(raw)
    synced = raw["models"]["providers"]["proxy"]["models"][0]
    assert synced["reasoning"] is False
    assert synced["reasoningOptions"] == []
    assert synced["thinkingLevels"] == []
    assert synced["defaultThinkingLevel"] == ""


def test_exact_model_source_lookup_returns_only_same_id_providers(tmp_path) -> None:
    catalog = _catalog_from_cache(tmp_path)
    assert catalog._catalog is not None
    catalog._catalog["providers"]["second"] = {
        "id": "second",
        "name": "Second Cloud",
        "models": {
            "demo/v1": {
                "id": "demo/v1",
                "name": "Second Demo",
                "limit": {"context": 64_000, "output": 4_096},
            },
            "demo-v1": {"id": "demo-v1", "name": "Similar but distinct"},
        },
    }

    sources = catalog.list_model_sources("demo/v1")
    assert [(item["providerId"], item["modelId"]) for item in sources] == [
        ("acme", "demo/v1"),
        ("second", "demo/v1"),
    ]
    assert catalog.list_model_sources("Demo V1") == []


def test_canonical_model_source_is_the_picker_default_and_sorts_first(tmp_path) -> None:
    payload = _catalog_payload()
    payload["models"]["openai/gpt-4.1"] = {"id": "openai/gpt-4.1", "name": "GPT-4.1"}
    payload["providers"]["relay"] = {
        "id": "relay",
        "name": "AAA Relay",
        "models": {"gpt-4.1": {"id": "gpt-4.1", "name": "GPT-4.1"}},
    }
    payload["providers"]["openai"] = {
        "id": "openai",
        "name": "OpenAI",
        "models": {"gpt-4.1": {"id": "gpt-4.1", "name": "GPT-4.1"}},
    }
    catalog = _catalog_from_cache(tmp_path, payload)

    assert catalog.default_provider_for_model("gpt-4.1") == "openai"
    sources = catalog.list_model_sources("gpt-4.1")
    assert [source["providerId"] for source in sources] == ["openai", "relay"]
    assert [source["isDefault"] for source in sources] == [True, False]

    models = ModelsConfig.model_validate({
        "providers": {
            "proxy": {
                "baseUrl": "https://proxy.example/v1",
                "apiKey": "secret",
                "protocol": "chat",
                "models": [{"id": "gpt-4.1"}],
            }
        },
        "primary": "proxy/gpt-4.1",
    })
    matches = channel_admin.models_dev_source_matches(models, "proxy", catalog)
    match = matches["items"][0]
    assert match["defaultProviderId"] == "openai"
    assert [source["providerId"] for source in match["candidates"]] == ["openai", "relay"]


def test_batch_preview_and_sync_apply_all_models_in_one_mutator(tmp_path) -> None:
    catalog = _catalog_from_cache(tmp_path)
    raw = {
        "models": {
            "providers": {
                "proxy": {
                    "baseUrl": "https://proxy.example/v1",
                    "apiKey": "secret",
                    "protocol": "chat",
                    "models": [
                        {"id": "one", "name": "One", "compactTriggerTokens": 77_777},
                        {"id": "two", "name": "Two", "compactTriggerTokens": 88_888},
                    ]
                }
            },
            "primary": "proxy/one",
        }
    }
    models = ModelsConfig.model_validate(raw["models"])
    selected = [
        {"localModelId": "one", "source": {"providerId": "acme", "modelId": "demo/v1"}},
        {"localModelId": "two", "source": {"providerId": "acme", "modelId": "demo/v1"}},
    ]

    preview = channel_admin.models_dev_batch_sync_preview(models, "proxy", catalog, selected)
    assert [item["localModelId"] for item in preview["items"]] == ["one", "two"]
    assert all(item["metadata"]["compactTriggerTokens"] == 200_000 for item in preview["items"])

    channel_admin.sync_models_from_models_dev_mutator(
        "proxy",
        preview["items"],
        catalog_sha256="catalog-sha",
        synced_at=1_700_000_000,
    )(raw)
    synced = ModelsConfig.model_validate(raw["models"])
    for local_model_id in ("one", "two"):
        resolved = synced.resolve(f"proxy/{local_model_id}")
        assert resolved is not None
        model = resolved[1]
        assert model.models_dev is not None
        assert model.models_dev.provider_id == "acme"
        assert model.compact_trigger_tokens == 200_000


def test_source_status_tracks_model_metadata_not_catalog_wide_digest(tmp_path) -> None:
    catalog = _catalog_from_cache(tmp_path)
    record = catalog.get_model("acme", "demo/v1")
    assert record is not None
    metadata = models_dev_metadata_to_openbear(record)
    source = {
        "providerId": "acme",
        "modelId": "demo/v1",
        "syncedAt": 1_700_000_000,
        # Deliberately different from the cached catalog SHA: unrelated catalog
        # records must not make this model appear stale.
        "catalogSha256": "older-full-catalog",
        "metadataSha256": models_dev_metadata_fingerprint(metadata),
    }
    model = ModelDef(id="private-demo", modelsDev=source)

    state = channel_admin.model_to_json(model, models_dev_catalog=catalog)["modelsDev"]
    assert state["needsSync"] is False
    assert state["updateAvailable"] is False

    assert catalog._catalog is not None  # Test-only mutation of the cached fixture.
    catalog._catalog["providers"]["acme"]["models"]["demo/v1"]["limit"]["context"] = 2_000_000
    changed = channel_admin.model_to_json(model, models_dev_catalog=catalog)["modelsDev"]
    assert changed["updateAvailable"] is True

    pending = ModelDef(id="pending", modelsDev={"providerId": "acme", "modelId": "demo/v1"})
    pending_state = channel_admin.model_to_json(pending, models_dev_catalog=catalog)["modelsDev"]
    assert pending_state["needsSync"] is True
    assert pending_state["updateAvailable"] is False


async def test_refresh_uses_etag_and_preserves_last_good_snapshot(tmp_path) -> None:
    requests: list[httpx.Request] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        requests.append(request)
        if calls == 1:
            return httpx.Response(200, json=_catalog_payload(), headers={"content-type": "application/json", "etag": '"v1"'})
        assert request.headers.get("if-none-match") == '"v1"'
        return httpx.Response(304)

    catalog = ModelsDevCatalog(
        tmp_path / "models-dev",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    first = await catalog.refresh()
    second = await catalog.refresh()

    assert first["ok"] is True and first["status"] == "updated"
    assert second["ok"] is True and second["status"] == "not_modified"
    assert catalog.get_model("acme", "demo/v1") is not None
    assert len(requests) == 2


async def test_status_only_reports_an_active_refresh_not_a_sleeping_periodic_task(tmp_path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(
            200,
            json=_catalog_payload(),
            headers={"content-type": "application/json", "etag": '"v1"'},
        )

    catalog = ModelsDevCatalog(
        tmp_path / "models-dev",
        refresh_interval_s=3600,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await catalog.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        assert catalog.status()["refreshing"] is True

        release.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if catalog.status()["fetchedAt"]:
                break
        status = catalog.status()
        assert status["fetchedAt"] > 0
        # The periodic worker remains alive until shutdown, but it is sleeping,
        # not refreshing the catalog.
        assert status["refreshing"] is False
        assert catalog._refresh_task is not None
        assert catalog._refresh_task.done() is False
    finally:
        release.set()
        await catalog.stop()
