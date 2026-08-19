from __future__ import annotations

import json

import httpx
import pytest

from app.llm.base import OpenBearLLMError
from app.llm.client import HTTPClient
from app.llm.error_classify import (
    AUTH,
    AUTH_PERMANENT,
    BILLING,
    FORMAT,
    MODEL_NOT_FOUND,
    RATE_LIMIT,
    SERVER_ERROR,
    UNKNOWN,
    classify_error,
    normalize_classification,
)
from app.llm.error_payloads import error_event, normalize_error_payload


def _structured_payload(
    *,
    status: int | None,
    classification: str,
    summary: str,
    message: str,
    retryable: bool,
    retry_scope: str = "account",
    code: str | None = None,
) -> dict:
    """Match Parrot's OpenAI/Responses terminal JSON error envelope."""
    root_cause = {
        "status": status,
        "classification": classification,
        "code": code or classification,
        "message": message,
        "retryable": retryable,
        "retry_scope": retry_scope,
    }
    attempt = {"attempt": 1, **root_cause}
    return {
        "error": {
            "message": summary,
            "type": "api_error",
            "code": code or classification,
            "param": None,
            "details": {
                "summary": summary,
                "root_cause": root_cause,
                "attempts": [attempt],
            },
        },
    }


@pytest.mark.parametrize(
    ("classification", "status", "expected"),
    [
        ("authentication_error", 401, AUTH),
        ("permission_error", 403, AUTH_PERMANENT),
        ("rate_limit_error", 429, RATE_LIMIT),
        ("upstream_server_error", 503, SERVER_ERROR),
        ("upstream_http_error", 400, FORMAT),
        ("upstream_http_error", 404, MODEL_NOT_FOUND),
        ("transport_error", 0, SERVER_ERROR),
        ("quota", 429, BILLING),
        ("quota_exhausted", 429, BILLING),
        ("billing", 403, BILLING),
        ("insufficient_quota", 429, BILLING),
        ("monthly spending limit", 403, BILLING),
        ("credits exhausted", 403, BILLING),
    ],
)
def test_actual_parrot_classifications_map_without_message_guessing(
    classification, status, expected,
):
    assert normalize_classification(classification, status) == expected


def test_structured_outer_503_preserves_root_429_and_human_summary_after_large_prefix():
    payload = _structured_payload(
        status=429,
        classification="rate_limit_error",
        summary="当前账户请求过于频繁，请稍后再试",
        message="Too many requests",
        retryable=True,
    )
    # Regression: the previous HTTP path sliced at 500 before parsing.
    payload = {"padding": "x" * 2000, **payload}
    normalized = normalize_error_payload(json.dumps(payload), transport_status=503)

    assert normalized.transport_status == 503
    assert normalized.upstream_status == normalized.status == 429
    assert normalized.reason == RATE_LIMIT
    assert normalized.retryable is True
    assert normalized.summary == "当前账户请求过于频繁，请稍后再试"
    assert normalized.root_cause["retry_scope"] == "account"
    assert normalized.attempts[0]["classification"] == "rate_limit_error"
    assert normalized.details["summary"] == normalized.summary
    assert len(normalized.payload) <= 4001

    exc = OpenBearLLMError(normalized.message, **normalized.exception_kwargs(protocol="chat"))
    assert exc.user_message() == "当前账户请求过于频繁，请稍后再试"
    assert "HTTP 503" not in exc.user_message()


def test_actual_parrot_legacy_candidate_quota_is_billing_and_terminal():
    payload = _structured_payload(
        status=429,
        classification="quota_exhausted",
        summary="本月消费额度已用完，请检查账户额度",
        message="Monthly spending limit reached; used all available credits",
        retryable=True,
        retry_scope="next_candidate",
        code="insufficient_quota",
    )
    normalized = normalize_error_payload(payload, transport_status=503)

    assert normalized.status == normalized.upstream_status == 429
    assert normalized.transport_status == 503
    assert normalized.reason == BILLING
    assert normalized.retryable is False
    assert normalized.summary.startswith("本月消费额度")
    assert normalized.root_cause["classification"] == "quota_exhausted"
    assert normalized.root_cause["retry_scope"] == "next_candidate"
    assert normalized.details["root_cause"] == normalized.root_cause
    assert normalized.details["attempts"] == normalized.attempts
    exc = OpenBearLLMError(normalized.message, **normalized.exception_kwargs())
    assert exc.reason == BILLING
    assert exc.retryable is False
    assert classify_error("monthly spending limit reached", 403) == BILLING
    assert classify_error("used all available credits", 403) == BILLING
    assert classify_error("insufficient_quota", 403) == BILLING


def test_root_retryable_false_and_none_remain_final_for_transient_classification():
    payload = _structured_payload(
        status=429,
        classification="rate_limit_error",
        summary="上游已结束本次请求",
        message="Too many requests",
        retryable=False,
        retry_scope="none",
    )

    normalized = normalize_error_payload(payload, transport_status=503)

    assert normalized.reason == RATE_LIMIT
    assert normalized.retryable is False
    assert normalized.root_cause["retryable"] is False
    assert normalized.root_cause["retry_scope"] == "none"


def test_actual_transport_error_is_generic_transient_and_preserves_details():
    payload = _structured_payload(
        status=None,
        classification="transport_error",
        summary="Upstream transport failed after candidate attempts",
        message="Connection reset",
        retryable=True,
        retry_scope="same_candidate",
    )

    normalized = normalize_error_payload(payload, transport_status=503)

    assert normalized.status == normalized.transport_status == 503
    assert normalized.upstream_status == 0
    assert normalized.reason == SERVER_ERROR
    assert normalized.retryable is True
    assert normalized.summary == "Upstream transport failed after candidate attempts"
    assert normalized.root_cause["classification"] == "transport_error"
    assert normalized.details["attempts"][0]["retry_scope"] == "same_candidate"


def test_internal_candidate_scope_cannot_make_permission_failure_retryable():
    for retry_scope in ("same_candidate", "next_candidate", "channel", "account"):
        payload = _structured_payload(
            status=403,
            classification="permission_error",
            summary="Upstream denied this candidate",
            message="Forbidden",
            retryable=True,
            retry_scope=retry_scope,
        )
        normalized = normalize_error_payload(payload, transport_status=503)
        assert normalized.reason == AUTH_PERMANENT
        assert normalized.retryable is False


@pytest.mark.parametrize("retry_scope", ["client", "request", "client_request"])
def test_explicit_client_request_scope_can_retry_an_otherwise_unknown_contract(retry_scope):
    payload = _structured_payload(
        status=409,
        classification="upstream_http_error",
        summary="Client may repeat the complete request",
        message="Conflict",
        retryable=True,
        retry_scope=retry_scope,
    )

    normalized = normalize_error_payload(payload, transport_status=503)

    assert normalized.reason == UNKNOWN
    assert normalized.retryable is True


def test_legacy_nested_http_strings_reach_innermost_429_message():
    inner = "HTTP 429: " + json.dumps({
        "error": {"type": "rate_limit_error", "message": "Too many requests for this account"}
    })
    middle = "HTTP 503: " + json.dumps({"error": {"message": inner}})
    body = json.dumps({"error": {"message": middle}})

    normalized = normalize_error_payload(body, transport_status=503)

    assert normalized.transport_status == 503
    assert normalized.upstream_status == normalized.status == 429
    assert normalized.reason == RATE_LIMIT
    assert normalized.retryable is True
    assert normalized.summary == "Too many requests for this account"
    assert "HTTP 503" not in normalized.summary
    assert "{" not in normalized.summary


def test_legacy_nested_http_403_spending_limit_is_billing_not_outer_server_error():
    inner = "HTTP 403: " + json.dumps({
        "error": {"message": "Monthly spending limit reached; credits exhausted"}
    })
    body = json.dumps({"error": {"message": "HTTP 503: " + json.dumps({"error": {"message": inner}})}})

    normalized = normalize_error_payload(body, transport_status=503)

    assert normalized.transport_status == 503
    assert normalized.upstream_status == normalized.status == 403
    assert normalized.reason == BILLING
    assert normalized.retryable is False
    assert normalized.summary == "Monthly spending limit reached; credits exhausted"


def test_malformed_body_falls_back_without_exposing_authorization_or_credentials():
    body = 'HTTP 503: {"error":{"message":"bad", "Authorization":"Bearer top-secret-token"'
    normalized = normalize_error_payload(body, transport_status=503)
    exc = OpenBearLLMError(normalized.message, **normalized.exception_kwargs())

    assert normalized.status == 503
    assert normalized.reason == "server_error"
    combined = " ".join([normalized.message, normalized.summary, normalized.payload, exc.user_message()])
    assert "top-secret-token" not in combined
    assert "Authorization" not in combined
    assert exc.user_message() == "❌ 上游服务异常,稍后重试～"


def test_sse_error_event_propagates_normalized_status_reason_summary_and_details():
    payload = _structured_payload(
        status=429,
        classification="rate_limit_error",
        summary="请求频率过高，正在等待重试",
        message="rate limit exceeded",
        retryable=True,
    )
    payload["status"] = 503

    event = error_event(payload, event_name="error")

    assert event is not None
    assert event.status == event.upstream_status == 429
    assert event.transport_status == 503
    assert event.reason == RATE_LIMIT
    assert event.summary == "请求频率过高，正在等待重试"
    assert event.details["root_cause"]["status"] == 429
    exc = OpenBearLLMError.from_stream_event(event, protocol="responses")
    assert exc.reason == RATE_LIMIT
    assert exc.user_message() == event.summary


async def _client_with_handler(handler) -> HTTPClient:
    client = HTTPClient()
    await client.raw.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_http_json_non_2xx_propagates_transport_and_structured_root():
    payload = _structured_payload(
        status=403,
        classification="billing",
        summary="可用额度已耗尽",
        message="credits exhausted",
        retryable=False,
    )
    client = await _client_with_handler(lambda request: httpx.Response(503, json=payload))
    try:
        with pytest.raises(OpenBearLLMError) as raised:
            await client.post_json("https://example.test/v1", {}, {}, protocol="chat")
        exc = raised.value
        assert exc.transport_status == 503
        assert exc.upstream_status == exc.status == 403
        assert exc.reason == BILLING
        assert exc.retryable is False
        assert exc.user_message() == "可用额度已耗尽"
    finally:
        await client.close()


async def test_http_sse_non_2xx_propagates_transport_and_structured_root():
    payload = _structured_payload(
        status=429,
        classification="rate_limit_error",
        summary="触发上游限流，请稍后重试",
        message="rate limit exceeded",
        retryable=True,
    )
    client = await _client_with_handler(lambda request: httpx.Response(503, json=payload))
    try:
        with pytest.raises(OpenBearLLMError) as raised:
            async for _ in client.post_sse("https://example.test/v1", {}, {}, protocol="responses"):
                pass
        exc = raised.value
        assert exc.transport_status == 503
        assert exc.upstream_status == exc.status == 429
        assert exc.reason == RATE_LIMIT
        assert exc.retryable is True
        assert exc.summary == "触发上游限流，请稍后重试"
    finally:
        await client.close()
