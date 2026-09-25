"""Process/HTTP/model integration only; does not launch an actual browser."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.agent.loop import Agent
from app.browser.service import Instance
from app.browser.worker import Worker
from app.llm.events import StreamEvent, ToolCall
from app.tools.base import ToolRegistry, current_tool_context
from tests import test_browser_native
from tests.test_agent_loop import FakeBackend, RecordRenderer
from tests.test_browser_native import ctx, new

service = test_browser_native.service


async def test_worker_timeout_reaps_child_and_rejects_pending(tmp_path):
    peer = tmp_path / "fake-python-worker"
    peer.write_text(
        "#!"
        + sys.executable
        + "\n"
        + r"""
import sys,struct,json,time
while True:
    header=sys.stdin.buffer.read(4)
    if not header:break
    size=struct.unpack('>I',header)[0]
    req=json.loads(sys.stdin.buffer.read(size))
    if req['action']=='hang':time.sleep(30)
    body=json.dumps({'id':req['id'],'result':{'ok':True}}).encode()
    sys.stdout.buffer.write(struct.pack('>I',len(body))+body);sys.stdout.buffer.flush()
"""
    )
    peer.chmod(0o700)
    worker = Worker(str(peer))
    try:
        await worker.start({"connectTimeoutS": 1})
        process = worker.proc
        assert (await worker.call("hello", {}, timeout=1))["ok"]
        with pytest.raises(TimeoutError):
            await worker.call("hang", {}, timeout=0.03)
        assert process.returncode is not None
        assert not worker.connected and not worker.pending
    finally:
        await worker.close()


async def test_browser_images_reach_next_model_after_complete_tool_batch(tmp_path):
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"fixture")

    async def capture(_):
        current_tool_context().tool_images.append(
            {"type": "image", "path": str(picture), "mime_type": "image/png"}
        )
        return '{"artifact":"workspace/artifacts/shot.png"}'

    async def other(_):
        return "other result"

    tools = ToolRegistry()
    tools.add("Browser", "test", {"type": "object"}, capture)
    tools.add("Other", "test", {"type": "object"}, other)
    backend = FakeBackend(
        [
            [
                StreamEvent(
                    kind="tool_call",
                    tool_calls=[
                        ToolCall(id="shot", name="Browser", arguments="{}"),
                        ToolCall(id="other", name="Other", arguments="{}"),
                    ],
                ),
                StreamEvent(kind="finish", finish_reason="tool_calls"),
            ],
            [
                StreamEvent(kind="content", text="done"),
                StreamEvent(kind="finish", finish_reason="stop"),
            ],
        ]
    )
    await Agent(backend, tools, max_retries=0).run(
        [{"role": "user", "content": "capture"}], RecordRenderer(), model="fake"
    )
    convo = backend.seen_convos[1]
    images = [(i, m) for i, m in enumerate(convo) if isinstance(m.get("content"), list)]
    assert len(images) == 1
    index, message = images[0]
    assert all(i < index for i, m in enumerate(convo) if m.get("role") == "tool")
    assert message["content"][1]["path"] == str(picture)
    assert all(isinstance(m["content"], str) for m in convo if m.get("role") == "tool")


async def test_restart_post_works_without_healthy_cdp_and_feedback_never_executes(service):
    page = await new(service)
    posts = []

    async def restart(request):
        posts.append("post")
        return web.json_response({"terminated": True})

    app = web.Application()
    app.router.add_post("/restart", restart)
    server = TestServer(app)
    await server.start_server()
    service.cfg.main_restart_url = str(server.make_url("/restart"))
    original_sync = service._sync

    async def sync_after_post(i):
        assert posts, "recovery must not depend on a healthy CDP before POST"
        return await original_sync(i)

    service._sync = sync_after_post
    try:
        args = {"action": "recover", "page": page, "params": {"op": "restart"}}
        denied = await service.call(args, ctx(answer={"confirmed": True, "text": "不要关闭其他页"}))
        assert denied["status"] == "denied" and not posts
        done = await service.call(args, ctx(answer={"confirmed": True}))
        assert done["status"] == "ok" and posts == ["post"]
        assert done["liveStateLost"] is True and page not in service.pages
    finally:
        await server.close()


async def test_cancellation_marks_unknown_releases_page_lock_and_keeps_control_independent(
    service,
):
    page = await new(service)
    inst = next(iter(service.instances.values()))
    started = asyncio.Event()

    async def hung(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    inst.worker.call = hung
    call = asyncio.create_task(
        service.call(
            {"action": "act", "page": page, "params": {"op": "click", "target": "css=button"}},
            ctx(),
        )
    )
    await started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert service.pages[page].state == "suspect"
    assert not service.pages[page].lock.locked()
    assert inst.active == 0
    # Control channel is independently callable after worker cancellation.
    assert (await service.call({"action": "page", "params": {"op": "list"}}, ctx()))[
        "status"
    ] == "ok"
