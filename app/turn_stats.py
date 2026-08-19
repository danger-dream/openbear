"""一轮对话结束后的统计卡片渲染。"""
from __future__ import annotations

import html as html_lib

from app.agent.result import RunResult


def _fmt_duration(ms: int) -> str:
    """毫秒 → 紧凑可读时长：1.2s / 13s / 2m13s / 1h2m。"""
    if ms <= 0:
        return "0s"
    s = ms / 1000
    if s < 10:
        return f"{s:.1f}s"
    sec = int(round(s))
    if sec < 60:
        return f"{sec}s"
    m, r = divmod(sec, 60)
    if m < 60:
        return f"{m}m{r:02d}s" if r else f"{m}m"
    h, m2 = divmod(m, 60)
    return f"{h}h{m2:02d}m" if m2 else f"{h}h"


def _fmt_tokens(n: int) -> str:
    """token 数 → 紧凑：1234 → 1.2k，1200000 → 1.2M。"""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def _esc(s: str) -> str:
    return html_lib.escape(s, quote=False)


def build_turn_stats_card(
    result: RunResult,
    *,
    model: str = "",
    think_level: str = "",
    cost_usd: float = 0.0,
    halted_reason: str = "",
    context_window: int = 0,
) -> str:
    """渲染一轮统计卡片（HTML，含可折叠 blockquote）。"""
    u = result.usage
    expert_u = result.expert_usage
    prompt_in = u.input_tokens + expert_u.input_tokens
    cache_read = u.cache_read_tokens + expert_u.cache_read_tokens
    cache_write = u.cache_write_tokens + expert_u.cache_write_tokens
    out = u.output_tokens + expert_u.output_tokens
    cache_base = prompt_in + cache_read
    cache_pct = (cache_read / cache_base * 100) if cache_base > 0 else 0.0
    total_tok = prompt_in + cache_read + cache_write + out

    total_model_calls = result.model_calls + result.expert_model_calls
    calls_str = f"模型调用 <code>{total_model_calls}</code> 次"
    extra = []
    if result.model_retry:
        extra.append(f"重试 {result.model_retry}")
    if result.model_fail:
        extra.append(f"失败 {result.model_fail}")
    if extra:
        calls_str += f"({_esc('、'.join(extra))})"
    tools_n = len(result.tools_used) + result.expert_tool_calls

    line_title = f"📊 <b>本轮统计</b> · ⏱ 耗时 <code>{_fmt_duration(result.total_time_ms)}</code>"
    if result.reasoning_ms_sum > 0:
        line_title += f" · 🧠 思考 <code>{_fmt_duration(result.reasoning_ms_sum)}</code>"

    lines = [line_title]
    if model:
        line_model = f"🤖 <code>{_esc(model)}</code>"
        if think_level and think_level != "off":
            line_model += f" · 🧠<code>{_esc(think_level)}</code>"
        lines.append(line_model)

    line_calls = f"🔄 {calls_str}"
    if tools_n:
        line_calls += f" · 🛠 工具调用 <code>{tools_n}</code> 次"
    lines.append(line_calls)
    if result.expert_tasks:
        lines.append(
            f"👥 Agent 已计入：任务 <code>{result.expert_tasks}</code> 个 · "
            f"🔄 <code>{result.expert_model_calls}</code> · "
            f"🛠 <code>{result.expert_tool_calls}</code>"
        )

    lu = result.last_usage
    ctx_prompt = lu.input_tokens + lu.cache_read_tokens + lu.cache_write_tokens
    if ctx_prompt > 0:
        if context_window > 0:
            ctx_pct = ctx_prompt / context_window * 100
            lines.append(
                f"🪟 上下文 <code>{_fmt_tokens(ctx_prompt)}</code> / "
                f"<code>{_fmt_tokens(context_window)}</code> "
                f"(<code>{ctx_pct:.1f}%</code>)"
            )
        else:
            lines.append(f"🪟 上下文 <code>{_fmt_tokens(ctx_prompt)}</code>")

    tok_parts = [
        f"<code>{_fmt_tokens(total_tok)}</code>",
        f"↑<code>{_fmt_tokens(prompt_in)}</code>",
        f"↓<code>{_fmt_tokens(out)}</code>",
    ]
    if cache_read or cache_write:
        cache_str = f"缓存 {_fmt_tokens(cache_read)}"
        if cache_base > 0:
            cache_str += f"({cache_pct:.1f}%)"
        tok_parts.append(_esc(cache_str))
    lines.append("📊 Tokens：" + " · ".join(tok_parts))

    total_cost = cost_usd + result.expert_cost_usd
    if total_cost > 0:
        lines.append(f"💰 <code>${total_cost:.4f}</code>")

    halt_label = {
        "no_progress": "⚠️ 检测到重复调用，已中止",
        "wall_time": "⚠️ 已达时长上限，已停止",
        "cancelled": "⏹ 已手动停止",
    }.get(halted_reason, "")
    if halt_label:
        lines.append(_esc(halt_label))

    return f"<blockquote expandable>{chr(10).join(lines)}</blockquote>"
