"""Real disposable stdio peers exercise message limits and reader lifecycle."""

import asyncio
import json
import sys
import time

import pytest

from app.config import MCPServerConfig
from app.mcp.errors import MCPConnectionError, MCPTimeoutError
from app.mcp.transports import StdioJSONRPCTransport

PEER = r"""
import sys,json
for line in sys.stdin:
    req=json.loads(line)
    if 'id' not in req:
        continue
    method=req['method']
    if method=='hang': continue
    if method=='bad':
        print('not JSON',flush=True);continue
    if method=='large': value='x'*100000
    elif method=='oversized': value='x'*300000
    else: value='ok'
    print(json.dumps({'id':req['id'],'jsonrpc':'2.0','result':value}),flush=True)
"""


def transport(limit=200000, peer=PEER):
    return StdioJSONRPCTransport(
        "test",
        MCPServerConfig(
            command=sys.executable,
            args=["-u", "-c", peer],
            stdioMode="newline",
            maxMessageBytes=limit,
        ),
    )


async def test_large_json_response_does_not_kill_reader():
    t = transport()
    try:
        await t.connect()
        assert len(await t.request("large", timeout_s=2)) == 100000
        assert await t.request("small", timeout_s=2) == "ok"
        assert not t.fatal_error
    finally:
        await t.close()


@pytest.mark.parametrize("method", ["oversized", "bad"])
async def test_reader_fatal_fast_fails_future_requests_and_reaps_child(method):
    t = transport()
    try:
        await t.connect()
        with pytest.raises(MCPConnectionError):
            await t.request(method, timeout_s=2)
        assert t.fatal_error
        start = time.monotonic()
        with pytest.raises(MCPConnectionError):
            await t.request("small", timeout_s=10)
        assert time.monotonic() - start < 0.2
        assert not t._pending
        await asyncio.wait_for(t.proc.wait(), 5)
        assert t.proc.returncode is not None
    finally:
        await t.close()


async def test_timeout_and_caller_cancel_cleanup_pending_and_keep_healthy_channel():
    t = transport()
    notifications = []
    original = t.notify

    async def record(method, params=None):
        notifications.append((method, params))
        await original(method, params)

    t.notify = record
    try:
        await t.connect()
        with pytest.raises(MCPTimeoutError):
            await t.request("hang", timeout_s=0.03)
        assert not t._pending
        task = asyncio.create_task(t.request("hang", timeout_s=10))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not t._pending
        assert len(notifications) == 2
        assert all(
            x[0] == "notifications/cancelled" and isinstance(x[1]["requestId"], int)
            for x in notifications
        )
        assert await t.request("small", timeout_s=2) == "ok"
    finally:
        await t.close()


async def test_content_length_is_bounded_before_body_read():
    peer = "import sys,time; sys.stdin.readline(); sys.stdout.write('Content-Length: 999999999\\r\\n\\r\\n');sys.stdout.flush();time.sleep(10)"
    t = transport(peer=peer)
    try:
        await t.connect()
        with pytest.raises(MCPConnectionError, match="message_too_large"):
            await t.request("header", timeout_s=2)
        assert not t._pending
    finally:
        await t.close()
