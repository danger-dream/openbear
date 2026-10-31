"""控制中心可编辑设置规格。"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

from app.browser.settings import build_specs as _browser_specs
from app.context.summary_prompt import DEFAULT_SUMMARY_PROMPT
from app.context.user_message_template import DEFAULT_USER_MESSAGE_TEMPLATE, USER_MESSAGE_TEMPLATE_VARIABLES
from app.conversation_titles import DEFAULT_NAMING_PROMPT
from app.prompt_suggestions import DEFAULT_SUGGESTION_PROMPT
from app.agents.prompts import PROMPT_SPECS

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
    editor: Literal["default", "prompt", "template"] = "default"
    variables: tuple[str, ...] = ()
    default_value: str = ""
    # API/config values stay in storage units; editors apply this display-only scale.
    display_scale: int = 1
    nullable: bool = False

    def display_value(self, value):
        return value / self.display_scale if value is not None and self.display_scale != 1 else value

    def parse_display(self, raw: str):
        if self.display_scale == 1:
            return self.parse(raw)
        try:
            number = float(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"请输入数字（{self.unit}）") from exc
        if not isfinite(number):
            raise ValueError(f"请输入数字（{self.unit}）")
        if self.min_value is not None and number < self.min_value / self.display_scale:
            raise ValueError(f"不能小于 {self.min_value / self.display_scale:g} {self.unit}")
        if self.max_value is not None and number > self.max_value / self.display_scale:
            raise ValueError(f"不能大于 {self.max_value / self.display_scale:g} {self.unit}")
        return round(number * self.display_scale)

    def parse(self, raw: str) -> bool | int | float | str | list[str]:
        text = (raw or "").strip()
        if self.nullable and text.lower() in {"", "null"}:
            return None
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
            if item not in allowed and self.path not in {"models.compressionModels", "models.namingModels", "models.suggestionModels"}:
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
    "userMessageTemplate.enabled": _s(
        "userMessageTemplate.enabled", "启用用户消息尾部模板",
        "为新用户消息追加模板；关闭后不追加。已进入上下文的历史消息保持原样。",
        "bool", "user_message_template", "下一轮生效",
    ),
    "userMessageTemplate.template": _s(
        "userMessageTemplate.template", "用户消息尾部提示词模板",
        "使用 [[ 变量 ]]、@if、@each 和 @raw 语法；留空不追加。时间在消息首次进入时固定，重试不刷新。",
        "str", "user_message_template", "下一轮生效", editor="template",
        variables=USER_MESSAGE_TEMPLATE_VARIABLES, default_value=DEFAULT_USER_MESSAGE_TEMPLATE,
    ),
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
        "模型连续这么多轮发出完全相同、不带说明的工具调用时先提醒一次；仍不说明、再重复同样轮数才中止本轮。",
        "int",
        "agent",
        "下一轮生效",
        min_value=1,
        max_value=50,
    ),
    "models.namingModels": _s(
        "models.namingModels", "命名模型",
        "按顺序循环用于生成会话名称；留空时自动选择预计费用最低的可用计费模型，无计费模型时回退当前会话主模型。",
        "multi", "agent", "立即生效",
    ),
    "agent.namingTimeoutS": _s(
        "agent.namingTimeoutS", "命名单次超时",
        "每次会话命名模型请求的独立等待上限。", "float", "agent", "立即生效",
        min_value=1, max_value=600, unit="秒",
    ),
    "agent.namingMaxRetries": _s(
        "agent.namingMaxRetries", "命名失败重试次数",
        "失败后允许的额外调用次数，不计首次请求；候选模型按配置顺序循环。默认 3 次，因此最多调用 4 次。",
        "int", "agent", "立即生效", min_value=0, max_value=50,
    ),
    "agent.namingMaxTurns": _s(
        "agent.namingMaxTurns", "命名会话最大获取轮数",
        "生成名称时读取最近多少轮真实用户对话；0 表示读取全部轮次。",
        "int", "agent", "立即生效", min_value=0,
    ),
    "agent.namingMaxChars": _s(
        "agent.namingMaxChars", "命名会话最大获取字符数",
        "轮数筛选后的正文目标上限；优先均衡裁剪助手输出，用户输入始终完整保留，因此极端情况下实际字符数可能超过该值。",
        "int", "agent", "立即生效", min_value=1000, max_value=100000, unit="字符",
    ),
    "agent.namingPrompt": _s(
        "agent.namingPrompt", "会话命名提示词",
        "会话命名模型使用的系统提示词；留空使用内置模板，会话正文始终作为单独的用户消息提供。",
        "str", "agent", "立即生效", editor="prompt", default_value=DEFAULT_NAMING_PROMPT,
    ),
    "agent.promptSuggestionsEnabled": _s(
        "agent.promptSuggestionsEnabled", "启用下一句推荐",
        "Web 会话每轮正常结束后，在输入框上方推荐几条你接下来可能要发的话，点选后填入输入框。关闭后不再生成，也不显示已有推荐。",
        "bool", "prompt_suggestions", "立即生效",
    ),
    "agent.suggestionFollowConversationModel": _s(
        "agent.suggestionFollowConversationModel", "跟随会话主模型",
        "开启后先用当前会话的主模型生成推荐（读的仍是最近几轮对话摘录），失败时再按“推荐模型”的顺序或自动选择兜底。主模型更懂你的意图，但通常会多花 1 秒左右和更多费用。",
        "bool", "prompt_suggestions", "立即生效",
    ),
    "models.suggestionModels": _s(
        "models.suggestionModels", "推荐模型",
        "按顺序尝试生成推荐，前一个失败才换下一个；留空时自动选择预计费用最低的可用计费模型，无计费模型时回退当前会话主模型。",
        "multi", "prompt_suggestions", "立即生效",
    ),
    "agent.suggestionCount": _s(
        "agent.suggestionCount", "推荐条数",
        "每轮最多推荐几条；下一步不明确时模型可以少给或不给。",
        "int", "prompt_suggestions", "立即生效", min_value=1, max_value=5,
    ),
    "agent.suggestionTimeoutS": _s(
        "agent.suggestionTimeoutS", "推荐单次超时",
        "每次推荐模型请求的等待上限；超时就换下一个候选模型，全部失败则本轮不显示推荐。",
        "float", "prompt_suggestions", "立即生效", min_value=1, max_value=600, unit="秒",
    ),
    "agent.suggestionPrompt": _s(
        "agent.suggestionPrompt", "下一句推荐提示词",
        "推荐模型使用的系统提示词，要求输出 JSON 字符串数组；留空使用内置模板。最近几轮对话和条数上限始终作为单独的用户消息提供。",
        "str", "prompt_suggestions", "立即生效", editor="prompt", default_value=DEFAULT_SUGGESTION_PROMPT,
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
        "默认 3 秒；后续按 3、5、10、30、180、300、600 秒阶梯递增。自定义基础值按比例调整，等待向上取整到秒。",
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
        "阶梯等待的单次上限，默认 600 秒；服务端 Retry-After 是最低等待要求，不会提前重试。",
        "float",
        "retry",
        "下一轮生效",
        min_value=0,
        max_value=600,
        unit="秒",
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
    'agent.compactRatio': _s(
        "agent.compactRatio",
        "压缩触发比例",
        "模型未设置独立触发阈值时，按模型窗口的这个比例触发当前会话选择的压缩策略。",
        "float",
        "compaction",
        "下一轮生效",
        min_value=0.1,
        max_value=0.95,
    ),
    'agent.keepRecentMessages': _s(
        "agent.keepRecentMessages",
        "摘要后保留近期消息数",
        "仅模型摘要使用。保留近期原文，边界向前对齐完整工具批次；用户要求、控制和确认问答另行原文保留。主控和 Agent 共用。",
        "int",
        "compaction",
        "下一轮生效",
        min_value=2,
        max_value=100,
    ),
    'agent.compactMaxTokens': _s(
        "agent.compactMaxTokens",
        "压缩输出上限",
        "生成历史压缩摘要时允许模型输出的最大 token 数。",
        "int",
        "compaction",
        "下一轮生效",
        min_value=512,
        max_value=64000,
    ),
    'agent.compactMaxRetries': _s(
        "agent.compactMaxRetries",
        "压缩质量重试次数",
        "摘要缺少必需小节或生成失败时，同一模型额外重试的次数；压缩模型失败后仍会回退主模型。",
        "int",
        "compaction",
        "下一轮生效",
        min_value=0,
        max_value=10,
    ),
    'agent.compactTimeoutS': _s(
        "agent.compactTimeoutS",
        "压缩模型单次超时",
        "压缩请求专用等待上限：覆盖流式首字与整流总时长、非流式读取；连接超时和流式空闲超时仍使用正常模型配置。主会话与 Agent 共用，每个候选模型和质量重试分别独立计时。",
        "float",
        "compaction",
        "下一轮生效",
        min_value=1,
        max_value=86400,
        unit="秒",
    ),
    'agent.manualCompactMinPercent': _s(
        "agent.manualCompactMinPercent", "手动压缩最低占用", "仅当最新真实主 Controller 上下文达到压缩阈值的这个百分比时允许手动压缩。", "int", "compaction", "立即生效", min_value=0, max_value=100, unit="%",
    ),
    'agent.compactPrompt': _s(
        "agent.compactPrompt",
        "压缩提示词",
        "历史压缩使用的提示词模板；留空使用内置模板。",
        "str",
        "compaction",
        "下一轮生效",
        editor="prompt",
        variables=("existing", "history"),
        default_value=DEFAULT_SUMMARY_PROMPT,
    ),
    "contextManagement.defaultStrategy": _s(
        "contextManagement.defaultStrategy", "默认压缩策略", "用于没有目录覆盖的新会话；不会改变已有会话。主控与所属 Agent 共用会话策略。",
        "str", "compaction", "下一轮生效", choices=(("sliding_window", "滑动窗口"), ("model_summary", "模型摘要")),
    ),
    "contextManagement.retainTokens": _s(
        "contextManagement.retainTokens", "滑动窗口保留执行记录",
        "压缩后从最新往前保留完整执行记录（工具调用和结果）的估算 token 数，所有模型共用；只算消息，不含系统提示词和工具定义。当前轮的指令、确认和最近两批工具结果始终保留，超出时允许超过此值。仅滑动窗口使用。",
        "int", "compaction", "下一轮生效", min_value=20_000, max_value=200_000, unit="token",
    ),
    "contextManagement.dialogueTokens": _s(
        "contextManagement.dialogueTokens", "滑动窗口保留对话文字",
        "执行记录之外，更早轮次只保留用户消息、助手文字和确认问答的估算 token 数，工具调用、结果和引用正文不保留。0 关闭。仅滑动窗口使用。",
        "int", "compaction", "下一轮生效", min_value=0, max_value=100_000, unit="token",
    ),
    "contextManagement.toolResultArtifactTokens": _s(
        "contextManagement.toolResultArtifactTokens", "工具结果落盘阈值",
        "仅实际压缩时，把超过阈值的普通工具结果完整保存为文件，上下文保留路径和头尾预览，可用 Read 分段读取全文；包括最新未读结果。滑动窗口和模型摘要共用。默认 16000 token，0 关闭；用户要求、确认决策和 Agent 编排结论不落盘，未授权 Read 时保持原文。",
        "int", "compaction", "下一轮生效", min_value=0, unit="token",
    ),
    "workItems.titleChars": _s(
        "workItems.titleChars", "标题长度", "任务和每一项的标题最多保留的字数，超出部分截掉。",
        "int", "work_items", "下一轮生效", min_value=50, max_value=2000, unit="字",
    ),
    "workItems.quoteChars": _s(
        "workItems.quoteChars", "原话长度",
        "提出、决策、纠正时存下的用户原话，以及问卷补充文字，最多保留的字数。记决策没给原话时，存下整条消息，也按这个长度截断。",
        "int", "work_items", "下一轮生效", min_value=200, max_value=20_000, unit="字",
    ),
    "workItems.maxCriteria": _s(
        "workItems.maxCriteria", "完成标准条数", "每一项最多能写几条完成标准，超出时拒绝保存。",
        "int", "work_items", "下一轮生效", min_value=1, max_value=100, unit="条",
    ),
    "workItems.maxEvidence": _s(
        "workItems.maxEvidence", "证据条数", "标记完成时每一项最多保留几条证据，多出的不保存。",
        "int", "work_items", "下一轮生效", min_value=1, max_value=200, unit="条",
    ),
    "workItems.outlineChars": _s(
        "workItems.outlineChars", "大纲长度",
        "每次请求带给模型的任务大纲最多多少字。超出时先省略未完成项，并注明省了几项，模型需要时再按编号查看。",
        "int", "work_items", "下一轮生效", min_value=1000, max_value=50_000, unit="字",
    ),
    "workItems.briefingChars": _s(
        "workItems.briefingChars", "开工交底长度",
        "开始做某个范围时，交给模型的完成标准、用户原话和决策原文最多多少字，超出部分截断。",
        "int", "work_items", "下一轮生效", min_value=2000, max_value=200_000, unit="字",
    ),
    "workItems.recentEdits": _s(
        "workItems.recentEdits", "大纲里的最近改动条数", "大纲末尾列出你在任务面板上最近几次修改，0 不列。",
        "int", "work_items", "下一轮生效", min_value=0, max_value=50, unit="条",
    ),
    "workItems.recentEditDays": _s(
        "workItems.recentEditDays", "最近改动保留天数", "超过这个天数的面板修改不再列进大纲。",
        "int", "work_items", "下一轮生效", min_value=1, max_value=90, unit="天",
    ),
    "models.compressionModels": _s(
        "models.compressionModels", "摘要模型", "按顺序尝试，最后回退当前执行模型；留空仅使用执行模型。仅模型摘要使用。",
        "multi", "compaction", "下一轮生效",
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
    "rath.contextTriggerTokens": _s(
        "rath.contextTriggerTokens",
        "Agent 压缩触发上限",
        "Agent 上下文达到此估算 token 数即压缩；模型自身的压缩阈值更低时用模型阈值。主会话不受影响。0 表示只用模型阈值。",
        "int",
        "rath",
        "下一轮生效",
        min_value=0,
        unit="token",
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
        "Bash 输出文件大小上限",
        "单次 Bash 完整输出文件最多占用多少MB；超过时终止命令，防止打满磁盘。",
        "int",
        "tools",
        "下一轮生效",
        min_value=1_000_000,
        max_value=1_000_000_000,
        unit="MB",
        display_scale=1024 * 1024,
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
        "Read 返回内容大小上限",
        "Read 每次返回的文本最多多少MB；超过时提示使用offset继续分段读取。",
        "int",
        "tools",
        "下一轮生效",
        min_value=10_000,
        max_value=10_000_000,
        unit="MB",
        display_scale=1024 * 1024,
    ),
    "tools.fileReadMaxLineBytes": _s(
        "tools.fileReadMaxLineBytes",
        "Read 单行大小上限",
        "Read 读取的每行内容最多多少MB；遇到超长单行时停止并提示，避免占满内存。",
        "int",
        "tools",
        "下一轮生效",
        min_value=1000,
        max_value=10_000_000,
        unit="MB",
        display_scale=1024 * 1024,
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
    "mcp.toolCountThreshold": _s(
        "mcp.toolCountThreshold", "MCP 工具数量确认阈值",
        "单个服务已启用工具数超过此值时需确认；0 表示不检查数量。",
        "int", "mcp", "MCP 重载后生效", min_value=0, max_value=100000, default_value=30, unit="个",
    ),
    "mcp.definitionSizeThresholdKB": _s(
        "mcp.definitionSizeThresholdKB", "MCP 定义体积确认阈值",
        "已启用工具发给模型的紧凑 JSON 定义超过此体积时需确认；0 表示不检查体积。",
        "int", "mcp", "MCP 重载后生效", min_value=0, max_value=1048576, default_value=64, unit="KB",
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
    "features.instances": _s(
        "features.instances", "启用实例",
        "显示“实例”菜单，并把 InstanceControl 工具开放给模型。这个工具的说明较长，开启后每轮对话会多占用一些 token。关闭只隐藏菜单和工具，已有实例的守护与通知照常运行。",
        "bool", "features", "下一轮生效",
    ),
    "features.servers": _s(
        "features.servers", "启用主机",
        "显示“主机”菜单，并把 Servers 工具和节点浏览器开放给模型。这个工具的说明较长，开启后每轮对话会多占用一些 token。关闭只隐藏菜单和工具，节点连接、组网和到期提醒照常运行。",
        "bool", "features", "下一轮生效",
    ),
    "updates.prerelease": _s(
        "updates.prerelease", "接收测试版",
        "除正式版外，也接收 beta 和 rc 测试版：OpenBear 的版本更新和主控给服务器节点挑选的节点版本都按这一项决定。关闭时只用正式版。",
        "bool", "updates", "立即生效",
    ),
    "updates.autoUpdateNodes": _s(
        "updates.autoUpdateNodes", "自动更新节点",
        "有可用的新节点版本时，自动把服务器节点更新过去，离线的节点上线后补上；中转节点放在最后逐台更新。关闭时只提醒，由你在服务器页点「全部更新」。只在开启主机功能时生效，关闭了命令执行的节点仍需在服务器上重新安装。",
        "bool", "updates", "立即生效",
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
    "web.interactionNotifications.enabled": _s(
        "web.interactionNotifications.enabled",
        "Telegram 交互待办提醒",
        "确认、选择、输入或问卷等待回答时立即提醒，不受长任务时长阈值限制；敏感内容只提醒前往 Web。",
        "bool", "web_notifications", "立即生效",
    ),
    "web.interactionNotifications.allowReply": _s(
        "web.interactionNotifications.allowReply",
        "允许在 Telegram 作答",
        "通过交互消息的按钮和指定回复处理非敏感待办；Web 与 Telegram 共用一次提交结果。",
        "bool", "web_notifications", "立即生效",
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

SPECS.update(_browser_specs(_s))
from app.webhooks.settings import build_specs as _webhook_specs, GROUP_TITLES as _WEBHOOK_GROUPS
SPECS.update(_webhook_specs(_s))

# GROUPS 是设置路径唯一归属清单；Web 二级导航和 Telegram 设置入口都从这里读取。
# 每个 SPECS 项必须恰好出现一次，测试会阻止“后端有定义、前端看不见”的漂移。
GROUPS: dict[str, tuple[str, list[str]]] = {
    **{key: (title, [p for p, spec in SPECS.items() if spec.group == key]) for key, title in _WEBHOOK_GROUPS.items()},
    **{key: (title, [p for p, spec in SPECS.items() if spec.group == key]) for key, title in (
        ("browser_connection", "浏览器连接"),
        ("browser_limits", "等待时间与用量"),
        ("browser_recovery", "恢复与权限"),
    )},
    "agent": (
        "运行与会话",
        [
        "agent.maxRunWallSeconds",
        "agent.noProgressRounds",
        "models.namingModels",
        "agent.namingTimeoutS",
        "agent.namingMaxRetries",
        "agent.namingMaxTurns",
        "agent.namingMaxChars",
        "agent.namingPrompt",
        ],
    ),
    "prompt_suggestions": (
        "下一句推荐",
        [
        "agent.promptSuggestionsEnabled",
        "agent.suggestionFollowConversationModel",
        "models.suggestionModels",
        "agent.suggestionCount",
        "agent.suggestionTimeoutS",
        "agent.suggestionPrompt",
        ],
    ),
    "user_message_template": (
        "用户消息尾部模板",
        ["userMessageTemplate.enabled", "userMessageTemplate.template"],
    ),
    "retry": (
        "模型失败恢复",
        [
        "agent.maxRetries",
        "agent.retryBackoffS",
        "agent.retryMaxDelayS",
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
    "compaction": (
        "上下文压缩",
        ['contextManagement.defaultStrategy', 'agent.compactRatio', 'contextManagement.retainTokens', 'contextManagement.dialogueTokens', 'contextManagement.toolResultArtifactTokens', 'models.compressionModels', 'agent.keepRecentMessages', 'agent.compactMaxTokens', 'agent.compactMaxRetries', 'agent.compactTimeoutS', 'agent.manualCompactMinPercent', 'agent.compactPrompt'],
    ),
    "work_items": (
        "任务",
        [
        "workItems.titleChars",
        "workItems.quoteChars",
        "workItems.maxCriteria",
        "workItems.maxEvidence",
        "workItems.outlineChars",
        "workItems.briefingChars",
        "workItems.recentEdits",
        "workItems.recentEditDays",
        ],
    ),
    "rath": (
        "子 Agent",
        [
        "rath.enabled",
        "rath.maxConcurrentTasks",
        "rath.contextTriggerTokens",
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
        "mcp.toolCountThreshold",
        "mcp.definitionSizeThresholdKB",
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
        "Telegram 通知",
        [
        "web.interactionNotifications.enabled",
        "web.interactionNotifications.allowReply",
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
    "features": (
        "按需开启",
        [
        "features.instances",
        "features.servers",
        ],
    ),
    "updates": (
        "版本更新",
        [
        "updates.prerelease",
        "updates.autoUpdateNodes",
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
            "prompt_suggestions",
            "user_message_template",
            "retry",
            "timeouts",
            "compaction",
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
    "work_items": ("任务", "会话任务记录的长度、条数和模型看到的内容", ["work_items"]),
    "features": ("可选功能", "实例、主机等按需开启的菜单和工具", ["features"]),
    "updates": ("版本更新", "是否接收测试版，以及服务器节点的自动更新", ["updates"]),
    "browser": ("浏览器", "网页操作、等待时间与文件大小", ["browser_connection", "browser_limits", "browser_recovery"]),
    "memory": ("记忆", "内置记忆与外部 prompt-memory 连接", ["memory"]),
    "media": ("附件与媒体", "入站媒体处理、体积限制与缓存", ["media"]),
    "notifications": ("通知", "本设备系统通知与 Telegram 提醒", ["web_notifications"]),
    "web": (
        "Web 与安全",
        "管理台监听、登录会话与安全策略",
        ["web"],
    ),
    "webhooks": ("外部触发", "消息接收、排队、脚本与数据保留", list(_WEBHOOK_GROUPS)),
    "interface": ("界面显示", "回答内容和运行统计的默认呈现方式", ["interface"]),
}


def get_spec(path: str) -> SettingSpec | None:
    return SPECS.get(path)


def group_specs(group: str) -> list[SettingSpec]:
    _title, paths = GROUPS[group]
    return [SPECS[p] for p in paths]
