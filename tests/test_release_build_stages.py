"""Release-stage contract tests. Docker is fake here only, never in production."""
from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_build_stages", ROOT / "scripts/release_support/build.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fixture_source(root, version="1.2.4"):
    put(root / "app/__init__.py", f'__version__ = "{version}"\n')
    put(root / "prompts/main.tpl", "hello")
    shutil.copytree(ROOT / "benchmark-package", root / "benchmark-package", dirs_exist_ok=True)
    put(root / "pyproject.toml", f'[project]\nname="openbear"\nversion="{version}"\n')
    put(root / "uv.lock", f'version=1\n[[package]]\nname="openbear"\nversion="{version}"\nsource={{virtual="."}}\n')
    for name in ("openbear.service", "openbear.json.example", "README.md"):
        put(root / name, "{}")
    put(root / "scripts/install.sh", "#!/bin/bash\necho installer\n")
    put(root / "scripts/runtime.py", "# retained runtime helper\n")
    put(root / "scripts/smoke_release_login.py", "# fake Docker executes only the test simulation\n")
    put(root / "scripts/release.py", "# must not ship\n")
    put(root / "scripts/release_local.py", "# local CLI adapter, must not ship\n")
    put(root / "scripts/release_support/harness.py", "# must not ship\n")
    for name in ("updater.py", "release_validation.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    package = {"name": "openbear-web", "version": version, "scripts": {"postinstall": "node scripts/patch.mjs"}}
    put(root / "web/package.json", json.dumps(package))
    put(root / "web/package-lock.json", json.dumps({"version": version, "packages": {"": {"version": version}}}))
    put(root / "web/scripts/patch.mjs", "// actual lifecycle input\n")
    put(root / "web/src/main.js", "// frontend\n")
    put(root / "tests/test_example.py", "# test input, not package content\n")
    # Public node source; it is released separately and never enters the ZIP.
    put(root / "node/go.mod", "module openbear/node\n")
    put(root / "node/VERSION", "1.0.0\n")
    put(root / "node/cmd/openbear-node/main.go", "package main\n")
    return root


def fixture_previous(root, version="1.2.3"):
    root.mkdir()
    meta = json.dumps({"schema": 1, "version": version, "effect": "restart", "requiresRestart": True})
    installer = "#!/bin/bash\necho installer\n"
    name = f"openbear-{version}.zip"
    with zipfile.ZipFile(root / name, "w") as archive:
        archive.writestr("app/__init__.py", f'__version__ = "{version}"\n')
        archive.writestr("scripts/install.sh", installer)
        archive.writestr("release-meta.json", meta)
    put(root / "install.sh", installer)
    put(root / "release-meta.json", meta)
    put(root / "SHA256SUMS", "".join(f"{build.sha256(root / n)}  {n}\n" for n in (name, "install.sh", "release-meta.json")))
    return root


@pytest.fixture
def config(tmp_path):
    source = fixture_source(tmp_path / "source")
    previous = fixture_previous(tmp_path / "previous")
    return build.validate_config({"schema": 1, "runId": "test-owned-run", "version": "1.2.4",
        "previousVersion": "1.2.3", "sourceDir": str(source), "runDir": str(tmp_path / "run"),
        "cacheDir": str(tmp_path / "cache"), "previousAssets": str(previous),
        "pythonImages": {v: "baseline-py" + v for v in ("311", "312", "313")},
        "systemdImage": "only-recorded:not-executed"})


class FakeDocker:
    def __init__(self, config, monkeypatch):
        self.config = config
        self.configs = {config["runId"]: config}
        self.real_run = subprocess.run
        self.images = {"baseline-py" + v: "sha256:" + v.ljust(64, "0") for v in ("311", "312", "313")}
        self.images[build.NODE_BASE] = "sha256:" + "f" * 64
        self.containers = {}
        self.calls = []
        self.created = []
        self.interrupt = None
        self.error_text = ""
        self.smoke_lifecycle = {"fullStartup": True, "ready": True, "cleanShutdown": True}
        self.test_returncode = 0
        self.junit = '<testsuites><testsuite tests="2" errors="0" failures="0" skipped="1"><testcase classname="tests.test_real" name="test_pass"/><testcase classname="tests.test_real" name="test_skip"><skipped message="live credentials absent"/></testcase></testsuite></testsuites>'
        self.tap = '# tests 3\n# pass 2\n# fail 0\n# cancelled 0\n# skipped 1\n# todo 0\n'
        monkeypatch.setattr(build.subprocess, "run", self.run)

    def response(self, args, kwargs, code=0, stdout="", stderr=""):
        stream = kwargs.get("stdout")
        if hasattr(stream, "write"):
            stream.write(stdout + stderr)
            stream.flush()
            stdout = None
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr=stderr)

    def run(self, args, **kwargs):
        if args[0] != "docker":
            return self.real_run(args, **kwargs)
        self.calls.append(args)
        if args[1:3] == ["image", "inspect"]:
            ref = args[3]
            image = ref if ref.startswith("sha256:") else self.images.get(ref)
            return self.response(args, kwargs, 0 if image else 1, image or "", "" if image else "No such image")
        if args[1] == "pull":
            self.images[args[2]] = "sha256:" + "f" * 64
            return self.response(args, kwargs)
        if args[1] == "create":
            name = args[args.index("--name") + 1]
            labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
            mounts = {}
            for i, value in enumerate(args):
                if value == "--mount":
                    data = dict(item.split("=", 1) for item in args[i + 1].split(",") if "=" in item)
                    mounts[data["dst"]] = Path(data["src"])
            owner = json.loads((self.configs[labels[build.LABEL]]["runDir"] / "results" / labels["openbear.release.stage"] / "attempts" / labels["openbear.release.attempt"] / "resources.json").read_text())
            assert any(r["name"] == name and r["state"] == "planned" for r in owner), "owner must precede create"
            self.containers[name] = {"Id": name + "-id", "Image": args[-3], "Config": {"Labels": labels}, "script": args[-1], "mounts": mounts, "args": args}
            self.created.append(self.containers[name])
            return self.response(args, kwargs, stdout=name)
        if args[1:3] == ["container", "inspect"]:
            info = self.containers.get(args[3])
            public = {k: info[k] for k in ("Id", "Image", "Config")} if info else None
            return self.response(args, kwargs, 0 if info else 1, json.dumps([public]) if info else "", "" if info else "Error: No such container")
        if args[1] == "rm":
            for name, data in list(self.containers.items()):
                if data["Id"] == args[-1]:
                    del self.containers[name]
            return self.response(args, kwargs)
        if args[1] == "commit":
            return self.response(args, kwargs, stdout="sha256:" + str(len(self.created)).zfill(64))
        if args[1] == "start":
            if self.interrupt:
                raise self.interrupt
            item = self.containers[args[-1]]
            script, mounts = item["script"], item["mounts"]
            out = mounts.get("/out")
            if "bash /recipes/node-env.sh" in script:
                put(out / "node_modules/module.js", "// npm ci simulation\n")
                put(out / "toolchain/bin/node", "node")
                put(out / "toolchain/bin/npm", "npm")
            if out:
                for name in re.findall(r"/out/([a-zA-Z0-9_.-]*toolchain[a-zA-Z0-9_.-]*\.json)", script):
                    put(out / name, json.dumps({"node": "v20.20.2", "npm": "11.11.0", "python": "3.12.9", "git": "git version 2.47", "bash": "GNU bash", "pytest-xdist": "3.8.0", "execnet": "2.1.2"}))
            if "python -m pytest" in script:
                put(out / "junit.xml", self.junit)
                return self.response(args, kwargs, self.test_returncode, "2 tests\n" + self.error_text)
            if "node --test --test-concurrency=2" in script:
                return self.response(args, kwargs, self.test_returncode, self.tap + self.error_text)
            if "npm run build -- --outDir /out/dist" in script:
                put(out / "dist/index.html", '<script type="module" src="/assets/main.js"></script>')
                put(out / "dist/assets/main.js", "console.log('built')")
                put(out / "dist/build-info.json", json.dumps({"schema": 1, "version": self.config["version"], "buildId": "a" * 16}))
            if "python scripts/smoke_release_login.py" in script:
                assert "/inputs/source" not in mounts
                with zipfile.ZipFile(mounts["/inputs/release.zip"]) as archive:
                    assert "scripts/smoke_release_login.py" in archive.namelist()
                put(out / "smoke.json", json.dumps({"ok": True, "version": self.config["version"], "transport": "http-websocket", "serviceLifecycle": self.smoke_lifecycle}))
            return self.response(args, kwargs, stdout=self.error_text)
        raise AssertionError(args)


@pytest.fixture
def docker(config, monkeypatch):
    return FakeDocker(config, monkeypatch)


def stage(config, name):
    result = build.Stage(config, name).execute()
    assert json.loads((config["runDir"] / "results" / name / "result.json").read_text()) == result
    return result


def prepare(config, docker):
    result = stage(config, "env")
    assert result["ok"], result
    return result


def test_env_matching_shared_node_cache_and_three_independent_abis(config, docker):
    result = prepare(config, docker)
    assert set(result["pythonImages"]) == {"311", "312", "313"}
    assert all(v.startswith("sha256:") for v in result["pythonImages"].values())
    assert len(set(result["pythonImages"].values())) == 3
    assert result["nodeImage"].startswith("sha256:")
    node_dir = Path(result["nodeDir"])
    assert node_dir.is_relative_to(config["cacheDir"])
    assert (node_dir / "complete.json").is_file()
    assert not docker.containers
    py_preparations = [i for i in docker.created if "bash /recipes/python-env.sh" in i["script"]]
    assert {i["Image"] for i in py_preparations} == set(docker.images[v] for v in config["pythonImages"].values())
    assert len([i for i in docker.created if "bash /recipes/node-env.sh" in i["script"]]) == 1
    before = len([c for c in docker.calls if c[1] == "commit"])
    again = prepare(config, docker)
    assert again["nodeDir"] == result["nodeDir"]
    assert len([c for c in docker.calls if c[1] == "commit"]) == before


def test_cross_run_concurrent_env_creation_has_single_atomic_cache(config, docker):
    other = dict(config, runId="second-run", runDir=config["runDir"].with_name("second-run"))
    docker.configs[other["runId"]] = other
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(stage, item, "env") for item in (config, other)]
        results = [f.result() for f in futures]
    assert all(r["ok"] for r in results), results
    assert results[0]["nodeDir"] == results[1]["nodeDir"]
    assert results[0]["pythonImages"] == results[1]["pythonImages"]
    assert len([call for call in docker.calls if call[1] == "commit"]) == 4
    assert not docker.containers


def test_node_cache_corruption_is_not_reused(config, docker):
    env = prepare(config, docker)
    put(Path(env["nodeDir"]) / "node_modules/module.js", "corrupted")
    report = stage(config, "py311")
    assert not report["ok"] and "Node cache content changed" in report["error"]
    assert not stage(config, "env")["ok"]


def test_recipe_uses_candidate_frozen_lock_extra_tools_and_real_postinstall():
    text = (build.IMAGES / "python-env.sh").read_text()
    assert 'uv sync --frozen --extra dev --python "$base_python"' in text
    assert "pytest-xdist==3.8.0 execnet==2.1.2" in text
    assert "rm -rf /opt/testenv" in text and "--inexact" not in text
    node = (build.IMAGES / "node-env.sh").read_text()
    assert "npm ci --include=dev" in node and "--ignore-scripts" not in node
    assert "npm@11.11.0" in node and "v20.20.2" in node


def test_dependency_fingerprint_normalizes_only_own_versions(config):
    source = config["sourceDir"]
    before = build.digest_json(build.normalize_dependencies(source))
    for name in ("pyproject.toml", "uv.lock", "web/package.json", "web/package-lock.json", "app/__init__.py"):
        path = source / name
        path.write_text(path.read_text().replace("1.2.4", "1.2.5"))
    assert build.digest_json(build.normalize_dependencies(source)) == before
    put(source / "web/scripts/patch.mjs", "// changed postinstall\n")
    assert build.digest_json(build.normalize_dependencies(source)) != before


def test_dependency_and_image_changes_never_reuse_old_cache(config, docker):
    first = prepare(config, docker)
    put(config["sourceDir"] / "web/scripts/patch.mjs", "// changed postinstall\n")
    second = prepare(config, docker)
    assert second["nodeDir"] != first["nodeDir"]
    assert second["pythonImages"] == first["pythonImages"]
    docker.images["baseline-py311"] = "sha256:" + "e" * 64
    third = prepare(config, docker)
    assert third["pythonImages"]["311"] != second["pythonImages"]["311"]
    assert third["pythonImages"]["312"] == second["pythonImages"]["312"]


@pytest.mark.parametrize("name", ["py311", "py312", "py313", "frontend"])
def test_tests_copy_source_readonly_shared_node_no_network_and_report_counts(config, docker, name):
    env = prepare(config, docker)
    result = stage(config, name)
    assert result["ok"], result
    executed = next(i for i in reversed(docker.created) if "node --test" in i["script"] or "python -m pytest" in i["script"])
    args = executed["args"]
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--cpus") + 1] == "2"
    assert args[args.index("--memory") + 1] == "6g"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert "--pids-limit" in args
    assert executed["Config"]["Labels"][build.LABEL] == config["runId"]
    assert executed["mounts"]["/inputs/node"] == Path(env["nodeDir"])
    assert all("readonly" in a for a in args if "dst=/inputs/" in a)
    assert "cp -a --no-preserve=ownership /inputs/source/. /work/source/" in executed["script"]
    assert "ln -s /inputs/node/node_modules" in executed["script"]
    assert result["counts"]["skipped"] == 1
    assert result["toolchain"]["node"] == "v20.20.2"
    if name.startswith("py"):
        assert "pytest -q -n 2 --dist=worksteal --junitxml=/out/junit.xml" in executed["script"]
        assert result["testCases"][1] == {"id": "tests.test_real::test_skip", "status": "skipped", "skipReason": "live credentials absent"}
        assert "junitSha256" in result["outputs"]
    else:
        assert "node --test --test-concurrency=2" in executed["script"]
    assert not docker.containers


@pytest.mark.parametrize("signal", ["Task exception was never retrieved", "Task was destroyed but it is pending!", "PytestUnraisableExceptionWarning", "Exception ignored in:"])
def test_zero_exit_with_real_unhandled_log_is_failure(config, docker, signal):
    prepare(config, docker)
    docker.error_text = signal
    result = stage(config, "py311")
    assert not result["ok"] and result["exitCode"] != 0
    assert result["unhandled"]
    assert result["counts"]["tests"] == 2


def test_failed_junit_preserves_complete_cases(config, docker):
    prepare(config, docker)
    docker.test_returncode = 1
    docker.junit = docker.junit.replace('<testcase classname="tests.test_real" name="test_pass"/>', '<testcase classname="tests.test_real" name="test_fail"><failure message="bad"/></testcase>')
    result = stage(config, "py313")
    assert not result["ok"] and result["counts"]["failed"] == 1
    assert len(result["testCases"]) == 2
    assert not docker.containers


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["docker", "start"], 600), KeyboardInterrupt()])
def test_timeout_and_interrupt_always_cleanup_and_save_failure(config, docker, error):
    prepare(config, docker)
    docker.interrupt = error
    result = stage(config, "py312")
    assert not result["ok"] and result["exitCode"] in (124, 130)
    assert Path(result["log"]).is_file()
    assert not docker.containers
    records = json.loads(Path(result["resources"]).read_text())
    assert records[0]["state"] == "removed"


def test_missing_baseline_fails_clearly_without_fallback(config, docker):
    del docker.images["baseline-py313"]
    result = stage(config, "env")
    assert not result["ok"]
    assert "explicit BASE_IMAGE" in result["error"]
    assert not docker.created


def test_build_has_no_test_gate_and_writable_node_copy_single_build(config, docker):
    prepare(config, docker)
    result = stage(config, "build")
    assert result["ok"], result
    script = docker.created[-1]["script"]
    assert "cp -a --no-preserve=ownership /inputs/node/node_modules /work/source/web/node_modules" in script
    assert "npm run build -- --outDir /out/dist" in script
    assert not (config["sourceDir"] / "web/dist").exists()
    assert (config["runDir"] / "build/dist/index.html").is_file()
    before = len(docker.created)
    assert stage(config, "build")["reused"]
    assert len(docker.created) == before
    put(config["runDir"] / "build/dist/assets/main.js", "corruption")
    refused = stage(config, "build")
    assert not refused["ok"] and "modified" in refused["error"]
    assert len(docker.created) == before


def test_existing_unidentified_build_is_not_overwritten(config, docker):
    prepare(config, docker)
    put(config["runDir"] / "build/dist/index.html", "already built")
    before = len(docker.created)
    result = stage(config, "build")
    assert not result["ok"] and "must not be overwritten" in result["error"]
    assert len(docker.created) == before


def test_package_actual_candidate_classifier_validator_unique_zip_and_smoke(config, docker):
    prepare(config, docker)
    assert stage(config, "build")["ok"]
    (config["runDir"] / "assets").mkdir()  # controller may precreate the empty output directory
    result = stage(config, "package")
    assert result["ok"], result
    assets = config["runDir"] / "assets"
    assert set(result["assets"]) == {"install.sh", "openbear-1.2.4.zip", "SHA256SUMS", "release-meta.json"}
    assert result["zipSha256"] == build.sha256(assets / "openbear-1.2.4.zip")
    with zipfile.ZipFile(assets / "openbear-1.2.4.zip") as archive:
        names = archive.namelist()
        assert "scripts/runtime.py" in names
        assert "benchmark-package/ip.check.place/v0.1.0/main/ref/cookies.txt" in names
        assert "benchmark-package/tcpquality/v0.1.0/manifest.json" in names
        assert "scripts/smoke_release_login.py" in names
        assert "scripts/release.py" not in names
        assert "scripts/release_local.py" not in names
        assert not any(n.startswith(("scripts/release_support/", "tests/")) for n in names)
        assert not any(n.startswith("node/") or n.rsplit("/", 1)[-1].startswith("openbear-node") for n in names)
        assert json.loads(archive.read("release-meta.json"))["comparedWith"] == "1.2.3"
    assert result["classification"]["effect"] in ("restart", "refresh", "noop")
    again = stage(config, "package")
    assert again["ok"] and again["reused"] and again["zipSha256"] == result["zipSha256"]
    smoke = stage(config, "smoke")
    assert smoke["ok"] and smoke["smoke"]["ok"]
    assert smoke["zipSha256"] == result["zipSha256"]
    assert docker.created[-1]["mounts"]["/inputs/release.zip"] == assets / "openbear-1.2.4.zip"
    assert "/inputs/source" not in docker.created[-1]["mounts"]
    put(config["sourceDir"] / "app/other.py", "# different candidate\n")
    refused = stage(config, "package")
    assert not refused["ok"]
    assert build.sha256(assets / "openbear-1.2.4.zip") == result["zipSha256"]


@pytest.mark.parametrize("missing", ["fullStartup", "ready", "cleanShutdown"])
def test_smoke_rejects_http_success_without_full_lifecycle(config, docker, missing):
    prepare(config, docker)
    assert stage(config, "build")["ok"]
    assert stage(config, "package")["ok"]
    docker.smoke_lifecycle.pop(missing)
    result = stage(config, "smoke")
    assert not result["ok"]
    assert "full service startup" in result["error"]


def test_package_refuses_tampering_and_previous_asset_mismatch(config, docker):
    prepare(config, docker)
    assert stage(config, "build")["ok"]
    put(config["previousAssets"] / "install.sh", "changed")
    failed = stage(config, "package")
    assert not failed["ok"] and "checksums" in failed["error"]
    assert not (config["runDir"] / "assets").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "x/../../escape", "x\\escape", "a/./b", "a//b", "C:/escape"])
def test_safe_zip_rejects_unsafe_paths(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(name, "payload")
    with pytest.raises(ValueError, match="unsafe"):
        build.safe_extract_zip(archive, tmp_path / "unpacked")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600])
def test_safe_zip_rejects_symlink_and_special_file(tmp_path, mode):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        info = zipfile.ZipInfo("link")
        info.external_attr = mode << 16
        z.writestr(info, "../../secret")
    with pytest.raises(ValueError, match="unsafe"):
        build.safe_extract_zip(archive, tmp_path / "unpacked")


def test_safe_zip_preserves_executable_runtime_script(tmp_path):
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as z:
        info = zipfile.ZipInfo("scripts/install.sh")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        z.writestr(info, "#!/bin/bash\n")
    build.safe_extract_zip(archive, tmp_path / "unpacked")
    assert (tmp_path / "unpacked/scripts/install.sh").stat().st_mode & 0o111


@pytest.mark.parametrize("name", ["scripts/tests/test_secret.py", "app/openbear.json", "app/data/secret.json", "app/tmp.db", "prompts/private/note", "scripts/release.py", "scripts/release_local.py", "app/tools/release.py", "scripts/release_support/harness.py", "web/src/main.js"])
def test_package_audit_rejects_private_or_harness(tmp_path, name):
    put(tmp_path / name, "private")
    with pytest.raises(build.ReleaseError):
        build.audit_package(tmp_path)


def test_safe_zip_rejects_duplicate_entries(tmp_path):
    archive = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("app/file", "one")
        with pytest.warns(UserWarning, match="Duplicate name"):
            z.writestr("app/file", "two")
    with pytest.raises(ValueError, match="unsafe"):
        build.safe_extract_zip(archive, tmp_path / "unpacked")


def test_source_symlink_and_output_escape_rejected(config, tmp_path):
    (config["sourceDir"] / "escape").symlink_to(tmp_path / "secret")
    result = stage(config, "env")
    assert not result["ok"] and "non-regular" in result["error"]
    (config["runDir"] / "assets").symlink_to(tmp_path / "foreign")
    with pytest.raises(build.ReleaseError, match="escapes"):
        build.owned(config["runDir"], "assets/release.zip")


def add_previous_frontend(config):
    root = config["previousAssets"]
    name = f"openbear-{config['previousVersion']}.zip"
    with zipfile.ZipFile(root / name, "a") as archive:
        archive.writestr("web/dist/index.html", '<script src="/assets/main.js"></script>')
        archive.writestr("web/dist/assets/main.js", "console.log('previous compiled bytes')")
        archive.writestr("web/dist/build-info.json", json.dumps({"schema": 1, "version": config["previousVersion"], "buildId": "a" * 16}))
    put(root / "SHA256SUMS", "".join(f"{build.sha256(root / n)}  {n}\n" for n in (name, "install.sh", "release-meta.json")))
    source = config["sourceDir"]
    identity = build.identities({p.relative_to(source).as_posix(): p.read_bytes() for p in (source / "web").rglob("*") if p.is_file()})["frontend"]
    stage_identity = build.identities({p: (source / p).read_bytes() for p in build.tree_files(source)})["frontendStage"]
    config["releaseScope"] = {"frontendChanged": False, "frontendFingerprint": identity,
                              "previousFrontendFingerprint": identity, "previousFrontendStageFingerprint": stage_identity}


def test_backend_release_reuses_original_frontend_bytes_and_build_id(config, docker):
    prepare(config, docker)
    add_previous_frontend(config)
    previous = config["previousAssets"] / f"openbear-{config['previousVersion']}.zip"
    original = build.sha256(previous)
    before = len(docker.created)
    result = stage(config, "build")
    assert result["ok"] and result["reusedFrontend"], result
    assert len(docker.created) == before, "reuse must not invoke npm or a build container"
    dist = config["runDir"] / "build/dist"
    with zipfile.ZipFile(previous) as archive:
        for name in ("index.html", "assets/main.js"):
            assert (dist / name).read_bytes() == archive.read("web/dist/" + name)
    assert json.loads((dist / "build-info.json").read_text()) == {"schema": 1, "version": config["version"], "buildId": "a" * 16}
    assert build.sha256(previous) == original
    assert stage(config, "package")["ok"]  # Existing, unchanged version validator.


def test_reuse_refuses_frontend_source_proof_drift(config, docker):
    prepare(config, docker)
    add_previous_frontend(config)
    config["releaseScope"]["previousFrontendFingerprint"] = "wrong"
    result = stage(config, "build")
    assert not result["ok"] and "source proof" in result["error"]


def test_frontend_contract_stage_uses_real_explicit_python_tests(config, docker):
    for name in build.FRONTEND_CHECKS:
        put(config["sourceDir"] / name, "# fixture test")
    prepare(config, docker)
    result = stage(config, "frontend-checks")
    assert result["ok"], result
    script = docker.created[-1]["script"]
    assert "python -m pytest" in script
    assert all(name in script for name in build.FRONTEND_CHECKS)


def test_known_postinstall_cache_ignores_ui_edits_but_not_patch_or_lock_changes(config):
    source = config["sourceDir"]
    package = json.loads((source / "web/package.json").read_text())
    package["scripts"]["postinstall"] = "node scripts/patch-sortable-scroll.mjs"
    put(source / "web/package.json", json.dumps(package))
    put(source / "web/scripts/patch-sortable-scroll.mjs", "// installer")
    before = build.normalize_dependencies(source)
    put(source / "web/src/main.js", "// UI correction")
    assert build.normalize_dependencies(source) == before
    put(source / "web/scripts/patch-sortable-scroll.mjs", "// changed installer")
    assert build.normalize_dependencies(source) != before


def test_config_allows_source_under_run_but_not_outputs_under_source(config):
    raw = {k: str(v) if isinstance(v, Path) else v for k, v in config.items()}
    raw["runDir"] = str(config["sourceDir"] / "bad-output")
    with pytest.raises(build.ReleaseError):
        build.validate_config(raw)
    raw["runDir"] = str(config["sourceDir"].parent)
    # cross-run cache cannot be placed inside this broader run either.
    with pytest.raises(build.ReleaseError):
        build.validate_config(raw)


def test_foreign_run_owner_not_modified(config, docker):
    prepare(config, docker)
    before = build.tree_files(config["runDir"])
    other = dict(config, runId="different-owner")
    with pytest.raises(build.ReleaseError, match="another release"):
        build.Stage(other, "build").execute()
    assert build.tree_files(config["runDir"]) == before


def test_package_reuse_and_smoke_reject_exact_zip_tampering(config, docker):
    prepare(config, docker)
    assert stage(config, "build")["ok"]
    assert stage(config, "package")["ok"]
    archive = config["runDir"] / "assets/openbear-1.2.4.zip"
    with archive.open("ab") as stream:
        stream.write(b"changed")
    for name in ("package", "smoke"):
        result = stage(config, name)
        assert not result["ok"] and "hash mismatch" in result["error"]


def test_frontend_malformed_cancelled_or_unhandled_cannot_pass(config, docker):
    prepare(config, docker)
    docker.tap = '# tests 1\n# pass 0\n# fail 0\n# cancelled 1\n# skipped 0\n'
    result = stage(config, "frontend")
    assert not result["ok"] and result["counts"]["cancelled"] == 1
    docker.tap = '# pass 2\n'
    assert not stage(config, "frontend")["ok"]


def test_cli_failure_nonzero_and_result(config, docker, tmp_path, capsys):
    path = tmp_path / "config.json"
    put(path, json.dumps({k: str(v) if isinstance(v, Path) else v for k, v in config.items()}))
    docker.images.clear()
    assert build.main(["--config", str(path), "--stage", "env"]) != 0
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "env" and report["ok"] is False
    assert Path(report["log"]).is_file()
