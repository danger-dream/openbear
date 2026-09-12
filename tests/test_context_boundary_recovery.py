"""Real runner regression for compression failure/cancel and durable controls.

No live upstream: every request/result is deterministic, every DB is temporary.
"""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.context.runtime import ContextManager
from app.context.store import ContextOwner, StaleWindow, WindowStore
from app.context.strategies import ModelSummaryStrategy
from app.context.window import WindowPolicy, mark_source, source_of
from app.db.dao import MessageDAO
from app.llm.base import AgentResult
from app.llm.events import StreamEvent, ToolCall, Usage
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.base import ToolRegistry
from app.web_console.live_stream import _WebLiveStream, _WebStreamRenderer
from tests.test_context_strategies import SUMMARY
from tests.test_context_strategies import env as shared_strategy_env
from tests.test_context_window import batch, human
from tests.test_rath_single_agent import env as shared_agent_env
from tests.test_web_admin import FakeRunFactory
from tests.test_web_admin import web_env as web_env

agent_env = shared_agent_env
strategy_env = shared_strategy_env


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("tool_text", [False, True])
async def test_web_summary_failure_preserves_complete_tool_checkpoint_and_next_request(web_env, monkeypatch, cancel, tool_text):
    env = web_env
    cfg = env.server.config
    cfg.agent.compact_max_retries = 0
    cfg.agent.keep_recent_messages = 2
    cfg.models.compression_models = ["openai/cheap"]
    cfg.models.providers["openai"].models[0].rollover_trigger_tokens = 20000
    reg = ToolRegistry()
    calls, effects, states = [], [], []
    entered = asyncio.Event()
    row = await env.server._create_web_conversation(123, run_config={"context_strategy": "model_summary"})
    chat, uuid = row["internal_chat_id"], row["conversation_uuid"]
    dao = MessageDAO(env.db)
    await dao.add(chat, "user", "Old completed work", turn_uuid="old")
    for i in range(4):
        await dao.add(chat, "assistant", "old read", tool_calls=[ToolCall(f"old-{i}", "Read", "{}")], turn_uuid="old")
        await dao.add(chat, "tool", "old evidence " * 300, tool_call_id=f"old-{i}", name="Read", turn_uuid="old")
    async def read(args):
        effects.append(args["i"])
        return f"CONFIRMED_{args['i']}"
    reg.add("Read", "Isolated read", {"type": "object"}, read)
    env.server.tools = reg
    class Backend:
        protocol = "chat"
        async def stream(self, messages, **kwargs):
            calls.append(copy.deepcopy(messages))
            if len(calls) == 1:
                if tool_text:
                    yield StreamEvent(kind="content", text="Reading two records")
                yield StreamEvent(kind="tool_call", tool_calls=[ToolCall("one", "Read", '{"i":1}'), ToolCall("two", "Read", '{"i":2}')])
                yield StreamEvent(kind="usage", usage=Usage(input_tokens=20000))
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
            else:
                yield StreamEvent(kind="content", text="Done, no replay")
                yield StreamEvent(kind="finish", finish_reason="stop")
        async def complete(self, messages, **kwargs):
            assert effects == [1, 2]
            store = WindowStore(env.db, ContextOwner.controller(chat_id=chat, session_uuid=await dao.get_or_create_session_uuid(chat)))
            before = await store.restore_messages()
            assert "CONFIRMED_1" in str(before) and "CONFIRMED_2" in str(before)
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("summary unavailable")
    env.server.llm_factory = FakeRunFactory(Backend(), context_window=128000)
    async def system():
        return "Read only. No deployment."
    monkeypatch.setattr(env.server, "_build_system_prompt_for_chat", system)
    async def sink(event):
        states.append(copy.deepcopy(event))
        return event
    live = _WebLiveStream(uuid, chat, event_sink=sink)
    await live.publish({"type": "accepted", "turnUuid": "current"})
    task = asyncio.create_task(env.server._run_web_turn(chat, "Read two records only.", _WebStreamRenderer(live), conversation=row, root_turn_uuid="current"))
    try:
        if cancel:
            await asyncio.wait_for(entered.wait(), 10)
            task.cancel()
        assert not await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    compactions = [e for e in states if e.get("type") == "context_compaction"]
    assert compactions[-1]["status"] == ("cancelled" if cancel else "failed")
    await env.db.close()
    await env.db.connect()
    await env.db.conn.execute("UPDATE web_conversations SET context_strategy='sliding_window' WHERE conversation_uuid=?", (uuid,))
    await env.db.conn.commit()
    await live.publish({"type": "accepted", "turnUuid": "next"})
    assert await env.server._run_web_turn(chat, "Report saved results, do not repeat reads.", _WebStreamRenderer(live), conversation=row, root_turn_uuid="next")
    assert len(calls) == 2 and effects == [1, 2]
    assert "CONFIRMED_1" in str(calls[1]) and "CONFIRMED_2" in str(calls[1])
    assert live.snapshot()["status"] == "idle"


@pytest.mark.parametrize("cancel", [False, True])
async def test_agent_final_response_is_checkpointed_before_summary_and_exposed_on_failure(agent_env, strategy_env, cancel):
    dao, tid, agent = agent_env
    e = strategy_env
    e.cfg.context_management.default_strategy = "model_summary"
    entered = asyncio.Event()
    class Backend:
        protocol = "chat"
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls <= 3:
                return AgentResult(tool_calls=[ToolCall(f"read{self.calls}", "Read", "{}")])
            return AgentResult(text="FINAL_RESPONSE: verified all records, no deployment.", usage=Usage(input_tokens=20000))
    backend = Backend()
    reg = ToolRegistry()
    effects = []
    async def read(args):
        effects.append(1)
        return "raw evidence " * 100
    reg.add("Read", "read", {"type": "object"}, read)
    runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=backend, model="main", model_label="p/main",
        max_tokens=1024, tools=reg, plan_protocol_enabled=False, context_window=128000, rollover_trigger_tokens=20000,
        context_config=e.cfg, context_llm_factory=e.factory)
    async def summary_gate():
        saved = await dao.task_model_context(tid)
        assert saved["state"]["stage"] == "final_response_received"
        assert "FINAL_RESPONSE" in str(saved["state"]["messages"])
        assert "FINAL_RESPONSE" in str(await runner._get_window_runtime().store.restore_messages())
        entered.set()
        if cancel:
            await asyncio.Event().wait()
    for b in e.backends.values():
        b.gate = summary_gate
        b.output = RuntimeError("summary unavailable")
    task = asyncio.create_task(runner.run())
    try:
        if cancel:
            await asyncio.wait_for(entered.wait(), 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            output = await task
            assert output["status"] == "needs_openbear_control" and output["continuable"] is False
            assert output["reason"] == "agent_context_compression_failed"
            response = output["detail"]["finalResponse"]
            assert response["text"].startswith("FINAL_RESPONSE")
            assert (await dao.get_task(tid)).output["detail"]["finalResponse"] == response
            original = await runner._get_window_runtime().store.event_payload(response["eventId"])
            assert original["payload"]["content"] == response["text"]
            with pytest.raises(RuntimeError, match="agent_task_not_continuable"):
                await runner.run_continue("not permitted")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert effects == [1, 1, 1] and backend.calls == 4


@pytest.mark.parametrize("legacy_null", [False, True])
async def test_archive_only_accepts_empty_pure_tool_assistant_equivalence(strategy_env, legacy_null):
    store = strategy_env.store
    messages = [human(), *batch(1)]
    messages[1]["content"] = None if legacy_null else ""
    await store.archive(messages)
    same = copy.deepcopy(messages)
    same[1]["content"] = "" if legacy_null else None
    await store.archive(same)
    for changed in ["new text", [{"type": "text", "text": ""}]]:
        bad = copy.deepcopy(same)
        bad[1]["content"] = changed
        with pytest.raises(StaleWindow):
            await store.archive(bad)
    bad = copy.deepcopy(same)
    bad[1]["tool_calls"][0].arguments = '{"different":true}'
    with pytest.raises(StaleWindow):
        await store.archive(bad)
    original = mark_source({"role": "assistant", "content": None})
    await store.archive([original])
    with pytest.raises(StaleWindow):
        await store.archive([{**original, "content": ""}])


async def test_control_delivery_rolls_back_with_checkpoint_and_retries_once(agent_env, monkeypatch):
    dao, tid, agent = agent_env
    runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=SimpleNamespace(protocol="chat"), model="main", max_tokens=1024)
    messages = [human("Task original")]
    await runner._checkpoint_model_context(messages, round_no=0, stage="before_model")
    cid = await dao.add_control(tid, "steer", message="CONTROL: no more tools.")
    save = dao.save_task_model_context
    async def fail(*args, **kwargs):
        raise RuntimeError("checkpoint write failure")
    monkeypatch.setattr(dao, "save_task_model_context", fail)
    with pytest.raises(RuntimeError, match="checkpoint write failure"):
        await runner._checkpoint_and_append_steers("window_pre_model", messages)
    assert (await dao.control(cid)).status == "pending"
    assert "CONTROL:" not in str((await dao.task_model_context(tid))["state"])
    assert "CONTROL:" not in str(await runner._get_window_runtime().store.restore_messages())
    monkeypatch.setattr(dao, "save_task_model_context", save)
    await runner._checkpoint_model_context(messages, round_no=0, stage="before_model")
    state = (await dao.task_model_context(tid))["state"]
    window = await runner._get_window_runtime().store.load()
    assert (await dao.control(cid)).status == "applied"
    assert state["pendingControlUuids"] == [cid] and "CONTROL:" in str(state["messages"])
    assert state["windowRevision"] == window["revision"] and state["windowVersion"] == window["window_version"]


async def test_same_task_resume_uses_newer_window_and_rebuilds_unacked_controls(agent_env):
    dao, tid, agent = agent_env
    reg = ToolRegistry()
    calls, effects = [], []
    async def read(args):
        effects.append(1)
        return "must not run before acknowledgement"
    reg.add("Read", "read", {"type": "object"}, read)
    reg.add("AgentControlAck", "ack", {"type": "object"}, lambda _: "unused")
    class Backend:
        protocol = "chat"
        async def complete(self, messages, **kwargs):
            calls.append(copy.deepcopy(messages))
            return AgentResult(tool_calls=[ToolCall("read", "Read", "{}")])
    runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=Backend(), model="main", max_tokens=1024,
        tools=reg, model_call_limit=1, plan_protocol_enabled=False)
    runner.chat_id = 123
    await runner._checkpoint_model_context([human("Task original"), *batch(1, text="EVICTED_OLD_FACT")], round_no=0, stage="before_model")
    # Simulates the independently committed rotation before process termination.
    await runner._get_window_runtime().checkpoint([human("Task original"), mark_source({"role": "assistant", "content": "CURRENT_WINDOW"})])
    cid = await dao.add_control(tid, "steer", message="LEGACY_CONTROL: do not read before ack.")
    await dao.mark_control(cid, "applied")
    await dao.update_task(tid, status="needs_openbear_control")
    await dao.db.close()
    await dao.db.connect()
    result = await runner.run_continue("continue without restoring old context")
    assert result["status"] == "needs_openbear_control"
    assert len(calls) == 1 and not effects
    assert "CURRENT_WINDOW" in str(calls[0]) and "EVICTED_OLD_FACT" not in str(calls[0])
    assert "LEGACY_CONTROL:" in str(calls[0]) and cid in str(calls[0])


async def test_control_received_during_summary_survives_failed_rebudget(agent_env, strategy_env):
    dao, tid, agent = agent_env
    e = strategy_env
    e.cfg.context_management.default_strategy = "model_summary"
    reg = ToolRegistry()
    calls = []
    async def read(args):
        return "large optional tool output " * 1500
    reg.add("Read", "isolated", {"type": "object"}, read)
    reg.add("AgentControlAck", "ack", {"type": "object"}, lambda _: "unused")
    class Backend:
        protocol = "chat"
        async def complete(self, messages, **kwargs):
            calls.append(copy.deepcopy(messages))
            assert len(calls) < 10
            return AgentResult(tool_calls=[ToolCall(f"r{len(calls)}", "Read", "{}")])
    runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=Backend(), model="main", model_label="p/main",
        max_tokens=1024, tools=reg, plan_protocol_enabled=False, context_window=128000, rollover_trigger_tokens=20000,
        context_config=e.cfg, context_llm_factory=e.factory)
    control_ids = []
    async def first_candidate():
        if not control_ids:
            control_ids.append(await dao.add_control(tid, "steer", message="REVOKED_READ: acknowledge before tools. " + "约束材料。" * 14000))
        else:
            raise RuntimeError("next summary fails")
    e.backends["p/first"].gate = first_candidate
    for label in ["p/second", "p/main"]:
        e.backends[label].output = RuntimeError("fallback unavailable")
    output = await runner.run()
    assert output["reason"] == "agent_context_compression_failed" and output["continuable"] is False
    state = (await dao.task_model_context(tid))["state"]
    window = await runner._get_window_runtime().store.load()
    assert control_ids and state["pendingControlUuids"] == control_ids
    assert "REVOKED_READ:" in str(state["messages"]) and "REVOKED_READ:" in str(window["state"]["messages"])
    assert state["windowRevision"] == window["revision"] and state["windowVersion"] == window["window_version"]
    assert (await dao.control(control_ids[0])).status == "applied"
    assert any(source_of(m).get("kind") == "summary" for m in state["messages"])
    count = len(calls)
    await dao.db.close()
    await dao.db.connect()
    with pytest.raises(RuntimeError, match="agent_task_not_continuable"):
        await runner.run_continue("must not weaken the public guard")
    assert len(calls) == count


async def test_internal_resume_rejects_mismatched_pending_tool_checkpoint(agent_env):
    dao, tid, agent = agent_env
    runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=SimpleNamespace(protocol="chat"), model="main", max_tokens=1024)
    messages = [human()]
    await runner._checkpoint_model_context(messages, round_no=0, stage="budget_boundary:tool",
        extra_state={"pendingToolCalls": [{"id": "uncertain", "name": "Read", "arguments": "{}"}]})
    await runner._get_window_runtime().checkpoint(messages)
    await dao.update_task(tid, status="needs_openbear_control")
    with pytest.raises(RuntimeError, match="agent_continuation_checkpoint_mismatch"):
        await runner.run_continue("do not replay uncertain calls")


async def test_final_summary_does_not_bypass_control_arriving_after_response(agent_env, strategy_env):
    dao, tid, agent = agent_env
    e = strategy_env
    e.cfg.context_management.default_strategy = "model_summary"
    reg = ToolRegistry()
    ids, calls, acks = [], [], []
    async def read(args):
        return "raw evidence " * 100
    async def ack(args):
        assert (await dao.control(ids[0])).status == "applied"
        assert await dao.mark_control_response(ids[0], response_status="accepted")
        acks.append(ids[0])
        return json.dumps({"ok": True, "controlUuid": ids[0]})
    reg.add("Read", "read", {"type": "object"}, read)
    reg.add("AgentControlAck", "ack", {"type": "object"}, ack)
    class Backend:
        protocol = "chat"
        async def complete(self, messages, **kwargs):
            calls.append(copy.deepcopy(messages))
            n = len(calls)
            if n <= 3:
                return AgentResult(tool_calls=[ToolCall(f"r{n}", "Read", "{}")])
            if n == 4:
                return AgentResult(text="Candidate final response", usage=Usage(input_tokens=20000))
            if n == 5:
                assert "LATEST_CONTROL" in str(messages)
                return AgentResult(tool_calls=[ToolCall("ack", "AgentControlAck", json.dumps({"controlUuid": ids[0], "status": "accepted"}))])
            assert acks == ids
            return AgentResult(text="Final after current control acknowledgement")
    async def gate():
        assert len(calls) == 4
        ids.append(await dao.add_control(tid, "steer", message="LATEST_CONTROL: acknowledge before finishing."))
    e.backends["p/first"].gate = gate
    runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=Backend(), model="main", model_label="p/main",
        max_tokens=1024, tools=reg, plan_protocol_enabled=False, context_window=128000, rollover_trigger_tokens=20000,
        context_config=e.cfg, context_llm_factory=e.factory)
    output = await runner.run()
    assert output["summary"] == "Final after current control acknowledgement"
    assert len(calls) == 6 and acks == ids
