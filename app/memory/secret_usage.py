"""Fill memory_secrets.last_used_at once from the usage recorded before the column existed.

Model reads of a credential are Memory(resource=secret, action=get) tool calls in the Web
operation log and credential references sent with a message. Later reads update the
column directly.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from app.logging import get_logger

log = get_logger("memory")

BACKFILL_KEY = "memory_secrets.last_used_backfill.v1"


def scan(db_path: str) -> dict[str, int]:
    """Latest use per secret, keyed "id:<n>" or "name:<name>", in unix seconds."""
    used: dict[str, int] = {}

    def note(key: str, ts: int) -> None:
        if ts > used.get(key, 0):
            used[key] = ts

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT payload_json, created_at_ms FROM web_operations WHERE op_type='tool' "
            "AND payload_json LIKE '%\"toolName\":\"Memory\"%' AND payload_json LIKE '%secret%'"
        )
        for payload, created_ms in rows:
            try:
                args = json.loads(json.loads(payload).get("args") or "{}")
            except (TypeError, ValueError, AttributeError):
                continue
            if not isinstance(args, dict) or args.get("resource") != "secret" or args.get("action") != "get":
                continue
            ts = int(created_ms or 0) // 1000
            if str(args.get("id") or "0").isdigit() and int(args.get("id") or 0):
                note(f"id:{int(args['id'])}", ts)
            elif str(args.get("name") or "").strip():
                note("name:" + str(args["name"]).removeprefix("@secret/").strip(), ts)
        for manifest, created in conn.execute(
            "SELECT manifest_json, created_at FROM web_reference_bundles WHERE manifest_json LIKE '%secret%'"
        ):
            try:
                refs = json.loads(manifest)
            except (TypeError, ValueError):
                continue
            for ref in refs if isinstance(refs, list) else []:
                if isinstance(ref, dict) and ref.get("kind") == "secret" and ref.get("mode") != "mention" and str(ref.get("id") or "").isdigit():
                    note(f"id:{int(ref['id'])}", int(created or 0))
    finally:
        conn.close()
    return used


async def backfill(db: Any) -> None:
    cur = await db.conn.execute("SELECT 1 FROM app_state WHERE key=?", (BACKFILL_KEY,))
    if await cur.fetchone() is not None:
        return
    used = await asyncio.to_thread(scan, str(db.path))
    for key, ts in used.items():
        kind, _, value = key.partition(":")
        column = "id" if kind == "id" else "name"
        await db.conn.execute(
            f"UPDATE memory_secrets SET last_used_at=? WHERE {column}=? AND last_used_at<?",
            (ts, int(value) if column == "id" else value, ts),
        )
    await db.conn.execute(
        "INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (BACKFILL_KEY, json.dumps({"secrets": len(used)})),
    )
    await db.conn.commit()
    log.info("凭证最近使用时间已从历史记录回填", 条目=len(used))
