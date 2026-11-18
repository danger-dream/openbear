"""Optional persona copies from resources shipped with this installation."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db.engine import now_ts

_PERSONA_DIR = Path(__file__).resolve().parents[2] / "prompts" / "personas"


@dataclass(frozen=True, slots=True)
class Persona:
    key: str
    name: str
    content: str
    digest: str


def bundled_personas() -> list[Persona]:
    manifest = json.loads((_PERSONA_DIR / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or not isinstance(manifest.get("personas"), list):
        raise ValueError("invalid persona manifest")
    result = []
    keys = set()
    for item in manifest["personas"]:
        if not isinstance(item, dict):
            raise ValueError("invalid persona entry")
        key, name, digest = item.get("id"), item.get("name"), item.get("sha256")
        if (not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", key)
                or key in keys or not isinstance(name, str) or not name.strip()
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("invalid persona entry")
        raw = (_PERSONA_DIR / f"{key}.tpl").read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"persona checksum mismatch: {key}")
        content = raw.decode("utf-8")
        if not content.strip():
            raise ValueError(f"empty persona: {key}")
        keys.add(key)
        result.append(Persona(key, name, content, digest))
    return result


async def _existing(conn: Any, persona: Persona) -> tuple[Any, bool]:
    cur = await conn.execute(
        "SELECT t.* FROM memory_persona_imports i JOIN memory_templates t ON t.id=i.template_id"
        " WHERE i.source_key=? AND i.content_hash=?", (persona.key, persona.digest))
    row = await cur.fetchone()
    if row is not None:
        return row, True
    cur = await conn.execute(
        "SELECT * FROM memory_templates WHERE name=? AND content=? AND is_persona=1"
        " AND COALESCE(is_active,0)=0 AND COALESCE(is_agent_active,0)=0 ORDER BY id LIMIT 1",
        (persona.name, persona.content))
    return await cur.fetchone(), False


async def catalog(db: Any, personas: list[Persona]) -> list[dict[str, Any]]:
    items = []
    for persona in personas:
        row, _ = await _existing(db.conn, persona)
        if row is not None:
            modified = (row["name"] != persona.name or row["content"] != persona.content
                        or not row["is_persona"] or bool(row["is_active"]) or bool(row["is_agent_active"]))
            status = "modified" if modified else "existing"
        else:
            cur = await db.conn.execute(
                "SELECT 1 FROM memory_persona_imports i JOIN memory_templates t ON t.id=i.template_id"
                " WHERE i.source_key=? LIMIT 1", (persona.key,))
            status = "update" if await cur.fetchone() else "available"
        items.append({"id": persona.key, "name": persona.name, "sha256": persona.digest,
                      "chars": len(persona.content), "status": status,
                      "templateId": int(row["id"]) if row is not None else None})
    return items


async def sync(db: Any, personas: list[Persona]) -> dict[str, Any]:
    items = []
    async with db.write_transaction(label="memory-persona-sync") as conn:
        for persona in personas:
            row, tracked = await _existing(conn, persona)
            created = row is None
            if created:
                cur = await conn.execute(
                    "INSERT INTO memory_templates (name,content,is_active,is_agent_active,is_persona,updated_at)"
                    " VALUES (?,?,0,0,1,?)", (persona.name, persona.content, now_ts()))
                template_id = int(cur.lastrowid)
            else:
                template_id = int(row["id"])
            if not tracked:
                # Import identity survives edits; a new resource revision gets a separate copy.
                await conn.execute(
                    "INSERT INTO memory_persona_imports (source_key,content_hash,template_id,imported_at)"
                    " VALUES (?,?,?,?) ON CONFLICT(source_key,content_hash) DO UPDATE"
                    " SET template_id=excluded.template_id,imported_at=excluded.imported_at",
                    (persona.key, persona.digest, template_id, now_ts()))
            items.append({"id": persona.key, "name": persona.name, "templateId": template_id,
                          "status": "created" if created else "existing"})
    created = sum(item["status"] == "created" for item in items)
    return {"ok": True, "items": items, "created": created, "reused": len(items) - created}
