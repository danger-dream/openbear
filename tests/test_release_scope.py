"""Branch selection, genuine prior evidence and dependency-gated concurrency."""
from __future__ import annotations

import copy
import json
import sys
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from release_support import runner, scope  # noqa: E402
from release_support.common import ReleaseError, atomic_json, load, sha  # noqa: E402


def source(version="1.0.0"):
    return {"app/__init__.py": f'__version__ = "{version}"\n'.encode(),
            "app/llm/client.py": b"backend", "web/src/App.vue": b"frontend",
            "web/package.json": json.dumps({"version": version}).encode(),
            "web/package-lock.json": json.dumps({"version": version, "packages": {"": {"version": version}, "node_modules/dep": {"version": "1"}}}).encode(),
            "pyproject.toml": f'[project]\nname="openbear"\nversion="{version}"\n'.encode(),
            "uv.lock": f'[[package]]\nname="openbear"\nversion="{version}"\n'.encode()}


@pytest.mark.parametrize("path,kind", [("web/src/App.vue", "frontend"), ("app/llm/client.py", "backend"),
    ("app/web_console/api.py", "both"), ("app/models/request.py", "both"),
    ("scripts/updater.py", "both"), ("tests/test_one.py", "both"), ("unknown/config", "both"),
    ("node/node.go", "both"), ("node/VERSION", "both")])
def test_change_scope_uses_actual_content_and_conservative_shared_boundaries(path, kind):
    old, new = source(), source("1.0.1")
    new[path] = b"modified"
    result = scope.compare(old, new, previous_commit="a" * 40)
    assert result["kind"] == kind
    assert result["changed"] == [path]
    if path.startswith("node/"):
        # Node source is an unknown path for the controller: full matrix, no reused backend evidence.
        assert result["unknown"] == [path] and result["backendChanged"]


def test_version_normalization_never_hides_dependencies_or_deleted_frontend():
    old, new = source(), source("1.0.1")
    assert scope.compare(old, new, previous_commit="a" * 40)["kind"] == "metadata"
    lock = json.loads(new["web/package-lock.json"])
    lock["packages"]["node_modules/dep"]["version"] = "2"
    new["web/package-lock.json"] = json.dumps(lock).encode()
    assert scope.compare(old, new, previous_commit="a" * 40)["frontendChanged"]
    del new["web/src/App.vue"]
    assert "web/src/App.vue" in scope.compare(old, new, previous_commit="a" * 40)["changed"]
    new = source("1.0.1")
    new["uv.lock"] += b'[[package]]\nname="dep"\nversion="2"\n'
    assert scope.compare(old, new, previous_commit="a" * 40)["kind"] == "both"


class Commands:
    def cancel_all(self):
        pass


def setup_runner(tmp_path, run_id="first", kind="both", front="front", back="back", shared="shared"):
    value = {"runId": run_id, "version": "1.0.1", "repository": "fixture/repo", "stages": {},
             "candidate": {"sourceFingerprint": run_id},
             "scope": {"kind": kind, "frontendChanged": kind in {"both", "frontend"},
                       "frontendFingerprint": front, "backendFingerprint": back,
                       "frontendStageFingerprint": front + shared, "backendStageFingerprint": back + shared}}
    obj = runner.Runner(tmp_path, tmp_path / run_id, value, github=SimpleNamespace(token="fixture"), command=Commands())
    env = {"nodeImage": "node", "nodeKey": "dependencies", "pythonImages": {v: "py" + v for v in ("311", "312", "313")}}
    calls = Counter()

    def stage(name):
        calls[name] += 1
        path = obj.directory / (name + ".log")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("passed " + name)
        return {"ok": True, "counts": {"tests": 1, "passed": 1, "failed": 0, "skipped": 0},
                "testCases": [{"id": "test_shared", "status": "passed"}],
                "outputs": {"log": str(path), "logSha256": sha(path.read_bytes())}}

    obj.build_stage = stage
    return obj, env, calls


def test_frontend_only_reuses_verified_backends_and_runs_current_contracts(tmp_path):
    original, env, calls = setup_runner(tmp_path)
    original.run_tests(env)
    assert set(calls) == set(runner.TEST_STAGES)
    changed, _, calls = setup_runner(tmp_path, "second", "frontend", front="new-front")
    result = changed.run_tests(env)
    assert calls == {"frontend": 1, "frontend-checks": 1}
    assert set(result["reusedStages"]) == {"py311", "py312", "py313"}
    assert result["results"]["frontend-checks"]["ok"]
    # A same-candidate resume can reuse both the matrix and its checked web
    # contract even if the separate cache file was lost after the state save.
    for path in (changed.cache / "tests").glob("*.json"):
        path.unlink()
    changed.run_tests(env)
    assert calls == {"frontend": 1, "frontend-checks": 1}
    # Another web correction must never reuse the previous cross-end checks.
    changed.value["scope"].update(frontendFingerprint="third-front", frontendStageFingerprint="third-front-shared")
    changed.run_tests(env)
    assert calls == {"frontend": 2, "frontend-checks": 2}


@pytest.mark.parametrize("changed", ["frontend", "backend", "nodeKey", "nodeImage", "py312"])
def test_cross_end_check_key_binds_both_sources_and_its_runtime(tmp_path, changed):
    obj, env, _ = setup_runner(tmp_path)
    before = obj.test_cache_key("frontend-checks", env)
    assert before["source"]["checks"] == list(scope.FRONTEND_CHECKS)
    if changed == "frontend":
        obj.value["scope"]["frontendFingerprint"] = "new-web"
    elif changed == "backend":
        obj.value["scope"]["backendStageFingerprint"] = "new-backend-or-tests"
    elif changed == "py312":
        env["pythonImages"]["312"] = "new-python"
    else:
        env[changed] = "new-node-environment"
    assert obj.test_cache_key("frontend-checks", env) != before
    if changed == "frontend":
        assert obj.test_cache_key("py311", env)["source"] == "backshared"


def test_python312_environment_change_reexecutes_contracts_but_not_other_pythons(tmp_path):
    original, env, _ = setup_runner(tmp_path)
    original.run_tests(env)
    changed, _, calls = setup_runner(tmp_path, "second", "frontend", front="new-front")
    changed.run_tests(env)
    env["pythonImages"]["312"] = "corrected-py312"
    changed.run_tests(env)
    assert calls == {"frontend": 1, "frontend-checks": 2, "py312": 1}


def test_backend_only_reuses_frontend_but_shared_or_unknown_does_not(tmp_path):
    original, env, _ = setup_runner(tmp_path)
    original.run_tests(env)
    changed, _, calls = setup_runner(tmp_path, "second", "backend", back="new-back")
    result = changed.run_tests(env)
    assert set(calls) == {"py311", "py312", "py313"}
    assert result["reusedStages"] == ["frontend"]
    again, _, repeated = setup_runner(tmp_path, "repeat", "both", back="another-back")
    reused = again.run_tests(env)["results"]["frontend"]
    assert repeated["frontend"] == 0
    assert reused["evidenceReuse"]["runId"] == "first"
    assert reused["outputs"]["log"] == str(original.directory / "frontend.log")
    shared, _, calls = setup_runner(tmp_path, "third", "both", shared="shared-change")
    shared.run_tests(env)
    assert "frontend" in calls


@pytest.mark.parametrize("damage", ["missing", "modified", "failed", "runtime"])
def test_invalid_or_absent_evidence_runs_tests_instead_of_skipping(tmp_path, damage):
    original, env, _ = setup_runner(tmp_path)
    original.run_tests(env)
    if damage == "missing":
        (original.directory / "py311.log").unlink()
    elif damage == "modified":
        (original.directory / "py311.log").write_text("tampered")
    elif damage == "failed":
        for path in (original.cache / "tests").glob("*.json"):
            value = load(path)
            if value["key"]["stage"] == "py311":
                value["result"]["ok"] = False
                atomic_json(path, value)
    else:
        env = copy.deepcopy(env)
        env["pythonImages"]["311"] = "new-runtime"
    changed, _, calls = setup_runner(tmp_path, "second", "metadata")
    changed.run_tests(env)
    assert calls["py311"] == 1
    assert calls["py312"] == calls["py313"] == 0


def test_build_starts_after_frontend_test_while_python_jobs_are_running(tmp_path):
    obj, env, _ = setup_runner(tmp_path)
    python_started = threading.Event()
    build_started = threading.Event()
    front_passed = threading.Event()

    def stage(name):
        if name.startswith("py"):
            python_started.set()
            assert build_started.wait(5), "build wrongly waits for Python tests"
        elif name == "frontend":
            assert python_started.wait(5)
            front_passed.set()
        return {"ok": True, "testCases": [{"id": "test_shared", "status": "passed"}]}

    def build(_env):
        assert front_passed.is_set()
        build_started.set()
        return {"ok": True, "compiled": True}

    obj.build_stage, obj.build_once = stage, build
    result, built = obj.run_tests(env, with_build=True)
    assert result["ok"] and built["compiled"]


def test_frontend_failure_never_starts_build_and_backend_failure_is_not_hidden(tmp_path):
    for broken in ("frontend", "py312"):
        obj, env, _ = setup_runner(tmp_path, broken)
        built = []
        def stage(name):
            if name == broken:
                raise ReleaseError("intentional " + broken)
            return {"ok": True, "testCases": [{"id": "test_shared", "status": "passed"}]}
        obj.build_stage = stage
        obj.build_once = lambda _env: built.append(True) or {"ok": True}
        with pytest.raises(ReleaseError, match="intentional"):
            obj.run_tests(env, with_build=True)
        if broken == "frontend":
            assert not built


def test_acceptance_jobs_are_independent_and_joined_on_failure(tmp_path):
    obj, _, _ = setup_runner(tmp_path)
    barrier = threading.Barrier(3)
    completed = []
    def work(name):
        barrier.wait(timeout=5)
        completed.append(name)
        if name == "B":
            raise ReleaseError("B failed")
        return {"ok": True}
    with pytest.raises(ReleaseError, match="B failed"):
        obj.parallel({name: lambda name=name: work(name) for name in ("A", "B", "smoke")})
    assert set(completed) == {"A", "B", "smoke"}
