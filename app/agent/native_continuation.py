"""Shared safety helpers for provider-native model continuation state.

Provider-native output items are private protocol state, not ordinary transcript
fields.  The helpers here keep Rath and the main Controller aligned on three
invariants: replay one canonical assistant turn, never partially edit an opaque
turn, and reject checkpoints with dangling or mismatched tool calls.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from app.llm.base import Message
from app.llm.events import ToolCall


def _tool_call(call: Any, index: int = 0) -> ToolCall:
    if isinstance(call, ToolCall):
        return call
    data = dict(call) if isinstance(call, dict) else {}
    return ToolCall(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or ""),
        arguments=str(data.get("arguments") or "{}"),
    )


def _tool_call_id(call: Any, index: int = 0) -> str:
    item = _tool_call(call, index)
    return str(item.id or f"call_{index}_{item.name}")


def serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Return a JSON-safe copy without changing provider-native item payloads."""
    payloads: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        calls = item.get("tool_calls") or []
        if calls:
            item["tool_calls"] = [
                asdict(call) if isinstance(call, ToolCall) else dict(call)
                for call in calls
                if isinstance(call, (ToolCall, dict))
            ]
        native_items = item.get("native_output_items") or []
        if native_items:
            item["native_output_items"] = [dict(native) for native in native_items if isinstance(native, dict)]
        payloads.append(item)
    return payloads


def deserialize_messages(payloads: list[dict[str, Any]]) -> list[Message]:
    """Restore ToolCall objects while preserving opaque native item dictionaries."""
    messages: list[Message] = []
    for raw in payloads:
        if not isinstance(raw, dict):
            continue
        item: Message = dict(raw)
        calls = item.get("tool_calls") or []
        if calls:
            item["tool_calls"] = [_tool_call(call, index) for index, call in enumerate(calls)]
        native_items = item.get("native_output_items") or []
        if native_items:
            item["native_output_items"] = [dict(native) for native in native_items if isinstance(native, dict)]
        messages.append(item)
    return messages


def native_items_for_tool_calls(
    native_output_items: list[dict[str, Any]] | None,
    emitted_tool_calls: list[ToolCall] | None,
    accepted_tool_calls: list[ToolCall] | None,
    *,
    has_content: bool = False,
    has_reasoning: bool = False,
) -> list[dict[str, Any]]:
    """Keep a complete opaque turn only when every emitted call was accepted.

    Encrypted reasoning can reference the whole function-call set.  If a caller
    runs only a subset, dropping the entire native turn is safer than rewriting
    provider-owned state.
    """
    items = [dict(item) for item in (native_output_items or []) if isinstance(item, dict)]
    if not items:
        return []
    types = {str(item.get("type") or "") for item in items}
    if has_content and "message" not in types:
        return []
    if has_reasoning and "reasoning" not in types:
        return []
    emitted = list(emitted_tool_calls or [])
    native_call_ids = [
        str(item.get("call_id") or item.get("id") or "")
        for item in items
        if item.get("type") == "function_call"
    ]
    if len(native_call_ids) != len(set(native_call_ids)):
        return []
    if not emitted:
        return [] if native_call_ids else items
    expected = [_tool_call_id(call, index) for index, call in enumerate(emitted)]
    allowed = [_tool_call_id(call, index) for index, call in enumerate(accepted_tool_calls or [])]
    if len(expected) != len(set(expected)) or len(allowed) != len(set(allowed)):
        return []
    return items if set(allowed) == set(expected) and set(native_call_ids) == set(expected) else []


def sanitize_paired_messages(messages: list[Message]) -> tuple[list[Message], int]:
    """Drop dangling calls/results and drop, never partially edit, opaque turns."""
    output_ids = {
        str(message.get("tool_call_id") or "")
        for message in messages
        if message.get("role") == "tool" and str(message.get("tool_call_id") or "")
    }
    kept_call_ids: set[str] = set()
    sanitized: list[Message] = []
    dropped = 0
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            item: Message = dict(message)
            calls = item.get("tool_calls") or []
            original_call_ids: set[str] = set()
            local_kept_ids: set[str] = set()
            if calls:
                kept: list[ToolCall] = []
                for index, call in enumerate(calls):
                    tool_call = _tool_call(call, index)
                    call_id = _tool_call_id(tool_call, index)
                    original_call_ids.add(call_id)
                    if call_id in output_ids:
                        if not tool_call.id:
                            tool_call = ToolCall(
                                id=call_id,
                                name=tool_call.name,
                                arguments=tool_call.arguments,
                            )
                        kept.append(tool_call)
                        kept_call_ids.add(call_id)
                        local_kept_ids.add(call_id)
                    else:
                        dropped += 1
                if kept:
                    item["tool_calls"] = kept
                else:
                    item.pop("tool_calls", None)
            native_items = item.get("native_output_items") or []
            native_call_ids = {
                str(native.get("call_id") or native.get("id") or "")
                for native in native_items
                if isinstance(native, dict) and native.get("type") == "function_call"
            }
            if native_items and (
                original_call_ids != local_kept_ids
                or (native_call_ids and native_call_ids != local_kept_ids)
            ):
                item.pop("native_output_items", None)
            if item.get("content") or item.get("tool_calls") or item.get("reasoning") or item.get("native_output_items"):
                sanitized.append(item)
            elif message.get("tool_calls"):
                continue
            else:
                sanitized.append(item)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id and call_id in kept_call_ids:
                sanitized.append(message)
            else:
                dropped += 1
            continue
        sanitized.append(message)
    return sanitized, dropped


def validate_model_context(messages: list[Message]) -> bool:
    """Validate a private checkpoint without repairing or partially editing it."""
    pending: set[str] = set()
    for message in messages:
        role = str(message.get("role") or "")
        if role == "assistant":
            if pending:
                return False
            calls = list(message.get("tool_calls") or [])
            call_ids = [_tool_call_id(call, index) for index, call in enumerate(calls)]
            if len(call_ids) != len(set(call_ids)) or any(not call_id for call_id in call_ids):
                return False
            native_raw = message.get("native_output_items") or []
            if any(not isinstance(item, dict) for item in native_raw):
                return False
            if native_raw:
                complete_native = native_items_for_tool_calls(
                    native_raw,
                    calls,
                    calls,
                    has_content=bool(message.get("content")),
                    has_reasoning=bool(message.get("reasoning")),
                )
                if len(complete_native) != len(native_raw):
                    return False
            pending = set(call_ids)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if not call_id or call_id not in pending:
                return False
            pending.remove(call_id)
            continue
        if pending:
            return False
    return not pending


def transcript_message_fingerprint(message: Message) -> str:
    """Hash only model-visible neutral transcript fields, never ownership metadata."""
    payload = serialize_messages([message])[0]
    payload.pop("native_output_items", None)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
