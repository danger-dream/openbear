from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass

from .common import RESOURCES, BrowserError, ProtocolError
from .geometry import transform, viewport_quad

INJECTED = (RESOURCES / "injected.js").read_text()
BRIDGE = (RESOURCES / "bridge.js").read_text()


def argument(value):
    return {"objectId": value.object_id} if isinstance(value, Node) else {"value": value}


def remote_result(result, by_value=True):
    if "exceptionDetails" in result:
        description = result["exceptionDetails"].get("exception", {}).get("description", "")
        if "strict mode violation" in description:
            raise BrowserError("strict_selector_violation")
        if "aria-ref" in description and ("not found" in description or "snapshot" in description):
            raise BrowserError("stale_ref: take a new snapshot")
        raise BrowserError("javascript_error")
    remote = result.get("result", {})
    if by_value:
        return remote.get("value")
    return remote


@dataclass
class Node:
    frame: Frame
    object_id: str
    epoch: int

    async def call(self, function, *args, by_value=True):
        if self.epoch != self.frame.epoch:
            raise BrowserError("stale_ref: take a new snapshot")
        return await self.frame.call_object(self.object_id, function, args, by_value)

    async def release(self):
        with contextlib.suppress(Exception):
            await self.frame.page.cdp.call(
                "Runtime.releaseObject",
                {"objectId": self.object_id},
                self.frame.session,
                timeout=0.25,
            )


class Frame:
    def __init__(self, page, frame_id, parent="", session=""):
        self.page, self.id, self.parent, self.session = page, frame_id, parent, session
        self.seq = page.engine.next_frame_seq()
        self.epoch = 0
        self.url = ""
        self.loader = ""
        self.context = None
        self.helper = None
        self.lock = asyncio.Lock()
        self.detached = False

    def invalidate(self):
        self.epoch += 1
        self.context = self.helper = None
        self.page.invalidate_refs()

    async def ready(self):
        if self.detached or not self.session:
            raise ProtocolError("frame_not_ready")
        async with self.lock:
            if self.helper:
                return
            epoch = self.epoch
            context = await self.page.cdp.call(
                "Page.createIsolatedWorld",
                {
                    "frameId": self.id,
                    "worldName": "openbear-browser-utility",
                },
                self.session,
            )
            options = {
                "isUnderTest": False,
                "sdkLanguage": "python",
                "frameSeq": self.seq,
                "testIdAttributeName": "data-testid",
                "stableRafCount": 1,
                "browserName": "chromium",
                "shouldPrependErrorPrefix": False,
                "isUtilityWorld": True,
                "customEngines": [],
            }
            source = (
                "(() => {const module={};"
                + INJECTED
                + ";const h=new (module.exports.InjectedScript())(globalThis,"
                + json.dumps(options)
                + ");"
                + BRIDGE
                + ";return h;})()"
            )
            result = await self.page.cdp.call(
                "Runtime.evaluate",
                {
                    "expression": source,
                    "contextId": context["executionContextId"],
                    "returnByValue": False,
                },
                self.session,
            )
            remote = remote_result(result, False)
            if epoch != self.epoch:
                raise ProtocolError("document_changed")
            self.context, self.helper = context["executionContextId"], remote["objectId"]

    async def call_object(self, object_id, function, args=(), by_value=True):
        result = await self.page.cdp.call(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": function,
                "arguments": [argument(a) for a in args],
                "awaitPromise": True,
                "returnByValue": by_value,
                "userGesture": True,
            },
            self.session,
        )
        return remote_result(result, by_value)

    async def run(self, function, *args, by_value=True):
        await self.ready()
        return await self.call_object(self.helper, function, args, by_value)

    async def query(self, selector):
        remote = await self.run(
            "function(selector){return this.__query(selector)}", selector, by_value=False
        )
        if not remote.get("objectId") or remote.get("subtype") == "null":
            return None
        return Node(self, remote["objectId"], self.epoch)

    async def resolve(self, backend_node):
        await self.ready()
        result = await self.page.cdp.call(
            "DOM.resolveNode",
            {
                "backendNodeId": backend_node,
                "executionContextId": self.context,
            },
            self.session,
        )
        if not result["object"].get("objectId"):
            raise ProtocolError("node_detached")
        return Node(self, result["object"]["objectId"], self.epoch)

    async def viewport(self):
        return await self.run(
            "function(){return {width:this.window.innerWidth,height:this.window.innerHeight}}"
        )

    async def owner(self):
        if not self.parent or self.parent not in self.page.frames:
            raise ProtocolError("frame_detached")
        parent = self.page.frames[self.parent]
        owner = await self.page.cdp.call("DOM.getFrameOwner", {"frameId": self.id}, parent.session)
        return await parent.resolve(owner["backendNodeId"])

    async def global_quad(self):
        owner = await self.owner()
        try:
            model = await self.page.cdp.call(
                "DOM.getBoxModel", {"objectId": owner.object_id}, owner.frame.session
            )
            quad = model["model"]["content"]
            # CDP geometry is relative to this session's local root, not every DOM frame.
            root = self.page.session_root(owner.frame.session)
            if root.id == self.page.main_id:
                return quad
            result = []
            for i in range(0, 8, 2):
                point = await root.to_main({"x": quad[i], "y": quad[i + 1]})
                result.extend((point["x"], point["y"]))
            return result
        finally:
            await owner.release()

    async def to_main(self, point):
        if self.id == self.page.main_id:
            return point
        vp, quad = await self.viewport(), await self.global_quad()
        return transform(point, viewport_quad(vp["width"], vp["height"]), quad)

    async def from_main(self, point):
        if self.id == self.page.main_id:
            return point
        vp, quad = await self.viewport(), await self.global_quad()
        return transform(point, quad, viewport_quad(vp["width"], vp["height"]))
