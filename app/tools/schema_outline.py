"""A short outline of a JSON Schema for describe results: one line per field, the full schema on request."""
from __future__ import annotations

from typing import Any


def outline(schema: dict[str, Any]) -> list[str]:
    """`path: type = default` per field, with the first sentence of its description."""
    defs = schema.get("$defs", {})
    lines: list[str] = []

    def resolve(spec: dict[str, Any]) -> dict[str, Any]:
        ref = spec.get("$ref")
        if ref:
            return defs.get(ref.rsplit("/", 1)[-1], {})
        options = [o for o in spec.get("anyOf", []) if o.get("type") != "null"]
        if len(options) == 1 and "$ref" in options[0]:
            return resolve(options[0])
        return spec

    def type_of(spec: dict[str, Any]) -> str:
        if "enum" in spec:
            return "|".join(map(str, spec["enum"]))
        if "const" in spec:
            return str(spec["const"])
        if "anyOf" in spec:
            return " | ".join(dict.fromkeys(type_of(o) for o in spec["anyOf"]))
        if "$ref" in spec:
            return "object"
        kind = spec.get("type", "any")
        if kind == "array":
            return f"[{type_of(spec.get('items', {}))}]"
        return str(kind)

    def walk(spec: dict[str, Any], path: str) -> None:
        for name, field in spec.get("properties", {}).items():
            here = f"{path}.{name}" if path else name
            target = resolve(field)
            if target.get("properties"):
                walk(target, here)
                continue
            items = resolve(target.get("items", {})) if target.get("type") == "array" else {}
            if items.get("properties"):
                walk(items, here + "[]")
                continue
            line = f"{here}: {type_of(field)}"
            if "default" in field and field["default"] not in (None, [], {}, ""):
                line += f" = {field['default']}"
            # The first sentence, whole: some carry a safety rule.
            note = str(field.get("description") or "").split(". ")[0].strip().rstrip(".")
            lines.append(line + (f" \u2014 {note}" if note else ""))
    walk(schema, "")
    return lines
