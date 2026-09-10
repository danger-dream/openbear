# ruff: noqa: F401,F403,F405
from __future__ import annotations

import shutil
from weakref import WeakValueDictionary

from app.web_console.core import *
from app.web_console.live_stream import *

# Transport buffers, not file-size limits. Each HTTP body is streamed to disk.
_UPLOAD_BUFFER_BYTES = 256 * 1024


def _upload_error(status: type[web.HTTPException], code: str) -> web.HTTPException:
    return status(text=json.dumps({"ok": False, "error": code}), content_type="application/json")


class WebAdminUploadsMixin:
    def _upload_directory(self, conversation: dict[str, Any], upload_id: str) -> Path:
        # Neither a client path nor another conversation's upload may be resolved.
        if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
            raise _upload_error(web.HTTPNotFound, "attachment_upload_not_found")
        return _download_root(self.config.media) / "web" / "uploads" / str(conversation["conversation_uuid"]) / upload_id

    def _upload_lock(self, directory: Path) -> asyncio.Lock:
        locks = getattr(self, "_http_upload_locks", None)
        if locks is None:
            locks = self._http_upload_locks = WeakValueDictionary()
        key = str(directory)
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    @staticmethod
    def _read_upload(directory: Path) -> tuple[dict[str, Any], Path]:
        try:
            metadata = json.loads((directory / "metadata.json").read_text("utf-8"))
            path = directory / "file" / metadata["fileName"]
            if not path.is_file():
                raise FileNotFoundError
            return metadata, path
        except (OSError, ValueError, KeyError):
            raise _upload_error(web.HTTPNotFound, "attachment_upload_not_found") from None

    @staticmethod
    def _write_upload(directory: Path, metadata: dict[str, Any]) -> None:
        temp = directory / "metadata.tmp"
        temp.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        temp.replace(directory / "metadata.json")

    async def handle_api_upload_create(self, request: web.Request) -> web.Response:
        conversation = await self._conversation_from_request(request)
        try:
            data = await request.json()
        except ValueError:
            raise _upload_error(web.HTTPBadRequest, "invalid_upload_metadata") from None
        if not isinstance(data, dict) or type(data.get("size")) is not int or data["size"] < 0:
            raise _upload_error(web.HTTPBadRequest, "invalid_upload_metadata")
        name = _safe_upload_name(str(data.get("name") or "upload.bin"))
        metadata = {
            "fileName": name,
            "mimeType": _guess_mime(name, str(data.get("type") or "")),
            "size": data["size"],
            "complete": False,
        }
        upload_id = uuid.uuid4().hex
        directory = self._upload_directory(conversation, upload_id)
        try:
            (directory / "file").mkdir(parents=True)
            (directory / "file" / name).touch()
            self._write_upload(directory, metadata)
        except OSError:
            shutil.rmtree(directory, ignore_errors=True)
            raise _upload_error(web.HTTPInsufficientStorage, "attachment_storage_failed") from None
        return web.json_response({"ok": True, "uploadId": upload_id, "offset": 0})

    async def handle_api_upload_chunk(self, request: web.Request) -> web.Response:
        conversation = await self._conversation_from_request(request)
        directory = self._upload_directory(conversation, str(request.match_info["upload_uuid"]))
        try:
            offset = int(request.query.get("offset", ""))
        except ValueError:
            raise _upload_error(web.HTTPBadRequest, "invalid_upload_offset") from None
        # A stale/repeated/parallel chunk must never silently append twice.
        async with self._upload_lock(directory):
            metadata, path = self._read_upload(directory)
            if metadata["complete"]:
                raise _upload_error(web.HTTPConflict, "attachment_upload_complete")
            if offset < 0 or offset != path.stat().st_size:
                raise _upload_error(web.HTTPConflict, "attachment_upload_offset_mismatch")
            try:
                with path.open("r+b") as dest:
                    dest.seek(offset)
                    try:
                        # Do not use request.read()/post(): those buffer the body
                        # and enforce aiohttp's ordinary JSON request-size limit.
                        async for chunk in request.content.iter_chunked(_UPLOAD_BUFFER_BYTES):
                            if dest.tell() + len(chunk) > metadata["size"]:
                                raise _upload_error(web.HTTPConflict, "attachment_upload_size_mismatch")
                            dest.write(chunk)
                        end = dest.tell()
                    except BaseException:
                        # A disconnected or rejected chunk is retryable at its
                        # original offset, never a silently committed prefix.
                        dest.truncate(offset)
                        raise
            except OSError:
                raise _upload_error(web.HTTPInsufficientStorage, "attachment_storage_failed") from None
        return web.json_response({"ok": True, "offset": end})

    async def handle_api_upload_complete(self, request: web.Request) -> web.Response:
        conversation = await self._conversation_from_request(request)
        upload_id = str(request.match_info["upload_uuid"])
        directory = self._upload_directory(conversation, upload_id)
        async with self._upload_lock(directory):
            metadata, path = self._read_upload(directory)
            if path.stat().st_size != metadata["size"]:
                raise _upload_error(web.HTTPConflict, "attachment_upload_incomplete")
            if not metadata["complete"]:
                # Do the large-file artifact work before the WS acceptance timer
                # starts. Sending a reference must not hash/copy the file again.
                try:
                    artifact = await self._register_web_artifact_from_path(path, conversation=conversation)
                    metadata["artifactUuid"] = (artifact or {}).get("artifactUuid", "")
                    metadata["complete"] = True
                    self._write_upload(directory, metadata)
                except OSError:
                    raise _upload_error(web.HTTPInsufficientStorage, "attachment_storage_failed") from None
        return web.json_response({"ok": True, "uploadId": upload_id})

    async def handle_api_upload_delete(self, request: web.Request) -> web.Response:
        conversation = await self._conversation_from_request(request)
        directory = self._upload_directory(conversation, str(request.match_info["upload_uuid"]))
        async with self._upload_lock(directory):
            if directory.exists():
                metadata, _path = self._read_upload(directory)
                # A lost completion/send receipt is not permission to delete a
                # file that may already belong to a message or model request.
                if metadata["complete"]:
                    raise _upload_error(web.HTTPConflict, "attachment_upload_complete")
                shutil.rmtree(directory)
        return web.json_response({"ok": True})

    async def _resolve_http_uploads(self, files: list[Any], *, conversation: dict[str, Any]) -> list[InboundMedia]:
        media: list[InboundMedia] = []
        max_items = max(0, int(self.config.media.max_media_per_message or 0))
        # Inline/base64 files are deliberately no longer a supported WS protocol.
        if any(not isinstance(raw, dict) or set(raw) != {"uploadId"} for raw in files):
            raise _upload_error(web.HTTPBadRequest, "attachment_upload_required")
        selected = files[:max_items] if max_items else files
        for raw in selected:
            directory = self._upload_directory(conversation, str(raw["uploadId"]))
            metadata, path = self._read_upload(directory)
            if not metadata["complete"] or path.stat().st_size != metadata["size"]:
                raise _upload_error(web.HTTPConflict, "attachment_upload_incomplete")
            item = InboundMedia(
                kind=_web_media_kind(metadata["fileName"], metadata["mimeType"]),
                upload_type="web_upload", file_name=metadata["fileName"],
                mime_type=metadata["mimeType"], size=metadata["size"], path=str(path),
                artifact_uuid=metadata.get("artifactUuid", ""),
            )
            if item.kind == "file" and _is_text_file(item.file_name, item.mime_type):
                item.text_excerpt, _truncated = _extract_text(path)
            media.append(item)
        omitted = len(files) - len(selected)
        if omitted:
            media.append(InboundMedia(kind="file", upload_type="limit", skipped=True, error=f"本轮 Web 附件数量超过配置上限，已忽略 {omitted} 个。"))
        return media


__all__ = [name for name in globals() if not name.startswith("__")]
