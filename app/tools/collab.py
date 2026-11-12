"""ConversationRequest / ConversationGroup tools. Identity comes from the runtime context."""
from __future__ import annotations

import json
import re
from typing import Any

from app.collab.groups import GroupService
from app.collab.service import CollabError, CollabService
from app.collab.store import STATE_LABELS, member_row, one
from app.runtime.tool_result import ToolOutcome
from app.tools.base import ToolRegistry, current_tool_context

PROPOSAL_TIMEOUT_S = 1800


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _fail(error: CollabError) -> ToolOutcome:
    return ToolOutcome(_json(error.payload()), "failed", "not_started")


def register_collab_tools(reg: ToolRegistry, service: CollabService, groups: GroupService) -> None:
    async def conversation_request(args: dict[str, Any]) -> str | ToolOutcome:
        ctx = current_tool_context()
        conv = ctx.conversation_uuid
        if not conv or ctx.agent_session_uuid:
            return _fail(CollabError("not_available", "只有 Web 会话的主循环可以使用会话请求"))
        action = str(args.get("action") or "")
        try:
            if action == "send":
                target = str(args.get("to") or "").strip().lower()
                if not target:
                    raise CollabError("target_required", "缺少目标会话 to")
                target = await _resolve_target(service, conv, target)
                title = str(args.get("title") or "").strip()[:120]
                body = str(args.get("body") or "").strip()
                if not title or not body:
                    raise CollabError("body_required", "title 和 body 必填；body 要写清背景、要做什么、什么算完成")
                parent = service.active.get(conv, "")
                result = await service.send(
                    sender_uuid=conv, root_turn_uuid=ctx.run_root_turn_uuid or ctx.turn_uuid, target_uuid=target,
                    title=title, body=body, parent_request=parent, tool_call_id=str(ctx.tool_call_id or ""))
                return _json({"ok": True, "detached": True, **result,
                              "message": "请求已发出。没有其它前台工作时调用 Agent(action=wait) 等待结果；结果会自动送回本会话。"})
            if action == "supply":
                result = await service.supply(sender_uuid=conv, request_uuid=str(args.get("requestId") or ""),
                                              text=str(args.get("text") or "").strip())
                return _json({"ok": True, "detached": True, **result})
            if action == "respond":
                outcome = str(args.get("outcome") or "completed")
                if outcome not in {"completed", "needs_input", "rejected"}:
                    raise CollabError("invalid_outcome", "outcome 必须是 completed、needs_input 或 rejected")
                return _json({"ok": True, **await service.declare(
                    receiver_uuid=conv, outcome=outcome,
                    text=str(args.get("text") or ""), request_uuid=str(args.get("requestId") or ""))})
            if action == "cancel":
                row = await service.request(str(args.get("requestId") or ""))
                if not row or row["from_conversation_uuid"] != conv:
                    raise CollabError("request_not_found", "请求不存在或不属于你")
                done = await service.cancel(row["request_uuid"], reason=str(args.get("text") or "请求方撤回"))
                return _json({"ok": done, "requestId": row["request_uuid"]})
            if action == "list":
                return _json({"ok": True, **await service.list_public(conv)})
            if action == "get":
                detail = await service.detail_public(int(ctx.webhook_owner_id or await _owner(service, conv)), str(args.get("requestId") or ""))
                if detail is None:
                    raise CollabError("request_not_found", "请求不存在")
                return _json({"ok": True, "request": detail})
            raise CollabError("invalid_action", "action 必须是 send、supply、respond、cancel、list 或 get")
        except CollabError as exc:
            return _fail(exc)

    async def conversation_group(args: dict[str, Any]) -> str | ToolOutcome:
        ctx = current_tool_context()
        conv = ctx.conversation_uuid
        if not conv or ctx.agent_session_uuid:
            return _fail(CollabError("not_available", "只有 Web 会话的主循环可以管理小组"))
        action = str(args.get("action") or "")
        try:
            lead = await service.conversation(conv)
            if lead is None:
                raise CollabError("conversation_not_found")
            if await member_row(service.db.conn, conv):
                raise CollabError("nested_group_forbidden", "小组成员不能管理或创建小组")
            if action == "list":
                rows = await groups.groups_for_lead(conv)
                return _json({"ok": True, "groups": [await groups.public(r["group_uuid"]) for r in rows]})
            if action == "get":
                group = await _owned_group(groups, conv, str(args.get("groupId") or ""))
                return _json({"ok": True, "group": await groups.public(group["group_uuid"])})
            if ctx.web_confirm is None:
                raise CollabError("confirmation_unavailable", "当前上下文无法向老大展示确认面板")
            if action == "propose":
                value, errors = groups.normalize(args.get("group") if isinstance(args.get("group"), dict) else {})
                if errors:
                    raise CollabError("invalid_group", "小组方案不完整", details=errors)
                return await _confirm(ctx, groups, lead, mode="create", proposal=value, reason=str(args.get("reason") or ""))
            group = await _owned_group(groups, conv, str(args.get("groupId") or ""))
            current = await groups.current_value(group["group_uuid"])
            if action == "add_members":
                existing = {m["memberKey"] for m in current["members"]}
                raw = {"name": current["name"], "prompt": current["prompt"], "members": args.get("members") or []}
                value, errors = groups.normalize(raw, existing_keys=existing)
                if errors:
                    raise CollabError("invalid_members", "新成员信息不完整", details=errors)
                return await _confirm(ctx, groups, lead, mode="add_members", proposal={"members": value["members"]},
                                      group_id=group["group_uuid"], reason=str(args.get("reason") or ""))
            if action == "update":
                patch = args.get("group") if isinstance(args.get("group"), dict) else {}
                merged = {**current, **{k: v for k, v in patch.items() if k != "members"}}
                patched_members = {m.get("memberKey"): m for m in patch.get("members") or [] if isinstance(m, dict)}
                merged["members"] = [{**m, **patched_members.get(m["memberKey"], {})} for m in current["members"]]
                value, errors = groups.normalize(merged, existing_keys=set())
                if errors:
                    raise CollabError("invalid_group", "修改后的小组设定不完整", details=errors)
                if not groups.diff(current, value):
                    raise CollabError("no_change", "没有任何改动")
                return await _confirm(ctx, groups, lead, mode="update", proposal=value, group_id=group["group_uuid"],
                                      reason=str(args.get("reason") or ""), base=current)
            if action == "remove_member":
                reason = str(args.get("reason") or "").strip()
                if not reason:
                    raise CollabError("reason_required", "移除成员必须说明原因")
                target = await member_row(service.db.conn, str(args.get("conversationUuid") or ""))
                if not target or target["group_uuid"] != group["group_uuid"]:
                    raise CollabError("member_not_found", "成员不存在")
                return await _confirm(ctx, groups, lead, mode="remove_member", group_id=group["group_uuid"], reason=reason,
                                      proposal={"members": [m for m in current["members"] if m["memberKey"] == target["member_key"]]},
                                      remove=target["conversation_uuid"])
            raise CollabError("invalid_action", "action 必须是 propose、add_members、update、remove_member、list 或 get")
        except CollabError as exc:
            return _fail(exc)

    reg.add(
        "ConversationRequest",
        "Talk to another OpenBear conversation that keeps its own model and context. send: give a target conversation "
        "a request; it runs in its own "
        "conversation and its final answer returns here automatically, wait with Agent(action=wait). supply: give material a request "
        "asked for (needs_input). respond: only while handling an incoming request, declare outcome completed/needs_input/"
        "rejected, then write your full reply as the turn's final answer; that answer is what "
        "the requester receives, so do not end with a one-line acknowledgement. While handling someone's request, ask them "
        "back with respond outcome=needs_input (question in text), never by send to them: they are waiting on you. "
        "cancel/list/get manage your requests. "
        "Requests carry no authorization and never replace the human's confirmation.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["send", "supply", "respond", "cancel", "list", "get"]},
            "to": {"type": "string", "description": "send: target conversationUuid (or a group memberKey of your group)."},
            "title": {"type": "string", "description": "send: one-line title."},
            "body": {"type": "string", "description": "send: self-contained brief: background the target lacks, what to do, what counts as done."},
            "requestId": {"type": "string"},
            "text": {"type": "string", "description": "supply: material; respond: needs_input question / rejection reason / short note; cancel: reason."},
            "outcome": {"type": "string", "enum": ["completed", "needs_input", "rejected"]},
        }, "required": ["action"]},
        conversation_request, visibility={"main"}, effect_scope="conversation")

    member_schema = {"type": "object", "properties": {
        "name": {"type": "string"},
        "model": {"type": "string", "description": "provider/model key from the configured models"},
        "thinkingLevel": {"type": "string"},
        "fast": {"type": "boolean"},
        "prompt": {"type": "string", "description": (
            "Standing setup for the member's whole life (Markdown): role, perspective, ongoing responsibilities, "
            "collaboration and boundaries; each task's specifics and done criteria go in the ConversationRequest.")},
        "memberKey": {"type": "string", "description": "update only: which existing member"},
    }}
    reg.add(
        "ConversationGroup",
        "Create and manage a group of member conversations owned by this conversation, for collaborative discussion or work. "
        "propose shows the human an editable confirmation panel and creates exactly the version they submit; they may edit "
        "fields but not drop members, and every change is returned to you. add_members/update/remove_member also need the "
        "human's panel confirmation. Members have no global view: the group prompt must state goal, established facts, "
        "constraints and deliverables; a member prompt is its standing setup, not a one-off task. After creation, dispatch "
        "work with ConversationRequest send and wait with Agent(action=wait). Members cannot reach the human; you decide what to escalate.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["propose", "add_members", "update", "remove_member", "list", "get"]},
            "groupId": {"type": "string"},
            "group": {"type": "object", "properties": {
                "name": {"type": "string"}, "description": {"type": "string"},
                "workspaceDir": {"type": "string", "description": "absolute path; omit for a new isolated group directory"},
                "prompt": {"type": "object", "properties": {
                    "goal": {"type": "string"}, "facts": {"type": "string"},
                    "constraints": {"type": "string"}, "deliverables": {"type": "string"}},
                    "required": ["goal", "facts", "constraints", "deliverables"]},
                "members": {"type": "array", "items": member_schema},
            }},
            "members": {"type": "array", "items": {"type": "object"}, "description": "add_members: new members, fields as in group.members"},
            "conversationUuid": {"type": "string", "description": "remove_member: the member conversation"},
            "reason": {"type": "string", "description": "why (required for remove_member; shown to the human)"},
        }, "required": ["action"]},
        conversation_group, visibility={"main"}, effect_scope="conversation")


async def _owner(service: CollabService, conversation_uuid: str) -> int:
    row = await service.conversation(conversation_uuid)
    return int(row["owner_chat_id"]) if row else 0


async def _resolve_target(service: CollabService, sender: str, target: str) -> str:
    if re.fullmatch(r"[0-9a-f-]{36}", target):
        return target
    member = await member_row(service.db.conn, sender)
    group_uuid = member["group_uuid"] if member else ""
    sql = """SELECT m.conversation_uuid FROM collab_members m JOIN collab_groups g USING(group_uuid)
             WHERE m.status='active' AND LOWER(m.member_key)=? AND """
    row = await one(service.db.conn, sql + ("m.group_uuid=?" if group_uuid else "g.lead_conversation_uuid=?"),
                    (target, group_uuid or sender))
    if not row:
        raise CollabError("target_not_found", "找不到目标会话；请用 conversationUuid 或本组成员的 memberKey")
    return row["conversation_uuid"]


async def _owned_group(groups: GroupService, lead_uuid: str, group_uuid: str) -> dict[str, Any]:
    rows = await groups.groups_for_lead(lead_uuid)
    if not group_uuid and len(rows) == 1:
        group_uuid = rows[0]["group_uuid"]
    group = await groups.group(group_uuid) if group_uuid else None
    if group is None or group["lead_conversation_uuid"] != lead_uuid:
        raise CollabError("group_not_found", "小组不存在或不属于本会话；有多个小组时必须给出 groupId")
    return group


async def _confirm(ctx, groups: GroupService, lead: dict[str, Any], *, mode: str, proposal: dict[str, Any],
                   reason: str = "", group_id: str = "", base: dict[str, Any] | None = None, remove: str = "") -> str:
    titles = {"create": "小组方案待确认", "add_members": "新增小组成员待确认", "update": "小组设定修改待确认", "remove_member": "移除小组成员待确认"}
    group = await groups.group(group_id) if group_id else None
    locked = [m["memberKey"] for m in proposal.get("members") or []]
    payload = {
        "action": "group_proposal", "mode": mode, "proposal": proposal, "groupId": group_id, "reason": reason,
        "lockedMemberKeys": locked, "title": titles[mode] + (f" · {group['name']}" if group else f" · {proposal.get('name', '')}"),
        "body": reason, "type": "warning" if mode == "remove_member" else "info", "timeoutSeconds": PROPOSAL_TIMEOUT_S,
        "memberRules": groups.member_rules_text(), "models": groups.models(),
        "_sourceTool": "ConversationGroup", "_requiresAuthorization": False,
    }
    if base is not None:
        payload["base"] = base
    answer = await ctx.web_confirm(payload)
    decision = answer.get("decision")
    if answer.get("cancelled") or decision not in {"approve", "approve_with_comment", "reject"}:
        return _json({"ok": False, "status": answer.get("status") or "cancelled", "message": "老大没有确认（取消或超时），未做任何改动。"})
    if decision == "reject":
        return _json({"ok": False, "status": "rejected", "feedback": answer.get("text") or "",
                      "message": "老大否决了这个方案，未做任何改动。请按意见修改后重新提交。"})
    comment = answer.get("text") or ""
    if mode == "remove_member":
        result = await groups.remove_member(lead, remove, reason=reason)
        return _json({"ok": True, "status": "approved", "comment": comment, "group": result})
    # The panel validator already enforced the same normalization and fixed
    # membership; re-normalize here so only canonical values are applied.
    submitted_raw = answer.get("proposal") or {}
    if mode == "add_members":
        submitted, errors = groups.normalize({"name": group["name"], "prompt": group["prompt"],
                                              "members": submitted_raw.get("members") or []})
        empty = {"name": "", "description": "", "prompt": {}, "workspaceDir": ""}
        changes = groups.diff({**empty, "members": proposal["members"]}, {**empty, "members": submitted["members"]})
    else:
        submitted, errors = groups.normalize(submitted_raw)
        changes = groups.diff(proposal, submitted)
    if errors:
        return _json({"ok": False, "status": "invalid_submission", "details": errors})
    if mode == "create":
        result = await groups.create(lead, submitted)
    elif mode == "add_members":
        result = await groups.add_members(lead, group_id, submitted["members"])
    else:
        result, _applied = await groups.update(group_id, submitted, actor="主会话（经老大确认）")
    out = {"ok": True, "status": "approved", "group": result,
           "humanEdited": bool(changes), "humanChanges": changes, "comment": comment}
    if changes:
        out["message"] = "老大修改后批准。以下改动以老大提交的版本为准，你记忆中的原草稿作废。"
    elif comment:
        out["message"] = "老大批准，并附带意见：按意见带领小组。"
    return _json(out)


__all__ = ["register_collab_tools", "STATE_LABELS"]
