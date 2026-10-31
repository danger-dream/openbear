"""Optional controller features: 实例 (InstanceControl) and 主机 (Servers).

The switches live in openbear.json under ``features`` and are edited through the
ordinary settings API. Turning one off hides its menu and keeps its tool (and
the tool's long description) away from the model; the services behind it keep
running either way.

A missing switch is decided once, at startup: on when this install already has
that feature's data, off otherwise. The decision is written back, so later data
changes never flip it again.
"""
from __future__ import annotations

from typing import Any

from app.logging import get_logger

log = get_logger("features")

FEATURES = ("instances", "servers")
# Rows that show a feature was already in use before the switch existed.
_EVIDENCE_TABLES = {
    "instances": ("managed_instances",),
    "servers": ("instance_servers", "server_nodes"),
}


def undecided(raw: dict[str, Any]) -> list[str]:
    section = raw.get("features")
    section = section if isinstance(section, dict) else {}
    return [name for name in FEATURES if not isinstance(section.get(name), bool)]


async def _has_rows(conn, table: str) -> bool:
    cur = await conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,))
    if await cur.fetchone() is None:
        return False
    cur = await conn.execute(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608 - fixed table names
    return await cur.fetchone() is not None


async def initial_decision(conn) -> dict[str, bool]:
    decision = {}
    for name, tables in _EVIDENCE_TABLES.items():
        found = False
        for table in tables:
            if await _has_rows(conn, table):
                found = True
                break
        decision[name] = found
    return decision


async def migrate(config_store, conn):
    """Write the one-time decision for undecided switches; returns the new Config or None."""
    if not undecided(await config_store.load_raw()):
        return None
    decision = await initial_decision(conn)

    def apply(raw: dict[str, Any]) -> None:
        section = raw.get("features")
        if not isinstance(section, dict):
            section = raw["features"] = {}
        for name in FEATURES:
            if not isinstance(section.get(name), bool):
                section[name] = decision[name]

    config = await config_store.mutate(apply)
    log.info("可选功能开关已初始化", 实例=config.features.instances, 主机=config.features.servers)
    return config
