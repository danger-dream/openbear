"""Independent openbear-node release: its own version, tag and five assets.

Stages after the shared local freeze: source (the controller release's public
export) -> node-deps (online Go module download into a keyed cache) ->
node-test (offline ``go test -race``) -> node-build (offline cross builds,
executable header and version checks, manifest) -> publication (the shared
push/tag/draft/upload/verify path with ``make_latest=false``; beta and rc are
GitHub pre-releases) -> local node/VERSION to the next minor's dev.1. There is
no ZIP and no A/B installation acceptance; the controller package never carries it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path

from . import build, candidate, versions
from .common import ReleaseError, atomic_json, fingerprint, load, process_ticks, sha
from .publication import asset_metadata, publish
from .runner import Runner

TAG_PREFIX = "node-v"
RUN_ID_RE = re.compile("node-" + versions.RUN_ID_RE.pattern)
STAGES = ("node-deps", "node-test", "node-build")
GO_VERSION = re.fullmatch(r"golang:(\d+\.\d+\.\d+)-[A-Za-z0-9.]+", build.GO_BASE)[1]


def _load_build_node():
    """Reuse the local build script's version/protocol/target definitions."""
    path = Path(__file__).resolve().parents[1] / "build_node.py"
    spec = importlib.util.spec_from_file_location("openbear_release_build_node", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_node = _load_build_node()
TARGETS = tuple(build_node.TARGETS)


def binary_name(target: str) -> str:
    return f"openbear-node-{target}" + (".exe" if target.startswith("windows-") else "")


BINARIES = tuple(binary_name(target) for target in TARGETS)
ASSETS = (*BINARIES, "manifest.json")


def parse_version(value: str) -> str:
    """A node version as written in node/VERSION and tags (development ones included), or ReleaseError."""
    if not versions.written(value):
        raise ReleaseError("Node version must be X.Y.Z, X.Y.Z-rc.N, X.Y.Z-beta.N or X.Y.Z-dev.N")
    return value


def node_version(repo) -> str:
    try:
        return build_node.version(Path(repo) / "node")
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"Cannot read node/VERSION: {exc}") from None


def _write_version(repo, value, _command) -> None:
    if node_version(repo) != value["version"]:
        (Path(repo) / "node/VERSION").write_text(value["version"] + "\n", encoding="utf-8")


def _check_version(repo, value, _command) -> None:
    if node_version(repo) != value["version"]:
        raise ReleaseError("node/VERSION does not hold this run's version")


VERSIONING = candidate.Versioning(files={"node/VERSION"}, read=node_version, write=_write_version, check=_check_version,
                                  message="release: prepare node {version}", retain=STAGES, allow_unchanged=True,
                                  develop_message="release: start node {version} development")


def remote_versions(github) -> list[str]:
    """Every published node-v<version> tag, oldest first; other tag shapes are not versions."""
    return versions.published(github, TAG_PREFIX)


def platform_of(data: bytes):
    """(GOOS, GOARCH) read from an ELF, Mach-O or PE header; None if unknown."""
    if data[:4] == b"\x7fELF" and len(data) >= 20 and data[4] == 2 and data[5] == 1:
        return {0x3E: ("linux", "amd64"), 0xB7: ("linux", "arm64")}.get(struct.unpack_from("<H", data, 18)[0])
    if data[:4] == b"\xcf\xfa\xed\xfe" and len(data) >= 8:
        return {0x01000007: ("darwin", "amd64"), 0x0100000C: ("darwin", "arm64")}.get(struct.unpack_from("<I", data, 4)[0])
    if data[:2] == b"MZ" and len(data) >= 64:
        offset = struct.unpack_from("<I", data, 0x3C)[0]
        if data[offset:offset + 4] == b"PE\0\0" and len(data) >= offset + 6:
            return {0x8664: ("windows", "amd64"), 0xAA64: ("windows", "arm64")}.get(struct.unpack_from("<H", data, offset + 4)[0])
    return None


def check_binaries(directory: Path) -> None:
    for target in TARGETS:
        path = directory / binary_name(target)
        if not path.is_file():
            raise ReleaseError("Missing node binary: " + path.name)
        with path.open("rb") as stream:
            found = platform_of(stream.read(65536))
        if found != tuple(target.split("-")):
            raise ReleaseError(f"{path.name} is not a {target} executable (header: {found})")


def check_reported(output: str, version: str, protocol: int) -> None:
    """`openbear-node version` must report exactly this release (and its protocol, when printed)."""
    match = re.fullmatch(r"openbear-node (\S+)(?: \(protocol v(\d+)\))?", output.strip())
    if not match or match[1] != version or (match[2] is not None and int(match[2]) != protocol):
        raise ReleaseError(f"Built node reports {output.strip()!r}, expected version {version}")


MANIFEST_KEYS = ("schema", "version", "protocol", "protocolMin", "features", "toolchain", "binaries")


def manifest(directory: Path, version: str, protocol: int, toolchain: str, *, protocol_min: int, features: list[str]) -> dict:
    """Same schema as scripts/build_node.py; controllers select downloads by it."""
    binaries = {}
    for name in BINARIES:
        data = (directory / name).read_bytes()
        binaries[name] = {"sha256": sha(data), "bytes": len(data)}
    return {"schema": 1, "version": version, "protocol": protocol, "protocolMin": protocol_min, "features": list(features),
            "toolchain": toolchain, "binaries": binaries}


def check_manifest(directory: Path, version: str, protocol: int, protocol_min: int, features: list[str]) -> dict:
    value = load(directory / "manifest.json")
    if (list(value) != list(MANIFEST_KEYS) or value["schema"] != 1 or value["version"] != version
            or type(value["protocol"]) is not int or value["protocol"] != protocol
            or type(value["protocolMin"]) is not int or value["protocolMin"] != protocol_min or not 1 <= protocol_min <= protocol
            or not isinstance(value["features"], list) or not all(isinstance(f, str) for f in value["features"])
            or value["features"] != list(features)
            or not str(value["toolchain"]).startswith("go version ") or list(value["binaries"]) != list(BINARIES)):
        raise ReleaseError("Node manifest does not match its schema/version/protocol/features")
    if value != manifest(directory, version, protocol, value["toolchain"], protocol_min=protocol_min, features=features):
        raise ReleaseError("Node manifest digests do not match the binaries")
    return value


PREPARE = f"""export PATH=/usr/local/go/bin:$PATH HOME=/root GOCACHE=/tmp/go-build GOTOOLCHAIN=local GOFLAGS=-mod=readonly
mkdir -p /work/node
cp -a --no-preserve=ownership /inputs/node/. /work/node/
chmod -R u+rwX /work/node
cd /work/node
go version | grep -Eq '^go version go{re.escape(GO_VERSION)} linux/(amd64|arm64)$'
"""
OFFLINE = """export GOPROXY=off GOSUMDB=off GOMODCACHE=/work/gomod
mkdir -p /work/gomod
cp -a --no-preserve=ownership /inputs/gomod/. /work/gomod/
chmod -R u+rwX /work/gomod
go mod verify
go version > /out/toolchain.txt
"""
DEPS_SCRIPT = PREPARE + "mkdir -p /out/gomod\nexport GOMODCACHE=/out/gomod\ngo mod download\ngo mod verify\n"
TEST_SCRIPT = PREPARE + OFFLINE + "go test -race ./...\n"


def build_script(version: str) -> str:
    parse_version(version)  # Interpolated into the shell script below.
    lines = [PREPARE + OFFLINE + "mkdir -p /out/assets"]
    for target in TARGETS:
        goos, arch = target.split("-")
        lines.append(f"CGO_ENABLED=0 GOOS={goos} GOARCH={arch} go build -trimpath -buildvcs=false "
                     f"-ldflags='-s -w -X openbear/node.Version={version}' -o /out/assets/{binary_name(target)} ./cmd/openbear-node")
    lines.append("/out/assets/openbear-node-linux-amd64 version > /out/version.txt")
    return "\n".join(lines) + "\n"


class NodeRunner(Runner):
    """Same run directory, state, receipts, cleanup and resume semantics as Runner."""

    versioning = VERSIONING
    resume_command = "scripts/release.py node resume"

    @property
    def tag(self) -> str:
        return TAG_PREFIX + self.value["version"]

    def preflight(self, approved: bool = False):
        if not self.github.token:
            raise ReleaseError("Provide GH_TOKEN (or GITHUB_TOKEN) in the environment; it is never read from application data")
        self.command(["docker", "info", "--format", "{{.ServerVersion}}"], "docker-info")
        self.value["dockerReady"] = True
        self.state.save()
        self.command.recover_children()
        self.cleanup_owned()
        if not self.value.get("publicBase"):
            permission = self.github.api("")
            if not permission.get("permissions", {}).get("push"):
                raise ReleaseError("GitHub credential lacks push access for the requested repository")
            version = versions.target(self.value["version"], "Node version")
            if self.github.api("/releases/tags/" + self.tag, missing=True) or self.github.api("/git/ref/tags/" + self.tag, missing=True):
                raise ReleaseError("Node version/tag already exists; do not start a duplicate release")
            published = remote_versions(self.github)
            versions.require_newer(version, published, TAG_PREFIX)
            previous = versions.changelog_base(version, published)
            notes = self.value["notes"]
            if previous:
                compare = f"https://github.com/{self.value['repository']}/compare/{TAG_PREFIX}{previous}...{self.tag}"
                if "Full Changelog" in notes and compare not in notes:
                    raise ReleaseError("Release notes have an incorrect Full Changelog range")
                if "Full Changelog" not in notes:
                    self.value["notes"] = notes.rstrip() + f"\n\n**Full Changelog**: {compare}\n"
            self.value.update(previousNodeVersion=previous, publicBase=self.github.api("/git/ref/heads/main")["object"]["sha"])
            (self.directory / "notes.md").write_text(self.value["notes"], encoding="utf-8")
            self.state.save()
        self.value["goImage"] = self.go_image()
        self.state.save()

    def go_image(self) -> str:
        inspect = ["docker", "image", "inspect", build.GO_BASE, "--format", "{{.Id}}"]
        try:
            identity = self.command(inspect, "go-image")
        except ReleaseError:
            self.command(["docker", "pull", build.GO_BASE], "go-pull")
            identity = self.command(inspect, "go-image")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
            raise ReleaseError("Invalid Go image identity")
        return identity

    def node_files(self) -> dict:
        return {p: h for p, h in self.value["candidate"]["files"].items() if p.startswith("node/")}

    def check_source(self):
        node = self.directory / "source/node"
        files = self.node_files()
        if not {"node/go.mod", "node/VERSION"} <= set(files) or not any(p.startswith("node/cmd/openbear-node/") for p in files):
            raise ReleaseError("Public candidate does not contain the node source (commit node/ with --files)")
        if node_version(self.directory / "source") != self.value["version"]:
            raise ReleaseError("Public node/VERSION differs from this run's version")
        return node

    def stage(self, name: str):
        config = {"runId": self.value["runId"], "runDir": self.directory.resolve(),
                  "sourceDir": (self.directory / "source").resolve(), "cacheDir": self.cache.resolve()}
        stage = build.Stage(config, name)
        stage.out.mkdir(parents=True)
        stage.log_path.touch()
        return stage

    def contain(self, stage, script, mounts, *, online=False):
        """Labelled, capability-dropped container from the shared build stage helper."""
        try:
            stage.run_container(self.value["goImage"], script, mounts, online=online,
                                timeout=self.value.get("timeout", 1800), role=stage.stage)
        except (build.ReleaseError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError(f"{stage.stage}: {exc}; log: {stage.log_path}") from None

    def deps_key(self) -> str:
        files = self.value["candidate"]["files"]
        return fingerprint({"image": self.value["goImage"], "go.mod": files["node/go.mod"], "go.sum": files.get("node/go.sum")})

    def deps_valid(self, result) -> bool:
        try:
            root = build.owned(self.cache, "go-node/" + result["key"])
            record = load(root / "complete.json")
            return (result["key"] == self.deps_key() and record["key"] == result["key"]
                    and record["files"] == build.tree_files(root / "gomod"))
        except (OSError, ValueError, KeyError, build.ReleaseError):
            return False

    def deps(self) -> dict:
        key = self.deps_key()
        root = build.owned(self.cache, "go-node/" + key)
        with build.locked(build.owned(self.cache, f"locks/go-node-{key}.lock")):
            if root.exists():
                result = {"ok": True, "key": key, "dir": str(root), "reused": True}
                if not self.deps_valid(result):
                    raise ReleaseError(f"Go module cache is incomplete or changed; inspect it before removing: {root}")
                return result
            stage = self.stage("node-deps")
            self.contain(stage, DEPS_SCRIPT, [(self.directory / "source/node", "/inputs/node", True), (stage.out, "/out", False)], online=True)
            pending = build.owned(self.cache, f"go-node/.{key}.{stage.attempt}")
            pending.mkdir(parents=True)
            (stage.out / "gomod").replace(pending / "gomod")
            atomic_json(pending / "complete.json", {"key": key, "image": self.value["goImage"], "files": build.tree_files(pending / "gomod")})
            pending.replace(root)
            return {"ok": True, "key": key, "dir": str(root), "reused": False,
                    "outputs": {"log": str(stage.log_path), "logSha256": sha(stage.log_path.read_bytes())}}

    def test(self, deps) -> dict:
        stage = self.stage("node-test")
        self.contain(stage, TEST_SCRIPT, [(self.directory / "source/node", "/inputs/node", True),
                                          (Path(deps["dir"]) / "gomod", "/inputs/gomod", True), (stage.out, "/out", False)])
        return {"ok": True, "image": self.value["goImage"], "toolchain": (stage.out / "toolchain.txt").read_text().strip(),
                "outputs": {"log": str(stage.log_path), "logSha256": sha(stage.log_path.read_bytes())}}

    def build_binaries(self, deps) -> dict:
        version = self.value["version"]
        source = self.directory / "source/node"
        try:
            protocol, protocol_min, features = build_node.protocol(source), build_node.protocol_min(source), build_node.features(source)
        except ValueError as exc:
            raise ReleaseError(f"node/protocol.go: {exc}") from None
        stage = self.stage("node-build")
        self.contain(stage, build_script(version), [(self.directory / "source/node", "/inputs/node", True),
                                                    (Path(deps["dir"]) / "gomod", "/inputs/gomod", True), (stage.out, "/out", False)])
        out = stage.out / "assets"
        check_binaries(out)
        reported = (stage.out / "version.txt").read_text().strip()
        check_reported(reported, version, protocol)
        toolchain = (stage.out / "toolchain.txt").read_text().strip()
        value = manifest(out, version, protocol, toolchain, protocol_min=protocol_min, features=features)
        (out / "manifest.json").write_text(json.dumps(value, indent=2) + "\n")
        if {p.name for p in out.iterdir()} != set(ASSETS):
            raise ReleaseError("Node build produced an unexpected asset set")
        check_manifest(out, version, protocol, protocol_min, features)
        assets = {name: {"bytes": (out / name).stat().st_size, "sha256": sha((out / name).read_bytes())} for name in ASSETS}
        result = {"ok": True, "version": version, "protocol": protocol, "protocolMin": protocol_min, "features": features,
                  "toolchain": toolchain, "image": self.value["goImage"],
                  "versionOutput": reported, "assets": assets,
                  "outputs": {"dir": str(out), "log": str(stage.log_path), "logSha256": sha(stage.log_path.read_bytes())}}
        if not self.materialize(result):
            raise ReleaseError("Node assets could not be placed for publication")
        return result

    def materialize(self, result) -> bool:
        """Verified build evidence -> exactly five run assets, restored after a candidate change."""
        try:
            assets = result["assets"]
            if list(assets) != list(ASSETS):
                return False
            target = self.directory / "assets"
            if target.is_dir() and {p.name for p in target.iterdir()} == set(assets) and asset_metadata(target, assets):
                return True
            origin = Path(result["outputs"]["dir"])
            if not asset_metadata(origin, assets) or {p.name for p in origin.iterdir()} != set(assets):
                return False
            if target.exists() or target.is_symlink():
                archive = self.directory / "superseded" / f"assets-{time.time_ns()}"
                archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), archive)
            pending = self.directory / f".assets-{time.time_ns()}"
            shutil.copytree(origin, pending)
            pending.replace(target)
            return asset_metadata(target, assets)
        except (OSError, KeyError, TypeError):
            return False

    def finish_cleanup(self):
        super().finish_cleanup()
        if self.value.get("completed"):
            # The published binaries stay in assets/; drop the build attempts' copies.
            for attempt in (self.directory / "results/node-build/attempts").glob("*"):
                path = attempt / "assets"
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)

    def report(self):
        value = self.value
        stages = value.get("stages", {})
        now = time.time()
        result = {"kind": "node", "runId": value["runId"], "version": value["version"], "tag": self.tag,
                  "completed": bool(value.get("completed")),
                  "candidate": {k: v for k, v in value.get("candidate", {}).items() if k != "files"},
                  "stages": [{"stage": name, "status": item["status"], "seconds": round(item.get("finishedAt", now) - item["startedAt"], 3)}
                             for name, item in stages.items()],
                  "wallSeconds": round(value.get("completedAt", now) - value["createdAt"], 3),
                  "lastError": value.get("lastError"), "publication": stages.get("publication", {}).get("result"),
                  "cleanup": value.get("cleanup"), "development": value.get("development")}
        atomic_json(self.directory / "report.json", result)
        print(json.dumps({"runId": value["runId"], "completed": result["completed"], "report": str(self.directory / "report.json"),
                          "url": (result.get("publication") or {}).get("url"), "lastError": result["lastError"]}, ensure_ascii=False), flush=True)

    def run(self, *, approved=False):
        if self.value.get("completed") and (self.value.get("development") or not self.value.get("startsDevelopment")):
            self.report()
            return
        if self.value.get("published"):
            self.finish_published()
            return
        invocation = {"startedAt": time.time(), "pid": os.getpid(), "startTicks": process_ticks(os.getpid())}
        self.value.setdefault("invocations", []).append(invocation)
        self.state.save()
        try:
            self.preflight()
            candidate.freeze(self.repo, self.directory, self.state, self.command, VERSIONING)
            identity = self.value["candidate"]
            self.state.execute("source", identity["sourceFingerprint"],
                lambda: candidate.export(self.repo, self.directory, self.state, self.command, label="node " + self.value["version"]),
                valid=lambda _: candidate.source_matches(self.directory / "source", identity))
            self.check_source()
            node = fingerprint(self.node_files())
            deps = self.state.execute("node-deps", {"key": self.deps_key()}, self.deps, valid=self.deps_valid)
            self.state.execute("node-test", {"node": node, "image": self.value["goImage"], "deps": deps["key"], "recipe": sha(TEST_SCRIPT.encode())},
                               lambda: self.test(deps))
            built = self.state.execute("node-build", {"node": node, "image": self.value["goImage"], "deps": deps["key"],
                                                      "recipe": sha(build_script(self.value["version"]).encode())},
                                       lambda: self.build_binaries(deps), valid=self.materialize)
            if list(built["assets"]) != list(ASSETS):
                raise ReleaseError("Node release must publish exactly: " + ", ".join(ASSETS))
            candidate.assert_local(self.repo, self.value, self.command)
            self.state.execute("publication", {"candidate": identity, "notes": self.value["notes"], "assets": built["assets"], "tag": self.tag},
                               lambda: publish(self.directory, self.state, self.command, self.github, tag=self.tag,
                                               name="OpenBear Node " + self.value["version"], assets=built["assets"], latest=False,
                                               prerelease=versions.prerelease(self.value["version"])))
            self.value.pop("lastError", None)
            self.value["published"] = True
        except BaseException as exc:
            self.value["lastError"] = {"type": type(exc).__name__, "message": self.command.redact(str(exc)),
                "exitCode": getattr(exc, "code", 130 if isinstance(exc, KeyboardInterrupt) else 1),
                "resume": f"{self.value['python']} {self.resume_command} {self.value['runId']}"}
            raise
        finally:
            self.command.cancel_all()
            try:
                self.finish_cleanup()
            except Exception as exc:
                self.value["cleanup"] = {"ok": False, "error": str(exc)}
                self.value["lastError"] = {"message": "Cleanup failed: " + str(exc)}
                raise
            finally:
                invocation["finishedAt"] = time.time()
                self.state.save()
                self.report()
        try:
            self.develop()
        finally:
            self.report()
