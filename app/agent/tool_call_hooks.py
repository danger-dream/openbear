"""Tool call ingress hooks for OpenBear's normalized ToolCall layer.

Provider adapters first translate raw protocol events (OpenAI Chat tool_calls,
OpenAI Responses function_call items, Anthropic tool_use blocks, ... ) into the
common ToolCall dataclass.  Hooks here run at that boundary, before the calls are
persisted, streamed to Web, or dispatched.
"""
from __future__ import annotations

from app.llm.events import ToolCall

_LONG_AGENT_TOOLS = {"Agent", "AgentMessage"}


def normalize_tool_calls(calls: list[ToolCall]) -> list[ToolCall]:
    """Normalize one model-emitted tool_calls array before OpenBear persists it.

    Long-running Agent tools run after ordinary tools so quick context-gathering
    calls finish first. Multiple Agent calls stay independent; parallelism is
    represented by multiple Agent tool calls, not a batch wrapper.
    """
    if not calls:
        return []
    normal: list[ToolCall] = []
    long_agent: list[ToolCall] = []
    for call in calls:
        if call.name in _LONG_AGENT_TOOLS:
            long_agent.append(call)
        else:
            normal.append(call)
    return [*normal, *long_agent]
