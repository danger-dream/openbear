#!/usr/bin/env python3
"""Independent real-systemd release acceptance cases, using frozen caller assets.

Each invocation needs fresh --approve-container-cgroup authorization. No build,
image pull, release upload, credential discovery, or global Docker cleanup occurs.
Evidence is retained per attempt; the case report is written only after cleanup.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tomllib
import traceback
import uuid
import zipfile
from datetime import UTC, datetime

if __package__:
    from .acceptance_helpers import systemd_runtime as sd
    from .acceptance_helpers.startup_audit import verify_audit
else:
    from acceptance_helpers import systemd_runtime as sd
    from acceptance_helpers.startup_audit import verify_audit

SCRIPTS = pathlib.Path(__file__).resolve().parent / "acceptance_helpers"
ADMIN_ID = "123456789"
BOT_TOKEN = "900000001:ACCEPTANCE_LOCAL_ONLY_123456789_ABCDEFGHIJK"
MODEL_KEY = "acceptance-local-only"


def _load_versioning():
    # app/versioning.py by path: this harness runs from the checkout without importing app.
    spec = importlib.util.spec_from_file_location("openbear_acceptance_versioning", pathlib.Path(__file__).resolve().parents[2] / "app/versioning.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


versioning = _load_versioning()


def published_version(value) -> bool:
    """A beta, rc or release version as written in tags and asset names."""
    parsed = versioning.parse(value) if isinstance(value, str) else None
    return parsed is not None and str(parsed) == value and versioning.publishable(value)


class CommandFailure(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validated_paths(config: dict) -> dict[str, pathlib.Path]:
    require(isinstance(config, dict), "config must be an object")
    paths = {}
    for key in ("currentAssets", "previousAssets", "outputDir", "cacheDir"):
        raw = config.get(key)
        require(isinstance(raw, str) and raw and not any(c in raw for c in (",", "\n", "\r", "\0")), f"invalid {key}")
        path = pathlib.Path(raw)
        require(path.is_absolute() and path != pathlib.Path("/"), f"{key} must be an absolute non-root directory")
        require(path == path.resolve(), f"{key} must be canonical (no symlink or .. components)")
        require(not path.exists() or path.is_dir(), f"{key} is not a directory")
        paths[key] = path
    # Writable bind mounts and evidence must never alias the frozen input mounts.
    for writable in ("outputDir", "cacheDir"):
        for key, path in paths.items():
            if key == writable:
                continue
            target = paths[writable]
            require(not (target == path or target in path.parents or path in target.parents), f"overlapping {writable}/{key}")
        target = paths[writable]
        require(not (target == SCRIPTS or target in SCRIPTS.parents or SCRIPTS in target.parents), f"{writable} overlaps harness source")
    return paths


def validate_config(config: dict, case: str) -> dict:
    require(case in {"A", "B"}, "case must be A or B")
    require(isinstance(config, dict), "config must be an object")
    require(type(config.get("schema")) is int and config["schema"] == 1, "config schema must be 1")
    require(isinstance(config.get("runId"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", config["runId"]), "runId must be a safe ASCII ID")
    for key in ("version", "previousVersion"):
        require(published_version(config.get(key)), f"invalid {key}: expected X.Y.Z, X.Y.Z-rc.N or X.Y.Z-beta.N")
    require(versioning.compare(config["version"], config["previousVersion"]) > 0, "version must be newer than previousVersion")
    require(isinstance(config.get("image"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", config["image"]), "image must be a fixed Docker image ID")
    paths = validated_paths(config)
    for key in ("currentAssets", "previousAssets"):
        require(paths[key].is_dir(), f"missing {key}")
    for version, key in ((config["version"], "assets"), (config["previousVersion"], "previousAssetsMetadata")):
        metadata = config.get(key)
        names = {f"openbear-{version}.zip", "install.sh", "release-meta.json", "SHA256SUMS"}
        require(isinstance(metadata, dict) and set(metadata) == names, f"{key} must identify exactly the four release assets")
        for name, item in metadata.items():
            require(isinstance(item, dict) and isinstance(item.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", item["sha256"]), f"invalid SHA256 for {name}")
            require(type(item.get("bytes")) is int and item["bytes"] > 0, f"invalid byte length for {name}")
    return config


def verify_assets(config: dict) -> dict:
    report = {"verifiedAt": now(), "versions": {}}
    for version, root, metadata in (
        (config["version"], pathlib.Path(config["currentAssets"]), config["assets"]),
        (config["previousVersion"], pathlib.Path(config["previousAssets"]), config["previousAssetsMetadata"]),
    ):
        rows = {}
        for name, expected in metadata.items():
            path = root / name
            require(path.is_file() and not path.is_symlink(), f"missing/nonregular frozen asset: {path}")
            actual = {"sha256": sha256(path), "bytes": path.stat().st_size}
            require(actual == {k: expected[k] for k in actual}, f"asset identity mismatch: {path}")
            rows[name] = actual
        zip_name = f"openbear-{version}.zip"
        sums = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        require(f"{rows[zip_name]['sha256']}  {zip_name}" in sums, f"SHA256SUMS does not pin {zip_name}")
        meta = json.loads((root / "release-meta.json").read_text(encoding="utf-8"))
        require(meta.get("version") == version, f"release-meta version mismatch for {version}")
        with zipfile.ZipFile(root / zip_name) as archive:
            names = archive.namelist()
            require(len(names) == len(set(names)), "duplicate ZIP entries")
            for name in names:
                path = pathlib.PurePosixPath(name)
                require(not path.is_absolute() and ".." not in path.parts and "\\" not in name, "unsafe ZIP path")
            package = archive.read("app/__init__.py").decode("utf-8")
            match = re.search(r'^__version__\s*=\s*[\"\']([^\"\']+)[\"\']', package, re.M)
            require(match is not None and match[1] == version, "ZIP app version mismatch")
            project = tomllib.loads(archive.read("pyproject.toml").decode("utf-8"))
            require(project.get("project", {}).get("version") == version, "ZIP project version mismatch")
            packaged_meta = json.loads(archive.read("release-meta.json"))
            require(packaged_meta.get("version") == version, "ZIP release-meta version mismatch")
        report["versions"][version] = {"files": rows, "releaseMeta": meta}
    report["lockedZipSha256"] = config["assets"][f"openbear-{config['version']}.zip"]["sha256"]
    return report


def service_identity(runtime: dict) -> tuple[str, str]:
    fields = dict(line.split("=", 1) for line in runtime["serviceShow"].splitlines() if "=" in line)
    pid, started = fields.get("MainPID", ""), fields.get("ExecMainStartTimestamp", "")
    require(pid.isdigit() and int(pid) > 1 and started, "missing running service identity")
    return pid, started


class AcceptanceRun:
    def __init__(self, config: dict, case: str, *, approved: bool = False):
        if approved is not True:
            raise PermissionError("this invocation requires --approve-container-cgroup")
        self.config = validate_config(config, case)
        self.case = case
        self.version, self.previous_version = config["version"], config["previousVersion"]
        self.run_id = config["runId"]
        self.attempt = uuid.uuid4().hex
        digest = hashlib.sha256(self.run_id.encode("ascii")).hexdigest()[:12]
        self.prefix = f"obrel-{digest}-{case.lower()}-{self.attempt}"
        self.labels = {"openbear.release.run": self.run_id, "openbear.release.case": case, "openbear.release.attempt": self.attempt}
        self.case_dir = pathlib.Path(config["outputDir"]) / f"case-{case.lower()}"
        self.dir = self.case_dir / "attempts" / self.attempt
        self.evidence = self.dir / "evidence"
        require(self.evidence.resolve() == self.evidence, "case output contains a symlink")
        self.evidence.mkdir(parents=True, exist_ok=False)
        self.dir.chmod(0o700)
        self.certs = self.dir / "certs"
        self.network = self.prefix + "-network"
        self.containers: list[str] = []
        self.command_no = 0
        self.started = now()
        self.stage = "initializing"
        self.results = {}
        self.ledger = {"runId": self.run_id, "case": case, "attempt": self.attempt, "createdAt": self.started,
                       "containers": [], "networks": [], "temporaryDirectories": [], "preservedEvidenceDirectory": str(self.dir)}
        self.save_ledger()

    def save_ledger(self):
        write_json(self.dir / "resource-ledger.json", self.ledger)

    def command(self, args, label, *, timeout=300, check=True):
        """Stream raw output to disk even on interruption; never replay a command."""
        self.command_no += 1
        stem = f"{self.command_no:03d}-{re.sub(r'[^a-z0-9-]+', '-', label.lower()).strip('-')}"
        stdout_path, stderr_path = (self.evidence / f"{stem}.{kind}.log" for kind in ("stdout", "stderr"))
        record = {"startedAt": now(), "argv": args, "timeoutSeconds": timeout, "timedOut": False}
        proc = None
        try:
            with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
                proc = subprocess.Popen(args, stdout=out, stderr=err)
                try:
                    proc.wait(timeout=timeout)
                except BaseException:
                    proc.kill()
                    proc.wait()
                    raise
        except BaseException as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["timedOut"] = isinstance(exc, subprocess.TimeoutExpired)
            raise
        finally:
            record.update(finishedAt=now(), returncode=proc.returncode if proc else None,
                          stdout=str(stdout_path), stderr=str(stderr_path))
            write_json(self.evidence / f"{stem}.json", record)
        result = subprocess.CompletedProcess(args, proc.returncode, stdout_path.read_text(errors="replace"), stderr_path.read_text(errors="replace"))
        if check and result.returncode != 0:
            raise CommandFailure(f"{label} failed rc={result.returncode}; see {stem} logs")
        return result

    def docker_exec(self, name, command, label, *, env=None, timeout=300, check=True):
        require(name in self.containers and name.startswith(self.prefix + "-"), f"refusing exec in unowned container: {name}")
        args = ["docker", "exec"]
        execution_env = {**(env or {}), "PATH": sd.EXEC_PATH}
        for key, value in execution_env.items():
            args += ["--env", f"{key}={value}"]
        return self.command([*args, name, *command], label, timeout=timeout, check=check)

    def inspect(self, kind, name):
        proc = self.command(["docker", kind, "inspect", name], f"inspect-{kind}-{name}", timeout=60, check=False)
        if proc.returncode:
            text = (proc.stdout + proc.stderr).lower()
            if re.search(r"no such (?:object|container|network)(?::|\s)", text) or (kind == "network" and "network " + name.lower() + " not found" in text):
                return None
            raise CommandFailure(f"cannot establish {kind} state: {text.strip()}")
        info = json.loads(proc.stdout)
        require(isinstance(info, list) and len(info) == 1, f"ambiguous {kind} inspection")
        return info[0]

    def intent(self, kind, name):
        require(self.inspect(kind, name) is None, f"resource already exists: {name}")
        item = {"name": name, "labels": self.labels.copy(), "intentAt": now(), "removed": False}
        self.ledger[kind + "s"].append(item)
        self.save_ledger()  # Durable before any resource creation, including unknown outcomes.
        return item

    def create_network(self):
        item = self.intent("network", self.network)
        args = ["docker", "network", "create"]
        for key, value in self.labels.items():
            args += ["--label", f"{key}={value}"]
        self.command([*args, self.network], "create-network")
        item["createdAt"] = now()
        self.save_ledger()
        inspected = self.inspect("network", self.network)
        require(inspected is not None and all(inspected.get("Labels", {}).get(k) == v for k, v in self.labels.items()), "network ownership mismatch")
        write_json(self.evidence / "network-inspect.json", inspected)

    def generate_certificates(self):
        self.ledger["temporaryDirectories"].append(str(self.certs))
        self.save_ledger()
        self.certs.mkdir(mode=0o700)
        extensions = self.certs / "server.ext"
        extensions.write_text("subjectAltName=DNS:api.github.com\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        self.command(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-sha256",
                      "-subj", "/CN=OpenBear ephemeral acceptance CA", "-addext", "basicConstraints=critical,CA:TRUE",
                      "-keyout", str(self.certs / "ca.key"), "-out", str(self.certs / "ca.crt")], "certificate-ca")
        self.command(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", "/CN=api.github.com",
                      "-keyout", str(self.certs / "server.key"), "-out", str(self.certs / "server.csr")], "certificate-server-csr")
        self.command(["openssl", "x509", "-req", "-in", str(self.certs / "server.csr"), "-CA", str(self.certs / "ca.crt"),
                      "-CAkey", str(self.certs / "ca.key"), "-CAcreateserial", "-days", "2", "-sha256", "-extfile", str(extensions),
                      "-out", str(self.certs / "server.crt")], "certificate-sign")
        self.command(["openssl", "verify", "-CAfile", str(self.certs / "ca.crt"), "-verify_hostname", "api.github.com",
                      str(self.certs / "server.crt")], "certificate-verify")
        (self.certs / "ca.key").unlink()  # No CA signing key is ever exposed to a container.
        (self.certs / "server.key").chmod(0o600)

    def start_container(self, case):
        require(case == self.case.lower(), "cross-case container request")
        name = self.prefix + "-container"
        state = self.dir / "state"
        state.mkdir(mode=0o700)
        (state / "release-version").write_text((self.version if case == "a" else self.previous_version) + "\n")
        cache = pathlib.Path(self.config["cacheDir"])
        self.ledger["preservedCacheDirectory"] = str(cache)
        self.save_ledger()
        cache.mkdir(parents=True, exist_ok=True)
        mounts = [(str(SCRIPTS), "/obaccept/scripts", False), (str(self.certs), "/obaccept/certs", False),
                  (str(state), "/obaccept/state", True),
                  (self.config["previousAssets"], f"/obaccept/assets/{self.previous_version}", False),
                  (self.config["currentAssets"], f"/obaccept/assets/{self.version}", False),
                  (str(cache), "/root/.cache/uv", True)]
        item = self.intent("container", name)
        self.containers.append(name)
        runtime = sd.start(name, image=self.config["image"], network=self.network, mounts=mounts,
                           labels=self.labels, command=self.command, approved=True)
        item["createdAt"] = now()
        self.save_ledger()
        write_json(state / "systemd-runtime.json", runtime)
        return name, state, runtime

    def asset_sha(self, version):
        key = 'assets' if version == self.version else 'previousAssetsMetadata'
        return self.config[key][f'openbear-{version}.zip']['sha256']

    def prepare_container(self, name: str, state: pathlib.Path, case: str) -> None:
        setup = """
set -euo pipefail
grep -qE '^127\\.0\\.0\\.1[[:space:]]+api\\.github\\.com([[:space:]]|$)' /etc/hosts || printf '127.0.0.1 api.github.com\\n' >> /etc/hosts
install -m 0644 /obaccept/certs/ca.crt /usr/local/share/ca-certificates/obrel-acceptance.crt
update-ca-certificates
install -d -m 0755 /etc/systemd/system/openbear.service.d
printf '%s\\n' '[Service]' 'Environment=PYTHONPATH=/obaccept/scripts' 'Environment=OB_ACCEPTANCE_TG=http://127.0.0.1:19580' > /etc/systemd/system/openbear.service.d/acceptance.conf
systemctl daemon-reload
"""
        self.docker_exec(name, ["bash", "-c", setup], f"{case}-prepare-loopback")
        unit = f"{self.prefix}-fixture"
        self.docker_exec(name, [
            "systemd-run", f"--unit={unit}", "--property=Type=exec", "--property=Restart=on-failure",
            "/usr/bin/python3", "/obaccept/scripts/fixture_server.py", "--root", "/obaccept",
            "--cert", "/obaccept/certs/server.crt", "--key", "/obaccept/certs/server.key",
            "--versions", self.previous_version, self.version,
        ], f"{case}-start-fixture")
        wait = "for i in $(seq 1 100); do curl -fsS http://127.0.0.1:19580/counts >/dev/null && exit 0; sleep 0.1; done; systemctl status " + unit + " --no-pager; exit 1"
        self.docker_exec(name, ["bash", "-c", wait], f"{case}-wait-fixture", timeout=30)
        fixture_state = self.docker_exec(name, ["systemctl", "show", unit, "--no-pager"], f"{case}-fixture-state")
        (state / "fixture-unit.txt").write_text(fixture_state.stdout, encoding="utf-8")

    @staticmethod
    def install_env(version: str) -> dict[str, str]:
        return {
            "OPENBEAR_NONINTERACTIVE": "1",
            "OPENBEAR_SOURCE": "release",
            "OPENBEAR_MODE": "fresh",
            "OPENBEAR_DIR": "/opt/openbear",
            "OPENBEAR_BOT_TOKEN": BOT_TOKEN,
            "OPENBEAR_ADMIN_ID": ADMIN_ID,
            "OPENBEAR_DISPLAY_NAME": "Acceptance",
            "OPENBEAR_CHANNEL_NAME": "acceptance",
            "OPENBEAR_MODEL_BASE_URL": "http://127.0.0.1:19580/v1",
            "OPENBEAR_MODEL_API_KEY": MODEL_KEY,
            "OPENBEAR_MODEL_PROTOCOL": "chat",
            "OPENBEAR_MODEL_ID": "acceptance-model",
            "OPENBEAR_MODEL_NAME": "Acceptance model",
            "OPENBEAR_WEB_PORT": "18961",
            "OPENBEAR_SKIP_FIREWALL": "1",
            "NO_PROXY": "api.github.com,127.0.0.1,localhost",
            "no_proxy": "api.github.com,127.0.0.1,localhost",
        }

    def install_release(self, name: str, state: pathlib.Path, version: str, case: str) -> None:
        self.docker_exec(name, ["bash", "-c", "test ! -e /opt/openbear"], f"{case}-assert-clean-root")
        path = f"/obaccept/assets/{version}/install.sh"
        proc = self.docker_exec(name, ["bash", "-x", path], f"{case}-install-{version}", env=self.install_env(version), timeout=1800)
        trace = proc.stdout + "\n" + proc.stderr
        frozen = re.findall(r"^\+ uv sync .*--frozen.*$", trace, re.M)
        fallback = re.findall(r"^\+ uv sync (?!.*--frozen).*$", trace, re.M)
        if not frozen:
            raise AssertionError(f"{case}: installer did not execute frozen uv sync")
        if fallback:
            raise AssertionError(f"{case}: installer executed forbidden non-frozen fallback: {fallback}")
        venv = self.docker_exec(name, ["bash", "-c", "test -f /opt/openbear/.venv/pyvenv.cfg && test ! -L /opt/openbear/.venv && stat -c '%i %y %a' /opt/openbear/.venv/pyvenv.cfg"], f"{case}-new-venv")
        (state / "venv-identity.txt").write_text(venv.stdout, encoding="utf-8")

    def collect_runtime(self, name: str, state: pathlib.Path, label: str, version: str) -> dict:
        pid1 = self.docker_exec(name, ["cat", "/proc/1/comm"], f"{label}-pid1").stdout.strip()
        system_state = self.docker_exec(name, ["systemctl", "is-system-running", "--wait"], f"{label}-system-state").stdout.strip()
        active = self.docker_exec(name, ["systemctl", "is-active", "openbear.service"], f"{label}-service-active").stdout.strip()
        failed = self.docker_exec(name, ["systemctl", "--failed", "--no-legend", "--no-pager"], f"{label}-failed-units").stdout.strip()
        show = self.docker_exec(name, ["systemctl", "show", "openbear.service", "--no-pager", "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStartTimestamp,FragmentPath,DropInPaths"], f"{label}-service-show").stdout
        cmdline = self.docker_exec(name, ["bash", "-c", "p=$(systemctl show openbear.service -p MainPID --value); test \"$p\" -gt 1; tr '\\0' ' ' < /proc/$p/cmdline"], f"{label}-service-cmdline").stdout.strip()
        installed = self.docker_exec(name, ["bash", "-c", "cd /opt/openbear && .venv/bin/python -c 'from app import installed_version; print(installed_version())'"], f"{label}-installed-version").stdout.strip()
        mode = self.docker_exec(name, ["stat", "-c", "%a", "/opt/openbear/openbear.json"], f"{label}-config-mode").stdout.strip()
        journal = self.docker_exec(name, ["journalctl", "-u", "openbear.service", "-n", "300", "--no-pager"], f"{label}-journal", check=False).stdout
        if pid1 != "systemd" or system_state not in {"running", "degraded"}:
            raise AssertionError((pid1, system_state))
        if active != "active" or failed:
            raise AssertionError({"active": active, "failedUnits": failed})
        if "-m app.main" not in cmdline or installed != version or mode != "600":
            raise AssertionError({"cmdline": cmdline, "version": installed, "mode": mode})
        bad = [text for text in ("Task exception was never retrieved", "Traceback (most recent call last)") if text in journal]
        if bad:
            raise AssertionError(f"{label} service journal contains background errors: {bad}")
        result = {
            "checkedAt": now(), "pid1": pid1, "systemState": system_state, "serviceActive": active,
            "failedUnits": [], "serviceShow": show, "commandLine": cmdline,
            "installedVersion": installed, "configMode": mode, "journalBackgroundErrors": bad,
        }
        write_json(state / f"{label}-runtime.json", result)
        return result

    def live_probe(self, name: str, state: pathlib.Path, label: str, version: str, *, save_session: bool = False) -> dict:
        output = f"/obaccept/state/{label}-live-probe.json"
        command = [
            "/opt/openbear/.venv/bin/python", "/obaccept/scripts/live_service_probe.py",
            "--root", "/opt/openbear", "--port", "18961", "--output", output,
            "--version", version, "--archive", f"/obaccept/assets/{version}/openbear-{version}.zip",
        ]
        if save_session:
            command += ["--session-output", "/obaccept/state/pre-upgrade-session.txt"]
        self.docker_exec(name, command, f"{label}-live-probe", timeout=180)
        result = json.loads((state / f"{label}-live-probe.json").read_text(encoding="utf-8"))
        if result["archiveSha256"] != self.asset_sha(version):
            raise AssertionError("live probe archive identity mismatch")
        return result

    def verify_package_extras(self, name: str, state: pathlib.Path, label: str, version: str) -> dict:
        output = f"/obaccept/state/{label}-package-extras.json"
        proc = self.docker_exec(name, [
            "/opt/openbear/.venv/bin/python", "/obaccept/scripts/verify_package_extras.py",
            "/opt/openbear", f"/obaccept/assets/{version}/openbear-{version}.zip", output,
        ], f"{label}-package-extras", timeout=60, check=False)
        report = state / f"{label}-package-extras.json"
        result = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {"ok": False, "errors": ["missing report"]}
        if proc.returncode or not result.get("ok"):
            raise AssertionError(f"{label}: installed tree lacks the package's instance/gateway files: {result}")
        return result

    def verify_history_boot(self, name, state, label, service, boot_number):
        """A mandatory B gate; retain structured evidence even when the helper fails."""
        self.stage = label
        pid, _ = service_identity(service)
        proc = self.docker_exec(name, [
            "/opt/openbear/.venv/bin/python", "/obaccept/scripts/startup_probe.py",
            "--baseline", "/obaccept/state/upgrade-baseline.json",
            "--audit", "/obaccept/state/startup-audit.jsonl",
            "--output", f"/obaccept/state/{label}.json", "--pid", pid,
            "--boot-number", str(boot_number), "--version", self.version, "--port", "18961",
        ], label, timeout=90, check=False)
        output = state / f"{label}.json"
        result = json.loads(output.read_text()) if output.is_file() else {"ok": False, "errors": ["missing startup probe report"]}
        self.results["heavyHistoryGate"]["boots"].append(result)
        if proc.returncode or not result.get("ok"):
            raise AssertionError(f"{label}: {result}")
        return result

    def run_a(self) -> dict:
        self.stage = "A-clean-install"
        print("Starting A: real clean installer", flush=True)
        name, state, runtime = self.start_container("a")
        self.prepare_container(name, state, "a")
        self.install_release(name, state, self.version, "a")
        service = self.collect_runtime(name, state, "a-post-install", self.version)
        extras = self.verify_package_extras(name, state, "a", self.version)
        probe = self.live_probe(name, state, "a", self.version)
        counts = json.loads(self.docker_exec(name, ["curl", "-fsS", "http://127.0.0.1:19580/counts"], "a-fixture-counts").stdout)
        if counts.get(f"release_metadata_{self.version}", 0) < 1 or counts.get(f"asset_{self.version}_openbear-{self.version}.zip", 0) < 1:
            raise AssertionError(f"A installer did not fetch locked assets through real curl: {counts}")
        if counts.get("model_request", 0) < 2 or counts.get("telegram_getUpdates", 0) < 1:
            raise AssertionError(f"A real app did not use controlled model/Telegram fixture: {counts}")
        result = {"status": "PASS", "zipSha256": self.asset_sha(self.version), "runtime": runtime, "service": service, "probe": probe, "packageExtras": extras, "fixtureCounts": counts}
        write_json(state / "a-result.json", result)
        return result

    def run_b(self) -> dict:
        self.stage = f"B-upgrade-from-{self.previous_version}"
        print(f"Starting B independently: {self.previous_version} install, full seeds and real updater", flush=True)
        name, state, runtime = self.start_container("b")
        self.prepare_container(name, state, "b")
        self.install_release(name, state, self.previous_version, "b")
        old_service = self.collect_runtime(name, state, "b-old-install", self.previous_version)
        old_probe = self.live_probe(name, state, "b-old", self.previous_version, save_session=True)
        self.docker_exec(name, ["/opt/openbear/.venv/bin/python", "/obaccept/scripts/seed_upgrade.py", "/opt/openbear", "/obaccept/state/upgrade-baseline.json"], "b-seed-upgrade", timeout=120)
        baseline = json.loads((state / "upgrade-baseline.json").read_text(encoding="utf-8"))
        if baseline["version"] != self.previous_version or baseline["files"]["openbear.json"]["mode"] != 0o600:
            raise AssertionError("invalid previous-version seed/config baseline")

        history = baseline.get("heavyHistory", {})
        require(history.get("frameCount", 0) >= 100_000 and len(history.get("latestFrames", [])) == 24,
                "B requires >=100k historical frames and every operation's latest-frame/body sentinel; no compatibility skip")
        self.results["heavyHistoryGate"] = {
            "baseline": history, "boots": [], "auditFile": str(state / "startup-audit.jsonl"),
        }
        # Only future service processes are observed, not old installation/seeding or
        # the updater. The real previous updater and installed application stay intact.
        self.docker_exec(name, ["bash", "-c", "printf '%s\\n' '[Service]' "
            "'Environment=OB_ACCEPTANCE_STARTUP_AUDIT=/obaccept/state/startup-audit.jsonl' "
            "> /etc/systemd/system/openbear.service.d/history-audit.conf && systemctl daemon-reload"], "b-arm-startup-audit")

        request = {
            "schema": 1,
            "installRoot": "/opt/openbear",
            "dataDir": "/opt/openbear/data",
            "serviceName": "openbear.service",
            "fromVersion": self.previous_version,
            "toVersion": self.version,
            "healthUrl": "http://127.0.0.1:18961/health",
            "zipPath": f"/obaccept/assets/{self.version}/openbear-{self.version}.zip",
            "sha256": self.asset_sha(self.version),
            "allowDirty": False,
        }
        write_json(state / "update-request.json", request)
        (state / "update-request.json").chmod(0o600)
        unit = f"{self.prefix}-updater"
        update_cmd = [
            "systemd-run", "--wait", "--collect", "--pipe", f"--unit={unit}",
            "--property=Type=exec", "--property=WorkingDirectory=/opt/openbear",
            "--setenv=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.local/bin",
            "/opt/openbear/.venv/bin/python", "/opt/openbear/scripts/updater.py", "apply",
            "--request", "/obaccept/state/update-request.json",
        ]
        self.docker_exec(name, update_cmd, "b-transient-updater", timeout=1800)
        unit_state = self.docker_exec(name, ["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus", "--no-pager"], "b-updater-collected-state", check=False)
        journal = self.docker_exec(name, ["journalctl", "-u", unit, "--no-pager"], "b-updater-journal", check=False)
        (state / "updater-unit-after-collect.txt").write_text(unit_state.stdout + unit_state.stderr, encoding="utf-8")
        (state / "updater-journal.txt").write_text(journal.stdout + journal.stderr, encoding="utf-8")
        if unit_state.returncode == 0 and "LoadState=not-found" not in unit_state.stdout:
            raise AssertionError(f"transient updater unit was not collected: {unit_state.stdout}")

        update_result_raw = self.docker_exec(name, ["cat", "/opt/openbear/data/update-result.json"], "b-update-result").stdout
        update_result = json.loads(update_result_raw)
        if update_result.get("status") != "success" or update_result.get("toVersion") != self.version:
            raise AssertionError(update_result)
        expected_restart = json.loads((pathlib.Path(self.config["currentAssets"]) / "release-meta.json").read_text())["requiresRestart"]
        if update_result.get("requiresRestart") is not expected_restart:
            raise AssertionError("Old updater restart decision differs from accepted package classification")
        new_service = self.collect_runtime(name, state, "b-post-upgrade", self.version)
        old_identity = service_identity(old_service)
        new_identity = service_identity(new_service)
        if (old_identity != new_identity) is not expected_restart:
            raise AssertionError(f"Unexpected restart behavior: expected restart={expected_restart}, before={old_identity}, after={new_identity}")
        if expected_restart:
            first_boot = self.verify_history_boot(name, state, "b-first-startup-history", new_service, 1)
        self.docker_exec(name, [
            "/opt/openbear/.venv/bin/python", "/obaccept/scripts/old_session_probe.py", "--port", "18961",
            "--session-file", "/obaccept/state/pre-upgrade-session.txt", "--output", "/obaccept/state/old-session-after-upgrade.json",
        ], "b-old-session-probe", timeout=60)
        old_session = json.loads((state / "old-session-after-upgrade.json").read_text(encoding="utf-8"))
        new_probe = self.live_probe(name, state, "b-new", self.version)
        self.docker_exec(name, ["/opt/openbear/.venv/bin/python", "/obaccept/scripts/verify_upgrade.py", "/obaccept/state/upgrade-baseline.json", "/obaccept/state/upgrade-preservation.json", "restart" if expected_restart else "refresh"], "b-verify-preservation", timeout=120)
        preservation = json.loads((state / "upgrade-preservation.json").read_text(encoding="utf-8"))
        if not preservation.get("ok"):
            raise AssertionError(preservation)
        self.docker_exec(name, [
            "/opt/openbear/.venv/bin/python", "/obaccept/scripts/verify_removed.py", "--root", "/opt/openbear",
            "--old", f"/obaccept/assets/{self.previous_version}/openbear-{self.previous_version}.zip", "--new", f"/obaccept/assets/{self.version}/openbear-{self.version}.zip",
            "--output", "/obaccept/state/removed-files.json",
        ], "b-verify-removed", timeout=120)
        removed = json.loads((state / "removed-files.json").read_text(encoding="utf-8"))
        # Refresh-only packages must still boot their newly installed code once;
        # this does not change the updater's separately checked restart decision.
        first_service, first_probe = new_service, new_probe
        if not expected_restart:
            self.docker_exec(name, ["systemctl", "restart", "openbear.service"], "b-first-boot-after-refresh", timeout=90)
            first_service = self.collect_runtime(name, state, "b-first-boot", self.version)
            require(service_identity(first_service) != new_identity, "refresh boot did not replace the process")
            first_boot = self.verify_history_boot(name, state, "b-first-startup-history", first_service, 1)
            first_probe = self.live_probe(name, state, "b-first-boot", self.version)
        # The previous updater may not replace these; the new code restores them
        # from its retained package when the upgraded service first starts.
        extras = self.verify_package_extras(name, state, "b", self.version)
        self.stage = "B-second-startup"
        self.docker_exec(name, ["systemctl", "restart", "openbear.service"], "b-second-restart", timeout=90)
        second_service = self.collect_runtime(name, state, "b-second-boot", self.version)
        require(service_identity(second_service) != service_identity(first_service), "second boot did not replace the process")
        second_boot = self.verify_history_boot(name, state, "b-second-startup-history", second_service, 2)
        second_probe = self.live_probe(name, state, "b-second-boot", self.version)
        second_check = self.docker_exec(name, ["/opt/openbear/.venv/bin/python", "/obaccept/scripts/verify_upgrade.py",
            "/obaccept/state/upgrade-baseline.json", "/obaccept/state/second-preservation.json",
            "restart" if expected_restart else "refresh"], "b-second-verify-preservation", timeout=120, check=False)
        second_preservation = json.loads((state / "second-preservation.json").read_text(encoding="utf-8"))
        self.results["heavyHistoryGate"]["secondPreservation"] = second_preservation
        require(second_check.returncode == 0 and second_preservation.get("ok"), f"second-start preservation failed: {second_preservation}")
        # Close the observation window AFTER the real login/model/WebSocket and
        # preservation checks, catching delayed cleanup and automatic restarts too.
        rows = [json.loads(line) for line in (state / "startup-audit.jsonl").read_text().splitlines()]
        final_audit = verify_audit(rows, service_identity(second_service)[0])
        if sum(row.get("event") == "observerInstalled" for row in rows) != 2:
            final_audit["errors"].append("unexpected service restart during live probes")
        if any(row.get("event") in {"frameDeleteAttempt", "startupFailed"} for row in rows):
            final_audit["errors"].append("DELETE/startup failure before live probes completed")
        final_audit["ok"] = not final_audit["errors"]
        self.results["heavyHistoryGate"]["finalAudit"] = final_audit
        require(final_audit["ok"], f"final startup observation failed: {final_audit}")
        counts = json.loads(self.docker_exec(name, ["curl", "-fsS", "http://127.0.0.1:19580/counts"], "b-fixture-counts").stdout)
        if counts.get(f"release_metadata_{self.previous_version}", 0) < 1 or counts.get(f"asset_{self.previous_version}_openbear-{self.previous_version}.zip", 0) < 1:
            raise AssertionError(f"B old installer did not fetch formal previous-version assets through real curl: {counts}")
        result = {
            "status": "PASS", "fromZipSha256": self.asset_sha(self.previous_version),
            "toZipSha256": request["sha256"], "runtime": runtime, "oldService": old_service,
            "oldProbe": old_probe, "baseline": {"version": baseline["version"], "conversationUuid": baseline["conversationUuid"], "fileCount": len(baseline["files"]), "tableCounts": {k: len(v) for k, v in baseline["tables"].items()}},
            "updater": {"unit": unit, "collected": True, "result": update_result},
            "newService": new_service, "oldSession": old_session,
            "updateBehavior": {"requiresRestart": expected_restart, "processChanged": old_identity != new_identity,
                               "frontendBuildChanged": old_probe["frontendBuild"]["buildId"] != new_probe["frontendBuild"]["buildId"]},
            "newProbe": new_probe, "preservation": preservation, "removedFiles": removed, "packageExtras": extras,
            "heavyHistoryGate": self.results["heavyHistoryGate"],
            "firstStartup": {"service": first_service, "probe": first_probe, "history": first_boot},
            "secondStartup": {"service": second_service, "probe": second_probe, "history": second_boot},
            "fixtureCounts": counts,
        }
        write_json(state / "b-result.json", result)
        return result

    def cleanup(self):
        began = now()
        errors, warnings, remaining = [], [], []
        for kind in ("container", "network"):
            for item in reversed(self.ledger[kind + "s"]):
                name = item["name"]
                try:
                    info = self.inspect(kind, name)
                    if info is not None:
                        labels = info.get("Config", {}).get("Labels", {}) if kind == "container" else info.get("Labels", {})
                        require(all((labels or {}).get(k) == v for k, v in item["labels"].items()), f"ownership mismatch; refusing removal: {name}")
                        identifier = info["Id"]
                        if kind == "container":
                            for argv, label in ((["docker", "logs", identifier], "cleanup-container-logs"),
                                                (["docker", "exec", "--env", f"PATH={sd.EXEC_PATH}", identifier,
                                                  "journalctl", "--no-pager", "-n", "400"], "cleanup-container-journal")):
                                try:
                                    self.command(argv, label, timeout=30, check=False)
                                except BaseException as exc:
                                    warnings.append(f"{label}: {type(exc).__name__}: {exc}")
                            try:
                                self.command(["docker", "stop", "--time", "10", identifier], "cleanup-stop", timeout=30, check=False)
                            except BaseException as exc:
                                warnings.append(f"stop {name}: {type(exc).__name__}: {exc}")
                            # An interrupted stop can have succeeded. Observe instead of replaying it.
                            still = self.inspect(kind, name)
                            if still is not None:
                                require(still["Id"] == identifier, f"container identity changed during cleanup: {name}")
                                self.command(["docker", "rm", "-f", identifier], "cleanup-container", timeout=60, check=False)
                        else:
                            self.command(["docker", "network", "rm", identifier], "cleanup-network", timeout=60, check=False)
                except BaseException as exc:
                    warnings.append(f"cleanup {name}: {type(exc).__name__}: {exc}")
                # Absence, not rm's exit status, establishes cleanup. Unknown is never success.
                try:
                    final = self.inspect(kind, name)
                    if final is not None:
                        remaining.append({"kind": kind, "name": name, "state": "present"})
                        errors.append(f"{kind} remains: {name}")
                    else:
                        item.update(removed=True, removedAt=now())
                except BaseException as exc:
                    remaining.append({"kind": kind, "name": name, "state": "unknown"})
                    errors.append(f"{kind} state unknown: {name}: {type(exc).__name__}: {exc}")
        # Certificates were trusted only inside this attempt's container, never on the host.
        for directory in self.ledger["temporaryDirectories"]:
            try:
                require(pathlib.Path(directory) == self.certs, "unowned temporary directory")
                if self.certs.exists():
                    shutil.rmtree(self.certs)
                require(not self.certs.exists(), "certificate directory remains")
            except BaseException as exc:
                errors.append(f"certificate cleanup: {type(exc).__name__}: {exc}")
                remaining.append({"kind": "directory", "name": directory, "state": "unknown"})
        report = {"ok": not errors, "startedAt": began, "finishedAt": now(), "errors": errors,
                  "warnings": warnings, "ownedResourcesRemaining": remaining, "globalPruneUsed": False,
                  "preservedEvidenceDirectory": str(self.dir), "preservedCacheDirectory": self.config["cacheDir"]}
        self.save_ledger()
        write_json(self.dir / "cleanup.json", report)
        return report

    def report(self, *, ok, cleanup, failure=None):
        return {"ok": ok, "case": self.case, "version": self.version, "previousVersion": self.previous_version,
                "runId": self.run_id, "attempt": self.attempt, "zipSha256": self.asset_sha(self.version),
                "startedAt": self.started, "finishedAt": now(), "stage": self.stage, "failure": failure,
                "cleanup": cleanup, "result": self.results, "evidenceDirectory": str(self.dir)}

    def execute(self):
        failure = None
        cleanup = {"ok": False, "errors": ["cleanup not completed"], "ownedResourcesRemaining": []}
        write_json(self.case_dir / "report.json", self.report(ok=False, cleanup=cleanup))
        try:
            self.stage = "asset-identity"
            write_json(self.dir / "asset-identity.json", verify_assets(self.config))
            self.stage = "image-identity"
            image = json.loads(self.command(["docker", "image", "inspect", self.config["image"]], "inspect-image").stdout)[0]
            require(image.get("Id") == self.config["image"], "Docker image identity mismatch")
            write_json(self.dir / "image-identity.json", image)
            self.stage = "fixture-certificates"
            self.generate_certificates()
            self.stage = "network"
            self.create_network()
            self.results = self.run_a() if self.case == "A" else self.run_b()
        except BaseException as exc:
            failure = {"stage": self.stage, "type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
            write_json(self.dir / "failure.json", failure)
        finally:
            try:
                cleanup = self.cleanup()
            except BaseException as exc:
                cleanup = {"ok": False, "errors": [f"{type(exc).__name__}: {exc}"],
                           "ownedResourcesRemaining": [{"kind": "unknown", "ledger": str(self.dir / "resource-ledger.json")} ]}
        if not cleanup["ok"] and failure is None:
            failure = {"stage": "cleanup", "type": "CleanupFailure", "message": "; ".join(cleanup["errors"])}
        report = self.report(ok=failure is None and cleanup["ok"], cleanup=cleanup, failure=failure)
        write_json(self.dir / "report.json", report)
        write_json(self.case_dir / "report.json", report)
        print(json.dumps({"ok": report["ok"], "case": self.case, "report": str(self.case_dir / "report.json")}, ensure_ascii=False))
        return 0 if report["ok"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--case", required=True, choices=("A", "B"))
    parser.add_argument("--approve-container-cgroup", action="store_true")
    args = parser.parse_args(argv)
    # No config reads, directory creation, commands, Docker or namespace operations before this gate.
    if not args.approve_container_cgroup:
        parser.error("this invocation requires --approve-container-cgroup (not persistent authorization)")
    started = now()
    config = None
    run = None
    previous_signal = signal.getsignal(signal.SIGTERM)

    def terminate(_signum, _frame):
        raise KeyboardInterrupt("SIGTERM")

    signal.signal(signal.SIGTERM, terminate)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        run = AcceptanceRun(config, args.case, approved=True)
        return run.execute()
    except BaseException as exc:
        # Structural/configuration failures still get evidence when a safe output path exists.
        failure = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        try:
            paths = validated_paths(config)
            case_dir = paths["outputDir"] / f"case-{args.case.lower()}"
            require(case_dir.resolve() == case_dir, "case output contains a symlink")
            assets = config.get("assets")
            zip_entry = assets.get(f"openbear-{config.get('version')}.zip") if isinstance(assets, dict) else None
            zip_hash = zip_entry.get("sha256") if isinstance(zip_entry, dict) else None
            report = {"ok": False, "case": args.case, "version": config.get("version"),
                      "previousVersion": config.get("previousVersion"), "runId": config.get("runId"),
                      "zipSha256": zip_hash,
                      "startedAt": started, "finishedAt": now(), "failure": failure,
                      "cleanup": ({"ok": True, "errors": [], "ownedResourcesRemaining": []} if run is None else
                                  {"ok": False, "errors": ["runner failed outside report finalization; inspect attempt ledger"],
                                   "ownedResourcesRemaining": [{"kind": "unknown", "ledger": str(run.dir / "resource-ledger.json")} ]})}
            write_json(case_dir / "report.json", report)
            write_json(case_dir / ("failure-" + uuid.uuid4().hex + ".json"), failure)
        except Exception:
            pass  # Malformed/unsafe paths cannot be used as an output authority.
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_signal)


if __name__ == "__main__":
    raise SystemExit(main())
