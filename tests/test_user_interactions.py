from __future__ import annotations

import asyncio
import json

import pytest

from app.db.engine import DB
from app.interaction_data import (
    canonical_answer,
    explicit_authorization,
    normalize_definition,
    redact_result,
)
from app.user_interactions import InteractionService


@pytest.fixture
async def interactions(tmp_path):
    db = DB(str(tmp_path / "interactions.sqlite"))
    await db.connect()
    service = InteractionService(db)
    await service.start()
    yield service
    await service.stop()
    await asyncio.sleep(0)
    await db.close()


async def pending(service, action="select", **extra):
    ready = asyncio.Event()
    seen = []

    async def listener(event, item):
        if event == "created":
            seen.append(item)
            ready.set()

    service.add_listener(listener)
    payload = {"action": action, "title": "问题", "body": "请选择或填写", "timeoutSeconds": 10}
    if action == "select":
        payload["options"] = [{"label": "方案 A", "value": "a"}, {"label": "方案 B", "value": "b"}]
    payload.update(extra)
    waiter = asyncio.create_task(service.request(payload, owner_chat_id=123, conversation_uuid="conv", turn_uuid="turn", tool_call_id="call"))
    await asyncio.wait_for(ready.wait(), 2)
    return seen[-1], waiter


@pytest.mark.parametrize("values", [[], ["a"]])
async def test_selection_and_original_text_survive_both_channels(interactions, values):
    item, waiter = await pending(interactions)
    text = "  不按选项执行，保留原文\n第二行  "
    response = await interactions.submit(item["interactionId"], 123, {"selectedValues": values, "text": text}, source="telegram")
    assert response["ok"]
    result = await waiter
    assert result == response["result"]
    assert result["text"] == text
    assert result["selectedValues"] == values
    assert result["answerMode"] == ("options_with_text" if values else "text")
    assert result["source"] == "telegram"
    assert interactions.pending_for("conv") == []


async def test_concurrent_submits_only_one_wins_and_retry_returns_canonical(interactions):
    item, waiter = await pending(interactions)
    cid = item["interactionId"]
    responses = await asyncio.gather(
        interactions.submit(cid, 123, {"selectedValues": ["a"]}, source="web"),
        interactions.submit(cid, 123, {"selectedValues": ["b"]}, source="telegram"),
    )
    assert sum(response["ok"] for response in responses) == 1
    assert sorted(response["statusCode"] for response in responses) == [200, 409]
    result = await waiter
    replay = await interactions.submit(cid, 123, {"selectedValues": result["selectedValues"]}, source="telegram")
    assert replay["replayed"] is True
    assert replay["result"] == result


async def test_confirm_text_never_authorizes_and_preserves_original_button(interactions):
    item, waiter = await pending(interactions, "confirm", _requiresAuthorization=True)
    response = await interactions.submit(item["interactionId"], 123, {"confirmed": True, "text": "先别重启"})
    result = await waiter
    assert response["ok"]
    assert result["decision"] == "feedback"
    assert result["selectedDecision"] == "confirm"
    assert result["confirmed"] is False
    assert result["authorizationGranted"] is False
    assert result["text"] == "先别重启"
    assert result["cancelled"] is False


async def test_authorization_select_preserves_options_but_blocks_execution_on_feedback(interactions):
    item, waiter = await pending(interactions, _requiresAuthorization=True)
    response = await interactions.submit(item["interactionId"], 123, {"selectedValues": ["a"], "text": "先调整参数"})
    assert response["ok"]
    result = await waiter
    assert result["selectedValues"] == ["a"]
    assert result["authorizationGranted"] is False
    assert result["decision"] == "feedback"


@pytest.mark.parametrize("action,defaults", [
    ("confirm", {"default": True}),
    ("select", {"defaultValues": ["a"]}),
    ("prompt", {"defaultValue": "不能当作答案"}),
    ("questionnaire", {"questions": [{"id": "q", "type": "open", "question": "问题"}]}),
])
async def test_timeout_never_answers_using_defaults(interactions, action, defaults):
    item, waiter = await pending(interactions, action, timeoutSeconds=0.04, **defaults)
    result = await asyncio.wait_for(waiter, 1)
    assert result["status"] == "timeout"
    assert result["cancelled"] is True
    assert not result.get("confirmed")
    assert not result.get("selectedValues")
    assert not result.get("value")
    expired = await interactions.submit(item["interactionId"], 123, {"confirmed": True, "value": "late", "text": "late"})
    assert expired["statusCode"] == 409
    assert expired["error"] == "confirmation_expired"


async def test_wrong_owner_revision_and_invalid_answer_keep_pending(interactions):
    item, waiter = await pending(interactions)
    cid = item["interactionId"]
    assert await interactions.get(cid, owner_chat_id=999) is None
    assert (await interactions.submit(cid, 999, {"text": "x"}))["statusCode"] == 404
    assert (await interactions.submit(cid, 123, {"text": "x", "revision": 3}))["statusCode"] == 409
    for body in ({}, {"selectedValues": ["missing"]}, {"selectedValues": ["a", "b"]}, {"selectedIndexes": [-1]}, {"text": 123}, {"text": "x" * 33000}):
        assert (await interactions.submit(cid, 123, body))["statusCode"] == 400
        assert not waiter.done()
    await interactions.submit(cid, 123, {"cancelled": True, "text": "草稿不得上传"})
    result = await waiter
    assert result["text"] == ""
    assert result["selectedValues"] == []


async def test_questionnaire_text_only_and_options_plus_text(interactions):
    questions = [
        {"id": "q1", "type": "choice", "question": "方向", "options": [{"label": "A", "value": "a"}]},
        {"id": "q2", "type": "open", "question": "说明", "required": False},
    ]
    item, waiter = await pending(interactions, "questionnaire", questions=questions)
    response = await interactions.submit(item["interactionId"], 123, {"answers": [{"questionId": "q1", "selectedValues": [], "text": "  自己的答案\n  "}]}, source="telegram")
    assert response["ok"]
    result = await waiter
    assert result["answers"][0]["text"] == "  自己的答案\n  "
    assert result["answers"][0]["answerMode"] == "text"
    assert result["answers"][1]["answerMode"] == "unanswered"


async def test_sensitive_input_is_web_only_and_never_stored_in_notification_or_ledger(interactions):
    secret = "NEVER-PERSIST-THIS-SECRET"
    item, waiter = await pending(interactions, "prompt", sensitive=True, body=secret, defaultValue=secret)
    cid = item["interactionId"]
    assert secret not in json.dumps(item)
    assert secret not in json.dumps(await interactions.get(cid))
    assert (await interactions.submit(cid, 123, {"value": secret}, source="telegram"))["statusCode"] == 403
    assert interactions.pending_for("conv")[0]["defaultValue"] == secret
    response = await interactions.submit(cid, 123, {"value": secret}, source="web")
    assert response["result"]["value"] == secret
    assert (await waiter)["value"] == secret
    cur = await interactions.db.conn.execute("SELECT * FROM user_interactions WHERE interaction_id=?", (cid,))
    assert secret not in json.dumps(dict(await cur.fetchone()))


async def test_cancelled_original_waiter_invalidates_pending(interactions):
    item, waiter = await pending(interactions)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert interactions.pending_for("conv") == []
    assert (await interactions.get(item["interactionId"]))["status"] == "interrupted"
    assert not (await interactions.submit(item["interactionId"], 123, {"text": "late"}))["ok"]


async def test_restart_marks_old_pending_interrupted_not_resumable(interactions):
    item, waiter = await pending(interactions)
    # Simulate the durable row from a dead process without adopting its Future.
    restarted = InteractionService(interactions.db)
    await restarted.start()
    response = await restarted.submit(item["interactionId"], 123, {"text": "late"})
    assert response["statusCode"] == 409
    assert (await restarted.get(item["interactionId"]))["status"] == "interrupted"
    # Release this test's stand-in for the dead process without hanging teardown.
    interactions.pending.pop(item["interactionId"])["future"].set_result({"status": "interrupted"})
    await waiter


async def test_notification_failure_does_not_prevent_answer(interactions):
    async def broken(_event, _item):
        raise RuntimeError("transport unavailable")
    interactions.add_listener(broken)
    item, waiter = await pending(interactions, "confirm")
    result = await interactions.submit(item["interactionId"], 123, {"confirmed": True})
    assert result["ok"]
    assert explicit_authorization(await waiter)


def test_shared_gate_and_redaction_cover_text_fields():
    assert not explicit_authorization({"confirmed": True, "text": "先别做"})
    assert not explicit_authorization({"confirmed": True, "status": "timeout"})
    assert explicit_authorization({"confirmed": True})
    result = redact_result({"text": "secret", "value": "secret", "selectedLabels": ["secret"], "answers": [{"questionId": "q", "text": "secret", "selectedValues": ["secret"]}]})
    assert "secret" not in json.dumps(result)
