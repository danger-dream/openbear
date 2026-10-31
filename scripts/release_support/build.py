#!/usr/bin/env python3
"""Independent release stages; the controller, not this module, owns ordering.

CLI: python scripts/release_support/build.py --config /absolute/config.json
     --stage env|py311|py312|py313|frontend|build|package|smoke

Schema 1 config: runId, version, previousVersion, sourceDir, runDir, cacheDir,
previousAssets, pythonImages ({311,312,313}: explicit baseline references),
systemdImage (recorded only). Node is fixed at 20.20.2/npm 11.11.0. env may use
network; all execution stages use network=none. No host dependencies installed.
Each invocation writes results/<stage>/result.json and an immutable attempt log.
Cache images are retained; every container is recorded before creation, labelled
openbear.release.run=<runId>, and removed in finally. No deployment or publishing.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import traceback
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

# Also works when invoked directly as a script or loaded by an isolated test.
if __package__:
    from .candidate import BENCHMARK_FILES, benchmark_path
    from .scope import FRONTEND_CHECKS, build_identity, identities
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from release_support.candidate import BENCHMARK_FILES, benchmark_path
    from release_support.scope import FRONTEND_CHECKS, build_identity, identities

STAGES = ("env", "py311", "py312", "py313", "frontend", "frontend-checks", "build", "package", "smoke")
IMAGES = Path(__file__).resolve().parent / "images"
NODE_BASE = "node:20.20.2-trixie"
GO_BASE = "golang:1.24.4-bookworm"
NODE_VERSION, NPM_VERSION = "v20.20.2", "11.11.0"
LABEL = "openbear.release.run"
TOP_FILES = ("pyproject.toml", "uv.lock", "openbear.service", "openbear.json.example", "README.md")
OPTIONAL_FILES = ("Dockerfile", ".dockerignore", "docs/instances.md")
ANOMALIES = (
    "Task exception was never retrieved", "Task was destroyed but it is pending",
    "PytestUnraisableExceptionWarning", "PytestUnhandledThreadExceptionWarning",
    "Exception ignored in:", "was never awaited", "Unclosed client session", "Unclosed connector",
    "no active connection", "UnhandledPromiseRejection",
    "unhandledRejection", "uncaughtException", "unhandled exception",
    "generated asynchronous activity after the test ended",
)


class ReleaseError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def owned(root, relative):
    """Reject symlink escapes even for not-yet-created output paths."""
    root = Path(root).resolve()
    path = root / relative
    require(path == path.resolve() and path.is_relative_to(root) and path != root,
            f"output path escapes its owner or contains symlinks: {path}")
    return path


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def tree_files(root):
    """Frozen source / published trees contain regular files only, never links."""
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), f"missing or symlinked tree: {root}")
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), f"non-regular input: {path}")
            if stat.S_ISREG(mode):
                files[path.relative_to(root).as_posix()] = sha256(path)
    return dict(sorted(files.items()))


def node_cache_files(root):
    """Hash cache content, preserving npm's internal symlinks but not escapes."""
    root = Path(root).resolve()
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == "complete.json":
                require(not path.is_symlink(), "symlinked cache completion marker")
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                require(path.resolve().is_relative_to(root), f"Node cache link escapes: {path}")
                files[relative] = {"link": os.readlink(path)}
            elif stat.S_ISREG(mode):
                files[relative] = {"sha256": sha256(path), "mode": stat.S_IMODE(mode)}
            else:
                require(stat.S_ISDIR(mode), f"non-regular Node cache file: {path}")
    return dict(sorted(files.items()))


def normalize_dependencies(source):
    lock = tomllib.loads((source / "uv.lock").read_text())
    own = [p for p in lock.get("package", []) if p.get("name") == "openbear"]
    require(len(own) == 1, "uv.lock must contain one openbear project")
    # This release recipe installs dependencies, not a stale editable checkout.
    require(own[0].get("source") == {"virtual": "."}, "baseline recipe requires uv's virtual openbear project")
    own[0]["version"] = "<project-version>"
    project = tomllib.loads((source / "pyproject.toml").read_text())
    project["project"]["version"] = "<project-version>"
    package = read_json(source / "web/package.json")
    package["version"] = "<project-version>"
    node_lock = read_json(source / "web/package-lock.json")
    node_lock["version"] = "<project-version>"
    node_lock["packages"][""]["version"] = "<project-version>"
    web = tree_files(source / "web")
    for name in ("package.json", "package-lock.json"):
        web.pop(name, None)
    require(not any(p.startswith(("node_modules/", "dist/")) for p in web), "source must not contain web/node_modules or web/dist")
    # The known self-contained installer reads only web/scripts + node_modules.
    # Unknown lifecycle commands retain the conservative all-web cache input.
    hooks = {name: value for name, value in package.get("scripts", {}).items()
             if name in {"preinstall", "install", "postinstall", "prepare", "prepublish", "preprepare", "postprepare"}}
    if hooks == {"postinstall": "node scripts/patch-sortable-scroll.mjs"}:
        web = {name: value for name, value in web.items() if name.startswith("scripts/")}
    recipes = tree_files(IMAGES)
    return {"python": {"lock": lock, "project": project},
            "node": {"lock": node_lock, "package": package, "localInputs": web},
            "recipes": recipes}


def validate_config(raw):
    require(raw.get("schema") == 1, "config.schema must be 1")
    for key in ("runId", "version", "previousVersion", "systemdImage"):
        require(isinstance(raw.get(key), str) and raw[key], f"missing {key}")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", raw["runId"]), "invalid runId")
    for key in ("version", "previousVersion"):
        require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", raw[key]), f"invalid {key}")
    config = dict(raw)
    for key in ("sourceDir", "runDir", "cacheDir", "previousAssets"):
        require(isinstance(raw.get(key), str) and raw[key], f"missing {key}")
        path = Path(raw[key]).expanduser().absolute()
        require("," not in str(path) and "\n" not in str(path), f"unsafe mount path: {path}")
        require(path == path.resolve() and path != Path("/"), f"symlink or unsafe root: {path}")
        config[key] = path
    source, run, cache, previous = (config[k] for k in ("sourceDir", "runDir", "cacheDir", "previousAssets"))
    require(source.is_dir() and previous.is_dir(), "sourceDir and previousAssets must exist")
    # Frozen source and previous assets may be children of runDir, but cannot
    # contain an output or overlap each other/the cross-run cache.
    for parent in (source, previous):
        require(not run.is_relative_to(parent) and not cache.is_relative_to(parent), "outputs cannot be inside read-only inputs")
        require(not parent.is_relative_to(cache), "read-only inputs cannot be inside cache")
    require(not source.is_relative_to(previous) and not previous.is_relative_to(source), "inputs overlap")
    require(not cache.is_relative_to(run) and not run.is_relative_to(cache), "runDir and cross-run cacheDir must be separate")
    for name in ("results", "build", "assets", "environment.json", "package-manifest.json", ".build-owner.json"):
        output = owned(run, name)
        require(not source.is_relative_to(output) and not previous.is_relative_to(output), f"input overlaps output {output}")
    images = raw.get("pythonImages")
    require(isinstance(images, dict) and set(images) == {"311", "312", "313"}, "three explicit pythonImages required")
    require(all(isinstance(v, str) and v and not v.startswith("-") for v in images.values()), "invalid Python image reference")
    return config


def safe_extract_zip(archive_path, destination):
    """No traversal, backslashes, links, duplicate entries, devices or zip bombs."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > 200000 or sum(i.file_size for i in entries) > 8 * 1024**3:
            raise ValueError("oversized release ZIP")
        seen = set()
        for item in entries:
            name = item.filename
            parts = PurePosixPath(name).parts
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if (not parts or "\\" in name or "\x00" in name or name.startswith("/")
                    or any(p in ("", "..", ".") or ":" in p for p in name.rstrip("/").split("/"))
                    or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or name.rstrip("/") in seen):
                raise ValueError(f"unsafe ZIP entry: {name!r}")
            seen.add(name.rstrip("/"))
            target = (root / name).resolve()
            if target == root or not target.is_relative_to(root):
                raise ValueError(f"unsafe ZIP target: {name!r}")
        for item in entries:
            target = root / item.filename
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)


def parse_junit(path):
    root = ET.parse(path).getroot()
    cases = []
    for item in root.iter("testcase"):
        skipped = item.find("skipped")
        failed = item.find("failure") is not None or item.find("error") is not None
        cases.append({"id": item.get("classname", "") + "::" + item.get("name", ""),
                      "status": "failed" if failed else "skipped" if skipped is not None else "passed",
                      "skipReason": (skipped.get("message") or skipped.text or "") if skipped is not None else ""})
    require(cases, "JUnit contains no test cases")
    counts = {"tests": len(cases), **{status: sum(c["status"] == status for c in cases)
                                   for status in ("passed", "failed", "skipped")}}
    # Session/collection errors must not vanish just because no testcase owns them.
    require(not any(int(s.get("errors", "0")) > counts["failed"] for s in root.iter("testsuite")), "JUnit has unaccounted session errors")
    return {"counts": counts, "testCases": cases}


def parse_frontend(text):
    values = {}
    for name in ("tests", "pass", "fail", "skipped", "cancelled"):
        matches = re.findall(r"^# " + name + r" (\d+)\s*$", text, re.M)
        require(len(matches) == 1, f"missing or ambiguous Node test summary: {name}")
        values[name] = int(matches[0])
    todo = re.findall(r"^# todo (\d+)\s*$", text, re.M)
    require(len(todo) <= 1, "ambiguous Node todo summary")
    values["todo"] = int(todo[0]) if todo else 0
    require(values["tests"] > 0 and values["tests"] == sum(values[k] for k in ("pass", "fail", "skipped", "cancelled", "todo")), "incomplete Node test summary")
    return {"tests": values["tests"], "passed": values["pass"], "failed": values["fail"],
            "skipped": values["skipped"], "cancelled": values["cancelled"], "todo": values["todo"]}


def anomalies(text):
    return [value for value in ANOMALIES if value.lower() in text.lower()]


# Executed with the actual candidate Python and shared Node binaries in every
# Python test/smoke. No command stubs and no trust in image tags.
PY_PROBE = r'''
import sys, json, platform, subprocess, importlib.metadata as m
from pathlib import Path
assert ".".join(map(str, sys.version_info[:2])) == sys.argv[1], sys.version
command = lambda *a: subprocess.check_output(a, text=True).strip()
p = {"python": platform.python_version(), "executable": sys.executable,
     "implementation": platform.python_implementation(), "pythonABI": sys.implementation.cache_tag,
     "node": command("node", "--version"), "npm": command("npm", "--version"),
     "bash": command("bash", "--version").splitlines()[0], "git": command("git", "--version"),
     "pytest-xdist": m.version("pytest-xdist"), "execnet": m.version("execnet")}
assert p["node"] == "v20.20.2" and p["npm"] == "11.11.0", p
assert p["pytest-xdist"] == "3.8.0" and p["execnet"] == "2.1.2", p
Path(sys.argv[2]).write_text(json.dumps(p))
'''
NODE_PROBE = r'''
const fs = require('node:fs'), cp = require('node:child_process');
const cmd = (...a) => cp.execFileSync(a[0], a.slice(1), {encoding:'utf8'}).trim();
const p = {node:process.version, npm:cmd('npm','--version'), bash:cmd('bash','--version').split('\n')[0], git:cmd('git','--version')};
if (p.node !== 'v20.20.2' || p.npm !== '11.11.0') throw Error(JSON.stringify(p));
fs.writeFileSync(process.argv[1], JSON.stringify(p));
'''


class Stage:
    def __init__(self, config, stage):
        self.config, self.stage = config, stage
        self.run, self.source, self.cache = (config[k] for k in ("runDir", "sourceDir", "cacheDir"))
        self.attempt = uuid.uuid4().hex
        self.result_dir = owned(self.run, f"results/{stage}")
        self.out = owned(self.run, f"results/{stage}/attempts/{self.attempt}")
        self.log_path = self.out / "stage.log"
        self.resources = []
        self.details = {}
        self.source_files = None

    def command(self, args, *, timeout=600, capture=False, check=True):
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write("$ " + shlex.join(map(str, args)) + "\n")
            log.flush()
            if capture:
                proc = subprocess.run(list(map(str, args)), capture_output=True,
                                      text=True, timeout=timeout)
                log.write(proc.stdout or "")
                log.write(proc.stderr or "")
            else:
                proc = subprocess.run(list(map(str, args)), stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        if check:
            require(proc.returncode == 0, f"command exited {proc.returncode}: {args[0:3]}; see {self.log_path}")
        return proc

    def image_id(self, reference):
        proc = self.command(["docker", "image", "inspect", reference, "--format", "{{.Id}}"], capture=True, check=False, timeout=60)
        require(proc.returncode == 0, f"missing explicit image {reference!r}; prepare a baseline using images/Dockerfile.python with an explicit BASE_IMAGE (no fallback)")
        identity = proc.stdout.strip()
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", identity), f"invalid image identity: {identity}")
        return identity

    @contextlib.contextmanager
    def container(self, image, script, mounts, *, online=False, role="stage"):
        name = f"ob-release-{hashlib.sha256(self.config['runId'].encode()).hexdigest()[:12]}-{self.stage}-{self.attempt[:12]}-{uuid.uuid4().hex[:8]}"
        labels = {LABEL: self.config["runId"], "openbear.release.stage": self.stage,
                  "openbear.release.attempt": self.attempt}
        resource = {"kind": "container", "name": name, "labels": labels, "image": image, "role": role, "state": "planned"}
        self.resources.append(resource)
        atomic_json(self.out / "resources.json", self.resources)  # before create, including interruption window
        args = ["docker", "create", "--name", name, "--network", "bridge" if online else "none",
                "--cpus", "2", "--memory", "6g", "--pids-limit", "1024", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "0:0", "--workdir", "/",
                "--entrypoint", "/bin/bash", "--env", "PYTHONDONTWRITEBYTECODE=1",
                "--env", "PATH=/opt/testenv/.venv/bin:/opt/baseline-venv/bin:/inputs/node/toolchain/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "--env", "PYTHONPATH="]
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
        for host, target, readonly in mounts:
            host = Path(host)
            require(host.is_absolute() and host.exists() and host == host.resolve(), f"unsafe/missing mount: {host}")
            args += ["--mount", f"type=bind,src={host},dst={target}" + (",readonly" if readonly else "")]
        args += [image, "-c", "set -euo pipefail\n" + script]
        try:
            self.command(args, timeout=90)
            resource["state"] = "created"
            info = json.loads(self.command(["docker", "container", "inspect", name], capture=True, timeout=30).stdout)[0]
            require(info["Image"] == image and all(info["Config"]["Labels"].get(k) == v for k, v in labels.items()), "container identity/owner mismatch")
            resource["id"] = info["Id"]
            atomic_json(self.out / "resources.json", self.resources)
            yield name
        finally:
            # Inspect exact name + owner, never infer ownership from a prefix.
            proc = self.command(["docker", "container", "inspect", name], capture=True, check=False, timeout=30)
            if proc.returncode == 0:
                info = json.loads(proc.stdout)[0]
                require(all(info["Config"]["Labels"].get(k) == v for k, v in labels.items()), "refusing to remove container with different owner")
                self.command(["docker", "rm", "-f", info["Id"]], timeout=60)
                resource["state"] = "removed"
            else:
                require("no such" in (proc.stderr or "").lower(), "cannot establish container cleanup state")
                resource["state"] = "absent"
            atomic_json(self.out / "resources.json", self.resources)

    def run_container(self, image, script, mounts, *, online=False, timeout=600, commit=False, role="stage"):
        with self.container(image, script, mounts, online=online, role=role) as name:
            proc = self.command(["docker", "start", "--attach", name], timeout=timeout, check=False)
            require(proc.returncode == 0, f"container exited {proc.returncode}; see {self.log_path}")
            if commit:
                identity = self.command(["docker", "commit", name], capture=True, timeout=180).stdout.strip()
                return self.image_id(identity)
        return image

    def dependency_data(self):
        data = normalize_dependencies(self.source)
        if (self.source / "gateway/go.mod").is_file():
            data["gateway"] = {"base": GO_BASE, "modules": {
                name: sha256(self.source / "gateway" / name)
                for name in ("go.mod", "go.sum") if (self.source / "gateway" / name).is_file()}}
        return data

    def probe(self, image, node_dir, version=None, role="probe"):
        target = self.out / f"toolchain-{role}.json"
        if version:
            script = "python -c " + shlex.quote(PY_PROBE) + " " + shlex.quote(version) + " /out/" + target.name
        else:
            script = "node -e " + shlex.quote(NODE_PROBE) + " /out/" + target.name
        self.run_container(image, script, [(node_dir, "/inputs/node", True), (self.out, "/out", False)], timeout=90, role=role)
        return read_json(target)

    def env(self):
        deps = self.dependency_data()
        fingerprint = digest_json(deps)
        baseline_ids = {v: self.image_id(ref) for v, ref in self.config["pythonImages"].items()}
        # The fixed Node baseline may be fetched in env only. Python baselines
        # are always explicit config inputs, never silently pulled/substituted.
        node_lookup = self.command(["docker", "image", "inspect", NODE_BASE, "--format", "{{.Id}}"], capture=True, check=False, timeout=60)
        if node_lookup.returncode:
            self.command(["docker", "pull", NODE_BASE], timeout=600)
        node_base = self.image_id(NODE_BASE)
        node_key = digest_json({"base": node_base, "deps": deps["node"], "recipes": deps["recipes"]})
        node_dir = owned(self.cache, f"node/{node_key}")
        with locked(owned(self.cache, f"locks/node-{node_key}.lock")):
            marker = owned(node_dir, "complete.json")
            if marker.exists():
                record = read_json(marker)
                require(record["key"] == node_key, "Node cache key mismatch")
                node_id = self.image_id(record["image"])
                require((node_dir / "node_modules").is_dir() and (node_dir / "toolchain/bin/node").is_file(), "incomplete Node cache")
                require(record["files"] == node_cache_files(node_dir), "Node cache content changed")
                node_tools = self.probe(node_id, node_dir, role="node")
            else:
                require(not node_dir.exists(), f"uncommitted Node cache, refusing to reuse: {node_dir}")
                temp = owned(self.cache, f"node/.{node_key}.{self.attempt}")
                temp.mkdir(parents=True)
                try:
                    node_id = self.run_container(node_base, "bash /recipes/node-env.sh",
                        [(self.source, "/inputs/source", True), (IMAGES, "/recipes", True), (temp, "/out", False)],
                        online=True, timeout=900, commit=True, role="prepare-node")
                    node_tools = self.probe(node_id, temp, role="prepared-node")
                    atomic_json(temp / "complete.json", {"key": node_key, "image": node_id, "base": node_base, "files": node_cache_files(temp)})
                    os.replace(temp, node_dir)
                finally:
                    if temp.exists():
                        shutil.rmtree(temp)
        py_ids, tools = {}, {"node": node_tools}
        for version, base in baseline_ids.items():
            key = digest_json({"base": base, "python": version, "deps": deps["python"], "recipes": deps["recipes"]})
            directory = owned(self.cache, f"python/{key}")
            with locked(owned(self.cache, f"locks/python-{key}.lock")):
                marker = owned(directory, "complete.json")
                if marker.exists():
                    record = read_json(marker)
                    require(record["key"] == key and record["base"] == base, "Python cache key mismatch")
                    image = self.image_id(record["image"])
                    tools[version] = self.probe(image, node_dir, "3." + version[1:], role="py" + version)
                else:
                    require(not directory.exists(), f"uncommitted Python cache: {directory}")
                    image = self.run_container(base, f"bash /recipes/python-env.sh 3.{version[1:]}",
                        [(self.source, "/inputs/source", True), (IMAGES, "/recipes", True)],
                        online=True, timeout=900, commit=True, role="prepare-py" + version)
                    tools[version] = self.probe(image, node_dir, "3." + version[1:], role="prepared-py" + version)
                    temp = owned(self.cache, f"python/.{key}.{self.attempt}")
                    temp.mkdir(parents=True)
                    atomic_json(temp / "complete.json", {"key": key, "base": base, "image": image})
                    os.replace(temp, directory)
                py_ids[version] = image
        environment = {"schema": 1, "pythonImages": py_ids, "baselineImages": baseline_ids,
                       "baselineReferences": self.config["pythonImages"],
                       "nodeImage": node_id, "nodeDir": str(node_dir), "nodeKey": node_key,
                       "dependencyFingerprint": fingerprint, "toolchain": tools,
                       "systemdImage": self.config["systemdImage"]}
        if (self.source / "gateway/go.mod").is_file():
            lookup = self.command(["docker", "image", "inspect", GO_BASE, "--format", "{{.Id}}"], capture=True, check=False, timeout=60)
            if lookup.returncode:
                self.command(["docker", "pull", GO_BASE], timeout=600)
            environment["goImage"] = self.image_id(GO_BASE)
            environment["goBase"] = GO_BASE
        path = owned(self.run, "environment.json")
        atomic_json(path, environment)
        return {**environment, "outputs": {"environment": str(path), "environmentSha256": sha256(path)}}

    def environment(self):
        env = read_json(owned(self.run, "environment.json"))
        require(env["dependencyFingerprint"] == digest_json(self.dependency_data()), "environment dependencies/recipes no longer match candidate")
        require(env["baselineReferences"] == self.config["pythonImages"], "configured baselines differ from prepared environment")
        node = Path(env["nodeDir"])
        require(node == owned(self.cache, f"node/{env['nodeKey']}"), "environment nodeDir is not owned by cacheDir")
        marker = read_json(owned(node, "complete.json"))
        require(marker["key"] == env["nodeKey"] and marker["image"] == env["nodeImage"], "Node cache identity mismatch")
        require(marker["files"] == node_cache_files(node), "Node cache content changed")
        return env, node

    def execution_script(self, *, python=None, writable_node=False):
        lines = ["mkdir -p /work/source", "cp -a --no-preserve=ownership /inputs/source/. /work/source/",
                 "chmod -R u+rwX /work/source", "test ! -e /work/source/.venv", "test ! -e /work/source/web/node_modules"]
        lines.append("cp -a --no-preserve=ownership /inputs/node/node_modules /work/source/web/node_modules\nchmod -R u+rwX /work/source/web/node_modules" if writable_node
                     else "ln -s /inputs/node/node_modules /work/source/web/node_modules")
        if python:
            lines += ["ln -s /opt/testenv/.venv /work/source/.venv", "cd /work/source",
                      "python -c " + shlex.quote(PY_PROBE) + f" 3.{python[1:]} /out/toolchain.json"]
        else:
            lines += ["node -e " + shlex.quote(NODE_PROBE) + " /out/toolchain.json"]
        return "\n".join(lines) + "\n"

    def tests(self):
        env, node = self.environment()
        python = self.stage[2:] if self.stage.startswith("py") else "312" if self.stage == "frontend-checks" else None
        image = self.image_id(env["pythonImages"][python] if python else env["nodeImage"])
        command = ("cd /work/source\npython -m pytest -q -n 2 --dist=worksteal --junitxml=/out/junit.xml -o cache_dir=/out/pytest-cache"
                   if python else "cd /work/source/web\nnode --test --test-concurrency=2")
        if self.stage == "frontend-checks":
            require(all((self.source / name).is_file() for name in FRONTEND_CHECKS), "declared frontend contract tests are missing; review the release policy")
            command += " " + " ".join(map(shlex.quote, FRONTEND_CHECKS))
        failure = None
        try:
            self.run_container(image, self.execution_script(python=python) + command,
                [(self.source, "/inputs/source", True), (node, "/inputs/node", True), (self.out, "/out", False)])
        except (ReleaseError, subprocess.TimeoutExpired) as exc:
            failure = exc
        # Parse actual output even on test failure, retaining full failed/skipped sets.
        text = self.log_path.read_text(errors="replace")
        self.details.update(image=image, unhandled=anomalies(text))
        if (self.out / "toolchain.json").exists():
            self.details["toolchain"] = read_json(self.out / "toolchain.json")
        if python:
            if (self.out / "junit.xml").exists():
                self.details.update(parse_junit(self.out / "junit.xml"))
        else:
            self.details["counts"] = parse_frontend(text)
        self.details["outputs"] = {"log": str(self.log_path)}
        if python and (self.out / "junit.xml").exists():
            self.details["outputs"].update(junit=str(self.out / "junit.xml"), junitSha256=sha256(self.out / "junit.xml"))
        if failure:
            raise failure
        require("counts" in self.details and self.details["counts"]["failed"] == 0
                and self.details["counts"].get("cancelled", 0) == 0 and not self.details["unhandled"], "test failure or unhandled asynchronous exception")
        return self.details

    def frontend_identity(self, env):
        inputs = identities({name: (self.source / name).read_bytes() for name in self.source_files if not name.startswith(".git/")})
        return build_identity(inputs["frontendStage"], env, self.config["version"])

    def build_outputs(self, dist, marker):
        # The canonical build is disposable; evidence paths must survive the next
        # candidate/environment's build without being moved or overwritten.
        target = self.out / "dist"
        if not target.exists():
            shutil.copytree(dist, target)
        manifest = self.out / "manifest.json"
        shutil.copy2(marker, manifest)
        return {"dist": str(target), "manifest": str(manifest),
                "distSha256": digest_json(tree_files(target)), "manifestSha256": sha256(manifest)}

    def build(self):
        env, node = self.environment()
        release_scope = self.config.get("releaseScope")
        identity = self.frontend_identity(env)
        reuse = bool(release_scope and not release_scope["frontendChanged"]
                     and release_scope.get("previousFrontendStageFingerprint") == identity["source"])
        if reuse:
            self.previous_assets()
        root = owned(self.run, "build")
        marker, dist = root / "manifest.json", root / "dist"
        if marker.exists() or dist.exists():
            require(marker.is_file() and dist.is_dir(), "completed/unidentified build must not be overwritten")
            record = read_json(marker)
            require(record["identity"] == identity and record["files"] == tree_files(dist), "existing build belongs to a different candidate or was modified")
            return {"reused": True, "toolchain": record["toolchain"], "outputs": self.build_outputs(dist, marker)}
        if reuse:
            actual = identities({name: (self.source / name).read_bytes() for name in self.source_files if name.startswith("web/")})["frontend"]
            require(actual == release_scope["frontendFingerprint"] == release_scope["previousFrontendFingerprint"], "frontend reuse source proof mismatch")
            previous = self.out / "previous"
            safe_extract_zip(self.config["previousAssets"] / f"openbear-{self.config['previousVersion']}.zip", previous)
            old_dist = previous / "web/dist"
            files_before = tree_files(old_dist)
            metadata = read_json(old_dist / "build-info.json")
            require(metadata.get("schema") == 1 and metadata.get("version") == self.config["previousVersion"]
                    and re.fullmatch(r"[0-9a-f]{16}", metadata.get("buildId", "")), "previous frontend build identity is missing or invalid")
            # Preserve compiled JS/CSS/HTML and its buildId; only stamp this
            # package's release version for existing installer/updater validators.
            target = self.out / "dist"
            shutil.copytree(old_dist, target)
            metadata["version"] = self.config["version"]
            atomic_json(target / "build-info.json", metadata)
            files = tree_files(target)
            require({n: h for n, h in files.items() if n != "build-info.json"} ==
                    {n: h for n, h in files_before.items() if n != "build-info.json"}, "reused compiled frontend bytes changed")
            require((target / "index.html").is_file(), "previous frontend has no index")
            tools = env["toolchain"]["node"]
            root.mkdir(exist_ok=True)
            shutil.copytree(target, dist)
            record = {"identity": identity, "files": files, "toolchain": tools, "reusedFrontend": True,
                      "fromVersion": self.config["previousVersion"], "buildId": metadata["buildId"]}
            atomic_json(marker, record)
            shutil.rmtree(previous)
            return {**record, "outputs": self.build_outputs(dist, marker)}
        image = self.image_id(env["nodeImage"])
        self.run_container(image, self.execution_script(writable_node=True) + "cd /work/source/web\nnpm run build -- --outDir /out/dist",
            [(self.source, "/inputs/source", True), (node, "/inputs/node", True), (self.out, "/out", False)], timeout=600)
        require((self.out / "dist/index.html").is_file(), "Vite produced no index.html")
        files = tree_files(self.out / "dist")
        tools = read_json(self.out / "toolchain.json")
        require(not anomalies(self.log_path.read_text(errors="replace")), "build logged an unhandled exception")
        root.mkdir(exist_ok=True)
        shutil.copytree(self.out / "dist", dist)
        atomic_json(marker, {"identity": identity, "files": files, "toolchain": tools})
        return {"image": image, "toolchain": tools, "outputs": self.build_outputs(dist, marker)}

    def previous_assets(self):
        root = self.config["previousAssets"]
        names = {"install.sh", f"openbear-{self.config['previousVersion']}.zip", "release-meta.json", "SHA256SUMS"}
        require({p.name for p in root.iterdir()} == names, "previousAssets must contain the actual four assets only")
        files = tree_files(root)
        sums = {}
        for line in (root / "SHA256SUMS").read_text().splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^/\\]+)", line)
            require(match and match[2] not in sums, "invalid previous SHA256SUMS")
            sums[match[2]] = match[1].lower()
        require(sums == {n: files[n] for n in names - {"SHA256SUMS"}}, "previous asset checksums do not match")
        require(read_json(root / "release-meta.json")["version"] == self.config["previousVersion"], "previous version mismatch")
        return files

    def package_identity(self):
        build = read_json(owned(self.run, "build/manifest.json"))
        env, _node = self.environment()
        require(build["identity"] == self.frontend_identity(env), "build inputs differ from candidate")
        require(build["files"] == tree_files(owned(self.run, "build/dist")), "built frontend changed")
        return {"runId": self.config["runId"], "version": self.config["version"],
                "previousVersion": self.config["previousVersion"], "source": digest_json(self.source_files),
                "build": digest_json(build), "previousAssets": self.previous_assets(),
                **({"gatewayImage": env.get("goImage"), "gatewayBase": env.get("goBase")}
                   if (self.source / "gateway/go.mod").is_file() else {})}

    def check_assets(self, identity=None):
        manifest = read_json(owned(self.run, "package-manifest.json"))
        if identity is not None:
            require(manifest["identity"] == identity, "existing package belongs to a different candidate")
        assets = owned(self.run, "assets")
        require(set(p.name for p in assets.iterdir()) == set(manifest["assets"]), "asset set changed")
        require(tree_files(assets) == {name: item["sha256"] for name, item in manifest["assets"].items()}, "published asset hash mismatch")
        require(all((assets / name).stat().st_size == item["bytes"] for name, item in manifest["assets"].items()), "asset size mismatch")
        return manifest

    def gateway_package(self, package):
        if not (self.source / "gateway/go.mod").is_file():
            return
        env, _ = self.environment()
        require(env.get("goBase") == GO_BASE and env.get("goImage"), "gateway build environment is missing or stale")
        image = self.image_id(env["goImage"])
        target = package / "gateway/bin"
        target.mkdir(parents=True)
        script = """export PATH=/usr/local/go/bin:$PATH
export GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOPROXY=off GOSUMDB=off
mkdir -p /work/gateway
cp -a --no-preserve=ownership /inputs/gateway/. /work/gateway/
cd /work/gateway
chmod -R u+rwX .
go version | grep -Eq '^go version go1\\.24\\.4 linux/(amd64|arm64)$'
go version > /out/toolchain.txt
go test -race ./...
for arch in amd64 arm64; do
  CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/openbear-gateway-linux-$arch ./cmd/openbear-gateway
done
/out/openbear-gateway-linux-$(go env GOARCH) --version
"""
        self.run_container(image, script, [(self.source / "gateway", "/inputs/gateway", True),
                                           (target, "/out", False)], role="gateway-build")
        files = {}
        for arch in ("amd64", "arm64"):
            binary = target / f"openbear-gateway-linux-{arch}"
            require(binary.is_file() and binary.read_bytes()[:4] == b"\x7fELF", "gateway binary is missing or invalid")
            files[binary.name] = {"sha256": sha256(binary), "bytes": binary.stat().st_size}
        toolchain = (target / "toolchain.txt").read_text().strip()
        (target / "toolchain.txt").unlink()
        atomic_json(target / "manifest.json", {"schema": 1, "toolchain": toolchain, "image": image, "binaries": files})
        if (self.source / "gateway/README.md").is_file():
            shutil.copy2(self.source / "gateway/README.md", package / "gateway/README.md")
        self.details["gateway"] = {"image": image, "toolchain": toolchain, "binaries": files}

    def package(self):
        identity = self.package_identity()
        assets = owned(self.run, "assets")
        if assets.exists() and any(assets.iterdir()):
            manifest = self.check_assets(identity)
            return {**manifest, "reused": True, "outputs": {"assets": str(assets), "zip": str(assets / f"openbear-{self.config['version']}.zip"), "manifest": str(self.run / "package-manifest.json"), "zipSha256": manifest["zipSha256"]}}
        package, previous, pending = (self.out / name for name in ("package", "previous", "assets"))
        package.mkdir()
        previous_zip = self.config["previousAssets"] / f"openbear-{self.config['previousVersion']}.zip"
        safe_extract_zip(previous_zip, previous)
        require((previous / "app").is_dir(), "previous ZIP must have a flat release root")
        for name in ("install.sh", "release-meta.json"):
            inner = previous / ("scripts/install.sh" if name == "install.sh" else name)
            require(sha256(inner) == identity["previousAssets"][name], f"previous ZIP/{name} differs from external asset")
        def ignore(directory, names):
            result = {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}
            if Path(directory) == self.source / "scripts":
                result.update({"release.py", "release_local.py", "release_support"} & set(names))
            return result
        for name in ("app", "prompts", "scripts"):
            shutil.copytree(self.source / name, package / name, ignore=ignore)
        copy_benchmarks(self.source, package)
        for name in TOP_FILES:
            shutil.copy2(self.source / name, package / name)
        for name in OPTIONAL_FILES:
            if (self.source / name).is_file():
                (package / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.source / name, package / name)
        shutil.copytree(owned(self.run, "build/dist"), package / "web/dist")
        self.gateway_package(package)
        audit_package(package)
        classify_code = '''import importlib.util,json,sys
from pathlib import Path
root,previous=map(Path,sys.argv[1:3])
spec=importlib.util.spec_from_file_location("release_candidate_updater",root/"scripts/updater.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
for _ in range(2):
 c=m.classify_trees(previous,root)
 if "error" in c: raise RuntimeError(c["error"])
 m.write_release_meta(root/"release-meta.json",version=sys.argv[3],compared_with=sys.argv[4],classification=c)
print(json.dumps(c))
'''
        result = self.command([sys.executable, "-B", "-c", classify_code, package, previous, self.config["version"], self.config["previousVersion"]], capture=True)
        classification = json.loads(result.stdout)
        atomic_json(self.out / "classification.json", classification)
        validation = self.command([sys.executable, "-B", package / "scripts/release_validation.py", "validate-tree", "--root", package, "--version", self.config["version"]], capture=True)
        report = json.loads(validation.stdout)
        require(report.get("ok") is True, "candidate validate-tree did not pass")
        atomic_json(self.out / "validation.json", report)
        files = audit_package(package)
        pending.mkdir()
        name = f"openbear-{self.config['version']}.zip"
        temp_zip = pending / (name + ".tmp")
        with zipfile.ZipFile(temp_zip, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for relative in files:
                archive.write(package / relative, relative)
        with zipfile.ZipFile(temp_zip) as archive:
            require(archive.testzip() is None, "ZIP CRC failure")
        os.replace(temp_zip, pending / name)
        shutil.copy2(package / "scripts/install.sh", pending / "install.sh")
        shutil.copy2(package / "release-meta.json", pending / "release-meta.json")
        (pending / "SHA256SUMS").write_text("".join(f"{sha256(pending / n)}  {n}\n" for n in ("install.sh", name, "release-meta.json")))
        manifest = {"identity": identity, "assets": {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(pending.iterdir())},
                    "zipSha256": sha256(pending / name), "classification": classification}
        # Manifest is durable before atomic publication of the entire four-asset set.
        atomic_json(owned(self.run, "package-manifest.json"), manifest)
        os.replace(pending, assets)
        self.check_assets(identity)
        return {**manifest, "outputs": {"assets": str(assets), "zip": str(assets / name), "manifest": str(self.run / "package-manifest.json"), "zipSha256": manifest["zipSha256"]}}

    def smoke(self):
        identity = self.package_identity()
        manifest = self.check_assets(identity)
        env, node = self.environment()
        image = self.image_id(env["pythonImages"]["312"])
        archive = owned(self.run, f"assets/openbear-{self.config['version']}.zip")
        # Execute this exact safe extraction function inside the container; never
        # mount the development/source checkout for a package smoke.
        extract = "from pathlib import Path, PurePosixPath\nimport stat, zipfile, shutil\n" + inspect.getsource(safe_extract_zip) + "\nsafe_extract_zip('/inputs/release.zip', '/work/package')\n"
        script = ("python -c " + shlex.quote(PY_PROBE) + " 3.12 /out/toolchain.json\n"
                  + "python -c " + shlex.quote(extract) + "\ncd /work/package\n"
                  + "python scripts/smoke_release_login.py --root /work/package --output /out/smoke.json")
        self.run_container(image, script, [(archive, "/inputs/release.zip", True), (node, "/inputs/node", True), (self.out, "/out", False)])
        report = read_json(self.out / "smoke.json")
        self.details.update(smoke=report, zipSha256=manifest["zipSha256"], toolchain=read_json(self.out / "toolchain.json"))
        require(report.get("ok") is True and report.get("version") == self.config["version"], "package smoke failed/version mismatch")
        require(all(report.get("serviceLifecycle", {}).get(key) is True
                    for key in ("fullStartup", "ready", "cleanShutdown")),
                "package smoke did not verify full service startup/readiness/shutdown")
        require(not anomalies(self.log_path.read_text(errors="replace")), "package smoke logged an unhandled exception")
        require(sha256(archive) == manifest["zipSha256"], "ZIP changed during smoke")
        return {**self.details, "outputs": {"smoke": str(self.out / "smoke.json"), "smokeSha256": sha256(self.out / "smoke.json"), "zip": str(archive), "zipSha256": manifest["zipSha256"]}}

    def execute(self):
        owner = owned(self.run, ".build-owner.json")
        wanted = {"runId": self.config["runId"], "sourceDir": str(self.source), "version": self.config["version"]}
        if owner.exists():
            require(read_json(owner) == wanted, "runDir belongs to another release")
        with locked(owned(self.run, ".build-owner.lock")):
            if owner.exists():
                require(read_json(owner) == wanted, "runDir belongs to another release")
            else:
                atomic_json(owner, wanted)
        with locked(owned(self.run, f"results/{self.stage}/stage.lock")):
            return self._execute_locked()

    def _execute_locked(self):
        self.out.mkdir(parents=True)
        self.log_path.touch()
        start = time.perf_counter()
        result = {"ok": False, "stage": self.stage, "attempt": self.attempt}
        try:
            self.source_files = tree_files(self.source)
            require(not any(n.startswith((".venv/", "node_modules/")) for n in self.source_files), "source is not a frozen public input")
            result.update(getattr(self, "tests" if self.stage.startswith("py") or self.stage in {"frontend", "frontend-checks"} else self.stage)())
            require(self.source_files == tree_files(self.source), "frozen source changed during execution")
            result["ok"] = True
        except (Exception, KeyboardInterrupt) as exc:
            result.update(self.details)
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["exitCode"] = 130 if isinstance(exc, KeyboardInterrupt) else 124 if isinstance(exc, subprocess.TimeoutExpired) else 1
            with self.log_path.open("a") as log:
                traceback.print_exc(file=log)
        result.update(stage=self.stage, seconds=round(time.perf_counter() - start, 3), log=str(self.log_path), resources=str(self.out / "resources.json"))
        result.setdefault("exitCode", 0 if result["ok"] else 1)
        result.setdefault("outputs", {})["log"] = str(self.log_path)
        result["outputs"]["logSha256"] = sha256(self.log_path)
        if self.source_files is not None:
            result["sourceFingerprint"] = digest_json(self.source_files)
        atomic_json(self.out / "result.json", result)
        atomic_json(self.result_dir / "result.json", result)
        return result


def copy_benchmarks(source, package):
    base = source / "benchmark-package"
    files = tree_files(base)
    for tool in BENCHMARK_FILES:
        require(any(name.startswith(tool + "/") and name.endswith("/manifest.json") for name in files), f"benchmark package is missing: {tool}")
    for name in files:
        require(benchmark_path("benchmark-package/" + name), f"unexpected benchmark resource: {name}")
    for name in files:
        if not name.endswith("/manifest.json"):
            continue
        manifest = read_json(base / name)
        parent = PurePosixPath(name).parent
        for rel, digest in manifest.get("files", {}).items():
            require(files.get(str(parent / rel)) == digest, f"benchmark resource checksum mismatch: {parent / rel}")
    shutil.copytree(base, package / "benchmark-package")


def audit_package(root):
    files = tree_files(root)
    forbidden = {".git", ".venv", ".env", "__pycache__", "node_modules", "tests", "test", "harness", "test_harness", "data", "workspace", "logs", "private", "private-data", "release_support", ".pytest_cache", ".ruff_cache"}
    for relative in files:
        parts = PurePosixPath(relative).parts
        name = parts[-1].lower()
        require(not (set(parts) & forbidden) and name not in {"openbear.json", "release.py", "release_local.py"}
                and not name.startswith(".env") and not name.endswith((".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key"))
                and not any(name.endswith(suffix) for suffix in (".db-wal", ".db-shm", ".sqlite3-wal", ".sqlite3-shm")),
                f"private data or test/release harness in package: {relative}")
        # The node is released on its own (node-v* tags); controllers download it.
        require(parts[0] != "node" and not name.startswith("openbear-node"), f"node source or program in controller package: {relative}")
        gateway_files = {"gateway/README.md", "gateway/bin/manifest.json", "gateway/bin/openbear-gateway-linux-amd64", "gateway/bin/openbear-gateway-linux-arm64"}
        require(parts[0] in {"app", "prompts", "scripts", "web"} or benchmark_path(relative) or relative in (*TOP_FILES, *OPTIONAL_FILES, "release-meta.json") or relative in gateway_files, f"unexpected package file: {relative}")
        require(parts[0] != "web" or relative.startswith("web/dist/"), f"non-built frontend in package: {relative}")
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--stage", required=True, choices=STAGES)
    args = parser.parse_args(argv)
    try:
        config = validate_config(read_json(args.config.resolve()))
        result = Stage(config, args.stage).execute()
    except (Exception, KeyboardInterrupt) as exc:
        # Do not write through unvalidated/unowned paths on malformed config.
        print(json.dumps({"ok": False, "stage": args.stage, "seconds": 0, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return result["exitCode"]


if __name__ == "__main__":
    raise SystemExit(main())
