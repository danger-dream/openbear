from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import installed_version
from app.config import Config
from app.db.engine import DB
from app.memory.builtin import BuiltinMemoryClient
from app.web_admin import WebAdminServer


class _FakeBot:
    async def send_message(self, chat_id, text, reply_markup=None):
        return SimpleNamespace(message_id=1)


def _cfg() -> Config:
    return Config.model_validate({
        "telegram": {"botToken": "t", "whitelistIds": [123]},
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://example.invalid",
                    "apiKey": "k",
                    "protocol": "chat",
                    "models": [{"id": "gpt"}],
                },
            },
            "primary": "openai/gpt",
        },
        "memory": {"baseUrl": "http://example.invalid", "identity": "openbear", "accessKey": "ak"},
        "web": {"enabled": True, "host": "127.0.0.1", "port": 18961, "sessionDays": 30},
    })


@pytest.fixture
async def web_env(tmp_path):
    db = DB(str(tmp_path / "template-import.db"))
    await db.connect()
    server = WebAdminServer(_cfg(), db, _FakeBot())  # type: ignore[arg-type]
    await server.ensure_secret_key()
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    try:
        yield SimpleNamespace(db=db, server=server, client=client)
    finally:
        await client.close()
        await db.close()


async def _login_cookie(web_env) -> dict[str, str]:
    key = await web_env.server.get_secret_key()
    start = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    request_uuid = (await start.json())["requestUuid"]
    await web_env.server.decide_login_request(request_uuid, approved=True, decided_by=123)
    approved = await web_env.client.post(f"/api/auth/login/consume/{request_uuid}")
    return {"openbear_web_session": approved.cookies["openbear_web_session"].value}


async def test_builtin_template_import_is_authenticated_validated_idempotent_and_nonactivating(web_env, monkeypatch):
    endpoint = "/api/memory/templates/import-builtin"
    unauthenticated = await web_env.client.post(endpoint, json={"kinds": ["main", "agent"]})
    assert unauthenticated.status == 401

    cookies = await _login_cookie(web_env)
    await web_env.db.conn.execute(
        "INSERT INTO memory_templates (name, content, is_active, is_agent_active, updated_at) VALUES (?,?,1,0,1)",
        ("我的主控模板", "custom main",),
    )
    await web_env.db.conn.execute(
        "INSERT INTO memory_templates (name, content, is_active, is_agent_active, updated_at) VALUES (?,?,0,1,1)",
        ("我的 Agent 模板", "custom agent",),
    )
    await web_env.db.conn.execute(
        "INSERT INTO sessions (chat_id, created_at, updated_at, session_uuid, system_snapshot) VALUES (?,?,?,?,?)",
        (7788, 1, 1, "existing-session", "LOCKED SYSTEM SNAPSHOT"),
    )
    await web_env.db.conn.commit()

    validation_calls: list[tuple[str, str, bool]] = []
    original_validate = web_env.server._validate_memory_template

    async def recording_validate(content: str, name: str, *, agent_active: bool = False) -> str:
        validation_calls.append((content, name, agent_active))
        return await original_validate(content, name, agent_active=agent_active)

    monkeypatch.setattr(web_env.server, "_validate_memory_template", recording_validate)

    # Two simultaneous requests model a double-click or two open tabs. The server
    # serializes the operation, so exactly one creates and the other reuses.
    responses = await asyncio.gather(
        web_env.client.post(endpoint, json={"kinds": ["main", "agent"]}, cookies=cookies),
        web_env.client.post(endpoint, json={"kinds": ["main", "agent"]}, cookies=cookies),
    )
    assert [response.status for response in responses] == [200, 200]
    payloads = [await response.json() for response in responses]
    assert sorted((payload["created"], payload["reused"]) for payload in payloads) == [(0, 2), (2, 0)]

    project_root = Path(__file__).resolve().parents[1]
    expected = {
        "main": (
            f"内置主控模板 · OpenBear {installed_version()}",
            (project_root / "prompts" / "openbear-system.tpl").read_text(encoding="utf-8"),
        ),
        "agent": (
            f"内置 Agent 模板 · OpenBear {installed_version()}",
            (project_root / "prompts" / "openbear-agent.tpl").read_text(encoding="utf-8"),
        ),
    }
    assert {(name, is_agent) for _content, name, is_agent in validation_calls} == {
        (expected["main"][0], False),
        (expected["agent"][0], True),
    }
    assert all(content == expected["agent" if is_agent else "main"][1] for content, _name, is_agent in validation_calls)

    cur = await web_env.db.conn.execute(
        "SELECT id, name, content, is_active, is_agent_active FROM memory_templates ORDER BY id",
    )
    rows = [dict(row) for row in await cur.fetchall()]
    assert len(rows) == 4
    by_name = {row["name"]: row for row in rows}
    assert (by_name["我的主控模板"]["is_active"], by_name["我的主控模板"]["is_agent_active"]) == (1, 0)
    assert (by_name["我的 Agent 模板"]["is_active"], by_name["我的 Agent 模板"]["is_agent_active"]) == (0, 1)
    for name, content in expected.values():
        assert by_name[name]["content"] == content
        assert (by_name[name]["is_active"], by_name[name]["is_agent_active"]) == (0, 0)

    cur = await web_env.db.conn.execute("SELECT system_snapshot FROM sessions WHERE chat_id=7788")
    assert (await cur.fetchone())["system_snapshot"] == "LOCKED SYSTEM SNAPSHOT"

    repeated = await web_env.client.post(endpoint, json={"kinds": ["main", "agent", "main"]}, cookies=cookies)
    assert repeated.status == 200
    repeated_data = await repeated.json()
    assert (repeated_data["created"], repeated_data["reused"]) == (0, 2)
    cur = await web_env.db.conn.execute("SELECT COUNT(*) AS n FROM memory_templates")
    assert int((await cur.fetchone())["n"]) == 4

    cur = await web_env.db.conn.execute(
        "SELECT kind, actor, chat_id, detail_json FROM audit_logs WHERE kind IN (?, ?) ORDER BY id",
        ("memory_template.save", "memory_template.import_builtin"),
    )
    audit_rows = [dict(row) for row in await cur.fetchall()]
    save_rows = [row for row in audit_rows if row["kind"] == "memory_template.save"]
    import_rows = [row for row in audit_rows if row["kind"] == "memory_template.import_builtin"]
    assert len(save_rows) == 2
    assert len(import_rows) == 3
    assert all(row["actor"] == "web" and row["chat_id"] == 123 for row in audit_rows)
    assert all(json.loads(row["detail_json"])["source"] == "builtin_import" for row in save_rows)


async def test_builtin_template_import_rejects_uncontrolled_inputs_and_memory_conflict(web_env):
    endpoint = "/api/memory/templates/import-builtin"
    cookies = await _login_cookie(web_env)
    cases = [
        ({}, "builtin_template_kinds_required"),
        ({"kinds": []}, "builtin_template_kinds_required"),
        ({"kinds": "main"}, "builtin_template_kinds_required"),
        ({"kinds": ["main", "unknown"]}, "unsupported_builtin_template_kind"),
        ({"kinds": ["main"], "path": "/etc/passwd"}, "unexpected_fields"),
    ]
    for body, error in cases:
        response = await web_env.client.post(endpoint, json=body, cookies=cookies)
        assert response.status == 400
        assert (await response.json())["error"] == error

    cur = await web_env.db.conn.execute("SELECT COUNT(*) AS n FROM memory_templates")
    assert int((await cur.fetchone())["n"]) == 0

    await web_env.db.conn.execute(
        "INSERT INTO operations (operation_uuid, chat_id, kind, status, detail_json, started_at) VALUES (?,?,?,?,?,?)",
        ("template-import-block", 0, "memory_import", "running", "{}", 1),
    )
    await web_env.db.conn.commit()
    conflict = await web_env.client.post(endpoint, json={"kinds": ["main"]}, cookies=cookies)
    assert conflict.status == 409
    assert "memory operation is running" in await conflict.text()


async def test_empty_database_template_names_follow_the_installed_version_source(tmp_path, monkeypatch):
    from app.memory import builtin as builtin_module

    monkeypatch.setattr(builtin_module, "installed_version", lambda: "source-version-for-test")
    db = DB(str(tmp_path / "fresh.db"))
    await db.connect()
    try:
        await BuiltinMemoryClient(db)._bootstrap()  # noqa: SLF001
        cur = await db.conn.execute(
            "SELECT name, is_active, is_agent_active FROM memory_templates ORDER BY id",
        )
        rows = [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()

    assert rows == [
        {"name": "内置主控模板 · OpenBear source-version-for-test", "is_active": 1, "is_agent_active": 0},
        {"name": "内置 Agent 模板 · OpenBear source-version-for-test", "is_active": 0, "is_agent_active": 1},
    ]
