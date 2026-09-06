"""三个真实失败场景的重放素材与行为检查。

场景来源（真实生产会话）：

- ``cursor-504``：会话 9a751527 的 Parrot 请求失败诊断。历史失败：深挖委派的 prompt 以
  "你负责继续上一只读诊断的独立深挖包" 开头（悬空指代），且 inheritFromTaskUuid 实际只
  带过去 8 个字符的标题。
- ``codex-replace``：会话 76532034。历史失败：同一只 Agent 被要求"调查 Codex 源码"并
  "判断能否替代计划中的统一 AgentRuntime、给出集成方式比较和专业建议"——事实调查和
  跨系统决策塞进一个包，且引用了子 Agent 不可能知道的内部计划概念。
- ``runtime-doc``：会话 5ffc8fba。历史失败：28566 字符的重构文档被主控制器压缩转述成
  约 2900 字符塞进 prompt，又让一只 Agent 既对照文档又审计全部源码、交一份报告。

这里检查的是控制器**实际发出的 Agent 工具调用**（拆包数量、prompt 自包含性、
attachments、工具授权），不检查系统提示词文本里有没有某个短语。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Finding:
    level: str  # "fail" | "warn" | "info"
    message: str
    evidence: str = ""


@dataclass
class ToolEvent:
    round_no: int
    name: str
    args: dict[str, Any]


@dataclass
class Scenario:
    id: str
    title: str
    messages: list[dict[str, str]]
    check: Callable[[list[ToolEvent]], list[Finding]]
    notes: str = ""


def _launches(events: list[ToolEvent]) -> list[ToolEvent]:
    return [e for e in events if e.name == "Agent"]


def _prompt(event: ToolEvent) -> str:
    return str(event.args.get("prompt") or "")


def _clip(text: str, limit: int = 160) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


# 子 Agent 世界里不存在的指代。出现即说明 prompt 不自包含（Cursor 504 案的核心失败）。
_DANGLING_PATTERNS = [
    r"上一(?:个|只|次|轮)?(?:任务|诊断|调查|会话|阶段)",
    r"(?:之前|此前|先前|早前)的?(?:任务|调查|诊断|讨论|会话)",
    r"刚才",
    r"继续上",
    r"你不需要(?:看到|了解|知道)",
    r"如前所述",
    r"前面(?:提到|说过)的",
    r"previous (?:task|investigation|session)",
    r"earlier (?:task|run|investigation)",
]


def common_checks(events: list[ToolEvent]) -> list[Finding]:
    findings: list[Finding] = []
    for event in _launches(events):
        prompt = _prompt(event)
        label = str(event.args.get("description") or "")[:40] or f"round{event.round_no}"
        plan_mode = str(event.args.get("planMode") or "direct")
        if event.args.get("inheritFromTaskUuid") and plan_mode != "managed":
            findings.append(Finding(
                "fail", f"[{label}] direct 任务使用了 inheritFromTaskUuid（运行时会拒绝）",
                _clip(str(event.args.get("inheritFromTaskUuid"))),
            ))
        for pattern in _DANGLING_PATTERNS:
            m = re.search(pattern, prompt, re.IGNORECASE)
            if m:
                findings.append(Finding(
                    "fail", f"[{label}] prompt 含悬空指代（子 Agent 世界里不存在的概念）",
                    _clip(m.group(0)),
                ))
        if event.args.get("tools") is None:
            findings.append(Finding("fail", f"[{label}] 未提供 tools 白名单", ""))
        if not str(event.args.get("description") or "").strip():
            findings.append(Finding("warn", f"[{label}] 缺 description 任务标签", ""))
        if len(prompt) < 200:
            findings.append(Finding(
                "warn", f"[{label}] prompt 仅 {len(prompt)} 字符，任务合同大概率不完整",
                _clip(prompt),
            ))
    return findings


# ---------------------------------------------------------------------------
# 场景一：Cursor 504 深挖
# ---------------------------------------------------------------------------

_CURSOR_UUID_A = "5b4de873-1068-434e-8221-2c3d23796e32"
_CURSOR_UUID_B = "e837bdd6-8887-4a03-b24b-c837d810e7e5"

_CURSOR_DIAGNOSIS = f"""老大，查清楚了：这两个请求都不是 Parrot 自身异常，而是整条上游候选链都失败了，最终都停在 FoxCode 的 HTTP 400「暂不支持」上，所以界面看起来是同一种错误。

# 只读诊断结论

## `{_CURSOR_UUID_A}`（模型 claude-fable-5，今天 09:17:46–09:17:59）
1. Cursor OAuth：约 10.5 秒后失败，HTTP 504，错误正文 `The read operation timed out`。
2. Kedaya-Claude：HTTP 403，`Insufficient balance, withholding failed`（余额不足，预扣款失败）。
3. FoxCode：HTTP 400，`invalid_request_error`，消息「暂不支持」。

## `{_CURSOR_UUID_B}`（模型 claude-opus-5）
1. Cursor OAuth：同样约 10.3 秒后 HTTP 504 读取超时。
2. FoxCode：HTTP 400「暂不支持」。

## 疑点
两个请求里 Cursor OAuth 都是约 10.3～10.5 秒后失败，时间高度一致，不像网络抖动，更像某个固定超时或代码回归。

数据来源：Parrot 请求日志数据库（/opt/src-space/parrot/data/ 下的 sqlite，requests 相关表）与 journalctl -u parrot。Parrot 源码在 /opt/src-space/parrot。本轮只读，未改任何东西。"""


def _check_cursor_504(events: list[ToolEvent]) -> list[Finding]:
    findings = common_checks(events)
    launches = _launches(events)
    if not launches:
        findings.append(Finding(
            "warn", "控制器未委派任何 Agent（自行前台调查）；本场景无法评估委派质量", ""))
        return findings
    uuid_seen = False
    for event in launches:
        prompt = _prompt(event)
        label = str(event.args.get("description") or "")[:40] or f"round{event.round_no}"
        if "504" not in prompt:
            findings.append(Finding("fail", f"[{label}] prompt 缺少核心事实：HTTP 504", ""))
        if not (re.search(r"read operation timed out", prompt, re.IGNORECASE)
                or "读取超时" in prompt or "读超时" in prompt):
            findings.append(Finding(
                "fail", f"[{label}] prompt 缺少核心事实：The read operation timed out / 读取超时", ""))
        if _CURSOR_UUID_A in prompt or _CURSOR_UUID_B in prompt:
            uuid_seen = True
        tools = [str(t) for t in (event.args.get("tools") or [])]
        if not ({"Bash", "Read"} & set(tools)):
            findings.append(Finding(
                "warn", f"[{label}] 源码根因调查却未授予 Bash/Read", str(tools)))
    if not uuid_seen:
        findings.append(Finding("fail", "没有任何委派带上具体请求 UUID，子 Agent 无法定位样本", ""))
    return findings


SCENARIO_CURSOR_504 = Scenario(
    id="cursor-504",
    title="Cursor 504 根因深挖（prompt 必须事实自包含、无悬空指代）",
    messages=[
        {"role": "user", "content": (
            f"帮我看下 parrot 里的 {_CURSOR_UUID_A} 和 {_CURSOR_UUID_B} 这两个请求，"
            "为什么都失败了？")},
        {"role": "assistant", "content": _CURSOR_DIAGNOSIS},
        {"role": "user", "content": (
            "嗯，那你深入查一下这个 504 的根因，究竟为什么 Cursor OAuth 一直读取超时？"
            "把具体原因定位出来。")},
    ],
    check=_check_cursor_504,
    notes="历史失败：深挖包 prompt 以「继续上一只读诊断的独立深挖包」开头，靠 inheritFromTaskUuid 传上下文但实际只带过去 8 个字符标题。",
)


# ---------------------------------------------------------------------------
# 场景二：Codex 替代评估
# ---------------------------------------------------------------------------

_RUNTIME_PLAN_SUMMARY = """老大，这个问题能解决，方向也基本正确，但目标要校准一下：

> 不是把主 Agent 原封不动包装成子 Agent，而是从主 Agent 中抽出唯一的 `AgentRuntime`，让主 Agent 和子 Agent 都成为这个 Runtime 的不同适配器。

要点：
- 当前确实有两套模型/工具执行循环：主会话在 `app/agent/loop.py::Agent.run`，子 Agent 在 `app/rath/single_agent.py::SingleAgentWorkflowRunner._run_agent_loop`，所以改一个底层能力必须改两处。
- 应该删除的是 Rath 里的第二套模型/工具循环；Rath 的子任务控制面（task lineage、后台执行、暂停恢复、Plan 治理、hard budget、事件与 artifact）要保留。
- 分六个阶段推进：先抽共享的 `ModelRequestExecutor`（只统一模型请求层），再统一 RuntimeState/checkpoint，再统一完整模型—工具循环（先迁主 Agent 和 direct 子 Agent），然后迁 Managed Plan，最后删旧 Rath 执行循环。
- 验收标准：主/子 Agent 的生产模型调用都经过同一个 AgentRuntime；Rath 生产路径不再有独立的 backend.stream/complete 循环；修改流式协议、retry 或 tool loop 时不需要再动 `app/rath/single_agent.py`。

这是大型重构，不适合一个 PR 做完，第一个 PR 建议严格只做共享 ModelRequestExecutor 的抽取。"""


def _check_codex_replace(events: list[ToolEvent]) -> list[Finding]:
    findings = common_checks(events)
    launches = _launches(events)
    if not launches:
        findings.append(Finding(
            "warn", "控制器未委派任何 Agent（自行前台调查）；本场景无法评估委派质量", ""))
        return findings
    for event in launches:
        prompt = _prompt(event)
        label = str(event.args.get("description") or "")[:40] or f"round{event.round_no}"
        investigates = re.search(r"(调查|审计|分析|研究|阅读|检查).{0,200}codex", prompt,
                                 re.IGNORECASE | re.DOTALL)
        decides = re.search(
            r"(能否|是否|可否).{0,30}(替代|引入|嵌入)|(最终|专业)建议|集成方式比较|做出?(取舍|决策)|判断.{0,20}(替代|可行)",
            prompt)
        # 背景里转述用户目标不算指派：prompt 若显式把跨系统结论排除在范围外，则不判失败。
        excludes_decision = re.search(
            r"不做.{0,16}(选择|结论|建议|判断)|不对.{0,24}(结论|判断|建议)|只(研究|调查|负责).{0,12}一侧|(供|留给|由)(主?控制器|主控).{0,20}(判断|综合|决策|整合)",
            prompt)
        if investigates and decides and not excludes_decision:
            findings.append(Finding(
                "fail", f"[{label}] 同一只 Agent 同时承担 Codex 事实调查与跨系统替代判断/建议",
                _clip(decides.group(0)),
            ))
        elif investigates and decides and excludes_decision:
            findings.append(Finding(
                "info", f"[{label}] 提及替代目标但已显式把跨系统结论排除在范围外",
                _clip(excludes_decision.group(0)),
            ))
        referent = re.search(r"计划中的|既定(方案|计划)|我们的(方案|计划|重构)", prompt)
        if referent:
            findings.append(Finding(
                "warn", f"[{label}] prompt 引用了内部计划概念，子 Agent 可能无法理解",
                _clip(referent.group(0)),
            ))
    return findings


SCENARIO_CODEX_REPLACE = Scenario(
    id="codex-replace",
    title="Codex 替代评估（事实调查可委派；跨系统比较与最终建议留在控制器）",
    messages=[
        {"role": "user", "content": (
            "我们openbear项目当前有个痛点，主agent和子agent是两套东西，每次要改个什么东西"
            "都必须得改两套...我觉得这个设计太傻了，当初我的技术选型出现了问题\n"
            "我主要想移除掉子代理那个rath agent，转而将我们的主agent封装为类似的东西....\n"
            "请你仔仔细细看看代码，帮我看看是否能解决这个问题？给我点建议、解决方案")},
        {"role": "assistant", "content": _RUNTIME_PLAN_SUMMARY},
        {"role": "user", "content": (
            "等下，本机 /opt/workspace/codex 里有完整的 OpenAI Codex 源码，它本身就带一整套"
            "完整的 agent 服务/执行体系。你研究下它的源码，看看能不能把它引入 openbear，"
            "直接用或者经过适配，替代我们计划中的统一 AgentRuntime？给我专业建议")},
    ],
    check=_check_codex_replace,
    notes="历史失败：一只 Agent 被同时要求调查 Codex、判断能否替代「计划中的统一 AgentRuntime」并给出集成方式比较和专业建议。",
)


# ---------------------------------------------------------------------------
# 场景三：Runtime 统一文档对照
# ---------------------------------------------------------------------------

def _check_runtime_doc(events: list[ToolEvent]) -> list[Finding]:
    findings = common_checks(events)
    launches = _launches(events)
    doc_read_foreground = any(
        e.name == "Memory" and str(e.args.get("resource")) == "doc"
        and str(e.args.get("action")) == "get" for e in events)
    if not launches:
        if doc_read_foreground:
            findings.append(Finding("info", "控制器前台读取文档并自行对照（可接受的处理方式）", ""))
        else:
            findings.append(Finding("fail", "既没有读取文档也没有委派任何工作", ""))
        return findings
    for event in launches:
        prompt = _prompt(event)
        label = str(event.args.get("description") or "")[:40] or f"round{event.round_no}"
        attachments = [str(a) for a in (event.args.get("attachments") or [])]
        mentions_doc = re.search(r"runtime-unification|统一\s*Runtime|重构文档|@doc/", prompt,
                                 re.IGNORECASE)
        if mentions_doc and not attachments and len(prompt) < 15000:
            findings.append(Finding(
                "fail",
                f"[{label}] 要求子 Agent 处理文档，却既没有 attachments 传正文，"
                f"prompt 也只有 {len(prompt)} 字符（原文档 28566 字符，转述必然失真）",
                _clip(mentions_doc.group(0)),
            ))
        if attachments:
            findings.append(Finding("info", f"[{label}] 通过 attachments 传递了完整材料",
                                    str(attachments)))
    if len(launches) == 1:
        prompt = _prompt(launches[0])
        if re.search(r"文档|doc", prompt, re.IGNORECASE) and re.search(r"源码|代码", prompt):
            # 历史灾难模式（无附件+转述+六合一）已由上面的 attachments 检查单独兜底；
            # 单包 vs 并行拆包是模型持续不认同的结构偏好，降为 WARN 保持可见但不阻断激活。
            findings.append(Finding(
                "warn", "唯一一只 Agent 同时承载文档对照与源码审计（按既定期望应拆并行包或部分留在控制器）", ""))
    return findings


SCENARIO_RUNTIME_DOC = Scenario(
    id="runtime-doc",
    title="Runtime 统一文档对照（文档正文须经 attachments 传递或留在控制器；审计要拆包）",
    messages=[
        {"role": "user", "content": (
            "请先查看并分析、理解 @doc/openbear-agent-runtime-unification 文档，"
            "并结合当前代码，搞清楚该做什么")},
    ],
    check=_check_runtime_doc,
    notes="历史失败：28566 字符文档被转述成约 2900 字符塞进 prompt，一只 Agent 被要求对照文档+审计全部源码+交一份报告。",
)


SCENARIOS: list[Scenario] = [SCENARIO_CURSOR_504, SCENARIO_CODEX_REPLACE, SCENARIO_RUNTIME_DOC]
