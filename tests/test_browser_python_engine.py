"""Python/CDP engine regression tests: protocol doubles, no real browser or Node."""

from __future__ import annotations

import asyncio
import base64
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.browser.config import BrowserConfig
from app.browser.pyworker.common import BrowserError, DialogOpened, Ring, mark_effect
from app.browser.pyworker.engine import Engine
from app.browser.pyworker.frames import Frame, Node
from app.browser.pyworker.geometry import clickable_point, transform, viewport_quad
from app.browser.pyworker.page import Page
from app.browser.pyworker.transport import Connection
from app.browser.worker import Worker


@pytest.fixture
def engine(tmp_path):
    obj = Engine()
    obj.config = dict(
        maxEventEntries=2,
        maxBodyBytes=100,
        maxArtifactBytes=1000,
        snapshotMaxChars=1000,
        downloadHostPath=str(tmp_path),
    )
    obj.cdp = SimpleNamespace(call=AsyncMock(return_value={}))
    page = Page(obj, "target")
    page.session = "session"
    frame = Frame(page, "frame", session=page.session)
    frame.url = "https://test.invalid"
    page.frames[frame.id] = frame
    page.main_id = frame.id
    obj.pages[page.target] = page
    return obj, page


def test_ring_cursor_and_loss():
    ring = Ring(2)
    for i in range(3):
        ring.add({"value": i})
    assert ring.read()["dropped"]
    assert [r["value"] for r in ring.read(2)["entries"]] == [2]
    assert not ring.read(3)["entries"]
    ring.trim(1)
    assert ring.read()["cursor"] == 3


async def test_configure_trims_live_events_and_request_details_without_reconnect(engine):
    obj, page = engine
    cdp = obj.cdp
    for i in range(2):
        page.observations.console.add({"text": str(i)})
        page.observations.event(
            "Network.requestWillBeSent",
            {"requestId": str(i), "request": {"method": "GET", "url": "https://test.invalid"}},
            page.session,
        )
    result = await obj.execute(
        "configure",
        dict(snapshotMaxChars=77, maxEventEntries=1, maxBodyBytes=50, maxArtifactBytes=80),
    )
    assert result == {"configured": True} and obj.cdp is cdp
    assert len(page.observations.records) == len(page.observations.current) == 1
    assert page.observations.console.read()["dropped"]
    assert len(page.observations.network.rows) == 1
    cdp.call.assert_not_called()


def snapshot_frame(page):
    frame = page.main

    async def run(source, *args, **kwargs):
        if "ariaSnapshotJSON" in source:
            return {
                "json": [{"role": "button", "name": "Save", "ref": "e1"}],
                "iframeRefs": [],
                "iframeDepths": {},
            }
        return '- button "Save" [ref=e1]\n- text: hello'

    frame.run = AsyncMock(side_effect=run)
    node = Node(frame, "node", frame.epoch)
    frame.query = AsyncMock(return_value=node)
    frame.call_object = AsyncMock(return_value=True)
    return frame


async def test_snapshot_refs_version_navigation_and_literal_find(engine):
    obj, page = engine
    frame = snapshot_frame(page)
    snap = await obj.execute("snapshot", {"targetId": page.target})
    ref = snap["snapshotId"] + ":e1"
    assert (await page.resolve(ref)).object_id == "node"
    frame.query.assert_awaited_with("aria-ref=e1")
    found = await obj.execute("find", {"targetId": page.target, "text": "save"})
    assert "Save" in found["text"]
    with pytest.raises(BrowserError, match="stale_ref"):
        await page.resolve(ref)
    assert (await obj.execute("find", {"targetId": page.target, "text": "[.*]"}))["text"] == ""
    fresh = await obj.execute("snapshot", {"targetId": page.target})
    frame.invalidate()
    with pytest.raises(BrowserError, match="stale_ref"):
        await page.resolve(fresh["snapshotId"] + ":e1")


async def test_detached_valid_ref_can_finish_hidden_wait_but_cannot_click(engine):
    obj, page = engine
    frame = snapshot_frame(page)
    snap = await obj.execute("snapshot", {"targetId": page.target})
    ref = snap["snapshotId"] + ":e1"
    frame.query.return_value = None
    assert await page.resolve(ref, wait=False) is None
    with pytest.raises(BrowserError, match="stale_ref"):
        await page.resolve(ref)


@pytest.mark.parametrize("started,expected", [(False, "failed"), (True, "unknown")])
async def test_timeout_never_retries_and_preserves_effect_status(engine, started, expected):
    obj, page = engine
    calls = []

    async def navigate(params):
        calls.append(params)
        if started:
            mark_effect()
        await asyncio.Event().wait()

    page.navigate = navigate
    with pytest.raises(BrowserError, match="action_timeout") as error:
        await obj.execute("navigate", {"targetId": page.target, "timeoutMs": 20})
    assert error.value.outcome == expected and len(calls) == 1


async def test_dialog_interrupts_input_without_replay_and_handle_is_explicit(engine):
    obj, page = engine
    calls = []

    async def call(method, params=None, session=None, **kwargs):
        calls.append((method, params))
        if method.startswith("Input."):
            page.observations.event(
                "Page.javascriptDialogOpening",
                {"type": "prompt", "message": "value?", "defaultPrompt": "x"},
                page.session,
            )
            await asyncio.Event().wait()
        return {}

    obj.cdp.call = call
    with pytest.raises(DialogOpened):
        await asyncio.wait_for(
            page.input("Input.dispatchMouseEvent", {"type": "mouseReleased"}), 0.2
        )
    assert len(calls) == 1 and page.observations.dialog
    inspect = await obj.execute("dialog", {"targetId": page.target, "op": "inspect"})
    assert inspect["dialog"]["message"] == "value?" and "session" not in inspect["dialog"]
    with pytest.raises(BrowserError, match="dialog_pending"):
        await obj.execute("snapshot", {"targetId": page.target})
    handled = await obj.execute(
        "dialog", {"targetId": page.target, "op": "handle", "accept": True, "text": "answer"}
    )
    assert handled["handled"] and page.observations.dialog is None
    assert len([m for m, _ in calls if m.startswith("Input.")]) == 1
    assert calls[-1] == ("Page.handleJavaScriptDialog", {"accept": True, "promptText": "answer"})


async def test_release_not_sent_before_dialog_is_deferred_once(engine):
    obj, page = engine
    page.observations.event(
        "Page.javascriptDialogOpening", {"type": "alert", "message": "pause"}, page.session
    )
    with pytest.raises(DialogOpened):
        await page.input("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter"})
    obj.cdp.call.assert_not_called()
    await obj.execute("dialog", {"targetId": page.target, "op": "handle", "accept": True})
    assert [c.args[0] for c in obj.cdp.call.await_args_list] == [
        "Page.handleJavaScriptDialog",
        "Input.dispatchKeyEvent",
    ]
    assert not page.deferred_inputs


async def test_evaluate_sent_to_browser_and_large_result_is_artifact(engine, tmp_path):
    obj, page = engine
    expression = 'function(){return fetch("/submit",{method:"POST"})}'
    obj.cdp.call.return_value = {"result": {"value": {"answer": 42}}}
    result = await obj.execute("evaluate", {"targetId": page.target, "expression": expression})
    assert result == {"value": {"answer": 42}}
    call = obj.cdp.call.await_args
    assert call.args[0] == "Runtime.evaluate"
    assert "function(){return fetch" in call.args[1]["expression"]
    obj.config["snapshotMaxChars"] = 3
    out = tmp_path / "value.json"
    result = await obj.execute(
        "evaluate", {"targetId": page.target, "expression": "42", "outputPath": str(out)}
    )
    assert result["artifact"] == str(out) and out.read_text() == '{"answer":42}'
    assert "data" not in result


async def test_capture_writes_css_viewport_size_and_limits(engine, tmp_path):
    obj, page = engine
    page.main.run = AsyncMock(side_effect=[None, 2])
    page.main.viewport = AsyncMock(return_value={"width": 1000, "height": 700})
    viewport = {"pageX": 0, "pageY": 0, "clientWidth": 985, "clientHeight": 700}

    async def call(method, *args, **kwargs):
        if method == "Page.getLayoutMetrics":
            return {"layoutViewport": viewport, "visualViewport": viewport}
        if method == "Page.captureScreenshot":
            assert args[0]["clip"]["width"] == 1000 and args[0]["clip"]["scale"] == 0.5
            return {"data": base64.b64encode(b"png").decode()}
        return {}

    obj.cdp.call.side_effect = call
    result = await obj.execute(
        "capture", {"targetId": page.target, "outputPath": str(tmp_path / "shot.png")}
    )
    assert result["bytes"] == 3 and result["mime"] == "image/png"
    assert (tmp_path / "shot.png").read_bytes() == b"png" and "data" not in result
    obj.config["maxArtifactBytes"] = 2
    with pytest.raises(BrowserError, match="artifact_too_large"):
        await obj.artifact(tmp_path / "too-big", b"png")
    assert not (tmp_path / "too-big").exists()


async def test_network_extra_headers_bodies_limits_and_event_cursors(engine, tmp_path):
    obj, page = engine
    obs = page.observations
    obs.event(
        "Network.requestWillBeSentExtraInfo",
        {"requestId": "r", "headers": {"X-Actual": "yes"}},
        page.session,
    )
    obs.event(
        "Network.requestWillBeSent",
        {
            "requestId": "r",
            "request": {
                "method": "POST",
                "url": "https://test.invalid/data",
                "postData": "request",
            },
        },
        page.session,
    )
    obs.event(
        "Network.responseReceived",
        {
            "requestId": "r",
            "response": {
                "url": "https://test.invalid/data",
                "status": 200,
                "headers": {"Content-Length": "101"},
            },
        },
        page.session,
    )
    headers = await obs.details({"id": 1, "part": "request-headers"})
    assert headers["headers"] == {"x-actual": "yes"}
    with pytest.raises(BrowserError, match="network_body_too_large"):
        await obs.details({"id": 1, "part": "response-body"})
    obj.cdp.call.assert_not_called()
    result = await obs.details(
        {"id": 1, "part": "request-body", "outputPath": str(tmp_path / "request")}
    )
    assert Path(result["artifact"]).read_text() == "request"
    obs.event(
        "Network.responseReceivedExtraInfo",
        {"requestId": "r", "headers": {"Content-Length": "3"}},
        page.session,
    )
    obs.event("Network.loadingFinished", {"requestId": "r"}, page.session)
    obj.cdp.call.return_value = {"body": base64.b64encode(b"raw").decode(), "base64Encoded": True}
    result = await obs.details(
        {"id": 1, "part": "response-body", "outputPath": str(tmp_path / "body")}
    )
    assert Path(result["artifact"]).read_bytes() == b"raw"
    obs.console_ready.add(page.session)
    for i in range(3):
        obs.event(
            "Console.messageAdded",
            {"message": {"source": "console-api", "text": str(i)}},
            page.session,
        )
    assert obs.console.read()["dropped"] and obs.console.read(2)["entries"][0]["text"] == "2"


async def test_download_uses_original_guid_bytes_and_remote_mapping_is_explicit(engine, tmp_path):
    obj, page = engine
    obj.files.event(
        "Browser.downloadWillBegin",
        {"frameId": page.main.id, "guid": "guid", "suggestedFilename": "original.bin"},
    )
    obj.files.event(
        "Browser.downloadProgress", {"guid": "guid", "state": "completed", "receivedBytes": 3}
    )
    params = {"op": "save", "id": 1, "outputPath": str(tmp_path / "saved")}
    with pytest.raises(BrowserError, match="download_file_unavailable"):
        await obj.files.execute(page, params)
    (tmp_path / "guid").write_bytes(b"raw")
    assert (await obj.files.execute(page, params))["bytes"] == 3
    assert (tmp_path / "saved").read_bytes() == b"raw"
    obj.cdp.call.assert_not_called()


def test_old_node_setting_is_ignored_and_shared_paths_can_be_saved_separately():
    cfg = BrowserConfig.model_validate(
        {"nodeCommand": "/old/node", "mainDownloadHostPath": "/host/downloads"}
    )
    assert cfg.main_download_host_path == "/host/downloads"
    assert "nodeCommand" not in cfg.model_dump(by_alias=True)
    with pytest.raises(ValueError):
        BrowserConfig.model_validate({"mainDownloadBrowserPath": "relative"})


def test_frame_coordinate_mapping_and_clipping():
    quad = [100, 100, 300, 100, 300, 300, 100, 300]
    assert transform({"x": 50, "y": 50}, viewport_quad(100, 100), quad) == {"x": 200, "y": 200}
    point = clickable_point([[-10, -10, 20, -10, 20, 20, -10, 20]], 10, 10)
    assert point and 0 <= point["x"] <= 10 and 0 <= point["y"] <= 10


async def test_real_python_worker_protocol_initializes_without_attaching_unrelated_pages(tmp_path):
    methods = []

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            data = msg.json()
            methods.append(data["method"])
            await ws.send_json({"id": data["id"], "result": {}})
        return ws

    app = web.Application()
    app.router.add_get("/devtools/browser/test", ws_handler)
    server = TestServer(app)
    await server.start_server()
    worker = Worker()
    try:
        await worker.start(
            {
                "endpoint": str(server.make_url("/devtools/browser/test")).replace("http:", "ws:"),
                "connectTimeoutS": 2,
                "downloadHostPath": str(tmp_path),
            }
        )
        process = worker.proc
        assert worker.connected
        assert methods == [
            "Browser.getVersion",
            "Browser.setDownloadBehavior",
            "Target.setDiscoverTargets",
        ]
        result = await worker.call(
            "configure",
            {
                "snapshotMaxChars": 1000,
                "maxEventEntries": 2,
                "maxBodyBytes": 100,
                "maxArtifactBytes": 1000,
            },
            timeout=1,
        )
        assert result == {"configured": True}
        await worker.close()
        assert process.returncode == 0
    finally:
        await worker.close()
        await server.close()


async def test_cdp_multiplexes_events_and_errors_without_replay():
    received = []
    observed = []

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            data = msg.json()
            received.append(data)
            if data["method"] == "hang":
                continue
            await ws.send_json(
                {"method": "Page.loadEventFired", "sessionId": "s", "params": {"timestamp": 1}}
            )
            await ws.send_json({"id": data["id"], "result": {"answer": data["method"]}})
        return ws

    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    server = TestServer(app)
    await server.start_server()
    cdp = Connection(str(server.make_url("/ws")).replace("http:", "ws:"))
    cdp.on_event = lambda *args: observed.append(args)
    try:
        await cdp.connect()
        assert await cdp.call("test", {"x": 1}, "s") == {"answer": "test"}
        assert observed == [("Page.loadEventFired", {"timestamp": 1}, "s")]
        assert received[0]["sessionId"] == "s"
        with pytest.raises(TimeoutError):
            await cdp.call("hang", timeout=0.02)
        assert not cdp.pending and len(received) == 2
    finally:
        await cdp.close()
        await server.close()
