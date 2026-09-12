"""Budget the same production payload that real runners hand to their backend.

All counts are offline serializer estimates, not real provider measurements.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agent.loop import Agent
from app.context.runtime import ContextManager
from app.context.store import ContextOwner, WindowStore
from app.context.window import WindowPolicy, estimate_payload, estimate_request
from app.db.dao import MessageDAO
from app.llm.anthropic import AnthropicBackend
from app.llm.base import AgentResult
from app.llm.events import StreamEvent, Usage
from app.llm.openai_chat import OpenAIChatBackend
from app.llm.openai_responses import OpenAIResponsesBackend
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.base import ToolRegistry
from app.web_console.live_stream import _WebLiveStream, _WebStreamRenderer
from tests.test_agent_loop import RecordRenderer
from tests.test_context_strategies import env as shared_strategy_env
from tests.test_context_window import batch, human
from tests.test_rath_single_agent import env as shared_agent_env
from tests.test_web_admin import web_env as web_env

agent_env = shared_agent_env
strategy_env = shared_strategy_env


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("runner_kind", ["controller", "agent_stream", "agent_complete"])
@pytest.mark.parametrize("fast", [False, True])
async def test_runner_budget_options_equal_production_request(agent_env, protocol, runner_kind, fast):
    dao, tid, agent = agent_env
    cls = {"chat": OpenAIChatBackend, "responses": OpenAIResponsesBackend, "anthropic": AnthropicBackend}[protocol]
    serializer = cls(None, "http://not-called.invalid", "test")
    budget, sent = [], []
    class Backend:
        _base = "http://not-called.invalid"
        def build_payload(self, messages, **options):
            payload = serializer.build_payload(messages, **options)
            budget.append((copy.deepcopy(options), copy.deepcopy(payload)))
            return payload
        async def stream(self, messages, **options):
            payload = serializer.build_payload(messages, **options, stream=True)
            sent.append((copy.deepcopy(options), payload))
            assert budget[-1][1] == payload
            yield StreamEvent(kind="content", text="Done")
            yield StreamEvent(kind="finish", finish_reason="stop")
        async def complete(self, messages, **options):
            payload = serializer.build_payload(messages, **options, stream=False)
            sent.append((copy.deepcopy(options), payload))
            assert budget[-1][1] == payload
            return AgentResult(text="Done")
    Backend.protocol = protocol
    backend = Backend()
    if runner_kind == "agent_complete":
        backend.stream = None
    reg = ToolRegistry()
    # Fast payload includes a measurable shape change and a header that must be
    # forwarded unchanged, not written into context JSON or public route data.
    fast_request = {"body": {"service_tier": "priority"}, "headers": {"X-Test-Fast": "yes"}} if fast else {}
    options = {"max_tokens": 8192, "think_level": "high", "session_id": "stable-test-session",
               "service_tier": "priority" if fast else "", "fast_request": fast_request}
    if runner_kind == "controller":
        runtime = ContextManager(WindowStore(dao.db, ContextOwner.controller(chat_id=123, session_uuid="session")),
            WindowPolicy(128000, trigger_tokens=30000, max_output_tokens=8192), backend=backend, model="gpt-test", model_label="p/gpt-test")
        result = await Agent(backend, reg).run([human()], RecordRenderer(), model="gpt-test", system="Stable", window_runtime=runtime, **options)
        assert result.text == "Done"
    else:
        runner = SingleAgentWorkflowRunner(dao, tid, agent=agent, backend=backend, model="gpt-test", model_label="p/gpt-test",
            tools=reg, context_window=128000, rollover_trigger_tokens=30000, plan_protocol_enabled=False, **options)
        result = await runner.run()
        assert result["summary"] == "Done"
        runtime = runner._get_window_runtime()
    assert len(sent) == 1
    request_options, payload = sent[0]
    for key, value in options.items():
        assert request_options[key] == value
    assert budget[0][0]["think_level"] == "high"
    assert budget[0][0]["stream"] == (runner_kind != "agent_complete")
    if protocol == "responses":
        assert payload["include"] == ["reasoning.encrypted_content"] and payload["store"] is False
    if fast:
        assert payload["service_tier"] == "priority"
    saved = await runtime.store.load()
    assert saved["state"]["requestModelLabel"] == "p/gpt-test"
    assert "X-Test-Fast" not in str(saved)


@pytest.mark.parametrize("third_high", [False, True])
async def test_real_web_anthropic_mode_switch_uses_matching_estimate_and_keeps_signed_history(web_env, monkeypatch, third_high):
    env = web_env
    requests = []
    class Backend(AnthropicBackend):
        async def stream(self, messages, **options):
            payload = self.build_payload(messages, **options)
            tokens = estimate_payload(payload).tokens
            requests.append({"tokens": tokens, "payload": copy.deepcopy(payload), "messages": copy.deepcopy(messages), "options": options})
            if len(requests) == 1:
                yield StreamEvent(kind="reasoning", text="SIGNED_REASONING " * 1400, signature="synthetic-signature")
            yield StreamEvent(kind="content", text="DONE")
            yield StreamEvent(kind="usage", usage=Usage(input_tokens=tokens, output_tokens=5))
            yield StreamEvent(kind="finish", finish_reason="stop")
    backend = Backend(None, "http://not-called.invalid", "test")
    model = env.server.config.models.providers["openai"].models[0]
    model.thinking_levels = ["off", "high"]
    model.default_thinking_level = "high"
    model.rollover_trigger_tokens = 8000
    env.server.config.models.providers["openai"].protocol = "anthropic"
    env.server.tools = ToolRegistry()
    env.server.llm_factory = SimpleNamespace(backend_for=lambda _: (backend, "claude-test", 8192), context_window=lambda _: 128000)
    async def system():
        return "Only answer the current user."
    monkeypatch.setattr(env.server, "_build_system_prompt_for_chat", system)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    chat, uuid = row["internal_chat_id"], row["conversation_uuid"]
    dao = MessageDAO(env.db)
    live = _WebLiveStream(uuid, chat)
    turns = [("high", "first"), ("off", "second small" if third_high else "second " + "current user material " * 400)]
    if third_high:
        turns.append(("high", "third " + "current user material " * 400))
    rotations_after_off = None
    for i, (mode, prompt) in enumerate(turns):
        await dao.set_thinking_level(chat, mode)
        await live.publish({"type": "accepted", "turnUuid": f"root-{i}"})
        assert await env.server._run_web_turn(chat, prompt, _WebStreamRenderer(live), conversation=row, root_turn_uuid=f"root-{i}")
        if mode == "off":
            rotations_after_off = (await (await env.db.conn.execute("SELECT COUNT(*) FROM context_window_rotations")).fetchone())[0]
    assert rotations_after_off == 0, "off must not falsely include signed reasoning in its budget"
    assert all(r["tokens"] < 8000 for r in requests), "no stale off calibration may underbudget the next high payload"
    assert "synthetic-signature" not in str(requests[1]["payload"])
    # Disabling thinking affects the provider payload, not the durable source.
    assert any(m.get("signature") == "synthetic-signature" for m in requests[1]["messages"])
    if third_high:
        assert (await (await env.db.conn.execute("SELECT COUNT(*) FROM context_window_rotations")).fetchone())[0] > 0
    assert all(r["options"]["max_tokens"] == 8192 for r in requests)


async def test_mode_fingerprint_invalidates_only_calibration_not_compatible_native_items(strategy_env):
    e = strategy_env
    backend = OpenAIResponsesBackend(None, "http://not-called.invalid", "test")
    e.manager.backend = backend
    e.manager.policy = WindowPolicy(128000, trigger_tokens=30000, max_output_tokens=8192)
    messages = [human(), *batch(1)]
    messages[1]["native_output_items"] = [
        {"type": "reasoning", "id": "rs-test", "encrypted_content": "opaque-valid-history"},
        {"type": "function_call", "id": "fc-test", "call_id": "t1", "name": "Read", "arguments": "{}", "status": "completed"},
    ]
    opts = {"think_level": "high", "session_id": "same", "native_continuation": True,
            "max_tokens": 8192, "stream": True, "fast_request": {}}
    prepared = await e.manager.prepare(messages, system="Stable", tools=[], request_options=opts)
    old_route = e.manager.route
    await e.manager.begin_request()
    await e.manager.observe_usage(Usage(input_tokens=15000))
    off = {**opts, "think_level": "off", "fast_request": {"body": {"service_tier": "priority"}, "headers": {"X-Test": "secret-like-value"}}}
    prepared = await e.manager.prepare(prepared, system="Stable", tools=[], request_options=off)
    assert e.manager.route != old_route
    assert any(m.get("native_output_items") for m in prepared)
    assert not (await e.store.load())["usage_known"]
    expected = estimate_request(prepared, system=e.manager.system, tools=[], backend=backend, model="main", request_options=off).tokens
    assert e.manager.last_estimate.tokens == expected
    saved = await e.store.load()
    assert "secret-like-value" not in str(saved)
    # Mutating the caller's options cannot change a frozen request signature.
    off["fast_request"]["body"]["service_tier"] = "changed"
    assert e.manager.request_options["fast_request"]["body"]["service_tier"] == "priority"
