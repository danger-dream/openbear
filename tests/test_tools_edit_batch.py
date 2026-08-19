from __future__ import annotations

import json
import os
import stat

import pytest

import app.tools.file_state as file_state
from app.tools.base import ToolRegistry
from app.tools.file_state import FileStateStore
from app.tools.files import register_file_tools


def _reg() -> ToolRegistry:
    registry = ToolRegistry()
    register_file_tools(registry, store=FileStateStore())
    return registry


_MISSING = object()


async def _edit(registry: ToolRegistry, path, **extra) -> str:
    return await registry.dispatch("Edit", json.dumps({"path": str(path), **extra}))


async def _edit_batch(registry: ToolRegistry, path, edits=_MISSING, **extra) -> str:
    payload = {"path": str(path), **extra}
    if edits is not _MISSING:
        payload["edits"] = edits
    return await registry.dispatch("EditBatch", json.dumps(payload))


def _field(output: str, key: str) -> str:
    prefix = f"{key}: "
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(f"missing {key!r} in:\n{output}")


def test_edit_schema_exposes_two_non_overlapping_tool_contracts():
    schemas = {item["name"]: item["parameters"] for item in _reg().schemas()}
    single = schemas["Edit"]
    batch = schemas["EditBatch"]

    assert single["required"] == ["path", "old_string", "new_string"]
    assert set(single["properties"]) == {"path", "old_string", "new_string", "replace_all"}
    assert batch["required"] == ["path", "edits"]
    assert set(batch["properties"]) == {"path", "edits"}
    edits = batch["properties"]["edits"]
    assert edits["type"] == "array"
    assert edits["minItems"] == 1
    assert edits["maxItems"] == 20
    assert edits["items"]["required"] == ["old_string", "new_string"]
    assert set(edits["items"]["properties"]) == {"old_string", "new_string", "replace_all"}
    assert "edits" not in single["properties"]
    assert not ({"old_string", "new_string", "replace_all"} & set(batch["properties"]))
    assert "oneOf" not in json.dumps(single)
    assert "anyOf" not in json.dumps(batch)


async def test_single_edit_still_supports_empty_new_string_deletion(tmp_path):
    path = tmp_path / "single.txt"
    path.write_text("keep remove", encoding="utf-8")

    output = await _edit(_reg(), path, old_string=" remove", new_string="")

    assert "status: success" in output
    assert "operation: edit" in output
    assert path.read_text(encoding="utf-8") == "keep"


async def test_edit_contracts_reject_cross_shape_without_modification(tmp_path):
    path = tmp_path / "cross-shape.txt"
    path.write_text("a", encoding="utf-8")
    registry = _reg()

    single_output = await _edit(
        registry,
        path,
        old_string="a",
        new_string="b",
        edits=[{"old_string": "a", "new_string": "b"}],
    )
    batch_output = await _edit_batch(
        registry,
        path,
        [{"old_string": "a", "new_string": "b"}],
        old_string="a",
    )

    assert single_output.startswith("error: Edit 仅支持")
    assert _field(batch_output, "code") == "unexpected_single_edit_fields"
    assert _field(batch_output, "committed") == "false"
    assert path.read_text(encoding="utf-8") == "a"


async def test_batch_edit_success_is_single_backup_and_single_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path / "batch.txt"
    path.write_text("alpha one\nbeta two\ngamma three\n", encoding="utf-8")
    backup_calls = 0
    replace_calls = 0
    original_backup = file_state._backup_file
    original_replace = file_state.os.replace

    def counted_backup(target):
        nonlocal backup_calls
        backup_calls += 1
        return original_backup(target)

    def counted_replace(source, target):
        nonlocal replace_calls
        replace_calls += 1
        return original_replace(source, target)

    monkeypatch.setattr(file_state, "_backup_file", counted_backup)
    monkeypatch.setattr(file_state.os, "replace", counted_replace)

    output = await _edit_batch(_reg(), path, [
        {"old_string": "alpha one", "new_string": "alpha 1"},
        {"old_string": "gamma three", "new_string": "gamma 3"},
    ])

    assert "status: success" in output
    assert "operation: edit_batch" in output
    assert _field(output, "edit_count") == "2"
    assert _field(output, "replacement_count") == "2"
    assert json.loads(_field(output, "per_edit_replacements")) == [1, 1]
    assert _field(output, "committed") == "true"
    assert output.count("backup_path:") == 1
    assert output.count("diff:") == 1
    assert backup_calls == 1
    assert replace_calls == 1
    assert path.read_text(encoding="utf-8") == "alpha 1\nbeta two\ngamma 3\n"


async def test_batch_replace_all_and_empty_new_preserve_crlf(tmp_path):
    path = tmp_path / "batch-crlf.txt"
    path.write_bytes(b"x remove\r\nx remove\r\n")

    output = await _edit_batch(_reg(), path, [
        {"old_string": "x", "new_string": "y", "replace_all": True},
        {"old_string": " remove", "new_string": "", "replace_all": True},
    ])

    assert "status: success" in output
    assert _field(output, "replacement_count") == "4"
    assert json.loads(_field(output, "per_edit_replacements")) == [2, 2]
    assert "newline: CRLF" in output
    assert path.read_bytes() == b"y\r\ny\r\n"


@pytest.mark.parametrize(
    ("payload", "code", "failed_index"),
    [
        ({"edits": [{"old_string": "a", "new_string": "b"}], "old_string": "a"}, "unexpected_single_edit_fields", "none"),
        ({}, "missing_edits", "none"),
        ({"old_string": "a"}, "unexpected_single_edit_fields", "none"),
        ({"edits": [{"old_string": "a", "new_string": "b", "replace_all": "false"}]}, "invalid_replace_all_type", "0"),
        ({"edits": [{"path": "other.txt", "old_string": "a", "new_string": "b"}]}, "cross_file_not_supported", "0"),
        ({"edits": [{"old_string": "a", "new_string": "b", "count": 1}]}, "unknown_edit_field", "0"),
        ({"edits": []}, "invalid_edit_count", "none"),
        ({"edits": [{"old_string": "a", "new_string": "b"}] * 21}, "invalid_edit_count", "none"),
        ({"edits": [{"old_string": "", "new_string": "b"}]}, "empty_old_string", "0"),
        ({"edits": [{"old_string": "a"}]}, "incomplete_edit_item", "0"),
    ],
)
async def test_batch_validation_is_strict_and_structured(tmp_path, payload, code, failed_index):
    path = tmp_path / "validation.txt"
    path.write_text("a", encoding="utf-8")

    output = await _edit_batch(_reg(), path, **payload)

    assert output.startswith("error:")
    assert _field(output, "code") == code
    assert _field(output, "operation") == "edit_batch"
    assert _field(output, "phase") == "validation"
    assert _field(output, "path") == str(path)
    assert _field(output, "failed_edit_index") == failed_index
    assert _field(output, "committed") == "false"
    assert path.read_text(encoding="utf-8") == "a"


async def test_batch_total_text_limit_is_utf8_bytes(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("a", encoding="utf-8")
    oversized = "界" * ((1024 * 1024 // 3) + 1)

    output = await _edit_batch(_reg(), path, [{"old_string": "a", "new_string": oversized}])

    assert _field(output, "code") == "edit_payload_too_large"
    assert _field(output, "committed") == "false"
    assert path.read_text(encoding="utf-8") == "a"


@pytest.mark.parametrize(
    ("content", "edits", "code", "failed_index"),
    [
        ("alpha", [{"old_string": "missing", "new_string": "x"}], "old_string_not_found", "0"),
        ("x x", [{"old_string": "x", "new_string": "y"}], "ambiguous_old_string", "0"),
        ("alpha", [{"old_string": "alpha", "new_string": "a"}, {"old_string": "alpha", "new_string": "b"}], "duplicate_old_string", "1"),
        ("abcdef", [{"old_string": "abc", "new_string": "x"}, {"old_string": "bc", "new_string": "y"}], "overlapping_edits", "1"),
        ("a", [{"old_string": "a", "new_string": "b"}, {"old_string": "b", "new_string": "c"}], "order_dependent_edit", "1"),
    ],
)
async def test_batch_preflight_failures_do_not_backup_or_write(tmp_path, monkeypatch, content, edits, code, failed_index):
    path = tmp_path / "preflight.txt"
    path.write_text(content, encoding="utf-8")

    def forbidden_backup(_path):
        raise AssertionError("preflight failure must not create a backup")

    monkeypatch.setattr(file_state, "_backup_file", forbidden_backup)

    output = await _edit_batch(_reg(), path, edits)

    assert output.startswith("error:")
    assert _field(output, "code") == code
    assert _field(output, "phase") == "preflight"
    assert _field(output, "failed_edit_index") == failed_index
    assert _field(output, "committed") == "false"
    assert path.read_text(encoding="utf-8") == content


async def test_batch_unchanged_does_not_backup_or_replace(tmp_path, monkeypatch):
    path = tmp_path / "unchanged.txt"
    path.write_text("alpha", encoding="utf-8")

    monkeypatch.setattr(file_state, "_backup_file", lambda _path: pytest.fail("unexpected backup"))
    monkeypatch.setattr(file_state.os, "replace", lambda *_args: pytest.fail("unexpected replace"))

    output = await _edit_batch(_reg(), path, [{"old_string": "alpha", "new_string": "alpha"}])

    assert "status: unchanged" in output
    assert "operation: edit_batch" in output
    assert _field(output, "committed") == "false"
    assert "backup_path:" not in output
    assert path.read_text(encoding="utf-8") == "alpha"


async def test_batch_detects_concurrent_change_before_write(tmp_path, monkeypatch):
    path = tmp_path / "concurrent.txt"
    path.write_text("alpha", encoding="utf-8")
    original_backup = file_state._backup_file

    def backup_then_external_change(target):
        backup = original_backup(target)
        target.write_text("external", encoding="utf-8")
        return backup

    monkeypatch.setattr(file_state, "_backup_file", backup_then_external_change)

    output = await _edit_batch(_reg(), path, [{"old_string": "alpha", "new_string": "beta"}])

    assert _field(output, "code") == "concurrent_change"
    assert _field(output, "phase") == "commit"
    assert _field(output, "committed") == "false"
    assert path.read_text(encoding="utf-8") == "external"


async def test_batch_atomic_replace_failure_reports_not_committed(tmp_path, monkeypatch):
    path = tmp_path / "replace-fail.txt"
    path.write_text("alpha", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(file_state.os, "replace", fail_replace)

    output = await _edit_batch(_reg(), path, [{"old_string": "alpha", "new_string": "beta"}])

    assert output.startswith("error:")
    assert _field(output, "code") == "atomic_write_failed"
    assert _field(output, "phase") == "commit"
    assert _field(output, "committed") == "false"
    assert path.read_text(encoding="utf-8") == "alpha"


async def test_batch_directory_fsync_failure_reports_committed_with_durability_warning(tmp_path, monkeypatch):
    path = tmp_path / "dir-fsync.txt"
    path.write_text("alpha", encoding="utf-8")
    original_fsync = file_state.os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        return original_fsync(fd)

    monkeypatch.setattr(file_state.os, "fsync", fail_directory_fsync)

    output = await _edit_batch(_reg(), path, [{"old_string": "alpha", "new_string": "beta"}])

    assert output.startswith("error:")
    assert _field(output, "code") == "durability_warning"
    assert _field(output, "phase") == "durability"
    assert _field(output, "committed") == "true"
    assert "durability_warning:" in output
    assert path.read_text(encoding="utf-8") == "beta"


async def test_batch_reports_unknown_when_final_hash_cannot_be_read(tmp_path, monkeypatch):
    path = tmp_path / "unknown-commit.txt"
    path.write_text("alpha", encoding="utf-8")
    original_fingerprint = file_state.fingerprint
    calls = 0

    def fail_final_fingerprint(target, *, include_hash=False):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("final hash unavailable")
        return original_fingerprint(target, include_hash=include_hash)

    monkeypatch.setattr(file_state, "fingerprint", fail_final_fingerprint)

    output = await _edit_batch(_reg(), path, [{"old_string": "alpha", "new_string": "beta"}])

    assert _field(output, "code") == "final_hash_unavailable"
    assert _field(output, "phase") == "verify"
    assert _field(output, "committed") == "unknown"
    assert path.read_text(encoding="utf-8") == "beta"


async def test_batch_preserves_utf16_encoding(tmp_path):
    path = tmp_path / "utf16.txt"
    path.write_text("alpha\r\nbeta\r\n", encoding="utf-16")

    output = await _edit_batch(_reg(), path, [{"old_string": "beta", "new_string": "gamma"}])

    assert "status: success" in output
    assert "encoding: utf-16" in output
    with path.open("r", encoding="utf-16", newline="") as stream:
        assert stream.read() == "alpha\r\ngamma\r\n"


async def test_batch_rejects_binary_without_modification_or_backup(tmp_path, monkeypatch):
    path = tmp_path / "binary.dat"
    original = b"alpha\x00beta"
    path.write_bytes(original)
    monkeypatch.setattr(file_state, "_backup_file", lambda _path: pytest.fail("unexpected backup"))

    output = await _edit_batch(_reg(), path, [{"old_string": "alpha", "new_string": "gamma"}])

    assert output.startswith("error:")
    assert _field(output, "code") == "file_read_failed"
    assert _field(output, "phase") == "preflight"
    assert _field(output, "committed") == "false"
    assert path.read_bytes() == original
