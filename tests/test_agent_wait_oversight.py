"""AgentWait delivery, slimming and supervision oversight; fake providers and local SQLite only."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from app.agents.supervision import (
    RUNNING_STATUSES, OversightSettings, digest_has_progress, external_effects, progress_digest, review_due_at_ms,
    scan_alerts, task_brief,
)
from app.db.engine import now_ts
from app.llm.events import StreamEvent, ToolCall
from app.tools.base import ToolRegistry, current_tool_context
from app.web_admin import _WebStreamRenderer
from tests.test_agent_continuity import call
from tests.test_agent_continuity import env as continuity_env
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _cfg, web_env  # noqa: F401


def _wait_calls(ids, *, final="reviewed"):
    return [
        *[[StreamEvent(kind="tool_call", tool_calls=[ToolCall(id=call_id, name="AgentWait", arguments=arguments)]),
           StreamEvent(kind="finish", finish_reason="tool_calls")] for call_id, arguments in ids],
        [StreamEvent(kind="content", text=final), StreamEvent(kind="finish", finish_reason="stop")],
    ]


async def _setup(web_env, monkeypatch, before_wait=None):
    server = web_env.server
    server.config = _cfg()
    server.model_selection = SimpleNamespace(current="openai/gpt")
    tools = ToolRegistry()
    calls = []

    async def wait(args):
        calls.append(args)
        if before_wait is not None:
            await before_wait(len(calls))
        return await current_tool_context().agent_wait(args)

    tools.add("AgentWait", "wait", {"type": "object", "properties": {}}, wait, preserve_result=True)
    server.tools = tools

    async def prompt():
        return "sys"

    monkeypatch.setattr(server, "_build_system_prompt_for_chat", prompt)
    monkeypatch.setattr(server, "_ensure_web_task_notification_worker", lambda *_a, **_k: None)
    row = await server._create_web_conversation(123, title="oversight", model="openai/gpt")
    return row, calls


async def _run_turn(server, row, backend, *, root, timeout=10):
    server.llm_factory = FakeRunFactory(backend, context_window=128000)
    live = server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": root})
    failures = []
    emitted = []

    class Renderer(_WebStreamRenderer):
        async def fail(self, error_text):
            failures.append(error_text)
            return await super().fail(error_text)

        async def emit(self, event):
            if event.get("type") != "stats":
                emitted.append(json.dumps(event, ensure_ascii=False, default=str)[:300])
            return await super().emit(event)

    ok = await asyncio.wait_for(server._run_web_turn(
        int(row["internal_chat_id"]), "supervise", Renderer(live),
        conversation=row, root_turn_uuid=root,
    ), timeout=timeout)
    assert ok, "\n".join([f"calls={backend.calls} failures={failures}", *emitted[-15:]])


def _tool_results(backend, ids):
    by_id = {m.get("tool_call_id"): json.loads(m["content"]) for m in backend.seen_convos[-1]
             if m.get("role") == "tool" and m.get("tool_call_id") in ids}
    return [by_id[call_id] for call_id in ids]


def _delivered_ids(result):
    return {value for item in result.get("notifications") or [] for value in item.get("notificationUuids") or []}


async def _long_turn_waits(web_env, monkeypatch):
    """Two AgentWaits in one controller turn, with the claim lease expiring in between."""
    server, dao = web_env.server, web_env.server.agent_dao
    root = "root-long-turn"
    running_ref = {}

    async def before_wait(index):
        if index != 2:
            return
        # The controller turn is now older than the 900 s claim lease and the
        # periodic outbox recovery runs, exactly as in production.
        await web_env.db.conn.execute(
            "UPDATE web_task_notifications SET claimed_at=? WHERE state='processing'", (now_ts() - 901,))
        await web_env.db.conn.commit()
        await server._recover_web_task_notifications()
        await dao.update_task(running_ref["task"], status="completed", current_status="任务完成",
                              output={"summary": "second result"}, finish=True, expected_statuses=("running",))

    row, _calls = await _setup(web_env, monkeypatch, before_wait)
    chat_id, conv = int(row["internal_chat_id"]), row["conversation_uuid"]
    finished = await dao.create_task(chat_id=chat_id, parent_session_uuid=conv, workflow_uuid="wf",
                                     title="finished first", status="running", turn_uuid=root, run_root_turn_uuid=root)
    running_ref["task"] = await dao.create_task(chat_id=chat_id, parent_session_uuid=conv, workflow_uuid="wf",
                                                title="still running", status="running", turn_uuid=root,
                                                run_root_turn_uuid=root)
    await dao.update_task(finished, status="completed", current_status="任务完成",
                          output={"summary": "first result"}, finish=True, expected_statuses=("running",))
    ids = [("wait-1", '{"mode":"review_after","reviewAfterSeconds":0.2}'), ("wait-2", '{"mode":"event_only"}')]
    backend = FakeStreamBackend(_wait_calls(ids))
    await _run_turn(server, row, backend, root=root)
    first, second = _tool_results(backend, [call_id for call_id, _ in ids])
    assert {item["taskUuid"] for item in first["notifications"]} == {finished}
    assert _delivered_ids(first)
    return finished, first, second


async def test_turn_longer_than_notification_lease_does_not_redeliver_claimed_result(web_env, monkeypatch):
    """The 15-minute duplicate: an expired lease of a live turn went back to pending."""
    finished, first, second = await _long_turn_waits(web_env, monkeypatch)
    assert not (_delivered_ids(first) & _delivered_ids(second)), "an already delivered result was pushed again"
    assert finished not in {item["taskUuid"] for item in second["notifications"]}
    cur = await web_env.db.conn.execute(
        "SELECT state FROM web_task_notifications WHERE task_uuid=?", (finished,))
    assert {str(item["state"]) for item in await cur.fetchall()} == {"delivered"}


async def test_without_held_claims_the_long_turn_redelivers(web_env, monkeypatch):
    """Control for the test above: the pre-fix behaviour (no live-claim registry) duplicates."""
    monkeypatch.setattr(web_env.server, "_hold_web_task_notification_claims", lambda held: object())
    finished, first, second = await _long_turn_waits(web_env, monkeypatch)
    assert _delivered_ids(first) & _delivered_ids(second)
    assert finished in {item["taskUuid"] for item in second["notifications"]}


env = continuity_env


async def _tool(dao, task_uuid, name, arguments, preview="status: ok"):
    await dao.append_event(task_uuid, "tool_call_started", detail={"name": name, "arguments": json.dumps(arguments)})
    await dao.append_event(task_uuid, "tool_call_finished", detail={"name": name, "resultPreview": preview, "durationMs": 5})


async def test_agent_info_get_returns_only_current_round_in_full_and_lists_tool_calls(env):
    dao, reg, _backend, ctx, _ = env
    first = await call(reg, "Agent", {"prompt": "round one", "tools": []}, ctx)
    sid, t1 = first["agentSession"]["agentId"], first["task"]["taskUuid"]
    second = await call(reg, "AgentContinue", {"to": sid, "prompt": "round two", "tools": []}, ctx)
    t2 = second["task"]["taskUuid"]
    info = await call(reg, "AgentInfo", {"action": "get", "to": sid}, ctx)
    rounds = {item["taskUuid"]: item for item in info["tasks"]}
    assert info["task"]["taskUuid"] == t2 and "planMode" in info["task"]
    assert rounds[t1]["brief"] and {"title", "status", "conclusion"} <= set(rounds[t1]) and "planMode" not in rounds[t1]
    assert rounds[t2]["current"] and "planMode" not in rounds[t2]
    for args in ({"includeHistory": True}, {"taskUuid": t1}):
        full = await call(reg, "AgentInfo", {"action": "get", "to": sid, **args}, ctx)
        assert "planMode" in {item["taskUuid"]: item for item in full["tasks"]}[t1]
    missing = await call(reg, "AgentInfo", {"action": "get", "to": sid, "taskUuid": "not-a-round"}, ctx)
    assert missing["error"] == "agent_task_not_in_instance"

    await _tool(dao, t2, "Read", {"path": "a.py"})
    await _tool(dao, t2, "Bash", {"command": "pytest -q"}, "status: failed\nexit_code: 2\nboom")
    calls = await call(reg, "AgentInfo", {"action": "tool_calls", "to": sid, "limit": 1}, ctx)
    assert calls["ok"] and calls["taskUuid"] == t2
    assert [(item["tool"], item["status"], item["error"]) for item in calls["toolCalls"]] == [("Bash", "failed", "exit_code 2")]
    assert "pytest -q" in calls["toolCalls"][0]["arguments"] and "boom" in calls["toolCalls"][0]["result"]


def test_external_effect_rules_are_conservative():
    hits = {command: [key for key, _label in external_effects(command)] for command in (
        "git push origin main", "git -C repo push", "systemctl restart openbear.service", "docker compose up -d",
        "ssh root@host uptime", "rm -rf build", "curl -X POST https://api.github.com/repos", "npm publish",
        "git status", "systemctl status openbear", "docker ps", "curl -X POST http://127.0.0.1:8080/api",
        "curl https://example.com", "rm -f a.txt", "grep ssh notes.md",
    )}
    assert all(hits[command] for command in list(hits)[:8]), hits
    assert not any(hits[command] for command in list(hits)[8:]), hits


def test_review_deadline_first_then_interval_and_long_single_step():
    settings = OversightSettings()
    task = SimpleNamespace(status="running", started_at=1000)
    assert review_due_at_ms(task, {}, settings, in_long_step=False) == 1_300_000
    assert review_due_at_ms(task, {"reviewedAtMs": 2_000_000}, settings, in_long_step=False) == 2_900_000
    assert review_due_at_ms(task, {"reviewedAtMs": 2_000_000}, settings, in_long_step=True) is None
    assert review_due_at_ms(SimpleNamespace(status="completed", started_at=1000), {}, settings, in_long_step=False) is None


async def test_progress_digest_and_alerts_are_derived_from_task_events_once_per_situation(env):
    dao, _reg, _backend, ctx, _ = env
    workflow = await dao.workflow_by_slug("single-agent")
    tid = await dao.create_task(chat_id=ctx.chat_id, workflow_uuid=workflow.workflow_uuid,
                                parent_session_uuid=ctx.session_uuid, title="worker", status="running",
                                input_data={"originalPrompt": "Fix the parser. Done when tests pass.",
                                            "ownedPaths": ["app/parser"]})
    await _tool(dao, tid, "Bash", {"command": "git push origin main"})
    for _ in range(3):
        await _tool(dao, tid, "Bash", {"command": "pytest tests/test_parser.py"}, "status: failed\nexit_code: 1")
    await _tool(dao, tid, "Edit", {"path": "app/parser/core.py", "old_string": "a", "new_string": "b"})
    await _tool(dao, tid, "Write", {"path": "/etc/hosts", "content": "x"})
    await dao.append_event(tid, "model_call_finished", detail={"inputTokens": 100, "outputTokens": 50, "costUsd": 0.02})
    await dao.append_event(tid, "model_context_compaction_completed", detail={"source": "auto"})
    task = await dao.get_task(tid)
    digest = await progress_digest(dao.db, task, after_seq=0, through_seq=10_000)
    assert digest["toolCalls"] == {"Bash": 4, "Edit": 1, "Write": 1}
    assert digest["filesChanged"] == ["app/parser/core.py", "/etc/hosts"]
    assert digest["failureCount"] == 3 and digest["failures"][0]["error"] == "exit_code 1"
    assert any("git push" in item for item in digest["externalEffects"])
    assert digest["contextCompactions"] == 1 and digest["usageDelta"] == {"tokens": 150, "costUsd": 0.02}
    assert len(json.dumps(digest, ensure_ascii=False).encode()) < 2048
    brief = await task_brief(dao.db, task)
    assert brief["objective"].startswith("Fix the parser") and brief["ownedPaths"] == ["app/parser"]

    settings = OversightSettings()
    alerts = await scan_alerts(dao.db, task, {}, settings, workspace="/srv/work")
    kinds = {item["kind"] for item in alerts}
    assert kinds == {"repeated_failure", "out_of_scope", "external_effect", "context_compaction"}, alerts
    assert next(item for item in alerts if item["kind"] == "out_of_scope")["path"] == "/etc/hosts"
    assert "git push origin main" in next(item for item in alerts if item["kind"] == "external_effect")["command"]
    state = {"raisedAlerts": [item["key"] for item in alerts]}
    assert await scan_alerts(dao.db, task, state, settings, workspace="/srv/work") == []
    # The same failure run growing is not a new situation; the repetition now is.
    await _tool(dao, tid, "Bash", {"command": "pytest tests/test_parser.py"}, "status: failed\nexit_code: 1")
    again = await scan_alerts(dao.db, task, state, settings, workspace="/srv/work")
    assert [(item["kind"], item["count"]) for item in again] == [("loop", 4)]
    state["raisedAlerts"] += [item["key"] for item in again]
    await _tool(dao, tid, "Bash", {"command": "pytest tests/test_parser.py"})
    for _ in range(3):  # a new failure run after a success
        await _tool(dao, tid, "Bash", {"command": "ruff check ."}, "status: failed\nexit_code: 1")
    fresh = await scan_alerts(dao.db, task, state, settings, workspace="/srv/work")
    assert [(item["kind"], item["count"]) for item in fresh] == [("repeated_failure", 3)]


async def test_a_call_running_across_a_review_reports_how_it_ended_in_the_next_digest(env):
    dao, _reg, _backend, ctx, _ = env
    workflow = await dao.workflow_by_slug("single-agent")
    tid = await dao.create_task(chat_id=ctx.chat_id, workflow_uuid=workflow.workflow_uuid,
                                parent_session_uuid=ctx.session_uuid, title="worker", status="running")
    await _tool(dao, tid, "Read", {"path": "a.py"})
    await dao.append_event(tid, "tool_call_started", detail={"name": "Bash", "arguments": json.dumps({"command": "pytest -q"})})
    reviewed = await dao.append_event(tid, "model_call_finished", detail={})
    task = await dao.get_task(tid)
    first = await progress_digest(dao.db, task, after_seq=0, through_seq=reviewed)
    assert first["toolCalls"] == {"Read": 1, "Bash": 1} and "failureCount" not in first
    await dao.append_event(tid, "tool_call_finished", detail={"name": "Bash", "resultPreview": "status: failed\nexit_code: 1", "durationMs": 900})
    await dao.append_event(tid, "tool_call_started", detail={"name": "Bash", "arguments": json.dumps({"command": "make lint"})})
    second_review = await dao.append_event(tid, "model_call_finished", detail={})
    await dao.append_event(tid, "tool_call_finished", detail={"name": "Bash", "resultPreview": "status: failed\nexit_code: 2", "durationMs": 5})
    second = await progress_digest(dao.db, task, after_seq=reviewed, through_seq=second_review)
    assert second["toolCalls"] == {"Bash": 1} and second["commands"] == ["make lint"]
    assert second["finishedRunning"] == [{"tool": "Bash", "durationMs": 900}]
    assert second["failureCount"] == 1 and second["failures"][0] == {"tool": "Bash", "error": "exit_code 1", "target": "pytest -q"}
    third = await progress_digest(dao.db, task, after_seq=second_review, through_seq=second_review + 10)
    assert third["toolCalls"] == {} and third["failureCount"] == 1 and third["failures"][0]["target"] == "make lint"
    assert digest_has_progress(third) and not digest_has_progress(await progress_digest(dao.db, task, after_seq=second_review + 10, through_seq=second_review + 20))


async def test_agent_wait_caps_review_wakes_on_alert_and_requires_assessments(web_env, monkeypatch):
    import app.web_console.chat_runtime as chat_runtime

    server, dao = web_env.server, web_env.server.agent_dao
    root = "root-oversight"
    task_ref = {}
    base = time.time()

    def due(task, state, settings, *, in_long_step):
        if task.status not in RUNNING_STATUSES:
            return None
        reviewed = int(state.get("reviewedAtMs") or 0)
        return reviewed + 150 if reviewed else int((base + 0.2) * 1000)

    monkeypatch.setattr(chat_runtime, "review_due_at_ms", due)
    monkeypatch.setattr(chat_runtime, "POLL_SECONDS", 0.05)

    async def before_wait(index):
        if index == 4:
            await dao.update_task(task_ref["task"], status="completed", current_status="任务完成",
                                  output={"summary": "done"}, finish=True, expected_statuses=("running",))

    row, _calls = await _setup(web_env, monkeypatch, before_wait)
    chat_id, conv = int(row["internal_chat_id"]), row["conversation_uuid"]
    task_ref["task"] = tid = await dao.create_task(chat_id=chat_id, parent_session_uuid=conv, workflow_uuid="wf",
                                                   title="pusher", status="running", turn_uuid=root,
                                                   run_root_turn_uuid=root)
    await _tool(dao, tid, "Bash", {"command": "git push origin main"})
    assessment = json.dumps({"mode": "event_only", "assessments": [
        {"agent": tid, "verdict": "continue", "basis": "push was part of the brief"}]})
    ids = [("w1", '{"mode":"review_after","reviewAfterSeconds":3600}'), ("w2", '{"mode":"event_only"}'),
           ("w3", assessment), ("w4", '{"mode":"event_only"}')]
    backend = FakeStreamBackend(_wait_calls(ids))
    await _run_turn(server, row, backend, root=root)
    first, missing, capped, _last = _tool_results(backend, [call_id for call_id, _ in ids])

    assert first["wakeReason"] == "supervision_alert"
    assert first["alerts"][0]["kind"] == "external_effect" and "git push" in first["alerts"][0]["command"]
    assert first["reviewSchedule"]["capped"] and first["reviewSchedule"]["requestedSeconds"] == 3600
    assert [item["taskUuid"] for item in first["assessmentsRequired"]["agents"]] == [tid]
    agent = next(item for item in first["agents"] if item["taskUuid"] == tid)
    assert agent["progress"]["toolCalls"] == {"Bash": 1} and "objective" in agent["progress"]["brief"]

    assert missing["ok"] is False and missing["error"] == "assessments_required"
    assert [item["taskUuid"] for item in missing["missing"]] == [tid]

    assert capped["wakeReason"] == "review_cap" and capped["reviewSchedule"]["capped"]
    assert capped["reviewSchedule"]["requestedSeconds"] is None
    assert "alerts" not in capped and "assessmentsRequired" not in capped
    assert capped["assessmentsRecorded"] == 1

    cur = await web_env.db.conn.execute(
        "SELECT payload_json FROM web_operations WHERE conversation_uuid=? AND op_type='agent_supervision'", (conv,))
    payloads = [json.loads(item["payload_json"]) for item in await cur.fetchall()]
    assert any(item.get("assessments", [{}])[0].get("verdict") == "continue" for item in payloads if item.get("assessments"))
    card = next(item for item in payloads if item.get("agents"))
    assert card["agents"][0]["taskUuid"] == tid and card["agents"][0]["alerts"]
