"""Request-only adaptation of known framework paragraphs; stored versions stay immutable."""
from __future__ import annotations

from app.context.window import WINDOW_SYSTEM_POLICY

# Strip only the exact previous framework paragraph from frozen prompts in the
# request view. Do not edit DB templates or arbitrary user-authored wording.
LEGACY_WINDOW_SYSTEM_POLICY = WINDOW_SYSTEM_POLICY.replace(
    "OpenBear's sliding window pins the current execution round's original user/task instructions, current controls and confirmation exchanges, their limited immediate assistant antecedents, and the latest runtime state. Older rounds' user messages, assistant replies, summaries and decisions are optional history retained only within the recent complete-batch budget.",
    "OpenBear manages this context by retaining original user/task instructions, decision exchanges and recent complete execution batches.",
)

REPLACEMENTS = {
    "Context compaction is automatic and its summary is lossy. A critical execution fact may be lost if it exists only in the active context and cannot be reliably recovered from a recorded source. Preserve semantic state that matters to correct continuation; do not equate every unrecorded detail with permanent loss.":
        "Context windows retain original user/task instructions, decisions and recent complete execution batches. Older recorded dialogue and tool evidence remain retrievable through History. The active runtime strategy determines whether older context is summarized or moved out of the window; no pre-compaction memory-writing step is used. Continue directly with sufficient evidence. Retrieve only a concrete missing fact, and never infer completion or permission from a window change.",
    "These survive compaction independently of the summary.":
        "These survive context-window rotation independently of the active message selection.",
    "History reads the Web-visible user/assistant transcript. Use it for exact earlier wording, specific referenced conversations, or the pre-compaction dialogue needed to continue correctly. After compaction, recover missing visible context through `scope=current` instead of asking the user to repeat it.":
        "History reads Web-visible user/assistant dialogue by default. Use it for exact earlier wording or a specific missing fact; do not repeat a read whose evidence is already retained. With source=execution it indexes and reads original tool arguments/results, including the current root task. Use stable event IDs and nextOffset to read every part of a large result.",
    "History does not recover tool results, reasoning, raw events, or hidden runtime state. A search snippet is not the full transcript. Use direct database/raw-log inspection only when the requested evidence requires those unavailable forms, not as an alternative route to the same visible dialogue.":
        "History does not expose hidden reasoning or opaque provider state. An index or search snippet is not full evidence: fetch the necessary original event pages. Current-task lookup and excluding that same task are contradictory and must not be combined.",
    "TaskMemory stores working state independently of context compaction.":
        "TaskMemory stores working state independently of context-window rotation.",
    "Compaction is lossy. Unlike the main controller, you do not have History to recover its visible dialogue. Files and services may be reread, but critical execution semantics existing only in the active context may not be reliably recoverable after compaction or interruption.":
        "Your context window preserves task instructions, accepted controls and recent complete execution batches. AgentHistory can retrieve your own instance's recorded task and tool originals across explicitly continued rounds; it cannot read the parent conversation or another Agent. The active runtime strategy determines whether older context is summarized or moved out of the window; no forced memory-writing checkpoint is used. Retrieve a specific missing fact only when the retained evidence is insufficient.",
}


def migrate_context_prompt(text: str) -> str:
    for old, new in REPLACEMENTS.items():
        text = text.replace(old, new)
    # Normalize the short-lived window-only wording in frozen snapshots too.
    text = text.replace("no summary model or pre-rotation memory-writing step is used", "the active runtime strategy determines context reduction; no pre-compaction memory-writing step is used")
    text = text.replace("No summary model or forced memory-writing checkpoint is used.", "The active runtime strategy determines context reduction; no forced memory-writing checkpoint is used.")
    text = text.replace("Model summaries are fallible context; no forced memory-writing checkpoint is used.", "The active runtime strategy determines context reduction; no forced memory-writing checkpoint is used.")
    return text


SUMMARY_SYSTEM_POLICY = """## Context summary runtime
OpenBear manages this context using model-generated summaries and retained recent complete execution batches. At the configured threshold the framework generates a summary; no pre-compaction memory-writing checkpoint is used. These runtime facts replace older descriptions of context-window rotation in this system prompt, without changing user instructions or authorization.
A summary is fallible context, not authority. Continue directly when evidence is sufficient. For a specific missing or conflicting fact use History's original dialogue/execution records (Agent: AgentHistory for your own instance). Do not ask the user to repeat established requirements or repeat an effect whose outcome is uncertain. TaskMemory remains independent; do not bulk-write memory merely because context was compressed.
"""


def effective_context_prompt(text: str, strategy: str = "sliding_window") -> str:
    """Adapt a frozen/custom snapshot in memory without changing its stored body.

    Unknown custom text stays intact. The versioned runtime policy states actual
    mechanics only; whole-snapshot refresh still uses the existing diff/hash UI.
    """
    text = text.replace(LEGACY_WINDOW_SYSTEM_POLICY.strip(), "")
    text = text.replace(WINDOW_SYSTEM_POLICY.strip(), "").replace(SUMMARY_SYSTEM_POLICY.strip(), "")
    text = migrate_context_prompt(text)
    known_strategy_phrases = {
        "Context windows retain original user/task instructions, decisions and recent complete execution batches.":
            "Sliding windows pin current-execution instructions and feedback; older rounds are optional recent history.",
        "Your context window preserves task instructions, accepted controls and recent complete execution batches.":
            "Your sliding window pins this task's instructions and controls, not all prior Agent rounds' inputs.",
    }
    for general, sliding in known_strategy_phrases.items():
        text = text.replace(sliding, general) if strategy == "model_summary" else text.replace(general, sliding)
    policy = SUMMARY_SYSTEM_POLICY if strategy == "model_summary" else WINDOW_SYSTEM_POLICY
    return text.rstrip() + "\n\n" + policy
