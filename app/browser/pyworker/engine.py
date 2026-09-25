from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import os
import re
import tempfile
from pathlib import Path

from .actions import Actions
from .common import (
    BrowserError,
    DialogOpened,
    Operation,
    ProtocolError,
    mark_effect,
    operation,
    set_phase,
)
from .files import Files
from .frames import Node, remote_result
from .page import Page
from .snapshots import snapshot
from .transport import Connection


class Engine:
    def __init__(self):
        self.config = {}
        self.cdp = None
        self.pages = {}
        self.sessions = {}
        self.page_locks = {}
        self.tasks = set()
        self.frame_seq = 0
        self.files = Files(self)

    def next_frame_seq(self):
        self.frame_seq += 1
        return self.frame_seq

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine, context=contextvars.Context())
        self.tasks.add(task)

        def done(task):
            self.tasks.discard(task)
            if not task.cancelled():
                task.exception()  # A failed child setup is recorded on its Page, not leaked to stderr.

        task.add_done_callback(done)
        return task

    def on_event(self, method, data, session):
        if method.startswith("Browser.download"):
            self.files.event(method, data)
        if page := self.sessions.get(session):
            page.event(method, data, session)
        if method == "Target.targetDestroyed":
            if page := self.pages.pop(data["targetId"], None):
                page.closed = True
                page.invalidate_refs()
                self.sessions = {
                    sid: owner for sid, owner in self.sessions.items() if owner is not page
                }
                self.page_locks.pop(page.target, None)
                for download in page.downloads.values():
                    self.files.by_guid.pop(download["guid"], None)
                self.spawn(page.close())

    async def initialize(self, config):
        self.config = dict(config)
        if not self.config.get("downloadHostPath"):
            self.config["downloadHostPath"] = tempfile.mkdtemp(prefix="openbear-browser-downloads-")
        self.config.setdefault("downloadBrowserPath", self.config["downloadHostPath"])
        Path(self.config["downloadHostPath"]).mkdir(parents=True, exist_ok=True)
        # Fixed hard ceiling accommodates the public maximum artifact size after base64.
        # Actual artifact/body limits are enforced separately and can hot-update.
        self.cdp = Connection(
            config["endpoint"],
            config["connectTimeoutS"],
            max_message=512 * 1024 * 1024 * 4 // 3 + 2 * 1024 * 1024,
        )
        self.cdp.on_event = self.on_event
        await self.cdp.connect()
        await self.cdp.call("Browser.getVersion")
        await self.cdp.call(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": self.config["downloadBrowserPath"],
                "eventsEnabled": True,
            },
        )
        # Discovery events do not initialize/auto-attach unrelated pages.
        await self.cdp.call("Target.setDiscoverTargets", {"discover": True})
        return {"connected": True, "engine": "python-cdp"}

    async def page(self, target):
        async with self.page_locks.setdefault(target, asyncio.Lock()):
            if target in self.pages:
                page = self.pages[target]
                if page.closed:
                    raise BrowserError("page_not_bound")
                return page
            page = Page(self, target)
            self.pages[target] = page
            try:
                await page.bind()
                return page
            except BaseException:
                self.pages.pop(target, None)
                self.sessions = {k: v for k, v in self.sessions.items() if v is not page}
                await page.close()
                raise

    async def artifact(self, filename, body):
        if len(body) > self.config["maxArtifactBytes"]:
            raise BrowserError("artifact_too_large", "completed")
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
        return {"artifact": str(filename), "bytes": len(body)}

    async def capture(self, page, params):
        node = None
        try:
            if params.get("target"):
                node, _ = await Actions(page).actionable(
                    params["target"], ["visible", "stable"], pointer=True
                )
            await page.main.run(
                "async function(){if(this.document.fonts)await this.document.fonts.ready}"
            )
            metrics = await self.cdp.call("Page.getLayoutMetrics", session=page.session)
            layout = metrics.get("cssLayoutViewport", metrics["layoutViewport"])
            visual = metrics.get("cssVisualViewport", metrics["visualViewport"])
            dpr = await page.main.run("function(){return this.window.devicePixelRatio}")
            viewport = await page.main.viewport()
            clip = {
                "x": visual["pageX"],
                "y": visual["pageY"],
                "width": viewport["width"],
                "height": viewport["height"],
                "scale": 1 / dpr,
            }
            if node:
                box = (
                    await self.cdp.call(
                        "DOM.getBoxModel", {"objectId": node.object_id}, node.frame.session
                    )
                )["model"]["border"]
                root = page.session_root(node.frame.session)
                points = [
                    await root.to_main({"x": box[i], "y": box[i + 1]}) for i in range(0, 8, 2)
                ]
                left, top = min(p["x"] for p in points), min(p["y"] for p in points)
                clip.update(
                    x=left + layout["pageX"],
                    y=top + layout["pageY"],
                    width=max(p["x"] for p in points) - left,
                    height=max(p["y"] for p in points) - top,
                )
            elif params.get("fullPage"):
                content = metrics.get("cssContentSize", metrics["contentSize"])
                clip.update(x=0, y=0, width=content["width"], height=content["height"])
            result = await self.cdp.call(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": True,
                    "clip": clip,
                },
                page.session,
            )
            artifact = await self.artifact(params["outputPath"], base64.b64decode(result["data"]))
            return {**artifact, "mime": "image/png"}
        finally:
            if node:
                await node.release()

    async def evaluate(self, page, params):
        node = main_node = None
        try:
            mark_effect()
            if params.get("target"):
                node = await page.resolve(params["target"])
                description = await self.cdp.call(
                    "DOM.describeNode", {"objectId": node.object_id}, node.frame.session
                )
                # Resolve in the element's default world, not our isolated helper world.
                remote = await self.cdp.call(
                    "DOM.resolveNode",
                    {
                        "backendNodeId": description["node"]["backendNodeId"],
                    },
                    node.frame.session,
                )
                main_node = Node(node.frame, remote["object"]["objectId"], node.frame.epoch)
                function = (
                    "function(){const value=("
                    + params["expression"]
                    + "\n);return typeof value==='function'?value(this):value}"
                )
                value = await main_node.call(function)
            else:
                source = params["expression"].strip()
                if re.match(r"^(?:async\s+)?function\b", source):
                    source = "(" + source + "\n)"
                expression = (
                    "(()=>{let value=(0,eval)("
                    + json.dumps(source)
                    + ");return typeof value==='function'?value():value})()"
                )
                result = await self.cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                        "userGesture": True,
                        "allowUnsafeEvalBlockedByCSP": True,
                    },
                    page.session,
                )
                value = remote_result(result)
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if len(text) > self.config["snapshotMaxChars"]:
                return await self.artifact(params["outputPath"], text.encode())
            return {"value": value}
        finally:
            if main_node:
                await main_node.release()
            if node:
                await node.release()

    async def execute(self, action, params):
        if action == "initialize":
            return await self.initialize(params)
        if action == "configure":
            for key in ("snapshotMaxChars", "maxEventEntries", "maxBodyBytes", "maxArtifactBytes"):
                self.config[key] = params[key]
            for page in self.pages.values():
                page.observations.configure(params["maxEventEntries"])
            return {"configured": True}
        token = operation.set(Operation.create(params.get("timeoutMs", 20000)))
        try:
            async with asyncio.timeout(operation.get().remaining()):
                page = await self.page(params["targetId"])
                set_phase(action)
                if page.observations.dialog and action not in {"dialog", "console", "network"}:
                    raise BrowserError("dialog_pending: inspect or handle the dialog")
                if action == "prepare":
                    # Private service operation: bind only the newly owned page
                    # and prove its helper execution context works before ready.
                    state = await page.main.run("function(){return this.document.readyState}")
                    if state not in {"interactive", "complete"}:
                        raise BrowserError("page_not_ready", "failed")
                    return {"ready": True, "url": page.url}
                if action == "navigate":
                    return await page.navigate(params)
                if action in {"snapshot", "find"}:
                    return await snapshot(page, action, params)
                if action == "act":
                    return await Actions(page).act(params)
                if action == "wait":
                    return await Actions(page).wait(params)
                if action == "capture":
                    return await self.capture(page, params)
                if action == "evaluate":
                    return await self.evaluate(page, params)
                if action == "files":
                    return await self.files.execute(page, params)
                if action == "console":
                    return page.observations.console.read(params.get("after", 0))
                if action == "network":
                    if params.get("id"):
                        return await page.observations.details(params)
                    return page.observations.network.read(params.get("after", 0))
                if action == "dialog":
                    dialog = page.observations.dialog
                    if not dialog:
                        return {"dialog": None}
                    if params["op"] == "inspect":
                        return {"dialog": {k: v for k, v in dialog.items() if k != "session"}}
                    mark_effect()
                    await self.cdp.call(
                        "Page.handleJavaScriptDialog",
                        {
                            "accept": params["accept"],
                            "promptText": params.get("text", ""),
                        },
                        dialog["session"],
                    )
                    if page.observations.dialog is dialog:
                        page.observations.dialog = None
                    page.observations.dialog_opened.clear()
                    inputs, page.deferred_inputs = page.deferred_inputs, []
                    for method, payload in inputs:
                        await self.cdp.call(method, payload, page.session)
                    guards, page.deferred_guards = page.deferred_guards, []
                    await Actions(page).stop_guards(guards, check=False)
                    return {"handled": True}
                raise BrowserError("unknown_action")
        except DialogOpened:
            dialog = page.observations.dialog
            return {
                "completed": False,
                "pendingDialog": True,
                "url": page.url,
                "dialog": {k: v for k, v in dialog.items() if k != "session"} if dialog else None,
                "downloads": [{"id": d["id"], "name": d["name"]} for d in page.downloads.values()],
            }
        except TimeoutError:
            details = {}
            page = self.pages.get(params.get("targetId"))
            if action == "navigate" and page and page.navigation is not None:
                details["navigation"] = page.navigation_details()
                code = "navigation_timeout"
            else:
                code = "action_timeout"
            raise BrowserError(code, details=details) from None
        except ConnectionError:
            raise BrowserError("browser_connection_lost") from None
        except ProtocolError as exc:
            raise BrowserError("browser_protocol_error") from exc
        finally:
            operation.reset(token)

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.cdp:
            await asyncio.gather(*(p.close() for p in self.pages.values()), return_exceptions=True)
            await self.cdp.close()
