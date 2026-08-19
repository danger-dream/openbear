# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.rath.agent_prompt import render_agent_base_system_prompt
from app.rath.plan import PlanError
from app.tools.allowlist import AGENT_DELEGATION_TOOL_NAMES
from app.web_console.core import *
from app.web_console.live_stream import *

_RATH_MONITOR_EVENT_KINDS = frozenset({
    "task_created", "task_started", "task_completed", "task_failed", "task_cancelled", "task_interrupted",
    "agent_control", "agent_supervision", "control_requested", "control_response", "steer_applied",
    "pause_applied", "resume_applied", "cancel_requested", "needs_openbear_control",
    "plan_submitted", "plan_decision", "plan_decision_approve", "plan_decision_revise", "plan_decision_cancel",
    "plan_replan_requested", "agent_plan_protocol_corrected", "agent_control_continuation_saved",
    "model_context_pre_compacted", "model_context_overflow_compacted", "model_context_compaction_failed",
})


def _is_rath_monitor_event_kind(kind: str) -> bool:
    value = str(kind or "")
    return (
        value in _RATH_MONITOR_EVENT_KINDS
        or value.startswith("control_")
        or value.startswith("plan_")
        or value.startswith("agent_supervision")
        or value.startswith("model_context_compaction")
    )


class WebAdminRathMixin:
    def _agent_name_for_key(self, agent_key: str, agent_names: dict[str, str], *, source: dict[str, Any] | None = None) -> str:
        key = str(agent_key or "")
        if not key:
            return ""
        if key in agent_names:
            return agent_names[key]
        snapshot = (source or {}).get("agentSnapshot")
        if isinstance(snapshot, dict) and str(snapshot.get("agentKey") or "") == key:
            return str(snapshot.get("name") or "")
        return key

    def _rath_task_item(self, task, agent_names: dict[str, str] | None = None) -> dict[str, Any]:
        item = asdict(task)
        key = str(item.get("current_agent_key") or "")
        item["current_agent_name"] = self._agent_name_for_key(key, agent_names or {}, source=item.get("input") or {})
        return item

    def _rath_agent_item(self, agent) -> dict[str, Any]:
        item = asdict(agent)
        item["tool_allowlist"] = [
            name for name in sanitize_tool_allowlist(item.get("tool_allowlist") or [])
            if name in AGENT_DELEGATION_TOOL_NAMES
        ]
        return item

    async def _default_agent_workflow_uuid(self) -> str:
        wf = await self.rath_dao.workflow_by_slug(SINGLE_AGENT_WORKFLOW_SLUG)
        if wf is not None:
            return wf.workflow_uuid
        # Startup should already ensure this workflow, but keep API robust in tests
        # and after partial migrations.
        return await self.rath_dao.upsert_workflow(
            slug=SINGLE_AGENT_WORKFLOW_SLUG,
            name="Single Agent Tasks",
            description="用户在 Web 控制台注册的自定义 Agent 默认工作流。",
            kind="single-agent",
            config={"version": 1, "mode": "single-agent"},
            enabled=True,
        )

    def _default_rath_agent_prompt(self) -> str:
        return """
你是 OpenBear 的后台执行 Agent。你只负责 OpenBear 在本次任务 prompt 中分配给你的子任务，不负责最终对用户发言。Web Agent 配置只是你的可复用 system prompt；真实任务目标始终来自每次 Agent 调用的 prompt。

## 执行边界
- 你不是一次性 skill，而是当前 OpenBear 会话中的可持续 Agent Session；Task 完成不代表 Session 结束，后续同一 Agent 可能复用你的历史摘要和产物。
- `Agent` / `AgentMessage` / `AgentStop` 只属于 OpenBear 主控；你不能调用，也不要要求用户选择 Agent。
- 收敛优先：你不是全量审计器。达到“足够回答本次子任务”的证据量后，应停止扩展搜索/读取并输出综合结论；接近预算上限时必须基于已有证据收口，不要把总结工作留给 OpenBear 重做。

## 核心职责
1. 严格完成本次 instruction 指定的子任务。
2. 基于可用上下文和工具给出可追踪结论。
3. 把已验证事实、合理推断、未覆盖项分开，方便 OpenBear 汇总。

## 工作流程
1. 先阅读本次 instruction，确认目标、上下文、边界和输出契约。
2. 判断可用工具；只在必要时调用工具。
3. 收集证据：文件路径、符号名、命令结果、URL、日志或文本片段。
4. 完成分析/执行/验证后，先给结论，再给依据。
5. 如果信息不足或工具不可用，说明缺口，不要编造。

## 工具使用规则
- 只有实际调用工具后，才能说“已读取/已搜索/已执行/已验证”。
- 不要调用 Agent / AgentMessage / AgentStop；这些只属于 OpenBear 主控。
- 不要要求用户选择 Agent。
- 不执行删除、重启、发布、改权限、外部发送、破坏性数据库操作，除非 instruction 明确授权。
- 如果 instruction 与安全边界冲突，停止并报告冲突。

## 输出格式
- 结论
- 已执行动作 / 使用工具
- 关键依据
- 风险 / 未覆盖项
- 建议下一步

## 质量标准
- 结论明确，不绕圈。
- 证据可追踪。
- 建议具体可执行。
- 不把推断写成事实。
""".strip()

    def _normalize_agent_payload(self, body: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if not partial or "name" in body:
            name = str(body.get("name") or "").strip()
            if not name and not partial:
                raise ValueError("agent_name_required")
            if name:
                out["name"] = name
        if not partial or "agentKey" in body or "agent_key" in body:
            key = str(body.get("agentKey") or body.get("agent_key") or "").strip()
            if key:
                out["agent_key"] = slugify_ref(key) or key
            elif not partial:
                out["agent_key"] = slugify_ref(str(out.get("name") or "agent")) or "agent"
        for src, dst in (
            ("description", "description"),
            ("systemPrompt", "system_prompt"),
            ("system_prompt", "system_prompt"),
            ("model", "model"),
            ("thinkLevel", "think_level"),
            ("think_level", "think_level"),
        ):
            if src in body:
                out[dst] = str(body.get(src) or "")
        if not partial and not str(out.get("system_prompt") or "").strip():
            out["system_prompt"] = self._default_rath_agent_prompt()
        if "toolAllowlist" in body or "tool_allowlist" in body:
            raw = body.get("toolAllowlist", body.get("tool_allowlist"))
            if isinstance(raw, str):
                tools = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
            elif isinstance(raw, list):
                tools = [str(x).strip() for x in raw if str(x).strip()]
            else:
                tools = []
            out["tool_allowlist"] = [name for name in sanitize_tool_allowlist(tools) if name in AGENT_DELEGATION_TOOL_NAMES]
        if "sort" in body:
            out["sort"] = int(body.get("sort") or 0)
        if "enabled" in body:
            out["enabled"] = bool(body.get("enabled"))
        return out

    async def _scoped_rath_task(self, request: web.Request, task_uuid: str):
        session: WebSession = request[_WEB_SESSION_KEY]
        task = await self.rath_dao.get_task(task_uuid)
        if task is None:
            return None
        owned_chat_ids = await self._owned_chat_ids_for_web_session(session.chat_id)
        if int(task.chat_id or 0) not in owned_chat_ids:
            return None
        return task

    @staticmethod
    def _plan_version_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
        before = (previous or {}).get("plan") if previous else {}
        after = current.get("plan") if isinstance(current.get("plan"), dict) else {}
        before = before if isinstance(before, dict) else {}
        before_steps = {str(item.get("id")): item for item in before.get("steps") or [] if isinstance(item, dict)}
        after_steps = {str(item.get("id")): item for item in after.get("steps") or [] if isinstance(item, dict)}
        changed_fields = [
            key for key in ("title", "objective", "scope", "assumptions", "finalOutputs", "risks")
            if before and before.get(key) != after.get(key)
        ]
        return {
            "fromVersion": int(previous.get("version") or 0) if previous else None,
            "toVersion": int(current.get("version") or 0),
            "addedStepIds": sorted(set(after_steps) - set(before_steps)),
            "removedStepIds": sorted(set(before_steps) - set(after_steps)),
            "retainedStepIds": sorted(set(before_steps) & set(after_steps)),
            "changedStepIds": sorted(
                step_id for step_id in set(before_steps) & set(after_steps)
                if before_steps[step_id] != after_steps[step_id]
            ),
            "changedFields": changed_fields,
        }

    async def handle_api_rath_task_plan(self, request: web.Request) -> web.Response:
        conversation_uuid = str(request.match_info.get("conversation_uuid") or "").strip()
        task_uuid = str(request.match_info.get("task_uuid") or "").strip()
        task = await self._scoped_rath_task(request, task_uuid)
        if task is None or str(task.parent_session_uuid or "") != conversation_uuid:
            return web.json_response({"ok": False, "error": "rath_task_not_found"}, status=404)
        coordinator = getattr(self.rath, "plan_coordinator", None)
        if coordinator is None:
            return web.json_response({"ok": False, "error": "agent_plan_unavailable"}, status=503)
        try:
            snapshot = await coordinator.snapshot(task_uuid)
        except PlanError as exc:
            return web.json_response(exc.public(), status=404 if exc.code == "task_not_found" else 400)
        versions = snapshot.get("versions") or []
        for index, version in enumerate(versions):
            version["diff"] = self._plan_version_diff(versions[index - 1] if index else None, version)
        state = snapshot.get("state") or {}
        active_version = int(state.get("active_plan_version") or 0)
        pending_version = int(state.get("pending_plan_version") or 0)
        current_version = pending_version or active_version
        current = next((item for item in versions if int(item.get("version") or 0) == current_version), None)
        return web.json_response({
            "ok": True,
            "task": self._rath_task_item(task),
            "current": current,
            **snapshot,
        })

    async def handle_api_rath_task_events(self, request: web.Request) -> web.Response:
        conversation_uuid = str(request.match_info.get("conversation_uuid") or "").strip()
        task_uuid = str(request.match_info.get("task_uuid") or "").strip()
        task = await self._scoped_rath_task(request, task_uuid)
        if task is None or str(task.parent_session_uuid or "") != conversation_uuid:
            return web.json_response({"ok": False, "error": "rath_task_not_found"}, status=404)
        try:
            before_seq = max(0, int(request.query.get("beforeSeq") or 0))
            after_seq = max(0, int(request.query.get("afterSeq") or 0))
            limit = max(1, min(100, int(request.query.get("limit") or 20)))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid_event_cursor"}, status=400)
        if before_seq and after_seq:
            return web.json_response({"ok": False, "error": "event_cursor_conflict"}, status=400)
        cur = await self.db.conn.execute(
            """
            SELECT kind, COUNT(*) AS count
            FROM rath_task_events
            WHERE task_uuid=?
            GROUP BY kind
            """,
            (task_uuid,),
        )
        kind_counts = {
            str(row["kind"] or ""): int(row["count"] or 0)
            for row in await cur.fetchall()
        }
        total = sum(kind_counts.values())
        monitor_total = sum(count for kind, count in kind_counts.items() if _is_rath_monitor_event_kind(kind))
        if after_seq:
            events = await self.rath_dao.events(task_uuid, after_seq=after_seq, limit=limit)
            delivered_through = int(events[-1].seq or after_seq) if events else after_seq
            cur = await self.db.conn.execute(
                """
                SELECT EXISTS(
                  SELECT 1 FROM rath_task_events WHERE task_uuid=? AND seq>?
                ) AS has_more
                """,
                (task_uuid, delivered_through),
            )
            more_row = await cur.fetchone()
            has_more = bool(more_row and int(more_row["has_more"] or 0))
        else:
            events, _stored_total, has_more = await self.rath_dao.events_before(
                task_uuid,
                before_seq=before_seq,
                limit=limit,
            )
        return web.json_response({
            "ok": True,
            "taskUuid": task_uuid,
            "events": [
                {
                    "seq": int(event.seq or 0),
                    "ts": int(event.ts or 0),
                    "kind": str(event.kind or ""),
                    "agentKey": str(event.agent_key or ""),
                    "summary": str(event.summary or ""),
                    "detail": event.detail if isinstance(event.detail, dict) else {},
                    "elapsedMs": int(event.elapsed_ms or 0),
                }
                for event in events
            ],
            "total": total,
            "monitorTotal": monitor_total,
            "hasMore": has_more,
            "nextBeforeSeq": int(events[0].seq or 0) if not after_seq and has_more and events else 0,
            "nextAfterSeq": int(events[-1].seq or after_seq) if events else after_seq,
            "direction": "newer" if after_seq else "older",
        })

    async def handle_api_rath_options(self, request: web.Request) -> web.Response:
        models: list[dict[str, Any]] = []
        # Provider mapping order is the channel-management order persisted in the config.
        # Preserve it so the composer selector matches the order users arranged there.
        for provider_key, provider in self.config.models.providers.items():
            if not provider.enabled:
                continue
            for model in provider.models:
                key = f"{provider_key}/{model.id}"
                models.append({
                    "key": key,
                    "label": model.name or key,
                    "provider": provider_key,
                    "model": model.id,
                    "protocol": provider.protocol,
                    "reasoning": model.reasoning,
                    "contextWindow": model.context_window,
                    "maxTokens": model.max_tokens,
                    "thinkingLevels": list(model.thinking_levels or []),
                    "defaultThinkingLevel": model.default_thinking_level,
                    "supportsFast": bool(model.supports_fast),
                    "compactTriggerTokens": int(model.compact_trigger_tokens or 0),
                    "compactRatio": float(self.config.agent.compact_ratio or 0.7),
                    "primary": key == self.config.models.primary,
                    "compression": key in self.config.models.compression_models,
                })
        tool_summaries = self.tools.summaries(scope="agent") if self.tools is not None else {}
        available_agent_tools = set(self.tools.names(scope="agent")) if self.tools is not None else set()
        tools = [
            {"name": name, "description": str(tool_summaries.get(name) or "")}
            for name in sorted(set(AGENT_DELEGATION_TOOL_NAMES) & available_agent_tools)
        ]
        return web.json_response({
            "ok": True,
            "models": models,
            "tools": tools,
            "primaryModel": self.config.models.primary,
            "compressionModels": list(self.config.models.compression_models),
            "currentModel": getattr(self.model_selection, "current", "") if self.model_selection else "",
            "thinkLevels": sorted({level for item in models for level in item.get("thinkingLevels", [])}) or ["off"],
        })

    async def handle_api_rath_agents(self, request: web.Request) -> web.Response:
        include_disabled = request.query.get("disabled") in {"1", "true", "yes"}
        agents = await self.rath_dao.list_agents(include_disabled=include_disabled)
        return web.json_response({"ok": True, "items": [self._rath_agent_item(a) for a in agents]})

    async def handle_api_rath_agent_create(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        try:
            payload = self._normalize_agent_payload(body)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        try:
            agent_id = await self.rath_dao.create_agent(**payload)
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"rath_agent_create_failed: {type(exc).__name__}: {exc}"}, status=400)
        agent = await self.rath_dao.agent_by_id(agent_id)
        session: WebSession = request[_WEB_SESSION_KEY]
        await self.audit(
            "rath.agent.created",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"agentId": agent_id, "agentKey": payload.get("agent_key"), "name": payload.get("name")},
        )
        return web.json_response({"ok": True, "item": self._rath_agent_item(agent) if agent else {"id": agent_id}})

    async def handle_api_rath_agent_update(self, request: web.Request) -> web.Response:
        agent_id = int(request.match_info["agent_id"])
        existing = await self.rath_dao.agent_by_id(agent_id, include_disabled=True)
        if existing is None:
            return web.json_response({"ok": False, "error": "rath_agent_not_found"}, status=404)
        body = await self._json_body(request)
        try:
            payload = self._normalize_agent_payload(body, partial=True)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        try:
            await self.rath_dao.update_agent(agent_id, **payload)
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"rath_agent_update_failed: {type(exc).__name__}: {exc}"}, status=400)
        agent = await self.rath_dao.agent_by_id(agent_id)
        session: WebSession = request[_WEB_SESSION_KEY]
        await self.audit(
            "rath.agent.updated",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"agentId": agent_id, "fields": sorted(payload.keys())},
        )
        return web.json_response({"ok": True, "item": self._rath_agent_item(agent) if agent else {"id": agent_id}})

    async def handle_api_rath_agent_trial(self, request: web.Request) -> web.Response:
        if self.llm_factory is None:
            return web.json_response({"ok": False, "error": "llm_factory_unavailable"}, status=503)
        agent_id = int(request.match_info["agent_id"])
        agent = await self.rath_dao.agent_by_id(agent_id, include_disabled=False)
        if agent is None:
            return web.json_response({"ok": False, "error": "rath_agent_not_found"}, status=404)
        body = await self._json_body(request)
        instruction = str(body.get("instruction") or "").strip()
        if not instruction:
            return web.json_response({"ok": False, "error": "instruction_required"}, status=400)
        if bool(getattr(self.config.rath, "agent_plan_enabled", True)):
            return web.json_response({
                "ok": False,
                "error": "controller_runtime_required",
                "message": "Plan-enabled Agent trial must be started from a conversation so AgentWait can review the Plan.",
            }, status=409)
        session: WebSession = request[_WEB_SESSION_KEY]
        session_uuid = await self._current_session_uuid(session.chat_id)
        title = f"试运行 {agent.name}: {instruction[:80]}"
        workflow_uuid = await self._default_agent_workflow_uuid()
        agent_session = await self.rath_dao.get_or_create_agent_session(
            openbear_session_uuid=session_uuid,
            chat_id=session.chat_id,
            workflow_uuid=workflow_uuid,
            agent_key=agent.agent_key,
            title=agent.name,
            metadata={"agentName": agent.name, "source": "web_trial"},
        )
        conv = None
        with contextlib.suppress(Exception):
            cur = await self.db.conn.execute(
                "SELECT * FROM web_conversations WHERE conversation_uuid=? LIMIT 1",
                (session_uuid,),
            )
            row = await cur.fetchone()
            conv = dict(row) if row else None
        main_fast = False
        with contextlib.suppress(Exception):
            fast_chat_id = int((conv or {}).get("internal_chat_id") or 0) or int(session.chat_id or 0)
            main_fast = bool(await MessageDAO(self.db).get_fast_mode(fast_chat_id))
        runtime = resolve_agent_runtime_config(
            agent,
            config=self.config,
            model_selection_current=str(getattr(self.model_selection, "current", "") or "") if self.model_selection else "",
            conversation=conv,
            main_model=str((conv or {}).get("model") or "") or (getattr(self.model_selection, "current", "") if self.model_selection else "") or self.config.models.primary,
            main_fast_requested=main_fast,
        )
        task_uuid = await self.rath.create_task(
            chat_id=session.chat_id,
            workflow_uuid=workflow_uuid,
            title=title[:120],
            input_data={
                "instruction": instruction,
                "raw": instruction,
                "source": "web_trial",
                "agentSnapshot": agent_to_snapshot(agent, runtime=runtime),
                "agentSessionUuid": agent_session.session_uuid,
            },
            parent_session_uuid=session_uuid,
            agent_session_uuid=agent_session.session_uuid,
        )

        async def _runner_factory(_task_uuid: str) -> None:
            model_name = str(runtime.get("model") or (getattr(self.model_selection, "current", "") if self.model_selection else "") or self.config.models.primary)
            backend, model_id, max_tokens = self.llm_factory.backend_for(model_name)
            cost = dict(runtime.get("cost") or {})
            base_cost = dict(runtime.get("baseCost") or {})
            fast_cost = dict(runtime.get("fastCost") or {})
            fast_requested = bool(runtime.get("fastMode"))
            think_level = str(runtime.get("thinkLevel") or "off")
            service_tier = str(runtime.get("serviceTier") or "")
            fast_request = dict(runtime.get("fastRequest") or {})
            agent_base_system_prompt = await render_agent_base_system_prompt(
                self.rath_dao.db,
                identity=str(getattr(self.config.memory, "identity", "openbear") or "openbear"),
                registry=self.tools,
                tool_allowlist=agent.tool_allowlist,
                model_name=model_name,
                workspace_dir=str(getattr(self, "workspace_dir", "") or ""),
            )
            async def _on_model_call(detail: dict[str, Any]) -> None:
                effective_label = str(detail.get("modelLabel") or model_name)
                effective_meta = self.config.models.resolve(effective_label)
                usage = Usage(
                    input_tokens=max(0, int(detail.get("inputTokens") or 0)),
                    output_tokens=max(0, int(detail.get("outputTokens") or 0)),
                    cache_read_tokens=max(0, int(detail.get("cacheReadTokens") or 0)),
                    cache_write_tokens=max(0, int(detail.get("cacheWriteTokens") or 0)),
                )
                await self._persist_web_model_call_delta(
                    MessageDAO(self.db),
                    session.chat_id,
                    session_uuid=session_uuid,
                    call={
                        "status": str(detail.get("status") or "ok"),
                        "usage": usage,
                        "totalTimeMs": int(detail.get("durationMs") or 0),
                        "outputTokens": usage.output_tokens,
                        "errorType": str(detail.get("errorType") or ""),
                    },
                    model_cost=effective_meta[1].cost if effective_meta else {},
                    model_label=effective_label,
                    protocol=str(detail.get("protocol") or getattr(backend, "protocol", "") or ""),
                    think_level=str(detail.get("thinkLevel") or "off"),
                    # The runner has already selected normal vs Fast cost for this
                    # physical call. Do not re-price it with the model's normal
                    # table when committing the Web ledger.
                    cost_usd_override=detail.get("costUsd"),
                )

            runner = SingleAgentWorkflowRunner(
                self.rath_dao,
                task_uuid,
                agent=agent,
                backend=backend,
                model=model_id,
                max_tokens=max_tokens,
                tools=self.tools,
                model_label=model_name,
                think_level=think_level,
                service_tier=service_tier,
                fast_request=fast_request,
                session_id=safe_agent_llm_session_id(agent_session.session_uuid, task_uuid, agent.agent_key),
                openbear_session_uuid=session_uuid,
                agent_session_uuid=agent_session.session_uuid,
                cost=cost,
                base_cost=base_cost,
                fast_cost=fast_cost,
                fast_requested=fast_requested,
                base_system_prompt=agent_base_system_prompt,
                tool_result_max_chars=max_tool_result_chars(
                    self.llm_factory.context_window(model_name)
                    if hasattr(self.llm_factory, "context_window") else 128000,
                    self.config.tools.tool_result_max_chars,
                ),
                max_retries=self.config.agent.max_retries,
                retry_backoff_s=self.config.agent.retry_backoff_s,
                retry_max_delay_s=self.config.agent.retry_max_delay_s,
                retry_jitter_ratio=self.config.agent.retry_jitter_ratio,
                retry_cancel_check=lambda: self.rath.consume_retry_cancel(task_uuid),
                model_call_limit=int(getattr(self.config.rath, "agent_model_call_limit", 20) or 0),
                tool_call_limit=int(getattr(self.config.rath, "agent_tool_call_limit", 40) or 0),
                plan_control_call_limit=int(getattr(self.config.rath, "plan_control_call_limit", 200) or 200),
                plan_protocol_enabled=False,
                poll_interval_s=0.5,
                on_model_call=_on_model_call,
                **self._rath_context_compact_kwargs(model_name),
            )
            await runner.run()

        self.rath.start(task_uuid, session.chat_id, _runner_factory)
        await self.audit(
            "rath.agent.trial.started",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"agentId": agent_id, "agentKey": agent.agent_key, "taskUuid": task_uuid},
        )
        task = await self.rath_dao.get_task(task_uuid)
        return web.json_response({"ok": True, "taskUuid": task_uuid, "item": self._rath_task_item(task) if task else {"task_uuid": task_uuid}})

    async def handle_api_rath_agent_delete(self, request: web.Request) -> web.Response:
        agent_id = int(request.match_info["agent_id"])
        agent = await self.rath_dao.agent_by_id(agent_id)
        if agent is None:
            return web.json_response({"ok": False, "error": "rath_agent_not_found"}, status=404)
        await self.rath_dao.delete_agent(agent_id)
        session: WebSession = request[_WEB_SESSION_KEY]
        await self.audit(
            "rath.agent.deleted",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"agentId": agent_id, "agentKey": agent.agent_key, "name": agent.name},
        )
        return web.json_response({"ok": True})


__all__ = [name for name in globals() if not name.startswith("__")]
