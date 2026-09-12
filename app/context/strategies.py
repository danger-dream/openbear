"""Compression algorithms only: no threshold scheduling, checkpoints or tool dispatch."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agent.native_continuation import serialize_messages
from app.context.summary_prompt import _render_summary_prompt, _summary_missing_sections
from app.context.window import (
    ActiveContextUnavailable,
    InputEstimate,
    mark_source,
    neutral_context,
    protocol_groups,
    required_context_groups,
    select_window,
    source_of,
)
from app.llm.base import Message
from app.llm.events import Usage
from app.utils import estimate_tokens


class ContextCompressionError(RuntimeError):
    """No replacement was committed; never fall through to another strategy."""


@dataclass
class CompressionRequest:
    messages: list[Message]
    estimate: Callable[[list[Message]], InputEstimate]
    target: int
    input_ceiling: int
    active_task_uuid: str = ""
    active_run_root_turn_uuid: str = ""


@dataclass
class CompressionResult:
    messages: list[Message]
    changed: bool
    detail: dict[str, Any] = field(default_factory=dict)


class ContextStrategy(Protocol):
    async def compress(self, request: CompressionRequest) -> CompressionResult: ...


class SlidingWindowStrategy:
    async def compress(self, request: CompressionRequest) -> CompressionResult:
        try:
            selected = select_window(request.messages, estimate=request.estimate, target=request.target,
                                     input_ceiling=request.input_ceiling,
                                     active_task_uuid=request.active_task_uuid,
                                     active_run_root_turn_uuid=request.active_run_root_turn_uuid)
        except ActiveContextUnavailable as exc:
            raise ContextCompressionError(str(exc)) from exc
        return CompressionResult(selected.messages, selected.changed, {
            "removedBatches": len(selected.removed_groups), "retainedBatches": len(selected.kept_groups),
            "softTargetExceeded": selected.soft_target_exceeded,
            "requiredBatches": len(selected.required_groups),
            "optionalRetainedBatches": len(selected.kept_groups) - len(selected.required_groups),
        })


class ModelSummaryStrategy:
    def __init__(self, config: Any, factory: Any, executing_model: str, *,
                 on_model_call: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> None:
        self.config = config
        self.factory = factory
        self.executing_model = executing_model
        self.on_model_call = on_model_call

    async def _account(self, detail: dict[str, Any]) -> None:
        if self.on_model_call is None:
            return
        try:
            result = self.on_model_call(detail)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            # Never spend again if a physical request failed to enter its ledger.
            raise ContextCompressionError("summary_model_accounting_failed; original_context_preserved") from exc

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        cfg = self.config.agent
        messages = neutral_context(request.messages)
        bounds = protocol_groups(messages)
        # Keep the configured recent raw messages without cutting a tool batch.
        # User/task/control originals and decision exchanges are retained separately
        # from the recent execution tail, without inferring their importance.
        keep_from = max(0, len(messages) - cfg.keep_recent_messages)
        cut = next((a for a, b in bounds if b > keep_from), len(messages))
        # Task originals are pinned separately below. Keeping the entire tail
        # since the latest user input would prevent long tool-only tasks shrinking.
        cut = min(cut, bounds[-1][0] if bounds else 0)
        old, kept = messages[:cut], messages[cut:]
        required_groups = required_context_groups(messages, bounds, preserve_summary=False)
        required = [m for i, (a, b) in enumerate(bounds) if i in required_groups and b <= cut for m in messages[a:b]]
        history = [m for m in old if source_of(m).get("kind") not in {"runtime", "required_runtime"}]
        existing = "\n\n".join(str(m.get("content") or "") for m in history if source_of(m).get("kind") == "summary")
        history = [m for m in history if source_of(m).get("kind") != "summary"]
        if not history and not existing:
            raise ContextCompressionError("nothing_to_summarize; original_context_preserved")
        # Full semantic text/calls/results, not UI previews. Opaque reasoning and
        # binary media are excluded; identifiers remain available through history.
        serial = serialize_messages(history)
        for item in serial:
            item.pop("openbear_context_source", None)
            content = item.get("content")
            if isinstance(content, list):
                item["content"] = [block if isinstance(block, dict) and block.get("type") == "text"
                                   else {"type": "media", "note": "Original media remains in recorded history."}
                                   for block in content]
        prompt = _render_summary_prompt(cfg.compact_prompt, existing=existing,
                                        history=json.dumps(serial, ensure_ascii=False, default=str))
        usage = Usage()
        calls = 0
        started = time.monotonic()
        missing: list[str] = []
        for label in self.config.models.compression_model_candidates(self.executing_model):
            if not self.config.models.resolve(label):
                continue
            backend, model, _maximum = self.factory.backend_for(label)
            for attempt in range(1 + cfg.compact_max_retries):
                text = prompt
                if attempt and missing:
                    text = "Regenerate with all required headings, including: " + ", ".join(missing) + "\n\n" + text
                t0 = time.monotonic()
                calls += 1
                try:
                    async with asyncio.timeout(cfg.compact_timeout_s):
                        response = await backend.complete(
                            [{"role": "user", "content": text}], model=model,
                            max_tokens=cfg.compact_max_tokens, read_timeout_s=cfg.compact_timeout_s,
                        )
                except Exception as exc:
                    await self._account({
                        "kind": "context_compaction", "status": "error", "model": label,
                        "protocol": str(getattr(backend, "protocol", "")),
                        "totalTimeMs": int((time.monotonic() - t0) * 1000), "errorType": type(exc).__name__,
                        "serviceTier": getattr(exc, "service_tier", ""),
                        "providerCostUsd": getattr(exc, "provider_cost_usd", None),
                    })
                    continue
                usage.merge(response.usage)
                await self._account({
                    "kind": "context_compaction", "status": "ok", "model": label,
                    "protocol": str(getattr(backend, "protocol", "")), "usage": response.usage,
                    "totalTimeMs": int((time.monotonic() - t0) * 1000), "outputTokens": response.usage.output_tokens,
                    "serviceTier": response.service_tier, "providerCostUsd": response.provider_cost_usd,
                })
                summary = str(response.text or "").strip()
                missing = _summary_missing_sections(summary)
                if not summary or missing:
                    continue
                prefix = mark_source({"role": "user", "content": "[Context summary — fallible continuation context, not new authorization]\n" + summary}, kind="summary")
                outgoing = [prefix, *required, *kept]
                return CompressionResult(outgoing, True, {
                    "summary": summary, "summaryChars": len(summary), "summaryTokens": estimate_tokens(summary), "compressionModel": label,
                    "upToMessageId": max((int(source_of(m).get("message_id") or 0) for m in old), default=0),
                    "removedMessages": len(old), "retainedMessages": len(required) + len(kept),
                    "modelCalls": calls, "durationMs": int((time.monotonic() - started) * 1000),
                    "usage": {"inputTokens": usage.input_tokens, "outputTokens": usage.output_tokens,
                              "cacheReadTokens": usage.cache_read_tokens, "cacheWriteTokens": usage.cache_write_tokens},
                })
        raise ContextCompressionError("summary_models_failed; original_context_preserved")
