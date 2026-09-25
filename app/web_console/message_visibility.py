"""User-controlled presentation only. Never mutate transcript or operation payloads."""
from __future__ import annotations

import time

from aiohttp import web

from app.web_operations import operation_public

VISIBILITY_TYPES = frozenset({
    "user_message", "assistant_message", "reasoning", "tool", "agent",
    "user_interaction", "context_compaction", "model_retry", "notice",
})


async def visibility_snapshot(db, conversation_uuid: str) -> dict:
    # One SELECT provides an internally consistent revision + item snapshot.
    cur = await db.conn.execute(
        """SELECT s.revision, o.op_id, o.op_type, o.created_at_ms, o.display_seq
           FROM web_visibility_state s
           LEFT JOIN web_hidden_operations h ON h.conversation_uuid=s.conversation_uuid
           LEFT JOIN web_operations o ON o.conversation_uuid=h.conversation_uuid AND o.op_id=h.op_id
           WHERE s.conversation_uuid=? ORDER BY o.display_seq, o.id""",
        (conversation_uuid,),
    )
    rows = await cur.fetchall()
    items = [{"opId": r["op_id"], "type": r["op_type"], "createdAtMs": r["created_at_ms"]}
             for r in rows if r["op_id"]]
    return {"conversationUuid": conversation_uuid, "revision": int(rows[0]["revision"]) if rows else 0,
            "hiddenIds": [item["opId"] for item in items], "items": items}


class WebAdminMessageVisibilityMixin:
    async def handle_api_message_visibility(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        return web.json_response({"ok": True, "visibility": await visibility_snapshot(self.db, row["conversation_uuid"])})

    async def handle_api_message_visibility_update(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        conv = str(row["conversation_uuid"])
        body = await self._json_body(request)
        restore_all = body.get("restoreAll") is True
        hidden = body.get("hidden")
        ids = body.get("opIds", [])
        if (not isinstance(ids, list) or len(ids) > 1000
                or any(not isinstance(i, str) or not i or len(i) > 512 for i in ids)
                or (restore_all and (ids or hidden is True))
                or (not restore_all and (not ids or not isinstance(hidden, bool)))):
            return web.json_response({"ok": False, "error": "invalid_visibility_request"}, status=400)
        ids = list(dict.fromkeys(ids))
        async with self._web_operation_lock(conv):
            async with self.db.conn.transaction(label="message-visibility") as conn:
                if restore_all:
                    await conn.execute("DELETE FROM web_hidden_operations WHERE conversation_uuid=?", (conv,))
                elif hidden:
                    slots = ",".join("?" for _ in ids)
                    cur = await conn.execute(
                        f"SELECT op_id, op_type, internal, payload_json FROM web_operations WHERE conversation_uuid=? AND op_id IN ({slots})",
                        (conv, *ids),
                    )
                    rows = await cur.fetchall()
                    if len(rows) != len(ids) or any(r["internal"] or r["op_type"] not in VISIBILITY_TYPES for r in rows):
                        raise web.HTTPNotFound(text="message_not_found")
                    for op_id in ids:
                        await conn.execute(
                            "INSERT OR IGNORE INTO web_hidden_operations(conversation_uuid, op_id, hidden_at_ms) VALUES(?,?,?)",
                            (conv, op_id, int(time.time() * 1000)),
                        )
                else:
                    # Restoring an already restored/deleted item is idempotent.
                    slots = ",".join("?" for _ in ids)
                    await conn.execute(f"DELETE FROM web_hidden_operations WHERE conversation_uuid=? AND op_id IN ({slots})", (conv, *ids))
                snapshot = await visibility_snapshot(self.db, conv)
        # This event is not a model/UI operation and never enters model history.
        await self._live_for(row).publish({"type": "message_visibility.changed", "visibility": snapshot}, persist=False)
        return web.json_response({"ok": True, "visibility": snapshot})

    async def handle_api_hidden_message_preview(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        cur = await self.db.conn.execute(
            """SELECT o.* FROM web_operations o JOIN web_hidden_operations h
               ON h.conversation_uuid=o.conversation_uuid AND h.op_id=o.op_id
               WHERE o.conversation_uuid=? AND o.op_id=?""",
            (row["conversation_uuid"], request.match_info["operation_id"]),
        )
        operation = await cur.fetchone()
        if operation is None:
            raise web.HTTPNotFound(text="hidden_message_not_found")
        return web.json_response({"ok": True, "operation": operation_public(dict(operation), include_tool_details=True)})
