"""prompt-memory → OpenBear 内置记忆导入器。"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.db.engine import DB, now_ts
from app.memory.builtin import BuiltinMemoryClient, slugify_ref


@dataclass(slots=True)
class ImportReport:
    ok: bool
    dry_run: bool
    identity: str
    source_path: str
    counts: dict[str, int] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    backup_path: str = ""
    operation_uuid: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dryRun": self.dry_run,
            "identity": self.identity,
            "sourcePath": self.source_path,
            "counts": self.counts,
            "conflicts": self.conflicts,
            "backupPath": self.backup_path,
            "operationUuid": self.operation_uuid,
            "error": self.error,
        }


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _safe_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _source_has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return bool(row)


def _visible_ids(conn: sqlite3.Connection, resource_type: str, identity_id: int) -> set[int]:
    if not _source_has_table(conn, "resource_acl"):
        return set()
    rows = conn.execute(
        """
        SELECT resource_id FROM resource_acl
        WHERE resource_type=? AND (subject_type='all' OR (subject_type='identity' AND identity_id=?))
        """,
        (resource_type, identity_id),
    ).fetchall()
    return {int(r["resource_id"]) for r in rows}


def _unique_name(existing: set[str], base: str) -> str:
    base = base or "imported"
    candidate = base
    idx = 2
    while candidate in existing:
        candidate = f"{base}-import-{idx}"
        idx += 1
    existing.add(candidate)
    return candidate


class PromptMemoryImporter:
    def __init__(self, db: DB) -> None:
        self.db = db
        self.mem = BuiltinMemoryClient(db)

    async def import_file(
        self,
        source_path: str | Path,
        *,
        identity: str = "openbear",
        dry_run: bool = True,
        overwrite: bool = False,
        rename_conflicts: bool = False,
    ) -> ImportReport:
        source = Path(source_path).expanduser()
        report = ImportReport(ok=False, dry_run=dry_run, identity=identity, source_path=str(source))
        if not source.exists():
            report.error = f"source_not_found: {source}"
            return report
        src = sqlite3.connect(str(source))
        src.row_factory = sqlite3.Row
        op_id = 0
        try:
            identity_row = src.execute("SELECT * FROM identities WHERE key=?", (identity,)).fetchone()
            if not identity_row:
                report.error = f"源 prompt-memory 中不存在 identity={identity}。请先创建该身份并调整资源授权，或显式指定 --identity。"
                return report
            identity_id = int(identity_row["id"])
            rows = self._load_source(src, identity_id, int(identity_row["prompt_template_id"] or 0))
            report.counts = {k: len(v) for k, v in rows.items() if isinstance(v, list)}
            report.conflicts = await self._detect_conflicts(rows)
            has_conflict = any(report.conflicts.values())
            if has_conflict and not overwrite and not rename_conflicts:
                report.error = "conflicts_found"
                return report
            if dry_run:
                report.ok = not has_conflict or overwrite or rename_conflicts
                await self._audit("prompt_memory.import.dry_run", report.to_dict())
                return report

            op_id, op_uuid = await self._start_operation(source, identity, overwrite, rename_conflicts)
            report.operation_uuid = op_uuid
            report.backup_path = await self._backup_current_db(source)
            await self._apply_atomic(rows, overwrite=overwrite, rename_conflicts=rename_conflicts)
            await self._finish_operation(op_id, "ok", report.to_dict())
            await self._audit("prompt_memory.import", report.to_dict())
            report.ok = True
            return report
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            if op_id:
                await self._finish_operation(op_id, "error", report.to_dict(), error=report.error)
            return report
        finally:
            src.close()

    async def _backup_current_db(self, source: Path) -> str:
        """Create a SQLite-level backup before applying an import."""
        db_path = Path(str(getattr(self.db, "_path", "") or "openbear.db")).expanduser()
        backup_dir = db_path.parent / "backups" / "prompt-memory-import"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        source_label = source.stem or "prompt-memory"
        backup_path = backup_dir / f"{db_path.stem}-before-{source_label}-{stamp}-{uuid.uuid4().hex[:8]}.db"
        await self.db.conn.commit()
        dst = sqlite3.connect(str(backup_path))
        try:
            await self.db.conn.backup(dst)
        finally:
            dst.close()
        return str(backup_path)

    def _load_source(self, src: sqlite3.Connection, identity_id: int, prompt_template_id: int = 0) -> dict[str, Any]:
        visible_entries = _visible_ids(src, "entry", identity_id)
        visible_docs = _visible_ids(src, "doc", identity_id)
        visible_secrets = _visible_ids(src, "secret", identity_id)
        all_categories = [_row_dict(r) for r in src.execute("SELECT * FROM categories ORDER BY sort, id").fetchall()]
        removed_category_ids = {
            int(row.get("id") or 0)
            for row in all_categories
            if str(row.get("key") or "") in {"identity", "persona", "rule"}
        }
        categories = [row for row in all_categories if int(row.get("id") or 0) not in removed_category_ids]
        entries: list[dict[str, Any]] = []
        if visible_entries:
            q = f"SELECT * FROM entries WHERE id IN ({','.join('?' for _ in visible_entries)}) ORDER BY sort, id"
            entries = [
                row for row in (_row_dict(r) for r in src.execute(q, tuple(visible_entries)).fetchall())
                if int(row.get("category_id") or 0) not in removed_category_ids
            ]
        docs: list[dict[str, Any]] = []
        if visible_docs:
            q = f"SELECT * FROM docs WHERE id IN ({','.join('?' for _ in visible_docs)}) ORDER BY importance DESC, id"
            docs = [_row_dict(r) for r in src.execute(q, tuple(visible_docs)).fetchall()]
        secrets: list[dict[str, Any]] = []
        if visible_secrets:
            q = f"SELECT * FROM secrets WHERE id IN ({','.join('?' for _ in visible_secrets)}) ORDER BY sort, id"
            secrets = [_row_dict(r) for r in src.execute(q, tuple(visible_secrets)).fetchall()]
        templates = [_row_dict(r) for r in src.execute("SELECT * FROM templates ORDER BY id").fetchall()]
        return {
            "categories": categories,
            "entries": entries,
            "docs": docs,
            "secrets": secrets,
            "templates": templates,
            "_meta": {"identityPromptTemplateId": prompt_template_id},
        }

    async def _detect_conflicts(self, rows: dict[str, Any]) -> dict[str, list[str]]:
        conflicts = {"entries": [], "docs": [], "secrets": [], "templates": []}
        cur = await self.db.conn.execute("SELECT ref FROM memory_entries WHERE ref<>''")
        entry_refs = {str(r["ref"]) for r in await cur.fetchall()}
        for row in rows["entries"]:
            ref = slugify_ref(str(row.get("ref") or row.get("title") or ""))
            if ref in entry_refs:
                conflicts["entries"].append(ref)
        cur = await self.db.conn.execute("SELECT name FROM memory_docs")
        doc_names = {str(r["name"]) for r in await cur.fetchall()}
        for row in rows["docs"]:
            if str(row.get("name") or "") in doc_names:
                conflicts["docs"].append(str(row.get("name") or ""))
        cur = await self.db.conn.execute("SELECT name FROM memory_secrets")
        secret_names = {str(r["name"]) for r in await cur.fetchall()}
        for row in rows["secrets"]:
            if str(row.get("name") or "") in secret_names:
                conflicts["secrets"].append(str(row.get("name") or ""))
        cur = await self.db.conn.execute("SELECT name FROM memory_templates")
        template_names = {str(r["name"]) for r in await cur.fetchall()}
        for row in rows["templates"]:
            if str(row.get("name") or "") in template_names:
                conflicts["templates"].append(str(row.get("name") or ""))
        return conflicts

    async def _apply_atomic(self, rows: dict[str, Any], *, overwrite: bool, rename_conflicts: bool) -> None:
        """原子应用导入数据；失败时不留下半导入 entries/docs/secrets/templates。"""
        # bootstrap 会提交默认分类/模板，放在导入事务外；这部分是幂等基础结构，
        # 不属于本次源数据导入，后续 savepoint 只保护真正的导入写入。
        await self.mem._bootstrap()
        sp_name = "prompt_memory_import"
        await self.db.conn.execute(f"SAVEPOINT {sp_name}")
        try:
            await self._apply(rows, overwrite=overwrite, rename_conflicts=rename_conflicts)
        except Exception:
            await self.db.conn.execute(f"ROLLBACK TO {sp_name}")
            await self.db.conn.execute(f"RELEASE {sp_name}")
            raise
        await self.db.conn.execute(f"RELEASE {sp_name}")

    async def _apply(self, rows: dict[str, Any], *, overwrite: bool, rename_conflicts: bool) -> None:
        cat_map: dict[int, int] = {}
        removed_category_ids: set[int] = set()
        for row in rows["categories"]:
            key = str(row.get("key") or "memory")
            if key in {"identity", "persona", "rule"}:
                removed_category_ids.add(int(row.get("id") or 0))
                continue
            await self.db.conn.execute(
                """
                INSERT INTO memory_categories (key, name, icon, render_type, schema_json, inject, sort)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET name=excluded.name, icon=excluded.icon,
                  render_type=excluded.render_type, schema_json=excluded.schema_json,
                  inject=excluded.inject, sort=excluded.sort
                """,
                (key, row.get("name") or key, row.get("icon") or "", row.get("render_type") or "fields", row.get("schema_json") or '{"fields":[]}', int(row.get("inject") or 1), int(row.get("sort") or 0)),
            )
            cur = await self.db.conn.execute("SELECT id FROM memory_categories WHERE key=?", (key,))
            cat_map[int(row["id"])] = int((await cur.fetchone())["id"])

        cur = await self.db.conn.execute("SELECT ref FROM memory_entries WHERE ref<>''")
        refs = {str(r["ref"]) for r in await cur.fetchall()}
        for row in rows["entries"]:
            if int(row.get("category_id") or 0) in removed_category_ids:
                continue
            ref = slugify_ref(str(row.get("ref") or row.get("title") or ""))
            if ref in refs and rename_conflicts and not overwrite:
                ref = _unique_name(refs, ref)
            title = str(row.get("title") or ref)
            values = (
                cat_map.get(int(row.get("category_id") or 0)) or await self.mem._category_id("memory"),
                row.get("grp") or "",
                ref,
                row.get("note") or "",
                title,
                row.get("fields_json") or "{}",
                row.get("body") or "",
                int(bool(row.get("expanded"))),
                int(row.get("enabled") if row.get("enabled") is not None else 1),
                int(row.get("archived") or 0),
                int(row.get("sort") or 0),
                now_ts(),
            )
            if overwrite and ref in refs:
                await self.db.conn.execute(
                    """
                    UPDATE memory_entries SET category_id=?, grp=?, ref=?, note=?, title=?, fields_json=?, body=?, expanded=?, enabled=?, archived=?, sort=?, updated_at=?
                    WHERE ref=?
                    """,
                    (*values, ref),
                )
            else:
                await self.db.conn.execute(
                    """
                    INSERT INTO memory_entries (category_id, grp, ref, note, title, fields_json, body, expanded, enabled, archived, sort, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (*values[:-1], int(row.get("created_at") or 0), values[-1]),
                )
                refs.add(ref)

        cur = await self.db.conn.execute("SELECT name FROM memory_docs")
        doc_names = {str(r["name"]) for r in await cur.fetchall()}
        for row in rows["docs"]:
            name = str(row.get("name") or "doc")
            if name in doc_names and rename_conflicts and not overwrite:
                name = _unique_name(doc_names, name)
            values = (name, row.get("title") or "", row.get("summary") or "", row.get("project") or "", int(row.get("importance") or 3), row.get("tags") or "", row.get("grp") or "", int(row.get("enabled") if row.get("enabled") is not None else 1), int(row.get("archived") or 0), int(row.get("sort") or 0), row.get("content") or "", now_ts())
            if overwrite and name in doc_names:
                await self.db.conn.execute("UPDATE memory_docs SET name=?, title=?, summary=?, project=?, importance=?, tags=?, grp=?, enabled=?, archived=?, sort=?, content=?, updated_at=? WHERE name=?", (*values, name))
            else:
                await self.db.conn.execute("INSERT INTO memory_docs (name, title, summary, project, importance, tags, grp, enabled, archived, sort, content, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values[:-1], int(row.get("created_at") or 0), values[-1]))
                doc_names.add(name)

        cur = await self.db.conn.execute("SELECT name FROM memory_secrets")
        secret_names = {str(r["name"]) for r in await cur.fetchall()}
        for row in rows["secrets"]:
            name = str(row.get("name") or "secret")
            if name in secret_names and rename_conflicts and not overwrite:
                name = _unique_name(secret_names, name)
            kv_json = json.dumps(_safe_json(row.get("kv_json") or "[]", []), ensure_ascii=False)
            values = (name, row.get("note") or "", kv_json, row.get("grp") or "", int(row.get("enabled") if row.get("enabled") is not None else 1), int(row.get("archived") or 0), int(row.get("sort") or 0), now_ts())
            if overwrite and name in secret_names:
                await self.db.conn.execute("UPDATE memory_secrets SET name=?, note=?, kv_json=?, grp=?, enabled=?, archived=?, sort=?, updated_at=? WHERE name=?", (*values, name))
            else:
                await self.db.conn.execute("INSERT INTO memory_secrets (name, note, kv_json, grp, enabled, archived, sort, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (*values[:-1], int(row.get("created_at") or 0), values[-1]))
                secret_names.add(name)

        cur = await self.db.conn.execute("SELECT name FROM memory_templates")
        template_names = {str(r["name"]) for r in await cur.fetchall()}
        active_seen = False
        bound_template_id = int((rows.get("_meta") or {}).get("identityPromptTemplateId") or 0)
        for row in rows["templates"]:
            name = str(row.get("name") or "template")
            if name in template_names and rename_conflicts and not overwrite:
                name = _unique_name(template_names, name)
            if bound_template_id:
                is_active = 1 if int(row.get("id") or 0) == bound_template_id else 0
            else:
                is_active = int(row.get("is_active") or 0)
            if is_active and not active_seen:
                await self.db.conn.execute("UPDATE memory_templates SET is_active=0")
                active_seen = True
            elif is_active and active_seen:
                is_active = 0
            # content 必须原样落库，不能格式化/替换模板字符串。
            content = str(row.get("content") or "")
            values = (name, content, is_active, now_ts())
            if overwrite and name in template_names:
                await self.db.conn.execute("UPDATE memory_templates SET name=?, content=?, is_active=?, updated_at=? WHERE name=?", (*values, name))
            else:
                await self.db.conn.execute("INSERT INTO memory_templates (name, content, is_active, updated_at) VALUES (?,?,?,?)", values)
                template_names.add(name)

    async def _start_operation(self, source: Path, identity: str, overwrite: bool, rename_conflicts: bool) -> tuple[int, str]:
        op_uuid = str(uuid.uuid4())
        cur = await self.db.conn.execute(
            """
            INSERT INTO operations (operation_uuid, chat_id, kind, status, detail_json, started_at)
            VALUES (?,?,?,?,?,?)
            """,
            (op_uuid, 0, "memory_import", "running", json.dumps({"source": str(source), "identity": identity, "overwrite": overwrite, "renameConflicts": rename_conflicts}, ensure_ascii=False), now_ts()),
        )
        await self.db.conn.commit()
        return int(cur.lastrowid or 0), op_uuid

    async def _finish_operation(self, op_id: int, status: str, detail: dict[str, Any], *, error: str = "") -> None:
        await self.db.conn.execute(
            "UPDATE operations SET status=?, detail_json=?, error=?, finished_at=? WHERE id=?",
            (status, json.dumps(detail, ensure_ascii=False), error, now_ts(), op_id),
        )
        await self.db.conn.commit()

    async def _audit(self, kind: str, detail: dict[str, Any]) -> None:
        await self.db.conn.execute(
            "INSERT INTO audit_logs (kind, actor, chat_id, ip, detail_json, created_at) VALUES (?,?,?,?,?,?)",
            (kind, "cli", 0, "", json.dumps(detail, ensure_ascii=False), now_ts()),
        )
        await self.db.conn.commit()
