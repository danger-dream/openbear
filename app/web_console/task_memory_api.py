# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.task_memory import (
    SCOPE_AGENT_TASK,
    SCOPE_CONVERSATION,
    TASK_MEMORY_ACTIVE_MAX,
    TASK_MEMORY_RUNTIME_MAX_TOKENS,
    TaskMemoryConflict,
    TaskMemoryDAO,
    TaskMemoryError,
    TaskMemoryNotFound,
    render_task_memory_runtime_block,
    task_memory_audit_detail,
    task_memory_catalog_xml,
    task_memory_result_for_exception,
)
from app.utils import estimate_tokens
from app.web_console.core import *


class WebAdminTaskMemoryMixin:
    @staticmethod
    def _task_memory_bool(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _task_memory_int(value: Any, *, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def _task_memory_scope(
        self,
        request: web.Request,
        row: dict[str, Any],
        *,
        body: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        source = body if body is not None else request.query
        scope_type = str(source.get("scopeType") or SCOPE_CONVERSATION).strip()
        if scope_type == SCOPE_CONVERSATION:
            return SCOPE_CONVERSATION, ""
        if scope_type != SCOPE_AGENT_TASK:
            raise web.HTTPBadRequest(text="invalid_scope_type")
        task_uuid = str(source.get("taskUuid") or "").strip()
        if not task_uuid:
            raise web.HTTPNotFound(text="task_memory_not_found")
        task = await self.rath_dao.get_task(task_uuid)
        if (
            task is None
            or int(task.chat_id or 0) != int(row.get("internal_chat_id") or 0)
            or str(task.parent_session_uuid or "") != str(row.get("conversation_uuid") or "")
        ):
            raise web.HTTPNotFound(text="task_memory_not_found")
        return SCOPE_AGENT_TASK, task_uuid

    @staticmethod
    def _task_memory_error(exc: Exception) -> web.Response:
        if isinstance(exc, TaskMemoryNotFound):
            return web.json_response({"ok": False, "error": "task_memory_not_found"}, status=404)
        if isinstance(exc, TaskMemoryConflict):
            return web.json_response({"ok": False, "error": exc.code, "message": exc.message}, status=409)
        if isinstance(exc, TaskMemoryError):
            return web.json_response({"ok": False, "error": exc.code, "message": exc.message}, status=400)
        return web.json_response({"ok": False, "error": "task_memory_error"}, status=500)

    async def _audit_task_memory(
        self,
        request: web.Request,
        action: str,
        *,
        conversation_uuid: str,
        scope_type: str,
        task_uuid: str,
        memory_uuid: str,
        changed_fields: Any,
        revision: int,
        result: str,
        idempotency_status: str = "none",
        idempotency_key: str = "",
    ) -> None:
        session: WebSession = request[_WEB_SESSION_KEY]
        # Keep the existing Web audit sink and kind stable; detail is metadata-only.
        detail = task_memory_audit_detail(
            actor="web",
            conversation_uuid=conversation_uuid,
            scope_type=scope_type,
            task_uuid=task_uuid,
            memory_uuid=memory_uuid,
            action=action,
            changed_fields=changed_fields,
            revision=revision,
            idempotency_status=idempotency_status,
            idempotency_key=idempotency_key,
            result=result,
        )
        await self.audit(
            f"web.task_memory.{action}",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail=detail,
        )

    async def _task_memory_mutation_scope(
        self,
        request: web.Request,
        row: dict[str, Any],
        body: dict[str, Any],
        *,
        action: str,
        memory_uuid: str,
        changed_fields: Any,
        revision: int,
        idempotency_status: str = "none",
        idempotency_key: str = "",
    ) -> tuple[str, str]:
        conversation_uuid = str(row.get("conversation_uuid") or "")
        requested_scope = str(body.get("scopeType") or SCOPE_CONVERSATION).strip()
        requested_task = str(body.get("taskUuid") or "").strip()
        try:
            return await self._task_memory_scope(request, row, body=body)
        except web.HTTPException as exc:
            await self._audit_task_memory(
                request,
                action,
                conversation_uuid=conversation_uuid,
                scope_type=requested_scope,
                task_uuid=requested_task,
                memory_uuid=memory_uuid,
                changed_fields=changed_fields,
                revision=revision,
                idempotency_status=idempotency_status,
                idempotency_key=idempotency_key,
                result="not_found" if int(exc.status or 0) == 404 else "validation",
            )
            raise

    async def handle_api_task_memories(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        scope_type, task_uuid = await self._task_memory_scope(request, row)
        try:
            result = await TaskMemoryDAO(self.db).list(
                conversation_uuid=str(row["conversation_uuid"]),
                scope_type=scope_type,
                task_uuid=task_uuid,
                query=str(request.query.get("query") or ""),
                include_deleted=self._task_memory_bool(request.query.get("includeDeleted")),
                offset=self._task_memory_int(request.query.get("offset"), default=0),
                limit=self._task_memory_int(request.query.get("limit"), default=50),
            )
        except Exception as exc:
            return self._task_memory_error(exc)
        return web.json_response({
            "ok": True,
            "conversationUuid": str(row["conversation_uuid"]),
            "scopeType": scope_type,
            "taskUuid": task_uuid,
            "maxActive": TASK_MEMORY_ACTIVE_MAX,
            **result,
        })

    async def handle_api_task_memory_tasks(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        conversation_uuid = str(row.get("conversation_uuid") or "")
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        cur = await self.db.conn.execute(
            """
            SELECT task_uuid, title, status, current_agent_key, input_json, updated_at
            FROM rath_tasks
            WHERE chat_id=? AND parent_session_uuid=?
            ORDER BY updated_at DESC, id DESC
            LIMIT 100
            """,
            (internal_chat_id, conversation_uuid),
        )
        tasks: list[dict[str, Any]] = []
        for raw in await cur.fetchall():
            data = dict(raw)
            try:
                payload = json.loads(str(data.get("input_json") or "{}"))
            except Exception:
                payload = {}
            snapshot = payload.get("agentSnapshot") if isinstance(payload, dict) else {}
            agent_name = str((snapshot or {}).get("name") or data.get("current_agent_key") or "Agent")
            task_uuid = str(data.get("task_uuid") or "")
            tasks.append({
                "taskUuid": task_uuid,
                "taskShortId": task_uuid[:8],
                "name": agent_name,
                "title": str(data.get("title") or ""),
                "status": str(data.get("status") or ""),
                "updatedAt": int(data.get("updated_at") or 0),
            })
        return web.json_response({"ok": True, "conversationUuid": conversation_uuid, "tasks": tasks})

    async def handle_api_task_memory_preview(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        scope_type, task_uuid = await self._task_memory_scope(request, row)
        conversation_uuid = str(row["conversation_uuid"])
        catalog_xml = await task_memory_catalog_xml(
            TaskMemoryDAO(self.db),
            conversation_uuid=conversation_uuid,
            task_uuid=task_uuid,
            for_agent=scope_type == SCOPE_AGENT_TASK,
        )
        runtime_block = render_task_memory_runtime_block(catalog_xml)
        return web.json_response({
            "ok": True,
            "conversationUuid": conversation_uuid,
            "scopeType": scope_type,
            "taskUuid": task_uuid,
            "catalogXml": catalog_xml,
            "estimatedRuntimeTokens": estimate_tokens(runtime_block) if runtime_block else 0,
            "maxRuntimeTokens": TASK_MEMORY_RUNTIME_MAX_TOKENS,
        })

    async def handle_api_task_memory_detail(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        scope_type, task_uuid = await self._task_memory_scope(request, row)
        try:
            item = await TaskMemoryDAO(self.db).get(
                str(request.match_info.get("memory_uuid") or ""),
                conversation_uuid=str(row["conversation_uuid"]),
                scope_type=scope_type,
                task_uuid=task_uuid,
                include_deleted=self._task_memory_bool(request.query.get("includeDeleted")),
            )
        except Exception as exc:
            return self._task_memory_error(exc)
        return web.json_response({"ok": True, "memory": item})

    async def handle_api_task_memory_create(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        conversation_uuid = str(row["conversation_uuid"])
        changed_fields = [
            key for key in ("name", "description", "body", "autoReinjectCatalog", "visibleToAgents")
            if key in body
        ]
        idempotency_key = str(body.get("idempotencyKey") or "").strip()
        idempotency_status = "explicit" if idempotency_key else "none"
        scope_type, task_uuid = await self._task_memory_mutation_scope(
            request,
            row,
            body,
            action="create",
            memory_uuid="",
            changed_fields=changed_fields,
            revision=0,
            idempotency_status=idempotency_status,
            idempotency_key=idempotency_key,
        )
        try:
            item, created = await TaskMemoryDAO(self.db).create(
                conversation_uuid=conversation_uuid,
                scope_type=scope_type,
                task_uuid=task_uuid,
                name=body.get("name"),
                description=body.get("description", ""),
                body=body.get("body", ""),
                auto_reinject_catalog=self._task_memory_bool(body.get("autoReinjectCatalog"), default=True),
                visible_to_agents=(
                    self._task_memory_bool(body.get("visibleToAgents"))
                    if scope_type == SCOPE_CONVERSATION else False
                ),
                created_by=f"web:{session.chat_id}",
                source_turn_uuid=str(body.get("sourceTurnUuid") or ""),
                source_run_uuid=str(body.get("sourceRunUuid") or ""),
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            await self._audit_task_memory(
                request,
                "create",
                conversation_uuid=conversation_uuid,
                scope_type=scope_type,
                task_uuid=task_uuid,
                memory_uuid="",
                changed_fields=changed_fields,
                revision=0,
                idempotency_status=(
                    "conflict"
                    if isinstance(exc, TaskMemoryConflict) and exc.code == "task_memory_idempotency_conflict"
                    else idempotency_status
                ),
                idempotency_key=idempotency_key,
                result=task_memory_result_for_exception(exc),
            )
            return self._task_memory_error(exc)
        await self._audit_task_memory(
            request,
            "create",
            conversation_uuid=conversation_uuid,
            scope_type=scope_type,
            task_uuid=task_uuid,
            memory_uuid=item["memoryUuid"],
            changed_fields=changed_fields,
            revision=item["revision"],
            idempotency_status=idempotency_status if created else "replayed",
            idempotency_key=idempotency_key,
            result="success",
        )
        return web.json_response({"ok": True, "created": created, "memory": item}, status=201 if created else 200)

    async def handle_api_task_memory_update(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        conversation_uuid = str(row["conversation_uuid"])
        memory_uuid = str(request.match_info.get("memory_uuid") or "")
        revision = self._task_memory_int(body.get("revision"))
        mapping = {
            "name": "name", "description": "description", "body": "body",
            "autoReinjectCatalog": "auto_reinject_catalog",
            "visibleToAgents": "visible_to_agents",
        }
        changes = {target: body[source] for source, target in mapping.items() if source in body}
        scope_type, task_uuid = await self._task_memory_mutation_scope(
            request,
            row,
            body,
            action="update",
            memory_uuid=memory_uuid,
            changed_fields=changes,
            revision=revision,
        )
        if scope_type == SCOPE_AGENT_TASK:
            changes.pop("visible_to_agents", None)
        try:
            item = await TaskMemoryDAO(self.db).update(
                memory_uuid,
                conversation_uuid=conversation_uuid,
                scope_type=scope_type,
                task_uuid=task_uuid,
                expected_revision=revision,
                changes=changes,
            )
        except Exception as exc:
            await self._audit_task_memory(
                request,
                "update",
                conversation_uuid=conversation_uuid,
                scope_type=scope_type,
                task_uuid=task_uuid,
                memory_uuid=memory_uuid,
                changed_fields=changes,
                revision=revision,
                result=task_memory_result_for_exception(exc),
            )
            return self._task_memory_error(exc)
        await self._audit_task_memory(
            request,
            "update",
            conversation_uuid=conversation_uuid,
            scope_type=scope_type,
            task_uuid=task_uuid,
            memory_uuid=item["memoryUuid"],
            changed_fields=changes,
            revision=item["revision"],
            result="success",
        )
        return web.json_response({"ok": True, "memory": item})

    async def handle_api_task_memory_delete(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        conversation_uuid = str(row["conversation_uuid"])
        memory_uuid = str(request.match_info.get("memory_uuid") or "")
        revision = self._task_memory_int(body.get("revision"))
        scope_type, task_uuid = await self._task_memory_mutation_scope(
            request,
            row,
            body,
            action="delete",
            memory_uuid=memory_uuid,
            changed_fields=["deletedAt"],
            revision=revision,
        )
        try:
            item = await TaskMemoryDAO(self.db).delete(
                memory_uuid,
                conversation_uuid=conversation_uuid,
                scope_type=scope_type,
                task_uuid=task_uuid,
                expected_revision=revision,
            )
        except Exception as exc:
            await self._audit_task_memory(
                request,
                "delete",
                conversation_uuid=conversation_uuid,
                scope_type=scope_type,
                task_uuid=task_uuid,
                memory_uuid=memory_uuid,
                changed_fields=["deletedAt"],
                revision=revision,
                result=task_memory_result_for_exception(exc),
            )
            return self._task_memory_error(exc)
        await self._audit_task_memory(
            request,
            "delete",
            conversation_uuid=conversation_uuid,
            scope_type=scope_type,
            task_uuid=task_uuid,
            memory_uuid=item["memoryUuid"],
            changed_fields=["deletedAt"],
            revision=item["revision"],
            result="success",
        )
        return web.json_response({"ok": True, "memory": item})

    async def handle_api_task_memory_restore(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        conversation_uuid = str(row["conversation_uuid"])
        memory_uuid = str(request.match_info.get("memory_uuid") or "")
        revision = self._task_memory_int(body.get("revision"))
        scope_type, task_uuid = await self._task_memory_mutation_scope(
            request,
            row,
            body,
            action="restore",
            memory_uuid=memory_uuid,
            changed_fields=["deletedAt"],
            revision=revision,
        )
        try:
            item = await TaskMemoryDAO(self.db).restore(
                memory_uuid,
                conversation_uuid=conversation_uuid,
                scope_type=scope_type,
                task_uuid=task_uuid,
                expected_revision=revision,
            )
        except Exception as exc:
            await self._audit_task_memory(
                request,
                "restore",
                conversation_uuid=conversation_uuid,
                scope_type=scope_type,
                task_uuid=task_uuid,
                memory_uuid=memory_uuid,
                changed_fields=["deletedAt"],
                revision=revision,
                result=task_memory_result_for_exception(exc),
            )
            return self._task_memory_error(exc)
        await self._audit_task_memory(
            request,
            "restore",
            conversation_uuid=conversation_uuid,
            scope_type=scope_type,
            task_uuid=task_uuid,
            memory_uuid=item["memoryUuid"],
            changed_fields=["deletedAt"],
            revision=item["revision"],
            result="success",
        )
        return web.json_response({"ok": True, "memory": item})
