"""Rath task data types.

These are deliberately small DTOs over SQLite rows.  They keep the first
implementation framework-agnostic: OpenBear can run Bear-owned workflows today
and later swap individual internals to OpenRath proper without changing the
persistence/API surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TaskStatus = Literal[
    "queued",
    "running",
    "pausing",
    "paused",
    "resuming",
    "stopping",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "needs_openbear_control",
]
ControlAction = Literal["pause", "resume", "stop", "steer"]
ControlStatus = Literal["pending", "applied", "ignored", "failed"]

ACTIVE_TASK_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "pausing",
    "paused",
    "resuming",
    "stopping",
)
CONTROLLABLE_TASK_STATUSES: tuple[str, ...] = (*ACTIVE_TASK_STATUSES, "needs_openbear_control")
TERMINAL_TASK_STATUSES: tuple[str, ...] = (
    "completed",
    "failed",
    "cancelled",
    "interrupted",
)


@dataclass(slots=True)
class RathWorkflow:
    workflow_uuid: str
    slug: str
    name: str
    description: str = ""
    kind: str = ""
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0
    updated_at: int = 0
    id: int = 0


@dataclass(slots=True)
class RathAgentDef:
    agent_key: str
    name: str
    description: str = ""
    system_prompt: str = ""
    model: str = ""
    think_level: str = ""
    tool_allowlist: list[str] = field(default_factory=list)
    sort: int = 0
    enabled: bool = True
    created_at: int = 0
    updated_at: int = 0
    id: int = 0
    workflow_uuid: str = ""


@dataclass(slots=True)
class RathAgentSession:
    session_uuid: str
    openbear_session_uuid: str = ""
    chat_id: int = 0
    workflow_uuid: str = ""
    agent_key: str = ""
    status: str = "active"
    title: str = ""
    summary: str = ""
    last_task_uuid: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0
    updated_at: int = 0
    closed_at: int = 0
    id: int = 0


@dataclass(slots=True)
class RathTask:
    task_uuid: str
    chat_id: int = 0
    parent_session_uuid: str = ""
    agent_session_uuid: str = ""
    caller_agent_session_uuid: str = ""
    workflow_uuid: str = ""
    title: str = ""
    status: str = "queued"
    control_state: str = ""
    current_agent_key: str = ""
    current_status: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    model_call_count: int = 0
    tool_call_count: int = 0
    work_tool_call_count: int = 0
    plan_tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_cache_read_tokens: int = 0
    last_cache_write_tokens: int = 0
    cost_usd: float = 0.0
    started_at: int = 0
    updated_at: int = 0
    finished_at: int = 0
    # Parent Web/main-controller ownership for turn deletion / audit.
    parent_task_uuid: str = ""
    turn_uuid: str = ""
    parent_turn_uuid: str = ""
    run_root_turn_uuid: str = ""
    id: int = 0


@dataclass(slots=True)
class RathTaskEvent:
    task_uuid: str
    seq: int
    kind: str
    ts: int = 0
    agent_key: str = ""
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    id: int = 0


@dataclass(slots=True)
class RathArtifact:
    artifact_uuid: str
    task_uuid: str
    agent_key: str = ""
    kind: str = ""
    name: str = ""
    summary: str = ""
    content: str = ""
    content_type: str = "text/plain"
    source_refs: list[Any] = field(default_factory=list)
    size_bytes: int = 0
    created_at: int = 0
    id: int = 0


@dataclass(slots=True)
class RathControl:
    control_uuid: str
    task_uuid: str
    action: str
    message: str = ""
    requested_by: str = ""
    status: str = "pending"
    created_at: int = 0
    applied_at: int = 0
    result: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    response_status: str = ""
    response_reason: str = ""
    response_plan_impact: str = ""
    response_next_action: str = ""
    responded_at: int = 0
    id: int = 0
