from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict

from .common import BrowserError, Ring, retry_pause


class Observations:
    def __init__(self, page):
        self.page = page
        limit = page.engine.config["maxEventEntries"]
        self.console, self.network = Ring(limit), Ring(limit)
        self.records = OrderedDict()
        self.current = {}
        self.pending_extra = OrderedDict()
        self.dialog = None
        self.dialog_opened = asyncio.Event()
        self.chooser = None
        self.console_ready = set()

    def configure(self, limit):
        self.console.trim(limit)
        self.network.trim(limit)
        while len(self.records) > limit:
            self.records.popitem(last=False)
        self.current = {key: rid for key, rid in self.current.items() if rid in self.records}
        while len(self.pending_extra) > limit:
            self.pending_extra.popitem(last=False)

    def event(self, method, data, sid):
        if method == "Console.messageAdded" and sid in self.console_ready:
            message = data["message"]
            if message.get("source") == "console-api":
                self.console.add(
                    {"level": message.get("level", "log"), "text": message.get("text", "")[:8000]}
                )
        elif method == "Runtime.bindingCalled" and data.get("name") == self.page.error_binding:
            import json

            try:
                value = json.loads(data["payload"][:16384])
                if isinstance(value.get("text"), str):
                    self.console.add({"level": "error", "text": value["text"][:8000]})
            except (ValueError, TypeError, AttributeError):
                pass
        elif method == "Page.javascriptDialogOpening":
            self.dialog = {
                "type": data["type"],
                "message": data["message"],
                "defaultValue": data.get("defaultPrompt", ""),
                "session": sid,
            }
            self.dialog_opened.set()
        elif method == "Page.javascriptDialogClosed":
            self.dialog = None
            self.dialog_opened.clear()
        elif method == "Page.fileChooserOpened":
            self.chooser = {**data, "session": sid}
        elif method.startswith("Network."):
            self._network(method, data, sid)

    def _extra(self, key, kind, headers):
        candidates = [r for r in self.records.values() if r["key"] == key and not r[kind + "Extra"]]
        if candidates:
            record = candidates[0]
            record[kind + "Headers"] = headers
            record[kind + "Extra"] = True
        else:
            self.pending_extra.setdefault((key, kind), []).append(headers)
            self.configure(self.network.limit)

    def _network(self, method, data, sid):
        rid = data.get("requestId")
        if not rid:
            return
        key = (sid, rid)
        nav = self.page.navigation
        if (
            method == "Network.loadingFailed"
            and nav is not None
            and nav.get("requestId") == rid
            and sid == self.page.session
            and nav["state"] in {"requested", "committed"}
        ):
            self.page.fail_navigation(data.get("errorText", "network_failure"))
        if method == "Network.requestWillBeSent":
            previous = self.records.get(self.current.get(key))
            if previous and data.get("redirectResponse"):
                response = data["redirectResponse"]
                previous.update(response=response, done=True, redirected=True)
                previous["row"]["status"] = response["status"]
                if not previous["responseExtra"]:
                    previous["responseHeaders"] = response.get("headers", {})
            request = data["request"]
            nav = self.page.navigation
            if (
                nav is not None
                and nav["state"] in {"requested", "committed"}
                and sid == self.page.session
                and data.get("type") == "Document"
                and data.get("frameId") == self.page.main_id
            ):
                nav.update(requestId=rid, loaderId=data.get("loaderId"))
            row = self.network.add(
                {
                    "method": request["method"],
                    "url": request["url"][:4000],
                    "resourceType": data.get("type", "Other").lower(),
                }
            )
            record = {
                "key": key,
                "request": request,
                "row": row,
                "response": None,
                "done": False,
                "requestHeaders": request.get("headers", {}),
                "responseHeaders": {},
                "requestExtra": False,
                "responseExtra": False,
                "failed": False,
            }
            self.records[row["id"]] = record
            self.current[key] = row["id"]
            for kind in ("request", "response"):
                waiting = self.pending_extra.get((key, kind))
                if waiting:
                    self._extra(key, kind, waiting.pop(0))
                    if not waiting:
                        self.pending_extra.pop((key, kind), None)
            self.configure(self.network.limit)
        elif method in {"Network.requestWillBeSentExtraInfo", "Network.responseReceivedExtraInfo"}:
            self._extra(
                key, "request" if "requestWill" in method else "response", data.get("headers", {})
            )
        elif record := self.records.get(self.current.get(key)):
            if method == "Network.responseReceived":
                record["response"] = data["response"]
                record["row"]["status"] = data["response"]["status"]
                if not record["responseExtra"]:
                    record["responseHeaders"] = data["response"].get("headers", {})
                frame = self.page.frames.get(data.get("frameId"))
                if frame and data.get("type") == "Document":
                    self.page.navigation_status[(frame.id, data.get("loaderId"))] = data[
                        "response"
                    ]["status"]
            elif method == "Network.loadingFinished":
                record["done"] = True
            elif method == "Network.loadingFailed":
                record.update(done=True, failed=True)
                if nav is not None and nav.get("requestId") == rid and nav.get("networkError"):
                    record["row"]["error"] = nav["networkError"]

    async def details(self, params):
        record = self.records.get(params["id"])
        if not record or not record["response"]:
            raise BrowserError("network_entry_expired")
        sid, request_id = record["key"]
        part = params["part"]
        response = record["response"]
        if part.endswith("headers"):
            headers = record["requestHeaders" if part == "request-headers" else "responseHeaders"]
            return {
                "url": response["url"],
                "statusCode": response["status"],
                "headers": {k.lower(): str(v) for k, v in headers.items()},
            }
        limit = self.page.engine.config["maxBodyBytes"]
        if part == "request-body":
            entries = record["request"].get("postDataEntries")
            if entries and all("bytes" in x for x in entries):
                body = b"".join(base64.b64decode(x["bytes"]) for x in entries)
            elif "postData" in record["request"]:
                body = record["request"]["postData"].encode()
            elif record["request"].get("hasPostData"):
                if record.get("redirected"):
                    raise BrowserError("network_body_unavailable")
                result = await self.page.cdp.call(
                    "Network.getRequestPostData", {"requestId": request_id}, sid
                )
                body = result["postData"].encode()
            else:
                body = record["request"].get("postData", "").encode()
        else:
            # CDP reuses a requestId for redirects. Fetching an earlier hop by
            # that id would silently return the final response's bytes.
            if record.get("redirected"):
                raise BrowserError("network_body_unavailable")
            lengths = [
                v for k, v in record["responseHeaders"].items() if k.lower() == "content-length"
            ]
            if lengths and str(lengths[0]).isdigit() and int(lengths[0]) > limit:
                raise BrowserError("network_body_too_large")
            while not record["done"]:
                await retry_pause()
            if record["failed"]:
                raise BrowserError("network_body_unavailable")
            result = await self.page.cdp.call(
                "Network.getResponseBody", {"requestId": request_id}, sid
            )
            body = (
                base64.b64decode(result["body"])
                if result.get("base64Encoded")
                else result["body"].encode()
            )
        if len(body) > limit:
            raise BrowserError("network_body_too_large")
        return await self.page.engine.artifact(params["outputPath"], body)
