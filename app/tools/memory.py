"""记忆工具 —— Memory。

转发到 prompt-memory 的 /tool/* 接口。自用单人，secret 正常返回明文，不脱敏。
"""
from __future__ import annotations

import json
from typing import Any

from app.assistant import changes
from app.memory.client import MemoryClient
from app.tools.base import ToolRegistry

_CHANGE_KINDS = {"entry": "mem", "doc": "doc", "secret": "secret"}

_RESOURCE_TO_ENDPOINT = {
    "identity": "identities",
    "identities": "identities",
    "secret": "secret",
    "entry": "entry",
    "doc": "doc",
}


def _fmt(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def register_memory_tools(reg: ToolRegistry, mem: MemoryClient) -> None:
    async def _memory(args: dict[str, Any]) -> str:
        resource = str(args.get("resource") or args.get("kind") or "").strip().lower()
        if not resource:
            return "error: missing resource"
        endpoint = _RESOURCE_TO_ENDPOINT.get(resource)
        if endpoint is None:
            return f"error: unknown Memory resource: {resource}"
        action = str(args.get("action") or "").strip().lower()
        if not action:
            return "error: missing action"
        if endpoint == "identities" and action != "list":
            return "error: Memory resource identity only supports action=list"
        payload = {k: v for k, v in args.items() if k not in {"resource", "kind"}}
        result = await mem.tool_call(endpoint, payload)
        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        if result.get("ok") and endpoint == "secret" and action == "get" and item.get("id") and hasattr(mem, "touch_secret"):
            await mem.touch_secret(int(item["id"]))
        if result.get("ok") and endpoint in _CHANGE_KINDS and action in {"set", "del"}:
            ident = item.get("ref") or item.get("name") or item.get("id") or result.get("id") or args.get("ref") or args.get("name") or args.get("id")
            await changes.publish(_CHANGE_KINDS[endpoint], ident, "delete" if action == "del" else "update",
                                  itemId=str(item.get("id") or result.get("id") or ""))
        return _fmt(result)

    reg.add(
        "Memory",
        "Read and write OpenBear memory. resource=identity|secret|entry|doc. identity supports list. secret/entry/doc support list|get|set|del using the same fields as the memory API. "
        "A secret has comma-separated tags (set replaces them); secret list returns tags and last_used_at (unix seconds when the model last read its values, 0 = no record) and keeps only secrets carrying every tag given in tags.",
        {"type": "object", "properties": {
            "resource": {"type": "string", "enum": ["identity", "secret", "entry", "doc"], "description": "Memory resource"},
            "action": {"type": "string", "description": "list|get|set|del"},
            "name": {"type": "string"},
            "ref": {"type": "string"},
            "category": {"type": "string"},
            "body": {"type": "string"},
            "expanded": {"type": "boolean", "description": "For entry set: expose the full memory body through memory.expandedEntries on every prompt render"},
            "fieldsJson": {"type": "string"},
            "id": {"type": "integer"},
            "content": {"type": "string"},
            "summary": {"type": "string"},
            "project": {"type": "string"},
            "tags": {"type": "string"},
            "kvJson": {"type": "string"},
            "note": {"type": "string"},
            "availableTo": {"type": "string"},
        }, "required": ["resource", "action"]},
        _memory,
    )
