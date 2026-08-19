"""上下文超限错误识别 —— 移植自 OpenClaw isContextOverflowError。

关键价值：少数模型在某些情况下**不返回 usage**，导致我们基于 usage 的预防性压缩
无法触发；一旦这轮 prompt 恰好超过模型上下文窗口，上游会报错。此时不能直接把错误
抛给用户，而要靠**错误文本识别**判定这是「上下文超限」，触发应急压缩后重试，自救。

识别完全基于错误消息文本，不依赖 usage 数字，覆盖英文主流措辞 + 中文代理常见措辞。
"""
from __future__ import annotations

# 速率限制(TPM)有时也用 413，但那是限流不是上下文超限，需排除以免误压缩。
_RATE_LIMIT_TPM_HINTS = (
    "tokens per min",
    "tokens per minute",
    "tpm",
    "rate limit",
    "rate_limit",
    "requests per min",
)


def _has_rate_limit_tpm_hint(lower: str) -> bool:
    # 仅当同时出现 413/too large 之外的限流字样时，才认为是限流
    return any(h in lower for h in ("tokens per min", "tokens per minute", "requests per min")) or (
        "tpm" in lower and "limit" in lower
    )


def is_context_overflow_error(error_message: str | None) -> bool:
    """错误文本是否表示「上下文/prompt 超出模型窗口」。"""
    if not error_message:
        return False
    msg = str(error_message)
    lower = msg.lower()

    if _has_rate_limit_tpm_hint(lower):
        return False

    has_request_size_exceeds = "request size exceeds" in lower
    has_context_window = (
        "context window" in lower
        or "context length" in lower
        or "maximum context length" in lower
    )

    return (
        "request_too_large" in lower
        or ("invalid_argument" in lower and "maximum number of tokens" in lower)
        or "request exceeds the maximum size" in lower
        or "context length exceeded" in lower
        # OpenAI 风格 code（下划线版）：parrot/各家 OpenAI 兼容上游常见
        or "context_length_exceeded" in lower
        or "maximum context length" in lower
        # 「input exceeds the context window」措辞：input + context window 组合
        or ("input exceeds" in lower and "context window" in lower)
        or "exceeds the context window" in lower
        or "prompt is too long" in lower
        or "prompt too long" in lower
        or "exceeds model context window" in lower
        or "model token limit" in lower
        or ("input exceeds" in lower and "maximum number of tokens" in lower)
        or (has_request_size_exceeds and has_context_window)
        or "context overflow:" in lower
        or "exceed context limit" in lower
        or "exceeds the model's maximum context" in lower
        or ("max_tokens" in lower and "exceed" in lower and "context" in lower)
        or ("input length" in lower and "exceed" in lower and "context" in lower)
        or ("413" in lower and "too large" in lower)
        # GLM / 智谱等 OpenAI 兼容上游 + Anthropic 的窗口超限 stop reason
        or "context_window_exceeded" in lower
        or "model_context_window_exceeded" in lower
        # 各家 provider 专有措辞
        or "input token count exceeds the maximum number of input tokens" in lower
        or "input is too long for this model" in lower
        or "input exceeds the maximum number of tokens" in lower
        or "exceeds the available context size" in lower
        or "input too long for the model" in lower
        or "input is too long for the model" in lower
        # 中文代理常见措辞
        or "上下文过长" in msg
        or "上下文超出" in msg
        or "上下文长度超" in msg
        or "超出最大上下文" in msg
        or "请压缩上下文" in msg
        or "上下文太长" in msg
    )
