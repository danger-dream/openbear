"""ContextBuilder 测试 —— mock 记忆系统 + DB。"""
from __future__ import annotations

import httpx
import pytest

from app.context.builder import _FALLBACK_SYSTEM, ContextBuilder, build_system_prompt_params
from app.db.dao import MessageDAO, SummaryDAO
from app.db.engine import DB
from app.memory.client import MemoryClient
from app.tools.base import ToolRegistry
from app.tools.skills import Skill


def _mem(handler) -> MemoryClient:
    c = MemoryClient("http://m/api", "openbear", "ak")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _tools() -> ToolRegistry:
    """空工具注册表，够测试用。"""
    return ToolRegistry()


@pytest.fixture
async def db(tmp_path):
    d = DB(str(tmp_path / "t.db"))
    await d.connect()
    yield d
    await d.close()


async def test_build_system_ok(db, tmp_path):
    def handler(req):
        return httpx.Response(200, json={"prompt": "你是测试助手"})
    skills = [Skill("weather", "查天气", "/x/SKILL.md", "/x")]
    cb = ContextBuilder(_mem(handler), MessageDAO(db), SummaryDAO(db),
                        skills, _tools(), str(tmp_path))
    system = await cb.build_system()
    assert "你是测试助手" in system


def test_build_system_prompt_params_include_runtime_schema():
    params = build_system_prompt_params(
        tool_names=["Read", "OpenBearControl"],
        tool_summaries={"Read": "读取文件", "OpenBearControl": "控制 OpenBear"},
        skills_prompt="<available_skills />",
        workspace_dir="/tmp/workspace",
        current_model="openai/gpt",
    )
    assert params["workspaceDir"] == "/tmp/workspace"
    assert params["tools"]["allowlist"] == ["Read", "OpenBearControl"]
    assert params["builtinToolNames"] == ["Read", "OpenBearControl"]
    assert params["mcpToolNames"] == []
    assert params["mcpToolGroups"] == []
    assert params["tools"]["builtin"]["allowlist"] == ["Read", "OpenBearControl"]
    assert params["tools"]["mcp"]["allowlist"] == []
    assert params["host"]["hostname"]
    assert params["runtimeInfo"]["hostname"] == params["host"]["hostname"]
    assert params["runtimeInfo"]["channel"] == "web"
    assert params["runtimeInfo"]["primaryInterface"] == "web_console"
    assert params["runtimeInfo"]["outputFormat"] == "markdown"
    assert params["runtimeInfo"]["web"]["rendering"] == "browser_markdown"
    assert params["outputFormat"] == "markdown"
    assert params["templateEngine"]["supportedSyntax"]


async def test_build_system_passes_params(db, tmp_path):
    """验证 build_system 把 toolNames/skillsPrompt/workspaceDir 传给了模板引擎。"""
    captured = {}
    def handler(req):
        import json
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={"prompt": "ok"})
    tools = _tools()
    tools.add("TestTool", "A test", {"type": "object", "properties": {}},
              lambda args: "")
    skills = [Skill("weather", "查天气", "/x/SKILL.md", "/x")]
    cb = ContextBuilder(_mem(handler), MessageDAO(db), SummaryDAO(db),
                        skills, tools, "/my/workspace")
    await cb.build_system(current_model="test-model")
    assert "TestTool" in captured.get("toolNames", [])
    assert "TestTool" in captured.get("toolSummaries", {})
    assert captured.get("workspaceDir") == "/my/workspace"
    assert captured.get("tools", {}).get("allowlist") == captured.get("toolNames")
    assert "weather" in captured.get("skillsPrompt", "")
    assert captured.get("runtimeInfo", {}).get("model") == "test-model"
    assert captured.get("runtimeInfo", {}).get("channel") == "web"
    assert captured.get("runtimeInfo", {}).get("outputFormat") == "markdown"


async def test_build_system_splits_builtin_and_mcp_tools(db):
    captured = {}

    def handler(req):
        import json
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={"prompt": "ok"})

    class FakeMCP:
        def server_instructions_snapshot(self):
            return [{"server": "serena", "instructions": "Use symbols before grep."}]

    tools = _tools()
    tools.add("Read", "读取文件", {"type": "object", "properties": {}}, lambda args: "")
    tools.add(
        "mcp__serena__find_symbol",
        "Find symbol",
        {"type": "object", "properties": {}},
        lambda args: "",
        source="mcp",
    )
    cb = ContextBuilder(_mem(handler), MessageDAO(db), SummaryDAO(db), [], tools, "/workspace", mcp_manager=FakeMCP())
    await cb.build_system()

    assert captured["toolNames"] == ["Read", "mcp__serena__find_symbol"]
    assert captured["builtinToolNames"] == ["Read"]
    assert captured["builtinToolSummaries"] == {"Read": "读取文件"}
    assert captured["mcpToolNames"] == ["mcp__serena__find_symbol"]
    assert captured["mcpToolSummaries"] == {"mcp__serena__find_symbol": "Find symbol"}
    assert captured["mcpToolGroups"] == [{
        "server": "serena",
        "toolCount": 1,
        "count": 1,
        "namespacePrefix": "",
        "exactToolName": "mcp__serena__find_symbol",
    }]
    assert captured["mcpServerInstructions"] == [{"server": "serena", "instructions": "Use symbols before grep."}]
    assert captured["tools"]["allowlist"] == captured["toolNames"]
    assert captured["tools"]["builtin"]["allowlist"] == ["Read"]
    assert captured["tools"]["mcp"]["allowlist"] == ["mcp__serena__find_symbol"]
    assert captured["tools"]["mcp"]["groups"] == captured["mcpToolGroups"]
    assert captured["tools"]["mcp"]["serverInstructions"] == captured["mcpServerInstructions"]


def test_build_system_prompt_params_groups_multi_tool_mcp_by_namespace():
    params = build_system_prompt_params(mcp_tool_names=[
        "mcp__playwright__browser_close",
        "mcp__playwright__browser_resize",
        "mcp__sequentialthinking__sequentialthinking",
    ])
    assert params["mcpToolGroups"] == [
        {
            "server": "playwright",
            "toolCount": 2,
            "count": 2,
            "namespacePrefix": "mcp__playwright__",
            "exactToolName": "",
        },
        {
            "server": "sequentialthinking",
            "toolCount": 1,
            "count": 1,
            "namespacePrefix": "",
            "exactToolName": "mcp__sequentialthinking__sequentialthinking",
        },
    ]
    assert params["tools"]["mcp"]["groups"] == params["mcpToolGroups"]


async def test_build_system_fallback(db, tmp_path):
    def handler(req):
        return httpx.Response(500, text="boom")
    cb = ContextBuilder(_mem(handler), MessageDAO(db), SummaryDAO(db),
                        [], _tools(), str(tmp_path))
    system = await cb.build_system()
    assert system == _FALLBACK_SYSTEM  # 降级兜底


async def test_build_history_with_summary(db, tmp_path):
    def handler(req):
        return httpx.Response(200, json={"prompt": "x"})
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    await sdao.add(1, "之前聊过天气", 0, 5)
    await mdao.add(1, "user", "现在呢")
    await mdao.add(1, "assistant", "在的")
    cb = ContextBuilder(_mem(handler), mdao, sdao, [], _tools(), str(tmp_path))
    history = await cb.build_history(1)
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert "之前聊过天气" in history[0]["content"]
    assert "<history_messages>" in history[0]["content"]
    assert "<user" in history[0]["content"] and "现在呢" in history[0]["content"]
    assert "<assistant" in history[0]["content"] and "在的" in history[0]["content"]


async def test_wrap_user_has_time(db, tmp_path):
    cb = ContextBuilder(_mem(lambda r: httpx.Response(200, json={"prompt": "x"})),
                        MessageDAO(db), SummaryDAO(db), [], _tools(), str(tmp_path))
    msg = cb.wrap_user("帮我查个东西")
    assert msg["role"] == "user"
    assert "帮我查个东西" in msg["content"]
    assert "当前时间" in msg["content"]
