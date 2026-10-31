"""Separate openbear-node release: real temporary Git/state, fake GitHub/remote/containers."""
from __future__ import annotations

import copy
import json
import struct
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release  # noqa: E402
from release_support import build, candidate, node_release  # noqa: E402
from release_support.acceptance_helpers import verify_package_extras  # noqa: E402
from release_support.common import (  # noqa: E402
    Commands,
    ReleaseError,
    State,
    load,
    sha,
)
from release_support.publication import publish  # noqa: E402

TOKEN = "only-in-memory-fixture"
IMAGE = "sha256:" + "a" * 64
EXPECTED_ASSETS = ["openbear-node-linux-amd64", "openbear-node-linux-arm64", "openbear-node-darwin-arm64",
                   "openbear-node-windows-amd64.exe", "manifest.json"]


def put(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


FEATURES = ["net.addrs", "net.probe"]


def protocol_go(protocol=3, protocol_min=None, features=FEATURES):
    text = f'package node\n\nvar Version = "dev"\n\nconst ProtocolVersion = {protocol}\n'
    if protocol_min is not None:
        text += f"\nconst ProtocolMin = {protocol_min}\n"
    if features is not None:
        text += "\nvar Features = []string{" + ", ".join(f'"{name}"' for name in features) + "}\n"
    return text


def node_source(root: Path, version="1.0.0", protocol=3, protocol_min=None):
    put(root / "node/go.mod", "module openbear/node\n\ngo 1.24.0\n")
    put(root / "node/go.sum", "golang.org/x/sys v0.38.0 h1:fixture\n")
    put(root / "node/VERSION", version + "\n")
    put(root / "node/protocol.go", protocol_go(protocol, protocol_min))
    put(root / "node/node.go", "package node\n")
    put(root / "node/cmd/openbear-node/main.go", "package main\n")
    put(root / "node/browser/resources/bridge.js", "// embedded\n")
    return root


# ---------------------------------------------------------------- formats

def test_asset_names_are_exactly_the_five_published_files():
    assert list(node_release.ASSETS) == EXPECTED_ASSETS
    assert node_release.TAG_PREFIX + "1.0.0" == "node-v1.0.0"


def elf(machine):
    return b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8) + struct.pack("<HH", 2, machine) + bytes(64)


def macho(cpu):
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu) + bytes(64)


def pe(machine):
    head = bytearray(b"MZ" + bytes(0x7E))
    struct.pack_into("<I", head, 0x3C, 0x80)
    return bytes(head) + b"PE\0\0" + struct.pack("<H", machine) + bytes(64)


FAKE = {"openbear-node-linux-amd64": elf(0x3E), "openbear-node-linux-arm64": elf(0xB7),
        "openbear-node-darwin-arm64": macho(0x0100000C), "openbear-node-windows-amd64.exe": pe(0x8664)}


@pytest.mark.parametrize("data, expected", [
    (elf(0x3E), ("linux", "amd64")), (elf(0xB7), ("linux", "arm64")), (macho(0x0100000C), ("darwin", "arm64")),
    (macho(0x01000007), ("darwin", "amd64")), (pe(0x8664), ("windows", "amd64")),
    (b"#!/bin/sh\necho fake\n", None), (b"\x7fELF" + bytes([1, 1]) + bytes(40), None), (b"MZ" + bytes(10), None),
])
def test_executable_headers_identify_platform(data, expected):
    assert node_release.platform_of(data) == expected


@pytest.mark.parametrize("output, ok", [
    ("openbear-node 1.0.1 (protocol v3)\n", True), ("openbear-node 1.0.1", True),
    ("openbear-node dev (protocol v3)", False), ("openbear-node 1.0.1 (protocol v2)", False), ("1.0.1", False),
])
def test_version_subcommand_must_report_this_release(output, ok):
    if ok:
        node_release.check_reported(output, "1.0.1", 3)
    else:
        with pytest.raises(ReleaseError, match="reports"):
            node_release.check_reported(output, "1.0.1", 3)


@pytest.mark.parametrize("value", ["1.0", "v1.0.0", "01.0.0", "1.0.0-rc1", "1.0.0 ", "1.0.0-alpha.1", "1.0.0-beta.0",
                                   "1.0.0-RC.1", "1.0.0+build"])
def test_node_version_format(value):
    with pytest.raises(ReleaseError, match=r"X\.Y\.Z, X\.Y\.Z-rc\.N"):
        node_release.parse_version(value)


@pytest.mark.parametrize("value", ["1.0.0", "1.2.0-rc.1", "1.2.0-beta.12", "1.0.0-dev.3"])
def test_node_version_format_accepts_every_written_kind(value):
    assert node_release.parse_version(value) == value
    node_release.build_script(value)  # Safe to interpolate into the build script.


def test_build_script_uses_the_required_flags_for_four_targets():
    script = node_release.build_script("1.2.3")
    builds = [line for line in script.splitlines() if " go build " in line]
    assert len(builds) == 4
    for line, target in zip(builds, ("linux-amd64", "linux-arm64", "darwin-arm64", "windows-amd64")):
        goos, arch = target.split("-")
        assert line.startswith(f"CGO_ENABLED=0 GOOS={goos} GOARCH={arch} go build -trimpath -buildvcs=false ")
        assert "-ldflags='-s -w -X openbear/node.Version=1.2.3'" in line and line.endswith("./cmd/openbear-node")
    assert "/out/assets/openbear-node-linux-amd64 version > /out/version.txt" in script
    assert "GOPROXY=off" in script and "go1\\.24\\.4" in script
    assert "go test -race ./..." in node_release.TEST_SCRIPT and "GOPROXY=off" in node_release.TEST_SCRIPT
    with pytest.raises(ReleaseError):
        node_release.build_script("1.2.3;rm -rf /")


# ---------------------------------------------------------------- public export / ZIP

@pytest.mark.parametrize("path, public", [
    ("node/go.mod", True), ("node/go.sum", True), ("node/VERSION", True), ("node/LICENSE", True), ("node/README.md", True),
    ("node/node.go", True), ("node/cmd/openbear-node/main.go", True), ("node/browser/cdp.go", True),
    ("node/browser/resources/bridge.js", True), ("node/browser/resources/keys.json", True),
    ("node/browser/resources/LICENSE", True), ("node/browser/resources/ThirdPartyNotices.txt", True),
    ("node/bin/openbear-node-linux-amd64", False), ("node/bin/manifest.json", False), ("node/bin/x.go", False),
    ("node/openbear-node", False), ("node/openbear-node-linux-amd64", False), ("node/browser/resources", False),
    ("node/config.json", False), ("node/notes.txt", False), ("node/browser/openbear-node.exe", False),
    ("node/mesh/wintun/amd64/wintun.dll", True), ("node/mesh/wintun/arm64/wintun.dll", True), ("node/mesh/wintun", False),
    ("node/third_party/wireguard-go/go.mod", True), ("node/third_party/wireguard-go/go.sum", True),
    ("node/third_party/wireguard-go/LICENSE", True), ("node/third_party/wireguard-go/device/device.go", True),
    ("node/third_party/wireguard-go/Makefile", True), ("node/third_party/wireguard-go/.gitignore", True),
    ("node/third_party/wireguard-go/tests/netns.sh", True), ("node/third_party/other/Makefile", False), ("node/mesh/go.mod", False),
])
def test_node_source_is_public_but_not_binaries(path, public):
    assert candidate.valid_path(path) is public


def test_controller_package_rejects_node_content(tmp_path):
    for name in ("node/go.mod", "app/openbear-node-linux-amd64", "node/bin/openbear-node-linux-amd64"):
        package = tmp_path / name.replace("/", "_")
        put(package / "app/__init__.py", "")
        put(package / name, "binary")
        with pytest.raises(build.ReleaseError, match="node"):
            build.audit_package(package)


def test_package_extras_check_rejects_node_content(tmp_path, monkeypatch):
    archive = tmp_path / "openbear.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("gateway/bin/openbear-gateway-linux-amd64", b"ELF")
        package.writestr("node/bin/openbear-node-linux-amd64", b"ELF")
    put(tmp_path / "install/gateway/bin/openbear-gateway-linux-amd64", "ELF")
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["verify_package_extras.py", str(tmp_path / "install"), str(archive), str(output)])
    assert verify_package_extras.main() == 1
    assert json.loads(output.read_text())["errors"] == ["package ships node content: node/bin/openbear-node-linux-amd64"]


# ---------------------------------------------------------------- local version and commit

def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    put(repo / "app/__init__.py", '__version__ = "0.13.1"\n')
    put(repo / ".gitignore", ".release-runs/\n/node/bin/\n")
    put(repo / "README.md", "fixture\n")
    node_source(repo)
    put(repo / "node/bin/openbear-node-linux-amd64", "local build output")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture base")
    return repo


def node_state(repo, version="1.0.1", files=("node/node.go",)):
    directory = repo / f".release-runs/node-{version}-123456789abc"
    directory.mkdir(parents=True)
    state = State(directory, {"version": version, "oldVersion": "1.0.0", "files": list(files),
                              "python": sys.executable, "stages": {}})
    return directory, state, Commands(directory)


def test_freeze_writes_node_version_and_commits_only_listed_files(repo):
    directory, state, command = node_state(repo)
    put(repo / "node/node.go", "package node\n\n// fixed\n")
    frozen = candidate.freeze(repo, directory, state, command, node_release.VERSIONING)
    assert (repo / "node/VERSION").read_text() == "1.0.1\n"
    assert git(repo, "log", "-1", "--format=%s") == "release: prepare node 1.0.1"
    assert set(git(repo, "show", "--name-only", "--format=", "HEAD").split()) == {"node/VERSION", "node/node.go"}
    assert frozen["files"]["node/VERSION"] == sha(b"1.0.1\n")
    assert "node/bin/openbear-node-linux-amd64" not in frozen["files"]
    assert candidate.version(repo) == "0.13.1"  # the controller version is untouched
    assert candidate.freeze(repo, directory, state, command, node_release.VERSIONING) == frozen
    assert git(repo, "status", "--porcelain") == ""


def test_freeze_refuses_unreviewed_changes(repo):
    directory, state, command = node_state(repo, files=())
    put(repo / "node/node.go", "package node\n\n// unreviewed\n")
    with pytest.raises(ReleaseError, match="Unreviewed changes: node/node.go"):
        candidate.freeze(repo, directory, state, command, node_release.VERSIONING)
    assert (repo / "node/VERSION").read_text() == "1.0.0\n"


def test_first_release_of_committed_source_needs_no_empty_commit(repo):
    head = git(repo, "rev-parse", "HEAD")
    directory, state, command = node_state(repo, version="1.0.0", files=())
    frozen = candidate.freeze(repo, directory, state, command, node_release.VERSIONING)
    assert frozen["localCommit"] == head == git(repo, "rev-parse", "HEAD")
    # The controller release still refuses an empty candidate.
    with pytest.raises(ReleaseError, match="No candidate changes"):
        directory2 = repo / ".release-runs/0.13.1-123456789abc"
        candidate.freeze(repo, directory2, State(directory2, {"version": "0.13.1", "oldVersion": "0.13.1", "files": [],
                         "python": sys.executable, "stages": {}}), Commands(directory2), candidate.Versioning(
            files=set(), read=candidate.version, write=lambda *a: None, check=lambda *a: None, message="x", retain=()))


def local_remote_commands(directory, remote):
    """Real git for the public checkout, with origin pointed at a local bare repository."""
    command = Commands(directory)

    def run(args, label, **kwargs):
        if args[:3] == ["git", "remote", "add"]:
            args = [*args[:-1], str(remote)]
        return command(args, label, **kwargs)
    return run


def test_node_release_of_already_public_source_tags_public_main(repo, tmp_path):
    # The controller release published this exact tree; a first node release of
    # committed source adds nothing and must reuse public main, not fail on an empty commit.
    remote = tmp_path / "public.git"
    git(tmp_path, "clone", "--bare", str(repo), str(remote))
    base = git(remote, "rev-parse", "main")
    directory, state, _ = node_state(repo, version="1.0.0", files=())
    state.value.update(repository="fixture/openbear", publicBase=base)
    command = local_remote_commands(directory, remote)
    candidate.freeze(repo, directory, state, command, node_release.VERSIONING)
    result = candidate.export(repo, directory, state, command, label="node 1.0.0")
    assert result["publicCommit"] == state.value["candidate"]["publicCommit"] == base
    assert candidate.source_matches(directory / "source", state.value["candidate"])
    # Changed node source still gets its own synchronizing commit on top of public main.
    directory2, state2, _ = node_state(repo, version="1.0.1")
    state2.value.update(repository="fixture/openbear", publicBase=base)
    put(repo / "node/node.go", "package node\n\n// fixed\n")
    command2 = local_remote_commands(directory2, remote)
    candidate.freeze(repo, directory2, state2, command2, node_release.VERSIONING)
    public = candidate.export(repo, directory2, state2, command2, label="node 1.0.1")["publicCommit"]
    assert public != base and git(directory2 / "source", "rev-parse", "HEAD^") == base
    assert git(directory2 / "source", "log", "-1", "--format=%s") == "release: synchronize local candidate for node 1.0.1"


def test_tracked_node_binary_blocks_public_export(repo):
    git(repo, "add", "-f", "node/bin/openbear-node-linux-amd64")
    git(repo, "commit", "-m", "accidental binary")
    directory, state, command = node_state(repo, version="1.0.0", files=())
    with pytest.raises(ReleaseError, match="not approved for public export: node/bin"):
        candidate.freeze(repo, directory, state, command, node_release.VERSIONING)


# ---------------------------------------------------------------- CLI

def notes_file(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("### Node\n\n- First separate release.\n")
    return path


@pytest.mark.parametrize("version, message", [
    ("1.0", "X.Y.Z, X.Y.Z-rc.N or X.Y.Z-beta.N"), ("1.0.0-rc1", "X.Y.Z, X.Y.Z-rc.N or X.Y.Z-beta.N"),
    ("1.0.1-dev.2", "1.0.1-dev.2 is a development version"), ("1.1.0-dev.1", "development version"),
])
def test_cli_rejects_invalid_or_development_versions(repo, tmp_path, capsys, version, message):
    code = release.main(["--repo", str(repo), "node", "start", version, "--notes", str(notes_file(tmp_path)), "--files"])
    assert code == 1
    assert message in json.loads(capsys.readouterr().err)["error"]
    assert not list((repo / ".release-runs").glob("node-*"))


def test_cli_start_status_and_duplicate_run(repo, tmp_path, capsys, monkeypatch):
    runs = []
    monkeypatch.setattr(node_release.NodeRunner, "run", lambda self, **_: runs.append(self.value["runId"]))
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    args = ["--repo", str(repo), "node", "start", "1.0.1", "--notes", str(notes_file(tmp_path)), "--files", "node/node.go"]
    assert release.main(args) == 0
    run_id = runs[0]
    assert node_release.RUN_ID_RE.fullmatch(run_id) and run_id.startswith("node-1.0.1-")
    capsys.readouterr()
    assert release.main(["--repo", str(repo), "node", "status", run_id]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["kind"] == "node" and value["version"] == "1.0.1" and value["files"] == ["node/node.go"]
    assert TOKEN not in (repo / ".release-runs" / run_id / "state.json").read_text()
    assert release.main(args) == 2
    assert "use node status/resume" in json.loads(capsys.readouterr().err)["error"]
    # A controller run ID is not a node run, and vice versa.
    assert release.main(["--repo", str(repo), "node", "status", "1.0.1-123456789abc"]) == 1
    assert release.main(["--repo", str(repo), "status", run_id]) == 1


# ---------------------------------------------------------------- GitHub fake

class NodeGitHub:
    """Realistic enough: drafts are invisible to /releases/tags, Latest is explicit."""
    token = TOKEN

    def __init__(self, *, tags=(), lose=None, steal_latest=False):
        self.main = "base"
        self.tags = {"refs/tags/" + name: "object-" + name for name in tags}
        self.releases = [{"id": 1, "tag_name": "v0.13.1", "draft": False, "prerelease": False, "body": "controller", "assets": []}]
        self.latest_id = 1
        self.lose, self.lost, self.steal_latest = lose, False, steal_latest
        self.posts, self.patches, self.uploads, self.payloads = [], [], [], {}

    def maybe_lose(self, phase):
        if phase == self.lose and not self.lost:
            self.lost = True
            raise ReleaseError("response lost after " + phase)

    def find(self, release_id):
        return next((item for item in self.releases if item["id"] == release_id), None)

    def api(self, path, method="GET", data=None, missing=False):
        if path == "":
            return {"permissions": {"push": True}}
        if path == "/git/ref/heads/main":
            return {"object": {"sha": self.main}}
        if path.startswith("/git/matching-refs/tags/"):
            # Like GitHub: every matching ref at once, whatever per_page and page say.
            prefix = "refs/tags/" + path.removeprefix("/git/matching-refs/tags/").partition("?")[0]
            return [{"ref": ref, "object": {"sha": sha_}} for ref, sha_ in self.tags.items() if ref.startswith(prefix)]
        if path.startswith("/git/ref/tags/"):
            ref = "refs/tags/" + path.removeprefix("/git/ref/tags/")
            return {"object": {"sha": self.tags[ref]}} if ref in self.tags else None
        if path.startswith("/releases/tags/"):
            tag = path.removeprefix("/releases/tags/")
            found = [item for item in self.releases if item["tag_name"] == tag and not item["draft"]]
            return copy.deepcopy(found[0]) if found else None
        if path == "/releases?per_page=100":
            return copy.deepcopy(self.releases)
        if path == "/releases/latest":
            return copy.deepcopy(self.find(self.latest_id))
        if path == "/releases" and method == "POST":
            self.posts.append(data)
            release = {**data, "id": 12, "assets": [], "upload_url": "https://uploads.github.com/upload{?name}",
                       "html_url": "https://github.com/fixture/openbear/releases/tag/" + data["tag_name"]}
            self.releases.append(release)
            self.maybe_lose("create")
            return copy.deepcopy(release)
        if path.startswith("/releases/"):
            release = self.find(int(path.removeprefix("/releases/")))
            if method == "PATCH":
                self.patches.append(data)
                release.update({k: v for k, v in data.items() if k != "make_latest"})
                if data.get("make_latest") == "true" or self.steal_latest:
                    self.latest_id = release["id"]
                self.maybe_lose("publish")
            return copy.deepcopy(release)
        raise AssertionError((path, method))

    def request(self, url, method="GET", data=None, **kwargs):
        if method == "POST":
            name = parse_qs(urlsplit(url).query)["name"][0]
            assert name not in self.uploads, "resuming must not reupload a confirmed asset"
            self.uploads.append(name)
            self.payloads[name] = data
            asset = {"id": 100 + len(self.uploads), "name": name, "state": "uploaded", "size": len(data),
                     "url": "https://api.github.com/assets/" + name,
                     "browser_download_url": "https://github.com/fixture/openbear/releases/download/node-v1.0.1/" + name}
            self.find(12)["assets"].append(asset)
            self.maybe_lose("upload")
            return asset
        assert method == "HEAD" and kwargs["authenticated"] is False
        return 200

    def download_asset(self, asset):
        return self.payloads[asset["name"]]


class FakeGit:
    """Public checkout + remote push via the shared publication mechanism."""

    def __init__(self, github, notes):
        self.github, self.notes = github, notes
        self.local_tag = None
        self.pushes = []

    def __call__(self, args, _label, **kwargs):
        if "status" in args:
            return ""
        if args[-2:] == ["rev-parse", "HEAD"] or args[-1].endswith("^{commit}"):
            return "candidate"
        if "tag" in args and "--list" in args:
            return self.local_tag or ""
        if "tag" in args and "-a" in args:
            assert "--cleanup=verbatim" in args
            self.local_tag = args[args.index("-a") + 1]
            return ""
        if "cat-file" in args:
            return "object candidate\ntagger OpenBear Release <release@openbear.invalid> 1 +0000\n\n" + self.notes.rstrip("\n")
        if "rev-parse" in args:
            return "tag-object"
        if "push" in args:
            assert kwargs["env"].get("GIT_CONFIG_COUNT") == "2"
            assert TOKEN not in " ".join(args)
            self.pushes.append(args[-1])
            if args[-1] == "candidate:refs/heads/main":
                self.github.main = "candidate"
            else:
                self.github.tags[args[-1]] = "tag-object"
            return ""
        raise AssertionError(args)

    def cancel_all(self):
        pass

    def recover_children(self):
        pass

    def redact(self, value):
        return value.replace(TOKEN, "<redacted>")


def node_assets(directory):
    (directory / "assets").mkdir(parents=True, exist_ok=True)
    assets = {}
    for name in EXPECTED_ASSETS:
        data = FAKE.get(name, b'{"schema": 1}\n')
        (directory / "assets" / name).write_bytes(data)
        assets[name] = {"bytes": len(data), "sha256": sha(data)}
    return assets


@pytest.fixture
def publication(tmp_path):
    (tmp_path / "source").mkdir()
    notes = "### Node\n\n- Separate release.\n"
    (tmp_path / "notes.md").write_text(notes)
    assets = node_assets(tmp_path)
    state = State(tmp_path, {"candidate": {"localCommit": "local", "publicCommit": "candidate"}, "version": "1.0.1",
                             "publicBase": "base", "notes": notes, "stages": {}})
    return tmp_path, state, notes, assets


def node_publish(directory, state, git_, github, assets):
    return publish(directory, state, git_, github, tag="node-v1.0.1", name="OpenBear Node 1.0.1", assets=assets, latest=False)


def test_publication_five_assets_never_latest(publication):
    directory, state, notes, assets = publication
    github = NodeGitHub()
    git_ = FakeGit(github, notes)
    result = node_publish(directory, state, git_, github, assets)
    assert result["ok"] and result["latest"] is False
    assert git_.pushes == ["candidate:refs/heads/main", "refs/tags/node-v1.0.1"]
    assert git_.local_tag == "node-v1.0.1"
    assert github.posts == [{"tag_name": "node-v1.0.1", "target_commitish": "candidate", "name": "OpenBear Node 1.0.1",
                             "body": notes, "draft": True, "prerelease": False, "generate_release_notes": False, "make_latest": "false"}]
    assert github.patches == [{"draft": False, "prerelease": False, "make_latest": "false"}]
    assert github.latest_id == 1  # the controller release stays Latest
    assert sorted(github.uploads) == sorted(EXPECTED_ASSETS) and len(github.uploads) == 5
    assert set(result["assets"]) == set(EXPECTED_ASSETS)
    assert all(github.payloads[name] == (directory / "assets" / name).read_bytes() for name in EXPECTED_ASSETS)


@pytest.mark.parametrize("phase", ["create", "upload", "publish"])
def test_unknown_outcome_is_reconciled_from_remote_before_any_retry(publication, phase):
    directory, state, notes, assets = publication
    github = NodeGitHub(lose=phase)
    git_ = FakeGit(github, notes)
    with pytest.raises(ReleaseError, match="response lost"):
        node_publish(directory, state, git_, github, assets)
    restored = State(directory, load(directory / "state.json"))
    result = node_publish(directory, restored, git_, github, assets)
    assert result["ok"] and result["latest"] is False
    assert len(github.posts) == 1 and len(github.patches) == 1
    assert len([item for item in github.releases if item["tag_name"] == "node-v1.0.1"]) == 1
    assert len(github.uploads) == 5 and len(set(github.uploads)) == 5
    assert git_.pushes == ["candidate:refs/heads/main", "refs/tags/node-v1.0.1"]
    assert github.latest_id == 1
    assert TOKEN not in (directory / "state.json").read_text()


def test_release_that_became_latest_stops_for_inspection(publication):
    directory, state, notes, assets = publication
    github = NodeGitHub(steal_latest=True)
    with pytest.raises(ReleaseError, match="never be Latest") as error:
        node_publish(directory, state, FakeGit(github, notes), github, assets)
    assert error.value.code == 2


def test_foreign_asset_blocks_publication(publication):
    directory, state, notes, assets = publication
    github = NodeGitHub(lose="upload")
    git_ = FakeGit(github, notes)
    with pytest.raises(ReleaseError):
        node_publish(directory, state, git_, github, assets)
    github.find(12)["assets"].append({"name": "openbear-node-linux-386", "id": 999})
    with pytest.raises(ReleaseError, match="foreign or duplicate"):
        node_publish(directory, state, git_, github, assets)
    assert not github.patches


# ---------------------------------------------------------------- preflight (remote version authority)

class DockerCommands(FakeGit):
    def __init__(self):
        self.calls = []

    def __call__(self, args, label, **kwargs):
        self.calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return IMAGE
        return ""


def preflight_runner(tmp_path, github, version, notes="### Node\n"):
    directory = tmp_path / ("node-" + version + "-123456789abc")
    directory.mkdir()
    value = {"schema": 1, "kind": "node", "runId": directory.name, "repository": "fixture/openbear", "version": version,
             "notes": notes, "python": sys.executable, "createdAt": 0, "stages": {}}
    return node_release.NodeRunner(tmp_path, directory, value, github=github, command=DockerCommands())


@pytest.mark.parametrize("version", ["1.10.0", "1.9.9", "1.2.0", "0.9.0", "1.10.0-rc.3", "1.10.0-beta.9"])
def test_version_must_exceed_highest_published_node_tag(tmp_path, version):
    github = NodeGitHub(tags=("node-v1.2.0", "node-v1.10.0", "node-v1.9.0", "node-vbad", "node-v2.0.0-dev.1", "v9.9.9"))
    runner = preflight_runner(tmp_path, github, version)
    with pytest.raises(ReleaseError, match=r"newer than the latest published node-v1\.10\.0|already exists"):
        runner.preflight()
    assert "publicBase" not in runner.value


@pytest.mark.parametrize("tags, version, ok", [
    # Project order, not semver's: release > rc > beta > dev for the same X.Y.Z.
    (("node-v1.0.1-beta.2",), "1.0.1-rc.1", True), (("node-v1.0.1-rc.1",), "1.0.1-beta.9", False),
    (("node-v1.0.1-rc.1",), "1.0.1", True), (("node-v1.0.1",), "1.0.1-rc.2", False),
    (("node-v1.0.1-beta.2",), "1.0.1-beta.10", True), (("node-v1.0.1-beta.10",), "1.0.1-beta.9", False),
    (("node-v1.2.0-beta.1",), "1.2.0-beta.2", True), (("node-v1.2.0",), "1.3.0-beta.1", True),
])
def test_newer_than_published_uses_the_project_order(tmp_path, tags, version, ok):
    runner = preflight_runner(tmp_path, NodeGitHub(tags=tags), version)
    if ok:
        runner.preflight()
    else:
        with pytest.raises(ReleaseError, match="must be newer than the latest published"):
            runner.preflight()


def test_preflight_rejects_a_development_version(tmp_path):
    runner = preflight_runner(tmp_path, NodeGitHub(), "1.1.0-dev.4")
    with pytest.raises(ReleaseError, match="development version"):
        runner.preflight()


def test_release_changelog_starts_from_the_previous_release_not_its_prereleases(tmp_path):
    github = NodeGitHub(tags=("node-v1.1.0", "node-v1.2.0-beta.1", "node-v1.2.0-rc.1"))
    runner = preflight_runner(tmp_path, github, "1.2.0")
    runner.preflight()
    assert runner.value["notes"].endswith("compare/node-v1.1.0...node-v1.2.0\n")
    (tmp_path / "rc").mkdir()
    runner = preflight_runner(tmp_path / "rc", github, "1.2.0-rc.2")
    runner.preflight()
    assert runner.value["notes"].endswith("compare/node-v1.2.0-rc.1...node-v1.2.0-rc.2\n")


def test_preflight_records_previous_node_tag_and_changelog(tmp_path):
    github = NodeGitHub(tags=("node-v1.2.0", "node-v1.10.0", "v9.9.9"))
    runner = preflight_runner(tmp_path, github, "1.10.1")
    runner.preflight()
    assert runner.value["previousNodeVersion"] == "1.10.0" and runner.value["publicBase"] == "base"
    assert runner.value["goImage"] == IMAGE
    assert runner.value["notes"].endswith(
        "**Full Changelog**: https://github.com/fixture/openbear/compare/node-v1.10.0...node-v1.10.1\n")
    assert (runner.directory / "notes.md").read_text() == runner.value["notes"]


def test_first_node_release_and_duplicate_tag(tmp_path):
    runner = preflight_runner(tmp_path, NodeGitHub(tags=("v0.13.1",)), "1.0.0")
    runner.preflight()
    assert runner.value["previousNodeVersion"] is None and "Full Changelog" not in runner.value["notes"]
    (tmp_path / "other").mkdir()
    duplicate = preflight_runner(tmp_path / "other", NodeGitHub(tags=("node-v1.0.0",)), "1.0.0")
    with pytest.raises(ReleaseError, match="already exists"):
        duplicate.preflight()


def test_missing_token_stops_before_docker_or_github(tmp_path):
    github = NodeGitHub()
    github.token = ""
    runner = preflight_runner(tmp_path, github, "1.0.0")
    with pytest.raises(ReleaseError, match="GH_TOKEN"):
        runner.preflight()
    assert runner.command.calls == []


# ---------------------------------------------------------------- build stage (fake container)

def build_runner(tmp_path, version="1.0.1", protocol=3):
    directory = (tmp_path / ("node-" + version + "-123456789abc")).resolve()
    node_source(directory / "source", version, protocol)
    files = {str(p.relative_to(directory / "source")): sha(p.read_bytes()) for p in (directory / "source").rglob("*") if p.is_file()}
    value = {"schema": 1, "kind": "node", "runId": directory.name, "repository": "fixture/openbear", "version": version,
             "notes": "n\n", "python": sys.executable, "createdAt": 0, "stages": {}, "goImage": IMAGE, "timeout": 60,
             "candidate": {"files": files, "sourceFingerprint": "fp", "localCommit": "local", "publicCommit": "candidate"}}
    runner = node_release.NodeRunner(tmp_path.resolve(), directory, value, github=SimpleNamespace(token=TOKEN), command=DockerCommands())
    deps = tmp_path.resolve() / "deps"
    (deps / "gomod").mkdir(parents=True)
    return runner, {"key": "deps", "dir": str(deps)}


def fake_container(binaries=FAKE, reported="openbear-node {version} (protocol v{protocol})", calls=None):
    def contain(self, stage, script, mounts, *, online=False):
        if calls is not None:
            calls.append({"stage": stage.stage, "script": script, "mounts": mounts, "online": online})
        out = stage.out / "assets"
        out.mkdir()
        for name, data in binaries.items():
            (out / name).write_bytes(data)
        (stage.out / "version.txt").write_text(reported.format(version=self.value["version"], protocol=3) + "\n")
        (stage.out / "toolchain.txt").write_text("go version go1.24.4 linux/amd64\n")
    return contain


def test_build_produces_five_assets_and_build_node_manifest(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(node_release.NodeRunner, "contain", fake_container(calls=calls))
    runner, deps = build_runner(tmp_path)
    result = runner.build_binaries(deps)
    assert list(result["assets"]) == EXPECTED_ASSETS
    assets = runner.directory / "assets"
    assert sorted(p.name for p in assets.iterdir()) == sorted(EXPECTED_ASSETS)
    text = (assets / "manifest.json").read_text()
    value = json.loads(text)
    assert value == {"schema": 1, "version": "1.0.1", "protocol": 3, "protocolMin": 3, "features": FEATURES,
                     "toolchain": "go version go1.24.4 linux/amd64",
                     "binaries": {name: {"sha256": sha(FAKE[name]), "bytes": len(FAKE[name])} for name in EXPECTED_ASSETS[:4]}}
    assert list(value) == ["schema", "version", "protocol", "protocolMin", "features", "toolchain", "binaries"]
    assert list(value["binaries"]) == EXPECTED_ASSETS[:4]
    assert text == json.dumps(value, indent=2) + "\n"  # byte format of scripts/build_node.py
    assert result["protocol"] == node_release.build_node.protocol(runner.directory / "source/node") == 3
    assert result["versionOutput"] == "openbear-node 1.0.1 (protocol v3)"
    [call] = calls
    assert call["script"] == node_release.build_script("1.0.1") and not call["online"]
    assert (runner.directory / "source/node", "/inputs/node", True) in call["mounts"]
    node_release.check_manifest(assets, "1.0.1", 3, 3, FEATURES)


def test_manifest_carries_protocol_min_and_features_from_protocol_go(tmp_path, monkeypatch):
    monkeypatch.setattr(node_release.NodeRunner, "contain", fake_container())
    runner, deps = build_runner(tmp_path, version="1.2.0-beta.1")
    put(runner.directory / "source/node/protocol.go", protocol_go(3, 2, ["a.b"]))
    result = runner.build_binaries(deps)
    value = json.loads((runner.directory / "assets/manifest.json").read_text())
    assert (value["version"], value["protocol"], value["protocolMin"], value["features"]) == ("1.2.0-beta.1", 3, 2, ["a.b"])
    assert (result["protocolMin"], result["features"]) == (2, ["a.b"])
    with pytest.raises(ReleaseError, match="features"):
        node_release.check_manifest(runner.directory / "assets", "1.2.0-beta.1", 3, 2, ["a.b", "c.d"])
    with pytest.raises(ReleaseError, match="features"):
        node_release.check_manifest(runner.directory / "assets", "1.2.0-beta.1", 3, 3, ["a.b"])


def test_build_requires_features_in_protocol_go(tmp_path, monkeypatch):
    monkeypatch.setattr(node_release.NodeRunner, "contain", fake_container())
    runner, deps = build_runner(tmp_path)
    put(runner.directory / "source/node/protocol.go", protocol_go(3, features=None))
    with pytest.raises(ReleaseError, match="no Features"):
        runner.build_binaries(deps)


@pytest.mark.parametrize("swap, message", [
    ({"openbear-node-darwin-arm64": elf(0xB7)}, "not a darwin-arm64 executable"),
    ({"openbear-node-linux-arm64": elf(0x3E)}, "not a linux-arm64 executable"),
    ({"openbear-node-windows-amd64.exe": b"#!/bin/sh\n"}, "not a windows-amd64 executable"),
])
def test_wrong_executable_header_fails_the_build(tmp_path, monkeypatch, swap, message):
    monkeypatch.setattr(node_release.NodeRunner, "contain", fake_container({**FAKE, **swap}))
    runner, deps = build_runner(tmp_path)
    with pytest.raises(ReleaseError, match=message):
        runner.build_binaries(deps)
    assert not (runner.directory / "assets").exists()


def test_wrong_reported_version_fails_the_build(tmp_path, monkeypatch):
    monkeypatch.setattr(node_release.NodeRunner, "contain", fake_container(reported="openbear-node dev (protocol v3)"))
    runner, deps = build_runner(tmp_path)
    with pytest.raises(ReleaseError, match="reports 'openbear-node dev"):
        runner.build_binaries(deps)


def test_manifest_check_rejects_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(node_release.NodeRunner, "contain", fake_container())
    runner, deps = build_runner(tmp_path)
    runner.build_binaries(deps)
    assets = runner.directory / "assets"
    (assets / "openbear-node-linux-arm64").write_bytes(elf(0xB7) + b"x")
    with pytest.raises(ReleaseError, match="digests"):
        node_release.check_manifest(assets, "1.0.1", 3, 3, FEATURES)
    with pytest.raises(ReleaseError, match="schema/version/protocol"):
        node_release.check_manifest(assets, "1.0.2", 3, 3, FEATURES)


def test_verified_build_is_restored_after_candidate_supersedes_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(node_release.NodeRunner, "contain", fake_container())
    runner, deps = build_runner(tmp_path)
    result = runner.build_binaries(deps)
    (runner.directory / "superseded").mkdir()
    (runner.directory / "assets").rename(runner.directory / "superseded/assets")
    assert runner.materialize(result)
    assert sorted(p.name for p in (runner.directory / "assets").iterdir()) == sorted(EXPECTED_ASSETS)
    (Path(result["outputs"]["dir"]) / "manifest.json").write_text("{}")
    (runner.directory / "assets/manifest.json").write_text("{}")
    assert not runner.materialize(result)


def test_container_stages_use_offline_go_image_and_cached_modules(tmp_path, monkeypatch):
    calls = []

    def contain(self, stage, script, mounts, *, online=False):
        calls.append({"stage": stage.stage, "script": script, "mounts": mounts, "online": online})
        if stage.stage == "node-deps":
            put(stage.out / "gomod/cache/download/golang.org/x/sys/@v/v0.38.0.zip", "zip")
        else:
            (stage.out / "toolchain.txt").write_text("go version go1.24.4 linux/amd64\n")

    monkeypatch.setattr(node_release.NodeRunner, "contain", contain)
    runner, _ = build_runner(tmp_path)
    runner.cache = (tmp_path / "cache").resolve()
    deps = runner.deps()
    assert not deps["reused"] and runner.deps_valid(deps)
    assert runner.deps()["reused"] and len(calls) == 1
    runner.test(deps)
    deps_call, test_call = calls
    assert deps_call["online"] and deps_call["script"] == node_release.DEPS_SCRIPT
    assert not test_call["online"] and test_call["script"] == node_release.TEST_SCRIPT
    assert (Path(deps["dir"]) / "gomod", "/inputs/gomod", True) in test_call["mounts"]
    (Path(deps["dir"]) / "gomod/cache/download/golang.org/x/sys/@v/v0.38.0.zip").chmod(0o644)
    (Path(deps["dir"]) / "gomod/cache/download/golang.org/x/sys/@v/v0.38.0.zip").write_text("changed")
    with pytest.raises(ReleaseError, match="changed"):
        runner.deps()


# ---------------------------------------------------------------- whole run: resume never republishes

def test_whole_node_run_resumes_without_rebuilding_or_republishing(tmp_path, monkeypatch):
    runner, _ = build_runner(tmp_path)
    directory, value = runner.directory, runner.value
    value.update(publicBase="base", runId=directory.name)
    calls = Counter()

    monkeypatch.setattr(candidate, "freeze", lambda *args: value["candidate"])
    monkeypatch.setattr(candidate, "export", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(candidate, "source_matches", lambda *args: True)
    monkeypatch.setattr(candidate, "assert_local", lambda *args: None)

    class Runner(node_release.NodeRunner):
        def preflight(self, approved=False):
            calls["preflight"] += 1

        def deps(self):
            calls["deps"] += 1
            return {"ok": True, "key": "deps", "dir": str(tmp_path / "deps")}

        def deps_valid(self, result):
            return True

        def deps_key(self):
            return "deps"

        def test(self, deps):
            calls["test"] += 1
            return {"ok": True}

    monkeypatch.setattr(node_release.NodeRunner, "contain", fake_container())
    original = Runner.build_binaries

    def counted_build(self, deps):
        calls["build"] += 1
        return original(self, deps)

    Runner.build_binaries = counted_build

    def publisher(directory_, state, command, github, **kwargs):
        calls["publish"] += 1
        assert kwargs["tag"] == "node-v1.0.1" and kwargs["latest"] is False and list(kwargs["assets"]) == EXPECTED_ASSETS
        if calls["publish"] == 1:
            raise ReleaseError("GitHub POST /releases: TimeoutError; outcome may be unknown, resume reconciles remote state")
        return {"ok": True, "url": "https://example.invalid/node-v1.0.1", "latest": False}

    monkeypatch.setattr(node_release, "publish", publisher)
    first = Runner(tmp_path, directory, value, github=SimpleNamespace(token=TOKEN), command=DockerCommands())
    with pytest.raises(ReleaseError, match="outcome may be unknown"):
        first.run()
    saved = load(directory / "state.json")
    assert saved["lastError"]["resume"].endswith("scripts/release.py node resume " + directory.name)
    resumed = Runner(tmp_path, directory, saved, github=SimpleNamespace(token=TOKEN), command=DockerCommands())
    resumed.run()
    assert resumed.value["completed"] and resumed.value["published"]
    assert calls == {"preflight": 2, "deps": 1, "test": 1, "build": 1, "publish": 2}
    again = Runner(tmp_path, directory, load(directory / "state.json"), github=SimpleNamespace(token=TOKEN), command=DockerCommands())
    again.run()
    assert calls["publish"] == 2 and calls["preflight"] == 2
    report = load(directory / "report.json")
    assert report["kind"] == "node" and report["tag"] == "node-v1.0.1" and report["completed"]
    assert not (directory / "source").exists()  # one-shot public checkout removed after publication
    # Published binaries are kept once, in assets/, not again in every build attempt.
    assert sorted(p.name for p in (directory / "assets").iterdir()) == sorted(EXPECTED_ASSETS)
    attempts = list((directory / "results/node-build/attempts").iterdir())
    assert attempts and all((a / "stage.log").is_file() and not (a / "assets").exists() for a in attempts)
