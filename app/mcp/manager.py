"""MCPManager: lifecycle, registration metadata, permission and calls."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Config, MCPServerConfig
from app.logging import get_logger
from app.mcp.audit import record_audit
from app.mcp.client import MCPClient
from app.mcp.output import format_mcp_result, redact_text_secrets
from app.mcp.permissions import build_tool_meta, can_call_without_prompt, summarize_arguments
from app.mcp.types import MCPManagerState, MCPServerState, MCPToolMeta
from app.tools.base import ToolRuntimeContext

log = get_logger("mcp")


class MCPManager:
    def __init__(
        self,
        config: Config,
        *,
        interactions: Any = None,
        db: Any = None,
        approval_updater: Callable[[str, str], Awaitable[Any]] | None = None,
        tools_changed_callback: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.config = config
        self.mcp_config = config.mcp
        self.interactions = interactions
        self.db = db
        self.approval_updater = approval_updater
        self.tools_changed_callback = tools_changed_callback
        self._clients: dict[str, MCPClient] = {}
        self._states: dict[str, MCPServerState] = {}
        self._tools: dict[str, MCPToolMeta] = {}
        self._all_tools: list[MCPToolMeta] = []
        self._prompts: dict[str, list[dict[str, Any]]] = {}
        self._server_instructions: dict[str, str] = {}
        # (conversation, server, original tool). Grants are cleared on every MCP
        # config generation change, so they can never authorize a replacement server.
        self._conversation_grants: set[tuple[str, str, str]] = set()
        self._registry_lock = asyncio.Lock()
        self._call_lifecycle_lock = asyncio.Lock()
        self._active_calls: dict[str, int] = {}
        self._draining_servers: set[str] = set()
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._closed = False
        if not self.mcp_config.enabled:
            log.info("mcp.disabled")
            return
        tasks = []
        for server_key, server_config in self.mcp_config.servers.items():
            if not server_config.enabled:
                self._states[server_key] = MCPServerState(
                    key=server_key,
                    transport=server_config.transport,
                    status="disabled",
                    required=server_config.required,
                    approval=_server_approval(self.mcp_config, server_config),
                )
                continue
            tasks.append(self._start_one(server_key, server_config))
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        required_errors = [str(item) for item in results if isinstance(item, Exception)]
        if required_errors:
            # Required server startup may fail after other MCP child processes have
            # already connected.  Clean them here because Services.startup/main may
            # not reach normal shutdown on startup exceptions.
            await self.close()
            raise RuntimeError("; ".join(required_errors))

    async def _start_one(self, server_key: str, server_config: MCPServerConfig) -> None:
        self._states[server_key] = MCPServerState(
            key=server_key,
            transport=server_config.transport,
            status="pending",
            required=server_config.required,
            approval=_server_approval(self.mcp_config, server_config),
        )
        log.info("mcp.server.starting", server=server_key, transport=server_config.transport, required=server_config.required)
        client = MCPClient(server_key, server_config)
        client.set_notification_handler(
            lambda method, params, key=server_key: self._handle_server_notification(key, method, params)
        )
        try:
            connect_timeout_s = float(server_config.connect_timeout_s or self.mcp_config.startup_timeout_s)
            client_connect_timeout_s = float(server_config.connect_timeout_s or 20)
            await asyncio.wait_for(
                client.connect(),
                timeout=_connect_outer_timeout(connect_timeout_s, client_connect_timeout_s, server_config),
            )
            raw_tools = await asyncio.wait_for(client.list_tools(), timeout=connect_timeout_s)
            raw_prompts = await self._list_prompts(client, timeout_s=connect_timeout_s)
            visible = 0
            async with self._registry_lock:
                self._prompts[server_key] = raw_prompts
                if client.instructions:
                    self._server_instructions[server_key] = client.instructions
                used = set(self._tools.keys())
                for raw_tool in raw_tools:
                    meta = build_tool_meta(
                        self.mcp_config,
                        server_config,
                        server_key=server_key,
                        raw_tool=raw_tool,
                        used_names=used,
                    )
                    self._all_tools.append(meta)
                    if meta.filtered:
                        log.info(
                            "mcp.tool.filtered",
                            server=server_key,
                            tool=raw_tool.name,
                            public=meta.public_name,
                            reason=meta.filter_reason,
                        )
                        continue
                    self._tools[meta.public_name] = meta
                    visible += 1
            self._clients[server_key] = client
            self._states[server_key] = MCPServerState(
                key=server_key,
                transport=server_config.transport,
                status="connected",
                tool_count=visible,
                required=server_config.required,
                last_connected_at=time.time(),
                approval=_server_approval(self.mcp_config, server_config),
            )
            log.info("mcp.server.connected", server=server_key, transport=server_config.transport, tools=visible, prompts=len(raw_prompts))
            log.info("mcp.tools.discovered", server=server_key, total=len(raw_tools), visible=visible)
            if raw_prompts:
                log.info("mcp.prompts.discovered", server=server_key, total=len(raw_prompts))
            await record_audit(self.db, "mcp.server.started", detail={"server": server_key, "transport": server_config.transport, "tools": visible, "prompts": len(raw_prompts)})
        except Exception as exc:
            await client.close()
            err = _short_error(exc)
            self._states[server_key] = MCPServerState(
                key=server_key,
                transport=server_config.transport,
                status="failed",
                required=server_config.required,
                error=err,
                last_failed_at=time.time(),
                approval=_server_approval(self.mcp_config, server_config),
            )
            log.warning("mcp.server.failed", server=server_key, transport=server_config.transport, required=server_config.required, error=err)
            await record_audit(self.db, "mcp.server.failed", detail={"server": server_key, "transport": server_config.transport, "error": err})
            if server_config.required:
                raise RuntimeError(f"required MCP server {server_key} failed: {err}") from exc

    async def _list_prompts(self, client: MCPClient, *, timeout_s: float) -> list[dict[str, Any]]:
        """Best-effort prompt discovery for management UI.

        Prompts are not registered as callable OpenBear tools, but showing them in
        /mcp makes servers like Ponytail understandable.  Missing prompt support is
        normal for many MCP servers, so failures are ignored.
        """
        try:
            return await asyncio.wait_for(
                client.list_prompts(),
                timeout=max(1.0, float(timeout_s or 1)),
            )
        except Exception:
            return []

    async def _handle_server_notification(
        self,
        server_key: str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        del params
        if method != "notifications/tools/list_changed":
            return
        client = self._clients.get(server_key)
        server_config = self.mcp_config.servers.get(server_key)
        if client is None or server_config is None:
            return
        try:
            timeout_s = float(server_config.connect_timeout_s or self.mcp_config.startup_timeout_s)
            raw_tools = await asyncio.wait_for(client.list_tools(), timeout=timeout_s)
            replacements: list[MCPToolMeta] = []
            visible: dict[str, MCPToolMeta] = {}
            async with self._registry_lock:
                # Ignore a stale notification that completed after a hot swap.
                if self._clients.get(server_key) is not client:
                    return
                retained_all = [meta for meta in self._all_tools if meta.server_key != server_key]
                retained_visible = {
                    name: meta for name, meta in self._tools.items() if meta.server_key != server_key
                }
                used = set(retained_visible)
                for raw_tool in raw_tools:
                    meta = build_tool_meta(
                        self.mcp_config,
                        server_config,
                        server_key=server_key,
                        raw_tool=raw_tool,
                        used_names=used,
                    )
                    replacements.append(meta)
                    if not meta.filtered:
                        visible[meta.public_name] = meta
                self._all_tools = retained_all + replacements
                self._tools = {**retained_visible, **visible}
                state = self._states.get(server_key)
                if state is not None:
                    state.tool_count = len(visible)
            log.info(
                "mcp.tools.list_changed",
                server=server_key,
                total=len(replacements),
                visible=len(visible),
            )
            await record_audit(
                self.db,
                "mcp.tools.list_changed",
                detail={"server": server_key, "total": len(replacements), "visible": len(visible)},
            )
            if self.tools_changed_callback is not None:
                await self.tools_changed_callback()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("mcp.tools.refresh_failed", server=server_key, error=_short_error(exc))
            await record_audit(
                self.db,
                "mcp.tools.refresh_failed",
                detail={"server": server_key, "error": _short_error(exc)},
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        clients = list(self._clients.items())
        self._clients.clear()
        for server_key, client in clients:
            try:
                await client.close()
            except Exception as exc:
                log.warning("mcp.server.close_failed", server=server_key, error=_short_error(exc))
            state = self._states.get(server_key)
            if state is not None:
                state.status = "stopped"
            log.info("mcp.server.stopped", server=server_key)
            await record_audit(self.db, "mcp.server.stopped", detail={"server": server_key})

    async def prepare_reload(self, config: Config) -> tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]]:
        """Prepare a hot reload and return ``(commit, abort)`` callbacks.

        The current manager is left untouched while this method starts new MCP
        servers and discovers tools.  Call ``commit`` only after other runtime state
        is ready to switch.  If preparation raises, old clients/tools are still
        active.  If a prepared reload is superseded before commit, call ``abort`` to
        close the fresh-but-unused clients.
        """
        if not config.mcp.enabled:
            async def commit_disabled() -> None:
                old_clients = list(self._clients.items())
                self.config = config
                self.mcp_config = config.mcp
                self._clients = {}
                self._tools = {}
                self._all_tools = []
                self._prompts = {}
                self._server_instructions = {}
                self._conversation_grants.clear()
                self._states = {
                    key: MCPServerState(
                        key=key,
                        transport=cfg.transport,
                        status="disabled",
                        required=cfg.required,
                        approval=_server_approval(config.mcp, cfg),
                    )
                    for key, cfg in config.mcp.servers.items()
                }
                self._started = True
                self._closed = False
                await _close_clients(old_clients, db=self.db)
                await record_audit(self.db, "mcp.reload.disabled", detail={"servers": len(config.mcp.servers)})

            async def abort_disabled() -> None:
                return None

            return commit_disabled, abort_disabled

        fresh = MCPManager(
            config,
            interactions=self.interactions,
            db=self.db,
            approval_updater=self.approval_updater,
        )
        try:
            await fresh.start()
        except asyncio.CancelledError:
            await fresh.close()
            raise
        except Exception:
            await fresh.close()
            raise

        # Optional servers are allowed to fail during a cold start, but an atomic hot
        # reload must not replace an already-healthy server with a failed generation.
        failed_replacements = sorted(
            key
            for key, state in fresh._states.items()
            if state.status == "failed" and key in self._clients
        )
        if failed_replacements:
            await fresh.close()
            joined = ", ".join(failed_replacements)
            raise RuntimeError(f"MCP reload would replace healthy server(s) with failed generation: {joined}")

        async def commit_fresh() -> None:
            old_clients = list(self._clients.items())
            self.config = config
            self.mcp_config = config.mcp
            self._clients = fresh._clients
            for server_key, client in self._clients.items():
                client.set_notification_handler(
                    lambda method, params, key=server_key: self._handle_server_notification(key, method, params)
                )
            self._states = fresh._states
            self._tools = fresh._tools
            self._all_tools = fresh._all_tools
            self._prompts = fresh._prompts
            self._server_instructions = fresh._server_instructions
            # A grant is valid only for the exact MCP config/transport generation in
            # which the user approved it. Reloading clears all ephemeral grants.
            self._conversation_grants.clear()
            self._started = fresh._started
            self._closed = False
            # Ownership moved to this manager; prevent accidental double-close if a
            # future cleanup touches the temporary object.
            fresh._clients = {}
            await _close_clients(old_clients, db=self.db)
            await record_audit(self.db, "mcp.reload.completed", detail={"servers": len(config.mcp.servers), "tools": len(self._tools)})

        async def abort_fresh() -> None:
            await fresh.close()

        return commit_fresh, abort_fresh

    async def reload(self, config: Config) -> None:
        """Hot-reload MCP servers/tools for a new runtime config."""
        commit, _abort = await self.prepare_reload(config)
        await commit()

    def status_snapshot(self) -> MCPManagerState:
        """Return configured servers in deterministic config order.

        Connection tasks complete asynchronously, so ``_states`` insertion order is
        runtime timing rather than user configuration order.  Build the snapshot
        from the config keys first and append only unexpected runtime states after.
        """
        states = dict(self._states)
        ordered: list[MCPServerState] = []
        configured_keys: set[str] = set()
        for key, cfg in self.mcp_config.servers.items():
            configured_keys.add(key)
            ordered.append(states.get(key) or MCPServerState(
                key=key,
                transport=cfg.transport,
                status="disabled" if not self.mcp_config.enabled or not cfg.enabled else "pending",
                required=cfg.required,
                approval=_server_approval(self.mcp_config, cfg),
            ))
        ordered.extend(state for key, state in states.items() if key not in configured_keys)
        return MCPManagerState(enabled=self.mcp_config.enabled, servers=ordered)

    def available_tools(self) -> list[MCPToolMeta]:
        return list(self._tools.values())

    def all_tools_snapshot(self) -> list[MCPToolMeta]:
        return list(self._all_tools)

    def prompts_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {key: [dict(item) for item in items] for key, items in self._prompts.items()}

    def server_instructions_snapshot(self) -> list[dict[str, str]]:
        return [
            {"server": key, "instructions": value}
            for key, value in self._server_instructions.items()
            if value.strip()
        ]

    async def begin_server_uninstall(self, server_key: str) -> tuple[bool, int]:
        """Reserve a server for uninstall without interrupting an active call."""
        async with self._call_lifecycle_lock:
            active = self._active_calls.get(server_key, 0)
            if active or server_key in self._draining_servers:
                return False, active
            self._draining_servers.add(server_key)
            return True, 0

    async def end_server_uninstall(self, server_key: str) -> None:
        async with self._call_lifecycle_lock:
            self._draining_servers.discard(server_key)

    async def _begin_server_call(self, server_key: str) -> bool:
        async with self._call_lifecycle_lock:
            if server_key in self._draining_servers:
                return False
            self._active_calls[server_key] = self._active_calls.get(server_key, 0) + 1
            return True

    async def _end_server_call(self, server_key: str) -> None:
        async with self._call_lifecycle_lock:
            active = self._active_calls.get(server_key, 0)
            if active <= 1:
                self._active_calls.pop(server_key, None)
            else:
                self._active_calls[server_key] = active - 1

    async def call_tool(self, public_tool_name: str, arguments: dict[str, Any], context: ToolRuntimeContext) -> str:
        meta = self._tools.get(public_tool_name)
        if meta is None:
            return _json({"status": "error", "error": "mcp_tool_not_found", "tool": public_tool_name})
        client = self._clients.get(meta.server_key)
        if client is None:
            return _json({"status": "error", "error": "mcp_server_not_connected", "server": meta.server_key})
        if not await self._begin_server_call(meta.server_key):
            return _json({"status": "error", "error": "mcp_server_draining", "server": meta.server_key})
        try:
            return await self._call_tool_reserved(meta, client, arguments, context)
        finally:
            await self._end_server_call(meta.server_key)

    async def _call_tool_reserved(
        self,
        meta: MCPToolMeta,
        client: MCPClient,
        arguments: dict[str, Any],
        context: ToolRuntimeContext,
    ) -> str:
        persist_trust = False
        allowed, reason = can_call_without_prompt(meta, context)
        # Explicit deny is authoritative. A conversation grant can only satisfy an
        # otherwise-confirmable ask policy, and only for this exact tool.
        if (
            not allowed
            and reason in {"confirmation_required", "needs_openbear_control"}
            and self._has_conversation_grant(meta, context)
        ):
            allowed, reason = True, "allowed_by_conversation_grant"
        if not allowed:
            if reason == "confirmation_required":
                decision = await self._confirm_call(meta, arguments, context)
                if decision in {"conversation", "always"}:
                    self._grant_conversation(meta, context)
                persist_trust = decision == "always"
                if decision not in {"once", "conversation", "always"}:
                    log.info("mcp.tool.confirm_denied", server=meta.server_key, tool=meta.original_tool_name)
                    await record_audit(self.db, "mcp.tool.denied", actor=_actor(context), chat_id=context.chat_id, detail={"server": meta.server_key, "tool": meta.original_tool_name, "reason": "user_denied"})
                    return _json({"status": "denied", "error": "mcp_tool_call_denied", "server": meta.server_key, "tool": meta.original_tool_name})
            elif reason == "needs_openbear_control":
                return _json({
                    "status": "needs_openbear_control",
                    "reason": "mcp_tool_requires_openbear_control",
                    "error": "mcp_tool_requires_openbear_control",
                    "message": "MCP tool 需要 OpenBear 主控确认或显式授权，子 Agent/后台上下文不会弹交互确认。",
                    "server": meta.server_key,
                    "tool": meta.original_tool_name,
                    "risk": meta.risk,
                    "approval": meta.approval,
                })
            else:
                await record_audit(self.db, "mcp.tool.denied", actor=_actor(context), chat_id=context.chat_id, detail={"server": meta.server_key, "tool": meta.original_tool_name, "reason": reason})
                return _json({
                    "status": "denied",
                    "error": "mcp_tool_call_denied",
                    "reason": reason,
                    "server": meta.server_key,
                    "tool": meta.original_tool_name,
                    "risk": meta.risk,
                    "approval": meta.approval,
                })
        server_config = self.mcp_config.servers.get(meta.server_key)
        if server_config is None:
            return _json({"status": "error", "error": "mcp_server_not_configured", "server": meta.server_key})
        timeout_s = float(server_config.tool_call_timeout_s or self.mcp_config.tool_call_timeout_s)
        try:
            log.info("mcp.tool.called", server=meta.server_key, tool=meta.original_tool_name, public=meta.public_name, risk=meta.risk)
            raw_result = await asyncio.wait_for(
                client.call_tool(meta.original_tool_name, arguments or {}),
                timeout=timeout_s,
            )
            rendered = format_mcp_result(raw_result, meta, self.mcp_config)
            await record_audit(self.db, "mcp.tool.called", actor=_actor(context), chat_id=context.chat_id, detail={"server": meta.server_key, "tool": meta.original_tool_name, "risk": meta.risk, "resultChars": len(rendered)})
            if persist_trust:
                await self._persist_server_trust(meta, context)
            return rendered
        except TimeoutError:
            log.warning("mcp.tool.timeout", server=meta.server_key, tool=meta.original_tool_name)
            await record_audit(self.db, "mcp.tool.failed", actor=_actor(context), chat_id=context.chat_id, detail={"server": meta.server_key, "tool": meta.original_tool_name, "error": "timeout"})
            return _json({"status": "error", "error": "mcp_tool_timeout", "server": meta.server_key, "tool": meta.original_tool_name})
        except Exception as exc:
            err = _short_error(exc)
            log.warning("mcp.tool.failed", server=meta.server_key, tool=meta.original_tool_name, error=err)
            await record_audit(self.db, "mcp.tool.failed", actor=_actor(context), chat_id=context.chat_id, detail={"server": meta.server_key, "tool": meta.original_tool_name, "error": err})
            return _json({"status": "error", "error": "mcp_tool_failed", "server": meta.server_key, "tool": meta.original_tool_name, "detail": err})

    async def _confirm_call(self, meta: MCPToolMeta, arguments: dict[str, Any], context: ToolRuntimeContext) -> str:
        if not (context.source == "web" and context.web_confirm is not None):
            return "deny"
        log.info("mcp.tool.confirm_requested", server=meta.server_key, tool=meta.original_tool_name, risk=meta.risk, source=context.source)
        body = (
            f"MCP server: {meta.server_key}\n"
            f"Tool: {meta.original_tool_name}\n"
            f"Risk: {meta.risk}\n"
            f"Approval: {meta.approval}\n"
            f"Arguments 摘要:\n{summarize_arguments(arguments)}"
        )
        payload = {
            "action": "select",
            "title": "确认执行 MCP 工具",
            "body": body,
            "type": "warning",
            "options": [
                {"label": "仅本次允许", "value": "once"},
                {"label": "当前会话允许", "value": "conversation"},
                {"label": "始终信任此 MCP", "value": "always"},
            ],
            "defaultValues": ["once"],
            "confirmText": "执行",
            "cancelText": "拒绝",
            "timeoutSeconds": 600,
        }
        result = await context.web_confirm(payload)
        decision = "deny"
        if not bool(result.get("cancelled")):
            values = result.get("selectedValues")
            if isinstance(values, list) and values:
                candidate = str(values[0] or "").strip().lower()
                if candidate in {"once", "conversation", "always"}:
                    decision = candidate
            # Compatibility with older/binary confirmation callbacks used by
            # integrations and tests while the Web UI moves to select mode.
            elif bool(result.get("confirmed")):
                decision = "once"
        if decision != "deny":
            log.info("mcp.tool.confirm_approved", server=meta.server_key, tool=meta.original_tool_name, scope=decision)
            await record_audit(self.db, "mcp.tool.approved", actor=_actor(context), chat_id=context.chat_id, detail={"server": meta.server_key, "tool": meta.original_tool_name, "risk": meta.risk, "scope": decision})
        return decision

    @staticmethod
    def _conversation_key(context: ToolRuntimeContext) -> str:
        return str(context.conversation_uuid or context.session_uuid or "").strip()

    def _has_conversation_grant(self, meta: MCPToolMeta, context: ToolRuntimeContext) -> bool:
        key = self._conversation_key(context)
        grant = (key, meta.server_key, meta.original_tool_name)
        return bool(key and grant in self._conversation_grants)

    def _grant_conversation(self, meta: MCPToolMeta, context: ToolRuntimeContext) -> None:
        key = self._conversation_key(context)
        if key:
            self._conversation_grants.add((key, meta.server_key, meta.original_tool_name))

    async def _persist_server_trust(self, meta: MCPToolMeta, context: ToolRuntimeContext) -> None:
        if self.approval_updater is None:
            log.warning("mcp.tool.trust_persist_unavailable", server=meta.server_key)
            return
        try:
            await self.approval_updater(meta.server_key, "allow")
            await record_audit(self.db, "mcp.server.approval.updated", actor=_actor(context), chat_id=context.chat_id, detail={"server": meta.server_key, "approval": "allow", "source": "confirmation"})
        except Exception as exc:
            log.warning("mcp.tool.trust_persist_failed", server=meta.server_key, error=_short_error(exc))
            await record_audit(self.db, "mcp.server.approval.update_failed", actor=_actor(context), chat_id=context.chat_id, detail={"server": meta.server_key, "approval": "allow", "source": "confirmation", "error": _short_error(exc)})


async def _close_clients(clients: list[tuple[str, MCPClient]], *, db: Any = None) -> None:
    for server_key, client in clients:
        try:
            await client.close()
        except Exception as exc:
            log.warning("mcp.server.close_failed", server=server_key, error=_short_error(exc))
        log.info("mcp.server.stopped", server=server_key)
        await record_audit(db, "mcp.server.stopped", detail={"server": server_key})


def _server_approval(mcp_config: Any, server_config: MCPServerConfig) -> str:
    value = str(server_config.approval or getattr(mcp_config, "default_approval", "ask") or "ask").strip().lower()
    return value if value in {"allow", "ask", "deny"} else "ask"


def _connect_outer_timeout(connect_timeout_s: float, client_connect_timeout_s: float, server_config: MCPServerConfig) -> float:
    if str(getattr(server_config, "transport", "") or "") == "stdio" and str(getattr(server_config, "stdio_mode", "auto") or "auto") == "auto":
        # Auto stdio may retry initialize once with Content-Length framing after a
        # newline-json timeout.  The manager-level timeout must allow both attempts.
        return max(connect_timeout_s, client_connect_timeout_s * 2 + 1.0)
    return connect_timeout_s


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _short_error(exc: Exception) -> str:
    text = redact_text_secrets(f"{type(exc).__name__}: {exc}")
    return text[:1200]


def _actor(context: ToolRuntimeContext) -> str:
    if context.agent_session_uuid or context.agent_key:
        return f"agent:{context.agent_key or context.agent_session_uuid}"
    return context.source or "chat"
