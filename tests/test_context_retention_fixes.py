"""Regression for accidental lifetime pins and stale Agent runtime snapshots."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from app.context.strategies import ContextCompressionError, ModelSummaryStrategy
from app.context.window import (
    InputEstimate,
    WindowPolicy,
    estimate_request,
    mark_source,
    select_window,
    source_of,
)
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.base import ToolRegistry
from tests.test_agent_window_runtime import env as shared_agent_env
from tests.test_context_strategies import env as shared_strategy_env
from tests.test_context_window import batch, human

agent_env = shared_agent_env
strategy_env = shared_strategy_env


def control(index, args, result=None):
    group = batch(index, name="OpenBearControl", text=json.dumps(result or {"status": "ok"}))
    group[0]["tool_calls"][0].arguments = json.dumps(args)
    return group


@pytest.mark.parametrize("args", [{"action": action} for action in ["status", "models", "mcp_status", "skills_status", "think"]])
def test_control_queries_no_longer_pin_whole_history(args):
    messages = [human("Only investigate; do not restart.")]
    for i in range(12):
        messages += control(i, args, {"status": "ok", "info": "model information " * 100})
    messages += batch(100) + batch(101)
    before = copy.deepcopy(messages)
    selected = select_window(messages, estimate=lambda items: estimate_request(items, system="", tools=[]),
                             target=1000, input_ceiling=20000)
    assert selected.estimate.tokens <= 1000
    assert sum(m.get("name") == "OpenBearControl" for m in selected.messages) < 12
    assert selected.messages[0] == messages[0]
    assert messages == before


@pytest.mark.parametrize("args,result", [
    ({"action": "restart"}, {"status": "scheduled", "when": "after_current_turn"}),
    ({"action": "stop"}, {"status": "ok", "softStopCurrentRun": True}),
    ({"action": "new"}, {"status": "cancelled", "confirmation": {"status": "timeout", "confirmed": False}}),
    ({"action": "mcp_reload"}, {"status": "cancelled", "confirmation": {"confirmed": False, "text": "Do not restart servers"}}),
    ({"action": "status"}, {"status": "cancelled", "confirmation": {"confirmed": False, "text": "Keep my conditions"}}),
    ({"action": "think", "args": {"level": "high"}}, {"status": "ok"}),
    ({"action": "future_unknown_action"}, {"status": "ok"}),
])
def test_actual_control_feedback_and_legacy_gated_calls_stay_pinned(args, result):
    messages = [human(), *control(1, args, result), *batch(2), *batch(3)]
    # Dict form is the restored checkpoint form, not only a live ToolCall.
    call = messages[1]["tool_calls"][0]
    messages[1]["tool_calls"][0] = {"id": call.id, "name": call.name, "arguments": call.arguments}
    selected = select_window(messages, estimate=lambda items: InputEstimate(len(items) * 10), target=30, latest_batches=1)
    assert messages[1:3] == selected.messages[1:3]


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_both_strategies_evict_queries_but_preserve_decisions(strategy_env, strategy):
    env = strategy_env
    env.selected["strategy"] = strategy
    messages = [human(), *control(1, {"action": "status"}, {"info": "old status " * 2000}),
                *batch(2, name="UserInteraction", text='{"confirmed":false,"text":"Do not deploy"}'),
                *control(3, {"action": "restart"}, {"status": "cancelled", "confirmation": {"text": "Stop"}}),
                *batch(4), *batch(5)]
    selected = await env.manager.prepare(messages, system="", tools=[], force=True)
    assert not any(m.get("tool_call_id") == "t1" for m in selected)
    assert all(any(m.get("tool_call_id") == call_id for m in selected) for call_id in ["t2", "t3"])
    assert await env.store.event_payload(source_of(messages[2])["id"])


def make_runner(env, managed=False):
    _, dao, task, agent = env
    agent.tool_allowlist = ["Read"]
    registry = ToolRegistry()
    for name in ["Read", "Write", "AgentHistory", "AgentControlAck"]:
        registry.add(name, name, {"type": "object"}, lambda _: "unused")
    return SingleAgentWorkflowRunner(dao, task, agent=agent, backend=SimpleNamespace(protocol="chat"),
        model="isolated", max_tokens=1024, tools=registry, plan_protocol_enabled=managed,
        context_window=128000, rollover_trigger_tokens=30000)


def runtime_messages(messages, kind):
    return [m for m in messages if m.get("_openbear_runtime", {}).get("kind") == kind]


async def test_capability_A_B_A_compares_latest_real_schema_and_keeps_prefix(agent_env):
    runner = make_runner(agent_env)
    messages = []
    schemas = await runner._allowed_tool_schemas()
    runner._append_capability_state(messages, schemas)
    original = copy.deepcopy(messages)
    runner._pending_control_acks.add("control-1")
    runner._append_capability_state(messages, await runner._allowed_tool_schemas())
    assert "AgentControlAck" in json.loads(messages[-1]["_openbear_runtime"]["capabilitySignature"])["effectiveTools"]
    runner._pending_control_acks.clear()
    schemas = await runner._allowed_tool_schemas()
    runner._append_capability_state(messages, schemas)
    assert len(messages) == 3
    assert "AgentControlAck" not in json.loads(messages[-1]["_openbear_runtime"]["capabilitySignature"])["effectiveTools"]
    runner._append_capability_state(messages, schemas)
    assert len(messages) == 3
    assert messages[:1] == original


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_repeated_agent_windows_only_keep_current_runtime(agent_env, strategy_env, strategy):
    runner = make_runner(agent_env)
    window = runner._get_window_runtime()
    window.policy = WindowPolicy(128000, trigger_tokens=8000)
    async def selected_strategy():
        return strategy
    window.strategy_resolver = selected_strategy
    window.strategies["model_summary"] = ModelSummaryStrategy(strategy_env.cfg, strategy_env.factory, "p/main")
    messages = [human("Literal agent_completion_gate is user text; do not remove."),
                mark_source({"role": "user", "content": "Actual control: do not deploy.",
                             "_openbear_runtime": {"kind": "agent_control"}}, kind="control")]
    runner._append_capability_state(messages, [{"name": "Read"}])
    runner._append_capability_state(messages, [{"name": "Write"}])
    for i in range(8):
        # Exercise upgrading a previously misclassified, already-saved gate too.
        messages += [mark_source({"role": "user", "content": f"OLD GATE {i}",
                                  "_openbear_runtime": {"kind": "agent_completion_gate"}}, kind="task")]
    messages += batch(80) + batch(81)
    await window.checkpoint(messages)
    for n in range(3):
        runner._pending_control_acks = {"control-1"} if n < 2 else set()
        messages = await window.prepare(messages + batch(90 + n, text="obsolete payload " * 1000) + batch(100 + n),
            system="", tools=await runner._allowed_tool_schemas(), force=True,
            refresh_after_rotation=runner._fresh_window_runtime_state)
        capabilities = runtime_messages(messages, "agent_capabilities")
        gates = runtime_messages(messages, "agent_completion_gate")
        assert len(capabilities) == 1
        assert len(gates) == (1 if n < 2 else 0)
        if gates:
            assert "AgentControlAck" in gates[0]["content"]
            assert source_of(gates[0])["kind"] == "runtime"
        assert "OLD GATE" not in str(messages)
        assert "Actual control: do not deploy." in str(messages)
        assert "Literal agent_completion_gate is user text" in str(messages)
        assert await window.store.restore_messages() == messages


@pytest.mark.parametrize("phase,expected", [("drafting", "AgentPlanSubmit"), ("executing", "finalize"), ("finalizing", "")])
async def test_refresh_regenerates_only_effective_plan_completion_gate(agent_env, monkeypatch, phase, expected):
    runner = make_runner(agent_env, managed=True)
    async def runtime():
        runner._plan_runtime = {"phase": phase, "approvedTools": ["Read"]}
        return runner._plan_runtime
    monkeypatch.setattr(runner, "_refresh_plan_runtime", runtime)
    messages = [{"role": "user", "content": "obsolete drafting reminder",
                 "_openbear_runtime": {"kind": "agent_completion_gate"}}]
    fresh = await runner._fresh_window_runtime_state(messages)
    gates = runtime_messages(fresh, "agent_completion_gate")
    assert len(gates) == bool(expected)
    if expected:
        assert expected in gates[0]["content"]
    runner._get_window_runtime().bind_sources(fresh)
    assert all(source_of(m)["kind"] == "runtime" for m in gates)
    assert "obsolete drafting reminder" not in str(fresh)


async def test_failed_agent_summary_does_not_replace_runtime_or_control_history(agent_env, strategy_env):
    runner = make_runner(agent_env)
    window = runner._get_window_runtime()
    async def strategy():
        return "model_summary"
    window.strategy_resolver = strategy
    window.strategies["model_summary"] = ModelSummaryStrategy(strategy_env.cfg, strategy_env.factory, "p/main")
    for backend in strategy_env.backends.values():
        backend.output = RuntimeError("unavailable")
    messages = [human("Do not deploy"), *batch(1), *batch(2), *batch(3)]
    runner._append_capability_state(messages, [{"name": "Read"}])
    runner._append_capability_state(messages, [{"name": "Write"}])
    await window.checkpoint(messages)
    original = copy.deepcopy(messages)
    before = await window.store.load()
    with pytest.raises(ContextCompressionError):
        await window.prepare(messages, system="", tools=[], force=True,
                             refresh_after_rotation=runner._fresh_window_runtime_state)
    assert messages == original
    assert await window.store.restore_messages() == original
    assert (await window.store.load())["window_version"] == before["window_version"]
