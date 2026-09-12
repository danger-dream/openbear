"""Exercise real runners/AgentContinue with isolated SQLite and deterministic model responses."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agent.loop import Agent
from app.agent.native_continuation import validate_model_context
from app.context.runtime import ContextManager
from app.context.strategies import ModelSummaryStrategy
from app.context.window import source_of
from app.llm.base import AgentResult
from app.llm.events import StreamEvent, ToolCall, Usage
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.base import ToolRegistry
from tests.test_agent_continuity import call
from tests.test_agent_continuity import env as continuation_fixture
from tests.test_agent_loop import RecordRenderer
from tests.test_context_strategies import SUMMARY, history
from tests.test_context_strategies import env as strategy_fixture
from tests.test_rath_single_agent import env as plan_fixture
from tests.test_tools_agent_orchestration import _FakeConfig

strategy_env = strategy_fixture
continuation_env = continuation_fixture
plan_env = plan_fixture


@pytest.mark.parametrize("final_only", [False, True])
async def test_controller_model_return_compacts_after_closed_batch_and_after_final(strategy_env, final_only):
    env = strategy_env
    env.selected["strategy"] = "model_summary"
    sequence, states = [], []
    registry = ToolRegistry()

    async def read(args):
        sequence.append("tool")
        return "last raw evidence"

    registry.add("Read", "read", {"type": "object"}, read)
    async def state(detail):
        states.append(detail)
    env.manager.on_state = state
    async def summary_start():
        sequence.append("summary")
        assert final_only or "tool" in sequence
    env.backends["p/first"].gate = summary_start

    class ExecutionBackend:
        protocol = "chat"
        calls = 0

        async def stream(self, messages, **kwargs):
            self.calls += 1
            assert validate_model_context(messages)
            assert "Context summary runtime" in kwargs["system"]
            if self.calls == 1 and not final_only:
                yield StreamEvent(kind="tool_call", tool_calls=[ToolCall("new", "Read", "{}")])
                yield StreamEvent(kind="usage", usage=Usage(input_tokens=8000, output_tokens=3))
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
            else:
                assert final_only or any(source_of(m).get("kind") == "summary" for m in messages)
                yield StreamEvent(kind="content", text="Task complete.")
                yield StreamEvent(kind="usage", usage=Usage(input_tokens=8000 if final_only else 0, output_tokens=3))
                yield StreamEvent(kind="finish", finish_reason="stop")

    backend = ExecutionBackend()
    env.manager.backend = backend
    # Starts below threshold; usage rather than initial length causes compaction.
    messages = history()
    messages[2]["content"] = "old A " * 400
    messages[4]["content"] = "old B " * 400
    result = await Agent(backend, registry).run(messages, RecordRenderer(), model="main", system="Bounded task", window_runtime=env.manager)
    assert result.text == "Task complete."
    assert backend.calls == (1 if final_only else 2)
    assert sequence == (["summary"] if final_only else ["tool", "summary"])
    assert len(env.events) == 1
    assert {e["compactionId"] for e in states} == {e["compactionId"] for e in env.events}
    assert all(e["status"] == "running" for e in states)
    assert all(e["status"] == "completed" for e in env.events)
    assert len(env.calls) == 1  # Maintenance calls do not recursively invoke the runner.


@pytest.mark.parametrize("summary_failure", [False, True])
async def test_real_agent_summary_then_continue_switches_shared_strategy_without_replaying_tools(continuation_env, monkeypatch, summary_failure):
    from app.tools.agents import AgentTools

    dao, registry, backend, ctx, _ = continuation_env
    cfg = _FakeConfig()
    cfg.agent = cfg.agent.model_copy(update={"keep_recent_messages": 2, "compact_max_retries": 0})
    # Parent session and Web conversation deliberately differ. Strategy is shared by internal chat ID.
    await dao.db.conn.execute(
        "INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title,model,context_strategy,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("real-web-conversation", 123, 123, "strategies", "openai/gpt", "model_summary", 1, 1),
    )
    await dao.db.conn.commit()
    factory = SimpleNamespace(backend_for=lambda label: (backend, "gpt", 2048))
    monkeypatch.setattr(AgentTools, "_context_window_kwargs", lambda self, model: {
        "context_window": 128000, "rollover_trigger_tokens": 20000,
        "context_config": cfg, "context_llm_factory": factory,
    })
    executions, summaries, writes, request_copies = [], [], [], []

    async def write(args):
        writes.append(len(writes) + 1)
        return "recorded execution evidence " * 150
    registry.add("Write", "bounded test write", {"type": "object"}, write)

    async def complete(messages, **kwargs):
        assert validate_model_context(messages)
        if "read_timeout_s" in kwargs:
            summaries.append(copy.deepcopy(messages))
            assert writes == [1, 2, 3]
            if summary_failure:
                raise RuntimeError("test summary upstream unavailable")
            return AgentResult(text=SUMMARY, usage=Usage(input_tokens=900, output_tokens=300))
        executions.append(len(executions) + 1)
        request_copies.append((copy.deepcopy(messages), kwargs))
        if len(executions) <= 3:
            return AgentResult(tool_calls=[ToolCall(f"write-{len(executions)}", "Write", "{}")],
                               usage=Usage(input_tokens=20000 if len(executions) == 3 else 0))
        return AgentResult(text="Verified, no duplicate writes.")
    monkeypatch.setattr(backend, "complete", complete)
    first = await call(registry, "Agent", {"prompt": "Perform three bounded writes; no deployment.", "tools": ["Write"]}, ctx)
    tid, sid = first["task"]["taskId"], first["agentSession"]["agentId"]
    events = await dao.events(tid)
    started = [e for e in events if e.kind == "model_context_compaction_started"]
    assert len(started) == 1 and started[0].detail["strategy"] == "model_summary"
    if summary_failure:
        assert first["status"] == "needs_openbear_control", first
        failed = [e for e in events if e.kind == "model_context_compaction_failed"]
        assert len(failed) == 1 and failed[0].detail["compactionId"] == started[0].detail["compactionId"]
        assert len(executions) == 3 and len(summaries) == 1
        saved = await dao.task_model_context(tid)
        assert "recorded execution evidence" in str(saved["state"]["messages"])
        assert not any(source_of(m).get("kind") == "summary" for m in saved["state"]["messages"])
        return
    assert first["status"] == "completed", first
    completed = [e for e in events if e.kind == "model_context_compaction_completed"]
    assert len(completed) == 1 and completed[0].detail["summary"] == SUMMARY
    assert completed[0].detail["compactionId"] == started[0].detail["compactionId"]
    assert len(executions) == 4 and len(summaries) == 1
    assert "Context summary runtime" in request_copies[-1][1]["system"]
    task = await dao.get_task(tid)
    assert task.model_call_count == 5 and task.tool_call_count == 3
    await dao.db.conn.execute("UPDATE web_conversations SET context_strategy='sliding_window' WHERE internal_chat_id=123")
    await dao.db.conn.commit()
    second = await call(registry, "AgentContinue", {"to": sid, "prompt": "Read retained conclusion only; do not write again.", "tools": []}, ctx)
    assert second["status"] == "completed", second
    assert second["agentSession"]["agentId"] == sid
    retained, options = request_copies[-1]
    assert any(source_of(m).get("kind") == "summary" for m in retained)
    assert "No model generates a new compaction summary" in options["system"]
    assert not options["tools"] or "Write" not in {t["name"] for t in options["tools"]}
    assert writes == [1, 2, 3] and len(summaries) == 1
    assert (await dao.get_task(tid)).output == first["result"]


async def test_managed_agent_summary_refreshes_full_plan_without_resetting_phase_or_grants(plan_env, strategy_env):
    dao, tid, agent = plan_env
    env = strategy_env
    env.cfg.context_management.default_strategy = "model_summary"
    await dao.db.conn.execute(
        "INSERT INTO rath_task_plan_state(task_uuid,phase,active_plan_version,approved_tools_json,row_revision,updated_at) VALUES(?, 'executing', 3, '[\"Read\"]', 7, 1)",
        (tid,),
    )
    await dao.db.conn.commit()
    runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=env.backends["p/main"], model="main",
        model_label="p/main", max_tokens=2048, tools=ToolRegistry(), plan_protocol_enabled=True,
        context_window=128000, rollover_trigger_tokens=16000, context_config=env.cfg, context_llm_factory=env.factory)
    task = await dao.get_task(tid)
    runner.chat_id = task.chat_id
    runner.conversation_uuid = task.parent_session_uuid
    runner._task_instruction = "Only review sources. Keep the existing approved Plan."
    messages = history()
    await runner._append_plan_runtime_update(messages)
    before = dict(await (await dao.db.conn.execute("SELECT * FROM rath_task_plan_state WHERE task_uuid=?", (tid,))).fetchone())
    await runner._prepare_context_window(messages, [], force=True)
    assert validate_model_context(messages)
    assert any(source_of(m).get("kind") == "summary" for m in messages)
    plan_messages = [m for m in messages if (m.get("_openbear_runtime") or {}).get("kind") == "rath_agent_plan_runtime"]
    assert len(plan_messages) == 1
    text = str(plan_messages[0]["content"])
    assert '"phase":"executing"' in text and '"activePlanVersion":3' in text
    assert '"Read"' in text and '"Bash"' not in text
    after = dict(await (await dao.db.conn.execute("SELECT * FROM rath_task_plan_state WHERE task_uuid=?", (tid,))).fetchone())
    assert after == before
    assert len(env.backends["p/first"].calls) == 1
