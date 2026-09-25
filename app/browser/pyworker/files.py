from __future__ import annotations

import asyncio
import base64
import contextlib
import mimetypes
import os
import uuid
from pathlib import Path

from .common import BrowserError, mark_effect, retry_pause


class Files:
    def __init__(self, engine):
        self.engine = engine
        self.by_guid = {}

    def event(self, method, params):
        if method == "Browser.downloadWillBegin":
            page = next(
                (p for p in self.engine.pages.values() if params["frameId"] in p.frames), None
            )
            if not page:
                return
            page.download_seq += 1
            record = {
                "id": page.download_seq,
                "name": params["suggestedFilename"],
                "guid": params["guid"],
                "state": "inProgress",
                "page": page,
                "received": 0,
            }
            page.downloads[record["id"]] = record
            self.by_guid[record["guid"]] = record
            while len(page.downloads) > 20:
                old = page.downloads.pop(next(iter(page.downloads)))
                self.by_guid.pop(old["guid"], None)
                if old["state"] == "inProgress":
                    self.engine.spawn(self.cancel(old))
        elif method == "Browser.downloadProgress":
            if record := self.by_guid.get(params["guid"]):
                record["state"] = params["state"]
                record["received"] = params.get("receivedBytes", 0)
                if (
                    record["received"] > self.engine.config["maxArtifactBytes"]
                    and record["state"] == "inProgress"
                ):
                    record["tooLarge"] = True
                    self.engine.spawn(self.cancel(record))

    async def cancel(self, record):
        if record["state"] == "inProgress":
            await self.engine.cdp.call("Browser.cancelDownload", {"guid": record["guid"]})
            record["state"] = "canceled"

    async def upload(self, page, params):
        drop = params["op"] == "drop"
        if params.get("target"):
            node = await page.resolve(params["target"])
        else:
            chooser = page.observations.chooser
            if not chooser:
                raise BrowserError("file_chooser_not_open")
            frame = page.frames.get(chooser["frameId"])
            if not frame or frame.detached:
                raise BrowserError("file_chooser_expired")
            node = await frame.resolve(chooser["backendNodeId"])
        transfer = uuid.uuid4().hex
        paths = [Path(p) for p in params.get("paths", [])]
        limit = self.engine.config["maxArtifactBytes"]
        try:
            if any(not p.is_file() for p in paths):
                raise BrowserError("upload_path_not_file")
            if sum(p.stat().st_size for p in paths) > limit:
                raise BrowserError("upload_too_large")
            await node.frame.run("function(id){this.__startFiles(id)}", transfer)
            total = 0
            for path in paths:
                meta = {
                    "name": path.name,
                    "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    "lastModified": int(path.stat().st_mtime * 1000),
                }
                await node.frame.run("function(id,meta){this.__startFile(id,meta)}", transfer, meta)
                with path.open("rb") as stream:
                    while chunk := stream.read(256 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise BrowserError("upload_too_large")
                        await node.frame.run(
                            "function(id,chunk){this.__fileChunk(id,chunk)}",
                            transfer,
                            base64.b64encode(chunk).decode(),
                        )
            mark_effect()
            await node.frame.run(
                "function(node,args){this.__finishFiles(node,args)}",
                node,
                {"id": transfer, "drop": drop, "data": params.get("data", {})},
            )
            if not drop:
                page.observations.chooser = None
            return {"dropped" if drop else "uploaded": True if drop else len(paths)}
        finally:
            with contextlib.suppress(Exception):
                await node.frame.run("function(id){this.__fileTransfers.delete(id)}", transfer)
            await node.release()

    async def execute(self, page, params):
        op = params["op"]
        if op in {"upload", "drop"}:
            return await self.upload(page, params)
        if op == "list":
            return {
                "downloads": [
                    {"id": row["id"], "name": row["name"]} for row in page.downloads.values()
                ]
            }
        record = page.downloads.get(params["id"])
        if not record:
            raise BrowserError("download_expired")
        if op == "cancel":
            mark_effect()
            await self.cancel(record)
            return {"cancelled": True}
        while record["state"] == "inProgress":
            await retry_pause()
        if record.get("tooLarge"):
            raise BrowserError("artifact_too_large")
        if record["state"] != "completed":
            raise BrowserError("download_cancelled")
        source = Path(self.engine.config["downloadHostPath"]) / record["guid"]
        if not source.is_file():
            raise BrowserError(
                "download_file_unavailable: configure shared download paths for a remote browser"
            )
        dest = Path(params["outputPath"])
        written = 0
        try:
            with source.open("rb") as reader, dest.open("wb") as writer:
                os.chmod(dest, 0o600)
                while chunk := reader.read(256 * 1024):
                    written += len(chunk)
                    if written > self.engine.config["maxArtifactBytes"]:
                        raise BrowserError("artifact_too_large")
                    writer.write(chunk)
                    await asyncio.sleep(0)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return {"artifact": str(dest), "bytes": written, "name": record["name"]}
