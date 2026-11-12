"""Agent control policy; coroutine ownership and capacity belong to the shared runtime."""
from __future__ import annotations

import asyncio

from app.runtime.lifecycle import cancel_execution
from app.runtime.scheduler import ExecutionScheduler, ExecutionLease
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from app.llm.base import OpenBearLLMError
from app.logging import get_logger
from app.agents.dao import AgentDAO
from app.agents.schemas import (
    ACTIVE_TASK_STATUSES,
    CONTROLLABLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    AgentTask,
)
from app.tools import processes

log = get_logger("agents.control")

RunnerFactory = Callable[[str], Awaitable[None]]
PlanWaiterCanceller = Callable[[str, str], Awaitable[None]]


class AgentControlService:
    """Task/Plan control policy over the shared runtime scheduler.

    Legacy rath_task_controls retains original decisions for history compatibility;
    this service owns no coroutine registry, model loop or concurrency pool.
    """

    def __init__(self, dao: AgentDAO, *, max_concurrent_tasks: int = 3,
                 scheduler: ExecutionScheduler | None = None) -> None:
        self.dao = dao
        self.scheduler = scheduler if scheduler is not None else ExecutionScheduler(
            max_concurrent_agents=max_concurrent_tasks)
        self._plan_waiter_canceller: PlanWaiterCanceller | None = None
        self.plan_coordinator = None

    @property
    def max_concurrent_tasks(self) -> int:
        return self.scheduler.max_concurrent_agents

    def configure(self, *, max_concurrent_tasks: int | None = None) -> None:
        if max_concurrent_tasks is not None:
            self.scheduler.configure(max_concurrent_agents=max_concurrent_tasks)

    @asynccontextmanager
    async def execution_slot(self, task_uuid: str) -> AsyncIterator[ExecutionLease]:
        async with self.scheduler.execution_slot(self.scheduler.key("agent", task_uuid)) as lease:
            yield lease

    def set_plan_waiter_canceller(self, callback: PlanWaiterCanceller) -> None:
        self._plan_waiter_canceller = callback

    async def release_execution_slot(self, task_uuid: str) -> bool:
        lease = self.scheduler.lease(self.scheduler.key("agent", task_uuid))
        return await lease.release() if lease is not None else False

    async def acquire_execution_slot(self, task_uuid: str) -> bool:
        lease = self.scheduler.lease(self.scheduler.key("agent", task_uuid))
        return await lease.acquire() if lease is not None else False

    def execution_slot_held(self, task_uuid: str) -> bool:
        lease = self.scheduler.lease(self.scheduler.key("agent", task_uuid))
        return bool(lease is not None and lease.held)

    @property
    def execution_slots_in_use(self) -> int:
        return self.scheduler.slots_in_use

    def register(self, task_uuid: str, chat_id: int, task: asyncio.Task, *, occupies_chat: bool = True) -> None:
        self.scheduler.register(self.scheduler.key("agent", task_uuid), task,
                                chat_id=chat_id, occupies_chat=occupies_chat)

    def task(self, task_uuid: str) -> asyncio.Task | None:
        return self.scheduler.task(self.scheduler.key("agent", task_uuid))

    def is_running(self, task_uuid: str) -> bool:
        return self.task(task_uuid) is not None

    def request_retry_cancel(self, task_uuid: str) -> bool:
        return self.scheduler.request_retry_cancel(self.scheduler.key("agent", task_uuid))

    def consume_retry_cancel(self, task_uuid: str) -> bool:
        return self.scheduler.consume_retry_cancel(self.scheduler.key("agent", task_uuid))

    def request_retry_action(self, task_uuid: str, wait_id: str, action: str) -> bool:
        return self.scheduler.request_retry_action(self.scheduler.key("agent", task_uuid), wait_id, action)

    def consume_retry_action(self, task_uuid: str, wait_id: str) -> str:
        return self.scheduler.consume_retry_action(self.scheduler.key("agent", task_uuid), wait_id)

    async def _finalize_stale_active_task(self, task: AgentTask) -> None:
        """Clear an active DB row that no in-memory runner can ever finish.

        Only route-active tasks should reach this path. Detached Agent tasks
        may remain active in DB while not intercepting chat messages, so they are
        deliberately excluded by ``active_tasks_for_chat`` before stale cleanup.
        """
        if task.status not in ACTIVE_TASK_STATUSES:
            return
        if self.task(task.task_uuid) is not None:
            return
        controls = await self.dao.pending_controls(task.task_uuid)
        for control in controls:
            await self.dao.mark_control(
                control.control_uuid,
                "applied" if control.action == "stop" else "ignored",
                result="stale active task has no running coroutine",
            )
        if task.status == "stopping" or any(c.action == "stop" for c in controls):
            changed = await self.dao.update_task(
                task.task_uuid,
                status="cancelled",
                control_state="",
                current_status="任务已取消（运行协程不存在）",
                finish=True,
                expected_statuses=(task.status,),
            )
            if not changed:
                return
            await self.dao.append_event(task.task_uuid, "task_cancelled", summary="清理无运行协程的 Agent 任务")
            return
        changed = await self.dao.update_task(
            task.task_uuid,
            status="interrupted",
            control_state="",
            current_status="任务已中断（运行协程不存在）",
            error="active Agent task had no in-memory runner",
            finish=True,
            expected_statuses=(task.status,),
        )
        if not changed:
            return
        await self.dao.append_event(task.task_uuid, "task_interrupted", summary="清理无运行协程的 Agent 任务")

    async def active_task_for_chat(self, chat_id: int) -> AgentTask | None:
        tasks = await self.active_tasks_for_chat(chat_id)
        return tasks[0] if tasks else None

    async def active_tasks_for_chat(self, chat_id: int) -> list[AgentTask]:
        # Only route-active tasks are allowed to intercept chat messages.  A
        # detached Agent task may still be running in DB and ``_runs``; it is
        # intentionally invisible here so Web can start the next main turn.
        cid = int(chat_id or 0)
        locals_ = self.scheduler.routed(cid, kind="agent")
        if not locals_:
            return []

        live: list[AgentTask] = []
        for task_uuid in locals_:
            task = await self.dao.get_task(task_uuid)
            if task is None:
                self.scheduler.unroute(self.scheduler.key("agent", task_uuid))
                continue
            if task.status not in ACTIVE_TASK_STATUSES:
                self.scheduler.unroute(self.scheduler.key("agent", task_uuid))
                continue
            if self.task(task_uuid) is None:
                await self._finalize_stale_active_task(task)
                self.scheduler.unroute(self.scheduler.key("agent", task_uuid))
                continue
            live.append(task)
        return live

    async def all_active_tasks_for_chat(self, chat_id: int, *, limit: int = 100) -> list[AgentTask]:
        """Return every live Agent task for a chat, including detached Agents.

        This is for status semantics, not message routing.  Detached Agent work
        must not intercept new input, but it is still part of the same Web
        conversation lifecycle and must remain visible while it is live.
        """
        tasks = await self.dao.active_tasks_for_chat(int(chat_id or 0), limit=limit)
        live: list[AgentTask] = []
        for task in tasks:
            if self.task(task.task_uuid) is None:
                await self._finalize_stale_active_task(task)
                continue
            refreshed = await self.dao.get_task(task.task_uuid)
            if refreshed is not None and refreshed.status in ACTIVE_TASK_STATUSES:
                live.append(refreshed)
        return live

    async def all_controllable_tasks_for_chat(self, chat_id: int, *, limit: int = 100) -> list[AgentTask]:
        """Return Agent tasks that a stop/delete operation must be able to cancel.

        ``needs_openbear_control`` is intentionally not live/running UI state, but
        it is still a pending Agent task owned by the conversation.  Stop/delete
        paths therefore include it without making it visible to routing/status
        helpers that are based on ``ACTIVE_TASK_STATUSES``.
        """
        tasks = await self.dao.active_tasks_for_chat(int(chat_id or 0), limit=limit, controllable=True)
        controllable: list[AgentTask] = []
        for task in tasks:
            refreshed = await self.dao.get_task(task.task_uuid)
            if refreshed is not None and refreshed.status in CONTROLLABLE_TASK_STATUSES:
                controllable.append(refreshed)
        return controllable

    async def create_task(
        self,
        *,
        chat_id: int,
        workflow_uuid: str,
        title: str,
        input_data: dict | None = None,
        parent_session_uuid: str = "",
        agent_session_uuid: str = "",
        caller_agent_session_uuid: str = "",
        parent_task_uuid: str = "",
        turn_uuid: str = "",
        parent_turn_uuid: str = "",
        run_root_turn_uuid: str = "",
    ) -> str:
        return await self.dao.create_task(
            chat_id=chat_id,
            workflow_uuid=workflow_uuid,
            title=title,
            input_data=input_data or {},
            parent_session_uuid=parent_session_uuid,
            agent_session_uuid=agent_session_uuid,
            caller_agent_session_uuid=caller_agent_session_uuid,
            parent_task_uuid=parent_task_uuid,
            turn_uuid=turn_uuid,
            parent_turn_uuid=parent_turn_uuid,
            run_root_turn_uuid=run_root_turn_uuid,
        )

    def start(self, task_uuid: str, chat_id: int, runner_factory: RunnerFactory) -> asyncio.Task:
        async def _run() -> None:
            try:
                async with self.execution_slot(task_uuid):
                    await runner_factory(task_uuid)
            except asyncio.CancelledError:
                await self.mark_cancelled(task_uuid)
                raise
            except Exception as exc:
                current = await self.dao.get_task(task_uuid)
                if current is not None and current.status == "stopping":
                    await self.mark_cancelled(task_uuid)
                    return
                log.exception("Agent task failed", task_uuid=task_uuid)
                if isinstance(exc, OpenBearLLMError):
                    error_summary = exc.user_message()
                    error_detail = exc.public_detail()
                else:
                    error_summary = f"{type(exc).__name__}: {exc}"
                    error_detail = {}
                changed = await self.dao.update_task(
                    task_uuid,
                    status="failed",
                    error=error_summary,
                    current_status=f"任务失败：{error_summary}",
                    finish=True,
                    expected_statuses=ACTIVE_TASK_STATUSES,
                )
                if not changed:
                    return
                await self.dao.append_event(
                    task_uuid,
                    "task_failed",
                    summary=f"任务失败：{error_summary[:300]}",
                    detail=error_detail,
                )
            else:
                current = await self.dao.get_task(task_uuid)
                if current is not None and current.status == "stopping":
                    await self.mark_cancelled(task_uuid)

        task = asyncio.create_task(_run(), name=f"agent-task-{task_uuid[:8]}")
        try:
            self.register(task_uuid, chat_id, task)
        except BaseException:
            # A duplicate registration must not leave an unowned coroutine that
            # races the existing runner's task/Plan terminal state.
            task.cancel()
            raise
        return task

    async def mark_cancelled(self, task_uuid: str, *, current_status: str = "任务已取消") -> AgentTask | None:
        existing = await self.dao.get_task(task_uuid)
        if existing is None or existing.status in TERMINAL_TASK_STATUSES:
            return existing
        changed = await self.dao.update_task(
            task_uuid,
            status="cancelled",
            control_state="",
            current_status=current_status,
            finish=True,
            expected_statuses=CONTROLLABLE_TASK_STATUSES,
        )
        if changed:
            await self.dao.append_event(task_uuid, "task_cancelled", summary=current_status)
        return await self.dao.get_task(task_uuid)

    async def pause(self, task_uuid: str, *, requested_by: str = "web", message: str = "") -> str:
        cid = await self.dao.add_control(task_uuid, "pause", message=message, requested_by=requested_by)
        changed = await self.dao.update_task(
            task_uuid,
            status="pausing",
            control_state="pause_requested",
            expected_statuses=("queued", "running", "resuming"),
        )
        if not changed:
            await self.dao.mark_control(cid, "ignored", result="task is not pausable")
        return cid

    async def resume(self, task_uuid: str, *, requested_by: str = "web", message: str = "") -> str:
        cid = await self.dao.add_control(task_uuid, "resume", message=message, requested_by=requested_by)
        changed = await self.dao.update_task(
            task_uuid,
            status="resuming",
            control_state="resume_requested",
            expected_statuses=("paused", "pausing"),
        )
        if not changed:
            await self.dao.mark_control(cid, "ignored", result="task is not resumable")
        return cid

    async def stop(self, task_uuid: str, *, requested_by: str = "web", message: str = "") -> str:
        cid = await self.dao.add_control(task_uuid, "stop", message=message, requested_by=requested_by)
        claimed = await self.dao.update_task(
            task_uuid,
            status="stopping",
            control_state="stop_requested",
            expected_statuses=CONTROLLABLE_TASK_STATUSES,
        )
        # A durable terminal row is not proof that its coroutine has exited.
        # Plan cancellation may reach this path after committing the terminal
        # state; stop the registered execution without rewriting that state.
        t = self.task(task_uuid)
        if not claimed and t is None:
            await self.dao.mark_control(cid, "ignored", result="task is already terminal")
            return cid
        if self._plan_waiter_canceller is not None:
            await self._plan_waiter_canceller(task_uuid, "task stopped")
        killed_processes = processes.kill_for_task(task_uuid)
        if killed_processes:
            log.info("已强制终止 Agent 任务子进程", task_uuid=task_uuid, 数量=killed_processes)
        if t is not None:
            # Stop is intentionally eager: unlike pause, it may cancel a running
            # model/tool await.  Mark the durable control as applied here because
            # the runner may never reach the next cooperative checkpoint.
            await self.dao.mark_control(cid, "applied", result="cancelled immediately")
            cancel_execution(t, "user_stop")
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=0.2)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                # The Agent child task itself was cancelled as requested.
                pass
            except Exception:
                pass
            finally:
                task = await self.dao.get_task(task_uuid)
                if t.done() and task is not None and task.status == "stopping":
                    await self.mark_cancelled(task_uuid)
        else:
            task = await self.dao.get_task(task_uuid)
            if task is not None:
                await self._finalize_stale_active_task(task)
        return cid

    async def steer(
        self,
        task_uuid: str,
        message: str,
        *,
        requested_by: str = "web",
        metadata: dict[str, Any] | None = None,
        control_uuid: str | None = None,
    ) -> str:
        if control_uuid:
            control = await self.dao.control(control_uuid)
            if control is None or control.task_uuid != task_uuid or control.action != "steer":
                raise ValueError(f"Invalid pre-created steer control: {control_uuid}")
            return control_uuid
        task = await self.dao.get_task(task_uuid)
        if task is None:
            raise ValueError(f"Agent task not found: {task_uuid}")
        if task.status not in ACTIVE_TASK_STATUSES:
            if task.status == "needs_openbear_control":
                raise RuntimeError("Agent task is waiting for OpenBear control; use Agent(action=message)/continue_task instead of steer.")
            raise RuntimeError(f"Agent task is not active: {task.status}")
        return await self.dao.add_control(
            task_uuid,
            "steer",
            message=message,
            requested_by=requested_by,
            metadata=metadata or {},
        )

    async def stop_active_for_chat(
        self,
        chat_id: int,
        *,
        requested_by: str = "web",
        message: str = "停止当前 Agent 任务",
        timeout_s: float = 10.0,
    ) -> int:
        tasks = await self.active_tasks_for_chat(chat_id)
        if not tasks:
            return 0
        waiters: list[asyncio.Task] = []
        for task in tasks:
            await self.stop(task.task_uuid, requested_by=requested_by, message=message)
            running = self.task(task.task_uuid)
            if running is not None:
                waiters.append(running)
        if waiters:
            try:
                await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), timeout=timeout_s)
            except TimeoutError:
                log.warning("等待 Agent 任务停止超时", 会话=chat_id, 数量=len(waiters), 超时=timeout_s)
        return len(tasks)

    async def stop_all_for_chat(
        self,
        chat_id: int,
        *,
        requested_by: str = "web",
        message: str = "停止当前 Agent 任务",
        timeout_s: float = 10.0,
        require_terminated: bool = False,
    ) -> int:
        tasks = await self.all_controllable_tasks_for_chat(chat_id)
        if not tasks:
            return 0
        waiters: list[asyncio.Task] = []
        for task in tasks:
            await self.stop(task.task_uuid, requested_by=requested_by, message=message)
            running = self.task(task.task_uuid)
            if running is not None:
                waiters.append(running)
        if waiters:
            try:
                await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), timeout=timeout_s)
            except TimeoutError:
                log.warning("等待全部 Agent 任务停止超时", 会话=chat_id, 数量=len(waiters), 超时=timeout_s)
        remaining = [task.task_uuid for task in tasks if self.task(task.task_uuid) is not None]
        if require_terminated and remaining:
            raise TimeoutError(f"Agent tasks did not stop before timeout: {', '.join(remaining)}")
        return len(tasks)

    def count(self) -> int:
        return len(self.scheduler.tasks(kind="agent"))
