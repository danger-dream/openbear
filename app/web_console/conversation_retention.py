"""Automatic deletion of Webhook/Cron-created conversations beyond their trigger's retention."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from app.tools import processes
from app.web_console.core import _WEB_SESSION_KEY, log
from app.webhooks.contracts import ConversationRetention

SWEEP_INTERVAL_S = 60

# Only the trigger's own untouched output is managed. Pinned, archived, moved or
# human-continued conversations are kept and do not occupy the newest-N quota.
_MANAGED = """w.owner_chat_id=? AND COALESCE(w.folder_uuid,'')=? AND COALESCE(w.pinned_at,0)=0
  AND COALESCE(w.archived_at,0)=0 AND w.last_interaction_at_ms=0"""

_SOURCES = {
    "webhook": f"""
SELECT w.conversation_uuid, w.internal_chat_id, w.created_at,
  (a.state!='finalized' OR a.recovery_state!='healthy' OR a.notification_key IS NULL
   OR EXISTS(SELECT 1 FROM webhook_waits x WHERE x.assignment_id=a.assignment_id AND x.state IN ('registered','collecting','sealed'))
   OR EXISTS(SELECT 1 FROM webhook_stage_jobs j WHERE j.assignment_id=a.assignment_id AND j.state NOT IN ('succeeded','cancelled'))
   OR EXISTS(SELECT 1 FROM web_task_notifications n WHERE n.notification_key=a.notification_key AND n.state NOT IN ('delivered','suppressed'))
   OR EXISTS(SELECT 1 FROM webhook_notification_routes nr JOIN web_tg_notification_outbox o USING(root_turn_uuid)
             WHERE nr.notification_key=a.notification_key AND o.state IN ('pending','processing'))
   OR EXISTS(SELECT 1 FROM web_push_deliveries p WHERE p.event_key=a.notification_key AND p.state='pending')
   OR EXISTS(SELECT 1 FROM webhook_assignment_events m JOIN webhook_events e USING(event_id)
             WHERE m.assignment_id=a.assignment_id AND (e.terminal_at_ms IS NULL OR e.review_required=1))) AS busy
FROM web_conversations w JOIN webhook_assignments a ON a.assignment_id=w.origin_ref
WHERE w.origin_kind='webhook' AND a.origin_endpoint_id=? AND {_MANAGED}
ORDER BY w.created_at DESC, w.id DESC""",
    "cron": f"""
SELECT w.conversation_uuid, w.internal_chat_id, w.created_at,
  (r.phase!='finished' OR r.notification_state='pending'
   OR EXISTS(SELECT 1 FROM web_tg_notification_outbox o WHERE o.root_turn_uuid='cron-result:'||r.run_id AND o.state IN ('pending','processing'))
   OR EXISTS(SELECT 1 FROM web_push_deliveries p WHERE p.event_key='cron-result:'||r.run_id AND p.state='pending')) AS busy
FROM web_conversations w JOIN cron_runs r ON r.run_id=w.origin_ref
WHERE w.origin_kind='cron' AND r.job_id=? AND {_MANAGED}
ORDER BY w.created_at DESC, w.id DESC""",
}


def expired_rows(rows: list[dict[str, Any]], retention: ConversationRetention, now_s: int) -> list[dict[str, Any]]:
    """Rows beyond either limit, newest first; busy rows still count toward keepLast."""
    cutoff = now_s - retention.keep_days * 86400 if retention.keep_days is not None else None
    result = []
    for index, row in enumerate(rows):
        over_count = retention.keep_last is not None and index >= retention.keep_last
        too_old = cutoff is not None and int(row["created_at"] or 0) < cutoff
        if (over_count or too_old) and not row["busy"]:
            result.append(row)
    return result


def parse_retention(raw: Any) -> ConversationRetention | None:
    try:
        retention = ConversationRetention.model_validate(raw or {})
    except ValidationError:
        return None
    return retention if retention.active else None


class WebAdminConversationRetentionMixin:
    def _schedule_conversation_retention(self) -> None:
        wake = getattr(self, "_conversation_retention_wake", None)
        if wake is not None:
            wake.set()

    async def _conversation_retention_loop(self) -> None:
        from app.lifecycle import wait_for_startup
        await wait_for_startup(self)
        wake = self._conversation_retention_wake = asyncio.Event()
        while True:
            try:
                await asyncio.wait_for(wake.wait(), timeout=SWEEP_INTERVAL_S)
            except TimeoutError:
                pass
            wake.clear()
            try:
                await self._sweep_conversation_retention()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("自动清理触发器会话失败")

    async def _conversation_retention_sources(self) -> list[dict[str, Any]]:
        """Live triggers with an active retention: {kind, id, owner, folder, retention}."""
        rows: list[tuple[str, Any, Any]] = []
        cur = await self.db.conn.execute(
            """SELECT e.endpoint_id AS id, e.owner_chat_id AS owner, e.binding_uuid AS folder, r.config_json
               FROM webhook_endpoints e JOIN webhook_revisions r
                 ON r.endpoint_id=e.endpoint_id AND r.revision=e.current_revision
               WHERE e.deleted_at_ms IS NULL AND e.binding_kind='folder' AND r.target_mode='newConversation'""",
        )
        for row in await cur.fetchall():
            rows.append(("webhook", row, (json.loads(row["config_json"]).get("target") or {}).get("conversationRetention")))
        cur = await self.db.conn.execute(
            "SELECT job_id AS id, owner_chat_id AS owner, folder_id AS folder, config_json FROM cron_jobs WHERE deleted_at_ms IS NULL",
        )
        for row in await cur.fetchall():
            rows.append(("cron", row, json.loads(row["config_json"]).get("conversationRetention")))
        sources = []
        for kind, row, raw in rows:
            retention = parse_retention(raw)
            if retention is not None:
                sources.append({"kind": kind, "id": str(row["id"]), "owner": int(row["owner"]),
                                "folder": str(row["folder"] or ""), "retention": retention})
        return sources

    async def _conversation_retention_candidates(
        self, kind: str, source_id: str, owner: int, folder: str, retention: ConversationRetention, now_s: int,
    ) -> list[dict[str, Any]]:
        cur = await self.db.conn.execute(_SOURCES[kind], (source_id, owner, folder))
        return expired_rows([dict(row) for row in await cur.fetchall()], retention, now_s)

    async def _sweep_conversation_retention(self) -> int:
        deleted = 0
        now_s = int(time.time())
        for source in await self._conversation_retention_sources():
            candidates = await self._conversation_retention_candidates(
                source["kind"], source["id"], source["owner"], source["folder"], source["retention"], now_s,
            )
            for row in candidates:
                if await self._retention_delete(source, row):
                    deleted += 1
        if deleted:
            log.info("已按保留策略清理触发器会话", 数量=deleted)
        return deleted

    async def _retention_delete(self, source: dict[str, Any], row: dict[str, Any]) -> bool:
        conv_uuid = str(row["conversation_uuid"])
        internal_chat_id = int(row["internal_chat_id"])
        async with self.operation_locks.try_chat(internal_chat_id, "web_conversation_delete") as acquired:
            if not acquired:
                return False
            # Deletion would stop live work; anything still active waits for a later sweep.
            if self.runs is not None and self.runs.is_running(internal_chat_id):
                return False
            if self.agents is not None and await self.agents.all_controllable_tasks_for_chat(internal_chat_id):
                return False
            if processes.active_for_chat(internal_chat_id):
                return False
            cur = await self.db.conn.execute(
                f"SELECT 1 FROM web_conversations w WHERE w.conversation_uuid=? AND w.status!='running' AND {_MANAGED}",
                (conv_uuid, source["owner"], source["folder"]),
            )
            if await cur.fetchone() is None:
                return False
            result = await self._delete_web_conversation_locked(
                owner_chat_id=source["owner"], conv_uuid=conv_uuid, internal_chat_id=internal_chat_id,
            )
        if not result.get("ok"):
            return False
        self._invalidate_tree_status(source["owner"])
        await self.audit(
            "web.conversation.auto_delete",
            actor="retention",
            chat_id=source["owner"],
            detail={**result["auditDetail"], "source": source["kind"], "sourceId": source["id"],
                    "retention": source["retention"].wire()},
        )
        return True

    async def handle_api_conversation_retention_preview(self, request: web.Request) -> web.Response:
        """Count what saving this retention would delete now; nothing is changed."""
        session = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        body = await self._json_body(request)
        kind = str(body.get("kind") or "")
        source_id = str(body.get("id") or "").strip()
        if kind not in _SOURCES:
            return web.json_response({"ok": False, "error": "invalid_kind"}, status=400)
        try:
            retention = ConversationRetention.model_validate(body.get("retention") or {})
        except ValidationError as error:
            return web.json_response({"ok": False, "error": "invalid_retention", "message": str(error)}, status=400)
        if kind == "webhook":
            cur = await self.db.conn.execute(
                "SELECT binding_uuid FROM webhook_endpoints WHERE endpoint_id=? AND owner_chat_id=? AND deleted_at_ms IS NULL AND binding_kind='folder'",
                (source_id, owner),
            )
        else:
            cur = await self.db.conn.execute(
                "SELECT folder_id FROM cron_jobs WHERE job_id=? AND owner_chat_id=? AND deleted_at_ms IS NULL",
                (source_id, owner),
            )
        found = await cur.fetchone()
        count = 0
        if found is not None and retention.active:
            count = len(await self._conversation_retention_candidates(
                kind, source_id, owner, str(found[0] or ""), retention, int(time.time()),
            ))
        return web.json_response({"ok": True, "count": count})
