import json

from app.rath.controller_projection import (
    project_agent_payload_for_controller,
    project_agent_tool_result_for_controller,
    project_history_message_for_controller,
)
from app.tools.agents import _render_agent_task_notification


def _raw_payload() -> dict:
    return {
        "ok": True,
        "status": "completed",
        "taskUuid": "task-1",
        "resultOutputTokens": 4321,
        "resultCount": 1,
        "task": {
            "taskUuid": "task-1",
            "title": "research",
            "status": "completed",
            "currentStatus": "done",
            "model": "provider/model",
            "modelCalls": 7,
            "toolCalls": 9,
            "workToolCalls": 8,
            "planToolCalls": 1,
            "tokens": {"input": 100, "output": 20},
            "lastUsage": {"inputTokens": 50, "outputTokens": 20},
            "contextTokens": 150,
            "contextWindow": 1000,
            "costUsd": 1.25,
            "durationMs": 5000,
        },
        "result": {
            "summary": "business result",
            "tokens": "this is domain data, not orchestration telemetry",
            "costUsd": "this field is part of the delegated business result",
            "evidence": [{"criterionId": "C1", "reference": "file.py:10"}],
        },
        "planRuntime": {
            "state": {"phase": "finalizing", "active_plan_version": 1},
            "evidence": [{"criterion_id": "C1", "reference": "file.py:10"}],
        },
        "recentEvents": [
            {
                "seq": 1,
                "kind": "model_call_finished",
                "summary": "model call finished",
                "detail": {"inputTokens": 100, "outputTokens": 20, "costUsd": 1.25},
            },
            {
                "seq": 2,
                "kind": "model_context_compaction_fallback_used",
                "summary": "model context compacted",
                "detail": {"inputTokens": 80, "threshold": 70},
            },
            {
                "seq": 3,
                "kind": "plan_progress_complete",
                "summary": "Plan complete: S1",
                "detail": {"stepId": "S1", "evidence": ["ev-1"]},
            },
        ],
    }


def test_controller_projection_removes_orchestration_telemetry_but_preserves_business_result():
    raw = _raw_payload()
    projected = project_agent_payload_for_controller(raw)

    assert "resultOutputTokens" not in projected
    assert "resultCount" not in projected
    for key in (
        "model", "modelCalls", "toolCalls", "workToolCalls", "planToolCalls",
        "tokens", "lastUsage", "contextTokens", "contextWindow", "costUsd", "durationMs",
    ):
        assert key not in projected["task"]
    assert projected["result"] == raw["result"]
    assert projected["planRuntime"] == raw["planRuntime"]
    assert [event["kind"] for event in projected["recentEvents"]] == ["plan_progress_complete"]


def test_controller_projection_normalizes_runtime_boundary_without_losing_continuation_state():
    projected = project_agent_payload_for_controller({
        "status": "needs_openbear_control",
        "reason": "agent_task_budget_exceeded",
        "budgetKind": "model",
        "message": "used 20/20 model calls",
        "taskUuid": "task-2",
        "continuationStateArtifactUuid": "artifact-1",
        "pendingToolCalls": [{"id": "call-1", "name": "Read", "arguments": "{}"}],
        "detail": {"reason": "agent_task_budget_exceeded", "used": 20, "limit": 20},
    })

    assert projected["reason"] == "agent_runtime_safety_boundary"
    assert "budgetKind" not in projected
    assert "20/20" not in projected["message"]
    assert projected["continuationStateArtifactUuid"] == "artifact-1"
    assert projected["pendingToolCalls"][0]["id"] == "call-1"
    assert "used" not in projected["detail"]
    assert "limit" not in projected["detail"]


def test_agent_tool_and_legacy_history_use_same_projection_boundary():
    raw_text = json.dumps(_raw_payload(), ensure_ascii=False)
    direct = project_agent_tool_result_for_controller("AgentWait", raw_text)
    replay = project_history_message_for_controller({
        "role": "tool",
        "name": "AgentWait",
        "tool_call_id": "wait-1",
        "content": raw_text,
    })["content"]

    assert json.loads(direct) == json.loads(replay)
    assert "resultOutputTokens" not in json.loads(direct)
    assert project_agent_tool_result_for_controller("Read", raw_text) == raw_text


def test_task_notification_renders_controller_projection_not_raw_payload():
    rendered = _render_agent_task_notification(_raw_payload())

    assert "resultOutputTokens" not in rendered
    assert "resultCount" not in rendered
    assert '"modelCalls"' not in rendered
    assert '"toolCalls"' not in rendered
    assert '"costUsd": 1.25' not in rendered
    assert "business result" in rendered
    assert "this field is part of the delegated business result" in rendered
    assert '"phase": "finalizing"' in rendered
