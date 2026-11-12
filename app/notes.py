"""User-only sticky notes (session notes & global multi-sheet sticky notes).

Notes belong strictly to the human user in the Web console: they are never
injected into model system prompts, conversation context windows, or tool schemas.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

MAX_SHEET_TITLE_CHARS = 60
MAX_QUOTE_CHARS = 8_000
MAX_NOTE_CHARS = 16_000
MAX_SHEETS_PER_USER = 50
DEFAULT_SHEET_TITLE = "默认便签"


def now_ms() -> int:
    return int(time.time() * 1000)


class NoteError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class NoteNotFound(NoteError):
    def __init__(self, message: str = "未找到对应的便签记录") -> None:
        super().__init__("note_not_found", message, status=404)


def _clean_text(value: Any, *, max_chars: int, label: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > max_chars:
        raise NoteError("text_too_long", f"{label}长度不能超过 {max_chars} 个字符")
    return text


class UserNotesDAO:
    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _serialize_note(row: Any, *, live_titles: dict[str, str] | None = None) -> dict[str, Any]:
        conv_uuid = str(row["conversation_uuid"] or "")
        stored_title = str(row["conversation_title"] or "")
        live_title = live_titles.get(conv_uuid) if live_titles is not None and conv_uuid else None
        return {
            "noteUuid": str(row["note_uuid"]),
            "scope": str(row["scope"]),
            "conversationUuid": conv_uuid,
            "conversationTitle": (live_title if live_title is not None else stored_title) or "未命名会话",
            "conversationExists": bool(live_title is not None) if (live_titles is not None and conv_uuid) else bool(conv_uuid),
            "sheetUuid": str(row["sheet_uuid"] or ""),
            "turnUuid": str(row["turn_uuid"] or ""),
            "turnNumber": int(row["turn_number"] or 0),
            "opId": str(row["op_id"] or ""),
            "quote": str(row["quote"] or ""),
            "content": str(row["content"] or ""),
            "createdAtMs": int(row["created_at_ms"] or 0),
            "updatedAtMs": int(row["updated_at_ms"] or 0),
        }

    @staticmethod
    def _serialize_sheet(row: Any, notes: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "sheetUuid": str(row["sheet_uuid"]),
            "title": str(row["title"]),
            "sortOrder": int(row["sort_order"] or 0),
            "createdAtMs": int(row["created_at_ms"] or 0),
            "updatedAtMs": int(row["updated_at_ms"] or 0),
            "notes": notes,
        }

    async def _ensure_default_sheet_in_tx(self, conn: Any, owner_chat_id: int) -> list[Any]:
        cur = await conn.execute(
            "SELECT * FROM user_note_sheets WHERE owner_chat_id=? ORDER BY sort_order ASC, id ASC",
            (owner_chat_id,),
        )
        rows = await cur.fetchall()
        if rows:
            return list(rows)
        ts = now_ms()
        sheet_uuid = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO user_note_sheets (sheet_uuid, owner_chat_id, title, sort_order, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (sheet_uuid, owner_chat_id, DEFAULT_SHEET_TITLE, ts, ts),
        )
        cur = await conn.execute(
            "SELECT * FROM user_note_sheets WHERE owner_chat_id=? ORDER BY sort_order ASC, id ASC",
            (owner_chat_id,),
        )
        return list(await cur.fetchall())

    async def _live_conversation_titles(self, conn: Any, owner_chat_id: int, conv_uuids: set[str]) -> dict[str, str]:
        uuids = [u for u in conv_uuids if u]
        if not uuids:
            return {}
        placeholders = ",".join("?" for _ in uuids)
        cur = await conn.execute(
            f"SELECT conversation_uuid, title FROM web_conversations WHERE owner_chat_id=? AND conversation_uuid IN ({placeholders})",  # noqa: S608
            (owner_chat_id, *uuids),
        )
        rows = await cur.fetchall()
        return {str(r["conversation_uuid"]): str(r["title"] or "未命名会话") for r in rows}

    async def _conversation_title(self, conn: Any, owner_chat_id: int, conversation_uuid: str, *, require: bool) -> str:
        if not conversation_uuid:
            if require:
                raise NoteError("conversation_required", "当前会话尚未创建，无法保存本会话便签")
            return ""
        cur = await conn.execute(
            "SELECT title FROM web_conversations WHERE owner_chat_id=? AND conversation_uuid=? LIMIT 1",
            (owner_chat_id, conversation_uuid),
        )
        row = await cur.fetchone()
        if row is None:
            if require:
                raise NoteNotFound("目标会话不存在或无权访问")
            return ""
        return str(row["title"] or "未命名会话")

    async def bootstrap(self, owner_chat_id: int, conversation_uuid: str = "") -> dict[str, Any]:
        conv_uuid = str(conversation_uuid or "").strip()
        if conv_uuid.startswith("local:"):
            conv_uuid = ""
        cur = await self.db.conn.execute(
            "SELECT * FROM user_note_sheets WHERE owner_chat_id=? ORDER BY sort_order ASC, id ASC",
            (owner_chat_id,),
        )
        sheet_rows = list(await cur.fetchall())
        if not sheet_rows:
            async with self.db.write_transaction(label="notes_ensure_default_sheet") as wconn:
                sheet_rows = await self._ensure_default_sheet_in_tx(wconn, owner_chat_id)

        session_rows: list[Any] = []
        if conv_uuid:
            cur = await self.db.conn.execute(
                """
                SELECT * FROM user_notes
                WHERE owner_chat_id=? AND scope='session' AND conversation_uuid=?
                ORDER BY id DESC
                """,
                (owner_chat_id, conv_uuid),
            )
            session_rows = list(await cur.fetchall())

        cur = await self.db.conn.execute(
            """
            SELECT * FROM user_notes
            WHERE owner_chat_id=? AND scope='global'
            ORDER BY id DESC
            """,
            (owner_chat_id,),
        )
        global_rows = list(await cur.fetchall())

        referenced_convs = {str(r["conversation_uuid"] or "") for r in [*session_rows, *global_rows] if r["conversation_uuid"]}
        live_titles = await self._live_conversation_titles(self.db.conn, owner_chat_id, referenced_convs)

        notes_by_sheet: dict[str, list[dict[str, Any]]] = {str(s["sheet_uuid"]): [] for s in sheet_rows}
        fallback_sheet_uuid = str(sheet_rows[0]["sheet_uuid"]) if sheet_rows else ""
        for row in global_rows:
            item = self._serialize_note(row, live_titles=live_titles)
            target = item["sheetUuid"] if item["sheetUuid"] in notes_by_sheet else fallback_sheet_uuid
            if target:
                notes_by_sheet[target].append(item)

        return {
            "sessionNotes": [self._serialize_note(r, live_titles=live_titles) for r in session_rows],
            "globalSheets": [
                self._serialize_sheet(s, notes_by_sheet.get(str(s["sheet_uuid"]), []))
                for s in sheet_rows
            ],
        }

    async def create_note(
        self,
        owner_chat_id: int,
        *,
        scope: str,
        conversation_uuid: str = "",
        sheet_uuid: str = "",
        turn_uuid: str = "",
        turn_number: int = 0,
        op_id: str = "",
        quote: str = "",
        content: str = "",
    ) -> dict[str, Any]:
        scope_val = str(scope or "").strip().lower()
        if scope_val not in {"session", "global"}:
            raise NoteError("invalid_scope", "便签模式必须是 session 或 global")
        quote_val = _clean_text(quote, max_chars=MAX_QUOTE_CHARS, label="摘录原文")
        content_val = _clean_text(content, max_chars=MAX_NOTE_CHARS, label="便签内容")
        if not quote_val and not content_val:
            raise NoteError("empty_note", "摘录内容或便签正文不能同时为空")

        conv_uuid = str(conversation_uuid or "").strip()
        if conv_uuid.startswith("local:"):
            conv_uuid = ""
        turn_uuid_val = str(turn_uuid or "").strip()[:120]
        op_id_val = str(op_id or "").strip()[:160]
        try:
            turn_num_val = max(0, int(turn_number or 0))
        except (TypeError, ValueError):
            turn_num_val = 0

        async with self.db.write_transaction(label="notes_create") as conn:
            conv_title = await self._conversation_title(
                conn,
                owner_chat_id,
                conv_uuid,
                require=(scope_val == "session"),
            )
            resolved_sheet_uuid = ""
            if scope_val == "global":
                sheets = await self._ensure_default_sheet_in_tx(conn, owner_chat_id)
                wanted = str(sheet_uuid or "").strip()
                if wanted and any(str(s["sheet_uuid"]) == wanted for s in sheets):
                    resolved_sheet_uuid = wanted
                else:
                    resolved_sheet_uuid = str(sheets[0]["sheet_uuid"])

            ts = now_ms()
            note_uuid = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO user_notes (
                    note_uuid, owner_chat_id, scope, conversation_uuid, conversation_title,
                    sheet_uuid, turn_uuid, turn_number, op_id, quote, content, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note_uuid,
                    owner_chat_id,
                    scope_val,
                    conv_uuid,
                    conv_title,
                    resolved_sheet_uuid,
                    turn_uuid_val,
                    turn_num_val,
                    op_id_val,
                    quote_val,
                    content_val,
                    ts,
                    ts,
                ),
            )
            if resolved_sheet_uuid:
                await conn.execute(
                    "UPDATE user_note_sheets SET updated_at_ms=? WHERE owner_chat_id=? AND sheet_uuid=?",
                    (ts, owner_chat_id, resolved_sheet_uuid),
                )
            cur = await conn.execute("SELECT * FROM user_notes WHERE note_uuid=?", (note_uuid,))
            row = await cur.fetchone()
            live_titles = {conv_uuid: conv_title} if conv_uuid and conv_title else {}
            return self._serialize_note(row, live_titles=live_titles)

    async def update_note(
        self,
        owner_chat_id: int,
        note_uuid: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        target_uuid = str(note_uuid or "").strip()
        if not target_uuid:
            raise NoteNotFound()
        async with self.db.write_transaction(label="notes_update") as conn:
            cur = await conn.execute(
                "SELECT * FROM user_notes WHERE owner_chat_id=? AND note_uuid=? LIMIT 1",
                (owner_chat_id, target_uuid),
            )
            row = await cur.fetchone()
            if row is None:
                raise NoteNotFound()

            quote_val = (
                _clean_text(changes["quote"], max_chars=MAX_QUOTE_CHARS, label="摘录原文")
                if "quote" in changes
                else str(row["quote"] or "")
            )
            content_val = (
                _clean_text(changes["content"], max_chars=MAX_NOTE_CHARS, label="便签内容")
                if "content" in changes
                else str(row["content"] or "")
            )
            if not quote_val and not content_val:
                raise NoteError("empty_note", "摘录内容或便签正文不能同时为空")

            scope_val = str(row["scope"])
            sheet_val = str(row["sheet_uuid"] or "")
            if "sheetUuid" in changes and scope_val == "global":
                sheets = await self._ensure_default_sheet_in_tx(conn, owner_chat_id)
                wanted = str(changes["sheetUuid"] or "").strip()
                if not any(str(s["sheet_uuid"]) == wanted for s in sheets):
                    raise NoteNotFound("目标全局便签不存在")
                sheet_val = wanted

            ts = now_ms()
            await conn.execute(
                """
                UPDATE user_notes
                SET quote=?, content=?, sheet_uuid=?, updated_at_ms=?
                WHERE owner_chat_id=? AND note_uuid=?
                """,
                (quote_val, content_val, sheet_val, ts, owner_chat_id, target_uuid),
            )
            cur = await conn.execute("SELECT * FROM user_notes WHERE note_uuid=?", (target_uuid,))
            updated = await cur.fetchone()
            conv_uuid = str(updated["conversation_uuid"] or "")
            live_titles = await self._live_conversation_titles(conn, owner_chat_id, {conv_uuid} if conv_uuid else set())
            return self._serialize_note(updated, live_titles=live_titles)

    async def copy_to_global(
        self,
        owner_chat_id: int,
        note_uuid: str,
        *,
        sheet_uuid: str = "",
    ) -> dict[str, Any]:
        target_uuid = str(note_uuid or "").strip()
        if not target_uuid:
            raise NoteNotFound()
        cur = await self.db.conn.execute(
            "SELECT * FROM user_notes WHERE owner_chat_id=? AND note_uuid=? LIMIT 1",
            (owner_chat_id, target_uuid),
        )
        row = await cur.fetchone()
        if row is None:
            raise NoteNotFound()
        return await self.create_note(
            owner_chat_id,
            scope="global",
            conversation_uuid=str(row["conversation_uuid"] or ""),
            sheet_uuid=sheet_uuid,
            turn_uuid=str(row["turn_uuid"] or ""),
            turn_number=int(row["turn_number"] or 0),
            op_id=str(row["op_id"] or ""),
            quote=str(row["quote"] or ""),
            content=str(row["content"] or ""),
        )

    async def delete_note(self, owner_chat_id: int, note_uuid: str) -> bool:
        target_uuid = str(note_uuid or "").strip()
        if not target_uuid:
            raise NoteNotFound()
        async with self.db.write_transaction(label="notes_delete") as conn:
            cur = await conn.execute(
                "DELETE FROM user_notes WHERE owner_chat_id=? AND note_uuid=?",
                (owner_chat_id, target_uuid),
            )
            if (cur.rowcount or 0) <= 0:
                raise NoteNotFound()
            return True

    async def create_sheet(self, owner_chat_id: int, title: str) -> dict[str, Any]:
        clean_title = _clean_text(title, max_chars=MAX_SHEET_TITLE_CHARS, label="便签标题")
        if not clean_title:
            raise NoteError("title_required", "请输入便签名称")
        async with self.db.write_transaction(label="notes_sheet_create") as conn:
            sheets = await self._ensure_default_sheet_in_tx(conn, owner_chat_id)
            if len(sheets) >= MAX_SHEETS_PER_USER:
                raise NoteError("too_many_sheets", f"全局便签最多创建 {MAX_SHEETS_PER_USER} 张")
            max_order = max((int(s["sort_order"] or 0) for s in sheets), default=-1)
            ts = now_ms()
            sheet_uuid = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO user_note_sheets (sheet_uuid, owner_chat_id, title, sort_order, created_at_ms, updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sheet_uuid, owner_chat_id, clean_title, max_order + 1, ts, ts),
            )
            cur = await conn.execute("SELECT * FROM user_note_sheets WHERE sheet_uuid=?", (sheet_uuid,))
            row = await cur.fetchone()
            return self._serialize_sheet(row, [])

    async def update_sheet(self, owner_chat_id: int, sheet_uuid: str, title: str) -> dict[str, Any]:
        target_uuid = str(sheet_uuid or "").strip()
        clean_title = _clean_text(title, max_chars=MAX_SHEET_TITLE_CHARS, label="便签标题")
        if not clean_title:
            raise NoteError("title_required", "便签名称不能为空")
        async with self.db.write_transaction(label="notes_sheet_update") as conn:
            ts = now_ms()
            cur = await conn.execute(
                "UPDATE user_note_sheets SET title=?, updated_at_ms=? WHERE owner_chat_id=? AND sheet_uuid=?",
                (clean_title, ts, owner_chat_id, target_uuid),
            )
            if (cur.rowcount or 0) <= 0:
                raise NoteNotFound("未找到该全局便签")
            cur = await conn.execute("SELECT * FROM user_note_sheets WHERE sheet_uuid=?", (target_uuid,))
            row = await cur.fetchone()
            return self._serialize_sheet(row, [])

    async def delete_sheet(self, owner_chat_id: int, sheet_uuid: str) -> dict[str, Any]:
        target_uuid = str(sheet_uuid or "").strip()
        if not target_uuid:
            raise NoteNotFound("未找到该全局便签")
        async with self.db.write_transaction(label="notes_sheet_delete") as conn:
            cur = await conn.execute(
                "SELECT 1 FROM user_note_sheets WHERE owner_chat_id=? AND sheet_uuid=? LIMIT 1",
                (owner_chat_id, target_uuid),
            )
            if await cur.fetchone() is None:
                raise NoteNotFound("未找到该全局便签")
            await conn.execute(
                "DELETE FROM user_notes WHERE owner_chat_id=? AND scope='global' AND sheet_uuid=?",
                (owner_chat_id, target_uuid),
            )
            await conn.execute(
                "DELETE FROM user_note_sheets WHERE owner_chat_id=? AND sheet_uuid=?",
                (owner_chat_id, target_uuid),
            )
            remaining = await self._ensure_default_sheet_in_tx(conn, owner_chat_id)
            return {"deletedSheetUuid": target_uuid, "activeSheetUuid": str(remaining[0]["sheet_uuid"])}

    async def hard_delete_conversation(self, conversation_uuid: str, *, conn: Any = None) -> int:
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return 0
        if conn is not None:
            cur = await conn.execute(
                "DELETE FROM user_notes WHERE scope='session' AND conversation_uuid=?",
                (conv_uuid,),
            )
            return int(cur.rowcount or 0)
        async with self.db.write_transaction(label="notes_delete_conversation") as wconn:
            cur = await wconn.execute(
                "DELETE FROM user_notes WHERE scope='session' AND conversation_uuid=?",
                (conv_uuid,),
            )
            return int(cur.rowcount or 0)
