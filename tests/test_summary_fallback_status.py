"""Recovered summary failures remain billed but do not fail the root turn."""
from __future__ import annotations

import pytest

from app.agent.result import RunResult
from app.context.runtime import ContextManager
from app.context.store import ContextOwner, WindowStore
from app.context.window import WindowPolicy
from app.db.dao import MessageDAO
from app.llm.events import StreamEvent, Usage
from app.turn_stats import build_turn_stats_card
from tests.test_context_strategies import Backend, history
from tests.test_notification_retry_stop import setup_server, stored_notification, worker
from tests.test_notification_retry_stop import web_env as web_fixture
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend

web_env = web_fixture


@pytest.mark.parametrize("case", ["pre_request", "after_final", "execution_failure", "all_summary_fail"])
async def test_summary_fallback_preserves_actual_turn_status_and_failure_ledger(web_env, monkeypatch, case):
    server, _, row = await setup_server(web_env, monkeypatch, "no_channels", "model_summary")
    config = server.config
    model = config.models.resolve("openai/gpt")[1]
    model.rollover_trigger_tokens = 8000
    model.context_window = 128000
    config.agent.compact_max_retries = 0
    config.agent.keep_recent_messages = 2
    config.models.providers["openai"].models.extend([
        model.model_copy(update={"id": "broken-summary"}),
        model.model_copy(update={"id": "good-summary"}),
    ])
    config.models.compression_models = ["openai/broken-summary", "openai/good-summary"]
    events = ([StreamEvent(kind="error", error="insufficient_quota", status=429, retryable=False)]
              if case == "execution_failure" else [StreamEvent(kind="content", text="Task complete."),
              StreamEvent(kind="usage", usage=Usage(input_tokens=8000 if case == "after_final" else 100, output_tokens=4)),
              StreamEvent(kind="finish", finish_reason="stop")])
    main = FakeStreamBackend(scripts=[events])
    broken, good = Backend(RuntimeError("summary provider unavailable")), Backend()
    if case == "all_summary_fail":
        good.output = RuntimeError("backup unavailable")
    factory = FakeRunFactory(main, context_window=128000)
    original_backend_for = factory.backend_for

    def route(label):
        if label == "openai/broken-summary":
            return broken, "broken-summary", 8192
        if label == "openai/good-summary":
            return good, "good-summary", 8192
        return original_backend_for(label)

    if case == "all_summary_fail":
        async def fail_fallback(*args, **kwargs):
            main.complete_calls += 1
            raise RuntimeError("last fallback unavailable")
        main.complete = fail_fallback
    factory.backend_for = route
    server.llm_factory = factory
    dao = MessageDAO(web_env.db)
    sid = await dao.get_or_create_session_uuid(row["internal_chat_id"])
    store = WindowStore(web_env.db, ContextOwner.controller(chat_id=row["internal_chat_id"], session_uuid=sid,
                                                          conversation_uuid=row["conversation_uuid"]))
    manager = ContextManager(store, WindowPolicy(128000, trigger_tokens=8000), backend=main, model="fake-gpt")
    original = history()
    if case == "after_final":
        original[2]["content"] = "old A " * 400
        original[4]["content"] = "old B " * 400
    await manager.checkpoint(original)
    queued = await server._persist_web_task_notification(row, {"taskUuid": "finished-task", "status": "completed",
        "summary": "result", "content": "A task finished successfully; report its result."})
    await worker(server, row, queued)
    fresh = await server._conversation_row(123, row["conversation_uuid"])
    stored = await stored_notification(web_env.db, queued)
    ops = await server._web_operations(row["conversation_uuid"])
    summary_states = [op["payload"].get("status") for op in ops if op["opType"] == "context_compaction"]
    assert len(broken.calls) == len(good.calls) == 1
    assert main.calls == (0 if case == "all_summary_fail" else 1)
    failed = case in {"execution_failure", "all_summary_fail"}
    assert fresh["status"] == ("error" if failed else "idle")
    assert stored["state"] == ("paused" if failed else "delivered")
    assert ("failed" if case == "all_summary_fail" else "completed") in summary_states
    calls = await dao.recent_model_calls(row["internal_chat_id"])
    assert len(calls) == 3
    errors = [call for call in calls if call.status != "ok"]
    assert len(errors) == (3 if case == "all_summary_fail" else 2 if case == "execution_failure" else 1)
    assert any(call.model == "openai/broken-summary" and call.call_kind == "context_compaction" for call in errors)
    if case != "all_summary_fail":
        stats = [op["payload"] for op in ops if op["opType"] == "stats"][-1]
        assert stats["modelFail"] == len(errors)
        assert stats["modelCalls"] == 3
    if failed:
        # Actual terminal failures must remain paused even through worker recovery.
        await server._recover_web_task_notifications(reset_processing=True)
        assert not server._web_task_notification_pending.get(row["conversation_uuid"])


def test_summary_failure_footer_does_not_require_a_terminal_failure():
    result = RunResult(model_calls=3, model_ok=2, summary_model_fail=1)
    assert result.model_fail == 0
    assert "失败 1" in build_turn_stats_card(result)
