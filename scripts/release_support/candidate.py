"""Local version authority and public-history export; never push private history."""
from __future__ import annotations

import copy
import io
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from .common import ReleaseError, atomic_json, fingerprint, sha
from .versions import versioning as versions
from .versions import written

VERSION_FILES = {"app/__init__.py", "pyproject.toml", "uv.lock", "web/package.json", "web/package-lock.json"}
PUBLIC_ROOTS = {".githooks", ".gitignore", ".gitattributes", ".dockerignore", "Dockerfile", "README.md", "README", "LICENSE", "app", "docs",
                "openbear.json.example", "openbear.service", "prompts", "pyproject.toml", "scripts", "tests", "uv.lock", "web", "gateway",
                "mobile", "node", "benchmark-package"}
# The node is Go source plus the resources it embeds (browser bridge, wintun) and
# the modules it vendors under third_party; binaries and local build output
# (node/bin, openbear-node-*) are never public source.
NODE_FILES = {"node/go.mod", "node/go.sum", "node/VERSION", "node/LICENSE",
              "node/mesh/routing/testdata/sim-links.json", "node/mesh/testdata/contract_configs.py", "node/mesh/testdata/hostfw_container.sh",
              "node/third_party/wireguard-go/.gitignore", "node/third_party/wireguard-go/Makefile", "node/third_party/wireguard-go/tests/netns.sh"}
NODE_RESOURCES = ("node/browser/resources/", "node/mesh/wintun/")
NODE_VENDORED = "node/third_party/"
VENDORED_FILES = {"go.mod", "go.sum", "LICENSE"}
BENCHMARK_FILES = {
    "ip.check.place": {"ip.sh", "main/ref/cookies.txt", "main/ref/dnsbl.list", "main/ref/iata-icao.csv", "main/ref/iso3166.json"},
    "net.check.place": {"net.sh", "main/ref/AS_Mapping.txt", "main/ref/iperf.json", "main/ref/iso3166.json", "main/ref/province.json", "main/ref/speedtest_cn.json"},
    "hardware.check.place": {"hardware.sh"}, "tcpquality": {"runTcpQuality-core.sh"},
    "service-unlock": {"NOTICE.md"}, "geekbench5": {"run.sh"},
}


def benchmark_path(name: str) -> bool:
    if name == "benchmark-package/README.md":
        return True
    parts = PurePosixPath(name).parts
    if len(parts) < 3 or parts[0] != "benchmark-package" or parts[1] not in BENCHMARK_FILES:
        return False
    if len(parts) == 3:
        return parts[2] == "recipe.json"
    return bool(re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", parts[2])
                and "/".join(parts[3:]) in BENCHMARK_FILES[parts[1]] | {"manifest.json", "openbear.patch", "LICENSE"})


class Versioning:
    """How one release kind reads, writes and commits its own version."""

    def __init__(self, *, files, read, write, check, message, retain, allow_unchanged=False, develop_message=""):
        self.files, self.read, self.write, self.check = set(files), read, write, check
        self.message, self.retain, self.allow_unchanged = message, set(retain), allow_unchanged
        self.develop_message = develop_message


def _bump(repo: Path, value: dict, command) -> None:
    command([value["python"], str(repo / "scripts/bump_version.py"), value["version"]], "bump-version", cwd=repo)


def _check_bump(repo: Path, value: dict, command) -> None:
    command([value["python"], str(repo / "scripts/bump_version.py"), "--check", "--expect", value["version"]], "check-version", cwd=repo)


def version(repo: Path) -> str:
    match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', (repo / "app/__init__.py").read_text(), re.M)
    if not match or not written(match[1]):
        raise ReleaseError("Cannot read the app/__init__.py version (X.Y.Z, X.Y.Z-rc.N, X.Y.Z-beta.N or X.Y.Z-dev.N)")
    return match[1]


def valid_path(name: str) -> bool:
    p = PurePosixPath(name)
    if name.startswith("benchmark-package/") and not benchmark_path(name):
        return False
    if name.startswith("gateway/") and not (name.endswith((".go", ".md")) or name in {"gateway/go.mod", "gateway/go.sum", "gateway/LICENSE", "gateway/login.html", "gateway/openbear-push-sw.js"}):
        return False
    if name.startswith("node/") and (name.startswith("node/bin/") or p.name.startswith("openbear-node")
                                     or not (name.endswith((".go", ".md")) or name in NODE_FILES
                                             or (name.startswith(NODE_RESOURCES) and len(p.parts) > 3)
                                             or (name.startswith(NODE_VENDORED) and p.name in VENDORED_FILES))):
        return False
    return bool(name and not p.is_absolute() and ".." not in p.parts and "\\" not in name
                and p.parts[0] in PUBLIC_ROOTS and not any(x in p.parts for x in ("__pycache__", "node_modules", ".venv", ".git"))
                and not name.startswith(("web/dist/", "web/data/", "web/logs/", "web/.dist-before-"))
                and not name.endswith((".db", ".db-wal", ".db-shm", ".pyc")))


def changed_paths(git) -> set[str]:
    raw = git("status", "--porcelain=v1", "--untracked-files=all", "-z")
    values, result, i = raw.split("\0"), set(), 0
    while i < len(values):
        row = values[i]
        i += 1
        if not row:
            continue
        result.add(row[3:])
        if "R" in row[:2] or "C" in row[:2]:
            result.add(values[i])
            i += 1
    # Old developer build backups are never release inputs. Tracked build files
    # still fail the complete public-tree allowlist below.
    return {p for p in result if not p.startswith("build/")}


def tree_files(git) -> list[str]:
    rows = filter(None, git("ls-tree", "-rz", "HEAD").split("\0"))
    files = []
    for row in rows:
        header, name = row.split("\t", 1)
        mode, kind, _oid = header.split()
        if kind != "blob" or mode not in {"100644", "100755"} or not valid_path(name):
            raise ReleaseError(f"Source is not approved for public export: {name}")
        files.append(name)
    return files


def assert_public_candidate(candidate: dict) -> None:
    # Saved candidates are bound to their complete source fingerprint. Reject an
    # old policy's private inputs rather than silently filtering a frozen tree.
    for name in candidate["files"]:
        if not valid_path(name):
            raise ReleaseError(f"Frozen candidate is not approved for public export: {name}")


def source_matches(source: Path, candidate: dict) -> bool:
    # A cached export must not retain local evals, even outside its manifest.
    return bool((source / ".git").is_dir()
                and not (source / "evals").exists() and not (source / "evals").is_symlink()
                and all(valid_path(p) and (source / p).is_file() and sha((source / p).read_bytes()) == digest
                        for p, digest in candidate["files"].items()))


APP_VERSIONING = Versioning(files=VERSION_FILES, read=version, write=_bump, check=_check_bump,
                            message="release: prepare {version}", retain={"py311", "py312", "py313", "frontend", "build"},
                            develop_message="release: start {version} development")


def freeze(repo: Path, directory: Path, state, command, versioning: Versioning = APP_VERSIONING) -> dict:
    value = state.value

    def git(*args, check=True):
        return command(["git", *args], "candidate-git", cwd=repo, check=check)

    if git("branch", "--show-current") != "main":
        raise ReleaseError("Release must start from local main")
    current = git("rev-parse", "HEAD")
    intent = value.get("commitIntent")
    recovered_commit = False
    if intent:
        if current != intent["parent"]:
            parents = git("show", "-s", "--format=%P", current).split()
            if parents != [intent["parent"]] or git("rev-parse", current + "^{tree}") != intent["tree"]:
                raise ReleaseError("Uncertain commit does not match the saved release intent", code=2)
            value["localCandidate"] = current
            recovered_commit = True
    prior = value.get("localCandidate")
    if prior and prior != current:
        raise ReleaseError("Local HEAD changed outside this run; refusing to adopt unrelated history", code=2)
    changed = changed_paths(git)
    allowed = set(value["files"]) | versioning.files
    deleted = set(filter(None, git("diff", "--cached", "--name-only", "--no-renames", "--diff-filter=D", "-z").split("\0")))
    prior_removals = set(value.get("localOnlyRemovals", [])) if prior or recovered_commit else set()
    indexed = set(filter(None, git("ls-files", "-z").split("\0")))
    removals = {p for p in allowed & (deleted | prior_removals) if p not in indexed
                and (p == "evals" or p.startswith("evals/"))
                and ".." not in PurePosixPath(p).parts and "\\" not in p}
    if any(not valid_path(p) for p in allowed - removals):
        raise ReleaseError("--files contains an unsafe or non-public path")
    if changed - allowed:
        raise ReleaseError("Unreviewed changes: " + ", ".join(sorted(changed - allowed)))
    if changed and value.get("publicationIntent"):
        raise ReleaseError("Candidate already has publication effects; do not replace it or move its tag", code=2)
    if prior and not changed and not recovered_commit:
        if versioning.read(repo) != value["version"]:
            raise ReleaseError("Local release version drifted")
        assert_public_candidate(value["candidate"])
        tree_files(git)  # Reapply the current public policy before stage reuse.
        return value["candidate"]
    current_version = versioning.read(repo)
    if not prior:
        # Development builds raise the dev number meanwhile (npm run build, build_node.py);
        # any other change of the local version is someone else's.
        if current_version not in {value["oldVersion"], value["version"]} and not versions.is_dev(current_version):
            raise ReleaseError("Local version changed outside this run")
        versioning.write(repo, value, command)
    elif current_version != value["version"]:
        raise ReleaseError("Corrective candidate must keep this run's version")
    versioning.check(repo, value, command)
    unchanged = False
    if not recovered_commit:
        git("add", "--all", "--", *sorted(allowed - removals))
        staged = set(filter(None, git("diff", "--cached", "--name-only", "--no-renames", "-z").split("\0")))
        remaining = set(filter(None, git("ls-files", "-z").split("\0")))
        if removals & remaining:
            raise ReleaseError("Local-only removal was replaced by staged content")
        if staged - allowed:
            raise ReleaseError("Index contains unrelated changes")
        # A kind whose committed version already equals the target (a first
        # node release of committed source) releases HEAD without an empty commit.
        unchanged = not staged and not prior and versioning.allow_unchanged
        if not staged and not unchanged:
            raise ReleaseError("No candidate changes to commit")
    if not recovered_commit and not unchanged:
        hook = repo / ".githooks/pre-commit"
        if hook.is_file() and git("config", "--get", "core.hooksPath", check=False) != ".githooks":
            command(["bash", str(hook)], "pre-commit-hook", cwd=repo)
        value["localOnlyRemovals"] = sorted(removals)
        value["commitIntent"] = {"parent": current, "tree": git("write-tree")}
        state.save()
        git("-c", "user.name=OpenBear Release", "-c", "user.email=release@openbear.invalid", "commit", "-m",
            versioning.message.format(version=value["version"]))
    local = git("rev-parse", "HEAD")
    files = tree_files(git)
    contents = {p: sha((repo / p).read_bytes()) for p in files}
    candidate = {"localCommit": local, "version": value["version"], "files": contents,
                 "sourceFingerprint": fingerprint(contents)}
    if value.get("candidate"):
        value.setdefault("candidateHistory", []).append(copy.deepcopy({"candidate": value["candidate"], "stages": value.get("stages", {})}))
        archive = directory / "superseded" / value["candidate"]["localCommit"]
        archive.mkdir(parents=True, exist_ok=True)
        # Attempt directories contain immutable evidence referenced by caches
        # and history. Never move them. Build stays until its own inputs change;
        # candidate-specific packages/acceptance receipts are never retained.
        for name in ("source", "package", "assets", "package-manifest.json"):
            path = directory / name
            if path.exists():
                shutil.move(str(path), archive / name)
    retained = {name: record for name, record in value.get("stages", {}).items() if name in versioning.retain}
    value.update(localCandidate=local, candidate=candidate, stages=retained)
    value.pop("scope", None)
    value.pop("commitIntent", None)
    state.save()
    return candidate


def export(repo: Path, directory: Path, state, command, *, label: str | None = None) -> dict:
    value, source = state.value, directory / "source"
    candidate = value["candidate"]
    assert_public_candidate(candidate)  # Before deleting a cache or fetching public history.
    if source.exists():
        if source.is_symlink():
            raise ReleaseError("Public source directory cannot be a symlink")
        shutil.rmtree(source)
    source.mkdir()
    repository = value["repository"]
    url = f"https://github.com/{repository}.git"

    def git(*args):
        return command(["git", *args], "public-git", cwd=source)

    git("init", "-b", "main")
    git("remote", "add", "origin", url)
    git("fetch", "--depth", "1", "origin", value["publicBase"])
    git("reset", "--hard", value["publicBase"])
    for item in source.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
    archive = subprocess.check_output(["git", "archive", "--format=tar", candidate["localCommit"]], cwd=repo)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            if member.isdir():
                continue
            if not member.isreg() or not valid_path(member.name) or member.name not in candidate["files"]:
                raise ReleaseError(f"Unsafe exported file: {member.name}")
            target = source / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            data = tar.extractfile(member).read()
            if sha(data) != candidate["files"][member.name]:
                raise ReleaseError("Frozen source differs from local candidate")
            target.write_bytes(data)
            target.chmod(member.mode)
    git("config", "core.hooksPath", ".githooks")
    git("add", "--all")
    git("diff", "--cached", "--check")
    # A node release of source the controller release already published has
    # nothing to add: it tags public main itself instead of failing on an
    # empty commit.
    if git("diff", "--cached", "--name-only"):
        git("-c", "user.name=OpenBear Release", "-c", "user.email=release@openbear.invalid", "commit", "-m",
            f"release: synchronize local candidate for {label or value['version']}")
    public = git("rev-parse", "HEAD")
    value["candidate"]["publicCommit"] = public
    state.save()
    atomic_json(directory / "candidate.json", value["candidate"])
    return {"sourceDir": str(source), "publicCommit": public, "sourceFingerprint": candidate["sourceFingerprint"]}


def start_development(repo: Path, state, command, versioning: Versioning = APP_VERSIONING) -> dict:
    """After publication: the local version becomes the next minor's dev.1 in one local
    commit of the version files only. It is never pushed; a later release commit replaces
    it with that release's version before anything is exported."""
    value = state.value
    if value.get("development"):
        return value["development"]
    following = versions.next_dev(value["version"])
    message = versioning.develop_message.format(version=following)
    files = sorted(versioning.files)

    def git(*args, check=True):
        return command(["git", *args], "development-git", cwd=repo, check=check)

    if git("branch", "--show-current") != "main":
        raise ReleaseError("Published; local main is not checked out, so the development version was not set")
    head = git("rev-parse", "HEAD")
    intent = value.get("developmentIntent")
    if intent and head != intent["parent"]:
        # The commit may have happened before its outcome was saved.
        if (git("show", "-s", "--format=%P", head).split() == [intent["parent"]] and git("log", "-1", "--format=%s", head) == message
                and versioning.read(repo) == following):
            value["development"] = {"version": following, "commit": head}
            value.pop("developmentIntent", None)
            state.save()
            return value["development"]
        raise ReleaseError("Published; local HEAD moved before the development version was committed; set it by hand", code=2)
    current = versioning.read(repo)
    if current not in {value["version"], following}:
        raise ReleaseError(f"Published; local version is now {current}, so it was left alone (expected {value['version']})", code=2)
    # Only this run's own interrupted write may be pending; other edits are never committed for someone.
    if not intent and git("status", "--porcelain", "--untracked-files=no", "--", *files):
        raise ReleaseError("Published; version files have uncommitted changes, so the development version was not set", code=2)
    value["developmentIntent"] = {"parent": head, "version": following}
    state.save()
    versioning.write(repo, {**value, "version": following}, command)
    versioning.check(repo, {**value, "version": following}, command)
    commit = None
    if git("status", "--porcelain", "--untracked-files=no", "--", *files):
        # Only the version files, whatever else is staged or edited meanwhile.
        git("-c", "user.name=OpenBear Release", "-c", "user.email=release@openbear.invalid", "commit",
            "-m", message, "--", *files)
        commit = git("rev-parse", "HEAD")
    value["development"] = {"version": following, "commit": commit}
    value.pop("developmentIntent", None)
    state.save()
    return value["development"]


def assert_local(repo: Path, value: dict, command):
    assert_public_candidate(value["candidate"])
    current = command(["git", "rev-parse", "HEAD"], "check-head", cwd=repo)
    if current != value["candidate"]["localCommit"]:
        raise ReleaseError("Local HEAD changed after validation", code=2)
    if any(not (repo / p).is_file() or sha((repo / p).read_bytes()) != digest for p, digest in value["candidate"]["files"].items()):
        raise ReleaseError("Local candidate files changed after validation", code=2)
    if changed_paths(lambda *args: command(["git", *args], "check-worktree", cwd=repo)):
        raise ReleaseError("Working tree has new unreviewed files after validation", code=2)
