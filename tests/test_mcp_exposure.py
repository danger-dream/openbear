"""MCP publication gates. Only stub peers / temporary databases are used."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import Config
from app.db.engine import DB
from app.mcp.exposure import (
    MCPExposureApprovals,
    MCPExposureNotAuthorized,
    compact,
    exposure_summary,
)
from app.mcp.manager import MCPManager
from app.mcp.permissions import build_tool_meta
from app.mcp.registry import register_mcp_tools
from app.mcp.types import MCPRawTool
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.openbear_control import register_openbear_control_tool
from tests.test_managed_runtime import runtime as _managed_runtime
from tests.test_web_settings_channels_api import admin_env as _admin_env

admin_env = _admin_env
managed_runtime = _managed_runtime


def config(**mcp):
    return Config.model_validate({
        "models": {"providers": {}, "primary": ""}, "memory": {"provider": "builtin"},
        "mcp": {"enabled": True, "toolCountThreshold": 30, "definitionSizeThresholdKB": 64,
                "servers": {"demo": {"command": "stub", "approval": "allow"}}, **mcp},
    })


@pytest.fixture
def peer(monkeypatch):
    state = SimpleNamespace(tools=[MCPRawTool(name=f"read_{i}", description="读取说明") for i in range(31)], calls=0, clients=[])

    class Client:
        instructions = "stub instructions"

        def __init__(self, key, cfg):
            self.key = key
            self.closed = False
            state.clients.append(self)

        def set_notification_handler(self, handler):
            self.handler = handler

        async def connect(self):
            pass

        async def list_tools(self):
            return [row.model_copy(deep=True) for row in state.tools]

        async def list_prompts(self):
            return []

        async def close(self):
            self.closed = True

        async def call_tool(self, *args, **kwargs):
            state.calls += 1
            raise AssertionError("disabled tool must not reach the server")

    monkeypatch.setattr("app.mcp.manager.MCPClient", Client)
    return state


def definitions(cfg, names):
    used = set()
    return [build_tool_meta(cfg.mcp, cfg.mcp.servers["demo"], server_key="demo", raw_tool=MCPRawTool(name=name), used_names=used) for name in names]


async def test_disabled_tools_absent_from_registry_payload_and_call(peer):
    peer.tools = peer.tools[:2]
    cfg = config()
    cfg.mcp.servers["demo"].disabled_tools = ["read_0", "removed_tool"]
    manager = MCPManager(cfg)
    await manager.start()
    try:
        registry = ToolRegistry()
        register_mcp_tools(registry, manager)
        assert registry.names() == ["mcp__demo__read_1"]
        assert [row["name"] for row in registry.schemas()] == ["mcp__demo__read_1"]
        from app.llm.anthropic import AnthropicBackend
        from app.llm.openai_chat import OpenAIChatBackend
        from app.llm.openai_responses import OpenAIResponsesBackend
        for backend_type in (OpenAIChatBackend, OpenAIResponsesBackend, AnthropicBackend):
            backend = backend_type(None, "http://127.0.0.1:9", "unused")
            payload = backend.build_payload([], model="test", tools=registry.schemas())
            names = [(row.get("function") or row)["name"] for row in payload["tools"]]
            assert names == ["mcp__demo__read_1"]
        result = json.loads(await manager.call_tool("mcp__demo__read_0", {}, ToolRuntimeContext()))
        assert result["error"] == "mcp_tool_not_found" and peer.calls == 0
        assert "未知工具" in await registry.dispatch("mcp__demo__read_0", "{}")
    finally:
        await manager.close()


def test_exact_registry_bytes_threshold_boundary_and_zero():
    cfg = config(toolCountThreshold=1, definitionSizeThresholdKB=0)
    tools = definitions(cfg, ["read_a"])
    manager = MCPManager(cfg)
    manager._tools = {row.public_name: row for row in tools}
    registry = ToolRegistry()
    register_mcp_tools(registry, manager)
    summary = exposure_summary(cfg.mcp, "demo", tools)
    assert summary["definitionBytes"] == len(compact(registry.schemas()))
    assert summary["exceeded"] is False
    assert exposure_summary(cfg.mcp, "demo", definitions(cfg, ["a", "b"]))["exceeded"] is True
    cfg.mcp.tool_count_threshold = 0
    cfg.mcp.definition_size_threshold_kb = 2
    tool = definitions(cfg, ["a"])[0]
    initial = exposure_summary(cfg.mcp, "demo", [tool])["definitionBytes"]
    assert initial < 2048
    tool.description += "x" * (2048 - initial)
    assert exposure_summary(cfg.mcp, "demo", [tool])["definitionBytes"] == 2048
    assert not exposure_summary(cfg.mcp, "demo", [tool])["exceeded"]
    tool.description += "中"
    assert exposure_summary(cfg.mcp, "demo", [tool])["definitionBytes"] == 2051
    assert exposure_summary(cfg.mcp, "demo", [tool])["exceeded"]
    cfg.mcp.definition_size_threshold_kb = 0
    assert not exposure_summary(cfg.mcp, "demo", [tool])["exceeded"]


async def test_pending_server_cannot_publish_and_changed_fingerprint_regates(peer):
    manager = MCPManager(config())
    await manager.start()
    try:
        state = manager.status_snapshot().servers[0]
        assert state.status == "awaiting_confirmation"
        assert state.enabled_tool_count == state.total_tool_count == 31
        assert state.tool_count == 0 and not manager.available_tools()
        assert not manager.server_instructions_snapshot()
        registry = ToolRegistry()
        assert register_mcp_tools(registry, manager) == 0
        assert registry.schemas() == []
        await manager.exposure_approvals.approve(manager.exposure_warnings())
        await manager.apply_exposure_approvals()
        assert len(manager.available_tools()) == 31
        before = state.exposure_fingerprint
        peer.tools[0].description += " changed"
        await manager.refresh_server_tools("demo")
        assert state.status == "awaiting_confirmation" and before != state.exposure_fingerprint
        assert not manager.available_tools()
        assert json.loads(await manager.call_tool("mcp__demo__read_0", {}, ToolRuntimeContext()))["error"] == "mcp_tool_not_found"
    finally:
        await manager.close()


async def test_upgrade_migration_and_restart_persistence(tmp_path, peer):
    cfg = config()
    # Old configuration contains neither of the new settings.
    cfg.mcp.__pydantic_fields_set__.difference_update({"tool_count_threshold", "definition_size_threshold_kb"})
    db = DB(str(tmp_path / "approval.db"))
    await db.connect()
    store = MCPExposureApprovals(db)
    await store.initialize(cfg)
    manager = MCPManager(cfg, exposure_approvals=store)
    await manager.start()
    try:
        assert len(manager.available_tools()) == 31
        assert not store.legacy
    finally:
        await manager.close()
    other = MCPExposureApprovals(db)
    await other.initialize(cfg)
    manager = MCPManager(cfg, exposure_approvals=other)
    await manager.start()
    assert len(manager.available_tools()) == 31
    await manager.close()
    peer.tools.append(MCPRawTool(name="new_tool"))
    manager = MCPManager(cfg, exposure_approvals=other)
    await manager.start()
    assert manager.status_snapshot().servers[0].status == "awaiting_confirmation"
    assert not manager.available_tools()
    await manager.close()
    await db.close()


async def test_first_start_explicit_new_settings_does_not_migrate(tmp_path, peer):
    cfg = config()
    db = DB(str(tmp_path / "new.db"))
    await db.connect()
    store = MCPExposureApprovals(db)
    await store.initialize(cfg)
    manager = MCPManager(cfg, exposure_approvals=store)
    await manager.start()
    assert not manager.available_tools()
    await manager.close()
    await db.close()


@pytest.mark.parametrize("answer", [
    {"confirmed": False, "status": "answered", "decision": "cancel"},
    {"confirmed": True, "status": "timeout"},
    {"confirmed": True, "decision": "confirm_with_changes", "requiresAmendment": True, "text": "先减少工具"},
])
async def test_model_gate_denial_timeout_and_amendment_cannot_commit(peer, answer):
    manager = MCPManager(config(enabled=False))
    async def confirm(rows, fingerprint):
        assert rows[0]["enabledTools"] == 31
        assert rows[0]["definitionBytes"] > 0 and fingerprint
        return answer
    with pytest.raises(MCPExposureNotAuthorized):
        await manager.prepare_reload(config(), exposure_confirmation=confirm)
    assert not manager.available_tools() and not manager.exposure_approvals.records
    assert all(client.closed for client in peer.clients)
    await manager.close()


async def test_openbear_control_amendment_receipt_is_bound_to_definition_set(peer):
    manager = MCPManager(config(enabled=False))
    requests = []
    approved_scope = None
    async def confirm(payload):
        nonlocal approved_scope
        requests.append(payload)
        assert "31 / 31" in payload["body"] and "tokens" in payload["body"]
        if payload.get("_approvalId"):
            return {"status": "answered", "confirmed": payload["_approvalScope"] == approved_scope}
        approved_scope = payload["_approvalScope"]
        return {"status": "answered", "confirmed": True, "decision": "confirm_with_changes",
                "authorizationGranted": True, "requiresAmendment": True, "interactionId": "receipt", "text": "先做检查"}
    async def gated_reload(callback):
        try:
            commit, _ = await manager.prepare_reload(config(), exposure_confirmation=callback)
            await commit()
            return {"ok": True}
        except MCPExposureNotAuthorized as exc:
            return {"ok": False, "status": "amendments_required", "confirmation": exc.confirmation}
    registry = ToolRegistry()
    register_openbear_control_tool(registry, SimpleNamespace(reload_mcp_with_confirmation=gated_reload))
    ctx = ToolRuntimeContext(source="web", chat_id=123, web_confirm=confirm)
    result = json.loads(await registry.dispatch("OpenBearControl", '{"action":"mcp_reload"}', context=ctx))
    assert result["status"] == "amendments_required" and result["approvalId"] == "receipt"
    assert not manager.available_tools()
    result = json.loads(await registry.dispatch("OpenBearControl", '{"action":"mcp_reload","_openbearApprovalId":"receipt"}', context=ctx))
    assert result["ok"] and len(manager.available_tools()) == 31
    assert requests[-1]["_approvalId"] == "receipt"
    await manager.close()


async def test_api_enable_and_selection_require_confirmation_without_config_write(admin_env, peer):
    env = admin_env
    live = MCPManager(env.server.config)
    await live.start()
    env.server.mcp = live
    before = env.cfg_path.read_text()
    try:
        response = await env.client.patch("/api/mcp/servers/playwright/tools", json={"disabledTools": ["read_0", "gone"]}, cookies=env.cookie)
        # Reducing to 30 meets both default budgets: no confirmation necessary.
        assert response.status == 200
        assert json.loads(env.cfg_path.read_text())["mcp"]["servers"]["playwright"]["disabledTools"] == ["read_0", "gone"]
        before = env.cfg_path.read_text()
        response = await env.client.patch("/api/mcp/servers/playwright/tools", json={"disabledTools": []}, cookies=env.cookie)
        assert response.status == 409
        challenge = await response.json()
        assert challenge["exposureWarnings"][0]["enabledTools"] == 31
        assert env.cfg_path.read_text() == before
        assert not live.available_tools()
        response = await env.client.patch("/api/mcp/servers/playwright/tools", json={"disabledTools": [], "exposureConfirmations": challenge["exposureConfirmations"]}, cookies=env.cookie)
        assert response.status == 200
        await live.apply_exposure_approvals()
        assert len(live.available_tools()) == 31
        # User toggling an already-approved large server still sees its current budget.
        response = await env.client.patch("/api/mcp/servers/playwright/enabled", json={"enabled": True}, cookies=env.cookie)
        assert response.status == 409
        response = await env.client.patch("/api/mcp/enabled", json={"enabled": True}, cookies=env.cookie)
        assert response.status == 409
        response = await env.client.post("/api/mcp/reload", json={}, cookies=env.cookie)
        assert response.status == 409
        response = await env.client.get("/api/mcp/status", cookies=env.cookie)
        status = await response.json()
        assert status["toolSelectionAvailable"] and status["servers"][0]["enabledTools"] == 31
        assert all(t["definitionBytes"] > 0 for t in status["tools"])
    finally:
        await live.close()


async def test_stale_api_fingerprint_cannot_approve_changed_definition(admin_env, peer):
    env = admin_env
    live = MCPManager(env.server.config)
    await live.start()
    env.server.mcp = live
    before = env.cfg_path.read_text()
    try:
        response = await env.client.post("/api/mcp/servers/playwright/confirm-exposure", json={}, cookies=env.cookie)
        assert response.status == 409
        challenge = await response.json()
        peer.tools[0].description += " changed"
        response = await env.client.post("/api/mcp/servers/playwright/confirm-exposure", json={"exposureConfirmations": challenge["exposureConfirmations"]}, cookies=env.cookie)
        assert response.status == 409 and not live.exposure_approvals.records
        assert env.cfg_path.read_text() == before
    finally:
        await live.close()


async def test_managed_install_cannot_write_or_activate_without_budget_confirmation(managed_runtime, peer):
    r = managed_runtime
    path = r.root / 'openbear.json'
    before = path.read_text()
    patch = {'mcp': {'enabled': True, 'servers': {'demo': {'command': 'stub'}}}}
    response = await r.client.patch('/api/managed/v1/config', headers=r.auth, json=patch)
    assert response.status == 409
    challenge = await response.json()
    assert path.read_text() == before
    assert not r.svc.mcp.available_tools()
    response = await r.client.patch('/api/managed/v1/config', headers=r.auth,
                                   json={**patch, 'mcpExposureConfirmations': challenge['exposureConfirmations']})
    assert response.status == 200
    if r.svc._mcp_reload_task:
        await r.svc._mcp_reload_task
    assert len(r.svc.mcp.available_tools()) == 31


@pytest.mark.parametrize('answer, authorized', [
    ({'confirmed': True, 'status': 'answered'}, True),
    ({'confirmed': False, 'status': 'answered', 'decision': 'cancel'}, False),
    ({'confirmed': True, 'status': 'timeout'}, False),
    ({'confirmed': True, 'status': 'answered', 'decision': 'confirm_with_changes',
      'requiresAmendment': True, 'authorizationGranted': True, 'interactionId': 'receipt', 'text': '先停用一部分'}, False),
])
@pytest.mark.parametrize('path', ['/api/mcp/servers/demo/enabled', '/%61pi/%6dcp/servers/demo/enabled'])
async def test_instance_control_requires_native_panel_and_ignores_model_supplied_approval(monkeypatch, answer, authorized, path):
    from app.tools.instances import register_instance_tools
    cfg = config()
    summary = exposure_summary(cfg.mcp, 'demo', definitions(cfg, [f'read_{i}' for i in range(31)]))
    challenge = {'ok': False, 'error': 'mcp_exposure_confirmation_required', 'exposureWarnings': [summary],
                 'exposureConfirmations': [{'server': 'demo', 'fingerprint': summary['fingerprint']}]}
    calls, prompts = [], []
    async def row(*args):
        return {'revision': 1}
    async def one(*args):
        return {'owner_chat_id': 123}
    async def request(owner, identity, data, **kwargs):
        calls.append(json.loads(json.dumps(data)))
        if data.get('body', {}).get('exposureConfirmations') == challenge['exposureConfirmations']:
            return {'ok': True, 'status': 200, 'data': {'ok': True}}
        return {'ok': True, 'status': 409, 'data': challenge}
    async def confirm(payload):
        prompts.append(payload)
        return answer
    monkeypatch.setattr('app.tools.instances.one', one)
    service = SimpleNamespace(managed=False, db=SimpleNamespace(conn=None), require_controller=lambda: None,
                              host=SimpleNamespace(config=SimpleNamespace(telegram=SimpleNamespace(whitelist_ids=[123]))),
                              row=row, request=request, public=lambda row: {'name': 'child'})
    registry = ToolRegistry()
    register_instance_tools(registry, service)
    result = json.loads(await registry.dispatch('InstanceControl', json.dumps({'action': 'request', 'params': {
        'instanceId': 'child', 'method': 'PATCH', 'path': path,
        'body': {'enabled': True, 'exposureConfirmations': challenge['exposureConfirmations']}}}),
        context=ToolRuntimeContext(source='web', chat_id=123, conversation_uuid='conv', web_confirm=confirm)))
    assert 'exposureConfirmations' not in calls[0]['body']
    assert calls[0]['path'] == '/api/mcp/servers/demo/enabled'
    assert len(prompts) == 1 and 'tokens' in prompts[0]['body']
    assert prompts[0]['_approvalScope']['gate']['instance'] == 'child'
    assert len(calls) == (2 if authorized else 1)
    if authorized:
        assert result['status'] == 200
    elif answer.get('requiresAmendment'):
        assert result['status'] == 'amendments_required' and result['approvalId'] == 'receipt'
    else:
        assert result['effective'] == 'not_applied'


async def test_direct_disk_edit_reload_and_fresh_manager_remain_pending(managed_runtime, peer):
    r = managed_runtime
    path = r.root / 'openbear.json'
    raw = json.loads(path.read_text())
    raw['mcp'] = config().mcp.model_dump(by_alias=True)
    path.write_text(json.dumps(raw))
    result = await r.svc.reload_mcp_from_disk()
    assert result['ok'] and result['tools'] == 0
    assert r.svc.mcp.status_snapshot().servers[0].status == 'awaiting_confirmation'
    assert not any(row['name'].startswith('mcp__') for row in r.svc.tools.schemas())
    store = MCPExposureApprovals(r.svc.db)
    await store.initialize(r.svc.config)
    manager = MCPManager(r.svc.config, exposure_approvals=store)
    await manager.start()
    assert not manager.available_tools()
    await manager.close()


async def test_budget_settings_api_default_zero_and_validation(admin_env):
    env = admin_env
    response = await env.client.get('/api/settings', cookies=env.cookie)
    values = (await response.json())['values']
    assert values['mcp.toolCountThreshold'] == 30
    assert values['mcp.definitionSizeThresholdKB'] == 64
    for path in ('mcp.toolCountThreshold', 'mcp.definitionSizeThresholdKB'):
        response = await env.client.patch('/api/settings/' + path, json={'value': 0}, cookies=env.cookie)
        assert response.status == 200
        raw = json.loads(env.cfg_path.read_text())
        assert raw['mcp'][path.split('.')[1]] == 0
        response = await env.client.patch('/api/settings/' + path, json={'value': -1}, cookies=env.cookie)
        assert response.status == 400
        assert json.loads(env.cfg_path.read_text())['mcp'][path.split('.')[1]] == 0
