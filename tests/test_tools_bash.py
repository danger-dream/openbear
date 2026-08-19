"""Bash + 搜索工具测试。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.tools import processes
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.bash import register_bash_tool


def test_process_registry_restart_blocking(monkeypatch):
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: None)
    processes.register(101, command="bash", blocks_restart=True)
    processes.register(102, command="mcp:playwright", blocks_restart=False)
    try:
        assert processes.count() == 1
        assert processes.count(blocking_only=False) == 2
        assert len(processes.active()) == 2
    finally:
        processes.unregister(101)
        processes.unregister(102)


async def test_bash_echo():
    reg = ToolRegistry()
    register_bash_tool(reg)
    out = await reg.dispatch("Bash", json.dumps({"command": "echo hello123"}))
    assert "hello123" in out
    assert "output_path:" in out


async def test_bash_exit_code():
    reg = ToolRegistry()
    register_bash_tool(reg)
    out = await reg.dispatch("Bash", json.dumps({"command": "exit 3"}))
    assert "exit_code: 3" in out


async def test_bash_grep_no_match_is_status_not_error(tmp_path):
    reg = ToolRegistry()
    register_bash_tool(reg)
    f = tmp_path / "a.txt"
    f.write_text("abc", encoding="utf-8")
    out = await reg.dispatch("Bash", json.dumps({"command": f"grep zzz {f}"}))
    assert "no matches" in out
    assert "[错误]" not in out and "<tool-meta>" not in out


async def test_bash_blocks_openbear_self_restart():
    reg = ToolRegistry()
    register_bash_tool(reg)
    for command in (
        "systemctl restart openbear.service",
        "systemctl restart openbear",
        "systemctl try-restart openbear",
        "sudo /bin/systemctl stop openbear.service",
        "bash -lc 'systemctl restart openbear'",
        "sh -c \"service openbear stop\"",
        "kill $(pgrep -f openbear)",
        "kill `pidof openbear`",
        "ps aux | grep openbear | awk '{print $2}' | xargs kill",
    ):
        out = await reg.dispatch("Bash", json.dumps({"command": command}))
        assert "OpenBear service-control" in out
        assert "OpenBearControl" in out


async def test_bash_self_control_guard_does_not_block_unrelated_service_commands():
    reg = ToolRegistry()
    register_bash_tool(reg)
    out = await reg.dispatch("Bash", json.dumps({"command": "printf safe"}))
    assert "safe" in out
    assert "OpenBear service-control" not in out
    for command in (
        "systemctl status nginx",
        "systemctl status openbear",
        "pgrep -f openbear || true",
    ):
        out = await reg.dispatch("Bash", json.dumps({"command": command}))
        assert "OpenBear service-control" not in out


async def test_bash_timeout():
    reg = ToolRegistry()
    register_bash_tool(reg, default_timeout_s=1.0)
    out = await reg.dispatch("Bash", json.dumps({"command": "sleep 5"}))
    assert "timeout" in out
    assert processes.count() == 0


async def test_bash_blocks_long_sleep_when_default_long():
    reg = ToolRegistry()
    register_bash_tool(reg, default_timeout_s=120.0)
    out = await reg.dispatch("Bash", json.dumps({"command": "sleep 5"}))
    assert "blocked long sleep" in out


async def test_bash_discovers_nvm_node_path(tmp_path, monkeypatch):
    fake_bin = tmp_path / ".nvm" / "versions" / "node" / "v99.0.0" / "bin"
    fake_bin.mkdir(parents=True)
    fake_tool = fake_bin / "fake-nvm-tool"
    fake_tool.write_text("#!/bin/sh\necho nvm-ok\n", encoding="utf-8")
    fake_tool.chmod(0o755)
    (fake_bin / "node").write_text("#!/bin/sh\necho node-ok\n", encoding="utf-8")
    (fake_bin / "node").chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))

    reg = ToolRegistry()
    register_bash_tool(reg)
    out = await reg.dispatch("Bash", json.dumps({"command": "fake-nvm-tool"}))

    assert "nvm-ok" in out


async def test_bash_ignores_unreadable_root_nvm(monkeypatch):
    real_is_dir = Path.is_dir

    def fake_is_dir(self):
        if str(self) == "/root/.nvm/versions/node":
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    reg = ToolRegistry()
    register_bash_tool(reg)
    out = await reg.dispatch("Bash", json.dumps({"command": "echo hello123"}))
    assert "hello123" in out


async def test_bash_cwd(tmp_path):
    reg = ToolRegistry()
    register_bash_tool(reg)
    (tmp_path / "marker.txt").write_text("x")
    out = await reg.dispatch("Bash", json.dumps({"command": "ls", "cwd": str(tmp_path)}))
    assert "marker.txt" in out


async def test_bash_output_truncate_and_spool():
    reg = ToolRegistry()
    register_bash_tool(reg, output_limit=200)
    out = await reg.dispatch("Bash", json.dumps({"command": "for i in $(seq 1 1000); do echo line$i; done"}))
    assert "截断" in out
    assert "output_path:" in out


async def test_bash_waits_past_legacy_auto_background_window():
    reg = ToolRegistry()
    register_bash_tool(reg, default_timeout_s=5.0, auto_background_after_s=0.05)
    out = await reg.dispatch(
        "Bash",
        json.dumps({"command": "python3 -c 'import time; print(\"start\", flush=True); time.sleep(0.15); print(\"done\")'"}),
    )
    assert "status: ok" in out
    assert "start" in out and "done" in out
    assert "status: running" not in out
    assert "auto_backgrounded" not in out
    assert processes.count() == 0


async def test_bash_background_flag_is_compatibility_only_and_sends_no_notification():
    reg = ToolRegistry()
    register_bash_tool(reg, default_timeout_s=5.0)
    notifications: list[dict] = []

    async def notify(payload: dict) -> None:
        notifications.append(payload)

    out = await reg.dispatch(
        "Bash",
        json.dumps({"command": "python3 -c 'import time; time.sleep(0.05); print(\"bg-done\")'", "background": True}),
        context=ToolRuntimeContext(task_notification=notify),
    )
    assert "status: ok" in out
    assert "bg-done" in out
    assert "job_id:" not in out
    assert notifications == []
    assert processes.count() == 0


async def test_bash_tool_descriptions_explain_foreground_completion():
    reg = ToolRegistry()
    register_bash_tool(reg)
    summaries = reg.summaries()
    assert "wait for its terminal result" in summaries["Bash"]
    assert "never emits a later completion notification" in summaries["Bash"]
    assert "legacy/recent Bash session records" in summaries["Process"]
    assert "BashStatus" not in summaries
    assert "BashKill" not in summaries


async def test_process_has_no_session_for_new_foreground_complete_bash():
    reg = ToolRegistry()
    register_bash_tool(reg, default_timeout_s=5.0)
    out = await reg.dispatch(
        "Bash",
        json.dumps({"command": "printf proc-line", "background": True}),
    )
    assert "proc-line" in out
    listed = await reg.dispatch("Process", json.dumps({"action": "list"}))
    assert "No running or recent Bash sessions" in listed


async def test_bash_status_and_kill_tools_are_removed():
    reg = ToolRegistry()
    register_bash_tool(reg)
    assert "BashStatus" not in reg.names()
    assert "BashKill" not in reg.names()
    status = await reg.dispatch("BashStatus", json.dumps({"jobId": "missing"}))
    killed = await reg.dispatch("BashKill", json.dumps({"jobId": "missing"}))
    assert "未知工具" in status or "unknown tool" in status
    assert "未知工具" in killed or "unknown tool" in killed


async def test_removed_search_tools_are_not_registered_by_default():
    reg = ToolRegistry()
    register_bash_tool(reg)

    assert "Glob" not in reg.names()
    assert "Grep" not in reg.names()
    out = await reg.dispatch("Grep", json.dumps({"pattern": "TARGET", "path": "."}))
    assert "未知工具" in out or "unknown tool" in out
