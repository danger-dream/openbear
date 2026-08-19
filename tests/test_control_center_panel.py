from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.admin import _status_keyboard, _status_text
from app.bot.menu import _COMMANDS


class _Messages:
    async def all_time_totals(self, chat_id: int):
        return {
            "session_count": 2,
            "conversation_count": 5,
            "model_call_count": 6,
            "tool_calls": 4,
            "ok_count": 5,
            "fail_count": 1,
            "retry_count": 1,
            "input_tokens": 200,
            "cache_read_tokens": 1800,
            "cache_write_tokens": 0,
            "output_tokens": 300,
            "cost_usd": 0.0123,
            "total_time_ms": 6000,
        }

    async def usage_totals(self, chat_id: int):  # pragma: no cover - 新 /status 不应调用本轮统计
        raise AssertionError("/status must not read turn-scoped usage_totals")


class _Models:
    primary = "openai/gpt"
    compression_models: list[str] = []

    def __init__(self) -> None:
        self.providers = {
            "openai": SimpleNamespace(
                protocol="chat",
                models=[SimpleNamespace(id="gpt", reasoning=True, context_window=400000)],
            )
        }

    def resolve(self, fullname: str):
        if fullname != "openai/gpt":
            return None
        return self.providers["openai"], self.providers["openai"].models[0]


class _Tools:
    def names(self) -> list[str]:
        return ["Read", "Bash"]


class _Runs:
    def count(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_status_text_contains_only_process_and_total_stats(monkeypatch):
    from app.bot import admin

    monkeypatch.setattr(admin.processes, "count", lambda: 0)
    svc = SimpleNamespace(
        messages=_Messages(),
        config=SimpleNamespace(models=_Models()),
        selection=SimpleNamespace(current="openai/gpt"),
        runs=_Runs(),
        rath=None,
        tools=_Tools(),
        skills=[object()],
        started_at=0,
    )

    text = await _status_text(svc, 5352767013)

    assert "OpenBear 状态" in text
    assert "总计统计" in text
    assert "本轮" not in text
    assert "从新会话起" not in text
    assert "当前会话" not in text
    assert "会话数" in text and "2" in text
    assert "模型调用" in text and "6 次" in text
    assert "成功率" in text and "83.3%" in text
    assert "总花费" in text
    assert "渠道管理" not in text
    assert "系统设置" not in text


def test_status_keyboard_is_refresh_only():
    labels = [button.text for row in _status_keyboard().inline_keyboard for button in row]
    callbacks = [button.callback_data for row in _status_keyboard().inline_keyboard for button in row]

    assert labels == ["🔄 刷新"]
    assert callbacks == ["status:refresh"]
    assert not any(cb and cb.startswith("status:open:") for cb in callbacks)
    assert "status:agent_sessions" not in callbacks


def test_bot_commands_are_slim_admin_entries_only():
    commands = [c.command for c in _COMMANDS]
    assert commands == ["status", "restart", "web", "memory"]
    for removed in ["help", "start", "channel", "sessions", "setting", "think", "stop", "new"]:
        assert removed not in commands
