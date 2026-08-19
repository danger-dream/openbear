"""Cooperative Rath workflow runner primitives."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.rath.dao import RathDAO

# Optional callback invoked after ``emit`` writes an event to DB.
# Used by the Agent tool layer to push real-time progress to the Web UI
# without waiting for the next polling cycle.
EventCallback = Callable[..., Awaitable[None]]


class RathTaskCancelled(asyncio.CancelledError):
    """Raised by a runner checkpoint when a Rath task should stop."""


class RathNeedsOpenBearControl(RuntimeError):
    """Raised when a child Agent must hand control back to OpenBear."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload.get("message") or payload.get("reason") or "needs_openbear_control"))


class RathWorkflowRunner:
    """Base runner with durable events and cooperative controls.

    Concrete workflows can subclass this and call :meth:`checkpoint` between
    model/tool/agent steps.  Pause is cooperative: it takes effect only at a
    checkpoint, preserving tool-call pairing and artifact consistency.
    """

    def __init__(self, dao: RathDAO, task_uuid: str, *, poll_interval_s: float = 0.5, on_event: EventCallback | None = None) -> None:
        self.dao = dao
        self.task_uuid = task_uuid
        self.poll_interval_s = max(0.01, float(poll_interval_s))
        self.started_monotonic = time.monotonic()
        self.steers: list[dict[str, Any]] = []
        self.on_event = on_event

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)

    async def emit(
        self,
        kind: str,
        *,
        agent_key: str = "",
        summary: str = "",
        detail: dict[str, Any] | None = None,
    ) -> int:
        event_id = await self.dao.append_event(
            self.task_uuid,
            kind,
            agent_key=agent_key,
            summary=summary,
            detail=detail or {},
            elapsed_ms=self.elapsed_ms(),
        )
        if self.on_event is not None:
            try:
                asyncio.create_task(self.on_event(
                    kind=kind, agent_key=agent_key, summary=summary, detail=detail or {},
                ))
            except RuntimeError:
                pass
        return event_id

    async def set_status(self, *, status: str | None = None, agent_key: str = "", current: str = "") -> None:
        await self.dao.update_task(
            self.task_uuid,
            status=status,
            current_agent_key=agent_key if agent_key else None,
            current_status=current if current else None,
        )

    async def checkpoint(self, stage: str, *, agent_key: str = "") -> None:
        """Apply pending controls at a safe boundary."""
        controls = await self.dao.pending_controls(self.task_uuid)
        for control in controls:
            if control.action == "steer":
                self.steers.append({
                    "controlUuid": control.control_uuid,
                    "message": control.message,
                    "requestedBy": control.requested_by,
                    "metadata": control.metadata,
                })
                await self.dao.mark_control(control.control_uuid, "applied", result="steer queued")
                await self.emit(
                    "steer_applied",
                    agent_key=agent_key,
                    summary="追加指导已加入任务上下文",
                    detail={"message": control.message, "stage": stage},
                )
            elif control.action == "resume":
                # A resume outside paused state is harmless. Mark it applied so it
                # does not get replayed forever.
                await self.dao.mark_control(control.control_uuid, "applied", result="already running")
            elif control.action == "pause":
                await self.dao.mark_control(control.control_uuid, "applied", result=f"paused at {stage}")
                await self._pause_loop(stage, agent_key=agent_key)
            elif control.action == "stop":
                await self.dao.mark_control(control.control_uuid, "applied", result=f"stopped at {stage}")
                await self.emit("cancel_requested", agent_key=agent_key, summary="任务收到结束请求")
                raise RathTaskCancelled()
            else:
                await self.dao.mark_control(control.control_uuid, "ignored", result=f"unknown action {control.action}")

    async def _pause_loop(self, stage: str, *, agent_key: str = "") -> None:
        await self.dao.update_task(
            self.task_uuid,
            status="paused",
            control_state="paused",
            current_agent_key=agent_key,
            current_status=f"已暂停：{stage}",
        )
        await self.emit("pause_applied", agent_key=agent_key, summary=f"任务已暂停：{stage}")
        while True:
            await asyncio.sleep(self.poll_interval_s)
            controls = await self.dao.pending_controls(self.task_uuid)
            for control in controls:
                if control.action == "steer":
                    self.steers.append({
                        "controlUuid": control.control_uuid,
                        "message": control.message,
                        "requestedBy": control.requested_by,
                        "metadata": control.metadata,
                    })
                    await self.dao.mark_control(control.control_uuid, "applied", result="steer queued while paused")
                    await self.emit(
                        "steer_applied",
                        agent_key=agent_key,
                        summary="暂停期间的追加指导已记录",
                        detail={"message": control.message, "stage": stage},
                    )
                elif control.action == "resume":
                    await self.dao.mark_control(control.control_uuid, "applied", result=f"resumed from {stage}")
                    await self.dao.update_task(
                        self.task_uuid,
                        status="running",
                        control_state="",
                        current_agent_key=agent_key,
                        current_status=f"已继续：{stage}",
                    )
                    await self.emit("resume_applied", agent_key=agent_key, summary="任务已继续")
                    return
                elif control.action == "stop":
                    await self.dao.mark_control(control.control_uuid, "applied", result=f"stopped while paused at {stage}")
                    await self.emit("cancel_requested", agent_key=agent_key, summary="暂停期间收到结束请求")
                    raise RathTaskCancelled()
                elif control.action == "pause":
                    await self.dao.mark_control(control.control_uuid, "ignored", result="already paused")

    async def run_step(
        self,
        agent_key: str,
        label: str,
        body: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run one named workflow step with standard events/checkpoints."""
        await self.dao.update_task(
            self.task_uuid,
            status="running",
            current_agent_key=agent_key,
            current_status=label,
        )
        await self.emit("agent_started", agent_key=agent_key, summary=label)
        await self.checkpoint("before_agent", agent_key=agent_key)
        try:
            result = await body()
        except (RathTaskCancelled, RathNeedsOpenBearControl):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.emit(
                "agent_failed",
                agent_key=agent_key,
                summary=f"{label} 失败：{type(exc).__name__}",
                detail={"error": str(exc)},
            )
            raise
        await self.emit("agent_finished", agent_key=agent_key, summary=f"{label} 完成")
        await self.checkpoint("after_agent", agent_key=agent_key)
        return result

    async def complete(self, output: dict[str, Any] | None = None) -> bool:
        changed = await self.dao.update_task(
            self.task_uuid,
            status="completed",
            current_status="任务完成",
            output=output or {},
            finish=True,
            expected_statuses=("running",),
        )
        if changed:
            await self.emit("task_completed", summary="任务完成")
        return changed
