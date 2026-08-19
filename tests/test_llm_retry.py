import asyncio

import pytest

from app.llm.retry import RetryCancelledError, RetryPolicy, retry_wait_payload, wait_for_retry


def test_retry_policy_matches_exponential_backoff_and_retry_after():
    policy = RetryPolicy(max_retries=10, base_delay_s=0.5, max_delay_s=32, jitter_ratio=0.25)
    assert policy.delay(1, random_value=0) == 0.5
    assert policy.delay(2, random_value=0) == 1.0
    assert policy.delay(7, random_value=0) == 32.0
    assert policy.delay(8, random_value=1) == 40.0
    assert policy.delay(2, retry_after_s=17, random_value=0) == 17.0


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
