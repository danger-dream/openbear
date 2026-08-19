"""Small SQLite compatibility migrations for existing development databases."""
from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite


def _json_with_close_reason(raw: str | None, reason: str) -> str:
    # Keep this dependency-free and conservative: existing metadata is best-effort.
    import json

    try:
        data: Any = json.loads(raw or "{}")
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data["closeReason"] = reason
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def remove_removed_tools_from_agent_allowlists(conn: aiosqlite.Connection) -> int:
    """Strip removed tool names from persisted Agent tool allowlists."""
    import json

    cur = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rath_agents' LIMIT 1"
    )
    if await cur.fetchone() is None:
        return 0
    cur = await conn.execute("SELECT id, tool_allowlist_json FROM rath_agents")
    rows = await cur.fetchall()
    changed = 0
    ts = int(time.time())
    removed = {"Glob", "Grep"}
    for row in rows:
        try:
            raw_tools = json.loads(row["tool_allowlist_json"] or "[]")
            if not isinstance(raw_tools, list):
                raw_tools = []
        except Exception:
            raw_tools = []
        tools: list[str] = []
        seen: set[str] = set()
        for raw in raw_tools:
            name = str(raw or "").strip()
            if not name or name in removed or name in seen:
                continue
            seen.add(name)
            tools.append(name)
        if tools != raw_tools:
            await conn.execute(
                "UPDATE rath_agents SET tool_allowlist_json=?, updated_at=? WHERE id=?",
                (json.dumps(tools, ensure_ascii=False, separators=(",", ":")), ts, int(row["id"])),
            )
            changed += 1
    return changed


async def backfill_web_operation_terminal_times(conn: aiosqlite.Connection) -> int:
    """Persist the first terminal boundary before old transport frames expire."""
    for table in ("web_operations", "web_event_frames"):
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        )
        if await cur.fetchone() is None:
            return 0
    cur = await conn.execute(
        """
        SELECT o.id, o.payload_json, MIN(f.created_at_ms) AS terminal_at_ms
        FROM web_operations AS o
        JOIN web_event_frames AS f
          ON f.conversation_uuid=o.conversation_uuid AND f.op_id=o.op_id
        WHERE o.lifecycle IN ('terminal', 'waiting_control')
          AND f.action IN ('end', 'error', 'cancel', 'stop')
        GROUP BY o.id
        """
    )
    changed = 0
    for row in await cur.fetchall():
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        if int(payload.get("terminalAtMs") or 0) > 0:
            continue
        terminal_at_ms = int(row["terminal_at_ms"] or 0)
        if terminal_at_ms <= 0:
            continue
        payload["terminalAtMs"] = terminal_at_ms
        await conn.execute(
            "UPDATE web_operations SET payload_json=?, revision=revision+1 WHERE id=?",
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), int(row["id"])),
        )
        changed += 1
    return changed


async def reconcile_web_operation_snapshot_frames(conn: aiosqlite.Connection) -> int:
    """Append a full snapshot frame when a legacy operation revision has no frame.

    Older startup recovery code updated ``web_operations`` directly.  A snapshot
    frame makes the append-only log replayable again without discarding the newer
    terminal snapshot.  The query is naturally idempotent: once the frame exists,
    MAX(frame.revision) equals the operation revision.
    """
    cur = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_operations' LIMIT 1"
    )
    if await cur.fetchone() is None:
        return 0
    cur = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_event_frames' LIMIT 1"
    )
    if await cur.fetchone() is None:
        return 0
    cur = await conn.execute(
        """
        SELECT o.*, COALESCE(MAX(f.revision), 0) AS max_frame_revision,
               COALESCE(c.owner_chat_id, 0) AS conversation_owner_chat_id
        FROM web_operations o
        LEFT JOIN web_event_frames f
          ON f.conversation_uuid=o.conversation_uuid AND f.op_id=o.op_id
        LEFT JOIN web_conversations c
          ON c.conversation_uuid=o.conversation_uuid
        GROUP BY o.id
        HAVING o.revision > COALESCE(MAX(f.revision), 0)
        ORDER BY o.conversation_uuid, o.display_seq, o.id
        """
    )
    rows = await cur.fetchall()
    if not rows:
        return 0
    next_frame_seq: dict[str, int] = {}
    inserted = 0
    now_ms = int(time.time() * 1000)
    for row in rows:
        conv_uuid = str(row["conversation_uuid"] or "")
        if conv_uuid not in next_frame_seq:
            seq_cur = await conn.execute(
                "SELECT COALESCE(MAX(frame_seq), 0) + 1 FROM web_event_frames WHERE conversation_uuid=?",
                (conv_uuid,),
            )
            seq_row = await seq_cur.fetchone()
            next_frame_seq[conv_uuid] = int(seq_row[0] if seq_row else 1)
        frame_seq = next_frame_seq[conv_uuid]
        next_frame_seq[conv_uuid] += 1
        debug = json.dumps(
            {
                "source": "legacy_snapshot_reconcile",
                "reason": "operation_revision_ahead_of_frame_log",
                "previousFrameRevision": int(row["max_frame_revision"] or 0),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await conn.execute(
            """
            INSERT INTO web_event_frames (
              conversation_uuid, internal_chat_id, owner_chat_id, frame_seq,
              op_id, op_type, action, turn_uuid, parent_turn_uuid, run_root_turn_uuid,
              target_type, target_id, task_uuid, run_id, revision, display_seq,
              payload_json, debug_json, created_at_ms, updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                conv_uuid,
                int(row["internal_chat_id"] or 0),
                int(row["conversation_owner_chat_id"] or 0),
                frame_seq,
                str(row["op_id"] or ""),
                str(row["op_type"] or ""),
                "snapshot",
                str(row["turn_uuid"] or ""),
                str(row["parent_turn_uuid"] or ""),
                str(row["run_root_turn_uuid"] or ""),
                str(row["target_type"] or ""),
                str(row["target_id"] or ""),
                str(row["task_uuid"] or ""),
                str(row["run_id"] or ""),
                int(row["revision"] or 0),
                int(row["display_seq"] or 0),
                str(row["payload_json"] or "{}"),
                debug,
                int(row["updated_at_ms"] or now_ms),
                now_ms,
            ),
        )
        inserted += 1
    return inserted


async def dedupe_active_rath_agent_sessions(conn: aiosqlite.Connection) -> int:
    """Close duplicate active Rath Agent Sessions before creating a partial unique index.

    A short race window existed before the unique index was introduced.  If a DB
    already contains duplicate active sessions for the same OpenBear session,
    workflow and agent, keep the most recently updated row and close the rest.
    """
    cur = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rath_agent_sessions' LIMIT 1"
    )
    if await cur.fetchone() is None:
        return 0
    cur = await conn.execute(
        """
        SELECT id, metadata_json FROM rath_agent_sessions
        WHERE status='active'
          AND id NOT IN (
            SELECT MAX(id)
            FROM rath_agent_sessions
            WHERE status='active'
            GROUP BY openbear_session_uuid, workflow_uuid, agent_key
          )
        """
    )
    rows = await cur.fetchall()
    if not rows:
        return 0
    ts = int(time.time())
    for row in rows:
        await conn.execute(
            """
            UPDATE rath_agent_sessions
            SET status='closed', metadata_json=?, updated_at=?, closed_at=?
            WHERE id=?
            """,
            (_json_with_close_reason(row["metadata_json"], "dedupe_active_unique_index"), ts, ts, int(row["id"])),
        )
    return len(rows)
