"""实例 / 主机 switches: one-time defaults, settings read/write, tool exposure."""
from __future__ import annotations

import json
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import features
from app.admin import settings as settings_admin
from app.config import Config
from app.config_store import ConfigStore
from app.db.engine import DB
from app.instances import schema as instance_schema
from app.instances.servers import SCHEMA as INSTANCE_SERVERS_SCHEMA
from app.servers import schema as server_schema
from app.services import Services


def _raw() -> dict:
    return {"telegram": {"botToken": secrets.token_urlsafe(24), "whitelistIds": [123]},
            "models": {"providers": {"p": {"baseUrl": "http://p.local/v1", "apiKey": "k", "protocol": "chat",
                                           "models": [{"id": "m", "name": "M"}]}},
                       "primary": "p/m"},
            "memory": {}}


@pytest.fixture
async def store_db(tmp_path):
    path = tmp_path / "openbear.json"
    path.write_text(json.dumps(_raw()), encoding="utf-8")
    db = DB(str(tmp_path / "t.db"))
    await db.connect()
    try:
        yield SimpleNamespace(store=ConfigStore(path), db=db, path=path)
    finally:
        await db.close()


async def _feature_tables(db):
    await instance_schema.migrate(db)
    await server_schema.migrate(db)
    async with db.write_transaction(label="test") as conn:
        await conn.execute(INSTANCE_SERVERS_SCHEMA)


def _stored(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")).get("features")


async def test_new_install_defaults_both_off(store_db):
    # Fresh install: the module tables do not even exist yet.
    config = await features.migrate(store_db.store, store_db.db.conn)
    assert _stored(store_db.path) == {"instances": False, "servers": False}
    assert not config.features.enabled("instances") and not config.features.enabled("servers")


async def test_empty_tables_also_default_off(store_db):
    await _feature_tables(store_db.db)
    await features.migrate(store_db.store, store_db.db.conn)
    assert _stored(store_db.path) == {"instances": False, "servers": False}


@pytest.mark.parametrize("table", ["instance_servers", "server_nodes"])
async def test_existing_servers_keep_servers_on(store_db, table):
    await _feature_tables(store_db.db)
    async with store_db.db.write_transaction(label="test") as conn:
        if table == "instance_servers":
            await conn.execute("INSERT INTO instance_servers(id,owner,public_json,protected,created_ms,updated_ms) VALUES('s1',1,'{}','',1,1)")
        else:
            await conn.execute("INSERT INTO server_nodes(server_id,owner,public_key,enrolled_ms) VALUES('n1',1,'pk',1)")
    await features.migrate(store_db.store, store_db.db.conn)
    assert _stored(store_db.path) == {"instances": False, "servers": True}


async def test_existing_instances_keep_instances_on(store_db):
    await _feature_tables(store_db.db)
    async with store_db.db.write_transaction(label="test") as conn:
        await conn.execute("INSERT INTO managed_instances(id,owner,public_json,protected,report_hash,authority) VALUES('i1',1,'{}','','h','a')")
    await features.migrate(store_db.store, store_db.db.conn)
    assert _stored(store_db.path) == {"instances": True, "servers": False}


async def test_decision_is_made_once_and_never_flips(store_db):
    await features.migrate(store_db.store, store_db.db.conn)
    await _feature_tables(store_db.db)
    async with store_db.db.write_transaction(label="test") as conn:
        await conn.execute("INSERT INTO managed_instances(id,owner,public_json,protected,report_hash,authority) VALUES('i1',1,'{}','','h','a')")
    assert await features.migrate(store_db.store, store_db.db.conn) is None
    assert _stored(store_db.path) == {"instances": False, "servers": False}


async def test_only_missing_switches_are_decided(store_db):
    raw = json.loads(store_db.path.read_text(encoding="utf-8"))
    raw["features"] = {"instances": True}
    store_db.path.write_text(json.dumps(raw), encoding="utf-8")
    await features.migrate(store_db.store, store_db.db.conn)
    assert _stored(store_db.path) == {"instances": True, "servers": False}


def test_missing_switch_counts_as_on():
    cfg = Config.model_validate(_raw())
    assert cfg.features.instances is None and cfg.features.enabled("instances") and cfg.features.enabled("servers")
    cfg = Config.model_validate({**_raw(), "features": {"instances": False, "servers": True}})
    assert not cfg.features.enabled("instances") and cfg.features.enabled("servers")


async def test_switches_are_ordinary_settings(store_db):
    payload = settings_admin.settings_specs_payload()
    domain = next(d for d in payload["domains"] if d["key"] == "features")
    assert domain["sections"][0]["paths"] == ["features.instances", "features.servers"]
    assert payload["specs"]["features.servers"]["kind"] == "bool"
    assert "token" in payload["specs"]["features.instances"]["desc"]
    assert settings_admin.parse_setting_value("features.servers", "开") is True
    cfg = await store_db.store.update_path("features.servers", True)
    assert cfg.features.servers is True and _stored(store_db.path) == {"servers": True}
    values = settings_admin.safe_settings_payload(cfg.model_dump(by_alias=True))["values"]
    assert values["features.servers"] is True and values["features.instances"] is None


def test_managed_instances_are_not_offered_the_switches():
    payload = settings_admin.settings_specs_payload(managed=True)
    assert "features" not in [d["key"] for d in payload["domains"]]
    assert "features" not in [g["key"] for g in payload["groups"]]
    assert not [path for path in payload["specs"] if path.startswith("features.")]


@pytest.fixture
async def controller(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENBEAR_INSTANCE_ROLE", raising=False)
    monkeypatch.chdir(tmp_path)
    raw = {**_raw(), "storage": {"dbPath": str(tmp_path / "t.db")}}
    path = tmp_path / "openbear.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("OPENBEAR_CONFIG", str(path))
    svc = Services(Config.model_validate(raw))
    await svc.db.connect()
    try:
        yield SimpleNamespace(svc=svc, path=path)
    finally:
        await svc.shutdown()


async def test_startup_decision_hides_tools_and_node_browsers(controller):
    svc = controller.svc
    # Before the one-time decision an undecided switch counts as on.
    assert {"InstanceControl", "Servers"} <= set(svc.tools.names())
    await svc._decide_optional_features()
    assert _stored(controller.path) == {"instances": False, "servers": False}
    names = set(svc.tools.names())
    assert "InstanceControl" not in names and "Servers" not in names
    assert svc.browser.nodes is None
    # The services behind the switches keep running.
    assert svc.instances is not None and svc.servers is not None


async def test_switching_applies_to_the_next_turn_without_restart(controller):
    svc = controller.svc
    await svc._decide_optional_features()
    registry_before = svc.tools
    svc.apply_config(await svc.config_store.update_path("features.servers", True))
    assert "Servers" in svc.tools.names("main") and "Servers" not in svc.tools.names("agent")
    assert "InstanceControl" not in svc.tools.names()
    assert svc.browser.nodes is svc.servers.browsers
    # A turn that already started keeps the registry it began with.
    assert "Servers" not in registry_before.names()
    svc.apply_config(await svc.config_store.update_path("features.instances", True))
    svc.apply_config(await svc.config_store.update_path("features.servers", False))
    assert "InstanceControl" in svc.tools.names() and "Servers" not in svc.tools.names()
    assert svc.browser.nodes is None


async def test_settings_api_toggles_tools(controller):
    svc = controller.svc
    await svc._decide_optional_features()
    server = svc.web_admin
    server.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    await server.ensure_secret_key()
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    try:
        start = await client.post("/api/auth/login/start", json={"secret": await server.get_secret_key()})
        req_uuid = (await start.json())["requestUuid"]
        await server.decide_login_request(req_uuid, approved=True, decided_by=123)
        cookie = {"openbear_web_session": (await client.post(f"/api/auth/login/consume/{req_uuid}")).cookies["openbear_web_session"].value}
        values = (await (await client.get("/api/settings", cookies=cookie)).json())["values"]
        assert values["features.instances"] is False and values["features.servers"] is False
        response = await client.patch("/api/settings/features.instances", cookies=cookie, json={"value": True})
        assert response.status == 200 and (await response.json())["value"] is True
        assert "InstanceControl" in svc.tools.names()
        values = (await (await client.get("/api/settings", cookies=cookie)).json())["values"]
        assert values["features.instances"] is True
    finally:
        await client.close()
