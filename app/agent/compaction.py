"""历史压缩 —— safeguard 式（参考 OpenClaw compaction: safeguard）。

平时不动；**当前上下文真实体积** 超过 contextWindow * ratio 才压缩：
保留近 N 轮原文，把更早的压成结构化中文摘要（便宜模型），存 summaries 表。

阈值判定用的是「模型最后一轮实际看到的 prompt 体积」（input + cache，由上游 usage
精确给出），而不是本地 estimate_tokens 自算——后者既粗又漏（不含 system / tools schema /
轮内 tool 往返），拿它判压缩会算出错误的值。estimate_tokens 仅在拿不到真实 usage 时兜底。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NamedTuple, Protocol

from app.db.dao import MessageDAO, SummaryDAO
from app.llm.base import LLMBackend
from app.logging import get_logger
from app.utils import estimate_tokens

log = get_logger("agent.compaction")


class CompactionAccountingError(RuntimeError):
    """A completed compression-model request could not be durably recorded."""


class _OperationLocks(Protocol):
    def chat(self, chat_id: int, operation: str = ""): ...



class CompressionCandidate(NamedTuple):
    backend: LLMBackend
    model: str
    source: str = "compression"
    label: str = ""


@dataclass(slots=True)
class CompactionOutcome:
    """Structured result for one compaction attempt.

    `did=False` means no durable history mutation happened.  The legacy bool APIs
    intentionally stay as thin wrappers around this detail object.
    """

    did: bool = False
    source: str = ""
    trigger_tokens: int = 0
    after_tokens: int = 0
    threshold_tokens: int = 0
    keep_recent: int = 0
    up_to_message_id: int = 0
    old_message_count: int = 0
    kept_message_count: int = 0
    summary_id: int = 0
    summary: str = ""
    summary_tokens: int = 0
    compression_model_label: str = ""
    token_source: str = ""
    reason: str = ""


class ContextCompactionGate(Protocol):
    """Normal, protocol-bound compaction gate used by Agent.run."""

    async def maybe_compact_and_rebuild(
        self,
        *,
        source: str,
        prompt_tokens: int | None = None,
        convo: list | None = None,
    ) -> list | None: ...


ContextCompactionCallback = Callable[[CompactionOutcome], Awaitable[None]]


DEFAULT_SUMMARY_PROMPT = """You are compacting an ongoing engineering conversation for future continuation. After compaction, the next model will only see this summary plus the most recent uncompressed messages, so the summary must be sufficient to continue the work without re-reading the compacted transcript.

Output in English only, even if the conversation history is in another language. Be dense, specific, and continuity-focused. Prefer concrete facts over generic prose. The latest user request, latest user corrections, and latest unfinished/current work have highest priority; do not let older context drown them out.

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
List all user messages represented in the compacted history when feasible. If there are too many, at minimum preserve every message that changed the task, constraints, priorities, permissions, or next step, plus the most recent user messages verbatim or near-verbatim. User wording matters; do not paraphrase away intent-changing details.

## Pending Tasks
List unfinished tasks in execution order. Distinguish confirmed tasks from optional follow-ups. Include required confirmations, safety boundaries, and tasks that must not be done unless the user asks.

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
- Preserve exact command outputs or code snippets only when they are necessary for continuation; otherwise summarize them with enough detail to avoid re-running work.
- If there is an existing summary, merge it with the new history without losing the latest current-work details.
- The final answer must be the summary only. Do not include apologies, prefaces, or commentary about doing the compaction.

Existing summary/context, if any:
{existing}

Conversation history to compact. Each line is prefixed by role:
{history}"""

# 质量门禁：合格摘要必须包含这些小节标题（缺了说明模型没按结构写，重试）。
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


def _format_history_for_summary(rows: list) -> str:
    """把待压缩消息格式化成喂给摘要模型的文本。

    不仅给 user/assistant 正文，也把 assistant 发起的工具调用名、tool 结果
    都带上（截断），让摘要模型看清「做过哪些操作、读了哪些文件」，才能写出
    包含项目/路径/标识符的高质量摘要。
    """
    # Lazy import avoids app.rath.__init__ -> single_agent -> compaction during
    # module initialization while keeping one shared controller projection.
    from app.rath.controller_projection import project_agent_tool_result_for_controller

    lines: list[str] = []
    for r in rows:
        role = r.role
        if role == "assistant":
            if r.content:
                lines.append(f"[assistant] {r.content[:1600]}")
            for tc in (r.tool_calls or []):
                args = (tc.arguments or "")[:500]
                lines.append(f"[assistant→工具] {tc.name}({args})")
        elif role == "tool":
            name = r.name or "tool"
            content = project_agent_tool_result_for_controller(name, r.content or "")
            meta = ""
            if "<tool-meta>" in content and "</tool-meta>" in content:
                start = content.find("<tool-meta>")
                end = content.find("</tool-meta>", start) + len("</tool-meta>")
                meta = content[start:end][:2000]
            preview = content[:800]
            lines.append(f"[工具结果:{name}] {meta or preview}")
        elif r.content:
            lines.append(f"[{role}] {r.content[:1600]}")
    return "\n".join(lines)


class Compactor:
    def __init__(self, messages: MessageDAO, summaries: SummaryDAO,
                 backend: LLMBackend, *, compression_model: str,
                 context_window: int, ratio: float = 0.7, keep_recent: int = 8,
                 retain_raw_recent: int | None = None,
                 summary_max_retries: int = 1,
                 summary_max_tokens: int = 4096,
                 summary_timeout_s: float = 1800.0,
                 prompt_template: str = "",
                 trigger_tokens: int = 0,
                 compression_label: str = "",
                 summary_source: str = "compression",
                 fallback_backend: LLMBackend | None = None,
                 fallback_compression_model: str = "",
                 fallback_compression_label: str = "",
                 extra_candidates: list[CompressionCandidate] | None = None,
                 operation_locks: _OperationLocks | None = None,
                 on_model_call: Callable[[dict], Awaitable[None]] | None = None) -> None:
        self._messages = messages
        self._summaries = summaries
        self._backend = backend
        self._model = compression_model
        self._model_label = compression_label or compression_model
        self._model_source = summary_source or "compression"
        self._window = context_window
        self._ratio = ratio
        # ``keep_recent`` is the user-facing visible-history limit. Root Web
        # compaction may retain zero raw protocol rows and rebuild that tail as
        # XML user/assistant text instead; other callers keep the legacy default.
        self._keep = max(0, int(keep_recent or 0))
        self._raw_keep = (
            self._keep
            if retain_raw_recent is None
            else max(0, int(retain_raw_recent or 0))
        )
        self._summary_max_retries = summary_max_retries
        self._summary_max_tokens = max(512, int(summary_max_tokens or 4096))
        self._summary_timeout_s = max(1.0, float(summary_timeout_s or 1800.0))
        self._prompt_template = prompt_template or ""
        self._trigger_tokens = max(0, int(trigger_tokens or 0))
        self._fallback_backend = fallback_backend
        self._fallback_model = fallback_compression_model or ""
        self._fallback_label = fallback_compression_label or fallback_compression_model or ""
        self._extra_candidates = list(extra_candidates or [])
        self._operation_locks = operation_locks
        self._on_model_call = on_model_call

    async def maybe_compact(self, chat_id: int, prompt_tokens: int | None = None) -> bool:
        """检查并按需压缩。返回是否执行了压缩。

        prompt_tokens — 模型最后一轮实际看到的 prompt 体积（input + cache），
                        由上游 usage 精确给出。这是当前上下文的真实体积，也是判压缩的
                        正确依据。为 None / <=0 时回退到本地 estimate_tokens 粗估兜底。
        """
        return (await self.maybe_compact_detail(chat_id, prompt_tokens=prompt_tokens)).did

    async def maybe_compact_detail(
        self,
        chat_id: int,
        prompt_tokens: int | None = None,
        *,
        source: str = "usage",
    ) -> CompactionOutcome:
        """检查并按需压缩，返回结构化结果供 Web/UI/stats 使用。"""
        if self._operation_locks is not None:
            async with self._operation_locks.chat(chat_id, "compact"):
                return await self._maybe_compact_unlocked(chat_id, prompt_tokens=prompt_tokens, source=source)
        return await self._maybe_compact_unlocked(chat_id, prompt_tokens=prompt_tokens, source=source)

    def _threshold(self) -> int:
        return self._trigger_tokens or int(self._window * self._ratio)

    async def _maybe_compact_unlocked(
        self,
        chat_id: int,
        prompt_tokens: int | None = None,
        *,
        source: str = "usage",
    ) -> CompactionOutcome:
        rows = await self._messages.recent(chat_id)
        if prompt_tokens and prompt_tokens > 0:
            total = int(prompt_tokens)
            token_source = "usage"
        else:
            total = sum(r.tokens or estimate_tokens(r.content) for r in rows)
            token_source = "estimate"
        threshold = self._threshold()
        if total <= threshold:
            return CompactionOutcome(
                did=False,
                source=source or token_source,
                trigger_tokens=total,
                threshold_tokens=threshold,
                keep_recent=self._keep,
                token_source=token_source,
                reason="under_threshold",
            )
        if len(rows) <= 2 and self._raw_keep > 0:
            return CompactionOutcome(
                did=False,
                source=source or token_source,
                trigger_tokens=total,
                threshold_tokens=threshold,
                keep_recent=self._keep,
                token_source=token_source,
                reason="too_few_messages",
            )

        # ``retain_raw_recent=0`` is used by root Web conversations: every raw
        # protocol message is folded into the durable summary, while the recent
        # visible dialogue is reconstructed separately as XML. Legacy callers keep
        # their raw tail behavior by default.
        keep = self._raw_keep
        if keep > 0 and len(rows) <= keep:
            keep = max(2, len(rows) // 2)

        compaction_source = source or token_source
        log.info("触发历史压缩", 会话=chat_id, 上下文Token=total, 来源=compaction_source,
                 token来源=token_source, 阈值=threshold, 原始保留=keep, 可见文本保留=self._keep,
                 未压缩条数=len(rows))
        return await self._compact_old(
            chat_id,
            rows,
            keep=keep,
            source=compaction_source,
            trigger_tokens=total,
            threshold_tokens=threshold,
            token_source=token_source,
        )

    async def force_compact(self, chat_id: int, *, keep: int | None = None) -> bool:
        """应急压缩：不看阈值，强制把较早消息压成摘要。

        用于「上下文超限」自救——某些模型不返回 usage，预防性压缩不触发，等到上游
        报超限错误时调用本方法兜底。keep 默认比常规更激进（保留更少近轮），尽量腾空间。
        """
        return (await self.force_compact_detail(chat_id, keep=keep)).did

    async def force_compact_detail(
        self,
        chat_id: int,
        *,
        keep: int | None = None,
        source: str = "emergency",
    ) -> CompactionOutcome:
        """强制压缩并返回结构化结果。"""
        if self._operation_locks is not None:
            async with self._operation_locks.chat(chat_id, "compact"):
                return await self._force_compact_unlocked(chat_id, keep=keep, source=source)
        return await self._force_compact_unlocked(chat_id, keep=keep, source=source)

    async def _force_compact_unlocked(
        self,
        chat_id: int,
        *,
        keep: int | None = None,
        source: str = "emergency",
    ) -> CompactionOutcome:
        rows = await self._messages.recent(chat_id)
        k = keep if keep is not None else max(0, self._raw_keep // 2)
        threshold = self._threshold()
        if len(rows) <= k:
            # 已经压无可压（剩的都在保留窗口内）
            return CompactionOutcome(
                did=False,
                source=source,
                threshold_tokens=threshold,
                keep_recent=self._keep,
                reason="too_few_messages",
            )
        log.info("应急压缩(上下文超限)", 会话=chat_id, 当前条数=len(rows), 保留=k)
        return await self._compact_old(
            chat_id,
            rows,
            keep=k,
            source=source,
            trigger_tokens=0,
            threshold_tokens=threshold,
            token_source="force",
        )

    async def _compact_old(
        self,
        chat_id: int,
        rows: list,
        *,
        keep: int,
        source: str = "compact",
        trigger_tokens: int = 0,
        threshold_tokens: int = 0,
        token_source: str = "",
    ) -> CompactionOutcome:
        """把 rows 中较早的部分压成摘要，保留最近 keep 条。返回是否执行。

        ★ old 会被压成摘要文本，其内部结构无所谓；真正必须合法的是 kept（保留窗口）。
          kept 唯一的非法形态就是「以 tool 结果打头」——配对的工具调用被压进摘要后，
          这个 tool 结果就成了孤儿，被上游判非法（协议无关，两边都炸）：
          - OpenAI Chat：「No tool call found for function call output with call_id ...」
          - Anthropic：孤儿 tool_result 找不到对应 tool_use。
          因此规则只有一条且充分：把 kept 开头连续的 tool 结果向前并入 old 压掉，
          直到 kept 以 user / assistant 打头。

          切勿在并入后再回退 old 末尾的 assistant 工具调用——那会把这个 assistant
          推回 kept 开头、而它配对的 tool 已被压走，反而制造 Anthropic 的
          「tool_use ids without tool_result」非法结构。
        """
        split = len(rows) - keep
        if split <= 0:
            return CompactionOutcome(
                did=False,
                source=source,
                trigger_tokens=trigger_tokens,
                threshold_tokens=threshold_tokens,
                keep_recent=self._keep,
                token_source=token_source,
                reason="empty_old_window",
            )
        # ① 先尽量多保留：若切点前一条是 assistant 工具调用，把它和它的 tool 结果
        #    一起留到 kept（往前挪切点），保住更完整的近期上下文。
        while split > 0 and rows[split - 1].role == "assistant" and rows[split - 1].tool_calls:
            split -= 1
        # ② 安全网：kept 开头不留孤儿 tool 结果，向前并入 old 压掉。这一步最终决定
        #    切点，保证 kept[0] 一定不是 tool。
        while split < len(rows) and rows[split].role == "tool":
            split += 1
        old = rows[:split]
        if not old:
            return CompactionOutcome(
                did=False,
                source=source,
                trigger_tokens=trigger_tokens,
                threshold_tokens=threshold_tokens,
                keep_recent=self._keep,
                token_source=token_source,
                reason="empty_old_window",
            )

        history_text = _format_history_for_summary(old)
        existing = ""
        prev = await self._summaries.latest(chat_id)
        if prev and prev.get("summary"):
            existing = f"已有摘要（请在此基础上增量合并，保持同样的小节结构）：\n{prev['summary']}\n\n"

        summary_result = await self._summarize_with_quality_gate(chat_id, existing, history_text)
        if summary_result is None:
            # 质量不达标 / 生成失败 → 不压缩，如实保留历史，绝不写入垃圾摘要。
            return CompactionOutcome(
                did=False,
                source=source,
                trigger_tokens=trigger_tokens,
                threshold_tokens=threshold_tokens,
                keep_recent=self._keep,
                old_message_count=len(old),
                kept_message_count=len(rows) - len(old),
                token_source=token_source,
                reason="summary_failed_quality_gate",
            )

        summary, compression_model_label = summary_result
        up_to = old[-1].id
        summary_tokens = estimate_tokens(summary)
        summary_id = await self._summaries.add(chat_id, summary, up_to, summary_tokens)
        await self._messages.mark_compacted(chat_id, up_to)
        kept = rows[split:]
        kept_count = len(kept)
        after_tokens = summary_tokens + sum(
            int(row.tokens or estimate_tokens(row.content or ""))
            for row in kept
        )
        log.info("历史压缩完成", 会话=chat_id, 覆盖至=up_to, 摘要Token=summary_tokens)
        return CompactionOutcome(
            did=True,
            source=source,
            trigger_tokens=trigger_tokens,
            after_tokens=after_tokens,
            threshold_tokens=threshold_tokens,
            keep_recent=self._keep,
            up_to_message_id=up_to,
            old_message_count=len(old),
            kept_message_count=kept_count,
            summary_id=summary_id,
            summary=summary,
            summary_tokens=summary_tokens,
            compression_model_label=compression_model_label,
            token_source=token_source,
        )

    async def _notify_model_call(self, detail: dict) -> None:
        if self._on_model_call is None:
            return
        maybe = self._on_model_call(detail)
        if inspect.isawaitable(maybe):
            await maybe

    async def _summarize_with_quality_gate(
        self, chat_id: int, existing: str, history_text: str) -> tuple[str, str] | None:
        """生成摘要 + 质量门禁，返回摘要及实际成功模型；不合格则返回 None。

        宁可不压缩、如实保留历史，也不写入一份「连项目都没说清」的垃圾摘要——那会让
        模型在压缩后彻底失去上下文（线上踩过的坑）。
        """
        prompt = _render_summary_prompt(self._prompt_template, existing=existing, history=history_text)
        attempts = 1 + max(0, self._summary_max_retries)
        last_missing: list[str] = []
        candidates: list[CompressionCandidate] = [CompressionCandidate(self._backend, self._model, self._model_source, self._model_label)]
        seen_keys = {self._model_label or self._model}
        for candidate in self._extra_candidates:
            candidate_key = candidate.label or candidate.model
            if not candidate.backend or not candidate.model or candidate_key in seen_keys:
                continue
            seen_keys.add(candidate_key)
            candidates.append(candidate)
        fallback_key = self._fallback_label or self._fallback_model
        if self._fallback_backend is not None and self._fallback_model and fallback_key not in seen_keys:
            candidates.append(CompressionCandidate(self._fallback_backend, self._fallback_model, "primary-fallback", fallback_key))
        for candidate_index, (backend, model, source, label) in enumerate(candidates):
            for attempt in range(attempts):
                content = prompt
                if attempt > 0 and last_missing:
                    # 重试时明确点出缺了哪些小节，但仍保持单条 user，避免严格协议拒绝连续 user。
                    retry_hint = (
                        "上一次摘要缺少这些必需小节："
                        + "、".join(last_missing)
                        + ". Please regenerate the summary with all required headings in English; write \"None\" for empty sections."
                    )
                    content = f"{retry_hint}\n\n{prompt}"
                messages = [{"role": "user", "content": content}]
                call_started = time.monotonic()
                try:
                    async with asyncio.timeout(self._summary_timeout_s):
                        result = await backend.complete(
                            messages,
                            model=model,
                            max_tokens=self._summary_max_tokens,
                            read_timeout_s=self._summary_timeout_s,
                        )
                except Exception as e:
                    if self._on_model_call is not None:
                        try:
                            await self._notify_model_call({
                                "status": "error",
                                "model": label or model,
                                "protocol": str(getattr(backend, "protocol", "") or ""),
                                "totalTimeMs": int((time.monotonic() - call_started) * 1000),
                                "errorType": type(e).__name__,
                                "serviceTier": getattr(e, "service_tier", ""),
                                "providerCostUsd": getattr(e, "provider_cost_usd", None),
                                "kind": "context_compaction",
                            })
                        except Exception as accounting_exc:
                            log.exception("失败压缩模型调用即时记账失败，停止压缩以避免继续产生未入账请求", 会话=chat_id, 模型=label or model)
                            raise CompactionAccountingError("compression model-call accounting failed") from accounting_exc
                    log.warning("摘要生成失败", 会话=chat_id, 模型来源=source, 模型=label or model, 尝试=attempt + 1, 错误=str(e)[:120])
                    continue
                if self._on_model_call is not None:
                    try:
                        await self._notify_model_call({
                            "status": "ok",
                            "usage": result.usage,
                            "model": label or model,
                            "protocol": str(getattr(backend, "protocol", "") or ""),
                            "totalTimeMs": int((time.monotonic() - call_started) * 1000),
                            "outputTokens": result.usage.output_tokens,
                            "serviceTier": result.service_tier,
                            "providerCostUsd": result.provider_cost_usd,
                            "kind": "context_compaction",
                        })
                    except Exception as accounting_exc:
                        log.exception("压缩模型调用即时记账失败，停止压缩以避免继续产生未入账请求", 会话=chat_id, 模型=label or model)
                        raise CompactionAccountingError("compression model-call accounting failed") from accounting_exc
                summary = (result.text or "").strip()
                if not summary:
                    continue
                missing = _summary_missing_sections(summary)
                if not missing:
                    if source == "primary-fallback":
                        log.warning("压缩模型全部失败后已回退主模型完成摘要", 会话=chat_id, 回退模型=label or model)
                    elif source != "compression":
                        log.warning("压缩候选模型完成摘要", 会话=chat_id, 模型来源=source, 模型=label or model)
                    return summary, label or model
                last_missing = missing
                log.warning("摘要缺必需小节，准备重试", 会话=chat_id, 模型来源=source, 模型=label or model, 尝试=attempt + 1,
                            缺失=missing)
            if source != "primary-fallback" and candidate_index < len(candidates) - 1:
                log.warning("压缩候选模型未产出合格摘要，继续尝试下一个候选", 会话=chat_id, 模型来源=source, 模型=label or model)
        log.warning("摘要多次不达标，放弃本次压缩（保留完整历史）", 会话=chat_id,
                    缺失=last_missing)
        return None
