"""Shared model-call retry policy and cancellable backoff waits.

The policy intentionally mirrors Claude Code's caller-side behavior: retries are
classified before this module is called, delays use exponential backoff with
jitter, and Retry-After wins when present.  This module does not impose a task
budget; it only controls recovery from one logical model call.
"""
from __future__ import annotations

import asyncio
import inspect
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

CancelCheck = Callable[[], bool | Awaitable[bool]]
RetryUpdate = Callable[[dict[str, Any]], None | Awaitable[None]]


class RetryCancelledError(RuntimeError):
    """The user cancelled a pending retry wait without cancelling prior work."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 10
    base_delay_s: float = 0.5
    max_delay_s: float = 32.0
    jitter_ratio: float = 0.25

    def delay(self, retry_number: int, *, retry_after_s: float = 0.0, random_value: float | None = None) -> float:
        """Return the wait before retry ``retry_number`` (1-based)."""
        if retry_after_s > 0:
            return float(retry_after_s)
        retry_number = max(1, int(retry_number or 1))
        base = min(
            max(0.0, float(self.base_delay_s)) * (2 ** (retry_number - 1)),
            max(0.0, float(self.max_delay_s)),
        )
        ratio = max(0.0, float(self.jitter_ratio))
        sample = random.random() if random_value is None else max(0.0, min(1.0, float(random_value)))
        return base + sample * ratio * base


def retry_wait_payload(
    *,
    retry_number: int,
    max_retries: int,
    delay_s: float,
    reason: str,
    error: str,
    summary: str = "",
    transport_status: int = 0,
    upstream_status: int = 0,
    root_cause: dict[str, Any] | None = None,
    attempts: list[Any] | None = None,
    details: dict[str, Any] | None = None,
    scope: str = "model_call",
    task_uuid: str = "",
) -> dict[str, Any]:
    from app.llm.error_payloads import sanitize_error_detail
    from app.llm.error_sanitize import redact_sensitive_text, sanitize_user_facing_text

    now_ms = int(time.time() * 1000)
    delay_ms = max(0, int(float(delay_s) * 1000))
    safe_summary = sanitize_user_facing_text(summary) if summary else ""
    return {
        "active": True,
        "scope": scope,
        "taskUuid": task_uuid,
        "attempt": max(1, int(retry_number)),
        "maxRetries": max(0, int(max_retries)),
        "delayMs": delay_ms,
        "retryAtMs": now_ms + delay_ms,
        "reason": str(reason or "upstream_error"),
        "summary": safe_summary,
        "error": redact_sensitive_text(str(error or ""))[:1000],
        "transportStatus": int(transport_status or 0),
        "upstreamStatus": int(upstream_status or 0),
        "rootCause": sanitize_error_detail(root_cause or {}),
        "attempts": sanitize_error_detail(attempts or []),
        "details": sanitize_error_detail(details or {}),
        "cancelable": True,
    }


async def _call_optional(callback: Callable[..., Any] | None, *args: Any) -> Any:
    if callback is None:
        return None
    value = callback(*args)
    if inspect.isawaitable(value):
        return await value
    return value


async def wait_for_retry(
    delay_s: float,
    *,
    state: dict[str, Any],
    cancel_check: CancelCheck | None = None,
    on_update: RetryUpdate | None = None,
    poll_interval_s: float = 0.25,
) -> None:
    """Wait for a retry while publishing one structured countdown deadline.

    The browser calculates the visible countdown from ``retryAtMs``; the server
    only polls for cancellation, avoiding one durable frame per second.
    """
    await _call_optional(on_update, dict(state))
    deadline = time.monotonic() + max(0.0, float(delay_s))
    terminal_status = "resumed"
    try:
        while True:
            if bool(await _call_optional(cancel_check)):
                raise RetryCancelledError("model retry cancelled by user")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(max(0.05, float(poll_interval_s)), remaining))
    except (RetryCancelledError, asyncio.CancelledError):
        terminal_status = "cancelled"
        raise
    except BaseException:
        terminal_status = "failed"
        raise
    finally:
        cleared = dict(state)
        cleared.update({
            "active": False,
            "cancelable": False,
            "retryAtMs": 0,
            "delayMs": 0,
            "status": terminal_status,
            "terminal": True,
        })
        await _call_optional(on_update, cleared)
