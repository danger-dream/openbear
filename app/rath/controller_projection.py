"""Controller-facing projection for Agent orchestration payloads.

Rath keeps complete task, usage, timing, and continuation data for accounting,
context preflight, recovery, and the Web Agent workspace.  The main controller
only needs task identity, durable Plan/evidence state, results, and explicit
control state.  This module defines that boundary without recursively deleting
similarly named fields from an Agent's business result.
"""
from __future__ import annotations

import json
from typing import Any

_INTERNAL_RESULT_KEYS = frozenset({"resultOutputTokens", "resultCount"})
_INTERNAL_TASK_KEYS = frozenset({
    "model",
    "modelCalls",
    "toolCalls",
    "workToolCalls",
    "planToolCalls",
    "tokens",
    "lastUsage",
    "contextTokens",
    "contextWindow",
    "costUsd",
    "startedAtMs",
    "finishedAtMs",
    "durationMs",
    "updatedAtMs",
})
_INTERNAL_EVENT_KINDS = frozenset({
    "tool_call_started",
    "tool_call_finished",
})
_INTERNAL_EVENT_DETAIL_KEYS = frozenset({
    "model",
    "modelLabel",
    "thinkLevel",
    "maxTokens",
    "inputTokens",
    "outputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "costUsd",
    "durationMs",
    "tps",
    "threshold",
    "estimatedTokensBefore",
    "estimatedTokensAfter",
    "messageCountBefore",
    "messageCountAfter",
    "textChars",
    "reasoningChars",
    "round",
    "attempt",
    "used",
    "limit",
    "toolCalls",
    "workToolCalls",
    "planToolCalls",
    "previousBudgetKind",
})
_LEGACY_RUNTIME_BOUNDARY_REASON = "agent_task_budget_exceeded"
_CONTROLLER_RUNTIME_BOUNDARY_REASON = "agent_runtime_safety_boundary"
_RUNTIME_BOUNDARY_MESSAGE = (
    "The Agent paused at a hard runtime safety boundary. Decide whether to continue only from the approved Plan, "
    "durable evidence, real blockers, scope, and safety. If work remains, use AgentMessage with narrow guidance; "
    "the saved continuation state will be reused."
)


def _copy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    if isinstance(value, tuple):
        return [_copy_value(item) for item in value]
    return value


def _project_runtime_boundary(value: dict[str, Any]) -> dict[str, Any]:
    """Hide hard-limit counters while preserving the actionable control state."""
    projected = {str(key): _copy_value(item) for key, item in value.items()}
    if str(projected.get("reason") or "") != _LEGACY_RUNTIME_BOUNDARY_REASON:
        return projected

    projected["reason"] = _CONTROLLER_RUNTIME_BOUNDARY_REASON
    projected.pop("budgetKind", None)
    projected["message"] = _RUNTIME_BOUNDARY_MESSAGE
    if "summary" in projected:
        projected["summary"] = "Agent paused at a hard runtime safety boundary and is awaiting controller action."

    detail = projected.get("detail")
    if isinstance(detail, dict):
        detail = _project_runtime_boundary(detail)
        for key in _INTERNAL_EVENT_DETAIL_KEYS:
            detail.pop(key, None)
        projected["detail"] = detail
    return projected


def _project_task(value: dict[str, Any]) -> dict[str, Any]:
    projected = {
        str(key): _copy_value(item)
        for key, item in value.items()
        if str(key) not in _INTERNAL_TASK_KEYS
    }
    output = projected.get("output")
    if isinstance(output, dict):
        projected["output"] = _project_runtime_boundary(output)
    return projected


def _project_event(value: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(value.get("kind") or "")
    # Every model_* event is execution telemetry (request lifecycle, retry,
    # streaming, accounting, or compaction). It remains in Rath/Web audit but
    # has no role in controller supervision.
    if kind.startswith("model_") or kind in _INTERNAL_EVENT_KINDS:
        return None
    projected = {str(key): _copy_value(item) for key, item in value.items()}
    if kind == "agent_budget_exhausted":
        projected["kind"] = "agent_runtime_safety_boundary"
        projected["summary"] = "Agent paused at a hard runtime safety boundary"
    elif kind == "agent_budget_continued":
        projected["kind"] = "agent_runtime_safety_continued"
        projected["summary"] = "Agent resumed from its saved continuation state"
    detail = projected.get("detail")
    if isinstance(detail, dict):
        detail = _project_runtime_boundary(detail)
        for key in _INTERNAL_EVENT_DETAIL_KEYS:
            detail.pop(key, None)
        projected["detail"] = detail
    return projected


def _looks_like_orchestration_payload(value: dict[str, Any]) -> bool:
    return any(key in value for key in ("taskUuid", "task", "agentSession", "planRuntime", "wakeReason"))


def project_agent_payload_for_controller(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the model-visible projection of one Agent protocol payload.

    Business results are copied verbatim.  Only documented orchestration
    envelopes, task snapshots, and telemetry event details are projected.
    """
    projected = {
        str(key): _copy_value(item)
        for key, item in payload.items()
        if str(key) not in _INTERNAL_RESULT_KEYS
    }
    projected = _project_runtime_boundary(projected)

    task = projected.get("task")
    if isinstance(task, dict):
        projected["task"] = _project_task(task)

    for key in ("recentEvents", "events"):
        events = projected.get(key)
        if isinstance(events, list):
            visible_events = [
                projected_event
                for item in events
                if isinstance(item, dict)
                for projected_event in [_project_event(item)]
                if projected_event is not None
            ]
            projected[key] = visible_events

    for key in ("notifications", "agents"):
        items = projected.get(key)
        if isinstance(items, list):
            projected[key] = [
                project_agent_payload_for_controller(item) if isinstance(item, dict) else _copy_value(item)
                for item in items
            ]

    results = projected.get("results")
    if isinstance(results, list):
        projected["results"] = [
            project_agent_payload_for_controller(item)
            if isinstance(item, dict) and _looks_like_orchestration_payload(item)
            else _copy_value(item)
            for item in results
        ]

    result = projected.get("result")
    if isinstance(result, dict) and _looks_like_orchestration_payload(result):
        projected["result"] = project_agent_payload_for_controller(result)
    elif isinstance(result, dict):
        projected["result"] = _project_runtime_boundary(result)

    return projected


def project_agent_tool_result_for_controller(tool_name: str, content: str) -> str:
    """Project a JSON Agent tool result while leaving every other tool untouched."""
    if not str(tool_name or "").startswith("Agent"):
        return content
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return content
    if not isinstance(payload, dict):
        return content
    return json.dumps(project_agent_payload_for_controller(payload), ensure_ascii=False, separators=(",", ":"))


def project_history_message_for_controller(message: dict[str, Any]) -> dict[str, Any]:
    """Apply the same boundary when replaying legacy persisted tool results."""
    projected = dict(message)
    if str(projected.get("role") or "") != "tool":
        return projected
    content = projected.get("content")
    if not isinstance(content, str):
        return projected
    projected["content"] = project_agent_tool_result_for_controller(
        str(projected.get("name") or ""),
        content,
    )
    return projected
