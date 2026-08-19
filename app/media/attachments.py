"""入口无关的用户附件归一化工具。"""
from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import MediaConfig

_TEXT_EXTRACT_MAX_CHARS = 12_000
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".weba"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg"}
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv", ".log",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg", ".sh", ".bash", ".zsh",
    ".sql", ".go", ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".rs", ".rb",
    ".php", ".swift", ".kt", ".kts", ".scala", ".lua", ".r", ".vue", ".svelte",
}
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIMES = {
    "application/json", "application/x-jsonlines", "application/xml", "application/xhtml+xml",
    "application/yaml", "application/x-yaml", "application/toml", "application/javascript",
    "application/x-javascript", "application/typescript", "application/x-sh", "application/sql",
}


@dataclass(slots=True)
class InboundMedia:
    """用户入口附件的协议无关描述。"""

    kind: str  # image | audio | video | file | sticker
    upload_type: str  # web_upload | websocket_upload | limit
    source: str = "current"  # current | reply
    message_id: int = 0
    file_id: str = ""
    file_unique_id: str = ""
    file_name: str = ""
    mime_type: str = ""
    size: int = 0
    width: int = 0
    height: int = 0
    duration: int = 0
    path: str = ""
    text_excerpt: str = ""
    transcript: str = ""
    transcription_error: str = ""
    error: str = ""
    skipped: bool = False
    caption: str = ""

    @property
    def is_image_block(self) -> bool:
        return self.kind in {"image", "sticker"} and bool(self.path) and not self.error and not self.skipped


def safe_filename(name: str, *, fallback: str) -> str:
    raw = (name or "").strip().replace("/", "_").replace("\\", "_")
    if not raw:
        raw = fallback
    raw = _SAFE_NAME_RE.sub("_", raw).strip("._")
    if not raw:
        raw = fallback
    # 留出前缀/目录空间，避免极长原文件名带来文件系统问题。
    return raw[:160]


def download_root(cfg: MediaConfig) -> Path:
    cwd = Path.cwd().resolve()
    root = Path(cfg.download_dir).expanduser()
    if not root.is_absolute():
        root = cwd / root
    root = root.resolve()
    try:
        root.relative_to(cwd)
    except ValueError:
        # 附件缓存必须留在工作目录内；配置到工作目录外时安全降级到默认目录。
        root = cwd / "data" / "media" / "inbound"
    return root


def guess_mime(file_name: str, fallback: str = "") -> str:
    guessed, _enc = mimetypes.guess_type(file_name or "")
    return (fallback or guessed or "application/octet-stream").split(";", 1)[0].strip().lower()


def mime_ext(mime: str, default: str) -> str:
    ext = mimetypes.guess_extension((mime or "").split(";", 1)[0].strip())
    if ext == ".jpe":
        return ".jpg"
    return ext or default


def is_text_file(file_name: str, mime: str) -> bool:
    ext = Path(file_name or "").suffix.lower()
    m = (mime or "").split(";", 1)[0].strip().lower()
    return ext in _TEXT_EXTS or m in _TEXT_MIMES or any(m.startswith(p) for p in _TEXT_MIME_PREFIXES)


def classify_media(file_name: str, mime: str) -> str:
    ext = Path(file_name or "").suffix.lower()
    m = (mime or "").split(";", 1)[0].strip().lower()
    if m.startswith("image/") or ext in _IMAGE_EXTS:
        return "image"
    if m.startswith("audio/") or ext in _AUDIO_EXTS:
        return "audio"
    if m.startswith("video/") or ext in _VIDEO_EXTS:
        return "video"
    return "file"


def size_limit_bytes(cfg: MediaConfig, kind: str) -> int:
    mb = {
        "image": cfg.max_image_mb,
        "sticker": cfg.max_image_mb,
        "audio": cfg.max_audio_mb,
        "video": cfg.max_video_mb,
        "file": cfg.max_file_mb,
    }.get(kind, cfg.max_file_mb)
    return int(max(0, mb) * 1024 * 1024)


def human_size(n: int) -> str:
    if n <= 0:
        return "未知大小"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{n}B"


def extract_text(path: Path, *, max_chars: int = _TEXT_EXTRACT_MAX_CHARS) -> tuple[str, bool]:
    data = path.read_bytes()[: max_chars * 4 + 4]
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars or path.stat().st_size > len(data)
    if len(text) > max_chars:
        text = text[:max_chars]
    # 粗略规避误判二进制：NUL 较多时不拼正文。
    if text.count("\x00") > max(2, len(text) // 100):
        return "", False
    if truncated:
        text = text.rstrip() + "\n…[附件文本已截断]"
    return text.strip(), truncated


def _label(m: InboundMedia) -> str:
    if m.upload_type == "limit":
        return "媒体数量限制"
    if m.kind == "image":
        return "图片" if m.upload_type == "photo" else "图片附件"
    if m.kind == "sticker":
        return "贴纸"
    if m.kind == "audio":
        return "语音" if m.upload_type == "voice" else "音频"
    if m.kind == "video":
        return "视频消息" if m.upload_type == "video_note" else "视频"
    return "文件"


def media_summary_line(m: InboundMedia, index: int) -> str:
    src = "回复消息" if m.source == "reply" else "当前消息"
    name = m.file_name or m.upload_type or "未命名"
    details: list[str] = [f"来源: {src}", f"类型: {_label(m)}"]
    if name:
        details.append(f"文件名: {name}")
    if m.mime_type:
        details.append(f"MIME: {m.mime_type}")
    if m.size:
        details.append(f"大小: {human_size(m.size)}")
    if m.width or m.height:
        details.append(f"尺寸: {m.width}x{m.height}")
    if m.duration:
        details.append(f"时长: {m.duration}s")
    if m.path:
        details.append(f"本地路径: {m.path}")
    if m.error:
        details.append(f"状态: {m.error}")
    elif m.kind == "audio":
        if m.transcript:
            details.append("状态: 已下载并完成音频转写。")
        elif m.transcription_error:
            details.append(f"状态: 已下载；音频转写失败：{m.transcription_error}")
        else:
            details.append("状态: 已下载；当前未启用音频转写，未声称已转写。")
    elif m.kind == "video":
        details.append("状态: 已下载；当前未做视频理解，仅提供文件信息。")
    elif m.kind == "file" and not m.text_excerpt:
        details.append("状态: 未识别为可安全内联的文本附件，仅提供文件信息。")
    return f"{index}. " + "；".join(details)


def build_media_text_summary(media: list[InboundMedia]) -> str:
    if not media:
        return ""
    lines = ["[用户附件]"]
    for i, m in enumerate(media, 1):
        lines.append(media_summary_line(m, i))
        if m.transcript:
            lines.append(f"[音频转写 {i} · 开始]\n{m.transcript}\n[音频转写 {i} · 结束]")
        if m.text_excerpt:
            lines.append(f"[附件文本提取 {i} · 开始]\n{m.text_excerpt}\n[附件文本提取 {i} · 结束]")
    lines.append("[/用户附件]")
    return "\n".join(lines)


def build_user_text_with_media(text: str, media: list[InboundMedia]) -> str:
    base = (text or "").strip()
    if not base and media:
        base = "请根据我发送的附件内容回答。"
    summary = build_media_text_summary(media)
    return f"{base}\n\n{summary}".strip() if summary else base


def build_llm_media_text_summary(media: list[InboundMedia]) -> str:
    """给模型看的附件说明：保留必要语义，不暴露本地路径。"""
    if not media:
        return ""
    lines = ["[用户附件说明]"]
    for i, m in enumerate(media, 1):
        name = m.file_name or f"attachment_{i}"
        if m.kind in {"image", "sticker"} and m.is_image_block:
            lines.append(
                f"{i}. 图片：{name}；MIME: {m.mime_type or 'image/*'}；大小: {human_size(m.size)}。"
                "该图片已经作为多模态视觉输入随本消息提供，请直接阅读图片内容；不要声称缺少 OCR 工具。"
            )
        elif m.kind == "audio" and m.transcript:
            lines.append(f"{i}. 音频：{name}；已转写如下。")
            lines.append(f"[音频转写 {i} · 开始]\n{m.transcript}\n[音频转写 {i} · 结束]")
        elif m.kind == "file" and m.text_excerpt:
            lines.append(f"{i}. 文本附件：{name}；提取文本如下。")
            lines.append(f"[附件文本提取 {i} · 开始]\n{m.text_excerpt}\n[附件文本提取 {i} · 结束]")
        elif m.error:
            lines.append(f"{i}. 附件：{name}；状态：{m.error}")
        else:
            lines.append(f"{i}. 附件：{name}；类型: {_label(m)}；MIME: {m.mime_type or 'unknown'}；大小: {human_size(m.size)}。")
    lines.append("[/用户附件说明]")
    return "\n".join(lines)


def build_llm_text_with_media(text: str, media: list[InboundMedia]) -> str:
    base = (text or "").strip()
    if not base and media:
        base = "请根据我发送的附件内容回答。"
    summary = build_llm_media_text_summary(media)
    return f"{base}\n\n{summary}".strip() if summary else base


def build_llm_content(text: str, media: list[InboundMedia] | None = None) -> str | list[dict[str, Any]]:
    """生成中性用户 content：文本摘要 + 当前可读图片 image block。"""
    media = media or []
    image_media = [m for m in media if m.is_image_block]
    if not image_media:
        return text
    blocks: list[dict[str, Any]] = []
    if text.strip():
        blocks.append({"type": "text", "text": text})
    for idx, m in enumerate(image_media, 1):
        blocks.append({
            "type": "image",
            "path": m.path,
            "mime_type": m.mime_type or guess_mime(m.file_name, "image/jpeg"),
            "name": m.file_name or f"image_{idx}",
        })
    return blocks or text
