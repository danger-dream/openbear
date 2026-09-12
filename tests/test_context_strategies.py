"""Dual-strategy acceptance with real persistence and protocol boundaries; no browser/live DB."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from app.agent.native_continuation import validate_model_context
from app.config import Config
from app.context.configuration import conversation_strategy, migrate_context_config
from app.context.prompts import (
    SUMMARY_SYSTEM_POLICY,
    WINDOW_SYSTEM_POLICY,
    effective_context_prompt,
)
from app.context.runtime import ContextManager
from app.context.store import ContextOwner, StaleWindow, WindowStore
from app.context.strategies import CompressionRequest, ContextCompressionError, ModelSummaryStrategy
from app.context.summary_prompt import _REQUIRED_SECTIONS
from app.context.window import (
    InputEstimate,
    RequiredContextTooLarge,
    WindowPolicy,
    mark_source,
    source_of,
)
from app.db.dao import SummaryDAO
from app.db.engine import DB
from app.llm.base import AgentResult
from app.llm.events import Usage
from tests.test_context_window import batch, human

SUMMARY = "\n\n".join(f"{heading}\nExact task: review A–F; no deployment. First finding saved to report." for heading in _REQUIRED_SECTIONS)


class Backend:
    protocol = "chat"

    def __init__(self, output=SUMMARY, gate=None):
        self.output, self.gate, self.calls = output, gate, []

    async def complete(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), kwargs))
        if self.gate:
            await self.gate()
        if isinstance(self.output, Exception):
            raise self.output
        return AgentResult(text=self.output, usage=Usage(input_tokens=321, output_tokens=99))


@pytest.fixture
async def env(tmp_path):
    cfg = Config.model_validate({
        "telegram": {"botToken": "test"}, "models": {"primary": "p/main", "compressionModels": ["p/first", "p/second"],
        "providers": {"p": {"baseUrl": "http://not-called.invalid", "apiKey": "test", "protocol": "chat", "models": [{"id": x} for x in ["main", "first", "second"]]}}},
        "agent": {"compactMaxRetries": 0, "keepRecentMessages": 2, "compactMaxTokens": 2048},
        "memory": {},
    })
    db = DB(str(tmp_path / "dual.db"))
    await db.connect()
    backends = {key: Backend() for key in ["p/first", "p/second", "p/main"]}
    factory = SimpleNamespace(backend_for=lambda label: (backends[label], label.split("/")[1], 8192))
    store = WindowStore(db, ContextOwner.controller(chat_id=7, session_uuid="session", conversation_uuid="conversation"))
    events, calls = [], []
    async def event(detail):
        events.append(detail)
    async def account(detail):
        calls.append(detail)
    selected = {"strategy": "sliding_window"}
    async def resolve():
        return selected["strategy"]
    manager = ContextManager(store, WindowPolicy(128000, trigger_tokens=8000), backend=backends["p/main"], model="main",
        strategy_resolver=resolve, strategies={"model_summary": ModelSummaryStrategy(cfg, factory, "p/main", on_model_call=account)},
        on_rotated=event)
    yield SimpleNamespace(cfg=cfg, db=db, backends=backends, factory=factory, store=store, manager=manager,
                          selected=selected, events=events, calls=calls)
    await db.close()


def history():
    return [human("Review A–F. Do not deploy."), *batch(1, text="old A evidence " * 2400),
            *batch(2, text="old B evidence " * 2400), *batch(3, text="recent C evidence"),
            *batch(4, text="recent D evidence")]


async def test_window_then_summary_then_window_preserves_state_and_originals(env):
    first = await env.manager.prepare(history(), system="Task system", tools=[])
    assert not env.calls
    assert len(first) < len(history())
    assert env.events[-1]["strategy"] == "sliding_window"
    env.selected["strategy"] = "model_summary"
    second = await env.manager.prepare(first + batch(5, text="new E evidence " * 3000) + batch(6, text="recent F"), system="Task system", tools=[], force=True)
    assert validate_model_context(second)
    assert len(env.calls) == 1
    summary = await SummaryDAO(env.db).latest(7)
    assert summary and summary["summary"] == SUMMARY
    assert env.events[-1]["summaryId"] == summary["id"]
    assert env.events[-1]["usage"]["inputTokens"] == 321
    assert env.events[-1]["estimateOnly"] is True
    assert env.manager.last_estimate.tokens < 8000
    assert SUMMARY_SYSTEM_POLICY.strip() in env.manager.system
    assert WINDOW_SYSTEM_POLICY.strip() not in env.manager.system
    env.selected["strategy"] = "sliding_window"
    third = await env.manager.prepare(second + batch(7) + batch(8), system=env.manager.system, tools=[], force=True)
    assert any(source_of(m).get("kind") == "summary" for m in third)
    assert len(env.calls) == 1
    assert WINDOW_SYSTEM_POLICY.strip() in env.manager.system
    assert SUMMARY_SYSTEM_POLICY.strip() not in env.manager.system
    assert await env.store.restore_messages() == third
    old = await env.store.index(query="old A evidence")
    assert old["events"]
    assert "old A evidence" in str(await env.store.event_payload(old["events"][0]["event_id"]))


async def test_candidate_order_quality_retry_fallback_and_original_parameters(env):
    env.cfg.agent.compact_max_retries = 1
    env.cfg.agent.compact_timeout_s = 47
    env.cfg.agent.compact_prompt = "Existing={existing}\nHistory={history}"
    env.backends["p/first"].output = RuntimeError("upstream unavailable")
    env.backends["p/second"].output = "Missing mandatory sections"
    env.selected["strategy"] = "model_summary"
    await env.manager.prepare(history(), system="", tools=[], force=True)
    assert [call["model"] for call in env.calls] == ["p/first", "p/first", "p/second", "p/second", "p/main"]
    options = env.backends["p/main"].calls[0][1]
    assert options["max_tokens"] == 2048 and options["read_timeout_s"] == 47
    assert env.backends["p/main"].calls[0][0][0]["content"].startswith("Existing=")
    assert env.events[-1]["compressionModel"] == "p/main"


async def test_all_candidates_fail_leaves_active_context_usage_and_no_summary(env):
    original = [human(), *batch(1), *batch(2)]
    await env.manager.prepare(original, system="", tools=[])
    before = await env.store.load()
    for backend in env.backends.values():
        backend.output = RuntimeError("failure")
    env.selected["strategy"] = "model_summary"
    with pytest.raises(ContextCompressionError, match="original_context_preserved"):
        await env.manager.prepare(original, system="", tools=[], force=True)
    after = await env.store.load()
    assert after["state"] == before["state"] and after["window_version"] == before["window_version"]
    assert await SummaryDAO(env.db).latest(7) is None
    assert not env.events


async def test_accounting_failure_never_spends_again(env):
    async def fail(_detail):
        raise RuntimeError("ledger unavailable")
    env.manager.strategies["model_summary"].on_model_call = fail
    env.selected["strategy"] = "model_summary"
    with pytest.raises(ContextCompressionError, match="accounting_failed"):
        await env.manager.prepare(history(), system="", tools=[], force=True)
    assert len(env.backends["p/first"].calls) == 1
    assert not env.backends["p/second"].calls and not env.backends["p/main"].calls
    assert await SummaryDAO(env.db).latest(7) is None


async def test_switch_during_summary_only_affects_next_boundary(env):
    async def switch():
        env.selected["strategy"] = "sliding_window"
    env.backends["p/first"].gate = switch
    env.selected["strategy"] = "model_summary"
    result = await env.manager.prepare(history(), system="", tools=[], force=True)
    assert env.events[-1]["strategy"] == "model_summary"
    await env.manager.prepare(result, system="", tools=[])
    assert env.manager.active_strategy == "sliding_window"
    assert len(env.calls) == 1


async def test_concurrent_source_change_rejects_summary_without_overwriting_new_context(env):
    original = history()
    await env.manager.checkpoint(original)
    async def newer_checkpoint():
        await env.manager.checkpoint(original + [human("New instruction: stop.", source_id="new-instruction")])
    env.backends["p/first"].gate = newer_checkpoint
    env.selected["strategy"] = "model_summary"
    with pytest.raises(StaleWindow):
        await env.manager.prepare(original, system="", tools=[], force=True)
    assert "New instruction: stop." in str(await env.store.restore_messages())
    assert await SummaryDAO(env.db).latest(7) is None


async def test_usage_judgement_is_immediate_but_unclosed_batch_cannot_compress(env):
    original = [human(), *batch(1), *batch(2)]
    await env.manager.prepare(original, system="", tools=[])
    ticket = await env.manager.begin_request()
    await env.manager.observe_usage(Usage(input_tokens=8000), ticket=ticket)
    assert env.manager.pending and not env.events
    with pytest.raises(ValueError, match="closed_tool_batch"):
        await env.manager.prepare(original + batch(3)[:1], system="", tools=[])
    assert env.manager.pending and not env.events


async def test_required_input_cannot_cross_configured_threshold_even_with_large_model(env):
    env.manager.policy = WindowPolicy(1_000_000, trigger_tokens=100)
    with pytest.raises(RequiredContextTooLarge) as caught:
        await env.manager.prepare([human("Must preserve " * 1000)], system="", tools=[])
    assert caught.value.input_ceiling == 99
    assert await SummaryDAO(env.db).latest(7) is None
    assert not env.calls


def test_configuration_migration_preserves_original_summary_settings_and_removes_only_reminder():
    raw = {"agent": {"compactRatio": .6, "keepRecentMessages": 9, "compactPrompt": "custom",
            "compactMaxTokens": 2048, "compactMaxRetries": 3, "compactTimeoutS": 90,
            "manualCompactMinPercent": 12, "memoryReminderPercent": 80, "memoryReminderPrompt": "retired"},
           "models": {"compressionModels": ["a/one", "b/two"]}, "contextManagement": {"triggerRatio": .7}}
    assert migrate_context_config(raw)
    assert raw["agent"]["compactRatio"] == .6
    assert raw["agent"]["compactPrompt"] == "custom" and raw["agent"]["compactTimeoutS"] == 90
    assert raw["models"]["compressionModels"] == ["a/one", "b/two"]
    assert not any("memoryReminder" in key for key in raw["agent"])
    assert not migrate_context_config(raw)


def test_strategy_prompt_is_idempotent_and_bidirectional():
    prompt = "Custom unmodified user rules."
    window = effective_context_prompt(prompt)
    summary = effective_context_prompt(window, "model_summary")
    assert effective_context_prompt(summary, "model_summary") == summary
    assert effective_context_prompt(summary, "sliding_window").count(WINDOW_SYSTEM_POLICY.strip()) == 1
    assert "Custom unmodified user rules." in summary


async def test_summary_keeps_original_decisions_antecedents_and_whole_tool_batches(env):
    proposal = mark_source({"role": "assistant", "content": "Edit source only; do not deploy."})
    decision = batch(20, name="UserInteraction", text='{"confirmed":false,"status":"feedback","text":"Pause deployment; source edits only."}')
    messages = [human(), proposal, human("yes", "u2"), *decision, *batch(21), *batch(22), *batch(23)]
    env.selected["strategy"] = "model_summary"
    result = await env.manager.prepare(messages, system="", tools=[], force=True)
    assert validate_model_context(result)
    assert proposal in result and decision[0] in result and decision[1] in result
    assert any(m.get("content") == "yes" for m in result)
    assert not any(m.get("tool_call_id") == "t21" for m in result)


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_model_usage_threshold_falls_back_to_ratio_only_without_explicit_value(env, strategy):
    env.manager.policy = WindowPolicy(32000, trigger_ratio=.25)
    env.selected["strategy"] = strategy
    original = [human(), *batch(1, text="old evidence " * 1000), *batch(2), *batch(3)]
    await env.manager.prepare(original, system="", tools=[])
    await env.manager.begin_request()
    await env.manager.observe_usage(Usage(input_tokens=4000, cache_read_tokens=3999))
    assert not env.manager.pending
    await env.manager.begin_request()  # A completed request's usage is immutable.
    await env.manager.observe_usage(Usage(input_tokens=4000, cache_read_tokens=4000))
    assert env.manager.pending and not env.events
    result = await env.manager.prepare(original, system="", tools=[])
    assert env.events[-1]["strategy"] == strategy and validate_model_context(result)
    assert env.manager.policy.threshold == 8000
    env.manager.policy = WindowPolicy(32000, trigger_tokens=12000, trigger_ratio=.25)
    await env.manager.begin_request()
    await env.manager.observe_usage(Usage(input_tokens=8000))
    assert not env.manager.pending and env.manager.policy.threshold == 12000


async def test_summary_larger_than_threshold_rolls_back_without_silent_window_fallback(env):
    original = [human(), *batch(1), *batch(2)]
    await env.manager.prepare(original, system="", tools=[])
    before = await env.store.load()
    env.backends["p/first"].output = SUMMARY + " oversized summary" * 10000
    env.selected["strategy"] = "model_summary"
    with pytest.raises(RequiredContextTooLarge):
        await env.manager.prepare(original, system="", tools=[], force=True)
    assert (await env.store.load())["state"] == before["state"]
    assert await SummaryDAO(env.db).latest(7) is None
    assert len(env.calls) == 1 and not env.events


def test_frozen_window_only_prompt_becomes_strategy_correct_in_both_directions():
    original = "Custom constraint: no production changes. No summary model or forced memory-writing checkpoint is used."
    summary = effective_context_prompt(original, "model_summary")
    assert "No summary model" not in summary and SUMMARY_SYSTEM_POLICY.strip() in summary
    window = effective_context_prompt(summary, "sliding_window")
    assert SUMMARY_SYSTEM_POLICY.strip() not in window and WINDOW_SYSTEM_POLICY.strip() in window
    assert "Custom constraint: no production changes." in window
    assert effective_context_prompt(window, "model_summary") == summary
