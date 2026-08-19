"""MCP audit helpers.

v0 keeps audit best-effort and schema-free by reusing the existing audit_logs table
when an async DB connection is available from Services.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.logging import get_logger

log = get_logger("mcp.audit")


def safe_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    # Import lazily to avoid a hard cycle: output.py does not depend on audit.py.
    from app.mcp.output import redact_secrets

    redacted = redact_secrets(detail or {})
    return redacted if isinstance(redacted, dict) else {"value": redacted}


async def record_audit(
    db: Any,
    kind: str,
    *,
    actor: str = "system",
    chat_id: int = 0,
    ip: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    if db is None or getattr(db, "conn", None) is None:
        return
    try:
        await db.conn.execute(
            "INSERT INTO audit_logs (kind, actor, chat_id, ip, detail_json, created_at) VALUES (?,?,?,?,?,?)",
            (
                kind,
                actor,
                int(chat_id or 0),
                ip,
                json.dumps(safe_detail(detail), ensure_ascii=False, separators=(",", ":"), default=str),
                int(time.time()),
            ),
        )
        await db.conn.commit()
    except Exception:
        log.exception("mcp.audit.failed", kind=kind)
