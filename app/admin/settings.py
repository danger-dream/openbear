"""Web/TG 共用的设置管理 helper。"""
from __future__ import annotations

from string import Formatter
from typing import Any

from app.rath.prompts import PROMPT_SPECS, render_plan_prompt, validate_prompt_template
from app.settings.specs import GROUPS, SPECS, WEB_DOMAINS, SettingSpec, get_spec

_SENSITIVE_EXACT = {
    "memory.accessKey",
}
_SENSITIVE_SUFFIXES = ("apiKey", "accessKey", "token", "password", "secret", "key")


def is_sensitive_path(path: str) -> bool:
    if path in _SENSITIVE_EXACT:
        return True
    leaf = path.split(".")[-1]
    low = leaf.lower()
    return any(low == item.lower() or low.endswith(item.lower()) for item in _SENSITIVE_SUFFIXES)


def mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return text[:2] + "***" + text[-2:]
    return text[:4] + "***" + text[-4:]


def value_at(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def serialize_spec(spec: SettingSpec) -> dict[str, Any]:
    payload = {
        "path": spec.path,
        "title": spec.title,
        "desc": spec.desc,
        "kind": spec.kind,
        "group": spec.group,
        "effect": spec.effect,
        "min": spec.min_value,
        "max": spec.max_value,
        "unit": spec.unit,
        "choices": [{"value": value, "label": label} for value, label in spec.choices],
        "sensitive": is_sensitive_path(spec.path),
        "editor": spec.editor,
        "variables": list(spec.variables),
        "defaultValue": spec.default_value,
    }
    return payload


def settings_specs_payload() -> dict[str, Any]:
    groups = []
    groups_by_key: dict[str, dict[str, Any]] = {}
    for key, (title, paths) in GROUPS.items():
        payload = {
            "key": key,
            "title": title,
            "paths": [path for path in paths if path in SPECS],
        }
        groups.append(payload)
        groups_by_key[key] = payload
    domains = []
    for key, (title, desc, section_keys) in WEB_DOMAINS.items():
        domains.append({
            "key": key,
            "title": title,
            "desc": desc,
            "sections": [groups_by_key[section_key] for section_key in section_keys if section_key in groups_by_key],
        })
    return {
        "domains": domains,
        "groups": groups,  # backward-compatible for older Web clients
        "specs": {path: serialize_spec(spec) for path, spec in SPECS.items()},
    }


def safe_settings_payload(config_dump: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    masked: dict[str, bool] = {}
    using_builtin: dict[str, bool] = {}
    for path, spec in SPECS.items():
        value = value_at(config_dump, path)
        builtin = spec.editor == "prompt" and not str(value or "").strip()
        values[path] = spec.default_value if builtin else value
        masked[path] = False
        using_builtin[path] = builtin
    return {"values": values, "masked": masked, "usingBuiltin": using_builtin}


def sensitive_value_is_noop(path: str, value: Any, current_value: Any) -> bool:
    spec = get_spec(path)
    if spec is None:
        raise ValueError("未知设置项")
    if not is_sensitive_path(path):
        return False
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return True
        # Refuse stale masked placeholders from older settings pages or clients.
        # Authenticated Web settings now receives the real value, but any
        # mask-looking echo must still never replace the real secret on disk.
        if "***" in text or "••" in text:
            return True
        return bool(current_value) and text == mask_secret(current_value)
    return False


def parse_setting_value(path: str, value: Any) -> Any:
    spec = get_spec(path)
    if spec is None:
        raise ValueError("未知设置项")
    if spec.editor == "prompt":
        parsed = "" if value is None else str(value)
        validate_prompt_setting(path, parsed)
        return parsed
    if isinstance(value, str):
        return spec.parse(value)
    if spec.kind == "bool":
        return bool(value)
    if spec.kind == "int":
        parsed = int(value)
        spec._check_number(parsed)
        return parsed
    if spec.kind == "float":
        parsed = float(value)
        spec._check_number(parsed)
        return parsed
    if spec.kind == "multi":
        if value is None:
            return []
        if isinstance(value, str):
            return spec.parse(value)
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("请选择一个或多个有效选项")
        return spec.validate_choices([str(item) for item in value])
    return "" if value is None else str(value)


def validate_prompt_setting(path: str, template: str) -> None:
    spec = get_spec(path)
    if spec is None or spec.editor != "prompt":
        raise ValueError("未知提示词设置")
    effective = str(template or "").strip() or spec.default_value
    if path in PROMPT_SPECS:
        validate_prompt_template(path, effective)
        return
    try:
        fields = {
            field
            for _literal, field, format_spec, conversion in Formatter().parse(effective)
            if field is not None
        }
    except Exception as exc:
        raise ValueError(f"提示词模板语法错误：{exc}") from exc
    unknown = sorted(fields - set(spec.variables))
    if unknown:
        raise ValueError(f"未知占位符：{', '.join(unknown)}")


def preview_prompt_setting(path: str, template: str, variables: dict[str, Any] | None = None) -> str:
    validate_prompt_setting(path, template)
    spec = get_spec(path)
    if spec is None:
        raise ValueError("未知提示词设置")
    if path in PROMPT_SPECS:
        return render_plan_prompt(path, template, variables)
    samples = {
        "existing": "现有摘要：已确认目标和约束。",
        "history": "用户：请继续执行。\n助手：正在读取相关文件。",
    }
    samples.update({key: str(value) for key, value in (variables or {}).items() if key in spec.variables})
    return (str(template or "").strip() or spec.default_value).format_map(
        {name: samples.get(name, "") for name in spec.variables}
    )
