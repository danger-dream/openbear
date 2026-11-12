"""AgentWait delivery cursors and bounded supervision queries; local fake data only."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aiosqlite
import pytest

from app.agents.controller_projection import project_agent_payload_for_controller
from app.agents.dao import AgentDAO
from app.agents.supervision import restore_review_state, review_observation
from app.db.dao import MessageDAO
from app.db.engine import DB
from app.llm.events import StreamEvent, ToolCall
from app.tools.base import ToolRegistry, current_tool_context
from app.web_admin import _WebStreamRenderer
from tests.test_rath_single_agent import env
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _cfg, web_env


async def _controller_setup(web_env, monkeypatch):
    server = web_env.server
    server.config = _cfg()
    server.model_selection = SimpleNamespace(current="openai/gpt")
    tools = ToolRegistry()

    async def wait(args):
        return await current_tool_context().agent_wait(args)

    tools.add("AgentWait", "wait", {"type": "object", "properties": {}}, wait, preserve_result=True)
    server.tools = tools

    async def prompt():
        return "sys"

    monkeypatch.setattr(server, "_build_system_prompt_for_chat", prompt)
    monkeypatch.setattr(server, "_ensure_web_task_notification_worker", lambda *_a, **_k: None)
    return await server._create_web_conversation(123, title="review pages", model="openai/gpt")


async def _run_reviews(server, row, *, root, run, count):
    ids = [f"{run}-wait-{i}" for i in range(count)]
    backend = FakeStreamBackend([
        *[[StreamEvent(kind="tool_call", tool_calls=[ToolCall(
            id=call_id, name="AgentWait", arguments='{"mode":"event_only"}')]),
           StreamEvent(kind="finish", finish_reason="tool_calls")] for call_id in ids],
        [StreamEvent(kind="content", text="reviewed"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    server.llm_factory = FakeRunFactory(backend, context_window=128000)
    live = server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": root, "runUuid": run})
    assert await asyncio.wait_for(server._run_web_turn(
        int(row["internal_chat_id"]), "review recorded facts", _WebStreamRenderer(live),
        conversation=row, root_turn_uuid=root,
    ), timeout=5)
    by_id = {m.get("tool_call_id"): json.loads(m["content"]) for m in backend.seen_convos[-1]
             if m.get("role") == "tool" and m.get("tool_call_id") in ids}
    assert set(by_id) == set(ids)
    return [by_id[call_id] for call_id in ids]


async def _completed_task(web_env, row, root, *, controls=1):
    dao = web_env.server.agent_dao
    task = await dao.create_task(chat_id=int(row["internal_chat_id"]),
        parent_session_uuid=row["conversation_uuid"], workflow_uuid="wf", title="review task",
        status="running", turn_uuid=root, run_root_turn_uuid=root)
    for i in range(120):
        await dao.append_event(task, "model_stream_progress", detail={"textChars": i})
    important = []
    for i in range(controls):
        important.append(await dao.append_event(task, "control_response", summary="Scope conflict",
            detail={"controlUuid": f"control-{i}", "status": "rejected", "reason": "scope conflict"}))
    last = await dao.append_event(task, "task_completed", summary="Completed original scope")
    await dao.update_task(task, status="completed", output={"summary": "Completed original scope"}, finish=True)
    # The original counterexample: result notifications were already consumed.
    await web_env.db.conn.execute(
        "UPDATE web_task_notifications SET state='delivered', delivered_at=1 WHERE task_uuid=?", (task,))
    await web_env.db.conn.commit()
    return task, important, last


async def test_terminal_dense_stream_delivers_control_and_next_run_does_not_replay(web_env, monkeypatch):
    row = await _controller_setup(web_env, monkeypatch)
    root = "root-dense"
    task, important, last = await _completed_task(web_env, row, root)
    dao = web_env.server.agent_dao
    chat_id, conv = int(row["internal_chat_id"]), row["conversation_uuid"]
    for other_chat, other_conv in ((chat_id, "other-conversation"), (999, conv)):
        await dao.create_task(chat_id=other_chat, parent_session_uuid=other_conv,
            workflow_uuid="wf", title="foreign", status="running")
    foreign = await dao.create_task(chat_id=chat_id, parent_session_uuid=conv,
        workflow_uuid="wf", title="other root", status="running", run_root_turn_uuid="other-root")
    await dao.append_event(foreign, "control_response", summary="foreign control")
    await web_env.db.conn.execute('''INSERT INTO web_operations
        (conversation_uuid,internal_chat_id,op_id,op_type,turn_uuid,run_root_turn_uuid,task_uuid,
         display_seq,revision,payload_json,created_at_ms,updated_at_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (conv, chat_id, "agent:foreign", "agent", "other-root", "other-root", foreign, 1, 1, '{}', 1, 1))
    await web_env.db.conn.commit()
    first, = await _run_reviews(web_env.server, row, root=root, run="dense-1", count=1)
    assert first["alreadyTerminal"] and not first["reviewPending"]
    assert "Do not call Agent(action=wait) again" in first["message"]
    assert [agent["taskUuid"] for agent in first["agents"]] == [task]
    observed = first["agents"][0]
    assert observed["taskUuid"] == task
    assert important[0] in [e["seq"] for e in observed["recentEvents"]]
    assert observed["eventPage"] == {"highWater": last, "nextAfter": last, "hasMore": False}
    assert observed["currentExecution"]["phase"] == "completed"
    assert observed["deliveryEvidence"]["known"] is False
    assert first["notifications"] == [] and observed["result"] == {}

    second, = await _run_reviews(web_env.server, row, root=root, run="dense-2", count=1)
    # Unchanged terminal Agents are only counted; their cursor still persists.
    assert second["agents"] == []
    assert second["unchangedAgents"]["count"] == 1
    assert second["unchangedAgents"]["cursors"][task][:2] == [last, last]
    assert second["notifications"] == []


@pytest.mark.parametrize("split_runs", [False, True])
async def test_terminal_control_pages_drain_without_wait_or_terminal_latch(web_env, monkeypatch, split_runs):
    row = await _controller_setup(web_env, monkeypatch)
    root = "root-pages"
    task, important, last = await _completed_task(web_env, row, root, controls=120)
    if split_runs:
        pages = await _run_reviews(web_env.server, row, root=root, run="page-1", count=1)
        # A different execution run must continue from the durable first page.
        pages += await _run_reviews(web_env.server, row, root=root, run="page-2", count=3)
    else:
        pages = await _run_reviews(web_env.server, row, root=root, run="pages", count=4)
    for page in pages[:2]:
        assert page["reviewPending"]
        assert "Call Agent(action=wait) again" in page["message"]
        assert "Do not call Agent(action=wait) again" not in page["message"]
    assert not pages[2]["reviewPending"]
    assert pages[1]["wakeReason"] == pages[2]["wakeReason"] == "event_backlog"
    assert pages[3]["alreadyTerminal"] and pages[3]["skipped"]  # terminal latch only AFTER draining
    seen = [event["seq"] for page in pages[:3] for event in page["agents"][0]["recentEvents"]]
    assert seen == [1, *important, last]
    assert len(seen) == len(set(seen))
    assert pages[2]["agents"][0]["eventPage"]["nextAfter"] == last
    assert all(page["notifications"] == [] for page in pages[:3])
    assert all(page["agents"][0]["hasActivity"] is False for page in pages[1:3])
    ops = await web_env.server._web_operations(row["conversation_uuid"])
    assert not any(op["opType"] == "agent_supervision" for op in ops)


async def test_running_review_backlog_drains_without_arming_event_only(web_env, monkeypatch):
    row = await _controller_setup(web_env, monkeypatch)
    dao, root = web_env.server.agent_dao, "root-running"
    chat_id, conv = int(row["internal_chat_id"]), row["conversation_uuid"]
    task = await dao.create_task(chat_id=chat_id, parent_session_uuid=conv,
        workflow_uuid="wf", title="still running", status="running", run_root_turn_uuid=root)
    for i in range(75):
        await dao.append_event(task, "control_response", detail={"controlUuid": f"c-{i}"})
    await dao.append_event(task, "model_call_started", detail={"attemptId": "current-attempt"})
    batch = await dao.supervision_events(task, controller_visible=True)
    task_row = await dao.get_task(task)
    observation, _ = review_observation(task_row, batch, {}, {}, now_ms=200000)
    snapshot = {"ok": True, "agents": [{**observation, "taskUuid": task, "status": "running",
        "currentStatus": task_row.current_status, "planRuntime": {},
        "recentEvents": [{"seq": e.seq, "kind": e.kind, "detail": e.detail} for e in batch["events"]]}]}
    messages = MessageDAO(web_env.db)
    await messages.ensure_session(chat_id)
    await messages.add(chat_id, "user", "review", conversation_uuid=conv, turn_uuid=root)
    await messages.add(chat_id, "assistant", tool_calls=[ToolCall(
        id="prior-review", name="AgentWait", arguments='{"mode":"event_only"}')],
        conversation_uuid=conv, turn_uuid=root)
    await messages.add(chat_id, "tool", json.dumps(snapshot), name="AgentWait", tool_call_id="prior-review",
        conversation_uuid=conv, turn_uuid=root)
    page, = await _run_reviews(web_env.server, row, root=root, run="running-next", count=1)
    assert page["wakeReason"] == "event_backlog" and not page["reviewPending"]
    assert page["summary"]["running"] == 1
    assert [e["seq"] for e in page["agents"][0]["recentEvents"]] == list(range(51, 77))
    assert page["agents"][0]["currentExecution"]["attemptId"] == "current-attempt"
    assert page["agents"][0]["hasActivity"] is False
    ops = await web_env.server._web_operations(conv)
    assert not any(op["opType"] == "agent_supervision" for op in ops)


async def test_review_receipt_scope_compaction_and_read_without_delivery(env):
    dao, task, _ = env
    messages = MessageDAO(dao.db)
    await messages.ensure_session(123)
    snapshot = {"ok": True, "agents": [{"taskUuid": task, "status": "running", "currentStatus": "working",
        "eventPage": {"nextAfter": 10, "highWater": 100, "hasMore": True},
        "planRuntime": {"activePlanVersion": 1, "steps": [{"stepId": "s1", "status": "running", "criteriaState": {}}]}}]}
    scope = dict(chat_id=123, conversation_uuid="conv", root_turn_uuid="root")
    assert await dao.last_supervision_snapshot(**scope) == {}
    mid = await messages.add(123, "tool", json.dumps(snapshot), name="AgentWait",
        conversation_uuid="conv", run_root_turn_uuid="root")
    for ownership in (
        {"conversation_uuid": "foreign", "run_root_turn_uuid": "root"},
        {"conversation_uuid": "conv", "run_root_turn_uuid": "foreign"},
        {"conversation_uuid": "conv", "run_root_turn_uuid": "root", "task_uuid": "child"},
    ):
        await messages.add(123, "tool", '{"agents":[]}', name="AgentWait", **ownership)
    await messages.add(999, "tool", '{"agents":[]}', name="AgentWait",
        conversation_uuid="conv", run_root_turn_uuid="root")
    for content in ('{"ok":true,"alreadyTerminal":true}', 'invalid json'):
        await messages.add(123, "tool", content, name="AgentWait",
            conversation_uuid="conv", run_root_turn_uuid="root")
    await dao.db.conn.execute("UPDATE messages SET compacted=1 WHERE id=?", (mid,))
    await dao.db.conn.commit()
    restored = restore_review_state(await dao.last_supervision_snapshot(**scope))
    assert restored[task]["lastEventSeq"] == 10 and restored[task]["hasMore"]
    assert restored[task]["observedEventSeq"] == 100
    assert restored[task]["deliveryEvidenceState"]["planVersion"] == 1
    # A read alone (or cancellation before save_tool_result) must not acknowledge it.
    await dao.supervision_events(task, controller_visible=True)
    assert await dao.last_supervision_snapshot(**scope) == snapshot


async def test_visible_page_matches_projection_and_preserves_high_water(env, monkeypatch):
    dao, task, _ = env
    for kind in ("model_call_started", "model_stream_progress", "tool_call_started", "tool_call_finished",
                 "modelXliteral", "control_response", "plan_submitted"):
        await dao.append_event(task, kind, detail={"planVersion": 1})
    before = await dao.events(task, limit=200)
    original = dao.events

    async def read_then_append(*args, **kwargs):
        result = await original(*args, **kwargs)
        await dao.append_event(task, "control_response", summary="after high-water")
        return result

    monkeypatch.setattr(dao, "events", read_then_append)
    batch = await dao.supervision_events(task, controller_visible=True)
    visible = project_agent_payload_for_controller({"events": [{"seq": e.seq, "kind": e.kind} for e in before]})
    assert [e.seq for e in batch["events"]] == [e["seq"] for e in visible["events"]]
    assert batch["nextAfter"] == batch["highWater"] == before[-1].seq
    assert all(e.seq <= batch["highWater"] for e in batch["current"])
    monkeypatch.setattr(dao, "events", original)
    next_page = await dao.supervision_events(task, after_seq=batch["nextAfter"], controller_visible=True)
    assert [e.seq for e in next_page["events"]] == [before[-1].seq + 1]
    # Ordinary events() remains a recent tail on first read, incremental thereafter.
    assert [e.seq for e in await dao.events(task, limit=2)] == [before[-1].seq, before[-1].seq + 1]


async def test_supervision_indexes_upgrade_and_bounded_empty_review(tmp_path):
    path = tmp_path / "supervision.db"
    db = DB(str(path))
    await db.connect()
    indexes = ("idx_rath_task_events_task_kind_seq", "idx_rath_task_events_controller_review",
               "idx_messages_agent_wait_review")
    for name in indexes:
        await db.conn.execute(f"DROP INDEX {name}")
    await db.conn.execute("INSERT INTO rath_task_events (task_uuid,seq,kind) VALUES ('preserved',1,'control_response')")
    await db.conn.commit()
    await db.close()
    # Normal startup installs indexes into an existing DB without rewriting facts.
    await db.connect()
    cur = await db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    assert set(indexes) <= {row[0] for row in await cur.fetchall()}
    assert len(await AgentDAO(db).events("preserved")) == 1
    await db.close()

    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        dao = AgentDAO(SimpleNamespace(conn=conn))
        previous_size, costs = 0, []
        for size in (1000, 10000, 100000):
            await conn.executemany(
                "INSERT INTO rath_task_events (task_uuid,seq,ts,kind,detail_json) VALUES (?,?,?,?,?)",
                (("dense", seq, 100, "model_call_started" if seq == 1 else "model_stream_progress", '{}')
                 for seq in range(previous_size + 1, size + 1)),
            )
            await conn.commit()
            traces, ticks = [], [0]

            def progress():
                ticks[0] += 1
                return 0

            await conn.set_trace_callback(traces.append)
            await conn.set_progress_handler(progress, 100)
            # First and no-new-event reviews must not scan dense hidden telemetry
            # (legacy stored progress rows are no longer a current-state kind).
            for after in (0, size):
                batch = await dao.supervision_events("dense", after_seq=after, controller_visible=True)
                assert batch["events"] == [] and not batch["hasMore"]
                assert batch["nextAfter"] == batch["highWater"] == size
                assert [e.seq for e in batch["current"]] == [1]
            await conn.set_progress_handler(None, 100)
            await conn.set_trace_callback(None)
            costs.append(ticks[0] * 100)
            query = next(q for q in traces if "WITH kinds" in q)
            cur = await conn.execute("EXPLAIN QUERY PLAN " + query)
            plan = "\n".join(str(tuple(row)) for row in await cur.fetchall())
            assert "idx_rath_task_events_task_kind_seq" in plan
            assert "GROUP BY" not in plan
            page_query = next(q for q in traces if "kind NOT GLOB" in q)
            cur = await conn.execute("EXPLAIN QUERY PLAN " + page_query)
            page_plan = "\n".join(str(tuple(row)) for row in await cur.fetchall())
            assert "idx_rath_task_events_controller_review" in page_plan
            print(f"supervision history={size}, two_reviews_vm_ops~={costs[-1]}")
            previous_size = size
        assert max(costs) < 4000
        assert max(costs) <= max(1, min(costs)) * 2
