"""Exact model-definition accounting and durable MCP exposure approvals."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from typing import Any

from app.config import Config, MCPConfig
from app.mcp.types import MCPToolMeta
from app.tools.confirmation import APPROVAL_ARGUMENT, APPROVAL_DESCRIPTION


def compact(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tool_description(meta: MCPToolMeta, config: MCPConfig) -> str:
    server = config.servers[meta.server_key]
    timeout_s = server.tool_call_timeout_s or config.tool_call_timeout_s
    return (f"{meta.description}\nOpenBear MCP transport deadline: {timeout_s:g} seconds per call. Tool timeout arguments cannot extend this limit; "
            "a transport timeout does not confirm remote cancellation, so check the outcome before retrying.")


def tool_definition(meta: MCPToolMeta, config: MCPConfig) -> dict[str, Any]:
    parameters = copy.deepcopy(meta.input_schema)
    properties = parameters.setdefault("properties", {})
    if APPROVAL_ARGUMENT not in properties:
        properties[APPROVAL_ARGUMENT] = {"type": "string", "description": APPROVAL_DESCRIPTION}
    return {"name": meta.public_name, "description": tool_description(meta, config), "parameters": parameters}


def server_identity(config: MCPConfig, key: str, *, discovery: bool = False) -> str:
    server = config.servers[key].model_dump(mode="json", by_alias=True)
    # Calling policy / Agent access is independent from model-context exposure.
    for field in ("approval", "agentAccess", "required"):
        server.pop(field, None)
    if discovery:
        for field in ("enabled", "disabledTools", "tools"):
            server.pop(field, None)
    return digest({"server": server, "prefix": config.tool_name_prefix,
                   "allow": [] if discovery else config.allow_tools, "deny": [] if discovery else config.deny_tools,
                   "timeout": config.tool_call_timeout_s})


def exposure_summary(config: MCPConfig, key: str, tools: list[MCPToolMeta]) -> dict[str, Any]:
    definitions = []
    for meta in tools:
        definition = tool_definition(meta, config)
        meta.definition_bytes = len(compact(definition))
        if meta.enabled:
            definitions.append(definition)
    definitions.sort(key=lambda row: row["name"])
    size = len(compact(definitions)) if definitions else 0
    count = len(definitions)
    count_limit = config.tool_count_threshold
    size_limit = config.definition_size_threshold_kb * 1024
    return {"server": key, "enabledTools": count, "totalTools": len(tools),
            "definitionBytes": size, "estimatedTokens": (size + 3) // 4,
            "toolCountThreshold": count_limit, "definitionSizeThresholdBytes": size_limit,
            "exceeded": bool((count_limit and count > count_limit) or (size_limit and size > size_limit)),
            "fingerprint": digest({"server": key, "identity": server_identity(config, key, discovery=True), "tools": definitions})}


def warning_text(rows: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"MCP {row['server']}：已启用 {row['enabledTools']} / {row['totalTools']} 个工具，"
        f"定义 {row['definitionBytes']:,} 字节（{row['definitionBytes'] / 1024:.1f} KB）。\n"
        f"数量阈值 {row['toolCountThreshold']}，体积阈值 {row['definitionSizeThresholdBytes'] / 1024:g} KB（0 表示不检查）。\n"
        f"每次请求固定占用约 {row['estimatedTokens']:,} tokens（4 字节约 1 token），可能挤占上下文并导致执行失败。"
        "可以先停用部分工具以降低占用。" for row in rows
    )


class MCPExposureNotAuthorized(RuntimeError):
    def __init__(self, confirmation: dict[str, Any]) -> None:
        self.confirmation = confirmation
        super().__init__("mcp_exposure_not_authorized")


class MCPExposureApprovals:
    """Stored outside editable MCP config. No DB means process-local, fail closed.

    Initialize once at service startup, before any management writes. Legacy
    enabled servers receive a one-time identity-bound discovery allowance; a
    failed connection retains it until the original server can be discovered.
    """
    KEY = "mcp.exposure.approvals.v1"

    def __init__(self, db: Any = None) -> None:
        self.db = db
        self.records: dict[str, str] = {}
        self.legacy: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def initialize(self, config: Config) -> None:
        if self.db is None:
            return
        async with self._lock:
            cur = await self.db.conn.execute("SELECT value FROM app_state WHERE key=?", (self.KEY,))
            row = await cur.fetchone()
            if row:
                data = json.loads(row["value"])
                self.records = data.get("records", {})
                self.legacy = data.get("legacy", {})
            else:
                # Explicit new budget settings mark a fresh configuration, not an upgrade.
                if not {"tool_count_threshold", "definition_size_threshold_kb"} & config.mcp.model_fields_set:
                    self.legacy = {key: server_identity(config.mcp, key) for key, server in config.mcp.servers.items()
                                   if config.mcp.enabled and server.enabled}
                await self._save()

    async def _save(self) -> None:
        if self.db is not None:
            await self.db.conn.execute(
                "INSERT INTO app_state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (self.KEY, json.dumps({"records": self.records, "legacy": self.legacy}), int(time.time())),
            )
            await self.db.conn.commit()

    async def is_approved(self, config: MCPConfig, key: str, fingerprint: str) -> bool:
        async with self._lock:
            if self.records.get(key) == fingerprint:
                return True
            if self.legacy.get(key) == server_identity(config, key):
                self.records[key] = fingerprint
                self.legacy.pop(key, None)
                await self._save()
                return True
            return False

    async def approve(self, rows: list[dict[str, Any]]) -> None:
        async with self._lock:
            for row in rows:
                self.records[row["server"]] = row["fingerprint"]
                self.legacy.pop(row["server"], None)
            await self._save()
