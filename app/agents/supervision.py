"""Objective AgentWait observations, not a judgement of work quality.

Event pagination, transport activity and recorded Plan evidence have separate
cursors. Reading an old event page never counts as newly observed activity.
"""
from __future__ import annotations

from typing import Any

from app.agents.schemas import AgentTask


def review_observation(
    task: AgentTask, events: dict[str, Any], plan: dict[str, Any],
    previous: dict[str, Any], *, now_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    latest = events["latest"]
    high_water = events["highWater"]
    activity = bool(
        high_water > int(previous.get("observedEventSeq") or 0)
        or task.status != previous.get("status")
        or task.current_status != previous.get("currentStatus")
    )
    last_at = latest.ts * 1000 if latest and latest.ts else None

    def age(start: int | None) -> int | None:
        return max(0, now_ms - start) if start is not None else None

    # Only recorded transitions establish a current call. A terminal/control
    # task must not look like a still-running model because its last event lags.
    execution: dict[str, Any] = {
        "phase": task.status, "startedAtMs": None, "elapsedMs": None,
    }
    transitions = [event for event in events["current"]
                   if event.kind not in {"model_stream_progress", "plan_submitted", "plan_replan_submitted"}]
    boundary = transitions[-1] if transitions else None
    if task.status in {"running", "resuming", "pausing"} and boundary:
        started = boundary.ts * 1000 if boundary.ts else None
        if boundary.kind == "model_call_started":
            execution.update(phase="model", startedAtMs=started,
                             attemptId=str(boundary.detail.get("attemptId") or ""))
            progress = next((event for event in reversed(events["current"])
                             if event.kind == "model_stream_progress" and event.seq > boundary.seq), None)
            # Reported progress is throttled; absence is not proof of idle
            # transport, lack of research, or lack of provider computation.
            execution["lastReportedOutputAtMs"] = progress.ts * 1000 if progress and progress.ts else None
            execution["reportedOutputAgeMs"] = age(execution["lastReportedOutputAtMs"])
            if progress:
                tool_input = progress.detail.get("toolInput")
                if isinstance(tool_input, dict):
                    execution["toolInput"] = {
                        key: tool_input[key] for key in ("toolNames", "receivedBytes", "phase")
                        if key in tool_input
                    }
        elif boundary.kind == "tool_call_started":
            execution.update(phase="tool", startedAtMs=started,
                             toolName=str(boundary.detail.get("name") or ""))
        elif boundary.kind == "model_call_retry_wait":
            retry = boundary.detail.get("retry") or {}
            if retry.get("active"):
                retry_at = int(retry.get("retryAtMs") or 0)
                delay = int(retry.get("delayMs") or 0)
                execution.update(phase="retry_wait", startedAtMs=retry_at - delay if retry_at else started,
                                 retryAtMs=retry_at or None, attempt=retry.get("attempt"))
        execution["elapsedMs"] = age(execution["startedAtMs"])
    elif task.status in {"paused", "needs_openbear_control"}:
        kind = "pause_applied" if task.status == "paused" else "needs_openbear_control"
        waiting = next((event for event in reversed(events["current"]) if event.kind == kind), None)
        if waiting and waiting.ts:
            execution.update(startedAtMs=waiting.ts * 1000, elapsedMs=age(waiting.ts * 1000))

    if task.status in {"running", "resuming", "pausing"} and plan.get("phase") in {
        "awaiting_plan_decision", "awaiting_replan_decision",
    }:
        pending = plan.get("pendingPlanVersion")
        submitted = next((event for event in reversed(events["current"])
                          if event.kind in {"plan_submitted", "plan_replan_submitted"}
                          and event.detail.get("planVersion") == pending), None)
        started = submitted.ts * 1000 if submitted and submitted.ts else None
        execution = {"phase": "plan_approval", "startedAtMs": started, "elapsedMs": age(started)}

    # A changed recorded criterion is evidence to review, not certification of
    # completion. Direct mode, missing Plan state, and the first baseline are
    # explicitly unknown; Read/TaskMemory/Write tool names are not classifiers.
    evidence_state = None
    if plan:
        evidence_state = {
            "planVersion": plan.get("activePlanVersion"),
            "steps": [{key: step.get(key) for key in ("stepId", "status", "criteriaState")}
                      for step in plan.get("steps", [])],
        }
    before = previous.get("deliveryEvidenceState")
    evidence = {
        "source": "recorded_plan_state" if evidence_state is not None else "unavailable",
        "known": evidence_state is not None,
        "changedSinceReview": evidence_state != before if evidence_state is not None and before is not None else None,
        "meaning": "Recorded step/criterion changes only; not verified delivery or a judgement of research value.",
    }
    observation = {
        "hasActivity": activity,
        # Compatibility alias: never promise artifact/criterion advancement.
        "hasMeaningfulProgress": activity,
        "progressSemantics": "hasMeaningfulProgress is a legacy alias for hasActivity, not delivery evidence.",
        "lastActivityAtMs": last_at, "activityAgeMs": age(last_at),
        "activitySource": "latest_recorded_event",
        "currentExecution": execution, "deliveryEvidence": evidence,
        "eventPage": {key: events[key] for key in ("highWater", "hasMore", "nextAfter")},
    }
    next_state = {
        "lastEventSeq": events["nextAfter"], "observedEventSeq": high_water,
        "status": task.status, "currentStatus": task.current_status,
        "deliveryEvidenceState": evidence_state,
    }
    return observation, next_state
