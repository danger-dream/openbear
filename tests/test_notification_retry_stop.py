"""No live upstream: exercise actual workers, controllers and temporary DBs."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent.loop import Agent
from app.agent.runs import RunRegistry
from app.db.dao import MessageDAO
from app.llm.base import OpenBearLLMError
from app.llm.events import StreamEvent
from app.llm.retry import wait_for_retry
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.base import ToolRegistry
from app.web_console.live_stream import _WebStreamRenderer
from app.web_console.notification_delivery import notification_context_messages
from tests.test_agent_loop import RecordRenderer
from tests.test_agent_window_runtime import env as agent_fixture
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _cfg
from tests.test_web_admin import web_env as web_fixture

web_env = web_fixture
env = agent_fixture


class AlwaysError(FakeStreamBackend):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind

    async def stream(self, messages, **kwargs):
        self.calls += 1
        self.seen_convos.append([dict(m) for m in messages])
        if self.kind == "quota":
            raise OpenBearLLMError("insufficient_quota: Monthly spending limit reached", status=429)
        raise OpenBearLLMError("No available upstream channels for model: gpt-6-astra (ingress=responses)", status=503)
        yield StreamEvent(kind="finish")


async def no_delay(delay_s, **kwargs):
    await wait_for_retry(0, **kwargs)


async def setup_server(web_env, monkeypatch, kind, strategy, *, original=None, task_uuid="finished-task"):
    server = web_env.server
    cfg = _cfg()
    server.config = cfg
    server.runs = RunRegistry()
    backend = AlwaysError(kind)
    server.llm_factory = FakeRunFactory(backend, context_window=128000)
    server.model_selection = SimpleNamespace(current="openai/gpt")
    server.tools = ToolRegistry()

    async def system_prompt():
        return "sys"

    monkeypatch.setattr(server, "_build_system_prompt_for_chat", system_prompt)
    monkeypatch.setattr("app.agent.loop.wait_for_retry", no_delay)
    # Test owns the worker; real recover/claim/persist/run behavior is unchanged.
    monkeypatch.setattr(server, "_ensure_web_task_notification_worker", lambda *a, **kw: None)
    row = await server._create_web_conversation(123, title="persistent upstream failure", model="openai/gpt")
    await web_env.db.conn.execute("UPDATE web_conversations SET context_strategy=? WHERE conversation_uuid=?", (strategy, row["conversation_uuid"]))
    await web_env.db.conn.commit()
    # A background result resumes a real user-owned root, never an empty chat.
    # Persist the source rows and user operation before creating the task's
    # operation/run link. Do not grant provenance by fabricating window metadata.
    original = original if original is not None else [
        {"role": "user", "content": "ORIGINAL_REQUEST: inspect the requested files and summarize the background result; do not deploy."},
        {"role": "assistant", "content": "The inspection is running in the background."},
    ]
    root = "notification-origin"
    live, dao = server._live_for(row), MessageDAO(web_env.db)
    await live.publish({"type": "accepted", "turnUuid": root, "runUuid": "notification-origin-run"})
    for index, message in enumerate(original):
        op_ids = None
        if message["role"] == "user":
            message_uuid = f"{root}-{index}"
            await live.publish({"type": "user", "turnUuid": root, "messageUuid": message_uuid, "text": message["content"]})
            op_ids = [f"msg:{message_uuid}"]
        await server._persist_web_transcript_message(
            dao, row["internal_chat_id"], message["role"], message.get("content") or "",
            conversation_uuid=row["conversation_uuid"], turn_uuid=root, run_root_turn_uuid=root,
            op_ids=op_ids, **{key: message[key] for key in ("tool_calls", "tool_call_id", "name") if key in message},
        )
    await server.rath_dao.create_task(
        chat_id=row["internal_chat_id"], parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="notification-fixture", title="Background inspection", status="completed", task_uuid=task_uuid,
    )
    await live.publish({"type": "tool_progress", "turnUuid": root, "toolCallId": "origin-agent", "name": "Agent",
                        "payload": {"status": "completed", "detached": True, "task": {"taskUuid": task_uuid, "status": "completed"}}})
    await live.publish({"type": "done", "turnUuid": root})
    assert await server._root_turn_for_task_notification(row["conversation_uuid"], {"taskUuid": task_uuid}) == root
    return server, backend, row


async def worker(server, row, queued):
    await asyncio.wait_for(server._run_web_task_notification_when_idle(
        conversation_uuid=row["conversation_uuid"], internal_chat_id=row["internal_chat_id"],
        owner_chat_id=123, payload=queued), timeout=5)


async def stored_notification(db, queued):
    cur = await db.conn.execute("SELECT * FROM web_task_notifications WHERE notification_uuid=?", (queued["_notificationUuid"],))
    return dict(await cur.fetchone())


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
@pytest.mark.parametrize("kind,expected", [("no_channels", 11), ("quota", 1)])
async def test_failed_notification_stops_through_restart_and_manual_continuation(web_env, monkeypatch, kind, expected, strategy):
    server, backend, row = await setup_server(web_env, monkeypatch, kind, strategy)
    payload = {"taskUuid": "finished-task", "status": "completed", "summary": "completed result",
               "content": "UNIQUE_COMPLETED_RESULT: keep the real result; never restart this work."}
    queued = await server._persist_web_task_notification(row, payload)
    await worker(server, row, queued)
    assert backend.calls == expected
    assert all("ORIGINAL_REQUEST" in str(messages) for messages in backend.seen_convos)
    stored = await stored_notification(web_env.db, queued)
    assert stored["state"] == "paused" and stored["attempts"] == 1
    receipt = json.loads(stored["payload_json"])["_contextMessageId"]
    assert receipt > 0

    for reset in (False, True):
        await server._recover_web_task_notifications(reset_processing=reset)
        assert not server._web_task_notification_pending.get(row["conversation_uuid"])
    assert await server._persist_web_task_notification(row, payload) is None
    # Even a stale in-memory duplicate cannot reset the retry budget.
    await worker(server, row, queued)
    assert backend.calls == expected

    # A failed manual continuation pauses again; it must not release the outbox
    # into an automatic retry loop or duplicate the prior result.
    if kind == "quota":
        live = server._live_for(row)
        await live.publish({"type": "accepted", "turnUuid": "manual-still-no-quota"})
        assert await server._run_web_turn(row["internal_chat_id"], "再试一次", _WebStreamRenderer(live),
            conversation=row, root_turn_uuid="manual-still-no-quota") is False
        assert backend.calls == expected + 1
        assert (await stored_notification(web_env.db, queued))["state"] == "paused"

    succeeding = FakeStreamBackend()
    server.llm_factory = FakeRunFactory(succeeding, context_window=128000)
    fresh_row = await server._conversation_row(123, row["conversation_uuid"])
    live = server._live_for(fresh_row)
    await live.publish({"type": "accepted", "turnUuid": "manual-resume"})
    succeeded = await server._run_web_turn(row["internal_chat_id"], "额度恢复了，继续汇总", _WebStreamRenderer(live),
        conversation=fresh_row, root_turn_uuid="manual-resume")
    assert succeeded is True and succeeding.calls == 1
    assert (await stored_notification(web_env.db, queued))["state"] == "delivered"
    cur = await web_env.db.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE chat_id=? AND role='user' AND content LIKE '%UNIQUE_COMPLETED_RESULT%'", (row["internal_chat_id"],))
    assert (await cur.fetchone())["n"] == 1
    assert "UNIQUE_COMPLETED_RESULT" in str(succeeding.seen_convos[0])
    assert server.config.agent.max_retries == 10


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_compression_failure_also_pauses_instead_of_starting_again(web_env, monkeypatch, strategy):
    server, backend, row = await setup_server(web_env, monkeypatch, "quota", strategy, task_uuid="large-result")
    server.config.models.resolve("openai/gpt")[1].rollover_trigger_tokens = 5000
    queued = await server._persist_web_task_notification(row, {"taskUuid": "large-result", "status": "completed",
        "content": "original-large-result " * 10000, "summary": "large result"})
    await worker(server, row, queued)
    assert (await stored_notification(web_env.db, queued))["state"] == "paused"
    failure = (await server._conversation_row(123, row["conversation_uuid"]))["last_error"]
    assert ("required_context_too_large" if strategy == "sliding_window" else "nothing_to_summarize") in failure
    assert backend.calls == 0
    await server._recover_web_task_notifications(reset_processing=True)
    await worker(server, row, queued)
    assert backend.calls == 0
    assert (await stored_notification(web_env.db, queued))["attempts"] == 1


async def test_notification_receipt_replay_batch_and_transaction_rollback(web_env, monkeypatch):
    server, _, row = await setup_server(web_env, monkeypatch, "quota", "sliding_window")
    queued = [await server._persist_web_task_notification(row, {"taskUuid": f"task-{i}", "status": "completed", "content": f"BODY_{i}"}) for i in range(2)]
    await server._claim_web_task_notifications(queued)
    kwargs = dict(chat_id=row["internal_chat_id"], conversation_uuid=row["conversation_uuid"], turn_uuid="t", history=[])
    first = await notification_context_messages(web_env.db, payloads=queued[:1], **kwargs)
    again = await notification_context_messages(web_env.db, payloads=queued, **kwargs)
    assert "BODY_0" not in str(again)
    assert "eventId=message:" in str(again)
    assert "BODY_1" in str(again)
    third = await notification_context_messages(web_env.db, payloads=queued, **{**kwargs, "history": first + again})
    assert third == []
    # A partial DB failure must roll back the new transcript body as well.
    from app.web_console import notification_delivery
    original = notification_delivery.bind_notification_receipts

    async def fail_binding(*args, **kw):
        raise RuntimeError("receipt write failed")

    fresh = await server._persist_web_task_notification(row, {"taskUuid": "rollback", "status": "completed", "content": "ROLLBACK_BODY"})
    await server._claim_web_task_notifications([fresh])
    monkeypatch.setattr(notification_delivery, "bind_notification_receipts", fail_binding)
    with pytest.raises(RuntimeError):
        await notification_context_messages(web_env.db, payloads=[fresh], **kwargs)
    monkeypatch.setattr(notification_delivery, "bind_notification_receipts", original)
    cur = await web_env.db.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE content='ROLLBACK_BODY'")
    assert (await cur.fetchone())["n"] == 0


async def test_agent_compression_does_not_reset_transient_retry_budget(env, monkeypatch):
    db, dao, task_uuid, agent = env

    class MixedErrors(AlwaysError):
        async def stream(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 11:
                raise OpenBearLLMError("maximum context length exceeded", status=400)
            raise OpenBearLLMError("upstream unavailable", status=503)
            yield StreamEvent(kind="finish")

    backend = MixedErrors("no_channels")
    runner = SingleAgentWorkflowRunner(dao, task_uuid, agent=agent, backend=backend,
        model="test", max_tokens=1024, tools=ToolRegistry(), context_window=128000)
    prepare = runner._prepare_context_window

    async def successful_overflow_recovery(*args, **kwargs):
        return True if kwargs.get("force") else await prepare(*args, **kwargs)

    monkeypatch.setattr(runner, "_prepare_context_window", successful_overflow_recovery)
    monkeypatch.setattr("app.rath.single_agent.wait_for_retry", no_delay)
    with pytest.raises(OpenBearLLMError):
        await runner.run()
    assert backend.calls == 12  # first + ten retries + one separate overflow recovery, not 22


async def test_retry_wait_is_not_success_until_model_returns(monkeypatch):
    class Renderer(RecordRenderer):
        def __init__(self):
            super().__init__()
            self.retry_states = []

        async def on_retry_state(self, state):
            self.retry_states.append(dict(state))

    backend = FakeStreamBackend([
        [StreamEvent(kind="error", error="service unavailable", status=503, retryable=True)],
        [StreamEvent(kind="error", error="service unavailable", status=503, retryable=True)],
        [StreamEvent(kind="content", text="recovered"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    monkeypatch.setattr("app.agent.loop.wait_for_retry", no_delay)
    renderer = Renderer()
    result = await Agent(backend, ToolRegistry()).run([{"role": "user", "content": "run"}], renderer, model="test")
    assert result.model_retry == 2
    assert [s.get("status") for s in renderer.retry_states] == [None, "resumed", "failed", None, "resumed", "completed"]
    assert [s["delayMs"] for s in renderer.retry_states if s["active"]] == [3000, 5000]
