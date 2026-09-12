"""Idempotent context configuration migration; retain the summary configuration."""
from __future__ import annotations

from typing import Any

RETIRED_AGENT_CONTEXT_KEYS = frozenset({
    "memoryReminderPercent", "memory_reminder_percent", "memoryReminderPrompt", "memory_reminder_prompt",
})
CONTEXT_STRATEGIES = ("sliding_window", "model_summary")


def normalize_strategy(value: Any, default: str = "sliding_window") -> str:
    return value if isinstance(value, str) and value in CONTEXT_STRATEGIES else default


async def conversation_strategy(db: Any, conversation_uuid: str, default: str = "sliding_window", *, chat_id: int | None = None) -> str:
    if conversation_uuid or chat_id is not None:
        # Runtime-owned internal chat id remains stable when a topic/session id
        # changes. A parent session UUID is not necessarily the Web conversation UUID.
        where, value = ("internal_chat_id", chat_id) if chat_id is not None else ("conversation_uuid", conversation_uuid)
        cur = await db.conn.execute(
            f"SELECT context_strategy FROM web_conversations WHERE {where}=?", (value,),
        )
        row = await cur.fetchone()
        if row is not None:
            return normalize_strategy(row["context_strategy"])
    return normalize_strategy(default)


def migrate_context_config(raw: dict[str, Any]) -> bool:
    changed = False
    if "context_management" in raw:
        raw.setdefault("contextManagement", raw.pop("context_management"))
        changed = True
    context = raw.get("contextManagement")
    if isinstance(context, dict):
        # Compatibility with the unreleased window-only configuration. The
        # original agent.compactRatio remains the sole editable trigger ratio.
        for key in ("triggerRatio", "trigger_ratio"):
            if key in context:
                agent = raw.setdefault("agent", {})
                if isinstance(agent, dict) and "compactRatio" not in agent and "compact_ratio" not in agent:
                    agent["compactRatio"] = context[key]
                del context[key]
                changed = True
    agent = raw.get("agent")
    if isinstance(agent, dict):
        for key in RETIRED_AGENT_CONTEXT_KEYS:
            if key in agent:
                del agent[key]
                changed = True
    models = raw.get("models")
    providers = models.get("providers") if isinstance(models, dict) else None
    if not isinstance(providers, dict):
        return changed
    for provider in providers.values():
        if not isinstance(provider, dict) or not isinstance(provider.get("models"), list):
            continue
        for model in provider["models"]:
            if not isinstance(model, dict):
                continue
            for legacy in ("compactTriggerTokens", "compact_trigger_tokens"):
                if legacy not in model:
                    continue
                if "rolloverTriggerTokens" not in model and "rollover_trigger_tokens" not in model:
                    model["rolloverTriggerTokens"] = model[legacy]
                del model[legacy]
                changed = True
    return changed
