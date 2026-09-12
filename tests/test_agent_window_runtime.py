"""Execute the real Agent loop across local windows without delegating an Agent."""
from __future__ import annotations

import json

import pytest

from app.context.store import ContextOwner, WindowStore
from app.db.engine import DB
from app.llm.base import AgentResult
from app.llm.events import ToolCall, Usage
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.rath.dao import RathDAO
from app.rath.schemas import RathAgentDef
from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.agent_history import register_agent_history_tool
from app.tools.base import ToolRegistry, ToolRuntimeContext


@pytest.fixture
async def env(tmp_path):
    db = DB(str(tmp_path / "agent-window.db"))
    await db.connect()
    dao = RathDAO(db)
    workflow = await ensure_builtin_workflows(dao)
    task_uuid = await dao.create_task(
        chat_id=123, workflow_uuid=workflow, title="Window acceptance",
        input_data={"instruction": "Write exactly six numbered lines. Do not deploy. Then recover the first result from your own history."},
        parent_session_uuid="test-parent",
    )
    agent = RathAgentDef(
        workflow_uuid=workflow, agent_key="window-test", name="Window test", id=1,
        system_prompt="Execute the bounded test", tool_allowlist=["Write"],
        model="test/model", enabled=True,
    )
    yield db, dao, task_uuid, agent
    await db.close()


@pytest.mark.asyncio
async def test_actual_agent_finishes_once_across_windows_and_recovers_evicted_result(env, tmp_path):
    db, dao, task_uuid, agent = env
    registry = ToolRegistry()
    register_agent_history_tool(registry, db)
    target = tmp_path / "result.txt"
    executed = []

    async def write(args):
        n = int(args["line"])
        assert n not in executed, "window rotation re-executed a completed effect"
        executed.append(n)
        with target.open("a") as stream:
            stream.write(f"{n}\n")
        return f"write-step-{n}: " + "original execution output. " * 400

    registry.add("Write", "Append one numbered line", {"type": "object", "properties": {"line": {"type": "integer"}}}, write)

    class Backend:
        protocol = "chat"
        calls = 0

        async def complete(self, messages, *, model, system="", tools=None, **kwargs):
            self.calls += 1
            names = {tool["name"] for tool in tools or []}
            assert "Write" in names and "AgentHistory" in names
            assert "Do not deploy" in "\n".join(str(m.get("content") or "") for m in messages)
            usage = Usage(output_tokens=10)
            if self.calls <= 6:
                return AgentResult(tool_calls=[ToolCall(f"write-{self.calls}", "Write", json.dumps({"line": self.calls}))], usage=usage)
            if self.calls == 7:
                assert all("write-step-1:" not in str(m.get("content") or "") for m in messages if m.get("role") == "tool")
                return AgentResult(tool_calls=[ToolCall("history-index", "AgentHistory", '{"action":"search","query":"write-step-1:"}')], usage=usage)
            if self.calls == 8:
                result = json.loads([m for m in messages if m.get("role") == "tool"][-1]["content"])
                assert result["ok"] and result["events"]
                return AgentResult(tool_calls=[ToolCall("history-body", "AgentHistory", json.dumps({"action": "read", "eventId": result["events"][0]["event_id"], "maxChars": 32000}))], usage=usage)
            result = json.loads([m for m in messages if m.get("role") == "tool"][-1]["content"])
            assert result["ok"]
            assert "write-step-1:" in result["events"][0]["text"]
            assert len(result["events"][0]["text"]) > 1000
            return AgentResult(text="Six lines verified; original first result recovered. No deployment.", usage=usage)

    backend = Backend()
    runner = SingleAgentWorkflowRunner(
        dao, task_uuid, agent=agent, backend=backend, model="test-model", max_tokens=1024,
        tools=registry, context_window=40_000, rollover_trigger_tokens=8000,
        window_retain_ratio=0.15, model_call_limit=20, tool_call_limit=20,
    )
    output = await runner.run()
    assert output["summary"].startswith("Six lines verified")
    assert target.read_text() == "1\n2\n3\n4\n5\n6\n"
    assert backend.calls == 9
    owner = ContextOwner.agent(task_uuid=task_uuid)
    cur = await db.conn.execute("SELECT COUNT(*) AS n FROM context_window_rotations WHERE owner_key=?", (owner.key,))
    assert (await cur.fetchone())["n"] >= 3
    events = await dao.events(task_uuid)
    assert all(event.detail.get("strategy") == "sliding_window" for event in events if event.kind == "model_context_compaction_completed")
    assert not any(event.detail.get("summary") for event in events if event.kind == "model_context_compaction_completed")
    store = WindowStore(db, owner)
    assert await store.restore_messages()
    assert (await store.load())["state"]["stage"] == "completed"


@pytest.mark.asyncio
async def test_agent_history_owner_cannot_be_selected_through_arguments(env):
    db, _, task_uuid, _ = env
    registry = ToolRegistry()
    register_agent_history_tool(registry, db)
    context = ToolRuntimeContext(source="agent:window-test", task_uuid=task_uuid)
    denied = json.loads(await registry.dispatch("AgentHistory", '{"action":"index","taskUuid":"some-other-task"}', context=context))
    assert denied["error"] == "agent_history_owner_is_runtime_bound"
    denied = json.loads(await registry.dispatch("AgentHistory", '{"action":"index"}', context=ToolRuntimeContext()))
    assert denied["error"] == "agent_history_requires_own_running_instance"
