from __future__ import annotations

import asyncio
import re
import uuid

from .common import BrowserError, ProtocolError, operation


async def snapshot(page, action, params):
    refs = {}
    warnings = []
    epochs = {}

    async def collect(frame, root=None, depth=None):
        epochs[frame.id] = frame.epoch
        options = {"mode": "ai", "boxes": params.get("boxes", False)}
        if depth is not None:
            options["depth"] = depth
        result = await frame.run(
            "function(node,opts){return this.ariaSnapshotJSON(node || this.document.body || this.document.documentElement,opts)}",
            root,
            options,
        )
        data = result["json"]

        def index(nodes):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("ref"):
                    refs[node["ref"]] = (frame.id, frame.epoch)
                index(node.get("children", []))

        index(data)
        children = {}
        for ref in result["iframeRefs"]:
            if ref not in result["iframeDepths"]:
                continue
            child_depth = depth - result["iframeDepths"][ref] - 1 if depth is not None else None
            if child_depth is not None and child_depth < 0:
                continue
            owner = None
            try:
                async with asyncio.timeout(min(2, operation.get().remaining())):
                    owner = await frame.query("aria-ref=" + ref)
                    if not owner:
                        continue
                    description = await page.cdp.call(
                        "DOM.describeNode", {"objectId": owner.object_id}, frame.session
                    )
                    fid = description["node"].get("frameId") or description["node"].get(
                        "contentDocument", {}
                    ).get("frameId")
                    child = page.frames.get(fid)
                    if child is None or child.detached:
                        raise ProtocolError("frame_detached")
                    if task := page.setup_tasks.get(child.session):
                        await asyncio.shield(task)
                    children[ref] = await collect(child, depth=child_depth)
            except (ProtocolError, BrowserError, TimeoutError, ConnectionError):
                warnings.append({"frameRef": ref, "error": "frame_snapshot_unavailable"})
            finally:
                if owner:
                    await owner.release()

        def merge(nodes):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("role") == "iframe" and node.get("ref") in children:
                    node["children"] = children[node["ref"]]
                else:
                    merge(node.get("children", []))

        merge(data)
        return data

    root = await page.resolve(params["target"]) if params.get("target") else None
    try:
        frame = root.frame if root else page.main
        data = await collect(frame, root, params.get("depth"))
        if any(page.frames[fid].epoch != epoch for fid, epoch in epochs.items()):
            raise BrowserError("document_changed: take a new snapshot")
        text = await frame.run("function(data){return this.__renderSnapshot(data)}", data)
    finally:
        if root:
            await root.release()
    if params.get("interactive"):
        text = "\n".join(
            line for line in text.split("\n") if re.search(r"\[ref=|heading|iframe", line)
        )
    if action == "find":
        lines = text.split("\n")
        selected = set()
        needle = params["text"].lower()
        for i, line in enumerate(lines):
            if needle in line.lower():
                selected.update(range(max(0, i - 2), min(len(lines), i + 3)))
        text = "\n".join(lines[i] for i in sorted(selected))
    version = "s" + uuid.uuid4().hex[:8]
    text = re.sub(r"\[ref=([^\]]+)\]", lambda match: f"[ref={version}:{match[1]}]", text)
    limit = min(
        params.get("maxChars", page.engine.config["snapshotMaxChars"]),
        page.engine.config["snapshotMaxChars"],
    )
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
        if "\n" in text:
            text = text.rsplit("\n", 1)[0]
        elif not text.endswith("]"):
            text = ""
    visible = set(re.findall(r"\[ref=[^:]+:([^\]]+)\]", text))
    page.snapshot = version
    page.refs = {ref: value for ref, value in refs.items() if ref in visible}
    result = {"snapshotId": version, "text": text, "truncated": truncated, "url": page.url}
    if warnings:
        result["warnings"] = warnings
    return result
