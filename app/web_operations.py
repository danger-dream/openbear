"""Web Event / Operation v2 helpers.

This module is deliberately dependency-light: it defines the operation reducer and
Web runtime and history are represented by operation snapshots plus frame
cursors; freshness for one UI object is represented by operation `revision`.
"""
from __future__ import annotations

import copy
import json
import re
import time
from typing import Any

from app.tools.base import redact_tool_arguments_for_audit, redact_tool_result_for_audit

AGENT_TASK_TOOL_NAMES = {"Agent"}
AGENT_CONTROL_TOOL_NAMES = {"AgentMessage", "AgentStop"}
AGENT_TOOL_NAMES = AGENT_TASK_TOOL_NAMES | AGENT_CONTROL_TOOL_NAMES
ACTIVE_STATUSES = {"queued", "running", "pausing", "resuming", "stopping"}
PAUSED_STATUSES = {"paused"}
WAITING_CONTROL_STATUSES = {"needs_openbear_control"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted", "partial"}


def now_ms() -> int:
    return int(time.time() * 1000)


def json_loads_dict(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def json_loads_list(raw: str | None) -> list[Any]:
    try:
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def is_context_compaction_operation(
    operation: dict[str, Any],
    *,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Recognize only canonical or identity-bearing context compactions.

    Persisted automatic compactions historically used ``op_type='tool'``.  The
    exact source marker is authoritative for those rows; frame-only compatibility
    additionally requires both the stable opId and a compaction-shaped payload so
    an ordinary/internal tool cannot become a visible compaction accidentally.
    """
    if not isinstance(operation, dict):
        return False
    candidate_payload = payload
    if candidate_payload is None:
        candidate_payload = (
            operation.get("payload")
            if isinstance(operation.get("payload"), dict)
            else json_loads_dict(str(operation.get("payload_json") or "{}"))
        )
    op_type = str(operation.get("op_type") or operation.get("opType") or "").strip()
    if op_type == "context_compaction":
        return True
    if str(operation.get("source") or "").strip() == "context_compaction":
        return True
    op_id = str(operation.get("op_id") or operation.get("opId") or "").strip()
    if not op_id.startswith("tool:context-compaction:"):
        return False
    tool_name = str(
        candidate_payload.get("toolName")
        or candidate_payload.get("name")
        or candidate_payload.get("rootToolName")
        or ""
    ).strip()
    compaction_id = str(candidate_payload.get("compactionId") or "").strip()
    summary_id = candidate_payload.get("summaryId")
    valid_summary_id = isinstance(summary_id, int) and not isinstance(summary_id, bool) and summary_id > 0
    return tool_name == "ContextCompaction" and (
        compaction_id.startswith("context-compaction:") or valid_summary_id
    )


def is_user_interaction_operation(
    operation: dict[str, Any],
    *,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Canonicalize only typed rows or exact historical UserInteraction tools."""
    if not isinstance(operation, dict):
        return False
    op_type = str(operation.get("op_type") or operation.get("opType") or "").strip()
    if op_type == "user_interaction":
        return True
    if op_type != "tool":
        return False
    candidate = payload
    if candidate is None:
        candidate = (
            operation.get("payload")
            if isinstance(operation.get("payload"), dict)
            else json_loads_dict(str(operation.get("payload_json") or "{}"))
        )
    return str(candidate.get("toolName") or candidate.get("name") or "").strip() == "UserInteraction"


_USER_INTERACTION_STATUS_MAP = {
    "pending": ("running", "start"),
    "answered": ("completed", "end"),
    "cancelled": ("cancelled", "cancel"),
    "timeout": ("completed", "end"),
    "error": ("failed", "error"),
}


def _user_interaction_result_state(result: str) -> tuple[str, str, str]:
    parsed = json_loads_dict(result)
    interaction_status = str(parsed.get("status") or "error").strip().lower()
    if interaction_status not in _USER_INTERACTION_STATUS_MAP:
        interaction_status = "error"
    operation_status, action = _USER_INTERACTION_STATUS_MAP[interaction_status]
    return interaction_status, operation_status, action


def _sanitize_user_interaction_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply a fail-closed second boundary to historical timeline payloads."""
    sanitized = copy.deepcopy(payload or {})
    arguments = sanitized.get("arguments") if "arguments" in sanitized else sanitized.get("args")
    if isinstance(arguments, str):
        raw_arguments = arguments
        redacted_arguments = redact_tool_arguments_for_audit("UserInteraction", arguments)
    elif isinstance(arguments, dict):
        raw_arguments = json_dumps(arguments)
        redacted_arguments = redact_tool_arguments_for_audit("UserInteraction", raw_arguments)
    else:
        raw_arguments = ""
        redacted_arguments = arguments
    sensitive = bool(re.search(
        r'"(?:sensitive|secret)"\s*:\s*true\b', raw_arguments, re.IGNORECASE,
    ))
    parsed_arguments = json_loads_dict(redacted_arguments if isinstance(redacted_arguments, str) else "")
    if parsed_arguments:
        sensitive = sensitive or bool(parsed_arguments.get("sensitive") or parsed_arguments.get("secret"))
    if sensitive:
        for key in ("arguments", "args"):
            if key in sanitized:
                sanitized[key] = redacted_arguments
        for key in ("result", "resultText"):
            if key in sanitized:
                sanitized[key] = redact_tool_result_for_audit(
                    "UserInteraction", str(sanitized.get(key) or ""), raw_arguments,
                )
        for key in ("preview", "previewArguments", "outputPreview", "searchText"):
            sanitized.pop(key, None)
    return sanitized


def deep_merge(old: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(old or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif value is None:
            # A patch omitting detail should not delete a recoverable snapshot.
            continue
        else:
            out[key] = copy.deepcopy(value)
    return out


def status_lifecycle(op_type: str, status: str = "", payload: dict[str, Any] | None = None) -> str:
    status = str(status or (payload or {}).get("status") or "").strip()
    op_type = str(op_type or "")
    # Notices are immutable transcript information, never runtime ownership.
    # In particular a needs_openbear_control notice describes an Agent boundary;
    # the Agent operation/task itself carries the actionable waiting lifecycle.
    if op_type == "notice":
        return "informational"
    if status in ACTIVE_STATUSES:
        return "active"
    if status in PAUSED_STATUSES:
        return "paused"
    if status in WAITING_CONTROL_STATUSES:
        return "waiting_control"
    if status in TERMINAL_STATUSES:
        return "terminal"
    if op_type in {"notice", "stats", "user_message", "agent_supervision"} and status not in ACTIVE_STATUSES:
        return "informational" if op_type != "user_message" else "terminal"
    if op_type == "assistant_message" and (payload or {}).get("complete"):
        return "terminal"
    if op_type == "reasoning" and (payload or {}).get("complete"):
        return "terminal"
    return ""


def _append_or_snapshot_text(old_text: str, incoming: str, delta: str = "") -> str:
    old_text = str(old_text or "")
    incoming = str(incoming or "")
    delta = str(delta or "")
    if incoming:
        return incoming
    if delta:
        return old_text + delta
    return old_text


def reduce_operation_payload(
    old_payload: dict[str, Any] | None,
    *,
    op_type: str,
    action: str,
    patch: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the full recoverable operation snapshot after applying one frame.

    `patch` may contain a full text snapshot during the compatibility phase. The
    frame payload can later be minimized to a delta, but DB operation payload must
    always stay complete for refresh/restore.
    """
    old = copy.deepcopy(old_payload or {})
    patch = copy.deepcopy(patch or {})
    action = str(action or "patch")
    op_type = str(op_type or "")

    if action in {"create", "start", "snapshot"} and not old:
        return patch

    if op_type == "assistant_message":
        next_payload = deep_merge(old, {k: v for k, v in patch.items() if k not in {"delta"}})
        text = _append_or_snapshot_text(old.get("text", ""), patch.get("text", ""), patch.get("delta", ""))
        if text or "text" in patch or "delta" in patch:
            next_payload["text"] = text
        if action in {"end", "error", "cancel"}:
            next_payload["complete"] = True
        elif "complete" in patch:
            next_payload["complete"] = bool(patch.get("complete"))
        else:
            next_payload.setdefault("complete", False)
        return next_payload

    if op_type == "reasoning":
        next_payload = deep_merge(old, {k: v for k, v in patch.items() if k not in {"delta"}})
        text = _append_or_snapshot_text(old.get("text", ""), patch.get("text", ""), patch.get("delta", ""))
        if text or "text" in patch or "delta" in patch:
            next_payload["text"] = text
        if action in {"end", "cancel"}:
            next_payload["complete"] = True
        elif "complete" in patch:
            next_payload["complete"] = bool(patch.get("complete"))
        else:
            next_payload.setdefault("complete", False)
        return next_payload

    next_payload = deep_merge(old, patch)
    if op_type == "agent" and old:
        # A Rath task has one stable UI card, but it may receive updates from
        # Agent, AgentMessage, AgentStop and task-notification turns.  Preserve
        # the root invocation identity so follow-up/continuation patches do not
        # visually replace the original Agent card with an AgentMessage card.
        for key in ("rootToolName", "rootToolCallId", "rootArguments"):
            if old.get(key):
                next_payload[key] = old[key]
        old_root = str(old.get("rootToolName") or old.get("toolName") or "").strip()
        patch_tool = str(patch.get("toolName") or patch.get("name") or "").strip()
        if patch_tool and old_root and patch_tool != old_root:
            next_payload["lastControlToolName"] = patch_tool
            for key in ("toolName", "name", "toolCallId", "args", "arguments", "preview", "resultText"):
                if key in patch:
                    next_payload[f"lastControl{key[:1].upper()}{key[1:]}"] = copy.deepcopy(patch.get(key))
                if key in old:
                    next_payload[key] = copy.deepcopy(old.get(key))
                else:
                    next_payload.pop(key, None)
    if action == "error" and not next_payload.get("status"):
        next_payload["status"] = "failed"
    elif action == "cancel" and not next_payload.get("status"):
        next_payload["status"] = "cancelled"
    elif action == "end" and not next_payload.get("status"):
        next_payload["status"] = "completed"
    return next_payload


def frame_payload_for_action(
    *,
    old_payload: dict[str, Any] | None,
    op_type: str,
    action: str,
    patch: dict[str, Any] | None,
    snapshot_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return the compact payload that should be sent/stored in frame log."""
    patch = copy.deepcopy(patch or {})
    old_payload = old_payload or {}
    if op_type in {"assistant_message", "reasoning"} and action == "append":
        incoming = str(patch.get("text") or "")
        delta = str(patch.get("delta") or "")
        if incoming:
            previous = str(old_payload.get("text") or "")
            if incoming.startswith(previous):
                delta = incoming[len(previous):]
            else:
                # Snapshot discontinuity: keep a full snapshot frame so the
                # frontend can resync without trying to concatenate bad deltas.
                return {**patch, "text": incoming, "snapshot": True, "textLength": len(incoming)}
        return {
            **{k: v for k, v in patch.items() if k not in {"text"}},
            "delta": delta,
            "textLength": len(str(snapshot_payload.get("text") or "")),
            "complete": bool(snapshot_payload.get("complete")),
        }
    return patch


def event_turn_uuid(event: dict[str, Any]) -> str:
    return str(event.get("turnUuid") or event.get("turn_uuid") or "").strip()


def event_ts_ms(event: dict[str, Any]) -> int:
    ts = int(event.get("ts") or 0)
    return ts if ts > 100000000000 else (ts * 1000 if ts else now_ms())


def _tool_call_id(event: dict[str, Any]) -> str:
    return str(event.get("toolCallId") or event.get("tool_call_id") or "").strip()


def _assistant_segment(event: dict[str, Any]) -> str:
    key = str(event.get("eventKey") or event.get("event_key") or "").strip()
    if key.startswith("assistant:draft:"):
        return key.rsplit(":", 1)[-1] or "0"
    return "0"


def _agent_status(payload: dict[str, Any]) -> str:
    rows = payload.get("results") if isinstance(payload.get("results"), list) else []
    statuses = [str((item or {}).get("status") or ((item or {}).get("task") or {}).get("status") or "").strip() for item in rows]
    statuses = [s for s in statuses if s]
    if statuses:
        if any(s in ACTIVE_STATUSES or s in PAUSED_STATUSES for s in statuses):
            return "running"
        if any(s in WAITING_CONTROL_STATUSES for s in statuses):
            return "needs_openbear_control"
        if all(s in TERMINAL_STATUSES for s in statuses):
            return "completed" if all(s == "completed" for s in statuses) else "partial"
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    if task.get("status"):
        return str(task.get("status") or "").strip()
    direct = str(payload.get("status") or "").strip()
    if direct:
        return direct
    return ""


def _agent_operation_action(status: str, default: str = "patch") -> str:
    if status in TERMINAL_STATUSES or status in WAITING_CONTROL_STATUSES:
        return "end"
    return default


def _operation_spec(
    *,
    op_id: str,
    op_type: str,
    action: str,
    turn_uuid: str = "",
    payload: dict[str, Any] | None = None,
    status: str = "",
    lifecycle: str = "",
    source: str = "",
    internal: bool = False,
    parent_turn_uuid: str = "",
    run_root_turn_uuid: str = "",
    run_id: str = "",
    skip_if_missing: bool = False,
) -> dict[str, Any]:
    payload = payload or {}
    if run_id:
        payload.setdefault("runId", run_id)
    status = status or str(payload.get("status") or "")
    return {
        "op_id": op_id,
        "op_type": op_type,
        "action": action,
        "turn_uuid": turn_uuid,
        "parent_turn_uuid": parent_turn_uuid,
        "run_root_turn_uuid": run_root_turn_uuid,
        "run_id": run_id,
        "payload": payload,
        "status": status,
        "lifecycle": lifecycle or status_lifecycle(op_type, status, payload),
        "source": source,
        "internal": bool(internal or payload.get("internal")),
        "skip_if_missing": skip_if_missing,
    }


def assistant_event_operation_specs(
    kind: str,
    *,
    turn_uuid: str = "",
    event_key: str = "",
    text: str = "",
    reasoning: str = "",
    footer: str = "",
    ts: int = 0,
) -> list[dict[str, Any]]:
    """Build native assistant text/reasoning operation specs from stream fields."""
    typ = str(kind or "").strip()
    if typ not in {"delta", "final", "cut"}:
        return []
    ts = int(ts or now_ms())
    segment = _assistant_segment({"eventKey": event_key})
    segment_index = int(segment) if str(segment).isdigit() else 0
    text = str(text or "")
    reasoning = str(reasoning or "")
    specs: list[dict[str, Any]] = []
    if typ == "delta":
        if reasoning:
            specs.append(_operation_spec(
                op_id=f"reasoning:{turn_uuid}:{segment}", op_type="reasoning", action="append", turn_uuid=turn_uuid,
                payload={"text": reasoning, "complete": False, "segmentIndex": segment_index},
                status="running", source="assistant",
            ))
        if text:
            specs.append(_operation_spec(
                op_id=f"assistant:{turn_uuid}:{segment}", op_type="assistant_message", action="append", turn_uuid=turn_uuid,
                payload={"text": text, "complete": False, "segmentIndex": segment_index},
                status="running", source="assistant",
            ))
        return specs
    if typ == "final":
        if reasoning:
            specs.append(_operation_spec(
                op_id=f"reasoning:{turn_uuid}:{segment}", op_type="reasoning", action="end", turn_uuid=turn_uuid,
                payload={"text": reasoning, "complete": True, "segmentIndex": segment_index},
                status="completed", source="assistant",
            ))
        if text or not reasoning:
            specs.append(_operation_spec(
                op_id=f"assistant:{turn_uuid}:{segment}", op_type="assistant_message", action="end", turn_uuid=turn_uuid,
                payload={"text": text, "complete": True, "footer": str(footer or ""), "segmentIndex": segment_index},
                status="completed", source="assistant",
            ))
        return specs
    if typ == "cut":
        specs.append(_operation_spec(
            op_id=f"assistant:{turn_uuid}:{segment}", op_type="assistant_message", action="end", turn_uuid=turn_uuid,
            payload={"complete": True, "segmentBoundary": True, "segmentIndex": segment_index},
            status="completed", source="assistant", skip_if_missing=True,
        ))
        specs.append(_operation_spec(
            op_id=f"reasoning:{turn_uuid}:{segment}", op_type="reasoning", action="end", turn_uuid=turn_uuid,
            payload={"complete": True, "segmentBoundary": True, "segmentIndex": segment_index},
            status="completed", source="assistant", skip_if_missing=True,
        ))
        return specs
    return specs


def tool_event_operation_specs(
    kind: str,
    *,
    turn_uuid: str = "",
    tool_call_id: str = "",
    name: str = "Tool",
    arguments: str = "",
    line: str = "",
    payload: dict[str, Any] | None = None,
    result: str = "",
    duration_ms: int = 0,
    event_uuid: str = "",
    event_id: str = "",
    ts: int = 0,
) -> list[dict[str, Any]]:
    """Build native tool/agent operation specs from renderer-level fields.

    It lets Web renderer/tool sources provide operation intent directly for the
    operation/frame pipeline.
    """
    typ = str(kind or "").strip()
    if typ not in {"tool_start", "tool_update", "tool_progress", "tool_result"}:
        return []
    ts = int(ts or now_ms())
    tool_call_id = str(tool_call_id or event_uuid or event_id or ts).strip()
    name = str(name or "Tool")
    arguments = str(arguments or "")
    raw_payload = payload if isinstance(payload, dict) else {}
    # Only Agent creates a task card. AgentMessage/AgentStop are control
    # operations attached to a target task; treating them as Agent tasks creates
    # fake queued cards (especially when the model uses an 8-char task id).
    tool_name = str(raw_payload.get("toolName") or name)
    compaction_metadata: dict[str, Any] = {}
    if tool_name == "ContextCompaction":
        parsed_arguments = json_loads_dict(arguments)
        for key in (
            "compactionId", "summaryId", "scope", "source", "status",
            "beforeTokens", "afterTokens", "summaryChars", "summaryTokens",
            "upToMessageId", "outputAvailable", "outputPreview", "summaryRef",
        ):
            if key in parsed_arguments:
                compaction_metadata[key] = copy.deepcopy(parsed_arguments[key])
    is_compaction = bool(compaction_metadata)
    is_user_interaction = tool_name == "UserInteraction" or name == "UserInteraction"
    operation_source = (
        "user_interaction" if is_user_interaction
        else "context_compaction" if is_compaction
        else "tool"
    )
    is_agent = tool_name in AGENT_TASK_TOOL_NAMES or name in AGENT_TASK_TOOL_NAMES
    is_agent_control = tool_name in AGENT_CONTROL_TOOL_NAMES or name in AGENT_CONTROL_TOOL_NAMES
    op_type = (
        "agent" if is_agent
        else "agent_control" if is_agent_control
        else "context_compaction" if is_compaction
        else "user_interaction" if is_user_interaction
        else "tool"
    )
    # Real Agent operations use taskUuid as their stable visual card id. Control
    # operations keep their tool-call identity and merely carry taskUuid as a
    # target, so they can never redirect or replace the task card.
    agent_task_uuid = ""
    if is_agent or is_agent_control:
        task_obj = raw_payload.get("task") if isinstance(raw_payload.get("task"), dict) else None
        agent_task_uuid = str((task_obj or {}).get("taskUuid") or raw_payload.get("taskUuid") or "")
        if not agent_task_uuid:
            try:
                parsed_args = json.loads(str(arguments or "{}"))
                if isinstance(parsed_args, dict):
                    agent_task_uuid = str(parsed_args.get("taskUuid") or parsed_args.get("task_uuid") or parsed_args.get("to") or "")
            except Exception:
                pass
        if not agent_task_uuid and typ == "tool_result":
            try:
                parsed = json.loads(str(result or ""))
                if isinstance(parsed, dict):
                    agent_task_uuid = str(parsed.get("taskUuid") or "")
            except Exception:
                pass
    if is_agent and agent_task_uuid and f"agent:{agent_task_uuid}" != f"{op_type}:{tool_call_id}":
        op_id = f"agent:{agent_task_uuid}"
    elif is_compaction or is_user_interaction:
        # Typed compatibility projections retain historical tool-call identity.
        # start/end must update one card even though the canonical type is no
        # longer the generic ``tool`` type.
        op_id = f"tool:{tool_call_id}"
    else:
        op_id = f"{op_type}:{tool_call_id}"
    specs: list[dict[str, Any]] = []
    # When agent opId is redirected to taskUuid, close the original
    # tool_call_id operation so the frontend does not render a stale empty card.
    if is_agent and agent_task_uuid and op_id == f"agent:{agent_task_uuid}" and tool_call_id and f"agent:{tool_call_id}" != op_id:
        specs.append(_operation_spec(
            op_id=f"agent:{tool_call_id}", op_type="agent", action="cancel",
            turn_uuid=turn_uuid,
            payload={
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "rootToolName": tool_name,
                "merged": True,
                "mergedTo": op_id,
                # The placeholder only needs one durable redirect. The store
                # keeps its first terminal target immutable.
                "mergeRedirect": True,
            },
            status="completed", lifecycle="terminal", source="tool",
            # A progress snapshot can arrive without the original tool_start
            # (for example after reconnect). The durable task card is enough;
            # never invent a transient tool-call placeholder in that case.
            skip_if_missing=True,
        ))
    if typ == "tool_start":
        assistant_segment = str(raw_payload.get("assistantSegment") or "0")
        specs.append(_operation_spec(
            op_id=f"reasoning:{turn_uuid}:{assistant_segment}", op_type="reasoning", action="end", turn_uuid=turn_uuid,
            payload={"complete": True}, status="completed", source="assistant", skip_if_missing=True,
        ))
        next_payload = {
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "rootToolName": tool_name,
            "name": name,
            "args": arguments,
            "arguments": arguments,
            "status": "queued" if is_agent else "running",
            **({"interactionStatus": "pending"} if is_user_interaction else {}),
            "taskUuid": agent_task_uuid,
            "preview": str(line or ""),
            "startedAtMs": ts,
            **compaction_metadata,
        }
        specs.append(_operation_spec(
            op_id=op_id, op_type=op_type, action="start", turn_uuid=turn_uuid,
            payload=next_payload, status=next_payload["status"], source=operation_source,
        ))
        return specs
    if typ == "tool_update":
        next_payload = {"toolCallId": tool_call_id, "toolName": tool_name, "name": name, "args": arguments, "preview": str(line or ""), "updatedAtMs": ts}
        if is_user_interaction:
            next_payload["interactionStatus"] = "pending"
        specs.append(_operation_spec(op_id=op_id, op_type=op_type, action="patch", turn_uuid=turn_uuid, payload=next_payload, status="running", source=operation_source))
        return specs
    if typ == "tool_progress":
        next_payload = {**raw_payload, "toolCallId": tool_call_id, "toolName": tool_name, "rootToolName": raw_payload.get("rootToolName") or tool_name, "name": name, "args": arguments, "arguments": arguments, "updatedAtMs": ts}
        if is_agent and name in AGENT_TASK_TOOL_NAMES:
            # The task card outlives the original Agent tool call. Preserve its
            # launch identity separately so later AgentWait/control progress
            # cannot replace the prompt shown to the user.
            next_payload.update({
                "rootToolCallId": tool_call_id,
                "rootToolName": name,
                "rootArguments": arguments,
            })
        status = _agent_status(next_payload) if is_agent else str(next_payload.get("status") or "running")
        if is_user_interaction:
            next_payload["interactionStatus"] = "pending"
            status = "running"
        if status:
            next_payload["status"] = status
        lifecycle = "waiting_control" if is_agent and status in WAITING_CONTROL_STATUSES else ""
        if is_agent_control and agent_task_uuid:
            # A control event may refresh an already existing Agent card, but it
            # must never create one. The separate informational control
            # operation below remains the transcript of the command itself.
            specs.append(_operation_spec(
                op_id=f"agent:{agent_task_uuid}", op_type="agent",
                action=_agent_operation_action(status, "patch"), turn_uuid=turn_uuid,
                payload=next_payload, status=status or "running",
                lifecycle="waiting_control" if status in WAITING_CONTROL_STATUSES else "",
                source="agent_control", skip_if_missing=True,
            ))
        specs.append(_operation_spec(
            op_id=op_id, op_type=op_type, action=_agent_operation_action(status, "patch") if is_agent else "patch",
            turn_uuid=turn_uuid, payload=next_payload, status=status or "running", lifecycle=lifecycle, source=operation_source,
        ))
        return specs
    if typ == "tool_result":
        if is_agent or is_agent_control:
            next_payload = {"toolCallId": tool_call_id, "toolName": tool_name, "rootToolName": tool_name, "name": name, "args": arguments, "arguments": arguments, "taskUuid": agent_task_uuid, "resultText": str(result or ""), "durationMs": int(duration_ms or 0), "updatedAtMs": ts, "transcriptResult": True}
            if is_agent and name in AGENT_TASK_TOOL_NAMES:
                next_payload.update({
                    "rootToolCallId": tool_call_id,
                    "rootToolName": name,
                    "rootArguments": arguments,
                })
            action = "patch"
            status = ""
            if is_agent and not agent_task_uuid:
                parsed_result = json_loads_dict(str(result or ""))
                if parsed_result.get("ok") is False or parsed_result.get("error"):
                    status = "failed"
                    next_payload["status"] = status
                    action = "error"
            if tool_name == "AgentStop":
                parsed_result = json_loads_dict(str(result or ""))
                task_result = parsed_result.get("task") if isinstance(parsed_result.get("task"), dict) else None
                status = str(parsed_result.get("status") or (task_result or {}).get("status") or "").strip()
                if task_result is not None:
                    next_payload["task"] = task_result
                if status in TERMINAL_STATUSES or status in WAITING_CONTROL_STATUSES:
                    next_payload["status"] = status
                    action = _agent_operation_action(status, "patch")
            if is_agent_control:
                target_status = str((next_payload.get("task") or {}).get("status") or "").strip() if isinstance(next_payload.get("task"), dict) else ""
                if agent_task_uuid and target_status:
                    specs.append(_operation_spec(
                        op_id=f"agent:{agent_task_uuid}", op_type="agent",
                        action=_agent_operation_action(target_status, "patch"), turn_uuid=turn_uuid,
                        payload=next_payload, status=target_status, source="agent_control",
                        skip_if_missing=True,
                    ))
                action = "end"
                status = "completed"
                next_payload["status"] = status
            specs.append(_operation_spec(
                op_id=op_id, op_type=op_type, action=action,
                turn_uuid=turn_uuid, payload=next_payload, status=status, source="tool",
            ))
        else:
            interaction_status, operation_status, interaction_action = (
                _user_interaction_result_state(str(result or ""))
                if is_user_interaction else ("", "completed", "end")
            )
            next_payload = {
                "toolCallId": tool_call_id,
                "name": name,
                "toolName": name,
                "args": arguments,
                "arguments": arguments,
                "status": operation_status,
                **({"interactionStatus": interaction_status} if is_user_interaction else {}),
                "result": str(result or ""),
                "durationMs": int(duration_ms or 0),
                "updatedAtMs": ts,
                **compaction_metadata,
            }
            if compaction_metadata:
                next_payload.setdefault("outputPreview", str(result or "")[:12_000])
            specs.append(_operation_spec(
                op_id=op_id, op_type=op_type,
                action=interaction_action if is_user_interaction else "end",
                turn_uuid=turn_uuid, payload=next_payload,
                status=operation_status, source=operation_source,
            ))
        return specs
    return specs


def web_event_operation_specs(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one Web event payload to v2 operation specs.

    This is the source-facing mapper used by Web live publishers.  It accepts
    renderer-level event payloads and returns operation intent for the
    operation/frame pipeline.
    """
    if not isinstance(event, dict):
        return []
    typ = str(event.get("type") or event.get("kind") or "").strip()
    turn_uuid = event_turn_uuid(event)
    run_root_turn_uuid = str(event.get("runRootTurnUuid") or event.get("run_root_turn_uuid") or turn_uuid or "").strip()
    # A visible/root turn may contain multiple controller executions (for example
    # an Agent result continuation).  Keep the visible turn stable while giving
    # every execution its own durable lifecycle identity.
    execution_run_uuid = str(
        event.get("runUuid")
        or event.get("run_uuid")
        or event.get("executionRunUuid")
        or event.get("execution_run_uuid")
        or turn_uuid
        or ""
    ).strip()
    ts = event_ts_ms(event)
    specs: list[dict[str, Any]] = []

    if typ == "accepted":
        task_notification = bool(event.get("taskNotification"))
        source = "task_notification" if task_notification else "user"
        payload = {
            "turnId": turn_uuid,
            "runId": execution_run_uuid,
            "source": source,
            "status": "running",
            "startedAtMs": ts,
            "internal": task_notification,
            "taskNotification": task_notification,
        }
        specs.append(_operation_spec(
            op_id=f"run:{execution_run_uuid}", op_type="run", action="start", turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            run_id=execution_run_uuid,
            payload=payload, status="running", source=source,
        ))
        return specs

    if typ == "user":
        msg_uuid = str(event.get("messageUuid") or event.get("message_uuid") or event.get("eventUuid") or event.get("eventId") or "").strip()
        specs.append(_operation_spec(
            op_id=f"msg:{msg_uuid or turn_uuid or ts}", op_type="user_message", action="create", turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            payload={
                "role": "user",
                "text": str(event.get("text") or event.get("content") or ""),
                "attachments": event.get("attachments") if isinstance(event.get("attachments"), list) else [],
                "queued": bool(event.get("queued", False)),
                "interruption": bool(event.get("interruption") or event.get("steeringInjected")),
                "status": str(event.get("status") or ("插话已交给主会话" if event.get("steeringInjected") else "")),
                "createdAtMs": ts,
            },
            status="completed", source="user",
        ))
        return specs

    if typ == "queued":
        ev_uuid = str(event.get("messageUuid") or event.get("message_uuid") or event.get("eventUuid") or event.get("eventId") or turn_uuid or ts)
        specs.append(_operation_spec(
            op_id=f"msg:{ev_uuid}", op_type="user_message", action="create", turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            payload={
                "role": "user",
                "text": str(event.get("text") or ""),
                "queued": True,
                "interruption": True,
                "status": str(event.get("status") or "已追加到当前运行"),
                "createdAtMs": ts,
            },
            status="completed", source="queued_steering",
        ))
        return specs

    if typ == "task_notification":
        task_uuids = event.get("taskUuids") if isinstance(event.get("taskUuids"), list) else []
        remaining_task_uuids = event.get("remainingTaskUuids") if isinstance(event.get("remainingTaskUuids"), list) else []
        summary = str(event.get("summary") or event.get("text") or "Agent 结果已回传，正在汇总")
        specs.append(_operation_spec(
            op_id=f"notice:task:{str(event.get('taskUuid') or event.get('eventUuid') or turn_uuid or ts)}",
            op_type="notice", action="create", turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            payload={
                "text": summary,
                "summary": summary,
                "level": "info",
                "taskUuid": str(event.get("taskUuid") or ""),
                "taskUuids": [str(value) for value in task_uuids if str(value)],
                "status": str(event.get("status") or ""),
                "title": str(event.get("title") or ""),
                "batchCount": int(event.get("batchCount") or 0),
                "deferredUntilRelatedTasksTerminal": bool(event.get("deferredUntilRelatedTasksTerminal")),
                "remainingTaskUuids": [str(value) for value in remaining_task_uuids if str(value)],
                "createdAtMs": ts,
                "internal": True,
                "hidden": bool(event.get("hidden")),
            },
            source="task_notification", internal=True,
        ))
        return specs

    if typ == "agent_supervision":
        status = str(event.get("status") or "running").strip() or "running"
        active = bool(event.get("active", status == "running"))
        action = "patch"
        lifecycle = "active" if active else "terminal"
        wait_cycle_uuid = str(event.get("waitCycleUuid") or event.get("wait_cycle_uuid") or "").strip()
        payload = {
            "status": status,
            "waitCycleUuid": wait_cycle_uuid,
            "statusText": str(event.get("statusText") or "等待 Agent"),
            "preview": str(event.get("preview") or event.get("reason") or ""),
            "active": active,
            "mode": str(event.get("mode") or ""),
            "reviewAfterSeconds": float(event.get("reviewAfterSeconds") or 0),
            "plannedReviewAtMs": int(event.get("plannedReviewAtMs") or 0),
            "wakeReason": str(event.get("wakeReason") or ""),
            "taskCounts": event.get("taskCounts") if isinstance(event.get("taskCounts"), dict) else {},
            "updatedAtMs": ts,
        }
        specs.append(_operation_spec(
            op_id=f"agent-supervision:{wait_cycle_uuid or run_root_turn_uuid or turn_uuid or 'current'}",
            op_type="agent_supervision", action=action, turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid, payload=payload,
            status=status, lifecycle=lifecycle, source="agent_supervision",
        ))
        return specs

    if typ == "agent_control":
        task_uuid = str(event.get("taskUuid") or event.get("task_uuid") or "").strip()
        control_uuid = str(event.get("controlUuid") or event.get("control_uuid") or event.get("eventUuid") or event.get("eventId") or ts).strip()
        action = str(event.get("controlAction") or event.get("action") or "message").strip() or "message"
        op_key = control_uuid or task_uuid or str(ts)
        payload = {
            "taskUuid": task_uuid,
            "taskUuids": event.get("taskUuids") if isinstance(event.get("taskUuids"), list) else ([task_uuid] if task_uuid else []),
            "controlUuid": control_uuid,
            "controlAction": action,
            "text": str(event.get("text") or ""),
            "summary": str(event.get("summary") or ""),
            "statusText": str(event.get("statusText") or event.get("summary") or "Agent 控制事件"),
            "createdAtMs": ts,
        }
        specs.append(_operation_spec(
            op_id=f"agent-control:{op_key}", op_type="agent_control", action="create", turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            payload=payload, status="completed", source="agent_control",
        ))
        return specs

    if typ == "retry_wait":
        retry = dict(event.get("retry")) if isinstance(event.get("retry"), dict) else {}
        # Rath child retries are durable task events keyed by taskUuid+seq.  They
        # must never leak into the root transcript as controller retry rows.
        retry_task_uuid = str(retry.get("taskUuid") or retry.get("task_uuid") or "").strip()
        if retry_task_uuid:
            return []
        retry.pop("taskUuid", None)
        retry.pop("task_uuid", None)
        active = bool(retry.get("active"))
        attempt = max(1, int(retry.get("attempt") or retry.get("retryNumber") or 1))
        retry_status = str(retry.get("status") or ("waiting" if active else "resumed")).strip()
        operation_status = (
            "running"
            if active
            else "cancelled"
            if retry_status == "cancelled"
            else "failed"
            if retry_status == "failed"
            else "completed"
        )
        retry.update({
            "active": active,
            "attempt": attempt,
            "status": retry_status,
            "terminal": not active,
        })
        payload = {
            **retry,
            "retry": dict(retry),
            "retryStatus": retry_status,
            "statusText": (
                "模型调用失败，等待重试"
                if active
                else "模型重试已取消"
                if retry_status == "cancelled"
                else "模型重试失败"
                if retry_status == "failed"
                else "继续运行"
            ),
            "source": "model_retry",
            "updatedAtMs": ts,
            "runId": execution_run_uuid,
        }
        specs.append(_operation_spec(
            op_id=f"model-retry:{execution_run_uuid or turn_uuid or 'current'}:{attempt}",
            op_type="model_retry",
            action="start" if active else "end",
            turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            run_id=execution_run_uuid,
            payload=payload,
            status=operation_status,
            lifecycle="active" if active else "terminal",
            source="model_retry",
        ))
        return specs

    if typ == "status":
        specs.append(_operation_spec(
            op_id=f"status:{execution_run_uuid or turn_uuid or 'current'}", op_type="status", action="patch", turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            run_id=execution_run_uuid,
            payload={"statusText": str(event.get("status") or "运行中"), "active": True, "updatedAtMs": ts, "runId": execution_run_uuid},
            status="running", source="system",
        ))
        return specs

    if typ in {"delta", "final", "cut"}:
        specs = assistant_event_operation_specs(
            typ,
            turn_uuid=turn_uuid,
            event_key=str(event.get("eventKey") or event.get("event_key") or ""),
            text=str(event.get("text") or ""),
            reasoning=str(event.get("reasoning") or ""),
            footer=str(event.get("footer") or ""),
            ts=ts,
        )
        for spec in specs:
            spec["run_root_turn_uuid"] = run_root_turn_uuid
            spec["run_id"] = execution_run_uuid
            spec_payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
            if execution_run_uuid:
                spec_payload.setdefault("runId", execution_run_uuid)
            if event.get("internal"):
                spec["internal"] = True
                spec_payload["internal"] = True
                if event.get("hidden"):
                    spec_payload["hidden"] = True
            spec["payload"] = spec_payload
        return specs

    if typ in {"tool_start", "tool_update", "tool_progress", "tool_result"}:
        tool_payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
        if "assistantSegment" in event:
            tool_payload.setdefault("assistantSegment", event.get("assistantSegment"))
        specs = tool_event_operation_specs(
            typ,
            turn_uuid=turn_uuid,
            tool_call_id=_tool_call_id(event),
            name=str(event.get("name") or "Tool"),
            arguments=str(event.get("arguments") or ""),
            line=str(event.get("line") or ""),
            payload=tool_payload,
            result=str(event.get("result") or ""),
            duration_ms=int(event.get("durationMs") or event.get("duration_ms") or 0),
            event_uuid=str(event.get("eventUuid") or event.get("event_uuid") or ""),
            event_id=str(event.get("eventId") or event.get("event_id") or ""),
            ts=ts,
        )
        for spec in specs:
            spec["run_root_turn_uuid"] = run_root_turn_uuid
            spec["run_id"] = execution_run_uuid
            spec_payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
            if execution_run_uuid:
                spec_payload.setdefault("runId", execution_run_uuid)
            spec["payload"] = spec_payload
        return specs

    if typ == "stats":
        # Live stats are high-frequency snapshots (normally every ~0.5s).  Use one
        # stable operation per turn so DB/state keep only the latest snapshot
        # instead of appending an operation for every timer tick.
        stats = event.get("stats") if isinstance(event.get("stats"), dict) else {}
        op_key = execution_run_uuid or turn_uuid or run_root_turn_uuid or str(event.get("eventUuid") or event.get("eventId") or "current")
        specs.append(_operation_spec(
            op_id=f"stats:{op_key}",
            op_type="stats", action="patch", turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            run_id=execution_run_uuid,
            payload={**stats, "createdAtMs": ts, "updatedAtMs": ts}, source="system",
        ))
        return specs

    if typ == "notice":
        specs.append(_operation_spec(
            op_id=f"notice:{str(event.get('eventUuid') or event.get('eventId') or ts)}",
            op_type="notice", action="create", turn_uuid=turn_uuid,
            payload={"text": str(event.get("text") or ""), "level": "info", "footer": str(event.get("footer") or ""), "createdAtMs": ts},
            source="system",
        ))
        return specs

    if typ == "error":
        err = str(event.get("error") or event.get("message") or event.get("text") or "Web 对话出错")
        specs.append(_operation_spec(
            op_id=f"assistant-error:{turn_uuid or str(event.get('eventUuid') or event.get('eventId') or ts)}",
            op_type="assistant_message", action="error", turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            run_id=execution_run_uuid,
            payload={"text": err, "error": True, "complete": True, "status": "failed", "runId": execution_run_uuid},
            status="failed", source="assistant",
        ))
        if execution_run_uuid:
            specs.append(_operation_spec(
                op_id=f"run:{execution_run_uuid}", op_type="run", action="error", turn_uuid=turn_uuid,
                run_root_turn_uuid=run_root_turn_uuid,
                run_id=execution_run_uuid,
                payload={"runId": execution_run_uuid, "status": "failed", "error": err}, status="failed", source="system", skip_if_missing=True,
            ))
        return specs

    if typ == "stopped":
        specs.append(_operation_spec(
            op_id=f"control:{str(event.get('eventUuid') or event.get('eventId') or ts)}",
            op_type="run_control", action="stop", turn_uuid=turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
            run_id=execution_run_uuid,
            payload={"reason": str(event.get("reason") or "已停止"), "status": "cancelled", "createdAtMs": ts, "runId": execution_run_uuid},
            status="cancelled", source="system",
        ))
        if execution_run_uuid:
            specs.append(_operation_spec(
                op_id=f"run:{execution_run_uuid}", op_type="run", action="cancel", turn_uuid=turn_uuid,
                run_root_turn_uuid=run_root_turn_uuid,
                run_id=execution_run_uuid,
                payload={"runId": execution_run_uuid, "status": "cancelled", "reason": str(event.get("reason") or "已停止")},
                status="cancelled", source="system", skip_if_missing=True,
            ))
        return specs

    if typ == "done":
        if execution_run_uuid:
            specs.append(_operation_spec(
                op_id=f"run:{execution_run_uuid}", op_type="run", action="end", turn_uuid=turn_uuid,
                run_root_turn_uuid=run_root_turn_uuid,
                run_id=execution_run_uuid,
                payload={"runId": execution_run_uuid, "status": "completed", "completedAtMs": ts},
                status="completed", source="system", skip_if_missing=True,
            ))
        return specs

    return specs



def _payload_task_uuid(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    direct = str(value.get("taskUuid") or value.get("task_uuid") or "").strip()
    if direct:
        return direct
    for key in ("task", "result", "agentSession"):
        nested = value.get(key)
        if isinstance(nested, dict):
            found = _payload_task_uuid(nested)
            if found:
                return found
            if key == "agentSession":
                sid = str(nested.get("lastTaskUuid") or nested.get("last_task_uuid") or "").strip()
                if sid:
                    return sid
    results = value.get("results")
    if isinstance(results, list):
        for item in results:
            found = _payload_task_uuid(item)
            if found:
                return found
    task_uuids = value.get("taskUuids")
    if isinstance(task_uuids, list):
        for item in task_uuids:
            text = str(item or "").strip()
            if text:
                return text
    return ""


def _operation_target_fields(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    stored_target_type = str(row.get("target_type") or row.get("targetType") or "").strip()
    stored_target_id = str(row.get("target_id") or row.get("targetId") or "").strip()
    stored_task_uuid = str(row.get("task_uuid") or row.get("taskUuid") or "").strip()
    stored_run_id = str(row.get("run_id") or row.get("runId") or "").strip()
    if stored_target_type or stored_target_id or stored_task_uuid or stored_run_id:
        return {
            "targetType": stored_target_type or ("task" if stored_task_uuid else "run" if stored_run_id else "conversation"),
            "targetId": stored_target_id or stored_task_uuid or stored_run_id,
            "taskUuid": stored_task_uuid,
            "runId": stored_run_id,
        }
    op_type = str(row.get("op_type") or row.get("opType") or "")
    turn_uuid = str(row.get("turn_uuid") or row.get("turnUuid") or row.get("turnId") or payload.get("turnId") or "").strip()
    task_uuid = _payload_task_uuid(payload)
    run_id = str(payload.get("runId") or payload.get("run_id") or turn_uuid or "").strip()
    if task_uuid and op_type in {"agent", "agent_control", "notice"}:
        return {"targetType": "task", "targetId": task_uuid, "taskUuid": task_uuid, "runId": run_id}
    if op_type in {"run", "user_message", "assistant_message", "reasoning", "tool", "user_interaction", "context_compaction", "status", "model_retry", "run_control", "stats"}:
        return {"targetType": "run" if run_id else "conversation", "targetId": run_id, "taskUuid": task_uuid, "runId": run_id}
    if task_uuid:
        return {"targetType": "task", "targetId": task_uuid, "taskUuid": task_uuid, "runId": run_id}
    return {"targetType": "conversation", "targetId": "", "taskUuid": "", "runId": run_id}


_TOOL_SUMMARY_PAYLOAD_FIELDS = frozenset({
    "toolCallId", "name", "toolName", "rootToolName",
    "durationMs", "startedAtMs", "updatedAtMs", "terminalAtMs", "status",
    "taskUuid", "runId",
    # ContextCompaction has a separate full-summary endpoint.  These bounded
    # facts are enough to keep its collapsed card and lazy loader meaningful.
    "compactionId", "summaryId", "scope", "source", "beforeTokens",
    "afterTokens", "summaryChars", "summaryTokens", "upToMessageId",
    "outputAvailable", "outputPreview", "summaryRef",
})
_TOOL_SUMMARY_TEXT_CHARS = 512
_TOOL_ERROR_STATUSES = {"failed", "cancelled", "interrupted"}
# Keep this order aligned with the established browser-side collapsed-card
# renderer.  The list API sends only the one source value it needs, while the
# browser keeps ownership of the visual presentation.
_TOOL_PREVIEW_ARGUMENT_KEYS = (
    "description", "command", "pattern", "query", "title", "body", "name",
    "ref", "path", "old_string", "content", "action", "text", "instruction", "task",
)


def _tool_summary_text(value: Any, *, limit: int = _TOOL_SUMMARY_TEXT_CHARS) -> str:
    text = str(value or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1].rstrip()}…"


def _tool_result_state(payload: dict[str, Any], status: str) -> str:
    """Classify a tool outcome without returning its full result body."""
    normalized_status = str(status or payload.get("status") or "").strip().lower()
    if normalized_status in _TOOL_ERROR_STATUSES:
        return "error"
    if normalized_status in ACTIVE_STATUSES:
        return "running"
    raw_result = payload.get("resultText") if "resultText" in payload else payload.get("result")
    result_text = str(raw_result or "").strip()
    error_text = str(payload.get("error") or "").strip()
    if (
        error_text
        or result_text.startswith("[错误]")
        or result_text.lower().startswith("error:")
        or "工具调用被中止" in result_text
        or re.search(r"^status:\s*(failed|timeout|killed)\b", result_text, re.IGNORECASE | re.MULTILINE)
        or '"exitCode": 127' in result_text
    ):
        return "error"
    if normalized_status in TERMINAL_STATUSES:
        return "ok"
    return "ok" if result_text else "running"


def _summary_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return json_loads_dict(str(value or "{}"))


def _task_memory_preview_arguments(payload: dict[str, Any]) -> str:
    """Build stable TaskMemory card facts for lazy operation snapshots."""
    arguments = _summary_json_object(payload.get("arguments") or payload.get("args"))
    action = str(arguments.get("action") or "").strip().lower()
    if not action:
        return ""
    preview: dict[str, Any] = {"action": action}
    if action == "search":
        query = str(arguments.get("query") or "").strip()
        if query:
            preview["query"] = _tool_summary_text(query)
        return json_dumps(preview)
    if action == "list":
        return json_dumps(preview)

    raw_result = payload.get("resultText") if "resultText" in payload else payload.get("result")
    result = _summary_json_object(raw_result)
    memory = result.get("memory") if isinstance(result.get("memory"), dict) else {}
    label = next((
        str(value).strip()
        for value in (
            memory.get("description"), memory.get("name"),
            arguments.get("description"), arguments.get("name"),
        )
        if str(value or "").strip()
    ), "")
    if label:
        preview["memoryLabel"] = _tool_summary_text(label)
    memory_uuid = str(memory.get("memoryUuid") or arguments.get("memoryUuid") or "").strip()
    if memory_uuid:
        preview["memoryUuid"] = _tool_summary_text(memory_uuid)
    return json_dumps(preview)


def _tool_preview_arguments(payload: dict[str, Any]) -> str:
    """Return a bounded source for the existing browser-side card preview.

    ``preview`` is a renderer line (for example ``💻 Bash: ...``), so returning
    it to a card that already renders the icon and tool name duplicates both.
    This field deliberately contains argument source only, never a newly
    formatted UI label.  Small argument objects are retained verbatim; for a
    large object, retain the same first display candidate used by the browser.
    """
    tool_name = str(payload.get("name") or payload.get("toolName") or "").strip()
    if tool_name == "TaskMemory":
        return _task_memory_preview_arguments(payload)
    raw = payload.get("arguments") or payload.get("args")
    if raw is None:
        return ""
    if isinstance(raw, str):
        raw_text = raw.strip()
    else:
        try:
            raw_text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raw_text = str(raw or "").strip()
    if not raw_text:
        return ""
    if len(raw_text) <= _TOOL_SUMMARY_TEXT_CHARS:
        return raw_text

    arguments = _summary_json_object(raw)
    if not arguments:
        return _tool_summary_text(raw_text)
    for key in _TOOL_PREVIEW_ARGUMENT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return json.dumps({key: _tool_summary_text(value)}, ensure_ascii=False, separators=(",", ":"))
    for key, value in arguments.items():
        if isinstance(value, str) and value.strip():
            return json.dumps({str(key): _tool_summary_text(value)}, ensure_ascii=False, separators=(",", ":"))
    return ""


def _user_interaction_public_state(
    payload: dict[str, Any], status: str = "",
) -> tuple[dict[str, Any], str, str]:
    sanitized = _sanitize_user_interaction_payload(payload)
    raw_result = sanitized.get("resultText") if "resultText" in sanitized else sanitized.get("result")
    if raw_result is not None:
        interaction_status, operation_status, action = _user_interaction_result_state(str(raw_result or ""))
    else:
        interaction_status = str(sanitized.get("interactionStatus") or "pending").strip().lower()
        if interaction_status not in _USER_INTERACTION_STATUS_MAP:
            interaction_status = "pending"
        operation_status, action = _USER_INTERACTION_STATUS_MAP[interaction_status]
        if str(status or "") in {"cancelled", "failed"}:
            operation_status = str(status)
            interaction_status = "cancelled" if operation_status == "cancelled" else "error"
            action = "cancel" if operation_status == "cancelled" else "error"
    sanitized["status"] = operation_status
    sanitized["interactionStatus"] = interaction_status
    return sanitized, operation_status, action


def _user_interaction_confirmed_flag(result: dict[str, Any]) -> bool | None:
    """Return confirm polarity without treating a missing answer as rejection."""
    if "confirmed" in result:
        return bool(result.get("confirmed"))
    choice = str(result.get("choice") or "").strip().lower()
    if choice == "confirm":
        return True
    if choice == "cancel":
        return False
    return None


def _user_interaction_summary_payload(payload: dict[str, Any], status: str) -> dict[str, Any]:
    sanitized, operation_status, _action = _user_interaction_public_state(payload, status)
    arguments = _summary_json_object(sanitized.get("arguments") or sanitized.get("args"))
    action = str(arguments.get("action") or "").strip().lower()
    summary: dict[str, Any] = {
        "action": action,
        "title": str(arguments.get("title") or ""),
        "status": operation_status,
        "interactionStatus": str(sanitized.get("interactionStatus") or "pending"),
        "sensitive": bool(arguments.get("sensitive") or arguments.get("secret")),
    }
    if action == "confirm":
        raw_result = sanitized.get("resultText") if "resultText" in sanitized else sanitized.get("result")
        confirmed = _user_interaction_confirmed_flag(_summary_json_object(raw_result))
        if confirmed is not None:
            summary["confirmed"] = confirmed
    for key in ("durationMs", "startedAtMs", "updatedAtMs", "terminalAtMs"):
        if key in sanitized:
            summary[key] = copy.deepcopy(sanitized[key])
    return summary


def _sanitize_user_interaction_debug(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_user_interaction_debug(item) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"preview", "outputPreview", "searchText"}:
            continue
        if key in {"value", "defaultValue"}:
            out[key] = "[敏感内容已隐藏]"
        else:
            out[key] = _sanitize_user_interaction_debug(item)
    return out


def _tool_summary_payload(payload: dict[str, Any], status: str) -> dict[str, Any]:
    """Return the bounded payload needed to render one collapsed tool card."""
    summary = {
        key: copy.deepcopy(payload[key])
        for key in _TOOL_SUMMARY_PAYLOAD_FIELDS
        if key in payload
    }
    preview_arguments = _tool_preview_arguments(payload)
    if preview_arguments:
        summary["previewArguments"] = preview_arguments
    if "outputPreview" in summary:
        summary["outputPreview"] = _tool_summary_text(summary["outputPreview"])
    summary["resultState"] = _tool_result_state(payload, status)
    return summary


def operation_public(row: dict[str, Any], *, include_tool_details: bool = True) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else json_loads_dict(str(row.get("payload_json") or "{}"))
    transcript_candidate = row.get("transcriptMessageIds")
    transcript_ids = transcript_candidate if isinstance(transcript_candidate, list) else json_loads_list(str(row.get("transcript_message_ids_json") or "[]"))
    stored_op_type = str(row.get("op_type") or row.get("opType") or "")
    is_interaction = is_user_interaction_operation(row, payload=payload)
    op_type = (
        "user_interaction" if is_interaction
        else "context_compaction" if is_context_compaction_operation(row, payload=payload)
        else stored_op_type
    )
    if is_interaction:
        payload, canonical_status, _canonical_action = _user_interaction_public_state(
            payload, str(row.get("status") or payload.get("status") or ""),
        )
    else:
        canonical_status = ""
    target = _operation_target_fields({**row, "op_type": op_type}, payload)
    lifecycle = "informational" if op_type == "notice" else str(row.get("lifecycle") or "")
    revision = int(row.get("revision") or 0)
    status = canonical_status or str(row.get("status") or payload.get("status") or "")
    public = {
        "conversationId": str(row.get("conversation_uuid") or row.get("conversationId") or ""),
        "conversationUuid": str(row.get("conversation_uuid") or row.get("conversationUuid") or row.get("conversationId") or ""),
        "internalChatId": int(row.get("internal_chat_id") or row.get("internalChatId") or 0),
        "opId": str(row.get("op_id") or row.get("opId") or ""),
        "opType": op_type,
        "turnId": str(row.get("turn_uuid") or row.get("turnId") or ""),
        "turnUuid": str(row.get("turn_uuid") or row.get("turnUuid") or row.get("turnId") or ""),
        "parentTurnId": str(row.get("parent_turn_uuid") or row.get("parentTurnId") or ""),
        "runRootTurnId": str(row.get("run_root_turn_uuid") or row.get("runRootTurnId") or ""),
        "displaySeq": int(row.get("display_seq") or row.get("displaySeq") or 0),
        "createdAtMs": int(row.get("created_at_ms") or row.get("createdAtMs") or 0),
        "updatedAtMs": int(row.get("updated_at_ms") or row.get("updatedAtMs") or 0),
        "terminalAtMs": int(row.get("terminal_at_ms") or row.get("terminalAtMs") or payload.get("terminalAtMs") or 0),
        "revision": revision,
        "status": status,
        "lifecycle": lifecycle,
        "internal": bool(row.get("internal") or False),
        "source": "user_interaction" if is_interaction else str(row.get("source") or ""),
        "transcriptMessageIds": transcript_ids,
        **target,
        "payload": payload,
    }
    if op_type in {"tool", "context_compaction", "user_interaction"}:
        public.update({
            "detailAvailable": True,
            "detailLoaded": bool(include_tool_details),
            "detailRevision": revision,
        })
        if not include_tool_details:
            public["payload"] = (
                _user_interaction_summary_payload(payload, status)
                if is_interaction else _tool_summary_payload(payload, status)
            )
    return public


def frame_public(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else json_loads_dict(str(row.get("payload_json") or "{}"))
    debug = row.get("debug") if isinstance(row.get("debug"), dict) else json_loads_dict(str(row.get("debug_json") or "{}"))
    stored_op_type = str(row.get("op_type") or row.get("opType") or "")
    is_interaction = is_user_interaction_operation(row, payload=payload)
    op_type = (
        "user_interaction" if is_interaction
        else "context_compaction" if is_context_compaction_operation(row, payload=payload)
        else stored_op_type
    )
    canonical_action = str(row.get("action") or "")
    if is_interaction:
        payload, _canonical_status, canonical_action = _user_interaction_public_state(
            payload, str(payload.get("status") or ""),
        )
        debug = _sanitize_user_interaction_debug(debug)
    target = _operation_target_fields({**row, "op_type": op_type}, payload)
    return {
        "frameSeq": int(row.get("frame_seq") or row.get("frameSeq") or 0),
        "conversationId": str(row.get("conversation_uuid") or row.get("conversationId") or ""),
        "conversationUuid": str(row.get("conversation_uuid") or row.get("conversationUuid") or row.get("conversationId") or ""),
        "internalChatId": int(row.get("internal_chat_id") or row.get("internalChatId") or 0),
        "ownerChatId": int(row.get("owner_chat_id") or row.get("ownerChatId") or 0),
        "turnId": str(row.get("turn_uuid") or row.get("turnId") or ""),
        "turnUuid": str(row.get("turn_uuid") or row.get("turnUuid") or row.get("turnId") or ""),
        "parentTurnId": str(row.get("parent_turn_uuid") or row.get("parentTurnId") or ""),
        "runRootTurnId": str(row.get("run_root_turn_uuid") or row.get("runRootTurnId") or ""),
        "opId": str(row.get("op_id") or row.get("opId") or ""),
        "opType": op_type,
        "action": canonical_action,
        "revision": int(row.get("revision") or 0),
        "displaySeq": int(row.get("display_seq") or row.get("displaySeq") or 0),
        "createdAtMs": int(row.get("created_at_ms") or row.get("createdAtMs") or 0),
        "updatedAtMs": int(row.get("updated_at_ms") or row.get("updatedAtMs") or 0),
        **target,
        "payload": payload,
        "debug": debug,
    }
