"""Pending interactions invalidate metadata, not every streamed progress update."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.web_console import realtime
from app.web_console.realtime import GlobalRealtime
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as shared_web_env

web_env = shared_web_env


async def isolated_hub(env, monkeypatch):
    await env.server.global_realtime.close()
    hub = GlobalRealtime(env.server)
    env.server.global_realtime = hub
    hub.version = AsyncMock(return_value={})
    clock = [100.0]
    monkeypatch.setattr(realtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    queue = asyncio.Queue()
    await hub.snapshot(queue, 123, False)
    queue.get_nowait()
    await hub.refresh()
    while not queue.empty():
        queue.get_nowait()
    return hub, queue, clock


def activity(hub, queue, uuid):
    return next(item for item in hub.clients[queue]["status"]["activityItems"]
                if item["conversationUuid"] == uuid)


@pytest.mark.parametrize("kind", ["chat-content", "mem", "doc", "secret"])
async def test_unrelated_catalog_changes_do_not_postpone_calibration(web_env, monkeypatch, kind):
    hub, queue, clock = await isolated_hub(web_env, monkeypatch)
    scan = AsyncMock(wraps=web_env.server._tree_running_state)
    monkeypatch.setattr(web_env.server, "_tree_running_state", scan)
    for at in [100.5, 101.0, 101.5, 102.0, 102.5]:
        clock[0] = at
        async with web_env.db.write_transaction(label="test_catalog_change") as conn:
            await conn.execute(
                "INSERT INTO web_catalog_changes(kind,entity_id,owner_chat_id) VALUES(?,?,?)",
                (kind, "test-entity", 123),
            )
        await hub.refresh()
    assert scan.await_count == 1  # One scan at two seconds, not per catalog patch.
    assert hub.last_calibration == 102.0


@pytest.mark.parametrize("outcome", ["answered", "cancelled", "timeout", "interrupted"])
@pytest.mark.parametrize("remaining", ["none", "interaction", "paused", "agent"])
async def test_interaction_terminal_refreshes_without_erasing_other_attention(
    web_env, monkeypatch, outcome, remaining,
):
    env = web_env
    row = await env.server._create_web_conversation(123)
    uuid = row["conversation_uuid"]
    # Keep a real active controller fact so stale-operation recovery cannot mask
    # either the interaction lifecycle or the other waiting reasons under test.
    env.server._web_starting_turns[uuid] = "test-controller"
    waiters = []

    async def request(call, timeout=10):
        task = asyncio.create_task(env.server.interactions.request(
            {"action": "prompt", "title": "Test", "timeoutSeconds": timeout},
            owner_chat_id=123, conversation_uuid=uuid, turn_uuid="test-turn", tool_call_id=call,
        ))
        waiters.append(task)
        for _ in range(200):
            items = env.server.interactions.pending_for(uuid)
            found = next((item for item in items if item["toolCallId"] == call), None)
            if found:
                return found
            await asyncio.sleep(0.005)
        raise AssertionError("interaction did not become pending")

    async def event(kind, **extra):
        return await env.server._publish_web_operation_event({
            "type": kind, "conversationUuid": uuid, "turnUuid": "test-turn",
            "name": "UserInteraction", "toolCallId": "test-call", **extra,
        })

    try:
        await event("tool_start")
        item = await request("test-call", 0.3 if outcome == "timeout" else 10)
        if remaining == "interaction":
            other = await request("other-call")
        elif remaining in {"paused", "agent"}:
            await env.server._publish_operation(
                uuid, internal_chat_id=row["internal_chat_id"], owner_chat_id=123,
                op_id="other:waiting", op_type="run" if remaining == "paused" else "agent",
                action="pause" if remaining == "paused" else "patch",
                lifecycle="paused" if remaining == "paused" else "waiting_control",
                status="paused" if remaining == "paused" else "needs_openbear_control",
                payload={"taskUuid": "test-agent"} if remaining == "agent" else {},
            )
        await env.server._publish_operation(
            uuid, internal_chat_id=row["internal_chat_id"], owner_chat_id=123,
            op_id="run:completed", op_type="run", action="end", status="completed",
        )
        hub, queue, clock = await isolated_hub(env, monkeypatch)
        assert activity(hub, queue, uuid)["activityState"] == "waiting"
        clock[0] = 100.1  # The periodic calibration is deliberately not yet due.
        if outcome == "answered":
            assert (await env.server.interactions.submit(item["interactionId"], 123, {"value": "done"}))["ok"]
        elif outcome == "timeout":
            assert (await asyncio.wait_for(waiters[0], 1))["status"] == "timeout"
        else:
            await env.server.interactions.terminate(item["interactionId"], outcome)
        result = await waiters[0]
        await hub.refresh()
        current = activity(hub, queue, uuid)
        assert item["interactionId"] not in [p["interactionId"] for p in current["activityPending"]]
        # Until its tool result commits, the active operation is still a valid
        # waiting fact. Completion must trigger another refresh, not clear blindly.
        assert current["activityState"] == "waiting"
        while not queue.empty():
            queue.get_nowait()
        hub.wake.clear()
        await event("tool_result", result=json.dumps(result))
        await hub.refresh()
        current = activity(hub, queue, uuid)
        assert current["running"] and current["activityUnread"]
        assert current["activityState"] == ("running" if remaining == "none" else "waiting")
        assert len(current["activityPending"]) == (1 if remaining == "interaction" else 0)
        if remaining == "interaction":
            assert current["activityPending"][0]["interactionId"] == other["interactionId"]
        assert not queue.empty() or remaining != "none"
    finally:
        await env.server.interactions.cancel_conversation(uuid)
        await asyncio.gather(*waiters, return_exceptions=True)
        env.server._web_starting_turns.pop(uuid, None)


async def test_streamed_progress_does_not_invalidate_tree(web_env, monkeypatch):
    env = web_env
    row = await env.server._create_web_conversation(123)
    uuid = row["conversation_uuid"]
    env.server._web_starting_turns[uuid] = "test-controller"
    try:
        await env.server._publish_web_operation_event({
            "type": "delta", "conversationUuid": uuid, "turnUuid": "test-turn", "text": "start",
        })
        hub, queue, clock = await isolated_hub(env, monkeypatch)
        scan = AsyncMock(wraps=env.server._tree_running_state)
        monkeypatch.setattr(env.server, "_tree_running_state", scan)
        clock[0] = 100.1
        for index in range(20):
            await env.server._publish_web_operation_event({
                "type": "delta", "conversationUuid": uuid, "turnUuid": "test-turn",
                "text": f"start {index}",
            })
            await hub.refresh()
        scan.assert_not_awaited()
    finally:
        env.server._web_starting_turns.pop(uuid, None)


async def test_interaction_boundaries_reach_live_global_socket_before_calibration(web_env):
    env = web_env
    row = await env.server._create_web_conversation(123)
    uuid = row["conversation_uuid"]
    env.server._web_starting_turns[uuid] = "test-controller"
    hub = env.server.global_realtime
    cookie = await _login_cookie(env)
    ws = await env.client.ws_connect(
        "/api/events/ws", headers={"Cookie": f"openbear_web_session={cookie}"},
    )
    task = None

    async def receive_status(predicate):
        # Less than the two-second fallback: exercise the actual wake/debounce
        # loop and socket, not just manual refresh calls.
        async with asyncio.timeout(1):
            while True:
                packet = await ws.receive_json()
                for item in packet.get("treeStatus", {}).get("activityItems", []):
                    if item["conversationUuid"] == uuid and predicate(item):
                        return item

    try:
        assert (await ws.receive_json(timeout=1))["type"] == "snapshot"
        await hub.refresh()
        await env.server._publish_web_operation_event({
            "type": "tool_start", "conversationUuid": uuid, "turnUuid": "test-turn",
            "name": "UserInteraction", "toolCallId": "socket-call",
        })
        task = asyncio.create_task(env.server.interactions.request(
            {"action": "prompt", "title": "Test", "timeoutSeconds": 10},
            owner_chat_id=123, conversation_uuid=uuid, turn_uuid="test-turn", tool_call_id="socket-call",
        ))
        pending = await receive_status(lambda item: bool(item["activityPending"]))
        cid = pending["activityPending"][0]["interactionId"]
        assert (await env.server.interactions.submit(cid, 123, {"value": "done"}))["ok"]
        result = await task
        # Deliberately let the ledger-only refresh reach the browser before the
        # tool result. The second invalidation must not be lost in this race.
        ledger_only = await receive_status(lambda item: not item["activityPending"])
        assert ledger_only["activityState"] == "waiting"
        await env.server._publish_web_operation_event({
            "type": "tool_result", "conversationUuid": uuid, "turnUuid": "test-turn",
            "name": "UserInteraction", "toolCallId": "socket-call", "result": json.dumps(result),
        })
        await receive_status(lambda item: item["activityState"] == "running")
        # Explicit resync/reconnect bootstrap still uses authoritative state.
        await ws.send_json({"type": "resync"})
        while True:
            packet = await ws.receive_json(timeout=1)
            if packet["type"] == "snapshot":
                break
        current = next(item for item in packet["treeStatus"]["activityItems"]
                       if item["conversationUuid"] == uuid)
        assert current["activityState"] == "running" and not current["activityPending"]
    finally:
        await ws.close()
        await env.server.interactions.cancel_conversation(uuid)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        env.server._web_starting_turns.pop(uuid, None)


async def test_invalidation_during_tree_scan_survives_next_refresh(web_env, monkeypatch):
    hub, queue, clock = await isolated_hub(web_env, monkeypatch)
    original = web_env.server._tree_running_state

    async def invalidate_during_scan(owner):
        hub.invalidate_tree()
        return await original(owner)

    scan = AsyncMock(side_effect=invalidate_during_scan)
    monkeypatch.setattr(web_env.server, "_tree_running_state", scan)
    hub.invalidate_tree()
    await hub.refresh()
    assert hub.last_calibration == 0.0
    scan.side_effect = original
    await hub.refresh()
    assert scan.await_count == 2
