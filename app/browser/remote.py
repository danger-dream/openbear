"""A browser on a server node: the same CDP and worker interfaces as the local
browser, carried over the node channel. The node runs the engine next to its
browser; artifacts and uploads cross the channel in bounded chunks."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from app.browser.worker import WorkerError

CHUNK = 4 * 1024 * 1024
# Raw CDP the service itself sends; page work always goes through the engine.
CDP_METHODS = {"Target.getTargets", "Target.createTarget", "Target.closeTarget", "Browser.getVersion"}
TARGET_METHODS = {"Runtime.evaluate", "Runtime.terminateExecution"}


class NodeUnavailable(WorkerError):
    pass


class NodeBrowserUnavailable(ConnectionError, ValueError):
    """The node's browser could not be reached or started; nothing was sent to a
    page. A ValueError so the tool reports its reason as not_started."""


async def _node(link, method, params, timeout):
    """One node call; transport failures keep the local worker's error vocabulary."""
    try:
        reply = await link.call(method, params, timeout)
    except LookupError:
        raise NodeUnavailable("node_offline", "not_started") from None
    except ConnectionError:
        raise WorkerError("browser_connection_lost", "unknown") from None
    if not reply.get("ok"):
        raise NodeUnavailable(str(reply.get("error") or "node_error")[:80], "not_started", responded=True)
    return reply.get("result") if isinstance(reply.get("result"), dict) else {}


async def restart_node_browser(link, timeout):
    """Restart the browser the node launched, or reconnect to the one it attaches to."""
    # The node closes the old browser (waiting for a desktop window to exit),
    # then launches and initializes a new one, each with its own bound.
    await _node(link, "browser.restart", {}, max(timeout, 15) + 105)


def _unavailable(state):
    """Why the node's browser is not ready, worded for the model."""
    name = state.get("state") or "unavailable"
    if name == "installing":
        got = int(state.get("downloaded") or 0) // (1024 * 1024)
        return (f"node_browser_installing: the node is downloading Chrome for Testing ({got} MB so far); "
                "try again in a few minutes")
    return f"node_browser_{name}: {state.get('error') or ''}".rstrip(": ")


class RemoteCDP:
    def __init__(self, link, timeout):
        self.link = link
        self.timeout = timeout
        self.endpoint = "node:" + link.server_id
        self.ws_url = ""
        self.session = None
        self.state = {}

    @property
    def connected(self):
        return bool(self.ws_url) and self.session is not None and self.session is self.link.session()

    def _epoch(self, value):
        if value.get("epoch"):
            self.ws_url = f"node://{self.link.server_id}/{value['epoch']}"

    async def connect(self):
        if self.connected:
            return
        try:
            state = await _node(self.link, "browser.start", {}, max(self.timeout, 15))
        except NodeUnavailable as exc:
            if str(exc) == "capability_disabled":
                raise NodeBrowserUnavailable("node_browser_disabled: the browser is turned off on this server") from None
            raise NodeBrowserUnavailable(str(exc)) from None
        except WorkerError as exc:
            raise ConnectionError(str(exc)) from None
        self.state = state
        if state.get("state") != "ready":
            raise NodeBrowserUnavailable(_unavailable(state))
        self.session = self.link.session()
        self._epoch(state)

    async def call(self, method, params=None, *, session_id=None, timeout=None):
        if method not in CDP_METHODS or session_id:
            raise RuntimeError("node_cdp_method_not_forwarded")
        await self.connect()
        return await self._raw({"method": method, "params": params or {}}, timeout)

    async def target_call(self, target_id, method, params=None, *, timeout=2):
        if method not in TARGET_METHODS:
            raise RuntimeError("node_cdp_method_not_forwarded")
        await self.connect()
        return await self._raw({"method": method, "params": params or {}, "target": target_id}, timeout)

    async def _raw(self, body, timeout):
        try:
            value = await _node(self.link, "browser.cdp", body, (timeout or self.timeout) + 5)
        except NodeUnavailable as exc:
            # Not sent, or refused by the node before reaching the browser.
            self.ws_url = ""
            raise NodeBrowserUnavailable(str(exc)) from None
        except WorkerError as exc:
            self.ws_url = ""
            raise ConnectionError(str(exc)) from None
        if value.get("error") == "browser_connection_lost":
            # The node drops that browser; the next connect starts or attaches again.
            self.ws_url = ""
            raise ConnectionError("cdp_disconnected")
        if value.get("error"):
            raise RuntimeError(str(value["error"])[:240])
        self._epoch(value)
        return value.get("result") if isinstance(value.get("result"), dict) else {}

    async def close(self):
        # The node keeps its browser; only this controller view is dropped.
        self.ws_url, self.session = "", None


class RemoteWorker:
    def __init__(self, link):
        self.link = link
        self.generation = 0
        self.session = None
        self.fatal = ""
        self.max_artifact = 0

    @property
    def connected(self):
        return self.session is not None and self.session is self.link.session() and not self.fatal

    async def start(self, init):
        limits = {k: init[k] for k in ("snapshotMaxChars", "maxEventEntries", "maxBodyBytes", "maxArtifactBytes")}
        await self._call("configure", limits, init["connectTimeoutS"] + 5)
        self.session, self.fatal = self.link.session(), ""
        self.max_artifact = limits["maxArtifactBytes"]
        self.generation += 1

    async def call(self, action, params, *, timeout):
        if not self.connected:
            raise WorkerError("worker_unavailable", "not_started")
        params = dict(params)
        output = params.pop("outputPath", None)
        if output:
            # The node writes into its own folder; the file is fetched back here.
            params["outputPath"] = "*"
        if params.get("paths"):
            params["paths"] = [await self._upload(Path(p)) for p in params["paths"]]
        result = await self._call(action, params, timeout)
        if action == "configure":
            self.max_artifact = params["maxArtifactBytes"]
        if result.get("artifact"):
            if not output:
                raise WorkerError("invalid_worker_artifact", "unknown")
            await self._fetch(result["artifact"], Path(output))
            result["artifact"] = output
        return result

    async def _call(self, action, params, timeout):
        try:
            value = await _node(self.link, "browser.call", {"action": action, "params": params}, timeout + 5)
        except NodeUnavailable:
            raise
        except WorkerError:
            # A lost channel does not prove the browser-side effect stopped. Never replay.
            self.fatal = "worker_request_interrupted"
            raise
        except TimeoutError:
            self.fatal = "worker_request_interrupted"
            raise
        if "error" in value:
            raise WorkerError(str(value["error"]), value.get("outcome", "unknown"),
                              details=value.get("details") or {}, responded=True)
        return value.get("result") if isinstance(value.get("result"), dict) else {}

    async def _upload(self, path):
        upload = base64.urlsafe_b64encode(os.urandom(9)).decode()
        size, offset, node_path = path.stat().st_size, 0, ""
        with path.open("rb") as stream:
            while True:
                data = stream.read(CHUNK)
                # A file that shrank while being sent ends early instead of looping.
                done = len(data) < CHUNK or offset + len(data) >= size
                value = await _node(self.link, "browser.upload", {"upload": upload, "name": path.name, "offset": offset,
                                                                  "data": base64.b64encode(data).decode(), "done": done}, 60)
                offset += len(data)
                if done:
                    node_path = value.get("path", "")
                    break
        if not node_path:
            raise WorkerError("upload_failed", "not_started")
        return node_path

    async def _fetch(self, remote, local):
        fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                offset = 0
                while True:
                    value = await _node(self.link, "browser.artifact", {"path": remote, "offset": offset, "limit": CHUNK}, 60)
                    data = base64.b64decode(value.get("data") or "")
                    offset += len(data)
                    if self.max_artifact and offset > self.max_artifact:
                        raise WorkerError("artifact_too_large", "completed", responded=True)
                    stream.write(data)
                    if value.get("eof"):
                        break
                    if not data:
                        raise WorkerError("artifact_transfer_failed", "unknown")
        except BaseException:
            local.unlink(missing_ok=True)
            raise

    async def close(self):
        self.session = None
        self.fatal = self.fatal or "worker_closed"
