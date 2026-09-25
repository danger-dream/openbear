"""One native Browser tool; action-specific policy stays in BrowserService."""

import json

from app.browser.contract import ACTIONS
from app.tools.base import current_tool_context


def register_browser_tool(registry, service):
    registry.browser_service = service
    if not service.available:
        return

    async def browser(args):
        result = await service.call(args, current_tool_context())
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    registry.add(
        "Browser",
        "Native browser. One explicit page handle per conversation/Agent; never assume a global current tab. "
        "Start: page {op:new,url?} verifies its new page before navigation; page {op:list} lists handles. "
        "snapshot {interactive?,maxChars?} or find {text} returns versioned refs. "
        "act {op:click|fill|type|press|check|select|hover|drag|scroll|resize|emulate,target?,text?,key?,checked?,values?,to?}; "
        "target is a snapshot ref or css=unique-selector. navigate {url,op?:goto|back|reload}. "
        "capture {fullPage?,view?} saves PNG; view=true also supplies image input. "
        "wait {text|target|url|timeMs,state?}. dialog {op:inspect|handle,accept?,text?}. "
        "For files/network/console/evaluate/recover use describe {action} for detailed parameters. "
        "Errors include phase/diagnostics/recovery. page_not_ready blocks repeated calls after failure; use recover probe, not repeated snapshots. "
        "outcome=failed means execution failed; not_started means this call was rejected before execution; unknown means effects are unconfirmed, never replay blindly. "
        "Recover does not replay actions. Destructive recovery/adoption supplies its own confirmation; "
        "feedback text or cancellation never authorizes it. evaluate runs in the page, not the server, and may have external effects. "
        "Uses only the configured browser service and its existing logins; no local browser is launched. "
        "URLs/localhost resolve in that browser's network, not OpenBear's. Do not infer shared networking from a local control endpoint or host-side reachability. "
        "No implicit browser/container restart. status reads cached state without launching a browser.",
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
                    "description": "Total deadline including connection, page creation and navigation, capped by settings. Queue waiting also has its own cap.",
                },
                "params": {
                    "type": "object",
                    "description": "Action parameters. Common fields below; rare operations: describe {action}. Runtime rejects unknown fields.",
                    "properties": {
                        "op": {"type": "string"},
                        "url": {"type": "string"},
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
