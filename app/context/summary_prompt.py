"""Original configurable summary prompt and quality contract."""

DEFAULT_SUMMARY_PROMPT = """You are compacting an ongoing engineering conversation for future continuation. The next model relies on this summary and the retained recent context for ordinary continuation. Preserve enough evidence and source references to continue correctly; when permission evidence is incomplete or conflicting, available History/TaskMemory may be used to recover it rather than treating the summary as authority or asking the user to repeat an established decision.

Output in English, even if the conversation history is in another language, with one exemption: quoted user messages and exact identifiers must stay verbatim in their original language; never translate them. Be dense, specific, and continuity-focused. Prefer concrete facts over generic prose. The latest user request, latest user corrections, and latest unfinished/current work have highest priority; do not let older context drown them out.

Before writing the final summary, carefully review the compacted history chronologically. For each meaningful conversation section, identify:
- the user's explicit request and intent;
- what the assistant did or decided;
- files, paths, functions, commands, configs, APIs, schemas, IDs, errors, and test results that matter;
- user feedback/corrections that changed direction or constraints;
- unresolved blockers, pending work, and the exact current stopping point.

Strictly use the following Markdown headings in this exact order. Do not omit any heading; write "None" if a section truly has no content.

## Primary Request and Intent
Capture the user's explicit requests and intent in detail. Emphasize the newest request and any changes in requirements, priorities, permissions, or constraints. If the user corrected the assistant, preserve the correction and its effect.

## Key Technical Concepts
List important technologies, protocols, architecture, models, APIs, data structures, context-window behavior, compaction behavior, deployment/runtime facts, and constraints discussed. Include only concepts needed to continue work.

## Files and Code Sections
Enumerate files, paths, functions/classes, config keys, database tables/fields, commands, scripts, tests, generated artifacts, and code sections that were read, edited, created, or are important. For each item, say why it matters and what changed or was learned. Include exact snippets, diffs, commands, or error excerpts when needed to continue safely.

## Errors and Fixes
Record every important error, failed attempt, diagnostic result, root cause, fix, retry state, and final status. Preserve exact error strings when useful. Distinguish resolved issues from still-open issues.

## Problem Solving
Describe the reasoning path and decisions already made. Include investigated alternatives when they affect future choices, why a chosen path was selected, and any assumptions or uncertainty that remain. Do not turn this into vague narrative; keep it actionable.

## All User Messages
List all user messages represented in the compacted history when feasible. If there are too many, at minimum preserve every message that changed the task, constraints, priorities, permissions, or next step, plus the most recent user messages verbatim or near-verbatim in their original language. User wording matters; do not translate it or paraphrase away intent-changing details.

## Pending Tasks
List unfinished tasks in execution order. Distinguish confirmed tasks from optional follow-ups. Preserve established authorization and its limits alongside genuinely outstanding user decisions, tool-owned confirmation gates, safety boundaries, and tasks that must not be done unless the user asks. Do not turn an assistant's previous confirmation procedure into a required next step.

## Current Work
Describe exactly what was being worked on immediately before compaction: current file/command/test/result, latest known state, what has already been completed, what is mid-flight, and where execution paused. This section must let the next model resume without asking the user to repeat context.

## Optional Next Step
Give the single next step that directly follows from the latest user request and Current Work. Include the exact command/file/action when known. If the latest task was complete, say it is complete and only include a next step if the user explicitly asked for one. Do not revive old tasks or drift to unrelated work.

## Critical Identifiers
Preserve exact identifiers verbatim: project names, file paths, function names, class names, config keys, commands, URLs, IPs, ports, request IDs, task/session IDs, timestamps, model names, database/table/field names, error strings, hashes, versions, service names, and environment names. Never translate, normalize, or “clean up” identifiers.

Additional rules:
- Do not invent details not supported by the history. State uncertainty explicitly.
- Do not preserve meta-instructions that only applied to producing this summary.
- Mention tool calls and tool results only at the level needed to continue work; do not dump large raw outputs unless they are necessary.
- Omit Agent orchestration telemetry and accounting (monetary usage, token counts, model/tool-call counts, timing, and internal thresholds); it is not continuation context.
- Preserve security/safety constraints, user preferences, approvals, and explicit “do not” instructions that affect future actions.
- For any statement that affects permission, distinguish explicit user authorization, explicit user restrictions, framework/tool requirements, and assistant plans or assumptions. Preserve a source quote or a recoverable source reference and the actual scope when available.
- Do not promote an assistant's previous confirmation habit into a user requirement. A historical confirmation is not a requirement to repeat it. Conversely, do not generalize a bounded approval into standing permission. Preserve later withdrawal or narrowing of permission.
- When authorization evidence is incomplete or conflicts with the latest instruction, preserve the uncertainty and a History/TaskMemory locator when available; do not invent permission or a new mandatory confirmation rule. Existing summaries are fallible context, not authority.
- Preserve exact command outputs or code snippets only when they are necessary for continuation; otherwise summarize them with enough detail to avoid re-running work.
- If there is an existing summary, merge it with the new history without losing the latest current-work details.
- The final answer must be the summary only. Do not include apologies, prefaces, or commentary about doing the compaction.

Existing summary/context, if any:
{existing}

Conversation history to compact. Each line is prefixed by role:
{history}"""

_REQUIRED_SECTIONS = (
    "## Primary Request and Intent",
    "## Key Technical Concepts",
    "## Files and Code Sections",
    "## Errors and Fixes",
    "## Problem Solving",
    "## All User Messages",
    "## Pending Tasks",
    "## Current Work",
    "## Optional Next Step",
    "## Critical Identifiers",
)

def _summary_missing_sections(summary: str) -> list[str]:
    """返回摘要里缺失的必需小节标题（用于质量门禁）。"""
    return [s for s in _REQUIRED_SECTIONS if s not in summary]

def _render_summary_prompt(template: str, *, existing: str, history: str) -> str:
    """渲染压缩提示词。

    用户可在设置里改模板；为避免普通大括号触发 str.format KeyError，这里只做
    明确占位符替换。模板不含 {history} 时，自动把历史附到末尾，避免误配置导致
    模型拿不到待压缩内容。
    """
    base = (template or DEFAULT_SUMMARY_PROMPT).strip() or DEFAULT_SUMMARY_PROMPT
    rendered = base.replace("{existing}", existing).replace("{history}", history)
    if "{history}" not in base:
        rendered = f"{rendered}\n\n对话历史：\n{history}"
    if existing and "{existing}" not in base:
        rendered = f"{rendered}\n\n{existing}"
    return rendered
