# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.task_memory import TaskMemoryDAO, task_memory_changed_public_event
from app.web_console.core import *
from app.web_console.live_stream import *


def _web_option_label_value(option: Any) -> dict[str, str]:
    if isinstance(option, dict):
        label = str(option.get("label") or option.get("text") or option.get("value") or "")
        value = str(option.get("value") if option.get("value") is not None else label)
    else:
        label = str(option or "")
        value = label
    return {"label": label, "value": value}


def _confirmation_answer_audit_result(
    action: str, item: dict[str, Any], result: dict[str, Any],
) -> dict[str, Any]:
    if action == "prompt" and bool(item.get("sensitive") or item.get("secret")):
        audit_result = dict(result)
        if "value" in audit_result:
            audit_result["value"] = "[敏感内容已隐藏]"
        audit_result["sensitiveRedacted"] = True
        return audit_result
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


def _canonical_questionnaire_answers(
    questions: list[dict[str, Any]], raw_answers: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not isinstance(raw_answers, list):
        return [], [{"path": "answers", "message": "answers must be an array"}]
    errors: list[dict[str, str]] = []
    questions_by_id = {str(question.get("id") or ""): question for question in questions}
    submitted: dict[str, tuple[list[str], str]] = {}
    for answer_index, raw_answer in enumerate(raw_answers):
        path = f"answers[{answer_index}]"
        if not isinstance(raw_answer, dict):
            errors.append({"path": path, "message": "answer must be an object"})
            continue
        raw_question_id = raw_answer.get("questionId")
        if not isinstance(raw_question_id, str) or not raw_question_id.strip():
            errors.append({"path": f"{path}.questionId", "message": "questionId must be a non-empty string"})
            continue
        question_id = raw_question_id.strip()
        if question_id in submitted:
            errors.append({"path": f"{path}.questionId", "message": f"duplicate questionId: {question_id}"})
            continue
        question = questions_by_id.get(question_id)
        if question is None:
            errors.append({"path": f"{path}.questionId", "message": f"unknown questionId: {question_id}"})
            continue
        raw_selected_values = raw_answer.get("selectedValues", [])
        if not isinstance(raw_selected_values, list) or any(not isinstance(value, str) for value in raw_selected_values):
            errors.append({"path": f"{path}.selectedValues", "message": "selectedValues must be an array of strings"})
            selected_values: list[str] = []
        else:
            selected_values = list(raw_selected_values)
        if len(set(selected_values)) != len(selected_values):
            errors.append({"path": f"{path}.selectedValues", "message": "selectedValues must not contain duplicates"})
        raw_text = raw_answer.get("text", "")
        if not isinstance(raw_text, str):
            errors.append({"path": f"{path}.text", "message": "text must be a string"})
            raw_text = ""
        text = raw_text if raw_text.strip() else ""
        if question.get("type") == "choice":
            option_values = {str(option.get("value")): option for option in question.get("options") or []}
            for value in selected_values:
                if value not in option_values:
                    errors.append({"path": f"{path}.selectedValues", "message": f"unknown option value for {question_id}: {value}"})
            if not question.get("multiple") and len(selected_values) > 1:
                errors.append({"path": f"{path}.selectedValues", "message": f"question {question_id} allows only one selected value"})
        elif selected_values:
            errors.append({"path": f"{path}.selectedValues", "message": f"open question {question_id} does not accept option values"})
        submitted[question_id] = (selected_values, text)

    canonical: list[dict[str, Any]] = []
    for question in questions:
        question_id = str(question.get("id") or "")
        selected_values, text = submitted.get(question_id, ([], ""))
        if question.get("required") and not selected_values and not text:
            errors.append({"path": f"answers.{question_id}", "message": f"required question is unanswered: {question_id}"})
        options_by_value = {
            str(option.get("value")): option for option in question.get("options") or []
        }
        selected_labels = [str(options_by_value[value].get("label") or "") for value in selected_values if value in options_by_value]
        if selected_values and text:
            answer_mode = "options_with_text"
        elif selected_values:
            answer_mode = "options"
        elif text:
            answer_mode = "text"
        else:
            answer_mode = "unanswered"
        canonical.append({
            "questionId": question_id,
            "type": question.get("type") or "open",
            "question": question.get("question") or "",
            "required": bool(question.get("required")),
            "answerMode": answer_mode,
            "selectedValues": selected_values,
            "selectedLabels": selected_labels,
            "text": text,
        })
    return canonical, errors


class WebAdminChatHandlersMixin:
    _WEB_DEFAULT_FIELDS = {
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
            "main_model": str(defaults.get("mainModel") or ""),
            "main_thinking_level": str(defaults.get("mainThinkingLevel") or ""),
            "main_fast_mode": 1 if defaults.get("mainFastMode") is True else 0,
            "agent_model": str(defaults.get("agentModel") or ""),
            "agent_think_level": str(defaults.get("agentThinkLevel") or ""),
            "agent_fast_mode": -1 if defaults.get("agentFastMode") is None else (1 if defaults.get("agentFastMode") is True else 0),
        }

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
        if require_complete and set(body) != set(self._WEB_DEFAULT_FIELDS):
            return None, ("run_config_incomplete", 400)
        if not body:
            return None, ("nothing_to_update", 400)

        merged = dict(current)
        for key, value in body.items():
            if key in {"mainModel", "mainThinkingLevel", "agentModel", "agentThinkLevel"}:
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
        _row, defaults = await self._web_run_defaults(session.chat_id)
        return web.json_response({"ok": True, "defaults": defaults})

    async def handle_api_conversation_defaults_patch(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
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
        _defaults_row, current = await self._web_run_defaults_candidate(session.chat_id)

        run_config_raw = body.get("runConfig")
        persist_defaults = run_config_raw is not None or "model" in body
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
                persist_defaults = True
            normalized = self._normalize_web_run_defaults({**self._web_defaults_storage(selected), "revision": 0, "updated_at": 0})

        storage = self._web_defaults_storage(normalized)
        row = await self._create_web_conversation(
            session.chat_id,
            title=title,
            model=normalized["mainModel"],
            run_config=storage,
            persist_defaults=persist_defaults,
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
        if await self.operation_locks.current_operation(internal_chat_id) == "web_manual_compact":
            return {"ok": False, "error": "conversation_compacting"}
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
        published_stop = False
        if stopped_run or stopped_tasks or live.status == "running":
            await live.publish({"type": "stopped", "reason": message, "stopAtMs": stop_at_ms})
            published_stop = True
        if conv_uuid:
            await self._touch_web_conversation(
                conv_uuid,
                status="idle",
                current_status="已停止" if published_stop else "就绪",
            )
        return {"ok": True, "stoppedRun": stopped_run, "stoppedTasks": stopped_tasks, "stoppedProcesses": killed_processes}

    async def handle_api_conversation_compact(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        chat_id = int(row.get("internal_chat_id") or 0)
        conv_uuid = str(row.get("conversation_uuid") or "")
        await self._reconcile_inactive_web_conversation_operations(
            row,
            source="manual_compaction_preflight",
        )
        row = await self._conversation_row(session.chat_id, conv_uuid, require=True)  # type: ignore[assignment]
        if await self._web_conversation_has_active_runtime(row):
            return web.json_response({"ok": False, "error": "busy"}, status=409)

        live = self._live_for(row)
        op_id = f"manual-compact:{uuid.uuid4()}"

        async def _publish_compaction_state(
            action: str,
            status: str,
            lifecycle: str,
            payload: dict[str, Any],
        ) -> None:
            operation_payload = {**payload, "source": "manual", "internal": False}
            await live.publish({
                "type": "context_compaction_state",
                "internal": False,
                "_webOperationSpecs": [{
                    "op_id": op_id,
                    "op_type": "context_compaction",
                    "action": action,
                    "payload": operation_payload,
                    "status": status,
                    "lifecycle": lifecycle,
                    "source": "manual",
                    "internal": False,
                }],
            })

        async with self.operation_locks.try_chat(chat_id, "web_manual_compact") as acquired:
            if not acquired:
                return web.json_response({"ok": False, "error": "busy"}, status=409)
            # The lock closes the check/start race. Re-read every authoritative
            # runtime source before invoking the long-running compression model.
            row = await self._conversation_row(session.chat_id, conv_uuid, require=True)  # type: ignore[assignment]
            if await self._web_conversation_has_active_runtime(row):
                return web.json_response({"ok": False, "error": "busy"}, status=409)
            messages = MessageDAO(self.db)
            session_uuid = await messages.get_or_create_session_uuid(chat_id)
            tokens = await messages.latest_controller_context_usage(
                chat_id,
                session_uuid=session_uuid,
            )
            model_label = str(row.get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
            trigger = self._model_compact_trigger_tokens(model_label)
            minimum = int(self.config.agent.manual_compact_min_percent)
            if tokens is None or trigger <= 0:
                return web.json_response({"ok": False, "error": "context_usage_unknown"}, status=409)
            if int(tokens) * 100 < trigger * minimum:
                return web.json_response({"ok": False, "error": "below_threshold", "tokens": tokens, "requiredPercent": minimum}, status=409)

            await _publish_compaction_state(
                "start",
                "running",
                "active",
                {"beforeTokens": int(tokens)},
            )
            try:
                compactor = self._make_web_compactor(chat_id, model_label=model_label)
                outcome = await compactor._force_compact_unlocked(chat_id, source="manual")
                if outcome.did:
                    clear_read_file_state(chat_id=chat_id)
                    await self._invalidate_web_controller_context_usage(
                        chat_id,
                        session_uuid=session_uuid,
                    )
                await _publish_compaction_state(
                    "end",
                    "completed" if outcome.did else "unavailable",
                    "terminal",
                    {**self._context_compaction_json(outcome), "beforeTokens": int(tokens)},
                )
            except Exception as exc:
                await _publish_compaction_state(
                    "error",
                    "failed",
                    "terminal",
                    {"error": str(exc)[:200]},
                )
                raise
        await self.audit("web.conversation.compact", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": conv_uuid, "did": outcome.did})
        return web.json_response({"ok": True, "outcome": self._context_compaction_json(outcome), "state": await self._chat_payload(chat_id, row)})

    async def handle_api_conversation_stop(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        result = await self._stop_web_conversation(row)
        await self.audit("web.conversation.stop", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid"), **result})
        return web.json_response(result, status=200 if result.get("ok") else 409)

    async def handle_api_conversation_retry_cancel(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        task_uuid = str(body.get("taskUuid") or "").strip()
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
                accepted = bool(self.rath is not None and self.rath.request_retry_cancel(task_uuid))
        elif self.control_actions is not None:
            live = self._live_for(row)
            retry_state = getattr(live, "active_retry", {})
            if isinstance(retry_state, dict) and retry_state.get("active"):
                self.control_actions.request_retry_cancel(internal_chat_id)
                accepted = True
        await self.audit(
            "web.conversation.retry.cancel",
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
        item = self._web_confirmations.get(confirmation_id)
        if not item or str(item.get("conversationUuid") or "") != conv_uuid:
            return web.json_response({"ok": False, "error": "confirmation_not_found"}, status=404)
        action = str(item.get("action") or "confirm")
        cancelled = bool(body.get("cancelled"))
        if action == "questionnaire":
            if cancelled:
                result = {
                    "status": "cancelled",
                    "cancelled": True,
                    "answers": [],
                    "interactionId": confirmation_id,
                }
            else:
                answers, errors = _canonical_questionnaire_answers(
                    item.get("questions") if isinstance(item.get("questions"), list) else [],
                    body.get("answers"),
                )
                if errors:
                    return web.json_response({
                        "ok": False,
                        "error": "invalid_questionnaire_answer",
                        "message": "Questionnaire answer validation failed",
                        "details": errors,
                    }, status=400)
                result = {
                    "status": "answered",
                    "cancelled": False,
                    "answers": answers,
                    "interactionId": confirmation_id,
                }
        elif action == "select":
            options = [_web_option_label_value(opt) for opt in item.get("options") or []]
            raw_indexes = body.get("selectedIndexes") if isinstance(body.get("selectedIndexes"), list) else []
            raw_values = body.get("selectedValues") if isinstance(body.get("selectedValues"), list) else []
            selected_indexes = {int(x) for x in raw_indexes if isinstance(x, int | float) or str(x).isdigit()}
            selected_values = {str(x) for x in raw_values}
            selected: list[tuple[int, dict[str, str]]] = []
            for idx, opt in enumerate(options):
                if idx in selected_indexes or opt["value"] in selected_values or opt["label"] in selected_values:
                    selected.append((idx, opt))
            if not item.get("multiple") and selected:
                selected = selected[:1]
            result = {
                "status": "cancelled" if cancelled else "answered",
                "cancelled": cancelled,
                "multiple": bool(item.get("multiple")),
                "selectedIndexes": [] if cancelled else [idx for idx, _opt in selected],
                "selectedValues": [] if cancelled else [opt["value"] for _idx, opt in selected],
                "selectedLabels": [] if cancelled else [opt["label"] for _idx, opt in selected],
                "interactionId": confirmation_id,
            }
        elif action == "prompt":
            result = {
                "status": "cancelled" if cancelled else "answered",
                "cancelled": cancelled,
                "value": "" if cancelled else str(body.get("value") or ""),
                "interactionId": confirmation_id,
            }
        else:
            confirmed = bool(body.get("confirmed")) and not cancelled
            result = {
                "status": "cancelled" if cancelled else "answered",
                "confirmed": confirmed,
                "choice": "confirm" if confirmed else "cancel",
                "label": item.get("confirmText") if confirmed else item.get("cancelText"),
                "interactionId": confirmation_id,
            }
        future = item.get("future")
        if future is not None and not future.done():
            future.set_result(result)
        audit_result = _confirmation_answer_audit_result(action, item, result)
        await self.audit("web.confirmation.answer", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": conv_uuid, "confirmationId": confirmation_id, "action": action, "result": audit_result})
        return web.json_response({"ok": True, "confirmationId": confirmation_id, "action": action, "result": result})

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
            title = title[:120]

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
        return web.json_response({"ok": True, "conversation": self._web_conversation_json(row, live=self._web_live_streams.get(conv_uuid))})

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
                SELECT conversation_uuid, pinned_at
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
            for neighbor_uuid in (before_uuid, after_uuid):
                if not neighbor_uuid:
                    continue
                neighbor = lookup.get(neighbor_uuid)
                if neighbor is None:
                    return web.json_response({"ok": False, "error": "reorder_neighbor_not_found"}, status=404)
                if (int(neighbor.get("pinned_at") or 0) > 0) != moving_pinned:
                    return web.json_response({"ok": False, "error": "conversation_reorder_group_mismatch"}, status=409)
            display_order = await self._reorder_web_conversation_display_group(
                session.chat_id,
                pinned=moving_pinned,
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
                    transcript_deleted = await MessageDAO(self.db).delete_from_message_id(
                        internal_chat_id, first_message_id,
                    )
                    rath_deleted = await self.rath_dao.delete_task_records(deleted_task_uuids)
                    if deleted_task_uuids:
                        task_placeholders = ",".join("?" for _ in deleted_task_uuids)
                        await conn.execute(
                            f"""
                            UPDATE rath_agent_sessions
                            SET last_task_uuid=COALESCE((
                              SELECT task_uuid FROM rath_tasks AS remaining
                              WHERE remaining.agent_session_uuid=rath_agent_sessions.session_uuid
                              ORDER BY remaining.updated_at DESC, remaining.id DESC LIMIT 1
                            ), ''), updated_at=?
                            WHERE chat_id=? AND last_task_uuid IN ({task_placeholders})
                            """,
                            (now, internal_chat_id, *deleted_task_uuids),
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

        self._web_task_notification_deferred.pop(conv_uuid, None)
        for confirmation_id in list(self._web_confirm_by_conversation.pop(conv_uuid, set())):
            item = self._web_confirmations.pop(confirmation_id, None)
            future = item.get("future") if isinstance(item, dict) else None
            if future is not None and not future.done():
                if item.get("action") == "questionnaire":
                    future.set_result({
                        "status": "cancelled",
                        "cancelled": True,
                        "answers": [],
                        "interactionId": confirmation_id,
                    })
                else:
                    future.set_result({"status": "cancelled", "confirmed": False, "choice": "cancel"})
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
        for confirmation_id in list(self._web_confirm_by_conversation.pop(conv_uuid, set())):
            self._web_confirmations.pop(confirmation_id, None)
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
        await self.audit("web.conversation.model", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid"), "model": model, "nextRun": running})
        return web.json_response({"ok": True, "model": model, "nextRun": running})

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
        await self.audit("web.conversation.thinking", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid"), "level": level, "nextRun": running})
        return web.json_response({"ok": True, "level": level, "nextRun": running})

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
        await self.audit("web.conversation.fast", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"conversationUuid": row.get("conversation_uuid"), "enabled": enabled, "nextRun": running})
        return web.json_response({"ok": True, "enabled": enabled, "effectiveFastMode": enabled and self._model_supports_fast(model), "nextRun": running})

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

        main_model = str(row.get("model") or "") or getattr(self.model_selection, "current", "") or self.config.models.primary
        main_fast = await MessageDAO(self.db).get_fast_mode(internal_chat_id)
        resolved = resolve_agent_runtime_config(
            None,
            config=self.config,
            model_selection_current=str(getattr(self.model_selection, "current", "") or ""),
            conversation=row,
            main_model=main_model,
            main_fast_requested=bool(main_fast),
        )
        payload = agent_run_config_public(resolved)
        await self.audit(
            "web.conversation.agent_run_config",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"conversationUuid": row.get("conversation_uuid"), "agentRunConfig": payload, "nextRun": running},
        )
        return web.json_response({"ok": True, "agentRunConfig": payload, "nextRun": running})

    @staticmethod
    def _background_task_display_name(task: Any) -> str:
        snapshot = task.input.get("agentSnapshot") if isinstance(getattr(task, "input", None), dict) else {}
        short_id = str(getattr(task, "task_uuid", "") or "")[:8]
        base_name = str((snapshot or {}).get("name") or getattr(task, "current_agent_key", "") or "Agent").strip() or "Agent"
        return f"{base_name}-{short_id}" if short_id else base_name

    async def _start_or_steer_web_conversation(self, row: dict[str, Any], text: str, media: list[InboundMedia], live: _WebLiveStream) -> dict[str, Any]:
        internal_chat_id = int(row["internal_chat_id"])
        # Sends may retain their existing serialization/steering behavior, but
        # must never queue behind manual compaction and arrive after it finishes.
        async with self.operation_locks.chat_unless(
            internal_chat_id, "web_send", reject_operation="web_manual_compact",
        ) as acquired:
            if not acquired:
                return {"ok": False, "error": "busy"}
            return await self._start_or_steer_web_conversation_locked(row, text, media, live)

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

    async def _start_or_steer_web_conversation_locked(self, row: dict[str, Any], text: str, media: list[InboundMedia], live: _WebLiveStream) -> dict[str, Any]:
        internal_chat_id = int(row["internal_chat_id"])
        conv_uuid = str(row.get("conversation_uuid") or "")
        turn_uuid = str(uuid.uuid4())
        user_message_uuid = str(uuid.uuid4())
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
        active_round = await self._web_active_round_info(conv_uuid, internal_chat_id)
        # A detached Agent does not create a new visible turn. The main
        # controller stays alive in an event-driven wait inside the original root
        # turn, so interruptions use the normal steering queue and wake it now.
        if active_round.get("active"):
            if media:
                return {"ok": False, "error": "attachments_while_running_not_supported"}
            root_turn_uuid = str(active_round.get("rootTurnUuid") or "").strip() or await self._latest_visible_root_turn_uuid(conv_uuid) or turn_uuid
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
                source="web",
            )
            # Wake the sleeping controller immediately. The message remains in
            # the steering queue and is consumed by the same Agent.run/root turn
            # at its safe boundary; it is never routed directly to a child Agent.
            wake_event = self._web_controller_wake_events.get(conv_uuid)
            if wake_event is not None:
                wake_event.set()
            await live.publish({
                "type": "queued",
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
            await live.publish({"type": "accepted", "chatId": internal_chat_id, "turnUuid": turn_uuid, "runUuid": turn_uuid})
            await live.publish({
                "type": "user",
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
            ))
            if self.runs is not None:
                self.runs.register(internal_chat_id, task)
        finally:
            if starting_turns is not None:
                starting_turns.discard(turn_uuid)
                if not starting_turns:
                    self._web_starting_turns.pop(conv_uuid, None)
        return {"ok": True, "queued": False}

    async def handle_api_conversation_ws(self, request: web.Request) -> web.WebSocketResponse:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        live = self._live_for(row)
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=64 * 1024 * 1024)
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
                await _send_json({"type": "state", "state": state_payload, "conversations": await self._list_web_conversations(session.chat_id)})
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
                        await _send_json({"type": "state", "state": await self._chat_payload(int(row["internal_chat_id"]), row), "conversations": await self._list_web_conversations(session.chat_id)})
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
                        media = await self._save_ws_uploads(files, chat_id=int(row["internal_chat_id"])) if files else []
                        if not text and not media:
                            await _send_json({"type": "error", "error": "empty_text", "requestId": request_id})
                            continue
                        result = await self._start_or_steer_web_conversation(row, text, media, live)
                        if not result.get("ok"):
                            await _send_json({"type": "error", "error": result.get("error") or "send_failed", "requestId": request_id})
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
