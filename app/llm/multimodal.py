"""中性多模态 content block 转各 LLM 协议原生入参。

OpenBear 内部只在“当前请求内存态”保留 image path/mime；历史 DB 只落可读文本摘要。
因此这里在构造上游 payload 的最后一刻读取图片文件并 base64 编码，不把 base64 落库。
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

Content = str | list[dict[str, Any]]

_SUPPORTED_ANTHROPIC_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _norm_mime(mime: str | None, path: str | None = None) -> str:
    raw = (mime or "").strip().lower()
    if raw in {"image/jpg", "image/pjpeg"}:
        return "image/jpeg"
    if raw:
        return raw
    if path:
        guessed, _enc = mimetypes.guess_type(path)
        if guessed:
            return guessed.lower()
    return "application/octet-stream"


def _image_data_url(block: dict[str, Any]) -> str:
    path = str(block.get("path") or "")
    if not path:
        raise ValueError("image block 缺少 path")
    p = Path(path)
    data = p.read_bytes()
    mime = _norm_mime(str(block.get("mime_type") or block.get("mime") or ""), str(p))
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _image_base64_source(block: dict[str, Any]) -> dict[str, str]:
    path = str(block.get("path") or "")
    if not path:
        raise ValueError("image block 缺少 path")
    p = Path(path)
    mime = _norm_mime(str(block.get("mime_type") or block.get("mime") or ""), str(p))
    if mime not in _SUPPORTED_ANTHROPIC_IMAGE_MIMES:
        raise ValueError(f"Anthropic 不支持的图片 MIME: {mime}")
    encoded = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"type": "base64", "media_type": mime, "data": encoded}


def _fallback_text_for_failed_image(block: dict[str, Any], exc: Exception) -> dict[str, str]:
    name = str(block.get("name") or block.get("path") or "图片")
    mime = str(block.get("mime_type") or block.get("mime") or "")
    detail = f"，MIME: {mime}" if mime else ""
    return {"type": "text", "text": f"[图片无法作为多模态输入读取：{name}{detail}；错误：{type(exc).__name__}]"}


def text_from_content(content: Any) -> str:
    """把中性 content 安全降级为文本（用于不支持 list 的历史/边界场景）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").lower()
        if btype == "text":
            text = str(block.get("text") or "")
            if text:
                parts.append(text)
        elif btype == "image":
            name = str(block.get("name") or block.get("path") or "图片")
            mime = str(block.get("mime_type") or block.get("mime") or "")
            parts.append(f"[图片: {name}{', ' + mime if mime else ''}]")
    return "\n\n".join(p for p in parts if p)


def to_openai_chat_content(content: Any) -> str | list[dict[str, Any]]:
    """中性 content → OpenAI Chat message.content。"""
    if not isinstance(content, list):
        return "" if content is None else str(content)
    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").lower()
        if btype == "text":
            text = str(block.get("text") or "")
            if text:
                out.append({"type": "text", "text": text})
        elif btype == "image":
            try:
                out.append({"type": "image_url", "image_url": {"url": _image_data_url(block)}})
            except Exception as exc:
                out.append(_fallback_text_for_failed_image(block, exc))
    return out or text_from_content(content)


def to_openai_responses_content(content: Any, *, output: bool = False) -> list[dict[str, Any]]:
    """中性 content → OpenAI Responses message.content。"""
    text_type = "output_text" if output else "input_text"
    if not isinstance(content, list):
        return [{"type": text_type, "text": "" if content is None else str(content)}]
    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").lower()
        if btype == "text":
            text = str(block.get("text") or "")
            if text:
                out.append({"type": text_type, "text": text})
        elif btype == "image" and not output:
            try:
                out.append({"type": "input_image", "image_url": _image_data_url(block)})
            except Exception as exc:
                fallback = _fallback_text_for_failed_image(block, exc)
                out.append({"type": text_type, "text": fallback["text"]})
    return out or [{"type": text_type, "text": text_from_content(content)}]


def to_anthropic_content(content: Any) -> str | list[dict[str, Any]]:
    """中性 content → Anthropic user/assistant content。

    仅 user 多模态会走到 image block；assistant 历史通常是字符串。
    """
    if not isinstance(content, list):
        return "" if content is None else str(content)
    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").lower()
        if btype == "text":
            text = str(block.get("text") or "")
            if text:
                out.append({"type": "text", "text": text})
        elif btype == "image":
            try:
                out.append({"type": "image", "source": _image_base64_source(block)})
            except Exception as exc:
                out.append(_fallback_text_for_failed_image(block, exc))
    return out or text_from_content(content)
