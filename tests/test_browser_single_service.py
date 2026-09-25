"""Single configured service and verified page creation; protocol doubles only."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.browser.config import BrowserConfig
from app.browser.service import BrowserService
from app.browser.worker import WorkerError
from app.settings.specs import SPECS
from app.tools.base import ToolRegistry
from app.tools.browser import register_browser_tool
from tests import test_browser_native, test_browser_python_engine
from tests.test_browser_native import config, ctx, new

service = test_browser_native.service
engine = test_browser_python_engine.engine


def test_legacy_configuration_migrates_without_changing_endpoint_or_limits():
    legacy = {
        "mainEndpoint": "http://browser.invalid:9222?fingerprint=existing",
        "mainDownloadHostPath": "/host/downloads",
        "mainDownloadBrowserPath": "/browser/downloads",
        "actionTimeoutS": 37,
        "maxPagesPerOwner": 8,
        "defaultMode": "isolated",
        "executablePath": "/old/chrome",
        "headless": True,
        "noSandbox": True,
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
        "viewportWidth": 1500,
        "viewportHeight": 1000,
        "maxIsolatedInstances": 3,
        "idleTimeoutS": 900,
        "nodeCommand": "/old/node",
    }
    cfg = BrowserConfig.model_validate(legacy)
    saved = cfg.model_dump(by_alias=True)
    for key in (
        "mainEndpoint",
        "mainDownloadHostPath",
        "mainDownloadBrowserPath",
        "actionTimeoutS",
        "maxPagesPerOwner",
    ):
        assert saved[key] == legacy[key]
    for key in set(legacy) - set(saved):
        assert "browser." + key not in SPECS
    assert legacy["defaultMode"] == "isolated"  # Input is not mutated.
    with pytest.raises(ValidationError):
        BrowserConfig.model_validate({"unknownLaunchOption": True})


async def test_legacy_isolated_config_without_service_does_not_guess_endpoint(tmp_path):
    svc = BrowserService(
        config(tmp_path, mainEndpoint="", defaultMode="isolated", executablePath="/old/chrome"),
        str(tmp_path / "workspace"),
    )
    try:
        result = await svc.call({"action": "page", "params": {"op": "new"}}, ctx())
        assert result["outcome"] == "not_started"
        assert "main_endpoint_missing" in result["error"]
        assert not svc.instances and not svc.pages
    finally:
        await svc.close()


@pytest.mark.parametrize(
    "params,reason",
    [
        (
            {"op": "new", "mode": "isolated", "url": "https://test.invalid"},
            "isolated_browser_removed",
        ),
        ({"op": "release"}, "isolated_release_removed"),
    ],
)
async def test_retired_requests_are_rejected_before_connection(service, params, reason):
    result = await service.call({"action": "page", "params": params}, ctx())
    assert result["outcome"] == "not_started"
    assert reason in str(result["detail"])
    assert not service.instances and not service.pages


async def test_two_owners_and_legacy_main_use_same_service_without_touching_old_page(service):
    first = await service.call(
        {"action": "page", "params": {"op": "new", "mode": "main"}}, ctx("a")
    )
    second = await new(service, "b")
    assert first["ready"] is True
    assert len(service.instances) == 1
    inst = next(iter(service.instances.values()))
    assert inst.cdp.endpoint == service.cfg.main_endpoint
    assert inst.cdp.targets["old"]["url"] == "https://old.test"
    assert [p["targetId"] for action, p in inst.worker.calls if action == "prepare"] == [
        service.pages[first["page"]].target,
        service.pages[second].target,
    ]
    assert inst.worker.generation == 1
    for page, wrong_owner in ((first["page"], "b"), (second, "a")):
        denied = await service.call({"action": "snapshot", "page": page}, ctx(wrong_owner))
        assert denied["outcome"] == "not_started" and "not_owned" in denied["error"]
    assert [action for action, _ in inst.worker.calls] == ["prepare", "prepare"]


async def test_new_url_prepares_exactly_once_before_navigation(service):
    result = await service.call(
        {"action": "page", "params": {"op": "new", "url": "https://test.invalid/exact-path"}}, ctx()
    )
    assert result["status"] == "ok"
    inst = next(iter(service.instances.values()))
    assert [action for action, _ in inst.worker.calls] == ["prepare", "navigate"]
    assert inst.worker.calls[-1][1]["url"] == "https://test.invalid/exact-path"
    assert {params["targetId"] for _, params in inst.worker.calls} == {
        service.pages[result["page"]].target
    }


@pytest.mark.parametrize("failure", ["initialize", "prepare"])
async def test_failed_preparation_keeps_created_handle_and_never_navigates(service, failure):
    inst = await service._instance()
    if failure == "initialize":
        inst.worker.init_fail = True
    else:
        inst.worker.fail_action = "prepare"
        inst.worker.fail = WorkerError(
            "binding_failed", "failed", responded=True, details={"phase": "bind_page"}
        )
    result = await service.call(
        {"action": "page", "params": {"op": "new", "url": "https://test.invalid"}}, ctx()
    )
    assert result["status"] == "error" and result["outcome"] == "failed"
    assert result["pageCreated"] is True and result["navigationStarted"] is False
    assert result["phase"] == ("initialize_worker" if failure == "initialize" else "bind_page")
    assert result["page"] in service.pages
    assert sum(m == "Target.createTarget" for m, _ in inst.cdp.calls) == 1
    assert all(a != "navigate" for a, _ in inst.worker.calls)
    before = len(inst.worker.calls)
    blocked = await service.call({"action": "snapshot", "page": result["page"]}, ctx())
    assert blocked["error"] == "page_not_ready" and len(inst.worker.calls) == before
    assert (await service.call({"action": "page", "params": {"op": "list"}}, ctx()))[
        "status"
    ] == "ok"


async def test_new_page_initialization_timeout_uses_total_budget_and_keeps_handle(service):
    inst = await service._instance()

    async def hang(_):
        await asyncio.Event().wait()

    inst.worker.start = hang
    started = time.monotonic()
    result = await service.call(
        {"action": "page", "timeoutMs": 30, "params": {"op": "new", "url": "https://test.invalid"}},
        ctx(),
    )
    assert time.monotonic() - started < 0.3
    assert result["outcome"] == "failed" and result["phase"] == "initialize_worker"
    assert result["pageCreated"] is True and result["navigationStarted"] is False
    assert result["page"] in service.pages and not inst.worker.calls


async def test_stuck_owned_page_does_not_block_new_page_or_control(service):
    first = await new(service)
    inst = next(iter(service.instances.values()))
    call = inst.worker.call
    started = asyncio.Event()
    release = asyncio.Event()

    async def worker(action, params, **kw):
        if action == "snapshot" and params["targetId"] == service.pages[first].target:
            started.set()
            await release.wait()
        return await call(action, params, **kw)

    inst.worker.call = worker
    task = asyncio.create_task(service.call({"action": "snapshot", "page": first}, ctx()))
    try:
        await started.wait()
        second = await new(service, "b")
        assert second != first
        assert (await service.call({"action": "snapshot", "page": second}, ctx("b")))[
            "status"
        ] == "ok"
        assert (await service.call({"action": "page", "params": {"op": "list"}}, ctx()))[
            "status"
        ] == "ok"
    finally:
        release.set()
        await task


async def test_saved_main_ownership_survives_and_retired_handles_are_not_restored(service):
    page = await new(service)
    state = service.state_dir / "pages.json"
    rows = json.loads(state.read_text())
    rows.append(
        {
            "id": "retired",
            "instance": "task-old",
            "epoch": "old",
            "target": "old",
            "owner": "conversation:a",
            "state": "ready",
        }
    )
    state.write_text(json.dumps(rows))
    service.pages.clear()
    service._load_pages()
    assert page in service.pages and "retired" not in service.pages
    assert service.pages[page].owner == "conversation:a"


async def test_prepare_validates_execution_context_without_visiting_other_pages(engine):
    obj, page = engine
    page.main.run = AsyncMock(return_value="complete")
    result = await obj.execute("prepare", {"targetId": page.target})
    assert result == {"ready": True, "url": page.url}
    page.main.run.assert_awaited_once_with("function(){return this.document.readyState}")
    obj.cdp.call.assert_not_called()


def test_public_tool_describes_single_service_and_private_prepare_is_not_callable(service):
    from app.browser.contract import Request

    registry = ToolRegistry()
    register_browser_tool(registry, service)
    spec = registry.schemas()[0]
    assert "mode" not in spec["parameters"]["properties"]["params"]["properties"]
    assert "no local browser is launched" in spec["description"]
    assert "browser's network" in spec["description"]
    with pytest.raises(ValidationError):
        Request.model_validate({"action": "prepare", "page": "p1"})
