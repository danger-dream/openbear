"""Restart completion notification persistence.

OpenBear schedules self-restarts from inside the current process.  Any in-memory
state is gone after systemd restarts the service, so completion notifications must
be written to a small durable queue before the restart is scheduled.  The new
process drains that queue after startup and sends a management-channel message.
"""
from __future__ import annotations

import html
import json
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram import Bot

from app.logging import get_logger
from app.models.thinking import clamp_think_level, default_think_level, normalize_think_level
from app.telegram_ui import send_rich

if TYPE_CHECKING:  # pragma: no cover
    from app.config import Config
    from app.services import Services

log = get_logger("restart_notify")

_BJ_TZ = timezone(timedelta(hours=8), name="UTC+8")
_NOTIFY_FILE = "restart-notifications.json"


def _storage_dir(config: Config) -> Path:
    db_path = Path(config.storage.db_path).expanduser()
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    return db_path.parent


def restart_notification_path(config: Config) -> Path:
    return _storage_dir(config) / _NOTIFY_FILE


def _read_entries(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        log.exception("读取重启完成通知队列失败", 路径=str(path))
        return []
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _write_entries(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_restart_completion_notice(config: Config, *, chat_id: int, reason: str = "",
                                  requested_by: str = "OpenBearControl") -> str:
    """Persist one restart-completion notification request and return its id."""
    path = restart_notification_path(config)
    entries = _read_entries(path)
    notice_id = f"{int(chat_id)}-{int(time.time() * 1000)}"
    entries.append({
        "id": notice_id,
        "chatId": int(chat_id),
        "reason": str(reason or "")[:500],
        "requestedBy": requested_by,
        "requestedAt": int(time.time()),
    })
    _write_entries(path, entries)
    log.info("已记录重启完成通知", 会话=chat_id, 路径=str(path))
    return notice_id


def remove_restart_completion_notice(config: Config, notice_id: str) -> None:
    """Remove one queued notification, used if scheduling the restart failed."""
    if not notice_id:
        return
    path = restart_notification_path(config)
    entries = [x for x in _read_entries(path) if str(x.get("id") or "") != notice_id]
    if entries:
        _write_entries(path, entries)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def pop_restart_completion_notices(config: Config) -> list[dict[str, Any]]:
    """Read and clear pending restart-completion notifications."""
    path = restart_notification_path(config)
    entries = _read_entries(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("清理重启完成通知队列失败", 路径=str(path))
    return entries


def restore_restart_completion_notices(config: Config, entries: list[dict[str, Any]]) -> None:
    """Put unsent notifications back to disk so they are not lost."""
    if not entries:
        return
    path = restart_notification_path(config)
    existing = _read_entries(path)
    seen = {str(x.get("id") or "") for x in existing}
    merged = existing + [x for x in entries if str(x.get("id") or "") not in seen]
    _write_entries(path, merged)


def _fmt_ts(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(float(ts), tz=UTC).astimezone(_BJ_TZ)
    except Exception:
        dt = datetime.now(_BJ_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _resolved_think_level(svc: Services, chat_id: int) -> str:
    try:
        current = svc.selection.current
        resolved = svc.config.models.resolve(current)
        if not resolved:
            return ""
        provider, model = resolved
        stored = normalize_think_level(await svc.messages.get_thinking_level(chat_id))
        level = stored or default_think_level(
            protocol=provider.protocol,
            model_id=model.id,
            reasoning=model.reasoning,
        )
        return clamp_think_level(level, provider.protocol, model.id)
    except Exception:
        return ""


async def build_restart_completion_message(svc: Services, chat_id: int) -> str:
    model = html.escape(str(svc.selection.current or "未知"))
    think = await _resolved_think_level(svc, chat_id)
    think_part = f"\n🧠 思考强度：<code>{html.escape(think)}</code>" if think else ""
    started = html.escape(_fmt_ts(float(getattr(svc, "started_at", time.time()))))
    return (
        "✅ <b>OpenBear 已重启完成</b>\n"
        f"启动时间：<code>{started}</code>\n"
        f"当前模型：<code>{model}</code>"
        f"{think_part}\n"
        "状态：<b>正常</b>"
    )


async def send_pending_restart_completion_notices(bot: Bot, svc: Services) -> int:
    """Send queued restart-completion notifications after startup."""
    entries = pop_restart_completion_notices(svc.config)
    if not entries:
        return 0

    sent = 0
    failed: list[dict[str, Any]] = []
    for entry in entries:
        try:
            chat_id = int(entry.get("chatId") or 0)
        except (TypeError, ValueError):
            continue
        if chat_id <= 0:
            continue
        try:
            await send_rich(bot, chat_id, await build_restart_completion_message(svc, chat_id))
            sent += 1
        except Exception:
            log.exception("发送重启完成通知失败", 会话=chat_id)
            failed.append(entry)
    if failed:
        restore_restart_completion_notices(svc.config, failed)
    if sent:
        log.info("重启完成通知已发送", 数量=sent)
    return sent
