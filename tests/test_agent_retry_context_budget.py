"""Actual Agent requests must budget their entire request-local recovery view."""
from __future__ import annotations

import copy

import pytest

from app.agent.native_continuation import validate_model_context
from app.context.window import estimate_request
from app.llm.events import StreamEvent, ToolCall
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.base import ToolRegistry
from tests.test_agent_window_runtime import env as agent_fixture
from tests.test_context_strategies import env as strategy_fixture
from tests.test_context_window import batch, human

agent_env = agent_fixture
strategy_env = strategy_fixture


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
@pytest.mark.parametrize("case", ["too_large", "fits", "shrink"])
async def test_retry_full_request_obeys_threshold_and_preserves_partial(agent_env, strategy_env, monkeypatch, strategy, case):
    _, dao, tid, agent = agent_env
    cfg = strategy_env.cfg
    cfg.context_management.default_strategy = strategy
    cfg.agent.keep_recent_messages = 2
    cfg.agent.compact_max_retries = 0
    partial = "partial output " * (4000 if case == "too_large" else 1200 if case == "shrink" else 50)
    requests = []
    writes = []
    tools = ToolRegistry()

    async def write(_args):
        writes.append("one effect")
        return "written once"

    tools.add("Write", "one write", {"type": "object"}, write)

    class Backend:
        protocol = "chat"

        async def stream(self, messages, **kwargs):
            window = runner._get_window_runtime()
            actual = estimate_request(messages, system=kwargs["system"], tools=kwargs.get("tools") or [],
                                      backend=self, model=kwargs["model"], max_tokens=kwargs["max_tokens"]).tokens
            requests.append(copy.deepcopy(messages))
            assert actual == window.last_estimate.tokens
            assert actual < window.policy.threshold == 8000
            assert validate_model_context(messages)
            # Verify real effects are not replayed when recovering a later request.
            if case == "fits" and len(requests) == 1:
                yield StreamEvent(kind="tool_call", tool_calls=[ToolCall("write-once", "Write", "{}")])
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
            elif len(requests) == (2 if case == "fits" else 1):
                yield StreamEvent(kind="content", text=partial)
                yield StreamEvent(kind="error", error="upstream interrupted", status=503, retryable=True)
            else:
                assert sum(m.get("content") == partial for m in messages) == 1
                assert "exact interruption point" in str(messages)
                yield StreamEvent(kind="content", text="finished")
                yield StreamEvent(kind="finish", finish_reason="stop")

    backend = Backend()
    runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=backend, model="main", model_label="p/main",
        max_tokens=16384, tools=tools, context_window=128000, rollover_trigger_tokens=8000,
        max_retries=1, retry_backoff_s=0, retry_max_delay_s=0,
        context_config=cfg, context_llm_factory=strategy_env.factory)
    if case == "shrink":
        async def old_context():
            return [human("Keep the task and do not deploy."),
                    *batch(1, text="old evidence " * 650), *batch(2, text="old evidence " * 650),
                    *batch(3, text="recent C"), *batch(4, text="recent D")]
        monkeypatch.setattr(runner, "_round_context_messages", old_context)
    output = await runner.run()
    window = runner._get_window_runtime()
    assert runner.retry_policy.max_retries == 1
    assert window.policy.threshold == 8000
    if case == "too_large":
        assert len(requests) == 1
        assert output["status"] == "needs_openbear_control"
        assert output["detail"]["instructionsPreserved"]
        refs = output["detail"]["partialOutputEventIds"]
        assert len(refs) == 1
        assert partial in str(await window.store.event_payload(refs[0]))
        assert partial not in str(await window.store.restore_messages())
    else:
        assert len(requests) == (3 if case == "fits" else 2)
        assert output["summary"] == partial + "finished"
        assert writes == (["one effect"] if case == "fits" else [])
        if case == "shrink":
            assert window.window_version > 0
            assert "old evidence " * 650 in str(requests[0])
            assert "old evidence " * 650 not in str(requests[1])
    # Recovery instructions are request-local and must not become permanent task pins.
    assert "exact interruption point" not in str(await window.store.restore_messages())


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
async def test_budget_includes_reasoning_progress_and_role_bridges(agent_env, protocol):
    _, dao, tid, agent = agent_env
    class Backend:
        pass
    backend = Backend()
    backend.protocol = protocol
    runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=backend, model="main",
        max_tokens=1024, tools=ToolRegistry(), context_window=128000, rollover_trigger_tokens=8000)
    # Establish the route first; adopting a new route intentionally strips old
    # native reasoning. This test targets later requests on the same route.
    await runner._prepare_context_window([], [])
    messages = [human("Task"), {"role": "assistant", "content": "answer", "reasoning": "progress " * 200},
                human("Followup", source_id="followup"), human("Another instruction", source_id="next")]
    await runner._prepare_context_window(messages, [])
    actual = runner._context_request_view(messages)
    window = runner._get_window_runtime()
    assert window.last_estimate.tokens == estimate_request(actual, system=window.system, tools=[], backend=backend,
                                                           model="main", max_tokens=1024).tokens
    assert "[Task-local reasoning/progress" in str(actual)
    assert "[Task-local reasoning/progress" not in str(messages)
