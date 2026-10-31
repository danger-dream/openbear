"""Version rules for both release lines, from app/versioning.py (loaded by path: the
release scripts run from the checkout without importing the app package).

Only beta, rc and release versions are published. A target must be newer than every
version already published on its line (tags v<version> or node-v<version>); beta and
rc are GitHub pre-releases; after publishing, the local version moves to the next
minor's dev.1.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from .common import ReleaseError


def _load():
    path = Path(__file__).resolve().parents[2] / "app" / "versioning.py"
    spec = importlib.util.spec_from_file_location("openbear_release_versioning", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


versioning = _load()
# A version as written in files, tags and run IDs (no leading v).
PATTERN = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:dev|beta|rc)\.[1-9]\d*)?"
RUN_ID_RE = re.compile(PATTERN + r"-[0-9a-f]{12}\Z")
PAGE_SIZE = 100
MAX_PAGES = 100


def written(value) -> bool:
    """value is a version in its canonical written form (what files and tags hold)."""
    parsed = versioning.parse(value) if isinstance(value, str) else None
    return parsed is not None and str(parsed) == value


def target(value, kind="Version") -> str:
    """A version this release may publish, or ReleaseError."""
    if not written(value):
        raise ReleaseError(f"{kind} must be X.Y.Z, X.Y.Z-rc.N or X.Y.Z-beta.N")
    if not versioning.publishable(value):
        raise ReleaseError(f"{kind} {value} is a development version; only beta, rc and release versions are published")
    return value


def prerelease(value) -> bool:
    """beta and rc are GitHub pre-releases; a release version is a normal release."""
    return versioning.parse(value).stage != "release"


def pages(github, path: str, key=repr) -> list:
    """Every row of a GitHub list, page by page (per_page=100, page 1, 2, ...) until a
    short page or one that adds nothing new: /git/matching-refs ignores the paging
    parameters and answers every matching ref at once, on every page. Rows are told
    apart by key(row). A list longer than MAX_PAGES pages is an error, never a silently
    partial answer."""
    rows, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        found = github.api(f"{path}{'&' if '?' in path else '?'}per_page={PAGE_SIZE}&page={page}") or []
        if not isinstance(found, list):
            raise ReleaseError(f"GitHub GET {path}: expected a list")
        new = [row for row in found if key(row) not in seen]
        seen.update(key(row) for row in new)
        rows.extend(new)
        if len(found) < PAGE_SIZE or not new:
            return rows
    raise ReleaseError(f"GitHub GET {path}: more than {MAX_PAGES * PAGE_SIZE} entries")


def tagged(github, prefix: str) -> set[str]:
    """Every beta, rc and release version tagged on this line (prefix v or node-v), all
    pages read. Other tag shapes, and a development tag nobody should have pushed, are
    ignored (the controller's node selection, app/servers/releases.py, ignores them too)."""
    found = set()
    def ref_of(row):
        return str(row.get("ref", "") if isinstance(row, dict) else "")

    for ref in pages(github, "/git/matching-refs/tags/" + prefix, key=ref_of):
        name = ref_of(ref).removeprefix("refs/tags/")
        if name.startswith(prefix) and written(name[len(prefix):]) and versioning.publishable(name[len(prefix):]):
            found.add(name[len(prefix):])
    return found


def published(github, prefix: str) -> list[str]:
    """tagged(), oldest first."""
    return sorted(tagged(github, prefix), key=lambda v: versioning.parse(v).key)


def require_newer(value: str, versions: list[str], prefix: str) -> None:
    if versions and versioning.compare(value, versions[-1]) <= 0:
        raise ReleaseError(f"Version {value} must be newer than the latest published {prefix}{versions[-1]}")


def changelog_base(value: str, versions: list[str]) -> str | None:
    """The version a release's Full Changelog starts from: for a release, the previous
    release (what its users had); for beta and rc, the latest published version."""
    earlier = [v for v in versions if versioning.compare(v, value) < 0]
    if not prerelease(value):
        releases = [v for v in earlier if not prerelease(v)]
        earlier = releases or earlier
    return earlier[-1] if earlier else None
