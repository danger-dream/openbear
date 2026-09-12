"""Agent-delegation tool names and capability aliases.

Keep model/Agent-facing tool configuration aligned with the tools that OpenBear
actually exposes.  Removed tools may still exist in historical transcripts or old
DB rows; runtime allowlists should silently drop them instead of advertising dead
capabilities to Agents.
"""
from __future__ import annotations

from collections.abc import Iterable

REMOVED_TOOL_NAMES = frozenset({"Glob", "Grep"})
# Instance-bound context recovery is not a grant of cross-conversation History
# or an executable business tool. Dispatch still enforces control/finalize gates.
AGENT_RUNTIME_TOOL_NAMES = frozenset({"AgentHistory"})
AGENT_DELEGATION_TOOL_NAMES = frozenset({
    "Read",
    "Write",
    "Edit",
    "Bash",
    "WebSearch",
    "WebExtract",
    "Process",
    "TaskMemory",
})

# EditBatch is a distinct model-visible tool contract, but it does not grant a
# broader Agent capability than Edit.  Keep the public Agent allowlist stable:
# granting Edit automatically exposes this one implementation-level companion.
_AGENT_TOOL_ALIASES = {"Edit": frozenset({"EditBatch"})}
_AGENT_TOOL_CAPABILITIES = {"EditBatch": "Edit"}


def expand_agent_tool_names(tools: Iterable[object] | None) -> set[str]:
    """Add model-visible companion tools implied by granted Agent capabilities."""
    expanded = {str(name or "").strip() for name in tools or []}
    expanded.discard("")
    for capability, aliases in _AGENT_TOOL_ALIASES.items():
        if capability in expanded:
            expanded.update(aliases)
    return expanded


def agent_tool_capability(name: object) -> str:
    """Return the canonical Agent permission required for a visible tool name."""
    tool_name = str(name or "").strip()
    return _AGENT_TOOL_CAPABILITIES.get(tool_name, tool_name)


def agent_phase_tool_names(
    initial: Iterable[object], *, managed: bool = False,
    phase: str = "", approved: Iterable[object] = (),
    pending_control: bool = False, ceiling: Iterable[object] = (),
) -> set[str]:
    """Canonical phase capabilities for runner schemas and inspection surfaces."""
    ordinary = set(sanitize_tool_allowlist(approved if managed else initial)) & AGENT_DELEGATION_TOOL_NAMES
    cap = set(sanitize_tool_allowlist(ceiling))
    if cap:
        ordinary &= cap
    if not managed:
        return expand_agent_tool_names(ordinary | AGENT_RUNTIME_TOOL_NAMES | ({"AgentControlAck"} if pending_control else set()))
    protocol = {
        "drafting": {"AgentPlanSubmit"}, "revising": {"AgentPlanSubmit"},
        "executing": {"AgentPlanProgress", "AgentPlanReplan"},
        # Keep the approved schema prefix stable through final output. The
        # dispatcher still rejects non-ack calls once finalization has passed.
        "finalizing": {"AgentPlanProgress", "AgentPlanReplan"},
        "replan_required": {"AgentPlanReplan"},
    }.get(phase or "drafting", set())
    return expand_agent_tool_names((ordinary if phase in {"executing", "finalizing"} else set()) | protocol | AGENT_RUNTIME_TOOL_NAMES | {"AgentControlAck"})


def sanitize_tool_allowlist(tools: Iterable[object] | None) -> list[str]:
    """Return a stable tool allowlist without removed/empty/duplicate entries."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tools or []:
        name = str(raw or "").strip()
        if not name or name in REMOVED_TOOL_NAMES or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out
