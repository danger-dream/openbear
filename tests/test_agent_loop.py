"""Agent 循环测试 —— 工具往返 + 无进展打转 + 软约束熔断。

用 FakeBackend 模拟 backend 行为，不打真实上游。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from app.agent.loop import Agent
from app.agent.transcript_repair import MISSING_TOOL_RESULT_TEXT
from app.llm.base import Message, OpenBearLLMError
from app.llm.events import StreamEvent, ToolCall, Usage
from app.llm.openai_responses import _to_responses_input
from app.tools.base import ToolRegistry, ToolRuntimeContext, current_tool_context
from app.tools.files import register_file_tools
from app.tools.user_interaction import UserInteractionManager, register_user_interaction_tools


class FakeBackend:
    """按预设脚本逐轮产出事件。每轮是一个 StreamEvent 列表。"""
    protocol = "fake"

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._round = 0
        self.seen_convos: list[list[Message]] = []
        self.seen_opts: list[dict] = []

    async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts) -> AsyncIterator[StreamEvent]:
        self.seen_convos.append([dict(m) for m in messages])
        self.seen_opts.append(dict(opts))
        script = self._scripts[min(self._round, len(self._scripts) - 1)]
        self._round += 1
        for ev in script:
            yield ev

    async def complete(self, *a, **k):
        raise NotImplementedError


class RecordRenderer:
    """忠实模拟真实 Web 渲染器的「时间线累积 + 追加不覆盖」行为。

    - on_delta 累积当前正文（真实 renderer 把它存进 segments）
    - on_tool 把工具行并入时间线
    - fail / finalize_notice 把错误/提示**追加**到已渲染时间线末尾，不覆盖
    这样测试才能验证「半途出错保留已输出」的真实契约。
    """
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.tool_lines: list[str] = []
        self.final = ""
        self.failed = ""
        self.cuts = 0
        self.tool_update_meta: list[dict[str, str]] = []
        self._timeline: list[str] = []  # 已渲染的正文/工具行（按序）
        self._tail = ""                  # 当前正在流式的正文段

    def _flush_tail(self) -> None:
        if self._tail:
            self._timeline.append(self._tail)
            self._tail = ""

    def _rendered(self) -> str:
        parts = [*self._timeline]
        if self._tail:
            parts.append(self._tail)
        return "\n\n".join(p for p in parts if p)

    async def on_status(self, status: str) -> None:
        self.statuses.append(status)

    async def on_tool(self, tool_line: str) -> None:
        self.tool_lines.append(tool_line)
        self._flush_tail()
        self._timeline.append(tool_line)

    async def on_tool_update(self, tool_line: str, *, tool_call_id: str = "", name: str = "", arguments: str = "") -> None:
        self.tool_update_meta.append({"tool_call_id": tool_call_id, "name": name, "arguments": arguments})
        if self.tool_lines:
            self.tool_lines[-1] = tool_line
        else:
            self.tool_lines.append(tool_line)
        if self._timeline:
            self._timeline[-1] = tool_line
        else:
            self._timeline.append(tool_line)

    async def on_delta(self, full_text: str, reasoning: str = "") -> None:
        self._tail = full_text  # 真实 renderer 用最新 full_text 覆盖 tail 段

    async def finalize(self, full_text: str, reasoning: str = "") -> None:
        self._tail = full_text
        self.final = self._rendered()

    async def finalize_notice(self, note: str) -> None:
        # 追加提示到已渲染时间线末尾，保留前面所有内容
        body = self._rendered()
        self.final = f"{body}\n\n{note}" if body.strip() else note

    async def cut(self) -> None:
        self.cuts += 1
        self._flush_tail()

    async def fail(self, error_text: str) -> None:
        # 追加错误到已渲染时间线末尾，保留前面所有内容
        self.failed = error_text
        body = self._rendered()
        self.final = f"{body}\n\n{error_text}" if body.strip() else error_text


def _echo_registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def _echo(args):
        return f"echo:{args.get('x', '')}"

    reg.add("echo", "echo", {"type": "object", "properties": {"x": {"type": "string"}}}, _echo)
    return reg


async def test_plain_answer():
    backend = FakeBackend([[
        StreamEvent(kind="content", text="你好"),
        StreamEvent(kind="usage", usage=Usage(total_tokens=10)),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    agent = Agent(backend, _echo_registry())
    r = await agent.run([{"role": "user", "content": "hi"}], RecordRenderer(), model="m")
    assert r.text == "你好"
    assert r.rounds == 1
    assert r.usage.total_tokens == 10


async def test_model_request_refresher_runs_after_normal_compaction_and_each_same_turn_request():
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c1", name="echo", arguments='{"x":"hi"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="done"), StreamEvent(kind="finish", finish_reason="stop")],
    ])

    class CompactOnce:
        def __init__(self):
            self.calls = 0

        async def maybe_compact_and_rebuild(self, *, source, prompt_tokens=None, convo=None):
            self.calls += 1
            if self.calls == 1:
                return [{"role": "user", "content": "canonical after normal compaction"}]
            return None

    refresh_calls = 0

    async def refresher(messages):
        nonlocal refresh_calls
        refresh_calls += 1
        if any(message.get("content") == "<runtime stable>" for message in messages):
            return list(messages)
        return list(messages) + [{"role": "user", "content": "<runtime stable>"}]

    original = [{"role": "user", "content": "canonical before compaction"}]
    await Agent(backend, _echo_registry()).run(
        original,
        RecordRenderer(),
        model="m",
        context_compactor=CompactOnce(),
        model_request_refresher=refresher,
    )

    assert refresh_calls == 2
    first_outbound, second_outbound = backend.seen_convos
    assert first_outbound[0]["content"] == "canonical after normal compaction"
    assert first_outbound[-1]["content"] == "<runtime stable>"
    assert second_outbound[:len(first_outbound)] == first_outbound
    assert sum(message.get("content") == "<runtime stable>" for message in second_outbound) == 1
    assert original == [{"role": "user", "content": "canonical before compaction"}]


async def test_tool_then_answer():
    """第1轮调工具 → 回灌 → 第2轮出答案。"""
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c1", name="echo", arguments='{"x":"hi"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="结果是 echo:hi"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])
    agent = Agent(backend, _echo_registry())
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "echo hi"}], rec, model="m")
    assert r.rounds == 2
    assert "echo:hi" in rec.final
    assert r.tools_used == ["echo"]
    # 第2轮 convo 里应有 tool 结果回灌
    second = backend.seen_convos[1]
    assert any(m["role"] == "tool" and "echo:hi" in m.get("content", "") for m in second)


async def test_questionnaire_result_enters_next_model_call_with_free_text_intact():
    registry = ToolRegistry()
    register_user_interaction_tools(registry, UserInteractionManager())
    free_text = "  原始自由文字，不能压缩成标签。\n保留第二行与空格  "

    async def web_confirm(_payload):
        return {
            "status": "answered", "cancelled": False, "interactionId": "q-1",
            "answers": [{
                "questionId": "scope", "type": "choice", "question": "范围？", "required": True,
                "answerMode": "options_with_text", "selectedValues": ["a"], "selectedLabels": ["方案 A"],
                "text": free_text,
            }],
        }

    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(
            id="q-call", name="UserInteraction",
            arguments=json.dumps({
                "action": "questionnaire", "title": "澄清", "body": "回答",
                "questions": [{"id": "scope", "type": "choice", "question": "范围？",
                               "options": [{"label": "方案 A", "value": "a"}]}],
            }, ensure_ascii=False),
        )]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="done"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    await Agent(backend, registry).run(
        [{"role": "user", "content": "clarify"}], RecordRenderer(), model="m",
        tool_context=ToolRuntimeContext(web_confirm=web_confirm, source="web"),
    )
    tool_messages = [message for message in backend.seen_convos[1] if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    result = json.loads(tool_messages[0]["content"])
    assert result["answers"][0]["text"] == free_text
    assert result["answers"][0]["answerMode"] == "options_with_text"
    assert result["answers"][0]["selectedLabels"] == ["方案 A"]


async def test_main_loop_exposes_and_dispatches_real_edit_batch(tmp_path):
    class CapturingBackend(FakeBackend):
        def __init__(self, scripts):
            super().__init__(scripts)
            self.seen_tools: list[list[dict]] = []

        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.seen_tools.append(list(tools or []))
            async for event in super().stream(
                messages,
                model=model,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                **opts,
            ):
                yield event

    path = tmp_path / "main-batch.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry)
    backend = CapturingBackend([
        [
            StreamEvent(
                kind="tool_call",
                tool_calls=[ToolCall(
                    id="batch-1",
                    name="EditBatch",
                    arguments=json.dumps({
                        "path": str(path),
                        "edits": [
                            {"old_string": "alpha", "new_string": "one"},
                            {"old_string": "beta", "new_string": "two"},
                        ],
                    }),
                )],
            ),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(kind="content", text="done"),
            StreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])

    result = await Agent(backend, registry).run(
        [{"role": "user", "content": "apply replacements"}],
        RecordRenderer(),
        model="m",
    )

    first_tools = {item["name"]: item["parameters"] for item in backend.seen_tools[0]}
    assert set(first_tools["Edit"]["properties"]) == {"path", "old_string", "new_string", "replace_all"}
    assert set(first_tools["EditBatch"]["properties"]) == {"path", "edits"}
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"
    assert result.tools_used == ["EditBatch"]



async def test_responses_controller_replays_native_reasoning_and_function_call_once():
    reasoning_item = {
        "type": "reasoning",
        "id": "reasoning-1",
        "summary": [{"type": "summary_text", "text": "先调用工具"}],
        "encrypted_content": "test-opaque-controller-round",
    }
    function_item = {
        "type": "function_call",
        "id": "function-item-1",
        "call_id": "call-1",
        "name": "echo",
        "arguments": '{"x":"native"}',
    }
    backend = FakeBackend([
        [
            StreamEvent(kind="reasoning", text="先调用工具"),
            StreamEvent(kind="native_output_item", native_output_items=[reasoning_item]),
            StreamEvent(kind="native_output_item", native_output_items=[function_item]),
            StreamEvent(
                kind="tool_call",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments='{"x":"native"}')],
            ),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(
                kind="native_output_item",
                native_output_items=[{
                    "type": "message",
                    "id": "message-2",
                    "content": [{"type": "output_text", "text": "完成"}],
                }],
            ),
            StreamEvent(kind="content", text="完成"),
            StreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    backend.protocol = "responses"

    result = await Agent(backend, _echo_registry()).run(
        [{"role": "user", "content": "run"}],
        RecordRenderer(),
        model="m",
        session_id="controller-session",
    )

    assert result.text == "完成"
    assert len(backend.seen_opts) == 2
    assert all(options.get("native_continuation") is True for options in backend.seen_opts)
    second_input = _to_responses_input(backend.seen_convos[1])
    assert sum(item.get("type") == "reasoning" for item in second_input) == 1
    assert sum(item.get("type") == "function_call" for item in second_input) == 1
    assert sum(item.get("type") == "function_call_output" for item in second_input) == 1
    assert not any(item.get("role") == "assistant" for item in second_input)
    assert reasoning_item in second_input
    assert function_item in second_input


async def test_non_responses_controller_does_not_enable_native_continuation():
    backend = FakeBackend([[
        StreamEvent(kind="content", text="plain"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])

    await Agent(backend, _echo_registry()).run(
        [{"role": "user", "content": "plain"}],
        RecordRenderer(),
        model="m",
    )

    assert backend.protocol == "fake"
    assert "native_continuation" not in backend.seen_opts[0]
    assert not any(message.get("native_output_items") for message in backend.seen_convos[0])


async def test_responses_without_native_items_falls_back_to_plain_checkpoint():
    backend = FakeBackend([[
        StreamEvent(kind="content", text="plain fallback"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    backend.protocol = "responses"

    class RecordingPersister:
        def __init__(self):
            self.checkpoints: list[list[Message]] = []

        async def save_assistant(self, **_kwargs):
            return None

        async def save_tool_result(self, **_kwargs):
            return None

        async def save_user(self, **_kwargs):
            return None

        async def save_native_context(self, *, messages):
            self.checkpoints.append(list(messages))

    persister = RecordingPersister()
    result = await Agent(backend, _echo_registry()).run(
        [{"role": "user", "content": "plain"}],
        RecordRenderer(),
        model="m",
        persister=persister,
    )

    assert result.text == "plain fallback"
    assert backend.seen_opts[0].get("native_continuation") is True
    assert len(persister.checkpoints) == 1
    assert not any(message.get("native_output_items") for message in persister.checkpoints[0])


async def test_partial_responses_tool_batch_never_checkpoints_opaque_turn():
    registry = ToolRegistry()

    async def _first(_args):
        return "first-ok"

    async def _second(_args):
        return "second-must-not-run"

    registry.add("first", "first", {"type": "object", "properties": {}}, _first)
    registry.add("second", "second", {"type": "object", "properties": {}}, _second)
    calls = [
        ToolCall(id="first-call", name="first", arguments="{}"),
        ToolCall(id="second-call", name="second", arguments="{}"),
    ]
    backend = FakeBackend([[
        StreamEvent(
            kind="native_output_item",
            native_output_items=[{"type": "reasoning", "encrypted_content": "test-partial"}],
        ),
        StreamEvent(
            kind="native_output_item",
            native_output_items=[
                {"type": "function_call", "call_id": call.id, "name": call.name, "arguments": call.arguments}
                for call in calls
            ],
        ),
        StreamEvent(kind="tool_call", tool_calls=calls),
        StreamEvent(kind="finish", finish_reason="tool_calls"),
    ]])
    backend.protocol = "responses"

    class RecordingPersister:
        def __init__(self):
            self.tool_results: list[str] = []
            self.checkpoints: list[list[Message]] = []

        async def save_assistant(self, **_kwargs):
            return None

        async def save_tool_result(self, *, tool_call_id, **_kwargs):
            self.tool_results.append(tool_call_id)

        async def save_user(self, **_kwargs):
            return None

        async def save_native_context(self, *, messages):
            self.checkpoints.append(list(messages))

    stop_checks = 0

    def _soft_stop():
        nonlocal stop_checks
        stop_checks += 1
        return "stop after first" if stop_checks == 1 else ""

    persister = RecordingPersister()
    result = await Agent(backend, registry).run(
        [{"role": "user", "content": "run two"}],
        RecordRenderer(),
        model="m",
        persister=persister,
        tool_context=ToolRuntimeContext(soft_stop_check=_soft_stop),
    )

    assert result.halted_reason == "soft_stop"
    assert persister.tool_results == ["first-call", "second-call"]
    assert persister.checkpoints == []


async def test_detached_agent_tool_allows_controller_foreground_work():
    reg = ToolRegistry()
    read_called = False

    async def _agent(_args):
        return '{"ok":true,"status":"running","detached":true,"taskUuid":"task-1"}'

    async def _blocked_read(_args):
        nonlocal read_called
        read_called = True
        return "should not run"

    reg.add("Agent", "agent", {"type": "object", "properties": {}}, _agent)
    reg.add("Read", "read", {"type": "object", "properties": {}}, _blocked_read)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="a1", name="Agent", arguments="{}")]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="r1", name="Read", arguments="{}")]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="Agent 已在后台运行，完成后我会汇总。"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])

    rec = RecordRenderer()
    result = await Agent(backend, reg).run([{"role": "user", "content": "delegate"}], rec, model="m")

    assert result.halted_reason == ""
    assert len(backend.seen_convos) == 3
    assert read_called is True
    assert result.tools_used == ["Agent", "Read"]
    assert "后台运行" in rec.final


async def test_multiple_detached_agent_calls_then_controller_summarizes_wait_state():
    import json

    reg = ToolRegistry()
    agent_calls = []

    async def _agent(args):
        agent_calls.append(args)
        return json.dumps({
            "ok": True,
            "status": "running",
            "detached": True,
            "taskUuid": f"task-{len(agent_calls)}",
        }, ensure_ascii=False)

    reg.add("Agent", "agent", {"type": "object", "properties": {}}, _agent)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[
            ToolCall(id="a1", name="Agent", arguments='{"prompt":"one"}'),
            ToolCall(id="a2", name="Agent", arguments='{"prompt":"two"}'),
        ]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="两个 Agent 都已启动，等完成通知后汇总。"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])

    rec = RecordRenderer()
    result = await Agent(backend, reg).run([{"role": "user", "content": "delegate"}], rec, model="m")

    assert result.halted_reason == ""
    assert len(backend.seen_convos) == 2
    assert [call["prompt"] for call in agent_calls] == ["one", "two"]
    assert result.tools_used == ["Agent", "Agent"]
    assert "两个 Agent 都已启动" in rec.final


async def test_completed_agent_result_allows_controller_write_user_decision_and_new_agent():
    reg = ToolRegistry()
    agent_calls: list[dict] = []
    writes: list[dict] = []
    interactions: list[dict] = []
    wait_calls = 0

    async def _agent(args):
        agent_calls.append(dict(args))
        index = len(agent_calls)
        if index == 1:
            return json.dumps({
                "ok": True,
                "status": "running",
                "detached": True,
                "taskUuid": "task-1",
            })
        return json.dumps({
            "ok": True,
            "status": "completed",
            "task": {
                "taskUuid": f"task-{index}",
                "status": "completed",
                "modelCalls": 1,
                "toolCalls": 1,
            },
            "result": {"summary": f"Agent {index} completed"},
        })

    async def _agent_wait(_args):
        nonlocal wait_calls
        wait_calls += 1
        return json.dumps({
            "ok": True,
            "wakeReason": "all_terminal",
            "summary": {"running": 0, "waitingControl": 0, "terminal": 1, "total": 1},
            "agents": [{"taskUuid": "task-1", "status": "completed"}],
        })

    async def _write(args):
        writes.append(dict(args))
        return "plan written"

    async def _user_interaction(args):
        interactions.append(dict(args))
        return json.dumps({
            "status": "answered",
            "cancelled": False,
            "selectedValues": ["approved"],
            "selectedLabels": ["批准方案"],
        }, ensure_ascii=False)

    empty_schema = {"type": "object", "properties": {}}
    reg.add("Agent", "agent", empty_schema, _agent)
    reg.add("AgentWait", "wait", empty_schema, _agent_wait)
    reg.add("Write", "write", empty_schema, _write)
    reg.add("UserInteraction", "interaction", empty_schema, _user_interaction)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[
            ToolCall(id="a1", name="Agent", arguments='{"prompt":"research"}'),
        ]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[
            ToolCall(id="aw1", name="AgentWait", arguments='{"mode":"event_only"}'),
        ]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[
            ToolCall(id="w1", name="Write", arguments='{"path":"plan.md","content":"plan"}'),
        ]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[
            ToolCall(
                id="u1",
                name="UserInteraction",
                arguments='{"action":"select","title":"批准方案","body":"请选择","options":["批准方案"]}',
            ),
        ]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[
            ToolCall(id="a2", name="Agent", arguments='{"prompt":"implement approved plan"}'),
        ]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="调研、规划、决策和实施均已完成。"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])

    rec = RecordRenderer()
    pers = _RecPersister()
    result = await Agent(backend, reg).run(
        [{"role": "user", "content": "research, plan, decide, then implement"}],
        rec,
        model="m",
        persister=pers,
    )

    assert agent_calls == [
        {"prompt": "research"},
        {"prompt": "implement approved plan"},
    ]
    assert wait_calls == 1
    assert writes == [{"path": "plan.md", "content": "plan"}]
    assert interactions == [{
        "action": "select",
        "title": "批准方案",
        "body": "请选择",
        "options": ["批准方案"],
    }]
    assert result.tools_used == ["Agent", "AgentWait", "Write", "UserInteraction", "Agent"]
    assert result.rounds == 6
    assert all("agent_controller_tool_required" not in content for _id, _name, content, _ms in pers.tools)
    assert "调研、规划、决策和实施均已完成" in rec.final


async def test_completed_agent_batch_allows_memory_integration_afterwards():
    reg = ToolRegistry()
    memory_calls: list[tuple[str, dict]] = []

    async def _agent(_args):
        return (
            '{"ok":true,"status":"completed",'
            '"task":{"taskUuid":"task-1","status":"completed","modelCalls":1,"toolCalls":1},'
            '"result":{"summary":"发现了需要保存的状态"}}'
        )

    async def _memory(args):
        memory_calls.append(("Memory", dict(args)))
        return '{"ok":true,"saved":"long-term"}'

    async def _task_memory(args):
        memory_calls.append(("TaskMemory", dict(args)))
        return '{"ok":true,"saved":"conversation"}'

    empty_schema = {"type": "object", "properties": {}}
    reg.add("Agent", "agent", empty_schema, _agent)
    reg.add("Memory", "long-term memory", empty_schema, _memory)
    reg.add("TaskMemory", "conversation memory", empty_schema, _task_memory)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="a1", name="Agent", arguments="{}")]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[
             ToolCall(id="m1", name="Memory", arguments='{"action":"set"}'),
             ToolCall(id="tm1", name="TaskMemory", arguments='{"action":"create"}'),
         ]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="已汇总并保存 Agent 结果。"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])

    rec = RecordRenderer()
    pers = _RecPersister()
    result = await Agent(backend, reg).run(
        [{"role": "user", "content": "delegate and preserve"}],
        rec,
        model="m",
        persister=pers,
    )

    assert memory_calls == [
        ("Memory", {"action": "set"}),
        ("TaskMemory", {"action": "create"}),
    ]
    assert result.tools_used == ["Agent", "Memory", "TaskMemory"]
    assert all("agent_controller_tool_required" not in content for _id, _name, content, _ms in pers.tools)
    assert "已汇总并保存 Agent 结果" in rec.final


async def test_detached_agent_tool_drains_queued_steers_into_next_controller_round():
    reg = ToolRegistry()

    async def _agent(_args):
        return '{"ok":true,"status":"running","detached":true,"taskUuid":"task-1"}'

    reg.add("Agent", "agent", {"type": "object", "properties": {}}, _agent)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="a1", name="Agent", arguments="{}")]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="已把补充要求交给主控处理"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])
    queued = ["运行中补充一句"]
    drain_calls = 0

    def _drain_after_tool():
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls < 2:
            return []
        return [queued.pop(0)] if queued else []

    pers = _RecPersister()
    rec = RecordRenderer()

    result = await Agent(backend, reg).run(
        [{"role": "user", "content": "delegate"}],
        rec,
        model="m",
        persister=pers,
        steer_drain=_drain_after_tool,
    )

    assert result.halted_reason == ""
    assert queued == []
    assert pers.users == ["运行中补充一句"]
    second = backend.seen_convos[1]
    assert any(m["role"] == "user" and "运行中补充一句" in str(m.get("content", "")) for m in second)
    assert "补充要求" in rec.final


async def test_detached_agent_requires_explicit_agent_wait_and_stays_in_same_run():
    reg = ToolRegistry()

    async def _agent(_args):
        return '{"ok":true,"status":"running","detached":true,"taskUuid":"task-1"}'

    async def _agent_wait(_args):
        return json.dumps({
            "ok": True,
            "wakeReason": "all_terminal",
            "summary": {"running": 0, "waitingControl": 0, "terminal": 1, "total": 1},
            "agents": [{"taskUuid": "task-1", "status": "completed"}],
        })

    reg.add("Agent", "agent", {"type": "object", "properties": {}}, _agent)
    reg.add("AgentWait", "wait", {"type": "object", "properties": {}}, _agent_wait)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="a1", name="Agent", arguments="{}")]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w1", name="AgentWait", arguments='{"mode":"event_only"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="Agent 已完成，统一汇总。"), StreamEvent(kind="finish", finish_reason="stop")],
    ])

    rec = RecordRenderer()
    result = await Agent(backend, reg).run(
        [{"role": "user", "content": "delegate"}], rec, model="m",
    )

    assert len(backend.seen_convos) == 3
    assert any(message.get("name") == "AgentWait" for message in backend.seen_convos[2])
    assert result.text == "Agent 已完成，统一汇总。"
    assert result.rounds == 3


async def test_agent_wait_terminal_latch_skips_repeats_and_new_agent_unlocks_it():
    reg = ToolRegistry()
    agent_calls = 0
    wait_calls = 0

    async def _agent(_args):
        nonlocal agent_calls
        agent_calls += 1
        return json.dumps({
            "ok": True,
            "status": "running",
            "detached": True,
            "taskUuid": f"task-{agent_calls}",
        })

    async def _agent_wait(_args):
        nonlocal wait_calls
        wait_calls += 1
        return json.dumps({
            "ok": True,
            "wakeReason": "all_terminal",
            "summary": {"running": 0, "waitingControl": 0, "terminal": 1, "total": 1},
            "agents": [{"taskUuid": f"task-{wait_calls}", "status": "completed"}],
        })

    reg.add("Agent", "agent", {"type": "object", "properties": {}}, _agent)
    reg.add("AgentWait", "wait", {"type": "object", "properties": {}}, _agent_wait)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="a1", name="Agent", arguments="{}")]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w1", name="AgentWait", arguments='{"mode":"event_only"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w-repeat", name="AgentWait", arguments='{"mode":"event_only"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="a2", name="Agent", arguments="{}")]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w2", name="AgentWait", arguments='{"mode":"event_only"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="两轮 Agent 均已完成。"), StreamEvent(kind="finish", finish_reason="stop")],
    ])

    result = await Agent(backend, reg).run(
        [{"role": "user", "content": "run two generations"}], RecordRenderer(), model="m",
    )

    assert agent_calls == 2
    assert wait_calls == 2  # the redundant middle AgentWait never reaches the runtime callback
    repeat_result = next(
        message for message in backend.seen_convos[3]
        if message.get("role") == "tool" and message.get("tool_call_id") == "w-repeat"
    )
    repeat_payload = json.loads(repeat_result["content"])
    assert repeat_payload["alreadyTerminal"] is True
    assert repeat_payload["skipped"] is True
    assert result.text == "两轮 Agent 均已完成。"


async def test_detached_agent_allows_unrelated_foreground_tool_before_waiting():
    reg = ToolRegistry()
    reads = []

    async def _agent(_args):
        return '{"ok":true,"status":"running","detached":true,"taskUuid":"task-1"}'

    async def _read(args):
        reads.append(args)
        return "foreground result"

    async def _wait(_args):
        return json.dumps({"ok": True, "wakeReason": "all_terminal", "summary": {"running": 0, "waitingControl": 0, "terminal": 1}})

    reg.add("Agent", "agent", {"type": "object", "properties": {}}, _agent)
    reg.add("Read", "read", {"type": "object", "properties": {}}, _read)
    reg.add("AgentWait", "wait", {"type": "object", "properties": {}}, _wait)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="a1", name="Agent", arguments="{}")]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="r1", name="Read", arguments='{"path":"foreground"}')]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w1", name="AgentWait", arguments='{"mode":"event_only"}')]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="两项工作都完成。"), StreamEvent(kind="finish", finish_reason="stop")],
    ])

    result = await Agent(backend, reg).run([{"role": "user", "content": "delegate then do foreground"}], RecordRenderer(), model="m")

    assert reads == [{"path": "foreground"}]
    assert result.text == "两项工作都完成。"


async def test_agent_wait_is_rejected_when_batched_with_foreground_tool():
    reg = ToolRegistry()
    wait_called = False

    async def _agent(_args):
        return '{"ok":true,"status":"running","detached":true,"taskUuid":"task-1"}'

    async def _read(_args):
        return "done"

    async def _wait(_args):
        nonlocal wait_called
        wait_called = True
        return "should not run"

    reg.add("Agent", "agent", {"type": "object", "properties": {}}, _agent)
    reg.add("Read", "read", {"type": "object", "properties": {}}, _read)
    reg.add("AgentWait", "wait", {"type": "object", "properties": {}}, _wait)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="a1", name="Agent", arguments="{}")]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[
            ToolCall(id="r1", name="Read", arguments='{"path":"x"}'),
            ToolCall(id="w1", name="AgentWait", arguments='{"mode":"event_only"}'),
        ]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w2", name="AgentWait", arguments='{"mode":"event_only"}')]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="done"), StreamEvent(kind="finish", finish_reason="stop")],
    ])

    await Agent(backend, reg).run([{"role": "user", "content": "x"}], RecordRenderer(), model="m")

    assert wait_called is True  # only the later standalone AgentWait dispatches
    second_convo = backend.seen_convos[2]
    assert any("foreground_work_in_same_batch" in str(message.get("content") or "") for message in second_convo if message.get("role") == "tool")


async def test_model_call_hook_runs_after_each_upstream_call_before_next_round():
    reg = _echo_registry()
    backend = FakeBackend([
        [StreamEvent(
            kind="usage",
            usage=Usage(input_tokens=10, output_tokens=2),
            details={"serviceTier": "default", "providerCostUsd": 0.0012},
        ),
         StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c1", name="echo", arguments='{"text":"x"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(
            kind="usage",
            usage=Usage(input_tokens=20, output_tokens=3),
            details={"serviceTier": "priority", "providerCostUsd": 0.0045},
        ),
         StreamEvent(kind="content", text="done"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    calls = []

    async def _hook(call):
        calls.append(call)

    await Agent(backend, reg).run(
        [{"role": "user", "content": "x"}], RecordRenderer(), model="m", model_call_hook=_hook,
    )

    assert len(calls) == 2
    assert calls[0]["usage"].input_tokens == 10
    assert calls[1]["usage"].input_tokens == 20
    assert calls[0]["serviceTier"] == "default"
    assert calls[0]["providerCostUsd"] == 0.0012
    assert calls[1]["serviceTier"] == "priority"
    assert calls[1]["providerCostUsd"] == 0.0045
    assert all(call["status"] == "ok" for call in calls)
    assert all(call["promptUsageReported"] is True for call in calls)


async def test_model_call_hook_distinguishes_default_zero_usage_from_provider_report():
    backend = FakeBackend([
        [StreamEvent(kind="content", text="done"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    calls = []

    async def _hook(call):
        calls.append(call)

    result = await Agent(backend, ToolRegistry()).run(
        [{"role": "user", "content": "x"}], RecordRenderer(), model="m", model_call_hook=_hook,
    )

    assert len(calls) == 1
    assert isinstance(calls[0]["usage"], Usage)
    assert calls[0]["promptUsageReported"] is False
    assert result.last_prompt_usage_reported is False


async def test_output_only_usage_event_does_not_claim_prompt_usage():
    backend = FakeBackend([[
        StreamEvent(kind="content", text="done"),
        StreamEvent(kind="usage", usage=Usage(output_tokens=7)),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    calls = []

    async def _hook(call):
        calls.append(call)

    result = await Agent(backend, ToolRegistry()).run(
        [{"role": "user", "content": "x"}],
        RecordRenderer(),
        model="m",
        model_call_hook=_hook,
    )

    assert calls[0]["usage"].output_tokens == 7
    assert calls[0]["promptUsageReported"] is False
    assert result.last_prompt_usage_reported is False


async def test_model_request_overlay_is_request_local_and_reaches_the_provider():
    canonical = [{"role": "user", "content": "hello"}]
    backend = FakeBackend([
        [StreamEvent(kind="content", text="done"), StreamEvent(kind="finish", finish_reason="stop")],
    ])

    async def _overlay(messages):
        cloned = [dict(message) for message in messages]
        cloned[-1]["content"] += "\n\n<runtime-checkpoint>save state</runtime-checkpoint>"
        return cloned

    await Agent(backend, ToolRegistry()).run(
        canonical,
        RecordRenderer(),
        model="m",
        model_request_overlay=_overlay,
    )

    assert canonical == [{"role": "user", "content": "hello"}]
    assert "<runtime-checkpoint>" in str(backend.seen_convos[0][-1]["content"])


async def test_model_request_overlay_never_targets_transient_retry_user_message():
    canonical = [{"role": "user", "content": "real user request"}]
    backend = FlakeyBackend(fail_times=1)

    async def _overlay(messages):
        cloned = [dict(message) for message in messages]
        cloned[-1]["content"] += "\n\n<runtime-checkpoint>save state</runtime-checkpoint>"
        return cloned

    result = await Agent(
        backend,
        ToolRegistry(),
        max_retries=1,
        retry_backoff_s=0,
    ).run(
        canonical,
        RecordRenderer(),
        model="m",
        model_request_overlay=_overlay,
    )

    assert result.text == "成功了"
    assert canonical == [{"role": "user", "content": "real user request"}]
    retry_request = backend.seen_messages[-1]
    assert "<runtime-checkpoint>" in str(retry_request[0]["content"])
    assert "transient upstream error" in str(retry_request[-1]["content"])
    assert "<runtime-checkpoint>" not in str(retry_request[-1]["content"])


async def test_model_call_hook_records_retryable_failure_before_retry():
    backend = FlakeyBackend(fail_times=1, retryable=True)
    calls = []

    async def _hook(call):
        calls.append(call)

    result = await Agent(backend, ToolRegistry(), max_retries=1, retry_backoff_s=0).run(
        [{"role": "user", "content": "x"}], RecordRenderer(), model="m", model_call_hook=_hook,
    )

    assert result.text == "成功了"
    assert [call["status"] for call in calls] == ["error", "ok"]
    assert result.model_calls == 2
    # RunResult preserves terminal-failure semantics; the immutable ledger hook
    # records the retryable failed physical request separately.
    assert result.model_fail == 0
    assert result.model_ok == 1


async def test_agent_result_preflight_uses_latest_controller_prompt_and_multi_result_tokens():
    reg = ToolRegistry()

    async def _agent_wait(_args):
        return json.dumps({
            "ok": True,
            "wakeReason": "all_terminal",
            "summary": {"running": 0, "waitingControl": 0, "terminal": 2, "total": 2},
            "notifications": [
                {"taskUuid": "task-a", "status": "completed", "resultOutputTokens": 3000},
                {"taskUuid": "task-b", "status": "completed", "resultOutputTokens": 4000},
            ],
            "resultOutputTokens": 7000,
            "resultCount": 2,
        })

    reg.add("AgentWait", "wait", {"type": "object", "properties": {}}, _agent_wait, preserve_result=True)
    backend = FakeBackend([
        [
            StreamEvent(kind="usage", usage=Usage(input_tokens=9000, cache_read_tokens=1000, output_tokens=20)),
            StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w1", name="AgentWait", arguments='{"mode":"event_only"}')]),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [StreamEvent(kind="content", text="已汇总两个 Agent。"), StreamEvent(kind="finish", finish_reason="stop")],
    ])

    class Gate:
        def __init__(self):
            self.calls: list[dict[str, object]] = []

        async def maybe_compact_and_rebuild(self, *, source, prompt_tokens=None, convo=None):
            self.calls.append({"source": source, "prompt_tokens": prompt_tokens, "convo": list(convo or [])})
            return None

    gate = Gate()
    persister = _RecPersister()
    result = await Agent(backend, reg).run(
        [{"role": "user", "content": "等待两个 Agent"}],
        RecordRenderer(),
        model="m",
        context_compactor=gate,
        persister=persister,
    )

    assert result.text == "已汇总两个 Agent。"
    preflight = next(call for call in gate.calls if call["source"] == "agent_result_preflight")
    assert preflight["prompt_tokens"] == 17_256
    assert sum(call["source"] == "agent_result_preflight" for call in gate.calls) == 1

    # Raw provider-reported output size still drives preflight, but neither the
    # current controller request nor durable history receives those internals.
    model_tool = next(
        message for message in backend.seen_convos[1]
        if message.get("role") == "tool" and message.get("tool_call_id") == "w1"
    )
    model_payload = json.loads(model_tool["content"])
    assert "resultOutputTokens" not in model_payload
    assert "resultCount" not in model_payload
    assert all("resultOutputTokens" not in item for item in model_payload["notifications"])
    persisted_payload = json.loads(next(item[2] for item in persister.tools if item[1] == "AgentWait"))
    assert persisted_payload == model_payload
    gate_payload = json.loads(next(
        message["content"] for message in preflight["convo"]
        if message.get("role") == "tool" and message.get("tool_call_id") == "w1"
    ))
    assert gate_payload == model_payload


async def test_agent_result_preflight_does_not_unlock_redundant_agent_wait():
    reg = ToolRegistry()
    wait_calls = 0

    async def _agent(_args):
        return json.dumps({"ok": True, "status": "running", "detached": True, "taskUuid": "task-1"})

    async def _agent_wait(_args):
        nonlocal wait_calls
        wait_calls += 1
        return json.dumps({
            "ok": True,
            "wakeReason": "all_terminal",
            "summary": {"running": 0, "waitingControl": 0, "terminal": 1, "total": 1},
            "resultOutputTokens": 500,
            "resultCount": 1,
        })

    reg.add("Agent", "agent", {"type": "object", "properties": {}}, _agent)
    reg.add("AgentWait", "wait", {"type": "object", "properties": {}}, _agent_wait, preserve_result=True)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="a1", name="Agent", arguments="{}")]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w1", name="AgentWait", arguments='{"mode":"event_only"}')]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w2", name="AgentWait", arguments='{"mode":"event_only"}')]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="done"), StreamEvent(kind="finish", finish_reason="stop")],
    ])

    class RebuildingGate:
        def __init__(self):
            self.sources: list[str] = []

        async def maybe_compact_and_rebuild(self, *, source, prompt_tokens=None, convo=None):
            self.sources.append(source)
            if source == "agent_result_preflight":
                return [dict(message) for message in (convo or [])]
            return None

    gate = RebuildingGate()
    await Agent(backend, reg).run(
        [{"role": "user", "content": "delegate"}], RecordRenderer(), model="m", context_compactor=gate,
    )

    assert wait_calls == 1
    assert gate.sources.count("agent_result_preflight") == 1
    repeated = next(
        message for message in backend.seen_convos[3]
        if message.get("role") == "tool" and message.get("tool_call_id") == "w2"
    )
    assert json.loads(repeated["content"])["alreadyTerminal"] is True


async def test_context_compaction_gate_runs_after_tool_batch_and_replaces_convo():
    reg = ToolRegistry()

    async def _echo(args):
        return f"echo:{args.get('x', '')}"

    reg.add("echo", "echo", {"type": "object", "properties": {"x": {"type": "string"}}}, _echo)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c1", name="echo", arguments='{"x":"hi"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="压缩后回答"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])

    class Gate:
        def __init__(self):
            self.calls: list[dict[str, object]] = []

        async def maybe_compact_and_rebuild(self, *, source, prompt_tokens=None, convo=None):
            self.calls.append({"source": source, "convo": [dict(m) for m in (convo or [])]})
            if source == "tool_batch":
                assert any(m.get("role") == "tool" and "echo:hi" in str(m.get("content", "")) for m in (convo or []))
                return [{"role": "user", "content": "[摘要] 压缩后的上下文"}]
            return None

    gate = Gate()
    rec = RecordRenderer()
    result = await Agent(backend, reg).run(
        [{"role": "user", "content": "echo hi"}],
        rec,
        model="m",
        context_compactor=gate,
    )

    assert result.text == "压缩后回答"
    assert [call["source"] for call in gate.calls] == ["pre_model_request", "tool_batch", "pre_model_request"]
    second = backend.seen_convos[1]
    assert second == [{"role": "user", "content": "[摘要] 压缩后的上下文"}]


async def test_context_compaction_gate_uses_provider_snapshot_for_responses_tool_loop():
    """A real provider snapshot must beat the incomplete local Responses estimate.

    Responses native items are replayed verbatim on the next request but are not
    represented by the ordinary message-content estimate. After a tool batch, the
    prior provider prompt is therefore the only reliable compaction trigger.
    """
    reasoning_item = {
        "type": "reasoning",
        "id": "reasoning-1",
        "encrypted_content": "opaque-reasoning",
    }
    function_item = {
        "type": "function_call",
        "id": "function-1",
        "call_id": "echo-1",
        "name": "echo",
        "arguments": '{"x":"provider"}',
    }
    backend = FakeBackend([
        [
            StreamEvent(kind="native_output_item", native_output_items=[reasoning_item]),
            StreamEvent(kind="native_output_item", native_output_items=[function_item]),
            StreamEvent(
                kind="usage",
                usage=Usage(input_tokens=782, cache_read_tokens=271_616, output_tokens=3),
            ),
            StreamEvent(
                kind="tool_call",
                tool_calls=[ToolCall(id="echo-1", name="echo", arguments='{"x":"provider"}')],
            ),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(kind="content", text="done"),
            StreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    backend.protocol = "responses"

    class Gate:
        def __init__(self):
            self.calls: list[dict[str, object]] = []

        async def maybe_compact_and_rebuild(self, *, source, prompt_tokens=None, convo=None):
            self.calls.append({"source": source, "prompt_tokens": prompt_tokens, "convo": list(convo or [])})
            if source == "tool_batch":
                assert prompt_tokens == 272_398
                assert any(message.get("native_output_items") for message in (convo or []))
                return [{"role": "user", "content": "[summary] compacted"}]
            return None

    gate = Gate()
    result = await Agent(backend, _echo_registry()).run(
        [{"role": "user", "content": "run"}],
        RecordRenderer(),
        model="m",
        session_id="provider-usage-compaction",
        context_compactor=gate,
    )

    assert result.text == "done"
    assert [(call["source"], call["prompt_tokens"]) for call in gate.calls] == [
        ("pre_model_request", None),
        ("tool_batch", 272_398),
        # The rebuilt context must not immediately be compacted again using the
        # same old provider snapshot before the next response provides a new one.
        ("pre_model_request", None),
    ]
    assert backend.seen_convos[1] == [{"role": "user", "content": "[summary] compacted"}]


async def test_tool_progress_update_receives_current_tool_call_metadata():
    reg = ToolRegistry()

    async def _progress(args):
        ctx = current_tool_context()
        assert ctx.progress_update is not None
        await ctx.progress_update("Bash: still running")
        return "done"

    reg.add("progress", "progress", {"type": "object", "properties": {}}, _progress)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="p1", name="progress", arguments="{}")]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="完成"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])
    rec = RecordRenderer()
    await Agent(backend, reg).run([{"role": "user", "content": "progress"}], rec, model="m")

    assert any(item["tool_call_id"] == "p1" for item in rec.tool_update_meta)
    assert any(item["name"] == "progress" and item["arguments"] == "{}" for item in rec.tool_update_meta)


async def test_late_structured_progress_retains_originating_tool_call_metadata():
    reg = ToolRegistry()
    retained_progress: dict[str, object] = {}

    async def _first(_args):
        callback = current_tool_context().progress_update_payload
        assert callback is not None
        retained_progress["callback"] = callback
        return "first done"

    async def _second(_args):
        callback = retained_progress["callback"]
        assert callable(callback)
        await callback({"status": "running"})
        return "second done"

    reg.add("first", "first", {"type": "object", "properties": {}}, _first)
    reg.add("second", "second", {"type": "object", "properties": {}}, _second)
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="first-call", name="first", arguments='{"origin":1}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="second-call", name="second", arguments='{"origin":2}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="完成"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])

    class StructuredProgressRenderer(RecordRenderer):
        def __init__(self) -> None:
            super().__init__()
            self.progress_meta: list[dict[str, object]] = []

        async def on_tool_progress(self, tool_call_id: str, name: str, arguments: str, payload: dict) -> None:
            self.progress_meta.append({
                "toolCallId": tool_call_id,
                "name": name,
                "arguments": arguments,
                "payload": dict(payload),
            })

    rec = StructuredProgressRenderer()
    await Agent(backend, reg).run([{"role": "user", "content": "run both"}], rec, model="m")

    assert rec.progress_meta == [{
        "toolCallId": "first-call",
        "name": "first",
        "arguments": '{"origin":1}',
        "payload": {"status": "running"},
    }]


class _RecPersister:
    """记录 loop 落库的 assistant / tool / steering user 单元，验证完整持久化。"""
    def __init__(self):
        self.assistants = []  # (content, reasoning, signature, [tool names])
        self.tools = []       # (call_id, name, content)
        self.users = []       # steering 注入的 user 文本

    async def save_assistant(self, *, content, reasoning, signature, tool_calls):
        self.assistants.append((content, reasoning, signature, [t.name for t in tool_calls]))

    async def save_tool_result(self, *, tool_call_id, name, content, duration_ms=0):
        self.tools.append((tool_call_id, name, content, duration_ms))

    async def save_user(self, *, content):
        self.users.append(content)


async def test_persist_full_timeline_with_tools():
    """多轮工具对话：assistant(含工具调用+思考) 与 工具结果 都必须实时落库。"""
    backend = FakeBackend([
        [StreamEvent(kind="reasoning", text="先想想"),
         StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c1", name="echo", arguments='{"x":"hi"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="结果是 echo:hi"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])
    agent = Agent(backend, _echo_registry())
    pers = _RecPersister()
    await agent.run([{"role": "user", "content": "echo hi"}], RecordRenderer(),
                    model="m", persister=pers)
    # 落库了 2 条 assistant：第1轮(带工具调用+思考)、第2轮(最终文本)
    assert len(pers.assistants) == 2
    assert pers.assistants[0][3] == ["echo"]      # 第1轮带工具调用
    assert pers.assistants[0][1] == "先想想"        # 第1轮思考入库
    assert pers.assistants[1][0] == "结果是 echo:hi"  # 最终文本
    # 落库了 1 条工具结果
    assert len(pers.tools) == 1
    assert pers.tools[0][1] == "echo" and "echo:hi" in pers.tools[0][2]


async def test_persist_single_round_no_tools():
    """无工具单轮：只落 1 条 assistant，无工具结果。"""
    backend = FakeBackend([[
        StreamEvent(kind="content", text="你好"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    agent = Agent(backend, _echo_registry())
    pers = _RecPersister()
    await agent.run([{"role": "user", "content": "hi"}], RecordRenderer(),
                    model="m", persister=pers)
    assert len(pers.assistants) == 1 and pers.assistants[0][0] == "你好"
    assert pers.tools == []


async def test_no_progress_halt():
    """连续重复同样工具调用且无文本 → 打转熔断。"""
    repeat = [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c", name="echo", arguments='{"x":"a"}')]),
              StreamEvent(kind="finish", finish_reason="tool_calls")]
    backend = FakeBackend([repeat])  # 永远重复同一轮
    agent = Agent(backend, _echo_registry(), no_progress_rounds=3)
    r = await agent.run([{"role": "user", "content": "loop"}], RecordRenderer(), model="m")
    assert r.halted_reason == "no_progress"


async def test_no_progress_halt_closes_pending_tool_calls():
    repeat = [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c", name="echo", arguments='{"x":"a"}')]),
              StreamEvent(kind="finish", finish_reason="tool_calls")]
    backend = FakeBackend([repeat])
    pers = _RecPersister()
    r = await Agent(backend, _echo_registry(), no_progress_rounds=1).run(
        [{"role": "user", "content": "loop"}], RecordRenderer(), model="m", persister=pers)

    assert r.halted_reason == "no_progress"
    assert len(pers.assistants) == 1
    assert len(pers.tools) == 1
    assert pers.tools[0][0] == "c"
    assert MISSING_TOOL_RESULT_TEXT in pers.tools[0][2]


async def test_soft_stop_closes_unexecuted_tool_calls():
    reg = ToolRegistry()
    async def first(_args):
        return "first-ok"
    async def second(_args):  # pragma: no cover - should not execute
        return "second-should-not-run"
    reg.add("first", "first", {"type": "object", "properties": {}}, first)
    reg.add("second", "second", {"type": "object", "properties": {}}, second)
    backend = FakeBackend([[
        StreamEvent(kind="tool_call", tool_calls=[
            ToolCall(id="a", name="first", arguments="{}"),
            ToolCall(id="b", name="second", arguments="{}"),
        ]),
        StreamEvent(kind="finish", finish_reason="tool_calls"),
    ]])
    pers = _RecPersister()
    r = await Agent(backend, reg).run(
        [{"role": "user", "content": "run"}], RecordRenderer(), model="m", persister=pers,
        tool_context=ToolRuntimeContext(soft_stop_check=lambda: "用户停止"),
    )

    assert r.halted_reason == "soft_stop"
    assert [item[0] for item in pers.tools] == ["a", "b"]
    assert pers.tools[0][2] == "first-ok"
    assert MISSING_TOOL_RESULT_TEXT in pers.tools[1][2]


async def test_wall_budget_zero_means_unlimited():
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c1", name="echo", arguments='{"x":"1"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="继续完成"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    agent = Agent(backend, _echo_registry(), max_run_wall_seconds=0)
    r = await agent.run([{"role": "user", "content": "x"}], RecordRenderer(), model="m")
    assert r.halted_reason == ""
    assert r.text == "继续完成"


async def test_no_progress_allows_paged_reads_when_arguments_change():
    """同工具翻页读取时 offset 变化代表有进展，不能按重复工具调用熔断。"""
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c1", name="echo", arguments='{"path":"big.log","offset":0}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c2", name="echo", arguments='{"path":"big.log","offset":200}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c3", name="echo", arguments='{"path":"big.log","offset":400}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="翻页读完"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    agent = Agent(backend, _echo_registry(), no_progress_rounds=3)
    r = await agent.run([{"role": "user", "content": "分页读"}], RecordRenderer(), model="m")
    assert r.halted_reason == ""
    assert r.text == "翻页读完"


class FlakeyBackend:
    """前 fail_times 次在产出前抛可重试错误，之后成功。"""
    protocol = "fake"

    def __init__(self, fail_times: int, retryable: bool = True):
        self._fail_times = fail_times
        self._retryable = retryable
        self.attempts = 0
        self.seen_messages: list[list[Message]] = []

    async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        self.seen_messages.append([dict(message) for message in messages])
        self.attempts += 1
        if self.attempts <= self._fail_times:
            from app.llm.base import OpenBearLLMError
            raise OpenBearLLMError("上游波动", status=503, retryable=self._retryable)
        yield StreamEvent(kind="content", text="成功了")
        yield StreamEvent(kind="finish", finish_reason="stop")

    async def complete(self, *a, **k):
        raise NotImplementedError


async def test_retry_then_succeed():
    """前2次失败 → 重试 → 第3次成功。"""
    backend = FlakeyBackend(fail_times=2)
    agent = Agent(backend, _echo_registry(), max_retries=2, retry_backoff_s=0.01)
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "hi"}], rec, model="m")
    assert backend.attempts == 3
    assert r.text == "成功了"
    # 计数不变式：calls == ok + fail + retry
    assert r.model_calls == 3
    assert r.model_retry == 2
    assert r.model_ok == 1
    assert r.model_fail == 0
    assert r.model_calls == r.model_ok + r.model_fail + r.model_retry


async def test_physical_retry_retains_reconciled_context_without_duplicate_runtime_state():
    backend = FlakeyBackend(fail_times=2)
    refresh_calls = 0

    async def refresher(messages):
        nonlocal refresh_calls
        refresh_calls += 1
        if any(message.get("content") == "<stable-runtime-state>" for message in messages):
            return list(messages)
        return list(messages) + [{"role": "user", "content": "<stable-runtime-state>"}]

    result = await Agent(
        backend, _echo_registry(), max_retries=2, retry_backoff_s=0,
    ).run(
        [{"role": "user", "content": "hi"}],
        RecordRenderer(),
        model="m",
        model_request_refresher=refresher,
    )

    assert result.text == "成功了"
    assert refresh_calls == 3
    assert len(backend.seen_messages) == 3
    assert backend.seen_messages[1][:len(backend.seen_messages[0])] == backend.seen_messages[0]
    assert backend.seen_messages[2][:len(backend.seen_messages[1])] == backend.seen_messages[1]
    assert sum(
        message.get("content") == "<stable-runtime-state>"
        for message in backend.seen_messages[-1]
    ) == 1


async def test_secondary_format_error_does_not_replace_first_structured_primary_cause():
    class PrimaryThenFormatBackend:
        protocol = "fake"

        def __init__(self):
            self.attempts = 0

        async def stream(self, messages, *, model, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise OpenBearLLMError(
                    "rate_limit: Too many requests",
                    status=429,
                    transport_status=503,
                    upstream_status=429,
                    retryable=True,
                    reason="rate_limit",
                    summary="当前账户请求过于频繁，请稍后再试",
                    root_cause={
                        "status": 429,
                        "classification": "rate_limit",
                        "message": "Too many requests",
                        "retryable": True,
                    },
                    details={"summary": "当前账户请求过于频繁，请稍后再试"},
                    structured=True,
                )
            raise OpenBearLLMError("invalid response continuation format", status=400, retryable=False)
            yield  # pragma: no cover

    backend = PrimaryThenFormatBackend()
    rec = RecordRenderer()
    result = await Agent(backend, _echo_registry(), max_retries=2, retry_backoff_s=0).run(
        [{"role": "user", "content": "hi"}], rec, model="m",
    )

    assert backend.attempts == 2
    assert result.model_fail == 1
    assert "当前账户请求过于频繁，请稍后再试" in rec.final
    assert "format" not in rec.final.lower()


async def test_retry_exhausted_fails():
    """超过重试次数仍失败 → 如实报错。"""
    backend = FlakeyBackend(fail_times=5)
    agent = Agent(backend, _echo_registry(), max_retries=2, retry_backoff_s=0.01)
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "hi"}], rec, model="m")
    assert backend.attempts == 3  # 1 + 2 retries
    assert rec.failed
    assert r.model_calls == 3
    assert r.model_retry == 2
    assert r.model_fail == 1
    assert r.model_ok == 0
    assert r.model_calls == r.model_ok + r.model_fail + r.model_retry


async def test_parrot_candidate_scoped_quota_is_not_retried_as_a_whole_request():
    class CandidateQuotaBackend:
        protocol = "fake"

        def __init__(self):
            self.attempts = 0

        async def stream(self, messages, *, model, **kwargs):
            self.attempts += 1
            raise OpenBearLLMError(
                "insufficient_quota: Monthly spending limit reached",
                status=429,
                transport_status=503,
                upstream_status=429,
                retryable=True,
                summary="本月消费额度已用完",
                root_cause={
                    "status": 429,
                    "classification": "quota_exhausted",
                    "code": "insufficient_quota",
                    "message": "Monthly spending limit reached",
                    "retryable": True,
                    "retry_scope": "next_candidate",
                },
                details={"summary": "本月消费额度已用完"},
                structured=True,
            )
            yield  # pragma: no cover

    backend = CandidateQuotaBackend()
    rec = RecordRenderer()
    result = await Agent(backend, _echo_registry(), max_retries=2, retry_backoff_s=0).run(
        [{"role": "user", "content": "hi"}], rec, model="m",
    )

    assert backend.attempts == 1
    assert result.model_retry == 0
    assert result.model_fail == 1
    assert "本月消费额度已用完" in rec.final


async def test_non_retryable_no_retry():
    """不可重试错误（如参数错误）→ 不重试，直接报错。"""
    backend = FlakeyBackend(fail_times=5, retryable=False)
    agent = Agent(backend, _echo_registry(), max_retries=2, retry_backoff_s=0.01)
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "hi"}], rec, model="m")
    assert backend.attempts == 1  # 不重试
    assert rec.failed
    assert r.model_calls == 1
    assert r.model_fail == 1
    assert r.model_retry == 0
    assert r.model_ok == 0


async def test_retry_after_content_produced_preserves_partial_and_recovers():
    """已产出内容后遇到瞬时错误 → 保留 partial，携带恢复上下文继续重试。"""
    class MidFailBackend:
        protocol = "fake"
        def __init__(self):
            self.attempts = 0
            self.seen_messages = []
        async def stream(self, messages, *, model, **k):
            self.attempts += 1
            self.seen_messages.append(messages)
            if self.attempts == 1:
                yield StreamEvent(kind="content", text="已经输出一半")
                from app.llm.base import OpenBearLLMError
                raise OpenBearLLMError("中途断了", status=503, retryable=True)
            yield StreamEvent(kind="content", text="，现在补完")
            yield StreamEvent(kind="finish", finish_reason="stop")
        async def complete(self, *a, **k):
            raise NotImplementedError

    backend = MidFailBackend()
    agent = Agent(backend, _echo_registry(), max_retries=3, retry_backoff_s=0)
    rec = RecordRenderer()
    result = await agent.run([{"role": "user", "content": "hi"}], rec, model="m")
    assert backend.attempts == 2
    assert result.text == "已经输出一半，现在补完"
    assert backend.seen_messages[1][-2]["content"] == "已经输出一半"
    assert "Continue the same task" in backend.seen_messages[1][-1]["content"]


async def test_retry_wait_can_be_cancelled_without_reissuing_request():
    class FailBackend:
        protocol = "fake"
        def __init__(self):
            self.attempts = 0
        async def stream(self, messages, *, model, **kwargs):
            self.attempts += 1
            raise OpenBearLLMError("busy", status=503, retryable=True)
            yield  # pragma: no cover

    backend = FailBackend()
    rec = RecordRenderer()
    result = await Agent(backend, _echo_registry(), max_retries=10, retry_backoff_s=10).run(
        [{"role": "user", "content": "hi"}],
        rec,
        model="m",
        retry_cancel_check=lambda: True,
    )
    assert backend.attempts == 1
    assert result.halted_reason == "retry_cancelled"
    assert "已取消模型重试" in rec.final


async def test_error_in_tool_followup_round_preserves_prior_output():
    """回归(503清屏 bug)：错误发生在「工具调用后的续写轮」，本轮 full_text 为空，
    但前面已有正文+工具行。出错收尾必须**保留**前面所有输出，只在末尾追加错误，
    绝不能把时间线覆盖成只剩一句错误。
    """
    class ToolThenFailBackend:
        protocol = "fake"

        def __init__(self):
            self.round = 0

        async def stream(self, messages, *, model, **k):
            self.round += 1
            if self.round == 1:
                # 第1轮：先吐正文，再发起工具调用
                yield StreamEvent(kind="content", text="我先查一下进程")
                yield StreamEvent(kind="tool_call",
                                  tool_calls=[ToolCall(id="c", name="echo", arguments='{"x":"a"}')])
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
                return
            # 第2轮（工具结果回灌后的续写）：还没吐任何正文就 503
            from app.llm.base import OpenBearLLMError
            raise OpenBearLLMError("上游服务错误（HTTP 503）", status=503, retryable=False)
            yield  # pragma: no cover

        async def complete(self, *a, **k):
            raise NotImplementedError

    backend = ToolThenFailBackend()
    agent = Agent(backend, _echo_registry())
    rec = RecordRenderer()
    await agent.run([{"role": "user", "content": "x"}], rec, model="m")
    # 前面的正文和工具行必须还在，错误追加在末尾
    assert "我先查一下进程" in rec.final, "503 后前面的正文被覆盖丢失了"
    assert "echo" in rec.final, "503 后工具行被覆盖丢失了"
    assert ("上游服务异常" in rec.final or "❌" in rec.final), "错误提示应追加在末尾"


async def test_retryable_error_event_before_content_retries():
    class EventFailBackend:
        protocol = "fake"

        def __init__(self):
            self.attempts = 0

        async def stream(self, messages, *, model, **k):
            self.attempts += 1
            if self.attempts == 1:
                yield StreamEvent(kind="error", error="rate limit", retryable=True)
                return
            yield StreamEvent(kind="content", text="成功")
            yield StreamEvent(kind="finish", finish_reason="stop")

        async def complete(self, *a, **k):
            raise NotImplementedError

    backend = EventFailBackend()
    agent = Agent(backend, _echo_registry(), max_retries=1, retry_backoff_s=0.01)
    rec = RecordRenderer()
    result = await agent.run([{"role": "user", "content": "x"}], rec, model="m")
    assert backend.attempts == 2
    assert result.text == "成功"


async def test_upstream_error_with_partial():
    backend = FakeBackend([[
        StreamEvent(kind="content", text="部分"),
        StreamEvent(kind="error", error="boom"),
    ]])
    agent = Agent(backend, _echo_registry())
    rec = RecordRenderer()
    await agent.run([{"role": "user", "content": "x"}], rec, model="m")
    assert "部分" in rec.final


async def test_model_call_counters_single_round():
    """无工具单轮：model_calls=1, model_ok=1, 无重试无失败。"""
    backend = FakeBackend([[
        StreamEvent(kind="content", text="hi"),
        StreamEvent(kind="usage", usage=Usage(input_tokens=100, output_tokens=10)),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    agent = Agent(backend, _echo_registry())
    r = await agent.run([{"role": "user", "content": "x"}], RecordRenderer(), model="m")
    assert r.model_calls == 1
    assert r.model_ok == 1
    assert r.model_retry == 0
    assert r.model_fail == 0
    assert r.output_tokens_sum == 10


async def test_model_call_tps_tracks_single_api_calls():
    """峰值/最低 TPS 按真实单次模型 API 调用统计，不按整轮 run 聚合。"""
    class SlowBackend:
        protocol = "fake"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.calls += 1
            await asyncio.sleep(0.005)
            if self.calls == 1:
                yield StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="1", name="echo", arguments='{"x":"a"}')])
                yield StreamEvent(kind="usage", usage=Usage(input_tokens=100, output_tokens=1))
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
            else:
                yield StreamEvent(kind="content", text="done")
                yield StreamEvent(kind="usage", usage=Usage(input_tokens=200, output_tokens=10))
                yield StreamEvent(kind="finish", finish_reason="stop")

    agent = Agent(SlowBackend(), _echo_registry())
    r = await agent.run([{"role": "user", "content": "x"}], RecordRenderer(), model="m")
    assert r.model_ok == 2
    assert r.peak_tps > r.min_tps > 0


async def test_model_call_counters_with_tools():
    """一轮工具 + 一轮收尾：两次成功调用。"""
    backend = FakeBackend([
        [
            StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="1", name="echo", arguments='{"x":"a"}')]),
            StreamEvent(kind="usage", usage=Usage(input_tokens=100, output_tokens=5)),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(kind="content", text="done"),
            StreamEvent(kind="usage", usage=Usage(input_tokens=200, output_tokens=8)),
            StreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    agent = Agent(backend, _echo_registry())
    r = await agent.run([{"role": "user", "content": "x"}], RecordRenderer(), model="m")
    assert r.model_calls == 2
    assert r.model_ok == 2
    assert len(r.tools_used) == 1
    # 最后一轮快照取第二次调用的 prompt
    assert r.last_usage.input_tokens == 200
    assert r.output_tokens_sum == 13


async def test_emergency_compaction_on_context_overflow():
    """上下文超限：首次报错 → 应急压缩重建 convo → 重试成功。"""
    class OverflowThenOK:
        protocol = "fake"
        def __init__(self):
            self.calls = 0
            self.seen_messages = []
        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **k):
            self.calls += 1
            self.seen_messages.append([dict(message) for message in messages])
            if self.calls == 1:
                from app.llm.base import OpenBearLLMError
                raise OpenBearLLMError("This model's maximum context length is 8000 tokens",
                                       status=400, retryable=False)
            yield StreamEvent(kind="content", text="压缩后成功")
            yield StreamEvent(kind="finish", finish_reason="stop")
        async def complete(self, *a, **k):
            raise NotImplementedError

    class FakeCompactor:
        def __init__(self):
            self.called = 0
        async def compact_and_rebuild(self):
            self.called += 1
            return [{"role": "user", "content": "[摘要] 之前的对话"}]

    refresh_calls = 0

    async def refresher(messages):
        nonlocal refresh_calls
        refresh_calls += 1
        cloned = [dict(message) for message in messages]
        cloned[-1]["content"] = f"{cloned[-1].get('content', '')}\n<runtime revision={refresh_calls}>"
        return cloned

    backend = OverflowThenOK()
    comp = FakeCompactor()
    agent = Agent(backend, _echo_registry())
    rec = RecordRenderer()
    r = await agent.run(
        [{"role": "user", "content": "x"}], rec, model="m",
        emergency_compactor=comp,
        model_request_refresher=refresher,
    )
    assert comp.called == 1            # 触发了一次应急压缩
    assert r.text == "压缩后成功"        # 压缩后重试成功
    assert backend.calls == 2
    assert refresh_calls == 2
    assert "<runtime revision=1>" in backend.seen_messages[0][-1]["content"]
    assert "[摘要] 之前的对话" in backend.seen_messages[1][-1]["content"]
    assert "<runtime revision=2>" in backend.seen_messages[1][-1]["content"]
    assert "<runtime revision=1>" not in backend.seen_messages[1][-1]["content"]


async def test_emergency_compaction_after_partial_output_on_context_overflow():
    """上游可能先吐 partial content，再以 context overflow 结束；仍应压缩重试。"""
    class PartialOverflowThenOK:
        protocol = "fake"
        def __init__(self):
            self.calls = 0
        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **k):
            self.calls += 1
            if self.calls == 1:
                from app.llm.base import OpenBearLLMError
                yield StreamEvent(kind="content", text="半截回答")
                raise OpenBearLLMError("context_length_exceeded after partial output", status=400, retryable=False)
            yield StreamEvent(kind="content", text="压缩后完整回答")
            yield StreamEvent(kind="finish", finish_reason="stop")
        async def complete(self, *a, **k):
            raise NotImplementedError

    class FakeCompactor:
        def __init__(self):
            self.called = 0
        async def compact_and_rebuild(self):
            self.called += 1
            return [{"role": "user", "content": "[摘要] 之前的对话"}]

    backend = PartialOverflowThenOK()
    comp = FakeCompactor()
    agent = Agent(backend, _echo_registry())
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "x"}], rec, model="m", emergency_compactor=comp)
    assert comp.called == 1
    assert backend.calls == 2
    assert r.model_retry == 1
    assert r.model_fail == 0
    assert r.text == "压缩后完整回答"
    assert not rec.failed


async def test_emergency_compaction_gives_up_when_cant_compact():
    """压不动了（compactor 返回 None）→ 如实报错，不死循环。"""
    class AlwaysOverflow:
        protocol = "fake"
        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **k):
            from app.llm.base import OpenBearLLMError
            raise OpenBearLLMError("context length exceeded", status=400, retryable=False)
            yield  # noqa
        async def complete(self, *a, **k):
            raise NotImplementedError

    class NoOpCompactor:
        async def compact_and_rebuild(self):
            return None  # 压无可压

    agent = Agent(AlwaysOverflow(), _echo_registry())
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "x"}], rec, model="m",
                        emergency_compactor=NoOpCompactor())
    assert rec.failed  # 如实报错
    assert r.model_fail == 1


async def test_steering_injects_mid_run():
    """运行中插话：steer_drain 在轮间把消息作为 user 注入 convo。"""
    backend = FakeBackend([
        [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="c1", name="echo", arguments='{"x":"a"}')]),
         StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="收到改目标"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])
    # 第一次 drain（首轮模型调用前）为空；第一轮工具结束后，第二轮前才注入插话。
    drain_calls = 0

    def drain():
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 2:
            return ["改个目标：先做 B"]
        return []

    agent = Agent(backend, _echo_registry())
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "做 A"}], rec,
                        model="m", steer_drain=drain)
    assert r.steered == 1
    first = backend.seen_convos[0]
    assert not any("改个目标" in str(m.get("content", "")) for m in first)
    # 第二轮的 convo 里应能看到插话消息
    second = backend.seen_convos[1]
    assert any(m["role"] == "user" and "改个目标" in str(m.get("content", "")) for m in second)
    # 注入时调用了软分段 cut()，让新目标回复另起一条消息
    assert rec.cuts == 1


async def test_steering_before_first_model_call_does_not_cut_empty_draft():
    """首轮前已有排队插话时可直接并入 prompt，但不能封口一个空草稿。"""
    backend = FakeBackend([[
        StreamEvent(kind="content", text="收到两条输入"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    queue = [["补一句：加上 B"]]

    def drain():
        return queue.pop(0) if queue else []

    agent = Agent(backend, _echo_registry())
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "做 A"}], rec,
                        model="m", steer_drain=drain)
    assert r.steered == 1
    assert rec.cuts == 0
    first = backend.seen_convos[0]
    assert any(m["role"] == "user" and "补一句" in str(m.get("content", "")) for m in first)


async def test_steering_arriving_during_final_round_continues_instead_of_silent_drop():
    """最终回答流式生成期间收到插话：定稿前 drain，软分段后继续一轮。"""
    queue: list[str] = []

    class FinalThenSteeredBackend:
        protocol = "fake"

        def __init__(self):
            self.calls = 0
            self.seen_convos: list[list[Message]] = []

        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.calls += 1
            self.seen_convos.append([dict(m) for m in messages])
            if self.calls == 1:
                yield StreamEvent(kind="content", text="原任务回答")
                queue.append("等等，先按新目标 B 继续")
                yield StreamEvent(kind="finish", finish_reason="stop")
                return
            yield StreamEvent(kind="content", text="新目标回答")
            yield StreamEvent(kind="finish", finish_reason="stop")

        async def complete(self, *a, **k):
            raise NotImplementedError

    def drain():
        items = list(queue)
        queue.clear()
        return items

    backend = FinalThenSteeredBackend()
    agent = Agent(backend, _echo_registry())
    rec = RecordRenderer()
    pers = _RecPersister()
    r = await agent.run([{"role": "user", "content": "做 A"}], rec,
                        model="m", steer_drain=drain, persister=pers)

    assert backend.calls == 2
    assert r.text == "新目标回答"
    assert r.steered == 1
    assert rec.cuts == 1
    assert pers.users == ["等等，先按新目标 B 继续"]
    # 第一段 assistant 不能丢：要回灌到第二轮上下文，也要落库。
    second = backend.seen_convos[1]
    assert any(m["role"] == "assistant" and m.get("content") == "原任务回答" for m in second)
    assert any(m["role"] == "user" and "新目标 B" in str(m.get("content", "")) for m in second)
    assert [a[0] for a in pers.assistants] == ["原任务回答", "新目标回答"]


async def test_cancel_preserves_stats_on_shared_result():
    """工具执行后续写阶段被停止时，调用方持有的共享 result 仍含真实统计。

    复现老大遇到的 bug：grep 工具跑很久被强停，统计卡片全 0。根因是 handler 的占位
    result 没拿到 loop 累加的数据。修法是 handler 传入并共享同一个 RunResult，loop
    全程往它累加；这里验证 run 被 cancel 后，那个共享对象里 model_calls/usage/工具
    都已就位，且 start_monotonic 已记录（handler 可据此补 total_time_ms）。
    """
    import asyncio

    from app.agent.result import RunResult

    started = asyncio.Event()

    class HangAfterToolBackend:
        """第1轮调用工具(产出 usage+tool_call)，第2轮续写时永久挂起，等待被取消。"""
        protocol = "fake"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(kind="usage", usage=Usage(input_tokens=120, output_tokens=8))
                yield StreamEvent(kind="tool_call",
                                  tool_calls=[ToolCall(id="c1", name="echo", arguments='{"x":"a"}')])
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
                return
            # 第2轮：标记已进入续写，然后永久挂起 → 模拟工具后长耗时，等外部 cancel
            started.set()
            yield StreamEvent(kind="content", text="开始续写…")
            await asyncio.sleep(3600)

        async def complete(self, *a, **k):
            raise NotImplementedError

    backend = HangAfterToolBackend()
    agent = Agent(backend, _echo_registry())
    shared = RunResult()  # handler 那个占位对象的等价物
    task = asyncio.create_task(
        agent.run([{"role": "user", "content": "跑个慢工具"}], RecordRenderer(),
                  model="m", result=shared))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 关键断言：共享对象拿到了真实统计，不是全 0 占位
    assert shared.model_calls == 2           # 两次上游调用都计了数
    assert shared.usage.input_tokens == 120  # 第1轮 usage 已累加
    assert shared.tools_used == ["echo"]     # 工具执行已记录
    assert shared.start_monotonic > 0        # handler 可据此补算 total_time_ms


async def test_empty_response_retry_then_succeed():
    """模型首轮 finish=stop 但无正文 → 补救重试 → 次轮出正文。"""
    backend = FakeBackend([
        [StreamEvent(kind="finish", finish_reason="stop")],  # 空响应
        [StreamEvent(kind="content", text="这次有答案了"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])
    agent = Agent(backend, _echo_registry(), empty_response_retry_limit=1)
    r = await agent.run([{"role": "user", "content": "hi"}], RecordRenderer(), model="m")
    assert r.text == "这次有答案了"
    assert r.model_retry >= 1
    # 防回归:补救重试发往上游的第二轮 convo 不能出现连续 user(否则 Anthropic 会 400)。
    second = backend.seen_convos[1]
    roles = [m["role"] for m in second]
    assert not any(roles[k] == roles[k + 1] == "user" for k in range(len(roles) - 1)), \
        f"补救重试制造了连续 user: {roles}"


async def test_reasoning_only_retry_then_succeed():
    """模型只吐 reasoning 无正文 → 走 reasoning_only 补救重试 → 次轮出正文。"""
    backend = FakeBackend([
        [StreamEvent(kind="reasoning", text="我在想..."),
         StreamEvent(kind="finish", finish_reason="stop")],  # 只思考无正文
        [StreamEvent(kind="content", text="想好了"),
         StreamEvent(kind="finish", finish_reason="stop")],
    ])
    agent = Agent(backend, _echo_registry(), reasoning_only_retry_limit=2)
    r = await agent.run([{"role": "user", "content": "hi"}], RecordRenderer(), model="m")
    assert r.text == "想好了"
    assert r.model_retry >= 1


async def test_empty_response_retry_gives_up_at_limit():
    """连续空响应达上限 → 不死循环,正常收尾(空回复)。"""
    backend = FakeBackend([[StreamEvent(kind="finish", finish_reason="stop")]])  # 永远空
    agent = Agent(backend, _echo_registry(), empty_response_retry_limit=1)
    r = await agent.run([{"role": "user", "content": "hi"}], RecordRenderer(), model="m")
    # 首轮空 + 重试1次仍空 → 收尾。总调用 = 2(1 原始 + 1 重试)
    assert r.text == ""
    assert r.model_calls == 2


async def test_length_finish_appends_truncation_notice():
    """finish=length → finalize 文案追加截断提示。"""
    backend = FakeBackend([[
        StreamEvent(kind="content", text="很长的回答被截断"),
        StreamEvent(kind="finish", finish_reason="length"),
    ]])
    agent = Agent(backend, _echo_registry())
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "hi"}], rec, model="m")
    assert "很长的回答被截断" in rec.final
    assert "长度上限被截断" in rec.final
    assert r.text == "很长的回答被截断"


async def test_empty_retry_through_real_anthropic_no_consecutive_user():
    """端到端堵盲区:走真实 Anthropic 协议转换 + post_sse,验证 #11 补救重试发往上游的
    messages 角色合法交替(不再连续 user)。这是之前 FakeBackend 绕过转换层没抓到的隐患。
    """
    import json as _json

    import httpx

    from app.llm.anthropic import AnthropicBackend
    from tests.conftest import make_client

    seen_payloads: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = _json.loads(req.content)
        seen_payloads.append(payload)
        roles = [m["role"] for m in payload["messages"]]
        # 核心断言:Anthropic 入参绝不能有连续相同 role
        assert not any(roles[k] == roles[k + 1] for k in range(len(roles) - 1)), \
            f"发往 Anthropic 的 messages 角色未交替: {roles}"
        # 第一次:返回空响应(只有 message_start / message_delta stop,无 text)→ 触发 #11
        # 第二次:返回正常文本
        if len(seen_payloads) == 1:
            lines = [
                'event: message_start',
                'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}',
                '',
                'event: message_delta',
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":0}}',
                '',
                'event: message_stop',
                'data: {"type":"message_stop"}',
                '',
            ]
        else:
            lines = [
                'event: message_start',
                'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}',
                '',
                'event: content_block_start',
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
                '',
                'event: content_block_delta',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"补救成功的回答"}}',
                '',
                'event: message_delta',
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}',
                '',
                'event: message_stop',
                'data: {"type":"message_stop"}',
                '',
            ]
        return httpx.Response(200, text="\n".join(lines) + "\n",
                              headers={"content-type": "text/event-stream"})

    backend = AnthropicBackend(make_client(handler), "https://x", "k")
    agent = Agent(backend, _echo_registry(), empty_response_retry_limit=1)
    rec = RecordRenderer()
    r = await agent.run([{"role": "user", "content": "老大的问题"}], rec, model="claude")
    assert r.text == "补救成功的回答"
    assert len(seen_payloads) == 2          # 确实触发了一次补救重试
    assert r.model_retry >= 1


async def test_multiple_agent_calls_are_normalized_before_persist_and_dispatch():
    """多个 Agent 调用保持独立，并排在普通工具后面执行。"""
    import json

    reg = _echo_registry()
    agent_calls = []

    async def _agent(args):
        agent_calls.append(args)
        index = len(agent_calls)
        return json.dumps({
            "ok": True,
            "status": "completed",
            "task": {"taskUuid": f"t{index}", "status": "completed"},
        }, ensure_ascii=False)

    reg.add("Agent", "agent", {"type": "object", "properties": {}}, _agent)
    backend = FakeBackend([
        [
            StreamEvent(kind="tool_call", tool_calls=[
                ToolCall(id="a", name="Agent", arguments='{"agent":"项目开发专家","prompt":"审查开发侧","title":"开发审查"}'),
                ToolCall(id="e", name="echo", arguments='{"x":"quick"}'),
                ToolCall(id="b", name="Agent", arguments='{"agent":"测试验证专家","prompt":"审查验证侧","title":"验证审查"}'),
            ]),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [StreamEvent(kind="content", text="done"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    rec = RecordRenderer()
    agent = Agent(backend, reg)

    await agent.run([{"role": "user", "content": "审查"}], rec, model="m")

    convo = backend.seen_convos[1]
    assistant = next(msg for msg in convo if msg.get("role") == "assistant" and msg.get("tool_calls"))
    calls = assistant["tool_calls"]
    assert [call.name for call in calls] == ["echo", "Agent", "Agent"]
    assert [call.id for call in calls] == ["e", "a", "b"]
    assert [json.loads(call.arguments)["title"] for call in calls[1:]] == ["开发审查", "验证审查"]
    tool_results = [msg for msg in convo if msg.get("role") == "tool"]
    assert [msg["name"] for msg in tool_results] == ["echo", "Agent", "Agent"]
    assert [msg["tool_call_id"] for msg in tool_results] == ["e", "a", "b"]
    assert [call["title"] for call in agent_calls] == ["开发审查", "验证审查"]
    assert json.loads(tool_results[1]["content"])["task"]["taskUuid"] == "t1"
    assert json.loads(tool_results[2]["content"])["task"]["taskUuid"] == "t2"
