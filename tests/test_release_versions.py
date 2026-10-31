"""Version rules in the release, build and update scripts: what may be published, in which
order, as which kind of GitHub release, and the local development number around it.

Everything external is a stand-in: temporary Git repositories, fake GitHub, fake Go and
containers. Nothing is pushed, tagged remotely or written to GitHub.
"""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_node  # noqa: E402
import release  # noqa: E402
import release_local  # noqa: E402
import release_validation  # noqa: E402
from release_support import acceptance, candidate, node_compat, node_release, versions  # noqa: E402
from release_support.common import Commands, ReleaseError, State, load  # noqa: E402
from release_support.publication import publish  # noqa: E402
from release_support.runner import Runner  # noqa: E402

from app import versioning  # noqa: E402
from app.update import service as service_module  # noqa: E402
from app.update.loader import load_updater  # noqa: E402
from app.update.service import UpdateService, read_json  # noqa: E402
from tests.test_bump_version import version_tree  # noqa: E402,F401
from tests.test_release_entrypoint import git_repo  # noqa: E402,F401
from tests.test_release_node import (  # noqa: E402
    FEATURES,
    FakeGit,
    NodeGitHub,
    build_runner,
    fake_container,
    git,
    node_source,
    protocol_go,
    publication,  # noqa: F401
    put,
    repo,  # noqa: F401
)

TOKEN = "only-in-memory-fixture"
IMAGE = "sha256:" + "a" * 64


# ---------------------------------------------------------------- the rules release.py applies

@pytest.mark.parametrize("value", ["1.2.0", "1.2.0-rc.1", "1.2.0-beta.3"])
def test_only_beta_rc_and_release_versions_are_targets(value):
    assert versions.target(value) == value


@pytest.mark.parametrize("value, message", [
    ("1.2.0-dev.1", "development version"), ("v1.2.0", "X.Y.Z"), ("1.2", "X.Y.Z"), ("1.2.0-alpha.1", "X.Y.Z"),
    ("1.2.0-rc1", "X.Y.Z"), ("01.2.0", "X.Y.Z"), (" 1.2.0", "X.Y.Z"), (None, "X.Y.Z"),
])
def test_development_and_malformed_versions_are_never_targets(value, message):
    with pytest.raises(ReleaseError, match=message):
        versions.target(value)


def test_beta_and_rc_are_github_prereleases():
    assert versions.prerelease("1.2.0-beta.1") and versions.prerelease("1.2.0-rc.2")
    assert not versions.prerelease("1.2.0")


def test_published_versions_follow_the_project_order_not_semver():
    tags = ["v1.0.1-dev.9", "v1.0.1-beta.1", "v1.0.1", "v1.0.1-rc.1", "v1.0.1-beta.2", "v1.0.1-beta.10", "vbad",
            "node-v9.0.0", "v1.0.0"]
    github = SimpleNamespace(api=lambda path: [{"ref": "refs/tags/" + t} for t in tags if t.startswith(path.rsplit("/", 1)[1].partition("?")[0])])
    assert versions.published(github, "v") == ["1.0.0", "1.0.1-beta.1", "1.0.1-beta.2", "1.0.1-beta.10", "1.0.1-rc.1", "1.0.1"]
    assert versions.published(github, "node-v") == ["9.0.0"]
    # Standard semver would put dev after beta; the project order puts it first.
    assert versioning.compare("1.0.1-dev.9", "1.0.1-beta.1") < 0


def test_changelog_of_a_release_starts_at_the_previous_release():
    published = ["1.1.0", "1.2.0-beta.1", "1.2.0-rc.1"]
    assert versions.changelog_base("1.2.0", published) == "1.1.0"
    assert versions.changelog_base("1.2.0-rc.2", published) == "1.2.0-rc.1"
    assert versions.changelog_base("1.0.0", []) is None


def test_run_ids_carry_prerelease_versions():
    assert versions.RUN_ID_RE.fullmatch("1.2.0-beta.1-0123456789ab")
    assert node_release.RUN_ID_RE.fullmatch("node-1.2.0-rc.3-0123456789ab")
    assert not versions.RUN_ID_RE.fullmatch("1.2.0-alpha.1-0123456789ab")


def test_runs_of_a_version_are_matched_exactly(tmp_path):
    for name in ("1.2.0-0123456789ab", "1.2.0-beta.1-0123456789ab", "node-1.2.0-rc.1-0123456789ab"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "state.json").write_text("{}")
    assert release.existing_runs(tmp_path, "1.2.0") == ["1.2.0-0123456789ab"]
    assert release.existing_runs(tmp_path, "1.2.0-beta.1") == ["1.2.0-beta.1-0123456789ab"]
    assert release.existing_runs(tmp_path, "node-1.2.0") == []
    assert release_local._run_of("1.2.0-beta.1-0123456789ab", "1.2.0-beta.1")
    assert not release_local._run_of("1.2.0-beta.1-0123456789ab", "1.2.0")


@pytest.mark.parametrize("value, ok", [("1.2.0", True), ("1.2.0-beta.1", True), ("1.2.0-rc.4", True),
                                       ("1.2.0-dev.1", False), ("1.2", False), ("1.2.0-alpha.1", False)])
def test_local_adapter_and_acceptance_take_published_versions(value, ok):
    assert bool(release_local._VERSION.fullmatch(value)) is ok
    assert acceptance.published_version(value) is ok


def test_cli_refuses_a_development_controller_version(tmp_path, capsys):
    notes = tmp_path / "notes.md"
    notes.write_text("notes\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app/__init__.py").write_text('__version__ = "1.3.0-dev.4"\n')
    code = release.main(["--repo", str(tmp_path), "start", "1.3.0-dev.5", "--notes", str(notes), "--files", "app/x.py"])
    assert code == 1
    assert "development version" in json.loads(capsys.readouterr().err)["error"]
    assert not list((tmp_path / ".release-runs").glob("1.3.0*"))


# ---------------------------------------------------------------- GitHub release kind

def test_node_beta_is_a_prerelease_and_never_latest(publication):  # noqa: F811
    directory, state, notes, assets = publication
    github = NodeGitHub()
    result = publish(directory, state, FakeGit(github, notes), github, tag="node-v1.0.1", name="OpenBear Node 1.0.1",
                     assets=assets, latest=False, prerelease=True)
    assert result["ok"] and result["prerelease"] is True and result["latest"] is False
    assert github.posts[0]["prerelease"] is True and github.posts[0]["make_latest"] == "false"
    assert github.patches == [{"draft": False, "prerelease": True, "make_latest": "false"}]
    assert github.latest_id == 1


def test_controller_beta_is_a_prerelease_and_not_latest(publication):  # noqa: F811
    directory, state, notes, assets = publication
    github = NodeGitHub()
    result = publish(directory, state, FakeGit(github, notes), github, tag="v1.0.1-beta.1", name="OpenBear v1.0.1-beta.1",
                     assets=assets, latest=True, prerelease=True)
    assert result["prerelease"] is True and result["latest"] is False
    assert github.posts[0]["prerelease"] is True and github.posts[0]["make_latest"] == "false"
    assert github.patches == [{"draft": False, "prerelease": True, "make_latest": "false"}]
    assert github.latest_id == 1  # the previous release stays what default users get


def test_controller_release_is_a_normal_latest_release(publication):  # noqa: F811
    directory, state, notes, assets = publication
    github = NodeGitHub()
    result = publish(directory, state, FakeGit(github, notes), github, tag="v1.0.1", name="OpenBear v1.0.1",
                     assets=assets, latest=True, prerelease=False)
    assert result["latest"] is True and result["prerelease"] is False
    assert github.posts[0]["prerelease"] is False and "make_latest" not in github.posts[0]
    assert github.patches == [{"draft": False, "prerelease": False, "make_latest": "true"}]
    assert github.latest_id == 12


# ---------------------------------------------------------------- after publication: next minor's dev.1

def released(root, version):
    """The release commit a controller run leaves before publishing."""
    subprocess.run([sys.executable, "-B", "scripts/bump_version.py", version], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qam", "release: prepare " + version], cwd=root, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def development_state(root, version, name):
    directory = root / ".release-runs" / name
    directory.mkdir(parents=True)
    return State(directory, {"version": version, "python": sys.executable, "stages": {}}), Commands(directory)


def head(root, fmt="%H"):
    return subprocess.check_output(["git", "log", "-1", "--format=" + fmt], cwd=root, text=True).strip()


@pytest.mark.parametrize("published, following", [("1.1.0", "1.2.0-dev.1"), ("1.2.0-beta.1", "1.3.0-dev.1"),
                                                  ("1.2.0-rc.2", "1.3.0-dev.1")])
def test_controller_moves_to_the_next_minor_dev_in_one_local_commit(git_repo, published, following):  # noqa: F811
    release_commit = released(git_repo, published)
    (git_repo / "app/main.py").write_text("VALUE = 'unrelated work'\n")
    subprocess.run(["git", "add", "app/main.py"], cwd=git_repo, check=True)
    state, command = development_state(git_repo, published, published + "-0123456789ab")
    result = candidate.start_development(git_repo, state, command)
    assert result == {"version": following, "commit": head(git_repo)}
    assert head(git_repo, "%s") == f"release: start {following} development"
    assert head(git_repo, "%P") == release_commit
    changed = subprocess.check_output(["git", "show", "--name-only", "--format=", "HEAD"], cwd=git_repo, text=True).split()
    assert sorted(changed) == sorted(candidate.VERSION_FILES)
    assert candidate.version(git_repo) == following
    assert f'version = "{versioning.pep440(following)}"' in (git_repo / "uv.lock").read_text()
    subprocess.run([sys.executable, "-B", "scripts/bump_version.py", "--check", "--expect", following], cwd=git_repo, check=True)
    # Someone's staged work is neither committed nor dropped.
    assert subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=git_repo, text=True).split() == ["app/main.py"]
    # Repeating it (a resumed run) changes nothing.
    assert candidate.start_development(git_repo, State(state.directory, load(state.directory / "state.json")), command) == result
    assert head(git_repo) == result["commit"]


def test_a_commit_whose_outcome_was_lost_is_recognised_not_repeated(git_repo):  # noqa: F811
    release_commit = released(git_repo, "1.1.0")
    state, command = development_state(git_repo, "1.1.0", "1.1.0-0123456789ab")
    done = candidate.start_development(git_repo, state, command)
    lost = load(state.directory / "state.json")
    lost.pop("development")
    lost["developmentIntent"] = {"parent": release_commit, "version": "1.2.0-dev.1"}
    again = candidate.start_development(git_repo, State(state.directory, lost), command)
    assert again == done and head(git_repo) == done["commit"]
    assert subprocess.check_output(["git", "rev-list", "--count", f"{release_commit}..HEAD"], cwd=git_repo, text=True).strip() == "1"


def test_an_interrupted_write_is_finished_from_its_intent(git_repo):  # noqa: F811
    release_commit = released(git_repo, "1.1.0")
    subprocess.run([sys.executable, "-B", "scripts/bump_version.py", "1.2.0-dev.1"], cwd=git_repo, check=True, capture_output=True)
    state, command = development_state(git_repo, "1.1.0", "1.1.0-0123456789ab")
    state.value["developmentIntent"] = {"parent": release_commit, "version": "1.2.0-dev.1"}
    result = candidate.start_development(git_repo, state, command)
    assert result["version"] == "1.2.0-dev.1" and head(git_repo, "%P") == release_commit


@pytest.mark.parametrize("change, message", [
    ("version", "local version is now 9.9.9"), ("dirty", "uncommitted changes"), ("moved", "HEAD moved"),
])
def test_development_version_is_never_forced_over_local_changes(git_repo, change, message):  # noqa: F811
    release_commit = released(git_repo, "1.1.0")
    state, command = development_state(git_repo, "1.1.0", "1.1.0-0123456789ab")
    if change == "version":
        released(git_repo, "9.9.9")
    elif change == "dirty":
        (git_repo / "pyproject.toml").write_text((git_repo / "pyproject.toml").read_text() + "# edit\n")
    else:
        state.value["developmentIntent"] = {"parent": release_commit, "version": "1.2.0-dev.1"}
        (git_repo / "app/main.py").write_text("VALUE = 3\n")
        subprocess.run(["git", "commit", "-qam", "other work"], cwd=git_repo, check=True)
    before = head(git_repo)
    with pytest.raises(ReleaseError, match=message) as error:
        candidate.start_development(git_repo, state, command)
    assert error.value.code == 2 and head(git_repo) == before


def test_node_moves_to_the_next_minor_dev_in_one_local_commit(repo):  # noqa: F811
    state, command = development_state(repo, "1.0.0", "node-1.0.0-0123456789ab")
    put(repo / "app/main.py", "unrelated\n")
    result = candidate.start_development(repo, state, command, node_release.VERSIONING)
    assert result["version"] == "1.1.0-dev.1"
    assert (repo / "node/VERSION").read_text() == "1.1.0-dev.1\n"
    assert head(repo, "%s") == "release: start node 1.1.0-dev.1 development"
    assert subprocess.check_output(["git", "show", "--name-only", "--format=", "HEAD"], cwd=repo, text=True).split() == ["node/VERSION"]
    assert candidate.version(repo) == "0.13.1"  # the controller line is untouched


@pytest.mark.parametrize("meanwhile, ok", [("1.0.0-dev.7", True), ("1.0.3", False)])
def test_development_builds_before_the_release_commit_do_not_stop_it(repo, meanwhile, ok):  # noqa: F811
    put(repo / "node/VERSION", "1.0.0-dev.1\n")
    git(repo, "commit", "-qam", "development")
    directory = repo / ".release-runs/node-1.0.1-0123456789ab"
    directory.mkdir(parents=True)
    state = State(directory, {"version": "1.0.1", "oldVersion": "1.0.0-dev.1", "files": [], "python": sys.executable, "stages": {}})
    put(repo / "node/VERSION", meanwhile + "\n")  # e.g. build_node.py ran after start
    if ok:
        candidate.freeze(repo, directory, state, Commands(directory), node_release.VERSIONING)
        assert (repo / "node/VERSION").read_text() == "1.0.1\n" and head(repo, "%s") == "release: prepare node 1.0.1"
    else:
        with pytest.raises(ReleaseError, match="changed outside this run"):
            candidate.freeze(repo, directory, state, Commands(directory), node_release.VERSIONING)


def node_run(tmp_path, monkeypatch, version, developments):
    runner, _ = build_runner(tmp_path, version=version)
    directory, value = runner.directory, runner.value
    value.update(publicBase="base", runId=directory.name, startsDevelopment=True)
    calls = Counter()
    for name, result in (("freeze", value["candidate"]), ("source_matches", True), ("assert_local", None)):
        monkeypatch.setattr(candidate, name, lambda *args, _r=result: _r)
    monkeypatch.setattr(candidate, "export", lambda *args, **kwargs: {"ok": True})

    def start_development(repo_, state, command, versioning_):
        calls["develop"] += 1
        assert versioning_ is node_release.VERSIONING and state.value["published"]
        outcome = developments.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        state.value["development"] = outcome
        return outcome

    monkeypatch.setattr(candidate, "start_development", start_development)

    class Run(node_release.NodeRunner):
        def preflight(self, approved=False):
            calls["preflight"] += 1

        def deps(self):
            return {"ok": True, "key": "deps", "dir": str(tmp_path / "deps")}

        def deps_valid(self, result):
            return True

        def deps_key(self):
            return "deps"

        def test(self, deps):
            return {"ok": True}

    monkeypatch.setattr(node_release.NodeRunner, "contain", fake_container())

    def publisher(directory_, state, command, github, **kwargs):
        calls["publish"] += 1
        calls["prerelease" if kwargs["prerelease"] else "release"] += 1
        assert kwargs["latest"] is False
        return {"ok": True, "url": "https://example.invalid/" + kwargs["tag"], "latest": False, "prerelease": kwargs["prerelease"]}

    monkeypatch.setattr(node_release, "publish", publisher)
    make = lambda: Run(tmp_path, directory, load(directory / "state.json") if (directory / "state.json").exists() else value,  # noqa: E731
                       github=SimpleNamespace(token=TOKEN), command=FakeGit(None, ""))
    return make, calls, directory


def test_node_run_publishes_a_beta_as_prerelease_then_starts_development(tmp_path, monkeypatch):
    make, calls, directory = node_run(tmp_path, monkeypatch, "1.2.0-beta.1", [{"version": "1.3.0-dev.1", "commit": "c"}])
    make().run()
    assert calls["publish"] == calls["prerelease"] == 1 and calls["develop"] == 1
    saved = load(directory / "state.json")
    assert saved["completed"] and saved["development"] == {"version": "1.3.0-dev.1", "commit": "c"}
    assert load(directory / "report.json")["development"]["version"] == "1.3.0-dev.1"
    make().run()
    assert calls["publish"] == 1 and calls["develop"] == 1 and calls["preflight"] == 1


def test_development_failure_after_publication_resumes_without_republishing(tmp_path, monkeypatch):
    make, calls, directory = node_run(tmp_path, monkeypatch, "1.2.0",
                                      [ReleaseError("Published; version files have uncommitted changes", code=2),
                                       {"version": "1.3.0-dev.1", "commit": "c"}])
    with pytest.raises(ReleaseError, match="uncommitted") as error:
        make().run()
    assert error.value.code == 2
    saved = load(directory / "state.json")
    assert saved["published"] and saved["lastError"]["published"] is True
    assert saved["lastError"]["resume"].endswith("scripts/release.py node resume " + directory.name)
    make().run()
    assert calls["publish"] == calls["release"] == 1 and calls["develop"] == 2
    assert "lastError" not in load(directory / "state.json")


def test_runs_started_before_the_rule_finish_as_they_were(tmp_path, monkeypatch):
    make, calls, directory = node_run(tmp_path, monkeypatch, "1.0.1", [])
    runner = make()
    runner.value.pop("startsDevelopment")
    runner.value.update(published=True, completed=True)
    runner.state.save()
    make().run()
    assert calls["develop"] == 0 and calls["publish"] == 0


# ---------------------------------------------------------------- controller preflight

class ControllerHub:
    token = TOKEN

    def __init__(self, tags=("v1.1.0",), latest="v1.1.0", nodes=()):
        self.tags, self.latest = list(tags), latest
        self.rows, self.files = [], {}
        for index, (tag, manifest) in enumerate(nodes):
            url = f"https://api.github.com/repos/fixture/openbear/releases/assets/{index}"
            self.files[url] = json.dumps(manifest).encode()
            self.rows.append({"tag_name": tag, "draft": False, "assets": [{"name": "manifest.json", "url": url}]})
        self.writes, self.by_tag, self.ref_queries = [], [], []

    def api(self, path, method="GET", data=None, missing=False):
        if method != "GET":
            self.writes.append(path)
            raise AssertionError("preflight never writes to GitHub")
        if path == "":
            return {"permissions": {"push": True}}
        if path.startswith("/releases/tags/"):
            self.by_tag.append(path.removeprefix("/releases/tags/"))
            return next((row for row in self.rows if row["tag_name"] == path.removeprefix("/releases/tags/")), None)
        if path.startswith("/git/matching-refs/tags/"):
            # Like GitHub: every matching ref at once, whatever per_page and page say.
            prefix, _, query = path.removeprefix("/git/matching-refs/tags/").partition("?")
            self.ref_queries.append(query)
            tags = [*self.tags, *(row["tag_name"] for row in self.rows)]
            return [{"ref": "refs/tags/" + tag} for tag in dict.fromkeys(tags) if tag.startswith(prefix)]
        if path.startswith("/git/ref/tags/"):
            tag = path.removeprefix("/git/ref/tags/")
            return {"object": {"type": "commit", "sha": "commit-" + tag}} if tag in self.tags else None
        if path == "/releases/latest":
            return {"tag_name": self.latest, "draft": False, "prerelease": False, "assets": []}
        if path == "/git/ref/heads/main":
            return {"object": {"sha": "base"}}
        raise AssertionError(path)

    def download_asset(self, asset):
        return self.files[asset["url"]]


class Docker:
    def __call__(self, args, label, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": IMAGE}])
        return ""

    def recover_children(self):
        pass

    def cancel_all(self):
        pass

    def redact(self, value):
        return value


def controller_repo(tmp_path, required=("net",)):
    put(tmp_path / "app/servers/compat.py", (ROOT / "app/servers/compat.py").read_text())
    put(tmp_path / "app/servers/releases.py", f"REQUIRED_FEATURES = {tuple(required)!r}\n")
    return tmp_path


def controller_runner(tmp_path, github, version):
    directory = tmp_path / ".release-runs" / f"{version}-0123456789ab"
    directory.mkdir(parents=True)
    value = {"schema": 1, "runId": directory.name, "repository": "fixture/openbear", "version": version, "notes": "n\n",
             "python": sys.executable, "createdAt": 0, "stages": {}, "timeout": 60,
             "environment": {"pythonImages": {"py311": "python:3.11"}, "systemdImage": "systemd:fixture"}}
    return Runner(controller_repo(tmp_path), directory, value, github=github, command=Docker())


def node_manifest(version, protocol=1, features=("net",), protocol_min=None):
    value = {"schema": 1, "version": version, "protocol": protocol, "features": list(features), "binaries": {}}
    if protocol_min is not None:
        value["protocolMin"] = protocol_min
    return value


@pytest.mark.parametrize("version, message", [
    ("1.2.0-dev.3", "development version"),
    ("1.2.0-beta.1", r"must be newer than the latest published v1\.2\.0-beta\.2"),
    ("1.0.9", "must be newer than the latest published"), ("1.1.0", "already exists"),
])
def test_controller_target_must_be_publishable_and_newer_than_its_line(tmp_path, version, message):
    github = ControllerHub(tags=("v1.1.0", "v1.2.0-beta.2", "node-v5.0.0"))
    with pytest.raises(ReleaseError, match=message):
        controller_runner(tmp_path, github, version).preflight(approved=True)


def test_controller_beta_after_a_release_records_its_upgrade_base(tmp_path, capsys):
    github = ControllerHub(tags=("v1.1.0", "v1.2.0-beta.2"), nodes=[("node-v1.0.0", node_manifest("1.0.0"))])
    runner = controller_runner(tmp_path, github, "1.2.0-beta.3")
    runner.preflight(approved=True)
    # Upgrade acceptance starts from the latest release, what default users run.
    assert runner.value["previousVersion"] == "1.1.0" and runner.value["previousSourceCommit"] == "commit-v1.1.0"
    assert runner.value["notes"].endswith("compare/v1.2.0-beta.2...v1.2.0-beta.3\n")
    assert runner.value["nodeCompatibility"] == {"ok": True, "node": "1.0.0", "warning": "", "checked": []}
    assert "Warning" not in capsys.readouterr().out
    assert not github.writes


def test_controller_release_changelog_starts_at_the_previous_release(tmp_path):
    github = ControllerHub(tags=("v1.1.0", "v1.2.0-beta.2", "v1.2.0-rc.1"))
    runner = controller_runner(tmp_path, github, "1.2.0")
    runner.preflight(approved=True)
    assert runner.value["notes"].endswith("compare/v1.1.0...v1.2.0\n")


@pytest.mark.parametrize("nodes, why", [
    ([], ""),
    ([("node-v1.0.0", node_manifest("1.0.0", features=()))], "lacks net"),
    ([("node-v1.0.0", node_manifest("1.0.0", protocol=3, protocol_min=2))], "protocol 2..3"),
    ([("node-v1.1.0-beta.1", node_manifest("1.1.0-beta.1"))], ""),  # a release controller's users do not take a beta node
])
def test_no_compatible_node_release_only_warns(tmp_path, capsys, nodes, why):
    github = ControllerHub(nodes=nodes)
    runner = controller_runner(tmp_path, github, "1.2.0")
    runner.preflight(approved=True)
    found = runner.value["nodeCompatibility"]
    assert found["ok"] is False and found["node"] is None and "releasing anyway" in found["warning"]
    assert all(why in line for line in found["checked"]) and bool(found["checked"]) is bool(why)
    assert "Warning: No published node release works with this controller" in capsys.readouterr().out
    assert runner.value["previousSourceCommit"] and runner.value["images"]  # the preflight carried on


def test_prerelease_controller_takes_a_beta_node(tmp_path):
    github = ControllerHub(tags=("v1.1.0",), nodes=[("node-v1.1.0-beta.1", node_manifest("1.1.0-beta.1")),
                                                     ("node-v1.0.0", node_manifest("1.0.0", features=()))])
    runner = controller_runner(tmp_path, github, "1.2.0-rc.1")
    runner.preflight(approved=True)
    assert runner.value["nodeCompatibility"]["node"] == "1.1.0-beta.1"


def test_node_check_finds_node_releases_by_their_tags(tmp_path):
    # Hundreds of controller releases never hide a node release; a tag whose release
    # is still a draft (no release by tag) is passed over.
    github = ControllerHub(tags=[f"v1.{n}.0" for n in range(250)] + ["node-v1.2.0"],
                           nodes=[("node-v1.1.0", node_manifest("1.1.0")), ("node-v1.0.0", node_manifest("1.0.0"))])
    result = node_compat.check(controller_repo(tmp_path), github, False)
    assert result["ok"] and result["node"] == "1.1.0"
    assert result["checked"] == ["1.2.0: tag without a published release"]
    assert github.by_tag == ["node-v1.2.0", "node-v1.1.0"]


def test_published_versions_read_every_page(tmp_path):
    many = [f"v1.{n}.0" for n in range(250)]
    github = ControllerHub(tags=many)
    assert versions.published(github, "v")[-1] == "1.249.0" and len(versions.published(github, "v")) == 250
    # GitHub answers every matching ref at once: the repeated second page ends the walk.
    assert github.ref_queries[:2] == ["per_page=100&page=1", "per_page=100&page=2"] and len(github.ref_queries) == 4

    class Paged:  # a list GitHub does page
        def __init__(self):
            self.asked = []

        def api(self, path, **kwargs):
            page = int(path.rsplit("page=", 1)[1])
            self.asked.append(page)
            return [{"ref": f"refs/tags/v1.{n}.0"} for n in range(250)][(page - 1) * 100:page * 100]

    paged = Paged()
    assert len(versions.published(paged, "v")) == 250 and paged.asked == [1, 2, 3]

    class Endless:
        def api(self, path, **kwargs):
            page = int(path.rsplit("page=", 1)[1])
            return [{"ref": f"refs/tags/v{page}.{n}.0"} for n in range(100)]

    with pytest.raises(ReleaseError, match="more than"):
        versions.published(Endless(), "v")


def test_node_check_failures_never_stop_a_release(tmp_path):
    def broken(path, **kwargs):
        raise ReleaseError("GitHub GET /releases: HTTP 502; inspect then resume")

    result = node_compat.check(controller_repo(tmp_path), SimpleNamespace(api=broken), False)
    assert result["ok"] is False and "Could not check published node releases" in result["warning"]


@pytest.mark.parametrize("manifest, why", [
    (node_manifest("1.0.0"), ""), (node_manifest("1.0.0", protocol=2, protocol_min=1), ""),
    (node_manifest("1.0.0", protocol=3, protocol_min=2), "protocol 2..3"), (node_manifest("1.0.0", protocol=0), "no protocol"),
    ({**node_manifest("1.0.0"), "features": None}, "lists no features"), (node_manifest("1.0.0", features=("x",)), "lacks net"),
])
def test_node_compatibility_follows_the_controller_selection(manifest, why):
    reason = node_compat.problem(manifest, (1, 1), ("net",))
    assert (why in reason) and bool(reason) is bool(why)


def test_node_compatibility_reads_the_real_controller_requirements():
    protocol, features = node_compat.requirements(ROOT)
    from app.servers import compat, releases
    assert protocol == compat.PROTOCOL and features == releases.REQUIRED_FEATURES


# ---------------------------------------------------------------- build_node.py

def fake_go(tmp_path):
    script = tmp_path / "go"
    script.write_text("#!/bin/sh\n"
                      'if [ "$1" = version ]; then echo "go version go1.24.4 linux/amd64"; exit 0; fi\n'
                      'if [ "$1" = test ]; then exit 0; fi\n'
                      'while [ $# -gt 0 ]; do if [ "$1" = -o ]; then shift; printf "%s" "$GOOS-$GOARCH" > "$1"; fi; shift; done\n')
    script.chmod(0o755)
    return script


def node_tree(tmp_path, version):
    node_source(tmp_path, version, protocol=2, protocol_min=1)
    return tmp_path / "node"


def test_each_build_of_a_development_node_raises_its_number(tmp_path, capsys):
    real = (ROOT / "node/VERSION").read_bytes()
    source = node_tree(tmp_path / "src", "1.1.0-dev.4")
    for expected in ("1.1.0-dev.5", "1.1.0-dev.6"):
        build_node.main(["--source", str(source), "--output", str(tmp_path / "bin"), "--go", str(fake_go(tmp_path)),
                         "--target", "linux-amd64", "--skip-tests"])
        manifest = json.loads(capsys.readouterr().out)
        assert (source / "VERSION").read_text() == expected + "\n" and manifest["version"] == expected
    assert manifest == {"schema": 1, "version": "1.1.0-dev.6", "protocol": 2, "protocolMin": 1, "features": FEATURES,
                        "toolchain": "go version go1.24.4 linux/amd64",
                        "binaries": {"openbear-node-linux-amd64": {"sha256": hashlib.sha256(b"linux-amd64").hexdigest(), "bytes": 11}}}
    assert json.loads((tmp_path / "bin/manifest.json").read_text()) == manifest
    assert (ROOT / "node/VERSION").read_bytes() == real


@pytest.mark.parametrize("version", ["1.1.0", "1.1.0-beta.2", "1.1.0-rc.1"])
def test_release_builds_keep_their_version(tmp_path, capsys, version):
    source = node_tree(tmp_path / "src", version)
    build_node.main(["--source", str(source), "--output", str(tmp_path / "bin"), "--go", str(fake_go(tmp_path)),
                     "--target", "linux-amd64", "--skip-tests"])
    assert json.loads(capsys.readouterr().out)["version"] == version
    assert (source / "VERSION").read_text() == version + "\n"


def test_build_function_itself_never_changes_the_version(tmp_path):
    source = node_tree(tmp_path / "src", "1.1.0-dev.4")
    manifest = build_node.build(source, tmp_path / "bin", go=str(fake_go(tmp_path)), targets=["linux-amd64"], test=False)
    assert manifest["version"] == "1.1.0-dev.4" and (source / "VERSION").read_text() == "1.1.0-dev.4\n"


@pytest.mark.parametrize("text, protocol_min, features", [
    (protocol_go(2), 2, FEATURES),  # no ProtocolMin yet: it equals ProtocolVersion
    (protocol_go(3, 1, ["a", "b.c"]), 1, ["a", "b.c"]),
    (protocol_go(3, 1, []), 1, []),
])
def test_protocol_min_and_features_come_from_protocol_go(tmp_path, text, protocol_min, features):
    put(tmp_path / "protocol.go", text)
    assert build_node.protocol_min(tmp_path) == protocol_min and build_node.features(tmp_path) == features


@pytest.mark.parametrize("text, message", [
    (protocol_go(2, 3), "between 1 and ProtocolVersion"), (protocol_go(2, features=None), "no Features"),
    (protocol_go(2, features=["a", "a"]), "distinct"), ("const ProtocolVersion = 1\nvar Features = []string{x}\n", "distinct"),
])
def test_malformed_protocol_go_is_refused(tmp_path, text, message):
    put(tmp_path / "protocol.go", text)
    with pytest.raises(ValueError, match=message):
        build_node.protocol_min(tmp_path)
        build_node.features(tmp_path)


def test_real_protocol_go_parses():
    source = ROOT / "node"
    assert 1 <= build_node.protocol_min(source) <= build_node.protocol(source)
    assert build_node.features(source) and versioning.valid(build_node.version(source))


# ---------------------------------------------------------------- controller version files

@pytest.mark.parametrize("version", ["1.2.0", "1.2.0-beta.1", "1.2.0-rc.3", "1.2.0-dev.7"])
def test_uv_lock_holds_the_pep440_spelling(version_tree, version):  # noqa: F811
    run = lambda *args: subprocess.run([sys.executable, "-B", str(version_tree / "scripts/bump_version.py"), *args],  # noqa: E731
                                       capture_output=True, text=True)
    assert run(version).returncode == 0
    assert versioning.pep440(version) == str(Version(version))
    assert f'name = "openbear"\nversion = "{Version(version)}"' in (version_tree / "uv.lock").read_text()
    assert f'version = "{version}"' in (version_tree / "pyproject.toml").read_text()
    assert run("--check", "--expect", version).returncode == 0
    lock = version_tree / "uv.lock"
    lock.write_text(lock.read_text().replace(f'"{Version(version)}"', f'"{version}"', 1))
    assert (run("--check").returncode == 0) is (str(Version(version)) == version)
    assert not (version_tree / "app/__pycache__").exists()


def test_current_version_files_agree():
    assert subprocess.run([sys.executable, "-B", "scripts/bump_version.py", "--check"], cwd=ROOT, capture_output=True).returncode == 0
    assert versioning.valid(candidate.version(ROOT)) and versioning.valid(build_node.version())


@pytest.mark.parametrize("version", ["1.2.0", "1.2.0-beta.1", "1.2.0-rc.1", "1.2.0-dev.2"])
def test_version_consistency_accepts_release_candidates(version_tree, monkeypatch, version):
    subprocess.run([sys.executable, "-B", str(version_tree / "scripts/bump_version.py"), version], check=True, capture_output=True)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", version_tree)
    test_current_version_files_agree()


@pytest.mark.parametrize("lock, expected", [("1.2.0", "1.2.0"), ("1.2.0b1", "1.2.0-beta.1"), ("1.2.0rc2", "1.2.0-rc.2"),
                                            ("1.2.0.dev3", "1.2.0-dev.3"), ("v1.2.0", "1.2.0"), ("junk", "junk")])
def test_release_validation_reads_uv_spelling(lock, expected):
    assert release_validation.uv_lock_version(lock) == expected


def test_release_validation_accepts_a_beta_tree(tmp_path):
    put(tmp_path / "app/__init__.py", '__version__ = "1.2.0-beta.1"\n')
    put(tmp_path / "pyproject.toml", '[project]\nname = "openbear"\nversion = "1.2.0-beta.1"\n')
    put(tmp_path / "uv.lock", '[[package]]\nname = "openbear"\nversion = "1.2.0b1"\nsource = { virtual = "." }\n')
    assert set(release_validation._read_versions(tmp_path).values()) == {"1.2.0-beta.1"}


# ---------------------------------------------------------------- npm run build (prebuild)

BUMP = ROOT / "web/scripts/bump-dev-version.mjs"


def run_bump(root):
    env = {k: v for k, v in os.environ.items() if k != "OPENBEAR_SKIP_DEV_BUMP"}  # a plain development build
    return subprocess.run(["node", str(BUMP), str(root)], capture_output=True, text=True, env=env)


def bumped_tree(version_tree, version):  # noqa: F811
    subprocess.run([sys.executable, "-B", str(version_tree / "scripts/bump_version.py"), version], check=True, capture_output=True)
    return version_tree


needs_node = pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not available")


def test_frontend_build_runs_the_bump_first():
    package = json.loads((ROOT / "web/package.json").read_text())
    assert package["scripts"]["prebuild"] == "node scripts/bump-dev-version.mjs"
    assert package["scripts"]["build"].startswith("vite build")


@needs_node
def test_each_frontend_build_of_a_development_version_raises_its_number(version_tree):  # noqa: F811
    root = bumped_tree(version_tree, "1.2.0-dev.4")
    before = {p: (root / p).read_text() for p in ("app/__init__.py", "web/package-lock.json")}
    result = run_bump(root)
    assert result.returncode == 0 and "1.2.0-dev.4 -> 1.2.0-dev.5" in result.stdout
    check = subprocess.run([sys.executable, "-B", str(root / "scripts/bump_version.py"), "--check", "--expect", "1.2.0-dev.5"],
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    assert 'version = "1.2.0.dev5"' in (root / "uv.lock").read_text()
    # Only the version strings change; dependencies and other lines stay byte for byte.
    assert (root / "app/__init__.py").read_text() == before["app/__init__.py"].replace("1.2.0-dev.4", "1.2.0-dev.5")
    lock = json.loads(before["web/package-lock.json"])
    lock["version"] = lock["packages"][""]["version"] = "1.2.0-dev.5"
    assert json.loads((root / "web/package-lock.json").read_text()) == lock
    assert run_bump(root).returncode == 0 and candidate.version(root) == "1.2.0-dev.6"


@needs_node
@pytest.mark.parametrize("version", ["1.2.0", "1.2.0-beta.1", "1.2.0-rc.2"])
def test_frontend_builds_of_release_versions_keep_them(version_tree, version):  # noqa: F811
    root = bumped_tree(version_tree, version)
    files = {p: (root / p).read_bytes() for p in candidate.VERSION_FILES}
    assert run_bump(root).returncode == 0
    assert {p: (root / p).read_bytes() for p in candidate.VERSION_FILES} == files


@needs_node
def test_frontend_build_refuses_drifted_version_files(version_tree):  # noqa: F811
    root = bumped_tree(version_tree, "1.2.0-dev.4")
    (root / "pyproject.toml").write_text((root / "pyproject.toml").read_text().replace("1.2.0-dev.4", "1.2.0-dev.3"))
    files = {p: (root / p).read_bytes() for p in candidate.VERSION_FILES}
    result = run_bump(root)
    assert result.returncode == 1 and "pyproject.toml=1.2.0-dev.3" in result.stderr
    assert {p: (root / p).read_bytes() for p in candidate.VERSION_FILES} == files


@needs_node
def test_installing_from_source_keeps_the_number(version_tree):  # noqa: F811
    root = bumped_tree(version_tree, "1.2.0-dev.4")
    files = {p: (root / p).read_bytes() for p in candidate.VERSION_FILES}
    result = subprocess.run(["node", str(BUMP), str(root)], capture_output=True, text=True,
                            env={**os.environ, "OPENBEAR_SKIP_DEV_BUMP": "1"})
    assert result.returncode == 0 and "kept (OPENBEAR_SKIP_DEV_BUMP=1)" in result.stdout
    assert {p: (root / p).read_bytes() for p in candidate.VERSION_FILES} == files
    # Only exactly "1" skips; a plain build still counts.
    result = subprocess.run(["node", str(BUMP), str(root)], capture_output=True, text=True,
                            env={**os.environ, "OPENBEAR_SKIP_DEV_BUMP": "0"})
    assert result.returncode == 0 and candidate.version(root) == "1.2.0-dev.5"


def test_install_sh_builds_the_frontend_without_counting_a_build():
    script = (ROOT / "scripts/install.sh").read_text()
    builds = [line.strip() for line in script.splitlines() if "npm run build" in line and not line.strip().startswith("#")]
    assert builds == ["OPENBEAR_SKIP_DEV_BUMP=1 npm run build"]
    # The updater installs a release's prebuilt web/dist and never builds the frontend.
    assert "npm run" not in (ROOT / "scripts/updater.py").read_text() and "vite" not in (ROOT / "scripts/updater.py").read_text()


@needs_node
def test_a_partial_tree_builds_with_its_number(version_tree):  # noqa: F811
    root = bumped_tree(version_tree, "1.2.0-dev.4")
    (root / "uv.lock").unlink()  # like the Docker frontend stage, which copies only a few files
    result = run_bump(root)
    assert result.returncode == 0 and "kept" in result.stdout and candidate.version(root) == "1.2.0-dev.4"


# ---------------------------------------------------------------- the controller's updater

SAMPLE = ["0.9.9", "1.0.0-dev.1", "1.0.0-dev.2", "1.0.0-beta.1", "1.0.0-beta.2", "1.0.0-beta.10", "1.0.0-rc.1",
          "1.0.0", "v1.0.1-dev.9", "1.0.1-beta.1", "1.0.1-beta.2", "1.0.1-rc.1", "1.0.1", "1.1.0-dev.1", "10.0.0"]


def test_standalone_updater_orders_versions_exactly_like_app_versioning():
    updater = load_updater()
    for left, right in itertools.product(SAMPLE, repeat=2):
        assert updater.cmp_semver(left, right) == versioning.compare(left, right), (left, right)
        assert updater.is_newer(left, right) is versioning.newer(left, right), (left, right)
    for bad in ("1.0", "1.0.0-alpha.1", "1.0.0-rc1", "1.0.0-beta.0", "01.0.0", "1.0.0+x"):
        assert versioning.parse(bad) is None
        with pytest.raises(ValueError):
            updater.parse_semver(bad)


def test_standalone_updater_does_not_import_the_app():
    spec = importlib.util.spec_from_file_location("isolated_updater", ROOT / "scripts/updater.py")
    source = spec.loader.get_source("isolated_updater")
    assert "from app" not in source and "import app" not in source


def releases_row(tag, *, prerelease=None, draft=False):
    stage = versioning.parse(tag.rsplit("v", 1)[-1])
    return {"tag_name": tag, "name": tag, "body": "notes " + tag, "draft": draft, "assets": [],
            "prerelease": (stage is not None and stage.stage != "release") if prerelease is None else prerelease}


@pytest.fixture
async def updates(tmp_path, monkeypatch):
    config = SimpleNamespace(storage=SimpleNamespace(db_path=str(tmp_path / "data/openbear.db")),
                             telegram=SimpleNamespace(whitelist_ids=[]), web=SimpleNamespace(host="127.0.0.1", port=18961))
    rows = [releases_row(t) for t in ("v1.2.0", "v1.3.0-beta.1", "v1.2.1-beta.5", "node-v9.0.0", "node-v9.1.0-beta.1")]
    rows += [releases_row("v1.3.0-rc.1", draft=True), releases_row("v1.4.0-dev.1"), releases_row("vjunk")]
    seen = []

    def github(request):
        seen.append((request.url.path, request.url.query.decode(), request.headers.get("If-None-Match")))
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, headers={"ETag": '"latest"'}, json=rows[0])
        return httpx.Response(200, headers={"ETag": '"list"'}, json=rows)

    monkeypatch.setattr(service_module, "installed_version", lambda: "1.2.0")
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as http:
        def make():
            service = UpdateService(SimpleNamespace(config=config, bot=None))
            service._http = http
            return service

        yield SimpleNamespace(make=make, config=config, seen=seen, rows=rows)


async def test_default_users_take_only_releases_from_latest(updates):
    service = updates.make()
    await service._check_github()
    assert [path for path, *_ in updates.seen] == ["/repos/danger-dream/openbear/releases/latest"]
    assert read_json(service.state_path)["available"]["version"] == "1.2.0"
    assert service.snapshot()["updateAvailable"] is False


async def test_prerelease_users_take_the_newest_beta_or_rc(updates):
    updates.config.updates = SimpleNamespace(prerelease=True)
    service = updates.make()
    await service._check_github()
    path, query, _ = updates.seen[-1]
    assert path.endswith("/releases") and query == "per_page=100"
    available = read_json(service.state_path)["available"]
    # Not the draft rc, the dev tag, junk or node releases: the newest offered controller version.
    assert available["version"] == "1.3.0-beta.1" and available["tag"] == "v1.3.0-beta.1"
    assert service.snapshot()["updateAvailable"] is True


async def test_switching_the_setting_does_not_reuse_the_other_answer(updates):
    service = updates.make()
    await service._check_github()
    updates.config.updates = SimpleNamespace(prerelease=True)
    await service._check_github()
    await service._check_github()
    updates.config.updates = SimpleNamespace(prerelease=False)
    await service._check_github()
    assert [tag for *_, tag in updates.seen] == [None, None, '"list"', None]
    assert read_json(service.state_path)["available"]["version"] == "1.2.0"


async def test_a_prerelease_marked_latest_is_not_offered_by_default(updates):
    updates.rows[0] = releases_row("v1.2.0", prerelease=True)
    service = updates.make()
    await service._check_github()
    assert read_json(service.state_path)["available"] is None


async def test_no_offered_release_clears_the_offer(updates):
    updates.config.updates = SimpleNamespace(prerelease=True)
    updates.rows[:] = [releases_row("node-v1.0.0"), releases_row("v2.0.0-dev.1")]
    service = updates.make()
    await service._check_github()
    assert read_json(service.state_path)["available"] is None


@pytest.fixture
async def paged_updates(tmp_path, monkeypatch):
    """A release list of 250 entries, newest first: two full pages of node releases, then
    the controller's. GitHub pages /releases for real (per_page, page)."""
    config = SimpleNamespace(storage=SimpleNamespace(db_path=str(tmp_path / "data/openbear.db")),
                             telegram=SimpleNamespace(whitelist_ids=[]), web=SimpleNamespace(host="127.0.0.1", port=18961),
                             updates=SimpleNamespace(prerelease=True))
    rows = [releases_row(f"node-v1.{n}.0") for n in range(200, 0, -1)]
    rows += [releases_row("v1.5.0-rc.1"), releases_row("v1.4.0")] + [releases_row(f"v0.{n}.0") for n in range(48, 0, -1)]
    seen, failing = [], set()

    def github(request):
        query = dict(request.url.params)
        page = int(query.get("page", 1))
        seen.append((page, request.headers.get("If-None-Match")))
        if page in failing:
            return httpx.Response(403, json={"message": "rate limited"})
        if request.headers.get("If-None-Match") == f'"page{page}"':
            return httpx.Response(304)
        size = int(query["per_page"])
        return httpx.Response(200, headers={"ETag": f'"page{page}"'}, json=rows[(page - 1) * size:page * size])

    monkeypatch.setattr(service_module, "installed_version", lambda: "1.2.0")
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as http:
        def make():
            service = UpdateService(SimpleNamespace(config=config, bot=None))
            service._http = http
            return service

        yield SimpleNamespace(make=make, seen=seen, rows=rows, failing=failing)


async def test_prerelease_users_read_every_page_of_the_release_list(paged_updates):
    service = paged_updates.make()
    await service._check_github()
    assert [page for page, _ in paged_updates.seen] == [1, 2, 3]
    state = read_json(service.state_path)
    assert state["available"]["version"] == "1.5.0-rc.1" and state["etag"] == '"page1"'
    # An unchanged first page (304 for its ETag) leaves the answer; only page 1 is asked.
    paged_updates.seen.clear()
    await service._check_github()
    assert paged_updates.seen == [(1, '"page1"')]


async def test_a_failed_later_page_keeps_the_previous_answer(paged_updates):
    paged_updates.failing.add(3)
    service = paged_updates.make()
    await service._check_github()
    state = read_json(service.state_path)
    # A partial list (no controller release read yet) never replaces the answer, nor stores an ETag.
    assert "403" in state["lastError"] and not state.get("available") and not state.get("etag")
    paged_updates.failing.clear()
    paged_updates.seen.clear()
    await service._check_github()
    assert paged_updates.seen[0] == (1, None)
    assert read_json(service.state_path)["available"]["version"] == "1.5.0-rc.1"


def test_newest_offered_ignores_what_the_user_does_not_take():
    rows = [releases_row(t) for t in ("v1.0.0", "v1.1.0-rc.1", "node-v3.0.0")]
    assert service_module.newest_offered(rows, prerelease=False)["tag_name"] == "v1.0.0"
    assert service_module.newest_offered(rows, prerelease=True)["tag_name"] == "v1.1.0-rc.1"
    assert service_module.controller_version({"tag_name": "node-v3.0.0"}) == ""
    assert service_module.controller_version({"tag_name": "v1.1.0-rc.1"}) == "1.1.0-rc.1"
