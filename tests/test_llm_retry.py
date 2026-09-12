import asyncio

import pytest

from app.llm.retry import RetryCancelledError, RetryPolicy, retry_wait_payload, wait_for_retry


def test_retry_policy_integer_staircase_preserves_count_and_retry_after():
    policy = RetryPolicy()
    assert policy.max_retries == 10
    assert [policy.delay(i) for i in range(1, 11)] == [3, 5, 10, 30, 180, 300, 600, 600, 600, 600]
    assert policy.delay(2, retry_after_s=17.2) == 18
    assert policy.delay(7, retry_after_s=17) == 600  # don't retry earlier than our backoff
    assert policy.delay(1, retry_after_s=7200) == 7200
    assert policy.delay(1000000) == 600  # bounded indexing, no exponent overflow


def test_retry_policy_custom_values_are_integer_and_deterministic():
    policy = RetryPolicy(base_delay_s=6, max_delay_s=300, jitter_ratio=0.9)
    assert [policy.delay(i, random_value=1) for i in range(1, 8)] == [6, 10, 20, 60, 300, 300, 300]
    assert policy.delay(2, random_value=0) == policy.delay(2, random_value=1)
    assert RetryPolicy(base_delay_s=2.1).delay(1) == 3
    assert RetryPolicy(base_delay_s=0).delay(8) == 0


def test_legacy_retry_default_profile_migrates_but_custom_limits_do_not():
    from app.config import AgentConfig
    for values in ({}, {"retryBackoffS": 0.5}, {"retry_backoff_s": 0.5, "retry_max_delay_s": 32, "retry_jitter_ratio": 0.25}):
        config = AgentConfig.model_validate({**values, "maxRetries": 7})
        assert config.max_retries == 7
        assert config.retry_backoff_s == 3 and config.retry_max_delay_s == 600
        assert "retryJitterRatio" not in config.model_dump(by_alias=True)
    custom = AgentConfig.model_validate({"retryBackoffS": 2, "retryMaxDelayS": 40, "maxRetries": 0})
    assert custom.retry_backoff_s == 2 and custom.retry_max_delay_s == 40
    assert custom.max_retries == 0


def test_retry_time_labels_are_seconds_or_minutes_without_decimals():
    from app.llm.retry import retry_delay_label
    assert retry_delay_label(3) == "3 秒"
    assert retry_delay_label(2.1) == "3 秒"
    assert retry_delay_label(180) == "3 分钟"
    assert retry_delay_label(181) == "3 分钟 1 秒"


@pytest.mark.asyncio
async def test_retry_wait_publishes_deadline_and_can_be_cancelled():
    updates = []
    cancelled = False

    async def cancel_check():
        nonlocal cancelled
        if cancelled:
            return True
        cancelled = True
        return False

    state = retry_wait_payload(
        retry_number=2,
        max_retries=10,
        delay_s=5,
        reason="rate_limit",
        error='HTTP 503: {"error":{"message":"busy"}}',
        summary="请求频率过高，请稍后重试",
        transport_status=503,
        upstream_status=429,
        root_cause={"status": 429, "classification": "rate_limit", "retryable": True},
        attempts=[{"status": 429}],
        details={"summary": "请求频率过高，请稍后重试"},
        task_uuid="task-1",
    )
    with pytest.raises(RetryCancelledError):
        await wait_for_retry(
            5,
            state=state,
            cancel_check=cancel_check,
            on_update=lambda payload: updates.append(payload),
            poll_interval_s=0.01,
        )

    assert updates[0]["active"] is True
    assert updates[0]["attempt"] == 2
    assert updates[0]["taskUuid"] == "task-1"
    assert updates[0]["summary"] == "请求频率过高，请稍后重试"
    assert updates[0]["transportStatus"] == 503
    assert updates[0]["upstreamStatus"] == 429
    assert updates[0]["rootCause"]["classification"] == "rate_limit"
    assert updates[0]["attempts"] == [{"status": 429}]
    assert updates[-1]["active"] is False
    assert updates[-1]["retryAtMs"] == 0
