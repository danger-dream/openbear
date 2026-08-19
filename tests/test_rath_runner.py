from __future__ import annotations

import asyncio

import pytest

from app.db.engine import DB
from app.llm.base import AgentResult
from app.llm.events import ToolCall, Usage
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.rath.runner import RathTaskCancelled, RathWorkflowRunner
from app.rath.schemas import RathAgentDef
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.base import ToolRegistry


@pytest.fixture
async def env(tmp_path):
    db = DB(str(tmp_path / "rath.db"))
    await db.connect()
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(chat_id=123, workflow_uuid=workflow_uuid, title="Runner 测试")
    try:
        yield dao, task_uuid
    finally:
        await db.close()


async def test_runner_pause_resume_and_steer(env):
    dao, task_uuid = env
    runner = RathWorkflowRunner(dao, task_uuid, poll_interval_s=0.01)

    await dao.add_control(task_uuid, "pause", message="暂停")

    async def hit_checkpoint():
        await runner.checkpoint("before_agent", agent_key="planner")

    task = asyncio.create_task(hit_checkpoint())
    row = None
    for _ in range(50):
        row = await dao.get_task(task_uuid)
        if row is not None and row.status == "paused":
            break
        await asyncio.sleep(0.01)
    assert row is not None
    assert row.status == "paused"
    assert "before_agent" in row.current_status

    await dao.add_control(task_uuid, "steer", message="重点看安全边界")
    await dao.add_control(task_uuid, "resume", message="继续")
    await asyncio.wait_for(task, timeout=1)

    row = await dao.get_task(task_uuid)
    assert row is not None
    assert row.status == "running"
    assert len(runner.steers) == 1
    assert runner.steers[0]["message"] == "重点看安全边界"
    assert runner.steers[0]["controlUuid"]
    assert runner.steers[0]["metadata"] == {}
    events = await dao.events(task_uuid)
    assert "pause_applied" in [e.kind for e in events]
    assert "resume_applied" in [e.kind for e in events]


class _CapturingSingleAgentBackend:
    protocol = "chat"

    def __init__(self) -> None:
        self.calls = []

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        self.calls.append(messages)
        return AgentResult(text="已按追加指导处理。", usage=Usage(input_tokens=10, output_tokens=5))


async def test_single_agent_steer_is_injected_into_model_messages(env):
    dao, seed_task_uuid = env
    seed_task = await dao.get_task(seed_task_uuid)
    assert seed_task is not None
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=seed_task.workflow_uuid,
        title="安全检查",
        input_data={"instruction": "检查安全边界"},
    )
    task = await dao.get_task(task_uuid)
    assert task is not None
    await dao.add_control(task_uuid, "steer", message="重点确认 Bash 自保护")
    agent = RathAgentDef(
        id=1,
        workflow_uuid=task.workflow_uuid,
        agent_key="security",
        name="安全检查员",
        description="检查安全边界",
        system_prompt="你是安全检查员",
        model="openai/gpt",
        think_level="off",
        tool_allowlist=[],
        enabled=True,
    )
    backend = _CapturingSingleAgentBackend()
    runner = SingleAgentWorkflowRunner(
        dao,
        task_uuid,
        agent=agent,
        backend=backend,
        model="gpt",
        max_tokens=1024,
        tools=ToolRegistry(),
    )

    output = await runner.run()

    assert output["summary"] == "已按追加指导处理。"
    assert backend.calls
    assert any("重点确认 Bash 自保护" in str(msg.get("content") or "") for msg in backend.calls[0])


async def test_runner_stop_at_checkpoint(env):
    dao, task_uuid = env
    runner = RathWorkflowRunner(dao, task_uuid, poll_interval_s=0.01)
    await dao.add_control(task_uuid, "stop", message="结束")

    with pytest.raises(RathTaskCancelled):
        await runner.checkpoint("before_model_call", agent_key="reader")

    events = await dao.events(task_uuid)
    assert "cancel_requested" in [e.kind for e in events]


async def test_manager_start_marks_cancelled(env):
    dao, task_uuid = env
    manager = RathTaskManager(dao)

    async def runner_factory(_task_uuid: str):
        await asyncio.sleep(10)

    task = manager.start(task_uuid, 123, runner_factory)
    await asyncio.sleep(0)
    await manager.stop(task_uuid, message="结束")
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await dao.get_task(task_uuid)
    assert row is not None
    assert row.status == "cancelled"
    controls = await dao.pending_controls(task_uuid)
    assert controls == []


async def test_manager_execution_slot_respects_runtime_config(env):
    dao, _task_uuid = env
    manager = RathTaskManager(dao, max_concurrent_tasks=1)
    entered: list[str] = []
    release_first = asyncio.Event()

    async def hold(name: str) -> None:
        async with manager.execution_slot(name):
            entered.append(name)
            if name == "first":
                await release_first.wait()

    first = asyncio.create_task(hold("first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(hold("second"))
    await asyncio.sleep(0.05)
    assert entered == ["first"]

    manager.configure(max_concurrent_tasks=2)
    for _ in range(50):
        if entered == ["first", "second"]:
            break
        await asyncio.sleep(0.01)
    assert entered == ["first", "second"]

    release_first.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)


async def test_manager_stop_active_for_chat_stops_all_active_tasks(env):
    dao, task_uuid = env
    workflow = (await dao.list_workflows())[0]
    second_uuid = await dao.create_task(chat_id=123, workflow_uuid=workflow.workflow_uuid, title="second")
    other_chat_uuid = await dao.create_task(chat_id=999, workflow_uuid=workflow.workflow_uuid, title="other")
    manager = RathTaskManager(dao)

    async def runner_factory(_task_uuid: str):
        await asyncio.sleep(10)

    first_task = manager.start(task_uuid, 123, runner_factory)
    second_task = manager.start(second_uuid, 123, runner_factory)
    other_task = manager.start(other_chat_uuid, 999, runner_factory)
    await asyncio.sleep(0)

    stopped = await manager.stop_active_for_chat(123, message="new session")

    assert stopped == 2
    with pytest.raises(asyncio.CancelledError):
        await first_task
    with pytest.raises(asyncio.CancelledError):
        await second_task
    assert not other_task.done()
    other_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await other_task

    first = await dao.get_task(task_uuid)
    second = await dao.get_task(second_uuid)
    other = await dao.get_task(other_chat_uuid)
    assert first is not None and first.status == "cancelled"
    assert second is not None and second.status == "cancelled"
    assert other is not None and other.status == "cancelled"
