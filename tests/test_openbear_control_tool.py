from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.control_actions import (
    ControlActionQueue,
    resolve_restart_completion_chat_id,
    schedule_openbear_restart,
)
from app.restart_notify import restart_notification_path
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.openbear_control import register_openbear_control_tool
from app.tools.user_interaction import UserInteractionManager


class FakeBot:
    def __init__(self) -> None:
        self.sent = []
        self.deleted = []
        self.next_message_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)

    async def edit_message_text(self, text, *, chat_id, message_id, **kwargs):
        return True

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


class FakeMessages:
    def __init__(self) -> None:
        self.level = ""

    async def current_session_uuid(self, chat_id: int) -> str:
        return "sess-1"

    async def get_thinking_level(self, chat_id: int) -> str:
        return self.level

    async def set_thinking_level(self, chat_id: int, level: str) -> None:
        self.level = level


class FakeRuns:
    def count(self) -> int:
        return 0

    def is_running(self, chat_id: int) -> bool:
        return False


class FakeRath:
    def count(self) -> int:
        return 0


class FakeSelection:
    current = "openai/gpt"


class FakeModels:
    primary = "openai/gpt"
    compression_models: list[str] = []

    def __init__(self) -> None:
        model = SimpleNamespace(
            id="gpt",
            name="GPT",
            reasoning=True,
            input=["text", "image"],
            context_window=128000,
            max_tokens=8192,
        )
        provider = SimpleNamespace(
            enabled=True,
            protocol="responses",
            base_url="http://127.0.0.1:22122/v1",
            api_key="sk-test-secret",
            models=[model],
        )
        self.providers = {"openai": provider}
        self._provider = provider
        self._model = model

    def resolve(self, fullname: str, *, include_disabled: bool = False):
        if fullname == "openai/gpt":
            return self._provider, self._model
        return None


class FakeSvc:
    def __init__(self, tmp_path: Path | None = None) -> None:
        self.started_at = 123
        self.bot = FakeBot()
        self.interactions = UserInteractionManager(self.bot)  # type: ignore[arg-type]
        self.messages = FakeMessages()
        self.runs = FakeRuns()
        self.rath = FakeRath()
        self.selection = FakeSelection()
        self.config = SimpleNamespace(
            models=FakeModels(),
            storage=SimpleNamespace(db_path=str((tmp_path or Path("/tmp")) / "openbear-test.db")),
            tools=SimpleNamespace(skills_dir="/tmp/openbear-skills", disabled_skills=["disabled-skill"]),
        )
        self.control_actions = ControlActionQueue()
        self.db = None
        self.skills = [
            SimpleNamespace(
                name="weather",
                description="Get current weather and forecasts.",
                location="/tmp/openbear-skills/weather/SKILL.md",
                base_dir="/tmp/openbear-skills/weather",
                enabled=True,
                metadata=SimpleNamespace(
                    always=False,
                    emoji="☁️",
                    homepage="",
                    skill_key="weather",
                    requires=SimpleNamespace(bins=[], env=[]),
                ),
            ),
            SimpleNamespace(
                name="frontend-design",
                description="Create distinctive frontend interfaces.",
                location="/tmp/openbear-skills/frontend-design/SKILL.md",
                base_dir="/tmp/openbear-skills/frontend-design",
                enabled=True,
                metadata=SimpleNamespace(
                    always=True,
                    emoji="",
                    homepage="",
                    skill_key="frontend-design",
                    requires=SimpleNamespace(bins=["node"], env=[]),
                ),
            ),
        ]
        self.skills_reload_called = 0
        self.mcp_reload_called = 0

    def reload_skills_from_disk(self) -> dict:
        self.skills_reload_called += 1
        self.skills.append(
            SimpleNamespace(
                name="new-skill",
                description="Newly loaded skill.",
                location="/tmp/openbear-skills/new-skill/SKILL.md",
                base_dir="/tmp/openbear-skills/new-skill",
                enabled=True,
                metadata=SimpleNamespace(always=False, emoji="", homepage="", skill_key="new-skill", requires=SimpleNamespace(bins=[], env=[])),
            )
        )
        return {
            "ok": True,
            "beforeCount": 2,
            "afterCount": len(self.skills),
            "added": ["new-skill"],
            "removed": [],
            "message": "skills_reloaded",
        }

    async def reload_mcp_from_disk(self) -> dict:
        self.mcp_reload_called += 1
        return {
            "ok": True,
            "message": "reloaded_no_change",
            "summary": {"visibleTools": 0},
            "sensitiveConfigHidden": True,
        }


def _reg(svc: FakeSvc) -> ToolRegistry:
    reg = ToolRegistry()
    register_openbear_control_tool(reg, svc)
    return reg


async def test_openbear_control_status_and_models_mask_key():
    svc = FakeSvc()
    reg = _reg(svc)
    ctx = ToolRuntimeContext(chat_id=123, session_uuid="s1", source="chat")

    status = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "status"}), context=ctx))
    assert status["status"] == "ok"
    assert status["session"]["sessionUuid"] == "sess-1"

    models = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "models"}), context=ctx))
    assert models["status"] == "ok"
    assert models["providers"]["openai"]["apiKey"] == "sk-t…cret"


async def test_openbear_control_status_allows_web_internal_chat_id():
    svc = FakeSvc()
    reg = _reg(svc)
    ctx = ToolRuntimeContext(chat_id=-13, session_uuid="web-session", source="web")

    status = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "status"}), context=ctx))
    assert status["status"] == "ok"
    assert status["session"]["chatId"] == -13


async def test_openbear_control_restart_uses_web_confirmation_for_web_internal_chat_id():
    svc = FakeSvc()
    reg = _reg(svc)

    async def web_confirm(payload: dict) -> dict:
        return {"status": "answered", "confirmed": True, "choice": "confirm", "label": payload.get("confirmText")}

    ctx = ToolRuntimeContext(chat_id=-13, session_uuid="web-session", source="web", web_confirm=web_confirm)
    result = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "restart"}), context=ctx))
    assert result["status"] == "scheduled"
    assert svc.control_actions.pending_count(-13) == 1


async def test_openbear_control_think_sets_level():
    svc = FakeSvc()
    reg = _reg(svc)
    ctx = ToolRuntimeContext(chat_id=123, session_uuid="s1", source="chat")

    result = json.loads(await reg.dispatch(
        "OpenBearControl",
        json.dumps({"action": "think", "args": {"level": "high"}}),
        context=ctx,
    ))
    assert result["status"] == "ok"
    assert svc.messages.level == "high"


async def test_openbear_control_restart_requires_confirm_and_enqueues_after_turn():
    svc = FakeSvc()
    reg = _reg(svc)

    async def web_confirm(payload: dict) -> dict:
        return {"status": "answered", "confirmed": True, "choice": "confirm", "label": payload.get("confirmText")}

    ctx = ToolRuntimeContext(chat_id=123, session_uuid="s1", source="chat", web_confirm=web_confirm)

    result = json.loads(await reg.dispatch(
        "OpenBearControl",
        json.dumps({"action": "restart", "reason": "测试重启"}, ensure_ascii=False),
        context=ctx,
    ))
    assert result["status"] == "scheduled"
    assert svc.control_actions.pending_count(123) == 1
    assert svc.bot.deleted == []


async def test_openbear_control_restart_drain_writes_completion_notice(tmp_path, monkeypatch):
    svc = FakeSvc(tmp_path)
    reg = _reg(svc)

    async def web_confirm(payload: dict) -> dict:
        return {"status": "answered", "confirmed": True, "choice": "confirm", "label": payload.get("confirmText")}

    ctx = ToolRuntimeContext(chat_id=123, session_uuid="s1", source="chat", web_confirm=web_confirm)

    async def fake_restart(*, delay_s: float = 1.0) -> None:
        return None

    monkeypatch.setattr("app.control_actions.schedule_openbear_restart", fake_restart)

    result = json.loads(await reg.dispatch(
        "OpenBearControl",
        json.dumps({"action": "restart", "reason": "测试重启通知"}, ensure_ascii=False),
        context=ctx,
    ))
    assert result["status"] == "scheduled"

    await svc.control_actions.drain_after_turn(svc, 123)
    path = restart_notification_path(svc.config)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw[0]["chatId"] == 123
    assert raw[0]["reason"] == "测试重启通知"


async def test_openbear_control_restart_drain_maps_web_internal_chat_to_owner(tmp_path, monkeypatch):
    svc = FakeSvc(tmp_path)
    reg = _reg(svc)

    class _Cursor:
        async def fetchone(self):
            return {"owner_chat_id": 5352767013}

    class _Conn:
        async def execute(self, sql, params=()):
            assert "web_conversations" in sql
            assert params == (-21,)
            return _Cursor()

    svc.db = SimpleNamespace(conn=_Conn())

    async def web_confirm(payload: dict) -> dict:
        return {"status": "answered", "confirmed": True, "choice": "confirm", "label": payload.get("confirmText")}

    async def fake_restart(*, delay_s: float = 1.0) -> None:
        return None

    monkeypatch.setattr("app.control_actions.schedule_openbear_restart", fake_restart)

    result = json.loads(await reg.dispatch(
        "OpenBearControl",
        json.dumps({"action": "restart", "reason": "Web 重启"}, ensure_ascii=False),
        context=ToolRuntimeContext(chat_id=-21, session_uuid="web-conv", source="web", web_confirm=web_confirm),
    ))
    assert result["status"] == "scheduled"

    await svc.control_actions.drain_after_turn(svc, -21)
    raw = json.loads(restart_notification_path(svc.config).read_text(encoding="utf-8"))
    assert raw[0]["chatId"] == 5352767013
    assert raw[0]["reason"] == "Web 重启"


async def test_schedule_openbear_restart_raises_on_systemd_run_failure(monkeypatch):
    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"failed to schedule"

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert args[:2] == ("systemd-run", "--collect")
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="systemd-run restart failed rc=1"):
        await schedule_openbear_restart(delay_s=1.0)


async def test_resolve_restart_completion_chat_id_maps_internal_chat_to_owner():
    class _Cursor:
        async def fetchone(self):
            return {"owner_chat_id": 5352767013}

    class _Conn:
        async def execute(self, sql, params=()):
            assert params == (-13,)
            return _Cursor()

    svc = SimpleNamespace(db=SimpleNamespace(conn=_Conn()))
    assert await resolve_restart_completion_chat_id(svc, -13) == 5352767013


async def test_openbear_control_denied_from_agent_context():
    svc = FakeSvc()
    reg = _reg(svc)
    ctx = ToolRuntimeContext(chat_id=123, session_uuid="s1", source="agent:dev")
    result = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "restart"}), context=ctx))
    assert result["status"] == "error"
    assert result["error"] == "openbear_control_not_available_in_this_context"


async def test_openbear_control_schema_includes_reload_and_skills_status_actions():
    svc = FakeSvc()
    reg = _reg(svc)
    schema = reg.schemas(scope="main")[0]
    actions = set(schema["parameters"]["properties"]["action"]["enum"])
    assert {"skills_status", "skills_reload", "mcp_reload"} <= actions
    assert "OpenBearControl" not in reg.names(scope="agent")


async def test_openbear_control_skills_status_filters_loaded_skills():
    svc = FakeSvc()
    reg = _reg(svc)
    ctx = ToolRuntimeContext(chat_id=123, session_uuid="s1", source="chat")

    result = json.loads(await reg.dispatch(
        "OpenBearControl",
        json.dumps({"action": "skills_status", "args": {"query": "weather"}}),
        context=ctx,
    ))

    assert result["status"] == "ok"
    assert result["action"] == "skills_status"
    assert result["loadedCount"] == 2
    assert result["matchedCount"] == 1
    assert result["skills"][0]["name"] == "weather"
    assert result["skills"][0]["location"].endswith("/weather/SKILL.md")
    assert result["disabledSkills"] == ["disabled-skill"]


async def test_openbear_control_skills_reload_denied_from_agent_context():
    svc = FakeSvc()
    reg = _reg(svc)
    ctx = ToolRuntimeContext(chat_id=123, session_uuid="s1", source="agent:dev")

    result = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "skills_reload"}), context=ctx))

    assert result["status"] == "error"
    assert result["error"] == "openbear_control_not_available_in_this_context"
    assert svc.skills_reload_called == 0


async def test_openbear_control_mcp_reload_allowed_from_web_after_confirmation_but_denied_from_agent_context():
    svc = FakeSvc()
    reg = _reg(svc)

    agent_ctx = ToolRuntimeContext(chat_id=123, session_uuid="s1", source="agent:dev")
    denied = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "mcp_reload"}), context=agent_ctx))
    assert denied["status"] == "error"
    assert denied["error"] == "openbear_control_not_available_in_this_context"
    assert denied["action"] == "mcp_reload"
    assert svc.mcp_reload_called == 0

    async def web_confirm(payload: dict) -> dict:
        assert payload["title"] == "确认执行：mcp_reload"
        return {"status": "answered", "confirmed": True, "choice": "confirm", "label": payload.get("confirmText")}

    web_ctx = ToolRuntimeContext(chat_id=-13, session_uuid="web-session", source="web", web_confirm=web_confirm)
    result = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "mcp_reload"}), context=web_ctx))
    assert result["status"] == "ok"
    assert result["action"] == "mcp_reload"
    assert result["sensitiveConfigHidden"] is True
    assert svc.mcp_reload_called == 1
