#!/usr/bin/env python3
"""Validate OpenBear release trees and deployed Web assets (stdlib only).

This module is shared by the standalone updater, ``install.sh``, tests, and the
release workflow.  It intentionally validates the extracted artifact rather
than trusting source-tree build results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import sqlite3
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
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
PROC_ROOT = Path("/proc")

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
    rf"(?P<quote>['\"])(?P<ref>(?:/assets/|\./|\.\./)[^'\"\\\s]+\.(?:{_RESOURCE_EXTENSIONS})(?:[?#][^'\"\\\s]*)?)(?P=quote)(?!\s*:)",
    re.I,
)
_NEW_URL_RESOURCE_RE = re.compile(
    rf"new\s+URL\(\s*(?P<quote>['\"])(?P<ref>[^'\"\\\s]+\.(?:{_RESOURCE_EXTENSIONS})(?:[?#][^'\"\\\s]*)?)(?P=quote)\s*,\s*import\.meta\.url\s*\)",
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


_PEP440_RE = re.compile(r"(\d+\.\d+\.\d+)(?:(b|rc)([1-9]\d*)|\.dev([1-9]\d*))?")


def uv_lock_version(value: Any) -> str:
    """uv.lock holds uv's PEP 440 spelling (1.2.0b1, 1.2.0rc1, 1.2.0.dev3); the
    other version files hold OpenBear's (1.2.0-beta.1, 1.2.0-rc.1, 1.2.0-dev.3)."""
    text = normalize_version(value)
    match = _PEP440_RE.fullmatch(text)
    if not match:
        return text
    base, label, number, dev = match.groups()
    if dev:
        return f"{base}-dev.{dev}"
    return f"{base}-{'beta' if label == 'b' else 'rc'}.{number}" if label else base


def expand_runtime_path(value: str, root: Path, home: str | None = None) -> Path:
    if home and (value == "~" or value.startswith("~/")):
        value = home + value[1:]
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).absolute()


def deployment_config(root: Path, explicit: str = "", service: str = "openbear.service", *, require_running: bool = False) -> dict[str, str]:
    """Read the service's effective configuration before stopping it; never guess a DB name.

    EnvironmentFiles and specifiers need the live process to disambiguate. A stopped
    ordinary unit can use its merged Environment; unrelated/mismatched deployments
    are rejected before any installation files or service state change.
    """
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service):
        raise ReleaseValidationError("升级服务名无效")
    properties = "MainPID,WorkingDirectory,Environment,EnvironmentFiles,UnsetEnvironment,LoadState"
    try:
        proc = subprocess.run(["systemctl", "show", service, "--property=" + properties],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseValidationError("无法及时读取升级服务配置，未停止服务或切换文件") from exc
    if proc.returncode:
        raise ReleaseValidationError("无法读取升级服务配置；尚未停止服务或切换文件")
    fields = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    pid = int(fields.get("MainPID") or "0")
    if require_running and not pid:
        raise ReleaseValidationError("旧版升级接管需要原服务仍在运行，以确认实际配置路径")
    if pid:
        process = PROC_ROOT / str(pid)
        try:
            before = (process / "stat").read_bytes()
            cwd = (process / "cwd").resolve(strict=True)
            env = dict(item.split(b"=", 1) for item in (process / "environ").read_bytes().split(b"\0") if b"=" in item)
            command = (process / "cmdline").read_bytes().split(b"\0")
            after = (process / "stat").read_bytes()
        except OSError as exc:
            raise ReleaseValidationError("无法读取原服务的运行配置；请在原服务稳定运行时重试") from exc
        # starttime is stable; CPU time in the rest of stat is not.
        if before.rsplit(b")", 1)[1].split()[19] != after.rsplit(b")", 1)[1].split()[19]:
            raise ReleaseValidationError("读取配置时原服务进程已变化，请重试")
        if cwd != root or not any(command[i:i + 2] == [b"-m", b"app.main"] for i in range(len(command) - 1)):
            raise ReleaseValidationError("服务工作目录或启动入口与升级目标不一致，未改变原系统")
        raw = env.get(b"OPENBEAR_CONFIG", b"./openbear.json").decode()
        home = env.get(b"HOME", b"/root").decode()
    else:
        cwd_text = fields.get("WorkingDirectory") or str(root)
        if "%" in cwd_text or Path(cwd_text).resolve() != root:
            raise ReleaseValidationError("停止状态服务的工作目录无法安全确认")
        if fields.get("EnvironmentFiles") or fields.get("UnsetEnvironment"):
            raise ReleaseValidationError("服务含环境文件/取消环境覆盖，请先启动原服务以确认有效配置")
        try:
            env_text = dict(item.split("=", 1) for item in shlex.split(fields.get("Environment", "")) if "=" in item)
        except ValueError as exc:
            raise ReleaseValidationError("无法解析服务环境，未改变原系统") from exc
        raw = env_text.get("OPENBEAR_CONFIG", explicit or str(root / "openbear.json"))
        home = env_text.get("HOME", os.path.expanduser("~"))
        if "%" in raw or "%" in home:
            raise ReleaseValidationError("停止状态服务含环境路径说明符，请先启动原服务")
    if any(c in raw + home for c in "\r\n\0"):
        raise ReleaseValidationError("原服务配置路径含不可安全写入 unit 的控制字符，未改变原系统")
    config = expand_runtime_path(raw, root, home)
    if explicit and expand_runtime_path(explicit, root, home).resolve() != config.resolve():
        raise ReleaseValidationError("指定配置与原服务实际配置不一致，拒绝改变部署意图")
    database = resolve_storage_db_path(root, config, home=home)
    for path in (config.resolve(), database):
        for rel in ("app", "scripts", "prompts", "web/dist", "gateway/bin", "benchmark-package", ".venv"):
            base = root / rel
            if path == base or base in path.parents:
                raise ReleaseValidationError("配置或数据库位于将被替换的程序目录，不能安全升级")
    return {"configPath": str(config), "configHome": home, "databasePath": str(database)}


def render_service_unit(template: str, root: Path, config: str, home: str = "") -> str:
    def quote(value):
        if any(c in value for c in "\r\n\0"):
            raise ReleaseValidationError("服务路径含无效控制字符")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    text = template.replace("__OPENBEAR_DIR__", str(root))
    line = "Environment=" + quote("OPENBEAR_CONFIG=" + config)
    text, count = re.subn(r"(?m)^Environment=OPENBEAR_CONFIG=.*$", lambda _m: line, text)
    if not count:
        text = text.replace("[Service]\n", "[Service]\n" + line + "\n", 1)
    if home:
        text = text.replace("[Service]\n", "[Service]\nEnvironment=" + quote("HOME=" + home) + "\n", 1)
    return text


# Exact standalone script shipped with 0.16.0. Do not intercept arbitrary Python
# callers, classify/validate CLIs, or newer updaters which speak this protocol.
LEGACY_0160_SHA256 = "4491aeafaa4cec0af6ac094acc67c69f8fe18f34935148ab8c59d8df97e9c945"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _handoff_0160(frame, staging: Path, version: str) -> None:
    if frame.f_code.co_name != "_download_and_verify" or frame.f_globals.get("UPDATER_PROTOCOL", 0) >= 2:
        return
    worker = frame.f_locals.get("self")
    source = Path(frame.f_code.co_filename)
    if not worker or getattr(worker, "from_version", "") != "0.16.0" or _file_digest(source) != LEGACY_0160_SHA256:
        return
    try:
        if worker.stopped or worker.did_swap:
            raise ReleaseValidationError("旧版更新器已进入切换阶段，拒绝重复接管")
        if len(sys.argv) != 4 or sys.argv[1:3] != ["apply", "--request"]:
            raise ReleaseValidationError("旧版升级请求入口无法核实")
        request_path = Path(sys.argv[3]).resolve()
        request_bytes = request_path.read_bytes()
        request = json.loads(request_bytes)
        if request != worker.request:
            raise ReleaseValidationError("旧版升级请求已变化")
        # The Web launcher uses data/update-request.json; the supported standalone
        # CLI (also fixed acceptance B) may use an external private request file.
        # Both are bound byte-for-byte, not identified by their filename.
        staging = staging.resolve()
        if Path(__file__).resolve() != staging / "scripts/release_validation.py" or staging != Path(frame.f_locals["staging"]).resolve():
            raise ReleaseValidationError("旧版升级候选目录不一致")
        archive = Path(frame.f_locals["zip_path"]).resolve()
        digest = frame.f_locals.get("expected", "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or _file_digest(archive) != digest:
            raise ReleaseValidationError("旧版升级候选摘要不一致")
        # Recursion is a plain validation call, not an updater frame. No handoff
        # happens until the complete candidate (including resources) validates.
        validate_release_tree(staging, version)
        config = deployment_config(worker.install_root, service=worker.service_name, require_running=True)
        if Path(config["databasePath"]).parent != worker.data_dir:
            raise ReleaseValidationError("原服务数据库目录与升级请求不一致")
        python = worker.install_root / ".venv/bin/python"
        check = subprocess.run([str(python), "-c", "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"],
                               capture_output=True, timeout=15)
        if check.returncode:
            raise ReleaseValidationError("已安装运行环境不满足 Python 3.11，未停止原服务")
        base = worker.data_dir / "updater"
        # Kept outside staging: the new updater clears staging before rechecking
        # and extracting this exact ZIP. exec replaces, rather than forks, apply.
        bridge = Path(tempfile.mkdtemp(prefix="compat-0160-", dir=base))
        for name in ("updater.py", "release_validation.py"):
            shutil.copy2(staging / "scripts" / name, bridge / name)
        shutil.copy2(archive, bridge / "package.zip")
        manifest = {"schema": 1, "requestPath": str(request_path), "requestSha256": hashlib.sha256(request_bytes).hexdigest(),
                    "request": request, "zipSha256": digest, "config": config}
        manifest_path = bridge / "handoff.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
        os.execv(str(python), [str(python), str(bridge / "updater.py"), "apply", "--request", str(request_path), "--handoff", str(manifest_path)])
        raise ReleaseValidationError("升级接管未替换旧进程")
    except Exception as exc:
        # The old write_result itself breaks on Python 3.8. Exit past its
        # Exception handlers, with a normal failure record and the service intact.
        message = "0.16.0 升级兼容接管失败，原服务和文件未切换: " + str(exc)
        worker.log(message)
        payload = {"schema": 1, "status": "failed", "fromVersion": worker.from_version, "toVersion": worker.to_version,
                   "finishedAt": int(time.time()), "message": message, "acked": False, "requiresRestart": True}
        temp = worker.result_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(worker.result_path)
        worker.write_state("done", lastResultStatus="failed")
        raise SystemExit(1) from None


def resolve_storage_db_path(install_root: Path, config_path: Path | None = None, *, home: str | None = None) -> Path:
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
    database = expand_runtime_path(raw_path, root, home)
    if not database.is_absolute():
        database = root / database
    return database.resolve()


def create_sqlite_backup(
    install_root: Path,
    backup_dir: Path | None = None,
    *,
    config_path: Path | None = None,
    home: str | None = None,
    expected_database: str | None = None,
    label: str = "upgrade",
) -> dict[str, Any]:
    """Create a private SQLite online backup that includes committed WAL contents.

    The caller must stop the old service before calling this function.  The SQLite
    backup API is still used instead of copying the main file, so a remaining WAL
    is handled consistently.  A missing database is a clean no-op.
    """
    root = Path(install_root).resolve()
    database = resolve_storage_db_path(root, config_path, home=home)
    if expected_database and database != Path(expected_database):
        raise ReleaseValidationError("停服前后数据库配置已变化，未切换文件")
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
        self.manifests: list[str] = []
        self.apple_icons: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): value for key, value in attrs}
        if tag.lower() == "script" and values.get("src"):
            ref = str(values["src"])
            self.references.append(ref)
            self.scripts.append(ref)
        elif tag.lower() == "link" and values.get("href"):
            ref = str(values["href"])
            self.references.append(ref)
            relations = str(values.get("rel") or "").lower().split()
            if "manifest" in relations:
                self.manifests.append(ref)
            if "apple-touch-icon" in relations:
                self.apple_icons.append(ref)


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
    if suffix in {".js", ".mjs"}:
        for match in _NEW_URL_RESOURCE_RE.finditer(text):
            yield match.group("ref")


def _validate_pwa_png(path: Path, size: int) -> None:
    try:
        data = path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("not PNG")
        offset, pixels, header, ended = 8, bytearray(), None, False
        while offset < len(data):
            length = struct.unpack_from(">I", data, offset)[0]
            kind = data[offset + 4:offset + 8]
            body = data[offset + 8:offset + 8 + length]
            crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
            if zlib.crc32(kind + body) != crc:
                raise ValueError("PNG CRC mismatch")
            if offset == 8 and kind != b"IHDR":
                raise ValueError("missing IHDR")
            if kind == b"IHDR":
                if header is not None:
                    raise ValueError("duplicate IHDR")
                header = struct.unpack(">IIBBBBB", body)
            elif kind == b"IDAT":
                pixels.extend(body)
            elif kind == b"IEND":
                ended = length == 0 and offset + 12 == len(data)
                break
            offset += length + 12
        if not ended or header is None or header[:2] != (size, size) or header[2] != 8 or header[3] not in (2, 6) or header[4:] != (0, 0, 0):
            raise ValueError("invalid PNG dimensions/format")
        stride = 1 + size * (3 if header[3] == 2 else 4)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(pixels, stride * size + 1)
        if len(raw) != stride * size or not decoder.eof or decoder.unused_data or any(raw[row * stride] > 4 for row in range(size)):
            raise ValueError("invalid PNG pixel data")
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        raise ReleaseValidationError(f"PWA 图标无效 {path}: {exc}") from exc


def _validate_pwa_resources(dist: Path, html: str) -> set[str]:
    parser = _IndexParser()
    parser.feed(html)
    manifest_path = dist / "manifest.webmanifest"
    # Historical update baselines have neither file nor link: preserve their
    # existing validation. Once either advertises PWA, require the entire set.
    if not parser.manifests and not manifest_path.exists():
        return set()

    def local(ref: Any, source: Path) -> Path:
        if not isinstance(ref, str) or not ref or "\\" in ref:
            raise ReleaseValidationError("PWA 资源地址无效")
        try:
            parts = urllib.parse.urlsplit(ref)
        except ValueError as exc:
            raise ReleaseValidationError(f"PWA 资源地址无效: {ref}") from exc
        if parts.scheme or parts.netloc or parts.query or parts.fragment:
            raise ReleaseValidationError(f"PWA 资源必须是无查询参数的相对同源地址: {ref}")
        candidate = _reference_path(dist, source, ref)
        if candidate is None or not candidate.is_file():
            raise ReleaseValidationError(f"PWA 资源缺失: {ref}")
        return candidate

    if len(parser.manifests) != 1 or local(parser.manifests[0], dist / "index.html") != dist.resolve() / "manifest.webmanifest":
        raise ReleaseValidationError("PWA 必须链接唯一的 manifest.webmanifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReleaseValidationError(f"PWA manifest 无效: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("display") != "standalone" or not all(isinstance(manifest.get(key), str) and manifest[key].strip() for key in ("name", "short_name")):
        raise ReleaseValidationError("PWA manifest 名称或显示模式无效")
    for key in ("id", "start_url", "scope"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value or "\\" in value:
            raise ReleaseValidationError(f"PWA manifest {key} 无效")
        try:
            parts = urllib.parse.urlsplit(value)
        except ValueError as exc:
            raise ReleaseValidationError(f"PWA manifest {key} 无效") from exc
        if parts.scheme or parts.netloc or parts.query or parts.fragment or urllib.parse.urljoin("https://openbear.invalid/manifest.webmanifest", value) != "https://openbear.invalid/":
            raise ReleaseValidationError(f"PWA manifest {key} 必须是同源根地址且不绑定会话")
    icons = manifest.get("icons")
    if not isinstance(icons, list):
        raise ReleaseValidationError("PWA manifest 缺少 icons")
    checked = {"manifest.webmanifest"}
    sizes_found: set[int] = set()
    for icon in icons:
        if not isinstance(icon, dict) or icon.get("type") != "image/png" or icon.get("purpose", "any") != "any":
            raise ReleaseValidationError("PWA manifest 图标类型无效")
        path = local(icon.get("src"), manifest_path)
        declared_size = icon.get("sizes")
        size = {"192x192": 192, "512x512": 512}.get(declared_size) if isinstance(declared_size, str) else None
        if size is None or path != dist.resolve() / f"icons/openbear-{size}.png":
            raise ReleaseValidationError("PWA manifest 图标必须使用已开放的 192/512 路径")
        _validate_pwa_png(path, size)
        sizes_found.add(size)
        checked.add(path.relative_to(dist.resolve()).as_posix())
    if sizes_found != {192, 512}:
        raise ReleaseValidationError("PWA manifest 缺少 192/512 图标")
    apple = dist / "icons/apple-touch-icon.png"
    if len(parser.apple_icons) != 1 or local(parser.apple_icons[0], dist / "index.html") != dist.resolve() / "icons/apple-touch-icon.png":
        raise ReleaseValidationError("PWA 缺少 apple-touch-icon 链接")
    _validate_pwa_png(apple, 180)
    checked.add("icons/apple-touch-icon.png")
    return checked


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
        elif candidate.suffix.lower() == ".wasm":
            try:
                head = candidate.open("rb").read(8)
            except OSError as exc:
                raise ReleaseValidationError(f"无法读取前端资源 {candidate}: {exc}") from exc
            if head != b"\x00asm\x01\x00\x00\x00":
                raise ReleaseValidationError(f"前端 Wasm 资源无效: {rel}")

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
    checked.update(_validate_pwa_resources(dist, html))
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
        versions["uv.lock"] = uv_lock_version(own_versions[0])

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


def validate_prompt_resources(root: Path, version: str) -> None:
    # Resource format introduced in the 0.16.1 family, including its dev/beta/rc
    # builds. The previous release must still validate during rollback.
    core = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-|$)", normalize_version(version))
    if not core or tuple(map(int, core.groups())) < (0, 16, 1):
        return
    required = ("guides/cron.md", "guides/webhook.md", "collab-member.md", "personas/manifest.json")
    prompts = root / "prompts"
    for name in required:
        path = prompts / name
        if not path.is_file() or not path.stat().st_size:
            raise ReleaseValidationError("发行包缺少提示资源: prompts/" + name)
    try:
        manifest = json.loads((prompts / "personas/manifest.json").read_text(encoding="utf-8"))
        entries = manifest["personas"]
        if manifest.get("schema") != 1 or not isinstance(entries, list):
            raise ValueError("schema/personas")
        seen = set()
        for item in entries:
            key, digest = item["id"], item["sha256"]
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key) or key in seen or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("id/sha256")
            seen.add(key)
            if _file_digest(prompts / "personas" / (key + ".tpl")) != digest:
                raise ValueError("资源摘要不符: " + key)
        if not {"little-code", "ctf-sandbox", "da-fei-yu", "gemini-argon-4"} <= seen:
            raise ValueError("缺少内置人设条目")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseValidationError("发行包人设资源无效: " + str(exc)) from exc


def validate_release_tree(root: Path, expected_version: str) -> dict[str, Any]:
    # The one legacy caller which cannot pass a new protocol is recognized by
    # its exact source fingerprint and live apply frame, never just argv/env.
    _handoff_0160(sys._getframe(1), Path(root), expected_version)
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
    validate_prompt_resources(root, expected)
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
    backup.add_argument("--home")
    backup.add_argument("--expected-database")
    backup.add_argument("--label", default="upgrade")
    config = sub.add_parser("resolve-config")
    config.add_argument("--root", required=True)
    config.add_argument("--config", default="")
    config.add_argument("--service", default="openbear.service")
    unit = sub.add_parser("render-unit")
    unit.add_argument("--root", required=True)
    unit.add_argument("--template", required=True)
    unit.add_argument("--config", required=True)
    unit.add_argument("--home", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "render-unit":
            print(render_service_unit(Path(args.template).read_text(encoding="utf-8"), Path(args.root), args.config, args.home), end="")
            return 0
        if args.command == "resolve-config":
            result = deployment_config(Path(args.root), args.config, args.service)
        elif args.command == "validate-tree":
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
                home=args.home,
                expected_database=args.expected_database,
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
