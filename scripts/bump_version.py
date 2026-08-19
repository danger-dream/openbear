#!/usr/bin/env python3
"""把 app/__init__.py 的版本同步到 pyproject.toml 和 web/package.json。

用法：
  python scripts/bump_version.py 0.2.0
  python scripts/bump_version.py --check   # 三个文件必须与 __version__ 一致
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT_PATH = ROOT / "app" / "__init__.py"
PYPROJECT_PATH = ROOT / "pyproject.toml"
PACKAGE_PATH = ROOT / "web" / "package.json"
LOCK_PATH = ROOT / "uv.lock"
INIT_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
PYPROJECT_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.M)
LOCK_RE = re.compile(r'(name = "openbear"\nversion = ")[^"]+(")')


def read_init_version() -> str:
    text = INIT_PATH.read_text(encoding="utf-8")
    match = INIT_RE.search(text)
    if not match:
        raise SystemExit(f"无法从 {INIT_PATH} 读取 __version__")
    return match.group(1)


def write_init_version(version: str) -> None:
    text = INIT_PATH.read_text(encoding="utf-8")
    if not INIT_RE.search(text):
        raise SystemExit(f"无法从 {INIT_PATH} 读取 __version__")
    INIT_PATH.write_text(INIT_RE.sub(f'__version__ = "{version}"', text, count=1), encoding="utf-8")


def read_pyproject_version() -> str:
    text = PYPROJECT_PATH.read_text(encoding="utf-8")
    match = PYPROJECT_RE.search(text)
    if not match:
        raise SystemExit(f"无法从 {PYPROJECT_PATH} 读取 version")
    return match.group(1)


def write_pyproject_version(version: str) -> None:
    text = PYPROJECT_PATH.read_text(encoding="utf-8")
    PYPROJECT_PATH.write_text(PYPROJECT_RE.sub(f'version = "{version}"', text, count=1), encoding="utf-8")


def read_package_version() -> str:
    data = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    return str(data.get("version") or "")


def write_package_version(version: str) -> None:
    data = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    data["version"] = version
    PACKAGE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_lock_version() -> str:
    text = LOCK_PATH.read_text(encoding="utf-8")
    match = re.search(r'name = "openbear"\nversion = "([^"]+)"', text)
    if not match:
        raise SystemExit(f"无法从 {LOCK_PATH} 读取 openbear version")
    return match.group(1)


def write_lock_version(version: str) -> None:
    text = LOCK_PATH.read_text(encoding="utf-8")
    new_text, count = LOCK_RE.subn(rf"\g<1>{version}\g<2>", text, count=1)
    if count != 1:
        raise SystemExit(f"无法更新 {LOCK_PATH} 中的 openbear version（匹配 {count} 次）")
    LOCK_PATH.write_text(new_text, encoding="utf-8")


def check(expected: str | None = None) -> int:
    init = read_init_version()
    if expected and init != expected:
        print(f"__version__={init} 与期望 {expected} 不一致", file=sys.stderr)
        return 1
    pyproject = read_pyproject_version()
    package = read_package_version()
    lock = read_lock_version()
    bad = False
    if pyproject != init:
        print(f"pyproject.toml version={pyproject} 与 __version__={init} 不一致", file=sys.stderr)
        bad = True
    if package != init:
        print(f"web/package.json version={package} 与 __version__={init} 不一致", file=sys.stderr)
        bad = True
    if lock != init:
        print(f"uv.lock openbear version={lock} 与 __version__={init} 不一致", file=sys.stderr)
        bad = True
    if bad:
        return 1
    print(init)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", nargs="?", help="要写入的 semver，不含 v")
    parser.add_argument("--check", action="store_true", help="只检查三个文件是否一致")
    parser.add_argument("--expect", default="", help="check 时额外要求等于该版本")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.expect or None)
    if not args.version:
        parser.error("需要版本号，或使用 --check")
    version = args.version[1:] if args.version.startswith("v") else args.version
    write_init_version(version)
    write_pyproject_version(version)
    write_package_version(version)
    write_lock_version(version)
    return check(version)


if __name__ == "__main__":
    raise SystemExit(main())
