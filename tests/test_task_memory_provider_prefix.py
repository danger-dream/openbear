from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import uuid

import pytest

import app.task_memory as task_memory
from app.agent.transcript_repair import repair_role_alternation
from app.db.engine import DB
from app.llm.anthropic import AnthropicBackend
from app.llm.events import ToolCall
from app.llm.openai_chat import OpenAIChatBackend
from app.llm.openai_responses import OpenAIResponsesBackend
from app.task_memory import (
    SCOPE_AGENT_TASK,
    SCOPE_CONVERSATION,
    TaskMemoryDAO,
    is_task_memory_runtime_message,
    reconcile_task_memory_runtime_state,
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _unit_stream(units: list[dict]) -> bytes:
    return b"".join(_canonical_bytes(unit) + b"\n" for unit in units)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _unit_hashes(units: list[dict]) -> list[str]:
    return [hashlib.sha256(_canonical_bytes(unit)).hexdigest() for unit in units]


def _lcp_units(left: list[dict], right: list[dict]) -> int:
    count = 0
    for left_unit, right_unit in zip(left, right):
        if _canonical_bytes(left_unit) != _canonical_bytes(right_unit):
            break
        count += 1
    return count


def _lcp_bytes(left: bytes, right: bytes) -> int:
    count = 0
    for left_byte, right_byte in zip(left, right):
        if left_byte != right_byte:
            break
        count += 1
    return count


@pytest.fixture
async def deterministic_task_memory(tmp_path, monkeypatch):
    uuid_counter = itertools.count(1)
    monkeypatch.setattr(
        task_memory.uuid, "uuid4", lambda: uuid.UUID(int=next(uuid_counter)),
    )
    monkeypatch.setattr(task_memory, "now_ts", lambda: 1_700_000_000)
    db = DB(str(tmp_path / "provider-prefix.db"))
    await db.connect()
    dao = TaskMemoryDAO(db)
    shared, _ = await dao.create(
        conversation_uuid="provider-prefix-conversation",
        scope_type=SCOPE_CONVERSATION,
        name="shared deterministic memory",
        description="stable shared catalog item",
        visible_to_agents=True,
    )
    await dao.create(
        conversation_uuid="provider-prefix-conversation",
        scope_type=SCOPE_AGENT_TASK,
        task_uuid="provider-prefix-task",
        name="private deterministic memory",
        description="stable private catalog item",
    )
    try:
        yield dao, shared
    finally:
        await db.close()


async def test_task_memory_final_provider_units_are_complete_prefixes(
    deterministic_task_memory,
):
    dao, shared = deterministic_task_memory
    first_context = await reconcile_task_memory_runtime_state(
        [{"role": "user", "content": "deterministic task"}],
        dao,
        conversation_uuid="provider-prefix-conversation",
        task_uuid="provider-prefix-task",
        epoch=0,
    )
    assert sum(is_task_memory_runtime_message(message) for message in first_context) == 1

    first_outbound = repair_role_alternation(first_context)
    # Cross a wall-clock second: physical-call time must not enter runtime state.
    await asyncio.sleep(1.05)
    second_context = [
        *first_context,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [ToolCall(
                id="call-1", name="Echo", arguments='{"value":"fixed"}',
            )],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "Echo",
            "content": "fixed tool result",
        },
    ]
    second_context = await reconcile_task_memory_runtime_state(
        second_context,
        dao,
        conversation_uuid="provider-prefix-conversation",
        task_uuid="provider-prefix-task",
        epoch=0,
    )
    assert sum(is_task_memory_runtime_message(message) for message in second_context) == 1
    second_outbound = repair_role_alternation(second_context)
    assert second_outbound[:len(first_outbound)] == first_outbound

    shared = await dao.update(
        shared["memoryUuid"],
        conversation_uuid="provider-prefix-conversation",
        scope_type=SCOPE_CONVERSATION,
        expected_revision=shared["revision"],
        changes={"description": "updated deterministic catalog item"},
    )
    third_context = await reconcile_task_memory_runtime_state(
        second_context,
        dao,
        conversation_uuid="provider-prefix-conversation",
        task_uuid="provider-prefix-task",
        epoch=0,
    )
    assert third_context[:len(second_context)] == second_context
    assert sum(is_task_memory_runtime_message(message) for message in third_context) == 2
    third_outbound = repair_role_alternation(third_context)
    assert third_outbound[:len(second_outbound)] == second_outbound

    fourth_context = [
        *third_context,
        {"role": "assistant", "content": "fixed completion"},
        {"role": "user", "content": "new deterministic user turn"},
    ]
    fourth_context = await reconcile_task_memory_runtime_state(
        fourth_context,
        dao,
        conversation_uuid="provider-prefix-conversation",
        task_uuid="provider-prefix-task",
        epoch=0,
    )
    assert sum(is_task_memory_runtime_message(message) for message in fourth_context) == 2
    fourth_outbound = repair_role_alternation(fourth_context)
    assert fourth_outbound[:len(third_outbound)] == third_outbound

    compacted_context = await reconcile_task_memory_runtime_state(
        [{"role": "user", "content": "deterministic compacted summary"}],
        dao,
        conversation_uuid="provider-prefix-conversation",
        task_uuid="provider-prefix-task",
        epoch=1,
    )
    assert len(compacted_context) == 2
    assert compacted_context[-1]["_openbear_runtime"]["epoch"] == 1
    compacted_outbound = repair_role_alternation(compacted_context)
    post_compaction_context = [
        *compacted_context,
        {"role": "assistant", "content": "post-compaction completion"},
        {"role": "user", "content": "post-compaction user"},
    ]
    post_compaction_context = await reconcile_task_memory_runtime_state(
        post_compaction_context,
        dao,
        conversation_uuid="provider-prefix-conversation",
        task_uuid="provider-prefix-task",
        epoch=1,
    )
    post_compaction_outbound = repair_role_alternation(post_compaction_context)
    assert post_compaction_outbound[:len(compacted_outbound)] == compacted_outbound

    system = "deterministic system"
    tools = [{
        "name": "Echo",
        "description": "deterministic tool",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    }]
    session_id = "provider-prefix-session"
    providers = {
        "responses": OpenAIResponsesBackend(None, "https://invalid.example/v1", "unused"),  # type: ignore[arg-type]
        "chat": OpenAIChatBackend(None, "https://invalid.example/v1", "unused"),  # type: ignore[arg-type]
        "anthropic": AnthropicBackend(None, "https://invalid.example/v1", "unused"),  # type: ignore[arg-type]
    }

    evidence: dict[str, dict] = {}
    outbounds = [
        first_outbound,
        second_outbound,
        third_outbound,
        fourth_outbound,
        compacted_outbound,
        post_compaction_outbound,
    ]
    for provider, backend in providers.items():
        payloads = [
            backend.build_payload(
                outbound,
                model="deterministic-model",
                system=system,
                tools=tools,
                max_tokens=1024,
                stream=True,
                think_level="off",
                session_id=session_id,
            )
            for outbound in outbounds
        ]
        unit_key = "input" if provider == "responses" else "messages"
        unit_sequences = [payload[unit_key] for payload in payloads]
        byte_sequences = [_unit_stream(units) for units in unit_sequences]
        transitions = []
        for index in range(1, len(unit_sequences)):
            previous_units = unit_sequences[index - 1]
            current_units = unit_sequences[index]
            previous_bytes = byte_sequences[index - 1]
            current_bytes = byte_sequences[index]
            lcp_units = _lcp_units(previous_units, current_units)
            lcp_bytes = _lcp_bytes(previous_bytes, current_bytes)
            if index == 4:
                # Explicit compaction is the one allowed cold cache-epoch boundary.
                assert current_units[:len(previous_units)] != previous_units
                assert lcp_units < len(previous_units)
                mode = "compaction_cold_boundary"
            else:
                assert current_units[:len(previous_units)] == previous_units
                assert lcp_units == len(previous_units)
                assert current_bytes.startswith(previous_bytes)
                assert lcp_bytes == len(previous_bytes)
                mode = "append_only"
            transitions.append({
                "fromRequest": index,
                "toRequest": index + 1,
                "mode": mode,
                "lcpUnits": lcp_units,
                "lcpBytes": lcp_bytes,
            })
        assert all(payload.get("tools") == payloads[0].get("tools") for payload in payloads)
        assert all(
            "_openbear_runtime" not in json.dumps(payload, ensure_ascii=False)
            for payload in payloads
        )
        if provider == "responses":
            assert all(payload["instructions"] == payloads[0]["instructions"] for payload in payloads)
            provider_system = payloads[0]["instructions"]
        elif provider == "chat":
            assert all(payload["messages"][0] == payloads[0]["messages"][0] for payload in payloads)
            provider_system = payloads[0]["messages"][0]
        else:
            assert all(payload["system"] == payloads[0]["system"] for payload in payloads)
            provider_system = payloads[0]["system"]
        evidence[provider] = {
            "requests": [{
                "unitHashes": _unit_hashes(units),
                "units": len(units),
                "bytes": len(unit_bytes),
            } for units, unit_bytes in zip(unit_sequences, byte_sequences)],
            "transitions": transitions,
            "systemSha256": _sha256(provider_system),
            "toolsSha256": _sha256(payloads[0].get("tools")),
        }

    # Evidence deliberately contains only hashes and counts, never prompt text.
    evidence_line = "PREFIX_EVIDENCE=" + json.dumps(
        evidence, sort_keys=True, separators=(",", ":"),
    )
    assert "deterministic task" not in evidence_line
    assert "fixed tool result" not in evidence_line
    print(evidence_line)
