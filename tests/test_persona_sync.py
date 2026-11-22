from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from app.db.engine import DB
from app.memory import personas
from app.memory.builtin import BuiltinMemoryClient
from tests.test_builtin_template_import import _login_cookie, web_env


async def _rows(db, sql="SELECT * FROM memory_templates ORDER BY id", args=()):
    cur = await db.conn.execute(sql, args)
    return [dict(row) for row in await cur.fetchall()]


async def test_sync_actual_resources_auth_idempotent_nonactivating(web_env):
    client, db = web_env.client, web_env.db
    endpoint = "/api/memory/templates/sync-personas"
    listing = "/api/memory/templates/personas"
    assert (await client.get(listing)).status == 401
    assert (await client.post(endpoint, json={"ids": ["little-code"]})).status == 401
    cookies = await _login_cookie(web_env)
    available = await client.get(listing, cookies=cookies)
    specs = (await available.json())["items"]
    assert {item["name"] for item in specs} == {"小码酱", "CTF 沙箱", "大肥鱼", "gemini-argon-4"}
    assert len({item["id"] for item in specs}) == 4
    assert all(item["status"] == "available" for item in specs)
    assert await _rows(db) == []  # Viewing local resources never imports them.
    await BuiltinMemoryClient(db)._bootstrap()
    originals = await _rows(db)
    await db.conn.execute(
        "INSERT INTO sessions (chat_id,created_at,updated_at,session_uuid,system_snapshot) VALUES(1,1,1,'old','KEEP')")
    await db.conn.commit()
    responses = await asyncio.gather(*[
        client.post(endpoint, json={"ids": [item["id"] for item in specs]}, cookies=cookies) for _ in range(2)])
    assert [response.status for response in responses] == [200, 200]
    results = [await response.json() for response in responses]
    assert sorted(result["created"] for result in results) == [0, 4]
    rows = await _rows(db)
    assert rows[:len(originals)] == originals
    by_name = {row["name"]: row for row in rows[len(originals):]}
    for spec in personas.bundled_personas():
        row = by_name[spec.name]
        assert row["content"] == spec.content
        assert (row["is_persona"], row["is_active"], row["is_agent_active"]) == (1, 0, 0)
    assert (await _rows(db, "SELECT system_snapshot FROM sessions WHERE session_uuid='old'"))[0]["system_snapshot"] == "KEEP"
    choices = await client.get("/api/prompt-templates/choices", cookies=cookies)
    assert choices.status == 200
    assert {item["name"] for item in (await choices.json())["items"]} == {spec.name for spec in personas.bundled_personas()}
    logs = await _rows(db, "SELECT detail_json FROM audit_logs WHERE kind='memory_template.sync_personas'")
    assert len(logs) == 2
    assert "content" not in logs[0]["detail_json"]


async def test_persona_sync_retains_edits_imports_new_revision_and_handles_deletion(tmp_path):
    db = DB(str(tmp_path / "personas.db"))
    await db.connect()
    try:
        original = personas.Persona("one", "原名", "original", hashlib.sha256(b"original").hexdigest())
        first = await personas.sync(db, [original])
        template_id = first["items"][0]["templateId"]
        await db.conn.execute("UPDATE memory_templates SET name='改名',content='EDITED',is_persona=0 WHERE id=?", (template_id,))
        await db.conn.commit()
        repeated = await personas.sync(db, [original])
        assert (repeated["created"], repeated["reused"]) == (0, 1)
        assert (await personas.catalog(db, [original]))[0]["status"] == "modified"
        new = replace(original, content="new revision", digest=hashlib.sha256(b"new revision").hexdigest())
        assert (await personas.catalog(db, [new]))[0]["status"] == "update"
        second = await personas.sync(db, [new])
        assert second["created"] == 1
        rows = await _rows(db)
        assert [(row["name"], row["content"], row["is_persona"]) for row in rows] == [("改名", "EDITED", 0), ("原名", "new revision", 1)]
        assert (await personas.sync(db, [new]))["created"] == 0
        await db.conn.execute("DELETE FROM memory_templates WHERE id=?", (second["items"][0]["templateId"],))
        await db.conn.commit()
        restored = await personas.sync(db, [new])
        assert restored["created"] == 1
        assert len(await _rows(db)) == 2
    finally:
        await db.close()


async def test_persona_adopts_exact_existing_persona_without_overwriting_name_collision(tmp_path):
    db = DB(str(tmp_path / "existing.db"))
    await db.connect()
    try:
        one = personas.bundled_personas()[0]
        await db.conn.execute("INSERT INTO memory_templates(name,content,is_persona) VALUES(?,?,1)", (one.name, one.content))
        await db.conn.execute("INSERT INTO memory_templates(name,content,is_active) VALUES(?,?,1)", (one.name, "MY MAIN"))
        await db.conn.commit()
        before = await _rows(db)
        assert (await personas.catalog(db, [one]))[0]["status"] == "existing"
        assert (await personas.sync(db, [one]))["created"] == 0
        assert await _rows(db) == before
        assert len(await _rows(db, "SELECT * FROM memory_persona_imports")) == 1
    finally:
        await db.close()


async def test_persona_rejects_bad_inputs_invalid_resource_and_memory_conflict(web_env, monkeypatch):
    endpoint = "/api/memory/templates/sync-personas"
    cookies = await _login_cookie(web_env)
    for data in [{}, {"ids": []}, {"ids": "little-code"}, {"ids": [12]}, {"ids": ["not-known"]}, {"ids": ["little-code"], "path": "/etc/passwd"}]:
        assert (await web_env.client.post(endpoint, json=data, cookies=cookies)).status == 400
    assert await _rows(web_env.db) == []
    async def reject(*args, **kwargs):
        return "invalid template"
    monkeypatch.setattr(web_env.server, "_validate_memory_template", reject)
    response = await web_env.client.post(endpoint, json={"ids": ["little-code"]}, cookies=cookies)
    assert response.status == 400
    assert await _rows(web_env.db) == []
    await web_env.db.conn.execute(
        "INSERT INTO operations(operation_uuid,chat_id,kind,status,detail_json,started_at) VALUES('blocked',0,'memory_import','running','{}',1)")
    await web_env.db.conn.commit()
    assert (await web_env.client.post(endpoint, json={"ids": ["little-code"]}, cookies=cookies)).status == 409


async def test_persona_sync_rolls_back_whole_selection_on_write_failure(tmp_path):
    db = DB(str(tmp_path / "atomic.db"))
    await db.connect()
    try:
        await db.conn.execute("CREATE TRIGGER fail_second BEFORE INSERT ON memory_templates WHEN NEW.name='two' BEGIN SELECT RAISE(ABORT,'full'); END")
        await db.conn.commit()
        specs = [personas.Persona(key, key, key, hashlib.sha256(key.encode()).hexdigest()) for key in ("one", "two")]
        with pytest.raises(sqlite3.IntegrityError, match="full"):
            await personas.sync(db, specs)
        assert await _rows(db) == []
        assert await _rows(db, "SELECT * FROM memory_persona_imports") == []
    finally:
        await db.close()


@pytest.mark.parametrize("fault", ["missing", "hash", "duplicate", "path", "empty"])
def test_persona_resource_manifest_fails_closed(tmp_path, monkeypatch, fault):
    spec = {"id": "sample", "name": "示例", "sha256": hashlib.sha256(b"hello").hexdigest()}
    (tmp_path / "sample.tpl").write_text("hello")
    entries = [spec]
    if fault == "hash": spec["sha256"] = "0" * 64
    if fault == "duplicate": entries.append(dict(spec))
    if fault == "path": spec["id"] = "../sample"
    if fault == "missing": (tmp_path / "sample.tpl").unlink()
    if fault == "empty":
        (tmp_path / "sample.tpl").write_bytes(b"")
        spec["sha256"] = hashlib.sha256(b"").hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps({"schema": 1, "personas": entries}))
    monkeypatch.setattr(personas, "_PERSONA_DIR", tmp_path)
    with pytest.raises((OSError, ValueError)):
        personas.bundled_personas()


async def test_persona_schema_upgrades_old_template_table_without_import(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE memory_templates(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,content TEXT NOT NULL DEFAULT '',is_active INTEGER DEFAULT 0,is_agent_active INTEGER DEFAULT 0,updated_at INTEGER DEFAULT 0)")
        old.execute("INSERT INTO memory_templates(name,content,is_active) VALUES('old main','CUSTOM',1)")
    for _ in range(2):
        db = DB(str(path))
        await db.connect()
        try:
            rows = await _rows(db)
            assert len(rows) == 1
            assert rows[0]["content"] == "CUSTOM" and rows[0]["is_active"] == 1 and rows[0]["is_persona"] == 0
            assert await _rows(db, "SELECT * FROM memory_persona_imports") == []
        finally:
            await db.close()
