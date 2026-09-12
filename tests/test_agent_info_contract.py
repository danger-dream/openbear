"""AgentInfo is current execution state, not a private-memory/history export.

All execution uses the deterministic backend and a temporary SQLite database.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from app.llm.base import AgentResult
from app.llm.events import ToolCall
from app.task_memory import TaskMemoryDAO
from app.tools.agent_history import register_agent_history_tool
from app.tools.history import register_history_tools
from tests.test_agent_continuity import call
from tests.test_agent_continuity import env as continuity_env
from tests.test_rath_plan import criterion_evidence, sample_plan
from tests.test_tools_agent_orchestration import _FakeConfig

env = continuity_env


async def inspect(reg, ctx, target):
    result = await call(reg, "AgentInfo", {"action": "get", "to": target}, ctx)
    assert result["ok"], result
    return result


async def test_direct_info_never_reads_private_memory_or_exports_raw_history(env, monkeypatch):
    dao, reg, _, ctx, manager = env
    first = await call(reg, "Agent", {"prompt": "Return the authorized result", "tools": []}, ctx)
    sid, tid = first["agentSession"]["agentId"], first["task"]["taskUuid"]
    item, _ = await TaskMemoryDAO(dao.db).create(
        conversation_uuid=ctx.session_uuid, scope_type="agent_session", task_uuid=sid,
        name="PRIVATE-NAME-MARKER", description="PRIVATE-DESCRIPTION-MARKER",
        body="PRIVATE-BODY-MARKER", auto_reinject_catalog=True,
    )
    await dao.append_event(tid, "tool_result", summary="RAW-TOOL-HISTORY-MARKER",
                           detail={"result": "RAW-TOOL-BODY-MARKER"})

    async def forbidden(*args, **kwargs):
        pytest.fail("AgentInfo must not construct memory catalogs or read a direct Plan")

    monkeypatch.setattr("app.task_memory.task_memory_catalog_snapshot", forbidden)
    monkeypatch.setattr(manager.plan_coordinator, "snapshot", forbidden)
    queries = []
    execute = dao.db.conn.execute

    def traced_execute(sql, *args, **kwargs):
        queries.append(sql)
        return execute(sql, *args, **kwargs)

    changes = dao.db.conn.total_changes
    with monkeypatch.context() as patch:
        patch.setattr(dao.db.conn, "execute", traced_execute)
        info = await inspect(reg, ctx, sid)
    assert dao.db.conn.total_changes == changes
    assert not any("conversation_task_memor" in query.lower() for query in queries)
    assert "memoryCatalog" not in info
    assert info["memoryScope"] == "agent_session"
    encoded = json.dumps(info, ensure_ascii=False)
    for value in (item["memoryUuid"], item["name"], item["description"], item["body"],
                  "RAW-TOOL-HISTORY-MARKER", "RAW-TOOL-BODY-MARKER"):
        assert value not in encoded
    assert info["task"]["output"] == first["result"]  # Explicit handoff is preserved.
    assert info["agentSession"]["canContinue"]
    assert info["contextAvailable"]
    assert info["planRuntime"] == {
        "taskUuid": tid, "planMode": "direct", "available": True, "phase": "direct",
        "planVersion": 0, "activePlanVersion": 0, "pendingPlanVersion": 0,
        "currentStepId": "", "plan": None, "steps": [], "evidence": [],
    }


async def test_simulated_continued_agent_can_be_supervised_and_approved_only_from_info(env, monkeypatch):
    dao, reg, backend, ctx, manager = env
    first = await call(reg, "Agent", {"prompt": "Retain investigation without writing notes", "tools": []}, ctx)
    sid, old_tid = first["agentSession"]["agentId"], first["task"]["taskUuid"]
    original_complete = backend.complete
    monkeypatch.setattr(_FakeConfig.rath, "agent_plan_enabled", True)
    submitted = asyncio.Event()
    stages = asyncio.Queue()
    release = asyncio.Queue()
    stage = 0

    async def notification(payload):
        # Notifications only wake the test; no Plan/version is read from them.
        if payload.get("kind") == "plan-approval-required":
            submitted.set()

    async def wait(*args, **kwargs):
        pass

    scripted = [
        ("AgentPlanSubmit", {"plan": sample_plan()}),
        ("AgentPlanProgress", {"action": "start", "stepId": "s1"}),
        ("AgentPlanProgress", {"action": "complete", "stepId": "s1", "result": "core complete",
                               "criteria": [{"id": "c1", "status": "satisfied"}],
                               "evidence": criterion_evidence("c1", "test:core-passed")}),
        ("AgentPlanProgress", {"action": "start", "stepId": "s2"}),
        ("AgentPlanProgress", {"action": "complete", "stepId": "s2", "result": "integration complete",
                               "criteria": [{"id": "c2", "status": "satisfied"}],
                               "evidence": criterion_evidence("c2", "test:integration-passed")}),
        ("AgentPlanProgress", {"action": "finalize", "finalOutputs": [
            {"id": "o1", "summary": "verified", "sources": ["step:s2"]},
        ]}),
    ]

    async def complete(messages, **kwargs):
        nonlocal stage
        stage += 1
        if stage == 1:
            assert "Retain investigation without writing notes" in str(messages)
        else:
            await stages.put(stage)
            await release.get()
        if stage > len(scripted):
            return AgentResult(text="Authorized managed result")
        name, args = scripted[stage - 1]
        return AgentResult(tool_calls=[ToolCall(f"info-stage-{stage}", name, json.dumps(args))])

    monkeypatch.setattr(backend, "complete", complete)
    managed_ctx = replace(ctx, task_notification=notification, agent_wait=wait)
    try:
        result = await call(reg, "AgentContinue", {
            "to": sid, "prompt": "Execute the approved package", "tools": [], "planMode": "managed",
        }, managed_ctx)
        tid = result["task"]["taskUuid"]
        await asyncio.wait_for(submitted.wait(), timeout=5)
        run = manager.task(tid)
        assert run is not None
        # An old task locator must resolve to the same instance's current round.
        pending = await inspect(reg, ctx, old_tid)
        runtime = pending["planRuntime"]
        assert pending["task"]["taskUuid"] == runtime["taskUuid"] == tid != old_tid
        assert runtime["available"] and runtime["planMode"] == "managed"
        assert runtime["phase"] == "awaiting_plan_decision"
        assert runtime["planVersion"] == runtime["pendingPlanVersion"] == 1
        assert runtime["activePlanVersion"] == 0 and runtime["currentStepId"] == ""
        assert runtime["plan"]["title"] == sample_plan()["title"]
        assert {step["id"] for step in runtime["plan"]["steps"]} == {"s1", "s2"}
        assert pending["capabilities"]["phase"] == runtime["phase"]
        assert not pending["agentSession"]["canContinue"]
        changes = dao.db.conn.total_changes
        assert (await inspect(reg, ctx, sid))["planRuntime"] == runtime
        assert dao.db.conn.total_changes == changes

        approved = await call(reg, "AgentPlanDecision", {
            "taskUuid": runtime["taskUuid"], "expectedPlanVersion": runtime["pendingPlanVersion"],
            "action": "approve", "reason": "Scope and criteria are sufficient", "requestId": "approve-from-info",
        }, ctx)
        assert approved["ok"], approved
        assert await asyncio.wait_for(stages.get(), timeout=5) == 2
        executing = (await inspect(reg, ctx, sid))["planRuntime"]
        assert executing["phase"] == "executing" and executing["activePlanVersion"] == 1
        assert executing["pendingPlanVersion"] == 0
        release.put_nowait(True)  # Start s1.
        assert await asyncio.wait_for(stages.get(), timeout=5) == 3
        running = (await inspect(reg, ctx, sid))["planRuntime"]
        assert running["currentStepId"] == "s1"
        assert {(s["step_id"], s["status"]) for s in running["steps"]} == {("s1", "running"), ("s2", "pending")}
        release.put_nowait(True)  # Complete s1 with evidence.
        assert await asyncio.wait_for(stages.get(), timeout=5) == 4
        finished_step = (await inspect(reg, ctx, sid))["planRuntime"]
        assert finished_step["currentStepId"] == ""
        assert finished_step["evidence"][0]["reference"] == "test:core-passed"
        assert finished_step["evidence"][0]["evidence_uuid"]
        assert finished_step["steps"][0]["status"] == "completed"
        release.put_nowait(True)  # Start s2.
        assert await asyncio.wait_for(stages.get(), timeout=5) == 5
        assert (await inspect(reg, ctx, sid))["planRuntime"]["currentStepId"] == "s2"
        for expected_stage in (6, 7):
            release.put_nowait(True)
            assert await asyncio.wait_for(stages.get(), timeout=5) == expected_stage
        release.put_nowait(True)
        await asyncio.wait_for(run, timeout=5)
        final = await inspect(reg, ctx, sid)
        assert final["task"]["status"] == "completed"
        assert final["planRuntime"]["phase"] == "finalizing"
        assert len(final["planRuntime"]["evidence"]) == 2
        assert final["agentSession"]["canContinue"]

        monkeypatch.setattr(backend, "complete", original_complete)
        direct = await call(reg, "AgentContinue", {"to": sid, "prompt": "Read retained result", "tools": []}, ctx)
        assert direct["status"] == "completed", direct
        current = await inspect(reg, ctx, tid)
        assert current["task"]["taskUuid"] == direct["task"]["taskUuid"]
        assert current["planRuntime"]["phase"] == "direct" and current["planRuntime"]["plan"] is None
        assert (await manager.plan_coordinator.snapshot(tid))["state"]["active_plan_version"] == 1
        row = await (await dao.db.conn.execute("SELECT COUNT(*) FROM conversation_task_memories")).fetchone()
        assert row[0] == 0  # Actual continuation never needed a memory report.
    finally:
        runs = list(manager._runs.values())
        for run in runs:
            run.cancel()
        await asyncio.gather(*runs, return_exceptions=True)


async def test_info_replan_versions_preserve_evidence_and_current_task_without_checkpoint(env, monkeypatch):
    dao, reg, _, ctx, manager = env
    first = await call(reg, "Agent", {"prompt": "prior checkpoint", "tools": []}, ctx)
    sid, old_tid = first["agentSession"]["agentId"], first["task"]["taskUuid"]
    session = await dao.agent_session(sid)
    monkeypatch.setattr(_FakeConfig.rath, "agent_plan_enabled", True)
    tid = await dao.create_task(
        chat_id=ctx.chat_id, workflow_uuid=session.workflow_uuid, agent_session_uuid=sid,
        parent_session_uuid=ctx.session_uuid, title="next managed round", status="running",
        input_data={"planMode": "managed", "contextSource": {
            "taskUuid": session.context_task_uuid, "revision": session.context_revision,
        }},
    )
    coordinator = manager.plan_coordinator
    await coordinator.submit_plan(tid, sample_plan(), request_id="initial", wait_for_decision=False)
    initial = (await inspect(reg, ctx, sid))["planRuntime"]
    assert (await call(reg, "AgentPlanDecision", {
        "taskUuid": initial["taskUuid"], "expectedPlanVersion": initial["pendingPlanVersion"],
        "action": "approve", "reason": "covered", "requestId": "approve-initial",
    }, ctx))["ok"]
    await coordinator.progress(tid, action="start", step_id="s1", request_id="start")
    await coordinator.progress(tid, action="complete", step_id="s1", request_id="complete",
                               result_text="done", criteria=[{"id": "c1", "status": "satisfied"}],
                               evidence=criterion_evidence("c1"))
    active = (await inspect(reg, ctx, sid))["planRuntime"]
    assert (await call(reg, "AgentPlanDecision", {
        "taskUuid": active["taskUuid"], "expectedPlanVersion": active["activePlanVersion"],
        "action": "request_replan", "reason": "integration method changed", "requestId": "replan",
    }, ctx))["ok"]
    replacement = sample_plan(title="Current replacement")
    replacement["steps"] = replacement["steps"][1:]
    await coordinator.submit_plan(tid, replacement, plan_type="replan", change_reason="method changed",
                                  request_id="submit-v2", wait_for_decision=False)
    pending = (await inspect(reg, ctx, old_tid))["planRuntime"]
    assert pending["phase"] == "awaiting_replan_decision"
    assert pending["activePlanVersion"] == 1 and pending["pendingPlanVersion"] == pending["planVersion"] == 2
    assert pending["plan"]["title"] == "Current replacement"
    assert any(s["plan_version"] == 1 and s["step_id"] == "s1" and s["status"] == "completed" for s in pending["steps"])
    assert pending["evidence"][0]["evidence_uuid"] == active["evidence"][0]["evidence_uuid"]
    stale = await call(reg, "AgentPlanDecision", {
        "taskUuid": tid, "expectedPlanVersion": 1, "action": "approve", "requestId": "stale",
    }, ctx)
    assert not stale["ok"] and stale["error"] == "stale_plan_version"
    assert (await call(reg, "AgentPlanDecision", {
        "taskUuid": pending["taskUuid"], "expectedPlanVersion": pending["pendingPlanVersion"],
        "action": "approve", "reason": "replacement covered", "requestId": "approve-v2",
    }, ctx))["ok"]
    # A round can terminate without replacing the old retained checkpoint.
    await dao.update_task(tid, status="failed", error="simulated failure before checkpoint")
    latest = await inspect(reg, ctx, sid)
    assert latest["agentSession"]["contextTaskUuid"] == old_tid
    assert latest["task"]["taskUuid"] == latest["planRuntime"]["taskUuid"] == tid
    assert latest["planRuntime"]["activePlanVersion"] == 2
    assert latest["capabilities"]["effectiveTools"] == []


@pytest.mark.parametrize("with_plan", [False, True])
async def test_legacy_info_does_not_invent_or_initialize_plan_state(env, with_plan):
    dao, reg, _, ctx, manager = env
    workflow = await dao.workflow_by_slug("single-agent")
    session = await dao.get_or_create_agent_session(
        openbear_session_uuid=ctx.session_uuid, chat_id=ctx.chat_id,
        workflow_uuid=workflow.workflow_uuid, agent_key="general-purpose",
    )
    tid = await dao.create_task(chat_id=ctx.chat_id, workflow_uuid=workflow.workflow_uuid,
                                parent_session_uuid=ctx.session_uuid, agent_session_uuid=session.session_uuid,
                                title="legacy task", status="running")
    item, _ = await TaskMemoryDAO(dao.db).create(
        conversation_uuid=ctx.session_uuid, scope_type="agent_task", task_uuid=tid,
        name="LEGACY-PRIVATE-NAME", description="LEGACY-PRIVATE-DESCRIPTION", body="LEGACY-PRIVATE-BODY",
    )
    if with_plan:
        await manager.plan_coordinator.submit_plan(tid, sample_plan(), request_id="legacy", wait_for_decision=False)
    changes = dao.db.conn.total_changes
    info = await inspect(reg, ctx, tid)
    assert dao.db.conn.total_changes == changes
    assert info["legacy"] and info["memoryScope"] == "agent_task"
    for key in ("name", "description", "body", "memoryUuid"):
        assert item[key] not in json.dumps(info)
    runtime = info["planRuntime"]
    assert runtime["planMode"] == "managed"
    assert runtime["available"] is with_plan
    if with_plan:
        assert runtime["pendingPlanVersion"] == 1 and runtime["phase"] == "awaiting_plan_decision"
    else:
        assert "plan" not in runtime and "phase" not in runtime
        row = await (await dao.db.conn.execute("SELECT 1 FROM rath_task_plan_state WHERE task_uuid=?", (tid,))).fetchone()
        assert row is None
    group = await call(reg, "AgentInfo", {"action": "get", "to": session.session_uuid}, ctx)
    assert group["error"] == "legacy_group_requires_explicit_task"


async def test_agent_info_cross_conversation_and_agent_callers_still_rejected(env):
    _, reg, _, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "same-conversation-only", "tools": []}, ctx)
    for target in (first["agentSession"]["agentId"], first["task"]["taskUuid"]):
        for outsider in (replace(ctx, session_uuid="other", conversation_uuid="other"),
                         replace(ctx, chat_id=456)):
            denied = await call(reg, "AgentInfo", {"action": "get", "to": target}, outsider)
            assert not denied["ok"] and denied["error"] == "agent_instance_not_found"
        denied = await call(reg, "AgentInfo", {"action": "get", "to": target},
                            replace(ctx, source="agent:general-purpose"))
        assert denied["error"] == "main_controller_only"


async def test_agent_history_and_continuation_tool_descriptions_match_runtime_boundaries(env):
    dao, reg, _, _, _ = env
    register_agent_history_tool(reg, dao.db)
    register_history_tools(reg, dao.db)
    schemas = {schema["name"]: schema for schema in reg.schemas()}
    agent = schemas["Agent"]
    assert "no business tools" in agent["description"]
    for name in ("Agent", "AgentContinue"):
        tools = schemas[name]["parameters"]["properties"]["tools"]["description"]
        assert "no business tools" in tools and "AgentHistory" in tools and "protocol tools" in tools
    continuation = schemas["AgentContinue"]["description"]
    assert "actual retained context" in continuation and "No prior memory report" in continuation
    assert "permissions are not inherited or unioned" in continuation
    info = schemas["AgentInfo"]["description"]
    assert "authoritative planRuntime" in info and "version-checked" in info
    for name in ("History", "AgentHistory"):
        description = schemas[name]["description"]
        assert "concrete missing fact" in description and "not current task progress" in description
        assert "not mechanically after every window rotation" in description
    assert "AgentInfo" in schemas["History"]["description"]
    assert "Other Agents and parent conversations are inaccessible" in schemas["AgentHistory"]["description"]
    assert "AgentHistory" not in schemas["Agent"]["parameters"]["properties"]["tools"]["items"]["enum"]
    assert "History" not in reg.names(scope="agent")
    assert "AgentHistory" not in reg.names(scope="main")
