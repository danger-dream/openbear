"""文件工具测试。"""
from __future__ import annotations

import json
import os
import re

from app.tools.base import ToolRegistry
from app.tools.file_state import FileStateStore
from app.tools.files import register_file_tools


def _reg() -> ToolRegistry:
    r = ToolRegistry()
    register_file_tools(r, store=FileStateStore())
    return r


def _field(output: str, key: str) -> str:
    prefix = f"{key}: "
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(f"missing output field {key!r} in:\n{output}")


def _assert_backup_name(backup_path: str, original_name: str) -> None:
    name = os.path.basename(backup_path)
    assert re.fullmatch(rf"{re.escape(original_name)}\.\d{{13}}(?:\.(?:\d+|[0-9a-f]{{8}}))?\.bak", name), name


def _read_header(output: str) -> str:
    first = output.splitlines()[0] if output else ""
    assert first.startswith("<file "), output
    assert first.endswith(">"), output
    return first


def _header_attr(header: str, name: str) -> str | None:
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', header)
    return match.group(1) if match else None


async def test_write_then_read(tmp_path):
    reg = _reg()
    f = tmp_path / "a.txt"
    out = await reg.dispatch("Write", json.dumps({"path": str(f), "content": "hello\nworld"}))
    assert "status: success" in out and "operation: create" in out
    assert "message: 新建成功" in out
    assert "backup_path:" not in out
    assert "new_size:" in out and re.fullmatch(r"[0-9a-f]{64}", _field(out, "new_sha256"))
    read = await reg.dispatch("Read", json.dumps({"path": str(f)}))
    assert "hello" in read and "world" in read
    assert "1\t" in read  # 行号
    header = _read_header(read)
    assert _header_attr(header, "path") == str(f)
    assert _header_attr(header, "size_bytes") == str(f.stat().st_size)
    assert _header_attr(header, "total_lines") == "2"
    assert _header_attr(header, "offset") == "0"
    assert _header_attr(header, "limit") == "2000"
    assert _header_attr(header, "returned_lines") == "2"
    assert _header_attr(header, "truncated") == "false"
    assert "deduped=" not in header
    assert "</file>" not in read


async def test_read_missing(tmp_path):
    reg = _reg()
    out = await reg.dispatch("Read", json.dumps({"path": str(tmp_path / "nope.txt")}))
    assert "不存在" in out
    assert not out.lstrip().startswith("<file ")



async def test_read_description_is_optional_and_ignored_by_handler(tmp_path):
    """description 仅用于进度展示，不改变读取结果。"""
    reg = _reg()
    f = tmp_path / "desc.txt"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    with_desc = await reg.dispatch(
        "Read",
        json.dumps({"path": str(f), "description": "核对说明字段不影响正文"}),
    )
    without = await reg.dispatch("Read", json.dumps({"path": str(f), "force": True}))
    # strip possible force-only differences: both should contain body lines
    assert "alpha" in with_desc and "beta" in with_desc
    assert _header_attr(_read_header(with_desc), "path") == str(f)
    assert "核对说明字段不影响正文" not in with_desc
    # schema exposes optional description
    schema = next(t for t in reg.schemas() if t["name"] == "Read")["parameters"]
    assert "description" in schema["properties"]
    assert "description" not in schema.get("required", [])
    assert schema["properties"]["description"]["type"] == "string"


async def test_read_binary_rejected(tmp_path):
    reg = _reg()
    f = tmp_path / "bin.dat"
    f.write_bytes(b"abc\x00def")
    out = await reg.dispatch("Read", json.dumps({"path": str(f)}))
    assert "二进制" in out
    assert not out.lstrip().startswith("<file ")


async def test_read_accepts_utf8_sample_cut_in_multibyte_char(tmp_path):
    reg = _reg()
    f = tmp_path / "utf8-boundary.py"
    f.write_text(("a" * 4095) + "中\nok", encoding="utf-8")
    out = await reg.dispatch("Read", json.dumps({"path": str(f), "limit": 2}))
    assert "二进制" not in out
    assert "非 UTF-8" not in out
    assert "中" in out and "ok" in out
    header = _read_header(out)
    assert _header_attr(header, "returned_lines") == "2"
    assert _header_attr(header, "truncated") == "false"


async def test_read_fifo_rejected(tmp_path):
    if not hasattr(os, "mkfifo"):
        return
    reg = _reg()
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    out = await reg.dispatch("Read", json.dumps({"path": str(fifo)}))
    assert "FIFO" in out or "特殊" in out


async def test_read_dedup_and_force(tmp_path):
    reg = _reg()
    f = tmp_path / "dup.txt"
    f.write_text("a\nb", encoding="utf-8")
    first = await reg.dispatch("Read", json.dumps({"path": str(f)}))
    second = await reg.dispatch("Read", json.dumps({"path": str(f)}))
    forced = await reg.dispatch("Read", json.dumps({"path": str(f), "force": True}))
    assert "a" in first
    assert "文件未变化" in second
    assert _header_attr(_read_header(second), "deduped") == "true"
    assert "a" in forced and "文件未变化" not in forced
    assert _header_attr(_read_header(forced), "deduped") is None


async def test_read_metadata_truncation_and_empty(tmp_path):
    reg = _reg()
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line-{i}" for i in range(1, 11)) + "\n", encoding="utf-8")
    out = await reg.dispatch("Read", json.dumps({"path": str(big), "offset": 2, "limit": 3}))
    header = _read_header(out)
    assert _header_attr(header, "offset") == "2"
    assert _header_attr(header, "limit") == "3"
    assert _header_attr(header, "returned_lines") == "3"
    assert _header_attr(header, "truncated") == "true"
    assert _header_attr(header, "total_lines") == "11"
    assert "继续读取用 offset=5" in out
    assert "     3\tline-3" in out

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    empty_out = await reg.dispatch("Read", json.dumps({"path": str(empty)}))
    empty_header = _read_header(empty_out)
    assert _header_attr(empty_header, "total_lines") == "0"
    assert _header_attr(empty_header, "returned_lines") == "0"
    assert _header_attr(empty_header, "truncated") == "false"
    assert "[空文件]" in empty_out

    quoted = tmp_path / 'quote"file.txt'
    quoted.write_text("x\n", encoding="utf-8")
    quoted_out = await reg.dispatch("Read", json.dumps({"path": str(quoted)}))
    assert 'path="' in _read_header(quoted_out)
    assert "&quot;" in _read_header(quoted_out)


async def test_edit_unique_without_prior_read_succeeds(tmp_path):
    reg = _reg()
    f = tmp_path / "b.txt"
    f.write_text("foo bar baz", encoding="utf-8")
    out = await reg.dispatch("Edit", json.dumps({"path": str(f), "old_string": "bar", "new_string": "BAR"}))
    assert "status: success" in out and "operation: edit" in out
    assert "message: 修改成功" in out
    assert "replacements: 1" in out
    assert "backup_path:" in out and "diff:" in out
    assert "old_size:" in out and "new_size:" in out
    assert re.fullmatch(r"[0-9a-f]{64}", _field(out, "old_sha256"))
    assert re.fullmatch(r"[0-9a-f]{64}", _field(out, "new_sha256"))
    _assert_backup_name(_field(out, "backup_path"), "b.txt")
    assert f.read_text() == "foo BAR baz"


async def test_edit_ambiguous_without_prior_read_requires_replace_all(tmp_path):
    reg = _reg()
    f = tmp_path / "c.txt"
    f.write_text("x x x", encoding="utf-8")
    out = await reg.dispatch("Edit", json.dumps({"path": str(f), "old_string": "x", "new_string": "y"}))
    assert "该原文出现多次" in out and "replace_all=true" in out and "扩大 old_string 上下文" in out
    assert f.read_text() == "x x x"

    out2 = await reg.dispatch("Edit", json.dumps({"path": str(f), "old_string": "x", "new_string": "y", "replace_all": True}))
    assert "status: success" in out2 and "operation: edit" in out2
    assert "replacements: 3" in out2 and "backup_path:" in out2
    assert f.read_text() == "y y y"


async def test_edit_not_found_without_prior_read(tmp_path):
    reg = _reg()
    f = tmp_path / "d.txt"
    f.write_text("abc", encoding="utf-8")
    out = await reg.dispatch("Edit", json.dumps({"path": str(f), "old_string": "zzz", "new_string": "y"}))
    assert "未找到" in out
    assert f.read_text() == "abc"


async def test_write_creates_parent(tmp_path):
    reg = _reg()
    f = tmp_path / "sub" / "dir" / "e.txt"
    out = await reg.dispatch("Write", json.dumps({"path": str(f), "content": "x"}))
    assert "status: success" in out and "operation: create" in out
    assert "new_size:" in out and "new_sha256:" in out
    assert f.exists()


async def test_write_existing_without_prior_read_creates_backup_and_hashes(tmp_path):
    reg = _reg()
    f = tmp_path / "stale.txt"
    f.write_text("old", encoding="utf-8")
    out = await reg.dispatch("Write", json.dumps({"path": str(f), "content": "newer"}))
    assert "status: success" in out and "operation: update" in out
    assert "message: 写入成功" in out
    assert "backup_path:" in out and "diff:" in out
    assert _field(out, "old_size") == "3"
    assert _field(out, "new_size") == "5"
    assert re.fullmatch(r"[0-9a-f]{64}", _field(out, "old_sha256"))
    assert re.fullmatch(r"[0-9a-f]{64}", _field(out, "new_sha256"))
    backup_path = _field(out, "backup_path")
    _assert_backup_name(backup_path, "stale.txt")
    with open(backup_path, encoding="utf-8") as backup:
        assert backup.read() == "old"
    assert f.read_text(encoding="utf-8") == "newer"


async def test_write_preserves_crlf_without_prior_read(tmp_path):
    reg = _reg()
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"a\r\nb\r\n")
    out = await reg.dispatch("Write", json.dumps({"path": str(f), "content": "a\nc\n"}))
    assert "status: success" in out and "operation: update" in out
    assert "newline: CRLF" in out
    assert "backup_path:" in out
    assert b"\r\n" in f.read_bytes()

