"""release.py --detach / wait: follow a run by its own process, never by a command-line match."""
from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release  # noqa: E402
from release_support.common import atomic_json, load, process_ticks  # noqa: E402

RUN = "1.0.1-0123456789ab"


def saved_run(root, repo, **value):
    directory = root / RUN
    atomic_json(directory / "state.json", {"schema": 1, "runId": RUN, "repo": str(repo), "stages": {}, **value})
    return directory


def wait(repo, *extra):
    return release.main(["--repo", str(repo), "wait", RUN, *extra])


def test_wait_reports_each_outcome_with_its_own_exit_code(tmp_path, capsys):
    root = tmp_path / ".release-runs"
    me = {"pid": os.getpid(), "startTicks": process_ticks(os.getpid()), "startedAt": time.time()}
    saved_run(root, tmp_path, invocations=[me])
    started = time.monotonic()
    assert wait(tmp_path, "--timeout", "0") == 3
    assert time.monotonic() - started < 5
    assert json.loads(capsys.readouterr().out)["state"] == "running"
    # A reused pid with another start time is not this run.
    saved_run(root, tmp_path, invocations=[{**me, "startTicks": "0"}], lastError={"message": "x", "exitCode": 1})
    assert wait(tmp_path) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "failed" and report["interrupted"]
    saved_run(root, tmp_path, invocations=[{**me, "finishedAt": time.time()}], lastError={"message": "GitHub refused", "exitCode": 2})
    assert wait(tmp_path) == 2
    assert json.loads(capsys.readouterr().out)["state"] == "paused"
    saved_run(root, tmp_path, invocations=[{**me, "finishedAt": time.time()}], completed=True,
              stages={"publication": {"status": "passed", "result": {"url": "https://example.invalid/r"}}})
    assert wait(tmp_path) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "completed" and report["url"] == "https://example.invalid/r" and not report["interrupted"]


def test_detach_returns_once_the_run_is_saved_and_wait_ends_when_its_process_does(tmp_path, monkeypatch, capsys):
    root = tmp_path / ".release-runs"
    fake = tmp_path / "fake_release.py"
    # Stands in for the real entrypoint: prints the run id, records its own invocation, works, exits.
    fake.write_text(textwrap.dedent(f"""
        import json, os, sys, time
        sys.path.insert(0, {str(ROOT / "scripts")!r})
        from release_support.common import atomic_json, process_ticks
        directory = {str(root)!r} + "/" + {RUN!r}
        print("Release run:", {RUN!r}, flush=True)
        value = {{"schema": 1, "runId": {RUN!r}, "repo": {str(tmp_path)!r}, "stages": {{}},
                  "invocations": [{{"pid": os.getpid(), "startTicks": process_ticks(os.getpid()), "startedAt": time.time()}}]}}
        atomic_json(__import__("pathlib").Path(directory) / "state.json", value)
        time.sleep(2)
        value["invocations"][0]["finishedAt"] = time.time()
        value["completed"] = True
        atomic_json(__import__("pathlib").Path(directory) / "state.json", value)
    """))
    monkeypatch.setattr(release, "__file__", str(fake))
    began = time.monotonic()
    assert release.main(["--repo", str(tmp_path), "start", "1.0.1", "--notes", "n", "--files", "--detach"]) == 0
    assert time.monotonic() - began < 2
    started = json.loads(capsys.readouterr().out)
    assert started["runId"] == RUN and started["running"] and started["wait"].endswith("scripts/release.py wait " + RUN)
    assert "--detach" not in Path(started["log"]).read_text()
    assert load(root / RUN / "detached.json")["pid"] == started["pid"]
    assert wait(tmp_path, "--timeout", "30") == 0
    assert json.loads(capsys.readouterr().out)["state"] == "completed"
    assert time.monotonic() - began < 10


def test_a_detached_command_that_fails_at_once_reports_its_output(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "fake_release.py"
    fake.write_text('import sys\nprint("Another release process owns this repository", file=sys.stderr)\nsys.exit(2)\n')
    monkeypatch.setattr(release, "__file__", str(fake))
    saved_run(tmp_path / ".release-runs", tmp_path)
    assert release.main(["--repo", str(tmp_path), "resume", RUN, "--detach"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert not result["running"] and "owns this repository" in result["output"]


@pytest.mark.parametrize("bad", ["../escape", "not-a-run"])
def test_wait_rejects_foreign_run_ids(tmp_path, bad, capsys):
    assert release.main(["--repo", str(tmp_path), "wait", bad]) != 0
