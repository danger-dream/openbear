"""Tool call ingress hooks for OpenBear's normalized ToolCall layer.

Provider adapters first translate raw protocol events (OpenAI Chat tool_calls,
OpenAI Responses function_call items, Anthropic tool_use blocks, ... ) into the
common ToolCall dataclass.  Hooks here run at that boundary, before the calls are
persisted, streamed to Web, or dispatched.
"""
from __future__ import annotations

from app.llm.events import ToolCall

_LONG_AGENT_TOOLS = {"Agent", "AgentContinue", "AgentMessage"}


def normalize_tool_calls(calls: list[ToolCall]) -> list[ToolCall]:
    """Normalize one model-emitted tool_calls array before OpenBear persists it.

    Long-running Agent tools run after ordinary tools so quick context-gathering
    calls finish first. Multiple Agent calls stay independent; parallelism is
    represented by multiple Agent tool calls, not a batch wrapper.

    Upstream models occasionally emit the SAME call id twice in one batch
    (observed on Cursor/claude-fable: two complete items sharing one toolu_ id).
    Executing both would duplicate side effects and persist two identical tool
    results, which ``validate_model_context`` then rejects
    (duplicate ids / unpaired results), leaving a turn that can neither
    checkpoint nor let the next controller boundary adopt its rows. Keep the
    first emission of each id (or, when ids are missing, each identical
    name+arguments pair) and drop the rest.
    """
    if not calls:
        return []
    normal: list[ToolCall] = []
    long_agent: list[ToolCall] = []
    seen: set[tuple[str, str] | str] = set()
    for call in calls:
        call_id = str(call.id or "")
        key: str | tuple[str, str] = call_id if call_id else (str(call.name or ""), str(call.arguments or ""))
        if key in seen:
            continue
        seen.add(key)
        if call.name in _LONG_AGENT_TOOLS:
            long_agent.append(call)
        else:
            normal.append(call)
    return [*normal, *long_agent]
