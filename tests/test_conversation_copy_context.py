"""Conversation copies own their history, summary references and execution checkpoint.

All persistence and HTTP requests use pytest's temporary SQLite/test server.
Continue uses local fake providers/tools; no real upstream or side effects.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from app.context.builder import build_controller_history
from app.context.history import ControllerExecutionHistory
from app.context.runtime import ContextManager
from app.context.store import ContextHistoryUnavailable, ContextOwner, WindowStore
from app.context.window import WindowPolicy, mark_source, source_of
from app.db.dao import MessageDAO, SummaryDAO
from app.llm.base import AgentResult
from app.llm.events import ToolCall
from app.rath.manager import RathTaskManager
from app.rath.schemas import RathAgentDef
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.agents import register_agent_tools
from app.tools.base import ToolRegistry, ToolRuntimeContext
from tests.test_agent_continuity import call
from tests.test_rath_web_api import web_env as rath_web_env
from tests.test_tools_agent_orchestration import (
    _FakeConfig,
    _FakeFactory,
    _FakeSelection,
    _RecordingBackend,
)

web_env = rath_web_env


class NoModelBackend:
    def __init__(self, protocol="chat"):
        self.protocol = protocol

    async def complete(self, *args, **kwargs):
        raise AssertionError("Copy/restore must not invoke a model")


async def copied(env, source):
    response = await env.client.post(
        f"/api/conversations/{source['conversation_uuid']}/duplicate", cookies=env.cookie,
    )
    assert response.status == 200, await response.text()
    public = (await response.json())["conversation"]
    return await env.server._conversation_row(123, public["conversationUuid"], require=True)


async def controller_store(env, conversation):
    chat = conversation["internal_chat_id"]
    session = await MessageDAO(env.db).get_or_create_session_uuid(chat)
    return WindowStore(env.db, ContextOwner.controller(
        chat_id=chat, session_uuid=session, conversation_uuid=conversation["conversation_uuid"],
    ))


async def save(store, messages, *, strategy="sliding_window"):
    archived = await store.archive(messages)
    return await store.save(
        messages, expected_revision=archived["revision"],
        expected_source_revision=archived["sourceRevision"], route="old-route",
        extra_state={"strategy": strategy},
    )


async def original_event(env, conversation, query):
    store = await controller_store(env, conversation)
    history = ControllerExecutionHistory(
        env.db, chat_id=conversation["internal_chat_id"], session_uuid=store.owner.session_uuid,
    )
    index = await history.index(query=query)
    assert len(index["events"]) == 1
    return index["events"][0], await history.event_payload(index["events"][0]["event_id"])


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_copy_retains_selection_archives_and_next_request(web_env, strategy):
    env = web_env
    dao = MessageDAO(env.db)
    source = await env.server._create_web_conversation(123, title="copy selection", run_config={"context_strategy": strategy})
    chat, conv = source["internal_chat_id"], source["conversation_uuid"]
    for role, text, root in [
        ("user", "OLD_REQUEST", "old-root"),
        ("assistant", "EVICTED_EVIDENCE", "old-root"),
        ("user", "CURRENT_REQUEST", "current-root"),
        ("assistant", "RECENT_RESULT", "current-root"),
    ]:
        await dao.add(chat, role, text, conversation_uuid=conv, turn_uuid=root, run_root_turn_uuid=root)
    original = await build_controller_history(dao, chat)
    store = await controller_store(env, source)
    await store.archive(original)
    selected = original[2:]
    if strategy == "model_summary":
        selected = [mark_source({"role": "user", "content": "SYNTHETIC_COMPACTION_SUMMARY"}, kind="summary"), *selected]
    await save(store, selected, strategy=strategy)
    ticket = await store.begin_request(route="old-route")
    await store.observe_usage(ticket, tokens=55555)
    cp = await copied(env, source)
    newchat, newconv = cp["internal_chat_id"], cp["conversation_uuid"]
    newstore = await controller_store(env, cp)
    restored = await build_controller_history(dao, newchat)
    assert [m["content"] for m in restored] == [m["content"] for m in selected]
    state = await newstore.load()
    assert not state["usage_known"]
    assert "calibration" not in state["state"]
    assert cp["context_strategy"] == strategy
    for conversation in (source, cp):
        _, event = await original_event(env, conversation, "EVICTED_EVIDENCE")
        assert event["payload"]["content"] == "EVICTED_EVIDENCE"
    await dao.add(newchat, "user", "NEW_COPY_TASK", conversation_uuid=newconv, turn_uuid="copy-root", run_root_turn_uuid="copy-root")

    async def strategy_resolver():
        return strategy

    runtime = ContextManager(
        newstore, WindowPolicy(100000, trigger_tokens=80000), backend=NoModelBackend(), model="gpt",
        active_run_root_turn_uuid="copy-root", strategy_resolver=strategy_resolver,
    )
    ready = await runtime.prepare(await build_controller_history(dao, newchat), system="Keep task scope", tools=[])
    assert runtime.active_strategy == strategy
    assert "NEW_COPY_TASK" in str(ready) and "EVICTED_EVIDENCE" not in str(ready)
    await dao.clear(chat)
    assert (await original_event(env, cp, "EVICTED_EVIDENCE"))[1]["payload"]["content"] == "EVICTED_EVIDENCE"


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
@pytest.mark.parametrize("archive_original", [False, True])
async def test_copy_legacy_summary_and_derivatives_never_alias_original_history(web_env, strategy, archive_original):
    env = web_env
    dao = MessageDAO(env.db)
    source = await env.server._create_web_conversation(123, title="legacy copy", run_config={"context_strategy": strategy})
    chat, conv = source["internal_chat_id"], source["conversation_uuid"]
    await dao.add(chat, "user", "OLD_TASK", conversation_uuid=conv, turn_uuid="old-root", run_root_turn_uuid="old-root")
    boundary = await dao.add(chat, "assistant", "EXACT_ORIGINAL_BEFORE_SUMMARY", conversation_uuid=conv, turn_uuid="old-root", run_root_turn_uuid="old-root")
    store = await controller_store(env, source)
    if archive_original:
        await store.archive(await build_controller_history(dao, chat))
    await SummaryDAO(env.db).add(chat, "LEGACY_SUMMARY_NOT_THE_ORIGINAL_EVENT", boundary, 20)
    await dao.add(chat, "user", "TAIL_TASK", conversation_uuid=conv, turn_uuid="tail-root", run_root_turn_uuid="tail-root")
    messages = await build_controller_history(dao, chat)
    messages.append(mark_source(
        {"role": "assistant", "content": "DERIVED_VIEW_NOT_THE_ORIGINAL"},
        source_id=f"message:{boundary}/closed:derivative", derived_from=f"message:{boundary}", message_id=boundary,
    ))
    await save(store, messages, strategy=strategy)
    first = await copied(env, source)
    second = await copied(env, first)
    seen_ids = set()
    for conversation in (source, first, second):
        index, original = await original_event(env, conversation, "EXACT_ORIGINAL_BEFORE_SUMMARY")
        assert index["role"] == original["payload"]["role"] == "assistant"
        assert original["payload"]["content"] == "EXACT_ORIGINAL_BEFORE_SUMMARY"
        assert index["event_id"] not in seen_ids
        seen_ids.add(index["event_id"])
        window = await controller_store(env, conversation)
        selected = await window.restore_messages()
        summary = await SummaryDAO(env.db).latest(conversation["internal_chat_id"])
        summary_source = source_of(selected[0])
        assert summary_source["id"] == f"legacy-summary:{summary['id']}"
        assert summary_source["summary_id"] == summary["id"]
        assert summary_source["message_id"] == index["seq"]
        assert (await window.event_payload(summary_source["id"]))["payload"]["content"] == selected[0]["content"]
        derivative = source_of(selected[-1])
        assert derivative["id"] != index["event_id"]
        assert derivative["derived_from"] == index["event_id"]
        assert (await window.event_payload(derivative["id"]))["payload"]["content"] == "DERIVED_VIEW_NOT_THE_ORIGINAL"
    for conversation in (source, first):
        response = await env.client.delete(f"/api/conversations/{conversation['conversation_uuid']}", cookies=env.cookie)
        assert response.status == 200, await response.text()
    assert (await original_event(env, second, "EXACT_ORIGINAL_BEFORE_SUMMARY"))[1]["payload"]["content"] == "EXACT_ORIGINAL_BEFORE_SUMMARY"
    assert "LEGACY_SUMMARY_NOT_THE_ORIGINAL_EVENT" in str(await (await controller_store(env, second)).restore_messages())


def runner_for(env, conversation, instance, task_uuid, agent, *, protocol="chat"):
    runner = SingleAgentWorkflowRunner(
        env.server.rath_dao, task_uuid, agent=agent, backend=NoModelBackend(protocol), model="gpt",
        max_tokens=1000, tools=ToolRegistry(), context_window=50000,
        agent_session_uuid=instance.session_uuid, openbear_session_uuid=conversation["conversation_uuid"],
    )
    runner.chat_id = conversation["internal_chat_id"]
    runner.conversation_uuid = conversation["conversation_uuid"]
    runner.turn_uuid = runner.run_root_turn_uuid = "agent-root"
    return runner


async def agent_checkpoint(env, *, partial):
    dao = env.server.rath_dao
    source = await env.server._create_web_conversation(123, title="agent copy")
    chat, conv = source["internal_chat_id"], source["conversation_uuid"]
    workflow = await dao.workflow_by_slug("single-agent")
    instance = await dao.create_agent_instance(openbear_session_uuid=conv, chat_id=chat, workflow_uuid=workflow.workflow_uuid, agent_key="copy-agent")
    tid = await dao.create_task(chat_id=chat, workflow_uuid=workflow.workflow_uuid, parent_session_uuid=conv, title="partial task", agent_session_uuid=instance.session_uuid)
    agent = RathAgentDef(workflow_uuid=workflow.workflow_uuid, agent_key="copy-agent", name="Copy agent", id=1, system_prompt="Only inspect", tool_allowlist=[], model="gpt", enabled=True)
    runner = runner_for(env, source, instance, tid, agent)
    runner.session_id = "SOURCE_PROVIDER_SESSION"
    messages = [mark_source({"role": "user", "content": "PARTIAL_TASK"}, kind="task", task_uuid=tid, turn_uuid="agent-root", run_root_turn_uuid="agent-root")]
    await runner._checkpoint_model_context(messages, round_no=0, stage="before_request")
    messages += [
        mark_source({"role": "assistant", "content": "", "tool_calls": [ToolCall("read-id", "Read", "{}"), ToolCall("write-id", "Bash", "{}")], "native_output_items": [{"type": "reasoning", "encrypted_content": "OLD_CACHE"}]}, task_uuid=tid),
        mark_source({"role": "tool", "name": "Read", "tool_call_id": "read-id", "content": "CONFIRMED_READ"}, task_uuid=tid),
    ]
    if not partial:
        messages.append(mark_source({"role": "tool", "name": "Bash", "tool_call_id": "write-id", "content": "CONFIRMED_SECOND_RESULT"}, task_uuid=tid))
    stage = "cancelled" if partial else "completed"
    await runner._checkpoint_model_context(
        messages, round_no=1, stage=stage,
        extra_state={"providerPromptSnapshot": {"system": "OLD_CACHE"}, **({"inflightTool": {"id": "write-id", "name": "Bash"}} if partial else {})},
    )
    await dao.update_task(tid, status=stage)
    checkpoint = await dao.task_model_context(tid)
    runner._round_input = {"contextSource": {"taskUuid": tid, "revision": checkpoint["revision"], "state": "partial" if partial else "restored"}}
    return source, instance, tid, agent, runner


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_copy_agent_checkpoint_pairing_and_independent_continue(web_env, partial, protocol):
    env = web_env
    dao = env.server.rath_dao
    source, instance, tid, agent, runner = await agent_checkpoint(env, partial=partial)
    before = await runner._round_context_messages()
    source_checkpoint = await dao.task_model_context(tid)
    previous = source
    for _ in range(2):
        cp = await copied(env, previous)
        sessions = await dao.list_agent_sessions(openbear_session_uuid=cp["conversation_uuid"])
        assert len(sessions) == 1
        ni = sessions[0]
        ntid = ni.context_task_uuid
        checkpoint = await dao.task_model_context(ntid)
        nr = runner_for(env, cp, ni, ntid, agent, protocol=protocol)
        nr._round_input = {"contextSource": {"taskUuid": ntid, "revision": checkpoint["revision"], "state": "partial" if partial else "restored"}}
        window = await nr._get_window_runtime().store.load()
        assert (checkpoint["state"]["windowVersion"], checkpoint["state"]["windowRevision"]) == (window["window_version"], window["revision"])
        assert checkpoint["sessionId"] == checkpoint["state"]["sessionId"] == ""
        assert checkpoint["state"]["providerPromptSnapshot"] == {}
        assert "OLD_CACHE" not in str(checkpoint)
        for message in checkpoint["state"]["messages"]:
            event_id = source_of(message)["id"]
            assert event_id in {source_of(m)["id"] for m in window["state"]["messages"]}
            payload = await nr._get_window_runtime().store.event_payload(event_id)
            assert payload["payload"]["content"] == message["content"]
            if source_of(message).get("derived_from"):
                original = await nr._get_window_runtime().store.event_payload(source_of(message)["derived_from"])
                assert len(original["payload"]["tool_calls"]) == 2
        after = await nr._round_context_messages()
        assert [m.get("content") for m in after] == [m.get("content") for m in before]
        assert "CONFIRMED_READ" in str(after)
        if partial:
            assert checkpoint["state"]["inflightTool"] == {"id": "write-id", "name": "Bash"}
            assert window["state"]["incompleteBatch"]
            assert "不要自动重放" in str(after)
            assert "write-id" not in str([m.get("tool_calls") for m in after])
        else:
            assert "CONFIRMED_SECOND_RESULT" in str(after)
        # Delete the entire source before restoring the copy again.
        response = await env.client.delete(f"/api/conversations/{previous['conversation_uuid']}", cookies=env.cookie)
        assert response.status == 200, await response.text()
        assert "CONFIRMED_READ" in str(await nr._round_context_messages())
        previous = cp
    assert source_checkpoint["sessionId"] == "SOURCE_PROVIDER_SESSION"


@pytest.mark.parametrize("bad_pair", [(0, 1), (99, 2), (None, None)])
async def test_copy_does_not_launder_unreliable_partial_checkpoint(web_env, bad_pair):
    env = web_env
    dao = env.server.rath_dao
    source, _, tid, agent, runner = await agent_checkpoint(env, partial=True)
    original = await dao.task_model_context(tid)
    state = original["state"]
    state["windowVersion"], state["windowRevision"] = bad_pair
    await env.db.conn.execute("UPDATE rath_task_model_contexts SET state_json=? WHERE task_uuid=?", (json.dumps(state), tid))
    await env.db.conn.commit()
    with pytest.raises(ContextHistoryUnavailable, match="incomplete_tool_batch_requires_reliable_execution_checkpoint"):
        await runner._round_context_messages()
    for _ in range(2):
        source = await copied(env, source)
        ni = (await dao.list_agent_sessions(openbear_session_uuid=source["conversation_uuid"]))[0]
        cp = await dao.task_model_context(ni.context_task_uuid)
        nr = runner_for(env, source, ni, ni.context_task_uuid, agent)
        nr._round_input = {"contextSource": {"taskUuid": ni.context_task_uuid, "revision": cp["revision"], "state": "partial"}}
        with pytest.raises(ContextHistoryUnavailable, match="incomplete_tool_batch_requires_reliable_execution_checkpoint"):
            await nr._round_context_messages()


@pytest.mark.parametrize("partial", [False, True])
async def test_copied_instance_agent_continue_tool_never_replays_uncertain_side_effect(web_env, tmp_path, monkeypatch, partial):
    env = web_env
    dao = env.server.rath_dao
    source = await env.server._create_web_conversation(123, title="Continue integration")
    reg = ToolRegistry()
    backend = _RecordingBackend()
    manager = RathTaskManager(dao)
    actual_calls = []

    async def read(_args):
        actual_calls.append("Read")
        return "CONFIRMED_COPY_READ"

    async def uncertain(_args):
        actual_calls.append("Bash")
        if partial:
            raise asyncio.CancelledError
        return "CONFIRMED_COPY_SECOND_RESULT"

    reg.add("Read", "test read", {"type": "object"}, read)
    reg.add("Bash", "test uncertain effect", {"type": "object"}, uncertain)
    register_agent_tools(
        reg, config=_FakeConfig(), dao=dao, manager=manager, llm_factory=_FakeFactory(backend),
        model_selection=_FakeSelection(), workspace_dir=str(tmp_path),
    )
    context = ToolRuntimeContext(
        chat_id=source["internal_chat_id"], session_uuid=source["conversation_uuid"],
        conversation_uuid=source["conversation_uuid"], source="web", turn_uuid="first", run_root_turn_uuid="first",
    )
    report = backend.complete
    requested = False

    async def first_request(messages, **kwargs):
        nonlocal requested
        if requested:
            return await report(messages, **kwargs)
        requested = True
        return AgentResult(tool_calls=[ToolCall("read", "Read", "{}"), ToolCall("write", "Bash", "{}")])

    monkeypatch.setattr(backend, "complete", first_request)
    first = await call(reg, "Agent", {"prompt": "copy execution facts", "tools": ["Read", "Bash"]}, context)
    assert first["status"] == ("cancelled" if partial else "completed"), first
    cp = await copied(env, source)
    ni = (await dao.list_agent_sessions(openbear_session_uuid=cp["conversation_uuid"]))[0]
    assert ni.session_uuid != first["agentSession"]["agentId"]
    monkeypatch.setattr(backend, "complete", report)
    backend.protocol = "responses"
    next_context = replace(
        context, chat_id=cp["internal_chat_id"], session_uuid=cp["conversation_uuid"],
        conversation_uuid=cp["conversation_uuid"], turn_uuid="continued", run_root_turn_uuid="continued",
    )
    second = await call(reg, "AgentContinue", {"to": ni.session_uuid, "prompt": "report retained facts; do not rerun", "tools": []}, next_context)
    assert second["status"] == "completed", second
    assert second["task"]["taskId"] != ni.context_task_uuid
    request = str(backend.calls[-1]["messages"])
    assert "CONFIRMED_COPY_READ" in request
    if partial:
        assert "不要自动重放" in request
    else:
        assert "CONFIRMED_COPY_SECOND_RESULT" in request
    assert actual_calls == ["Read", "Bash"]
    assert (await dao.get_task(first["task"]["taskId"])).status == first["status"]


async def test_copy_rolls_back_windows_when_checkpoint_copy_fails(web_env, monkeypatch):
    env = web_env
    source, _, _, _, runner = await agent_checkpoint(env, partial=True)
    tables = ["web_conversations", "sessions", "messages", "summaries", "rath_tasks", "rath_agent_sessions",
              "rath_task_model_contexts", "context_windows", "context_execution_events"]

    async def counts():
        return {table: (await (await env.db.conn.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] for table in tables}

    before = await counts()
    original_copy = env.server._copy_table_rows_for_duplicate

    async def fail_checkpoint(table, *args, **kwargs):
        if table == "rath_task_model_contexts":
            raise RuntimeError("injected checkpoint copy failure")
        return await original_copy(table, *args, **kwargs)

    monkeypatch.setattr(env.server, "_copy_table_rows_for_duplicate", fail_checkpoint)
    with pytest.raises(RuntimeError, match="injected checkpoint copy failure"):
        await env.server._duplicate_web_conversation_data(source)
    assert await counts() == before
    assert "CONFIRMED_READ" in str(await runner._round_context_messages())


async def test_copy_compaction_references_details_and_source_deletion(web_env):
    env = web_env
    dao = MessageDAO(env.db)
    source = await env.server._create_web_conversation(123, title="summary card", run_config={"context_strategy": "model_summary"})
    chat, conv = source["internal_chat_id"], source["conversation_uuid"]
    # Message and summary numeric domains intentionally differ.
    await dao.add(chat, "user", "earlier task", conversation_uuid=conv)
    mid = await dao.add(chat, "user", "summarized task", conversation_uuid=conv, turn_uuid="card-root", run_root_turn_uuid="card-root")
    sid = await SummaryDAO(env.db).add(chat, "THE_FULL_SUMMARY 1 context-compaction:1", mid, 20)
    assert mid != sid
    payload = {
        "compactionId": f"context-compaction:{sid}", "summaryId": sid,
        "summaryRef": f"/api/conversations/{conv}/compactions/{sid}", "scope": "root", "source": "manual",
        "status": "completed", "outputAvailable": True, "beforeTokens": sid, "afterTokens": 1,
        "output": f"Exact text {sid} context-compaction:{sid}",
        "metadata": {"summary_id": str(sid), "messageId": mid, "op_id": f"tool:context-compaction:{sid}"},
    }
    await env.server._publish_operation(
        conv, internal_chat_id=chat, owner_chat_id=123, op_id=f"tool:context-compaction:{sid}",
        op_type="context_compaction", action="end", turn_uuid="card-root", payload=payload,
        status="completed", lifecycle="terminal", source="manual", internal=False,
    )
    await env.db.conn.execute(
        "UPDATE web_event_frames SET debug_json=? WHERE conversation_uuid=?",
        (json.dumps(payload), conv),
    )
    await env.db.conn.execute(
        "INSERT INTO operations(operation_uuid,chat_id,kind,detail_json) VALUES(?,?,?,?)",
        ("source-summary-operation", chat, "context_compaction", json.dumps(payload)),
    )
    await env.db.conn.execute(
        "INSERT INTO web_operation_messages(conversation_uuid,op_id,message_id,created_at_ms) VALUES(?,?,?,?)",
        (conv, f"tool:context-compaction:{sid}", mid, 1),
    )
    await env.db.conn.commit()
    first = await copied(env, source)
    second = await copied(env, first)
    for conversation in (source, first, second):
        conv, chat = conversation["conversation_uuid"], conversation["internal_chat_id"]
        summary = await SummaryDAO(env.db).latest(chat)
        expected_id = summary["id"]
        expected_op = f"tool:context-compaction:{expected_id}"
        cur = await env.db.conn.execute("SELECT op_id,payload_json FROM web_operations WHERE conversation_uuid=?", (conv,))
        operations = await cur.fetchall()
        assert len(operations) == 1
        assert operations[0]["op_id"] == expected_op
        card = json.loads(operations[0]["payload_json"])
        assert card["summaryId"] == expected_id
        assert card["compactionId"] == f"context-compaction:{expected_id}"
        assert card["metadata"]["summary_id"] == str(expected_id)
        assert card["metadata"]["messageId"] == summary["up_to_message_id"]
        assert card["metadata"]["op_id"] == expected_op
        assert card["beforeTokens"] == sid
        assert card["afterTokens"] == 1
        assert card["output"] == payload["output"]
        response = await env.client.get(card["summaryRef"], cookies=env.cookie)
        assert response.status == 200, await response.text()
        detail = await response.json()
        assert detail["summaryId"] == expected_id
        assert detail["compactedOutput"] == "THE_FULL_SUMMARY 1 context-compaction:1"
        assert detail["source"] == "manual"
        for table, column, where, value in [
            ("operations", "detail_json", "chat_id", chat),
            ("web_event_frames", "payload_json", "conversation_uuid", conv),
            ("web_event_frames", "debug_json", "conversation_uuid", conv),
        ]:
            cur = await env.db.conn.execute(f"SELECT {column} FROM {table} WHERE {where}=?", (value,))
            details = [json.loads(row[column]) for row in await cur.fetchall()]
            assert any(item.get("summaryId") == expected_id for item in details)
        cur = await env.db.conn.execute("SELECT op_id,message_id FROM web_operation_messages WHERE conversation_uuid=?", (conv,))
        linked = await cur.fetchone()
        assert linked["op_id"] == expected_op
        assert linked["message_id"] == summary["up_to_message_id"]
        cur = await env.db.conn.execute("SELECT op_id FROM web_event_frames WHERE conversation_uuid=?", (conv,))
        assert expected_op in {row["op_id"] for row in await cur.fetchall()}
    for conversation in (source, first):
        response = await env.client.delete(f"/api/conversations/{conversation['conversation_uuid']}", cookies=env.cookie)
        assert response.status == 200, await response.text()
    response = await env.client.get(card["summaryRef"], cookies=env.cookie)
    assert response.status == 200
    assert (await response.json())["compactedOutput"] == "THE_FULL_SUMMARY 1 context-compaction:1"
