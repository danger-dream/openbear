"""控制中心可编辑设置规格。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.agent.compaction import DEFAULT_SUMMARY_PROMPT
from app.config import DEFAULT_MEMORY_REMINDER_PROMPT
from app.rath.prompts import PROMPT_SPECS

SettingKind = Literal["bool", "int", "float", "str", "multi"]
Effect = Literal["立即生效", "下一轮生效", "需要重启"]


@dataclass(frozen=True, slots=True)
class SettingSpec:
    path: str
    title: str
    desc: str
    kind: SettingKind
    group: str
    effect: Effect
    min_value: float | None = None
    max_value: float | None = None
    unit: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    editor: Literal["default", "prompt"] = "default"
    variables: tuple[str, ...] = ()
    default_value: str = ""

    def parse(self, raw: str) -> bool | int | float | str | list[str]:
        text = (raw or "").strip()
        if self.kind == "bool":
            key = text.lower()
            if key in {"1", "true", "on", "yes", "y", "开", "开启", "启用", "是"}:
                return True
            if key in {"0", "false", "off", "no", "n", "关", "关闭", "禁用", "否"}:
                return False
            raise ValueError("请输入：开 / 关")
        if self.kind == "int":
            try:
                value = int(text)
            except ValueError as exc:
                raise ValueError("请输入整数") from exc
            self._check_number(value)
            return value
        if self.kind == "float":
            try:
                value = float(text)
            except ValueError as exc:
                raise ValueError("请输入数字") from exc
            self._check_number(value)
            return value
        if self.kind == "multi":
            values = [item.strip() for item in text.replace("；", ",").replace(";", ",").split(",")]
            return self.validate_choices([item for item in values if item])
        return text

    def validate_choices(self, values: list[str]) -> list[str]:
        allowed = {value for value, _label in self.choices}
        out: list[str] = []
        for value in values:
            item = str(value or "").strip()
            if not item:
                continue
            if item not in allowed:
                raise ValueError(f"不支持的选项：{item}")
            if item not in out:
                out.append(item)
        return out

    def _check_number(self, value: float) -> None:
        if self.min_value is not None and value < self.min_value:
            raise ValueError(f"不能小于 {self.min_value:g}")
        if self.max_value is not None and value > self.max_value:
            raise ValueError(f"不能大于 {self.max_value:g}")


def _s(*args, **kwargs) -> SettingSpec:
    return SettingSpec(*args, **kwargs)


SPECS: dict[str, SettingSpec] = {
    "agent.maxRunWallSeconds": _s(
        "agent.maxRunWallSeconds",
        "单轮最长运行时间",
        "单轮任务允许运行的最长时间；0 表示不限制。",
        "float",
        "agent",
        "下一轮生效",
        min_value=0,
        max_value=86400,
        unit="秒",
    ),
    "agent.noProgressRounds": _s(
        "agent.noProgressRounds",
        "连续无进展轮数",
        "模型连续多轮没有有效输出或工具进展时，OpenBear 会主动收敛。",
        "int",
        "agent",
        "下一轮生效",
        min_value=1,
        max_value=50,
    ),
    "agent.maxRetries": _s(
        "agent.maxRetries",
        "模型调用失败重试次数",
        "上游限流、超时、网络错误等可重试失败的最大补救次数；默认 10 次，不计首次请求。",
        "int",
        "retry",
        "下一轮生效",
        min_value=0,
        max_value=50,
    ),
    "agent.retryBackoffS": _s(
        "agent.retryBackoffS",
        "重试基础等待",
        "首次重试前的等待时间；后续按指数增长。",
        "float",
        "retry",
        "下一轮生效",
        min_value=0,
        max_value=60,
        unit="秒",
    ),
    "agent.retryMaxDelayS": _s(
        "agent.retryMaxDelayS",
        "重试等待上限",
        "指数退避的单次等待上限；服务端 Retry-After 不受此上限覆盖。",
        "float",
        "retry",
        "下一轮生效",
        min_value=0,
        max_value=600,
        unit="秒",
    ),
    "agent.retryJitterRatio": _s(
        "agent.retryJitterRatio",
        "重试随机抖动",
        "在指数退避上附加的随机比例，避免多个请求同时再次撞上游。",
        "float",
        "retry",
        "下一轮生效",
        min_value=0,
        max_value=1,
    ),
    "agent.emptyResponseRetryLimit": _s(
        "agent.emptyResponseRetryLimit",
        "空回复补救次数",
        "模型调用成功但没有正文时，允许额外补救重试的次数。",
        "int",
        "retry",
        "下一轮生效",
        min_value=0,
        max_value=10,
    ),
    "agent.reasoningOnlyRetryLimit": _s(
        "agent.reasoningOnlyRetryLimit",
        "只有思考无正文的补救次数",
        "模型只输出 thinking 但没给最终正文时，允许补救重试的次数。",
        "int",
        "retry",
        "下一轮生效",
        min_value=0,
        max_value=10,
    ),
    "agent.compactRatio": _s(
        "agent.compactRatio",
        "历史压缩触发比例",
        "最近一次上下文接近模型窗口到这个比例后，后台会压缩旧历史。",
        "float",
        "compact",
        "下一轮生效",
        min_value=0.1,
        max_value=0.95,
    ),
    "agent.keepRecentMessages": _s(
        "agent.keepRecentMessages",
        "压缩后保留最近可见消息数",
        "主会话仅以 XML 保留最近用户消息与最终助手文本；Rath Agent 仅保留有界的任务/控制/纯文本 XML，并重新注入最新 Plan 与 Task Memory。工具、通知、旧 Plan、TaskMemory 回执和内部运行状态不会作为原始上下文回放。",
        "int",
        "compact",
        "下一轮生效",
        min_value=2,
        max_value=100,
    ),
    "agent.compactMaxTokens": _s(
        "agent.compactMaxTokens",
        "压缩输出上限",
        "生成历史压缩摘要时允许模型输出的最大 token 数。",
        "int",
        "compact",
        "下一轮生效",
        min_value=512,
        max_value=64000,
    ),
    "agent.compactMaxRetries": _s(
        "agent.compactMaxRetries",
        "压缩质量重试次数",
        "摘要缺少必需小节或生成失败时，同一模型额外重试的次数；压缩模型失败后仍会回退主模型。",
        "int",
        "compact",
        "下一轮生效",
        min_value=0,
        max_value=10,
    ),
    "agent.compactTimeoutS": _s(
        "agent.compactTimeoutS",
        "压缩模型单次超时",
        "压缩请求专用等待上限：覆盖流式首字与整流总时长、非流式读取；连接超时和流式空闲超时仍使用正常模型配置。主会话与 Agent 共用，每个候选模型和质量重试分别独立计时。",
        "float",
        "compact",
        "下一轮生效",
        min_value=1,
        max_value=86400,
        unit="秒",
    ),
    "agent.manualCompactMinPercent": _s(
        "agent.manualCompactMinPercent", "手动压缩最低占用", "仅当最新真实主 Controller 上下文达到压缩阈值的这个百分比时允许手动压缩。", "int", "compact", "立即生效", min_value=0, max_value=100, unit="%",
    ),
    "agent.memoryReminderPercent": _s(
        "agent.memoryReminderPercent", "压缩前记忆提醒占用", "达到压缩阈值的这个百分比后，在下一次安全模型调用提醒保存持久记忆；0 表示关闭。", "int", "compact", "下一次调用生效", min_value=0, max_value=100, unit="%",
    ),
    "agent.memoryReminderPrompt": _s(
        "agent.memoryReminderPrompt", "压缩前记忆提醒提示词", "英文提醒正文；系统会将其封装为 XML，并仅在本次请求中合并到最近一条真实用户消息，不写入会话。", "str", "compact", "下一次调用生效", editor="prompt", variables=("latest_context_tokens", "reminder_threshold_tokens", "compact_trigger_tokens"), default_value=DEFAULT_MEMORY_REMINDER_PROMPT,
    ),
    "agent.compactPrompt": _s(
        "agent.compactPrompt",
        "压缩提示词",
        "历史压缩使用的提示词模板；留空使用内置模板。",
        "str",
        "compact",
        "下一轮生效",
        editor="prompt",
        variables=("existing", "history"),
        default_value=DEFAULT_SUMMARY_PROMPT,
    ),
    "rath.enabled": _s(
        "rath.enabled",
        "启用子 Agent",
        "启用 Agent、AgentMessage、AgentWait 等子任务协作能力。关闭后需要重启 OpenBear。",
        "bool",
        "rath",
        "需要重启",
    ),
    "rath.maxConcurrentTasks": _s(
        "rath.maxConcurrentTasks",
        "Agent 并发上限",
        "Rath Agent task 的全局并发槽数量；同一轮多个 Agent 调用会在该上限内并行执行，过量任务显示为排队中。",
        "int",
        "rath",
        "下一轮生效",
        min_value=1,
        max_value=5,
    ),
    "rath.agentModelCallLimit": _s(
        "rath.agentModelCallLimit",
        "Agent 模型调用上限",
        "单个 Rath Agent task 的模型调用安全预算；达到后暂停并交回 OpenBear 判断是否继续，0 表示不限制。",
        "int",
        "rath",
        "下一轮生效",
        min_value=0,
        max_value=200,
    ),
    "rath.agentToolCallLimit": _s(
        "rath.agentToolCallLimit",
        "工作工具调用上限",
        "单个 Agent task 的普通工作工具安全预算；Plan 控制工具不消耗该预算，0 表示不限制。",
        "int",
        "rath",
        "下一轮生效",
        min_value=0,
        max_value=500,
    ),
    "rath.planControlCallLimit": _s(
        "rath.planControlCallLimit",
        "Plan 控制调用上限",
        "单个 Agent task 累计允许的 Plan 提交、进度和 Replan 控制调用上限。",
        "int",
        "rath",
        "下一轮生效",
        min_value=1,
        max_value=2000,
    ),
    "rath.planMaxRevisionRounds": _s(
        "rath.planMaxRevisionRounds",
        "自动修改轮数",
        "每个初始 Plan 或 Replan 审批周期最多自动 revise 次数；达到后等待用户裁决。",
        "int",
        "rath",
        "下一轮生效",
        min_value=1,
        max_value=10,
    ),
    "rath.planMaxSteps": _s(
        "rath.planMaxSteps",
        "Plan 步骤上限",
        "单个待执行 Plan 最多允许的剩余步骤数。",
        "int",
        "rath",
        "下一轮生效",
        min_value=1,
        max_value=100,
    ),
    "rath.planMaxCriteriaPerStep": _s(
        "rath.planMaxCriteriaPerStep",
        "每步条件上限",
        "单个 Plan step 最多允许的 completion criteria 数。",
        "int",
        "rath",
        "下一轮生效",
        min_value=1,
        max_value=50,
    ),
    "rath.planMaxFinalOutputs": _s(
        "rath.planMaxFinalOutputs",
        "最终交付项上限",
        "单个 Plan 最多允许的 final outputs 数。",
        "int",
        "rath",
        "下一轮生效",
        min_value=1,
        max_value=100,
    ),
    "rath.planDraftPrompt": _s(
        "rath.planDraftPrompt",
        "初始 Plan 提示词",
        "指导 Agent 生成初始完整 Plan；留空跟随内置默认。",
        "str",
        "rath_prompts",
        "下一轮生效",
        editor="prompt",
        variables=PROMPT_SPECS["rath.planDraftPrompt"].variables,
        default_value=PROMPT_SPECS["rath.planDraftPrompt"].default,
    ),
    "rath.planRevisionPrompt": _s(
        "rath.planRevisionPrompt",
        "Plan 修改 / Replan 提示词",
        "指导 Agent 根据审批意见生成完整替代 Plan；留空跟随内置默认。",
        "str",
        "rath_prompts",
        "下一轮生效",
        editor="prompt",
        variables=PROMPT_SPECS["rath.planRevisionPrompt"].variables,
        default_value=PROMPT_SPECS["rath.planRevisionPrompt"].default,
    ),
    "rath.planReviewPrompt": _s(
        "rath.planReviewPrompt",
        "主模型审批提示词",
        "注入 Plan approval notification，指导主模型按版本审批；留空跟随内置默认。",
        "str",
        "rath_prompts",
        "下一轮生效",
        editor="prompt",
        variables=PROMPT_SPECS["rath.planReviewPrompt"].variables,
        default_value=PROMPT_SPECS["rath.planReviewPrompt"].default,
    ),
    "rath.planExecutionPrompt": _s(
        "rath.planExecutionPrompt",
        "Agent 执行提示词",
        "指导 Agent 按 current step、criteria 和 evidence 执行；留空跟随内置默认。",
        "str",
        "rath_prompts",
        "下一轮生效",
        editor="prompt",
        variables=PROMPT_SPECS["rath.planExecutionPrompt"].variables,
        default_value=PROMPT_SPECS["rath.planExecutionPrompt"].default,
    ),
    "rath.planContextRestorePrompt": _s(
        "rath.planContextRestorePrompt",
        "Plan 上下文恢复提示词",
        "用于压缩、预算续跑和阻塞恢复后的数据库事实状态块；留空跟随内置默认。",
        "str",
        "rath_prompts",
        "下一轮生效",
        editor="prompt",
        variables=PROMPT_SPECS["rath.planContextRestorePrompt"].variables,
        default_value=PROMPT_SPECS["rath.planContextRestorePrompt"].default,
    ),
    "agent.llmConnectTimeoutS": _s(
        "agent.llmConnectTimeoutS",
        "连接超时",
        "连接模型上游时允许等待的最长时间。",
        "float",
        "timeouts",
        "下一轮生效",
        min_value=1,
        max_value=300,
        unit="秒",
    ),
    "agent.llmFirstByteTimeoutS": _s(
        "agent.llmFirstByteTimeoutS",
        "首字超时",
        "流式请求建立后，等到第一个有效数据块的最长时间。",
        "float",
        "timeouts",
        "下一轮生效",
        min_value=1,
        max_value=600,
        unit="秒",
    ),
    "agent.llmIdleTimeoutS": _s(
        "agent.llmIdleTimeoutS",
        "空闲超时",
        "模型开始输出后，相邻数据块之间允许空闲的最长时间。",
        "float",
        "timeouts",
        "下一轮生效",
        min_value=1,
        max_value=1800,
        unit="秒",
    ),
    "agent.llmTotalTimeoutS": _s(
        "agent.llmTotalTimeoutS",
        "总超时",
        "整条模型流允许持续的最长时间；0 表示不限制。",
        "float",
        "timeouts",
        "下一轮生效",
        min_value=0,
        max_value=86400,
        unit="秒",
    ),
    "tools.bashTimeoutS": _s(
        "tools.bashTimeoutS",
        "Bash 默认超时",
        "模型调用 Bash 工具但没指定 timeout 时使用的默认超时。",
        "float",
        "tools",
        "下一轮生效",
        min_value=1,
        max_value=3600,
        unit="秒",
    ),
    "tools.bashMaxTimeoutS": _s(
        "tools.bashMaxTimeoutS",
        "Bash 最大超时",
        "模型可请求的 Bash 单次最长运行时间上限。",
        "float",
        "tools",
        "下一轮生效",
        min_value=1,
        max_value=86400,
        unit="秒",
    ),
    "tools.bashOutputLimit": _s(
        "tools.bashOutputLimit",
        "Bash 输出回灌上限",
        "Bash 输出内联回灌给模型的最大字符数；完整原始输出会落盘。",
        "int",
        "tools",
        "下一轮生效",
        min_value=1000,
        max_value=2_000_000,
        unit="字符",
    ),
    "tools.bashSpoolMaxBytes": _s(
        "tools.bashSpoolMaxBytes",
        "Bash 落盘上限",
        "单次 Bash 完整原始输出落盘允许的最大字节数，超过会终止命令防止打满磁盘。",
        "int",
        "tools",
        "下一轮生效",
        min_value=1_000_000,
        max_value=1_000_000_000,
        unit="字节",
    ),
    "tools.fileReadLimitLines": _s(
        "tools.fileReadLimitLines",
        "Read 默认行数上限",
        "Read 未指定 limit 时默认最多返回的行数，也是单次读取行数硬上限。",
        "int",
        "tools",
        "下一轮生效",
        min_value=1,
        max_value=100_000,
        unit="行",
    ),
    "tools.fileReadOutputBytes": _s(
        "tools.fileReadOutputBytes",
        "Read 输出字节上限",
        "Read 单次回灌文本的最大字节数，超过会提示继续 offset 分段读取。",
        "int",
        "tools",
        "下一轮生效",
        min_value=10_000,
        max_value=10_000_000,
        unit="字节",
    ),
    "tools.fileReadMaxLineBytes": _s(
        "tools.fileReadMaxLineBytes",
        "Read 单行字节上限",
        "Read 遇到超长单行时停止并提示，避免单行大文件撑爆内存。",
        "int",
        "tools",
        "下一轮生效",
        min_value=1000,
        max_value=10_000_000,
        unit="字节",
    ),
    "tools.fileStateMaxEntries": _s(
        "tools.fileStateMaxEntries",
        "Read 状态缓存上限",
        "会话级文件读取状态缓存数量，用于重复读取去重等只读逻辑。",
        "int",
        "tools",
        "下一轮生效",
        min_value=8,
        max_value=100_000,
    ),
    "tools.fileDiffMaxChars": _s(
        "tools.fileDiffMaxChars",
        "文件 Diff 预览上限",
        "Write/Edit 返回给模型的 diff 预览最大字符数，超出会另存 diff 文件。",
        "int",
        "tools",
        "下一轮生效",
        min_value=1000,
        max_value=500_000,
        unit="字符",
    ),
    "tools.toolResultMaxChars": _s(
        "tools.toolResultMaxChars",
        "工具结果回灌上限",
        "单个工具结果回灌给模型前允许保留的最大字符数。",
        "int",
        "tools",
        "下一轮生效",
        min_value=1000,
        max_value=500_000,
        unit="字符",
    ),
    "mcp.installDir": _s(
        "mcp.installDir",
        "MCP 安装目录",
        "本地 MCP 的默认安装目录；相对路径以 OpenBear 根目录为基准。",
        "str",
        "mcp",
        "需要重启",
        default_value="./mcp-servers",
    ),
    "memory.baseUrl": _s(
        "memory.baseUrl",
        "记忆服务地址",
        "prompt-memory 服务的 API 地址。",
        "str",
        "memory",
        "下一轮生效",
    ),
    "memory.provider": _s(
        "memory.provider",
        "记忆模式",
        "内置模式使用 OpenBear 自带记忆库；外部模式连接独立的 prompt-memory 服务。",
        "str",
        "memory",
        "需要重启",
    ),
    "memory.identity": _s(
        "memory.identity",
        "记忆身份",
        "调用 prompt-memory 时使用的身份名。",
        "str",
        "memory",
        "下一轮生效",
    ),
    "memory.accessKey": _s(
        "memory.accessKey",
        "记忆访问密钥",
        "调用 prompt-memory 时使用的访问密钥。",
        "str",
        "memory",
        "下一轮生效",
    ),
    "memory.timeoutS": _s(
        "memory.timeoutS",
        "记忆请求超时",
        "访问 prompt-memory 时允许等待的最长时间。",
        "float",
        "memory",
        "下一轮生效",
        min_value=1,
        max_value=60,
        unit="秒",
    ),
    "media.downloadDir": _s(
        "media.downloadDir",
        "媒体下载目录",
        "入站媒体缓存目录；相对路径会落在 OpenBear 工作目录内。",
        "str",
        "media",
        "下一轮生效",
    ),
    "media.maxImageMb": _s(
        "media.maxImageMb",
        "图片上限",
        "单张图片/静态贴纸允许下载的最大体积。",
        "int",
        "media",
        "下一轮生效",
        min_value=0,
        max_value=200,
        unit="MB",
    ),
    "media.maxAudioMb": _s(
        "media.maxAudioMb",
        "音频上限",
        "单条语音/音频允许下载的最大体积。",
        "int",
        "media",
        "下一轮生效",
        min_value=0,
        max_value=200,
        unit="MB",
    ),
    "media.maxVideoMb": _s(
        "media.maxVideoMb",
        "视频上限",
        "单条视频/视频消息允许下载的最大体积。",
        "int",
        "media",
        "下一轮生效",
        min_value=0,
        max_value=500,
        unit="MB",
    ),
    "media.maxFileMb": _s(
        "media.maxFileMb",
        "文件上限",
        "普通附件允许下载的最大体积。",
        "int",
        "media",
        "下一轮生效",
        min_value=0,
        max_value=200,
        unit="MB",
    ),
    "media.maxMediaPerMessage": _s(
        "media.maxMediaPerMessage",
        "单轮媒体数量",
        "单条消息/相册最多处理的媒体数量。",
        "int",
        "media",
        "下一轮生效",
        min_value=0,
        max_value=50,
    ),
    "ui.showTurnStats": _s(
        "ui.showTurnStats",
        "显示本轮统计",
        "在回答完成后显示耗时、Token、TPS 和费用等本轮统计。",
        "bool",
        "interface",
        "下一轮生效",
    ),
    "web.host": _s("web.host", "Web 绑定地址", "Web 管理台监听地址。", "str", "web", "需要重启"),
    "web.port": _s(
        "web.port",
        "Web 端口",
        "Web 管理台监听端口。",
        "int",
        "web",
        "需要重启",
        min_value=1,
        max_value=65535,
    ),
    "web.customUrl": _s(
        "web.customUrl",
        "自定义访问地址",
        "可选。用于展示带域名/SSL 的 Web 管理台访问地址，留空表示不设置。",
        "str",
        "web",
        "立即生效",
    ),
    "web.sessionDays": _s(
        "web.sessionDays",
        "Web Session 有效期",
        "Web 登录会话有效天数。",
        "int",
        "web",
        "需要重启",
        min_value=1,
        max_value=365,
        unit="天",
    ),
    "web.loginRequestTtlSeconds": _s(
        "web.loginRequestTtlSeconds",
        "登录确认有效期",
        "Web 输入 Secret Key 后，等待管理通道二次确认的有效时间。",
        "int",
        "web",
        "需要重启",
        min_value=60,
        max_value=3600,
        unit="秒",
    ),
    "web.failedLoginCooldownMinutes": _s(
        "web.failedLoginCooldownMinutes",
        "失败登录冷却",
        "同一 IP 连续输错 Secret Key 后进入冷却的时间。",
        "int",
        "web",
        "需要重启",
        min_value=1,
        max_value=1440,
        unit="分钟",
    ),
    "web.taskNotifications.enabled": _s(
        "web.taskNotifications.enabled",
        "Telegram 长任务通知",
        "Web 任务运行超过阈值后，通过当前 OpenBear Bot 通知该会话的登录用户。",
        "bool",
        "web_notifications",
        "立即生效",
    ),
    "web.taskNotifications.includeResult": _s(
        "web.taskNotifications.includeResult",
        "发送最终回答",
        "任务成功完成后，先发完成通知，再将该轮最终正式回答通过 Telegram 富文本渐进显示。",
        "bool",
        "web_notifications",
        "立即生效",
    ),
    "web.taskNotifications.thresholdMinutes": _s(
        "web.taskNotifications.thresholdMinutes",
        "长任务时长阈值",
        "只有达到该时长的 Web 任务才发送通知；任务启动时会冻结本轮配置。",
        "int",
        "web_notifications",
        "立即生效",
        min_value=3,
        max_value=1440,
        unit="分钟",
    ),
    "web.taskNotifications.events": _s(
        "web.taskNotifications.events",
        "通知事件",
        "选择达到阈值后要推送的任务事件；阈值前事件会缓存，短任务不会通知。",
        "multi",
        "web_notifications",
        "立即生效",
        choices=(
            ("task_started", "任务启动"),
            ("agent_started", "Agent 启动"),
            ("agent_finished", "Agent 执行结束"),
            ("retrying", "模型出错重试"),
            ("task_completed", "任务完成"),
            ("task_failed", "任务失败"),
            ("task_interrupted", "任务中断"),
        ),
    ),
}

# GROUPS 是设置路径唯一归属清单；Web 二级导航和 Telegram 设置入口都从这里读取。
# 每个 SPECS 项必须恰好出现一次，测试会阻止“后端有定义、前端看不见”的漂移。
GROUPS: dict[str, tuple[str, list[str]]] = {
    "agent": (
        "运行与会话",
        [
        "agent.maxRunWallSeconds",
        "agent.noProgressRounds",
        ],
    ),
    "retry": (
        "模型失败恢复",
        [
        "agent.maxRetries",
        "agent.retryBackoffS",
        "agent.retryMaxDelayS",
        "agent.retryJitterRatio",
        "agent.emptyResponseRetryLimit",
        "agent.reasoningOnlyRetryLimit",
        ],
    ),
    "timeouts": (
        "模型超时",
        [
        "agent.llmConnectTimeoutS",
        "agent.llmFirstByteTimeoutS",
        "agent.llmIdleTimeoutS",
        "agent.llmTotalTimeoutS",
        ],
    ),
    "compact": (
        "上下文压缩",
        [
        "agent.compactRatio",
        "agent.keepRecentMessages",
        "agent.compactMaxTokens",
        "agent.compactMaxRetries",
        "agent.compactTimeoutS",
        "agent.manualCompactMinPercent",
        "agent.memoryReminderPercent",
        "agent.memoryReminderPrompt",
        "agent.compactPrompt",
        ],
    ),
    "rath": (
        "子 Agent",
        [
        "rath.enabled",
        "rath.maxConcurrentTasks",
        "rath.agentModelCallLimit",
        "rath.agentToolCallLimit",
        "rath.planControlCallLimit",
        "rath.planMaxRevisionRounds",
        "rath.planMaxSteps",
        "rath.planMaxCriteriaPerStep",
        "rath.planMaxFinalOutputs",
        ],
    ),
    "rath_prompts": (
        "Agent Plan 提示词",
        [
        "rath.planDraftPrompt",
        "rath.planRevisionPrompt",
        "rath.planReviewPrompt",
        "rath.planExecutionPrompt",
        "rath.planContextRestorePrompt",
        ],
    ),
    "bash": (
        "Bash 执行",
        [
        "tools.bashTimeoutS",
        "tools.bashMaxTimeoutS",
        "tools.bashOutputLimit",
        "tools.bashSpoolMaxBytes",
        ],
    ),
    "files": (
        "文件读取",
        [
        "tools.fileReadLimitLines",
        "tools.fileReadOutputBytes",
        "tools.fileReadMaxLineBytes",
        "tools.fileStateMaxEntries",
        ],
    ),
    "tool_results": (
        "修改与结果回灌",
        [
        "tools.fileDiffMaxChars",
        "tools.toolResultMaxChars",
        ],
    ),
    "mcp": (
        "MCP",
        [
        "mcp.installDir",
        ],
    ),
    "memory": (
        "记忆服务",
        [
        "memory.provider",
        "memory.baseUrl",
        "memory.identity",
        "memory.accessKey",
        "memory.timeoutS",
        ],
    ),
    "media": (
        "附件与媒体",
        [
        "media.downloadDir",
        "media.maxImageMb",
        "media.maxAudioMb",
        "media.maxVideoMb",
        "media.maxFileMb",
        "media.maxMediaPerMessage",
        ],
    ),
    "web": (
        "Web 与登录安全",
        [
        "web.host",
        "web.port",
        "web.customUrl",
        "web.sessionDays",
        "web.loginRequestTtlSeconds",
        "web.failedLoginCooldownMinutes",
        ],
    ),
    "web_notifications": (
        "Telegram 长任务通知",
        [
        "web.taskNotifications.enabled",
        "web.taskNotifications.includeResult",
        "web.taskNotifications.thresholdMinutes",
        "web.taskNotifications.events",
        ],
    ),
    "interface": (
        "界面显示",
        [
        "ui.showTurnStats",
        ],
    ),
}


# Web 端只维护“哪些小节属于哪个领域”；设置路径仍只在 GROUPS 维护一份。
WEB_DOMAINS: dict[str, tuple[str, str, list[str]]] = {
    "agent": (
        "Agent",
        "运行、恢复、上下文与子任务协作",
        [
            "agent",
            "retry",
            "timeouts",
            "compact",
            "rath",
            "rath_prompts",
        ],
    ),
    "tools": (
        "工具",
        "命令执行、文件处理与结果回灌",
        [
            "bash",
            "files",
            "tool_results",
            "mcp",
        ],
    ),
    "memory": ("记忆", "内置记忆与外部 prompt-memory 连接", ["memory"]),
    "media": ("附件与媒体", "入站媒体处理、体积限制与缓存", ["media"]),
    "web": (
        "Web 与安全",
        "管理台监听、登录会话、长任务通知与安全策略",
        ["web", "web_notifications"],
    ),
    "interface": ("界面显示", "回答内容和运行统计的默认呈现方式", ["interface"]),
}


def get_spec(path: str) -> SettingSpec | None:
    return SPECS.get(path)


def group_specs(group: str) -> list[SettingSpec]:
    _title, paths = GROUPS[group]
    return [SPECS[p] for p in paths]
