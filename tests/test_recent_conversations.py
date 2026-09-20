"""Recent interaction navigation uses durable operations, not metadata activity."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.runs import RunRegistry
from app.web_console import operation_store, realtime
from tests.test_web_admin import _login_cookie, web_env

BASE = 1_600_000_000_000  # Old interactions have no navigation expiry.


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(ms=BASE)
    monkeypatch.setattr(operation_store, "time", SimpleNamespace(time=lambda: value.ms / 1000))
    return value


async def api(env, cookie, method, path, **kwargs):
    response = await env.client.request(method, path, headers={"Cookie": f"openbear_web_session={cookie}"}, **kwargs)
    assert response.status == 200, await response.text()
    return await response.json()


async def status(env, cookie):
    return await api(env, cookie, "GET", "/api/conversation-tree/status")


def recent_ids(state):
    return [item["conversationUuid"] for item in state["recentItems"]]


async def publish(env, row, clock, typ, **data):
    return await env.server._live_for(row).publish({"type": typ, "ts": clock.ms, **data})


async def completed_exchange(env, row, clock, ms):
    turn = f"turn-{row['conversation_uuid']}-{ms}"
    clock.ms = ms
    await publish(env, row, clock, "accepted", turnUuid=turn)
    await publish(env, row, clock, "user", turnUuid=turn, messageUuid=turn, text="PRIVATE USER BODY")
    clock.ms += 10
    await publish(env, row, clock, "final", text="PRIVATE ASSISTANT BODY")
    await publish(env, row, clock, "done")


async def test_recent_top_five_owner_folders_read_archive_delete_and_ties(web_env, clock):
    env = web_env
    cookie = await _login_cookie(env)
    first_folder = (await api(env, cookie, "POST", "/api/conversation-folders", json={"name": "Project"}))["folder"]["folderId"]
    leaf_folder = (await api(env, cookie, "POST", "/api/conversation-folders", json={"name": "Nested", "parentId": first_folder}))["folder"]["folderId"]
    rows = []
    times = {}
    for index in range(6):
        row = await env.server._create_web_conversation(123, title=f"Recent {index}", conversation_uuid=f"recent-{index}", folder_uuid=leaf_folder if index % 2 else "")
        rows.append(row)
        ms = BASE + min(index, 4) * 100  # The last two tie; UUID is the stable tie-break.
        await completed_exchange(env, row, clock, ms)
        times[row["conversation_uuid"]] = ms + 10

    empty = await env.server._create_web_conversation(123, title="Unsent empty conversation")
    clock.ms = BASE + 9000
    await publish(env, empty, clock, "user", turnUuid="blank", messageUuid="blank", text="   ")
    for kind in ("hidden", "internal", "foreign"):
        row = await env.server._create_web_conversation(456 if kind == "foreign" else 123, title=kind)
        if kind == "foreign":
            await completed_exchange(env, row, clock, BASE + 10000)
            continue
        # Internal inputs use native operation specs, not the visible user's
        # event mapper (which deliberately constructs public user messages).
        clock.ms = BASE + 10000
        for op_type in ("user_message", "assistant_message"):
            await env.server._publish_operation(
                row["conversation_uuid"], internal_chat_id=row["internal_chat_id"], owner_chat_id=123,
                op_id=f"{kind}:{op_type}", op_type=op_type,
                action="create" if op_type == "user_message" else "end",
                turn_uuid=f"{kind}-turn", status="completed", internal=kind == "internal",
                payload={"text": "PRIVATE HIDDEN BODY", "complete": True, "hidden": kind == "hidden"},
            )

    expected = ["recent-4", "recent-5", "recent-3", "recent-2", "recent-1"]
    state = await status(env, cookie)
    assert recent_ids(state) == expected
    bootstrap = await api(env, cookie, "GET", "/api/conversation-tree/bootstrap")
    assert bootstrap["running"]["recentItems"] == state["recentItems"]
    assert state["items"] == []
    for item in state["recentItems"]:
        assert item["kind"] == "conversation"
        assert item["id"] == item["conversationUuid"]
        assert item["archived"] is False and item["running"] is False
        assert item["lastInteractionAtMs"] == times[item["id"]]
        assert item["folderId"] == item["parentId"]
        assert item["path"] == ("Project / Nested" if item["folderId"] else "临时会话")
        assert "activityUnread" in item and "currentStatus" in item
    assert "PRIVATE USER BODY" not in str(state) and "PRIVATE ASSISTANT BODY" not in str(state)

    # Reading completion only clears the separate activity inbox.
    receipts = [{"conversationUuid": item["id"], "version": item["activityVersion"]} for item in state["recentItems"]]
    await api(env, cookie, "POST", "/api/conversation-tree/read", json={"items": receipts})
    read_state = await status(env, cookie)
    assert recent_ids(read_state) == expected
    assert all(not item["activityUnread"] for item in read_state["recentItems"])
    assert not ({item["id"] for item in read_state["activityItems"]} & set(expected))
    assert read_state["folderConversationCounts"] == state["folderConversationCounts"]
    assert read_state["folderRunningCounts"] == state["folderRunningCounts"]

    await api(env, cookie, "PATCH", "/api/conversations/recent-4", json={"archived": True})
    assert recent_ids(await status(env, cookie)) == ["recent-5", "recent-3", "recent-2", "recent-1", "recent-0"]
    await api(env, cookie, "PATCH", "/api/conversations/recent-4", json={"archived": False})
    assert recent_ids(await status(env, cookie)) == expected
    await api(env, cookie, "DELETE", "/api/conversations/recent-4")
    assert recent_ids(await status(env, cookie)) == ["recent-5", "recent-3", "recent-2", "recent-1", "recent-0"]


async def test_recent_ignores_metadata_tools_streaming_and_uses_user_and_reply_boundaries(web_env, clock):
    env = web_env
    cookie = await _login_cookie(env)
    older = await env.server._create_web_conversation(123, title="Older", conversation_uuid="older")
    newer = await env.server._create_web_conversation(123, title="Newer", conversation_uuid="newer")
    await completed_exchange(env, older, clock, BASE + 100)
    await completed_exchange(env, newer, clock, BASE + 200)
    original = {i["id"]: i["lastInteractionAtMs"] for i in (await status(env, cookie))["recentItems"]}

    folder = (await api(env, cookie, "POST", "/api/conversation-folders", json={"name": "Moved"}))["folder"]["folderId"]
    await api(env, cookie, "PATCH", "/api/conversations/older", json={"title": "Renamed", "pinned": True})
    await api(env, cookie, "POST", "/api/conversation-tree/move", json={"kind": "conversation", "id": "older", "targetFolderId": folder, "updateSnapshots": False})
    await api(env, cookie, "GET", "/api/conversations/older/state")  # Opening is not interaction.
    before = await status(env, cookie)
    assert {i["id"]: i["lastInteractionAtMs"] for i in before["recentItems"]} == original
    old_item = next(i for i in before["recentItems"] if i["id"] == "older")
    assert old_item["title"] == "Renamed" and old_item["path"] == "Moved"

    # A real registered execution owns streaming state while HTTP status reads.
    env.server.runs = RunRegistry()
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    env.server.runs.register(int(older["internal_chat_id"]), task)
    try:
        clock.ms = BASE + 300
        await publish(env, older, clock, "accepted", turnUuid="stream-turn")
        await publish(env, older, clock, "tool_start", toolCallId="tool-1", name="Read", arguments="{}")
        await publish(env, older, clock, "tool_result", toolCallId="tool-1", name="Read", result="tool body")
        await publish(env, older, clock, "stats", stats={"live": True, "modelCalls": 1})
        for ms, text in ((400, "partial"), (500, "partial more")):
            clock.ms = BASE + ms
            await publish(env, older, clock, "delta", text=text)
            current = await status(env, cookie)
            assert recent_ids(current) == ["newer", "older"]
            assert {i["id"]: i["lastInteractionAtMs"] for i in current["recentItems"]} == original
            assert next(i for i in current["recentItems"] if i["id"] == "older")["running"] is True
        clock.ms = BASE + 600
        await publish(env, older, clock, "final", text="Finished visible reply")
        completed = await status(env, cookie)
        assert recent_ids(completed) == ["older", "newer"]
        assert completed["recentItems"][0]["lastInteractionAtMs"] == BASE + 600
        await publish(env, older, clock, "done")
    finally:
        gate.set()
        await task

    # A queued user original counts at receipt, not when later injected.
    clock.ms = BASE + 700
    await publish(env, newer, clock, "accepted", turnUuid="queued-turn")
    await publish(env, newer, clock, "queued", turnUuid="queued-turn", messageUuid="queued-input", text="User interruption")
    assert recent_ids(await status(env, cookie))[0] == "newer"
    await completed_exchange(env, older, clock, BASE + 750)
    clock.ms = BASE + 800
    await publish(env, newer, clock, "user", turnUuid="queued-turn", messageUuid="queued-input", text="User interruption", steeringInjected=True)
    injected = await status(env, cookie)
    assert recent_ids(injected) == ["older", "newer"]
    assert injected["recentItems"][1]["lastInteractionAtMs"] == BASE + 700
    clock.ms = BASE + 900
    await publish(env, newer, clock, "error", error="Upstream failure")
    await publish(env, older, clock, "stopped", reason="User stop", turnUuid="stop-turn")
    settled = await status(env, cookie)
    assert recent_ids(settled) == ["older", "newer"]
    assert [i["lastInteractionAtMs"] for i in settled["recentItems"]] == [BASE + 760, BASE + 700]
    clock.ms = BASE + 1000
    await publish(env, newer, clock, "user", turnUuid="next-user", messageUuid="next-user", text="A new user send")
    latest = await status(env, cookie)
    assert recent_ids(latest) == ["newer", "older"]
    assert latest["recentItems"][0]["lastInteractionAtMs"] == BASE + 1000


async def test_recent_calibration_pushes_user_and_reply_and_reconnect_matches_http(web_env, clock):
    env = web_env
    cookie = await _login_cookie(env)
    row = await env.server._create_web_conversation(123, title="Realtime interaction")
    headers = {"Cookie": f"openbear_web_session={cookie}"}
    ws = await env.client.ws_connect("/api/events/ws", headers=headers)
    try:
        first = await ws.receive_json(timeout=2)
        assert first["type"] == "snapshot" and first["treeStatus"]["recentItems"] == []
        assert first["conversationContentLimit"] == 20000
        catalog_item = next(i for i in first["items"] if i["key"] == f"chat:{row['conversation_uuid']}")
        assert "bodyChars" in catalog_item and "recentTurnChars" in catalog_item
        hub = env.server.global_realtime
        await hub.refresh()  # Establish unchanged cursor and fresh calibration.
        cur = await env.db.conn.execute("SELECT COALESCE(MAX(seq),0) FROM web_catalog_changes")
        cursor = (await cur.fetchone())[0]
        clock.ms = BASE + 2000
        await publish(env, row, clock, "user", turnUuid="realtime-user", messageUuid="realtime-user", text="private realtime input")
        cur = await env.db.conn.execute("SELECT COALESCE(MAX(seq),0) FROM web_catalog_changes")
        assert (await cur.fetchone())[0] == cursor
        # Keep the existing ~2s tree calibration (plus scheduling/debounce),
        # rather than requesting a full tree aggregation for every commit.
        async with asyncio.timeout(5):
            while True:
                patch = await ws.receive_json()
                if patch.get("treeStatus", {}).get("recentItems"):
                    break
        assert patch["type"] == "patch"
        assert patch["conversationContentLimit"] == 20000
        assert recent_ids(patch["treeStatus"]) == [row["conversation_uuid"]]
        assert patch["treeStatus"]["recentItems"][0]["lastInteractionAtMs"] == clock.ms
        http_state = await status(env, cookie)
        assert http_state["recentItems"] == patch["treeStatus"]["recentItems"]
        clock.ms = BASE + 3000
        await publish(env, row, clock, "final", text="visible completed reply")
        # Keep the existing ~2s tree calibration (plus scheduling/debounce),
        # rather than requesting a full tree aggregation for every commit.
        async with asyncio.timeout(5):
            while True:
                patch = await ws.receive_json()
                recent = patch.get("treeStatus", {}).get("recentItems") or []
                if recent and recent[0]["lastInteractionAtMs"] == clock.ms:
                    break
        http_state = await status(env, cookie)
        assert http_state["recentItems"] == patch["treeStatus"]["recentItems"]
        second_device = await env.client.ws_connect("/api/events/ws", headers=headers)
        try:
            second_snapshot = await second_device.receive_json(timeout=2)
            assert second_snapshot["treeStatus"]["recentItems"] == http_state["recentItems"]
        finally:
            await second_device.close()
    finally:
        await ws.close()


async def test_unchanged_catalog_before_calibration_skips_tree_but_refreshes_overview(monkeypatch):
    cursor = SimpleNamespace(fetchone=AsyncMock(return_value=(7,)))
    owner = SimpleNamespace(
        db=SimpleNamespace(conn=SimpleNamespace(execute=AsyncMock(return_value=cursor))),
        _tree_running_state=AsyncMock(return_value={"recentItems": []}),
    )
    hub = realtime.GlobalRealtime(owner)
    hub.last_cursor = 7
    hub.last_calibration = 100.0
    monkeypatch.setattr(realtime, "time", SimpleNamespace(monotonic=lambda: 100.5))
    hub.version = AsyncMock(return_value={})
    hub.refresh_overviews = AsyncMock()
    hub.clients[asyncio.Queue()] = {
        "owner": 123, "archive": False, "cursor": 7, "items": {},
        "status": {"recentItems": []}, "version": {}, "seq": 1,
    }
    await hub.refresh()
    await hub.refresh()
    owner._tree_running_state.assert_not_awaited()
    hub.version.assert_not_awaited()
    assert hub.refresh_overviews.await_count == 2
    assert hub.last_calibration == 100.0
