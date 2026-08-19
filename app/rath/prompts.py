"""Built-in Agent Plan prompt templates and strict rendering helpers."""
from __future__ import annotations

import json
from dataclasses import dataclass
from string import Formatter
from typing import Any


@dataclass(frozen=True, slots=True)
class PlanPromptSpec:
    path: str
    default: str
    variables: tuple[str, ...]
    sample_values: dict[str, str]


PLAN_DRAFT_PROMPT = """
根据以下原始任务生成可审批的完整初始 Plan。批准前不得执行普通工具。

原始任务：
{task}

Plan JSON 结构要求：
{plan_schema}

计划必须覆盖目标、included/excluded scope、假设、依赖、每步 required criteria、预期证据、final outputs 与风险。调查或源码审查步骤还必须写明“证据何时足够”的停止条件：先做有边界的生产路径/权威来源定位，再批量读取相关片段；除非原始任务明确要求穷尽性审计，不得把逐文件、逐页面线性扫描当作计划。只提交真实可执行步骤，不把准备执行写成已完成。
""".strip()

PLAN_REVISION_PROMPT = """
根据主控制器意见提交一份完整替代 Plan；不要只解释差异，也不要继续旧计划工作。

原始任务：
{task}

当前持久化 Plan 状态：
{plan_state}

主控制器意见：
{controller_guidance}

若为 Replan，必须覆盖全部剩余工作，明确已完成成果是否仍有效、作废/待复核结论和结构化变化。
""".strip()

PLAN_REVIEW_PROMPT = """
你是 OpenBear 主控制器。请审批以下 Agent Plan。

原始任务：
{task}

待审 Plan：
{plan}

当前自动修改轮数：{revision_count}

检查目标覆盖、范围、依赖顺序、执行方法、required criteria、证据、final outputs、风险与用户授权。调查/审查计划若没有证据充分停止条件、依赖逐文件或逐页面线性扫描，或新增来源并不能解决任何未满足 criterion，应当 revise，要求改成有边界定位、批量取证和证据满足即收敛。可执行才 approve；否则 revise 并给出具体 issues/reason/requiredChanges。达到自动协商上限时向用户说明完整计划、修改、分歧和影响，并等待用户裁决。
""".strip()

PLAN_EXECUTION_PROMPT = """
严格执行当前已批准 Plan，不得越过 current step 或扩大范围。

原始任务：
{task}

当前持久化 Plan 状态：
{plan_state}

当前步骤：
{current_step}

步骤开始、更新、完成、阻塞和 finalize 必须通过 AgentPlanProgress 持久化。普通工具活动必须属于 running step；实质变化必须 AgentPlanReplan；证据必须真实且可引用。每次追加调查工具前，先确认它能解决哪个仍未满足的 criterion 或哪项阻断冲突；否则不要调用。互相独立的文件/页面优先有边界地批量定位和读取；全部必需 criterion 已有直接证据且无阻断冲突时，立即冻结证据、完成步骤并成稿。
""".strip()

PLAN_CONTEXT_RESTORE_PROMPT = """
以下是 Runtime 从数据库重建的权威 Plan 事实。压缩摘要、旧消息或续跑指导与它冲突时，以本状态块为准。

原始任务：
{task}

Plan 状态：
{plan_state}

关键证据：
{evidence}

继续当前 task 和 current step，不从头重做；等待审批、Replan、用户或控制时不得绕过对应门禁。
""".strip()


PROMPT_SPECS: dict[str, PlanPromptSpec] = {
    "rath.planDraftPrompt": PlanPromptSpec(
        "rath.planDraftPrompt",
        PLAN_DRAFT_PROMPT,
        ("task", "plan_schema"),
        {
            "task": "审查项目配置并给出可验证结论。",
            "plan_schema": '{"title":"...","steps":[{"id":"s1","criteria":[...]}]}',
        },
    ),
    "rath.planRevisionPrompt": PlanPromptSpec(
        "rath.planRevisionPrompt",
        PLAN_REVISION_PROMPT,
        ("task", "plan_state", "controller_guidance"),
        {
            "task": "审查项目配置并给出可验证结论。",
            "plan_state": '{"phase":"revising","activePlanVersion":0,"pendingPlanVersion":1}',
            "controller_guidance": "补充回归测试，并排除部署操作。",
        },
    ),
    "rath.planReviewPrompt": PlanPromptSpec(
        "rath.planReviewPrompt",
        PLAN_REVIEW_PROMPT,
        ("task", "plan", "revision_count"),
        {
            "task": "审查项目配置并给出可验证结论。",
            "plan": '{"title":"配置审查","steps":[{"id":"s1","required":true}]}',
            "revision_count": "1",
        },
    ),
    "rath.planExecutionPrompt": PlanPromptSpec(
        "rath.planExecutionPrompt",
        PLAN_EXECUTION_PROMPT,
        ("task", "plan_state", "current_step"),
        {
            "task": "审查项目配置并给出可验证结论。",
            "plan_state": '{"phase":"executing","activePlanVersion":1}',
            "current_step": '{"id":"s1","title":"读取配置"}',
        },
    ),
    "rath.planContextRestorePrompt": PlanPromptSpec(
        "rath.planContextRestorePrompt",
        PLAN_CONTEXT_RESTORE_PROMPT,
        ("task", "plan_state", "evidence"),
        {
            "task": "审查项目配置并给出可验证结论。",
            "plan_state": '{"phase":"executing","currentStepId":"s1"}',
            "evidence": '[{"evidenceUuid":"ev-1","reference":"config.py:10"}]',
        },
    ),
}


def _fields(template: str) -> set[str]:
    fields: set[str] = set()
    try:
        parsed = Formatter().parse(str(template or ""))
        for _literal, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if not field_name or "." in field_name or "[" in field_name:
                raise ValueError(f"不支持的占位符：{field_name or '{}'}")
            if format_spec or conversion:
                raise ValueError(f"占位符不支持格式转换：{field_name}")
            fields.add(field_name)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"提示词模板语法错误：{exc}") from exc
    return fields


def validate_prompt_template(path: str, template: str) -> None:
    spec = PROMPT_SPECS.get(path)
    if spec is None:
        raise ValueError("未知 Plan 提示词")
    unknown = sorted(_fields(template) - set(spec.variables))
    if unknown:
        raise ValueError(f"未知占位符：{', '.join(unknown)}")


def effective_prompt(path: str, raw: str) -> str:
    spec = PROMPT_SPECS.get(path)
    if spec is None:
        raise ValueError("未知 Plan 提示词")
    template = str(raw or "").strip() or spec.default
    validate_prompt_template(path, template)
    return template


def render_plan_prompt(path: str, raw: str, values: dict[str, Any] | None = None) -> str:
    spec = PROMPT_SPECS.get(path)
    if spec is None:
        raise ValueError("未知 Plan 提示词")
    merged: dict[str, str] = dict(spec.sample_values)
    for key, value in (values or {}).items():
        if key not in spec.variables:
            continue
        merged[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return effective_prompt(path, raw).format_map({name: merged.get(name, "") for name in spec.variables})
