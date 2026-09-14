"""Metadata shutdown owns its task through wake/cancel races and repeated close.

Unit tests use an in-memory listener set, not a database. The socket test uses
pytest's temporary SQLite/aiohttp fixture. No model or external API is invoked.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import WSMsgType

from app.web_console.realtime import GlobalRealtime
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as shared_web_env

web_env = shared_web_env


class ObservedEvent(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.returned = asyncio.Event()
        self.waiter = None

    async def wait(self):
        self.waiter = asyncio.current_task()
        self.entered.set()
        result = await super().wait()
        self.returned.set()
        return result


def make_hub():
    listeners = set()
    hub = GlobalRealtime(SimpleNamespace(db=SimpleNamespace(
        conn=SimpleNamespace(commit_listeners=listeners),
    )))
    hub.wake = ObservedEvent()
    return hub, listeners


def add_clients(hub, count):
    queues = [asyncio.Queue(maxsize=2) for _ in range(count)]
    for queue in queues:
        queue.put_nowait({"type": "patch", "seq": 1})
        queue.put_nowait({"type": "patch", "seq": 2})
        hub.clients[queue] = {}
    return queues


async def completed(*tasks):
    # Unlike wait_for, this deadline cannot cancel the task under test and
    # accidentally repair the very cancellation bug being asserted against.
    done, pending = await asyncio.wait(tasks, timeout=1)
    assert not pending, f"close did not finish: {pending}"
    for task in done:
        assert task.result() is None


async def dispose_failed_test_tasks(before):
    # Assertions run BEFORE this failure-only disposal. An old broken close
    # must fail promptly rather than hang pytest's own loop finalizer.
    remaining = asyncio.all_tasks() - before
    if remaining:
        for task in remaining:
            task.cancel()
        _, pending = await asyncio.wait(remaining, timeout=1)
        assert not pending, f"test disposal left tasks: {pending}"


@pytest.mark.parametrize("subscribers", [0, 2])
@pytest.mark.parametrize("closers", [1, 3])
async def test_close_finishes_when_event_completion_races_cancellation(subscribers, closers):
    before = asyncio.all_tasks()
    hub, listeners = make_hub()
    def other_listener():
        pass

    listeners.add(other_listener)
    queues = add_clients(hub, subscribers)
    hub.refresh = AsyncMock()
    hub.cached_version = {"version": "retained"}
    hub.serial, hub.last_cursor, hub.last_calibration, hub.version_at = 7, 9, 11.0, 13.0
    try:
        await hub.start()
        await hub.wake.entered.wait()
        child = hub.wake.waiter
        hub.wake.set()
        await asyncio.sleep(0)
        # Event.wait has returned in both implementations. Python 3.11 runs
        # it in a child task; 3.12+ awaits it inline in the owned hub task.
        # Keep the 3.11 cancellation-swallowing race assertion without
        # requiring the inline 3.12+ hub task to have already finished.
        assert hub.wake.returned.is_set()
        if child is not hub.task:
            assert child.done() and not child.cancelled()
        assert not hub.task.done()
        closing = [asyncio.create_task(hub.close()) for _ in range(closers)]
        await completed(*closing)
        assert hub.task.done() and child.done()
        # A swallowed cancellation may finish normally; cancelled() is NOT
        # the criterion. It must stop without refreshing or leaving any task.
        hub.refresh.assert_not_awaited()
        assert not (asyncio.all_tasks() - before)
        assert listeners == {other_listener}
        assert not hub.clients
        for queue in queues:
            assert queue.get_nowait() == {"type": "reconnect"}
            assert queue.empty()
        await completed(asyncio.create_task(hub.close()))
        assert all(queue.empty() for queue in queues)
        assert hub.cached_version == {"version": "retained"}
        assert (hub.serial, hub.last_cursor, hub.last_calibration, hub.version_at) == (7, 9, 11.0, 13.0)
    finally:
        await dispose_failed_test_tasks(before)


@pytest.mark.parametrize("phase", ["event_wait", "debounce", "refresh"])
async def test_close_interrupts_each_background_wait_phase(phase):
    before = asyncio.all_tasks()
    hub, listeners = make_hub()
    refreshing, cleaned = asyncio.Event(), asyncio.Event()

    async def refresh():
        refreshing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    hub.refresh = refresh
    try:
        await hub.start()
        await hub.wake.entered.wait()
        if phase != "event_wait":
            hub.wake.set()
        if phase == "refresh":
            async with asyncio.timeout(1):
                await refreshing.wait()
        elif phase == "debounce":
            async with asyncio.timeout(1):
                while True:
                    awaited = hub.task.get_coro().cr_await
                    frame = getattr(awaited, "cr_frame", None)
                    if frame and frame.f_code.co_name == "sleep":
                        break
                    await asyncio.sleep(0)
        await completed(asyncio.create_task(hub.close()))
        assert hub.task.done() and not listeners
        assert cleaned.is_set() == (phase == "refresh")
        assert not (asyncio.all_tasks() - before)
    finally:
        await dispose_failed_test_tasks(before)


async def test_concurrent_close_waits_for_refresh_cleanup_without_recancelling():
    before = asyncio.all_tasks()
    hub, listeners = make_hub()
    queue = add_clients(hub, 1)[0]
    refreshing, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def refresh():
        refreshing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    hub.refresh = refresh
    try:
        await hub.start()
        hub.wake.set()
        async with asyncio.timeout(1):
            await refreshing.wait()
        first = asyncio.create_task(hub.close())
        async with asyncio.timeout(1):
            await cleaning.wait()
        second = asyncio.create_task(hub.close())
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert hub.task.cancelling() == 1
        assert not listeners and queue.qsize() == 2
        release.set()
        await completed(first, second)
        assert cleaned.is_set() and hub.task.done()
        assert queue.get_nowait() == {"type": "reconnect"} and queue.empty()
        assert not (asyncio.all_tasks() - before)
    finally:
        release.set()
        await dispose_failed_test_tasks(before)


async def test_close_before_start_and_restart_keep_one_owned_task():
    before = asyncio.all_tasks()
    hub, listeners = make_hub()
    try:
        await completed(asyncio.create_task(hub.close()))
        for _ in range(2):
            hub.wake.entered.clear()
            await hub.start()
            task = hub.task
            await hub.start()
            assert hub.task is task
            async with asyncio.timeout(1):
                await hub.wake.entered.wait()
            assert listeners == {hub.wake.set}
            await completed(asyncio.create_task(hub.close()))
            assert task.done() and not listeners
            assert not (asyncio.all_tasks() - before)
    finally:
        await dispose_failed_test_tasks(before)


async def test_real_subscriber_receives_reconnect_and_socket_closes(web_env):
    env = web_env
    cookie = await _login_cookie(env)
    ws = await env.client.ws_connect(
        "/api/events/ws", headers={"Cookie": f"openbear_web_session={cookie}"},
    )
    hub = env.server.global_realtime
    try:
        assert (await ws.receive_json(timeout=2))["type"] == "snapshot"
        await completed(asyncio.create_task(hub.close()))
        assert hub.task.done() and not hub.clients
        assert hub.wake.set not in env.db.conn.commit_listeners
        assert (await ws.receive_json(timeout=2))["type"] == "reconnect"
        assert (await ws.receive(timeout=2)).type == WSMsgType.CLOSE
        assert ws.close_code == 1012
        await completed(asyncio.create_task(hub.close()))
    finally:
        await ws.close()
