"""MCP v0 tool naming, filtering and approval policy."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from app.config import MCPConfig, MCPServerConfig
from app.mcp.types import MCPRawTool, MCPToolMeta
from app.tools.base import ToolRuntimeContext

_ALLOWED_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_UNDERSCORE_RE = re.compile(r"_+")
_MAX_TOOL_NAME_LEN = 64
_APPROVALS = {"allow", "ask", "deny"}
_HIGH_RISKS = {"write", "destructive", "external", "secret", "unknown"}
_RISK_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("secret", ("secret", "token", "api_key", "apikey", "password", "credential", "cookie", "auth")),
    ("external", ("send", "email", "mail", "post", "publish", "tweet", "slack", "telegram", "webhook")),
    ("destructive", ("delete", "remove", "rm", "drop", "destroy", "wipe", "kill", "shutdown", "restart", "chmod", "chown")),
    ("write", ("write", "create", "update", "replace", "edit", "rename", "move", "patch", "commit", "execute", "run", "shell")),
]
_READ_KEYWORDS = ("read", "list", "search", "find", "get", "fetch", "lookup", "query", "inspect", "overview")


def stable_hash(value: str, length: int = 8) -> str:
    return hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[: max(4, int(length))]


def sanitize_component(value: str, *, fallback: str) -> str:
    raw = str(value or "").strip()
    clean = _ALLOWED_NAME_RE.sub("_", raw)
    clean = _UNDERSCORE_RE.sub("_", clean).strip("_")
    if not clean:
        clean = fallback
    if len(clean) > _MAX_TOOL_NAME_LEN:
        suffix = stable_hash(clean)
        clean = f"{clean[: _MAX_TOOL_NAME_LEN - len(suffix) - 1]}_{suffix}".strip("_")
    return clean or fallback


def _shrink_component(value: str, budget: int, *, fallback: str) -> str:
    budget = max(1, int(budget or 1))
    clean = value or fallback
    if len(clean) <= budget:
        return clean
    suffix = stable_hash(clean)
    if budget <= len(suffix) + 1:
        return suffix[:budget]
    return f"{clean[: budget - len(suffix) - 1].rstrip('_')}_{suffix}".strip("_") or fallback[:budget]


def make_public_tool_name(prefix: str, server_key: str, tool_name: str) -> tuple[str, str, str]:
    safe_prefix = sanitize_component(prefix or "mcp", fallback="mcp")
    safe_server = sanitize_component(server_key, fallback="server")
    safe_tool = sanitize_component(tool_name, fallback="tool")
    public_name = f"{safe_prefix}__{safe_server}__{safe_tool}"
    if len(public_name) <= _MAX_TOOL_NAME_LEN:
        return public_name, safe_server, safe_tool

    # Enforce the final public name limit, not just the tool component limit. LLM
    # providers commonly reject long function/tool names, so long prefixes/server
    # keys must shrink too.  Keep at least a readable fragment plus stable hash for
    # each component when possible.
    suffix = stable_hash(public_name)
    sep_budget = len("______")  # three "__" separators if a final hash is needed
    prefix_budget = max(3, min(len(safe_prefix), 12))
    server_budget = max(6, min(len(safe_server), 18))
    tool_budget = _MAX_TOOL_NAME_LEN - prefix_budget - server_budget - sep_budget - len(suffix)
    if tool_budget < 8:
        deficit = 8 - tool_budget
        reduce_prefix = min(deficit, max(0, prefix_budget - 3))
        prefix_budget -= reduce_prefix
        deficit -= reduce_prefix
        reduce_server = min(deficit, max(0, server_budget - 6))
        server_budget -= reduce_server
        deficit -= reduce_server
        tool_budget = max(8, _MAX_TOOL_NAME_LEN - prefix_budget - server_budget - sep_budget - len(suffix))
    safe_prefix = _shrink_component(safe_prefix, prefix_budget, fallback="mcp")
    safe_server = _shrink_component(safe_server, server_budget, fallback="server")
    safe_tool = _shrink_component(safe_tool, tool_budget, fallback="tool")
    public_name = f"{safe_prefix}__{safe_server}__{safe_tool}_{suffix}"
    if len(public_name) > _MAX_TOOL_NAME_LEN:
        # Last-resort guard for pathological budgets; preserves deterministic name.
        public_name = f"{public_name[: _MAX_TOOL_NAME_LEN - len(suffix) - 1].rstrip('_')}_{suffix}"
    return public_name, safe_server, safe_tool


def ensure_unique_public_name(public_name: str, *, server_key: str, tool_name: str, used: set[str]) -> str:
    if public_name not in used:
        used.add(public_name)
        return public_name
    suffix = stable_hash(f"{server_key}:{tool_name}:{public_name}")
    max_base = max(1, _MAX_TOOL_NAME_LEN - len(suffix) - 1)
    candidate = f"{public_name[:max_base].rstrip('_')}_{suffix}"
    counter = 2
    while candidate in used:
        extra = stable_hash(f"{server_key}:{tool_name}:{counter}", 10)
        max_base = max(1, _MAX_TOOL_NAME_LEN - len(extra) - 1)
        candidate = f"{public_name[:max_base].rstrip('_')}_{extra}"
        counter += 1
    used.add(candidate)
    return candidate


def _patterns(values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            out.append(text)
    return out


def match_candidates(server_key: str, raw_tool_name: str, public_tool_name: str) -> list[str]:
    server = str(server_key or "")
    raw = str(raw_tool_name or "")
    public = str(public_tool_name or "")
    return [
        raw,
        public,
        f"{server}/{raw}",
        f"{server}:{raw}",
        f"{server}/{public}",
        f"{server}:{public}",
    ]


def matches_any(patterns: Iterable[str] | None, candidates: Iterable[str]) -> bool:
    pats = _patterns(patterns)
    if not pats:
        return False
    items = list(candidates)
    for pat in pats:
        for item in items:
            if fnmatch.fnmatchcase(item, pat):
                return True
    return False


def is_tool_allowed(
    mcp_config: MCPConfig,
    server_config: MCPServerConfig,
    *,
    server_key: str,
    raw_tool_name: str,
    public_tool_name: str,
) -> tuple[bool, str]:
    """Return whether a discovered MCP tool should be registered.

    deny is always stronger than allow.  Global and per-server allow lists both act
    as exposure gates when non-empty, matching the design doc priority.
    """
    if not mcp_config.enabled:
        return False, "global_disabled"
    if not server_config.enabled:
        return False, "server_disabled"
    candidates = match_candidates(server_key, raw_tool_name, public_tool_name)
    if matches_any(mcp_config.deny_tools, candidates):
        return False, "global_deny"
    if matches_any(server_config.tools.deny, candidates):
        return False, "server_deny"
    global_allow = _patterns(mcp_config.allow_tools)
    if global_allow and not matches_any(global_allow, candidates):
        return False, "global_allow_not_matched"
    server_allow = _patterns(server_config.tools.allow)
    if server_allow and not matches_any(server_allow, candidates):
        return False, "server_allow_not_matched"
    return True, "allowed"


def _annotation_bool(annotations: dict[str, Any], key: str) -> bool:
    value = annotations.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _contains_keyword(text: str, keyword: str) -> bool:
    """Match semantic word segments instead of arbitrary substrings.

    MCP names commonly use snake/kebab case.  Normalising separators lets ``rm``
    match ``rm_file`` without also matching the middle of ``information``.
    """
    normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    needle = re.sub(r"[^a-z0-9]+", " ", str(keyword or "").lower()).strip()
    return bool(needle and f" {needle} " in f" {normalized} ")


def classify_risk(raw_tool_name: str, description: str = "", annotations: dict[str, Any] | None = None) -> str:
    ann = annotations or {}
    haystack = f"{raw_tool_name}\n{description}".lower()
    # Treat annotations as descriptive hints.  Explicit dangerous words still
    # outrank readOnlyHint, while openWorldHint must not turn a read-only snapshot
    # or search operation into a side-effecting call.
    if _annotation_bool(ann, "destructiveHint"):
        return "destructive"
    for risk, words in _RISK_KEYWORDS:
        if any(_contains_keyword(haystack, word) for word in words):
            return risk
    for risk_value in (ann.get("risk"), ann.get("riskLevel")):
        risk = str(risk_value or "").strip().lower()
        if risk in {"write", "destructive", "external", "secret"}:
            return risk
    if _annotation_bool(ann, "readOnlyHint"):
        return "read"
    if _annotation_bool(ann, "openWorldHint"):
        return "external"
    for risk_value in (ann.get("risk"), ann.get("riskLevel")):
        risk = str(risk_value or "").strip().lower()
        if risk in {"read", "unknown"}:
            return risk
    if any(_contains_keyword(haystack, word) for word in _READ_KEYWORDS):
        return "read"
    return "unknown"


def effective_approval(mcp_config: MCPConfig, server_config: MCPServerConfig, risk: str) -> str:
    """Resolve the operator-configured approval policy.

    Risk is descriptive metadata, not a hidden policy override.  A server explicitly
    configured as ``allow`` is trusted for every exposed tool; silently changing it
    back to ``ask`` makes the admin setting misleading and breaks background/Agent
    use.  New or unconfigured servers remain safe because the global default is
    still ``ask``.
    """
    del risk  # Kept in the signature because callers also build risk metadata here.
    approval = str(server_config.approval or mcp_config.default_approval or "ask").strip().lower()
    return approval if approval in _APPROVALS else "ask"


def build_tool_meta(
    mcp_config: MCPConfig,
    server_config: MCPServerConfig,
    *,
    server_key: str,
    raw_tool: MCPRawTool,
    used_names: set[str],
) -> MCPToolMeta:
    public_name, _safe_server, normalized_tool = make_public_tool_name(
        mcp_config.tool_name_prefix,
        server_key,
        raw_tool.name,
    )
    public_name = ensure_unique_public_name(
        public_name,
        server_key=server_key,
        tool_name=raw_tool.name,
        used=used_names,
    )
    risk = classify_risk(raw_tool.name, raw_tool.description, raw_tool.annotations)
    approval = effective_approval(mcp_config, server_config, risk)
    allowed, reason = is_tool_allowed(
        mcp_config,
        server_config,
        server_key=server_key,
        raw_tool_name=raw_tool.name,
        public_tool_name=public_name,
    )
    return MCPToolMeta(
        public_name=public_name,
        server_key=server_key,
        original_tool_name=raw_tool.name,
        normalized_tool_name=normalized_tool,
        description=raw_tool.description or f"MCP tool {server_key}/{raw_tool.name}",
        input_schema=normalize_input_schema(raw_tool.input_schema),
        annotations=raw_tool.annotations,
        approval=approval,
        risk=risk,
        filtered=not allowed,
        filter_reason=reason,
    )


def normalize_input_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    out = dict(schema)
    out.setdefault("type", "object")
    if out.get("type") != "object":
        out = {"type": "object", "properties": {}, "additionalProperties": True, "description": json.dumps(schema, ensure_ascii=False)[:1000]}
    out.setdefault("properties", {})
    return out


def summarize_arguments(arguments: dict[str, Any], *, max_chars: int = 1200) -> str:
    from app.mcp.output import redact_secrets

    safe = redact_secrets(arguments or {})
    text = json.dumps(safe, ensure_ascii=False, indent=2, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…[arguments truncated, original chars={len(text)}]"


def can_call_without_prompt(meta: MCPToolMeta, context: ToolRuntimeContext) -> tuple[bool, str]:
    """Apply the effective approval policy to one call.

    ``allow`` and ``deny`` are authoritative in every runtime context.  Under
    ``ask``, read-only calls are harmless enough to proceed without interruption;
    side-effecting/unknown calls require an interactive Web decision, or return to
    the main controller when running in a background Agent context.
    """
    if meta.approval == "deny":
        return False, "approval_deny"
    if meta.approval == "allow":
        return True, "allowed_by_policy"
    if meta.risk == "read":
        return True, "allowed_read_context"
    if context.source not in {"chat", "web"}:
        return False, "needs_openbear_control"
    if context.source != "web" or context.web_confirm is None:
        return False, "confirmation_unavailable"
    return False, "confirmation_required"
