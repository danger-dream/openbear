"""Media replay becomes file references in two cases only.

Recovery after an explicit message_too_big rejection keeps the newest
media-bearing message (the complete attachment batch) and replaces older
local/inline media. A conversation that turned on "图片只看一轮" replaces media
from earlier rounds. Otherwise normal requests never retire media. Text, tool
batches and opaque assistant state are preserved.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from app.context.window import mark_source, source_of
from app.llm.base import Message

_MEDIA_TYPES = frozenset(
    {
        "image",
        "image_url",
        "input_image",
        "file",
        "input_file",
        "document",
        "audio",
        "input_audio",
    }
)


_IMAGE_TYPES = frozenset({"image", "image_url", "input_image"})


def _is_media(block: Any, types: frozenset[str] = _MEDIA_TYPES) -> bool:
    return isinstance(block, dict) and block.get("type") in types


def _inline_media(block: dict[str, Any]) -> tuple[bytes, str] | None:
    """Only decode typed attachment fields, never text or encrypted reasoning."""
    source = block.get("source")
    raw, mime = "", str(block.get("mime_type") or block.get("mime") or "")
    if isinstance(source, dict) and source.get("type") == "base64":
        raw, mime = source.get("data"), str(source.get("media_type") or mime)
    elif block.get("type") in {"image_url", "input_image"}:
        raw = block.get("image_url") or block.get("url")
        if isinstance(raw, dict):
            raw = raw.get("url")
    elif block.get("type") in {"input_file", "file", "document"}:
        file = block.get("file") if isinstance(block.get("file"), dict) else block
        raw = file.get("file_data")
    elif block.get("type") == "input_audio":
        audio = block.get("input_audio")
        if isinstance(audio, dict):
            raw = audio.get("data")
            mime = {"wav": "audio/wav", "mp3": "audio/mpeg"}.get(audio.get("format"), mime)
    elif block.get("encoding") == "base64":
        raw = block.get("data")
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith("data:"):
        header, sep, raw = raw.partition(",")
        if not sep or not header.endswith(";base64"):
            return None
        mime = header[5:-7]
    elif block.get("type") in {"image_url", "input_image"}:
        return None  # External image URLs are not embedded binary payloads.
    try:
        data = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        return None
    return (data, mime) if data else None


def _media_path(block: dict[str, Any], artifact_dir: Path) -> Path | None:
    path = block.get("path")
    if isinstance(path, str) and path and Path(path).is_file():
        return Path(path)
    inline = _inline_media(block)
    if inline is None:
        return None
    data, mime = inline
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
        "audio/wav": ".wav",
        "audio/mpeg": ".mp3",
    }.get(mime, ".bin")
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = artifact_dir / (hashlib.sha256(data).hexdigest() + extension)
    # Deterministic private storage makes a cancelled/retried preparation safe.
    # A partially written file is never referenced; original inline bytes remain
    # in immutable execution history even if preparation does not commit.
    if not target.is_file() or target.read_bytes() != data:
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as stream:
            temporary.chmod(0o600)
            stream.write(data)
        temporary.replace(target)
    return target


_INPUT_KINDS = frozenset({"human", "task", "cron", "webhook"})


def _media_batches(messages: list[Message], types: frozenset[str] = _MEDIA_TYPES) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if message.get("role") in {"user", "tool"}
        and isinstance(message.get("content"), list)
        and any(_is_media(block, types) for block in message["content"])
    ]


def reference_historical_media(
    messages: list[Message],
    *,
    artifact_dir: Path,
) -> tuple[list[Message], int]:
    """Caller archives originals first and commits the derived view atomically.

    No byte threshold, resizing, summarization, or neutral_context conversion.
    Missing files/unrecognized inline forms remain intact, not invented paths.
    """
    batches = _media_batches(messages)
    if len(batches) < 2:
        return messages, 0
    return _reference_media_at(messages, batches[:-1], artifact_dir=artifact_dir)


def reference_earlier_round_media(
    messages: list[Message],
    *,
    artifact_dir: Path,
    scope_key: str,
    active_scope: str,
    include_input: bool,
) -> tuple[list[Message], int]:
    """图片只看一轮: images from rounds other than active_scope (a run root turn,
    or an Agent's task) become file references; the active round's stay, and
    so do documents and audio.

    include_input also covers images the user or the task sent, not only tool
    images. A batch without a known round is left alone rather than guessed.
    """
    if not active_scope:
        return messages, 0
    batches = []
    for index in _media_batches(messages, _IMAGE_TYPES):
        meta = source_of(messages[index])
        scope = str(meta.get(scope_key) or "")
        if not scope or scope == active_scope:
            continue
        if meta.get("kind") in _INPUT_KINDS and not include_input:
            continue
        batches.append(index)
    return _reference_media_at(messages, batches, artifact_dir=artifact_dir, types=_IMAGE_TYPES)


def _reference_media_at(messages: list[Message], batches: list[int], *, artifact_dir: Path,
                        types: frozenset[str] = _MEDIA_TYPES) -> tuple[list[Message], int]:
    if not batches:
        return messages, 0
    outgoing = copy.deepcopy(messages)
    retired = 0
    for index in batches:
        message = outgoing[index]
        changed = False
        for block_index, block in enumerate(message["content"]):
            if not _is_media(block, types):
                continue
            path = _media_path(block, artifact_dir)
            if path is None:
                continue
            message["content"][block_index] = {
                "type": "text",
                "text": f"[Historical attachment: {block.get('name') or block.get('filename') or path.name}; "
                f"local file: {path}. Original preserved. Binary content is not included in this "
                "request; reload the file explicitly if needed.]",
            }
            retired += 1
            changed = True
        if not changed:
            continue
        meta = source_of(message)
        source_id = str(meta["id"])
        digest = hashlib.sha256(
            json.dumps(message["content"], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        mark_source(
            message,
            kind=str(meta.get("kind") or "execution"),
            source_id=f"{source_id}/media-reference:{digest}",
            derived_from=source_id,
            message_id=0,
            reference_only=False,
        )
    return (outgoing, retired) if retired else (messages, 0)
