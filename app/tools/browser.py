"""One native Browser tool; action-specific policy stays in BrowserService."""

import json

from app.browser.contract import ACTIONS
from app.runtime.tool_result import ToolOutcome
from app.tools.base import current_tool_context


def register_browser_tool(registry, service):
    registry.browser_service = service
    if not service.available and not (service.nodes is not None and service.nodes.any_browser):
        return
    max_timeout_ms = int(service.cfg.max_timeout_s * 1000)
    action_timeout_ms = min(int(service.cfg.action_timeout_s * 1000), max_timeout_ms)
    navigation_timeout_ms = min(int(service.cfg.navigation_timeout_s * 1000), max_timeout_ms)
    # A managed instance has no server nodes, so it is not told about them.
    nodes = service.nodes is not None
    node_note = " page {op:new,node} opens it in a server node's browser (names from status; main controller only) and later operations follow the page." if nodes else ""

    async def browser(args):
        result = await service.call(args, current_tool_context())
        # BrowserService owns this contract; do not infer state from rendered text.
        reported = result.get("outcome")
        status = {"completed": "completed", "not_started": "denied",
                  "failed": "failed", "unknown": "unknown"}.get(reported)
        if status is None:
            status = "completed" if result.get("status") == "ok" else "unknown"
        return ToolOutcome(json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            status=status,
            effect_state="not_started" if reported == "not_started" else "reported" if status == "completed" else "unknown",
            business_success=True if status == "completed" else False if status in {"failed", "denied"} else None)

    registry.add(
        "Browser",
        "Remote browser service with its existing logins; no local browser is launched. Every page operation uses an explicit page handle; start with page {op:new,url?} or page {op:list}." + node_note + " Core: snapshot/find return refs; act {op:click|fill|type|press|check|select|hover|drag|scroll|resize|emulate, target?, text?, key?, checked?, values?, to?} where target is a ref or css=selector; navigate {url, op?:goto|back|reload}; capture {fullPage?, view?} (view=true also returns the image to you); wait {text|target|url|timeMs}; dialog. Use describe {action} for files/network/console/evaluate/recover and full parameters. Results: failed = executed and failed; not_started = rejected; unknown = effects unconfirmed, do not replay blindly. After page_not_ready use recover probe, not repeated snapshots. evaluate runs in the page and may have external effects. Destructive recovery has its own confirmation gate; on amendments_required apply the user's conditions and resubmit the same operation with _openbearApprovalId. URLs and localhost resolve in the browser's network, not OpenBear's.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "page": {
                    "type": "string",
                    "description": "Opaque page handle from page/snapshot. Required for page operations except list/new.",
                },
                "timeoutMs": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": max_timeout_ms,
                    "description": f"Total deadline in milliseconds including connection, page creation and navigation. Defaults: {action_timeout_ms} for actions, {navigation_timeout_ms} for navigation or page creation with a URL. Maximum {max_timeout_ms}; accepted requests are also capped at runtime, and this argument cannot extend the system limit. Queue waiting also has its own cap.",
                },
                "params": {
                    "type": "object",
                    "description": "Action parameters. Common fields below; rare operations: describe {action}. Runtime rejects unknown fields.",
                    "properties": {
                        "op": {"type": "string"},
                        "url": {"type": "string"},
                        **({"node": {"type": "string"}} if nodes else {}),
                        "target": {"type": "string"},
                        "text": {"type": "string"},
                        "key": {"type": "string"},
                        "action": {"type": "string"},
                        "maxChars": {"type": "integer"},
                        "interactive": {"type": "boolean"},
                        "fullPage": {"type": "boolean"},
                        "view": {"type": "boolean"},
                    },
                    "additionalProperties": True,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        browser,
        visibility={"main", "runtime"} | ({"agent"} if service.cfg.agent_access else set()),
    )
