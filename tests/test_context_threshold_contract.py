"""Configured input thresholds are independent of model output capabilities."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.context.store import ContextOwner, WindowStore
from app.context.window import InputEstimate, WindowPolicy
from app.llm.base import AgentResult
from app.llm.events import StreamEvent, Usage
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.base import ToolRegistry
from app.web_admin import _WebLiveStream, _WebStreamRenderer
from tests.test_context_strategies import env as shared_strategy_env
from tests.test_context_strategies import history
from tests.test_context_window import human
from tests.test_rath_single_agent import env as shared_agent_env
from tests.test_web_admin import web_env as shared_web_env
from tests.test_web_context_strategies import setup_manual

strategy_env = shared_strategy_env
agent_env = shared_agent_env
web_env = shared_web_env


@pytest.mark.parametrize("output_limit", [0, 8192, 128000, 500000, 750000])
@pytest.mark.parametrize("explicit", [0, 250000])
def test_output_capability_never_deducts_from_configured_threshold(output_limit, explicit):
    policy = WindowPolicy(500000, trigger_tokens=explicit, max_output_tokens=output_limit)
    expected = explicit or 350000
    assert policy.threshold == expected
    assert policy.target() == int(expected * .15)
    assert policy.max_output_tokens == output_limit


def test_ratio_has_no_hidden_minimum_output_or_two_percent_reserve():
    policy = WindowPolicy(1000, trigger_ratio=.9, max_output_tokens=1000)
    assert policy.threshold == 900
    assert policy.target() == 135


@pytest.mark.parametrize("scope", ["controller", "agent"])
@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
@pytest.mark.parametrize("explicit", [0, 250000])
async def test_new_context_does_not_compress_due_to_large_output_limit(
    strategy_env, monkeypatch, scope, strategy, explicit,
):
    env = strategy_env
    if scope == "agent":
        env.manager.store = WindowStore(env.db, ContextOwner.agent(
            task_uuid="task", agent_session_uuid="agent", conversation_uuid="conversation",
        ))
    env.manager.policy = WindowPolicy(500000, trigger_tokens=explicit, max_output_tokens=500000)
    env.selected["strategy"] = strategy
    monkeypatch.setattr("app.context.runtime.estimate_request", lambda *args, **kwargs: InputEstimate(23792))
    original = [human("帮我看看 ontology 是什么")]
    prepared = await env.manager.prepare(original, system="Fixed system and tool definitions", tools=[])
    assert prepared == original
    assert env.manager.last_estimate.tokens == 23792
    rotations = await env.db.conn.execute("SELECT COUNT(*) FROM context_window_rotations")
    assert (await rotations.fetchone())[0] == 0
    assert not env.events and not env.calls
    assert all(not backend.calls for backend in env.backends.values())
    threshold = explicit or 350000
    await env.manager.begin_request()
    await env.manager.observe_usage(Usage(input_tokens=threshold - 1))
    assert not env.manager.pending
    await env.manager.begin_request()
    await env.manager.observe_usage(Usage(input_tokens=threshold))
    assert env.manager.pending


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_compression_still_runs_at_exact_configured_input_threshold(strategy_env, strategy):
    env = strategy_env
    env.manager.policy = WindowPolicy(128000, trigger_tokens=8000, max_output_tokens=128000)
    env.selected["strategy"] = strategy
    await env.manager.prepare(history(), system="Fixed policy", tools=[])
    assert env.events[-1]["strategy"] == strategy
    assert env.events[-1]["rolloverTriggerTokens"] == 8000
    assert env.events[-1]["beforeEstimateTokens"] >= 8000
    assert env.events[-1]["afterEstimateTokens"] < 8000
    assert env.manager.policy.max_output_tokens == 128000
    assert len(env.calls) == (1 if strategy == "model_summary" else 0)


class RecordingBackend:
    protocol = "chat"

    def __init__(self):
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), kwargs))
        return AgentResult(text="The requested explanation.", usage=Usage(input_tokens=23792, output_tokens=10))

    async def stream(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), kwargs))
        yield StreamEvent(kind="content", text="The requested explanation.")
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=23792, output_tokens=10))
        yield StreamEvent(kind="finish", finish_reason="stop")


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_web_first_request_reaches_model_with_unchanged_output_limit(web_env, monkeypatch, strategy):
    env = web_env
    model = env.server.config.models.providers["openai"].models[0]
    model.context_window = model.max_tokens = 500000
    model.rollover_trigger_tokens = 250000
    backend = RecordingBackend()
    env.server.llm_factory = SimpleNamespace(
        backend_for=lambda label: (backend, label.split("/")[1], 500000),
        context_window=lambda label: 500000,
    )
    env.server.tools = ToolRegistry()

    async def system():
        return "Fixed policy. " * 4000

    monkeypatch.setattr(env.server, "_build_system_prompt_for_chat", system)
    row = await env.server._create_web_conversation(123, model="openai/gpt", run_config={"context_strategy": strategy})
    chat, uuid = row["internal_chat_id"], row["conversation_uuid"]
    live = _WebLiveStream(uuid, chat)
    await live.publish({"type": "accepted", "turnUuid": "first-root"})
    assert await env.server._run_web_turn(
        chat, "帮我看看 ontology 是什么", _WebStreamRenderer(live), conversation=row, root_turn_uuid="first-root",
    ) is True
    assert len(backend.calls) == 1
    assert backend.calls[0][1]["max_tokens"] == 500000
    assert "ontology" in str(backend.calls[0][0])
    operations = await env.server._web_operations(uuid)
    assert not any(op["opType"] == "context_compaction" for op in operations)


async def test_agent_first_request_keeps_model_output_limit(agent_env):
    dao, task_uuid, agent = agent_env
    backend = RecordingBackend()
    runner = SingleAgentWorkflowRunner(
        dao, task_uuid, agent=agent, backend=backend, model="gpt", max_tokens=500000,
        tools=ToolRegistry(), context_window=500000, rollover_trigger_tokens=250000,
        plan_protocol_enabled=False,
    )
    result = await runner.run()
    assert result["summary"] == "The requested explanation."
    assert len(backend.calls) == 1
    assert backend.calls[0][1]["max_tokens"] == 500000
    assert runner.window_policy.threshold == 250000
    rotations = await dao._db.conn.execute("SELECT COUNT(*) FROM context_window_rotations")
    assert (await rotations.fetchone())[0] == 0


async def test_manual_summary_uses_input_threshold_not_executing_output_capacity(web_env):
    row, backend, store = await setup_manual(web_env)
    web_env.server.llm_factory = SimpleNamespace(
        backend_for=lambda label: (backend, label.split("/")[1], 500000),
        context_window=lambda label: 500000,
    )
    response = await web_env.client.post(f"/api/conversations/{row['conversation_uuid']}/compact")
    assert response.status == 200, await response.text()
    outcome = (await response.json())["outcome"]
    assert outcome["rolloverTriggerTokens"] == 8000
    assert len(backend.calls) == 1
    assert backend.calls[0][1]["max_tokens"] == web_env.server.config.agent.compact_max_tokens
    assert (await store.load())["window_version"] == 1
