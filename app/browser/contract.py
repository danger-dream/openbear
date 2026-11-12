"""Small public schema; complete, strict action validation before any side effect."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, StrictBool, StrictStr, ValidationError, model_validator

ACTIONS = {
    "status": "Cached service/instance status; does not start a browser. nodes lists server nodes that offer a browser (main controller only).",
    "page": "op=list|new|adopt|close; new: url?; connects only the configured browser service and verifies the new page before navigation. new/list: node? = a server name or id from status to use that server's browser instead; every later operation on the page follows it. A node browser has that machine's network, logins and IP. adopt: page handle from list (confirmation). URLs resolve in the browser's network, not OpenBear's; localhost is browser-local.",
    "navigate": "op=goto|back|reload (default goto); goto requires url. Waits for DOMContentLoaded. observe? as for act.",
    "snapshot": "target?, depth?, boxes?, interactive?, maxChars?. Returns versioned refs for this page. Re-snapshot after navigation/recovery.",
    "find": "text required, literal case-insensitive search in a fresh snapshot; target?, maxChars?. Returns new versioned refs.",
    "act": "op=click|fill|type|press|check|select|hover|drag|scroll|resize|emulate. target=ref or css=unique-selector; text/key/checked/values/to as appropriate. fill optionally fields:[{target,value}]. click: button?, doubleClick?, modifiers?; type: slowly?, submit?; resize: width,height; emulate: media:{colorScheme?,reducedMotion?,forcedColors?,contrast?,media?}; scroll: target or x,y. observe?=summary|snapshot|none (default summary) adds observation, the page afterwards: summary = title, url, changed (vs the last full snapshot/observation; null without one), elements = interactive lines with refs of what changed (whole page after navigation), text = changed text, removed, more; snapshot = the snapshot action's full result. An open dialog is reported instead. Earlier refs stay usable while their element is unchanged.",
    "wait": "target or text with state=visible|hidden|attached|detached; alternatively url or timeMs. No implicit navigation retry.",
    "steps": "steps:[{action:act|wait|navigate, timeoutMs?, ...that action's params}] (1-20) plus observe? (as act) once at the end. Runs in order on one page; all steps are validated first and each behaves exactly as its own call. Stops at the first step not completed, or that leaves a dialog open; later steps do not run. Returns steps[] (each step's result), completedSteps, stoppedAt?, observation. outcome: completed when all ran; else the stopping step's outcome, except failed when earlier steps took effect. Refs are those of the snapshot before the call; after a navigate step use css= or a later call.",
    "capture": "Screenshot PNG: target?, fullPage?; view=true additionally supplies a structured image to the current model request. Files always returned as artifacts.",
    "files": "op=upload|drop|list|save|cancel; upload: paths (absolute files), target optional for open chooser; drop: target and paths or data:{mime:text}; list downloads; save/cancel: id from download list.",
    "dialog": "op=inspect|handle; handle requires accept:boolean, text optional for prompt. Runs outside the page queue to unblock dialogs.",
    "console": "after: event cursor, default 0; bounded events and overflow marker. Events start when this worker binds the page.",
    "network": "after cursor for list; id and part=request-headers|response-headers|request-body|response-body for details. Bodies saved as artifacts; limited retention.",
    "evaluate": "expression: JavaScript function/expression executed ONLY in the web page; target optional. May have effects, is not inherently read-only, never automatically retried. waitFor?:{text|target|url|expression, state?, timeoutMs?} first waits for text/target (state as wait), a url glob, or a side-effect-free expression to become truthy (polled), then runs expression; on timeout returns wait_for_timeout and the expression is not run.",
    "recover": "op=probe|connection|terminate|close|restart. page required except instance-level connection/restart (node? selects a server node's browser); destructive operations require built-in confirmation. Does not replay the previous action.",
    "describe": "action: name to return full argument reference, or omit for a one-line index of the actions. Not needed for ordinary page/snapshot/act calls.",
}
# One line per action for describe's index.
SUMMARIES = {
    "status": "service and instance status", "page": "list, open, adopt or close pages", "navigate": "go to a URL, back or reload",
    "snapshot": "accessibility snapshot with refs", "find": "elements matching text or a selector, with refs",
    "act": "click, fill, type, press, check, select, hover, drag, scroll, resize, emulate", "wait": "wait for text, an element, a URL or a time",
    "steps": "several act, wait and navigate steps in one call", "capture": "screenshot", "files": "upload and download files",
    "dialog": "answer a JavaScript dialog", "console": "console messages", "network": "network requests and their bodies",
    "evaluate": "run JavaScript in the page", "recover": "probe or recover a stuck page or browser",
    "describe": "this index; action=<action> gives its full parameters and usage",
}
# Where no server node can offer a browser (a managed instance) the node references are left out.
LOCAL_ACTIONS = {
    **ACTIONS,
    "status": "Cached service/instance status; does not start a browser.",
    "page": "op=list|new|adopt|close; new: url?; connects only the configured browser service and verifies the new page before navigation. adopt: page handle from list (confirmation). URLs resolve in the browser's network, not OpenBear's; localhost is browser-local.",
    "recover": "op=probe|connection|terminate|close|restart. page required except instance-level connection/restart; destructive operations require built-in confirmation. Does not replay the previous action.",
}

ALLOWED = {
    "status": set(),
    "describe": {"action"},
    "page": {"op", "url", "node"},
    "navigate": {"op", "url", "observe"},
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
        "observe",
    },
    "wait": {"target", "text", "state", "url", "timeMs"},
    "steps": {"steps", "observe"},
    "capture": {"target", "fullPage", "view"},
    "files": {"op", "target", "paths", "data", "id"},
    "dialog": {"op", "accept", "text"},
    "console": {"after"},
    "network": {"after", "id", "part"},
    "evaluate": {"target", "expression", "waitFor"},
    "recover": {"op", "node"},
}
STEP_ACTIONS = {"act", "wait", "navigate"}
MAX_STEPS = 20

REF = "s1a2b3c4:f1e5"
EXAMPLES = {
    "status": {},
    "describe": {"action": "act"},
    "page": {"op": "new", "url": "https://example.com/"},
    "navigate": {"url": "https://example.com/"},
    "snapshot": {"interactive": True},
    "find": {"text": "Sign in"},
    "act": {"op": "click", "target": REF},
    "wait": {"text": "Saved"},
    "steps": {
        "steps": [
            {"action": "act", "op": "fill", "fields": [{"target": REF, "value": "Ada"}]},
            {"action": "act", "op": "click", "target": "css=button[type=submit]"},
            {"action": "wait", "text": "Saved"},
        ]
    },
    "capture": {"fullPage": True},
    "files": {"op": "upload", "paths": ["/abs/path/file.pdf"], "target": REF},
    "dialog": {"op": "handle", "accept": True},
    "console": {"after": 0},
    "network": {"after": 0},
    "evaluate": {"expression": "() => document.title", "waitFor": {"text": "Loaded"}},
    "recover": {"op": "probe"},
}
ACT_EXAMPLES = {
    "click": {"target": REF},
    "fill": {"target": REF, "text": "value"},
    "type": {"target": REF, "text": "query", "submit": True},
    "press": {"key": "Enter"},
    "check": {"target": REF, "checked": True},
    "select": {"target": REF, "values": ["Option label"]},
    "hover": {"target": REF},
    "drag": {"target": REF, "to": "s1a2b3c4:f1e9"},
    "scroll": {"y": 600},
    "resize": {"width": 1280, "height": 800},
    "emulate": {"media": {"colorScheme": "dark"}},
}


def usage(action, op=None, actions=ACTIONS):
    """The accepted shape of one action, returned with argument errors."""
    if action not in actions:
        return {"actions": list(actions)}
    params = EXAMPLES[action]
    if action == "act" and op in ACT_EXAMPLES:
        params = {"op": op, **ACT_EXAMPLES[op]}
    allowed = ALLOWED[action] - (set() if actions is ACTIONS else {"node"})
    example = {"action": action}
    if action not in {"status", "describe"} and not (action == "page" and params.get("op") == "new"):
        example["page"] = "<page handle>"
    if params:
        example["params"] = params
    return {
        "action": action,
        "params": sorted(allowed),
        "reference": actions[action],
        "example": example,
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


class WaitFor(BaseModel):
    text: StrictStr | None = Field(default=None, min_length=1)
    target: StrictStr | None = Field(default=None, min_length=1)
    url: StrictStr | None = Field(default=None, min_length=1)
    expression: StrictStr | None = Field(default=None, min_length=1)
    state: Literal["visible", "hidden", "attached", "detached"] | None = None
    timeoutMs: int | None = Field(default=None, ge=1, le=1800000, strict=True)
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def one_condition(self):
        if sum(v is not None for v in (self.text, self.target, self.url, self.expression)) != 1:
            raise ValueError("waitFor_needs_exactly_one_of_text_target_url_expression")
        if self.state and not (self.text or self.target):
            raise ValueError("waitFor_state_applies_to_text_or_target")
        return self


class Parameters(BaseModel):
    op: StrictStr | None = None
    action: StrictStr | None = None
    url: StrictStr | None = None
    node: StrictStr | None = Field(default=None, min_length=1, max_length=120)
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
    observe: Literal["none", "summary", "snapshot"] | None = None
    waitFor: WaitFor | None = None
    steps: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=MAX_STEPS)
    model_config = {"extra": "forbid"}


class StepError(ValueError):
    def __init__(self, message, index, action=None, op=None):
        super().__init__(message)
        self.index, self.action, self.op = index, action, op


def _step(index, raw, page):
    """One steps entry validated exactly as its own call; errors name the step."""
    if not isinstance(raw, dict) or raw.get("action") not in STEP_ACTIONS:
        raise StepError(f"steps[{index}]: action must be one of act, wait, navigate", index)
    params = {k: v for k, v in raw.items() if k not in {"action", "timeoutMs"}}
    if "observe" in params:
        raise StepError(
            f"steps[{index}]: observe is set once for the whole steps call", index, raw["action"], params.get("op")
        )
    try:
        step = Request.model_validate(
            {"action": raw["action"], "page": page, "params": params,
             **({"timeoutMs": raw["timeoutMs"]} if "timeoutMs" in raw else {})}
        )
    except ValidationError as exc:
        first = exc.errors(include_input=False)[0]
        message = str(first["msg"]).removeprefix("Value error, ")
        where = ".".join(str(x) for x in first["loc"] if x not in {"params"})
        raise StepError(
            f"steps[{index}] {raw['action']}: {where + ': ' if where else ''}{message}",
            index, raw["action"], params.get("op"),
        ) from None
    return step


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
        if a == "steps":
            required("steps")
            p["steps"] = [_step(i, raw, self.page) for i, raw in enumerate(p["steps"])]
        if a == "describe" and p.get("action") not in {None, *ACTIONS}:
            raise ValueError("unknown_described_action")
        if p.get("url") and a in {"page", "navigate"}:
            u = urlsplit(p["url"])
            if p["url"] != "about:blank" and (u.scheme not in {"http", "https"} or not u.hostname):
                raise ValueError("only_http_https_navigation_allowed")
        self.params = p
        return self
