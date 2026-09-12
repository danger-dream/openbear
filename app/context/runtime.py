"""Shared window lifecycle, separate from Controller/Agent execution policy."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.native_continuation import (
    serialize_messages,
    validate_model_context,
)
from app.context.configuration import normalize_strategy
from app.context.prompts import effective_context_prompt
from app.context.store import RequestTicket, StaleWindow, WindowStore
from app.context.strategies import (
    CompressionRequest,
    ContextCompressionError,
    ContextStrategy,
    SlidingWindowStrategy,
)
from app.context.window import (
    InputEstimate,
    RequiredContextTooLarge,
    WindowPolicy,
    estimate_request,
    mark_source,
    neutral_context,
    sanitize_window_checkpoint,
    source_of,
)
from app.llm.base import Message
from app.llm.events import Usage


class ContextManager:
    def __init__(
        self, store: WindowStore, policy: WindowPolicy, *, backend: Any,
        model: str, on_rotated: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        strategy_resolver: Callable[[], Awaitable[str]] | None = None,
        strategies: dict[str, ContextStrategy] | None = None,
        on_state: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        active_run_root_turn_uuid: str = "",
        model_label: str = "",
    ) -> None:
        self.store = store
        self.policy = policy
        self.backend = backend
        self.model = model
        self.model_label = model_label
        self.request_options: dict[str, Any] = {}
        self.replay_identity = ""
        self.on_rotated = on_rotated
        self.ticket: RequestTicket | None = None
        self.route = ""
        self.window_version = 0
        self.state_revision = 0
        self.last_estimate: InputEstimate | None = None
        self.strategy_resolver = strategy_resolver
        self.strategies = {"sliding_window": SlidingWindowStrategy(), **(strategies or {})}
        self.active_strategy = "sliding_window"
        self.system = ""
        self.pending = False
        self._lock = asyncio.Lock()
        self.on_state = on_state
        self.active_run_root_turn_uuid = active_run_root_turn_uuid
        self._active_compression: dict[str, Any] | None = None

    async def prepare(self, messages: list[Message], **kwargs: Any) -> list[Message]:
        async with self._lock:
            # Snapshot once at this boundary. An in-flight algorithm never changes
            # because the user switches the next compression's strategy.
            if self.strategy_resolver:
                self.active_strategy = normalize_strategy(await self.strategy_resolver())
            self._active_compression = None
            try:
                return await self._prepare(messages, **kwargs)
            except BaseException as exc:
                if self._active_compression and self.on_state:
                    with contextlib.suppress(Exception):
                        await self.on_state({**self._active_compression,
                            "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                            "error": str(exc)[:500], "active": False,
                            "instructionsPreserved": not self._active_compression.get("stateCommitted", False)})
                raise
            finally:
                self._active_compression = None

    def bind_sources(self, messages: list[Message], *, origin_task_uuid: str | None = None) -> None:
        """Only call on the owning runner's constructed messages, not tool text."""
        from app.task_memory import is_task_memory_runtime_message
        for message in messages:
            if source_of(message).get("id"):
                continue
            agent_runtime = message.get("_openbear_runtime") or {}
            runtime_kind = agent_runtime.get("kind") if isinstance(agent_runtime, dict) else ""
            if source_of(message).get("kind"):
                kind = str(source_of(message)["kind"])
            elif is_task_memory_runtime_message(message) or runtime_kind in {"rath_agent_plan_runtime", "agent_capabilities", "agent_completion_gate"}:
                kind = "runtime"
            elif runtime_kind == "rath_agent_context_summary":
                kind = "legacy_handoff"
            elif message.get("role") == "user":
                # Caller-created user messages in Agent are task/control inputs;
                # Controller synthetic notifications must be marked at creation.
                kind = "task" if self.store.owner.kind == "agent" else "human"
            else:
                kind = "execution"
            mark_source(message, kind=kind,
                        task_uuid=self.store.owner.task_uuid if origin_task_uuid is None else origin_task_uuid,
                        run_root_turn_uuid=self.active_run_root_turn_uuid)

    async def restore_source_scopes(self, messages: list[Message], *, origin_task_uuid: str = "") -> None:
        """Enrich provenance only, never rewrite archived semantic originals.

        Old Controller checkpoints have turn_uuid but not the same-run root.
        Resolve it from their exact transcript rows. Agent checkpoints use the
        original event's task; only truly unmarked legacy units fall back to the
        checkpoint's owning task, NOT a Continue round's new task.
        CONTEXT_META is excluded from source fingerprints by WindowStore.
        """
        owner = self.store.owner
        pending = [m for m in messages if not source_of(m).get(
            "task_uuid" if owner.kind == "agent" else "run_root_turn_uuid")]
        for offset in range(0, len(pending), 400):
            chunk = pending[offset:offset + 400]
            if owner.kind == "agent":
                ids = [str(source_of(m).get("id") or "") for m in chunk]
                if not ids:
                    continue
                cur = await self.store.db.conn.execute(
                    f"SELECT event_id,task_uuid FROM context_execution_events WHERE owner_key=? AND event_id IN ({','.join('?' for _ in ids)})",
                    (owner.key, *ids),
                )
                owners = {str(row["event_id"]): str(row["task_uuid"] or "") for row in await cur.fetchall()}
                for message in chunk:
                    meta = source_of(message)
                    task = owners.get(str(meta.get("id") or "")) or origin_task_uuid
                    if task:
                        message["openbear_context_source"] = {**meta, "task_uuid": task}
            else:
                ids = [int(source_of(m).get("message_id") or 0) for m in chunk]
                if not ids:
                    continue
                cur = await self.store.db.conn.execute(
                    f"SELECT id,turn_uuid,run_root_turn_uuid FROM messages WHERE chat_id=? AND id IN ({','.join('?' for _ in ids)})",
                    (owner.chat_id, *ids),
                )
                roots = {int(row["id"]): str(row["run_root_turn_uuid"] or row["turn_uuid"] or "") for row in await cur.fetchall()}
                for message in chunk:
                    meta = source_of(message)
                    root = roots.get(int(meta.get("message_id") or 0))
                    if root:
                        message["openbear_context_source"] = {**meta, "run_root_turn_uuid": root}
        if origin_task_uuid:
            self.bind_sources(messages, origin_task_uuid=origin_task_uuid)

    def request_route(self, system: str, tools: list[dict[str, Any]]) -> str:
        payload = {"model": self.model, "protocol": str(getattr(self.backend, "protocol", "")),
                   # Identical model IDs on different providers are not the same
                   # route. Only the resulting digest is persisted or exposed.
                   "endpoint": str(getattr(self.backend, "_base", "")),
                   "maxOutputTokens": self.policy.max_output_tokens,
                   "system": system, "tools": tools, "modelLabel": self.model_label,
                   "requestOptions": self.request_options}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

    async def checkpoint(
        self, messages: list[Message], *, extra_state: dict[str, Any] | None = None,
        route: str | None = None,
    ) -> dict[str, Any]:
        self.bind_sources(messages)
        archived = await self.store.archive(messages)
        safe, dropped = sanitize_window_checkpoint(messages)
        if dropped:
            archived = await self.store.archive(safe)
        saved_state = ((await self.store.load()) or {}).get("state") or {}
        # Checkpoints do not change the request identity; keep it even when a
        # compatibility caller has not supplied the full label/options yet.
        identity = {key: saved_state[key] for key in ("requestModelLabel", "replayIdentity") if key in saved_state}
        if self.model_label:
            identity["requestModelLabel"] = self.model_label
        if self.replay_identity:
            identity["replayIdentity"] = self.replay_identity
        measurement = {}
        if saved_state.get("messages") == serialize_messages(neutral_context(safe)):
            measurement = {key: saved_state[key] for key in (
                "rawEstimatedInputTokens", "estimatedNextInputTokens", "mediaTokensUnknown"
            ) if key in saved_state}
        extra = {**measurement, **(extra_state or {}), **identity, "incompleteBatch": bool(dropped)}
        saved_route = route if route is not None else self.route
        if not saved_route:
            saved_route = str(((await self.store.load()) or {}).get("route_fingerprint") or "")
        saved = await self.store.save(
            safe, expected_revision=archived["revision"], expected_source_revision=archived["sourceRevision"],
            route=saved_route, extra_state=extra,
        )
        self.window_version = saved["windowVersion"]
        self.state_revision = saved["revision"]
        return saved

    async def _prepare(
        self, messages: list[Message], *, system: str, tools: list[dict[str, Any]],
        force: bool = False, attempt: int = 0, source: str = "",
        refresh_after_rotation: Callable[[list[Message]], Awaitable[list[Message]]] | None = None,
        expected_message_high_water: int | None = None,
        request_view: Callable[[list[Message]], list[Message]] | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> list[Message]:
        if not validate_model_context(messages):
            raise ValueError("window_prepare_requires_closed_tool_batch")
        self.bind_sources(messages)
        archived = await self.store.archive(messages)
        saved = await self.store.load()
        system = effective_context_prompt(system, self.active_strategy)
        self.system = system
        self.request_options = copy.deepcopy(request_options or {})
        route = self.request_route(system, tools)
        self.route = route
        # Metering changes (thinking/Fast/tools) invalidate calibration, not a
        # compatible signed/native chain. Only transport identity changes reset it.
        identity = {"model": self.model, "modelLabel": self.model_label,
                    "protocol": str(getattr(self.backend, "protocol", "")),
                    "endpoint": str(getattr(self.backend, "_base", "")),
                    "sessionId": self.request_options.get("session_id", "")}
        self.replay_identity = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        old_identity = ((saved or {}).get("state") or {}).get("replayIdentity")
        if saved and (old_identity != self.replay_identity if old_identity else (
            saved.get("route_fingerprint") and saved["route_fingerprint"] != route
        )):
            messages = neutral_context(messages)
        calibration = ((saved or {}).get("state") or {}).get("calibration") or {}
        calibrated = (calibration.get("route") == route
                      and calibration.get("windowVersion") == (saved or {}).get("window_version")
                      and bool(calibration.get("tokens") and calibration.get("estimate")))

        def raw_estimator(items: list[Message]) -> InputEstimate:
            return estimate_request(request_view(items) if request_view else items, system=system, tools=tools, backend=self.backend,
                                    model=self.model, max_tokens=self.policy.max_output_tokens,
                                    request_options=self.request_options)

        def estimator(items: list[Message]) -> InputEstimate:
            raw = raw_estimator(items)
            tokens = raw.tokens
            if calibrated and not raw.media_unknown:
                previous = int(calibration["estimate"])
                actual = int(calibration["tokens"])
                # Anchor growth at the real request, estimate only newly added
                # material. A provider's fixed/native overhead must not multiply
                # a later large tool result. Shrinkage remains only a soft guess.
                tokens = actual + (raw.tokens - previous) if raw.tokens >= previous else round(actual * raw.tokens / previous)
            return InputEstimate(max(0, tokens), raw.media_unknown)

        measured = estimator(messages)
        self.last_estimate = measured
        exact = int((saved or {}).get("usage_tokens") or 0) if (saved or {}).get("usage_known") and (saved or {}).get("route_fingerprint") == route else 0
        threshold = self.policy.threshold
        needed = force or self.pending or bool(threshold and max(measured.tokens, exact) >= threshold)
        selected = None
        outgoing = messages
        started = time.monotonic()
        # The input must fall below the same configured trigger before the next
        # request. There is no second, output-derived compression allowance.
        ceiling = max(1, threshold - 1) if threshold else 0
        if needed:
            self._active_compression = {
                "strategy": self.active_strategy, "scope": "agent" if self.store.owner.kind == "agent" else "root",
                "ownerKind": self.store.owner.kind, "ownerId": self.store.owner.key,
                "compactionId": f"context-compaction:{uuid.uuid4()}", "status": "running", "active": True,
                "source": source or ("overflow" if force else "model_return" if self.pending else "pre_model_request"),
                "beforeEstimateTokens": measured.tokens, "estimateOnly": True,
            }
            if self.on_state:
                await self.on_state(dict(self._active_compression))
            # Runtime snapshots remain required until the owner replaces them
            # with a fresh full state; they are not archived as human records.
            working = [dict(message) for message in messages]
            if self.active_strategy == "sliding_window":
                await self.restore_source_scopes(working)
            if refresh_after_rotation:
                working = await refresh_after_rotation(working)
                self.bind_sources(working)
            for message in working:
                if source_of(message).get("kind") == "runtime":
                    message["openbear_context_source"] = {**source_of(message), "kind": "required_runtime"}
            strategy = self.strategies.get(self.active_strategy)
            if strategy is None:
                raise ContextCompressionError(f"context_strategy_unavailable:{self.active_strategy}")
            selected = await strategy.compress(CompressionRequest(
                working, estimator, self.policy.target(attempt), ceiling,
                active_task_uuid=self.store.owner.task_uuid if self.store.owner.kind == "agent" else "",
                active_run_root_turn_uuid=self.active_run_root_turn_uuid,
            ))
            outgoing = selected.messages
            for message in outgoing:
                if source_of(message).get("kind") == "required_runtime":
                    message["openbear_context_source"] = {**source_of(message), "kind": "runtime"}
            measured = estimator(outgoing)
            self.last_estimate = measured
        if ceiling and measured.tokens > ceiling:
            raise RequiredContextTooLarge(measured.tokens, ceiling)
        rotated = bool(selected and (selected.changed or serialize_messages(outgoing) != serialize_messages(messages)))
        detail = {
            "ownerKind": self.store.owner.kind, "ownerId": self.store.owner.key,
            "source": (self._active_compression or {}).get("source", source or "pre_model_request"),
            "beforeEstimateTokens": self.last_estimate.tokens if not selected else estimator(messages).tokens,
            "afterEstimateTokens": measured.tokens, "estimateOnly": True,
            "mediaTokensUnknown": measured.media_unknown,
            "rolloverTriggerTokens": threshold,
            "strategy": self.active_strategy,
            "scope": "agent" if self.store.owner.kind == "agent" else "root",
            "compactionId": (self._active_compression or {}).get("compactionId", ""),
            "status": "completed", "active": False,
            "durationMs": int((time.monotonic() - started) * 1000),
            **(selected.detail if selected else {}),
        }
        if rotated:
            # Algorithms may create a new summary source. Archive it only after
            # budget validation; the active state is still swapped atomically.
            with_generated = await self.store.archive(outgoing)
            if (with_generated["revision"] != archived["revision"]
                    or with_generated["sourceRevision"] != archived["sourceRevision"] + with_generated["added"]):
                raise StaleWindow("context_changed_during_compression; original_context_preserved")
            archived = with_generated
        result = await self.store.save(
            outgoing, expected_revision=archived["revision"], expected_source_revision=archived["sourceRevision"],
            route=route, rotated=rotated, reason=detail["source"], detail=detail,
            expected_message_high_water=expected_message_high_water,
            extra_state={"strategy": self.active_strategy, "compressionPending": False,
                         "replayIdentity": self.replay_identity,
                         **({"requestModelLabel": self.model_label or saved["state"]["requestModelLabel"]}
                            if self.model_label or ((saved or {}).get("state") or {}).get("requestModelLabel") else {}),
                         "estimatedNextInputTokens": measured.tokens, "rawEstimatedInputTokens": raw_estimator(outgoing).tokens, "mediaTokensUnknown": measured.media_unknown},
        )
        self.window_version = result["windowVersion"]
        self.state_revision = result["revision"]
        self.pending = False
        if self._active_compression:
            self._active_compression["stateCommitted"] = True
        if rotated and self.on_rotated:
            await self.on_rotated({**detail, **result})
        elif needed and self.on_state:
            await self.on_state({**detail, **result, "status": "unavailable"})
        return outgoing

    async def begin_request(self) -> RequestTicket:
        self.ticket = await self.store.begin_request(route=self.route)
        return self.ticket

    async def observe_usage(self, usage: Usage | None, *, ticket: RequestTicket | None = None) -> bool:
        target = ticket or self.ticket
        if target is None:
            return False
        tokens = None
        if usage is not None:
            total = int(usage.input_tokens or 0) + int(usage.cache_read_tokens or 0) + int(usage.cache_write_tokens or 0)
            if total > 0:
                tokens = total
        accepted = await self.store.observe_usage(target, tokens=tokens)
        if accepted:
            self.pending = bool(self.policy.threshold and tokens is not None and tokens >= self.policy.threshold)
        return accepted


# Compatibility for callers/tests of the window-only prototype. Both runners now
# use this same manager; the algorithm is selected through the registry above.
WindowRuntime = ContextManager
