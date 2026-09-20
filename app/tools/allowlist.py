"""Agent-delegation tool names and capability aliases.

Keep model/Agent-facing tool configuration aligned with the tools that OpenBear
actually exposes.  Removed tools may still exist in historical transcripts or old
DB rows; runtime allowlists should silently drop them instead of advertising dead
capabilities to Agents.
"""
from __future__ import annotations

from collections.abc import Iterable

REMOVED_TOOL_NAMES = frozenset({"Glob", "Grep", "WebSearch", "WebExtract"})
# Instance-bound context recovery is not a grant of cross-conversation History
# or an executable business tool. Dispatch still enforces control/finalize gates.
AGENT_RUNTIME_TOOL_NAMES = frozenset({"AgentHistory"})
AGENT_DELEGATION_TOOL_NAMES = frozenset({
    "Read",
    "Write",
    "Edit",
    "Bash",
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
    available: Iterable[str] | None = None,
) -> set[str]:
    """Canonical phase capabilities for runner schemas and inspection surfaces."""
    ordinary = set(sanitize_tool_allowlist(approved if managed else initial)) & set(AGENT_DELEGATION_TOOL_NAMES if available is None else available)
    configured_ceiling = preserve_tool_allowlist(ceiling)
    cap = set(sanitize_tool_allowlist(configured_ceiling))
    # A persisted non-empty ceiling must remain restrictive even when every
    # stored name has since been removed. Empty alone means "no preset limit".
    if configured_ceiling:
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


def preserve_tool_allowlist(tools: Iterable[object] | None) -> list[str]:
    """Normalize stored names without erasing unavailable historical choices."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tools or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def sanitize_tool_allowlist(tools: Iterable[object] | None) -> list[str]:
    """Return runtime-callable names without removed/empty/duplicate entries."""
    return [name for name in preserve_tool_allowlist(tools) if name not in REMOVED_TOOL_NAMES]


def agent_delegation_catalog(registry) -> list[dict]:
    """Only explicitly delegable builtins plus real, currently eligible MCP tools.

    Does not follow current_registry: a running round keeps its own contracts.
    Creation/Continue and the persistent coordinator explicitly use the latest
    registry. Both paths still consult the same live MCP access gate.
    """
    if registry is None:
        return []
    summaries = registry.summaries(scope="agent", source="builtin")
    rows = [{"name": name, "description": summaries[name], "kind": "builtin"}
            for name in sorted(set(registry.names(scope="agent", source="builtin")) & AGENT_DELEGATION_TOOL_NAMES)]
    manager = getattr(registry, "mcp_manager", None)
    if manager is not None:
        registered = set(registry.names(source="mcp"))
        rows.extend({"name": meta.public_name, "description": meta.description, "kind": "mcp",
                     "serverKey": meta.server_key, "originalToolName": meta.original_tool_name}
                    for meta in manager.agent_tools() if meta.public_name in registered
                    and registry.mcp_tool_identities.get(meta.public_name) == (meta.server_key, meta.original_tool_name))
    return rows


def agent_delegation_names(registry) -> set[str]:
    return {row["name"] for row in agent_delegation_catalog(registry)}


def agent_tool_unavailable_reason(registry, name: str) -> str:
    if name in agent_delegation_names(registry):
        return ""
    manager = getattr(registry, "mcp_manager", None)
    if manager is not None and name in registry.names(source="mcp"):
        return manager.agent_tool_unavailable_reason(name) or "agent_tool_not_available"
    return "agent_tool_not_available"


def refresh_agent_tool_enums(registry) -> None:
    """Finalize only the Agent/Plan declaration enums after all tools registered."""
    names = sorted(agent_delegation_names(registry))
    for tool in registry._tools.values():
        props = tool.parameters.get("properties", {})
        if tool.name in {"Agent", "AgentContinue"}:
            props["tools"]["items"]["enum"] = names
        elif tool.name == "AgentPlanDecision":
            props["grantedTools"]["items"]["enum"] = names
        elif tool.name in {"AgentPlanSubmit", "AgentPlanReplan"}:
            requests = props["plan"]["properties"]["toolRequests"]
            requests["maxItems"] = len(names)
            requests["items"]["properties"]["name"]["enum"] = names
