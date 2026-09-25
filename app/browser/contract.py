"""Small public schema; complete, strict action validation before any side effect."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, StrictBool, StrictStr, model_validator

ACTIONS = {
    "status": "Cached service/instance status; does not start a browser.",
    "page": "op=list|new|adopt|close; new: url?; connects only the configured browser service and verifies the new page before navigation. adopt: page handle from list (confirmation). URLs resolve in the browser's network, not OpenBear's; localhost is browser-local.",
    "navigate": "op=goto|back|reload (default goto); goto requires url. Waits for DOMContentLoaded.",
    "snapshot": "target?, depth?, boxes?, interactive?, maxChars?. Returns versioned refs for this page. Re-snapshot after navigation/recovery.",
    "find": "text required, literal case-insensitive search in a fresh snapshot; target?, maxChars?. Returns new versioned refs.",
    "act": "op=click|fill|type|press|check|select|hover|drag|scroll|resize|emulate. target=ref or css=unique-selector; text/key/checked/values/to as appropriate. fill optionally fields:[{target,value}]. click: button?, doubleClick?, modifiers?; type: slowly?, submit?; resize: width,height; emulate: media:{colorScheme?,reducedMotion?,forcedColors?,contrast?,media?}; scroll: target or x,y.",
    "wait": "target or text with state=visible|hidden|attached|detached; alternatively url or timeMs. No implicit navigation retry.",
    "capture": "Screenshot PNG: target?, fullPage?; view=true additionally supplies a structured image to the current model request. Files always returned as artifacts.",
    "files": "op=upload|drop|list|save|cancel; upload: paths (absolute files), target optional for open chooser; drop: target and paths or data:{mime:text}; list downloads; save/cancel: id from download list.",
    "dialog": "op=inspect|handle; handle requires accept:boolean, text optional for prompt. Runs outside the page queue to unblock dialogs.",
    "console": "after: event cursor, default 0; bounded events and overflow marker. Events start when this worker binds the page.",
    "network": "after cursor for list; id and part=request-headers|response-headers|request-body|response-body for details. Bodies saved as artifacts; limited retention.",
    "evaluate": "expression: JavaScript function/expression executed ONLY in the web page; target optional. May have effects, is not inherently read-only, never automatically retried.",
    "recover": "op=probe|connection|terminate|close|restart. page required except instance-level connection/restart; destructive operations require built-in confirmation. Does not replay the previous action.",
    "describe": "action: name to return full argument reference, or omit to list all. Not needed for ordinary page/snapshot/act calls.",
}

ALLOWED = {
    "status": set(),
    "describe": {"action"},
    "page": {"op", "url"},
    "navigate": {"op", "url"},
    "snapshot": {"target", "depth", "boxes", "interactive", "maxChars"},
    "find": {"target", "text", "depth", "maxChars"},
    "act": {
        "op",
        "target",
        "text",
        "key",
        "checked",
        "values",
        "to",
        "fields",
        "button",
        "doubleClick",
        "modifiers",
        "slowly",
        "submit",
        "width",
        "height",
        "x",
        "y",
        "media",
    },
    "wait": {"target", "text", "state", "url", "timeMs"},
    "capture": {"target", "fullPage", "view"},
    "files": {"op", "target", "paths", "data", "id"},
    "dialog": {"op", "accept", "text"},
    "console": {"after"},
    "network": {"after", "id", "part"},
    "evaluate": {"target", "expression"},
    "recover": {"op"},
}
OPS = {
    "page": {"list", "new", "adopt", "close"},
    "navigate": {"goto", "back", "reload"},
    "act": {
        "click",
        "fill",
        "type",
        "press",
        "check",
        "select",
        "hover",
        "drag",
        "scroll",
        "resize",
        "emulate",
    },
    "files": {"upload", "drop", "list", "save", "cancel"},
    "dialog": {"inspect", "handle"},
    "recover": {"probe", "connection", "terminate", "close", "restart"},
}
DEFAULT_OP = {"page": "list", "navigate": "goto", "dialog": "inspect", "recover": "probe"}


class FieldValue(BaseModel):
    target: StrictStr
    value: StrictStr
    model_config = {"extra": "forbid"}


class Parameters(BaseModel):
    op: StrictStr | None = None
    action: StrictStr | None = None
    url: StrictStr | None = None
    target: StrictStr | None = None
    text: StrictStr | None = None
    key: StrictStr | None = None
    expression: StrictStr | None = None
    checked: StrictBool | None = None
    accept: StrictBool | None = None
    values: list[StrictStr] | None = None
    to: StrictStr | None = None
    fields: list[FieldValue] | None = None
    paths: list[StrictStr] | None = None
    data: dict[StrictStr, StrictStr] | None = None
    button: Literal["left", "middle", "right"] | None = None
    modifiers: list[Literal["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]] | None = None
    doubleClick: StrictBool | None = None
    slowly: StrictBool | None = None
    submit: StrictBool | None = None
    fullPage: StrictBool | None = None
    view: StrictBool | None = None
    interactive: StrictBool | None = None
    boxes: StrictBool | None = None
    width: int | None = Field(default=None, ge=320, le=7680, strict=True)
    height: int | None = Field(default=None, ge=240, le=4320, strict=True)
    depth: int | None = Field(default=None, ge=1, le=100, strict=True)
    maxChars: int | None = Field(default=None, ge=100, le=64000, strict=True)
    x: int | None = Field(default=None, ge=-100000, le=100000, strict=True)
    y: int | None = Field(default=None, ge=-100000, le=100000, strict=True)
    timeMs: int | None = Field(default=None, ge=0, le=60000, strict=True)
    after: int | None = Field(default=None, ge=0, strict=True)
    id: int | None = Field(default=None, ge=1, strict=True)
    state: Literal["visible", "hidden", "attached", "detached"] | None = None
    part: Literal["request-headers", "response-headers", "request-body", "response-body"] | None = (
        None
    )
    media: dict[str, Any] | None = None
    model_config = {"extra": "forbid"}


class Request(BaseModel):
    action: str
    page: StrictStr | None = None
    timeoutMs: int | None = Field(default=None, ge=1, le=1800000, strict=True)
    params: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_action(self):
        a, p = self.action, self.params
        if a not in ACTIONS:
            raise ValueError("unknown_action")
        if a == "page":
            p = dict(p)
            # Old main calls keep their exact login/network semantics. Never
            # turn an isolated request into a shared-login operation silently.
            if "mode" in p:
                if p["mode"] == "isolated":
                    raise ValueError(
                        "isolated_browser_removed: only the configured browser service is supported; no fallback was performed"
                    )
                if p["mode"] != "main":
                    raise ValueError("invalid_browser_mode")
                p.pop("mode")
            if p.get("op") == "release":
                raise ValueError(
                    "isolated_release_removed: use page close for an explicitly owned page; the browser service is not released"
                )
        unknown = set(p) - ALLOWED[a]
        if unknown:
            raise ValueError("unexpected params: " + ",".join(sorted(unknown)))
        p = Parameters.model_validate(p).model_dump(exclude_none=True)
        if a in OPS:
            p.setdefault("op", DEFAULT_OP.get(a, ""))
            if p["op"] not in OPS[a]:
                raise ValueError("invalid_operation")
        op = p.get("op")

        def required(*keys):
            for key in keys:
                if key not in p or p[key] is None or p[key] == "":
                    raise ValueError(f"{key}_required")

        if a == "navigate" and op == "goto":
            required("url")
        if a == "find":
            required("text")
        if a == "evaluate":
            required("expression")
        if a == "act":
            if op in {"click", "type", "check", "select", "hover", "drag"}:
                required("target")
            if op in {"fill", "type"}:
                if not p.get("fields"):
                    required("target")
                    p.setdefault("text", "")
            if op == "press":
                required("key")
            if op == "check":
                required("checked")
            if op == "select":
                required("values")
            if op == "drag":
                required("to")
            if op == "resize":
                required("width", "height")
            if op == "scroll" and not any(k in p for k in ("target", "x", "y")):
                raise ValueError("scroll_target_or_delta_required")
            if op == "emulate":
                required("media")
                enums = {
                    "colorScheme": {"light", "dark"},
                    "reducedMotion": {"reduce", "no-preference"},
                    "forcedColors": {"active", "none"},
                    "contrast": {"more", "no-preference"},
                    "media": {"screen", "print"},
                }
                for key, value in p["media"].items():
                    if key not in enums or (value is not None and value not in enums[key]):
                        raise ValueError("invalid_media")
        if a == "files":
            if op == "upload":
                required("paths")
            if op == "drop":
                required("target")
                if not p.get("paths") and not p.get("data"):
                    raise ValueError("drop_data_required")
            if op in {"save", "cancel"}:
                required("id")
        if a == "dialog" and op == "handle":
            required("accept")
        if a == "wait" and sum(k in p for k in ("target", "text", "url", "timeMs")) != 1:
            raise ValueError("exactly_one_wait_condition_required")
        if a == "network" and p.get("id"):
            required("part")
        if a == "describe" and p.get("action") not in {None, *ACTIONS}:
            raise ValueError("unknown_described_action")
        if p.get("url") and a in {"page", "navigate"}:
            u = urlsplit(p["url"])
            if p["url"] != "about:blank" and (u.scheme not in {"http", "https"} or not u.hostname):
                raise ValueError("only_http_https_navigation_allowed")
        self.params = p
        return self
