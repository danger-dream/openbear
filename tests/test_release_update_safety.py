from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.config import StorageConfig
from app.update.loader import load_updater

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "release_validation.py"
INSTALL_PATH = ROOT / "scripts" / "install.sh"


def validation_module():
    spec = importlib.util.spec_from_file_location("test_release_validation", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_release(root: Path, version: str = "2.0.0") -> Path:
    (root / "app").mkdir(parents=True)
    (root / "web" / "dist" / "assets").mkdir(parents=True)
    (root / "prompts").mkdir()
    (root / "scripts").mkdir()
    (root / "app" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "app" / "main.py").write_text('VALUE = "new"\n', encoding="utf-8")
    (root / "app" / "marker").write_text("new", encoding="utf-8")
    (root / "prompts" / "openbear-system.tpl").write_text("prompt\n", encoding="utf-8")
    shutil.copy2(VALIDATOR_PATH, root / "scripts" / "release_validation.py")
    (root / "scripts" / "updater.py").write_text("# updater\n", encoding="utf-8")
    (root / "scripts" / "install.sh").write_text("# installer\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "openbear"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "uv.lock").write_text(
        f'version = 1\nrevision = 3\nrequires-python = ">=3.11"\n\n'
        f'[[package]]\nname = "openbear"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "openbear.service").write_text(
        "[Service]\nWorkingDirectory=__OPENBEAR_DIR__\nExecStart=__OPENBEAR_DIR__/.venv/bin/python -m app.main\n",
        encoding="utf-8",
    )
    (root / "openbear.json.example").write_text("{}\n", encoding="utf-8")
    (root / "release-meta.json").write_text(
        json.dumps({"schema": 1, "version": version, "requiresRestart": True}) + "\n",
        encoding="utf-8",
    )
    (root / "web" / "dist" / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/app-aaaa1111.js"></script>'
        '<link rel="stylesheet" href="/assets/app-aaaa1111.css">',
        encoding="utf-8",
    )
    (root / "web" / "dist" / "assets" / "app-aaaa1111.js").write_text(
        'import("./chunk-bbbb2222.js");\n', encoding="utf-8"
    )
    (root / "web" / "dist" / "assets" / "chunk-bbbb2222.js").write_text(
        "export default 1;\n", encoding="utf-8"
    )
    (root / "web" / "dist" / "assets" / "app-aaaa1111.css").write_text(
        'body{src:url("./font-cccc3333.woff2")}', encoding="utf-8"
    )
    (root / "web" / "dist" / "assets" / "font-cccc3333.woff2").write_bytes(b"font")
    return root


def zip_tree(root: Path, dest: Path) -> str:
    with zipfile.ZipFile(dest, "w") as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root))
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def make_worker(tmp_path: Path, archive: Path, *, sha256: str = "", version: str = "2.0.0"):
    install = tmp_path / "install"
    data = tmp_path / "data"
    install.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    return load_updater().Updater(
        {
            "installRoot": str(install),
            "dataDir": str(data),
            "fromVersion": "1.0.0",
            "toVersion": version,
            "zipPath": str(archive),
            "sha256": sha256,
        }
    )


def test_release_tree_requires_complete_matching_artifact(tmp_path: Path):
    validator = validation_module()
    release = make_release(tmp_path / "release")
    report = validator.validate_release_tree(release, "2.0.0")
    assert report["ok"] is True
    assert report["frontend"] == {"indexReferences": 2, "checkedResources": 4}

    (release / "web" / "dist" / "build-info.json").write_text(
        json.dumps({"schema": 1, "version": "2.0.0", "buildId": "0123456789abcdef"}),
        encoding="utf-8",
    )
    assert validator.validate_release_tree(release, "2.0.0")["versions"]["web/dist/build-info.json"] == "2.0.0"
    (release / "web" / "dist" / "build-info.json").write_text(
        json.dumps({"schema": 1, "version": "9.0.0", "buildId": "0123456789abcdef"}),
        encoding="utf-8",
    )
    with pytest.raises(validator.ReleaseValidationError, match="版本不一致"):
        validator.validate_release_tree(release, "2.0.0")


def test_release_tree_rejects_app_only_missing_asset_and_version_mismatch(tmp_path: Path):
    validator = validation_module()
    app_only = make_release(tmp_path / "app-only")
    shutil.rmtree(app_only / "web")
    with pytest.raises(validator.ReleaseValidationError, match="web/dist"):
        validator.validate_release_tree(app_only, "2.0.0")

    missing_asset = make_release(tmp_path / "missing-asset")
    (missing_asset / "web" / "dist" / "assets" / "chunk-bbbb2222.js").unlink()
    with pytest.raises(validator.ReleaseValidationError, match="资源引用缺失"):
        validator.validate_release_tree(missing_asset, "2.0.0")

    wrong_version = make_release(tmp_path / "wrong-version")
    (wrong_version / "app" / "__init__.py").write_text('__version__ = "2.0.1"\n', encoding="utf-8")
    with pytest.raises(validator.ReleaseValidationError, match="版本不一致"):
        validator.validate_release_tree(wrong_version, "2.0.0")


def test_updater_requires_checksum_and_validates_before_swap(tmp_path: Path):
    updater = load_updater()
    release = make_release(tmp_path / "release")
    archive = tmp_path / "release.zip"
    digest = zip_tree(release, archive)

    with pytest.raises(updater.UpdateError, match="缺少 SHA256"):
        make_worker(tmp_path / "no-sha", archive)._download_and_verify()

    staged = make_worker(tmp_path / "valid", archive, sha256=digest)._download_and_verify()
    assert (staged / "app" / "main.py").is_file()

    (release / "web" / "dist" / "assets" / "chunk-bbbb2222.js").unlink()
    bad_archive = tmp_path / "missing.zip"
    bad_digest = zip_tree(release, bad_archive)
    with pytest.raises(updater.UpdateError, match="资源引用缺失"):
        make_worker(tmp_path / "missing", bad_archive, sha256=bad_digest)._download_and_verify()

    app_only = make_release(tmp_path / "app-only")
    shutil.rmtree(app_only / "web")
    app_only_archive = tmp_path / "app-only.zip"
    app_only_digest = zip_tree(app_only, app_only_archive)
    with pytest.raises(updater.UpdateError, match="web/dist"):
        make_worker(
            tmp_path / "app-only-worker", app_only_archive, sha256=app_only_digest
        )._download_and_verify()


def test_two_upgrades_keep_only_one_previous_asset_generation(tmp_path: Path):
    validator = validation_module()

    def dist(name: str) -> Path:
        root = tmp_path / name
        (root / "assets").mkdir(parents=True)
        (root / "index.html").write_text(
            f'<script src="/assets/{name}-aaaa1111.js"></script>', encoding="utf-8"
        )
        (root / "assets" / f"{name}-aaaa1111.js").write_text(name, encoding="utf-8")
        return root

    first = dist("first")
    second = dist("second")
    third = dist("third")
    one = validator.retain_previous_assets(second, first)
    assert one["retainedFiles"] == ["first-aaaa1111.js"]
    assert (second / "assets" / "first-aaaa1111.js").is_file()

    two = validator.retain_previous_assets(third, second)
    assert two["retainedFiles"] == ["second-aaaa1111.js"]
    assert (third / "assets" / "second-aaaa1111.js").is_file()
    assert not (third / "assets" / "first-aaaa1111.js").exists()


class _ReadyHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []
    missing_asset = False

    def do_GET(self):  # noqa: N802
        forwarded = self.headers.get("X-Forwarded-Proto", "")
        self.__class__.requests.append((self.path, forwarded))
        if self.path == "/health":
            body = b'{"ok":true,"version":"2.0.0"}'
            content_type = "application/json"
        elif self.path == "/login":
            body = b'<script src="/assets/app-aaaa1111.js"></script>'
            content_type = "text/html"
        elif self.path == "/assets/app-aaaa1111.js" and not self.__class__.missing_asset:
            body = b"console.log(1);" * 200
            content_type = "application/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def test_deployment_probe_checks_login_assets_and_https_forwarding():
    validator = validation_module()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    _ReadyHandler.requests = []
    _ReadyHandler.missing_asset = False
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/health"
    try:
        report = validator.probe_deployment(url, "2.0.0")
        assert report["assets"] == ["/assets/app-aaaa1111.js"]
        assert ("/login", "https") in _ReadyHandler.requests
        assert ("/assets/app-aaaa1111.js", "https") in _ReadyHandler.requests
        _ReadyHandler.missing_asset = True
        with pytest.raises(validator.ReleaseValidationError, match="请求失败"):
            validator.probe_deployment(url, "2.0.0")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def install_library(tmp_path: Path) -> Path:
    text = INSTALL_PATH.read_text(encoding="utf-8")
    assert text.rstrip().endswith('main "$@"')
    text = text.rsplit('main "$@"', 1)[0]
    library = tmp_path / "install-library.sh"
    library.write_text(text, encoding="utf-8")
    return library


@pytest.mark.parametrize(("initial_active", "initial_enabled"), [(1, 1), (0, 0)])
def test_install_upgrade_prepares_before_stop_and_rolls_back(
    tmp_path: Path, initial_active: int, initial_enabled: int
):
    install = tmp_path / "install"
    package = make_release(tmp_path / "package")
    unit_dir = tmp_path / "systemd"
    fake_bin = tmp_path / "bin"
    events = tmp_path / "events"
    active = tmp_path / "active"
    enabled = tmp_path / "enabled"
    unit_dir.mkdir()
    fake_bin.mkdir()
    active.write_text(str(initial_active), encoding="utf-8")
    enabled.write_text(str(initial_enabled), encoding="utf-8")

    (install / "app").mkdir(parents=True)
    (install / "app" / "marker").write_text("old", encoding="utf-8")
    (install / "app" / "old-only.py").write_text("old", encoding="utf-8")
    (install / "web" / "dist" / "assets").mkdir(parents=True)
    (install / "web" / "dist" / "index.html").write_text("old", encoding="utf-8")
    (install / "web" / "dist" / "assets" / "old-aaaa1111.js").write_text("old", encoding="utf-8")
    for directory in ("prompts", "scripts", "data", "workspace", "skills", "mcp-servers"):
        (install / directory).mkdir(exist_ok=True)
    (install / "data" / "keep").write_text("data", encoding="utf-8")
    with sqlite3.connect(install / "data" / "openbear.db") as conn:
        conn.execute("CREATE TABLE persistent_marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO persistent_marker(value) VALUES ('old-database')")
    (install / "workspace" / "keep").write_text("workspace", encoding="utf-8")
    (install / "skills" / "keep").write_text("skills", encoding="utf-8")
    (install / "mcp-servers" / "keep").write_text("mcp", encoding="utf-8")
    (install / "openbear.json").write_text('{"web":{"port":18961}}', encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock", "openbear.service", "openbear.json.example", "release-meta.json"):
        (install / name).write_text(f"old-{name}", encoding="utf-8")
    (install / ".venv" / "bin").mkdir(parents=True)
    old_python = install / ".venv" / "bin" / "python"
    old_python.write_text("#!/bin/sh\necho old-python\n", encoding="utf-8")
    old_python.chmod(0o755)
    (unit_dir / "openbear.service").write_text("old-unit", encoding="utf-8")

    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/bash\n"
        f'echo "prepare:$(cat {install}/app/marker)" >> {events}\n'
        'mkdir -p "$(dirname "$UV_PROJECT_ENVIRONMENT")"\n'
        f'ln -s {ROOT / ".venv"} "$UV_PROJECT_ENVIRONMENT"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    library = install_library(tmp_path)
    harness = tmp_path / "harness.sh"
    harness.write_text(
        f"""#!/bin/bash
set -euo pipefail
source {library}
INSTALL_DIR={install}
SYSTEMD_UNIT_DIR={unit_dir}
SERVICE_NAME=openbear.service
RELEASE_VERSION=2.0.0
PATH={fake_bin}:$PATH
systemctl() {{
    local cmd="$1"; shift || true
    case "$cmd" in
        is-active) [[ "$(cat {active})" == 1 ]] ;;
        is-enabled) [[ "$(cat {enabled})" == 1 ]] ;;
        stop) echo "stop:$(cat {install}/app/marker 2>/dev/null || echo missing)" >> {events}; echo 0 > {active} ;;
        start|restart) echo "$cmd:$(cat {install}/app/marker 2>/dev/null || echo missing)" >> {events}; echo 1 > {active} ;;
        enable) echo 1 > {enabled} ;;
        disable) echo 0 > {enabled} ;;
        daemon-reload) echo "daemon:$(cat {install}/app/marker 2>/dev/null || echo missing)" >> {events} ;;
        *) return 0 ;;
    esac
}}
verify_web_ready() {{ echo "verify:$(cat {install}/app/marker)" >> {events}; return 1; }}
validate_release_package {package}
prepare_release_upgrade {package}
[[ "$(cat {install}/app/marker)" == old ]]
[[ "$(cat {active})" == {initial_active} ]]
if commit_release_upgrade "$RELEASE_TXN"; then
    echo unexpected-success >&2
    exit 2
fi
""",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

    lines = events.read_text(encoding="utf-8").splitlines()
    assert lines.index("prepare:old") < lines.index("stop:old")
    assert "daemon:new" in lines
    assert "verify:new" in lines
    assert (install / "app" / "marker").read_text(encoding="utf-8") == "old"
    assert (install / "app" / "old-only.py").is_file()
    assert not (install / "app" / "main.py").exists()
    assert (install / ".venv" / "bin" / "python").read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert (unit_dir / "openbear.service").read_text(encoding="utf-8") == "old-unit"
    assert (install / "openbear.json").read_text(encoding="utf-8") == '{"web":{"port":18961}}'
    assert (install / "data" / "keep").read_text(encoding="utf-8") == "data"
    database_backups = list((install / "data" / "backups").glob("database-*.sqlite3"))
    assert len(database_backups) == 1
    assert stat.S_IMODE(database_backups[0].stat().st_mode) == 0o600
    with sqlite3.connect(database_backups[0]) as conn:
        assert conn.execute("SELECT value FROM persistent_marker").fetchone()[0] == "old-database"
    assert not database_backups[0].is_relative_to(install / ".openbear-install-transaction")
    assert (install / "workspace" / "keep").read_text(encoding="utf-8") == "workspace"
    assert (install / "skills" / "keep").read_text(encoding="utf-8") == "skills"
    assert (install / "mcp-servers" / "keep").read_text(encoding="utf-8") == "mcp"
    assert active.read_text(encoding="utf-8").strip() == str(initial_active)
    assert enabled.read_text(encoding="utf-8").strip() == str(initial_enabled)


def test_restore_stop_failure_keeps_running_files_and_transaction(tmp_path: Path):
    install = tmp_path / "install"
    txn = install / ".openbear-install-transaction"
    unit_dir = tmp_path / "systemd"
    (install / "app").mkdir(parents=True)
    txn.mkdir()
    unit_dir.mkdir()
    (install / "app" / "marker").write_text("new-running", encoding="utf-8")
    (txn / "old-code.tgz").write_text("only-backup", encoding="utf-8")
    library = install_library(tmp_path)
    harness = tmp_path / "restore-stop-fails.sh"
    harness.write_text(
        f"""#!/bin/bash
set -euo pipefail
source {library}
INSTALL_DIR={install}
SYSTEMD_UNIT_DIR={unit_dir}
SERVICE_NAME=openbear.service
RELEASE_SERVICE_WAS_ACTIVE=1
RELEASE_SERVICE_WAS_ENABLED=1
systemctl() {{
    [[ "$1" != stop ]]
}}
if restore_release_upgrade {txn} injected; then
    exit 2
fi
if prepare_release_upgrade /unused-package; then
    exit 3
fi
""",
        encoding="utf-8",
    )
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "app" / "marker").read_text(encoding="utf-8") == "new-running"
    assert (txn / "old-code.tgz").read_text(encoding="utf-8") == "only-backup"
    assert "未删除任何当前文件" in result.stderr
    assert "拒绝覆盖恢复材料" in result.stderr


def test_tar_restore_failure_keeps_only_backup_transaction(tmp_path: Path):
    install = tmp_path / "install"
    txn = install / ".openbear-install-transaction"
    unit_dir = tmp_path / "systemd"
    (install / "app").mkdir(parents=True)
    txn.mkdir()
    unit_dir.mkdir()
    (install / "app" / "marker").write_text("new", encoding="utf-8")
    (txn / "old-code.tgz").write_text("only-backup", encoding="utf-8")
    library = install_library(tmp_path)
    harness = tmp_path / "tar-restore-fails.sh"
    harness.write_text(
        f"""#!/bin/bash
set -euo pipefail
source {library}
INSTALL_DIR={install}
SYSTEMD_UNIT_DIR={unit_dir}
SERVICE_NAME=openbear.service
RELEASE_SERVICE_WAS_ACTIVE=1
RELEASE_SERVICE_WAS_ENABLED=1
systemctl() {{ return 0; }}
tar() {{
    if [[ "$1" == -xzf ]]; then return 1; fi
    command tar "$@"
}}
if restore_release_upgrade {txn} injected; then
    exit 2
fi
""",
        encoding="utf-8",
    )
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (install / "app").exists()
    assert (txn / "old-code.tgz").read_text(encoding="utf-8") == "only-backup"
    assert "唯一备份保留" in result.stderr


def test_install_apply_rejects_app_only_before_switch(tmp_path: Path):
    install = tmp_path / "install"
    package = make_release(tmp_path / "package")
    shutil.rmtree(package / "web")
    install.mkdir()
    (install / "marker").write_text("old", encoding="utf-8")
    library = install_library(tmp_path)
    harness = tmp_path / "app-only.sh"
    harness.write_text(
        f"""#!/bin/bash
set -euo pipefail
source {library}
INSTALL_DIR={install}
RELEASE_VERSION=2.0.0
if apply_release_tree {package}; then
    exit 2
fi
""",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "marker").read_text(encoding="utf-8") == "old"


def test_install_release_path_rejects_missing_checksum_before_switch(tmp_path: Path):
    install = tmp_path / "install"
    install.mkdir()
    (install / "marker").write_text("old", encoding="utf-8")
    library = install_library(tmp_path)
    harness = tmp_path / "missing-sha.sh"
    harness.write_text(
        f"""#!/bin/bash
set -euo pipefail
source {library}
INSTALL_DIR={install}
MODE=upgrade
RELEASE_VERSION=2.0.0
RELEASE_TAG=v2.0.0
RELEASE_ZIP_URL=https://invalid.test/openbear-2.0.0.zip
RELEASE_SUMS_URL=
curl() {{
    local out="" prev=""
    for arg in "$@"; do
        if [[ "$prev" == -o ]]; then out="$arg"; fi
        prev="$arg"
    done
    : > "$out"
}}
sync_code_release
""",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TMPDIR"] = str(tmp_path)
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "缺少 SHA256SUMS" in result.stderr
    assert (install / "marker").read_text(encoding="utf-8") == "old"


def test_sqlite_backup_captures_wal_custom_path_and_private_unique_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    validator = validation_module()
    assert validator.DEFAULT_STORAGE_DB_PATH == StorageConfig().db_path
    install = tmp_path / "install"
    backup_dir = install / "data" / "backups"
    database = install / "state" / "custom.db"
    database.parent.mkdir(parents=True)
    install.mkdir(exist_ok=True)
    (install / "openbear.json").write_text(
        json.dumps({"storage": {"dbPath": "./state/custom.db"}}), encoding="utf-8"
    )

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        writer.execute("INSERT INTO marker(value) VALUES ('from-wal')")
        writer.commit()
        assert database.with_name(database.name + "-wal").is_file()

        first = validator.create_sqlite_backup(install, backup_dir, label="v1-to-v2")
        second = validator.create_sqlite_backup(install, backup_dir, label="v1-to-v2")
    finally:
        writer.close()

    first_path = Path(first["backupPath"])
    second_path = Path(second["backupPath"])
    assert first["databasePath"] == str(database.resolve())
    assert first_path != second_path
    assert first_path.parent == backup_dir.resolve()
    assert not first_path.is_relative_to(install / ".openbear-install-transaction")
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o600
    with sqlite3.connect(first_path) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "from-wal"
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    default_install = tmp_path / "default-install"
    (default_install / "data").mkdir(parents=True)
    (default_install / "openbear.json").write_text("{}", encoding="utf-8")
    with sqlite3.connect(default_install / "data" / "openbear.db") as conn:
        conn.execute("CREATE TABLE fallback_default (value INTEGER)")
    default = validator.create_sqlite_backup(default_install)
    assert default["created"] is True
    assert default["databasePath"] == str((default_install / "data" / "openbear.db").resolve())

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (default_install / "openbear.json").write_text(
        json.dumps({"storage": {"dbPath": "~/outside.db"}}), encoding="utf-8"
    )
    expanded_database = home / "outside.db"
    with sqlite3.connect(expanded_database) as conn:
        conn.execute("CREATE TABLE expanded_path (value TEXT)")
        conn.execute("INSERT INTO expanded_path(value) VALUES ('home-db')")
    assert validator.resolve_storage_db_path(default_install) == expanded_database.resolve()
    expanded = validator.create_sqlite_backup(default_install)
    with sqlite3.connect(expanded["backupPath"]) as conn:
        assert conn.execute("SELECT value FROM expanded_path").fetchone()[0] == "home-db"


def test_web_updater_restart_backs_up_database_before_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    updater = load_updater()
    install = tmp_path / "install"
    database = install / "custom" / "runtime.db"
    data = database.parent
    database.parent.mkdir(parents=True)
    (install / "openbear.json").write_text(
        json.dumps({"storage": {"dbPath": "custom/runtime.db"}}), encoding="utf-8"
    )
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker(value) VALUES ('before-web-update')")
    staging = make_release(tmp_path / "staging")
    worker = updater.Updater(
        {
            "installRoot": str(install),
            "dataDir": str(data),
            "fromVersion": "1.0.0",
            "toVersion": "2.0.0",
        }
    )
    events: list[str] = []
    code_backup = data / "backups" / "code.tgz"
    code_backup.parent.mkdir(parents=True)
    code_backup.write_bytes(b"code")
    monkeypatch.setattr(worker, "_download_and_verify", lambda: staging)
    monkeypatch.setattr(
        updater,
        "classify_trees",
        lambda *_args: {"requiresRestart": True, "effect": "restart", "changed": ["app/main.py"]},
    )
    monkeypatch.setattr(worker, "_backup", lambda: code_backup)

    def stop() -> None:
        events.append("stop")
        worker.stopped = True

    def start() -> None:
        events.append("start")
        worker.stopped = False

    def swap(_staging: Path) -> None:
        events.append("swap")
        assert worker.database_backup_path is not None
        with sqlite3.connect(worker.database_backup_path) as conn:
            assert conn.execute("SELECT value FROM marker").fetchone()[0] == "before-web-update"

    monkeypatch.setattr(worker, "_stop_service", stop)
    monkeypatch.setattr(worker, "_start_service", start)
    monkeypatch.setattr(worker, "_swap", swap)
    monkeypatch.setattr(worker, "_sync_deps", lambda: None)
    monkeypatch.setattr(worker, "_install_unit", lambda: None)
    monkeypatch.setattr(worker, "_wait_health", lambda: None)

    worker._run()
    assert events == ["stop", "swap", "start"]
    result = json.loads(worker.result_path.read_text(encoding="utf-8"))
    backup = install / result["databaseBackupPath"]
    assert backup == worker.database_backup_path
    assert backup.parent == (install / "data" / "backups").resolve()
    assert backup.is_file()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_web_updater_frontend_only_skips_database_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    updater = load_updater()
    install = tmp_path / "install"
    data = install / "data"
    data.mkdir(parents=True)
    (install / "openbear.json").write_text("{}", encoding="utf-8")
    with sqlite3.connect(data / "openbear.db") as conn:
        conn.execute("CREATE TABLE marker (value INTEGER)")
    staging = make_release(tmp_path / "staging")
    worker = updater.Updater(
        {"installRoot": str(install), "dataDir": str(data), "fromVersion": "1.0.0", "toVersion": "2.0.0"}
    )
    monkeypatch.setattr(worker, "_download_and_verify", lambda: staging)
    monkeypatch.setattr(
        updater,
        "classify_trees",
        lambda *_args: {"requiresRestart": False, "effect": "refresh", "changed": ["web/dist/index.html"]},
    )
    monkeypatch.setattr(worker, "_backup", lambda: data / "code.tgz")
    monkeypatch.setattr(worker, "_stop_service", lambda: pytest.fail("frontend-only must not stop"))
    monkeypatch.setattr(worker, "_backup_database", lambda: pytest.fail("frontend-only must not backup DB"))
    monkeypatch.setattr(worker, "_swap", lambda _staging: None)
    monkeypatch.setattr(worker, "_sync_deps", lambda: None)
    monkeypatch.setattr(worker, "_wait_health", lambda: None)
    worker._run()
    assert not list((data / "backups").glob("database-*.sqlite3"))


def test_web_updater_database_backup_failure_restores_old_service_without_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    updater = load_updater()
    install = tmp_path / "install"
    data = install / "data"
    data.mkdir(parents=True)
    (install / "openbear.json").write_text("{}", encoding="utf-8")
    (data / "openbear.db").write_bytes(b"not-a-sqlite-database")
    staging = make_release(tmp_path / "staging")
    worker = updater.Updater(
        {"installRoot": str(install), "dataDir": str(data), "fromVersion": "1.0.0", "toVersion": "2.0.0"}
    )
    events: list[str] = []
    code_backup = data / "code.tgz"
    code_backup.write_bytes(b"code")
    monkeypatch.setattr(worker, "_download_and_verify", lambda: staging)
    monkeypatch.setattr(
        updater,
        "classify_trees",
        lambda *_args: {"requiresRestart": True, "effect": "restart", "changed": ["app/main.py"]},
    )
    monkeypatch.setattr(worker, "_backup", lambda: code_backup)

    def stop() -> None:
        events.append("stop")
        worker.stopped = True

    def start() -> None:
        events.append("start")
        worker.stopped = False

    monkeypatch.setattr(worker, "_stop_service", stop)
    monkeypatch.setattr(worker, "_start_service", start)
    monkeypatch.setattr(worker, "_swap", lambda _staging: events.append("swap"))

    assert worker.run() == 1
    assert events == ["stop", "start"]
    assert worker.stopped is False
    result = json.loads(worker.result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert "数据库一致性备份失败" in result["message"]
    assert "旧服务已恢复" in result["message"]
    assert not list((data / "backups").glob("database-*.sqlite3"))


def test_installer_database_backup_failure_restores_active_service_before_switch(tmp_path: Path):
    install = tmp_path / "install"
    txn = install / ".openbear-install-transaction"
    package = make_release(txn / "new")
    (package / ".venv" / "bin").mkdir(parents=True)
    (package / ".venv" / "bin" / "python").symlink_to(Path(sys.executable).resolve())
    (install / "app").mkdir(parents=True)
    (install / "app" / "marker").write_text("old", encoding="utf-8")
    (install / "data").mkdir()
    (install / "data" / "openbear.db").write_bytes(b"not-a-sqlite-database")
    (install / "openbear.json").write_text("{}", encoding="utf-8")
    events = tmp_path / "events"
    library = install_library(tmp_path)
    harness = tmp_path / "backup-failure.sh"
    harness.write_text(
        f'''#!/bin/bash
set -euo pipefail
source {library}
INSTALL_DIR={install}
SERVICE_NAME=openbear.service
RELEASE_VERSION=2.0.0
RELEASE_SERVICE_WAS_ACTIVE=1
RELEASE_REQUIRES_DB_BACKUP=1
systemctl() {{ echo "$1" >> {events}; return 0; }}
if commit_release_upgrade {txn}; then
    exit 2
fi
[[ "$(cat {install}/app/marker)" == old ]]
''',
        encoding="utf-8",
    )
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == ["stop", "start"]
    assert "未切换任何文件" in result.stderr
    assert (install / "app" / "marker").read_text(encoding="utf-8") == "old"
