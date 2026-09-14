"""Restore an explicitly resumed Controller root's inputs, not its whole history.

This lifecycle read does not grant authority to notifications or commit a window.
The caller still budgets and atomically saves the normal request selection.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.agent.native_continuation import deserialize_messages
from app.context.store import ContextOwner, WindowStore
from app.context.window import (
    ActiveContextUnavailable,
    mark_source,
    neutral_context,
    protocol_groups,
    sliding_required_context_groups,
    source_of,
)
from app.db.dao import MessageDAO
from app.llm.base import Message
from app.rath.controller_projection import project_history_message_for_controller


async def restore_controller_run_inputs(
    messages: MessageDAO, chat_id: int, history: list[Message], *,
    session_uuid: str, run_root_turn_uuid: str, reference_store: Any = None,
) -> list[Message]:
    """Rehydrate original inputs/decisions and their immediate complete antecedents.

    Archived semantic originals take precedence over public transcript text (which
    can omit media and runtime-added input context). Unarchived user rows need an
    actual user-message operation; role=user alone could be a background receipt.
    Missing trusted inputs fail even when the next request is below the threshold.
    """
    if not run_root_turn_uuid:
        raise ActiveContextUnavailable("active_execution_input_missing; original_context_preserved")
    store = WindowStore(messages._db, ContextOwner.controller(chat_id=chat_id, session_uuid=session_uuid))

    async def rows(where: str, params: tuple) -> list[dict]:
        cur = await messages.connection.execute(
            """SELECT m.*, e.kind AS context_kind, e.event_id AS context_event_id,
                      EXISTS (SELECT 1 FROM web_operation_messages link JOIN web_operations op
                        ON op.conversation_uuid=link.conversation_uuid AND op.op_id=link.op_id
                        WHERE link.message_id=m.id AND op.internal_chat_id=m.chat_id
                          AND op.op_type='user_message') AS linked_input
               FROM messages m LEFT JOIN context_execution_events e
                 ON e.owner_key=? AND e.event_id='message:' || m.id
               WHERE m.chat_id=? AND """ + where + " ORDER BY m.id",
            (store.owner.key, chat_id, *params),
        )
        return [dict(row) for row in await cur.fetchall()]

    def kind(row: dict) -> str:
        if row["context_kind"]:
            return str(row["context_kind"])
        if row["role"] == "user":
            return "human" if row["linked_input"] else "notification"
        return "execution"

    root_rows = await rows("COALESCE(NULLIF(m.run_root_turn_uuid,''),m.turn_uuid)=?", (run_root_turn_uuid,))
    if not any(kind(row) == "human" and row["turn_uuid"] == run_root_turn_uuid for row in root_rows):
        raise ActiveContextUnavailable("active_execution_input_missing; original_context_preserved")
    candidates = {row["id"]: row for row in root_rows}
    for row in root_rows:
        if kind(row) not in {"human", "task", "control", "decision"}:
            continue
        before = row["id"]
        while True:
            preceding = await rows(
                "m.id<? AND m.id>=(SELECT MAX(id) FROM messages WHERE chat_id=? AND id<? AND role IN ('user','assistant'))",
                (before, chat_id, before),
            )
            if not preceding:
                break
            first = preceding[0]
            if kind(first) == "notification":
                before = first["id"]
                continue
            if first["role"] == "assistant" and first["content"]:
                candidates.update((item["id"], item) for item in preceding)
            break

    ordered = sorted(candidates.values(), key=lambda row: row["id"])
    originals = []
    for row in ordered:
        item = messages._row(row)
        original = project_history_message_for_controller(item.to_message())
        mark_source(original, kind=kind(row), source_id=f"message:{item.id}", message_id=item.id,
                    turn_uuid=item.turn_uuid, run_root_turn_uuid=item.run_root_turn_uuid or item.turn_uuid)
        originals.append(original)
    metadata = {source_of(m)["message_id"]: source_of(m) for m in originals}
    bounds = protocol_groups(originals)
    required = sliding_required_context_groups(originals, bounds, active_run_root_turn_uuid=run_root_turn_uuid)
    selected_rows = [ordered[i] for group in sorted(required) for i in range(*bounds[group])]
    bundles = await reference_store.bundle_ids_for_rows([messages._row(row) for row in selected_rows]) if reference_store else {}
    restored = []
    for row in selected_rows:
        meta = metadata[row["id"]]
        event_id = row["context_event_id"]
        if event_id:
            original = deserialize_messages([(await store.event_payload(event_id))["payload"]])[0]
        else:
            original = project_history_message_for_controller(messages._row(row).to_message())
        mark_source(original, **{k: v for k, v in meta.items() if k != "id"}, source_id=meta["id"])
        if bundles.get(row["id"]) and not original.get("openbear_reference_bundle"):
            original["openbear_reference_bundle"] = bundles[row["id"]]
            if event_id:
                # A legacy original without bundle metadata is immutable too.
                # Bind its frozen material in a traceable view, not by changing
                # the archived event's payload/fingerprint.
                digest = hashlib.sha256(json.dumps(bundles[row["id"]]).encode()).hexdigest()
                mark_source(original, kind=kind(row), source_id=f"{event_id}/references:{digest}", derived_from=event_id)
        restored.append(original)

    by_id = {source_of(m).get("id"): m for m in history}
    if all(source_of(m)["id"] in by_id and m.get("openbear_reference_bundle") ==
           by_id[source_of(m)["id"]].get("openbear_reference_bundle") for m in restored):
        return history
    # Insert only required complete groups at their original transcript position.
    # Unrelated retained history and runtime snapshots keep their relative order.
    restored_ids = {int(source_of(m).get("message_id") or 0) for m in restored}
    groups = [history[a:b] for a, b in protocol_groups(history)
              if not any(source_of(m).get("kind") != "summary" and
                         int(source_of(m).get("message_id") or 0) in restored_ids for m in history[a:b])]
    for a, b in protocol_groups(restored):
        group = restored[a:b]
        first_id = int(source_of(group[0])["message_id"])
        position = next((i for i, kept in enumerate(groups)
                         if int(source_of(kept[0]).get("message_id") or 0) > first_id), len(groups))
        groups.insert(position, group)
    # An inserted prefix is not compatible with any old signed/native chain.
    return neutral_context([m for group in groups for m in group])
