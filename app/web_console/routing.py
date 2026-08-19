# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.web_console.core import *
from app.web_console.live_stream import *


class WebAdminAppMixin:
    def _allowed_origins(self, request: web.Request) -> set[str]:
        origins: set[str] = set()
        if self.config.web.custom_url:
            custom = _origin_key(self.config.web.custom_url)
            if custom:
                origins.add(custom)
        host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or ""
        proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",", 1)[0].strip()
        if host:
            current = _origin_key(f"{proto}://{host}")
            if current:
                origins.add(current)
        return origins

    def _origin_allowed(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin") or ""
        referer = request.headers.get("Referer") or ""
        if (request.headers.get("Upgrade") or "").lower() == "websocket":
            candidate = _origin_key(origin or referer)
            # Non-browser clients may omit Origin/Referer. Browser cross-site
            # WebSocket CSRF carries Origin; when present it must be same-origin.
            if not candidate:
                return True
            return candidate in self._allowed_origins(request)
        if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return True
        # 同源表单/旧客户端可能不带 Origin/Referer；保留兼容，带了就必须同源。
        candidate = _origin_key(origin or referer)
        if not candidate:
            return True
        return candidate in self._allowed_origins(request)

    def _cookie_secure(self, request: web.Request) -> bool:
        proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",", 1)[0].strip().lower()
        if proto == "https" or request.secure:
            return True
        return _origin_key(self.config.web.custom_url).startswith("https://")

    def make_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        app.add_routes([
            web.get("/health", self.handle_health),
            web.get("/", self.handle_index),
            web.get("/login", self.handle_index),
            web.get("/chat", self.handle_index),
            web.get("/memory", self.handle_index),
            web.get("/secrets", self.handle_index),
            web.get("/docs", self.handle_index),
            web.get("/mcp", self.handle_index),
            web.get("/settings", self.handle_index),
            web.get("/assets/{path:.*}", self.handle_asset),
            web.post("/api/auth/login/start", self.handle_login_post),
            web.get("/api/auth/login/status/{request_uuid}", self.handle_api_auth_status),
            web.post("/api/auth/login/consume/{request_uuid}", self.handle_api_auth_consume),
            web.get("/api/auth/session", self.handle_api_auth_session),
            web.get("/api/conversations", self.handle_api_conversations),
            web.post("/api/conversations", self.handle_api_conversation_create),
            web.get("/api/conversations/defaults", self.handle_api_conversation_defaults),
            web.patch("/api/conversations/defaults", self.handle_api_conversation_defaults_patch),
            web.get("/api/conversations/{conversation_uuid}/state", self.handle_api_conversation_state),
            web.get("/api/conversations/{conversation_uuid}/operations", self.handle_api_conversation_operations),
            web.get("/api/conversations/{conversation_uuid}/operations/{operation_id}/detail", self.handle_api_conversation_operation_detail),
            web.get("/api/conversations/{conversation_uuid}/compactions/{summary_id:\\d+}", self.handle_api_conversation_compaction),
            web.get("/api/conversations/{conversation_uuid}/frames", self.handle_api_conversation_frames),
            web.get("/api/conversations/{conversation_uuid}/artifacts", self.handle_api_conversation_artifacts),
            web.get("/api/conversations/{conversation_uuid}/artifacts/{artifact_uuid}", self.handle_api_conversation_artifact),
            web.get("/api/conversations/{conversation_uuid}/artifacts/{artifact_uuid}/content", self.handle_api_conversation_artifact_content),
            web.get("/api/conversations/{conversation_uuid}/ws", self.handle_api_conversation_ws),
            web.post("/api/conversations/{conversation_uuid}/compact", self.handle_api_conversation_compact),
            web.post("/api/conversations/{conversation_uuid}/stop", self.handle_api_conversation_stop),
            web.post("/api/conversations/{conversation_uuid}/retry/cancel", self.handle_api_conversation_retry_cancel),
            web.post("/api/conversations/{conversation_uuid}/confirmations/{confirmation_id}/answer", self.handle_api_conversation_confirmation_answer),
            web.patch("/api/conversations/{conversation_uuid}", self.handle_api_conversation_patch),
            web.get("/api/conversations/{conversation_uuid}/task-memories", self.handle_api_task_memories),
            web.get("/api/conversations/{conversation_uuid}/task-memories/tasks", self.handle_api_task_memory_tasks),
            web.get("/api/conversations/{conversation_uuid}/task-memories/preview", self.handle_api_task_memory_preview),
            web.get("/api/conversations/{conversation_uuid}/task-memories/{memory_uuid}", self.handle_api_task_memory_detail),
            web.post("/api/conversations/{conversation_uuid}/task-memories", self.handle_api_task_memory_create),
            web.patch("/api/conversations/{conversation_uuid}/task-memories/{memory_uuid}", self.handle_api_task_memory_update),
            web.delete("/api/conversations/{conversation_uuid}/task-memories/{memory_uuid}", self.handle_api_task_memory_delete),
            web.post("/api/conversations/{conversation_uuid}/task-memories/{memory_uuid}/restore", self.handle_api_task_memory_restore),
            web.post("/api/conversations/{conversation_uuid}/duplicate", self.handle_api_conversation_duplicate),
            web.post("/api/conversations/{conversation_uuid}/reorder", self.handle_api_conversation_reorder),
            web.post("/api/conversations/{conversation_uuid}/pin", self.handle_api_conversation_pin),
            web.post("/api/conversations/{conversation_uuid}/unpin", self.handle_api_conversation_unpin),
            web.post("/api/conversations/{conversation_uuid}/model", self.handle_api_conversation_model),
            web.post("/api/conversations/{conversation_uuid}/thinking", self.handle_api_conversation_thinking),
            web.post("/api/conversations/{conversation_uuid}/fast", self.handle_api_conversation_fast),
            web.post("/api/conversations/{conversation_uuid}/agent-run-config", self.handle_api_conversation_agent_run_config),
            web.delete("/api/conversations/{conversation_uuid}/turns/{turn_uuid}/suffix", self.handle_api_conversation_turn_suffix_delete),
            web.delete("/api/conversations/{conversation_uuid}", self.handle_api_conversation_delete),
            web.get("/api/audit-logs", self.handle_api_audit),
            web.get("/api/audit-logs/export.json", self.handle_api_audit_export),
            web.get("/api/audit-logs/{audit_id:\\d+}", self.handle_api_audit_detail),
            web.get("/api/mcp/status", self.handle_api_mcp_status),
            web.patch("/api/mcp/enabled", self.handle_api_mcp_enabled),
            web.patch("/api/mcp/servers/{server}/enabled", self.handle_api_mcp_server_enabled),
            web.patch("/api/mcp/servers/{server}/approval", self.handle_api_mcp_server_approval),
            web.post("/api/mcp/servers/{server}/uninstall", self.handle_api_mcp_server_uninstall),
            web.post("/api/mcp/reload", self.handle_api_mcp_reload),
            web.post("/api/system/restart", self.handle_api_system_restart),
            web.get("/api/system/version", self.handle_api_system_version),
            web.post("/api/system/update", self.handle_api_system_update),
            web.post("/api/system/update/ack", self.handle_api_system_update_ack),
            web.get("/api/settings/specs", self.handle_api_settings_specs),
            web.get("/api/settings", self.handle_api_settings_get),
            web.post("/api/settings/prompt-preview", self.handle_api_settings_prompt_preview),
            web.post("/api/settings/web-task-notifications/test", self.handle_api_web_task_notification_test),
            web.patch("/api/settings/{path:.+}", self.handle_api_settings_patch),
            web.get("/api/models-dev/status", self.handle_api_models_dev_status),
            web.post("/api/models-dev/refresh", self.handle_api_models_dev_refresh),
            web.get("/api/models-dev/providers", self.handle_api_models_dev_providers),
            web.get("/api/models-dev/provider-models", self.handle_api_models_dev_provider_models),
            web.get("/api/channels", self.handle_api_channels),
            web.post("/api/channels", self.handle_api_channel_create),
            web.post("/api/channels/reorder", self.handle_api_channels_reorder),
            web.post("/api/channels/primary", self.handle_api_channels_primary),
            web.post("/api/channels/compression", self.handle_api_channels_compression),
            web.post("/api/channels/models/fetch", self.handle_api_channels_fetch_models),
            web.get("/api/channels/{name}", self.handle_api_channel_detail),
            web.patch("/api/channels/{name}", self.handle_api_channel_update),
            web.delete("/api/channels/{name}", self.handle_api_channel_delete),
            web.post("/api/channels/{name}/test", self.handle_api_channel_test),
            web.get("/api/channels/{name}/test/{job_uuid}", self.handle_api_channel_test_status),
            web.post("/api/channels/{name}/models", self.handle_api_channel_model_create),
            web.post("/api/channels/{name}/models/reorder", self.handle_api_channel_models_reorder),
            web.get("/api/channels/{name}/models-dev/matches", self.handle_api_channel_models_dev_matches),
            web.post("/api/channels/{name}/models-dev/preview", self.handle_api_channel_models_dev_batch_preview),
            web.post("/api/channels/{name}/models-dev/sync", self.handle_api_channel_models_dev_batch_sync),
            web.post("/api/channels/{name}/models/{model_id}/test", self.handle_api_channel_model_test),
            web.post("/api/channels/{name}/models/{model_id}/models-dev/preview", self.handle_api_channel_model_models_dev_preview),
            web.post("/api/channels/{name}/models/{model_id}/models-dev/sync", self.handle_api_channel_model_models_dev_sync),
            web.patch("/api/channels/{name}/models/{model_id}", self.handle_api_channel_model_update),
            web.delete("/api/channels/{name}/models/{model_id}", self.handle_api_channel_model_delete),
            web.post("/api/auth/logout", self.handle_api_logout),
            web.get("/api/memory/categories", self.handle_api_memory_categories),
            web.get("/api/memory/entries", self.handle_api_memory_entries),
            web.get("/api/memory/entries/{item_id}", self.handle_api_memory_entry_detail),
            web.post("/api/memory/entries", self.handle_api_memory_entry_create),
            web.put("/api/memory/entries/{item_id}", self.handle_api_memory_entry_update),
            web.delete("/api/memory/entries/{item_id}", self.handle_api_memory_entry_delete),
            web.get("/api/memory/secrets", self.handle_api_memory_secrets),
            web.get("/api/memory/secrets/{item_id}", self.handle_api_memory_secret_detail),
            web.post("/api/memory/secrets", self.handle_api_memory_secret_create),
            web.put("/api/memory/secrets/{item_id}", self.handle_api_memory_secret_update),
            web.delete("/api/memory/secrets/{item_id}", self.handle_api_memory_secret_delete),
            web.get("/api/memory/docs", self.handle_api_memory_docs),
            web.get("/api/memory/docs/{item_id}", self.handle_api_memory_doc_detail),
            web.post("/api/memory/docs", self.handle_api_memory_doc_create),
            web.put("/api/memory/docs/{item_id}", self.handle_api_memory_doc_update),
            web.delete("/api/memory/docs/{item_id}", self.handle_api_memory_doc_delete),
            web.get("/api/memory/templates", self.handle_api_memory_templates),
            web.post("/api/memory/templates", self.handle_api_memory_template_create),
            web.put("/api/memory/templates/{item_id}", self.handle_api_memory_template_update),
            web.delete("/api/memory/templates/{item_id}", self.handle_api_memory_template_delete),
            web.post("/api/memory/reorder", self.handle_api_memory_reorder),
            web.post("/api/memory/preview", self.handle_api_memory_preview),
            web.get("/api/memory/render-logs", self.handle_api_memory_render_logs),
            web.get("/api/memory/render-logs/{log_id:\\d+}", self.handle_api_memory_render_log_detail),
            web.get("/api/conversations/{conversation_uuid}/agents/{task_uuid}/plan", self.handle_api_rath_task_plan),
            web.get("/api/conversations/{conversation_uuid}/agents/{task_uuid}/events", self.handle_api_rath_task_events),
            web.get("/api/rath/options", self.handle_api_rath_options),
            web.get("/api/rath/agents", self.handle_api_rath_agents),
            web.post("/api/rath/agents", self.handle_api_rath_agent_create),
            web.put("/api/rath/agents/{agent_id:\\d+}", self.handle_api_rath_agent_update),
            web.post("/api/rath/agents/{agent_id:\\d+}/trial", self.handle_api_rath_agent_trial),
            web.delete("/api/rath/agents/{agent_id:\\d+}", self.handle_api_rath_agent_delete),
        ])
        return app

    async def _json_body(self, request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

__all__ = [name for name in globals() if not name.startswith("__")]
