#!/usr/bin/env python3
"""Build openbear-node from this checkout into node/bin (no service is installed).

Each run of a development version (node/VERSION X.Y.Z-dev.N) first raises N by one;
beta, rc and release versions are built as they are.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# GOOS-GOARCH pairs the controller ships; Windows builds end in .exe.
TARGETS = ("linux-amd64", "linux-arm64", "darwin-arm64", "windows-amd64")


def _load_versioning():
    # By path, so this script runs without the app package or its environment.
    spec = importlib.util.spec_from_file_location("openbear_versioning", ROOT / "app" / "versioning.py")
    module = importlib.util.module_from_spec(spec)
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # leave no app/__pycache__ behind
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = keep
    return module


versioning = _load_versioning()


def version(source: Path = ROOT / "node"):
    """The node's own version, released apart from the controller."""
    value = (source / "VERSION").read_text().strip()
    if not versioning.valid(value) or value.startswith("v"):
        raise ValueError("node/VERSION must be X.Y.Z, X.Y.Z-rc.N, X.Y.Z-beta.N or X.Y.Z-dev.N")
    return value


def bump_dev_version(source: Path = ROOT / "node"):
    """Raise a development version's number by one (one per build); other versions stay."""
    current = version(source)
    following = versioning.bump_dev(current)
    if following != current:
        temporary = source / "VERSION.pending"
        temporary.write_text(following + "\n")
        os.replace(temporary, source / "VERSION")
    return following


def _protocol_go(source: Path) -> str:
    return (source / "protocol.go").read_text()


def protocol(source: Path = ROOT / "node"):
    """The controller-node protocol a build speaks; controllers pick builds by it."""
    match = re.search(r"^const ProtocolVersion = (\d+)$", _protocol_go(source), re.M)
    if not match:
        raise ValueError("node/protocol.go has no ProtocolVersion")
    return int(match.group(1))


def protocol_min(source: Path = ROOT / "node"):
    """The oldest protocol a build still speaks (const ProtocolMin); only ProtocolVersion when it has none."""
    newest = protocol(source)
    match = re.search(r"^const ProtocolMin = (\d+)$", _protocol_go(source), re.M)
    oldest = int(match.group(1)) if match else newest
    if not 1 <= oldest <= newest:
        raise ValueError("node/protocol.go ProtocolMin must be between 1 and ProtocolVersion")
    return oldest


def features(source: Path = ROOT / "node"):
    """The methods a build has beyond protocol 1 (var Features); controllers skip builds lacking one they need."""
    match = re.search(r"^var Features = \[\]string\{([^}]*)\}", _protocol_go(source), re.M)
    if not match:
        raise ValueError("node/protocol.go has no Features")
    names = re.findall(r'"([^"\\]+)"', match.group(1))
    if len(names) != len(set(names)) or re.sub(r'"[^"\\]+"|[\s,]', "", match.group(1)):
        raise ValueError("node/protocol.go Features must be distinct string literals")
    return names


def build(source: Path, output: Path, *, go="go", targets=TARGETS, test=True):
    source, output = source.resolve(), output.resolve()
    if not (source / "go.mod").is_file():
        raise ValueError("node Go source is missing")
    output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=readonly"}
    if not env.get("HOME"):
        # Go keeps its module and build caches under HOME; some service shells run without one.
        import pwd
        env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    toolchain = subprocess.check_output([go, "version"], text=True, env=env).strip()
    if test:
        subprocess.run([go, "test", "-race", "./..."], cwd=source, env=env, check=True)
    release = version(source)
    binaries = {}
    for name in targets:
        if name not in TARGETS:
            raise ValueError("unsupported node target")
        goos, arch = name.split("-")
        target = output / (f"openbear-node-{name}" + (".exe" if goos == "windows" else ""))
        temporary = target.with_name(target.name + ".pending")
        try:
            subprocess.run([go, "build", "-trimpath", "-buildvcs=false", f"-ldflags=-s -w -X openbear/node.Version={release}",
                            "-o", str(temporary), "./cmd/openbear-node"], cwd=source,
                           env={**env, "GOOS": goos, "GOARCH": arch, "CGO_ENABLED": "0"}, check=True)
            temporary.chmod(0o755)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        binaries[target.name] = {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                                 "bytes": target.stat().st_size}
    manifest = {"schema": 1, "version": release, "protocol": protocol(source), "protocolMin": protocol_min(source),
                "features": features(source), "toolchain": toolchain, "binaries": binaries}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "node")
    parser.add_argument("--output", type=Path, default=ROOT / "node/bin")
    parser.add_argument("--go", default="go")
    parser.add_argument("--target", choices=TARGETS, action="append")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)
    bump_dev_version(args.source)
    print(json.dumps(build(args.source, args.output, go=args.go, targets=args.target or TARGETS,
                           test=not args.skip_tests), indent=2))


if __name__ == "__main__":
    main()
