"""归一化事件 —— 所有 backend 把各协议流式/非流式响应翻译成这套事件。

Agent 只认这套，不感知底层协议。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ToolCall:
    """一次工具调用（协议无关）。

    id: 协议各自的 call id（回灌结果时必须原样带回）。
    name: 工具名。
    arguments: 入参 JSON 字符串（统一为字符串，由 Agent 侧解析）。
    """
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(slots=True)
class Usage:
    """Normalized, non-overlapping provider usage for one physical request.

    ``input_tokens`` is fresh (non-cached) prompt input. Cache reads and writes
    live in their dedicated fields, so their sum is the inclusive prompt size
    used for context-tier selection.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def merge(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass(slots=True)
class StreamEvent:
    """归一化流式事件。

    kind:
      - "content"    正文增量（text）
      - "reasoning"  思考增量（text）；anthropic 还可带 signature
      - "tool_call"  完整工具调用集合（tool_calls）
      - "usage"      用量（usage）
      - "finish"     结束（finish_reason: stop|tool_calls|length|...）
      - "error"      错误（error）
      - "metrics"    传输层指标（connect_ms）
      - "native_output_item" provider 原生 output item（仅支持显式回放的 backend 使用）
    """
    kind: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Provider 原生 output items。当前由 OpenAI Responses 用于无状态
    # encrypted reasoning continuation；不得放入用户可见事件或日志。
    native_output_items: list[dict] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str = ""
    error: str = ""
    retryable: bool = False
    status: int = 0
    reason: str = ""
    summary: str = ""
    transport_status: int = 0
    upstream_status: int = 0
    root_cause: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)
    details: dict = field(default_factory=dict)
    # HTTP/SSE 连接建立到响应头可用的耗时（毫秒）；kind="metrics" 时使用。
    connect_ms: int = 0
    # anthropic thinking 块的 signature（多轮工具调用需原样回传）
    signature: str = ""
