# ruff: noqa: F401,F403,F405
"""User-only sticky notes HTTP API."""
from __future__ import annotations

from app.notes import NoteError, UserNotesDAO
from app.web_console.core import *


class WebAdminNotesMixin:
    def _user_notes(self) -> UserNotesDAO:
        return UserNotesDAO(self.db)

    @staticmethod
    def _note_error_response(exc: NoteError) -> web.Response:
        return web.json_response(
            {"ok": False, "error": exc.code, "message": exc.message},
            status=exc.status,
        )

    async def handle_api_notes(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        conversation_uuid = str(request.query.get("conversationUuid") or "").strip()
        data = await self._user_notes().bootstrap(session.chat_id, conversation_uuid)
        return web.json_response({"ok": True, **data})

    async def handle_api_note_create(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        try:
            note = await self._user_notes().create_note(
                session.chat_id,
                scope=str(body.get("scope") or "session"),
                conversation_uuid=str(body.get("conversationUuid") or ""),
                sheet_uuid=str(body.get("sheetUuid") or ""),
                turn_uuid=str(body.get("turnUuid") or ""),
                turn_number=body.get("turnNumber") or 0,
                op_id=str(body.get("opId") or ""),
                quote=body.get("quote") or "",
                content=body.get("content") or "",
            )
        except NoteError as exc:
            return self._note_error_response(exc)
        return web.json_response({"ok": True, "note": note})

    async def handle_api_note_update(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        changes = {k: body[k] for k in ("quote", "content", "sheetUuid") if k in body}
        try:
            note = await self._user_notes().update_note(
                session.chat_id,
                request.match_info["note_uuid"],
                changes,
            )
        except NoteError as exc:
            return self._note_error_response(exc)
        return web.json_response({"ok": True, "note": note})

    async def handle_api_note_copy_global(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        try:
            note = await self._user_notes().copy_to_global(
                session.chat_id,
                request.match_info["note_uuid"],
                sheet_uuid=str(body.get("sheetUuid") or ""),
            )
        except NoteError as exc:
            return self._note_error_response(exc)
        return web.json_response({"ok": True, "note": note})

    async def handle_api_note_delete(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        try:
            await self._user_notes().delete_note(session.chat_id, request.match_info["note_uuid"])
        except NoteError as exc:
            return self._note_error_response(exc)
        return web.json_response({"ok": True})

    async def handle_api_note_sheet_create(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        try:
            sheet = await self._user_notes().create_sheet(session.chat_id, body.get("title") or "")
        except NoteError as exc:
            return self._note_error_response(exc)
        return web.json_response({"ok": True, "sheet": sheet})

    async def handle_api_note_sheet_update(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        try:
            sheet = await self._user_notes().update_sheet(
                session.chat_id,
                request.match_info["sheet_uuid"],
                body.get("title") or "",
            )
        except NoteError as exc:
            return self._note_error_response(exc)
        return web.json_response({"ok": True, "sheet": sheet})

    async def handle_api_note_sheet_delete(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        try:
            result = await self._user_notes().delete_sheet(session.chat_id, request.match_info["sheet_uuid"])
        except NoteError as exc:
            return self._note_error_response(exc)
        return web.json_response({"ok": True, **result})
