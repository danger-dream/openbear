"""运行时版本探测、更新启动与结果通知。"""
from __future__ import annotations

import asyncio
import html
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from app import __version__, installed_version
from app.logging import get_logger
from app.telegram_ui import send_rich
from app.update.loader import load_updater, updater_script_path

if TYPE_CHECKING:
    from app.services import Services

log = get_logger("update")

GITHUB_REPO = os.environ.get("OPENBEAR_GITHUB_REPO", "danger-dream/openbear")
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
POLL_INTERVAL_S = 300
USER_AGENT = "OpenBear-UpdateCheck/1.0"
RESULT_FILE = "update-result.json"
STATE_FILE = "update-state.json"
REQUEST_FILE = "update-request.json"


def data_dir_from_config(config: Any) -> Path:
    db_path = Path(getattr(getattr(config, "storage", None), "db_path", "") or "./data/openbear.db")
    db_path = db_path.expanduser()
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    return db_path.parent


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("读取 JSON 失败", 路径=str(path))
        return {}
    return data if isinstance(data, dict) else {}


class UpdateService:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.config = svc.config
        self.bot = svc.bot
        self.install_root = Path.cwd().resolve()
        self.data_dir = data_dir_from_config(svc.config)
        self.state_path = self.data_dir / STATE_FILE
        self.result_path = self.data_dir / RESULT_FILE
        self.request_path = self.data_dir / REQUEST_FILE
        self._task: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()
        self._etag = ""
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.github+json",
            },
        )
        token = (os.environ.get("OPENBEAR_GITHUB_TOKEN") or "").strip()
        if token:
            self._http.headers["Authorization"] = f"Bearer {token}"
        state = read_json(self.state_path)
        self._etag = str(state.get("etag") or "")
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("OPENBEAR_DISABLE_UPDATE_CHECK") == "1":
            log.info("版本检查未启动", 原因="测试或已禁用")
            return
        self._task = asyncio.create_task(self._loop(), name="openbear-update-check")
        log.info("版本检查已启动", 仓库=GITHUB_REPO, 当前版本=__version__)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _loop(self) -> None:
        try:
            await self._consume_result_and_notify()
            while True:
                try:
                    await self._check_github()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("检查 GitHub 发行版失败")
                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise

    def snapshot(self, *, running: dict[str, Any] | None = None) -> dict[str, Any]:
        updater = load_updater()
        state = read_json(self.state_path)
        result = read_json(self.result_path)
        available = state.get("available") if isinstance(state.get("available"), dict) else None
        current = installed_version()
        latest_version = str((available or {}).get("version") or "")
        update_available = bool(latest_version and updater.is_newer(latest_version, current))
        phase = str(state.get("phase") or "idle")
        if phase in {"done", ""}:
            phase = "idle"
        return {
            "ok": True,
            "version": current,
            "latest": available,
            "updateAvailable": update_available,
            "phase": phase,
            "dirtyWorktree": bool(updater.inspect_dirty(self.install_root)),
            "installRoot": str(self.install_root),
            "hasGit": (self.install_root / ".git").exists(),
            "lastResult": result or None,
            "lastCheckAt": int(state.get("checkedAt") or 0),
            "lastError": str(state.get("lastError") or ""),
            "running": running or {},
        }

    async def start_update(
        self,
        *,
        confirm: bool,
        force: bool,
        allow_dirty: bool,
        running: dict[str, Any],
    ) -> dict[str, Any]:
        updater = load_updater()
        if not confirm:
            return {"ok": False, "error": "confirm_required", "running": running}
        async with self._lock:
            state = read_json(self.state_path)
            phase = str(state.get("phase") or "idle")
            if phase not in {"", "idle", "done", "checking"}:
                return {"ok": False, "error": "update_in_progress", "phase": phase}
            available = state.get("available") if isinstance(state.get("available"), dict) else None
            if not available:
                return {"ok": False, "error": "no_release"}
            latest = str(available.get("version") or "")
            current = installed_version()
            if not updater.is_newer(latest, current):
                return {"ok": False, "error": "already_latest", "version": current}
            if running.get("busy") and not force:
                return {"ok": False, "error": "system_busy", "running": running}
            dirty = bool(updater.inspect_dirty(self.install_root))
            if dirty and not allow_dirty:
                return {"ok": False, "error": "dirty_worktree"}
            zip_url = str(available.get("zipUrl") or "")
            if not zip_url:
                return {"ok": False, "error": "missing_zip_asset"}
            request_id = uuid.uuid4().hex
            request = {
                "schema": 1,
                "requestId": request_id,
                "fromVersion": current,
                "toVersion": latest,
                "tag": str(available.get("tag") or f"v{latest}"),
                "zipUrl": zip_url,
                "sha256": str(available.get("sha256") or ""),
                "sha256Url": str(available.get("sha256Url") or ""),
                "installRoot": str(self.install_root),
                "dataDir": str(self.data_dir),
                "serviceName": "openbear.service",
                "healthUrl": self._health_url(),
                "force": bool(force),
                "allowDirty": bool(allow_dirty),
                "requestedAt": int(time.time()),
            }
            dest_dir = self.data_dir / "updater"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / "openbear-update.py"
            shutil.copy2(updater_script_path(), dest)
            atomic_write_json(self.request_path, request)
            state["phase"] = "starting"
            state["requestId"] = request_id
            atomic_write_json(self.state_path, state)
            try:
                await self._launch_updater(dest)
            except Exception as exc:
                state["phase"] = "idle"
                state["lastError"] = f"{type(exc).__name__}: {exc}"
                atomic_write_json(self.state_path, state)
                log.exception("启动更新器失败")
                return {"ok": False, "error": f"launch_failed: {type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "started": True,
                "requestId": request_id,
                "toVersion": latest,
                "previewRequiresRestart": bool(available.get("requiresRestart")),
            }

    def ack_result(self) -> dict[str, Any]:
        result = read_json(self.result_path)
        if not result:
            return {"ok": True, "acked": False}
        result["acked"] = True
        atomic_write_json(self.result_path, result)
        return {"ok": True, "acked": True}

    def _health_url(self) -> str:
        port = int(getattr(self.config.web, "port", 18961) or 18961)
        host = str(getattr(self.config.web, "host", "127.0.0.1") or "127.0.0.1")
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        return f"http://{host}:{port}/health"

    async def _launch_updater(self, script: Path) -> None:
        python = shutil.which("python3") or sys_executable()
        updater = load_updater()
        env = updater.with_tool_path()
        proc = await asyncio.create_subprocess_exec(
            "systemd-run",
            "--collect",
            f"--unit=openbear-update-{int(time.time())}",
            f"--setenv=PATH={env.get('PATH') or ''}",
            f"--setenv=HOME={env.get('HOME') or '/root'}",
            python,
            str(script),
            "apply",
            "--request",
            str(self.request_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_raw, stderr_raw = await proc.communicate()
        stdout = (stdout_raw or b"").decode("utf-8", "replace").strip()
        stderr = (stderr_raw or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            detail = (stderr or stdout or "no output")[:500]
            raise RuntimeError(f"systemd-run updater failed rc={proc.returncode}: {detail}")
        log.info("更新器已通过 systemd-run 启动", stdout=stdout[-500:], stderr=stderr[-500:])

    async def _check_github(self) -> None:
        if self._http is None:
            return
        headers = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        try:
            resp = await self._http.get(f"{GITHUB_API}/releases/latest", headers=headers)
        except Exception as exc:
            self._merge_state(lastError=f"{type(exc).__name__}: {exc}", checkedAt=int(time.time()))
            return
        if resp.status_code == 304:
            self._merge_state(checkedAt=int(time.time()), lastError="")
            return
        if resp.status_code == 404:
            self._merge_state(checkedAt=int(time.time()), lastError="", available=None)
            return
        if resp.status_code >= 400:
            self._merge_state(
                checkedAt=int(time.time()),
                lastError=f"github http {resp.status_code}",
            )
            return
        etag = resp.headers.get("ETag") or self._etag
        payload = resp.json()
        if not isinstance(payload, dict):
            return
        if payload.get("prerelease") or payload.get("draft"):
            self._merge_state(checkedAt=int(time.time()), etag=etag, lastError="", available=None)
            return
        available = await self._release_to_available(payload)
        state = read_json(self.state_path)
        prev = state.get("available") if isinstance(state.get("available"), dict) else {}
        notified_version = str(prev.get("notifiedVersion") or "")
        self._merge_state(
            checkedAt=int(time.time()),
            etag=etag,
            lastError="",
            available=available,
        )
        updater = load_updater()
        latest = str(available.get("version") or "")
        if latest and updater.is_newer(latest, __version__) and notified_version != latest:
            await self._notify_admins(self._new_version_text(available))
            available["notifiedVersion"] = latest
            self._merge_state(available=available)
        self._etag = etag

    async def _release_to_available(self, payload: dict[str, Any]) -> dict[str, Any]:
        tag = str(payload.get("tag_name") or "")
        version = tag[1:] if tag.startswith("v") else tag
        assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
        zip_url = ""
        sha_url = ""
        meta_url = ""
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if name == f"openbear-{version}.zip" or (name.startswith("openbear-") and name.endswith(".zip") and not zip_url):
                zip_url = url
            elif name == "SHA256SUMS":
                sha_url = url
            elif name == "release-meta.json":
                meta_url = url
        sha256 = ""
        if sha_url and self._http is not None:
            try:
                sums = await self._http.get(sha_url, headers={"Accept": "text/plain, */*"})
                if sums.status_code == 200:
                    sha256 = load_updater().parse_sha256sums(sums.text, f"openbear-{version}.zip")
                else:
                    log.warning("读取 SHA256SUMS 失败", 状态=sums.status_code)
            except Exception:
                log.exception("读取 SHA256SUMS 失败")
        requires_restart = True
        compared_with = ""
        if meta_url and self._http is not None:
            try:
                meta_resp = await self._http.get(meta_url, headers={"Accept": "application/json, */*"})
                if meta_resp.status_code == 200:
                    meta = meta_resp.json()
                    if isinstance(meta, dict):
                        requires_restart = bool(meta.get("requiresRestart", True))
                        compared_with = str(meta.get("comparedWith") or "")
                else:
                    log.warning("读取 release-meta.json 失败", 状态=meta_resp.status_code)
            except Exception:
                log.exception("读取 release-meta.json 失败")
        return {
            "version": version,
            "tag": tag,
            "name": str(payload.get("name") or tag),
            "body": str(payload.get("body") or ""),
            "publishedAt": str(payload.get("published_at") or ""),
            "htmlUrl": str(payload.get("html_url") or ""),
            "zipUrl": zip_url,
            "sha256Url": sha_url,
            "sha256": sha256,
            "requiresRestart": requires_restart,
            "comparedWith": compared_with,
        }

    async def _consume_result_and_notify(self) -> None:
        result = read_json(self.result_path)
        if not result or result.get("acked") or result.get("notified"):
            return
        text = self._result_text(result)
        if text:
            await self._notify_admins(text)
        result["notified"] = True
        atomic_write_json(self.result_path, result)

    async def _notify_admins(self, text: str) -> None:
        ids = [int(x) for x in (getattr(self.config.telegram, "whitelist_ids", None) or []) if int(x) > 0]
        if not ids or self.bot is None:
            return
        for chat_id in ids:
            try:
                await send_rich(self.bot, chat_id, text)
            except Exception:
                log.exception("发送更新通知失败", 会话=chat_id)

    def _new_version_text(self, available: dict[str, Any]) -> str:
        latest = html.escape(str(available.get("version") or ""))
        current = html.escape(installed_version())
        effect = "需要重启" if available.get("requiresRestart") else "刷新页面即可（预告，以本机对比为准）"
        return (
            "🆕 <b>OpenBear 发现新版本</b>\n"
            f"当前：<code>v{current}</code>\n"
            f"最新：<code>v{latest}</code>\n"
            f"生效方式预告：{html.escape(effect)}\n"
            "打开 Web 控制台，点击左上角版本号即可更新。"
        )

    def _result_text(self, result: dict[str, Any]) -> str:
        status = str(result.get("status") or "")
        message = html.escape(str(result.get("message") or ""))
        frm = html.escape(str(result.get("fromVersion") or "?"))
        to = html.escape(str(result.get("toVersion") or "?"))
        if status == "success":
            return f"✅ <b>OpenBear 更新成功</b>\n<code>v{frm}</code> → <code>v{to}</code>\n{message}"
        if status == "rolled_back":
            return f"⚠️ <b>OpenBear 更新失败，已回滚</b>\n目标 <code>v{to}</code>，已回到 <code>v{frm}</code>\n{message}"
        if status == "failed":
            return f"❌ <b>OpenBear 更新失败</b>\n目标 <code>v{to}</code>\n{message}"
        return ""

    def _merge_state(self, **fields: Any) -> None:
        state = read_json(self.state_path)
        state.update(fields)
        state.setdefault("schema", 1)
        atomic_write_json(self.state_path, state)


def sys_executable() -> str:
    return os.environ.get("OPENBEAR_PYTHON") or shutil.which("python3") or "/usr/bin/python3"
