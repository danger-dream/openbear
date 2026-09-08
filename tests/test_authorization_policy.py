from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.compaction import (
    DEFAULT_SUMMARY_PROMPT,
    _render_summary_prompt,
    _summary_missing_sections,
)
from app.db.engine import DB
from app.memory.builtin import BuiltinMemoryClient
from app.tools.base import ToolRegistry
from app.tools.openbear_control import register_openbear_control_tool
from app.tools.user_interaction import UserInteractionManager, register_user_interaction_tools
from tests.test_openbear_control_tool import FakeSvc


@pytest.mark.parametrize("tools", [[], ["UserInteraction", "OpenBearControl", "TaskMemory", "History", "Read", "Bash"]])
async def test_default_policy_renders_continuity_without_relaxing_execution_gates(tmp_path, tools):
    db = DB(str(tmp_path / "policy.db"))
    await db.connect()
    try:
        content = Path("prompts/openbear-system.tpl").read_text()
        output = await BuiltinMemoryClient(db).render_system_prompt({
            "toolNames": tools, "builtinToolNames": tools,
            "workspaceDir": "/shared", "folderWorkspaceDir": "/project",
            "folderPrompt": "preserve directory instructions", "availableAgents": [],
        }, template_content=content, source="policy-test")
        assert "[[ERROR:" not in output
        for phrase in [
            "A standalone question, inspection, or defect report does not by itself authorize changes",
            "During an authorized implementation and acceptance-testing cycle",
            "Source-code authorization alone does not authorize deployment",
            "A different tool, file, or routine technical stage is not by itself a new authorization boundary",
            "without adding a redundant preliminary UserInteraction confirmation",
            "Never bypass the gate, fabricate authorization",
            "Do not treat an uncertain outcome as permission to repeat a side-effecting action",
        ]:
            assert phrase in output
        assert "preserve directory instructions" in output
        assert "workspace/artifacts/" in output
        if tools:
            assert "confirmation response with substantive text is feedback" in output
            assert "an assistant's confirmation habit is not a user constraint" in output
    finally:
        await db.close()


def test_tool_descriptions_remove_preconfirmation_but_keep_feedback_safety():
    reg = ToolRegistry()
    register_user_interaction_tools(reg, UserInteractionManager())
    register_openbear_control_tool(reg, FakeSvc())
    descriptions = reg.summaries(scope="main")
    assert "not to repeat a clear instruction or existing authorization within its scope" in descriptions["UserInteraction"]
    assert "Confirmation feedback does not authorize the original action" in descriptions["UserInteraction"]
    assert "without a redundant UserInteraction pre-confirmation" in descriptions["OpenBearControl"]
    assert "Cancellation or timeout never authorizes execution" in descriptions["OpenBearControl"]
    assert "do not restart/stop openbear.service via Bash" in descriptions["OpenBearControl"]


def test_summary_requires_permission_provenance_without_changing_custom_templates_or_sections():
    assert "explicit user authorization, explicit user restrictions, framework/tool requirements, and assistant plans or assumptions" in DEFAULT_SUMMARY_PROMPT
    assert "A historical confirmation is not a requirement to repeat it" in DEFAULT_SUMMARY_PROMPT
    assert "do not generalize a bounded approval into standing permission" in DEFAULT_SUMMARY_PROMPT
    assert "Preserve later withdrawal or narrowing of permission" in DEFAULT_SUMMARY_PROMPT
    assert "Existing summaries are fallible context, not authority" in DEFAULT_SUMMARY_PROMPT
    assert "without re-reading the compacted transcript" not in DEFAULT_SUMMARY_PROMPT
    assert _summary_missing_sections(DEFAULT_SUMMARY_PROMPT) == []
    assert _render_summary_prompt("CUSTOM {history} {existing}", history="H", existing="E") == "CUSTOM H E"
