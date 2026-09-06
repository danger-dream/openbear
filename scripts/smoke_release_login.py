#!/usr/bin/env python3
"""Test the built release with its own auth handlers, a disposable DB and local Chrome.

Usage: .venv/bin/python scripts/smoke_release_login.py --root /path/to/unpacked/release
No production configuration, real Bot, updater, or service lifecycle is used.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class FakeBot:
    def __init__(self):
        self.requests = []

    async def send_message(self, chat_id, text, reply_markup=None):
        # Deliberately async: the old Enter + form-submit bug needs this window.
        await asyncio.sleep(0.15)
        self.requests.append(reply_markup.inline_keyboard[0][0].callback_data.rsplit(":", 1)[1])
        return SimpleNamespace(message_id=len(self.requests))


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 0
        self.errors = []

    async def call(self, method, params=None):
        self.seq += 1
        seq = self.seq
        await self.ws.send_json({"id": seq, "method": method, "params": params or {}})
        while True:
            result = await asyncio.wait_for(self.ws.receive_json(), 20)
            if result.get("method") == "Runtime.exceptionThrown":
                self.errors.append(result["params"]["exceptionDetails"].get("text", "exception"))
            if result.get("id") == seq:
                if "error" in result:
                    raise RuntimeError(result["error"])
                return result.get("result", {})

    async def evaluate(self, expression):
        result = await self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    async def until(self, expression, timeout=12):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            try:
                result = await self.evaluate(expression)
                if result:
                    return result
            except RuntimeError:
                pass  # Navigation can replace the JS execution context.
            await asyncio.sleep(0.05)
        raise AssertionError(f"Browser condition timed out: {expression}")


async def verify(args):
    root = args.root.resolve()
    dist = (args.dist or root / "web/dist").resolve()
    assert (dist / "index.html").is_file(), "Built frontend missing"
    sys.path.insert(0, str(root))
    # Import from the unpacked release, not the caller's working tree.
    import app.web_console.auth_api as auth_module
    from app.admin.skills_web import SkillsWebAdminServer
    from app.config import Config
    from app.db.engine import DB
    assert Path(auth_module.__file__).resolve().is_relative_to(root), "Wrong backend imported"
    report = {}
    with tempfile.TemporaryDirectory(prefix="openbear-release-smoke-") as temp:
        temp = Path(temp)
        os.environ["OPENBEAR_WEB_ARTIFACT_DIR"] = str(temp / "artifacts")
        config = Config.model_validate({
            "telegram": {"botToken": "test-only", "whitelistIds": [123]},
            "models": {"providers": {"test": {"baseUrl": "http://127.0.0.1:1", "apiKey": "test-only",
                "protocol": "chat", "models": [{"id": "test"}]}}, "primary": "test/test"},
            "memory": {"baseUrl": "http://127.0.0.1:1", "identity": "smoke", "accessKey": "test-only"},
            "web": {"enabled": True, "host": "127.0.0.1", "port": 18961},
        })
        db = DB(str(temp / "smoke.db"))
        await db.connect()
        bot = FakeBot()
        server = SkillsWebAdminServer(config, db, bot)
        server.workspace_dir = str(temp / "workspace")
        server._web_dist_dir = lambda: dist
        await server.ensure_secret_key()
        secret = await server.get_secret_key()
        records = []

        @web.middleware
        async def record(request, handler):
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = exc
            # No request bodies, Cookie values, credentials or user material in evidence.
            path = re.sub(r"(/auth/login/(?:status|consume)/)[^/]+", r"\1<request>", request.path)
            records.append({"method": request.method, "path": path, "status": response.status,
                            "document": request.headers.get("Sec-Fetch-Dest") == "document"})
            return response

        app = server.make_app()
        app.middlewares.insert(0, record)
        client = TestClient(TestServer(app))
        await client.start_server()
        base = str(client.make_url("")).rstrip("/")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            chrome_port = sock.getsockname()[1]
        chrome = args.chrome or shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            await client.close()
            await db.close()
            raise RuntimeError("Chrome/Chromium is required for release smoke")
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--no-proxy-server", "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-component-update",
                f"--remote-debugging-port={chrome_port}", "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={temp / 'chrome'}", "about:blank",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            async with aiohttp.ClientSession() as http:
                for _ in range(200):
                    try:
                        async with http.get(f"http://127.0.0.1:{chrome_port}/json/list") as response:
                            tabs = await response.json()
                        break
                    except (aiohttp.ClientError, OSError):
                        await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("Chrome startup failed")
                tab = next(x for x in tabs if x.get("type") == "page")
                async with http.ws_connect(tab["webSocketDebuggerUrl"]) as ws:
                    browser = CDP(ws)
                    await browser.call("Page.enable")
                    await browser.call("Runtime.enable")
                    await browser.call("Network.enable")
                    await browser.call("Network.setCacheDisabled", {"cacheDisabled": True})

                    async def navigate_login():
                        await browser.call("Network.clearBrowserCookies")
                        await browser.call("Page.navigate", {"url": base + "/login"})
                        await browser.until("Boolean(document.querySelector('input[type=password]'))")
                        await asyncio.sleep(0.2)

                    async def fill():
                        await browser.evaluate("(() => {const e=document.querySelector('input[type=password]'); e.value="
                            + json.dumps(secret) + ";e.dispatchEvent(new Event('input',{bubbles:true}));e.focus()})()")
                        await asyncio.sleep(0.05)

                    async def click():
                        await fill()
                        await browser.evaluate("document.querySelector('form button.el-button').click()")
                        await browser.until("document.body.innerText.includes('请求：')")

                    async def terminal():
                        await browser.until("!document.querySelector('form button.el-button').classList.contains('is-loading')")
                        count = sum("/status/" in x["path"] for x in records)
                        await asyncio.sleep(2.1)
                        assert sum("/status/" in x["path"] for x in records) == count, "Terminal state kept polling"
                        assert await browser.evaluate("location.pathname") == "/login", "401 caused a login reload"
                        assert not await browser.evaluate("document.querySelector('form button.el-button').disabled")

                    await navigate_login()
                    await browser.evaluate("['focus','pageshow','popstate','openbear:conversations-refresh'].forEach(x=>window.dispatchEvent(new Event(x)))")
                    await asyncio.sleep(1)
                    apis = [x for x in records if x["path"].startswith("/api/")]
                    docs = sum(x["document"] and x["path"] == "/login" for x in records)
                    assert not apis, f"Pre-auth frontend called API: {apis}"
                    assert docs == 1, f"Login document reloaded {docs} times"
                    assert not browser.errors, f"Login runtime errors: {browser.errors}"
                    assert not [x for x in records if x["path"].startswith("/assets/") and x["status"] != 200]
                    assert (await client.get("/api/system/version")).status == 401, "Version API was made public"
                    report["anonymousBootstrap"] = {"apiCalls": len(apis), "loginDocuments": docs, "runtimeErrors": 0}
                    if args.anonymous_only:
                        return report

                    records.clear()
                    start = len(bot.requests)
                    await fill()
                    await browser.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "text": "\r"})
                    await browser.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
                    await browser.until("document.body.innerText.includes('请求：')")
                    await asyncio.sleep(0.3)
                    assert len(bot.requests) - start == 1, "Enter sent multiple Telegram requests"
                    assert sum(x["path"] == "/api/auth/login/start" for x in records) == 1
                    await server.decide_login_request(bot.requests[-1], approved=True, decided_by=123)
                    await browser.until("location.pathname !== '/login' && Boolean(document.querySelector('textarea'))")
                    assert sum("/consume/" in x["path"] for x in records) == 1
                    report["nativeEnter"] = {"loginStarts": 1, "notifications": 1, "approvedLoginSucceeded": True}

                    for status in ["missing", "consumed", "expired", "rejected", "unexpected"]:
                        await navigate_login()
                        await click()
                        uuid = bot.requests[-1]
                        if status == "missing":
                            await db.conn.execute("DELETE FROM web_login_requests WHERE request_uuid=?", (uuid,))
                        else:
                            await db.conn.execute("UPDATE web_login_requests SET status=? WHERE request_uuid=?", (status, uuid))
                        await db.conn.commit()
                        records.clear()
                        await terminal()
                        assert not any(x["document"] for x in records), "Terminal handling navigated/reloaded login"
                        report[status] = {"pollingStopped": True, "canRetry": True, "reloaded": False}

                    # Existing valid session + consumed request recovers rather than starting another login.
                    await navigate_login()
                    await click()
                    uuid = bot.requests[-1]
                    await server.decide_login_request(uuid, approved=True, decided_by=123)
                    consumed = await browser.evaluate("fetch('/api/auth/login/consume/" + uuid + "',{method:'POST'}).then(r=>r.status)")
                    assert consumed == 200
                    await browser.until("location.pathname !== '/login' && Boolean(document.querySelector('textarea'))")
                    report["consumedWithSession"] = {"resumed": True}

                    # Both tabs already contain the original bundle; a backend+frontend
                    # update result may have been acknowledged by a different tab.
                    draft = "release smoke: unsent draft must survive"
                    await browser.evaluate("(() => {const e=document.querySelector('textarea');e.value=" + json.dumps(draft)
                        + ";e.dispatchEvent(new Event('input',{bubbles:true}))})()")
                    await browser.until("(localStorage.getItem('openbear.console.drafts.v1') || '').includes('unsent draft')")
                    new_target = await browser.call("Target.createTarget", {"url": base + "/chat"})
                    async with http.get(f"http://127.0.0.1:{chrome_port}/json/list") as response:
                        all_tabs = await response.json()
                    other = next(x for x in all_tabs if x["id"] == new_target["targetId"])
                    async with http.ws_connect(other["webSocketDebuggerUrl"]) as other_ws:
                        second = CDP(other_ws)
                        await second.call("Page.enable")
                        await second.call("Runtime.enable")
                        await second.until("Boolean(document.querySelector('textarea'))")
                        original_build_info = server._frontend_build_info
                        real_info = original_build_info()
                        assert real_info, "Release lacks frontend build identity"
                        changed_info = {**real_info, "buildId": "f" * 16 if real_info["buildId"] != "f" * 16 else "e" * 16}
                        server._frontend_build_info = lambda: changed_info
                        server.update_service = SimpleNamespace(snapshot=lambda **_: {
                            "ok": True, "version": real_info["version"], "phase": "idle", "latest": None,
                            "lastResult": {"status": "success", "requiresRestart": True, "acked": True},
                        })
                        for page in [browser, second]:
                            await page.call("Page.bringToFront")
                            await page.evaluate("window.dispatchEvent(new Event('focus'))")
                            await page.until("Boolean(document.querySelector('.el-message-box')) && document.querySelector('.el-message-box').innerText.includes('前端版本已变化')")
                            await page.evaluate("Array.from(document.querySelectorAll('.el-message-box button')).find(e=>e.innerText.includes('保留当前编辑')).click()")
                            await page.until("Boolean(document.querySelector('[data-testid=frontend-refresh-required]'))")
                        assert await browser.evaluate("document.querySelector('textarea').value") == draft
                        server._frontend_build_info = original_build_info
                        server.update_service = None
                        await browser.call("Page.bringToFront")
                        await browser.evaluate("document.querySelector('[data-testid=frontend-refresh-required] button').click()")
                        await browser.until("Boolean(document.querySelector('.el-message-box'))")
                        await browser.evaluate("Array.from(document.querySelectorAll('.el-message-box button')).find(e=>e.innerText.includes('已处理未提交内容')).click()")
                        await browser.until("Boolean(document.querySelector('textarea')) && !document.querySelector('[data-testid=frontend-refresh-required]')")
                        assert await browser.evaluate("document.querySelector('textarea').value") == draft
                        report["versionHandshake"] = {"tabsNotified": 2, "ackedRestartResultHandled": True, "draftSurvivedRefresh": True}
                        await second.call("Page.navigate", {"url": "about:blank"})
                    await browser.call("Page.navigate", {"url": "about:blank"})

                    config.web.custom_url = "https://configured.invalid"
                    redirect = await client.get("/login", allow_redirects=False)
                    assert redirect.status == 302 and redirect.headers["Location"] == "https://configured.invalid/login"
                    start = len(bot.requests)
                    rejected = await client.post("/api/auth/login/start", json={"secret": secret})
                    assert rejected.status == 400 and (await rejected.json())["error"] == "https_required"
                    assert len(bot.requests) == start and not rejected.cookies
                    report["httpsEntry"] = {"redirectedToConfiguredOrigin": True, "unsafePostRejectedBeforeNotification": True}
                    from scripts.release_validation import probe_deployment
                    probe = await asyncio.to_thread(probe_deployment, base + "/health", real_info["version"])
                    assert probe["ok"] and probe["assets"]
                    report["updaterWebProbe"] = {"httpsConfigured": True, "entryAssetsFetched": len(probe["assets"])}
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            with contextlib.suppress(Exception):
                await client.close()
            await db.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--chrome")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--anonymous-only", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(verify(args))
    text = json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
