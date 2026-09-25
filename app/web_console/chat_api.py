# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.context.configuration import CONTEXT_STRATEGIES, normalize_strategy
from app.interaction_data import canonical_questionnaire_answers as _canonical_questionnaire_answers
from app.interaction_data import redact_result
from app.references import ReferenceError, effective_reference_text
from app.task_memory import TaskMemoryDAO, task_memory_changed_public_event
from app.web_console.message_visibility import visibility_snapshot
from app.web_console.activity import clear_deleted_completion
from app.web_console.core import *
from app.web_console.live_stream import *


def _confirmation_answer_audit_result(
    action: str, item: dict[str, Any], result: dict[str, Any],
) -> dict[str, Any]:
    if bool(item.get("sensitive") or item.get("secret")):
        return redact_result(result)
    if action == "questionnaire":
        answers = result.get("answers") if isinstance(result.get("answers"), list) else []
        return {
            "status": str(result.get("status") or ""),
            "cancelled": bool(result.get("cancelled")),
            "interactionId": str(result.get("interactionId") or ""),
            "answerCount": len(answers),
            "questionIds": [
                str(answer.get("questionId") or "")
                for answer in answers if isinstance(answer, dict)
            ],
            "answerModes": [
                str(answer.get("answerMode") or "")
                for answer in answers if isinstance(answer, dict)
            ],
        }
    return result


class WebAdminChatHandlersMixin:
    _WEB_DEFAULT_FIELDS = {
        "contextStrategy": "context_strategy",
        "mainModel": "main_model",
        "mainThinkingLevel": "main_thinking_level",
        "mainFastMode": "main_fast_mode",
        "agentModel": "agent_model",
        "agentThinkLevel": "agent_think_level",
        "agentFastMode": "agent_fast_mode",
    }

    def _web_current_default_model(self) -> str:
        candidates = [
            str(getattr(self.model_selection, "current", "") or ""),
            str(self.config.models.primary or ""),
        ]
        for provider_name, provider in self.config.models.providers.items():
            if not provider.enabled:
                continue
            candidates.extend(f"{provider_name}/{model.id}" for model in provider.models)
        return next((label for label in candidates if label and self.config.models.resolve(label)), "")

    def _web_builtin_run_defaults(self) -> dict[str, Any]:
        model = self._web_current_default_model()
        return {
            "main_model": model,
            "main_thinking_level": self._model_default_thinking_level(model),
            "main_fast_mode": 0,
            "agent_model": "",
            "agent_think_level": "",
            "agent_fast_mode": -1,
        }

    def _normalize_web_run_defaults(self, row: dict[str, Any]) -> dict[str, Any]:
        main_model = str(row.get("main_model") or "").strip()
        if not self.config.models.resolve(main_model):
            main_model = self._web_current_default_model()
        main_levels = self._model_thinking_levels(main_model)
        requested_main_think = normalize_think_level(str(row.get("main_thinking_level") or ""))
        main_think = (
            requested_main_think
            if requested_main_think and requested_main_think in main_levels
            else (self._model_default_thinking_level(main_model) if main_levels else "off")
        )
        main_fast = bool(row.get("main_fast_mode")) and self._model_supports_fast(main_model)

        agent_model = str(row.get("agent_model") or "").strip()
        if agent_model and not self.config.models.resolve(agent_model):
            agent_model = ""
        effective_agent_model = agent_model or main_model
        agent_levels = self._model_thinking_levels(effective_agent_model)
        requested_agent_think = normalize_think_level(str(row.get("agent_think_level") or ""))
        agent_think = requested_agent_think if requested_agent_think and requested_agent_think in agent_levels else ""
        try:
            agent_fast_raw = int(row.get("agent_fast_mode") if row.get("agent_fast_mode") is not None else -1)
        except (TypeError, ValueError):
            agent_fast_raw = -1
        if agent_fast_raw not in {-1, 0, 1}:
            agent_fast_raw = -1
        if agent_fast_raw == 1 and not self._model_supports_fast(effective_agent_model):
            agent_fast_raw = -1

        return {
            "contextStrategy": normalize_strategy(row.get("context_strategy"), self.config.context_management.default_strategy),
            "mainModel": main_model,
            "mainThinkingLevel": main_think or "off",
            "mainFastMode": main_fast,
            "agentModel": agent_model,
            "agentThinkLevel": agent_think,
            "agentFastMode": None if agent_fast_raw < 0 else bool(agent_fast_raw),
            "revision": int(row.get("revision") or 0),
            "updatedAt": int(row.get("updated_at") or 0),
        }

    @staticmethod
    def _web_defaults_storage(defaults: dict[str, Any]) -> dict[str, Any]:
        return {
            "context_strategy": normalize_strategy(defaults.get("contextStrategy")),
            "main_model": str(defaults.get("mainModel") or ""),
            "main_thinking_level": str(defaults.get("mainThinkingLevel") or ""),
            "main_fast_mode": 1 if defaults.get("mainFastMode") is True else 0,
            "agent_model": str(defaults.get("agentModel") or ""),
            "agent_think_level": str(defaults.get("agentThinkLevel") or ""),
            "agent_fast_mode": -1 if defaults.get("agentFastMode") is None else (1 if defaults.get("agentFastMode") is True else 0),
        }

    def _apply_folder_run_defaults(self, defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        if not overrides:
            return dict(defaults)
        selected = dict(defaults)
        for key, value in overrides.items():
            # A removed/disabled model cannot become an executable default. Fall
            # back to the remembered configuration, while properties still show
            # the stored value so the user can repair it.
            if key in {"mainModel", "agentModel"} and value and not self.config.models.resolve(str(value)):
                continue
            selected[key] = value
        return self._normalize_web_run_defaults({
            **self._web_defaults_storage(selected),
            "revision": defaults.get("revision", 0), "updated_at": defaults.get("updatedAt", 0),
        })

    async def _web_run_defaults(self, owner_chat_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        row = await WebConversationDefaultsDAO(self.db).get_or_seed(
            owner_chat_id,
            self._web_builtin_run_defaults(),
        )
        return row, self._normalize_web_run_defaults(row)

    async def _web_run_defaults_candidate(self, owner_chat_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        row = await WebConversationDefaultsDAO(self.db).candidate(
            owner_chat_id,
            self._web_builtin_run_defaults(),
        )
        return row, self._normalize_web_run_defaults(row)

    def _validate_web_defaults_patch(
        self,
        body: dict[str, Any],
        current: dict[str, Any],
        *,
        require_complete: bool = False,
    ) -> tuple[dict[str, Any] | None, tuple[str, int] | None]:
        unknown = set(body) - set(self._WEB_DEFAULT_FIELDS)
        if unknown:
            return None, ("invalid_defaults_field", 400)
        if require_complete and not (set(self._WEB_DEFAULT_FIELDS) - {"contextStrategy"}).issubset(body):
            return None, ("run_config_incomplete", 400)
        if not body:
            return None, ("nothing_to_update", 400)

        merged = dict(current)
        for key, value in body.items():
            if key == "contextStrategy":
                if not isinstance(value, str) or value not in CONTEXT_STRATEGIES:
                    return None, ("invalid_context_strategy", 400)
                merged[key] = value
            elif key in {"mainModel", "mainThinkingLevel", "agentModel", "agentThinkLevel"}:
                if not isinstance(value, str):
                    return None, ("invalid_defaults_type", 400)
                merged[key] = value.strip()
            elif key in {"mainFastMode"}:
                if not isinstance(value, bool):
                    return None, ("invalid_defaults_type", 400)
                merged[key] = value
            elif key == "agentFastMode":
                if value is not None and not isinstance(value, bool):
                    return None, ("invalid_defaults_type", 400)
                merged[key] = value

        main_model = str(merged.get("mainModel") or "")
        if not main_model:
            return None, ("model_required", 400)
        if self.config.models.resolve(main_model) is None:
            return None, ("model_not_found", 404)
        main_levels = self._model_thinking_levels(main_model)
        main_think = normalize_think_level(str(merged.get("mainThinkingLevel") or ""))
        if "mainThinkingLevel" in body:
            if (main_levels and main_think not in main_levels) or (not main_levels and main_think not in {None, "off"}):
                return None, ("invalid_thinking_level", 400)
        elif "mainModel" in body and main_think not in main_levels:
            main_think = self._model_default_thinking_level(main_model) if main_levels else "off"
        merged["mainThinkingLevel"] = main_think or (self._model_default_thinking_level(main_model) if main_levels else "off")
        if merged.get("mainFastMode") is True and not self._model_supports_fast(main_model):
            if "mainFastMode" in body:
                return None, ("fast_not_supported", 400)
            merged["mainFastMode"] = False

        agent_model = str(merged.get("agentModel") or "")
        if agent_model and self.config.models.resolve(agent_model) is None:
            return None, ("model_not_found", 404)
        effective_agent_model = agent_model or main_model
        agent_levels = self._model_thinking_levels(effective_agent_model)
        agent_think_text = str(merged.get("agentThinkLevel") or "")
        agent_think = normalize_think_level(agent_think_text)
        if "agentThinkLevel" in body:
            if agent_think_text and (not agent_think or agent_think not in agent_levels):
                return None, ("invalid_thinking_level", 400)
        elif ("agentModel" in body or ("mainModel" in body and not agent_model)) and agent_think not in agent_levels:
            agent_think = None
        merged["agentThinkLevel"] = agent_think or ""
        if merged.get("agentFastMode") is True and not self._model_supports_fast(effective_agent_model):
            if "agentFastMode" in body:
                return None, ("fast_not_supported", 400)
            merged["agentFastMode"] = None

        storage = self._web_defaults_storage(merged)
        fields = set(body)
        if "mainModel" in body:
            fields.update({"mainThinkingLevel", "mainFastMode"})
            if not agent_model:
                fields.update({"agentThinkLevel", "agentFastMode"})
        if "agentModel" in body:
            fields.update({"agentThinkLevel", "agentFastMode"})
        validated = {self._WEB_DEFAULT_FIELDS[key]: storage[self._WEB_DEFAULT_FIELDS[key]] for key in fields}
        return validated, None

    def _web_assistant_artifact_rewriter(self, row: dict[str, Any], *, turn_uuid: str = ""):
        async def rewrite(content: str) -> str:
            conv_uuid = str(row.get("conversation_uuid") or "")
            if not conv_uuid or not hasattr(self, "_rewrite_web_artifact_links"):
                return content
            return await self._rewrite_web_artifact_links(
                content,
                conversation=row,
                turn_uuid=turn_uuid,
            )
        return rewrite

    async def _conversation_from_request(self, request: web.Request) -> dict[str, Any]:
        session: WebSession = request[_WEB_SESSION_KEY]
        conv_uuid = str(request.match_info.get("conversation_uuid") or "").strip()
        if not conv_uuid:
            return await self._ensure_default_web_conversation(session.chat_id)
        return await self._conversation_row(session.chat_id, conv_uuid, require=True)  # type: ignore[return-value]

    @staticmethod
    def _timeline_page_query(request: web.Request) -> tuple[int | None, int | None]:
        """Parse the opt-in timeline cursor without changing legacy requests."""
        has_limit = "timelineLimit" in request.query
        has_before = "beforeDisplaySeq" in request.query
        if not has_limit and not has_before:
            return None, None
        try:
            limit = int(request.query.get("timelineLimit") or 200)
            before = int(request.query.get("beforeDisplaySeq") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_timeline_cursor") from exc
        if limit < 1 or limit > 1000 or before < 0:
            raise ValueError("invalid_timeline_cursor")
        return limit, before or None

    async def handle_api_conversations(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        include_archived = str(request.query.get("includeArchived") or "").strip().lower() in {"1", "true", "yes", "on"}
        items = await self._list_web_conversations(session.chat_id, include_archived=include_archived)
        active = next((x for x in items if x.get("kind") != "archive"), None)
        return web.json_response({
            "ok": True,
            "items": items,
            "activeConversationUuid": (active or items[0])["conversationUuid"] if items else "",
        })

    async def handle_api_conversation_defaults(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        folder_id = str(request.query.get("folderId") or "").strip()
        folder_defaults = await self._tree_folder_run_defaults(session.chat_id, folder_id)
        _row, defaults = await self._web_run_defaults(session.chat_id)
        return web.json_response({
            "ok": True, "defaults": self._apply_folder_run_defaults(defaults, folder_defaults),
            "folderId": folder_id, "folderDefaults": folder_defaults,
        })

    async def handle_api_conversation_defaults_patch(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        if "contextStrategy" in body:
            return web.json_response({"ok": False, "error": "use_system_context_default"}, status=400)
        # Serialize validation + partial write so dependency checks (for example
        # Fast support after a concurrent model change) observe server commit order.
        async with self._web_conversation_create_lock:
            _row, current = await self._web_run_defaults_candidate(session.chat_id)
            updates, error = self._validate_web_defaults_patch(body, current)
            if error:
                code, status = error
                return web.json_response({"ok": False, "error": code}, status=status)
            stored = await WebConversationDefaultsDAO(self.db).patch_or_seed(
                session.chat_id,
                updates or {},
                self._web_builtin_run_defaults(),
            )
        defaults = self._normalize_web_run_defaults(stored)
        await self.audit(
            "web.conversation.defaults",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"fields": sorted(body), "revision": defaults["revision"]},
        )
        return web.json_response({"ok": True, "defaults": defaults})

    async def handle_api_conversation_create(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        title_raw = body.get("title", "新对话")
        if not isinstance(title_raw, str):
            return web.json_response({"ok": False, "error": "invalid_title_type"}, status=400)
        title = title_raw.strip() or "新对话"
        folder_uuid = str(body.get("folderId") or "").strip()
        folder_defaults = await self._tree_folder_run_defaults(session.chat_id, folder_uuid)
        _defaults_row, global_defaults = await self._web_run_defaults_candidate(session.chat_id)
        current = self._apply_folder_run_defaults(global_defaults, folder_defaults)

        run_config_raw = body.get("runConfig")
        persist_defaults = not folder_defaults and (run_config_raw is not None or "model" in body)
        if run_config_raw is not None:
            if not isinstance(run_config_raw, dict):
                return web.json_response({"ok": False, "error": "invalid_run_config_type"}, status=400)
            updates, error = self._validate_web_defaults_patch(run_config_raw, current, require_complete=True)
            if error:
                code, status = error
                return web.json_response({"ok": False, "error": code}, status=status)
            selected = dict(current)
            selected.update(run_config_raw)
            normalized = self._normalize_web_run_defaults({**self._web_defaults_storage(selected), "revision": 0, "updated_at": 0})
        else:
            selected = dict(current)
            if "model" in body:
                if not isinstance(body.get("model"), str):
                    return web.json_response({"ok": False, "error": "invalid_model_type"}, status=400)
                model = str(body.get("model") or "").strip()
                if not model:
                    return web.json_response({"ok": False, "error": "model_required"}, status=400)
                if self.config.models.resolve(model) is None:
                    return web.json_response({"ok": False, "error": "model_not_found"}, status=404)
                selected["mainModel"] = model
                selected["mainThinkingLevel"] = self._model_default_thinking_level(model)
                selected["mainFastMode"] = False
                persist_defaults = not folder_defaults
            normalized = self._normalize_web_run_defaults({**self._web_defaults_storage(selected), "revision": 0, "updated_at": 0})

        storage = self._web_defaults_storage(normalized)
        row = await self._create_web_conversation(
            session.chat_id,
            title=title,
            model=normalized["mainModel"],
            run_config=storage,
            folder_uuid=folder_uuid,
            persist_defaults=persist_defaults,
            defaults_seed=self._web_defaults_storage(global_defaults) if folder_defaults else None,
        )
        live = self._live_for(row)
        await self.audit("web.conversation.create", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row["conversation_uuid"], "internalChatId": row["internal_chat_id"]})
        return web.json_response({"ok": True, "conversation": self._web_conversation_json(row, live=live), "state": await self._chat_payload(int(row["internal_chat_id"]), row)})

    async def handle_api_conversation_state(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        try:
            timeline_limit, before_display_seq = self._timeline_page_query(request)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response(await self._chat_payload(
            int(row["internal_chat_id"]),
            row,
            timeline_limit=timeline_limit,
            before_display_seq=before_display_seq,
        ))

    async def handle_api_conversation_operations(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        await self._reconcile_inactive_web_conversation_operations(row, source="conversation_operations_reconcile")
        conv_uuid = str(row.get("conversation_uuid") or "")
        try:
            timeline_limit, before_display_seq = self._timeline_page_query(request)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        if timeline_limit is None:
            operations = await self._web_operations(conv_uuid, include_tool_details=False)
            page = {
                "hasMoreBefore": False,
                "nextBeforeDisplaySeq": None,
                "timelineLimit": None,
                "beforeDisplaySeq": None,
            }
        else:
            async with self._web_operation_lock(conv_uuid):
                operations, page = await self._web_operations_page(
                    conv_uuid,
                    limit=timeline_limit,
                    before_display_seq=before_display_seq,
                    include_tool_details=False,
                )
        operations = await self._project_context_compaction_operations(
            int(row.get("internal_chat_id") or 0),
            conv_uuid,
            operations,
            include_tool_details=False,
            timeline_page=page if timeline_limit is not None else None,
        )
        return web.json_response({
            "ok": True,
            "conversationUuid": conv_uuid,
            "operations": operations,
            "hasMoreBefore": bool(page.get("hasMoreBefore")),
            "nextBeforeDisplaySeq": page.get("nextBeforeDisplaySeq"),
            "timelineLimit": page.get("timelineLimit"),
            "beforeDisplaySeq": page.get("beforeDisplaySeq"),
        })

    async def handle_api_conversation_operation_detail(self, request: web.Request) -> web.Response:
        """Return one full tool snapshot after conversation ownership validation."""
        row = await self._conversation_from_request(request)
        operation_id = str(request.match_info.get("operation_id") or "").strip()
        if not operation_id or len(operation_id) > 512:
            return web.json_response({"ok": False, "error": "invalid_operation_id"}, status=400)
        conv_uuid = str(row.get("conversation_uuid") or "")
        cur = await self.db.conn.execute(
            """
            SELECT operations.*, (
              SELECT MIN(frame.created_at_ms)
              FROM web_event_frames AS frame
              WHERE frame.conversation_uuid=?
                AND frame.op_id=operations.op_id
                AND frame.action IN ('end', 'error', 'cancel', 'stop')
            ) AS terminal_at_ms
            FROM web_operations AS operations
            WHERE operations.conversation_uuid=?
              AND operations.op_id=?
              AND operations.op_type IN ('tool','user_interaction','context_compaction')
            LIMIT 1
            """,
            (conv_uuid, conv_uuid, operation_id),
        )
        operation = await cur.fetchone()
        if operation is None:
            return web.json_response({"ok": False, "error": "operation_detail_not_found"}, status=404)
        public = operation_public(dict(operation), include_tool_details=True)
        return web.json_response({
            "ok": True,
            "conversationUuid": conv_uuid,
            "operation": public,
        })

    async def handle_api_conversation_compaction(self, request: web.Request) -> web.Response:
        """Return one full root summary after conversation ownership validation."""
        row = await self._conversation_from_request(request)
        try:
            summary_id = int(request.match_info.get("summary_id") or 0)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid_summary_id"}, status=400)
        chat_id = int(row.get("internal_chat_id") or 0)
        summary = await SummaryDAO(self.db).get(chat_id, summary_id)
        if summary is None:
            return web.json_response({"ok": False, "error": "context_compaction_not_found"}, status=404)
        conv_uuid = str(row.get("conversation_uuid") or "")
        compaction_id = f"context-compaction:{summary_id}"
        source = "legacy_summary"
        before_tokens = 0
        after_tokens = 0
        cur = await self.db.conn.execute(
            "SELECT op_id, op_type, payload_json FROM web_operations "
            "WHERE conversation_uuid=? AND (op_id=? OR op_type='context_compaction') "
            "ORDER BY id DESC",
            (conv_uuid, f"tool:{compaction_id}"),
        )
        operation_payload: dict[str, Any] = {}
        for operation in await cur.fetchall():
            candidate = operation_json_loads_dict(str(operation["payload_json"] or "{}"))
            candidate_summary_id = str(candidate.get("summaryId") or "").strip()
            if str(operation["op_id"] or "") == f"tool:{compaction_id}" or candidate_summary_id == str(summary_id):
                operation_payload = candidate
                break
        if operation_payload:
            source = str(operation_payload.get("source") or source)
            before_tokens = int(operation_payload.get("beforeTokens") or 0)
            after_tokens = int(operation_payload.get("afterTokens") or 0)
        compacted_output = str(summary.get("summary") or "")
        return web.json_response({
            "ok": True,
            "conversationUuid": conv_uuid,
            "compactionId": compaction_id,
            "summaryId": summary_id,
            "scope": "root",
            "source": source,
            "status": "completed",
            "beforeTokens": before_tokens,
            "afterTokens": after_tokens,
            "summaryChars": len(compacted_output),
            "summaryTokens": int(summary.get("tokens") or 0),
            "upToMessageId": int(summary.get("up_to_message_id") or 0),
            "outputAvailable": bool(compacted_output),
            "summaryRef": f"/api/conversations/{conv_uuid}/compactions/{summary_id}",
            "compactedOutput": compacted_output,
            "renderFormat": "markdown",
        })

    async def handle_api_conversation_frames(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        after = int(request.query.get("afterFrameSeq") or 0)
        limit = min(5000, max(1, int(request.query.get("limit") or 1000)))
        frames = await self._web_frames(str(row.get("conversation_uuid") or ""), after_frame_seq=after, limit=limit)
        return web.json_response({"ok": True, "conversationUuid": str(row.get("conversation_uuid") or ""), "frames": frames, "frameSeq": int(frames[-1]["frameSeq"] if frames else after)})

    async def _stop_web_conversation(self, row: dict[str, Any], *, requested_by: str = "web", message: str = "已停止") -> dict[str, Any]:
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        conv_uuid = str(row.get("conversation_uuid") or "")
        stop_at_ms = int(time.time() * 1000)
        if conv_uuid:
            # Set this before cancellation can enter _run_web_turn's handler;
            # otherwise both paths may publish their own terminal stop frame.
            self._web_stop_markers[conv_uuid] = stop_at_ms
        stopped_run = False
        run_still_running = False
        if self.runs is not None:
            had_running_run = self.runs.is_running(internal_chat_id)
            if had_running_run and self.control_actions is not None:
                # Hard cancellation should be immediate.  Keep a cooperative
                # boundary signal as a fallback in case an upstream await denies
                # cancellation; the current turn clears it when it exits.
                self.control_actions.request_soft_stop(internal_chat_id, message)
            if had_running_run:
                stopped_run = await self.runs.cancel_and_wait(internal_chat_id, timeout_s=5.0)
            run_still_running = self.runs.is_running(internal_chat_id)
            if not run_still_running and self.control_actions is not None:
                self.control_actions.consume_soft_stop(internal_chat_id)
        stopped_tasks = 0
        stopped_task_uuids: set[str] = set()
        if self.rath is not None:
            # Web 多会话用 internal_chat_id 隔离 Rath 任务；不会误停其他 live 会话。
            # 这里必须覆盖 detached Agent：它不拦截新消息，但仍属于当前会话生命周期。
            with contextlib.suppress(Exception):
                stopped_task_uuids = {str(getattr(task, "task_uuid", "") or "") for task in await self.rath.all_controllable_tasks_for_chat(internal_chat_id)}
                stopped_task_uuids.discard("")
            stopped_tasks = await self.rath.stop_all_for_chat(
                internal_chat_id,
                requested_by=requested_by,
                message=message,
                timeout_s=2.0,
            )
        process_task_uuids = {
            str(getattr(proc, "task_uuid", "") or "").strip()
            for proc in processes.active()
            if int(getattr(proc, "chat_id", 0) or 0) == internal_chat_id
        }
        process_task_uuids.discard("")
        killed_processes = processes.kill_for_chat(internal_chat_id)
        stopped_task_uuids.update(process_task_uuids)
        if conv_uuid:
            self._web_stopped_task_uuids[conv_uuid] = stopped_task_uuids
            self._web_task_notification_deferred.pop(conv_uuid, None)
            ts = now_ts()
            await self.db.conn.execute(
                "UPDATE web_task_notifications SET state='suppressed', claim_token='', delivered_at=?, updated_at=? WHERE conversation_uuid=? AND state IN ('pending','processing')",
                (ts, ts, conv_uuid),
            )
            await self.db.conn.commit()
        steering.clear(internal_chat_id)
        live = self._live_for(row)
        await live.publish({"type": "pending_steering", "action": "clear", "items": []})
        if run_still_running:
            if conv_uuid:
                await self._touch_web_conversation(
                    conv_uuid,
                    status="running",
                    current_status="停止中",
                )
            return {
                "ok": False,
                "error": "conversation_stop_timeout",
                "message": "主运行未在 5 秒内退出，仍保留停止信号",
                "stoppedRun": False,
                "stoppedTasks": stopped_tasks,
                "stoppedProcesses": killed_processes,
            }
        # A durable card may outlive every actual runner. Stopping must still
        # close those operations, but a genuinely empty idle stop stays a no-op.
        active_operations = False
        if conv_uuid:
            cur = await self.db.conn.execute(
                "SELECT 1 FROM web_operations WHERE conversation_uuid=? "
                "AND op_type IN ('run','tool','user_interaction','agent','agent_supervision','assistant_message','reasoning','status') "
                "AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control') LIMIT 1",
                (conv_uuid,),
            )
            active_operations = await cur.fetchone() is not None
        published_stop = False
        if stopped_run or stopped_tasks or killed_processes or live.status == "running" or active_operations:
            await live.publish({"type": "stopped", "reason": message, "stopAtMs": stop_at_ms})
            published_stop = True
        if conv_uuid:
            await self._touch_web_conversation(
                conv_uuid,
                status="idle",
                current_status="已停止" if published_stop else "就绪",
            )
        return {"ok": True, "stoppedRun": stopped_run, "stoppedTasks": stopped_tasks, "stoppedProcesses": killed_processes}

    async def handle_api_conversation_context_strategy(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        value = body.get("strategy")
        if set(body) != {"strategy"} or not isinstance(value, str) or value not in CONTEXT_STRATEGIES:
            return web.json_response({"ok": False, "error": "invalid_context_strategy"}, status=400)
        async with self.db.conn.transaction(label="conversation-context-strategy") as conn:
            await conn.execute("UPDATE web_conversations SET context_strategy=?,updated_at=? WHERE conversation_uuid=? AND owner_chat_id=?",
                               (value, now_ts(), row["conversation_uuid"], session.chat_id))
        config = await self._conversation_run_config_public(session.chat_id, row["conversation_uuid"])
        await self.audit("web.conversation.context_strategy", actor="web", chat_id=session.chat_id,
                         detail={"conversationUuid": row["conversation_uuid"], "strategy": value})
        return web.json_response({"ok": True, "strategy": value, "effectiveAt": "next_safe_boundary", "runConfig": config})

    async def handle_api_conversation_compact(self, request: web.Request) -> web.Response:
        from app.context.builder import build_controller_history
        from app.context.request_view import expanded_request_view
        from app.context.runtime import ContextManager
        from app.context.store import ContextOwner, WindowStore
        from app.context.strategies import ModelSummaryStrategy
        from app.context.window import WindowPolicy
        from app.task_memory import (
            reconcile_task_memory_runtime_state,
            reset_task_memory_runtime_epoch,
        )
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        chat_id, conv_uuid = int(row["internal_chat_id"]), str(row["conversation_uuid"])
        async with self.operation_locks.try_chat(chat_id, "web_manual_compact") as acquired:
            if not acquired:
                return web.json_response({"ok": False, "error": "busy"}, status=409)
            row = await self._conversation_row(session.chat_id, conv_uuid, require=True)
            if row.get("context_strategy") != "model_summary":
                return web.json_response({"ok": False, "error": "manual_compaction_requires_summary_strategy"}, status=409)
            if await self._web_conversation_has_active_runtime(row):
                return web.json_response({"ok": False, "error": "busy"}, status=409)
            messages = MessageDAO(self.db)
            sid = await messages.get_or_create_session_uuid(chat_id)
            label = str(row.get("model") or self.config.models.primary)
            backend, model, max_tokens = self.llm_factory.backend_for(label)
            store = WindowStore(self.db, ContextOwner.controller(chat_id=chat_id, session_uuid=sid, conversation_uuid=conv_uuid))
            tokens = await messages.latest_controller_context_usage(chat_id, session_uuid=sid, expected_model=label)
            trigger = self._model_rollover_trigger_tokens(label)
            minimum = self.config.agent.manual_compact_min_percent
            if tokens is None or trigger <= 0:
                return web.json_response({"ok": False, "error": "context_usage_unknown"}, status=409)
            if tokens * 100 < trigger * minimum:
                return web.json_response({"ok": False, "error": "below_threshold", "requiredPercent": minimum}, status=409)
            # Budget the selected execution model's next request, not the
            # separate summary model or serializer defaults. Freeze the same
            # effective mode fields used by _run_web_turn / Agent.run.
            request_options: dict[str, Any] = {
                "max_tokens": max_tokens,
                "think_level": await self._effective_thinking_level(chat_id, label),
                "session_id": sid, "service_tier": "",
                "fast_request": {"body": {}, "headers": {}},
            }
            if str(getattr(backend, "protocol", "") or "").lower() == "responses":
                request_options["native_continuation"] = True
            model_meta = self.config.models.resolve(label)
            if model_meta and await messages.get_fast_mode(chat_id):
                provider_def, model_def = model_meta
                if model_def.supports_fast:
                    if model_def.fast_request is not None:
                        request_options["fast_request"] = model_def.fast_request.model_dump(mode="json")
                    else:
                        request_options["service_tier"] = fast_request_mode(provider_def, model_def)
            live = self._live_for(row)
            op_id = f"context-compaction:{uuid.uuid4()}"
            async def publish(status: str, detail: dict[str, Any]) -> None:
                await live.publish({"type": "context_compaction_state", "_webOperationSpecs": [{
                    "op_id": op_id, "op_type": "context_compaction", "action": "start" if status == "running" else "end",
                    "payload": {**detail, "strategy": "model_summary", "scope": "root", "source": "manual",
                                "name": "ContextCompaction", "compactionId": op_id, "status": status, "active": status == "running"},
                    "status": status, "lifecycle": "active" if status == "running" else "terminal", "source": "manual", "internal": False,
                }]})
            async def account(call: dict[str, Any]) -> None:
                actual = str(call.get("model") or label)
                resolved = self.config.models.resolve(actual)
                await self._persist_web_model_call_delta(messages, chat_id, session_uuid=sid, call=call,
                    model_cost=resolved[1].cost if resolved else {}, model_label=actual, protocol=call.get("protocol", ""),
                    think_level="off", call_kind="context_compaction")
            outcome: dict[str, Any] = {}
            async def done(detail: dict[str, Any]) -> None:
                outcome.update(detail)
                clear_read_file_state(chat_id, sid, agent_session_uuid="", task_uuid="", store=self.tools.file_state)
                await publish("completed", detail)
            async def strategy() -> str:
                return "model_summary"  # This already-started manual operation keeps its strategy.
            manager = ContextManager(store, WindowPolicy(self.llm_factory.context_window(label),
                trigger_tokens=trigger, trigger_ratio=self.config.agent.compact_ratio,
                retain_ratio=self.config.context_management.retain_ratio, max_output_tokens=max_tokens),
                backend=backend, model=model, model_label=label, strategy_resolver=strategy, on_rotated=done,
                strategies={"model_summary": ModelSummaryStrategy(self.config, self.llm_factory, label, on_model_call=account)})
            async def refresh_runtime(request_messages: list[Message]) -> list[Message]:
                epoch = reset_task_memory_runtime_epoch(request_messages)
                return await reconcile_task_memory_runtime_state(
                    request_messages, TaskMemoryDAO(self.db), conversation_uuid=conv_uuid, epoch=epoch,
                )
            await publish("running", {"beforeTokens": tokens})
            try:
                history = await build_controller_history(
                    messages, chat_id, reference_store=self._reference_store(),
                )
                manager.bind_sources(history)
                expanded = await self._reference_store().overlay(history, conversation_uuid=conv_uuid)
                request_view = expanded_request_view(expanded)
                system = await messages.get_system_snapshot(chat_id) or await self._build_system_prompt_for_chat(conversation_uuid=conv_uuid)
                cur = await self.db.conn.execute("SELECT COALESCE(MAX(id),0) AS n FROM messages WHERE chat_id=?", (chat_id,))
                high_water = int((await cur.fetchone())["n"])
                await manager.prepare(history, system=system, tools=self.tools.schemas(scope="main"),
                                      force=True, source="manual", expected_message_high_water=high_water,
                                      refresh_after_rotation=refresh_runtime, request_view=request_view,
                                      request_options=request_options)
            except Exception as exc:
                await publish("failed", {"error": str(exc)[:500], "instructionsPreserved": True})
                return web.json_response({"ok": False, "error": "context_compaction_failed", "message": str(exc)[:500]}, status=409)
        return web.json_response({"ok": True, "outcome": outcome, "state": await self._chat_payload(chat_id, row)})

    async def handle_api_conversation_stop(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        result = await self._stop_web_conversation(row)
        await self.audit("web.conversation.stop", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid"), **result})
        return web.json_response(result, status=200 if result.get("ok") else 409)

    async def handle_api_conversation_retry_cancel(self, request: web.Request) -> web.Response:
        return await self._handle_api_conversation_retry_action(request, "cancel")

    async def handle_api_conversation_retry_now(self, request: web.Request) -> web.Response:
        return await self._handle_api_conversation_retry_action(request, "retry")

    async def _handle_api_conversation_retry_action(self, request: web.Request, action: str) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        task_uuid = str(body.get("taskUuid") or "").strip()
        wait_id = str(body.get("waitId") or "").strip()
        if not wait_id and action != "cancel":
            return web.json_response({"ok": False, "error": "retry_wait_id_required"}, status=400)
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        accepted = False
        scope = "main"
        if task_uuid:
            scope = "agent"
            task = await self.rath_dao.get_task(task_uuid) if self.rath_dao is not None else None
            if task is None or int(task.chat_id or 0) != internal_chat_id:
                return web.json_response({"ok": False, "error": "retry_task_not_found"}, status=404)
            retry_state = task.output.get("retry") if isinstance(task.output, dict) else None
            if isinstance(retry_state, dict) and retry_state.get("active"):
                if not wait_id and action == "cancel":
                    wait_id = str(retry_state.get("waitId") or "")
                if retry_state.get("waitId") == wait_id:
                    accepted = bool(self.rath is not None and self.rath.request_retry_action(task_uuid, wait_id, action))
        elif self.control_actions is not None:
            live = self._live_for(row)
            retry_state = getattr(live, "active_retry", {})
            if getattr(live, "status", "") == "running" and isinstance(retry_state, dict) and retry_state.get("active"):
                if not wait_id and action == "cancel":
                    wait_id = str(retry_state.get("waitId") or "")
                if retry_state.get("waitId") == wait_id:
                    accepted = self.control_actions.request_retry_action(internal_chat_id, wait_id, action)
        await self.audit(
            f"web.conversation.retry.{action}",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"conversationUuid": row.get("conversation_uuid"), "taskUuid": task_uuid, "scope": scope, "accepted": accepted},
        )
        return web.json_response({"ok": accepted, "accepted": accepted, "scope": scope, "taskUuid": task_uuid})

    async def handle_api_conversation_confirmation_answer(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        conv_uuid = str(row.get("conversation_uuid") or "")
        confirmation_id = str(request.match_info.get("confirmation_id") or "").strip()
        body = await self._json_body(request)
        item = await self.interactions.get(confirmation_id, owner_chat_id=session.chat_id)
        if not item or item.get("conversationUuid") != conv_uuid:
            return web.json_response({"ok": False, "error": "confirmation_not_found"}, status=404)
        response = await self.interactions.submit(confirmation_id, session.chat_id, body, source="web")
        status_code = int(response.pop("statusCode", 200))
        if response.get("ok") and not response.get("replayed"):
            await self.audit(
                "web.confirmation.answer", actor="web", chat_id=session.chat_id, ip=request.remote or "",
                detail={"conversationUuid": conv_uuid, "confirmationId": confirmation_id,
                        "action": item["action"], "result": _confirmation_answer_audit_result(item["action"], item, response["result"])},
            )
        return web.json_response({"confirmationId": confirmation_id, "action": item["action"], **response}, status=status_code)

    async def handle_api_conversation_title_generate(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        conv_uuid = str(row.get("conversation_uuid") or "")
        if self._web_starting_turns.get(conv_uuid) or await self._web_conversation_has_active_runtime(row):
            return web.json_response({"ok": False, "error": "run_is_active", "message": "当前轮完成后再生成"}, status=409)
        if not await self._conversation_title_turns(conv_uuid):
            return web.json_response({"ok": False, "error": "conversation_has_no_completed_turns"}, status=409)
        task = self._start_conversation_title_task(row, automatic=False)
        if task is None:
            return web.json_response({"ok": False, "error": "conversation_title_generation_in_progress"}, status=409)
        result = await asyncio.shield(task)
        if not result.get("ok"):
            return web.json_response({
                "ok": False,
                "error": str(result.get("error") or "conversation_title_generation_failed"),
                "message": "名称生成失败，已保留原名称",
            }, status=502)
        await self.audit(
            "web.conversation.title.generate",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"conversationUuid": conv_uuid, "changed": bool(result.get("changed"))},
        )
        return web.json_response({"ok": True, **result})

    async def handle_api_conversation_patch(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        has_title = "title" in body
        has_archived = "archived" in body
        if not has_title and not has_archived:
            return web.json_response({"ok": False, "error": "nothing_to_update"}, status=400)

        title = ""
        if has_title:
            title = str(body.get("title")).strip()
            if not title:
                return web.json_response({"ok": False, "error": "title_required"}, status=400)

        archived_at = 0
        if has_archived:
            if not isinstance(body.get("archived"), bool):
                return web.json_response({"ok": False, "error": "invalid_archived_type"}, status=400)
            archived_at = now_ts() if body["archived"] else 0

        assignments: list[str] = []
        params: list[Any] = []
        if has_title:
            ts = now_ts()
            assignments.extend(["title=?", "updated_at=?"])
            params.extend([title, ts])
        if has_archived:
            # Archive is deliberately a visibility field only: it does not touch
            # status, pinning, display order, or a running conversation's runtime.
            assignments.append("archived_at=?")
            params.append(archived_at)
        conv_uuid = str(row.get("conversation_uuid") or "")
        params.extend([conv_uuid, session.chat_id])
        await self.db.conn.execute(
            f"UPDATE web_conversations SET {', '.join(assignments)} WHERE conversation_uuid=? AND owner_chat_id=?",
            tuple(params),
        )
        await self.db.conn.commit()
        self._invalidate_tree_status(int(session.chat_id))
        if has_archived:
            getattr(self, "_tree_projection_ready", set()).discard(int(session.chat_id))
        if has_title:
            row["title"] = title
            row["updated_at"] = ts
            await self.audit("web.conversation.rename", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": conv_uuid, "title": title})
        if has_archived:
            row["archived_at"] = archived_at
            await self.audit(
                "web.conversation.archive" if archived_at else "web.conversation.unarchive",
                actor="web",
                chat_id=session.chat_id,
                ip=request.remote or "",
                detail={"conversationUuid": conv_uuid},
            )
        response = {"ok": True, "conversation": self._web_conversation_json(row, live=self._web_live_streams.get(conv_uuid))}
        if has_archived and archived_at:
            response["nextConversation"] = await self._tree_archive_successor(int(session.chat_id), conv_uuid)
        return web.json_response(response)

    async def handle_api_conversation_reorder(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)

        def optional_uuid(key: str) -> str | None:
            raw = body.get(key, "")
            if raw is None:
                return ""
            return raw.strip() if isinstance(raw, str) else None

        before_uuid = optional_uuid("beforeConversationUuid")
        after_uuid = optional_uuid("afterConversationUuid")
        if before_uuid is None or after_uuid is None:
            return web.json_response({"ok": False, "error": "invalid_reorder_neighbor"}, status=400)
        moving_uuid = str(row.get("conversation_uuid") or "")
        if not before_uuid and not after_uuid:
            return web.json_response({"ok": False, "error": "reorder_neighbor_required"}, status=400)
        if moving_uuid in {before_uuid, after_uuid} or (before_uuid and before_uuid == after_uuid):
            return web.json_response({"ok": False, "error": "invalid_reorder_neighbor"}, status=400)

        lookup_uuids = [moving_uuid, *[value for value in (before_uuid, after_uuid) if value]]
        async with self.db.conn.transaction(label="reorder-web-conversation") as conn:
            placeholders = ",".join("?" for _ in lookup_uuids)
            cur = await conn.execute(
                f"""
                SELECT conversation_uuid, pinned_at, folder_uuid
                FROM web_conversations
                WHERE owner_chat_id=? AND conversation_uuid IN ({placeholders})
                """,
                (session.chat_id, *lookup_uuids),
            )
            lookup = {str(item["conversation_uuid"] or ""): dict(item) for item in await cur.fetchall()}
            moving = lookup.get(moving_uuid)
            if moving is None:
                return web.json_response({"ok": False, "error": "conversation_not_found"}, status=404)
            moving_pinned = int(moving.get("pinned_at") or 0) > 0
            moving_folder = str(moving.get("folder_uuid") or "")
            for neighbor_uuid in (before_uuid, after_uuid):
                if not neighbor_uuid:
                    continue
                neighbor = lookup.get(neighbor_uuid)
                if neighbor is None:
                    return web.json_response({"ok": False, "error": "reorder_neighbor_not_found"}, status=404)
                if ((int(neighbor.get("pinned_at") or 0) > 0) != moving_pinned
                        or str(neighbor.get("folder_uuid") or "") != moving_folder):
                    return web.json_response({"ok": False, "error": "conversation_reorder_group_mismatch"}, status=409)
            display_order = await self._reorder_web_conversation_display_group(
                session.chat_id,
                pinned=moving_pinned,
                folder_uuid=moving_folder,
                moving_uuid=moving_uuid,
                before_uuid=before_uuid,
                after_uuid=after_uuid,
                conn=conn,
            )
            if display_order is None:
                return web.json_response({"ok": False, "error": "conversation_reorder_stale"}, status=409)

        row["display_order"] = display_order
        await self.audit(
            "web.conversation.reorder",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={
                "conversationUuid": moving_uuid,
                "beforeConversationUuid": before_uuid,
                "afterConversationUuid": after_uuid,
                "pinned": moving_pinned,
            },
        )
        return web.json_response({"ok": True, "conversation": self._web_conversation_json(row, live=self._web_live_streams.get(moving_uuid))})

    async def handle_api_conversation_pin(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        ts = now_ts()
        await self.db.conn.execute(
            "UPDATE web_conversations SET pinned_at=?, updated_at=? WHERE conversation_uuid=? AND owner_chat_id=?",
            (ts, ts, str(row.get("conversation_uuid") or ""), session.chat_id),
        )
        await self.db.conn.commit()
        self._invalidate_tree_status(int(session.chat_id))
        row["pinned_at"] = ts
        row["updated_at"] = ts
        await self.audit("web.conversation.pin", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid")})
        return web.json_response({"ok": True, "conversation": self._web_conversation_json(row, live=self._web_live_streams.get(str(row.get("conversation_uuid") or "")))})

    async def handle_api_conversation_unpin(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        ts = now_ts()
        await self.db.conn.execute(
            "UPDATE web_conversations SET pinned_at=0, updated_at=? WHERE conversation_uuid=? AND owner_chat_id=?",
            (ts, str(row.get("conversation_uuid") or ""), session.chat_id),
        )
        await self.db.conn.commit()
        self._invalidate_tree_status(int(session.chat_id))
        row["pinned_at"] = 0
        row["updated_at"] = ts
        await self.audit("web.conversation.unpin", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid")})
        return web.json_response({"ok": True, "conversation": self._web_conversation_json(row, live=self._web_live_streams.get(str(row.get("conversation_uuid") or "")))})

    async def handle_api_conversation_duplicate(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        await self._reconcile_inactive_web_conversation_operations(row, source="conversation_duplicate_reconcile")
        row = await self._conversation_row(session.chat_id, str(row.get("conversation_uuid") or ""), require=True)  # type: ignore[assignment]
        if await self._web_conversation_has_active_runtime(row):
            return web.json_response({"ok": False, "error": "conversation_is_active"}, status=409)
        body = await self._json_body(request)
        title = str(body.get("title") or "").strip()
        async with self._web_operation_lock(str(row.get("conversation_uuid") or "")):
            new_row = await self._duplicate_web_conversation_data(row, title=title)
        live = self._live_for(new_row)
        await self.audit(
            "web.conversation.duplicate",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={
                "sourceConversationUuid": row.get("conversation_uuid"),
                "conversationUuid": new_row.get("conversation_uuid"),
                "sourceInternalChatId": row.get("internal_chat_id"),
                "internalChatId": new_row.get("internal_chat_id"),
            },
        )
        return web.json_response({"ok": True, "conversation": self._web_conversation_json(new_row, live=live), "state": await self._chat_payload(int(new_row["internal_chat_id"]), new_row)})

    async def handle_api_conversation_turn_suffix_delete(self, request: web.Request) -> web.Response:
        """Delete one visible user turn and every later model/UI fact atomically."""
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        conv_uuid = str(row.get("conversation_uuid") or "")
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        turn_uuid = str(request.match_info.get("turn_uuid") or "").strip()
        if not conv_uuid or not internal_chat_id or not turn_uuid:
            return web.json_response({"ok": False, "error": "turn_not_found"}, status=404)

        await self._reconcile_inactive_web_conversation_operations(row, source="turn_suffix_delete_reconcile")
        async with self.operation_locks.try_chat(internal_chat_id, "web_turn_suffix_delete") as acquired:
            if not acquired:
                return web.json_response(
                    {"ok": False, "error": "conversation_is_active", "message": "会话正在执行其他操作，请稍后重试删除。"},
                    status=409,
                )
            row = await self._conversation_row(session.chat_id, conv_uuid, require=True)  # type: ignore[assignment]
            if await self._web_conversation_has_active_runtime(row) or steering.has_pending(internal_chat_id):
                return web.json_response(
                    {
                        "ok": False,
                        "error": "conversation_is_active",
                        "message": "会话仍在运行或有待处理插话，请先停止并等待收尾后再删除。",
                    },
                    status=409,
                )

            async with self._web_operation_lock(conv_uuid):
                cur = await self.db.conn.execute(
                    """
                    SELECT * FROM web_operations
                    WHERE conversation_uuid=? AND turn_uuid=? AND op_type='user_message' AND internal=0
                    ORDER BY display_seq ASC, id ASC
                    """,
                    (conv_uuid, turn_uuid),
                )
                target = None
                for candidate in await cur.fetchall():
                    payload = operation_json_loads_dict(str(candidate["payload_json"] or "{}"))
                    if payload.get("queued") or payload.get("interruption") or payload.get("hidden") or payload.get("internal"):
                        continue
                    target = candidate
                    break
                if target is None:
                    return web.json_response({"ok": False, "error": "turn_not_found"}, status=404)

                target_display_seq = int(target["display_seq"] or 0)
                target_root_turn = str(target["run_root_turn_uuid"] or target["turn_uuid"] or turn_uuid)
                cur = await self.db.conn.execute(
                    """
                    SELECT turn_uuid, run_root_turn_uuid, payload_json, display_seq
                    FROM web_operations
                    WHERE conversation_uuid=? AND op_type='user_message' AND internal=0 AND display_seq>=?
                    ORDER BY display_seq ASC, id ASC
                    """,
                    (conv_uuid, target_display_seq),
                )
                deleted_roots: list[str] = []
                seen_roots: set[str] = set()
                for candidate in await cur.fetchall():
                    payload = operation_json_loads_dict(str(candidate["payload_json"] or "{}"))
                    if payload.get("queued") or payload.get("interruption") or payload.get("hidden") or payload.get("internal"):
                        continue
                    root = str(candidate["run_root_turn_uuid"] or candidate["turn_uuid"] or "").strip()
                    if root and root not in seen_roots:
                        seen_roots.add(root)
                        deleted_roots.append(root)
                if target_root_turn not in seen_roots:
                    deleted_roots.insert(0, target_root_turn)
                root_placeholders = ",".join("?" for _ in deleted_roots)

                cur = await self.db.conn.execute(
                    """
                    SELECT MIN(id) AS first_message_id
                    FROM messages
                    WHERE chat_id=? AND conversation_uuid=? AND run_root_turn_uuid=?
                    """,
                    (internal_chat_id, conv_uuid, target_root_turn),
                )
                cutoff_row = await cur.fetchone()
                first_message_id = int((cutoff_row["first_message_id"] if cutoff_row else 0) or 0)
                if first_message_id <= 0:
                    cur = await self.db.conn.execute(
                        """
                        SELECT MIN(link.message_id) AS first_message_id
                        FROM web_operation_messages AS link
                        JOIN messages AS message ON message.id=link.message_id
                        WHERE link.conversation_uuid=? AND link.op_id=? AND message.chat_id=?
                        """,
                        (conv_uuid, str(target["op_id"] or ""), internal_chat_id),
                    )
                    cutoff_row = await cur.fetchone()
                    first_message_id = int((cutoff_row["first_message_id"] if cutoff_row else 0) or 0)
                if first_message_id <= 0:
                    return web.json_response(
                        {
                            "ok": False,
                            "error": "turn_not_traceable",
                            "message": "这轮属于旧历史，缺少精确 DB 绑定，已拒绝模糊删除。",
                        },
                        status=409,
                    )

                root_params = tuple(deleted_roots)
                cur = await self.db.conn.execute(
                    f"""
                    SELECT op_id FROM web_operations
                    WHERE conversation_uuid=? AND (
                      turn_uuid IN ({root_placeholders}) OR run_root_turn_uuid IN ({root_placeholders})
                    )
                    """,
                    (conv_uuid, *root_params, *root_params),
                )
                deleted_op_ids = [str(item["op_id"] or "") for item in await cur.fetchall() if item["op_id"]]
                cur = await self.db.conn.execute(
                    f"""
                    SELECT task_uuid FROM rath_tasks
                    WHERE chat_id=? AND (
                      turn_uuid IN ({root_placeholders}) OR run_root_turn_uuid IN ({root_placeholders})
                    )
                    """,
                    (internal_chat_id, *root_params, *root_params),
                )
                deleted_task_uuids = [str(item["task_uuid"] or "") for item in await cur.fetchall() if item["task_uuid"]]

                from app.context.restart import controller_restart_selection
                from app.context.window import WindowPolicy
                dao = MessageDAO(self.db)
                label = str(row.get("model") or self.config.models.primary)
                resolved = self.config.models.resolve(label)
                threshold = self._model_rollover_trigger_tokens(label)
                try:
                    backend, model, max_output = self.llm_factory.backend_for(label) if self.llm_factory else (None, "", 0)
                    policy = WindowPolicy(
                        context_window=int(resolved[1].context_window or 0) if resolved else 0,
                        trigger_tokens=threshold, trigger_ratio=self.config.agent.compact_ratio,
                        retain_ratio=self.config.context_management.retain_ratio, max_output_tokens=max_output,
                    )
                    restart_messages = await controller_restart_selection(
                        dao, internal_chat_id, first_message_id, policy=policy,
                        system=await dao.get_system_snapshot(internal_chat_id) or "",
                        tools=self.tools.schemas(scope="main") if self.tools else [],
                        reference_store=self._reference_store(), conversation_uuid=conv_uuid,
                        backend=backend, model=model,
                    )
                except (ValueError, RuntimeError) as exc:
                    return web.json_response({"ok": False, "error": "restart_context_unavailable",
                                              "message": str(exc), "originalContextPreserved": True}, status=409)
                now = now_ts()
                async with self.db.conn.transaction(label="delete-web-turn-suffix") as conn:
                    await conn.execute(
                        f"""
                        UPDATE web_artifacts SET deleted_at=?
                        WHERE conversation_uuid=? AND deleted_at=0 AND (
                          message_id>=? OR turn_uuid IN ({root_placeholders})
                        )
                        """,
                        (now, conv_uuid, first_message_id, *root_params),
                    )
                    if deleted_task_uuids:
                        task_placeholders = ",".join("?" for _ in deleted_task_uuids)
                        await conn.execute(
                            f"DELETE FROM web_task_notifications WHERE conversation_uuid=? AND task_uuid IN ({task_placeholders})",
                            (conv_uuid, *deleted_task_uuids),
                        )
                    for root in deleted_roots:
                        await conn.execute(
                            "DELETE FROM web_task_notifications WHERE conversation_uuid=? AND instr(payload_json, ?) > 0",
                            (conv_uuid, root),
                        )
                    await conn.execute(
                        f"DELETE FROM web_tg_notification_outbox WHERE root_turn_uuid IN ({root_placeholders})",
                        root_params,
                    )
                    await conn.execute(
                        f"DELETE FROM web_tg_notification_runs WHERE root_turn_uuid IN ({root_placeholders})",
                        root_params,
                    )
                    await conn.execute(
                        f"""
                        DELETE FROM web_event_frames
                        WHERE conversation_uuid=? AND (
                          turn_uuid IN ({root_placeholders}) OR run_root_turn_uuid IN ({root_placeholders})
                        )
                        """,
                        (conv_uuid, *root_params, *root_params),
                    )
                    if deleted_op_ids:
                        op_placeholders = ",".join("?" for _ in deleted_op_ids)
                        await conn.execute(
                            f"DELETE FROM web_operation_messages WHERE conversation_uuid=? AND op_id IN ({op_placeholders})",
                            (conv_uuid, *deleted_op_ids),
                        )
                    await conn.execute(
                        f"""
                        DELETE FROM web_operations
                        WHERE conversation_uuid=? AND (
                          turn_uuid IN ({root_placeholders}) OR run_root_turn_uuid IN ({root_placeholders})
                        )
                        """,
                        (conv_uuid, *root_params, *root_params),
                    )
                    await clear_deleted_completion(conn, conv_uuid)
                    transcript_deleted = await MessageDAO(self.db).delete_from_message_id(
                        internal_chat_id, first_message_id, restart_messages=restart_messages,
                    )
                    rath_deleted = await self.rath_dao.delete_task_suffix_records(
                        deleted_task_uuids, chat_id=internal_chat_id, deleted_roots=deleted_roots,
                    )
                    await conn.execute(
                        """
                        UPDATE sessions SET stat_user_turns=(
                          SELECT COUNT(*) FROM messages WHERE chat_id=? AND role='user'
                        ), updated_at=? WHERE chat_id=?
                        """,
                        (internal_chat_id, now, internal_chat_id),
                    )
                    await conn.execute(
                        """
                        UPDATE web_conversations
                        SET status='idle', current_status='就绪', last_error='', updated_at=?
                        WHERE conversation_uuid=? AND owner_chat_id=?
                        """,
                        (now, conv_uuid, session.chat_id),
                    )

        # Only after the complete DB transaction commits. A rollback must retain
        # its original file-read state; other conversations/instances are untouched.
        cache = self.tools.file_state if self.tools else None
        clear_read_file_state(internal_chat_id, agent_session_uuid="", task_uuid="", store=cache)
        for sid in rath_deleted.get("affectedAgentSessions", []):
            clear_read_file_state(internal_chat_id, agent_session_uuid=sid, store=cache)
        for tid in deleted_task_uuids:
            clear_read_file_state(internal_chat_id, task_uuid=tid, store=cache)
        # A late callback from a deleted Agent must not start a new controller
        # turn. Keep unrelated pending notifications from surviving turns intact.
        self._web_task_notification_deferred.pop(conv_uuid, None)
        self._web_stopped_task_uuids.setdefault(conv_uuid, set()).update(deleted_task_uuids)
        await self.interactions.cancel_conversation(conv_uuid)
        live = self._web_live_streams.get(conv_uuid)
        if live is not None:
            live.status = "idle"
            live.current_status = "就绪"
            live.current_turn_uuid = ""
            live.current_run_uuid = ""
            live._agent_turn_uuid = ""
            live._latest_user_turn_uuid = ""
            live.draft_text = ""
            live.draft_reasoning = ""
            live.live_tools = []
            with contextlib.suppress(Exception):
                await live.publish({
                    "type": "conversation_reset",
                    "reason": "turn_suffix_deleted",
                    "deletedTurnUuid": turn_uuid,
                    "deletedRootTurns": deleted_roots,
                }, persist=False)
        await self.audit(
            "web.conversation.turn_suffix.delete",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={
                "conversationUuid": conv_uuid,
                "internalChatId": internal_chat_id,
                "turnUuid": turn_uuid,
                "deletedRootTurns": deleted_roots,
                "firstMessageId": first_message_id,
                "deletedOperations": len(deleted_op_ids),
                "transcriptDeleted": transcript_deleted,
                "rathDeleted": rath_deleted,
            },
        )
        return web.json_response({
            "ok": True,
            "conversationUuid": conv_uuid,
            "turnUuid": turn_uuid,
            "deletedRootTurns": deleted_roots,
            "deletedOperations": len(deleted_op_ids),
            "transcriptDeleted": transcript_deleted,
            "rathDeleted": rath_deleted,
        })

    async def handle_api_conversation_delete(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        conv_uuid = str(row.get("conversation_uuid") or "")
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        if not conv_uuid or not internal_chat_id:
            return web.json_response({"ok": False, "error": "conversation_not_found"}, status=404)
        async with self.operation_locks.try_chat(
            internal_chat_id,
            "web_conversation_delete",
        ) as acquired:
            if not acquired:
                return web.json_response(
                    {"ok": False, "error": "conversation_delete_busy", "message": "会话正在执行其他操作，请稍后重试删除。"},
                    status=409,
                )
            return await self._handle_api_conversation_delete_locked(
                request,
                session=session,
                row=row,
                conv_uuid=conv_uuid,
                internal_chat_id=internal_chat_id,
            )

    async def _handle_api_conversation_delete_locked(
        self,
        request: web.Request,
        *,
        session: WebSession,
        row: dict[str, Any],
        conv_uuid: str,
        internal_chat_id: int,
    ) -> web.Response:
        stopped_run = False
        if self.runs is not None:
            had_running_controller = self.runs.is_running(internal_chat_id)
            stopped_run = await self.runs.cancel_and_wait(internal_chat_id, timeout_s=2.0)
            if had_running_controller and (not stopped_run or self.runs.is_running(internal_chat_id)):
                return web.json_response(
                    {"ok": False, "error": "conversation_delete_busy", "message": "会话仍在停止中，请稍后重试删除。"},
                    status=409,
                )
        stopped_tasks = 0
        if self.rath is not None:
            try:
                stopped_tasks = await self.rath.stop_all_for_chat(
                    internal_chat_id,
                    requested_by="web",
                    message="会话已删除",
                    timeout_s=2.0,
                    require_terminated=True,
                )
            except TimeoutError:
                return web.json_response(
                    {"ok": False, "error": "conversation_delete_busy", "message": "后台 Agent 仍在停止中，请稍后重试删除。"},
                    status=409,
                )
        killed_processes = processes.kill_for_chat(internal_chat_id)
        if processes.active_for_chat(internal_chat_id):
            return web.json_response(
                {"ok": False, "error": "conversation_delete_busy", "message": "会话子进程仍在停止中，请稍后重试删除。"},
                status=409,
            )
        steering.clear(internal_chat_id)

        live = self._web_live_streams.pop(conv_uuid, None)
        if live is not None:
            with contextlib.suppress(Exception):
                await live.publish({"type": "stopped", "reason": "会话已删除"})

        rath_deleted = {"tasks": 0, "events": 0, "artifacts": 0, "controls": 0, "taskMemories": 0}
        task_memory_deleted = 0
        async with self._web_operation_lock(conv_uuid):
            await self.db.conn.execute("DELETE FROM web_operation_messages WHERE conversation_uuid=?", (conv_uuid,))
            await self.db.conn.execute("DELETE FROM web_event_frames WHERE conversation_uuid=?", (conv_uuid,))
            await self.db.conn.execute("DELETE FROM web_operations WHERE conversation_uuid=?", (conv_uuid,))
            await self.db.conn.execute("DELETE FROM web_task_notifications WHERE conversation_uuid=?", (conv_uuid,))
            await self.db.conn.execute("UPDATE web_artifacts SET deleted_at=? WHERE conversation_uuid=? AND deleted_at=0", (now_ts(), conv_uuid))
            from app.context.lifecycle import delete_controller_windows
            await delete_controller_windows(self.db.conn, internal_chat_id)
            await self.db.conn.execute("DELETE FROM messages WHERE chat_id=?", (internal_chat_id,))
            await self.db.conn.execute("DELETE FROM model_calls WHERE chat_id=?", (internal_chat_id,))
            await self.db.conn.execute("DELETE FROM tool_calls WHERE chat_id=?", (internal_chat_id,))
            await self.db.conn.execute("DELETE FROM operations WHERE chat_id=?", (internal_chat_id,))
            await self.db.conn.execute("DELETE FROM summaries WHERE chat_id=?", (internal_chat_id,))
            await self.db.conn.execute("DELETE FROM controller_model_contexts WHERE chat_id=?", (internal_chat_id,))
            await self.db.conn.execute("DELETE FROM web_controller_context_snapshots WHERE chat_id=?", (internal_chat_id,))
            await self.db.conn.execute("DELETE FROM web_memory_reminders WHERE chat_id=?", (internal_chat_id,))
            rath_deleted = await self.rath_dao.delete_task_records_for_chat(internal_chat_id)
            task_memory_deleted = await TaskMemoryDAO(self.db).hard_delete_conversation(
                conv_uuid,
                conn=self.db.conn,
            )
            await self.db.conn.execute("DELETE FROM rath_agent_sessions WHERE chat_id=?", (internal_chat_id,))
            await self.db.conn.execute("DELETE FROM sessions WHERE chat_id=?", (internal_chat_id,))
            await self.db.conn.execute("DELETE FROM web_conversations WHERE conversation_uuid=?", (conv_uuid,))
            await self.db.conn.commit()

        self._web_stop_markers.pop(conv_uuid, None)
        self._web_stopped_task_uuids.pop(conv_uuid, None)
        self._web_task_notification_deferred.pop(conv_uuid, None)
        await self.interactions.cancel_conversation(conv_uuid)
        await self.audit(
            "web.conversation.delete",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={
                "conversationUuid": conv_uuid,
                "internalChatId": internal_chat_id,
                "stoppedRun": stopped_run,
                "stoppedTasks": stopped_tasks,
                "killedProcesses": killed_processes,
                "rathDeleted": rath_deleted,
                "taskMemoriesDeleted": task_memory_deleted,
            },
        )
        return web.json_response({
            "ok": True,
            "conversationUuid": conv_uuid,
            "taskMemoriesDeleted": task_memory_deleted,
        })

    async def _conversation_run_config_public(
        self, owner_chat_id: int, conversation_uuid: str,
    ) -> dict[str, Any]:
        """Return one coherent, timeline-free snapshot of conversation run settings."""
        cur = await self.db.conn.execute(
            """
            SELECT conversations.*,
                   sessions.thinking_level AS run_thinking_level,
                   sessions.fast_mode AS run_fast_mode
            FROM web_conversations AS conversations
            LEFT JOIN sessions ON sessions.chat_id=conversations.internal_chat_id
            WHERE conversations.owner_chat_id=? AND conversations.conversation_uuid=?
            LIMIT 1
            """,
            (int(owner_chat_id), str(conversation_uuid or "")),
        )
        row = await cur.fetchone()
        if not row:
            raise web.HTTPNotFound(text="conversation_not_found")
        conversation = dict(row)
        model = (
            str(conversation.get("model") or "")
            or str(getattr(self.model_selection, "current", "") or "")
            or self.config.models.primary
        )
        thinking_levels = self._model_thinking_levels(model)
        thinking_level = normalize_think_level(str(conversation.get("run_thinking_level") or "")) or ""
        effective_thinking = (
            thinking_level
            if thinking_level and thinking_level in thinking_levels
            else (self._model_default_thinking_level(model) if thinking_levels else "off")
        )
        fast_requested = bool(int(conversation.get("run_fast_mode") or 0))
        fast_supported = self._model_supports_fast(model)
        model_meta = self.config.models.resolve(model)
        context_window = int(model_meta[1].context_window or 0) if model_meta else 0
        agent_runtime = resolve_agent_runtime_config(
            None,
            config=self.config,
            model_selection_current=str(getattr(self.model_selection, "current", "") or ""),
            conversation=conversation,
            main_model=model,
            main_fast_requested=fast_requested,
        )
        return {
            "conversationUuid": str(conversation.get("conversation_uuid") or ""),
            "contextStrategy": normalize_strategy(conversation.get("context_strategy")),
            "manualCompactMinPercent": self.config.agent.manual_compact_min_percent,
            "model": model,
            "thinkingLevel": thinking_level,
            "effectiveThinkingLevel": effective_thinking or "off",
            "thinkingLevels": thinking_levels,
            "defaultThinkingLevel": self._model_default_thinking_level(model) if thinking_levels else "",
            "supportsThinking": bool(thinking_levels),
            "fastMode": bool(fast_requested and fast_supported),
            "fastRequested": fast_requested,
            "fastSupported": bool(fast_supported),
            "effectiveFastMode": bool(fast_requested and fast_supported),
            "agentRunConfig": agent_run_config_public(agent_runtime),
            "contextWindow": context_window,
            "rolloverTriggerTokens": self._model_rollover_trigger_tokens(model),
            "windowTriggerRatio": float(self.config.agent.compact_ratio or 0.7),
        }

    async def handle_api_conversation_model(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        internal_chat_id = int(row["internal_chat_id"])
        body = await self._json_body(request)
        if set(body) != {"model"}:
            return web.json_response({"ok": False, "error": "invalid_model_request"}, status=400)
        if not isinstance(body.get("model"), str):
            return web.json_response({"ok": False, "error": "invalid_model_type"}, status=400)
        model = body["model"].strip()
        if not model:
            return web.json_response({"ok": False, "error": "model_required"}, status=400)
        running = bool(self.runs is not None and self.runs.is_running(internal_chat_id))
        if self.model_selection is None or self.config.models.resolve(model) is None:
            return web.json_response({"ok": False, "error": "model_not_found"}, status=404)
        current_model = str(row.get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
        messages = MessageDAO(self.db)
        if await messages.has_history(internal_chat_id) and self.model_selection.family_of(current_model) != self.model_selection.family_of(model):
            return web.json_response({"ok": False, "error": "cross_family_requires_new_session"}, status=409)
        levels = self._model_thinking_levels(model)
        stored_thinking = normalize_think_level(await messages.get_thinking_level(internal_chat_id))
        next_thinking = (
            stored_thinking
            if stored_thinking and stored_thinking in levels
            else (self._model_default_thinking_level(model) if levels else "")
        )
        next_fast = bool(await messages.get_fast_mode(internal_chat_id) and self._model_supports_fast(model))
        async with self.db.conn.transaction(label="web-conversation-model-and-defaults") as conn:
            if not running and model != current_model:
                await conn.execute("UPDATE context_windows SET usage_known=0,usage_tokens=0,route_fingerprint='',window_version=window_version+1 WHERE owner_kind='controller' AND owner_key LIKE ?", (f"controller:{internal_chat_id}:%",))
            # Even a same-family model change starts a new provider-native chain.
            await conn.execute(
                "DELETE FROM controller_model_contexts WHERE chat_id=?", (internal_chat_id,)
            )
            await conn.execute(
                "UPDATE web_conversations SET model=?, updated_at=? WHERE conversation_uuid=? AND owner_chat_id=?",
                (model, now_ts(), str(row["conversation_uuid"]), session.chat_id),
            )
            await conn.execute(
                "UPDATE sessions SET thinking_level=?, fast_mode=?, updated_at=? WHERE chat_id=?",
                (next_thinking, 1 if next_fast else 0, now_ts(), internal_chat_id),
            )
            await WebConversationDefaultsDAO(self.db).patch_or_seed(
                session.chat_id,
                {
                    "main_model": model,
                    "main_thinking_level": next_thinking,
                    "main_fast_mode": 1 if next_fast else 0,
                },
                self._web_builtin_run_defaults(),
            )
        row["model"] = model
        run_config = await self._conversation_run_config_public(session.chat_id, str(row["conversation_uuid"]))
        await self.audit("web.conversation.model", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid"), "model": model, "nextRun": running})
        return web.json_response({"ok": True, "model": model, "nextRun": running, "runConfig": run_config})

    async def handle_api_conversation_thinking(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        internal_chat_id = int(row["internal_chat_id"])
        body = await self._json_body(request)
        if set(body) != {"level"} or not isinstance(body.get("level"), str):
            return web.json_response({"ok": False, "error": "invalid_thinking_request"}, status=400)
        level = normalize_think_level(body["level"]) or ""
        running = bool(self.runs is not None and self.runs.is_running(internal_chat_id))
        model = str(row.get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
        levels = self._model_thinking_levels(model)
        if level and level not in levels:
            return web.json_response({"ok": False, "error": "invalid_thinking_level"}, status=400)
        if not level and levels:
            level = self._model_default_thinking_level(model)
        async with self.db.conn.transaction(label="web-conversation-thinking-and-defaults") as conn:
            await conn.execute(
                "UPDATE sessions SET thinking_level=?, updated_at=? WHERE chat_id=?",
                (level, now_ts(), internal_chat_id),
            )
            await WebConversationDefaultsDAO(self.db).patch_or_seed(
                session.chat_id,
                {"main_thinking_level": level},
                self._web_builtin_run_defaults(),
            )
        run_config = await self._conversation_run_config_public(session.chat_id, str(row["conversation_uuid"]))
        await self.audit("web.conversation.thinking", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid"), "level": level, "nextRun": running})
        return web.json_response({"ok": True, "level": level, "nextRun": running, "runConfig": run_config})

    async def handle_api_conversation_fast(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        internal_chat_id = int(row["internal_chat_id"])
        running = bool(self.runs is not None and self.runs.is_running(internal_chat_id))
        body = await self._json_body(request)
        if set(body) != {"enabled"} or not isinstance(body.get("enabled"), bool):
            return web.json_response({"ok": False, "error": "invalid_fast_request"}, status=400)
        enabled = body["enabled"]
        model = str(row.get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
        if enabled and not self._model_supports_fast(model):
            return web.json_response({"ok": False, "error": "fast_not_supported"}, status=400)
        async with self.db.conn.transaction(label="web-conversation-fast-and-defaults") as conn:
            await conn.execute(
                "UPDATE sessions SET fast_mode=?, updated_at=? WHERE chat_id=?",
                (1 if enabled else 0, now_ts(), internal_chat_id),
            )
            await WebConversationDefaultsDAO(self.db).patch_or_seed(
                session.chat_id,
                {"main_fast_mode": 1 if enabled else 0},
                self._web_builtin_run_defaults(),
            )
        run_config = await self._conversation_run_config_public(session.chat_id, str(row["conversation_uuid"]))
        await self.audit("web.conversation.fast", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid"), "enabled": enabled, "nextRun": running})
        return web.json_response({"ok": True, "enabled": enabled, "effectiveFastMode": enabled and self._model_supports_fast(model), "nextRun": running, "runConfig": run_config})

    async def handle_api_conversation_agent_run_config(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        internal_chat_id = int(row["internal_chat_id"])
        running = bool(self.runs is not None and self.runs.is_running(internal_chat_id))
        body = await self._json_body(request)
        allowed = {"model", "thinkLevel", "thinkingLevel", "level", "fastMode", "fast", "enabled"}
        if not body or set(body) - allowed:
            return web.json_response({"ok": False, "error": "invalid_agent_run_config_field"}, status=400)
        think_keys = [key for key in ("thinkLevel", "thinkingLevel", "level") if key in body]
        fast_keys = [key for key in ("fastMode", "fast", "enabled") if key in body]
        if len(think_keys) > 1 or len(fast_keys) > 1:
            return web.json_response({"ok": False, "error": "duplicate_agent_run_config_field"}, status=400)
        if "model" in body and not isinstance(body.get("model"), str):
            return web.json_response({"ok": False, "error": "invalid_model_type"}, status=400)
        model = str(body.get("model") or "").strip()
        if "model" in body and model and self.config.models.resolve(model) is None:
            return web.json_response({"ok": False, "error": "model_not_found"}, status=404)

        think_raw = body.get(think_keys[0]) if think_keys else None
        if think_keys and not isinstance(think_raw, str):
            return web.json_response({"ok": False, "error": "invalid_thinking_type"}, status=400)
        think_level = ""
        if think_raw is not None and str(think_raw).strip():
            think_level = normalize_think_level(str(think_raw)) or ""
            if not think_level:
                return web.json_response({"ok": False, "error": "invalid_thinking_level"}, status=400)
            check_model = model or str(row.get("agent_model") or "") or str(row.get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
            levels = self._model_thinking_levels(check_model)
            if think_level not in levels:
                return web.json_response({"ok": False, "error": "invalid_thinking_level"}, status=400)

        fast_raw = body.get(fast_keys[0]) if fast_keys else None
        if fast_keys and fast_raw is not None and not isinstance(fast_raw, bool):
            return web.json_response({"ok": False, "error": "invalid_fast_type"}, status=400)
        agent_fast_mode = int(row.get("agent_fast_mode") if row.get("agent_fast_mode") is not None else -1)
        if fast_keys:
            if fast_raw is None:
                agent_fast_mode = -1
            else:
                enabled = fast_raw
                check_model = model or str(row.get("agent_model") or "") or str(row.get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
                if enabled and not self._model_supports_fast(check_model):
                    return web.json_response({"ok": False, "error": "fast_not_supported"}, status=400)
                agent_fast_mode = 1 if enabled else 0

        next_model = model if "model" in body else str(row.get("agent_model") or "")
        next_think = think_level if think_keys else str(row.get("agent_think_level") or "")
        # Explicit empty string clears conversation override and falls back.
        if "model" in body and not model:
            next_model = ""
        if think_keys and not str(think_raw or "").strip():
            next_think = ""
        main_model = str(row.get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
        effective_agent_model = next_model or main_model
        if not think_keys and next_think not in self._model_thinking_levels(effective_agent_model):
            next_think = ""
        if not fast_keys and agent_fast_mode == 1 and not self._model_supports_fast(effective_agent_model):
            agent_fast_mode = -1

        async with self.db.conn.transaction(label="web-conversation-agent-config-and-defaults") as conn:
            await conn.execute(
                """
                UPDATE web_conversations
                SET agent_model=?, agent_think_level=?, agent_fast_mode=?, updated_at=?
                WHERE conversation_uuid=? AND owner_chat_id=?
                """,
                (
                    next_model,
                    next_think,
                    int(agent_fast_mode),
                    now_ts(),
                    str(row["conversation_uuid"]),
                    session.chat_id,
                ),
            )
            await WebConversationDefaultsDAO(self.db).patch_or_seed(
                session.chat_id,
                {
                    "agent_model": next_model,
                    "agent_think_level": next_think,
                    "agent_fast_mode": int(agent_fast_mode),
                },
                self._web_builtin_run_defaults(),
            )
        row["agent_model"] = next_model
        row["agent_think_level"] = next_think
        row["agent_fast_mode"] = int(agent_fast_mode)

        run_config = await self._conversation_run_config_public(session.chat_id, str(row["conversation_uuid"]))
        payload = run_config["agentRunConfig"]
        await self.audit(
            "web.conversation.agent_run_config",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"conversationUuid": row.get("conversation_uuid"), "agentRunConfig": payload, "nextRun": running},
        )
        return web.json_response({"ok": True, "agentRunConfig": payload, "nextRun": running, "runConfig": run_config})

    @staticmethod
    def _background_task_display_name(task: Any) -> str:
        snapshot = task.input.get("agentSnapshot") if isinstance(getattr(task, "input", None), dict) else {}
        short_id = str(getattr(task, "task_uuid", "") or "")[:8]
        base_name = str((snapshot or {}).get("name") or getattr(task, "current_agent_key", "") or "Agent").strip() or "Agent"
        return f"{base_name}-{short_id}" if short_id else base_name

    async def _submit_telegram_reply(self, row: dict[str, Any], text: str, *, submission_id: int) -> dict[str, Any]:
        return await self._start_or_steer_web_conversation(
            row, text, [], self._live_for(row), telegram_submission_id=submission_id,
        )

    async def _start_or_steer_web_conversation(self, row: dict[str, Any], text: str, media: list[InboundMedia], live: _WebLiveStream, *, telegram_submission_id: int = 0, reference_order: list[str] | None = None) -> dict[str, Any]:
        internal_chat_id = int(row["internal_chat_id"])
        # Serialize acceptance while preserving the existing steering behavior.
        async with self.operation_locks.chat_unless(internal_chat_id, "web_send", reject_operation="web_manual_compact") as acquired:
            if not acquired:
                return {"ok": False, "error": "busy", "message": "上下文压缩中，请稍后发送。"}
            if telegram_submission_id:
                current = await self._conversation_row(int(row["owner_chat_id"]), str(row["conversation_uuid"]))
                if (
                    not self.config.web.enabled
                    or int(row["owner_chat_id"]) not in self.config.telegram.whitelist_ids
                    or not current
                    or int(current.get("archived_at") or 0)
                ):
                    return {"ok": False, "error": "conversation_unavailable"}
                row = current
            return await self._start_or_steer_web_conversation_locked(
                row, text, media, live, telegram_submission_id=telegram_submission_id, reference_order=reference_order,
            )

    async def _web_media_attachments_public(self, row: dict[str, Any], media: list[InboundMedia], *, turn_uuid: str = "", op_id: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        conv_uuid = str(row.get("conversation_uuid") or "")
        for idx, item in enumerate(media or [], 1):
            file_name = item.file_name or f"attachment_{idx}"
            public: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "kind": item.kind,
                "fileName": file_name,
                "mimeType": item.mime_type or _guess_mime(file_name, ""),
                "sizeBytes": int(item.size or 0),
                "error": item.error or "",
                "skipped": bool(item.skipped),
            }
            if item.path and conv_uuid and not item.skipped:
                try:
                    artifact = None
                    if item.upload_type == "web_upload" and item.artifact_uuid:
                        cur = await self.db.conn.execute(
                            "SELECT * FROM web_artifacts WHERE artifact_uuid=? AND conversation_uuid=? AND owner_chat_id=? AND deleted_at=0",
                            (item.artifact_uuid, conv_uuid, int(row["owner_chat_id"])),
                        )
                        stored = await cur.fetchone()
                        artifact = self._web_artifact_public(dict(stored), conv_uuid) if stored else None
                        if stored and not stored["turn_uuid"]:
                            await self.db.conn.execute(
                                "UPDATE web_artifacts SET turn_uuid=?, op_id=? WHERE artifact_uuid=? AND turn_uuid=''",
                                (turn_uuid, op_id, item.artifact_uuid),
                            )
                            await self.db.conn.commit()
                    if artifact is None:
                        # A previous message may have been deleted after upload;
                        # an explicit resend must still get a live attachment URL.
                        artifact = await self._register_web_artifact_from_path(Path(item.path), conversation=row, turn_uuid=turn_uuid, op_id=op_id)
                except Exception:
                    artifact = None
                    log.exception("Web 上传附件注册 artifact 失败", 会话=conv_uuid, 文件=file_name)
                if artifact:
                    public.update({
                        "artifactUuid": artifact.get("artifactUuid") or "",
                        "contentUrl": artifact.get("contentUrl") or "",
                        "previewUrl": artifact.get("previewUrl") or artifact.get("contentUrl") or "",
                        "downloadUrl": artifact.get("downloadUrl") or "",
                        "inlinePreview": bool(artifact.get("inlinePreview")),
                    })
            out.append(public)
        return out

    async def _start_or_steer_web_conversation_locked(self, row: dict[str, Any], text: str, media: list[InboundMedia], live: _WebLiveStream, *, telegram_submission_id: int = 0, reference_order: list[str] | None = None) -> dict[str, Any]:
        internal_chat_id = int(row["internal_chat_id"])
        conv_uuid = str(row.get("conversation_uuid") or "")
        turn_uuid = str(uuid.uuid4())
        user_message_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"openbear:tg:{conv_uuid}:{telegram_submission_id}")) if telegram_submission_id else str(uuid.uuid4())
        source = "telegram" if telegram_submission_id else "web"
        input_metadata = {"source": "telegram", "telegramSubmissionId": telegram_submission_id} if telegram_submission_id else {}
        visible_user_text = (text or "").strip() or ("请根据我发送的附件内容回答。" if media else "")
        attachments_public: list[dict[str, Any]] = []
        active_background_tasks = []
        if self.rath_dao is not None:
            with contextlib.suppress(Exception):
                active_background_tasks = await self.rath_dao.active_tasks_for_chat(internal_chat_id, limit=100, controllable=True)
        active_background_tasks = [
            task_row for task_row in active_background_tasks
            if not conv_uuid or str(getattr(task_row, "parent_session_uuid", "") or "") == conv_uuid
        ]
        # Do not queue a new user message behind a stale operation whose runner
        # has already ended. A known unfinished task may still be registering its
        # runner; preserve its existing same-root interruption path.
        if not active_background_tasks:
            await self._reconcile_inactive_web_conversation_operations(row, source="conversation_send_reconcile")
        active_round = await self._web_active_round_info(conv_uuid, internal_chat_id)
        # A stranded steering queue alone cannot consume another interruption.
        # Start a controller below and leave the old messages queued for it.
        pending_only = set(active_round.get("activeReasons") or []) == {"steering"}
        if active_round.get("active") and not pending_only and media:
            return {"ok": False, "error": "attachments_while_running_not_supported"}
        try:
            reference_bundle_id, reference_manifest = await self._prepare_reference_bundle(row, text, f"msg:{user_message_uuid}", existing_keys=reference_order)
        except ReferenceError as exc:
            return {"ok": False, "error": exc.code, "referenceError": exc.public()}
        if reference_manifest:
            # Only reference destinations change. Publish/persist the effective
            # mode so history, copying and the model agree after source growth.
            text = effective_reference_text(text, reference_manifest)
            visible_user_text = effective_reference_text(visible_user_text, reference_manifest)
            input_metadata.update(referenceBundleId=reference_bundle_id, references=reference_manifest)
        # A detached Agent does not create a new visible turn. The main
        # controller stays alive in an event-driven wait inside the original root
        # turn, so interruptions use the normal steering queue and wake it now.
        if active_round.get("active") and not pending_only:
            if media:
                return {"ok": False, "error": "attachments_while_running_not_supported"}
            root_turn_uuid = str(active_round.get("rootTurnUuid") or "").strip() or await self._latest_visible_root_turn_uuid(conv_uuid) or turn_uuid
            if telegram_submission_id:
                await self.web_task_telegram.enable_direct_reply(row, root_turn_uuid)
            # Composer interruptions always target the main controller.  The
            # model may then decide to call AgentMessage/AgentStop, but the Web
            # routing layer never interprets or forwards the user's text itself.
            item = steering.enqueue(
                internal_chat_id,
                text,
                visibleText=visible_user_text,
                turnUuid=root_turn_uuid,
                rootTurnUuid=root_turn_uuid,
                messageUuid=user_message_uuid,
                source=source,
                referenceBundleId=reference_bundle_id,
                references=reference_manifest,
            )
            # Wake the sleeping controller immediately. The message remains in
            # the steering queue and is consumed by the same Agent.run/root turn
            # at its safe boundary; it is never routed directly to a child Agent.
            wake_event = self._web_controller_wake_events.get(conv_uuid)
            if wake_event is not None:
                wake_event.set()
            await live.publish({
                "type": "queued",
                **input_metadata,
                "turnUuid": root_turn_uuid,
                "rootTurnUuid": root_turn_uuid,
                "messageUuid": user_message_uuid,
                "text": visible_user_text,
                "status": "已追加到当前轮",
                "activeReasons": active_round.get("activeReasons") if isinstance(active_round.get("activeReasons"), list) else [],
            })
            pending_items = steering.pending_items(internal_chat_id)
            await live.publish({
                "type": "pending_steering",
                "action": "snapshot",
                "items": pending_items,
                "addedItem": item or {},
                "rootTurnUuid": root_turn_uuid,
            })
            await self._touch_web_conversation(conv_uuid, current_status="已追加到当前轮")
            return {"ok": True, "queued": True, "pendingSteering": pending_items, "rootTurnUuid": root_turn_uuid, "activeRound": active_round}

        background_control_payload: dict[str, Any] | None = None
        if active_background_tasks:
            background_tasks_payload = []
            for task_row in active_background_tasks:
                task_uuid = str(getattr(task_row, "task_uuid", "") or "")
                short_id = task_uuid[:8]
                task_payload = {
                    "taskUuid": task_uuid,
                    "taskShortId": short_id,
                    "displayName": self._background_task_display_name(task_row),
                    "title": getattr(task_row, "title", "") or "",
                    "status": getattr(task_row, "status", "") or "",
                    "currentStatus": getattr(task_row, "current_status", "") or "",
                    "agentSessionUuid": getattr(task_row, "agent_session_uuid", "") or "",
                }
                coordinator = getattr(self.rath, "plan_coordinator", None) if self.rath is not None else None
                if coordinator is not None and task_uuid:
                    with contextlib.suppress(Exception):
                        plan_snapshot = await coordinator.snapshot(task_uuid)
                        plan_state = plan_snapshot.get("state") if isinstance(plan_snapshot.get("state"), dict) else {}
                        active_version = int(plan_state.get("active_plan_version") or 0)
                        pending_version = int(plan_state.get("pending_plan_version") or 0)
                        if active_version or pending_version:
                            task_payload["planRuntime"] = {
                                "phase": str(plan_state.get("phase") or ""),
                                "activePlanVersion": active_version,
                                "pendingPlanVersion": pending_version,
                                "currentStepId": str(plan_state.get("current_step_id") or ""),
                            }
                background_tasks_payload.append(task_payload)
            background_control_payload = {
                "activeBackgroundTasks": background_tasks_payload,
                "routing": (
                    "User interruption belongs to the main OpenBear conversation first. "
                    "Do not forward it blindly. Use AgentMessage/AgentStop only when the user clearly wants "
                    "to control a specific background task; if multiple tasks match ambiguously, ask a brief clarification."
                ),
            }

        if conv_uuid:
            self._web_stop_markers.pop(conv_uuid, None)
            self._web_stopped_task_uuids.pop(conv_uuid, None)
        attachments_public = await self._web_media_attachments_public(row, media or [], turn_uuid=turn_uuid, op_id=f"msg:{user_message_uuid}")
        renderer = _WebStreamRenderer(
            live=live,
            artifact_rewriter=self._web_assistant_artifact_rewriter(row, turn_uuid=turn_uuid),
        )
        starting_turns = self._web_starting_turns.setdefault(conv_uuid, set()) if conv_uuid else None
        if starting_turns is not None:
            starting_turns.add(turn_uuid)
        try:
            if telegram_submission_id:
                # Persist the return channel before starting any Agent side effect.
                await self.web_task_telegram.enable_direct_reply(row, turn_uuid)
            await live.publish({"type": "accepted", "chatId": internal_chat_id, "turnUuid": turn_uuid, "runUuid": turn_uuid, **input_metadata})
            await live.publish({
                "type": "user",
                **input_metadata,
                "turnUuid": turn_uuid,
                "messageUuid": user_message_uuid,
                "text": visible_user_text,
                "attachments": attachments_public,
            })
            task = asyncio.create_task(self._run_web_turn(
                internal_chat_id,
                text,
                renderer,
                media=media,
                conversation=row,
                background_control_payload=background_control_payload,
                root_turn_uuid=turn_uuid,
                user_op_id=f"msg:{user_message_uuid}",
                **({"reference_bundle_id": reference_bundle_id} if reference_bundle_id else {}),
            ))
            if self.runs is not None:
                self.runs.register(internal_chat_id, task)
        finally:
            if starting_turns is not None:
                starting_turns.discard(turn_uuid)
                if not starting_turns:
                    self._web_starting_turns.pop(conv_uuid, None)
        return {"ok": True, "queued": False, **({"rootTurnUuid": turn_uuid} if telegram_submission_id else {})}

    async def handle_api_conversation_ws(self, request: web.Request) -> web.WebSocketResponse:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        live = self._live_for(row)
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=128 * 1024 * 1024)
        await ws.prepare(request)
        try:
            after_frame_seq = max(0, int(request.query.get("afterFrameSeq") or 0))
        except (TypeError, ValueError):
            after_frame_seq = 0
        incremental_bootstrap = str(request.query.get("bootstrap") or "").strip().lower() == "incremental"
        # Subscribe before reading the durable frame high-water. Anything already
        # committed is recovered from SQL; anything committed afterwards is in
        # this queue. Queue/SQL overlap is removed by frameSeq below.
        sub = live.subscribe()
        ws_id = str(uuid.uuid4())
        send_index = 0
        last_sent_frame_seq = after_frame_seq
        conv_uuid_for_log = str(row.get("conversation_uuid") or "")
        internal_chat_id_for_log = int(row.get("internal_chat_id") or 0)
        owner_chat_id_for_log = int(session.chat_id or 0)

        _log_web_ws_audit({
            "stage": "ws.open",
            "conversationUuid": conv_uuid_for_log,
            "chatId": internal_chat_id_for_log,
            "ownerChatId": owner_chat_id_for_log,
            "wsId": ws_id,
            "afterFrameSeq": after_frame_seq,
            "remote": str(request.remote or ""),
            "path": str(request.rel_url),
        })

        async def _send_json(payload: dict[str, Any]) -> None:
            nonlocal send_index
            if ws.closed:
                return
            send_index += 1
            frame_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            frame_type = str(payload.get("type") or "")
            frame = payload.get("frame") if isinstance(payload.get("frame"), dict) else None
            state = payload.get("state") if isinstance(payload.get("state"), dict) else None
            frame_seq_value = int((frame or {}).get("frameSeq") or ((state or {}).get("frameSeq") if isinstance(state, dict) else 0) or 0)
            turn_uuid = str((frame or {}).get("turnId") or (frame or {}).get("turnUuid") or "")
            op_id = str((frame or {}).get("opId") or "")
            op_type = str((frame or {}).get("opType") or "")
            op_action = str((frame or {}).get("action") or "")
            revision = int((frame or {}).get("revision") or 0)
            display_seq = int((frame or {}).get("displaySeq") or 0)
            audit_base = {
                "conversationUuid": conv_uuid_for_log,
                "chatId": internal_chat_id_for_log,
                "ownerChatId": owner_chat_id_for_log,
                "wsId": ws_id,
                "sendIndex": send_index,
                "frameType": frame_type,
                "eventType": "",
                "frameSeq": frame_seq_value,
                "turnUuid": turn_uuid,
                "eventUuid": "",
                "opId": op_id,
                "opType": op_type,
                "action": op_action,
                "revision": revision,
                "displaySeq": display_seq,
                "byteLength": len(frame_text.encode("utf-8")),
                "sha256": hashlib.sha256(frame_text.encode("utf-8")).hexdigest(),
                "payload": payload,
                "frameText": frame_text,
            }
            _log_web_ws_audit({"stage": "ws.send.attempt", **audit_base})
            try:
                await ws.send_str(frame_text)
            except Exception as exc:
                _log_web_ws_audit({"stage": "ws.send.error", **audit_base, "error": repr(exc)})
                raise
            _log_web_ws_audit({"stage": "ws.send.ok", **audit_base})
            _log_web_frontend_event({
                "stage": "ws.send",
                "conversationUuid": conv_uuid_for_log,
                "chatId": internal_chat_id_for_log,
                "ownerChatId": owner_chat_id_for_log,
                "wsId": ws_id,
                "sendIndex": send_index,
                "frameType": frame_type,
                "eventType": "",
                "frameSeq": frame_seq_value,
                "turnUuid": turn_uuid,
                "eventUuid": "",
                "opId": op_id,
                "opType": op_type,
                "action": op_action,
                "revision": revision,
                "displaySeq": display_seq,
                "payload": payload,
            })

        async def _send_frame_once(frame: dict[str, Any]) -> bool:
            nonlocal last_sent_frame_seq
            frame_seq = int(frame.get("frameSeq") or 0)
            if frame_seq > 0 and frame_seq <= last_sent_frame_seq:
                return False
            await _send_json({"type": "frame", "frame": frame})
            if frame_seq > 0:
                last_sent_frame_seq = frame_seq
            return True

        async def _writer() -> None:
            try:
                while True:
                    event = await sub.get()
                    if event.get("_webLiveStreamControl") == "overflow":
                        _log_web_ws_audit({
                            "stage": "ws.queue_overflow",
                            "conversationUuid": conv_uuid_for_log,
                            "chatId": internal_chat_id_for_log,
                            "ownerChatId": owner_chat_id_for_log,
                            "wsId": ws_id,
                            "sendCount": send_index,
                        })
                        # 1013 tells the browser this connection cannot keep up.
                        # ConsoleView reconnects with its last applied frameSeq;
                        # the initial state snapshot then restores every skipped op.
                        await ws.close(code=1013, message=b"subscriber queue overflow; reconnect")
                        return
                    event_type = str(event.get("type") or "")
                    if event_type == "task_memory.changed":
                        public_event = task_memory_changed_public_event(event)
                        if public_event is not None:
                            await _send_json(public_event)
                    elif event_type == "message_visibility.changed":
                        await _send_json({"type": event_type, "visibility": event.get("visibility")})
                    elif event_type == "web_confirmation":
                        await _send_json({
                            "type": "web_confirmation",
                            "action": event.get("action") or "",
                            "confirmationId": event.get("confirmationId") or "",
                            "confirmations": event.get("confirmation") if isinstance(event.get("confirmation"), list) else [],
                        })
                    elif event_type == "pending_steering":
                        await _send_json({
                            "type": "pending_steering",
                            "action": event.get("action") or "snapshot",
                            "items": event.get("items") if isinstance(event.get("items"), list) else [],
                            "itemIds": event.get("itemIds") if isinstance(event.get("itemIds"), list) else [],
                            "addedItem": event.get("addedItem") if isinstance(event.get("addedItem"), dict) else {},
                        })
                    frames = event.get("_webFrames") if isinstance(event.get("_webFrames"), list) else []
                    for frame in frames:
                        if isinstance(frame, dict):
                            await _send_frame_once(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("WebSocket event writer failed", 会话=row.get("conversation_uuid"))

        async def _send_incremental_bootstrap() -> None:
            nonlocal last_sent_frame_seq
            cur = await self.db.conn.execute(
                "SELECT COALESCE(MAX(frame_seq), 0) AS frame_seq FROM web_event_frames WHERE conversation_uuid=?",
                (conv_uuid_for_log,),
            )
            high_row = await cur.fetchone()
            bootstrap_high_water = int(high_row["frame_seq"] or 0) if high_row else 0
            cursor = last_sent_frame_seq
            if cursor > bootstrap_high_water:
                await _send_json({
                    "type": "resync_required",
                    "afterFrameSeq": cursor,
                    "frameSeq": bootstrap_high_water,
                    "resetOperations": True,
                })
                last_sent_frame_seq = bootstrap_high_water
                return
            while cursor < bootstrap_high_water:
                frames = await self._web_frames(
                    conv_uuid_for_log,
                    after_frame_seq=cursor,
                    up_to_frame_seq=bootstrap_high_water,
                    limit=1000,
                )
                if not frames:
                    # A reconnect cursor can outlive the retained frame window.
                    # Ask the new client for a bounded operation snapshot rather
                    # than advancing the cursor across an unobservable gap.
                    await _send_json({
                        "type": "resync_required",
                        "afterFrameSeq": cursor,
                        "frameSeq": bootstrap_high_water,
                    })
                    last_sent_frame_seq = bootstrap_high_water
                    return
                first_seq = int(frames[0].get("frameSeq") or 0)
                if first_seq > cursor + 1:
                    await _send_json({
                        "type": "resync_required",
                        "afterFrameSeq": cursor,
                        "frameSeq": bootstrap_high_water,
                    })
                    last_sent_frame_seq = bootstrap_high_water
                    return
                for frame in frames:
                    await _send_frame_once(frame)
                next_cursor = int(frames[-1].get("frameSeq") or cursor)
                if next_cursor <= cursor:
                    break
                cursor = next_cursor

        writer: asyncio.Task[Any] | None = None
        try:
            if incremental_bootstrap:
                # New clients already own the HTTP state snapshot. Send only the
                # durable gap and small non-frame interaction snapshots; never
                # repeat state or the unrelated conversation list.
                await _send_incremental_bootstrap()
                await _send_json({
                    "type": "bootstrap",
                    "messageVisibility": await visibility_snapshot(self.db, conv_uuid_for_log),
                    "frameSeq": last_sent_frame_seq,
                    "pendingConfirmations": self._pending_web_confirmations(conv_uuid_for_log),
                    "pendingSteering": steering.pending_items(internal_chat_id_for_log),
                })
            else:
                # Legacy clients retain the original full-state bootstrap.
                state_payload = await self._chat_payload(int(row["internal_chat_id"]), row)
                # A suffix delete can legitimately lower this conversation's
                # durable frame high-water. The full state replaces all prior
                # client facts, so its cursor is authoritative even when lower
                # than the reconnect query's stale afterFrameSeq.
                last_sent_frame_seq = int(state_payload.get("frameSeq") or 0)
                await _send_json({"type": "state", "state": state_payload, "conversations": await self._list_web_conversations(session.chat_id, limit=100)})
            writer = asyncio.create_task(_writer())
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data or "{}")
                    except Exception:
                        await _send_json({"type": "error", "error": "bad_json"})
                        continue
                    if not isinstance(data, dict):
                        await _send_json({"type": "error", "error": "bad_json"})
                        continue
                    kind = str(data.get("type") or "")
                    if kind == "ping":
                        await _send_json({"type": "pong", "ts": now_ts()})
                    elif kind == "refresh":
                        row = await self._conversation_row(session.chat_id, str(row["conversation_uuid"]), require=True)  # type: ignore[assignment]
                        await _send_json({"type": "state", "state": await self._chat_payload(int(row["internal_chat_id"]), row), "conversations": await self._list_web_conversations(session.chat_id, limit=100)})
                    elif kind == "stop":
                        result = await self._stop_web_conversation(row, message="已停止")
                        if result.get("ok"):
                            await _send_json({"type": "stopped", **result})
                        else:
                            await _send_json({"type": "error", "error": result.get("error") or "stop_failed"})
                    elif kind == "send":
                        # The WebSocket owns its initial row snapshot for its whole
                        # lifetime. Refresh here so a model/configuration change made
                        # through HTTP before this send is applied to the new run.
                        request_id = str(data.get("requestId") or "").strip()[:128]
                        row = await self._conversation_row(session.chat_id, str(row["conversation_uuid"]), require=True)  # type: ignore[assignment]
                        text = str(data.get("text") or "").strip()
                        files = data.get("files") if isinstance(data.get("files"), list) else []
                        try:
                            media = await self._resolve_http_uploads(files, conversation=row) if files else []
                        except web.HTTPException as exc:
                            await _send_json({"type": "error", "error": json.loads(exc.text)["error"], "requestId": request_id})
                            continue
                        if not text and not media:
                            await _send_json({"type": "error", "error": "empty_text", "requestId": request_id})
                            continue
                        reference_order = data.get("referenceOrder")
                        reference_kwargs = {"reference_order": [key for key in reference_order if isinstance(key, str)]} if isinstance(reference_order, list) else {}
                        result = await self._start_or_steer_web_conversation(row, text, media, live, **reference_kwargs)
                        if not result.get("ok"):
                            await _send_json({"type": "error", "error": result.get("error") or "send_failed", "requestId": request_id, **({"referenceError": result["referenceError"]} if result.get("referenceError") else {})})
                        else:
                            await _send_json({"type": "ack", "requestId": request_id, **result})
                    else:
                        await _send_json({"type": "error", "error": "unknown_command"})
                elif msg.type == web.WSMsgType.ERROR:
                    log.warning("WebSocket closed with error", 错误=str(ws.exception()))
                    break
        finally:
            _log_web_ws_audit({
                "stage": "ws.close",
                "conversationUuid": conv_uuid_for_log,
                "chatId": internal_chat_id_for_log,
                "ownerChatId": owner_chat_id_for_log,
                "wsId": ws_id,
                "sendCount": send_index,
                "closed": bool(ws.closed),
                "closeCode": getattr(ws, "close_code", None),
                "exception": repr(ws.exception()) if ws.exception() else "",
            })
            live.unsubscribe(sub)
            if writer is not None:
                writer.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await writer
        return ws

__all__ = [name for name in globals() if not name.startswith("__")]
