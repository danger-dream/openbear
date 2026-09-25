from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid

from .common import BrowserError, DialogOpened, ProtocolError, mark_effect, retry_pause, set_phase
from .frames import Frame, remote_result
from .observations import Observations


def transient(error):
    text = str(error).lower()
    return any(
        x in text
        for x in (
            "context",
            "node",
            "frame_not_ready",
            "frame_detached",
            "document_changed",
            "cannot find",
            "target closed",
        )
    )


class Page:
    def __init__(self, engine, target):
        self.engine, self.cdp, self.target = engine, engine.cdp, target
        self.session = ""
        self.main_id = ""
        self.frames = {}
        self.session_roots = {}
        self.setup_tasks = {}
        self.failed_sessions = set()
        self.snapshot = ""
        self.refs = {}
        self.life = {}
        self.navigation_status = {}
        self.navigation = None
        self.navigation_failed = asyncio.Event()
        self.pending_navigation = False
        self.navigation_serial = 0
        self.closed = False
        self.error_binding = "__obError_" + uuid.uuid4().hex
        self.observations = Observations(self)
        self.downloads = {}
        self.download_seq = 0
        self.drag_data = None
        self.deferred_guards = []
        self.deferred_inputs = []
        self.mouse = {"x": 0.0, "y": 0.0}
        self.modifiers = set()
        self.pressed = set()
        self.scripts = {}
        self.observer_sessions = {}
        self.binding_locks = {}
        self.binding_tasks = {}

    @property
    def main(self):
        frame = self.frames.get(self.main_id)
        if not frame:
            raise ProtocolError("frame_not_ready")
        return frame

    @property
    def url(self):
        return self.frames[self.main_id].url if self.main_id in self.frames else ""

    def invalidate_refs(self):
        self.snapshot = ""
        self.refs.clear()

    def session_root(self, session):
        frame = self.frames.get(self.session_roots.get(session))
        if not frame:
            raise ProtocolError("frame_not_ready")
        return frame

    def update_frame(self, data, sid, parent=None):
        fid = data["id"]
        frame = self.frames.get(fid)
        if frame is None:
            frame = self.frames[fid] = Frame(self, fid)
        frame.detached = False
        if parent is not None or data.get("parentId"):
            frame.parent = parent if parent is not None else data["parentId"]
        if frame.session and frame.session != sid:
            frame.invalidate()
        frame.session = sid
        if data.get("loaderId") and frame.loader != data["loaderId"]:
            frame.invalidate()
            frame.loader = data["loaderId"]
        frame.url = data.get("url", frame.url)
        return frame

    def update_tree(self, tree, sid, parent=None):
        frame = self.update_frame(tree["frame"], sid, parent)
        for child in tree.get("childFrames", []):
            self.update_tree(child, sid, frame.id)

    async def bind(self):
        result = await self.cdp.call(
            "Target.attachToTarget", {"targetId": self.target, "flatten": True}
        )
        self.session = result["sessionId"]
        self.engine.sessions[self.session] = self
        await self.setup_session(self.session, self.target, main=True)

    async def setup_session(self, sid, target, main=False):
        try:
            await self.cdp.call("Page.enable", session=sid)
            tree = (await self.cdp.call("Page.getFrameTree", session=sid))["frameTree"]
            root = tree["frame"]["id"]
            self.session_roots[sid] = root
            if main:
                self.main_id = root
            self.update_tree(tree, sid)
            await asyncio.gather(
                self.cdp.call("Page.setLifecycleEventsEnabled", {"enabled": True}, sid),
                self.cdp.call("Runtime.enable", session=sid),
                self.cdp.call(
                    "Network.enable",
                    {
                        "maxTotalBufferSize": max(
                            32 * 1024 * 1024, self.engine.config["maxBodyBytes"] * 2
                        ),
                        "maxResourceBufferSize": self.engine.config["maxBodyBytes"] + 1024 * 1024,
                    },
                    sid,
                ),
                self.cdp.call("Page.setInterceptFileChooserDialog", {"enabled": True}, sid),
            )
            # Keep observation off the Runtime-enabled control session: CloakBrowser
            # suppresses some Runtime events there after navigation.
            observer = (
                await self.cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})
            )["sessionId"]
            self.observer_sessions[sid] = observer
            self.engine.sessions[observer] = self
            await self.cdp.call("Console.enable", session=observer)
            self.observations.console_ready.add(observer)
            await self.cdp.call("Runtime.addBinding", {"name": self.error_binding}, observer)
            name = json.dumps(self.error_binding)
            source = (
                "(()=>{const name="
                + name
                + ";const key=name+'_installed';if(globalThis[key])return;"
                "const pending=[];const flush=()=>{if(typeof globalThis[name]!=='function')return;"
                "while(pending.length){try{globalThis[name](pending.shift())}catch{break}}};"
                "globalThis[key]={flush};const send=text=>{pending.push(JSON.stringify({text:String(text).slice(0,8000)}));"
                "if(pending.length>200)pending.shift();flush()};"
                "addEventListener('error',e=>send(e.message));"
                "addEventListener('unhandledrejection',e=>send(e.reason?.stack||e.reason));})()"
            )
            result = await self.cdp.call(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": source,
                    "runImmediately": True,
                },
                sid,
            )
            self.scripts[sid] = result["identifier"]
            # Only descendants of this owned page/session; never browser-root auto attach.
            await self.cdp.call(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": False,
                    "flatten": True,
                    "filter": [{"type": "iframe", "exclude": False}, {"exclude": True}],
                },
                sid,
            )
        except BaseException:
            self.failed_sessions.add(sid)
            raise

    async def refresh_binding(self, sid):
        observer = self.observer_sessions.get(sid)
        if not observer:
            return
        async with self.binding_locks.setdefault(sid, asyncio.Lock()):
            # Cloak can drop the injected binding function at navigation while retaining
            # the new-document listeners. Reinstall and flush their bounded early errors.
            await self.cdp.call("Runtime.addBinding", {"name": self.error_binding}, observer)
            key = json.dumps(self.error_binding + "_installed")
            expression = (
                "(()=>{const visit=w=>{try{w["
                + key
                + "]?.flush();for(const f of w.frames)visit(f)}catch{}};visit(globalThis)})()"
            )
            await self.cdp.call("Runtime.evaluate", {"expression": expression}, observer)

    def event(self, method, data, sid):
        if self.closed:
            return
        self.observations.event(method, data, sid)
        if method == "Target.attachedToTarget":
            if data["targetInfo"]["type"] != "iframe":
                return
            child = data["sessionId"]
            self.engine.sessions[child] = self
            task = self.engine.spawn(self.setup_session(child, data["targetInfo"]["targetId"]))
            self.setup_tasks[child] = task
        elif method == "Target.detachedFromTarget":
            child = data["sessionId"]
            self.engine.sessions.pop(child, None)
            self.session_roots.pop(child, None)
            for frame in self.frames.values():
                if frame.session == child:
                    frame.invalidate()
                    frame.session = ""
        elif method == "Page.frameAttached":
            fid = data["frameId"]
            if fid not in self.frames:
                self.frames[fid] = Frame(self, fid, data["parentFrameId"], sid)
            else:
                self.frames[fid].parent = data["parentFrameId"]
        elif method == "Page.frameNavigated":
            frame = self.update_frame(data["frame"], sid)
            if frame.id == self.main_id:
                self.navigation_serial += 1
                if self.navigation and self.navigation["state"] in {"requested", "committed"}:
                    loader = self.navigation.get("loaderId")
                    if not loader or loader == frame.loader:
                        self.navigation.update(
                            state="committed", committed=True, loaderId=frame.loader
                        )
        elif method == "Page.navigatedWithinDocument":
            if frame := self.frames.get(data["frameId"]):
                frame.url = data["url"]
                if frame.id == self.main_id:
                    self.pending_navigation = False
                    self.navigation_serial += 1
        elif method == "Page.frameDetached":
            if frame := self.frames.get(data["frameId"]):
                frame.invalidate()
                if data.get("reason") != "swap":
                    removed = {frame.id}
                    while descendants := {
                        f.id
                        for f in self.frames.values()
                        if f.parent in removed and f.id not in removed
                    }:
                        removed.update(descendants)
                    for fid in removed:
                        descendant = self.frames[fid]
                        if descendant is not frame:
                            descendant.invalidate()
                        descendant.detached = True
        elif method == "Page.frameRequestedNavigation" and data.get("frameId") == self.main_id:
            if data.get("disposition") == "currentTab":
                self.pending_navigation = True
        elif method == "Page.frameStartedLoading" and data.get("frameId") == self.main_id:
            self.pending_navigation = True
        elif method == "Page.frameStoppedLoading" and data.get("frameId") == self.main_id:
            self.pending_navigation = False
        elif method == "Page.lifecycleEvent":
            if data["name"] == "DOMContentLoaded" and sid in self.observer_sessions:
                self.binding_tasks[sid] = self.engine.spawn(self.refresh_binding(sid))
            key = (data["frameId"], data["loaderId"])
            self.life.setdefault(key, set()).add(data["name"])
            if data["frameId"] == self.main_id and data["name"] in {"DOMContentLoaded", "load"}:
                self.pending_navigation = False
            while len(self.life) > 100:
                self.life.pop(next(iter(self.life)))
            while len(self.navigation_status) > 100:
                self.navigation_status.pop(next(iter(self.navigation_status)))
        elif method == "Runtime.executionContextsCleared":
            for frame in self.frames.values():
                if frame.session == sid:
                    frame.invalidate()
        elif method == "Runtime.executionContextDestroyed":
            for frame in self.frames.values():
                if frame.session == sid and frame.context == data.get("executionContextId"):
                    frame.invalidate()
        elif method == "Input.dragIntercepted":
            self.drag_data = data["data"]
        elif method == "Inspector.targetCrashed":
            self.invalidate_refs()

    async def resolve(self, target, *, wait=True):
        if target.startswith("css="):
            selector, frame, expected = target, self.main, None
        else:
            prefix = self.snapshot + ":"
            if (
                not self.snapshot
                or not target.startswith(prefix)
                or target[len(prefix) :] not in self.refs
            ):
                raise BrowserError("stale_ref: take a new snapshot")
            ref = target[len(prefix) :]
            fid, expected = self.refs[ref]
            frame = self.frames.get(fid)
            if not frame or frame.epoch != expected or frame.detached:
                raise BrowserError("stale_ref: take a new snapshot")
            selector = "aria-ref=" + ref
        while True:
            try:
                node = await frame.query(selector)
                if node and not await node.call("function(){return this.isConnected}"):
                    await node.release()
                    node = None
                if node:
                    return node
                if not wait:
                    return None
                if expected is not None:
                    raise BrowserError("stale_ref: take a new snapshot")
            except ProtocolError as exc:
                if expected is not None:
                    raise BrowserError("stale_ref: take a new snapshot") from None
                if not transient(exc):
                    raise
                frame.helper = frame.context = None
            await retry_pause()

    async def main_evaluate(self, expression):
        result = await self.cdp.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
            self.session,
        )
        return remote_result(result)

    async def input(self, method, params):
        if self.observations.dialog:
            if params.get("type") in {"mouseReleased", "keyUp"}:
                self.deferred_inputs.append((method, params))
            raise DialogOpened()
        command = asyncio.create_task(self.cdp.call(method, params, self.session))
        modal = asyncio.create_task(self.observations.dialog_opened.wait())
        try:
            done, _ = await asyncio.wait({command, modal}, return_when=asyncio.FIRST_COMPLETED)
            if command in done:
                result = await command
                if self.observations.dialog:
                    raise DialogOpened()
                return result
            # The input was sent once; the concrete dialog event is its acknowledgement.
            # Cancel only local waiting, never repeat that input after handling the dialog.
            raise DialogOpened()
        finally:
            for task in (command, modal):
                if not task.done():
                    task.cancel()
            await asyncio.gather(command, modal, return_exceptions=True)

    async def after_input(self):
        if self.observations.dialog:
            return
        await self.cdp.call("Page.enable", session=self.session)
        while self.pending_navigation and not self.observations.dialog:
            await retry_pause()
        if task := self.binding_tasks.get(self.session):
            await asyncio.shield(task)

    def navigation_details(self):
        nav = self.navigation or {}
        return {
            key: nav[key]
            for key in ("state", "committed", "loaderId", "networkError")
            if key in nav
        }

    def fail_navigation(self, error):
        if self.navigation is None:
            return
        # Preserve the useful Chromium error code, not arbitrary protocol text/URLs.
        match = re.search(r"net::ERR_[A-Z0-9_]+", str(error))
        self.navigation.update(
            state="failed", networkError=match[0] if match else "network_failure"
        )
        self.navigation_failed.set()
        self.pending_navigation = False

    def check_navigation_failure(self):
        if self.navigation_failed.is_set():
            raise BrowserError(
                "navigation_failed", "failed", details={"navigation": self.navigation_details()}
            )

    async def navigation_command(self, method, params=None):
        set_phase("navigation_response")
        mark_effect()
        command = asyncio.create_task(self.cdp.call(method, params, self.session))
        failed = asyncio.create_task(self.navigation_failed.wait())
        try:
            await asyncio.wait({command, failed}, return_when=asyncio.FIRST_COMPLETED)
            self.check_navigation_failure()
            result = await command
            if result.get("errorText"):
                self.fail_navigation(result["errorText"])
                self.check_navigation_failure()
            return result
        finally:
            for task in (command, failed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(command, failed, return_exceptions=True)

    async def navigate(self, params):
        op = params.get("op", "goto")
        self.navigation = {"state": "requested", "committed": False}
        self.navigation_failed.clear()
        if op == "back":
            set_phase("navigation_history")
            history = await self.cdp.call("Page.getNavigationHistory", session=self.session)
            if history["currentIndex"] == 0:
                self.navigation["state"] = "completed"
                return {"url": self.url, "statusCode": None}
            self.pending_navigation = True
            await self.navigation_command(
                "Page.navigateToHistoryEntry",
                {
                    "entryId": history["entries"][history["currentIndex"] - 1]["id"],
                },
            )
        elif op == "reload":
            self.pending_navigation = True
            await self.navigation_command("Page.reload")
        else:
            result = await self.navigation_command("Page.navigate", {"url": params["url"]})
            loader = result.get("loaderId")
            if result.get("isDownload"):
                self.navigation["state"] = "download"
            elif loader:
                self.navigation["loaderId"] = loader
                set_phase("dom_content_loaded")
                while "DOMContentLoaded" not in self.life.get((result["frameId"], loader), set()):
                    self.check_navigation_failure()
                    if self.observations.dialog:
                        break
                    await retry_pause()
        if op in {"back", "reload"}:
            set_phase("dom_content_loaded")
            while self.pending_navigation and not self.observations.dialog:
                self.check_navigation_failure()
                await retry_pause()
        self.check_navigation_failure()
        set_phase("navigation_bindings")
        if task := self.binding_tasks.get(self.session):
            await asyncio.shield(task)
        if self.navigation["state"] != "download":
            self.navigation["state"] = "dialog" if self.observations.dialog else "completed"
        return {
            "url": self.url,
            "statusCode": self.navigation_status.get((self.main_id, self.main.loader)),
        }

    async def close(self):
        self.closed = True
        tasks = {
            task
            for task in (*self.setup_tasks.values(), *self.binding_tasks.values())
            if task is not asyncio.current_task() and not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for observer in list(self.observer_sessions.values()):
            with contextlib.suppress(Exception):
                await self.cdp.call(
                    "Runtime.removeBinding", {"name": self.error_binding}, observer, timeout=0.2
                )
                await self.cdp.call("Target.detachFromTarget", {"sessionId": observer}, timeout=0.2)
        # Initialization can fail before getFrameTree registers the root session.
        sessions = set(self.session_roots) | set(self.setup_tasks)
        if self.session:
            sessions.add(self.session)
        for sid in sessions:
            with contextlib.suppress(Exception):
                if sid in self.scripts:
                    await self.cdp.call(
                        "Page.removeScriptToEvaluateOnNewDocument",
                        {"identifier": self.scripts[sid]},
                        sid,
                        timeout=0.2,
                    )
                await self.cdp.call(
                    "Runtime.removeBinding", {"name": self.error_binding}, sid, timeout=0.2
                )
                await self.cdp.call("Target.detachFromTarget", {"sessionId": sid}, timeout=0.2)
