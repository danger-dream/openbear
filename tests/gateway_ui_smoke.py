"""Headless Chrome UI companion for gateway_stack_smoke.py; no browser profile reuse."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote, urlsplit

from gateway_stack_smoke import port, ready


class ContextReplaced(RuntimeError):
    pass


class Page:
    def __init__(self, ws):
        self.ws, self.sequence = ws, 0
        self.requests, self.exceptions = [], []

    async def call(self, method, params=None):
        self.sequence += 1
        sequence = self.sequence
        await self.ws.send_json({"id": sequence, "method": method, "params": params or {}})
        async with asyncio.timeout(35):
            while True:
                packet = await self.ws.receive_json()
                if packet.get("method") == "Network.requestWillBeSent":
                    self.requests.append(packet["params"]["request"])
                if packet.get("method") == "Runtime.exceptionThrown":
                    self.exceptions.append(packet["params"]["exceptionDetails"].get("text", "exception"))
                if packet.get("id") == sequence:
                    if "error" in packet:
                        message = packet["error"].get("message", "")
                        if "navigated" in message.lower() or "context" in message.lower() and ("destroyed" in message.lower() or "find" in message.lower()):
                            raise ContextReplaced(message)
                        raise AssertionError(f"Chrome rejected {method}: {message}")
                    return packet.get("result", {})

    async def evaluate(self, expression):
        result = await self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
        assert "exceptionDetails" not in result, "browser expression failed"
        return result.get("result", {}).get("value")

    async def wait(self, expression):
        for _ in range(4):
            try:
                result = await self.evaluate("(async () => {const until=Date.now()+20000; while(Date.now()<until){if(" + expression + ")return true; await new Promise(r=>setTimeout(r,50));} return false;})()")
                if not result:
                    state = await self.evaluate("({path:location.pathname,text:document.body.innerText.slice(0,1200)})")
                    raise AssertionError(f"browser UI condition timed out: {state}; exceptions={self.exceptions}")
                return
            except ContextReplaced:
                continue
        raise AssertionError("browser kept navigating during UI check")

    async def navigate(self, url):
        await self.call("Page.navigate", {"url": url})

    async def close(self):
        await self.ws.close()


async def new_page(client, debug, url):
    async with client.put(debug + "/json/new?" + quote("about:blank", safe="")) as response:
        target = await response.json()
    page = Page(await client.ws_connect(target["webSocketDebuggerUrl"]))
    for method in ("Page.enable", "Runtime.enable", "Network.enable"):
        await page.call(method)
    await page.navigate(url)
    return page


async def login_page(page, child):
    await page.wait("document.querySelector('input[aria-label=\"账号\"]')?.offsetParent && !document.querySelector('input[aria-label=\"账号\"]').disabled")
    await page.evaluate("(() => {const v=" + json.dumps({"username": child["username"], "password": child["password"]}) + "; for(const [label,value] of [['账号',v.username],['密码',v.password]]){const input=document.querySelector('input[aria-label=\"'+label+'\"]'); input.value=value; input.dispatchEvent(new Event('input',{bubbles:true}));}return true;})()")
    await page.wait("!document.querySelector('form button[type=submit]').disabled")
    await page.evaluate("document.querySelector('form button[type=submit]').click()")
    try:
        await page.wait("location.pathname !== '/login' && !document.querySelector('input[aria-label=\"账号\"]')")
    except AssertionError as error:
        state = await page.evaluate("({path:location.pathname,status:document.querySelector('#status')?.textContent,formHidden:document.querySelector('form')?.hidden})")
        raise AssertionError(f"login navigation did not complete: {state}") from error
    await page.wait("document.body.innerText.includes('OpenBear') && document.querySelector('#app')?.children.length > 0")


async def exercise_browser(stack, client, chrome):
    debug = f"http://127.0.0.1:{port()}"
    process = stack.start([str(chrome), "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                           "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
                           "--disable-sync", "--no-first-run", "--no-default-browser-check",
                           "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + debug.rsplit(":", 1)[1],
                           "--user-data-dir=" + str(stack.root / "chrome-profile"), "about:blank"], stack.root)
    await ready(client, debug + "/json/version", process)
    first, second = stack.children
    old = await new_page(client, debug, stack.public + "/skills")
    other = None
    try:
        await old.call("Network.setCookie", {"name": "openbear_controller_fixture", "value": "preserve", "url": stack.public, "httpOnly": True})
        await old.wait("document.querySelector('input[aria-label=\"账号\"]')")
        for width in (320, 390):
            await old.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 844, "deviceScaleFactor": 1, "mobile": True})
            for theme in ("light", "dark"):
                await old.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": theme}]})
                assert await old.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "gateway login overflow"
                assert await old.evaluate("[...document.querySelectorAll('input')].every(el => parseFloat(getComputedStyle(el).fontSize)>=16)"), "gateway login input zoom risk"
        await old.call("Emulation.clearDeviceMetricsOverride")
        await login_page(old, first)
        await old.wait("location.pathname === '/skills' && document.querySelector('.skills-page')")
        await old.evaluate("window.__beforeSkillsReload = true")
        await old.call("Page.reload")
        await old.wait("!window.__beforeSkillsReload && location.pathname === '/skills' && document.querySelector('.skills-page')")
        prefix = "/_i/" + first["id"] + "/"
        sizes = await old.evaluate("(() => {const assets=performance.getEntriesByType('resource').filter(e=>new URL(e.name).pathname.startsWith(" + json.dumps(prefix + "assets/") + ") && /-[A-Za-z0-9_-]{8,}\\.(js|css)$/.test(new URL(e.name).pathname)); return {count:assets.length,bodyBytes:assets.reduce((n,e)=>n+e.encodedBodySize,0),networkBytes:assets.reduce((n,e)=>n+e.transferSize,0)};})()")
        assert sizes['count'] > 0 and sizes['bodyBytes'] > 0 and sizes['networkBytes'] < sizes['bodyBytes'] / 2, f"compiled resources were downloaded again: {sizes}"
        print(f"real Chrome Skills: bookmark -> login -> Skills -> refresh passed; hashed JS/CSS body={sizes['bodyBytes']} bytes, refresh network={sizes['networkBytes']} bytes")
        assert "served-by:" + first["username"] in await old.evaluate("new XMLSerializer().serializeToString(document)")
        await old.navigate(stack.public + "/chat")
        await old.wait("document.body.innerText.includes('family-one-private')")
        loaded = await old.evaluate("""(async () => {
          const prefix = """ + json.dumps(prefix) + """;
          await document.fonts.load('16px KaTeX_Main');
          const worker = window.MonacoEnvironment.getWorker();
          const registration = await navigator.serviceWorker.register('/openbear-push-sw.js', {scope: '/', updateViaCache: 'none'});
          const ready = await navigator.serviceWorker.ready;
          let rejected = false;
          try { await navigator.serviceWorker.register(document.querySelector('script[type=module]').src, {scope: prefix + 'assets/'}); } catch { rejected = true; }
          await new Promise(resolve => setTimeout(resolve, 500));
          worker.terminate();
          const script = ready.active.scriptURL;
          await registration.unregister();
          const paths = performance.getEntriesByType('resource').map(entry => new URL(entry.name).pathname);
          return {rejected, script: new URL(script).pathname, katex: paths.filter(path => path.includes('KaTeX_Main-Regular'))};
        })()""")
        assert loaded["rejected"] and loaded["script"] == "/openbear-push-sw.js", loaded
        assert loaded["katex"] and all(path.startswith(prefix + "assets/") for path in loaded["katex"]), loaded
        worker = [item["url"] for item in old.requests if "editor.worker" in item["url"]]
        assert worker and all(urlsplit(url).path.startswith(prefix + "assets/") for url in worker), worker
        # The install manifest is fetched by the browser itself and needs the login cookie.
        manifest = await old.call("Page.getAppManifest")
        manifest_path = urlsplit(manifest.get("url", "")).path
        assert json.loads(manifest.get("data") or "{}").get("name") == "OpenBear", manifest
        assert manifest_path == "/manifest.webmanifest" or manifest_path.startswith(prefix + "assets/pwa/"), manifest_path
        print("real Chrome: gateway notification worker registered, node script refused as a worker; KaTeX font, editor worker and "
              f"install manifest ({manifest_path}) load below " + prefix)
        await old.evaluate("localStorage.setItem('controller-fixture','preserve'); true")
        other = await new_page(client, debug, stack.public + "/chat")
        await other.wait("document.body.innerText.includes('family-one-private')")
        # This is another tab/device revoking the session, not a new binding
        # injected into the old document. The app must detect the real WS close.
        status = await other.evaluate("(async()=>{const s=await (await fetch('/api/auth/session')).json();const r=await fetch('/api/auth/logout',{method:'POST',headers:{'X-OpenBear-Context':s.contextToken,'Content-Type':'application/json'},body:'{}'});return r.status;})()")
        assert status == 200
        await old.wait("document.body.innerText.includes('当前页面登录已失效')")
        await other.navigate(stack.public + "/login")
        await login_page(other, second)
        assert "served-by:" + second["username"] in await other.evaluate("new XMLSerializer().serializeToString(document)")
        await other.wait("document.querySelector('#app')?.innerText.length > 10")
        assert not await other.evaluate("document.body.innerText.includes('family-one-private')")
        assert await old.evaluate("document.body.innerText.includes('当前页面登录已失效')")
        assert await old.evaluate("localStorage.getItem('controller-fixture')") == "preserve"
        cookies = await other.call("Network.getCookies", {"urls": [stack.public]})
        assert any(cookie["name"] == "openbear_controller_fixture" for cookie in cookies["cookies"])
        # The second tab showed the first instance before switching accounts.
        for page, children in ((old, (first,)), (other, (first, second))):
            assert not page.exceptions, "uncaught frontend exception"
            # Every compiled file comes from the instance prefix of its document.
            prefixes = tuple("/_i/" + child["id"] + "/assets/" for child in children)
            paths = [urlsplit(item["url"]).path for item in page.requests if item["url"].startswith(stack.public)]
            assert all(any(path.startswith(prefix) for path in paths) for prefix in prefixes), "no prefixed asset loaded"
            stray = [path for path in paths if path.startswith(("/assets/", "/_i/")) and not path.startswith(prefixes)]
            assert not stray, f"asset outside the page's instance prefix: {stray[:5]}"
            requests = [item for item in page.requests if item["url"].startswith(stack.public + "/api/") and "/api/auth/" not in item["url"]]
            assert requests, "frontend did not make real business requests"
            for item in requests:
                headers = {key.lower(): value for key, value in item.get("headers", {}).items()}
                assert headers.get("x-openbear-context") or "_ob_ctx=" in item["url"], "unbound browser business request"
        for width in (320, 390):
            await other.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 844, "deviceScaleFactor": 1, "mobile": True})
            for theme in ("light", "dark"):
                await other.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": theme}]})
                await other.evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                assert await other.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "mobile document overflow"
        print("real Chrome: embedded login at 320/390px; actual node-owned pages; shared-cookie two-tab logout/switch; stale document blocked; business requests bound; controller cookie/storage retained; 320/390px light/dark no document overflow passed")
    finally:
        await old.close()
        if other:
            await other.close()
        stack.stop(process)
