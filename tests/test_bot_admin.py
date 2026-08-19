from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import telegram_ui as state
from app.bot import admin
from app.restart_notify import restart_notification_path


class _Cursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Conn:
    async def execute(self, sql, params=()):
        if "COUNT(*) FROM web_sessions" in sql:
            return _Cursor((2,))
        if "COUNT(*) FROM web_login_requests" in sql:
            return _Cursor((1,))
        return _Cursor((0,))


class _WebAdmin:
    async def ensure_secret_key(self) -> str:
        return "secret-key"

    async def decide_login_request(self, req_uuid: str, *, approved: bool, decided_by: int) -> str:
        self.decision = (req_uuid, approved, decided_by)
        return "approved" if approved else "denied"

    async def revoke_all_sessions(self, **kwargs) -> int:
        self.revoked_kwargs = kwargs
        return 3

    async def reset_secret_key(self, **kwargs) -> str:
        self.reset_kwargs = kwargs
        return "new-secret"


class _Config:
    def __init__(self) -> None:
        self.web = SimpleNamespace(
            enabled=True,
            host="127.0.0.1",
            port=18961,
            custom_url="https://bear.example.com",
            session_days=30,
            login_request_ttl_seconds=300,
            failed_login_cooldown_minutes=10,
        )
        self.memory = SimpleNamespace(
            provider="builtin",
            base_url="http://memory/api",
            identity="openbear",
            access_key="access-key",
            timeout_s=8,
        )
        self.storage = SimpleNamespace(db_path="")
        self.data = {
            "web": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 18961,
                "customUrl": "https://bear.example.com",
                "sessionDays": 30,
                "loginRequestTtlSeconds": 300,
                "failedLoginCooldownMinutes": 10,
            },
            "memory": {
                "provider": "builtin",
                "baseUrl": "http://memory/api",
                "identity": "openbear",
                "accessKey": "access-key",
                "timeoutS": 8,
            },
        }

    def model_dump(self, by_alias=True):
        return self.data


class _ConfigStore:
    def __init__(self, cfg: _Config) -> None:
        self.cfg = cfg
        self.writes = []

    async def update_path(self, path: str, value):
        self.writes.append((path, value))
        if path == "memory.identity":
            self.cfg.memory.identity = value
            self.cfg.data["memory"]["identity"] = value
        elif path == "memory.timeoutS":
            self.cfg.memory.timeout_s = value
            self.cfg.data["memory"]["timeoutS"] = value
        elif path == "web.port":
            self.cfg.web.port = value
            self.cfg.data["web"]["port"] = value
        else:
            raise AssertionError(f"unexpected path {path}")
        return self.cfg


class _Bot:
    def __init__(self) -> None:
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _svc(tmp_path=None) -> SimpleNamespace:
    cfg = _Config()
    if tmp_path is not None:
        cfg.storage.db_path = str(tmp_path / "openbear.db")
    applied = []

    def apply_config(new_cfg):
        applied.append(new_cfg)

    return SimpleNamespace(
        config=cfg,
        config_store=_ConfigStore(cfg),
        db=SimpleNamespace(conn=_Conn()),
        web_admin=_WebAdmin(),
        apply_config=apply_config,
        applied=applied,
        runs=SimpleNamespace(count=lambda: 0),
        rath=SimpleNamespace(count=lambda: 0),
    )


def _button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _button_callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_web_panel_has_maintenance_actions_without_disable_or_legacy_buttons():
    svc = _svc()

    text = await admin.render_web_console_text(svc)
    markup = admin._web_console_keyboard(svc)
    buttons = _button_texts(markup)
    callbacks = _button_callbacks(markup)

    assert "Web 管理台" in text
    assert "访问密钥" in text
    assert "当前 Web Session" in text
    assert "待确认登录" in text
    assert "状态：<code>启用</code>" in text
    assert not any("关闭" in text for text in buttons)
    assert not any("返回系统设置" in text for text in buttons)
    assert not any("取消" in text for text in buttons)
    assert "settings:web_toggle" not in callbacks
    assert "web.enabled" not in "\n".join(callbacks)
    assert all(not cb.startswith("settings:") for cb in callbacks)
    assert "web:reset_key_confirm" in callbacks
    assert "web:sessions" in callbacks
    assert "web:kick_confirm" in callbacks


def test_memory_panel_exposes_all_memory_settings():
    svc = _svc()

    text = admin.render_memory_text(svc)
    callbacks = _button_callbacks(admin._memory_keyboard(svc))

    assert "记忆服务" in text
    assert "记忆模式" in text
    assert "记忆服务地址" in text
    assert "记忆身份" in text
    assert "记忆访问密钥" in text
    assert "记忆请求超时" in text
    assert "access-key" not in text
    assert callbacks == [
        "memory:toggle:memory.provider",
        "memory:edit:memory.baseUrl",
        "memory:edit:memory.identity",
        "memory:edit:memory.accessKey",
        "memory:edit:memory.timeoutS",
    ]


@pytest.mark.asyncio
async def test_memory_setting_input_uses_config_store_and_apply_config(monkeypatch):
    svc = _svc()
    bot = _Bot()
    chat_id = 123
    state.clear_pending(chat_id)
    state.set_pending(chat_id, action="admin_setting_edit", message_id=77, data={"path": "memory.identity", "panel": "memory"})
    edits = []

    async def fake_edit_rich(bot_obj, chat_id_arg, message_id, text, reply_markup=None):
        edits.append((chat_id_arg, message_id, text, reply_markup))

    monkeypatch.setattr(admin, "edit_rich", fake_edit_rich)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        text="openbear-prod",
        caption=None,
        bot=bot,
        message_id=88,
    )

    await admin.on_admin_setting_input(message, svc)

    assert svc.config_store.writes == [("memory.identity", "openbear-prod")]
    assert svc.applied == [svc.config]
    assert edits and edits[0][1] == 77
    assert "openbear-prod" in edits[0][2]
    assert bot.deleted == [(chat_id, 88)]
    assert state.get_pending(chat_id) is None


@pytest.mark.asyncio
async def test_web_login_callback_approves_request(monkeypatch):
    svc = _svc()
    edits = []

    async def fake_edit_rich(bot_obj, chat_id_arg, message_id, text, reply_markup=None):
        edits.append(text)

    monkeypatch.setattr(admin, "edit_rich", fake_edit_rich)

    class _Query:
        data = "web_login:approve:req-1"
        from_user = SimpleNamespace(id=5352767013)
        message = SimpleNamespace(chat=SimpleNamespace(id=5352767013), message_id=9)
        bot = object()

        async def answer(self, *args, **kwargs):
            self.answered = (args, kwargs)

    query = _Query()
    await admin.cb_web_login_decision(query, svc)

    assert svc.web_admin.decision == ("req-1", True, 5352767013)
    assert edits and "已确认登录" in edits[0]


@pytest.mark.asyncio
async def test_telegram_restart_writes_completion_notice(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    scheduled = []

    async def fake_restart(*, delay_s: float = 1.0):
        scheduled.append(delay_s)

    async def fake_sleep(_delay: float):
        return None

    monkeypatch.setattr("app.control_actions.schedule_openbear_restart", fake_restart)
    monkeypatch.setattr(admin.asyncio, "sleep", fake_sleep)

    await admin._schedule_restart(svc, 5352767013, reason="test tg restart")

    assert scheduled == [1.0]
    raw = json.loads(restart_notification_path(svc.config).read_text(encoding="utf-8"))
    assert raw[0]["chatId"] == 5352767013
    assert raw[0]["reason"] == "test tg restart"
    assert raw[0]["requestedBy"] == "telegram"


@pytest.mark.asyncio
async def test_telegram_restart_schedule_failure_removes_notice(tmp_path, monkeypatch):
    svc = _svc(tmp_path)

    async def fake_restart(*, delay_s: float = 1.0):
        raise RuntimeError("boom")

    async def fake_sleep(_delay: float):
        return None

    monkeypatch.setattr("app.control_actions.schedule_openbear_restart", fake_restart)
    monkeypatch.setattr(admin.asyncio, "sleep", fake_sleep)

    await admin._schedule_restart(svc, 5352767013, reason="test tg restart")

    assert not restart_notification_path(svc.config).exists()
