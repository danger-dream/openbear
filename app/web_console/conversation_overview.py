"""Read-only, bounded conversation overview. Never loads chat/tool material."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from app.models.thinking import normalize_think_level
from app.tools import processes

_ACTIVE = "'queued','running','pausing','paused','resuming','stopping','needs_openbear_control'"
_DURATION = """CASE WHEN json_valid(payload_json) THEN
    CASE WHEN json_type(payload_json,'$.durationMs') IN ('integer','real')
        AND json_extract(payload_json,'$.durationMs') >= 0
    THEN CAST(json_extract(payload_json,'$.durationMs') AS INTEGER) ELSE 0 END
    ELSE 0 END"""


def read_conversation_overview(db_path: str, owner: int, conversation_uuid: str) -> dict | None:
    """One consistent SQLite snapshot; only aggregate numbers and safe metadata."""
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        row = conn.execute(
            """SELECT conversation_uuid,internal_chat_id,title,folder_uuid,
            status,current_status,updated_at,archived_at,model FROM web_conversations
            WHERE conversation_uuid=? AND owner_chat_id=?""",
            (conversation_uuid, owner),
        ).fetchone()
        if row is None:
            return None
        conversation = dict(row)
        chat_id = int(row["internal_chat_id"])
        usage = conn.execute(
            """SELECT session_uuid,usage_input_tokens AS input_tokens,
            usage_output_tokens AS output_tokens,usage_cache_read_tokens AS cache_read_tokens,
            usage_cache_write_tokens AS cache_write_tokens,usage_cost_usd AS cost_usd,
            stat_total_time_ms_sum,thinking_level,fast_mode FROM sessions WHERE chat_id=?""",
            (chat_id,),
        ).fetchone()
        ledger = dict(usage) if usage else None
        settings = {
            "thinkingLevel": ledger.pop("thinking_level", "") if ledger else "",
            "fastRequested": bool(ledger.pop("fast_mode", False)) if ledger else False,
        }
        session_uuid = str((ledger or {}).get("session_uuid") or "")
        model = conn.execute(
            """SELECT COALESCE(SUM(model_call_count),0) AS calls,
            COALESCE(SUM(expert_tool_calls),0) AS legacy_agent_tools,
            COALESCE(SUM(model_retry_count),0) AS retries,
            COALESCE(SUM(model_fail_count),0) AS failures
            FROM model_calls WHERE chat_id=? AND session_uuid=?""",
            (chat_id, session_uuid),
        ).fetchone()
        direct_tools = conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE chat_id=? AND session_uuid=?",
            (chat_id, session_uuid),
        ).fetchone()[0]
        # Modern child tools live in task counters, NOT in the parent's tool_calls.
        # Legacy run-ledger counters are a fallback only if no task rows survive;
        # they describe the same work and must not be added to the task counters.
        tasks = conn.execute(
            f"""SELECT COUNT(*) AS count, COALESCE(SUM(tool_call_count),0) AS tools,
            SUM(CASE WHEN status IN ({_ACTIVE}) THEN 1 ELSE 0 END) AS active,
            MIN(CASE WHEN status IN ({_ACTIVE}) AND started_at>0 THEN started_at END) AS started
            FROM rath_tasks WHERE chat_id=? AND parent_session_uuid=?""",
            (chat_id, session_uuid),
        ).fetchone()
        ops = conn.execute(
            f"""SELECT COUNT(*) AS count,
            SUM(CASE WHEN op_type!='notice' AND lifecycle IN ('active','paused','waiting_control') THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN op_type='run' AND lifecycle IN ('active','paused','waiting_control') THEN 1 ELSE 0 END) AS foreground,
            SUM(CASE WHEN op_type='agent' AND lifecycle IN ('active','paused','waiting_control') THEN 1 ELSE 0 END) AS agents,
            SUM(CASE WHEN op_type!='notice' AND lifecycle='paused' THEN 1 ELSE 0 END) AS paused,
            SUM(CASE WHEN op_type!='notice' AND lifecycle='waiting_control' THEN 1 ELSE 0 END) AS waiting,
            MAX(CASE WHEN op_type!='notice' AND lifecycle IN ('active','paused','waiting_control') THEN updated_at_ms ELSE 0 END) AS latest_active,
            MIN(CASE WHEN op_type='run' AND lifecycle IN ('active','paused','waiting_control') AND created_at_ms>0 THEN created_at_ms END) AS started,
            CAST(TOTAL(CASE WHEN op_type='stats' THEN {_DURATION} ELSE 0 END) AS INTEGER) AS duration
            FROM web_operations WHERE conversation_uuid=?""",
            (conversation_uuid,),
        ).fetchone()
        # This is the existing chat header's legacy model-row fallback (the first
        # 500 detail rows). Full call COUNTS above are deliberately not capped.
        model_display_ms = conn.execute(
            """SELECT COALESCE(SUM(total_time_ms),0) FROM (
            SELECT total_time_ms FROM model_calls WHERE chat_id=? AND session_uuid=?
            ORDER BY created_at ASC,id ASC LIMIT 500)""",
            (chat_id, session_uuid),
        ).fetchone()[0]
        message_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=? AND role IN ('user','assistant')",
            (chat_id,),
        ).fetchone()[0]
        folders = {
            r["folder_uuid"]: r
            for r in conn.execute(
                "SELECT folder_uuid,parent_uuid,name FROM web_conversation_folders WHERE owner_chat_id=?",
                (owner,),
            )
        }
        names, seen, folder = [], set(), row["folder_uuid"]
        while folder and folder in folders and folder not in seen:
            seen.add(folder)
            names.append(folders[folder]["name"])
            folder = folders[folder]["parent_uuid"]
        return {
            "conversation": conversation,
            "settings": settings,
            "facts": {
                "hasOperations": bool(ops["count"]),
                "activeCount": int(ops["active"] or 0),
                "activeForegroundCount": int(ops["foreground"] or 0),
                "activeAgentCount": int(ops["agents"] or 0),
                "pausedCount": int(ops["paused"] or 0),
                "waitingControlCount": int(ops["waiting"] or 0),
                "latestActiveUpdatedAtMs": int(ops["latest_active"] or 0),
                "activeRathTaskCount": int(tasks["active"] or 0),
            },
            "operationStartedAtMs": int(ops["started"] or 0),
            "backgroundStartedAtMs": int(tasks["started"] or 0) * 1000,
            "path": " / ".join(reversed(names)) or "临时会话",
            "usage": ledger,
            "messageCount": int(message_count),
            "calls": {
                "model": int(model["calls"]),
                "tool": int(direct_tools)
                + int(tasks["tools"] if tasks["count"] else model["legacy_agent_tools"]),
                "retry": int(model["retries"]),
                "failed": int(model["failures"]),
            },
            "duration": {
                "timelineTotalDurationMs": int(ops["duration"]),
                "modelCallsMs": int(model_display_ms),
            },
        }


class WebAdminConversationOverviewMixin:
    async def _conversation_overview(
        self, owner: int, conversation_uuid: str
    ) -> dict[str, Any] | None:
        data = await asyncio.to_thread(
            read_conversation_overview, self.db.path, owner, conversation_uuid
        )
        if data is None:
            return None
        row, facts = data.pop("conversation"), data.pop("facts")
        # Reuse the existing conversation status rules without the heavyweight
        # chat payload or any reconciliation writes on a hover read.
        live = self._web_live_streams.get(conversation_uuid)
        summary = self._web_conversation_json(row, live=live, operation_facts=facts)
        active_processes = processes.active_for_chat(int(row["internal_chat_id"]))
        running = bool(summary["running"] or active_processes)
        started = 0
        if running:
            if live is not None and live.status == "running":
                started = int(live.started_at_ms or 0)
            started = started or data["operationStartedAtMs"] or data["backgroundStartedAtMs"]
            if not started and active_processes:
                started = min(int(proc.started_at * 1000) for proc in active_processes)
        # This block is explicitly the current conversation configuration, as in
        # the composer. Changes made during a run apply to the NEXT run; do not
        # substitute last_stats from an older model or claim an upstream tier.
        settings = data.pop("settings")
        model_label = (
            str(row.get("model") or "")
            or getattr(self.model_selection, "current", "")
            or self.config.models.primary
        )
        resolved = self.config.models.resolve(model_label)
        levels = self._model_thinking_levels(model_label)
        stored_thinking = normalize_think_level(settings["thinkingLevel"])
        thinking = (
            stored_thinking
            if stored_thinking in levels
            else self._model_default_thinking_level(model_label)
        )
        supports_fast = self._model_supports_fast(model_label)
        configuration = {
            "model": model_label,
            "modelName": (resolved[1].name or resolved[1].id) if resolved else model_label,
            "thinkingLevel": thinking if resolved else None,
            "supportsThinking": bool(levels) if resolved else None,
            "fastMode": bool(settings["fastRequested"] and supports_fast) if resolved else None,
            "supportsFast": supports_fast if resolved else None,
        }
        live_stats = getattr(live, "last_stats", None) or {}
        data["duration"]["liveMs"] = max(0, int(live_stats.get("durationMs") or 0))
        data.pop("operationStartedAtMs")
        data.pop("backgroundStartedAtMs")
        return {
            "conversationUuid": conversation_uuid,
            "title": row["title"] or "新会话",
            "archived": bool(row["archived_at"]),
            "running": running,
            "status": summary["currentStatus"] or ("运行中" if running else "就绪"),
            "startedAtMs": started or None,
            "configuration": configuration,
            **data,
        }
