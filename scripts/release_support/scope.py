"""Release change scope: exact source comparison, never an LLM/test-name guess."""
from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path

from .common import ReleaseError, fingerprint, sha

# These Python modules exercise actual web source/resources or version contracts.
# Changing any Python test/config invalidates the backend evidence, so an unknown
# new cross-language test cannot be introduced while keeping a cached matrix.
FRONTEND_CHECKS = (
    "tests/test_pwa_release_validation.py", "tests/test_pwa_static.py",
    "tests/test_reference_contracts.py", "tests/test_web_uploads.py",
    "tests/test_conversation_restart.py", "tests/test_tool_input_agent_progress.py",
    "tests/test_folder_run_defaults.py", "tests/test_update.py", "tests/test_bump_version.py",
)
# These surfaces can alter the contract of otherwise unchanged frontend source.
SHARED_PREFIXES = ("app/web_console/", "app/models/", "app/db/", "app/update/", "scripts/", "tests/", "prompts/")
SHARED_FILES = {"app/web_admin.py", "app/config.py", "app/references.py", "app/events.py",
                "pyproject.toml", "uv.lock", "openbear.service"}
POLICY = 2


def normalized(name: str, data: bytes) -> bytes:
    """Only the project's own version is metadata; dependency versions stay real."""
    if name == "app/__init__.py":
        return re.sub(rb'^__version__\s*=\s*[\"\'][^\"\']+[\"\']', b'__version__ = ""', data, flags=re.M)
    if name in {"web/package.json", "web/package-lock.json"}:
        value = json.loads(data)
        value["version"] = "<release>"
        if name.endswith("package-lock.json"):
            value["packages"][""]["version"] = "<release>"
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if name in {"pyproject.toml", "uv.lock"}:
        value = tomllib.loads(data.decode())
        if name == "pyproject.toml":
            value["project"]["version"] = "<release>"
        else:
            own = [p for p in value.get("package", []) if p.get("name") == "openbear"]
            if len(own) != 1:
                raise ReleaseError("Expected one openbear project in uv.lock")
            own[0]["version"] = "<release>"
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return data


def frontend_input(name: str) -> bool:
    # Only standalone Python test modules and non-shared backend implementation
    # are outside the Node test/build contract. Fixtures, conftest, scripts,
    # shared APIs, dependencies and all unknown paths remain conservative inputs.
    if re.fullmatch(r"tests/test_[^/]+\.py", name):
        return False
    if name.startswith("app/") and name not in SHARED_FILES and not name.startswith(SHARED_PREFIXES):
        return False
    return True


def identities(files: dict[str, bytes]) -> dict:
    hashes = {name: sha(normalized(name, data)) for name, data in sorted(files.items())}
    return {"files": hashes,
            "frontend": fingerprint({n: h for n, h in hashes.items() if n.startswith("web/")}),
            "backend": fingerprint({n: h for n, h in hashes.items() if not n.startswith("web/")}),
            "frontendStage": fingerprint({n: h for n, h in hashes.items() if frontend_input(n)}),
            # The established release policy splits web-dependent Python
            # coverage into FRONTEND_CHECKS when this non-web matrix is reused.
            "backendStage": fingerprint({n: h for n, h in hashes.items() if not n.startswith("web/")})}


def compare(previous: dict[str, bytes], current: dict[str, bytes], *, previous_commit: str) -> dict:
    old, new = identities(previous), identities(current)
    changed = sorted(n for n in old["files"].keys() | new["files"].keys() if old["files"].get(n) != new["files"].get(n))
    frontend = old["frontend"] != new["frontend"]
    backend = old["backend"] != new["backend"]
    unknown = [n for n in changed if not n.startswith(("web/", "app/", *SHARED_PREFIXES)) and n not in SHARED_FILES]
    shared = [n for n in changed if n in SHARED_FILES or n.startswith(SHARED_PREFIXES)]
    kind = "both" if (frontend and backend) or shared or unknown else "frontend" if frontend else "backend" if backend else "metadata"
    return {"schema": 1, "policy": POLICY, "kind": kind, "changed": changed,
            "frontendChanged": frontend, "backendChanged": backend,
            "shared": shared, "unknown": unknown, "previousCommit": previous_commit,
            "previousFrontendFingerprint": old["frontend"], "previousBackendFingerprint": old["backend"],
            "previousFrontendStageFingerprint": old["frontendStage"],
            "frontendFingerprint": new["frontend"], "backendFingerprint": new["backend"],
            "frontendStageFingerprint": new["frontendStage"], "backendStageFingerprint": new["backendStage"],
            "frontendChecks": list(FRONTEND_CHECKS)}


def git_files(source: Path, commit: str) -> dict[str, bytes]:
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ReleaseError("Previous source must be pinned to a commit")
    data = subprocess.check_output(["git", "archive", "--format=tar", commit], cwd=source, timeout=120)
    result = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.isreg():
                raise ReleaseError("Previous source has unsupported non-regular entry: " + member.name)
            result[member.name] = archive.extractfile(member).read()
    return result


def from_checkout(source: Path, candidate: dict, previous_commit: str) -> dict:
    current = {name: (source / name).read_bytes() for name in candidate["files"]}
    return compare(git_files(source, previous_commit), current, previous_commit=previous_commit)


def build_identity(frontend_fingerprint, environment, version):
    return {"policy": POLICY, "source": frontend_fingerprint, "version": version,
            "nodeImage": environment["nodeImage"], "nodeKey": environment["nodeKey"]}


def verified_result(result: dict) -> bool:
    """A cache miss is safe; missing/corrupt evidence is never a passed test."""
    if not result.get("ok") or result.get("unhandled"):
        return False
    counts = result.get("counts", {})
    if not counts.get("tests") or counts.get("failed") != 0 or counts.get("cancelled", 0) or counts.get("todo", 0):
        return False
    outputs = result.get("outputs", {})
    if not outputs.get("log") or not outputs.get("logSha256"):
        return False
    for name in ("log", "junit", "fastJunit", "rerunJunit", "reusedJunit"):
        if name not in outputs:
            continue
        path = Path(outputs[name])
        if not path.is_file() or sha(path.read_bytes()) != outputs.get(name + "Sha256"):
            return False
    return True
