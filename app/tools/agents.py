"""OpenBear Agent orchestration tools.

Rath remains the durable execution substrate, but the main OpenBear model sees
only a Claude-Code-style control surface: launch an Agent, message it, or stop it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from app.agent.compaction import CompressionCandidate
from app.config import Config
from app.db.dao import MessageDAO
from app.llm.events import Usage
from app.llm.factory import BackendFactory
from app.models.agent_runtime import resolve_agent_runtime_config
from app.models.selection import ModelSelection
from app.rath.agent_prompt import render_agent_base_system_prompt
from app.rath.builtin_workflows import SINGLE_AGENT_WORKFLOW_SLUG, ensure_builtin_workflows
from app.rath.controller_projection import project_agent_payload_for_controller
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.rath.plan import AgentPlanCoordinator, PlanError, register_agent_plan_tools
from app.rath.schemas import (
    ACTIVE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    RathAgentDef,
    RathAgentSession,
    RathTask,
)
from app.rath.single_agent import (
    SingleAgentWorkflowRunner,
    agent_to_snapshot,
    safe_agent_llm_session_id,
)
from app.stream.tool_progress import (
    format_agent_task_progress_card,
)
from app.tools.allowlist import AGENT_DELEGATION_TOOL_NAMES, sanitize_tool_allowlist
from app.tools.base import (
    ToolRegistry,
    ToolRuntimeContext,
    current_tool_context,
    max_tool_result_chars,
)
from app.utils import estimate_tokens

_AGENT_PLAN_MODE_DIRECT = "direct"
_AGENT_PLAN_MODE_MANAGED = "managed"
_AGENT_PLAN_MODES = frozenset({_AGENT_PLAN_MODE_DIRECT, _AGENT_PLAN_MODE_MANAGED})


def _normalize_agent_plan_mode(value: Any, *, default: str = _AGENT_PLAN_MODE_DIRECT) -> str:
    mode = str(value or "").strip().lower()
    return mode or default


def _task_agent_plan_mode(task: RathTask | None) -> str:
    """Return the task's frozen Plan mode.

    Tasks created before per-task Plan modes existed have no planMode field and
    keep the historical managed behavior. New tasks always persist the field.
    """
    if task is None or not isinstance(task.input, dict) or "planMode" not in task.input:
        return _AGENT_PLAN_MODE_MANAGED
    mode = _normalize_agent_plan_mode(task.input.get("planMode"))
    return mode if mode in _AGENT_PLAN_MODES else _AGENT_PLAN_MODE_MANAGED


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _events_public(events) -> list[dict[str, Any]]:
    return [
        {
            "taskUuid": e.task_uuid,
            "seq": e.seq,
            "ts": e.ts,
            "kind": e.kind,
            "agentKey": e.agent_key,
            "summary": e.summary,
            "detail": e.detail,
            "elapsedMs": e.elapsed_ms,
        }
        for e in events
    ]


def _task_public(
    task: RathTask | None,
    *,
    include_output: bool = False,
    model: str = "",
    context_window: int = 0,
) -> dict[str, Any]:
    if task is None:
        return {}
    snapshot = task.input.get("agentSnapshot") if isinstance(task.input, dict) else {}
    model_label = model or str((snapshot or {}).get("model") or "")
    task_short_id = str(task.task_uuid or "")[:8]
    display_base = str((snapshot or {}).get("name") or task.current_agent_key or "Agent").strip() or "Agent"
    context_tokens = task.last_input_tokens + task.last_cache_read_tokens + task.last_cache_write_tokens
    started_at_ms = int(task.started_at or 0) * 1000
    finished_at_ms = int(task.finished_at or 0) * 1000
    duration_ms = max(0, finished_at_ms - started_at_ms) if started_at_ms and finished_at_ms else 0
    retry_state = task.output.get("retry") if isinstance(task.output, dict) else None
    include_runtime_output = isinstance(retry_state, dict)
    return {
        "taskUuid": task.task_uuid,
        "taskShortId": task_short_id,
        "displayName": f"{display_base}-{task_short_id}" if task_short_id else display_base,
        "title": task.title,
        "status": task.status,
        "currentAgent": task.current_agent_key,
        "currentStatus": task.current_status,
        "openbearSessionUuid": task.parent_session_uuid,
        "agentSessionUuid": task.agent_session_uuid,
        "callerAgentSessionUuid": task.caller_agent_session_uuid,
        "parentTaskUuid": getattr(task, "parent_task_uuid", "") or "",
        # Parent Web ownership: taskUuid is one Agent run; agentSessionUuid is continuity.
        "turnUuid": getattr(task, "turn_uuid", "") or "",
        "parentTurnUuid": getattr(task, "parent_turn_uuid", "") or "",
        "runRootTurnUuid": getattr(task, "run_root_turn_uuid", "") or "",
        "planMode": _task_agent_plan_mode(task),
        "model": model_label,
        "modelCalls": task.model_call_count,
        "toolCalls": task.tool_call_count,
        "workToolCalls": task.work_tool_call_count,
        "planToolCalls": task.plan_tool_call_count,
        "tokens": {
            "input": task.input_tokens + task.cache_read_tokens + task.cache_write_tokens,
            "output": task.output_tokens,
            "cache": task.cache_read_tokens + task.cache_write_tokens,
        },
        "lastUsage": {
            "inputTokens": task.last_input_tokens,
            "outputTokens": task.last_output_tokens,
            "cacheReadTokens": task.last_cache_read_tokens,
            "cacheWriteTokens": task.last_cache_write_tokens,
        },
        "contextTokens": context_tokens,
        "contextWindow": int(context_window or 0),
        "costUsd": task.cost_usd,
        "startedAtMs": started_at_ms,
        "finishedAtMs": finished_at_ms,
        "durationMs": duration_ms,
        "error": task.error,
        **({"output": task.output} if include_output or include_runtime_output else {}),
    }


def _agent_progress_signature(
    task_payload: dict[str, Any],
    event_payload: list[dict[str, Any]],
    result_payload: dict[str, Any] | None,
    agent_session: dict[str, Any],
    ledger_usage: dict[str, Any] | None = None,
) -> str:
    """Return a durable-progress signature without timer-only fields.

    Detached progress has a client-side elapsed clock.  Persisting another full
    Agent snapshot merely because duration/updatedAt changed creates frames with
    no new task fact, so only task/session state and the latest durable events
    participate in this signature.
    """
    stable_task = {key: value for key, value in task_payload.items() if key not in {"durationMs", "updatedAtMs"}}
    return json.dumps(
        {
            "task": stable_task,
            "events": event_payload,
            "result": result_payload,
            "agentSession": agent_session,
            "ledgerUsage": ledger_usage,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _task_status_label(status: str) -> str:
    return {
        "completed": "任务完成",
        "needs_openbear_control": "等待 OpenBear 裁决",
        "failed": "任务失败",
        "cancelled": "任务已取消",
        "interrupted": "任务已中断",
        "running": "执行中",
        "queued": "排队中",
        "paused": "已暂停",
    }.get(status or "", status or "执行中")


def _render_agent_task_notification(payload: dict[str, Any]) -> str:
    """Render one complete but telemetry-free Agent notification for the controller."""
    controller_payload = project_agent_payload_for_controller(payload)
    compact = {
        key: value
        for key, value in controller_payload.items()
        if key != "content" and not str(key).startswith("_")
    }
    status = str(controller_payload.get("status") or "").strip()
    if status == "needs_openbear_control":
        task_payload = controller_payload.get("task") if isinstance(controller_payload.get("task"), dict) else {}
        task_output = task_payload.get("output") if isinstance(task_payload.get("output"), dict) else {}
        result_payload = controller_payload.get("result") if isinstance(controller_payload.get("result"), dict) else {}
        reason = str(
            controller_payload.get("reason")
            or task_output.get("reason")
            or result_payload.get("reason")
            or ""
        )
        if reason == "agent_plan_blocked":
            plan_runtime = controller_payload.get("planRuntime") if isinstance(controller_payload.get("planRuntime"), dict) else {}
            plan_state = plan_runtime.get("state") if isinstance(plan_runtime.get("state"), dict) else {}
            active_version = int(plan_state.get("active_plan_version") or 0)
            instruction = (
                "This is an internal Agent Plan blocked-control boundary, not task completion. "
                f"Review the blocker and active Plan v{active_version}. If the task should continue, first call "
                f"AgentPlanDecision with taskUuid={controller_payload.get('taskUuid')}, expectedPlanVersion={active_version}, "
                "action=request_replan, and a concrete reason. Then call AgentMessage with narrow guidance so the same "
                "Agent resumes from its saved continuation state and submits AgentPlanReplan. If continuing is no longer "
                "appropriate, call AgentStop. Do not ask the worker to continue the blocked Plan unchanged."
            )
        elif reason == "agent_context_overflow_unrecoverable":
            instruction = (
                "This Agent task cannot safely continue because its saved context already exceeds the model window. "
                "Call AgentStop for this task, then create a fresh Agent task with a materially narrower prompt and minimal inputs. "
                "Do not call AgentMessage on the overflowing task and do not present a final synthesis until required sibling tasks are terminal."
            )
        else:
            instruction = (
                "This is an internal background Agent control notification, not a task completion and not a new user request. "
                "The Agent is paused at a hard runtime safety or explicit control boundary. Continue as the OpenBear main controller. "
                "Judge continuation only from the approved Plan, durable evidence, real blockers, scope, and safety; never from usage telemetry. "
                "If work remains, call AgentMessage with narrow guidance; if the Plan no longer requires work, call AgentStop or explain the decision. "
                "Do not present a final user-facing synthesis until all required sibling Agent tasks are truly terminal."
            )
    else:
        plan_runtime = controller_payload.get("planRuntime") if isinstance(controller_payload.get("planRuntime"), dict) else {}
        plan_state = plan_runtime.get("state") if isinstance(plan_runtime.get("state"), dict) else {}
        plan_phase = str(plan_state.get("phase") or "")
        completion_gate = (
            f" The attached Plan Runtime phase is {plan_phase or 'unavailable'}. Before synthesis, verify it is finalizing, "
            "all required steps/criteria are completed with evidence, and every declared final output has sources. "
            "If the attached state is inconsistent, do not claim successful completion; report the control failure."
            if plan_runtime
            else ""
        )
        instruction = (
            "This is an internal background Agent completion notification, not a new user request. "
            "Continue as the OpenBear main controller. Use the Agent result above and the existing conversation/tool history."
            + completion_gate
            + " This terminal result completes only its delegated work package; it does not end the root task or restrict controller tools. "
            "Do not redo the same delegated work. Integrate the result, then continue controller-owned work, user decisions, artifact creation, "
            "or newly scoped Agent packages as required. Give the final user-facing answer only when the root objective is complete."
        )
    return (
        "<task-notification>\n"
        + json.dumps(compact, ensure_ascii=False, indent=2, default=str)
        + "\n</task-notification>\n\n"
        + instruction
    )


def _agent_result_output_tokens(task: RathTask | None, result: dict[str, Any]) -> int:
    """Return final Agent output tokens, with a conservative local fallback."""
    actual = max(0, int(getattr(task, "last_output_tokens", 0) or 0)) if task is not None else 0
    if actual > 0:
        return actual
    if not result:
        return 0
    return max(0, estimate_tokens(json.dumps(result, ensure_ascii=False, default=str)))


_ACTIVE_TASK_STATUSES = set(ACTIVE_TASK_STATUSES)
_NOTIFIABLE_AGENT_STATUSES = set(TERMINAL_TASK_STATUSES) | {"needs_openbear_control"}
_DETACHED_PROGRESS_POLL_INTERVAL_S = 10.0


class AgentTools:
    def __init__(
        self,
        *,
        config: Config,
        dao: RathDAO,
        manager: RathTaskManager,
        llm_factory: BackendFactory,
        model_selection: ModelSelection,
        registry: ToolRegistry,
        messages: MessageDAO | None = None,
        workspace_dir: str = "",
    ) -> None:
        self.config = config
        self.dao = dao
        self.manager = manager
        self.llm_factory = llm_factory
        self.model_selection = model_selection
        self.registry = registry
        self.messages = messages or MessageDAO(dao.db)
        self.workspace_dir = str(workspace_dir or "").strip()
        coordinator = manager.plan_coordinator
        if coordinator is None:
            rath_config = config.rath
            coordinator = AgentPlanCoordinator(
                dao,
                manager,
                max_revision_rounds=int(getattr(rath_config, "agent_plan_max_revision_rounds", 3) or 3),
                max_steps=int(getattr(rath_config, "agent_plan_max_steps", 30) or 30),
                max_criteria_per_step=int(
                    getattr(rath_config, "agent_plan_max_criteria_per_step", 10) or 10
                ),
                max_final_outputs=int(getattr(rath_config, "agent_plan_max_final_outputs", 20) or 20),
                plan_review_prompt=str(getattr(rath_config, "plan_review_prompt", "") or ""),
            )
            manager.plan_coordinator = coordinator
        self.plan: AgentPlanCoordinator = coordinator
        self.plan.max_revision_rounds = max(1, int(getattr(config.rath, "agent_plan_max_revision_rounds", 3) or 3))
        self.plan.max_steps = max(1, int(getattr(config.rath, "agent_plan_max_steps", 30) or 30))
        self.plan.max_criteria_per_step = max(
            1, int(getattr(config.rath, "agent_plan_max_criteria_per_step", 10) or 10)
        )
        self.plan.max_final_outputs = max(1, int(getattr(config.rath, "agent_plan_max_final_outputs", 20) or 20))
        self.plan.plan_review_prompt = str(getattr(config.rath, "plan_review_prompt", "") or "")

    def _plan_prompts(self) -> dict[str, str]:
        rath = self.config.rath
        return {
            "rath.planDraftPrompt": str(getattr(rath, "plan_draft_prompt", "") or ""),
            "rath.planRevisionPrompt": str(getattr(rath, "plan_revision_prompt", "") or ""),
            "rath.planExecutionPrompt": str(getattr(rath, "plan_execution_prompt", "") or ""),
            "rath.planContextRestorePrompt": str(getattr(rath, "plan_context_restore_prompt", "") or ""),
        }

    async def _plan_notification_snapshot(self, task_uuid: str) -> dict[str, Any]:
        task = await self.dao.get_task(task_uuid)
        if _task_agent_plan_mode(task) == _AGENT_PLAN_MODE_DIRECT:
            return {}
        try:
            snapshot = await self.plan.snapshot(task_uuid)
        except Exception:
            return {}
        state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
        active_version = int(state.get("active_plan_version") or 0)
        pending_version = int(state.get("pending_plan_version") or 0)
        visible_version = pending_version or active_version
        version = next(
            (
                item
                for item in reversed(snapshot.get("versions") or [])
                if int(item.get("version") or 0) == visible_version
            ),
            None,
        )
        relevant_versions = {active_version, pending_version} - {0}
        steps = [
            item
            for item in snapshot.get("steps") or []
            if int(item.get("plan_version") or 0) in relevant_versions
            or str(item.get("status") or "") == "completed"
        ]
        evidence = [
            item
            for item in snapshot.get("evidence") or []
            if int(item.get("plan_version") or 0) in relevant_versions
            or any(
                str(step.get("step_id") or "") == str(item.get("step_id") or "")
                and str(step.get("status") or "") == "completed"
                for step in steps
            )
        ]
        return {
            "state": state,
            "planVersion": visible_version,
            "plan": (version or {}).get("plan") or {},
            "steps": steps,
            "evidence": evidence,
            "recentDecisions": list(snapshot.get("decisions") or [])[-10:],
        }

    async def _conversation_for_chat_id(self, chat_id: int) -> dict[str, Any] | None:
        if not chat_id:
            return None
        cur = await self.dao.db.conn.execute(
            "SELECT * FROM web_conversations WHERE internal_chat_id=? LIMIT 1",
            (int(chat_id),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _resolve_agent_runtime(
        self,
        agent: RathAgentDef,
        *,
        chat_id: int = 0,
        conversation: dict[str, Any] | None = None,
        frozen: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conv = conversation if conversation is not None else await self._conversation_for_chat_id(chat_id)
        main_model = str((conv or {}).get("model") or "") or self.model_selection.current or self.config.models.primary
        main_fast = False
        if chat_id:
            with contextlib.suppress(Exception):
                main_fast = bool(await self.messages.get_fast_mode(int(chat_id)))
        return resolve_agent_runtime_config(
            agent,
            config=self.config,
            model_selection_current=str(self.model_selection.current or ""),
            conversation=conv,
            main_model=main_model,
            main_fast_requested=main_fast,
            frozen=frozen,
        )

    async def _persist_agent_model_call(
        self,
        *,
        chat_id: int,
        session_uuid: str,
        model_label: str,
        protocol: str,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        usage = Usage(
            input_tokens=max(0, int(detail.get("inputTokens") or 0)),
            output_tokens=max(0, int(detail.get("outputTokens") or 0)),
            cache_read_tokens=max(0, int(detail.get("cacheReadTokens") or 0)),
            cache_write_tokens=max(0, int(detail.get("cacheWriteTokens") or 0)),
        )
        usage.total_tokens = usage.input_tokens + usage.output_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        effective_model_label = str(detail.get("modelLabel") or model_label)
        effective_protocol = str(detail.get("protocol") or protocol)
        cost = max(0.0, float(detail.get("costUsd") or 0.0))
        duration_ms = max(0, int(detail.get("durationMs") or 0))
        status = str(detail.get("status") or "ok")
        ledger_usage: dict[str, Any]
        async with self.dao.db.accounting_transaction() as connection:
            accounting = MessageDAO(self.dao.db, connection=connection)
            await accounting.add_usage(
                chat_id,
                usage,
                cost,
                commit=False,
                last_usage=usage,
                last_cost_usd=cost,
                total_time_ms=duration_ms,
                run_total_time_ms=duration_ms,
                run_model_calls=1,
                model=effective_model_label,
                protocol=effective_protocol,
                think_level=str(detail.get("thinkLevel") or ""),
            )
            await accounting.add_turn_stats(
                chat_id,
                commit=False,
                model_calls=1,
                model_ok=1 if status == "ok" else 0,
                model_fail=0 if status == "ok" else 1,
                total_time_ms_sum=duration_ms,
                output_tokens_sum=usage.output_tokens,
            )
            model_call_id = await accounting.add_model_call(
                chat_id,
                commit=False,
                session_uuid=session_uuid,
                model=effective_model_label,
                protocol=effective_protocol,
                think_level=str(detail.get("thinkLevel") or ""),
                call_kind="agent_request",
                usage=usage,
                last_usage=usage,
                cost_usd=cost,
                total_time_ms=duration_ms,
                peak_tps=max(0.0, float(detail.get("tps") or 0.0)),
                min_tps=max(0.0, float(detail.get("tps") or 0.0)),
                status=status,
                model_call_count=1,
                model_ok_count=1 if status == "ok" else 0,
                model_fail_count=0 if status == "ok" else 1,
                error_type=str(detail.get("errorType") or ""),
            )
            cur = await connection.execute(
                """
                SELECT usage_input_tokens, usage_output_tokens,
                       usage_cache_read_tokens, usage_cache_write_tokens, usage_cost_usd
                FROM sessions WHERE chat_id=?
                """,
                (chat_id,),
            )
            row = await cur.fetchone()
            ledger_usage = {
                "ledgerRevision": max(0, int(model_call_id or 0)),
                "inputTokens": max(0, int((row["usage_input_tokens"] if row else 0) or 0)),
                "outputTokens": max(0, int((row["usage_output_tokens"] if row else 0) or 0)),
                "cacheReadTokens": max(0, int((row["usage_cache_read_tokens"] if row else 0) or 0)),
                "cacheWriteTokens": max(0, int((row["usage_cache_write_tokens"] if row else 0) or 0)),
                "costUsd": max(0.0, float((row["usage_cost_usd"] if row else 0.0) or 0.0)),
            }
        return ledger_usage

    def _agent_tool_names(self) -> list[str]:
        return sorted(set(self.registry.names(scope="agent")) & set(AGENT_DELEGATION_TOOL_NAMES))

    @staticmethod
    def _raw_tool_list(value: Any) -> list[Any] | None:
        if value is None:
            return None
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            with contextlib.suppress(Exception):
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            return [item.strip() for item in text.split(",")]
        return None

    def _resolve_requested_agent_tools(self, args: dict[str, Any], agent: RathAgentDef) -> dict[str, Any]:
        raw = None
        found = False
        for key in ("tools", "toolAllowlist", "tool_allowlist", "allowedTools"):
            if key in args:
                raw = args.get(key)
                found = True
                break
        available = self._agent_tool_names()
        if not found:
            return {
                "error": "agent_tools_required",
                "message": "Agent requires an explicit tools array. Pass only the minimal tools this worker may use, or [] for no tools.",
                "availableTools": available,
            }
        raw_items = self._raw_tool_list(raw)
        if raw_items is None:
            return {
                "error": "agent_tools_invalid",
                "message": "Agent tools must be an array of tool names (or a comma-separated compatibility string).",
                "availableTools": available,
            }
        requested = sanitize_tool_allowlist(raw_items)
        available_set = set(available)
        unknown = [name for name in requested if name not in available_set]
        if unknown:
            return {
                "error": "agent_tool_not_available",
                "message": "Agent requested tools that are not available to child Agents.",
                "unknownTools": unknown,
                "availableTools": available,
            }
        preset_allowed = sanitize_tool_allowlist(agent.tool_allowlist or [])
        if preset_allowed:
            preset_set = set(preset_allowed)
            denied_by_preset = [name for name in requested if name not in preset_set]
            if denied_by_preset:
                return {
                    "error": "agent_tool_not_allowed_by_preset",
                    "message": "Requested tools exceed this Agent preset's configured tool allowlist.",
                    "deniedTools": denied_by_preset,
                    "presetAllowedTools": preset_allowed,
                    "availableTools": available,
                }
        return {"tools": requested, "availableTools": available}

    async def agent(self, args: dict[str, Any]) -> str:
        """Claude-Code-style subagent entrypoint exposed to the main model."""
        ctx = current_tool_context()
        if ctx.source.startswith("agent:"):
            return "error: child Agents cannot launch other Agents. Delegate only from the OpenBear main controller."
        prompt = str(args.get("prompt") or args.get("instruction") or args.get("task") or "").strip()
        if not prompt:
            return "error: missing required prompt."
        inherit_ref = str(args.get("inheritFromTaskUuid") or args.get("inherit_from_task_uuid") or "").strip()
        plan_mode = _normalize_agent_plan_mode(
            args.get("planMode") if "planMode" in args else args.get("plan_mode"),
            default=_AGENT_PLAN_MODE_DIRECT,
        )
        if plan_mode not in _AGENT_PLAN_MODES:
            return _json({
                "ok": False,
                "error": "invalid_agent_plan_mode",
                "planMode": plan_mode,
                "allowedPlanModes": sorted(_AGENT_PLAN_MODES),
                "message": "planMode must be direct or managed.",
            })
        plan_capability_enabled = bool(getattr(self.config.rath, "agent_plan_enabled", True))
        if plan_mode == _AGENT_PLAN_MODE_MANAGED and not plan_capability_enabled:
            return _json({
                "ok": False,
                "error": "managed_agent_plan_disabled",
                "message": "planMode=managed is unavailable because the global Agent Plan capability is disabled.",
            })
        if plan_mode == _AGENT_PLAN_MODE_MANAGED and (
            ctx.task_notification is None or ctx.agent_wait is None
        ):
            return _json({
                "ok": False,
                "error": "controller_runtime_required",
                "message": (
                    "planMode=managed requires a main-controller runtime with durable task_notification, "
                    "AgentWait, and AgentPlanDecision support."
                ),
            })
        inherited_context: dict[str, Any] = {}
        if inherit_ref:
            inherited_context, inherit_error = await self._inherit_plan_context(inherit_ref)
            if inherit_error:
                return _json({"ok": False, **inherit_error})
            if plan_mode == _AGENT_PLAN_MODE_DIRECT:
                inherited_context = dict(inherited_context)
                inherited_context["instruction"] = (
                    "Treat these as durable facts and references only. Do not resume the old coroutine or transcript. "
                    "Continue only the remaining requested work directly, without repeating completed work."
                )
        worker_type = str(
            args.get("workerType")
            or args.get("subagent_type")
            or args.get("agent")
            or args.get("agentName")
            or args.get("agentKey")
            or "general-purpose"
        ).strip() or "general-purpose"
        agent = await self._resolve_agent_preset(worker_type)
        if agent is None:
            available = [a.agent_key for a in await self.dao.list_agents(include_disabled=False) if a.enabled]
            return _json({
                "ok": False,
                "error": "agent_preset_not_found",
                "workerType": worker_type,
                "availableWorkerTypes": ["general-purpose", *available],
                "message": "Unknown Agent preset. Omit workerType or use general-purpose when no preset is needed.",
            })
        tool_resolution = self._resolve_requested_agent_tools(args, agent)
        if tool_resolution.get("error"):
            return _json({"ok": False, **tool_resolution})
        agent = replace(agent, tool_allowlist=tool_resolution["tools"])
        description = str(args.get("description") or args.get("title") or "").strip()
        title = (description or f"{agent.name}: {prompt[:80]}").strip()[:120]
        output = await self._run_one(
            agent,
            instruction=prompt,
            title=title,
            source="openbear_agent_tool",
            progress_tool_name="Agent",
            inherit_from_task_uuid=inherit_ref,
            inherited_plan_context=inherited_context,
            plan_mode=plan_mode,
        )
        return _json(output)

    async def agent_message(self, args: dict[str, Any]) -> str:
        """Send a structured, auditable intervention to an existing Agent task."""
        if current_tool_context().source.startswith("agent:"):
            return _json({"ok": False, "error": "agent_context_not_allowed"})
        message = str(args.get("message") or args.get("prompt") or args.get("guidance") or "").strip()
        if not message:
            return _json({"ok": False, "error": "message_required"})
        reason_code = str(args.get("reasonCode") or "").strip()
        allowed_reason_codes = {
            "user_instruction", "safety_risk", "scope_drift", "blocked",
            "repeated_no_progress", "evidence_sufficient", "criterion_gap", "plan_consistency",
        }
        if reason_code not in allowed_reason_codes:
            return _json({
                "ok": False,
                "error": "intervention_reason_required",
                "message": "AgentMessage requires a supported reasonCode.",
                "allowedReasonCodes": sorted(allowed_reason_codes),
            })
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return _json({"ok": False, "error": "intervention_reason_required", "message": "reason is required"})
        evidence = [str(item).strip() for item in (args.get("evidence") or []) if str(item).strip()]
        criterion_ids = [str(item).strip() for item in (args.get("criterionIds") or []) if str(item).strip()]
        metadata = {
            "reasonCode": reason_code,
            "reason": reason[:8000],
            "evidence": evidence[:100],
            "criterionIds": criterion_ids[:100],
            "expectedPlanVersion": int(args.get("expectedPlanVersion") or 0),
        }
        task = await self._resolve_scoped_task(args.get("to") or args.get("taskUuid") or args.get("task_uuid"))
        if task is None:
            return _json({
                "ok": False,
                "error": "agent_task_not_found",
                "message": "No scoped Agent task matched the target. Use the task id returned by Agent.",
            })
        if task.status in TERMINAL_TASK_STATUSES:
            # A delayed controller message must not resurrect completed work as
            # a hidden follow-up task. Starting more work requires a fresh,
            # explicit Agent call with a complete prompt and tool allowlist.
            return _json({
                "ok": True,
                "status": task.status,
                "terminal": True,
                "alreadyTerminal": True,
                "taskUuid": task.task_uuid,
                "task": _task_public(task, include_output=True),
                "message": "目标 Agent 已终止；未创建 follow-up。需要追加任务时请显式调用 Agent。",
            })
        try:
            queued = await self.plan.queue_intervention(
                task.task_uuid,
                message=message,
                requested_by="AgentMessage",
                metadata=metadata,
                expected_plan_version=metadata["expectedPlanVersion"],
            )
        except PlanError as exc:
            error = exc.public()
            error.setdefault("status", str(error.get("taskStatus") or task.status))
            error["task"] = _task_public(task, include_output=task.status == "needs_openbear_control")
            return _json(error)
        control_uuid = str(queued["controlUuid"])
        if str(queued["taskStatus"]) == "needs_openbear_control":
            return await self.continue_task({
                "taskUuid": task.task_uuid,
                "guidance": "",
                "_controlUuid": control_uuid,
                "_progressToolName": "AgentMessage",
            })
        return await self.steer_task({
            "taskUuid": task.task_uuid,
            "message": message,
            "metadata": metadata,
            "_controlUuid": control_uuid,
        })

    async def agent_stop(self, args: dict[str, Any]) -> str:
        if current_tool_context().source.startswith("agent:"):
            return _json({"ok": False, "error": "agent_context_not_allowed"})
        task = await self._resolve_scoped_task(args.get("to") or args.get("taskUuid") or args.get("task_uuid"))
        if task is None:
            return _json({
                "ok": False,
                "error": "agent_task_not_found",
                "message": "No scoped Agent task matched the target. Use the task id returned by Agent.",
            })
        reason = str(args.get("reason") or "AgentStop requested").strip()
        return await self.stop_task({"taskUuid": task.task_uuid, "reason": reason})

    async def agent_wait(self, args: dict[str, Any]) -> str:
        """Suspend the main controller until one aggregate Agent review boundary."""
        ctx = current_tool_context()
        if ctx.source.startswith("agent:"):
            return _json({"ok": False, "error": "agent_context_not_allowed"})
        if ctx.agent_wait is None:
            return _json({
                "ok": False,
                "error": "agent_wait_not_supported",
                "message": "AgentWait is available only in a controller runtime that owns Agent wake events.",
            })
        mode = str(args.get("mode") or "").strip().lower()
        if mode not in {"event_only", "review_after"}:
            return _json({
                "ok": False,
                "error": "invalid_agent_wait_mode",
                "message": "mode must be event_only or review_after.",
            })
        reason = str(args.get("reason") or "").strip()
        try:
            raw_delay = float(args.get("reviewAfterSeconds") or 0)
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "invalid_review_delay"})
        # This is only a safety rail against accidental busy loops or effectively
        # immortal timers. It is not a default review cadence: the model chooses.
        if mode == "event_only":
            review_after_s = 0.0
        else:
            if raw_delay <= 0:
                return _json({
                    "ok": False,
                    "error": "review_delay_required",
                    "message": "reviewAfterSeconds must be positive in review_after mode.",
                })
            review_after_s = min(86_400.0, max(10.0, raw_delay))
        return await ctx.agent_wait({
            "mode": mode,
            "reviewAfterSeconds": review_after_s,
            "reason": reason,
        })

    async def continue_task(self, args: dict[str, Any]) -> str:
        ctx = current_tool_context()
        if ctx.source.startswith("agent:"):
            return "error: 子 Agent 不能续跑 Rath task；必须交给 OpenBear 总负责人裁决。"
        task_uuid = str(args.get("taskUuid") or args.get("task_uuid") or "").strip()
        if not task_uuid:
            return "error: 缺少 taskUuid。"
        task = await self.dao.get_task(task_uuid)
        if task is None:
            return "error: 未找到 Agent 任务。"
        scope_error = self._task_scope_error(task, ctx)
        if scope_error:
            return scope_error
        if task.status != "needs_openbear_control":
            return f"error: 该 Agent task 当前状态为 {task.status}，不处于等待 OpenBear 裁决状态。"
        agent = await self._agent_from_task(task)
        if agent is None:
            return "error: 无法从 task 恢复 Agent 定义，不能安全续跑。"
        guidance = str(args.get("guidance") or args.get("instruction") or "").strip()
        progress_tool_name = str(args.get("_progressToolName") or "AgentMessage").strip() or "AgentMessage"
        progress_cb = getattr(ctx, "progress_update", None)
        progress_payload_cb = getattr(ctx, "progress_update_payload", None)
        notification_cb = getattr(ctx, "task_notification", None)
        conversation_event_cb = getattr(ctx, "conversation_event", None)
        progress_started = asyncio.get_running_loop().time()
        detached = False
        last_progress_signature = ""
        latest_ledger_usage: dict[str, Any] | None = None

        async def _emit_progress() -> str:
            nonlocal last_progress_signature
            if progress_cb is None and progress_payload_cb is None:
                return ""
            task_row = await self.dao.get_task(task_uuid)
            if task_row is None:
                return ""
            events = await self.dao.events(task_uuid, limit=12)
            refreshed_session = await self.dao.agent_session(task_row.agent_session_uuid or task.agent_session_uuid)
            task_payload = _task_public(task_row, model=model_name, context_window=context_window)
            task_payload["title"] = task_row.title
            task_payload["detached"] = detached
            task_payload["agentSession"] = (
                self._agent_session_public(refreshed_session)
                if refreshed_session is not None
                else {"title": agent.name, "agentKey": agent.agent_key}
            )
            event_payload = _events_public(events)
            duration_ms = int((asyncio.get_running_loop().time() - progress_started) * 1000)
            result_payload = task_row.output if str(task_row.status or "") in _NOTIFIABLE_AGENT_STATUSES and isinstance(task_row.output, dict) else None
            if result_payload is not None:
                task_payload["output"] = result_payload
            if progress_payload_cb is not None:
                agent_session_payload = task_payload.get("agentSession") or {"title": agent.name, "agentKey": agent.agent_key}
                signature = _agent_progress_signature(
                    task_payload, event_payload, result_payload, agent_session_payload, latest_ledger_usage
                )
                if signature != last_progress_signature:
                    last_progress_signature = signature
                    await progress_payload_cb({
                        "toolName": progress_tool_name,
                        "status": task_payload.get("status") or "running",
                        "detached": detached,
                        "agentSession": agent_session_payload,
                        "task": task_payload,
                        **({"ledgerUsage": latest_ledger_usage} if latest_ledger_usage is not None else {}),
                        **({"result": result_payload} if result_payload is not None else {}),
                        "recentEvents": event_payload,
                        "durationMs": duration_ms,
                    })
            if progress_cb is not None and not (detached and progress_payload_cb is not None):
                line = format_agent_task_progress_card(
                    progress_tool_name,
                    {
                        "taskUuid": task_uuid,
                        "guidance": guidance,
                        "title": task_row.title,
                        "agent": agent.name,
                    },
                    task_payload,
                    events=event_payload,
                    duration_ms=duration_ms,
                )
                await progress_cb(line)
            return str(task_row.status or "")

        progress_stop_event = asyncio.Event()

        async def _emit_progress_and_maybe_stop() -> str:
            emitted_status = await _emit_progress()
            if emitted_status in _NOTIFIABLE_AGENT_STATUSES:
                progress_stop_event.set()
            return emitted_status

        async def _progress_loop() -> None:
            while not progress_stop_event.is_set():
                try:
                    await asyncio.wait_for(progress_stop_event.wait(), timeout=_DETACHED_PROGRESS_POLL_INTERVAL_S)
                    return
                except TimeoutError:
                    pass
                emitted_status = await _emit_progress_and_maybe_stop()
                if emitted_status in _NOTIFIABLE_AGENT_STATUSES:
                    return

        async def _on_runner_event(**kwargs: Any) -> None:
            try:
                await _emit_progress_and_maybe_stop()
            except Exception:
                pass

        parent_conversation_uuid = task.parent_session_uuid
        if str(parent_conversation_uuid or "").startswith("chat:"):
            with contextlib.suppress(Exception):
                conv = await self._conversation_for_chat_id(int(task.chat_id or 0))
                parent_conversation_uuid = str((conv or {}).get("conversation_uuid") or parent_conversation_uuid)
        snapshot = task.input.get("agentSnapshot") if isinstance(task.input, dict) else {}
        runtime = await self._resolve_agent_runtime(
            agent,
            chat_id=int(task.chat_id or 0),
            frozen=snapshot if isinstance(snapshot, dict) else None,
        )
        model_name = str(runtime.get("model") or self.model_selection.current or self.config.models.primary)
        think_level = str(runtime.get("thinkLevel") or "off")
        service_tier = str(runtime.get("serviceTier") or "")
        fast_request = dict(runtime.get("fastRequest") or {})
        cost = dict(runtime.get("cost") or {})
        base_cost = dict(runtime.get("baseCost") or {})
        fast_cost = dict(runtime.get("fastCost") or {})
        fast_requested = bool(runtime.get("fastMode"))
        try:
            context_window = int(self.llm_factory.context_window(model_name) or 0)
        except Exception:
            context_window = 0
        backend, model_id, max_tokens = self.llm_factory.backend_for(model_name)
        agent_base_system_prompt = await render_agent_base_system_prompt(
            self.dao.db,
            identity=str(getattr(self.config.memory, "identity", "openbear") or "openbear"),
            registry=self.registry,
            tool_allowlist=agent.tool_allowlist,
            model_name=model_name,
            workspace_dir=self.workspace_dir,
        )

        async def _on_agent_model_call(detail: dict[str, Any]) -> None:
            nonlocal latest_ledger_usage
            latest_ledger_usage = await self._persist_agent_model_call(
                chat_id=int(task.chat_id or 0),
                session_uuid=task.parent_session_uuid,
                model_label=model_name,
                protocol=str(getattr(backend, "protocol", "") or ""),
                detail=detail,
            )
            await _emit_progress()

        runner = SingleAgentWorkflowRunner(
            self.dao,
            task_uuid,
            agent=agent,
            backend=backend,
            model=model_id,
            max_tokens=max_tokens,
            tools=self.registry,
            model_label=model_name,
            think_level=think_level,
            service_tier=service_tier,
            fast_request=fast_request,
            session_id=safe_agent_llm_session_id(task.agent_session_uuid, task_uuid, agent.agent_key),
            openbear_session_uuid=task.parent_session_uuid,
            agent_session_uuid=task.agent_session_uuid,
            caller_agent_session_uuid=task.caller_agent_session_uuid,
            cost=cost,
            base_cost=base_cost,
            fast_cost=fast_cost,
            fast_requested=fast_requested,
            base_system_prompt=agent_base_system_prompt,
            tool_result_max_chars=max_tool_result_chars(
                self.llm_factory.context_window(model_name),
                self.config.tools.tool_result_max_chars,
            ),
            **self._model_retry_kwargs(task_uuid),
            model_call_limit=self._model_call_limit_for(agent),
            tool_call_limit=self._tool_call_limit_for(agent),
            plan_control_call_limit=int(getattr(self.config.rath, "plan_control_call_limit", 200) or 200),
            poll_interval_s=0.5,
            on_model_call=_on_agent_model_call,
            on_event=_on_runner_event,
            task_notification=notification_cb,
            conversation_event=conversation_event_cb,
            plan_protocol_enabled=(
                bool(getattr(self.config.rath, "agent_plan_enabled", True))
                and _task_agent_plan_mode(task) == _AGENT_PLAN_MODE_MANAGED
            ),
            plan_prompts=self._plan_prompts(),
            **self._context_compact_kwargs(model_name),
        )
        progress_task: asyncio.Task | None = None
        if progress_cb is not None or progress_payload_cb is not None:
            await _emit_progress_and_maybe_stop()
            progress_task = asyncio.create_task(_progress_loop())
        async def _run_registered_continue() -> dict[str, Any]:
            async with self.manager.execution_slot(task_uuid):
                return await runner.run_continue(guidance)

        claimed = await self.dao.update_task(
            task_uuid,
            status="resuming",
            control_state="continuation_claimed",
            current_status="已取得续跑权，准备继续执行",
            expected_statuses=("needs_openbear_control",),
        )
        if not claimed:
            refreshed = await self.dao.get_task(task_uuid)
            return _json({
                "ok": False,
                "continued": False,
                "error": "agent_task_continuation_already_claimed",
                "status": str(refreshed.status if refreshed is not None else "unknown"),
                "taskUuid": task_uuid,
                "message": "该 Agent task 已被另一个 AgentMessage 取得续跑权。",
                "task": _task_public(refreshed, model=model_name, context_window=context_window),
            })
        child_task = asyncio.create_task(_run_registered_continue(), name=f"agent-continue-{task_uuid[:8]}")
        try:
            self.manager.register(task_uuid, int(task.chat_id or 0), child_task, occupies_chat=False)
        except Exception:
            child_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await child_task
            await self.dao.update_task(
                task_uuid,
                status="needs_openbear_control",
                control_state="waiting_openbear_control",
                current_status="续跑注册失败，等待 OpenBear 裁决",
                expected_statuses=("resuming",),
            )
            raise
        try:
            wait_s = self._foreground_wait_s_for_context(ctx, allow_detach=True)
            done, _pending = await asyncio.wait({child_task}, timeout=wait_s)
            if child_task not in done:
                detached = True
                self._watch_detached_agent_task(task_uuid, child_task, continue_task=True, notification_cb=notification_cb)
                with contextlib.suppress(Exception):
                    await _emit_progress()
                refreshed = await self.dao.get_task(task_uuid)
                session = await self.dao.agent_session(task.agent_session_uuid)
                task_payload = _task_public(refreshed, model=model_name, context_window=context_window)
                task_payload["detached"] = True
                return _json({
                    "ok": True,
                    "continued": True,
                    "status": "running",
                    "detached": True,
                    "taskUuid": task_uuid,
                    "message": f"{progress_tool_name} is running in background; do not poll repeatedly in this turn.",
                    "next": "Wait for the task-notification or send AgentMessage only if the Agent needs specific guidance.",
                    "agentSession": self._agent_session_public(session) if session else {},
                    "task": task_payload,
                    "recentEvents": _events_public(await self.dao.events(task_uuid, limit=8)),
                })
            try:
                output = child_task.result()
            except asyncio.CancelledError:
                refreshed = await self.manager.mark_cancelled(task_uuid)
                with contextlib.suppress(Exception):
                    await _emit_progress()
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                session = await self.dao.agent_session(task.agent_session_uuid)
                return _json({
                    "ok": False,
                    "continued": False,
                    "status": "cancelled",
                    "reason": "agent_task_continue_cancelled",
                    "message": "AgentMessage 续跑已取消。",
                    "agentSession": self._agent_session_public(session) if session else {},
                    "task": _task_public(refreshed, model=model_name, context_window=context_window),
                    "recentEvents": _events_public(await self.dao.events(task_uuid, limit=8)),
                })
            except Exception as exc:
                restored = await self.dao.update_task(
                    task_uuid,
                    status="needs_openbear_control",
                    control_state="waiting_openbear_control",
                    current_status="续跑失败，等待 OpenBear 裁决",
                    error=f"{type(exc).__name__}: {exc}",
                    output={
                        "status": "needs_openbear_control",
                        "reason": "agent_task_continue_failed",
                        "message": f"AgentMessage 续跑失败：{type(exc).__name__}: {exc}",
                        "taskUuid": task_uuid,
                    },
                    finish=True,
                    expected_statuses=("running", "resuming"),
                )
                if restored:
                    await self.dao.append_event(
                    task_uuid,
                    "agent_continue_failed",
                    summary=f"AgentMessage 续跑失败：{type(exc).__name__}",
                    detail={"error": str(exc)[:2000]},
                    )
                with contextlib.suppress(Exception):
                    await _emit_progress()
                refreshed = await self.dao.get_task(task_uuid)
                session = await self.dao.agent_session(task.agent_session_uuid)
                return _json({
                    "ok": False,
                    "continued": False,
                    "status": "needs_openbear_control",
                    "reason": "agent_task_continue_failed",
                    "message": f"AgentMessage 续跑失败：{type(exc).__name__}: {exc}",
                    "agentSession": self._agent_session_public(session) if session else {},
                    "task": _task_public(refreshed, model=model_name, context_window=context_window),
                    "recentEvents": _events_public(await self.dao.events(task_uuid, limit=8)),
                })
        except asyncio.CancelledError:
            if not detached and child_task is not None and not child_task.done():
                child_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await child_task
                with contextlib.suppress(Exception):
                    await self.manager.mark_cancelled(task_uuid)
            raise
        finally:
            if progress_task is not None and not detached:
                progress_stop_event.set()
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await progress_task
        with contextlib.suppress(Exception):
            await _emit_progress()
        refreshed = await self.dao.get_task(task_uuid)
        result = await self._prepare_agent_result(output, task=refreshed)
        session = await self.dao.agent_session(task.agent_session_uuid)
        status = refreshed.status if refreshed is not None else "completed"
        return _json({
            "ok": status == "completed",
            "continued": True,
            "status": status,
            "agentSession": self._agent_session_public(session) if session else {},
            "task": _task_public(refreshed, model=model_name, context_window=context_window),
            "result": result,
            "resultOutputTokens": _agent_result_output_tokens(refreshed, result) if status == "completed" else 0,
            "resultCount": 1 if status == "completed" else 0,
            "recentEvents": _events_public(await self.dao.events(task_uuid, limit=8)),
        })

    async def stop_task(self, args: dict[str, Any]) -> str:
        ctx = current_tool_context()
        if ctx.source.startswith("agent:"):
            return _json({"ok": False, "error": "agent_context_not_allowed", "message": "子 Agent 不能停止 Rath Agent task；必须交给 OpenBear 总负责人裁决。"})
        task_uuid = str(args.get("taskUuid") or args.get("task_uuid") or "").strip()
        reason = str(args.get("reason") or "AgentStop requested").strip()
        if not task_uuid:
            return _json({"ok": False, "error": "task_uuid_required"})
        task = await self.dao.get_task(task_uuid)
        if task is None:
            return _json({"ok": False, "error": "task_not_found", "taskUuid": task_uuid})
        scope_error = self._task_scope_error(task, ctx)
        if scope_error:
            return _json({"ok": False, "error": "task_out_of_scope", "taskUuid": task_uuid, "message": scope_error})
        if task.status in TERMINAL_TASK_STATUSES:
            return _json({
                "ok": True,
                "stopped": False,
                "alreadyTerminal": True,
                "taskUuid": task_uuid,
                "status": task.status,
                "currentStatus": task.current_status or _task_status_label(task.status),
                "reason": reason,
                "task": _task_public(task, include_output=False),
            })
        control_uuid = await self.manager.stop(task_uuid, requested_by="AgentStop", message=reason)
        refreshed = await self.dao.get_task(task_uuid)
        return _json({
            "ok": True,
            "stopped": True,
            "taskUuid": task_uuid,
            "controlUuid": control_uuid,
            "status": str((refreshed or task).status or ""),
            "currentStatus": str((refreshed or task).current_status or _task_status_label(str((refreshed or task).status or ""))),
            "reason": reason,
            "task": _task_public(refreshed or task, include_output=False),
        })

    async def steer_task(self, args: dict[str, Any]) -> str:
        ctx = current_tool_context()
        if ctx.source.startswith("agent:"):
            return _json({"ok": False, "error": "agent_context_not_allowed", "message": "子 Agent 不能直接插话 Rath Agent task；必须交给 OpenBear 总负责人裁决。"})
        task_uuid = str(args.get("taskUuid") or args.get("task_uuid") or "").strip()
        message = str(args.get("message") or args.get("text") or "").strip()
        if not task_uuid:
            return _json({"ok": False, "error": "task_uuid_required"})
        if not message:
            return _json({"ok": False, "error": "message_required"})
        task = await self.dao.get_task(task_uuid)
        if task is None:
            return _json({"ok": False, "error": "task_not_found", "taskUuid": task_uuid})
        scope_error = self._task_scope_error(task, ctx)
        if scope_error:
            return _json({"ok": False, "error": "task_out_of_scope", "taskUuid": task_uuid, "message": scope_error})
        precreated_control_uuid = str(args.get("_controlUuid") or "").strip()
        if not precreated_control_uuid and task.status not in _ACTIVE_TASK_STATUSES:
            return _json({
                "ok": False,
                "error": "task_not_active",
                "taskUuid": task_uuid,
                "status": task.status,
                "task": _task_public(task, include_output=False),
            })
        metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
        control_uuid = await self.manager.steer(
            task_uuid,
            message,
            requested_by="AgentMessage",
            metadata=metadata,
            control_uuid=precreated_control_uuid or None,
        )
        refreshed = await self.dao.get_task(task_uuid)
        return _json({
            "ok": True,
            "steered": True,
            "taskUuid": task_uuid,
            "controlUuid": control_uuid,
            "status": str((refreshed or task).status or ""),
            "currentStatus": str((refreshed or task).current_status or ""),
            "message": "干预已记录；Agent 将在下一个 cooperative checkpoint 先给出结构化回执。",
            "intervention": metadata,
            "task": _task_public(refreshed or task, include_output=False),
        })

    def _task_scope_error(self, task: RathTask, ctx: ToolRuntimeContext) -> str:
        if ctx.chat_id <= 0 and not ctx.session_uuid:
            return "error: 缺少运行时会话范围，拒绝读取/控制 Rath 任务。"
        if ctx.chat_id > 0 and int(task.chat_id or 0) != int(ctx.chat_id):
            return "error: Rath 任务不属于当前会话，已拒绝访问。"
        if ctx.session_uuid and task.parent_session_uuid and task.parent_session_uuid != ctx.session_uuid:
            return "error: Rath 任务不属于当前 OpenBear Session，已拒绝访问。"
        return ""

    async def _resolve_agent(self, ref: Any) -> RathAgentDef | None:
        agents = await self.dao.list_agents(include_disabled=False)
        if ref is None:
            return None
        text = str(ref).strip()
        if not text:
            return None
        if text.isdigit():
            return await self.dao.agent_by_id(int(text), include_disabled=False)
        low = text.lower()
        for agent in agents:
            if agent.agent_key.lower() == low or agent.name.lower() == low:
                return agent
        for agent in agents:
            if low in agent.name.lower() or low in agent.agent_key.lower():
                return agent
        return None

    async def _resolve_agent_preset(self, ref: Any) -> RathAgentDef | None:
        text = str(ref or "").strip()
        if not text or text.lower() in {"general-purpose", "general", "default"}:
            return await self._default_general_agent()
        return await self._resolve_agent(text)

    async def _default_general_agent(self) -> RathAgentDef:
        workflow_uuid = await self._default_workflow_uuid()
        return RathAgentDef(
            id=0,
            agent_key="general-purpose",
            name="general-purpose",
            description=(
                "General-purpose Agent for research, codebase reading, implementation support, "
                "testing, and other focused delegated tasks."
            ),
            system_prompt=(
                "You are a focused general-purpose subagent running under OpenBear's main controller.\n"
                "Work only on the task in the user message. Use the available tools to gather evidence, "
                "make necessary edits when the task explicitly calls for implementation, and stop when "
                "you have enough information for the delegated objective.\n\n"
                "Return a concise handoff for OpenBear, including: conclusion, actions taken, evidence "
                "(files, commands, tests, sources), remaining risks, and any recommended next step. "
                "Do not address the end user directly unless the task asks for user-facing wording."
            ),
            model="",
            think_level="",
            tool_allowlist=[],
            enabled=True,
            workflow_uuid=workflow_uuid,
        )

    async def _resolve_scoped_task(self, ref: Any) -> RathTask | None:
        ctx = current_tool_context()
        text = str(ref or "").strip()
        candidates: list[RathTask] = []
        if text:
            exact = await self.dao.get_task(text)
            if exact is not None and not self._task_scope_error(exact, ctx):
                return exact
        if ctx.chat_id > 0:
            candidates = await self.dao.list_tasks(chat_id=ctx.chat_id, limit=100)
        else:
            candidates = await self.dao.list_tasks(limit=100)
        if ctx.session_uuid:
            candidates = [task for task in candidates if task.parent_session_uuid == ctx.session_uuid]
        candidates = [task for task in candidates if not self._task_scope_error(task, ctx)]
        if text:
            lowered = text.lower()
            for task in candidates:
                if task.task_uuid.lower().startswith(lowered):
                    return task
                task_short_id = task.task_uuid[:8].lower()
                if task_short_id == lowered:
                    return task
                title = str(task.title or "").lower()
                agent_key = str(task.current_agent_key or "").lower()
                if lowered and (lowered in title or lowered == agent_key):
                    return task
            return None
        active = [
            task for task in candidates
            if str(task.status or "") in (_ACTIVE_TASK_STATUSES | {"needs_openbear_control"})
        ]
        return active[0] if len(active) == 1 else None

    async def _inherit_plan_context(self, source_task_uuid: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        ctx = current_tool_context()
        source = await self.dao.get_task(source_task_uuid)
        if source is None or self._task_scope_error(source, ctx):
            return {}, {
                "error": "inherit_task_not_found",
                "message": "The inheritance source is not accessible in this conversation.",
            }
        if str(source.status or "") in _ACTIVE_TASK_STATUSES or str(source.status or "") == "needs_openbear_control":
            return {}, {
                "error": "inherit_task_still_active",
                "taskUuid": source.task_uuid,
                "status": str(source.status or ""),
                "message": "Stop or wait for the source Agent task before inheriting its durable facts.",
            }
        try:
            snapshot = await self.plan.snapshot(source.task_uuid)
        except Exception as exc:
            return {}, {
                "error": "inherit_plan_unavailable",
                "taskUuid": source.task_uuid,
                "message": f"Could not read the source Plan facts: {type(exc).__name__}",
            }
        state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
        visible_version = int(state.get("pending_plan_version") or state.get("active_plan_version") or 0)
        visible_plan = next(
            (
                item for item in reversed(snapshot.get("versions") or [])
                if int(item.get("version") or 0) == visible_version
            ),
            None,
        )
        completed_steps = [
            {
                "planVersion": int(item.get("plan_version") or 0),
                "stepId": str(item.get("step_id") or ""),
                "result": str(item.get("result") or "")[:8000],
                "criteriaState": item.get("criteria_state") or {},
            }
            for item in snapshot.get("steps") or []
            if str(item.get("status") or "") == "completed"
        ]
        evidence = [
            {
                "evidenceUuid": str(item.get("evidence_uuid") or ""),
                "planVersion": int(item.get("plan_version") or 0),
                "stepId": str(item.get("step_id") or ""),
                "criterionId": str(item.get("criterion_id") or ""),
                "type": str(item.get("evidence_type") or ""),
                "reference": str(item.get("reference") or "")[:4000],
                "summary": str(item.get("summary") or "")[:4000],
                "sourceTaskUuid": source.task_uuid,
            }
            for item in (snapshot.get("evidence") or [])[-500:]
        ]
        return {
            "sourceTask": {
                "taskUuid": source.task_uuid,
                "title": str(source.title or ""),
                "status": str(source.status or ""),
                "currentStatus": str(source.current_status or ""),
            },
            "sourcePlanState": {
                "phase": str(state.get("phase") or ""),
                "activePlanVersion": int(state.get("active_plan_version") or 0),
                "pendingPlanVersion": int(state.get("pending_plan_version") or 0),
                "currentStepId": str(state.get("current_step_id") or ""),
                "finalOutputsState": state.get("final_outputs_state") or {},
            },
            "sourcePlan": visible_plan or {},
            "completedSteps": completed_steps,
            "evidence": evidence,
            "instruction": (
                "Treat these as durable facts and references only. Do not resume the old coroutine or transcript. "
                "Submit a new complete Plan for all remaining work and obtain fresh controller approval."
            ),
        }, None

    async def _agent_from_task(self, task: RathTask) -> RathAgentDef | None:
        snapshot = task.input.get("agentSnapshot") if isinstance(task.input, dict) else {}
        workflow_uuid = str(task.workflow_uuid or "").strip()
        if isinstance(snapshot, dict):
            agent_key = str(snapshot.get("agentKey") or "").strip()
            if agent_key:
                return RathAgentDef(
                    id=int(snapshot.get("id") or 0),
                    agent_key=agent_key,
                    name=str(snapshot.get("name") or agent_key),
                    description=str(snapshot.get("description") or ""),
                    system_prompt=str(snapshot.get("systemPrompt") or ""),
                    model=str(snapshot.get("model") or ""),
                    think_level=str(snapshot.get("thinkLevel") or ""),
                    tool_allowlist=sanitize_tool_allowlist(snapshot.get("toolAllowlist") or []),
                    enabled=bool(snapshot.get("enabled", True)),
                    workflow_uuid=workflow_uuid,
                )
        agent_key = str(task.current_agent_key or "").strip()
        if not agent_key:
            return None
        agent = await self.dao.agent_by_key(agent_key, include_disabled=True)
        if agent is not None:
            agent.workflow_uuid = workflow_uuid
        return agent

    async def _default_workflow_uuid(self) -> str:
        wf = await self.dao.workflow_by_slug(SINGLE_AGENT_WORKFLOW_SLUG)
        if wf is not None:
            return wf.workflow_uuid
        await ensure_builtin_workflows(self.dao)
        wf = await self.dao.workflow_by_slug(SINGLE_AGENT_WORKFLOW_SLUG)
        if wf is None:
            raise RuntimeError("single-agent workflow route missing")
        return wf.workflow_uuid

    def _agent_limit_for(self, config_attr: str, default: int) -> int:
        return max(0, int(getattr(self.config.rath, config_attr, default) or 0))

    def _model_call_limit_for(self, agent: RathAgentDef) -> int:
        return self._agent_limit_for("agent_model_call_limit", 40)

    def _tool_call_limit_for(self, agent: RathAgentDef) -> int:
        return self._agent_limit_for("agent_tool_call_limit", 80)

    def _model_retry_kwargs(self, task_uuid: str) -> dict[str, Any]:
        agent_cfg = getattr(self.config, "agent", None)
        return {
            "max_retries": int(getattr(agent_cfg, "max_retries", 10) or 0),
            "retry_backoff_s": float(getattr(agent_cfg, "retry_backoff_s", 0.5) or 0.0),
            "retry_max_delay_s": float(getattr(agent_cfg, "retry_max_delay_s", 32.0) or 0.0),
            "retry_jitter_ratio": float(getattr(agent_cfg, "retry_jitter_ratio", 0.25) or 0.0),
            "retry_cancel_check": lambda: self.manager.consume_retry_cancel(task_uuid),
        }

    def _model_compact_trigger_tokens(self, model_name: str) -> int:
        resolved = self.config.models.resolve(model_name)
        if not resolved:
            return 0
        return max(0, int(getattr(resolved[1], "compact_trigger_tokens", 0) or 0))

    def _compression_candidates_for(self, model_name: str) -> list[tuple[Any, str, str, str]]:
        models_cfg = getattr(self.config, "models", None)
        configured_labels = list(getattr(models_cfg, "compression_models", []) or [])
        if hasattr(models_cfg, "compression_model_candidates"):
            labels = models_cfg.compression_model_candidates(model_name)
        else:
            labels = [*list(getattr(models_cfg, "compression_models", []) or []), model_name]
        candidates: list[tuple[Any, str, str, str]] = []
        seen: set[str] = set()
        for label in labels:
            label = str(label or "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            source = "primary-fallback" if configured_labels and label == model_name and label not in configured_labels else "compression"
            try:
                backend, model_id, _ = self.llm_factory.backend_for(label)
            except Exception:
                continue
            candidates.append((backend, model_id, source, label))
        primary_already_configured = any(label == model_name for _backend, _model, _source, label in candidates)
        if not primary_already_configured:
            try:
                backend, model_id, _ = self.llm_factory.backend_for(model_name)
                candidates.append((backend, model_id, "primary-fallback", model_name))
            except Exception:
                pass
        return candidates

    def _context_compact_kwargs(self, model_name: str) -> dict[str, Any]:
        agent_cfg = getattr(self.config, "agent", None)
        kwargs: dict[str, Any] = {
            "context_window": int(self.llm_factory.context_window(model_name) or 0),
            "context_compact_trigger_tokens": self._model_compact_trigger_tokens(model_name),
            "context_compact_ratio": float(getattr(agent_cfg, "compact_ratio", 0.7) or 0.7),
            "context_compact_keep_recent": int(getattr(agent_cfg, "keep_recent_messages", 8) or 8),
            "context_compact_prompt": str(getattr(agent_cfg, "compact_prompt", "") or ""),
            "context_compact_max_tokens": int(getattr(agent_cfg, "compact_max_tokens", 4096) or 4096),
            "context_compact_max_retries": int(getattr(agent_cfg, "compact_max_retries", 1) or 1),
            "context_compact_timeout_s": float(getattr(agent_cfg, "compact_timeout_s", 1800.0) or 1800.0),
        }
        candidates = self._compression_candidates_for(model_name)
        if not candidates:
            return kwargs
        compact_costs: dict[str, dict[str, float]] = {}
        for _backend, _model_id, _source, candidate_label in candidates:
            model_meta = self.config.models.resolve(candidate_label)
            if model_meta:
                compact_costs[candidate_label] = model_meta[1].cost
        kwargs["context_compact_costs"] = compact_costs
        backend, model_id, source, label = candidates[0]
        kwargs["context_compact_backend"] = backend
        kwargs["context_compact_model"] = model_id
        kwargs["context_compact_source"] = source
        kwargs["context_compact_label"] = label
        extra = [
            CompressionCandidate(candidate_backend, candidate_model, candidate_source, candidate_label)
            for candidate_backend, candidate_model, candidate_source, candidate_label in candidates[1:]
        ]
        if extra:
            kwargs["context_compact_extra_candidates"] = extra
        for candidate_backend, candidate_model, candidate_source, _candidate_label in candidates[1:]:
            if candidate_source == "primary-fallback":
                kwargs["context_compact_fallback_backend"] = candidate_backend
                kwargs["context_compact_fallback_model"] = candidate_model
                break
        return kwargs

    async def _agent_session_for(
        self,
        agent: RathAgentDef,
        *,
        chat_id: int,
        openbear_session_uuid: str,
    ) -> RathAgentSession:
        workflow_uuid = agent.workflow_uuid or await self._default_workflow_uuid()
        session_key = openbear_session_uuid or f"chat:{chat_id}"
        return await self.dao.get_or_create_agent_session(
            openbear_session_uuid=session_key,
            chat_id=chat_id,
            workflow_uuid=workflow_uuid,
            agent_key=agent.agent_key,
            title=agent.name,
            metadata={"agentName": agent.name},
        )

    async def _mark_task_failed(self, task_uuid: str, exc: Exception, *, current_status: str = "任务失败") -> RathTask | None:
        changed = await self.dao.update_task(
            task_uuid,
            status="failed",
            control_state="",
            current_status=current_status,
            error=f"{type(exc).__name__}: {exc}",
            finish=True,
            expected_statuses=tuple(_ACTIVE_TASK_STATUSES),
        )
        if changed:
            await self.dao.append_event(
                task_uuid,
                "task_failed",
                summary=f"{current_status}：{type(exc).__name__}: {str(exc)[:200]}",
            )
        return await self.dao.get_task(task_uuid)

    def _agent_tool_foreground_wait_s(self) -> float:
        try:
            return max(0.0, float(getattr(self.config.rath, "agent_tool_foreground_wait_s", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _foreground_wait_s_for_context(self, ctx: ToolRuntimeContext, *, allow_detach: bool = True) -> float | None:
        if not allow_detach:
            return None
        # Claude Code assistant/KAIROS path forces AgentTool async because a
        # synchronous subagent keeps the main loop's turn open and blocks the
        # daemon/web input queue.  OpenBear Web installs task_notification, so
        # it gets the same async-from-start behavior regardless of legacy config.
        if getattr(ctx, "task_notification", None) is not None:
            return 0.0
        return self._agent_tool_foreground_wait_s()

    @staticmethod
    def _render_agent_task_notification(payload: dict[str, Any]) -> str:
        return _render_agent_task_notification(payload)

    async def _agent_task_notification_payload(
        self,
        task_uuid: str,
        *,
        raw_output: dict[str, Any] | None = None,
        continue_task: bool = False,
    ) -> dict[str, Any]:
        task = await self.dao.get_task(task_uuid)
        status = str(task.status if task is not None else "")
        output = raw_output if isinstance(raw_output, dict) else None
        if output is None and task is not None and isinstance(task.output, dict):
            output = task.output
        result: dict[str, Any] = {}
        if output:
            if status == "completed":
                result = await self._prepare_agent_result(output, task=task)
            else:
                result = dict(output)
        session = await self.dao.agent_session(task.agent_session_uuid) if task is not None and task.agent_session_uuid else None
        task_payload = _task_public(task, include_output=status != "completed")
        if result and status == "completed":
            task_payload["output"] = result
        agent_name = "Agent"
        if session is not None:
            agent_name = session.title or session.agent_key or agent_name
        elif isinstance(output, dict) and isinstance(output.get("agent"), dict):
            agent_name = str(output["agent"].get("name") or output["agent"].get("agentKey") or agent_name)
        summary = f'{agent_name} {"续跑" if continue_task else "任务"}{_task_status_label(status)}'
        if result.get("summary"):
            summary = str(result.get("summary") or "").split("\n", 1)[0][:240] or summary
        payload: dict[str, Any] = {
            "kind": "task-notification",
            "taskUuid": task_uuid,
            "status": status or "unknown",
            "summary": summary,
            "continued": bool(continue_task),
            "agentSession": self._agent_session_public(session) if session is not None else {},
            "task": task_payload,
            "result": result,
            "resultOutputTokens": _agent_result_output_tokens(task, result) if status == "completed" else 0,
            "resultCount": 1 if status == "completed" else 0,
            "recentEvents": _events_public(await self.dao.events(task_uuid, limit=12)),
            "planRuntime": await self._plan_notification_snapshot(task_uuid),
        }
        payload["content"] = self._render_agent_task_notification(payload)
        return payload

    def _watch_detached_agent_task(
        self,
        task_uuid: str,
        child_task: asyncio.Task,
        *,
        continue_task: bool = False,
        notification_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        def _done(t: asyncio.Task) -> None:
            async def _finalize() -> None:
                raw_output: dict[str, Any] | None = None
                should_notify = False
                try:
                    result = t.result()
                    raw_output = result if isinstance(result, dict) else None
                    task = await self.dao.get_task(task_uuid)
                    should_notify = bool(task is not None and str(task.status or "") in _NOTIFIABLE_AGENT_STATUSES)
                except asyncio.CancelledError:
                    await self.manager.mark_cancelled(task_uuid)
                    # User-initiated Web stop must not resurrect the conversation
                    # with a cancellation summary turn.  Claude sends killed
                    # notifications for model-invoked stop_task; OpenBear Web
                    # stop is a stronger user cancellation boundary.
                    should_notify = False
                except Exception as exc:
                    if continue_task:
                        restored = await self.dao.update_task(
                            task_uuid,
                            status="needs_openbear_control",
                            control_state="waiting_openbear_control",
                            current_status="续跑失败，等待 OpenBear 裁决",
                            error=f"{type(exc).__name__}: {exc}",
                            output={
                                "status": "needs_openbear_control",
                                "reason": "agent_task_continue_failed",
                                "message": f"AgentMessage 续跑失败：{type(exc).__name__}: {exc}",
                                "taskUuid": task_uuid,
                            },
                            finish=True,
                            expected_statuses=("running", "resuming"),
                        )
                        if restored:
                            await self.dao.append_event(
                            task_uuid,
                            "agent_continue_failed",
                            summary=f"AgentMessage 续跑失败：{type(exc).__name__}",
                                detail={"error": str(exc)[:2000]},
                            )
                    else:
                        await self._mark_task_failed(task_uuid, exc)
                    should_notify = True
                if should_notify and notification_cb is not None:
                    try:
                        payload = await self._agent_task_notification_payload(
                            task_uuid, raw_output=raw_output, continue_task=continue_task
                        )
                        await notification_cb(payload)
                    except Exception:
                        # Notification delivery is best-effort.  The durable Rath
                        # task/output/artifacts remain available through the Web task UI.
                        pass

            asyncio.create_task(_finalize())

        child_task.add_done_callback(_done)

    async def _run_one(
        self,
        agent: RathAgentDef,
        *,
        instruction: str,
        title: str,
        source: str = "openbear_agent_tool",
        caller_agent_session_uuid: str = "",
        openbear_session_uuid: str = "",
        chat_id: int | None = None,
        progress_tool_name: str = "Agent",
        progress_payload_update: Callable[[dict[str, Any], list[dict[str, Any]], int], Awaitable[None]] | None = None,
        allow_detach: bool = True,
        notify_detached: bool = True,
        inherit_from_task_uuid: str = "",
        inherited_plan_context: dict[str, Any] | None = None,
        plan_mode: str = _AGENT_PLAN_MODE_DIRECT,
    ) -> dict[str, Any]:
        ctx = current_tool_context()
        resolved_chat_id = ctx.chat_id if chat_id is None else int(chat_id or 0)
        resolved_openbear_session_uuid = openbear_session_uuid or ctx.session_uuid or f"chat:{resolved_chat_id}"
        parent_task_uuid = str(getattr(ctx, "task_uuid", "") or "").strip()
        parent_task = await self.dao.get_task(parent_task_uuid) if parent_task_uuid else None
        lineage_turn_uuid = str(
            getattr(ctx, "turn_uuid", "")
            or getattr(parent_task, "turn_uuid", "")
            or ""
        )
        lineage_root_turn_uuid = str(
            getattr(ctx, "run_root_turn_uuid", "")
            or getattr(parent_task, "run_root_turn_uuid", "")
            or lineage_turn_uuid
        )
        lineage_parent_turn_uuid = str(
            getattr(parent_task, "turn_uuid", "")
            or lineage_turn_uuid
        )
        workflow_uuid = agent.workflow_uuid or await self._default_workflow_uuid()
        agent_session = await self._agent_session_for(
            agent,
            chat_id=resolved_chat_id,
            openbear_session_uuid=resolved_openbear_session_uuid,
        )
        runtime = await self._resolve_agent_runtime(agent, chat_id=resolved_chat_id)
        model_name = str(runtime.get("model") or self.model_selection.current or self.config.models.primary)
        think_level = str(runtime.get("thinkLevel") or "off")
        service_tier = str(runtime.get("serviceTier") or "")
        fast_request = dict(runtime.get("fastRequest") or {})
        cost = dict(runtime.get("cost") or {})
        base_cost = dict(runtime.get("baseCost") or {})
        fast_cost = dict(runtime.get("fastCost") or {})
        fast_requested = bool(runtime.get("fastMode"))
        task_uuid = await self.manager.create_task(
            chat_id=resolved_chat_id,
            workflow_uuid=workflow_uuid,
            title=title,
            input_data={
                "instruction": instruction,
                "raw": instruction,
                "source": source,
                "agentSnapshot": agent_to_snapshot(agent, runtime=runtime),
                "agentSessionUuid": agent_session.session_uuid,
                "callerAgentSessionUuid": caller_agent_session_uuid,
                "parentTaskUuid": parent_task_uuid,
                "turnUuid": lineage_turn_uuid,
                "runRootTurnUuid": lineage_root_turn_uuid,
                "parentTurnUuid": lineage_parent_turn_uuid,
                "inheritFromTaskUuid": str(inherit_from_task_uuid or ""),
                "inheritedPlanContext": dict(inherited_plan_context or {}),
                "planMode": plan_mode,
            },
            parent_session_uuid=resolved_openbear_session_uuid,
            agent_session_uuid=agent_session.session_uuid,
            caller_agent_session_uuid=caller_agent_session_uuid,
            parent_task_uuid=parent_task_uuid,
            # task_uuid is the Agent run id; agent_session_uuid is the long-lived Agent continuity id.
            turn_uuid=lineage_turn_uuid,
            parent_turn_uuid=lineage_parent_turn_uuid,
            run_root_turn_uuid=lineage_root_turn_uuid,
        )
        if inherit_from_task_uuid and inherited_plan_context:
            await self.dao.append_event(
                task_uuid,
                "agent_plan_inherited",
                summary=f"Inherited durable Plan facts from {inherit_from_task_uuid[:8]}",
                detail={
                    "sourceTaskUuid": inherit_from_task_uuid,
                    "completedStepCount": len(inherited_plan_context.get("completedSteps") or []),
                    "evidenceCount": len(inherited_plan_context.get("evidence") or []),
                },
            )
        progress_cb = getattr(ctx, "progress_update", None) if progress_tool_name else None
        progress_payload_cb = getattr(ctx, "progress_update_payload", None) if progress_tool_name else None
        notification_cb = getattr(ctx, "task_notification", None)
        conversation_event_cb = getattr(ctx, "conversation_event", None)
        progress_started = asyncio.get_running_loop().time()
        last_progress_signature = ""
        latest_ledger_usage: dict[str, Any] | None = None
        try:
            context_window = int(self.llm_factory.context_window(model_name) or 0)
        except Exception:
            context_window = 0
        detached = False

        async def _emit_progress() -> str:
            nonlocal last_progress_signature
            if progress_cb is None and progress_payload_cb is None and progress_payload_update is None:
                return ""
            task_row = await self.dao.get_task(task_uuid)
            if task_row is None:
                return ""
            events = await self.dao.events(task_uuid, limit=12)
            event_payload = _events_public(events)
            task_payload = _task_public(task_row, model=model_name, context_window=context_window)
            task_payload["title"] = task_row.title
            task_payload["agentSession"] = self._agent_session_public(agent_session)
            task_payload["detached"] = detached
            result_payload = task_row.output if str(task_row.status or "") in _NOTIFIABLE_AGENT_STATUSES and isinstance(task_row.output, dict) else None
            if result_payload is not None:
                task_payload["output"] = result_payload
            duration_ms = int((asyncio.get_running_loop().time() - progress_started) * 1000)
            agent_session_payload = task_payload.get("agentSession") or self._agent_session_public(agent_session)
            signature = _agent_progress_signature(
                task_payload, event_payload, result_payload, agent_session_payload, latest_ledger_usage
            )
            changed = signature != last_progress_signature
            if changed:
                last_progress_signature = signature
            if changed and progress_payload_update is not None:
                await progress_payload_update(task_payload, event_payload, duration_ms)
            if changed and progress_payload_cb is not None:
                await progress_payload_cb({
                    "toolName": progress_tool_name,
                    "status": task_payload.get("status") or "running",
                    "detached": detached,
                    "agentSession": agent_session_payload,
                    "task": task_payload,
                    **({"ledgerUsage": latest_ledger_usage} if latest_ledger_usage is not None else {}),
                    **({"result": result_payload} if result_payload is not None else {}),
                    "recentEvents": event_payload,
                    "durationMs": duration_ms,
                })
            if progress_cb is not None and not (detached and progress_payload_cb is not None):
                line = format_agent_task_progress_card(
                    progress_tool_name,
                    {"agent": agent.name, "instruction": instruction, "title": title},
                    task_payload,
                    events=event_payload,
                    duration_ms=duration_ms,
                )
                await progress_cb(line)
            return str(task_row.status or "")

        progress_stop_event = asyncio.Event()

        async def _emit_progress_and_maybe_stop() -> str:
            emitted_status = await _emit_progress()
            if emitted_status in _NOTIFIABLE_AGENT_STATUSES:
                progress_stop_event.set()
            return emitted_status

        async def _progress_loop() -> None:
            while not progress_stop_event.is_set():
                try:
                    await asyncio.wait_for(progress_stop_event.wait(), timeout=_DETACHED_PROGRESS_POLL_INTERVAL_S)
                    return
                except TimeoutError:
                    pass
                emitted_status = await _emit_progress_and_maybe_stop()
                if emitted_status in _NOTIFIABLE_AGENT_STATUSES:
                    return

        async def _on_runner_event(**kwargs: Any) -> None:
            try:
                await _emit_progress_and_maybe_stop()
            except Exception:
                pass

        async def _recent_events(limit: int = 8) -> list[dict[str, Any]]:
            return _events_public(await self.dao.events(task_uuid, limit=limit))

        progress_task: asyncio.Task | None = None
        if progress_cb is not None or progress_payload_cb is not None or progress_payload_update is not None:
            await _emit_progress_and_maybe_stop()
            progress_task = asyncio.create_task(_progress_loop())
        child_task: asyncio.Task | None = None
        try:
            backend, model_id, max_tokens = self.llm_factory.backend_for(model_name)
            agent_base_system_prompt = await render_agent_base_system_prompt(
                self.dao.db,
                identity=str(getattr(self.config.memory, "identity", "openbear") or "openbear"),
                registry=self.registry,
                tool_allowlist=agent.tool_allowlist,
                model_name=model_name,
                workspace_dir=self.workspace_dir,
            )
            async def _on_agent_model_call(detail: dict[str, Any]) -> None:
                nonlocal latest_ledger_usage
                latest_ledger_usage = await self._persist_agent_model_call(
                    chat_id=resolved_chat_id,
                    session_uuid=resolved_openbear_session_uuid,
                    model_label=model_name,
                    protocol=str(getattr(backend, "protocol", "") or ""),
                    detail=detail,
                )
                await _emit_progress()

            runner = SingleAgentWorkflowRunner(
                self.dao,
                task_uuid,
                agent=agent,
                backend=backend,
                model=model_id,
                max_tokens=max_tokens,
                tools=self.registry,
                model_label=model_name,
                think_level=think_level,
                service_tier=service_tier,
                fast_request=fast_request,
                session_id=safe_agent_llm_session_id(agent_session.session_uuid, task_uuid, agent.agent_key),
                openbear_session_uuid=resolved_openbear_session_uuid,
                agent_session_uuid=agent_session.session_uuid,
                caller_agent_session_uuid=caller_agent_session_uuid,
                cost=cost,
                base_cost=base_cost,
                fast_cost=fast_cost,
                fast_requested=fast_requested,
                base_system_prompt=agent_base_system_prompt,
                tool_result_max_chars=max_tool_result_chars(
                    self.llm_factory.context_window(model_name),
                    self.config.tools.tool_result_max_chars,
                ),
                **self._model_retry_kwargs(task_uuid),
                model_call_limit=self._model_call_limit_for(agent),
                tool_call_limit=self._tool_call_limit_for(agent),
                plan_control_call_limit=int(getattr(self.config.rath, "plan_control_call_limit", 200) or 200),
                poll_interval_s=0.5,
                on_model_call=_on_agent_model_call,
                on_event=_on_runner_event,
                task_notification=notification_cb,
                conversation_event=conversation_event_cb,
                plan_protocol_enabled=(
                    bool(getattr(self.config.rath, "agent_plan_enabled", True))
                    and plan_mode == _AGENT_PLAN_MODE_MANAGED
                ),
                plan_prompts=self._plan_prompts(),
                **self._context_compact_kwargs(model_name),
            )

            async def _run_registered_child() -> dict[str, Any]:
                async with self.manager.execution_slot(task_uuid):
                    return await runner.run()

            child_task = asyncio.create_task(_run_registered_child(), name=f"agent-tool-{task_uuid[:8]}")
            self.manager.register(task_uuid, resolved_chat_id, child_task, occupies_chat=False)
        except Exception as exc:
            task = await self._mark_task_failed(task_uuid, exc, current_status="任务启动失败")
            with contextlib.suppress(Exception):
                await _emit_progress()
            if progress_task is not None:
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await progress_task
            refreshed_session = await self.dao.agent_session(agent_session.session_uuid)
            return {
                "ok": False,
                "status": "failed",
                "currentStatus": "任务启动失败",
                "error": f"{type(exc).__name__}: {exc}",
                "agentSession": self._agent_session_public(refreshed_session or agent_session),
                "task": _task_public(task, model=model_name, context_window=context_window),
                "recentEvents": await _recent_events(),
            }
        try:
            wait_s = self._foreground_wait_s_for_context(ctx, allow_detach=allow_detach)
            done, _pending = await asyncio.wait({child_task}, timeout=wait_s)
            if child_task not in done:
                detached = True
                if notify_detached:
                    self._watch_detached_agent_task(task_uuid, child_task, notification_cb=notification_cb)
                with contextlib.suppress(Exception):
                    await _emit_progress()
                task = await self.dao.get_task(task_uuid)
                refreshed_session = await self.dao.agent_session(agent_session.session_uuid)
                task_payload = _task_public(task, model=model_name, context_window=context_window)
                task_payload["detached"] = True
                return {
                    "ok": True,
                    "status": "running",
                    "detached": True,
                    "taskUuid": task_uuid,
                    "message": "Agent task is running in background; do not poll repeatedly in this turn.",
                    "next": "Wait for the task-notification or send AgentMessage only if the Agent needs specific guidance.",
                    "agentSession": self._agent_session_public(refreshed_session or agent_session),
                    "task": task_payload,
                    "recentEvents": await _recent_events(),
                }
            try:
                output = child_task.result()
            except asyncio.CancelledError:
                task = await self.manager.mark_cancelled(task_uuid)
                with contextlib.suppress(Exception):
                    await _emit_progress()
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                refreshed_session = await self.dao.agent_session(agent_session.session_uuid)
                return {
                    "ok": False,
                    "status": "cancelled",
                    "error": "cancelled",
                    "agentSession": self._agent_session_public(refreshed_session or agent_session),
                    "task": _task_public(task, model=model_name, context_window=context_window),
                    "recentEvents": await _recent_events(),
                }
            except Exception as exc:
                task = await self._mark_task_failed(task_uuid, exc)
                with contextlib.suppress(Exception):
                    await _emit_progress()
                refreshed_session = await self.dao.agent_session(agent_session.session_uuid)
                return {
                    "ok": False,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "agentSession": self._agent_session_public(refreshed_session or agent_session),
                    "task": _task_public(task, model=model_name, context_window=context_window),
                    "recentEvents": await _recent_events(),
                }
        except asyncio.CancelledError:
            if not detached and not child_task.done():
                child_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await child_task
                with contextlib.suppress(Exception):
                    await self.manager.mark_cancelled(task_uuid)
            raise
        finally:
            if progress_task is not None and not detached:
                progress_stop_event.set()
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await progress_task
        with contextlib.suppress(Exception):
            await _emit_progress()
        task = await self.dao.get_task(task_uuid)
        result = await self._prepare_agent_result(output, task=task)
        refreshed_session = await self.dao.agent_session(agent_session.session_uuid)
        terminal_status = str(task.status if task is not None else "completed")
        payload = {
            "ok": task is not None and task.status == "completed",
            "status": terminal_status,
            "agentSession": self._agent_session_public(refreshed_session or agent_session),
            "task": _task_public(task, model=model_name, context_window=context_window),
            "result": result,
            "resultOutputTokens": _agent_result_output_tokens(task, result) if terminal_status == "completed" else 0,
            "resultCount": 1 if terminal_status == "completed" else 0,
            "recentEvents": await _recent_events(),
        }
        if terminal_status in TERMINAL_TASK_STATUSES:
            payload["next"] = "Agent result is terminal; summarize for the user now. Do not call Read/Bash/search to redo this delegated work."
            payload["finalOnly"] = True
        return payload

    def _agent_session_public(self, session: RathAgentSession) -> dict[str, Any]:
        return {
            "sessionUuid": session.session_uuid,
            "openbearSessionUuid": session.openbear_session_uuid,
            "agentKey": session.agent_key,
            "status": session.status,
            "title": session.title,
            "summary": session.summary,
            "lastTaskUuid": session.last_task_uuid,
        }


    async def _prepare_agent_result(self, output: dict[str, Any], *, task: RathTask | None) -> dict[str, Any]:
        """Return the complete Agent result verbatim.

        Agent conclusions are protected controller input. Context pressure is
        handled by compacting the parent conversation before delivery, never by
        summarizing or truncating the child result itself.
        """
        del task
        return dict(output)


def register_agent_tools(
    reg: ToolRegistry,
    *,
    config: Config,
    dao: RathDAO,
    manager: RathTaskManager,
    llm_factory: BackendFactory,
    model_selection: ModelSelection,
    messages: MessageDAO | None = None,
    workspace_dir: str = "",
) -> None:
    tools = AgentTools(
        config=config,
        dao=dao,
        manager=manager,
        llm_factory=llm_factory,
        model_selection=model_selection,
        registry=reg,
        messages=messages,
        workspace_dir=workspace_dir,
    )
    reg.add(
        "Agent",
        "Launch a focused background Agent task. New tasks run directly by default without the Agent Plan protocol. Use planMode=managed only for explicitly Plan-governed work. Always provide a concise task prompt and an explicit minimal tools array. Empty tools means the child Agent receives no tools. In Web mode this usually returns running/detached; do not poll or redo delegated work.",
        {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "Complete task brief for the Agent, including context, constraints, scope, and output requirements."},
            "tools": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(AGENT_DELEGATION_TOOL_NAMES)},
                "description": (
                    "Required explicit allowlist of tool names this child Agent may use, chosen minimally for this task. "
                    "Use [] for no tools. Values are generated from the canonical child-Agent delegation allowlist."
                ),
            },
            "description": {"type": "string", "description": "Short task label for progress display."},
            "workerType": {"type": "string", "description": "Optional Agent preset key/name from Web Agent presets. Defaults to general-purpose."},
            "planMode": {
                "type": "string",
                "enum": [_AGENT_PLAN_MODE_DIRECT, _AGENT_PLAN_MODE_MANAGED],
                "default": _AGENT_PLAN_MODE_DIRECT,
                "description": (
                    "Execution mode. direct (default) starts work immediately without Plan gates. "
                    "managed enables the full submit/approval/progress/replan/finalize protocol and is reserved for "
                    "explicitly requested planning/audit checkpoints, high-risk external or destructive actions, "
                    "or genuinely dependent multi-Agent work. Task length, code complexity, repository size, and "
                    "tool count alone never require managed mode."
                ),
            },
            "inheritFromTaskUuid": {"type": "string", "description": "Optional interrupted/terminal Agent task UUID from this same conversation. Inherits only durable Plan facts, completed steps, and evidence references; the new task still uses its own planMode, which defaults to direct."},
        }, "required": ["prompt", "tools"]},
        tools.agent,
        visibility={"main", "runtime"},
        preserve_result=True,
    )
    reg.add(
        "AgentMessage",
        "Send a narrow, evidence-based intervention to an active Agent task. Intervene only for a new user instruction, safety risk, scope drift, a real blocker, repeated lack of progress, sufficient evidence, a criterion gap, or Plan inconsistency. Do not steer based on usage metrics. The Agent must explicitly accept, reject, appeal, or request clarification before continuing. If the instruction materially changes an approved Plan's objective, scope, constraints, evidence requirements, or execution method, first call AgentPlanDecision(action=request_replan) against the active version.",
        {"type": "object", "properties": {
            "to": {"type": "string", "description": "Agent task id, short id, title, or preset key. Omit only when exactly one scoped Agent task is active."},
            "message": {"type": "string", "description": "Correction or narrow continuation guidance for the Agent."},
            "reasonCode": {
                "type": "string",
                "enum": ["user_instruction", "safety_risk", "scope_drift", "blocked", "repeated_no_progress", "evidence_sufficient", "criterion_gap", "plan_consistency"],
            },
            "reason": {"type": "string", "description": "Concrete factual basis for intervening now."},
            "evidence": {"type": "array", "items": {"type": "string"}, "description": "Relevant event seqs, evidence UUIDs, tool results, or observed contradictions."},
            "criterionIds": {"type": "array", "items": {"type": "string"}},
            "expectedPlanVersion": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Optimistic CAS for Plan-managed tasks: required and >0 when a visible Plan version exists, "
                    "and must equal pendingPlanVersion or activePlanVersion (replan_required uses active). "
                    "Legacy/Plan-disabled tasks without Plan state may omit it or use 0."
                ),
            },
        }, "required": ["message", "reasonCode", "reason"]},
        tools.agent_message,
        visibility={"main", "runtime"},
        preserve_result=True,
    )
    reg.add(
        "AgentStop",
        "Stop a scoped background Agent task when the user cancels, the task has the wrong context, or continuing is no longer useful.",
        {"type": "object", "properties": {
            "to": {"type": "string", "description": "Agent task id, short id, title, or preset key. Omit only when exactly one scoped Agent task is active."},
            "reason": {"type": "string", "description": "Short reason for stopping the Agent."},
        }},
        tools.agent_stop,
        visibility={"main", "runtime"},
    )
    reg.add(
        "AgentWait",
        "Suspend this same main-controller root turn only when no direct foreground user work remains and scoped Agents are still active. Choose event_only to wait for an interruption/important Agent event, or review_after to schedule one model-selected aggregate review of all scoped Agents. Never use Process or Bash sleep as an Agent timer.",
        {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["event_only", "review_after"], "description": "event_only waits without a timer; review_after wakes once after the model-selected delay unless an important event interrupts it first."},
            "reviewAfterSeconds": {"type": "number", "minimum": 10, "maximum": 86400, "description": "Required only for review_after. Choose based on task scale/risk/progress; progressively lengthen healthy reviews and shorten after intervention or stalled progress."},
            "reason": {"type": "string", "description": "Brief rationale for this wait/review choice, used in the same timeline status card."},
        }, "required": ["mode"]},
        tools.agent_wait,
        visibility={"main", "runtime"},
        preserve_result=True,
    )
    register_agent_plan_tools(reg, tools.plan)
