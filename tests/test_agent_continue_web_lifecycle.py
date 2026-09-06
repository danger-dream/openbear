"""Regression for an AgentContinue source id becoming an immortal task card."""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from app.agent import steering
from app.agent.runs import RunRegistry
from app.llm.events import StreamEvent
from app.tools.base import ToolRegistry
from app.web_operations import tool_event_operation_specs
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _login_cookie
from tests.test_web_admin import web_env as _web_env

web_env = _web_env


@pytest.mark.parametrize("source", ["instance-id", "previous-task-id"])
@pytest.mark.parametrize("nested", [False, True])
def test_continue_uses_new_task_identity_only(source, nested):
    args = json.dumps({"to": source, "prompt": "next round", "tools": []})
    common = dict(name="AgentContinue", arguments=args, tool_call_id="continue-call", turn_uuid="root")
    start = tool_event_operation_specs("tool_start", **common)[-1]
    assert start["op_id"] == "agent:continue-call"
    assert start["payload"]["taskUuid"] == ""
    result = {"ok": True, "status": "completed"}
    result.update({"task": {"taskUuid": "new-task", "status": "completed"}} if nested else {"taskUuid": "new-task"})
    specs = tool_event_operation_specs("tool_result", result=json.dumps(result), **common)
    assert specs[0]["payload"]["mergedTo"] == "agent:new-task"
    assert specs[-1]["op_id"] == "agent:new-task"
    assert specs[-1]["status"] == "completed"
    assert specs[-1]["lifecycle"] == "terminal"
    assert all(spec["op_id"] != f"agent:{source}" for spec in specs)


def test_continue_rejection_does_not_mutate_blocking_task():
    specs = tool_event_operation_specs(
        "tool_result", name="AgentContinue", tool_call_id="failed-call",
        arguments=json.dumps({"to": "instance-id"}),
        result=json.dumps({"ok": False, "error": "agent_instance_busy", "taskUuid": "busy-task"}),
    )
    assert len(specs) == 1
    assert specs[0]["op_id"] == "agent:failed-call"
    assert specs[0]["status"] == "failed"
    assert specs[0]["lifecycle"] == "terminal"


async def _legacy_continue(web_env, *, task_status="completed", source_is_task=False):
    server = web_env.server
    row = await server._create_web_conversation(123, title="legacy continuation")
    conv, chat = row["conversation_uuid"], int(row["internal_chat_id"])
    live = server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "root"})
    await live.publish({"type": "user", "turnUuid": "root", "messageUuid": "original", "text": "original request"})
    await live.publish({"type": "final", "turnUuid": "root", "text": "original answer"})
    source = "previous-task" if source_is_task else "instance-id"
    if source_is_task:
        await server.rath_dao.create_task(task_uuid=source, chat_id=chat, parent_session_uuid=conv, status="completed", workflow_uuid="test", title="previous round")
    await server.rath_dao.create_task(task_uuid="new-task", chat_id=chat, parent_session_uuid=conv, status=task_status, workflow_uuid="test", title="new round")
    args = json.dumps({"to": source, "prompt": "next round", "tools": []})
    result = {"ok": True, "taskUuid": "new-task", "status": "running", "detached": True}
    async with server._web_operation_lock(conv):
        await server._publish_operation(
            conv, internal_chat_id=chat, op_id=f"agent:{source}", op_type="agent", action="start", turn_uuid="root",
            payload={"toolName": "AgentContinue", "toolCallId": "continue-call", "taskUuid": source,
                     "args": args, "status": "queued", "resultText": json.dumps(result), "transcriptResult": True},
            status="queued", lifecycle="active",
        )
    await live.publish({"type": "tool_progress", "name": "AgentContinue", "toolCallId": "continue-call", "arguments": args,
                        "payload": {"status": task_status, "task": {"taskUuid": "new-task", "status": task_status}}})
    await live.publish({"type": "done", "turnUuid": "root"})
    await server._touch_web_conversation(conv, status="idle", current_status="就绪")
    return row, live, f"agent:{source}"


@pytest.mark.parametrize("task_status", ["completed", "running"])
async def test_legacy_continue_merges_only_phantom_and_preserves_real_card(web_env, task_status):
    row, live, phantom = await _legacy_continue(web_env, task_status=task_status)
    server, conv = web_env.server, row["conversation_uuid"]
    background = asyncio.create_task(asyncio.Event().wait()) if task_status == "running" else None
    if background:
        server.rath.register("new-task", int(row["internal_chat_id"]), background)
    try:
        before = {op["opId"]: op for op in await server._web_operations(conv)}
        frames = await server._reconcile_inactive_web_conversation_operations(row, source="test")
        after = {op["opId"]: op for op in await server._web_operations(conv)}
        assert after[phantom]["lifecycle"] == "terminal"
        assert after[phantom]["payload"]["mergedTo"] == "agent:new-task"
        assert after["agent:new-task"] == before["agent:new-task"]
        assert [op for op in after.values() if op["opType"] in {"user_message", "assistant_message"}] == [
            op for op in before.values() if op["opType"] in {"user_message", "assistant_message"}
        ]
        assert len(frames) == 1
        assert frames[0]["debug"]["reason"] == "agent_continue_identity_reconcile"
        assert await server._reconcile_inactive_web_conversation_operations(row, source="test-again") == []
        cookie = await _login_cookie(web_env)
        response = await web_env.client.get(f"/api/conversations/{conv}/state?timelineLimit=200", cookies={"openbear_web_session": cookie})
        assert response.status == 200
        state = await response.json()
        assert state["running"] is (task_status == "running")
    finally:
        if background:
            background.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background


async def test_legacy_continue_never_merges_a_real_previous_task(web_env):
    row, live, previous = await _legacy_continue(web_env, source_is_task=True)
    await web_env.server._reconcile_inactive_web_conversation_operations(row, source="test")
    ops = {op["opId"]: op for op in await web_env.server._web_operations(row["conversation_uuid"])}
    assert ops[previous]["status"] == "completed"
    assert not ops[previous]["payload"].get("merged")
    assert ops["agent:new-task"]["status"] == "completed"


async def test_missing_task_reconciles_but_failed_lookup_does_not(web_env, monkeypatch):
    payload = {"toolName": "AgentContinue", "taskUuid": "not-a-task", "status": "queued"}
    assert await web_env.server._terminal_status_for_agent_operation(payload, set()) == "interrupted"

    async def unavailable(_):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(web_env.server.rath_dao, "get_task", unavailable)
    assert await web_env.server._terminal_status_for_agent_operation(payload, set()) == ""


async def test_idle_stop_terminalizes_phantom_without_changing_completed_work(web_env):
    row, live, phantom = await _legacy_continue(web_env)
    server, conv = web_env.server, row["conversation_uuid"]
    before = {op["opId"]: op for op in await server._web_operations(conv)}
    result = await server._stop_web_conversation(row)
    assert result == {"ok": True, "stoppedRun": False, "stoppedTasks": 0, "stoppedProcesses": 0}
    after = {op["opId"]: op for op in await server._web_operations(conv)}
    assert after[phantom]["lifecycle"] == "terminal"
    assert after[phantom]["status"] == "interrupted"
    assert after["agent:new-task"] == before["agent:new-task"]
    assert all(op["lifecycle"] not in {"active", "paused", "waiting_control"} for op in after.values())
    frames = await server._web_frames(conv)
    assert any(frame["opId"] == phantom and frame["action"] == "cancel" for frame in frames)
    count = len(frames)
    await server._stop_web_conversation(row)
    assert len(await server._web_frames(conv)) == count


@pytest.mark.parametrize("pending", [False, True])
async def test_send_after_phantom_starts_controller_and_consumes_stranded_message(web_env, monkeypatch, pending):
    row, live, phantom = await _legacy_continue(web_env)
    server, chat = web_env.server, int(row["internal_chat_id"])
    steering.clear(chat)
    server.runs = RunRegistry()
    server.tools = ToolRegistry()
    backend = FakeStreamBackend([[StreamEvent(kind="content", text="new answer"), StreamEvent(kind="finish", finish_reason="stop")]])
    server.llm_factory = FakeRunFactory(backend, context_window=100_000)

    async def system_prompt(*args, **kwargs):
        return "Answer the user."

    monkeypatch.setattr(server, "_build_system_prompt_for_chat", system_prompt)
    if pending:
        steering.enqueue(chat, "stranded follow-up", visibleText="stranded follow-up", turnUuid="root", rootTurnUuid="root", messageUuid="stranded", source="web")
        await live.publish({"type": "queued", "turnUuid": "root", "messageUuid": "stranded", "text": "stranded follow-up", "status": "已追加到当前轮"})
    try:
        result = await server._start_or_steer_web_conversation(row, "new request", [], live)
        assert result == {"ok": True, "queued": False}
        task = server.runs.task(chat)
        assert task is not None
        await asyncio.wait_for(task, timeout=5)
        assert backend.calls >= 1
        sent = json.dumps(backend.seen_convos, ensure_ascii=False)
        assert "new request" in sent
        if pending:
            assert "stranded follow-up" in sent
        assert steering.pending_items(chat) == []
        state = await server._chat_payload(chat, row)
        assert state["running"] is False
        assert state["facts"]["activeOperationIds"] == []
        ops = {op["opId"]: op for op in state["operations"]}
        assert ops[phantom]["payload"]["merged"] is True
        assert ops["msg:original"]["payload"]["text"] == "original request"
    finally:
        await server.runs.cancel_all_and_wait()
        steering.clear(chat)
