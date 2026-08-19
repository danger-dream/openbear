# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.web_console.core import *


class WebAdminUpdateMixin:
    async def handle_api_system_version(self, request: web.Request) -> web.Response:
        update = getattr(self, "update_service", None)
        running = await self._restart_running_json()
        if update is None:
            from app import installed_version

            return web.json_response({
                "ok": True,
                "version": installed_version(),
                "latest": None,
                "updateAvailable": False,
                "phase": "idle",
                "dirtyWorktree": False,
                "lastResult": None,
                "running": running,
            })
        return web.json_response(update.snapshot(running=running))

    async def handle_api_system_update(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        update = getattr(self, "update_service", None)
        if update is None:
            return web.json_response({"ok": False, "error": "update_unavailable"}, status=503)
        body = await self._json_body(request)
        confirm = bool(body.get("confirm"))
        force = bool(body.get("force"))
        allow_dirty = bool(body.get("allowDirty"))
        running = await self._restart_running_json()
        result = await update.start_update(
            confirm=confirm,
            force=force,
            allow_dirty=allow_dirty,
            running=running,
        )
        status = 200
        if not result.get("ok"):
            error = str(result.get("error") or "")
            status = {
                "confirm_required": 400,
                "system_busy": 409,
                "dirty_worktree": 409,
                "update_in_progress": 409,
                "already_latest": 400,
                "no_release": 404,
                "missing_zip_asset": 404,
            }.get(error, 500)
        else:
            await self.audit(
                "system.update",
                actor="web",
                chat_id=session.chat_id,
                ip=request.remote or "",
                detail={
                    "toVersion": result.get("toVersion"),
                    "force": force,
                    "allowDirty": allow_dirty,
                },
            )
        return web.json_response(result, status=status)

    async def handle_api_system_update_ack(self, request: web.Request) -> web.Response:
        update = getattr(self, "update_service", None)
        if update is None:
            return web.json_response({"ok": True, "acked": False})
        return web.json_response(update.ack_result())
