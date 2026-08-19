from __future__ import annotations

from types import SimpleNamespace

from app.main import _drain_startup_backlog


class FakeBot:
    def __init__(self, updates):
        self._updates = list(updates)
        self.sent: list[str] = []
        self.calls = 0
        self.offsets = []

    async def get_updates(self, **kwargs):
        self.calls += 1
        self.offsets.append(kwargs.get("offset"))
        if self.calls == 1:
            return self._updates
        return []

    async def send_message(self, chat_id, text, **kwargs):  # pragma: no cover - backlog 不应再回复
        self.sent.append(text)
        return SimpleNamespace(message_id=100 + len(self.sent))


def update(update_id: int, text: str, chat_id: int = 123):
    return SimpleNamespace(
        update_id=update_id,
        message=SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            chat=SimpleNamespace(id=chat_id, type="private"),
            text=text,
            caption=None,
        ),
    )


async def test_startup_backlog_only_consumes_updates_without_replay():
    bot = FakeBot([
        update(10, "旧积压消息"),
        update(11, "我想新增一个 OpenBear Agent"),
        update(12, "/status"),
    ])
    svc = SimpleNamespace()

    await _drain_startup_backlog(bot, svc)

    assert bot.calls == 2
    assert bot.offsets == [None, 13]
    assert bot.sent == []
