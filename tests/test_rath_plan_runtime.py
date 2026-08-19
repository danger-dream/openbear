from __future__ import annotations

import asyncio
import json

from app.db.engine import DB
from app.llm.base import AgentResult
from app.llm.events import ToolCall
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.rath.plan import AgentPlanCoordinator, register_agent_plan_tools
from app.rath.schemas import RathAgentDef
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.base import ToolRegistry

INITIAL_PLAN = {
    "title": "Initial plan",
    "objective": "Complete the delegated runtime task",
    "scope": {"included": ["runtime test"], "excluded": []},
    "assumptions": [],
    "steps": [{
        "id": "s1",
        "title": "Old step",
        "objective": "Execute the original approach",
        "method": "Use the Read tool",
        "dependsOn": [],
        "required": True,
        "criteria": [{"id": "c1", "description": "Original result verified", "required": True}],
        "expectedEvidence": ["tool result"],
    }],
    "finalOutputs": [{
        "id": "o1",
        "title": "Runtime result",
        "description": "Verified output",
        "supportedBy": ["s1"],
    }],
    "risks": [],
}

REPLAN = {
    "title": "Replacement remaining plan",
    "objective": "Complete the task under changed requirements",
    "scope": {"included": ["replacement runtime test"], "excluded": ["old approach"]},
    "assumptions": [],
    "steps": [{
        "id": "r1",
        "title": "Replacement step",
        "objective": "Execute the replacement approach",
        "method": "Read the replacement source and verify it",
        "dependsOn": [],
        "required": True,
        "criteria": [{"id": "rc1", "description": "Replacement result verified", "required": True}],
        "expectedEvidence": ["read result"],
    }],
    "finalOutputs": [{
        "id": "o1",
        "title": "Runtime result",
        "description": "Verified replacement output",
        "supportedBy": ["r1"],
    }],
    "risks": [],
}


class PlanRuntimeBackend:
    protocol = "chat"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.executing_call_started = asyncio.Event()
        self.release_stale_read = asyncio.Event()

    @staticmethod
    def _call(call_id: str, name: str, args: dict) -> AgentResult:
        return AgentResult(
            tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False))],
            finish_reason="tool_calls",
        )

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        names = [str(item.get("name") or "") for item in tools or []]
        runtime_messages = [
            str(item.get("content") or "")
            for item in messages
            if item.get("role") == "user" and "<agent-plan-runtime" in str(item.get("content") or "")
        ]
        self.calls.append({"names": names, "system": system, "messages": list(messages)})
        number = len(self.calls)
        if number == 1:
            assert set(names) == {"AgentPlanSubmit", "AgentControlAck"}
            assert '"phase":"drafting"' not in system
            assert runtime_messages and '"phase":"drafting"' in runtime_messages[-1]
            return self._call("submit-initial", "AgentPlanSubmit", {"plan": INITIAL_PLAN})
        if number == 2:
            assert set(names) == {"Read", "AgentPlanProgress", "AgentPlanReplan", "AgentControlAck"}
            assert '"phase":"executing"' not in system
            assert runtime_messages and '"phase":"executing"' in runtime_messages[-1]
            self.executing_call_started.set()
            await self.release_stale_read.wait()
            # The controller changes the phase while this model request is in flight.
            # Runtime must re-check the durable gate and deny this stale business call.
            return self._call("stale-read", "Read", {"path": "old.txt"})
        if number == 3:
            assert set(names) == {"AgentPlanReplan", "AgentControlAck"}
            denied = [m for m in messages if m.get("role") == "tool" and m.get("name") == "Read"]
            assert denied and "tool_denied_by_plan_phase" in str(denied[-1].get("content") or "")
            return self._call(
                "submit-replan",
                "AgentPlanReplan",
                {"changeReason": "Controller changed requirements", "plan": REPLAN},
            )
        if number == 4:
            assert set(names) == {"Read", "AgentPlanProgress", "AgentPlanReplan", "AgentControlAck"}
            return self._call(
                "start-r1",
                "AgentPlanProgress",
                {"action": "start", "stepId": "r1"},
            )
        if number == 5:
            assert set(names) == {"Read", "AgentPlanProgress", "AgentPlanReplan", "AgentControlAck"}
            return self._call("real-read", "Read", {"path": "replacement.txt"})
        if number == 6:
            return self._call(
                "complete-r1",
                "AgentPlanProgress",
                {
                    "action": "complete",
                    "stepId": "r1",
                    "result": "Replacement source verified",
                    "criteria": [{"id": "rc1", "status": "satisfied"}],
                    "evidence": [{
                        "type": "tool_result",
                        "reference": "Read:replacement.txt",
                        "summary": "Replacement source was read successfully",
                        "criterionId": "rc1",
                    }],
                },
            )
        if number == 7:
            return self._call(
                "finalize-r1",
                "AgentPlanProgress",
                {
                    "action": "finalize",
                    "finalOutputs": [{
                        "id": "o1",
                        "summary": "Replacement runtime flow completed",
                        "sources": ["step:r1"],
                    }],
                },
            )
        if number == 8:
            assert set(names) == {"Read", "AgentPlanProgress", "AgentPlanReplan", "AgentControlAck"}
            assert '"phase":"finalizing"' not in system
            assert runtime_messages and '"phase":"finalizing"' in runtime_messages[-1]
            return AgentResult(text="结论：Plan Runtime 完整流程已通过。")
        raise AssertionError(f"unexpected model call {number}")


async def _wait_phase(coordinator: AgentPlanCoordinator, task_uuid: str, phase: str):
    for _ in range(300):
        snapshot = await coordinator.snapshot(task_uuid)
        if snapshot["state"]["phase"] == phase:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"Plan phase did not become {phase}")


async def _wait_slots(manager: RathTaskManager, expected: int):
    for _ in range(300):
        if manager.execution_slots_in_use == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"execution slots did not become {expected}")


async def test_runtime_enforces_plan_gate_and_rechecks_phase_before_each_tool(tmp_path):
    db = DB(str(tmp_path / "runtime-plan.db"))
    await db.connect()
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    manager = RathTaskManager(dao, max_concurrent_tasks=1)
    coordinator = AgentPlanCoordinator(dao, manager)
    registry = ToolRegistry()
    read_calls: list[str] = []
    notifications: list[dict] = []
    notification_event = asyncio.Event()

    async def plan_notification(payload: dict):
        notifications.append(payload)
        notification_event.set()

    async def read_tool(args):
        path = str(args.get("path") or "")
        read_calls.append(path)
        return f"content:{path}"

    registry.add(
        "Read",
        "Read a test source.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        read_tool,
        visibility={"agent"},
    )
    register_agent_plan_tools(registry, coordinator)
    agent = RathAgentDef(
        agent_key="plan-worker",
        name="Plan worker",
        description="Exercise the Plan runtime",
        system_prompt="Follow the runtime protocol exactly.",
        model="gpt",
        think_level="off",
        tool_allowlist=["Read"],
        workflow_uuid=workflow_uuid,
    )
    task_uuid = await dao.create_task(
        chat_id=1,
        workflow_uuid=workflow_uuid,
        title="Plan runtime integration",
        input_data={
            "instruction": "Run the integration task",
            "agentSnapshot": {"toolAllowlist": ["Read"]},
        },
        parent_session_uuid="conversation-plan-runtime",
        status="queued",
    )
    backend = PlanRuntimeBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=4096,
        tools=registry,
        model_call_limit=20,
        tool_call_limit=1,
        plan_control_call_limit=20,
        task_notification=plan_notification,
        plan_protocol_enabled=True,
    )

    async def run(_task_uuid: str):
        await runner.run()

    task = manager.start(task_uuid, 1, run)
    try:
        first = await _wait_phase(coordinator, task_uuid, "awaiting_plan_decision")
        assert first["state"]["pending_plan_version"] == 1
        await asyncio.wait_for(notification_event.wait(), timeout=2)
        assert len(notifications) == 1
        assert notifications[0]["kind"] == "plan-approval-required"
        assert notifications[0]["requiresDecision"] is True
        assert notifications[0]["expectedPlanVersion"] == 1
        assert notifications[0]["planType"] == "initial"
        assert "reviewPrompt" in notifications[0]
        assert "Run the integration task" in notifications[0]["reviewPrompt"]
        assert "AgentPlanDecision" in notifications[0]["content"]
        await _wait_slots(manager, 0)
        await coordinator.decide(
            task_uuid,
            expected_version=1,
            action="approve",
            request_id="controller-approve-initial",
            reason="Initial Plan is sound",
        )

        await asyncio.wait_for(backend.executing_call_started.wait(), timeout=2)
        requested = await coordinator.decide(
            task_uuid,
            expected_version=1,
            action="request_replan",
            request_id="controller-request-replan",
            reason="Requirements changed before execution",
        )
        assert requested["phase"] == "replan_required"
        backend.release_stale_read.set()

        pending_replan = await _wait_phase(coordinator, task_uuid, "awaiting_replan_decision")
        assert pending_replan["state"]["pending_plan_version"] == 2
        for _ in range(200):
            if len(notifications) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(notifications) == 2
        assert notifications[1]["expectedPlanVersion"] == 2
        assert notifications[1]["planType"] == "replan"
        await _wait_slots(manager, 0)
        await coordinator.decide(
            task_uuid,
            expected_version=2,
            action="approve",
            request_id="controller-approve-replan",
            reason="Replacement Plan covers all remaining work",
        )

        await asyncio.wait_for(task, timeout=3)
        stored = await dao.get_task(task_uuid)
        assert stored is not None
        assert stored.status == "completed"
        assert stored.output["summary"] == "结论：Plan Runtime 完整流程已通过。"
        assert stored.tool_call_count == 6
        assert stored.work_tool_call_count == 1
        assert stored.plan_tool_call_count == 5
        assert read_calls == ["replacement.txt"]
        snapshot = await coordinator.snapshot(task_uuid)
        assert snapshot["state"]["phase"] == "finalizing"
        assert [item["status"] for item in snapshot["versions"]] == ["superseded", "approved"]
        assert {(item["step_id"], item["status"]) for item in snapshot["steps"]} == {
            ("s1", "superseded"),
            ("r1", "completed"),
        }
        assert len(snapshot["evidence"]) == 1
        assert manager.execution_slots_in_use == 0
        assert len(backend.calls) == 8
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await db.close()


async def test_runtime_refuses_final_answer_before_plan_submission(tmp_path):
    db = DB(str(tmp_path / "runtime-final-gate.db"))
    await db.connect()
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    manager = RathTaskManager(dao)
    coordinator = AgentPlanCoordinator(dao, manager)
    registry = ToolRegistry()
    register_agent_plan_tools(registry, coordinator)
    agent = RathAgentDef(
        agent_key="premature-worker",
        name="Premature worker",
        description="Attempts to finish without a Plan",
        system_prompt="Return immediately.",
        model="gpt",
        think_level="off",
        tool_allowlist=[],
        workflow_uuid=workflow_uuid,
    )
    task_uuid = await dao.create_task(
        chat_id=2,
        workflow_uuid=workflow_uuid,
        title="Premature final gate",
        input_data={"instruction": "Try to finish immediately"},
        status="queued",
    )

    class PrematureBackend:
        protocol = "chat"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.calls.append({"messages": list(messages), "tools": list(tools or []), "system": system})
            return AgentResult(text="我已经完成，无需 Plan。")

    backend = PrematureBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=1024,
        tools=registry,
        model_call_limit=2,
        tool_call_limit=5,
        plan_protocol_enabled=True,
    )
    try:
        output = await runner.run()
        assert output["status"] == "needs_openbear_control"
        assert output["reason"] == "agent_task_budget_exceeded"
        assert len(backend.calls) == 2
        assert all(
            {item.get("name") for item in call["tools"]} == {"AgentPlanSubmit", "AgentControlAck"}
            for call in backend.calls
        )
        second_user_messages = [
            str(item.get("content") or "")
            for item in backend.calls[1]["messages"]
            if item.get("role") == "user"
        ]
        assert any("尚未提交初始 Plan" in item for item in second_user_messages)
        stored = await dao.get_task(task_uuid)
        assert stored is not None and stored.status == "needs_openbear_control"
        cur = await db.conn.execute(
            "SELECT COUNT(*) FROM rath_task_plan_versions WHERE task_uuid=?",
            (task_uuid,),
        )
        assert (await cur.fetchone())[0] == 0
    finally:
        await db.close()
