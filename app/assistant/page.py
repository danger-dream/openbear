"""The Page tool: page actions the assistant runs in the user's browser tab.

The assistant panel's conversation socket registers as the page host of its conversation.
A request goes to the most recently registered host and waits for its answer; without a
host, or past the timeout, the tool says so at once instead of hanging. A request names the
page and object its conversation belongs to; the browser refuses it while the user is elsewhere.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.runtime.tool_result import ToolOutcome
from app.tools.base import ToolRegistry, current_tool_context

Send = Callable[[dict[str, Any]], Awaitable[None]]
ScopeOf = Callable[[str], Awaitable[dict[str, Any] | None]]

ACTIONS = ("read", "locate", "fill", "invoke")


def _page_left(scope: dict[str, Any] | None) -> dict[str, Any]:
    where = f"「{scope['label']}」" if scope and scope.get("label") else ""
    return {"ok": False, "error": "page_left",
            "message": f"用户已离开发起这个任务的页面{where}（切到了别的页面或对象、关闭或刷新了浏览器页面，或在主对话里打开了这段对话），"
                       "页面操作没有执行。用后台工具完成；只能在页面上做的部分，告诉用户回到该页面后再做。"}


class PageBridge:
    def __init__(self, timeout_s: Callable[[], float], scope_of: ScopeOf | None = None) -> None:
        self._timeout_s = timeout_s
        self._scope_of = scope_of
        self._hosts: dict[str, dict[str, Send]] = {}
        self._pending: dict[str, tuple[str, str, dict[str, Any] | None, asyncio.Future]] = {}

    def attach(self, conversation_uuid: str, host_id: str, send: Send) -> None:
        hosts = self._hosts.setdefault(conversation_uuid, {})
        hosts.pop(host_id, None)
        hosts[host_id] = send

    def detach(self, conversation_uuid: str, host_id: str) -> None:
        hosts = self._hosts.get(conversation_uuid)
        if hosts is not None:
            hosts.pop(host_id, None)
            if not hosts:
                self._hosts.pop(conversation_uuid, None)
        for request_id, (conv, target_host, scope, future) in list(self._pending.items()):
            if conv == conversation_uuid and target_host == host_id and not future.done():
                future.set_result(_page_left(scope))

    def connected(self, conversation_uuid: str) -> bool:
        return bool(self._hosts.get(conversation_uuid))

    def resolve(self, conversation_uuid: str, request_id: str, result: Any) -> bool:
        pending = self._pending.get(request_id)
        if pending is None or pending[0] != conversation_uuid or pending[3].done():
            return False
        pending[3].set_result(result if isinstance(result, dict) else {"ok": False, "error": "invalid_page_result"})
        return True

    async def request(self, conversation_uuid: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        scope = await self._scope_of(conversation_uuid) if self._scope_of else None
        hosts = self._hosts.get(conversation_uuid)
        if not hosts:
            return _page_left(scope)
        host_id, send = next(reversed(hosts.items()))
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (conversation_uuid, host_id, scope, future)
        try:
            await send({"type": "page_request", "requestId": request_id, "action": action, "params": params,
                        **({"scope": {"key": scope["key"], "label": scope.get("label", "")}} if scope else {})})
            return await asyncio.wait_for(future, timeout=max(1.0, float(self._timeout_s())))
        except TimeoutError:
            return {"ok": False, "error": "page_timeout", "message": "页面没有在限定时间内回应；页面可能已切走或卡住，结果未知。"}
        except Exception as exc:
            return {"ok": False, "error": "page_send_failed", "message": f"无法把请求发给页面：{type(exc).__name__}"}
        finally:
            self._pending.pop(request_id, None)


DESCRIPTION = (
    "Act on the console page the user has open, in their browser tab, while the page assistant panel is open. "
    "read: the current page context again, with the visible text of the topmost overlay or of the page (region), "
    "its form fields with their current values, and its buttons. "
    "locate: scroll to the element whose visible text, label or aria-label matches target and highlight it for a few seconds. "
    "fill: put value into the text input, textarea or select whose label matches label (as read lists the fields); it never submits. "
    "Switches, checkboxes, radios, pickers and rich-text editors are only highlighted for the user to set. "
    "invoke: run one of the page's registered actions (the page context lists them under actions) by name with args. "
    "Nothing here presses buttons: when the remaining step is save, submit, delete or confirm, locate the button and ask the user to press it."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "region": {"type": "string", "enum": ["overlay", "page"],
                   "description": "read: which region's text and fields; default the topmost overlay, else the page"},
        "target": {"type": "string", "description": "locate: text, label or aria-label of the element"},
        "label": {"type": "string", "description": "fill: the field's label"},
        "value": {"type": "string", "description": "fill: the new text, or the option of a select"},
        "name": {"type": "string", "description": "invoke: a registered page action"},
        "args": {"type": "object", "description": "invoke: the action's arguments"},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _params(args: dict[str, Any]) -> dict[str, Any] | str:
    action = str(args.get("action") or "")
    if action not in ACTIONS:
        return f"unknown action: {action}"
    if action == "read":
        region = str(args.get("region") or "")
        return {"region": region} if region in {"overlay", "page"} else {}
    if action == "locate":
        target = str(args.get("target") or "").strip()
        return {"target": target} if target else "locate needs target"
    if action == "fill":
        label = str(args.get("label") or "").strip()
        if not label or "value" not in args:
            return "fill needs label and value"
        return {"label": label, "value": str(args["value"])}
    name = str(args.get("name") or "").strip()
    if not name:
        return "invoke needs name"
    return {"name": name, "args": args.get("args") if isinstance(args.get("args"), dict) else {}}


def page_registry(bridge: PageBridge) -> ToolRegistry:
    """A registry holding only Page, added to the tools of page assistant conversations."""
    registry = ToolRegistry()

    async def handler(args: dict[str, Any]) -> Any:
        params = _params(args)
        if isinstance(params, str):
            return ToolOutcome(json.dumps({"ok": False, "error": "invalid_arguments", "message": params}, ensure_ascii=False),
                               "failed", "not_started")
        result = await bridge.request(current_tool_context().conversation_uuid, str(args["action"]), params)
        content = json.dumps(result, ensure_ascii=False)
        if result.get("ok"):
            return content
        error = str(result.get("error") or "")
        return ToolOutcome(content, "failed", "not_started" if error in {"page_left", "page_send_failed"} else "unknown")

    registry.add("Page", DESCRIPTION, SCHEMA, handler, visibility={"main"})
    return registry


class PageToolRegistry:
    """A conversation's tools plus Page, in schema and dispatch."""

    def __init__(self, registry: Any, page: ToolRegistry) -> None:
        self.registry = registry
        self.page = page
        self.file_state = getattr(registry, "file_state", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)

    def _main(self, scope) -> bool:
        return scope in (None, "main")

    def schemas(self, scope=None):
        out = list(self.registry.schemas(scope=scope))
        return out + self.page.schemas(scope=scope) if self._main(scope) else out

    def names(self, scope=None, source=None):
        out = list(self.registry.names(scope=scope, source=source))
        return out + self.page.names(scope=scope, source=source) if self._main(scope) else out

    def summaries(self, scope=None, source=None):
        out = dict(self.registry.summaries(scope=scope, source=source))
        return {**out, **self.page.summaries(scope=scope, source=source)} if self._main(scope) else out

    async def dispatch(self, name, arguments, **kwargs):
        if name == "Page":
            return await self.page.dispatch(name, arguments, **kwargs)
        return await self.registry.dispatch(name, arguments, **kwargs)
