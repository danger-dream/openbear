"""Deadline/failure regression through service and CDP engine; no live browser."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app.browser.pyworker.common import BrowserError
from app.browser.worker import WorkerError
from tests import test_browser_native, test_browser_python_engine
from tests.test_browser_native import ctx, new

service = test_browser_native.service
engine = test_browser_python_engine.engine


async def test_new_url_uses_one_budget_for_connect_setup_and_navigation(service, monkeypatch):
    inst = await service._instance()
    original_sync = service._sync

    async def slow_sync(instance):
        await asyncio.sleep(0.02)
        return await original_sync(instance)

    monkeypatch.setattr(service, "_sync", slow_sync)
    result = await service.call(
        {
            "action": "page",
            "timeoutMs": 200,
            "params": {"op": "new", "url": "https://test.invalid"},
        },
        ctx(),
    )
    assert result["status"] == "ok", result
    action, params = inst.worker.calls[-1]
    assert action == "navigate" and 1 <= params["timeoutMs"] < 160
    assert sum(m == "Target.createTarget" for m, _ in inst.cdp.calls) == 1


async def test_connection_timeout_obeys_total_deadline_without_creating_page(service):
    inst = await service._instance()

    async def hung():
        await asyncio.Event().wait()

    inst.cdp.connect = AsyncMock(side_effect=hung)
    start = time.monotonic()
    result = await service.call(
        {"action": "page", "timeoutMs": 40, "params": {"op": "new", "url": "https://test.invalid"}},
        ctx(),
    )
    assert time.monotonic() - start < 0.3
    assert result["error"] == "browser_connection_timeout"
    assert result["outcome"] == "not_started" and result["phase"] == "connect_browser"
    assert not inst.cdp.calls and not inst.worker.calls


async def test_navigation_timeout_diagnoses_once_and_snapshot_fails_fast(service):
    inst = await service._instance()
    inst.worker.fail_action = "navigate"
    inst.worker.fail = WorkerError(
        "navigation_timeout",
        "unknown",
        responded=True,
        details={
            "phase": "navigation_response",
            "navigation": {"committed": False, "state": "requested"},
        },
    )
    inst.cdp.target_call = AsyncMock(
        return_value={"result": {"value": {"readyState": "loading", "url": "about:blank"}}}
    )
    result = await service.call(
        {
            "action": "page",
            "timeoutMs": 200,
            "params": {"op": "new", "url": "https://test.invalid"},
        },
        ctx(),
    )
    assert result["error"] == "navigation_timeout" and result["outcome"] == "unknown"
    assert result["diagnostics"]["browserResponsive"] is True
    assert result["diagnostics"]["documentReady"] is False
    assert inst.worker.connected and inst.failures == 0
    worker_calls = len(inst.worker.calls)
    cdp_calls = len(inst.cdp.calls)
    snapshot = await service.call({"action": "snapshot", "page": result["page"]}, ctx())
    assert snapshot["error"] == "page_not_ready" and snapshot["outcome"] == "not_started"
    assert snapshot["blockedBy"]["error"] == "navigation_timeout"
    assert len(inst.worker.calls) == worker_calls and len(inst.cdp.calls) == cdp_calls
    inst.cdp.target_call.assert_awaited_once()


async def test_read_timeout_is_failed_not_unknown_and_keeps_worker(service):
    page = await new(service)
    inst = next(iter(service.instances.values()))
    inst.worker.fail = WorkerError(
        "action_timeout",
        "failed",
        responded=True,
        details={"phase": "snapshot", "effectsPossible": False},
    )
    result = await service.call({"action": "snapshot", "page": page}, ctx())
    assert result["outcome"] == "failed" and result["phase"] == "snapshot"
    assert inst.worker.connected and inst.failures == 0
    inst.worker.fail = None
    for action in ("network", "console"):
        assert (await service.call({"action": action, "page": page}, ctx()))["status"] == "ok"


async def test_total_deadline_while_queued_does_not_poison_the_page(service):
    page = await new(service)
    record = service.pages[page]
    async with record.lock:
        result = await service.call({"action": "snapshot", "page": page, "timeoutMs": 20}, ctx())
        assert result["outcome"] == "not_started" and result["phase"] == "page_queue"
        assert record.failure is None
    assert (await service.call({"action": "snapshot", "page": page}, ctx()))["status"] == "ok"


async def test_expired_worker_setup_is_not_dispatched(service, monkeypatch):
    page = await new(service)

    async def setup(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_worker", setup)
    result = await service.call({"action": "snapshot", "page": page, "timeoutMs": 20}, ctx())
    assert result["outcome"] == "not_started" and result["phase"] == "initialize_worker"
    assert [a for a, _ in next(iter(service.instances.values())).worker.calls] == ["prepare"]


async def test_known_network_failure_allows_corrected_navigation_but_not_false_probe_success(
    service,
):
    page = await new(service)
    inst = next(iter(service.instances.values()))
    inst.worker.fail = WorkerError(
        "navigation_failed",
        "failed",
        responded=True,
        details={"navigation": {"networkError": "net::ERR_CONNECTION_REFUSED"}},
    )
    result = await service.call(
        {"action": "navigate", "page": page, "params": {"url": "https://wrong.invalid"}}, ctx()
    )
    assert result["outcome"] == "failed"
    inst.cdp.target_call = AsyncMock(
        return_value={
            "result": {"value": {"readyState": "complete", "url": "chrome-error://chromewebdata/"}}
        }
    )
    probe = await service.call(
        {"action": "recover", "page": page, "params": {"op": "probe"}}, ctx()
    )
    assert probe["pageResponsive"] is True and probe["pageReady"] is False
    assert service.pages[page].failure is not None
    inst.worker.fail = None
    result = await service.call(
        {"action": "navigate", "page": page, "params": {"url": "https://correct.invalid"}}, ctx()
    )
    assert result["status"] == "ok" and service.pages[page].failure is None
    assert inst.worker.generation == 1


async def test_probe_clears_recovered_read_failure_without_replaying(service):
    page = await new(service)
    inst = next(iter(service.instances.values()))
    inst.worker.fail = WorkerError("action_timeout", "failed", responded=True)
    await service.call({"action": "snapshot", "page": page}, ctx())
    before = len(inst.worker.calls)
    inst.cdp.target_call = AsyncMock(
        return_value={
            "result": {"value": {"readyState": "complete", "url": "https://test.invalid"}}
        }
    )
    result = await service.call(
        {"action": "recover", "page": page, "params": {"op": "probe"}}, ctx()
    )
    assert result["pageReady"] is True and len(inst.worker.calls) == before
    assert service.pages[page].failure is None
    inst.worker.fail = None
    assert (await service.call({"action": "snapshot", "page": page}, ctx()))["status"] == "ok"


@pytest.mark.parametrize("committed", [False, True])
async def test_network_failure_interrupts_response_or_dom_wait_without_replay(engine, committed):
    obj, page = engine
    entered = asyncio.Event()

    async def command(method, params=None, session=None):
        page.observations.event(
            "Network.requestWillBeSent",
            {
                "requestId": "nav",
                "loaderId": "loader",
                "type": "Document",
                "frameId": page.main_id,
                "request": {"method": "GET", "url": "https://test.invalid"},
            },
            page.session,
        )
        entered.set()
        if committed:
            page.navigation.update(state="committed", committed=True)
            return {"frameId": page.main_id, "loaderId": "loader"}
        await asyncio.Event().wait()

    obj.cdp.call.side_effect = command
    task = asyncio.create_task(
        obj.execute(
            "navigate", {"targetId": page.target, "url": "https://test.invalid", "timeoutMs": 1000}
        )
    )
    await entered.wait()
    # Failure tracking must survive network-log eviction, not depend on a row still being retained.
    page.observations.records.clear()
    page.observations.event(
        "Network.loadingFailed",
        {"requestId": "nav", "errorText": "net::ERR_CONNECTION_TIMED_OUT"},
        page.session,
    )
    with pytest.raises(BrowserError, match="navigation_failed") as error:
        await asyncio.wait_for(task, 0.2)
    assert error.value.outcome == "failed"
    assert error.value.details["navigation"]["committed"] is committed
    assert error.value.details["navigation"]["networkError"] == "net::ERR_CONNECTION_TIMED_OUT"
    obj.cdp.call.assert_awaited_once()


async def test_subresource_failure_does_not_fail_main_navigation(engine):
    obj, page = engine
    page.navigation = {"state": "requested", "committed": False, "requestId": "main"}
    page.observations.event(
        "Network.loadingFailed",
        {"requestId": "image", "errorText": "net::ERR_ABORTED"},
        page.session,
    )
    assert not page.navigation_failed.is_set()


async def test_snapshot_execution_timeout_has_read_failure_phase(engine):
    obj, page = engine

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    page.main.run = blocked
    with pytest.raises(BrowserError, match="action_timeout") as error:
        await obj.execute("snapshot", {"targetId": page.target, "timeoutMs": 20})
    assert error.value.outcome == "failed"
    assert error.value.details == {"phase": "snapshot", "effectsPossible": False}


async def test_navigation_deadline_keeps_unknown_outcome_and_commit_state(engine):
    obj, page = engine
    obj.cdp.call.return_value = {"frameId": page.main_id, "loaderId": "new"}
    with pytest.raises(BrowserError, match="navigation_timeout") as error:
        await obj.execute(
            "navigate", {"targetId": page.target, "url": "https://test.invalid", "timeoutMs": 20}
        )
    assert error.value.outcome == "unknown"
    assert error.value.details["phase"] == "dom_content_loaded"
    assert error.value.details["navigation"]["committed"] is False
    obj.cdp.call.assert_awaited_once()


@pytest.mark.parametrize("action,params", [("page", {"op": "list"}), ("recover", {"op": "probe"})])
async def test_readonly_recovery_entrypoints_honor_connection_budget(service, action, params):
    page = await new(service)
    inst = next(iter(service.instances.values()))

    async def hung():
        await asyncio.Event().wait()

    inst.cdp.connect = AsyncMock(side_effect=hung)
    start = time.monotonic()
    result = await service.call(
        {"action": action, "page": page, "params": params, "timeoutMs": 20}, ctx()
    )
    assert time.monotonic() - start < 0.3
    assert result["outcome"] == "not_started" and result["phase"] == "connect_browser"
    assert [a for a, _ in inst.worker.calls] == ["prepare"]


async def test_javascript_failure_on_responsive_page_keeps_snapshot_available(service):
    page = await new(service)
    inst = next(iter(service.instances.values()))
    inst.cdp.target_call = AsyncMock(
        return_value={"result": {"value": {"readyState": "complete", "url": "about:blank"}}}
    )
    inst.worker.fail = WorkerError("javascript_error", "unknown", responded=True)
    request = {"action": "evaluate", "page": page, "params": {"expression": "throw Error('test')"}}
    error = await service.call(request, ctx())
    assert error["outcome"] == "unknown" and "Snapshot may inspect" in error["recovery"]
    assert service.pages[page].failure is None and service.pages[page].state == "suspect"
    denied = await service.call(request, ctx())
    assert denied["outcome"] == "not_started" and "page_outcome_unknown" in denied["error"]
    assert [a for a, _ in inst.worker.calls] == ["prepare", "evaluate"]
    inst.worker.fail = None
    snapshot = await service.call({"action": "snapshot", "page": page}, ctx())
    assert snapshot["status"] == "ok" and service.pages[page].state == "ready"
    assert [a for a, _ in inst.worker.calls] == ["prepare", "evaluate", "snapshot"]
