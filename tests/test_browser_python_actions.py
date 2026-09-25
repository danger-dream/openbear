"""Action failure-path regressions; deterministic protocol doubles, no browser."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.browser.pyworker.actions import Actions
from app.browser.pyworker.common import BrowserError, DialogOpened, ProtocolError
from tests import test_browser_python_engine

engine = test_browser_python_engine.engine


def node_for(page, name="node"):
    return SimpleNamespace(frame=page.main, object_id=name, release=AsyncMock())


def pointer_scene(obj, page):
    actions = Actions(page)
    page.session_roots[page.session] = page.main_id
    page.main.viewport = AsyncMock(return_value={"width": 100, "height": 100})
    obj.cdp.call.return_value = {"quads": [[0, 0, 20, 0, 20, 20, 0, 20]]}
    page.after_input = AsyncMock()
    page.input = AsyncMock()
    actions.scroll_node = AsyncMock()
    actions.guards = AsyncMock(return_value=[])
    return actions


async def test_click_re_resolves_replaced_node_and_waits_for_overlay_without_replaying(engine):
    obj, page = engine
    actions = pointer_scene(obj, page)
    nodes = [node_for(page, str(i)) for i in range(3)]
    page.resolve = AsyncMock(side_effect=nodes)
    page.main.run = AsyncMock(side_effect=[ProtocolError("node detached"), None, None])
    actions.hit_target = AsyncMock(side_effect=[False, True])
    await asyncio.wait_for(actions.click({"op": "click", "target": "css=button"}), 0.5)
    assert page.resolve.await_count == 3
    assert [c.args[1]["type"] for c in page.input.await_args_list] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert all(n.release.await_count == 1 for n in nodes)


async def test_fatal_actionability_error_releases_node_and_is_not_retried(engine):
    obj, page = engine
    actions = pointer_scene(obj, page)
    node = node_for(page)
    page.resolve = AsyncMock(return_value=node)
    page.main.run = AsyncMock(side_effect=ProtocolError("unsupported command"))
    with pytest.raises(ProtocolError, match="unsupported"):
        await actions.actionable("css=button", ["enabled"])
    node.release.assert_awaited_once()
    page.resolve.assert_awaited_once()
    page.input.assert_not_awaited()


async def test_disabled_element_cancellation_releases_handle_without_any_input(engine):
    obj, page = engine
    actions = pointer_scene(obj, page)
    node = node_for(page)
    page.resolve = AsyncMock(return_value=node)
    entered = asyncio.Event()

    async def states(*args):
        entered.set()
        await asyncio.Event().wait()

    page.main.run = states
    task = asyncio.create_task(actions.actionable("css=button", ["enabled"]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    node.release.assert_awaited_once()
    page.input.assert_not_awaited()


async def test_post_click_interception_reports_unknown_and_does_not_click_again(engine):
    obj, page = engine
    actions = pointer_scene(obj, page)
    node = node_for(page)
    actions.actionable = AsyncMock(return_value=(node, {"x": 10, "y": 10}))
    guard = SimpleNamespace(call=AsyncMock(return_value="overlay intercepted"), release=AsyncMock())
    actions.guards.return_value = [guard]
    with pytest.raises(BrowserError, match="pointer_intercepted") as error:
        await actions.click({"op": "click", "target": "css=button"})
    assert error.value.outcome == "unknown"
    assert sum(c.args[1]["type"] == "mousePressed" for c in page.input.await_args_list) == 1
    node.release.assert_awaited_once()
    guard.release.assert_awaited_once()


@pytest.mark.parametrize("failure", [ConnectionError, asyncio.CancelledError])
async def test_interrupted_key_chord_releases_key_and_all_modifiers(engine, failure):
    obj, page = engine
    calls = []

    async def input(method, payload):
        calls.append(payload.copy())
        if payload["key"] == "K" and payload["type"] == "rawKeyDown":
            raise failure()

    page.input = input
    with pytest.raises(failure):
        await Actions(page).press("Control+Shift+K")
    assert not page.modifiers and not page.pressed
    assert [p["key"] for p in calls if p["type"] == "keyUp"] == ["K", "Control", "Shift"]
    assert sum(p["key"] == "K" and p["type"] == "rawKeyDown" for p in calls) == 1


async def test_modal_during_chord_defers_every_release_and_never_replays_keydown(engine):
    obj, page = engine
    calls = []

    async def call(method, payload=None, session=None, **kwargs):
        calls.append((method, payload))
        if (
            method == "Input.dispatchKeyEvent"
            and payload["key"] == "K"
            and payload["type"] == "rawKeyDown"
        ):
            page.observations.event(
                "Page.javascriptDialogOpening", {"type": "alert", "message": "pause"}, page.session
            )
            await asyncio.Event().wait()
        return {}

    obj.cdp.call = call
    with pytest.raises(DialogOpened):
        await asyncio.wait_for(Actions(page).press("Control+Shift+K"), 0.5)
    assert not page.modifiers and not page.pressed
    assert [p["key"] for _, p in page.deferred_inputs] == ["K", "Control", "Shift"]
    await obj.execute("dialog", {"targetId": page.target, "op": "handle", "accept": True})
    assert [
        p["key"] for m, p in calls if m == "Input.dispatchKeyEvent" and p["type"] == "keyUp"
    ] == ["K", "Control", "Shift"]
    assert (
        sum(
            m == "Input.dispatchKeyEvent" and p["key"] == "K" and p["type"] == "rawKeyDown"
            for m, p in calls
        )
        == 1
    )


@pytest.mark.parametrize("platform,modifier", [("MacIntel", "Meta"), ("Linux x86_64", "Control")])
async def test_platform_chord_resolves_modifier_and_restores_previous_state(
    engine, platform, modifier
):
    _, page = engine
    page.main.run = AsyncMock(return_value=platform)
    page.input = AsyncMock()
    await Actions(page).press("ControlOrMeta+A")
    payloads = [c.args[1] for c in page.input.await_args_list]
    assert payloads[0]["key"] == payloads[-1]["key"] == modifier
    assert payloads[1]["text"] == "" and not page.modifiers and not page.pressed


@pytest.mark.parametrize("failure", [ConnectionError, asyncio.CancelledError])
async def test_interrupted_mouse_press_releases_once_and_restores_modifiers(engine, failure):
    obj, page = engine
    actions = pointer_scene(obj, page)
    node = node_for(page)
    actions.actionable = AsyncMock(return_value=(node, {"x": 10, "y": 10}))
    calls = []

    async def input(method, payload):
        calls.append(payload.copy())
        if payload["type"] == "mousePressed":
            raise failure()

    page.input = input
    with pytest.raises(failure):
        await actions.click({"op": "click", "target": "css=x", "modifiers": ["Control"]})
    assert [p["type"] for p in calls if p["type"].startswith("mouse")] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert not page.modifiers and not page.pressed
    node.release.assert_awaited_once()


async def test_drag_release_failure_is_not_sent_twice_and_disables_interception(engine):
    obj, page = engine
    actions = Actions(page)
    nodes = [node_for(page, "source"), node_for(page, "target")]
    actions.actionable = AsyncMock(
        side_effect=[(nodes[0], {"x": 0, "y": 0}), (nodes[1], {"x": 50, "y": 50})]
    )
    calls = []

    async def input(method, payload):
        calls.append(payload.copy())
        if payload["type"] == "mouseReleased":
            raise ConnectionError("release acknowledgement lost")

    page.input = input
    with pytest.raises(ConnectionError):
        await actions.drag({"target": "css=source", "to": "css=target"})
    assert sum(p["type"] == "mouseReleased" for p in calls) == 1
    assert obj.cdp.call.await_args.args[:2] == ("Input.setInterceptDrags", {"enabled": False})
    assert all(n.release.await_count == 1 for n in nodes)


async def test_drag_target_disappears_releases_pressed_mouse_and_source(engine):
    obj, page = engine
    actions = Actions(page)
    source = node_for(page)
    actions.actionable = AsyncMock(
        side_effect=[(source, {"x": 0, "y": 0}), BrowserError("stale_ref")]
    )
    page.input = AsyncMock()
    with pytest.raises(BrowserError, match="stale_ref"):
        await actions.drag({"target": "css=source", "to": "stale"})
    assert [c.args[1]["type"] for c in page.input.await_args_list] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    source.release.assert_awaited_once()
    assert obj.cdp.call.await_args.args[:2] == ("Input.setInterceptDrags", {"enabled": False})


async def test_check_does_not_toggle_matching_state_or_retry_failed_postcondition(engine):
    _, page = engine
    actions = Actions(page)
    actions.checked = AsyncMock(side_effect=[True, False, False])
    actions.click = AsyncMock()
    await actions.act({"op": "check", "target": "css=x", "checked": True})
    actions.click.assert_not_awaited()
    with pytest.raises(BrowserError, match="checked_state_not_changed") as error:
        await actions.act({"op": "check", "target": "css=x", "checked": True})
    assert error.value.outcome == "completed"
    actions.click.assert_awaited_once()


async def test_select_retries_only_precondition_failures_and_releases_each_handle(engine):
    _, page = engine
    actions = Actions(page)
    node = node_for(page)
    actions.actionable = AsyncMock(return_value=(node, None))
    page.main.run = AsyncMock(
        side_effect=["error:optionsnotfound", "error:optionnotenabled", ["b"]]
    )
    result = await asyncio.wait_for(
        actions.act({"op": "select", "target": "css=select", "values": ["b"]}), 0.5
    )
    assert result["completed"] and node.release.await_count == 3
    page.main.run = AsyncMock(return_value="unsupported")
    with pytest.raises(BrowserError, match="select_failed"):
        await actions.act({"op": "select", "target": "css=select", "values": ["b"]})
    page.main.run.assert_awaited_once()


async def test_wait_recovers_from_destroyed_context_then_releases_matched_node(engine):
    _, page = engine
    node = node_for(page)
    page.resolve = AsyncMock(side_effect=[ProtocolError("context destroyed"), node])
    page.main.run = AsyncMock(return_value={"matches": True})
    result = await asyncio.wait_for(
        Actions(page).wait({"target": "css=x", "state": "visible"}), 0.5
    )
    assert result == {"ready": True} and page.resolve.await_count == 2
    node.release.assert_awaited_once()
