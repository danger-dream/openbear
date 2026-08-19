"""Depth-bounded normalization for HTTP, SSE, and provider error payloads.

Parrot's current contract places ``summary``, ``root_cause`` and ``attempts``
under the standard ``error.details`` object.  Older gateways may instead wrap
one or more JSON errors inside strings such as ``HTTP 503: {...HTTP 429...}``.
This module consumes both forms before applying any storage bound.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.llm.error_classify import (
    BILLING,
    classify_error,
    normalize_classification,
    retryable_from_contract,
)
from app.llm.error_sanitize import redact_sensitive_text, sanitize_user_facing_text
from app.llm.events import StreamEvent

_MAX_PARSE_DEPTH = 8
_MAX_DETAIL_DEPTH = 6
_MAX_RAW_CHARS = 4000
_MAX_DETAIL_STRING = 2000
_MAX_COLLECTION_ITEMS = 64
_HTTP_PREFIX_RE = re.compile(r"(?is)\bHTTP\s+(\d{3})\s*:\s*")
_SENSITIVE_KEYS = frozenset({
    "authorization", "proxy_authorization", "proxy-authorization",
    "api_key", "apikey", "api-key", "access_token", "refresh_token",
    "credential", "credentials", "client_secret", "secret",
})
_STATUS_KEYS = ("status", "status_code", "statusCode", "http_status", "httpStatus")
_MESSAGE_KEYS = ("message", "error_message", "errorMessage", "description", "title")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _status(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if 100 <= parsed <= 599 else 0
    return 0


def _dict_status(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    for key in _STATUS_KEYS:
        parsed = _status(value.get(key))
        if parsed:
            return parsed
    # Some provider payloads put an HTTP status in ``code``.
    return _status(value.get("code"))


def _sensitive_key(key: Any) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    return normalized in {item.replace("-", "_") for item in _SENSITIVE_KEYS}


def _safe_copy(value: Any, *, depth: int = 0) -> Any:
    """Bound and redact details without mutating the parsed full payload."""
    if depth >= _MAX_DETAIL_DEPTH:
        return "[详情层级已截断]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                result["_truncated"] = True
                break
            if _sensitive_key(key):
                continue
            result[str(key)] = _safe_copy(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [_safe_copy(item, depth=depth + 1) for item in list(value)[:_MAX_COLLECTION_ITEMS]]
        if len(value) > _MAX_COLLECTION_ITEMS:
            result.append("[更多项目已截断]")
        return result
    if isinstance(value, str):
        redacted = redact_sensitive_text(value)
        return redacted[:_MAX_DETAIL_STRING] + ("…" if len(redacted) > _MAX_DETAIL_STRING else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_user_facing_text(str(value), max_len=_MAX_DETAIL_STRING)


def sanitize_error_detail(value: Any) -> Any:
    """Return a credential-free, bounded copy for persisted error detail."""
    return _safe_copy(value)


def _bounded_payload(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(_safe_copy(value), ensure_ascii=False, separators=(",", ":"))
    else:
        text = redact_sensitive_text(str(value or ""))
    return text[:_MAX_RAW_CHARS] + ("…" if len(text) > _MAX_RAW_CHARS else "")


def _json_value(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[\"":
        return None
    try:
        return json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


@dataclass(slots=True)
class _Scan:
    statuses: list[tuple[int, int]] = field(default_factory=list)
    messages: list[tuple[int, str, str]] = field(default_factory=list)
    structured: list[tuple[int, dict[str, Any]]] = field(default_factory=list)


def _details_candidate(node: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("details", "error_details", "errorDetails"):
        candidate = node.get(key)
        if isinstance(candidate, dict) and any(
            field_name in candidate
            for field_name in ("summary", "root_cause", "rootCause", "attempts")
        ):
            return candidate
    if any(field_name in node for field_name in ("summary", "root_cause", "rootCause", "attempts")):
        return node
    return None


def _walk(value: Any, scan: _Scan, *, depth: int = 0, field_name: str = "") -> None:
    if depth > _MAX_PARSE_DEPTH:
        return
    if isinstance(value, dict):
        candidate = _details_candidate(value)
        if candidate is not None:
            scan.structured.append((depth, candidate))
        status = _dict_status(value)
        if status:
            scan.statuses.append((depth, status))
        code = _text(value.get("classification") or value.get("error_type") or value.get("type") or value.get("code"))
        for key in _MESSAGE_KEYS:
            message = _text(value.get(key)).strip()
            if message:
                scan.messages.append((depth, message, code))
                break
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                break
            if _sensitive_key(key):
                continue
            _walk(item, scan, depth=depth + 1, field_name=str(key))
        return
    if isinstance(value, (list, tuple)):
        for item in list(value)[:_MAX_COLLECTION_ITEMS]:
            _walk(item, scan, depth=depth + 1, field_name=field_name)
        return
    if not isinstance(value, str):
        return

    text = value.strip()
    if not text:
        return
    if field_name in {"error", "detail", "cause"}:
        scan.messages.append((depth, text, ""))

    decoded = _json_value(text)
    if decoded is not None:
        _walk(decoded, scan, depth=depth + 1, field_name=field_name)
        return

    # Legacy wrappers often prepend explanatory text before ``HTTP N:``. Parse
    # the complete suffix; do not truncate before JSON decoding.
    match = _HTTP_PREFIX_RE.search(text)
    if match:
        scan.statuses.append((depth, int(match.group(1))))
        suffix = text[match.end():].strip()
        decoded = _json_value(suffix)
        if decoded is not None:
            _walk(decoded, scan, depth=depth + 1, field_name=field_name)


def _root_value(details: dict[str, Any]) -> dict[str, Any]:
    root = details.get("root_cause") or details.get("rootCause") or {}
    return root if isinstance(root, dict) else {}


def _attempts_value(details: dict[str, Any]) -> list[Any]:
    attempts = details.get("attempts")
    return list(attempts) if isinstance(attempts, (list, tuple)) else []


def _message_with_code(message: str, code: str) -> str:
    code = str(code or "").strip()
    if not code or code.lower() in {"error", "response.failed", "failed"}:
        return message
    if code.lower() in message.lower():
        return message
    return f"{code}: {message}"


@dataclass(slots=True)
class NormalizedError:
    """One protocol-independent error with transport and root cause separated."""

    message: str
    status: int
    retryable: bool
    reason: str
    summary: str = ""
    transport_status: int = 0
    upstream_status: int = 0
    code: str = ""
    root_cause: dict[str, Any] = field(default_factory=dict)
    attempts: list[Any] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    payload: str = ""
    structured: bool = False

    def __iter__(self):
        """Keep legacy ``message, retryable = read_error(...)`` callers working."""
        yield self.message
        yield self.retryable

    def exception_kwargs(self, *, protocol: str = "") -> dict[str, Any]:
        return {
            "status": self.status,
            "transport_status": self.transport_status,
            "upstream_status": self.upstream_status,
            "retryable": self.retryable,
            "protocol": protocol,
            "payload": self.payload,
            "reason": self.reason,
            "summary": self.summary,
            "root_cause": self.root_cause,
            "attempts": self.attempts,
            "details": self.details,
            "structured": self.structured,
        }

    def stream_event(self) -> StreamEvent:
        return StreamEvent(
            kind="error",
            error=self.message,
            retryable=self.retryable,
            status=self.status,
            reason=self.reason,
            summary=self.summary,
            transport_status=self.transport_status,
            upstream_status=self.upstream_status,
            root_cause=dict(self.root_cause),
            attempts=list(self.attempts),
            details=dict(self.details),
        )


def normalize_error_payload(value: Any, *, transport_status: int = 0) -> NormalizedError:
    """Parse a complete error body, then redact and bound retained detail.

    Parsing is deliberately depth-limited and collection-limited, but input text
    is never sliced before JSON decoding. Structured ``error.details`` wins over
    all text heuristics.
    """
    original = value
    parsed = value
    if isinstance(value, str):
        text = value.strip()
        match = _HTTP_PREFIX_RE.search(text)
        if match:
            transport_status = transport_status or int(match.group(1))
            decoded = _json_value(text[match.end():])
        else:
            decoded = _json_value(text)
        if decoded is not None:
            parsed = decoded

    scan = _Scan()
    _walk(parsed, scan)
    structured_entry = scan.structured[0] if scan.structured else None
    structured_details = structured_entry[1] if structured_entry else {}
    structured = structured_entry is not None
    root = _root_value(structured_details)

    summary = _text(structured_details.get("summary")).strip()
    root_message = _text(root.get("message")).strip()
    code = redact_sensitive_text(_text(root.get("code") or root.get("type"))).strip()[:200]
    raw_classification = _text(root.get("classification")).strip()
    upstream_status = _dict_status(root)

    # A top-level status accompanying a structured root cause is the gateway's
    # transport status. Root status remains the semantic status.
    if isinstance(parsed, dict) and structured:
        transport_status = transport_status or _dict_status(parsed)

    if not upstream_status and scan.statuses:
        # The deepest status is the innermost legacy root cause.
        upstream_status = max(enumerate(scan.statuses), key=lambda item: (item[1][0], item[0]))[1][1]

    deepest_message = ""
    deepest_code = ""
    if scan.messages:
        _, deepest_message, deepest_code = max(
            enumerate(scan.messages), key=lambda item: (item[1][0], item[0])
        )[1]
    if not root_message:
        root_message = deepest_message
    if not code:
        code = redact_sensitive_text(deepest_code).strip()[:200]

    if not summary and root_message:
        summary = sanitize_user_facing_text(root_message)
    if not root_message and isinstance(original, str):
        raw = redact_sensitive_text(original).strip()
        root_message = raw
        # Malformed JSON/HTML is useful for local classification but is not a
        # human summary. The exception's reason-specific fallback remains clear.
        if raw and raw[:1] not in "{[<" and not _HTTP_PREFIX_RE.search(raw):
            summary = sanitize_user_facing_text(raw)
    if summary:
        summary = sanitize_user_facing_text(summary)

    effective_status = upstream_status or int(transport_status or 0)
    fallback_message = f"HTTP {effective_status}" if effective_status else "upstream error"
    message = _message_with_code(root_message or summary or fallback_message, code)
    classification = normalize_classification(raw_classification, effective_status)
    code_reason = normalize_classification(code, effective_status)
    fallback_reason = classify_error(message or summary, effective_status)
    # Structured quota/billing codes and unmistakable billing messages are final,
    # even when an older gateway mislabeled the same payload as a transient error.
    if BILLING in {classification, code_reason, fallback_reason}:
        reason = BILLING
    else:
        reason = classification or code_reason or fallback_reason

    structured_retryable = root.get("retryable")
    if not isinstance(structured_retryable, bool):
        structured_retryable = structured_details.get("retryable")
    retry_scope = redact_sensitive_text(
        _text(root.get("retry_scope") or root.get("retryScope"))
    ).strip()[:200]
    retryable = retryable_from_contract(
        reason,
        classification=raw_classification or code,
        status=effective_status,
        retryable=structured_retryable if isinstance(structured_retryable, bool) else None,
        retry_scope=retry_scope,
        structured=structured,
    )
    normalized_root = dict(_safe_copy(root)) if root else {}
    if effective_status:
        normalized_root["status"] = effective_status
    if raw_classification:
        normalized_root["classification"] = redact_sensitive_text(raw_classification)[:200]
    elif reason:
        normalized_root["classification"] = reason
    if code:
        normalized_root["code"] = code
    if root_message:
        normalized_root["message"] = sanitize_user_facing_text(root_message, max_len=_MAX_DETAIL_STRING)
    normalized_root["retryable"] = bool(retryable)
    if retry_scope:
        normalized_root["retry_scope"] = retry_scope

    attempts = _safe_copy(_attempts_value(structured_details))
    safe_details = dict(_safe_copy(structured_details)) if structured_details else {}
    if safe_details:
        if summary:
            safe_details["summary"] = summary
        safe_details["root_cause"] = dict(normalized_root)
        safe_details.pop("rootCause", None)
        safe_details["attempts"] = list(attempts)

    return NormalizedError(
        message=sanitize_user_facing_text(message, max_len=_MAX_DETAIL_STRING),
        status=effective_status,
        retryable=bool(retryable),
        reason=reason,
        summary=summary,
        transport_status=int(transport_status or 0),
        upstream_status=int(upstream_status or 0),
        code=code,
        root_cause=normalized_root,
        attempts=list(attempts),
        details=safe_details,
        payload=_bounded_payload(parsed if parsed is not original else original),
        structured=structured,
    )


def _is_error_payload(data: dict[str, Any], event_name: str) -> bool:
    event_type = str(data.get("type") or event_name or "").strip().lower()
    if event_name.strip().lower() == "error" or event_type == "error" or event_type.endswith(".failed"):
        return True
    return data.get("error") not in (None, "", False)


def read_error(data: dict[str, Any], *, event_name: str = "") -> NormalizedError | None:
    """Return a normalized error, or ``None`` when the payload is not an error."""
    if not isinstance(data, dict) or not _is_error_payload(data, event_name):
        return None
    return normalize_error_payload(data)


def error_event(data: dict[str, Any], *, event_name: str = "") -> StreamEvent | None:
    parsed = read_error(data, event_name=event_name)
    return parsed.stream_event() if parsed is not None else None
