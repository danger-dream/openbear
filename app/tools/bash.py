"""Bash 工具 —— 执行 shell 命令。

每次独立 subprocess（不做跨调用持久 shell）；超时/取消都会杀进程组。
参考 Claude Code 做了三类增强：
- 大输出落盘：完整 stdout/stderr 写入 data/tool_artifacts/bash-output，可用 Read 分段查看。
- 运行中进度：前台长命令会通过 ToolRuntimeContext.progress_update 刷新最近输出。
- 完成语义：Bash 工具调用始终等待子进程结束；兼容的 background 参数不再分离任务。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging import get_logger
from app.tools import processes
from app.tools.base import ToolRegistry, current_tool_context

log = get_logger("tools.bash")

_READ_CHUNK = 8192
_PROGRESS_AFTER_S = 2.0
_PROGRESS_INTERVAL_S = 2.0
_DEFAULT_SPOOL_MAX_BYTES = 64 * 1024 * 1024
_OPENBEAR_SELF_TARGET_RE = r"(?:app\.main|openbear(?:\.service)?)"
_SELF_SERVICE_RE = re.compile(
    r"(?:^|[;&|()\s])(?:sudo\s+)?(?:/bin/|/usr/bin/)?systemctl\s+"
    r"(?:restart|stop|kill|try-restart|reload-or-restart|reload|force-reload)\s+openbear(?:\.service)?\b"
    r"|(?:^|[;&|()\s])(?:sudo\s+)?(?:/sbin/|/usr/sbin/|/bin/|/usr/bin/)?service\s+openbear\s+(?:restart|stop|reload|force-reload)\b"
    r"|(?:^|[;&|()\s])(?:sudo\s+)?(?:pkill|killall|fuser)\b[^\n;&|]*\b" + _OPENBEAR_SELF_TARGET_RE + r"\b"
    r"|(?:^|[;&|()\s])(?:sudo\s+)?(?:/bin/|/usr/bin/)?kill\b[^\n;&|]*"
    r"(?:\$\([^)]*\b(?:pgrep|pidof)\b[^)]*\b" + _OPENBEAR_SELF_TARGET_RE + r"\b[^)]*\)"
    r"|`[^`]*\b(?:pgrep|pidof)\b[^`]*\b" + _OPENBEAR_SELF_TARGET_RE + r"\b[^`]*`)"
    r"|\b(?:ps|pgrep|pidof)\b[^\n;&]*\b" + _OPENBEAR_SELF_TARGET_RE + r"\b[^\n;&|]*\|[^\n;&]*\b(?:xargs\s+)?(?:/bin/|/usr/bin/)?kill\b",
    re.I,
)
_SLEEP_FIRST_RE = re.compile(r"^\s*sleep\s+(\d+)(?:\s*(?:&&|;).*)?\s*$", re.S)

_SEARCH_EXIT_SEMANTICS = {
    "grep": {1: "no matches"},
    "rg": {1: "no matches"},
    "diff": {1: "files differ"},
    "test": {1: "condition false"},
    "[": {1: "condition false"},
}
_SILENT_COMMANDS = {"mv", "cp", "rm", "mkdir", "rmdir", "chmod", "chown", "chgrp", "touch", "ln", "cd", "true"}


@dataclass(slots=True)
class BashJob:
    job_id: str
    pid: int
    command: str
    cwd: str
    output_path: str
    started_at: float
    timeout_s: float
    status: str = "running"
    completed_at: float = 0.0
    returncode: int | None = None
    output_bytes: int = 0
    error: str = ""
    auto_backgrounded: bool = False
    chat_id: int = 0
    session_uuid: str = ""
    turn_uuid: str = ""
    run_root_turn_uuid: str = ""
    tool_call_id: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)
    poll_offset: int = 0


_JOBS: dict[str, BashJob] = {}
_PROCESS_TERMINAL_TTL_S = 6 * 60 * 60
_PROCESS_TERMINAL_KEEP = 50
_PROCESS_LIST_TERMINAL_KEEP = 20
_PROCESS_LOG_TAIL_BYTES = 512 * 1024
_PROCESS_DELTA_MAX_BYTES = 256 * 1024


def _job_finished_at(job: BashJob) -> float:
    return float(job.completed_at or (job.started_at + max(0.0, float(job.timeout_s or 0.0))) or job.started_at)


def _cleanup_terminal_jobs(*, force: bool = False) -> None:
    now = time.time()
    terminal = [job for job in _JOBS.values() if job.status != "running"]
    if not terminal:
        return
    expired = {job.job_id for job in terminal if force or (job.completed_at and now - job.completed_at > _PROCESS_TERMINAL_TTL_S)}
    survivors = sorted((job for job in terminal if job.job_id not in expired), key=_job_finished_at, reverse=True)
    for job in survivors[_PROCESS_TERMINAL_KEEP:]:
        expired.add(job.job_id)
    for job_id in expired:
        _JOBS.pop(job_id, None)


def _embedded_shell_commands(command: str) -> list[str]:
    """Return obvious `sh -c` / `bash -lc` payloads for self-control checks.

    This is deliberately a guardrail, not a sandbox. Bash remains a general
    shell tool; we only catch common accidental OpenBear self-stop/restart
    forms and route them to OpenBearControl.
    """
    try:
        tokens = shlex.split(command or "", posix=True)
    except ValueError:
        return []
    payloads: list[str] = []
    shell_bins = {"sh", "bash", "dash", "zsh", "ksh"}
    for idx, token in enumerate(tokens):
        base = os.path.basename(str(token or ""))
        if base not in shell_bins:
            continue
        j = idx + 1
        while j < len(tokens):
            opt = str(tokens[j] or "")
            if not opt.startswith("-"):
                j += 1
                continue
            if "c" in opt[1:]:
                if j + 1 < len(tokens):
                    payloads.append(str(tokens[j + 1] or ""))
                break
            j += 1
    return payloads


def _blocks_openbear_self_control(command: str) -> bool:
    text = command or ""
    if _SELF_SERVICE_RE.search(text):
        return True
    return any(_blocks_openbear_self_control(payload) for payload in _embedded_shell_commands(text) if payload and payload != text)


def _detect_blocked_sleep(command: str) -> str | None:
    first = re.split(r"&&|;|\|", command.strip(), maxsplit=1)[0].strip()
    m = re.match(r"^sleep\s+(\d+)\s*$", first)
    if not m:
        return None
    secs = int(m.group(1))
    if secs < 2:
        return None
    return f"sleep {secs}"


def _node_version_key(bin_dir: Path) -> tuple[int, ...]:
    """Return a sortable version tuple for ~/.nvm/versions/node/vX.Y.Z/bin."""
    name = bin_dir.parent.name.lstrip("v")
    parts: list[int] = []
    for piece in name.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def _iter_nvm_bin_dirs(root: Path) -> list[Path]:
    """List nvm version bin dirs. Unreadable roots must not break Bash."""
    try:
        if not root.is_dir():
            return []
        found: list[Path] = []
        for bin_dir in root.glob("*/bin"):
            try:
                if (bin_dir / "node").exists() or (bin_dir / "npm").exists() or (bin_dir / "pnpm").exists():
                    found.append(bin_dir)
            except OSError:
                continue
        return found
    except OSError:
        return []


def _nvm_node_bins() -> list[str]:
    """Discover Node/NPM installed by nvm for non-interactive service shells."""
    roots = [Path.home() / ".nvm" / "versions" / "node"]
    root_nvm = Path("/root/.nvm/versions/node")
    if root_nvm not in roots:
        roots.append(root_nvm)
    bins: list[Path] = []
    for root in roots:
        bins.extend(_iter_nvm_bin_dirs(root))
    resolved: set[Path] = set()
    for path in bins:
        try:
            resolved.add(path.resolve())
        except OSError:
            continue
    return [str(path) for path in sorted(resolved, key=_node_version_key, reverse=True)]


def _shell_env() -> dict[str, str]:
    """Build a stable shell environment for Bash tool subprocesses."""
    env = os.environ.copy()
    candidates: list[str] = []
    extra = env.get("OPENBEAR_BASH_EXTRA_PATH", "")
    if extra:
        candidates.extend(extra.split(os.pathsep))
    candidates.extend(_nvm_node_bins())
    candidates.extend((env.get("PATH") or "").split(os.pathsep))
    candidates.extend([
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ])

    seen: set[str] = set()
    path_parts: list[str] = []
    for item in candidates:
        item = item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        path_parts.append(item)
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def _artifact_dir() -> Path:
    ctx = current_tool_context()
    session = ctx.session_uuid or f"chat-{ctx.chat_id or 0}"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session)[:120] or "session"
    path = Path.cwd() / "data" / "tool_artifacts" / "bash-output" / safe
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class SpoolingTextBuffer:
    """完整落盘 + 内存 head/tail 预览，避免海量输出撑爆内存。"""

    def __init__(self, *, inline_limit: int, spool_max_bytes: int = _DEFAULT_SPOOL_MAX_BYTES) -> None:
        self.inline_limit = max(1, int(inline_limit))
        self.spool_max_bytes = max(self.inline_limit, int(spool_max_bytes))
        self.head_cap = max(1, self.inline_limit // 2)
        self.tail_cap = max(1, self.inline_limit - self.head_cap)
        self.head = ""
        self.tail = ""
        self.total_chars = 0
        self.total_bytes = 0
        self.output_path = str(_artifact_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.log")
        self._fh = open(self.output_path, "ab", buffering=0)
        try:
            os.chmod(self.output_path, 0o600)
        except OSError:
            pass
        self.overflowed = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self.total_bytes + len(chunk) > self.spool_max_bytes:
            allowed = max(0, self.spool_max_bytes - self.total_bytes)
            if allowed:
                self._write_and_preview(chunk[:allowed])
            self.overflowed = True
            return
        self._write_and_preview(chunk)

    def _write_and_preview(self, chunk: bytes) -> None:
        self._fh.write(chunk)
        self.total_bytes += len(chunk)
        text = chunk.decode("utf-8", "replace")
        self.total_chars += len(text)
        rest = text
        if len(self.head) < self.head_cap:
            take = min(self.head_cap - len(self.head), len(rest))
            self.head += rest[:take]
            rest = rest[take:]
        if rest:
            self.tail = (self.tail + rest)[-self.tail_cap:]

    @property
    def truncated(self) -> bool:
        return self.total_chars > len(self.head) + len(self.tail) or self.overflowed

    def render_preview(self) -> str:
        if not self.truncated:
            return self.head + self.tail
        note = (
            f"\n…[原始输出约 {self.total_bytes} 字节，已截断回灌；"
            f"完整输出见 {self.output_path}；保留前 {len(self.head)} + 后 {len(self.tail)} 字符]\n"
        )
        return self.head + note + self.tail

    def tail_preview(self, max_chars: int = 1200) -> str:
        text = self.tail or self.head
        return text[-max_chars:].strip()

    def close(self) -> None:
        try:
            self._fh.flush()
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


async def _kill_and_wait(proc: asyncio.subprocess.Process) -> None:
    processes.kill_process_group(proc.pid)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except Exception:
        pass


def _base_command(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return ""
    # 轻量提取最后一个简单命令；不作为安全判断，只用于展示/退出码语义。
    parts = re.split(r"&&|;|\|", stripped)
    last = parts[-1].strip() if parts else stripped
    return last.split()[0] if last.split() else ""


def _exit_interpretation(command: str, rc: int) -> tuple[bool, str]:
    if rc == 0:
        return False, ""
    base = _base_command(command)
    message = _SEARCH_EXIT_SEMANTICS.get(base, {}).get(rc)
    if message:
        return False, message
    return True, f"exit code {rc}"


def _is_silent_success(command: str) -> bool:
    base = _base_command(command)
    return base in _SILENT_COMMANDS


def _progress_line(command: str, buf: SpoolingTextBuffer, started_at: float, *, pid: int | None = None) -> str:
    elapsed = int(time.monotonic() - started_at)
    preview = buf.tail_preview(800)
    cmd = command.replace("\n", " ")[:80]
    head = f"💻 Bash 运行中 · {elapsed}s · {buf.total_bytes} bytes"
    if pid:
        head += f" · PID {pid}"
    if preview:
        preview = preview.replace("<", "‹").replace(">", "›")
        return f"{head}\n最近输出：\n{preview}"
    return f"{head}\n命令：{cmd}"


async def _consume_proc(
    proc: asyncio.subprocess.Process,
    *,
    command: str,
    timeout: float,
    buf: SpoolingTextBuffer,
    progress_update=None,
    foreground: bool = True,
    detach_after_s: float = 0.0,
) -> tuple[int, bool, str, bool]:
    assert proc.stdout is not None
    started = time.monotonic()
    deadline = started + timeout if timeout > 0 else 0
    detach_deadline = started + detach_after_s if foreground and detach_after_s > 0 else 0
    last_progress = 0.0
    read_task: asyncio.Task[bytes] | None = asyncio.create_task(proc.stdout.read(_READ_CHUNK))
    timed_out = False
    overflow_killed = False
    try:
        while True:
            now = time.monotonic()
            if deadline and now >= deadline:
                timed_out = True
                await _kill_and_wait(proc)
                break
            wait_s = 0.5
            if deadline:
                wait_s = min(wait_s, max(0.05, deadline - now))
            if detach_deadline:
                wait_s = min(wait_s, max(0.05, detach_deadline - now))
            done, _pending = await asyncio.wait({read_task}, timeout=wait_s)
            if not done:
                now = time.monotonic()
                if foreground and progress_update and now - started >= _PROGRESS_AFTER_S and now - last_progress >= _PROGRESS_INTERVAL_S:
                    last_progress = now
                    await progress_update(_progress_line(command, buf, started, pid=proc.pid))
                if detach_deadline and now >= detach_deadline and proc.returncode is None:
                    if read_task and not read_task.done():
                        read_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await read_task
                    read_task = None
                    return 0, False, f"auto-backgrounded after {detach_after_s}s", True
                continue
            chunk = read_task.result()
            if not chunk:
                break
            buf.append(chunk)
            if buf.overflowed:
                overflow_killed = True
                await _kill_and_wait(proc)
                break
            read_task = asyncio.create_task(proc.stdout.read(_READ_CHUNK))
            now = time.monotonic()
            if foreground and progress_update and now - started >= _PROGRESS_AFTER_S and now - last_progress >= _PROGRESS_INTERVAL_S:
                last_progress = now
                await progress_update(_progress_line(command, buf, started, pid=proc.pid))
        rc = await proc.wait()
    except asyncio.CancelledError:
        if read_task and not read_task.done():
            read_task.cancel()
        await _kill_and_wait(proc)
        raise
    finally:
        if read_task and not read_task.done():
            read_task.cancel()
    if timed_out:
        return rc if "rc" in locals() else 124, True, f"timeout after {timeout}s; process terminated", False
    if overflow_killed:
        return rc if "rc" in locals() else 137, True, f"output exceeded safety limit ({buf.spool_max_bytes} bytes); process terminated", False
    return rc, False, "", False


def _format_bash_result(command: str, rc: int, buf: SpoolingTextBuffer, *, interrupted: bool, note: str = "") -> str:
    is_error, semantic = _exit_interpretation(command, rc)
    preview = buf.render_preview().rstrip()
    status = "timeout" if interrupted else "failed" if is_error else "ok"
    lines: list[str] = [
        f"status: {status}",
        f"exit_code: {rc}",
        f"output_path: {buf.output_path}",
        f"output_bytes: {buf.total_bytes}",
        f"truncated: {str(buf.truncated).lower()}",
    ]
    if note:
        lines.append(f"note: {note}")
    elif semantic:
        lines.append(f"note: {semantic}")
    lines.append("output:")
    if preview:
        lines.append(preview)
    elif rc == 0 and _is_silent_success(command):
        lines.append("(completed with no output)")
    else:
        lines.append("(no output)")
    return "\n".join(lines)



async def _spawn(command: str, cwd: str | None) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=_shell_env(),
        start_new_session=True,
    )




def make_bash_tool(default_timeout_s: float = 120.0, output_limit: int = 30000,
                   max_timeout_s: float = 600.0, spool_max_bytes: int = _DEFAULT_SPOOL_MAX_BYTES,
                   auto_background_after_s: float = 15.0):
    async def _bash(args: dict[str, Any]) -> str:
        command = str(args.get("command", ""))
        if not command.strip():
            return "error: missing command"
        if _blocks_openbear_self_control(command):
            return (
                "error: OpenBear service-control commands are not allowed through Bash. "
                "Use OpenBearControl(action=\"restart\"/\"stop\"); "
                "high-risk actions require confirmation and run after the current reply is finalized."
            )
        # `background` / `run_in_background` remain accepted for API compatibility,
        # but Bash is deliberately not a detached orchestration primitive. The
        # controller awaits the command's terminal result in this same tool call;
        # only Agent owns post-turn completion notifications.
        background_requested = bool(args.get("background", False) or args.get("run_in_background", False))
        sleep = _detect_blocked_sleep(command)
        if sleep and float(args.get("timeout", default_timeout_s) or default_timeout_s) > 5:
            return f"error: blocked long sleep detected ({sleep}); use a real command with a bounded timeout"
        try:
            requested_timeout = float(args.get("timeout", default_timeout_s) or default_timeout_s)
        except (TypeError, ValueError):
            return "error: timeout must be a number of seconds"
        timeout = max(1.0, min(requested_timeout, max_timeout_s))
        cwd = args.get("cwd") or None
        expanded_cwd = os.path.expanduser(str(cwd)) if cwd else None
        if expanded_cwd and not os.path.isdir(expanded_cwd):
            return f"error: cwd does not exist: {cwd}"
        log.info("执行Bash", 命令=command[:200], 超时=timeout, 请求超时=requested_timeout, 目录=cwd or ".", 请求后台=background_requested, 实际后台=False)
        try:
            proc = await _spawn(command, expanded_cwd)
        except Exception as e:
            return f"error: failed to start process: {e}"

        ctx = current_tool_context()
        processes.register(
            proc.pid,
            command=command,
            cwd=expanded_cwd or os.getcwd(),
            chat_id=ctx.chat_id,
            session_uuid=ctx.session_uuid,
            task_uuid=ctx.task_uuid,
            source=ctx.source,
            turn_uuid=ctx.turn_uuid,
            run_root_turn_uuid=ctx.run_root_turn_uuid or ctx.turn_uuid,
        )
        buf = SpoolingTextBuffer(inline_limit=output_limit, spool_max_bytes=spool_max_bytes)
        progress_update = ctx.progress_update
        detached = False
        # Retained as a function/config compatibility argument; automatic detach
        # is disabled so a Bash call cannot outlive its owning controller step.
        detach_after_s = 0.0
        try:
            rc, interrupted, note, detached = await _consume_proc(
                proc,
                command=command,
                timeout=timeout,
                buf=buf,
                progress_update=progress_update,
                foreground=True,
                detach_after_s=detach_after_s,
            )
        except asyncio.CancelledError:
            await _kill_and_wait(proc)
            raise
        finally:
            if not detached:
                processes.unregister(proc.pid)
                buf.close()
        if buf.truncated:
            log.info("Bash输出已落盘并截断回灌", 命令=command[:120], 字节=buf.total_bytes, 路径=buf.output_path)
        return _format_bash_result(command, rc, buf, interrupted=interrupted, note=note)

    return _bash


_DEFAULT_PROCESS_LOG_TAIL_LINES = 200
_MAX_PROCESS_LOG_LINES = 2000
_MAX_PROCESS_POLL_OUTPUT_CHARS = 6000
_MAX_PROCESS_POLL_WAIT_MS = 120_000


def _process_session_id(args: dict[str, Any]) -> str:
    return str(args.get("sessionId") or args.get("session_id") or args.get("jobId") or args.get("job_id") or "").strip()


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _process_poll_wait_ms(value: Any) -> int:
    return max(0, min(_MAX_PROCESS_POLL_WAIT_MS, _coerce_int(value, 0)))


def _job_elapsed_s(job: BashJob) -> int:
    return max(0, int(time.time() - job.started_at))


def _refresh_job_output_bytes(job: BashJob) -> int:
    try:
        path = Path(job.output_path)
        if path.exists():
            job.output_bytes = path.stat().st_size
    except OSError:
        pass
    return int(job.output_bytes or 0)


def _read_job_tail(job: BashJob, max_bytes: int = 2000) -> str:
    try:
        path = Path(job.output_path)
        if not path.exists():
            return ""
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _read_job_delta(job: BashJob, max_chars: int = _MAX_PROCESS_POLL_OUTPUT_CHARS) -> str:
    try:
        path = Path(job.output_path)
        if not path.exists():
            return ""
        size = path.stat().st_size
        start = max(0, min(int(job.poll_offset or 0), size))
        unread = max(0, size - start)
        read_start = start if unread <= _PROCESS_DELTA_MAX_BYTES else max(0, size - _PROCESS_DELTA_MAX_BYTES)
        with path.open("rb") as f:
            f.seek(read_start)
            data = f.read(_PROCESS_DELTA_MAX_BYTES)
        if read_start > start:
            data = "…[delta byte window truncated; showing latest bytes]\n".encode() + data
        job.poll_offset = size
        job.output_bytes = size
    except OSError:
        return ""
    if not data:
        return ""
    text = data.decode("utf-8", "replace").strip()
    if len(text) <= max_chars:
        return text
    return f"…[delta truncated; showing last {max_chars} chars of {len(text)}]\n{text[-max_chars:]}"


def _count_file_lines(path: Path, size: int) -> int:
    if size <= 0:
        return 0
    count = 0
    last = b""
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            count += chunk.count(b"\n")
            last = chunk[-1:]
    if last and last not in {b"\n", b"\r"}:
        count += 1
    return count


def _read_job_log_window(job: BashJob, *, offset: Any = None, limit: Any = None) -> tuple[str, int, int, bool]:
    try:
        path = Path(job.output_path)
        if not path.exists():
            return "", 0, 0, False
        size = path.stat().st_size
        using_default_tail = offset is None and limit is None
        if using_default_tail:
            total_lines = _count_file_lines(path, size)
            with path.open("rb") as f:
                f.seek(max(0, size - _PROCESS_LOG_TAIL_BYTES))
                raw_tail = f.read()
            raw = raw_tail.decode("utf-8", errors="replace")
            lines = raw.splitlines()
            selected = lines[-_DEFAULT_PROCESS_LOG_TAIL_LINES:]
            text = "\n".join(selected).strip()
            if size > _PROCESS_LOG_TAIL_BYTES:
                prefix = f"[showing tail window from last {_PROCESS_LOG_TAIL_BYTES} bytes of {size} bytes]"
                text = prefix + ("\n" + text if text else "")
            if total_lines > _DEFAULT_PROCESS_LOG_TAIL_LINES:
                text = (text + "\n\n" if text else "") + f"[showing last {_DEFAULT_PROCESS_LOG_TAIL_LINES} of {total_lines} lines; pass offset/limit to page]"
            return text, total_lines, size, True
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", 0, 0, False
    lines = raw.splitlines()
    total_lines = len(lines)
    total_chars = len(raw)
    start = max(0, _coerce_int(offset, 0))
    count = _coerce_int(limit, _DEFAULT_PROCESS_LOG_TAIL_LINES)
    count = max(1, min(_MAX_PROCESS_LOG_LINES, count))
    selected = lines[start:start + count]
    text = "\n".join(selected).strip()
    return text, total_lines, total_chars, False


def _format_process_job_header(job: BashJob) -> list[str]:
    _refresh_job_output_bytes(job)
    return [
        f"status: {job.status}",
        f"session_id: {job.job_id}",
        f"job_id: {job.job_id}",
        f"pid: {job.pid}",
        f"exit_code: {job.returncode}",
        f"output_path: {job.output_path}",
        f"output_bytes: {job.output_bytes}",
        f"elapsed_s: {_job_elapsed_s(job)}",
    ]


def _terminate_job(job: BashJob, *, reason: str = "terminated by Process") -> bool:
    if job.status != "running":
        return False
    processes.kill_process_group(job.pid)
    if job.task:
        job.task.cancel()
    job.status = "killed"
    job.error = reason
    processes.unregister(job.pid)
    return True


async def _process_tool(args: dict[str, Any]) -> str:
    _cleanup_terminal_jobs()
    action = str(args.get("action") or "").strip().lower()
    if not action:
        return "error: missing action"
    if action == "wait":
        return "error: Process action wait is not available; use action=poll with timeout milliseconds."
    if action == "list":
        running_jobs = [job for job in _JOBS.values() if job.status == "running"]
        terminal_jobs = sorted((job for job in _JOBS.values() if job.status != "running"), key=_job_finished_at, reverse=True)[:_PROCESS_LIST_TERMINAL_KEEP]
        jobs = sorted(running_jobs, key=lambda item: item.started_at, reverse=True) + terminal_jobs
        if not jobs:
            return "No running or recent Bash sessions."
        lines: list[str] = []
        for job in jobs:
            _refresh_job_output_bytes(job)
            label = job.command.replace("\n", " ")[:120]
            line = f"{job.job_id} {job.status:9} {_job_elapsed_s(job)}s pid={job.pid} bytes={job.output_bytes} :: {label}"
            tail = _read_job_tail(job, 300).replace("\n", " ")
            if tail:
                line += f"\n  tail: {tail[-300:]}"
            lines.append(line)
        return "\n".join(lines)

    session_id = _process_session_id(args)
    if not session_id:
        return "error: sessionId is required for this action"
    job = _JOBS.get(session_id)
    if job is None:
        return f"error: Bash background session not found: {session_id}"

    if action == "poll":
        wait_ms = _process_poll_wait_ms(args.get("timeout"))
        if wait_ms > 0 and job.status == "running":
            deadline = time.monotonic() + wait_ms / 1000.0
            while job.status == "running" and time.monotonic() < deadline:
                await asyncio.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
        output_delta = _read_job_delta(job)
        lines = _format_process_job_header(job)
        if job.auto_backgrounded:
            lines.append("auto_backgrounded: true")
        if job.error:
            lines.append(f"error: {job.error}")
        lines.append("output_delta:")
        lines.append(output_delta or "(no new output)")
        if job.status == "running":
            lines.append("Process still running.")
        elif job.status == "killed":
            lines.append("Process killed.")
        elif job.returncode is not None:
            lines.append(f"Process exited with code {job.returncode}.")
        else:
            lines.append(f"Process finished with status {job.status}.")
        return "\n".join(lines)

    if action == "log":
        text, total_lines, total_chars, _using_default_tail = _read_job_log_window(
            job,
            offset=args.get("offset"),
            limit=args.get("limit"),
        )
        lines = _format_process_job_header(job)
        lines.extend([
            f"total_lines: {total_lines}",
            f"total_chars: {total_chars}",
            "output:",
            text or "(no output recorded)",
        ])
        return "\n".join(lines)

    if action == "kill":
        terminated = _terminate_job(job, reason="terminated by Process kill")
        if terminated:
            job.completed_at = time.time()
        if not terminated:
            return f"status: no_action\nsession_id: {session_id}\ncurrent_status: {job.status}"
        return "\n".join([
            "status: killed",
            f"session_id: {job.job_id}",
            f"job_id: {job.job_id}",
            f"pid: {job.pid}",
            f"output_path: {job.output_path}",
        ])

    if action == "remove":
        was_running = job.status == "running"
        if was_running:
            _terminate_job(job, reason="terminated by Process remove")
            job.completed_at = time.time()
        _JOBS.pop(session_id, None)
        return "\n".join([
            "status: removed",
            f"session_id: {session_id}",
            f"terminated: {str(was_running).lower()}",
        ])

    return f"error: unknown Process action: {action}"


def register_bash_tool(reg: ToolRegistry, *, default_timeout_s: float = 120.0,
                       output_limit: int = 30000, max_timeout_s: float = 600.0,
                       spool_max_bytes: int = _DEFAULT_SPOOL_MAX_BYTES,
                       auto_background_after_s: float = 15.0) -> None:
    reg.add(
        "Bash",
        "Run a shell command and wait for its terminal result. Commands are killed on timeout and large output is spooled to disk. background/run_in_background are accepted only as compatibility flags and do not detach execution; Bash never emits a later completion notification.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {"type": "number", "description": "超时秒数，默认 120"},
                "cwd": {"type": "string", "description": "工作目录，默认进程当前目录"},
                "description": {"type": "string", "description": "一句话说明这个命令做什么，便于进度展示"},
                "background": {"type": "boolean", "description": "兼容参数；即使为 true 也会等待命令结束"},
                "run_in_background": {"type": "boolean", "description": "background 的兼容别名；不会分离执行"},
            },
            "required": ["command"],
        },
        make_bash_tool(default_timeout_s, output_limit, max_timeout_s, spool_max_bytes, auto_background_after_s),
    )
    reg.add(
        "Process",
        "Inspect or clean up legacy/recent Bash session records with list, poll, log, kill, or remove. New Bash calls are foreground-complete and normally do not require Process. Do not use Process as a timer.",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "poll", "log", "kill", "remove"],
                    "description": "Process action",
                },
                "sessionId": {"type": "string", "description": "Bash background session id; omitted for list"},
                "session_id": {"type": "string", "description": "sessionId 的兼容别名"},
                "offset": {"type": "number", "description": "For log: 0-based line offset"},
                "limit": {"type": "number", "description": "For log: maximum number of lines"},
                "timeout": {"type": "number", "description": "For poll: wait up to this many milliseconds before returning"},
            },
            "required": ["action"],
        },
        _process_tool,
    )
