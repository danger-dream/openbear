"""Corrective-candidate end-to-end graph; external Docker/publication are fakes."""
from __future__ import annotations

import json
import shutil
import sys
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_release_build_stages import FakeDocker, build, fixture_previous, fixture_source
from tests.test_release_entrypoint import git_repo  # noqa: F401 (temporary Git fixture)
from tests.test_release_scope import setup_runner, source

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from release_support import candidate, runner, scope  # noqa: E402
from release_support.common import Commands, ReleaseError, load, sha  # noqa: E402


@pytest.mark.parametrize("path,frontend_changes", [
    ("tests/test_cron.py", False), ("app/llm/client.py", False),
    ("tests/conftest.py", True), ("tests/fixtures/api.json", True),
    ("scripts/build.py", True), ("app/web_console/api.py", True),
    ("app/models/request.py", True), ("unknown/input", True), ("web/src/App.vue", True),
])
def test_stage_input_fingerprints_ignore_only_proven_independent_inputs(path, frontend_changes):
    old = source()
    new = {**old, path: b"new input"}
    first, second = scope.identities(old), scope.identities(new)
    assert (first["frontendStage"] != second["frontendStage"]) is frontend_changes
    assert (first["backendStage"] != second["backendStage"]) is (not path.startswith("web/"))


def test_success_is_cached_before_sibling_finishes_or_fails(tmp_path):
    obj, env, calls = setup_runner(tmp_path)
    ordinary_stage = obj.build_stage
    cached = threading.Event()

    def stage(name):
        if name == "py312":
            assert cached.wait(5)
            # The first backend is still blocked: frontend evidence must already
            # be durable, without a successful combined matrix.
            assert obj.cached_test("frontend", env)
            raise ReleaseError("diagnosed failure, no retry")
        return ordinary_stage(name)

    def build_once(_env):
        assert obj.cached_test("frontend", env)
        cached.set()
        raise ReleaseError("independent build failure")

    obj.build_stage, obj.build_once = stage, build_once
    with pytest.raises(ReleaseError):
        obj.run_tests(env, with_build=True)
    assert obj.value["stages"]["frontend"]["status"] == "passed"
    assert obj.cached_test("py311", env) and obj.cached_test("py313", env)
    assert obj.cached_test("py312", env) is None
    assert calls["frontend"] == 1


@pytest.mark.parametrize("correction", ["python", "frontend"])
def test_failed_candidate_fix_reuses_independent_stages_and_reaccepts(git_repo, tmp_path, monkeypatch, correction):
    # Populate a disposable Git repository only. No commands touch the real
    # workspace Git history, Docker daemon, remote repositories or services.
    repo = git_repo
    fixture_source(repo, "1.0.0")
    package = json.loads((repo / "web/package.json").read_text())
    package["scripts"]["postinstall"] = "node scripts/patch-sortable-scroll.mjs"
    (repo / "web/package.json").write_text(json.dumps(package))
    (repo / "web/scripts/patch.mjs").rename(repo / "web/scripts/patch-sortable-scroll.mjs")
    (repo / "uv.lock").write_text('version=1\n[[package]]\nname = "openbear"\nversion = "1.0.0"\nsource={virtual="."}\n')
    (repo / "tests/test_cron.py").write_text("# initial Python test\n")
    for name in scope.FRONTEND_CHECKS:
        (repo / name).write_text("# declared cross-end contract fixture\n")
    previous_root = fixture_previous(tmp_path / "formal-assets", "1.0.0")
    run_id = "1.0.1-123456789abc"
    directory = repo / ".release-runs" / run_id
    directory.mkdir(parents=True)
    files = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*")
                   if p.is_file() and not p.relative_to(repo).parts[0].startswith(".git")
                   and p.relative_to(repo).parts[0] != ".release-runs")
    # .gitignore is public input, unlike the .git internals excluded above.
    files.append(".gitignore")
    previous_source = {p: (repo / p).read_bytes() for p in files}
    previous_source["web/src/main.js"] = b"previous frontend"
    previous_source["tests/test_cron.py"] = b"previous Python tests"
    value = {"schema": 1, "runId": run_id, "repo": str(repo), "version": "1.0.1", "oldVersion": "1.0.0",
             "previousVersion": "1.0.0", "repository": "fixture/repo", "createdAt": 0, "files": files,
             "python": sys.executable, "notes": "fixture notes", "stages": {},
             "images": {**{v: "baseline-py" + v for v in ("311", "312", "313")}, "systemd": "fixture"},
             "previous": {"id": 1}, "previousSourceCommit": "a" * 40}
    cfg = {"schema": 1, "runId": run_id, "version": "1.0.1", "previousVersion": "1.0.0",
           "sourceDir": str(directory / "source"), "runDir": directory,
           "cacheDir": repo / ".release-runs/cache", "previousAssets": str(directory / "previous-assets"),
           "pythonImages": {v: "baseline-py" + v for v in ("311", "312", "313")}, "systemdImage": "fixture"}
    docker = FakeDocker(cfg, monkeypatch)
    docker.junit = '<testsuites><testsuite><testcase classname="tests.test_cron" name="test_ok"/></testsuite></testsuites>'
    calls = Counter()
    package_sources = []

    def export(repo, directory, state, _command):
        root = directory / "source"
        (root / ".git").mkdir(parents=True)
        for name in state.value["candidate"]["files"]:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / name, target)
        state.value["candidate"]["publicCommit"] = "fixture-public"
        return {"ok": True}

    monkeypatch.setattr(candidate, "export", export)
    monkeypatch.setattr(runner, "publish", lambda *_, **__: calls.update(["publication"]) or {"ok": True, "url": "https://example.invalid/release"})

    class LocalRunner(runner.Runner):
        def preflight(self, approved):
            assert approved

        def previous_assets(self):
            target = directory / "previous-assets"
            if not target.exists():
                shutil.copytree(previous_root, target)
            assets = {p.name: {"bytes": p.stat().st_size, "sha256": sha(p.read_bytes())} for p in target.iterdir()}
            self.value["previousAssetsMetadata"] = assets
            return {"ok": True, "assets": assets}

        def classify_scope(self):
            current = {p: (directory / "source" / p).read_bytes() for p in self.value["candidate"]["files"]}
            return scope.compare(previous_source, current, previous_commit="a" * 40)

        def build_stage(self, name):
            calls[name] += 1
            if name == "py312" and calls[name] == 1:
                raise ReleaseError("first Python failure")
            current = build.validate_config(self.build_config())
            result = build.Stage(current, name).execute()
            if not result["ok"]:
                raise ReleaseError(result["error"])
            if name == "package":
                package_sources.append(result["identity"]["source"])
            return result

        def acceptance(self, case, approved):
            assert approved
            calls[case] += 1
            if case == "B" and calls[case] == 1:
                raise ReleaseError("first B failure")
            return {"ok": True}

    def instance(saved):
        return LocalRunner(repo, directory, saved, github=SimpleNamespace(token="fixture"), command=Commands(directory))

    obj = instance(value)
    with pytest.raises(ReleaseError, match="first Python failure"):
        obj.run(approved=True)
    assert calls["publication"] == calls["package"] == 0
    assert calls["frontend"] == calls["build"] == 1
    original_frontend = obj.value["stages"]["frontend"]["result"]
    original_build = obj.value["stages"]["build"]["result"]
    assert scope.verified_result(original_frontend) and runner.verified_build(original_build)
    assert len(list((obj.cache / "tests").glob("*.json"))) == 3

    (repo / "tests/test_cron.py").write_text("# corrected Python test\n")
    obj = instance(load(directory / "state.json"))
    with pytest.raises(ReleaseError, match="first B failure"):
        obj.run(approved=True)
    assert obj.value["scope"]["kind"] == "both", "classification against the formal release must not block stage reuse"
    assert calls["frontend"] == calls["build"] == 1
    assert all(calls[n] == 2 for n in runner.TEST_STAGES[:3])
    assert load(directory / "matrix-summary.json")["reusedStages"] == ["frontend"]
    assert calls["A"] == calls["B"] == calls["package"] == 1
    assert scope.verified_result(original_frontend) and runner.verified_build(original_build)

    if correction == "python":
        (repo / "tests/test_cron.py").write_text("# another reviewed test correction\n")
    else:
        (repo / "web/src/main.js").write_text("// reviewed frontend correction\n")
        passed_junit = docker.junit
        docker.junit = '<testsuites><testsuite><testcase classname="contracts" name="test_web"><failure message="contract mismatch">assert False</failure></testcase></testsuite></testsuites>'
        obj = instance(load(directory / "state.json"))
        with pytest.raises(ReleaseError, match="test failure"):
            obj.run(approved=True)
        assert obj.value["stages"]["frontend-checks"]["status"] == "failed"
        assert all(calls[n] == 2 for n in runner.TEST_STAGES[:3]), "web correction must reuse the three main matrices"
        assert calls["frontend-checks"] == 1
        assert calls["package"] == calls["A"] == calls["B"] == 1
        assert calls["publication"] == 0, "cross-end failure must block packaging and publication"
        contracts = [item for item in docker.created if item["Config"]["Labels"]["openbear.release.stage"] == "frontend-checks"]
        assert contracts and "python -m pytest" in contracts[0]["script"]
        assert all(name in contracts[0]["script"] for name in scope.FRONTEND_CHECKS)
        docker.junit = passed_junit
    obj = instance(load(directory / "state.json"))
    obj.run(approved=True)
    assert obj.value["completed"]
    assert calls["publication"] == 1
    assert calls["frontend"] == calls["build"] == (1 if correction == "python" else 2)
    assert calls["frontend-checks"] == (0 if correction == "python" else 2)
    assert calls["package"] == calls["smoke"] == calls["A"] == calls["B"] == 2
    assert all(calls[n] == (3 if correction == "python" else 2) for n in runner.TEST_STAGES[:3])
    assert len(set(package_sources)) == 2
    assert scope.verified_result(original_frontend) and runner.verified_build(original_build)
    assert not docker.containers
    before = calls.copy()
    instance(load(directory / "state.json")).run(approved=False)
    assert calls == before
