"""Advisory controller-release check: is a published node release usable by this controller?

The node is optional and released on its own line, so the answer only becomes a
warning. "Usable" follows the controller's own selection (app/servers/releases.py):
a node-v<version> release the controller's users take (releases; beta and rc too
for a beta or rc controller), whose manifest speaks a protocol in the controller's
compat.PROTOCOL range and lists every REQUIRED_FEATURES entry. Both are read from
the checkout being released, without importing the app. Candidates come the way the
controller finds them: every node-v tag (all pages), newest first, each release
fetched by its tag, so a long controller release history never hides a node release.
"""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

from .versions import tagged, versioning

TAG_PREFIX = "node-v"
MAX_CANDIDATES = 20


def requirements(repo: Path) -> tuple[tuple[int, int], tuple[str, ...]]:
    """(protocol range, required features) of the controller in repo."""
    path = Path(repo) / "app/servers/compat.py"
    spec = importlib.util.spec_from_file_location("openbear_release_compat", path)
    compat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compat)
    low, high = (int(x) for x in compat.PROTOCOL)
    features: tuple[str, ...] = ()
    releases = Path(repo) / "app/servers/releases.py"
    if releases.is_file():
        for node in ast.parse(releases.read_text(encoding="utf-8")).body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "REQUIRED_FEATURES"):
                features = tuple(str(name) for name in ast.literal_eval(node.value))
    return (low, high), features


def problem(manifest, protocol: tuple[int, int], features: tuple[str, ...]) -> str:
    """Why a release manifest cannot be used by this controller, or ''."""
    top = manifest.get("protocol") if isinstance(manifest, dict) else None
    low = manifest.get("protocolMin", top) if isinstance(manifest, dict) else None
    if type(top) is not int or type(low) is not int or not 1 <= low <= top:
        return "no protocol"
    if max(low, protocol[0]) > min(top, protocol[1]):
        return f"protocol {low}..{top}, controller speaks {protocol[0]}..{protocol[1]}"
    listed = manifest.get("features")
    if not isinstance(listed, list):
        return "manifest lists no features"
    missing = [name for name in features if name not in listed]
    return "lacks " + ", ".join(missing) if missing else ""


def check(repo: Path, github, prerelease: bool) -> dict:
    """{"ok", "node", "warning", "checked"}; never raises: this must not stop a release."""
    taken = "release, beta or rc" if prerelease else "release"
    try:
        protocol, features = requirements(repo)
        offered = sorted((v for v in tagged(github, TAG_PREFIX) if versioning.offered(v, prerelease)),
                         key=lambda v: versioning.parse(v).key, reverse=True)
        checked = []
        for version in offered[:MAX_CANDIDATES]:
            # A draft (a node release still being published) has no release by tag yet.
            row = github.api(f"/releases/tags/{TAG_PREFIX}{version}", missing=True)
            if not isinstance(row, dict) or row.get("draft"):
                checked.append(f"{version}: tag without a published release")
                continue
            asset = next((a for a in row.get("assets") or [] if isinstance(a, dict) and a.get("name") == "manifest.json"), None)
            if asset is None:
                checked.append(f"{version}: no manifest.json")
                continue
            manifest = json.loads(github.download_asset(asset))
            reason = "manifest version differs" if manifest.get("version") != version else problem(manifest, protocol, features)
            if not reason:
                return {"ok": True, "node": version, "warning": "", "checked": checked}
            checked.append(f"{version}: {reason}")
        warning = (f"No published node {taken} works with this controller (protocol {protocol[0]}..{protocol[1]}, "
                   f"features {', '.join(features) or 'none'}); releasing anyway, the node is optional")
        return {"ok": False, "node": None, "warning": warning, "checked": checked}
    except Exception as exc:  # noqa: BLE001 - advisory: any failure is reported, never fatal
        return {"ok": False, "node": None, "warning": f"Could not check published node releases ({type(exc).__name__}: {exc})",
                "checked": []}
