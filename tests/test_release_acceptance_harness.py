"""Directed, offline tests: no Docker, namespace operations or real installation."""
from __future__ import annotations

import copy
import importlib.util
import hashlib
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from unittest.mock import Mock

import pytest

from scripts.release_support import acceptance as harness
from scripts.release_support.acceptance_helpers import systemd_runtime as runtime

REAL_POPEN = subprocess.Popen


@pytest.fixture(autouse=True)
def forbid_external_commands(monkeypatch):
    def local_only(args, *positional, **kwargs):
        # Only local Python helper tests and certificate generation are allowed.
        assert args[0] in {sys.executable, "openssl"}, args
        return REAL_POPEN(args, *positional, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", local_only)


def assets(root, version, *, package_version=None):
    root.mkdir()
    package_version = package_version or version
    meta = {"schema": 1, "version": version, "requiresRestart": True}
    with zipfile.ZipFile(root / f"openbear-{version}.zip", "w") as archive:
        archive.writestr("app/__init__.py", f'__version__ = "{package_version}"\n')
        archive.writestr("pyproject.toml", f'[project]\nname = "openbear"\nversion = "{version}"\n')
        archive.writestr("release-meta.json", json.dumps(meta))
    (root / "release-meta.json").write_text(json.dumps(meta))
    (root / "install.sh").write_text("#!/bin/sh\nexit 0\n")
    (root / "SHA256SUMS").write_text(f"{harness.sha256(root / f'openbear-{version}.zip')}  openbear-{version}.zip\n")
    return metadata(root)


def metadata(root):
    return {p.name: {"sha256": harness.sha256(p), "bytes": p.stat().st_size} for p in root.iterdir()}


@pytest.fixture
def config(tmp_path):
    current, previous = tmp_path / "current", tmp_path / "previous"
    return {"schema": 1, "runId": "test-run_ASCII.1", "version": "2.4.0", "previousVersion": "2.3.0",
            "currentAssets": str(current), "previousAssets": str(previous),
            "assets": assets(current, "2.4.0"), "previousAssetsMetadata": assets(previous, "2.3.0"),
            "image": "sha256:" + "a" * 64, "outputDir": str(tmp_path / "output"), "cacheDir": str(tmp_path / "cache")}


def make_run(config, case="A"):
    return harness.AcceptanceRun(config, case, approved=True)


def read_report(run):
    return json.loads((run.case_dir / "report.json").read_text())


class FakeDocker:
    """Models actual objects separately from process success, with exact labels/IDs."""
    def __init__(self, run):
        self.run = run
        self.calls = []
        self.objects = {("container", "unrelated"): {"Id": "unrelated-id", "Config": {"Labels": {"keep": "yes"}}}}
        self.network_failure = None
        self.start_failure = None
        self.remove_fails = False
        self.inspect_unknown = False

    def object(self, kind, name):
        return self.objects.get((kind, name))

    def create(self, kind, name, labels):
        disk_ledger = json.loads((self.run.dir / "resource-ledger.json").read_text())
        assert any(item["name"] == name and item["intentAt"] for item in disk_ledger[kind + "s"])
        assert labels == self.run.labels
        assert labels["openbear.release.run"] == self.run.config["runId"]
        info = {"Id": name + "-id", "State": {"Running": True}, "Labels": labels.copy(), "Config": {"Labels": labels.copy()}}
        self.objects[kind, name] = info
        return info

    def command(self, args, label, *, timeout=300, check=True):
        self.calls.append(list(args))
        output, error, rc = "", "", 0
        assert args[0] == "docker", args
        if args[1:3] == ["image", "inspect"]:
            output = json.dumps([{"Id": self.run.config["image"]}])
        elif args[1:3] == ["network", "create"]:
            labels = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--label")
            self.create("network", args[-1], labels)
            if self.network_failure:
                raise self.network_failure
        elif len(args) > 2 and args[2] == "inspect":
            kind, name = args[1], args[3]
            if self.inspect_unknown:
                rc, error = 1, "Cannot connect to Docker daemon"
            elif self.object(kind, name):
                output = json.dumps([self.object(kind, name)])
            else:
                rc, error = 1, f"Error: No such {kind}: {name}"
        elif args[1] in {"logs", "stop", "exec"}:
            pass
        elif args[1] == "rm" or args[1:3] == ["network", "rm"]:
            assert not any(arg in args for arg in ("prune", "--all"))
            if self.remove_fails:
                rc, error = 1, "busy"
            else:
                key = next(key for key, value in self.objects.items() if value["Id"] == args[-1])
                del self.objects[key]
        else:
            pytest.fail(f"unexpected fake Docker command: {args}")
        if check and rc:
            raise harness.CommandFailure(error)
        return subprocess.CompletedProcess(args, rc, output, error)

    def start(self, name, *, image, network, mounts, labels, command, approved):
        assert approved is True
        assert image == self.run.config["image"]
        assert network == self.run.network
        assert name.startswith(self.run.prefix)
        self.create("container", name, labels)
        if self.start_failure:
            raise self.start_failure
        return {"image": image, "hostMountUnchanged": True, "pid1": "systemd"}


def attach_fake(monkeypatch, run):
    fake = FakeDocker(run)
    monkeypatch.setattr(run, "command", fake.command)
    monkeypatch.setattr(runtime, "start", fake.start)

    def certificates():
        run.ledger["temporaryDirectories"].append(str(run.certs))
        run.save_ledger()
        run.certs.mkdir()
        (run.certs / "server.key").write_text("test private key")
    monkeypatch.setattr(run, "generate_certificates", certificates)
    return fake


def minimal_case(run):
    def body():
        run.start_container(run.case.lower())
        return {"observedCase": run.case, "probe": {"testDouble": True}}
    return body


def test_no_approval_has_no_resources_or_config_reads(config, monkeypatch):
    before = sorted(pathlib.Path(config["outputDir"]).parent.iterdir())
    popen = Mock(side_effect=AssertionError("no subprocess allowed"))
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(SystemExit) as exc:
        harness.main(["--config", "/nonexistent-config", "--case", "A"])
    assert exc.value.code == 2
    with pytest.raises(PermissionError):
        harness.AcceptanceRun(config, "A")
    callback = Mock()
    with pytest.raises(PermissionError):
        runtime.start("anything", image=config["image"], network="none", mounts=[], labels={}, command=callback)
    popen.assert_not_called()
    callback.assert_not_called()
    assert sorted(pathlib.Path(config["outputDir"]).parent.iterdir()) == before


@pytest.mark.parametrize("field,value", [
    ("schema", 2), ("schema", True), ("version", "v2.4.0"), ("version", "2.4.0;touch evil"),
    ("previousVersion", "2.4.0"), ("previousVersion", "3.0.0"), ("runId", "中文"), ("runId", "x/y"),
    ("image", "a-mutable-tag:latest"), ("outputDir", "/"), ("cacheDir", "relative"), ("currentAssets", "/bad,bind"),
])
def test_config_validation_rejects_before_resources(config, field, value):
    config[field] = value
    with pytest.raises(ValueError):
        make_run(config)


@pytest.mark.parametrize("case", ["C", "a", "", None])
def test_bad_case_rejected(config, case):
    with pytest.raises(ValueError, match="case"):
        make_run(config, case)


def test_input_writable_aliases_and_symlinks_rejected(config, tmp_path):
    config["cacheDir"] = config["currentAssets"]
    with pytest.raises(ValueError, match="overlapping"):
        make_run(config)
    config["cacheDir"] = str(tmp_path / "cache")
    link = tmp_path / "link"
    link.symlink_to(config["currentAssets"], target_is_directory=True)
    config["currentAssets"] = str(link)
    with pytest.raises(ValueError, match="canonical"):
        make_run(config)


def test_asset_identity_and_package_versions(config):
    identity = harness.verify_assets(harness.validate_config(config, "B"))
    assert identity["lockedZipSha256"] == config["assets"]["openbear-2.4.0.zip"]["sha256"]
    root = pathlib.Path(config["currentAssets"])
    with zipfile.ZipFile(root / "openbear-2.4.0.zip", "w") as archive:
        archive.writestr("app/__init__.py", '__version__ = "9.0.0"\n')
    # Updating all external identities cannot make the wrong packaged version valid.
    (root / "SHA256SUMS").write_text(f"{harness.sha256(root / 'openbear-2.4.0.zip')}  openbear-2.4.0.zip\n")
    config["assets"] = metadata(root)
    with pytest.raises(ValueError, match="ZIP app version mismatch"):
        harness.verify_assets(config)


@pytest.mark.parametrize("part", ["sha", "size", "missing", "sums", "metadata"])
def test_asset_mismatch_reports_failure_before_docker(config, monkeypatch, part):
    root = pathlib.Path(config["currentAssets"])
    if part == "sha":
        config["assets"]["install.sh"]["sha256"] = "0" * 64
    elif part == "size":
        config["assets"]["install.sh"]["bytes"] += 1
    elif part == "missing":
        (root / "install.sh").unlink()
    else:
        (root / ("SHA256SUMS" if part == "sums" else "release-meta.json")).write_text("wrong" if part == "sums" else '{"version":"9.0.0"}')
        config["assets"] = metadata(root)
    run = make_run(config)
    command = Mock(side_effect=AssertionError("no commands before asset identity"))
    monkeypatch.setattr(run, "command", command)
    assert run.execute() == 1
    command.assert_not_called()
    report = read_report(run)
    assert report["ok"] is False and report["cleanup"]["ok"] is True
    assert report["cleanup"]["ownedResourcesRemaining"] == []
    assert (run.dir / "failure.json").exists()


def test_structural_failure_report_when_output_is_safe(config, tmp_path):
    config["schema"] = 8
    source = tmp_path / "config.json"
    source.write_text(json.dumps(config))
    assert harness.main(["--config", str(source), "--case", "B", "--approve-container-cgroup"]) == 1
    report = json.loads((pathlib.Path(config["outputDir"]) / "case-b/report.json").read_text())
    assert not report["ok"] and report["cleanup"]["ok"]


@pytest.mark.parametrize("case", ["A", "B"])
def test_cases_are_independent_and_cleanup_before_success(config, monkeypatch, case):
    run = make_run(config, case)
    fake = attach_fake(monkeypatch, run)
    own = "run_a" if case == "A" else "run_b"
    other = "run_b" if case == "A" else "run_a"
    monkeypatch.setattr(run, own, minimal_case(run))
    monkeypatch.setattr(run, other, Mock(side_effect=AssertionError("other case ran")))
    assert run.execute() == 0
    assert list(fake.objects) == [("container", "unrelated")]
    assert not run.certs.exists()
    report = read_report(run)
    assert report["ok"] and report["case"] == case
    assert report["cleanup"]["ok"] and not report["cleanup"]["ownedResourcesRemaining"]
    assert report["result"]["observedCase"] == case
    assert not (pathlib.Path(config["outputDir"]) / ("case-b" if case == "A" else "case-a")).exists()
    ledger = json.loads((run.dir / "resource-ledger.json").read_text())
    assert all(x["removed"] for group in ("containers", "networks") for x in ledger[group])
    assert "approved" not in json.dumps(ledger)


def test_b_retry_preserves_a_and_previous_attempt_evidence(config, monkeypatch):
    a_report = pathlib.Path(config["outputDir"]) / "case-a/report.json"
    a_report.parent.mkdir(parents=True)
    a_report.write_text('{"ok":true,"sentinel":"do not rerun"}')
    original = a_report.read_bytes()
    attempts = []
    for fail in (True, False):
        run = make_run(config, "B")
        fake = attach_fake(monkeypatch, run)
        fake.start_failure = RuntimeError("fixture startup failed") if fail else None
        monkeypatch.setattr(run, "run_b", minimal_case(run))
        monkeypatch.setattr(run, "run_a", Mock(side_effect=AssertionError("A reran")))
        assert run.execute() == int(fail)
        attempts.append(run)
        assert a_report.read_bytes() == original
        assert list(fake.objects) == [("container", "unrelated")]
    assert attempts[0].prefix != attempts[1].prefix
    assert (attempts[0].dir / "failure.json").is_file()
    assert read_report(attempts[1])["ok"]


@pytest.mark.parametrize("failure", [RuntimeError("unknown outcome"), subprocess.TimeoutExpired("docker", 1), KeyboardInterrupt()])
@pytest.mark.parametrize("point", ["network", "container"])
def test_ambiguous_creation_is_inspected_and_cleaned(config, monkeypatch, failure, point):
    run = make_run(config, "B")
    fake = attach_fake(monkeypatch, run)
    setattr(fake, "network_failure" if point == "network" else "start_failure", failure)
    monkeypatch.setattr(run, "run_b", minimal_case(run))
    assert run.execute() == 1
    assert list(fake.objects) == [("container", "unrelated")]
    assert read_report(run)["cleanup"]["ok"]
    assert not run.certs.exists()
    creates = [call for call in fake.calls if call[1:3] == ["network", "create"]]
    assert len(creates) == 1  # No blind replay of ambiguous creation.


@pytest.mark.parametrize("mode", ["remove-fails", "daemon-unknown", "foreign-label"])
def test_cleanup_failure_never_reports_success_or_removes_foreign_objects(config, monkeypatch, mode):
    run = make_run(config)
    fake = attach_fake(monkeypatch, run)

    def body():
        run.start_container("a")
        if mode == "remove-fails":
            fake.remove_fails = True
        elif mode == "daemon-unknown":
            fake.inspect_unknown = True
        else:
            fake.object("container", run.containers[0])["Config"]["Labels"] = {"openbear.release.run": "somebody-else"}
        return {"probe": "passed"}
    monkeypatch.setattr(run, "run_a", body)
    assert run.execute() == 1
    report = read_report(run)
    assert not report["ok"] and not report["cleanup"]["ok"]
    assert report["cleanup"]["ownedResourcesRemaining"]
    assert fake.object("container", "unrelated")
    if mode == "foreign-label":
        assert not any(call[1] in {"rm", "stop"} for call in fake.calls)


def test_docker_exec_always_has_sbin_and_rejects_foreign_container(config, monkeypatch):
    run = make_run(config)
    name = run.prefix + "-container"
    run.containers.append(name)
    command = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(run, "command", command)
    run.docker_exec(name, ["systemctl", "status"], "test", env={"PATH": "/bad"})
    args = command.call_args.args[0]
    assert f"PATH={runtime.EXEC_PATH}" in args
    assert "/usr/sbin" in runtime.EXEC_PATH and "/sbin" in runtime.EXEC_PATH
    with pytest.raises(ValueError, match="unowned"):
        run.docker_exec("other-container", ["true"], "test")


def test_command_timeout_keeps_raw_logs(config):
    run = make_run(config)
    with pytest.raises(subprocess.TimeoutExpired):
        run.command([sys.executable, "-c", "import time; print('before timeout', flush=True); time.sleep(30)"], "timeout", timeout=0.15)
    assert "before timeout" in next(run.evidence.glob("*.stdout.log")).read_text()
    record = json.loads(next(run.evidence.glob("*.json")).read_text())
    assert record["timedOut"] is True


def test_local_certificates_validate_hostname_and_are_deleted(config):
    if not shutil.which("openssl"):
        pytest.skip("local openssl is unavailable")
    run = make_run(config)
    run.generate_certificates()
    assert not (run.certs / "ca.key").exists()
    assert (run.certs / "server.key").stat().st_mode & 0o777 == 0o600
    result = run.command(["openssl", "verify", "-CAfile", str(run.certs / "ca.crt"), "-verify_hostname", "wrong.example",
                          str(run.certs / "server.crt")], "reject-other-host", check=False)
    assert result.returncode != 0
    assert run.cleanup()["ok"]
    assert not run.certs.exists()


def test_runtime_uses_fixed_image_and_only_private_remount(config, monkeypatch):
    run = make_run(config)
    name = run.prefix + "-container"
    calls = []
    cid = "c" * 64
    info = {"Image": config["image"], "Id": cid, "State": {"Pid": 123}, "Config": {"Labels": run.labels},
            "HostConfig": {"CgroupnsMode": "private", "Privileged": False, "PidMode": "", "NetworkMode": run.network,
                           "SecurityOpt": ["no-new-privileges"]}}

    def command(args, label, **kwargs):
        calls.append(args)
        if args[1:2] == ["inspect"]:
            output = json.dumps([info])
        elif args[0] == "findmnt":
            output = "ro,nosuid,nodev"
        elif args[-2:] == ["cat", "/proc/1/cgroup"]:
            output = "0::/"
        elif "findmnt" in args:
            output = "rw,nosuid,nodev"
        elif "is-system-running" in args:
            output = "running"
        else:
            output = ""
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(runtime.os, "readlink", lambda path: "host" if "/self/" in path else "container")
    original_read = pathlib.Path.read_text
    monkeypatch.setattr(pathlib.Path, "read_text", lambda path, *a, **kw: f"0::/docker/{cid}" if str(path) == "/proc/123/cgroup" else original_read(path, *a, **kw))
    result = runtime.start(name, image=config["image"], network=run.network,
                           mounts=[(config["currentAssets"], "/obaccept/assets/current", False)],
                           labels=run.labels, command=command, approved=True)
    assert result["hostMountUnchanged"]
    create = calls[0]
    assert config["image"] in create and "--pull=never" in create
    assert "--cgroupns=private" in create and "no-new-privileges" in create
    assert f"openbear.release.run={config['runId']}" in create
    assert "--privileged" not in create and "--pid=host" not in create and "--network=host" not in create
    assert not any("docker.sock" in arg or "unconfined" in arg for arg in create)
    remounts = [args for args in calls if "remount,rw" in args]
    assert remounts == [["nsenter", "--target", "123", "--mount", "--cgroup", "--root", "--wd", "--", "mount", "-o", "remount,rw", "/sys/fs/cgroup"]]
    for args in calls:
        if args[:2] == ["docker", "exec"]:
            assert f"PATH={runtime.EXEC_PATH}" in args


@pytest.mark.parametrize("bad", ["image", "privileged", "host-network", "namespace", "host-mount"])
def test_runtime_rejects_broken_isolation(config, monkeypatch, bad):
    run = make_run(config)
    cid = "c" * 64
    calls = []
    info = {"Image": config["image"], "Id": cid, "State": {"Pid": 123}, "Config": {"Labels": run.labels},
            "HostConfig": {"CgroupnsMode": "private", "Privileged": bad == "privileged",
                           "NetworkMode": "host" if bad == "host-network" else run.network}}
    if bad == "image":
        info["Image"] = "different"

    def command(args, label, **kwargs):
        calls.append(args)
        if args[1:2] == ["inspect"]:
            text = json.dumps([info])
        elif args[-2:] == ["cat", "/proc/1/cgroup"]:
            text = "0::/"
        elif args[0] == "findmnt":
            text = "rw" if label.endswith("after") else "ro"
        else:
            text = ""
        return subprocess.CompletedProcess(args, 0, text, "")

    monkeypatch.setattr(runtime.os, "readlink", lambda path: "host" if bad == "namespace" or "/self/" in path else "container")
    original_read = pathlib.Path.read_text
    monkeypatch.setattr(pathlib.Path, "read_text", lambda path, *a, **kw: f"0::/docker/{cid}" if str(path) == "/proc/123/cgroup" else original_read(path, *a, **kw))
    with pytest.raises(RuntimeError):
        runtime.start(run.prefix + "-container", image=config["image"], network=run.network,
                      mounts=[], labels=run.labels, command=command, approved=True)
    if bad != "host-mount":
        assert not any("nsenter" in args for args in calls)


@pytest.mark.parametrize("case,late_delete", [("A", False), ("B", False), ("B", True)])
@pytest.mark.parametrize("requires_restart", [True, False])
def test_real_case_orchestration_keeps_install_and_old_updater_paths(config, monkeypatch, case, requires_restart, late_delete):
    meta = pathlib.Path(config["currentAssets"]) / "release-meta.json"
    meta.write_text(json.dumps({"requiresRestart": requires_restart}))
    config["assets"] = metadata(meta.parent)
    run = make_run(config, case)
    state = run.dir / "state"
    state.mkdir()
    name = run.prefix + "-container"
    calls = []
    monkeypatch.setattr(run, "start_container", lambda chosen: (name, state, {"hostMountUnchanged": True}))
    monkeypatch.setattr(run, "prepare_container", lambda *args: None)
    def collect_runtime(*args):
        label = args[-2]
        pid = "300" if label == "b-second-boot" else "200"
        if args[-1] == config["previousVersion"] or (label == "b-post-upgrade" and not requires_restart):
            pid = "100"
        return {"installedVersion": args[-1], "configMode": "600",
                "serviceShow": f"MainPID={pid}\nExecMainStartTimestamp=started\n"}
    monkeypatch.setattr(run, "collect_runtime", collect_runtime)
    monkeypatch.setattr(run, "live_probe", lambda *args, **kw: {"version": args[-1], "saveSession": bool(kw.get("save_session")), "frontendBuild": {"buildId": "a" * 16}})

    def exec_command(container, args, label, **kwargs):
        assert container == name
        calls.append((args, label, kwargs))
        output = ""
        if label.endswith("install-" + config["version"]) or label.endswith("install-" + config["previousVersion"]):
            output = "+ uv sync --frozen --no-dev\n"
        if label == "b-seed-upgrade":
            harness.write_json(state / "upgrade-baseline.json", {"version": config["previousVersion"],
                "files": {"openbear.json": {"mode": 0o600}}, "tables": {"messages": []}, "conversationUuid": "seeded",
                "heavyHistory": {"frameCount": 120000, "latestFrames": [{}] * 24}})
            assert "--legacy-terminal-time" not in args
        if label == "b-updater-collected-state":
            output = "LoadState=not-found\n"
        if label == "b-update-result":
            output = json.dumps({"status": "success", "toVersion": config["version"], "requiresRestart": requires_restart})
        if label == "b-old-session-probe":
            harness.write_json(state / "old-session-after-upgrade.json", {"oldSessionSurvived": True})
        if label == "b-verify-preservation":
            harness.write_json(state / "upgrade-preservation.json", {"ok": True, "additionalChecks": {}})
        if label == "b-second-verify-preservation":
            harness.write_json(state / "second-preservation.json", {"ok": True, "additionalChecks": {}})
            assert args[-1] == ("restart" if requires_restart else "refresh")
            if late_delete:
                with (state / "startup-audit.jsonl").open("a") as handle:
                    handle.write(json.dumps({"pid": 300, "bootId": "300", "event": "frameDeleteAttempt"}) + "\n")
        if label in {"b-first-startup-history", "b-second-startup-history"}:
            harness.write_json(state / f"{label}.json", {"ok": True, "observedBoots": int(args[args.index("--boot-number") + 1])})
            pid = args[args.index("--pid") + 1]
            with (state / "startup-audit.jsonl").open("a") as handle:
                for event in ("observerInstalled", "startupEntered", "sqliteObserved", "startupReturned"):
                    handle.write(json.dumps({"pid": pid, "bootId": pid, "event": event,
                        "database": "/opt/openbear/data/openbear.db", "startupReady": True, "elapsedSeconds": 0.5}) + "\n")
        if label == "b-verify-removed":
            harness.write_json(state / "removed-files.json", {"ok": True, "checked": 0})
        if label.endswith("-package-extras"):
            assert args[2:4] == ["/opt/openbear", f"/obaccept/assets/{config['version']}/openbear-{config['version']}.zip"]
            harness.write_json(state / f"{label}.json", {"ok": True, "checked": ["Dockerfile"], "errors": []})
        if label.endswith("fixture-counts"):
            output = json.dumps({f"release_metadata_{v}": 1 for v in (config["version"], config["previousVersion"])} |
                                {f"asset_{v}_openbear-{v}.zip": 1 for v in (config["version"], config["previousVersion"])} |
                                {"model_request": 2, "telegram_getUpdates": 1})
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(run, "docker_exec", exec_command)
    if late_delete:
        with pytest.raises(ValueError, match="final startup observation failed"):
            run.run_b()
        assert not run.results["heavyHistoryGate"]["finalAudit"]["ok"]
        return
    result = run.run_a() if case == "A" else run.run_b()
    assert result["status"] == "PASS"
    installed = config["version"] if case == "A" else config["previousVersion"]
    installs = [args for args, label, kwargs in calls if args[:2] == ["bash", "-x"]]
    assert installs == [["bash", "-x", f"/obaccept/assets/{installed}/install.sh"]]
    assert result["packageExtras"]["ok"]
    labels = [label for args, label, kwargs in calls]
    if case == "A":
        assert labels.index(f"a-install-{config['version']}") < labels.index("a-package-extras")
    if case == "B":
        # The new code restores files the old updater left out on its first start.
        assert labels.index("b-first-startup-history") < labels.index("b-package-extras") < labels.index("b-second-restart")
        updater = next(args for args, label, kwargs in calls if label == "b-transient-updater")
        assert updater[0] == "systemd-run" and "--wait" in updater and "--collect" in updater
        assert "/opt/openbear/scripts/updater.py" in updater
        request = json.loads((state / "update-request.json").read_text())
        assert request["fromVersion"] == config["previousVersion"] and request["toVersion"] == config["version"]
        assert request["sha256"] == config["assets"][f"openbear-{config['version']}.zip"]["sha256"]
        assert request["zipPath"] == f"/obaccept/assets/{config['version']}/openbear-{config['version']}.zip"
        assert not request["allowDirty"]
        assert (state / "update-request.json").stat().st_mode & 0o777 == 0o600
        removed = next(args for args, label, kwargs in calls if label == "b-verify-removed")
        assert f"/obaccept/assets/{config['previousVersion']}/openbear-{config['previousVersion']}.zip" in removed
        labels = [label for args, label, kwargs in calls]
        assert labels.index("b-arm-startup-audit") < labels.index("b-transient-updater")
        assert labels.index("b-first-startup-history") < labels.index("b-second-restart") < labels.index("b-second-startup-history")
        assert ("b-first-boot-after-refresh" in labels) is not requires_restart
        assert [boot["observedBoots"] for boot in result["heavyHistoryGate"]["boots"]] == [1, 2]
        assert result["heavyHistoryGate"]["secondPreservation"]["ok"]
        assert result["heavyHistoryGate"]["finalAudit"]["ok"]


def test_missing_instance_and_gateway_files_fail_acceptance(config, monkeypatch):
    run = make_run(config)
    state = run.dir / "state"
    state.mkdir()

    def exec_command(container, args, label, **kwargs):
        harness.write_json(state / f"{label}.json", {"ok": False, "errors": ["missing Dockerfile"]})
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(run, "docker_exec", exec_command)
    with pytest.raises(AssertionError, match="missing Dockerfile"):
        run.verify_package_extras("c", state, "a", config["version"])


def test_package_extras_helper_compares_installed_tree_with_the_package(tmp_path, monkeypatch):
    from scripts.release_support.acceptance_helpers import verify_package_extras as helper
    archive = tmp_path / "openbear.zip"
    files = {"gateway/bin/openbear-gateway-linux-amd64": b"ELF", "Dockerfile": b"FROM x", "docs/instances.md": b"doc", "app/main.py": b""}
    with zipfile.ZipFile(archive, "w") as package:
        for name, data in files.items():
            package.writestr(name, data)
    root = tmp_path / "install"
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["verify_package_extras.py", str(root), str(archive), str(output)])

    assert helper.main() == 0
    assert json.loads(output.read_text())["checked"] == ["Dockerfile", "docs/instances.md", "gateway/bin/openbear-gateway-linux-amd64"]
    (root / "Dockerfile").write_bytes(b"changed")
    (root / "gateway/bin/openbear-gateway-linux-amd64").unlink()
    assert helper.main() == 1
    assert json.loads(output.read_text())["errors"] == ["missing gateway/bin/openbear-gateway-linux-amd64", "differs from package: Dockerfile"]


def test_installer_unfrozen_fallback_is_failure(config, monkeypatch):
    run = make_run(config)
    monkeypatch.setattr(run, "docker_exec", lambda *a, **kw: subprocess.CompletedProcess([], 0, "+ uv sync --frozen\n+ uv sync --no-dev\n", ""))
    with pytest.raises(AssertionError, match="non-frozen"):
        run.install_release("fake", run.dir, config["version"], "a")


def test_generic_preservation_works_without_legacy_schema(config, tmp_path):
    root = tmp_path / "installed"
    data = root / "data"
    (data / "backups").mkdir(parents=True)
    database = data / "openbear.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    for index in range(3):
        conn.execute("INSERT INTO messages VALUES (?,?)", (index, f"UPGRADE_{index}"))
    tables = {"messages": [dict(row) for row in conn.execute("SELECT * FROM messages")]}
    for name in ("sessions", "summaries", "web_conversations", "memory_templates", "web_conversation_folders", "memory_entries", "memory_docs"):
        conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, content TEXT, updated_at INTEGER)")
        conn.execute(f"INSERT INTO {name} VALUES (1,?,10)", (f"preserved {name}",))
        tables[name] = [dict(row) for row in conn.execute(f"SELECT * FROM {name}")]
    conn.commit()
    conn.close()
    shutil.copyfile(database, data / "backups" / "pre-upgrade.sqlite3")
    user_file = root / "openbear.json"
    user_file.write_text('{"testConfig":true}')
    user_file.chmod(0o600)
    baseline = {"root": str(root), "dbPath": str(database), "version": config["previousVersion"],
                "files": {"openbear.json": {"sha256": harness.sha256(user_file), "mode": 0o600}}, "tables": tables}
    before, output = tmp_path / "baseline.json", tmp_path / "preservation.json"
    before.write_text(json.dumps(baseline))
    digest = harness.sha256(database)
    proc = subprocess.run([sys.executable, str(harness.SCRIPTS / "verify_upgrade.py"), str(before), str(output)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(output.read_text())
    assert report["ok"] and report["additionalChecks"] == {}
    assert report["integrity"] == "ok" and report["backups"][0]["seededMessages"] == 3
    assert harness.sha256(database) == digest  # No schema patch/repair by verification.
    (data / "backups/pre-upgrade.sqlite3").unlink()
    refresh = subprocess.run([sys.executable, str(harness.SCRIPTS / "verify_upgrade.py"), str(before), str(output), "refresh"], capture_output=True, text=True)
    assert refresh.returncode == 0, refresh.stderr
    assert not json.loads(output.read_text())["backupRequired"]
    restarted = subprocess.run([sys.executable, str(harness.SCRIPTS / "verify_upgrade.py"), str(before), str(output), "restart"], capture_output=True, text=True)
    assert restarted.returncode == 1
    assert "no verified pre-upgrade backup" in output.read_text()
    user_file.chmod(0o644)
    proc = subprocess.run([sys.executable, str(harness.SCRIPTS / "verify_upgrade.py"), str(before), str(output)], capture_output=True, text=True)
    assert proc.returncode == 1
    assert not json.loads(output.read_text())["ok"]


def test_config_preservation_allows_only_the_feature_switches_a_release_adds(config, tmp_path):
    root = tmp_path / "installed"
    data = root / "data"
    (data / "backups").mkdir(parents=True)
    database = data / "openbear.db"
    conn = sqlite3.connect(database)
    tables = {}
    for name in ("messages", "sessions", "summaries", "web_conversations", "memory_templates", "web_conversation_folders", "memory_entries", "memory_docs"):
        conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, content TEXT)")
        tables[name] = []
    conn.commit()
    conn.close()
    user_file = root / "openbear.json"
    old = {"testConfig": True, "web": {"port": 8080}}
    user_file.write_text(json.dumps(old))
    user_file.chmod(0o600)
    digest = hashlib.sha256(json.dumps(old, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    baseline = {"root": str(root), "dbPath": str(database), "version": config["previousVersion"], "tables": tables,
                "files": {"openbear.json": {"sha256": harness.sha256(user_file), "mode": 0o600, "config": {"sha256": digest, "features": None}}}}
    before, output = tmp_path / "baseline.json", tmp_path / "preservation.json"
    before.write_text(json.dumps(baseline))

    def verify(body, mode=0o600):
        user_file.write_text(json.dumps(body, indent=2))
        user_file.chmod(mode)
        proc = subprocess.run([sys.executable, str(harness.SCRIPTS / "verify_upgrade.py"), str(before), str(output), "refresh"], capture_output=True, text=True)
        return proc.returncode, json.loads(output.read_text())

    code, report = verify({"web": {"port": 8080}, "testConfig": True, "features": {"instances": False, "servers": True}})
    assert code == 0 and report["configMigrated"] == ["openbear.json"], report
    for body, mode in (({"testConfig": True, "web": {"port": 9090}, "features": {"servers": True}}, 0o600),
                       ({"testConfig": True, "web": {"port": 8080}, "features": {"servers": "yes"}}, 0o600),
                       ({"testConfig": True, "web": {"port": 8080}, "features": {"servers": True, "other": True}}, 0o600),
                       ({"testConfig": True, "web": {"port": 8080}, "features": {"servers": True}}, 0o644),
                       ({"testConfig": True, "web": {"port": 8080}, "added": 1}, 0o600)):
        code, report = verify(body, mode)
        assert code == 1 and report["errors"] == [{"file": "openbear.json", "error": "changed bytes or permissions"}], (body, report)


def test_removed_file_probe_uses_actual_archive_difference(tmp_path):
    old, new = tmp_path / "old.zip", tmp_path / "new.zip"
    root, output = tmp_path / "install", tmp_path / "removed.json"
    root.mkdir()
    with zipfile.ZipFile(old, "w") as archive:
        for name in ("app/keep.py", "scripts/obsolete.py", "workspace/user.txt", "app/__pycache__/old.pyc"):
            archive.writestr(name, "old")
    with zipfile.ZipFile(new, "w") as archive:
        archive.writestr("app/keep.py", "new")
    command = [sys.executable, str(harness.SCRIPTS / "verify_removed.py"), "--root", str(root), "--old", str(old), "--new", str(new), "--output", str(output)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert json.loads(output.read_text())["managedOldOnly"] == ["scripts/obsolete.py"]
    (root / "scripts").mkdir()
    (root / "scripts/obsolete.py").write_text("not removed")
    assert subprocess.run(command, capture_output=True).returncode == 1


def test_helpers_have_no_frozen_versions_host_paths_or_insecure_tls():
    texts = [harness.SCRIPTS.parent.joinpath("acceptance.py").read_text()]
    texts += [path.read_text() for path in harness.SCRIPTS.glob("*.py")]
    for text in texts:
        assert str(pathlib.Path(__file__).resolve().parents[1]) not in text
        assert "0.9.2" not in text and "0.9.3" not in text
        assert "ob093" not in text and "--insecure" not in text
        assert '"-k"' not in text
    env = harness.AcceptanceRun.install_env("arbitrary")
    assert env["OPENBEAR_MODEL_BASE_URL"].startswith("http://127.0.0.1:")
    assert env["OPENBEAR_MODEL_API_KEY"] == "acceptance-local-only"
    assert "127.0.0.1" in (harness.SCRIPTS / "sitecustomize.py").read_text()


def test_case_output_symlink_cannot_escape_boundary(config, tmp_path):
    outside = tmp_path / "unowned"
    outside.mkdir()
    output = pathlib.Path(config["outputDir"])
    output.mkdir()
    (output / "case-a").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        make_run(config)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("broken", [None, [], {"openbear-2.4.0.zip": None}])
def test_malformed_asset_metadata_still_gets_failure_report(config, tmp_path, broken):
    config["assets"] = broken
    source = tmp_path / "bad-config.json"
    source.write_text(json.dumps(config))
    assert harness.main(["--config", str(source), "--case", "A", "--approve-container-cgroup"]) == 1
    report = json.loads((pathlib.Path(config["outputDir"]) / "case-a/report.json").read_text())
    assert report["ok"] is False
    assert report["cleanup"] == {"ok": True, "errors": [], "ownedResourcesRemaining": []}


def test_certificate_failure_cleans_partial_secrets_without_docker_creation(config, monkeypatch):
    run = make_run(config)
    calls = []

    def command(args, label, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([{"Id": config["image"]}]), "")
        assert args[0] == "openssl"
        (run.certs / "ca.key").write_text("partial test key")
        raise harness.CommandFailure("certificate generation failed")

    monkeypatch.setattr(run, "command", command)
    assert run.execute() == 1
    assert not run.certs.exists()
    assert read_report(run)["cleanup"]["ok"]
    assert not any("create" in args or "run" in args for args in calls)


@pytest.mark.parametrize("failure", ["delete", "missing-report", "nonzero-exit"])
def test_history_gate_failure_is_retained_in_existing_case_report(config, monkeypatch, failure):
    run = make_run(config, "B")
    attach_fake(monkeypatch, run)

    def exec_probe(name, args, label, **kwargs):
        if failure != "missing-report":
            harness.write_json(run.dir / "state" / f"{label}.json", {
                "ok": failure == "nonzero-exit", "errors": ["startup DELETE observed"] if failure == "delete" else [],
                "startup": {"frameDeleteAttempts": 1 if failure == "delete" else 0},
            })
        return subprocess.CompletedProcess(args, 1, "", "")

    def case():
        name, state, _ = run.start_container("b")
        run.results["heavyHistoryGate"] = {"boots": []}
        run.verify_history_boot(name, state, "b-first-startup-history", {
            "serviceShow": "MainPID=123\nExecMainStartTimestamp=started\n",
        }, 1)
        pytest.fail("failed history gate must not produce PASS")

    monkeypatch.setattr(run, "docker_exec", exec_probe)
    monkeypatch.setattr(run, "run_b", case)
    assert run.execute() == 1
    report = read_report(run)
    assert not report["ok"] and report["cleanup"]["ok"]
    assert report["failure"]["stage"] == "b-first-startup-history"
    boot = report["result"]["heavyHistoryGate"]["boots"][0]
    if failure == "delete":
        assert boot["startup"]["frameDeleteAttempts"] == 1
    elif failure == "missing-report":
        assert "missing startup probe report" in boot["errors"]
