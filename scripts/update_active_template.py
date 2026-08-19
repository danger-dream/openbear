#!/usr/bin/env python3
"""Update the active OpenBear system prompt template in the local SQLite DB.

This script is intentionally separate from ad-hoc shell commands so DB writes are
explicit, reviewable, and repeatable.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Update active OpenBear memory template")
    parser.add_argument("--db", default="data/openbear.db", help="SQLite DB path")
    parser.add_argument("--template", default="workspace/active-openbear-template.final.md", help="Template markdown path")
    parser.add_argument("--id", type=int, default=3, help="memory_templates.id to update")
    parser.add_argument("--name", default="OpenBear", help="Template name")
    args = parser.parse_args()

    db_path = Path(args.db)
    template_path = Path(args.template)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")
    if not template_path.exists():
        raise SystemExit(f"Template not found: {template_path}")

    content = template_path.read_text(encoding="utf-8")
    if "OpenBear Multi-Agent / Rath Orchestration Guide" not in content:
        raise SystemExit("Template sanity check failed: missing multi-agent guide")
    if "OpenBearControl self-control tool" not in content:
        raise SystemExit("Template sanity check failed: missing OpenBearControl instructions")
    if "Primary interface: Web console" not in content:
        raise SystemExit("Template sanity check failed: missing Web console runtime instructions")

    backup_dir = Path("data/tool_artifacts/manual-backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"openbear.db.before-template-update.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(db_path, backup_path)

    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id, name, length(content) AS n FROM memory_templates WHERE id=?", (args.id,)).fetchone()
        if not row:
            raise SystemExit(f"Template id not found: {args.id}")
        con.execute("UPDATE memory_templates SET is_active=0")
        con.execute(
            "UPDATE memory_templates SET name=?, content=?, is_active=1, updated_at=? WHERE id=?",
            (args.name, content, int(time.time()), args.id),
        )
        con.commit()
    finally:
        con.close()

    print(f"updated template id={args.id} name={args.name!r} chars={len(content)} backup={backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
