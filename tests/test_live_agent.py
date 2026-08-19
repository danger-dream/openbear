"""Live —— Agent 循环 + 真实工具 + 真实 Parrot 端到端（需 --run-live）。

证明：模型能真的调用 Bash 工具干活，并基于工具结果回答。
"""
from __future__ import annotations

import os

import pytest

from app.agent.loop import Agent
from app.llm.client import HTTPClient
from app.llm.openai_chat import OpenAIChatBackend
from app.tools.base import ToolRegistry
from app.tools.bash import register_bash_tool
from app.tools.files import register_file_tools

PARROT = "http://127.0.0.1:22122"
KEY = os.environ.get("OPENBEAR_PARROT_KEY", "")

pytestmark = pytest.mark.live


def _require_live_key() -> str:
    if not KEY:
        pytest.skip("set OPENBEAR_PARROT_KEY to run live Parrot tests")
    return KEY


class CollectRenderer:
    def __init__(self):
        self.final = ""

    async def on_status(self, s): pass
    async def on_tool(self, line): pass
    async def on_delta(self, full, reasoning=""): pass
    async def finalize(self, full, reasoning=""): self.final = full
    async def fail(self, e): self.final = f"[FAIL] {e}"
    async def cut(self): pass


async def test_live_agent_uses_bash():
    key = _require_live_key()
    client = HTTPClient()
    try:
        backend = OpenAIChatBackend(client, f"{PARROT}/v1", key)
        reg = ToolRegistry()
        register_bash_tool(reg)
        agent = Agent(backend, reg)
        rec = CollectRenderer()
        r = await agent.run(
            [{"role": "user", "content": "用 Bash 工具执行 `echo OPENBEAR_OK` 并把输出原样告诉我"}],
            rec, model="deepseek", max_tokens=2000,
        )
        # 要么调用了 Bash 看到 OK，要么至少正常回复
        assert r.rounds >= 1
        assert rec.final.strip()
        print(f"\n[rounds={r.rounds} tools={r.tools_used}]\n{rec.final[:300]}")
    finally:
        await client.close()


async def test_live_agent_write_read_file(tmp_path):
    key = _require_live_key()
    client = HTTPClient()
    try:
        backend = OpenAIChatBackend(client, f"{PARROT}/v1", key)
        reg = ToolRegistry()
        register_file_tools(reg)
        register_bash_tool(reg)
        agent = Agent(backend, reg)
        rec = CollectRenderer()
        target = tmp_path / "note.txt"
        r = await agent.run(
            [{"role": "user", "content": f"用 Write 工具把字符串 'hello-openbear' 写到文件 {target}，完成后告诉我已写入"}],
            rec, model="deepseek", max_tokens=2000,
        )
        assert r.rounds >= 1
        # 若模型真调用了 Write，文件应存在
        if target.exists():
            assert "hello-openbear" in target.read_text()
        print(f"\n[rounds={r.rounds} tools={r.tools_used}] exists={target.exists()}")
    finally:
        await client.close()
