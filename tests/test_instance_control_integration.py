"""Controller relay and verification against a real managed HTTP runtime."""
from __future__ import annotations

import asyncio
import json
import secrets
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from app.config import Config, load_config
from app.services import Services
from tests.test_instances import fake_gateway
from tests.test_managed_runtime import seed
from tests.test_web_admin import FakeBot, _login_cookie


@pytest.fixture
async def linked_instances(monkeypatch, tmp_path):
    parent_root = tmp_path / "controller"
    parent_root.mkdir()
    monkeypatch.chdir(parent_root)
    monkeypatch.delenv("OPENBEAR_INSTANCE_ROLE", raising=False)
    raw = {"telegram": {"botToken": secrets.token_urlsafe(24), "whitelistIds": [123]},
           "models": {"providers": {}, "primary": ""}, "memory": {"provider": "builtin"}}
    parent_config = parent_root / "openbear.json"
    parent_config.write_text(json.dumps(raw))
    monkeypatch.setenv("OPENBEAR_CONFIG", str(parent_config))
    parent = Services(Config.model_validate(raw), FakeBot())
    parent_client = child_client = child = None
    g, gateway_app = fake_gateway()
    gateway_server = TestServer(gateway_app)
    await gateway_server.start_server()
    g.url, g.public = str(gateway_server.make_url("")).rstrip("/"), "http://203.0.113.10:18962"
    try:
        await parent.db.connect()
        await parent.instances.start()
        await parent.instances.gateway.store({"enabled": True, "publicUrl": g.public, "serverId": "local", "port": 18962, "status": "connected",
                                               # Never reinstall a real gateway on this host from a test.
                                               "autoUpdate": False},
                                              {"controlKey": g.key, "controlUrl": g.url})
        parent_client = TestClient(TestServer(parent.web_admin.make_app()))
        await parent_client.start_server()
        root, values, _ = seed(monkeypatch, tmp_path)
        values.pop("OPENBEAR_USERNAME")
        values.pop("OPENBEAR_PASSWORD")
        monkeypatch.delenv("OPENBEAR_USERNAME")
        monkeypatch.delenv("OPENBEAR_PASSWORD")
        # The real child must exist first, so its address can be given to the controller.
        values.update(OPENBEAR_CHILD_AUTH="gateway", OPENBEAR_GATEWAY_KEY=secrets.token_urlsafe(32))
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        child = Services(load_config())
        await child.db.connect()
        await child.mem._bootstrap()
        await child.managed_runtime.open()
        child.managed_runtime.ready = True
        child_client = TestClient(TestServer(child.web_admin.make_app()))
        await child_client.start_server()
        child_url = str(child_client.make_url("/")).rstrip("/")
        form = {"name": "linked fixture", "serverId": "local", "managementUrl": child_url, "workspacePath": "/tmp/fixture-volume",
                "account": {"username": "owner", "password": secrets.token_urlsafe(24)}, "models": {"mode": "later"},
                "notifications": {"loginSuccess": False, "loginFailure": False, "health": False}}
        item = (await parent.instances.save(123, form, adopt={"id": values["OPENBEAR_INSTANCE_ID"],
                "managementKey": values["OPENBEAR_MANAGEMENT_KEY"], "businessKey": values["OPENBEAR_GATEWAY_KEY"]}))["item"]
        cookie = {"openbear_web_session": await _login_cookie(SimpleNamespace(server=parent.web_admin, client=parent_client))}
        yield SimpleNamespace(parent=parent, parent_client=parent_client, child=child, child_client=child_client,
                              item=item, form=form, values=values, root=root, cookie=cookie, gateway=g,
                              base="/api/instances/" + item["id"])
    finally:
        if child_client:
            await child_client.close()
        if child:
            await child.shutdown()
        if parent_client:
            await parent_client.close()
        await parent.shutdown()
        await gateway_server.close()


async def test_relay_reaches_real_child_and_preserves_role_boundary(linked_instances):
    e = linked_instances

    async def request(method, path, body=None):
        response = await e.parent_client.post(e.base + "/request", cookies=e.cookie,
                                             json={"method": method, "path": path, "body": body})
        assert response.status == 200, await response.text()
        return await response.json()
    status = await request("GET", "/api/managed/v1/status")
    assert status["status"] == 200
    assert status["data"]["instanceId"] == e.item["id"]
    assert status["data"]["persistence"]["mounted"] is False
    assert "InstanceControl" in e.parent.tools.names()
    assert "InstanceControl" not in e.child.tools.names()
    installed = await request("PUT", "/api/managed/v1/files/skills/linked/SKILL.md",
                              {"content": "---\nname: linked\ndescription: Integration fixture\n---\nUse this isolated fixture.\n"})
    assert installed["status"] == 200
    listing = await request("GET", "/api/skills")
    assert any(item["name"] == "linked" for item in listing["data"]["items"])
    job = await request("POST", "/api/managed/v1/jobs", {"command": "printf 'integration-fixture\\n'", "timeoutSeconds": 5})
    assert job["status"] == 202
    job_id = job["data"]["job"]["id"]
    for _ in range(50):
        result = await request("GET", "/api/managed/v1/jobs/" + job_id)
        if result["data"]["job"]["status"] not in {"pending", "queued", "running"}:
            break
        await asyncio.sleep(0.02)
    assert "integration-fixture" in json.dumps(result["data"]["job"])
    forbidden = await e.parent_client.post(e.base + "/request", cookies=e.cookie,
                                          json={"method": "GET", "path": "/api/instances"})
    assert forbidden.status == 403
    listing = await e.parent_client.get("/api/instances", cookies=e.cookie)
    rendered = json.dumps(await listing.json())
    for secret in (e.form["account"]["password"], e.values["OPENBEAR_MANAGEMENT_KEY"], e.values["OPENBEAR_GATEWAY_KEY"]):
        assert secret not in rendered


async def test_real_unmounted_child_is_not_verified_ready(linked_instances):
    e = linked_instances
    response = await e.parent_client.post(e.base + "/verify", cookies=e.cookie, json={})
    assert response.status == 200
    value = await response.json()
    assert value["verification"]["identity"] is True
    assert value["verification"]["persistence"] is False
    assert value["verification"]["ok"] is False
    assert value["item"]["status"] != "ready"


async def test_controller_page_carries_login_mode(linked_instances, tmp_path, monkeypatch):
    e = linked_instances
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<script>window.__OPENBEAR_AUTH_MODE__ = "[[controller]]";</script>')
    for server in (e.parent.web_admin, e.child.web_admin):
        monkeypatch.setattr(server, "_web_dist_dir", lambda: dist)
    parent_page = await (await e.parent_client.get("/chat")).text()
    assert '"[[controller]]"' not in parent_page and '"controller"' in parent_page
    child_page = await e.child.web_admin.handle_index(make_mocked_request("GET", "/chat"))
    assert '"gateway"' in child_page.text
