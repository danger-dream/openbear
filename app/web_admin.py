"""OpenBear 内置 Web 管理服务。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiohttp import web

from app.config import Config
from app.control_actions import schedule_openbear_restart
from app.db.engine import DB
from app.operation_locks import ChatOperationLocks
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.web_console.artifacts_api import WebAdminArtifactsMixin
from app.web_console.auth_api import WebAdminAuthMixin
from app.web_console.chat_api import WebAdminChatHandlersMixin
from app.web_console.chat_runtime import WebAdminChatRunMixin
from app.web_console.chat_state import WebAdminChatStateMixin
from app.web_console.config_api import WebAdminSettingsChannelsMixin
from app.web_console.conversations import WebAdminConversationsMixin
from app.web_console.core import (
    _COOKIE,
    _LOGIN_FAIL_LIMIT,
    _LOGIN_NONCE_COOKIE,
    _STATE_WEB_SECRET,
    _WEB_FRONTEND_EVENT_LOG_DIR,
    _WEB_SESSION_KEY,
    _WEB_WS_AUDIT_LOG_DIR,
    WebSession,
    _ChannelTestJob,
    _human_bytes,
    _json,
    _log_web_frontend_event,
    _log_web_ws_audit,
    _MCPServerNotFoundError,
    _origin_key,
    _parse_ts,
    _safe_upload_name,
    _sha256,
    _usage_cost_usd,
    _usage_json,
    _usage_sum,
    _web_media_kind,
    _web_operation_event_key,
    _web_operation_event_uuid,
    log,
)
from app.web_console.live_stream import (
    _WebContextCompactionGate,
    _WebDBPersister,
    _WebEmergencyCompactor,
    _WebLiveStream,
    _WebStreamRenderer,
)
from app.web_console.memory_api import WebAdminMemoryMixin
from app.web_console.operation_store import WebAdminOperationsMixin
from app.web_console.rath_api import WebAdminRathMixin
from app.web_console.routing import WebAdminAppMixin
from app.web_console.system_mcp_api import WebAdminSystemMcpMixin
from app.web_console.task_memory_api import WebAdminTaskMemoryMixin
from app.web_console.update_api import WebAdminUpdateMixin
from app.web_console.uploads import WebAdminUploadsMixin
from app.web_task_telegram import WebTaskTelegramNotifier


class WebAdminServer(
    WebAdminAppMixin,
    WebAdminAuthMixin,
    WebAdminArtifactsMixin,
    WebAdminConversationsMixin,
    WebAdminOperationsMixin,
    WebAdminRathMixin,
    WebAdminSystemMcpMixin,
    WebAdminUpdateMixin,
    WebAdminSettingsChannelsMixin,
    WebAdminUploadsMixin,
    WebAdminChatStateMixin,
    WebAdminChatRunMixin,
    WebAdminChatHandlersMixin,
    WebAdminTaskMemoryMixin,
    WebAdminMemoryMixin,
):
    def __init__(
        self,
        config: Config,
        db: DB,
        bot: Bot,
        *,
        operation_locks: ChatOperationLocks | None = None,
        control_actions: Any = None,
        runs: Any = None,
        llm_factory: Any = None,
        model_selection: Any = None,
        rath: RathTaskManager | None = None,
        tools: Any = None,
        messages: Any = None,
        config_store: Any = None,
        models_dev_catalog: Any = None,
        apply_config_hook: Callable[[Config], None] | None = None,
        mcp_reload_hook: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.bot = bot
        self.operation_locks = operation_locks or ChatOperationLocks()
        self.control_actions = control_actions
        self.runs = runs
        self.llm_factory = llm_factory
        self.model_selection = model_selection
        self.rath = rath or RathTaskManager(RathDAO(db))
        self.rath_dao = self.rath.dao
        self.mcp = None
        self.tools = tools
        self.messages = messages
        self.config_store = config_store
        # Kept as a separate cache service rather than a config field: external
        # catalog data must never be serialized with API keys or routing settings.
        self.models_dev_catalog = models_dev_catalog
        self._apply_config_hook = apply_config_hook
        self._mcp_reload_hook = mcp_reload_hook
        self.skills_prompt = ""
        self.skills_count = 0
        self.workspace_dir = str((Path.cwd() / "workspace").resolve())
        self._memory_operation_lock = asyncio.Lock()
        self._web_conversation_create_lock = asyncio.Lock()
        self.started_at = time.time()
        self._web_operation_locks: dict[str, asyncio.Lock] = {}
        self._web_live_streams: dict[str, _WebLiveStream] = {}
        # Foreground turn startup spans several awaited steps: persist accepted,
        # persist the user operation, create the controller task, then register
        # it in RunRegistry. Reconciliation must not classify that short window
        # as a stale inactive run merely because process-local facts are not all
        # visible yet. Values are turn UUID sets so concurrent starters cannot
        # clear each other's guard.
        self._web_starting_turns: dict[str, set[str]] = {}
        self._web_task_notification_locks: dict[int, asyncio.Lock] = {}
        self._web_task_notification_pending: dict[str, list[dict[str, Any]]] = {}
        self._web_task_notification_deferred: dict[str, list[dict[str, Any]]] = {}
        self._web_task_notification_workers: set[str] = set()
        self._web_frame_prune_task: asyncio.Task[Any] | None = None
        self._web_notification_recovery_task: asyncio.Task[Any] | None = None
        # One event/queue per Web conversation keeps the original controller run
        # asleep (not exited) while its detached Agents work. User interruptions
        # and Agent notifications wake that same root turn immediately.
        self._web_controller_wake_events: dict[str, asyncio.Event] = {}
        self._web_controller_notifications: dict[str, list[dict[str, Any]]] = {}
        self._web_stop_markers: dict[str, int] = {}
        self._web_stopped_task_uuids: dict[str, set[str]] = {}
        self._web_confirmations: dict[str, dict[str, Any]] = {}
        self._web_confirm_by_conversation: dict[str, set[str]] = {}
        self._channel_test_jobs: dict[str, _ChannelTestJob] = {}
        self.web_task_telegram = WebTaskTelegramNotifier(config, db, bot)
        self.update_service = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        if not self.config.web.enabled:
            return
        await self.ensure_secret_key()
        app = self.make_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.config.web.host, self.config.web.port)
        await self.web_task_telegram.start()
        try:
            await self._site.start()
        except Exception:
            await self.web_task_telegram.stop()
            raise
        await self._recover_web_task_notifications(reset_processing=True)

        async def _notification_recovery_loop() -> None:
            while True:
                await asyncio.sleep(60)
                try:
                    await self._recover_web_task_notifications()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("恢复 Web task notification outbox 失败")

        self._web_notification_recovery_task = asyncio.create_task(
            _notification_recovery_loop(),
            name="web-task-notification-recovery",
        )

        async def _frame_retention_loop() -> None:
            while True:
                try:
                    deleted = await self._prune_web_event_frames()
                    notification_deleted = await self._prune_web_task_notification_history()
                    tg_runs_deleted, tg_deliveries_deleted = await self.web_task_telegram.prune()
                    if deleted or notification_deleted or tg_runs_deleted or tg_deliveries_deleted:
                        log.info(
                            "已清理过期 Web 运行历史",
                            event_frames=deleted,
                            task_notifications=notification_deleted,
                            telegram_notification_runs=tg_runs_deleted,
                            telegram_notification_deliveries=tg_deliveries_deleted,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("清理 Web Event Frames 失败")
                await asyncio.sleep(86_400)

        self._web_frame_prune_task = asyncio.create_task(
            _frame_retention_loop(),
            name="web-event-frame-retention",
        )
        log.info("Web 管理服务已启动", host=self.config.web.host, port=self.config.web.port)

    async def stop(self) -> None:
        if self._web_notification_recovery_task is not None:
            self._web_notification_recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._web_notification_recovery_task
            self._web_notification_recovery_task = None
        if self._web_frame_prune_task is not None:
            self._web_frame_prune_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._web_frame_prune_task
            self._web_frame_prune_task = None
        # Stop accepting browser work first, then let every registered controller
        # unwind while SQLite and the live operation sink are still available.
        # Closing DB before these tasks receive CancelledError leaves active Web
        # run/status/reasoning operations durable across restart.
        if self._runner is not None:
            # cleanup() stops all sites and drains already-accepted HTTP/WS
            # handlers. Do this before snapshotting RunRegistry so an in-flight
            # composer POST cannot register a new controller after cancellation.
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        if self.runs is not None:
            await self.runs.cancel_all_and_wait()
        await self.web_task_telegram.stop()
        workers = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and str(task.get_name() or "").startswith("web-task-notification-")
        ]
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    def apply_config(self, config: Config, *, llm_factory: Any = None,
                     model_selection: Any = None, tools: Any = None) -> None:
        """同步运行期配置快照，避免 Web 管理端继续使用旧模型/旧记忆身份。"""
        old_web = (self.config.web.enabled, self.config.web.host, self.config.web.port)
        new_web = (config.web.enabled, config.web.host, config.web.port)
        self.config = config
        self.web_task_telegram.apply_config(config)
        self.llm_factory = llm_factory
        self.model_selection = model_selection
        if tools is not None:
            self.tools = tools
        if self._runner is not None and old_web != new_web:
            log.warning(
                "Web 监听配置已更新；host/port/enabled 需重启服务后完全生效",
                旧配置=old_web,
                新配置=new_web,
            )


__all__ = [
    "WebAdminServer",
    "WebSession",
    "_COOKIE",
    "_LOGIN_FAIL_LIMIT",
    "_LOGIN_NONCE_COOKIE",
    "_MCPServerNotFoundError",
    "_STATE_WEB_SECRET",
    "_WEB_FRONTEND_EVENT_LOG_DIR",
    "_WEB_SESSION_KEY",
    "_WEB_WS_AUDIT_LOG_DIR",
    "_WebContextCompactionGate",
    "_WebDBPersister",
    "_WebEmergencyCompactor",
    "_WebLiveStream",
    "_WebStreamRenderer",
    "_human_bytes",
    "_json",
    "_log_web_frontend_event",
    "_log_web_ws_audit",
    "_origin_key",
    "_parse_ts",
    "_safe_upload_name",
    "_sha256",
    "_usage_cost_usd",
    "_usage_json",
    "_usage_sum",
    "_web_media_kind",
    "_web_operation_event_key",
    "_web_operation_event_uuid",
    "log",
    "schedule_openbear_restart",
]
