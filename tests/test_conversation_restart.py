"""Suffix API -> next real Controller/Agent runner; temporary DB and recording models."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.context.runtime import ContextManager
from app.context.store import ContextOwner, WindowStore
from app.db.dao import MessageDAO, SummaryDAO
from app.llm.base import AgentResult
from app.llm.events import StreamEvent, ToolCall
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.continuity import agent_session_public
from app.rath.manager import RathTaskManager
from app.task_memory import (
    TaskMemoryDAO,
    is_task_memory_runtime_message,
    task_memory_runtime_metadata,
)
from app.tools.agent_history import register_agent_history_tool
from app.tools.agents import register_agent_tools
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.file_state import FileStateStore
from app.tools.files import make_read_tool
from app.tools.history import register_history_tools
from app.web_admin import _WebStreamRenderer
from tests.test_agent_continuity import env as shared_continuity_env
from tests.test_tools_agent_orchestration import _FakeConfig, _FakeFactory, _FakeSelection
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _login_cookie
from tests.test_web_admin import web_env as shared_web_env

web_env = shared_web_env
continuity_env = shared_continuity_env
SUMMARY = "\n".join(f"## {h}\n- SUMMARY_BODY" for h in [
    "Primary Request and Intent", "Key Technical Concepts", "Files and Code Sections",
    "Errors and Fixes", "Problem Solving", "All User Messages", "Pending Tasks", "Current Work",
    "Optional Next Step", "Critical Identifiers",
])



async def sql_rows(env, sql, args=()):
    cur = await env.db.conn.execute(sql, args)
    return [dict(r) for r in await cur.fetchall()]


async def seed_turn(env, row, root, text, *, answer="surviving answer", payload=None, ref=None):
    chat, conv = row["internal_chat_id"], row["conversation_uuid"]
    live, dao = env.server._live_for(row), MessageDAO(env.db)
    await live.publish({"type": "accepted", "turnUuid": root, "runUuid": root})
    await live.publish({"type": "user", "turnUuid": root, "messageUuid": root, "text": text})
    uid = await env.server._persist_web_transcript_message(
        dao, chat, "user", text, conversation_uuid=conv, turn_uuid=root,
        run_root_turn_uuid=root, op_ids=[f"msg:{root}"],
    )
    if ref:
        await env.server._reference_store().save(
            {"manifest": [{"id": root, "sensitive": False}], "materials": [
                {"reference": {"id": root, "sensitive": False}, "content": ref}]},
            conversation_uuid=conv, op_id=f"msg:{root}",
        )
    if payload:
        for i in range(3):
            call = f"{root}-read-{i}"
            await dao.add(chat, "assistant", tool_calls=[ToolCall(call, "Read", "{}")],
                          conversation_uuid=conv, turn_uuid=root, run_root_turn_uuid=root)
            await dao.add(chat, "tool", f"{payload} {i} " + "padding " * 180, name="Read", tool_call_id=call,
                          conversation_uuid=conv, turn_uuid=root, run_root_turn_uuid=root)
    renderer = _WebStreamRenderer(live)
    await renderer.finalize(answer)
    aid = await env.server._persist_web_transcript_message(
        dao, chat, "assistant", answer, conversation_uuid=conv, turn_uuid=root,
        run_root_turn_uuid=root, op_ids=[f"assistant:{root}:0"],
    )
    await renderer.close()
    return uid, aid


async def run_web(env, row, root, text, *, ref=None):
    live = env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": root, "runUuid": root})
    await live.publish({"type": "user", "turnUuid": root, "messageUuid": root, "text": text})
    bundle = ""
    if ref:
        bundle = await env.server._reference_store().save(
            {"manifest": [{"id": root, "sensitive": False}], "materials": [
                {"reference": {"id": root, "sensitive": False}, "content": ref}]},
            conversation_uuid=row["conversation_uuid"], op_id=f"msg:{root}",
        )
    renderer = _WebStreamRenderer(live)
    try:
        return await env.server._run_web_turn(
            row["internal_chat_id"], text, renderer, conversation=row,
            root_turn_uuid=root, user_op_id=f"msg:{root}", reference_bundle_id=bundle,
        )
    finally:
        await renderer.close()


async def configure_web(env, monkeypatch):
    backend = FakeStreamBackend(summary=SUMMARY)
    backend.protocol = "chat"
    env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    env.server.model_selection = _FakeSelection()
    env.server.tools = ToolRegistry()
    env.server.config.agent.max_retries = 0
    env.server.config.agent.compact_max_retries = 0
    env.server.config.agent.keep_recent_messages = 2
    env.server.config.models.providers["openai"].models[0].rollover_trigger_tokens = 8000

    async def system():
        return "Isolated restart audit. No business side effects."
    monkeypatch.setattr(env.server, "_build_system_prompt_for_chat", system)
    return backend


async def truncate(env, row, root="future-1"):
    response = await env.client.delete(
        f"/api/conversations/{row['conversation_uuid']}/turns/{root}/suffix",
        cookies={"openbear_web_session": await _login_cookie(env)},
    )
    data = await response.json()
    assert response.status == 200, data
    return data


@pytest.mark.parametrize("strategy,legacy", [(strategy, legacy) for strategy in ("sliding_window", "model_summary") for legacy in ("none", "valid", "covering_future", "regressed_future")] + [("summary_to_sliding", "none")])
async def test_controller_suffix_real_next_request(web_env, monkeypatch, strategy, legacy):
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, title="restart-controller", model="openai/gpt",
                                                   run_config={"context_strategy": "model_summary" if strategy == "summary_to_sliding" else strategy})
    chat, conv = row["internal_chat_id"], row["conversation_uuid"]
    dao = MessageDAO(env.db)
    early = await seed_turn(env, row, "early", "SURVIVING_ORIGINAL", payload="EVICTED_SURVIVING_TOOL")
    if legacy == "valid":
        await SummaryDAO(env.db).add(chat, "VALID_LEGACY_ONLY", early[1], 10)
        await dao.mark_compacted(chat, early[1])
    await seed_turn(env, row, "survivor", "Use [doc](openbear://ref/doc/1)",
                                answer="CURRENT_BOUNDARY_PROPOSAL: retain correct source", ref="SURVIVING_REFERENCE")
    await TaskMemoryDAO(env.db).create(
        conversation_uuid=conv, scope_type="conversation", name="Synthetic lasting style", body="KEEP_STYLE",
    )
    original_prepare = ContextManager.prepare
    prepare_records = []

    async def force_then_observe(self, messages, **kwargs):
        if self.active_run_root_turn_uuid.startswith("future-"):
            kwargs["force"] = True
        before = await self.store.load()
        result = await original_prepare(self, messages, **kwargs)
        prepare_records.append({"root": self.active_run_root_turn_uuid, "before": before,
                                "incoming": copy.deepcopy(messages), "outgoing": copy.deepcopy(result),
                                "estimate": self.last_estimate.tokens})
        return result

    monkeypatch.setattr(ContextManager, "prepare", force_then_observe)
    assert await run_web(env, row, "future-1", "FUTURE_TEXT_1 [future](openbear://ref/doc/2)", ref="FUTURE_REFERENCE")
    assert await run_web(env, row, "future-2", "FUTURE_TEXT_2")
    sid = await dao.get_or_create_session_uuid(chat)
    store = WindowStore(env.db, ContextOwner.controller(chat_id=chat, session_uuid=sid, conversation_uuid=conv))
    before = await store.load()
    rotations = await sql_rows(env, "SELECT * FROM context_window_rotations WHERE owner_key=?", (store.owner.key,))
    assert len(rotations) >= 2
    if legacy in {"covering_future", "regressed_future"}:
        last = (await sql_rows(env, "SELECT MAX(id) n FROM messages WHERE chat_id=?", (chat,)))[0]["n"]
        await SummaryDAO(env.db).add(chat, "FUTURE_SUMMARY_SHOULD_DISAPPEAR", last, 1234)
        await dao.mark_compacted(chat, last)
        if legacy == "regressed_future":
            # A cumulative successor can have no newly consumed raw rows. Its
            # smaller own anchor does not erase the preceding summary's coverage.
            await SummaryDAO(env.db).add(chat, "FUTURE_SUMMARY_LOW_ANCHOR", 0, 1234)
    ticket = await store.begin_request(route=before["route_fingerprint"])
    await store.observe_usage(ticket, tokens=77777)
    await dao.set_controller_context_usage(chat, session_uuid=sid, tokens=77777, summary_id=0)
    deleted_ids = [r["id"] for r in await sql_rows(env, "SELECT id FROM messages WHERE chat_id=? AND run_root_turn_uuid LIKE 'future-%'", (chat,))]
    await truncate(env, row)
    assert (await store.load())["state"]["restartCutoff"] == deleted_ids[0]
    assert await dao.latest_controller_context_usage(chat, session_uuid=sid) is None
    assert "FUTURE_SUMMARY" not in str(await SummaryDAO(env.db).list_with_anchors(chat))
    rebuilt = await env.server._build_history(chat)
    expanded = await env.server._reference_store().overlay(rebuilt, conversation_uuid=conv)
    assert "FUTURE_TEXT" not in str(expanded) and "FUTURE_SUMMARY" not in str(expanded)
    assert "FUTURE_REFERENCE" not in str(expanded)
    # Summary adoption may replace an old source body, but its original must remain readable.
    reg = ToolRegistry()
    register_history_tools(reg, env.db)
    ctx = ToolRuntimeContext(chat_id=chat, conversation_uuid=conv, session_uuid=sid,
                             source="web", turn_uuid="restart", run_root_turn_uuid="restart")
    recovered = json.loads(await reg.dispatch("History", json.dumps({
        "action": "read_event", "source": "execution", "eventId": f"message:{early[0]}"}), context=ctx))
    future = json.loads(await reg.dispatch("History", json.dumps({
        "action": "read_event", "source": "execution", "eventId": f"message:{deleted_ids[0]}"}), context=ctx))
    assert "SURVIVING_ORIGINAL" in str(recovered) and not future.get("ok")
    if strategy == "summary_to_sliding":
        await env.db.conn.execute("UPDATE web_conversations SET context_strategy='sliding_window' WHERE conversation_uuid=?", (conv,))
        await env.db.conn.commit()
    primary_before, summary_before = backend.calls, backend.complete_calls
    ok = await run_web(env, row, "restart", "好，按保留的边界继续")
    assert ok
    assert backend.calls == primary_before + 1 and backend.complete_calls == summary_before
    sent = backend.seen_convos[-1]
    assert all(marker not in str(sent) for marker in ("FUTURE_TEXT", "FUTURE_REFERENCE", "FUTURE_SUMMARY"))
    boundary_visible = "CURRENT_BOUNDARY_PROPOSAL" in str(sent)
    assert boundary_visible
    assert "SURVIVING_REFERENCE" in str(sent)
    # Reference-bearing tail can be inside a valid model summary rather than a raw message.
    if "Use [doc]" in str(sent):
        assert "SURVIVING_REFERENCE" in str(sent)
    states = [m for m in sent if is_task_memory_runtime_message(m)]
    assert len(states) == 1 and "KEEP_STYLE" in str(states)
    assert task_memory_runtime_metadata(states[0])["epoch"] == 0
    after_request = next(p for p in prepare_records if p["root"] == "restart")
    assert not (after_request["before"] or {}).get("usage_known")
    assert (await store.load())["window_version"] > before["window_version"]
    assert not await store.observe_usage(ticket, tokens=999999)


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_rehydration_budget_after_many_surviving_dialogues(web_env, monkeypatch, strategy):
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    chat = row["internal_chat_id"]
    for i in range(40):
        await seed_turn(env, row, f"old-{i}", f"OLD_{i} " + "surviving old dialogue "*90, answer=f"OLD_REPLY_{i}")
    original = ContextManager.prepare
    stages = []

    async def observe(self, messages, **kwargs):
        if self.active_run_root_turn_uuid.startswith("future-"):
            kwargs["force"] = True
        try:
            result = await original(self, messages, **kwargs)
            stages.append({"root": self.active_run_root_turn_uuid, "inputMessages": len(messages),
                           "outputMessages": len(result), "estimate": self.last_estimate.tokens})
            return result
        except Exception as exc:
            stages.append({"root": self.active_run_root_turn_uuid, "inputMessages": len(messages),
                           "errorType": type(exc).__name__, "error": str(exc),
                           "estimate": self.last_estimate.tokens if self.last_estimate else None})
            raise

    monkeypatch.setattr(ContextManager, "prepare", observe)
    assert await run_web(env, row, "future-1", "FUTURE_LARGE_1")
    assert await run_web(env, row, "future-2", "FUTURE_LARGE_2")
    await truncate(env, row)
    await env.db.conn.execute("UPDATE web_conversations SET context_strategy=? WHERE conversation_uuid=?", (strategy, row["conversation_uuid"]))
    await env.db.conn.commit()
    rebuilt = await env.server._build_history(chat)
    assert len(rebuilt) < 80
    before = backend.calls, backend.complete_calls
    ok = await run_web(env, row, "restart", "CURRENT_NEW_REQUEST")
    assert ok
    assert backend.complete_calls == before[1]
    if ok:
        assert "FUTURE_LARGE" not in str(backend.seen_convos[-1])
        assert len(backend.seen_convos[-1]) < 80


class RecordingAgentBackend:
    protocol = "chat"

    def __init__(self):
        self.calls = []
        self.summary_calls = []

    async def complete(self, messages, *, tools=None, **kwargs):
        if tools is None:
            self.summary_calls.append(copy.deepcopy(messages))
            marker = "FUTURE_AGENT_SUMMARY" if "FUTURE_AGENT" in str(messages) else "SAFE_AGENT_SUMMARY"
            return AgentResult(text=SUMMARY.replace("SUMMARY_BODY", marker))
        self.calls.append(copy.deepcopy(messages))
        return AgentResult(text="isolated simulated completion")


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
@pytest.mark.parametrize("born_after_cut,checkpoint_mode", [(False, "present"), (True, "present"), (False, "missing"), (False, "malformed"), (False, "orphan_future"), (False, "rollback")])
async def test_deleted_agent_rounds_cannot_reenter_continue(web_env, monkeypatch, strategy, born_after_cut, checkpoint_mode):
    env = web_env
    row = await env.server._create_web_conversation(123, model="openai/gpt", run_config={"context_strategy": strategy})
    chat, conv = row["internal_chat_id"], row["conversation_uuid"]
    await seed_turn(env, row, "surviving-root", "SAFE_CONTROLLER")
    await seed_turn(env, row, "future-1", "FUTURE_CONTROLLER_1")
    await seed_turn(env, row, "future-2", "FUTURE_CONTROLLER_2")
    dao = env.server.rath_dao
    await ensure_builtin_workflows(dao)
    reg, backend = ToolRegistry(), RecordingAgentBackend()
    register_agent_history_tool(reg, env.db)
    cfg = _FakeConfig()
    cfg.agent = cfg.agent.model_copy(deep=True)
    cfg.agent.keep_recent_messages = 1
    cfg.agent.compact_max_retries = 0
    manager = RathTaskManager(dao)
    register_agent_tools(reg, config=cfg, dao=dao, manager=manager, llm_factory=_FakeFactory(backend),
                         model_selection=_FakeSelection(), workspace_dir=str(Path(env.db.path).parent))
    async def call(name, args, root):
        return json.loads(await reg.dispatch(name, json.dumps(args), context=ToolRuntimeContext(
            chat_id=chat, session_uuid=conv, conversation_uuid=conv, source="web",
            turn_uuid=root, run_root_turn_uuid=root)))
    original = ContextManager.prepare
    async def rotate_future(self, messages, **kwargs):
        # First input has nothing to summarize; only force on the second task.
        if self.store.owner.kind == "agent" and self.active_run_root_turn_uuid == "future-2":
            kwargs["force"] = True
        return await original(self, messages, **kwargs)
    monkeypatch.setattr(ContextManager, "prepare", rotate_future)
    first_root = "future-1" if born_after_cut else "surviving-root"
    first = await call("Agent", {"prompt": "FUTURE_AGENT_BORN" if born_after_cut else "SAFE_AGENT_ORIGIN", "tools": []}, first_root)
    assert first["status"] == "completed", first
    instance, t1 = first["agentSession"]["sessionUuid"], first["task"]["taskUuid"]
    second = await call("AgentContinue", {"to": instance, "prompt": "FUTURE_AGENT_INSTRUCTION", "tools": []}, "future-2")
    assert second["status"] == "completed", second
    t2 = second["task"]["taskUuid"]
    memdao = TaskMemoryDAO(env.db)
    for scope, owner in [("agent_task", t2), ("agent_session", instance), ("conversation", "")]:
        await memdao.create(conversation_uuid=conv, scope_type=scope, task_uuid=owner,
                            source_run_uuid=t2, source_turn_uuid="future-2", name=f"synthetic-{scope}",
                            body=f"FUTURE_NOTE_{scope}", visible_to_agents=scope=="conversation")
    # Plan rows are inert fixtures, not an approved/implemented real Plan.
    await env.db.conn.execute("INSERT OR REPLACE INTO rath_task_plan_state(task_uuid,phase,last_controller_guidance) VALUES(?, 'completed', 'FUTURE_PLAN')", (t2,))
    await env.db.conn.execute("INSERT INTO rath_task_plan_versions(task_uuid,version,plan_type,plan_json,plan_hash,submit_request_id,submitted_at) VALUES(?,1,'initial','{}','fixture','fixture',1)", (t2,))
    await env.db.conn.commit()
    owner = ContextOwner.agent(task_uuid=t2, agent_session_uuid=instance, conversation_uuid=conv, chat_id=chat)
    store = WindowStore(env.db, owner)
    prior_window = await store.load()
    ticket = await store.begin_request(route=prior_window["route_fingerprint"])
    await store.observe_usage(ticket, tokens=77777)
    session_before = await dao.agent_session(instance)
    if checkpoint_mode == "missing":
        await dao.clear_task_model_context(t1)
    elif checkpoint_mode == "malformed":
        await env.db.conn.execute("UPDATE rath_task_model_contexts SET state_json=? WHERE task_uuid=?",
                                  (json.dumps({"messages": [{"role": "tool", "content": "unpaired", "tool_call_id": "missing"}]}), t1))
        await env.db.conn.commit()
    elif checkpoint_mode == "orphan_future":
        await dao.delete_task_records([t2])
        await env.db.conn.commit()
        assert await dao.task_model_context(t2)
    if checkpoint_mode == "rollback":
        tables = ["messages", "web_operations", "rath_tasks", "rath_agent_sessions", "rath_task_model_contexts",
                  "context_windows", "context_execution_events", "context_window_rotations", "rath_task_plan_state",
                  "rath_task_plan_versions", "conversation_task_memories"]
        snapshot = {table: await sql_rows(env, f"SELECT * FROM {table}") for table in tables}
        real_delete = dao.delete_task_records
        async def fail_delete(ids):
            raise RuntimeError("injected failure after Agent head rewind")
        monkeypatch.setattr(dao, "delete_task_records", fail_delete)
        response = await env.client.delete(f"/api/conversations/{conv}/turns/future-1/suffix",
                                           cookies={"openbear_web_session": await _login_cookie(env)})
        assert response.status == 500
        assert {table: await sql_rows(env, f"SELECT * FROM {table}") for table in tables} == snapshot
        monkeypatch.setattr(dao, "delete_task_records", real_delete)
    await truncate(env, row)
    session_after = await dao.agent_session(instance)
    assert session_after.metadata["llmSessionId"] != session_before.metadata["llmSessionId"]
    orphan_checkpoint = await dao.task_model_context(t2)
    orphan_window = await store.restore_messages()
    assert await dao.get_task(t2) is None
    assert bool(await dao.get_task(t1)) == (not born_after_cut)
    assert orphan_checkpoint is None
    assert "FUTURE_AGENT" not in str(orphan_window)
    assert session_after.context_task_uuid == ("" if born_after_cut or checkpoint_mode in {"missing", "malformed"} else t1)
    assert not (await store.load() or {}).get("usage_known")
    memories = await sql_rows(env, "SELECT scope_type,task_uuid,source_run_uuid,body FROM conversation_task_memories WHERE conversation_uuid=?", (conv,))
    assert {m["scope_type"] for m in memories} == {"conversation", "agent_session"}
    assert not await sql_rows(env, "SELECT * FROM rath_task_plan_state WHERE task_uuid=?", (t2,))
    assert not await sql_rows(env, "SELECT * FROM rath_task_plan_versions WHERE task_uuid=?", (t2,))
    histctx = ToolRuntimeContext(chat_id=chat, conversation_uuid=conv, session_uuid=conv,
                                 source="agent:general-purpose", task_uuid=t2, agent_session_uuid=instance)
    history = json.loads(await reg.dispatch("AgentHistory", '{"action":"search","query":"FUTURE_AGENT"}', context=histctx))
    continued = await call("AgentContinue", {"to": instance, "prompt": "SAFE_RESTART_VERIFY_ONLY", "tools": []}, "restart")
    assert not history.get("events")
    if born_after_cut:
        assert not continued["ok"] and continued["error"] == "agent_instance_closed"
        assert not agent_session_public(session_after)["canContinue"]
        assert len(backend.calls) == 2
    elif checkpoint_mode in {"missing", "malformed"}:
        assert not continued["ok"] and continued["error"] == "agent_context_unavailable"
        assert len(backend.calls) == 2
        assert not agent_session_public(session_after)["canContinue"]
    else:
        assert continued["status"] == "completed", continued
        sent = backend.calls[-1]
        assert "SAFE_AGENT_ORIGIN" in str(sent)
        assert "FUTURE_AGENT" not in str(sent)
        assert "FUTURE_PLAN" not in str(sent)
        original = json.loads(await reg.dispatch("AgentHistory", '{"action":"search","query":"SAFE_AGENT_ORIGIN"}', context=histctx))
        assert original["events"]
        assert not (await dao.task_model_context(t1))["state"].get("providerPromptSnapshot")
        assert (await store.load())["window_version"] > prior_window["window_version"]
        assert not await store.observe_usage(ticket, tokens=999999)
        # Persistent notes deliberately are NOT rolled back.
        assert "FUTURE_NOTE_conversation" in str(sent)


async def test_suffix_does_not_rollback_conversation_preferences(web_env, monkeypatch):
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    conv = row["conversation_uuid"]
    await seed_turn(env, row, "survivor", "SAFE_ORIGINAL")
    dao = TaskMemoryDAO(env.db)
    old, _ = await dao.create(conversation_uuid=conv, scope_type="conversation", name="existing preference",
                              body="SAFE_BEFORE", source_turn_uuid="survivor", source_run_uuid="survivor")
    await seed_turn(env, row, "future-1", "FUTURE_MESSAGE")
    await dao.update(old["memoryUuid"], conversation_uuid=conv, scope_type="conversation",
                     expected_revision=old["revision"], changes={"body": "FUTURE_UPDATED_PREFERENCE"})
    await dao.create(conversation_uuid=conv, scope_type="conversation", name="created after cut",
                     body="FUTURE_CREATED_PREFERENCE", source_turn_uuid="future-1", source_run_uuid="future-1")
    await truncate(env, row)
    memories = await sql_rows(env, "SELECT memory_uuid,body,revision,source_turn_uuid,source_run_uuid FROM conversation_task_memories WHERE conversation_uuid=?", (conv,))
    assert len(memories) == 2
    assert await run_web(env, row, "restart", "SAFE_NEW_REQUEST")
    sent = backend.seen_convos[-1]
    assert "FUTURE_MESSAGE" not in str(sent)
    assert "FUTURE_UPDATED_PREFERENCE" in str(sent) and "FUTURE_CREATED_PREFERENCE" in str(sent)
    assert "SAFE_BEFORE" not in str(sent)


async def test_suffix_clears_read_dedup_for_actual_next_request(web_env, monkeypatch, tmp_path):
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    await seed_turn(env, row, "survivor", "SAFE_CONTEXT", payload="OLD_TOOL")
    path = tmp_path / "input.txt"
    path.write_text("FILE_CONTENT_READ_IN_DELETED_TURN\n")
    state = FileStateStore()
    env.server.tools.file_state = state
    env.server.tools.add("Read", "Read the isolated fixture", {"type": "object", "properties": {"path": {"type": "string"}}}, make_read_tool(store=state))
    def read_call(call_id):
        return [StreamEvent(kind="tool_call", tool_calls=[ToolCall(call_id, "Read", json.dumps({"path": str(path)}))]),
                StreamEvent(kind="finish", finish_reason="tool_calls")]
    done = [StreamEvent(kind="content", text="finished"), StreamEvent(kind="finish", finish_reason="stop")]
    backend.scripts = [read_call("first"), done, read_call("second"), done]
    original = ContextManager.prepare
    forced = set()
    async def first_boundary(self, messages, **kwargs):
        if self.active_run_root_turn_uuid == "future-1" and self not in forced:
            forced.add(self)
            kwargs["force"] = True
        return await original(self, messages, **kwargs)
    monkeypatch.setattr(ContextManager, "prepare", first_boundary)
    assert await run_web(env, row, "future-1", "Read fixture once")
    first_result = [m for m in backend.seen_convos[1] if m.get("role") == "tool" and m.get("tool_call_id") == "first"][0]["content"]
    assert "FILE_CONTENT_READ_IN_DELETED_TURN" in first_result
    rotations = await sql_rows(env, "SELECT * FROM context_window_rotations WHERE owner_key LIKE ?", (f"controller:{row['internal_chat_id']}:%",))
    assert rotations
    await truncate(env, row)
    assert "FILE_CONTENT_READ_IN_DELETED_TURN" not in str(await env.server._build_history(row["internal_chat_id"]))
    assert await run_web(env, row, "restart", "Read the file now, from this corrected point")
    result = [m for m in backend.seen_convos[-1] if m.get("role") == "tool" and m.get("tool_call_id") == "second"][0]["content"]
    assert "文件未变化" not in result and "FILE_CONTENT_READ_IN_DELETED_TURN" in result
    assert "FILE_CONTENT_READ_IN_DELETED_TURN" in str(backend.seen_convos[-1])


async def test_suffix_cache_reset_is_committed_and_owner_scoped(web_env, monkeypatch, tmp_path):
    from app.tools.base import ToolRuntimeContext
    env = web_env
    await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    chat, conv = row["internal_chat_id"], row["conversation_uuid"]
    await seed_turn(env, row, "survivor", "safe")
    await seed_turn(env, row, "future-1", "delete")
    sid = await MessageDAO(env.db).get_or_create_session_uuid(chat)
    cache = FileStateStore()
    env.server.tools.file_state = cache
    env.server.tools.add("Read", "test", {"type": "object"}, make_read_tool(store=cache))
    path = tmp_path / "scope.txt"
    path.write_text("CURRENT_FILE_BODY")
    contexts = [
        ToolRuntimeContext(chat_id=chat, session_uuid=sid, source="web"),
        ToolRuntimeContext(chat_id=chat, session_uuid=sid, source="agent:test", agent_session_uuid="sibling", task_uuid="sibling-task"),
        ToolRuntimeContext(chat_id=chat-1, session_uuid="other", source="web"),
    ]
    async def read(ctx):
        return await env.server.tools.dispatch("Read", json.dumps({"path": str(path)}), context=ctx)
    for ctx in contexts:
        assert "CURRENT_FILE_BODY" in await read(ctx)
    real = env.server.rath_dao.delete_task_records
    async def fail(ids):
        raise RuntimeError("rollback cache test")
    monkeypatch.setattr(env.server.rath_dao, "delete_task_records", fail)
    response = await env.client.delete(f"/api/conversations/{conv}/turns/future-1/suffix",
                                       cookies={"openbear_web_session": await _login_cookie(env)})
    assert response.status == 500
    for ctx in contexts:
        assert "文件未变化" in await read(ctx)
    monkeypatch.setattr(env.server.rath_dao, "delete_task_records", real)
    await truncate(env, row)
    assert "CURRENT_FILE_BODY" in await read(contexts[0])
    for ctx in contexts[1:]:
        assert "文件未变化" in await read(ctx)


async def test_suffix_reference_budget_failure_preserves_original_stores(web_env, monkeypatch):
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    await seed_turn(env, row, "survivor", "Use [huge](openbear://ref/doc/1)",
                    answer="Necessary boundary proposal", ref="large reference body " * 5000)
    await seed_turn(env, row, "future-1", "delete this")
    tables = ["messages", "web_operations", "web_operation_messages", "summaries", "context_windows", "web_reference_bundles"]
    before = {t: await sql_rows(env, f"SELECT * FROM {t}") for t in tables}
    response = await env.client.delete(f"/api/conversations/{row['conversation_uuid']}/turns/future-1/suffix",
                                       cookies={"openbear_web_session": await _login_cookie(env)})
    body = await response.json()
    assert response.status == 409 and body["error"] == "restart_context_unavailable"
    assert "required_context_too_large" in body["message"]
    assert {t: await sql_rows(env, f"SELECT * FROM {t}") for t in tables} == before
    assert backend.calls == backend.complete_calls == 0
    assert env.server.config.models.providers["openai"].models[0].rollover_trigger_tokens == 8000


async def test_restart_raw_legacy_boundary_is_bounded_and_complete(web_env):
    from app.context.restart import controller_restart_selection
    from app.context.window import WindowPolicy
    dao = MessageDAO(web_env.db)
    row = await web_env.server._create_web_conversation(123)
    chat = row["internal_chat_id"]
    for i in range(30):
        await dao.add(chat, "user", f"old {i}")
        await dao.add(chat, "assistant", f"proposal {i}")
    await dao.add(chat, "user", "Last surviving legacy input")
    await dao.add(chat, "assistant", tool_calls=[ToolCall("paired", "Read", "{}")])
    await dao.add(chat, "tool", "kept whole result", tool_call_id="paired", name="Read")
    await dao.add(chat, "assistant", "LAST LEGACY PROPOSAL")
    cutoff = await dao.add(chat, "user", "future")
    selected = await controller_restart_selection(dao, chat, cutoff, policy=WindowPolicy(128000, trigger_tokens=8000))
    assert "LAST LEGACY PROPOSAL" in str(selected) and "kept whole result" in str(selected)
    assert "proposal 29" in str(selected) and "proposal 0" not in str(selected)
    assert sum(m.get("role") == "tool" for m in selected) == 1


def test_restart_confirmation_explains_persistent_side_effects():
    source = (Path(__file__).resolve().parents[1] / "web/src/views/consoleView/ConsoleView.vue").read_text()
    confirmation = source.split('async function deleteTurnSuffix(turn)', 1)[1].split('confirmButtonText:', 1)[0]
    assert "会话/实例便笺仍保留当前版本" in confirmation
    assert "不会撤销已发生的文件修改、配置变更、发送或其他现实副作用" in confirmation
    assert "可靠的存活检查点" in confirmation


async def test_agent_rewind_preserves_evicted_history_and_sibling(continuity_env, monkeypatch):
    from dataclasses import replace

    from app.context.window import WindowPolicy
    from tests.test_agent_continuity import call

    dao, reg, backend, ctx, _manager = continuity_env
    original = ContextManager.prepare
    async def bounded(self, messages, **kwargs):
        if self.active_run_root_turn_uuid == "surviving-root":
            self.policy = WindowPolicy(128000, trigger_tokens=16000)
            kwargs["force"] = True
        return await original(self, messages, **kwargs)
    monkeypatch.setattr(ContextManager, "prepare", bounded)
    first = await call(reg, "Agent", {"prompt": "EVICTED_OLD_ORIGINAL " + "prior standalone material "*900, "tools": []}, ctx)
    sid, t0 = first["agentSession"]["sessionUuid"], first["task"]["taskUuid"]
    second = await call(reg, "AgentContinue", {"to": sid, "prompt": "SURVIVING BRIEF", "tools": []},
                        replace(ctx, turn_uuid="surviving-root", run_root_turn_uuid="surviving-root"))
    t1 = second["task"]["taskUuid"]
    assert "EVICTED_OLD_ORIGINAL" not in str(backend.calls[-1]["messages"])
    await dao.delete_task_records([t0])
    await dao.db.conn.commit()
    sibling = await call(reg, "Agent", {"prompt": "SIBLING FACT", "tools": []}, ctx)
    other_sid = sibling["agentSession"]["sessionUuid"]
    other_head = await dao.task_model_context(sibling["task"]["taskUuid"])
    other_session = await dao.agent_session(other_sid)
    third = await call(reg, "AgentContinue", {"to": sid, "prompt": "REMOVED_FUTURE", "tools": []},
                       replace(ctx, turn_uuid="future-root", run_root_turn_uuid="future-root"))
    t2 = third["task"]["taskUuid"]
    async with dao.db.conn.transaction(label="test-suffix"):
        await dao.delete_task_suffix_records([t2], chat_id=ctx.chat_id, deleted_roots=["future-root"])
    assert await dao.task_model_context(sibling["task"]["taskUuid"]) == other_head
    assert await dao.agent_session(other_sid) == other_session
    store = WindowStore(dao.db, ContextOwner.agent(task_uuid=t1, agent_session_uuid=sid))
    assert (await store.index(query="EVICTED_OLD_ORIGINAL"))["events"]
    assert not (await store.index(query="REMOVED_FUTURE"))["events"]
    assert "REMOVED_FUTURE" not in str(await store.restore_messages())
    fourth = await call(reg, "AgentContinue", {"to": sid, "prompt": "SAFE NEXT", "tools": []}, ctx)
    assert fourth["status"] == "completed"
    assert fourth["task"]["sessionTurn"] == second["task"]["sessionTurn"] + 1
    assert "SURVIVING BRIEF" in str(backend.calls[-1]["messages"])
    assert "REMOVED_FUTURE" not in str(backend.calls[-1]["messages"])
