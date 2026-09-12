"""Shared, model-free context-window selection for Controller and Agent runners.

The selector owns no permissions, persistence, or tool execution. Callers provide
trusted provenance and a complete request estimator; a soft target is never a
promise about a provider's tokenizer. Tool batches remain indivisible.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agent.native_continuation import (
    sanitize_paired_messages,
    serialize_messages,
    validate_model_context,
)
from app.llm.base import Message
from app.utils import estimate_tokens

CONTEXT_META = "openbear_context_source"
PINNED_KINDS = frozenset({"human", "task", "control", "decision", "required_runtime", "legacy_handoff", "summary"})
_MEDIA_TYPES = frozenset({"image", "image_url", "input_image", "input_audio", "input_file", "audio"})

WINDOW_SYSTEM_POLICY = """## Context window runtime
OpenBear's sliding window pins the current execution round's original user/task instructions, current controls and confirmation exchanges, their limited immediate assistant antecedents, and the latest runtime state. Older rounds' user messages, assistant replies, summaries and decisions are optional history retained only within the recent complete-batch budget. Older records may be outside the active window but remain retrievable where recorded. No model generates a new compaction summary, and no pre-compaction memory-writing checkpoint is required. These runtime facts replace older descriptions of automatic summary compaction in this system prompt; they do not change user instructions or authorization.
Continue directly when the available evidence is sufficient. Use History's execution source (Agent: AgentHistory, limited to your own instance) only for a concrete missing fact. Do not ask for established requirements again or bulk-write memory merely because the window changed. A proposed action is not an observed task state. A timed-out wait does not prove a tool or background operation stopped: establish its actual state before repeating an effect.
"""


class UnsafeWindowBoundary(ValueError):
    """The current protocol batch is not complete; nothing may be evicted."""


class ActiveContextUnavailable(ValueError):
    """The owning runner cannot locate its current input; do not evict on a guess."""


class RequiredContextTooLarge(ValueError):
    def __init__(self, estimated_tokens: int, input_ceiling: int) -> None:
        self.estimated_tokens = estimated_tokens
        self.input_ceiling = input_ceiling
        super().__init__(
            f"required_context_too_large: required input is estimated at {estimated_tokens} "
            f"tokens, above the safe input allowance {input_ceiling}; "
            "the configured compression threshold was not raised and instructions were not truncated; "
            "context compression could not fit the required input, so execution is blocked."
        )


def source_of(message: Message) -> dict[str, Any]:
    value = message.get(CONTEXT_META)
    return value if isinstance(value, dict) else {}


def mark_source(message: Message, *, kind: str = "execution", source_id: str = "", **metadata: Any) -> Message:
    """Attach runtime-owned provenance. Never infer authority from message text."""
    old = source_of(message)
    message[CONTEXT_META] = {
        **old, **metadata,
        "id": source_id or str(old.get("id") or uuid.uuid4()),
        "kind": kind,
    }
    return message


def neutral_context(messages: list[Message]) -> list[Message]:
    """Start a fresh protocol chain without partially rewriting opaque turns."""
    result = copy.deepcopy(messages)
    for message in result:
        for key in ("native_output_items", "reasoning", "signature"):
            message.pop(key, None)
    return result


def sanitize_window_checkpoint(messages: list[Message]) -> tuple[list[Message], int]:
    """Keep interrupted-batch originals immutable; closed views are derivatives."""
    safe, dropped = sanitize_paired_messages(messages)
    originals = {source_of(message).get("id"): message for message in messages if source_of(message).get("id")}
    for message in safe:
        source = source_of(message)
        original = originals.get(source.get("id"))
        if original is None:
            continue
        old, new = serialize_messages(neutral_context([original, message]))
        old.pop(CONTEXT_META, None)
        new.pop(CONTEXT_META, None)
        if old != new:
            digest = hashlib.sha256(json.dumps(new, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            mark_source(message, kind=str(source.get("kind") or "execution"),
                        source_id=f"{source['id']}/closed:{digest}", derived_from=source["id"],
                        message_id=0, reference_only=False)
    return safe, dropped


def protocol_groups(messages: list[Message]) -> list[tuple[int, int]]:
    if not validate_model_context(messages):
        raise UnsafeWindowBoundary("unclosed_or_invalid_tool_batch")
    groups: list[tuple[int, int]] = []
    index = 0
    while index < len(messages):
        start = index
        message = messages[index]
        index += 1
        if message.get("role") == "assistant":
            index += len(message.get("tool_calls") or [])
        groups.append((start, index))
    return groups


def _text_payload(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        if isinstance(value.get("type"), str) and value["type"] in _MEDIA_TYPES:
            # Vision/audio cost is provider-dependent. Neither file bytes nor a
            # base64 string is a trustworthy token count.
            return {"type": "media_size_unknown"}, True
        out, unknown = {}, False
        for key, item in value.items():
            if key == CONTEXT_META:
                continue
            out[key], media = _text_payload(item)
            unknown |= media
        return out, unknown
    if isinstance(value, list):
        out, unknown = [], False
        for item in value:
            clean, media = _text_payload(item)
            out.append(clean)
            unknown |= media
        return out, unknown
    return value, False


@dataclass(frozen=True)
class InputEstimate:
    tokens: int
    media_unknown: bool = False


def estimate_payload(payload: Any) -> InputEstimate:
    clean, media_unknown = _text_payload(payload)
    text = json.dumps(clean, ensure_ascii=False, separators=(",", ":"), default=str)
    return InputEstimate(estimate_tokens(text), media_unknown)


def estimate_request(
    messages: list[Message], *, system: str, tools: list[dict[str, Any]],
    backend: Any = None, model: str = "", max_tokens: int = 0,
    request_options: dict[str, Any] | None = None,
) -> InputEstimate:
    """Measure the production serializer when available, not Message.content."""
    build = getattr(backend, "build_payload", None)
    if callable(build):
        options = {"max_tokens": max_tokens or 8192, "stream": False,
                   **copy.deepcopy(request_options or {})}
        options.update(model=model, system=system, tools=tools)
        payload = build(messages, **options)
    else:
        payload = {"system": system, "tools": tools, "messages": serialize_messages(messages)}
    return estimate_payload(payload)


@dataclass(frozen=True)
class WindowPolicy:
    context_window: int
    trigger_tokens: int = 0
    trigger_ratio: float = 0.7
    retain_ratio: float = 0.15
    # Request serialization/routing only; never reserve output against the
    # user-configured input compression threshold.
    max_output_tokens: int = 0

    @property
    def threshold(self) -> int:
        # Explicit configuration is authoritative. Only an unset threshold uses
        # the context-window ratio; neither path deducts output capacity or an
        # additional hidden reserve, or caps the user's value to model metadata.
        return max(0, self.trigger_tokens or int(self.context_window * self.trigger_ratio))

    def target(self, attempt: int = 0) -> int:
        threshold = self.threshold
        if not threshold:
            return 0
        return max(1, int(threshold * self.retain_ratio / (2 ** max(0, attempt))))


@dataclass(frozen=True)
class WindowSelection:
    messages: list[Message]
    kept_groups: tuple[int, ...]
    removed_groups: tuple[int, ...]
    required_groups: tuple[int, ...]
    estimate: InputEstimate
    soft_target_exceeded: bool

    @property
    def changed(self) -> bool:
        return bool(self.removed_groups)


def _decision_exchange(call: Any, group: list[Message]) -> bool:
    """Keep decisions, not every read-only control-plane inspection.

    Older successful gated calls did not return their confirmation object. Keep
    all non-query/unknown actions conservatively, including cancellations and
    feedback. Only the known query forms may be treated as ordinary evidence.
    """
    name = call.get("name") if isinstance(call, dict) else call.name
    if name == "UserInteraction":
        return True
    if name != "OpenBearControl":
        return False
    call_id = call.get("id") if isinstance(call, dict) else call.id
    for message in group[1:]:
        if message.get("tool_call_id") != call_id:
            continue
        try:
            result = json.loads(message.get("content") or "{}")
        except (ValueError, TypeError):
            result = {}
        if isinstance(result, dict) and isinstance(result.get("confirmation"), dict):
            return True
    arguments = call.get("arguments") if isinstance(call, dict) else call.arguments
    try:
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, TypeError):
        return True
    if not isinstance(arguments, dict):
        return True
    action = str(arguments.get("action") or "").strip().lower()
    if action in {"status", "models", "mcp_status", "skills_status"}:
        return False
    if action == "think":
        args = arguments.get("args") or {}
        if isinstance(args, dict) and not (args.get("level") or args.get("thinkLevel") or args.get("value")):
            return False
    return True


def required_context_groups(
    messages: list[Message], bounds: list[tuple[int, int]], *, preserve_summary: bool = True,
) -> set[int]:
    """Model-summary preservation policy. Do not relax it for sliding windows."""
    pinned = PINNED_KINDS if preserve_summary else PINNED_KINDS - {"summary"}
    required: set[int] = set()
    human: list[int] = []
    for index, (start, end) in enumerate(bounds):
        group = messages[start:end]
        kinds = {source_of(message).get("kind") for message in group}
        if kinds & pinned:
            required.add(index)
        if kinds & {"human", "task", "control"}:
            human.append(index)
        for call in group[0].get("tool_calls") or []:
            if _decision_exchange(call, group):
                required.add(index)
    # Preserve the original antecedent, not an inferred meaning of a short reply.
    for index in human:
        for previous in range(index - 1, -1, -1):
            first = messages[bounds[previous][0]]
            if source_of(first).get("kind") in {"human", "task", "control"}:
                break
            if first.get("role") == "assistant" and not first.get("tool_calls") and first.get("content"):
                required.add(previous)
    return required


def sliding_required_context_groups(
    messages: list[Message], bounds: list[tuple[int, int]], *,
    active_task_uuid: str = "", active_run_root_turn_uuid: str = "",
    latest_batches: int = 0,
) -> set[int]:
    """Current execution only, using trusted provenance rather than text/role guesses.

    Agent task identity takes precedence over the parent root: Continue creates a
    new task even within that root. Legacy unscoped execution units may inherit
    the preceding input's scope, never a later/new task's identity. If no identity
    can be resolved, a single unscoped input may anchor its execution suffix.
    Multiple unscoped inputs are ambiguous: fail instead of dropping a possibly
    current instruction or turning all historical inputs into lifetime pins.
    """
    input_kinds = {"human", "task", "control", "decision"}
    primary_kinds = {"human", "task"}
    target = active_task_uuid or active_run_root_turn_uuid
    agent = bool(active_task_uuid)
    kinds: list[set[str]] = []
    identities: list[str] = []
    inherited = ""
    for start, end in bounds:
        sources = [source_of(m) for m in messages[start:end]]
        group_kinds = {str(s.get("kind") or "") for s in sources}
        # Legacy Agent checkpoints labelled real structured controls as task
        # inputs. Their trusted runtime type identifies feedback, without text
        # parsing or rewriting an archived source.
        if any((m.get("_openbear_runtime") or {}).get("kind") == "agent_control"
               for m in messages[start:end] if isinstance(m.get("_openbear_runtime"), dict)):
            group_kinds.add("control")
        kinds.append(group_kinds)
        explicit = next((str(s.get("task_uuid") or "") if agent else
                         str(s.get("run_root_turn_uuid") or s.get("turn_uuid") or "")
                         for s in sources if (s.get("task_uuid") if agent else
                                             s.get("run_root_turn_uuid") or s.get("turn_uuid"))), "")
        if group_kinds & primary_kinds:
            inherited = explicit
        identities.append(explicit or inherited)

    anchors = [i for i, ks in enumerate(kinds) if ks & input_kinds and target and identities[i] == target]
    fallback = None
    if not anchors:
        # No authoritative match: only unscoped inputs may be adopted when the
        # caller supplies an active id. Explicitly old tasks remain old.
        candidates = [i for i, ks in enumerate(kinds) if ks & primary_kinds
                      and (not target or not identities[i])]
        if not candidates:
            candidates = [i for i, ks in enumerate(kinds) if ks & input_kinds
                          and (not target or not identities[i])]
        if candidates:
            fallback = candidates[-1]
            if not target and identities[fallback]:
                target = identities[fallback]
                anchors = [i for i, ks in enumerate(kinds) if ks & input_kinds and identities[i] == target]
                fallback = None
            else:
                if len(candidates) > 1:
                    raise ActiveContextUnavailable("ambiguous_legacy_execution_inputs; original_context_preserved")
                anchors = [i for i in range(fallback, len(bounds)) if kinds[i] & input_kinds
                           and (not target or not identities[i])]

    if target and not anchors and any(ks & input_kinds for ks in kinds):
        raise ActiveContextUnavailable("active_execution_input_missing; original_context_preserved")

    def current(index: int) -> bool:
        return bool(target and identities[index] == target) or bool(
            fallback is not None and index >= fallback and not identities[index]
        )

    required = {i for i, ks in enumerate(kinds) if "required_runtime" in ks}
    required.update(anchors)
    for i, (start, end) in enumerate(bounds):
        group = messages[start:end]
        if current(i) and any(_decision_exchange(call, group) for call in group[0].get("tool_calls") or []):
            required.add(i)
    for index in anchors:
        # Agent's complete task brief is standalone. Only current controls need
        # local antecedents; never backfill an old Agent round's entire brief.
        if agent and not kinds[index] & {"control", "decision"}:
            continue
        previous = index - 1
        while previous >= 0 and kinds[previous] <= {"runtime", "required_runtime", "notification"}:
            previous -= 1
        if previous < 0:
            continue
        first = messages[bounds[previous][0]]
        if first.get("role") == "assistant" and first.get("content") and (not agent or current(previous)):
            required.add(previous)
    batches = [i for i, (start, _end) in enumerate(bounds)
               if current(i) and messages[start].get("tool_calls")]
    if latest_batches > 0 and batches:
        # The recent-batch floor belongs to this same resolved execution scope.
        # Old tools before a long dialogue must not pin that entire old suffix.
        start = batches[-latest_batches] if len(batches) >= latest_batches else batches[0]
        required.update(i for i in range(start, len(bounds)) if current(i))
    return required


def select_window(
    messages: list[Message], *, estimate: Callable[[list[Message]], InputEstimate],
    target: int, input_ceiling: int = 0, latest_batches: int = 2,
    active_task_uuid: str = "", active_run_root_turn_uuid: str = "",
) -> WindowSelection:
    """Pin active execution inputs, then retain recent contiguous complete batches.

    Runtime state must first be refreshed by the owner. Summary compression has
    its own unchanged preservation policy, not this sliding-window policy.
    """
    clean = neutral_context(messages)
    bounds = protocol_groups(clean)
    if not bounds:
        return WindowSelection([], (), (), (), estimate([]), False)
    required = sliding_required_context_groups(
        clean, bounds, active_task_uuid=active_task_uuid,
        active_run_root_turn_uuid=active_run_root_turn_uuid, latest_batches=latest_batches,
    )
    required.add(len(bounds) - 1)

    def assemble(indices: set[int]) -> list[Message]:
        return [message for i in sorted(indices) for message in clean[bounds[i][0]:bounds[i][1]]]

    kept = set(required)
    minimum = estimate(assemble(required))
    if input_ceiling and minimum.tokens > input_ceiling:
        raise RequiredContextTooLarge(minimum.tokens, input_ceiling)
    ceiling = min(target, input_ceiling) if target and input_ceiling else target or input_ceiling
    # Per-group estimates provide a linear preliminary selection. The exact
    # selected request is then serialized as a whole, including system/tools.
    base = estimate([]).tokens
    sizes = [max(0, estimate(clean[a:b]).tokens - base) for a, b in bounds]
    rough = minimum.tokens
    added: list[int] = []
    for index in reversed(range(len(bounds))):
        if index in kept:
            continue
        if ceiling and rough + sizes[index] > ceiling:
            break
        kept.add(index)
        added.append(index)
        rough += sizes[index]
    selected = assemble(kept)
    measured = estimate(selected)
    while added and ceiling and measured.tokens > max(ceiling, minimum.tokens):
        kept.remove(added.pop())
        selected = assemble(kept)
        measured = estimate(selected)
    if input_ceiling and measured.tokens > input_ceiling:
        raise RequiredContextTooLarge(measured.tokens, input_ceiling)
    if not validate_model_context(selected):
        raise UnsafeWindowBoundary("selected_context_not_closed")
    return WindowSelection(
        selected, tuple(sorted(kept)), tuple(sorted(set(range(len(bounds))) - kept)),
        tuple(sorted(required)), measured, bool(target and measured.tokens > target),
    )
