from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app import __version__
from app.update.loader import load_updater
from app.update.service import atomic_write_json, data_dir_from_config, read_json


def updater():
    return load_updater()


def test_installed_version_reads_disk():
    from app import __version__, installed_version

    assert installed_version() == __version__


def test_version_files_are_in_sync():
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    package = json.loads((root / "web" / "package.json").read_text(encoding="utf-8"))
    assert f'version = "{__version__}"' in pyproject
    assert package["version"] == __version__


def test_semver_compare_and_newer():
    u = updater()
    assert u.parse_semver("v1.2.3") == (1, 2, 3, "")
    assert u.cmp_semver("0.2.0", "0.1.9") > 0
    assert u.cmp_semver("1.0.0-rc.1", "1.0.0") < 0
    assert u.is_newer("0.2.0", "0.1.0")
    assert not u.is_newer("0.1.0", "0.1.0")
    assert not u.is_newer("0.1.0-rc.1", "0.1.0")


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("app/main.py", "restart"),
        ("pyproject.toml", "restart"),
        ("uv.lock", "restart"),
        ("openbear.service", "restart"),
        ("web/dist/index.html", "refresh"),
        ("web/src/App.vue", "refresh"),
        ("prompts/openbear-system.tpl", "noop"),
        ("scripts/install.sh", "noop"),
        ("README.md", "noop"),
        ("data/openbear.db", "ignore"),
        ("openbear.json", "ignore"),
        ("mystery.bin", "restart"),
    ],
)
def test_classify_path(path, kind):
    assert updater().classify_path(path) == kind


def test_classify_names_dist_only_is_refresh():
    result = updater().classify_names(["web/dist/assets/app.js", "web/dist/index.html"])
    assert result["requiresRestart"] is False
    assert result["effect"] == "refresh"


def test_classify_names_backend_is_restart():
    result = updater().classify_names(["web/dist/index.html", "app/main.py"])
    assert result["requiresRestart"] is True
    assert result["effect"] == "restart"


def test_classify_names_version_files_with_dist_is_refresh():
    result = updater().classify_names(["app/__init__.py", "pyproject.toml", "uv.lock", "web/dist/index.html"])
    assert result["requiresRestart"] is False
    assert result["effect"] == "refresh"


def test_classify_trees_compares_digests(tmp_path: Path):
    u = updater()
    current = tmp_path / "current"
    incoming = tmp_path / "incoming"
    (current / "web" / "dist").mkdir(parents=True)
    (incoming / "web" / "dist").mkdir(parents=True)
    (current / "app").mkdir()
    (incoming / "app").mkdir()
    (current / "app" / "main.py").write_text("old\n", encoding="utf-8")
    (incoming / "app" / "main.py").write_text("old\n", encoding="utf-8")
    (current / "web" / "dist" / "index.html").write_text("a", encoding="utf-8")
    (incoming / "web" / "dist" / "index.html").write_text("b", encoding="utf-8")
    result = u.classify_trees(current, incoming)
    assert result["requiresRestart"] is False
    assert result["effect"] == "refresh"
    assert "web/dist/index.html" in result["changed"]

    (incoming / "app" / "main.py").write_text("new\n", encoding="utf-8")
    result = u.classify_trees(current, incoming)
    assert result["requiresRestart"] is True


def test_classify_trees_version_bump_and_dist_is_refresh(tmp_path: Path):
    u = updater()
    current = tmp_path / "current"
    incoming = tmp_path / "incoming"
    for root in (current, incoming):
        (root / "app").mkdir(parents=True)
        (root / "web" / "dist").mkdir(parents=True)
    (current / "app" / "__init__.py").write_text('__version__ = "0.1.2"\n', encoding="utf-8")
    (incoming / "app" / "__init__.py").write_text('__version__ = "0.1.3"\n', encoding="utf-8")
    (current / "pyproject.toml").write_text('name = "openbear"\nversion = "0.1.2"\n', encoding="utf-8")
    (incoming / "pyproject.toml").write_text('name = "openbear"\nversion = "0.1.3"\n', encoding="utf-8")
    (current / "uv.lock").write_text('name = "openbear"\nversion = "0.1.2"\nsource = { virtual = "." }\n', encoding="utf-8")
    (incoming / "uv.lock").write_text('name = "openbear"\nversion = "0.1.3"\nsource = { virtual = "." }\n', encoding="utf-8")
    (current / "web" / "dist" / "index.html").write_text("old", encoding="utf-8")
    (incoming / "web" / "dist" / "index.html").write_text("new", encoding="utf-8")
    result = u.classify_trees(current, incoming)
    assert result["requiresRestart"] is False
    assert result["effect"] == "refresh"
    assert "uv.lock" in result["versionOnly"]

    (incoming / "pyproject.toml").write_text('name = "openbear"\nversion = "0.1.3"\ndependencies = ["x"]\n', encoding="utf-8")
    result = u.classify_trees(current, incoming)
    assert result["requiresRestart"] is True


def test_classify_trees_lock_dependency_change_is_restart(tmp_path: Path):
    u = updater()
    current = tmp_path / "current"
    incoming = tmp_path / "incoming"
    current.mkdir()
    incoming.mkdir()
    (current / "uv.lock").write_text('name = "openbear"\nversion = "0.1.4"\nname = "httpx"\nversion = "0.27.0"\n', encoding="utf-8")
    (incoming / "uv.lock").write_text('name = "openbear"\nversion = "0.1.5"\nname = "httpx"\nversion = "0.28.0"\n', encoding="utf-8")
    result = u.classify_trees(current, incoming)
    assert result["requiresRestart"] is True
    assert result["effect"] == "restart"
    assert "uv.lock" not in result["versionOnly"]


def test_parse_sha256sums():
    text = "abc123  openbear-0.2.0.zip\n"
    assert updater().parse_sha256sums(text, "openbear-0.2.0.zip") == "abc123"


def test_update_result_roundtrip(tmp_path: Path):
    path = tmp_path / "update-result.json"
    atomic_write_json(path, {
        "schema": 1,
        "status": "rolled_back",
        "fromVersion": "0.1.0",
        "toVersion": "0.2.0",
        "message": "health failed",
        "acked": False,
    })
    data = read_json(path)
    assert data["status"] == "rolled_back"
    assert data["message"] == "health failed"


def test_data_dir_from_config(tmp_path: Path):
    config = type("C", (), {"storage": type("S", (), {"db_path": str(tmp_path / "db" / "openbear.db")})()})()
    assert data_dir_from_config(config) == tmp_path / "db"


def test_rollback_stops_running_new_process(tmp_path: Path, monkeypatch):
    u = updater()
    install = tmp_path / "install"
    data = tmp_path / "data"
    install.mkdir()
    data.mkdir()
    backup = data / "code-0.1.0.tgz"
    backup.write_bytes(b"not-a-real-tar")
    calls: list[tuple[str, ...]] = []

    worker = u.Updater({
        "installRoot": str(install),
        "dataDir": str(data),
        "fromVersion": "0.1.0",
        "toVersion": "0.2.0",
        "serviceName": "openbear.service",
        "healthUrl": "http://127.0.0.1:18961/health",
    })
    worker.stopped = False
    worker.backup_path = backup
    worker.log_path = data / "update.log"

    def fake_systemctl(*args, check=True):
        calls.append(args)

    class FakeTar:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extractall(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(worker, "_systemctl", fake_systemctl)
    monkeypatch.setattr(u.tarfile, "open", lambda *_args, **_kwargs: FakeTar())
    monkeypatch.setattr(worker, "_sync_deps", lambda: None)
    monkeypatch.setattr(worker, "_install_unit", lambda: None)
    monkeypatch.setattr(u, "_probe_health", lambda *_a, **_k: (True, '{"ok": true, "version": "0.1.0"}'))

    worker._rollback("health failed")
    assert calls[0] == ("stop", "openbear.service")
    assert any(item[:1] == ("start",) for item in calls)
    result = json.loads((data / "update-result.json").read_text(encoding="utf-8"))
    assert result["status"] == "rolled_back"


def test_updater_module_is_stdlib_only():
    path = Path(__file__).resolve().parents[1] / "scripts" / "updater.py"
    spec = importlib.util.spec_from_file_location("standalone_updater_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SCHEMA == 1


def test_sync_deps_skips_when_lock_unchanged(tmp_path: Path):
    u = updater()
    install = tmp_path / "install"
    install.mkdir()
    (install / "uv.lock").write_text("lock\n", encoding="utf-8")
    worker = u.Updater({
        "installRoot": str(install),
        "dataDir": str(tmp_path / "data"),
        "fromVersion": "0.1.0",
        "toVersion": "0.1.1",
        "serviceName": "openbear.service",
        "healthUrl": "http://127.0.0.1:18961/health",
    })
    worker.classification = {"changed": ["pyproject.toml", "app/__init__.py"]}
    called = []
    worker.log = lambda message: called.append(message)
    worker._sync_deps()
    assert called == []


def test_sync_deps_skips_when_lock_is_version_only(tmp_path: Path):
    u = updater()
    install = tmp_path / "install"
    install.mkdir()
    (install / "uv.lock").write_text("lock\n", encoding="utf-8")
    worker = u.Updater({
        "installRoot": str(install),
        "dataDir": str(tmp_path / "data"),
        "fromVersion": "0.1.4",
        "toVersion": "0.1.5",
        "serviceName": "openbear.service",
        "healthUrl": "http://127.0.0.1:18961/health",
    })
    worker.classification = {
        "changed": ["app/__init__.py", "pyproject.toml", "uv.lock", "web/dist/index.html"],
        "versionOnly": ["app/__init__.py", "pyproject.toml", "uv.lock"],
    }
    called = []
    worker.log = lambda message: called.append(message)
    worker._sync_deps()
    assert called == []


def test_resolve_uv_finds_home_local_bin(tmp_path: Path, monkeypatch):
    u = updater()
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "uv"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    found = u.resolve_uv({"HOME": str(tmp_path), "PATH": "/usr/bin"})
    assert found == str(fake)


async def test_release_assets_follow_github_redirects():
    import httpx

    from app.update.service import UpdateService

    svc = type(
        "S",
        (),
        {
            "config": type("C", (), {"storage": type("St", (), {"db_path": "./data/openbear.db"})()})(),
            "bot": None,
        },
    )()
    us = UpdateService(svc)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://github.com/danger-dream/openbear/releases/download/v0.1.0/SHA256SUMS":
            return httpx.Response(302, headers={"Location": "https://objects.test/SHA256SUMS"})
        if url == "https://objects.test/SHA256SUMS":
            return httpx.Response(200, text="deadbeef  openbear-0.1.0.zip\n")
        if url == "https://github.com/danger-dream/openbear/releases/download/v0.1.0/release-meta.json":
            return httpx.Response(302, headers={"Location": "https://objects.test/release-meta.json"})
        if url == "https://objects.test/release-meta.json":
            return httpx.Response(200, json={"requiresRestart": False, "comparedWith": "0.0.1"})
        return httpx.Response(404)

    us._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        available = await us._release_to_available({
            "tag_name": "v0.1.0",
            "name": "v0.1.0",
            "body": "notes",
            "published_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/danger-dream/openbear/releases/tag/v0.1.0",
            "assets": [
                {
                    "name": "openbear-0.1.0.zip",
                    "browser_download_url": "https://github.com/danger-dream/openbear/releases/download/v0.1.0/openbear-0.1.0.zip",
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": "https://github.com/danger-dream/openbear/releases/download/v0.1.0/SHA256SUMS",
                },
                {
                    "name": "release-meta.json",
                    "browser_download_url": "https://github.com/danger-dream/openbear/releases/download/v0.1.0/release-meta.json",
                },
            ],
        })
    finally:
        await us._http.aclose()

    assert available["sha256"] == "deadbeef"
    assert available["requiresRestart"] is False
    assert available["comparedWith"] == "0.0.1"
