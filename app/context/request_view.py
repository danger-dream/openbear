"""Request-local expansion shared by automatic and manual compression budgets."""
from __future__ import annotations

from collections.abc import Callable

from app.agent.transcript_repair import repair_role_alternation
from app.context.window import source_of
from app.llm.base import Message


def expanded_request_view(
    expanded: list[Message], *, retry_tail: list[Message] | None = None,
) -> Callable[[list[Message]], list[Message]]:
    """Project frozen reference contents onto a selection, never its checkpoint.

    Sources must be bound before expansion. New runtime snapshots and summaries
    retain their own contents; they must not inherit another message's overlay.
    Protected materials stay in this request-local closure, not durable context.
    """
    content_by_id = {source_of(message)["id"]: message.get("content")
                     for message in expanded if source_of(message).get("id")}

    def view(selected: list[Message]) -> list[Message]:
        messages = []
        for message in selected:
            item = dict(message)
            source_id = source_of(message).get("id")
            if source_id in content_by_id:
                item["content"] = content_by_id[source_id]
            messages.append(item)
        return repair_role_alternation(messages + list(retry_tail or []))

    return view
