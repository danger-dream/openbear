"""Replay a published schema through the complete current service lifecycle.

The frozen SQL is the empty DB produced by v0.9.2 (see its provenance header),
not DB.connect from the candidate. All payloads and configuration are synthetic.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from app.config import Config
from app.db import engine
from app.services import Services

# These test migrations, so every database here is built the real way.
pytestmark = pytest.mark.real_db

SCHEMA = Path(__file__).parent / "fixtures/upgrade_v092.sql"


def seed_legacy(path, count):
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA.read_text())
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='schema_data_migrations'").fetchone() is None
        conn.execute("INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title) VALUES('upgrade',1,-1,'Original title')")
        conn.execute("INSERT INTO sessions(chat_id,session_uuid,system_snapshot) VALUES(-1,'upgrade','ORIGINAL_SESSION_SYSTEM')")
        conn.execute("INSERT INTO messages(chat_id,role,content,conversation_uuid) VALUES(-1,'user','ORIGINAL_USER_MESSAGE','upgrade')")
        conn.executemany("INSERT INTO memory_templates(name,content,is_active,is_agent_active) VALUES(?,?,?,?)", [
            ("Custom main", "ORIGINAL_MAIN_TEMPLATE", 1, 0),
            ("Custom agent", "ORIGINAL_AGENT_TEMPLATE", 0, 1),
        ])
        conn.execute("INSERT INTO memory_docs(name,content,sort) VALUES('custom','ORIGINAL_DOC',80)")
        conn.executemany("""INSERT INTO web_operations(conversation_uuid,op_id,op_type,lifecycle,status,
            display_seq,revision,payload_json,created_at_ms,updated_at_ms)
            VALUES('upgrade',?,'reasoning','terminal','completed',?,?,?,1,1)""", [
            (f"op:{i}", i, count // 100, json.dumps({"text": f"ORIGINAL_OP_{i}", "terminalAtMs": 1}))
            for i in range(100)
        ])
        conn.executemany("""INSERT INTO web_event_frames(conversation_uuid,op_id,op_type,action,
            frame_seq,display_seq,revision,payload_json,created_at_ms,updated_at_ms)
            VALUES('upgrade',?,'reasoning','patch',?,?,? ,?,1,1)""", (
            (f"op:{seq % 100}", seq, seq % 100, (seq + 99) // 100, '{"text":"' + 'x' * 1024 + '"}')
            for seq in range(1, count + 1)
        ))
        conn.commit()
        return dict(conn.execute("SELECT op_id,MAX(id) FROM web_event_frames GROUP BY op_id"))


def preserved(path, latest):
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT system_snapshot FROM sessions WHERE chat_id=-1").fetchone()[0] == "ORIGINAL_SESSION_SYSTEM"
        assert conn.execute("SELECT content FROM messages WHERE chat_id=-1 ORDER BY id LIMIT 1").fetchone()[0] == "ORIGINAL_USER_MESSAGE"
        assert conn.execute("SELECT content FROM memory_templates WHERE is_active=1").fetchone()[0] == "ORIGINAL_MAIN_TEMPLATE"
        assert conn.execute("SELECT content FROM memory_templates WHERE is_agent_active=1").fetchone()[0] == "ORIGINAL_AGENT_TEMPLATE"
        assert conn.execute("SELECT content,sort FROM memory_docs WHERE name='custom'").fetchone() == ("ORIGINAL_DOC", 80)
        rows = conn.execute("SELECT op_id,payload_json FROM web_operations WHERE conversation_uuid='upgrade'").fetchall()
        assert len(rows) == 100
        for op, payload in rows:
            assert json.loads(payload)["text"] == f"ORIGINAL_OP_{op.split(':')[1]}"
        assert dict(conn.execute("SELECT op_id,MAX(id) FROM web_event_frames GROUP BY op_id")) == latest
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


async def test_published_large_database_first_upgrade_restart_and_first_retention(tmp_path, monkeypatch):
    count = max(100_000, int(os.environ.get("OPENBEAR_TEST_UPGRADE_FRAMES", "100000")))
    count = ((count + 99) // 100) * 100
    path = tmp_path / "old-user.db"
    latest = await asyncio.to_thread(seed_legacy, path, count)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENBEAR_CONFIG", str(tmp_path / "openbear.json"))
    monkeypatch.delenv("OPENBEAR_INSTANCE_ROLE", raising=False)
    monkeypatch.setenv("OPENBEAR_DISABLE_UPDATE_CHECK", "1")
    config = Config.model_validate({
        "telegram": {"botToken": "test", "whitelistIds": [1]},
        "models": {"providers": {}, "primary": "test/model"},
        "memory": {"provider": "builtin"}, "storage": {"dbPath": str(path)},
        "tools": {"skillsDir": str(tmp_path / "skills")},
        "web": {"enabled": True, "host": "127.0.0.1"},
        "mcp": {"enabled": False}, "browser": {"enabled": False},
    })
    config.web.port = 0
    open_connection = engine.open_sqlite_connection
    frame_deletes = []

    async def traced_open(filename):
        conn = await open_connection(filename)
        def trace(sql):
            text = sql.upper()
            if "DELETE FROM WEB_EVENT_FRAMES" in text:
                frame_deletes.append(sql)
        await conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(engine, "open_sqlite_connection", traced_open)
    expected_frames = count
    timings = []
    for restart in range(2):
        service = Services(config, SimpleNamespace())
        service.models_dev_catalog.refresh = AsyncMock()
        service.cron._sync_holidays = AsyncMock()
        frame_deletes.clear()
        try:
            began = time.monotonic()
            async with asyncio.timeout(20):
                await service.startup()
            timings.append(time.monotonic() - began)
            assert service.startup_ready.is_set()
            port = service.web_admin._site._server.sockets[0].getsockname()[1]
            async with aiohttp.ClientSession() as client:
                for _ in range(3):
                    response = await client.get(f"http://127.0.0.1:{port}/health")
                    assert response.status == 200
                    async with service.db.write_transaction(wait_timeout_s=0.5) as conn:
                        await conn.execute("INSERT OR REPLACE INTO app_state(key,value,updated_at) VALUES('upgrade-write','ok',1)")
            assert not frame_deletes, "Startup must not run any event frame DELETE"
            async with await service.db.conn.execute("SELECT COUNT(*) FROM web_event_frames") as cur:
                assert (await cur.fetchone())[0] == expected_frames
            if restart == 0:
                async with asyncio.timeout(2):
                    removed = await service.web_admin._prune_web_event_frames(max_scan_rows=256)
                assert 0 < removed <= 256
                expected_frames -= removed
                assert service.web_admin._web_event_frame_retention_pending
            await asyncio.to_thread(preserved, path, latest)
        finally:
            async with asyncio.timeout(10):
                await service.shutdown()
        assert service.db._conn is None and service.web_admin._runner is None
    print(json.dumps({"oldSchema": "v0.9.2", "frames": count, "databaseBytes": path.stat().st_size,
                      "startupSeconds": timings, "framesAfterSlice": expected_frames}))
