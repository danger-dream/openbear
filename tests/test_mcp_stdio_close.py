"""Shutdown must not depend on a stalled MCP peer consuming its input pipe."""

import asyncio
import contextlib
import json
import os
import signal
import sys
from types import SimpleNamespace

import pytest

from app.config import Config
from app.mcp.errors import MCPConnectionError, MCPTimeoutError
from app.mcp.manager import MCPManager
from app.tools import processes
from app.tools.base import ToolRuntimeContext

# A real disposable subprocess, not a mocked drain: it completes discovery, then
# stops reading stdin while keeping all pipes open. Ignore INT to exercise TERM
# escalation as well as cancellation during the process-wait phase.
PAUSED_PEER = r"""
import json, signal, sys
signal.signal(signal.SIGINT, signal.SIG_IGN)
if len(sys.argv) > 1:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    msg = json.loads(line)
    if 'id' not in msg:
        continue
    method = msg['method']
    if method == 'initialize':
        result = {'protocolVersion':'2024-11-05', 'capabilities':{}}
    elif method == 'tools/list':
        result = {'tools':[{'name':'echo','inputSchema':{'type':'object'},
                           'annotations':{'readOnlyHint':True}}]}
    else:
        result = {'prompts':[]}
    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}), flush=True)
    if method == 'prompts/list':
        while True:
            signal.pause()
"""
PAYLOAD = {"payload": "x" * (2 * 1024 * 1024)}


@pytest.fixture
async def paused_manager(request):
    args = ["-u", "-c", PAUSED_PEER]
    if getattr(request, "param", False):
        args.append("ignore-term")
    config = Config.model_validate({
        "telegram": {"botToken": "test", "whitelistIds": [1]},
        "models": {"providers": {}, "primary": ""},
        "memory": {"provider": "builtin"},
        "mcp": {"enabled": True, "allowTools": ["*"], "servers": {"paused": {
            "transport": "stdio", "stdioMode": "newline", "command": sys.executable,
            "args": args, "approval": "allow",
            "connectTimeoutS": 1, "toolCallTimeoutS": 1,
        }}},
    })
    manager = MCPManager(config)
    await asyncio.wait_for(manager.start(), 3)
    transport = manager._clients["paused"].transport
    tasks = []

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    try:
        yield SimpleNamespace(manager=manager, transport=transport, spawn=spawn)
    finally:
        # Test failures must not leave the reproducer itself stuck or orphaned.
        # Only this fixture's private process group is ever signalled.
        proc = transport.proc
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        proc._transport.close()
        await asyncio.wait_for(proc.wait(), 2)
        for task in (*tasks, transport._reader_task, transport._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(
            *tasks, transport._reader_task, transport._stderr_task,
            return_exceptions=True,
        ), 2)
        processes.unregister(proc.pid)


async def finished(task, timeout=3):
    # A test deadline observes rather than cancels the close being verified.
    done, _ = await asyncio.wait({task}, timeout=timeout)
    assert task in done, "stdio cleanup exceeded its bounded shutdown budget"
    return await task


async def backpressure(transport):
    with pytest.raises(MCPTimeoutError):
        await transport.request("tools/call", PAYLOAD, timeout_s=0.03)
    assert transport.proc.stdin.transport.get_write_buffer_size() > 65536
    assert not transport._pending
    assert not transport._write_lock.locked()


def shorten_sigint_grace(monkeypatch, transport):
    # The peer ignores SIGINT; tests whose subject is not the escalation itself
    # need not sit through the production SIGINT grace window before SIGTERM.
    monkeypatch.setattr(transport, "_SIGINT_WAIT_S", 0.05)


def assert_reaped(transport):
    proc = transport.proc
    assert proc.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)
    assert proc.pid not in {entry.pid for entry in processes.active()}
    assert proc.stdin.is_closing()
    assert all(proc._transport.get_pipe_transport(fd).is_closing() for fd in (0, 1, 2))
    assert transport._reader_task.done()
    assert transport._stderr_task.done()
    assert not transport._pending
    assert not transport._write_lock.locked()


@pytest.mark.parametrize("mode", ["close", "disable_reload"])
async def test_manager_shutdown_after_tool_timeout_reaps_backpressured_peer(paused_manager, monkeypatch, mode):
    env = paused_manager
    manager, transport = env.manager, env.transport
    shorten_sigint_grace(monkeypatch, transport)
    # The config floor is a whole second; the timeout path is the same with a shorter one.
    for server_config in {id(c): c for c in (manager.mcp_config.servers["paused"], manager._clients["paused"].config)}.values():
        monkeypatch.setattr(server_config, "tool_call_timeout_s", 0.2)
    result = json.loads(await manager.call_tool(
        manager.available_tools()[0].public_name, PAYLOAD,
        ToolRuntimeContext(source="web", conversation_uuid="test"),
    ))
    assert result["error"] == "mcp_tool_timeout"
    assert transport.proc.stdin.transport.get_write_buffer_size() > 65536
    assert not transport._write_lock.locked()
    if mode == "close":
        close = env.spawn(manager.close())
    else:
        disabled = manager.config.model_copy(deep=True)
        disabled.mcp.enabled = False
        close = env.spawn(manager.reload(disabled))
    await finished(close)
    assert_reaped(transport)
    assert not manager._clients
    # Idempotency is completion, not merely an early _closed flag.
    await asyncio.wait_for(transport.close(), 0.2)
    assert_reaped(transport)


async def test_shutdown_budget_includes_waiting_for_request_write_lock(paused_manager, monkeypatch):
    env = paused_manager
    transport = env.transport
    shorten_sigint_grace(monkeypatch, transport)
    request = env.spawn(transport.request("tools/call", PAYLOAD, timeout_s=30))
    async with asyncio.timeout(1):
        while not transport._write_lock.locked():
            await asyncio.sleep(0)
    # The request still owns the lock in drain; cancellation of its response
    # Future alone cannot wake that drain. close must reach process termination.
    await finished(env.spawn(transport.close()))
    with pytest.raises((MCPConnectionError, asyncio.CancelledError)):
        await finished(request, timeout=1)
    assert_reaped(transport)


@pytest.mark.parametrize("stage", ["notification", "termination"])
async def test_cancel_close_waits_for_cleanup_and_other_closers_join(paused_manager, monkeypatch, stage):
    env = paused_manager
    transport = env.transport
    await backpressure(transport)
    entered = asyncio.Event()
    if stage == "notification":
        # Termination keeps the real grace window: that case cancels during it.
        shorten_sigint_grace(monkeypatch, transport)
        original = transport.notify

        async def observed(method, params=None):
            if params and params.get("reason") == "client_shutdown":
                entered.set()
            await original(method, params)

        monkeypatch.setattr(transport, "notify", observed)
    else:
        # This case cancels during the INT grace window, so it keeps one, but 0.5 s is ample
        # for the few loop turns below (the production 1.5 s only lengthened the test).
        monkeypatch.setattr(transport, "_SIGINT_WAIT_S", 0.5)
        original = transport._terminate_process

        async def observed(proc):
            entered.set()
            await original(proc)

        monkeypatch.setattr(transport, "_terminate_process", observed)

    first = env.spawn(transport.close())
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel("cancel during " + stage)
    await asyncio.sleep(0)
    second = env.spawn(transport.close())
    await asyncio.sleep(0.02)
    # A second cancellation must not cancel the shared resource-cleanup task.
    first.cancel("cancel cleanup again")
    assert not first.done()
    assert not second.done(), "_closed must not hide an unfinished close"
    with pytest.raises(asyncio.CancelledError):
        await finished(first)
    # Cancellation is reported only after the resources have been released.
    assert_reaped(transport)
    await finished(second)
    assert_reaped(transport)


async def test_shutdown_notification_failure_still_reaps_peer(paused_manager, monkeypatch):
    env = paused_manager
    transport = env.transport
    shorten_sigint_grace(monkeypatch, transport)
    await backpressure(transport)

    async def failed_notify(method, params=None):
        raise OSError("simulated notification failure")

    monkeypatch.setattr(transport, "notify", failed_notify)
    await finished(env.spawn(transport.close()))
    assert_reaped(transport)


@pytest.mark.parametrize("paused_manager", [True], indirect=True)
async def test_stubborn_backpressured_peer_is_killed_and_reaped(paused_manager, monkeypatch):
    env = paused_manager
    # The peer ignores INT and TERM; the escalation is the same with a short INT grace.
    # (The 2 s TERM grace is a literal in _terminate_process and cannot be shortened here.)
    shorten_sigint_grace(monkeypatch, env.transport)
    await backpressure(env.transport)
    await finished(env.spawn(env.transport.close()), timeout=5.5)
    assert env.transport.proc.returncode == -signal.SIGKILL
    assert_reaped(env.transport)


async def test_cleanup_error_still_closes_pipes_and_close_can_retry(paused_manager, monkeypatch):
    env = paused_manager
    transport = env.transport
    original = transport._terminate_process
    attempts = 0

    async def fail_once(proc):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated termination error")
        await original(proc)

    monkeypatch.setattr(transport, "_terminate_process", fail_once)
    with pytest.raises(OSError, match="simulated termination error"):
        await finished(env.spawn(transport.close()))
    # The finally path still force-closes pipes and reaps the private child.
    assert_reaped(transport)
    assert transport._closed
    await finished(env.spawn(transport.close()))
    assert attempts == 2, "a failed close must not be hidden by _closed"
    assert_reaped(transport)
