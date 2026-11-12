"""Extension switches of a conversation and the defaults a folder gives new conversations."""
from __future__ import annotations

from aiohttp import web

from app.extensions import TEMPORARY, ExtensionError
from app.web_console.core import _WEB_SESSION_KEY


class WebAdminExtensionsMixin:
    def _extension_error(self, exc: ExtensionError) -> web.Response:
        status = 409 if exc.payload["error"] == "extension_in_use" else 404 if exc.payload["error"] == "conversation_not_found" else 400
        return web.json_response(exc.payload, status=status)

    async def handle_api_conversation_extensions(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        return web.json_response(await self.extensions.describe(row["conversation_uuid"]))

    async def handle_api_conversation_extensions_update(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        enable, disable, auto_allow = body.get("enable", []), body.get("disable", []), body.get("autoAllow")
        if (not isinstance(enable, list) or not isinstance(disable, list) or any(not isinstance(k, str) for k in (*enable, *disable))
                or (auto_allow is not None and not isinstance(auto_allow, bool))):
            return web.json_response({"ok": False, "error": "invalid_extensions"}, status=400)
        try:
            await self.extensions.update(row["conversation_uuid"], enable=enable, disable=disable, auto_allow=auto_allow)
        except ExtensionError as exc:
            return self._extension_error(exc)
        return web.json_response(await self.extensions.describe(row["conversation_uuid"]))

    async def _extension_folder(self, request: web.Request) -> tuple[int, str] | web.Response:
        owner = int(request[_WEB_SESSION_KEY].chat_id)
        folder = str(request.match_info.get("folder_uuid") or "").strip()
        if folder != TEMPORARY and folder not in await self._tree_context_folders(owner):
            return web.json_response({"ok": False, "error": "folder_not_found"}, status=404)
        return owner, folder

    async def _folder_extensions_payload(self, owner: int, folder: str) -> dict:
        default = await self.extensions.folder_default(owner, folder)
        inherited = await self.extensions.folder_default(owner, folder, inherited=True)
        cur = await self.db.conn.execute(
            "SELECT parent_uuid FROM web_conversation_folders WHERE folder_uuid=? AND owner_chat_id=?", (folder, owner))
        parent = await cur.fetchone()
        # A top-level folder or the temporary node has no folder above it to inherit from.
        return {"ok": True, "default": {**default, "own": default["from"] == folder}, "inherited": inherited,
                "canInherit": bool(parent and parent["parent_uuid"]),
                "extensions": self.extensions.panel(set(default["enabled"]))}

    async def handle_api_folder_extensions(self, request: web.Request) -> web.Response:
        found = await self._extension_folder(request)
        if isinstance(found, web.Response):
            return found
        return web.json_response(await self._folder_extensions_payload(*found))

    async def handle_api_folder_extensions_update(self, request: web.Request) -> web.Response:
        found = await self._extension_folder(request)
        if isinstance(found, web.Response):
            return found
        owner, folder = found
        body = await self._json_body(request)
        if body.get("inherit") is True:
            value = None
        else:
            enabled, auto_allow = body.get("enabled"), body.get("autoAllow", False)
            if not isinstance(enabled, list) or any(not isinstance(k, str) for k in enabled) or not isinstance(auto_allow, bool):
                return web.json_response({"ok": False, "error": "invalid_extensions"}, status=400)
            value = {"enabled": enabled, "autoAllow": auto_allow}
        try:
            await self.extensions.set_folder_default(owner, folder, value)
        except ExtensionError as exc:
            return self._extension_error(exc)
        return web.json_response(await self._folder_extensions_payload(owner, folder))
