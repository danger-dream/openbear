from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.restart_notify import (
    add_restart_completion_notice,
    pop_restart_completion_notices,
    restart_notification_path,
    send_pending_restart_completion_notices,
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict]] = []

    async def send_rich_message(self, *args, **kwargs):
        raise AttributeError("no rich")

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=1)


class FakeMessages:
    async def get_thinking_level(self, chat_id: int) -> str:
        return "high"


class FakeModels:
    def resolve(self, fullname: str):
        provider = SimpleNamespace(protocol="responses")
        model = SimpleNamespace(id="gpt", reasoning=True)
        return provider, model


class FakeSvc:
    def __init__(self, tmp_path: Path) -> None:
        self.started_at = 1782200439
        self.config = SimpleNamespace(
            storage=SimpleNamespace(db_path=str(tmp_path / "openbear.db")),
            models=FakeModels(),
        )
        self.selection = SimpleNamespace(current="openai/gpt")
        self.messages = FakeMessages()


def test_restart_notice_persist_and_pop(tmp_path):
    svc = FakeSvc(tmp_path)
    notice_id = add_restart_completion_notice(svc.config, chat_id=123, reason="test")
    path = restart_notification_path(svc.config)
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw[0]["id"] == notice_id
    assert raw[0]["chatId"] == 123

    entries = pop_restart_completion_notices(svc.config)
    assert entries[0]["id"] == notice_id
    assert not path.exists()


async def test_send_pending_restart_completion_notices(tmp_path):
    svc = FakeSvc(tmp_path)
    bot = FakeBot()
    add_restart_completion_notice(svc.config, chat_id=123, reason="test")

    sent = await send_pending_restart_completion_notices(bot, svc)

    assert sent == 1
    assert len(bot.sent) == 1
    chat_id, text, kwargs = bot.sent[0]
    assert chat_id == 123
    assert "OpenBear 已重启完成" in text
    assert "2026-06-23 15:40:39" in text
    assert "openai/gpt" in text
    assert "正常" in text
    assert not restart_notification_path(svc.config).exists()
