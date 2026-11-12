"""RequestExtension: the controller asks for an extension that is off in its conversation."""
from __future__ import annotations

import json

from app.extensions import ExtensionError, ExtensionService
from app.runtime.tool_result import ToolOutcome
from app.tools.base import current_tool_context


async def request_extension(service: ExtensionService, args: dict, ctx) -> dict:
    key = str(args.get("extension") or "").strip()
    reason = str(args.get("reason") or "").strip()
    extensions = {e.key: e for e in service.catalog()}
    if key not in extensions:
        raise ExtensionError("unknown_extension", "use a key from the extension catalog", extensions=list(extensions))
    if not reason:
        raise ExtensionError("reason_required", "say why the current task needs this extension")
    conversation = ctx.conversation_uuid
    state = await service.state(conversation)
    if key in service.effective(state):
        return {"ok": True, "status": "already_enabled", "extension": key}
    label = extensions[key].label
    if state is not None and state.auto_allow:
        await service.update(conversation, enable=[key], note="模型申请，自动允许：" + reason)
        return {"ok": True, "status": "enabled", "extension": key, "approvedBy": "auto_allow",
                "note": "its tools are available from your next model request"}
    from app.tools.servers import automatic_turn
    # A scheduled or event run has nobody watching: a confirmation card would only stall it.
    if ctx.web_confirm is None or await automatic_turn(service.db.conn, ctx):
        raise ExtensionError("user_confirmation_unavailable",
                             "nobody can confirm in this run and automatic allowing is off; continue without it or report that it is needed")
    answer = await ctx.web_confirm({
        "_sourceTool": "RequestExtension", "_requiresAuthorization": True,
        "_approvalScope": {"contract": "extension-request-v1", "extension": key},
        "title": f"开启扩展：{label}",
        "body": f"{extensions[key].about}\n\n理由：{reason}\n\n开启后，从模型的下一次请求起提供这些工具。",
        "type": "info", "default": False, "confirmText": "开启", "cancelText": "不开启", "timeoutSeconds": 600,
    })
    if answer.get("decision") == "confirm_with_changes" or answer.get("requiresAmendment"):
        # Approved with conditions: nothing is turned on until the call comes again with the receipt.
        return {"ok": False, "status": "amendments_required", "extension": key}
    if not answer.get("confirmed"):
        return {"ok": False, "status": "declined", "extension": key, "userFeedback": answer}
    await service.update(conversation, enable=[key], note="模型申请，已同意：" + reason)
    result = {"ok": True, "status": "enabled", "extension": key, "approvedBy": "user",
              "note": "its tools are available from your next model request"}
    if answer.get("appliedConditions"):
        result["appliedConditions"] = answer["appliedConditions"]
    return result


def register_extension_tool(registry, service: ExtensionService) -> None:
    async def handler(args):
        try:
            return json.dumps(await request_extension(service, args, current_tool_context()), ensure_ascii=False)
        except ExtensionError as exc:
            return ToolOutcome(json.dumps(exc.payload, ensure_ascii=False), "failed", "not_started")

    registry.add(
        "RequestExtension",
        "Turn on an extension from the system prompt's extension catalog for this conversation. The user confirms it, "
        "or it is allowed at once when the conversation allows the model to turn extensions on; its tools then appear "
        "from your next model request. On amendments_required nothing was turned on: apply the user's conditions, "
        "then call again with _openbearApprovalId=approvalId.",
        {"type": "object", "properties": {
            "extension": {"type": "string", "description": "Catalog key, e.g. browser or mcp:parrot."},
            "reason": {"type": "string", "description": "Why the current task needs it, shown to the user."},
        }, "required": ["extension", "reason"], "additionalProperties": False},
        handler, visibility={"main"},
    )
