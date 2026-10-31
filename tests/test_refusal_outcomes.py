"""Refusal terminal states through the protocol, Web runtime and Agent runner."""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents.control import AgentControlService
from app.agents.execution import AgentExecutor
from app.llm.anthropic import AnthropicBackend
from app.llm.base import AgentResult
from app.llm.events import Usage
from app.tools.base import ToolRegistry
from app.web_console.live_stream import _WebStreamRenderer
from tests.conftest import make_client
from tests.test_controller_model_switch import OLD, originals, setup
from tests.test_rath_single_agent import env as shared_agent_env
from tests.test_web_admin import web_env as web_env

agent_env = shared_agent_env


def response(*, reason, text, thinking, details=None):
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 7}}}]
    blocks = []
    if thinking:
        blocks.append(({"type": "thinking", "thinking": ""},
                       {"type": "thinking_delta", "thinking": "fixture reasoning"}))
    if text:
        blocks.append(({"type": "text", "text": ""}, {"type": "text_delta", "text": text}))
    for index, (block, delta) in enumerate(blocks):
        events.extend([
            {"type": "content_block_start", "index": index, "content_block": block},
            {"type": "content_block_delta", "index": index, "delta": delta},
            {"type": "content_block_stop", "index": index},
        ])
    events.extend([
        {"type": "message_delta", "delta": {"stop_reason": reason, **({"stop_details": details} if details else {})}, "usage": {"output_tokens": 11}},
        {"type": "message_stop"},
    ])
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("reason,text,thinking,calls", [
    ("refusal", "", True, 1),
    ("refusal", "", False, 1),
    ("refusal", "保留这段正文", True, 1),
    ("end_turn", "", True, 3),
    ("end_turn", "", False, 2),
])
async def test_web_rejected_or_empty_response_is_not_completed(
    web_env, monkeypatch, tmp_path, reason, text, thinking, calls,
):
    x = await setup(web_env, monkeypatch, tmp_path, "anthropic")
    requests = []

    def handler(request):
        requests.append(request)
        return response(reason=reason, text=text, thinking=thinking,
                        details={'type': 'refusal', 'category': 'cyber', 'explanation': 'Cyber policy restriction from upstream.'} if reason == 'refusal' else None)

    x.backends[OLD] = AnthropicBackend(make_client(handler), "https://fixture.invalid", "fixture")
    turn = "refusal-test"
    live = web_env.server._live_for(x.row)
    await live.publish({"type": "accepted", "turnUuid": turn})
    assert await web_env.server._run_web_turn(
        x.chat, "test", _WebStreamRenderer(live), conversation=x.row, root_turn_uuid=turn,
    ) is False
    assert len(requests) == calls
    row = await web_env.server._conversation_row(123, x.uuid, require=True)
    assert row["status"] == "error"
    assert "本轮未完成" in row["last_error"]
    assert ("refusal" in row["last_error"]) == (reason == "refusal")
    assert json.loads(row["activity_result_json"])["status"] == "failed"
    cur = await web_env.db.conn.execute("SELECT status FROM runtime_runs WHERE root_turn_uuid=?", (turn,))
    assert [item["status"] for item in await cur.fetchall()] == ["failed"]
    cur = await web_env.db.conn.execute("SELECT * FROM model_calls WHERE chat_id=? ORDER BY id", (x.chat,))
    ledger = await cur.fetchall()
    assert len(ledger) == calls
    assert sum(item["output_tokens"] for item in ledger) == calls * 11
    if reason == "refusal":
        assert ledger[0]["status"] == "error" and ledger[0]["error_type"] == "refusal"
        assert '网络安全（cyber）' in row['last_error']
        assert 'Cyber policy restriction from upstream.' in row['last_error']
    cur = await web_env.db.conn.execute("SELECT payload_json FROM web_operations WHERE conversation_uuid=?", (x.uuid,))
    payloads = [item["payload_json"] for item in await cur.fetchall()]
    assert any("本轮未完成" in payload for payload in payloads)
    if reason == 'refusal':
        assert any('Cyber policy restriction from upstream.' in payload for payload in payloads)
    assert all("（空回复）" not in payload for payload in payloads)
    if text:
        assert any(item["role"] == "assistant" and item["content"] == text for item in await originals(x))
        assert any(text in payload for payload in payloads)


async def test_independent_agent_refusal_fails_once_with_usage(agent_env):
    dao, task_uuid, agent = agent_env
    calls = []

    class Backend:
        protocol = "anthropic"

        async def complete(self, messages, **options):
            calls.append(messages)
            return AgentResult(reasoning="fixture reasoning", finish_reason="refusal",
                               usage=Usage(input_tokens=7, output_tokens=11))

    runner = AgentExecutor(dao, task_uuid, agent=agent, backend=Backend(),
                           model="claude", max_tokens=2048, tools=ToolRegistry())
    manager = AgentControlService(dao)
    await manager.start(task_uuid, 123, lambda _: runner.run())
    task = await dao.get_task(task_uuid)
    assert len(calls) == 1
    assert task.status == "failed"
    assert "refusal" in task.error
    assert task.model_call_count == 1 and task.output_tokens == 11
    events = await dao.events(task_uuid)
    assert not any(event.kind == "model_call_retry" for event in events)
    cur = await dao.db.conn.execute("SELECT status FROM runtime_runs WHERE task_uuid=?", (task_uuid,))
    assert [item["status"] for item in await cur.fetchall()] == ["failed"]
