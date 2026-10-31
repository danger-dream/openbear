"""Python worker vs Go node browser engine: the same scenarios against a real Chrome.

Both engines are driven through app.browser.worker.Worker and must return the same
results after normalising volatile fields (snapshot ids, artifact directories,
event cursors). Skipped when Chrome or Go is unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from aiohttp import web

from app.browser.cdp import CDP
from app.browser.worker import Worker, WorkerError

ROOT = Path(__file__).resolve().parents[1]
CHROME = shutil.which("google-chrome") or "/usr/bin/google-chrome"
GO = shutil.which("go")

pytestmark = pytest.mark.skipif(
    not (os.path.isfile(CHROME) and os.access(CHROME, os.X_OK)) or not GO,
    reason="needs google-chrome and Go",
)

LIMITS = {
    "snapshotMaxChars": 20000,
    "maxEventEntries": 200,
    "maxBodyBytes": 1024 * 1024,
    "maxArtifactBytes": 50 * 1024 * 1024,
}

MAIN = """<!doctype html><html><head><title>Parity</title>
<style>*{caret-color:transparent} #cover{position:relative} #hidden{display:none} #dst,#drop,#src{width:120px;height:40px;border:1px solid #888}
iframe{width:420px;height:160px;border:0}</style></head><body>
<h1>Parity page</h1>
<button id="btn" onclick="out('clicked '+(++window.clicks))">Click me</button>
<button id="dbl" ondblclick="out('double')">Double me</button>
<button id="right" oncontextmenu="out('context');return false">Right me</button>
<div id="out">idle</div>
<label>Name <input id="name"></label>
<label>Email <input id="email"></label>
<label>Notes <textarea id="notes"></textarea></label>
<label><input type="checkbox" id="agree"> Agree</label>
<label>Color <select id="color"><option value="r">Red</option><option value="g">Green</option></select></label>
<div id="hover" onmouseover="this.textContent='hovered'">Hover here</div>
<div id="hidden">Hidden text</div>
<div id="src" draggable="true">Drag me</div><div id="dst">Drop target</div>
<button id="alert" onclick="alert('hello there')">Alert</button>
<button id="confirm" onclick="out('confirm:'+confirm('sure?'))">Confirm</button>
<button id="prompt" onclick="out('prompt:'+prompt('name?','def'))">Prompt</button>
<label>Files <input type="file" id="file" multiple></label>
<div id="drop">Drop files</div>
<a id="dl" href="/download">Download file</a>
<a id="slowdl" href="/slowdownload">Slow download</a>
<button id="post" onclick="fetch('/api',{method:'POST',headers:{'Content-Type':'application/json','X-Test':'yes'},body:JSON.stringify({hello:'world'})}).then(r=>r.text()).then(t=>out('api:'+t))">Post</button>
<iframe id="frame" src="http://localhost:PORT/frame.html" onload="this.dataset.loaded='1'"></iframe>
<div style="height:1600px">Tall area</div>
<div id="bottom">Bottom</div>
<script>
window.clicks=0; window.keylog=[];
function out(t){document.getElementById('out').textContent=t}
document.addEventListener('keydown',e=>keylog.push((e.ctrlKey?'C-':'')+(e.shiftKey?'S-':'')+(e.altKey?'A-':'')+e.key+'/'+e.code));
document.getElementById('src').addEventListener('dragstart',e=>e.dataTransfer.setData('text/plain','payload'));
const dst=document.getElementById('dst');
dst.addEventListener('dragover',e=>e.preventDefault());
dst.addEventListener('drop',e=>{e.preventDefault();out('dropped:'+e.dataTransfer.getData('text/plain'))});
document.getElementById('file').addEventListener('change',async e=>{
  const rows=[];for(const f of e.target.files)rows.push(f.name+':'+f.type+':'+f.size+':'+(await f.text()));
  out('files:'+rows.join('|'))});
const drop=document.getElementById('drop');
drop.addEventListener('drop',async e=>{e.preventDefault();const rows=[];
  for(const f of e.dataTransfer.files)rows.push(f.name+':'+f.type+':'+f.size);
  out('drop:'+rows.join('|')+':'+e.dataTransfer.getData('text/plain'))});
console.log('main loaded'); console.warn('main warning', 42);
setTimeout(()=>{throw new Error('boom')},0);
</script></body></html>"""

FRAME = """<!doctype html><html><head><style>*{caret-color:transparent}</style></head><body>
<p>Inside frame</p>
<button id="fbtn" onclick="document.getElementById('fout').textContent='frame clicked'">Frame button</button>
<input id="finput" placeholder="frame input">
<div id="fout">frame idle</div>
<script>console.log('frame loaded')</script>
</body></html>"""

NEXT = "<!doctype html><html><head><title>Next</title></head><body><h1>Next page</h1><p>second</p></body></html>"


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    prebuilt = os.environ.get("OPENBEAR_PARITY_NODE_BIN")
    if prebuilt:
        return Path(prebuilt)
    out = tmp_path_factory.mktemp("node-bin") / "openbear-node"
    env = {**os.environ, "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=readonly", "CGO_ENABLED": "0"}
    if not env.get("HOME") and not env.get("GOCACHE"):
        env["GOCACHE"] = str(ROOT / ".cache" / "tests" / "go-build")
    subprocess.run(
        [GO, "build", "-o", str(out), "./cmd/openbear-node"], cwd=ROOT / "node", env=env, check=True
    )
    return out


async def _serve():
    app = web.Application()
    state = {}

    async def page(request):
        name = request.match_info["name"]
        body = {"main.html": MAIN, "frame.html": FRAME, "next.html": NEXT}.get(name)
        if body is None:
            raise web.HTTPNotFound()
        return web.Response(text=body.replace("PORT", str(state["port"])), content_type="text/html")

    async def api(request):
        data = await request.read()
        return web.json_response(
            {"received": data.decode(), "n": 1.5}, headers={"X-Reply": "ok"}
        )

    async def download(request):
        return web.Response(
            body=b"download-bytes\n" * 10,
            headers={"Content-Disposition": 'attachment; filename="report.txt"'},
            content_type="application/octet-stream",
        )

    async def slowdownload(request):
        response = web.StreamResponse(
            headers={
                "Content-Disposition": 'attachment; filename="slow.bin"',
                "Content-Type": "application/octet-stream",
                "Content-Length": "100000",
            }
        )
        await response.prepare(request)
        await response.write(b"x" * 1000)
        await asyncio.sleep(30)
        return response

    async def slow(request):
        await asyncio.sleep(30)
        return web.Response(text="late")

    async def favicon(request):
        return web.Response(status=204)

    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/download", download)
    app.router.add_get("/slowdownload", slowdownload)
    app.router.add_get("/slow", slow)
    app.router.add_post("/api", api)
    app.router.add_get("/{name}", page)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state["port"] = site._server.sockets[0].getsockname()[1]
    return runner, state["port"]


def _start_chrome(profile: Path):
    args = [
        CHROME,
        "--headless=new",
        "--remote-debugging-port=0",
        "--site-per-process",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--hide-scrollbars",
        "--mute-audio",
        "--window-size=1200,900",
        "about:blank",
    ]
    if os.geteuid() == 0:
        args.insert(1, "--no-sandbox")
    return subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def _devtools_url(profile: Path, proc) -> str:
    port_file = profile / "DevToolsActivePort"
    for _ in range(300):
        if proc.poll() is not None:
            raise RuntimeError("chrome exited")
        if port_file.is_file():
            lines = port_file.read_text().split("\n")
            if len(lines) >= 2 and lines[1].strip():
                return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
        await asyncio.sleep(0.05)
    raise RuntimeError("DevToolsActivePort not written")


class Run:
    """One engine executing the scenario on its own page."""

    def __init__(self, worker, cdp, base, out: Path, upload: Path):
        self.worker, self.cdp, self.base, self.out, self.upload = worker, cdp, base, out, upload
        self.target = ""
        self.steps = []
        self.files = {}
        self.n = 0

    async def call(self, label, action, params=None, *, timeout_ms=10000, artifact=None):
        params = dict(params or {})
        if action not in {"initialize", "configure"}:
            params.setdefault("targetId", self.target)
            params.setdefault("timeoutMs", timeout_ms)
        if artifact:
            self.n += 1
            params["outputPath"] = str(self.out / f"{self.n:03d}-{artifact}")
        try:
            result = await self.worker.call(action, params, timeout=params.get("timeoutMs", 10000) / 1000 + 5)
            record = {"result": result}
        except WorkerError as exc:
            if not exc.responded:
                raise
            record = {"error": str(exc), "outcome": exc.outcome, "details": exc.details}
        if artifact and isinstance(record.get("result"), dict) and record["result"].get("artifact"):
            path = Path(record["result"]["artifact"])
            self.files[label] = path.read_bytes() if path.is_file() else None
        self.steps.append((label, action, record))
        return record

    def text(self, label):
        for name, _, record in reversed(self.steps):
            if name == label:
                return record["result"]["text"]
        raise KeyError(label)

    def ref(self, label, needle):
        for line in self.text(label).split("\n"):
            if needle in line:
                match = re.search(r"\[ref=([^\]]+)\]", line)
                if match:
                    return match.group(1)
        raise AssertionError(f"{needle!r} not in snapshot {label}")


async def scenario(run: Run, endpoint: str, download_dir: Path):
    base = run.base
    init = {
        "endpoint": endpoint, "downloadHostPath": str(download_dir),
        "downloadBrowserPath": str(download_dir), "connectTimeoutS": 5, **LIMITS,
    }
    # Worker.start sends initialize; record its result for comparison.
    original = run.worker.call

    async def recording(action, params, *, timeout):
        result = await original(action, params, timeout=timeout)
        run.steps.append(("initialize", action, {"result": result}))
        return result

    run.worker.call = recording
    try:
        await run.worker.start(init)
    finally:
        del run.worker.call
    await run.call("configure", "configure", LIMITS)
    run.target = (await run.cdp.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
    c = run.call
    await c("prepare", "prepare")
    await c("goto main", "navigate", {"op": "goto", "url": base + "/main.html"})
    await c("iframe loaded", "evaluate", {"expression": (
        "new Promise(r=>{const f=document.getElementById('frame');"
        "if(f.dataset.loaded)r(true);else f.addEventListener('load',()=>r(true))})")}, artifact="v.json")
    await c("snapshot", "snapshot", {})
    await c("snapshot interactive", "snapshot", {"interactive": True})
    await c("snapshot depth", "snapshot", {"depth": 2})
    await c("snapshot truncated", "snapshot", {"maxChars": 300})
    await c("find", "find", {"text": "click me"})
    btn = run.ref("find", 'button "Click me"')
    await c("click ref", "act", {"op": "click", "target": btn})
    await c("out after click", "evaluate", {"expression": "document.getElementById('out').textContent"}, artifact="v.json")
    await c("snapshot 2", "snapshot", {})
    refs = {name: run.ref("snapshot 2", needle) for name, needle in {
        "btn": 'button "Click me"', "dbl": 'button "Double me"', "right": 'button "Right me"',
        "name": 'textbox "Name"', "email": 'textbox "Email"', "notes": 'textbox "Notes"',
        "agree": 'checkbox "Agree"', "color": 'combobox "Color"', "fbtn": 'button "Frame button"',
        "finput": 'textbox "frame input"', "src": "Drag me", "dst": "Drop target",
    }.items()}
    await c("double click", "act", {"op": "click", "target": refs["dbl"], "doubleClick": True})
    await c("right click", "act", {"op": "click", "target": refs["right"], "button": "right"})
    await c("click css modifiers", "act", {"op": "click", "target": "css=#btn", "modifiers": ["Shift"]})
    await c("out", "evaluate", {"expression": "[document.getElementById('out').textContent, window.clicks]"}, artifact="v.json")
    await c("fill", "act", {"op": "fill", "target": refs["name"], "text": "Alice"})
    await c("fill fields", "act", {"op": "fill", "fields": [
        {"target": refs["email"], "value": "a@example.test"}, {"target": refs["notes"], "value": "line1\nline2 ✓"}]})
    await c("fill empty", "act", {"op": "fill", "target": "css=#notes", "text": ""})
    await c("type slowly", "act", {"op": "type", "target": refs["notes"], "text": "Hi+A b", "slowly": True, "submit": True})
    await c("press combo", "act", {"op": "press", "target": refs["name"], "key": "Control+A"})
    await c("press shift", "act", {"op": "press", "key": "Shift+X"})
    await c("press plus", "act", {"op": "press", "key": "Control++"})
    await c("press meta-or-control", "act", {"op": "press", "key": "ControlOrMeta+ArrowLeft"})
    await c("press unknown", "act", {"op": "press", "key": "NotAKey"})
    await c("values", "evaluate", {"expression": (
        "({name:document.getElementById('name').value,email:document.getElementById('email').value,"
        "notes:document.getElementById('notes').value,keys:keylog})")}, artifact="v.json")
    await c("check", "act", {"op": "check", "target": refs["agree"], "checked": True})
    await c("check same", "act", {"op": "check", "target": refs["agree"], "checked": True})
    await c("select", "act", {"op": "select", "target": refs["color"], "values": ["Green"]})
    await c("select missing", "act", {"op": "select", "target": refs["color"], "values": ["Blue"]}, timeout_ms=1200)
    await c("hover", "act", {"op": "hover", "target": "css=#hover"})
    await c("drag", "act", {"op": "drag", "target": refs["src"], "to": refs["dst"]})
    await c("state", "evaluate", {"expression": (
        "({out:document.getElementById('out').textContent,agree:document.getElementById('agree').checked,"
        "color:document.getElementById('color').value,hover:document.getElementById('hover').textContent})")}, artifact="v.json")
    # OOPIF (127.0.0.1 page, localhost frame) interaction.
    await c("frame click", "act", {"op": "click", "target": refs["fbtn"]})
    await c("frame fill", "act", {"op": "fill", "target": refs["finput"], "text": "in frame"})
    await c("frame hover", "act", {"op": "hover", "target": refs["fbtn"]})
    await c("frame wait", "wait", {"target": refs["fbtn"], "state": "visible"})
    await c("frame evaluate", "evaluate", {"target": refs["finput"], "expression": "el => [el.value, location.host === 'localhost:' + location.port]"}, artifact="v.json")
    await c("frame capture", "capture", {"target": refs["fbtn"]}, artifact="shot.png")
    await c("frame snapshot", "find", {"text": "frame"})
    fref = run.ref("frame snapshot", 'button "Frame button"')
    await c("frame press", "act", {"op": "press", "target": run.ref("frame snapshot", 'textbox "frame input"'), "key": "End"})
    await c("frame type", "act", {"op": "type", "target": run.ref("frame snapshot", 'textbox "frame input"'), "text": "!?", "slowly": True})
    await c("frame scoped snapshot", "snapshot", {"target": fref})
    await c("frame old ref", "act", {"op": "click", "target": refs["fbtn"]})
    await c("frame iframe reload", "evaluate", {"expression": (
        "new Promise(r=>{const f=document.getElementById('frame');f.addEventListener('load',()=>r(1),{once:true});"
        "f.src=f.src})")}, artifact="v.json")
    await c("frame ref after frame reload", "act", {"op": "click", "target": fref})
    await c("scroll delta", "act", {"op": "scroll", "x": 0, "y": 400})
    await c("scroll target", "act", {"op": "scroll", "target": "css=#bottom"})
    await c("settle", "wait", {"timeMs": 300})
    await c("scrolled", "evaluate", {"expression": "window.scrollY > 0"}, artifact="v.json")
    await c("resize", "act", {"op": "resize", "width": 800, "height": 600})
    await c("emulate", "act", {"op": "emulate", "media": {"colorScheme": "dark", "reducedMotion": "reduce"}})
    await c("emulate more", "act", {"op": "emulate", "media": {"media": "print", "colorScheme": None}})
    await c("media", "evaluate", {"expression": (
        "[innerWidth, innerHeight, matchMedia('(prefers-color-scheme: dark)').matches,"
        "matchMedia('(prefers-reduced-motion: reduce)').matches, matchMedia('print').matches]")}, artifact="v.json")
    await c("emulate reset", "act", {"op": "emulate", "media": {"media": None, "reducedMotion": None}})
    await c("scroll top", "evaluate", {"expression": "function(){scrollTo(0,0);return scrollY}"}, artifact="v.json")
    # Waits.
    await c("wait visible", "wait", {"target": "css=#out", "state": "visible"})
    await c("wait hidden", "wait", {"target": "css=#hidden", "state": "hidden"})
    await c("wait attached", "wait", {"target": "css=#out", "state": "attached"})
    await c("wait detached", "wait", {"target": "css=#gone", "state": "detached"})
    await c("wait text", "wait", {"text": "parity PAGE"})
    await c("wait url", "wait", {"url": "*/main.htm[!x]"})
    await c("wait time", "wait", {"timeMs": 50})
    await c("wait timeout", "wait", {"target": "css=#never", "state": "visible"}, timeout_ms=800)
    # Captures and evaluate.
    await c("capture", "capture", {}, artifact="shot.png")
    await c("capture full", "capture", {"fullPage": True}, artifact="shot.png")
    await c("capture element", "capture", {"target": "css=#btn"}, artifact="shot.png")
    await c("evaluate number", "evaluate", {"expression": "1.5 + 2"}, artifact="v.json")
    await c("evaluate object", "evaluate", {"expression": "({b:1,a:[1,2.5,'<&>',null,true],c:{'é':'✓','\\u2028':1e21,small:1e-7}})"}, artifact="v.json")
    await c("evaluate function", "evaluate", {"expression": "async function(){return document.title}"}, artifact="v.json")
    await c("evaluate undefined", "evaluate", {"expression": "undefined"}, artifact="v.json")
    await c("evaluate big", "evaluate", {"expression": "({z:'x'.repeat(30000),a:'ü'})"}, artifact="v.json")
    await c("evaluate error", "evaluate", {"expression": "throw new Error('secret source')"}, artifact="v.json")
    await c("evaluate target", "evaluate", {"target": "css=#name", "expression": "el => el.value + '!'"}, artifact="v.json")
    # Files.
    await c("chooser closed", "files", {"op": "upload", "paths": [str(run.upload / "a.txt")]})
    await c("upload", "files", {"op": "upload", "target": "css=#file", "paths": [str(run.upload / "a.txt"), str(run.upload / "b.json")]})
    await c("upload wait", "wait", {"text": "files:"})
    await c("upload result", "evaluate", {"expression": "document.getElementById('out').textContent"}, artifact="v.json")
    await c("open chooser", "act", {"op": "click", "target": "css=#file"})
    await c("chooser upload", "files", {"op": "upload", "paths": [str(run.upload / "c.tar.gz")]})
    await c("chooser wait", "wait", {"text": "files:c.tar.gz:application/x-tar"})
    await c("chooser used", "files", {"op": "upload", "paths": [str(run.upload / "b.json")]})
    await c("upload missing", "files", {"op": "upload", "target": "css=#file", "paths": [str(run.upload / "missing.txt")]})
    await c("drop", "files", {"op": "drop", "target": "css=#drop", "paths": [str(run.upload / "c.tar.gz")], "data": {"text/plain": "dropped text"}})
    await c("files out", "wait", {"text": "drop:c.tar.gz"})
    await c("files result", "evaluate", {"expression": "document.getElementById('out').textContent"}, artifact="v.json")
    await c("drop data", "files", {"op": "drop", "target": "css=#dst", "data": {"text/plain": "only data"}})
    await c("drop data wait", "wait", {"text": "dropped:only data"})
    await c("drop stale ref", "files", {"op": "drop", "target": refs["dst"], "data": {"text/plain": "x"}})
    await c("download click", "act", {"op": "click", "target": "css=#dl"})
    await c("download wait", "wait", {"timeMs": 800})
    await c("list", "files", {"op": "list"})
    await c("save", "files", {"op": "save", "id": 1}, artifact="download.bin")
    await c("save expired", "files", {"op": "save", "id": 99}, artifact="download.bin")
    await c("slow download", "act", {"op": "click", "target": "css=#slowdl"})
    await c("slow wait", "wait", {"timeMs": 800})
    await c("list 2", "files", {"op": "list"})
    await c("cancel", "files", {"op": "cancel", "id": 2})
    await c("save cancelled", "files", {"op": "save", "id": 2}, artifact="download.bin")
    # Dialogs.
    await c("dialog none", "dialog", {"op": "inspect"})
    await c("alert", "act", {"op": "click", "target": "css=#alert"})
    await c("blocked snapshot", "snapshot", {})
    await c("dialog inspect", "dialog", {"op": "inspect"})
    await c("dialog console ok", "console", {"after": 10**6})
    await c("dialog accept", "dialog", {"op": "handle", "accept": True})
    await c("confirm", "act", {"op": "click", "target": "css=#confirm"})
    await c("confirm handle", "dialog", {"op": "handle", "accept": True})
    await c("confirm out", "evaluate", {"expression": "document.getElementById('out').textContent"}, artifact="v.json")
    await c("prompt", "act", {"op": "click", "target": "css=#prompt"})
    await c("prompt inspect", "dialog", {"op": "inspect"})
    await c("prompt handle", "dialog", {"op": "handle", "accept": True, "text": "Bob"})
    await c("prompt out", "evaluate", {"expression": "document.getElementById('out').textContent"}, artifact="v.json")
    await c("prompt dismiss", "act", {"op": "click", "target": "css=#prompt"})
    await c("prompt dismiss handle", "dialog", {"op": "handle", "accept": False})
    await c("dismiss out", "evaluate", {"expression": "document.getElementById('out').textContent"}, artifact="v.json")
    # Console and network.
    await c("post", "act", {"op": "click", "target": "css=#post"})
    await c("post wait", "wait", {"text": "api:"})
    await c("console", "console", {})
    await c("console after", "console", {"after": 2})
    await c("network", "network", {})
    rows = run.steps[-1][2]["result"]["entries"]
    api = next(r["id"] for r in rows if r["url"].endswith("/api"))
    await c("request headers", "network", {"id": api, "part": "request-headers"})
    await c("response headers", "network", {"id": api, "part": "response-headers"})
    await c("request body", "network", {"id": api, "part": "request-body"}, artifact="body.bin")
    await c("response body", "network", {"id": api, "part": "response-body"}, artifact="body.bin")
    await c("network expired", "network", {"id": 999999, "part": "request-headers"})
    await c("configure small", "configure", {**LIMITS, "maxBodyBytes": 5, "maxEventEntries": 2})
    await c("body too large", "network", {"id": api, "part": "response-body"}, artifact="body.bin")
    await c("console trimmed", "console", {})
    await c("configure restore", "configure", LIMITS)
    # Navigation, versions and stale refs.
    await c("snapshot before nav", "snapshot", {})
    old = run.ref("snapshot before nav", 'button "Click me"')
    await c("goto next", "navigate", {"url": base + "/next.html"})
    await c("stale ref", "act", {"op": "click", "target": old})
    await c("back", "navigate", {"op": "back"})
    await c("reload", "navigate", {"op": "reload"})
    await c("stale after reload", "snapshot", {"target": old})
    await c("bad ref", "act", {"op": "click", "target": "s00000000:e1"})
    await c("navigate timeout", "navigate", {"url": base + "/slow"}, timeout_ms=1500)
    await c("navigate failed", "navigate", {"url": "http://127.0.0.1:1/unreachable"})
    await c("goto again", "navigate", {"url": base + "/next.html"})
    await c("unknown action", "bogus", {})
    await c("unknown act", "act", {"op": "bogus"})
    await run.cdp.call("Target.closeTarget", {"targetId": run.target})
    await asyncio.sleep(0.3)
    await c("closed page", "snapshot", {})


VOLATILE_HEADERS = {"date", "keep-alive", "connection"}


def normalize(run: Run):
    out = []
    for label, action, record in run.steps:
        record = copy.deepcopy(record)
        text = json.dumps(record, ensure_ascii=False, sort_keys=True)
        text = text.replace(str(run.out), "<OUT>")
        text = re.sub(r"\bs[0-9a-f]{8}\b", "S", text)
        text = re.sub(r'"loaderId": "[0-9A-F]+"', '"loaderId": "L"', text)
        record = json.loads(text)
        result = record.get("result")
        if isinstance(result, dict):
            if label == "initialize":
                result["engine"] = "<engine>"
            if action in {"network"} and "entries" in result:
                result["entries"] = [
                    {k: v for k, v in row.items() if k != "id"}
                    for row in result["entries"]
                    if "favicon" not in row["url"]
                ]
                result.pop("cursor", None)
            if action == "console" and "entries" in result:
                result["entries"] = sorted(
                    ({k: v for k, v in row.items() if k != "id"} for row in result["entries"]),
                    key=lambda r: json.dumps(r, sort_keys=True),
                )
            if "headers" in result:
                result["headers"] = {
                    k: v for k, v in result["headers"].items() if k not in VOLATILE_HEADERS
                }
        out.append((label, record))
    return out


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


@pytest.fixture
async def environment(tmp_path):
    runner, port = await _serve()
    profile = tmp_path / "profile"
    profile.mkdir()
    chrome = _start_chrome(profile)
    upload = tmp_path / "upload"
    upload.mkdir()
    (upload / "a.txt").write_text("alpha")
    (upload / "b.json").write_text('{"b":1}')
    (upload / "c.tar.gz").write_bytes(b"\x1f\x8b fake")
    try:
        endpoint = await _devtools_url(profile, chrome)
        yield endpoint, f"http://127.0.0.1:{port}", upload
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        await runner.cleanup()


async def run_engine(worker, endpoint, base, upload, tmp: Path):
    out = tmp / "out"
    downloads = tmp / "downloads"
    out.mkdir(parents=True)
    downloads.mkdir()
    cdp = CDP(endpoint, 5)
    run = Run(worker, cdp, base, out, upload)
    try:
        await scenario(run, endpoint, downloads)
    finally:
        await worker.close()
        await cdp.close()
    return run


async def test_python_and_go_engines_return_the_same_results(environment, binary, tmp_path):
    endpoint, base, upload = environment
    python = await run_engine(Worker(), endpoint, base, upload, tmp_path / "python")
    go = await run_engine(Worker(command=[str(binary), "browser-engine"]), endpoint, base, upload, tmp_path / "go")
    assert python.steps[0][2]["result"]["engine"] == "python-cdp"
    assert go.steps[0][2]["result"]["engine"] == "go-cdp"
    left, right = normalize(python), normalize(go)
    assert [label for label, _ in left] == [label for label, _ in right]
    mismatches = [
        (label, a, b) for (label, a), (_, b) in zip(left, right) if a != b
    ]
    assert not mismatches, "\n".join(
        f"{label}:\n  python={json.dumps(a, ensure_ascii=False)[:1500]}\n  go    ={json.dumps(b, ensure_ascii=False)[:1500]}"
        for label, a, b in mismatches
    )
    # Artifacts are compared byte for byte (screenshots: same size and pixels).
    assert python.files.keys() == go.files.keys()
    for label, data in python.files.items():
        other = go.files[label]
        if data and data.startswith(b"\x89PNG"):
            assert png_size(data) == png_size(other), label
        assert data == other, label
    # Sanity: the scenario exercised what it claims.
    steps = dict((label, record) for label, record in left)
    assert steps["stale ref"]["error"].startswith("stale_ref")
    assert steps["blocked snapshot"]["error"].startswith("dialog_pending")
    assert steps["wait timeout"]["error"] == "action_timeout"
    assert steps["navigate timeout"]["error"] == "navigation_timeout"
    assert steps["prompt out"]["result"]["value"] == "prompt:Bob"
    assert "frame clicked" in json.dumps(steps["frame snapshot"])
    assert steps["evaluate big"]["result"]["artifact"]
