"""Bulk delete, archive and move for the conversation tree's multi-select mode."""
from __future__ import annotations

from typing import Any

from aiohttp import web

from app.db.engine import now_ts
from app.web_console.core import _WEB_SESSION_KEY

MAX_BATCH = 500
_ACTIONS = {"delete", "archive", "unarchive", "move"}
_IMPACT_COUNTS = ("affectedCount", "archivedCount", "runningCount", "updatableCount")


def _failure(conv_uuid: str, error: str, message: str = "") -> dict[str, Any]:
    return {"conversationUuid": conv_uuid, "ok": False, "error": error, **({"message": message} if message else {})}


class WebAdminConversationBatchMixin:
    async def handle_api_conversation_batch(self, request: web.Request) -> web.Response:
        session = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        body = await self._json_body(request)
        action = str(body.get("action") or "")
        ids = body.get("conversationIds")
        if action not in _ACTIONS:
            return web.json_response({"ok": False, "error": "invalid_batch_action"}, status=400)
        if (not isinstance(ids, list) or not ids or len(ids) > MAX_BATCH
                or any(not isinstance(item, str) or not item.strip() for item in ids)):
            return web.json_response({"ok": False, "error": "invalid_conversation_ids", "max": MAX_BATCH}, status=400)
        ids = list(dict.fromkeys(item.strip() for item in ids))
        if action == "move":
            return await self._batch_move(request, owner, ids, body)
        results = []
        for conv_uuid in ids:
            row = await self._conversation_row(owner, conv_uuid)
            if row is None:
                results.append(_failure(conv_uuid, "conversation_not_found"))
            elif action == "delete":
                results.append(await self._batch_delete_one(request, owner, row))
            else:
                results.append(await self._batch_archive_one(request, owner, row, action == "archive"))
        self._invalidate_tree_status(owner)
        if action != "delete":
            getattr(self, "_tree_projection_ready", set()).discard(owner)
        return web.json_response(self._batch_summary(action, results))

    @staticmethod
    def _batch_summary(action: str, results: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        succeeded = sum(1 for item in results if item["ok"])
        return {"ok": True, "action": action, "results": results, "succeeded": succeeded,
                "failed": len(results) - succeeded, **extra}

    async def _batch_delete_one(self, request: web.Request, owner: int, row: dict[str, Any]) -> dict[str, Any]:
        conv_uuid = str(row["conversation_uuid"])
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        # Unlike the single delete, a batch never stops live work as a side effect.
        if str(row.get("status") or "") == "running" or (self.runs is not None and self.runs.is_running(internal_chat_id)):
            return _failure(conv_uuid, "conversation_running", "运行中的会话不能删除")
        async with self.operation_locks.try_chat(internal_chat_id, "web_conversation_delete") as acquired:
            if not acquired:
                return _failure(conv_uuid, "conversation_delete_busy", "会话正在执行其他操作")
            result = await self._delete_web_conversation_locked(
                owner_chat_id=owner, conv_uuid=conv_uuid, internal_chat_id=internal_chat_id,
            )
        if not result.get("ok"):
            return _failure(conv_uuid, str(result.get("error") or "conversation_delete_busy"), str(result.get("message") or ""))
        await self.audit("web.conversation.delete", actor="web", chat_id=owner, ip=request.remote or "",
                         detail={**result["auditDetail"], "batch": True})
        return {"conversationUuid": conv_uuid, "ok": True}

    async def _batch_archive_one(self, request: web.Request, owner: int, row: dict[str, Any], archive: bool) -> dict[str, Any]:
        conv_uuid = str(row["conversation_uuid"])
        if bool(int(row.get("archived_at") or 0)) == archive:
            return {"conversationUuid": conv_uuid, "ok": True, "unchanged": True}
        archived_at = now_ts() if archive else 0
        async with self.db.write_transaction(label="web-conversation-batch-archive") as conn:
            await conn.execute(
                "UPDATE web_conversations SET archived_at=? WHERE conversation_uuid=? AND owner_chat_id=?",
                (archived_at, conv_uuid, owner),
            )
        if getattr(self, "collab_groups", None) is not None:
            await self.collab_groups.follow_lead(conv_uuid, archived_at=archived_at)
        await self.audit("web.conversation.archive" if archive else "web.conversation.unarchive", actor="web",
                         chat_id=owner, ip=request.remote or "", detail={"conversationUuid": conv_uuid, "batch": True})
        return {"conversationUuid": conv_uuid, "ok": True}

    async def _batch_move(self, request: web.Request, owner: int, ids: list[str], body: dict[str, Any]) -> web.Response:
        target = str(body.get("targetFolderId") or "").strip()
        if target and target not in await self._tree_folders(owner):
            return web.json_response({"ok": False, "error": "folder_not_found"}, status=404)
        impact_only = body.get("impactOnly") is True
        update_snapshots = body.get("updateSnapshots") is True
        totals: dict[str, Any] = {key: 0 for key in _IMPACT_COUNTS}
        totals["cacheInvalidated"] = False
        updated = skipped = 0
        results, moved = [], []
        async with self._conversation_tree_lock:
            for conv_uuid in ids:
                row = await self._conversation_row(owner, conv_uuid)
                if row is None:
                    results.append(_failure(conv_uuid, "conversation_not_found"))
                    continue
                if str(row.get("folder_uuid") or "") == target:
                    results.append({"conversationUuid": conv_uuid, "ok": True, "unchanged": True})
                    continue
                impact = await self._tree_change_impact(owner, kind="conversation", item_id=conv_uuid, target_folder_id=target)
                for key in _IMPACT_COUNTS:
                    totals[key] += int(impact[key])
                totals["cacheInvalidated"] = totals["cacheInvalidated"] or impact["cacheInvalidated"]
                if impact_only:
                    continue

                async def mutate(conn: Any, item_id: str = conv_uuid) -> None:
                    await self._tree_reorder_entity(conn, owner=owner, kind="conversation", item_id=item_id,
                                                    target_folder=target, before_id="", after_id="")

                result = await self._tree_apply_snapshot_updates_locked(impact, update_snapshots=update_snapshots, mutate=mutate)
                updated += result["updatedCount"]
                skipped += result["skippedRunningCount"]
                moved.append(conv_uuid)
                results.append({"conversationUuid": conv_uuid, "ok": True})
        if impact_only:
            return web.json_response({"ok": True, **totals})
        if getattr(self, "collab_groups", None) is not None:
            for conv_uuid in moved:
                await self.collab_groups.follow_lead(conv_uuid, folder_uuid=target)
        self._invalidate_tree_status(owner)
        for conv_uuid in moved:
            await self.audit("web.conversation_tree.move", actor="web", chat_id=owner, ip=request.remote or "",
                             detail={"kind": "conversation", "id": conv_uuid, "targetFolderId": target, "batch": True})
        return web.json_response(self._batch_summary("move", results, **totals, updatedCount=updated, skippedRunningCount=skipped))
