"""One-use amended approvals; only tool-owned contracts can permit scope changes.

No part of this adapter interprets prose as a structured constraint. Generic MCP
has no trusted amendment contract: its receipt binds the entire argument object.
Native gates may supply exact scope plus explicitly monotone constraints.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import replace

from app.runtime.tool_result import ToolOutcome

APPROVAL_ARGUMENT = "_openbearApprovalId"
GATED_TOOLS = {"OpenBearControl", "Browser", "Cron", "Webhook", "InstanceControl", "Servers"}
APPROVAL_DESCRIPTION = (
    "Only after this tool returns amendments_required: apply all user conditions, "
    "then resubmit within its approvalPolicy with approvalId here. "
    "One use, current turn only; never a different target or broader effects. "
    "Generic MCP receipts require identical business arguments; changed arguments need a fresh tool confirmation without this ID."
)


def argument_digest(value):
    """Exact JSON identity, including types; never infer resource fields in MCP."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def constraints_within(original, candidate):
    """Compare only host-authored sets/minima, not arbitrary business arguments.

    Gate owners define the meaning of every set/minimum. Missing, malformed or
    newly added dimensions fail closed. These constraints stay process-local.
    """
    if not isinstance(original, dict) or not isinstance(candidate, dict):
        return False
    if set(original) != set(candidate) or set(original) - {"sets", "minimums"}:
        return False
    for kind in original:
        before, after = original[kind], candidate[kind]
        if not isinstance(before, dict) or not isinstance(after, dict) or set(before) != set(after):
            return False
        for key, old in before.items():
            new = after[key]
            if kind == "sets":
                if (not isinstance(old, list) or not isinstance(new, list)
                        or any(not isinstance(v, str) for v in [*old, *new])
                        or not set(new).issubset(old)):
                    return False
            elif (type(old) not in (int, float) or type(new) not in (int, float)
                  or not math.isfinite(old) or not math.isfinite(new) or new < old):
                return False
    return True


def confirmation_scope(name, arguments, identity=None, *, payload=None):
    """Only native gate code may opt into a non-exact business contract."""
    scope = {"tool": name}
    if identity:
        scope["mcpIdentity"] = list(identity)
    if name in GATED_TOOLS and payload and payload.get("_approvalScope"):
        scope["gate"] = copy.deepcopy(payload["_approvalScope"])
    else:
        # Unknown/native legacy gates fail closed too. Browser's timeout is only
        # a transport budget; its complete operation/page/params remain exact.
        business = {k: v for k, v in arguments.items() if name != "Browser" or k != "timeoutMs"}
        scope["argumentsDigest"] = argument_digest(business)
        if name in GATED_TOOLS and payload:
            scope["bodyDigest"] = argument_digest(payload.get("body", ""))
    return scope


def without_approval_argument(schema):
    """Agents cannot redeem receipts, so they see the tool's own schema."""
    properties = schema["parameters"].get("properties") or {}
    if APPROVAL_ARGUMENT not in properties or properties[APPROVAL_ARGUMENT].get("description") != APPROVAL_DESCRIPTION:
        return schema
    parameters = {**schema["parameters"], "properties": {k: v for k, v in properties.items() if k != APPROVAL_ARGUMENT}}
    return {**schema, "parameters": parameters}


def with_amended_confirmation(tool, identity=None):
    """Decorate only native gated tools / MCP; keep upstream business fields."""
    from app.tools.base import _TOOL_CONTEXT, current_tool_context

    if tool.name not in GATED_TOOLS and tool.source != "mcp":
        return tool
    if APPROVAL_ARGUMENT in tool.parameters.get("properties", {}):
        # Upstream owns this field; never intercept its business data.
        return tool
    parameters = copy.deepcopy(tool.parameters)
    parameters.setdefault("properties", {})[APPROVAL_ARGUMENT] = {"type": "string", "description": APPROVAL_DESCRIPTION}
    original = tool.handler

    async def handler(arguments):
        args = dict(arguments)
        approval_id = args.pop(APPROVAL_ARGUMENT, None)
        parent = current_tool_context()
        if approval_id is not None and (not isinstance(approval_id, str) or not approval_id.strip() or parent.web_confirm is None or parent.agent_session_uuid):
            return ToolOutcome(json.dumps({"status": "error", "error": "invalid_amended_approval"}), "denied", "not_started")
        amendment = None
        policy = "exact_arguments" if tool.source == "mcp" else "native_scope"

        async def confirm(payload):
            nonlocal amendment
            request = dict(payload)
            request["_approvalScope"] = confirmation_scope(tool.name, args, identity, payload=payload)
            request["_approvalConstraints"] = copy.deepcopy(payload.get("_approvalConstraints", {})) if tool.name in GATED_TOOLS else {}
            request["_approvalPolicy"] = policy
            if approval_id is not None:
                request["_approvalId"] = approval_id
            answer = await parent.web_confirm(request)
            if answer.get("decision") == "confirm_with_changes" and answer.get("authorizationGranted") is True:
                amendment = answer
            return answer

        context = replace(parent, web_confirm=confirm if parent.web_confirm else None)
        token = _TOOL_CONTEXT.set(context)
        try:
            result = await original(args)
        finally:
            parent.preserve_user_answer |= context.preserve_user_answer
            _TOOL_CONTEXT.reset(token)
        if amendment is not None:
            parent.preserve_user_answer = True
            return ToolOutcome(json.dumps({
                "status": "amendments_required", "outcome": "not_started",
                "approvalId": amendment.get("interactionId"), "approvalPolicy": policy,
                "confirmation": amendment,
                "message": (
                    "旧操作未执行。先完整落实附言；回执不证明自然语言条件已完成。"
                    + ("通用 MCP 没有可信参数变更契约：只有业务参数完全相同时可用此回执执行一次（例如先完成备份）。"
                       "若附言要求修改参数，请保留全部条件，用修订参数不带 _openbearApprovalId 重新调用工具取得确认；不得为复用回执执行冲突的旧参数。"
                       if policy == "exact_arguments" else
                       "在同一操作和资源内用修订参数及 _openbearApprovalId 续用一次；原生门仅允许其明确支持的不扩权修改／现场缩小。"
                       "范围不匹配时不要恢复冲突旧方案；保留全部条件，明确询问扩大或无法判定的范围。")
                    + "授权仅限当前轮，不含持久信任。"
                ),
            }, ensure_ascii=False), "completed", "not_started", business_success=None)
        return result

    return replace(tool, parameters=parameters, handler=handler)
