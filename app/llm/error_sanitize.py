"""错误文案净化 —— 把上游原始错误整理成适合展示给用户的短文本。

上游错误五花八门:有的是 Cloudflare/nginx 的 HTML 错误页,有的是一大坨 JSON,有的
带一堆内部栈。直接丢给老大既难看也泄露细节。这里做轻量净化:去 HTML 标签、压缩空白、
截断到合理长度。只用于「用户可见文案」,不影响日志里保留的完整原文。
"""
from __future__ import annotations

import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Credentials can appear in malformed gateway bodies where structured redaction is
# impossible.  Remove both the field name and value before any user-visible text
# or bounded raw payload is retained.
_AUTH_HEADER_RE = re.compile(
    r"(?i)[\"']?\b(?:authorization|proxy-authorization)\b[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\r\n,;}]+)"
)
_SECRET_FIELD_RE = re.compile(
    r'''(?ix)
    ["']?(?:api[-_]?key|access[-_]?token|refresh[-_]?token|credential|client[-_]?secret)["']?
    \s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;}]+)
    '''
)
_BEARER_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_TOKEN_RE = re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_MAX_LEN = 300


def redact_sensitive_text(raw: str | None) -> str:
    """Remove credential-bearing fragments from arbitrary upstream text."""
    if not raw:
        return ""
    text = str(raw)
    text = _AUTH_HEADER_RE.sub("[凭证已隐藏]", text)
    text = _SECRET_FIELD_RE.sub("[凭证已隐藏]", text)
    text = _BEARER_RE.sub("[凭证已隐藏]", text)
    return _KEY_TOKEN_RE.sub("[凭证已隐藏]", text)


def _looks_like_html(text: str) -> bool:
    low = text.lower()
    return "<html" in low or "<!doctype" in low or "<body" in low or "cloudflare" in low


def sanitize_user_facing_text(raw: str | None, *, max_len: int = _MAX_LEN) -> str:
    """净化错误文本:HTML 页 → 提炼一句;长文 → 截断。"""
    if not raw:
        return "未知错误"
    text = redact_sensitive_text(str(raw)).strip()

    if _looks_like_html(text):
        # HTML 错误页:剥标签后取首个有意义的短句,失败则给通用提示。
        stripped = _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()
        if not stripped:
            return "上游返回了一个网页错误(可能是网关/CDN 拦截)。"
        text = stripped

    # 压缩空白
    text = _WS_RE.sub(" ", text).strip()

    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text or "未知错误"
