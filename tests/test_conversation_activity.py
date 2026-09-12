"""Durable activity inbox: actual SQLite operations, tree API and owner-scoped read receipts."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.db.engine import DB
from app.web_console.activity import activity_fields, clear_deleted_completion
from app.web_console.realtime import GlobalRealtime
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as shared_web_env

web_env = shared_web_env


async def publish(env, row, op_id="run:one", *, op_type="run", status="completed", payload=None, conn=None):
    return await env.server._publish_operation(
        row["conversation_uuid"], internal_chat_id=row["internal_chat_id"], owner_chat_id=123,
        op_id=op_id, op_type=op_type, action="start" if status == "running" else "end",
        turn_uuid="turn-one", status=status, payload=payload or {}, conn=conn,
    )


async def fields(env, row):
    saved = await env.server._conversation_row(123, row["conversation_uuid"])
    return activity_fields(saved)


async def read(env, row, version):
    return await env.client.post("/api/conversation-tree/read", json={"items": [{"conversationUuid": row["conversation_uuid"], "version": version}]})


async def test_completed_without_browser_survives_reconnect_and_read_is_idempotent(web_env):
    env = web_env
    row = await env.server._create_web_conversation(123)
    await publish(env, row, status="running")
    assert not (await fields(env, row))["activityUnread"]
    terminal = await publish(env, row)
    saved = await fields(env, row)
    assert saved["activityVersion"] == 1 and saved["activityUnread"]
    assert saved["activityResult"]["frameSeq"] == terminal["frameSeq"]
    # A separate connection represents a reload/device/server with no retained UI state.
    with sqlite3.connect(env.db.path) as connection:
        assert connection.execute("SELECT activity_version,activity_read_version FROM web_conversations WHERE conversation_uuid=?", (row["conversation_uuid"],)).fetchone() == (1, 0)
    await _login_cookie(env)
    status = await (await env.client.get("/api/conversation-tree/status")).json()
    assert status["items"] == []
    assert status["activityItems"][0]["activityState"] == "completed"
    for _ in range(2):
        response = await read(env, row, 1)
        assert response.status == 200
    assert not (await fields(env, row))["activityUnread"]
    assert not (await (await env.client.get("/api/conversation-tree/status")).json())["activityItems"]
    await publish(env, row, payload={"enriched": True})
    assert (await fields(env, row))["activityVersion"] == 1


async def test_global_channel_delivers_completion_and_other_device_read_without_catalog_change(web_env):
    env = web_env
    await _login_cookie(env)
    row = await env.server._create_web_conversation(123)
    hub = GlobalRealtime(env.server)
    queues = [asyncio.Queue(), asyncio.Queue()]
    for queue in queues:
        await hub.snapshot(queue, 123, False)
        assert (await queue.get())["treeStatus"]["activityItems"] == []
    await publish(env, row)
    hub.last_calibration = 0
    await hub.refresh()
    for queue in queues:
        packet = await queue.get()
        assert packet["type"] == "patch"
        assert packet["treeStatus"]["activityItems"][0]["activityUnread"]
    assert (await read(env, row, 1)).status == 200
    hub.last_calibration = 0
    await hub.refresh()
    for queue in queues:
        packet = await queue.get()
        assert packet["type"] == "patch"
        assert packet["treeStatus"]["activityItems"] == []
    await hub.close()


async def test_stale_run_recovery_returns_interrupted_unread_in_same_status_response(web_env):
    env = web_env
    row = await env.server._create_web_conversation(123)
    await publish(env, row, status="running")
    await env.server._touch_web_conversation(row["conversation_uuid"], status="running")
    status = await env.server._tree_running_state(123)
    assert status["items"] == []
    assert len(status["activityItems"]) == 1
    assert status["activityItems"][0]["activityState"] == "interrupted"
    assert status["activityItems"][0]["activityUnread"]
    assert (await fields(env, row))["activityVersion"] == 1
    await env.server._tree_running_state(123)
    assert (await fields(env, row))["activityVersion"] == 1


async def test_stale_read_and_concurrent_completions_never_swallow_new_results(web_env):
    env = web_env
    await _login_cookie(env)
    row = await env.server._create_web_conversation(123)
    await publish(env, row)
    await publish(env, row, "run:two")
    response = await read(env, row, 1)
    assert response.status == 200
    saved = await fields(env, row)
    assert (saved["activityVersion"], saved["activityReadVersion"], saved["activityUnread"]) == (2, 1, True)
    await asyncio.gather(read(env, row, 2), publish(env, row, "agent:three", op_type="agent"))
    saved = await fields(env, row)
    assert (saved["activityVersion"], saved["activityReadVersion"], saved["activityUnread"]) == (3, 2, True)
    assert (await read(env, row, 99)).status == 409
    assert (await fields(env, row))["activityReadVersion"] == 2


async def test_root_done_while_agent_runs_stays_running_then_agent_terminal_is_unread(web_env):
    env = web_env
    row = await env.server._create_web_conversation(123)
    uuid = row["conversation_uuid"]
    env.server._web_starting_turns[uuid] = "hold-reconciliation"
    try:
        await publish(env, row, "agent:child", op_type="agent", status="running", payload={"taskUuid": "child"})
        await publish(env, row)
        status = await env.server._tree_running_state(123)
        assert len(status["items"]) == len(status["activityItems"]) == 1
        assert status["activityItems"][0]["activityUnread"]
        await publish(env, row, "agent:child", op_type="agent", status="failed", payload={"taskUuid": "child"})
    finally:
        env.server._web_starting_turns.pop(uuid, None)
    status = await env.server._tree_running_state(123)
    assert not status["items"]
    assert status["activityItems"][0]["activityState"] == "failed"
    assert status["activityItems"][0]["activityVersion"] == 2


@pytest.mark.parametrize("op_type,status,payload", [
    ("user_interaction", "running", {"interactionStatus": "pending"}),
    ("agent", "needs_openbear_control", {"taskUuid": "child"}),
])
async def test_waiting_rows_are_attention_and_do_not_mark_a_completion(web_env, op_type, status, payload):
    env = web_env
    row = await env.server._create_web_conversation(123)
    env.server._web_starting_turns[row["conversation_uuid"]] = "hold"
    try:
        await publish(env, row, "wait:one", op_type=op_type, status=status, payload=payload)
        item = (await env.server._tree_running_state(123))["activityItems"][0]
        assert item["activityState"] == "waiting" and item["running"]
        assert not item["activityUnread"]
    finally:
        env.server._web_starting_turns.pop(row["conversation_uuid"], None)


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted", "partial"])
async def test_terminal_outcomes_are_preserved(web_env, status):
    row = await web_env.server._create_web_conversation(123)
    await publish(web_env, row, status=status)
    item = (await web_env.server._tree_running_state(123))["activityItems"][0]
    assert item["activityState"] == status and item["activityUnread"]


async def test_non_execution_operations_and_merged_agent_redirect_never_create_unread(web_env):
    env = web_env
    row = await env.server._create_web_conversation(123)
    for kind in ["tool", "stats", "context_compaction", "notice", "assistant_message", "agent_control"]:
        await publish(env, row, f"{kind}:one", op_type=kind, payload={"complete": True})
    await publish(env, row, "agent:redirect", op_type="agent", payload={"merged": True})
    assert (await fields(env, row))["activityVersion"] == 0
    assert (await env.server._tree_running_state(123))["activityItems"] == []


async def test_completion_and_operation_roll_back_together(web_env):
    env = web_env
    row = await env.server._create_web_conversation(123)
    with pytest.raises(RuntimeError, match="rollback"):
        async with env.db.web_operation_transaction() as conn:
            await publish(env, row, conn=conn)
            raise RuntimeError("rollback")
    assert (await fields(env, row))["activityVersion"] == 0
    cur = await env.db.conn.execute("SELECT COUNT(*) FROM web_operations WHERE conversation_uuid=?", (row["conversation_uuid"],))
    assert (await cur.fetchone())[0] == 0


async def test_read_scope_and_batch_validation_are_atomic_and_not_future_wide(web_env):
    env = web_env
    await _login_cookie(env)
    first = await env.server._create_web_conversation(123)
    second = await env.server._create_web_conversation(123)
    other = await env.server._create_web_conversation(999)
    for row in [first, second, other]:
        await publish(env, row)
    response = await env.client.post("/api/conversation-tree/read", json={"items": [
        {"conversationUuid": first["conversation_uuid"], "version": 1},
        {"conversationUuid": other["conversation_uuid"], "version": 1},
    ]})
    assert response.status == 404 and (await fields(env, first))["activityUnread"]
    response = await env.client.post("/api/conversation-tree/read", json={"items": [
        {"conversationUuid": row["conversation_uuid"], "version": 1} for row in [first, second]
    ]})
    assert response.status == 200
    assert not (await fields(env, first))["activityUnread"] and not (await fields(env, second))["activityUnread"]
    with sqlite3.connect(env.db.path) as conn:
        assert conn.execute("SELECT activity_read_version FROM web_conversations WHERE owner_chat_id=999").fetchone()[0] == 0
    assert (await read(env, first, True)).status == 400


async def test_tree_alias_keeps_folder_counts_and_duplicate_does_not_copy_unread(web_env):
    env = web_env
    row = await env.server._create_web_conversation(123)
    await publish(env, row)
    before = (await env.server._tree_running_state(123))["folderConversationCounts"]
    duplicate = await env.server._duplicate_web_conversation_data(row)
    assert not activity_fields(duplicate)["activityUnread"]
    status = await env.server._tree_running_state(123)
    assert status["folderConversationCounts"][""] == before[""] + 1
    assert len(status["activityItems"]) == 1
    saved = await env.server._conversation_row(123, row["conversation_uuid"])
    assert saved["folder_uuid"] == row["folder_uuid"]
    async with env.db.web_operation_transaction() as conn:
        await conn.execute("DELETE FROM web_operations WHERE conversation_uuid=?", (row["conversation_uuid"],))
        await clear_deleted_completion(conn, row["conversation_uuid"])
    assert not (await fields(env, row))["activityUnread"]


async def start_interaction(env, row, payload):
    created = asyncio.Event()
    captured = {}

    async def listener(event, item):
        if event == "created" and item["conversationUuid"] == row["conversation_uuid"]:
            captured.update(item)
            created.set()

    env.server.interactions.add_listener(listener)
    task = asyncio.create_task(env.server.interactions.request(
        {"title": "not for the global tree", "body": "private question body", "timeoutSeconds": 60, **payload},
        owner_chat_id=row["owner_chat_id"], conversation_uuid=row["conversation_uuid"],
    ))
    await asyncio.wait_for(created.wait(), 2)
    return task, captured["interactionId"]


@pytest.mark.parametrize("source,action,extra", [
    ("UserInteraction", "confirm", {}),
    ("UserInteraction", "select", {"options": ["a", "b"]}),
    ("UserInteraction", "prompt", {}),
    ("UserInteraction", "questionnaire", {"questions": [{"id": "q", "type": "open", "question": "private question"}]}),
    ("OpenBearControl", "confirm", {"_requiresAuthorization": True}),
])
async def test_real_pending_interactions_survive_read_receipt_without_browser_or_tool_operation(web_env, source, action, extra):
    env = web_env
    await _login_cookie(env)
    row = await env.server._create_web_conversation(123)
    await publish(env, row)
    task, interaction_id = await start_interaction(env, row, {"_sourceTool": source, "action": action, **extra})
    try:
        status = await (await env.client.get("/api/conversation-tree/status")).json()
        item = status["activityItems"][0]
        assert item["activityState"] == "waiting" and item["running"]
        assert item["activityPending"][0]["action"] == action
        assert item["activityPending"][0]["sourceTool"] == source
        assert item["activityPending"][0]["interactionId"] == interaction_id
        assert set(item["activityPending"][0]) == {"action", "sourceTool", "interactionId", "expiresAtMs"}
        assert "private question" not in json.dumps(status)
        assert (await read(env, row, 1)).status == 200
        status = await (await env.client.get("/api/conversation-tree/status")).json()
        assert status["activityItems"][0]["activityState"] == "waiting"
        assert not status["activityItems"][0]["activityUnread"]
        assert (await env.server.interactions.get(interaction_id))["status"] == "pending"
    finally:
        await env.server.interactions.terminate(interaction_id)
        result = await task
        assert result["cancelled"] is True and result["status"] == "cancelled"
        if action == "confirm":
            assert result["confirmed"] is False
    assert (await env.server._tree_running_state(123))["activityItems"] == []


async def test_pending_request_order_scope_and_resolution_broadcast(web_env):
    env = web_env
    row = await env.server._create_web_conversation(123)
    other = await env.server._create_web_conversation(999)
    tasks = []
    try:
        tasks.append(await start_interaction(env, row, {"action": "prompt", "sensitive": True, "timeoutSeconds": 90}))
        tasks.append(await start_interaction(env, row, {"action": "confirm", "timeoutSeconds": 60}))
        tasks.append(await start_interaction(env, other, {"action": "prompt"}))
        hub = GlobalRealtime(env.server)
        queue = asyncio.Queue()
        await hub.snapshot(queue, 123, False)
        packet = await queue.get()
        assert len(packet["treeStatus"]["activityItems"]) == 1
        assert [item["interactionId"] for item in packet["treeStatus"]["activityItems"][0]["activityPending"]] == [tasks[1][1], tasks[0][1]]
        # Resolving on another transport updates the same persistent request.
        result = await env.server.interactions.submit(tasks[1][1], 123, {"cancelled": True}, source="telegram")
        assert result["ok"]
        hub.last_calibration = 0
        await hub.refresh()
        packet = await queue.get()
        assert len(packet["treeStatus"]["activityItems"][0]["activityPending"]) == 1
        await env.server.interactions.terminate(tasks[0][1], "timeout")
        hub.last_calibration = 0
        await hub.refresh()
        assert (await queue.get())["treeStatus"]["activityItems"] == []
        await hub.close()
    finally:
        for task, interaction_id in tasks:
            await env.server.interactions.terminate(interaction_id)
            await task


async def test_existing_schema_gets_zero_watermarks_without_history_backfill(tmp_path):
    path = tmp_path / "old.db"
    # A genuine previous schema; adding columns never marks the historical row unread.
    from pathlib import Path
    schema = (Path(__file__).parents[1] / "app/db/schema.sql").read_text()
    schema = "\n".join(line for line in schema.splitlines() if not line.strip().startswith(("activity_version ", "activity_read_version ", "activity_result_json ")))
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title) VALUES ('old',123,-1,'old title')")
    for _ in range(2):
        db = DB(str(path))
        await db.connect()
        try:
            cur = await db.conn.execute("SELECT * FROM web_conversations WHERE conversation_uuid='old'")
            row = dict(await cur.fetchone())
            assert not activity_fields(row)["activityUnread"] and row["title"] == "old title"
            assert json.loads(row["activity_result_json"]) == {}
        finally:
            await db.close()
