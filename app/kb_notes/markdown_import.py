"""Import hierarchical notes and embedded images from Markdown files or ZIP archives."""
from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from app.kb_notes.dao import KBNoteError, KBNotesDAO, clean_title, normalize_markdown

MAX_ZIP_ENTRIES = 5000
MAX_ZIP_TOTAL_BYTES = 120 * 1024 * 1024

_MD_IMAGE_RE = re.compile(r"(!\[[^\]]*\]\()(?P<url><[^>]+>|[^)\s]+)(?P<tail>(?:\s+[\"'][^\"']*[\"'])?\))")
_HTML_IMG_RE = re.compile(r"(<img\b[^>]*?\bsrc=[\"'])(?P<url>[^\"']+)([\"'][^>]*>)", re.IGNORECASE)
_LEADING_H1_RE = re.compile(r"^\s*#\s+(?P<heading>[^\n]+)(?:\n+(?P<body>[\s\S]*))?$")
# Same front matter shape the editor keeps verbatim (web/src/notes/markdownCodec.js).
_FRONT_MATTER_RE = re.compile(r"^---\n(?=[A-Za-z0-9_-]+[ \t]*:)(?:[^\n]*\n)*?(?:---|\.\.\.)[ \t]*(?:\n|$)")


def strip_redundant_leading_h1(markdown: str, doc_title: str) -> str:
    """Drop a leading `# <title>` that only repeats the note title (as written by export).

    Front matter is kept as is; the heading may follow it.
    """
    text = normalize_markdown(markdown).removeprefix("\ufeff").lstrip("\n")
    front = _FRONT_MATTER_RE.match(text)
    head, rest = (front.group(0), text[front.end():].lstrip("\n")) if front else ("", text)
    match = _LEADING_H1_RE.match(rest)
    if not match:
        return text
    heading = html.unescape(match.group("heading").strip())
    if heading != html.unescape(str(doc_title or "").strip()):
        return text
    body = (match.group("body") or "").lstrip("\n")
    if not head:
        return body
    head = head.rstrip("\n")
    return f"{head}\n\n{body}" if body else head


def _extract_local_asset_refs(markdown: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in [*_MD_IMAGE_RE.finditer(markdown), *_HTML_IMG_RE.finditer(markdown)]:
        target = match.group("url").strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        if not target or target.startswith(("http://", "https://", "data:", "/api/")):
            continue
        if target not in seen:
            seen.add(target)
            found.append(target)
    return found


def _rewrite_asset_refs(markdown: str, url_map: dict[str, str]) -> str:
    if not url_map:
        return markdown

    def replace_md(match: re.Match[str]) -> str:
        raw_url = match.group("url")
        stripped = raw_url[1:-1].strip() if raw_url.startswith("<") and raw_url.endswith(">") else raw_url.strip()
        mapped = url_map.get(stripped)
        if not mapped:
            return match.group(0)
        return f"{match.group(1)}{mapped}{match.group('tail')}"

    def replace_html(match: re.Match[str]) -> str:
        mapped = url_map.get(match.group("url").strip())
        if not mapped:
            return match.group(0)
        return f"{match.group(1)}{mapped}{match.group(3)}"

    out = _MD_IMAGE_RE.sub(replace_md, markdown)
    return _HTML_IMG_RE.sub(replace_html, out)


async def import_from_markdown_or_zip(
    dao: KBNotesDAO,
    owner_chat_id: int,
    *,
    files: list[tuple[str, bytes]],
    target_parent_uuid: str = "",
) -> dict[str, Any]:
    """Import one or more `.md` files or `.zip` bundles preserving folder hierarchy and relative images."""
    parent_root = str(target_parent_uuid or "").strip()
    if parent_root:
        await dao.get_note(owner_chat_id, parent_root)

    md_entries: dict[str, str] = {}
    asset_entries: dict[str, bytes] = {}
    total_bytes = 0

    def add_entry(rel_path: str, data: bytes) -> None:
        nonlocal total_bytes
        norm = rel_path.replace("\\", "/").lstrip("/")
        if not norm:
            return
        parts = PurePosixPath(norm).parts
        if any(p in {"", ".", "..", "__MACOSX"} or p.startswith(".") for p in parts):
            return
        total_bytes += len(data)
        if total_bytes > MAX_ZIP_TOTAL_BYTES:
            raise KBNoteError("import_archive_too_large", "导入文件总大小超过上限（120 MiB）")
        lower = norm.lower()
        if lower.endswith((".md", ".markdown", ".txt")):
            md_entries[norm] = data.decode("utf-8", errors="replace")
        elif Path(norm).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico", ".svg"}:
            asset_entries[norm] = data

    for filename, payload in files:
        if filename.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                    infos = zf.infolist()
                    if len(infos) > MAX_ZIP_ENTRIES:
                        raise KBNoteError("import_too_many_entries", f"压缩包内文件数超过上限（{MAX_ZIP_ENTRIES}）")
                    for info in infos:
                        if info.is_dir():
                            continue
                        if info.file_size > MAX_ZIP_TOTAL_BYTES:
                            raise KBNoteError("import_archive_too_large", "压缩包内单个文件超过大小限制")
                        add_entry(info.filename, zf.read(info))
            except zipfile.BadZipFile as exc:
                raise KBNoteError("invalid_zip_file", "无效的 ZIP 压缩包文件") from exc
        else:
            add_entry(Path(filename).name, payload)

    if not md_entries:
        raise KBNoteError("no_markdown_found", "未找到可导入的 Markdown（.md）文档")

    folder_note_uuids: dict[str, str] = {"": parent_root}
    created_count = 0
    imported_assets = 0
    uploaded_asset_urls: dict[str, str] = {}
    first_created_uuid = ""

    async def ensure_folder(dir_posix: str) -> str:
        nonlocal created_count, first_created_uuid
        if dir_posix in folder_note_uuids:
            return folder_note_uuids[dir_posix]
        p = PurePosixPath(dir_posix)
        parent_dir = "" if str(p.parent) == "." else str(p.parent)
        up_uuid = await ensure_folder(parent_dir)
        created = await dao.create_note(
            owner_chat_id,
            parent_uuid=up_uuid,
            title=clean_title(p.name),
            icon="📁",
            content="",
            source_kind="markdown_import",
        )
        nuuid = str(created["noteUuid"])
        if not first_created_uuid:
            first_created_uuid = nuuid
        folder_note_uuids[dir_posix] = nuuid
        created_count += 1
        return nuuid

    for rel_md_path in sorted(md_entries.keys()):
        raw_md = md_entries[rel_md_path]
        p = PurePosixPath(rel_md_path)
        parent_dir = "" if str(p.parent) == "." else str(p.parent)
        stem = clean_title(p.stem)
        cleaned_md = strip_redundant_leading_h1(raw_md, stem)

        same_named_dir = f"{parent_dir}/{p.stem}".lstrip("/")
        has_matching_dir = any(k.startswith(f"{same_named_dir}/") for k in md_entries if k != rel_md_path)

        refs = _extract_local_asset_refs(cleaned_md)
        url_map: dict[str, str] = {}
        for ref in refs:
            clean_ref = unquote(ref.split("?", 1)[0].split("#", 1)[0])
            candidates = [
                str(PurePosixPath(parent_dir) / clean_ref).lstrip("./"),
                clean_ref.lstrip("./"),
            ]
            matched_key = next((c for c in candidates if c in asset_entries), "")
            if not matched_key:
                continue
            if matched_key not in uploaded_asset_urls:
                try:
                    stored = await dao.store_asset(
                        owner_chat_id,
                        data=asset_entries[matched_key],
                        filename=PurePosixPath(matched_key).name,
                        source_path=matched_key,
                    )
                    uploaded_asset_urls[matched_key] = stored["url"]
                    imported_assets += 1
                except KBNoteError:
                    continue
            url_map[ref] = uploaded_asset_urls[matched_key]

        final_md = _rewrite_asset_refs(cleaned_md, url_map)

        if has_matching_dir:
            folder_uuid = await ensure_folder(same_named_dir)
            await dao.update_note(owner_chat_id, folder_uuid, {"content": final_md, "icon": "📄"})
        else:
            up_uuid = await ensure_folder(parent_dir)
            created = await dao.create_note(
                owner_chat_id,
                parent_uuid=up_uuid,
                title=stem,
                content=final_md,
                source_kind="markdown_import",
            )
            if not first_created_uuid:
                first_created_uuid = str(created["noteUuid"])
            created_count += 1

    return {
        "createdCount": created_count,
        "importedAssets": imported_assets,
        "firstNoteUuid": first_created_uuid,
    }
