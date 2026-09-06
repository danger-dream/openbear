#!/usr/bin/env python3
"""Validate OpenBear release trees and deployed Web assets (stdlib only).

This module is shared by the standalone updater, ``install.sh``, tests, and the
release workflow.  It intentionally validates the extracted artifact rather
than trusting source-tree build results.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - OpenBear requires Python 3.11+
    tomllib = None  # type: ignore[assignment]

SCHEMA = 1
ASSET_MARKER = ".openbear-assets.json"
USER_AGENT = "OpenBear-Release-Validation/1.0"
DEFAULT_STORAGE_DB_PATH = "./data/openbear.db"

REQUIRED_DIRS = ("app", "web/dist", "prompts", "scripts")
REQUIRED_FILES = (
    "app/__init__.py",
    "web/dist/index.html",
    "pyproject.toml",
    "uv.lock",
    "openbear.service",
    "openbear.json.example",
    "release-meta.json",
    "scripts/updater.py",
    "scripts/install.sh",
    "scripts/release_validation.py",
)
_RESOURCE_EXTENSIONS = (
    "js|mjs|css|wasm|json|ttf|otf|eot|woff|woff2|svg|png|jpe?g|gif|webp|ico|map"
)
_QUOTED_RESOURCE_RE = re.compile(
    rf"(?P<quote>['\"])(?P<ref>(?:/assets/|\./|\.\./)[^'\"\\\s]+\.(?:{_RESOURCE_EXTENSIONS})(?:[?#][^'\"\\\s]*)?)(?P=quote)",
    re.I,
)
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^)'\"\s]+)\1\s*\)", re.I)
_CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?['\"]([^'\"]+)['\"]", re.I)
_INIT_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)


class ReleaseValidationError(ValueError):
    pass


def normalize_version(value: Any) -> str:
    text = str(value or "").strip()
    return text[1:] if text[:1].lower() == "v" else text


def resolve_storage_db_path(install_root: Path, config_path: Path | None = None) -> Path:
    """Resolve storage.dbPath with the same default/cwd meaning as ``app.config``."""
    root = Path(install_root).resolve()
    config = Path(config_path) if config_path is not None else root / "openbear.json"
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReleaseValidationError(f"无法读取数据库配置 {config}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseValidationError(f"数据库配置不是 JSON 对象: {config}")
    storage = payload.get("storage")
    if storage is None:
        raw_path = DEFAULT_STORAGE_DB_PATH
    elif not isinstance(storage, dict):
        raise ReleaseValidationError("storage 配置必须是对象")
    elif "dbPath" in storage:
        raw_path = storage.get("dbPath")
    elif "db_path" in storage:
        raw_path = storage.get("db_path")
    else:
        raw_path = DEFAULT_STORAGE_DB_PATH
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ReleaseValidationError("storage.dbPath 必须是非空路径")
    database = Path(raw_path).expanduser()
    if not database.is_absolute():
        database = root / database
    return database.resolve()


def create_sqlite_backup(
    install_root: Path,
    backup_dir: Path | None = None,
    *,
    config_path: Path | None = None,
    label: str = "upgrade",
) -> dict[str, Any]:
    """Create a private SQLite online backup that includes committed WAL contents.

    The caller must stop the old service before calling this function.  The SQLite
    backup API is still used instead of copying the main file, so a remaining WAL
    is handled consistently.  A missing database is a clean no-op.
    """
    root = Path(install_root).resolve()
    database = resolve_storage_db_path(root, config_path)
    if not database.exists():
        return {
            "ok": True,
            "created": False,
            "reason": "database_missing",
            "databasePath": str(database),
            "backupPath": "",
        }
    if not database.is_file():
        raise ReleaseValidationError(f"数据库路径不是文件: {database}")

    target_dir = Path(backup_dir) if backup_dir is not None else root / "data" / "backups"
    if not target_dir.is_absolute():
        target_dir = root / target_dir
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_dir = target_dir.resolve()
    transaction_dir = (root / ".openbear-install-transaction").resolve()
    if target_dir == transaction_dir or transaction_dir in target_dir.parents:
        raise ReleaseValidationError("数据库备份目录不能位于安装事务目录内")
    try:
        target_dir.chmod(0o700)
    except OSError as exc:
        raise ReleaseValidationError(f"无法收紧数据库备份目录权限 {target_dir}: {exc}") from exc

    safe_label = re.sub(r"[^0-9A-Za-z._-]+", "-", str(label or "upgrade")).strip(".-") or "upgrade"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fd, raw_backup = tempfile.mkstemp(
        prefix=f"database-{safe_label}-{stamp}-",
        suffix=".sqlite3",
        dir=target_dir,
    )
    os.close(fd)
    backup = Path(raw_backup)
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    failure: Exception | None = None
    try:
        backup.chmod(0o600)
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(str(backup))
        source.backup(destination)
        destination.commit()
        checks = [str(row[0]) for row in destination.execute("PRAGMA quick_check").fetchall()]
        if checks != ["ok"]:
            raise ReleaseValidationError("数据库备份 quick_check 失败: " + "; ".join(checks[:10]))
    except Exception as exc:
        failure = exc
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
    if failure is not None:
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(failure, ReleaseValidationError):
            raise failure
        raise ReleaseValidationError(f"SQLite 一致性备份失败 {database}: {failure}") from failure
    backup.chmod(0o600)
    return {
        "ok": True,
        "created": True,
        "reason": "",
        "databasePath": str(database),
        "backupPath": str(backup),
        "size": backup.stat().st_size,
    }


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): value for key, value in attrs}
        if tag.lower() == "script" and values.get("src"):
            ref = str(values["src"])
            self.references.append(ref)
            self.scripts.append(ref)
        elif tag.lower() == "link" and values.get("href"):
            self.references.append(str(values["href"]))


def index_references(text: str) -> tuple[list[str], list[str]]:
    parser = _IndexParser()
    parser.feed(text)
    return list(dict.fromkeys(parser.references)), list(dict.fromkeys(parser.scripts))


def _clean_reference(reference: str) -> str:
    return urllib.parse.unquote(str(reference or "").strip().split("#", 1)[0].split("?", 1)[0])


def _reference_path(dist: Path, source: Path, reference: str) -> Path | None:
    ref = _clean_reference(reference)
    if not ref or ref.startswith(("data:", "http://", "https://", "//", "#")):
        return None
    if ref.startswith("/assets/"):
        candidate = dist / ref.lstrip("/")
    elif ref.startswith("/"):
        candidate = dist / ref.lstrip("/")
    else:
        candidate = source.parent / ref
    resolved = candidate.resolve()
    dist_resolved = dist.resolve()
    try:
        resolved.relative_to(dist_resolved)
    except ValueError as exc:
        raise ReleaseValidationError(f"前端资源引用越界: {source}: {reference}") from exc
    return resolved


def _text_resource_references(path: Path, text: str) -> Iterable[str]:
    suffix = path.suffix.lower()
    if suffix == ".css":
        for match in _CSS_URL_RE.finditer(text):
            yield match.group(2)
        for match in _CSS_IMPORT_RE.finditer(text):
            yield match.group(1)
    if suffix in {".js", ".mjs", ".css"}:
        for match in _QUOTED_RESOURCE_RE.finditer(text):
            yield match.group("ref")


def validate_frontend_resources(dist: Path) -> dict[str, Any]:
    dist = Path(dist)
    index = dist / "index.html"
    if not index.is_file():
        raise ReleaseValidationError("发行包缺少 web/dist/index.html")
    try:
        html = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReleaseValidationError(f"无法读取 web/dist/index.html: {exc}") from exc
    direct, scripts = index_references(html)
    if not scripts:
        raise ReleaseValidationError("web/dist/index.html 没有本地 script 入口")

    checked: set[str] = set()
    missing: set[str] = set()

    def check(source: Path, ref: str) -> None:
        candidate = _reference_path(dist, source, ref)
        if candidate is None:
            return
        rel = candidate.relative_to(dist.resolve()).as_posix()
        checked.add(rel)
        if not candidate.is_file():
            missing.add(rel)

    for ref in direct:
        check(index, ref)
    for path in sorted(dist.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".js", ".mjs", ".css"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ReleaseValidationError(f"无法读取前端资源 {path}: {exc}") from exc
        for ref in _text_resource_references(path, text):
            check(path, ref)
    if missing:
        preview = ", ".join(sorted(missing)[:20])
        raise ReleaseValidationError(f"前端资源引用缺失 ({len(missing)}): {preview}")
    return {"indexReferences": len(direct), "checkedResources": len(checked)}


def _read_versions(root: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    init_path = root / "app" / "__init__.py"
    if init_path.is_file():
        match = _INIT_VERSION_RE.search(init_path.read_text(encoding="utf-8"))
        if not match:
            raise ReleaseValidationError("app/__init__.py 缺少 __version__")
        versions["app/__init__.py"] = normalize_version(match.group(1))

    pyproject_path = root / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            pyproject_text = pyproject_path.read_text(encoding="utf-8")
            if tomllib is not None:
                project_version = (tomllib.loads(pyproject_text).get("project") or {}).get("version")
            else:  # Python 3.10 compatibility for the bootstrap installer.
                project_block = re.search(r"(?ms)^\[project\]\s*$.*?(?=^\[|\Z)", pyproject_text)
                match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', project_block.group(0) if project_block else "")
                project_version = match.group(1) if match else ""
        except Exception as exc:
            raise ReleaseValidationError(f"pyproject.toml 无效: {exc}") from exc
        versions["pyproject.toml"] = normalize_version(project_version)

    lock_path = root / "uv.lock"
    if lock_path.is_file():
        try:
            lock_text = lock_path.read_text(encoding="utf-8")
            if tomllib is not None:
                lock = tomllib.loads(lock_text)
                own_versions = [
                    item.get("version")
                    for item in lock.get("package") or []
                    if isinstance(item, dict) and item.get("name") == "openbear"
                ]
            else:
                own_versions = []
                for block in re.findall(r"(?ms)^\[\[package\]\]\s*$.*?(?=^\[\[package\]\]|\Z)", lock_text):
                    if re.search(r'(?m)^name\s*=\s*["\']openbear["\']\s*$', block):
                        match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', block)
                        own_versions.append(match.group(1) if match else "")
        except Exception as exc:
            raise ReleaseValidationError(f"uv.lock 无效: {exc}") from exc
        if len(own_versions) != 1:
            raise ReleaseValidationError("uv.lock 必须包含唯一 openbear package")
        versions["uv.lock"] = normalize_version(own_versions[0])

    meta_path = root / "release-meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ReleaseValidationError(f"release-meta.json 无效: {exc}") from exc
        if not isinstance(meta, dict) or int(meta.get("schema") or 0) != SCHEMA:
            raise ReleaseValidationError("release-meta.json schema 无效")
        versions["release-meta.json"] = normalize_version(meta.get("version"))

    # build-info 是新发行格式的构建握手；历史 v0.1.x 包没有该文件，仍须可升级。
    build_info_path = root / "web" / "dist" / "build-info.json"
    if build_info_path.is_file():
        try:
            build_info = json.loads(build_info_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ReleaseValidationError(f"web/dist/build-info.json 无效: {exc}") from exc
        if not isinstance(build_info, dict) or int(build_info.get("schema") or 0) != SCHEMA:
            raise ReleaseValidationError("web/dist/build-info.json schema 无效")
        build_id = str(build_info.get("buildId") or "")
        if not re.fullmatch(r"[0-9a-f]{16}", build_id):
            raise ReleaseValidationError("web/dist/build-info.json buildId 无效")
        versions["web/dist/build-info.json"] = normalize_version(build_info.get("version"))
    return versions


def validate_release_tree(root: Path, expected_version: str) -> dict[str, Any]:
    root = Path(root).resolve()
    missing = [rel for rel in REQUIRED_DIRS if not (root / rel).is_dir()]
    missing.extend(rel for rel in REQUIRED_FILES if not (root / rel).is_file())
    if missing:
        raise ReleaseValidationError("发行包不完整，缺少: " + ", ".join(missing))
    expected = normalize_version(expected_version)
    if not expected:
        raise ReleaseValidationError("缺少目标版本，无法校验发行包")
    versions = _read_versions(root)
    mismatched = {name: value for name, value in versions.items() if value != expected}
    if mismatched:
        detail = ", ".join(f"{name}={value or '<empty>'}" for name, value in sorted(mismatched.items()))
        raise ReleaseValidationError(f"发行包版本不一致，期望 {expected}: {detail}")
    frontend = validate_frontend_resources(root / "web" / "dist")
    return {
        "ok": True,
        "root": str(root),
        "version": expected,
        "versions": versions,
        "frontend": frontend,
    }


def _asset_files(dist: Path) -> list[str]:
    assets = dist / "assets"
    if not assets.is_dir():
        return []
    return sorted(path.relative_to(assets).as_posix() for path in assets.rglob("*") if path.is_file())


def _active_assets(dist: Path) -> list[str]:
    marker = dist / ASSET_MARKER
    if marker.is_file():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            active = payload.get("active") if isinstance(payload, dict) else None
            if isinstance(active, list) and all(isinstance(item, str) and item for item in active):
                return sorted(set(active))
        except Exception:
            pass
    return _asset_files(dist)


def retain_previous_assets(incoming_dist: Path, current_dist: Path | None) -> dict[str, Any]:
    """Copy exactly the previous active asset generation into an incoming dist.

    The marker records incoming active files separately from retained files.  On
    the next upgrade only that active list is retained, so assets older than one
    generation are dropped.
    """
    incoming_dist = Path(incoming_dist)
    if not (incoming_dist / "index.html").is_file():
        raise ReleaseValidationError("incoming web/dist 缺少 index.html")
    incoming_assets = incoming_dist / "assets"
    incoming_assets.mkdir(parents=True, exist_ok=True)
    active = _asset_files(incoming_dist)
    retained: list[str] = []
    if current_dist is not None:
        current_dist = Path(current_dist)
        current_assets = current_dist / "assets"
        for rel in _active_assets(current_dist):
            source = (current_assets / rel).resolve()
            try:
                source.relative_to(current_assets.resolve())
            except ValueError:
                continue
            target = incoming_assets / rel
            if target.exists() or not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            retained.append(rel)
    marker = {
        "schema": SCHEMA,
        "active": active,
        "retained": sorted(retained),
    }
    (incoming_dist / ASSET_MARKER).write_text(
        json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"active": len(active), "retained": len(retained), "retainedFiles": sorted(retained)}


def _fetch(
    url: str,
    *,
    accept: str,
    max_bytes: int,
    timeout: float,
    headers: dict[str, str] | None = None,
    allow_truncated: bool = False,
) -> tuple[int, bytes, str]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": accept}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            body = response.read(max_bytes + 1)
            final_url = str(getattr(response, "url", url) or url)
    except Exception as exc:
        raise ReleaseValidationError(f"请求失败 {url}: {type(exc).__name__}: {exc}") from exc
    if status < 200 or status >= 300:
        raise ReleaseValidationError(f"请求失败 {url}: HTTP {status}")
    if len(body) > max_bytes:
        if not allow_truncated:
            raise ReleaseValidationError(f"响应过大 {url}")
        body = body[:max_bytes]
    return status, body, final_url


def probe_deployment(health_url: str, expected_version: str, *, timeout: float = 5.0) -> dict[str, Any]:
    expected = normalize_version(expected_version)
    _, health_raw, _ = _fetch(health_url, accept="application/json", max_bytes=64 * 1024, timeout=timeout)
    try:
        health = json.loads(health_raw.decode("utf-8"))
    except Exception as exc:
        raise ReleaseValidationError(f"/health 非 JSON: {exc}") from exc
    if not isinstance(health, dict) or not health.get("ok"):
        raise ReleaseValidationError("/health 未返回 ok=true")
    actual = normalize_version(health.get("version"))
    if expected and actual != expected:
        raise ReleaseValidationError(f"/health version={actual!r}，期望 {expected!r}")

    split = urllib.parse.urlsplit(health_url)
    if not split.scheme or not split.netloc:
        raise ReleaseValidationError(f"health URL 无效: {health_url}")
    origin = urllib.parse.urlunsplit((split.scheme, split.netloc, "", "", ""))
    login_url = urllib.parse.urljoin(origin + "/", "login")
    # 这是更新器发起的可信本机探测。声明外层 HTTPS，避免启用 HTTPS customUrl
    # 时安全入口把 loopback GET 重定向到公网地址，既不放宽浏览器 Cookie 规则也不误报。
    probe_headers = {"X-Forwarded-Proto": "https"}
    _, html_raw, final_url = _fetch(
        login_url,
        accept="text/html",
        max_bytes=2 * 1024 * 1024,
        timeout=timeout,
        headers=probe_headers,
    )
    final_split = urllib.parse.urlsplit(final_url)
    if (final_split.scheme, final_split.netloc) != (split.scheme, split.netloc):
        raise ReleaseValidationError(f"/login 探测被重定向到非本机地址: {final_url}")
    try:
        html = html_raw.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseValidationError(f"/login 不是 UTF-8 HTML: {exc}") from exc
    references, scripts = index_references(html)
    if not scripts:
        raise ReleaseValidationError("/login HTML 没有 script 入口")
    fetched: list[str] = []
    for reference in references:
        clean = _clean_reference(reference)
        if not clean or clean.startswith(("data:", "#")):
            continue
        target = urllib.parse.urljoin(origin + "/", clean.lstrip("/") if clean.startswith("/") else clean)
        target_split = urllib.parse.urlsplit(target)
        if (target_split.scheme, target_split.netloc) != (split.scheme, split.netloc):
            continue
        _fetch(
            target,
            accept="*/*",
            max_bytes=1024,
            timeout=timeout,
            headers=probe_headers,
            allow_truncated=True,
        )
        fetched.append(clean)
    return {
        "ok": True,
        "version": actual,
        "healthUrl": health_url,
        "loginUrl": final_url,
        "assets": fetched,
    }


def _json_print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate an OpenBear release artifact or deployment")
    sub = parser.add_subparsers(dest="command", required=True)
    tree = sub.add_parser("validate-tree")
    tree.add_argument("--root", required=True)
    tree.add_argument("--version", required=True)
    retain = sub.add_parser("retain-assets")
    retain.add_argument("--incoming", required=True)
    retain.add_argument("--current")
    probe = sub.add_parser("probe-web")
    probe.add_argument("--health-url", required=True)
    probe.add_argument("--version", required=True)
    probe.add_argument("--timeout", type=float, default=5.0)
    backup = sub.add_parser("backup-database")
    backup.add_argument("--root", required=True)
    backup.add_argument("--backup-dir")
    backup.add_argument("--config")
    backup.add_argument("--label", default="upgrade")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-tree":
            result = validate_release_tree(Path(args.root), args.version)
        elif args.command == "retain-assets":
            result = retain_previous_assets(
                Path(args.incoming),
                Path(args.current) if args.current else None,
            )
        elif args.command == "backup-database":
            result = create_sqlite_backup(
                Path(args.root),
                Path(args.backup_dir) if args.backup_dir else None,
                config_path=Path(args.config) if args.config else None,
                label=args.label,
            )
        else:
            result = probe_deployment(args.health_url, args.version, timeout=args.timeout)
    except ReleaseValidationError as exc:
        _json_print({"ok": False, "error": str(exc)})
        return 1
    _json_print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
