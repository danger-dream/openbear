"""Transfer, redirected network and concurrent disconnect regression tests."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.browser.pyworker.common import BrowserError
from app.browser.pyworker.transport import Connection
from tests import test_browser_python_engine

engine = test_browser_python_engine.engine


def upload_node(page):
    node = SimpleNamespace(frame=page.main, object_id="input", release=AsyncMock())
    page.resolve = AsyncMock(return_value=node)
    page.main.run = AsyncMock()
    return node


@pytest.mark.parametrize("kind", ["missing", "oversize"])
async def test_invalid_upload_never_dispatches_file_events(engine, tmp_path, kind):
    obj, page = engine
    node = upload_node(page)
    path = tmp_path / "upload.bin"
    if kind == "oversize":
        path.write_bytes(b"x" * (obj.config["maxArtifactBytes"] + 1))
    with pytest.raises(BrowserError, match="upload_"):
        await obj.execute(
            "files",
            {"targetId": page.target, "op": "upload", "target": "css=input", "paths": [str(path)]},
        )
    assert not any(
        "__startFiles" in c.args[0] or "__finishFiles" in c.args[0]
        for c in page.main.run.await_args_list
    )
    node.release.assert_awaited_once()


async def test_cancelled_upload_cleans_chunk_buffer_without_dispatch_or_retry(engine, tmp_path):
    obj, page = engine
    node = upload_node(page)
    path = tmp_path / "upload.bin"
    path.write_bytes(b"original bytes")
    calls = []

    async def run(source, *args):
        calls.append(source)
        if "__fileChunk" in source:
            raise asyncio.CancelledError()

    page.main.run = run
    with pytest.raises(asyncio.CancelledError):
        await obj.files.upload(page, {"op": "upload", "target": "css=input", "paths": [str(path)]})
    assert sum("__fileChunk" in s for s in calls) == 1
    assert not any("__finishFiles" in s for s in calls)
    assert "__fileTransfers.delete" in calls[-1]
    assert path.read_bytes() == b"original bytes"
    node.release.assert_awaited_once()


async def test_upload_dispatch_failure_is_unknown_and_not_replayed(engine, tmp_path):
    obj, page = engine
    node = upload_node(page)
    path = tmp_path / "file"
    path.write_bytes(b"content")

    async def run(source, *args):
        if "__finishFiles" in source:
            raise BrowserError("javascript_error")

    page.main.run.side_effect = run
    with pytest.raises(BrowserError, match="javascript_error") as error:
        await obj.execute(
            "files",
            {"targetId": page.target, "op": "upload", "target": "css=input", "paths": [str(path)]},
        )
    assert error.value.outcome == "unknown"
    assert sum("__finishFiles" in c.args[0] for c in page.main.run.await_args_list) == 1
    assert "__fileTransfers.delete" in page.main.run.await_args.args[0]
    node.release.assert_awaited_once()


async def test_file_chooser_from_destroyed_frame_does_not_upload_elsewhere(engine):
    obj, page = engine
    page.observations.chooser = {"frameId": "gone", "backendNodeId": 1}
    with pytest.raises(BrowserError, match="file_chooser_expired"):
        await obj.files.upload(page, {"op": "upload", "paths": []})
    obj.cdp.call.assert_not_awaited()


def download(obj, page, guid="file"):
    obj.files.event(
        "Browser.downloadWillBegin",
        {"frameId": page.main_id, "guid": guid, "suggestedFilename": "original.bin"},
    )
    obj.files.event(
        "Browser.downloadProgress", {"guid": guid, "state": "completed", "receivedBytes": 100}
    )


async def test_cancelled_download_save_removes_partial_output_but_keeps_original(
    engine, tmp_path, monkeypatch
):
    obj, page = engine
    obj.config["maxArtifactBytes"] = 1024 * 1024
    download(obj, page)
    source = tmp_path / "file"
    source.write_bytes(b"x" * (300 * 1024))
    output = tmp_path / "saved"
    # The cancellation point is between copied chunks, not a timing race.
    monkeypatch.setattr(
        "app.browser.pyworker.files.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await obj.files.execute(page, {"op": "save", "id": 1, "outputPath": str(output)})
    assert not output.exists() and source.stat().st_size == 300 * 1024


async def test_download_size_limit_removes_output_and_auto_cancel_is_not_repeated(engine, tmp_path):
    obj, page = engine
    download(obj, page)
    (tmp_path / "file").write_bytes(b"x" * 1001)
    output = tmp_path / "saved"
    with pytest.raises(BrowserError, match="artifact_too_large"):
        await obj.files.execute(page, {"op": "save", "id": 1, "outputPath": str(output)})
    assert not output.exists()
    obj.files.event(
        "Browser.downloadWillBegin",
        {"frameId": page.main_id, "guid": "large", "suggestedFilename": "large"},
    )
    obj.files.event(
        "Browser.downloadProgress", {"guid": "large", "state": "inProgress", "receivedBytes": 1001}
    )
    await asyncio.gather(*list(obj.tasks))
    await obj.files.execute(page, {"op": "cancel", "id": 2})
    assert [c.args[0] for c in obj.cdp.call.await_args_list] == ["Browser.cancelDownload"]
    with pytest.raises(BrowserError, match="artifact_too_large"):
        await obj.files.execute(page, {"op": "save", "id": 2, "outputPath": str(output)})


def test_unowned_download_events_are_not_adopted(engine):
    obj, page = engine
    obj.files.event(
        "Browser.downloadWillBegin",
        {"frameId": "other-page", "guid": "foreign", "suggestedFilename": "private"},
    )
    assert not obj.files.by_guid and not page.downloads


def redirect_chain(page):
    obs = page.observations
    obs.event(
        "Network.requestWillBeSent",
        {
            "requestId": "same-id",
            "request": {
                "method": "POST",
                "url": "https://test.invalid/first",
                "hasPostData": True,
                "postData": "first body",
            },
        },
        page.session,
    )
    obs.event(
        "Network.responseReceivedExtraInfo",
        {"requestId": "same-id", "headers": {"X-Actual": "first-hop"}},
        page.session,
    )
    obs.event(
        "Network.requestWillBeSent",
        {
            "requestId": "same-id",
            "redirectResponse": {
                "url": "https://test.invalid/first",
                "status": 302,
                "headers": {"Location": "/second"},
            },
            "request": {"method": "GET", "url": "https://test.invalid/second"},
        },
        page.session,
    )
    obs.event(
        "Network.responseReceived",
        {
            "requestId": "same-id",
            "response": {
                "url": "https://test.invalid/second",
                "status": 200,
                "headers": {"X-Hop": "second"},
            },
        },
        page.session,
    )
    obs.event("Network.loadingFinished", {"requestId": "same-id"}, page.session)
    return obs


async def test_redirect_hops_keep_their_own_headers_and_captured_request_body(engine, tmp_path):
    obj, page = engine
    obs = redirect_chain(page)
    headers = await obs.details({"id": 1, "part": "response-headers"})
    assert headers["headers"] == {"x-actual": "first-hop"}
    result = await obs.details(
        {"id": 1, "part": "request-body", "outputPath": str(tmp_path / "request")}
    )
    assert Path(result["artifact"]).read_text() == "first body"
    obj.cdp.call.assert_not_awaited()
    assert [r["status"] for r in obs.network.read()["entries"]] == [302, 200]


async def test_redirect_response_body_never_borrows_final_hop_content(engine, tmp_path):
    obj, page = engine
    obs = redirect_chain(page)
    obj.cdp.call.return_value = {"body": "FINAL RESPONSE"}
    with pytest.raises(BrowserError, match="network_body_unavailable"):
        await obs.details({"id": 1, "part": "response-body", "outputPath": str(tmp_path / "first")})
    obj.cdp.call.assert_not_awaited()
    result = await obs.details(
        {"id": 2, "part": "response-body", "outputPath": str(tmp_path / "second")}
    )
    assert Path(result["artifact"]).read_text() == "FINAL RESPONSE"


async def test_network_failure_and_eviction_do_not_fetch_another_request(engine, tmp_path):
    obj, page = engine
    obs = redirect_chain(page)
    obs.event("Network.loadingFailed", {"requestId": "same-id"}, page.session)
    with pytest.raises(BrowserError, match="network_body_unavailable"):
        await obs.details({"id": 2, "part": "response-body", "outputPath": str(tmp_path / "body")})
    obs.configure(1)
    with pytest.raises(BrowserError, match="network_entry_expired"):
        await obs.details({"id": 1, "part": "request-headers"})
    obj.cdp.call.assert_not_awaited()


async def test_network_body_checks_decoded_size_even_without_content_length(engine, tmp_path):
    obj, page = engine
    obs = redirect_chain(page)
    obj.cdp.call.return_value = {"body": "x" * 101}
    output = tmp_path / "body"
    with pytest.raises(BrowserError, match="network_body_too_large"):
        await obs.details({"id": 2, "part": "response-body", "outputPath": str(output)})
    assert not output.exists()


async def test_concurrent_cdp_disconnect_rejects_every_pending_call_without_resending():
    received = []

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            received.append(message.json())
            if len(received) == 2:
                await ws.close()
                break
        return ws

    app = web.Application()
    app.router.add_get("/ws", socket)
    server = TestServer(app)
    await server.start_server()
    cdp = Connection(str(server.make_url("/ws")).replace("http:", "ws:"))
    try:
        await cdp.connect()
        results = await asyncio.gather(
            cdp.call("first"), cdp.call("second"), return_exceptions=True
        )
        assert all(isinstance(r, ConnectionError) for r in results)
        assert not cdp.pending and not cdp.connected
        assert [r["method"] for r in received] == ["first", "second"]
    finally:
        await cdp.close()
        await server.close()


async def test_cdp_out_of_order_replies_stay_with_their_own_call_and_timeout_does_not_poison_next():
    received = []

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            data = message.json()
            received.append(data)
            if data["method"] == "timeout":
                continue
            if len(received) == 2:
                for row in reversed(received):
                    await ws.send_json({"id": row["id"], "result": {"method": row["method"]}})
            elif data["method"] == "after":
                await ws.send_json({"id": received[-2]["id"], "result": {"late": True}})
                await ws.send_json({"id": data["id"], "result": {"method": "after"}})
        return ws

    app = web.Application()
    app.router.add_get("/ws", socket)
    server = TestServer(app)
    await server.start_server()
    cdp = Connection(str(server.make_url("/ws")).replace("http:", "ws:"))
    try:
        await cdp.connect()
        results = await asyncio.gather(cdp.call("first"), cdp.call("second"))
        assert results == [{"method": "first"}, {"method": "second"}]
        with pytest.raises(TimeoutError):
            await cdp.call("timeout", timeout=0.01)
        assert await cdp.call("after") == {"method": "after"}
        assert not cdp.pending and len(received) == 4
    finally:
        await cdp.close()
        await server.close()
