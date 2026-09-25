from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json

from .common import RESOURCES, BrowserError, DialogOpened, ProtocolError, mark_effect, retry_pause
from .frames import Node
from .geometry import clickable_point
from .page import transient

KEYS = json.loads((RESOURCES / "keys.json").read_text())
MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}


class Actions:
    def __init__(self, page):
        self.page, self.cdp = page, page.cdp

    def modifiers(self):
        return sum(MODIFIER_BITS[x] for x in self.page.modifiers)

    async def scroll_node(self, node):
        await self.cdp.call(
            "DOM.scrollIntoViewIfNeeded", {"objectId": node.object_id}, node.frame.session
        )
        frame = node.frame
        while frame.parent:
            owner = await frame.owner()
            try:
                await self.cdp.call(
                    "DOM.scrollIntoViewIfNeeded", {"objectId": owner.object_id}, owner.frame.session
                )
                frame = owner.frame
            finally:
                await owner.release()

    async def actionable(self, target, states, *, pointer=False):
        while True:
            node = await self.page.resolve(target)
            try:
                if pointer:
                    await self.scroll_node(node)
                result = await node.frame.run(
                    "function(node,states){return this.checkElementStates(node,states)}",
                    node,
                    states,
                )
                if result is None:
                    if not pointer:
                        return node, None
                    quads = await self.cdp.call(
                        "DOM.getContentQuads", {"objectId": node.object_id}, node.frame.session
                    )
                    root = self.page.session_root(node.frame.session)
                    vp = await root.viewport()
                    local = clickable_point(quads.get("quads", []), vp["width"], vp["height"])
                    if local:
                        point = await root.to_main(local)
                        if await self.hit_target(node, point):
                            return node, point
            except ProtocolError as exc:
                if not transient(exc):
                    await node.release()
                    raise
            except BaseException:
                await node.release()
                raise
            await node.release()
            await retry_pause()

    async def hit_target(self, node, point):
        current = node
        try:
            while True:
                local = await current.frame.from_main(point)
                result = await current.frame.run(
                    "function(node,point){return this.expectHitTarget(point,node)}", current, local
                )
                if result != "done":
                    return False
                if not current.frame.parent:
                    return True
                owner = await current.frame.owner()
                if current is not node:
                    await current.release()
                current = owner
        finally:
            if current is not node:
                await current.release()

    async def guards(self, node, point, action):
        guards = []
        current = node
        try:
            while True:
                local = await current.frame.from_main(point)
                remote = await current.frame.run(
                    "function(node,args){return this.setupHitTargetInterceptor(node,args.action,args.point,false)}",
                    current,
                    {"point": local, "action": action},
                    by_value=False,
                )
                if not remote.get("objectId"):
                    raise BrowserError("pointer_intercepted")
                guards.append(Node(current.frame, remote["objectId"], current.frame.epoch))
                if not current.frame.parent:
                    return guards
                owner = await current.frame.owner()
                if current is not node:
                    await current.release()
                current = owner
        except BaseException:
            await self.stop_guards(guards, check=False)
            raise
        finally:
            if current is not node:
                await current.release()

    async def stop_guards(self, guards, *, check):
        intercepted = False
        for guard in guards:
            try:
                result = await guard.call("function(){return this.stop()}")
                intercepted |= result != "done"
            except (ProtocolError, BrowserError):
                # Navigation destroys the world after a successful input as well.
                pass
            finally:
                await guard.release()
        if check and intercepted:
            raise BrowserError("pointer_intercepted", "unknown")

    async def mouse(self, kind, point, *, button="none", buttons=0, count=0):
        mark_effect()
        self.page.mouse = point
        await self.page.input(
            "Input.dispatchMouseEvent",
            {
                "type": kind,
                **point,
                "button": button,
                "buttons": buttons,
                "clickCount": count,
                "modifiers": self.modifiers(),
            },
        )

    async def set_modifiers(self, desired):
        desired = set(desired)
        if "ControlOrMeta" in desired:
            desired.remove("ControlOrMeta")
            platform = await self.page.main.run("function(){return this.window.navigator.platform}")
            desired.add("Meta" if platform.startswith("Mac") else "Control")
        modal = False
        for key in sorted(self.page.modifiers - desired):
            try:
                await self.key(key, down=False)
            except DialogOpened:
                # Each unsent release must be queued even while a modal is open.
                modal = True
        if modal:
            raise DialogOpened()
        for key in sorted(desired - self.page.modifiers):
            await self.key(key, down=True)

    def describe_key(self, key):
        aliases = {
            "Control": "ControlLeft",
            "Shift": "ShiftLeft",
            "Alt": "AltLeft",
            "Meta": "MetaLeft",
            "Esc": "Escape",
            "Del": "Delete",
            "Space": "Space",
        }
        code = aliases.get(key, key)
        if code in KEYS:
            row = KEYS[code].copy()
            value = (
                row.get("shiftKey", row["key"]) if "Shift" in self.page.modifiers else row["key"]
            )
        else:
            found = next(
                ((c, r) for c, r in KEYS.items() if key in (r.get("key"), r.get("shiftKey"))), None
            )
            if not found:
                if len(key) == 1:
                    return {
                        "key": key,
                        "code": "",
                        "windowsVirtualKeyCode": 0,
                        "text": key,
                        "location": 0,
                    }
                raise BrowserError("unknown_key")
            code, row = found
            value = key
        return {
            "key": value,
            "code": code,
            "windowsVirtualKeyCode": row.get("keyCodeWithoutLocation", row["keyCode"]),
            "location": row.get("location", 0),
            "text": row.get("text", value if len(value) == 1 else ""),
        }

    async def key(self, key, *, down):
        if key == "ControlOrMeta":
            platform = await self.page.main.run("function(){return this.window.navigator.platform}")
            key = "Meta" if platform.startswith("Mac") else "Control"
        description = self.describe_key(key)
        modifier = description["key"] if description["key"] in MODIFIER_BITS else None
        if modifier:
            if down:
                self.page.modifiers.add(modifier)
            else:
                self.page.modifiers.discard(modifier)
        text = description.pop("text")
        if self.page.modifiers - {"Shift"}:
            text = ""
        payload = {
            **description,
            "type": ("keyDown" if text else "rawKeyDown") if down else "keyUp",
            "modifiers": self.modifiers(),
            "autoRepeat": down and key in self.page.pressed,
            "isKeypad": description["location"] == 3,
        }
        if down:
            payload.update(text=text, unmodifiedText=text)
            self.page.pressed.add(key)
        else:
            self.page.pressed.discard(key)
        mark_effect()
        await self.page.input("Input.dispatchKeyEvent", payload)

    async def press(self, chord):
        # A trailing '+' is itself a key (Control++).
        parts = chord.split("+")
        if not parts[-1] and len(parts) > 1:
            parts = parts[:-2] + ["+"]
        modifiers, key = parts[:-1], parts[-1]
        previous = self.page.modifiers.copy()
        try:
            await self.set_modifiers(previous | set(modifiers))
            await self.key(key, down=True)
            await self.key(key, down=False)
        finally:
            if key in self.page.pressed:
                with contextlib.suppress(Exception):
                    await self.key(key, down=False)
            await self.set_modifiers(previous)

    async def click(self, params):
        hover = params["op"] == "hover"
        node, point = await self.actionable(
            params["target"], ["visible", "stable"] + ([] if hover else ["enabled"]), pointer=True
        )
        guards = []
        previous = self.page.modifiers.copy()
        button = params.get("button", "left")
        bits = {"left": 1, "right": 2, "middle": 4}
        down = False
        try:
            await self.set_modifiers(params.get("modifiers", []))
            guards = await self.guards(node, point, "hover" if hover else "mouse")
            await self.mouse("mouseMoved", point)
            if not hover:
                for count in range(1, 3 if params.get("doubleClick") else 2):
                    down = True
                    await self.mouse(
                        "mousePressed", point, button=button, buttons=bits[button], count=count
                    )
                    down = False
                    await self.mouse("mouseReleased", point, button=button, count=count)
            if self.page.observations.dialog:
                # A modal pauses renderer JS. Stop the interceptors after the explicit
                # dialog handler resumes it, rather than evaluating into the modal.
                self.page.deferred_guards.extend(guards)
            else:
                completed_guards, guards = guards, []
                await self.stop_guards(completed_guards, check=True)
            guards = []
            await self.page.after_input()
        finally:
            if down:
                with contextlib.suppress(Exception):
                    await self.mouse("mouseReleased", point, button=button)
            if self.page.observations.dialog:
                self.page.deferred_guards.extend(guards)
            else:
                await self.stop_guards(guards, check=False)
            await node.release()
            await self.set_modifiers(previous)

    async def fill(self, target, text, *, slowly=False):
        node, _ = await self.actionable(target, ["visible", "enabled", "editable"])
        try:
            mark_effect()
            if slowly:
                result = await node.frame.run("function(node){return this.focusNode(node)}", node)
                if result != "done":
                    raise BrowserError("element_not_editable")
                for char in text:
                    if any(char in (r.get("key"), r.get("shiftKey")) for r in KEYS.values()):
                        await self.press(char)
                    else:
                        await self.page.input("Input.insertText", {"text": char})
                    await asyncio.sleep(0.04)
            else:
                result = await node.frame.run(
                    "function(node,value){return this.fill(node,value)}", node, text
                )
                if result == "needsinput":
                    if text:
                        await self.page.input("Input.insertText", {"text": text})
                    else:
                        await self.press("Delete")
                elif result != "done":
                    raise BrowserError("element_not_editable")
        finally:
            await node.release()

    async def checked(self, target):
        node = await self.page.resolve(target)
        try:
            result = await node.frame.run(
                "function(node){return this.elementState(node,'checked')}", node
            )
            if result.get("received") == "error:notconnected":
                raise BrowserError("element_detached")
            return result["matches"]
        finally:
            await node.release()

    async def drag(self, params):
        source, start = await self.actionable(params["target"], ["visible", "stable"], pointer=True)
        target = None
        self.page.drag_data = None
        released = True
        try:
            await self.cdp.call("Input.setInterceptDrags", {"enabled": True}, self.page.session)
            await self.mouse("mouseMoved", start)
            released = False
            await self.mouse("mousePressed", start, button="left", buttons=1, count=1)
            target, end = await self.actionable(params["to"], ["visible", "stable"], pointer=True)
            for step in range(1, 9):
                point = {k: start[k] + (end[k] - start[k]) * step / 8 for k in ("x", "y")}
                await self.mouse("mouseMoved", point, button="left", buttons=1)
            await self.cdp.call("Page.enable", session=self.page.session)
            if self.page.drag_data:
                for kind in ("dragEnter", "dragOver", "drop"):
                    mark_effect()
                    await self.cdp.call(
                        "Input.dispatchDragEvent",
                        {
                            "type": kind,
                            **end,
                            "data": self.page.drag_data,
                            "modifiers": self.modifiers(),
                        },
                        self.page.session,
                    )
            # The release may have happened even if its acknowledgement is lost.
            released = True
            await self.mouse("mouseReleased", end, button="left", count=1)
            await self.page.after_input()
        finally:
            if not released:
                with contextlib.suppress(Exception):
                    await self.mouse("mouseReleased", self.page.mouse, button="left")
            with contextlib.suppress(Exception):
                await self.cdp.call(
                    "Input.setInterceptDrags", {"enabled": False}, self.page.session, timeout=0.3
                )
            await source.release()
            if target:
                await target.release()

    async def act(self, params):
        op = params["op"]
        if op in {"click", "hover"}:
            await self.click(params)
        elif op in {"fill", "type"}:
            fields = params.get("fields") or [
                {"target": params["target"], "value": params.get("text", "")}
            ]
            for field in fields:
                await self.fill(
                    field["target"],
                    field["value"],
                    slowly=op == "type" and params.get("slowly", False),
                )
            if op == "type" and params.get("submit"):
                await self.press("Enter")
                await self.page.after_input()
        elif op == "press":
            if params.get("target"):
                node = await self.page.resolve(params["target"])
                try:
                    await node.frame.run("function(node){return this.focusNode(node)}", node)
                finally:
                    await node.release()
            await self.press(params["key"])
            await self.page.after_input()
        elif op == "check":
            if await self.checked(params["target"]) != params["checked"]:
                await self.click({"op": "click", "target": params["target"]})
                if await self.checked(params["target"]) != params["checked"]:
                    raise BrowserError("checked_state_not_changed", "completed")
        elif op == "select":
            while True:
                node, _ = await self.actionable(params["target"], ["visible", "enabled"])
                try:
                    mark_effect()
                    result = await node.frame.run(
                        "function(node,values){return this.selectOptions(node,values.map(value=>({valueOrLabel:value})))}",
                        node,
                        params["values"],
                    )
                finally:
                    await node.release()
                if isinstance(result, list):
                    break
                if result not in {
                    "error:optionsnotfound",
                    "error:optionnotenabled",
                    "error:notconnected",
                }:
                    raise BrowserError("select_failed")
                await retry_pause()
        elif op == "drag":
            await self.drag(params)
        elif op == "scroll":
            mark_effect()
            if params.get("target"):
                node = await self.page.resolve(params["target"])
                try:
                    await self.scroll_node(node)
                finally:
                    await node.release()
            else:
                await self.cdp.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        **self.page.mouse,
                        "deltaX": params.get("x", 0),
                        "deltaY": params.get("y", 0),
                    },
                    self.page.session,
                )
        elif op == "resize":
            mark_effect()
            await self.cdp.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": params["width"],
                    "height": params["height"],
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
                self.page.session,
            )
        elif op == "emulate":
            mapping = {
                "colorScheme": "prefers-color-scheme",
                "reducedMotion": "prefers-reduced-motion",
                "forcedColors": "forced-colors",
                "contrast": "prefers-contrast",
            }
            media = self.page.__dict__.setdefault("media", {})
            media.update(params["media"])
            mark_effect()
            await self.cdp.call(
                "Emulation.setEmulatedMedia",
                {
                    "media": media.get("media") or "",
                    "features": [
                        {"name": name, "value": media.get(key) or ""}
                        for key, name in mapping.items()
                    ],
                },
                self.page.session,
            )
        else:
            raise BrowserError("unknown_act_operation")
        dialog = self.page.observations.dialog
        return {
            "completed": True,
            "url": self.page.url,
            "dialog": {k: dialog[k] for k in ("type", "message")} if dialog else None,
            "downloads": [
                {"id": row["id"], "name": row["name"]} for row in self.page.downloads.values()
            ],
        }

    async def wait(self, params):
        if "timeMs" in params:
            await asyncio.sleep(params["timeMs"] / 1000)
            return {"ready": True}
        state = params.get("state", "visible")
        while True:
            if "url" in params:
                if fnmatch.fnmatchcase(self.page.url, params["url"]):
                    return {"ready": True}
            else:
                node = None
                try:
                    if params.get("target"):
                        node = await self.page.resolve(params["target"], wait=False)
                    else:
                        selector = "internal:text=" + json.dumps(params["text"]) + "i >> nth=0"
                        node = await self.page.main.query(selector)
                    if state in {"attached", "detached"}:
                        matches = bool(node) == (state == "attached")
                    elif not node:
                        matches = state == "hidden"
                    else:
                        result = await node.frame.run(
                            "function(node,state){return this.elementState(node,state)}",
                            node,
                            state,
                        )
                        matches = result["matches"]
                    if matches:
                        return {"ready": True}
                except ProtocolError as exc:
                    if not transient(exc):
                        raise
                finally:
                    if node:
                        await node.release()
            await retry_pause()
