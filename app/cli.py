"""OpenBear maintenance CLI."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.config import load_config
from app.db.engine import DB, now_ts
from app.memory.importer import PromptMemoryImporter

_RATH_RUNTIME_TABLES = (
    "rath_task_controls",
    "rath_task_model_contexts",
    "rath_task_events",
    "rath_task_artifacts",
    "rath_tasks",
    "rath_agent_sessions",
)
_SYSTEM_TEMPLATE_NAME = "OpenBear-v3.2"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openbear")
    parser.add_argument("--config", default="openbear.json", help="OpenBear config path")
    sub = parser.add_subparsers(dest="cmd")
    mem = sub.add_parser("memory")
    mem_sub = mem.add_subparsers(dest="memory_cmd")
    imp = mem_sub.add_parser("import-prompt-memory")
    imp.add_argument("source_db", help="prompt-memory SQLite DB path")
    imp.add_argument("--identity", default="openbear")
    imp.add_argument("--dry-run", action="store_true", default=True)
    imp.add_argument("--apply", action="store_true", help="actually write target OpenBear DB")
    imp.add_argument("--overwrite", action="store_true")
    imp.add_argument("--rename-conflicts", action="store_true")
    imp.add_argument("--json", action="store_true", help="print machine-readable JSON")
    maint = sub.add_parser("maintenance")
    maint_sub = maint.add_subparsers(dest="maintenance_cmd")
    reset = maint_sub.add_parser("reset-agent-runtime")
    reset.add_argument("--dry-run", action="store_true", default=True)
    reset.add_argument("--apply", action="store_true", help="actually delete/reset data")
    reset.add_argument("--presets", action="store_true", help="also delete Agent presets from rath_agents")
    reset.add_argument("--clear-render-logs", action="store_true", help="also clear memory_render_logs")
    reset.add_argument(
        "--activate-current-system-prompt",
        action="store_true",
        help="insert/update prompts/openbear-system.tpl as the active memory template",
    )
    reset.add_argument("--template-name", default=_SYSTEM_TEMPLATE_NAME)
    reset.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


async def _table_count(db: DB, table: str) -> int:
    cur = await db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    if await cur.fetchone() is None:
        return 0
    cur = await db.conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
    row = await cur.fetchone()
    return int(row["n"] or 0)


async def _activate_current_system_prompt(db: DB, *, template_name: str, apply: bool) -> dict[str, object]:
    path = Path("prompts/openbear-system.tpl")
    content = path.read_text(encoding="utf-8")
    cur = await db.conn.execute(
        "SELECT id, is_active FROM memory_templates WHERE name=? ORDER BY id DESC LIMIT 1",
        (template_name,),
    )
    row = await cur.fetchone()
    result: dict[str, object] = {
        "templateName": template_name,
        "contentLength": len(content),
        "existingId": int(row["id"]) if row is not None else 0,
        "wouldCreate": row is None,
        "wouldActivate": True,
        "applied": False,
    }
    if not apply:
        return result
    ts = now_ts()
    await db.conn.execute("UPDATE memory_templates SET is_active=0")
    if row is None:
        cur = await db.conn.execute(
            "INSERT INTO memory_templates (name, content, is_active, updated_at) VALUES (?,?,?,?)",
            (template_name, content, 1, ts),
        )
        result["templateId"] = int(cur.lastrowid or 0)
    else:
        await db.conn.execute(
            "UPDATE memory_templates SET content=?, is_active=1, updated_at=? WHERE id=?",
            (content, ts, int(row["id"])),
        )
        result["templateId"] = int(row["id"])
    result["applied"] = True
    return result


async def _reset_agent_runtime(args: argparse.Namespace) -> dict[str, object]:
    cfg = load_config(Path(args.config))
    db = DB(cfg.storage.db_path)
    await db.connect()
    apply = bool(args.apply)
    try:
        tables = list(_RATH_RUNTIME_TABLES)
        if args.presets:
            tables.append("rath_agents")
        if args.clear_render_logs:
            tables.append("memory_render_logs")
        before = {table: await _table_count(db, table) for table in tables}
        prompt_result: dict[str, object] | None = None
        if args.activate_current_system_prompt:
            prompt_result = await _activate_current_system_prompt(
                db,
                template_name=str(args.template_name or _SYSTEM_TEMPLATE_NAME),
                apply=apply,
            )
        if apply:
            for table in tables:
                await db.conn.execute(f"DELETE FROM {table}")
            await db.conn.commit()
        after = {table: await _table_count(db, table) for table in tables}
        return {
            "ok": True,
            "dryRun": not apply,
            "dbPath": cfg.storage.db_path,
            "schemaMigrationsMayRunOnConnect": True,
            "tables": {
                table: {
                    "before": before[table],
                    "after": after[table],
                    "wouldDelete": before[table],
                    "deleted": before[table] - after[table] if apply else 0,
                }
                for table in tables
            },
            "systemPrompt": prompt_result,
        }
    finally:
        await db.close()


async def _run(args: argparse.Namespace) -> int:
    if args.cmd == "memory" and args.memory_cmd == "import-prompt-memory":
        cfg = load_config(Path(args.config))
        db = DB(cfg.storage.db_path)
        await db.connect()
        try:
            report = await PromptMemoryImporter(db).import_file(
                args.source_db,
                identity=args.identity,
                dry_run=not args.apply,
                overwrite=args.overwrite,
                rename_conflicts=args.rename_conflicts,
            )
        finally:
            await db.close()
        data = report.to_dict()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"ok={data['ok']} dryRun={data['dryRun']} identity={data['identity']}")
            print("counts=" + json.dumps(data["counts"], ensure_ascii=False))
            print("conflicts=" + json.dumps(data["conflicts"], ensure_ascii=False))
            if data.get("backupPath"):
                print(f"backup={data['backupPath']}")
            if data.get("error"):
                print(f"error={data['error']}")
        return 0 if report.ok else 1
    if args.cmd == "maintenance" and args.maintenance_cmd == "reset-agent-runtime":
        data = await _reset_agent_runtime(args)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"ok={data['ok']} dryRun={data['dryRun']} db={data['dbPath']}")
            if data.get("schemaMigrationsMayRunOnConnect"):
                print("note=DB schema migrations may run during connection before CLI deletes")
            for table, stats in (data["tables"] or {}).items():
                print(
                    f"{table}: before={stats['before']} after={stats['after']} "
                    f"wouldDelete={stats['wouldDelete']} deleted={stats['deleted']}"
                )
            if data.get("systemPrompt"):
                prompt = data["systemPrompt"]
                print(
                    "systemPrompt="
                    + json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
                )
        return 0
    raise SystemExit("unknown command")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
