"""Main-controller continuation and read-only inspection of independent Agents."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from app.rath.continuity import AgentContinuityError, agent_session_public
from app.rath.schemas import TERMINAL_TASK_STATUSES, RathTask
from app.tools.allowlist import (
    AGENT_DELEGATION_TOOL_NAMES,
    agent_phase_tool_names,
    expand_agent_tool_names,
)
from app.tools.base import current_tool_context


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class AgentContinuationTools:
    async def _resolve_instance_target(self, ref: str):
        ctx = current_tool_context()
        session = await self.dao.agent_session(ref)
        task = None
        if session is None:
            task = await self.dao.get_task(ref)
            if task is None or self._task_scope_error(task, ctx):
                raise AgentContinuityError("agent_instance_not_found", "No Agent instance or exact task UUID exists in this conversation")
            session = await self.dao.agent_session(task.agent_session_uuid)
        if session:
            scoped = RathTask(task_uuid="", chat_id=session.chat_id, parent_session_uuid=session.openbear_session_uuid)
            if self._task_scope_error(scoped, ctx):
                raise AgentContinuityError("agent_instance_not_found", "No Agent instance exists in this conversation")
        if session is None and task is None:
            raise AgentContinuityError("agent_instance_not_found", "Agent instance not found")
        if session and session.session_kind == "independent":
            task = await self.dao.get_task(session.context_task_uuid or session.last_task_uuid) or task
        elif task is None:
            raise AgentContinuityError("legacy_group_requires_explicit_task", "This is a legacy role group; select one exact task UUID, not the whole group")
        return session, task

    async def agent_continue(self, args: dict[str, Any]) -> str:
        from app.tools.agents import _normalize_agent_plan_mode, _task_public
        ctx = current_tool_context()
        if ctx.source.startswith("agent:"):
            return _json({"ok": False, "error": "main_controller_only"})
        prompt = str(args.get("prompt") or "").strip()
        if not prompt or not str(args.get("to") or "").strip():
            return _json({"ok": False, "error": "agent_continue_target_and_prompt_required"})
        try:
            session, task = await self._resolve_instance_target(str(args["to"]).strip())
            if session and session.status != "active":
                raise AgentContinuityError("agent_instance_closed", "The Agent instance is closed")
            if session and session.active_task_uuid and not args.get("requestId"):
                raise AgentContinuityError("agent_instance_busy", "Use AgentMessage for the unfinished task, or stop it before a new instruction", task_uuid=session.active_task_uuid)
            if task and task.status not in TERMINAL_TASK_STATUSES and (not session or session.session_kind != "independent"):
                raise AgentContinuityError("agent_instance_busy", "Finish or stop the original task first", task_uuid=task.task_uuid)
            snapshot = (session.metadata or {}).get("agentSnapshot") if session else None
            if snapshot:
                source = RathTask(task_uuid="", workflow_uuid=session.workflow_uuid, input={"agentSnapshot": snapshot})
                agent = await self._agent_from_task(source)
            else:
                agent = await self._agent_from_task(task) if task else None
            if agent is None:
                raise AgentContinuityError("agent_definition_unavailable", "The original Agent definition cannot be restored")
            ceiling = (session.metadata or {}).get("presetToolCeiling") if session else None
            if ceiling is None:
                ceiling = (task.input or {}).get("presetToolCeiling") if task else None
            if ceiling is None:
                preset = await self.dao.agent_by_key(agent.agent_key, include_disabled=True) if agent.id else None
                ceiling = list(preset.tool_allowlist) if preset else (list(agent.tool_allowlist) if agent.id else [])
            resolution = self._resolve_requested_agent_tools(args, replace(agent, tool_allowlist=ceiling))
            if resolution.get("error"):
                return _json({"ok": False, **resolution})
            mode = _normalize_agent_plan_mode(args.get("planMode"))
            if mode not in {"direct", "managed"}:
                return _json({"ok": False, "error": "invalid_agent_plan_mode"})
            if mode == "managed" and not getattr(self.config.rath, "agent_plan_enabled", True):
                return _json({"ok": False, "error": "managed_agent_plan_disabled"})
            if mode == "managed" and (ctx.task_notification is None or ctx.agent_wait is None):
                return _json({"ok": False, "error": "controller_runtime_required"})
            attachments, error = await self._resolve_attachments(args.get("attachments"))
            if error:
                return _json({"ok": False, **error})
            granted = list(resolution["tools"])
            if attachments and "Read" not in granted:
                if ceiling and "Read" not in ceiling:
                    raise AgentContinuityError("agent_attachment_tool_denied", "Attached files require Read within the preset tool ceiling")
                granted.append("Read")
            if session is None or session.session_kind != "independent":
                session = await self.dao.adopt_legacy_agent_task(task.task_uuid, preset_tool_ceiling=ceiling)
                await self.dao.freeze_agent_instance(session.session_uuid, presetToolCeiling=ceiling)
            if not session.context_task_uuid:
                raise AgentContinuityError("agent_context_unavailable", "No reliable checkpoint is available; continuation cannot be fabricated from a report")
            checkpoint = await self.dao.task_model_context(session.context_task_uuid)
            if not checkpoint:
                raise AgentContinuityError("agent_context_unavailable", "The instance checkpoint is unavailable")
            source = {"taskUuid": session.context_task_uuid, "revision": session.context_revision,
                      "state": "restored" if checkpoint["state"].get("stage") == "completed" else "partial"}
            if attachments:
                prompt = self._materialize_attachments(prompt, attachments)
            result = await self._run_one(
                replace(agent, tool_allowlist=granted), instruction=prompt,
                title=str(args.get("description") or prompt[:80]), progress_tool_name="AgentContinue",
                plan_mode=mode, instance=session, context_source=source, preset_ceiling=ceiling,
                continuation_request_id=str(args.get("requestId") or ctx.tool_call_id or ""),
                attachment_refs=list(args.get("attachments") or []),
                original_prompt=str(args["prompt"]).strip(),
            )
            return _json(result)
        except AgentContinuityError as exc:
            if exc.code == "agent_continue_replayed":
                existing = await self.dao.get_task(exc.task_uuid)
                return _json({"ok": True, "replayed": True, "task": _task_public(existing, include_output=True),
                              "taskUuid": exc.task_uuid, "status": existing.status})
            return _json(exc.public())
        except ValueError as exc:
            return _json({"ok": False, "error": "agent_continue_invalid", "message": str(exc)})

    async def _inspect_capabilities(self, task) -> dict[str, Any]:
        """Use the runner's capability resolver, then apply its dispatch gates."""
        if task is None:
            return {"schemaTools": [], "effectiveTools": [], "phase": "idle"}
        from app.tools.agents import _task_agent_plan_mode
        data = task.input or {}
        granted = (data.get("agentSnapshot") or {}).get("toolAllowlist") or []
        managed = bool(getattr(self.config.rath, "agent_plan_enabled", True)) and _task_agent_plan_mode(task) == "managed"
        cur = await self.dao.db.conn.execute("SELECT * FROM rath_task_plan_state WHERE task_uuid=?", (task.task_uuid,))
        row = await cur.fetchone()
        state = dict(row) if row else {}
        phase = str(state.get("phase") or ("drafting" if managed else "direct"))
        approved = json.loads(state.get("approved_tools_json") or "[]")
        if state.get("active_plan_version") and not approved:
            approved = granted
        cur = await self.dao.db.conn.execute(
            "SELECT 1 FROM rath_task_controls WHERE task_uuid=? AND action='steer' AND (status='pending' OR (status='applied' AND responded_at=0)) LIMIT 1",
            (task.task_uuid,),
        )
        pending = await cur.fetchone() is not None
        schemas = agent_phase_tool_names(granted, managed=managed, phase=phase,
                                         approved=approved, pending_control=pending,
                                         ceiling=data.get("presetToolCeiling") or [])
        schemas &= set(self.registry.names(scope="agent"))
        effective = set(schemas)
        if task.status in TERMINAL_TASK_STATUSES or task.status == "needs_openbear_control":
            effective.clear()
        elif pending or (managed and phase == "finalizing"):
            effective &= {"AgentControlAck"}
        elif managed and phase == "executing" and not state.get("current_step_id"):
            effective -= expand_agent_tool_names(AGENT_DELEGATION_TOOL_NAMES)
        return {"schemaTools": sorted(schemas), "effectiveTools": sorted(effective),
                "grantedTools": sorted(expand_agent_tool_names(granted)),
                "presetToolCeiling": list(data.get("presetToolCeiling") or []), "phase": phase}

    async def agent_info(self, args: dict[str, Any]) -> str:
        from app.tools.agents import _task_agent_plan_mode, _task_public
        ctx = current_tool_context()
        if ctx.source.startswith("agent:"):
            return _json({"ok": False, "error": "main_controller_only"})
        action = str(args.get("action") or "get")
        if action == "capabilities":
            available = self._agent_tool_names()
            return _json({"ok": True, "delegableTools": available,
                          "expandedDelegableTools": sorted(expand_agent_tool_names(available)),
                          "descriptions": {name: self.registry.summaries(scope="agent").get(name, "") for name in available},
                          "context": "Initial: supplied prompt, attached files, own instance memory and explicit shared memory. Continuation: same instance checkpoint, not parent or sibling histories."})
        if not ctx.session_uuid and ctx.chat_id <= 0:
            return _json({"ok": False, "error": "conversation_scope_required"})
        if action == "list":
            sessions = await self.dao.list_agent_sessions(openbear_session_uuid=ctx.session_uuid,
                                                         chat_id=ctx.chat_id if ctx.chat_id > 0 else None, limit=100)
            return _json({"ok": True, "instances": [agent_session_public(item) for item in sessions]})
        if action != "get":
            return _json({"ok": False, "error": "invalid_action"})
        try:
            session, task = await self._resolve_instance_target(str(args.get("to") or ""))
            independent = session and session.session_kind == "independent"
            tasks = await self.dao.agent_instance_tasks(session.session_uuid) if independent else [task]
            checkpoint = await self.dao.task_model_context(session.context_task_uuid) if independent and session.context_task_uuid else None
            from app.context.store import ContextOwner, WindowStore
            context_task = session.context_task_uuid if independent and session.context_task_uuid else (task.task_uuid if task else "")
            window_owner = ContextOwner.agent(task_uuid=context_task, agent_session_uuid=session.session_uuid if independent else "") if context_task else None
            window = await WindowStore(self.dao.db, window_owner).load() if window_owner else None
            # The checkpoint may still belong to an earlier round, especially
            # when the latest round ended before saving any context.
            current_uuid = (session.active_task_uuid or session.last_task_uuid) if independent else ""
            current = await self.dao.get_task(current_uuid) if current_uuid else task
            capabilities = await self._inspect_capabilities(current)
            plan_runtime = {"taskUuid": current.task_uuid if current else "",
                            "planMode": _task_agent_plan_mode(current) if current else None,
                            "available": False}
            if current and plan_runtime["planMode"] == "direct":
                plan_runtime.update(available=True, phase="direct", planVersion=0,
                                    activePlanVersion=0, pendingPlanVersion=0, currentStepId="",
                                    plan=None, steps=[], evidence=[])
            elif current:
                # Coordinator.snapshot lazily initializes missing Plan state.
                # Inspection must remain read-only, including for legacy tasks.
                cur = await self.dao.db.conn.execute(
                    "SELECT 1 FROM rath_task_plan_state WHERE task_uuid=?", (current.task_uuid,),
                )
                snapshot = await self._plan_notification_snapshot(current.task_uuid) if await cur.fetchone() else {}
                if snapshot:
                    state = snapshot["state"]
                    plan_runtime.update(snapshot, available=True, phase=state.get("phase"),
                                        activePlanVersion=int(state.get("active_plan_version") or 0),
                                        pendingPlanVersion=int(state.get("pending_plan_version") or 0),
                                        currentStepId=str(state.get("current_step_id") or ""))
            material_refs = [{"taskUuid": item.task_uuid, "attachments": item.input.get("attachmentRefs") or []}
                             for item in tasks if item.input.get("attachmentRefs")]
            context_detail = {"revision": session.context_revision if independent else 0,
                              "templatePinned": bool(independent and "baseSystemPrompt" in session.metadata),
                              "legacyTemplateUnpinned": bool(session and session.metadata.get("legacyTemplateUnpinned")),
                              "windowAvailable": bool(window and window["state"].get("messages") is not None),
                              "windowVersion": int(window["window_version"]) if window else None,
                              "historyCoverage": "incremental_originals" if window else "legacy_checkpoint_only"}
            return _json({"ok": True, "agentSession": agent_session_public(session) if session else {},
                          "legacy": not independent, "tasks": [_task_public(item, include_output=True) for item in tasks],
                          "task": _task_public(current, include_output=True),
                          "contextAvailable": bool(checkpoint),
                          "contextState": str((checkpoint or {}).get("state", {}).get("stage") or "unavailable"),
                          "memoryScope": "agent_session" if independent else "agent_task",
                          "capabilities": capabilities, "materials": material_refs, "context": context_detail,
                          "planRuntime": plan_runtime,
                          "workspace": self.workspace_dir})
        except AgentContinuityError as exc:
            return _json(exc.public())


def register_continuation_tools(reg, tools) -> None:
    reg.add("AgentContinue", "Continue the SAME independent Agent after its round has ended. Restores its actual retained context and private memory, appends a new instruction, and creates a new task record without reopening the previous result. No prior memory report or progress note is required. Pass the COMPLETE tools array for this round; permissions are not inherited or unioned. Use AgentMessage for unfinished tasks. Main controller only; new scope still requires user authorization.",
            {"type": "object", "properties": {
                "to": {"type": "string", "description": "Exact agentId, or exact legacy task UUID to adopt only that task."},
                "prompt": {"type": "string", "description": "New instruction, changed scope and preserved constraints; do not repeat the retained investigation."},
                "tools": {"type": "array", "items": {"type": "string", "enum": sorted(AGENT_DELEGATION_TOOL_NAMES)},
                          "description": "Complete business-tool allowlist for this round. [] means no business tools; runtime AgentHistory and necessary protocol tools may still be present."},
                "description": {"type": "string"}, "attachments": {"type": "array", "items": {"type": "string"}},
                "planMode": {"type": "string", "enum": ["direct", "managed"], "default": "direct"},
                "requestId": {"type": "string", "description": "Optional idempotency key; reuse only for the same continuation request."},
            }, "required": ["to", "prompt", "tools"]}, tools.agent_continue,
            visibility={"main", "runtime"}, preserve_result=True)
    reg.add("AgentInfo", "Read current-conversation Agent instances, retained-context availability, rounds/results and delegable capabilities. get returns the current task's authoritative planRuntime (phase, active/pending versions, Plan, steps and evidence), not inferred historical notifications. Use its taskUuid and pendingPlanVersion or activePlanVersion for version-checked Plan decisions; direct explicitly has no Plan, and available=false means Plan state could not be read. Read-only; use on demand, not as a polling timer. Does not expose private model messages or memory catalogs/bodies.",
            {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "get", "capabilities"]},
                "to": {"type": "string", "description": "Exact agentId or task UUID for get."},
            }, "required": ["action"]}, tools.agent_info, visibility={"main", "runtime"}, preserve_result=True)
