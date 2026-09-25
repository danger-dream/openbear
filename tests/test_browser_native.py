"""Native Browser contracts and recovery with protocol doubles, never a real browser."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import ValidationError

from app.browser.cdp import CDP
from app.browser.config import BrowserConfig
from app.browser.contract import Request
from app.browser.service import BrowserService
from app.browser.worker import WorkerError
from app.config import Config
from app.settings.specs import GROUPS, SPECS, WEB_DOMAINS
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.browser import register_browser_tool


def config(tmp_path, **browser):
    return Config.model_validate(
        {
            "telegram": {"botToken": "test", "whitelistIds": [1]},
            "models": {"providers": {}, "primary": ""},
            "memory": {"provider": "builtin"},
            "storage": {"dbPath": str(tmp_path / "db.sqlite")},
            "browser": {
                "enabled": True,
                "mainEndpoint": "http://local.invalid:9222?fingerprint=test",
                "recoveryCooldownS": 0,
                **browser,
            },
        }
    )


class FakeCDP:
    def __init__(self, endpoint, timeout=10):
        self.endpoint = endpoint
        self.ws_url = "ws://local/browser/original"
        self.connected = False
        self.targets = {
            "old": {"targetId": "old", "type": "page", "url": "https://old.test", "title": "legacy"}
        }
        self.calls = []
        self.hung = False

    async def connect(self):
        self.connected = True

    async def call(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": list(self.targets.values())}
        if method == "Target.createTarget":
            tid = f"target-{len(self.targets)}"
            self.targets[tid] = {"targetId": tid, "type": "page", "url": "about:blank"}
            return {"targetId": tid}
        if method == "Target.closeTarget":
            self.targets.pop(params["targetId"], None)
            return {"success": True}
        if method == "Browser.getVersion":
            return {"product": "Fake/1"}
        return {}

    async def target_call(self, target, method, params=None, **kwargs):
        self.calls.append((method, target))
        if self.hung:
            raise TimeoutError()
        return {"result": {"value": 1}}

    async def close(self):
        self.connected = False


class FakeWorker:
    def __init__(self, python=None):
        self.connected = False
        self.generation = 0
        self.calls = []
        self.fail = None
        self.fail_action = ""
        self.init_fail = False
        self.effects = 0

    async def start(self, params):
        if self.init_fail:
            raise WorkerError("initialize_failed")
        self.connected = True
        self.generation += 1

    async def call(self, a, p, **kwargs):
        self.calls.append((a, p))
        if a == "act":
            self.effects += 1
        if self.fail and (not self.fail_action or a == self.fail_action):
            raise self.fail
        if a == "prepare":
            return {"ready": True, "url": "about:blank"}
        if a == "capture":
            Path(p["outputPath"]).write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1kAAAAASUVORK5CYII="
                )
            )
            return {"artifact": p["outputPath"], "mime": "image/png"}
        return {"text": "button [ref=s1:e1]", "completed": True}

    async def close(self):
        self.connected = False


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setattr("app.browser.service.CDP", FakeCDP)
    monkeypatch.setattr("app.browser.connection.CDP", FakeCDP)
    monkeypatch.setattr("app.browser.service.Worker", FakeWorker)
    svc = BrowserService(config(tmp_path), str(tmp_path / "workspace"))
    assert (await svc.validate_connection())["ok"]
    yield svc
    await svc.close()


def ctx(owner="a", answer=None):
    async def confirm(_):
        return answer

    return ToolRuntimeContext(
        conversation_uuid=owner, source="web", web_confirm=confirm if answer is not None else None
    )


async def new(svc, owner="a"):
    result = await svc.call({"action": "page", "params": {"op": "new"}}, ctx(owner))
    assert result["status"] == "ok", result
    return result["page"]


def test_configuration_complete_and_boundaries(tmp_path):
    cfg = config(tmp_path)
    assert cfg.browser.main_endpoint == "http://local.invalid:9222?fingerprint=test"
    assert cfg.browser.agent_access is False
    for field in BrowserConfig.model_fields.values():
        path = "browser." + (field.alias or "")
        if not field.alias:
            path = "browser." + next(k for k, v in BrowserConfig.model_fields.items() if v is field)
        assert path in SPECS
        assert sum(path in paths for _, paths in GROUPS.values()) == 1
    assert "browser" in WEB_DOMAINS
    for bad in (
        {"actionTimeoutS": 0},
        {"maxPagesPerOwner": 0},
        {"mainEndpoint": "file:///etc/passwd"},
        {"mainRestartUrl": "ws://browser.invalid/control"},
    ):
        with pytest.raises((ValidationError, KeyError)):
            BrowserConfig.model_validate(bad)


async def test_native_browser_works_with_no_mcp_services(service):
    service.config.mcp.enabled = False
    service.config.mcp.servers.clear()
    reg = ToolRegistry()
    register_browser_tool(reg, service)
    assert reg.names() == ["Browser"]
    result = json.loads(
        await reg.dispatch("Browser", '{"action":"page","params":{"op":"new"}}', context=ctx())
    )
    assert result["status"] == "ok", result
    inst = next(iter(service.instances.values()))
    assert inst.cdp.endpoint == service.cfg.main_endpoint
    result = json.loads(
        await reg.dispatch(
            "Browser", json.dumps({"action": "snapshot", "page": result["page"]}), context=ctx()
        )
    )
    assert result["status"] == "ok", result
    assert [a for a, _ in inst.worker.calls] == ["prepare", "snapshot"]


async def test_missing_browser_endpoint_does_not_fall_back_to_mcp(service):
    from app.config import MCPServerConfig

    service.cfg.main_endpoint = ""
    service.config.mcp.servers["playwright"] = MCPServerConfig(
        args=["--cdp-endpoint", "http://other.invalid:9222"]
    )
    result = await service.call({"action": "page", "params": {"op": "new"}}, ctx())
    assert result["outcome"] == "not_started"
    assert "main_endpoint_missing" in result["error"]
    assert not service.instances


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "act", "params": {"op": "click"}},
        {"action": "act", "params": {"op": "check", "target": "r", "checked": "yes"}},
        {"action": "navigate", "params": {"url": "javascript:alert(1)"}},
        {"action": "navigate", "params": {"url": "file:///etc/passwd"}},
        {"action": "status", "params": {"execute": "anything"}},
        {"action": "wait", "params": {"text": "x", "timeMs": 10}},
        {"action": "files", "params": {"op": "save", "id": -1}},
        {"action": "act", "params": {"op": "emulate", "media": {"unknown": True}}},
        {"action": "dialog", "params": {"op": "handle"}},
    ],
)
def test_invalid_arguments_rejected_before_any_io(payload):
    with pytest.raises(ValidationError):
        Request.model_validate(payload)


async def test_status_never_launches(service):
    result = await service.call({"action": "status"}, ctx())
    assert result["instances"] == []
    assert not service.instances


async def test_limits_hot_update_without_reconnect_or_replay(service):
    page = await new(service)
    request = {"action": "snapshot", "page": page}
    assert (await service.call(request, ctx()))["status"] == "ok"
    inst = next(iter(service.instances.values()))
    generation = inst.worker.generation
    updated = service.config.model_copy(deep=True)
    updated.browser.max_event_entries = 37
    updated.browser.snapshot_max_chars = 2345
    service.configure(updated)
    assert (await service.call(request, ctx()))["status"] == "ok"
    assert inst.worker.generation == generation
    updates = [p for a, p in inst.worker.calls if a == "configure"]
    assert len(updates) == 1
    assert updates[0]["maxEventEntries"] == 37
    assert updates[0]["snapshotMaxChars"] == 2345
    assert inst.worker.effects == 0
    assert (await service.call(request, ctx()))["status"] == "ok"
    assert len([a for a, _ in inst.worker.calls if a == "configure"]) == 1


async def test_owner_and_epoch_enforced(service):
    page = await new(service)
    denied = await service.call(
        {"action": "act", "page": page, "params": {"op": "click", "target": "css=button"}}, ctx("b")
    )
    assert denied["outcome"] == "not_started" and "not_owned" in denied["error"]
    inst = next(iter(service.instances.values()))
    assert [a for a, _ in inst.worker.calls] == ["prepare"]
    inst.cdp.ws_url = "ws://local/browser/restarted"
    result = await service.call({"action": "snapshot", "page": page}, ctx())
    assert "expired" in result["error"]
    assert [a for a, _ in inst.worker.calls] == ["prepare"]


async def test_legacy_adoption_respects_confirmation_feedback(service):
    listing = await service.call({"action": "page", "params": {"op": "list"}}, ctx())
    handle = listing["pages"][0]["page"]
    assert listing["pages"][0]["adoptable"]
    result = await service.call(
        {"action": "page", "page": handle, "params": {"op": "adopt"}},
        ctx(answer={"confirmed": True, "text": "先别操作"}),
    )
    assert result["status"] == "denied"
    assert not service.pages[handle].owner
    result = await service.call(
        {"action": "page", "page": handle, "params": {"op": "adopt"}},
        ctx(answer={"confirmed": True}),
    )
    assert result["status"] == "ok"
    assert service.pages[handle].owner == "conversation:a"


async def test_timeout_effect_not_replayed_and_next_mutation_blocked(service):
    page = await new(service)
    inst = next(iter(service.instances.values()))
    inst.worker.fail = TimeoutError()
    args = {"action": "act", "page": page, "params": {"op": "click", "target": "css=button"}}
    result = await service.call(args, ctx())
    assert result["outcome"] == "unknown" and result["replayed"] is False
    assert inst.worker.effects == 1
    result = await service.call(args, ctx())
    assert result["outcome"] == "not_started"
    assert inst.worker.effects == 1


async def test_worker_fatal_does_not_block_control_channel(service):
    page = await new(service)
    inst = next(iter(service.instances.values()))
    await inst.worker.close()
    inst.worker.init_fail = True
    inst.cdp.hung = True
    failed = await service.call({"action": "snapshot", "page": page}, ctx())
    assert failed["outcome"] == "not_started"
    listed = await service.call({"action": "page", "params": {"op": "list"}}, ctx())
    assert listed["status"] == "ok"
    probe = await service.call(
        {"action": "recover", "page": page, "params": {"op": "probe"}}, ctx()
    )
    assert probe["browserResponsive"] and not probe["pageResponsive"]
    result = await service.call(
        {"action": "recover", "page": page, "params": {"op": "close"}},
        ctx(answer={"confirmed": True}),
    )
    assert result["status"] == "ok" and page not in service.pages


async def test_queue_timeout_is_not_started_and_dialog_bypasses_lock(service):
    service.cfg.queue_timeout_s = 0.01
    page = await new(service)
    lock = service.pages[page].lock
    await lock.acquire()
    try:
        result = await service.call({"action": "snapshot", "page": page}, ctx())
        assert result["error"] == "page_queue_timeout" and result["outcome"] == "not_started"
        result = await service.call(
            {"action": "dialog", "page": page, "params": {"op": "inspect"}}, ctx()
        )
        assert result["status"] == "ok"
    finally:
        lock.release()


async def test_snapshot_limits_and_evaluate_switch(service):
    page = await new(service)
    service.cfg.snapshot_max_chars = 1000
    result = await service.call(
        {"action": "snapshot", "page": page, "params": {"maxChars": 60000}}, ctx()
    )
    inst = next(iter(service.instances.values()))
    assert inst.worker.calls[-1][1]["maxChars"] == 1000
    service.cfg.allow_evaluate = False
    result = await service.call(
        {"action": "evaluate", "page": page, "params": {"expression": "1"}}, ctx()
    )
    assert result["outcome"] == "not_started"


async def test_capture_artifact_and_structured_media(service):
    page = await new(service)
    context = ctx()
    result = await service.call(
        {"action": "capture", "page": page, "params": {"view": True}}, context
    )
    assert result["artifact"].startswith("workspace/artifacts/browser/")
    assert "base64" not in json.dumps(result)
    assert context.tool_images[0]["mime_type"] == "image/png"
    assert Path(context.tool_images[0]["path"]).is_file()
    from app.llm.multimodal import (
        to_anthropic_content,
        to_openai_chat_content,
        to_openai_responses_content,
    )

    assert to_openai_chat_content(context.tool_images)[0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert to_openai_responses_content(context.tool_images)[0]["type"] == "input_image"
    assert to_anthropic_content(context.tool_images)[0]["source"]["type"] == "base64"


async def test_page_registry_survives_service_recreation(service):
    page = await new(service)
    replacement = BrowserService(service.config, str(service.workspace), str(service.state_dir))
    try:
        assert replacement.pages[page].owner == "conversation:a"
    finally:
        await replacement.close()


async def test_agent_access_and_service_disable_live_gate(service):
    reg = ToolRegistry()
    register_browser_tool(reg, service)
    assert "Browser" not in reg.names(scope="agent")
    result = await service.call(
        {"action": "status"}, ToolRuntimeContext(agent_session_uuid="agent1")
    )
    assert result["error"] == "browser_agent_access_denied"
    service.cfg.enabled = False
    result = json.loads(await reg.dispatch("Browser", '{"action":"status"}', context=ctx()))
    assert result["error"] == "browser_disabled"


async def test_one_compact_tool_schema(service):
    reg = ToolRegistry()
    register_browser_tool(reg, service)
    assert reg.names() == ["Browser"]
    assert len(json.dumps(reg.schemas())) < 6000


async def test_raw_cdp_protocol_without_browser():
    seen = []

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            data = json.loads(message.data)
            seen.append(data)
            if data["method"] == "hang":
                continue
            result = (
                {"sessionId": "target-session"}
                if data["method"] == "Target.attachToTarget"
                else {"ok": True}
            )
            await ws.send_json({"id": data["id"], "result": result})
        return ws

    app = web.Application()
    app.router.add_get("/ws", socket)
    server = TestServer(app)
    await server.start_server()
    cdp = CDP(str(server.make_url("/ws")).replace("http://", "ws://"), timeout=1)
    try:
        assert (await cdp.target_call("t1", "Runtime.evaluate", {"expression": "1"}))["ok"]
        assert [m["method"] for m in seen] == [
            "Target.attachToTarget",
            "Runtime.evaluate",
            "Target.detachFromTarget",
        ]
        assert seen[1]["sessionId"] == "target-session"
        with pytest.raises(TimeoutError):
            await cdp.call("hang", timeout=0.02)
        assert cdp._pending == {}
        assert (await cdp.call("Browser.getVersion"))["ok"]
        assert all(m["method"] != "Target.setAutoAttach" for m in seen)
    finally:
        await cdp.close()
        await server.close()
