"""白名单中间件测试。"""
from __future__ import annotations

from types import SimpleNamespace

from app.bot.whitelist import WhitelistMiddleware


def _data(uid=123, chat_type="private"):
    return {
        "event_from_user": SimpleNamespace(id=uid),
        "event_chat": SimpleNamespace(id=uid, type=chat_type),
    }


async def test_allowed_private_passes():
    mw = WhitelistMiddleware([123])
    called = {}

    async def handler(event, data):
        called["ok"] = True
        return "done"

    result = await mw(handler, object(), _data())
    assert result == "done"
    assert called.get("ok")


async def test_allowed_user_in_group_is_ignored():
    mw = WhitelistMiddleware([123])
    called = {}

    async def handler(event, data):
        called["ok"] = True
        return "done"

    result = await mw(handler, object(), _data(chat_type="group"))
    assert result is None
    assert "ok" not in called


async def test_blocked_ignored():
    mw = WhitelistMiddleware([123])
    called = {}

    async def handler(event, data):
        called["ok"] = True
        return "done"

    result = await mw(handler, object(), _data(uid=999))
    assert result is None
    assert "ok" not in called


async def test_no_user_ignored():
    mw = WhitelistMiddleware([123])

    async def handler(event, data):
        return "done"

    result = await mw(handler, object(), {})
    assert result is None
