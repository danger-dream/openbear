"""Trigger-owned conversation retention and conversation-tree batch actions."""
from __future__ import annotations

import json
import time
import uuid

import pytest

from app.cron.contracts import JobConfig
from app.db.dao import MessageDAO
from app.webhooks.contracts import WebhookError, parse_config, Scope
from app.webhooks.repository import insert, one
from tests.test_web_admin import _login_cookie
from tests.test_webhooks_backend import create, env  # noqa: F401

DAY = 86400


async def _cookie(env):
    await env.server.ensure_secret_key()
    return {"openbear_web_session": await _login_cookie(env)}


async def _created(env, row, created_at):
    await env.db.conn.execute("UPDATE web_conversations SET created_at=? WHERE conversation_uuid=?",
                              (created_at, row["conversation_uuid"]))
    await env.db.conn.commit()


async def _webhook_conversation(env, endpoint_id, created_at, *, state="finalized", notification="delivered"):
    aid = str(uuid.uuid4())
    row = await env.server._create_web_conversation(123, title="event " + aid[:6], folder_uuid="folder",
                                                    origin_kind="webhook", origin_ref=aid)
    key = "webhook:assignment:" + aid + ":final"
    now_ms = int(time.time() * 1000)
    async with env.db.webhook_transaction() as conn:
        await insert(conn, "webhook_assignments", assignment_id=aid, origin_kind="webhook",
                     conversation_uuid=row["conversation_uuid"], internal_chat_id=row["internal_chat_id"],
                     root_turn_uuid="root:" + aid, origin_endpoint_id=endpoint_id, origin_revision=1,
                     task_start_cursor=0, authorization_snapshot_json="{}", state=state, created_at_ms=now_ms,
                     finalized_at_ms=now_ms if state == "finalized" else None,
                     final_snapshot_json="{}" if state == "finalized" else None,
                     notification_key=key if state == "finalized" else None)
        if state == "finalized":
            await insert(conn, "web_task_notifications", notification_uuid=aid, notification_key=key,
                         conversation_uuid=row["conversation_uuid"], internal_chat_id=row["internal_chat_id"],
                         owner_chat_id=123, kind="webhook-result", payload_json="{}", state=notification,
                         created_at=now_ms // 1000, updated_at=now_ms // 1000)
    await _created(env, row, created_at)
    return row


async def _rows(env, ids):
    return [await env.server._conversation_row(123, conv_uuid) for conv_uuid in ids]


async def _exists(env, row):
    return await env.server._conversation_row(123, row["conversation_uuid"]) is not None


async def test_webhook_retention_keeps_newest_untouched_conversations_and_protects_user_choices(env):
    c = await create(env, target={"mode": "newConversation", "conversationRetention": {"keepLast": 2}})
    eid = c["endpoint"]["id"]
    now = int(time.time())
    newest = await _webhook_conversation(env, eid, now - 10)
    pending = await _webhook_conversation(env, eid, now - 20, notification="pending")
    old = await _webhook_conversation(env, eid, now - 30)
    open_run = await _webhook_conversation(env, eid, now - 40, state="open")
    pinned = await _webhook_conversation(env, eid, now - 50)
    archived = await _webhook_conversation(env, eid, now - 60)
    continued = await _webhook_conversation(env, eid, now - 70)
    moved = await _webhook_conversation(env, eid, now - 80)
    oldest = await _webhook_conversation(env, eid, now - 90)
    await MessageDAO(env.db).add(int(oldest["internal_chat_id"]), "user", "transcript to remove")
    await env.db.conn.execute("UPDATE web_conversations SET pinned_at=1 WHERE conversation_uuid=?", (pinned["conversation_uuid"],))
    await env.db.conn.execute("UPDATE web_conversations SET archived_at=1 WHERE conversation_uuid=?", (archived["conversation_uuid"],))
    await env.db.conn.execute("UPDATE web_conversations SET last_interaction_at_ms=1 WHERE conversation_uuid=?", (continued["conversation_uuid"],))
    await env.db.conn.execute("INSERT INTO web_conversation_folders(folder_uuid,owner_chat_id,name) VALUES('other',123,'Other')")
    await env.db.conn.execute("UPDATE web_conversations SET folder_uuid='other' WHERE conversation_uuid=?", (moved["conversation_uuid"],))
    await env.db.conn.commit()
    manual = await env.server._create_web_conversation(123, title="manual", folder_uuid="folder")
    await _created(env, manual, now - 1000)

    cookie = await _cookie(env)
    preview = await env.client.post("/api/conversation-retention/preview", cookies=cookie,
                                    json={"kind": "webhook", "id": eid, "retention": {"keepLast": 2}})
    assert (await preview.json())["count"] == 2

    assert await env.server._sweep_conversation_retention() == 2

    # Pending notification and the open assignment occupy quota but are never deleted.
    for row in (newest, pending, open_run, pinned, archived, continued, moved, manual):
        assert await _exists(env, row)
    for row in (old, oldest):
        assert not await _exists(env, row)
    assert await MessageDAO(env.db).recent(int(oldest["internal_chat_id"])) == []
    audit = await one(env.db.conn, "SELECT detail_json FROM audit_logs WHERE kind='web.conversation.auto_delete' ORDER BY id DESC LIMIT 1")
    detail = json.loads(audit["detail_json"])
    assert detail["source"] == "webhook" and detail["sourceId"] == eid and detail["retention"]["keepLast"] == 2
    assert await env.server._sweep_conversation_retention() == 0


async def test_retention_deletes_when_either_limit_is_exceeded_and_unset_keeps_everything(env):
    c = await create(env)
    eid = c["endpoint"]["id"]
    now = int(time.time())
    rows = [await _webhook_conversation(env, eid, now - index * DAY - 60) for index in range(5)]
    assert await env.server._sweep_conversation_retention() == 0

    current = await env.s.get(123, eid)
    config = current["endpoint"]["config"]
    config["target"]["conversationRetention"] = {"keepLast": 4, "keepDays": 2}
    await env.s.update(123, eid, {"requestId": "retention", "expectedRevision": current["endpoint"]["revision"], "config": config})

    assert await env.server._sweep_conversation_retention() == 3
    assert [await _exists(env, row) for row in rows] == [True, True, False, False, False]


async def test_cron_retention_respects_unfinished_runs_and_pending_notifications(env):
    now = int(time.time())
    config = JobConfig.model_validate({"conversationRetention": {"keepDays": 1}}).wire()
    await env.db.conn.execute(
        "INSERT INTO cron_jobs(job_id,owner_chat_id,folder_id,name,config_json,created_at_ms,updated_at_ms) VALUES('job',123,'folder','Job',?,0,0)",
        (json.dumps(config),),
    )
    rows = {}
    for name, phase, notification in (("recent", "finished", "enqueued"), ("done", "finished", "enqueued"),
                                      ("running", "model", "not_requested"), ("notifying", "finished", "pending")):
        run_id = "run-" + name
        await env.db.conn.execute(
            """INSERT INTO cron_runs(run_id,job_id,owner_chat_id,folder_id,job_name,trigger_kind,scheduled_at_ms,occurrence_key,
                   config_json,config_revision,root_turn_uuid,phase,notification_state,started_at_ms)
               VALUES(?,'job',123,'folder','Job','schedule',0,?,?,1,?,?,?,0)""",
            (run_id, run_id, json.dumps(config), "cron:" + run_id, phase, notification),
        )
        await env.db.conn.commit()
        row = await env.server._create_web_conversation(123, title=name, folder_uuid="folder", origin_kind="cron", origin_ref=run_id)
        await _created(env, row, now - 60 if name == "recent" else now - 3 * DAY)
        rows[name] = row

    assert await env.server._sweep_conversation_retention() == 1
    assert {name: await _exists(env, row) for name, row in rows.items()} == {
        "recent": True, "done": False, "running": True, "notifying": True}


async def test_retention_contracts():
    retention = {"keepLast": 5}
    with pytest.raises(WebhookError):
        parse_config({"target": {"mode": "fixedConversation", "conversationId": "c", "conversationRetention": retention}},
                     Scope(type="folder", id="f"), _system())
    with pytest.raises(WebhookError):
        parse_config({"target": {"conversationRetention": {"keepLast": 0}}}, Scope(type="folder", id="f"), _system())
    assert JobConfig.model_validate({"conversationRetention": {"keepDays": 3}}).wire()["conversationRetention"] == {"keepLast": None, "keepDays": 3}


def _system():
    from app.webhooks.contracts import WebhooksConfig
    return WebhooksConfig()


async def test_batch_delete_archive_and_move(env):
    cookie = await _cookie(env)
    rows = [await env.server._create_web_conversation(123, title=f"c{index}", folder_uuid="folder") for index in range(4)]
    ids = [row["conversation_uuid"] for row in rows]
    await env.db.conn.execute("INSERT INTO web_conversation_folders(folder_uuid,owner_chat_id,name) VALUES('target',123,'Target')")
    await env.db.conn.execute("UPDATE web_conversations SET status='running' WHERE conversation_uuid=?", (ids[1],))
    await env.db.conn.commit()

    response = await env.client.post("/api/conversations/batch", cookies=cookie,
                                     json={"action": "delete", "conversationIds": [ids[0], ids[1], "missing"]})
    data = await response.json()
    assert (data["succeeded"], data["failed"]) == (1, 2)
    assert [item.get("error") for item in data["results"]] == [None, "conversation_running", "conversation_not_found"]
    assert not await _exists(env, rows[0]) and await _exists(env, rows[1])

    response = await env.client.post("/api/conversations/batch", cookies=cookie,
                                     json={"action": "archive", "conversationIds": ids[2:]})
    assert (await response.json())["succeeded"] == 2
    assert [bool(int(row["archived_at"])) for row in await _rows(env, ids[2:])] == [True, True]
    response = await env.client.post("/api/conversations/batch", cookies=cookie,
                                     json={"action": "unarchive", "conversationIds": ids[2:]})
    assert (await response.json())["succeeded"] == 2
    assert [bool(int(row["archived_at"])) for row in await _rows(env, ids[2:])] == [False, False]

    impact = await env.client.post("/api/conversations/batch", cookies=cookie,
                                   json={"action": "move", "conversationIds": ids[2:], "targetFolderId": "target", "impactOnly": True})
    assert (await impact.json())["ok"] is True
    assert [row["folder_uuid"] for row in await _rows(env, ids[2:])] == ["folder", "folder"]
    moved = await env.client.post("/api/conversations/batch", cookies=cookie,
                                  json={"action": "move", "conversationIds": ids[2:], "targetFolderId": "target"})
    assert (await moved.json())["succeeded"] == 2
    assert [row["folder_uuid"] for row in await _rows(env, ids[2:])] == ["target", "target"]

    invalid = await env.client.post("/api/conversations/batch", cookies=cookie, json={"action": "explode", "conversationIds": ids})
    assert invalid.status == 400
    missing_folder = await env.client.post("/api/conversations/batch", cookies=cookie,
                                           json={"action": "move", "conversationIds": ids[2:], "targetFolderId": "nope"})
    assert missing_folder.status == 404
