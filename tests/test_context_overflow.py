"""上下文超限错误识别测试。"""
from __future__ import annotations

from app.agent.context_overflow import is_context_overflow_error


def test_detects_common_english_overflow():
    samples = [
        "This model's maximum context length is 128000 tokens",
        "prompt is too long: 200000 tokens > 128000",
        "context length exceeded",
        "Error: context_window_exceeded",
        "input is too long for this model",
        "request_too_large",
        "400 request (66202 tokens) exceeds the available context size (65536 tokens)",
        "Unhandled stop reason: model_context_window_exceeded",
        # 回归：parrot OpenAI 兼容上游的真实 400（下划线 code + input exceeds context window）
        'HTTP 400: {"error":{"message":"{\"type\": \"invalid_request_error\", '
        '\"code\": \"context_length_exceeded\", \"message\": \"Your input exceeds '
        'the context window of this model. Please adjust your input and try again.\", '
        '\"param\": \"input\"}","type":"invalid_request_error"}}',
        "code: context_length_exceeded",
        "Your input exceeds the context window of this model",
    ]
    for s in samples:
        assert is_context_overflow_error(s), f"应识别为超限: {s!r}"


def test_detects_chinese_proxy_overflow():
    for s in ["上下文过长，请压缩上下文", "超出最大上下文长度", "上下文太长了"]:
        assert is_context_overflow_error(s), f"应识别中文超限: {s!r}"


def test_does_not_misfire_on_rate_limit():
    # TPM 限流不是上下文超限
    assert not is_context_overflow_error("rate limit: 90000 tokens per minute exceeded")
    assert not is_context_overflow_error("429 too many requests per min")


def test_does_not_misfire_on_unrelated():
    for s in ["connection reset", "500 internal server error", "invalid api key", "", None]:
        assert not is_context_overflow_error(s)
