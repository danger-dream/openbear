"""本轮统计卡片测试。"""
from __future__ import annotations

from app.agent.result import RunResult
from app.llm.events import Usage
from app.turn_stats import _fmt_duration, _fmt_tokens, build_turn_stats_card


def test_fmt_duration():
    assert _fmt_duration(0) == "0s"
    assert _fmt_duration(1200) == "1.2s"
    assert _fmt_duration(13000) == "13s"
    assert _fmt_duration(133000) == "2m13s"
    assert _fmt_duration(120000) == "2m"
    assert _fmt_duration(3660000) == "1h01m"


def test_fmt_tokens():
    assert _fmt_tokens(999) == "999"
    assert _fmt_tokens(1234) == "1.2k"
    assert _fmt_tokens(1_200_000) == "1.20M"


def _result(**kw) -> RunResult:
    r = RunResult()
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_card_basic_fields():
    r = _result(
        total_time_ms=133000,
        reasoning_ms_sum=18000,
        model_calls=7, model_ok=7,
        tools_used=["Read", "Edit", "Bash"],
        usage=Usage(input_tokens=1200, output_tokens=3100,
                    cache_read_tokens=8100, cache_write_tokens=0),
        last_usage=Usage(input_tokens=1200, output_tokens=3100,
                         cache_read_tokens=8100),
    )
    card = build_turn_stats_card(r, model="openai/gpt", think_level="xhigh", cost_usd=0.0421,
                                 context_window=400000)
    assert "<blockquote expandable>" in card
    assert "本轮统计" in card
    assert "2m13s" in card             # 耗时
    assert "思考" in card and "18s" in card
    assert "调用" in card and "7" in card
    assert "工具" in card and "3" in card
    assert "Tokens" in card                       # 大写 Tokens（样式定稿）
    assert "本轮统计" in card and "<b>本轮统计</b>" in card  # 标题加粗
    assert "$0.0421" in card
    assert "🤖" in card and "openai/gpt" in card   # 模型名带图标
    assert "xhigh" in card
    # 输出 3100
    assert "3.1k" in card
    # 上下文用量行：last_usage prompt = 1200+8100 = 9.3k
    assert "上下文" in card and "9.3k" in card
    assert "400.0k" in card          # 窗口大小
    assert "%" in card               # 百分比


def test_tokens_accumulated_across_calls():
    """token 用累加 usage（本轮消耗视角，与花费同源），不是某次快照。
    场景：2 次未命中缓存的调用，累加 input=375k —— 卡片应如实显示总消耗。
    """
    r = _result(
        total_time_ms=32000, model_calls=2, model_ok=2,
        usage=Usage(input_tokens=375000, output_tokens=507, cache_read_tokens=0),
        last_usage=Usage(input_tokens=187500, output_tokens=354),
    )
    card = build_turn_stats_card(r, model="openai/gpt", cost_usd=1.88)
    assert "375.0k" in card        # 累加输入，如实显示
    assert "507" in card           # 输出 507
    # 本轮无缓存 → 不显示缓存段
    assert "缓存" not in card


def test_cache_percentage():
    r = _result(
        total_time_ms=5000, model_calls=1, model_ok=1,
        usage=Usage(input_tokens=1000, output_tokens=500, cache_read_tokens=9000),
    )
    card = build_turn_stats_card(r, model="m", cost_usd=0.0)
    # 缓存命中率 = 9000 / (1000 + 9000) = 90.0%（1 位小数，样式定稿）
    assert "90.0%" in card
    # cost=0 时不显示花费行
    assert "$" not in card


def test_no_reasoning_when_zero():
    r = _result(total_time_ms=3000, model_calls=1, model_ok=1,
                reasoning_ms_sum=0,
                usage=Usage(input_tokens=100, output_tokens=50))
    card = build_turn_stats_card(r, model="m")
    assert "思考" not in card


def test_retry_and_fail_annotated():
    r = _result(total_time_ms=9000, model_calls=5, model_ok=3,
                model_retry=2, model_fail=1,
                usage=Usage(input_tokens=100, output_tokens=50))
    card = build_turn_stats_card(r, model="m")
    assert "重试 2" in card
    assert "失败 1" in card


def test_halted_reason_shown():
    r = _result(total_time_ms=9000, model_calls=8, model_ok=8,
                halted_reason="no_progress",
                usage=Usage(input_tokens=100, output_tokens=50))
    card = build_turn_stats_card(r, model="m", halted_reason="no_progress")
    assert "重复调用" in card


def test_empty_result_safe():
    """全 0 的极端 result 不应崩（如首调即失败、无 usage）。"""
    card = build_turn_stats_card(RunResult(), model="m")
    assert "<blockquote expandable>" in card
    assert "本轮统计" in card
