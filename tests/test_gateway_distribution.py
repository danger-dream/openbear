"""Gateway build and release packaging; installation is the controller's job."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("gateway_builder", ROOT / "scripts/build_gateway.py")


def test_gateway_build_cross_arch_manifest_and_flags(tmp_path, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "out"
    source.mkdir()
    (source / "go.mod").write_text("module fixture\n\ngo 1.24.0\n")
    calls = []
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *a, **kw: "go version go1.24.4 linux/amd64\n")
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "build" in args:
            Path(args[args.index("-o") + 1]).write_bytes(b"\x7fELF" + kwargs["env"]["GOARCH"].encode())
    monkeypatch.setattr(builder.subprocess, "run", run)
    manifest = builder.build(source, output)
    assert calls[0][0] == ["go", "test", "-race", "./..."]
    assert len(manifest["binaries"]) == 2
    for command, options in calls[1:]:
        assert options["env"]["CGO_ENABLED"] == "0"
        assert options["env"]["GOTOOLCHAIN"] == "local"
        assert "-trimpath" in command and "-buildvcs=false" in command
        assert command[-1] == "./cmd/openbear-gateway"
    assert json.loads((output / "manifest.json").read_text()) == manifest
    assert not list(output.glob("*.pending"))


def test_release_source_and_binary_allowlists_are_separate(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from release_support import build, candidate
        assert candidate.valid_path("gateway/cmd/openbear-gateway/main.go")
        assert candidate.valid_path("gateway/go.mod")
        assert candidate.valid_path("gateway/README.md")
        assert candidate.valid_path("gateway/login.html")
        assert not candidate.valid_path("gateway/private.html")
        for name in ("gateway/gateway.json", "gateway/state/state.json", "gateway/bin/openbear-gateway-linux-amd64"):
            assert not candidate.valid_path(name)
        package = tmp_path / "package"
        (package / "gateway/bin").mkdir(parents=True)
        for name in ("openbear-gateway-linux-amd64", "openbear-gateway-linux-arm64", "manifest.json"):
            (package / "gateway/bin" / name).write_text("fixture")
        assert len(build.audit_package(package)) == 3
        for name in build.OPTIONAL_FILES:
            (package / name).parent.mkdir(parents=True, exist_ok=True)
            (package / name).write_text("fixture")
        assert len(build.audit_package(package)) == 6
        (package / "gateway/gateway.json").write_text("private")
        with pytest.raises(build.ReleaseError):
            build.audit_package(package)
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def test_updater_ships_gateway_artifacts_without_restarting_gateway():
    updater = load("gateway_update_policy", ROOT / "scripts/updater.py")
    assert "gateway/bin" in updater.REPLACE_DIRS
    assert "benchmark-package" in updater.REPLACE_DIRS
    assert "gateway/README.md" in updater.REPLACE_FILES
    assert updater.classify_path("gateway/bin/openbear-gateway-linux-amd64") == "noop"
    assert "gateway/state" not in updater.REPLACE_DIRS
    for name in ("Dockerfile", ".dockerignore", "docs/instances.md"):
        assert name in updater.REPLACE_FILES
        assert updater.classify_path(name) == "noop"
