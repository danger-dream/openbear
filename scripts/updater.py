#!/usr/bin/env python3
"""OpenBear 独立更新器（仅标准库）。

必须先复制到 data/updater/ 再执行，避免换文件时踩空自己。

用法：
  python scripts/updater.py classify --current DIR --incoming DIR [--write-meta PATH]
  python scripts/updater.py classify-names --files PATH [--write-meta PATH]
  python scripts/updater.py apply --request PATH
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

SCHEMA = 1
BACKUP_KEEP = 3
HEALTH_WAIT_S = 90
HEALTH_INTERVAL_S = 1.0
USER_AGENT = "OpenBear-Updater/1.0"

REPLACE_DIRS = ("app", "web/dist", "prompts", "scripts")
REPLACE_FILES = (
    "pyproject.toml",
    "uv.lock",
    "openbear.service",
    "openbear.json.example",
    "README.md",
    "README",
    "release-meta.json",
)
PROTECTED_PREFIXES = (
    "data/",
    "workspace/",
    "skills/",
    "mcp-servers/",
    ".venv/",
    ".git/",
)
PROTECTED_NAMES = {
    "openbear.json",
    "data",
    "workspace",
    "skills",
    "mcp-servers",
    ".venv",
    ".git",
}


# ---------------------------------------------------------------------------
# semver
# ---------------------------------------------------------------------------


def _strip_v(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("v") or text.startswith("V"):
        return text[1:]
    return text


def parse_semver(value: str) -> tuple[int, int, int, str]:
    """Parse ``1.2.3`` / ``v1.2.3-rc.1`` into (major, minor, patch, prerelease)."""
    text = _strip_v(value)
    if not text:
        raise ValueError("empty version")
    main, sep, pre = text.partition("-")
    parts = main.split(".")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        raise ValueError(f"invalid semver: {value}")
    return int(parts[0]), int(parts[1]), int(parts[2]), pre if sep else ""


def cmp_semver(left: str, right: str) -> int:
    """Compare two semver strings. Pre-release is lower than the same numeric triple."""
    a = parse_semver(left)
    b = parse_semver(right)
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    if a[3] == b[3]:
        return 0
    if not a[3]:
        return 1
    if not b[3]:
        return -1
    return -1 if a[3] < b[3] else 1


def is_newer(latest: str, current: str) -> bool:
    try:
        return cmp_semver(latest, current) > 0
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def normalize_rel(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


VERSION_METADATA_FILES = {"app/__init__.py", "pyproject.toml", "web/package.json"}
# uv.lock 里虚拟项目自身的 version 会随发版被 uv sync 改写，不等于依赖变化。
VERSION_PREVIEW_REFRESH_FILES = VERSION_METADATA_FILES | {"uv.lock"}
_INIT_VERSION_RE = re.compile(r'^__version__\s*=\s*["\'][^"\']+["\']', re.M)
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*["\'][^"\']+["\']', re.M)
_UV_PROJECT_VERSION_RE = re.compile(r'(name = "openbear"\nversion = ")[^"]+(")')


def _blank_version_text(rel: str, text: str) -> str:
    if rel == "app/__init__.py":
        return _INIT_VERSION_RE.sub('__version__ = ""', text)
    if rel == "pyproject.toml":
        return _PYPROJECT_VERSION_RE.sub('version = ""', text)
    if rel == "uv.lock":
        return _UV_PROJECT_VERSION_RE.sub(r"\1\2", text)
    if rel == "web/package.json":
        try:
            data = json.loads(text)
        except Exception:
            return text
        if isinstance(data, dict):
            data["version"] = ""
            return json.dumps(data, sort_keys=True, ensure_ascii=False)
        return text
    return text


def is_version_metadata_only(rel: str, current: Path, incoming: Path) -> bool:
    rel = normalize_rel(rel)
    if rel not in VERSION_PREVIEW_REFRESH_FILES:
        return False
    if not current.is_file() or not incoming.is_file():
        return False
    try:
        old = current.read_text(encoding="utf-8")
        new = incoming.read_text(encoding="utf-8")
    except OSError:
        return False
    return _blank_version_text(rel, old) == _blank_version_text(rel, new)


def classify_change(rel: str, current: Path | None = None, incoming: Path | None = None) -> str:
    kind = classify_path(rel)
    if (
        kind == "restart"
        and current is not None
        and incoming is not None
        and is_version_metadata_only(rel, current, incoming)
    ):
        return "refresh"
    return kind


def classify_path(rel: str) -> str:
    """Classify one relative path: restart / refresh / noop / ignore."""
    rel = normalize_rel(rel)
    if not rel or rel.endswith("/"):
        return "ignore"
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return "ignore"
    name = parts[-1]
    if name in {".DS_Store", "Thumbs.db"} or name.endswith(".pyc"):
        return "ignore"
    if "__pycache__" in parts or name == "__pycache__":
        return "ignore"
    if parts[0] in PROTECTED_NAMES or rel in PROTECTED_NAMES:
        return "ignore"
    if any(rel.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        return "ignore"
    if parts[0] == "app":
        return "restart"
    if rel in {"pyproject.toml", "uv.lock", "openbear.service"}:
        return "restart"
    if parts[0] == "web":
        return "refresh"
    if parts[0] in {"prompts", "tests", "scripts"} or rel.startswith(".github/"):
        return "noop"
    if rel in {
        "README.md",
        "README",
        "LICENSE",
        "openbear.json.example",
        "release-meta.json",
        ".gitignore",
        ".gitattributes",
    }:
        return "noop"
    if name.endswith((".md", ".example", ".txt")):
        return "noop"
    return "restart"


def classify_names(paths: list[str]) -> dict[str, Any]:
    changed = [normalize_rel(p) for p in paths if classify_path(p) != "ignore"]
    classes: set[str] = set()
    for path in changed:
        kind = classify_path(path)
        # 预告：只动版本号 / lock 里的项目版本时按刷新。现场 classify_trees 会再按内容确认。
        if kind == "restart" and path in VERSION_PREVIEW_REFRESH_FILES:
            kind = "refresh"
        classes.add(kind)
    if "restart" in classes:
        requires_restart = True
        effect = "restart"
    elif "refresh" in classes:
        requires_restart = False
        effect = "refresh"
    elif changed:
        requires_restart = False
        effect = "noop"
    else:
        requires_restart = False
        effect = "none"
    return {
        "requiresRestart": requires_restart,
        "effect": effect,
        "changed": changed,
        "classes": sorted(classes),
    }


def _iter_files(root: Path) -> list[str]:
    out: list[str] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".git", ".venv"}]
        base = Path(dirpath)
        for name in filenames:
            rel = normalize_rel(str((base / name).relative_to(root)))
            if classify_path(rel) == "ignore":
                continue
            out.append(rel)
    return sorted(set(out))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def classify_trees(current_dir: Path, incoming_dir: Path) -> dict[str, Any]:
    current_dir = Path(current_dir)
    incoming_dir = Path(incoming_dir)
    try:
        current_files = set(_iter_files(current_dir))
        incoming_files = set(_iter_files(incoming_dir))
        tracked = set()
        for rel in incoming_files | current_files:
            kind = classify_path(rel)
            if kind == "ignore":
                continue
            # Only compare paths the zip is allowed to own.
            if not _managed_rel(rel) and rel not in incoming_files:
                continue
            tracked.add(rel)
        changed: list[str] = []
        version_only: list[str] = []
        classes: set[str] = set()
        for rel in sorted(tracked):
            src = incoming_dir / rel
            dst = current_dir / rel
            if src.is_file() and dst.is_file():
                if sha256_file(src) != sha256_file(dst):
                    changed.append(rel)
                    classes.add(classify_change(rel, dst, src))
                    if is_version_metadata_only(rel, dst, src):
                        version_only.append(rel)
            elif src.is_file() != dst.is_file():
                changed.append(rel)
                classes.add(classify_change(rel, dst if dst.exists() else None, src if src.exists() else None))
        if "restart" in classes:
            requires_restart, effect = True, "restart"
        elif "refresh" in classes:
            requires_restart, effect = False, "refresh"
        elif changed:
            requires_restart, effect = False, "noop"
        else:
            requires_restart, effect = False, "none"
        return {
            "requiresRestart": requires_restart,
            "effect": effect,
            "changed": changed,
            "versionOnly": version_only,
            "classes": sorted(classes),
        }
    except Exception as exc:
        return {
            "requiresRestart": True,
            "effect": "restart",
            "changed": [],
            "classes": ["restart"],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _managed_rel(rel: str) -> bool:
    rel = normalize_rel(rel)
    if rel in REPLACE_FILES:
        return True
    return any(rel == prefix or rel.startswith(prefix + "/") for prefix in REPLACE_DIRS)


def write_release_meta(
    path: Path,
    *,
    version: str,
    compared_with: str,
    classification: dict[str, Any],
) -> None:
    payload = {
        "schema": SCHEMA,
        "version": _strip_v(version),
        "requiresRestart": bool(classification.get("requiresRestart")),
        "effect": str(classification.get("effect") or ""),
        "comparedWith": _strip_v(compared_with),
        "changedCount": len(classification.get("changed") or []),
    }
    if classification.get("error"):
        payload["error"] = classification["error"]
    atomic_write_json(path, payload)


# ---------------------------------------------------------------------------
# files / state
# ---------------------------------------------------------------------------


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def unwrap_staging(staging: Path) -> Path:
    if (staging / "app").is_dir() or (staging / "web").is_dir() or (staging / "pyproject.toml").is_file():
        return staging
    kids = [p for p in staging.iterdir() if p.name not in {"__MACOSX", ".DS_Store"}]
    if len(kids) == 1 and kids[0].is_dir():
        return kids[0]
    return staging


def safe_extract_zip(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ValueError(f"unsafe zip path: {info.filename}")
        zf.extractall(dest)


def with_tool_path(env: dict[str, str] | None = None) -> dict[str, str]:
    """systemd-run 默认 PATH 看不到 ~/.local/bin，补上 uv 常见位置。"""
    out = dict(os.environ if env is None else env)
    home = Path(out.get("HOME") or Path.home() or "/root")
    out.setdefault("HOME", str(home))
    extras = [str(home / ".local" / "bin"), "/usr/local/bin"]
    path = out.get("PATH") or ""
    parts = [p for p in path.split(":") if p]
    prepend = [extra for extra in extras if extra and extra not in parts]
    out["PATH"] = ":".join(prepend + parts)
    return out


def resolve_uv(env: dict[str, str] | None = None) -> str:
    env = with_tool_path(env)
    found = shutil.which("uv", path=env.get("PATH"))
    if found:
        return found
    for extra in (env.get("PATH") or "").split(":"):
        candidate = Path(extra) / "uv"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def parse_sha256sums(text: str, filename: str) -> str:
    want = Path(filename).name
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "  " in line:
            digest, name = line.split("  ", 1)
        elif " *" in line:
            digest, name = line.split(" *", 1)
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            digest, name = parts[0], parts[-1]
        if Path(name.strip().lstrip("*")).name == want:
            return digest.strip().lower()
    raise ValueError(f"SHA256SUMS 中没有 {want}")


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


class UpdateError(RuntimeError):
    def __init__(self, message: str, *, status: str = "failed") -> None:
        super().__init__(message)
        self.status = status


class Updater:
    def __init__(self, request: dict[str, Any]) -> None:
        self.request = request
        self.install_root = Path(request["installRoot"]).resolve()
        self.data_dir = Path(request.get("dataDir") or (self.install_root / "data")).resolve()
        self.service_name = str(request.get("serviceName") or "openbear.service")
        self.from_version = _strip_v(request.get("fromVersion") or "")
        self.to_version = _strip_v(request.get("toVersion") or "")
        self.health_url = str(request.get("healthUrl") or "http://127.0.0.1:18961/health")
        self.allow_dirty = bool(request.get("allowDirty"))
        self.log_path = self.data_dir / "backups" / f"update-{self.to_version or 'unknown'}-{int(time.time())}.log"
        self.state_path = self.data_dir / "update-state.json"
        self.result_path = self.data_dir / "update-result.json"
        self.stopped = False
        self.backup_path: Path | None = None
        self.did_swap = False
        self.classification: dict[str, Any] = {}

    def log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print(line, flush=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def write_state(self, phase: str, **extra: Any) -> None:
        current: dict[str, Any] = {}
        if self.state_path.is_file():
            try:
                current = read_json(self.state_path)
            except Exception:
                current = {}
        current.update({
            "schema": SCHEMA,
            "phase": phase,
            "fromVersion": self.from_version,
            "toVersion": self.to_version,
            "updatedAt": int(time.time()),
        })
        current.update(extra)
        atomic_write_json(self.state_path, current)

    def write_result(self, status: str, message: str, **extra: Any) -> None:
        payload = {
            "schema": SCHEMA,
            "status": status,
            "fromVersion": self.from_version,
            "toVersion": self.to_version,
            "requiresRestart": bool((self.classification or {}).get("requiresRestart")),
            "effect": str((self.classification or {}).get("effect") or ""),
            "finishedAt": int(time.time()),
            "message": message,
            "logPath": str(self.log_path.relative_to(self.install_root)) if self.log_path.is_relative_to(self.install_root) else str(self.log_path),
            "acked": False,
        }
        payload.update(extra)
        atomic_write_json(self.result_path, payload)
        self.write_state("done", lastResultStatus=status)

    def run(self) -> int:
        try:
            self._run()
            return 0
        except UpdateError as exc:
            self.log(f"失败: {exc}")
            try:
                if self.did_swap:
                    self._rollback(str(exc))
                else:
                    self.write_result(exc.status, str(exc))
            except Exception as rollback_exc:
                self.log(f"回滚失败: {rollback_exc}")
                self.write_result("failed", f"{exc}；回滚失败: {rollback_exc}")
            return 1
        except Exception as exc:
            self.log(f"未处理异常: {type(exc).__name__}: {exc}")
            try:
                if self.did_swap:
                    self._rollback(f"{type(exc).__name__}: {exc}")
                else:
                    self.write_result("failed", f"{type(exc).__name__}: {exc}")
            except Exception as rollback_exc:
                self.write_result("failed", f"{type(exc).__name__}: {exc}；回滚失败: {rollback_exc}")
            return 1

    def _run(self) -> None:
        self.log(f"开始更新 {self.from_version or '?'} -> {self.to_version or '?'}")
        self.write_state("checking")
        if not self.allow_dirty and _git_dirty(self.install_root):
            raise UpdateError("安装目录有未提交改动，已拒绝覆盖。如需继续请确认覆盖工作区。")
        staging = self._download_and_verify()
        self.classification = classify_trees(self.install_root, staging)
        changed_names = list(self.classification.get("changed") or [])
        self.log(
            "分类: effect={effect} restart={restart} changed={n} files={files}".format(
                effect=self.classification.get("effect"),
                restart=self.classification.get("requiresRestart"),
                n=len(changed_names),
                files=",".join(changed_names[:20]),
            )
        )
        if self.classification.get("error"):
            self.log(f"分类出错，按重启处理: {self.classification['error']}")
        requires_restart = bool(self.classification.get("requiresRestart"))
        self.write_state("applying", requiresRestart=requires_restart, effect=self.classification.get("effect"))
        self.backup_path = self._backup()
        if requires_restart:
            self._stop_service()
        try:
            self._swap(staging)
            self.did_swap = True
            self._sync_deps()
            if requires_restart:
                self._install_unit()
                self.write_state("restarting")
                self._start_service()
            self._wait_health()
        except Exception:
            raise
        message = (
            f"已更新到 v{self.to_version}，请刷新页面"
            if not requires_restart
            else f"已更新到 v{self.to_version} 并完成健康检查"
        )
        self.write_result("success", message)
        self.log(message)

    def _download_and_verify(self) -> Path:
        self.write_state("downloading")
        work = self.data_dir / "updater" / "staging"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        zip_url = str(self.request.get("zipUrl") or "")
        zip_name = f"openbear-{self.to_version}.zip"
        zip_path = work / zip_name
        if zip_url:
            self.log(f"下载 {zip_url}")
            _download(zip_url, zip_path)
        else:
            local = Path(str(self.request.get("zipPath") or ""))
            if not local.is_file():
                raise UpdateError("更新请求缺少 zipUrl / zipPath")
            shutil.copy2(local, zip_path)
        expected = str(self.request.get("sha256") or "").strip().lower()
        sums_url = str(self.request.get("sha256Url") or "")
        if not expected and sums_url:
            sums_path = work / "SHA256SUMS"
            _download(sums_url, sums_path)
            expected = parse_sha256sums(sums_path.read_text(encoding="utf-8"), zip_name)
        actual = sha256_file(zip_path)
        if expected and actual != expected:
            raise UpdateError(f"SHA256 不匹配：期望 {expected}，实际 {actual}")
        if not expected:
            self.log("警告: 没有提供 SHA256，仅记录实际值 " + actual)
        extract_to = work / "extract"
        safe_extract_zip(zip_path, extract_to)
        staging = unwrap_staging(extract_to)
        if not (staging / "app").is_dir() and not (staging / "web" / "dist").is_dir():
            raise UpdateError("发行包结构无效：缺少 app/ 或 web/dist/")
        return staging

    def _backup(self) -> Path:
        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = backup_dir / f"code-{self.from_version or 'unknown'}-{stamp}.tgz"
        self.log(f"备份代码到 {path}")
        with tarfile.open(path, "w:gz") as tar:
            for rel in (*REPLACE_DIRS, *REPLACE_FILES):
                src = self.install_root / rel
                if src.exists():
                    tar.add(src, arcname=rel)
        existing = sorted(backup_dir.glob("code-*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in existing[BACKUP_KEEP:]:
            stale.unlink(missing_ok=True)
        return path

    def _swap(self, staging: Path) -> None:
        self.log("替换代码文件")
        for rel in REPLACE_DIRS:
            src = staging / rel
            dest = self.install_root / rel
            if src.is_dir():
                _replace_dir(src, dest)
            elif dest.exists() and rel in {"app", "web/dist"}:
                # Incoming package omitted a managed tree; keep current.
                self.log(f"发行包没有 {rel}，保留现有目录")
        for rel in REPLACE_FILES:
            src = staging / rel
            dest = self.install_root / rel
            if src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

    def _sync_deps(self) -> None:
        changed = set(self.classification.get("changed") or [])
        version_only = set(self.classification.get("versionOnly") or [])
        material = changed - version_only
        has_lock = (self.install_root / "uv.lock").is_file()
        # lock 没变、或只改了项目自身版本号，不必重装。
        if has_lock and "uv.lock" not in material:
            return
        if not has_lock and "pyproject.toml" not in material:
            return
        self.log("同步 Python 依赖")
        env = with_tool_path()
        uv = resolve_uv(env)
        if uv and has_lock:
            cmd = [uv, "sync", "--frozen", "--directory", str(self.install_root)]
        elif uv:
            cmd = [uv, "sync", "--directory", str(self.install_root)]
        else:
            python = self.install_root / ".venv" / "bin" / "python"
            if not python.is_file():
                raise UpdateError("找不到 uv，也没有 .venv/bin/python，无法同步依赖")
            cmd = [str(python), "-m", "pip", "install", "-e", str(self.install_root)]
        proc = subprocess.run(cmd, cwd=self.install_root, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or ""))[-2000:]
            raise UpdateError(f"依赖同步失败 rc={proc.returncode}: {tail}")

    def _install_unit(self) -> None:
        src = self.install_root / "openbear.service"
        if not src.is_file():
            return
        dest = Path("/etc/systemd/system") / self.service_name
        if not dest.parent.is_dir():
            self.log("没有 /etc/systemd/system，跳过 unit 安装")
            return
        text = src.read_text(encoding="utf-8").replace("__OPENBEAR_DIR__", str(self.install_root))
        dest.write_text(text, encoding="utf-8")
        self._systemctl("daemon-reload")

    def _stop_service(self) -> None:
        self.log(f"停止 {self.service_name}")
        self._systemctl("stop", self.service_name, check=False)
        self.stopped = True

    def _start_service(self) -> None:
        self.log(f"启动 {self.service_name}")
        self._systemctl("start", self.service_name)
        self.stopped = False

    def _wait_health(self) -> None:
        self.write_state("verifying")
        deadline = time.time() + HEALTH_WAIT_S
        last = ""
        while time.time() < deadline:
            ok, last = _probe_health(self.health_url, self.to_version)
            if ok:
                self.log(f"健康检查通过: {last}")
                return
            time.sleep(HEALTH_INTERVAL_S)
        raise UpdateError(
            f"新版本 /health 在 {HEALTH_WAIT_S}s 内未返回 ok=true 且 version={self.to_version}（最后: {last}）"
        )

    def _rollback(self, reason: str) -> None:
        self.log(f"开始回滚: {reason}")
        self.write_state("rolling_back")
        # 新版本可能已经 start 成功但 health 失败；此时 stopped=False，
        # 只 start 不会换进程。回滚前必须先停掉正在跑的新进程。
        self._systemctl("stop", self.service_name, check=False)
        self.stopped = True
        if self.backup_path and self.backup_path.is_file():
            with tarfile.open(self.backup_path, "r:gz") as tar:
                try:
                    tar.extractall(self.install_root, filter="data")
                except TypeError:
                    tar.extractall(self.install_root)
        else:
            raise UpdateError(f"回滚失败：没有备份。原始错误: {reason}")
        try:
            self._sync_deps()
        except Exception as exc:
            self.log(f"回滚后依赖同步失败: {exc}")
        try:
            self._install_unit()
            self._start_service()
            ok, last = False, ""
            deadline = time.time() + HEALTH_WAIT_S
            while time.time() < deadline:
                ok, last = _probe_health(self.health_url, self.from_version or None)
                if ok:
                    break
                time.sleep(HEALTH_INTERVAL_S)
            if not ok:
                self.write_result("failed", f"{reason}；回滚后健康检查仍失败: {last}")
                return
        except Exception as exc:
            self.write_result("failed", f"{reason}；回滚启动失败: {exc}")
            return
        self.write_result("rolled_back", f"更新失败已回滚到 v{self.from_version or '?'}。原因: {reason}")
        self.log("回滚完成")

    def _systemctl(self, *args: str, check: bool = True) -> None:
        cmd = ["systemctl", *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if check and proc.returncode != 0:
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or ""))[-800:]
            raise UpdateError(f"systemctl {' '.join(args)} 失败 rc={proc.returncode}: {tail}")
        if proc.returncode != 0:
            self.log(f"systemctl {' '.join(args)} rc={proc.returncode}")


def _replace_dir(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".openbear-new")
    old = dest.with_name(dest.name + ".openbear-old")
    if tmp.exists():
        shutil.rmtree(tmp)
    if old.exists():
        shutil.rmtree(old)
    shutil.copytree(src, tmp)
    if dest.exists():
        dest.replace(old)
    tmp.replace(dest)
    if old.exists():
        shutil.rmtree(old)


def _git_dirty(root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    proc = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as handle:
            shutil.copyfileobj(resp, handle)
    except urllib.error.URLError as exc:
        raise UpdateError(f"下载失败 {url}: {exc}") from exc


def _probe_health(url: str, expected_version: str | None) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = getattr(resp, "status", 200)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    try:
        data = json.loads(raw)
    except Exception:
        return False, f"http {status} 非 JSON"
    if not isinstance(data, dict) or not data.get("ok"):
        return False, raw[:200]
    version = str(data.get("version") or "")
    if expected_version and _strip_v(version) != _strip_v(expected_version):
        return False, f"ok 但 version={version!r} 期望 {expected_version!r}"
    return True, raw[:200]


def inspect_dirty(root: Path) -> bool:
    return _git_dirty(Path(root))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_classify(args: argparse.Namespace) -> int:
    result = classify_trees(Path(args.current), Path(args.incoming))
    if args.write_meta:
        write_release_meta(
            Path(args.write_meta),
            version=args.version or "",
            compared_with=args.compared_with or "",
            classification=result,
        )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_classify_names(args: argparse.Namespace) -> int:
    if args.files:
        lines = Path(args.files).read_text(encoding="utf-8").splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    paths = [line.strip() for line in lines if line.strip()]
    result = classify_names(paths)
    if args.write_meta:
        write_release_meta(
            Path(args.write_meta),
            version=args.version or "",
            compared_with=args.compared_with or "",
            classification=result,
        )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    request = read_json(Path(args.request))
    return Updater(request).run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenBear standalone updater")
    sub = parser.add_subparsers(dest="cmd", required=True)

    classify = sub.add_parser("classify", help="compare two directory trees")
    classify.add_argument("--current", required=True)
    classify.add_argument("--incoming", required=True)
    classify.add_argument("--write-meta")
    classify.add_argument("--version", default="")
    classify.add_argument("--compared-with", default="")
    classify.set_defaults(func=_cmd_classify)

    names = sub.add_parser("classify-names", help="classify a list of relative paths")
    names.add_argument("--files", help="file with one relative path per line; default stdin")
    names.add_argument("--write-meta")
    names.add_argument("--version", default="")
    names.add_argument("--compared-with", default="")
    names.set_defaults(func=_cmd_classify_names)

    apply_cmd = sub.add_parser("apply", help="apply an update request")
    apply_cmd.add_argument("--request", required=True)
    apply_cmd.set_defaults(func=_cmd_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
