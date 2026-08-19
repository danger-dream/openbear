"""Durable Agent Plan state machine, approval waiters and Plan tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import aiosqlite

from app.db.engine import now_ts
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.rath.prompts import render_plan_prompt
from app.rath.schemas import ACTIVE_TASK_STATUSES, TERMINAL_TASK_STATUSES
from app.tools.allowlist import AGENT_DELEGATION_TOOL_NAMES, sanitize_tool_allowlist
from app.tools.base import ToolRegistry, current_tool_context

PLAN_PHASES = {
    "drafting",
    "awaiting_plan_decision",
    "revising",
    "executing",
    "replan_required",
    "awaiting_replan_decision",
    "resume_queued",
    "finalizing",
    "needs_user_decision",
    "blocked_control",
}
PLAN_TOOL_NAMES = {
    "AgentPlanSubmit",
    "AgentPlanDecision",
    "AgentPlanProgress",
    "AgentPlanReplan",
}
PLAN_MAX_BYTES = 128 * 1024


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _uuid() -> str:
    return str(uuid.uuid4())


def _request_fingerprint(
    *, operation: str, plan_version: int, step_id: str, payload: dict[str, Any]
) -> str:
    payload_hash = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    canonical = {
        "operation": str(operation),
        "planVersion": int(plan_version),
        "stepId": str(step_id),
        "payloadHash": payload_hash,
    }
    return hashlib.sha256(_json(canonical).encode("utf-8")).hexdigest()


def _text(value: Any, *, field: str, required: bool = False, limit: int = 4000) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise PlanError("invalid_plan", f"{field} is required")
    if len(text) > limit:
        raise PlanError("invalid_plan", f"{field} exceeds {limit} characters")
    return text


def _string_list(value: Any, *, field: str, limit: int = 100, item_limit: int = 2000) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlanError("invalid_plan", f"{field} must be an array")
    if len(value) > limit:
        raise PlanError("invalid_plan", f"{field} has too many items")
    return [_text(item, field=f"{field}[]", required=True, limit=item_limit) for item in value]


class PlanError(RuntimeError):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def public(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message, **self.detail}


def normalize_plan(
    raw: Any,
    *,
    max_steps: int = 30,
    max_criteria_per_step: int = 10,
    max_final_outputs: int = 20,
    external_step_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Validate and canonicalize an immutable Plan definition."""
    if not isinstance(raw, dict):
        raise PlanError("invalid_plan", "plan must be an object")
    title = _text(raw.get("title"), field="title", required=True, limit=500)
    objective = _text(raw.get("objective"), field="objective", required=True, limit=8000)
    scope = raw.get("scope") or {}
    if not isinstance(scope, dict):
        raise PlanError("invalid_plan", "scope must be an object")
    normalized_scope = {
        "included": _string_list(scope.get("included"), field="scope.included", item_limit=2000),
        "excluded": _string_list(scope.get("excluded"), field="scope.excluded", item_limit=2000),
    }
    assumptions = _string_list(raw.get("assumptions"), field="assumptions", item_limit=3000)
    risks = _string_list(raw.get("risks"), field="risks", item_limit=3000)

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise PlanError("invalid_plan", "steps must be a non-empty array")
    if len(steps_raw) > max_steps:
        raise PlanError("invalid_plan", f"plan exceeds {max_steps} remaining steps")
    steps: list[dict[str, Any]] = []
    step_ids: set[str] = set()
    criterion_ids: set[str] = set()
    for index, item in enumerate(steps_raw):
        if not isinstance(item, dict):
            raise PlanError("invalid_plan", f"steps[{index}] must be an object")
        step_id = _text(item.get("id"), field=f"steps[{index}].id", required=True, limit=120)
        if step_id in step_ids:
            raise PlanError("invalid_plan", f"duplicate step id: {step_id}")
        step_ids.add(step_id)
        criteria_raw = item.get("criteria")
        if criteria_raw is None:
            criteria_raw = item.get("completionCriteria")
        if not isinstance(criteria_raw, list) or not criteria_raw:
            raise PlanError("invalid_plan", f"step {step_id} requires completion criteria")
        if len(criteria_raw) > max_criteria_per_step:
            raise PlanError("invalid_plan", f"step {step_id} exceeds {max_criteria_per_step} criteria")
        criteria: list[dict[str, Any]] = []
        local_criteria: set[str] = set()
        for c_index, criterion in enumerate(criteria_raw):
            if not isinstance(criterion, dict):
                raise PlanError("invalid_plan", f"criterion {step_id}[{c_index}] must be an object")
            criterion_id = _text(
                criterion.get("id"),
                field=f"steps[{index}].criteria[{c_index}].id",
                required=True,
                limit=120,
            )
            if criterion_id in criterion_ids or criterion_id in local_criteria:
                raise PlanError("invalid_plan", f"duplicate criterion id: {criterion_id}")
            criterion_ids.add(criterion_id)
            local_criteria.add(criterion_id)
            criteria.append({
                "id": criterion_id,
                "description": _text(
                    criterion.get("description") or criterion.get("text"),
                    field=f"criterion {criterion_id}.description",
                    required=True,
                    limit=2000,
                ),
                "required": bool(criterion.get("required", True)),
            })
        depends = item.get("dependsOn")
        if depends is None:
            depends = item.get("depends_on")
        steps.append({
            "id": step_id,
            "title": _text(item.get("title"), field=f"step {step_id}.title", required=True, limit=500),
            "objective": _text(item.get("objective"), field=f"step {step_id}.objective", required=True),
            "method": _text(item.get("method"), field=f"step {step_id}.method", required=True),
            "dependsOn": _string_list(depends, field=f"step {step_id}.dependsOn", item_limit=120),
            "required": bool(item.get("required", True)),
            "criteria": criteria,
            "expectedEvidence": _string_list(
                item.get("expectedEvidence"), field=f"step {step_id}.expectedEvidence", item_limit=1000
            ),
        })

    known_dependencies = step_ids | {str(item) for item in external_step_ids if str(item)}
    graph: dict[str, list[str]] = {}
    for step in steps:
        if step["id"] in step["dependsOn"]:
            raise PlanError("invalid_plan", f"step {step['id']} cannot depend on itself")
        unknown = [item for item in step["dependsOn"] if item not in known_dependencies]
        if unknown:
            raise PlanError("invalid_plan", f"step {step['id']} has unknown dependencies", dependencies=unknown)
        graph[step["id"]] = [item for item in step["dependsOn"] if item in step_ids]

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise PlanError("invalid_plan", "step dependency cycle detected", stepId=step_id)
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in graph.get(step_id, []):
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in graph:
        visit(step_id)

    outputs_raw = raw.get("finalOutputs")
    if not isinstance(outputs_raw, list) or not outputs_raw:
        raise PlanError("invalid_plan", "finalOutputs must be a non-empty array")
    if len(outputs_raw) > max_final_outputs:
        raise PlanError("invalid_plan", f"plan exceeds {max_final_outputs} final outputs")
    output_ids: set[str] = set()
    outputs: list[dict[str, Any]] = []
    for index, item in enumerate(outputs_raw):
        if not isinstance(item, dict):
            raise PlanError("invalid_plan", f"finalOutputs[{index}] must be an object")
        output_id = _text(item.get("id"), field=f"finalOutputs[{index}].id", required=True, limit=120)
        if output_id in output_ids:
            raise PlanError("invalid_plan", f"duplicate final output id: {output_id}")
        output_ids.add(output_id)
        supports = item.get("supportedBy")
        if supports is None:
            supports = item.get("sources")
        outputs.append({
            "id": output_id,
            "title": _text(item.get("title"), field=f"output {output_id}.title", required=True, limit=500),
            "description": _text(
                item.get("description"), field=f"output {output_id}.description", required=True, limit=3000
            ),
            "supportedBy": _string_list(supports, field=f"output {output_id}.supportedBy", item_limit=200),
        })

    tool_requests_raw = raw.get("toolRequests") or []
    if not isinstance(tool_requests_raw, list):
        raise PlanError("invalid_plan", "toolRequests must be an array")
    if len(tool_requests_raw) > len(AGENT_DELEGATION_TOOL_NAMES):
        raise PlanError("invalid_plan", "toolRequests contains too many items")
    tool_requests: list[dict[str, Any]] = []
    requested_tool_names: set[str] = set()
    for index, item in enumerate(tool_requests_raw):
        if not isinstance(item, dict):
            raise PlanError("invalid_plan", f"toolRequests[{index}] must be an object")
        name = _text(item.get("name"), field=f"toolRequests[{index}].name", required=True, limit=120)
        if name not in AGENT_DELEGATION_TOOL_NAMES:
            raise PlanError("invalid_plan", f"toolRequests contains unavailable Agent tool: {name}")
        if name in requested_tool_names:
            raise PlanError("invalid_plan", f"duplicate tool request: {name}")
        requested_tool_names.add(name)
        needed_for = _string_list(
            item.get("neededForSteps"), field=f"toolRequests[{index}].neededForSteps", item_limit=120
        )
        unknown_steps = [step_id for step_id in needed_for if step_id not in step_ids]
        if unknown_steps:
            raise PlanError(
                "invalid_plan",
                f"tool request {name} references unknown steps",
                steps=unknown_steps,
            )
        tool_requests.append({
            "name": name,
            "reason": _text(
                item.get("reason"), field=f"toolRequests[{index}].reason", required=True, limit=2000
            ),
            "neededForSteps": needed_for,
        })

    plan = {
        "title": title,
        "objective": objective,
        "scope": normalized_scope,
        "assumptions": assumptions,
        "steps": steps,
        "finalOutputs": outputs,
        "risks": risks,
        "toolRequests": tool_requests,
    }
    if len(_json(plan).encode("utf-8")) > PLAN_MAX_BYTES:
        raise PlanError("invalid_plan", f"plan exceeds {PLAN_MAX_BYTES} bytes")
    return plan


class AgentPlanCoordinator:
    """Own Plan persistence, state transitions and in-process approval waiters."""

    def __init__(
        self,
        dao: RathDAO,
        manager: RathTaskManager,
        *,
        max_revision_rounds: int = 3,
        max_steps: int = 30,
        max_criteria_per_step: int = 10,
        max_final_outputs: int = 20,
        plan_review_prompt: str = "",
    ) -> None:
        self.dao = dao
        self.manager = manager
        self.max_revision_rounds = max(1, int(max_revision_rounds))
        self.max_steps = max(1, int(max_steps))
        self.max_criteria_per_step = max(1, int(max_criteria_per_step))
        self.max_final_outputs = max(1, int(max_final_outputs))
        self.plan_review_prompt = str(plan_review_prompt or "")
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[tuple[str, int], set[asyncio.Future[dict[str, Any]]]] = {}
        manager.set_plan_waiter_canceller(self.cancel_waiter)

    def _lock(self, task_uuid: str) -> asyncio.Lock:
        return self._locks.setdefault(task_uuid, asyncio.Lock())

    async def cancel_waiter(self, task_uuid: str, reason: str = "task stopped") -> None:
        for key, futures in list(self._waiters.items()):
            if key[0] != task_uuid:
                continue
            for future in list(futures):
                if not future.done():
                    future.set_exception(asyncio.CancelledError(reason))
            self._waiters.pop(key, None)

    @staticmethod
    async def _row(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _task_row(self, conn: aiosqlite.Connection, task_uuid: str) -> dict[str, Any]:
        row = await self._row(conn, "SELECT * FROM rath_tasks WHERE task_uuid=?", (task_uuid,))
        if row is None:
            raise PlanError("task_not_found", "Rath task not found", taskUuid=task_uuid)
        return row

    async def _state_row(self, conn: aiosqlite.Connection, task_uuid: str) -> dict[str, Any]:
        await conn.execute(
            """
            INSERT OR IGNORE INTO rath_task_plan_state
              (task_uuid, phase, row_revision, updated_at)
            VALUES (?, 'drafting', 1, ?)
            """,
            (task_uuid, now_ts()),
        )
        row = await self._row(conn, "SELECT * FROM rath_task_plan_state WHERE task_uuid=?", (task_uuid,))
        assert row is not None
        return row

    @staticmethod
    def _assert_active_task(task: dict[str, Any]) -> None:
        status = str(task.get("status") or "")
        if status in TERMINAL_TASK_STATUSES or status in {"stopping", "needs_openbear_control"}:
            raise PlanError("task_not_active", f"task is not active: {status}", taskStatus=status)

    async def queue_intervention(
        self,
        task_uuid: str,
        *,
        message: str,
        requested_by: str,
        metadata: dict[str, Any] | None = None,
        expected_plan_version: int = 0,
    ) -> dict[str, Any]:
        """Atomically validate Plan governance and enqueue one steer control."""
        message = _text(message, field="message", required=True, limit=20000)
        requested_by = _text(requested_by, field="requestedBy", limit=300)
        expected_plan_version = int(expected_plan_version or 0)
        metadata = dict(metadata or {})
        control_uuid = _uuid()
        async with self._lock(task_uuid):
            async with self.dao.db.plan_transaction() as conn:
                task = await self._task_row(conn, task_uuid)
                state = await self._row(
                    conn,
                    "SELECT * FROM rath_task_plan_state WHERE task_uuid=?",
                    (task_uuid,),
                )
                phase = str((state or {}).get("phase") or "")
                active_version = int((state or {}).get("active_plan_version") or 0)
                pending_version = int((state or {}).get("pending_plan_version") or 0)
                visible_version = (
                    active_version
                    if phase == "replan_required"
                    else (pending_version or active_version)
                )

                # blocked_control is authoritative even while the task row is still
                # running or its derived output reason has already drifted.
                if phase == "blocked_control":
                    raise PlanError(
                        "plan_replan_required",
                        (
                            "该 Agent 的已批准 Plan 仍处于 blocked_control。先对当前 active Plan 调用 "
                            "AgentPlanDecision(action=request_replan)，再用 AgentMessage 恢复并让 Agent提交 "
                            "AgentPlanReplan；不能用控制消息直接解除 blocked step。"
                        ),
                        taskUuid=task_uuid,
                        taskStatus=str(task.get("status") or ""),
                        reason="agent_plan_blocked",
                        planPhase=phase,
                        activePlanVersion=active_version,
                        pendingPlanVersion=pending_version,
                        visiblePlanVersion=active_version,
                    )

                if visible_version and expected_plan_version != visible_version:
                    raise PlanError(
                        "stale_plan_version",
                        "expectedPlanVersion does not match the task's current visible Plan version",
                        taskUuid=task_uuid,
                        taskStatus=str(task.get("status") or ""),
                        planPhase=phase,
                        activePlanVersion=active_version,
                        pendingPlanVersion=pending_version,
                        visiblePlanVersion=visible_version,
                    )

                if phase in {
                    "awaiting_plan_decision",
                    "awaiting_replan_decision",
                    "needs_user_decision",
                    "finalizing",
                }:
                    raise PlanError(
                        "plan_intervention_not_allowed",
                        f"AgentMessage is not allowed while Plan phase is {phase}",
                        taskUuid=task_uuid,
                        taskStatus=str(task.get("status") or ""),
                        planPhase=phase,
                        activePlanVersion=active_version,
                        pendingPlanVersion=pending_version,
                        visiblePlanVersion=visible_version,
                    )

                task_status = str(task.get("status") or "")
                if task_status not in {*ACTIVE_TASK_STATUSES, "needs_openbear_control"}:
                    raise PlanError(
                        "agent_task_not_messageable",
                        f"Agent task is not messageable: {task_status}",
                        taskUuid=task_uuid,
                        taskStatus=task_status,
                    )
                if task_status == "needs_openbear_control":
                    output = _loads(task.get("output_json"), {})
                    detail = output.get("detail") if isinstance(output, dict) else {}
                    reason = str(output.get("reason") or "") if isinstance(output, dict) else ""
                    continuable = detail.get("continuable") if isinstance(detail, dict) else None
                    if reason == "agent_context_overflow_unrecoverable" or continuable is False:
                        raise PlanError(
                            "agent_task_not_continuable",
                            "该 task 的原上下文已无法安全送入模型；请 AgentStop 后用更窄的 prompt 新建 Agent task。",
                            taskUuid=task_uuid,
                            taskStatus=task_status,
                            reason=reason or "continuation_not_supported",
                        )

                await conn.execute(
                    """
                    INSERT INTO rath_task_controls (
                      control_uuid, task_uuid, action, message, requested_by, status, created_at, metadata_json
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        control_uuid,
                        task_uuid,
                        "steer",
                        message,
                        requested_by,
                        "pending",
                        now_ts(),
                        _json(metadata),
                    ),
                )
        await self.dao.append_event(
            task_uuid,
            "control_requested",
            summary="控制请求：steer",
            detail={
                "controlUuid": control_uuid,
                "action": "steer",
                "message": message,
                "requestedBy": requested_by,
                "metadata": metadata,
            },
        )
        return {
            "ok": True,
            "taskUuid": task_uuid,
            "taskStatus": task_status,
            "controlUuid": control_uuid,
            "planPhase": phase,
            "activePlanVersion": active_version,
            "pendingPlanVersion": pending_version,
            "visiblePlanVersion": visible_version,
        }

    async def _completed_step_ids(self, conn: aiosqlite.Connection, task_uuid: str) -> set[str]:
        cur = await conn.execute(
            "SELECT DISTINCT step_id FROM rath_task_plan_step_runs WHERE task_uuid=? AND status='completed'",
            (task_uuid,),
        )
        return {str(row[0]) for row in await cur.fetchall()}

    async def submit_plan(
        self,
        task_uuid: str,
        raw_plan: dict[str, Any],
        *,
        request_id: str,
        plan_type: str = "initial",
        change_reason: str = "",
        wait_for_decision: bool = True,
        on_submitted: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        request_id = _text(request_id, field="requestId", required=True, limit=300)
        if plan_type not in {"initial", "replan"}:
            raise PlanError("invalid_plan_type", "planType must be initial or replan")
        external = set()
        if plan_type == "replan":
            async with self.dao.db.plan_transaction() as conn:
                external = await self._completed_step_ids(conn, task_uuid)
        plan = normalize_plan(
            raw_plan,
            max_steps=self.max_steps,
            max_criteria_per_step=self.max_criteria_per_step,
            max_final_outputs=self.max_final_outputs,
            external_step_ids=external,
        )
        plan_json = _json(plan)
        plan_hash = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        created = False
        async with self._lock(task_uuid):
            async with self.dao.db.plan_transaction() as conn:
                task = await self._task_row(conn, task_uuid)
                self._assert_active_task(task)
                state = await self._state_row(conn, task_uuid)
                task_input = _loads(task.get("input_json"), {})
                snapshot = task_input.get("agentSnapshot") if isinstance(task_input, dict) else {}
                initial_tools = sanitize_tool_allowlist(
                    snapshot.get("toolAllowlist") if isinstance(snapshot, dict) else []
                )
                requested_tools = [
                    str(item.get("name") or "") for item in plan.get("toolRequests") or []
                ]
                already_initialized = [name for name in requested_tools if name in initial_tools]
                if already_initialized:
                    raise PlanError(
                        "tool_already_initialized",
                        "toolRequests must contain only additional tools not already granted at Agent launch",
                        tools=already_initialized,
                    )
                if plan_type == "replan":
                    approved_tools = set(_loads(state.get("approved_tools_json"), []))
                    unavailable = [name for name in requested_tools if name not in approved_tools]
                    if unavailable:
                        raise PlanError(
                            "tool_expansion_locked",
                            "tool permissions are frozen after initial Plan approval; replan cannot request new tools",
                            tools=unavailable,
                            approvedTools=sorted(approved_tools),
                        )
                existing = await self._row(
                    conn,
                    "SELECT * FROM rath_task_plan_versions WHERE task_uuid=? AND submit_request_id=?",
                    (task_uuid, request_id),
                )
                if existing is None:
                    existing = await self._row(
                        conn,
                        """
                        SELECT * FROM rath_task_plan_versions
                        WHERE task_uuid=? AND plan_hash=? AND status IN ('pending','approved')
                        ORDER BY version DESC LIMIT 1
                        """,
                        (task_uuid, plan_hash),
                    )
                if existing is not None:
                    version = int(existing["version"])
                    if str(existing["plan_json"]) != plan_json:
                        raise PlanError("request_id_conflict", "requestId already belongs to a different plan")
                    result = {
                        "ok": True,
                        "taskUuid": task_uuid,
                        "planVersion": version,
                        "planType": str(existing["plan_type"]),
                        "status": str(existing["status"]),
                        "idempotent": True,
                    }
                else:
                    phase = str(state["phase"] or "drafting")
                    allowed = {"drafting", "revising"} if plan_type == "initial" else {"executing", "replan_required"}
                    if phase not in allowed:
                        raise PlanError(
                            "invalid_plan_phase",
                            f"cannot submit {plan_type} plan while phase is {phase}",
                            phase=phase,
                        )
                    if plan_type == "initial" and int(state["active_plan_version"] or 0):
                        raise PlanError("initial_plan_exists", "task already has an approved initial plan")
                    if plan_type == "replan" and not int(state["active_plan_version"] or 0):
                        raise PlanError("approved_plan_required", "replan requires an approved active plan")
                    cur = await conn.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 FROM rath_task_plan_versions WHERE task_uuid=?",
                        (task_uuid,),
                    )
                    version = int((await cur.fetchone())[0])
                    parent_version = int(state["active_plan_version"] or 0)
                    previous = await self._row(
                        conn,
                        "SELECT * FROM rath_task_plan_versions WHERE task_uuid=? ORDER BY version DESC LIMIT 1",
                        (task_uuid,),
                    )
                    revised_same_cycle = bool(
                        previous
                        and str(previous["plan_type"]) == plan_type
                        and str(previous["status"]) == "revise_requested"
                    )
                    if revised_same_cycle:
                        parent_version = int(previous["version"])
                        await conn.execute(
                            """
                            UPDATE rath_task_plan_step_runs
                            SET status='superseded', updated_at=?, row_revision=row_revision+1
                            WHERE task_uuid=? AND plan_version=?
                              AND status IN ('pending','running','blocked','skipped')
                            """,
                            (now_ts(), task_uuid, parent_version),
                        )
                    approval_cycle = int(state["approval_cycle"] or 0)
                    revision_count = int(state["revision_count"] or 0)
                    if not revised_same_cycle:
                        approval_cycle += 1
                        revision_count = 0
                    await conn.execute(
                        """
                        INSERT INTO rath_task_plan_versions
                          (task_uuid, version, plan_type, parent_version, status, plan_json,
                           plan_hash, change_reason, submit_request_id, submitted_at, decided_at)
                        VALUES (?,?,?,?, 'pending', ?,?,?,?,?,0)
                        """,
                        (
                            task_uuid,
                            version,
                            plan_type,
                            parent_version,
                            plan_json,
                            plan_hash,
                            _text(change_reason, field="changeReason", limit=8000),
                            request_id,
                            now_ts(),
                        ),
                    )
                    ts = now_ts()
                    for step in plan["steps"]:
                        await conn.execute(
                            """
                            INSERT INTO rath_task_plan_step_runs
                              (task_uuid, plan_version, step_id, status, updated_at, row_revision)
                            VALUES (?, ?, ?, 'pending', ?, 1)
                            """,
                            (task_uuid, version, step["id"], ts),
                        )
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_state
                        SET phase=?, pending_plan_version=?, current_step_id='',
                            approval_cycle=?, revision_count=?, row_revision=row_revision+1, updated_at=?
                        WHERE task_uuid=?
                        """,
                        (
                            "awaiting_plan_decision" if plan_type == "initial" else "awaiting_replan_decision",
                            version,
                            approval_cycle,
                            revision_count,
                            ts,
                            task_uuid,
                        ),
                    )
                    created = True
                    result = {
                        "ok": True,
                        "taskUuid": task_uuid,
                        "planVersion": version,
                        "planType": plan_type,
                        "status": "pending",
                        "idempotent": False,
                    }
        if created:
            await self.dao.append_event(
                task_uuid,
                "plan_submitted" if plan_type == "initial" else "plan_replan_submitted",
                summary=f"Agent submitted {plan_type} Plan v{version}",
                detail={"planVersion": version, "planType": plan_type, "planHash": plan_hash},
            )
        if on_submitted is not None and result["status"] in {"pending", "revise_requested"}:
            try:
                await on_submitted(dict(result))
            except Exception as exc:
                await self.dao.append_event(
                    task_uuid,
                    "plan_notification_failed",
                    summary=f"Plan v{version} review notification failed",
                    detail={"error": f"{type(exc).__name__}: {exc}"},
                )
        if not wait_for_decision or result["status"] == "approved":
            return result
        decision = await self.wait_for_decision(task_uuid, version)
        return {**result, "status": "decided", "decision": decision}

    async def _decision_after_wait_started(self, task_uuid: str, version: int) -> dict[str, Any] | None:
        async with self.dao.db.plan_transaction() as conn:
            state = await self._state_row(conn, task_uuid)
            if str(state["phase"]) == "needs_user_decision":
                return None
            version_row = await self._row(
                conn,
                "SELECT status, plan_type FROM rath_task_plan_versions WHERE task_uuid=? AND version=?",
                (task_uuid, version),
            )
            if version_row is None or str(version_row["status"]) == "pending":
                return None
            decision = await self._row(
                conn,
                """
                SELECT * FROM rath_task_plan_decisions
                WHERE task_uuid=? AND expected_version=? AND action IN ('approve','revise','cancel')
                ORDER BY id DESC LIMIT 1
                """,
                (task_uuid, version),
            )
            if decision is None:
                return None
            decision["plan_type"] = str(version_row["plan_type"])
            return self._decision_public(decision, state)

    async def wait_for_decision(self, task_uuid: str, version: int) -> dict[str, Any]:
        immediate = await self._decision_after_wait_started(task_uuid, version)
        if immediate is not None:
            return immediate
        released = await self.manager.release_execution_slot(task_uuid)
        key = (task_uuid, int(version))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, set()).add(future)
        try:
            immediate = await self._decision_after_wait_started(task_uuid, version)
            decision = immediate if immediate is not None else await future
            if str(decision.get("action")) == "cancel":
                return decision
            if released:
                resume_phase = str(decision.get("resumePhase") or "executing")
                async with self.dao.db.plan_transaction() as conn:
                    task = await self._task_row(conn, task_uuid)
                    self._assert_active_task(task)
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_state SET phase='resume_queued', row_revision=row_revision+1,
                          updated_at=? WHERE task_uuid=?
                        """,
                        (now_ts(), task_uuid),
                    )
                await self.manager.acquire_execution_slot(task_uuid)
                async with self.dao.db.plan_transaction() as conn:
                    task = await self._task_row(conn, task_uuid)
                    self._assert_active_task(task)
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_state SET phase=?, row_revision=row_revision+1,
                          updated_at=? WHERE task_uuid=? AND phase='resume_queued'
                        """,
                        (resume_phase, now_ts(), task_uuid),
                    )
            return decision
        finally:
            futures = self._waiters.get(key)
            if futures is not None:
                futures.discard(future)
                if not futures:
                    self._waiters.pop(key, None)

    @staticmethod
    def _decision_public(row: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        action = str(row["action"])
        plan_type = str(row.get("plan_type") or "initial")
        resume_phase = "executing"
        if action == "revise":
            resume_phase = "revising" if plan_type == "initial" else "replan_required"
        elif action == "cancel":
            resume_phase = "blocked_control"
        elif action == "request_replan":
            resume_phase = "replan_required"
        return {
            "decisionUuid": str(row["decision_uuid"]),
            "taskUuid": str(row["task_uuid"]),
            "planVersion": int(row["expected_version"]),
            "action": action,
            "issues": _loads(row.get("issues_json"), []),
            "reason": str(row.get("reason") or ""),
            "requiredChanges": _loads(row.get("required_changes_json"), []),
            "grantedTools": _loads(row.get("granted_tools_json"), []),
            "approvedTools": _loads(state.get("approved_tools_json"), []),
            "requestedBy": str(row.get("requested_by") or ""),
            "userInstructionId": str(row.get("user_instruction_id") or ""),
            "phase": str(state.get("phase") or ""),
            "resumePhase": resume_phase,
            "planType": plan_type,
        }

    def _resolve_waiters(self, task_uuid: str, version: int, result: dict[str, Any]) -> None:
        for future in list(self._waiters.get((task_uuid, int(version)), set())):
            if not future.done():
                future.set_result(dict(result))

    async def decide(
        self,
        task_uuid: str,
        *,
        expected_version: int,
        action: str,
        request_id: str,
        issues: list[Any] | None = None,
        reason: str = "",
        required_changes: list[Any] | None = None,
        granted_tools: list[Any] | None = None,
        requested_by: str = "main-controller",
        user_instruction_id: str = "",
    ) -> dict[str, Any]:
        action = str(action or "").strip()
        if action not in {"approve", "revise", "cancel", "request_replan"}:
            raise PlanError("invalid_decision", "unsupported Plan decision action", action=action)
        request_id = _text(request_id, field="requestId", required=True, limit=300)
        expected_version = int(expected_version or 0)
        if expected_version <= 0:
            raise PlanError("invalid_version", "expectedPlanVersion must be positive")
        issues = list(issues or [])
        required_changes = list(required_changes or [])
        granted_tools = sanitize_tool_allowlist(granted_tools or [])
        reason = _text(reason, field="reason", required=action in {"revise", "request_replan"}, limit=8000)
        user_instruction_id = _text(user_instruction_id, field="userInstructionId", limit=300)
        should_resolve = False
        async with self._lock(task_uuid):
            async with self.dao.db.plan_transaction() as conn:
                duplicate = await self._row(
                    conn,
                    """
                    SELECT d.*, v.plan_type
                    FROM rath_task_plan_decisions d
                    LEFT JOIN rath_task_plan_versions v
                      ON v.task_uuid=d.task_uuid AND v.version=d.expected_version
                    WHERE d.task_uuid=? AND d.request_id=?
                    """,
                    (task_uuid, request_id),
                )
                if duplicate is not None:
                    state = await self._state_row(conn, task_uuid)
                    result = self._decision_public(duplicate, state)
                    result.update({"ok": True, "idempotent": True})
                    return result
                task = await self._task_row(conn, task_uuid)
                if not (
                    action == "request_replan"
                    and str(task.get("status") or "") == "needs_openbear_control"
                ):
                    self._assert_active_task(task)
                state = await self._state_row(conn, task_uuid)
                phase = str(state["phase"])
                if phase == "needs_user_decision" and action != "cancel" and not user_instruction_id:
                    raise PlanError(
                        "user_instruction_required",
                        "this Plan cycle is waiting for a new user instruction/ruling",
                        revisionCount=int(state["revision_count"] or 0),
                    )
                active_version = int(state["active_plan_version"] or 0)
                pending_version = int(state["pending_plan_version"] or 0)
                if action == "request_replan":
                    if phase not in {"executing", "blocked_control"} or active_version != expected_version:
                        raise PlanError(
                            "stale_plan_version",
                            "request_replan must target the active executing Plan",
                            phase=phase,
                            activePlanVersion=active_version,
                            pendingPlanVersion=pending_version,
                        )
                    version_row = await self._row(
                        conn,
                        "SELECT * FROM rath_task_plan_versions WHERE task_uuid=? AND version=?",
                        (task_uuid, expected_version),
                    )
                    assert version_row is not None
                else:
                    if pending_version != expected_version or phase not in {
                        "awaiting_plan_decision",
                        "awaiting_replan_decision",
                        "needs_user_decision",
                    }:
                        raise PlanError(
                            "stale_plan_version",
                            "decision does not match the current pending Plan",
                            phase=phase,
                            activePlanVersion=active_version,
                            pendingPlanVersion=pending_version,
                        )
                    version_row = await self._row(
                        conn,
                        "SELECT * FROM rath_task_plan_versions WHERE task_uuid=? AND version=?",
                        (task_uuid, expected_version),
                    )
                    if version_row is None or str(version_row["status"]) not in {"pending", "revise_requested"}:
                        raise PlanError("plan_already_decided", "Plan version can no longer be decided")
                task_input = _loads(task.get("input_json"), {})
                agent_snapshot = task_input.get("agentSnapshot") if isinstance(task_input, dict) else {}
                initial_tools = sanitize_tool_allowlist(
                    agent_snapshot.get("toolAllowlist") if isinstance(agent_snapshot, dict) else []
                )
                plan_definition = _loads(version_row.get("plan_json"), {})
                requested_tools = [
                    str(item.get("name") or "")
                    for item in (plan_definition.get("toolRequests") or [])
                    if isinstance(item, dict) and str(item.get("name") or "")
                ]
                if action != "approve" and granted_tools:
                    raise PlanError(
                        "tool_grants_require_approval",
                        "grantedTools is valid only for action=approve",
                    )
                if action == "approve":
                    if active_version:
                        # A Replan replaces only the remaining execution method.
                        # It never repeats or renegotiates the initial tool grant;
                        # approving it must carry the already-frozen set forward.
                        if granted_tools:
                            raise PlanError(
                                "tool_expansion_locked",
                                "grantedTools is valid only for the initial Plan approval; Replan permissions are frozen",
                            )
                        legacy_initial_grant = await self._row(
                            conn,
                            """
                            SELECT d.granted_tools_json
                            FROM rath_task_plan_decisions d
                            JOIN rath_task_plan_versions v
                              ON v.task_uuid=d.task_uuid AND v.version=d.expected_version
                            WHERE d.task_uuid=? AND d.action='approve' AND v.plan_type='initial'
                            ORDER BY d.id ASC LIMIT 1
                            """,
                            (task_uuid,),
                        )
                        legacy_granted_tools = sanitize_tool_allowlist(
                            _loads(
                                legacy_initial_grant.get("granted_tools_json")
                                if legacy_initial_grant is not None else "",
                                [],
                            )
                        )
                        approved_tools = sanitize_tool_allowlist([
                            *initial_tools,
                            *_loads(state.get("approved_tools_json"), []),
                            *legacy_granted_tools,
                        ])
                    else:
                        unavailable_grants = [
                            name
                            for name in granted_tools
                            if name not in AGENT_DELEGATION_TOOL_NAMES or name not in requested_tools
                        ]
                        if unavailable_grants:
                            raise PlanError(
                                "invalid_tool_grant",
                                "grantedTools must be requested by this Plan and belong to the Agent base whitelist",
                                tools=unavailable_grants,
                                requestedTools=requested_tools,
                            )
                        denied_requests = [name for name in requested_tools if name not in granted_tools]
                        if denied_requests and not reason:
                            raise PlanError(
                                "tool_decision_reason_required",
                                "approving a Plan while denying requested tools requires a reason",
                                deniedTools=denied_requests,
                            )
                        approved_tools = sanitize_tool_allowlist([*initial_tools, *granted_tools])
                else:
                    approved_tools = sanitize_tool_allowlist(_loads(state.get("approved_tools_json"), []))
                decision_uuid = _uuid()
                decision_ts = now_ts()
                await conn.execute(
                    """
                    INSERT INTO rath_task_plan_decisions
                      (decision_uuid, task_uuid, expected_version, action, issues_json, reason,
                       required_changes_json, granted_tools_json, requested_by, user_instruction_id, request_id, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        decision_uuid,
                        task_uuid,
                        expected_version,
                        action,
                        _json(issues),
                        reason,
                        _json(required_changes),
                        _json(granted_tools),
                        requested_by,
                        user_instruction_id,
                        request_id,
                        decision_ts,
                    ),
                )
                await conn.execute(
                    """
                    UPDATE web_task_notifications
                    SET state='delivered', claim_token='', delivered_at=?, updated_at=?, last_error=''
                    WHERE task_uuid=? AND kind='plan-approval-required'
                      AND CAST(json_extract(payload_json, '$.expectedPlanVersion') AS INTEGER)=?
                      AND state NOT IN ('delivered','suppressed')
                    """,
                    (decision_ts, decision_ts, task_uuid, expected_version),
                )
                ts = decision_ts
                plan_type = str(version_row["plan_type"])
                if action == "request_replan":
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_state
                        SET phase='replan_required', last_controller_guidance=?,
                            row_revision=row_revision+1, updated_at=? WHERE task_uuid=?
                        """,
                        (reason, ts, task_uuid),
                    )
                    resume_phase = "replan_required"
                elif action == "approve":
                    if active_version and active_version != expected_version:
                        await conn.execute(
                            """
                            UPDATE rath_task_plan_versions SET status='superseded', decided_at=?
                            WHERE task_uuid=? AND version=? AND status='approved'
                            """,
                            (ts, task_uuid, active_version),
                        )
                        await conn.execute(
                            """
                            UPDATE rath_task_plan_step_runs
                            SET status='superseded', updated_at=?, row_revision=row_revision+1
                            WHERE task_uuid=? AND plan_version=? AND status IN ('pending','running','blocked')
                            """,
                            (ts, task_uuid, active_version),
                        )
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_versions SET status='approved', decided_at=?
                        WHERE task_uuid=? AND version=?
                        """,
                        (ts, task_uuid, expected_version),
                    )
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_state
                        SET phase='executing', active_plan_version=?, pending_plan_version=0,
                            current_step_id='', approved_tools_json=?, last_controller_guidance=?,
                            row_revision=row_revision+1, updated_at=? WHERE task_uuid=?
                        """,
                        (expected_version, _json(approved_tools), reason, ts, task_uuid),
                    )
                    resume_phase = "executing"
                    should_resolve = True
                elif action == "revise":
                    revision_count = int(state["revision_count"] or 0) + 1
                    if int(state["revision_count"] or 0) >= self.max_revision_rounds and not user_instruction_id:
                        raise PlanError(
                            "user_instruction_required",
                            "further revise decisions require a new user instruction",
                            revisionCount=int(state["revision_count"] or 0),
                        )
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_versions SET status='revise_requested', decided_at=?
                        WHERE task_uuid=? AND version=?
                        """,
                        (ts, task_uuid, expected_version),
                    )
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_step_runs
                        SET status='superseded', updated_at=?, row_revision=row_revision+1
                        WHERE task_uuid=? AND plan_version=?
                          AND status IN ('pending','running','blocked')
                        """,
                        (ts, task_uuid, expected_version),
                    )
                    waiting_user = revision_count >= self.max_revision_rounds and not user_instruction_id
                    next_phase = (
                        "needs_user_decision"
                        if waiting_user
                        else ("revising" if plan_type == "initial" else "replan_required")
                    )
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_state
                        SET phase=?, pending_plan_version=?, revision_count=?,
                            last_controller_guidance=?, row_revision=row_revision+1, updated_at=?
                        WHERE task_uuid=?
                        """,
                        (
                            next_phase,
                            expected_version if waiting_user else 0,
                            revision_count,
                            reason,
                            ts,
                            task_uuid,
                        ),
                    )
                    resume_phase = next_phase
                    should_resolve = not waiting_user
                else:  # cancel
                    await conn.execute(
                        "UPDATE rath_task_plan_versions SET status='cancelled', decided_at=? WHERE task_uuid=? AND version=?",
                        (ts, task_uuid, expected_version),
                    )
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_state
                        SET phase='cancelled', pending_plan_version=0, current_step_id='',
                            last_controller_guidance=?, row_revision=row_revision+1, updated_at=?
                        WHERE task_uuid=?
                        """,
                        (reason, ts, task_uuid),
                    )
                    await conn.execute(
                        """
                        UPDATE rath_tasks SET status='cancelled', control_state='', current_status=?,
                          updated_at=?, finished_at=?
                        WHERE task_uuid=? AND status NOT IN ('completed','failed','cancelled','interrupted')
                        """,
                        (reason or "Plan cancelled", ts, ts, task_uuid),
                    )
                    await self.dao.finalize_task_plan_terminal(
                        conn,
                        task_uuid,
                        "cancelled",
                        updated_at=ts,
                    )
                    resume_phase = "cancelled"
                    should_resolve = True
                result = {
                    "ok": True,
                    "idempotent": False,
                    "decisionUuid": decision_uuid,
                    "taskUuid": task_uuid,
                    "planVersion": expected_version,
                    "planType": plan_type,
                    "action": action,
                    "issues": issues,
                    "reason": reason,
                    "requiredChanges": required_changes,
                    "grantedTools": granted_tools,
                    "approvedTools": approved_tools,
                    "requestedBy": requested_by,
                    "userInstructionId": user_instruction_id,
                    "phase": resume_phase,
                    "resumePhase": resume_phase,
                    "waitingForUser": action == "revise" and not should_resolve,
                }
        await self.dao.append_event(
            task_uuid,
            f"plan_{action}",
            summary=f"Plan v{expected_version}: {action}",
            detail={k: v for k, v in result.items() if k not in {"ok", "idempotent"}},
        )
        if should_resolve:
            self._resolve_waiters(task_uuid, expected_version, result)
        return result

    async def _version_plan(
        self, conn: aiosqlite.Connection, task_uuid: str, version: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        row = await self._row(
            conn,
            "SELECT * FROM rath_task_plan_versions WHERE task_uuid=? AND version=?",
            (task_uuid, version),
        )
        if row is None:
            raise PlanError("plan_not_found", "Plan version not found", planVersion=version)
        return row, _loads(row["plan_json"], {})

    @staticmethod
    def _step_definition(plan: dict[str, Any], step_id: str) -> dict[str, Any]:
        for step in plan.get("steps") or []:
            if str(step.get("id")) == step_id:
                return step
        raise PlanError("step_not_found", "step is not part of the active Plan", stepId=step_id)

    async def _dependency_completed(
        self, conn: aiosqlite.Connection, task_uuid: str, version: int, dependency: str
    ) -> bool:
        current = await self._row(
            conn,
            "SELECT status FROM rath_task_plan_step_runs WHERE task_uuid=? AND plan_version=? AND step_id=?",
            (task_uuid, version, dependency),
        )
        if current is not None:
            return str(current["status"]) == "completed"
        historical = await self._row(
            conn,
            """
            SELECT 1 AS ok FROM rath_task_plan_step_runs
            WHERE task_uuid=? AND step_id=? AND status='completed' LIMIT 1
            """,
            (task_uuid, dependency),
        )
        return historical is not None

    async def _insert_evidence(
        self,
        conn: aiosqlite.Connection,
        *,
        task_uuid: str,
        version: int,
        step_id: str,
        request_id: str,
        evidence: list[Any],
    ) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise PlanError("invalid_evidence", f"evidence[{index}] must be an object")
            evidence_type = _text(
                item.get("type") or item.get("evidenceType"),
                field=f"evidence[{index}].type",
                required=True,
                limit=120,
            )
            reference = _text(
                item.get("reference") or item.get("ref"),
                field=f"evidence[{index}].reference",
                required=True,
                limit=8000,
            )
            summary = _text(item.get("summary"), field=f"evidence[{index}].summary", required=True, limit=3000)
            criterion_id = _text(item.get("criterionId"), field="criterionId", limit=120)
            metadata = item.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise PlanError("invalid_evidence", "evidence metadata must be an object")
            evidence_uuid = _uuid()
            await conn.execute(
                """
                INSERT INTO rath_task_plan_evidence
                  (evidence_uuid, task_uuid, plan_version, step_id, criterion_id,
                   evidence_type, reference, summary, metadata_json, request_id, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    evidence_uuid,
                    task_uuid,
                    version,
                    step_id,
                    criterion_id,
                    evidence_type,
                    reference,
                    summary,
                    _json(metadata),
                    request_id,
                    now_ts(),
                ),
            )
            created.append({
                "evidenceUuid": evidence_uuid,
                "criterionId": criterion_id,
                "type": evidence_type,
                "reference": reference,
                "summary": summary,
                "metadata": metadata,
            })
        return created

    async def _store_request(
        self,
        conn: aiosqlite.Connection,
        task_uuid: str,
        request_id: str,
        operation: str,
        request_fingerprint: str,
        result: dict[str, Any],
    ) -> None:
        await conn.execute(
            """
            INSERT INTO rath_task_plan_requests
              (task_uuid, request_id, operation, request_fingerprint, result_json, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (task_uuid, request_id, operation, request_fingerprint, _json(result), now_ts()),
        )

    async def progress(
        self,
        task_uuid: str,
        *,
        action: str,
        request_id: str,
        step_id: str = "",
        result_text: str = "",
        criteria: list[Any] | dict[str, Any] | None = None,
        evidence: list[Any] | None = None,
        blocker: dict[str, Any] | str | None = None,
        final_outputs: list[Any] | None = None,
    ) -> dict[str, Any]:
        action = str(action or "").strip()
        if action not in {"start", "update", "complete", "block", "finalize"}:
            raise PlanError("invalid_progress_action", "unsupported Plan progress action", action=action)
        request_id = _text(request_id, field="requestId", required=True, limit=300)
        step_id = _text(step_id, field="stepId", required=action != "finalize", limit=120)
        evidence = list(evidence or [])
        if action == "start":
            request_payload: dict[str, Any] = {}
        elif action == "update":
            request_payload = {"result": str(result_text or "").strip(), "evidence": evidence}
        elif action == "complete":
            request_payload = {
                "result": str(result_text or "").strip(),
                "criteria": criteria or {},
                "evidence": evidence,
            }
        elif action == "block":
            blocker_payload = dict(blocker) if isinstance(blocker, dict) else {"reason": str(blocker or "")}
            blocker_payload["reason"] = str(blocker_payload.get("reason") or "").strip()
            request_payload = {"blocker": blocker_payload, "evidence": evidence}
        else:
            request_payload = {"finalOutputs": final_outputs or []}
        event_detail: dict[str, Any] = {}
        async with self._lock(task_uuid):
            async with self.dao.db.plan_transaction() as conn:
                task = await self._task_row(conn, task_uuid)
                state = await self._row(
                    conn,
                    "SELECT * FROM rath_task_plan_state WHERE task_uuid=?",
                    (task_uuid,),
                )
                request_version = int((state or {}).get("pending_plan_version") or 0) or int(
                    (state or {}).get("active_plan_version") or 0
                )
                request_fingerprint = _request_fingerprint(
                    operation=action,
                    plan_version=request_version,
                    step_id=step_id,
                    payload=request_payload,
                )
                replay = await self._row(
                    conn,
                    """
                    SELECT operation, request_fingerprint, result_json
                    FROM rath_task_plan_requests WHERE task_uuid=? AND request_id=?
                    """,
                    (task_uuid, request_id),
                )
                if replay is not None:
                    if (
                        str(replay.get("operation") or "") != action
                        or not str(replay.get("request_fingerprint") or "")
                        or str(replay.get("request_fingerprint") or "") != request_fingerprint
                    ):
                        raise PlanError(
                            "request_id_conflict",
                            "requestId already belongs to a different Plan progress request",
                            requestId=request_id,
                            existingOperation=str(replay.get("operation") or ""),
                            operation=action,
                            planVersion=request_version,
                        )
                    return {**_loads(replay["result_json"], {}), "idempotent": True}
                self._assert_active_task(task)
                if state is None:
                    state = await self._state_row(conn, task_uuid)
                phase = str(state["phase"])
                if phase != "executing":
                    raise PlanError("invalid_plan_phase", f"progress requires executing phase, got {phase}", phase=phase)
                version = int(state["active_plan_version"] or 0)
                if not version or int(state["pending_plan_version"] or 0):
                    raise PlanError("approved_plan_required", "progress requires the latest approved Plan")
                _version_row, plan = await self._version_plan(conn, task_uuid, version)
                ts = now_ts()
                control_payload: dict[str, Any] = {}
                if action == "finalize":
                    if str(state["current_step_id"] or ""):
                        raise PlanError("step_still_running", "current step must finish before finalize")
                    for step in plan.get("steps") or []:
                        if not bool(step.get("required", True)):
                            continue
                        row = await self._row(
                            conn,
                            """
                            SELECT status FROM rath_task_plan_step_runs
                            WHERE task_uuid=? AND plan_version=? AND step_id=?
                            """,
                            (task_uuid, version, str(step["id"])),
                        )
                        if row is None or str(row["status"]) != "completed":
                            raise PlanError(
                                "completion_gate_failed",
                                "required Plan steps are incomplete",
                                stepId=str(step["id"]),
                            )
                    outputs_by_id: dict[str, dict[str, Any]] = {}
                    for item in final_outputs or []:
                        if not isinstance(item, dict):
                            raise PlanError("invalid_final_output", "final output state must be an object")
                        output_id = _text(item.get("id"), field="finalOutputs[].id", required=True, limit=120)
                        sources = _string_list(item.get("sources"), field=f"final output {output_id}.sources", item_limit=500)
                        if not sources:
                            raise PlanError("invalid_final_output", f"final output {output_id} requires sources")
                        if output_id in outputs_by_id:
                            raise PlanError("invalid_final_output", f"duplicate final output state: {output_id}")
                        for source in sources:
                            if source.startswith("step:"):
                                source_step = source.removeprefix("step:").strip()
                                found = await self._row(
                                    conn,
                                    """
                                    SELECT 1 AS ok FROM rath_task_plan_step_runs
                                    WHERE task_uuid=? AND step_id=? AND status='completed' LIMIT 1
                                    """,
                                    (task_uuid, source_step),
                                )
                            else:
                                evidence_uuid = source.removeprefix("evidence:").strip()
                                found = await self._row(
                                    conn,
                                    """
                                    SELECT 1 AS ok FROM rath_task_plan_evidence
                                    WHERE task_uuid=? AND evidence_uuid=? LIMIT 1
                                    """,
                                    (task_uuid, evidence_uuid),
                                )
                            if found is None:
                                raise PlanError(
                                    "final_output_source_not_found",
                                    "final output references an unknown or incomplete source",
                                    outputId=output_id,
                                    source=source,
                                )
                        outputs_by_id[output_id] = {
                            "id": output_id,
                            "summary": _text(item.get("summary"), field=f"final output {output_id}.summary", required=True),
                            "sources": sources,
                        }
                    for definition in plan.get("finalOutputs") or []:
                        output_id = str(definition["id"])
                        if output_id not in outputs_by_id:
                            raise PlanError(
                                "completion_gate_failed",
                                "required final output has no registered sources",
                                outputId=output_id,
                            )
                    await conn.execute(
                        """
                        UPDATE rath_task_plan_state
                        SET phase='finalizing', final_outputs_state_json=?,
                            row_revision=row_revision+1, updated_at=? WHERE task_uuid=?
                        """,
                        (_json(outputs_by_id), ts, task_uuid),
                    )
                    response = {
                        "ok": True,
                        "idempotent": False,
                        "taskUuid": task_uuid,
                        "planVersion": version,
                        "action": action,
                        "phase": "finalizing",
                        "finalOutputs": list(outputs_by_id.values()),
                    }
                else:
                    definition = self._step_definition(plan, step_id)
                    step = await self._row(
                        conn,
                        """
                        SELECT * FROM rath_task_plan_step_runs
                        WHERE task_uuid=? AND plan_version=? AND step_id=?
                        """,
                        (task_uuid, version, step_id),
                    )
                    assert step is not None
                    current_step = str(state["current_step_id"] or "")
                    if action == "start":
                        if current_step:
                            raise PlanError("step_already_running", "another Plan step is already running", stepId=current_step)
                        if str(step["status"]) != "pending":
                            raise PlanError("invalid_step_state", "only pending steps can start", stepStatus=step["status"])
                        missing = [
                            dependency
                            for dependency in definition.get("dependsOn") or []
                            if not await self._dependency_completed(conn, task_uuid, version, str(dependency))
                        ]
                        if missing:
                            raise PlanError("step_dependencies_incomplete", "step dependencies are incomplete", dependencies=missing)
                        await conn.execute(
                            """
                            UPDATE rath_task_plan_step_runs
                            SET status='running', started_at=?, updated_at=?, row_revision=row_revision+1
                            WHERE task_uuid=? AND plan_version=? AND step_id=? AND status='pending'
                            """,
                            (ts, ts, task_uuid, version, step_id),
                        )
                        await conn.execute(
                            """
                            UPDATE rath_task_plan_state SET current_step_id=?, row_revision=row_revision+1,
                              updated_at=? WHERE task_uuid=?
                            """,
                            (step_id, ts, task_uuid),
                        )
                        created_evidence: list[dict[str, Any]] = []
                    else:
                        if current_step != step_id or str(step["status"]) != "running":
                            raise PlanError(
                                "step_not_running",
                                "progress must target the current running step",
                                currentStepId=current_step,
                                stepStatus=step["status"],
                            )
                        created_evidence = await self._insert_evidence(
                            conn,
                            task_uuid=task_uuid,
                            version=version,
                            step_id=step_id,
                            request_id=request_id,
                            evidence=evidence,
                        )
                        if action == "update":
                            update_text = _text(result_text, field="result", required=True, limit=12000)
                            await conn.execute(
                                """
                                UPDATE rath_task_plan_step_runs
                                SET result=?, updated_at=?, row_revision=row_revision+1
                                WHERE task_uuid=? AND plan_version=? AND step_id=?
                                """,
                                (update_text, ts, task_uuid, version, step_id),
                            )
                        elif action == "complete":
                            complete_result = _text(result_text, field="result", required=True, limit=20000)
                            if isinstance(criteria, list):
                                criteria_map = {
                                    str(item.get("id") or item.get("criterionId") or ""): item
                                    for item in criteria
                                    if isinstance(item, dict)
                                    and str(item.get("id") or item.get("criterionId") or "")
                                }
                            elif isinstance(criteria, dict):
                                criteria_map = dict(criteria)
                            else:
                                criteria_map = {}
                            created_by_criterion: dict[str, list[str]] = {}
                            for item in created_evidence:
                                criterion_id = str(item.get("criterionId") or "")
                                if criterion_id:
                                    created_by_criterion.setdefault(criterion_id, []).append(item["evidenceUuid"])
                            normalized_criteria: dict[str, Any] = {}
                            for criterion in definition.get("criteria") or []:
                                criterion_id = str(criterion["id"])
                                raw_state = criteria_map.get(criterion_id) or {}
                                if isinstance(raw_state, str):
                                    raw_state = {"status": raw_state}
                                if not isinstance(raw_state, dict):
                                    raw_state = {}
                                status = str(raw_state.get("status") or "").strip().lower()
                                refs = (
                                    raw_state.get("evidence")
                                    or raw_state.get("evidenceUuids")
                                    or raw_state.get("evidenceIds")
                                    or []
                                )
                                if not isinstance(refs, list):
                                    raise PlanError("invalid_criteria_state", "criterion evidence must be an array")
                                evidence_refs = [str(item) for item in refs if str(item)]
                                evidence_refs.extend(created_by_criterion.get(criterion_id, []))
                                evidence_refs = list(dict.fromkeys(evidence_refs))
                                for evidence_uuid in evidence_refs:
                                    found = await self._row(
                                        conn,
                                        """
                                        SELECT 1 AS ok FROM rath_task_plan_evidence
                                        WHERE task_uuid=? AND evidence_uuid=? LIMIT 1
                                        """,
                                        (task_uuid, evidence_uuid),
                                    )
                                    if found is None:
                                        raise PlanError(
                                            "evidence_not_found",
                                            "criterion references unknown evidence",
                                            evidenceUuid=evidence_uuid,
                                        )
                                if bool(criterion.get("required", True)) and (
                                    status not in {"satisfied", "completed", "passed"} or not evidence_refs
                                ):
                                    raise PlanError(
                                        "completion_gate_failed",
                                        "required criterion needs satisfied status and evidence",
                                        criterionId=criterion_id,
                                    )
                                normalized_criteria[criterion_id] = {
                                    "status": status or "not_required",
                                    "evidence": evidence_refs,
                                    "note": _text(raw_state.get("note"), field="criterion note", limit=3000),
                                }
                            await conn.execute(
                                """
                                UPDATE rath_task_plan_step_runs
                                SET status='completed', result=?, criteria_state_json=?, completed_at=?,
                                  updated_at=?, row_revision=row_revision+1
                                WHERE task_uuid=? AND plan_version=? AND step_id=?
                                """,
                                (
                                    complete_result,
                                    _json(normalized_criteria),
                                    ts,
                                    ts,
                                    task_uuid,
                                    version,
                                    step_id,
                                ),
                            )
                            await conn.execute(
                                """
                                UPDATE rath_task_plan_state SET current_step_id='', row_revision=row_revision+1,
                                  updated_at=? WHERE task_uuid=?
                                """,
                                (ts, task_uuid),
                            )
                        else:  # block
                            blocker_data = blocker if isinstance(blocker, dict) else {"reason": str(blocker or "")}
                            blocker_reason = _text(
                                blocker_data.get("reason"), field="blocker.reason", required=True, limit=8000
                            )
                            blocker_data = {**blocker_data, "reason": blocker_reason}
                            await conn.execute(
                                """
                                UPDATE rath_task_plan_step_runs
                                SET status='blocked', blocker_json=?, updated_at=?, row_revision=row_revision+1
                                WHERE task_uuid=? AND plan_version=? AND step_id=?
                                """,
                                (_json(blocker_data), ts, task_uuid, version, step_id),
                            )
                            await conn.execute(
                                """
                                UPDATE rath_task_plan_state SET current_step_id='', row_revision=row_revision+1,
                                  updated_at=? WHERE task_uuid=?
                                """,
                                (ts, task_uuid),
                            )
                            runnable = False
                            for candidate in plan.get("steps") or []:
                                candidate_id = str(candidate["id"])
                                candidate_row = await self._row(
                                    conn,
                                    """
                                    SELECT status FROM rath_task_plan_step_runs
                                    WHERE task_uuid=? AND plan_version=? AND step_id=?
                                    """,
                                    (task_uuid, version, candidate_id),
                                )
                                if candidate_row is None or str(candidate_row["status"]) != "pending":
                                    continue
                                if all(
                                    [
                                        await self._dependency_completed(conn, task_uuid, version, str(dep))
                                        for dep in candidate.get("dependsOn") or []
                                    ]
                                ):
                                    runnable = True
                                    break
                            if not runnable:
                                await conn.execute(
                                    """
                                    UPDATE rath_task_plan_state SET phase='blocked_control',
                                      row_revision=row_revision+1, updated_at=? WHERE task_uuid=?
                                    """,
                                    (ts, task_uuid),
                                )
                                control_payload = {
                                    "status": "needs_openbear_control",
                                    "reason": "agent_plan_blocked",
                                    "message": f"Plan step {step_id} is blocked and no other step is runnable: {blocker_reason}",
                                    "continuable": True,
                                    "next": (
                                        "First request_replan against the active Plan, then use AgentMessage to resume "
                                        "the Agent and have it submit AgentPlanReplan; otherwise stop the Agent."
                                    ),
                                }
                        event_detail = {"stepId": step_id, "evidence": created_evidence}
                    response = {
                        "ok": True,
                        "idempotent": False,
                        "taskUuid": task_uuid,
                        "planVersion": version,
                        "action": action,
                        "stepId": step_id,
                        "evidence": created_evidence,
                        **control_payload,
                    }
                await self._store_request(
                    conn,
                    task_uuid,
                    request_id,
                    action,
                    request_fingerprint,
                    response,
                )
        await self.dao.append_event(
            task_uuid,
            f"plan_progress_{action}",
            summary=(f"Plan {action}: {step_id}" if step_id else "Plan finalize accepted"),
            detail=event_detail or {"planVersion": response["planVersion"]},
        )
        return response

    async def snapshot(self, task_uuid: str) -> dict[str, Any]:
        async with self.dao.db.plan_transaction() as conn:
            await self._task_row(conn, task_uuid)
            state = await self._state_row(conn, task_uuid)
            cur = await conn.execute(
                "SELECT * FROM rath_task_plan_versions WHERE task_uuid=? ORDER BY version ASC",
                (task_uuid,),
            )
            versions = []
            for row in await cur.fetchall():
                item = dict(row)
                item["plan"] = _loads(item.pop("plan_json"), {})
                versions.append(item)
            cur = await conn.execute(
                "SELECT * FROM rath_task_plan_decisions WHERE task_uuid=? ORDER BY id ASC",
                (task_uuid,),
            )
            decisions = []
            for row in await cur.fetchall():
                item = dict(row)
                item["issues"] = _loads(item.pop("issues_json"), [])
                item["required_changes"] = _loads(item.pop("required_changes_json"), [])
                item["granted_tools"] = _loads(item.pop("granted_tools_json"), [])
                decisions.append(item)
            cur = await conn.execute(
                "SELECT * FROM rath_task_plan_step_runs WHERE task_uuid=? ORDER BY plan_version, id",
                (task_uuid,),
            )
            steps = []
            for row in await cur.fetchall():
                item = dict(row)
                item["criteria_state"] = _loads(item.pop("criteria_state_json"), {})
                item["blocker"] = _loads(item.pop("blocker_json"), {})
                steps.append(item)
            cur = await conn.execute(
                "SELECT * FROM rath_task_plan_evidence WHERE task_uuid=? ORDER BY id ASC",
                (task_uuid,),
            )
            evidence_rows = []
            for row in await cur.fetchall():
                item = dict(row)
                item["metadata"] = _loads(item.pop("metadata_json"), {})
                evidence_rows.append(item)
            state_public = dict(state)
            state_public["final_outputs_state"] = _loads(state_public.pop("final_outputs_state_json"), {})
            state_public["approved_tools"] = _loads(state_public.pop("approved_tools_json"), [])
            return {
                "taskUuid": task_uuid,
                "state": state_public,
                "versions": versions,
                "decisions": decisions,
                "steps": steps,
                "evidence": evidence_rows,
            }


def _tool_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AgentPlanTools:
    def __init__(self, coordinator: AgentPlanCoordinator) -> None:
        self.coordinator = coordinator

    async def _notify_plan_review(self, task_uuid: str, submitted: dict[str, Any]) -> None:
        ctx = current_tool_context()
        callback = ctx.task_notification
        if callback is None:
            return
        snapshot = await self.coordinator.snapshot(task_uuid)
        version = int(submitted.get("planVersion") or 0)
        version_row = next(
            (item for item in snapshot["versions"] if int(item.get("version") or 0) == version),
            None,
        )
        if version_row is None:
            raise PlanError("plan_not_found", "submitted Plan version disappeared before notification")
        task = await self.coordinator.dao.get_task(task_uuid)
        if task is None:
            raise PlanError("task_not_found", "Rath task disappeared before Plan notification")
        state = snapshot["state"]
        phase = str(state.get("phase") or "awaiting_plan_decision")
        plan_type = str(version_row.get("plan_type") or submitted.get("planType") or "initial")
        completed_history = [
            {
                "planVersion": int(item.get("plan_version") or 0),
                "stepId": str(item.get("step_id") or ""),
                "result": str(item.get("result") or ""),
                "criteriaState": item.get("criteria_state") or {},
            }
            for item in snapshot["steps"]
            if str(item.get("status") or "") == "completed"
        ]
        prior_decisions = [
            {
                "expectedVersion": int(item.get("expected_version") or 0),
                "action": str(item.get("action") or ""),
                "issues": item.get("issues") or [],
                "reason": str(item.get("reason") or ""),
                "requiredChanges": item.get("required_changes") or [],
                "userInstructionId": str(item.get("user_instruction_id") or ""),
            }
            for item in snapshot["decisions"][-10:]
        ]
        events = await self.coordinator.dao.events(task_uuid, limit=12)
        payload: dict[str, Any] = {
            "kind": "plan-approval-required",
            "notificationKey": (
                f"{task.parent_session_uuid}:{task_uuid}:{version}:plan-approval-required"
            ),
            "requiresDecision": True,
            "taskUuid": task_uuid,
            "taskStatus": str(task.status or "running"),
            "status": phase,
            "summary": f"Agent submitted {plan_type} Plan v{version} for controller review",
            "expectedPlanVersion": version,
            "planVersion": version,
            "planType": plan_type,
            "approvalCycle": int(state.get("approval_cycle") or 0),
            "revisionCount": int(state.get("revision_count") or 0),
            "controllerGuidance": str(state.get("last_controller_guidance") or ""),
            "plan": version_row.get("plan") or {},
            "completedHistory": completed_history,
            "priorDecisions": prior_decisions,
            "task": {
                "taskUuid": task_uuid,
                "title": str(task.title or ""),
                "status": str(task.status or ""),
                "currentStatus": str(task.current_status or ""),
                "parentSessionUuid": str(task.parent_session_uuid or ""),
            },
            "recentEvents": [
                {
                    "seq": int(event.seq or 0),
                    "kind": str(event.kind or ""),
                    "summary": str(event.summary or ""),
                    "detail": event.detail,
                }
                for event in events
            ],
            "turnUuid": str(task.turn_uuid or ctx.turn_uuid or ""),
            "runRootTurnUuid": str(task.run_root_turn_uuid or ctx.run_root_turn_uuid or ""),
            "decisionTool": {
                "name": "AgentPlanDecision",
                "arguments": {
                    "taskUuid": task_uuid,
                    "expectedPlanVersion": version,
                    "action": "approve|revise|cancel",
                },
            },
            "resultOutputTokens": 0,
            "resultCount": 0,
        }
        payload["reviewPrompt"] = render_plan_prompt(
            "rath.planReviewPrompt",
            self.coordinator.plan_review_prompt,
            {
                "task": str(task.input.get("instruction") or task.input.get("question") or task.title or ""),
                "plan": payload["plan"],
                "revision_count": payload["revisionCount"],
            },
        )
        compact = {key: value for key, value in payload.items() if key != "content"}
        payload["content"] = (
            "<agent-plan-notification>\n"
            + json.dumps(compact, ensure_ascii=False, indent=2, default=str)
            + "\n</agent-plan-notification>\n\n"
            + payload["reviewPrompt"]
            + "\n\nThis is an internal Agent Plan review boundary, not a user request and not task completion. "
            + "Act as the OpenBear main controller. Review objective/scope, dependency order, executable methods, "
            + "required completion criteria, expected evidence, final outputs, risks, and—when planType=replan—"
            + "whether completed work is preserved and all remaining work is covered. "
            + f"Then call AgentPlanDecision exactly once with taskUuid={task_uuid} and expectedPlanVersion={version}. "
            + "Use approve only when the Plan is actually executable; use revise with concrete issues/reason/requiredChanges; "
            + "use cancel only when continuing is no longer appropriate. Do not use AgentMessage for Plan approval. "
            + "If the decision result says waitingForUser=true, stop autonomous revision, explain the full plan/disagreement/impact "
            + "to the user, then call AgentWait(mode=event_only). After user_interruption, pass AgentWait.userInstructionId "
            + "as userInstructionId in the next AgentPlanDecision."
        )
        await callback(payload)

    @staticmethod
    def _request_id(args: dict[str, Any]) -> str:
        ctx = current_tool_context()
        return str(ctx.tool_call_id or args.get("requestId") or args.get("request_id") or "").strip()

    @staticmethod
    def _agent_task_uuid() -> str:
        ctx = current_tool_context()
        if not ctx.source.startswith("agent:") or not ctx.task_uuid:
            raise PlanError("agent_runtime_required", "Plan worker tool requires an Agent runtime task context")
        return ctx.task_uuid

    async def submit(self, args: dict[str, Any]) -> str:
        try:
            task_uuid = self._agent_task_uuid()
            plan = args.get("plan") if isinstance(args.get("plan"), dict) else args
            result = await self.coordinator.submit_plan(
                task_uuid,
                plan,
                request_id=self._request_id(args),
                plan_type="initial",
                wait_for_decision=True,
                on_submitted=lambda submitted: self._notify_plan_review(task_uuid, submitted),
            )
            return _tool_json(result)
        except PlanError as exc:
            return _tool_json(exc.public())

    async def replan(self, args: dict[str, Any]) -> str:
        try:
            task_uuid = self._agent_task_uuid()
            plan = args.get("plan")
            if not isinstance(plan, dict):
                raise PlanError("invalid_plan", "AgentPlanReplan requires a complete remaining plan object")
            result = await self.coordinator.submit_plan(
                task_uuid,
                plan,
                request_id=self._request_id(args),
                plan_type="replan",
                change_reason=str(args.get("changeReason") or args.get("reason") or ""),
                wait_for_decision=True,
                on_submitted=lambda submitted: self._notify_plan_review(task_uuid, submitted),
            )
            return _tool_json(result)
        except PlanError as exc:
            return _tool_json(exc.public())

    async def decision(self, args: dict[str, Any]) -> str:
        try:
            ctx = current_tool_context()
            if ctx.source.startswith("agent:"):
                raise PlanError("controller_runtime_required", "AgentPlanDecision is main-controller-only")
            task_uuid = str(args.get("taskUuid") or args.get("task_uuid") or "").strip()
            if not task_uuid:
                raise PlanError("task_required", "taskUuid is required")
            task = await self.coordinator.dao.get_task(task_uuid)
            if task is None:
                raise PlanError("task_not_found", "Rath task not found")
            controller_scope = str(ctx.session_uuid or ctx.conversation_uuid or "")
            if controller_scope and task.parent_session_uuid != controller_scope:
                raise PlanError("task_scope_mismatch", "task does not belong to this controller session")
            user_instruction_id = str(args.get("userInstructionId") or "").strip()
            snapshot = await self.coordinator.snapshot(task_uuid)
            if str(snapshot["state"].get("phase") or "") == "needs_user_decision":
                new_user_turn = str(ctx.turn_uuid or "").strip()
                original_root = str(task.run_root_turn_uuid or "").strip()
                if new_user_turn and new_user_turn != original_root:
                    user_instruction_id = new_user_turn
            result = await self.coordinator.decide(
                task_uuid,
                expected_version=int(args.get("expectedPlanVersion") or args.get("planVersion") or 0),
                action=str(args.get("action") or ""),
                request_id=self._request_id(args),
                issues=args.get("issues") if isinstance(args.get("issues"), list) else [],
                reason=str(args.get("reason") or ""),
                required_changes=(
                    args.get("requiredChanges") if isinstance(args.get("requiredChanges"), list) else []
                ),
                granted_tools=(
                    args.get("grantedTools") if isinstance(args.get("grantedTools"), list) else []
                ),
                requested_by=str(args.get("requestedBy") or "main-controller"),
                user_instruction_id=user_instruction_id,
            )
            return _tool_json(result)
        except PlanError as exc:
            return _tool_json(exc.public())

    async def control_ack(self, args: dict[str, Any]) -> str:
        try:
            task_uuid = self._agent_task_uuid()
            control_uuid = _text(
                args.get("controlUuid"), field="controlUuid", required=True, limit=200
            )
            response_status = str(args.get("status") or "").strip().lower()
            if response_status not in {"accepted", "rejected", "appeal", "needs_clarification"}:
                raise PlanError(
                    "invalid_control_response",
                    "status must be accepted, rejected, appeal, or needs_clarification",
                )
            reason = _text(
                args.get("reason"),
                field="reason",
                required=response_status != "accepted",
                limit=8000,
            )
            plan_impact = _text(args.get("planImpact"), field="planImpact", limit=4000)
            next_action = _text(args.get("nextAction"), field="nextAction", limit=4000)
            control = await self.coordinator.dao.control(control_uuid)
            if control is None or control.task_uuid != task_uuid:
                raise PlanError("control_not_found", "control does not belong to this Agent task")
            if control.status != "applied":
                raise PlanError(
                    "control_not_applied",
                    "control must be applied to the Agent context before it can be acknowledged",
                    controlStatus=control.status,
                )
            if control.responded_at:
                return _tool_json({
                    "ok": True,
                    "idempotent": True,
                    "taskUuid": task_uuid,
                    "controlUuid": control_uuid,
                    "status": control.response_status,
                    "reason": control.response_reason,
                    "planImpact": control.response_plan_impact,
                    "nextAction": control.response_next_action,
                })
            stored = await self.coordinator.dao.mark_control_response(
                control_uuid,
                response_status=response_status,
                reason=reason,
                plan_impact=plan_impact,
                next_action=next_action,
            )
            if not stored:
                # Another duplicate acknowledgement may have won after our
                # initial read. Re-read the durable response and return it as
                # idempotent instead of exposing a transient conflict.
                replay = await self.coordinator.dao.control(control_uuid)
                if replay is not None and replay.responded_at:
                    return _tool_json({
                        "ok": True,
                        "idempotent": True,
                        "taskUuid": task_uuid,
                        "controlUuid": control_uuid,
                        "status": replay.response_status,
                        "reason": replay.response_reason,
                        "planImpact": replay.response_plan_impact,
                        "nextAction": replay.response_next_action,
                    })
                raise PlanError("control_response_conflict", "control response could not be recorded")
            await self.coordinator.dao.append_event(
                task_uuid,
                "control_response",
                summary=f"Agent 对控制请求的回应：{response_status}",
                detail={
                    "controlUuid": control_uuid,
                    "status": response_status,
                    "reason": reason,
                    "planImpact": plan_impact,
                    "nextAction": next_action,
                },
            )
            return _tool_json({
                "ok": True,
                "taskUuid": task_uuid,
                "controlUuid": control_uuid,
                "status": response_status,
                "reason": reason,
                "planImpact": plan_impact,
                "nextAction": next_action,
            })
        except PlanError as exc:
            return _tool_json(exc.public())

    async def progress(self, args: dict[str, Any]) -> str:
        try:
            result = await self.coordinator.progress(
                self._agent_task_uuid(),
                action=str(args.get("action") or ""),
                request_id=self._request_id(args),
                step_id=str(args.get("stepId") or ""),
                result_text=str(args.get("result") or args.get("summary") or ""),
                criteria=args.get("criteria"),
                evidence=args.get("evidence") if isinstance(args.get("evidence"), list) else [],
                blocker=args.get("blocker") or args.get("reason"),
                final_outputs=(
                    args.get("finalOutputs") if isinstance(args.get("finalOutputs"), list) else []
                ),
            )
            return _tool_json(result)
        except PlanError as exc:
            return _tool_json(exc.public())


def register_agent_plan_tools(registry: ToolRegistry, coordinator: AgentPlanCoordinator) -> None:
    tools = AgentPlanTools(coordinator)

    def string_array_schema(*, item_limit: int, limit: int = 100) -> dict[str, Any]:
        return {
            "type": "array",
            "maxItems": limit,
            "items": {"type": "string", "minLength": 1, "maxLength": item_limit},
        }

    plan_definition_schema = {
        "type": "object",
        "description": (
            "Complete immutable Agent Plan. Submit every required Plan field even when requesting tools; "
            "toolRequests supplements the Plan and never replaces it. Do not request tools already granted "
            "at Agent launch."
        ),
        "properties": {
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "Short title for the complete Plan.",
            },
            "objective": {
                "type": "string",
                "minLength": 1,
                "maxLength": 8000,
                "description": "Exact outcome the Plan must achieve.",
            },
            "scope": {
                "type": "object",
                "properties": {
                    "included": string_array_schema(item_limit=2000),
                    "excluded": string_array_schema(item_limit=2000),
                },
            },
            "assumptions": string_array_schema(item_limit=3000),
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": coordinator.max_steps,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 120},
                        "title": {"type": "string", "minLength": 1, "maxLength": 500},
                        "objective": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "method": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "dependsOn": string_array_schema(item_limit=120),
                        "required": {"type": "boolean"},
                        "criteria": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": coordinator.max_criteria_per_step,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 120,
                                    },
                                    "description": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 2000,
                                    },
                                    "required": {"type": "boolean"},
                                },
                                "required": ["id", "description"],
                            },
                        },
                        "expectedEvidence": string_array_schema(item_limit=1000),
                    },
                    "required": ["id", "title", "objective", "method", "criteria"],
                },
            },
            "finalOutputs": {
                "type": "array",
                "minItems": 1,
                "maxItems": coordinator.max_final_outputs,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 120},
                        "title": {"type": "string", "minLength": 1, "maxLength": 500},
                        "description": {"type": "string", "minLength": 1, "maxLength": 3000},
                        "supportedBy": string_array_schema(item_limit=200),
                    },
                    "required": ["id", "title", "description"],
                },
            },
            "risks": string_array_schema(item_limit=3000),
            "toolRequests": {
                "type": "array",
                "maxItems": len(AGENT_DELEGATION_TOOL_NAMES),
                "description": (
                    "Additional tools requested for specific Plan steps. This field does not replace the "
                    "required title, objective, steps, or finalOutputs fields."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": sorted(AGENT_DELEGATION_TOOL_NAMES),
                        },
                        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "neededForSteps": string_array_schema(item_limit=120),
                    },
                    "required": ["name", "reason", "neededForSteps"],
                },
            },
        },
        "required": ["title", "objective", "steps", "finalOutputs"],
    }
    plan_schema = {
        "type": "object",
        "properties": {
            "plan": plan_definition_schema,
            "requestId": {"type": "string", "description": "Optional explicit idempotency key."},
        },
        "required": ["plan"],
    }
    registry.add(
        "AgentPlanSubmit",
        "Submit the complete initial Plan, notify the controller, release the execution slot, and wait for a decision.",
        plan_schema,
        tools.submit,
        visibility={"agent", "runtime"},
        preserve_result=True,
    )
    registry.add(
        "AgentPlanDecision",
        "Approve, revise, cancel, or request replanning for a scoped Agent Plan version.",
        {
            "type": "object",
            "properties": {
                "taskUuid": {"type": "string"},
                "expectedPlanVersion": {"type": "integer", "minimum": 1},
                "action": {"type": "string", "enum": ["approve", "revise", "cancel", "request_replan"]},
                "issues": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
                "requiredChanges": {"type": "array", "items": {"type": "string"}},
                "grantedTools": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(AGENT_DELEGATION_TOOL_NAMES)},
                    "description": (
                        "For action=approve only: additional Plan-requested tools approved by the controller. "
                        "Omit or use [] to deny all requests; when denying any request, explain why in reason."
                    ),
                },
                "userInstructionId": {"type": "string"},
                "requestId": {"type": "string"},
            },
            "required": ["taskUuid", "expectedPlanVersion", "action"],
        },
        tools.decision,
        visibility={"main", "runtime"},
        preserve_result=True,
    )
    registry.add(
        "AgentControlAck",
        "Acknowledge one applied OpenBear control before using any other Agent tool.",
        {
            "type": "object",
            "properties": {
                "controlUuid": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["accepted", "rejected", "appeal", "needs_clarification"],
                },
                "reason": {
                    "type": "string",
                    "description": "Required for rejected, appeal, and needs_clarification; optional concise rationale for accepted.",
                },
                "planImpact": {"type": "string"},
                "nextAction": {"type": "string"},
            },
            "required": ["controlUuid", "status"],
        },
        tools.control_ack,
        visibility={"agent", "runtime"},
        preserve_result=True,
    )
    registry.add(
        "AgentPlanProgress",
        "Start/update/complete/block the current approved Plan step, record evidence, or pass the finalization gate.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "update", "complete", "block", "finalize"]},
                "stepId": {"type": "string"},
                "result": {"type": "string"},
                "criteria": {
                    "type": ["array", "object"],
                    "description": (
                        "Required for action=complete. Prefer an array of {id,status,evidence,note}; "
                        "status must be satisfied/completed/passed and evidence contains durable evidence UUIDs. "
                        "criterionId and evidenceUuids/evidenceIds are accepted aliases."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Criterion id from the approved Plan."},
                            "criterionId": {"type": "string", "description": "Alias for id."},
                            "status": {"type": "string", "enum": ["satisfied", "completed", "passed"]},
                            "evidence": {"type": "array", "items": {"type": "string"}},
                            "evidenceUuids": {"type": "array", "items": {"type": "string"}},
                            "evidenceIds": {"type": "array", "items": {"type": "string"}},
                            "note": {"type": "string"},
                        },
                        "required": ["status"],
                    },
                },
                "evidence": {
                    "type": "array",
                    "description": "Durable evidence created by update/complete and optionally linked to a criterionId.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "reference": {"type": "string"},
                            "summary": {"type": "string"},
                            "criterionId": {"type": "string"},
                            "metadata": {"type": "object"},
                        },
                        "required": ["type", "reference", "summary"],
                    },
                },
                "blocker": {"type": ["object", "string"], "description": "Required for action=block."},
                "finalOutputs": {
                    "type": "array",
                    "description": (
                        "Required for action=finalize. One item per approved Plan final output: "
                        "{id,summary,sources}. Each source is a durable evidence UUID/evidence:<uuid> "
                        "or a completed step:<stepId>."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "summary": {"type": "string"},
                            "sources": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["id", "summary", "sources"],
                    },
                },
                "requestId": {"type": "string"},
            },
            "required": ["action"],
        },
        tools.progress,
        visibility={"agent", "runtime"},
        preserve_result=True,
    )
    registry.add(
        "AgentPlanReplan",
        "Submit a complete replacement for all remaining Plan work and wait for controller approval.",
        {
            "type": "object",
            "properties": {
                "changeReason": {"type": "string"},
                "completedWork": {"type": "array", "items": {"type": "string"}},
                "retainedResults": {"type": "array", "items": {"type": "string"}},
                "invalidatedResults": {"type": "array", "items": {"type": "string"}},
                "plan": {"type": "object"},
                "requestId": {"type": "string"},
            },
            "required": ["changeReason", "plan"],
        },
        tools.replan,
        visibility={"agent", "runtime"},
        preserve_result=True,
    )
