"""Model switching must not lend a frozen request's usage to another model."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agent.runs import RunRegistry
from app.db.dao import MessageDAO
from app.llm.base import AgentResult
from app.llm.events import StreamEvent, Usage
from app.models.selection import ModelSelection
from app.tools.base import ToolRegistry
from app.web_admin import _WebStreamRenderer
from tests.test_context_strategies import SUMMARY
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as shared_web_env

web_env = shared_web_env


@pytest.mark.parametrize("timing", ["running", "idle"])
@pytest.mark.parametrize("switches", [("cheap",), ("cheap", "gpt"), ("gpt",)])
async def test_model_switch_usage_identity_and_manual_gate(web_env, monkeypatch, tmp_path, timing, switches):
    env = web_env
    cookie = {"openbear_web_session": await _login_cookie(env)}
    env.server.runs = RunRegistry()
    env.server.model_selection = ModelSelection(env.server.config.models, tmp_path / "unused-model-selection.json")
    models = env.server.config.models.providers["openai"].models
    models[0].rollover_trigger_tokens = 64000
    models[1].rollover_trigger_tokens = 8000
    env.server.config.agent.keep_recent_messages = 2
    env.server.config.agent.compact_max_retries = 0
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Backend:
        protocol = "chat"

        async def stream(self, messages, **options):
            calls.append(("execute", options["model"]))
            if len(calls) == 1:
                entered.set()
                await release.wait()
            yield StreamEvent(kind="content", text="The requested work is complete.")
            tokens = 9000 if options["model"] == "gpt" else 5000
            yield StreamEvent(kind="usage", usage=Usage(input_tokens=tokens, output_tokens=8))
            yield StreamEvent(kind="finish", finish_reason="stop")

        async def complete(self, messages, **options):
            calls.append(("summary", options["model"]))
            return AgentResult(text=SUMMARY, usage=Usage(input_tokens=1000, output_tokens=80))

    backend = Backend()
    env.server.llm_factory = SimpleNamespace(
        backend_for=lambda label: (backend, label.split("/", 1)[1], 8192),
        context_window=lambda _: 128000,
    )
    env.server.tools = ToolRegistry()

    async def system(**kwargs):
        return "Only follow the user's actual instructions."

    monkeypatch.setattr(env.server, "_build_system_prompt_for_chat", system)
    row = await env.server._create_web_conversation(
        123, model="openai/gpt", run_config={"context_strategy": "model_summary"},
    )
    chat, conv = row["internal_chat_id"], row["conversation_uuid"]
    dao = MessageDAO(env.db)
    await dao.add(chat, "user", "old request", conversation_uuid=conv, turn_uuid="old", run_root_turn_uuid="old")
    await dao.add(chat, "assistant", "old result", conversation_uuid=conv, turn_uuid="old", run_root_turn_uuid="old")
    live = env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "first"})
    task = asyncio.create_task(env.server._run_web_turn(
        chat, "finish this", _WebStreamRenderer(live), conversation=row, root_turn_uuid="first",
    ))
    env.server.runs.register(chat, task)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if timing == "idle":
            release.set()
            assert await asyncio.wait_for(task, 10) is True
        for target in switches:
            response = await env.client.post(
                f"/api/conversations/{conv}/model", cookies=cookie,
                json={"model": f"openai/{target}"},
            )
            body = await response.json()
            assert response.status == 200, body
            assert body["nextRun"] is (timing == "running")
    finally:
        release.set()
    assert await asyncio.wait_for(task, 10) is True

    sid = await dao.current_session_uuid(chat)
    label = f"openai/{switches[-1]}"
    # An idle real model change invalidates the old window immediately. A
    # next-run switch back to the unchanged in-flight model need not discard it.
    known = switches[-1] == "gpt" and (timing == "running" or switches == ("gpt",))
    expected = 9000 if known else None
    assert await dao.latest_controller_context_usage(chat, session_uuid=sid, expected_model=label) == expected
    state = await (await env.client.get(f"/api/conversations/{conv}/state", cookies=cookie)).json()
    assert state["contextUsage"]["known"] is known
    assert state["contextUsage"]["tokens"] == (expected or 0)
    # Historical billing still belongs to A, even when the next model is B.
    ledger = await dao.recent_model_calls(chat)
    assert len(ledger) == 1
    assert ledger[0].model == "openai/gpt"
    assert ledger[0].input_tokens == 9000
    compact = await env.client.post(f"/api/conversations/{conv}/compact", cookies=cookie)
    body = await compact.json()
    assert compact.status == 409, body
    assert body["error"] == ("below_threshold" if known else "context_usage_unknown")
    assert calls == [("execute", "gpt")]

    current = await env.server._conversation_row(123, conv, require=True)
    await live.publish({"type": "accepted", "turnUuid": "second"})
    assert await env.server._run_web_turn(
        chat, "A fresh request for the selected model.", _WebStreamRenderer(live),
        conversation=current, root_turn_uuid="second",
    ) is True
    current_tokens = 5000 if switches[-1] == "cheap" else 9000
    assert await dao.latest_controller_context_usage(chat, session_uuid=sid, expected_model=label) == current_tokens
    if switches[-1] == "cheap":
        compact = await env.client.post(f"/api/conversations/{conv}/compact", cookies=cookie)
        assert compact.status == 200, await compact.json()
        assert calls == [("execute", "gpt"), ("execute", "cheap"), ("summary", "cheap")]
        assert await dao.latest_controller_context_usage(chat, session_uuid=sid, expected_model=label) is None


async def test_legacy_usage_checks_latest_request_model_without_searching_back(web_env):
    row = await web_env.server._create_web_conversation(123, model="openai/gpt")
    chat = row["internal_chat_id"]
    dao = MessageDAO(web_env.db)
    sid = await dao.get_or_create_session_uuid(chat)
    for label, tokens in [("openai/gpt", 100), ("openai/cheap", 200)]:
        await web_env.server._persist_web_model_call_delta(
            dao, chat, session_uuid=sid,
            call={"status": "ok", "usage": Usage(input_tokens=tokens), "promptUsageReported": True},
            model_cost={}, model_label=label, protocol="chat", think_level="off",
        )
    assert await dao.latest_controller_context_usage(chat, session_uuid=sid, expected_model="openai/cheap") == 200
    assert await dao.latest_controller_context_usage(chat, session_uuid=sid, expected_model="openai/gpt") is None
    # A legacy snapshot has no model identity; do not infer one from the ledger.
    await dao.set_controller_context_usage(chat, session_uuid=sid, tokens=999)
    assert await dao.latest_controller_context_usage(chat, session_uuid=sid) == 999
    assert await dao.latest_controller_context_usage(chat, session_uuid=sid, expected_model="openai/cheap") is None


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("confirmed_fast", [False, True])
async def test_manual_budget_uses_frozen_execution_mode(web_env, monkeypatch, protocol, confirmed_fast):
    import copy

    from app.config import FastRequestConfig
    from app.context.runtime import ContextManager
    from tests.test_web_context_strategies import setup_manual

    row, backend, store = await setup_manual(web_env)
    provider = web_env.server.config.models.providers["openai"]
    provider.protocol = protocol
    backend.protocol = protocol
    model = provider.models[0]
    model.thinking_levels = ["off", "high"]
    model.supports_fast = True
    if confirmed_fast:
        model.fast_request = FastRequestConfig(body={"service_tier": "priority"}, headers={"x-speed": "fast"})
    else:
        model.fast_request = None
    dao = MessageDAO(web_env.db)
    chat, conv = row["internal_chat_id"], row["conversation_uuid"]
    await dao.set_thinking_level(chat, "high")
    await dao.set_fast_mode(chat, True)
    seen = []
    original_prepare = ContextManager.prepare

    async def capture(manager, messages, **options):
        seen.append(copy.deepcopy(options["request_options"]))
        return await original_prepare(manager, messages, **options)

    monkeypatch.setattr(ContextManager, "prepare", capture)
    response = await web_env.client.post(f"/api/conversations/{conv}/compact")
    assert response.status == 200, await response.text()
    assert len(seen) == 1
    sid = await dao.current_session_uuid(chat)
    expected = {
        "max_tokens": 8192, "think_level": "high", "session_id": sid,
        "service_tier": "" if confirmed_fast else ("fast" if protocol == "anthropic" else "priority"),
        "fast_request": model.fast_request.model_dump(mode="json") if confirmed_fast else {"body": {}, "headers": {}},
    }
    if protocol == "responses":
        expected["native_continuation"] = True
    assert seen[0] == expected
    # Summary execution keeps its separate existing contract: no main-model
    # thinking or Fast flags are accidentally imposed on the compression call.
    assert len(backend.calls) == 1
    assert "think_level" not in backend.calls[0][1]
    assert not backend.calls[0][1].get("fast_request")
    assert (await store.load())["state"]["requestModelLabel"] == "openai/gpt"
