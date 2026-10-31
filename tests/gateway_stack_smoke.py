"""Opt-in real Go/Python gateway stack smoke; only disposable loopback processes.

Run with a built gateway and node frontend, without deploying either:
  python tests/gateway_stack_smoke.py --gateway PATH --web-root PATH
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Stack:
    def __init__(self, root, gateway, web_root):
        self.root, self.gateway, self.web_root = root, gateway, web_root
        self.processes, self.logs = [], []
        self.public = f"http://127.0.0.1:{port()}"
        self.control_key = secrets.token_urlsafe(36)
        self.children = []

    def start(self, command, cwd, env=None):
        log = (self.root / f"process-{len(self.logs)}.log").open("wb")
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log)
        self.processes.append(process)
        self.logs.append(log)
        return process

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        for process in reversed(self.processes):
            self.stop(process)
        for log in self.logs:
            log.close()

    def child(self, username, seed=None):
        volume = self.root / username
        volume.mkdir(mode=0o700)
        listen = port()
        child = {"id": secrets.token_hex(12), "root": volume, "url": f"http://127.0.0.1:{listen}",
                 "businessKey": secrets.token_urlsafe(36), "managementKey": secrets.token_urlsafe(36),
                 "username": username, "password": secrets.token_urlsafe(24)}
        child.update(seed or {})
        config = {"models": {"providers": {"test": {"baseUrl": "http://127.0.0.1:9", "apiKey": "isolated-fixture",
                  "protocol": "chat", "models": [{"id": "test"}]}}, "primary": "test/test"},
                  "memory": {"provider": "builtin"}, "web": {"host": "127.0.0.1", "port": listen}}
        path = volume / "openbear.json"
        path.write_text(json.dumps(config))
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "LC_ALL", "TZ"}}
        code = self.root / (username + "-code")
        shutil.copytree(ROOT / "app", code / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "prompts", code / "prompts")
        shutil.copy2(ROOT / "pyproject.toml", code / "pyproject.toml")
        shutil.copytree(self.web_root, code / "web/dist")
        child["webRoot"] = code / "web/dist"
        index = child["webRoot"] / "index.html"
        index.write_text(index.read_text() + "\n<!-- served-by:" + username + " -->\n")
        (child["webRoot"] / "assets/node-proof.js").write_text("window.nodeProof=" + json.dumps(username) + ";\n")
        env.update(PYTHONPATH=str(code), OPENBEAR_INSTANCE_ROLE="managed", OPENBEAR_CHILD_AUTH="gateway",
                   OPENBEAR_WORKSPACE=str(volume), OPENBEAR_CONFIG=str(path), OPENBEAR_INSTANCE_ID=child["id"],
                   OPENBEAR_GATEWAY_KEY=child["businessKey"], OPENBEAR_MANAGEMENT_KEY=child["managementKey"])
        child["process"] = self.start([sys.executable, "-m", "app.main"], volume, env)
        self.children.append(child)
        return child

    def start_gateway(self):
        # One port: browsers and the controller's control API share it.
        config = {"listen": self.public.removeprefix("http://"), "publicUrl": self.public,
                  "controlKey": self.control_key, "stateDir": str(self.root / "gateway-state")}
        self.config = self.root / "gateway.json"
        self.config.write_text(json.dumps(config))
        self.config.chmod(0o600)
        return self.start([str(self.gateway), "--config", str(self.config)], self.root)


async def ready(client, url, process):
    for _ in range(160):
        assert process.poll() is None, "isolated process exited before readiness"
        try:
            async with client.get(url, timeout=aiohttp.ClientTimeout(total=1)) as response:
                if response.status == 200:
                    return
        except (aiohttp.ClientError, TimeoutError):
            pass
        await asyncio.sleep(.125)
    raise AssertionError("isolated service did not become ready")


async def request(client, method, url, *, expected=200, **kwargs):
    async with client.request(method, url, **kwargs) as response:
        raw = await response.read()
        assert response.status == expected, f"{method} {url.split('?')[0]}: expected {expected}, received {response.status}: {raw[:500]!r}"
        return json.loads(raw) if raw else None


async def login(client, stack, child):
    await request(client, "POST", stack.public + "/api/auth/password/login",
                  json={"username": child["username"], "password": child["password"]}, headers={"Origin": stack.public})
    session = await request(client, "GET", stack.public + "/api/auth/session")
    assert session["instanceId"] == child["id"] and session["mode"] == "gateway" and session["chatId"] == 1
    return {"X-OpenBear-Context": session["contextToken"], "Origin": stack.public}


async def socket_closed(ws):
    async with asyncio.timeout(5):
        while True:
            message = await ws.receive()
            if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                return


async def control_plane(stack):
    from app.config import Config
    from app.db.engine import DB
    from app.instances import InstanceService
    root = stack.root / "controller"
    root.mkdir(exist_ok=True)
    db = DB(str(root / "state.db"))
    await db.connect()
    config = Config.model_validate({"models": {"providers": {}, "primary": ""}, "memory": {}})
    service = InstanceService(db, SimpleNamespace(config=config))
    await service.start()
    # As enable() leaves it after installing the gateway on this host.
    value, private = await service.gateway.config()
    if not value["enabled"]:
        await service.gateway.store({**value, "enabled": True, "publicUrl": stack.public, "serverId": "local",
                                     "port": int(stack.public.rsplit(":", 1)[1]), "status": "connected",
                                     "autoUpdate": False},  # Never reinstall the host's gateway.
                                    {"controlKey": stack.control_key, "controlUrl": stack.public})
    return service, db


async def provision(stack, client):
    service, db = await control_plane(stack)
    try:
        result = await service.gateway.get()
        assert result["connected"] and stack.control_key not in json.dumps(result)
        for username in ("family-one", "family-two"):
            password = secrets.token_urlsafe(24)
            child = stack.child(username, {"password": password})
            await ready(client, child["url"] + "/health", child["process"])
            form = {"name": username, "serverId": "local", "managementUrl": child["url"], "workspacePath": "/srv/fixture",
                    "account": {"username": username, "password": password}, "models": {"mode": "later"},
                    "notifications": {"loginSuccess": True, "loginFailure": True, "health": False}}
            await service.save(123, form, adopt={k: child[k] for k in ("id", "managementKey", "businessKey")})
            row = await service.row(123, child["id"])
            assert password not in json.dumps(service.vault.open(row["protected"]))
            probe = await service.gateway.probe(row)
            assert probe["ok"] and probe["status"]["instanceId"] == child["id"], probe
            assert probe["status"]["persistence"]["root"] == str(child["root"])
            child["dbPath"] = probe["status"]["persistence"]["dbPath"]
        print("real Python controller -> Go (single port): account sync on save, revision ACK, password material removal, gateway -> actual child identity passed")
    finally:
        await service.close()
        await db.close()


async def login_events(stack):
    service, db = await control_plane(stack)
    try:
        await service.gateway.pull_events()
        first = stack.children[0]
        kinds = [event["type"] for event in (await service.events(123, first["id"]))["items"]]
        assert "login_success" in kinds, kinds
        notes = [row["text"] for row in await (await db.conn.execute("SELECT text FROM instance_notifications")).fetchall()]
        assert any("登录成功" in text for text in notes), notes
        print("real controller pulled login events from Go and queued notifications passed")
    finally:
        await service.close()
        await db.close()


async def register_push(stack, client, child, auth):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.web_push import b64url
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    endpoint = "https://fcm.googleapis.com/isolated-not-delivered/" + secrets.token_hex(16)
    subscription = {"endpoint": endpoint, "keys": {"p256dh": b64url(public), "auth": b64url(secrets.token_bytes(16))}}
    value = await request(client, "POST", stack.public + "/api/push/subscription", headers=auth, json={"subscription": subscription})
    assert value["enabled"]
    assert (await request(client, "POST", stack.public + "/api/push/status", headers=auth, json={"endpoint": endpoint}))["enabled"]
    await request(client, "POST", stack.public + "/api/push/presence", headers=auth, json={"endpoint": endpoint, "conversationUuid": child["conversation"], "clientId": "isolated"})
    with sqlite3.connect("file:" + child["dbPath"] + "?mode=ro", uri=True) as database:
        binding = database.execute("SELECT session_token_hash FROM web_push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()[0]
    assert binding and binding != hashlib.sha256(b"").hexdigest(), "gateway subscription used empty Cookie identity"
    return binding


async def push_session_status(stack, child, binding):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as client:
        return await request(client, "GET", stack.public + "/api/gateway/v1/session-status",
                             headers={"Authorization": "Bearer " + child["businessKey"], "X-OpenBear-Instance": child["id"],
                                      "X-OpenBear-Gateway-Session-Hash": binding})


def node_page(child, raw):
    """The node writes its login mode and its gateway asset prefix into the page."""
    from app.web_console.auth_api import _INDEX_ASSET_REF
    prefix = "/_i/" + child["id"] + "/"
    text = raw.replace('"[[controller]]"', '"gateway"', 1).replace('"[[asset-base]]"', json.dumps(prefix), 1)
    return _INDEX_ASSET_REF.sub(lambda match: match[1] + prefix + "assets/", text)


async def node_frontend(client, stack, child, other):
    from app.web_console.auth_api import PWA_PUBLIC_FILES
    prefix = "/_i/" + child["id"] + "/"
    for path in ("/", "/chat", "/skills", "/assets/node-proof.js", prefix + "assets/node-proof.js", *PWA_PUBLIC_FILES):
        page = path in {"/", "/chat", "/skills"}
        relative = "index.html" if page else PWA_PUBLIC_FILES.get(path, (path.removeprefix(prefix).lstrip("/"),))[0]
        expected = (child["webRoot"] / relative).read_bytes()
        if path == "/openbear-push-sw.js":  # The gateway's own copy, never the node's.
            expected = (ROOT / "gateway/openbear-push-sw.js").read_bytes()
        if page:
            expected = node_page(child, expected.decode()).encode()
            assert b'"/assets/' not in expected
        async with client.get(stack.public + path) as response:
            assert response.status == 200, f"node frontend rejected {path}"
            assert await response.read() == expected, f"wrong node frontend at {path}"
    async with client.get(stack.public + "/openbear-push-sw.js", headers={"Service-Worker": "script"}) as response:
        assert response.status == 200
    for path in ("/", prefix + "assets/node-proof.js", "/assets/node-proof.js"):
        async with client.get(stack.public + path, headers={"Service-Worker": "script"}) as response:
            assert response.status == 403, f"node script accepted as a service worker: {path}"
    asset = next((child["webRoot"] / "assets").glob("index-*.js"))
    asset_url = stack.public + prefix + "assets/" + asset.name
    async with client.get(asset_url) as response:
        assert response.status == 200 and await response.read() == asset.read_bytes()
        assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
        validators = {"If-None-Match": response.headers["ETag"], "If-Modified-Since": response.headers["Last-Modified"]}
    for name, value in validators.items():
        async with client.get(asset_url, headers={name: value}) as response:
            assert response.status == 304 and not await response.read()
    # Unprefixed (pre-prefix node) assets are shared by every instance: never cached.
    async with client.get(stack.public + "/assets/" + asset.name, headers=validators) as response:
        assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
    async with client.get(stack.public + "/_i/" + other["id"] + "/assets/" + asset.name) as response:
        assert response.status == 409, "another instance's prefix was served"
    async with client.get(stack.public + "/skills", headers=validators) as response:
        assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
    index = child["webRoot"] / "index.html"
    updated = index.read_text() + "<!-- independent-node-update -->\n"
    index.write_text(updated)
    async with client.get(stack.public + "/chat") as response:
        assert response.status == 200 and await response.text() == node_page(child, updated)
    print("node-owned frontend: " + child["username"] + " exact HTML/Skills/JS/PWA bytes below its own asset prefix, gateway-owned service worker, prefixed hashed asset 304 revalidation, unprefixed no-store and independent update without gateway restart passed")


async def ip_policy(stack, child):
    """Default automatic detection: a loopback peer is a local reverse proxy."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as browser:
        await request(browser, "POST", stack.public + "/api/auth/password/login", headers={"Origin": stack.public, "X-Forwarded-For": "198.51.100.42"},
                      json={"username": child["username"], "password": child["password"]})
        session = await request(browser, "GET", stack.public + "/api/auth/session")
        auth = {"X-OpenBear-Context": session["contextToken"], "Origin": stack.public}
        sessions = await request(browser, "GET", stack.public + "/api/auth/sessions", headers=auth)
        assert next(item for item in sessions["items"] if item["current"])["ip"] == "198.51.100.42"
        await request(browser, "POST", stack.public + "/api/auth/logout", headers=auth, json={})
    service, db = await control_plane(stack)
    try:
        observed = (await service.gateway.get())["observed"]
        assert observed["peer"] == "127.0.0.1" and observed["header"] == "X-Forwarded-For" and observed["trusted"] is True, observed
        print("real Go automatic client IP: local proxy detected, forwarded IP recorded, observation reported to controller passed")
    finally:
        await service.close()
        await db.close()


async def exercise(stack, client, *, check_push=False):
    gateway = stack.start_gateway()
    await ready(client, stack.public + "/api/auth/mode", gateway)
    await provision(stack, client)
    first, second = stack.children
    await ip_policy(stack, first)
    await login_events(stack)
    auth = await login(client, stack, first)
    await node_frontend(client, stack, first, second)
    await request(client, "POST", stack.public + "/api/auth/password/login", expected=409,
                  json={"username": second["username"], "password": second["password"]}, headers={"Origin": stack.public})
    await request(client, "GET", stack.public + "/api/conversations", expected=409)
    await request(client, "GET", stack.public + "/api/managed/v1/status", expected=403, headers=auth)
    created = await request(client, "POST", stack.public + "/api/conversations", headers=auth, json={"title": "family-one-private"})
    first["conversation"] = created["conversation"]["conversationUuid"]
    assert "family-one-private" in json.dumps(await request(client, "GET", stack.public + "/api/conversations", headers=auth))
    prefix = stack.public + "/api/conversations/" + first["conversation"]
    payload = (b"gateway-real-file-proof\n" * 70000)
    upload = await request(client, "POST", prefix + "/uploads", headers=auth,
                           json={"name": "proof.txt", "size": len(payload), "type": "text/plain"})
    upload_url = prefix + "/uploads/" + upload["uploadId"]
    await request(client, "PUT", upload_url + "?offset=0", headers=auth, data=payload)
    await request(client, "POST", upload_url + "/complete", headers=auth, json={})
    files = await request(client, "GET", prefix + "/artifacts", headers=auth)
    artifact = files["items"][0]
    first["download"] = artifact["downloadUrl"]
    async with client.get(stack.public + first["download"] + "&_ob_ctx=" + quote(auth["X-OpenBear-Context"])) as response:
        assert response.status == 200 and await response.read() == payload
        assert response.headers["Cache-Control"] == "no-store"
    if check_push:
        binding = await register_push(stack, client, first, auth)
        assert (await push_session_status(stack, first, binding))["active"]
    ws = await client.ws_connect(stack.public + "/api/events/ws?_ob_ctx=" + quote(auth["X-OpenBear-Context"]), origin=stack.public)
    await request(client, "POST", stack.public + "/api/auth/logout", headers=auth, json={})
    await socket_closed(ws)
    if check_push:
        assert not (await push_session_status(stack, first, binding))["active"]
        print("real Go/Python push: subscription/status/presence use nonempty original-session binding; node-authenticated active check flips inactive on logout; no provider traffic sent")
    new_auth = await login(client, stack, second)
    await node_frontend(client, stack, second, first)
    await request(client, "POST", stack.public + "/api/conversations", expected=409, headers=auth, json={"title": "must-not-arrive"})
    async with client.get(stack.public + first["download"] + "&_ob_ctx=" + quote(auth["X-OpenBear-Context"])) as response:
        assert response.status == 409
    second_data = json.dumps(await request(client, "GET", stack.public + "/api/conversations", headers=new_auth))
    assert "family-one-private" not in second_data and "must-not-arrive" not in second_data
    stack.stop(gateway)
    gateway = stack.start_gateway()
    await ready(client, stack.public + "/api/auth/mode", gateway)
    assert (await request(client, "GET", stack.public + "/api/auth/session"))["instanceId"] == second["id"]
    await request(client, "GET", stack.public + "/api/conversations", headers=new_auth)
    ws = await client.ws_connect(stack.public + "/api/events/ws?_ob_ctx=" + quote(new_auth["X-OpenBear-Context"]), origin=stack.public)
    password = secrets.token_urlsafe(24)
    await request(client, "POST", stack.public + "/api/auth/password/change", headers=new_auth,
                  json={"currentPassword": second["password"], "password": password})
    second["password"] = password
    await socket_closed(ws)
    await request(client, "GET", stack.public + "/api/conversations", expected=401, headers=new_auth)
    await login(client, stack, second)
    service, db = await control_plane(stack)
    try:
        # A later controller-side edit must not write the old password back.
        await service.save(123, {"name": "renamed"}, second["id"])
    finally:
        await service.close()
        await db.close()
    client.cookie_jar.clear()
    await login(client, stack, second)
    client.cookie_jar.clear()
    print("real Go/Python: HTTP login without controller; route identity; API + 1.5MB upload/download; two-instance isolation; logout/change-password close WS; persisted sessions after gateway restart passed")


async def main(args):
    with tempfile.TemporaryDirectory(prefix="gateway-stack-") as directory:
        stack = Stack(Path(directory), args.gateway.resolve(), args.web_root.resolve())
        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=aiohttp.ClientTimeout(total=20)) as client:
                await exercise(stack, client, check_push=args.push)
                if args.chrome:
                    from gateway_ui_smoke import exercise_browser
                    await exercise_browser(stack, client, args.chrome)
        finally:
            stack.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--web-root", type=Path, required=True)
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--push", action="store_true", help="check push registration and gateway revocation without sending to a provider")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args))
