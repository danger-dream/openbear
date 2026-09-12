"""Copy/restart combinations preserve instance chronology, Plan archives and coverage.

HTTP APIs and real Controller/Agent runners use temporary SQLite and recording
backends. Internal DAO selection boundaries are explicitly distinguished below.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.context.builder import build_controller_history
from app.context.history import ControllerExecutionHistory
from app.context.store import ContextOwner, WindowStore
from app.context.window import source_of
from app.db.dao import MessageDAO, SummaryDAO
from app.llm.base import AgentResult
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.manager import RathTaskManager
from app.tools.agent_history import register_agent_history_tool
from app.tools.agents import register_agent_tools
from app.tools.base import ToolRegistry, ToolRuntimeContext
from tests.test_conversation_restart import configure_web, run_web, seed_turn
from tests.test_tools_agent_orchestration import _FakeConfig, _FakeFactory, _FakeSelection
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as shared_web_env

web_env = shared_web_env


@pytest.fixture(autouse=True)
async def stop_metadata_subscription(web_env):
    # No realtime UI assertions; leave no unrelated subscription at teardown.
    await web_env.server.global_realtime.close()




async def http(env, method, path):
    response = await getattr(env.client, method)(path, cookies={"openbear_web_session": await _login_cookie(env)})
    return response.status, await response.json()


async def duplicate(env, row):
    status, data = await http(env, "post", f"/api/conversations/{row['conversation_uuid']}/duplicate")
    assert status == 200, (status, data)
    return await env.server._conversation_row(123, data["conversation"]["conversationUuid"], require=True)


async def suffix(env, row, root):
    return await http(env, "delete", f"/api/conversations/{row['conversation_uuid']}/turns/{root}/suffix")


async def store(env, row):
    sid = await MessageDAO(env.db).get_or_create_session_uuid(row["internal_chat_id"])
    return WindowStore(env.db, ContextOwner.controller(chat_id=row["internal_chat_id"], session_uuid=sid, conversation_uuid=row["conversation_uuid"]))


async def rows(env, table, where="1", args=()):
    return [dict(r) for r in await (await env.db.conn.execute(f"SELECT * FROM {table} WHERE {where}", args)).fetchall()]


class AgentBackend:
    protocol = "chat"
    def __init__(self):
        self.calls = []
    async def complete(self, messages, *, tools=None, **kwargs):
        self.calls.append(copy.deepcopy({"protocol": self.protocol, "messages": messages, "tools": tools, **kwargs}))
        return AgentResult(text="LOCAL_AGENT_DONE")


async def agents(env):
    await ensure_builtin_workflows(env.server.rath_dao)
    reg, backend = ToolRegistry(), AgentBackend()
    async def noop(args):
        return "LOCAL_ONLY"
    for name in ("Read", "Bash", "Edit", "EditBatch"):
        reg.add(name, name, {"type": "object"}, noop)
    register_agent_history_tool(reg, env.db)
    manager = RathTaskManager(env.server.rath_dao)
    cfg = _FakeConfig()
    cfg.agent = cfg.agent.model_copy(deep=True)
    register_agent_tools(reg, config=cfg, dao=env.server.rath_dao, manager=manager,
                         llm_factory=_FakeFactory(backend), model_selection=_FakeSelection(), workspace_dir=str(Path(env.db.path).parent))
    return reg, backend, manager


async def call_agent(reg, row, root, *, agent_id="", prompt="", tools=None, runtime_hooks=None, **extra):
    return json.loads(await reg.dispatch("AgentContinue" if agent_id else "Agent", json.dumps({
        **({"to": agent_id} if agent_id else {}), "prompt": prompt, "tools": tools or [], **extra,
    }), context=ToolRuntimeContext(chat_id=row["internal_chat_id"], session_uuid=row["conversation_uuid"],
        conversation_uuid=row["conversation_uuid"], source="web", turn_uuid=root, run_root_turn_uuid=root, **(runtime_hooks or {}))))


@pytest.mark.parametrize("initial_copy", [False, True], ids=["restart-copy-continue", "copy-restart-copy-continue"])
@pytest.mark.parametrize("strategy,protocol", [("sliding_window", "chat"), ("model_summary", "responses")])
async def test_agent_combinations(web_env, monkeypatch, initial_copy, strategy, protocol):
    env = web_env
    await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt", run_config={"context_strategy": strategy})
    reg, backend, manager = await agents(env)
    await seed_turn(env, row, "safe", "SAFE_CONTROLLER", ref="SAFE_FROZEN")
    first = await call_agent(reg, row, "safe", prompt="SAFE_AGENT_ORIGIN", tools=["Read"])
    assert first["status"] == "completed", first
    sid = first["agentSession"]["agentId"]
    await seed_turn(env, row, "future", "FUTURE_CONTROLLER")
    future = await call_agent(reg, row, "future", agent_id=sid, prompt="FUTURE_AGENT_SECRET", tools=["Bash"])
    assert future["status"] == "completed", future
    untouched = row
    if initial_copy:
        row = await duplicate(env, row)
        sid = (await env.server.rath_dao.list_agent_sessions(openbear_session_uuid=row["conversation_uuid"]))[0].session_uuid
    status, result = await suffix(env, row, "future")
    assert status == 200, (status, result)
    reset_session = await env.server.rath_dao.agent_session(sid)
    cp = await duplicate(env, row)
    ni = (await env.server.rath_dao.list_agent_sessions(openbear_session_uuid=cp["conversation_uuid"]))[0]
    checkpoint = await env.server.rath_dao.task_model_context(ni.context_task_uuid)
    cp_window = await WindowStore(env.db, ContextOwner.agent(task_uuid=ni.context_task_uuid, agent_session_uuid=ni.session_uuid)).load()
    assert (checkpoint["state"]["windowVersion"], checkpoint["state"]["windowRevision"]) == (cp_window["window_version"], cp_window["revision"])
    backend.protocol = protocol
    continued_copy = await call_agent(reg, cp, "copy-next", agent_id=ni.session_uuid, prompt="COPY_ONLY_NEXT", tools=["Edit"])
    copy_request = copy.deepcopy(backend.calls[-1])
    continued_source = await call_agent(reg, row, "source-next", agent_id=sid, prompt="SOURCE_ONLY_NEXT", tools=["Read"])
    source_request = copy.deepcopy(backend.calls[-1])
    report = {"initialCopy": initial_copy, "strategy": strategy, "protocol": protocol,
        "sourceConversation": row, "copyConversation": cp, "sourceResetSession": reset_session,
        "copiedSession": ni, "copiedCheckpoint": checkpoint, "copiedWindowBeforeContinue": cp_window, "copyResult": continued_copy,
        "sourceResult": continued_source, "copyRequest": copy_request, "sourceRequest": source_request}
    history_ctx = ToolRuntimeContext(chat_id=cp["internal_chat_id"], conversation_uuid=cp["conversation_uuid"],
        source="agent:general-purpose", task_uuid=continued_copy["task"]["taskId"], agent_session_uuid=ni.session_uuid)
    report["copyHistorySafe"] = json.loads(await reg.dispatch("AgentHistory", '{"action":"search","query":"SAFE_AGENT_ORIGIN"}', context=history_ctx))
    report["copyHistoryFuture"] = json.loads(await reg.dispatch("AgentHistory", '{"action":"search","query":"FUTURE_AGENT_SECRET"}', context=history_ctx))
    report["wrongInstanceContinue"] = await call_agent(reg, cp, "cross", agent_id=sid, prompt="DENY_CROSS_OWNER")
    if initial_copy:
        report["untouchedSourceWindow"] = await (await store(env, untouched)).load()
        report["untouchedSourceFutureTask"] = await env.server.rath_dao.get_task(future["task"]["taskId"])
    assert continued_copy["status"] == continued_source["status"] == "completed"
    assert "SAFE_AGENT_ORIGIN" in str(copy_request) and "FUTURE_AGENT_SECRET" not in str(copy_request)
    assert "COPY_ONLY_NEXT" not in str(source_request) and "SOURCE_ONLY_NEXT" not in str(copy_request)
    names = {t["name"] for t in copy_request["tools"]}
    assert "Edit" in names and "Bash" not in names and "Read" not in names
    assert report["copyHistorySafe"]["events"] and not report["copyHistoryFuture"].get("events")
    assert not report["wrongInstanceContinue"].get("ok")
    assert checkpoint["sessionId"] == ""
    # Product expectation: source and copy must not share the opaque provider cache scope.
    assert copy_request["session_id"] != source_request["session_id"]
    assert copy_request["session_id"] == ni.metadata["llmSessionId"]
    another = await call_agent(reg, cp, "copy-again", agent_id=ni.session_uuid, prompt="COPY_AGAIN", tools=[])
    assert another["status"] == "completed"
    assert backend.calls[-1]["session_id"] == copy_request["session_id"]


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_controller_copy_restart_copy_repeat_suffix(web_env, monkeypatch, strategy):
    env = web_env
    backend = await configure_web(env, monkeypatch)
    source = await env.server._create_web_conversation(123, model="openai/gpt", run_config={"context_strategy": strategy})
    await seed_turn(env, source, "old", "OLD_RAW", answer="OLD_ANSWER")
    await seed_turn(env, source, "safe", "SAFE_INPUT [doc](openbear://ref/doc/1)", answer="SAFE_PLAN", ref="SAFE_REFERENCE")
    await seed_turn(env, source, "future", "FUTURE_INPUT", answer="FUTURE_ANSWER", ref="FUTURE_REFERENCE")
    a = await duplicate(env, source)
    assert (await suffix(env, a, "future"))[0] == 200
    state_before_repeat = await (await store(env, a)).load()
    repeat_status, repeat = await suffix(env, a, "future")
    assert repeat_status == 404 and repeat["error"] == "turn_not_found"
    assert state_before_repeat == await (await store(env, a)).load()
    b = await duplicate(env, a)
    c = await duplicate(env, b)
    selected = await env.server._build_history(c["internal_chat_id"])
    assert "FUTURE_INPUT" not in str(selected) and "SAFE_PLAN" in str(selected)
    assert await run_web(env, c, "c-next", "COPY_BRANCH_REQUEST")
    copy_request = copy.deepcopy(backend.seen_convos[-1])
    assert "SAFE_REFERENCE" in str(copy_request) and "FUTURE_REFERENCE" not in str(copy_request)
    assert await run_web(env, source, "source-next", "SOURCE_BRANCH_REQUEST")
    source_request = copy.deepcopy(backend.seen_convos[-1])
    assert "FUTURE_INPUT" in str(source_request) and "COPY_BRANCH_REQUEST" not in str(source_request)
    assert (await suffix(env, b, "safe"))[0] == 200
    assert "SAFE_PLAN" in str(await env.server._build_history(c["internal_chat_id"]))
    for row in (source, a, b):
        assert (await http(env, "delete", f"/api/conversations/{row['conversation_uuid']}"))[0] == 200
    latest = await env.server._build_history(c["internal_chat_id"])
    assert "SAFE_PLAN" in str(latest)
    hist = ControllerExecutionHistory(env.db, chat_id=c["internal_chat_id"], session_uuid=(await store(env, c)).owner.session_uuid)
    originals = await hist.index(query="OLD_RAW")
    assert originals["events"]
    original = await hist.event_payload(originals["events"][0]["event_id"])
    assert original["payload"]["content"] == "OLD_RAW"


async def test_empty_first_turn_restart_then_copy(web_env, monkeypatch):
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    await seed_turn(env, row, "first", "ALL_DELETED")
    assert (await suffix(env, row, "first"))[0] == 200
    cp = await duplicate(env, row)
    assert await env.server._build_history(cp["internal_chat_id"]) == []
    assert await run_web(env, cp, "new", "FRESH_REQUEST")
    assert "ALL_DELETED" not in str(backend.seen_convos[-1])


async def test_summary_lineage_copy_restart_copy(web_env, monkeypatch):
    from app.llm.events import StreamEvent, Usage
    from tests.test_conversation_restart import SUMMARY
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt", run_config={"context_strategy": "model_summary"})
    for i in range(4):
        await seed_turn(env, row, f"old-{i}", f"OLD_ORIGINAL_{i}", answer=f"OLD_RESULT_{i}")
    # Genuine next-run provider usage enables the manual HTTP API (no DB usage edit).
    backend.scripts = [[StreamEvent(kind="content", text="SAFE_PROPOSAL"), StreamEvent(kind="usage", usage=Usage(input_tokens=6000)), StreamEvent(kind="finish", finish_reason="stop")]]
    assert await run_web(env, row, "safe", "SAFE_WITH_REF [doc](openbear://ref/doc/1)", ref="SAFE_LINEAGE_REF")
    backend.summary = SUMMARY.replace("SUMMARY_BODY", "SAFE_SUMMARY")
    safe_status, safe_compact = await http(env, "post", f"/api/conversations/{row['conversation_uuid']}/compact")
    assert safe_status == 200, safe_compact
    await seed_turn(env, row, "future", "FUTURE_LINEAGE_TEXT", answer="FUTURE_LINEAGE_RESULT")
    assert await run_web(env, row, "future-2", "FUTURE_LINEAGE_NEXT")
    backend.summary = SUMMARY.replace("SUMMARY_BODY", "FUTURE_LINEAGE_SUMMARY")
    future_status, future_compact = await http(env, "post", f"/api/conversations/{row['conversation_uuid']}/compact")
    assert future_status == 200, future_compact
    original_summaries = await SummaryDAO(env.db).list_with_anchors(row["internal_chat_id"])
    cp = await duplicate(env, row)
    copied_summaries = await SummaryDAO(env.db).list_with_anchors(cp["internal_chat_id"])
    assert len(original_summaries) == len(copied_summaries) == 2
    assert {s["id"] for s in original_summaries}.isdisjoint(s["id"] for s in copied_summaries)
    status, deleted = await suffix(env, cp, "future")
    assert status == 200, deleted
    descendants = await duplicate(env, cp)
    remaining = await SummaryDAO(env.db).list_with_anchors(descendants["internal_chat_id"])
    assert remaining and "FUTURE_LINEAGE_SUMMARY" not in str(remaining)
    assert await run_web(env, descendants, "new", "SAFE_NEXT_AFTER_COPY")
    sent = backend.seen_convos[-1]
    assert "FUTURE_LINEAGE_" not in str(sent) and "SAFE_LINEAGE_REF" in str(sent)
    source_history = await env.server._build_history(row["internal_chat_id"])
    assert "FUTURE_LINEAGE_" in str(source_history)


async def test_legacy_summary_only_copy_restart_copy(web_env, monkeypatch):
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt", run_config={"context_strategy": "model_summary"})
    _, boundary = await seed_turn(env, row, "safe", "ORIGINAL_BEFORE_LEGACY_SUMMARY", answer="SAFE_LEGACY_PROPOSAL")
    # Supported legacy adoption: all raw rows covered, no raw tail. No invented window state.
    await SummaryDAO(env.db).add(row["internal_chat_id"], "LEGACY_SUMMARY_ONLY", boundary, 10)
    st = await store(env, row)
    selected = await build_controller_history(MessageDAO(env.db), row["internal_chat_id"])
    assert len(selected) == 1 and source_of(selected[0])["kind"] == "summary"
    archive = await st.archive(selected)
    await st.save(selected, expected_revision=archive["revision"], expected_source_revision=archive["sourceRevision"], route="")
    first = await duplicate(env, row)
    assert len(await env.server._build_history(first["internal_chat_id"])) == 1
    await seed_turn(env, first, "future", "FUTURE_LEGACY_TAIL")
    assert (await suffix(env, first, "future"))[0] == 200
    second = await duplicate(env, first)
    assert await run_web(env, second, "new", "CONTINUE_LEGACY")
    assert "FUTURE_LEGACY_TAIL" not in str(backend.seen_convos[-1])
    assert "SAFE_LEGACY_PROPOSAL" in str(backend.seen_convos[-1])


async def test_managed_plan_archive_restart_copy_continue(web_env, monkeypatch):
    import asyncio

    from app.llm.events import ToolCall
    from tests.test_rath_plan import sample_plan
    env = web_env
    await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    await seed_turn(env, row, "safe", "SAFE_MANAGED_CONTROLLER")
    monkeypatch.setattr(_FakeConfig.rath, "agent_plan_enabled", True)
    reg, backend, manager = await agents(env)
    env.server.rath = manager
    from app.tools.base import current_tool_context
    async def recorded_read(args):
        context = current_tool_context()
        artifact_uuid = await env.server.rath_dao.create_artifact(
            context.task_uuid, kind="local_read_evidence", name="observed fixture", content="LOCAL_ONLY",
        )
        return json.dumps({"observed": "LOCAL_ONLY", "artifactUuid": artifact_uuid, "taskUuid": context.task_uuid})
    reg.add("Read", "Read fixture and record its evidence", {"type": "object"}, recorded_read)
    plan = sample_plan(title="Retain immutable local fact", second_step=False)
    plan["objective"] = f"Read one local synthetic fact in conversation {row['conversation_uuid']}; no product effects"
    plan["steps"][0].update(title="Read fact", objective="Inspect fixture", method="Read local no-op tool")
    sequence = [
        ("AgentPlanSubmit", {"plan": plan, "requestId": "submit"}),
        ("AgentPlanProgress", {"action": "start", "stepId": "s1", "requestId": "start"}),
        ("Read", {}),
        ("AgentPlanProgress", {"action": "complete", "stepId": "s1", "requestId": "complete", "result": "Observed LOCAL_ONLY", "criteria": [{"id": "c1", "status": "satisfied"}], "evidence": [{"type": "tool_result", "reference": "Read: LOCAL_ONLY", "summary": "Actual no-op Read result", "criterionId": "c1"}]}),
        ("AgentPlanProgress", {"action": "finalize", "requestId": "finalize", "finalOutputs": [{"id": "o1", "summary": "LOCAL_ONLY observed", "sources": ["step:s1"]}]}),
    ]
    recorded = backend.complete
    async def scripted(messages, *, tools=None, **kwargs):
        await recorded(messages, tools=tools, **kwargs)
        if sequence:
            name, args = sequence.pop(0)
            if name == "AgentPlanProgress" and args["action"] == "complete":
                observed = json.loads(next(m["content"] for m in reversed(messages)
                                           if m.get("role") == "tool" and m.get("name") == "Read"))
                args["evidence"][0]["reference"] = f"artifact:{observed['artifactUuid']}"
                args["evidence"][0]["metadata"] = {"artifactUuid": observed["artifactUuid"], "taskUuid": observed["taskUuid"]}
            if name == "AgentPlanProgress" and args["action"] == "finalize":
                completed = json.loads(next(m["content"] for m in reversed(messages)
                                            if m.get("role") == "tool" and m.get("name") == "AgentPlanProgress"))
                evidence_uuid = completed["evidence"][0]["evidenceUuid"]
                args["finalOutputs"][0]["sources"] = [f"evidence:{evidence_uuid}"]
            return AgentResult(tool_calls=[ToolCall(f"managed-{len(sequence)}", name, json.dumps(args))])
        return AgentResult(text="SAFE_MANAGED_DONE")
    monkeypatch.setattr(backend, "complete", scripted)
    notifications = []
    async def notification(payload):
        notifications.append(copy.deepcopy(payload))
    async def wait(*args, **kwargs):
        pass
    launch = asyncio.create_task(call_agent(reg, row, "safe", prompt="SAFE_MANAGED_ORIGIN", tools=["Read"], planMode="managed", runtime_hooks={"task_notification": notification, "agent_wait": wait}))
    tid = ""
    try:
        async with asyncio.timeout(10):
            while True:
                pending = await rows(env, "rath_task_plan_state", "phase='awaiting_plan_decision'")
                if pending:
                    tid = pending[0]["task_uuid"]
                    break
                await asyncio.sleep(.02)
        decision = json.loads(await reg.dispatch("AgentPlanDecision", json.dumps({"taskUuid": tid, "action": "approve", "expectedPlanVersion": 1, "requestId": "approve", "reason": "Read-only fixture plan within scope"}), context=ToolRuntimeContext(chat_id=row["internal_chat_id"], session_uuid=row["conversation_uuid"], conversation_uuid=row["conversation_uuid"], source="web")))
        assert decision.get("ok"), decision
        await asyncio.wait_for(launch, 10)
        running = manager.task(tid)
        if running:
            await asyncio.wait_for(running, 10)
    finally:
        if not launch.done():
            launch.cancel()
            await asyncio.gather(launch, return_exceptions=True)
        for running in list(manager._runs.values()):
            if not running.done():
                running.cancel()
        if manager._runs:
            await asyncio.gather(*manager._runs.values(), return_exceptions=True)
    actual = await env.server.rath_dao.get_task(tid)
    assert actual.status == "completed", actual
    assert not sequence
    sid = actual.agent_session_uuid
    source_plan_before = await manager.plan_coordinator.snapshot(tid)
    assert source_plan_before["state"]["phase"] == "finalizing"
    assert source_plan_before["versions"] and source_plan_before["evidence"]
    monkeypatch.setattr(backend, "complete", recorded)
    await seed_turn(env, row, "future", "FUTURE_MANAGED_CONTROLLER")
    future = await call_agent(reg, row, "future", agent_id=sid, prompt="FUTURE_MANAGED_CONTEXT", tools=["Bash"], planMode="direct")
    assert future["status"] == "completed", future
    assert (await suffix(env, row, "future"))[0] == 200
    source_status, source_plan = await http(env, "get", f"/api/conversations/{row['conversation_uuid']}/agents/{tid}/plan")
    assert source_status == 200, source_plan
    # Fail after several Plan tables/windows have copied: every table rolls back.
    tables = ["web_conversations", "messages", "context_windows", "context_execution_events", "rath_tasks",
              "rath_agent_sessions", "rath_task_plan_state", "rath_task_plan_versions", "rath_task_plan_decisions",
              "rath_task_plan_step_runs", "rath_task_plan_evidence", "rath_task_plan_requests"]
    before = {table: await rows(env, table) for table in tables}
    real_copy = env.server._copy_table_rows_for_duplicate
    async def fail_plan_copy(table, *args, **kwargs):
        if table == "rath_task_plan_evidence":
            raise RuntimeError("injected Plan copy failure")
        return await real_copy(table, *args, **kwargs)
    monkeypatch.setattr(env.server, "_copy_table_rows_for_duplicate", fail_plan_copy)
    with pytest.raises(RuntimeError, match="injected Plan copy failure"):
        await env.server._duplicate_web_conversation_data(row)
    assert {table: await rows(env, table) for table in tables} == before
    monkeypatch.setattr(env.server, "_copy_table_rows_for_duplicate", real_copy)
    cp = await duplicate(env, row)
    ni = (await env.server.rath_dao.list_agent_sessions(openbear_session_uuid=cp["conversation_uuid"]))[0]
    copied_status, copied_plan = await http(env, "get", f"/api/conversations/{cp['conversation_uuid']}/agents/{ni.context_task_uuid}/plan")
    assert copied_status == 200, copied_plan
    backend.protocol = "responses"
    continued = await call_agent(reg, cp, "next", agent_id=ni.session_uuid, prompt="NEW_DIRECT_READ_ONLY", tools=["Edit"], planMode="direct")
    request = backend.calls[-1]
    assert continued["status"] == "completed", continued
    assert "SAFE_MANAGED_ORIGIN" in str(request) and "FUTURE_MANAGED_CONTEXT" not in str(request)
    assert {t["name"] for t in request["tools"]}.isdisjoint({"Read", "Bash", "AgentPlanProgress"})
    assert copied_plan["versions"]
    for key in ("versions", "decisions", "steps", "evidence"):
        assert len(copied_plan[key]) == len(source_plan[key]) == 1
    assert copied_plan["state"]["phase"] == source_plan["state"]["phase"]
    assert copied_plan["state"]["active_plan_version"] == 1
    assert copied_plan["evidence"][0]["evidence_uuid"] != source_plan["evidence"][0]["evidence_uuid"]
    assert copied_plan["decisions"][0]["decision_uuid"] != source_plan["decisions"][0]["decision_uuid"]
    import hashlib
    evidence_uuid = copied_plan["evidence"][0]["evidence_uuid"]
    artifacts = await env.server.rath_dao.artifacts(ni.context_task_uuid, kind="local_read_evidence")
    assert len(artifacts) == 1 and artifacts[0].content == "LOCAL_ONLY"
    assert copied_plan["evidence"][0]["reference"] == f"artifact:{artifacts[0].artifact_uuid}"
    assert copied_plan["evidence"][0]["metadata"] == {"artifactUuid": artifacts[0].artifact_uuid, "taskUuid": ni.context_task_uuid}
    assert copied_plan["state"]["final_outputs_state"]["o1"]["sources"] == [f"evidence:{evidence_uuid}"]
    assert evidence_uuid in str(copied_plan["steps"][0]["criteria_state"])
    document = copied_plan["versions"][0]["plan"]
    assert cp["conversation_uuid"] in document["objective"] and row["conversation_uuid"] not in str(document)
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    new_hash = hashlib.sha256(canonical.encode()).hexdigest()
    assert copied_plan["versions"][0]["plan_hash"] == new_hash
    events = await env.server.rath_dao.events(ni.context_task_uuid)
    assert any(e.detail.get("planHash") == new_hash for e in events if e.kind == "plan_submitted")
    old_hash = source_plan["versions"][0]["plan_hash"]
    probe = {"planHash": old_hash, "body": f"Literal audit digest {old_hash}"}
    mapped = env.server._rewrite_duplicate_json_obj(probe, [], old_internal_chat_id=-1,
                                                    new_internal_chat_id=-2, plan_hash_map={old_hash: new_hash})
    assert mapped == {"planHash": new_hash, "body": probe["body"]}
    requests = await rows(env, "rath_task_plan_requests", "task_uuid=?", (ni.context_task_uuid,))
    assert len(requests) == 3
    assert all(json.loads(r["result_json"])["taskUuid"] == ni.context_task_uuid for r in requests)
    assert evidence_uuid in str(requests) and source_plan["evidence"][0]["evidence_uuid"] not in str(requests)
    assert not await rows(env, "rath_task_plan_versions", "task_uuid=?", (continued["task"]["taskId"],))
    # A second copy and deletion of both ancestors must not alias the Plan graph.
    grandchild = await duplicate(env, cp)
    inherited = (await rows(env, "rath_tasks", "chat_id=? AND json_extract(input_json,'$.planMode')='managed'", (grandchild["internal_chat_id"],)))[0]
    for ancestor in (row, cp):
        assert (await http(env, "delete", f"/api/conversations/{ancestor['conversation_uuid']}"))[0] == 200
    status, last_plan = await http(env, "get", f"/api/conversations/{grandchild['conversation_uuid']}/agents/{inherited['task_uuid']}/plan")
    assert status == 200
    last_evidence = last_plan["evidence"][0]["evidence_uuid"]
    assert last_evidence != evidence_uuid
    assert last_plan["state"]["final_outputs_state"]["o1"]["sources"] == [f"evidence:{last_evidence}"]


@pytest.mark.parametrize("selection_shape", ["tail_omitted", "summary_only", "empty"])
async def test_copy_preserves_internal_restart_selection_high_water(web_env, monkeypatch, selection_shape):
    """A supported DAO API boundary, NOT claimed to be produced by the current HTTP selector.

    The suffix DAO explicitly supports finite/summary-only/empty restart selections
    covering a larger surviving raw prefix. Do not hide this reachability limitation.
    """
    from app.context.window import mark_source
    from app.llm.events import ToolCall
    env = web_env
    backend = await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    dao = MessageDAO(env.db)
    chat, conv = row["internal_chat_id"], row["conversation_uuid"]
    _, safe_answer = await seed_turn(env, row, "safe", "SAFE_PREFIX", answer="SAFE_PREFIX_RESULT")
    await dao.add(chat, "assistant", tool_calls=[ToolCall("old-read", "Read", "{}")], conversation_uuid=conv, turn_uuid="safe", run_root_turn_uuid="safe")
    high = await dao.add(chat, "tool", "RAW_TAIL_OUTSIDE_SELECTION", name="Read", tool_call_id="old-read", conversation_uuid=conv, turn_uuid="safe", run_root_turn_uuid="safe")
    cutoff, _ = await seed_turn(env, row, "future", "FUTURE_CUTOFF")
    history = await build_controller_history(dao, chat)
    if selection_shape == "summary_only":
        sid = await SummaryDAO(env.db).add(chat, "SELECTED_SUMMARY", safe_answer, 10)
        selected = [mark_source({"role": "user", "content": "SELECTED_SUMMARY"}, kind="summary", source_id=f"legacy-summary:{sid}", message_id=safe_answer, summary_id=sid)]
    elif selection_shape == "empty":
        selected = []
    else:
        selected = history[:2]
    # This is the exact public Python DAO API, no SQL edits to context_windows.
    await dao.delete_from_message_id(chat, cutoff, restart_messages=selected)
    original_window = await (await store(env, row)).load()
    original_history = await build_controller_history(dao, chat)
    assert original_window["state"]["sourceMessageHighWater"] == high
    assert "RAW_TAIL_OUTSIDE_SELECTION" not in str(original_history)
    source_store = await store(env, row)
    # A no-new-content save must not regress suffix's larger trusted coverage.
    await source_store.save(selected, expected_revision=original_window["revision"],
                            expected_source_revision=original_window["source_revision"], route="")
    assert (await source_store.load())["state"]["sourceMessageHighWater"] == high
    await seed_turn(env, row, "pending", "UNCONSUMED_COPY_TAIL", answer="PENDING_RESULT")
    cp = await duplicate(env, row)
    copied_window = await (await store(env, cp)).load()
    copied_history = await build_controller_history(dao, cp["internal_chat_id"])
    copied_raw = await rows(env, "messages", "chat_id=?", (cp["internal_chat_id"],))
    mapped_high = next(m["id"] for m in copied_raw if m["content"] == "RAW_TAIL_OUTSIDE_SELECTION")
    assert copied_window["state"]["sourceMessageHighWater"] == mapped_high < max(m["id"] for m in copied_raw)
    assert "RAW_TAIL_OUTSIDE_SELECTION" not in str(copied_history)
    assert "UNCONSUMED_COPY_TAIL" in str(copied_history)
    copy_store = await store(env, cp)
    await copy_store.save(await copy_store.restore_messages(), expected_revision=copied_window["revision"],
                          expected_source_revision=copied_window["source_revision"], route="")
    assert (await copy_store.load())["state"]["sourceMessageHighWater"] == mapped_high
    assert await run_web(env, cp, "new", "NEXT_REQUEST")
    sent = backend.seen_convos[-1]
    tail_reentered = "RAW_TAIL_OUTSIDE_SELECTION" in str(sent)
    assert not tail_reentered
    assert "UNCONSUMED_COPY_TAIL" in str(sent)


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_agent_rotated_history_copy_restart_copy(web_env, monkeypatch, strategy):
    from app.context.runtime import ContextManager
    from app.context.window import WindowPolicy
    from app.llm.events import ToolCall
    from tests.test_conversation_restart import SUMMARY
    env = web_env
    await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt", run_config={"context_strategy": strategy})
    monkeypatch.setattr(_FakeConfig.agent, "keep_recent_messages", 1)
    reg, backend, manager = await agents(env)
    async def old_read(args):
        return "EVICTED_AGENT_ORIGINAL " + "local archived material " * 1500
    reg.add("Read", "Read", {"type": "object"}, old_read)
    original_prepare = ContextManager.prepare
    forced = set()
    async def rotate(self, messages, **kwargs):
        if self.store.owner.kind == "agent" and self.active_run_root_turn_uuid in {"safe", "future"}:
            self.policy = WindowPolicy(128000, trigger_tokens=16000)
            if self not in forced:
                forced.add(self)
                kwargs["force"] = True
        return await original_prepare(self, messages, **kwargs)
    monkeypatch.setattr(ContextManager, "prepare", rotate)
    recorded = backend.complete
    stage = 0
    summaries = []
    async def scripted(messages, *, tools=None, **kwargs):
        nonlocal stage
        if tools is None:
            marker = "FUTURE_AGENT_SUMMARY" if "FUTURE_AGENT_INPUT" in str(messages) else "SAFE_AGENT_SUMMARY"
            summaries.append(copy.deepcopy(messages))
            return AgentResult(text=SUMMARY.replace("SUMMARY_BODY", marker))
        await recorded(messages, tools=tools, **kwargs)
        stage += 1
        if stage == 1:
            return AgentResult(tool_calls=[ToolCall("old-read", "Read", "{}")])
        return AgentResult(text="LOCAL_COMPLETED")
    monkeypatch.setattr(backend, "complete", scripted)
    await seed_turn(env, row, "old", "OLD_CONTROLLER")
    first = await call_agent(reg, row, "old", prompt="OLD_AGENT_TASK", tools=["Read"])
    assert first["status"] == "completed", first
    sid = first["agentSession"]["agentId"]
    await seed_turn(env, row, "safe", "SAFE_CONTROLLER")
    second = await call_agent(reg, row, "safe", agent_id=sid, prompt="SAFE_AGENT_TASK", tools=[])
    assert second["status"] == "completed", second
    safe_request = copy.deepcopy(backend.calls[-1])
    assert "EVICTED_AGENT_ORIGINAL" not in str(safe_request)
    await seed_turn(env, row, "future", "FUTURE_CONTROLLER")
    future = await call_agent(reg, row, "future", agent_id=sid, prompt="FUTURE_AGENT_INPUT", tools=[])
    assert future["status"] == "completed", future
    first_copy = await duplicate(env, row)
    assert (await suffix(env, first_copy, "future"))[0] == 200
    second_copy = await duplicate(env, first_copy)
    ni = (await env.server.rath_dao.list_agent_sessions(openbear_session_uuid=second_copy["conversation_uuid"]))[0]
    backend.protocol = "responses"
    continued = await call_agent(reg, second_copy, "new", agent_id=ni.session_uuid, prompt="AFTER_REWIND_NEXT", tools=[])
    assert continued["status"] == "completed", continued
    request = backend.calls[-1]
    owner = WindowStore(env.db, ContextOwner.agent(task_uuid=ni.context_task_uuid, agent_session_uuid=ni.session_uuid))
    indexed = await owner.index(query="EVICTED_AGENT_ORIGINAL")
    assert indexed["events"]
    payload = await owner.event_payload(indexed["events"][0]["event_id"])
    assert "EVICTED_AGENT_ORIGINAL" in str(payload)
    for ancestor in (row, first_copy):
        assert (await http(env, "delete", f"/api/conversations/{ancestor['conversation_uuid']}"))[0] == 200
    assert await owner.event_payload(indexed["events"][0]["event_id"]) == payload
    evicted_reentered = "EVICTED_AGENT_ORIGINAL" in str(request)
    assert not evicted_reentered, "copied suffix selected older task checkpoint and rehydrated evicted material"
    assert "SAFE_AGENT_TASK" in str(request) and "FUTURE_AGENT_INPUT" not in str(request) and "FUTURE_AGENT_SUMMARY" not in str(request)


@pytest.mark.parametrize("copy_depth", [1, 2])
async def test_agent_task_order_copy_suffix_minimal(web_env, monkeypatch, copy_depth):
    """No forced rotation, no malformed rows, all Agent/Continue tasks really complete."""
    env = web_env
    await configure_web(env, monkeypatch)
    source = await env.server._create_web_conversation(123, model="openai/gpt")
    reg, backend, manager = await agents(env)
    sid = ""
    for root, prompt in [("old", "OLD_REJECTED_APPROACH"), ("safe", "SAFE_CONFIRMED_CORRECTION"), ("future", "FUTURE_REMOVED")]:
        await seed_turn(env, source, root, f"CONTROLLER_{root}")
        outcome = await call_agent(reg, source, root, agent_id=sid, prompt=prompt, tools=[])
        assert outcome["status"] == "completed", outcome
        sid = outcome["agentSession"]["agentId"]
    source_tasks = await rows(env, "rath_tasks", "chat_id=? ORDER BY id", (source["internal_chat_id"],))
    current = source
    for _ in range(copy_depth):
        current = await duplicate(env, current)
    copied_tasks = await rows(env, "rath_tasks", "chat_id=? ORDER BY id", (current["internal_chat_id"],))
    assert (await suffix(env, current, "future"))[0] == 200
    ni = (await env.server.rath_dao.list_agent_sessions(openbear_session_uuid=current["conversation_uuid"]))[0]
    checkpoint = await env.server.rath_dao.task_model_context(ni.context_task_uuid)
    assert "SAFE_CONFIRMED_CORRECTION" in str(checkpoint["state"]["messages"])
    backend.protocol = "responses"
    continued = await call_agent(reg, current, "next", agent_id=ni.session_uuid, prompt="USE_SAFE_CORRECTION_NOW", tools=[])
    assert continued["status"] == "completed", continued
    request = backend.calls[-1]
    def task_order(items):
        return [{"id": t["id"], "taskUuid": t["task_uuid"], "root": t["run_root_turn_uuid"], "sessionTurn": json.loads(t["input_json"])["sessionTurn"], "updatedAt": t["updated_at"]} for t in items]
    own_history = WindowStore(env.db, ContextOwner.agent(task_uuid=ni.context_task_uuid, agent_session_uuid=ni.session_uuid))
    safe_history = await own_history.index(query="SAFE_CONFIRMED_CORRECTION")
    assert "FUTURE_REMOVED" not in str(request)
    safe_present = "SAFE_CONFIRMED_CORRECTION" in str(request)
    assert safe_present
    assert safe_history["events"]
    assert [t["sessionTurn"] for t in task_order(source_tasks)] == [t["sessionTurn"] for t in task_order(copied_tasks)] == [1, 2, 3]
    assert continued["task"]["sessionTurn"] == 3


@pytest.mark.parametrize("checkpoint_mode", ["present", "missing_latest", "ambiguous_rounds"])
async def test_suffix_uses_logical_rounds_when_legacy_copy_ids_are_reversed(web_env, monkeypatch, checkpoint_mode):
    """Compatibility fixture only: reproduce an older copier's reversed physical IDs."""
    env = web_env
    await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    reg, backend, _ = await agents(env)
    sid = ""
    tids = []
    for root, prompt in [("old", "OLD_SAFE"), ("safe", "LATEST_SAFE"), ("future", "REMOVED_FUTURE")]:
        await seed_turn(env, row, root, root)
        result = await call_agent(reg, row, root, agent_id=sid, prompt=prompt)
        assert result["status"] == "completed"
        sid = result["agentSession"]["agentId"]
        tids.append(result["task"]["taskId"])
    # Only physical IDs change; real task inputs, checkpoints and History remain.
    await env.db.conn.execute("UPDATE rath_tasks SET id=1000-id WHERE chat_id=?", (row["internal_chat_id"],))
    if checkpoint_mode == "missing_latest":
        await env.server.rath_dao.clear_task_model_context(tids[1])
    elif checkpoint_mode == "ambiguous_rounds":
        await env.db.conn.execute("UPDATE rath_tasks SET input_json=json_set(input_json,'$.sessionTurn',1) WHERE task_uuid=?", (tids[1],))
    await env.db.conn.commit()
    assert (await suffix(env, row, "future"))[0] == 200
    instance = await env.server.rath_dao.agent_session(sid)
    before_calls = len(backend.calls)
    continued = await call_agent(reg, row, "next", agent_id=sid, prompt="NEXT")
    if checkpoint_mode != "present":
        assert instance.context_task_uuid == ""
        assert continued["error"] == "agent_context_unavailable"
        assert len(backend.calls) == before_calls
        return
    assert instance.context_task_uuid == tids[1] and instance.turn_count == 2
    assert continued["status"] == "completed" and continued["task"]["sessionTurn"] == 3
    assert "LATEST_SAFE" in str(backend.calls[-1]) and "REMOVED_FUTURE" not in str(backend.calls[-1])


async def test_copy_maps_committed_watermark_not_unconsumed_archived_tail(web_env, monkeypatch):
    env = web_env
    await configure_web(env, monkeypatch)
    row = await env.server._create_web_conversation(123, model="openai/gpt")
    await seed_turn(env, row, "safe", "COMMITTED_INPUT")
    dao = MessageDAO(env.db)
    source = await store(env, row)
    selected = await build_controller_history(dao, row["internal_chat_id"])
    archived = await source.archive(selected)
    await source.save(selected, expected_revision=archived["revision"], expected_source_revision=archived["sourceRevision"], route="")
    high = (await source.load())["state"]["sourceMessageHighWater"]
    await seed_turn(env, row, "pending", "UNCONSUMED_ARCHIVED_INPUT")
    # Archive is append-only, but a selection commit has not consumed this tail.
    await source.archive(await build_controller_history(dao, row["internal_chat_id"]))
    assert (await source.load())["state"]["sourceMessageHighWater"] == high
    cp = await duplicate(env, row)
    restored = await build_controller_history(dao, cp["internal_chat_id"])
    assert "UNCONSUMED_ARCHIVED_INPUT" in str(restored)
    raw = await rows(env, "messages", "chat_id=? ORDER BY id", (cp["internal_chat_id"],))
    assert (await (await store(env, cp)).load())["state"]["sourceMessageHighWater"] == raw[1]["id"] < raw[-1]["id"]
