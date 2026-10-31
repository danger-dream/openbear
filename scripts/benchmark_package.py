#!/usr/bin/env python3
"""Maintain benchmark-package/: our pinned, patched copies of the server benchmark tools.

  prepare TOOL [--commit SHA]   clone the upstream at SHA (default: its newest commit) into
                                workspace/benchmark-work/TOOL and apply the newest version's patch
  build TOOL VERSION            write benchmark-package/TOOL/VERSION from that work tree (its diff
                                becomes openbear.patch) and pin every binary by its SHA256
  check [TOOL]                  every version's files still match its manifest

A tool without an upstream repository has its files written by hand in the version
directory; build then only pins its binaries and writes the manifest.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "benchmark-package"
WORK = ROOT / "workspace" / "benchmark-work"
CACHE = WORK / ".cache"
PATCH = "openbear.patch"
MANIFEST = "manifest.json"


def recipe(tool: str) -> dict:
    path = PACKAGES / tool / "recipe.json"
    if not path.is_file():
        sys.exit(f"no recipe: {path.relative_to(ROOT)}")
    return json.loads(path.read_text())


def version_key(name: str):
    return tuple(int(part) for part in name.lstrip("v").split("."))


def versions(tool: str) -> list[str]:
    base = PACKAGES / tool
    names = [p.name for p in base.iterdir() if p.is_dir() and (p / MANIFEST).is_file()] if base.is_dir() else []
    return sorted(names, key=version_key)


def git(*args, cwd=None, capture=False):
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(tool: str, commit: str | None):
    spec = recipe(tool)
    if not spec.get("repo"):
        sys.exit(f"{tool} has no upstream repository; write its files in the version directory")
    work = WORK / tool
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "--quiet", spec["repo"], str(work))
    if commit:
        git("checkout", "--quiet", commit, cwd=work)
    known = versions(tool)
    patch = PACKAGES / tool / known[-1] / PATCH if known else None
    if patch and patch.is_file():
        try:
            git("apply", "--3way", str(patch), cwd=work)
        except subprocess.CalledProcessError:
            print(f"{patch.relative_to(ROOT)} did not apply cleanly; resolve the conflicts in {work} before build")
    head = git("rev-parse", "HEAD", cwd=work, capture=True)
    print(f"{work} at {head[:12]}" + (f", with {known[-1]}'s patch" if patch and patch.is_file() else ""))


def download(url: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as tmp, urllib.request.urlopen(url, timeout=120) as response:
        shutil.copyfileobj(response, tmp)
    path = Path(tmp.name)
    final = CACHE / sha256(path)
    path.replace(final)
    return final


def pin(binary: dict) -> dict:
    print(f"  pinning {binary['url']}")
    path = download(binary["url"])
    return {**binary, "sha256": path.name, "size": path.stat().st_size}


def build(tool: str, version: str):
    spec = recipe(tool)
    if version_key(version) <= max((version_key(v) for v in versions(tool)), default=()):
        sys.exit(f"{version} is not newer than {versions(tool)[-1]}")
    target = PACKAGES / tool / version
    upstream = None
    if spec.get("repo"):
        work = WORK / tool
        if not (work / ".git").is_dir():
            sys.exit(f"run prepare {tool} first")
        if target.exists():
            sys.exit(f"{target.relative_to(ROOT)} already exists")
        target.mkdir(parents=True)
        diff = git("diff", "HEAD", cwd=work, capture=True)
        if diff:
            (target / PATCH).write_text(diff + "\n")
        for dest, source in spec.get("files", {}).items():
            out = target / dest
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(work / source, out)
        for name in spec.get("keep", []):
            shutil.copy2(work / name, target / Path(name).name)
        upstream = {"repo": spec["repo"], "commit": git("rev-parse", "HEAD", cwd=work, capture=True),
                    "date": git("show", "-s", "--format=%cs", "HEAD", cwd=work, capture=True), "license": spec.get("license", "")}
    elif not target.is_dir():
        sys.exit(f"write {tool}'s files in {target.relative_to(ROOT)} first")
    files = {}
    for dest in spec.get("files", {}):
        path = target / dest
        if not path.is_file():
            sys.exit(f"missing {path.relative_to(ROOT)}")
        files[dest] = sha256(path)
    manifest = {
        "tool": tool,
        "version": version,
        "name": spec["name"],
        "upstream": upstream,
        "platforms": spec["platforms"],
        "entry": spec.get("entry", ""),
        "files": files,
        "binaries": [pin(b) for b in spec.get("binaries", [])],
        "patch": PATCH if (target / PATCH).is_file() else None,
        "built": dt.date.today().isoformat(),
    }
    (target / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {target.relative_to(ROOT)}")


def check(only: str | None) -> int:
    bad = 0
    tools = [only] if only else sorted(p.name for p in PACKAGES.iterdir() if (p / "recipe.json").is_file())
    for tool in tools:
        for version in versions(tool):
            base = PACKAGES / tool / version
            manifest = json.loads((base / MANIFEST).read_text())
            for name, digest in manifest["files"].items():
                path = base / name
                if not path.is_file() or sha256(path) != digest:
                    print(f"{tool} {version}: {name} differs from its manifest")
                    bad += 1
            for binary in manifest["binaries"]:
                if not {"url", "sha256", "dest", "platform"} <= binary.keys():
                    print(f"{tool} {version}: binary entry without url, sha256, dest or platform")
                    bad += 1
    print("ok" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("tool")
    p.add_argument("--commit")
    b = sub.add_parser("build")
    b.add_argument("tool")
    b.add_argument("version")
    c = sub.add_parser("check")
    c.add_argument("tool", nargs="?")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        prepare(args.tool, args.commit)
    elif args.command == "build":
        if not args.version.startswith("v") or len(args.version.lstrip("v").split(".")) != 3:
            sys.exit("version must look like v0.1.0")
        build(args.tool, args.version)
    else:
        return check(args.tool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
