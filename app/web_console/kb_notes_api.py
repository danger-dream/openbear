"""HTTP API for hierarchical Markdown knowledge-base notes, assets, and Markdown import."""
from __future__ import annotations

import io
import zipfile
from urllib.parse import quote

from app.kb_notes import KBNoteError, KBNotesDAO, import_from_markdown_or_zip
from app.kb_notes.dao import MAX_ASSET_BYTES
from app.kb_notes.markdown_import import MAX_ZIP_TOTAL_BYTES as _MAX_IMPORT_BYTES
from app.web_console.core import *

# A full note body (up to MAX_NOTE_CONTENT_CHARS) is far above aiohttp's 1 MiB
# default; an oversized or unreadable body must fail, never become an empty edit.
_JSON_BODY_LIMIT = 16 * 1024 * 1024
_EXPORT_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _export_name(title: Any, used: set[str]) -> str:
    name = _EXPORT_NAME_RE.sub("_", str(title or "")).strip().strip(".").strip() or "note"
    candidate, index = name, 2
    while candidate.lower() in used:
        candidate = f"{name} ({index})"
        index += 1
    used.add(candidate.lower())
    return candidate


def _format_capture_markdown(
    *,
    quote_text: str = "",
    note_text: str = "",
    conversation_uuid: str = "",
    conversation_title: str = "",
    turn_uuid: str = "",
    op_id: str = "",
    turn_number: int = 0,
) -> str:
    blocks: list[str] = []
    conv_id = str(conversation_uuid or "").strip()
    if conv_id and not conv_id.startswith("local:"):
        clean_label = (str(conversation_title or "").strip() or "原会话").replace("[", "\\[").replace("]", "\\]")
        t_uuid = str(turn_uuid or "").strip()
        op_key = str(op_id or "").strip()
        turn_tag = f" · 第 {int(turn_number)} 轮" if int(turn_number or 0) > 0 else ""
        if op_key:
            blocks.append(
                f"来源：[{clean_label}{turn_tag or ' · 原消息'}](openbear://ref/message/{quote(conv_id, safe='')}/{quote(op_key, safe='')}?mode=mention)"
            )
        elif t_uuid:
            blocks.append(
                f"来源：[{clean_label}{turn_tag or ' · 原轮次'}](openbear://ref/turn/{quote(conv_id, safe='')}/{quote(t_uuid, safe='')}?mode=mention)"
            )
        else:
            blocks.append(f"来源：[{clean_label}](openbear://ref/chat/{quote(conv_id, safe='')}?mode=mention)")

    q = str(quote_text or "").replace("\r\n", "\n").strip()
    if q:
        quoted_lines = "\n".join(f"> {line}" if line.strip() else ">" for line in q.split("\n"))
        blocks.append(quoted_lines)

    c = str(note_text or "").replace("\r\n", "\n").strip()
    if c:
        blocks.append(c)
    return "\n\n".join(blocks).strip()


class WebAdminKBNotesMixin:
    def _kb_notes(self) -> KBNotesDAO:
        return KBNotesDAO(self.db)

    @staticmethod
    def _kb_note_error_response(exc: KBNoteError) -> web.Response:
        return web.json_response(
            {"ok": False, "error": exc.code, "message": exc.message, **exc.extra},
            status=exc.status,
        )

    @staticmethod
    async def _kb_note_body(request: web.Request) -> dict[str, Any]:
        try:
            data = await request.clone(client_max_size=_JSON_BODY_LIMIT).json()
        except web.HTTPRequestEntityTooLarge:
            raise KBNoteError("request_too_large", "提交的内容过大，未保存", status=413) from None
        except (ValueError, UnicodeError):
            raise KBNoteError("invalid_json", "请求内容不是有效的 JSON，未保存") from None
        if not isinstance(data, dict):
            raise KBNoteError("invalid_json", "请求内容格式不正确，未保存")
        return data

    async def handle_api_kb_notes_tree(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        data = await self._kb_notes().list_tree(session.chat_id)
        return web.json_response({"ok": True, **data})

    async def handle_api_kb_notes_search(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        q = str(request.query.get("q") or "").strip()
        try:
            limit = int(request.query.get("limit") or 40)
        except ValueError:
            limit = 40
        items = await self._kb_notes().search_notes(session.chat_id, q, limit=limit)
        return web.json_response({"ok": True, "items": items})

    async def handle_api_kb_note_detail(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        note_uuid = request.match_info["note_uuid"]
        try:
            note = await self._kb_notes().get_note(session.chat_id, note_uuid)
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        return web.json_response({"ok": True, "note": note})

    async def handle_api_kb_note_create(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        try:
            body = await self._kb_note_body(request)
            note = await self._kb_notes().create_note(
                session.chat_id,
                parent_uuid=str(body.get("parentUuid") or ""),
                title=str(body.get("title") or "未命名笔记"),
                icon=str(body.get("icon") or ""),
                content=str(body.get("content") or ""),
                is_readonly=bool(body.get("isReadonly")),
                source_kind=str(body.get("sourceKind") or ""),
                source_ref=str(body.get("sourceRef") or ""),
                after_uuid=str(body.get("afterUuid") or ""),
            )
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        return web.json_response({"ok": True, "note": note})

    async def handle_api_kb_note_update(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        note_uuid = request.match_info["note_uuid"]
        try:
            body = await self._kb_note_body(request)
            allowed = ("title", "icon", "content", "pinned", "isReadonly")
            changes = {k: body[k] for k in allowed if k in body}
            expected_rev = body.get("expectedRevision")
            if expected_rev is not None:
                try:
                    expected_rev = int(expected_rev)
                except (TypeError, ValueError):
                    raise KBNoteError("invalid_revision", "版本号格式不正确，未保存") from None
            note = await self._kb_notes().update_note(
                session.chat_id,
                note_uuid,
                changes,
                expected_revision=expected_rev,
            )
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        return web.json_response({"ok": True, "note": note})

    async def handle_api_kb_note_append(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        note_uuid = request.match_info["note_uuid"]
        try:
            body = await self._kb_note_body(request)
            note = await self._kb_notes().append_to_note(
                session.chat_id,
                note_uuid,
                str(body.get("content") or ""),
            )
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        return web.json_response({"ok": True, "note": note})

    async def handle_api_kb_note_capture(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        try:
            body = await self._kb_note_body(request)
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        mode = str(body.get("mode") or "create").strip()
        conv_uuid = str(body.get("conversationUuid") or "").strip()
        conv_title = str(body.get("conversationTitle") or "").strip()
        if conv_uuid and not conv_title:
            row = await self._conversation_row(session.chat_id, conv_uuid)
            if row:
                conv_title = str(row["title"] or "")

        md_block = _format_capture_markdown(
            quote_text=str(body.get("quote") or ""),
            note_text=str(body.get("content") or ""),
            conversation_uuid=conv_uuid,
            conversation_title=conv_title,
            turn_uuid=str(body.get("turnUuid") or ""),
            op_id=str(body.get("opId") or ""),
            turn_number=_int_or_zero(body.get("turnNumber")),
        )
        if not md_block:
            return web.json_response({"ok": False, "error": "empty_capture", "message": "要转存的内容不能为空"}, status=400)

        dao = self._kb_notes()
        try:
            if mode == "append":
                target_uuid = str(body.get("targetNoteUuid") or "").strip()
                if not target_uuid:
                    return web.json_response({"ok": False, "error": "target_required", "message": "请选择要追加的目标笔记"}, status=400)
                note = await dao.append_to_note(session.chat_id, target_uuid, f"---\n\n{md_block}")
            else:
                raw_title = str(body.get("title") or "").strip()
                if not raw_title:
                    seed = str(body.get("content") or body.get("quote") or conv_title or "会话摘录").strip().splitlines()[0]
                    raw_title = seed[:48] or "会话摘录"
                note = await dao.create_note(
                    session.chat_id,
                    parent_uuid=str(body.get("parentUuid") or ""),
                    title=raw_title,
                    icon=str(body.get("icon") or "📝"),
                    content=md_block,
                    source_kind="session_capture",
                    source_ref=conv_uuid,
                )
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        return web.json_response({"ok": True, "note": note})

    async def handle_api_kb_note_move(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        note_uuid = request.match_info["note_uuid"]
        try:
            body = await self._kb_note_body(request)
            tree = await self._kb_notes().move_note(
                session.chat_id,
                note_uuid,
                target_parent_uuid=str(body.get("targetParentUuid") or ""),
                before_uuid=str(body.get("beforeUuid") or ""),
                after_uuid=str(body.get("afterUuid") or ""),
            )
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        return web.json_response({"ok": True, **tree})

    async def handle_api_kb_note_delete(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        note_uuid = request.match_info["note_uuid"]
        mode = str(request.query.get("mode") or "cascade").strip()
        try:
            result = await self._kb_notes().delete_note(session.chat_id, note_uuid, mode=mode)
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        return web.json_response({"ok": True, **result})

    async def handle_api_kb_note_export(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        note_uuid = request.match_info["note_uuid"]
        include_children = str(request.query.get("children") or "").lower() in {"1", "true", "yes"}
        dao = self._kb_notes()
        try:
            root = await dao.get_note(session.chat_id, note_uuid)
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)

        def markdown_file(title: str, content: str) -> bytes:
            return (f"# {title}\n\n{content}" if content else f"# {title}\n").encode("utf-8")

        safe_title = _export_name(root["title"], set())
        if not include_children or int(root.get("childCount") or 0) == 0:
            encoded = quote(f"{safe_title}.md")
            return web.Response(
                body=markdown_file(root["title"], root["content"]),
                content_type="text/markdown",
                charset="utf-8",
                headers={"Content-Disposition": f"attachment; filename=\"note.md\"; filename*=UTF-8''{encoded}"},
            )

        items = (await dao.list_tree(session.chat_id))["items"]
        contents = await dao.note_contents(session.chat_id)
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            children_by_parent.setdefault(str(item["parentUuid"] or ""), []).append(item)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Sibling titles may repeat or sanitize to the same name; each level
            # gets unique names so no entry overwrites another when extracted.
            stack = [(root["noteUuid"], root["title"], "", set())]
            while stack:
                note_uuid, title, prefix, used = stack.pop()
                rel_base = prefix + _export_name(title, used)
                zf.writestr(f"{rel_base}.md", markdown_file(title, contents.get(note_uuid, "")))
                child_names: set[str] = set()
                for child in reversed(children_by_parent.get(note_uuid, [])):
                    stack.append((child["noteUuid"], child["title"], f"{rel_base}/", child_names))

        encoded = quote(f"{safe_title}.zip")
        return web.Response(
            body=buf.getvalue(),
            content_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=\"notes.zip\"; filename*=UTF-8''{encoded}"},
        )

    async def handle_api_kb_note_asset_upload(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        if not (request.content_type or "").lower().startswith("multipart/"):
            return web.json_response({"ok": False, "error": "multipart_required", "message": "请使用 multipart/form-data 上传图片"}, status=400)
        reader = await request.multipart()
        file_bytes = b""
        filename = "image.png"
        mime_type = ""
        note_uuid = ""
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "noteUuid":
                note_uuid = (await part.text()).strip()
            elif part.name == "file":
                filename = part.filename or "image.png"
                mime_type = part.headers.get("Content-Type", "")
                chunks = bytearray()
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > MAX_ASSET_BYTES:
                        return web.json_response(
                            {"ok": False, "error": "asset_too_large", "message": f"单张图片不能超过 {MAX_ASSET_BYTES // (1024 * 1024)} MiB"},
                            status=413,
                        )
                file_bytes = bytes(chunks)

        try:
            stored = await self._kb_notes().store_asset(
                session.chat_id,
                data=file_bytes,
                filename=filename,
                mime_type=mime_type,
                note_uuid=note_uuid,
            )
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        return web.json_response({"ok": True, "asset": stored})

    async def handle_api_kb_note_asset_get(self, request: web.Request) -> web.StreamResponse:
        session: WebSession = request[_WEB_SESSION_KEY]
        asset_uuid = request.match_info["asset_uuid"]
        try:
            asset = await self._kb_notes().get_asset(session.chat_id, asset_uuid)
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)

        headers = {
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        }
        if asset["mimeType"] == "image/svg+xml":
            headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return web.FileResponse(path=asset["path"], headers={**headers, "Content-Type": asset["mimeType"]})

    async def handle_api_kb_notes_files_import(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        if not (request.content_type or "").lower().startswith("multipart/"):
            return web.json_response({"ok": False, "error": "multipart_required", "message": "请使用 multipart/form-data 上传导入文件"}, status=400)
        reader = await request.multipart()
        uploaded: list[tuple[str, bytes]] = []
        target_parent_uuid = ""
        total = 0
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "targetParentUuid":
                target_parent_uuid = (await part.text()).strip()
            elif part.name in {"file", "files"}:
                fname = part.filename or "note.md"
                chunks = bytearray()
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    total += len(chunk)
                    if total > _MAX_IMPORT_BYTES:
                        return web.json_response(
                            {"ok": False, "error": "import_archive_too_large", "message": "上传文件总大小超过 120 MiB"},
                            status=413,
                        )
                if chunks:
                    uploaded.append((fname, bytes(chunks)))
        try:
            result = await import_from_markdown_or_zip(
                self._kb_notes(),
                session.chat_id,
                files=uploaded,
                target_parent_uuid=target_parent_uuid,
            )
        except KBNoteError as exc:
            return self._kb_note_error_response(exc)
        return web.json_response({"ok": True, **result})
