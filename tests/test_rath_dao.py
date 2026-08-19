from __future__ import annotations

import asyncio

import pytest

from app.db.engine import DB
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.dao import RathDAO


@pytest.fixture
async def db(tmp_path):
    d = DB(str(tmp_path / "rath.db"))
    await d.connect()
    yield d
    await d.close()


async def test_workflow_task_event_artifact_control_roundtrip(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    wf = await dao.workflow_by_slug("single-agent")
    assert wf is not None
    assert wf.workflow_uuid == workflow_uuid
    assert wf.config["mode"] == "single-agent"

    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="调研 OpenRath",
        input_data={"question": "OpenRath 是否适合接入 OpenBear"},
        parent_session_uuid="s1",
    )
    task = await dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "queued"
    assert task.input["question"].startswith("OpenRath")

    await dao.update_task(task_uuid, status="running", current_agent_key="planner", current_status="拆解问题")
    active = await dao.active_task_for_chat(123)
    assert active is not None
    assert active.task_uuid == task_uuid
    assert active.current_agent_key == "planner"

    seq = await dao.append_event(task_uuid, "agent_started", agent_key="planner", summary="Planner 启动")
    assert seq >= 2  # create_task already writes task_created
    events = await dao.events(task_uuid)
    assert [e.kind for e in events][:1] == ["task_created"]
    assert events[-1].summary == "Planner 启动"

    artifact_uuid = await dao.create_artifact(
        task_uuid,
        kind="research_plan",
        name="调研计划",
        content="计划正文",
        agent_key="planner",
        summary="已生成调研计划",
        source_refs=[{"url": "https://example.com"}],
    )
    artifacts = await dao.artifacts(task_uuid)
    assert artifacts[0].artifact_uuid == artifact_uuid
    assert artifacts[0].source_refs[0]["url"] == "https://example.com"

    control_uuid = await dao.add_control(task_uuid, "pause", message="暂停任务", requested_by="web")
    pending = await dao.pending_controls(task_uuid)
    assert pending[0].control_uuid == control_uuid
    await dao.mark_control(control_uuid, "applied", result="paused")
    assert await dao.pending_controls(task_uuid) == []


async def test_agent_registry_crud(db):
    dao = RathDAO(db)

    seeded_agents = await dao.list_agents()
    assert seeded_agents == []

    agent_id = await dao.create_agent(
        agent_key="deep-researcher",
        name="深度调研员",
        description="适合做长资料调研",
        system_prompt="你是深度调研员",
        model="openai/gpt",
        think_level="high",
        tool_allowlist=["WebSearch", "WebExtract"],
        sort=99,
    )
    agent = await dao.agent_by_id(agent_id)
    assert agent is not None
    assert agent.name == "深度调研员"
    assert agent.tool_allowlist == ["WebSearch", "WebExtract"]
    assert agent.sort == 99

    await dao.update_agent(agent_id, enabled=False, model="openai/other", tool_allowlist=["Read"])
    assert await dao.agent_by_key("deep-researcher") is None
    disabled = await dao.agent_by_key("deep-researcher", include_disabled=True)
    assert disabled is not None
    assert disabled.model == "openai/other"
    assert disabled.tool_allowlist == ["Read"]

    await dao.delete_agent(agent_id)
    assert await dao.agent_by_id(agent_id) is None


async def test_mark_interrupted_running_tasks(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    running = await dao.create_task(chat_id=1, workflow_uuid=workflow_uuid, title="running", status="running")
    done = await dao.create_task(chat_id=1, workflow_uuid=workflow_uuid, title="done", status="completed")

    count = await dao.mark_interrupted_running()
    assert count == 1
    assert (await dao.get_task(running)).status == "interrupted"  # type: ignore[union-attr]
    assert (await dao.get_task(done)).status == "completed"  # type: ignore[union-attr]


async def test_agent_session_reuse_unique_and_close_on_new_boundary(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)

    first = await dao.get_or_create_agent_session(
        openbear_session_uuid="openbear-1",
        chat_id=123,
        workflow_uuid=workflow_uuid,
        agent_key="research",
        title="调研 Agent",
    )
    second = await dao.get_or_create_agent_session(
        openbear_session_uuid="openbear-1",
        chat_id=123,
        workflow_uuid=workflow_uuid,
        agent_key="research",
        title="调研 Agent",
    )
    assert second.session_uuid == first.session_uuid

    closed = await dao.close_agent_sessions_for_openbear_session("openbear-1", reason="test_new")
    assert closed == 1
    assert (await dao.agent_session(first.session_uuid)).status == "closed"  # type: ignore[union-attr]

    third = await dao.get_or_create_agent_session(
        openbear_session_uuid="openbear-1",
        chat_id=123,
        workflow_uuid=workflow_uuid,
        agent_key="research",
        title="调研 Agent",
    )
    assert third.session_uuid != first.session_uuid
    active = await dao.list_agent_sessions(openbear_session_uuid="openbear-1", status="active")
    assert [s.session_uuid for s in active] == [third.session_uuid]


async def test_events_without_after_seq_returns_recent_tail_in_order(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(chat_id=123, workflow_uuid=workflow_uuid, title="event-tail")

    for idx in range(1, 21):
        await dao.append_event(task_uuid, f"event_{idx}", summary=f"事件 {idx}")

    events = await dao.events(task_uuid, limit=5)
    assert [e.kind for e in events] == ["event_16", "event_17", "event_18", "event_19", "event_20"]
    assert [e.seq for e in events] == sorted(e.seq for e in events)


async def test_append_event_is_safe_for_concurrent_writers(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(chat_id=123, workflow_uuid=workflow_uuid, title="event-race")

    seqs = await asyncio.gather(*(
        dao.append_event(task_uuid, f"event_{idx}", summary=f"事件 {idx}")
        for idx in range(100)
    ))

    events = await dao.events(task_uuid, limit=200)
    assert len(events) == 101  # task_created + 100 concurrent events
    assert sorted(seqs) == list(range(2, 102))
    assert [e.seq for e in events] == list(range(1, 102))


async def test_append_event_ignores_a_pinned_shared_reader_snapshot(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(chat_id=123, workflow_uuid=workflow_uuid, title="event-stale-reader")
    await dao.append_event(task_uuid, "event_2")
    await dao.append_event(task_uuid, "event_3")

    held_cursor = await db.conn.execute(
        "SELECT seq FROM rath_task_events WHERE task_uuid=? ORDER BY seq",
        (task_uuid,),
    )
    first = await held_cursor.fetchone()
    assert int(first["seq"]) == 1
    try:
        assert await dao.append_event(task_uuid, "event_4") == 4
        assert await dao.append_event(task_uuid, "event_5") == 5
    finally:
        await held_cursor.close()

    events = await dao.events(task_uuid, limit=20)
    assert [event.seq for event in events] == [1, 2, 3, 4, 5]


async def test_append_event_is_atomic_across_db_instances(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(chat_id=123, workflow_uuid=workflow_uuid, title="event-multi-db")
    other_db = DB(db.path)
    await other_db.connect()
    other_dao = RathDAO(other_db)
    try:
        seqs = await asyncio.gather(*(
            (dao if idx % 2 == 0 else other_dao).append_event(task_uuid, f"event_{idx}")
            for idx in range(40)
        ))
    finally:
        await other_db.close()

    events = await dao.events(task_uuid, limit=100)
    assert sorted(seqs) == list(range(2, 42))
    assert [event.seq for event in events] == list(range(1, 42))


async def test_terminal_task_rejects_late_unconditional_updates(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(
        chat_id=123,
        workflow_uuid=workflow_uuid,
        title="terminal immutable",
        status="running",
    )
    completed = await dao.update_task(
        task_uuid,
        status="completed",
        output={"summary": "durable result"},
        finish=True,
        expected_statuses=("running",),
    )
    late = await dao.update_task(
        task_uuid,
        status="failed",
        output={"summary": "late overwrite"},
        error="late failure",
        finish=True,
    )

    task = await dao.get_task(task_uuid)
    assert completed is True
    assert late is False
    assert task is not None
    assert task.status == "completed"
    assert task.output == {"summary": "durable result"}
    assert task.error == ""


async def test_events_with_after_seq_keeps_incremental_semantics(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(chat_id=123, workflow_uuid=workflow_uuid, title="event-incremental")

    for idx in range(1, 8):
        await dao.append_event(task_uuid, f"event_{idx}", summary=f"事件 {idx}")

    events = await dao.events(task_uuid, after_seq=4, limit=2)
    assert [e.seq for e in events] == [5, 6]
    assert [e.kind for e in events] == ["event_4", "event_5"]


async def test_historical_agent_compaction_event_without_body_is_explicitly_unavailable(db):
    dao = RathDAO(db)
    workflow_uuid = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(chat_id=123, workflow_uuid=workflow_uuid, title="legacy compaction")
    await dao.append_event(
        task_uuid,
        "model_context_pre_compacted",
        summary="legacy event stored metadata only",
        detail={
            "estimatedTokensBefore": 50_000,
            "estimatedTokensAfter": 10_000,
            "summaryChars": 8_000,
        },
    )

    event = [item for item in await dao.events(task_uuid) if item.kind == "model_context_pre_compacted"][-1]
    assert event.detail["scope"] == "agent"
    assert event.detail["status"] == "pre_compacted"
    assert event.detail["beforeTokens"] == 50_000
    assert event.detail["afterTokens"] == 10_000
    assert event.detail["summaryChars"] == 8_000
    assert event.detail["outputAvailable"] is False
    assert event.detail["outputUnavailable"] == "historical_summary_not_stored"
    assert "compactedOutput" not in event.detail
