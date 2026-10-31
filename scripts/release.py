#!/usr/bin/env python3
"""Fixed OpenBear release entrypoint: start once, inspect failures, resume that run.

`node start|status|resume|cleanup` releases openbear-node on its own version
(node/VERSION) and tag (node-v<version>), independent of the controller release.

Either line publishes only X.Y.Z, X.Y.Z-rc.N or X.Y.Z-beta.N (beta and rc as GitHub
pre-releases), newer than everything already published on that line; afterwards the
local version becomes the next minor's X.Y.Z-dev.1 in one local commit (not pushed).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import signal
import sys
import time
import uuid
from pathlib import Path

if __package__:
    from .release_support import versions
    from .release_support.candidate import version
    from .release_support.common import ReleaseError, atomic_json, load
    from .release_support.runner import DEFAULT_IMAGES, Runner
else:
    from release_support import versions
    from release_support.candidate import version
    from release_support.common import ReleaseError, atomic_json, load
    from release_support.runner import DEFAULT_IMAGES, Runner


def environment(path, repo=None):
    local = Path(repo) / ".release-environment.json" if repo else None
    selected = Path(path).resolve() if path else local if local and local.is_file() else None
    result = load(selected) if selected else DEFAULT_IMAGES
    if set(result) != {"pythonImages", "systemdImage"} or set(result["pythonImages"]) != {"311", "312", "313"}:
        raise ReleaseError("Environment requires pythonImages 311/312/313 and systemdImage")
    images = list(result["pythonImages"].values()) + [result["systemdImage"]]
    if any(not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", image) for image in images):
        raise ReleaseError("Invalid Docker image reference")
    return result


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    commands = cli.add_subparsers(dest="action", required=True)
    start = commands.add_parser("start", help="Freeze the local version and execute the full release once")
    start.add_argument("version")
    start.add_argument("--notes", required=True)
    start.add_argument("--files", nargs="*", required=True, help="Explicit reviewed paths; version files are included automatically")
    start.add_argument("--repository", default="danger-dream/openbear")
    start.add_argument("--environment", help="Image references JSON; defaults to local .release-environment.json or generic baseline names")
    start.add_argument("--timeout", type=int, default=1800, help="Per-stage timeout; not a retry permission")
    start.add_argument("--approve-container-cgroup", action="store_true", help="Only after this run's private-container remount authorization")
    resume = commands.add_parser("resume", help="Reconcile side effects and continue the same run")
    resume.add_argument("run_id")
    resume.add_argument("--files", nargs="*", help="Explicitly include corrective files; revalidate changed stage inputs and the new package")
    resume.add_argument("--environment", help="Reviewed replacement test environment after diagnosing an environment failure")
    resume.add_argument("--approve-container-cgroup", action="store_true")
    status = commands.add_parser("status", help="Read saved state without executing any stage")
    status.add_argument("run_id")
    status.add_argument("--summary", action="store_true", help="Bounded read-only JSON diagnostics (schema 1)")
    cleanup = commands.add_parser("cleanup", help="Clean only owned interrupted resources, preserving restart evidence and assets")
    cleanup.add_argument("run_id")
    node = commands.add_parser("node", help="Release openbear-node separately: tag node-v<version>, five assets, never Latest")
    actions = node.add_subparsers(dest="node_action", required=True)
    start = actions.add_parser("start", help="Write node/VERSION, commit reviewed files and run the node release once")
    start.add_argument("version")
    start.add_argument("--notes", required=True)
    start.add_argument("--files", nargs="*", required=True, help="Explicit reviewed paths; node/VERSION is included automatically")
    start.add_argument("--repository", default="danger-dream/openbear")
    start.add_argument("--timeout", type=int, default=1800, help="Per-container timeout; not a retry permission")
    resume = actions.add_parser("resume", help="Reconcile side effects and continue the same node run")
    resume.add_argument("run_id")
    resume.add_argument("--files", nargs="*", help="Explicitly include corrective files")
    status = actions.add_parser("status", help="Read saved node run state without executing any stage")
    status.add_argument("run_id")
    cleanup = actions.add_parser("cleanup", help="Clean only this node run's interrupted resources")
    cleanup.add_argument("run_id")
    return cli


def load_node_release():
    # Imported on demand: the controller release does not depend on node files.
    if __package__:
        from .release_support import node_release
    else:
        from release_support import node_release
    return node_release


def node_state(root, run_id, repo):
    node_release = load_node_release()
    if not node_release.RUN_ID_RE.fullmatch(run_id):
        raise ReleaseError("Invalid node release run ID")
    value = load(root / run_id / "state.json")
    if value.get("schema") != 1 or value.get("kind") != "node" or value.get("runId") != run_id or value.get("repo") != str(repo):
        raise ReleaseError("Node release state schema/owner mismatch")
    return value


def node(args, repo, root):
    """Separate node release; shares the repository lock because it commits and pushes the same main."""
    node_release = load_node_release()
    if args.node_action == "status":
        print(json.dumps(node_state(root, args.run_id, repo), ensure_ascii=False, indent=2))
        return 0
    root.mkdir(mode=0o700, exist_ok=True)
    with (root / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseError("Another release process owns this repository", code=2) from None
        if args.node_action == "start":
            # Newer than every published node-v tag is checked against GitHub in preflight;
            # the local node/VERSION is a development number and no bound.
            versions.target(args.version, "Node version")
            old = node_release.node_version(repo)
            for existing in existing_runs(root, "node-" + args.version):
                raise ReleaseError(f"Node version already has a release run: {existing}; use node status/resume", code=2)
            if args.timeout < 1:
                raise ReleaseError("timeout must be positive")
            notes = Path(args.notes).read_text(encoding="utf-8").rstrip("\r\n") + "\n"
            if not notes.strip():
                raise ReleaseError("Write user-facing release notes before starting")
            run_id = "node-" + args.version + "-" + uuid.uuid4().hex[:12]
            directory = root / run_id
            directory.mkdir(mode=0o700)
            value = {"schema": 1, "kind": "node", "runId": run_id, "repository": args.repository, "repo": str(repo),
                     "oldVersion": old, "version": args.version, "notes": notes, "files": sorted(set(args.files)),
                     "python": str(Path(sys.executable).absolute()), "timeout": args.timeout, "createdAt": time.time(),
                     "startsDevelopment": True, "stages": {}}
            atomic_json(directory / "state.json", value)
            print("Node release run:", run_id, flush=True)
        else:
            value = node_state(root, args.run_id, repo)
            directory = root / args.run_id
            if getattr(args, "files", None):
                value["files"] = sorted(set(value["files"]) | set(args.files))
                atomic_json(directory / "state.json", value)
        runner = node_release.NodeRunner(repo, directory, value)
        if args.node_action == "cleanup":
            runner.command.recover_children()
            value["cleanup"] = runner.cleanup_owned()
            runner.state.save()
            runner.report()
            return 0
        old_handler = signal.signal(signal.SIGTERM, lambda _sig, _frame: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            runner.run()
        finally:
            signal.signal(signal.SIGTERM, old_handler)
        return 0


def existing_runs(root, prefix):
    """Run IDs of exactly this version (1.2.0 does not match a 1.2.0-beta.1 run)."""
    pattern = re.compile(re.escape(prefix) + r"-[0-9a-f]{12}")
    return sorted(path.parent.name for path in root.glob(prefix + "-*/state.json") if pattern.fullmatch(path.parent.name))


def main(argv=None):
    args = parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    root = repo / ".release-runs"
    try:
        if args.action == "node":
            return node(args, repo, root)
        if args.action == "status":
            if not versions.RUN_ID_RE.fullmatch(args.run_id):
                raise ReleaseError("Invalid release run ID")
            value = load(root / args.run_id / "state.json")
            if value.get("schema") != 1 or value.get("runId") != args.run_id or value.get("repo") != str(repo):
                raise ReleaseError("Release state schema/owner mismatch")
            if args.summary:
                if __package__:
                    from .release_support.diagnostics import summary
                else:
                    from release_support.diagnostics import summary
                value = summary(root / args.run_id, value)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        root.mkdir(mode=0o700, exist_ok=True)
        with (root / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReleaseError("Another release process owns this repository", code=2) from None
            if args.action == "start":
                # Newer than every published v tag is checked against GitHub in preflight;
                # the local version is a development number and no bound.
                versions.target(args.version)
                old = version(repo)
                # A stopped same-version run must be resumed, not replaced.
                for existing in existing_runs(root, args.version):
                    raise ReleaseError(f"Version already has a release run: {existing}; use status/resume", code=2)
                if args.timeout < 1:
                    raise ReleaseError("timeout must be positive")
                notes = Path(args.notes).read_text(encoding="utf-8").rstrip("\r\n") + "\n"
                if not notes.strip():
                    raise ReleaseError("Write user-facing release notes before starting")
                run_id = args.version + "-" + uuid.uuid4().hex[:12]
                directory = root / run_id
                directory.mkdir(mode=0o700)
                value = {"schema": 1, "runId": run_id, "repository": args.repository, "repo": str(repo),
                         "oldVersion": old, "version": args.version, "notes": notes, "files": sorted(set(args.files)),
                         "python": str(Path(sys.executable).absolute()), "timeout": args.timeout,
                         "environment": environment(args.environment, repo), "createdAt": time.time(),
                         "startsDevelopment": True, "stages": {}}
                atomic_json(directory / "state.json", value)
                print("Release run:", run_id, flush=True)
            else:
                if not versions.RUN_ID_RE.fullmatch(args.run_id):
                    raise ReleaseError("Invalid release run ID")
                directory = root / args.run_id
                value = load(directory / "state.json")
                if value.get("schema") != 1 or value.get("runId") != args.run_id or value.get("repo") != str(repo):
                    raise ReleaseError("Release state schema/owner mismatch")
                if getattr(args, "files", None):
                    value["files"] = sorted(set(value["files"]) | set(args.files))
                if getattr(args, "environment", None):
                    value["environment"] = environment(args.environment)
                atomic_json(directory / "state.json", value)
            runner = Runner(repo, directory, value)
            if args.action == "cleanup":
                runner.command.recover_children()
                value["cleanup"] = runner.cleanup_owned()
                runner.state.save()
                runner.report()
                return 0
            old_handler = signal.signal(signal.SIGTERM, lambda _sig, _frame: (_ for _ in ()).throw(KeyboardInterrupt()))
            try:
                runner.run(approved=args.approve_container_cgroup)
            finally:
                signal.signal(signal.SIGTERM, old_handler)
            return 0
    except ReleaseError as exc:
        print(json.dumps({"error": str(exc), "exitCode": exc.code}, ensure_ascii=False), file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("Interrupted; inspect status and resume the saved run. No implicit retry.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
