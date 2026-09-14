from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = (
    "app/__init__.py", "pyproject.toml", "uv.lock", "web/package.json", "web/package-lock.json",
)


@pytest.fixture
def version_tree(tmp_path: Path) -> Path:
    for directory in ("app", "scripts", "web"):
        (tmp_path / directory).mkdir()
    shutil.copy2(ROOT / "scripts/bump_version.py", tmp_path / "scripts/bump_version.py")
    (tmp_path / "app/__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openbear"\nversion = "1.2.3"\n', encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "openbear"\nversion = "1.2.3"\n'
        '\n[[package]]\nname = "dependency"\nversion = "1.2.3"\n', encoding="utf-8",
    )
    package = {
        "name": "openbear-web", "version": "1.2.3", "private": True,
        "dependencies": {"dependency": "1.2.3"},
        "devDependencies": {"build-tool": "^4.5.6"},
    }
    lock = {
        "name": "openbear-web", "version": "1.2.3", "lockfileVersion": 3, "requires": True,
        "packages": {
            "": copy.deepcopy(package),
            "node_modules/dependency": {
                "version": "1.2.3", "resolved": "https://example.invalid/dependency.tgz",
                "integrity": "sha512-fixture", "license": "MIT",
            },
            "node_modules/build-tool": {"version": "4.5.6", "dev": True},
        },
    }
    for name, data in (("package.json", package), ("package-lock.json", lock)):
        (tmp_path / "web" / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return tmp_path


def run_bump(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(root / "scripts/bump_version.py"), *args],
        cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=15,
    )


def snapshot(root: Path) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in VERSION_FILES}


def test_check_matching_versions_is_read_only(version_tree: Path):
    before = snapshot(version_tree)
    result = run_bump(version_tree, "--check", "--expect", "1.2.3")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1.2.3"
    assert snapshot(version_tree) == before


@pytest.mark.parametrize("field", ["version", 'packages[""].version'])
@pytest.mark.parametrize("value", ["9.9.9", None])
def test_check_rejects_each_npm_lock_version_drift_or_omission(version_tree: Path, field: str, value):
    path = version_tree / "web/package-lock.json"
    lock = json.loads(path.read_text(encoding="utf-8"))
    target = lock if field == "version" else lock["packages"][""]
    if value is None:
        target.pop("version")
    else:
        target["version"] = value
    path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    before = snapshot(version_tree)
    result = run_bump(version_tree, "--check", "--expect", "1.2.3")
    assert result.returncode == 1
    assert f"web/package-lock.json {field}=" in result.stderr
    assert snapshot(version_tree) == before


def test_check_preserves_expected_version_gate(version_tree: Path):
    before = snapshot(version_tree)
    result = run_bump(version_tree, "--check", "--expect", "9.9.9")
    assert result.returncode == 1
    assert "__version__=1.2.3" in result.stderr and "9.9.9" in result.stderr
    assert snapshot(version_tree) == before


def test_bump_synchronizes_both_npm_lock_versions_without_dependency_changes(version_tree: Path):
    before_lock = json.loads((version_tree / "web/package-lock.json").read_text(encoding="utf-8"))
    before_package = json.loads((version_tree / "web/package.json").read_text(encoding="utf-8"))
    before_uv = (version_tree / "uv.lock").read_text(encoding="utf-8")
    # The real CLI runs only in this disposable tree, never against the checkout.
    result = run_bump(version_tree, "v1.2.4")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1.2.4"
    expected_lock = copy.deepcopy(before_lock)
    expected_lock["version"] = "1.2.4"
    expected_lock["packages"][""]["version"] = "1.2.4"
    assert json.loads((version_tree / "web/package-lock.json").read_text(encoding="utf-8")) == expected_lock
    assert json.loads((version_tree / "web/package.json").read_text(encoding="utf-8")) == {
        **before_package, "version": "1.2.4",
    }
    assert (version_tree / "app/__init__.py").read_text(encoding="utf-8") == '__version__ = "1.2.4"\n'
    assert 'version = "1.2.4"' in (version_tree / "pyproject.toml").read_text(encoding="utf-8")
    assert (version_tree / "uv.lock").read_text(encoding="utf-8") == before_uv.replace(
        'name = "openbear"\nversion = "1.2.3"', 'name = "openbear"\nversion = "1.2.4"',
    )
    check = run_bump(version_tree, "--check", "--expect", "1.2.4")
    assert check.returncode == 0, check.stderr
    after = snapshot(version_tree)
    repeated = run_bump(version_tree, "1.2.4")
    assert repeated.returncode == 0, repeated.stderr
    assert snapshot(version_tree) == after
