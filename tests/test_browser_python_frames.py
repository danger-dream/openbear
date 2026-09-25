"""Frame/session/navigation races with protocol events, no real browser."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.browser.pyworker.common import BrowserError, ProtocolError
from app.browser.pyworker.frames import Frame
from app.browser.pyworker.page import Page
from tests import test_browser_python_engine

engine = test_browser_python_engine.engine


def child(page, name="child", session="child-session", parent=None):
    frame = Frame(page, name, parent or page.main_id, session)
    frame.context, frame.helper = 7, "helper-" + name
    page.frames[name] = frame
    page.session_roots[session] = name
    return frame


async def test_main_attach_preserves_viewport_language_and_timezone(engine):
    obj, page = engine
    obj.config["mode"] = "main"
    obj.cdp.call.return_value = {"sessionId": "attached"}
    page.setup_session = AsyncMock()
    await page.bind()
    assert [c.args[0] for c in obj.cdp.call.await_args_list] == ["Target.attachToTarget"]
    page.setup_session.assert_awaited_once_with("attached", page.target, main=True)


def test_iframe_swap_invalidates_refs_but_late_old_detach_does_not_erase_new_session(engine):
    obj, page = engine
    frame = child(page)
    page.snapshot = "s1"
    page.refs = {"e1": (frame.id, frame.epoch)}
    epoch = frame.epoch
    page.event("Page.frameDetached", {"frameId": frame.id, "reason": "swap"}, page.session)
    assert not frame.detached and frame.epoch > epoch and not page.refs
    page.event(
        "Page.frameNavigated",
        {
            "frame": {
                "id": frame.id,
                "parentId": page.main_id,
                "loaderId": "new",
                "url": "https://other.test",
            }
        },
        "new-session",
    )
    frame.helper, frame.context = "new-helper", 8
    page.event("Target.detachedFromTarget", {"sessionId": "child-session"}, page.session)
    assert frame.session == "new-session" and frame.helper == "new-helper"
    assert page.main.epoch == 0


def test_removing_iframe_invalidates_entire_descendant_subtree(engine):
    _, page = engine
    parent = child(page)
    nested = child(page, "nested", "nested-session", parent.id)
    page.event("Page.frameDetached", {"frameId": parent.id, "reason": "remove"}, page.session)
    assert parent.detached and nested.detached
    assert parent.helper is None and nested.helper is None


def test_context_destroyed_is_scoped_to_originating_session(engine):
    _, page = engine
    frame = child(page)
    page.main.context, page.main.helper = 7, "main-helper"
    page.event("Runtime.executionContextDestroyed", {"executionContextId": 7}, frame.session)
    assert frame.context is None and frame.helper is None
    assert page.main.context == 7 and page.main.helper == "main-helper"


def test_same_document_navigation_keeps_valid_ref_and_finishes_pending_navigation(engine):
    _, page = engine
    page.snapshot = "s1"
    page.refs = {"e1": (page.main_id, 0)}
    page.pending_navigation = True
    page.event(
        "Page.navigatedWithinDocument",
        {"frameId": page.main_id, "url": "https://test.invalid/#next"},
        page.session,
    )
    assert page.url.endswith("#next") and not page.pending_navigation
    assert page.snapshot == "s1" and "e1" in page.refs


async def test_concurrent_helper_initialization_is_single_and_document_change_rejects_stale_world(
    engine,
):
    obj, page = engine
    frame = page.main
    entered = asyncio.Event()
    resume = asyncio.Event()
    methods = []

    async def call(method, *args, **kwargs):
        methods.append(method)
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        entered.set()
        await resume.wait()
        return {"result": {"objectId": "helper"}}

    obj.cdp.call = call
    first = asyncio.create_task(frame.ready())
    second = asyncio.create_task(frame.ready())
    await entered.wait()
    resume.set()
    await asyncio.gather(first, second)
    assert methods == ["Page.createIsolatedWorld", "Runtime.evaluate"]
    frame.invalidate()
    entered.clear()
    resume.clear()
    methods.clear()
    pending = asyncio.create_task(frame.ready())
    await entered.wait()
    frame.invalidate()
    resume.set()
    with pytest.raises(ProtocolError, match="document_changed"):
        await pending
    assert frame.helper is None and frame.context is None


async def test_partial_attach_failure_detaches_control_session_without_closing_browser(engine):
    obj, _ = engine
    methods = []

    async def call(method, params=None, session=None, **kwargs):
        methods.append((method, params))
        if method == "Target.attachToTarget":
            return {"sessionId": "partial-session"}
        if method == "Page.enable":
            raise ProtocolError("initialization failure")
        return {}

    obj.cdp.call = call
    with pytest.raises(ProtocolError):
        await obj.page("new-target")
    assert "new-target" not in obj.pages and "partial-session" not in obj.sessions
    assert ("Target.detachFromTarget", {"sessionId": "partial-session"}) in methods
    assert not any(m in {"Browser.close", "Target.closeTarget"} for m, _ in methods)


async def test_target_destruction_cleans_owned_session_maps_and_caches(engine):
    obj, page = engine
    page.session_roots[page.session] = page.main_id
    page.observer_sessions[page.session] = "observer"
    obj.sessions.update({page.session: page, "observer": page, "other": object()})
    obj.page_locks[page.target] = asyncio.Lock()
    page.close = AsyncMock()
    obj.on_event("Target.targetDestroyed", {"targetId": page.target}, None)
    if obj.tasks:
        await asyncio.gather(*list(obj.tasks))
    assert page.closed and page.target not in obj.pages
    assert set(obj.sessions) == {"other"} and page.target not in obj.page_locks
    page.close.assert_awaited_once()


async def test_navigation_waits_for_matching_loader_and_uses_its_status(engine):
    obj, page = engine
    sent = asyncio.Event()

    async def call(method, params=None, session=None, **kwargs):
        assert method == "Page.navigate"
        page.event(
            "Page.frameNavigated",
            {"frame": {"id": page.main_id, "loaderId": "new", "url": params["url"]}},
            page.session,
        )
        page.navigation_status[(page.main_id, "new")] = 201
        sent.set()
        return {"frameId": page.main_id, "loaderId": "new"}

    obj.cdp.call = call
    task = asyncio.create_task(
        obj.execute(
            "navigate", {"targetId": page.target, "url": "https://next.test", "timeoutMs": 500}
        )
    )
    await sent.wait()
    page.event(
        "Page.lifecycleEvent",
        {"frameId": page.main_id, "loaderId": "old", "name": "DOMContentLoaded"},
        page.session,
    )
    assert not task.done()
    page.event(
        "Page.lifecycleEvent",
        {"frameId": page.main_id, "loaderId": "new", "name": "DOMContentLoaded"},
        page.session,
    )
    result = await task
    assert result == {"url": "https://next.test", "statusCode": 201}


async def test_failed_navigation_is_reported_once_and_download_does_not_wait_for_dom(engine):
    obj, page = engine
    obj.cdp.call.return_value = {"errorText": "net::ERR_NAME_NOT_RESOLVED"}
    with pytest.raises(BrowserError, match="navigation_failed") as error:
        await obj.execute("navigate", {"targetId": page.target, "url": "https://invalid.test"})
    assert error.value.outcome == "failed" and obj.cdp.call.await_count == 1
    assert error.value.details["navigation"]["networkError"] == "net::ERR_NAME_NOT_RESOLVED"
    obj.cdp.call.return_value = {
        "isDownload": True,
        "frameId": page.main_id,
        "loaderId": "download",
    }
    await asyncio.wait_for(
        obj.execute("navigate", {"targetId": page.target, "url": "https://file.test"}), 0.2
    )
    assert obj.cdp.call.await_count == 2


async def test_page_close_cancels_inflight_child_setup_before_detaching_sessions(engine):
    obj, page = engine
    entered = asyncio.Event()

    async def setup():
        entered.set()
        await asyncio.Event().wait()

    task = obj.spawn(setup())
    page.setup_tasks["child-session"] = task
    await entered.wait()
    await page.close()
    assert task.cancelled()
