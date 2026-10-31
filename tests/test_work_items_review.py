from __future__ import annotations

import json

import pytest

from app.db.engine import DB
from app.work_items import Actor, WorkItemError, WorkItemsDAO, WorkItemsRuntime, render_outline
from tests.test_work_items import CONV, TURN, controller, questionnaire, seeded, user_message


@pytest.fixture
async def db(tmp_path):
    database = DB(str(tmp_path / "review.db"))
    await database.connect()
    yield database
    await database.close()


async def confirmation(db, decision, text=""):
    await questionnaire(db, "confirm", TURN)
    payload = {"action": "confirm", "title": "决定", "body": "放弃这一项"}
    result = {"action": "confirm", "decision": decision, "text": text}
    await db.conn.execute("UPDATE user_interactions SET payload_json=?, result_json=? WHERE interaction_id='confirm'",
                          (json.dumps(payload), json.dumps(result)))
    await db.conn.commit()


@pytest.mark.parametrize("decision", ["feedback", "reject"])
async def test_feedback_is_evidence_but_not_approval(db, decision):
    dao = await seeded(db)
    await confirmation(db, decision, "放弃这一项是什么意思？")
    source = await dao.resolve_source(CONV, controller(), {"interactionId": "confirm", "quote": "放弃这一项是什么意思"})
    await dao.decide(CONV, controller(), ["1.3"], source)
    recorded = (await dao.detail(CONV, "1.3"))["events"][-1]
    assert recorded["data"]["questions"][0]["text"] == "放弃这一项是什么意思？"
    with pytest.raises(WorkItemError, match="does not approve"):
        await dao.transition(CONV, controller(), "1.3", "drop", source=source)
    with pytest.raises(WorkItemError, match="does not approve"):
        await dao.resolve_source(CONV, controller(), {"interactionId": "confirm", "quote": "放弃这一项"},
                                 require_human=True, require_quote=True)
    with pytest.raises(WorkItemError) as exc:
        await dao.resolve_source(CONV, controller(), {"interactionId": "confirm", "quote": "决定"})
    assert exc.value.code == "quote_not_found"


async def test_interaction_state_changes_store_answers_and_check_quotes(db):
    dao = await seeded(db)
    await questionnaire(db, "questionnaire", TURN)
    with pytest.raises(WorkItemError) as exc:
        await dao.resolve_source(CONV, controller(), {"interactionId": "questionnaire"}, require_human=True, require_quote=True)
    assert exc.value.code == "quote_required"
    with pytest.raises(WorkItemError) as exc:
        await dao.resolve_source(CONV, controller(), {"interactionId": "questionnaire", "quote": "动手时才建"},
                                 require_human=True, require_quote=True)
    assert exc.value.code == "quote_not_found"
    source = await dao.resolve_source(CONV, controller(), {"interactionId": "questionnaire", "quote": "一提出就建"},
                                     require_human=True, require_quote=True)
    await dao.transition(CONV, controller(), "1", "phase", phase="decided", source=source)
    event = (await dao.detail(CONV, "1"))["events"][-1]
    assert event["data"]["questions"][0]["selected"] == ["一提出就建"]
    assert event["data"]["questions"][0]["text"] == "调查也记进去"
    assert event["source"]["quote"] == "一提出就建"
    assert "interaction" not in event["source"]


@pytest.mark.parametrize("decision", ["confirm", "confirm_with_changes"])
async def test_approved_confirmation_can_quote_proposal_and_preserves_amendments(db, decision):
    dao = await seeded(db)
    await confirmation(db, decision, "只限这一项")
    source = await dao.resolve_source(CONV, controller(), {"interactionId": "confirm", "quote": "放弃这一项"},
                                     require_human=True, require_quote=True)
    await dao.transition(CONV, controller(), "1.3", "drop", source=source)
    event = (await dao.detail(CONV, "1.3"))["events"][-1]
    assert event["data"]["questions"][0]["text"] == "只限这一项"
    assert event["data"]["questions"][0]["selected"] == [decision]


async def test_parent_drop_and_reopen_restore_only_the_same_cascade(db):
    dao = await seeded(db)
    source = await dao.resolve_source(CONV, controller(), {})
    await dao.set_focus(CONV, TURN, ["1"], basis=source.public(), actor=controller())
    await dao.transition(CONV, controller(), "1.1.1", "complete", summary="done",
                         evidence=[{"kind": "test", "text": "passed"}])
    await dao.transition(CONV, controller(), "1.3", "drop", source=source)
    await dao.transition(CONV, controller(), "1.1.2", "start")
    await dao.transition(CONV, controller(), "1.2", "block", reason="等待环境")
    await dao.transition(CONV, controller(), "1", "drop", source=source)
    rows = {row["number"]: row for row in await dao.rows(CONV)}
    assert rows["1"]["status"] == "abandoned"
    assert rows["1.1.1"]["status"] == "done"
    assert all(rows[n]["status"] == "dropped" for n in ["1.1", "1.1.2", "1.2", "1.3"])
    assert await dao.unfinished(CONV, TURN) == []
    for action in ["start", "complete", "reopen"]:
        with pytest.raises(WorkItemError) as exc:
            await dao.transition(CONV, controller(), "1.1.2", action, source=source, reason="again")
        assert exc.value.code == "parent_closed"
    await dao.transition(CONV, controller(), "1", "reopen", source=source, reason="继续")
    rows = {row["number"]: row for row in await dao.rows(CONV)}
    assert rows["1.1.1"]["status"] == "done"
    assert rows["1.1.2"]["status"] == "todo"
    assert rows["1.2"]["status"] == "blocked"
    assert rows["1.2"]["statusReason"] == "等待环境"
    assert rows["1.3"]["status"] == "dropped"
    assert [row["number"] for row in await dao.unfinished(CONV, TURN)] == ["1.1.2"]
    await dao.transition(CONV, controller(), "1.1", "drop", source=source)
    await dao.transition(CONV, controller(), "1", "drop", source=source)
    await dao.transition(CONV, controller(), "1", "reopen", source=source, reason="继续其余部分")
    assert (await dao.get_row(CONV, "1.1"))["status"] == "dropped"
    assert (await dao.get_row(CONV, "1.1.2"))["status"] == "dropped"
    assert (await dao.find(CONV, "1.1"))["effectiveStatus"] == "dropped"
    event = (await dao.detail(CONV, "1.2"))["events"][-1]
    assert event["source"]["opId"] == "msg:a"
    assert event["data"]["cascadeRoot"] == "1"


async def test_closed_ancestor_guards_legacy_open_children(db):
    dao = await seeded(db)
    await db.conn.execute("UPDATE work_items SET status='abandoned' WHERE number='1'")
    await db.conn.commit()
    with pytest.raises(WorkItemError, match="reopen it first"):
        await dao.set_focus(CONV, TURN, ["1.1.1"], basis={}, actor=controller())
    with pytest.raises(WorkItemError, match="reopen it first"):
        await dao.add(CONV, controller(), "1.1", ["new"])
    with pytest.raises(WorkItemError, match="reopen it first"):
        await dao.transition(CONV, controller(), "1.1.1", "start")


async def test_childless_task_is_checked_and_stays_visible_when_blocked(db):
    dao = WorkItemsDAO(db)
    await user_message(db, "m", TURN, "做这个")
    await dao.create(CONV, controller(), {"title": "单项任务"})
    await dao.set_focus(CONV, TURN, ["1"], basis={}, actor=controller())
    await dao.transition(CONV, controller(), "1", "start")
    assert [item["number"] for item in await dao.unfinished(CONV, TURN)] == ["1"]
    assert await WorkItemsRuntime(dao, CONV, TURN).finish_check() is not None
    for action in ["block", "ask"]:
        await dao.transition(CONV, Actor("user"), "1", action, reason="需要说明")
        outline = render_outline(await dao.tree(CONV))
        assert "单项任务" in outline and "需要说明" in outline
        assert await dao.unfinished(CONV, TURN) == []


@pytest.mark.parametrize("budget", [100, 300, 1000, 6000])
async def test_outline_budget_includes_titles_focus_recent_and_tags(db, budget):
    dao = WorkItemsDAO(db)
    for _ in range(15):
        await dao.create(CONV, controller(), {"title": "很长的标题" * 60, "children": ["子项" * 100]})
    recent = [{"createdAtMs": 1000, "number": "1", "summary": "修改" * 200} for _ in range(50)]
    focus = {"numbers": [str(i) for i in range(1, 16)], "basis": {"human": True}}
    text = render_outline(await dao.tree(CONV), focus=focus, recent=recent, max_chars=budget)
    assert len(text) <= budget
    assert "省略" in text
    assert text.endswith("</work-items>")
    if budget >= 300:
        assert "本轮执行范围" in text
    for line in text.splitlines():
        if line.startswith("  #"):
            parent = line.split("#")[1].split(".")[0]
            assert f"\n#{parent} " in text
