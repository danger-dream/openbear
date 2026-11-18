"""Hierarchical Markdown knowledge-base notes and embedded assets DAO."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

MAX_NOTE_TITLE_CHARS = 200
MAX_NOTE_ICON_CHARS = 16
MAX_NOTE_CONTENT_CHARS = 2_000_000
MAX_ASSET_BYTES = 25 * 1024 * 1024

_FENCE_BLOCK_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
_REF_OR_MD_LINK_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)|\[([^\]]+)\]\([^)]+\)")
_MD_STRIP_RE = re.compile(r"(?m)^(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+\.\s+)|[*_~`]+")
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]+")

ALLOWED_IMAGE_MIMES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/svg+xml": ".svg",
}


def now_ms() -> int:
    return int(time.time() * 1000)


class KBNoteError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}


class KBNoteNotFound(KBNoteError):
    def __init__(self, message: str = "未找到对应的笔记") -> None:
        super().__init__("kb_note_not_found", message, status=404)


class KBNoteConflict(KBNoteError):
    def __init__(self, current_note: dict[str, Any], message: str = "笔记已在其他位置更新，请先同步最新内容") -> None:
        super().__init__("kb_note_revision_conflict", message, status=409, extra={"currentNote": current_note})


class KBNoteReadonly(KBNoteError):
    def __init__(self, message: str = "当前笔记已开启只读锁定，请先解锁再编辑") -> None:
        super().__init__("kb_note_readonly", message, status=423)


def normalize_markdown(text: Any) -> str:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(raw) > MAX_NOTE_CONTENT_CHARS:
        raise KBNoteError("content_too_long", f"笔记正文不能超过 {MAX_NOTE_CONTENT_CHARS} 个字符")
    return raw


def clean_title(value: Any, *, default: str = "未命名笔记") -> str:
    title = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not title:
        title = default
    if len(title) > MAX_NOTE_TITLE_CHARS:
        title = title[:MAX_NOTE_TITLE_CHARS].rstrip()
    return title


def clean_icon(value: Any) -> str:
    icon = str(value or "").strip()
    if len(icon) > MAX_NOTE_ICON_CHARS or any(c in icon for c in "\r\n\t/<>\"'"):
        return ""
    return icon


def summarize_markdown(content: str, *, max_chars: int = 160) -> tuple[str, int]:
    if not content:
        return "", 0
    visible = _REF_OR_MD_LINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", content)
    char_count = len(re.sub(r"\s+", "", visible))
    without_code = _FENCE_BLOCK_RE.sub(" ", visible)
    plain = _MD_STRIP_RE.sub("", without_code)
    plain = re.sub(r"\|", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:max_chars], char_count


def sanitize_filename(name: str, fallback_ext: str = ".png") -> str:
    base = Path(str(name or "").strip()).name
    stem = Path(base).stem
    ext = Path(base).suffix.lower()
    clean_stem = _SAFE_FILENAME_RE.sub("_", stem).strip("._-")[:80] or "image"
    clean_ext = ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else fallback_ext
    return f"{clean_stem}{clean_ext}"


def sniff_image_mime(data: bytes, filename: str = "", declared_mime: str = "") -> str:
    head = data[:32]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[4:12] in {b"ftypavif", b"ftypavis"}:
        return "image/avif"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    mime = str(declared_mime or "").split(";", 1)[0].strip().lower()
    if not mime and filename:
        guessed, _ = mimetypes.guess_type(filename)
        mime = (guessed or "").lower()
    if mime == "image/svg+xml" or (filename.lower().endswith(".svg") and b"<svg" in data[:1024].lower()):
        return "image/svg+xml"
    if mime in ALLOWED_IMAGE_MIMES:
        return mime
    raise KBNoteError("unsupported_asset_type", "仅支持上传常见图片格式（PNG、JPG、GIF、WebP、AVIF、BMP、ICO、SVG）")


def default_asset_root() -> Path:
    configured = str(os.environ.get("OPENBEAR_KB_NOTE_ASSET_DIR") or "").strip()
    root = Path(configured).expanduser() if configured else Path.cwd() / "data" / "kb_note_assets"
    return root.resolve()


class KBNotesDAO:
    def __init__(self, db: Any, *, asset_root: Path | None = None) -> None:
        self.db = db
        self.asset_root = (asset_root or default_asset_root()).resolve()

    @staticmethod
    def _serialize_tree_row(
        row: Any,
        *,
        child_count: int = 0,
        path: str = "",
        depth: int = 0,
    ) -> dict[str, Any]:
        pinned_at = int(row["pinned_at"] or 0)
        return {
            "noteUuid": str(row["note_uuid"]),
            "parentUuid": str(row["parent_uuid"] or ""),
            "title": str(row["title"] or "未命名笔记"),
            "icon": str(row["icon"] or ""),
            "summary": str(row["summary"] or ""),
            "charCount": int(row["char_count"] or 0),
            "pinned": pinned_at > 0,
            "pinnedAt": pinned_at,
            "sortOrder": int(row["sort_order"] or 0),
            "isReadonly": bool(row["is_readonly"]),
            "revision": int(row["revision"] or 1),
            "sourceKind": str(row["source_kind"] or ""),
            "sourceRef": str(row["source_ref"] or ""),
            "childCount": child_count,
            "path": path,
            "depth": depth,
            "createdAtMs": int(row["created_at_ms"] or 0),
            "updatedAtMs": int(row["updated_at_ms"] or 0),
        }

    async def _fetch_light_rows(self, conn: Any, owner_chat_id: int) -> list[Any]:
        cur = await conn.execute(
            """
            SELECT id, note_uuid, parent_uuid, title, icon, summary, char_count,
                   pinned_at, sort_order, is_readonly, revision, source_kind, source_ref,
                   created_at_ms, updated_at_ms
            FROM kb_notes
            WHERE owner_chat_id=?
            ORDER BY CASE WHEN pinned_at > 0 THEN 0 ELSE 1 END ASC,
                     pinned_at DESC,
                     sort_order ASC,
                     id ASC
            """,
            (owner_chat_id,),
        )
        return list(await cur.fetchall())

    @classmethod
    def _build_tree_metadata(cls, rows: list[Any]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        by_uuid = {str(r["note_uuid"]): r for r in rows}
        children_map: dict[str, list[Any]] = {}
        for r in rows:
            parent = str(r["parent_uuid"] or "")
            if parent and parent not in by_uuid:
                parent = ""
            children_map.setdefault(parent, []).append(r)

        ordered_items: list[dict[str, Any]] = []
        ancestors_map: dict[str, list[dict[str, Any]]] = {}
        visited: set[str] = set()

        def visit(parent_uuid: str, trail: list[dict[str, Any]], depth: int) -> None:
            for r in children_map.get(parent_uuid, []):
                nuuid = str(r["note_uuid"])
                if nuuid in visited:
                    continue
                visited.add(nuuid)
                title = str(r["title"] or "未命名笔记")
                icon = str(r["icon"] or "")
                current_trail = [*trail, {"noteUuid": nuuid, "title": title, "icon": icon}]
                ancestors_map[nuuid] = trail
                path_str = " / ".join(item["title"] for item in current_trail)
                sub_count = len(children_map.get(nuuid, []))
                node = cls._serialize_tree_row(r, child_count=sub_count, path=path_str, depth=depth)
                if parent_uuid != str(r["parent_uuid"] or ""):
                    node["parentUuid"] = parent_uuid
                ordered_items.append(node)
                visit(nuuid, current_trail, depth + 1)

        visit("", [], 0)
        for r in rows:
            nuuid = str(r["note_uuid"])
            if nuuid not in visited:
                visited.add(nuuid)
                title = str(r["title"] or "未命名笔记")
                ancestors_map[nuuid] = []
                node = cls._serialize_tree_row(
                    r,
                    child_count=len(children_map.get(nuuid, [])),
                    path=title,
                    depth=0,
                )
                node["parentUuid"] = ""
                ordered_items.append(node)
        return ordered_items, ancestors_map

    async def list_tree(self, owner_chat_id: int) -> dict[str, Any]:
        rows = await self._fetch_light_rows(self.db.conn, owner_chat_id)
        items, _ = self._build_tree_metadata(rows)
        return {
            "items": items,
            "total": len(items),
        }

    async def get_note(self, owner_chat_id: int, note_uuid: str) -> dict[str, Any]:
        target = str(note_uuid or "").strip()
        if not target:
            raise KBNoteNotFound()
        cur = await self.db.conn.execute(
            "SELECT * FROM kb_notes WHERE owner_chat_id=? AND note_uuid=? LIMIT 1",
            (owner_chat_id, target),
        )
        row = await cur.fetchone()
        if row is None:
            raise KBNoteNotFound()
        light_rows = await self._fetch_light_rows(self.db.conn, owner_chat_id)
        items, ancestors_map = self._build_tree_metadata(light_rows)
        meta = next((item for item in items if item["noteUuid"] == target), None)
        base = meta or self._serialize_tree_row(row, child_count=0, path=str(row["title"] or ""), depth=0)
        return {
            **base,
            "content": str(row["content"] or ""),
            "ancestors": ancestors_map.get(target, []),
        }

    async def note_contents(self, owner_chat_id: int) -> dict[str, str]:
        cur = await self.db.conn.execute(
            "SELECT note_uuid, content FROM kb_notes WHERE owner_chat_id=?",
            (owner_chat_id,),
        )
        return {str(r["note_uuid"]): str(r["content"] or "") for r in await cur.fetchall()}

    async def _next_sort_order(self, conn: Any, owner_chat_id: int, parent_uuid: str) -> int:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS max_sort FROM kb_notes WHERE owner_chat_id=? AND parent_uuid=?",
            (owner_chat_id, parent_uuid),
        )
        row = await cur.fetchone()
        return int(row["max_sort"] if row is not None else 0) + 10

    async def create_note(
        self,
        owner_chat_id: int,
        *,
        parent_uuid: str = "",
        title: str = "未命名笔记",
        icon: str = "",
        content: str = "",
        is_readonly: bool = False,
        source_kind: str = "",
        source_ref: str = "",
        after_uuid: str = "",
    ) -> dict[str, Any]:
        parent = str(parent_uuid or "").strip()
        cleaned_title = clean_title(title)
        cleaned_icon = clean_icon(icon)
        cleaned_content = normalize_markdown(content)
        summary, char_count = summarize_markdown(cleaned_content)
        note_uuid = str(uuid.uuid4())
        ts = now_ms()

        async with self.db.write_transaction(label="kb_notes_create") as conn:
            if parent:
                cur = await conn.execute(
                    "SELECT 1 FROM kb_notes WHERE owner_chat_id=? AND note_uuid=? LIMIT 1",
                    (owner_chat_id, parent),
                )
                if await cur.fetchone() is None:
                    raise KBNoteNotFound("指定的上级笔记不存在")
            sort_order = await self._next_sort_order(conn, owner_chat_id, parent)
            await conn.execute(
                """
                INSERT INTO kb_notes (
                    note_uuid, owner_chat_id, parent_uuid, title, icon, content,
                    summary, char_count, pinned_at, sort_order, is_readonly, revision,
                    source_kind, source_ref, created_at_ms, updated_at_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    note_uuid,
                    owner_chat_id,
                    parent,
                    cleaned_title,
                    cleaned_icon,
                    cleaned_content,
                    summary,
                    char_count,
                    sort_order,
                    1 if is_readonly else 0,
                    str(source_kind or "")[:40],
                    str(source_ref or "")[:200],
                    ts,
                    ts,
                ),
            )
            if after_uuid:
                await self._reorder_in_tx(
                    conn,
                    owner_chat_id,
                    note_uuid,
                    target_parent_uuid=parent,
                    after_uuid=str(after_uuid).strip(),
                )
        return await self.get_note(owner_chat_id, note_uuid)

    async def update_note(
        self,
        owner_chat_id: int,
        note_uuid: str,
        changes: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        target = str(note_uuid or "").strip()
        if not target:
            raise KBNoteNotFound()

        conflict = False
        async with self.db.write_transaction(label="kb_notes_update") as conn:
            cur = await conn.execute(
                "SELECT * FROM kb_notes WHERE owner_chat_id=? AND note_uuid=? LIMIT 1",
                (owner_chat_id, target),
            )
            row = await cur.fetchone()
            if row is None:
                raise KBNoteNotFound()

            current_rev = int(row["revision"] or 1)
            if expected_revision is not None and int(expected_revision) != current_rev:
                conflict = True
            else:
                currently_readonly = bool(row["is_readonly"])
                next_readonly = (
                    bool(changes["isReadonly"])
                    if "isReadonly" in changes
                    else currently_readonly
                )
                editing_body_or_title = any(k in changes for k in ("title", "content", "icon"))
                if currently_readonly and next_readonly and editing_body_or_title:
                    raise KBNoteReadonly()

                next_title = clean_title(changes["title"]) if "title" in changes else str(row["title"])
                next_icon = clean_icon(changes["icon"]) if "icon" in changes else str(row["icon"] or "")
                next_content = (
                    normalize_markdown(changes["content"])
                    if "content" in changes
                    else str(row["content"] or "")
                )
                if "content" in changes:
                    summary, char_count = summarize_markdown(next_content)
                else:
                    summary, char_count = str(row["summary"] or ""), int(row["char_count"] or 0)

                current_pinned_at = int(row["pinned_at"] or 0)
                if "pinned" in changes:
                    next_pinned_at = now_ms() if bool(changes["pinned"]) else 0
                else:
                    next_pinned_at = current_pinned_at

                changed = (
                    next_title != str(row["title"])
                    or next_icon != str(row["icon"] or "")
                    or next_content != str(row["content"] or "")
                    or next_pinned_at != current_pinned_at
                    or next_readonly != currently_readonly
                )
                if changed:
                    ts = now_ms()
                    content_or_meta_edited = (
                        next_title != str(row["title"])
                        or next_icon != str(row["icon"] or "")
                        or next_content != str(row["content"] or "")
                    )
                    updated_at = ts if content_or_meta_edited else int(row["updated_at_ms"] or ts)
                    await conn.execute(
                        """
                        UPDATE kb_notes
                        SET title=?, icon=?, content=?, summary=?, char_count=?,
                            pinned_at=?, is_readonly=?, revision=?, updated_at_ms=?
                        WHERE owner_chat_id=? AND note_uuid=?
                        """,
                        (
                            next_title,
                            next_icon,
                            next_content,
                            summary,
                            char_count,
                            next_pinned_at,
                            1 if next_readonly else 0,
                            current_rev + 1,
                            updated_at,
                            owner_chat_id,
                            target,
                        ),
                    )
        latest = await self.get_note(owner_chat_id, target)
        if conflict:
            raise KBNoteConflict(latest)
        return latest

    async def append_to_note(
        self,
        owner_chat_id: int,
        note_uuid: str,
        markdown_block: str,
    ) -> dict[str, Any]:
        target = str(note_uuid or "").strip()
        block = normalize_markdown(markdown_block).strip()
        if not block:
            raise KBNoteError("empty_append", "追加内容不能为空")

        async with self.db.write_transaction(label="kb_notes_append") as conn:
            cur = await conn.execute(
                "SELECT * FROM kb_notes WHERE owner_chat_id=? AND note_uuid=? LIMIT 1",
                (owner_chat_id, target),
            )
            row = await cur.fetchone()
            if row is None:
                raise KBNoteNotFound()
            if bool(row["is_readonly"]):
                raise KBNoteReadonly()
            existing = str(row["content"] or "").rstrip()
            combined = f"{existing}\n\n{block}\n" if existing else f"{block}\n"
            if len(combined) > MAX_NOTE_CONTENT_CHARS:
                raise KBNoteError("content_too_long", f"笔记正文不能超过 {MAX_NOTE_CONTENT_CHARS} 个字符")
            summary, char_count = summarize_markdown(combined)
            ts = now_ms()
            await conn.execute(
                """
                UPDATE kb_notes
                SET content=?, summary=?, char_count=?, revision=revision+1, updated_at_ms=?
                WHERE owner_chat_id=? AND note_uuid=?
                """,
                (combined, summary, char_count, ts, owner_chat_id, target),
            )
        return await self.get_note(owner_chat_id, target)

    async def _reorder_in_tx(
        self,
        conn: Any,
        owner_chat_id: int,
        note_uuid: str,
        *,
        target_parent_uuid: str = "",
        before_uuid: str = "",
        after_uuid: str = "",
    ) -> None:
        cur = await conn.execute(
            "SELECT note_uuid, parent_uuid, pinned_at, sort_order, id FROM kb_notes WHERE owner_chat_id=?",
            (owner_chat_id,),
        )
        all_rows = list(await cur.fetchall())
        by_uuid = {str(r["note_uuid"]): r for r in all_rows}
        if note_uuid not in by_uuid:
            raise KBNoteNotFound()

        target_parent = str(target_parent_uuid or "").strip()
        if target_parent:
            if target_parent == note_uuid:
                raise KBNoteError("circular_note_hierarchy", "不能将笔记移动到其自身之下")
            if target_parent not in by_uuid:
                raise KBNoteNotFound("目标上级笔记不存在")
            cursor = target_parent
            seen: set[str] = set()
            while cursor:
                if cursor == note_uuid:
                    raise KBNoteError("circular_note_hierarchy", "不能将笔记移动到其子孙笔记之下")
                if cursor in seen:
                    break
                seen.add(cursor)
                parent_row = by_uuid.get(cursor)
                cursor = str(parent_row["parent_uuid"] or "") if parent_row is not None else ""

        siblings = [
            r for r in all_rows
            if str(r["parent_uuid"] or "") == target_parent and str(r["note_uuid"]) != note_uuid
        ]
        siblings.sort(
            key=lambda r: (
                0 if int(r["pinned_at"] or 0) > 0 else 1,
                -int(r["pinned_at"] or 0),
                int(r["sort_order"] or 0),
                int(r["id"] or 0),
            )
        )
        sibling_uuids = [str(r["note_uuid"]) for r in siblings]

        insert_idx = len(sibling_uuids)
        if before_uuid and before_uuid in sibling_uuids:
            insert_idx = sibling_uuids.index(before_uuid)
        elif after_uuid and after_uuid in sibling_uuids:
            insert_idx = sibling_uuids.index(after_uuid) + 1

        sibling_uuids.insert(insert_idx, note_uuid)
        await conn.execute(
            "UPDATE kb_notes SET parent_uuid=? WHERE owner_chat_id=? AND note_uuid=?",
            (target_parent, owner_chat_id, note_uuid),
        )
        for idx, suuid in enumerate(sibling_uuids, start=1):
            await conn.execute(
                "UPDATE kb_notes SET sort_order=? WHERE owner_chat_id=? AND note_uuid=?",
                (idx * 10, owner_chat_id, suuid),
            )

    async def move_note(
        self,
        owner_chat_id: int,
        note_uuid: str,
        *,
        target_parent_uuid: str = "",
        before_uuid: str = "",
        after_uuid: str = "",
    ) -> dict[str, Any]:
        target = str(note_uuid or "").strip()
        if not target:
            raise KBNoteNotFound()
        async with self.db.write_transaction(label="kb_notes_move") as conn:
            await self._reorder_in_tx(
                conn,
                owner_chat_id,
                target,
                target_parent_uuid=str(target_parent_uuid or "").strip(),
                before_uuid=str(before_uuid or "").strip(),
                after_uuid=str(after_uuid or "").strip(),
            )
        return await self.list_tree(owner_chat_id)

    async def delete_note(
        self,
        owner_chat_id: int,
        note_uuid: str,
        *,
        mode: str = "cascade",
    ) -> dict[str, Any]:
        target = str(note_uuid or "").strip()
        if not target:
            raise KBNoteNotFound()
        if mode not in {"cascade", "promote_children"}:
            raise KBNoteError("invalid_delete_mode", "无效的删除模式")

        deleted_uuids: list[str] = []
        async with self.db.write_transaction(label="kb_notes_delete") as conn:
            cur = await conn.execute(
                "SELECT note_uuid, parent_uuid, is_readonly FROM kb_notes WHERE owner_chat_id=?",
                (owner_chat_id,),
            )
            rows = list(await cur.fetchall())
            by_uuid = {str(r["note_uuid"]): r for r in rows}
            target_row = by_uuid.get(target)
            if target_row is None:
                raise KBNoteNotFound()
            if bool(target_row["is_readonly"]):
                raise KBNoteReadonly("当前笔记已开启只读锁定，请先解锁再删除")

            children_map: dict[str, list[str]] = {}
            for r in rows:
                children_map.setdefault(str(r["parent_uuid"] or ""), []).append(str(r["note_uuid"]))

            if mode == "promote_children":
                parent_uuid = str(target_row["parent_uuid"] or "")
                await conn.execute(
                    "UPDATE kb_notes SET parent_uuid=? WHERE owner_chat_id=? AND parent_uuid=?",
                    (parent_uuid, owner_chat_id, target),
                )
                await conn.execute(
                    "DELETE FROM kb_notes WHERE owner_chat_id=? AND note_uuid=?",
                    (owner_chat_id, target),
                )
                deleted_uuids = [target]
            else:
                stack = [target]
                seen: set[str] = set()
                while stack:
                    current = stack.pop()
                    if current in seen:
                        continue
                    seen.add(current)
                    row = by_uuid.get(current)
                    if row is not None and bool(row["is_readonly"]) and current != target:
                        raise KBNoteReadonly("子笔记中存在已开启只读锁定的文档，请先解锁再级联删除")
                    deleted_uuids.append(current)
                    stack.extend(children_map.get(current, []))
                placeholders = ",".join("?" for _ in deleted_uuids)
                await conn.execute(
                    f"DELETE FROM kb_notes WHERE owner_chat_id=? AND note_uuid IN ({placeholders})",  # noqa: S608
                    (owner_chat_id, *deleted_uuids),
                )

        return {"deletedUuids": deleted_uuids, "deletedCount": len(deleted_uuids)}

    async def search_notes(
        self,
        owner_chat_id: int,
        query: str,
        *,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        terms = [t.lower() for t in str(query or "").strip().split() if t.strip()]
        if not terms:
            return []
        max_limit = max(1, min(100, int(limit or 40)))
        light_rows = await self._fetch_light_rows(self.db.conn, owner_chat_id)
        items, _ = self._build_tree_metadata(light_rows)
        meta_by_uuid = {item["noteUuid"]: item for item in items}

        cur = await self.db.conn.execute(
            "SELECT note_uuid, title, content, updated_at_ms FROM kb_notes WHERE owner_chat_id=?",
            (owner_chat_id,),
        )
        full_rows = list(await cur.fetchall())
        scored: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
        for r in full_rows:
            nuuid = str(r["note_uuid"])
            title = str(r["title"] or "")
            content = str(r["content"] or "")
            meta = meta_by_uuid.get(nuuid)
            path_str = meta["path"] if meta else title
            hay_title = f"{title} {path_str}".lower()
            hay_body = content.lower()
            matched_all = all(t in hay_title or t in hay_body for t in terms)
            if not matched_all:
                continue
            title_hits = sum(1 for t in terms if t in title.lower())
            path_hits = sum(1 for t in terms if t in hay_title)
            snippet = self._extract_snippet(content, terms, fallback=meta["summary"] if meta else "")
            entry = {
                **(meta or {"noteUuid": nuuid, "title": title, "path": path_str, "icon": ""}),
                "snippet": snippet,
            }
            scored.append(((title_hits, path_hits, int(r["updated_at_ms"] or 0)), entry))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in scored[:max_limit]]

    @staticmethod
    def _extract_snippet(content: str, terms: list[str], *, fallback: str = "") -> str:
        if not content:
            return fallback
        clean = re.sub(r"\s+", " ", content).strip()
        lower = clean.lower()
        pos = -1
        for t in terms:
            idx = lower.find(t)
            if idx >= 0 and (pos < 0 or idx < pos):
                pos = idx
        if pos < 0:
            return fallback or clean[:140]
        start = max(0, pos - 42)
        end = min(len(clean), pos + 98)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(clean) else ""
        return f"{prefix}{clean[start:end].strip()}{suffix}"

    def _blob_path(self, sha256_hex: str) -> Path:
        return (self.asset_root / "blobs" / sha256_hex[:2] / sha256_hex).resolve()

    async def store_asset(
        self,
        owner_chat_id: int,
        *,
        data: bytes,
        filename: str = "image.png",
        mime_type: str = "",
        note_uuid: str = "",
        source_path: str = "",
    ) -> dict[str, Any]:
        if not data:
            raise KBNoteError("empty_asset", "上传图片内容为空")
        if len(data) > MAX_ASSET_BYTES:
            raise KBNoteError("asset_too_large", f"单张图片大小不能超过 {MAX_ASSET_BYTES // (1024 * 1024)} MiB")

        detected_mime = sniff_image_mime(data, filename=filename, declared_mime=mime_type)
        ext = ALLOWED_IMAGE_MIMES.get(detected_mime, ".png")
        safe_name = sanitize_filename(filename, fallback_ext=ext)
        digest = hashlib.sha256(data).hexdigest()

        cur = await self.db.conn.execute(
            """
            SELECT asset_uuid, filename, mime_type, size_bytes, storage_relpath
            FROM kb_note_assets
            WHERE owner_chat_id=? AND sha256=?
            LIMIT 1
            """,
            (owner_chat_id, digest),
        )
        existing = await cur.fetchone()
        blob_file = self._blob_path(digest)
        if not blob_file.exists():
            blob_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = blob_file.with_suffix(f".tmp.{uuid.uuid4().hex}")
            tmp_file.write_bytes(data)
            tmp_file.replace(blob_file)

        if existing is not None:
            asset_uuid = str(existing["asset_uuid"])
            stored_name = str(existing["filename"])
            return {
                "assetUuid": asset_uuid,
                "filename": stored_name,
                "mimeType": str(existing["mime_type"]),
                "sizeBytes": int(existing["size_bytes"]),
                "url": f"/api/kb-notes/assets/{asset_uuid}/{stored_name}",
            }

        asset_uuid = uuid.uuid4().hex
        relpath = f"blobs/{digest[:2]}/{digest}"
        ts = now_ms()
        async with self.db.write_transaction(label="kb_note_asset_store") as conn:
            await conn.execute(
                """
                INSERT INTO kb_note_assets (
                    asset_uuid, owner_chat_id, note_uuid, filename, mime_type,
                    size_bytes, sha256, storage_relpath, source_path, created_at_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_uuid,
                    owner_chat_id,
                    str(note_uuid or "").strip(),
                    safe_name,
                    detected_mime,
                    len(data),
                    digest,
                    relpath,
                    str(source_path or "")[:300],
                    ts,
                ),
            )
        return {
            "assetUuid": asset_uuid,
            "filename": safe_name,
            "mimeType": detected_mime,
            "sizeBytes": len(data),
            "url": f"/api/kb-notes/assets/{asset_uuid}/{safe_name}",
        }

    async def get_asset(self, owner_chat_id: int, asset_uuid: str) -> dict[str, Any]:
        target = re.sub(r"[^a-zA-Z0-9_-]", "", str(asset_uuid or "").strip())
        if not target:
            raise KBNoteNotFound("未找到对应的图片资源")
        cur = await self.db.conn.execute(
            "SELECT * FROM kb_note_assets WHERE owner_chat_id=? AND asset_uuid=? LIMIT 1",
            (owner_chat_id, target),
        )
        row = await cur.fetchone()
        if row is None:
            raise KBNoteNotFound("未找到对应的图片资源")
        relpath = str(row["storage_relpath"] or "")
        file_path = (self.asset_root / relpath).resolve()
        if not file_path.is_relative_to(self.asset_root) or not file_path.is_file():
            raise KBNoteNotFound("图片资源文件已不存在")
        return {
            "assetUuid": str(row["asset_uuid"]),
            "filename": str(row["filename"]),
            "mimeType": str(row["mime_type"]),
            "sizeBytes": int(row["size_bytes"] or 0),
            "path": file_path,
        }
