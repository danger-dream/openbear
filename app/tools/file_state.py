"""会话级文件读取状态与安全写入辅助。

这一层把 Claude Code 里成熟的 Read/Write/Edit 经验裁剪到 OpenBear：
- Read 只处理真正的普通文件，提前拒绝会阻塞/无限输出的设备和特殊文件。
- Read 记录同一会话/Agent scope 下的读取状态，用于重复读取去重等只读逻辑。
- Write/Edit 内部读取与校验目标文件；覆盖/修改既有文件不再依赖事先 Read。
- Write/Edit 写前强制备份、生成 unified diff，并尽量保留原文件编码/换行风格。
"""
from __future__ import annotations

import codecs
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.tools.base import current_tool_context
from app.tools.truncate import truncate_tool_result

FAST_PATH_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_READ_LIMIT_LINES = 2000
DEFAULT_READ_LIMIT_BYTES = 100_000
DEFAULT_READ_MAX_LINE_BYTES = 64_000
DEFAULT_DIFF_MAX_CHARS = 12_000

FILE_UNCHANGED_STUB = (
    "[文件未变化] 这个文件和本会话前一次相同范围的 Read 结果一致；"
    "请直接使用前一次 Read 的内容，避免重复占用上下文。若确实需要重新返回内容，设置 force=true。"
)

_XML_ATTR_ESCAPE = (
    ("&", "&amp;"),
    ('"', "&quot;"),
    ("<", "&lt;"),
    (">", "&gt;"),
)

BLOCKED_DEVICE_PATHS = {
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/full",
    "/dev/stdin",
    "/dev/tty",
    "/dev/console",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/fd/0",
    "/dev/fd/1",
    "/dev/fd/2",
}

BINARY_EXTENSIONS = {
    ".7z",
    ".a",
    ".avi",
    ".bin",
    ".bmp",
    ".class",
    ".dll",
    ".dmg",
    ".doc",
    ".docx",
    ".DS_Store".lower(),
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".o",
    ".obj",
    ".pdf",
    ".png",
    ".pyc",
    ".rar",
    ".so",
    ".tar",
    ".tgz",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xz",
    ".zip",
}

_CONTROL_BYTES = set(range(0, 32)) - {9, 10, 12, 13}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class ToolScope:
    chat_id: int = 0
    session_uuid: str = ""
    source: str = "chat"
    agent_session_uuid: str = ""
    task_uuid: str = ""
    agent_key: str = ""

    @classmethod
    def current(cls) -> ToolScope:
        ctx = current_tool_context()
        return cls(
            chat_id=int(ctx.chat_id or 0),
            session_uuid=ctx.session_uuid or "",
            source=ctx.source or "chat",
            agent_session_uuid=ctx.agent_session_uuid or "",
            task_uuid=ctx.task_uuid or "",
            agent_key=ctx.agent_key or "",
        )

    def key(self) -> tuple[Any, ...]:
        return (
            self.chat_id,
            self.session_uuid,
            self.source,
            self.agent_session_uuid,
            self.task_uuid,
            self.agent_key,
        )


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    path: str
    resolved_path: str
    dev: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str | None = None
    is_symlink: bool = False

    def same_identity(self, other: FileFingerprint) -> bool:
        return self.resolved_path == other.resolved_path and self.dev == other.dev and self.inode == other.inode

    def same_content(self, other: FileFingerprint) -> bool:
        if self.sha256 and other.sha256:
            return self.sha256 == other.sha256
        return self.size == other.size and self.mtime_ns == other.mtime_ns


@dataclass(slots=True)
class ReadRecord:
    scope: ToolScope
    path: str
    resolved_path: str
    fingerprint: FileFingerprint
    encoding: str
    newline: str
    offset: int
    limit: int | None
    state_id: str
    content_sha256: str | None
    updated_at: float


class FileStateStore:
    def __init__(self, max_entries: int = 512) -> None:
        self.max_entries = max(8, int(max_entries))
        self._records: dict[tuple[tuple[Any, ...], str], ReadRecord] = {}

    def _key(self, scope: ToolScope, resolved_path: str) -> tuple[tuple[Any, ...], str]:
        return (scope.key(), resolved_path)

    def get(self, scope: ToolScope, resolved_path: str) -> ReadRecord | None:
        record = self._records.get(self._key(scope, resolved_path))
        if record is not None:
            record.updated_at = time.time()
        return record

    def put(self, record: ReadRecord) -> None:
        self._records[self._key(record.scope, record.resolved_path)] = record
        self._evict_if_needed()

    def clear_scope(self, *, chat_id: int | None = None, session_uuid: str | None = None) -> int:
        removed = 0
        for key, record in list(self._records.items()):
            if chat_id is not None and record.scope.chat_id != chat_id:
                continue
            if session_uuid is not None and record.scope.session_uuid != session_uuid:
                continue
            self._records.pop(key, None)
            removed += 1
        return removed

    def clear_all(self) -> None:
        self._records.clear()

    def _evict_if_needed(self) -> None:
        extra = len(self._records) - self.max_entries
        if extra <= 0:
            return
        ordered = sorted(self._records.items(), key=lambda item: item[1].updated_at)
        for key, _record in ordered[:extra]:
            self._records.pop(key, None)


DEFAULT_FILE_STATE = FileStateStore()


@dataclass(frozen=True, slots=True)
class ReadLimits:
    limit_lines: int = DEFAULT_READ_LIMIT_LINES
    output_bytes: int = DEFAULT_READ_LIMIT_BYTES
    max_line_bytes: int = DEFAULT_READ_MAX_LINE_BYTES


@dataclass(slots=True)
class ReadResult:
    text: str
    fingerprint: FileFingerprint
    encoding: str
    newline: str
    offset: int
    limit: int | None
    content_sha256: str | None
    deduped: bool = False


@dataclass(slots=True)
class WriteResult:
    text: str
    backup_path: str | None
    diff_path: str | None
    changed: bool


class FileToolError(Exception):
    """可直接返回给模型的文件工具错误。"""


class EditBatchError(Exception):
    """Structured batch-edit failure without echoing replacement payloads."""

    def __init__(
        self,
        code: str,
        phase: str,
        message: str,
        *,
        failed_edit_index: int | None = None,
        committed: str = "false",
        durability_warning: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.message = message
        self.failed_edit_index = failed_edit_index
        self.committed = committed
        self.durability_warning = durability_warning


class AtomicWriteError(OSError):
    """Atomic write failed, possibly after os.replace made the new bytes visible."""

    def __init__(self, message: str, *, replaced: bool) -> None:
        super().__init__(message)
        self.replaced = replaced


def clear_read_file_state(chat_id: int | None = None, session_uuid: str | None = None) -> int:
    """压缩/新会话后清理 Read 状态，避免 dedup 指向已被压缩掉的工具结果。"""
    return DEFAULT_FILE_STATE.clear_scope(chat_id=chat_id, session_uuid=session_uuid)


def normalize_path(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p



def _is_blocked_device_path(path: Path) -> bool:
    s = str(path)
    if s in BLOCKED_DEVICE_PATHS:
        return True
    return s.startswith("/proc/") and (s.endswith("/fd/0") or s.endswith("/fd/1") or s.endswith("/fd/2"))


def _stat_regular_file(path: Path) -> tuple[os.stat_result, bool, str]:
    if _is_blocked_device_path(path):
        raise FileToolError(f"不能读取/写入会阻塞或无限输出的特殊设备文件: {path}")
    try:
        lst = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as e:
        raise FileToolError(f"无法访问路径: {e}") from e

    is_link = stat.S_ISLNK(lst.st_mode)
    try:
        st = path.stat()
    except FileNotFoundError:
        raise
    except OSError as e:
        raise FileToolError(f"无法访问符号链接目标: {e}") from e

    mode = st.st_mode
    if stat.S_ISDIR(mode):
        raise FileToolError(f"是目录而非文件: {path}")
    if not stat.S_ISREG(mode):
        kind = "特殊文件"
        if stat.S_ISFIFO(mode):
            kind = "FIFO/命名管道"
        elif stat.S_ISCHR(mode):
            kind = "字符设备"
        elif stat.S_ISBLK(mode):
            kind = "块设备"
        elif stat.S_ISSOCK(mode):
            kind = "Socket"
        raise FileToolError(f"拒绝读取/写入{kind}，避免工具阻塞或产生无限输出: {path}")
    return st, is_link, str(path.resolve(strict=True))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path: Path, *, include_hash: bool = False) -> FileFingerprint:
    st, is_link, resolved = _stat_regular_file(path)
    digest = sha256_file(Path(resolved)) if include_hash else None
    return FileFingerprint(
        path=str(path),
        resolved_path=resolved,
        dev=int(st.st_dev),
        inode=int(st.st_ino),
        mode=int(st.st_mode),
        size=int(st.st_size),
        mtime_ns=int(st.st_mtime_ns),
        ctime_ns=int(st.st_ctime_ns),
        sha256=digest,
        is_symlink=is_link,
    )


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:4096]
    controls = sum(1 for b in sample if b in _CONTROL_BYTES)
    return controls / max(1, len(sample)) > 0.30


def _utf8_prefix_is_valid(data: bytes) -> tuple[bool, str]:
    """Return whether *data* is a valid UTF-8 prefix.

    Read only samples the first bytes of a file for fast binary/text detection.
    A sample may legitimately end in the middle of a multibyte UTF-8 character;
    treating that prefix as a complete string makes valid files look invalid
    with "unexpected end of data".  Use the incremental decoder with
    ``final=False`` so incomplete trailing sequences are accepted, while truly
    invalid bytes inside the prefix are still rejected.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        decoder.decode(data, final=False)
    except UnicodeDecodeError as e:
        return False, str(e)
    return True, ""


def _detect_encoding_from_sample(data: bytes, suffix: str) -> tuple[str, bool, str]:
    suffix = suffix.lower()
    if suffix in BINARY_EXTENSIONS:
        return "", True, f"扩展名 {suffix} 通常是二进制文件"
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", False, ""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return "utf-16", False, ""
    if _looks_binary(data):
        return "", True, "文件内容包含二进制控制字节"
    ok, error = _utf8_prefix_is_valid(data)
    if not ok:
        return "", True, f"文件不是有效 UTF-8 文本（{error}）"
    return "utf-8", False, ""


def _newline_style(text: str) -> str:
    crlf = text.count("\r\n")
    tmp = text.replace("\r\n", "")
    cr = tmp.count("\r")
    lf = tmp.count("\n")
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    return "\n"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _apply_newline_style(text: str, newline: str) -> str:
    normalized = _normalize_newlines(text)
    if newline == "\r\n":
        return normalized.replace("\n", "\r\n")
    if newline == "\r":
        return normalized.replace("\n", "\r")
    return normalized


def _line_bytes(line: str) -> int:
    return len(line.encode("utf-8", "replace"))


def _render_lines(lines: list[tuple[int, str]]) -> str:
    return "\n".join(f"{line_no + 1:6d}\t{text}" for line_no, text in lines)


def _xml_attr(value: str | int | bool | None) -> str:
    text = "true" if value is True else "false" if value is False else "" if value is None else str(value)
    for src, dst in _XML_ATTR_ESCAPE:
        text = text.replace(src, dst)
    return text


def _format_read_header(
    *,
    path: str,
    size_bytes: int,
    offset: int,
    limit: int | None,
    returned_lines: int,
    truncated: bool,
    total_lines: int | None = None,
    total_lines_known: bool = True,
    resolved: str | None = None,
    deduped: bool = False,
) -> str:
    """Single-line open tag metadata; body follows without a closing wrapper."""
    attrs = [
        f'path="{_xml_attr(path)}"',
        f'size_bytes="{_xml_attr(size_bytes)}"',
    ]
    if total_lines is not None:
        attrs.append(f'total_lines="{_xml_attr(total_lines)}"')
        if not total_lines_known:
            attrs.append('total_lines_known="false"')
    attrs.extend(
        [
            f'offset="{_xml_attr(offset)}"',
            f'limit="{_xml_attr("none" if limit is None else limit)}"',
            f'returned_lines="{_xml_attr(returned_lines)}"',
            f'truncated="{_xml_attr(truncated)}"',
        ]
    )
    if resolved and resolved != path:
        attrs.append(f'resolved="{_xml_attr(resolved)}"')
    if deduped:
        attrs.append('deduped="true"')
    return "<file " + " ".join(attrs) + ">"


def _select_lines(
    text: str,
    *,
    offset: int,
    limit: int | None,
    output_bytes: int,
    max_line_bytes: int,
) -> tuple[list[tuple[int, str]], int, bool, int | None, str]:
    normalized = _normalize_newlines(text)
    all_lines = normalized.split("\n")
    # split('\n') 对空文件返回 ['']，这里修正为 0 行。
    if len(all_lines) == 1 and all_lines[0] == "" and text == "":
        all_lines = []
    selected: list[tuple[int, str]] = []
    byte_count = 0
    truncated = False
    next_offset: int | None = None
    reason = ""
    end = offset + limit if limit is not None else None
    for idx, line in enumerate(all_lines):
        if idx < offset:
            continue
        if end is not None and idx >= end:
            truncated = True
            next_offset = idx
            reason = f"达到 limit={limit}"
            break
        lb = _line_bytes(line)
        if lb > max_line_bytes:
            truncated = True
            next_offset = idx
            reason = f"第 {idx + 1} 行超过单行上限 {max_line_bytes} 字节"
            break
        rendered_len = len(f"{idx + 1:6d}\t".encode()) + lb + 1
        if selected and byte_count + rendered_len > output_bytes:
            truncated = True
            next_offset = idx
            reason = f"达到输出上限 {output_bytes} 字节"
            break
        selected.append((idx, line))
        byte_count += rendered_len
    return selected, len(all_lines), truncated, next_offset, reason


def _stream_select_lines(
    path: Path,
    *,
    encoding: str,
    offset: int,
    limit: int | None,
    output_bytes: int,
    max_line_bytes: int,
) -> tuple[list[tuple[int, str]], int, bool, int | None, str, bool]:
    selected: list[tuple[int, str]] = []
    byte_count = 0
    total_lines = 0
    truncated = False
    next_offset: int | None = None
    reason = ""
    eof = False
    end = offset + limit if limit is not None else None
    partial = b""

    with path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                eof = True
                break
            data = partial + chunk
            parts = data.split(b"\n")
            partial = parts.pop()
            if len(partial) > max_line_bytes:
                idx = total_lines
                if idx >= offset:
                    truncated = True
                    next_offset = idx
                    reason = f"第 {idx + 1} 行超过单行上限 {max_line_bytes} 字节"
                    return selected, total_lines, truncated, next_offset, reason, eof
                # 即使目标 offset 在后面，也不能无限缓存超长行；直接停止，避免内存膨胀。
                truncated = True
                next_offset = idx
                reason = f"第 {idx + 1} 行超过单行上限 {max_line_bytes} 字节"
                return selected, total_lines, truncated, next_offset, reason, eof
            for raw_line in parts:
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]
                idx = total_lines
                total_lines += 1
                if idx < offset:
                    continue
                if end is not None and idx >= end:
                    truncated = True
                    next_offset = idx
                    reason = f"达到 limit={limit}"
                    return selected, total_lines, truncated, next_offset, reason, eof
                if len(raw_line) > max_line_bytes:
                    truncated = True
                    next_offset = idx
                    reason = f"第 {idx + 1} 行超过单行上限 {max_line_bytes} 字节"
                    return selected, total_lines, truncated, next_offset, reason, eof
                line = raw_line.decode(encoding, "strict")
                rendered_len = len(f"{idx + 1:6d}\t".encode()) + _line_bytes(line) + 1
                if selected and byte_count + rendered_len > output_bytes:
                    truncated = True
                    next_offset = idx
                    reason = f"达到输出上限 {output_bytes} 字节"
                    return selected, total_lines, truncated, next_offset, reason, eof
                selected.append((idx, line))
                byte_count += rendered_len
        if partial:
            idx = total_lines
            total_lines += 1
            if idx >= offset and (end is None or idx < end):
                if len(partial) > max_line_bytes:
                    truncated = True
                    next_offset = idx
                    reason = f"第 {idx + 1} 行超过单行上限 {max_line_bytes} 字节"
                else:
                    line = partial.rstrip(b"\r").decode(encoding, "strict")
                    rendered_len = len(f"{idx + 1:6d}\t".encode()) + _line_bytes(line) + 1
                    if selected and byte_count + rendered_len > output_bytes:
                        truncated = True
                        next_offset = idx
                        reason = f"达到输出上限 {output_bytes} 字节"
                    else:
                        selected.append((idx, line))
                        byte_count += rendered_len
        elif total_lines == 0:
            total_lines = 0
    return selected, total_lines, truncated, next_offset, reason, eof


def read_file_for_tool(
    path_str: str,
    *,
    offset: int = 0,
    limit: int | None = DEFAULT_READ_LIMIT_LINES,
    force: bool = False,
    limits: ReadLimits | None = None,
    store: FileStateStore = DEFAULT_FILE_STATE,
) -> ReadResult:
    if not path_str:
        raise FileToolError("缺少 path")
    limits = limits or ReadLimits()
    offset = max(0, int(offset or 0))
    if limit is None:
        effective_limit = None
    else:
        effective_limit = max(1, min(int(limit or limits.limit_lines), limits.limit_lines))
    path = normalize_path(path_str)
    try:
        st, is_link, resolved = _stat_regular_file(path)
    except FileNotFoundError as e:
        raise FileToolError(f"文件不存在: {path_str}") from e

    scope = ToolScope.current()
    record = store.get(scope, resolved)
    display_path = str(path)
    if not force and record and record.offset == offset and record.limit == effective_limit:
        current = fingerprint(Path(resolved), include_hash=bool(record.fingerprint.sha256))
        if record.fingerprint.same_identity(current) and record.fingerprint.same_content(current):
            header = _format_read_header(
                path=display_path,
                size_bytes=int(current.size),
                offset=offset,
                limit=effective_limit,
                returned_lines=0,
                truncated=False,
                resolved=resolved if is_link or resolved != display_path else None,
                deduped=True,
            )
            return ReadResult(
                text=f"{header}\n{FILE_UNCHANGED_STUB}",
                fingerprint=current,
                encoding=record.encoding,
                newline=record.newline,
                offset=offset,
                limit=effective_limit,
                content_sha256=record.content_sha256,
                deduped=True,
            )

    suffix = path.suffix.lower()
    if suffix in BINARY_EXTENSIONS:
        size = st.st_size
        raise FileToolError(f"二进制/非文本文件，已跳过文本读取: {path_str}（{size} 字节，扩展名 {suffix}）")

    sample_path = Path(resolved)
    with sample_path.open("rb") as f:
        sample = f.read(4096)
    encoding, is_binary, binary_reason = _detect_encoding_from_sample(sample, suffix)
    if is_binary:
        raise FileToolError(f"二进制/非 UTF-8 文本文件，已跳过文本读取: {path_str}（{st.st_size} 字节；{binary_reason}）")

    if encoding == "utf-16" and st.st_size >= FAST_PATH_MAX_BYTES:
        raise FileToolError("大型 UTF-16 文件暂不支持流式分段读取，请先转换为 UTF-8 或用 Bash 针对性提取。")

    if st.st_size < FAST_PATH_MAX_BYTES:
        raw = sample_path.read_bytes()
        encoding, is_binary, binary_reason = _detect_encoding_from_sample(raw[:4096], suffix)
        if is_binary:
            raise FileToolError(f"二进制/非 UTF-8 文本文件，已跳过文本读取: {path_str}（{st.st_size} 字节；{binary_reason}）")
        try:
            text = raw.decode(encoding, "strict")
        except UnicodeDecodeError as e:
            raise FileToolError(f"读取失败：文件不是有效 {encoding} 文本（{e}）") from e
        newline = _newline_style(text)
        selected, total_lines, truncated, next_offset, reason = _select_lines(
            text,
            offset=offset,
            limit=effective_limit,
            output_bytes=limits.output_bytes,
            max_line_bytes=limits.max_line_bytes,
        )
        eof = True
        total_lines_known = True
        content_hash = sha256_bytes(raw)
    else:
        newline = "\n"
        try:
            selected, total_lines, truncated, next_offset, reason, eof = _stream_select_lines(
                sample_path,
                encoding=encoding,
                offset=offset,
                limit=effective_limit,
                output_bytes=limits.output_bytes,
                max_line_bytes=limits.max_line_bytes,
            )
        except UnicodeDecodeError as e:
            raise FileToolError(f"读取失败：文件不是有效 {encoding} 文本（{e}）") from e
        # Stream path only knows the full line count after reaching EOF.
        total_lines_known = bool(eof)
        content_hash = None

    body = _render_lines(selected)
    returned_lines = len(selected)
    if not body and total_lines == 0:
        body = "[空文件]"
    elif not body:
        body = f"[文件存在，但 offset={offset} 超出可读取范围；已读取到 {total_lines} 行]"

    include_hash = content_hash is not None
    final_fp = fingerprint(Path(resolved), include_hash=include_hash)
    if content_hash and final_fp.sha256 is None:
        final_fp = FileFingerprint(
            path=final_fp.path,
            resolved_path=final_fp.resolved_path,
            dev=final_fp.dev,
            inode=final_fp.inode,
            mode=final_fp.mode,
            size=final_fp.size,
            mtime_ns=final_fp.mtime_ns,
            ctime_ns=final_fp.ctime_ns,
            sha256=content_hash,
            is_symlink=final_fp.is_symlink,
        )

    if truncated:
        no = next_offset if next_offset is not None else (selected[-1][0] + 1 if selected else offset)
        # Keep the lower-bound note when the full line count is unknown.
        shown_total = total_lines if total_lines_known else max(total_lines, no)
        body += f"\n…[文件至少 {shown_total} 行，本次截断：{reason}；继续读取用 offset={no}]"
    elif total_lines > returned_lines and offset > 0:
        body += f"\n[已读取到文件末尾；文件共 {total_lines} 行]"

    header = _format_read_header(
        path=display_path,
        size_bytes=int(st.st_size),
        offset=offset,
        limit=effective_limit,
        returned_lines=returned_lines,
        truncated=truncated,
        total_lines=total_lines,
        total_lines_known=total_lines_known,
        resolved=resolved if is_link or resolved != display_path else None,
    )
    rendered = f"{header}\n{body}"

    record = ReadRecord(
        scope=scope,
        path=str(path),
        resolved_path=resolved,
        fingerprint=final_fp,
        encoding=encoding,
        newline=newline,
        offset=offset,
        limit=effective_limit,
        state_id=uuid.uuid4().hex,
        content_sha256=final_fp.sha256,
        updated_at=time.time(),
    )
    store.put(record)
    return ReadResult(
        text=rendered,
        fingerprint=final_fp,
        encoding=encoding,
        newline=newline,
        offset=offset,
        limit=effective_limit,
        content_sha256=final_fp.sha256,
    )


def _safe_name(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._")
    return cleaned[:80] or "file"


def _artifact_base(kind: str, scope: ToolScope | None = None) -> Path:
    scope = scope or ToolScope.current()
    session = scope.session_uuid or f"chat-{scope.chat_id or 0}"
    base = Path.cwd() / "data" / "tool_artifacts" / kind / _safe_name(session)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


def _backup_file(path: Path) -> str:
    """Back up *path* without a size cap before destructive writes.

    If the filesystem cannot copy the file (for example because of disk space
    or permission errors), the caller gets a FileToolError and must not continue
    writing.
    """
    base = _artifact_base("file-backups")
    stamp_ms = f"{time.time_ns() // 1_000_000:013d}"
    original_name = path.name or "file"

    def candidate(suffix: str = "") -> Path:
        return base / f"{original_name}.{stamp_ms}{suffix}.bak"

    dest: Path | None = None
    fd: int | None = None
    for idx in range(1000):
        suffix = "" if idx == 0 else f".{idx}"
        current = candidate(suffix)
        try:
            fd = os.open(str(current), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        except OSError as e:
            raise FileToolError(f"备份失败，已拒绝写入: {type(e).__name__}: {e}") from e
        dest = current
        break
    if dest is None or fd is None:
        dest = candidate(f".{uuid.uuid4().hex[:8]}")
        try:
            fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as e:
            raise FileToolError(f"备份失败，已拒绝写入: {type(e).__name__}: {e}") from e

    try:
        with os.fdopen(fd, "wb") as out:
            fd = None
            with path.open("rb") as src:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        with contextlib_suppress_all():
            shutil.copystat(path, dest, follow_symlinks=True)
            dest.chmod(0o600)
    except Exception as e:
        if fd is not None:
            with contextlib_suppress_all():
                os.close(fd)
        with contextlib_suppress_all():
            os.unlink(dest)
        raise FileToolError(f"备份失败，已拒绝写入: {type(e).__name__}: {e}") from e
    return str(dest)


def _ensure_backup_matches(backup_path: str, before_fp: FileFingerprint) -> None:
    if not before_fp.sha256:
        return
    backup = Path(backup_path)
    try:
        backup_size = backup.stat().st_size
        backup_sha = sha256_file(backup)
    except OSError as e:
        raise FileToolError(f"备份校验失败，已拒绝写入: {type(e).__name__}: {e}") from e
    if backup_size != before_fp.size or backup_sha != before_fp.sha256:
        raise FileToolError("备份校验失败：备份内容与写入前文件不一致，已拒绝写入。")


def _write_diff(path: Path, diff_text: str) -> str:
    base = _artifact_base("file-diffs")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = base / f"{stamp}-{uuid.uuid4().hex[:8]}-{_safe_name(path.name)}.diff"
    dest.write_text(diff_text, encoding="utf-8")
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    return str(dest)


def _unified_diff(old: str, new: str, path: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{path} (before)",
            tofile=f"{path} (after)",
            lineterm="",
        )
    )


def _count_diff_lines(diff_text: str) -> tuple[int, int]:
    added = 0
    removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _decode_existing_for_write(path: Path) -> tuple[str, str, str, FileFingerprint]:
    raw = path.read_bytes()
    raw_sha = sha256_bytes(raw)
    fp = fingerprint(path, include_hash=True)
    if fp.size != len(raw) or fp.sha256 != raw_sha:
        raise FileToolError("文件在读取校验过程中发生变化，已拒绝写入；请稍后重试。")
    encoding, is_binary, reason = _detect_encoding_from_sample(raw[:4096], path.suffix.lower())
    if is_binary:
        raise FileToolError(f"拒绝修改二进制/非文本文件: {path}（{reason}）")
    try:
        text = raw.decode(encoding, "strict")
    except UnicodeDecodeError as e:
        raise FileToolError(f"拒绝修改无法按 {encoding} 解码的文件: {path}（{e}）") from e
    return _normalize_newlines(text), encoding, _newline_style(text), fp



def _atomic_write_text(path: Path, text: str, *, encoding: str, newline: str, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out_text = _apply_newline_style(text, newline)
    data = out_text.encode(encoding)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    replaced = False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp_name, stat.S_IMODE(mode))
        os.replace(tmp_name, path)
        replaced = True
        try:
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception as exc:
        with contextlib_suppress_all():
            os.unlink(tmp_name)
        raise AtomicWriteError(f"{type(exc).__name__}: {exc}", replaced=replaced) from exc


class contextlib_suppress_all:
    def __enter__(self):
        return None

    def __exit__(self, *_exc: object) -> bool:
        return True


def write_file_for_tool(
    path_str: str,
    content: str,
    *,
    store: FileStateStore = DEFAULT_FILE_STATE,
    diff_max_chars: int = DEFAULT_DIFF_MAX_CHARS,
) -> WriteResult:
    del store  # Write no longer depends on ReadRecord state.
    if not path_str:
        raise FileToolError("缺少 path")
    path = normalize_path(path_str)
    exists = path.exists()
    if exists:
        old_text, encoding, newline, before_fp = _decode_existing_for_write(path)
        write_path = Path(before_fp.resolved_path)
        new_text = _normalize_newlines(str(content))
        if old_text == new_text:
            meta = {
                "type": "update",
                "path": path_str,
                "resolvedPath": str(write_path),
                "encoding": encoding,
                "newline": "CRLF" if newline == "\r\n" else "CR" if newline == "\r" else "LF",
                "oldSize": before_fp.size,
                "newSize": before_fp.size,
                "oldSha256": before_fp.sha256,
                "newSha256": before_fp.sha256,
                "reason": "content is unchanged; no overwrite performed",
            }
            return WriteResult(text=_format_write_result({"status": "unchanged", **meta}, ""), backup_path=None, diff_path=None, changed=False)

        backup_path = _backup_file(write_path)
        _ensure_backup_matches(backup_path, before_fp)
        after_backup_fp = fingerprint(write_path, include_hash=True)
        if not before_fp.same_identity(after_backup_fp):
            raise FileToolError("文件在备份前后身份发生变化，已拒绝写入。")
        if not before_fp.same_content(after_backup_fp):
            raise FileToolError("文件在备份前后内容发生变化，已拒绝写入。")

        diff_text = _unified_diff(old_text, new_text, path_str)
        diff_path = _write_diff(write_path, diff_text) if len(diff_text) > diff_max_chars else None
        preview = truncate_tool_result(diff_text, diff_max_chars) if diff_text else ""
        before_write_fp = fingerprint(write_path, include_hash=True)
        if not before_fp.same_identity(before_write_fp):
            raise FileToolError("文件在写入前身份发生变化，已拒绝写入。")
        if not before_fp.same_content(before_write_fp):
            raise FileToolError("文件在写入前内容发生变化，已拒绝写入。")
        _atomic_write_text(write_path, new_text, encoding=encoding, newline=newline, mode=before_fp.mode)
        new_fp = fingerprint(write_path, include_hash=True)
        added, removed = _count_diff_lines(diff_text)
        meta = {
            "type": "update",
            "message": "写入成功，已覆盖既有文件并保留写前备份",
            "path": path_str,
            "resolvedPath": str(write_path),
            "backupPath": backup_path,
            "diffPath": diff_path,
            "encoding": encoding,
            "newline": "CRLF" if newline == "\r\n" else "CR" if newline == "\r" else "LF",
            "oldSize": before_fp.size,
            "newSize": new_fp.size,
            "oldSha256": before_fp.sha256,
            "newSha256": new_fp.sha256,
            "addedLines": added,
            "removedLines": removed,
        }
        text = _format_write_result(meta, preview)
        return WriteResult(text=text, backup_path=backup_path, diff_path=diff_path, changed=True)

    new_text = str(content)
    _atomic_write_text(path, new_text, encoding="utf-8", newline="\n", mode=0o644)
    new_fp = fingerprint(path, include_hash=True)
    meta = {
        "type": "create",
        "message": "新建成功",
        "path": path_str,
        "resolvedPath": str(path.resolve(strict=True)),
        "encoding": "utf-8",
        "newline": "LF",
        "chars": len(new_text),
        "newSize": new_fp.size,
        "newSha256": new_fp.sha256,
    }
    return WriteResult(text=_format_write_result(meta, ""), backup_path=None, diff_path=None, changed=True)


def edit_file_for_tool(
    path_str: str,
    old: str,
    new: str,
    *,
    replace_all: bool = False,
    store: FileStateStore = DEFAULT_FILE_STATE,
    diff_max_chars: int = DEFAULT_DIFF_MAX_CHARS,
) -> WriteResult:
    del store  # Edit performs its own file matching/backup checks.
    if not path_str:
        raise FileToolError("缺少 path")
    if old == "":
        raise FileToolError("old_string 不能为空（新建文件请用 Write）")
    path = normalize_path(path_str)
    if not path.exists():
        raise FileToolError(f"文件不存在: {path_str}")

    text, encoding, newline, before_fp = _decode_existing_for_write(path)
    write_path = Path(before_fp.resolved_path)
    old_norm = _normalize_newlines(old)
    new_norm = _normalize_newlines(new)
    count = text.count(old_norm)
    if count == 0:
        raise FileToolError(f"未找到 old_string，未修改: {path_str}")
    if count > 1 and not replace_all:
        raise FileToolError(
            f"该原文出现多次（{count} 次），操作不安全已被拦截；"
            "如确实要替换全部匹配，请使用 replace_all=true 重新调用；"
            "如只想替换一处，请扩大 old_string 上下文使其唯一。"
        )
    replacements = count if replace_all else 1
    new_text = text.replace(old_norm, new_norm) if replace_all else text.replace(old_norm, new_norm, 1)
    if new_text == text:
        meta = {
            "type": "edit",
            "path": path_str,
            "resolvedPath": str(write_path),
            "replacements": replacements,
            "oldSize": before_fp.size,
            "newSize": before_fp.size,
            "oldSha256": before_fp.sha256,
            "newSha256": before_fp.sha256,
            "reason": "replacement produced no content change; no write performed",
        }
        return WriteResult(text=_format_write_result({"status": "unchanged", **meta}, ""), backup_path=None, diff_path=None, changed=False)

    backup_path = _backup_file(write_path)
    _ensure_backup_matches(backup_path, before_fp)
    after_backup_fp = fingerprint(write_path, include_hash=True)
    if not before_fp.same_identity(after_backup_fp):
        raise FileToolError("文件在备份前后身份发生变化，已拒绝写入。")
    if not before_fp.same_content(after_backup_fp):
        raise FileToolError("文件在备份前后内容发生变化，已拒绝写入。")

    diff_text = _unified_diff(text, new_text, path_str)
    diff_path = _write_diff(write_path, diff_text) if len(diff_text) > diff_max_chars else None
    preview = truncate_tool_result(diff_text, diff_max_chars) if diff_text else ""
    before_write_fp = fingerprint(write_path, include_hash=True)
    if not before_fp.same_identity(before_write_fp):
        raise FileToolError("文件在写入前身份发生变化，已拒绝写入。")
    if not before_fp.same_content(before_write_fp):
        raise FileToolError("文件在写入前内容发生变化，已拒绝写入。")
    _atomic_write_text(write_path, new_text, encoding=encoding, newline=newline, mode=before_fp.mode)
    new_fp = fingerprint(write_path, include_hash=True)
    added, removed = _count_diff_lines(diff_text)
    meta = {
        "type": "edit",
        "message": "修改成功，已保存写前备份",
        "path": path_str,
        "resolvedPath": str(write_path),
        "backupPath": backup_path,
        "diffPath": diff_path,
        "encoding": encoding,
        "newline": "CRLF" if newline == "\r\n" else "CR" if newline == "\r" else "LF",
        "replacements": replacements,
        "oldSize": before_fp.size,
        "newSize": new_fp.size,
        "oldSha256": before_fp.sha256,
        "newSha256": new_fp.sha256,
        "addedLines": added,
        "removedLines": removed,
    }
    return WriteResult(text=_format_write_result(meta, preview), backup_path=backup_path, diff_path=diff_path, changed=True)


def _decode_existing_for_edit_batch(path: Path) -> tuple[str, str, str, FileFingerprint]:
    """Read/decode once and bind the batch plan to one normalized snapshot."""
    before_stat, is_link, resolved = _stat_regular_file(path)
    resolved_path = Path(resolved)
    raw = resolved_path.read_bytes()
    try:
        after_stat = resolved_path.stat()
    except OSError as exc:
        raise FileToolError(f"文件在读取校验过程中发生变化，已拒绝写入: {type(exc).__name__}: {exc}") from exc
    before_identity = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size, before_stat.st_mtime_ns, before_stat.st_ctime_ns)
    after_identity = (after_stat.st_dev, after_stat.st_ino, after_stat.st_size, after_stat.st_mtime_ns, after_stat.st_ctime_ns)
    if before_identity != after_identity or len(raw) != int(after_stat.st_size):
        raise FileToolError("文件在读取校验过程中发生变化，已拒绝写入；请稍后重试。")
    encoding, is_binary, reason = _detect_encoding_from_sample(raw[:4096], path.suffix.lower())
    if is_binary:
        raise FileToolError(f"拒绝修改二进制/非文本文件: {path}（{reason}）")
    try:
        decoded = raw.decode(encoding, "strict")
    except UnicodeDecodeError as exc:
        raise FileToolError(f"拒绝修改无法按 {encoding} 解码的文件: {path}（{exc}）") from exc
    fp = FileFingerprint(
        path=str(path),
        resolved_path=resolved,
        dev=int(after_stat.st_dev),
        inode=int(after_stat.st_ino),
        mode=int(after_stat.st_mode),
        size=int(after_stat.st_size),
        mtime_ns=int(after_stat.st_mtime_ns),
        ctime_ns=int(after_stat.st_ctime_ns),
        sha256=sha256_bytes(raw),
        is_symlink=is_link,
    )
    return _normalize_newlines(decoded), encoding, _newline_style(decoded), fp


def _match_spans(text: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return spans
        end = found + len(needle)
        spans.append((found, end))
        start = end


def _prior_edits_can_create_needle(text: str, edits: list[dict[str, Any]], index: int, needle: str) -> bool:
    simulated = text
    for prior in edits[:index]:
        old = _normalize_newlines(str(prior["old_string"]))
        new = _normalize_newlines(str(prior["new_string"]))
        replace_all = bool(prior.get("replace_all", False))
        simulated = simulated.replace(old, new) if replace_all else simulated.replace(old, new, 1)
    return needle in simulated


def edit_file_batch_for_tool(
    path_str: str,
    edits: list[dict[str, Any]],
    *,
    store: FileStateStore = DEFAULT_FILE_STATE,
    diff_max_chars: int = DEFAULT_DIFF_MAX_CHARS,
) -> WriteResult:
    del store  # Batch Edit owns one complete source snapshot and transaction.
    path = normalize_path(path_str)
    if not path.exists():
        raise EditBatchError("file_not_found", "preflight", f"文件不存在: {path_str}")
    try:
        text, encoding, newline, before_fp = _decode_existing_for_edit_batch(path)
    except FileToolError as exc:
        raise EditBatchError("file_read_failed", "preflight", str(exc)) from exc
    except Exception as exc:
        raise EditBatchError("file_read_failed", "preflight", f"读取文件失败: {type(exc).__name__}: {exc}") from exc

    write_path = Path(before_fp.resolved_path)
    normalized: list[dict[str, Any]] = []
    seen_old: dict[str, int] = {}
    selected: list[tuple[int, int, int, str]] = []
    per_edit_replacements: list[int] = []

    for index, item in enumerate(edits):
        old = _normalize_newlines(str(item["old_string"]))
        new = _normalize_newlines(str(item["new_string"]))
        replace_all = bool(item.get("replace_all", False))
        normalized.append({"old_string": old, "new_string": new, "replace_all": replace_all})
        if old in seen_old:
            raise EditBatchError(
                "duplicate_old_string",
                "preflight",
                f"第 {index} 段与较早编辑使用了重复 old_string；未修改文件。",
                failed_edit_index=index,
            )
        seen_old[old] = index
        spans = _match_spans(text, old)
        if not spans:
            if _prior_edits_can_create_needle(text, normalized, index, old):
                raise EditBatchError(
                    "order_dependent_edit",
                    "preflight",
                    f"第 {index} 段只会在较早编辑应用后出现；批量编辑不允许顺序依赖。",
                    failed_edit_index=index,
                )
            raise EditBatchError(
                "old_string_not_found",
                "preflight",
                f"第 {index} 段未在原始文件快照中找到 old_string；未修改文件。",
                failed_edit_index=index,
            )
        if len(spans) > 1 and not replace_all:
            raise EditBatchError(
                "ambiguous_old_string",
                "preflight",
                f"第 {index} 段的 old_string 在原始快照中出现 {len(spans)} 次；请扩大上下文或设置 replace_all=true。",
                failed_edit_index=index,
            )
        chosen = spans if replace_all else spans[:1]
        per_edit_replacements.append(len(chosen))
        selected.extend((start, end, index, new) for start, end in chosen)

    by_position = sorted(selected, key=lambda span: (span[0], span[1], span[2]))
    previous: tuple[int, int, int, str] | None = None
    for span in by_position:
        if previous is not None and span[0] < previous[1]:
            raise EditBatchError(
                "overlapping_edits",
                "preflight",
                f"第 {span[2]} 段选择的原始跨度与第 {previous[2]} 段重叠；未修改文件。",
                failed_edit_index=span[2],
            )
        previous = span

    new_text = text
    for start, end, _index, replacement in sorted(selected, key=lambda span: (span[0], span[1]), reverse=True):
        new_text = new_text[:start] + replacement + new_text[end:]
    replacement_count = sum(per_edit_replacements)
    common_meta = {
        "type": "edit_batch",
        "path": path_str,
        "resolvedPath": str(write_path),
        "editCount": len(edits),
        "replacementCount": replacement_count,
        "perEditReplacements": json.dumps(per_edit_replacements, separators=(",", ":")),
    }
    if new_text == text:
        meta = {
            "status": "unchanged",
            **common_meta,
            "committed": "false",
            "oldSize": before_fp.size,
            "newSize": before_fp.size,
            "oldSha256": before_fp.sha256,
            "newSha256": before_fp.sha256,
            "reason": "all replacements produced no content change; no backup or write performed",
        }
        return WriteResult(text=_format_write_result(meta, ""), backup_path=None, diff_path=None, changed=False)

    try:
        backup_path = _backup_file(write_path)
        _ensure_backup_matches(backup_path, before_fp)
    except FileToolError as exc:
        raise EditBatchError("backup_failed", "backup", str(exc)) from exc
    except Exception as exc:
        raise EditBatchError("backup_failed", "backup", f"备份失败: {type(exc).__name__}: {exc}") from exc

    diff_text = _unified_diff(text, new_text, path_str)
    try:
        diff_path = _write_diff(write_path, diff_text) if len(diff_text) > diff_max_chars else None
    except Exception as exc:
        raise EditBatchError("diff_failed", "commit", f"diff 生成失败: {type(exc).__name__}: {exc}") from exc
    preview = truncate_tool_result(diff_text, diff_max_chars) if diff_text else ""

    try:
        before_write_fp = fingerprint(write_path, include_hash=True)
    except Exception as exc:
        raise EditBatchError("concurrent_change", "commit", f"写入前无法复核文件: {type(exc).__name__}: {exc}") from exc
    if not before_fp.same_identity(before_write_fp) or not before_fp.same_content(before_write_fp):
        raise EditBatchError("concurrent_change", "commit", "文件在批量编辑写入前发生变化；本次编辑未提交。")

    expected_bytes = _apply_newline_style(new_text, newline).encode(encoding)
    expected_hash = sha256_bytes(expected_bytes)
    try:
        _atomic_write_text(write_path, new_text, encoding=encoding, newline=newline, mode=before_fp.mode)
    except AtomicWriteError as exc:
        committed = "false"
        final_hash = ""
        if exc.replaced:
            committed = "unknown"
            try:
                final_hash = str(fingerprint(write_path, include_hash=True).sha256 or "")
            except Exception:
                final_hash = ""
            if final_hash == expected_hash:
                committed = "true"
            elif final_hash == str(before_fp.sha256 or ""):
                committed = "false"
        if committed == "true":
            warning = "新内容已通过最终哈希确认，但目录 fsync 失败，崩溃恢复耐久性不能保证。"
            raise EditBatchError(
                "durability_warning",
                "durability",
                f"批量编辑内容已提交，但持久化同步失败: {exc}",
                committed="true",
                durability_warning=warning,
            ) from exc
        raise EditBatchError(
            "atomic_write_failed",
            "commit",
            f"批量编辑原子写入失败: {exc}",
            committed=committed,
        ) from exc

    try:
        new_fp = fingerprint(write_path, include_hash=True)
    except Exception as exc:
        raise EditBatchError(
            "final_hash_unavailable",
            "verify",
            f"原子替换已执行，但最终哈希读取失败: {type(exc).__name__}: {exc}",
            committed="unknown",
        ) from exc
    if new_fp.sha256 != expected_hash:
        committed = "false" if new_fp.sha256 == before_fp.sha256 else "unknown"
        raise EditBatchError(
            "final_hash_mismatch",
            "verify",
            "原子替换后的文件哈希与预期不一致。",
            committed=committed,
        )

    added, removed = _count_diff_lines(diff_text)
    meta = {
        "status": "success",
        **common_meta,
        "message": "批量修改成功，已保存单个写前备份",
        "committed": "true",
        "backupPath": backup_path,
        "diffPath": diff_path,
        "encoding": encoding,
        "newline": "CRLF" if newline == "\r\n" else "CR" if newline == "\r" else "LF",
        "oldSize": before_fp.size,
        "newSize": new_fp.size,
        "oldSha256": before_fp.sha256,
        "newSha256": new_fp.sha256,
        "addedLines": added,
        "removedLines": removed,
    }
    return WriteResult(text=_format_write_result(meta, preview), backup_path=backup_path, diff_path=diff_path, changed=True)


def _format_write_result(meta: dict[str, Any], diff_preview: str) -> str:
    def snake(key: str) -> str:
        out = []
        for idx, ch in enumerate(str(key)):
            if ch.isupper() and idx > 0:
                out.append("_")
            out.append(ch.lower())
        return "".join(out)

    status = str(meta.get("status") or "success")
    lines = [f"status: {status}"]
    for key, value in meta.items():
        if value is None or key == "status":
            continue
        out_key = "operation" if key == "type" else snake(key)
        lines.append(f"{out_key}: {value}")
    if diff_preview:
        lines.append("diff:")
        lines.append(diff_preview)
    return "\n".join(lines)
