"""OpenBear 控制动作队列。

控制动作与普通 Bash/文件工具解耦：高风险动作先由工具确认并入队，等当前
OpenBear 回合完成渲染、统计和落库后，再在这里执行。这样 restart/new 这类
会改变 OpenBear 自身状态的动作不会把当前回合半路杀掉。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.agent import steering
from app.logging import get_logger
from app.restart_notify import add_restart_completion_notice, remove_restart_completion_notice

log = get_logger("control_actions")


@dataclass(slots=True)
class QueuedControlAction:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    requested_by: str = "model_tool"
    created_at: float = field(default_factory=time.time)


async def schedule_openbear_restart(*, delay_s: float = 1.0) -> None:
    """用 transient unit 调度重启，脱离 openbear.service 当前 cgroup。"""
    delay_s = max(0.0, float(delay_s))
    proc = await asyncio.create_subprocess_exec(
        "systemd-run",
        "--collect",
        f"--on-active={delay_s:g}s",
        "/bin/systemctl",
        "restart",
        "openbear.service",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_raw, stderr_raw = await proc.communicate()
    stdout = (stdout_raw or b"").decode("utf-8", "replace").strip()
    stderr = (stderr_raw or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        log.error(
            "调度 OpenBear 重启失败",
            返回码=proc.returncode,
            stdout=stdout[-1000:],
            stderr=stderr[-1000:],
        )
        detail = (stderr or stdout or "no output")[:500]
        raise RuntimeError(f"systemd-run restart failed rc={proc.returncode}: {detail}")
    log.info(
        "systemd-run 已接受 OpenBear 重启",
        返回码=proc.returncode,
        stdout=stdout[-1000:],
        stderr=stderr[-1000:],
    )


async def resolve_restart_completion_chat_id(svc: Any, chat_id: int) -> int:
    """Return the real management chat id for a restart completion notice.

    Web conversations use negative internal_chat_id values for isolated runtime
    state.  Telegram cannot receive messages at those ids, so completion notices
    must be addressed to web_conversations.owner_chat_id.
    """
    cid = int(chat_id or 0)
    if cid >= 0:
        return cid
    db = getattr(svc, "db", None)
    if db is None:
        log.warning("无法解析 Web internal chat_id：服务缺少 DB", 会话=cid)
        return cid
    try:
        cur = await db.conn.execute(
            "SELECT owner_chat_id FROM web_conversations WHERE internal_chat_id=? LIMIT 1",
            (cid,),
        )
        row = await cur.fetchone()
        owner = int(row["owner_chat_id"] or 0) if row is not None else 0
        if owner > 0:
            return owner
        log.warning("无法解析 Web internal chat_id 对应 owner", 会话=cid)
    except Exception:
        log.exception("解析 Web internal chat_id 对应 owner 失败", 会话=cid)
    return cid


async def schedule_openbear_restart_with_completion_notice(
    svc: Any,
    *,
    chat_id: int,
    delay_s: float = 1.0,
    reason: str = "",
    requested_by: str = "OpenBearControl",
) -> str:
    """Persist a completion notice first, then schedule the self-restart.

    If systemd-run scheduling fails, the just-written notice is removed so the
    next process will not send a false positive completion message.
    """
    notice_chat_id = await resolve_restart_completion_chat_id(svc, chat_id)
    notice_id = ""
    if notice_chat_id > 0:
        notice_id = add_restart_completion_notice(
            svc.config,
            chat_id=notice_chat_id,
            reason=reason,
            requested_by=requested_by,
        )
    else:
        log.warning("跳过重启完成通知：通知 chat_id 无效", 会话=chat_id, 通知会话=notice_chat_id)
    try:
        await schedule_openbear_restart(delay_s=delay_s)
    except Exception:
        if notice_id:
            remove_restart_completion_notice(svc.config, notice_id)
        raise
    log.info("已调度 OpenBear 重启", 会话=chat_id, 通知会话=notice_chat_id, 延迟秒=delay_s, 来源=requested_by)
    return notice_id


class ControlActionQueue:
    """按 chat 保存 post-turn 控制动作和软停止信号。"""

    def __init__(self) -> None:
        self._after_turn: dict[int, list[QueuedControlAction]] = {}
        self._soft_stop: dict[int, str] = {}
        self._retry_cancel: set[int] = set()

    def enqueue_after_turn(
        self,
        chat_id: int,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        reason: str = "",
        requested_by: str = "model_tool",
    ) -> None:
        item = QueuedControlAction(
            action=action,
            args=dict(args or {}),
            reason=reason,
            requested_by=requested_by,
        )
        self._after_turn.setdefault(int(chat_id), []).append(item)
        log.info("已加入 post-turn 控制动作", 会话=chat_id, 动作=action, 来源=requested_by)

    def pending_count(self, chat_id: int | None = None) -> int:
        if chat_id is not None:
            return len(self._after_turn.get(int(chat_id), []))
        return sum(len(v) for v in self._after_turn.values())

    def request_soft_stop(self, chat_id: int, reason: str = "") -> None:
        self._soft_stop[int(chat_id)] = reason or "OpenBearControl stop requested"
        log.info("已设置当前回合软停止信号", 会话=chat_id, 原因=reason[:120])

    def consume_soft_stop(self, chat_id: int) -> str:
        return self._soft_stop.pop(int(chat_id), "")

    def request_retry_cancel(self, chat_id: int) -> None:
        self._retry_cancel.add(int(chat_id))
        log.info("已设置模型重试取消信号", 会话=chat_id)

    def consume_retry_cancel(self, chat_id: int) -> bool:
        cid = int(chat_id)
        if cid not in self._retry_cancel:
            return False
        self._retry_cancel.discard(cid)
        return True

    def clear_retry_cancel(self, chat_id: int) -> None:
        self._retry_cancel.discard(int(chat_id))

    async def drain_after_turn(self, svc: Any, chat_id: int) -> list[str]:
        """执行当前 chat 的 post-turn 动作；返回执行摘要。"""
        items = self._after_turn.pop(int(chat_id), [])
        results: list[str] = []
        for item in items:
            try:
                results.append(await self._execute_one(svc, int(chat_id), item))
            except Exception as exc:
                log.exception("post-turn 控制动作失败", 会话=chat_id, 动作=item.action)
                results.append(f"{item.action}: failed {type(exc).__name__}: {exc}")
        return results

    async def _execute_one(self, svc: Any, chat_id: int, item: QueuedControlAction) -> str:
        action = item.action
        if action == "restart":
            delay_s = float(item.args.get("delayS") or item.args.get("delay_s") or 1.0)
            await schedule_openbear_restart_with_completion_notice(
                svc,
                chat_id=chat_id,
                delay_s=delay_s,
                reason=item.reason,
                requested_by=item.requested_by,
            )
            return "restart: scheduled"
        if action == "new":
            await self._do_new_session(svc, chat_id, source=item.requested_by)
            log.info("post-turn 新建会话完成", 会话=chat_id)
            return "new: done"
        log.warning("未知 post-turn 控制动作", 会话=chat_id, 动作=action)
        return f"{action}: unknown"

    async def _do_new_session(self, svc: Any, chat_id: int, *, source: str) -> None:
        """清空当前会话。复制新会话的底层行为。"""
        async with svc.operation_locks.chat(chat_id, "new"):
            current_session_uuid = getattr(svc.messages, "current_session_uuid", None)
            session_uuid = await current_session_uuid(chat_id) if current_session_uuid is not None else ""
            rath_dao = getattr(svc, "rath_dao", None)
            if session_uuid and rath_dao is not None:
                await rath_dao.close_agent_sessions_for_openbear_session(
                    session_uuid,
                    reason="openbear_control_new_session",
                )
            steering.clear(chat_id)
            await svc.messages.clear(chat_id)
            await svc.messages.reset_turn_stats(chat_id)

    async def shutdown(self) -> None:
        self._after_turn.clear()
        self._soft_stop.clear()
        self._retry_cancel.clear()
