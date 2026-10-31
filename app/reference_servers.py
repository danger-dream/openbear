"""Server references: picker rows and the live facts a reference supplies.

A server reference is identified only by its serverId. Every read goes through
the existing ServerService listing for the authenticated owner, so another
owner's server is indistinguishable from a deleted one. The projection is an
explicit allow-list: no SSH username, key label, password, credential or
access-point detail ever leaves here.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time

SERVER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
USAGE = (
    "可用 Servers 工具按 serverId 直接操作（exec、list_dir、read_file、write_file、job_start 等）；"
    "节点离线但有 SSH 登录时，按 serverId 执行的 exec 会改走 SSH。"
)
MISSING_NOTE = "该服务器已不存在（已删除或无权访问），不要对它执行任何操作。"


class ServersUnavailable(Exception):
    """The 主机 feature is off, or this controller has no server service."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def servers_enabled(service) -> bool:
    if service is None:
        return False
    try:
        return bool(service.servers_enabled())
    except Exception:
        return False


def _ip_literal(value) -> str:
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return ""


async def _mesh_addresses(service, owner: int) -> dict[str, str]:
    """Business addresses of active members in the applied mesh ('' when none)."""
    mesh = getattr(service, "mesh", None)
    load = getattr(mesh, "_load", None)
    if load is None:
        return {}
    try:
        doc = await load(owner, "applied")
    except Exception:
        # A mesh read problem must not hide the server itself.
        return {}
    return {
        str(m.get("serverId")): str(m.get("address") or "")
        for m in (doc or {}).get("members") or []
        if isinstance(m, dict) and not m.get("removing") and m.get("address")
    }


def server_facts(item: dict, mesh: dict[str, str]) -> dict:
    """The compact, credential-free facts for one ServerService.list() item."""
    sid = str(item.get("id") or "")
    profile = item.get("profile") or {}
    node = item.get("node") or {}
    info = node.get("info") or {}
    ssh = item.get("ssh") or {}
    caps = node.get("caps") or {}
    online = bool(item.get("live"))
    node_exec = online and bool(caps.get("exec"))
    has_ssh = bool(item.get("ssh"))
    public_ip = (
        _ip_literal(info.get("ipv4"))
        or _ip_literal(ssh.get("publicIp"))
        or _ip_literal(ssh.get("host"))
        or _ip_literal(info.get("ipv6"))
    )
    ipv6 = _ip_literal(info.get("ipv6"))
    facts = {
        "serverId": sid,
        "name": str(item.get("name") or sid),
        **({"local": True} if item.get("local") else {}),
        "group": str(profile.get("group") or ""),
        "region": str(item.get("region") or ""),
        "platform": str(info.get("platform") or ssh.get("platform") or ("linux" if node else "")),
        "os": str(info.get("os") or ssh.get("os") or ""),
        **({"arch": str(info["arch"])} if info.get("arch") else {}),
        "node": (
            {
                "installed": True,
                "online": online,
                "exec": bool(caps.get("exec")),
                "version": str(node.get("version") or ""),
                **({"lastSeenAtMs": node.get("lastSeenAtMs")} if not online and node.get("lastSeenAtMs") else {}),
            }
            if node
            else {"installed": False, "online": False, "exec": False}
        ),
        "sshLogin": has_ssh,
        **({"sshStatus": str(ssh.get("status"))} if has_ssh and ssh.get("status") else {}),
        "canExec": node_exec or has_ssh,
        "execVia": "node" if node_exec else "ssh" if has_ssh else "",
        "publicIp": public_ip,
        **({"ipv6": ipv6} if ipv6 and ipv6 != public_ip else {}),
        "meshAddress": mesh.get(sid, ""),
    }
    return facts


async def server_index(service, owner: int) -> dict[str, dict]:
    """serverId -> facts for the owner's servers (本机 included)."""
    if not servers_enabled(service):
        raise ServersUnavailable("servers_feature_disabled")
    try:
        listing = await service.list(owner)
    except Exception:
        raise ServersUnavailable("servers_unavailable") from None
    mesh = await _mesh_addresses(service, owner)
    out = {}
    for item in listing.get("items") or []:
        facts = server_facts(item, mesh)
        if facts["serverId"]:
            out[facts["serverId"]] = facts
    return out


def picker_row(facts: dict) -> dict:
    """A picker candidate: identity, display and search fields only."""
    node = facts["node"]
    search = [facts["group"], facts["region"], facts["publicIp"], facts.get("ipv6", ""), facts["meshAddress"],
              facts["platform"], facts["os"], "本机 local" if facts.get("local") else ""]
    return {
        "key": f"server:{facts['serverId']}",
        "kind": "server",
        "id": facts["serverId"],
        "name": facts["serverId"],
        "label": facts["name"],
        "group": facts["group"],
        "region": facts["region"],
        "platform": facts["platform"],
        "os": facts["os"],
        "ip": facts["publicIp"],
        "meshAddress": facts["meshAddress"],
        "local": bool(facts.get("local")),
        "online": node["online"],
        "nodeInstalled": node["installed"],
        "canExec": facts["canExec"],
        "sshLogin": facts["sshLogin"],
        "searchText": " ".join(part for part in search if part),
    }


def server_content(facts: dict | None, server_id: str) -> str:
    if facts is None:
        return json.dumps({"serverId": server_id, "exists": False, "note": MISSING_NOTE}, ensure_ascii=False)
    observed = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())
    return json.dumps({**facts, "exists": True, "observedAt": observed, "usage": USAGE}, ensure_ascii=False)


def server_locator(server_id: str, exists: bool) -> dict:
    locator = {"tool": "Servers", "action": "get", "serverId": server_id}
    if not exists:
        locator["exists"] = False
    return locator
