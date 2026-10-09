"""P0 timing/AgentWait regressions; fake providers and task-local SQLite only."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.agent.result import RunResult
from app.agents.control import AgentControlService
from app.agents.execution import AgentExecutor
from app.agents.supervision import review_observation
from app.db.dao import MessageDAO
from app.llm.base import AgentResult, OpenBearLLMError
from app.llm.events import StreamEvent, ToolCall, Usage
from app.tools.agents import AgentTools
from app.tools.base import ToolRegistry
from app.web_console.chat_state import WebAdminChatStateMixin
from tests import test_rath_single_agent as agent_fixtures
from tests.test_tools_agent_orchestration import _FakeConfig, _FakeFactory, _FakeSelection

env = agent_fixtures.env


@pytest.mark.parametrize("mode", ["text", "tool", "failure", "cancel", "unknown", "complete"])
async def test_agent_outcome_event_and_tool_entry_ledger_timings(env, mode):
    dao, task_uuid, agent = env
    messages = MessageDAO(dao.db)
    await messages.ensure_session(123)
    session = await messages.get_or_create_session_uuid(123)
    tools = AgentTools(config=_FakeConfig(), dao=dao, manager=AgentControlService(dao),
                       llm_factory=_FakeFactory(), model_selection=_FakeSelection(), registry=ToolRegistry())
    calls = []

    async def account(detail):
        calls.append(detail)
        await tools._persist_agent_model_call(chat_id=123, session_uuid=session,
            model_label="openai/gpt", protocol="chat", detail=detail)

    class Backend:
        protocol = "chat"
        count = 0

        async def stream(self, *args, **kwargs):
            self.count += 1
            if mode == "unknown":
                raise OpenBearLLMError("before headers", retryable=False)
            yield StreamEvent("metrics", connect_ms=7)
            yield StreamEvent("usage", usage=Usage(input_tokens=3, output_tokens=2))
            if mode == "tool" and self.count == 1:
                yield StreamEvent("tool_input", details={"receivedBytes": 10, "toolNames": ["Read"]})
                yield StreamEvent("tool_call", tool_calls=[ToolCall("read", "Read", "{}")])
                yield StreamEvent("finish", finish_reason="tool_calls")
                return
            yield StreamEvent("content", text="done")
            if mode == "failure":
                raise OpenBearLLMError("after output", retryable=False)
            if mode == "cancel":
                raise asyncio.CancelledError()
            yield StreamEvent("finish", finish_reason="stop")

    class Complete:
        protocol = "chat"

        async def complete(self, *args, **kwargs):
            return AgentResult(text="done", usage=Usage(input_tokens=3, output_tokens=2))

    registry = ToolRegistry()

    async def read(_args):
        return "recorded research result"

    registry.add("Read", "fake", {"type": "object"}, read, visibility={"agent"})
    runner = AgentExecutor(dao, task_uuid, agent=agent, backend=Complete() if mode == "complete" else Backend(),
                           model="gpt", max_tokens=1024, tools=registry, max_retries=0, on_model_call=account)
    try:
        await runner.run()
    except asyncio.CancelledError:
        assert mode == "cancel"
    except OpenBearLLMError:
        assert mode in {"failure", "unknown"}
    cur = await dao.db.conn.execute("SELECT * FROM model_calls WHERE chat_id=? ORDER BY id DESC", (123,))
    rows = [dict(row) for row in await cur.fetchall()]
    assert len(rows) == len(calls) == (2 if mode == "tool" else 1)
    assert len({row["attempt_id"] for row in rows}) == len(rows)
    events = await dao.events(task_uuid, limit=1000)
    for detail in calls:
        row = next(row for row in rows if row["attempt_id"] == detail["attemptId"])
        assert row["connect_ms"] == detail["connectMs"] == (0 if mode in {"unknown", "complete"} else 7)
        assert row["first_token_ms"] == detail["firstTokenMs"]
        assert (row["first_token_ms"] > 0) == (mode not in {"unknown", "complete"})
        assert row["total_time_ms"] == detail["durationMs"]
        terminal = next(event for event in events if event.kind in {"model_call_finished", "model_stream_interrupted"}
                        and event.detail["attemptId"] == row["attempt_id"])
        assert terminal.detail["connectMs"] == row["connect_ms"]
        assert terminal.detail["firstTokenMs"] == row["first_token_ms"]
    assert rows[0]["status"] == {"failure": "error", "unknown": "error", "cancel": "cancelled"}.get(mode, "ok")


def test_turn_average_population_excludes_children_and_unknown_stages():
    result = RunResult(model_ok=3, model_calls=3, expert_model_calls=80,
                       connect_samples=1, first_token_samples=2, call_time_samples=3,
                       connect_ms_sum=12, first_token_ms_sum=100, call_time_ms_sum=300)
    stats = WebAdminChatStateMixin._run_stats_json(None, result, cost_usd=0, model="fake",
                                                   think_level="off", context_window=1000)
    assert stats["modelOk"] == 83
    assert (stats["avgConnectMs"], stats["avgFirstTokenMs"], stats["avgTotalMs"]) == (12, 50, 100)
    assert (stats["connectSamples"], stats["firstTokenSamples"], stats["totalTimeSamples"]) == (1, 2, 3)
    empty = WebAdminChatStateMixin._run_stats_json(None, RunResult(expert_model_calls=80),
                        cost_usd=0, model="fake", think_level="off", context_window=1000)
    assert empty["avgConnectMs"] is empty["avgFirstTokenMs"] is empty["avgTotalMs"] is None


async def test_review_backlog_is_not_new_activity_and_does_not_lose_events(env):
    dao, task_uuid, _agent = env
    await dao.update_task(task_uuid, status="running", current_status="模型调用中")
    await dao.append_event(task_uuid, "model_call_started", detail={"attemptId": "a"})
    for size in range(120):
        await dao.append_event(task_uuid, "model_stream_progress", detail={
            "toolInput": {"attemptId": "a", "receivedBytes": size, "toolNames": ["Write"], "phase": "generating"}})
    critical = await dao.append_event(task_uuid, "plan_criterion_recorded", detail={"criterionId": "c1"})
    await dao.db.conn.execute("UPDATE rath_task_events SET ts=100 WHERE task_uuid=?", (task_uuid,))
    await dao.db.conn.commit()
    task = await dao.get_task(task_uuid)
    previous, seen = {}, []
    for index in range(3):
        batch = await dao.supervision_events(task_uuid, after_seq=previous.get("lastEventSeq", 0))
        observation, previous = review_observation(task, batch, {}, previous, now_ms=700000)
        seen += [event.seq for event in batch["events"]]
        assert observation["hasActivity"] is (index == 0)
        assert observation["hasMeaningfulProgress"] == observation["hasActivity"]
        assert observation["activityAgeMs"] == 600000
        assert observation["currentExecution"]["elapsedMs"] == 600000
        assert observation["currentExecution"]["attemptId"] == "a"
        assert observation["currentExecution"]["toolInput"]["receivedBytes"] == 119
        assert observation["deliveryEvidence"]["known"] is False
        assert observation["deliveryEvidence"]["changedSinceReview"] is None
        assert observation["eventPage"]["hasMore"] is (index < 2)
    assert seen == list(range(1, critical + 1))
    # Reading or writing memory counts as activity, not proof of delivery failure.
    await dao.append_event(task_uuid, "tool_call_started", detail={"name": "TaskMemory"})
    observation, _ = review_observation(task, await dao.supervision_events(task_uuid), {}, previous, now_ms=700000)
    assert observation["hasActivity"]
    assert observation["currentExecution"]["phase"] == "tool"
    assert observation["deliveryEvidence"]["known"] is False


async def test_review_retry_wait_recorded_plan_and_terminal_boundary(env):
    dao, task_uuid, _agent = env
    await dao.update_task(task_uuid, status="running")
    task = await dao.get_task(task_uuid)
    await dao.append_event(task_uuid, "model_call_started", detail={"attemptId": "a"})
    await dao.append_event(task_uuid, "model_stream_interrupted", detail={"attemptId": "a"})
    await dao.append_event(task_uuid, "model_call_retry_wait", detail={
        "retry": {"active": True, "retryAtMs": 900000, "delayMs": 300000, "attempt": 1}})
    plan = {"activePlanVersion": 1, "steps": [{"stepId": "s1", "status": "running", "criteriaState": {}}]}
    batch = await dao.supervision_events(task_uuid)
    observation, previous = review_observation(task, batch, plan, {}, now_ms=700000)
    assert observation["currentExecution"] == {"phase": "retry_wait", "startedAtMs": 600000,
        "elapsedMs": 100000, "retryAtMs": 900000, "attempt": 1}
    assert observation["deliveryEvidence"]["changedSinceReview"] is None
    observation, _ = review_observation(task, batch, plan, previous, now_ms=700000)
    assert observation["deliveryEvidence"]["changedSinceReview"] is False
    updated = {"activePlanVersion": 1, "steps": [{"stepId": "s1", "status": "running",
               "criteriaState": {"c1": {"status": "passed", "evidence": "test:unit"}}}]}
    observation, _ = review_observation(task, batch, updated, previous, now_ms=700000)
    assert observation["deliveryEvidence"]["known"]
    assert observation["deliveryEvidence"]["changedSinceReview"] is True
    await dao.append_event(task_uuid, "model_call_retry_resumed", detail={"retry": {"active": False}})
    await dao.append_event(task_uuid, "model_call_started", detail={"attemptId": "b"})
    batch = await dao.supervision_events(task_uuid)
    observation, _ = review_observation(task, batch, updated, previous, now_ms=700000)
    assert observation["currentExecution"]["attemptId"] == "b"
    assert observation["currentExecution"]["lastReportedOutputAtMs"] is None
    terminal, _ = review_observation(replace(task, status="completed"), batch, updated, previous, now_ms=700000)
    assert terminal["currentExecution"] == {"phase": "completed", "startedAtMs": None, "elapsedMs": None}


@pytest.mark.parametrize("status,kind", [("paused", "pause_applied"), ("needs_openbear_control", "needs_openbear_control")])
async def test_review_control_wait_uses_recorded_boundary_not_last_activity(env, status, kind):
    dao, task_uuid, _agent = env
    await dao.update_task(task_uuid, status=status)
    await dao.append_event(task_uuid, kind)
    await dao.db.conn.execute("UPDATE rath_task_events SET ts=100 WHERE task_uuid=?", (task_uuid,))
    await dao.db.conn.commit()
    await dao.append_event(task_uuid, "steer_applied")
    observation, _ = review_observation(await dao.get_task(task_uuid),
        await dao.supervision_events(task_uuid), {}, {}, now_ms=200000)
    assert observation["currentExecution"] == {"phase": status, "startedAtMs": 100000, "elapsedMs": 100000}


async def test_review_high_water_excludes_append_racing_with_current_snapshot(env, monkeypatch):
    dao, task_uuid, _agent = env
    start_seq = await dao.append_event(task_uuid, "model_call_started", detail={"attemptId": "a"})
    original = dao.events

    async def read_then_append(*args, **kwargs):
        result = await original(*args, **kwargs)
        await dao.append_event(task_uuid, "model_call_started", detail={"attemptId": "b"})
        return result

    monkeypatch.setattr(dao, "events", read_then_append)
    batch = await dao.supervision_events(task_uuid)
    assert batch["highWater"] == start_seq
    assert batch["events"][-1].seq == start_seq
    assert [event.detail["attemptId"] for event in batch["current"]] == ["a"]


async def test_review_plan_wait_matches_pending_version(env):
    dao, task_uuid, _agent = env
    await dao.update_task(task_uuid, status="running")
    await dao.append_event(task_uuid, "tool_call_started", detail={"name": "AgentPlanSubmit"})
    await dao.append_event(task_uuid, "plan_submitted", detail={"planVersion": 1})
    await dao.db.conn.execute("UPDATE rath_task_events SET ts=100 WHERE task_uuid=?", (task_uuid,))
    await dao.db.conn.commit()
    plan = {"phase": "awaiting_plan_decision", "pendingPlanVersion": 1, "activePlanVersion": 0, "steps": []}
    task, batch = await dao.get_task(task_uuid), await dao.supervision_events(task_uuid)
    observed, _ = review_observation(task, batch, plan, {}, now_ms=200000)
    assert observed["currentExecution"] == {"phase": "plan_approval", "startedAtMs": 100000, "elapsedMs": 100000}
    observed, _ = review_observation(task, batch, {**plan, "pendingPlanVersion": 2}, {}, now_ms=200000)
    assert observed["currentExecution"]["elapsedMs"] is None
