"""历史压缩测试 —— safeguard 阈值 + 摘要落库。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent.compaction import (
    CompactionOutcome,
    Compactor,
    CompressionCandidate,
    _format_history_for_summary,
)
from app.agent.transcript_repair import build_visible_history_xml
from app.db.dao import MessageDAO, SummaryDAO
from app.db.engine import DB
from app.llm.base import AgentResult
from app.llm.events import ToolCall, Usage

# 一份通过质量门禁的合格结构化摘要（含全部必需小节）。
_GOOD_SUMMARY = (
    "## Primary Request and Intent\n- Test project compact request.\n"
    "## Key Technical Concepts\n- Context compaction.\n"
    "## Files and Code Sections\n- None\n"
    "## Errors and Fixes\n- None\n"
    "## Problem Solving\n- None\n"
    "## All User Messages\n- None\n"
    "## Pending Tasks\n- None\n"
    "## Current Work\n- None\n"
    "## Optional Next Step\n- None\n"
    "## Critical Identifiers\n- None\n"
)


class FakeBackend:
    protocol = "fake"

    def __init__(self, summary=_GOOD_SUMMARY):
        self._summary = summary
        self.called = False
        self.last_messages = None
        self.last_model = ""
        self.last_kwargs = {}

    async def complete(self, messages, *, model, **k) -> AgentResult:
        self.called = True
        self.last_messages = messages
        self.last_model = model
        self.last_kwargs = dict(k)
        return AgentResult(text=self._summary, usage=Usage(input_tokens=12, output_tokens=3))

    async def stream(self, *a, **k):
        raise NotImplementedError


class FailingBackend(FakeBackend):
    async def complete(self, messages, *, model, **k) -> AgentResult:
        self.called = True
        self.last_messages = messages
        self.last_model = model
        self.last_kwargs = dict(k)
        raise RuntimeError("boom")


def test_summary_history_projects_legacy_agent_telemetry_before_compaction():
    content = json.dumps({
        "taskUuid": "task-1",
        "task": {"title": "research", "modelCalls": 4, "costUsd": 1.5},
        "result": {"summary": "business result", "tokens": "domain value"},
        "events": [
            {"kind": "model_call_finished", "detail": {"inputTokens": 500}},
            {"kind": "plan_progress_complete", "detail": {"stepId": "S1"}},
        ],
    })
    history = _format_history_for_summary([
        SimpleNamespace(role="tool", name="AgentWait", content=content),
    ])

    assert "modelCalls" not in history
    assert "costUsd" not in history
    assert "model_call_finished" not in history
    assert "inputTokens" not in history
    assert "business result" in history
    assert "domain value" in history
    assert "plan_progress_complete" in history


@pytest.fixture
async def db(tmp_path):
    d = DB(str(tmp_path / "t.db"))
    await d.connect()
    yield d
    await d.close()


async def test_no_compact_under_threshold(db):
    mdao = MessageDAO(db)
    for i in range(3):
        await mdao.add(1, "user", "短消息", tokens=10)
    backend = FakeBackend()
    c = Compactor(mdao, SummaryDAO(db), backend, compression_model="m",
                  context_window=100000, ratio=0.7, keep_recent=8)
    assert await c.maybe_compact(1) is False
    assert not backend.called


async def test_maybe_compact_detail_returns_outcome_and_bool_api_stays_compatible(db):
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(20):
        await mdao.add(1, "user", f"消息{i}" * 100, tokens=1000)
    backend = FakeBackend()
    calls = []
    c = Compactor(mdao, sdao, backend, compression_model="m", compression_label="provider/m",
                  context_window=10000, ratio=0.7, keep_recent=4,
                  on_model_call=lambda detail: calls.append(detail))

    outcome = await c.maybe_compact_detail(1, prompt_tokens=9000, source="pre_model_request")

    assert isinstance(outcome, CompactionOutcome)
    assert outcome.did is True
    assert outcome.source == "pre_model_request"
    assert outcome.trigger_tokens == 9000
    assert outcome.threshold_tokens == 7000
    assert outcome.old_message_count == 16
    assert outcome.kept_message_count == 4
    assert outcome.summary_id > 0
    assert outcome.summary == _GOOD_SUMMARY.strip()
    assert outcome.summary_tokens > 0
    assert outcome.compression_model_label == "provider/m"
    assert len(calls) == 1
    assert calls[0]["model"] == "provider/m"
    assert calls[0]["usage"].input_tokens == 12
    # 旧 bool API 仍可用；压缩后剩余消息低于阈值，不会再次压。
    assert await c.maybe_compact(1) is False


async def test_compact_over_threshold(db):
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    # 造 20 条大消息，超阈值
    for i in range(20):
        await mdao.add(1, "user", f"消息{i}" * 100, tokens=1000)
    good = _GOOD_SUMMARY.replace("Test project compact request", "Updated compact summary project")
    backend = FakeBackend(summary=good)
    c = Compactor(mdao, sdao, backend, compression_model="m",
                  context_window=10000, ratio=0.7, keep_recent=4)
    did = await c.maybe_compact(1)
    assert did is True
    assert backend.called
    # 摘要落库（生成时会 strip 尾部空白）
    latest = await sdao.latest(1)
    assert latest["summary"] == good.strip()
    # 旧消息被标记 compacted，recent 只剩近 4 条
    rows = await mdao.recent(1)
    assert len(rows) == 4


async def test_compact_uses_real_prompt_snapshot(db):
    """压缩按「模型实际看到的 prompt 体积」判阈值，而非本地 estimate/累计。

    场景：留存消息本地估算只有几十 token（远低于阈值），但上游 usage 报的
    真实 prompt 已经撑爆窗口——必须以真实快照为准触发压缩。
    """
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(20):
        await mdao.add(1, "user", "短", tokens=5)  # 本地估算极小
    backend = FakeBackend()  # 默认返回合格结构化摘要
    c = Compactor(mdao, sdao, backend, compression_model="m",
                  context_window=10000, ratio=0.7, keep_recent=4)
    # 不传快照：本地估算远低于阈值，不该压
    assert await c.maybe_compact(1) is False
    assert not backend.called
    # 传真实快照 9000 > 10000*0.7=7000：必须压
    did = await c.maybe_compact(1, prompt_tokens=9000)
    assert did is True
    assert backend.called


async def test_compact_skips_tool_call_boundary(db):
    """压缩点不切在 assistant 工具调用中间。"""
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(10):
        await mdao.add(1, "user", "x" * 500, tokens=1000)
    # 让倒数第 keep+1 条恰好是带工具调用的 assistant
    await mdao.add(1, "assistant", "", tool_calls=[ToolCall(id="c", name="Bash", arguments="{}")], tokens=1000)
    for i in range(4):
        await mdao.add(1, "user", "y" * 500, tokens=1000)
    backend = FakeBackend()
    c = Compactor(mdao, sdao, backend, compression_model="m",
                  context_window=5000, ratio=0.7, keep_recent=4)
    did = await c.maybe_compact(1)
    assert did is True
    # 压缩成功且未报错即说明边界处理正常


async def test_compact_kept_window_never_starts_with_orphan_tool(db):
    """回归：压缩后保留窗口绝不能以孤儿 tool 结果打头。

    复现线上 bug：assistant(工具调用) + tool(结果) 这一对，若切点正好把 assistant
    压进摘要、tool 结果留在 kept 开头，OpenAI 报「No tool call found for call_id」、
    Anthropic 报孤儿 tool_result。修复后 kept[0] 一定不是 tool。
    """
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    # 较早一批普通消息（会被压）
    for _ in range(6):
        await mdao.add(1, "user", "x" * 500, tokens=1000)
    # 紧贴切点的工具往返：assistant 工具调用 + 两条 tool 结果
    await mdao.add(1, "assistant", "", tool_calls=[
        ToolCall(id="a", name="Bash", arguments="{}"),
        ToolCall(id="b", name="Read", arguments="{}"),
    ], tokens=1000)
    await mdao.add(1, "tool", "结果A", tool_call_id="a", name="Bash", tokens=1000)
    await mdao.add(1, "tool", "结果B", tool_call_id="b", name="Read", tokens=1000)
    # 之后的近期消息
    for _ in range(3):
        await mdao.add(1, "user", "y" * 500, tokens=1000)
    backend = FakeBackend()
    # keep=4 会让切点恰好落在「tool 结果」上，触发孤儿场景
    c = Compactor(mdao, sdao, backend, compression_model="m",
                  context_window=5000, ratio=0.7, keep_recent=4)
    did = await c.maybe_compact(1)
    assert did is True
    rows = await mdao.recent(1)
    assert rows, "压缩后不应清空保留窗口"
    assert rows[0].role != "tool", "保留窗口不能以孤儿 tool 结果打头"


async def test_force_compact_kept_window_clean_for_emergency(db):
    """应急压缩（上下文超限自救）同样保证 kept 开头不是孤儿 tool。"""
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for _ in range(8):
        await mdao.add(1, "user", "x" * 300, tokens=500)
    await mdao.add(1, "assistant", "", tool_calls=[
        ToolCall(id="a", name="Bash", arguments="{}")], tokens=500)
    await mdao.add(1, "tool", "结果", tool_call_id="a", name="Bash", tokens=500)
    for _ in range(2):
        await mdao.add(1, "user", "y" * 300, tokens=500)
    backend = FakeBackend()
    c = Compactor(mdao, sdao, backend, compression_model="m",
                  context_window=5000, ratio=0.7, keep_recent=8)
    did = await c.force_compact(1)
    assert did is True
    rows = await mdao.recent(1)
    assert rows
    assert rows[0].role != "tool"


class _SeqBackend:
    """按调用次数依次返回不同摘要，用于测质量门禁的重试。"""
    protocol = "fake"

    def __init__(self, summaries):
        self._summaries = list(summaries)
        self.calls = 0

    async def complete(self, messages, *, model, **k) -> AgentResult:
        s = self._summaries[min(self.calls, len(self._summaries) - 1)]
        self.calls += 1
        return AgentResult(text=s)

    async def stream(self, *a, **k):
        raise NotImplementedError


async def test_quality_gate_rejects_summary_without_sections(db):
    """质量门禁：摘要缺必需小节 → 多次重试仍不合格 → 放弃压缩、保留完整历史。

    复现线上「摘要连项目都没说清」的坑：宁可不压，也不写垃圾摘要。
    """
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(20):
        await mdao.add(1, "user", f"消息{i}" * 100, tokens=1000)
    backend = FakeBackend(summary="一段没有任何小节标题的随意摘要")
    c = Compactor(mdao, sdao, backend, compression_model="m",
                  context_window=10000, ratio=0.7, keep_recent=4, summary_max_retries=1)
    did = await c.maybe_compact(1)
    assert did is False           # 放弃压缩
    assert await sdao.latest(1) is None  # 没有写入垃圾摘要
    assert len(await mdao.recent(1)) == 20  # 历史原样保留，一条没动


async def test_quality_gate_retry_then_success(db):
    """质量门禁：首次摘要不合格、重试一次后合格 → 压缩成功，落库的是合格摘要。"""
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(20):
        await mdao.add(1, "user", f"消息{i}" * 100, tokens=1000)
    backend = _SeqBackend(["缺小节的烂摘要", _GOOD_SUMMARY])
    c = Compactor(mdao, sdao, backend, compression_model="m",
                  context_window=10000, ratio=0.7, keep_recent=4, summary_max_retries=1)
    did = await c.maybe_compact(1)
    assert did is True
    assert backend.calls == 2            # 确实重试了一次
    assert await sdao.latest(1) is not None


async def test_compact_absolute_trigger_tokens_overrides_keep_recent(db):
    """绝对触发阈值超过时，keep_recent 不能把压缩整个短路掉。"""
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(10):
        await mdao.add(1, "user", f"消息{i}", tokens=10)
    backend = FakeBackend()
    c = Compactor(
        mdao, sdao, backend, compression_model="m",
        context_window=400000, ratio=0.7, keep_recent=100,
        trigger_tokens=250000,
    )
    did = await c.maybe_compact(1, prompt_tokens=270048)
    assert did is True
    assert backend.called
    rows = await mdao.recent(1)
    assert 2 <= len(rows) < 10


async def test_compact_absolute_trigger_tokens_overrides_ratio(db):
    """模型级绝对触发阈值优先于全局 ratio。"""
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(20):
        await mdao.add(1, "user", f"消息{i}", tokens=10)
    backend = FakeBackend()
    c = Compactor(
        mdao, sdao, backend, compression_model="m",
        context_window=10000, ratio=0.9, keep_recent=4,
        trigger_tokens=500,
    )
    did = await c.maybe_compact(1, prompt_tokens=600)
    assert did is True
    assert backend.called


async def test_compact_falls_back_to_primary_model_when_compression_fails(db):
    """压缩模型失败后应回退主模型生成摘要，而不是直接放弃。"""
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(20):
        await mdao.add(1, "user", f"消息{i}" * 100, tokens=1000)
    compression_backend = FailingBackend()
    primary_backend = FakeBackend()
    calls = []
    c = Compactor(
        mdao, sdao, compression_backend, compression_model="bad-compression",
        context_window=10000, ratio=0.7, keep_recent=4,
        fallback_backend=primary_backend, fallback_compression_model="primary-model",
        on_model_call=lambda detail: calls.append(detail),
    )
    outcome = await c.maybe_compact_detail(1)
    assert outcome.did is True
    assert outcome.compression_model_label == "primary-model"
    assert compression_backend.called
    assert primary_backend.called
    assert primary_backend.last_model == "primary-model"
    assert [call["status"] for call in calls] == ["error", "error", "ok"]
    assert await sdao.latest(1) is not None


async def test_compact_tries_multiple_compression_models_before_primary(db):
    """多个压缩模型应按配置顺序尝试，全部失败后才回退主模型。"""
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(20):
        await mdao.add(1, "user", f"消息{i}" * 100, tokens=1000)
    first_backend = FailingBackend()
    second_backend = FakeBackend()
    primary_backend = FakeBackend()
    c = Compactor(
        mdao, sdao, first_backend, compression_model="bad-1",
        context_window=10000, ratio=0.7, keep_recent=4,
        extra_candidates=[CompressionCandidate(second_backend, "good-2", "compression", "provider/good-2")],
        fallback_backend=primary_backend, fallback_compression_model="primary-model",
    )
    outcome = await c.maybe_compact_detail(1)
    assert outcome.did is True
    assert outcome.compression_model_label == "provider/good-2"
    assert first_backend.called
    assert second_backend.called
    assert second_backend.last_model == "good-2"
    assert not primary_backend.called
    assert await sdao.latest(1) is not None


async def test_compact_uses_configured_timeout_per_model_attempt(db, monkeypatch):
    """主会话压缩的每次模型调用必须使用独立、可配置的总超时。"""
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(20):
        await mdao.add(1, "user", f"消息{i}" * 100, tokens=1000)

    timeouts = []

    class FakeTimeout:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    def fake_timeout(timeout_s):
        timeouts.append(timeout_s)
        return FakeTimeout()

    monkeypatch.setattr("app.agent.compaction.asyncio.timeout", fake_timeout)
    c = Compactor(
        mdao,
        sdao,
        FakeBackend(),
        compression_model="m",
        context_window=10000,
        ratio=0.7,
        keep_recent=4,
        summary_timeout_s=1234,
    )

    assert await c.maybe_compact(1) is True
    assert timeouts == [1234]


async def test_compact_uses_custom_prompt_and_max_tokens(db):
    """历史压缩提示词和输出 token 参数应来自配置。"""
    mdao = MessageDAO(db)
    sdao = SummaryDAO(db)
    for i in range(20):
        await mdao.add(1, "user", f"消息{i}" * 100, tokens=1000)
    backend = FakeBackend()
    prompt = "自定义压缩提示词\n已有：{existing}\n历史：{history}"
    c = Compactor(
        mdao, sdao, backend, compression_model="m",
        context_window=10000, ratio=0.7, keep_recent=4,
        prompt_template=prompt, summary_max_tokens=1234,
    )
    did = await c.maybe_compact(1)
    assert did is True
    content = backend.last_messages[0]["content"]
    assert "自定义压缩提示词" in content
    assert "历史：" in content
    assert backend.last_kwargs["max_tokens"] == 1234
    assert backend.last_kwargs["read_timeout_s"] == 1800
    assert "connect_timeout_s" not in backend.last_kwargs
    assert "idle_timeout_s" not in backend.last_kwargs


async def test_root_style_compaction_folds_all_raw_rows_and_rebuilds_visible_xml_tail(db):
    messages = MessageDAO(db)
    summaries = SummaryDAO(db)
    await messages.add(1, "user", "用户的可见问题", tokens=100)
    await messages.add(
        1, "assistant", "执行工具中",
        tool_calls=[ToolCall(id="read-1", name="Read", arguments="{}")],
        tokens=100,
    )
    await messages.add(
        1, "tool", '{"massive":"AgentWait Plan runtime payload"}',
        tool_call_id="read-1", name="Read", tokens=5000,
    )
    await messages.add(1, "assistant", "最终给用户的可见结论", tokens=100)

    compactor = Compactor(
        messages, summaries, FakeBackend(), compression_model="m",
        context_window=1000, ratio=0.7, keep_recent=2, retain_raw_recent=0,
    )
    outcome = await compactor.maybe_compact_detail(1, prompt_tokens=900)

    assert outcome.did is True
    assert outcome.old_message_count == 4
    assert outcome.kept_message_count == 0
    assert outcome.keep_recent == 2  # visible XML limit, not raw protocol rows
    assert await messages.recent(1) == []

    xml = build_visible_history_xml(await messages.recent_visible_history(1), max_messages=2)
    assert "用户的可见问题" in xml
    assert "最终给用户的可见结论" in xml
    assert "执行工具中" not in xml
    assert "AgentWait Plan runtime payload" not in xml
