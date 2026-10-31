#!/usr/bin/env python3
"""Local foreground adapter for release.py; not an application tool or API.

The core script owns commits, validation, publication, recovery and cleanup.
This optional CLI preserves bounded diagnostics, redacted progress, duplicate
start protection and child-process cancellation without importing the app.
Credentials are supplied only through the maintainer process environment.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
from typing import Callable

# What release.py publishes: X.Y.Z, X.Y.Z-rc.N or X.Y.Z-beta.N (never a -dev.N).
_VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:beta|rc)\.[1-9]\d*)?")
_RUN_ID = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:dev|beta|rc)\.[1-9]\d*)?-[0-9a-f]{12}")


def _run_of(run_id: str, version: str) -> bool:
    """run_id belongs to exactly this version (a 1.2.0-beta.1 run is not a 1.2.0 run)."""
    return re.fullmatch(re.escape(version) + r"-[0-9a-f]{12}", run_id) is not None


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _environment(token: str = "") -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"}
           and not k.startswith(("GIT_CONFIG_", "GIT_TRACE"))}
    if token:
        env["GH_TOKEN"] = token
    return env


def _redactor(token: str) -> Callable[[str], str]:
    secrets = (token, base64.b64encode(("x-access-token:" + token).encode()).decode()) if token else ()

    def redact(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, "<redacted>")
        return text
    return redact


class LocalRelease:
    def __init__(self, repo: Path, *, progress: Callable[[str], None] | None = None):
        self.repo = repo.resolve()
        self.root = self.repo / ".release-runs"
        self.python = self.repo / ".venv/bin/python"
        self.script = self.repo / "scripts/release.py"
        self.progress = progress
        self.lock = asyncio.Lock()

    def available(self) -> bool:
        return (self.repo / ".git").exists() and self.script.is_file() and self.python.is_file()

    def _state(self, run_id: str) -> dict:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid release run ID")
        directory = self.root / run_id
        if directory.is_symlink():
            raise ValueError("Release directory must not be a symlink")
        state = json.loads((directory / "state.json").read_text())
        if (state.get("schema") != 1 or state.get("runId") != run_id
                or state.get("repo") != str(self.repo)):
            raise ValueError("Release state target/owner mismatch")
        return state

    async def _invoke(self, args: list[str], *, token: str = "", on_line=None, progress=False) -> tuple[int, str]:
        redact = _redactor(token)
        proc = await asyncio.create_subprocess_exec(
            str(self.python), str(self.script), *args, cwd=self.repo,
            env=_environment(token), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True, limit=1024 * 1024,
        )
        output = ""

        async def stop():
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), 60)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()

        try:
            assert proc.stdout is not None
            while raw := await proc.stdout.readline():
                line = redact(raw.decode("utf-8", "replace").rstrip())
                output = (output + line + "\n")[-65536:]
                if on_line:
                    on_line(line)
                if progress and self.progress and (line.startswith("[") or line.startswith("Release ")):
                    self.progress(line[:2000])
            return await proc.wait(), output
        except BaseException:
            cleanup = asyncio.create_task(stop())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise

    async def _summary(self, run_id: str) -> dict:
        code, text = await self._invoke(["status", run_id, "--summary"])
        if code:
            return {"status": "error", "runId": run_id, "error": "release_summary_unavailable",
                    "exitCode": code, "details": text[-4000:]}
        return json.loads(text)

    async def _cleanup_interrupted(self, run_id: str) -> None:
        await self._invoke(["cleanup", run_id])

    async def run(self, args: dict) -> dict:
        token = ""
        if not self.available():
            return {"status": "error", "error": "maintainer_checkout_unavailable", "exitCode": 1}
        try:
            allowed = {"action", "version", "notes", "files", "runId", "repository",
                       "environment", "timeout", "approveContainerCgroup"}
            if set(args) - allowed:
                raise ValueError("Unsupported arguments; credentials belong only in the environment")
            action = args.get("action")
            if action not in {"start", "status", "resume"}:
                raise ValueError("action must be start, status or resume")
            if action == "start" and args.get("runId"):
                raise ValueError("start does not accept runId; use resume for an existing run")
            if action != "start" and any(key in args for key in ("version", "notes", "repository", "timeout")):
                raise ValueError("Existing runs keep their frozen version, target and notes")
            run_id = str(args.get("runId") or "")
            if action != "start":
                state = self._state(run_id)
                version = state["version"]
                if action == "status" or state.get("completed"):
                    return await self._summary(run_id)
            else:
                version = str(args.get("version") or "")
                if not _VERSION.fullmatch(version):
                    raise ValueError("version must be X.Y.Z, X.Y.Z-rc.N or X.Y.Z-beta.N")
                if not isinstance(args.get("notes"), str) or not args["notes"].strip():
                    raise ValueError("notes must contain user-facing release notes")
                # An uncertain start is inspected, never automatically replayed.
                for existing in (p for p in self.root.glob(version + "-*/state.json") if _run_of(p.parent.name, version)):
                    self._state(existing.parent.name)
                    return {**await self._summary(existing.parent.name), "startNotRepeated": True}
            files = args.get("files", [] if action == "resume" else None)
            if not isinstance(files, list) or any(not isinstance(p, str) or not p or p.startswith(("/", "-"))
                                                  or ".." in Path(p).parts for p in files):
                raise ValueError("files must be an explicit list of repository-relative paths")
            if self.lock.locked():
                return {"status": "busy", "error": "release_already_running", "exitCode": 2}
            async with self.lock:
                # This flag records authorization obtained by the local operator;
                # no approval is inferred from previous invocations.
                if args.get("approveContainerCgroup") is not True:
                    return {"status": "not_started", "error": "container_cgroup_approval_required", "exitCode": 2}
                token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
                if not token:
                    raise ValueError("Provide GH_TOKEN or GITHUB_TOKEN in the environment")
                notes_path = None

                def remember_run(line: str):
                    nonlocal run_id
                    if line.startswith("Release run: "):
                        found = line.removeprefix("Release run: ").strip()
                        if not _RUN_ID.fullmatch(found) or not _run_of(found, version):
                            raise ValueError("Release returned an invalid run identity")
                        self._state(found)
                        run_id = found

                try:
                    if action == "start":
                        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                        fd, filename = tempfile.mkstemp(prefix=".local-notes-", suffix=".md", dir=self.root)
                        notes_path = Path(filename)
                        with os.fdopen(fd, "w", encoding="utf-8") as stream:
                            stream.write(args["notes"])
                        command = ["start", version, "--notes", filename, "--files", *files]
                        if args.get("repository"):
                            command.extend(["--repository", args["repository"]])
                        if args.get("timeout") is not None:
                            command.extend(["--timeout", str(args["timeout"])])
                    else:
                        command = ["resume", run_id]
                        if files:
                            command.extend(["--files", *files])
                    if args.get("environment"):
                        command.extend(["--environment", args["environment"]])
                    command.append("--approve-container-cgroup")
                    code, output = await self._invoke(command, token=token, on_line=remember_run, progress=True)
                    if code < 0 or code == 130:
                        if run_id:
                            await self._cleanup_interrupted(run_id)
                    if run_id:
                        result = await self._summary(run_id)
                        result["exitCode"] = code or result.get("exitCode", 0)
                    else:
                        result = {"status": "failed", "exitCode": code or 1, "error": output[-4000:]}
                    return json.loads(_redactor(token)(_json(result)))
                except asyncio.CancelledError:
                    if run_id:
                        cleanup = asyncio.create_task(self._cleanup_interrupted(run_id))
                        while not cleanup.done():
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError:
                                continue
                        with contextlib.suppress(Exception):
                            cleanup.result()
                    raise
                finally:
                    if notes_path:
                        notes_path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError) as exc:
            return {"status": "error", "error": _redactor(token)(str(exc)), "exitCode": 1}


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    commands = cli.add_subparsers(dest="action", required=True)
    start = commands.add_parser("start", help="Start a release with reviewed files and notes")
    start.add_argument("version")
    start.add_argument("--notes", type=Path, required=True)
    start.add_argument("--files", nargs="*", required=True)
    start.add_argument("--repository")
    start.add_argument("--timeout", type=int)
    resume = commands.add_parser("resume", help="Resume the same failed or interrupted release")
    resume.add_argument("runId")
    resume.add_argument("--files", nargs="*", default=[])
    for command in (start, resume):
        command.add_argument("--environment", type=Path)
        command.add_argument("--approve-container-cgroup", dest="approveContainerCgroup", action="store_true",
                             help="Only after authorization for this run's private container mount")
    status = commands.add_parser("status", help="Read a bounded summary without credentials or execution")
    status.add_argument("runId")
    return cli


def main(argv=None):
    args = vars(parser().parse_args(argv))
    repo = args.pop("repo")
    try:
        if "notes" in args:
            args["notes"] = args["notes"].read_text(encoding="utf-8")
        if args.get("environment"):
            args["environment"] = str(args["environment"].resolve())
    except OSError:
        print(_json({"status": "error", "error": "Cannot read release notes", "exitCode": 1}))
        return 1

    async def execute():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            return await LocalRelease(repo, progress=lambda line: print(line, file=sys.stderr, flush=True)).run(args)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    try:
        result = asyncio.run(execute())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(_json({"status": "interrupted", "exitCode": 130}))
        return 130
    print(_json(result))
    return result.get("exitCode", 0 if result.get("status") in {"completed", "running", "pending"} else 1)


if __name__ == "__main__":
    raise SystemExit(main())
