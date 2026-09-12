"""Window-specific ingress, execution-history and lifecycle acceptance (no live services)."""
from __future__ import annotations

import json

import pytest

from app.agent.loop import Agent
from app.context.builder import build_controller_history
from app.context.runtime import WindowRuntime
from app.context.store import ContextHistoryUnavailable, ContextOwner, WindowStore
from app.context.window import WindowPolicy, mark_source
from app.db.dao import MessageDAO
from app.llm.events import StreamEvent, ToolCall
from app.rath.runner import RathTaskCancelled
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.agent_history import register_agent_history_tool
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.history import register_history_tools
from tests.test_agent_loop import RecordRenderer
from tests.test_agent_window_runtime import env as agent_window_env
from tests.test_context_window import batch, human
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as controller_web_env

web_env = controller_web_env
env = agent_window_env


async def dispatch(reg, name, args, ctx):
    return json.loads(await reg.dispatch(name, json.dumps(args), context=ctx))


async def test_controller_steering_during_selection_is_in_first_request(web_env):
    row = await web_env.server._create_web_conversation(123, title="selection-race")
    chat = int(row["internal_chat_id"])
    dao = MessageDAO(web_env.db)
    session = await dao.get_or_create_session_uuid(chat)
    owner = ContextOwner.controller(chat_id=chat, session_uuid=session)
    queue = []
    rotated = []

    async def on_rotated(detail):
        rotated.append(detail)
        if len(rotated) == 1:
            queue.append("Cancel the proposed write; answer without changing files.")

    def drain():
        out = list(queue)
        queue.clear()
        return out

    class Backend:
        protocol = "chat"
        calls = 0

        async def stream(self, messages, **kwargs):
            self.calls += 1
            assert "Cancel the proposed write" in str(messages)
            yield StreamEvent(kind="content", text="No file changed.")
            yield StreamEvent(kind="finish", finish_reason="stop")

    backend = Backend()
    runtime = WindowRuntime(WindowStore(web_env.db, owner), WindowPolicy(50_000, trigger_tokens=1800),
                            backend=backend, model="test", on_rotated=on_rotated)
    result = await Agent(backend, ToolRegistry()).run(
        [human("Preserve the agreed restrictions"), *batch(1, text="old payload " * 1000), *batch(2), *batch(3)],
        RecordRenderer(), model="test", steer_drain=drain, window_runtime=runtime,
    )
    assert result.text == "No file changed."
    assert backend.calls == 1
    assert rotated and not queue
    assert "Cancel the proposed write" in str(await runtime.store.restore_messages())


async def test_agent_stop_accepted_during_window_preparation_prevents_model_call(env, monkeypatch):
    db, dao, task_uuid, agent = env

    class Backend:
        protocol = "chat"
        calls = 0

        async def complete(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("A stopped Agent must not start a model request")

    backend = Backend()
    runner = SingleAgentWorkflowRunner(dao, task_uuid, agent=agent, backend=backend,
        model="test", max_tokens=1024, tools=ToolRegistry(), context_window=40000)
    original = runner._prepare_context_window

    async def prepare(*args, **kwargs):
        result = await original(*args, **kwargs)
        await dao.add_control(task_uuid, "stop", message="Stop now", requested_by="main-controller")
        return result

    monkeypatch.setattr(runner, "_prepare_context_window", prepare)
    with pytest.raises(RathTaskCancelled):
        await runner.run()
    assert backend.calls == 0
    assert not await dao.pending_controls(task_uuid)


async def test_execution_history_facade_exact_paging_and_scope(web_env):
    db = web_env.db
    row = await web_env.server._create_web_conversation(123, title="history-window")
    other = await web_env.server._create_web_conversation(123, title="other-window")
    chat = int(row["internal_chat_id"])
    dao = MessageDAO(db)
    session = await dao.get_or_create_session_uuid(chat)
    body = "页内原文\n" * 15000
    await dao.add(chat, "user", "early instructions", turn_uuid="old", run_root_turn_uuid="old")
    call_id = await dao.add(chat, "assistant", tool_calls=[ToolCall("read", "Read", '{"path":"/bounded/input.txt"}')],
                           reasoning="PRIVATE_REASONING", turn_uuid="current", run_root_turn_uuid="root")
    result_id = await dao.add(chat, "tool", body, name="Read", tool_call_id="read", turn_uuid="current", run_root_turn_uuid="root")
    foreign_id = await dao.add(int(other["internal_chat_id"]), "user", "foreign original")
    reg = ToolRegistry()
    register_history_tools(reg, db)
    register_agent_history_tool(reg, db)
    # Both facades share the strict-schema zero-cursor convention.
    private = WindowStore(db, ContextOwner.agent(task_uuid="task-private"))
    await private.archive([mark_source({"role": "user", "content": "own private task"}, kind="task")])
    ctx = ToolRuntimeContext(chat_id=chat, conversation_uuid=row["conversation_uuid"], session_uuid=session,
                             source="web", turn_uuid="current", run_root_turn_uuid="root")
    args = {"source": "execution", "scope": "current", "currentTask": True}
    index = await dispatch(reg, "History", {**args, "action": "index", "limit": 1, "highWater": 0}, ctx)
    assert index["ok"] and index["events"][0]["seq"] == call_id
    await dao.add(chat, "user", "arrived after index", turn_uuid="current", run_root_turn_uuid="root")
    page = await dispatch(reg, "History", {**args, "action": "index", "after": index["nextAfter"], "highWater": index["highWater"]}, ctx)
    assert [event["seq"] for event in page["events"]] == [result_id]
    assert not page["hasMore"]
    parts, offset = [], 0
    while True:
        result = await dispatch(reg, "History", {**args, "action": "read_event", "eventId": f"message:{result_id}", "maxChars": 1000, "offset": offset}, ctx)
        event = result["events"][0]
        parts.append(event["text"])
        if not event["hasMore"]:
            break
        assert event["nextOffset"] > offset
        offset = event["nextOffset"]
    assert json.loads("".join(parts))["content"] == body
    arguments = await dispatch(reg, "History", {**args, "action": "read_event", "eventId": f"message:{call_id}"}, ctx)
    assert "/bounded/input.txt" in str(arguments) and "PRIVATE_REASONING" not in str(arguments)
    denied = await dispatch(reg, "History", {**args, "action": "read_event", "eventId": f"message:{foreign_id}"}, ctx)
    assert not denied["ok"]
    contradictory = await dispatch(reg, "History", {**args, "action": "index", "excludeCurrentTurn": True}, ctx)
    assert contradictory["error"] == "current_task_cannot_exclude_itself"
    agent_ctx = ToolRuntimeContext(chat_id=chat, conversation_uuid=row["conversation_uuid"], session_uuid=session,
                                   source="agent:test", task_uuid="task-private")
    assert (await dispatch(reg, "History", {**args, "action": "index"}, agent_ctx))["error"] == "use_own_AgentHistory"
    assert not (await dispatch(reg, "AgentHistory", {"action": "index", "ownerId": "someone-else"}, agent_ctx))["ok"]
    fresh = await dispatch(reg, "AgentHistory", {"action": "index", "highWater": 0}, agent_ctx)
    assert fresh["ok"] and len(fresh["events"]) == 1


async def test_duplicate_delete_and_suffix_preserve_window_ownership(web_env):
    db = web_env.db
    dao = MessageDAO(db)
    row = await web_env.server._create_web_conversation(123, title="copy-window")
    chat = int(row["internal_chat_id"])
    session = await dao.get_or_create_session_uuid(chat)
    ids = [await dao.add(chat, role, content) for role, content in [
        ("user", "keep instructions"), ("assistant", "evicted evidence"), ("assistant", "recent result")]]
    originals = await build_controller_history(dao, chat)
    store = WindowStore(db, ContextOwner.controller(chat_id=chat, session_uuid=session, conversation_uuid=row["conversation_uuid"]))
    at = await store.archive(originals)
    await store.save([originals[0], originals[-1]], expected_revision=at["revision"], expected_source_revision=at["sourceRevision"],
                     route="source-model", rotated=True)
    ticket = await store.begin_request(route="source-model")
    await store.observe_usage(ticket, tokens=9999)
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    response = await web_env.client.post(f"/api/conversations/{row['conversation_uuid']}/duplicate", cookies=cookie)
    assert response.status == 200, await response.text()
    copy = (await response.json())["conversation"]
    copied_chat = copy["internalChatId"]
    copied_session = await dao.get_or_create_session_uuid(copied_chat)
    copied = WindowStore(db, ContextOwner.controller(chat_id=copied_chat, session_uuid=copied_session))
    restored = await build_controller_history(dao, copied_chat)
    assert [m["content"] for m in restored] == ["keep instructions", "recent result"]
    assert not (await copied.load())["usage_known"]
    old_evidence = (await copied.index(query="evicted evidence"))["events"][0]["event_id"]
    assert (await copied.event_payload(old_evidence))["payload"]["content"] == "evicted evidence"
    await dao.clear(chat)
    assert await store.load() is None
    assert (await copied.event_payload(old_evidence))["payload"]["content"] == "evicted evidence"
    copied_ids = [row.id for row in await dao.recent(copied_chat)]
    await dao.delete_from_message_id(copied_chat, copied_ids[1])
    assert [m["content"] for m in await build_controller_history(dao, copied_chat)] == ["keep instructions"]
    with pytest.raises(ContextHistoryUnavailable):
        await copied.event_payload(old_evidence)
    assert ids
