"""Suffix-only reconstruction from surviving originals, never the future window.

This is a lifecycle selection, not a change to either compression strategy.
No model is called and no source text/provenance is rewritten.
"""
from __future__ import annotations

from typing import Any

from app.agent.transcript_repair import repair_tool_pairing
from app.context.request_view import expanded_request_view
from app.context.window import (
    WindowPolicy,
    estimate_request,
    mark_source,
    neutral_context,
    select_window,
    source_of,
)
from app.db.dao import MessageDAO
from app.rath.controller_projection import project_history_message_for_controller


async def controller_restart_selection(
    messages: MessageDAO, chat_id: int, cutoff: int, *, policy: WindowPolicy,
    system: str = "", tools: list[dict[str, Any]] | None = None,
    reference_store: Any = None, conversation_uuid: str = "",
    backend: Any = None, model: str = "",
) -> list[dict[str, Any]]:
    """Retain the last surviving run's structural boundary and a budgeted tail.

    A valid summary is optional context, not a reason to skip the raw antecedent
    and its frozen reference bundle. Original rows remain available to History.
    The caller commits the selection and deletion in the same transaction.
    """
    cur = await messages.connection.execute(
        "SELECT * FROM messages WHERE chat_id=? AND id<? ORDER BY id", (chat_id, cutoff),
    )
    rows = [messages._row(row) for row in await cur.fetchall()]
    if not rows:
        return []
    if policy.threshold <= 0:
        raise ValueError("restart_context_budget_unavailable; original_context_preserved")
    bundles = await reference_store.bundle_ids_for_rows(rows) if reference_store else {}
    originals = []
    for row in rows:
        message = project_history_message_for_controller(row.to_message())
        if bundles.get(row.id):
            message["openbear_reference_bundle"] = bundles[row.id]
        mark_source(message, kind="human" if row.role == "user" else "execution",
                    source_id=f"message:{row.id}", message_id=row.id,
                    reference_only=message == row.to_message(), turn_uuid=row.turn_uuid,
                    run_root_turn_uuid=row.run_root_turn_uuid or row.turn_uuid)
        originals.append(message)
    originals = neutral_context(repair_tool_pairing(originals))
    cur = await messages.connection.execute(
        """SELECT * FROM summaries s WHERE chat_id=? AND NOT EXISTS (
             SELECT 1 FROM summaries prior WHERE prior.chat_id=s.chat_id AND prior.id<=s.id
             AND prior.up_to_message_id>=?) ORDER BY id DESC LIMIT 1""",
        (chat_id, cutoff),
    )
    summary = await cur.fetchone()
    candidates = originals
    if summary and summary["summary"]:
        candidates = [mark_source(
            {"role": "user", "content": "[Earlier context summary — fallible continuation context]\n" + summary["summary"]},
            kind="summary", source_id=f"legacy-summary:{summary['id']}",
            message_id=int(summary["up_to_message_id"]), summary_id=int(summary["id"]),
        ), *originals]
    # Scope is taken from durable execution provenance at the surviving boundary,
    # not the new user's text or a semantic guess about the earlier task.
    root = next((str(source_of(m).get("run_root_turn_uuid")) for m in reversed(originals)
                 if source_of(m).get("run_root_turn_uuid")), "")
    if not root:
        # Traceable legacy rows may predate root metadata. Keep the last input's
        # complete tail and preceding assistant, not every unmarked historical input.
        last_input = next((i for i in reversed(range(len(originals))) if originals[i].get("role") == "user"), 0)
        start = last_input
        if start and originals[start - 1].get("role") == "assistant" and not originals[start - 1].get("tool_calls"):
            start -= 1
        candidates = originals[start:]
    expanded = await reference_store.overlay(candidates, conversation_uuid=conversation_uuid) if reference_store else candidates
    # Only the selection is saved. Reference material remains an immutable bundle
    # overlay; do not persist the expanded/provider copy as original user content.
    request_view = expanded_request_view(expanded)

    def estimate(items):
        return estimate_request(request_view(items), system=system, tools=tools or [],
                                backend=backend, model=model, max_tokens=policy.max_output_tokens)

    return select_window(candidates, estimate=estimate, target=policy.target(),
                         input_ceiling=policy.threshold, active_run_root_turn_uuid=root).messages
