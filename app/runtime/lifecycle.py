"""Run ownership and durable execution boundaries, independent of UI/task policy."""
from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from app.runtime.store import RuntimeStore

log = logging.getLogger(__name__)
_current: ContextVar[RunSession | None] = ContextVar("execution_session", default=None)
_active: dict[asyncio.Task, RunSession] = {}


def current_session() -> RunSession | None:
    session = _current.get()
    return session if session is not None and session.task is asyncio.current_task() else None


def session_for_task(task: asyncio.Task | None) -> RunSession | None:
    return _active.get(task) if task else None


def controller_session(db: Any, chat_id: int) -> RunSession | None:
    return next((s for s in _active.values() if s.store and s.store.db is db
                 and s.chat_id == chat_id and not s.task_uuid), None)


async def receive_agent_control(db, task_uuid: str, control_uuid: str) -> None:
    session = next((s for s in _active.values() if s.store and s.store.db is db
                    and s.task_uuid == task_uuid), None)
    if session:
        await session.accept_control(control_uuid, kind="steer", source_ref=f"agent-control:{control_uuid}")


def cancel_execution(task: asyncio.Task, reason: str = "cancelled") -> bool:
    """One cancellation owner; eager cancellation never implies side-effect rollback."""
    if task.done():
        return False
    session = _active.get(task)
    if session:
        session.cancel_reason = reason
    task.cancel()
    return True


class ObserverProxy:
    """A presentation failure cannot turn completed work into a retry request."""
    def __init__(self, target: Any) -> None:
        self._target = target

    _presentation_methods = frozenset({
        "on_status", "on_delta", "on_tool", "on_tool_start", "on_tool_result",
        "on_tool_update", "on_tool_progress", "on_model_output_progress", "on_retry_state", "set_footer",
        "cut", "finalize", "finalize_notice", "fail",
    })

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if not callable(value) or name not in self._presentation_methods:
            return value
        if inspect.iscoroutinefunction(value):
            async def async_observer(*args, **kwargs):
                try:
                    return await value(*args, **kwargs)
                except Exception:
                    log.exception("execution presentation observer failed: %s", name)
            return async_observer
        def observer(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            except Exception:
                log.exception("execution presentation observer failed: %s", name)
        return observer


def _consume_cleanup_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("execution cancellation cleanup failed")


class RunSession:
    def __init__(self, window=None, *, owner_key: str = "", task_uuid: str = "",
                 root_turn_uuid: str = "", chat_id: int = 0) -> None:
        self.window = window
        self.store = RuntimeStore(window.store.db) if window is not None else None
        self.owner_key = window.store.owner.key if window is not None else owner_key
        self.task_uuid, self.root_turn_uuid, self.chat_id = task_uuid, root_turn_uuid, chat_id
        self.run_id = uuid.uuid4().hex
        self.task: asyncio.Task | None = None
        self.cancel_reason = ""
        self.tool_action_id = ""
        self.tool_outcome = None
        self._accepted_controls: set[str] = set()
        self.delivered_controls: set[str] = set()

    async def run(self, body, *, classify=None):
        self.task = asyncio.current_task()
        token = _current.set(self)
        _active[self.task] = self
        try:
            if self.store:
                await self.store.begin_run(self.owner_key, run_id=self.run_id,
                    task_uuid=self.task_uuid, root_turn_uuid=self.root_turn_uuid)
            value = await body()
            await self.finish(classify(value) if classify else "completed")
            return value
        except asyncio.CancelledError:
            # A second stop must not leave indefinitely running database records.
            cleanup = asyncio.create_task(self.finish("cancelled"))
            deadline = asyncio.get_running_loop().time() + 5
            while not cleanup.done():
                try:
                    async with asyncio.timeout_at(deadline):
                        await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # A repeated stop does not cancel the terminal write itself.
                    continue
                except TimeoutError:
                    cleanup.cancel()
                    # The bounded cleanup may finish after this runner exits.
                    cleanup.add_done_callback(_consume_cleanup_result)
                    break
                except Exception:
                    break
            if cleanup.done():
                _consume_cleanup_result(cleanup)
            raise
        except BaseException as exc:
            status = getattr(exc, "runtime_status", "failed")
            try:
                await self.finish(status if status in {"failed", "needs_control"} else "failed")
            except Exception:
                log.exception("execution terminal persistence failed: %s", self.run_id)
            raise
        finally:
            _active.pop(self.task, None)
            _current.reset(token)

    async def phase(self, phase: str, *, waiting=False) -> None:
        if self.store:
            await self.store.transition(self.run_id, "waiting" if waiting else "running", phase=phase)

    async def finish(self, status: str) -> None:
        if self.store is None:
            return
        async with self.store.db.write_transaction(label="runtime-finish-run"):
            # Cancellation can happen before begin_run commits anything.
            if await self.store.get_run(self.run_id) is None:
                return
            for action in await self.store.actions(self.run_id):
                if action["status"] == "started":
                    await self.store.finish_action(action["action_id"], status="unknown",
                        detail={"reason": "execution_ended_before_result_commit"})
            await self.store.transition(self.run_id, status, detail={"cancelReason": self.cancel_reason} if self.cancel_reason else None)

    async def start_attempt(self, outcome) -> None:
        if self.store:
            await self.store.begin_action(self.run_id, "model", action_id=outcome.attempt_id,
                name=str(outcome.request.options.get("model") or ""),
                detail={"usageKnown": False})

    @asynccontextmanager
    async def account_attempt(self, outcome):
        if self.store is None:
            yield True
            return
        async with self.store.accounting_claim(outcome.attempt_id) as first:
            yield first
            if first:
                await self.store.finish_action(outcome.attempt_id,
                    status={"ok": "completed", "error": "failed", "cancelled": "cancelled"}[outcome.status],
                    result_ref=f"model-attempt:{outcome.attempt_id}", detail={
                        "usageKnown": outcome.usage_reported,
                        "promptUsageKnown": outcome.prompt_usage_reported,
                        "inputTokens": outcome.response.usage.input_tokens,
                        "outputTokens": outcome.response.usage.output_tokens,
                        "cacheReadTokens": outcome.response.usage.cache_read_tokens,
                        "cacheWriteTokens": outcome.response.usage.cache_write_tokens,
                        "providerCostUsd": outcome.response.provider_cost_usd,
                        "errorType": outcome.error.reason if outcome.error else "",
                    })

    async def archive(self, messages) -> None:
        if self.window is not None:
            self.window.bind_sources(messages)
            await self.window.store.archive(messages)

    async def start_tool(self, call) -> str:
        if self.store:
            return await self.store.begin_action(self.run_id, "tool", call_id=call.id, name=call.name)
        return uuid.uuid4().hex

    async def finish_tool(self, action_id: str = "", *, outcome=None, result_ref: str = "") -> None:
        action_id = action_id or self.tool_action_id
        if self.store:
            status = getattr(outcome, "status", "unknown")
            if status == "denied":
                status = "not_started"
            await self.store.finish_action(action_id, status=status, result_ref=result_ref,
                detail={"effectState": getattr(outcome, "effect_state", "unknown"),
                        "businessSuccess": getattr(outcome, "business_success", None)})

    async def accept_control(self, source_id: str, *, kind="input", source_ref="") -> str:
        if self.store is None:
            return ""
        command_id = f"{self.run_id}:{source_id}"
        await self.store.enqueue_command(self.run_id, command_id=command_id, kind=kind,
                                         source_ref=source_ref or source_id)
        self._accepted_controls.add(command_id)
        return command_id

    async def delivered(self, command_ids, revision: int) -> None:
        if self.store:
            rows = {r["command_id"]: r for r in await self.store.pending_commands(self.run_id)}
            for command_id in command_ids:
                row = rows.get(command_id)
                if row and row["status"] == "received":
                    await self.store.mark_delivered(command_id, checkpoint_revision=revision)
                if row:
                    self.delivered_controls.add(command_id)

    async def deliver_context(self, messages, command_ids) -> None:
        if self.window is None or not command_ids:
            return
        async with self.store.db.write_transaction(label="runtime-control-context"):
            state = await self.window.checkpoint(messages)
            await self.delivered(command_ids, state["revision"])

    async def acknowledge_commands(self, command_ids) -> None:
        if self.store:
            for command_id in command_ids:
                await self.store.acknowledge_command(command_id)
                self.delivered_controls.discard(command_id)

    async def acknowledged(self, source_id: str) -> None:
        if self.store:
            command_id = f"{self.run_id}:{source_id}"
            if command_id in self._accepted_controls:
                await self.store.acknowledge_command(command_id)
