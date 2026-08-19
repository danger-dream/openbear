from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Config
from app.config_store import ConfigStore
from app.db.dao import MessageDAO
from app.db.engine import DB
from app.llm.base import AgentResult
from app.llm.events import Usage
from app.restart_notify import restart_notification_path
from app.web_admin import WebAdminServer


def _raw_cfg() -> dict:
    return {
        "telegram": {"botToken": "t", "whitelistIds": [123]},
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://openai.local/v1",
                    "apiKey": "sk-openai-secret",
                    "protocol": "chat",
                    "models": [
                        {"id": "gpt", "name": "GPT", "reasoning": True, "contextWindow": 400000, "maxTokens": 8192},
                        {"id": "mini", "name": "Mini"},
                    ],
                },
                "anthropic": {
                    "baseUrl": "http://anthropic.local",
                    "apiKey": "sk-anthropic-secret",
                    "protocol": "anthropic",
                    "models": [{"id": "claude", "name": "Claude"}],
                },
            },
            "primary": "openai/gpt",
            "compressionModels": ["anthropic/claude"],
        },
        "memory": {"provider": "external", "baseUrl": "http://memory", "identity": "openbear", "accessKey": "memory-secret", "timeoutS": 8},
        "mcp": {
            "enabled": True,
            "defaultApproval": "ask",
            "servers": {
                "playwright": {
                    "enabled": True,
                    "transport": "stdio",
                    "command": "playwright-mcp",
                    "approval": "ask",
                }
            },
        },
        "web": {"enabled": True, "host": "127.0.0.1", "port": 18961, "sessionDays": 30},
    }


class FakeBot:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, reply_markup=reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


class FakeLifecycleMCP:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.draining: set[str] = set()

    async def begin_server_uninstall(self, server: str) -> tuple[bool, int]:
        if self.busy or server in self.draining:
            return False, 1 if self.busy else 0
        self.draining.add(server)
        return True, 0

    async def end_server_uninstall(self, server: str) -> None:
        self.draining.discard(server)


class FakeProbeBackend:
    def __init__(self) -> None:
        self.calls = []

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=32, **opts):
        self.calls.append({"messages": messages, "model": model, "max_tokens": max_tokens, **opts})
        return AgentResult(text="OK", usage=Usage(input_tokens=10, output_tokens=1))


class FakeProbeFactory:
    def __init__(self) -> None:
        self.backend = FakeProbeBackend()

    def backend_for(self, fullname: str):
        return self.backend, fullname.split("/", 1)[1], 32


@pytest.fixture
async def admin_env(tmp_path):
    cfg_path = tmp_path / "openbear.json"
    raw = _raw_cfg()
    raw["storage"] = {"dbPath": str(tmp_path / "t.db")}
    cfg_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(cfg_path)
    cfg = Config.model_validate(json.loads(cfg_path.read_text(encoding="utf-8")))
    db = DB(str(tmp_path / "t.db"))
    await db.connect()
    bot = FakeBot()

    async def reload_mcp():
        return {"ok": True, "enabled": True, "changed": True, "reloaded": True, "summary": {"serverCount": 1, "visibleTools": 1}}

    server = WebAdminServer(
        cfg,
        db,
        bot,
        config_store=store,
        llm_factory=FakeProbeFactory(),
        mcp_reload_hook=reload_mcp,
    )  # type: ignore[arg-type]
    await server.ensure_secret_key()
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    key = await server.get_secret_key()
    start = await client.post("/api/auth/login/start", json={"secret": key})
    req_uuid = (await start.json())["requestUuid"]
    await server.decide_login_request(req_uuid, approved=True, decided_by=123)
    approved = await client.post(f"/api/auth/login/consume/{req_uuid}")
    cookie = approved.cookies["openbear_web_session"].value
    try:
        yield SimpleNamespace(db=db, server=server, client=client, cookie={"openbear_web_session": cookie}, cfg_path=cfg_path)
    finally:
        await client.close()
        await db.close()


async def test_web_task_notification_test_uses_logged_in_telegram_owner(admin_env, monkeypatch):
    called = []

    async def fake_send_test(chat_id: int) -> int:
        called.append(chat_id)
        return 88

    monkeypatch.setattr(admin_env.server.web_task_telegram, "send_test", fake_send_test)
    response = await admin_env.client.post(
        "/api/settings/web-task-notifications/test",
        cookies=admin_env.cookie,
    )
    assert response.status == 200
    assert await response.json() == {"ok": True, "messageId": 88}
    assert called == [123]


async def test_web_settings_specs_get_and_patch_masks_sensitive_values(admin_env):
    specs = await admin_env.client.get("/api/settings/specs", cookies=admin_env.cookie)
    assert specs.status == 200
    specs_data = await specs.json()
    assert "memory" in [g["key"] for g in specs_data["groups"]]
    assert [domain["key"] for domain in specs_data["domains"]] == [
        "agent", "tools", "memory", "media", "web", "interface",
    ]
    agent_sections = next(domain for domain in specs_data["domains"] if domain["key"] == "agent")["sections"]
    assert [section["key"] for section in agent_sections] == [
        "agent", "retry", "timeouts", "compact", "rath", "rath_prompts",
    ]
    assert "agent.retryMaxDelayS" in next(section for section in agent_sections if section["key"] == "retry")["paths"]
    assert specs_data["specs"]["memory.accessKey"]["sensitive"] is True
    notification_spec = specs_data["specs"]["web.taskNotifications.events"]
    assert notification_spec["kind"] == "multi"
    assert {item["value"] for item in notification_spec["choices"]} >= {"task_completed", "retrying"}

    current = await admin_env.client.get("/api/settings", cookies=admin_env.cookie)
    assert current.status == 200
    data = await current.json()
    assert data["values"]["memory.accessKey"] == "memory-secret"
    assert data["masked"]["memory.accessKey"] is False
    assert data["usingBuiltin"]["rath.planDraftPrompt"] is True
    assert specs_data["specs"]["rath.planDraftPrompt"]["editor"] == "prompt"
    assert specs_data["specs"]["rath.planDraftPrompt"]["variables"] == ["task", "plan_schema"]

    preview = await admin_env.client.post(
        "/api/settings/prompt-preview",
        cookies=admin_env.cookie,
        json={"path": "rath.planDraftPrompt", "value": "任务={task}; schema={plan_schema}"},
    )
    assert preview.status == 200
    assert "任务=审查项目配置" in (await preview.json())["rendered"]
    invalid_preview = await admin_env.client.post(
        "/api/settings/prompt-preview",
        cookies=admin_env.cookie,
        json={"path": "rath.planDraftPrompt", "value": "{unknown}"},
    )
    assert invalid_preview.status == 400

    custom_prompt = await admin_env.client.patch(
        "/api/settings/rath.planDraftPrompt",
        cookies=admin_env.cookie,
        json={"value": "任务={task}; schema={plan_schema}"},
    )
    assert custom_prompt.status == 200
    restored_prompt = await admin_env.client.patch(
        "/api/settings/rath.planDraftPrompt",
        cookies=admin_env.cookie,
        json={"value": ""},
    )
    assert restored_prompt.status == 200
    refreshed = await admin_env.client.get("/api/settings", cookies=admin_env.cookie)
    refreshed_data = await refreshed.json()
    assert refreshed_data["usingBuiltin"]["rath.planDraftPrompt"] is True

    def saved_access_key() -> str:
        return json.loads(admin_env.cfg_path.read_text(encoding="utf-8"))["memory"]["accessKey"]

    assert saved_access_key() == "memory-secret"
    revision_before_noops = refreshed_data["revision"]
    old_mask_echo = "memo***cret"
    for noop_value in (None, "", "   ", "sk-***-placeholder", "sk-••-placeholder", old_mask_echo):
        noop = await admin_env.client.patch("/api/settings/memory.accessKey", cookies=admin_env.cookie, json={"value": noop_value})
        assert noop.status == 200
        noop_data = await noop.json()
        assert noop_data["value"] == "memory-secret"
        assert noop_data["revision"] == revision_before_noops
        assert saved_access_key() == "memory-secret"
        assert admin_env.server.config.memory.access_key == "memory-secret"

    secret_patch = await admin_env.client.patch("/api/settings/memory.accessKey", cookies=admin_env.cookie, json={"value": "memory-new-secret"})
    assert secret_patch.status == 200
    secret_data = await secret_patch.json()
    assert secret_data["value"] == "memory-new-secret"
    assert secret_data["revision"] == revision_before_noops + 1
    assert saved_access_key() == "memory-new-secret"
    assert admin_env.server.config.memory.access_key == "memory-new-secret"

    patched = await admin_env.client.patch("/api/settings/memory.timeoutS", cookies=admin_env.cookie, json={"value": 12})
    assert patched.status == 200
    assert (await patched.json())["value"] == 12.0
    assert admin_env.server.config.memory.timeout_s == 12

    event_patch = await admin_env.client.patch(
        "/api/settings/web.taskNotifications.events",
        cookies=admin_env.cookie,
        json={"value": ["task_started", "retrying", "task_completed"]},
    )
    assert event_patch.status == 200
    assert (await event_patch.json())["value"] == ["task_started", "retrying", "task_completed"]
    assert admin_env.server.config.web.task_notifications.events == ["task_started", "retrying", "task_completed"]

    bad_event = await admin_env.client.patch(
        "/api/settings/web.taskNotifications.events",
        cookies=admin_env.cookie,
        json={"value": ["task_completed", "secrets_dumped"]},
    )
    assert bad_event.status == 400

    bad = await admin_env.client.patch("/api/settings/models.primary", cookies=admin_env.cookie, json={"value": "openai/mini"})
    assert bad.status == 400


async def test_web_mcp_server_approval_update_persists_and_validates(admin_env):
    updated = await admin_env.client.patch(
        "/api/mcp/servers/playwright/approval",
        cookies=admin_env.cookie,
        json={"approval": "allow"},
    )
    assert updated.status == 200
    payload = await updated.json()
    assert payload["ok"] is True
    raw = json.loads(admin_env.cfg_path.read_text(encoding="utf-8"))
    assert raw["mcp"]["servers"]["playwright"]["approval"] == "allow"

    invalid = await admin_env.client.patch(
        "/api/mcp/servers/playwright/approval",
        cookies=admin_env.cookie,
        json={"approval": "sometimes"},
    )
    assert invalid.status == 400
    assert (await invalid.json())["error"] == "approval_invalid"

    missing = await admin_env.client.patch(
        "/api/mcp/servers/missing/approval",
        cookies=admin_env.cookie,
        json={"approval": "allow"},
    )
    assert missing.status == 404


async def test_web_mcp_server_uninstall_requires_exact_name_and_hot_reloads(admin_env):
    manager = FakeLifecycleMCP()
    admin_env.server.mcp = manager

    mismatch = await admin_env.client.post(
        "/api/mcp/servers/playwright/uninstall",
        cookies=admin_env.cookie,
        json={"confirm": True, "name": "wrong"},
    )
    assert mismatch.status == 400
    assert "playwright" in json.loads(admin_env.cfg_path.read_text(encoding="utf-8"))["mcp"]["servers"]

    removed = await admin_env.client.post(
        "/api/mcp/servers/playwright/uninstall",
        cookies=admin_env.cookie,
        json={"confirm": True, "name": "playwright"},
    )
    assert removed.status == 200
    payload = await removed.json()
    assert payload["uninstalled"] is True
    assert payload["sensitiveConfigHidden"] is True
    assert "playwright" not in json.loads(admin_env.cfg_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert manager.draining == set()


async def test_web_mcp_server_uninstall_rolls_back_config_when_reload_fails(admin_env):
    manager = FakeLifecycleMCP()
    admin_env.server.mcp = manager
    calls = 0

    async def reload_with_recovery():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"ok": False, "error": "mcp_reload_failed"}
        return {"ok": True, "enabled": True, "reloaded": True, "summary": {"serverCount": 1}}

    admin_env.server._mcp_reload_hook = reload_with_recovery
    response = await admin_env.client.post(
        "/api/mcp/servers/playwright/uninstall",
        cookies=admin_env.cookie,
        json={"confirm": True, "name": "playwright"},
    )

    assert response.status == 400
    payload = await response.json()
    assert payload["error"] == "mcp_reload_failed_rolled_back"
    assert payload["runtimeRestored"] is True
    assert "playwright" in json.loads(admin_env.cfg_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert calls == 2
    assert manager.draining == set()


async def test_web_mcp_server_uninstall_does_not_overwrite_concurrent_config_change(admin_env):
    manager = FakeLifecycleMCP()
    admin_env.server.mcp = manager

    async def reload_after_concurrent_write():
        await admin_env.server.config_store.update_path("ui.showTurnStats", False)
        return {"ok": False, "error": "mcp_reload_failed"}

    admin_env.server._mcp_reload_hook = reload_after_concurrent_write
    response = await admin_env.client.post(
        "/api/mcp/servers/playwright/uninstall",
        cookies=admin_env.cookie,
        json={"confirm": True, "name": "playwright"},
    )

    assert response.status == 409
    assert (await response.json())["error"] == "config_rollback_conflict"
    raw = json.loads(admin_env.cfg_path.read_text(encoding="utf-8"))
    assert raw["ui"]["showTurnStats"] is False
    assert "playwright" not in raw["mcp"]["servers"]
    assert manager.draining == set()


async def test_web_mcp_server_uninstall_refuses_active_call(admin_env):
    admin_env.server.mcp = FakeLifecycleMCP(busy=True)
    response = await admin_env.client.post(
        "/api/mcp/servers/playwright/uninstall",
        cookies=admin_env.cookie,
        json={"confirm": True, "name": "playwright"},
    )
    assert response.status == 409
    assert (await response.json())["error"] == "mcp_server_busy"
    assert "playwright" in json.loads(admin_env.cfg_path.read_text(encoding="utf-8"))["mcp"]["servers"]


async def test_web_channels_list_detail_and_primary_switch(admin_env):
    conv = await admin_env.server._create_web_conversation(123, title="stats", model="openai/gpt")
    await MessageDAO(admin_env.db).add_model_call(
        int(conv["internal_chat_id"]), model="openai/gpt", status="ok", model_call_count=2, model_ok_count=2,
        usage=Usage(input_tokens=100, output_tokens=20, cache_read_tokens=30, cache_write_tokens=5),
        cost_usd=0.75, total_time_ms=2000, peak_tps=15,
    )
    await MessageDAO(admin_env.db).add_model_call(
        123, model="anthropic/claude", status="error", model_call_count=1, model_ok_count=0, model_fail_count=1,
        usage=Usage(input_tokens=50, output_tokens=5), cost_usd=0.25, total_time_ms=1000, peak_tps=8,
    )
    resp = await admin_env.client.get("/api/channels", cookies=admin_env.cookie)
    assert resp.status == 200
    data = await resp.json()
    assert data["primaryModel"] == "openai/gpt"
    assert data["compressionModels"] == ["anthropic/claude"]
    assert data["compressionCandidates"] == [{
        "fullname": "anthropic/claude",
        "provider": "anthropic",
        "id": "claude",
        "name": "Claude",
    }]
    openai = next(p for p in data["providers"] if p["name"] == "openai")
    assert openai["apiKeyMasked"] != "sk-openai-secret"
    assert openai["modelCount"] == 2
    assert openai["stats"]["calls"] == 2
    anthropic = next(p for p in data["providers"] if p["name"] == "anthropic")
    assert anthropic["stats"]["calls"] == 1
    overview = data["overview"]
    assert overview["stats"]["calls"] == 3
    assert overview["stats"]["ok_count"] == 2
    assert overview["stats"]["fail_count"] == 1
    assert overview["stats"]["input_tokens"] == 150
    assert overview["stats"]["output_tokens"] == 25
    assert overview["stats"]["cache_read_tokens"] == 30
    assert overview["stats"]["cache_write_tokens"] == 5
    assert overview["stats"]["cost_usd"] == pytest.approx(1.0)
    assert overview["stats"]["avg_tps"] == pytest.approx(25 / 3)
    assert overview["stats"]["peak_tps"] == pytest.approx(15)
    assert "topModels" not in overview

    detail = await admin_env.client.get("/api/channels/openai", cookies=admin_env.cookie)
    assert detail.status == 200
    detail_data = await detail.json()
    assert detail_data["compressionCandidates"] == data["compressionCandidates"]
    provider = detail_data["provider"]
    assert [m["id"] for m in provider["models"]] == ["gpt", "mini"]
    assert next(m for m in provider["models"] if m["id"] == "gpt")["stats"]["calls"] == 2

    switch = await admin_env.client.post("/api/channels/primary", cookies=admin_env.cookie, json={"model": "openai/mini"})
    assert switch.status == 200
    assert admin_env.server.config.models.primary == "openai/mini"


async def test_web_channels_rename_provider_rewrites_history_stats(admin_env):
    conv = await admin_env.server._create_web_conversation(123, title="rename-stats", model="openai/gpt")
    internal = int(conv["internal_chat_id"])
    dao = MessageDAO(admin_env.db)
    await dao.add_model_call(
        internal,
        model="openai/gpt",
        status="ok",
        model_call_count=3,
        model_ok_count=3,
        usage=Usage(input_tokens=90, output_tokens=30),
        cost_usd=1.5,
        total_time_ms=3000,
        peak_tps=20,
    )
    await dao.add_usage(
        internal,
        usage=Usage(input_tokens=90, output_tokens=30),
        model="openai/gpt",
        protocol="chat",
        total_time_ms=3000,
        commit=True,
    )

    rename = await admin_env.client.patch(
        "/api/channels/openai",
        cookies=admin_env.cookie,
        json={"name": "OpenAI"},
    )
    assert rename.status == 200
    assert "OpenAI" in admin_env.server.config.models.providers
    assert "openai" not in admin_env.server.config.models.providers
    assert admin_env.server.config.models.primary == "OpenAI/gpt"

    cur = await admin_env.db.conn.execute(
        "SELECT model FROM model_calls WHERE chat_id=? ORDER BY id",
        (internal,),
    )
    assert [row["model"] for row in await cur.fetchall()] == ["OpenAI/gpt"]
    cur = await admin_env.db.conn.execute(
        "SELECT model FROM web_conversations WHERE conversation_uuid=?",
        (conv["conversation_uuid"],),
    )
    conv_row = await cur.fetchone()
    assert conv_row["model"] == "OpenAI/gpt"
    cur = await admin_env.db.conn.execute(
        "SELECT last_model FROM sessions WHERE chat_id=?",
        (internal,),
    )
    session_row = await cur.fetchone()
    assert session_row["last_model"] == "OpenAI/gpt"

    listing = await admin_env.client.get("/api/channels", cookies=admin_env.cookie)
    assert listing.status == 200
    data = await listing.json()
    openai = next(p for p in data["providers"] if p["name"] == "OpenAI")
    assert openai["stats"]["calls"] == 3
    assert openai["stats"]["cost_usd"] == pytest.approx(1.5)

    detail = await admin_env.client.get("/api/channels/OpenAI", cookies=admin_env.cookie)
    assert detail.status == 200
    provider = (await detail.json())["provider"]
    gpt = next(m for m in provider["models"] if m["id"] == "gpt")
    assert gpt["stats"]["calls"] == 3


async def test_models_dev_sync_applies_only_the_previewed_metadata_version(admin_env):
    class MutableCatalog:
        def __init__(self) -> None:
            self.record = {
                "id": "demo/v1",
                "name": "Demo V1",
                "limit": {"context": 1_000_000, "output": 32_768},
                "modalities": {"input": ["text"], "output": ["text"]},
            }

        def status(self) -> dict:
            return {
                "available": True,
                "refreshing": False,
                "etag": '"fixture"',
                "sha256": "fixture-catalog",
                "fetchedAt": 1,
                "checkedAt": 1,
            }

        def get_model(self, provider_id: str, model_id: str):
            if (provider_id, model_id) != ("acme", "demo/v1"):
                return None
            return json.loads(json.dumps(self.record))

    catalog = MutableCatalog()
    admin_env.server.models_dev_catalog = catalog
    source = {"providerId": "acme", "modelId": "demo/v1"}

    preview_response = await admin_env.client.post(
        "/api/channels/openai/models/gpt/models-dev/preview",
        cookies=admin_env.cookie,
        json=source,
    )
    assert preview_response.status == 200
    preview = await preview_response.json()
    assert preview["metadataSha256"]

    missing_preview = await admin_env.client.post(
        "/api/channels/openai/models/gpt/models-dev/sync",
        cookies=admin_env.cookie,
        json=source,
    )
    assert missing_preview.status == 400
    assert (await missing_preview.json())["code"] == "models_dev_preview_required"

    # Simulate a catalog refresh between the user seeing the diff and clicking
    # confirm.  The stale request must not mutate either public fields or source.
    catalog.record["limit"]["context"] = 2_000_000
    stale = await admin_env.client.post(
        "/api/channels/openai/models/gpt/models-dev/sync",
        cookies=admin_env.cookie,
        json={**source, "metadataSha256": preview["metadataSha256"]},
    )
    assert stale.status == 409
    stale_data = await stale.json()
    assert stale_data["code"] == "models_dev_preview_stale"

    detail = await admin_env.client.get("/api/channels/openai", cookies=admin_env.cookie)
    gpt = next(model for model in (await detail.json())["provider"]["models"] if model["id"] == "gpt")
    assert gpt["contextWindow"] == 400_000
    assert gpt["modelsDev"]["bound"] is False

    fresh_response = await admin_env.client.post(
        "/api/channels/openai/models/gpt/models-dev/preview",
        cookies=admin_env.cookie,
        json=source,
    )
    fresh = await fresh_response.json()
    applied = await admin_env.client.post(
        "/api/channels/openai/models/gpt/models-dev/sync",
        cookies=admin_env.cookie,
        json={**source, "metadataSha256": fresh["metadataSha256"]},
    )
    assert applied.status == 200

    detail = await admin_env.client.get("/api/channels/openai", cookies=admin_env.cookie)
    gpt = next(model for model in (await detail.json())["provider"]["models"] if model["id"] == "gpt")
    assert gpt["contextWindow"] == 2_000_000
    assert gpt["modelsDev"]["providerId"] == "acme"
    assert gpt["modelsDev"]["modelId"] == "demo/v1"


async def test_models_dev_batch_matches_and_syncs_selected_models_atomically(admin_env):
    class BatchCatalog:
        def __init__(self) -> None:
            self.record = {
                "id": "gpt",
                "name": "GPT Batch",
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["none", "low", "high"]}],
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 1_000_000, "output": 32_768},
                "cost": {
                    "input": 1,
                    "output": 4,
                    "tiers": [{"tier": {"type": "context", "size": 272_000}, "input": 2, "output": 8}],
                },
            }

        def status(self) -> dict:
            return {"available": True, "refreshing": False, "etag": '"fixture"', "sha256": "fixture-catalog", "fetchedAt": 1, "checkedAt": 1}

        def list_model_sources(self, model_id: str):
            if model_id == "gpt":
                return [
                    {"providerId": "first", "providerName": "First", "modelId": "gpt", "modelName": "GPT Batch"},
                    {"providerId": "second", "providerName": "Second", "modelId": "gpt", "modelName": "GPT Batch"},
                ]
            if model_id == "mini":
                return [{"providerId": "first", "providerName": "First", "modelId": "mini", "modelName": "Mini"}]
            return []

        def get_model(self, provider_id: str, model_id: str):
            if (provider_id, model_id) != ("second", "gpt"):
                return None
            return json.loads(json.dumps(self.record))

    admin_env.server.models_dev_catalog = BatchCatalog()
    matches_response = await admin_env.client.get("/api/channels/openai/models-dev/matches", cookies=admin_env.cookie)
    assert matches_response.status == 200
    matches = await matches_response.json()
    gpt_match = next(item for item in matches["items"] if item["modelId"] == "gpt")
    assert [item["providerId"] for item in gpt_match["candidates"]] == ["first", "second"]

    selected = [{"localModelId": "gpt", "source": {"providerId": "second", "modelId": "gpt"}}]
    preview_response = await admin_env.client.post(
        "/api/channels/openai/models-dev/preview",
        cookies=admin_env.cookie,
        json={"items": selected},
    )
    assert preview_response.status == 200
    preview = await preview_response.json()
    item = preview["items"][0]
    assert item["metadata"]["compactTriggerTokens"] == 272_000
    assert item["current"]["contextWindow"] == 400_000

    # A refreshed record must not let a stale batch preview silently write a
    # different set of metadata than the one the user saw.
    admin_env.server.models_dev_catalog.record["limit"]["context"] = 2_000_000
    stale = await admin_env.client.post(
        "/api/channels/openai/models-dev/sync",
        cookies=admin_env.cookie,
        json={"items": [{**selected[0], "metadataSha256": item["metadataSha256"]}]},
    )
    assert stale.status == 409
    assert (await stale.json())["code"] == "models_dev_preview_stale"

    fresh_response = await admin_env.client.post(
        "/api/channels/openai/models-dev/preview",
        cookies=admin_env.cookie,
        json={"items": selected},
    )
    fresh = await fresh_response.json()
    applied = await admin_env.client.post(
        "/api/channels/openai/models-dev/sync",
        cookies=admin_env.cookie,
        json={"items": [{**selected[0], "metadataSha256": fresh["items"][0]["metadataSha256"]}]},
    )
    assert applied.status == 200
    detail = await admin_env.client.get("/api/channels/openai", cookies=admin_env.cookie)
    gpt = next(model for model in (await detail.json())["provider"]["models"] if model["id"] == "gpt")
    assert gpt["modelsDev"]["providerId"] == "second"
    assert gpt["compactTriggerTokens"] == 272_000
    assert gpt["contextWindow"] == 2_000_000


async def test_web_channels_rename_model_rewrites_history_stats(admin_env):
    conv = await admin_env.server._create_web_conversation(123, title="rename-model", model="openai/mini")
    internal = int(conv["internal_chat_id"])
    await MessageDAO(admin_env.db).add_model_call(
        internal,
        model="openai/mini",
        status="ok",
        model_call_count=2,
        model_ok_count=2,
        usage=Usage(input_tokens=40, output_tokens=10),
        cost_usd=0.4,
        total_time_ms=1000,
    )

    rename = await admin_env.client.patch(
        "/api/channels/openai/models/mini",
        cookies=admin_env.cookie,
        json={"id": "mini2"},
    )
    assert rename.status == 200
    assert admin_env.server.config.models.resolve("openai/mini2") is not None
    assert admin_env.server.config.models.resolve("openai/mini") is None

    cur = await admin_env.db.conn.execute(
        "SELECT model FROM model_calls WHERE chat_id=?",
        (internal,),
    )
    assert [row["model"] for row in await cur.fetchall()] == ["openai/mini2"]

    detail = await admin_env.client.get("/api/channels/openai", cookies=admin_env.cookie)
    assert detail.status == 200
    provider = (await detail.json())["provider"]
    mini2 = next(m for m in provider["models"] if m["id"] == "mini2")
    assert mini2["stats"]["calls"] == 2


async def test_web_channels_update_model_and_guard_deleting_compression(admin_env):
    patch = await admin_env.client.patch("/api/channels/openai/models/mini", cookies=admin_env.cookie, json={"id": "mini2", "contextWindow": 64000, "compactTriggerTokens": 250000, "cost": {"input": 1.5, "output": 6}})
    assert patch.status == 200
    resolved = admin_env.server.config.models.resolve("openai/mini2")
    assert resolved is not None
    assert resolved[1].compact_trigger_tokens == 250000
    detail = await admin_env.client.get("/api/channels/openai", cookies=admin_env.cookie)
    provider = (await detail.json())["provider"]
    assert next(m for m in provider["models"] if m["id"] == "mini2")["compactTriggerTokens"] == 250000

    delete_compression = await admin_env.client.delete("/api/channels/anthropic/models/claude", cookies=admin_env.cookie)
    assert delete_compression.status == 400
    assert "压缩" in (await delete_compression.json())["error"]

    set_multi = await admin_env.client.post("/api/channels/compression", cookies=admin_env.cookie, json={"models": ["anthropic/claude", "openai/gpt"]})
    assert set_multi.status == 200
    assert admin_env.server.config.models.compression_models == ["anthropic/claude", "openai/gpt"]
    ordered = await admin_env.client.get("/api/channels", cookies=admin_env.cookie)
    assert [item["fullname"] for item in (await ordered.json())["compressionCandidates"]] == [
        "anthropic/claude",
        "openai/gpt",
    ]

    clear_compression = await admin_env.client.post("/api/channels/compression", cookies=admin_env.cookie, json={"models": []})
    assert clear_compression.status == 200
    delete_model = await admin_env.client.delete("/api/channels/anthropic/models/claude", cookies=admin_env.cookie)
    assert delete_model.status == 200


async def test_web_channels_update_default_thinking_level_only(admin_env):
    prepared = await admin_env.client.patch(
        "/api/channels/openai/models/gpt",
        cookies=admin_env.cookie,
        json={"thinkingLevels": ["off", "low", "high"], "defaultThinkingLevel": "high"},
    )
    assert prepared.status == 200

    changed = await admin_env.client.patch(
        "/api/channels/openai/models/gpt",
        cookies=admin_env.cookie,
        json={"defaultThinkingLevel": "low"},
    )
    assert changed.status == 200

    resolved = admin_env.server.config.models.resolve("openai/gpt")
    assert resolved is not None
    assert resolved[1].thinking_levels == ["off", "low", "high"]
    assert resolved[1].default_thinking_level == "low"

    detail = await admin_env.client.get("/api/channels/openai", cookies=admin_env.cookie)
    gpt = next(model for model in (await detail.json())["provider"]["models"] if model["id"] == "gpt")
    assert gpt["thinkingLevels"] == ["off", "low", "high"]
    assert gpt["defaultThinkingLevel"] == "low"


async def test_web_channels_fetch_models_from_base_url(admin_env):
    async def models_handler(request):
        assert request.headers.get("Authorization") == "Bearer sk-fetch"
        return web.json_response({"data": [{"id": "gpt-4.1", "name": "GPT 4.1"}, {"id": "mini"}]})

    app = web.Application()
    app.router.add_get("/models", models_handler)
    upstream = TestServer(app)
    await upstream.start_server()
    try:
        resp = await admin_env.client.post("/api/channels/models/fetch", cookies=admin_env.cookie, json={
            "baseUrl": str(upstream.make_url("/v1")),
            "apiKey": "sk-fetch",
            "protocol": "chat",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["models"] == [
            {"id": "gpt-4.1", "name": "GPT 4.1"},
            {"id": "mini", "name": "mini"},
        ]
        assert data["endpoint"].endswith("/models")
        assert not data["endpoint"].endswith("/v1/models")
    finally:
        await upstream.close()


async def test_web_channels_fetch_models_falls_back_to_v1_models(admin_env):
    async def root_missing(_request):
        return web.json_response({"error": "not found"}, status=404)

    async def v1_models(request):
        assert request.headers.get("Authorization") == "Bearer sk-fetch"
        return web.json_response({"data": [{"id": "fallback-model"}]})

    app = web.Application()
    app.router.add_get("/models", root_missing)
    app.router.add_get("/v1/models", v1_models)
    upstream = TestServer(app)
    await upstream.start_server()
    try:
        resp = await admin_env.client.post("/api/channels/models/fetch", cookies=admin_env.cookie, json={
            "baseUrl": str(upstream.make_url("/v1")),
            "apiKey": "sk-fetch",
            "protocol": "chat",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["models"] == [{"id": "fallback-model", "name": "fallback-model"}]
        assert data["endpoint"].endswith("/v1/models")
    finally:
        await upstream.close()


async def test_web_channels_fetch_models_reuses_saved_key_when_blank_in_edit(admin_env):
    async def root_missing(_request):
        return web.json_response({"error": "not found"}, status=404)

    async def v1_models(request):
        assert request.headers.get("Authorization") == "Bearer sk-openai-secret"
        return web.json_response({"data": [{"id": "parrot-model"}]})

    app = web.Application()
    app.router.add_get("/models", root_missing)
    app.router.add_get("/v1/models", v1_models)
    upstream = TestServer(app)
    await upstream.start_server()
    try:
        resp = await admin_env.client.post("/api/channels/models/fetch", cookies=admin_env.cookie, json={
            "name": "openai",
            "baseUrl": str(upstream.make_url("/v1")),
            "apiKey": "",
            "protocol": "chat",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["models"] == [{"id": "parrot-model", "name": "parrot-model"}]
        assert data["endpoint"].endswith("/v1/models")
    finally:
        await upstream.close()


async def test_web_channels_create_and_reorder_provider(admin_env):
    create = await admin_env.client.post("/api/channels", cookies=admin_env.cookie, json={
        "name": "zai",
        "baseUrl": "https://zai.example/v1",
        "apiKey": "sk-zai",
        "protocol": "chat",
        "models": [{"id": "glm", "name": "GLM", "reasoning": True}],
    })
    assert create.status == 200
    assert admin_env.server.config.models.resolve("zai/glm") is not None

    reorder = await admin_env.client.post("/api/channels/reorder", cookies=admin_env.cookie, json={"order": ["zai", "openai", "anthropic"]})
    assert reorder.status == 200
    assert list(admin_env.server.config.models.providers.keys())[:2] == ["zai", "openai"]

    options = await admin_env.client.get("/api/rath/options", cookies=admin_env.cookie)
    assert options.status == 200
    model_providers = list(dict.fromkeys(item["provider"] for item in (await options.json())["models"]))
    assert model_providers == ["zai", "openai", "anthropic"]


async def test_web_system_restart_api(monkeypatch, admin_env):
    status = await admin_env.client.get("/api/system/status", cookies=admin_env.cookie)
    assert status.status == 404

    scheduled = []

    async def fake_restart(*, delay_s: float = 1.0):
        scheduled.append(delay_s)

    monkeypatch.setattr("app.control_actions.schedule_openbear_restart", fake_restart)
    restart = await admin_env.client.post("/api/system/restart", cookies=admin_env.cookie, json={"confirm": True, "reason": "test"})
    assert restart.status == 200
    assert (await restart.json())["scheduled"] is True
    assert scheduled == [1.0]
    raw = json.loads(restart_notification_path(admin_env.server.config).read_text(encoding="utf-8"))
    assert raw[0]["chatId"] == 123
    assert raw[0]["reason"] == "test"


async def test_web_channel_model_and_channel_probe(admin_env):
    one = await admin_env.client.post("/api/channels/openai/models/gpt/test", cookies=admin_env.cookie)
    assert one.status == 200
    data = await one.json()
    assert data["result"]["ok"] is True
    assert data["result"]["snippet"] == "OK"

    channel = await admin_env.client.post("/api/channels/openai/test", cookies=admin_env.cookie)
    assert channel.status == 200
    cdata = await channel.json()
    assert cdata["ok"] is True
    assert cdata["provider"] == "openai"
    assert cdata["jobUuid"]
    assert cdata["status"] == "queued"
    assert cdata["done"] == 0
    assert cdata["total"] == 2
    assert "okAll" not in cdata
    assert "results" not in cdata

    status_data = {}
    for _ in range(100):
        status = await admin_env.client.get(f"/api/channels/openai/test/{cdata['jobUuid']}", cookies=admin_env.cookie)
        assert status.status == 200
        status_data = await status.json()
        if status_data["status"] == "completed":
            break
        await asyncio.sleep(0.01)

    assert status_data
    assert status_data["jobUuid"] == cdata["jobUuid"]
    assert status_data["provider"] == "openai"
    assert status_data["status"] == "completed"
    assert status_data["done"] == 2
    assert status_data["total"] == 2
    assert status_data["okAll"] is True
    assert [r["model"] for r in status_data["results"]] == ["openai/gpt", "openai/mini"]
    assert [r["snippet"] for r in status_data["results"]] == ["OK", "OK"]
    rows = await MessageDAO(admin_env.db).recent_model_calls(123)
    assert len(rows) == 3
    assert sum(row.input_tokens for row in rows) == 30
