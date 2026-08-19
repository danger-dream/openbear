from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from app.db.engine import DB
from app.memory.builtin import BuiltinMemoryClient
from app.memory.importer import PromptMemoryImporter


@pytest.fixture
async def env(tmp_path):
    db = DB(str(tmp_path / "openbear.db"))
    await db.connect()
    try:
        yield SimpleNamespace(db=db, tmp=tmp_path)
    finally:
        await db.close()


def _make_source(path, *, with_openbear: bool = True):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, icon TEXT DEFAULT '', render_type TEXT DEFAULT 'prose', schema_json TEXT DEFAULT '{"fields":[]}', inject INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
        CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL, grp TEXT DEFAULT '', ref TEXT DEFAULT '', note TEXT DEFAULT '', title TEXT NOT NULL, fields_json TEXT DEFAULT '{}', body TEXT DEFAULT '', enabled INTEGER DEFAULT 1, archived INTEGER DEFAULT 0, sort INTEGER DEFAULT 0, updated_at TEXT DEFAULT '');
        CREATE TABLE secrets (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, note TEXT DEFAULT '', kv_json TEXT DEFAULT '[]', enabled INTEGER DEFAULT 1, archived INTEGER DEFAULT 0, sort INTEGER DEFAULT 0, updated_at TEXT DEFAULT '');
        CREATE TABLE docs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, title TEXT DEFAULT '', summary TEXT DEFAULT '', project TEXT DEFAULT '', importance INTEGER DEFAULT 3, tags TEXT DEFAULT '', enabled INTEGER DEFAULT 1, archived INTEGER DEFAULT 0, content TEXT DEFAULT '', updated_at TEXT DEFAULT '');
        CREATE TABLE identities (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, access_key TEXT DEFAULT '', prompt_template_id INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1);
        CREATE TABLE resource_acl (id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT NOT NULL, resource_id INTEGER NOT NULL, subject_type TEXT NOT NULL, identity_id INTEGER);
        CREATE TABLE templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', is_active INTEGER DEFAULT 0, updated_at TEXT DEFAULT '');
        """
    )
    conn.execute("INSERT INTO categories (id,key,name,sort) VALUES (1,'rule','行为准则',10)")
    if with_openbear:
        conn.execute("INSERT INTO identities (id,key,name,prompt_template_id) VALUES (1,'openbear','OpenBear',2)")
    conn.execute("INSERT INTO identities (id,key,name) VALUES (2,'xiaoxi','小夕')")
    conn.execute("INSERT INTO entries (id,category_id,grp,ref,title,body,fields_json) VALUES (1,1,'安全','openbear-rule','OpenBear规则','只给 openbear','{}')")
    conn.execute("INSERT INTO entries (id,category_id,grp,ref,title,body,fields_json) VALUES (2,1,'安全','xiaoxi-rule','小夕规则','不能默认导入','{}')")
    conn.execute("INSERT INTO docs (id,name,title,summary,content,importance) VALUES (1,'openbear-doc','OpenBear文档','摘要','全文',5)")
    conn.execute(
        "INSERT INTO secrets (id,name,note,kv_json) VALUES (1,'openbear-secret','说明',?)",
        ('[{"key":"token","value":"SECRET"}]',),
    )
    conn.execute("INSERT INTO templates (id,name,content,is_active) VALUES (1,'tpl-default','Default [[ memory.byCat.rule.length ]]',1)")
    conn.execute("INSERT INTO templates (id,name,content,is_active) VALUES (2,'tpl-bound','Bound [[ memory.byCat.rule.length ]] &amp; keep',0)")
    if with_openbear:
        conn.execute("INSERT INTO resource_acl (resource_type,resource_id,subject_type,identity_id) VALUES ('entry',1,'identity',1)")
        conn.execute("INSERT INTO resource_acl (resource_type,resource_id,subject_type,identity_id) VALUES ('doc',1,'identity',1)")
        conn.execute("INSERT INTO resource_acl (resource_type,resource_id,subject_type,identity_id) VALUES ('secret',1,'identity',1)")
    conn.execute("INSERT INTO resource_acl (resource_type,resource_id,subject_type,identity_id) VALUES ('entry',2,'identity',2)")
    conn.commit()
    conn.close()


async def test_prompt_memory_import_dry_run_does_not_write(env):
    source = env.tmp / "prompt-memory.db"
    _make_source(source)
    report = await PromptMemoryImporter(env.db).import_file(source, dry_run=True)
    assert report.ok is True
    assert report.counts["entries"] == 0
    cur = await env.db.conn.execute("SELECT COUNT(*) AS n FROM memory_entries")
    assert (await cur.fetchone())["n"] == 0
    cur = await env.db.conn.execute("SELECT kind, detail_json FROM audit_logs ORDER BY id DESC LIMIT 1")
    audit = await cur.fetchone()
    assert audit["kind"] == "prompt_memory.import.dry_run"
    assert '"dryRun": true' in audit["detail_json"]


async def test_prompt_memory_import_apply_visible_openbear_only_and_preserve_template(env):
    source = env.tmp / "prompt-memory.db"
    _make_source(source)
    report = await PromptMemoryImporter(env.db).import_file(source, dry_run=False)
    assert report.ok is True
    assert report.backup_path.endswith(".db")

    mem = BuiltinMemoryClient(env.db)
    entries = await mem.tool_call("entry", {"action": "list"})
    refs = {item["ref"] for item in entries["items"]}
    assert "openbear-rule" not in refs
    assert "xiaoxi-rule" not in refs

    secret = await mem.tool_call("secret", {"action": "get", "name": "openbear-secret"})
    assert secret["item"]["kv"][0]["value"] == "SECRET"
    prompt = await mem.build_system_prompt({})
    assert "Bound 0 &amp; keep" in prompt
    assert "Default 1" not in prompt
    assert "SECRET" not in prompt


async def test_prompt_memory_import_apply_failure_rolls_back_import_rows(env):
    source = env.tmp / "prompt-memory.db"
    _make_source(source)

    class FailingImporter(PromptMemoryImporter):
        async def _apply(self, rows, *, overwrite: bool, rename_conflicts: bool) -> None:
            await super()._apply(rows, overwrite=overwrite, rename_conflicts=rename_conflicts)
            raise RuntimeError("boom")

    report = await FailingImporter(env.db).import_file(source, dry_run=False)
    assert report.ok is False
    assert "boom" in report.error
    cur = await env.db.conn.execute("SELECT COUNT(*) AS n FROM memory_entries WHERE ref='openbear-rule'")
    assert (await cur.fetchone())["n"] == 0
    cur = await env.db.conn.execute("SELECT COUNT(*) AS n FROM memory_docs WHERE name='openbear-doc'")
    assert (await cur.fetchone())["n"] == 0
    cur = await env.db.conn.execute("SELECT COUNT(*) AS n FROM memory_secrets WHERE name='openbear-secret'")
    assert (await cur.fetchone())["n"] == 0
    cur = await env.db.conn.execute("SELECT COUNT(*) AS n FROM memory_templates WHERE name='tpl-bound'")
    assert (await cur.fetchone())["n"] == 0
    cur = await env.db.conn.execute("SELECT status, error FROM operations WHERE kind='memory_import' ORDER BY id DESC LIMIT 1")
    op = await cur.fetchone()
    assert op["status"] == "error"
    assert "boom" in op["error"]


async def test_prompt_memory_import_missing_identity_stops(env):
    source = env.tmp / "prompt-memory.db"
    _make_source(source, with_openbear=False)
    report = await PromptMemoryImporter(env.db).import_file(source, dry_run=True)
    assert report.ok is False
    assert "不存在 identity=openbear" in report.error


async def test_prompt_memory_import_conflict_requires_policy(env):
    source = env.tmp / "prompt-memory.db"
    _make_source(source)
    mem = BuiltinMemoryClient(env.db)
    await mem.tool_call("doc", {"action": "set", "name": "openbear-doc", "title": "已有", "content": "old"})
    conflict = await PromptMemoryImporter(env.db).import_file(source, dry_run=True)
    assert conflict.ok is False
    assert "openbear-doc" in conflict.conflicts["docs"]

    renamed = await PromptMemoryImporter(env.db).import_file(source, dry_run=False, rename_conflicts=True)
    assert renamed.ok is True
    docs = await mem.tool_call("doc", {"action": "list"})
    names = {item["name"] for item in docs["items"]}
    assert "openbear-doc" in names
    assert any(name.startswith("openbear-doc-import") for name in names)
