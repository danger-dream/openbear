"""渠道/模型管理纯业务 helper，不依赖 Telegram UI。"""
from __future__ import annotations

import copy
import re
import time
from typing import Any

from app.config import ModelDef, ModelsConfig, ProviderDef
from app.models.thinking import (
    configured_default_think_level,
    normalize_think_levels,
)
from app.models_dev import (
    models_dev_metadata_changes,
    models_dev_metadata_fingerprint,
    models_dev_metadata_to_openbear,
)

PROTOCOLS = ("anthropic", "chat", "responses")
_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***" + key[-2:]
    return key[:6] + "***" + key[-4:]


def validate_provider_name(name: str) -> str:
    value = (name or "").strip()
    if not _PROVIDER_NAME_RE.match(value):
        raise ValueError("渠道名称只能包含字母、数字、下划线、点和短横线，长度 1-40")
    return value


def validate_base_url(value: str) -> str:
    out = (value or "").strip().rstrip("/")
    if not (out.startswith("http://") or out.startswith("https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    return out


def validate_protocol(value: str) -> str:
    proto = (value or "").strip().lower()
    if proto not in PROTOCOLS:
        raise ValueError("协议必须是 anthropic / chat / responses")
    return proto


def validate_model_id(model_id: str) -> str:
    value = (model_id or "").strip()
    if not value:
        raise ValueError("模型 ID 不能为空")
    if "/" in value or any(ch.isspace() for ch in value):
        raise ValueError("模型 ID 不能包含空格或 /")
    return value


def parse_models_input(text: str) -> list[dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型列表不能为空")
    chunks = [p.strip() for p in re.split(r"[,;\n]+", raw) if p.strip()]
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        if ":" in chunk:
            mid, label = [p.strip() for p in chunk.split(":", 1)]
        else:
            mid, label = chunk, chunk
        mid = validate_model_id(mid)
        if mid in seen:
            raise ValueError(f"模型 ID 重复：{mid}")
        seen.add(mid)
        models.append({"id": mid, "name": label or mid})
    return models


def parse_cost_input(raw: str | dict[str, Any] | None, *, field: str = "cost") -> dict[str, Any]:
    """Normalize legacy flat prices plus arbitrary context-price tiers.

    The final Pydantic validation is shared with runtime config parsing; this
    boundary keeps CRUD errors concise and prevents the old form parser from
    silently dropping ``tiers`` during a later ordinary model edit.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        candidate: dict[str, Any] = dict(raw)
    else:
        candidate = {}
        for part in re.split(r"[,;\n]+", str(raw)):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                key, value = [p.strip() for p in part.split("=", 1)]
            elif ":" in part:
                key, value = [p.strip() for p in part.split(":", 1)]
            else:
                raise ValueError(f"费用项格式不对：{part}")
            candidate[key] = value
    unknown = set(candidate) - {"input", "output", "cacheRead", "cacheWrite", "tiers"}
    if unknown:
        raise ValueError(f"未知费用项：{sorted(unknown)[0]}")
    try:
        if field == "fastCost":
            return dict(ModelDef(id="_cost_validation", fastCost=candidate).fast_cost)
        return dict(ModelDef(id="_cost_validation", cost=candidate).cost)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def parse_fast_request_input(raw: Any) -> dict[str, Any] | None:
    """Validate the Fast request overlay through the canonical config schema."""
    if raw is None or raw == "":
        return None
    try:
        request = ModelDef(id="_fast_request_validation", fastRequest=raw).fast_request
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    return request.model_dump(mode="json") if request is not None else None


def _models_dev_source_from_payload(data: Any) -> dict[str, str] | None:
    """Accept a model-level source pair without confusing it with local IDs."""
    if data is None or data == "":
        return None
    if not isinstance(data, dict):
        raise ValueError("元数据来源必须是对象")
    provider_id = str(data.get("providerId") or data.get("provider_id") or "").strip()
    model_id = str(data.get("modelId") or data.get("model_id") or "").strip()
    if not provider_id and not model_id:
        return None
    if not provider_id or not model_id:
        raise ValueError("元数据来源必须同时填写提供者和模型 ID")
    return {"providerId": provider_id, "modelId": model_id}


def _models_dev_json(model: ModelDef, catalog: Any = None) -> dict[str, Any]:
    source = model.models_dev
    if source is None:
        return {"bound": False}
    data = source.model_dump(by_alias=True)
    data["bound"] = True
    if catalog is None:
        return data
    status = catalog.status()
    current = catalog.get_model(source.provider_id, source.model_id) if status.get("available") else None
    data["available"] = bool(current)
    data["needsSync"] = source.synced_at <= 0
    if isinstance(current, dict):
        current_fingerprint = models_dev_metadata_fingerprint(models_dev_metadata_to_openbear(current))
        # New bindings have no accepted catalog state yet.  Older bindings from
        # before metadataSha256 use the old catalog-wide digest once, then become
        # field-level after the next confirmed sync.
        data["updateAvailable"] = bool(
            not data["needsSync"]
            and (
                (source.metadata_sha256 and source.metadata_sha256 != current_fingerprint)
                or (
                    not source.metadata_sha256
                    and status.get("sha256")
                    and source.catalog_sha256
                    and status.get("sha256") != source.catalog_sha256
                )
            )
        )
        data["name"] = str(current.get("name") or source.model_id)
    else:
        data["updateAvailable"] = False
    return data


def model_to_json(model: ModelDef, *, fullname: str = "", stats: dict[str, Any] | None = None,
                  primary: str = "", compression: Any = "", models_dev_catalog: Any = None) -> dict[str, Any]:
    compression_models = _compression_list(compression)
    is_compression = fullname in compression_models
    thinking_levels = list(model.thinking_levels or [])
    return {
        "id": model.id,
        "name": model.name,
        "reasoning": bool(model.reasoning),
        "reasoningOptions": copy.deepcopy(model.reasoning_options or []),
        "input": list(model.input or []),
        "contextWindow": int(model.context_window or 0),
        "maxTokens": int(model.max_tokens or 0),
        "cost": copy.deepcopy(model.cost or {}),
        "fastCost": copy.deepcopy(model.fast_cost or {}),
        "fastRequest": model.fast_request.model_dump(mode="json") if model.fast_request is not None else None,
        "modelsDev": _models_dev_json(model, models_dev_catalog),
        "thinkingLevels": thinking_levels,
        "defaultThinkingLevel": model.default_thinking_level or (thinking_levels[-1] if thinking_levels else ""),
        "supportsFast": bool(model.supports_fast),
        "compactTriggerTokens": int(model.compact_trigger_tokens or 0),
        "fullname": fullname,
        "primary": fullname == primary,
        "compression": is_compression,
        "stats": stats or {},
    }


def compression_candidates_to_json(models: ModelsConfig) -> list[dict[str, Any]]:
    """Ordered display metadata for the configured compression fallback chain."""
    out: list[dict[str, Any]] = []
    for fullname in models.compression_models:
        provider_name, _, model_id = str(fullname or "").partition("/")
        resolved = models.resolve(fullname, include_disabled=True)
        model = resolved[1] if resolved is not None else None
        out.append({
            "fullname": fullname,
            "provider": provider_name,
            "id": model_id,
            "name": (model.name or model.id) if model is not None else model_id,
        })
    return out


def provider_to_json(name: str, provider: ProviderDef, *, stats: dict[str, Any] | None = None,
                     model_stats: dict[str, dict[str, Any]] | None = None,
                     primary: str = "", compression: Any = "", include_models: bool = True,
                     models_dev_catalog: Any = None) -> dict[str, Any]:
    model_stats = model_stats or {}
    compression_models = _compression_list(compression)
    data = {
        "name": name,
        "baseUrl": provider.base_url,
        "apiKeyMasked": mask_key(provider.api_key),
        "hasApiKey": bool(provider.api_key),
        "protocol": provider.protocol,
        "enabled": bool(provider.enabled),
        "modelsDevProviderId": provider.models_dev_provider_id,
        "modelCount": len(provider.models),
        "primary": primary.startswith(name + "/"),
        "compression": any(item.startswith(name + "/") for item in compression_models),
        "stats": stats or {},
    }
    if include_models:
        data["models"] = [
            model_to_json(
                m,
                fullname=f"{name}/{m.id}",
                stats=model_stats.get(f"{name}/{m.id}", {}),
                primary=primary,
                compression=compression,
                models_dev_catalog=models_dev_catalog,
            )
            for m in provider.models
        ]
    return data


def providers_payload(
    models: ModelsConfig,
    provider_stats: list[dict[str, Any]] | None = None,
    *,
    models_dev_catalog: Any = None,
) -> dict[str, Any]:
    stats = {str(row.get("provider") or ""): dict(row) for row in (provider_stats or [])}
    return {
        "primaryModel": models.primary,
        "compressionModels": list(models.compression_models),
        "compressionCandidates": compression_candidates_to_json(models),
        "providers": [
            provider_to_json(
                name,
                provider,
                stats=stats.get(name, {}),
                primary=models.primary,
                compression=models.compression_models,
                include_models=False,
                models_dev_catalog=models_dev_catalog,
            )
            for name, provider in models.providers.items()
        ],
    }


def channels_overview_payload(provider_stats: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = [dict(row) for row in (provider_stats or [])]
    totals = {
        key: sum(float(row.get(key) or 0) for row in rows)
        for key in (
            "runs", "calls", "ok_count", "fail_count", "retry_count",
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "cost_usd", "total_time_ms",
        )
    }
    total_time_ms = float(totals["total_time_ms"] or 0)
    totals["avg_tps"] = float(totals["output_tokens"] or 0) * 1000 / total_time_ms if total_time_ms else 0
    totals["peak_tps"] = max((float(row.get("peak_tps") or 0) for row in rows), default=0)
    return {"stats": totals}


def provider_detail_payload(models: ModelsConfig, name: str, provider_stats: list[dict[str, Any]] | None = None,
                            model_stats: list[dict[str, Any]] | None = None, *, models_dev_catalog: Any = None) -> dict[str, Any]:
    provider = models.providers.get(name)
    if provider is None:
        raise KeyError(name)
    pstats = {str(row.get("provider") or ""): dict(row) for row in (provider_stats or [])}
    mstats = {str(row.get("model") or ""): dict(row) for row in (model_stats or [])}
    return {
        "primaryModel": models.primary,
        "compressionModels": list(models.compression_models),
        "compressionCandidates": compression_candidates_to_json(models),
        "provider": provider_to_json(name, provider, stats=pstats.get(name, {}), model_stats=mstats,
                                      primary=models.primary, compression=models.compression_models, include_models=True,
                                      models_dev_catalog=models_dev_catalog),
    }


def _providers(raw: dict[str, Any]) -> dict[str, Any]:
    return raw.setdefault("models", {}).setdefault("providers", {})


def _model_rows(raw_provider: dict[str, Any]) -> list[dict[str, Any]]:
    rows = raw_provider.setdefault("models", [])
    if not isinstance(rows, list):
        raise ValueError("models 必须是数组")
    return rows


def _compression_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[,;\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        label = str(item or "").strip()
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _compression_models(raw_models: dict[str, Any]) -> list[str]:
    return _compression_list(raw_models.get("compressionModels"))



def _set_compression_models(raw_models: dict[str, Any], fullnames: list[str] | str) -> None:
    values = _compression_list(fullnames)
    raw_models["compressionModels"] = values



def _rewrite_model_prefix(value: str, old_prefix: str, new_prefix: str) -> str:
    text = str(value or "")
    return new_prefix + text[len(old_prefix):] if text.startswith(old_prefix) else text


def _rewrite_compression_prefixes(raw_models: dict[str, Any], old_prefix: str, new_prefix: str) -> None:
    values = [_rewrite_model_prefix(item, old_prefix, new_prefix) for item in _compression_models(raw_models)]
    _set_compression_models(raw_models, values)


def _replace_compression_model(raw_models: dict[str, Any], old_full: str, new_full: str) -> None:
    values = [new_full if item == old_full else item for item in _compression_models(raw_models)]
    _set_compression_models(raw_models, values)

def _normalized_model_thinking(data: dict[str, Any]) -> tuple[list[str], str]:
    levels = list(normalize_think_levels(data.get("thinkingLevels", data.get("thinking_levels", ""))))
    default_raw = data.get("defaultThinkingLevel", data.get("default_thinking_level", ""))
    default_level = configured_default_think_level(levels, str(default_raw or "")) if levels else ""
    return levels, default_level


def create_provider_mutator(data: dict[str, Any]):
    name = validate_provider_name(str(data.get("name") or ""))
    base_url = validate_base_url(str(data.get("baseUrl") or data.get("base_url") or ""))
    api_key = str(data.get("apiKey") if data.get("apiKey") is not None else data.get("api_key") or "")
    protocol = validate_protocol(str(data.get("protocol") or "chat"))
    models_dev_provider_id = str(data.get("modelsDevProviderId") or data.get("models_dev_provider_id") or "").strip()
    models_data = data.get("models")
    if isinstance(models_data, str):
        models = parse_models_input(models_data)
    elif isinstance(models_data, list):
        models = [normalize_model_payload(item) for item in models_data]
    else:
        raise ValueError("至少需要一个模型")
    if not models:
        raise ValueError("至少需要一个模型")

    def mut(raw: dict[str, Any]) -> None:
        providers = _providers(raw)
        if name in providers:
            raise ValueError(f"渠道已存在：{name}")
        provider_row = {
            "baseUrl": base_url,
            "apiKey": api_key,
            "protocol": protocol,
            "enabled": bool(data.get("enabled", True)),
            "models": models,
        }
        if models_dev_provider_id:
            provider_row["modelsDevProviderId"] = models_dev_provider_id
        providers[name] = provider_row
        raw.setdefault("models", {}).setdefault("primary", f"{name}/{models[0]['id']}")
    return mut


def update_provider_mutator(name: str, patch: dict[str, Any]):
    old_name = validate_provider_name(name)

    def mut(raw: dict[str, Any]) -> None:
        providers = _providers(raw)
        if old_name not in providers:
            raise ValueError(f"渠道不存在：{old_name}")
        provider = dict(providers[old_name])
        new_name = old_name
        if "name" in patch and str(patch.get("name") or "").strip() != old_name:
            new_name = validate_provider_name(str(patch.get("name") or ""))
            if new_name in providers:
                raise ValueError(f"渠道已存在：{new_name}")
        if "baseUrl" in patch:
            provider["baseUrl"] = validate_base_url(str(patch.get("baseUrl") or ""))
        if "apiKey" in patch and patch.get("apiKey") is not None:
            provider["apiKey"] = str(patch.get("apiKey") or "")
        if "protocol" in patch:
            provider["protocol"] = validate_protocol(str(patch.get("protocol") or ""))
        if "enabled" in patch:
            provider["enabled"] = bool(patch.get("enabled"))
        if "modelsDevProviderId" in patch or "models_dev_provider_id" in patch:
            provider["modelsDevProviderId"] = str(
                patch.get("modelsDevProviderId", patch.get("models_dev_provider_id", "")) or ""
            ).strip()
        if "models" in patch:
            models_value = patch.get("models")
            existing = _model_rows(provider)
            if isinstance(models_value, str):
                parsed = parse_models_input(models_value)
            elif isinstance(models_value, list):
                parsed = [normalize_model_payload(item) for item in models_value]
            else:
                raise ValueError("models 必须是数组或文本")
            by_id = {str(item.get("id") or ""): dict(item) for item in existing if isinstance(item, dict)}
            merged = []
            for item in parsed:
                mid = item["id"]
                kept = dict(by_id.get(mid, {}))
                kept.update({k: v for k, v in item.items() if v is not None})
                kept.setdefault("id", mid)
                kept.setdefault("name", mid)
                merged.append(kept)
            provider["models"] = merged
        if new_name != old_name:
            providers.pop(old_name)
            providers[new_name] = provider
            models_root = raw.setdefault("models", {})
            value = str(models_root.get("primary") or "")
            if value.startswith(old_name + "/"):
                models_root["primary"] = new_name + value[len(old_name):]
            _rewrite_compression_prefixes(models_root, old_name + "/", new_name + "/")
        else:
            providers[old_name] = provider
    return mut


def delete_provider_mutator(name: str):
    target = validate_provider_name(name)

    def mut(raw: dict[str, Any]) -> None:
        providers = _providers(raw)
        if target not in providers:
            raise ValueError(f"渠道不存在：{target}")
        models_root = raw.setdefault("models", {})
        prefix = target + "/"
        if str(models_root.get("primary") or "").startswith(prefix):
            raise ValueError("不能删除当前主力模型所在渠道")
        if any(item.startswith(prefix) for item in _compression_models(models_root)):
            raise ValueError("不能删除当前压缩模型所在渠道")
        providers.pop(target)
    return mut


def reorder_providers_mutator(order: list[str]):
    order = [validate_provider_name(str(x)) for x in order]

    def mut(raw: dict[str, Any]) -> None:
        providers = _providers(raw)
        ordered: dict[str, Any] = {}
        for name in order:
            if name in providers and name not in ordered:
                ordered[name] = providers[name]
        for name, value in providers.items():
            if name not in ordered:
                ordered[name] = value
        raw.setdefault("models", {})["providers"] = ordered
    return mut


def normalize_model_payload(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("模型必须是对象")
    mid = validate_model_id(str(data.get("id") or ""))
    thinking_levels, default_thinking = _normalized_model_thinking(data)
    raw_reasoning_options = data.get("reasoningOptions", data.get("reasoning_options", []))
    if raw_reasoning_options is None:
        raw_reasoning_options = []
    if not isinstance(raw_reasoning_options, list):
        raise ValueError("reasoningOptions 必须是数组")
    item = {
        "id": mid,
        "name": str(data.get("name") or mid),
        "reasoning": bool(data.get("reasoning", False)),
        "reasoningOptions": copy.deepcopy(raw_reasoning_options),
        "input": list(data.get("input") or ["text"]),
        "contextWindow": int(data.get("contextWindow") or 128000),
        "maxTokens": int(data.get("maxTokens") or 8192),
        "cost": parse_cost_input(data.get("cost") or {}),
        "thinkingLevels": thinking_levels,
        "defaultThinkingLevel": default_thinking,
        "supportsFast": bool(data.get("supportsFast", data.get("fast", False))),
        "compactTriggerTokens": int(data.get("compactTriggerTokens", data.get("compact_trigger_tokens", 0)) or 0),
    }
    if "fastCost" in data or "fast_cost" in data:
        item["fastCost"] = parse_cost_input(data.get("fastCost", data.get("fast_cost")), field="fastCost")
    if "fastRequest" in data or "fast_request" in data:
        fast_request = parse_fast_request_input(data.get("fastRequest", data.get("fast_request")))
        if fast_request is not None:
            item["fastRequest"] = fast_request
    if "modelsDev" in data:
        source = _models_dev_source_from_payload(data.get("modelsDev"))
        if source is not None:
            item["modelsDev"] = source
    if item["contextWindow"] <= 0:
        raise ValueError("contextWindow 必须大于 0")
    if item["maxTokens"] <= 0:
        raise ValueError("maxTokens 必须大于 0")
    if item["compactTriggerTokens"] < 0:
        raise ValueError("compactTriggerTokens 不能小于 0")
    return item


def create_model_mutator(provider_name: str, data: dict[str, Any]):
    provider_name = validate_provider_name(provider_name)
    model = normalize_model_payload(data)

    def mut(raw: dict[str, Any]) -> None:
        providers = _providers(raw)
        if provider_name not in providers:
            raise ValueError(f"渠道不存在：{provider_name}")
        rows = _model_rows(providers[provider_name])
        if any(str(row.get("id") or "") == model["id"] for row in rows if isinstance(row, dict)):
            raise ValueError(f"模型已存在：{model['id']}")
        rows.append(copy.deepcopy(model))
    return mut


def update_model_mutator(provider_name: str, model_id: str, patch: dict[str, Any]):
    provider_name = validate_provider_name(provider_name)
    old_model_id = validate_model_id(model_id)

    def mut(raw: dict[str, Any]) -> None:
        providers = _providers(raw)
        if provider_name not in providers:
            raise ValueError(f"渠道不存在：{provider_name}")
        rows = _model_rows(providers[provider_name])
        target = None
        for row in rows:
            if isinstance(row, dict) and str(row.get("id") or "") == old_model_id:
                target = row
                break
        if target is None:
            raise ValueError(f"模型不存在：{old_model_id}")
        new_id = old_model_id
        if "id" in patch and str(patch.get("id") or "").strip() != old_model_id:
            new_id = validate_model_id(str(patch.get("id") or ""))
            if any(isinstance(row, dict) and str(row.get("id") or "") == new_id for row in rows):
                raise ValueError(f"模型已存在：{new_id}")
            target["id"] = new_id
        if "name" in patch:
            target["name"] = str(patch.get("name") or new_id)
        if "reasoning" in patch:
            target["reasoning"] = bool(patch.get("reasoning"))
        if "reasoningOptions" in patch or "reasoning_options" in patch:
            options = patch.get("reasoningOptions", patch.get("reasoning_options", []))
            if options is None:
                options = []
            if not isinstance(options, list):
                raise ValueError("reasoningOptions 必须是数组")
            target["reasoningOptions"] = copy.deepcopy(options)
        if "modelsDev" in patch:
            source = _models_dev_source_from_payload(patch.get("modelsDev"))
            if source is None:
                target.pop("modelsDev", None)
            else:
                old_source = target.get("modelsDev") if isinstance(target.get("modelsDev"), dict) else {}
                if (
                    old_source.get("providerId") == source["providerId"]
                    and old_source.get("modelId") == source["modelId"]
                ):
                    for key in ("syncedAt", "catalogSha256", "metadataSha256"):
                        if key in old_source:
                            source[key] = old_source[key]
                target["modelsDev"] = source
        if "input" in patch:
            target["input"] = list(patch.get("input") or ["text"])
        if "contextWindow" in patch:
            value = int(patch.get("contextWindow") or 0)
            if value <= 0:
                raise ValueError("contextWindow 必须大于 0")
            target["contextWindow"] = value
        if "maxTokens" in patch:
            value = int(patch.get("maxTokens") or 0)
            if value <= 0:
                raise ValueError("maxTokens 必须大于 0")
            target["maxTokens"] = value
        if "cost" in patch:
            target["cost"] = parse_cost_input(patch.get("cost"))
        if "fastCost" in patch or "fast_cost" in patch:
            target["fastCost"] = parse_cost_input(patch.get("fastCost", patch.get("fast_cost")), field="fastCost")
        if "fastRequest" in patch or "fast_request" in patch:
            fast_request = parse_fast_request_input(patch.get("fastRequest", patch.get("fast_request")))
            if fast_request is None:
                target.pop("fastRequest", None)
            else:
                target["fastRequest"] = fast_request
        if "thinkingLevels" in patch or "thinking_levels" in patch:
            levels = list(normalize_think_levels(patch.get("thinkingLevels", patch.get("thinking_levels", ""))))
            target["thinkingLevels"] = levels
            if "defaultThinkingLevel" not in patch and "default_thinking_level" not in patch:
                target["defaultThinkingLevel"] = levels[-1] if levels else ""
        if "defaultThinkingLevel" in patch or "default_thinking_level" in patch:
            levels = list(normalize_think_levels(target.get("thinkingLevels", [])))
            default_raw = patch.get("defaultThinkingLevel", patch.get("default_thinking_level", ""))
            target["defaultThinkingLevel"] = configured_default_think_level(levels, str(default_raw or "")) if levels else ""
        if "supportsFast" in patch or "fast" in patch:
            target["supportsFast"] = bool(patch.get("supportsFast", patch.get("fast", False)))
        if "compactTriggerTokens" in patch or "compact_trigger_tokens" in patch:
            value = int(patch.get("compactTriggerTokens", patch.get("compact_trigger_tokens", 0)) or 0)
            if value < 0:
                raise ValueError("compactTriggerTokens 不能小于 0")
            target["compactTriggerTokens"] = value
        if new_id != old_model_id:
            models_root = raw.setdefault("models", {})
            old_full = f"{provider_name}/{old_model_id}"
            new_full = f"{provider_name}/{new_id}"
            if models_root.get("primary") == old_full:
                models_root["primary"] = new_full
            if old_full in _compression_models(models_root):
                _replace_compression_model(models_root, old_full, new_full)
    return mut


def delete_model_mutator(provider_name: str, model_id: str):
    provider_name = validate_provider_name(provider_name)
    model_id = validate_model_id(model_id)

    def mut(raw: dict[str, Any]) -> None:
        models_root = raw.setdefault("models", {})
        fullname = f"{provider_name}/{model_id}"
        if models_root.get("primary") == fullname:
            raise ValueError("不能删除当前主力模型")
        if fullname in _compression_models(models_root):
            raise ValueError("不能删除当前压缩模型")
        providers = _providers(raw)
        if provider_name not in providers:
            raise ValueError(f"渠道不存在：{provider_name}")
        rows = _model_rows(providers[provider_name])
        providers[provider_name]["models"] = [row for row in rows if not (isinstance(row, dict) and str(row.get("id") or "") == model_id)]
    return mut


def reorder_models_mutator(provider_name: str, order: list[str]):
    provider_name = validate_provider_name(provider_name)
    order = [validate_model_id(str(x)) for x in order]

    def mut(raw: dict[str, Any]) -> None:
        providers = _providers(raw)
        if provider_name not in providers:
            raise ValueError(f"渠道不存在：{provider_name}")
        rows = _model_rows(providers[provider_name])
        by_id = {str(row.get("id") or ""): row for row in rows if isinstance(row, dict)}
        ordered = [by_id[mid] for mid in order if mid in by_id]
        ordered.extend(row for row in rows if isinstance(row, dict) and str(row.get("id") or "") not in order)
        providers[provider_name]["models"] = ordered
    return mut


def set_primary_mutator(fullname: str):
    fullname = str(fullname or "").strip()

    def mut(raw: dict[str, Any]) -> None:
        models = ModelsConfig.model_validate(raw.get("models") or {})
        if models.resolve(fullname) is None:
            raise ValueError("主力模型不存在或所属渠道已停用")
        raw.setdefault("models", {})["primary"] = fullname
    return mut


def set_compression_mutator(fullnames: Any):
    requested = _compression_list(fullnames)

    def mut(raw: dict[str, Any]) -> None:
        if requested:
            models = ModelsConfig.model_validate(raw.get("models") or {})
            for fullname in requested:
                if models.resolve(fullname) is None:
                    raise ValueError(f"压缩模型不存在或所属渠道已停用: {fullname}")
        _set_compression_models(raw.setdefault("models", {}), requested)
    return mut


def _model_for_source_sync(models: ModelsConfig, provider_name: str, model_id: str) -> tuple[ProviderDef, ModelDef]:
    provider_key = validate_provider_name(provider_name)
    local_model_id = validate_model_id(model_id)
    provider = models.providers.get(provider_key)
    if provider is None:
        raise ValueError("渠道不存在")
    for model in provider.models:
        if model.id == local_model_id:
            return provider, model
    raise ValueError("模型不存在")


def _source_pair_for_sync(_provider: ProviderDef, model: ModelDef, source_data: Any = None) -> dict[str, str]:
    """Resolve a source only from an explicit pair or an existing explicit binding.

    A channel-level default provider narrows the picker in the UI; it is never a
    source binding.  Likewise, a local/upstream model ID must never become a
    models.dev ID merely because the strings happen to match.
    """
    if source_data is not None and not isinstance(source_data, dict):
        raise ValueError("元数据来源必须是对象")
    incoming = source_data if isinstance(source_data, dict) else {}
    if "modelsDev" in incoming:
        nested = incoming["modelsDev"]
        if not isinstance(nested, dict):
            raise ValueError("元数据来源必须是对象")
    else:
        nested = incoming

    provider_id = str(nested.get("providerId") or nested.get("provider_id") or "").strip()
    source_model_id = str(nested.get("modelId") or nested.get("model_id") or "").strip()
    if provider_id or source_model_id:
        if not provider_id or not source_model_id:
            raise ValueError("元数据来源必须同时填写提供者和模型 ID")
        return {"providerId": provider_id, "modelId": source_model_id}

    existing = model.models_dev
    if existing is not None:
        return {"providerId": existing.provider_id, "modelId": existing.model_id}
    raise ValueError("请先选择元数据提供者和模型 ID")


def _current_public_metadata(model: ModelDef) -> dict[str, Any]:
    return {
        "name": model.name,
        "reasoning": bool(model.reasoning),
        "reasoningOptions": copy.deepcopy(model.reasoning_options or []),
        "input": list(model.input or []),
        "contextWindow": int(model.context_window or 0),
        "maxTokens": int(model.max_tokens or 0),
        "compactTriggerTokens": int(model.compact_trigger_tokens or 0),
        "cost": copy.deepcopy(model.cost or {}),
        "fastCost": copy.deepcopy(model.fast_cost or {}),
        "fastRequest": model.fast_request.model_dump(mode="json") if model.fast_request is not None else None,
        "supportsFast": bool(model.supports_fast),
        "thinkingLevels": list(model.thinking_levels or []),
    }


def models_dev_sync_preview(
    models: ModelsConfig,
    provider_name: str,
    model_id: str,
    catalog: Any,
    source_data: Any = None,
) -> dict[str, Any]:
    """Build a deterministic local preview; no config or external state is changed."""
    if catalog is None or not catalog.status().get("available"):
        raise ValueError("元数据目录尚不可用；请等待自动拉取或手动刷新")
    provider, model = _model_for_source_sync(models, provider_name, model_id)
    source = _source_pair_for_sync(provider, model, source_data)
    record = catalog.get_model(source["providerId"], source["modelId"])
    if record is None:
        raise ValueError("未找到指定的元数据来源")
    metadata = models_dev_metadata_to_openbear(record)
    if not metadata:
        raise ValueError("该元数据记录没有可同步到 OpenBear 的公共字段")
    # This fingerprint describes exactly the public fields shown in the preview.
    # It intentionally ignores unrelated catalog records and fields OpenBear does
    # not sync, while still making a changed proposed value require a re-preview.
    metadata_sha256 = models_dev_metadata_fingerprint(metadata)
    current_metadata = _current_public_metadata(model)
    catalog_status = catalog.status()
    return {
        "source": {
            **source,
            "name": str(record.get("name") or source["modelId"]),
        },
        "catalog": {
            key: catalog_status.get(key)
            for key in ("etag", "sha256", "fetchedAt", "checkedAt")
        },
        "current": current_metadata,
        "metadata": metadata,
        "metadataSha256": metadata_sha256,
        "changes": models_dev_metadata_changes(current_metadata, metadata),
    }


def _models_dev_candidates_with_default(
    catalog: Any,
    model_id: str,
    raw_candidates: Any,
) -> tuple[list[dict[str, Any]], str]:
    """Normalize same-ID candidates and put the canonical source first.

    ``models.dev`` exposes a canonical ``providerId/modelId`` index separately
    from its provider offerings.  Its provider is a convenience default for the
    picker, never an implicit OpenBear binding.
    """
    candidates = [copy.deepcopy(item) for item in raw_candidates if isinstance(item, dict)] if isinstance(raw_candidates, list) else []
    candidate_provider_ids = {
        str(candidate.get("providerId") or "").strip()
        for candidate in candidates
        if str(candidate.get("providerId") or "").strip()
    }
    default_provider_id = ""
    default_lookup = getattr(catalog, "default_provider_for_model", None)
    if callable(default_lookup):
        candidate = str(default_lookup(model_id) or "").strip()
        if candidate in candidate_provider_ids:
            default_provider_id = candidate
    if not default_provider_id:
        declared_defaults = {
            str(candidate.get("providerId") or "").strip()
            for candidate in candidates
            if candidate.get("isDefault") and str(candidate.get("providerId") or "").strip()
        }
        if len(declared_defaults) == 1:
            default_provider_id = next(iter(declared_defaults))

    for candidate in candidates:
        candidate["isDefault"] = bool(default_provider_id and candidate.get("providerId") == default_provider_id)
    candidates.sort(key=lambda candidate: (
        0 if candidate.get("isDefault") else 1,
        str(candidate.get("providerName") or candidate.get("providerId") or "").casefold(),
        str(candidate.get("providerId") or "").casefold(),
    ))
    return candidates, default_provider_id


def models_dev_source_matches(
    models: ModelsConfig,
    provider_name: str,
    catalog: Any,
) -> dict[str, Any]:
    """Return exact same-ID provider choices for every model in one channel.

    Candidate discovery is read-only.  It deliberately does not pre-bind an
    ambiguous source; the caller must submit an explicit pair for every model
    it wants to synchronize.
    """
    if catalog is None or not catalog.status().get("available"):
        raise ValueError("元数据目录尚不可用；请等待自动拉取或手动刷新")
    provider_key = validate_provider_name(provider_name)
    provider = models.providers.get(provider_key)
    if provider is None:
        raise ValueError("渠道不存在")
    source_lookup = getattr(catalog, "list_model_sources", None)
    if not callable(source_lookup):
        raise ValueError("元数据目录不支持同名来源匹配")

    items: list[dict[str, Any]] = []
    for model in provider.models:
        existing = model.models_dev
        current_source = (
            {"providerId": existing.provider_id, "modelId": existing.model_id}
            if existing is not None
            else None
        )
        candidates, default_provider_id = _models_dev_candidates_with_default(
            catalog,
            model.id,
            source_lookup(model.id),
        )
        items.append({
            "modelId": model.id,
            "name": model.name,
            "currentSource": current_source,
            "defaultProviderId": default_provider_id,
            "candidates": candidates,
        })
    return {
        "provider": provider_key,
        "items": items,
        "catalog": catalog.status(),
    }


def _batch_source_entries(source_items: Any) -> list[dict[str, Any]]:
    if not isinstance(source_items, list) or not source_items:
        raise ValueError("至少选择一个元数据来源")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in source_items:
        if not isinstance(raw, dict):
            raise ValueError("批量来源项必须是对象")
        local_model_id = validate_model_id(str(raw.get("localModelId") or raw.get("local_model_id") or ""))
        if local_model_id in seen:
            raise ValueError(f"批量来源中模型重复：{local_model_id}")
        seen.add(local_model_id)
        source = _models_dev_source_from_payload(raw.get("source"))
        if source is None:
            raise ValueError(f"模型 {local_model_id} 缺少完整的元数据来源")
        entries.append({
            "localModelId": local_model_id,
            "source": source,
            "metadataSha256": str(raw.get("metadataSha256") or raw.get("metadata_sha256") or "").strip(),
        })
    return entries


def models_dev_batch_sync_preview(
    models: ModelsConfig,
    provider_name: str,
    catalog: Any,
    source_items: Any,
) -> dict[str, Any]:
    """Build all selected sync previews from one local catalog snapshot."""
    if catalog is None or not catalog.status().get("available"):
        raise ValueError("元数据目录尚不可用；请等待自动拉取或手动刷新")
    entries = _batch_source_entries(source_items)
    previews: list[dict[str, Any]] = []
    for entry in entries:
        _provider, model = _model_for_source_sync(models, provider_name, entry["localModelId"])
        source = entry["source"]
        record = catalog.get_model(source["providerId"], source["modelId"])
        if record is None:
            raise ValueError(f"未找到 {entry['localModelId']} 指定的元数据来源")
        metadata = models_dev_metadata_to_openbear(record)
        if not metadata:
            raise ValueError(f"{entry['localModelId']} 的元数据记录没有可同步字段")
        current_metadata = _current_public_metadata(model)
        previews.append({
            "localModelId": entry["localModelId"],
            "localName": model.name,
            "source": {
                **source,
                "name": str(record.get("name") or source["modelId"]),
            },
            "current": current_metadata,
            "metadata": metadata,
            "metadataSha256": models_dev_metadata_fingerprint(metadata),
            "changes": models_dev_metadata_changes(current_metadata, metadata),
        })
    catalog_status = catalog.status()
    return {
        "catalog": {key: catalog_status.get(key) for key in ("etag", "sha256", "fetchedAt", "checkedAt")},
        "items": previews,
    }


def sync_models_from_models_dev_mutator(
    provider_name: str,
    previews: list[dict[str, Any]],
    *,
    catalog_sha256: str = "",
    synced_at: int | None = None,
):
    """Atomically apply a set of already-previewed metadata projections."""
    provider_name = validate_provider_name(provider_name)
    if not previews:
        raise ValueError("没有可同步的元数据")
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    now = max(0, int(synced_at if synced_at is not None else time.time()))
    for preview in previews:
        if not isinstance(preview, dict):
            raise ValueError("批量同步预览项必须是对象")
        local_model_id = validate_model_id(str(preview.get("localModelId") or ""))
        if local_model_id in seen:
            raise ValueError(f"批量同步模型重复：{local_model_id}")
        seen.add(local_model_id)
        source = _models_dev_source_from_payload(preview.get("source"))
        if source is None:
            raise ValueError(f"模型 {local_model_id} 缺少元数据来源")
        metadata = preview.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"模型 {local_model_id} 缺少可同步元数据")
        prepared.append({
            "localModelId": local_model_id,
            "metadata": copy.deepcopy(metadata),
            "sourceState": {
                **source,
                "syncedAt": now,
                "catalogSha256": str(catalog_sha256 or ""),
                "metadataSha256": str(preview.get("metadataSha256") or models_dev_metadata_fingerprint(metadata)),
            },
        })

    def mut(raw: dict[str, Any]) -> None:
        providers = _providers(raw)
        provider = providers.get(provider_name)
        if not isinstance(provider, dict):
            raise ValueError("渠道不存在")
        rows = _model_rows(provider)
        targets = {
            str(row.get("id") or ""): row
            for row in rows
            if isinstance(row, dict)
        }
        # Resolve every target before touching one, so a bad batch cannot leave
        # a partial mutation in callers that execute this mutator directly.
        for entry in prepared:
            if entry["localModelId"] not in targets:
                raise ValueError(f"模型不存在：{entry['localModelId']}")
        for entry in prepared:
            target = targets[entry["localModelId"]]
            metadata = entry["metadata"]
            for key in ("name", "reasoning", "input", "contextWindow", "maxTokens", "compactTriggerTokens", "reasoningOptions", "supportsFast"):
                if key in metadata:
                    target[key] = copy.deepcopy(metadata[key])
            if "fastRequest" in metadata:
                fast_request = parse_fast_request_input(metadata["fastRequest"])
                if fast_request is None:
                    target.pop("fastRequest", None)
                else:
                    target["fastRequest"] = fast_request
            if "cost" in metadata:
                target["cost"] = parse_cost_input(metadata["cost"])
            if "fastCost" in metadata:
                target["fastCost"] = parse_cost_input(metadata["fastCost"], field="fastCost")
            if metadata.get("reasoning") is False:
                target["reasoningOptions"] = []
                target["thinkingLevels"] = []
                target["defaultThinkingLevel"] = ""
            elif "thinkingLevels" in metadata:
                levels = list(normalize_think_levels(metadata["thinkingLevels"]))
                target["thinkingLevels"] = levels
                prior_default = str(target.get("defaultThinkingLevel") or "")
                target["defaultThinkingLevel"] = configured_default_think_level(levels, prior_default) if levels else ""
            target["modelsDev"] = copy.deepcopy(entry["sourceState"])

    return mut


def sync_model_from_models_dev_mutator(
    provider_name: str,
    model_id: str,
    *,
    source: dict[str, str],
    metadata: dict[str, Any],
    catalog_sha256: str = "",
    metadata_sha256: str = "",
    synced_at: int | None = None,
):
    """Apply an already previewed catalog projection in one config-store mutation."""
    provider_name = validate_provider_name(provider_name)
    local_model_id = validate_model_id(model_id)
    source = _models_dev_source_from_payload(source)
    if source is None:
        raise ValueError("元数据来源不能为空")
    approved_metadata = copy.deepcopy(metadata)
    source_state = {
        **source,
        "syncedAt": max(0, int(synced_at if synced_at is not None else time.time())),
        "catalogSha256": str(catalog_sha256 or ""),
        "metadataSha256": str(metadata_sha256 or models_dev_metadata_fingerprint(approved_metadata)),
    }

    def mut(raw: dict[str, Any]) -> None:
        providers = _providers(raw)
        provider = providers.get(provider_name)
        if not isinstance(provider, dict):
            raise ValueError("渠道不存在")
        target = next(
            (
                row for row in _model_rows(provider)
                if isinstance(row, dict) and str(row.get("id") or "") == local_model_id
            ),
            None,
        )
        if target is None:
            raise ValueError("模型不存在")
        for key in ("name", "reasoning", "input", "contextWindow", "maxTokens", "compactTriggerTokens", "reasoningOptions", "supportsFast"):
            if key in approved_metadata:
                target[key] = copy.deepcopy(approved_metadata[key])
        if "fastRequest" in approved_metadata:
            fast_request = parse_fast_request_input(approved_metadata["fastRequest"])
            if fast_request is None:
                target.pop("fastRequest", None)
            else:
                target["fastRequest"] = fast_request
        if "cost" in approved_metadata:
            target["cost"] = parse_cost_input(approved_metadata["cost"])
        if "fastCost" in approved_metadata:
            target["fastCost"] = parse_cost_input(approved_metadata["fastCost"], field="fastCost")
        if approved_metadata.get("reasoning") is False:
            # A catalog explicitly declaring no reasoning must not leave stale UI
            # options that could generate unsupported effort parameters.
            target["reasoningOptions"] = []
            target["thinkingLevels"] = []
            target["defaultThinkingLevel"] = ""
        elif "thinkingLevels" in approved_metadata:
            levels = list(normalize_think_levels(approved_metadata["thinkingLevels"]))
            target["thinkingLevels"] = levels
            prior_default = str(target.get("defaultThinkingLevel") or "")
            target["defaultThinkingLevel"] = (
                configured_default_think_level(levels, prior_default) if levels else ""
            )
        target["modelsDev"] = copy.deepcopy(source_state)

    return mut


async def probe_model(llm_factory: Any, fullname: str) -> dict[str, Any]:
    """手动模型连通性测试。调用方必须由用户显式触发。"""
    started = time.monotonic()
    try:
        backend, model_id, _max_tokens = llm_factory.backend_for(fullname)
        result = await backend.complete(
            [{"role": "user", "content": "请只回答 OK"}],
            model=model_id,
            system="",
            tools=[],
            max_tokens=32,
            think_level="off",
            session_id=f"openbear-web-probe-{int(time.time())}",
        )
        elapsed = int((time.monotonic() - started) * 1000)
        snippet = (getattr(result, "text", "") or getattr(result, "reasoning", "") or "").strip().replace("\n", " ")[:200]
        ok = bool(str(getattr(result, "text", "") or "").strip())
        usage = getattr(result, "usage", None)
        return {
            "model": fullname,
            "ok": ok,
            "elapsedMs": elapsed,
            "snippet": snippet or "（空回复）",
            "error": "",
            "usage": usage,
            "serviceTier": str(getattr(result, "service_tier", "") or ""),
            "providerCostUsd": getattr(result, "provider_cost_usd", None),
            "protocol": str(getattr(backend, "protocol", "") or ""),
        }
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "model": fullname,
            "ok": False,
            "elapsedMs": elapsed,
            "snippet": "",
            "error": f"{type(exc).__name__}: {str(exc)[:600]}",
            "usage": getattr(exc, "usage", None),
            "serviceTier": str(getattr(exc, "service_tier", "") or ""),
            "providerCostUsd": getattr(exc, "provider_cost_usd", None),
            "protocol": str(getattr(locals().get("backend"), "protocol", "") or ""),
        }
