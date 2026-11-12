"""Input queued after a runner's last steering boundary starts its own turn."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import steering
from app.llm.events import StreamEvent
from app.runtime.scheduler import ControllerRuns
from app.tools.base import ToolRegistry
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _cfg, web_env  # noqa: F401


def _setup(server, monkeypatch, backend):
    server.config = _cfg()
    server.llm_factory = FakeRunFactory(backend, context_window=128000)
    server.model_selection = SimpleNamespace(current="openai/gpt")
    server.tools = ToolRegistry()
    server.runs = ControllerRuns()
    monkeypatch.setattr(server, "_ensure_web_task_notification_worker", lambda *args, **kwargs: None)

    async def system():
        return "sys"

    monkeypatch.setattr(server, "_build_system_prompt_for_chat", system)


async def _wait_idle(server, chat):
    for _ in range(200):
        # A waiter on the old runner can resume before a later done callback.
        # Let that callback register its handoff; pending input is never idle.
        await asyncio.sleep(0)
        tasks = list(server._web_steering_handoffs)
        if task := server.runs.task(chat):
            tasks.append(task)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
        elif not steering.has_pending(chat):
            return
        else:
            await asyncio.sleep(0.01)
    raise AssertionError("conversation did not settle; queued input was not consumed")


async def test_message_queued_after_last_boundary_gets_its_own_turn(web_env, monkeypatch):  # noqa: F811
    server = web_env.server
    backend = FakeStreamBackend([[StreamEvent(kind="content", text="FIRST"), StreamEvent(kind="finish", finish_reason="stop")],
                                 [StreamEvent(kind="content", text="SECOND"), StreamEvent(kind="finish", finish_reason="stop")]])
    _setup(server, monkeypatch, backend)
    row = await server._create_web_conversation(123, title="late steer", model="openai/gpt")
    chat, conv_uuid = int(row["internal_chat_id"]), str(row["conversation_uuid"])
    live = server._live_for(row)
    steering.clear(chat)
    late = {}
    original_touch = server._touch_web_conversation

    async def touch(conversation_uuid, **kwargs):
        # The loop has finished; the runner is still registered while it records the result.
        if kwargs.get("current_status") == "就绪" and not late:
            late["result"] = await server._start_or_steer_web_conversation(row, "LATE question", [], live)
        return await original_touch(conversation_uuid, **kwargs)

    monkeypatch.setattr(server, "_touch_web_conversation", touch)
    try:
        await server._start_or_steer_web_conversation(row, "FIRST question", [], live)
        await _wait_idle(server, chat)
        assert late["result"]["queued"] is True
        assert backend.calls == 2
        assert not steering.has_pending(chat)
        last_users = [m["content"] for m in backend.seen_convos[1] if m["role"] == "user"]
        assert str(last_users[-1]).startswith("LATE question")
        operations = await server._web_operations(conv_uuid)
        late_op = next(o for o in operations if o["opId"] == f"msg:{late['result']['pendingSteering'][0]['messageUuid']}")
        answer = next(o for o in operations if o["opType"] == "assistant_message" and o["payload"].get("text") == "SECOND")
        assert late_op["turnUuid"] == answer["turnUuid"]
        assert sum(1 for o in operations if o["opType"] == "user_message" and o["payload"].get("text") == "LATE question") == 1
    finally:
        await server.runs.cancel_all_and_wait()
        steering.clear(chat)


async def test_an_interjection_carries_its_attachments_to_the_model_and_the_timeline(web_env, monkeypatch, tmp_path):  # noqa: F811
    from app.media.attachments import InboundMedia
    server = web_env.server
    backend = FakeStreamBackend([[StreamEvent(kind="content", text="FIRST"), StreamEvent(kind="finish", finish_reason="stop")],
                                 [StreamEvent(kind="content", text="SECOND"), StreamEvent(kind="finish", finish_reason="stop")]])
    _setup(server, monkeypatch, backend)
    row = await server._create_web_conversation(123, title="steer attachments", model="openai/gpt")
    chat, conv_uuid = int(row["internal_chat_id"]), str(row["conversation_uuid"])
    live = server._live_for(row)
    steering.clear(chat)
    image, notes = tmp_path / "shot.png", tmp_path / "notes.txt"
    image.write_bytes(b"png"); notes.write_text("line one")
    media = [InboundMedia(kind="image", upload_type="websocket_upload", file_name="shot.png", mime_type="image/png", size=3, path=str(image)),
             InboundMedia(kind="file", upload_type="websocket_upload", file_name="notes.txt", mime_type="text/plain", size=8, path=str(notes), text_excerpt="line one")]

    async def register(path, *, conversation, turn_uuid="", op_id="", source_url=""):
        return {"artifactUuid": f"art-{Path(path).name}", "contentUrl": f"/a/{Path(path).name}", "inlinePreview": True}

    monkeypatch.setattr(server, "_register_web_artifact_from_path", register)
    late, published = {}, []
    original_touch, original_publish = server._touch_web_conversation, live.publish

    async def touch(conversation_uuid, **kwargs):
        if kwargs.get("current_status") == "就绪" and not late:
            late["result"] = await server._start_or_steer_web_conversation(row, "", media, live)
        return await original_touch(conversation_uuid, **kwargs)

    async def publish(event, **kwargs):
        published.append(event)
        return await original_publish(event, **kwargs)

    monkeypatch.setattr(server, "_touch_web_conversation", touch)
    monkeypatch.setattr(live, "publish", publish)
    try:
        await server._start_or_steer_web_conversation(row, "FIRST question", [], live)
        await _wait_idle(server, chat)
        assert late["result"]["queued"] is True
        # What the composer receives names the attachments without their paths or extracted text.
        assert "media" not in late["result"]["pendingSteering"][0]
        assert [a["fileName"] for a in late["result"]["pendingSteering"][0]["attachments"]] == ["shot.png", "notes.txt"]
        assert all("media" not in item for e in published if e.get("type") == "pending_steering" for item in e.get("items") or [])
        content = [m["content"] for m in backend.seen_convos[1] if m["role"] == "user"][-1]
        assert isinstance(content, list) and {"type": "image", "path": str(image), "mime_type": "image/png", "name": "shot.png"} in content
        text = next(block["text"] for block in content if block["type"] == "text")
        assert text.startswith("请根据我发送的附件内容回答。") and "line one" in text and "shot.png" in text
        operations = await server._web_operations(conv_uuid)
        message = next(o for o in operations if o["opId"] == f"msg:{late['result']['pendingSteering'][0]['messageUuid']}")
        assert [a["fileName"] for a in message["payload"]["attachments"]] == ["shot.png", "notes.txt"]
    finally:
        await server.runs.cancel_all_and_wait()
        steering.clear(chat)


async def test_stopped_or_failed_turn_does_not_start_a_handoff(web_env, monkeypatch):  # noqa: F811
    server = web_env.server
    _setup(server, monkeypatch, FakeStreamBackend())
    row = await server._create_web_conversation(123, title="no handoff", model="openai/gpt")
    chat = int(row["internal_chat_id"])
    steering.clear(chat)
    started = []

    async def resume(r):
        started.append(r)

    monkeypatch.setattr(server, "_resume_stranded_steering", resume)
    try:
        for outcome in (False, "cancel", RuntimeError("boom")):
            async def runner(value=outcome):
                if value == "cancel":
                    await asyncio.Event().wait()
                if isinstance(value, Exception):
                    raise value
                return value
            task = asyncio.create_task(runner())
            await asyncio.sleep(0)
            if outcome == "cancel":
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            server._schedule_steering_handoff(row, task)
        await asyncio.sleep(0)
        assert started == []
        ok = asyncio.create_task(asyncio.sleep(0, result=True))
        await ok
        server._schedule_steering_handoff(row, ok)
        await asyncio.gather(*list(server._web_steering_handoffs))
        assert started == [row]
    finally:
        steering.clear(chat)


async def test_handoff_without_pending_input_or_while_running_does_nothing(web_env, monkeypatch):  # noqa: F811
    server = web_env.server
    backend = FakeStreamBackend()
    _setup(server, monkeypatch, backend)
    row = await server._create_web_conversation(123, title="nothing pending", model="openai/gpt")
    chat = int(row["internal_chat_id"])
    steering.clear(chat)
    await server._resume_stranded_steering(row)
    assert server.runs.task(chat) is None
    gate = asyncio.Event()
    busy = asyncio.create_task(gate.wait())
    server.runs.register(chat, busy)
    steering.enqueue(chat, "queued for the live runner", turnUuid="t", messageUuid="m", source="web")
    try:
        await server._resume_stranded_steering(row)
        assert server.runs.task(chat) is busy
        assert [copy.deepcopy(i["text"]) for i in steering.pending_items(chat)] == ["queued for the live runner"]
    finally:
        gate.set()
        await busy
        steering.clear(chat)
    assert backend.calls == 0
