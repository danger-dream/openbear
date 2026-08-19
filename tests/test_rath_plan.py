from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy

import pytest

from app.db.engine import DB
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.rath.plan import AgentPlanCoordinator, PlanError, normalize_plan, register_agent_plan_tools
from app.rath.runner import RathWorkflowRunner
from app.tools.base import ToolRegistry, ToolRuntimeContext


@pytest.fixture
async def env(tmp_path):
    db = DB(str(tmp_path / "plan.db"))
    await db.connect()
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    manager = RathTaskManager(dao, max_concurrent_tasks=1)
    coordinator = AgentPlanCoordinator(dao, manager)
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="Plan task",
        status="running",
        parent_session_uuid="conversation-1",
        run_root_turn_uuid="root-1",
    )
    try:
        yield db, dao, manager, coordinator, task_uuid, workflow_uuid
    finally:
        for task in list(manager._runs.values()):
            task.cancel()
        if manager._runs:
            await asyncio.gather(*manager._runs.values(), return_exceptions=True)
        await db.close()


def sample_plan(*, title: str = "Implement feature", second_step: bool = True):
    steps = [
        {
            "id": "s1",
            "title": "Build core",
            "objective": "Implement the core behavior",
            "method": "Edit code and run focused tests",
            "dependsOn": [],
            "required": True,
            "criteria": [
                {"id": "c1", "description": "Focused test passes", "required": True},
            ],
            "expectedEvidence": ["test output"],
        }
    ]
    if second_step:
        steps.append(
            {
                "id": "s2",
                "title": "Verify integration",
                "objective": "Verify integrated behavior",
                "method": "Run integration tests",
                "dependsOn": ["s1"],
                "required": True,
                "criteria": [
                    {"id": "c2", "description": "Integration test passes", "required": True},
                ],
                "expectedEvidence": ["integration output"],
            }
        )
    return {
        "title": title,
        "objective": "Deliver the requested feature with tests",
        "scope": {"included": ["backend"], "excluded": ["unrelated refactors"]},
        "assumptions": ["The existing test runner is available"],
        "steps": steps,
        "finalOutputs": [
            {
                "id": "o1",
                "title": "Working implementation",
                "description": "Implemented and verified behavior",
                "supportedBy": [steps[-1]["id"]],
            }
        ],
        "risks": ["Migration compatibility"],
    }


def criterion_evidence(criterion_id: str, reference: str = "pytest: passed"):
    return [{
        "type": "test_result",
        "reference": reference,
        "summary": "The required test passed",
        "criterionId": criterion_id,
    }]


async def approve(coordinator, task_uuid: str, version: int, request_id: str = "approve-1"):
    return await coordinator.decide(
        task_uuid,
        expected_version=version,
        action="approve",
        request_id=request_id,
        reason="Plan covers the requested scope",
    )


def test_plan_validation_rejects_duplicate_ids_and_cycles():
    plan = sample_plan()
    plan["steps"][1]["id"] = "s1"
    with pytest.raises(PlanError, match="duplicate step id"):
        normalize_plan(plan)

    plan = sample_plan()
    plan["steps"][0]["dependsOn"] = ["s2"]
    with pytest.raises(PlanError, match="dependency cycle"):
        normalize_plan(plan)

    plan = sample_plan()
    plan["toolRequests"] = [{"name": "Glob", "reason": "scan", "neededForSteps": ["s1"]}]
    with pytest.raises(PlanError, match="unavailable Agent tool"):
        normalize_plan(plan)


async def test_initial_plan_tool_request_is_audited_and_execution_permissions_freeze(env):
    _db, _dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    plan = sample_plan()
    plan["toolRequests"] = [{
        "name": "TaskMemory",
        "reason": "Step s1 needs same-task continuity across an approved execution pause",
        "neededForSteps": ["s1"],
    }]
    submitted = await coordinator.submit_plan(
        task_uuid, plan, request_id="submit-tools", wait_for_decision=False
    )

    with pytest.raises(PlanError, match="requires a reason"):
        await coordinator.decide(
            task_uuid,
            expected_version=submitted["planVersion"],
            action="approve",
            request_id="deny-without-reason",
            granted_tools=[],
        )
    with pytest.raises(PlanError, match="must be requested"):
        await coordinator.decide(
            task_uuid,
            expected_version=submitted["planVersion"],
            action="approve",
            request_id="grant-unrequested",
            reason="invalid grant test",
            granted_tools=["Bash"],
        )

    decision = await coordinator.decide(
        task_uuid,
        expected_version=submitted["planVersion"],
        action="approve",
        request_id="grant-task-memory",
        reason="TaskMemory is necessary and limited to same-task continuity",
        granted_tools=["TaskMemory"],
    )
    assert decision["grantedTools"] == ["TaskMemory"]
    assert decision["approvedTools"] == ["TaskMemory"]
    snapshot = await coordinator.snapshot(task_uuid)
    assert snapshot["state"]["approved_tools"] == ["TaskMemory"]
    assert snapshot["decisions"][-1]["granted_tools"] == ["TaskMemory"]

    await coordinator.decide(
        task_uuid,
        expected_version=submitted["planVersion"],
        action="request_replan",
        request_id="request-replan-tools",
        reason="The remaining method needs revision",
    )
    replan = sample_plan(title="replacement")
    replan["toolRequests"] = [{
        "name": "Bash",
        "reason": "new tool request after execution",
        "neededForSteps": ["s1"],
    }]
    with pytest.raises(PlanError, match="permissions are frozen"):
        await coordinator.submit_plan(
            task_uuid,
            replan,
            request_id="replan-expands-tools",
            plan_type="replan",
            change_reason="The remaining method needs revision",
            wait_for_decision=False,
        )

    replan.pop("toolRequests")
    submitted_replan = await coordinator.submit_plan(
        task_uuid,
        replan,
        request_id="replan-keeps-frozen-tools",
        plan_type="replan",
        change_reason="The remaining method needs revision",
        wait_for_decision=False,
    )
    replan_decision = await coordinator.decide(
        task_uuid,
        expected_version=submitted_replan["planVersion"],
        action="approve",
        request_id="approve-replan-keeps-frozen-tools",
        reason="The replacement Plan stays within the original authorization",
        granted_tools=[],
    )
    assert replan_decision["grantedTools"] == []
    assert replan_decision["approvedTools"] == ["TaskMemory"]
    assert (await coordinator.snapshot(task_uuid))["state"]["approved_tools"] == ["TaskMemory"]


async def test_agent_control_response_is_explicit_and_durable(env):
    _db, dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    control_uuid = await dao.add_control(
        task_uuid,
        "steer",
        message="Stop expanding search and synthesize the satisfied criteria",
        requested_by="AgentMessage",
        metadata={
            "reasonCode": "evidence_sufficient",
            "reason": "All required criteria have durable evidence",
            "criterionIds": ["c1"],
        },
    )
    runner = RathWorkflowRunner(dao, task_uuid)
    await runner.checkpoint("before_model", agent_key="worker")
    assert runner.steers[0]["controlUuid"] == control_uuid

    registry = ToolRegistry()
    register_agent_plan_tools(registry, coordinator)
    context = ToolRuntimeContext(
        chat_id=123,
        session_uuid="conversation-1",
        conversation_uuid="conversation-1",
        source="agent:worker",
        task_uuid=task_uuid,
        agent_key="worker",
        tool_call_id="ack-1",
    )
    result = json.loads(await registry.dispatch(
        "AgentControlAck",
        json.dumps({
            "controlUuid": control_uuid,
            "status": "accepted",
            "reason": "The cited criterion is satisfied",
            "planImpact": "No replan required",
            "nextAction": "Finalize the report",
        }),
        context=context,
    ))
    assert result["ok"] is True
    assert result["status"] == "accepted"
    stored = await dao.control(control_uuid)
    assert stored is not None
    assert stored.response_status == "accepted"
    assert stored.response_plan_impact == "No replan required"
    events = await dao.events(task_uuid)
    assert any(event.kind == "control_response" for event in events)

    replay = json.loads(await registry.dispatch(
        "AgentControlAck",
        json.dumps({"controlUuid": control_uuid, "status": "appeal", "reason": "late appeal"}),
        context=ToolRuntimeContext(
            chat_id=123,
            session_uuid="conversation-1",
            conversation_uuid="conversation-1",
            source="agent:worker",
            task_uuid=task_uuid,
            agent_key="worker",
            tool_call_id="ack-2",
        ),
    ))
    assert replay["idempotent"] is True
    assert replay["status"] == "accepted"


async def test_submit_revise_approve_progress_and_finalize(env):
    _db, _dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    first = await coordinator.submit_plan(
        task_uuid, sample_plan(title="v1"), request_id="submit-1", wait_for_decision=False
    )
    assert first["planVersion"] == 1
    assert (await coordinator.snapshot(task_uuid))["state"]["phase"] == "awaiting_plan_decision"

    revised = await coordinator.decide(
        task_uuid,
        expected_version=1,
        action="revise",
        request_id="revise-1",
        issues=["Clarify verification"],
        reason="Verification needs to be explicit",
        required_changes=["Name the integration verification"],
    )
    assert revised["phase"] == "revising"

    second_plan = sample_plan(title="v2")
    second_plan["steps"][1]["method"] = "Run the named end-to-end integration test"
    second = await coordinator.submit_plan(
        task_uuid, second_plan, request_id="submit-2", wait_for_decision=False
    )
    assert second["planVersion"] == 2
    await approve(coordinator, task_uuid, 2)

    await coordinator.progress(task_uuid, action="start", step_id="s1", request_id="start-s1")
    with pytest.raises(PlanError, match="required criterion"):
        await coordinator.progress(
            task_uuid,
            action="complete",
            step_id="s1",
            request_id="complete-s1-no-evidence",
            result_text="Core implemented",
            criteria=[{"id": "c1", "status": "satisfied"}],
        )
    completed = await coordinator.progress(
        task_uuid,
        action="complete",
        step_id="s1",
        request_id="complete-s1",
        result_text="Core implemented",
        criteria=[{"id": "c1", "status": "satisfied"}],
        evidence=criterion_evidence("c1"),
    )
    assert len(completed["evidence"]) == 1

    await coordinator.progress(task_uuid, action="start", step_id="s2", request_id="start-s2")
    await coordinator.progress(
        task_uuid,
        action="complete",
        step_id="s2",
        request_id="complete-s2",
        result_text="Integration verified",
        criteria=[{"id": "c2", "status": "passed"}],
        evidence=criterion_evidence("c2", "pytest integration: passed"),
    )
    with pytest.raises(PlanError, match="unknown or incomplete source"):
        await coordinator.progress(
            task_uuid,
            action="finalize",
            request_id="finalize-bad-source",
            final_outputs=[{"id": "o1", "summary": "Feature works", "sources": ["step:missing"]}],
        )
    finalized = await coordinator.progress(
        task_uuid,
        action="finalize",
        request_id="finalize-1",
        final_outputs=[{"id": "o1", "summary": "Feature works", "sources": ["step:s2"]}],
    )
    assert finalized["phase"] == "finalizing"

    final_outputs = [{"id": "o1", "summary": "Feature works", "sources": ["step:s2"]}]
    replay = await coordinator.progress(
        task_uuid,
        action="finalize",
        request_id="finalize-1",
        final_outputs=final_outputs,
    )
    assert replay["idempotent"] is True
    with pytest.raises(PlanError) as conflict:
        await coordinator.progress(
            task_uuid,
            action="finalize",
            request_id="finalize-1",
            final_outputs=[],
        )
    assert conflict.value.code == "request_id_conflict"
    snapshot = await coordinator.snapshot(task_uuid)
    assert [v["status"] for v in snapshot["versions"]] == ["revise_requested", "approved"]
    assert snapshot["state"]["active_plan_version"] == 2
    assert snapshot["state"]["final_outputs_state"]["o1"]["sources"] == ["step:s2"]


async def test_progress_request_id_replays_only_exact_fingerprint(env):
    db, _dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    await coordinator.submit_plan(
        task_uuid,
        sample_plan(title="fingerprint v1"),
        request_id="submit-fingerprint-v1",
        wait_for_decision=False,
    )
    await approve(coordinator, task_uuid, 1)

    await db.conn.execute(
        """
        INSERT INTO rath_task_plan_requests
          (task_uuid, request_id, operation, request_fingerprint, result_json, created_at)
        VALUES (?, 'legacy-empty-fingerprint', 'start', '', '{}', 1)
        """,
        (task_uuid,),
    )
    await db.conn.commit()
    with pytest.raises(PlanError) as legacy_conflict:
        await coordinator.progress(
            task_uuid,
            action="start",
            step_id="s1",
            request_id="legacy-empty-fingerprint",
        )
    assert legacy_conflict.value.code == "request_id_conflict"

    started = await coordinator.progress(
        task_uuid,
        action="start",
        step_id="s1",
        request_id="start-shared",
    )
    replay = await coordinator.progress(
        task_uuid,
        action="start",
        step_id="s1",
        request_id="start-shared",
    )
    assert replay["idempotent"] is True
    assert replay["stepId"] == started["stepId"] == "s1"

    with pytest.raises(PlanError) as step_conflict:
        await coordinator.progress(
            task_uuid,
            action="start",
            step_id="s2",
            request_id="start-shared",
        )
    assert step_conflict.value.code == "request_id_conflict"
    with pytest.raises(PlanError) as action_conflict:
        await coordinator.progress(
            task_uuid,
            action="update",
            step_id="s1",
            request_id="start-shared",
            result_text="same version, different action",
        )
    assert action_conflict.value.code == "request_id_conflict"

    update_evidence = [{
        "type": "test",
        "reference": "command:update-a",
        "summary": "update evidence A",
    }]
    updated = await coordinator.progress(
        task_uuid,
        action="update",
        step_id="s1",
        request_id="update-shared",
        result_text="result A",
        evidence=update_evidence,
    )
    update_replay = await coordinator.progress(
        task_uuid,
        action="update",
        step_id="s1",
        request_id="update-shared",
        result_text="result A",
        evidence=update_evidence,
    )
    assert update_replay["idempotent"] is True
    assert update_replay["stepId"] == updated["stepId"] == "s1"
    with pytest.raises(PlanError) as result_conflict:
        await coordinator.progress(
            task_uuid,
            action="update",
            step_id="s1",
            request_id="update-shared",
            result_text="result B",
            evidence=update_evidence,
        )
    assert result_conflict.value.code == "request_id_conflict"
    with pytest.raises(PlanError) as evidence_conflict:
        await coordinator.progress(
            task_uuid,
            action="update",
            step_id="s1",
            request_id="update-shared",
            result_text="result A",
            evidence=[{**update_evidence[0], "summary": "update evidence B"}],
        )
    assert evidence_conflict.value.code == "request_id_conflict"

    await coordinator.progress(
        task_uuid,
        action="complete",
        step_id="s1",
        request_id="complete-v1-s1",
        result_text="v1 s1 complete",
        criteria=[{"id": "c1", "status": "passed"}],
        evidence=criterion_evidence("c1", "v1 s1 evidence"),
    )
    await coordinator.decide(
        task_uuid,
        expected_version=1,
        action="request_replan",
        request_id="request-replan-v1",
        reason="replace the remaining v1 step",
    )
    replacement = sample_plan(title="fingerprint v2", second_step=False)
    replacement["steps"][0].update({
        "id": "r1",
        "title": "Replacement step",
        "criteria": [{"id": "rc1", "description": "replacement verified", "required": True}],
    })
    replacement["finalOutputs"][0]["supportedBy"] = ["r1"]
    await coordinator.submit_plan(
        task_uuid,
        replacement,
        request_id="submit-fingerprint-v2",
        plan_type="replan",
        change_reason="replace the remaining v1 step",
        wait_for_decision=False,
    )
    await approve(coordinator, task_uuid, 2)
    with pytest.raises(PlanError) as version_conflict:
        await coordinator.progress(
            task_uuid,
            action="start",
            step_id="r1",
            request_id="start-shared",
        )
    assert version_conflict.value.code == "request_id_conflict"


async def test_plan_request_fingerprint_column_migrates_legacy_sqlite(tmp_path):
    path = tmp_path / "legacy-plan-requests.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE rath_task_plan_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_uuid TEXT NOT NULL,
          request_id TEXT NOT NULL,
          operation TEXT NOT NULL,
          result_json TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          UNIQUE(task_uuid, request_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO rath_task_plan_requests
          (task_uuid, request_id, operation, result_json, created_at)
        VALUES ('legacy-task', 'legacy-request', 'start', '{}', 1)
        """
    )
    conn.commit()
    conn.close()

    db = DB(str(path))
    await db.connect()
    try:
        cur = await db.conn.execute("PRAGMA table_info(rath_task_plan_requests)")
        assert "request_fingerprint" in {str(row["name"]) for row in await cur.fetchall()}
        cur = await db.conn.execute(
            "SELECT request_fingerprint FROM rath_task_plan_requests WHERE task_uuid='legacy-task'"
        )
        assert str((await cur.fetchone())["request_fingerprint"] or "") == ""
    finally:
        await db.close()


async def test_replan_preserves_completed_history_and_supersedes_old_remaining_steps(env):
    _db, _dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    submitted = await coordinator.submit_plan(
        task_uuid, sample_plan(), request_id="submit-initial", wait_for_decision=False
    )
    await approve(coordinator, task_uuid, submitted["planVersion"])
    await coordinator.progress(task_uuid, action="start", step_id="s1", request_id="start-s1")
    await coordinator.progress(
        task_uuid,
        action="complete",
        step_id="s1",
        request_id="complete-s1",
        result_text="Core complete",
        criteria=[{"id": "c1", "status": "satisfied"}],
        evidence=criterion_evidence("c1"),
    )
    await coordinator.decide(
        task_uuid,
        expected_version=1,
        action="request_replan",
        request_id="request-replan",
        reason="Integration environment changed",
    )

    replan = sample_plan(title="Remaining plan", second_step=False)
    replan["steps"] = [{
        "id": "r2",
        "title": "Verify changed integration",
        "objective": "Verify against the new environment",
        "method": "Run the replacement integration test",
        "dependsOn": ["s1"],
        "required": True,
        "criteria": [{"id": "rc2", "description": "Replacement test passes", "required": True}],
        "expectedEvidence": ["replacement test output"],
    }]
    replan["finalOutputs"][0]["supportedBy"] = ["r2"]
    submitted_replan = await coordinator.submit_plan(
        task_uuid,
        replan,
        request_id="submit-replan",
        plan_type="replan",
        change_reason="Integration environment changed",
        wait_for_decision=False,
    )
    assert submitted_replan["planVersion"] == 2
    await approve(coordinator, task_uuid, 2, "approve-replan")

    snapshot = await coordinator.snapshot(task_uuid)
    by_version_step = {(row["plan_version"], row["step_id"]): row["status"] for row in snapshot["steps"]}
    assert by_version_step[(1, "s1")] == "completed"
    assert by_version_step[(1, "s2")] == "superseded"
    assert by_version_step[(2, "r2")] == "pending"
    assert len(snapshot["evidence"]) == 1

    await coordinator.progress(task_uuid, action="start", step_id="r2", request_id="start-r2")
    await coordinator.progress(
        task_uuid,
        action="complete",
        step_id="r2",
        request_id="complete-r2",
        result_text="Replacement integration verified",
        criteria=[{"id": "rc2", "status": "satisfied"}],
        evidence=criterion_evidence("rc2", "pytest replacement: passed"),
    )
    final = await coordinator.progress(
        task_uuid,
        action="finalize",
        request_id="finalize-replan",
        final_outputs=[{"id": "o1", "summary": "Feature verified", "sources": ["step:r2"]}],
    )
    assert final["phase"] == "finalizing"


async def test_revised_replan_steps_are_superseded_when_replacement_is_submitted(env):
    _db, _dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    await coordinator.submit_plan(
        task_uuid, sample_plan(title="initial"), request_id="submit-initial", wait_for_decision=False
    )
    await approve(coordinator, task_uuid, 1)
    await coordinator.decide(
        task_uuid,
        expected_version=1,
        action="request_replan",
        request_id="request-replan",
        reason="Scope changed",
    )

    replan_v2 = sample_plan(title="replan v2")
    await coordinator.submit_plan(
        task_uuid,
        replan_v2,
        request_id="submit-replan-v2",
        plan_type="replan",
        change_reason="Scope changed",
        wait_for_decision=False,
    )
    await coordinator.decide(
        task_uuid,
        expected_version=2,
        action="revise",
        request_id="revise-replan-v2",
        reason="Dependency direction is wrong",
        required_changes=["Correct the dependency direction"],
    )
    revised_snapshot = await coordinator.snapshot(task_uuid)
    revised_steps = {
        (row["plan_version"], row["step_id"]): row["status"]
        for row in revised_snapshot["steps"]
    }
    assert revised_steps[(2, "s1")] == "superseded"
    assert revised_steps[(2, "s2")] == "superseded"

    replan_v3 = sample_plan(title="replan v3")
    replan_v3["steps"][0]["method"] = "Use the corrected dependency direction"
    submitted = await coordinator.submit_plan(
        task_uuid,
        replan_v3,
        request_id="submit-replan-v3",
        plan_type="replan",
        change_reason="Corrected dependency direction",
        wait_for_decision=False,
    )
    assert submitted["planVersion"] == 3

    snapshot = await coordinator.snapshot(task_uuid)
    by_version_step = {(row["plan_version"], row["step_id"]): row["status"] for row in snapshot["steps"]}
    assert by_version_step[(2, "s1")] == "superseded"
    assert by_version_step[(2, "s2")] == "superseded"
    assert by_version_step[(3, "s1")] == "pending"
    assert by_version_step[(3, "s2")] == "pending"
    assert snapshot["versions"][1]["status"] == "revise_requested"
    assert snapshot["versions"][2]["parent_version"] == 2


async def test_decisions_are_idempotent_and_only_one_concurrent_decision_wins(env):
    _db, _dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    await coordinator.submit_plan(task_uuid, sample_plan(), request_id="submit", wait_for_decision=False)

    results = await asyncio.gather(
        coordinator.decide(
            task_uuid,
            expected_version=1,
            action="approve",
            request_id="decision-a",
            reason="approve",
        ),
        coordinator.decide(
            task_uuid,
            expected_version=1,
            action="cancel",
            request_id="decision-b",
            reason="cancel",
        ),
        return_exceptions=True,
    )
    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], PlanError)

    winner = successes[0]
    replay = await coordinator.decide(
        task_uuid,
        expected_version=1,
        action=winner["action"],
        request_id="decision-a" if winner["action"] == "approve" else "decision-b",
        reason=winner["reason"],
    )
    assert replay["idempotent"] is True
    assert replay["decisionUuid"] == winner["decisionUuid"]


async def test_three_revision_rounds_escalate_to_user_decision(env):
    _db, _dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    for version in range(1, 4):
        await coordinator.submit_plan(
            task_uuid,
            sample_plan(title=f"v{version}"),
            request_id=f"submit-{version}",
            wait_for_decision=False,
        )
        result = await coordinator.decide(
            task_uuid,
            expected_version=version,
            action="revise",
            request_id=f"revise-{version}",
            reason=f"revision {version} required",
            required_changes=[f"change {version}"],
        )
    assert result["waitingForUser"] is True
    assert result["phase"] == "needs_user_decision"

    with pytest.raises(PlanError, match="new user instruction"):
        await coordinator.decide(
            task_uuid,
            expected_version=3,
            action="revise",
            request_id="revise-4-no-user",
            reason="another change",
        )
    user_result = await coordinator.decide(
        task_uuid,
        expected_version=3,
        action="revise",
        request_id="revise-4-user",
        reason="apply the user's ruling",
        required_changes=["user-directed change"],
        user_instruction_id="message-42",
    )
    assert user_result["phase"] == "revising"
    assert user_result["userInstructionId"] == "message-42"


async def test_block_without_other_runnable_step_requests_openbear_control(env):
    _db, dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    await coordinator.submit_plan(
        task_uuid,
        sample_plan(second_step=False),
        request_id="submit-block",
        wait_for_decision=False,
    )
    await approve(coordinator, task_uuid, 1)
    await coordinator.progress(task_uuid, action="start", step_id="s1", request_id="start-block")
    result = await coordinator.progress(
        task_uuid,
        action="block",
        step_id="s1",
        request_id="block-s1",
        blocker={"reason": "Required dependency is unavailable"},
    )
    assert result["status"] == "needs_openbear_control"
    assert result["reason"] == "agent_plan_blocked"
    snapshot = await coordinator.snapshot(task_uuid)
    assert snapshot["state"]["phase"] == "blocked_control"
    stored = await dao.get_task(task_uuid)
    assert stored is not None and stored.status == "running"


async def test_plan_wait_releases_and_reacquires_execution_slot(env):
    _db, _dao, manager, coordinator, task_uuid, workflow_uuid = env
    second_uuid = await _dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="second",
        status="running",
    )
    submitted = asyncio.Event()
    second_acquired = asyncio.Event()
    release_second = asyncio.Event()
    first_resumed = asyncio.Event()

    async def first_runner(_task_uuid: str):
        submitted.set()
        result = await coordinator.submit_plan(
            task_uuid,
            sample_plan(),
            request_id="lease-submit",
            wait_for_decision=True,
        )
        assert result["decision"]["action"] == "approve"
        first_resumed.set()

    async def second_runner(_task_uuid: str):
        second_acquired.set()
        await release_second.wait()

    first_task = manager.start(task_uuid, 123, first_runner)
    await submitted.wait()
    for _ in range(100):
        if manager.execution_slots_in_use == 0:
            break
        await asyncio.sleep(0.01)
    assert manager.execution_slots_in_use == 0

    second_task = manager.start(second_uuid, 123, second_runner)
    await asyncio.wait_for(second_acquired.wait(), timeout=1)
    assert manager.execution_slots_in_use == 1
    await approve(coordinator, task_uuid, 1, "lease-approve")
    await asyncio.sleep(0.05)
    assert not first_resumed.is_set()
    assert not first_task.done()

    release_second.set()
    await asyncio.wait_for(first_resumed.wait(), timeout=1)
    await asyncio.gather(first_task, second_task)
    assert manager.execution_slots_in_use == 0


async def test_decision_before_waiter_registration_resumes_replan_revision(env):
    _db, _dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    await coordinator.submit_plan(task_uuid, sample_plan(), request_id="initial", wait_for_decision=False)
    await approve(coordinator, task_uuid, 1)
    await coordinator.decide(
        task_uuid,
        expected_version=1,
        action="request_replan",
        request_id="request-replan-first",
        reason="requirements changed",
    )
    replacement = sample_plan(title="replacement", second_step=False)
    await coordinator.submit_plan(
        task_uuid,
        replacement,
        request_id="replacement",
        plan_type="replan",
        wait_for_decision=False,
    )
    await coordinator.decide(
        task_uuid,
        expected_version=2,
        action="revise",
        request_id="revise-replan-first",
        reason="replacement needs one change",
    )
    decision = await coordinator.wait_for_decision(task_uuid, 2)
    assert decision["planType"] == "replan"
    assert decision["resumePhase"] == "replan_required"


async def test_delete_task_records_removes_plan_owned_rows(env):
    _db, dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    await coordinator.submit_plan(task_uuid, sample_plan(), request_id="submit-delete", wait_for_decision=False)
    await approve(coordinator, task_uuid, 1)
    await coordinator.progress(task_uuid, action="start", step_id="s1", request_id="start-delete")
    await coordinator.progress(
        task_uuid,
        action="complete",
        step_id="s1",
        request_id="complete-delete",
        result_text="done",
        criteria=[{"id": "c1", "status": "satisfied"}],
        evidence=criterion_evidence("c1"),
    )
    deleted = await dao.delete_task_records([task_uuid])
    await _db.conn.commit()
    assert deleted["tasks"] == 1
    assert deleted["planStates"] == 1
    assert deleted["planVersions"] == 1
    assert deleted["planDecisions"] == 1
    assert deleted["planSteps"] == 2
    assert deleted["planEvidence"] == 1
    assert deleted["planRequests"] == 2
    for table in (
        "rath_task_plan_state",
        "rath_task_plan_versions",
        "rath_task_plan_decisions",
        "rath_task_plan_step_runs",
        "rath_task_plan_evidence",
        "rath_task_plan_requests",
    ):
        cur = await _db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE task_uuid=?", (task_uuid,))
        assert (await cur.fetchone())[0] == 0


async def test_stop_cancels_plan_waiter_without_reacquiring_slot(env):
    _db, _dao, manager, coordinator, task_uuid, _workflow_uuid = env
    entered = asyncio.Event()

    async def runner(_task_uuid: str):
        entered.set()
        await coordinator.submit_plan(
            task_uuid,
            sample_plan(),
            request_id="cancel-submit",
            wait_for_decision=True,
        )

    task = manager.start(task_uuid, 123, runner)
    await entered.wait()
    for _ in range(100):
        if coordinator._waiters:
            break
        await asyncio.sleep(0.01)
    assert coordinator._waiters
    assert manager.execution_slots_in_use == 0

    await manager.stop(task_uuid, message="cancel waiting approval")
    await asyncio.gather(task, return_exceptions=True)
    assert coordinator._waiters == {}
    assert manager.execution_slots_in_use == 0
    stored = await _dao.get_task(task_uuid)
    assert stored is not None and stored.status == "cancelled"


async def test_progress_complete_accepts_documented_criterion_and_evidence_aliases(env):
    _db, _dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    await coordinator.submit_plan(
        task_uuid,
        sample_plan(second_step=False),
        request_id="alias-submit",
        wait_for_decision=False,
    )
    await approve(coordinator, task_uuid, 1, "alias-approve")
    await coordinator.progress(task_uuid, action="start", step_id="s1", request_id="alias-start")
    updated = await coordinator.progress(
        task_uuid,
        action="update",
        step_id="s1",
        request_id="alias-evidence",
        result_text="focused test executed",
        evidence=criterion_evidence("c1", "pytest alias: passed"),
    )
    evidence_uuid = updated["evidence"][0]["evidenceUuid"]

    completed = await coordinator.progress(
        task_uuid,
        action="complete",
        step_id="s1",
        request_id="alias-complete",
        result_text="focused test passed",
        criteria=[{
            "criterionId": "c1",
            "status": "satisfied",
            "evidenceIds": [evidence_uuid],
        }],
    )

    assert completed["ok"] is True
    snapshot = await coordinator.snapshot(task_uuid)
    assert snapshot["steps"][0]["status"] == "completed"
    assert snapshot["steps"][0]["criteria_state"]["c1"]["evidence"] == [evidence_uuid]


async def test_restart_interruption_preserves_plan_steps_and_evidence(env):
    _db, dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    await coordinator.submit_plan(
        task_uuid,
        sample_plan(second_step=False),
        request_id="restart-submit",
        wait_for_decision=False,
    )
    await approve(coordinator, task_uuid, 1, "restart-approve")
    await coordinator.progress(task_uuid, action="start", step_id="s1", request_id="restart-start")
    await coordinator.progress(
        task_uuid,
        action="complete",
        step_id="s1",
        request_id="restart-complete",
        result_text="verified before restart",
        criteria=[{"id": "c1", "status": "satisfied"}],
        evidence=criterion_evidence("c1", "pytest restart retention: passed"),
    )
    before = await coordinator.snapshot(task_uuid)

    assert await dao.mark_interrupted_running() == 1
    stored = await dao.get_task(task_uuid)
    after = await coordinator.snapshot(task_uuid)

    assert stored is not None and stored.status == "interrupted"
    assert after["state"]["active_plan_version"] == before["state"]["active_plan_version"]
    assert after["state"]["final_outputs_state"] == before["state"]["final_outputs_state"]
    assert after["state"]["approved_tools"] == before["state"]["approved_tools"]
    assert after["versions"] == before["versions"]
    assert after["steps"] == before["steps"]
    assert after["evidence"] == before["evidence"]
    assert after["steps"][0]["status"] == "completed"
    assert after["evidence"][0]["reference"] == "pytest restart retention: passed"
    assert after["state"]["phase"] == "interrupted"
    assert after["state"]["current_step_id"] == ""


async def _prepare_completed_and_running_plan(db, dao, coordinator, task_uuid: str) -> dict:
    await coordinator.submit_plan(
        task_uuid,
        sample_plan(),
        request_id=f"terminal-submit-{task_uuid}",
        wait_for_decision=False,
    )
    await approve(coordinator, task_uuid, 1, f"terminal-approve-{task_uuid}")
    await coordinator.progress(
        task_uuid,
        action="start",
        step_id="s1",
        request_id=f"terminal-start-s1-{task_uuid}",
    )
    completed = await coordinator.progress(
        task_uuid,
        action="complete",
        step_id="s1",
        request_id=f"terminal-complete-s1-{task_uuid}",
        result_text="completed evidence must remain durable",
        criteria=[{"id": "c1", "status": "satisfied"}],
        evidence=criterion_evidence("c1", "pytest terminal retention: passed"),
    )
    await coordinator.progress(
        task_uuid,
        action="start",
        step_id="s2",
        request_id=f"terminal-start-s2-{task_uuid}",
    )
    for index, status in enumerate(("pending", "blocked", "superseded", "skipped"), start=1):
        await db.conn.execute(
            """
            INSERT INTO rath_task_plan_step_runs (
              task_uuid, plan_version, step_id, status, result,
              criteria_state_json, blocker_json, updated_at, row_revision
            ) VALUES (?,?,?,?,?,?,?,?,1)
            """,
            (
                task_uuid,
                1,
                f"legacy-{status}",
                status,
                f"preserve-{status}",
                json.dumps({"criterion": status}),
                json.dumps({"reason": status}) if status == "blocked" else "{}",
                100 + index,
            ),
        )
    await db.conn.execute(
        "UPDATE rath_task_plan_state SET final_outputs_state_json=? WHERE task_uuid=?",
        ('{"gate":"preserve"}', task_uuid),
    )
    await db.conn.commit()
    return completed


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "interrupted"])
async def test_task_terminal_cas_atomically_ends_only_running_plan_step_and_rejects_late_progress(env, terminal_status):
    db, dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    completed = await _prepare_completed_and_running_plan(db, dao, coordinator, task_uuid)
    before = await coordinator.snapshot(task_uuid)

    changed = await dao.update_task(
        task_uuid,
        status=terminal_status,
        current_status=f"terminal:{terminal_status}",
        error=f"error:{terminal_status}",
        finish=True,
        expected_statuses=("running",),
    )

    assert changed is True
    stored = await dao.get_task(task_uuid)
    after = await coordinator.snapshot(task_uuid)
    statuses = {row["step_id"]: row["status"] for row in after["steps"]}
    assert stored is not None and stored.status == terminal_status
    assert after["state"]["phase"] == terminal_status
    assert after["state"]["current_step_id"] == ""
    assert statuses["s2"] == terminal_status
    assert statuses["s1"] == "completed"
    for preserved in ("pending", "blocked", "superseded", "skipped"):
        assert statuses[f"legacy-{preserved}"] == preserved
    assert after["steps"][0]["criteria_state"] == before["steps"][0]["criteria_state"]
    assert after["evidence"] == before["evidence"]
    assert after["evidence"][0]["evidence_uuid"] == completed["evidence"][0]["evidenceUuid"]
    assert after["state"]["final_outputs_state"] == {"gate": "preserve"}

    with pytest.raises(PlanError) as late:
        await coordinator.progress(
            task_uuid,
            action="update",
            step_id="s2",
            request_id=f"late-progress-{terminal_status}",
            result_text="must not revive",
        )
    assert late.value.code == "task_not_active"
    repaired = await coordinator.snapshot(task_uuid)
    assert {row["step_id"]: row["status"] for row in repaired["steps"]}["s2"] == terminal_status


async def test_startup_repairs_terminal_task_orphan_running_plan_idempotently(env):
    db, dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    await _prepare_completed_and_running_plan(db, dao, coordinator, task_uuid)
    # Simulate a legacy/crashed write that terminalized only the task row.
    await db.conn.execute(
        "UPDATE rath_tasks SET status='interrupted', finished_at=123, updated_at=123 WHERE task_uuid=?",
        (task_uuid,),
    )
    await db.conn.commit()

    assert await dao.mark_interrupted_running() == 0
    once = await coordinator.snapshot(task_uuid)
    assert once["state"]["phase"] == "interrupted"
    assert once["state"]["current_step_id"] == ""
    assert {row["step_id"]: row["status"] for row in once["steps"]}["s2"] == "interrupted"

    assert await dao.mark_interrupted_running() == 0
    twice = await coordinator.snapshot(task_uuid)
    assert twice == once


async def test_plan_cancel_uses_cancelled_terminal_invariant(env):
    _db, dao, _manager, coordinator, task_uuid, _workflow_uuid = env
    submitted = await coordinator.submit_plan(
        task_uuid,
        sample_plan(),
        request_id="cancel-submit",
        wait_for_decision=False,
    )

    decision = await coordinator.decide(
        task_uuid,
        expected_version=submitted["planVersion"],
        action="cancel",
        request_id="cancel-decision",
        reason="Plan cancelled by controller",
    )

    task = await dao.get_task(task_uuid)
    snapshot = await coordinator.snapshot(task_uuid)
    assert task is not None and task.status == "cancelled"
    assert decision["phase"] == "cancelled"
    assert snapshot["state"]["phase"] == "cancelled"
    assert snapshot["state"]["current_step_id"] == ""
    assert all(row["status"] == "pending" for row in snapshot["steps"])
