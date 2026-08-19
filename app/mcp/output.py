"""MCP result governance: redaction, truncation and artifacts."""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from app.mcp.types import MCPRawResult, MCPToolMeta

_SECRET_KEY_RE = re.compile(r"(?:token|api[_-]?key|apikey|authorization|password|secret|cookie|session)", re.I)
_TEXT_AUTH_BEARER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_TEXT_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)((?:[\"']?\b(?:api[_-]?key|apikey|token|password|secret|cookie|session)\b[\"']?)\s*[:=]\s*)([\"']?)([^\s'\"`;,}]{4,})(\2)"
)
_TEXT_SK_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b")
_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_TEXT_TYPES = {"text", "resource"}
_BINARY_TYPES = {"image", "audio", "blob"}


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(str(key or "")))


def redact_secret_value(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "[REDACTED]"
    return f"[REDACTED:{len(text)} chars]"


def redact_text_secrets(text: str) -> str:
    out = str(text or "")
    out = _TEXT_AUTH_BEARER_RE.sub(lambda m: m.group(1) + "[REDACTED]", out)
    out = _TEXT_KEY_VALUE_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(4)}", out)
    out = _TEXT_SK_RE.sub("[REDACTED]", out)
    return out


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            out[text_key] = redact_secret_value(item) if is_secret_key(text_key) else redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    return value


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {str(k): (redact_secret_value(v) if is_secret_key(str(k)) else str(v)) for k, v in (headers or {}).items()}



def expand_env_refs(value: str) -> str:
    text = str(value or "")

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return os.environ.get(name, "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, text)


def expand_header_env(headers: dict[str, str] | None) -> dict[str, str]:
    return {str(k): expand_env_refs(str(v)) for k, v in (headers or {}).items()}


def _safe_path_component(value: str, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    clean = _SAFE_PATH_RE.sub("_", raw).strip("._")
    return (clean or fallback)[:96]


def _artifact_dir(server_key: str, tool_name: str) -> Path:
    path = (
        Path.cwd()
        / "data"
        / "tool_artifacts"
        / "mcp"
        / _safe_path_component(server_key, "server")
        / _safe_path_component(tool_name, "tool")
    )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _artifact_path(server_key: str, tool_name: str, *, suffix: str, call_id: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return _artifact_dir(server_key, tool_name) / f"{timestamp}-{call_id}{safe_suffix}"


def _write_artifact(
    server_key: str,
    tool_name: str,
    data: str | bytes,
    *,
    suffix: str,
    call_id: str,
    binary: bool = False,
) -> str:
    path = _artifact_path(server_key, tool_name, suffix=suffix, call_id=call_id)
    if binary:
        path.write_bytes(data if isinstance(data, bytes) else str(data).encode("utf-8"))
    else:
        path.write_text(data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def _truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max(1, int(max_chars * 0.65))
    tail = max(1, max_chars - head)
    return text[:head] + f"\n…[truncated {len(text) - max_chars} chars]…\n" + text[-tail:]


def _json_text(value: Any) -> str:
    return json.dumps(redact_secrets(value), ensure_ascii=False, indent=2, default=str)


def _block_to_text(block: Any, meta: MCPToolMeta, call_id: str, block_index: int = 0) -> str:
    if isinstance(block, str):
        return redact_text_secrets(block)
    if not isinstance(block, dict):
        return _json_text(block)
    btype = str(block.get("type") or "text").strip().lower() or "text"
    if btype == "text":
        return redact_text_secrets(str(block.get("text") or ""))
    if btype in _BINARY_TYPES:
        raw_data = str(block.get("data") or "")
        mime = str(block.get("mimeType") or block.get("mime_type") or "application/octet-stream")
        binary = b""
        if raw_data:
            try:
                binary = base64.b64decode(raw_data, validate=False)
            except Exception:
                binary = raw_data.encode("utf-8", "replace")
        suffix = ".bin"
        if btype == "image":
            suffix = ".img"
        elif btype == "audio":
            suffix = ".audio"
        path = _write_artifact(meta.server_key, meta.original_tool_name, binary, suffix=suffix, call_id=f"{call_id}-{block_index}", binary=True)
        return _json_text({
            "type": btype,
            "mimeType": mime,
            "sizeBytes": len(binary),
            "artifactPath": path,
            "note": "binary MCP content saved to artifact; base64 omitted from context",
        })
    if btype == "resource":
        # v0 does not support resources/read.  Keep the block as metadata/text only.
        return _json_text({"type": "resource", "resource": redact_secrets(block.get("resource") or block)})
    return _json_text(block)


def _raw_result_to_text(result: MCPRawResult, meta: MCPToolMeta, call_id: str) -> str:
    sections: list[str] = []
    if result.structured_content is not None:
        sections.append("structuredContent:\n" + _json_text(result.structured_content))
    if result.content:
        parts = [_block_to_text(block, meta, call_id, idx) for idx, block in enumerate(result.content)]
        sections.append("\n".join(part for part in parts if part != ""))
    if not sections and result.raw is not None:
        sections.append(_json_text(result.raw))
    text = "\n\n".join(section for section in sections if section != "")
    if result.is_error:
        text = f"MCP tool returned isError=true\n\n{text}" if text else "MCP tool returned isError=true"
    return text


def govern_text_output(
    text: str,
    meta: MCPToolMeta,
    *,
    inline_max_chars: int,
    output_max_chars: int,
    call_id: str | None = None,
) -> str:
    call = call_id or uuid.uuid4().hex[:10]
    inline_limit = max(1, int(inline_max_chars or 8000))
    output_limit = max(inline_limit, int(output_max_chars or 20000))
    safe_text = redact_text_secrets(str(text or ""))
    if len(safe_text) <= inline_limit:
        return safe_text
    if len(safe_text) <= output_limit:
        preview = _truncate_middle(safe_text, inline_limit)
        return (
            f"{preview}\n\n"
            f"[MCP output truncated for context: original chars={len(safe_text)}, inlineMaxChars={inline_limit}]"
        )
    path = _write_artifact(meta.server_key, meta.original_tool_name, safe_text, suffix=".txt", call_id=call)
    preview = _truncate_middle(safe_text, min(1200, inline_limit))
    return _json_text({
        "status": "artifact_saved",
        "server": meta.server_key,
        "tool": meta.original_tool_name,
        "chars": len(safe_text),
        "artifactPath": path,
        "preview": preview,
    })


def format_mcp_result(result: MCPRawResult, meta: MCPToolMeta, mcp_config: Any) -> str:
    call_id = uuid.uuid4().hex[:10]
    text = _raw_result_to_text(result, meta, call_id)
    # Redact JSON-looking whole outputs as a second pass.  Plain text is not regex
    # scrubbed aggressively to avoid destroying legitimate code snippets; secret
    # keys inside structured MCP blocks are redacted before serialization above.
    return govern_text_output(
        text,
        meta,
        inline_max_chars=int(getattr(mcp_config, "inline_max_chars", 8000) or 8000),
        output_max_chars=int(getattr(mcp_config, "output_max_chars", 20000) or 20000),
        call_id=call_id,
    )
