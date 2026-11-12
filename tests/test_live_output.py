from __future__ import annotations

import pytest

from app.agent.live_output import LiveOutput
from app.agent.result import RunResult
from app.llm.events import StreamEvent, Usage
from app.web_console.chat_state import WebAdminChatStateMixin


def _content(text: str) -> StreamEvent:
    return StreamEvent("content", text=text)


def test_speed_counts_streamed_text_over_the_recent_window_and_stops_when_silent():
    live = LiveOutput()
    live.start_call()
    for second in range(4):
        live.observe(_content("abcd" * 10), now=100.0 + second)  # 10 tokens per second
    assert live.tokens_per_second(now=103.0) == pytest.approx(40 / 3.0)
    assert live.tokens_per_second(now=104.0) == pytest.approx(30 / 3.0)
    assert live.tokens_per_second(now=104.6) == 0.0


def test_a_new_burst_is_not_diluted_by_the_silence_before_it():
    live = LiveOutput()
    live.observe(_content("你好世界"), now=50.0)
    assert live.tokens_per_second(now=50.2) == pytest.approx(4 / 0.5)
    live.observe(_content("你好世界"), now=51.0)
    assert live.tokens_per_second(now=51.0) == pytest.approx(8 / 1.0)


def test_tool_argument_bytes_count_per_call_and_restart_with_the_next_call():
    live = LiveOutput()
    live.start_call()
    live.observe(StreamEvent("tool_input", details={"receivedBytes": 350}), now=10.0)
    live.observe(StreamEvent("tool_input", details={"receivedBytes": 700}), now=11.0)
    assert live.in_flight_usage()["outputTokens"] == 200
    live.end_call()
    live.start_call()
    live.observe(StreamEvent("tool_input", details={"receivedBytes": 70}), now=12.0)
    assert live.in_flight_usage()["outputTokens"] == 20


def test_in_flight_usage_reports_the_prompt_and_estimated_output_until_the_call_settles():
    live = LiveOutput()
    assert live.in_flight_usage() is None
    live.start_call()
    assert live.in_flight_usage() == {"inputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0, "outputTokens": 0,
                                      "promptEstimated": True}
    live.observe(StreamEvent("usage_preview", usage=Usage(input_tokens=2000, cache_read_tokens=478000, cache_write_tokens=300)))
    live.observe(StreamEvent("reasoning", text="想" * 40), now=1.0)
    assert live.in_flight_usage() == {"inputTokens": 2000, "cacheReadTokens": 478000, "cacheWriteTokens": 300, "outputTokens": 40,
                                      "promptEstimated": False}
    live.end_call()
    assert live.in_flight_usage() is None


def test_without_a_reported_prompt_the_context_estimate_is_split_by_the_latest_cache_share():
    live = LiveOutput()
    live.note_prompt(Usage(input_tokens=1000, cache_read_tokens=99000))  # the previous request: 99% cached
    live.start_call(500_000)
    live.observe(StreamEvent("usage_preview", usage=Usage()))  # an empty preview is no report
    assert live.in_flight_usage() == {"inputTokens": 5000, "cacheReadTokens": 495000, "cacheWriteTokens": 0, "outputTokens": 0,
                                      "promptEstimated": True}
    live.end_call(Usage(input_tokens=250_000, cache_read_tokens=250_000))
    live.start_call(600_000)
    assert live.in_flight_usage()["cacheReadTokens"] == 300_000, "the settled call's actual share replaces the old one"
    live.end_call(None)
    live.start_call(100)
    assert live.in_flight_usage()["cacheReadTokens"] == 50, "a call without prompt usage keeps the last known share"


def test_live_run_stats_carry_speed_in_flight_usage_and_server_elapsed_time():
    result = RunResult()
    result.live_output.start_call()
    result.live_output.observe(StreamEvent("usage_preview", usage=Usage(input_tokens=10, cache_read_tokens=90)))
    live = WebAdminChatStateMixin._run_stats_json(None, result, cost_usd=0, model="fake", think_level="off",
                                                  context_window=1000, live=True, elapsed_ms=65_000)
    assert live["elapsedMs"] == 65_000
    assert live["inflightUsage"]["cacheReadTokens"] == 90
    assert live["liveTps"] == 0.0
    final = WebAdminChatStateMixin._run_stats_json(None, result, cost_usd=0, model="fake", think_level="off",
                                                   context_window=1000)
    assert not {"elapsedMs", "inflightUsage", "liveTps"} & set(final)
