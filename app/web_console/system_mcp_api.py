# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.config_store import ConfigConflictError
from app.web_console.core import *
from app.web_console.live_stream import *


class WebAdminSystemMcpMixin:
    async def _running_operations_json(self, *, limit: int = 8) -> list[dict[str, Any]]:
        try:
            cur = await self.db.conn.execute(
                """
                SELECT operation_uuid, chat_id, kind, status, detail_json, started_at
                FROM operations
                WHERE status='running'
                ORDER BY started_at ASC, id ASC
                LIMIT ?
                """,
                (int(limit),),
            )
            rows = await cur.fetchall()
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            detail = {}
            try:
                detail = json.loads(str(row["detail_json"] or "{}"))
            except Exception:
                detail = {}
            out.append({
                "operationUuid": str(row["operation_uuid"] or ""),
                "chatId": int(row["chat_id"] or 0),
                "kind": str(row["kind"] or ""),
                "status": str(row["status"] or ""),
                "detail": detail if isinstance(detail, dict) else {},
                "startedAt": int(row["started_at"] or 0),
            })
        return out

    async def _restart_running_json(self) -> dict[str, Any]:
        active_processes = processes.active()
        running_openbear = self.runs.count() if self.runs is not None else 0
        running_rath = self.rath.count() if self.rath is not None else 0
        running_children = len([info for info in active_processes if getattr(info, "blocks_restart", True)])
        running_operations = await self._running_operations_json()
        return {
            "openbearRuns": running_openbear,
            "rathTasks": running_rath,
            "childProcesses": running_children,
            "totalChildProcesses": len(active_processes),
            "operations": len(running_operations),
            "busy": bool(running_openbear or running_rath or running_children or running_operations),
            "processes": [
                {
                    "pid": info.pid,
                    "command": info.command,
                    "cwd": info.cwd,
                    "startedAt": int(info.started_at),
                    "ageSeconds": max(0, int(time.time() - info.started_at)),
                    "blocksRestart": bool(getattr(info, "blocks_restart", True)),
                }
                for info in active_processes[:10]
            ],
            "operationItems": running_operations,
        }

    @staticmethod
    def _safe_mcp_text(value: Any, *, max_chars: int = 1000) -> str:
        text = "" if value is None else str(value).strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "…"

    @classmethod
    def _safe_mcp_schema(cls, schema: Any, *, depth: int = 0) -> dict[str, Any]:
        """Return public MCP tool schema metadata without examples/default values.

        MCP input schema is public server metadata, but it can be arbitrarily large or
        contain example/default literals. Keep structural JSON-Schema fields that the
        UI needs for parameter summaries and drop value-bearing extras.
        """
        if not isinstance(schema, dict) or depth > 5:
            return {}
        out: dict[str, Any] = {}
        scalar_keys = {
            "type", "title", "description", "format", "minimum", "maximum",
            "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
            "pattern", "minItems", "maxItems", "uniqueItems",
        }
        for key in scalar_keys:
            if key not in schema:
                continue
            value = schema.get(key)
            if isinstance(value, str):
                out[key] = cls._safe_mcp_text(value, max_chars=800)
            elif isinstance(value, (int, float, bool)) or value is None:
                out[key] = value
            elif isinstance(value, list):
                out[key] = [cls._safe_mcp_text(v, max_chars=120) for v in value[:20] if isinstance(v, (str, int, float, bool))]
        if isinstance(schema.get("required"), list):
            out["required"] = [cls._safe_mcp_text(x, max_chars=120) for x in schema.get("required", [])[:100] if isinstance(x, str)]
        properties = schema.get("properties")
        if isinstance(properties, dict):
            out["properties"] = {
                cls._safe_mcp_text(str(name), max_chars=160): cls._safe_mcp_schema(prop, depth=depth + 1)
                for name, prop in list(properties.items())[:120]
                if isinstance(name, str) and isinstance(prop, dict)
            }
        items = schema.get("items")
        if isinstance(items, dict):
            out["items"] = cls._safe_mcp_schema(items, depth=depth + 1)
        for key in ("anyOf", "oneOf", "allOf"):
            value = schema.get(key)
            if isinstance(value, list):
                out[key] = [cls._safe_mcp_schema(item, depth=depth + 1) for item in value[:12] if isinstance(item, dict)]
        additional = schema.get("additionalProperties")
        if isinstance(additional, bool):
            out["additionalProperties"] = additional
        elif isinstance(additional, dict):
            out["additionalProperties"] = cls._safe_mcp_schema(additional, depth=depth + 1)
        enum_values = schema.get("enum")
        if isinstance(enum_values, list):
            rendered = []
            for item in enum_values[:30]:
                if isinstance(item, (str, int, float, bool)) or item is None:
                    rendered.append(cls._safe_mcp_text(item, max_chars=160))
            if rendered:
                out["enum"] = rendered
        out.setdefault("type", "object" if out.get("properties") is not None else str(schema.get("type") or ""))
        return out

    @classmethod
    def _safe_mcp_annotations(cls, annotations: Any) -> dict[str, Any]:
        if not isinstance(annotations, dict):
            return {}
        allowed = {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        out: dict[str, Any] = {}
        for key in allowed:
            if key not in annotations:
                continue
            value = annotations.get(key)
            if isinstance(value, bool):
                out[key] = value
            elif isinstance(value, (int, float)):
                out[key] = value
            elif isinstance(value, str):
                out[key] = cls._safe_mcp_text(value, max_chars=300)
        return out

    @classmethod
    def _mcp_schema_type(cls, schema: Any) -> str:
        if not isinstance(schema, dict):
            return ""
        raw = schema.get("type")
        if isinstance(raw, str):
            return raw
        if isinstance(raw, list):
            return "|".join(str(x) for x in raw if isinstance(x, str))
        for key in ("anyOf", "oneOf", "allOf"):
            value = schema.get(key)
            if isinstance(value, list):
                nested = [cls._mcp_schema_type(item) for item in value if isinstance(item, dict)]
                nested = [x for x in nested if x]
                if nested:
                    return "|".join(dict.fromkeys(nested))
        if isinstance(schema.get("items"), dict):
            return "array"
        if isinstance(schema.get("properties"), dict):
            return "object"
        return ""

    @classmethod
    def _mcp_parameters_from_schema(cls, schema: Any) -> list[dict[str, Any]]:
        safe_schema = cls._safe_mcp_schema(schema)
        properties = safe_schema.get("properties") if isinstance(safe_schema, dict) else {}
        if not isinstance(properties, dict):
            return []
        required = {str(x) for x in safe_schema.get("required", []) if isinstance(x, str)}
        rows: list[dict[str, Any]] = []
        for name, prop in properties.items():
            prop = prop if isinstance(prop, dict) else {}
            desc = cls._safe_mcp_text(prop.get("description") or prop.get("title") or "", max_chars=800)
            row: dict[str, Any] = {
                "name": str(name),
                "type": cls._mcp_schema_type(prop) or "unknown",
                "description": desc,
                "required": str(name) in required,
            }
            if isinstance(prop.get("enum"), list):
                row["enum"] = prop.get("enum")[:30]
            rows.append(row)
        return rows

    @classmethod
    def _mcp_server_description(cls, cfg: Any) -> str:
        # Current MCP config intentionally exposes no command/env/headers/url.  If a
        # future safe display field is added, surface only that human description.
        for attr in ("description", "summary"):
            value = getattr(cfg, attr, "") if cfg is not None else ""
            if value:
                return cls._safe_mcp_text(value, max_chars=1000)
        return ""

    @classmethod
    def _safe_mcp_prompt(cls, server_key: str, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        args = []
        for arg in item.get("arguments") if isinstance(item.get("arguments"), list) else []:
            if not isinstance(arg, dict):
                continue
            args.append({
                "name": cls._safe_mcp_text(arg.get("name") or "", max_chars=160),
                "description": cls._safe_mcp_text(arg.get("description") or "", max_chars=800),
                "required": bool(arg.get("required")),
            })
        return {
            "serverKey": server_key,
            "serverName": server_key,
            "name": cls._safe_mcp_text(item.get("name") or "", max_chars=160),
            "title": cls._safe_mcp_text(item.get("title") or "", max_chars=300),
            "description": cls._safe_mcp_text(item.get("description") or "", max_chars=1000),
            "arguments": args[:50],
        }

    async def handle_api_mcp_status(self, request: web.Request) -> web.Response:
        manager = getattr(self, "mcp", None)
        if manager is None:
            return web.json_response({
                "ok": True,
                "enabled": False,
                "summary": {
                    "enabled": False,
                    "serverCount": 0,
                    "connectedCount": 0,
                    "failedCount": 0,
                    "visibleTools": 0,
                    "filteredTools": 0,
                },
                "servers": [],
                "tools": [],
                "sensitiveConfigHidden": True,
                "note": "MCP 管理只返回安全摘要；command/env/headers/token/apiKey 不会从 Web API 返回。",
            })
        snapshot = manager.status_snapshot()
        all_tool_meta = manager.all_tools_snapshot()
        prompt_meta = manager.prompts_snapshot() if hasattr(manager, "prompts_snapshot") else {}
        visible_tools = [meta for meta in all_tool_meta if not meta.filtered]
        filtered_tools = [meta for meta in all_tool_meta if meta.filtered]
        tools_by_server: dict[str, dict[str, Any]] = {}
        risk_counts: dict[str, int] = {}
        approval_counts: dict[str, int] = {}
        for meta in all_tool_meta:
            row = tools_by_server.setdefault(meta.server_key, {"total": 0, "visible": 0, "filtered": 0, "riskCounts": {}, "approvalCounts": {}})
            row["total"] += 1
            row["filtered" if meta.filtered else "visible"] += 1
            row["riskCounts"][meta.risk] = int(row["riskCounts"].get(meta.risk, 0)) + 1
            row["approvalCounts"][meta.approval] = int(row["approvalCounts"].get(meta.approval, 0)) + 1
            risk_counts[meta.risk] = int(risk_counts.get(meta.risk, 0)) + 1
            approval_counts[meta.approval] = int(approval_counts.get(meta.approval, 0)) + 1

        configured_servers = getattr(getattr(manager, "mcp_config", None), "servers", {}) or {}
        servers = []
        for server in snapshot.servers:
            cfg = configured_servers.get(server.key)
            counts = tools_by_server.get(server.key, {"total": 0, "visible": 0, "filtered": 0, "riskCounts": {}, "approvalCounts": {}})
            servers.append({
                "key": server.key,
                "name": server.key,
                "displayName": server.key,
                "description": self._mcp_server_description(cfg),
                "transport": server.transport,
                "status": server.status,
                "enabled": bool(getattr(cfg, "enabled", server.status != "disabled")) if cfg is not None else server.status != "disabled",
                "required": bool(server.required),
                "approval": server.approval,
                "approvalSource": "server" if cfg is not None and getattr(cfg, "approval", None) else "default",
                "toolCount": int(server.tool_count or counts.get("visible") or 0),
                "visibleTools": int(counts.get("visible") or server.tool_count or 0),
                "filteredTools": int(counts.get("filtered") or 0),
                "totalTools": int(counts.get("total") or 0),
                "riskCounts": counts.get("riskCounts") or {},
                "approvalCounts": counts.get("approvalCounts") or {},
                "lastConnectedAt": server.last_connected_at,
                "lastFailedAt": server.last_failed_at,
                "lastConnected": server.last_connected_at,
                "lastFailed": server.last_failed_at,
                "errorPresent": bool(server.error),
                "errorHidden": bool(server.error),
            })

        tools = [
            {
                "publicName": meta.public_name,
                "serverKey": meta.server_key,
                "serverName": meta.server_key,
                "originalToolName": meta.original_tool_name,
                "normalizedToolName": meta.normalized_tool_name,
                "description": self._safe_mcp_text(meta.description, max_chars=2000),
                "inputSchema": self._safe_mcp_schema(meta.input_schema),
                "parameters": self._mcp_parameters_from_schema(meta.input_schema),
                "annotations": self._safe_mcp_annotations(meta.annotations),
                "risk": meta.risk,
                "approval": meta.approval,
                "visible": not meta.filtered,
                "filtered": meta.filtered,
                "filterReason": meta.filter_reason,
            }
            for meta in all_tool_meta
        ]
        prompts = [
            prompt
            for server_key, items in (prompt_meta or {}).items()
            for prompt in [self._safe_mcp_prompt(str(server_key), item) for item in (items or [])]
            if prompt.get("name")
        ]
        status_counts: dict[str, int] = {}
        for server in servers:
            status = str(server.get("status") or "unknown")
            status_counts[status] = int(status_counts.get(status, 0)) + 1
        summary = {
            "enabled": bool(snapshot.enabled),
            "serverCount": len(servers),
            "connectedCount": int(status_counts.get("connected", 0)),
            "failedCount": int(status_counts.get("failed", 0)),
            "disabledCount": int(status_counts.get("disabled", 0)),
            "pendingCount": int(status_counts.get("pending", 0)),
            "totalTools": len(all_tool_meta),
            "visibleTools": len(visible_tools),
            "filteredTools": len(filtered_tools),
            "promptCount": len(prompts),
            "riskCounts": risk_counts,
            "approvalCounts": approval_counts,
            "statusCounts": status_counts,
        }
        return web.json_response({
            "ok": True,
            "enabled": snapshot.enabled,
            "summary": summary,
            "servers": servers,
            "tools": tools,
            "prompts": prompts,
            "sensitiveConfigHidden": True,
            "settingsAvailable": self._config_writer_available(),
            "note": "MCP 管理只返回安全摘要；command/env/headers/token/apiKey 不会从 Web API 返回。卸载只移除 OpenBear 注册，不操作外部软件。",
        })

    @staticmethod
    def _mcp_enabled_value(body: dict[str, Any]) -> bool:
        if "enabled" not in body:
            raise ValueError("enabled_required")
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled_must_be_boolean")
        return enabled

    @staticmethod
    def _mcp_approval_value(body: dict[str, Any]) -> str:
        approval = str(body.get("approval") or "").strip().lower()
        if approval not in {"allow", "ask", "deny"}:
            raise ValueError("approval_invalid")
        return approval

    @staticmethod
    def _safe_mcp_update_error(exc: Exception) -> dict[str, Any]:
        """Return a non-secret error payload for MCP config writes.

        Pydantic validation errors may include the rejected input value. MCP config
        can contain command/env/headers/token/apiKey, so never echo str(exc) to the
        browser. Only our own sentinel validation codes are allowed through.
        """
        allowed = {"enabled_required", "enabled_must_be_boolean", "server_required", "approval_invalid", "mcp_server_config_invalid"}
        code = ""
        if isinstance(exc, ValueError) and exc.args:
            raw = exc.args[0]
            code = raw if isinstance(raw, str) and raw in allowed else ""
        return {"ok": False, "error": code or "config_update_failed", "errorType": type(exc).__name__, "sensitiveConfigHidden": True}

    async def _mcp_reload_response_after_config_write(
        self,
        request: web.Request,
        *,
        audit_kind: str,
        detail: dict[str, Any],
    ) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        try:
            result = await self._mcp_reload_hook()
        except Exception as exc:
            log.warning("Web MCP config update reload hook failed", 错误类型=type(exc).__name__)
            await self.audit(
                audit_kind,
                actor="web",
                chat_id=session.chat_id,
                ip=request.remote or "",
                detail={**detail, "ok": False, "error": "mcp_reload_failed", "errorType": type(exc).__name__},
            )
            return web.json_response({"ok": False, "error": "mcp_reload_failed", "errorType": type(exc).__name__}, status=400)
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        await self.audit(
            audit_kind,
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={
                **detail,
                "ok": bool(result.get("ok")),
                "enabled": bool(result.get("enabled")),
                "changed": bool(result.get("changed")),
                "reloaded": bool(result.get("reloaded")),
                "servers": int(result.get("servers") or summary.get("serverCount") or 0),
                "tools": int(result.get("tools") or summary.get("visibleTools") or 0),
                "message": str(result.get("message") or "")[:120],
            },
        )
        payload = dict(result)
        payload["revision"] = int(getattr(self.config_store, "revision", 0) or 0)
        payload["sensitiveConfigHidden"] = True
        payload.setdefault("note", "MCP 管理只返回安全摘要；command/env/headers/token/apiKey 不会从 Web API 返回。")
        return web.json_response(payload, status=200 if result.get("ok") else 400)

    async def handle_api_mcp_enabled(self, request: web.Request) -> web.Response:
        if not self._config_writer_available():
            return web.json_response({"ok": False, "error": "config_store_unavailable"}, status=503)
        if self._mcp_reload_hook is None:
            return web.json_response({"ok": False, "error": "mcp_reload_unavailable"}, status=503)
        body = await self._json_body(request)
        try:
            enabled = self._mcp_enabled_value(body)
            await self.config_store.update_path("mcp.enabled", enabled)
        except Exception as exc:
            log.warning("Web MCP enabled update failed", 错误类型=type(exc).__name__)
            return web.json_response(self._safe_mcp_update_error(exc), status=400)
        return await self._mcp_reload_response_after_config_write(
            request,
            audit_kind="web.mcp.enabled.update",
            detail={"scope": "mcp", "enabled": enabled},
        )

    async def handle_api_mcp_server_enabled(self, request: web.Request) -> web.Response:
        if not self._config_writer_available():
            return web.json_response({"ok": False, "error": "config_store_unavailable"}, status=503)
        if self._mcp_reload_hook is None:
            return web.json_response({"ok": False, "error": "mcp_reload_unavailable"}, status=503)
        server = str(request.match_info.get("server") or "").strip()
        if not server:
            return web.json_response({"ok": False, "error": "server_required"}, status=400)
        body = await self._json_body(request)
        try:
            enabled = self._mcp_enabled_value(body)

            def mutator(raw: dict[str, Any]) -> None:
                mcp = raw.get("mcp")
                servers = mcp.get("servers") if isinstance(mcp, dict) else None
                if not isinstance(servers, dict) or server not in servers:
                    raise _MCPServerNotFoundError(server)
                server_config = servers.get(server)
                if not isinstance(server_config, dict):
                    raise ValueError("mcp_server_config_invalid")
                server_config["enabled"] = enabled

            await self.config_store.mutate(mutator)
        except _MCPServerNotFoundError:
            return web.json_response({"ok": False, "error": "mcp_server_not_found", "server": server}, status=404)
        except Exception as exc:
            log.warning("Web MCP server enabled update failed", 错误类型=type(exc).__name__, server=server)
            return web.json_response(self._safe_mcp_update_error(exc), status=400)
        return await self._mcp_reload_response_after_config_write(
            request,
            audit_kind="web.mcp.server.enabled.update",
            detail={"scope": "mcp.server", "server": server, "enabled": enabled},
        )

    async def handle_api_mcp_server_approval(self, request: web.Request) -> web.Response:
        if not self._config_writer_available():
            return web.json_response({"ok": False, "error": "config_store_unavailable"}, status=503)
        if self._mcp_reload_hook is None:
            return web.json_response({"ok": False, "error": "mcp_reload_unavailable"}, status=503)
        server = str(request.match_info.get("server") or "").strip()
        if not server:
            return web.json_response({"ok": False, "error": "server_required"}, status=400)
        body = await self._json_body(request)
        try:
            approval = self._mcp_approval_value(body)

            def mutator(raw: dict[str, Any]) -> None:
                mcp = raw.get("mcp")
                servers = mcp.get("servers") if isinstance(mcp, dict) else None
                if not isinstance(servers, dict) or server not in servers:
                    raise _MCPServerNotFoundError(server)
                server_config = servers.get(server)
                if not isinstance(server_config, dict):
                    raise ValueError("mcp_server_config_invalid")
                server_config["approval"] = approval

            await self.config_store.mutate(mutator)
        except _MCPServerNotFoundError:
            return web.json_response({"ok": False, "error": "mcp_server_not_found", "server": server}, status=404)
        except Exception as exc:
            log.warning("Web MCP server approval update failed", 错误类型=type(exc).__name__, server=server)
            return web.json_response(self._safe_mcp_update_error(exc), status=400)
        return await self._mcp_reload_response_after_config_write(
            request,
            audit_kind="web.mcp.server.approval.update",
            detail={"scope": "mcp.server", "server": server, "approval": approval},
        )

    async def handle_api_mcp_server_uninstall(self, request: web.Request) -> web.Response:
        if not self._config_writer_available():
            return web.json_response({"ok": False, "error": "config_store_unavailable"}, status=503)
        if self._mcp_reload_hook is None:
            return web.json_response({"ok": False, "error": "mcp_reload_unavailable"}, status=503)
        manager = getattr(self, "mcp", None)
        if manager is None or not hasattr(manager, "begin_server_uninstall"):
            return web.json_response({"ok": False, "error": "mcp_manager_unavailable"}, status=503)
        server = str(request.match_info.get("server") or "").strip()
        if not server:
            return web.json_response({"ok": False, "error": "server_required"}, status=400)
        body = await self._json_body(request)
        if body.get("confirm") is not True or str(body.get("name") or "") != server:
            return web.json_response({"ok": False, "error": "confirmation_name_mismatch"}, status=400)

        reserved, active_calls = await manager.begin_server_uninstall(server)
        if not reserved:
            return web.json_response({
                "ok": False,
                "error": "mcp_server_busy",
                "server": server,
                "activeCalls": active_calls,
            }, status=409)

        session: WebSession = request[_WEB_SESSION_KEY]
        snapshot = None
        try:
            def mutator(raw: dict[str, Any]) -> None:
                mcp = raw.get("mcp")
                servers = mcp.get("servers") if isinstance(mcp, dict) else None
                if not isinstance(servers, dict) or server not in servers:
                    raise _MCPServerNotFoundError(server)
                del servers[server]

            try:
                snapshot = await self.config_store.mutate_with_snapshot(mutator)
            except _MCPServerNotFoundError:
                return web.json_response({"ok": False, "error": "mcp_server_not_found", "server": server}, status=404)
            except Exception as exc:
                log.warning("Web MCP server uninstall config write failed", 错误类型=type(exc).__name__, server=server)
                return web.json_response(self._safe_mcp_update_error(exc), status=400)

            try:
                result = await self._mcp_reload_hook()
            except Exception as exc:
                result = {"ok": False, "error": "mcp_reload_failed", "errorType": type(exc).__name__}
            runtime_servers = getattr(getattr(manager, "mcp_config", None), "servers", None)
            if result.get("ok") and isinstance(runtime_servers, dict) and server in runtime_servers:
                result = {"ok": False, "error": "mcp_uninstall_not_applied"}
            if not result.get("ok"):
                try:
                    await self.config_store.restore_snapshot(snapshot)
                except ConfigConflictError:
                    await self.audit(
                        "web.mcp.server.uninstall",
                        actor="web",
                        chat_id=session.chat_id,
                        ip=request.remote or "",
                        detail={"server": server, "ok": False, "error": "config_rollback_conflict"},
                    )
                    return web.json_response({
                        "ok": False,
                        "error": "config_rollback_conflict",
                        "server": server,
                        "sensitiveConfigHidden": True,
                    }, status=409)
                except Exception as exc:
                    log.error("Web MCP uninstall config rollback failed", 错误类型=type(exc).__name__, server=server)
                    return web.json_response({
                        "ok": False,
                        "error": "config_rollback_failed",
                        "errorType": type(exc).__name__,
                        "server": server,
                        "sensitiveConfigHidden": True,
                    }, status=500)
                try:
                    rollback_result = await self._mcp_reload_hook()
                except Exception as exc:
                    rollback_result = {"ok": False, "errorType": type(exc).__name__}
                await self.audit(
                    "web.mcp.server.uninstall",
                    actor="web",
                    chat_id=session.chat_id,
                    ip=request.remote or "",
                    detail={
                        "server": server,
                        "ok": False,
                        "error": "mcp_reload_failed_rolled_back",
                        "runtimeRestored": bool(rollback_result.get("ok")),
                    },
                )
                return web.json_response({
                    "ok": False,
                    "error": "mcp_reload_failed_rolled_back",
                    "server": server,
                    "runtimeRestored": bool(rollback_result.get("ok")),
                    "sensitiveConfigHidden": True,
                }, status=400 if rollback_result.get("ok") else 500)

            await self.audit(
                "web.mcp.server.uninstall",
                actor="web",
                chat_id=session.chat_id,
                ip=request.remote or "",
                detail={"server": server, "ok": True},
            )
            payload = dict(result)
            payload.update({
                "ok": True,
                "uninstalled": True,
                "server": server,
                "revision": int(getattr(self.config_store, "revision", 0) or 0),
                "sensitiveConfigHidden": True,
                "note": "仅移除 OpenBear MCP 注册；未卸载外部软件或关闭远程服务。",
            })
            return web.json_response(payload)
        finally:
            await manager.end_server_uninstall(server)

    async def handle_api_mcp_reload(self, request: web.Request) -> web.Response:
        if self._mcp_reload_hook is None:
            return web.json_response({"ok": False, "error": "mcp_reload_unavailable"}, status=503)
        session: WebSession = request[_WEB_SESSION_KEY]
        try:
            result = await self._mcp_reload_hook()
        except Exception as exc:
            log.warning("Web MCP reload hook failed", 错误类型=type(exc).__name__)
            return web.json_response({"ok": False, "error": "mcp_reload_failed", "errorType": type(exc).__name__}, status=400)
        await self.audit(
            "web.mcp.reload",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={
                "ok": bool(result.get("ok")),
                "enabled": bool(result.get("enabled")),
                "changed": bool(result.get("changed")),
                "reloaded": bool(result.get("reloaded")),
                "servers": int(result.get("servers") or result.get("summary", {}).get("serverCount") or 0),
                "tools": int(result.get("tools") or result.get("summary", {}).get("visibleTools") or 0),
                "message": str(result.get("message") or "")[:120],
            },
        )
        return web.json_response(result, status=200 if result.get("ok") else 400)

    async def handle_api_system_restart(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        confirm = bool(body.get("confirm"))
        force = bool(body.get("force"))
        reason = str(body.get("reason") or "web restart").strip()[:200]
        running = await self._restart_running_json()
        if running.get("busy") and not force:
            return web.json_response({"ok": False, "error": "system_busy", "running": running}, status=409)
        if not confirm:
            return web.json_response({"ok": False, "error": "confirm_required", "running": running}, status=400)
        try:
            await schedule_openbear_restart_with_completion_notice(
                self,
                chat_id=session.chat_id,
                delay_s=1.0,
                reason=reason,
                requested_by="web",
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"restart_schedule_failed: {type(exc).__name__}: {exc}"}, status=500)
        await self.audit("system.restart", actor="web", chat_id=session.chat_id, ip=request.remote or "", detail={"reason": reason, "force": force})
        return web.json_response({"ok": True, "scheduled": True})

    def _config_writer_available(self) -> bool:
        return self.config_store is not None

    def _apply_runtime_config(self, config: Config) -> None:
        if self._apply_config_hook is not None:
            self._apply_config_hook(config)
        else:
            self.apply_config(config)

__all__ = [name for name in globals() if not name.startswith("__")]
