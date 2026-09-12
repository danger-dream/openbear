"""Durable completion/read watermarks, independent of open browser sessions."""
from __future__ import annotations

import json
from typing import Any


def activity_fields(row: dict[str, Any]) -> dict[str, Any]:
    try:
        result = json.loads(row.get("activity_result_json") or "{}")
    except (TypeError, ValueError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    version = int(row.get("activity_version") or 0)
    read_version = int(row.get("activity_read_version") or 0)
    return {
        "activityVersion": version,
        "activityReadVersion": read_version,
        "activityUnread": version > read_version,
        "activityResult": result,
    }


async def record_completion(
    conn: Any, conversation_uuid: str, *, op_id: str, op_type: str,
    lifecycle: str, status: str, payload: dict[str, Any],
    previous: dict[str, Any] | None, revision: int, frame_seq: int, at_ms: int,
) -> None:
    # Only real execution boundaries, not tools, summary calls, stats or notices.
    # Repeated terminal enrichment must not turn an already-read result unread.
    if op_type not in {"run", "agent"} or lifecycle != "terminal":
        return
    if previous and str(previous.get("lifecycle") or "") == "terminal":
        return
    if op_type == "agent" and payload.get("merged"):
        return
    result = {
        "opId": op_id, "opRevision": revision, "frameSeq": frame_seq,
        "status": status or "completed", "atMs": at_ms,
    }
    await conn.execute(
        """UPDATE web_conversations SET activity_version=activity_version+1,
           activity_result_json=? WHERE conversation_uuid=?""",
        (json.dumps(result, ensure_ascii=False, separators=(",", ":")), conversation_uuid),
    )


async def clear_deleted_completion(conn: Any, conversation_uuid: str) -> None:
    """Deleting the result itself must not strand an unviewable unread badge."""
    await conn.execute(
        """UPDATE web_conversations SET activity_read_version=activity_version,
           activity_result_json='{}' WHERE conversation_uuid=?
           AND activity_version>activity_read_version
           AND NOT EXISTS (
             SELECT 1 FROM web_operations o WHERE o.conversation_uuid=web_conversations.conversation_uuid
               AND o.op_id=json_extract(web_conversations.activity_result_json,'$.opId')
           )""",
        (conversation_uuid,),
    )
