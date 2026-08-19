"""运行中子进程登记表。

OpenBear 当前只有 Bash 工具会拉起子进程。登记表用于：
- /restart 前发现还有命令在跑，提醒确认；
- stop 或 task cancel 时确保子进程组被杀掉，避免孤儿进程。
"""
from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    command: str
    started_at: float
    cwd: str
    blocks_restart: bool = True
    chat_id: int = 0
    session_uuid: str = ""
    task_uuid: str = ""
    source: str = ""
    turn_uuid: str = ""
    run_root_turn_uuid: str = ""


_PROCS: dict[int, ProcessInfo] = {}


def register(
    pid: int | None,
    *,
    command: str,
    cwd: str = "",
    blocks_restart: bool = True,
    chat_id: int = 0,
    session_uuid: str = "",
    task_uuid: str = "",
    source: str = "",
    turn_uuid: str = "",
    run_root_turn_uuid: str = "",
) -> None:
    if pid is None:
        return
    _PROCS[pid] = ProcessInfo(
        pid=pid,
        command=command,
        started_at=time.time(),
        cwd=cwd,
        blocks_restart=blocks_restart,
        chat_id=int(chat_id or 0),
        session_uuid=str(session_uuid or ""),
        task_uuid=str(task_uuid or ""),
        source=str(source or ""),
        turn_uuid=str(turn_uuid or ""),
        run_root_turn_uuid=str(run_root_turn_uuid or ""),
    )


def unregister(pid: int | None) -> None:
    if pid is None:
        return
    _PROCS.pop(pid, None)


def active() -> list[ProcessInfo]:
    live: list[ProcessInfo] = []
    for pid, info in list(_PROCS.items()):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _PROCS.pop(pid, None)
            continue
        except PermissionError:
            pass
        live.append(info)
    return live


def count(*, blocking_only: bool = True) -> int:
    items = active()
    if blocking_only:
        items = [info for info in items if info.blocks_restart]
    return len(items)


def summary(limit: int = 5, *, blocking_only: bool = True) -> str:
    items = active()
    if blocking_only:
        items = [info for info in items if info.blocks_restart]
    if not items:
        return "无"
    lines: list[str] = []
    now = time.time()
    for info in items[:limit]:
        age = int(now - info.started_at)
        cmd = info.command.replace("\n", " ")[:120]
        lines.append(f"PID {info.pid} · {age}s · {cmd}")
    if len(items) > limit:
        lines.append(f"… 还有 {len(items) - limit} 个")
    return "\n".join(lines)


def kill_process_group(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def active_for_chat(chat_id: int) -> list[ProcessInfo]:
    target = int(chat_id or 0)
    if not target:
        return []
    return [info for info in active() if int(info.chat_id or 0) == target]


def kill_for_chat(chat_id: int) -> int:
    target = int(chat_id or 0)
    if not target:
        return 0
    killed = 0
    for pid, info in list(_PROCS.items()):
        if int(info.chat_id or 0) != target:
            continue
        kill_process_group(pid)
        _PROCS.pop(pid, None)
        killed += 1
    return killed


def kill_for_task(task_uuid: str) -> int:
    target = str(task_uuid or "").strip()
    if not target:
        return 0
    killed = 0
    for pid, info in list(_PROCS.items()):
        if str(info.task_uuid or "") != target:
            continue
        kill_process_group(pid)
        _PROCS.pop(pid, None)
        killed += 1
    return killed
