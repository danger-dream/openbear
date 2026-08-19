from __future__ import annotations

import json
from types import SimpleNamespace

from app.rath.single_agent import SingleAgentWorkflowRunner
from app.tools.allowlist import agent_tool_capability, expand_agent_tool_names
from app.tools.base import ToolRegistry, current_tool_context
from app.tools.file_state import FileStateStore
from app.tools.files import register_file_tools


def _file_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_file_tools(registry, store=FileStateStore())
    return registry


def _by_name(schemas: list[dict]) -> dict[str, dict]:
    return {str(schema.get("name") or ""): schema for schema in schemas}


def test_edit_batch_is_a_real_agent_visible_contract_under_edit_permission():
    registry = _file_registry()
    names = expand_agent_tool_names({"Edit"})
    schemas = _by_name([
        schema for schema in registry.schemas(scope="agent")
        if schema["name"] in names
    ])

    assert names == {"Edit", "EditBatch"}
    assert agent_tool_capability("EditBatch") == "Edit"
    assert set(schemas) == {"Edit", "EditBatch"}
    assert set(schemas["Edit"]["parameters"]["properties"]) == {
        "path", "old_string", "new_string", "replace_all",
    }
    assert set(schemas["EditBatch"]["parameters"]["properties"]) == {"path", "edits"}


def test_full_properties_required_compiler_cannot_mix_real_edit_contracts():
    schemas = _by_name(_file_registry().schemas(scope="agent"))
    single_keys = set(schemas["Edit"]["parameters"]["properties"])
    batch_keys = set(schemas["EditBatch"]["parameters"]["properties"])

    assert single_keys == {"path", "old_string", "new_string", "replace_all"}
    assert batch_keys == {"path", "edits"}
    assert "edits" not in single_keys
    assert not ({"old_string", "new_string", "replace_all"} & batch_keys)


async def test_runner_allowlist_derives_edit_batch_from_edit_permission():
    registry = _file_registry()

    runner = object.__new__(SingleAgentWorkflowRunner)
    runner.tools = registry
    runner.agent = SimpleNamespace(tool_allowlist=["Edit"])
    runner.plan_protocol_enabled = False
    runner._plan_runtime = {}
    assert {item["name"] for item in await runner._allowed_tool_schemas()} == {
        "Edit", "EditBatch",
    }

    runner.agent = SimpleNamespace(tool_allowlist=["Read"])
    assert {item["name"] for item in await runner._allowed_tool_schemas()} == {"Read"}


class _Dao:
    def __init__(self):
        self.updates: list[dict] = []

    async def update_task(self, _task_uuid: str, **fields):
        self.updates.append(fields)


async def test_runner_dispatches_real_edit_batch_without_execution_alias():
    captured: dict = {}
    registry = ToolRegistry()

    async def edit_handler(args):
        return "single"

    async def batch_handler(args):
        captured["args"] = args
        captured["tool_call_id"] = current_tool_context().tool_call_id
        return "ok"

    registry.add(
        "Edit",
        "single edit",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        edit_handler,
        visibility={"agent", "runtime"},
    )
    registry.add(
        "EditBatch",
        "batch edit",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {"type": "array"},
            },
            "required": ["path", "edits"],
        },
        batch_handler,
        visibility={"agent", "runtime"},
    )
    schemas = registry.schemas(scope="agent")
    events: list[tuple[str, dict]] = []

    runner = object.__new__(SingleAgentWorkflowRunner)
    runner.tools = registry
    runner.agent = SimpleNamespace(agent_key="agent", name="Agent", tool_allowlist=["Edit"])
    runner._allowed_tool_schemas = lambda: _async_value(schemas)
    runner.checkpoint = lambda *_args, **_kwargs: _async_value(None)
    runner.emit = lambda kind, **detail: _append_async(events, kind, detail)
    runner.dao = _Dao()
    runner.steers = []
    runner._pending_control_acks = set()
    runner._plan_runtime = {"phase": "executing", "currentStepId": "r2"}
    runner.task_uuid = "task"
    runner.chat_id = 0
    runner.openbear_session_uuid = "session"
    runner.conversation_uuid = "conversation"
    runner.agent_session_uuid = "agent-session"
    runner.turn_uuid = "turn"
    runner.run_root_turn_uuid = "root"
    runner.task_notification = None
    runner.conversation_event = None
    runner.tool_result_max_chars = 32_000
    runner._tool_calls_made = 0
    runner._work_tool_calls_made = 0
    runner._plan_tool_calls_made = 0

    arguments = json.dumps({
        "path": "file.txt",
        "edits": [{"old_string": "a", "new_string": "b"}],
    })
    result = await runner._dispatch_tool(
        "EditBatch",
        arguments,
        round_no=1,
        tool_call_id="call-batch",
    )

    assert result == "ok"
    assert captured == {
        "args": {
            "path": "file.txt",
            "edits": [{"old_string": "a", "new_string": "b"}],
        },
        "tool_call_id": "call-batch",
    }
    assert [detail["summary"] for _kind, detail in events] == [
        "调用工具 EditBatch", "工具 EditBatch 调用完成",
    ]


async def test_edit_batch_does_not_bypass_plan_step_gate():
    registry = _file_registry()
    schemas = [
        schema for schema in registry.schemas(scope="agent")
        if schema["name"] in {"Edit", "EditBatch"}
    ]
    runner = object.__new__(SingleAgentWorkflowRunner)
    runner.tools = registry
    runner.agent = SimpleNamespace(agent_key="agent", name="Agent", tool_allowlist=["Edit"])
    runner._allowed_tool_schemas = lambda: _async_value(schemas)
    runner.checkpoint = lambda *_args, **_kwargs: _async_value(None)
    runner.emit = lambda *_args, **_kwargs: _async_value(None)
    runner.steers = []
    runner._pending_control_acks = set()
    runner._plan_runtime = {"phase": "executing", "currentStepId": ""}

    payload = json.loads(await runner._dispatch_tool(
        "EditBatch",
        '{"path":"x","edits":[]}',
        round_no=1,
    ))

    assert payload["error"] == "plan_step_not_started"
    assert payload["tool"] == "EditBatch"


async def _async_value(value):
    return value


async def _append_async(events: list, kind: str, detail: dict):
    events.append((kind, detail))
