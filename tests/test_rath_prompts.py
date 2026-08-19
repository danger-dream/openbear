from __future__ import annotations

import pytest

from app.admin.settings import (
    parse_setting_value,
    preview_prompt_setting,
    safe_settings_payload,
    serialize_spec,
)
from app.rath.prompts import PROMPT_SPECS, render_plan_prompt
from app.settings.specs import get_spec
from app.web_console.rath_api import WebAdminRathMixin


def test_all_plan_prompt_defaults_render_with_sample_values() -> None:
    for path, spec in PROMPT_SPECS.items():
        rendered = render_plan_prompt(path, "")
        assert rendered
        assert not any(f"{{{name}}}" in rendered for name in spec.variables)


def test_builtin_plan_prompts_require_bounded_evidence_driven_investigation() -> None:
    draft = render_plan_prompt("rath.planDraftPrompt", "")
    review = render_plan_prompt("rath.planReviewPrompt", "")
    execution = render_plan_prompt("rath.planExecutionPrompt", "")

    assert "证据何时足够" in draft
    assert "不得把逐文件、逐页面线性扫描当作计划" in draft
    assert "批量取证" in review
    assert "仍未满足的 criterion" in execution
    assert "立即冻结证据" in execution


def test_prompt_setting_metadata_validation_preview_and_builtin_tracking() -> None:
    spec = get_spec("rath.planExecutionPrompt")
    assert spec is not None
    payload = serialize_spec(spec)
    assert payload["editor"] == "prompt"
    assert payload["variables"] == ["task", "plan_state", "current_step"]
    assert payload["defaultValue"]

    rendered = preview_prompt_setting(
        spec.path,
        "任务={task}\n步骤={current_step}",
        {"task": "检查配置", "current_step": {"id": "s1"}},
    )
    assert "任务=检查配置" in rendered
    assert '"id": "s1"' in rendered
    with pytest.raises(ValueError, match="未知占位符"):
        parse_setting_value(spec.path, "bad={unknown}")

    settings = safe_settings_payload({"rath": {"planExecutionPrompt": ""}})
    assert settings["usingBuiltin"][spec.path] is True
    assert settings["values"][spec.path] == spec.default_value
    assert parse_setting_value(spec.path, "") == ""


def test_compact_prompt_uses_generic_prompt_editor_validation() -> None:
    spec = get_spec("agent.compactPrompt")
    assert spec is not None and spec.editor == "prompt"
    assert "现有" in preview_prompt_setting(spec.path, "摘要={existing}\n历史={history}")
    with pytest.raises(ValueError, match="未知占位符"):
        parse_setting_value(spec.path, "{task}")


def test_plan_version_diff_reports_structural_changes() -> None:
    previous = {
        "version": 1,
        "plan": {
            "title": "v1",
            "steps": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
        },
    }
    current = {
        "version": 2,
        "plan": {
            "title": "v2",
            "steps": [{"id": "a", "title": "A2"}, {"id": "c", "title": "C"}],
        },
    }
    diff = WebAdminRathMixin._plan_version_diff(previous, current)
    assert diff == {
        "fromVersion": 1,
        "toVersion": 2,
        "addedStepIds": ["c"],
        "removedStepIds": ["b"],
        "retainedStepIds": ["a"],
        "changedStepIds": ["a"],
        "changedFields": ["title"],
    }
