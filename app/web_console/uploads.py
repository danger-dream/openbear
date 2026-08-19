# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.web_console.core import *
from app.web_console.live_stream import *


class WebAdminUploadsMixin:
    def _web_upload_limit_bytes(self, kind: str) -> int:
        return _size_limit_bytes(self.config.media, kind)

    async def _save_ws_uploads(self, files: list[Any], *, chat_id: int) -> list[InboundMedia]:
        media: list[InboundMedia] = []
        max_items = max(0, int(self.config.media.max_media_per_message or 0))
        omitted = 0
        for idx, raw in enumerate(files or [], start=1):
            if max_items and len(media) >= max_items:
                omitted += 1
                continue
            if not isinstance(raw, dict):
                continue
            original_name = str(raw.get("name") or raw.get("filename") or f"upload_{idx}.bin")
            safe_name = _safe_upload_name(original_name, fallback=f"upload_{idx}.bin")
            mime = _guess_mime(safe_name, str(raw.get("type") or raw.get("mime") or ""))
            data_text = str(raw.get("data") or raw.get("base64") or "")
            if "," in data_text and data_text.lstrip().startswith("data:"):
                data_text = data_text.split(",", 1)[1]
            data_text = "".join(data_text.split())
            item = InboundMedia(kind=_web_media_kind(safe_name, mime), upload_type="websocket_upload", source="current", file_name=safe_name, mime_type=mime)
            limit = self._web_upload_limit_bytes(item.kind)
            padding = len(data_text) - len(data_text.rstrip("="))
            estimated_size = max(0, (len(data_text) * 3) // 4 - padding)
            if limit and estimated_size > limit:
                item.size = estimated_size
                item.skipped = True
                item.error = f"文件超过配置上限（{_human_bytes(estimated_size)} > {_human_bytes(limit)}），未作为输入。"
                media.append(item)
                continue
            try:
                payload = base64.b64decode(data_text, validate=True)
            except Exception as exc:
                item.skipped = True
                item.error = f"WebSocket 附件解码失败：{type(exc).__name__}: {exc}"
                media.append(item)
                continue
            item.size = len(payload)
            if limit and item.size > limit:
                item.skipped = True
                item.error = f"文件超过配置上限（{_human_bytes(item.size)} > {_human_bytes(limit)}），未作为输入。"
                media.append(item)
                continue
            date_dir = time.strftime("%Y%m%d")
            dest_dir = _download_root(self.config.media) / "web" / date_dir / str(chat_id) / f"{now_ts()}_{secrets.token_hex(4)}"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / safe_name
            try:
                dest.write_bytes(payload)
                item.path = str(dest)
                if item.kind == "file" and _is_text_file(safe_name, mime):
                    item.text_excerpt, _truncated = _extract_text(dest)
            except Exception as exc:
                item.skipped = True
                item.error = f"WebSocket 附件保存失败：{type(exc).__name__}: {exc}"
            media.append(item)
        if omitted:
            media.append(InboundMedia(kind="file", upload_type="limit", source="current", skipped=True, error=f"本轮 Web 附件数量超过配置上限，已忽略 {omitted} 个。"))
        return media

__all__ = [name for name in globals() if not name.startswith("__")]
