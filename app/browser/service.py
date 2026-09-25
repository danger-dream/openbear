from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import aiohttp
from pydantic import ValidationError

from app.browser.cdp import CDP
from app.browser.connection import probe_connection
from app.browser.contract import ACTIONS, Request
from app.browser.worker import Worker, WorkerError
from app.interaction_data import explicit_authorization


@dataclass
class Page:
    id: str
    instance: str
    epoch: str
    target: str
    owner: str = ""
    state: str = "unverified"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    failure: dict | None = None
    failed_url: str = ""
    retry_at: float = 0


@dataclass
class CallBudget:
    deadline: float
    phase: str = "connect_browser"
    page: Page | None = None
    dispatched: bool = False
    page_created: bool = False

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return value


call_budget = contextvars.ContextVar("browser_call_budget", default=None)


@dataclass
class Instance:
    id: str
    cdp: CDP
    worker: Worker
    epoch: str = ""
    last_recovery: float = 0
    failures: int = 0
    active: int = 0
    init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker_limits: dict = field(default_factory=dict)


class BrowserService:
    def __init__(self, config, workspace: str, state_dir: str | None = None):
        self.config = config
        self.workspace = Path(workspace).resolve()
        self.state_dir = Path(
            state_dir or Path(config.storage.db_path).resolve().parent / "browser"
        )
        self.pages: dict[str, Page] = {}
        self.instances: dict[str, Instance] = {}
        self._instance_lock = asyncio.Lock()
        self._owner_locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self._verified_endpoint = ""
        self._connection_generation = 0
        self.connection_status = {"ok": False, "code": "browser_connection_unverified"}
        self.configure(config)
        self._load_pages()

    @property
    def cfg(self):
        return self.config.browser

    @property
    def available(self):
        return bool(
            self.cfg.enabled and not self._closed and self.cfg.main_endpoint
            and self._verified_endpoint == self.cfg.main_endpoint
        )

    def configure(self, config):
        old = (self.cfg.enabled, self.cfg.main_endpoint)
        self.config = config
        if old != (self.cfg.enabled, self.cfg.main_endpoint):
            self._connection_generation += 1
            self._verified_endpoint = ""
            self.connection_status = {"ok": False, "code": "browser_connection_unverified"}
        if self.cfg._connection_verified and self.cfg.enabled:
            self._verified_endpoint = self.cfg.main_endpoint
            self.connection_status = {"ok": True}
            self.cfg._connection_verified = False
        if not self.cfg.enabled:
            self._verified_endpoint = ""

    async def validate_connection(self):
        generation = self._connection_generation
        config = self.cfg.model_copy(deep=True)
        if not config.enabled or self._closed:
            return {"ok": False, "applied": False}
        result = await probe_connection(config)
        if generation != self._connection_generation or self._closed or not self.cfg.enabled:
            return {**result, "applied": False}
        self._verified_endpoint = config.main_endpoint if result["ok"] else ""
        self.connection_status = result
        return {**result, "applied": True}

    @staticmethod
    def owner(ctx):
        if ctx.agent_session_uuid:
            return "agent:" + ctx.agent_session_uuid
        if ctx.conversation_uuid:
            return "conversation:" + ctx.conversation_uuid
        if ctx.session_uuid:
            return "session:" + ctx.session_uuid
        if ctx.chat_id:
            return "chat:" + str(ctx.chat_id)
        raise ValueError("browser_owner_missing")

    def _load_pages(self):
        try:
            for row in json.loads((self.state_dir / "pages.json").read_text()):
                if set(row) == {"id", "instance", "epoch", "target", "owner", "state"} and row[
                    "instance"
                ].startswith("main-"):
                    self.pages[row["id"]] = Page(**row)
        except (OSError, ValueError, TypeError):
            pass

    def _save_pages(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            {k: getattr(p, k) for k in ("id", "instance", "epoch", "target", "owner", "state")}
            for p in self.pages.values()
        ]
        temporary = self.state_dir / "pages.tmp"
        temporary.write_text(json.dumps(rows), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.state_dir / "pages.json")

    async def _confirm(self, ctx, body):
        if ctx.agent_session_uuid or ctx.web_confirm is None:
            return {"confirmed": False, "error": "main_controller_confirmation_required"}
        return await ctx.web_confirm(
            {
                "action": "confirm",
                "title": "浏览器操作确认",
                "body": body,
                "type": "warning",
                "default": False,
                "timeoutSeconds": 600,
                "_sourceTool": "Browser",
                "_requiresAuthorization": True,
            }
        )

    async def _instance(self):
        endpoint = self.cfg.main_endpoint
        if not endpoint:
            raise ValueError(
                "main_endpoint_missing: configure the browser service connection; local browser launching is no longer supported"
            )
        # Keep the existing identity so saved page ownership/login state survives.
        key = "main-" + hashlib.sha256(endpoint.encode()).hexdigest()[:16]
        async with self._instance_lock:
            if key not in self.instances:
                self.instances[key] = Instance(
                    key,
                    CDP(endpoint, self.cfg.connect_timeout_s),
                    Worker(),
                )
            return self.instances[key]

    async def _sync(self, inst):
        await inst.cdp.connect()
        epoch = hashlib.sha256(inst.cdp.ws_url.encode()).hexdigest()[:20]
        targets = (await inst.cdp.call("Target.getTargets"))["targetInfos"]
        targets = [
            x
            for x in targets
            if x.get("type") == "page" and not str(x.get("url", "")).startswith("devtools:")
        ]
        live = {x["targetId"] for x in targets}
        for key, page in list(self.pages.items()):
            if page.instance == inst.id and (page.epoch != epoch or page.target not in live):
                self.pages.pop(key)
        inst.epoch = epoch
        by_target = {p.target: p for p in self.pages.values() if p.instance == inst.id}
        result = []
        for t in targets:
            page = by_target.get(t["targetId"])
            if page is None:
                opener = by_target.get(t.get("openerId"))
                page = Page(
                    "p" + uuid.uuid4().hex[:12],
                    inst.id,
                    epoch,
                    t["targetId"],
                    owner=opener.owner if opener else "",
                )
                self.pages[page.id] = page
            result.append((page, t))
        self._save_pages()
        return result

    async def _worker(self, inst):
        async with inst.init_lock:
            limits = {
                "snapshotMaxChars": self.cfg.snapshot_max_chars,
                "maxEventEntries": self.cfg.max_event_entries,
                "maxBodyBytes": self.cfg.max_body_bytes,
                "maxArtifactBytes": self.cfg.max_artifact_bytes,
            }
            if inst.worker.connected:
                if inst.worker_limits != limits:
                    await inst.worker.call("configure", limits, timeout=2)
                    inst.worker_limits = limits
                return
            if inst.failures:
                if not self.cfg.auto_reconnect:
                    raise WorkerError("reconnect_disabled", "not_started")
                if time.monotonic() - inst.last_recovery < self.cfg.recovery_cooldown_s:
                    raise WorkerError("recovery_cooldown", "not_started")
                if inst.failures >= 3:
                    raise WorkerError(
                        "recovery_circuit_open: use recover connection", "not_started"
                    )
            inst.last_recovery = time.monotonic()
            try:
                if bool(self.cfg.main_download_host_path) != bool(
                    self.cfg.main_download_browser_path
                ):
                    raise WorkerError(
                        "download_mapping_incomplete: configure both shared download paths",
                        "not_started",
                    )
                mapped = bool(self.cfg.main_download_host_path)
                base = (
                    Path(self.cfg.main_download_host_path)
                    if mapped
                    else self.state_dir / "downloads" / inst.id
                )
                generation = "w" + uuid.uuid4().hex
                host_path = base / generation
                host_path.mkdir(parents=True, exist_ok=True)
                browser_path = (
                    str(PurePosixPath(self.cfg.main_download_browser_path) / generation)
                    if mapped
                    else str(host_path.resolve())
                )
                await inst.worker.start(
                    {
                        "endpoint": inst.cdp.ws_url,
                        "downloadHostPath": str(host_path.resolve()),
                        "downloadBrowserPath": browser_path,
                        "connectTimeoutS": self.cfg.connect_timeout_s,
                        **limits,
                    }
                )
                inst.failures = 0
                inst.worker_limits = limits
            except BaseException:
                inst.failures += 1
                raise

    async def _owned_page(self, request, owner, *, verify=True):
        if not request.page:
            raise ValueError("page_required")
        page = self.pages.get(request.page)
        if page is None:
            raise ValueError("page_not_found: list pages again")
        if page.owner != owner:
            raise ValueError("page_not_owned: explicit adoption/handoff required")
        inst = self.instances.get(page.instance)
        if inst is None:
            inst = await self._instance()
            if inst.id != page.instance:
                raise ValueError("page_endpoint_changed")
        if verify:
            await self._sync(inst)
        if request.page not in self.pages:
            raise ValueError("page_expired: browser or document target changed")
        return page, inst

    def _artifact(self, extension):
        folder = self.workspace / "artifacts" / "browser" / time.strftime("%Y%m%d")
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (uuid.uuid4().hex + extension)

    async def call(self, args, ctx):
        if not self.cfg.enabled or self._closed:
            return {"status": "error", "error": "browser_disabled", "outcome": "not_started"}
        if not self.available:
            return {
                "status": "error",
                "error": "browser_connection_unverified" if self.cfg.main_endpoint else "main_endpoint_missing",
                "outcome": "not_started",
            }
        if ctx.agent_session_uuid and not self.cfg.agent_access:
            return {
                "status": "error",
                "error": "browser_agent_access_denied",
                "outcome": "not_started",
            }
        try:
            req = Request.model_validate(args)
            owner = self.owner(ctx)
            return await self._budgeted_call(req, owner, ctx)
        except asyncio.CancelledError:
            raise
        except ValidationError as exc:
            return {
                "status": "error",
                "error": "invalid_browser_arguments",
                "detail": [
                    {"loc": list(e["loc"]), "msg": e["msg"]}
                    for e in exc.errors(include_input=False)
                ],
                "outcome": "not_started",
            }
        except ValueError as exc:
            return {"status": "error", "error": str(exc), "outcome": "not_started"}
        except WorkerError as exc:
            return {"status": "error", "error": str(exc), "outcome": exc.outcome, **exc.details}
        except Exception as exc:
            return {
                "status": "error",
                "error": "browser_control_failed",
                "kind": type(exc).__name__,
                "outcome": "unknown",
            }

    async def _budgeted_call(self, req, owner, ctx):
        # Confirmation/control calls keep their own gates. Data-plane calls share
        # one deadline across connect, page creation, worker setup and navigation.
        timed = (
            req.action not in {"status", "describe", "recover", "page"}
            or (req.action == "page" and req.params["op"] in {"new", "list"})
            or (req.action == "recover" and req.params["op"] == "probe")
        )
        if not timed:
            return await self._call(req, owner, ctx)
        navigation = req.action == "navigate" or (req.action == "page" and req.params.get("url"))
        duration = min(
            req.timeoutMs / 1000
            if req.timeoutMs
            else (self.cfg.navigation_timeout_s if navigation else self.cfg.action_timeout_s),
            self.cfg.max_timeout_s,
        )
        budget = CallBudget(time.monotonic() + duration, page=self.pages.get(req.page))
        token = call_budget.set(budget)
        try:
            async with asyncio.timeout_at(budget.deadline):
                return await self._call(req, owner, ctx)
        except TimeoutError:
            preparing = budget.page_created and budget.phase != "execute_navigate"
            outcome = (
                "failed"
                if preparing
                else ("unknown" if self._can_mutate(req) else "failed")
                if budget.dispatched
                else "not_started"
            )
            details = {"phase": budget.phase}
            if budget.page_created:
                details["pageCreated"] = True
                if preparing:
                    details["navigationStarted"] = False
            exc = WorkerError("request_timeout", outcome, details=details)
            if budget.page and budget.page.instance in self.instances:
                if not budget.dispatched and not budget.page_created:
                    return {
                        "status": "error",
                        "error": "request_timeout",
                        "outcome": "not_started",
                        "phase": budget.phase,
                        "page": budget.page.id,
                        "replayed": False,
                        "recovery": "This operation was not dispatched; no page action was replayed.",
                    }
                return await self._operation_error(
                    (Request.model_construct(action="prepare", params={}) if preparing else req),
                    budget.page,
                    self.instances[budget.page.instance],
                    exc,
                )
            return {
                "status": "error",
                "error": "page_creation_timeout"
                if budget.dispatched
                else (
                    "page_creation_queue_timeout"
                    if budget.phase == "page_creation_queue"
                    else "browser_connection_timeout"
                ),
                "outcome": outcome,
                "phase": budget.phase,
                "replayed": False,
                "retryable": False,
                "recovery": (
                    "Page creation may have completed. List pages to inspect the result; do not create another page blindly."
                    if budget.dispatched
                    else "Check the reported phase and configured browser connection; no page action was dispatched."
                ),
            }
        finally:
            call_budget.reset(token)

    @staticmethod
    def _can_mutate(req):
        return (
            req.action in {"act", "navigate", "evaluate", "page"}
            or (req.action == "dialog" and req.params.get("op") == "handle")
            or (req.action == "files" and req.params.get("op") != "list")
        )

    async def _diagnose(self, page, inst):
        # This never enters the page queue, initializes a worker or takes a
        # snapshot. All checks are read-only and fit the original call budget.
        result = {"browserResponsive": None, "pageResponsive": None, "documentReady": None}
        budget = call_budget.get()
        remaining = budget.deadline - time.monotonic() if budget else 1
        if remaining <= 0.02:
            return {**result, "reason": "diagnostic_budget_exhausted"}
        try:
            async with asyncio.timeout(min(1, remaining)):
                await inst.cdp.call("Browser.getVersion", timeout=min(1, remaining))
                result["browserResponsive"] = True
                reply = await inst.cdp.target_call(
                    page.target,
                    "Runtime.evaluate",
                    {
                        "expression": "({readyState:document.readyState,url:location.href})",
                        "returnByValue": True,
                    },
                    timeout=min(1, remaining),
                )
                value = reply.get("result", {}).get("value")
                result["pageResponsive"] = True
                if isinstance(value, dict):
                    result.update(
                        readyState=value.get("readyState"),
                        currentUrl=value.get("url"),
                        documentReady=value.get("readyState") in {"interactive", "complete"},
                    )
        except (TimeoutError, ConnectionError, RuntimeError):
            result["reason"] = (
                "page_probe_unavailable"
                if result["browserResponsive"]
                else "control_probe_unavailable"
            )
        return result

    @staticmethod
    def _blocked(page):
        return {
            "status": "error",
            "error": "page_not_ready",
            "outcome": "not_started",
            "phase": "preflight",
            "page": page.id,
            "replayed": False,
            "retryable": False,
            "blockedBy": page.failure,
            "recovery": page.failure["recovery"],
        }

    async def _operation_error(self, req, page, inst, exc):
        details = getattr(exc, "details", {})
        outcome = getattr(exc, "outcome", "unknown" if self._can_mutate(req) else "failed")
        if outcome == "unknown" and not self._can_mutate(req):
            outcome = "failed"
        error = str(exc) or "action_timeout"
        result = {
            "status": "error",
            "error": error,
            "outcome": outcome,
            "page": page.id,
            "replayed": False,
            **details,
        }
        uncertain = outcome == "unknown"
        failed_page = error in {
            "navigation_failed",
            "navigation_timeout",
            "action_timeout",
            "request_timeout",
            "browser_connection_lost",
            "page_preparation_failed",
            "page_not_ready",
        }
        block_page = failed_page
        if error == "navigation_failed":
            result.update(
                retryable=False,
                recovery="The navigation failed. Correct the target URL or browser-side network access, then navigate explicitly. Snapshot cannot repair this failure.",
            )
        elif failed_page or uncertain:
            diagnosis = result["diagnostics"] = await self._diagnose(page, inst)
            readable = diagnosis["pageResponsive"] is True and diagnosis["documentReady"] is True
            block_page = failed_page or not readable
            result.update(
                retryable=False,
                recovery=(
                    "Do not repeat the action or snapshot. Use recover probe to check page readiness; consult the reported error and phase. Close/terminate/restart still require their existing authorization."
                    if block_page
                    else "The operation failed but the page is responsive. Snapshot may inspect the current state. Correct the script/action before explicitly executing it again; earlier effects remain unconfirmed and nothing was replayed."
                ),
            )
        else:
            result.update(
                retryable=False,
                recovery="Correct the reported operation error before issuing a new action.",
            )
        if failed_page or uncertain:
            page.state = "suspect" if uncertain else "failed"
            page.failure = dict(result) if block_page else None
            page.failed_url = req.params.get("url", "")
            page.retry_at = time.monotonic() + max(2, self.cfg.recovery_cooldown_s)
            self._save_pages()
        return result

    async def _call(self, req, owner, ctx):
        a, p = req.action, req.params
        if a == "describe":
            return {
                "status": "ok",
                "actions": {p["action"]: ACTIONS[p["action"]]} if p.get("action") else ACTIONS,
            }
        if a == "status":
            return {
                "status": "ok",
                "enabled": True,
                "networkScope": "configured_browser",
                "instances": [
                    {
                        "id": i.id,
                        "cdpConnected": i.cdp.connected,
                        "workerConnected": i.worker.connected,
                        "workerGeneration": i.worker.generation,
                        "failures": i.failures,
                        "pages": sum(x.instance == i.id for x in self.pages.values()),
                    }
                    for i in self.instances.values()
                ],
                "dependenciesInstalled": all(
                    (Path(__file__).parent / "pyworker/resources" / name).is_file()
                    for name in ("injected.js", "bridge.js", "keys.json", "provenance.json")
                ),
                "engine": "python-cdp",
                "note": "Cached status only; no browser was started or probed. URLs and localhost resolve in the configured browser's network, not OpenBear's. A local CDP endpoint does not prove shared networking.",
            }
        if a == "page":
            if p["op"] == "new":
                if budget := call_budget.get():
                    budget.phase = "page_creation_queue"
                lock = self._owner_locks.setdefault(owner, asyncio.Lock())
                try:
                    await asyncio.wait_for(lock.acquire(), self.cfg.queue_timeout_s)
                except TimeoutError:
                    raise ValueError("page_creation_queue_timeout") from None
                try:
                    return await self._page(req, owner, ctx)
                finally:
                    lock.release()
            return await self._page(req, owner, ctx)
        if a == "recover":
            return await self._recover(req, owner, ctx)
        page, inst = await self._owned_page(req, owner, verify=False)
        if budget := call_budget.get():
            budget.page = page
        if page.failure and a not in {"console", "network", "dialog"}:
            # A fresh, deliberate navigation can correct an address. Never silently
            # replay the failed navigation or let a snapshot enter the same timeout.
            corrected = (
                a == "navigate"
                and p.get("op") == "goto"
                and (p.get("url") != page.failed_url or time.monotonic() >= page.retry_at)
                and page.failure.get("outcome") != "unknown"
            )
            if corrected:
                page.failure = None
                page.state = "ready"
            else:
                return self._blocked(page)
        await self._sync(inst)
        if page.id not in self.pages:
            raise ValueError("page_expired: browser or document target changed")
        if a == "evaluate" and not self.cfg.allow_evaluate:
            raise ValueError("evaluate_disabled")
        if page.state == "suspect" and a not in {
            "snapshot",
            "find",
            "capture",
            "console",
            "network",
            "dialog",
        }:
            raise ValueError("page_outcome_unknown: inspect or recover before another mutation")
        if a == "files" and p.get("paths") is not None:
            paths = [Path(x).expanduser().resolve() for x in p["paths"]]
            if any(not x.is_file() for x in paths):
                raise ValueError("upload_path_not_file")
            if sum(x.stat().st_size for x in paths) > self.cfg.max_artifact_bytes:
                raise ValueError("upload_too_large")
            p = {**p, "paths": [str(x) for x in paths]}
        duration = min(
            req.timeoutMs / 1000
            if req.timeoutMs
            else (self.cfg.navigation_timeout_s if a == "navigate" else self.cfg.action_timeout_s),
            self.cfg.max_timeout_s,
        )
        params = {**p, "targetId": page.target, "timeoutMs": int(duration * 1000)}
        if a in {"snapshot", "find"}:
            params["maxChars"] = min(
                p.get("maxChars", self.cfg.snapshot_max_chars), self.cfg.snapshot_max_chars
            )
        artifact = None
        if (
            a in {"capture", "evaluate"}
            or (a == "files" and p["op"] == "save")
            or (a == "network" and p.get("part", "").endswith("body"))
        ):
            artifact = self._artifact(
                ".png" if a == "capture" else ".json" if a == "evaluate" else ".bin"
            )
            params["outputPath"] = str(artifact)
        locked = False
        inst.active += 1
        try:
            # Dialog handling must be able to unblock an operation holding the page lock.
            if a != "dialog":
                if budget := call_budget.get():
                    budget.phase = "page_queue"
                try:
                    await asyncio.wait_for(page.lock.acquire(), self.cfg.queue_timeout_s)
                except TimeoutError:
                    return {
                        "status": "error",
                        "error": "page_queue_timeout",
                        "outcome": "not_started",
                    }
                locked = True
                # Another operation may have failed while this one was queued.
                if page.failure and a not in {"console", "network"}:
                    return self._blocked(page)
            try:
                if budget := call_budget.get():
                    budget.phase = "initialize_worker"
                await self._worker(inst)
            except Exception as exc:
                return {
                    "status": "error",
                    "error": "browser_worker_unavailable",
                    "phase": "initialize_worker",
                    "page": page.id,
                    "detail": str(exc)[:300],
                    "outcome": "not_started",
                    "recovery": "Independent page list/recover remains available",
                }
            try:
                if budget := call_budget.get():
                    budget.phase = "execute_" + a
                    duration = budget.remaining()
                    # Reserve a small part of the SAME budget for IPC and an
                    # independent, read-only diagnosis. Never grant a second deadline.
                    reserve = min(1.2, duration * 0.2)
                    params["timeoutMs"] = max(1, int((duration - reserve) * 1000))
                    budget.dispatched = True
                    duration -= reserve / 2
                result = await inst.worker.call(a, params, timeout=duration)
            except (TimeoutError, WorkerError, asyncio.CancelledError) as exc:
                responded = isinstance(exc, WorkerError) and exc.responded
                if not responded or str(exc) == "browser_connection_lost":
                    inst.failures += 1
                    inst.last_recovery = time.monotonic()
                    await inst.worker.close()
                # A normal navigation/DOM timeout is not a crashed worker.
                if isinstance(exc, asyncio.CancelledError):
                    page.state = "suspect"
                    self._save_pages()
                    raise
                return await self._operation_error(req, page, inst, exc)
            if a in {"snapshot", "find", "capture", "navigate", "prepare"}:
                page.failure = None
                page.state = "ready"
                self._save_pages()
            if result.get("artifact"):
                actual = Path(result.pop("artifact")).resolve()
                if artifact is None or actual != artifact.resolve() or not actual.is_file():
                    raise RuntimeError("invalid_worker_artifact")
                if actual.stat().st_size > self.cfg.max_artifact_bytes:
                    actual.unlink()
                    raise RuntimeError("artifact_too_large")
                reference = "workspace/" + actual.relative_to(self.workspace).as_posix()
                result["artifact"] = reference
                result["link"] = f"[{actual.name}]({reference})"
                if a == "capture" and p.get("view"):
                    ctx.tool_images.append(
                        {
                            "type": "image",
                            "path": str(actual),
                            "mime_type": "image/png",
                            "name": actual.name,
                        }
                    )
                    result["imageInput"] = True
            return {"status": "ok", "outcome": "completed", "page": page.id, **result}
        finally:
            inst.active -= 1
            if locked:
                page.lock.release()
            if artifact and artifact.exists() and artifact.stat().st_size == 0:
                artifact.unlink(missing_ok=True)

    async def _page(self, req, owner, ctx):
        p, op = req.params, req.params["op"]
        if op == "close":
            page, inst = await self._owned_page(req, owner)
            # Explicit close is an ordinary requested page operation, serialized with its work.
            try:
                await asyncio.wait_for(page.lock.acquire(), self.cfg.queue_timeout_s)
            except TimeoutError:
                raise ValueError("page_busy: use recover close for a stuck operation")
            try:
                await inst.cdp.call("Target.closeTarget", {"targetId": page.target})
                self.pages.pop(page.id, None)
                self._save_pages()
                return {"status": "ok", "closed": page.id, "liveStateLost": True}
            finally:
                page.lock.release()
        if op == "adopt":
            if not req.page or req.page not in self.pages:
                raise ValueError("page_not_found")
            page = self.pages[req.page]
            if page.owner and page.owner != owner:
                raise ValueError("page_owned_by_another_session")
            if page.owner == owner:
                return {"status": "ok", "page": page.id, "owned": True}
            confirmation = await self._confirm(
                ctx, f"将已有标签 {page.id} 交给本会话操作。不会复制或更换登录状态。"
            )
            if not explicit_authorization(confirmation):
                return {"status": "denied", "outcome": "not_started", "confirmation": confirmation}
            page.owner = owner
            self._save_pages()
            return {"status": "ok", "page": page.id, "owned": True}
        if budget := call_budget.get():
            budget.phase = "connect_browser"
        inst = await self._instance()
        inst.active += 1
        try:
            rows = await self._sync(inst)
            if op == "new":
                if (
                    sum(x.owner == owner for x in self.pages.values())
                    >= self.cfg.max_pages_per_owner
                ):
                    raise ValueError("owner_page_limit_reached")
                if budget := call_budget.get():
                    budget.phase = "create_page"
                    budget.dispatched = True
                target = (await inst.cdp.call("Target.createTarget", {"url": "about:blank"}))[
                    "targetId"
                ]
                page = Page("p" + uuid.uuid4().hex[:12], inst.id, inst.epoch, target, owner=owner)
                self.pages[page.id] = page
                if budget := call_budget.get():
                    budget.page = page
                    budget.page_created = True
                    budget.dispatched = False
                self._save_pages()
                # prepare is a private, read-only worker operation; it is never
                # accepted by the public Request validator or exposed as a tool.
                prepare = Request.model_construct(
                    action="prepare", page=page.id, timeoutMs=req.timeoutMs, params={}
                )
                if budget := call_budget.get():
                    budget.phase = "prepare_page"
                try:
                    prepared = await self._call(prepare, owner, ctx)
                except Exception as exc:
                    prepared = await self._operation_error(
                        prepare,
                        page,
                        inst,
                        WorkerError(
                            "page_preparation_failed",
                            "failed",
                            details={
                                "phase": "prepare_page",
                                "kind": type(exc).__name__,
                            },
                        ),
                    )
                if prepared["status"] != "ok":
                    failure = {
                        **prepared,
                        "outcome": "failed",
                        "pageCreated": True,
                        "navigationStarted": False,
                    }
                    page.state = "failed"
                    page.failure = dict(failure)
                    self._save_pages()
                    return failure
                if budget := call_budget.get():
                    budget.dispatched = False
                if p.get("url") and p["url"] != "about:blank":
                    result = await self._call(
                        Request(
                            action="navigate",
                            page=page.id,
                            timeoutMs=req.timeoutMs,
                            params={"url": p["url"]},
                        ),
                        owner,
                        ctx,
                    )
                    return {**result, "pageCreated": True}
                return {"status": "ok", "page": page.id, "ready": True, "outcome": "completed"}
            return {
                "status": "ok",
                "pages": [
                    {
                        "page": x.id,
                        "url": t.get("url", ""),
                        "title": t.get("title", ""),
                        "owned": x.owner == owner,
                        "adoptable": not x.owner,
                        "state": x.state,
                    }
                    for x, t in rows
                    if x.owner == owner or (not ctx.agent_session_uuid and not x.owner)
                ],
            }
        finally:
            inst.active -= 1

    async def _recover(self, req, owner, ctx):
        op = req.params["op"]
        page = None
        if req.page:
            page, inst = await self._owned_page(
                req, owner, verify=op not in {"connection", "restart"}
            )
        else:
            if op in {"probe", "terminate", "close"}:
                raise ValueError("page_required")
            inst = await self._instance()
        if op == "probe":
            diagnosis = await self._diagnose(page, inst)
            url = diagnosis.get("currentUrl", "")
            ready = (
                diagnosis["pageResponsive"] is True
                and diagnosis["documentReady"] is True
                and not url.startswith("chrome-error:")
                and not (page.failure and page.failure.get("error") == "navigation_failed")
                and not (
                    page.failed_url and page.failed_url != "about:blank" and url == "about:blank"
                )
            )
            if ready:
                page.state = "ready"
                page.failure = None
            else:
                page.state = "suspect"
            self._save_pages()
            return {
                "status": "ok",
                **diagnosis,
                "pageReady": ready,
                "outcome": "completed",
                "note": "Probe checks document readiness; it does not establish the outcome of any prior action or replay it.",
            }
        if op == "connection":
            if time.monotonic() - inst.last_recovery < self.cfg.recovery_cooldown_s:
                raise ValueError("recovery_cooldown")
            if inst.active:
                raise ValueError("instance_busy")
            await inst.worker.close()
            await inst.cdp.close()
            inst.failures = 0
            await self._sync(inst)
            await self._worker(inst)
            return {
                "status": "ok",
                "connection": "restored",
                "pageState": "unverified",
                "replayed": False,
            }
        if op == "restart" and not self.cfg.main_restart_url:
            raise ValueError(
                "external_restart_not_configured: set browser.mainRestartUrl or ask the browser owner"
            )
        restart_url = self.cfg.main_restart_url
        if op == "restart" and inst.cdp.endpoint != self.cfg.main_endpoint:
            raise ValueError(
                "instance_configuration_changed: do not restart an old endpoint with new settings"
            )
        impact = "此操作影响该浏览器实例的所有会话页面。" if op == "restart" else ""
        confirmation = await self._confirm(
            ctx,
            f"执行 {op}，对象 {page.id if page else inst.id}。{impact}可能终止脚本或丢失页面未保存状态，不会重放原操作。",
        )
        if not explicit_authorization(confirmation):
            return {"status": "denied", "outcome": "not_started", "confirmation": confirmation}
        if op == "terminate":
            await inst.cdp.target_call(page.target, "Runtime.terminateExecution", timeout=2)
            page.state = "suspect"
        elif op == "close":
            await inst.cdp.call("Target.closeTarget", {"targetId": page.target})
            self.pages.pop(page.id, None)
        elif op == "restart":
            if inst.active:
                raise ValueError("instance_busy")
            # Independent of CDP/worker health. Configured by the user, never
            # supplied by the model; still gated above, never retried.
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.cfg.connect_timeout_s), trust_env=False
            ) as http:
                async with http.post(restart_url, allow_redirects=False) as response:
                    response.raise_for_status()
                    if response.status >= 300:
                        raise RuntimeError("restart_redirect_refused")
            await inst.worker.close()
            await inst.cdp.close()
            for key, old in list(self.pages.items()):
                if old.instance == inst.id:
                    self.pages.pop(key)
            self._save_pages()
            await self._sync(inst)
        await inst.worker.close()
        inst.failures = 0
        self._save_pages()
        return {
            "status": "ok",
            "recovery": op,
            "pageState": "unverified",
            "liveStateLost": op in {"close", "restart"},
            "replayed": False,
        }

    async def close(self):
        self._closed = True
        # Only release OpenBear's control clients. The configured browser,
        # its pages, profile and login state are never owned by this service.
        for inst in list(self.instances.values()):
            await inst.worker.close()
            await inst.cdp.close()
        self.instances.clear()
