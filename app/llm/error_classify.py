"""统一错误分类 —— 自给自足、通用健壮,不依赖任何特定上游已归一。

设计原则(老大定调)
====================
OpenBear 当前接 Parrot,但 Parrot 只是上游之一;一旦 Parrot 开源,别人可能裸接
OpenAI / Anthropic / 任意网关。所以错误处理必须自己判得准,不能假设"上游已经帮我
分好类了"。

双重判据(参考 Parrot failover 的 status 优先 + 文本兜底)
=========================================================
1. HTTP status code(第一判据,标准语义,任何规范上游都遵守):
   - 401 / 403           → auth / auth_permanent(鉴权,不可重试)
   - 402                 → billing(欠费/额度,不可重试)
   - 404                 → model_not_found(不可重试)
   - 408                 → timeout(可重试)
   - 400 / 422           → 看文本细分:上下文超限 / 限流(TPM) / 否则 format(不可重试)
   - 413                 → 看文本:TPM 限流(可重试) / 否则 context_overflow(走压缩)
   - 429                 → rate_limit(可重试;但 context-credit 类除外,见文本判定)
   - 500/502/503/504/529 → server_error / overloaded(可重试)
2. 错误文本(第二判据,覆盖「200 OK 但 body 是错误」、SSE 流内 error 事件无 status、
   非标准上游只在 message 里写原因的情况)。

输出 reason 枚举 → 推导 retryable:
   可重试   = rate_limit, overloaded, timeout, server_error
   不可重试 = billing, auth, auth_permanent, format, model_not_found, context_overflow
   (context_overflow 不走普通重试,交给上层应急压缩路径处理)
"""
from __future__ import annotations

import re

from app.agent.context_overflow import is_context_overflow_error

# —— reason 枚举(字符串常量,便于序列化/日志) ——
RATE_LIMIT = "rate_limit"
OVERLOADED = "overloaded"
TIMEOUT = "timeout"
SERVER_ERROR = "server_error"
BILLING = "billing"
AUTH = "auth"
AUTH_PERMANENT = "auth_permanent"
FORMAT = "format"
MODEL_NOT_FOUND = "model_not_found"
CONTEXT_OVERFLOW = "context_overflow"
UNKNOWN = "unknown"

# 可重试的 reason 集合(其余一律不可重试)。
_RETRYABLE_REASONS = frozenset({RATE_LIMIT, OVERLOADED, TIMEOUT, SERVER_ERROR})
_KNOWN_REASONS = frozenset({
    RATE_LIMIT, OVERLOADED, TIMEOUT, SERVER_ERROR, BILLING, AUTH,
    AUTH_PERMANENT, FORMAT, MODEL_NOT_FOUND, CONTEXT_OVERFLOW, UNKNOWN,
})
_CLASSIFICATION_ALIASES = {
    # Parrot's structured terminal classifications. Keep this mapping explicit:
    # these values are a protocol contract, not prose to be guessed from.
    "authentication_error": AUTH,
    "permission_error": AUTH_PERMANENT,
    "rate_limit_error": RATE_LIMIT,
    "upstream_server_error": SERVER_ERROR,
    "transport_error": SERVER_ERROR,
    "rate_limited": RATE_LIMIT,
    "ratelimit": RATE_LIMIT,
    "quota": BILLING,
    "quota_exhausted": BILLING,
    "quota_exceeded": BILLING,
    "insufficient_quota": BILLING,
    "billing_error": BILLING,
    "monthly_spending": BILLING,
    "monthly_spending_limit": BILLING,
    "monthly_spending_limit_reached": BILLING,
    "credit_exhausted": BILLING,
    "credits_exhausted": BILLING,
    "permission": AUTH_PERMANENT,
    "forbidden": AUTH_PERMANENT,
    "invalid_request": FORMAT,
    "invalid_request_error": FORMAT,
    "upstream": SERVER_ERROR,
}
_CLIENT_REQUEST_RETRY_SCOPES = frozenset({
    "client", "request", "client_request", "client/request", "whole_request",
})


def _normalized_contract_token(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def normalize_classification(value: str | None, status: int = 0) -> str:
    """Map structured Parrot/provider classifications to OpenBear reasons."""
    normalized = _normalized_contract_token(value)
    if normalized in _KNOWN_REASONS:
        return normalized
    # Parrot uses this for a non-special upstream HTTP status. Status is the
    # structured discriminator (for example 404 vs 408); no message guessing is
    # needed. Without one, keep the contract terminal and conservative.
    if normalized == "upstream_http_error":
        return classify_error("", status) if status else UNKNOWN
    return _CLASSIFICATION_ALIASES.get(normalized, "")


def is_retryable_reason(reason: str) -> bool:
    return reason in _RETRYABLE_REASONS


def retryable_from_contract(
    reason: str,
    *,
    classification: str = "",
    status: int = 0,
    retryable: bool | None = None,
    retry_scope: str = "",
    structured: bool = False,
) -> bool:
    """Translate an upstream retry contract to a whole-request retry decision.

    Parrot's candidate/account scopes describe work *inside* one Parrot request;
    they are not permission for OpenBear to repeat that whole request. Explicit
    request/client scopes may grant that permission. Independently, recognized
    transient rate/server/transport classifications keep OpenBear's normal retry.
    Billing/quota contracts and an explicit ``retryable=false`` are always final.
    """
    classified_reason = normalize_classification(classification, status)
    if reason == BILLING or classified_reason == BILLING:
        return False
    if retryable is False:
        return False
    if not structured:
        return is_retryable_reason(reason) if retryable is None else bool(retryable)

    scope = _normalized_contract_token(retry_scope)
    if retryable is True and scope in _CLIENT_REQUEST_RETRY_SCOPES:
        return True

    # Internal scopes such as same_candidate/next_candidate/channel/account are
    # intentionally not consulted here. A known transient classification (or a
    # status-derived contract with no classification) is sufficient on its own.
    if classified_reason:
        return is_retryable_reason(classified_reason)
    if not _normalized_contract_token(classification):
        return is_retryable_reason(reason)
    return False


# —— 文本判定辅助(全小写匹配,中英文兼顾) ——
def _has_tpm_rate_limit(low: str) -> bool:
    """TPM(tokens per minute)类限流:用 413/429 表达但本质是限流,可重试。"""
    return (
        "tokens per min" in low
        or "tokens per minute" in low
        or "requests per min" in low
        or ("tpm" in low and "limit" in low)
        or "rate limit" in low
        or "rate_limit" in low
        or "too many requests" in low
        or "tokens per day" in low
        or "requests per day" in low
    )


def _is_billing_text(low: str) -> bool:
    return (
        "insufficient" in low and ("credit" in low or "balance" in low or "quota" in low or "fund" in low)
        or "billing" in low
        or "payment required" in low
        or "credit balance" in low
        or "exceeded your current quota" in low
        or "monthly spending limit" in low
        or "spending limit" in low
        or "used all available credits" in low
        or "credits exhausted" in low
        or "credit exhausted" in low
        or "insufficient_quota" in low
        or "余额不足" in low
        or "欠费" in low
        or "充值" in low
        or "额度不足" in low
    )


def _is_auth_text(low: str) -> bool:
    return (
        "invalid api key" in low
        or "invalid_api_key" in low
        or "incorrect api key" in low
        or "unauthorized" in low
        or "authentication" in low
        or "api key" in low and ("invalid" in low or "missing" in low or "expired" in low)
        or "鉴权" in low
        or "密钥" in low and ("无效" in low or "错误" in low)
    )


def _is_auth_permanent_text(low: str) -> bool:
    return (
        "permission" in low and ("denied" in low or "insufficient" in low)
        or "forbidden" in low
        or "not allowed" in low
        or "access denied" in low
        or "无权限" in low
        or "禁止访问" in low
    )


def _is_rate_limit_text(low: str) -> bool:
    return _has_tpm_rate_limit(low) or "429" in low or "throttl" in low or "限流" in low or "请求过于频繁" in low


def _is_overloaded_text(low: str) -> bool:
    return (
        "overloaded" in low
        or "overloaded_error" in low
        or "capacity" in low and "exceed" in low
        or "server is busy" in low
        or "服务繁忙" in low
        or "过载" in low
    )


def _is_timeout_text(low: str) -> bool:
    return (
        "timeout" in low
        or "timed out" in low
        or "deadline exceeded" in low
        or "超时" in low
    )


def _is_model_not_found_text(low: str) -> bool:
    return (
        "model not found" in low
        or "model_not_found" in low
        or "does not exist" in low and "model" in low
        or "unknown model" in low
        or "no such model" in low
        or "模型不存在" in low
    )


def _is_server_error_text(low: str) -> bool:
    return (
        "internal server error" in low
        or "internal_error" in low
        or "server_error" in low
        or "service unavailable" in low
        or "bad gateway" in low
        or "gateway timeout" in low
        or "temporarily unavailable" in low
        or "503" in low
        or "502" in low
        or "504" in low
        or "529" in low
        or "500" in low and "error" in low
    )


def classify_error(message: str | None, status: int = 0) -> str:
    """把(错误文本, HTTP status)归类成 reason 枚举。

    status 优先(标准语义),缺失/不决时回退文本匹配;两者都不命中 → UNKNOWN。
    """
    msg = message or ""
    low = msg.lower()

    # —— context overflow 最优先:它要走压缩自救,绝不能被当普通错误吞掉 ——
    # (但纯 TPM 限流形如 413 不算 overflow,is_context_overflow_error 内部已排除)
    if is_context_overflow_error(msg):
        return CONTEXT_OVERFLOW

    # —— 第一判据:HTTP status ——
    if status:
        if status == 401:
            return AUTH
        if status == 403:
            # Several providers use 403 for account spending/credit exhaustion.
            # Billing text must win before permission/auth classification.
            if _is_billing_text(low):
                return BILLING
            return AUTH_PERMANENT if _is_auth_permanent_text(low) else AUTH
        if status == 402:
            return BILLING
        if status == 404:
            return MODEL_NOT_FOUND if _is_model_not_found_text(low) else MODEL_NOT_FOUND
        if status == 408:
            return TIMEOUT
        if status == 413:
            # 413:TPM 限流(可重试) vs 请求体过大(context overflow,上面已拦)
            return RATE_LIMIT if _has_tpm_rate_limit(low) else CONTEXT_OVERFLOW
        if status == 429:
            # 429 多为限流;少数上游用 429 表达欠费(credit required),文本能识别则归 billing
            return BILLING if _is_billing_text(low) else RATE_LIMIT
        if status in (400, 422):
            # 客户端请求错误:先看是不是被包装的限流/欠费,否则按参数格式错(不可重试)
            if _is_rate_limit_text(low):
                return RATE_LIMIT
            if _is_billing_text(low):
                return BILLING
            return FORMAT
        if status >= 500:
            # Gateways may keep their transport 5xx while embedding the actionable
            # upstream 429/403 cause in text. Do not flatten that root cause.
            if _is_billing_text(low):
                return BILLING
            if _is_rate_limit_text(low) and ("rate limit" in low or "rate_limit" in low or re.search(r"\bhttp\s+429\b", low)):
                return RATE_LIMIT
            if re.search(r"\bhttp\s+403\b", low):
                return AUTH_PERMANENT if _is_auth_permanent_text(low) else AUTH
            return OVERLOADED if _is_overloaded_text(low) else SERVER_ERROR

    # —— 第二判据:错误文本(无 status / status 未决,如 200 带错误 body、SSE 流内 error) ——
    if _is_billing_text(low):
        return BILLING
    if _is_auth_permanent_text(low):
        return AUTH_PERMANENT
    if _is_auth_text(low):
        return AUTH
    if _is_overloaded_text(low):
        return OVERLOADED
    if _is_rate_limit_text(low):
        return RATE_LIMIT
    if _is_model_not_found_text(low):
        return MODEL_NOT_FOUND
    if _is_timeout_text(low):
        return TIMEOUT
    if _is_server_error_text(low):
        return SERVER_ERROR

    return UNKNOWN


# —— 用户可见文案(按 reason) ——
_USER_MESSAGES = {
    RATE_LIMIT: "⚠️ 上游限流了,稍等再试试～",
    OVERLOADED: "⚠️ 上游负载过高,稍后重试～",
    TIMEOUT: "⏱ 上游响应超时了,再试一次看看～",
    SERVER_ERROR: "❌ 上游服务异常,稍后重试～",
    BILLING: "💳 上游余额/额度不足,需要检查账户。",
    AUTH: "❌ 上游鉴权失败(检查 apiKey)。",
    AUTH_PERMANENT: "❌ 上游拒绝访问(权限不足)。",
    FORMAT: "❌ 请求被上游拒绝(参数/格式问题)。",
    MODEL_NOT_FOUND: "❌ 上游找不到该模型(检查模型名)。",
    CONTEXT_OVERFLOW: "📦 上下文超出模型窗口,正在尝试压缩后重试…",
}


def user_message_for(reason: str, raw: str = "") -> str:
    """按 reason 给出用户友好文案;UNKNOWN 回退到净化后的原始错误。"""
    if reason in _USER_MESSAGES:
        return _USER_MESSAGES[reason]
    from app.llm.error_sanitize import sanitize_user_facing_text
    return f"❌ 模型调用失败:{sanitize_user_facing_text(raw)}"
