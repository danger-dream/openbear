"""Durable failure stop and exactly-once notification transcript receipts.

A model turn already owns its retry budget. Its failed/cancelled outcome is not
permission for the outbox to start another turn. Paused results stay available
until a real user message takes ownership; normal crash recovery stays separate.
"""
from __future__ import annotations

import json
from typing import Any

from app.context.window import mark_source, source_of
from app.db.dao import MessageDAO
from app.db.engine import DB, now_ts
from app.utils import estimate_tokens


def notification_ids(payload: dict[str, Any]) -> set[str]:
    ids = payload.get("_notificationUuids")
    values = list(ids) if isinstance(ids, list) else []
    values.append(payload.get("_notificationUuid"))
    return {str(value) for value in values if value}


def notification_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("results") if payload.get("batched") else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else [payload]


async def pause_notifications(db: DB, ids: set[str], error: str) -> None:
    if not ids:
        return
    async with db.write_transaction(label="pause-failed-notifications") as conn:
        await conn.execute(
            f"""UPDATE web_task_notifications SET state='paused', claim_token='', claimed_at=0,
                next_attempt_at=0, last_error=?, updated_at=?
                WHERE notification_uuid IN ({','.join('?' for _ in ids)}) AND state IN ('pending','processing')""",
            (str(error or "controller turn failed; awaiting user continuation")[:2000], now_ts(), *sorted(ids)),
        )


async def claim_paused_notifications(db: DB, conversation_uuid: str) -> list[dict[str, Any]]:
    """Only the foreground real-user turn may call this; never a recovery scan."""
    async with db.write_transaction(label="user-claims-paused-notifications") as conn:
        cur = await conn.execute(
            "SELECT notification_uuid,payload_json FROM web_task_notifications WHERE conversation_uuid=? AND state='paused' ORDER BY id",
            (conversation_uuid,),
        )
        result = []
        for row in await cur.fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            payload["_notificationUuid"] = row["notification_uuid"]
            payload["_notificationUuids"] = [row["notification_uuid"]]
            result.append(payload)
        if result:
            await conn.execute(
                "UPDATE web_task_notifications SET state='processing', claim_token='user-turn', claimed_at=?, updated_at=? WHERE conversation_uuid=? AND state='paused'",
                (now_ts(), now_ts(), conversation_uuid),
            )
        return result


async def bind_notification_receipts(conn: Any, conversation_uuid: str, ids: set[str], message_id: int) -> None:
    if not ids:
        return
    await conn.execute(
        f"""UPDATE web_task_notifications
            SET payload_json=json_set(payload_json, '$._contextMessageId', ?)
            WHERE conversation_uuid=? AND notification_uuid IN ({','.join('?' for _ in ids)})
              AND state='processing'""",
        (message_id, conversation_uuid, *sorted(ids)),
    )


async def notification_context_messages(
    db: DB, *, chat_id: int, conversation_uuid: str, turn_uuid: str,
    payloads: list[dict[str, Any]], history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist each notification body and receipt together, never replay it.

    If the original has left the selected context, supply its exact history
    locator, not a new copy of an arbitrarily large completed result.
    """
    known = {str(source_of(message).get("id") or "") for message in history}
    out = []
    messages = MessageDAO(db)
    async with db.write_transaction(label="notification-transcript-receipts") as conn:
        for payload in payloads:
            ids = notification_ids(payload)
            receipt = 0
            if ids:
                cur = await conn.execute(
                    f"""SELECT m.id FROM web_task_notifications n JOIN messages m
                        ON m.id=json_extract(n.payload_json,'$._contextMessageId') AND m.chat_id=n.internal_chat_id
                        WHERE n.conversation_uuid=? AND n.notification_uuid IN ({','.join('?' for _ in ids)})
                        ORDER BY m.id LIMIT 1""",
                    (conversation_uuid, *sorted(ids)),
                )
                row = await cur.fetchone()
                receipt = int(row["id"]) if row else 0
            if receipt:
                await bind_notification_receipts(conn, conversation_uuid, ids, receipt)
                if f"message:{receipt}" in known:
                    continue
                source_id = f"notification-resume:{receipt}"
                if source_id in known:
                    continue
                content = (
                    "[Pending background result already recorded]\n"
                    f"Task: {payload.get('taskUuid') or payload.get('jobId') or ''}. "
                    f"Original result: History(source=execution, action=read_event, eventId=message:{receipt}). "
                    "Use the retained context/summary first and retrieve only missing material. "
                    "The user's current instruction takes priority; do not restart completed work."
                )
                out.append(mark_source({"role": "user", "content": content}, kind="notification", source_id=source_id))
                known.add(source_id)
                continue
            content = str(payload.get("content") or "").strip()
            if not content:
                content = json.dumps({k: v for k, v in payload.items() if not k.startswith("_")}, ensure_ascii=False, default=str)
            receipt = await messages.add(chat_id, "user", content, conversation_uuid=conversation_uuid,
                turn_uuid=turn_uuid, run_root_turn_uuid=turn_uuid, tokens=estimate_tokens(content), commit=False)
            await bind_notification_receipts(conn, conversation_uuid, ids, receipt)
            source_id = f"message:{receipt}"
            out.append(mark_source({"role": "user", "content": content}, kind="notification",
                source_id=source_id, message_id=receipt, turn_uuid=turn_uuid))
            known.add(source_id)
    return out
