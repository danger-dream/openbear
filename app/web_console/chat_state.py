# ruff: noqa: F401,F403,F405
from __future__ import annotations

import inspect

from app.rath.controller_projection import project_history_message_for_controller
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


def _web_select_defaults(item: dict[str, Any]) -> list[tuple[int, dict[str, str]]]:
    options = [_web_option_label_value(opt) for opt in item.get("options") or []]
    default_indexes = {int(x) for x in item.get("defaultIndexes") or [] if isinstance(x, int | float) or str(x).isdigit()}
    default_values = {str(x) for x in item.get("defaultValues") or []}
    selected: list[tuple[int, dict[str, str]]] = []
    for idx, opt in enumerate(options):
        if idx in default_indexes or opt["value"] in default_values or opt["label"] in default_values:
            selected.append((idx, opt))
    if not item.get("multiple") and selected:
        return selected[:1]
    return selected


def _normalize_web_questionnaire(raw_questions: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    if not isinstance(raw_questions, list) or not raw_questions:
        return [], [{"path": "questions", "message": "questions must be a non-empty array"}]
    normalized: list[dict[str, Any]] = []
    seen_question_ids: set[str] = set()
    for question_index, raw_question in enumerate(raw_questions):
        path = f"questions[{question_index}]"
        if not isinstance(raw_question, dict):
            errors.append({"path": path, "message": "question must be an object"})
            continue
        question_id = str(raw_question.get("id") or "").strip()
        question_type = str(raw_question.get("type") or "").strip().lower()
        question_text = raw_question.get("question")
        if not question_id:
            errors.append({"path": f"{path}.id", "message": "question id must be a non-empty string"})
        elif question_id in seen_question_ids:
            errors.append({"path": f"{path}.id", "message": f"duplicate question id: {question_id}"})
        else:
            seen_question_ids.add(question_id)
        if question_type not in {"choice", "open"}:
            errors.append({"path": f"{path}.type", "message": "question type must be choice or open"})
        if not isinstance(question_text, str) or not question_text.strip():
            errors.append({"path": f"{path}.question", "message": "question must be a non-empty string"})
        if "description" in raw_question and not isinstance(raw_question.get("description"), str):
            errors.append({"path": f"{path}.description", "message": "description must be a string"})
        if "required" in raw_question and not isinstance(raw_question.get("required"), bool):
            errors.append({"path": f"{path}.required", "message": "required must be a boolean"})
        normalized_question: dict[str, Any] = {
            "id": question_id,
            "type": question_type,
            "question": question_text if isinstance(question_text, str) else "",
            "required": raw_question.get("required", True) if isinstance(raw_question.get("required", True), bool) else True,
        }
        if "description" in raw_question and isinstance(raw_question.get("description"), str):
            normalized_question["description"] = raw_question["description"]
        if question_type == "choice":
            if "multiple" in raw_question and not isinstance(raw_question.get("multiple"), bool):
                errors.append({"path": f"{path}.multiple", "message": "multiple must be a boolean"})
            normalized_question["multiple"] = raw_question.get("multiple", False) if isinstance(raw_question.get("multiple", False), bool) else False
            raw_options = raw_question.get("options")
            normalized_options: list[dict[str, str]] = []
            seen_values: set[str] = set()
            if not isinstance(raw_options, list) or not raw_options:
                errors.append({"path": f"{path}.options", "message": "choice options must be a non-empty array"})
            else:
                for option_index, raw_option in enumerate(raw_options):
                    option_path = f"{path}.options[{option_index}]"
                    if not isinstance(raw_option, dict):
                        errors.append({"path": option_path, "message": "option must be an object"})
                        continue
                    label = raw_option.get("label")
                    value = raw_option.get("value")
                    if not isinstance(label, str) or not label.strip():
                        errors.append({"path": f"{option_path}.label", "message": "option label must be a non-empty string"})
                    if not isinstance(value, str) or not value.strip():
                        errors.append({"path": f"{option_path}.value", "message": "option value must be a non-empty string"})
                    elif value in seen_values:
                        errors.append({"path": f"{option_path}.value", "message": f"duplicate option value: {value}"})
                    else:
                        seen_values.add(value)
                    if "description" in raw_option and not isinstance(raw_option.get("description"), str):
                        errors.append({"path": f"{option_path}.description", "message": "option description must be a string"})
                    normalized_option = {
                        "label": label if isinstance(label, str) else "",
                        "value": value if isinstance(value, str) else "",
                    }
                    if "description" in raw_option and isinstance(raw_option.get("description"), str):
                        normalized_option["description"] = raw_option["description"]
                    normalized_options.append(normalized_option)
            normalized_question["options"] = normalized_options
            if "recommendation" in raw_question:
                recommendation = raw_question.get("recommendation")
                if not isinstance(recommendation, dict):
                    errors.append({"path": f"{path}.recommendation", "message": "recommendation must be an object"})
                else:
                    values = recommendation.get("values")
                    reason = recommendation.get("reason")
                    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                        errors.append({"path": f"{path}.recommendation.values", "message": "recommendation values must be an array of strings"})
                        values = []
                    for value in values:
                        if value not in seen_values:
                            errors.append({"path": f"{path}.recommendation.values", "message": f"unknown recommended option value: {value}"})
                    if not isinstance(reason, str):
                        errors.append({"path": f"{path}.recommendation.reason", "message": "recommendation reason must be a string"})
                        reason = ""
                    normalized_question["recommendation"] = {"values": list(values), "reason": reason}
        normalized.append(normalized_question)
    return normalized, errors


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

    def _model_compact_trigger_tokens(self, model_label: str) -> int:
        """Resolve the same effective threshold used by the root Compactor."""
        resolved = self.config.models.resolve(model_label)
        if not resolved:
            return 0
        model_def = resolved[1]
        explicit = max(0, int(model_def.compact_trigger_tokens or 0))
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

    @staticmethod
    def _format_context_compaction_markdown(outcome: CompactionOutcome) -> str:
        def _tokens(value: int) -> str:
            n = max(0, int(value or 0))
            if n >= 1_000_000:
                return f"{n / 1_000_000:.2f}M".rstrip("0").rstrip(".")
            if n >= 1000:
                return f"{n / 1000:.1f}K".rstrip("0").rstrip(".")
            return str(n)

        return (
            "## 上下文压缩完成\n"
            f"- 触发来源：{outcome.source or 'compact'}\n"
            f"- 压缩前上下文：{_tokens(outcome.trigger_tokens)}\n"
            f"- 阈值：{_tokens(outcome.threshold_tokens)}\n"
            f"- 压缩模型：{outcome.compression_model_label or 'unknown'}\n"
            f"- 压缩消息：{outcome.old_message_count} 条\n"
            f"- 保留消息：{outcome.kept_message_count} 条\n"
            f"- 摘要 token：{_tokens(outcome.summary_tokens)}\n"
            f"- 覆盖至 message_id：{outcome.up_to_message_id}\n"
            + "\n---\n\n"
            "## 压缩后摘要\n"
            f"{outcome.summary or '（摘要为空）'}"
        )

    @staticmethod
    def _context_compaction_json(outcome: CompactionOutcome, *, after_tokens: int = 0) -> dict[str, Any]:
        summary_id = int(outcome.summary_id or 0)
        identity_suffix = str(summary_id) if summary_id else f"message-{int(outcome.up_to_message_id or 0)}"
        compaction_id = f"context-compaction:{identity_suffix}"
        return {
            "did": bool(outcome.did),
            "compactionId": compaction_id,
            "summaryId": summary_id,
            "scope": "root",
            "source": outcome.source,
            "status": "completed" if outcome.did else "unavailable",
            "beforeTokens": int(outcome.trigger_tokens or 0),
            "afterTokens": int(after_tokens or outcome.after_tokens or 0),
            "summaryChars": len(str(outcome.summary or "")),
            "outputAvailable": bool(outcome.did and outcome.summary),
            # Compatibility fields retained for stats and older clients.
            "triggerTokens": int(outcome.trigger_tokens or 0),
            "thresholdTokens": int(outcome.threshold_tokens or 0),
            "keepRecent": int(outcome.keep_recent or 0),
            "upToMessageId": int(outcome.up_to_message_id or 0),
            "oldMessageCount": int(outcome.old_message_count or 0),
            "keptMessageCount": int(outcome.kept_message_count or 0),
            "summaryTokens": int(outcome.summary_tokens or 0),
            "compressionModelLabel": outcome.compression_model_label,
            "tokenSource": outcome.token_source,
            "reason": outcome.reason,
        }

    async def _invalidate_web_controller_context_usage(
        self,
        chat_id: int,
        *,
        session_uuid: str = "",
    ) -> None:
        """Make post-compaction context explicitly unknown for this generation."""
        messages = MessageDAO(self.db)
        session = str(session_uuid or "") or await messages.get_or_create_session_uuid(chat_id)
        await messages.set_controller_context_usage(
            chat_id,
            session_uuid=session,
            tokens=None,
        )

    async def _emit_context_compaction_event(
        self,
        renderer: Any,
        outcome: CompactionOutcome,
        *,
        source: str = "",
    ) -> None:
        if not outcome.did:
            return
        live = getattr(renderer, "live", None)
        compaction_chat_id = int(getattr(live, "internal_chat_id", 0) or 0)
        if compaction_chat_id > 0:
            await self._invalidate_web_controller_context_usage(compaction_chat_id)
        metadata = self._context_compaction_json(outcome)
        metadata["source"] = source or outcome.source
        tool_call_id = str(metadata["compactionId"])
        conversation_uuid = str(getattr(live, "conversation_uuid", "") or "").strip()
        if conversation_uuid and int(metadata.get("summaryId") or 0):
            metadata["summaryRef"] = (
                f"/api/conversations/{conversation_uuid}/compactions/{metadata['summaryId']}"
            )
        start_metadata = {
            **metadata,
            "status": "running",
            "outputAvailable": False,
        }
        result_metadata = {
            **metadata,
            "status": "completed",
            "outputAvailable": bool(outcome.summary),
            "outputPreview": str(outcome.summary or "")[:12_000],
        }
        start_args = json.dumps(start_metadata, ensure_ascii=False)
        result_args = json.dumps(result_metadata, ensure_ascii=False)
        line = "ContextCompaction: 上下文达到阈值，正在压缩历史"
        start = getattr(renderer, "on_tool_start", None)
        result = getattr(renderer, "on_tool_result", None)
        if callable(start):
            maybe = start(tool_call_id, "ContextCompaction", start_args, line)
            if inspect.isawaitable(maybe):
                await maybe
        else:
            await renderer.emit({"type": "tool_start", "toolCallId": tool_call_id, "name": "ContextCompaction", "arguments": start_args, "line": line})
        markdown = self._format_context_compaction_markdown(outcome)
        if callable(result):
            maybe = result(tool_call_id, "ContextCompaction", result_args, markdown, 0)
            if inspect.isawaitable(maybe):
                await maybe
        else:
            await renderer.emit({"type": "tool_result", "toolCallId": tool_call_id, "name": "ContextCompaction", "arguments": result_args, "result": markdown, "durationMs": 0})

    def _compression_candidates_for(self, model_label: str, *, chat_id: int = 0) -> list[tuple[Any, str, str, str]]:
        """Return ordered compression candidates plus the primary model fallback.

        Each item is (backend, model_id, source, label). Invalid configured
        compression models are skipped; if none are usable, the primary/current
        model remains as the final candidate.
        """
        fallback_label = model_label or getattr(self.model_selection, "current", "") or self.config.models.primary
        configured_labels = list(getattr(self.config.models, "compression_models", []) or [])
        labels = self.config.models.compression_model_candidates(fallback_label)
        candidates: list[tuple[Any, str, str, str]] = []
        seen: set[str] = set()
        for label in labels:
            if label in seen:
                continue
            seen.add(label)
            source = "primary-fallback" if configured_labels and label == fallback_label and label not in configured_labels else "compression"
            try:
                backend, model_id, _ = self.llm_factory.backend_for(label)
            except Exception as exc:
                log.warning("压缩候选模型不可用，跳过", 会话=chat_id, 模型=label, 来源=source, 错误=str(exc)[:120])
                continue
            candidates.append((backend, model_id, source, label))
        if not candidates:
            backend, model_id, _ = self.llm_factory.backend_for(fallback_label)
            candidates.append((backend, model_id, "primary-fallback", fallback_label))
        else:
            primary_already_configured = any(label == fallback_label for _backend, _model_id, _source, label in candidates)
            if not primary_already_configured:
                try:
                    backend, model_id, _ = self.llm_factory.backend_for(fallback_label)
                    candidates.append((backend, model_id, "primary-fallback", fallback_label))
                except Exception as exc:
                    log.warning("压缩 fallback 主模型不可用", 会话=chat_id, 主模型=fallback_label, 错误=str(exc)[:120])
        return candidates

    def _make_web_compactor(self, chat_id: int, *, model_label: str) -> Compactor:
        fallback_label = model_label or getattr(self.model_selection, "current", "") or self.config.models.primary
        candidates = self._compression_candidates_for(fallback_label, chat_id=chat_id)
        backend, compression_model_id, _source, _label = candidates[0]
        extra_candidates = [
            CompressionCandidate(candidate_backend, candidate_model, source, label)
            for candidate_backend, candidate_model, source, label in candidates[1:]
        ]
        fallback_backend = None
        fallback_model_id = ""
        for candidate_backend, candidate_model, source, _candidate_label in candidates[1:]:
            if source == "primary-fallback":
                fallback_backend = candidate_backend
                fallback_model_id = candidate_model
                break
        async def _on_compaction_model_call(call: dict[str, Any]) -> None:
            call_model_label = str(call.get("model") or _label or compression_model_id)
            model_meta = self.config.models.resolve(call_model_label)
            if model_meta is None:
                model_meta = next(
                    (self.config.models.resolve(candidate.label) for candidate in extra_candidates if self.config.models.resolve(candidate.label) is not None),
                    None,
                )
            call_cost = model_meta[1].cost if model_meta else {}
            session_uuid = await MessageDAO(self.db).get_or_create_session_uuid(chat_id)
            await self._persist_web_model_call_delta(
                MessageDAO(self.db),
                chat_id,
                session_uuid=session_uuid,
                call=call,
                model_cost=call_cost,
                model_label=call_model_label,
                protocol=str(call.get("protocol") or ""),
                think_level="off",
                call_kind="context_compaction",
            )

        return Compactor(
            MessageDAO(self.db), SummaryDAO(self.db), backend,
            compression_model=compression_model_id,
            compression_label=_label,
            summary_source=_source,
            context_window=self.llm_factory.context_window(fallback_label),
            ratio=self.config.agent.compact_ratio,
            # Root compaction keeps no raw protocol tail. ``keep_recent`` is now
            # the visible XML dialogue limit used by _build_history.
            keep_recent=self.config.agent.keep_recent_messages,
            retain_raw_recent=0,
            summary_max_retries=self.config.agent.compact_max_retries,
            summary_max_tokens=self.config.agent.compact_max_tokens,
            summary_timeout_s=self.config.agent.compact_timeout_s,
            prompt_template=self.config.agent.compact_prompt,
            trigger_tokens=self._model_compact_trigger_tokens(fallback_label),
            fallback_backend=fallback_backend,
            fallback_compression_model=fallback_model_id,
            fallback_compression_label=next((candidate_label for _candidate_backend, _candidate_model, source, candidate_label in candidates[1:] if source == "primary-fallback"), ""),
            extra_candidates=extra_candidates,
            operation_locks=self.operation_locks,
            on_model_call=_on_compaction_model_call,
        )

    def _rath_context_compact_kwargs(self, model_label: str) -> dict[str, Any]:
        label = model_label or getattr(self.model_selection, "current", "") or self.config.models.primary
        kwargs: dict[str, Any] = {
            "context_window": int(self.llm_factory.context_window(label) if hasattr(self.llm_factory, "context_window") else 0),
            "context_compact_trigger_tokens": self._model_compact_trigger_tokens(label),
            "context_compact_ratio": self.config.agent.compact_ratio,
            "context_compact_keep_recent": self.config.agent.keep_recent_messages,
            "context_compact_prompt": self.config.agent.compact_prompt,
            "context_compact_max_tokens": self.config.agent.compact_max_tokens,
            "context_compact_max_retries": self.config.agent.compact_max_retries,
            "context_compact_timeout_s": self.config.agent.compact_timeout_s,
        }
        candidates = self._compression_candidates_for(label)
        if not candidates:
            return kwargs
        compact_costs: dict[str, dict[str, float]] = {}
        for _backend, _model_id, _source, candidate_label in candidates:
            model_meta = self.config.models.resolve(candidate_label)
            if model_meta:
                compact_costs[candidate_label] = model_meta[1].cost
        kwargs["context_compact_costs"] = compact_costs
        backend, model_id, source, candidate_label = candidates[0]
        kwargs["context_compact_backend"] = backend
        kwargs["context_compact_model"] = model_id
        kwargs["context_compact_source"] = source
        kwargs["context_compact_label"] = candidate_label
        extra = [
            CompressionCandidate(candidate_backend, candidate_model, candidate_source, label_text)
            for candidate_backend, candidate_model, candidate_source, label_text in candidates[1:]
        ]
        if extra:
            kwargs["context_compact_extra_candidates"] = extra
        for candidate_backend, candidate_model, candidate_source, _label_text in candidates[1:]:
            if candidate_source == "primary-fallback":
                kwargs["context_compact_fallback_backend"] = candidate_backend
                kwargs["context_compact_fallback_model"] = candidate_model
                break
        return kwargs

    async def _effective_thinking_level(self, chat_id: int, model_label: str) -> str:
        messages = MessageDAO(self.db)
        levels = self._model_thinking_levels(model_label)
        if not levels:
            return "off"
        stored = normalize_think_level(await messages.get_thinking_level(chat_id))
        if stored and stored in levels:
            return stored
        return self._model_default_thinking_level(model_label)

    def _pending_web_confirmations(self, conversation_uuid: str) -> list[dict[str, Any]]:
        ids = list(self._web_confirm_by_conversation.get(str(conversation_uuid or ""), set()))
        out: list[dict[str, Any]] = []
        now_mono = time.monotonic()
        for cid in ids:
            item = self._web_confirmations.get(cid)
            if not item:
                continue
            if now_mono >= float(item.get("expiresAtMono") or 0):
                continue
            out.append({
                "confirmationId": cid,
                "interactionId": cid,
                "action": item.get("action") or "confirm",
                "title": item.get("title") or "请确认",
                "body": item.get("body") or "",
                "type": item.get("type") or "warning",
                "confirmText": item.get("confirmText") or "确认",
                "cancelText": item.get("cancelText") or "取消",
                "options": item.get("options") if isinstance(item.get("options"), list) else [],
                "multiple": bool(item.get("multiple")),
                "defaultValues": item.get("defaultValues") if isinstance(item.get("defaultValues"), list) else [],
                "defaultIndexes": item.get("defaultIndexes") if isinstance(item.get("defaultIndexes"), list) else [],
                "defaultValue": item.get("defaultValue") or "",
                "sensitive": bool(item.get("sensitive")),
                **({"questions": item.get("questions") if isinstance(item.get("questions"), list) else []}
                   if item.get("action") == "questionnaire" else {}),
                "expiresAtMs": int(item.get("expiresAtMs") or 0),
            })
        return sorted(out, key=lambda x: int(x.get("expiresAtMs") or 0))

    async def _web_confirm(self, conversation_uuid: str, payload: dict[str, Any]) -> dict[str, Any]:
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return {"status": "error", "confirmed": False, "error": "missing_conversation_uuid"}
        timeout_s = max(1.0, float(payload.get("timeoutSeconds") or payload.get("timeout") or 600))
        cid = secrets.token_urlsafe(10).replace("-", "_")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        now_ms_value = int(time.time() * 1000)
        action = str(payload.get("action") or "confirm").strip().lower()
        if action not in {"confirm", "select", "prompt", "questionnaire"}:
            action = "confirm"
        questions: list[dict[str, Any]] = []
        if action == "questionnaire":
            questions, errors = _normalize_web_questionnaire(payload.get("questions"))
            if errors:
                return {
                    "status": "error",
                    "error": "invalid_questionnaire",
                    "message": "Questionnaire validation failed",
                    "details": errors,
                }
        item = {
            "confirmationId": cid,
            "conversationUuid": conv_uuid,
            "action": action,
            "title": str(payload.get("title") or "请确认"),
            "body": str(payload.get("body") or payload.get("message") or ""),
            "type": str(payload.get("type") or payload.get("tone") or "warning"),
            "confirmText": str(payload.get("confirmText") or ("确认" if action != "confirm" else "确认")),
            "cancelText": str(payload.get("cancelText") or "取消"),
            "default": bool(payload.get("default") or payload.get("defaultConfirmed")),
            "options": payload.get("options") if isinstance(payload.get("options"), list) else [],
            "multiple": bool(payload.get("multiple") or payload.get("multi")),
            "defaultValues": payload.get("defaultValues") if isinstance(payload.get("defaultValues"), list) else [],
            "defaultIndexes": payload.get("defaultIndexes") if isinstance(payload.get("defaultIndexes"), list) else [],
            "defaultValue": str(payload.get("defaultValue") or payload.get("default") or "") if action == "prompt" else "",
            "sensitive": bool(payload.get("sensitive") or payload.get("secret")),
            "questions": questions,
            "expiresAtMono": time.monotonic() + timeout_s,
            "expiresAtMs": now_ms_value + int(timeout_s * 1000),
            "future": future,
        }
        self._web_confirmations[cid] = item
        self._web_confirm_by_conversation.setdefault(conv_uuid, set()).add(cid)
        live = self._web_live_streams.get(conv_uuid)
        if live is not None:
            await live.publish({"type": "web_confirmation", "action": "created", "confirmation": self._pending_web_confirmations(conv_uuid), "confirmationId": cid})
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        except TimeoutError:
            if action == "questionnaire":
                result = {
                    "status": "timeout",
                    "cancelled": True,
                    "answers": [],
                    "interactionId": cid,
                }
            elif action == "select":
                selected = _web_select_defaults(item)
                result = {
                    "status": "timeout",
                    "cancelled": False,
                    "multiple": bool(item.get("multiple")),
                    "selectedIndexes": [idx for idx, _opt in selected],
                    "selectedValues": [str(opt.get("value") if opt.get("value") is not None else opt.get("label") or "") for _idx, opt in selected],
                    "selectedLabels": [str(opt.get("label") or opt.get("value") or "") for _idx, opt in selected],
                    "interactionId": cid,
                }
            elif action == "prompt":
                result = {
                    "status": "timeout",
                    "cancelled": True,
                    "value": str(item.get("defaultValue") or ""),
                    "interactionId": cid,
                }
            else:
                result = {
                    "status": "timeout",
                    "confirmed": bool(item.get("default")),
                    "choice": "confirm" if item.get("default") else "cancel",
                    "label": item.get("confirmText") if item.get("default") else item.get("cancelText"),
                    "interactionId": cid,
                }
            if not future.done():
                future.set_result(result)
            return result
        finally:
            self._web_confirmations.pop(cid, None)
            ids = self._web_confirm_by_conversation.get(conv_uuid)
            if ids is not None:
                ids.discard(cid)
                if not ids:
                    self._web_confirm_by_conversation.pop(conv_uuid, None)
            live = self._web_live_streams.get(conv_uuid)
            if live is not None:
                with contextlib.suppress(Exception):
                    await live.publish({"type": "web_confirmation", "action": "resolved", "confirmation": self._pending_web_confirmations(conv_uuid), "confirmationId": cid})

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
        usage = await messages.usage_totals(chat_id)
        # sessions/model_calls receives every controller and child-Agent request
        # at its completion boundary; rath_tasks is progress metadata for those
        # same Agent calls and must not be added again.
        usage_dict = asdict(usage)
        session_uuid = await messages.current_session_uuid(chat_id)
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
        fast_requested = await messages.get_fast_mode(chat_id)
        fast_supported = self._model_supports_fast(model_label)
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
        exact_context_tokens = await messages.latest_controller_context_usage(chat_id, session_uuid=session_uuid)
        context_trigger_tokens = self._model_compact_trigger_tokens(model_label)
        context_usage = {
            "known": exact_context_tokens is not None,
            "tokens": int(exact_context_tokens or 0),
            "compactTriggerTokens": context_trigger_tokens,
            "percent": ((int(exact_context_tokens) * 100.0 / context_trigger_tokens) if exact_context_tokens is not None and context_trigger_tokens > 0 else None),
            "manualMinPercent": int(self.config.agent.manual_compact_min_percent),
        }
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
            "thinkingLevel": await messages.get_thinking_level(chat_id),
            "effectiveThinkingLevel": await self._effective_thinking_level(chat_id, model_label),
            "thinkingLevels": thinking_levels,
            "defaultThinkingLevel": self._model_default_thinking_level(model_label) if thinking_levels else "",
            "supportsThinking": bool(thinking_levels),
            "fastMode": bool(fast_requested and fast_supported),
            "fastRequested": bool(fast_requested),
            "fastSupported": bool(fast_supported),
            "effectiveFastMode": bool(fast_requested and fast_supported),
            "agentRunConfig": agent_run_config,
            "compactTriggerTokens": self._model_compact_trigger_tokens(model_label),
            "compactRatio": float(self.config.agent.compact_ratio or 0.7),
            "showThinking": await messages.get_show_thinking(chat_id, default=self.config.ui.show_thinking),
            "messages": [self._message_json(r) for r in message_rows],
            "modelCalls": await self._chat_model_calls(chat_id, session_uuid),
            "toolCalls": await self._chat_tool_calls(chat_id, session_uuid),
            "pendingConfirmations": self._pending_web_confirmations(conv_uuid),
            "pendingSteering": pending_steering,
            "usage": usage_dict,
        }

    async def _build_history(self, chat_id: int) -> list[Message]:
        summary = await SummaryDAO(self.db).latest(chat_id)
        messages = MessageDAO(self.db)
        summary_text = str((summary or {}).get("summary") or "")
        if summary_text:
            # Once a root summary exists, the recent tail is a semantic reminder for
            # the main model, not a raw protocol replay.  Keep only the visible user
            # / final-assistant transcript in XML; tool, AgentWait, Plan, TaskMemory,
            # reasoning and other runtime payloads remain in DB/audit or the summary.
            visible_rows = await messages.recent_visible_history(
                chat_id,
                limit=max(1, int(self.config.agent.keep_recent_messages or 100)),
            )
            return build_summary_prefixed_visible_history(
                summary_text,
                visible_rows,
                max_messages=max(1, int(self.config.agent.keep_recent_messages or 100)),
            )
        rows = await messages.recent(chat_id)
        recent = [project_history_message_for_controller(row.to_message()) for row in rows]
        history = build_summary_prefixed_history("", recent)
        return repair_tool_pairing(history)

    async def _build_system_prompt_for_chat(self) -> str:
        try:
            params = await self._prompt_template_params_live()
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
                        compactions: list[tuple[CompactionOutcome, int]] | None = None,
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
        compaction_items = list(compactions or [])
        latest_compaction: dict[str, Any] | None = None
        context_after_compaction_tokens = 0
        if compaction_items:
            latest_outcome, latest_after_tokens = compaction_items[-1]
            context_after_compaction_tokens = int(latest_after_tokens or 0)
            latest_compaction = self._context_compaction_json(
                latest_outcome,
                after_tokens=context_after_compaction_tokens,
            )
        epilogue_compacted = bool(
            compaction_items and str(compaction_items[-1][0].source or "") == "turn_epilogue"
        )
        context_usage_known = bool(result.model_ok > 0 and result.last_prompt_usage_reported and not epilogue_compacted)
        stats = {
            "live": bool(live),
            "model": model,
            "thinkLevel": think_level,
            "durationMs": duration_ms,
            "reasoningMs": result.reasoning_ms_sum,
            "modelCalls": result.model_calls + result.expert_model_calls,
            "modelOk": model_ok,
            "modelRetry": result.model_retry,
            "modelFail": result.model_fail,
            "toolCalls": len(result.tools_used) + result.expert_tool_calls,
            "expertModelCalls": result.expert_model_calls,
            "expertToolCalls": result.expert_tool_calls,
            "expertTasks": result.expert_tasks,
            "expertTaskUuids": sorted(result.expert_accounted_task_uuids),
            # Compatibility display field: older clients already understand this
            # provider snapshot. Authorization and post-compaction invalidation use
            # the explicit contextUsage.known contract below.
            "contextTokens": prompt_tokens,
            "contextWindow": context_window,
            "contextUsage": {
                "available": bool(result.model_ok > 0),
                "known": context_usage_known,
                "tokens": prompt_tokens if context_usage_known else 0,
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
        if latest_compaction is not None:
            stats["contextCompacted"] = True
            stats["contextAfterCompactionTokens"] = context_after_compaction_tokens
            stats["contextCompaction"] = latest_compaction
            stats["contextCompactions"] = [
                self._context_compaction_json(outcome, after_tokens=after_tokens)
                for outcome, after_tokens in compaction_items
            ]
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
        memory_reminder_generation: int | None = None,
        cost_usd_override: float | None = None,
    ) -> float:
        """Commit one completed upstream request before the next tool/model step.

        `model_calls` is the durable billing ledger used by channel/model
        statistics.  Writing one immutable row per request means a later crash
        cannot erase already returned usage.  Session totals and per-turn counters
        are updated in the same awaited boundary.
        """
        usage_present = isinstance(call.get("usage"), Usage)
        prompt_usage_reported = bool(call.get("promptUsageReported")) and usage_present
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
            if call_kind == "controller_request" and status == "ok":
                cur = await connection.execute(
                    "SELECT COALESCE(MAX(id),0) AS generation FROM summaries WHERE chat_id=?",
                    (chat_id,),
                )
                generation = int((await cur.fetchone())["generation"] or 0)
                prompt_tokens = (
                    max(0, int(usage.input_tokens or 0) + int(usage.cache_read_tokens or 0) + int(usage.cache_write_tokens or 0))
                    if prompt_usage_reported
                    else None
                )
                # The newest successful request is authoritative. A provider that
                # omits usage writes an explicit unknown tombstone so immutable
                # legacy model-call rows cannot revive an older context snapshot.
                await accounting.set_controller_context_usage(
                    chat_id,
                    session_uuid=session_uuid,
                    tokens=prompt_tokens,
                    summary_id=generation,
                    commit=False,
                )
                if memory_reminder_generation is not None:
                    await connection.execute(
                        """INSERT INTO web_memory_reminders(
                               chat_id, session_uuid, summary_id, delivered_at
                           ) VALUES(?,?,?,?)
                           ON CONFLICT(chat_id, summary_id) DO UPDATE SET
                               session_uuid=excluded.session_uuid,
                               delivered_at=excluded.delivered_at""",
                        (chat_id, session_uuid, int(memory_reminder_generation), now_ts()),
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

    async def _post_compact_after_web_turn(
        self,
        chat_id: int,
        last_prompt_tokens: int,
        *,
        model_label: str,
        source: str = "turn_epilogue",
    ) -> CompactionOutcome:
        try:
            compactor = self._make_web_compactor(chat_id, model_label=model_label)
            outcome = await compactor.maybe_compact_detail(chat_id, prompt_tokens=last_prompt_tokens, source=source)
            if outcome.did:
                clear_read_file_state(chat_id=chat_id)
            return outcome
        except Exception:
            log.exception("Web 对话历史压缩后处理异常", 会话=chat_id)
            return CompactionOutcome(did=False, source=source, trigger_tokens=last_prompt_tokens, reason="exception")

    async def _pre_compact_before_web_turn(
        self,
        chat_id: int,
        prompt_tokens: int,
        *,
        model_label: str,
        source: str = "pre_model_request",
    ) -> CompactionOutcome:
        try:
            compactor = self._make_web_compactor(chat_id, model_label=model_label)
            outcome = await compactor.maybe_compact_detail(chat_id, prompt_tokens=prompt_tokens, source=source)
            if outcome.did:
                clear_read_file_state(chat_id=chat_id)
            return outcome
        except Exception:
            log.exception("Web 对话历史预压缩异常", 会话=chat_id)
            return CompactionOutcome(did=False, source=source, trigger_tokens=prompt_tokens, reason="exception")

__all__ = [name for name in globals() if not name.startswith("__")]
