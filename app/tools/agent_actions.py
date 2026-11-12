"""One Agent tool for the controller and one AgentPlan tool for Agents, each selecting an earlier tool by action.

The model sees only the merged tools. OpenBear handles every call as the earlier tool it stands
for (its records, cards, waits and gates), so a call behaves the same under either form, and the
earlier names stay callable for conversations whose prompts still name them.
"""
from __future__ import annotations

import copy
import json
from typing import Any

CONTROLLER = {"start": "Agent", "continue": "AgentContinue", "message": "AgentMessage", "stop": "AgentStop",
              "wait": "AgentWait", "info": "AgentInfo", "plan": "AgentPlanDecision"}
AGENT_PLAN = {"submit": "AgentPlanSubmit", "progress": "AgentPlanProgress", "replan": "AgentPlanReplan", "ack": "AgentControlAck"}
MERGED = {"Agent": CONTROLLER, "AgentPlan": AGENT_PLAN}
DEFAULT_ACTION = {"Agent": "start"}
# An earlier tool's own action field travels under this name in the merged tool.
INNER_ACTION = {"AgentInfo": "op", "AgentPlanDecision": "decision", "AgentPlanProgress": "op"}


def _parsed(arguments: Any) -> dict[str, Any] | None:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def canonical_call(name: str, arguments: Any) -> tuple[str, str]:
    """The earlier tool and its own arguments (JSON text) that a call stands for; other calls unchanged."""
    text = arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False)
    table = MERGED.get(name)
    args = _parsed(arguments) if table else None
    if table is None or args is None:
        return name, text
    target = table.get(str(args.get("action") or DEFAULT_ACTION.get(name, "")))
    if target is None:
        return name, text
    inner = {k: v for k, v in args.items() if k != "action"}
    field = INNER_ACTION.get(target)
    if field and field in inner:
        inner["action"] = inner.pop(field)
    return target, json.dumps(inner, ensure_ascii=False)


def canonical_tool(name: str, arguments: Any) -> str:
    return canonical_call(name, arguments)[0]


def phase_schemas(schemas: list[dict[str, Any]], allowed: set[str]) -> list[dict[str, Any]]:
    """An Agent's schemas for the earlier tools its phase allows: AgentPlan keeps only the allowed actions."""
    out = []
    for schema in schemas:
        name = str(schema.get("name") or "")
        if name in MERGED:
            actions = [a for a, target in MERGED[name].items() if target in allowed]
            if actions:
                out.append(_narrowed(schema, actions))
        elif name in allowed:
            out.append(schema)
    return out


def schema_tool_names(schemas: list[dict[str, Any]]) -> set[str]:
    """The earlier tool names a schema list offers, the merged tools counted by their listed actions."""
    names = set()
    for schema in schemas:
        name = str(schema.get("name") or "")
        if name in MERGED:
            enum = schema.get("parameters", {}).get("properties", {}).get("action", {}).get("enum") or []
            names.update(MERGED[name][a] for a in enum if a in MERGED[name])
        else:
            names.add(name)
    return names


def _narrowed(schema: dict[str, Any], actions: list[str]) -> dict[str, Any]:
    """The merged schema offering only these actions and their fields."""
    name = schema["name"]
    if list(actions) == list(MERGED[name]):
        return schema
    narrowed = copy.deepcopy(schema)
    properties = narrowed["parameters"]["properties"]
    properties["action"]["enum"] = list(actions)
    keep = {"action", *_fields_of(name, actions)}
    narrowed["parameters"]["properties"] = {k: v for k, v in properties.items() if k in keep}
    narrowed["description"] = "\n".join(line for line in schema["description"].split("\n")
                                        if not line.startswith("- ") or line[2:].split(" ", 1)[0] in actions)
    return narrowed


# Fields of each action, as named in the merged tool; registered by the merging code.
ACTION_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {}


def _fields_of(name: str, actions: list[str]) -> set[str]:
    table = ACTION_FIELDS.get(name, {})
    return {field for action in actions for field in table.get(action, ())}


def merge_agent_tools(registry, name: str, description: str, about: dict[str, str], *,
                      fields: dict[str, dict[str, Any]] | None = None) -> None:
    """Merge the registered earlier tools of one merged name; about gives each action's one line."""
    table = MERGED[name]
    present = {action: target for action, target in table.items() if target in registry._tools}
    ACTION_FIELDS[name] = {
        action: tuple(INNER_ACTION.get(target, key) if key == "action" else key
                      for key in registry._tools[target].parameters.get("properties", {}))
        for action, target in present.items()}
    lines = [description] + [f"- {action} {{{', '.join(_shown(registry._tools[target], target))}}}: {about[action]}"
                             for action, target in present.items()]
    registry.merge(name, "\n".join(lines), present, default=DEFAULT_ACTION.get(name), inner=INNER_ACTION, fields=fields)


def _shown(tool, target: str) -> list[str]:
    required = set(tool.parameters.get("required") or [])
    out = []
    for key, spec in tool.parameters.get("properties", {}).items():
        shown = INNER_ACTION.get(target, key) if key == "action" else key
        enum = spec.get("enum") if isinstance(spec, dict) and key == "action" else None
        label = f"{shown}: {'|'.join(enum)}" if enum else shown
        out.append(label if key in required else label + "?")
    return out
