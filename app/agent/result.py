"""Agent 运行结果。"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.llm.events import Usage


@dataclass(slots=True)
class RunResult:
    text: str = ""
    reasoning: str = ""
    rounds: int = 0
    tools_used: list[str] = field(default_factory=list)
    # 专家/Rath 子任务在主 Agent 工具调用里完成。它们的用量独立计价，
    # 但本轮统计展示时必须合并；只在 Agent/AgentMessage 返回终态任务时入账一次。
    expert_tasks: int = 0
    expert_model_calls: int = 0
    expert_tool_calls: int = 0
    expert_usage: Usage = field(default_factory=Usage)
    expert_cost_usd: float = 0.0
    # Long-running child Agent wall duration.  Used only for user-facing
    # combined turn stats so a detached Agent that runs after the controller
    # turn does not show e.g. "14s · $0.27".
    expert_duration_ms: int = 0
    expert_accounted_task_uuids: set[str] = field(default_factory=set)
    usage: Usage = field(default_factory=Usage)          # 跨轮累加：本轮 Agent 所有 API 调用之和（用于 token 统计）
    # Each physical call selects a tier independently. Web accounting appends the
    # committed per-call amount here instead of re-pricing the aggregate usage.
    controller_cost_usd: float = 0.0
    last_usage: Usage = field(default_factory=Usage)     # 最后一次 API 调用的快照：prompt 体积 = 模型实际看到的整个上下文（用于上下文占用/压缩判定）
    last_prompt_usage_reported: bool = False  # 最近一次成功物理调用是否由渠道明确返回 prompt usage
    stopped: bool = False          # 被停止或新消息打断
    halted_reason: str = ""        # 软约束触发（token/时长/打转）
    steered: int = 0               # 本轮被运行中插话注入的消息数（steering）
    connect_ms: int = 0             # 兼容字段：本轮首次有值的模型请求连接耗时
    first_token_ms: int = 0         # 本轮首次模型输出/工具调用耗时
    total_time_ms: int = 0          # 本轮 Agent 总耗时
    start_monotonic: float = 0.0    # 本轮 run 起点(time.monotonic())。被停止时,
                                    # loop 来不及设 total_time_ms,handler 兜底用它现算耗时。
    last_call_connect_ms: int = 0   # 最近一次成功模型 API 调用连接耗时
    last_call_first_token_ms: int = 0
    last_call_time_ms: int = 0
    # —— 模型调用计数（一次 Agent.run 内可能多次调用上游：多轮工具 + 重试）——
    model_calls: int = 0            # 模型调用次数（每次发起 stream 计一次，含重试）
    model_ok: int = 0               # 成功完成的调用次数
    model_retry: int = 0            # 重试次数
    model_fail: int = 0             # 终态失败次数
    # —— 每次成功调用的指标累加（求会话平均用，分母 = model_ok）——
    connect_ms_sum: int = 0
    first_token_ms_sum: int = 0
    call_time_ms_sum: int = 0       # 单次调用耗时累加（注意：非整轮 total_time_ms）
    output_tokens_sum: int = 0
    peak_tps: float = 0.0           # 本轮内单次模型 API 调用最高输出速率
    min_tps: float = 0.0            # 本轮内单次模型 API 调用最低输出速率（仅统计有输出的调用）
    # 本轮思考(reasoning)总时长(ms)：累加每次 API 调用「首个 reasoning 增量 → 首个
    # 正文 content 增量」之间的间隔。思考内容虽加密，但时间可测。多轮工具调用会有
    # 多段思考，全部累加。think=off / 模型不吐 reasoning 的调用记 0。
    reasoning_ms_sum: int = 0
