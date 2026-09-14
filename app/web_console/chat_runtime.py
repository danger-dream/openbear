# ruff: noqa: F401,F403,F405
from __future__ import annotations

import inspect

from app.agent.native_continuation import deserialize_messages, validate_model_context
from app.context.configuration import conversation_strategy
from app.context.prompts import effective_context_prompt
from app.context.resume import restore_controller_run_inputs
from app.context.runtime import ContextManager
from app.context.store import ContextOwner, WindowStore
from app.context.strategies import ModelSummaryStrategy
from app.context.window import WindowPolicy, mark_source
from app.model_cost import resolved_usage_cost_usd
from app.references import BUNDLE_FIELD, ReferenceError
from app.task_memory import (
    TaskMemoryDAO,
    inject_runtime_block_into_latest_user,
    is_task_memory_runtime_message,
    reconcile_task_memory_runtime_state,
    reset_task_memory_runtime_epoch,
    task_memory_runtime_epoch,
)
from app.tools.file_state import clear_read_file_state
from app.web_console.conversation_prompt import system_prompt_sha256
from app.web_console.core import *
from app.web_console.live_stream import *
from app.web_console.notification_delivery import (
    claim_paused_notifications,
    notification_context_messages,
    notification_ids,
    notification_items,
)


class WebAdminChatRunMixin:
    async def _run_web_turn(
        self,
        chat_id: int,
        user_text: str,
        renderer: _WebStreamRenderer,
        media: list[InboundMedia] | None = None,
        *,
        conversation: dict[str, Any] | None = None,
        task_notification: bool = False,
        task_notification_payload: dict[str, Any] | None = None,
        background_control_payload: dict[str, Any] | None = None,
        root_turn_uuid: str = "",
        user_op_id: str = "",
        reference_bundle_id: str = "",
    ) -> bool:
        messages = MessageDAO(self.db)
        user_saved = False
        turn_succeeded = False
        controller_notification_ids: set[str] = set()
        result = RunResult()
        model_cost: dict[str, Any] = {}
        base_model_cost: dict[str, Any] = {}
        fast_model_cost: dict[str, Any] = {}
        think_level = ""
        run_stored_think = ""
        run_fast_mode_requested = False
        backend_protocol = ""
        ctx_window = 0
        session_id = ""
        stats_task: asyncio.Task[Any] | None = None
        post_turn_actions_drained = False
        conversation_uuid = str((conversation or {}).get("conversation_uuid") or "")
        model_label = str((conversation or {}).get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
        if self.control_actions is not None:
            # Never let a late cancel click from a completed turn affect a new run.
            self.control_actions.clear_retry_cancel(chat_id)
        try:
            await messages.ensure_session(chat_id)
            session_id = await messages.get_or_create_session_uuid(chat_id)
            # Snapshot per-run preferences as early as possible.  The composer can
            # change thinking/Fast while a run is active; those edits are persisted
            # for the next run and must not tear this in-flight request.
            run_stored_think = normalize_think_level(await messages.get_thinking_level(chat_id))
            run_fast_mode_requested = await messages.get_fast_mode(chat_id)
            if not root_turn_uuid and renderer.live is not None:
                root_turn_uuid = str(getattr(renderer.live, "_agent_turn_uuid", "") or getattr(renderer.live, "current_turn_uuid", "") or "")
            prompt_builder = self._build_system_prompt_for_chat
            # Keep compatibility with focused tests/integrations that replace the
            # historical no-argument builder while production passes the owning
            # conversation explicitly for folder inheritance.
            if "conversation_uuid" in inspect.signature(prompt_builder).parameters:
                system_live = await prompt_builder(conversation_uuid=conversation_uuid)
            else:
                system_live = await prompt_builder()
            system = await messages.get_or_set_system_snapshot(chat_id, system_live)
            system = effective_context_prompt(system, await conversation_strategy(self.db, conversation_uuid, self.config.context_management.default_strategy))
            # The live render is only a candidate; the frozen value below is the
            # actual system input for this run. Never log either prompt body.
            log.info(
                "web.system_prompt.selected", chat_id=chat_id,
                conversation_uuid=conversation_uuid,
                effective_sha256=system_prompt_sha256(system),
                live_sha256=system_prompt_sha256(system_live),
                matches_live=system == system_live,
                effective_chars=len(system),
            )
            history = await self._build_history(chat_id)

            # Resolve the immutable per-run model identity before accepting the
            # current user row. A private checkpoint anchor covers exactly the
            # prior durable transcript; the rich current user message is appended
            # only after that checkpoint has been safely restored.
            backend, model_id, max_tokens = self.llm_factory.backend_for(model_label)
            backend_protocol = str(getattr(backend, "protocol", "") or "").lower()
            model_meta = self.config.models.resolve(model_label)
            service_tier = ""
            fast_request: dict[str, Any] = {"body": {}, "headers": {}}
            fast_active = False
            if model_meta:
                _provider_def, model_def = model_meta
                levels = list(normalize_think_levels(model_def.thinking_levels))
                think_level = run_stored_think if run_stored_think in levels else (configured_default_think_level(levels, model_def.default_thinking_level) if levels else "off")
                if run_fast_mode_requested and model_def.supports_fast:
                    if model_def.fast_request is not None:
                        # A confirmed models.dev Fast mode may intentionally have
                        # no extras; its presence still selects Fast accounting.
                        fast_request = model_def.fast_request.model_dump(mode="json")
                        fast_active = True
                    else:
                        # Preserve existing manually configured Fast channels until
                        # their confirmed metadata sync supplies Fast request data.
                        service_tier = fast_request_mode(_provider_def, model_def)
                        fast_active = bool(service_tier)
                # Keep both tables for response-time billing: xAI may accept a
                # Priority request but explicitly return ``service_tier=default``.
                base_model_cost = dict(model_def.cost or {})
                fast_model_cost = dict(model_def.fast_cost or {})
                model_cost = fast_model_cost if fast_active and fast_model_cost else base_model_cost
            else:
                think_level = "off"
            ctx_window = self.llm_factory.context_window(model_label)

            # The existing private sidecar is protocol-neutral for ordinary neutral
            # messages. Responses additionally carries opaque items, while Chat and
            # Anthropic use the same anchored context to retain runtime-only state.
            private_state = await messages.load_controller_model_context(
                chat_id,
                conversation_uuid=conversation_uuid,
                session_id=session_id,
                protocol=backend_protocol,
                model=model_id,
                model_label=model_label,
            )
            raw_private_messages = list((private_state or {}).get("messages") or [])
            if raw_private_messages:
                # load_controller_model_context has already verified that this private
                # checkpoint matches the independently persisted window and transcript.
                # A new window invalidates the older native prefix; neutral selection
                # remains available across model changes and later root turns.
                if (
                    all(isinstance(item, dict) for item in raw_private_messages)
                    and validate_model_context(raw_private_messages)
                ):
                    history = deserialize_messages(raw_private_messages)
                else:
                    await messages.clear_controller_model_context(chat_id)

            if conversation_uuid and task_notification:
                history = await restore_controller_run_inputs(
                    messages, chat_id, history, session_uuid=session_id,
                    run_root_turn_uuid=root_turn_uuid, reference_store=self._reference_store(),
                )

            if conversation_uuid and not task_notification:
                # A real user message, not a timer/reconnect/duplicate callback,
                # takes ownership of results paused after a terminal model failure.
                paused = await claim_paused_notifications(self.db, conversation_uuid)
                for item in paused:
                    controller_notification_ids.update(notification_ids(item))
                history += await notification_context_messages(
                    self.db, chat_id=chat_id, conversation_uuid=conversation_uuid,
                    turn_uuid=root_turn_uuid, payloads=paused, history=history,
                )

            visible_user_text = (user_text or "").strip() or ("请根据我发送的附件内容回答。" if media else "")
            llm_text = build_llm_text_with_media(user_text, media or [])
            if background_control_payload:
                llm_text += (
                    "\n\n<background-agent-control-context>\n"
                    "当前 Web conversation 仍有 detached Rath/Agent task 在后台运行。用户最新消息先视为发给主会话；它可能是在讨论后台任务、要求你判断是否转告某个 Agent，也可能是独立新需求。\n"
                    "处理规则：不要把用户消息无条件转发给 Agent。只有当用户明确要控制后台任务时才用 AgentMessage / AgentStop；如果有多个后台任务且目标不明确，先用一句话向用户确认目标。用户表达停止、撤回、上下文错误、任务不该继续时，用 AgentStop，不要用 OpenBearControl 停止 Agent task。若用户指令实质改变了已批准 Plan 的 objective、scope、约束、证据要求或执行方法，必须先根据 planRuntime.activePlanVersion 调用 AgentPlanDecision(action=request_replan)，成功关闭旧 Plan 的执行权限后再调用 AgentMessage；禁止只发 AgentMessage 而让旧 Plan 继续执行。\n"
                    + json.dumps(background_control_payload, ensure_ascii=False, indent=2, default=str)
                    + "\n</background-agent-control-context>"
                )
            llm_text = f"{llm_text}\n\n[⏰ 当前时间: {now_cn()}]"
            user_msg: Message = {
                "role": "user",
                "content": build_llm_content(llm_text, media or []),
                **({BUNDLE_FIELD: [reference_bundle_id]} if reference_bundle_id else {}),
            }
            # Public history and the current user stay clean. The private model
            # context retains trusted runtime-state messages across physical calls
            # and Web turns through the existing anchored sidecar.
            convo = history + [user_msg]
            task_memory_dao = TaskMemoryDAO(self.db)
            task_memory_epoch = task_memory_runtime_epoch(history)

            async def _refresh_task_memory_request(request_messages: list[Message]) -> list[Message]:
                return await reconcile_task_memory_runtime_state(
                    request_messages,
                    task_memory_dao,
                    conversation_uuid=conversation_uuid,
                    epoch=task_memory_epoch,
                )

            async def _reference_request_overlay(request_messages: list[Message]) -> list[Message]:
                # The shared selector accounts for expanded material before deciding
                # what can be evicted. A separate pre-selection size gate would
                # incorrectly reject a request whose optional history can fit.
                return await self._reference_store().overlay(request_messages, conversation_uuid=conversation_uuid)

            if task_notification and conversation_uuid:
                # The receipt and original are one DB transaction. A notification
                # replay never appends its full body again, including after restart.
                payload = dict(task_notification_payload or {})
                payload.setdefault("content", visible_user_text)
                convo = history + await notification_context_messages(
                    self.db, chat_id=chat_id, conversation_uuid=conversation_uuid,
                    turn_uuid=root_turn_uuid, payloads=notification_items(payload), history=history,
                )
                user_message_id = 0
            elif conversation_uuid:
                user_message_id = await self._persist_web_transcript_message(
                    messages,
                    chat_id,
                    "user",
                    visible_user_text,
                    conversation_uuid=conversation_uuid,
                    turn_uuid=root_turn_uuid,
                    run_root_turn_uuid=root_turn_uuid,
                    op_ids=[user_op_id] if user_op_id else None,
                    tokens=estimate_tokens(visible_user_text),
                )
            else:
                user_message_id = await messages.add(
                    chat_id,
                    "user",
                    visible_user_text,
                    tokens=estimate_tokens(visible_user_text),
                )
            mark_source(user_msg, kind="notification" if task_notification else "human",
                        source_id=f"message:{user_message_id}", message_id=user_message_id,
                        turn_uuid=root_turn_uuid, run_root_turn_uuid=root_turn_uuid)
            if not task_notification:
                await messages.bump_user_turn(chat_id)
            user_saved = True
            if conversation:
                if not task_notification:
                    await self._maybe_title_web_conversation(conversation, visible_user_text)
                await self._touch_web_conversation(
                    conversation_uuid,
                    status="running",
                    current_status="Agent 结果汇总中" if task_notification else ("后台 Agent 控制中" if background_control_payload else "运行中"),
                    last_error="",
                )

            agent = Agent(
                backend, self.tools,
                max_run_wall_seconds=self.config.agent.max_run_wall_seconds,
                no_progress_rounds=self.config.agent.no_progress_rounds,
                tool_result_max_chars=max_tool_result_chars(
                    ctx_window, self.config.tools.tool_result_max_chars,
                ),
                max_retries=self.config.agent.max_retries,
                retry_backoff_s=self.config.agent.retry_backoff_s,
                retry_max_delay_s=self.config.agent.retry_max_delay_s,
                retry_jitter_ratio=self.config.agent.retry_jitter_ratio,
                empty_response_retry_limit=self.config.agent.empty_response_retry_limit,
                reasoning_only_retry_limit=self.config.agent.reasoning_only_retry_limit,
            )
            async def _rewrite_assistant_artifacts(content: str) -> str:
                if not conversation_uuid or not hasattr(self, "_rewrite_web_artifact_links"):
                    return content
                return await self._rewrite_web_artifact_links(content, conversation=conversation)

            async def _write_web_message(
                role: str,
                content: str,
                message_kwargs: dict[str, Any],
                meta: dict[str, Any],
            ) -> int:
                turn = str(meta.get("turnUuid") or root_turn_uuid or "")
                op_ids = [str(item or "") for item in (meta.get("opIds") or []) if str(item or "")]
                if role == "assistant":
                    live = renderer.live
                    segment = int(getattr(live, "_assistant_segment", 0) or 0) if live is not None else 0
                    assistant_turn = str(
                        (getattr(live, "_agent_turn_uuid", "") if live is not None else "")
                        or (getattr(live, "current_turn_uuid", "") if live is not None else "")
                        or turn
                    )
                    if assistant_turn:
                        op_ids.append(f"assistant:{assistant_turn}:{segment}")
                    for tool_id in meta.get("toolCallIds") or []:
                        tool_key = str(tool_id or "").strip()
                        if tool_key:
                            op_ids.extend([f"tool:{tool_key}", f"agent:{tool_key}"])
                elif role == "tool":
                    tool_key = str(meta.get("toolCallId") or "").strip()
                    if tool_key:
                        op_ids.extend([f"tool:{tool_key}", f"agent:{tool_key}", f"agent_control:{tool_key}"])
                receipt_ids: set[str] = set()
                if role == "tool" and controller_notification_ids:
                    try:
                        parsed = json.loads(content)
                    except (TypeError, ValueError):
                        parsed = {}
                    if isinstance(parsed, dict):
                        for item in parsed.get("notifications") or []:
                            if isinstance(item, dict):
                                receipt_ids.update(str(value) for value in item.get("notificationUuids", []))
                    receipt_ids &= controller_notification_ids
                return await self._persist_web_transcript_message(
                    messages,
                    chat_id,
                    role,
                    content,
                    conversation_uuid=conversation_uuid,
                    turn_uuid=turn,
                    run_root_turn_uuid=root_turn_uuid or turn,
                    op_ids=op_ids,
                    binding_meta=meta,
                    notification_receipt_ids=receipt_ids,
                    **message_kwargs,
                )

            persister = _WebDBPersister(
                messages,
                chat_id,
                session_uuid=session_id,
                conversation_uuid=conversation_uuid,
                protocol=backend_protocol,
                model=model_id,
                model_label=model_label,
                turn_uuid=root_turn_uuid,
                run_root_turn_uuid=root_turn_uuid,
                message_writer=_write_web_message if conversation_uuid else None,
                artifact_rewriter=_rewrite_assistant_artifacts if conversation_uuid else None,
            )

            async def _refresh_window_request(request_messages: list[Message]) -> list[Message]:
                nonlocal task_memory_epoch
                # The manager supplies a replacement copy. Drop obsolete catalog
                # snapshots only at this boundary, not from the live/cache prefix.
                task_memory_epoch = reset_task_memory_runtime_epoch(
                    request_messages, current_epoch=task_memory_epoch,
                )
                return await _refresh_task_memory_request(request_messages)

            async def _on_window_rotated(detail: dict[str, Any]) -> None:
                clear_read_file_state(chat_id, session_id, agent_session_uuid="", task_uuid="",
                                      store=self.tools.file_state)
                await renderer.emit({"type": "context_compaction", **detail})

            async def _summary_model_call(call: dict[str, Any]) -> None:
                label = str(call.get("model") or model_label)
                resolved = self.config.models.resolve(label)
                committed = await self._persist_web_model_call_delta(
                    messages, chat_id, session_uuid=session_id, call=call,
                    model_cost=resolved[1].cost if resolved else {}, model_label=label,
                    protocol=str(call.get("protocol") or ""), think_level="off", call_kind="context_compaction",
                )
                result.controller_cost_usd += committed
                result.call_time_ms_sum += int(call.get("totalTimeMs") or 0)
                result.output_tokens_sum += int(call.get("outputTokens") or 0)
                if isinstance(call.get("usage"), Usage):
                    result.usage.merge(call["usage"])
                result.model_calls += 1
                result.model_ok += int(call.get("status") == "ok")
                # A failed candidate may be recovered by the next summary model.
                # Keep its physical failure visible without failing the root turn.
                result.summary_model_fail += int(call.get("status") != "ok")

            async def _compression_state(detail: dict[str, Any]) -> None:
                await renderer.emit({"type": "context_compaction", **detail})

            window_runtime = ContextManager(
                WindowStore(self.db, ContextOwner.controller(
                    chat_id=chat_id, session_uuid=session_id, conversation_uuid=conversation_uuid)),
                WindowPolicy(context_window=ctx_window,
                             trigger_tokens=(int(self.config.models.resolve(model_label)[1].rollover_trigger_tokens or 0)
                                             if self.config.models.resolve(model_label) else 0),
                             trigger_ratio=self.config.agent.compact_ratio,
                             retain_ratio=self.config.context_management.retain_ratio,
                             max_output_tokens=max_tokens),
                backend=backend, model=model_id, model_label=model_label,
                on_rotated=_on_window_rotated, on_state=_compression_state,
                active_run_root_turn_uuid=root_turn_uuid,
                strategy_resolver=lambda: conversation_strategy(self.db, conversation_uuid, self.config.context_management.default_strategy),
                strategies={"model_summary": ModelSummaryStrategy(self.config, self.llm_factory, model_label, on_model_call=_summary_model_call)},
            )
            persister.window_runtime = window_runtime

            def _footer_provider(res: RunResult) -> str:
                if not self.config.ui.show_turn_stats:
                    return ""
                return build_turn_stats_card(
                    res,
                    model=model_label,
                    think_level=think_level,
                    cost_usd=res.controller_cost_usd,
                    halted_reason=res.halted_reason,
                    context_window=ctx_window,
                )

            async def _ledger_usage() -> dict[str, Any]:
                # Read-only absolute total. Do not call usage_totals here: it runs
                # ensure_session/commit and turns a display refresh into a writer.
                cur = await self.db.conn.execute(
                    """
                    SELECT usage_input_tokens, usage_output_tokens,
                           usage_cache_read_tokens, usage_cache_write_tokens, usage_cost_usd,
                           COALESCE((
                               SELECT MAX(model_call.id) FROM model_calls AS model_call
                               WHERE model_call.chat_id=ledger_session.chat_id
                                 AND model_call.session_uuid=COALESCE(ledger_session.session_uuid, '')
                           ), 0) AS ledger_revision
                    FROM sessions AS ledger_session WHERE chat_id=?
                    """,
                    (chat_id,),
                )
                try:
                    row = await cur.fetchone()
                finally:
                    await cur.close()
                return {
                    "ledgerRevision": max(0, int((row["ledger_revision"] if row else 0) or 0)),
                    "inputTokens": max(0, int((row["usage_input_tokens"] if row else 0) or 0)),
                    "outputTokens": max(0, int((row["usage_output_tokens"] if row else 0) or 0)),
                    "cacheReadTokens": max(0, int((row["usage_cache_read_tokens"] if row else 0) or 0)),
                    "cacheWriteTokens": max(0, int((row["usage_cache_write_tokens"] if row else 0) or 0)),
                    "costUsd": max(0.0, float((row["usage_cost_usd"] if row else 0.0) or 0.0)),
                }

            async def _task_notification_cb(payload: dict[str, Any]) -> None:
                # Persist first, then offer the durable fact to the live same-root
                # controller.  The post-turn worker remains a fallback, but it must
                # never be the only path while AgentWait is holding the root run.
                notification_kind = str((payload or {}).get("kind") or "")
                if notification_kind.startswith("task-notification") or notification_kind == "plan-approval-required":
                    queued = await self._persist_web_task_notification(conversation, dict(payload or {}))
                    if queued is None:
                        return
                    item = dict(queued)
                    self._enqueue_web_task_notification(
                        conversation_uuid,
                        chat_id,
                        int((conversation or {}).get("owner_chat_id") or 0),
                        item,
                    )
                    await self._offer_web_task_notification_to_controller(
                        conversation,
                        item,
                        root_turn_uuid=root_turn_uuid,
                    )
                    return
                await self._schedule_web_task_notification(conversation, payload)

            async def _publish_live_stats() -> None:
                while True:
                    await asyncio.sleep(0.5)
                    ledger_usage = await _ledger_usage()
                    await renderer.emit({
                        "type": "stats",
                        "stats": self._run_stats_json(
                            result,
                            cost_usd=result.controller_cost_usd,
                            model=model_label,
                            think_level=think_level,
                            context_window=ctx_window,
                            live=True,
                            ledger_usage=ledger_usage,
                        ),
                    })

            stats_task = asyncio.create_task(_publish_live_stats(), name=f"web-live-stats-{conversation_uuid[:8] or chat_id}")

            controller_wake = asyncio.Event()
            if conversation_uuid:
                self._web_controller_wake_events[conversation_uuid] = controller_wake
                self._web_controller_notifications.setdefault(conversation_uuid, [])

            last_agent_review: dict[str, dict[str, Any]] = {}

            async def _scoped_agent_tasks(*, include_terminal: bool = True) -> list[Any]:
                if self.rath_dao is None:
                    return []
                tasks = await self.rath_dao.list_tasks(chat_id=chat_id, limit=200)
                tasks = [task for task in tasks if str(getattr(task, "parent_session_uuid", "") or "") == conversation_uuid]
                tasks = await self._filter_tasks_for_root_turn(conversation_uuid, tasks, root_turn_uuid)
                if include_terminal:
                    return tasks
                terminal = {"completed", "failed", "cancelled", "interrupted"}
                return [task for task in tasks if str(getattr(task, "status", "") or "") not in terminal]

            async def _aggregate_agent_snapshot(
                *,
                wake_reason: str,
                notifications: list[dict[str, Any]],
            ) -> dict[str, Any]:
                tasks = await _scoped_agent_tasks(include_terminal=True)
                snapshots: list[dict[str, Any]] = []
                next_review_state: dict[str, dict[str, Any]] = {}
                terminal_statuses = {"completed", "failed", "cancelled", "interrupted"}
                counts = {"running": 0, "waitingControl": 0, "terminal": 0, "total": len(tasks)}
                for task in sorted(tasks, key=lambda item: int(getattr(item, "id", 0) or 0)):
                    task_uuid = str(getattr(task, "task_uuid", "") or "")
                    status = str(getattr(task, "status", "") or "")
                    # AgentWait owns the durable terminal snapshot for same-root
                    # detached Agents. Merge their exact Rath counters here; the
                    # task UUID set also deduplicates Agent tool-result accounting.
                    self._merge_agent_task_stats(result, task, status=status, task_uuid=task_uuid)
                    previous = last_agent_review.get(task_uuid, {})
                    after_seq = int(previous.get("lastEventSeq") or 0)
                    events = await self.rath_dao.events(task_uuid, after_seq=after_seq, limit=50)
                    if not events and after_seq <= 0:
                        events = await self.rath_dao.events(task_uuid, limit=8)
                    last_event_seq = max([int(getattr(event, "seq", 0) or 0) for event in events] + [after_seq])
                    previous_status = str(previous.get("status") or "")
                    previous_current = str(previous.get("currentStatus") or "")
                    if status == "needs_openbear_control":
                        counts["waitingControl"] += 1
                    elif status in terminal_statuses:
                        counts["terminal"] += 1
                    else:
                        counts["running"] += 1
                    plan_runtime: dict[str, Any] = {}
                    cur = await self.db.conn.execute(
                        """
                        SELECT phase, active_plan_version, pending_plan_version, current_step_id,
                               approval_cycle, revision_count, last_controller_guidance
                        FROM rath_task_plan_state WHERE task_uuid=?
                        """,
                        (task_uuid,),
                    )
                    plan_row = await cur.fetchone()
                    if plan_row is not None:
                        plan_runtime = {
                            "phase": str(plan_row["phase"] or ""),
                            "activePlanVersion": int(plan_row["active_plan_version"] or 0),
                            "pendingPlanVersion": int(plan_row["pending_plan_version"] or 0),
                            "currentStepId": str(plan_row["current_step_id"] or ""),
                            "approvalCycle": int(plan_row["approval_cycle"] or 0),
                            "revisionCount": int(plan_row["revision_count"] or 0),
                            "controllerGuidance": str(plan_row["last_controller_guidance"] or ""),
                        }
                        visible_version = int(
                            plan_runtime["pendingPlanVersion"] or plan_runtime["activePlanVersion"] or 0
                        )
                        cur = await self.db.conn.execute(
                            """
                            SELECT plan_json, plan_type, status
                            FROM rath_task_plan_versions
                            WHERE task_uuid=? AND version=?
                            """,
                            (task_uuid, visible_version),
                        )
                        version_row = await cur.fetchone()
                        visible_plan: dict[str, Any] = {}
                        if version_row is not None:
                            try:
                                parsed_plan = json.loads(str(version_row["plan_json"] or "{}"))
                            except Exception:
                                parsed_plan = {}
                            if isinstance(parsed_plan, dict):
                                visible_plan = parsed_plan
                            visible_plan = {
                                **visible_plan,
                                "version": visible_version,
                                "planType": str(version_row["plan_type"] or "initial"),
                                "status": str(version_row["status"] or ""),
                            }
                        # The controller needs the complete Plan exactly at the
                        # approval boundary. Routine progress snapshots stay
                        # compact and carry only current steps/criteria below.
                        if plan_runtime["pendingPlanVersion"]:
                            plan_runtime["plan"] = visible_plan
                        cur = await self.db.conn.execute(
                            """
                            SELECT step_id, status, criteria_state_json FROM rath_task_plan_step_runs
                            WHERE task_uuid=? AND plan_version=? ORDER BY id ASC
                            """,
                            (task_uuid, visible_version),
                        )
                        plan_runtime["steps"] = []
                        for step_row in await cur.fetchall():
                            try:
                                criteria_state = json.loads(str(step_row["criteria_state_json"] or "{}"))
                            except Exception:
                                criteria_state = {}
                            plan_runtime["steps"].append({
                                "stepId": str(step_row["step_id"] or ""),
                                "status": str(step_row["status"] or ""),
                                "criteriaState": criteria_state if isinstance(criteria_state, dict) else {},
                            })
                        step_status = {
                            str(item.get("stepId") or ""): str(item.get("status") or "")
                            for item in plan_runtime["steps"]
                        }
                        for step in visible_plan.get("steps") or []:
                            if not isinstance(step, dict):
                                continue
                            step_id = str(step.get("id") or "")
                            matching = next(
                                (item for item in plan_runtime["steps"] if item["stepId"] == step_id),
                                None,
                            )
                            if matching is not None:
                                matching["title"] = str(step.get("title") or "")
                                matching["required"] = bool(step.get("required", True))
                                matching["criteria"] = list(step.get("criteria") or [])
                        plan_runtime["completedSteps"] = sum(
                            1 for status_value in step_status.values() if status_value == "completed"
                        )
                        plan_runtime["remainingSteps"] = sum(
                            1
                            for step in visible_plan.get("steps") or []
                            if isinstance(step, dict)
                            and step_status.get(str(step.get("id") or ""), "pending") != "completed"
                        )
                    snapshots.append({
                        "taskUuid": task_uuid,
                        "title": getattr(task, "title", "") or "",
                        "status": status,
                        "currentStatus": getattr(task, "current_status", "") or "",
                        "updatedAt": int(getattr(task, "updated_at", 0) or 0),
                        "hasMeaningfulProgress": bool(
                            events
                            or status != previous_status
                            or str(getattr(task, "current_status", "") or "") != previous_current
                        ),
                        "planRuntime": plan_runtime,
                        # Full terminal/control output is carried exactly once by
                        # the newly claimed notification payload below. Keeping it
                        # in every status snapshot would re-inject old conclusions
                        # after a later Agent generation and double the batch size.
                        "result": {},
                        "recentEvents": [
                            {
                                "seq": event.seq,
                                "kind": event.kind,
                                "summary": event.summary,
                                "detail": {
                                    key: event.detail[key]
                                    for key in (
                                        "stepId", "criterionId", "controlUuid", "status", "reason",
                                        "planImpact", "nextAction", "planVersion", "action",
                                    )
                                    if isinstance(event.detail, dict) and key in event.detail
                                },
                            }
                            for event in events
                        ],
                    })
                    next_review_state[task_uuid] = {
                        "lastEventSeq": last_event_seq,
                        "status": status,
                        "currentStatus": str(getattr(task, "current_status", "") or ""),
                    }
                last_agent_review.clear()
                last_agent_review.update(next_review_state)
                deduped_notifications = self._dedupe_web_task_notifications(notifications)
                result_budgets = [self._task_notification_result_budget(item) for item in deduped_notifications]
                controller_notifications = [
                    self._controller_task_notification(item) for item in deduped_notifications
                ]
                return {
                    "wakeReason": wake_reason,
                    "rootTurnUuid": root_turn_uuid,
                    "summary": counts,
                    "agents": snapshots,
                    "notifications": controller_notifications,
                    "resultOutputTokens": sum(tokens for tokens, _count in result_budgets),
                    "resultCount": sum(count for _tokens, count in result_budgets),
                }

            async def _agent_wait(wait_request: dict[str, Any]) -> str:
                if not conversation_uuid or self.rath_dao is None:
                    return json.dumps({"ok": False, "error": "agent_wait_not_supported"}, ensure_ascii=False)
                mode = str(wait_request.get("mode") or "event_only")
                review_after_s = float(wait_request.get("reviewAfterSeconds") or 0.0)
                reason = str(wait_request.get("reason") or "")
                # Clear first, then inspect durable queues/tasks. This ordering avoids
                # losing a completion/interruption that races with arming the wait:
                # pre-clear events remain visible in queues; post-clear events keep
                # the asyncio.Event set and wake wait() immediately.
                controller_wake.clear()
                # Reconcile the durable outbox before deciding whether to sleep.
                # This closes the race where a completion was persisted before its
                # in-memory wake edge could be attached to this controller run.
                if not self._web_controller_notifications.get(conversation_uuid):
                    await self._recover_web_task_notifications(conversation_uuid)
                active = await _scoped_agent_tasks(include_terminal=False)
                queued_notifications = self._web_controller_notifications.get(conversation_uuid, [])
                queued_requires_wake = (not active and bool(queued_notifications)) or any(
                    bool(item.get("requiresDecision"))
                    or str(item.get("status") or "")
                    in {"needs_openbear_control", "failed", "cancelled", "interrupted"}
                    for item in queued_notifications
                    if isinstance(item, dict)
                )
                if not active and not queued_notifications:
                    # No active Agent means there is no supervision cycle to show.
                    # Return the durable snapshot to the model without emitting a
                    # second “all completed” operation into the user timeline.
                    snapshot = await _aggregate_agent_snapshot(wake_reason="all_terminal", notifications=[])
                    return json.dumps({
                        "ok": True,
                        "skipped": True,
                        "alreadyTerminal": True,
                        "message": "All scoped Agents are already terminal. Do not call AgentWait again; integrate their results and continue the root task as needed.",
                        **snapshot,
                    }, ensure_ascii=False, default=str)

                # A real wait (active Agent or queued completion/control event) is
                # one immutable supervision cycle. Its start and wake events share
                # this id; later real waits get a new chronological position.
                wait_cycle_uuid = str(uuid.uuid4())
                planned_review_at_ms = int((time.time() + review_after_s) * 1000) if mode == "review_after" and review_after_s > 0 else 0
                status_text = "等待 Agent 事件" if mode == "event_only" else f"计划 {int(review_after_s)} 秒后统一复查 Agent"
                await renderer.emit({
                    "type": "agent_supervision",
                    "runRootTurnUuid": root_turn_uuid,
                    "waitCycleUuid": wait_cycle_uuid,
                    "statusText": status_text,
                    "preview": reason,
                    "status": "running",
                    "active": True,
                    "mode": mode,
                    "reviewAfterSeconds": review_after_s,
                    "plannedReviewAtMs": planned_review_at_ms,
                    "taskCounts": {"running": len(active)},
                })
                await self._touch_web_conversation(conversation_uuid, current_status=status_text)

                wake_reason = "task_notification"
                if not queued_requires_wake and not steering.has_pending(chat_id):
                    if mode == "review_after" and review_after_s > 0:
                        try:
                            await asyncio.wait_for(controller_wake.wait(), timeout=review_after_s)
                        except TimeoutError:
                            wake_reason = "review_due"
                    else:
                        await controller_wake.wait()
                controller_wake.clear()
                if steering.has_pending(chat_id):
                    wake_reason = "user_interruption"
                elif queued_requires_wake:
                    wake_reason = "task_notification"
                notifications = self._dedupe_web_task_notifications(
                    self._web_controller_notifications.pop(conversation_uuid, [])
                )
                seen_notification_ids = set().union(*(
                    self._web_task_notification_ids(item)
                    for item in notifications
                    if isinstance(item, dict)
                )) if notifications else set()
                _claim_token, claimed_notification_ids = await self._claim_web_task_notifications(notifications)
                # The post-turn worker may hold the same durable UUID in memory, but
                # cannot claim it while this root run is active. Remove that mirror
                # after the DB claim so only one path can invoke the model.
                self._remove_web_task_notification_ids_from_pending(
                    conversation_uuid,
                    seen_notification_ids,
                )
                claimed_notifications: list[dict[str, Any]] = []
                for item in notifications:
                    if not isinstance(item, dict):
                        continue
                    item_ids = self._web_task_notification_ids(item) & claimed_notification_ids
                    if not item_ids:
                        continue
                    claimed_item = dict(item)
                    claimed_item["_notificationUuids"] = sorted(item_ids)
                    if str(claimed_item.get("_notificationUuid") or "") not in item_ids:
                        claimed_item["_notificationUuid"] = sorted(item_ids)[0]
                    claimed_notifications.append(claimed_item)
                    controller_notification_ids.update(item_ids)
                notifications = self._dedupe_web_task_notifications(claimed_notifications)
                snapshot = await _aggregate_agent_snapshot(wake_reason=wake_reason, notifications=notifications)
                summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
                if int(summary.get("running") or 0) == 0 and int(summary.get("waitingControl") or 0) == 0:
                    status_text = "全部 Agent 已完成"
                    status_value = "completed"
                    active_value = False
                elif wake_reason == "user_interruption":
                    status_text = "用户插话，已提前唤醒"
                    status_value = "completed"
                    active_value = False
                elif wake_reason == "review_due":
                    status_text = "Agent 统一复查时间已到"
                    status_value = "completed"
                    active_value = False
                else:
                    status_text = "Agent 关键事件已唤醒主控"
                    status_value = "completed"
                    active_value = False
                await renderer.emit({
                    "type": "agent_supervision",
                    "runRootTurnUuid": root_turn_uuid,
                    "waitCycleUuid": wait_cycle_uuid,
                    "statusText": status_text,
                    "preview": reason,
                    "status": status_value,
                    "active": active_value,
                    "mode": mode,
                    "reviewAfterSeconds": review_after_s,
                    "wakeReason": wake_reason,
                    "taskCounts": summary,
                })
                if wake_reason == "user_interruption":
                    # Real text remains in steering and is persisted/injected by the
                    # normal loop boundary. Return its stable queue id as the formal
                    # user-instruction reference for Plan decisions beyond the
                    # autonomous revision limit.
                    pending_user_items = steering.pending_items(chat_id)
                    user_instruction_ids = [
                        str(item.get("id") or "")
                        for item in pending_user_items
                        if str(item.get("id") or "")
                    ]
                    return json.dumps(
                        {
                            "ok": True,
                            **snapshot,
                            "steeringPending": True,
                            "userInstructionId": user_instruction_ids[-1] if user_instruction_ids else "",
                            "userInstructionIds": user_instruction_ids,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                return json.dumps({"ok": True, **snapshot}, ensure_ascii=False, default=str)

            async def _model_call_hook(call: dict[str, Any]) -> None:
                call_usage = call.get("usage") if isinstance(call.get("usage"), Usage) else Usage()
                resolved_cost = resolved_usage_cost_usd(
                    base_model_cost,
                    call_usage,
                    fast_cost=fast_model_cost,
                    fast_requested=fast_active,
                    actual_service_tier=call.get("serviceTier"),
                    provider_cost_usd=call.get("providerCostUsd"),
                )
                committed_cost = await self._persist_web_model_call_delta(
                    messages,
                    chat_id,
                    session_uuid=session_id,
                    call=call,
                    model_cost=model_cost,
                    model_label=model_label,
                    protocol=backend_protocol,
                    think_level=think_level,
                    cost_usd_override=resolved_cost,
                )
                # Cost tiers are non-linear, so retain the durable amount chosen
                # for this physical request rather than pricing aggregate usage.
                result.controller_cost_usd += committed_cost

            async def _conversation_event_cb(event: dict[str, Any]) -> None:
                if renderer.live is not None:
                    await renderer.live.publish(event, persist=False)

            result = await agent.run(
                convo, renderer, model=model_id, system=system,
                max_tokens=max_tokens,
                # Web 控制台最终态会渲染 reasoning；live 态必须同样接收 reasoning，
                # 否则“先思考 -> 工具调用”的轮次 live 只剩“正在思考”占位。
                show_thinking=True,
                think_level=think_level,
                service_tier=service_tier,
                fast_request=fast_request,
                session_id=session_id,
                persister=persister,
                window_runtime=window_runtime,
                window_request_refresher=_refresh_window_request,
                steer_drain=lambda: steering.drain_items(chat_id),
                model_request_refresher=_refresh_task_memory_request,
                model_request_overlay=_reference_request_overlay,
                model_call_hook=_model_call_hook,
                footer_provider=_footer_provider,
                result=result,
                retry_cancel_check=(
                    (lambda: self.control_actions.consume_retry_cancel(chat_id))
                    if self.control_actions is not None else None
                ),
                tool_context=ToolRuntimeContext(
                    chat_id=chat_id,
                    session_uuid=session_id,
                    conversation_uuid=conversation_uuid,
                    source="web",
                    turn_uuid=root_turn_uuid,
                    run_root_turn_uuid=root_turn_uuid,
                    soft_stop_check=(lambda: self.control_actions.consume_soft_stop(chat_id)) if self.control_actions is not None else None,
                    task_notification=_task_notification_cb,
                    conversation_event=_conversation_event_cb,
                    web_confirm=lambda payload: self._web_confirm(conversation_uuid, payload),
                    agent_wait=_agent_wait,
                ),
            )
            if stats_task is not None:
                stats_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await stats_task
                stats_task = None
            if task_notification:
                self._merge_agent_notification_stats(result, task_notification_payload)
            # Main-controller model calls are committed one-by-one by
            # _model_call_hook.  The epilogue only publishes the already durable
            # total; writing the aggregate again would double-count billing.
            request_cost = result.controller_cost_usd
            ledger_usage = await _ledger_usage()
            await renderer.emit({
                "type": "stats",
                "stats": self._run_stats_json(
                    result, cost_usd=request_cost, model=model_label,
                    think_level=think_level, context_window=ctx_window,
                    ledger_usage=ledger_usage,
                ),
            })
            if self.control_actions is not None:
                await self.control_actions.drain_after_turn(self, chat_id)
                post_turn_actions_drained = True
            if int(result.model_fail or 0) <= 0:
                if controller_notification_ids:
                    resolved_ids, unresolved_ids = await self._partition_web_task_notification_ack(
                        controller_notification_ids
                    )
                    if resolved_ids:
                        await self._mark_web_task_notifications_delivered(resolved_ids)
                    if unresolved_ids:
                        await self._requeue_web_task_notifications(
                            unresolved_ids,
                            "same-root notification boundary remains unresolved after controller turn",
                        )
                    controller_notification_ids.clear()
                turn_succeeded = True
            if conversation_uuid:
                if result.model_fail > 0:
                    live_error = str(getattr(renderer.live, "last_error", "") or "") if renderer.live else ""
                    await self._touch_web_conversation(
                        conversation_uuid,
                        status="error",
                        current_status="出错 · 可发送消息继续" if task_notification or controller_notification_ids else "出错",
                        last_error=live_error or result.halted_reason or "模型调用失败",
                    )
                else:
                    await self._touch_web_conversation(conversation_uuid, status="idle", current_status="就绪", last_error="")
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await messages.repair_dangling_tool_calls(chat_id)
            result.stopped = True
            if not result.halted_reason:
                result.halted_reason = "cancelled"
            if result.total_time_ms <= 0 and result.start_monotonic > 0:
                result.total_time_ms = int((time.monotonic() - result.start_monotonic) * 1000)
            if stats_task is not None:
                stats_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await stats_task
                stats_task = None
            # Every completed upstream request was already committed by the
            # per-call hook. Cancellation must not replay the aggregate.
            request_cost = result.controller_cost_usd
            with contextlib.suppress(Exception):
                renderer.set_footer(build_turn_stats_card(
                    result,
                    model=model_label,
                    think_level=think_level,
                    cost_usd=request_cost,
                    halted_reason=result.halted_reason,
                    context_window=ctx_window or self.llm_factory.context_window(model_label),
                ))
            with contextlib.suppress(Exception):
                ledger_usage = await _ledger_usage()
                await renderer.emit({
                    "type": "stats",
                    "stats": self._run_stats_json(
                        result, cost_usd=request_cost, model=model_label,
                        think_level=think_level, context_window=ctx_window or self.llm_factory.context_window(model_label),
                            ledger_usage=ledger_usage,
                    ),
                })
            already_stopped = bool(conversation_uuid and self._web_stop_markers.get(conversation_uuid))
            if not already_stopped:
                await renderer.emit({"type": "stopped"})
            if user_saved and not already_stopped:
                with contextlib.suppress(Exception):
                    if conversation_uuid:
                        live = renderer.live
                        segment = int(getattr(live, "_assistant_segment", 0) or 0) if live is not None else 0
                        assistant_turn = str(
                            (getattr(live, "_agent_turn_uuid", "") if live is not None else "")
                            or root_turn_uuid
                        )
                        await self._persist_web_transcript_message(
                            messages,
                            chat_id,
                            "assistant",
                            "⏹ 已停止",
                            conversation_uuid=conversation_uuid,
                            turn_uuid=root_turn_uuid,
                            run_root_turn_uuid=root_turn_uuid,
                            op_ids=[f"assistant:{assistant_turn}:{segment}"] if assistant_turn else None,
                            tokens=estimate_tokens("⏹ 已停止"),
                        )
                    else:
                        await messages.add(
                            chat_id,
                            "assistant",
                            "⏹ 已停止",
                            tokens=estimate_tokens("⏹ 已停止"),
                        )
            if conversation_uuid:
                await self._touch_web_conversation(conversation_uuid, status="idle", current_status="已停止")
        except Exception as exc:
            log.exception("Web 对话处理异常", 会话=chat_id)
            await renderer.fail(f"❌ 内部错误：{type(exc).__name__}: {exc}")
            if conversation_uuid:
                await self._touch_web_conversation(conversation_uuid, status="error", current_status="出错", last_error=f"{type(exc).__name__}: {exc}")
        finally:
            if self.control_actions is not None:
                self.control_actions.clear_retry_cancel(chat_id)
                # A Web stop may leave a cooperative fallback while hard
                # cancellation is unwinding.  Never leak it into the next turn.
                self.control_actions.consume_soft_stop(chat_id)
            if controller_notification_ids:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._pause_web_task_notifications(
                        controller_notification_ids,
                        "same-root notification turn stopped; awaiting user continuation",
                    ))
                controller_notification_ids.clear()
            if conversation_uuid:
                wake = self._web_controller_wake_events.pop(conversation_uuid, None)
                if wake is not None:
                    wake.set()
                self._web_controller_notifications.pop(conversation_uuid, None)
            if stats_task is not None:
                stats_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await stats_task
            await renderer.close()
            if not post_turn_actions_drained and self.control_actions is not None:
                await self.control_actions.drain_after_turn(self, chat_id)
                post_turn_actions_drained = True
            if conversation_uuid and renderer.live is not None and str(getattr(renderer.live, "status", "") or "") == "idle" and int(result.model_fail or 0) <= 0:
                with contextlib.suppress(Exception):
                    await self._touch_web_conversation(
                        conversation_uuid,
                        status="idle",
                        current_status="就绪" if not result.stopped else "已停止",
                        last_error="",
                    )
            if conversation_uuid and not turn_succeeded:
                with contextlib.suppress(Exception):
                    await self._recover_web_task_notifications(conversation_uuid)
        return turn_succeeded

__all__ = [name for name in globals() if not name.startswith("__")]
