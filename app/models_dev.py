"""models.dev 公共模型目录的本地缓存与安全元数据投影。

目录仅提供公共事实的候选基线。它绝不提供渠道 URL、认证或路由配置；唯一的
受控例外是 models.dev 明确发布的 Fast mode ``provider.body/headers``：只有用户
从渠道页确认同步后，OpenBear 才会将其作为该模型 Fast 模式的请求附加项写入配置。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from app.logging import get_logger

log = get_logger("models_dev")

CATALOG_URL = "https://models.dev/catalog.json"
DEFAULT_REFRESH_INTERVAL_S = 24 * 60 * 60
DEFAULT_OVERRIDES_PATH = Path(__file__).with_name("models_dev_overrides.json")
_MAX_CATALOG_BYTES = 16 * 1024 * 1024
_USER_AGENT = "OpenBear models.dev catalog cache"

_RATE_KEY_MAP = {
    "input": "input",
    "output": "output",
    "cache_read": "cacheRead",
    "cache_write": "cacheWrite",
}
_SYNC_FIELD_LABELS = {
    "name": "显示名称",
    "reasoning": "推理能力",
    "reasoningOptions": "推理选项",
    "thinkingLevels": "可选思考档位",
    "input": "输入模态",
    "contextWindow": "上下文窗口",
    "maxTokens": "最大输出",
    "compactTriggerTokens": "压缩触发 Token",
    "cost": "费率与上下文阶梯价",
    "supportsFast": "Fast 模式",
    "fastCost": "Fast 模式费率",
    "fastRequest": "Fast 模式请求配置",
}


def catalog_cache_dir(config_file: Path | str) -> Path:
    """Return a cache path adjacent to the active OpenBear configuration.

    The directory deliberately does not live in ``openbear.json``: the catalog is
    an externally fetched cache, not a user-authored configuration value.
    """
    return Path(config_file).expanduser().resolve().parent / "data" / "models-dev"


def _json_clone(value: Any) -> Any:
    """Make a JSON-only defensive copy of externally sourced data."""
    return json.loads(json.dumps(value, ensure_ascii=False))


def _json_overlay(base: Any, overlay: Any) -> Any:
    """Recursively overlay one JSON value; arrays and scalars replace as a unit."""
    if not isinstance(base, Mapping) or not isinstance(overlay, Mapping):
        return _json_clone(overlay)
    out = _json_clone(dict(base))
    for key, value in overlay.items():
        out[str(key)] = _json_overlay(out.get(str(key)), value)
    return out


def _load_local_overrides(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Load versioned, source-controlled corrections without touching catalog.json."""
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        raise ValueError("models_dev_overrides_invalid_version")
    patches = raw.get("patches")
    if not isinstance(patches, Mapping):
        raise ValueError("models_dev_overrides_invalid_patches")

    loaded: dict[tuple[str, str], dict[str, Any]] = {}
    for source_key, patch in patches.items():
        provider_id, separator, model_id = str(source_key or "").strip().partition("/")
        if not provider_id or not separator or not model_id or not isinstance(patch, Mapping):
            raise ValueError(f"models_dev_override_invalid_source:{source_key}")
        merge = patch.get("merge")
        if not isinstance(merge, Mapping):
            raise ValueError(f"models_dev_override_invalid_merge:{source_key}")
        loaded[(provider_id, model_id)] = _json_clone(dict(patch))
    return loaded


def models_dev_metadata_fingerprint(metadata: Mapping[str, Any]) -> str:
    """Return a stable digest of the public fields OpenBear can actually sync.

    A catalog-wide ETag/SHA changes for unrelated providers too.  The source
    binding therefore records this field-level fingerprint so the channel page
    only flags a model when its own effective public metadata changed.
    """
    canonical = json.dumps(
        _json_clone(dict(metadata)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


def _nonnegative_number(value: Any) -> float | None:
    # bool is a number subclass but never a meaningful price.
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _project_cost(raw_cost: Any) -> dict[str, Any]:
    """Translate one schema-valid models.dev cost table into OpenBear's shape.

    OpenCode requires input/output on the base table and every tier.  Its cost()
    conversion normalizes optional cache_read/cache_write to zero; storing those
    zeroes explicitly prevents OpenBear's generic tier fallback from incorrectly
    reusing the normal/base cache rate.
    """
    if not isinstance(raw_cost, Mapping):
        return {}
    input_rate = _nonnegative_number(raw_cost.get("input"))
    output_rate = _nonnegative_number(raw_cost.get("output"))
    if input_rate is None or output_rate is None:
        return {}
    projected_cost: dict[str, Any] = {
        "input": input_rate,
        "output": output_rate,
        "cacheRead": _nonnegative_number(raw_cost.get("cache_read")) or 0.0,
        "cacheWrite": _nonnegative_number(raw_cost.get("cache_write")) or 0.0,
    }
    tiers: list[dict[str, Any]] = []
    raw_tiers = raw_cost.get("tiers")
    if isinstance(raw_tiers, list):
        for raw_tier in raw_tiers:
            if not isinstance(raw_tier, Mapping):
                continue
            tier_rule = raw_tier.get("tier")
            if not isinstance(tier_rule, Mapping) or str(tier_rule.get("type") or "context") != "context":
                continue
            threshold = _positive_int(tier_rule.get("size"))
            tier_input = _nonnegative_number(raw_tier.get("input"))
            tier_output = _nonnegative_number(raw_tier.get("output"))
            if not threshold or tier_input is None or tier_output is None:
                continue
            tiers.append({
                "contextTokens": threshold,
                "input": tier_input,
                "output": tier_output,
                "cacheRead": _nonnegative_number(raw_tier.get("cache_read")) or 0.0,
                "cacheWrite": _nonnegative_number(raw_tier.get("cache_write")) or 0.0,
            })
    if tiers:
        projected_cost["tiers"] = sorted(tiers, key=lambda item: int(item["contextTokens"]))
    return projected_cost


def _merge_fast_cost(base_cost: Mapping[str, Any], fast_overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Build Fast's effective price table from normal cost plus Fast overrides.

    OpenCode creates a Fast model variant from the normal model and overlays the
    mode's published cost.  OpenBear stores that resulting effective table so
    every existing cost consumer can select tiers normally.  Tiers merge by
    context threshold; base tiers at thresholds absent from Fast are retained.
    """
    result = _json_clone(dict(base_cost)) if base_cost else {}
    for key in _RATE_KEY_MAP.values():
        if key in fast_overrides:
            result[key] = fast_overrides[key]

    base_tiers = result.get("tiers")
    tiers_by_threshold: dict[int, dict[str, Any]] = {}
    if isinstance(base_tiers, list):
        for tier in base_tiers:
            if not isinstance(tier, Mapping):
                continue
            threshold = _positive_int(tier.get("contextTokens"))
            if threshold:
                tiers_by_threshold[threshold] = _json_clone(dict(tier))

    fast_tiers = fast_overrides.get("tiers")
    if isinstance(fast_tiers, list):
        for fast_tier in fast_tiers:
            if not isinstance(fast_tier, Mapping):
                continue
            threshold = _positive_int(fast_tier.get("contextTokens"))
            if not threshold:
                continue
            merged_tier = dict(tiers_by_threshold.get(threshold) or {"contextTokens": threshold})
            for key in _RATE_KEY_MAP.values():
                if key in fast_tier:
                    merged_tier[key] = fast_tier[key]
            if len(merged_tier) > 1:
                tiers_by_threshold[threshold] = merged_tier

    if tiers_by_threshold:
        result["tiers"] = [tiers_by_threshold[key] for key in sorted(tiers_by_threshold)]
    elif "tiers" in result:
        result.pop("tiers", None)
    return result


def _project_fast_request(fast_mode: Mapping[str, Any]) -> dict[str, Any]:
    """Project the models.dev Fast provider additions into validated local shape."""
    provider = fast_mode.get("provider")
    body = provider.get("body") if isinstance(provider, Mapping) else None
    headers = provider.get("headers") if isinstance(provider, Mapping) else None
    projected_body = _json_clone(dict(body)) if isinstance(body, Mapping) else {}
    projected_headers = {
        str(name): value
        for name, value in headers.items()
        if isinstance(name, str) and isinstance(value, str)
    } if isinstance(headers, Mapping) else {}
    # Keep an explicit empty object: an empty published Fast mode is distinct
    # from a legacy local ``supportsFast`` flag with no source request config.
    return {"body": projected_body, "headers": projected_headers}


def _catalog_shape(data: Any) -> tuple[dict[str, Any], int, int]:
    """Validate only the stable, consumed catalog shape.

    models.dev may add fields at any time.  We retain those fields untouched but
    reject a candidate snapshot when the provider/model index that OpenBear relies
    on is not structurally usable, preserving the previous last-known-good cache.
    """
    if not isinstance(data, dict):
        raise ValueError("catalog_root_not_object")
    providers = data.get("providers")
    models = data.get("models")
    if not isinstance(providers, dict) or not isinstance(models, dict):
        raise ValueError("catalog_missing_provider_or_model_index")
    if not providers:
        raise ValueError("catalog_provider_index_empty")

    provider_count = 0
    model_count = 0
    for provider_id, provider in providers.items():
        if not _clean_text(provider_id) or not isinstance(provider, dict):
            raise ValueError("catalog_provider_invalid")
        provider_models = provider.get("models")
        if not isinstance(provider_models, dict):
            raise ValueError("catalog_provider_models_invalid")
        provider_count += 1
        for model_id, model in provider_models.items():
            if not _clean_text(model_id) or not isinstance(model, dict):
                raise ValueError("catalog_model_invalid")
            model_count += 1
    if model_count <= 0:
        raise ValueError("catalog_model_index_empty")
    return data, provider_count, model_count


class ModelsDevCatalog:
    """A local last-known-good cache of ``models.dev/catalog.json``.

    Runtime model calls never invoke this class's network code.  ``start`` only
    schedules refreshes; consumers use the in-memory snapshot loaded from disk.
    """

    def __init__(
        self,
        cache_dir: Path | str,
        *,
        url: str = CATALOG_URL,
        refresh_interval_s: float = DEFAULT_REFRESH_INTERVAL_S,
        client_factory: Any = None,
        overrides_path: Path | str = DEFAULT_OVERRIDES_PATH,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_path = self.cache_dir / "catalog.json"
        self.meta_path = self.cache_dir / "catalog.meta.json"
        self.url = str(url)
        self.refresh_interval_s = max(60.0, float(refresh_interval_s or DEFAULT_REFRESH_INTERVAL_S))
        self._client_factory = client_factory
        self.overrides_path = Path(overrides_path)
        self._local_overrides = _load_local_overrides(self.overrides_path)
        self._catalog: dict[str, Any] | None = None
        self._provider_count = 0
        self._model_count = 0
        # ``catalog.models`` is the canonical provider/model index maintained by
        # models.dev.  It gives the UI a data-driven default source such as
        # openai/gpt-*, anthropic/claude-* or xai/grok-*, without guessing from a
        # local model name or persisting a binding prematurely.
        self._canonical_source_provider_by_model_id: dict[str, str] = {}
        self._meta: dict[str, Any] = {}
        self._last_error = ""
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        # The periodic loop remains alive while it sleeps between refreshes, so
        # task liveness is not a valid indicator of an active HTTP refresh.
        self._refresh_task: asyncio.Task[None] | None = None
        self._refreshing = False
        self._load_cached()

    @property
    def available(self) -> bool:
        return self._catalog is not None

    def _load_cached(self) -> None:
        if not self.cache_path.is_file():
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            catalog, provider_count, model_count = _catalog_shape(raw)
            metadata: dict[str, Any] = {}
            if self.meta_path.is_file():
                candidate = json.loads(self.meta_path.read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    metadata = candidate
            digest = hashlib.sha256(self.cache_path.read_bytes()).hexdigest()
            metadata["sha256"] = digest
            metadata.setdefault("providerCount", provider_count)
            metadata.setdefault("modelCount", model_count)
            self._set_catalog(catalog, metadata, provider_count, model_count)
        except Exception as exc:
            self._last_error = f"cached_catalog_invalid:{type(exc).__name__}"
            log.warning("models.dev 本地缓存无效，等待后续刷新", 错误类型=type(exc).__name__)

    def _set_catalog(
        self,
        catalog: dict[str, Any],
        metadata: dict[str, Any],
        provider_count: int,
        model_count: int,
    ) -> None:
        canonical_sources: dict[str, set[str]] = {}
        canonical_models = catalog.get("models")
        if isinstance(canonical_models, dict):
            for canonical_key in canonical_models:
                # Canonical keys are providerId/modelId.  Split only once: a
                # models.dev model ID is allowed to contain additional slashes.
                provider_id, separator, model_id = str(canonical_key).strip().partition("/")
                if provider_id and separator and model_id:
                    canonical_sources.setdefault(model_id, set()).add(provider_id)
        self._canonical_source_provider_by_model_id = {
            model_id: next(iter(provider_ids))
            for model_id, provider_ids in canonical_sources.items()
            if len(provider_ids) == 1
        }
        self._catalog = catalog
        self._meta = dict(metadata)
        self._provider_count = provider_count
        self._model_count = model_count
        self._last_error = ""

    @staticmethod
    def _write_json_atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_meta_best_effort(self) -> None:
        try:
            self._write_json_atomic(self.meta_path, self._meta)
        except Exception as exc:
            log.warning("models.dev 缓存元数据写入失败", 错误类型=type(exc).__name__)

    def status(self) -> dict[str, Any]:
        """A safe status payload suitable for the authenticated Web console."""
        return {
            "available": self.available,
            "refreshing": bool(self._refreshing),
            "url": self.url,
            "etag": str(self._meta.get("etag") or ""),
            "sha256": str(self._meta.get("sha256") or ""),
            "fetchedAt": int(self._meta.get("fetchedAt") or 0),
            "checkedAt": int(self._meta.get("checkedAt") or 0),
            "providerCount": self._provider_count,
            "modelCount": self._model_count,
            "localOverrideCount": len(self._local_overrides),
            "lastError": self._last_error or str(self._meta.get("lastError") or ""),
        }

    def get_model(self, provider_id: str, model_id: str) -> dict[str, Any] | None:
        """Return a defensive copy for one explicit provider/model source pair."""
        if self._catalog is None:
            return None
        provider = self._catalog.get("providers", {}).get(str(provider_id or ""))
        if not isinstance(provider, dict):
            return None
        models = provider.get("models")
        if not isinstance(models, dict):
            return None
        normalized_provider_id = str(provider_id or "")
        normalized_model_id = str(model_id or "")
        model = models.get(normalized_model_id)
        if not isinstance(model, dict):
            return None
        effective = _json_clone(model)
        patch = self._local_overrides.get((normalized_provider_id, normalized_model_id))
        if patch is not None:
            effective = _json_overlay(effective, patch["merge"])
        return effective

    def default_provider_for_model(self, model_id: str) -> str:
        """Return models.dev's canonical provider for one exact model ID.

        This is only a source-picker recommendation.  A channel does not become
        bound until the user confirms its preview/sync action.
        """
        return self._canonical_source_provider_by_model_id.get(str(model_id or "").strip(), "")

    def list_providers(self, query: str = "", *, limit: int = 300) -> list[dict[str, Any]]:
        if self._catalog is None:
            return []
        needle = str(query or "").strip().casefold()
        items: list[dict[str, Any]] = []
        for provider_id, provider in self._catalog.get("providers", {}).items():
            if not isinstance(provider, dict):
                continue
            name = _clean_text(provider.get("name")) or str(provider_id)
            haystack = f"{provider_id} {name}".casefold()
            if needle and needle not in haystack:
                continue
            models = provider.get("models")
            items.append({
                "id": str(provider_id),
                "name": name,
                "modelCount": len(models) if isinstance(models, dict) else 0,
            })
        items.sort(key=lambda item: (str(item["name"]).casefold(), str(item["id"]).casefold()))
        return items[:max(1, min(int(limit or 300), 500))]

    def list_provider_models(self, provider_id: str, query: str = "", *, limit: int = 500) -> list[dict[str, Any]]:
        if self._catalog is None:
            return []
        provider = self._catalog.get("providers", {}).get(str(provider_id or ""))
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, dict):
            return []
        needle = str(query or "").strip().casefold()
        items: list[dict[str, Any]] = []
        for model_id, model in models.items():
            if not isinstance(model, dict):
                continue
            name = _clean_text(model.get("name")) or str(model_id)
            haystack = f"{model_id} {name}".casefold()
            if needle and needle not in haystack:
                continue
            limit_info = model.get("limit") if isinstance(model.get("limit"), dict) else {}
            items.append({
                "id": str(model_id),
                "name": name,
                "contextWindow": _positive_int(limit_info.get("context")),
                "maxTokens": _positive_int(limit_info.get("output")),
                "status": _clean_text(model.get("status")),
            })
        items.sort(key=lambda item: (str(item["name"]).casefold(), str(item["id"]).casefold()))
        return items[:max(1, min(int(limit or 500), 1000))]

    def list_model_sources(self, model_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return exact provider-model matches for one local/upstream model ID.

        This is deliberately an exact ID lookup, not fuzzy matching by display
        name.  A match only narrows the user's source choice; it never writes a
        binding or guesses between multiple providers.
        """
        wanted = str(model_id or "").strip()
        if not wanted or self._catalog is None:
            return []
        items: list[dict[str, Any]] = []
        providers = self._catalog.get("providers", {})
        if not isinstance(providers, dict):
            return []
        default_provider_id = self.default_provider_for_model(wanted)
        for provider_id, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            models = provider.get("models")
            model = models.get(wanted) if isinstance(models, dict) else None
            if not isinstance(model, dict):
                continue
            limit_info = model.get("limit") if isinstance(model.get("limit"), dict) else {}
            items.append({
                "providerId": str(provider_id),
                "providerName": _clean_text(provider.get("name")) or str(provider_id),
                "modelId": wanted,
                "modelName": _clean_text(model.get("name")) or wanted,
                "contextWindow": _positive_int(limit_info.get("context")),
                "maxTokens": _positive_int(limit_info.get("output")),
                "status": _clean_text(model.get("status")),
                "isDefault": str(provider_id) == default_provider_id,
            })
        items.sort(key=lambda item: (
            0 if item["isDefault"] else 1,
            str(item["providerName"]).casefold(),
            str(item["providerId"]).casefold(),
        ))
        return items[:max(1, min(int(limit or 500), 1000))]

    async def start(self) -> None:
        """Schedule an immediate refresh and periodic conditional refreshes."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._stop_event.clear()
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="models-dev-catalog-refresh")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._refresh_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self._refresh_task = None

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                # refresh() intentionally turns normal network/data failures into a
                # result payload; keep this guard for programming errors so one bad
                # iteration cannot permanently kill the automatic updater.
                log.exception("models.dev 自动刷新出现未预期错误")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.refresh_interval_s)
                return
            except TimeoutError:
                continue

    async def refresh(self) -> dict[str, Any]:
        """Conditionally fetch a new catalog without replacing a good snapshot on error."""
        async with self._lock:
            self._refreshing = True
            try:
                result = await self._refresh_locked()
            finally:
                self._refreshing = False
        # ``_refresh_locked`` builds its status while the request is active.  A
        # completed refresh response must instead describe the state the caller
        # receives, not the state from just before its ``finally`` block ran.
        result["refreshing"] = False
        return result

    async def _refresh_locked(self) -> dict[str, Any]:
        """Perform one conditional GET while ``_lock`` is held by ``refresh``."""
        now = int(time.time())
        headers = {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        etag = _clean_text(self._meta.get("etag"))
        if etag:
            headers["If-None-Match"] = etag
        try:
            timeout = httpx.Timeout(30.0, connect=10.0)
            factory = self._client_factory or (lambda: httpx.AsyncClient(timeout=timeout, follow_redirects=False))
            async with factory() as client:
                async with client.stream("GET", self.url, headers=headers) as response:
                    if response.status_code == 304:
                        self._meta["checkedAt"] = now
                        self._meta["lastError"] = ""
                        self._last_error = ""
                        self._write_meta_best_effort()
                        return {"ok": True, "status": "not_modified", **self.status()}
                    if response.status_code != 200:
                        raise RuntimeError(f"http_{response.status_code}")
                    content_type = str(response.headers.get("content-type") or "").casefold()
                    if content_type and "json" not in content_type and "text/plain" not in content_type:
                        raise RuntimeError("unexpected_content_type")
                    declared_size = _positive_int(response.headers.get("content-length"))
                    if declared_size > _MAX_CATALOG_BYTES:
                        raise RuntimeError("catalog_too_large")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_CATALOG_BYTES:
                            raise RuntimeError("catalog_too_large")
                    if not content:
                        raise RuntimeError("catalog_empty")
                    raw = json.loads(content)
                    catalog, provider_count, model_count = _catalog_shape(raw)
                    digest = hashlib.sha256(content).hexdigest()
                    metadata = {
                        "etag": _clean_text(response.headers.get("etag")),
                        "fetchedAt": now,
                        "checkedAt": now,
                        "sha256": digest,
                        "providerCount": provider_count,
                        "modelCount": model_count,
                        "lastError": "",
                    }
                    # Both writes are local, atomic file replacements.  Write the
                    # catalog first: a sidecar failure only loses freshness metadata,
                    # never the usable snapshot itself.
                    self._write_json_atomic(self.cache_path, catalog)
                    self._write_json_atomic(self.meta_path, metadata)
                    self._set_catalog(catalog, metadata, provider_count, model_count)
                    log.info("models.dev catalog 已刷新", providers=provider_count, models=model_count)
                    return {"ok": True, "status": "updated", **self.status()}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}:{str(exc)[:180]}"
            self._last_error = error
            self._meta["checkedAt"] = now
            self._meta["lastError"] = error
            self._write_meta_best_effort()
            log.warning("models.dev catalog 刷新失败，继续使用最近有效快照", 错误=error, 有缓存=self.available)
            return {"ok": False, "status": "failed", "error": error, **self.status()}


def models_dev_metadata_to_openbear(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one provider-model record onto OpenBear's supported public fields.

    A missing whole cost table remains omitted.  Within a schema-valid table,
    optional cache rates follow OpenCode and normalize to zero.
    """
    out: dict[str, Any] = {}
    name = _clean_text(record.get("name"))
    if name:
        out["name"] = name
    if isinstance(record.get("reasoning"), bool):
        out["reasoning"] = bool(record["reasoning"])

    modalities = record.get("modalities")
    if isinstance(modalities, dict) and isinstance(modalities.get("input"), list):
        inputs = [str(value).strip().lower() for value in modalities["input"] if _clean_text(value)]
        if inputs:
            out["input"] = list(dict.fromkeys(inputs))

    limits = record.get("limit")
    if isinstance(limits, dict):
        context_window = _positive_int(limits.get("context"))
        max_tokens = _positive_int(limits.get("output"))
        if context_window:
            out["contextWindow"] = context_window
        if max_tokens:
            out["maxTokens"] = max_tokens

    projected_cost = _project_cost(record.get("cost"))
    if projected_cost:
        ordered_tiers = projected_cost.get("tiers")
        if isinstance(ordered_tiers, list) and ordered_tiers:
            # In OpenBear, an explicit compact trigger is the per-model guard
            # against carrying the conversation into a higher price bracket.
            # The first context-price tier is therefore the natural trigger;
            # later tiers never matter if compaction already happens here.
            out["compactTriggerTokens"] = int(ordered_tiers[0]["contextTokens"])
        out["cost"] = projected_cost

    # Fast is an explicit provider-model mode in models.dev, not a heuristic
    # based on the local channel name.  Its absence is an authoritative "not
    # published as Fast-capable" result for this confirmed source binding; the
    # confirmation preview makes an enable/disable change visible before write.
    experimental = record.get("experimental")
    modes = experimental.get("modes") if isinstance(experimental, Mapping) else None
    fast_mode = modes.get("fast") if isinstance(modes, Mapping) else None
    if isinstance(fast_mode, Mapping):
        out["supportsFast"] = True
        # Like OpenCode's generated Fast variant, overlay the mode's normalized
        # cost and retain normal tiers whose context thresholds are not replaced.
        out["fastCost"] = _merge_fast_cost(
            projected_cost,
            _project_cost(fast_mode.get("cost")),
        )
        out["fastRequest"] = _project_fast_request(fast_mode)
    else:
        out["supportsFast"] = False
        # Explicitly clear a prior synced Fast price/request when a later public
        # record no longer publishes this mode.
        out["fastCost"] = {}
        out["fastRequest"] = None

    raw_options = record.get("reasoning_options")
    if isinstance(raw_options, list):
        out["reasoningOptions"] = _json_clone(raw_options)
        levels: list[str] = []
        for option in raw_options:
            if not isinstance(option, dict) or option.get("type") != "effort":
                continue
            values = option.get("values")
            if not isinstance(values, list):
                continue
            for value in values:
                normalized = "off" if value == "none" else _clean_text(value).lower()
                if normalized in {"off", "minimal", "low", "medium", "high", "xhigh", "max"} and normalized not in levels:
                    levels.append(normalized)
        if levels:
            out["thinkingLevels"] = levels
    return out


def models_dev_metadata_changes(current: Mapping[str, Any], proposed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a compact, JSON-safe field-by-field sync preview."""
    changes: list[dict[str, Any]] = []
    for field, label in _SYNC_FIELD_LABELS.items():
        if field not in proposed:
            continue
        before = current.get(field)
        after = proposed.get(field)
        if _json_clone(before) != _json_clone(after):
            changes.append({"field": field, "label": label, "current": _json_clone(before), "proposed": _json_clone(after)})
    return changes


__all__ = [
    "CATALOG_URL",
    "DEFAULT_REFRESH_INTERVAL_S",
    "DEFAULT_OVERRIDES_PATH",
    "ModelsDevCatalog",
    "catalog_cache_dir",
    "models_dev_metadata_changes",
    "models_dev_metadata_fingerprint",
    "models_dev_metadata_to_openbear",
]
