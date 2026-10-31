"""install.sh provides its own pinned uv under the install directory, never in the system."""
from __future__ import annotations

import hashlib
import re
import subprocess
import tarfile
from pathlib import Path

import pytest


from app.update.loader import load_updater

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install.sh"


def test_installer_pins_the_same_uv_as_the_updater():
    text = INSTALL.read_text(encoding="utf-8")
    pins = dict(re.findall(r'^(UV_VERSION|UV_SHA256_X86_64|UV_SHA256_AARCH64)="([^"]+)"$', text, re.M))
    updater = load_updater()
    assert pins == {
        "UV_VERSION": updater.UV_VERSION,
        "UV_SHA256_X86_64": updater.UV_BUILDS["x86_64"][1],
        "UV_SHA256_AARCH64": updater.UV_BUILDS["aarch64"][1],
    }
    assert "/usr/local/bin/uv" not in text and "astral.sh/uv/install.sh" not in text


def _harness(tmp_path: Path, digest: str, mode: str = "fresh", *, offline: bool = False,
             uv_body: str = "echo 'uv 0.11.6 (fake)'") -> Path:
    library = tmp_path / "install-library.sh"
    library.write_text(INSTALL.read_text(encoding="utf-8").rsplit('main "$@"', 1)[0], encoding="utf-8")
    for build in ("uv-x86_64-unknown-linux-musl", "uv-aarch64-unknown-linux-musl"):
        (tmp_path / "src" / build).mkdir(parents=True)
        fake = tmp_path / "src" / build / "uv"
        fake.write_text(f"#!/bin/sh\n{uv_body}\n", encoding="utf-8")
        fake.chmod(0o755)
    archive = tmp_path / "uv.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for build in ("uv-x86_64-unknown-linux-musl", "uv-aarch64-unknown-linux-musl"):
            tar.add(tmp_path / "src" / build, arcname=build)
    digest = digest or hashlib.sha256(archive.read_bytes()).hexdigest()
    script = tmp_path / "run.sh"
    script.write_text(f'''#!/bin/bash
set -euo pipefail
source {library}
UV_SHA256_X86_64={digest}
UV_SHA256_AARCH64={digest}
INSTALL_DIR={tmp_path}/install
MODE={mode}
[[ "$MODE" == fresh ]] || mkdir -p "$INSTALL_DIR"
curl() {{
    local out="" url=""
    while [[ $# -gt 0 ]]; do case "$1" in -o) out="$2"; shift 2 ;; -A) shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac; done
    echo "$url" > {tmp_path}/url
    cp {archive} "$out"
    {'head -c 100 ' + str(archive) + ' > "$out"; return 6' if offline else ''}
}}
if ! download_uv; then echo DOWNLOAD_FAILED; exit 0; fi
temp="$UV_TEMP_DIR"
echo "TEMP_UV=$(command -v uv)"
mkdir -p "$INSTALL_DIR"
persist_uv
echo "OWN_UV=$(command -v uv)"
echo "VERSION=$(uv --version)"
[[ -z "$temp" || ! -e "$temp" ]] && echo TEMP_REMOVED
''', encoding="utf-8")
    return script


@pytest.mark.parametrize("mode", ["fresh", "upgrade"])
def test_installer_downloads_verified_uv_then_keeps_it_in_the_install_directory(tmp_path, mode):
    proc = subprocess.run(["bash", str(_harness(tmp_path, "", mode))], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    own = tmp_path / "install" / ".tools" / "uv" / "uv"
    if mode == "fresh":
        # The empty install directory is left alone until sync_code; never /tmp (often noexec).
        assert Path(lines["TEMP_UV"]).parent.name.startswith(".openbear-uv.")
        assert Path(lines["TEMP_UV"]).parent.parent == tmp_path
    else:
        assert lines["TEMP_UV"] == str(own)
    assert lines["OWN_UV"] == str(own)
    assert lines["VERSION"] == "uv 0.11.6 (fake)"
    assert "TEMP_REMOVED" in proc.stdout and not list(tmp_path.glob(".openbear-uv.*"))
    assert (tmp_path / "install" / ".tools" / ".gitignore").read_text() == "*\n"
    assert re.fullmatch(r"https://github.com/astral-sh/uv/releases/download/0\.11\.6/uv-(x86_64|aarch64)-unknown-linux-musl\.tar\.gz",
                        (tmp_path / "url").read_text().strip())


@pytest.mark.parametrize("case", ["offline", "broken"])
def test_failed_uv_download_leaves_nothing_a_later_run_would_trust(tmp_path, case):
    # A partial archive or a uv that cannot run would otherwise stay in .tools/uv,
    # and the next installer run would pick that uv up and stop on it every time.
    script = _harness(tmp_path, "", "upgrade", offline=case == "offline", uv_body="exit 126" if case == "broken" else "echo 'uv 0.11.6 (fake)'")
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DOWNLOAD_FAILED" in proc.stdout
    assert ("无法下载 uv" if case == "offline" else "无法解压或运行") in proc.stderr
    assert sorted(p.name for p in (tmp_path / "install" / ".tools" / "uv").iterdir()) == []


def test_installer_rejects_a_uv_download_with_the_wrong_checksum(tmp_path):
    proc = subprocess.run(["bash", str(_harness(tmp_path, "0" * 64))], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DOWNLOAD_FAILED" in proc.stdout and "校验和不匹配" in proc.stderr
    assert not (tmp_path / "install").exists() and not list(tmp_path.glob(".openbear-uv.*"))
