"""轻量 Telegram 管理入口。

TG 端只保留安全兜底能力：状态、重启、Web 维护、记忆服务维护、Web 登录确认。
普通对话与完整管理台迁移到 Web。
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import __version__
from app import telegram_ui as tg_ui
from app.control_actions import schedule_openbear_restart_with_completion_notice
from app.db.engine import now_ts
from app.logging import get_logger
from app.services import Services
from app.settings.specs import GROUPS, SettingSpec, get_spec, group_specs
from app.telegram_ui import answer_rich, edit_rich
from app.tools import processes

log = get_logger("bot.admin")
router = Router()
_TZ = timezone(timedelta(hours=8))

_WEB_SETTING_PATHS = {
    "web.host",
    "web.port",
    "web.customUrl",
    "web.sessionDays",
    "web.loginRequestTtlSeconds",
    "web.failedLoginCooldownMinutes",
}
_MEMORY_SETTING_PATHS = {
    "memory.provider",
    "memory.baseUrl",
    "memory.identity",
    "memory.accessKey",
    "memory.timeoutS",
}


# ---------------------------------------------------------------------------
# 通用格式化


def _format_uptime(seconds: int) -> str:
    days, rem = divmod(max(0, int(seconds)), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    mins, sec = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m {sec}s"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _format_clock(ts: int) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=_TZ).strftime("%m-%d %H:%M:%S")


def _fmt_ts(ts: int) -> str:
    return _format_clock(ts) if ts else "-"


def _format_tokens(value: int) -> str:
    value = max(0, int(value or 0))
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _format_percent(value: float) -> str:
    return f"{value:.1f}%"


def _format_tps(value: float) -> str:
    if value <= 0:
        return "—"
    if value >= 100:
        return f"{value:.0f} t/s"
    return f"{value:.1f} t/s"


def _format_cost(value: float) -> str:
    value = float(value or 0.0)
    if value <= 0:
        return "$0"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _summary_line(label: str, value: str) -> str:
    return f"• {label}：<code>{html.escape(value)}</code>"


def _value_at(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _config_dump(svc: Services) -> dict[str, Any]:
    return svc.config.model_dump(by_alias=True)


def _fmt_value(value: Any, spec: SettingSpec) -> str:
    if spec.kind == "bool":
        return "开" if bool(value) else "关"
    if spec.path == "memory.provider":
        return "内置模式" if str(value or "builtin") == "builtin" else "外部模式"
    if value is None:
        return "未设置"
    text = f"{value}"
    if spec.unit:
        text += spec.unit
    return text


def _input_hint(spec: SettingSpec, current_value: Any) -> str:
    example = current_value
    if example is None or example == "":
        example = spec.min_value if spec.min_value is not None else ("文本" if spec.kind == "str" else 1)
    if spec.kind == "bool":
        return "请输入：开 / 关"
    if spec.kind in {"int", "float"}:
        kind = "整数" if spec.kind == "int" else "数字"
        if spec.unit:
            return f"请输入纯{kind}，例如：{example}（单位：{spec.unit}，不要带“{spec.unit}”）"
        return f"请输入{kind}，例如：{example}"
    if spec.path == "web.customUrl":
        return "请输入完整访问地址，例如：https://panel.example.com；留空表示不设置"
    return "请输入新值"


async def _count(svc: Services, sql: str, params: tuple[Any, ...] = ()) -> int:
    cur = await svc.db.conn.execute(sql, params)
    row = await cur.fetchone()
    return int(row[0] or 0) if row else 0


# ---------------------------------------------------------------------------
# /status


async def _running_operation_lines(svc: Services) -> list[str]:
    if not hasattr(svc, "db"):
        return []
    try:
        cur = await svc.db.conn.execute(
            """
            SELECT operation_uuid, chat_id, kind, started_at
            FROM operations
            WHERE status='running'
            ORDER BY started_at ASC, id ASC
            LIMIT 5
            """
        )
        rows = await cur.fetchall()
    except Exception:
        return []
    if not rows:
        return []
    lines = ["", "🚧 <b>高风险操作进行中</b>"]
    for row in rows:
        op = str(row["operation_uuid"] or "")[:8]
        started = _format_clock(int(row["started_at"] or 0))
        lines.append(
            f"• <code>{html.escape(str(row['kind'] or 'operation'))}</code> "
            f"· chat <code>{int(row['chat_id'] or 0)}</code> "
            f"· <code>{html.escape(op)}</code> · {started}"
        )
    return lines


def _safe_tool_count(svc: Services) -> int:
    try:
        return len(svc.tools.names())
    except Exception:
        return 0


def _safe_skill_count(svc: Services) -> int:
    try:
        return len(svc.skills)
    except Exception:
        return 0


async def _status_text(svc: Services, chat_id: int) -> str:
    current = getattr(getattr(svc, "selection", None), "current", "?")
    resolved = svc.config.models.resolve(current) if getattr(svc.config, "models", None) else None
    proto = resolved[0].protocol if resolved else "?"
    compression_models = list(getattr(svc.config.models, "compression_models", []) or [])
    compression_model = " → ".join(compression_models) if compression_models else "跟随主力模型"
    uptime = int(time.time() - float(getattr(svc, "started_at", time.time())))
    running = svc.runs.count() if getattr(svc, "runs", None) is not None else 0
    rath_running = svc.rath.count() if getattr(svc, "rath", None) is not None else 0
    child_count = processes.count()

    # Main-controller and Rath Agent requests now share the per-call
    # model_calls ledger. rath_tasks remains progress metadata; adding it here
    # would count every Agent token and dollar twice.
    totals = await svc.messages.all_time_totals(chat_id)

    total_sessions = int(totals.get("session_count") or 0)
    total_conversations = int(totals.get("conversation_count") or 0)
    total_model_calls = int(totals.get("model_call_count") or 0)
    total_tools = int(totals.get("tool_calls") or 0)
    total_ok = int(totals.get("ok_count") or 0)
    total_fail = int(totals.get("fail_count") or 0)
    total_retry = int(totals.get("retry_count") or 0)
    total_input = int(totals.get("input_tokens") or 0)
    total_output = int(totals.get("output_tokens") or 0)
    total_cache = int(totals.get("cache_read_tokens") or 0) + int(totals.get("cache_write_tokens") or 0)
    total_prompt = total_input + total_cache
    total_cost = float(totals.get("cost_usd") or 0.0)
    total_model_ms = int(totals.get("total_time_ms") or 0)
    total_rate = (total_output / (total_model_ms / 1000.0)) if total_model_ms > 0 else 0.0
    ok_base = total_ok + total_fail
    ok_rate = _format_percent(total_ok / ok_base * 100) if ok_base else "—"
    cache_rate = _format_percent(total_cache / total_prompt * 100) if total_prompt else "—"

    summary_lines = [
        _summary_line("🧵 会话数", str(total_sessions)),
        _summary_line("💬 对话数", str(total_conversations)),
        _summary_line("🔄 模型调用", f"{total_model_calls} 次"),
        _summary_line("🛠 工具调用", f"{total_tools} 次"),
        _summary_line("✅ 成功对话", str(total_ok)),
    ]
    if total_fail > 0:
        summary_lines.append(_summary_line("❌ 失败对话", str(total_fail)))
    if total_retry > 0:
        summary_lines.append(_summary_line("🔁 重试", str(total_retry)))
    summary_lines.extend([
        _summary_line("🎯 成功率", ok_rate),
        _summary_line("⏱ 总耗时", _format_duration(total_model_ms // 1000)),
        _summary_line(
            "📊 Tokens",
            f"↑ {_format_tokens(total_prompt)} · ↓ {_format_tokens(total_output)} · 缓存 {_format_tokens(total_cache)} ({cache_rate})",
        ),
        _summary_line("⚡ 平均 TPS", _format_tps(total_rate)),
        _summary_line("💰 总花费", _format_cost(total_cost)),
    ])
    operation_lines = await _running_operation_lines(svc)
    update_line = f"📦 版本：<code>v{html.escape(__version__)}</code>"
    update = getattr(svc, "update", None)
    if update is not None:
        try:
            snap = update.snapshot()
            latest = (snap.get("latest") or {}).get("version") if isinstance(snap.get("latest"), dict) else ""
            if snap.get("updateAvailable") and latest:
                update_line += f" · 有新版本 <code>v{html.escape(str(latest))}</code>"
            result = snap.get("lastResult") if isinstance(snap.get("lastResult"), dict) else None
            if result and not result.get("acked"):
                status = str(result.get("status") or "")
                if status == "rolled_back":
                    update_line += " · 上次更新已回滚"
                elif status == "failed":
                    update_line += " · 上次更新失败"
        except Exception:
            pass
    return (
        "📊 <b>OpenBear 状态</b>\n\n"
        f"{update_line}\n"
        f"⚙️ 进程：运行中 · uptime <code>{html.escape(_format_uptime(uptime))}</code>\n"
        f"🚦 任务：OpenBear <code>{running}</code> 个 · Rath <code>{rath_running}</code> 个 · 子进程 <code>{child_count}</code> 个\n"
        f"🤖 主力模型：<code>{html.escape(current)}</code> · <code>{html.escape(proto)}</code>\n"
        f"🧹 压缩模型：<code>{html.escape(compression_model)}</code>\n"
        f"🛠 工具：<code>{_safe_tool_count(svc)}</code> · Skills：<code>{_safe_skill_count(svc)}</code>"
        f"{chr(10).join(operation_lines)}\n\n"
        "📊 <b>总计统计</b>\n"
        f"{chr(10).join(summary_lines)}"
    )


def _status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 刷新", callback_data="status:refresh"),
    ]])


@router.message(Command("status"))
async def cmd_status(message: Message, svc: Services) -> None:
    await answer_rich(message, await _status_text(svc, message.chat.id), reply_markup=_status_keyboard())
    await tg_ui.delete_trigger_message(message)


@router.callback_query(F.data == "status:refresh")
async def cb_status_refresh(query: CallbackQuery, svc: Services) -> None:
    if query.message is None:
        await query.answer("无法定位状态消息", show_alert=True)
        return
    await query.answer("已刷新")
    await edit_rich(
        query.bot,
        query.message.chat.id,
        query.message.message_id,
        await _status_text(svc, query.message.chat.id),
        reply_markup=_status_keyboard(),
    )


# ---------------------------------------------------------------------------
# /restart 与 Web 登录二次确认


def _restart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="确认重启", callback_data="restart:confirm"),
        InlineKeyboardButton(text="❌ 取消", callback_data="restart:cancel"),
    ]])


async def _schedule_restart(svc: Services, chat_id: int, *, reason: str = "telegram /restart") -> None:
    await asyncio.sleep(0.5)
    try:
        await schedule_openbear_restart_with_completion_notice(
            svc,
            chat_id=chat_id,
            delay_s=1.0,
            reason=reason,
            requested_by="telegram",
        )
    except Exception:
        log.exception("Telegram /restart 调度失败", 会话=chat_id)


@router.message(Command("restart"))
async def cmd_restart(message: Message, svc: Services) -> None:
    running = svc.runs.count()
    rath_running = svc.rath.count() if getattr(svc, "rath", None) is not None else 0
    child_count = processes.count()
    if running or rath_running or child_count:
        await answer_rich(
            message,
            "⚠️ 当前还有运行中的任务/子进程，重启会中断它们。\n\n"
            f"• 运行中任务：{running}\n"
            f"• Rath 任务：{rath_running}\n"
            f"• 子进程：{child_count}\n"
            f"<pre>{html.escape(processes.summary())}</pre>\n\n"
            "确认要重启吗？",
            reply_markup=_restart_keyboard(),
        )
        return
    await answer_rich(message, "🔄 正在重启 openbear.service …")
    log.info("收到 /restart，触发 systemd 重启")
    asyncio.create_task(_schedule_restart(svc, message.chat.id))


@router.callback_query(F.data == "restart:cancel")
async def cb_restart_cancel(query: CallbackQuery, svc: Services) -> None:
    await query.answer("已取消")
    if query.message:
        with contextlib.suppress(Exception):
            await edit_rich(query.bot, query.message.chat.id, query.message.message_id, "已取消重启")


@router.callback_query(F.data == "restart:confirm")
async def cb_restart_confirm(query: CallbackQuery, svc: Services) -> None:
    await query.answer("正在重启")
    if query.message:
        with contextlib.suppress(Exception):
            await edit_rich(query.bot, query.message.chat.id, query.message.message_id, "🔄 已确认，正在重启 openbear.service …")
    log.info("确认 /restart，触发 systemd 重启", 运行任务=svc.runs.count(), Rath任务=svc.rath.count(), 子进程=processes.count())
    chat_id = int(query.message.chat.id if query.message else query.from_user.id)
    asyncio.create_task(_schedule_restart(svc, chat_id, reason="telegram /restart confirm"))


@router.callback_query(F.data.startswith("web_login:"))
async def cb_web_login_decision(query: CallbackQuery, svc: Services) -> None:
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("请求格式错误", show_alert=True)
        return
    action, req_uuid = parts[1], parts[2]
    approved = action == "approve"
    status = await svc.web_admin.decide_login_request(
        req_uuid,
        approved=approved,
        decided_by=query.from_user.id,
    )
    await query.answer("已确认" if approved else "已拒绝")
    if query.message:
        label = "✅ 已确认登录" if status == "approved" else f"❌ 登录状态：{status}"
        if not approved:
            label = "❌ 已拒绝登录" if status in {"denied", "rejected"} else f"❌ 登录状态：{status}"
        await edit_rich(
            query.bot,
            query.message.chat.id,
            query.message.message_id,
            f"{label}\n\n请求：<code>{html.escape(req_uuid)}</code>",
        )


# ---------------------------------------------------------------------------
# /web 维护入口


async def _web_counts(svc: Services) -> dict[str, int]:
    ts = now_ts()
    return {
        "sessions": await _count(
            svc,
            "SELECT COUNT(*) FROM web_sessions WHERE revoked_at=0 AND expires_at>?",
            (ts,),
        ),
        "pending": await _count(
            svc,
            "SELECT COUNT(*) FROM web_login_requests WHERE status='pending' AND expires_at>?",
            (ts,),
        ),
    }


def _default_route_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return str(sock.getsockname()[0])
    except Exception:
        return "<可访问IP>"


def _web_access_url(svc: Services) -> str:
    host = svc.config.web.host.strip() or "0.0.0.0"
    shown_host = _default_route_ip() if host in {"0.0.0.0", "::"} else host
    return f"http://{shown_host}:{svc.config.web.port}"


def _web_console_keyboard(svc: Services) -> InlineKeyboardMarkup:
    rows = [
        [tg_ui.btn("🔗 绑定地址", "web:edit:web.host"), tg_ui.btn("🔌 绑定端口", "web:edit:web.port")],
        [tg_ui.btn("🔗 自定义地址", "web:edit:web.customUrl"), tg_ui.btn("⏱ Session 有效期", "web:edit:web.sessionDays")],
        [tg_ui.btn("⏱ 登录确认有效期", "web:edit:web.loginRequestTtlSeconds"), tg_ui.btn("⏱ 失败登录冷却时间", "web:edit:web.failedLoginCooldownMinutes")],
        [tg_ui.btn("🔄 重置访问密钥", "web:reset_key_confirm"), tg_ui.btn("👥 当前登录会话", "web:sessions")],
        [tg_ui.btn("🚪 踢出全部 Web 会话", "web:kick_confirm")],
    ]
    return tg_ui.kb(rows)


async def render_web_console_text(svc: Services) -> str:
    web = svc.config.web
    counts = await _web_counts(svc)
    key = await svc.web_admin.ensure_secret_key()
    custom_url = web.custom_url.strip() or "未设置"
    status = "启用" if web.enabled else "停用（需改配置/重启恢复；TG 不提供关闭入口）"
    return "\n".join([
        "🌐 <b>Web 管理台</b>",
        "",
        f"状态：<code>{html.escape(status)}</code>",
        f"绑定：<code>{html.escape(web.host)}:{web.port}</code>",
        f"访问地址：<code>{html.escape(_web_access_url(svc))}</code>",
        f"访问地址（自定义地址）：<code>{html.escape(custom_url)}</code>",
        f"访问密钥：<code>{html.escape(key)}</code>",
        f"当前 Web Session：<code>{counts['sessions']}</code>",
        f"待确认登录：<code>{counts['pending']}</code>",
        "",
        f"Web Session 有效期：<code>{web.session_days}天</code>",
        f"登录确认有效期：<code>{web.login_request_ttl_seconds}秒</code>",
        f"失败登录冷却：<code>{web.failed_login_cooldown_minutes}分钟</code>",
    ])


async def _web_sessions_text(svc: Services) -> str:
    cur = await svc.db.conn.execute(
        """
        SELECT id, chat_id, ip, user_agent, created_at, last_seen_at, expires_at
        FROM web_sessions
        WHERE revoked_at=0 AND expires_at>?
        ORDER BY last_seen_at DESC, id DESC
        LIMIT 10
        """,
        (now_ts(),),
    )
    rows = await cur.fetchall()
    lines = ["👥 <b>当前登录会话</b>", ""]
    if not rows:
        lines.append("暂无有效 Web Session。")
        return "\n".join(lines)
    for idx, row in enumerate(rows, 1):
        ua = html.escape(str(row["user_agent"] or "")[:80])
        lines.append(f"{idx}. Session：<code>#{int(row['id'] or 0)}</code>")
        lines.append(f"用户：<code>{int(row['chat_id'] or 0)}</code> · IP：<code>{html.escape(str(row['ip'] or ''))}</code>")
        lines.append(f"创建：<code>{_fmt_ts(int(row['created_at'] or 0))}</code>")
        lines.append(f"活跃：<code>{_fmt_ts(int(row['last_seen_at'] or 0))}</code> · 过期：<code>{_fmt_ts(int(row['expires_at'] or 0))}</code>")
        if ua:
            lines.append(f"UA：<code>{ua}</code>")
        lines.append("")
    return "\n".join(lines).rstrip()


async def _revoke_all_web_sessions(svc: Services, *, actor: str, chat_id: int) -> int:
    return await svc.web_admin.revoke_all_sessions(
        chat_id=0,
        actor=actor,
        ip=f"telegram:{chat_id}",
    )


@router.message(Command("web"))
async def cmd_web(message: Message, svc: Services) -> None:
    await answer_rich(message, await render_web_console_text(svc), reply_markup=_web_console_keyboard(svc))
    await tg_ui.delete_trigger_message(message)


@router.callback_query(F.data == "web:home")
async def cb_web_home(query: CallbackQuery, svc: Services) -> None:
    if not query.message:
        await query.answer("无法定位菜单消息", show_alert=True)
        return
    tg_ui.clear_pending(query.message.chat.id)
    await query.answer()
    await edit_rich(query.bot, query.message.chat.id, query.message.message_id, await render_web_console_text(svc), reply_markup=_web_console_keyboard(svc))


@router.callback_query(F.data == "web:reset_key_confirm")
async def cb_web_reset_key_confirm(query: CallbackQuery, svc: Services) -> None:
    if not query.message:
        await query.answer("无法定位菜单消息", show_alert=True)
        return
    tg_ui.clear_pending(query.message.chat.id)
    await query.answer("请确认重置")
    await edit_rich(
        query.bot,
        query.message.chat.id,
        query.message.message_id,
        "⚠️ <b>重置访问密钥？</b>\n\n旧访问密钥会立即失效，所有已登录 Web Session 也会被踢出。",
        reply_markup=tg_ui.kb([
            [tg_ui.btn("✅ 确认重置", "web:reset_key")],
            [tg_ui.btn("◀ 返回 Web 管理台", "web:home")],
        ]),
    )


@router.callback_query(F.data == "web:reset_key")
async def cb_web_reset_key(query: CallbackQuery, svc: Services) -> None:
    if not query.message:
        await query.answer("无法定位菜单消息", show_alert=True)
        return
    tg_ui.clear_pending(query.message.chat.id)
    await svc.web_admin.reset_secret_key(actor="telegram", chat_id=query.from_user.id)
    await query.answer("访问密钥已重置")
    await edit_rich(query.bot, query.message.chat.id, query.message.message_id, "✅ 访问密钥已重置。\n\n" + await render_web_console_text(svc), reply_markup=_web_console_keyboard(svc))


@router.callback_query(F.data == "web:sessions")
async def cb_web_sessions(query: CallbackQuery, svc: Services) -> None:
    if not query.message:
        await query.answer("无法定位菜单消息", show_alert=True)
        return
    tg_ui.clear_pending(query.message.chat.id)
    await query.answer()
    await edit_rich(
        query.bot,
        query.message.chat.id,
        query.message.message_id,
        await _web_sessions_text(svc),
        reply_markup=tg_ui.kb([
            [tg_ui.btn("🚪 踢出全部 Web 会话", "web:kick_confirm")],
            [tg_ui.btn("◀ 返回 Web 管理台", "web:home")],
        ]),
    )


@router.callback_query(F.data == "web:kick_confirm")
async def cb_web_kick_confirm(query: CallbackQuery, svc: Services) -> None:
    if not query.message:
        await query.answer("无法定位菜单消息", show_alert=True)
        return
    tg_ui.clear_pending(query.message.chat.id)
    await query.answer("请确认踢出")
    await edit_rich(
        query.bot,
        query.message.chat.id,
        query.message.message_id,
        "⚠️ <b>踢出全部 Web Session？</b>\n\n所有已登录 Web 管理台的浏览器都会失效，访问密钥不会改变。",
        reply_markup=tg_ui.kb([
            [tg_ui.btn("✅ 确认踢出", "web:kick_all")],
            [tg_ui.btn("◀ 返回 Web 管理台", "web:home")],
        ]),
    )


@router.callback_query(F.data == "web:kick_all")
async def cb_web_kick_all(query: CallbackQuery, svc: Services) -> None:
    if not query.message:
        await query.answer("无法定位菜单消息", show_alert=True)
        return
    tg_ui.clear_pending(query.message.chat.id)
    revoked = await _revoke_all_web_sessions(svc, actor="telegram", chat_id=query.from_user.id)
    await query.answer(f"已踢出 {revoked} 个")
    await edit_rich(query.bot, query.message.chat.id, query.message.message_id, f"✅ 已踢出 <code>{revoked}</code> 个 Web Session。\n\n" + await render_web_console_text(svc), reply_markup=_web_console_keyboard(svc))


# ---------------------------------------------------------------------------
# /memory 维护入口


def _memory_specs() -> list[SettingSpec]:
    return [spec for spec in group_specs("memory") if spec.path in _MEMORY_SETTING_PATHS]


def _memory_keyboard(svc: Services) -> InlineKeyboardMarkup:
    buttons = []
    for spec in _memory_specs():
        if spec.path == "memory.provider":
            buttons.append(tg_ui.btn(f"🔁 切换：{spec.title}", "memory:toggle:memory.provider"))
        else:
            buttons.append(tg_ui.btn(f"✏ {spec.title}", f"memory:edit:{spec.path}"))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return tg_ui.kb(rows)


def render_memory_text(svc: Services) -> str:
    title, _paths = GROUPS["memory"]
    data = _config_dump(svc)
    lines = [f"{title}", ""]
    for spec in _memory_specs():
        value = _value_at(data, spec.path)
        shown = "********" if spec.path == "memory.accessKey" and value else _fmt_value(value, spec)
        lines.append(f"<b>{html.escape(spec.title)}</b>：<code>{html.escape(shown)}</code>")
        lines.append(f"{html.escape(spec.desc)}（<code>{html.escape(spec.effect)}</code>）")
        lines.append("")
    return "\n".join(lines).rstrip()


@router.message(Command("memory"))
async def cmd_memory(message: Message, svc: Services) -> None:
    await answer_rich(message, render_memory_text(svc), reply_markup=_memory_keyboard(svc))
    await tg_ui.delete_trigger_message(message)


@router.callback_query(F.data == "memory:home")
async def cb_memory_home(query: CallbackQuery, svc: Services) -> None:
    if not query.message:
        await query.answer("无法定位菜单消息", show_alert=True)
        return
    tg_ui.clear_pending(query.message.chat.id)
    await query.answer()
    await edit_rich(query.bot, query.message.chat.id, query.message.message_id, render_memory_text(svc), reply_markup=_memory_keyboard(svc))


@router.callback_query(F.data == "memory:toggle:memory.provider")
async def cb_memory_provider_toggle(query: CallbackQuery, svc: Services) -> None:
    if not query.message:
        await query.answer("无法定位菜单消息", show_alert=True)
        return
    tg_ui.clear_pending(query.message.chat.id)
    current_provider = svc.config.memory.provider
    next_provider = "external" if current_provider == "builtin" else "builtin"
    try:
        new_cfg = await svc.config_store.update_path("memory.provider", next_provider)
    except Exception as exc:
        await query.answer(f"切换失败：{str(exc)[:160]}", show_alert=True)
        return
    svc.apply_config(new_cfg)
    await query.answer(f"已切换为：{'外部模式' if next_provider == 'external' else '内置模式'}")
    await edit_rich(query.bot, query.message.chat.id, query.message.message_id, render_memory_text(svc), reply_markup=_memory_keyboard(svc))


# ---------------------------------------------------------------------------
# 设置项编辑：/web 与 /memory 共用 SettingSpec / ConfigStore / apply_config


def _setting_panel_for_path(path: str) -> str | None:
    if path in _WEB_SETTING_PATHS:
        return "web"
    if path in _MEMORY_SETTING_PATHS:
        return "memory"
    return None


@router.callback_query(F.data.startswith("web:edit:"))
async def cb_web_setting_edit(query: CallbackQuery, svc: Services) -> None:
    await _begin_setting_edit(query, svc, path=(query.data or "").split(":", 2)[2], panel="web")


@router.callback_query(F.data.startswith("memory:edit:"))
async def cb_memory_setting_edit(query: CallbackQuery, svc: Services) -> None:
    await _begin_setting_edit(query, svc, path=(query.data or "").split(":", 2)[2], panel="memory")


async def _begin_setting_edit(query: CallbackQuery, svc: Services, *, path: str, panel: str) -> None:
    if not query.message:
        await query.answer("无法定位菜单消息", show_alert=True)
        return
    spec = get_spec(path)
    if spec is None or _setting_panel_for_path(path) != panel:
        await query.answer("未知或不可在 Telegram 编辑的设置项", show_alert=True)
        return
    if path == "web.enabled":
        await query.answer("TG 不允许关闭 Web 管理台", show_alert=True)
        return
    if path == "memory.provider":
        await query.answer("记忆模式请用切换按钮", show_alert=True)
        return
    data = _config_dump(svc)
    current_value = _value_at(data, path)
    current = _fmt_value(current_value, spec)
    tg_ui.set_pending(query.message.chat.id, action="admin_setting_edit", message_id=query.message.message_id, data={"path": path, "panel": panel})
    await query.answer("请发送新值")
    hint = _input_hint(spec, current_value)
    back_cb = "web:home" if panel == "web" else "memory:home"
    back_text = "◀ 返回 Web 管理台" if panel == "web" else "◀ 返回记忆服务"
    await edit_rich(
        query.bot,
        query.message.chat.id,
        query.message.message_id,
        f"✏ <b>修改：{html.escape(spec.title)}</b>\n\n"
        f"当前：<code>{html.escape(current)}</code>\n"
        f"说明：{html.escape(spec.desc)}\n"
        f"生效：<code>{spec.effect}</code>\n\n"
        f"{hint}，或返回上级面板。",
        reply_markup=tg_ui.kb([[tg_ui.btn(back_text, back_cb)]]),
    )


def _has_pending_admin_setting(message: Message) -> bool:
    text = (message.text or message.caption or "").strip()
    pending = tg_ui.get_pending(message.chat.id)
    return bool(text) and not text.startswith("/") and pending is not None and pending.action == "admin_setting_edit"


@router.message(_has_pending_admin_setting)
async def on_admin_setting_input(message: Message, svc: Services) -> None:
    pending = tg_ui.pop_pending(message.chat.id)
    if pending is None or pending.action != "admin_setting_edit":
        return
    path = str(pending.data.get("path") or "")
    panel = str(pending.data.get("panel") or "")
    spec = get_spec(path)
    if spec is None or _setting_panel_for_path(path) != panel:
        await answer_rich(message, "❌ 这个设置项已经失效，请重新打开维护面板。")
        return
    raw = (message.text or message.caption or "").strip()
    try:
        value = spec.parse(raw)
    except ValueError as exc:
        tg_ui.set_pending(message.chat.id, action="admin_setting_edit", message_id=pending.message_id, data={"path": path, "panel": panel})
        await answer_rich(
            message,
            f"❌ <b>输入不合法</b>\n\n{html.escape(str(exc))}\n请重新发送 <b>{html.escape(spec.title)}</b> 的新值，或返回原面板。",
        )
        return
    try:
        new_cfg = await svc.config_store.update_path(path, value)
    except Exception as exc:
        tg_ui.set_pending(message.chat.id, action="admin_setting_edit", message_id=pending.message_id, data={"path": path, "panel": panel})
        await answer_rich(
            message,
            f"❌ <b>配置校验失败</b>\n\n<code>{html.escape(str(exc)[:500])}</code>\n请重新输入，或返回原面板。",
        )
        return
    svc.apply_config(new_cfg)
    try:
        if panel == "web":
            await edit_rich(message.bot, message.chat.id, pending.message_id, await render_web_console_text(svc), reply_markup=_web_console_keyboard(svc))
        else:
            await edit_rich(message.bot, message.chat.id, pending.message_id, render_memory_text(svc), reply_markup=_memory_keyboard(svc))
    except Exception:
        pass
    with contextlib.suppress(Exception):
        await message.bot.delete_message(message.chat.id, message.message_id)
