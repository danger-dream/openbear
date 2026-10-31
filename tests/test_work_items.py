from __future__ import annotations

import json
import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.agent.loop import Agent
from app.db.engine import DB
from app.llm.events import StreamEvent
from app.tools.base import ToolRegistry, ToolRuntimeContext, _TOOL_CONTEXT
from app.tools.work_items import register_work_item_tool, snapshot_diffs
from app.web_console.core import _WEB_SESSION_KEY, WebSession
from app.web_console.work_items_api import WebAdminWorkItemsMixin
from app.work_items import (
    BRIEFING_KIND,
    OUTLINE_KIND,
    Actor,
    Source,
    WorkItemConflict,
    WorkItemError,
    WorkItemsDAO,
    WorkItemsRuntime,
    bind_work_items_finish,
    reset_work_items_runtime_epoch,
    runtime_metadata,
)
from tests.test_agent_loop import FakeBackend, RecordRenderer, _echo_registry

CONV = "conv-1"
TURN = "turn-1"


@pytest.fixture
async def db(tmp_path):
    database = DB(str(tmp_path / "work-items.db"))
    await database.connect()
    yield database
    await database.close()


async def user_message(db, op_id, turn, text, *, source="user", seq=1):
    await db.conn.execute(
        """INSERT INTO web_operations (conversation_uuid, op_id, op_type, turn_uuid, run_root_turn_uuid, display_seq, source,
           revision, payload_json, created_at_ms, updated_at_ms) VALUES (?,?,'user_message',?,?,?,?,1,?,?,?)""",
        (CONV, op_id, turn, turn, seq, source, json.dumps({"text": text}), 1000 * seq, 1000 * seq),
    )
    await db.conn.commit()


async def questionnaire(db, interaction_id, turn):
    payload = {"action": "questionnaire", "interactionId": interaction_id, "title": "定几件事", "questions": [
        {"id": "q1", "question": "1. 什么时候建任务？", "options": [{"label": "一提出就建", "value": "a"}, {"label": "动手时才建", "value": "b"}]},
        {"id": "q2", "question": "2. 在哪里看？", "options": [{"label": "只在会话里", "value": "c"}]},
    ]}
    result = {"action": "questionnaire", "answers": [
        {"questionId": "q1", "question": "1. 什么时候建任务？", "selectedLabels": ["一提出就建"], "selectedValues": ["a"], "text": "调查也记进去"},
        {"questionId": "q2", "question": "2. 在哪里看？", "selectedLabels": ["只在会话里"], "selectedValues": ["c"], "text": ""},
    ]}
    await db.conn.execute(
        """INSERT INTO user_interactions (interaction_id, owner_chat_id, conversation_uuid, turn_uuid, tool_call_id, payload_json,
           status, expires_at_ms, created_at_ms, resolved_at_ms, result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (interaction_id, 1, CONV, turn, "call-q", json.dumps(payload), "answered", 10**13, 1, 2, json.dumps(result)),
    )
    await db.conn.commit()


def controller(turn=TURN, call="call-1"):
    return Actor("controller", turn_uuid=turn, tool_call_id=call)


async def seeded(db):
    dao = WorkItemsDAO(db)
    await user_message(db, "msg:a", TURN, "安全问题都修一下，Mac 那几个别管")
    source = await dao.resolve_source(CONV, controller(), {})
    await dao.create(CONV, controller(), {"title": "修复安全问题", "children": [
        {"title": "网关", "children": ["保留配置", "锁内读取"]}, "防火墙", "Mac 提权"]}, source=source)
    return dao


async def test_numbers_are_stable_and_nesting_is_bounded(db):
    dao = await seeded(db)
    assert [row["number"] for row in await dao.rows(CONV)] == ["1", "1.1", "1.2", "1.3", "1.1.1", "1.1.2"]
    await dao.delete(CONV, controller(), "1.2", reason="建错了")
    created = await dao.add(CONV, controller(), "1", ["新的一项"])
    assert created == ["1.4"]
    with pytest.raises(WorkItemError) as exc:
        await dao.add(CONV, controller(), "1.1.1", ["第四层"])
    assert exc.value.code == "too_deep"
    raise_event = (await dao.events(CONV, (await dao.get_row(CONV, "1"))["itemUuid"]))[0]
    assert raise_event["kind"] == "raise" and raise_event["actor"] == "user"
    assert raise_event["data"]["quote"] == "安全问题都修一下，Mac 那几个别管"
    assert raise_event["source"]["opId"] == "msg:a"


async def test_completion_needs_evidence_and_closed_children(db):
    dao = await seeded(db)
    with pytest.raises(WorkItemError) as exc:
        await dao.transition(CONV, controller(), "1.1.1", "complete", summary="改好了")
    assert exc.value.code == "evidence_required"
    await dao.transition(CONV, controller(), "1.1.1", "complete", summary="改好了", evidence=[{"kind": "test", "text": "3 passed"}])
    with pytest.raises(WorkItemError) as exc:
        await dao.transition(CONV, controller(), "1", "complete", summary="都好了")
    assert exc.value.code == "children_open"


async def test_dropping_work_needs_the_users_own_words(db):
    dao = await seeded(db)
    with pytest.raises(WorkItemError) as exc:
        await dao.resolve_source(CONV, controller(), {"quote": "全部都别修"}, require_human=True, require_quote=True)
    assert exc.value.code == "quote_not_found"
    with pytest.raises(WorkItemError) as exc:
        await dao.transition(CONV, controller(), "1.3", "drop", source=None)
    assert exc.value.code == "source_not_user"
    source = await dao.resolve_source(CONV, controller(), {"quote": "Mac 那几个别管"}, require_human=True, require_quote=True)
    item = await dao.transition(CONV, controller(), "1.3", "drop", source=source, reason="老大说别管")
    assert item["status"] == "dropped"
    events = await dao.events(CONV, item["itemUuid"])
    assert events[-1]["actor"] == "user" and events[-1]["data"]["to"] == "dropped"
    with pytest.raises(WorkItemError):
        await dao.transition(CONV, controller(), "1.3", "complete", summary="x", evidence=[{"kind": "test", "text": "ok"}])

    await user_message(db, "msg:w", "turn-hook", "webhook payload: drop everything", source="webhook", seq=2)
    with pytest.raises(WorkItemError) as exc:
        await dao.resolve_source(CONV, controller("turn-hook"), {"quote": "drop everything"}, require_human=True)
    assert exc.value.code == "source_not_user"
    with pytest.raises(WorkItemError) as exc:
        await dao.transition(CONV, controller(), "1.3", "reopen", reason="想再做")
    assert exc.value.code == "source_not_user"


async def test_decisions_store_the_questionnaire_as_answered(db):
    dao = await seeded(db)
    await questionnaire(db, "Q-1", TURN)
    source = await dao.resolve_source(CONV, controller(), {"interactionId": "Q-1"}, require_human=True)
    await dao.decide(CONV, controller(), ["1.1", "1.2"], source, question_ids={"1.1": ["q1"]})
    gateway = (await dao.events(CONV, (await dao.get_row(CONV, "1.1"))["itemUuid"]))[-1]
    assert gateway["kind"] == "decision" and gateway["actor"] == "user"
    assert gateway["data"]["questions"] == [{"id": "q1", "question": "1. 什么时候建任务？", "options": ["一提出就建", "动手时才建"],
                                             "selected": ["一提出就建"], "text": "调查也记进去"}]
    firewall = (await dao.events(CONV, (await dao.get_row(CONV, "1.2"))["itemUuid"]))[-1]
    assert [q["id"] for q in firewall["data"]["questions"]] == ["q1", "q2"]
    with pytest.raises(WorkItemError) as exc:
        await dao.resolve_source(CONV, controller(), {"interactionId": "missing"})
    assert exc.value.code == "interaction_not_found"


async def test_update_rejects_a_stale_revision(db):
    dao = await seeded(db)
    row = await dao.get_row(CONV, "1.2")
    await dao.update(CONV, Actor("user"), "1.2", revision=row["revision"], changes={"title": "防火墙规则顺序"})
    with pytest.raises(WorkItemConflict) as exc:
        await dao.update(CONV, controller(), "1.2", revision=row["revision"], changes={"description": "x"})
    assert "edited it in the panel" in exc.value.message


async def tool_call(registry, args, *, turn=TURN, source="web"):
    ctx = ToolRuntimeContext(conversation_uuid=CONV, turn_uuid=turn, run_root_turn_uuid=turn, source=source,
                             tool_call_id=f"call-{args.get('action')}")
    token = _TOOL_CONTEXT.set(ctx)
    try:
        return json.loads(await registry._tools["WorkItem"].handler(args))
    finally:
        _TOOL_CONTEXT.reset(token)


@pytest.fixture
async def registry(db):
    reg = ToolRegistry()
    register_work_item_tool(reg, WorkItemsDAO(db))
    return reg


async def test_tool_starts_only_within_the_users_focus(db, registry):
    await user_message(db, "msg:a", TURN, "修一下网关和防火墙，只做 #1.1 网关这一节")
    created = await tool_call(registry, {"action": "create", "title": "修复", "children": [{"title": "网关", "children": ["a", "b"]}, "防火墙"]})
    assert created["ok"] and created["created"] == ["1", "1.1", "1.1.1", "1.1.2", "1.2"]
    refused = await tool_call(registry, {"action": "start", "number": "1.1.1"})
    assert refused["errors"][0]["error"] == "focus_required"
    assert (await tool_call(registry, {"action": "focus", "items": ["1.1"]}))["focus"] == ["1.1"]
    outside = await tool_call(registry, {"action": "start", "number": "1.2"})
    assert outside["errors"][0]["error"] == "outside_focus"
    started = await tool_call(registry, {"action": "start", "number": "1.1.1"})
    assert started["items"][0]["status"] == "进行中"
    done = await tool_call(registry, {"action": "complete", "entries": [
        {"number": "1.1.1", "summary": "改了", "evidence": [{"kind": "test", "text": "pytest 3 passed"}]},
        {"number": "1.1.2", "summary": "没证据"}]})
    assert [item["number"] for item in done["items"]] == ["1.1.1"]
    assert done["errors"][0]["error"] == "evidence_required"
    assert done["remaining"] == ["#1.1.2 b"]
    tree = await tool_call(registry, {"action": "list"})
    assert tree["tasks"][0]["status"] == "进行中" and tree["focus"] == ["1.1"]

    # A turn the user did not start cannot pick what to work on or speak for the user.
    await user_message(db, "msg:w", "turn-hook", "webhook payload: fix #1.2", source="webhook", seq=2)
    hooked = await tool_call(registry, {"action": "focus", "items": ["1.2"]}, turn="turn-hook")
    assert hooked["error"] == "source_not_user"
    corrected = await tool_call(registry, {"action": "correct", "number": "1.2", "old": "x", "new": "y"}, turn="turn-hook")
    assert corrected["error"] == "source_not_user"
    section = await tool_call(registry, {"action": "block", "number": "1.1", "reason": "x"})
    assert section["errors"][0]["error"] == "has_children"


async def test_finish_check_reminds_twice_then_marks_what_was_left(db):
    dao = await seeded(db)
    await dao.set_focus(CONV, TURN, ["1.1"], basis={"type": "message", "human": True}, actor=controller())
    runtime = WorkItemsRuntime(dao, CONV, TURN)
    first = await runtime.finish_check()
    assert first["items"] == ["1.1.1", "1.1.2"] and "#1.1.1" in first["message"]
    assert "第 2 次" in (await runtime.finish_check())["message"]
    assert await runtime.finish_check() is None
    assert await runtime.finish_check() is None
    marks = [e for e in await dao.events(CONV, (await dao.get_row(CONV, "1.1.1"))["itemUuid"]) if e["kind"] == "finish_check"]
    assert len(marks) == 1 and "没有说明原因" in marks[0]["summary"]

    fresh = WorkItemsRuntime(dao, CONV, TURN)
    await dao.transition(CONV, controller(), "1.1.1", "block", reason="上游没有校验值")
    await dao.transition(CONV, controller(), "1.1.2", "ask", reason="要不要固定版本？")
    assert await fresh.finish_check() is None
    assert await WorkItemsRuntime(dao, CONV, "unrelated-turn").finish_check() is None


async def test_completion_stays_inside_the_focus(db, registry):
    await user_message(db, "msg:a", TURN, "只做 #1.1 网关")
    await tool_call(registry, {"action": "create", "title": "修复", "children": [{"title": "网关", "children": ["a"]}, "防火墙"]})
    evidence = [{"kind": "test", "text": "ok"}]
    unfocused = await tool_call(registry, {"action": "complete", "number": "1.2", "summary": "x", "evidence": evidence})
    assert unfocused["errors"][0]["error"] == "focus_required"
    await tool_call(registry, {"action": "focus", "items": ["1.1"]})
    outside = await tool_call(registry, {"action": "complete", "number": "1.2", "summary": "x", "evidence": evidence})
    assert outside["errors"][0]["error"] == "outside_focus"
    done = await tool_call(registry, {"action": "complete", "number": "1.1.1", "summary": "改了", "evidence": evidence})
    assert done["items"][0]["status"] == "已完成"
    # The task above the focus may be closed, but only once everything under it is.
    above = await tool_call(registry, {"action": "complete", "number": "1", "summary": "都好了"})
    assert above["errors"][0]["error"] == "children_open"
    assert await WorkItemsDAO(db).unfinished(CONV, "turn-without-focus") == []


async def test_finish_policy_keeps_the_turn_going_in_the_real_loop(db):
    dao = await seeded(db)
    await dao.set_focus(CONV, TURN, ["1.2"], basis={"type": "message", "human": True}, actor=controller())
    finish = lambda text: [StreamEvent(kind="content", text=text), StreamEvent(kind="finish", finish_reason="stop")]
    backend = FakeBackend([finish("全部做完了"), finish("真的做完了"), finish("好吧")])
    ctx = ToolRuntimeContext(conversation_uuid=CONV, turn_uuid=TURN, run_root_turn_uuid=TURN)
    bind_work_items_finish(ctx, WorkItemsRuntime(dao, CONV, TURN))
    agent = Agent(backend, _echo_registry())
    result = await agent.run([{"role": "user", "content": "只做防火墙"}], RecordRenderer(), model="m", tool_context=ctx)
    assert result.model_calls == 3
    assert result.text == "好吧"


async def test_outline_is_announced_on_turn_start_panel_edits_and_rotation(db):
    dao = await seeded(db)
    await dao.set_focus(CONV, TURN, ["1.1"], basis={"type": "message", "human": True, "createdAtMs": 1000}, actor=controller())
    runtime = WorkItemsRuntime(dao, CONV, TURN)
    messages = await runtime.reconcile([], epoch=1)
    kinds = [runtime_metadata(m)["kind"] for m in messages]
    assert kinds == [OUTLINE_KIND, BRIEFING_KIND]
    assert "本轮执行范围：#1.1" in messages[0]["content"] and "原话：安全问题都修一下" in messages[1]["content"]
    # The model reads the user as 用户, at Beijing time, never the panel's 你.
    assert "依据：用户 01-01 08:00 的消息" in messages[0]["content"]
    assert re.search(r"- \[提出\] #1 \d\d-\d\d \d\d:\d\d 用户（", messages[1]["content"])
    assert "老大" not in messages[1]["content"] and " 你（" not in messages[1]["content"]
    await dao.transition(CONV, controller(), "1.2", "block", reason="model change")
    assert len(await runtime.reconcile(messages, epoch=1)) == len(messages)
    row = await dao.get_row(CONV, "1.3")
    await dao.transition(CONV, Actor("user"), "1.3", "drop", summary="标为不做", source=Source("panel", True, "user"), revision=row["revision"])
    after_edit = await runtime.reconcile(messages, epoch=1)
    assert len(after_edit) == len(messages) + 1 and "最近改动" in after_edit[-1]["content"]
    next_run = await WorkItemsRuntime(dao, CONV, TURN).reconcile(after_edit, epoch=1)
    assert len(next_run) == len(after_edit)
    rotated = list(after_edit)
    epoch = reset_work_items_runtime_epoch(rotated, current_epoch=1)
    assert epoch == 2 and rotated == []
    again = await runtime.reconcile(rotated, epoch=epoch)
    assert [runtime_metadata(m)["kind"] for m in again] == [OUTLINE_KIND, BRIEFING_KIND]


async def test_diff_snapshot_failure_keeps_the_evidence(tmp_path):
    evidence = [{"kind": "file", "path": str(tmp_path / "missing.py")}, {"kind": "test", "text": "ok"}]
    assert await snapshot_diffs(list(evidence)) == evidence
    with_commit = [{"kind": "commit", "commit": "abc1234"}, {"kind": "file", "path": "x"}]
    assert await snapshot_diffs(with_commit) == with_commit


async def test_duplicate_and_delete_conversation(db):
    dao = await seeded(db)
    await dao.transition(CONV, controller(), "1.2", "complete", summary="改好了",
                         evidence=[{"kind": "report", "path": "workspace/artifacts/old-id/fw.md"}])
    await dao.duplicate_conversation(CONV, "conv-2", rewrite_text=lambda v: str(v).replace("old-id", "new-id"))
    copied = await WorkItemsDAO(db).get_row("conv-2", "1.2")
    assert copied["evidence"][0]["path"] == "workspace/artifacts/new-id/fw.md"
    assert len(await dao.events("conv-2", copied["itemUuid"])) == len(await dao.events(CONV, (await dao.get_row(CONV, "1.2"))["itemUuid"]))
    await dao.hard_delete_conversation(CONV)
    assert await dao.rows(CONV, include_deleted=True) == [] and len(await dao.rows("conv-2")) == 6


class _Runs:
    def __init__(self) -> None:
        self.running = False

    def is_running(self, chat_id):
        return self.running


class _Live:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event, persist=True):
        self.events.append(event)


class _ApiHarness(WebAdminWorkItemsMixin):
    def __init__(self, db, workspace) -> None:
        self.db = db
        self.runs = _Runs()
        self.live = _Live()
        self.workspace_dir = str(workspace)

    async def _conversation_from_request(self, request):
        if request.match_info.get("conversation_uuid") != CONV:
            raise web.HTTPNotFound()
        return {"conversation_uuid": CONV, "internal_chat_id": 7, "owner_chat_id": 1}

    async def _json_body(self, request):
        try:
            return await request.json()
        except Exception:
            return {}

    def _live_for(self, row):
        return self.live

    async def _web_active_round_info(self, conv, chat_id):
        return {"rootTurnUuid": TURN}

    def _web_artifact_denied_roots(self):
        return []

    @staticmethod
    def _web_artifact_is_within(path, roots):
        return any(path == root or root in path.parents for root in roots)

    @staticmethod
    def _tool_file_probe(path, *, digest):
        return True, ""

    @staticmethod
    def _tool_file_mime(name, looks_text):
        return "text/plain"

    @staticmethod
    def _web_artifact_inline_allowed(name, mime):
        return True

    @staticmethod
    def _web_artifact_content_disposition(name, *, inline):
        return "inline"


@pytest.fixture
async def api(db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "artifacts").mkdir(parents=True)
    harness = _ApiHarness(db, workspace)

    @web.middleware
    async def session(request, handler):
        request[_WEB_SESSION_KEY] = WebSession(chat_id=1, expires_at=2**31)
        return await handler(request)

    app = web.Application(middlewares=[session])
    base = "/api/conversations/{conversation_uuid}/work-items"
    app.add_routes([
        web.get(base, harness.handle_api_work_items),
        web.get(base + "/{number}", harness.handle_api_work_item_detail),
        web.patch(base + "/{number}", harness.handle_api_work_item_update),
        web.delete(base + "/{number}", harness.handle_api_work_item_delete),
        web.post(base + "/{number}/children", harness.handle_api_work_item_add),
        web.post(base + "/{number}/drop", harness.handle_api_work_item_drop),
        web.post(base + "/{number}/reopen", harness.handle_api_work_item_reopen),
        web.get(base + "/{number}/file", harness.handle_api_work_item_file),
        web.get(base + "/{number}/file/content", harness.handle_api_work_item_file_content),
    ])
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, harness, tmp_path
    await client.close()


async def test_panel_edits_are_the_users_and_reach_the_outline(db, api):
    client, harness, _ = api
    dao = await seeded(db)
    listing = await (await client.get(f"/api/conversations/{CONV}/work-items")).json()
    assert listing["tasks"][0]["progress"]["total"] == 4 and "<work-items>" in listing["outline"]["text"]
    added = await (await client.post(f"/api/conversations/{CONV}/work-items/1/children", json={"title": "补一项"})).json()
    assert added["created"] == ["1.4"]
    row = await dao.get_row(CONV, "1.2")
    response = await client.patch(f"/api/conversations/{CONV}/work-items/1.2", json={"revision": row["revision"], "criteria": ["规则在封禁之后"]})
    assert response.status == 200
    stale = await client.patch(f"/api/conversations/{CONV}/work-items/1.2", json={"revision": row["revision"], "title": "x"})
    assert stale.status == 409
    dropped = await (await client.post(f"/api/conversations/{CONV}/work-items/1.3/drop", json={"reason": "极端情况"})).json()
    assert dropped["item"]["status"] == "dropped"
    reopened = await (await client.post(f"/api/conversations/{CONV}/work-items/1.3/reopen", json={})).json()
    assert reopened["item"]["status"] == "todo"
    assert (await client.delete(f"/api/conversations/{CONV}/work-items/1.4")).status == 200
    recent = await dao.recent_user_edits(CONV)
    assert [event["actor"] for event in recent] == ["user"] * 5
    assert all(event["source"].get("type") == "panel" for event in recent)
    assert {event["action"] for event in harness.live.events} == {"add", "update", "drop", "reopen", "delete"}
    detail = await (await client.get(f"/api/conversations/{CONV}/work-items/1.3")).json()
    assert [e["label"] for e in detail["item"]["events"]][-2:] == ["状态", "状态"]


async def test_files_open_only_from_the_record_and_never_secrets(db, api):
    client, _, root = api
    dao = await seeded(db)
    (root / "app.py").write_text("print('ok')\n")
    (root / "openbear.json").write_text("{\"secret\": 1}")
    (root / "workspace" / "artifacts" / "report.md").write_text("# 报告")
    other = root.parent / f"{root.name}-other-project"
    other.mkdir()
    (other / "main.go").write_text("package main\n")
    await dao.transition(CONV, controller(), "1.2", "complete", summary="ok", evidence=[
        {"kind": "file", "path": str(root / "app.py")}, {"kind": "file", "path": str(root / "openbear.json")},
        {"kind": "report", "path": "workspace/artifacts/report.md"}, {"kind": "file", "path": str(other / "main.go")},
        {"kind": "file", "path": "/etc/nginx/nginx.conf", "server": "BeroHost-Fra"}])
    base = f"/api/conversations/{CONV}/work-items/1.2/file"
    meta = await (await client.get(base, params={"ref": "item:0"})).json()
    assert meta["artifact"]["fileName"] == "app.py" and meta["artifact"]["projectFile"] is True
    assert await (await client.get(base + "/content", params={"ref": "item:0"})).text() == "print('ok')\n"
    assert (await client.get(base, params={"ref": "item:1"})).status == 403
    report = await (await client.get(base, params={"ref": "item:2"})).json()
    assert report["artifact"]["projectFile"] is False
    # Any directory on this machine opens; a file on another server does not.
    assert await (await client.get(base + "/content", params={"ref": "item:3"})).text() == "package main\n"
    assert (await client.get(base, params={"ref": "item:4"})).status == 404
    detail = await (await client.get(f"/api/conversations/{CONV}/work-items/1.2")).json()
    assert detail["item"]["evidence"][4]["server"] == "BeroHost-Fra"
    assert (await client.get(base, params={"ref": "item:9"})).status == 404
