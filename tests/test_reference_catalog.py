# ruff: noqa: F811
# Imported fixtures are registered by pytest and intentionally reused as parameter names.
from __future__ import annotations

import asyncio
import json

import pytest

from app.memory.builtin import BuiltinMemoryClient
from app.web_console.realtime import read_catalog
from tests.test_builtin_template_import import _login_cookie, web_env  # noqa: F401


async def receive_type(ws, kind):
    for _ in range(30):
        data = await ws.receive_json(timeout=4)
        if data.get("type") == kind:
            return data
    raise AssertionError(kind)


async def test_catalog_is_authenticated_and_secret_values_never_broadcast(web_env):
    e = web_env
    assert (await e.client.get("/api/reference-catalog")).status == 401
    cookies = await _login_cookie(e)
    memory = BuiltinMemoryClient(e.db)
    item = await memory.tool_call(
        "secret",
        {
            "action": "set",
            "name": "release key",
            "kvJson": '[{"key":"token","value":"NEVER-IN-CATALOG"}]',
        },
    )
    response = await e.client.get("/api/reference-catalog", cookies=cookies)
    data = await response.json()
    assert response.status == 200
    row = next(row for row in data["items"] if row["key"] == f"secret:{item['item']['id']}")
    assert row["fieldKeys"] == ["token"]
    assert "NEVER-IN-CATALOG" not in json.dumps(data)
    assert "kv_json" not in json.dumps(data)
    changes = await e.db.conn.execute("SELECT * FROM web_catalog_changes")
    assert "NEVER-IN-CATALOG" not in str([tuple(row) for row in await changes.fetchall()])


async def test_tool_created_renamed_and_deleted_doc_updates_open_global_socket(web_env):
    e = web_env
    cookies = await _login_cookie(e)
    ws = await e.client.ws_connect(
        "/api/events/ws", headers={"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
    )
    try:
        snapshot = await receive_type(ws, "snapshot")
        memory = BuiltinMemoryClient(e.db)
        result = await memory.tool_call(
            "doc",
            {
                "action": "set",
                "name": "new-document",
                "title": "新建文档",
                "content": "这是展示在候选列表中的文档正文摘要。" * 4
                + "FULL-BODY-TAIL-NOT-IN-CATALOG",
            },
        )
        key = f"doc:{result['item']['id']}"
        patch = await receive_type(ws, "patch")
        assert patch["previousSeq"] == snapshot["seq"]
        row = next(row for row in patch["upserts"] if row["key"] == key)
        assert row["preview"] == ("这是展示在候选列表中的文档正文摘要。" * 4)[:30] + "…"
        assert "FULL-BODY-TAIL-NOT-IN-CATALOG" not in json.dumps(patch)
        assert "content" not in row
        await memory.tool_call(
            "doc",
            {
                "action": "set",
                "id": result["item"]["id"],
                "title": "改名文档",
                "content": "修改后的\n正文",
            },
        )
        patch = await receive_type(ws, "patch")
        row = next(row for row in patch["upserts"] if row["key"] == key)
        assert row["label"] == "改名文档"
        assert row["preview"] == "修改后的 正文"
        await memory.tool_call("doc", {"action": "del", "id": result["item"]["id"]})
        patch = await receive_type(ws, "patch")
        assert key in patch["removed"]
    finally:
        await ws.close()


async def test_previews_are_short_unicode_excerpts_with_empty_and_structured_fallback(web_env):
    e = web_env
    memory = BuiltinMemoryClient(e.db)
    body = "  🦜项目\n\t配置 " + "中文摘要" * 30 + "HIDDEN-TAIL"
    entry = await memory.tool_call("entry", {"action": "set", "title": "预览记忆", "body": body})
    structured = await memory.tool_call(
        "entry", {"action": "set", "title": "结构化记忆", "fieldsJson": '{"path":"/srv/app"}'}
    )
    empty = await memory.tool_call("doc", {"action": "set", "name": "empty-preview", "content": ""})
    _, items = await asyncio.to_thread(read_catalog, e.db.path, 123)
    row = items[f"mem:{entry['item']['id']}"]
    assert row["preview"] == " ".join(body.split())[:30] + "…"
    assert "body" not in row and "HIDDEN-TAIL" not in json.dumps(items)
    assert "/srv/app" in items[f"mem:{structured['item']['id']}"]["preview"]
    assert items[f"doc:{empty['item']['id']}"]["preview"] == ""


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            '[{"key":" host ","value":"NEVER-IN-CATALOG"},{"key":"password","value":{"nested":"NEVER-IN-CATALOG"}},{"key":"host","value":"NEVER-IN-CATALOG"},{"key":"","value":"NEVER-IN-CATALOG"}]',
            ["host", "password"],
        ),
        (
            '["NEVER-IN-CATALOG",null,42,[],{"value":"NEVER-IN-CATALOG"},{"key":{"nested":"NEVER-IN-CATALOG"}}]',
            [],
        ),
        ('{"password":"NEVER-IN-CATALOG"}', []),
        ("invalid-NEVER-IN-CATALOG", []),
    ],
)
async def test_secret_projection_is_names_only_even_for_malformed_legacy_rows(
    web_env, payload, expected
):
    e = web_env
    await e.db.conn.execute(
        "INSERT INTO memory_secrets(name,kv_json,note) VALUES(?,?,?)",
        ("key-projection", payload, "NEVER-IN-CATALOG"),
    )
    await e.db.conn.commit()
    _, items = await asyncio.to_thread(read_catalog, e.db.path, 123)
    row = next(item for item in items.values() if item["name"] == "key-projection")
    assert row["fieldKeys"] == expected
    assert "NEVER-IN-CATALOG" not in json.dumps(items)
    assert not ({"kv", "kv_json", "note", "preview", "value"} & row.keys())


async def test_secret_key_changes_propagate_without_values_over_websocket(web_env):
    e = web_env
    cookies = await _login_cookie(e)
    memory = BuiltinMemoryClient(e.db)
    item = await memory.tool_call(
        "secret",
        {
            "action": "set",
            "name": "live-keys",
            "kvJson": '[{"key":"host","value":"NEVER-IN-CATALOG"}]',
        },
    )
    key = f"secret:{item['item']['id']}"
    ws = await e.client.ws_connect(
        "/api/events/ws", headers={"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
    )
    try:
        snapshot = await receive_type(ws, "snapshot")
        assert next(row for row in snapshot["items"] if row["key"] == key)["fieldKeys"] == ["host"]
        await memory.tool_call(
            "secret",
            {
                "action": "set",
                "id": item["item"]["id"],
                "kvJson": '[{"key":"username","value":"NEVER-IN-CATALOG"},{"key":"password","value":"NEVER-IN-CATALOG"}]',
            },
        )
        patch = await receive_type(ws, "patch")
        assert next(row for row in patch["upserts"] if row["key"] == key)["fieldKeys"] == [
            "username",
            "password",
        ]
        assert "NEVER-IN-CATALOG" not in json.dumps([snapshot, patch])
    finally:
        await ws.close()


async def test_catalog_sees_only_committed_mutations_and_recovers_snapshot(web_env):
    e = web_env
    wake = asyncio.Event()
    e.db.conn.commit_listeners.add(wake.set)
    await e.db.conn.execute("INSERT INTO memory_docs(name,title) VALUES('rolled','rolled')")
    _, items = await asyncio.to_thread(read_catalog, e.db.path, 123)
    assert not any(item["name"] == "rolled" for item in items.values())
    await e.db.conn.rollback()
    assert not wake.is_set()
    async with e.db.write_transaction(label="catalog-test") as conn:
        await conn.execute("INSERT INTO memory_docs(name,title) VALUES('committed','committed')")
    assert wake.is_set()
    _, items = await asyncio.to_thread(read_catalog, e.db.path, 123)
    assert any(item["name"] == "committed" for item in items.values())
    e.db.conn.commit_listeners.discard(wake.set)


async def test_catalog_conversation_ownership_and_archive_are_independent(web_env):
    e = web_env
    await e.db.conn.executemany(
        "INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title,archived_at) VALUES(?,?,?,?,?)",
        [
            ("mine", 123, -1, "mine", 0),
            ("archived", 123, -2, "archived", 1),
            ("other", 456, -3, "other", 0),
        ],
    )
    await e.db.conn.commit()
    _, items = await asyncio.to_thread(read_catalog, e.db.path, 123)
    assert "chat:mine" in items and "chat:archived" not in items and "chat:other" not in items
    _, items = await asyncio.to_thread(read_catalog, e.db.path, 123, True)
    assert "chat:archived" in items and "chat:other" not in items
