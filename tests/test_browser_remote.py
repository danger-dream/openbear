"""The controller side of a server node's browser, against a scripted node link:
error reporting, epoch changes, transfers and node names. No real node or Chrome."""

from __future__ import annotations

import asyncio
import base64
from pathlib import PosixPath
from types import SimpleNamespace

import pytest

from app.browser.remote import RemoteCDP, RemoteWorker
from app.browser.service import BrowserService
from app.browser.worker import WorkerError
from app.servers.browser import NodeBrowsers
from tests.test_browser_native import config, ctx


class Link:
    server_id = "srv-1"

    def __init__(self, handler):
        self.handler, self.calls, self._session = handler, [], object()

    def session(self):
        return self._session

    async def call(self, method, params, timeout):
        self.calls.append((method, params))
        return await self.handler(method, params)


class Nodes:
    any_browser = True

    def __init__(self, link):
        self._link = link

    def link(self, server_id):
        return self._link

    async def owner(self, context):
        return 1

    async def available(self, owner):
        return [{"node": "web-1", "serverId": "srv-1", "online": True}]

    async def resolve(self, context, value):
        return (await self.available(1))[0]


def node_service(tmp_path, handler):
    link = Link(handler)
    service = BrowserService(config(tmp_path, enabled=False, mainEndpoint=""), str(tmp_path / "workspace"), str(tmp_path / "state"))
    service.nodes = Nodes(link)
    return service, link


class Node:
    """A node with one browser: its epoch, targets and engine replies."""

    def __init__(self):
        self.state = {"state": "ready", "epoch": "e1"}
        self.targets = []

    async def __call__(self, method, params):
        if method == "browser.start":
            return {"ok": True, "result": dict(self.state)}
        if method == "browser.cdp":
            epoch = self.state["epoch"]
            if params["method"] == "Target.getTargets":
                return {"ok": True, "result": {"result": {"targetInfos": [
                    {"targetId": t, "type": "page", "url": "about:blank"} for t in self.targets]}, "epoch": epoch}}
            if params["method"] == "Target.createTarget":
                self.targets.append(f"t{len(self.targets) + 1}")
                return {"ok": True, "result": {"result": {"targetId": self.targets[-1]}, "epoch": epoch}}
        if method == "browser.call":
            action = params["action"]
            result = {"configure": {"configured": True}, "prepare": {"ready": True, "url": "about:blank"}}.get(action, {"ok": action})
            return {"ok": True, "result": {"result": result}}
        raise AssertionError(method)


async def test_a_node_browser_that_is_not_ready_says_why(tmp_path):
    state = {"state": "installing", "downloaded": 37 * 1024 * 1024}

    async def handler(method, params):
        return {"ok": True, "result": dict(state)}
    service, _ = node_service(tmp_path, handler)
    opened = await service.call({"action": "page", "params": {"op": "new", "node": "web-1"}}, ctx())
    assert opened["status"] == "error" and opened["outcome"] == "not_started", opened
    assert opened["error"].startswith("node_browser_installing: the node is downloading Chrome for Testing (37 MB"), opened

    state.clear()
    state.update(state="failed", error="browser_not_runnable: /opt/google/chrome/chrome")
    service.instances["node-srv-1"].cdp.session = None
    opened = await service.call({"action": "page", "params": {"op": "new", "node": "web-1"}}, ctx())
    assert opened["error"] == "node_browser_failed: browser_not_runnable: /opt/google/chrome/chrome", opened
    assert opened["outcome"] == "not_started"


async def test_an_offline_node_is_reported_as_not_started(tmp_path):
    async def handler(method, params):
        raise LookupError("node_offline")
    service, _ = node_service(tmp_path, handler)
    opened = await service.call({"action": "page", "params": {"op": "list", "node": "web-1"}}, ctx())
    assert opened == {"status": "error", "error": "node_offline", "outcome": "not_started"}


async def test_a_node_with_its_browser_turned_off_says_so(tmp_path):
    async def handler(method, params):
        return {"ok": False, "error": "capability_disabled"}
    service, _ = node_service(tmp_path, handler)
    opened = await service.call({"action": "page", "params": {"op": "new", "node": "web-1"}}, ctx())
    assert opened["error"].startswith("node_browser_disabled") and opened["outcome"] == "not_started", opened


async def test_a_cdp_call_the_node_refuses_was_not_sent(tmp_path):
    node = Node()

    async def handler(method, params):
        if method == "browser.cdp":
            return {"ok": False, "error": "busy"}
        return await node(method, params)
    service, _ = node_service(tmp_path, handler)
    opened = await service.call({"action": "page", "params": {"op": "new", "node": "web-1"}}, ctx())
    assert opened == {"status": "error", "error": "busy", "outcome": "not_started"}, opened


async def test_pages_follow_the_browser_a_node_reports(tmp_path):
    node = Node()
    service, link = node_service(tmp_path, node)
    first = await service.call({"action": "page", "params": {"op": "new", "node": "web-1"}}, ctx())
    assert first["status"] == "ok" and first["node"] == "web-1", first
    assert (await service.call({"action": "snapshot", "page": first["page"], "params": {}}, ctx()))["status"] == "ok"

    # The node attaches its browser again (same tabs, new epoch) while this
    # controller stays connected: the old handle expires, a new page works.
    node.state["epoch"] = "e2"
    expired = await service.call({"action": "snapshot", "page": first["page"], "params": {}}, ctx())
    assert expired["error"].startswith("page_expired"), expired
    second = await service.call({"action": "page", "params": {"op": "new", "node": "web-1"}}, ctx())
    assert second["status"] == "ok", second
    snapshot = await service.call({"action": "snapshot", "page": second["page"], "params": {}}, ctx())
    assert snapshot["status"] == "ok", snapshot
    # The new engine was given this service's limits again.
    assert [p["action"] for m, p in link.calls if m == "browser.call"].count("configure") == 2
    listed = await service.call({"action": "page", "params": {"op": "list", "node": "web-1"}}, ctx())
    assert {p["page"]: p["owned"] for p in listed["pages"]}[second["page"]] is True
    assert sum(not p["owned"] and p["adoptable"] for p in listed["pages"]) == 1


async def test_a_lost_browser_connection_makes_the_next_call_start_again(tmp_path):
    replies = [{"ok": True, "result": {"error": "browser_connection_lost", "epoch": "e1"}}]

    async def handler(method, params):
        if method == "browser.start":
            return {"ok": True, "result": {"state": "ready", "epoch": "e1"}}
        return replies.pop(0)
    cdp = RemoteCDP(Link(handler), 5)
    with pytest.raises(ConnectionError):
        await cdp.call("Browser.getVersion")
    assert not cdp.connected


async def test_an_upload_that_shrinks_while_sent_ends(tmp_path):
    class Shrunk(PosixPath):
        def stat(self, **kwargs):
            return SimpleNamespace(st_size=100)
    path = tmp_path / "f.txt"
    path.write_bytes(b"0123456789")

    async def handler(method, params):
        return {"ok": True, "result": {"path": "/node/uploads/x/f.txt"} if params["done"] else {}}
    link = Link(handler)
    worker = RemoteWorker(link)
    assert await asyncio.wait_for(worker._upload(Shrunk(path)), 2) == "/node/uploads/x/f.txt"
    assert len(link.calls) == 1 and base64.b64decode(link.calls[0][1]["data"]) == b"0123456789"


async def test_an_artifact_over_the_limit_is_not_kept(tmp_path):
    async def handler(method, params):
        return {"ok": True, "result": {"data": base64.b64encode(b"x" * 8).decode(), "eof": False}}
    worker = RemoteWorker(Link(handler))
    worker.max_artifact = 20
    local = tmp_path / "shot.png"
    with pytest.raises(WorkerError) as failure:
        await worker._fetch("/node/artifacts/a", local)
    assert str(failure.value) == "artifact_too_large" and not local.exists()


async def test_a_node_name_shared_by_two_servers_needs_the_id():
    browsers = NodeBrowsers(SimpleNamespace())
    rows = [{"node": "web", "serverId": "a", "online": True}, {"node": "Web", "serverId": "b", "online": True}]

    async def owner(context):
        return 1

    async def available(owner):
        return rows
    browsers.owner, browsers.available = owner, available
    with pytest.raises(ValueError, match="node_browser_ambiguous"):
        await browsers.resolve(None, "web")
    assert (await browsers.resolve(None, "b"))["serverId"] == "b"
