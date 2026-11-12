"""文件工具 —— Read / Write / Edit。

文件工具参考 Claude Code 的成熟语义做了安全化：
- Read 支持文本和本地图片；拒绝设备/FIFO/socket 等特殊文件及其他二进制文件。
- Read 记录当前会话/Agent scope 下的文本读取状态，重复读取同一范围会去重；图片每次返回。
- Write/Edit 覆盖或修改既有文件前不再要求事先 Read，由工具内部读取校验。
- Write/Edit 写前强制备份、尽量保留原编码/换行风格，并返回 diff 摘要和文件摘要。
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.runtime.tool_result import ToolOutcome
from app.tools.base import ToolRegistry
from app.tools.file_images import image_tool_outcome, load_image_for_tool, write_image_snapshot
from app.tools.file_state import (
    DEFAULT_DIFF_MAX_CHARS,
    DEFAULT_FILE_STATE,
    DEFAULT_READ_LIMIT_BYTES,
    DEFAULT_READ_LIMIT_LINES,
    DEFAULT_READ_MAX_LINE_BYTES,
    EditBatchError,
    FileStateStore,
    FileToolError,
    ReadLimits,
    _artifact_base,
    edit_file_batch_for_tool,
    edit_file_for_tool,
    read_file_for_tool,
    write_file_for_tool,
)


def _int_arg(args: dict[str, Any], name: str, default: int) -> int:
    try:
        return int(args.get(name, default) or default)
    except (TypeError, ValueError):
        return default


_MAX_BATCH_EDITS = 20
_MAX_READS = 20
_MAX_BATCH_TEXT_BYTES = 1024 * 1024
_BATCH_ITEM_FIELDS = {"old_string", "new_string", "replace_all"}


def _edit_batch_error_text(path: str, error: EditBatchError) -> str:
    failed_index = "none" if error.failed_edit_index is None else str(error.failed_edit_index)
    lines = [
        "error:",
        f"code: {error.code}",
        "operation: edit_batch",
        f"phase: {error.phase}",
        f"path: {path}",
        f"failed_edit_index: {failed_index}",
        f"committed: {error.committed}",
        f"message: {error.message}",
    ]
    if error.durability_warning:
        lines.append(f"durability_warning: {error.durability_warning}")
    return "\n".join(lines)


def _edit_batch_validation_error(
    path: str,
    code: str,
    message: str,
    *,
    failed_edit_index: int | None = None,
) -> str:
    return _edit_batch_error_text(
        path,
        EditBatchError(
            code,
            "validation",
            message,
            failed_edit_index=failed_edit_index,
            committed="false",
        ),
    )


def make_read_tool(
    *,
    store: FileStateStore = DEFAULT_FILE_STATE,
    default_limit_lines: int = DEFAULT_READ_LIMIT_LINES,
    output_limit_bytes: int = DEFAULT_READ_LIMIT_BYTES,
    max_line_bytes: int = DEFAULT_READ_MAX_LINE_BYTES,
):
    budget = max(1024, int(output_limit_bytes))

    async def _read_one(args: dict[str, Any], output_bytes: int, *, allow_image: bool) -> str | ToolOutcome:
        path = str(args.get("path") or "")
        if not path:
            return "error: missing path"
        try:
            offset = max(0, int(args.get("offset", 0) or 0))
        except (TypeError, ValueError):
            return "error: offset must be a non-negative integer"
        raw_limit = args.get("limit", default_limit_lines)
        try:
            limit = None if raw_limit is None else max(1, int(raw_limit or default_limit_lines))
        except (TypeError, ValueError):
            return "error: limit must be a positive integer"
        force = bool(args.get("force", False))
        limits = ReadLimits(
            limit_lines=max(1, int(default_limit_lines)),
            output_bytes=output_bytes,
            max_line_bytes=max(1024, int(max_line_bytes)),
        )
        try:
            image = await asyncio.to_thread(load_image_for_tool, path)
            if image is not None:
                if not allow_image:
                    return f"error: {path} is an image; read images one per call with path"
                directory = _artifact_base("images")
                snapshot = await asyncio.to_thread(write_image_snapshot, directory, image)
                return image_tool_outcome(image, snapshot)
            result = read_file_for_tool(
                path,
                offset=offset,
                limit=limit,
                force=force,
                limits=limits,
                store=store,
            )
        except FileToolError as e:
            return f"error: {e}"
        except Exception as e:
            return f"error: read failed: {type(e).__name__}: {e}"
        return result.text

    async def _read(args: dict[str, Any]) -> str | ToolOutcome:
        reads = args.get("reads")
        if reads is None:
            return await _read_one(args, budget, allow_image=True)
        if args.get("path"):
            return "error: give either path or reads, not both"
        if not isinstance(reads, list) or not reads or not all(isinstance(entry, dict) for entry in reads):
            return "error: reads must be a non-empty list of {path, offset, limit}"
        if len(reads) > _MAX_READS:
            return f"error: at most {_MAX_READS} reads per call"
        # One output budget shared by all entries, in order; later entries get what is left.
        remaining = budget
        parts = [f"<reads count=\"{len(reads)}\" budget_bytes=\"{budget}\">"]
        for index, entry in enumerate(reads, 1):
            if remaining < 1024:
                parts.append(f"[reads[{index}] {entry.get('path') or ''}] skipped: shared output budget used up; "
                             f"read it in another call")
                continue
            text = await _read_one({"force": args.get("force", False), **entry}, remaining, allow_image=False)
            if text.startswith("error: "):
                text = f"[reads[{index}] {entry.get('path') or ''}] {text}"
            parts.append(text)
            remaining -= len(text.encode("utf-8"))
        parts.append("</reads>")
        return "\n\n".join(parts)

    return _read


def make_write_tool(
    *,
    store: FileStateStore = DEFAULT_FILE_STATE,
    diff_max_chars: int = DEFAULT_DIFF_MAX_CHARS,
):
    async def _write(args: dict[str, Any]) -> str:
        path = str(args.get("path") or "")
        if not path:
            return "error: missing path"
        content = str(args.get("content", ""))
        try:
            result = write_file_for_tool(
                path,
                content,
                store=store,
                diff_max_chars=diff_max_chars,
            )
        except FileToolError as e:
            return f"error: {e}"
        except Exception as e:
            return f"error: write failed: {type(e).__name__}: {e}"
        return result.text

    return _write


def make_edit_tool(
    *,
    store: FileStateStore = DEFAULT_FILE_STATE,
    diff_max_chars: int = DEFAULT_DIFF_MAX_CHARS,
):
    """Build the single-replacement Edit handler.

    ``Edit`` deliberately owns one non-overlapping parameter shape.  Batch
    replacement is a separate model-visible ``EditBatch`` tool so providers
    that mishandle optional JSON-Schema fields cannot manufacture a mixed call.
    """
    async def _edit(args: dict[str, Any]) -> str:
        raw_path = args.get("path")
        path = raw_path if isinstance(raw_path, str) else ""
        if "edits" in args:
            return (
                "error: Edit 仅支持 path、old_string、new_string 和可选 replace_all；"
                "1-20 处独立替换请调用 EditBatch。"
            )
        if "old_string" not in args or "new_string" not in args:
            return "error: missing old_string or new_string"
        if not path:
            return "error: missing path"
        old = args["old_string"]
        new = args["new_string"]
        if not isinstance(old, str) or not isinstance(new, str):
            return "error: old_string and new_string must be strings"
        if "replace_all" in args and not isinstance(args["replace_all"], bool):
            return "error: replace_all must be a boolean"
        replace_all = args.get("replace_all", False)
        try:
            result = edit_file_for_tool(
                path,
                old,
                new,
                replace_all=replace_all,
                store=store,
                diff_max_chars=diff_max_chars,
            )
        except FileToolError as error:
            return f"error: {error}"
        except Exception as error:
            return f"error: edit failed: {type(error).__name__}: {error}"
        return result.text

    return _edit


def make_edit_batch_tool(
    *,
    store: FileStateStore = DEFAULT_FILE_STATE,
    diff_max_chars: int = DEFAULT_DIFF_MAX_CHARS,
):
    """Build the transactional 1-20 replacement EditBatch handler."""
    async def _edit_batch(args: dict[str, Any]) -> str:
        raw_path = args.get("path")
        path = raw_path if isinstance(raw_path, str) else ""
        single_fields = {"old_string", "new_string", "replace_all"}
        unexpected = sorted(single_fields & set(args))
        if unexpected:
            return _edit_batch_validation_error(
                path,
                "unexpected_single_edit_fields",
                "EditBatch 仅接受 path 和 edits；不接受顶层 "
                f"{', '.join(unexpected)}。单次替换请调用 Edit。",
            )
        if not path:
            return _edit_batch_validation_error(path, "invalid_path", "path 必须是非空字符串。")
        if "edits" not in args:
            return _edit_batch_validation_error(path, "missing_edits", "EditBatch 必须提供 edits。")
        raw_edits = args.get("edits")
        if not isinstance(raw_edits, list):
            return _edit_batch_validation_error(path, "invalid_edits_type", "edits 必须是数组。")
        if not 1 <= len(raw_edits) <= _MAX_BATCH_EDITS:
            return _edit_batch_validation_error(
                path,
                "invalid_edit_count",
                f"edits 数量必须在 1 到 {_MAX_BATCH_EDITS} 之间。",
            )

        validated: list[dict[str, Any]] = []
        total_bytes = 0
        for index, item in enumerate(raw_edits):
            if not isinstance(item, dict):
                return _edit_batch_validation_error(
                    path,
                    "invalid_edit_item_type",
                    f"第 {index} 段必须是对象。",
                    failed_edit_index=index,
                )
            if "path" in item:
                return _edit_batch_validation_error(
                    path,
                    "cross_file_not_supported",
                    "批量 Edit 第一版只允许顶层 path 指定的同一个文件；item.path 不受支持。",
                    failed_edit_index=index,
                )
            unknown = sorted(set(item) - _BATCH_ITEM_FIELDS)
            if unknown:
                return _edit_batch_validation_error(
                    path,
                    "unknown_edit_field",
                    f"第 {index} 段包含不支持的字段: {', '.join(unknown)}。",
                    failed_edit_index=index,
                )
            if "old_string" not in item or "new_string" not in item:
                return _edit_batch_validation_error(
                    path,
                    "incomplete_edit_item",
                    f"第 {index} 段必须显式包含 old_string 和 new_string。",
                    failed_edit_index=index,
                )
            old = item["old_string"]
            new = item["new_string"]
            if not isinstance(old, str) or not isinstance(new, str):
                return _edit_batch_validation_error(
                    path,
                    "invalid_edit_string_type",
                    f"第 {index} 段的 old_string/new_string 必须是字符串。",
                    failed_edit_index=index,
                )
            if old == "":
                return _edit_batch_validation_error(
                    path,
                    "empty_old_string",
                    f"第 {index} 段的 old_string 不能为空。",
                    failed_edit_index=index,
                )
            if "replace_all" in item and not isinstance(item["replace_all"], bool):
                return _edit_batch_validation_error(
                    path,
                    "invalid_replace_all_type",
                    f"第 {index} 段的 replace_all 必须是布尔值。",
                    failed_edit_index=index,
                )
            try:
                total_bytes += len(old.encode("utf-8")) + len(new.encode("utf-8"))
            except UnicodeEncodeError:
                return _edit_batch_validation_error(
                    path,
                    "invalid_string_encoding",
                    f"第 {index} 段包含无法编码为 UTF-8 的字符串。",
                    failed_edit_index=index,
                )
            if total_bytes > _MAX_BATCH_TEXT_BYTES:
                return _edit_batch_validation_error(
                    path,
                    "edit_payload_too_large",
                    "所有 old_string/new_string 的 UTF-8 总大小不能超过 1 MiB。",
                    failed_edit_index=index,
                )
            validated.append({
                "old_string": old,
                "new_string": new,
                "replace_all": item.get("replace_all", False),
            })

        try:
            result = edit_file_batch_for_tool(
                path,
                validated,
                store=store,
                diff_max_chars=diff_max_chars,
            )
        except EditBatchError as error:
            return _edit_batch_error_text(path, error)
        except Exception as error:
            return _edit_batch_error_text(
                path,
                EditBatchError(
                    "internal_edit_batch_error",
                    "internal",
                    f"批量编辑发生未预期错误: {type(error).__name__}: {error}",
                    committed="unknown",
                ),
            )
        return result.text

    return _edit_batch


def register_file_tools(
    reg: ToolRegistry,
    *,
    store: FileStateStore = DEFAULT_FILE_STATE,
    default_limit_lines: int = DEFAULT_READ_LIMIT_LINES,
    output_limit_bytes: int = DEFAULT_READ_LIMIT_BYTES,
    max_line_bytes: int = DEFAULT_READ_MAX_LINE_BYTES,
    diff_max_chars: int = DEFAULT_DIFF_MAX_CHARS,
) -> None:
    reg.file_state = store
    reg.add(
        "Read",
        "Read local text files or images. JPEG, PNG, GIF and WebP images (up to 20 MiB) are sent to your vision input, with resizing or compression when required by supported model limits; source files remain unchanged. Images are returned on every call without text deduplication. Other binary/special files are rejected. Text supports offset/limit and force. "
        f"reads[] reads up to {_MAX_READS} text files or ranges in one call, in order, sharing one output budget (the single-read size); an entry that does not fit is skipped and named.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "reads": {"type": "array", "maxItems": _MAX_READS, "items": {"type": "object", "properties": {
                    "path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"},
                    "force": {"type": "boolean"},
                }, "required": ["path"]}, "description": "代替 path：一次读多个文本文件或片段，各项参数同单文件；图片仍需单独读取"},
                "offset": {"type": "integer", "description": "文本起始行（0 基）；图片不适用"},
                "limit": {"type": "integer", "description": "文本最多读取行数；图片不适用"},
                "force": {"type": "boolean", "description": "文本即使未变化也强制重新返回，默认 false；图片每次均返回"},
                "description": {"type": "string", "description": "一句话说明这次读取做什么，便于进度展示"},
            },
        },
        make_read_tool(
            store=store,
            default_limit_lines=default_limit_lines,
            output_limit_bytes=output_limit_bytes,
            max_line_bytes=max_line_bytes,
        ),
    )

    reg.add(
        "Write",
        "Create or overwrite files; creates parents. Existing files are backed up before overwrite; no prior Read required.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "完整内容"},
            },
            "required": ["path", "content"],
        },
        make_write_tool(store=store, diff_max_chars=diff_max_chars),
    )

    reg.add(
        "Edit",
        "Replace one exact text segment in a file. Use EditBatch for 1-20 independent replacements. Existing files are backed up before modification.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "old_string": {"type": "string", "description": "要替换的原文（需唯一）"},
                "new_string": {"type": "string", "description": "替换为；空字符串表示删除"},
                "replace_all": {"type": "boolean", "description": "替换所有匹配，默认 false"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        make_edit_tool(store=store, diff_max_chars=diff_max_chars),
    )

    reg.add(
        "EditBatch",
        "Apply 1-20 independent exact replacements to one file from the same original snapshot. Existing files are backed up before modification.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "所有编辑均作用于此文件"},
                "edits": {
                    "type": "array",
                    "description": "同一原始文件快照上的 1-20 处独立替换",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string", "description": "要替换的非空原文"},
                            "new_string": {"type": "string", "description": "替换为；空字符串表示删除"},
                            "replace_all": {"type": "boolean", "description": "替换此原文的所有匹配，默认 false"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
        make_edit_batch_tool(store=store, diff_max_chars=diff_max_chars),
    )
