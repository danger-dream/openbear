"""Management-key HTTP API. Cookie owner auth cannot replace management auth."""
from __future__ import annotations

import base64
import binascii
import hmac
import os
import secrets
from pathlib import Path, PurePosixPath

from aiohttp import web

from app.managed.configuration import BUSINESS_FIELDS, business_config, merge_patch

BUSINESS_ROOTS = frozenset({
    "conversations", "conversation-tree", "conversation-folders", "prompt-tools",
    "prompt-template", "events", "reference-catalog", "references", "reference-history",
    "audit-logs", "statistics", "mcp", "settings", "models-dev", "channels", "memory",
    "rath", "skills", "webhooks", "cron", "push", "notes", "kb-notes",
})


def business_route_allowed(method: str, path: str) -> bool:
    if method.upper() not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        return False
    if (not path.startswith("/api/") or "\\" in path or "//" in path or "%" in path
            or any(p in {".", ".."} for p in path.split("/"))):
        return False
    if path == "/api/system/version":
        return method.upper() in {"GET", "HEAD"}
    if path == "/api/settings/web-task-notifications/test":
        return False
    return path.split("/")[2] in BUSINESS_ROOTS


# Exact application documents, including routes installed by feature modules.
# Login belongs to the gateway; instance administration is controller-only.
GATEWAY_APP_PAGES = frozenset({
    "/", "/chat", "/memory", "/secrets", "/docs", "/notes", "/mcp", "/skills", "/settings",
    "/statistics", "/webhooks", "/cron",
})


def gateway_frontend_route_allowed(request, host) -> bool:
    from app.web_console.auth_api import PWA_PUBLIC_FILES

    path = request.path
    if (request.method not in {"GET", "HEAD"} or "\\" in path or "//" in path or "%" in path
            or any(part in {".", ".."} for part in path.split("/"))
            or any(ord(char) <= 32 or ord(char) == 127 for char in path)):
        return False
    # Match the actual installed handler as well as its public path. No generic
    # static/file fallback, API prefix, or newly added document gets authorized.
    handler = request.match_info.handler
    return ((path in GATEWAY_APP_PAGES and handler == host.handle_index)
            or (path.startswith("/assets/") and handler == host.handle_asset)
            or (path in PWA_PUBLIC_FILES and handler == host.handle_pwa_asset))


def valid_management_key(request, runtime) -> bool:
    supplied = request.headers.get("Authorization", "")
    expected = "Bearer " + runtime.settings.management_key
    return hmac.compare_digest(supplied.encode(), expected.encode())


GATEWAY_HEADERS = ("X-OpenBear-Gateway-Key", "X-OpenBear-Instance", "X-OpenBear-Gateway-Session",
                   "X-OpenBear-Gateway-Origin", "X-OpenBear-Gateway-Expires")


def has_gateway_credentials(request) -> bool:
    return any(name in request.headers for name in GATEWAY_HEADERS)


def valid_gateway_key(request, runtime, *, require_session=True) -> bool:
    settings = runtime.settings
    if settings.auth_mode != "gateway" or not settings.gateway_key:
        return False
    if request.headers.get("Authorization") or request.headers.get("Cookie"):
        return False  # Gateway must strip browser credentials; never mix auth schemes.
    key = request.headers.get("X-OpenBear-Gateway-Key", "")
    instance = request.headers.get("X-OpenBear-Instance", "")
    if not (hmac.compare_digest(key.encode(), settings.gateway_key.encode())
            and hmac.compare_digest(instance.encode(), settings.instance_id.encode())):
        return False
    if require_session:
        session = request.headers.get("X-OpenBear-Gateway-Session", "")
        if not 1 <= len(session) <= 1024 or any(ord(c) <= 32 or ord(c) >= 127 for c in session):
            return False
    return True


def error(code: str, status=400):
    return web.json_response({"ok": False, "error": code}, status=status, headers={"Cache-Control": "no-store"})


def install(app, host, runtime):
    async def status(request):
        return web.json_response(await runtime.status(), headers={"Cache-Control": "no-store"})

    async def get_config(request):
        return web.json_response({"ok": True, "config": business_config(host.config)}, headers={"Cache-Control": "no-store"})

    async def patch_config(request):
        try:
            patch = await request.json()
            confirmations = patch.pop("mcpExposureConfirmations", None) if isinstance(patch, dict) else None
            if not isinstance(patch, dict) or not patch or set(patch) - BUSINESS_FIELDS:
                return error("business_config_fields_required")
            def apply(raw):
                merge_patch(raw, patch)
            if "mcp" in patch:
                import copy

                from app.config import Config
                before = await host.config_store.load_raw()
                candidate = copy.deepcopy(before)
                apply(candidate)
                response = await host._mcp_check_exposure(Config.model_validate(candidate), {"exposureConfirmations": confirmations})
                if response is not None:
                    return response
                def checked(raw):
                    if raw != before:
                        raise ValueError("mcp_config_changed_during_discovery")
                    apply(raw)
                config = await host.config_store.mutate(checked)
            else:
                config = await host.config_store.mutate(apply)
            host._apply_runtime_config(config)
        except Exception:
            return error("invalid_business_config")
        return await get_config(request)

    async def create_job(request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"command", "timeoutSeconds"}:
                return error("invalid_job")
            job = await runtime.jobs.create(body.get("command"), body.get("timeoutSeconds", 120))
        except (ValueError, TypeError):
            return error("invalid_job")
        except RuntimeError:
            return error("job_capacity_reached", 429)
        return web.json_response({"ok": True, "job": job}, status=202)

    async def get_job(request):
        job = await runtime.jobs.get(request.match_info["id"])
        return web.json_response({"ok": True, "job": job}) if job else error("job_not_found", 404)

    async def cancel_job(request):
        job = await runtime.jobs.cancel(request.match_info["id"])
        return web.json_response({"ok": True, "job": job}) if job else error("job_not_found", 404)

    async def install_file(request):
        kind = request.match_info["kind"]
        configured = host.config.tools.skills_dir if kind == "skills" else host.config.mcp.install_dir
        root = Path(configured).resolve()
        relative = request.match_info["path"]
        parts = PurePosixPath(relative).parts
        if (not parts or relative.startswith("/") or "\\" in relative
                or any(p in {"", ".", ".."} or p.startswith(".") for p in relative.split("/"))
                or any(ord(c) < 32 for c in relative)):
            return error("unsafe_install_path")
        candidate = root.joinpath(*parts)
        if not candidate.resolve().is_relative_to(root):
            return error("unsafe_install_path")
        if any(p.is_symlink() for p in [candidate, *candidate.parents]):
            return error("unsafe_install_path")
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"content", "base64", "executable"}:
                return error("invalid_file")
            if ("content" in body) == ("base64" in body):
                return error("provide_content_or_base64")
            data = body["content"].encode("utf-8") if "content" in body else base64.b64decode(body["base64"], validate=True)
            if len(data) > 512 * 1024:
                return error("file_too_large", 413)
            candidate.parent.mkdir(parents=True, exist_ok=True)
            tmp = candidate.with_name("." + candidate.name + "." + secrets.token_hex(8))
            try:
                fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o700 if body.get("executable") else 0o600)
                with os.fdopen(fd, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(tmp, candidate)
            finally:
                tmp.unlink(missing_ok=True)
        except (ValueError, TypeError, AttributeError, binascii.Error, OSError):
            return error("invalid_file")
        return web.json_response({"ok": True, "kind": kind, "path": relative, "bytes": len(data)})

    app.add_routes([
        web.get("/api/managed/v1/status", status),
        web.get("/api/gateway/v1/probe", status, allow_head=False),
        web.get("/api/managed/v1/config", get_config),
        web.patch("/api/managed/v1/config", patch_config),
        web.post("/api/managed/v1/jobs", create_job),
        web.get("/api/managed/v1/jobs/{id}", get_job),
        web.post("/api/managed/v1/jobs/{id}/cancel", cancel_job),
        web.put("/api/managed/v1/files/{kind:skills|mcp}/{path:.+}", install_file),
    ])
