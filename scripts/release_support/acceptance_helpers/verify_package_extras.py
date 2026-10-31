"""Check that an installed tree holds the package's instance and gateway files.

Instance deployment builds from the install root's Dockerfile and the access
gateway ships gateway/bin from it; a package that carries them but an install
that leaves them out passes every other check while both features fail.

The node program is released separately (node-v* tags) and downloaded by the
controller at runtime, so the package must carry no node source or binary.
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

PREFIX = "gateway/bin/"
FILES = ("gateway/README.md", "Dockerfile", ".dockerignore", "docs/instances.md")


def node_content(name: str) -> bool:
    return name.startswith("node/") or name.rsplit("/", 1)[-1].startswith("openbear-node")


def main() -> int:
    root, archive, output = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    checked, errors = [], []
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            name = info.filename
            if node_content(name):
                errors.append(f"package ships node content: {name}")
                continue
            if info.is_dir() or not (name.startswith(PREFIX) or name in FILES):
                continue
            installed = root / name
            if not installed.is_file():
                errors.append(f"missing {name}")
            elif hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(package.read(name)).digest():
                errors.append(f"differs from package: {name}")
            checked.append(name)
    if not any(name.startswith(PREFIX) for name in checked):
        errors.append("package ships no gateway binaries")
    output.write_text(json.dumps({"ok": not errors, "checked": sorted(checked), "errors": errors}, indent=2), encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
