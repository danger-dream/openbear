"""The controller is tested with real temporary Git/state and mocked externals."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from release_support import candidate, runner  # noqa: E402
from release_support.common import (  # noqa: E402
    Commands,
    ReleaseError,
    State,
    atomic_json,
    load,
    sha,
)
from release_support.github import SafeRedirect  # noqa: E402


def test_stage_receipts_reuse_only_identical_inputs_and_preserve_other_successes(tmp_path):
    state = State(tmp_path, {"stages": {}})
    calls = Counter()

    def work(stage):
        calls[stage] += 1
        return {"ok": True, "value": stage}

    state.execute("A", {"zip": "one"}, lambda: work("A"))
    with pytest.raises(ReleaseError):
        state.execute("B", {"zip": "one"}, lambda: (_ for _ in ()).throw(ReleaseError("broken B")))
    restored = State(tmp_path, load(tmp_path / "state.json"))
    restored.execute("A", {"zip": "one"}, lambda: work("A"))
    restored.execute("B", {"zip": "one"}, lambda: work("B"))
    assert calls == {"A": 1, "B": 1}
    restored.execute("A", {"zip": "two"}, lambda: work("A"))
    assert calls["A"] == 2
    assert restored.value["stages"]["B"]["status"] == "passed"


def test_command_children_do_not_inherit_tokens_or_leak_them_to_logs(tmp_path, monkeypatch):
    secret = "temporary-secret-for-command-test"
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    command = Commands(tmp_path, secret)
    output = command([sys.executable, "-c", "import os; print(os.getenv('GH_TOKEN'));print(os.getenv('GITHUB_TOKEN'));print('  leading-space')"], "test")
    assert output.splitlines() == ["None", "None", "  leading-space"]
    assert secret not in "".join(path.read_text() for path in (tmp_path / "logs").iterdir())
    assert command([sys.executable, "-c", "print(' M app/main.py')"], "status") == " M app/main.py"


def test_download_redirect_strips_auth_and_rejects_plaintext():
    import urllib.request
    redirect = SafeRedirect()
    request = urllib.request.Request("https://api.github.com/a", headers={"Authorization": "Bearer private"})
    redirected = redirect.redirect_request(request, None, 302, "", {}, "https://release-assets.githubusercontent.com/a")
    assert not redirected.has_header("Authorization")
    with pytest.raises(ReleaseError, match="non-HTTPS"):
        redirect.redirect_request(request, None, 302, "", {}, "http://example.com/a")


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repo, check=True)
    for rel, body in {
        "app/__init__.py": '__version__ = "1.0.0"\n', "app/main.py": "VALUE = 1\n",
        "pyproject.toml": '[project]\nname = "openbear"\nversion = "1.0.0"\n',
        "uv.lock": '[[package]]\nname = "openbear"\nversion = "1.0.0"\n',
        "web/package.json": '{"version":"1.0.0"}',
        "web/package-lock.json": '{"version":"1.0.0","packages":{"":{"version":"1.0.0"}}}',
        ".gitignore": ".release-runs/\n", "README.md": "fixture\n",
    }.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    (repo / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/bump_version.py", repo / "scripts/bump_version.py")
    shutil.copy2(ROOT / "app/versioning.py", repo / "app/versioning.py")  # bump_version.py's version rules
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "fixture base"], cwd=repo, check=True, capture_output=True)
    return repo


def new_state(repo):
    directory = repo / ".release-runs/1.0.1-123456789abc"
    directory.mkdir(parents=True)
    state = State(directory, {"version": "1.0.1", "oldVersion": "1.0.0", "files": ["app/main.py"],
                             "python": sys.executable, "stages": {}})
    return directory, state, Commands(directory)


def test_candidate_version_sync_happens_locally_before_export(git_repo):
    directory, state, command = new_state(git_repo)
    (git_repo / "app/main.py").write_text("VALUE = 2\n")
    first = candidate.freeze(git_repo, directory, state, command)
    assert candidate.version(git_repo) == "1.0.1"
    assert first["files"]["app/main.py"] == sha(b"VALUE = 2\n")
    assert candidate.freeze(git_repo, directory, state, command) == first
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=git_repo) == b""


def test_candidate_refuses_unreviewed_files_and_unrelated_staging(git_repo):
    directory, state, command = new_state(git_repo)
    (git_repo / "README.md").write_text("unreviewed")
    with pytest.raises(ReleaseError, match="Unreviewed"):
        candidate.freeze(git_repo, directory, state, command)
    assert candidate.version(git_repo) == "1.0.0"


def test_uncertain_local_commit_is_reconciled_without_second_commit(git_repo):
    directory, state, command = new_state(git_repo)
    (git_repo / "app/main.py").write_text("VALUE = 2\n")

    def lose_commit_response(args, label, **kwargs):
        result = command(args, label, **kwargs)
        if "commit" in args:
            raise ReleaseError("lost commit response")
        return result

    with pytest.raises(ReleaseError, match="lost commit response"):
        candidate.freeze(git_repo, directory, state, lose_commit_response)
    committed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()
    restored = State(directory, load(directory / "state.json"))
    result = candidate.freeze(git_repo, directory, restored, command)
    assert result["localCommit"] == committed
    assert "commitIntent" not in restored.value


def test_changed_candidate_invalidates_validation_but_preserves_old_evidence(git_repo):
    directory, state, command = new_state(git_repo)
    first = candidate.freeze(git_repo, directory, state, command)
    state.value["stages"]["py311"] = {"status": "passed"}
    (directory / "assets").mkdir()
    (directory / "assets/old.zip").write_bytes(b"original")
    (git_repo / "app/main.py").write_text("VALUE = 3\n")
    second = candidate.freeze(git_repo, directory, state, command)
    assert first["localCommit"] != second["localCommit"]
    assert state.value["stages"] == {"py311": {"status": "passed"}}  # rechecked by its stage-input key
    assert (directory / "superseded" / first["localCommit"] / "assets/old.zip").read_bytes() == b"original"


@pytest.mark.parametrize("path", ["data/openbear.db", "../secret", "/absolute", "web/node_modules/x", "web/dist/index.html", ".env", "app/__pycache__/x.pyc", "evals", "evals/replay.py", "evals/prompt_regression/runs/result.json"])
def test_runtime_and_private_paths_never_become_public_candidate(path):
    assert not candidate.valid_path(path)


@pytest.mark.parametrize("path", ["Dockerfile", ".dockerignore", "scripts/container-entrypoint.sh"])
def test_container_build_definitions_are_public_source(path):
    assert candidate.valid_path(path)


@pytest.mark.parametrize("path, public", [
    ("mobile/src/app/index.tsx", True), ("mobile/package.json", True), ("mobile/app.config.ts", True),
    ("mobile/node_modules/expo/index.js", False), ("mobile/src/__pycache__/x.pyc", False),
])
def test_native_client_source_is_public_but_not_its_dependencies(path, public):
    assert candidate.valid_path(path) is public


def _stage_local_only_removal(repo, name):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("local fixture\n")
    subprocess.run(["git", "add", "-f", "--", name], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "legacy local fixture"], cwd=repo, check=True, capture_output=True)
    with (repo / ".gitignore").open("a") as stream:
        stream.write("/evals/\n")
    subprocess.run(["git", "rm", "--cached", "--", name], cwd=repo, check=True, capture_output=True)
    return target


@pytest.mark.parametrize("name", ["evals/local.py", "evals/中文.py", "evals/line\nbreak.py"])
def test_candidate_accepts_only_reviewed_cached_local_removal(git_repo, name):
    target = _stage_local_only_removal(git_repo, name)
    directory, state, command = new_state(git_repo)
    state.value["files"] += [".gitignore", name]
    frozen = candidate.freeze(git_repo, directory, state, command)
    assert name not in frozen["files"]
    assert target.read_text() == "local fixture\n"
    assert not command(["git", "ls-files", "--", name], "check-removed", cwd=git_repo)
    assert candidate.freeze(git_repo, directory, state, command) == frozen
    candidate.assert_local(git_repo, state.value, command)
    (git_repo / "app/main.py").write_text("VALUE = 4\n")
    updated = candidate.freeze(git_repo, directory, state, command)
    assert updated["localCommit"] != frozen["localCommit"]
    assert target.read_text() == "local fixture\n"


def test_local_removal_commit_response_recovery_keeps_file(git_repo):
    name = "evals/local.py"
    target = _stage_local_only_removal(git_repo, name)
    directory, state, command = new_state(git_repo)
    state.value["files"] += [".gitignore", name]

    def lose_commit_response(args, label, **kwargs):
        result = command(args, label, **kwargs)
        if "commit" in args:
            raise ReleaseError("lost response")
        return result

    with pytest.raises(ReleaseError, match="lost response"):
        candidate.freeze(git_repo, directory, state, lose_commit_response)
    committed = command(["git", "rev-parse", "HEAD"], "head", cwd=git_repo)
    restored = State(directory, load(directory / "state.json"))
    frozen = candidate.freeze(git_repo, directory, restored, command)
    assert frozen["localCommit"] == committed
    assert name not in frozen["files"]
    assert target.read_text() == "local fixture\n"


def test_local_removal_must_be_reviewed_and_cannot_be_restaged(git_repo):
    name = "evals/local.py"
    _stage_local_only_removal(git_repo, name)
    directory, state, command = new_state(git_repo)
    state.value["files"] += [".gitignore"]
    with pytest.raises(ReleaseError, match="Unreviewed changes.*evals"):
        candidate.freeze(git_repo, directory, state, command)
    state.value["files"].append(name)
    command(["git", "add", "-f", "--", name], "restore-local", cwd=git_repo)
    with pytest.raises(ReleaseError, match="unsafe or non-public"):
        candidate.freeze(git_repo, directory, state, command)
    assert candidate.version(git_repo) == "1.0.0"


def test_frontend_backups_are_ignored_but_never_public(git_repo):
    shutil.copyfile(ROOT / ".gitignore", git_repo / ".gitignore")
    name = "web/.dist-before-fixture/index.html"
    target = git_repo / name
    target.parent.mkdir(parents=True)
    target.write_text("local build")
    subprocess.run(["git", "check-ignore", "--", name], cwd=git_repo, check=True, capture_output=True)
    assert not candidate.valid_path(name)
    directory, state, command = new_state(git_repo)
    state.value["files"] += [".gitignore"]
    assert name not in candidate.freeze(git_repo, directory, state, command)["files"]
    assert target.read_text() == "local build"


def test_candidate_preserves_public_special_filenames(git_repo):
    name = "docs/中文\nname.md"
    target = git_repo / name
    target.parent.mkdir()
    target.write_text("public fixture")
    directory, state, command = new_state(git_repo)
    state.value["files"].append(name)
    assert candidate.freeze(git_repo, directory, state, command)["files"][name] == sha(b"public fixture")


def test_candidate_reuse_rejects_legacy_evals_manifest(git_repo):
    directory, state, command = new_state(git_repo)
    frozen = candidate.freeze(git_repo, directory, state, command)
    frozen["files"]["evals/replay.py"] = sha(b"local fixture")
    with pytest.raises(ReleaseError, match="Frozen candidate.*evals"):
        candidate.freeze(git_repo, directory, state, command)


def test_candidate_reuse_rechecks_committed_public_tree(git_repo):
    directory, state, command = new_state(git_repo)
    frozen = candidate.freeze(git_repo, directory, state, command)
    (git_repo / "evals").mkdir()
    (git_repo / "evals/replay.py").write_text("local fixture")
    command(["git", "add", "evals"], "add-local-evals", cwd=git_repo)
    command(["git", "commit", "-m", "legacy fixture"], "legacy-commit", cwd=git_repo)
    frozen["localCommit"] = state.value["localCandidate"] = command(["git", "rev-parse", "HEAD"], "legacy-head", cwd=git_repo)
    # Even an incomplete saved manifest must not bypass today's tree policy.
    with pytest.raises(ReleaseError, match="Source is not approved.*evals"):
        candidate.freeze(git_repo, directory, state, command)


@pytest.mark.parametrize("operation", ["export", "assert_local"])
def test_legacy_evals_candidate_rejected_before_export_or_publication(tmp_path, operation):
    source = tmp_path / "source"
    source.mkdir()
    marker = source / "evidence.txt"
    marker.write_text("preserve old evidence")
    value = {"candidate": {"files": {"evals/replay.py": sha(b"local fixture")}}}

    def forbidden_command(*args, **kwargs):
        pytest.fail("legacy candidate rejection must precede commands or network access")

    with pytest.raises(ReleaseError, match="Frozen candidate.*evals"):
        if operation == "export":
            candidate.export(tmp_path, tmp_path, SimpleNamespace(value=value), forbidden_command)
        else:
            candidate.assert_local(tmp_path, value, forbidden_command)
    assert marker.read_text() == "preserve old evidence"


@pytest.mark.parametrize("extra", ["directory", "file", "broken-symlink"])
def test_cached_public_source_rejects_extra_evals(tmp_path, extra):
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("public")
    frozen = {"files": {"README.md": sha(b"public")}}
    assert candidate.source_matches(tmp_path, frozen)
    if extra == "directory":
        (tmp_path / "evals").mkdir()
    elif extra == "file":
        (tmp_path / "evals").write_text("local")
    else:
        (tmp_path / "evals").symlink_to(tmp_path / "missing")
    assert not candidate.source_matches(tmp_path, frozen)


def test_matrix_collection_and_skip_contract():
    good = {"ok": True, "testCases": [{"id": "tests.test_one::test_ok", "status": "passed"}]}
    values = {name: good for name in runner.TEST_STAGES}
    assert runner.matrix_contract(values)["ok"]
    values = {**values, "py313": {"ok": True, "testCases": [{"id": "unexpected", "status": "skipped", "skipReason": "not installed"}]}}
    with pytest.raises(ReleaseError, match="Unexpected conditional skip"):
        runner.matrix_contract(values)
    systemd = {"ok": True, "testCases": [{"id": "tests.test_systemd_watchdog_live::test_case", "status": "skipped",
                                          "skipReason": "set OPENBEAR_SYSTEMD_TEST=1 to test disposable systemd services"}]}
    assert runner.matrix_contract({name: systemd for name in runner.TEST_STAGES})["ok"]
    live = {"ok": True, "testCases": [{"id": f"tests.{module}::test_case", "status": "skipped", "skipReason": "需 --run-live"}
                                      for module in ("test_live_parrot", "test_live_read_images")]}
    assert runner.matrix_contract({name: live for name in runner.TEST_STAGES})["ok"]
    unlisted = {"ok": True, "testCases": [{"id": "tests.test_other::test_case", "status": "skipped", "skipReason": "需 --run-live"}]}
    with pytest.raises(ReleaseError, match="Unexpected conditional skip"):
        runner.matrix_contract({name: unlisted for name in runner.TEST_STAGES})
    node = {"ok": True, "testCases": [{"id": "tests.test_servers::test_node", "status": "skipped", "skipReason": "Go toolchain is not available"},
                                      {"id": "tests.test_node_browser::test_drive", "status": "skipped", "skipReason": "needs Chrome and Go"},
                                      {"id": "tests.test_browser_engine_parity::test_same", "status": "skipped", "skipReason": "needs google-chrome and Go"},
                                      {"id": "tests.test_gateway_update::test_reinstall", "status": "skipped", "skipReason": "needs the built gateway program"},
                                      {"id": "tests.test_servers_transfers::test_upload", "status": "skipped", "skipReason": "Go toolchain is not available"},
                                      {"id": "tests.test_instance_hardening::test_gateway", "status": "skipped", "skipReason": "gateway binary not built"},
                                      {"id": "tests.test_servers_hardening::test_ssh", "status": "skipped", "skipReason": "ssh-keygen is not available"}]}
    assert runner.matrix_contract({name: node for name in runner.TEST_STAGES})["ok"]
    benchmark = {"ok": True, "testCases": [{"id": "tests.test_servers_bench_live::test_real", "status": "skipped",
        "skipReason": "set OPENBEAR_BENCH_LIVE=1 to run a real benchmark"}]}
    assert runner.matrix_contract({name: benchmark for name in runner.TEST_STAGES})["ok"]
    for case in ({"id": "tests.test_servers_bench_live::test_real", "skipReason": "network failed"},
                 {"id": "tests.test_other::test_case", "skipReason": benchmark["testCases"][0]["skipReason"]},
                 {"id": "tests.test_other::test_case", "skipReason": systemd["testCases"][0]["skipReason"]},
                 {"id": "tests.test_systemd_watchdog_live::test_case", "skipReason": "not installed"},
                 {"id": "tests.test_other::test_case", "skipReason": "Go toolchain is not available"},
                 {"id": "tests.test_other::test_case", "skipReason": "needs the built gateway program"},
                 {"id": "tests.test_gateway_update::test_reinstall", "skipReason": "gateway unreachable"},
                 {"id": "tests.test_other::test_case", "skipReason": "ssh-keygen is not available"},
                 {"id": "tests.test_servers_hardening::test_ssh", "skipReason": "Go toolchain is not available"},
                 {"id": "tests.test_node_browser::test_case", "skipReason": "dropping to another account needs root"}):
        values = {name: {"ok": True, "testCases": [{**case, "status": "skipped"}]} for name in runner.TEST_STAGES}
        with pytest.raises(ReleaseError, match="Unexpected conditional skip"):
            runner.matrix_contract(values)


class MemoryCommands:
    def __call__(self, *_args, **_kwargs):
        return ""

    def cancel_all(self):
        pass

    def recover_children(self):
        pass

    def redact(self, value):
        return value


def passed_result(directory, name):
    log = directory / "results" / name / "attempts" / "fixture" / "stage.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("passed")
    return {"ok": True, "counts": {"tests": 1, "passed": 1, "failed": 0, "skipped": 0},
            "testCases": [{"id": "test_x", "status": "passed"}],
            "outputs": {"log": str(log), "logSha256": sha(log.read_bytes())}}


def test_whole_pipeline_B_failure_resumes_without_repeating_tests_build_or_A(tmp_path, monkeypatch):
    directory = tmp_path / "run"
    directory.mkdir()
    value = {"runId": "1.0.1-123456789abc", "version": "1.0.1", "previousVersion": "1.0.0",
             "repository": "fixture/repo", "createdAt": 0, "files": [], "python": sys.executable,
             "images": {"311": "p311", "312": "p312", "313": "p313", "systemd": "systemd"},
             "previous": {"id": 1}, "previousSourceCommit": "a" * 40, "stages": {}, "notes": "notes\n",
             "candidate": {"localCommit": "local", "sourceFingerprint": "one", "publicCommit": "public", "files": {"uv.lock": "lock", "web/package-lock.json": "node"}}}
    calls = Counter()

    def freeze(*_args):
        return value["candidate"]

    def export(*_args):
        calls["source"] += 1
        (directory / "source/.git").mkdir(parents=True)
        for rel, text in [("uv.lock", "lock"), ("web/package-lock.json", "node")]:
            path = directory / "source" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            value["candidate"]["files"][rel] = sha(text.encode())
        return {"ok": True}

    monkeypatch.setattr(candidate, "freeze", freeze)
    monkeypatch.setattr(candidate, "export", export)
    monkeypatch.setattr(candidate, "assert_local", lambda *_: None)
    monkeypatch.setattr(runner.build, "normalize_dependencies", lambda *_: {"fixture": "deps"})

    class TestRunner(runner.Runner):
        def preflight(self, approved):
            assert approved

        def classify_scope(self):
            return {"kind": "both", "frontendChanged": True, "backendChanged": True,
                    "frontendFingerprint": "front", "backendFingerprint": "back"}

        def previous_assets(self):
            calls["previous"] += 1
            p = directory / "previous-assets"
            p.mkdir(exist_ok=True)
            (p / "old.zip").write_bytes(b"old")
            return {"ok": True, "assets": {"old.zip": {"bytes": 3, "sha256": sha(b"old")}}}

        def build_stage(self, stage):
            calls[stage] += 1
            if stage == "env":
                atomic_json(directory / "environment.json", {"nodeImage": "node", "nodeKey": "node-deps", "pythonImages": {v: "py" + v for v in ("311", "312", "313")}})
            if stage == "build":
                (directory / "build/dist").mkdir(parents=True)
                (directory / "build/dist/index.html").write_text("built")
                result = passed_result(directory, stage)
                out = Path(result["outputs"]["log"]).parent
                shutil.copytree(directory / "build/dist", out / "dist")
                files = runner.build.tree_files(out / "dist")
                identity = runner.scope.build_identity("one", load(directory / "environment.json"), value["version"])
                manifest = {"identity": identity, "files": files}
                atomic_json(directory / "build/manifest.json", manifest)
                atomic_json(out / "manifest.json", manifest)
                result["outputs"].update(dist=str(out / "dist"), manifest=str(out / "manifest.json"),
                    distSha256=runner.build.digest_json(files), manifestSha256=sha((out / "manifest.json").read_bytes()))
                return result
            if stage == "package":
                (directory / "assets").mkdir()
                (directory / "assets/only.zip").write_bytes(b"zip")
                return {"ok": True, "assets": {"only.zip": {"bytes": 3, "sha256": sha(b"zip")}}}
            if stage in (*runner.TEST_STAGES, "frontend-checks"):
                return passed_result(directory, stage)
            return {"ok": True}

        def acceptance(self, case, approved):
            calls[case] += 1
            if case == "B" and calls[case] == 1:
                raise ReleaseError("B fixture failure")
            return {"ok": True}

    def publisher(*_, **__):
        calls["publish"] += 1
        return {"ok": True, "url": "https://example.invalid/release"}

    monkeypatch.setattr(runner, "publish", publisher)
    obj = TestRunner(tmp_path, directory, value, github=SimpleNamespace(token="fixture"), command=MemoryCommands())
    with pytest.raises(ReleaseError, match="B fixture failure"):
        obj.run(approved=True)
    assert calls["publish"] == 0
    resumed = TestRunner(tmp_path, directory, load(directory / "state.json"), github=SimpleNamespace(token="fixture"), command=MemoryCommands())
    resumed.run(approved=True)
    assert resumed.value["completed"]
    assert calls["B"] == 2 and calls["publish"] == 1
    for stage in (*runner.TEST_STAGES, "build", "package", "smoke", "A"):
        assert calls[stage] == 1, (stage, calls)


def test_upgrade_harness_changes_invalidate_acceptance_evidence(tmp_path):
    support = tmp_path / "support"
    helpers = support / "acceptance_helpers"
    helpers.mkdir(parents=True)
    (support / "acceptance.py").write_text("harness")
    helper = helpers / "seed_upgrade.py"
    helper.write_text("small-old-fixture")
    state = State(tmp_path / "run", {"stages": {}})
    calls = []

    def check():
        calls.append("verified")
        return {"ok": True}

    def run():
        return state.execute("acceptance-B", {"assets": "same-zip", "harness": runner.acceptance_fingerprint(support)}, check)

    run()
    run()
    assert calls == ["verified"]
    helper.write_text("large-old-fixture-with-readiness-check")
    run()
    assert calls == ["verified", "verified"]
    (helpers / "__pycache__").mkdir()
    (helpers / "__pycache__/seed_upgrade.pyc").write_bytes(b"not an input")
    run()
    assert calls == ["verified", "verified"]


def test_one_python_environment_correction_reuses_unaffected_stages(tmp_path):
    obj = runner.Runner(tmp_path, tmp_path / "run", {
        "repository": "fixture/repo", "candidate": {"sourceFingerprint": "same-source"}, "stages": {},
    }, github=SimpleNamespace(token="fixture"), command=MemoryCommands())
    calls = Counter()

    def stage(name):
        calls[name] += 1
        return passed_result(obj.directory, name)

    obj.build_stage = stage
    env = {"nodeImage": "node", "nodeKey": "deps", "pythonImages": {v: "py" + v for v in ("311", "312", "313")}, "systemdImage": "systemd"}
    obj.run_tests(env)
    build_identity = runner.execution_environment(env, "build")
    env["pythonImages"]["311"] = "corrected-py311"
    obj.run_tests(env)
    assert calls == {"py311": 2, "py312": 1, "py313": 1, "frontend": 1}
    assert runner.execution_environment(env, "build") == build_identity
    env["systemdImage"] = "corrected-systemd"
    obj.run_tests(env)
    assert calls == {"py311": 2, "py312": 1, "py313": 1, "frontend": 1}


def test_success_cleanup_removes_package_copies_but_keeps_evidence(tmp_path):
    directory = tmp_path / "run"
    attempt = directory / "results/package/attempts/owned"
    for name in ("package", "previous", "assets"):
        (attempt / name).mkdir(parents=True)
        (attempt / name / "file").write_text("temporary copy")
    (attempt / "result.json").write_text("evidence")
    obj = runner.Runner(tmp_path, directory, {"repository": "fixture/repo", "published": True},
                        github=SimpleNamespace(token="fixture"), command=MemoryCommands())
    obj.finish_cleanup()
    assert obj.value["completed"]
    assert sorted(p.name for p in attempt.iterdir()) == ["result.json"]
