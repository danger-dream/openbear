# ruff: noqa: F401
"""OpenBear 内置 Web 管理服务。

Phase 4 已完成 Secret Key + Telegram 二次确认登录；Phase 5 在同一个
轻量 aiohttp 管理台里接入会话历史浏览、详情、恢复与导出。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web

from app.admin import channels as channel_admin
from app.admin import settings as settings_admin
from app.agent import steering
from app.agent.compaction import (
    CompactionAccountingError,
    CompactionOutcome,
    Compactor,
    CompressionCandidate,
)
from app.agent.loop import Agent
from app.agent.result import RunResult
from app.agent.transcript_repair import (
    build_summary_prefixed_history,
    build_summary_prefixed_visible_history,
    repair_tool_pairing,
)
from app.config import Config, fast_request_mode
from app.context.builder import build_system_prompt_params
from app.control_actions import (
    schedule_openbear_restart,
    schedule_openbear_restart_with_completion_notice,
)
from app.db.dao import MessageDAO, SummaryDAO, WebConversationDefaultsDAO
from app.db.engine import DB, now_ts
from app.llm.base import Message
from app.llm.events import Usage
from app.logging import get_logger
from app.media.attachments import (
    InboundMedia,
    build_llm_content,
    build_llm_text_with_media,
    build_user_text_with_media,
)
from app.media.attachments import (
    classify_media as _classify_document,
)
from app.media.attachments import (
    download_root as _download_root,
)
from app.media.attachments import (
    extract_text as _extract_text,
)
from app.media.attachments import (
    guess_mime as _guess_mime,
)
from app.media.attachments import (
    human_size as _human_size,
)
from app.media.attachments import (
    is_text_file as _is_text_file,
)
from app.media.attachments import (
    mime_ext as _mime_ext,
)
from app.media.attachments import (
    safe_filename as _safe_filename,
)
from app.media.attachments import (
    size_limit_bytes as _size_limit_bytes,
)
from app.memory.builtin import BuiltinMemoryClient, slugify_ref
from app.model_cost import resolved_usage_cost_usd as _resolved_usage_cost_usd
from app.model_cost import usage_cost_usd as _usage_cost_usd
from app.models.agent_runtime import (
    agent_run_config_public,
    resolve_agent_runtime_config,
)
from app.models.thinking import (
    configured_default_think_level,
    normalize_think_level,
    normalize_think_levels,
)
from app.operation_locks import ChatOperationLocks
from app.rath.builtin_workflows import SINGLE_AGENT_WORKFLOW_SLUG
from app.rath.dao import RathDAO
from app.rath.manager import RathTaskManager
from app.rath.single_agent import (
    SingleAgentWorkflowRunner,
    agent_to_snapshot,
    safe_agent_llm_session_id,
)
from app.tools import processes
from app.tools.allowlist import sanitize_tool_allowlist
from app.tools.base import ToolRuntimeContext, max_tool_result_chars
from app.tools.file_state import clear_read_file_state
from app.turn_stats import build_turn_stats_card
from app.utils import estimate_tokens, now_cn
from app.web_operations import (
    _operation_target_fields,
    frame_payload_for_action,
    frame_public,
    operation_public,
    reduce_operation_payload,
    status_lifecycle,
    web_event_operation_specs,
)
from app.web_operations import (
    json_dumps as operation_json_dumps,
)
from app.web_operations import (
    json_loads_dict as operation_json_loads_dict,
)
from app.web_operations import (
    json_loads_list as operation_json_loads_list,
)

log = get_logger("web_admin")

_COOKIE = "openbear_web_session"
_LOGIN_NONCE_COOKIE = "openbear_web_login_nonce"
_STATE_WEB_SECRET = "web_secret_key"
_LOGIN_FAIL_LIMIT = 5


@dataclass(slots=True)
class WebSession:
    chat_id: int
    expires_at: int


@dataclass(slots=True)
class _ChannelTestJob:
    job_uuid: str
    owner_chat_id: int
    provider: str
    model_ids: list[str]
    started_at: int
    updated_at: int
    status: str = "queued"
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    task: asyncio.Task[Any] | None = field(default=None, repr=False)


class _MCPServerNotFoundError(ValueError):
    """Internal sentinel for safe MCP server-not-found API responses."""


_WEB_SESSION_KEY = web.RequestKey("web_session", WebSession)
_WEB_FRONTEND_EVENT_LOG_DIR = Path(os.environ.get("OPENBEAR_WEB_FRONTEND_EVENT_LOG_DIR") or "logs/web-frontend-events")
_WEB_WS_AUDIT_LOG_DIR = Path(os.environ.get("OPENBEAR_WEB_WS_AUDIT_LOG_DIR") or "logs/web-ws-audit")
_WEB_DEBUG_FILE_LOGS_ENABLED = os.environ.get("OPENBEAR_WEB_DEBUG_FILE_LOGS") == "1"


def _log_web_frontend_event(record: dict[str, Any]) -> None:
    """Append Web frontend debug records when explicitly enabled.

    Full-fidelity frontend event JSONL logs are very large, so file logging is
    disabled by default. Set OPENBEAR_WEB_DEBUG_FILE_LOGS=1 for temporary UI
    debugging.
    """
    if not _WEB_DEBUG_FILE_LOGS_ENABLED:
        return
    try:
        ts_ms = int(time.time() * 1000)
        payload = {
            "tsMs": ts_ms,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + f".{ts_ms % 1000:03d}Z",
            "pid": os.getpid(),
            **record,
        }
        _WEB_FRONTEND_EVENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _WEB_FRONTEND_EVENT_LOG_DIR / f"{time.strftime('%Y-%m-%d', time.gmtime(ts_ms / 1000))}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")))
            fh.write("\n")
    except Exception:
        # Never let debug logging affect production event delivery.
        pass


def _log_web_ws_audit(record: dict[str, Any]) -> None:
    """Append exact WebSocket push debug records when explicitly enabled.

    Full-fidelity WebSocket JSONL logs are very large and duplicate data across
    by-day/by-conversation indexes, so file logging is disabled by default. Set
    OPENBEAR_WEB_DEBUG_FILE_LOGS=1 for temporary UI debugging.
    """
    if not _WEB_DEBUG_FILE_LOGS_ENABLED:
        return
    try:
        ts_ms = int(time.time() * 1000)
        day = time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000))
        payload = {
            "tsMs": ts_ms,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + f".{ts_ms % 1000:03d}Z",
            "pid": os.getpid(),
            **record,
        }
        line = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        _WEB_WS_AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        (_WEB_WS_AUDIT_LOG_DIR / "by-day").mkdir(parents=True, exist_ok=True)
        with (_WEB_WS_AUDIT_LOG_DIR / "by-day" / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line)
        conv = str(record.get("conversationUuid") or "").strip()
        if conv:
            conv_dir = _WEB_WS_AUDIT_LOG_DIR / "by-conversation" / re.sub(r"[^A-Za-z0-9_.-]+", "_", conv)
            conv_dir.mkdir(parents=True, exist_ok=True)
            with (conv_dir / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _origin_key(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"//{value}", scheme="http")
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    port = parsed.port
    default_port = 443 if scheme == "https" else 80
    rendered_port = "" if port in {None, default_port} else f":{port}"
    return f"{scheme}://{host}{rendered_port}"


def _safe_upload_name(name: str, *, fallback: str = "upload.bin") -> str:
    return _safe_filename(name, fallback=fallback)


def _human_bytes(n: int) -> str:
    return _human_size(n)


def _web_media_kind(filename: str, mime: str) -> str:
    return _classify_document(filename, mime)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))





def _parse_ts(value: str | None) -> int:
    if not value:
        return 0
    raw = str(value).strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    from datetime import UTC, datetime

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())




def _usage_json(usage) -> dict[str, int]:
    return {
        "inputTokens": int(getattr(usage, "input_tokens", 0) or 0),
        "outputTokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cacheReadTokens": int(getattr(usage, "cache_read_tokens", 0) or 0),
        "cacheWriteTokens": int(getattr(usage, "cache_write_tokens", 0) or 0),
        "totalTokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _usage_sum(*items: Usage) -> Usage:
    total = Usage()
    for item in items:
        if item is not None:
            total.merge(item)
    return total


_WEB_OPERATION_STABLE_EVENT_KINDS = {"delta", "status", "agent_supervision", "tool_update", "tool_progress", "stats"}


def _web_operation_event_key(payload: dict[str, Any], kind: str | None = None) -> str:
    """Return a stable identity key for high-frequency Web operation events.

    Streaming snapshots carry the full current text/reasoning/status.  They must
    update the same operation instead of creating duplicate assistant/tool/status
    cards on long answers or reconnects.  The key is scoped to a turn so a new
    user message starts a fresh draft.
    """
    typ = str(kind or payload.get("type") or payload.get("kind") or "").strip()
    if typ not in _WEB_OPERATION_STABLE_EVENT_KINDS:
        return ""
    turn_uuid = str(payload.get("turnUuid") or payload.get("turn_uuid") or "").strip() or "turn"
    tool_call_id = str(payload.get("toolCallId") or payload.get("tool_call_id") or "").strip()
    event_key = str(payload.get("eventKey") or payload.get("event_key") or "").strip()
    if typ == "delta":
        return f"{turn_uuid}:delta:{event_key or 'assistant:draft'}"
    if typ == "status":
        return f"{turn_uuid}:status"
    if typ == "agent_supervision":
        root_turn_uuid = str(payload.get("runRootTurnUuid") or payload.get("run_root_turn_uuid") or turn_uuid).strip()
        return f"{root_turn_uuid}:agent_supervision"
    if typ == "tool_progress":
        return f"{turn_uuid}:tool_progress:{tool_call_id or event_key or 'current'}"
    if typ == "tool_update":
        # tool_update often contains only a changing human line. Treat it as
        # one latest preview per turn/tool instead of one row per repaint.
        return f"{turn_uuid}:tool_update:{tool_call_id or 'current'}"
    if typ == "stats":
        return f"{turn_uuid}:stats"
    return ""


def _web_operation_event_uuid(conversation_uuid: str, event_key: str) -> str:
    digest = hashlib.sha1(event_key.encode("utf-8", "ignore")).hexdigest()
    return f"{conversation_uuid}:event:{digest}"

__all__ = [name for name in globals() if not name.startswith("__")]
