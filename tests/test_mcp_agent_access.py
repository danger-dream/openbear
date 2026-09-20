"""Isolated native MCP/Agent lifecycle acceptance; no external services or live config."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from collections import deque
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import Config, MCPAgentAccessConfig, MCPServerConfig
from app.llm.base import AgentResult
from app.llm.events import ToolCall
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.plan import PlanError
from app.services import Services
from app.tools.allowlist import agent_delegation_catalog, agent_delegation_names
from app.tools.base import ToolRuntimeContext
from tests.test_rath_plan import sample_plan
from tests.test_rath_web_api import FakeBot
from tests.test_tools_agent_orchestration import _FakeFactory

SERVER = r'''
import json, os, sys, time
state_path, log_path, label = sys.argv[1:]
def reply(mid, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": mid}
    message["error" if error else "result"] = error or result
    print(json.dumps(message), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    mid, method, params = message.get("id"), message.get("method"), message.get("params") or {}
    with open(log_path, "a") as log:
        log.write(json.dumps({"method": method, "params": params, "pid": os.getpid()}) + "\n")
    if method == "initialize":
        reply(mid, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": True}}, "instructions": label + " service instructions"})
    elif method == "tools/list":
        state = json.load(open(state_path))
        cursor = int(params.get("cursor", 0))
        if state.get("fail") and cursor:
            reply(mid, error={"code": -32000, "message": "isolated discovery failure"})
        else:
            tools = state["tools"]
            result = {"tools": tools[cursor:cursor+1]}
            if cursor + 1 < len(tools): result["nextCursor"] = str(cursor + 1)
            reply(mid, result)
    elif method == "tools/call":
        time.sleep(float(params.get("arguments", {}).get("delay", 0)))
        reply(mid, {"content": [{"type": "text", "text": json.dumps({"service": label, **params})}]})
    elif mid is not None:
        reply(mid, {"prompts": []})
'''


def raw_tool(name="search", version=1):
    return {"name": name, "description": f"Native search contract v{version}",
            "inputSchema": {"type": "object", "properties": {
                "source": {"type": "string", "enum": ["one"] if version == 1 else ["one", "two"], "default": "one", "examples": ["one"]},
                "query": {"type": "string", "minLength": 1}, "delay": {"type": "number"}},
                "required": ["source", "query"], "additionalProperties": False},
            "annotations": {"readOnlyHint": True}}


class Backend:
    protocol = "chat"

    def __init__(self):
        self.actions = deque()
        self.calls = []

    async def complete(self, messages, *, model, system="", tools=None, **options):
        self.calls.append(copy.deepcopy({"messages": messages, "tools": tools, "system": system}))
        action = self.actions.popleft() if self.actions else None
        if callable(action):
            action = await action(self.calls[-1])
        if action is None:
            return AgentResult(text="verified native MCP result")
        name, args = action
        return AgentResult(tool_calls=[ToolCall(id=f"call-{len(self.calls)}", name=name, arguments=json.dumps(args))], finish_reason="tool_calls")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    states, logs, servers = {}, {}, {}
    for key in ("search.service", "other"):
        states[key] = tmp_path / f"{key}.json"
        logs[key] = tmp_path / f"{key}.jsonl"
        states[key].write_text(json.dumps({"tools": [raw_tool(), raw_tool("read_more")]}))
        servers[key] = {"command": sys.executable, "args": [str(script), str(states[key]), str(logs[key]), key],
                        "stdioMode": "newline", "approval": "allow", "connectTimeoutS": 3}
    path = tmp_path / "openbear.json"
    raw = {"telegram": {"botToken": "test", "whitelistIds": [123]}, "memory": {"provider": "builtin"},
           "models": {"providers": {"openai": {"baseUrl": "http://unused.invalid", "apiKey": "unused", "protocol": "chat", "models": [{"id": "gpt"}]}}, "primary": "openai/gpt"},
           "storage": {"dbPath": str(tmp_path / "test.db")}, "web": {"enabled": False},
           "rath": {"agentToolForegroundWaitS": 5},
           "tools": {"skillsDir": str(tmp_path / "skills")}, "mcp": {"enabled": True, "toolNamePrefix": "native", "servers": servers}}
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("OPENBEAR_CONFIG", str(path))
    svc = Services(Config.model_validate(raw), FakeBot())
    await svc.db.connect()
    await ensure_builtin_workflows(svc.rath_dao)
    backend = Backend()
    svc.factory = _FakeFactory(backend)
    await svc.mcp.start()
    svc._rebuild_tools_and_context()
    ctx = ToolRuntimeContext(chat_id=123, session_uuid="isolated-conversation", conversation_uuid="isolated-conversation", source="web", turn_uuid="turn-1", run_root_turn_uuid="turn-1")
    await svc.web_admin.ensure_secret_key()
    client = TestClient(TestServer(svc.web_admin.make_app()))
    await client.start_server()
    secret = await svc.web_admin.get_secret_key()
    started = await client.post("/api/auth/login/start", json={"secret": secret})
    request = (await started.json())["requestUuid"]
    await svc.web_admin.decide_login_request(request, approved=True, decided_by=123)
    logged = await client.post(f"/api/auth/login/consume/{request}")
    cookie = {"openbear_web_session": logged.cookies["openbear_web_session"].value}
    async def grant(mode="all", tools=None, server="search.service"):
        old = (await svc.config_store.load_raw())["mcp"]["servers"][server].get("agentAccess", {"mode": "disabled", "tools": []})
        response = await client.patch(f"/api/mcp/servers/{server}/agent-access", cookies=cookie,
                                      json={"agentAccess": {"mode": mode, "tools": tools or []}, "expectedAgentAccess": old})
        assert response.status == 200, await response.text()
        return await response.json()
    try:
        yield SimpleNamespace(svc=svc, backend=backend, ctx=ctx, client=client, cookie=cookie,
                              states=states, logs=logs, grant=grant, path=path)
    finally:
        for task in list(svc.rath._runs.values()):
            task.cancel()
        if svc.rath._runs:
            await asyncio.gather(*list(svc.rath._runs.values()), return_exceptions=True)
        await client.close()
        await svc.mcp.close()
        await svc.http.close()
        await svc.mem.close()
        await svc.db.close()


async def call(env, name, args, *, registry=None, context=None):
    result = await (registry or env.svc.tools).dispatch(name, json.dumps(args), context=context or env.ctx)
    return json.loads(result)


def events(env, server="search.service", method="tools/call"):
    return [row for row in map(json.loads, env.logs[server].read_text().splitlines()) if row["method"] == method]


def name(env, original="search", server="search.service"):
    return next(meta.public_name for meta in env.svc.mcp.available_tools() if meta.server_key == server and meta.original_tool_name == original)


async def test_default_off_native_main_unchanged_and_exact_service_scoped_selection(env):
    tool = name(env)
    assert tool in env.svc.tools.names(scope="main")
    assert tool not in agent_delegation_names(env.svc.tools)
    assert (await call(env, tool, {"source": "one", "query": "main"}))["service"] == "search.service"
    denied = await call(env, "Agent", {"prompt": "read", "tools": [tool]})
    assert denied["error"] == "agent_tool_not_available"
    await env.grant("selected", ["Search", "missing"])
    assert tool not in agent_delegation_names(env.svc.tools)
    await env.grant("selected", ["search", "missing"])
    assert tool in agent_delegation_names(env.svc.tools)
    assert name(env, server="other") not in agent_delegation_names(env.svc.tools)
    env.svc.tools.add("mcp__forged__search", "forged", {}, lambda _: None, source="mcp")
    env.svc.tools.add("ControllerOnly", "not delegable", {}, lambda _: None)
    assert not {"mcp__forged__search", "ControllerOnly"} & agent_delegation_names(env.svc.tools)
    state = await env.client.get("/api/mcp/status", cookies=env.cookie)
    payload = await state.json()
    selected = next(row for row in payload["servers"] if row["key"] == "search.service")
    assert selected["agentAccess"] == {"mode": "selected", "tools": ["search", "missing"]}
    assert sys.executable not in json.dumps(payload)
    assert all("command" not in row and "env" not in row and "headers" not in row for row in payload["servers"])


async def test_refresh_updates_full_schema_and_agent_create_continue_without_reconnect(env):
    await env.grant("all")
    old_registry = env.svc.tools
    clients = dict(env.svc.mcp._clients)
    tool = name(env)
    env.backend.actions.extend([(tool, {"source": "one", "query": "first"}), None])
    first = await call(env, "Agent", {"prompt": "Use native search", "tools": [tool]})
    assert first["status"] == "completed", first
    assert events(env)[-1]["params"]["arguments"]["query"] == "first"
    first_schema = next(t for t in env.backend.calls[0]["tools"] if t["name"] == tool)
    assert first_schema["parameters"] == raw_tool()["inputSchema"]
    assert "other service instructions" not in str(env.backend.calls[0])
    assert "search.service service instructions" in str(env.backend.calls[0])
    other_meta = [m.model_dump() for m in env.svc.mcp.available_tools() if m.server_key == "other"]
    env.states["search.service"].write_text(json.dumps({"tools": [raw_tool(version=2), raw_tool("new_search", 2)]}))
    response = await env.client.post("/api/mcp/servers/search.service/refresh-tools", cookies=env.cookie)
    assert response.status == 200 and (await response.json())["changed"]
    assert env.svc.mcp._clients == clients
    assert [m.model_dump() for m in env.svc.mcp.available_tools() if m.server_key == "other"] == other_meta
    assert len(events(env, method="initialize")) == 1
    assert name(env, "new_search") in agent_delegation_names(env.svc.tools)
    assert len(events(env, method="tools/list")) == 4  # both complete two-page lists
    env.backend.actions.extend([(tool, {"source": "two", "query": "continued"}), None])
    second = await call(env, "AgentContinue", {"to": first["agentSession"]["sessionUuid"], "prompt": "Use the new source enum", "tools": [tool]}, registry=old_registry)
    assert second["status"] == "completed", second
    schema = next(t for t in env.backend.calls[-2]["tools"] if t["name"] == tool)
    assert schema["description"] == raw_tool(version=2)["description"]
    assert schema["parameters"] == raw_tool(version=2)["inputSchema"]
    assert name(env, "new_search") not in {t["name"] for t in env.backend.calls[-2]["tools"]}
    assert len(events(env)) == 2
    assert events(env)[-1]["params"]["arguments"]["source"] == "two"
    snapshot = copy.deepcopy(env.svc.mcp.all_tools_snapshot())
    env.states["search.service"].write_text(json.dumps({"tools": [raw_tool(version=3), raw_tool("fail")], "fail": True}))
    failure = await env.client.post("/api/mcp/servers/search.service/refresh-tools", cookies=env.cookie)
    assert failure.status == 503
    assert env.svc.mcp.all_tools_snapshot() == snapshot
    assert env.svc.mcp._clients == clients


async def test_access_save_cas_light_apply_disk_reload_and_selected_new_names(env):
    clients = dict(env.svc.mcp._clients)
    before = await env.svc.config_store.load_raw()
    await env.grant("selected", ["search", "missing"])
    conflict = await env.client.patch("/api/mcp/servers/search.service/agent-access", cookies=env.cookie,
        json={"agentAccess": {"mode": "all", "tools": []}, "expectedAgentAccess": {"mode": "disabled", "tools": []}})
    assert conflict.status == 409
    after = await env.svc.config_store.load_raw()
    after["mcp"]["servers"]["search.service"].pop("agentAccess")
    assert before == after
    env.states["search.service"].write_text(json.dumps({"tools": [raw_tool(), raw_tool("new_search")]}))
    await env.svc.mcp._handle_server_notification("search.service", "notifications/tools/list_changed", {})
    assert name(env, "new_search") not in agent_delegation_names(env.svc.tools)
    await env.svc.config_store.mutate(lambda raw: raw["mcp"]["servers"]["search.service"].update(agentAccess={"mode": "all", "tools": ["missing"]}))
    result = await env.svc.reload_mcp_from_disk()
    assert result["ok"] and name(env, "new_search") in agent_delegation_names(env.svc.tools)
    assert clients == env.svc.mcp._clients
    assert len(events(env, method="initialize")) == 1


async def test_live_revoke_blocks_old_registry_and_grant_but_does_not_replay_inflight(env):
    await env.grant("all")
    old = env.svc.tools
    tool = name(env)
    ctx = ToolRuntimeContext(source="agent:fixture", agent_session_uuid="fixture", conversation_uuid="legacy-grant")
    env.svc.mcp._grant_conversation(next(m for m in env.svc.mcp.available_tools() if m.public_name == tool), ctx)
    pending = asyncio.create_task(call(env, tool, {"query": "inflight", "source": "one", "delay": .15}, registry=old, context=ctx))
    for _ in range(100):
        if events(env):
            break
        await asyncio.sleep(.005)
    assert len(events(env)) == 1
    await env.grant("disabled")
    assert (await pending)["arguments"]["query"] == "inflight"
    blocked = await call(env, tool, {"query": "must not send", "source": "one"}, registry=old, context=ctx)
    assert blocked["error"] == "mcp_agent_access_disabled"
    assert len(events(env)) == 1
    denied = await call(env, "Agent", {"prompt": "old contract", "tools": [tool]}, registry=old)
    assert denied["error"] == "agent_tool_not_available"


async def test_preset_dynamic_candidates_invalid_selection_roundtrip_and_conflict(env):
    await env.grant("all")
    tool = name(env)
    options = await (await env.client.get("/api/rath/options", cookies=env.cookie)).json()
    row = next(t for t in options["tools"] if t["name"] == tool)
    assert row["kind"] == "mcp" and row["originalToolName"] == "search"
    created = await env.client.post("/api/rath/agents", cookies=env.cookie, json={"name": "native", "toolAllowlist": [tool]})
    agent = (await created.json())["item"]
    await env.grant("disabled")
    updated = await env.client.put(f"/api/rath/agents/{agent['id']}", cookies=env.cookie, json={"name": "preserved", "toolAllowlist": [tool], "expectedToolAllowlist": [tool]})
    assert updated.status == 200 and (await updated.json())["item"]["tool_allowlist"] == [tool]
    bad = await env.client.post("/api/rath/agents", cookies=env.cookie, json={"name": "cannot add", "toolAllowlist": [tool]})
    assert bad.status == 400
    conflict = await env.client.put(f"/api/rath/agents/{agent['id']}", cookies=env.cookie, json={"toolAllowlist": [], "expectedToolAllowlist": []})
    assert conflict.status == 409
    listed = await (await env.client.get("/api/rath/agents", cookies=env.cookie)).json()
    assert next(a for a in listed["items"] if a["id"] == agent["id"])["tool_allowlist"] == [tool]


async def test_plan_native_grant_step_gate_frozen_contract_and_live_revoke(env):
    await env.grant("all")
    tool, extra = name(env), name(env, "read_more")
    plan = sample_plan(second_step=False)
    plan["toolRequests"] = [{"name": tool, "reason": "native search", "neededForSteps": ["s1"]}]
    notifications = []
    async def notify(payload):
        notifications.append(payload)
        if payload.get("kind") == "plan-approval-required":
            bad = await call(env, "AgentPlanDecision", {"taskUuid": payload["taskUuid"], "expectedPlanVersion": payload["planVersion"], "action": "approve", "grantedTools": [extra], "requestId": "bad-grant"})
            assert bad["error"] == "invalid_tool_grant"
            good = await call(env, "AgentPlanDecision", {"taskUuid": payload["taskUuid"], "expectedPlanVersion": payload["planVersion"], "action": "approve", "grantedTools": [tool], "requestId": "good-grant"})
            assert good["ok"], good
    async def wait(_):
        return ""
    env.ctx.task_notification, env.ctx.agent_wait = notify, wait
    async def refresh_during_execution(request):
        assert not events(env)
        assert "plan_step_not_started" in str(request["messages"])
        env.states["search.service"].write_text(json.dumps({"tools": [raw_tool(version=2), raw_tool("read_more", 2)]}))
        await env.svc.mcp.refresh_server_tools("search.service")
        return tool, {"source": "one", "query": "Plan native call"}
    replan = sample_plan(second_step=False)
    replan["toolRequests"] = [{"name": extra, "reason": "not permitted", "neededForSteps": ["s1"]}]
    async def revoke(request):
        schema = next(t for t in request["tools"] if t["name"] == tool)
        assert schema["parameters"] == raw_tool()["inputSchema"]  # frozen v1, not refreshed v2
        assert "tool_expansion_locked" in str(request["messages"])
        assert len(events(env)) == 1
        await env.grant("disabled")
        return tool, {"source": "one", "query": "stale model call must not be sent"}
    env.backend.actions.extend([
        (tool, {"source": "one", "query": "before approval"}),
        ("AgentPlanSubmit", {"plan": plan}),
        (tool, {"source": "one", "query": "before step"}),
        ("AgentPlanProgress", {"action": "start", "stepId": "s1"}),
        refresh_during_execution,
        ("AgentPlanReplan", {"changeReason": "try to expand", "plan": replan}),
        revoke,
    ])
    launched = await call(env, "Agent", {"prompt": "Managed native MCP task", "tools": [], "planMode": "managed"})
    task_id = launched["taskUuid"]
    run = env.svc.rath._runs.get(task_id)
    assert run is not None
    await asyncio.wait_for(asyncio.shield(run), timeout=10)
    task = await env.svc.rath_dao.get_task(task_id)
    assert task.status == "needs_openbear_control", task
    assert len(events(env)) == 1
    assert len(env.backend.calls) == 7
    assert "tool_denied_by_plan_phase" in str(env.backend.calls[1]["messages"])
    snapshot = await env.svc.rath.plan_coordinator.snapshot(task_id)
    assert snapshot["state"]["approved_tools"] == [tool]
    info = await call(env, "AgentInfo", {"action": "get", "to": task.agent_session_uuid})
    assert tool in info["capabilities"]["grantedTools"]
    assert tool not in info["capabilities"]["effectiveTools"]
    assert info["capabilities"]["unavailableTools"][tool] == "mcp_agent_access_disabled"


async def test_dynamic_plan_request_count_and_revoked_grant_rejected(env):
    env.states["search.service"].write_text(json.dumps({"tools": [raw_tool(f"read_{i}") for i in range(12)]}))
    await env.svc.mcp.refresh_server_tools("search.service")
    await env.grant("all")
    names = [row["name"] for row in agent_delegation_catalog(env.svc.tools) if row["kind"] == "mcp"]
    schema = next(t for t in env.svc.tools.schemas() if t["name"] == "AgentPlanSubmit")
    requests = schema["parameters"]["properties"]["plan"]["properties"]["toolRequests"]
    assert len(names) == 12 and requests["maxItems"] >= 12
    assert set(names) <= set(requests["items"]["properties"]["name"]["enum"])
    coordinator = env.svc.rath.plan_coordinator
    workflow = await env.svc.rath_dao.workflow_by_slug("single-agent")
    task_id = await env.svc.rath_dao.create_task(chat_id=123, workflow_uuid=workflow.workflow_uuid, title="dynamic", status="running", input_data={"agentSnapshot": {"toolAllowlist": []}}, parent_session_uuid=env.ctx.session_uuid)
    plan = sample_plan(second_step=False)
    plan["toolRequests"] = [{"name": name, "reason": "needed", "neededForSteps": ["s1"]} for name in names]
    submitted = await coordinator.submit_plan(task_id, plan, request_id="large", wait_for_decision=False)
    await env.grant("disabled")
    with pytest.raises(PlanError, match="current Agent delegation catalog"):
        await coordinator.decide(task_id, expected_version=submitted["planVersion"], action="approve", granted_tools=names, request_id="revoked")


async def test_empty_preset_preserves_no_extra_ceiling_but_round_empty_has_no_mcp(env):
    await env.grant("all")
    tool = name(env)
    await env.svc.rath_dao.create_agent(agent_key="empty-cap", name="Empty cap", tool_allowlist=[])
    result = await call(env, "Agent", {"prompt": "No business tools this round", "workerType": "empty-cap", "tools": []})
    assert result["status"] == "completed"
    assert not any(t["name"] == tool for t in env.backend.calls[-1]["tools"])
    granted = await call(env, "AgentContinue", {"to": result["agentSession"]["sessionUuid"], "prompt": "Explicit permission now", "tools": [tool]})
    assert granted["status"] == "completed"
    assert tool in {t["name"] for t in env.backend.calls[-1]["tools"]}
    await env.grant("disabled")
    refused = await call(env, "AgentContinue", {"to": result["agentSession"]["sessionUuid"], "prompt": "History cannot restore permission", "tools": [tool]})
    assert refused["error"] == "agent_tool_not_available"


def test_access_config_defaults_strict_literal_names_and_old_search_removed():
    from pydantic import ValidationError

    from app.tools.allowlist import sanitize_tool_allowlist
    assert MCPServerConfig(approval="allow").agent_access.mode == "disabled"
    assert MCPServerConfig().model_dump(by_alias=True)["agentAccess"] == {"mode": "disabled", "tools": []}
    policy = MCPAgentAccessConfig(mode="selected", tools=["Search", "search", "*", "Search"])
    assert policy.tools == ["Search", "search", "*"]
    for invalid in ({"mode": "allow"}, {"tools": "search"}, {"tools": [1]}, {"tools": [""]}, {"all": True}):
        with pytest.raises(ValidationError):
            MCPAgentAccessConfig.model_validate(invalid)
    assert sanitize_tool_allowlist(["WebSearch", "WebExtract", "Read"]) == ["Read"]


async def test_offline_configuration_safe_status_and_saved_not_applied(env):
    assert not {"WebSearch", "WebExtract"} & set(env.svc.tools.names())
    client = env.svc.mcp._clients.pop("search.service")
    try:
        await env.grant("selected", ["search", "gone"])
        assert name(env) not in agent_delegation_names(env.svc.tools)
        state = await (await env.client.get("/api/mcp/status", cookies=env.cookie)).json()
        row = next(row for row in state["servers"] if row["key"] == "search.service")
        assert row["agentAccess"]["tools"] == ["search", "gone"]
        assert row["agentDelegatableTools"] == 0
        response = await env.client.post("/api/mcp/servers/search.service/refresh-tools", cookies=env.cookie)
        assert response.status == 503
    finally:
        env.svc.mcp._clients["search.service"] = client
    async def failure(_):
        raise RuntimeError("secret_connection_detail_must_not_leak")
    hook = env.svc.web_admin._mcp_agent_access_hook
    env.svc.web_admin._mcp_agent_access_hook = failure
    failed = await env.client.patch("/api/mcp/servers/search.service/agent-access", cookies=env.cookie,
        json={"agentAccess": {"mode": "disabled", "tools": ["gone"]}, "expectedAgentAccess": {"mode": "selected", "tools": ["search", "gone"]}})
    payload = await failed.json()
    assert failed.status == 503 and payload["saved"] and not payload["applied"]
    assert "secret_connection_detail" not in str(payload)
    status = await (await env.client.get("/api/mcp/status", cookies=env.cookie)).json()
    row = next(row for row in status["servers"] if row["key"] == "search.service")
    assert row["agentAccess"]["mode"] == "disabled" and not row["agentAccessApplied"]
    env.svc.web_admin._mcp_agent_access_hook = hook
    await env.grant("disabled", ["gone"])
    env.svc.web_admin.mcp = None
    status = await (await env.client.get("/api/mcp/status", cookies=env.cookie)).json()
    assert len(status["servers"]) == 2 and status["tools"] == []
    assert status["servers"][0]["agentAccess"]["tools"] == ["gone"]


async def test_legacy_removed_only_preset_remains_restrictive(env):
    await env.svc.db.conn.execute(
        """
        INSERT INTO rath_agents (
          agent_key, name, tool_allowlist_json, enabled, created_at, updated_at
        ) VALUES (?,?,?,?,?,?)
        """,
        ("legacy-websearch-only", "Legacy search", '["WebSearch"]', 1, 1, 1),
    )
    await env.svc.db.conn.commit()

    denied = await call(env, "Agent", {
        "workerType": "legacy-websearch-only",
        "prompt": "must not gain another tool after upgrade",
        "tools": ["Read"],
    })
    assert denied["error"] == "agent_tool_not_allowed_by_preset"
    assert denied["presetAllowedTools"] == []

    no_business_tools = await call(env, "Agent", {
        "workerType": "legacy-websearch-only",
        "prompt": "removed legacy capability stays unavailable",
        "tools": [],
    })
    assert no_business_tools["status"] == "completed"
    assert not any(t["name"] == "Read" for t in env.backend.calls[-1]["tools"])


async def test_nonempty_preset_caps_native_tools_and_unregistered_builtins_are_not_candidates(env):
    await env.grant("all")
    tool = name(env)
    await env.svc.rath_dao.create_agent(agent_key="read-only-cap", name="Read only cap", tool_allowlist=["Read"])
    denied = await call(env, "Agent", {"workerType": "read-only-cap", "prompt": "must not expand", "tools": [tool]})
    assert denied["error"] == "agent_tool_not_allowed_by_preset"
    env.svc.web_admin.tools = None
    denied = await env.client.post("/api/rath/agents", cookies=env.cookie, json={"name": "not registered", "toolAllowlist": ["Read"]})
    assert denied.status == 400
