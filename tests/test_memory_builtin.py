from __future__ import annotations

import json

import pytest

from app.db.engine import DB
from app.memory.builtin import BuiltinMemoryClient, TemplateEngine, slugify_ref
from app.tools.base import ToolRegistry
from app.tools.memory import register_memory_tools


@pytest.fixture
async def db(tmp_path):
    d = DB(str(tmp_path / "t.db"))
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


async def test_builtin_memory_entry_doc_secret_crud_and_prompt(db):
    mem = BuiltinMemoryClient(db)
    entry = await mem.tool_call("entry", {
        "action": "set",
        "category": "memory",
        "name": "常用称呼",
        "ref": "boss",
        "body": "称呼老大。",
        "grp": "个人",
        "expanded": True,
        "fieldsJson": '{"role":"owner"}',
    })
    assert entry["ok"] is True
    assert entry["item"]["ref"] == "boss"
    assert entry["item"]["fields"] == {"role": "owner"}
    assert entry["item"]["grp"] == "个人"
    assert entry["item"]["expanded"] is True
    assert entry["item"]["createdAt"] > 0

    memory_object = await mem._memory_object()  # noqa: SLF001
    assert [item["ref"] for item in memory_object["expandedEntries"]] == ["boss"]
    assert all(item["ref"] != "boss" for group in memory_object["groupsByCat"]["memory"] for item in group["entries"])

    doc = await mem.tool_call("doc", {
        "action": "set",
        "name": "handbook",
        "title": "手册",
        "summary": "重要文档",
        "content": "长文内容",
        "importance": 5,
        "grp": "指南",
        "sort": 20,
    })
    assert doc["ok"] is True
    secret = await mem.tool_call("secret", {
        "action": "set",
        "name": "github",
        "note": "GitHub PAT",
        "grp": "开发",
        "sort": 10,
        "kvJson": json.dumps([{"key": "token", "value": "SECRET-VALUE"}], ensure_ascii=False),
    })
    assert secret["ok"] is True

    prompt = await mem.build_system_prompt({"toolNames": ["Memory"], "workspaceDir": "/tmp/w"})
    assert "常用称呼" in prompt
    assert "@mem/boss" in prompt
    assert prompt.count("称呼老大。") == 1
    assert "@doc/handbook" in prompt
    assert "@secret/github" in prompt
    assert "SECRET-VALUE" not in prompt

    got_secret = await mem.tool_call("secret", {"action": "get", "name": "github"})
    assert got_secret["item"]["kv"][0]["value"] == "SECRET-VALUE"
    assert got_secret["item"]["grp"] == "开发"
    assert got_secret["item"]["created_at"] > 0

    updated_secret = await mem.tool_call("secret", {
        "action": "set", "id": got_secret["item"]["id"], "note": "updated",
    })
    assert updated_secret["item"]["created_at"] == got_secret["item"]["created_at"]
    assert updated_secret["item"]["updated_at"] >= got_secret["item"]["updated_at"]

    got_doc = await mem.tool_call("doc", {"action": "get", "name": "handbook"})
    assert got_doc["item"]["content"] == "长文内容"
    assert got_doc["item"]["grp"] == "指南"
    assert got_doc["item"]["sort"] == 20
    assert got_doc["item"]["created_at"] > 0


async def test_agent_system_prompt_template_uses_minimal_context(db):
    mem = BuiltinMemoryClient(db)
    await db.conn.execute(
        "INSERT INTO memory_templates (name, content, is_agent_active, updated_at) VALUES (?,?,1,1)",
        ("Agent基础提示词", "Tools:\n[[ helpers.toolLines(builtinToolNames, builtinToolSummaries) ]]\nWorkspace: [[ workspaceDir ]]"),
    )
    await db.conn.commit()

    prompt = await mem.render_agent_system_prompt({
        "toolNames": ["Read"],
        "toolSummaries": {"Read": "读取文件"},
        "workspaceDir": "/tmp/agent-workspace",
    })

    assert "- Read: 读取文件" in prompt
    assert "Workspace: /tmp/agent-workspace" in prompt
    ctx = await mem._agent_template_context({
        "toolNames": ["Read"],
        "toolSummaries": {"Read": "读取文件"},
        "mcpToolNames": ["mcp__serena__find_symbol"],
        "mcpToolGroups": [{"server": "serena", "toolCount": 1}],
        "mcpServerInstructions": [{"server": "serena", "instructions": "hidden"}],
        "skillsPrompt": "hidden skill",
        "availableAgents": [{"name": "hidden"}],
        "agents": {"available": [{"name": "hidden"}]},
        "tools": {"mcp": {"names": ["mcp__serena__find_symbol"]}},
    })  # noqa: SLF001
    for main_only_key in ("mcpToolNames", "mcpToolSummaries", "mcpToolGroups", "mcpServerInstructions", "skillsPrompt", "availableAgents", "agents"):
        assert main_only_key not in ctx
    assert "mcp" not in ctx["tools"]


def test_template_helpers_render_compact_mcp_groups_and_literal_blocks():
    engine = TemplateEngine()
    groups = [
        {
            "server": "playwright",
            "toolCount": 24,
            "namespacePrefix": "mcp__playwright__",
            "exactToolName": "",
        },
        {
            "server": "sequentialthinking",
            "toolCount": 1,
            "namespacePrefix": "",
            "exactToolName": "mcp__sequentialthinking__sequentialthinking",
        },
    ]
    rendered = engine.render("[[ helpers.mcpGroupLines(mcpToolGroups) ]]", {"mcpToolGroups": groups})
    assert "playwright: 24 tools; public namespace prefix `mcp__playwright__`" in rendered
    assert "sequentialthinking: 1 tool; exact callable name `mcp__sequentialthinking__sequentialthinking`" in rendered
    assert "browser_close" not in rendered

    literal = engine.render("[[ helpers.literalBlock(text) ]]", {"text": "- item\n* emphasis\n```nested```"})
    assert literal.startswith("````\n")
    assert "\n- item\n* emphasis\n```nested```\n````" in literal


async def test_builtin_memory_tools_dispatch(db):
    mem = BuiltinMemoryClient(db)
    reg = ToolRegistry()
    register_memory_tools(reg, mem)  # type: ignore[arg-type]

    removed = await reg.dispatch("Memory", json.dumps({
        "resource": "entry",
        "action": "set",
        "category": "rule",
        "name": "回复风格",
        "body": "中文简洁。",
    }, ensure_ascii=False))
    assert "category_removed" in removed

    result = await reg.dispatch("Memory", json.dumps({
        "resource": "entry",
        "action": "set",
        "category": "memory",
        "name": "回复偏好",
        "body": "中文简洁。",
    }, ensure_ascii=False))
    assert "回复偏好" in result

    listed = await reg.dispatch("Memory", json.dumps({"resource": "entry", "action": "list", "category": "memory"}))
    assert "中文简洁" in listed


async def test_builtin_memory_ref_rename_cascades(db):
    mem = BuiltinMemoryClient(db)
    first = await mem.tool_call("entry", {
        "action": "set",
        "category": "project",
        "name": "Alpha",
        "ref": "alpha",
        "body": "Alpha body",
    })
    assert first["ok"]
    second = await mem.tool_call("entry", {
        "action": "set",
        "category": "project",
        "name": "Beta",
        "ref": "beta",
        "body": "依赖 @mem/alpha",
    })
    assert second["ok"]

    changed = await mem.tool_call("entry", {
        "action": "set",
        "id": first["item"]["id"],
        "name": "Alpha",
        "ref": "alpha-new",
        "body": "Alpha body",
    })
    assert changed["item"]["ref"] == "alpha-new"
    got = await mem.tool_call("entry", {"action": "get", "ref": "beta"})
    assert "@mem/alpha-new" in got["item"]["body"]


def test_template_engine_supports_control_flow_and_raw():
    rendered = TemplateEngine().render(
        """@if items.length
@each x in items
[[ x.name ]]
@endeach
@else
empty
@endif
@raw
[[ untouched ]]
@endraw""",
        {"items": [{"name": "A"}, {"name": "B"}]},
    )
    assert "A\nB" in rendered
    assert "[[ untouched ]]" in rendered


def test_slugify_ref_is_stable():
    assert slugify_ref("Hello World!") == "hello-world"
    assert slugify_ref("  老大 记忆  ") == "老大-记忆"


async def test_bootstrap_seeds_latest_main_and_agent_templates(db):
    from app.memory.builtin import AGENT_TEMPLATE_NAME, MAIN_TEMPLATE_NAME

    mem = BuiltinMemoryClient(db)
    await mem._bootstrap()  # noqa: SLF001
    cur = await db.conn.execute(
        "SELECT name, is_active, is_agent_active, length(content) AS n FROM memory_templates ORDER BY id"
    )
    rows = [dict(row) for row in await cur.fetchall()]
    by_name = {row["name"]: row for row in rows}
    assert MAIN_TEMPLATE_NAME in by_name
    assert AGENT_TEMPLATE_NAME in by_name
    assert int(by_name[MAIN_TEMPLATE_NAME]["is_active"]) == 1
    assert int(by_name[MAIN_TEMPLATE_NAME]["is_agent_active"]) == 0
    assert int(by_name[AGENT_TEMPLATE_NAME]["is_agent_active"]) == 1
    assert int(by_name[MAIN_TEMPLATE_NAME]["n"]) > 1000
    assert int(by_name[AGENT_TEMPLATE_NAME]["n"]) > 1000
    prompt = await mem.build_system_prompt({"workspaceDir": "/opt/openbear/workspace"})
    assert "You are OpenBear" in prompt
    assert "/opt/openbear/workspace" in prompt


async def test_bootstrap_applies_custom_display_name(db, tmp_path, monkeypatch):
    from app.memory import builtin as builtin_mod

    meta = tmp_path / "install-meta.json"
    meta.write_text('{"displayName": "朋友"}', encoding="utf-8")
    monkeypatch.setattr(builtin_mod, "_INSTALL_META", meta)

    mem = BuiltinMemoryClient(db)
    await mem._bootstrap()  # noqa: SLF001
    cur = await db.conn.execute("SELECT content FROM memory_templates WHERE is_active=1")
    row = await cur.fetchone()
    assert row is not None
    assert "**UserName**: 朋友" in row["content"]
    assert "**UserName**: 老大" not in row["content"]


async def test_bootstrap_keeps_default_display_name(db, tmp_path, monkeypatch):
    from app.memory import builtin as builtin_mod

    meta = tmp_path / "install-meta.json"
    meta.write_text('{"displayName": "老大"}', encoding="utf-8")
    monkeypatch.setattr(builtin_mod, "_INSTALL_META", meta)

    mem = BuiltinMemoryClient(db)
    await mem._bootstrap()  # noqa: SLF001
    cur = await db.conn.execute("SELECT content FROM memory_templates WHERE is_active=1")
    row = await cur.fetchone()
    assert row is not None
    assert "**UserName**: 老大" in row["content"]
