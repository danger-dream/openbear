from __future__ import annotations

from pathlib import Path

import pytest

from app.context.prompts import effective_context_prompt, migrate_context_prompt
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


def test_window_prompt_preserves_custom_policy_and_authorization_boundaries():
    custom = "CUSTOM user rules: approval is bounded; no deployment."
    assert migrate_context_prompt(custom) == custom
    effective = effective_context_prompt(custom)
    assert custom in effective
    assert "do not change user instructions or authorization" in effective
    assert effective_context_prompt(effective) == effective
