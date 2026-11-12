"""Browser observation after act/navigate, steps, evaluate waitFor and argument usage.

Protocol doubles cover the service contract; the last test drives a real Chrome through
both the Python worker and the Go node engine, which run the same service code."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess

import pytest
from aiohttp import web

from app.browser.service import BrowserService
from app.browser.worker import Worker, WorkerError
from tests.test_browser_engine_parity import _devtools_url, _start_chrome, binary  # noqa: F401
from tests.test_browser_native import FakeCDP, config, ctx, new

BEFORE = '- generic [active] [ref=e1]:\n  - textbox "Name" [ref=e2]\n  - button "Save" [ref=e3]\n  - paragraph [ref=e4]: idle'
AFTER = (
    '- generic [ref=e1]:\n  - textbox "Name" [active] [ref=e2]: Ada\n  - button "Save" [ref=e3]\n'
    '  - paragraph [ref=e4]: Saved\n  - button "Next" [ref=e5]'
)


class ScriptedWorker:
    """Answers like the engine: versioned snapshots of `text`, act results, scripted failures."""

    def __init__(self, python=None):
        self.connected = False
        self.generation = 0
        self.calls = []
        self.text = BEFORE
        self.url = "https://site.test/"
        self.script = {}
        self.version = 0

    async def start(self, params):
        self.connected = True
        self.generation += 1

    async def call(self, action, params, **kwargs):
        self.calls.append((action, dict(params)))
        if action in self.script:
            reply = self.script[action](params)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        if action == "prepare":
            return {"ready": True, "url": "about:blank"}
        if action == "snapshot":
            self.version += 1
            version = f"s{self.version:08x}"
            text = self.text.replace("[ref=", f"[ref={version}:")
            return {"snapshotId": version, "text": text, "truncated": False, "url": self.url}
        if action == "navigate":
            return {"url": params["url"], "statusCode": 200}
        if action in {"wait", "evaluate"}:
            return {"ready": True} if action == "wait" else {"value": 1}
        return {"completed": True, "url": self.url, "dialog": None, "downloads": []}

    async def close(self):
        self.connected = False

    def sent(self, action):
        return [p for a, p in self.calls if a == action]


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setattr("app.browser.service.CDP", FakeCDP)
    monkeypatch.setattr("app.browser.connection.CDP", FakeCDP)
    monkeypatch.setattr("app.browser.service.Worker", ScriptedWorker)
    svc = BrowserService(config(tmp_path), str(tmp_path / "workspace"))
    assert (await svc.validate_connection())["ok"]
    yield svc
    await svc.close()


async def opened(svc):
    page = await new(svc)
    return page, next(iter(svc.instances.values())).worker


def ref(result, label):
    text = result["text"] if isinstance(result.get("text"), str) else "\n".join(result["elements"])
    return re.search(re.escape(label) + r"[^\n]*?\[ref=([^\]]+)\]", text)[1]


async def test_act_reports_changed_region_with_refs_usable_next(service):
    page, worker = await opened(service)
    shot = await service.call({"action": "snapshot", "page": page}, ctx())
    save = ref(shot, 'button "Save"')
    worker.text = AFTER
    result = await service.call({"action": "act", "page": page, "params": {"op": "click", "target": save}}, ctx())
    assert result["status"] == "ok" and result["outcome"] == "completed"
    assert "observe" not in worker.sent("act")[0] and worker.sent("act")[0]["target"] == save
    seen = result["observation"]
    latest = seen["snapshotId"]
    assert seen["changed"] is True and seen["url"] == "https://site.test/" and "title" in seen
    # Focus moving is not a change; the new value, text and button are.
    assert seen["elements"] == [f'textbox "Name" [active] [ref={latest}:e2]: Ada', f'button "Next" [ref={latest}:e5]']
    assert seen["text"] == [f"paragraph [ref={latest}:e4]: Saved"] and seen["removed"] == 2
    # The returned ref works as is, and the earlier ref of an unchanged element is carried over.
    await service.call({"action": "act", "page": page, "params": {"op": "click", "target": ref(seen, 'button "Next"'), "observe": "none"}}, ctx())
    await service.call({"action": "act", "page": page, "params": {"op": "click", "target": save, "observe": "none"}}, ctx())
    assert [p["target"] for p in worker.sent("act")[1:]] == [f"{latest}:e5", f"{latest}:e3"]
    # Once the element's name changes the old ref is left for the engine to reject.
    worker.text = AFTER.replace('"Save"', '"Submit"')
    await service.call({"action": "snapshot", "page": page}, ctx())
    await service.call({"action": "act", "page": page, "params": {"op": "click", "target": save, "observe": "none"}}, ctx())
    assert worker.sent("act")[-1]["target"] == save


async def test_an_observation_out_of_time_leaves_the_action_completed(service):
    page, worker = await opened(service)
    service.cfg.action_timeout_s = 0.2
    original = worker.call

    async def stuck(action, params, **kwargs):
        if action == "snapshot":
            await asyncio.sleep(30)
        return await original(action, params, **kwargs)
    worker.call = stuck
    result = await service.call({"action": "act", "page": page, "params": {"op": "click", "target": "css=button"}}, ctx())
    assert result["status"] == "ok" and result["outcome"] == "completed"
    assert result["observation"] == {"error": "observe_timeout"}
    assert service.pages[page].failure is None
    worker.call = original
    again = await service.call({"action": "act", "page": page, "params": {"op": "press", "key": "Enter", "observe": "none"}}, ctx())
    assert again["status"] == "ok"


async def test_observe_none_snapshot_and_navigation(service):
    page, worker = await opened(service)
    result = await service.call({"action": "act", "page": page, "params": {"op": "press", "key": "Enter", "observe": "none"}}, ctx())
    assert "observation" not in result and not worker.sent("snapshot")
    result = await service.call({"action": "act", "page": page, "params": {"op": "press", "key": "Enter", "observe": "snapshot"}}, ctx())
    assert set(result["observation"]) == {"snapshotId", "text", "truncated", "url"}
    # Without an earlier full snapshot nothing is known to have changed.
    result = await service.call({"action": "act", "page": page, "params": {"op": "press", "key": "Enter"}}, ctx())
    assert result["observation"]["changed"] is False
    old = result["observation"]["snapshotId"]
    worker.url = "https://site.test/next"
    result = await service.call({"action": "navigate", "page": page, "params": {"url": "https://site.test/next"}}, ctx())
    seen = result["observation"]
    assert seen["navigated"] is True and seen["changed"] is True and len(seen["elements"]) == 2
    # Refs of the previous document never carry over.
    await service.call({"action": "act", "page": page, "params": {"op": "click", "target": f"{old}:e3", "observe": "none"}}, ctx())
    assert worker.sent("act")[-1]["target"] == f"{old}:e3"


async def test_first_observation_without_baseline_lists_page_elements(service):
    page, _ = await opened(service)
    result = await service.call({"action": "act", "page": page, "params": {"op": "press", "key": "Tab"}}, ctx())
    assert result["observation"]["changed"] is None and len(result["observation"]["elements"]) == 2


async def test_a_click_that_leaves_the_page_is_a_navigation_without_a_full_snapshot_before(service):
    page, worker = await opened(service)

    def find(params):
        worker.version += 1
        version = f"s{worker.version:08x}"
        return {"snapshotId": version, "text": worker.text.replace("[ref=", f"[ref={version}:"), "truncated": False, "url": worker.url}
    worker.script["find"] = find
    # Only a find was taken: it is no baseline, but it saw the URL.
    found = await service.call({"action": "find", "page": page, "params": {"text": "Save"}}, ctx())
    worker.url = "https://other.test/landing"
    result = await service.call({"action": "act", "page": page, "params": {"op": "click", "target": ref(found, 'button "Save"')}}, ctx())
    seen = result["observation"]
    assert seen["navigated"] is True and seen["changed"] is True and seen["url"] == "https://other.test/landing"
    # Staying on the document (a fragment change included) is no navigation.
    worker.url = "https://other.test/landing#part"
    result = await service.call({"action": "act", "page": page, "params": {"op": "press", "key": "Tab"}}, ctx())
    assert "navigated" not in result["observation"]


async def test_steps_run_in_order_with_one_observation(service):
    page, worker = await opened(service)
    shot = await service.call({"action": "snapshot", "page": page}, ctx())
    worker.text = AFTER
    result = await service.call({"action": "steps", "page": page, "params": {"steps": [
        {"action": "act", "op": "fill", "target": ref(shot, 'textbox "Name"'), "text": "Ada"},
        {"action": "act", "op": "click", "target": ref(shot, 'button "Save"')},
        {"action": "wait", "text": "Saved", "timeoutMs": 3000},
    ]}}, ctx())
    assert result["status"] == "ok" and result["outcome"] == "completed" and result["completedSteps"] == 3
    assert [(s["action"], s.get("op"), s["outcome"]) for s in result["steps"]] == [
        ("act", "fill", "completed"), ("act", "click", "completed"), ("wait", None, "completed")]
    assert [a for a, _ in worker.calls] == ["prepare", "snapshot", "act", "act", "wait", "snapshot"]
    # Each step keeps its own deadline inside the shared one.
    assert worker.sent("wait")[0]["timeoutMs"] <= 3000 and worker.sent("act")[0]["timeoutMs"] <= 20000
    assert "Next" in "\n".join(result["observation"]["elements"])


async def test_steps_stop_at_the_first_step_not_completed(service):
    page, worker = await opened(service)
    calls = iter([None, WorkerError("element_not_editable", "failed", responded=True)])

    def act(params):
        return next(calls) or {"completed": True, "url": worker.url, "dialog": None, "downloads": []}

    worker.script["act"] = act
    steps = [{"action": "act", "op": "click", "target": "css=#a"}, {"action": "act", "op": "fill", "target": "css=#b", "text": "x"},
             {"action": "act", "op": "click", "target": "css=#c"}]
    result = await service.call({"action": "steps", "page": page, "params": {"steps": steps}}, ctx())
    assert result["outcome"] == "failed" and result["error"] == "element_not_editable"
    assert result["completedSteps"] == 1 and result["stoppedAt"] == 1 and len(result["steps"]) == 2
    assert [p["target"] for p in worker.sent("act")] == ["css=#a", "css=#b"] and "observation" in result

    # An unconfirmed step is reported as such, blocks the page and nothing after it runs.
    worker.script["act"] = lambda params: TimeoutError()
    result = await service.call({"action": "steps", "page": page, "params": {"steps": steps[:2]}}, ctx())
    assert result["outcome"] == "unknown" and result["stoppedAt"] == 0 and len(worker.sent("act")) == 3
    assert result["observation"] == {"skipped": "page_not_ready"}
    again = await service.call({"action": "steps", "page": page, "params": {"steps": steps[:1]}}, ctx())
    assert again["outcome"] == "not_started" and again["steps"][0]["error"] == "page_not_ready"
    assert len(worker.sent("act")) == 3


async def test_steps_stop_when_a_dialog_stays_open(service):
    page, worker = await opened(service)
    dialog = {"type": "confirm", "message": "Sure?", "defaultValue": ""}
    worker.script["act"] = lambda params: {"completed": False, "pendingDialog": True, "url": worker.url, "dialog": dialog, "downloads": []}
    result = await service.call({"action": "steps", "page": page, "params": {"steps": [
        {"action": "act", "op": "click", "target": "css=#ask"}, {"action": "act", "op": "click", "target": "css=#next"}]}}, ctx())
    assert result["error"] == "dialog_pending" and result["outcome"] == "failed" and result["completedSteps"] == 1
    assert len(worker.sent("act")) == 1 and not worker.sent("snapshot")
    assert result["observation"] == {"dialog": dialog, "skipped": "dialog_pending"}


async def test_steps_are_validated_before_any_runs(service):
    page, worker = await opened(service)
    result = await service.call({"action": "steps", "page": page, "params": {"steps": [
        {"action": "act", "op": "click", "target": "css=#a"}, {"action": "act", "op": "check", "target": "css=#b"}]}}, ctx())
    assert result["error"] == "invalid_browser_arguments" and result["outcome"] == "not_started"
    assert "steps[1] act: checked_required" in result["detail"][0]["msg"]
    assert result["usage"]["action"] == "steps" and result["stepUsage"]["example"]["params"]["op"] == "check"
    for bad in ({"action": "snapshot"}, {"action": "act", "op": "click", "target": "css=#a", "observe": "none"}):
        result = await service.call({"action": "steps", "page": page, "params": {"steps": [bad]}}, ctx())
        assert result["error"] == "invalid_browser_arguments"
    assert [a for a, _ in worker.calls] == ["prepare"]


async def test_evaluate_wait_for(service):
    page, worker = await opened(service)
    args = {"action": "evaluate", "page": page, "params": {"expression": "() => 1", "waitFor": {"text": "Ready", "timeoutMs": 500}}}
    result = await service.call(args, ctx())
    assert result["status"] == "ok" and result["value"] == 1 and "waitedMs" in result
    assert worker.sent("wait")[0]["text"] == "Ready" and worker.sent("wait")[0]["timeoutMs"] <= 500
    assert [a for a, _ in worker.calls][-2:] == ["wait", "evaluate"]

    worker.script["wait"] = lambda params: WorkerError("action_timeout", "failed", responded=True)
    result = await service.call(args, ctx())
    assert result["error"] == "wait_for_timeout" and result["outcome"] == "not_started"
    assert len(worker.sent("evaluate")) == 1
    # A condition that did not hold is not a broken page.
    assert service.pages[page].failure is None
    assert (await service.call({"action": "snapshot", "page": page}, ctx()))["status"] == "ok"

    answers = iter([{"value": False}, {"value": True}, {"value": "done"}])
    worker.script["evaluate"] = lambda params: next(answers)
    result = await service.call({"action": "evaluate", "page": page, "params": {
        "expression": "() => 'done'", "waitFor": {"expression": "document.querySelector('#x')"}}}, ctx())
    assert result["value"] == "done"
    probes = [p["expression"] for p in worker.sent("evaluate")[1:]]
    assert "document.querySelector('#x')" in probes[0] and "!!(await v)" in probes[0] and probes[2] == "() => 'done'"

    for wait_for in ({"text": "a", "url": "b"}, {"state": "visible", "url": "x"}, {"text": "a", "other": 1}):
        result = await service.call({"action": "evaluate", "page": page, "params": {"expression": "1", "waitFor": wait_for}}, ctx())
        assert result["error"] == "invalid_browser_arguments" and result["usage"]["action"] == "evaluate"


async def test_argument_errors_and_describe_carry_usage(service):
    page, _ = await opened(service)
    result = await service.call({"action": "act", "page": page, "params": {"op": "fill"}}, ctx())
    usage = result["usage"]
    assert result["error"] == "invalid_browser_arguments" and "observe" in usage["params"]
    assert usage["example"] == {"action": "act", "page": "<page handle>", "params": {"op": "fill", "target": "s1a2b3c4:f1e5", "text": "value"}}
    assert "steps" in (await service.call({"action": "nope"}, ctx()))["usage"]["actions"]
    # A managed instance offers no server nodes, so its usage leaves node out.
    result = await service.call({"action": "page", "params": {"op": "new", "bogus": 1}}, ctx())
    assert "node" not in result["usage"]["params"] and "example" in result["usage"]
    described = await service.call({"action": "describe", "params": {"action": "steps"}}, ctx())
    assert described["usage"]["example"]["params"]["steps"][0]["action"] == "act"


CHROME = shutil.which("google-chrome")
FORM = """<!doctype html><title>Form</title>
<label>Name <input id="name"></label>
<button id="save" onclick="setTimeout(()=>{out.textContent='Saved '+document.getElementById('name').value;
  const b=document.createElement('button');b.textContent='Next';document.body.append(b)},300)">Save</button>
<p id="out"></p><p id="late"></p>
<script>setTimeout(()=>{late.textContent='Late text'},400)</script>"""


@pytest.mark.skipif(not CHROME or not shutil.which("go"), reason="needs google-chrome and Go")
@pytest.mark.parametrize("engine", ["python", "go"])
async def test_real_browser_through_either_engine(engine, request, tmp_path, monkeypatch):
    site = web.Application()
    site.router.add_get("/", lambda _: web.Response(text=FORM, content_type="text/html"))
    runner = web.AppRunner(site)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    if engine == "go":
        command = [str(request.getfixturevalue("binary")), "browser-engine"]
        monkeypatch.setattr("app.browser.service.Worker", lambda: Worker(command=command))
    profile = tmp_path / "profile"
    profile.mkdir()
    chrome = _start_chrome(profile)
    svc = None
    try:
        endpoint = await _devtools_url(profile, chrome)
        svc = BrowserService(config(tmp_path, mainEndpoint=endpoint), str(tmp_path / "workspace"))
        assert (await svc.validate_connection())["ok"]

        async def call(action, page=None, **params):
            return await svc.call({"action": action, **({"page": page} if page else {}), "params": params}, ctx())

        page = (await call("page", op="new", url=f"http://127.0.0.1:{port}/"))["page"]
        shot = await call("snapshot", page)
        result = await call("steps", page, steps=[
            {"action": "act", "op": "fill", "target": ref(shot, 'textbox "Name"'), "text": "Ada"},
            {"action": "act", "op": "click", "target": ref(shot, 'button "Save"')},
            {"action": "wait", "text": "Saved Ada"},
        ])
        assert result["outcome"] == "completed", result
        seen = result["observation"]
        assert seen["title"] == "Form" and seen["changed"] is True, seen
        assert 'paragraph [ref=' in "\n".join(seen["text"]) and "Saved Ada" in "\n".join(seen["text"])
        hovered = await call("act", page, op="hover", target=ref(seen, 'button "Next"'))
        assert hovered["outcome"] == "completed", hovered
        # The Save ref from the first snapshot still names the same button.
        again = await call("act", page, op="hover", target=ref(shot, 'button "Save"'), observe="none")
        assert again["outcome"] == "completed" and "observation" not in again, again
        late = await call("evaluate", page, expression="() => late.textContent", waitFor={"text": "Late text"})
        assert late["value"] == "Late text", late
        counted = await call("evaluate", page, expression="() => 2", waitFor={"expression": "() => !!document.querySelector('#out').textContent"})
        assert counted["value"] == 2, counted
        missed = await call("evaluate", page, expression="() => window.ran = 1", waitFor={"text": "Never", "timeoutMs": 300})
        assert missed["error"] == "wait_for_timeout", missed
        assert (await call("evaluate", page, expression="() => window.ran === undefined"))["value"] is True
    finally:
        if svc:
            await svc.close()
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        await runner.cleanup()


@pytest.mark.parametrize('change', ['url', 'same_url_reload', 'iframe_navigation'])
async def test_click_navigation_rejects_old_refs_instead_of_rebinding(service, change):
    page, worker = await opened(service)
    document = ['document-before']
    original = worker.call

    async def call(action, params, **kwargs):
        if action == 'act' and params.get('target', '').startswith('s'):
            # Match the real worker's latest-snapshot gate, not the fake's
            # default unconditional success. The bug rewrites this old prefix.
            if not params['target'].startswith(f's{worker.version:08x}:'):
                raise WorkerError('stale_ref: take a new snapshot', 'failed', responded=True)
        result = await original(action, params, **kwargs)
        if action == 'snapshot' and change != 'url':
            result['documentId'] = document[0]
        return result
    worker.call = call
    before = await service.call({'action': 'snapshot', 'page': page}, ctx())
    old = ref(before, 'button "Save"')
    if change == 'url':
        worker.url = 'https://site.test/another-object'
    else:
        document[0] = 'document-after'
    # A click, not Browser navigate, caused the document/iframe change. Its
    # observation sees identical role/name/ref numbers belonging to new nodes.
    clicked = await service.call({'action': 'act', 'page': page, 'params': {'op': 'click', 'target': old}}, ctx())
    assert clicked['outcome'] == 'completed'
    assert before['snapshotId'] not in service.pages[page].snapshots
    rejected = await service.call({'action': 'act', 'page': page, 'params': {'op': 'click', 'target': old, 'observe': 'none'}}, ctx())
    assert rejected['outcome'] == 'failed' and 'stale_ref' in rejected['error']


async def test_worker_restart_discards_cross_snapshot_ref_maps(service):
    page, worker = await opened(service)
    before = await service.call({'action': 'snapshot', 'page': page}, ctx())
    old = ref(before, 'button "Save"')
    await worker.close()
    await service.call({'action': 'snapshot', 'page': page}, ctx())
    assert before['snapshotId'] not in service.pages[page].snapshots
    assert service._fresh_refs(service.pages[page], {'target': old}) == {'target': old}
