# ruff: noqa: F401,F403,F405
from __future__ import annotations

import inspect

from app.web_console.message_visibility import visibility_snapshot

from app.interaction_data import normalize_questionnaire as _normalize_web_questionnaire
from app.rath.controller_projection import project_history_message_for_controller
from app.tools.base import current_tool_context
from app.web_console.core import *
from app.web_console.live_stream import *


class WebAdminChatStateMixin:
    def _message_json(self, row) -> dict[str, Any]:
        return {
            "id": row.id,
            "chatId": row.chat_id,
            "role": row.role,
            "content": row.content,
            "reasoning": row.reasoning,
            "signature": row.signature,
            "toolCalls": [
                {"id": t.id, "name": t.name, "arguments": t.arguments}
                for t in (row.tool_calls or [])
            ],
            "toolCallId": row.tool_call_id,
            "name": row.name,
            "tokens": row.tokens,
            "createdAt": row.created_at,
        }

    async def _chat_model_calls(self, chat_id: int, session_uuid: str) -> list[dict[str, Any]]:
        if not session_uuid:
            return []
        cur = await self.db.conn.execute(
            """
            SELECT * FROM model_calls
            WHERE chat_id=? AND session_uuid=?
            ORDER BY created_at ASC, id ASC
            LIMIT 500
            """,
            (chat_id, session_uuid),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def _chat_tool_calls(self, chat_id: int, session_uuid: str) -> list[dict[str, Any]]:
        if not session_uuid:
            return []
        cur = await self.db.conn.execute(
            """
            SELECT * FROM tool_calls
            WHERE chat_id=? AND session_uuid=?
            ORDER BY created_at ASC, id ASC
            LIMIT 1000
            """,
            (chat_id, session_uuid),
        )
        return [dict(r) for r in await cur.fetchall()]

    def _model_thinking_levels(self, model_label: str) -> list[str]:
        resolved = self.config.models.resolve(model_label)
        if not resolved:
            return []
        return list(normalize_think_levels(resolved[1].thinking_levels))

    def _model_default_thinking_level(self, model_label: str) -> str:
        resolved = self.config.models.resolve(model_label)
        if not resolved:
            return "off"
        model_def = resolved[1]
        return configured_default_think_level(model_def.thinking_levels, model_def.default_thinking_level)

    def _model_supports_fast(self, model_label: str) -> bool:
        resolved = self.config.models.resolve(model_label)
        return bool(resolved and resolved[1].supports_fast)

    def _model_rollover_trigger_tokens(self, model_label: str) -> int:
        """Resolve the same effective threshold used by the root Compactor."""
        resolved = self.config.models.resolve(model_label)
        if not resolved:
            return 0
        model_def = resolved[1]
        explicit = max(0, int(model_def.rollover_trigger_tokens or 0))
        if explicit > 0:
            return explicit
        return max(0, int(int(model_def.context_window or 0) * float(self.config.agent.compact_ratio)))

    def _estimate_prompt_tokens(self, *, system: str, convo: list[Message]) -> int:
        total = estimate_tokens(system or "")
        for msg in convo:
            content = msg.get("content") if isinstance(msg, dict) else ""
            if isinstance(content, list):
                total += estimate_tokens(str(content))
            else:
                total += estimate_tokens(str(content or ""))
        return total



    def _rath_context_window_kwargs(self, model_label: str) -> dict[str, Any]:
        label = model_label or getattr(self.model_selection, "current", "") or self.config.models.primary
        resolved = self.config.models.resolve(label)
        policy = self.config.context_management
        return {
            "context_window": int(self.llm_factory.context_window(label) if hasattr(self.llm_factory, "context_window") else 0),
            "rollover_trigger_tokens": int(resolved[1].rollover_trigger_tokens or 0) if resolved else 0,
            "window_trigger_ratio": self.config.agent.compact_ratio,
            "context_config": self.config,
            "context_llm_factory": self.llm_factory,
            "window_retain_ratio": policy.retain_ratio,
        }

    async def _effective_thinking_level(
        self,
        chat_id: int,
        model_label: str,
        *,
        stored_level: str | None = None,
    ) -> str:
        levels = self._model_thinking_levels(model_label)
        if not levels:
            return "off"
        if stored_level is None:
            stored_level = await MessageDAO(self.db).get_thinking_level(chat_id)
        stored = normalize_think_level(stored_level)
        if stored and stored in levels:
            return stored
        return self._model_default_thinking_level(model_label)

    def _pending_web_confirmations(self, conversation_uuid: str) -> list[dict[str, Any]]:
        return self.interactions.pending_for(str(conversation_uuid or ""))

    async def _interaction_changed(self, event: str, item: dict[str, Any]) -> None:
        conv_uuid = str(item.get("conversationUuid") or "")
        live = self._web_live_streams.get(conv_uuid)
        if live is not None:
            # Pending forms are transient authenticated state, not historical
            # operation payloads. The ledger and tool result persist lifecycle.
            await live.publish({
                "type": "web_confirmation", "action": "created" if event == "created" else "resolved",
                "confirmation": self._pending_web_confirmations(conv_uuid),
                "confirmationId": item.get("interactionId"),
            }, persist=False)

    async def _web_confirm(self, conversation_uuid: str, payload: dict[str, Any]) -> dict[str, Any]:
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return {"status": "error", "confirmed": False, "error": "missing_conversation_uuid"}
        cur = await self.db.conn.execute(
            "SELECT owner_chat_id,title FROM web_conversations WHERE conversation_uuid=?", (conv_uuid,),
        )
        row = await cur.fetchone()
        if row is None:
            return {"status": "error", "confirmed": False, "error": "conversation_not_found"}
        ctx = current_tool_context()
        result = await self.interactions.request(
            payload, owner_chat_id=int(row["owner_chat_id"]), conversation_uuid=conv_uuid,
            conversation_title=str(row["title"] or ""),
            turn_uuid=str(ctx.turn_uuid or ctx.run_root_turn_uuid or ""), tool_call_id=str(ctx.tool_call_id or ""),
        )
        if str(result.get("text") or "").strip():
            ctx.preserve_user_answer = True
        return result

    async def _project_context_compaction_operations(
        self,
        chat_id: int,
        conversation_uuid: str,
        operations: list[dict[str, Any]],
        *,
        include_tool_details: bool = True,
        timeline_page: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Read-project summary-only legacy compactions in their anchored turn.

        Native operations remain authoritative. A summary-only fallback is local:
        it is emitted only when its durable message anchor resolves to a turn that
        has operation rows, and a paged response emits it only with that complete
        turn. Missing placement facts are not guessed from timestamps or moved to
        the current/global tail.
        """
        conv_uuid = str(conversation_uuid or "").strip()
        projected = list(operations or [])
        existing_ids = {
            int((item.get("payload") or {}).get("summaryId") or 0)
            for item in projected
            if isinstance(item.get("payload"), dict)
        }
        summaries = await SummaryDAO(self.db).list_with_anchors(chat_id)
        paged = timeline_page is not None
        if paged:
            # A native compaction operation may live outside this page. Read only
            # the small set of compaction payloads/ids so a paged result cannot
            # synthesize a duplicate for an off-page native record.
            cur = await self.db.conn.execute(
                "SELECT op_id, payload_json FROM web_operations "
                "WHERE conversation_uuid=? AND (op_type='context_compaction' "
                "OR source='context_compaction' OR op_id LIKE 'tool:context-compaction:%')",
                (conv_uuid,),
            )
            prefix = "tool:context-compaction:"
            for native_row in await cur.fetchall():
                op_id = str(native_row["op_id"] or "")
                suffix = op_id[len(prefix):] if op_id.startswith(prefix) else ""
                if suffix.isdigit():
                    existing_ids.add(int(suffix))
                native_payload = operation_json_loads_dict(str(native_row["payload_json"] or "{}"))
                native_summary_id = int(native_payload.get("summaryId") or 0)
                if native_summary_id > 0:
                    existing_ids.add(native_summary_id)
        selected_turn_keys = {
            str(key or "")
            for key in ((timeline_page or {}).get("selectedTurnKeys") or [])
            if str(key or "")
        }
        for row in summaries:
            summary_id = int(row.get("id") or 0)
            if not summary_id or summary_id in existing_ids:
                continue
            anchor_conversation = str(row.get("anchor_conversation_uuid") or "").strip()
            if anchor_conversation and conv_uuid and anchor_conversation != conv_uuid:
                continue
            summary = str(row.get("summary") or "")
            created_at_ms = int(row.get("created_at") or 0) * 1000
            turn_uuid = str(row.get("anchor_turn_uuid") or "").strip()
            parent_turn_uuid = str(row.get("anchor_parent_turn_uuid") or "").strip()
            run_root_turn_uuid = str(row.get("anchor_run_root_turn_uuid") or turn_uuid).strip()
            visible_turn_key = run_root_turn_uuid or turn_uuid
            if not turn_uuid or not visible_turn_key:
                continue
            turn_cur = await self.db.conn.execute(
                """
                SELECT COUNT(*) AS operation_count,
                       COALESCE(MAX(display_seq), 0) AS display_seq
                FROM web_operations
                WHERE conversation_uuid=?
                  AND COALESCE(
                    NULLIF(run_root_turn_uuid, ''),
                    NULLIF(turn_uuid, ''),
                    CASE WHEN COALESCE(target_type, '')='run'
                      THEN COALESCE(NULLIF(run_id, ''), NULLIF(target_id, ''))
                    END,
                    ''
                  )=?
                """,
                (conv_uuid, visible_turn_key),
            )
            turn_row = await turn_cur.fetchone()
            if turn_row is None or int(turn_row["operation_count"] or 0) <= 0:
                continue
            if paged and visible_turn_key not in selected_turn_keys:
                continue
            # Summary-only records have no native placement row. Reuse the last
            # durable displaySeq in their own complete turn (stable sort places
            # summaries after durable ties) instead of inventing a global/tail
            # sequence that would make them appear in an unrelated page.
            display_seq = int(turn_row["display_seq"] or 0)
            compaction_id = f"context-compaction:{summary_id}"
            summary_ref = f"/api/conversations/{conv_uuid}/compactions/{summary_id}"
            output_preview = summary[:12_000]
            metadata = {
                "compactionId": compaction_id,
                "summaryId": summary_id,
                "scope": "root",
                "source": "legacy_summary",
                "status": "completed",
                "beforeTokens": 0,
                "afterTokens": 0,
                "summaryChars": len(summary),
                "summaryTokens": int(row.get("tokens") or 0),
                "upToMessageId": int(row.get("up_to_message_id") or 0),
                "outputAvailable": bool(summary),
                "outputPreview": output_preview,
                "summaryRef": summary_ref,
            }
            payload = {
                **metadata,
                "toolCallId": compaction_id,
                "name": "ContextCompaction",
                "toolName": "ContextCompaction",
                "args": json.dumps(metadata, ensure_ascii=False),
                "arguments": json.dumps(metadata, ensure_ascii=False),
                "result": output_preview,
                "durationMs": 0,
                "terminalAtMs": created_at_ms,
            }
            run_id = run_root_turn_uuid or turn_uuid
            synthetic_operation = {
                "conversationId": conv_uuid,
                "conversationUuid": conv_uuid,
                "internalChatId": int(chat_id or 0),
                "opId": f"tool:{compaction_id}",
                "opType": "context_compaction",
                "turnId": turn_uuid,
                "turnUuid": turn_uuid,
                "parentTurnId": parent_turn_uuid,
                "runRootTurnId": run_root_turn_uuid,
                "displaySeq": display_seq,
                "createdAtMs": created_at_ms,
                "updatedAtMs": created_at_ms,
                "terminalAtMs": created_at_ms,
                "revision": 1,
                "status": "completed",
                "lifecycle": "terminal",
                "internal": False,
                "source": "context_compaction",
                "transcriptMessageIds": [],
                "targetType": "run" if run_id else "conversation",
                "targetId": run_id,
                "taskUuid": "",
                "runId": run_id,
                "payload": payload,
            }
            # Persisted operations were already public-projected by
            # `_web_operations`. Only the synthetic legacy compaction record
            # still has a full payload here; project that one exactly once.
            if not include_tool_details:
                synthetic_operation = operation_public(synthetic_operation, include_tool_details=False)
            projected.append(synthetic_operation)
            existing_ids.add(summary_id)
        # Python's sort is stable: durable operations with an equal displaySeq
        # keep the database's canonical id order. Synthetic legacy compactions
        # retain their deterministic SummaryDAO order without using opId as an
        # unrelated and potentially order-changing tie breaker.
        projected.sort(key=lambda item: int(item.get("displaySeq") or 0))
        return projected

    async def _chat_payload(
        self,
        chat_id: int,
        conversation: dict[str, Any] | None = None,
        *,
        timeline_limit: int | None = None,
        before_display_seq: int | None = None,
    ) -> dict[str, Any]:
        messages = MessageDAO(self.db)
        # One sessions read supplies usage and all display preferences. Existing
        # conversations must keep GET /state read-only; only a genuinely absent
        # sessions row retains the historical auto-create/default semantics.
        session_config = await messages.session_snapshot(chat_id)
        usage = messages.usage_totals_from_session(session_config)
        # sessions/model_calls receives every controller and child-Agent request
        # at its completion boundary; rath_tasks is progress metadata for those
        # same Agent calls and must not be added again.
        usage_dict = asdict(usage)
        session_uuid = str(session_config.get("session_uuid") or "")
        ledger_revision = 0
        if session_uuid:
            cur = await self.db.conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS ledger_revision "
                "FROM model_calls WHERE chat_id=? AND session_uuid=?",
                (chat_id, session_uuid),
            )
            try:
                revision_row = await cur.fetchone()
            finally:
                await cur.close()
            ledger_revision = max(0, int((revision_row["ledger_revision"] if revision_row else 0) or 0))
        usage_dict["ledger_revision"] = ledger_revision
        if conversation:
            await self._reconcile_inactive_web_conversation_operations(conversation, source="conversation_state_reconcile")
        model_label = str((conversation or {}).get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
        conv_uuid = str((conversation or {}).get("conversation_uuid") or session_uuid or "")
        live = self._web_live_streams.get(conv_uuid) if conv_uuid else None
        live_snapshot = live.snapshot() if live is not None else {}
        if live_snapshot and not bool(live_snapshot.get("running")):
            conv_status = str((conversation or {}).get("status") or "idle")
            conv_current = str((conversation or {}).get("current_status") or ("运行中" if conv_status == "running" else "就绪"))
            live_snapshot = {
                **live_snapshot,
                "running": False,
                "status": conv_status,
                "currentStatus": conv_current,
                "startedAtMs": 0,
                "statusStartedAtMs": 0,
            }
        # v2 state exposes operation snapshots + latest frame seq as the UI fact
        # source. frame_seq is the only reconnect/resync cursor.  Capture both
        # under the operation lock: a frame cannot be committed between the
        # snapshot query and its high-water mark, so HTTP -> incremental WS
        # bootstrap cannot silently skip that frame.
        operation_page = {
            "hasMoreBefore": False,
            "nextBeforeDisplaySeq": None,
            "timelineLimit": int(timeline_limit) if timeline_limit is not None else None,
            "beforeDisplaySeq": int(before_display_seq or 0) or None,
            "hasAnyOperations": False,
        }
        operations: list[dict[str, Any]] = []
        frame_seq = 0
        timeline_total_duration_ms = 0
        if conv_uuid:
            async with self._web_operation_lock(conv_uuid):
                if timeline_limit is None:
                    operations = await self._web_operations(conv_uuid, include_tool_details=False)
                    operation_page["hasAnyOperations"] = bool(operations)
                else:
                    operations, operation_page = await self._web_operations_page(
                        conv_uuid,
                        limit=timeline_limit,
                        before_display_seq=before_display_seq,
                        include_tool_details=False,
                    )
                # Keep the baseline UI definition even when ``operations`` is a
                # bounded page: one stats snapshot represents one timeline turn.
                # The nested CASE prevents JSON functions from seeing malformed
                # payloads and excludes values that are missing, non-numeric, or
                # negative without loading/deserializing operation rows in Python.
                cur = await self.db.conn.execute(
                    """
                    SELECT CAST(TOTAL(
                      CASE WHEN json_valid(payload_json) THEN
                        CASE
                          WHEN json_type(payload_json, '$.durationMs') IN ('integer', 'real')
                           AND json_extract(payload_json, '$.durationMs') >= 0
                          THEN CAST(json_extract(payload_json, '$.durationMs') AS INTEGER)
                          ELSE 0
                        END
                      ELSE 0 END
                    ) AS INTEGER) AS timeline_total_duration_ms
                    FROM web_operations
                    WHERE conversation_uuid=? AND op_type='stats'
                    """,
                    (conv_uuid,),
                )
                duration_row = await cur.fetchone()
                timeline_total_duration_ms = max(
                    0,
                    int(duration_row["timeline_total_duration_ms"] or 0) if duration_row else 0,
                )
                cur = await self.db.conn.execute(
                    "SELECT COALESCE(MAX(frame_seq), 0) AS frame_seq FROM web_event_frames WHERE conversation_uuid=?",
                    (conv_uuid,),
                )
                frame_row = await cur.fetchone()
                frame_seq = int(frame_row["frame_seq"] or 0) if frame_row else 0
            operations = await self._project_context_compaction_operations(
                chat_id,
                conv_uuid,
                operations,
                include_tool_details=False,
                timeline_page=operation_page if timeline_limit is not None else None,
            )
            if operations:
                operation_page["hasAnyOperations"] = True
        operation_source = "web_operations"
        active_ops = [op for op in operations if str(op.get("lifecycle") or "") in {"active", "paused", "waiting_control"}]
        active_agent_ops = [op for op in active_ops if str(op.get("opType") or "") == "agent"]
        active_foreground_turns = [op for op in active_ops if str(op.get("opType") or "") == "run"]
        operation_facts = {
            "hasOperations": bool(operations),
            "operationCount": len(operations),
            "activeCount": len(active_ops),
            "activeForegroundCount": len(active_foreground_turns),
            "activeAgentCount": len(active_agent_ops),
            "pausedCount": len([op for op in active_ops if str(op.get("lifecycle") or "") == "paused"]),
            "waitingControlCount": len([op for op in active_ops if str(op.get("lifecycle") or "") == "waiting_control"]),
            "latestUpdatedAtMs": max([int(op.get("updatedAtMs") or 0) for op in operations] or [0]),
            "latestActiveUpdatedAtMs": max([int(op.get("updatedAtMs") or 0) for op in active_ops] or [0]),
        }
        thinking_levels = self._model_thinking_levels(model_label)
        stored_thinking_level = str(session_config.get("thinking_level") or "")
        fast_requested = bool(int(session_config.get("fast_mode") or 0))
        fast_supported = self._model_supports_fast(model_label)
        show_thinking_override = session_config.get("show_thinking")
        show_thinking = (
            self.config.ui.show_thinking
            if show_thinking_override is None or int(show_thinking_override) < 0
            else bool(int(show_thinking_override))
        )
        agent_runtime = resolve_agent_runtime_config(
            None,
            config=self.config,
            model_selection_current=str(getattr(self.model_selection, "current", "") or ""),
            conversation=conversation,
            main_model=model_label,
            main_fast_requested=bool(fast_requested),
        )
        agent_run_config = agent_run_config_public(agent_runtime)
        active_rath_tasks = []
        if self.rath is not None:
            with contextlib.suppress(Exception):
                active_rath_tasks = await self.rath.all_active_tasks_for_chat(chat_id)
        rath_running = bool(active_rath_tasks)
        rath_started_candidates = [int(getattr(task, "started_at", 0) or getattr(task, "updated_at", 0) or 0) for task in active_rath_tasks]
        rath_started_candidates = [ts for ts in rath_started_candidates if ts > 0]
        background_started_at_ms = min(rath_started_candidates) * 1000 if rath_started_candidates else 0
        background_status = "Agent 并行执行中" if len(active_rath_tasks) > 1 else "Agent 后台执行中"
        background_tasks: list[dict[str, Any]] = []
        for task in active_rath_tasks:
            session = None
            with contextlib.suppress(Exception):
                session = await self.rath_dao.agent_session(str(getattr(task, "agent_session_uuid", "") or ""))
            snapshot = task.input.get("agentSnapshot") if isinstance(task.input, dict) else {}
            short_id = str(task.task_uuid or "")[:8]
            base_name = str((snapshot or {}).get("name") or (getattr(session, "title", "") if session else "") or task.current_agent_key or "Agent").strip() or "Agent"
            background_tasks.append({
                "taskUuid": task.task_uuid,
                "taskShortId": short_id,
                "displayName": f"{base_name}-{short_id}" if short_id else base_name,
                "title": task.title,
                "status": task.status,
                "currentStatus": task.current_status,
                "agentSessionUuid": task.agent_session_uuid,
                "startedAtMs": int((task.started_at or task.updated_at or 0) * 1000),
                "updatedAtMs": int((task.updated_at or 0) * 1000),
            })
        operation_facts["activeRathTaskCount"] = len(background_tasks)
        operation_facts["activeRathTaskUuids"] = [str(task.get("taskUuid") or "") for task in background_tasks if task.get("taskUuid")]
        pending_steering = steering.pending_items(chat_id)
        exact_context_tokens = await messages.latest_controller_context_usage(chat_id, session_uuid=session_uuid, expected_model=model_label)
        context_trigger_tokens = self._model_rollover_trigger_tokens(model_label)
        context_usage = {
            "known": exact_context_tokens is not None,
            "tokens": int(exact_context_tokens or 0),
            "rolloverTriggerTokens": context_trigger_tokens,
            "percent": ((int(exact_context_tokens) * 100.0 / context_trigger_tokens) if exact_context_tokens is not None and context_trigger_tokens > 0 else None),
        }
        from app.context.store import ContextOwner, WindowStore
        window = await WindowStore(self.db, ContextOwner.controller(chat_id=chat_id, session_uuid=session_uuid)).load() if session_uuid else None
        if window:
            context_usage.update({
                "ownerId": window["owner_key"], "windowVersion": window["window_version"],
                "requestSequence": window["usage_request_sequence"], "requestId": window["usage_request_id"],
                "estimatedNextInputTokens": window["state"].get("estimatedNextInputTokens"),
                "mediaTokensUnknown": bool(window["state"].get("mediaTokensUnknown")),
            })
        # The operation timeline is authoritative whenever it exists. Returning
        # raw messages as well duplicates every tool envelope/output and is only
        # needed for a legacy conversation that has no operation snapshots.
        # A cursor page may legitimately be empty even though durable operations
        # exist outside that cursor. Only a genuinely operation-less legacy
        # conversation falls back to raw messages; ``recent()`` intentionally has
        # no implicit row limit, so legacy history is never silently truncated.
        message_rows = [] if operation_page.get("hasAnyOperations") else await messages.recent(chat_id)
        return {
            "ok": True,
            "chatId": chat_id,
            "conversationUuid": conv_uuid,
            "sessionUuid": session_uuid,
            "contextUsage": context_usage,
            "running": (bool(live_snapshot.get("running")) if live_snapshot else bool(self.runs and self.runs.is_running(chat_id))) or rath_running,
            "backgroundRunning": rath_running,
            "backgroundStartedAtMs": background_started_at_ms,
            "backgroundStatus": background_status if rath_running else "",
            "backgroundTasks": background_tasks,
            "live": live_snapshot,
            "operations": operations,
            "operationSource": operation_source,
            "messageVisibility": await visibility_snapshot(self.db, conv_uuid),
            "frameSeq": frame_seq,
            "timelineTotalDurationMs": timeline_total_duration_ms,
            "hasMoreBefore": bool(operation_page.get("hasMoreBefore")),
            "nextBeforeDisplaySeq": operation_page.get("nextBeforeDisplaySeq"),
            "timelineLimit": operation_page.get("timelineLimit"),
            "beforeDisplaySeq": operation_page.get("beforeDisplaySeq"),
            "facts": {
                "activeOperationIds": [str(op.get("opId") or "") for op in active_ops if op.get("opId")],
                "activeForegroundTurnIds": [str(op.get("turnId") or op.get("turnUuid") or "") for op in active_foreground_turns],
                "activeBackgroundAgentOpIds": [str(op.get("opId") or "") for op in active_agent_ops if op.get("opId")],
                "activeBackgroundTaskUuids": [str(task.get("taskUuid") or "") for task in background_tasks if task.get("taskUuid")],
                "latestFrameSeq": frame_seq,
            },
            "conversation": self._web_conversation_json({**conversation, "cost_usd": usage_dict["cost_usd"]}, live=live, operation_facts=operation_facts) if conversation else None,
            "model": model_label,
            "thinkingLevel": stored_thinking_level,
            "effectiveThinkingLevel": await self._effective_thinking_level(
                chat_id, model_label, stored_level=stored_thinking_level,
            ),
            "thinkingLevels": thinking_levels,
            "defaultThinkingLevel": self._model_default_thinking_level(model_label) if thinking_levels else "",
            "supportsThinking": bool(thinking_levels),
            "fastMode": bool(fast_requested and fast_supported),
            "fastRequested": bool(fast_requested),
            "fastSupported": bool(fast_supported),
            "effectiveFastMode": bool(fast_requested and fast_supported),
            "contextStrategy": str((conversation or {}).get("context_strategy") or "sliding_window"),
            "manualCompactMinPercent": self.config.agent.manual_compact_min_percent,
            "agentRunConfig": agent_run_config,
            "rolloverTriggerTokens": self._model_rollover_trigger_tokens(model_label),
            "windowTriggerRatio": float(self.config.agent.compact_ratio or 0.7),
            "showThinking": show_thinking,
            "messages": [self._message_json(r) for r in message_rows],
            "modelCalls": await self._chat_model_calls(chat_id, session_uuid),
            "toolCalls": await self._chat_tool_calls(chat_id, session_uuid),
            "pendingConfirmations": self._pending_web_confirmations(conv_uuid),
            "pendingSteering": pending_steering,
            "usage": usage_dict,
        }

    async def _build_history(self, chat_id: int) -> list[Message]:
        from app.context.builder import build_controller_history
        return await build_controller_history(MessageDAO(self.db), chat_id, reference_store=self._reference_store())

    async def _build_system_prompt_for_chat(
        self,
        conversation_uuid: str = "",
        *,
        folder_values: tuple[str, str] | None = None,
        strict: bool = False,
    ) -> str:
        try:
            params = await self._prompt_template_params_live(
                conversation_uuid,
                folder_values=folder_values,
            )
            if self.config.memory.provider == "builtin":
                mem = BuiltinMemoryClient(self.db, identity=self.config.memory.identity)
            else:
                from app.memory.client import MemoryClient
                mem = MemoryClient(
                    self.config.memory.base_url,
                    self.config.memory.identity,
                    self.config.memory.access_key,
                    timeout_s=self.config.memory.timeout_s,
                )
            prompt = await mem.build_system_prompt(params)
            if not str(prompt or "").strip():
                raise ValueError("empty system prompt")
            return prompt
        except Exception as exc:
            if strict:
                raise
            log.warning("Web 对话拉取系统提示词失败，降级兜底", 错误=str(exc)[:160])
            return "你是 OpenBear，一个单人自用智能助理。请用中文、简洁、专业地完成用户任务。"

    @staticmethod
    def _merge_agent_task_stats(
        result: RunResult,
        task: Any,
        *,
        status: str = "",
        task_uuid: str = "",
    ) -> bool:
        """Merge one stable Rath task into a root turn exactly once.

        Live/public Agent payloads expose prompt input as a total that already
        includes cache tokens, while durable ``RathTask`` rows expose the exact
        non-cache/cache split. Normalize both shapes into ``Usage`` here so the
        turn footer and historical stats share one accounting rule.
        """

        def value(*names: str, default: Any = None) -> Any:
            for name in names:
                if isinstance(task, dict):
                    if name in task:
                        return task.get(name)
                elif hasattr(task, name):
                    return getattr(task, name)
            return default

        stable_statuses = {
            "completed", "failed", "cancelled", "interrupted", "partial",
            "needs_openbear_control",
        }
        task_status = str(status or value("status", default="") or "")
        if task_status not in stable_statuses:
            return False
        uuid_value = str(task_uuid or value("taskUuid", "task_uuid", default="") or "")
        if not uuid_value or uuid_value in result.expert_accounted_task_uuids:
            return False

        exact_names = {
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens",
        }
        has_exact_usage = (
            any(name in task for name in exact_names)
            if isinstance(task, dict)
            else any(hasattr(task, name) for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        )
        if has_exact_usage:
            input_tokens = max(0, int(value("input_tokens", "inputTokens", default=0) or 0))
            output_tokens = max(0, int(value("output_tokens", "outputTokens", default=0) or 0))
            cache_read_tokens = max(0, int(value("cache_read_tokens", "cacheReadTokens", default=0) or 0))
            cache_write_tokens = max(0, int(value("cache_write_tokens", "cacheWriteTokens", default=0) or 0))
        else:
            tokens = value("tokens", default={})
            tokens = tokens if isinstance(tokens, dict) else {}
            public_input = max(0, int(tokens.get("input") or 0))
            output_tokens = max(0, int(tokens.get("output") or 0))
            public_cache = max(0, int(tokens.get("cache") or 0))
            input_tokens = max(0, public_input - public_cache)
            cache_read_tokens = public_cache
            cache_write_tokens = 0

        if uuid_value:
            result.expert_accounted_task_uuids.add(uuid_value)
        result.expert_tasks += 1
        result.expert_model_calls += max(0, int(value("modelCalls", "model_call_count", default=0) or 0))
        result.expert_tool_calls += max(0, int(value("toolCalls", "tool_call_count", default=0) or 0))
        result.expert_usage.input_tokens += input_tokens
        result.expert_usage.output_tokens += output_tokens
        result.expert_usage.cache_read_tokens += cache_read_tokens
        result.expert_usage.cache_write_tokens += cache_write_tokens
        result.expert_usage.total_tokens += input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
        try:
            result.expert_cost_usd += max(0.0, float(value("costUsd", "cost_usd", default=0.0) or 0.0))
        except (TypeError, ValueError):
            pass
        try:
            duration_ms = max(0, int(value("durationMs", "duration_ms", default=0) or 0))
            if duration_ms <= 0:
                started_at = max(0, int(value("started_at", "startedAt", default=0) or 0))
                finished_at = max(0, int(value("finished_at", "finishedAt", default=0) or 0))
                if started_at and finished_at > started_at:
                    duration_ms = (finished_at - started_at) * 1000
            result.expert_duration_ms = max(int(getattr(result, "expert_duration_ms", 0) or 0), duration_ms)
        except (TypeError, ValueError):
            pass
        return True

    @staticmethod
    def _merge_agent_notification_stats(result: RunResult, payload: dict[str, Any] | None) -> None:
        if not isinstance(payload, dict):
            return

        def task_items() -> list[dict[str, Any]]:
            if isinstance(payload.get("results"), list):
                return [item for item in payload["results"] if isinstance(item, dict)]
            items: list[dict[str, Any]] = []
            task = payload.get("task") if isinstance(payload.get("task"), dict) else None
            if task is not None:
                items.append({"task": task, "status": payload.get("status")})
            return items

        for item in task_items():
            task = item.get("task") if isinstance(item.get("task"), dict) else {}
            WebAdminChatStateMixin._merge_agent_task_stats(
                result,
                task,
                status=str(task.get("status") or item.get("status") or ""),
                task_uuid=str(task.get("taskUuid") or item.get("taskUuid") or payload.get("taskUuid") or ""),
            )

    def _run_stats_json(self, result: RunResult, *, cost_usd: float, model: str,
                        think_level: str, context_window: int, live: bool = False,
                        ledger_usage: dict[str, Any] | None = None,
                        ledger_cost_usd: float | None = None) -> dict[str, Any]:
        # `contextTokens` means the latest OpenBear controller prompt size shown
        # in the per-turn stats panel.  Child Agent cumulative usage stays in
        # `expertUsage`; mixing it into context makes the value look larger than
        # the model window and the frontend has to hide it as dirty data.
        prompt_tokens = (
            result.last_usage.input_tokens
            + result.last_usage.cache_read_tokens
            + result.last_usage.cache_write_tokens
        )
        duration_ms = max(int(result.total_time_ms or 0), int(getattr(result, "expert_duration_ms", 0) or 0))
        if live and duration_ms <= 0 and result.start_monotonic > 0:
            duration_ms = int((time.monotonic() - result.start_monotonic) * 1000)
        model_ok = max(0, result.model_ok + result.expert_model_calls)
        avg_connect = result.connect_ms_sum / model_ok if model_ok else 0
        avg_first = result.first_token_ms_sum / model_ok if model_ok else 0
        avg_total = result.call_time_ms_sum / model_ok if model_ok else 0
        avg_tps = (
            result.output_tokens_sum / (result.call_time_ms_sum / 1000)
            if result.call_time_ms_sum > 0 else 0.0
        )
        context_usage_known = bool(result.model_ok > 0 and result.last_prompt_usage_reported)
        stats = {
            "live": bool(live),
            "model": model,
            "thinkLevel": think_level,
            "durationMs": duration_ms,
            "reasoningMs": result.reasoning_ms_sum,
            "modelCalls": result.model_calls + result.expert_model_calls,
            "modelOk": model_ok,
            "modelRetry": result.model_retry,
            "modelFail": result.model_fail + result.summary_model_fail,
            "toolCalls": len(result.tools_used) + result.expert_tool_calls,
            "expertModelCalls": result.expert_model_calls,
            "expertToolCalls": result.expert_tool_calls,
            "expertTasks": result.expert_tasks,
            "expertTaskUuids": sorted(result.expert_accounted_task_uuids),
            # Compatibility display field: older clients already understand this
            # provider snapshot. Authorization and post-rotation invalidation use
            # the explicit contextUsage.known contract below.
            "contextTokens": prompt_tokens,
            "contextWindow": context_window,
            "contextUsage": {
                "available": bool(result.model_ok > 0),
                "known": context_usage_known,
                "tokens": prompt_tokens if context_usage_known else 0,
                "ownerId": result.context_owner_id,
                "windowVersion": result.context_window_version,
                "requestSequence": result.context_request_sequence,
                "requestId": result.context_request_id,
                "estimatedNextInputTokens": result.context_estimated_input_tokens,
            },
            "lastUsage": _usage_json(result.last_usage),
            "expertUsage": _usage_json(result.expert_usage),
            "avgConnectMs": avg_connect,
            "avgFirstTokenMs": avg_first,
            "avgTotalMs": avg_total,
            "avgTps": avg_tps,
            "peakTps": result.peak_tps,
            "minTps": result.min_tps,
            "usage": _usage_json(result.usage),
            "costUsd": cost_usd + result.expert_cost_usd,
            "haltedReason": result.halted_reason,
        }
        if ledger_usage is not None:
            # Absolute durable session total, not a delta. `ledgerRevision` is the
            # latest model_calls.id for the current session epoch, so concurrent
            # Agent operations can reject an older snapshot without adding totals.
            normalized_ledger_usage = {
                "ledgerRevision": max(0, int(ledger_usage.get("ledgerRevision") or 0)),
                "inputTokens": max(0, int(ledger_usage.get("inputTokens") or 0)),
                "outputTokens": max(0, int(ledger_usage.get("outputTokens") or 0)),
                "cacheReadTokens": max(0, int(ledger_usage.get("cacheReadTokens") or 0)),
                "cacheWriteTokens": max(0, int(ledger_usage.get("cacheWriteTokens") or 0)),
                "costUsd": max(0.0, float(ledger_usage.get("costUsd") or 0.0)),
            }
            stats["ledgerUsage"] = normalized_ledger_usage
            # Retain the old cost-only field for older Web clients.
            stats["ledgerCostUsd"] = normalized_ledger_usage["costUsd"]
        elif ledger_cost_usd is not None:
            stats["ledgerCostUsd"] = max(0.0, float(ledger_cost_usd or 0.0))
        return stats

    async def _persist_web_model_call_delta(
        self,
        messages: MessageDAO,
        chat_id: int,
        *,
        session_uuid: str,
        call: dict[str, Any],
        model_cost: dict[str, float],
        model_label: str,
        protocol: str,
        think_level: str,
        call_kind: str = "controller_request",
        cost_usd_override: float | None = None,
    ) -> float:
        """Commit one completed upstream request before the next tool/model step.

        `model_calls` is the durable billing ledger used by channel/model
        statistics.  Writing one immutable row per request means a later crash
        cannot erase already returned usage.  Session totals and per-turn counters
        are updated in the same awaited boundary.
        """
        usage_present = isinstance(call.get("usage"), Usage)
        usage = call.get("usage") if usage_present else Usage()
        if cost_usd_override is None:
            cost = _resolved_usage_cost_usd(
                model_cost,
                usage,
                actual_service_tier=call.get("serviceTier"),
                provider_cost_usd=call.get("providerCostUsd"),
            )
        else:
            try:
                cost = max(0.0, float(cost_usd_override))
            except (TypeError, ValueError, OverflowError):
                cost = 0.0
        status = str(call.get("status") or "ok")
        connect_ms = max(0, int(call.get("connectMs") or 0))
        first_token_ms = max(0, int(call.get("firstTokenMs") or 0))
        total_time_ms = max(0, int(call.get("totalTimeMs") or 0))
        output_tokens = max(0, int(call.get("outputTokens") or usage.output_tokens or 0))
        ok_count = 1 if status == "ok" else 0
        fail_count = 0 if status == "ok" else 1
        async with self.db.accounting_transaction() as connection:
            accounting = MessageDAO(self.db, connection=connection)
            await accounting.add_usage(
                chat_id,
                usage,
                cost,
                commit=False,
                last_usage=usage,
                last_cost_usd=cost,
                connect_ms=connect_ms,
                first_token_ms=first_token_ms,
                total_time_ms=total_time_ms,
                run_total_time_ms=total_time_ms,
                run_model_calls=1,
                run_tool_calls=0,
                model=model_label,
                protocol=protocol,
                think_level=think_level,
            )
            await accounting.add_turn_stats(
                chat_id,
                commit=False,
                model_calls=1,
                model_ok=ok_count,
                model_fail=fail_count,
                connect_ms_sum=connect_ms,
                first_token_ms_sum=first_token_ms,
                total_time_ms_sum=total_time_ms,
                output_tokens_sum=output_tokens,
            )
            await accounting.add_model_call(
                chat_id,
                commit=False,
                session_uuid=session_uuid,
                model=model_label,
                protocol=protocol,
                think_level=think_level,
                call_kind=call_kind,
                usage=usage,
                last_usage=usage,
                cost_usd=cost,
                connect_ms=connect_ms,
                first_token_ms=first_token_ms,
                total_time_ms=total_time_ms,
                status=status,
                model_call_count=1,
                model_ok_count=ok_count,
                model_retry_count=1 if call.get("retry") else 0,
                model_fail_count=fail_count,
                error_type=str(call.get("errorType") or ""),
            )
        return cost

    async def _persist_web_run_metrics(
        self,
        messages: MessageDAO,
        chat_id: int,
        *,
        session_uuid: str,
        result: RunResult,
        model_cost: dict[str, float],
        model_label: str,
        protocol: str,
        think_level: str,
        status: str = "ok",
        error_type: str = "",
    ) -> float:
        """Persist the model/usage counters accumulated by Agent.run.

        The Agent loop mutates the shared RunResult as each model/tool step
        completes.  A manual Web stop raises CancelledError before the normal
        success epilogue, so this helper is used by both paths to avoid losing
        the already-completed token/cost/context statistics.
        """
        request_cost = _usage_cost_usd(model_cost, result.usage)
        total_run_cost = request_cost + result.expert_cost_usd
        last_request_cost = _usage_cost_usd(model_cost, result.last_usage)
        total_model_calls = result.model_calls + result.expert_model_calls
        total_model_ok = result.model_ok + result.expert_model_calls
        total_tool_calls = len(result.tools_used) + result.expert_tool_calls
        await messages.add_usage(
            chat_id,
            _usage_sum(result.usage, result.expert_usage),
            total_run_cost,
            last_usage=result.last_usage,
            last_cost_usd=last_request_cost,
            connect_ms=result.last_call_connect_ms,
            first_token_ms=result.last_call_first_token_ms,
            total_time_ms=result.last_call_time_ms,
            run_total_time_ms=result.total_time_ms,
            run_model_calls=total_model_calls,
            run_tool_calls=total_tool_calls,
            model=model_label,
            protocol=protocol,
            think_level=think_level,
        )
        await messages.add_turn_stats(
            chat_id,
            tool_calls=total_tool_calls,
            model_calls=total_model_calls,
            model_ok=total_model_ok,
            model_retry=result.model_retry,
            model_fail=result.model_fail,
            connect_ms_sum=result.connect_ms_sum,
            first_token_ms_sum=result.first_token_ms_sum,
            total_time_ms_sum=result.call_time_ms_sum,
            output_tokens_sum=result.output_tokens_sum,
        )
        usage_total = (
            result.usage.input_tokens
            + result.usage.output_tokens
            + result.usage.cache_read_tokens
            + result.usage.cache_write_tokens
            + result.expert_usage.input_tokens
            + result.expert_usage.output_tokens
            + result.expert_usage.cache_read_tokens
            + result.expert_usage.cache_write_tokens
        )
        if result.model_calls or result.expert_model_calls or usage_total:
            await messages.add_model_call(
                chat_id,
                session_uuid=session_uuid,
                model=model_label,
                protocol=protocol,
                think_level=think_level,
                call_kind="controller_run",
                usage=result.usage,
                last_usage=result.last_usage,
                expert_usage=result.expert_usage,
                cost_usd=total_run_cost,
                connect_ms=result.connect_ms_sum,
                first_token_ms=result.first_token_ms_sum,
                total_time_ms=result.call_time_ms_sum,
                peak_tps=result.peak_tps,
                min_tps=result.min_tps,
                status=status,
                model_call_count=total_model_calls,
                model_ok_count=total_model_ok,
                model_retry_count=result.model_retry,
                model_fail_count=result.model_fail,
                expert_tool_calls=result.expert_tool_calls,
                error_type=error_type or result.halted_reason,
            )
        return request_cost



__all__ = [name for name in globals() if not name.startswith("__")]
