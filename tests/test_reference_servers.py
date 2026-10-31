# ruff: noqa: F811
# Imported fixtures are registered by pytest and intentionally reused as parameter names.
"""@server references: syntax, live facts for the model, deletion, owner isolation, the 主机 switch."""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.reference_servers import picker_row, server_content, server_facts, server_index
from app.references import (
    BUNDLE_FIELD,
    ReferenceMaterials,
    parse_references,
    reference_display_text,
)
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

PASSWORD = "Pw-Never-Leaves-7731"
USERNAME = "opsuser9931"


def token(identity, label="服务器", mode=""):
    return f"[{label}](openbear://ref/server/{identity}{'?mode=mention' if mode else ''})"


def test_server_kind_parses_by_id_and_rejects_bad_ids():
    refs = parse_references(token("local", "本机") + " " + token("3f2a-uuid_1", "香港") + " " + token("local", "别名"))
    assert [(r["kind"], r["id"], r["label"]) for r in refs] == [("server", "local", "本机"), ("server", "3f2a-uuid_1", "香港")]
    assert refs[0]["key"] == "server:local::full:"
    assert parse_references(token("local", "x", "mention"))[0]["mode"] == "mention"
    for bad in ["a/b", "has space", "x" * 65, "semi;colon", "dot.dot"]:
        assert parse_references(f"[x](openbear://ref/server/{bad})") == [], bad
    assert parse_references("[x](openbear://ref/server/local/extra)") == []
    assert parse_references("`" + token("local") + "`") == []
    assert reference_display_text("看 " + token("local", "本机") + " 状态") == "看 本机 状态"


def _fake_item(**over):
    item = {
        "id": "srv-1", "name": "香港-1", "local": False, "region": "HK",
        "ssh": {"host": "198.51.100.7", "port": 22, "username": USERNAME, "auth": "password", "keyLabel": "my-key",
                "status": "ok", "error": "", "os": "Debian 12", "publicIp": "198.51.100.7", "platform": "linux"},
        "profile": {"group": "生产", "billing": {"price": 9}},
        "node": {"online": True, "version": "1.4.0", "caps": {"exec": True, "terminal": True},
                 "info": {"platform": "linux", "os": "Ubuntu 22.04", "arch": "amd64", "ipv4": "203.0.113.5", "ipv6": "2001:db8::5"},
                 "endpoint": "wss://controller.invalid", "lastSeenAtMs": 1},
        "live": {"online": True, "caps": {"exec": True}},
        "access": {"primary": {"id": "default", "url": "https://controller.invalid/secret-path"}},
    }
    item.update(over)
    return item


def test_facts_are_compact_complete_and_credential_free():
    facts = server_facts(_fake_item(), {"srv-1": "100.96.0.3"})
    assert facts == {
        "serverId": "srv-1", "name": "香港-1", "group": "生产", "region": "HK", "platform": "linux", "os": "Ubuntu 22.04", "arch": "amd64",
        "node": {"installed": True, "online": True, "exec": True, "version": "1.4.0"},
        "sshLogin": True, "sshStatus": "ok", "canExec": True, "execVia": "node",
        "publicIp": "203.0.113.5", "ipv6": "2001:db8::5", "meshAddress": "100.96.0.3",
    }
    content = json.loads(server_content(facts, "srv-1"))
    assert content["exists"] is True and "Servers" in content["usage"] and "serverId" in content["usage"] and content["observedAt"]
    dumped = json.dumps([facts, content, picker_row(facts)], ensure_ascii=False)
    for secret in (USERNAME, "my-key", "password", "controller.invalid", "billing", "secret-path"):
        assert secret not in dumped, secret

    offline = server_facts(_fake_item(live=None), {})
    assert offline["node"]["online"] is False and offline["node"]["lastSeenAtMs"] == 1
    assert (offline["canExec"], offline["execVia"], offline["meshAddress"]) == (True, "ssh", "")
    bare = server_facts(_fake_item(live=None, ssh=None, node=None), {})
    assert bare["node"] == {"installed": False, "online": False, "exec": False}
    assert (bare["canExec"], bare["execVia"], bare["sshLogin"], bare["publicIp"]) == (False, "", False, "")
    refused = server_facts(_fake_item(ssh=None, live={"online": True}, node={**_fake_item()["node"], "caps": {"exec": False}}), {})
    assert (refused["canExec"], refused["node"]["exec"]) == (False, False)
    missing = json.loads(server_content(None, "gone"))
    assert missing == {"serverId": "gone", "exists": False, "note": missing["note"]} and "不存在" in missing["note"]


async def test_mesh_addresses_come_from_the_applied_mesh_only():
    class Mesh:
        async def _load(self, owner, stage):
            assert (owner, stage) == (7, "applied")
            return {"members": [{"serverId": "a", "address": "100.96.0.2"}, {"serverId": "b", "address": "100.96.0.3", "removing": True}]}

    class Service:
        mesh = Mesh()

        def servers_enabled(self):
            return True

        async def list(self, owner):
            return {"items": [_fake_item(id="a"), _fake_item(id="b")]}

    index = await server_index(Service(), 7)
    assert index["a"]["meshAddress"] == "100.96.0.2" and index["b"]["meshAddress"] == ""


async def _ssh_server(e, monkeypatch, name="SSH 机"):
    from app.instances import servers as registry

    async def fake_ssh(db, target, command, *, stdin=b"", timeout=120):
        return {"ok": True, "exitCode": 0, "timedOut": False, "stdout": "os=Debian 12\nsystem=Linux\ndocker=\nip=198.51.100.7\n",
                "stderr": "", "knownHosts": "pinned"}
    monkeypatch.setattr(registry, "_ssh", fake_ssh)
    data = {"name": name, "profile": {"group": "生产"},
            "ssh": {"host": "198.51.100.7", "port": 22, "username": USERNAME, "auth": "password", "password": PASSWORD}}
    return (await api(e, "POST", "/api/servers", data))["item"]["id"]


async def _conversation(e, owner=123):
    return await e.server._create_web_conversation(owner, title="server refs", model="openai/gpt")


async def test_picker_rows_and_materials_reach_the_model_without_credentials(env, monkeypatch):
    e = env
    ssh_id = await _ssh_server(e, monkeypatch)
    node_id = (await api(e, "POST", "/api/servers", {"name": "东京-节点", "profile": {"group": "边缘"}}))["item"]["id"]
    code = (await api(e, "POST", f"/api/servers/{node_id}/enrollment"))["code"]
    key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    assert (await e.app.post("/api/server-nodes/v1/enroll", json={"code": code, "publicKey": base64.b64encode(key).decode()})).status == 200

    listed = await api(e, "GET", "/api/references/servers")
    assert listed["enabled"] is True
    rows = {r["id"]: r for r in listed["items"]}
    assert {"local", ssh_id, node_id} <= set(rows)
    assert rows["local"]["local"] is True and rows["local"]["kind"] == "server" and rows["local"]["key"] == "server:local"
    assert rows[ssh_id]["label"] == "SSH 机" and rows[ssh_id]["group"] == "生产" and rows[ssh_id]["ip"] == "198.51.100.7"
    assert rows[ssh_id]["sshLogin"] is True and rows[ssh_id]["canExec"] is True and rows[ssh_id]["online"] is False
    assert rows[node_id]["nodeInstalled"] is True and rows[node_id]["online"] is False and rows[node_id]["group"] == "边缘"
    assert "198.51.100.7" in rows[ssh_id]["searchText"] and "生产" in rows[ssh_id]["searchText"]

    row = await _conversation(e)
    text = "在这两台装 htop：" + token(ssh_id, "SSH 机") + " " + token(node_id, "东京") + " 和 " + token("local", "本机")
    bundle_id, bindings = await e.server._prepare_reference_bundle(row, text, "msg:servers")
    assert bundle_id and [b["id"] for b in bindings] == [ssh_id, node_id, "local"]
    overlaid = await e.server._reference_store().overlay(
        [{"role": "user", "content": text, BUNDLE_FIELD: [bundle_id]}], conversation_uuid=row["conversation_uuid"])
    block = overlaid[0]["content"]
    materials = json.loads(block.rsplit("）：\n", 1)[1])
    facts = {m["reference"]["id"]: json.loads(m["content"]) for m in materials}
    assert facts[ssh_id]["sshLogin"] is True and facts[ssh_id]["execVia"] == "ssh" and facts[ssh_id]["publicIp"] == "198.51.100.7"
    assert facts[ssh_id]["platform"] == "linux" and facts[ssh_id]["os"] == "Debian 12" and facts[ssh_id]["group"] == "生产"
    assert facts[node_id]["node"]["installed"] is True and facts[node_id]["node"]["online"] is False and facts[node_id]["canExec"] is False
    assert facts["local"]["local"] is True and facts["local"]["name"]
    assert all(f["exists"] is True and "serverId" in f["usage"] for f in facts.values())
    serialized = json.dumps({"listed": listed, "block": block, "bindings": bindings}, ensure_ascii=False)
    assert PASSWORD not in serialized and USERNAME not in serialized and "knownHosts" not in serialized and "pinned" not in serialized
    cursor = await e.db.conn.execute("SELECT manifest_json,material_json,protected_material FROM web_reference_bundles WHERE bundle_uuid=?", (bundle_id,))
    stored = dict(await cursor.fetchone())
    assert stored["protected_material"] == "" and PASSWORD not in json.dumps(stored) and USERNAME not in json.dumps(stored)

    preview = await api(e, "POST", "/api/references/preview", {"text": token(ssh_id, "SSH 机", "mention")})
    item = preview["items"][0]
    assert item["mode"] == "mention" and item["locator"] == {"tool": "Servers", "action": "get", "serverId": ssh_id} and item["exists"] is True


async def test_deleted_and_renamed_servers_keep_old_messages_readable(env):
    e = env
    server_id = (await api(e, "POST", "/api/servers", {"name": "旧名"}))["item"]["id"]
    row = await _conversation(e)
    text = "检查 " + token(server_id, "旧名")
    bundle_id, _ = await e.server._prepare_reference_bundle(row, text, "msg:old")

    await api(e, "PATCH", f"/api/servers/{server_id}", {"name": "新名"})
    renamed = await api(e, "POST", "/api/references/inspect", {"text": token(server_id, "旧名")})
    assert renamed["item"]["sourceLabel"] == "新名" and json.loads(renamed["content"])["name"] == "新名"

    await api(e, "DELETE", f"/api/servers/{server_id}")
    # The frozen message still opens as it was sent; a live read says it is gone, never an error.
    frozen = await api(e, "POST", "/api/references/inspect", {"text": token(server_id, "旧名"), "bundleId": bundle_id})
    assert frozen["frozen"] is True and json.loads(frozen["content"])["name"] == "旧名"
    live = await api(e, "POST", "/api/references/inspect", {"text": token(server_id, "旧名")})
    assert live["item"]["exists"] is False and live["item"]["sourceLabel"] == "旧名"
    assert json.loads(live["content"])["exists"] is False
    preview = await api(e, "POST", "/api/references/preview", {"text": text})
    assert preview["items"][0]["exists"] is False
    resent, _ = await e.server._prepare_reference_bundle(row, text, "msg:resend")
    assert resent
    mention = await api(e, "POST", "/api/references/preview", {"text": token(server_id, "旧名", "mention")})
    assert mention["items"][0]["locator"]["exists"] is False
    assert server_id not in {r["id"] for r in (await api(e, "GET", "/api/references/servers"))["items"]}


async def test_only_the_owners_own_servers_resolve(env):
    e = env
    foreign = (await e.servers.create(456, {"name": "别人的机器", "profile": {"group": "隐私组"}}))["item"]["id"]
    mine = (await api(e, "POST", "/api/servers", {"name": "我的机器"}))["item"]["id"]
    rows = {r["id"] for r in (await api(e, "GET", "/api/references/servers"))["items"]}
    assert mine in rows and foreign not in rows
    preview = await api(e, "POST", "/api/references/preview", {"text": token(foreign, "猜的名字")})
    item = preview["items"][0]
    assert item["exists"] is False and item["sourceLabel"] == "猜的名字"
    inspected = await api(e, "POST", "/api/references/inspect", {"text": token(foreign, "猜的名字")})
    assert "别人的机器" not in json.dumps(inspected, ensure_ascii=False) and "隐私组" not in json.dumps(inspected, ensure_ascii=False)
    # The owner sees it as theirs through the same store.
    store = ReferenceMaterials(e.db, servers=e.servers)
    theirs = await store.resolve(token(foreign, "x"), owner=456)
    assert theirs["manifest"][0]["exists"] is True and json.loads(theirs["materials"][0]["content"])["group"] == "隐私组"


async def test_the_servers_switch_off_hides_and_refuses_the_kind(env):
    e = env
    e.server.config.features.servers = False
    assert await api(e, "GET", "/api/references/servers") == {"ok": True, "enabled": False, "items": []}
    response = await e.app.post("/api/references/preview", json={"text": token("local", "本机")}, cookies=e.cookies, headers=e.origin)
    assert response.status == 422 and (await response.json())["error"] == "servers_feature_disabled"
    row = await _conversation(e)
    from app.references import ReferenceError
    with pytest.raises(ReferenceError) as raised:
        await e.server._prepare_reference_bundle(row, token("local", "本机"), "msg:off")
    assert raised.value.code == "servers_feature_disabled"
    # Without a server service at all (e.g. managed mode) the kind is not offered either.
    e.server.config.features.servers = True
    e.server.servers = None
    assert (await api(e, "GET", "/api/references/servers"))["enabled"] is False
