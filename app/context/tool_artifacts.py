"""Lossless file references for oversized tool bodies at compression boundaries."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from app.context.window import (
    PINNED_KINDS,
    _decision_exchange,
    mark_source,
    protocol_groups,
    source_of,
)
from app.llm.base import Message
from app.utils import estimate_tokens
from app.tools.agent_actions import canonical_tool

# These conclusions/decisions are protected parent inputs, not ordinary evidence.
# Keep their existing preflight/paging contract rather than hiding it behind Read.
_PROTECTED_TOOLS = frozenset({
    "Agent", "AgentContinue", "AgentMessage", "AgentWait", "AgentPlanDecision", "AgentControlAck",
})
_PREVIEW_CHARS = 2400
_READ_CHUNK_CHARS = 2000


def _body_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
    return None


def _write_original(directory: Path, data: bytes, extension: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (hashlib.sha256(data).hexdigest() + extension)
    if path.is_file() and path.read_bytes() == data:
        return path
    # Concurrent preparations never expose a partial artifact or share a temp file.
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".tool-result-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            temporary.chmod(0o600)
            stream.write(data)
            stream.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def externalize_tool_results(
    messages: list[Message], *, artifact_dir: Path, threshold_tokens: int,
) -> tuple[list[Message], dict[str, int]]:
    """Archive originals first; call only when runtime actually starts compression.

    Latest/unseen results are eligible. Never mutate caller objects or tool calls.
    Structured content is saved as lossless JSON; non-text blocks stay in context.
    """
    detail = {"externalizedToolResults": 0, "toolResultTokensSaved": 0}
    if threshold_tokens <= 0:
        return messages, detail
    outgoing = copy.deepcopy(messages)
    for start, end in protocol_groups(messages):
        group = messages[start:end]
        calls = {str(call.get("id") if isinstance(call, dict) else call.id): call
                 for call in group[0].get("tool_calls") or []}
        for index in range(start + 1, end):
            original = messages[index]
            if original.get("role") != "tool":
                continue
            meta = source_of(original)
            call = calls.get(str(original.get("tool_call_id")))
            if (call is None or not meta.get("id") or meta.get("tool_result_artifact")
                    or meta.get("kind") in PINNED_KINDS | {"runtime", "required_runtime"}):
                continue
            name = canonical_tool(str(call.get("name") if isinstance(call, dict) else call.name),
                                  call.get("arguments") if isinstance(call, dict) else call.arguments)
            if name in _PROTECTED_TOOLS or _decision_exchange(call, group):
                continue
            content = original.get("content")
            text = _body_text(content)
            if not text or estimate_tokens(text) <= threshold_tokens:
                continue
            tokens = estimate_tokens(text)
            structured = not isinstance(content, str)
            stored = json.dumps(content, ensure_ascii=False, separators=(",", ":")) if structured else text
            data = stored.encode("utf-8")
            path = artifact_dir.resolve() / (hashlib.sha256(data).hexdigest() + (".json" if structured else ".txt"))
            # Read paginates by line; long lines can exceed its byte limit or
            # result budget. Keep raw bytes AND a lossless paginatable view.
            read_data = None
            read_path = path
            if any(len(line) > _READ_CHUNK_CHARS for line in stored.splitlines()):
                read_data = ("\n".join(json.dumps(stored[offset:offset + _READ_CHUNK_CHARS], ensure_ascii=False)
                                       for offset in range(0, len(stored), _READ_CHUNK_CHARS)) + "\n").encode()
                read_path = path.parent / (hashlib.sha256(read_data).hexdigest() + ".jsonl")
            half = _PREVIEW_CHARS // 2
            preview = (
                f"[Tool result saved to file: tool={name}; original size={len(text)} characters / {tokens} estimated tokens.\n"
                f"Full original: {path}" + (" (JSON content blocks)" if structured else " (UTF-8 text)") + "\n"
                + (f"Read-safe view: {read_path} (JSON string per line; decode and concatenate in order to recover the exact original).\n"
                   if read_data is not None else "")
                + "Use Read with path, offset and limit (e.g. 4 lines) to read the full result in segments. "
                "This is a preview, not the complete result; read the file before relying on omitted details.]\n"
                f"--- head preview ---\n{text[:half]}\n"
                f"--- omitted middle ---\n--- tail preview ---\n{text[-half:]}"
            )
            saved_tokens = tokens - estimate_tokens(preview)
            if saved_tokens <= 0:
                continue  # A very low user threshold must not expand the request.
            _write_original(path.parent, data, path.suffix)
            if read_data is not None:
                _write_original(read_path.parent, read_data, read_path.suffix)
            message = outgoing[index]
            message["content"] = ([{"type": "text", "text": preview},
                                   *[block for block in content if not isinstance(block, dict) or block.get("type") != "text"]]
                                  if structured else preview)
            source_id = str(meta["id"])
            digest = hashlib.sha256(preview.encode()).hexdigest()
            mark_source(message, kind=str(meta.get("kind") or "execution"),
                        source_id=f"{source_id}/tool-artifact:{digest}", derived_from=source_id,
                        message_id=0, reference_only=False, tool_result_artifact=str(path),
                        tool_result_read_path=str(read_path))
            detail["externalizedToolResults"] += 1
            detail["toolResultTokensSaved"] += saved_tokens
    return (outgoing, detail) if detail["externalizedToolResults"] else (messages, detail)
