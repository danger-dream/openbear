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
        ("balance_exhausted", 429, BILLING),
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


_DRIVE_UPLOAD_FAILED = (
    '{"type": "response.failed", "response": {"id": "resp_1", "object": "response", '
    '"status": "failed", "error": {"code": "server_error", '
    '"message": "Internal server error: AI Studio DriveUpload 返回 HTTP 502: \n \n'
)


def test_nested_failed_response_with_raw_newlines_reaches_its_502():
    inner = _DRIVE_UPLOAD_FAILED + '<html><title>502 Bad Gateway</title></html>"}}}'
    envelope = {"error": {"message": inner, "type": "invalid_request_error"}}

    normalized = normalize_error_payload(envelope, transport_status=400)
    exc = OpenBearLLMError(normalized.message, **normalized.exception_kwargs(protocol="chat"))

    assert normalized.upstream_status == normalized.status == 502
    assert normalized.reason == SERVER_ERROR and normalized.retryable is True
    assert "response.failed" not in exc.user_message()
    assert "AI Studio DriveUpload 返回 HTTP 502" in exc.user_message()


def test_truncated_nested_json_under_invalid_request_keeps_its_502_and_no_raw_summary():
    envelope = {"error": {"message": _DRIVE_UPLOAD_FAILED + "…", "type": "invalid_request_error"}}

    normalized = normalize_error_payload(envelope, transport_status=400)
    exc = OpenBearLLMError(normalized.message, **normalized.exception_kwargs(protocol="chat"))

    assert normalized.upstream_status == normalized.status == 502
    assert normalized.reason == SERVER_ERROR and normalized.retryable is True
    assert normalized.summary == ""
    assert exc.user_message() == "❌ 上游服务异常,稍后重试～"


def test_invalid_request_with_a_502_only_in_its_text_stays_final():
    payload = {"type": "error", "error": {"type": "invalid_request_error",
                                          "message": "messages.502.content.0.text: field required"}}

    normalized = normalize_error_payload(payload)

    assert normalized.status == 0
    assert normalized.reason == FORMAT and normalized.retryable is False


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


@pytest.mark.parametrize("error", [
    {"type": "invalid_request_error", "message": "Invalid schema for function"},
    None,
])
def test_echoed_request_tool_schema_is_not_read_as_error_message(error):
    tool = {"type": "function", "name": "ConversationGroup", "parameters": {
        "type": "object", "properties": {"group": {"type": "object", "properties": {
            "workspaceDir": {"type": "string",
                             "description": "absolute path; omit for a new isolated group directory"},
        }}}}}
    payload = {"type": "response.failed", "response": {
        "status": "failed", "error": error, "instructions": "system", "tools": [tool],
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "output": [],
    }}

    event = error_event(payload, event_name="response.failed")

    assert event is not None
    assert "isolated group directory" not in event.error
    if error:
        assert event.error == "invalid_request_error: Invalid schema for function"


def test_gemini_malformed_function_call_is_a_retryable_server_error():
    payload = {"type": "response.failed", "response": {"status": "failed", "error": {
        "code": "gemini_malformed_function_call",
        "message": "Gemini generation ended with MALFORMED_FUNCTION_CALL: Function call is empty - no input to parse.",
    }, "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]}]}}

    event = error_event(payload, event_name="response.failed")
    exc = OpenBearLLMError.from_stream_event(event, protocol="responses")

    assert event.reason == SERVER_ERROR and event.retryable is True
    assert exc.retryable is True and "MALFORMED_FUNCTION_CALL" in exc.user_message()
    assert classify_error("Gemini finishReason MALFORMED_FUNCTION_CALL", 400) == SERVER_ERROR
    assert classify_error("Invalid value for 'tool_choice'", 400) == FORMAT


def test_gemini_response_without_finish_reason_is_a_retryable_server_error():
    payload = {"type": "response.failed", "response": {"status": "failed", "error": {
        "code": "upstream_stream_incomplete",
        "message": "Gemini response ended without finishReason",
    }, "output": []}}

    event = error_event(payload, event_name="response.failed")
    exc = OpenBearLLMError.from_stream_event(event, protocol="responses")

    assert event is not None
    assert event.reason == SERVER_ERROR and event.retryable is True
    assert exc.reason == SERVER_ERROR and exc.retryable is True
    assert "Gemini response ended without finishReason" in exc.user_message()
    assert classify_error("Gemini response ended without finishReason") == SERVER_ERROR
    assert classify_error("Gemini response ended without finishReason", 400) == SERVER_ERROR


@pytest.mark.parametrize("message", [
    "Gemini generation ended with MALFORMED_FUNCTION_CALL: Malformed function call: thought\n"
    "call:default_api:Bash{command: docker logs ...}",
    "gemini_unexpected_tool_call: Gemini generation ended with UNEXPECTED_TOOL_CALL",
    "Gemini response ended without finishReason",
])
@pytest.mark.parametrize("status", [200, 400])
def test_gemini_sampling_failure_in_invalid_request_envelope_is_retryable(message, status):
    # Parrot's Chat/Anthropic error frame types a request-scoped 400 as invalid_request_error.
    payload = {"error": {"message": message, "type": "invalid_request_error", "code": None, "param": None}}

    normalized = normalize_error_payload(payload, transport_status=status)
    exc = OpenBearLLMError(normalized.message, **normalized.exception_kwargs(protocol="chat"))

    assert normalized.reason == SERVER_ERROR and normalized.retryable is True
    assert exc.reason == SERVER_ERROR and exc.retryable is True


def test_gemini_sampling_failure_keeps_the_structured_retry_contract():
    def payload(*, retryable: bool, retry_scope: str) -> dict:
        body = _structured_payload(
            status=400, classification="invalid_request",
            summary="Upstream 400 invalid_request: Gemini generation ended with MALFORMED_FUNCTION_CALL",
            message="Gemini generation ended with MALFORMED_FUNCTION_CALL: Malformed function call: thought",
            retryable=retryable, retry_scope=retry_scope, code="gemini_malformed_function_call",
        )
        body["error"]["type"] = "invalid_request_error"
        return body

    granted = normalize_error_payload(payload(retryable=True, retry_scope="request"), transport_status=400)
    final = normalize_error_payload(payload(retryable=False, retry_scope="request"), transport_status=400)

    assert granted.reason == SERVER_ERROR and granted.retryable is True
    assert granted.code == "gemini_malformed_function_call"
    assert final.retryable is False


def test_ordinary_invalid_request_envelope_stays_a_final_format_error():
    payload = {"error": {"message": "Invalid JSON payload: unknown field 'foo'",
                         "type": "invalid_request_error", "code": None}}

    normalized = normalize_error_payload(payload, transport_status=400)

    assert normalized.reason == FORMAT and normalized.retryable is False


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


def test_parrot_workbuddy_credits_exhausted_is_billing_not_a_rate_limit():
    # Parrot's actual 402 for WorkBuddy 14018 (root status 429), as received on 2026-11-05.
    payload = {"error": {"message": "Upstream 429 balance_exhausted: WorkBuddy account quota exhausted", "type": "server_error",
                         "code": "14018", "param": None, "details": {
                             "summary": "Upstream 429 balance_exhausted: WorkBuddy account quota exhausted",
                             "root_cause": {"status": 429, "classification": "balance_exhausted", "code": "14018",
                                            "message": "WorkBuddy account quota exhausted", "retryable": False, "retry_scope": "none"}}}}
    normalized = normalize_error_payload(payload, transport_status=402)
    assert normalized.reason == BILLING and normalized.retryable is False
