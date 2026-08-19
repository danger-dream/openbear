from __future__ import annotations

import asyncio

import pytest

from app.db.engine import DB
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager


@pytest.fixture
async def env(tmp_path):
    db = DB(str(tmp_path / "manager.db"))
    await db.connect()
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    try:
        yield dao, workflow_uuid
    finally:
        await db.close()


async def test_register_rejects_duplicate_live_runner(env):
    dao, _workflow_uuid = env
    manager = RathTaskManager(dao)

    async def _sleep() -> None:
        await asyncio.sleep(30)

    first = asyncio.create_task(_sleep())
    second = asyncio.create_task(_sleep())
    try:
        manager.register("same-task", 123, first, occupies_chat=False)
        with pytest.raises(RuntimeError, match="already has an active runner"):
            manager.register("same-task", 123, second, occupies_chat=False)
        assert manager.task("same-task") is first
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_stop_does_not_rewrite_completed_task(env):
    dao, workflow_uuid = env
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="already completed",
        status="running",
    )
    await dao.update_task(
        task_uuid,
        status="completed",
        output={"summary": "durable result"},
        finish=True,
        expected_statuses=("running",),
    )
    manager = RathTaskManager(dao)

    control_uuid = await manager.stop(task_uuid, message="late stop")

    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "completed"
    assert task.output["summary"] == "durable result"
    cur = await dao.db.conn.execute(
        "SELECT status FROM rath_task_controls WHERE control_uuid=?",
        (control_uuid,),
    )
    assert (await cur.fetchone())["status"] == "ignored"


async def test_stop_wins_over_late_runner_completion(env):
    dao, workflow_uuid = env
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="stop race",
        status="running",
    )
    manager = RathTaskManager(dao)
    completion_changed: list[bool] = []

    async def _late_complete() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            completion_changed.append(await dao.update_task(
                task_uuid,
                status="completed",
                output={"summary": "late result"},
                finish=True,
                expected_statuses=("running", "resuming"),
            ))

    runner = asyncio.create_task(_late_complete())
    manager.register(task_uuid, 123, runner, occupies_chat=False)
    await manager.stop(task_uuid, message="cancel now")

    task = await dao.get_task(task_uuid)
    assert completion_changed == [False]
    assert task is not None
    assert task.status == "cancelled"
    assert task.output == {}


async def test_stop_without_live_runner_marks_task_cancelled(env):
    dao, workflow_uuid = env
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="stale",
        status="running",
    )
    manager = RathTaskManager(dao)

    await manager.stop(task_uuid, message="用户停止")

    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "cancelled"
    assert task.control_state == ""
    assert task.finished_at > 0
    controls = await dao.pending_controls(task_uuid)
    assert controls == []


async def test_active_lookup_cleans_stale_running_task(env):
    dao, workflow_uuid = env
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="stale",
        status="running",
    )
    manager = RathTaskManager(dao)
    manager._chat_active[123] = [task_uuid]  # simulate an interactive route-active task whose coroutine vanished

    active = await manager.active_task_for_chat(123)

    assert active is None
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "interrupted"
    assert task.control_state == ""
    assert task.finished_at > 0


async def test_detached_running_task_does_not_intercept_chat(env):
    dao, workflow_uuid = env
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="detached",
        status="running",
    )
    manager = RathTaskManager(dao)

    async def _sleep() -> None:
        await asyncio.sleep(30)

    task = asyncio.create_task(_sleep())
    manager.register(task_uuid, 123, task, occupies_chat=False)
    try:
        active = await manager.active_task_for_chat(123)

        assert active is None
        assert manager.task(task_uuid) is task
        stored = await dao.get_task(task_uuid)
        assert stored is not None
        assert stored.status == "running"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_stop_all_for_chat_cancels_detached_running_task(env):
    dao, workflow_uuid = env
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="detached",
        status="running",
    )
    manager = RathTaskManager(dao)

    async def _sleep() -> None:
        await asyncio.sleep(30)

    task = asyncio.create_task(_sleep())
    manager.register(task_uuid, 123, task, occupies_chat=False)

    stopped = await manager.stop_all_for_chat(123, timeout_s=1.0)

    assert stopped == 1
    assert task.done()
    stored = await dao.get_task(task_uuid)
    assert stored is not None
    assert stored.status == "cancelled"


async def test_stop_all_for_chat_cancels_needs_openbear_control_task(env):
    dao, workflow_uuid = env
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="waiting control",
        status="needs_openbear_control",
    )
    manager = RathTaskManager(dao)

    assert await manager.all_active_tasks_for_chat(123) == []
    controllable = await manager.all_controllable_tasks_for_chat(123)
    assert [task.task_uuid for task in controllable] == [task_uuid]

    stopped = await manager.stop_all_for_chat(123, timeout_s=1.0)

    assert stopped == 1
    stored = await dao.get_task(task_uuid)
    assert stored is not None
    assert stored.status == "cancelled"
    assert stored.control_state == ""


async def test_interactive_registered_task_intercepts_chat(env):
    dao, workflow_uuid = env
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="interactive",
        status="running",
    )
    manager = RathTaskManager(dao)

    async def _sleep() -> None:
        await asyncio.sleep(30)

    task = asyncio.create_task(_sleep())
    manager.register(task_uuid, 123, task)
    try:
        active = await manager.active_task_for_chat(123)

        assert active is not None
        assert active.task_uuid == task_uuid
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_active_lookup_does_not_rewrite_terminal_local_tail(env):
    dao, workflow_uuid = env
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="done",
        status="running",
    )
    await dao.update_task(task_uuid, status="completed", current_status="任务完成", finish=True)
    manager = RathTaskManager(dao)
    manager._chat_active[123] = [task_uuid]  # simulate callback tail before in-memory clear

    active = await manager.active_task_for_chat(123)

    assert active is None
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "completed"
