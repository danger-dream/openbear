"""Dedicated TaskMemory model tool.

Conversation/task identity is runtime metadata.  The model can choose an action,
but it cannot submit an owner, conversation UUID, task UUID, or Agent session UUID.
"""
from __future__ import annotations

import json
from typing import Any

from app.logging import get_logger
from app.task_memory import (
    SCOPE_AGENT_TASK,
    SCOPE_CONVERSATION,
    TaskMemoryConflict,
    TaskMemoryDAO,
    TaskMemoryError,
    TaskMemoryNotFound,
    TaskMemoryValidationError,
    audit_task_memory_domain,
    build_task_memory_changed_event,
    derive_task_memory_tool_idempotency_key,
)
from app.tools.base import ToolRegistry, current_tool_context

log = get_logger("task_memory.event")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _changes(args: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "name": "name",
        "description": "description",
        "body": "body",
        "autoReinjectCatalog": "auto_reinject_catalog",
        "visibleToAgents": "visible_to_agents",
    }
    return {target: args[source] for source, target in mapping.items() if source in args}


class TaskMemoryTool:
    def __init__(self, dao: TaskMemoryDAO) -> None:
        self.dao = dao

    @staticmethod
    def _context() -> tuple[Any, str, bool]:
        ctx = current_tool_context()
        conversation_uuid = str(ctx.conversation_uuid or ctx.session_uuid or "").strip()
        is_agent = str(ctx.source or "").startswith("agent:")
        return ctx, conversation_uuid, is_agent

    @staticmethod
    async def _publish_changed(
        ctx: Any,
        *,
        conversation_uuid: str,
        scope_type: str,
        task_uuid: str,
        item: dict[str, Any],
        action: str,
    ) -> None:
        callback = getattr(ctx, "conversation_event", None)
        if not callable(callback):
            return
        event = build_task_memory_changed_event(
            conversation_uuid=conversation_uuid,
            scope_type=scope_type,
            task_uuid=task_uuid,
            memory_uuid=str(item.get("memoryUuid") or ""),
            action=action,
            revision=int(item.get("revision") or 0),
        )
        try:
            await callback(event)
        except Exception:
            log.error(
                "task_memory.changed publish failed",
                conversationUuid=conversation_uuid,
                scopeType=scope_type,
                taskUuid=task_uuid,
                memoryUuid=str(item.get("memoryUuid") or ""),
                action=action,
                revision=int(item.get("revision") or 0),
            )

    async def _agent_list(self, args: dict[str, Any], *, query: str = "") -> dict[str, Any]:
        ctx, conversation_uuid, _ = self._context()
        if not conversation_uuid or not str(ctx.task_uuid or "").strip():
            raise TaskMemoryNotFound()
        private_scope, private_owner = await self.dao.agent_scope(conversation_uuid, str(ctx.task_uuid).strip())
        own = await self.dao.list(
            conversation_uuid=conversation_uuid,
            scope_type=private_scope,
            task_uuid=private_owner,
            query=query,
            include_deleted=bool(args.get("includeDeleted")),
            offset=int(args.get("offset") or 0),
            limit=int(args.get("limit") or 50),
        )
        shared: dict[str, Any] = {"items": [], "total": 0, "offset": 0, "limit": int(args.get("limit") or 50)}
        if bool(args.get("includeShared", True)) and not bool(args.get("includeDeleted")):
            shared = await self.dao.list(
                conversation_uuid=conversation_uuid,
                scope_type=SCOPE_CONVERSATION,
                query=query,
                visible_to_agents_only=True,
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 50),
            )
        return {"ok": True, "scope": f"current_{private_scope}", "own": own, "sharedConversation": shared}

    async def _get_agent(self, memory_uuid: str) -> dict[str, Any]:
        ctx, conversation_uuid, _ = self._context()
        if not conversation_uuid or not str(ctx.task_uuid or "").strip():
            raise TaskMemoryNotFound()
        private_scope, private_owner = await self.dao.agent_scope(conversation_uuid, str(ctx.task_uuid).strip())
        try:
            item = await self.dao.get(
                memory_uuid,
                conversation_uuid=conversation_uuid,
                scope_type=private_scope,
                task_uuid=private_owner,
            )
            return {"ok": True, "scope": f"current_{private_scope}", "memory": item}
        except TaskMemoryNotFound:
            item = await self.dao.get(
                memory_uuid,
                conversation_uuid=conversation_uuid,
                scope_type=SCOPE_CONVERSATION,
                visible_to_agents_only=True,
            )
            return {"ok": True, "scope": "shared_conversation_read_only", "memory": item}

    async def handle(self, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "list").strip().lower()
        ctx, conversation_uuid, is_agent = self._context()
        scope_type = SCOPE_AGENT_TASK if is_agent else SCOPE_CONVERSATION
        task_uuid = str(ctx.task_uuid or "").strip() if is_agent else ""
        if is_agent and task_uuid and conversation_uuid:
            scope_type, task_uuid = await self.dao.agent_scope(conversation_uuid, task_uuid)
        actor = f"agent:{ctx.agent_key}" if is_agent else "main-controller"
        memory_uuid = str(args.get("memoryUuid") or "").strip()
        mutation = action in {"create", "update", "delete", "restore"}
        changes = _changes(args) if action == "update" else {}
        if is_agent:
            changes.pop("visible_to_agents", None)
        changed_fields: Any = (
            [key for key in ("name", "description", "body", "autoReinjectCatalog", "visibleToAgents") if key in args]
            if action == "create"
            else (changes if action == "update" else (["deletedAt"] if action in {"delete", "restore"} else []))
        )
        revision = 0
        idempotency_key = str(args.get("idempotencyKey") or "").strip()
        idempotency_status = "explicit" if idempotency_key else "none"

        def audit(
            result: str,
            *,
            item: dict[str, Any] | None = None,
            idem_status: str | None = None,
        ) -> None:
            if not mutation:
                return
            audit_task_memory_domain(
                actor=actor,
                conversation_uuid=conversation_uuid,
                scope_type=scope_type,
                task_uuid=task_uuid,
                memory_uuid=str((item or {}).get("memoryUuid") or memory_uuid),
                action=action,
                changed_fields=changed_fields,
                revision=int((item or {}).get("revision") or revision or 0),
                idempotency_status=idem_status or idempotency_status,
                idempotency_key=idempotency_key,
                result=result,
            )

        if not conversation_uuid:
            audit("not_found")
            return _json({"ok": False, "error": "task_memory_not_found"})
        try:
            if action in {"list", "search"}:
                query = str(args.get("query") or "").strip() if action == "search" else ""
                if is_agent:
                    return _json(await self._agent_list(args, query=query))
                listing = await self.dao.list(
                    conversation_uuid=conversation_uuid,
                    scope_type=SCOPE_CONVERSATION,
                    query=query,
                    include_deleted=bool(args.get("includeDeleted")),
                    offset=int(args.get("offset") or 0),
                    limit=int(args.get("limit") or 50),
                )
                return _json({"ok": True, "scope": "current_conversation", **listing})

            if action == "get":
                if not memory_uuid:
                    return _json({"ok": False, "error": "memory_uuid_required"})
                if is_agent:
                    return _json(await self._get_agent(memory_uuid))
                item = await self.dao.get(
                    memory_uuid,
                    conversation_uuid=conversation_uuid,
                    scope_type=SCOPE_CONVERSATION,
                    include_deleted=bool(args.get("includeDeleted")),
                )
                return _json({"ok": True, "scope": "current_conversation", "memory": item})

            if is_agent and not task_uuid:
                raise TaskMemoryNotFound()
            if action == "create":
                if not idempotency_key:
                    idempotency_key = derive_task_memory_tool_idempotency_key(
                        conversation_uuid=conversation_uuid,
                        scope_type=scope_type,
                        task_uuid=task_uuid,
                        source=str(ctx.source or ""),
                        agent_session_uuid=str(ctx.agent_session_uuid or ""),
                        run_identity=str(ctx.run_root_turn_uuid or ctx.turn_uuid or ctx.task_uuid or ""),
                        tool_call_id=str(ctx.tool_call_id or ""),
                    )
                    idempotency_status = "generated"
                item, created = await self.dao.create(
                    conversation_uuid=conversation_uuid,
                    scope_type=scope_type,
                    task_uuid=task_uuid,
                    name=args.get("name"),
                    description=args.get("description", ""),
                    body=args.get("body", ""),
                    auto_reinject_catalog=bool(args.get("autoReinjectCatalog", True)),
                    visible_to_agents=bool(args.get("visibleToAgents", False)) if not is_agent else False,
                    created_by=actor,
                    source_turn_uuid=str(ctx.turn_uuid or ""),
                    source_run_uuid=str(ctx.task_uuid or ctx.run_root_turn_uuid or ""),
                    idempotency_key=idempotency_key,
                )
                audit("success", item=item, idem_status=idempotency_status if created else "replayed")
                if created:
                    await self._publish_changed(
                        ctx,
                        conversation_uuid=conversation_uuid,
                        scope_type=scope_type,
                        task_uuid=task_uuid,
                        item=item,
                        action="create",
                    )
                return _json({"ok": True, "created": created, "memory": item})
            if not memory_uuid:
                raise TaskMemoryValidationError("memory_uuid_required", "memoryUuid is required")
            revision = int(args.get("revision") or 0)
            if revision <= 0:
                raise TaskMemoryValidationError("revision_required", "revision is required")
            if action == "update":
                item = await self.dao.update(
                    memory_uuid,
                    conversation_uuid=conversation_uuid,
                    scope_type=scope_type,
                    task_uuid=task_uuid,
                    expected_revision=revision,
                    changes=changes,
                )
                audit("success", item=item)
                await self._publish_changed(
                    ctx,
                    conversation_uuid=conversation_uuid,
                    scope_type=scope_type,
                    task_uuid=task_uuid,
                    item=item,
                    action="update",
                )
                return _json({"ok": True, "memory": item})
            if action == "delete":
                item = await self.dao.delete(
                    memory_uuid,
                    conversation_uuid=conversation_uuid,
                    scope_type=scope_type,
                    task_uuid=task_uuid,
                    expected_revision=revision,
                )
                audit("success", item=item)
                await self._publish_changed(
                    ctx,
                    conversation_uuid=conversation_uuid,
                    scope_type=scope_type,
                    task_uuid=task_uuid,
                    item=item,
                    action="delete",
                )
                return _json({"ok": True, "memory": item})
            if action == "restore":
                item = await self.dao.restore(
                    memory_uuid,
                    conversation_uuid=conversation_uuid,
                    scope_type=scope_type,
                    task_uuid=task_uuid,
                    expected_revision=revision,
                )
                audit("success", item=item)
                await self._publish_changed(
                    ctx,
                    conversation_uuid=conversation_uuid,
                    scope_type=scope_type,
                    task_uuid=task_uuid,
                    item=item,
                    action="restore",
                )
                return _json({"ok": True, "memory": item})
            return _json({"ok": False, "error": "unsupported_action"})
        except TaskMemoryNotFound as exc:
            audit("not_found")
            return _json({"ok": False, "error": exc.code})
        except TaskMemoryConflict as exc:
            audit(
                "conflict",
                idem_status="conflict" if exc.code == "task_memory_idempotency_conflict" else None,
            )
            return _json({"ok": False, "error": exc.code, "message": exc.message, "conflict": True})
        except TaskMemoryError as exc:
            audit("validation")
            return _json({"ok": False, "error": exc.code, "message": exc.message})
        except (TypeError, ValueError):
            audit("validation")
            return _json({"ok": False, "error": "invalid_task_memory_arguments"})
        except Exception:
            audit("error")
            return _json({"ok": False, "error": "task_memory_error"})


def register_task_memory_tool(registry: ToolRegistry, dao: TaskMemoryDAO) -> None:
    handler = TaskMemoryTool(dao)
    registry.add(
        "TaskMemory",
        (
            "Reference notes and continuing execution preferences for the owning conversation or independent Agent instance, "
            "preserved across context-window rotation. Main controllers own current-conversation memory; "
            "child Agents own their independent instance memory across successive task rounds, or task-local memory "
            "for legacy tasks. Identity is runtime-derived, never supplied by the model. Agents may also read "
            "explicitly shared conversation memory, but cannot modify it or access another instance's private memory. "
            "list/search return body-free locators. Auto-injected snapshots include complete nonempty bodies of at most "
            "500 Unicode characters; use those directly without another get. Longer bodies require get when needed. "
            "Snapshots share a 20-item/1500-estimated-token budget and report omissions. Shared preferences are Agent "
            "inputs even without this tool; private auto-injection requires this round's tool grant. "
            "Do not store task logs, progress, Plan copies, implementation/test trails, completion reports or deployment receipts. "
            "Update changed preferences in place, keeping only applicable content; consult History/AgentHistory for original "
            "exchanges and execution records. Delete withdrawn or unneeded notes using the existing CAS revision."
        ),
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "search", "get", "create", "update", "delete", "restore"],
                },
                "memoryUuid": {"type": "string", "description": "Memory id returned by list/search; never a conversation/task id."},
                "query": {"type": "string", "description": "Search text for action=search."},
                "name": {"type": "string", "maxLength": 80},
                "description": {"type": "string", "maxLength": 200, "description": "Brief description for locating this reference or preference."},
                "body": {"type": "string", "description": "Reference or continuing preference, up to 16 KiB UTF-8. Nonempty bodies <=500 Unicode characters are auto-injected in full when selected; longer bodies are never partially injected."},
                "autoReinjectCatalog": {"type": "boolean", "description": "Include in the bounded automatic snapshot (default true): full short body, otherwise name/description/ID only. Over-budget entries are explicitly omitted."},
                "visibleToAgents": {"type": "boolean", "description": "Conversation notes only: share as read-only Agent input, independent of TaskMemory tool grants. Automatic snapshots still require autoReinjectCatalog."},
                "revision": {"type": "integer", "minimum": 1, "description": "Required CAS revision for update/delete/restore."},
                "idempotencyKey": {"type": "string", "maxLength": 160},
                "includeShared": {"type": "boolean", "description": "For child Agent list/search, include visible conversation memories."},
                "includeDeleted": {"type": "boolean", "description": "Own writable scope only; shared deleted memories are never visible."},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler.handle,
        visibility={"main", "agent"},
    )
