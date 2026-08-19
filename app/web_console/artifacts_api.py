# ruff: noqa: F401,F403,F405
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote, unquote, urlsplit

from app.web_console.core import *
from app.web_console.live_stream import *

_ARTIFACT_MARKDOWN_LINK_RE = re.compile(r"(?P<prefix>!?\[[^\]\n]{0,300}\]\()(?P<target><[^>\n]+>|[^)\s\n]+)")
_ARTIFACT_HTML_ATTR_RE = re.compile(r"(?P<prefix>\b(?:src|href)=['\"])(?P<target>[^'\"]+)(?P<suffix>['\"])", re.IGNORECASE)
_ARTIFACT_QUOTED_LOCAL_RE = re.compile(
    r"(?P<quote>['\"`])(?P<target>/assets/[^'\"`\s]+|(?:\./)?workspace/artifacts/[^'\"`\s]+)(?P=quote)"
)

_ARTIFACT_INLINE_MIMES = {
    "application/pdf",
    "application/json",
    "application/x-jsonlines",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/javascript",
    "application/x-javascript",
    "application/typescript",
    "application/x-sh",
    "application/sql",
}
_ARTIFACT_FORCE_DOWNLOAD_MIMES = {
    "image/svg+xml",
    "text/html",
    "application/xhtml+xml",
}
_ARTIFACT_FORCE_DOWNLOAD_EXTS = {
    ".7z", ".apk", ".appimage", ".bat", ".bin", ".bz2", ".cab", ".cmd", ".deb",
    ".dmg", ".exe", ".gz", ".iso", ".jar", ".msi", ".pkg", ".ps1", ".rar",
    ".rpm", ".run", ".sh", ".tar", ".tgz", ".xz", ".zip", ".zst",
}


class WebAdminArtifactsMixin:
    def _web_artifact_root(self) -> Path:
        configured = str(os.environ.get("OPENBEAR_WEB_ARTIFACT_DIR") or "").strip()
        root = Path(configured).expanduser() if configured else Path.cwd() / "data" / "web_artifacts"
        return root.resolve()

    def _web_artifact_blob_root(self) -> Path:
        return self._web_artifact_root() / "blobs"

    def _web_artifact_blob_path(self, digest: str) -> Path:
        safe = re.sub(r"[^a-fA-F0-9]", "", str(digest or "").lower())
        if len(safe) != 64:
            raise ValueError("invalid artifact digest")
        return (self._web_artifact_blob_root() / safe[:2] / safe).resolve()

    def _web_artifact_source_roots(self) -> list[Path]:
        cwd = Path.cwd().resolve()
        workspace_root = Path(str(getattr(self, "workspace_dir", "") or (cwd / "workspace"))).resolve()
        media_root = _download_root(self.config.media) if getattr(getattr(self, "config", None), "media", None) is not None else cwd / "data" / "media" / "inbound"
        roots = [
            workspace_root / "artifacts",
            cwd / "web" / "dist" / "assets",
            media_root,
        ]
        return [p.resolve() for p in roots]

    def _web_artifact_denied_roots(self) -> list[Path]:
        cwd = Path.cwd().resolve()
        return [
            cwd / ".git",
            cwd / ".venv",
            cwd / "data" / "web_artifacts",
            cwd / "data" / "tool_artifacts",
            cwd / "logs",
            cwd / "web" / "node_modules",
        ]

    @staticmethod
    def _web_artifact_is_within(path: Path, roots: Iterable[Path]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _web_artifact_source_allowed(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        if not resolved.is_file():
            return False
        lowered_name = resolved.name.lower()
        if lowered_name in {"openbear.json", ".env"} or lowered_name.endswith((".db", ".sqlite", ".sqlite3")):
            return False
        if self._web_artifact_is_within(resolved, self._web_artifact_denied_roots()):
            return False
        return self._web_artifact_is_within(resolved, self._web_artifact_source_roots())

    def _web_artifact_source_from_ref(self, value: str) -> Path | None:
        raw = str(value or "").strip()
        if raw.startswith("<") and raw.endswith(">"):
            raw = raw[1:-1].strip()
        if not raw or "\x00" in raw:
            return None
        split = urlsplit(raw)
        if split.scheme and split.scheme.lower() not in {"file"}:
            return None
        if split.scheme.lower() == "file":
            raw_path = unquote(split.path or "")
        else:
            raw_path = unquote(split.path or raw)
        if not raw_path or raw_path.startswith("/api/"):
            return None
        cwd = Path.cwd().resolve()
        workspace_root = Path(str(getattr(self, "workspace_dir", "") or (cwd / "workspace"))).resolve()
        if raw_path.startswith("/assets/"):
            candidate = cwd / "web" / "dist" / "assets" / raw_path.removeprefix("/assets/")
        elif raw_path == "workspace" or raw_path.startswith("workspace/"):
            rel = raw_path.removeprefix("workspace/") if raw_path != "workspace" else ""
            candidate = workspace_root / rel
        elif raw_path == "./workspace" or raw_path.startswith("./workspace/"):
            rel = raw_path.removeprefix("./workspace/") if raw_path != "./workspace" else ""
            candidate = workspace_root / rel
        else:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = cwd / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        return resolved if self._web_artifact_source_allowed(resolved) else None

    @staticmethod
    def _web_artifact_inline_allowed(file_name: str, mime_type: str) -> bool:
        mime = (mime_type or "").split(";", 1)[0].strip().lower()
        ext = Path(file_name or "").suffix.lower()
        if mime in _ARTIFACT_FORCE_DOWNLOAD_MIMES or ext in {".html", ".htm", ".svg"}:
            return False
        if ext in _ARTIFACT_FORCE_DOWNLOAD_EXTS:
            return False
        return (
            mime.startswith("image/")
            or mime.startswith("audio/")
            or mime.startswith("video/")
            or mime.startswith("text/")
            or mime in _ARTIFACT_INLINE_MIMES
            or _is_text_file(file_name, mime)
        )

    @staticmethod
    def _web_artifact_content_disposition(file_name: str, *, inline: bool) -> str:
        safe = _safe_upload_name(file_name or "artifact.bin", fallback="artifact.bin")
        ascii_name = safe.encode("ascii", "ignore").decode("ascii") or "artifact.bin"
        disposition = "inline" if inline else "attachment"
        return f"{disposition}; filename=\"{ascii_name.replace(chr(34), '_')}\"; filename*=UTF-8''{quote(safe)}"

    def _web_artifact_public(self, row: dict[str, Any] | Any, conversation_uuid: str) -> dict[str, Any]:
        data = dict(row)
        artifact_uuid = str(data.get("artifact_uuid") or data.get("artifactUuid") or "")
        file_name = str(data.get("file_name") or data.get("fileName") or "artifact.bin")
        mime_type = str(data.get("mime_type") or data.get("mimeType") or "application/octet-stream")
        base = f"/api/conversations/{conversation_uuid}/artifacts/{artifact_uuid}/content"
        inline = self._web_artifact_inline_allowed(file_name, mime_type)
        return {
            "artifactUuid": artifact_uuid,
            "conversationUuid": conversation_uuid,
            "fileName": file_name,
            "mimeType": mime_type,
            "sizeBytes": int(data.get("size_bytes") or data.get("sizeBytes") or 0),
            "sha256": str(data.get("sha256") or ""),
            "createdAt": int(data.get("created_at") or data.get("createdAt") or 0),
            "inlinePreview": inline,
            "contentUrl": base,
            "previewUrl": f"{base}?preview=1",
            "downloadUrl": f"{base}?download=1",
        }

    async def _register_web_artifact_from_path(
        self,
        source: Path,
        *,
        conversation: dict[str, Any],
        turn_uuid: str = "",
        op_id: str = "",
        source_url: str = "",
    ) -> dict[str, Any] | None:
        if not self._web_artifact_source_allowed(source):
            return None
        conv_uuid = str(conversation.get("conversation_uuid") or "").strip()
        if not conv_uuid:
            return None
        owner_chat_id = int(conversation.get("owner_chat_id") or 0)
        internal_chat_id = int(conversation.get("internal_chat_id") or 0)
        safe_name = _safe_upload_name(source.name, fallback="artifact.bin")
        mime_type = _guess_mime(safe_name, "")

        tmp_dir = self._web_artifact_root() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"{uuid.uuid4()}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as src, tmp_path.open("wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
                    dst.write(chunk)
            sha256_value = digest.hexdigest()
            cur = await self.db.conn.execute(
                """
                SELECT * FROM web_artifacts
                WHERE conversation_uuid=? AND sha256=? AND file_name=? AND deleted_at=0
                LIMIT 1
                """,
                (conv_uuid, sha256_value, safe_name),
            )
            existing = await cur.fetchone()
            if existing is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                return self._web_artifact_public(dict(existing), conv_uuid)

            blob_path = self._web_artifact_blob_path(sha256_value)
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            if blob_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
            else:
                tmp_path.replace(blob_path)
            artifact_uuid = str(uuid.uuid4())
            await self.db.conn.execute(
                """
                INSERT INTO web_artifacts (
                  artifact_uuid, conversation_uuid, owner_chat_id, internal_chat_id,
                  turn_uuid, message_id, op_id, file_name, mime_type, size_bytes,
                  sha256, storage_path, source_path, source_url, created_at, deleted_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    artifact_uuid,
                    conv_uuid,
                    owner_chat_id,
                    internal_chat_id,
                    str(turn_uuid or ""),
                    0,
                    str(op_id or ""),
                    safe_name,
                    mime_type,
                    size,
                    sha256_value,
                    str(blob_path),
                    str(source),
                    str(source_url or ""),
                    now_ts(),
                ),
            )
            await self.db.conn.commit()
            return self._web_artifact_public(
                {
                    "artifact_uuid": artifact_uuid,
                    "file_name": safe_name,
                    "mime_type": mime_type,
                    "size_bytes": size,
                    "sha256": sha256_value,
                    "created_at": now_ts(),
                },
                conv_uuid,
            )
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink()

    async def _rewrite_web_artifact_links(
        self,
        text: str,
        *,
        conversation: dict[str, Any] | None,
        turn_uuid: str = "",
        op_id: str = "",
    ) -> str:
        if not text or not conversation:
            return text
        workspace_root = Path(str(getattr(self, "workspace_dir", "") or (Path.cwd() / "workspace"))).resolve()
        markers = (
            "/assets/",
            "workspace/artifacts/",
            "./workspace/artifacts/",
            "/web/dist/assets/",
            str(Path.cwd().resolve() / "web" / "dist" / "assets"),
            str(workspace_root / "artifacts"),
            "file://",
        )
        if not any(marker in text for marker in markers):
            return text

        resolved: dict[tuple[str, bool], str] = {}

        async def artifact_url(raw_target: str, *, preview: bool) -> str:
            key = (raw_target, preview)
            if key in resolved:
                return resolved[key]
            path = self._web_artifact_source_from_ref(raw_target)
            if path is None:
                resolved[key] = raw_target
                return raw_target
            artifact = await self._register_web_artifact_from_path(
                path,
                conversation=conversation,
                turn_uuid=turn_uuid,
                op_id=op_id,
                source_url=raw_target,
            )
            if not artifact:
                resolved[key] = raw_target
                return raw_target
            resolved[key] = str(artifact.get("previewUrl") if preview else artifact.get("contentUrl") or raw_target)
            return resolved[key]

        async def replace_pattern(value: str, pattern: re.Pattern[str], *, preview_from_prefix: bool = False, preview: bool = False) -> str:
            out: list[str] = []
            last = 0
            for match in pattern.finditer(value):
                target = str(match.group("target") or "")
                target_for_lookup = target[1:-1] if target.startswith("<") and target.endswith(">") else target
                embed = preview or (preview_from_prefix and str(match.groupdict().get("prefix") or "").startswith("!["))
                replacement = await artifact_url(target_for_lookup, preview=embed)
                if replacement == target_for_lookup:
                    continue
                out.append(value[last:match.start("target")])
                out.append(replacement)
                last = match.end("target")
            if not out:
                return value
            out.append(value[last:])
            return "".join(out)

        text = await replace_pattern(text, _ARTIFACT_MARKDOWN_LINK_RE, preview_from_prefix=True)
        text = await replace_pattern(text, _ARTIFACT_HTML_ATTR_RE, preview=True)
        text = await replace_pattern(text, _ARTIFACT_QUOTED_LOCAL_RE)
        return text

    async def _web_artifact_row_for_request(self, request: web.Request) -> tuple[dict[str, Any], dict[str, Any]]:
        conversation = await self._conversation_from_request(request)
        conv_uuid = str(conversation.get("conversation_uuid") or "")
        artifact_uuid = str(request.match_info.get("artifact_uuid") or "").strip()
        cur = await self.db.conn.execute(
            """
            SELECT * FROM web_artifacts
            WHERE conversation_uuid=? AND artifact_uuid=? AND deleted_at=0
            LIMIT 1
            """,
            (conv_uuid, artifact_uuid),
        )
        row = await cur.fetchone()
        if row is None:
            raise web.HTTPNotFound(text="artifact not found")
        data = dict(row)
        if int(data.get("owner_chat_id") or 0) != int(conversation.get("owner_chat_id") or 0):
            raise web.HTTPNotFound(text="artifact not found")
        if int(data.get("internal_chat_id") or 0) != int(conversation.get("internal_chat_id") or 0):
            raise web.HTTPNotFound(text="artifact not found")
        return conversation, data

    async def handle_api_conversation_artifacts(self, request: web.Request) -> web.Response:
        conversation = await self._conversation_from_request(request)
        conv_uuid = str(conversation.get("conversation_uuid") or "")
        cur = await self.db.conn.execute(
            """
            SELECT * FROM web_artifacts
            WHERE conversation_uuid=? AND deleted_at=0
            ORDER BY created_at DESC, id DESC
            LIMIT 500
            """,
            (conv_uuid,),
        )
        return web.json_response({
            "ok": True,
            "conversationUuid": conv_uuid,
            "items": [self._web_artifact_public(dict(row), conv_uuid) for row in await cur.fetchall()],
        })

    async def handle_api_conversation_artifact(self, request: web.Request) -> web.Response:
        conversation, row = await self._web_artifact_row_for_request(request)
        conv_uuid = str(conversation.get("conversation_uuid") or "")
        return web.json_response({"ok": True, "artifact": self._web_artifact_public(row, conv_uuid)})

    async def handle_api_conversation_artifact_content(self, request: web.Request) -> web.StreamResponse:
        _conversation, row = await self._web_artifact_row_for_request(request)
        file_name = str(row.get("file_name") or "artifact.bin")
        mime_type = str(row.get("mime_type") or "application/octet-stream")
        storage_path = Path(str(row.get("storage_path") or "")).expanduser().resolve()
        blob_root = self._web_artifact_blob_root().resolve()
        try:
            storage_path.relative_to(blob_root)
        except ValueError:
            raise web.HTTPNotFound(text="artifact not found") from None
        if not storage_path.is_file():
            raise web.HTTPNotFound(text="artifact content missing")
        forced_download = str(request.query.get("download") or "").lower() in {"1", "true", "yes", "on"}
        requested_preview = str(request.query.get("preview") or "").lower() in {"1", "true", "yes", "on"}
        inline_allowed = self._web_artifact_inline_allowed(file_name, mime_type)
        inline = inline_allowed and (requested_preview or not forced_download)
        response_mime_type = mime_type.split(";", 1)[0].strip() or "application/octet-stream"
        if _is_text_file(file_name, response_mime_type):
            response_mime_type = f"{response_mime_type}; charset=utf-8"
        headers = {
            "Content-Type": response_mime_type,
            "Content-Disposition": self._web_artifact_content_disposition(file_name, inline=inline),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        }
        return web.FileResponse(storage_path, headers=headers)


__all__ = [name for name in globals() if not name.startswith("__")]
