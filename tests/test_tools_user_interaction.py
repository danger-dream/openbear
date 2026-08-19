from __future__ import annotations

import json

from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.user_interaction import UserInteractionManager, register_user_interaction_tools


class FakeBot:
    pass


async def test_user_interaction_confirm_uses_web_confirm_callback():
    manager = UserInteractionManager(FakeBot())  # type: ignore[arg-type]
    reg = ToolRegistry()
    register_user_interaction_tools(reg, manager)
    captured: dict = {}

    async def web_confirm(payload: dict) -> dict:
        captured.update(payload)
        return {
            "status": "answered",
            "confirmed": True,
            "choice": "confirm",
            "label": payload.get("confirmText") or "确认",
            "interactionId": "web-confirm-1",
        }

    out = await reg.dispatch(
        "UserInteraction",
        json.dumps({
            "action": "confirm",
            "title": "是否调优",
            "body": "Agent 输出无效",
            "confirmText": "调优",
            "cancelText": "不调优",
            "timeoutSeconds": 5,
        }, ensure_ascii=False),
        context=ToolRuntimeContext(web_confirm=web_confirm, source="web"),
    )
    result = json.loads(out)
    assert result["status"] == "answered"
    assert result["confirmed"] is True
    assert result["label"] == "调优"
    assert captured["title"] == "是否调优"
    assert captured["body"] == "Agent 输出无效"


async def test_user_interaction_confirm_requires_web_context():
    manager = UserInteractionManager(FakeBot())  # type: ignore[arg-type]
    reg = ToolRegistry()
    register_user_interaction_tools(reg, manager)

    out = await reg.dispatch("UserInteraction", json.dumps({"action": "confirm", "title": "t", "body": "b"}))
    result = json.loads(out)
    assert result["status"] == "error"
    assert result["error"] == "user_interaction_not_available_in_this_context"


async def test_user_interaction_select_and_prompt_use_web_callback():
    manager = UserInteractionManager(FakeBot())  # type: ignore[arg-type]
    reg = ToolRegistry()
    register_user_interaction_tools(reg, manager)
    seen: list[str] = []

    async def web_confirm(payload: dict) -> dict:
        seen.append(payload["action"])
        if payload["action"] == "select":
            return {"status": "answered", "cancelled": False, "selectedValues": ["B"], "selectedLabels": ["B"]}
        return {"status": "answered", "cancelled": False, "value": "hello"}

    select = json.loads(await reg.dispatch(
        "UserInteraction",
        json.dumps({"action": "select", "title": "选择方向", "body": "选", "options": ["A", "B"]}, ensure_ascii=False),
        context=ToolRuntimeContext(web_confirm=web_confirm),
    ))
    prompt = json.loads(await reg.dispatch(
        "UserInteraction",
        json.dumps({"action": "prompt", "title": "输入", "body": "填"}, ensure_ascii=False),
        context=ToolRuntimeContext(web_confirm=web_confirm),
    ))

    assert select["selectedValues"] == ["B"]
    assert prompt["value"] == "hello"
    assert seen == ["select", "prompt"]


async def test_old_user_tools_are_removed():
    manager = UserInteractionManager(FakeBot())  # type: ignore[arg-type]
    reg = ToolRegistry()
    register_user_interaction_tools(reg, manager)

    assert "UserInteraction" in reg.names()
    assert "UserInteraction" in reg.names(scope="main")
    assert "UserInteraction" not in reg.names(scope="agent")
    for name in ("UserConfirm", "UserSelect", "UserPrompt"):
        assert name not in reg.names()
        out = await reg.dispatch(name, json.dumps({"title": "t", "body": "b"}))
        assert "未知工具" in out or "unknown tool" in out


async def test_questionnaire_schema_and_dispatch_preserve_canonical_input():
    reg = ToolRegistry()
    register_user_interaction_tools(reg, UserInteractionManager(FakeBot()))
    schema = next(item for item in reg.schemas(scope="main") if item["name"] == "UserInteraction")
    props = schema["parameters"]["properties"]
    question_props = props["questions"]["items"]["properties"]
    assert set(question_props["type"]["enum"]) == {"choice", "open"}
    assert {"description", "options", "recommendation"}.issubset(question_props)
    assert "defaultAnswer" not in question_props
    assert "questionnaire" in props["action"]["enum"]
    assert not any(item["name"] == "UserInteraction" for item in reg.schemas(scope="agent"))

    original = {
        "action": "questionnaire", "title": "澄清", "body": "请回答",
        "questions": [{
            "id": "scope", "type": "choice", "question": "范围？", "description": "题目说明",
            "options": [{"label": "A", "value": "a", "description": "选项说明"}],
            "recommendation": {"values": ["a"], "reason": "仅推荐，不预选"},
        }],
    }
    captured = {}

    async def web_confirm(payload: dict) -> dict:
        captured.update(payload)
        return {"status": "answered", "cancelled": False, "answers": [{
            "questionId": "scope", "selectedValues": [], "text": "我选择其他范围",
        }]}

    result = json.loads(await reg.dispatch(
        "UserInteraction", json.dumps(original, ensure_ascii=False),
        context=ToolRuntimeContext(web_confirm=web_confirm, source="web"),
    ))
    assert captured["questions"] == original["questions"]
    assert result["answers"][0]["text"] == "我选择其他范围"
