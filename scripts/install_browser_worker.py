#!/usr/bin/env python3
"""Validate the bundled Python browser worker; no Node, network, or Chrome startup.

The historical filename is kept for existing installers/updaters.
"""

import argparse
import hashlib
import json
from pathlib import Path


def validate(root):
    folder = root / "app/browser/pyworker/resources"
    for name in (
        "injected.js",
        "bridge.js",
        "keys.json",
        "provenance.json",
        "LICENSE",
        "NOTICE",
        "ThirdPartyNotices.txt",
    ):
        if not (folder / name).is_file():
            raise SystemExit("Browser resource missing: " + name)
    provenance = json.loads((folder / "provenance.json").read_text())
    if hashlib.sha256((folder / "injected.js").read_bytes()).hexdigest() != provenance["sha256"]:
        raise SystemExit("Browser injected resource checksum mismatch")
    json.loads((folder / "keys.json").read_text())
    print("Browser Python CDP worker resources ready (no Node/npm required)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--if-enabled", action="store_true")
    args = parser.parse_args()
    config = args.config or args.root / "openbear.json"
    if args.if_enabled:
        data = json.loads(config.read_text()) if config.exists() else {}
        if not data.get("browser", {}).get("enabled", False):
            return
    validate(args.root)


if __name__ == "__main__":
    main()
