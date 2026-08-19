"""服务容器 —— 组件接线（单例）。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from aiogram import Bot

from app.admin.skills_web import SkillsWebAdminServer
from app.agent.runs import RunRegistry
from app.config import Config, config_path, load_config
from app.config_store import ConfigStore
from app.context.builder import ContextBuilder
from app.control_actions import ControlActionQueue
from app.db.dao import MessageDAO, SummaryDAO
from app.db.engine import DB
from app.llm.client import HTTPClient
from app.llm.factory import BackendFactory
from app.logging import get_logger
from app.mcp.manager import MCPManager
from app.memory.builtin import BuiltinMemoryClient
from app.memory.client import MemoryClient
from app.models.selection import ModelSelection
from app.models_dev import ModelsDevCatalog, catalog_cache_dir
from app.operation_locks import ChatOperationLocks
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.task_memory import TaskMemoryDAO
from app.tools.agents import register_agent_tools
from app.tools.base import ToolRegistry
from app.tools.bash import register_bash_tool
from app.tools.file_state import FileStateStore
from app.tools.files import register_file_tools
from app.tools.history import register_history_tools
from app.tools.mcp import register_mcp_tools
from app.tools.memory import register_memory_tools
from app.tools.openbear_control import register_openbear_control_tool
from app.tools.skills import filter_skills, load_skills, render_skills_block
from app.tools.task_memory import register_task_memory_tool
from app.tools.user_interaction import UserInteractionManager, register_user_interaction_tools
from app.tools.web_search import register_web_search_tools
from app.update.service import UpdateService

log = get_logger("services")


class Services:
    def __init__(self, config: Config, bot: Bot) -> None:
        self.config = config
        self.config_store = ConfigStore(config_path())
        # Public model metadata is a separate, recoverable cache.  Its contents
        # are never treated as an authoritative channel configuration.
        self.models_dev_catalog = ModelsDevCatalog(catalog_cache_dir(config_path()))
        self.bot = bot
        self.started_at = time.time()
        self.control_actions = ControlActionQueue()

        self.http = HTTPClient(
            connect_timeout_s=config.agent.llm_connect_timeout_s,
            first_byte_timeout_s=config.agent.llm_first_byte_timeout_s,
            idle_timeout_s=config.agent.llm_idle_timeout_s,
            total_timeout_s=config.agent.llm_total_timeout_s,
        )
        self.db = DB(config.storage.db_path)
        self.messages = MessageDAO(self.db)
        self.summaries = SummaryDAO(self.db)
        self.operation_locks = ChatOperationLocks()

        self.mem = self._make_memory_client(config)
        self.factory = BackendFactory(config.models, self.http)
        self.selection = ModelSelection(config.models, config_path())
        self.runs = RunRegistry()
        self.rath_dao = RathDAO(self.db)
        self.task_memories = TaskMemoryDAO(self.db)
        self.rath = RathTaskManager(self.rath_dao, max_concurrent_tasks=config.rath.max_concurrent_tasks)
        # workspace: 默认为运行目录下的 workspace/，不存在则自动创建
        self.workspace_dir = str(Path(os.getcwd(), "workspace").resolve())
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        self.interactions = UserInteractionManager(bot)
        self.mcp = MCPManager(
            config,
            interactions=self.interactions,
            db=self.db,
            approval_updater=self._update_mcp_server_approval,
            tools_changed_callback=self._on_mcp_tools_changed,
        )
        self._mcp_reload_lock = asyncio.Lock()
        self._mcp_reload_generation = 0
        self._mcp_reload_task: asyncio.Task[Any] | None = None
        self.web_admin = SkillsWebAdminServer(
            config,
            self.db,
            bot,
            operation_locks=self.operation_locks,
            control_actions=self.control_actions,
            runs=self.runs,
            llm_factory=self.factory,
            model_selection=self.selection,
            rath=self.rath,
            tools=None,
            messages=self.messages,
            config_store=self.config_store,
            models_dev_catalog=self.models_dev_catalog,
            apply_config_hook=self.apply_config,
            mcp_reload_hook=self.reload_mcp_from_disk,
        )
        self.web_admin.mcp = self.mcp
        self.update = UpdateService(self)
        self.web_admin.update_service = self.update

        # 工具注册
        self.file_state = FileStateStore(config.tools.file_state_max_entries)
        self.tools = ToolRegistry()
        register_file_tools(
            self.tools,
            store=self.file_state,
            default_limit_lines=config.tools.file_read_limit_lines,
            output_limit_bytes=config.tools.file_read_output_bytes,
            max_line_bytes=config.tools.file_read_max_line_bytes,
            diff_max_chars=config.tools.file_diff_max_chars,
        )
        register_web_search_tools(self.tools, skills_dir=config.tools.skills_dir)
        register_bash_tool(self.tools,
                           default_timeout_s=config.tools.bash_timeout_s,
                           output_limit=config.tools.bash_output_limit,
                           max_timeout_s=config.tools.bash_max_timeout_s,
                           spool_max_bytes=config.tools.bash_spool_max_bytes,
                           auto_background_after_s=config.tools.bash_auto_background_after_s)
        register_memory_tools(self.tools, self.mem)
        register_task_memory_tool(self.tools, self.task_memories)
        register_history_tools(self.tools, self.db)
        register_user_interaction_tools(self.tools, self.interactions)
        register_openbear_control_tool(self.tools, self)
        register_agent_tools(
            self.tools,
            config=config,
            dao=self.rath_dao,
            manager=self.rath,
            llm_factory=self.factory,
            model_selection=self.selection,
            messages=self.messages,
            workspace_dir=self.workspace_dir,
        )
        self.web_admin.tools = self.tools

        # skills: 加载 → 过滤 → 只把通过的交给 context
        all_skills = load_skills(config.tools.skills_dir)
        result = filter_skills(all_skills, disabled_names=set(config.tools.disabled_skills or []))
        self.skills = result.included

        self.web_admin.skills_prompt = render_skills_block(self.skills)
        self.web_admin.skills_count = len(self.skills)
        self.web_admin.workspace_dir = self.workspace_dir

        self.context = ContextBuilder(
            self.mem, self.messages, self.summaries, self.skills,
            self.tools, self.workspace_dir, self.rath_dao, self.mcp,
        )

        # 累计用量（本进程内，/status 用）
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_write_tokens = 0
        self.total_cost_usd = 0.0

    def _make_memory_client(self, config: Config):
        if config.memory.provider == "builtin":
            return BuiltinMemoryClient(self.db, identity=config.memory.identity)
        return MemoryClient(
            config.memory.base_url,
            config.memory.identity,
            config.memory.access_key,
            timeout_s=config.memory.timeout_s,
        )

    async def startup(self) -> None:
        await self.db.connect()
        if isinstance(self.mem, BuiltinMemoryClient):
            await self.mem._bootstrap()
        # Starts an immediate background conditional GET.  Startup and model
        # calls keep using the last-known-good local snapshot meanwhile.
        await self.models_dev_catalog.start()
        await self._mark_interrupted_operations()
        # Rath/controller coroutines are process-local even when Rath is disabled
        # in the new config.  Always close durable leftovers from the old process.
        interrupted = await self.rath_dao.mark_interrupted_running()
        if interrupted:
            log.warning("启动时标记未完成 Rath 任务为 interrupted", 数量=interrupted)
        reconciled = await self._mark_interrupted_web_agent_operations()
        if reconciled:
            log.warning("启动时同步终结未完成 Web Agent 操作", 数量=reconciled)
        reconciled_runtime = await self._mark_interrupted_web_runtime_operations()
        if reconciled_runtime:
            log.warning("启动时同步终结未完成 Web 运行操作", 数量=reconciled_runtime)
        if self.config.rath.enabled:
            await ensure_builtin_workflows(self.rath_dao)
        if self.config.mcp.enabled:
            try:
                await self.mcp.start()
            except Exception:
                await self.mcp.close()
                raise
            self._rebuild_tools_and_context(include_mcp=True)
            registered_mcp_tools = len(self.mcp.available_tools())
            log.info("MCP 工具已注册", 数量=registered_mcp_tools)
        await self.web_admin.start()
        await self.update.start()
        log.info("服务已启动", 工具数=len(self.tools.names()), skills=len(self.skills),
                 主力模型=self.selection.current)

    async def _mark_interrupted_operations(self) -> int:
        ts = int(time.time())
        cur = await self.db.conn.execute(
            """
            UPDATE operations
            SET status='interrupted', finished_at=?, error=CASE
              WHEN COALESCE(error, '') = '' THEN 'interrupted by OpenBear startup'
              ELSE error
            END
            WHERE status='running'
            """,
            (ts,),
        )
        await self.db.conn.commit()
        count = int(cur.rowcount or 0)
        if count:
            log.warning("启动时标记未完成操作为 interrupted", 数量=count)
        return count

    @staticmethod
    def _web_agent_task_uuids(payload: dict) -> set[str]:
        out: set[str] = set()
        direct = str(payload.get("taskUuid") or "").strip()
        if direct:
            out.add(direct)
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        task_uuid = str(task.get("taskUuid") or task.get("task_uuid") or "").strip()
        if task_uuid:
            out.add(task_uuid)
        for item in payload.get("results") if isinstance(payload.get("results"), list) else []:
            if not isinstance(item, dict):
                continue
            row_uuid = str(item.get("taskUuid") or item.get("task_uuid") or "").strip()
            row_task = item.get("task") if isinstance(item.get("task"), dict) else {}
            row_task_uuid = str(row_task.get("taskUuid") or row_task.get("task_uuid") or "").strip()
            if row_uuid:
                out.add(row_uuid)
            if row_task_uuid:
                out.add(row_task_uuid)
        return out

    @staticmethod
    def _terminal_agent_status(statuses: list[str]) -> str:
        clean = [str(s or "").strip() for s in statuses if str(s or "").strip()]
        if not clean:
            return ""
        if all(s == "completed" for s in clean):
            return "completed"
        if all(s in {"completed", "failed", "cancelled", "interrupted", "partial"} for s in clean):
            if any(s == "interrupted" for s in clean):
                return "interrupted"
            if any(s in {"failed", "cancelled", "partial"} for s in clean):
                return "partial"
            return "completed"
        return ""

    @staticmethod
    def _usage_dict(input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> dict[str, int]:
        return {
            "inputTokens": max(0, int(input_tokens or 0)),
            "outputTokens": max(0, int(output_tokens or 0)),
            "cacheReadTokens": max(0, int(cache_read_tokens or 0)),
            "cacheWriteTokens": max(0, int(cache_write_tokens or 0)),
            "totalTokens": max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0)) + max(0, int(cache_read_tokens or 0)) + max(0, int(cache_write_tokens or 0)),
        }

    @staticmethod
    def _add_usage_dict(payload: dict, key: str, usage: dict[str, int]) -> None:
        current = payload.get(key) if isinstance(payload.get(key), dict) else {}
        payload[key] = {
            "inputTokens": int(current.get("inputTokens") or 0) + int(usage.get("inputTokens") or 0),
            "outputTokens": int(current.get("outputTokens") or 0) + int(usage.get("outputTokens") or 0),
            "cacheReadTokens": int(current.get("cacheReadTokens") or 0) + int(usage.get("cacheReadTokens") or 0),
            "cacheWriteTokens": int(current.get("cacheWriteTokens") or 0) + int(usage.get("cacheWriteTokens") or 0),
            "totalTokens": int(current.get("totalTokens") or 0) + int(usage.get("totalTokens") or 0),
        }

    @staticmethod
    def _apply_terminal_task_to_agent_payload(payload: dict, task_rows: dict[str, dict], status: str) -> dict:
        payload = dict(payload or {})
        payload["status"] = status
        payload.setdefault("error", "")

        def apply_task(task: dict) -> dict:
            task = dict(task or {})
            task_uuid = str(task.get("taskUuid") or task.get("task_uuid") or "").strip()
            row = task_rows.get(task_uuid) if task_uuid else None
            if row:
                row_status = str(row.get("status") or status)
                row_error = str(row.get("error") or "")
                task["status"] = row_status
                task["currentStatus"] = row_error if row_status in {"failed", "cancelled", "interrupted", "partial"} and row_error else (row.get("current_status") or "已中断")
                if row_error:
                    task["error"] = row_error
            elif status:
                task["status"] = status
            return task

        if isinstance(payload.get("task"), dict):
            payload["task"] = apply_task(payload["task"])
            if payload["task"].get("error") and not payload.get("error"):
                payload["error"] = payload["task"].get("error")
        if isinstance(payload.get("results"), list):
            results = []
            for item in payload["results"]:
                if not isinstance(item, dict):
                    results.append(item)
                    continue
                next_item = dict(item)
                row_uuid = str(next_item.get("taskUuid") or next_item.get("task_uuid") or "").strip()
                row_task = next_item.get("task") if isinstance(next_item.get("task"), dict) else {}
                if row_task:
                    next_item["task"] = apply_task(row_task)
                    next_item["status"] = next_item["task"].get("status") or status
                    next_item["currentStatus"] = next_item["task"].get("currentStatus") or ""
                    if next_item["task"].get("error"):
                        next_item["error"] = next_item["task"].get("error")
                elif row_uuid and row_uuid in task_rows:
                    row = task_rows[row_uuid]
                    row_status = str(row.get("status") or status)
                    row_error = str(row.get("error") or "")
                    next_item["status"] = row_status
                    next_item["currentStatus"] = row_error if row_status in {"failed", "cancelled", "interrupted", "partial"} and row_error else (row.get("current_status") or "已中断")
                    if row_error:
                        next_item["error"] = row_error
                elif status:
                    next_item["status"] = status
                results.append(next_item)
            payload["results"] = results
        if payload.get("resultText"):
            with contextlib.suppress(Exception):
                result_data = json.loads(str(payload.get("resultText") or "{}"))
                if isinstance(result_data, dict):
                    result_data["status"] = status
                    result_data["ok"] = status == "completed"
                    if payload.get("error"):
                        result_data["error"] = payload.get("error")
                    if isinstance(payload.get("task"), dict):
                        result_data["task"] = payload["task"]
                    if isinstance(payload.get("results"), list):
                        result_data["results"] = payload["results"]
                    payload["resultText"] = json.dumps(result_data, ensure_ascii=False, indent=2, default=str)
        return payload

    async def _merge_terminal_agent_usage_into_web_stats(
        self,
        *,
        conversation_uuid: str,
        turn_uuid: str,
        task_rows: dict[str, dict],
    ) -> int:
        if not conversation_uuid or not turn_uuid or not task_rows:
            return 0
        cur = await self.db.conn.execute(
            """
            SELECT id, internal_chat_id, op_id, payload_json
            FROM web_operations
            WHERE conversation_uuid=? AND turn_uuid=? AND op_type='stats'
            ORDER BY display_seq DESC, id DESC
            LIMIT 1
            """,
            (conversation_uuid, turn_uuid),
        )
        row = await cur.fetchone()
        if row is None:
            return 0
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        accounted = {str(x) for x in payload.get("expertTaskUuids", []) if str(x)} if isinstance(payload.get("expertTaskUuids"), list) else set()
        added = 0
        for task_uuid, task in task_rows.items():
            task_uuid = str(task_uuid or "")
            status = str(task.get("status") or "")
            if not task_uuid or task_uuid in accounted or status not in {"completed", "failed", "cancelled", "interrupted", "partial", "needs_openbear_control"}:
                continue
            model_calls = max(0, int(task.get("model_call_count") or 0))
            tool_calls = max(0, int(task.get("tool_call_count") or 0))
            usage = self._usage_dict(
                input_tokens=int(task.get("input_tokens") or 0),
                output_tokens=int(task.get("output_tokens") or 0),
                cache_read_tokens=int(task.get("cache_read_tokens") or 0),
                cache_write_tokens=int(task.get("cache_write_tokens") or 0),
            )
            payload["modelCalls"] = int(payload.get("modelCalls") or 0) + model_calls
            payload["modelOk"] = int(payload.get("modelOk") or 0) + model_calls
            payload["toolCalls"] = int(payload.get("toolCalls") or 0) + tool_calls
            payload["expertModelCalls"] = int(payload.get("expertModelCalls") or 0) + model_calls
            payload["expertToolCalls"] = int(payload.get("expertToolCalls") or 0) + tool_calls
            payload["expertTasks"] = int(payload.get("expertTasks") or 0) + 1
            payload["costUsd"] = float(payload.get("costUsd") or 0.0) + float(task.get("cost_usd") or 0.0)
            self._add_usage_dict(payload, "expertUsage", usage)
            accounted.add(task_uuid)
            added += 1
        if not added:
            return 0
        payload["expertTaskUuids"] = sorted(accounted)
        await self.web_admin._publish_operation(
            conversation_uuid,
            internal_chat_id=int(row["internal_chat_id"] or 0),
            op_id=str(row["op_id"] or f"stats:{turn_uuid}"),
            op_type="stats",
            action="snapshot",
            turn_uuid=turn_uuid,
            payload=payload,
            source="startup_reconciliation",
            debug={"source": "merge_terminal_agent_usage_into_web_stats", "taskUuids": sorted(task_rows)},
        )
        return added

    async def _mark_interrupted_web_agent_operations(self) -> int:
        """Reconcile Web Agent cards with Rath tasks interrupted during startup.

        `rath_tasks` are marked interrupted on startup because in-process Agent
        execution cannot survive a service restart.  Without this reconciliation,
        the durable Web operation can remain `running/active` forever, making the
        browser show a zombie Agent even though Rath already terminated it.
        """
        cur = await self.db.conn.execute(
            """
            SELECT id, conversation_uuid, internal_chat_id, op_id, turn_uuid, status, lifecycle, payload_json
            FROM web_operations
            WHERE op_type='agent'
              AND (
                lifecycle='active'
                OR status IN ('queued','running','pausing','resuming','stopping')
                OR status IN ('failed','cancelled','interrupted')
              )
            """
        )
        rows = await cur.fetchall()
        changed = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            task_uuids = self._web_agent_task_uuids(payload)
            if not task_uuids:
                continue
            placeholders = ",".join("?" for _ in task_uuids)
            task_cur = await self.db.conn.execute(
                f"""
                SELECT task_uuid, status, current_status, error,
                       model_call_count, tool_call_count,
                       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd
                FROM rath_tasks
                WHERE task_uuid IN ({placeholders})
                """,
                tuple(task_uuids),
            )
            task_rows = {str(task["task_uuid"]): dict(task) for task in await task_cur.fetchall()}
            if not task_rows:
                continue
            status = self._terminal_agent_status([str(task.get("status") or "") for task in task_rows.values()])
            if not status:
                continue
            next_payload = self._apply_terminal_task_to_agent_payload(payload, task_rows, status)
            next_payload_json = json.dumps(next_payload, ensure_ascii=False, separators=(",", ":"), default=str)
            row_status = str(row["status"] or "")
            row_lifecycle = str(row["lifecycle"] or "")
            row_is_terminal = row_lifecycle == "terminal" and row_status in {
                "completed", "failed", "cancelled", "interrupted", "partial",
            }
            if not row_is_terminal and (
                row_status != status
                or row_lifecycle != "terminal"
                or next_payload_json != str(row["payload_json"] or "")
            ):
                await self.web_admin._publish_operation(
                    str(row["conversation_uuid"] or ""),
                    internal_chat_id=int(row["internal_chat_id"] or 0),
                    op_id=str(row["op_id"] or ""),
                    op_type="agent",
                    action="end",
                    turn_uuid=str(row["turn_uuid"] or ""),
                    payload=next_payload,
                    status=status,
                    lifecycle="terminal",
                    source="startup_reconciliation",
                    debug={"source": "mark_interrupted_web_agent_operations", "taskUuids": sorted(task_uuids)},
                )
                changed += 1
            stats_added = await self._merge_terminal_agent_usage_into_web_stats(
                conversation_uuid=str(row["conversation_uuid"] or ""),
                turn_uuid=str(row["turn_uuid"] or ""),
                task_rows=task_rows,
            )
            changed += stats_added
        if changed:
            await self.db.conn.commit()
        return changed

    async def _mark_interrupted_web_runtime_operations(self) -> int:
        """Close non-Agent Web execution records that cannot survive restart.

        Agent cards are reconciled separately from Rath task rows.  This sweep is
        intentionally limited to controller-local lifecycle records; notices and
        already-terminal history remain untouched, while active runs, tools,
        Agent controls, statuses, supervision, and assistant drafts become a
        truthful interrupted terminal snapshot.
        """
        cur = await self.db.conn.execute(
            """
            SELECT * FROM web_operations
            WHERE op_type IN ('run','status','tool','user_interaction','agent_control','assistant_message','reasoning','agent_supervision','context_compaction')
              AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')
            ORDER BY conversation_uuid, display_seq, id
            """
        )
        rows = await cur.fetchall()
        if not rows:
            # A prior crash may have persisted only the coarse conversation flag.
            cur = await self.db.conn.execute(
                "UPDATE web_conversations SET status='idle', current_status='已中断（服务重启）' WHERE status IN ('running','stopping')"
            )
            count = int(cur.rowcount or 0)
            if count:
                await self.db.conn.commit()
            return count

        now_ms_value = int(time.time() * 1000)
        changed = 0
        for row in rows:
            conv_uuid = str(row["conversation_uuid"] or "")
            op_type = str(row["op_type"] or "")
            try:
                old_payload = json.loads(str(row["payload_json"] or "{}"))
            except Exception:
                old_payload = {}
            patch: dict[str, Any] = {"status": "interrupted", "interruptedBy": "service_restart"}
            operation_status = "interrupted"
            if op_type == "user_interaction":
                patch.update({"status": "cancelled", "interactionStatus": "cancelled"})
                operation_status = "cancelled"
            elif op_type == "run":
                patch.update({
                    "runId": str(row["run_id"] or row["target_id"] or old_payload.get("runId") or row["turn_uuid"] or ""),
                    "completedAtMs": now_ms_value,
                })
            elif op_type in {"assistant_message", "reasoning"}:
                patch["complete"] = True
            elif op_type in {"status", "agent_supervision"}:
                patch.update({"active": False, "statusText": "已中断（服务重启）"})
            await self.web_admin._publish_operation(
                conv_uuid,
                internal_chat_id=int(row["internal_chat_id"] or 0),
                op_id=str(row["op_id"] or ""),
                op_type=op_type,
                action="cancel",
                turn_uuid=str(row["turn_uuid"] or ""),
                run_root_turn_uuid=str(row["run_root_turn_uuid"] or ""),
                payload=patch,
                status=operation_status,
                lifecycle="terminal",
                source="startup_reconciliation",
                debug={"source": "mark_interrupted_web_runtime_operations"},
            )
            changed += 1

        # There is no live controller in a fresh process.  Coarse running flags
        # must therefore be reset even for conversations that lost all operation
        # frames before the crash.
        cur = await self.db.conn.execute(
            "UPDATE web_conversations SET status='idle', current_status='已中断（服务重启）' WHERE status IN ('running','stopping')"
        )
        changed += max(0, int(cur.rowcount or 0))
        await self.db.conn.commit()
        return changed

    async def _on_mcp_tools_changed(self) -> None:
        """Publish a refreshed MCP tool list to subsequent Agent turns."""
        self._rebuild_tools_and_context(include_mcp=True, preserve_file_state=True)
        log.info("MCP 动态工具列表已应用", 工具数=len(self.mcp.available_tools()))

    @staticmethod
    def _mcp_config_fingerprint(config: Config) -> str:
        return json.dumps(config.mcp.model_dump(by_alias=True, mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _rebuild_tools_and_context(
        self,
        *,
        include_mcp: bool = True,
        preserve_file_state: bool = False,
    ) -> None:
        """Rebuild next-turn tools/skills/context from the current runtime config.

        Already-running turns keep their old ToolRegistry reference.  New turns see
        this rebuilt registry.  MCP tools are optional here because MCP hot reload is
        asynchronous: while a new MCP config is still starting, we can either keep
        currently-live MCP tools or deliberately hide them for disable/failure paths.
        """
        config = self.config
        if not preserve_file_state:
            self.file_state = FileStateStore(config.tools.file_state_max_entries)
        self.tools = ToolRegistry()
        register_file_tools(
            self.tools,
            store=self.file_state,
            default_limit_lines=config.tools.file_read_limit_lines,
            output_limit_bytes=config.tools.file_read_output_bytes,
            max_line_bytes=config.tools.file_read_max_line_bytes,
            diff_max_chars=config.tools.file_diff_max_chars,
        )
        register_web_search_tools(self.tools, skills_dir=config.tools.skills_dir)
        register_bash_tool(self.tools,
                           default_timeout_s=config.tools.bash_timeout_s,
                           output_limit=config.tools.bash_output_limit,
                           max_timeout_s=config.tools.bash_max_timeout_s,
                           spool_max_bytes=config.tools.bash_spool_max_bytes,
                           auto_background_after_s=config.tools.bash_auto_background_after_s)
        register_memory_tools(self.tools, self.mem)
        register_task_memory_tool(self.tools, self.task_memories)
        register_history_tools(self.tools, self.db)
        register_user_interaction_tools(self.tools, self.interactions)
        register_openbear_control_tool(self.tools, self)
        register_agent_tools(
            self.tools,
            config=config,
            dao=self.rath_dao,
            manager=self.rath,
            llm_factory=self.factory,
            model_selection=self.selection,
            messages=self.messages,
            workspace_dir=self.workspace_dir,
        )
        if include_mcp:
            register_mcp_tools(self.tools, self.mcp)
        self.web_admin.tools = self.tools
        all_skills = load_skills(config.tools.skills_dir)
        result = filter_skills(all_skills, disabled_names=set(config.tools.disabled_skills or []))
        self.skills = result.included
        self.web_admin.skills_prompt = render_skills_block(self.skills)
        self.web_admin.skills_count = len(self.skills)
        self.web_admin.workspace_dir = self.workspace_dir
        self.context = ContextBuilder(
            self.mem, self.messages, self.summaries, self.skills,
            self.tools, self.workspace_dir, self.rath_dao, self.mcp,
        )

    def reload_skills_from_disk(self) -> dict[str, Any]:
        """Reload Agent Skills from disk and rebuild next-turn tools/context.

        This is intentionally narrower than apply_config(): it keeps the current
        runtime Config and service objects unchanged, and only refreshes the
        derived ToolRegistry/skills prompt/context that new turns will use.
        """
        before_names = {skill.name for skill in self.skills}
        before_count = len(before_names)
        self._rebuild_tools_and_context(include_mcp=True)
        after_names = {skill.name for skill in self.skills}
        added = sorted(after_names - before_names)
        removed = sorted(before_names - after_names)
        result = {
            "ok": True,
            "status": "ok",
            "action": "skills_reload",
            "beforeCount": before_count,
            "afterCount": len(after_names),
            "added": added,
            "removed": removed,
            "skillsDir": self.config.tools.skills_dir,
            "effective": "next_turn",
            "message": "skills_reloaded",
        }
        log.info("Skills 磁盘配置热重载完成", 变更=len(added) + len(removed), skills=len(after_names))
        return result

    def _mcp_status_summary(self, *, message: str, changed: bool, reloaded: bool) -> dict[str, Any]:
        snapshot = self.mcp.status_snapshot()
        all_tools = self.mcp.all_tools_snapshot()
        status_counts: dict[str, int] = {}
        for server in snapshot.servers:
            status = str(server.status or "unknown")
            status_counts[status] = int(status_counts.get(status, 0)) + 1
        visible_tools = [tool for tool in all_tools if not tool.filtered]
        filtered_tools = [tool for tool in all_tools if tool.filtered]
        summary = {
            "enabled": bool(snapshot.enabled),
            "serverCount": len(snapshot.servers),
            "connectedCount": int(status_counts.get("connected", 0)),
            "failedCount": int(status_counts.get("failed", 0)),
            "disabledCount": int(status_counts.get("disabled", 0)),
            "pendingCount": int(status_counts.get("pending", 0)),
            "totalTools": len(all_tools),
            "visibleTools": len(visible_tools),
            "filteredTools": len(filtered_tools),
            "statusCounts": status_counts,
        }
        return {
            "ok": True,
            "enabled": bool(snapshot.enabled),
            "changed": bool(changed),
            "reloaded": bool(reloaded),
            "message": message,
            "summary": summary,
            "servers": len(snapshot.servers),
            "serverCount": len(snapshot.servers),
            "tools": len(visible_tools),
            "toolCount": len(visible_tools),
            "totalTools": len(all_tools),
            "sensitiveConfigHidden": True,
        }

    async def _update_mcp_server_approval(self, server: str, approval: str) -> dict[str, Any]:
        """Persist an approval chosen from an MCP call and hot-reload it safely."""
        server_key = str(server or "").strip()
        policy = str(approval or "").strip().lower()
        if not server_key or policy not in {"allow", "ask", "deny"}:
            raise ValueError("invalid_mcp_approval_update")

        def mutator(raw: dict[str, Any]) -> None:
            mcp = raw.get("mcp")
            servers = mcp.get("servers") if isinstance(mcp, dict) else None
            if not isinstance(servers, dict) or server_key not in servers:
                raise ValueError("mcp_server_not_found")
            server_config = servers.get(server_key)
            if not isinstance(server_config, dict):
                raise ValueError("mcp_server_config_invalid")
            server_config["approval"] = policy

        await self.config_store.mutate(mutator)
        result = await self.reload_mcp_from_disk()
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "mcp_reload_failed"))
        return result

    async def reload_mcp_from_disk(self) -> dict[str, Any]:
        """Synchronously reload only MCP runtime state from current openbear.json.

        This is intentionally narrower than apply_config(): it reads the full config
        file for validation, but only copies the MCP section into the live Config and
        hot-swaps MCP clients/tools.  Other runtime services keep their current
        in-memory objects so a manual MCP reload cannot change model, memory, DB,
        Web, or Rath state.
        """
        try:
            config = load_config(config_path())
        except Exception as exc:
            log.warning("MCP 磁盘配置读取失败，保留上一组已连接 MCP 工具", 错误类型=type(exc).__name__)
            return {
                **self._mcp_status_summary(message="config_load_failed_kept_previous", changed=False, reloaded=False),
                "ok": False,
                "error": "config_load_failed",
                "errorType": type(exc).__name__,
            }
        # Compare against the runtime Config snapshot so direct edits to
        # openbear.json are detected even when MCP failed or was disabled at startup.
        current_fingerprint = self._mcp_config_fingerprint(self.config)
        next_fingerprint = self._mcp_config_fingerprint(config)
        changed = current_fingerprint != next_fingerprint
        manager_changed = self._mcp_config_fingerprint(self.mcp.config) != next_fingerprint
        runtime_config = self.config.model_copy(update={"mcp": config.mcp}, deep=True)
        effective_changed = changed or manager_changed
        self._mcp_reload_generation += 1
        generation = self._mcp_reload_generation
        async with self._mcp_reload_lock:
            if generation != self._mcp_reload_generation:
                return self._mcp_status_summary(message="superseded", changed=effective_changed, reloaded=False)
            try:
                commit, abort = await self.mcp.prepare_reload(runtime_config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("MCP 磁盘配置热重载失败，保留上一组已连接 MCP 工具", 错误类型=type(exc).__name__)
                self._rebuild_tools_and_context(include_mcp=bool(self.mcp.available_tools()))
                return {
                    **self._mcp_status_summary(message="reload_failed_kept_previous", changed=effective_changed, reloaded=False),
                    "ok": False,
                    "error": "mcp_reload_failed",
                    "errorType": type(exc).__name__,
                }
            if generation != self._mcp_reload_generation:
                await abort()
                return self._mcp_status_summary(message="superseded", changed=effective_changed, reloaded=False)
            try:
                await commit()
                self.config = runtime_config
                self.web_admin.apply_config(
                    runtime_config,
                    llm_factory=self.factory,
                    model_selection=self.selection,
                )
            except asyncio.CancelledError:
                await abort()
                raise
            except Exception as exc:
                log.warning("MCP 磁盘配置热重载提交失败", 错误类型=type(exc).__name__)
                self._rebuild_tools_and_context(include_mcp=bool(self.mcp.available_tools()))
                return {
                    **self._mcp_status_summary(message="reload_commit_failed", changed=effective_changed, reloaded=False),
                    "ok": False,
                    "error": "mcp_reload_commit_failed",
                    "errorType": type(exc).__name__,
                }
            self.web_admin.mcp = self.mcp
            self._rebuild_tools_and_context(include_mcp=True)
            message = "reloaded" if changed else "reloaded_no_change"
            if manager_changed and not changed:
                message = "reloaded_runtime_recovered"
            log.info("MCP 磁盘配置热重载完成", 启用=runtime_config.mcp.enabled, 工具数=len(self.mcp.available_tools()), 配置变更=effective_changed)
            return self._mcp_status_summary(message=message, changed=effective_changed, reloaded=True)

    def _schedule_mcp_hot_reload(self, config: Config) -> None:
        self._mcp_reload_generation += 1
        generation = self._mcp_reload_generation
        old_task = self._mcp_reload_task
        if old_task is not None and not old_task.done():
            # Do not cancel a reload while it may be swapping/closing subprocesses;
            # the generation guard lets it finish cleanup and skip stale commits.
            log.info("已有 MCP 热重载任务仍在运行，新的配置会在代际检查后接管")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("MCP 热重载需要运行中的事件循环；本次配置变更暂未热应用")
            return
        self._mcp_reload_task = loop.create_task(self._run_mcp_hot_reload(config, generation), name=f"mcp-hot-reload-{generation}")

    async def _run_mcp_hot_reload(self, config: Config, generation: int) -> None:
        async with self._mcp_reload_lock:
            if generation != self._mcp_reload_generation:
                return
            try:
                commit, abort = await self.mcp.prepare_reload(config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("MCP 热重载失败，保留上一组已连接 MCP 工具", 错误=f"{type(exc).__name__}: {exc}")
                # Reload failed: keep the previous MCP manager/tools if they still
                # exist, but rebuild non-MCP runtime pieces so other config changes
                # remain visible to subsequent turns.
                self._rebuild_tools_and_context(include_mcp=bool(self.mcp.available_tools()))
                return
            if generation != self._mcp_reload_generation:
                await abort()
                return
            self.config = config
            self.web_admin.apply_config(
                config,
                llm_factory=self.factory,
                model_selection=self.selection,
            )
            await commit()
            self.web_admin.mcp = self.mcp
            self._rebuild_tools_and_context(include_mcp=True)
            log.info("MCP 热重载完成", 启用=config.mcp.enabled, 工具数=len(self.mcp.available_tools()))

    def apply_config(self, config: Config) -> None:
        """应用运行期配置快照。

        已开始的 Agent run 仍使用启动时读到的参数；这里影响后续菜单展示和下一轮任务。
        MCP server/tool 变更会热重载：旧工具先保持可用（禁用时立即隐藏），新 server
        成功启动后再切换到新工具；失败时保留上一组已连接 MCP 工具，不要求重启。
        """
        old_mem = self.mem
        # Compare against the actually active MCP manager config, not merely
        # self.config: a previous hot-reload attempt may have failed while other
        # runtime settings still applied successfully.
        old_mcp_fingerprint = self._mcp_config_fingerprint(self.mcp.config)
        new_mcp_fingerprint = self._mcp_config_fingerprint(config)
        mcp_changed = old_mcp_fingerprint != new_mcp_fingerprint
        self.config = config
        self.mem = self._make_memory_client(config)
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(old_mem.close())
        self.factory = BackendFactory(config.models, self.http)
        self.selection = ModelSelection(config.models, config_path())
        self.rath.configure(max_concurrent_tasks=config.rath.max_concurrent_tasks)
        self.web_admin.apply_config(
            config,
            llm_factory=self.factory,
            model_selection=self.selection,
        )

        # If MCP is being disabled, hide tools immediately.  For enabled config
        # changes, keep the currently connected MCP tools until the async reload has
        # successfully prepared replacement clients/tools.
        include_current_mcp = (not mcp_changed) or bool(config.mcp.enabled and self.mcp.available_tools())
        # If MCP was previously enabled but failed to start during startup, avoid
        # leaking a new, unstarted config's pending/disabled tool metadata into the
        # active ToolRegistry before hot reload succeeds.
        if not self.mcp._started or self.mcp._closed:
            include_current_mcp = False
        self._rebuild_tools_and_context(include_mcp=include_current_mcp)
        if mcp_changed:
            log.info("MCP 配置已变更，开始热重载", 启用=config.mcp.enabled)
            self._schedule_mcp_hot_reload(config)
        log.info("运行期配置已应用", 主力模型=self.selection.current,
                 工具数=len(self.tools.names()), skills=len(self.skills), MCP热重载=mcp_changed)

    async def shutdown(self) -> None:
        await self.update.stop()
        await self.web_admin.stop()
        await self.models_dev_catalog.stop()
        await self.mcp.close()
        await self.control_actions.shutdown()
        await self.db.close()
        await self.http.close()
        await self.mem.close()
        log.info("服务已关停")
