from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from app.agent import steering
from app.agent.compaction import CompactionOutcome
from app.agent.result import RunResult
from app.agent.runs import RunRegistry
from app.config import Config
from app.control_actions import ControlActionQueue
from app.db.dao import MessageDAO, SummaryDAO
from app.db.engine import DB, now_ts
from app.llm.base import AgentResult, OpenBearLLMError
from app.llm.events import StreamEvent, ToolCall, Usage
from app.llm.openai_responses import _to_responses_input
from app.media.attachments import InboundMedia
from app.rath.manager import RathTaskManager
from app.rath.plan import AgentPlanCoordinator
from app.task_memory import (
    SCOPE_AGENT_TASK,
    SCOPE_CONVERSATION,
    TaskMemoryDAO,
    is_task_memory_runtime_message,
)
from app.tools.base import (
    ToolRegistry,
    current_tool_context,
    redact_tool_arguments_for_audit,
    redact_tool_result_for_audit,
)
from app.utils import estimate_tokens
from app.web_admin import (
    WebAdminServer,
    _sha256,
    _WebContextCompactionGate,
    _WebEmergencyCompactor,
    _WebLiveStream,
    _WebStreamRenderer,
)


def _cfg() -> Config:
    return Config.model_validate({
        "telegram": {"botToken": "t", "whitelistIds": [123]},
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://x",
                    "apiKey": "k",
                    "protocol": "chat",
                    "models": [{
                        "id": "gpt",
                        "thinkingLevels": ["low", "medium", "high"],
                        "defaultThinkingLevel": "medium",
                        "supportsFast": True,
                    }, {
                        "id": "cheap",
                        "thinkingLevels": ["low", "medium"],
                        "defaultThinkingLevel": "low",
                        "supportsFast": False,
                    }],
                }
            },
            "primary": "openai/gpt",
        },
        "memory": {"baseUrl": "http://m", "identity": "openbear", "accessKey": "ak"},
        "web": {"enabled": True, "host": "127.0.0.1", "port": 18961, "sessionDays": 30},
    })




class FakeMemoryBackend:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = []

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        self.calls.append({"messages": messages, "model": model, "system": system, "max_tokens": max_tokens})
        return AgentResult(text=self.text)


class FakeMemoryFactory:
    def __init__(self, text: str) -> None:
        self.backend = FakeMemoryBackend(text)
        self.requested = []

    def backend_for(self, fullname: str):
        self.requested.append(fullname)
        return self.backend, "fake-gpt", 8192


class FakeStreamBackend:
    protocol = "fake"

    def __init__(self, scripts: list[list[StreamEvent]] | None = None, summary: str = "") -> None:
        self.scripts = scripts or []
        self.summary = summary
        self.calls = 0
        self.complete_calls = 0
        self.seen_convos = []
        self.seen_systems = []
        self.seen_tools = []
        self.seen_opts = []

    async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        self.seen_convos.append(copy.deepcopy(messages))
        self.seen_systems.append(system)
        self.seen_tools.append(copy.deepcopy(tools))
        self.seen_opts.append(dict(opts))
        script = self.scripts[min(self.calls, len(self.scripts) - 1)] if self.scripts else [StreamEvent(kind="content", text="ok"), StreamEvent(kind="finish", finish_reason="stop")]
        self.calls += 1
        for ev in script:
            yield ev

    async def complete(self, messages, *, model, **k):
        self.complete_calls += 1
        return AgentResult(text=self.summary or "## Primary Request and Intent\n- Web compaction test.\n## Key Technical Concepts\n- Context compaction.\n## Files and Code Sections\n- None\n## Errors and Fixes\n- None\n## Problem Solving\n- None\n## All User Messages\n- None\n## Pending Tasks\n- None\n## Current Work\n- None\n## Optional Next Step\n- None\n## Critical Identifiers\n- None\n")


class FakeRunFactory:
    def __init__(self, backend: FakeStreamBackend, *, context_window: int = 1000) -> None:
        self.backend = backend
        self._context_window = context_window

    def backend_for(self, fullname: str):
        return self.backend, "fake-gpt", 8192

    def context_window(self, fullname: str) -> int:
        return self._context_window


class FakeBot:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, reply_markup=reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


async def _web_frames_for(web_env, conversation_uuid: str, *, event_type: str = "", op_type: str = "", limit: int = 10000) -> list[dict]:
    frames = await web_env.server._web_frames(conversation_uuid, limit=limit)
    if event_type:
        frames = [frame for frame in frames if str((frame.get("debug") or {}).get("eventType") or "") == event_type]
    if op_type:
        frames = [frame for frame in frames if str(frame.get("opType") or "") == op_type]
    return frames


def _frame_payload(frame: dict) -> dict:
    payload = frame.get("payload") if isinstance(frame, dict) else None
    return payload if isinstance(payload, dict) else {}


def _trusted_task_memory_state(*, epoch: int = 0) -> dict:
    return {
        "role": "user",
        "content": "<openbear-task-memory-state>private</openbear-task-memory-state>",
        "_openbear_runtime": {
            "kind": "task_memory_state",
            "version": 1,
            "digest": "d" * 64,
            "epoch": epoch,
            "itemCount": 1,
        },
    }


@pytest.fixture
async def web_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBEAR_WEB_ARTIFACT_DIR", str(tmp_path / "web_artifacts"))
    db = DB(str(tmp_path / "t.db"))
    await db.connect()
    bot = FakeBot()
    server = WebAdminServer(_cfg(), db, bot)  # type: ignore[arg-type]
    await server.ensure_secret_key()
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    try:
        yield SimpleNamespace(db=db, bot=bot, server=server, client=client)
    finally:
        await client.close()
        await db.close()


async def test_run_registry_shutdown_waits_for_cancel_cleanup():
    registry = RunRegistry()
    cleanup_finished = asyncio.Event()

    async def run():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleanup_finished.set()

    task = asyncio.create_task(run())
    registry.register(-1, task)
    await asyncio.sleep(0)

    cancelled = await registry.cancel_all_and_wait()

    assert cancelled == 1
    assert cleanup_finished.is_set()
    assert registry.count() == 0


async def test_web_admin_public_exports_and_route_baseline(web_env):
    import app.web_admin as web_admin

    assert web_admin.WebAdminServer is WebAdminServer
    assert web_admin._WebStreamRenderer is _WebStreamRenderer
    assert web_admin._sha256("openbear") == _sha256("openbear")
    assert _sha256("openbear") != "openbear"
    for name in (
        "_MCPServerNotFoundError",
        "_LOGIN_FAIL_LIMIT",
        "_human_bytes",
        "_log_web_frontend_event",
        "_log_web_ws_audit",
        "log",
    ):
        assert hasattr(web_admin, name)


    routes = {(route.method, route.resource.canonical) for route in web_env.server.make_app().router.routes()}
    assert ("GET", "/health") in routes
    assert ("POST", "/api/auth/login/start") in routes
    assert ("GET", "/api/conversations/{conversation_uuid}/ws") in routes
    assert ("GET", "/api/conversations/{conversation_uuid}/state") in routes
    assert ("GET", "/api/conversations/{conversation_uuid}/operations/{operation_id}/detail") in routes
    assert ("PATCH", "/api/conversations/{conversation_uuid}") in routes
    assert ("POST", "/api/conversations/{conversation_uuid}/duplicate") in routes
    assert ("POST", "/api/conversations/{conversation_uuid}/reorder") in routes
    assert ("POST", "/api/conversations/{conversation_uuid}/pin") in routes
    assert ("POST", "/api/conversations/{conversation_uuid}/unpin") in routes
    assert ("GET", "/api/conversations/defaults") in routes
    assert ("PATCH", "/api/conversations/defaults") in routes
    assert ("POST", "/api/conversations/{conversation_uuid}/agent-run-config") in routes
    assert ("GET", "/api/conversations/{conversation_uuid}/artifacts") in routes
    assert ("GET", "/api/conversations/{conversation_uuid}/artifacts/{artifact_uuid}/content") in routes
    assert ("GET", "/api/mcp/status") in routes
    assert ("PATCH", "/api/mcp/servers/{server}/approval") in routes
    assert ("POST", "/api/mcp/servers/{server}/uninstall") in routes
    assert ("GET", "/api/rath/tasks") not in routes
    assert ("GET", "/api/memory/templates") in routes


async def _login_cookie(web_env) -> dict[str, str]:
    key = await web_env.server.get_secret_key()
    start = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    req_uuid = (await start.json())["requestUuid"]
    await web_env.server.decide_login_request(req_uuid, approved=True, decided_by=123)
    approved = await web_env.client.post(f"/api/auth/login/consume/{req_uuid}")
    return {"openbear_web_session": approved.cookies["openbear_web_session"].value}


async def test_web_artifacts_rewrite_and_auth_scoped_content(web_env, tmp_path):
    cookie = await _login_cookie(web_env)
    web_env.client.session.cookie_jar.clear()
    row = await web_env.server._create_web_conversation(123, title="artifacts")
    workspace = tmp_path / "workspace"
    web_env.server.workspace_dir = str(workspace)
    source_dir = workspace / "artifacts" / "openbear-web-artifact-test"
    source_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / "hello.txt"
    source.write_text("artifact body", encoding="utf-8")

    rewritten = await web_env.server._rewrite_web_artifact_links(
        f"请看 [文件]({source}) 和 ![预览]({source})",
        conversation=row,
    )
    assert str(source) not in rewritten
    assert f"/api/conversations/{row['conversation_uuid']}/artifacts/" in rewritten
    assert "preview=1" in rewritten

    cur = await web_env.db.conn.execute("SELECT * FROM web_artifacts WHERE conversation_uuid=?", (row["conversation_uuid"],))
    artifact = dict(await cur.fetchone())
    assert artifact["owner_chat_id"] == 123
    assert artifact["internal_chat_id"] == row["internal_chat_id"]

    unauth = await web_env.client.get(
        f"/api/conversations/{row['conversation_uuid']}/artifacts/{artifact['artifact_uuid']}/content"
    )
    assert unauth.status == 401

    content = await web_env.client.get(
        f"/api/conversations/{row['conversation_uuid']}/artifacts/{artifact['artifact_uuid']}/content?preview=1",
        cookies={"openbear_web_session": cookie},
    )
    assert content.status == 200
    assert await content.text() == "artifact body"
    assert content.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert content.headers["X-Content-Type-Options"] == "nosniff"
    assert content.headers["Content-Disposition"].startswith("inline;")

    download = await web_env.client.get(
        f"/api/conversations/{row['conversation_uuid']}/artifacts/{artifact['artifact_uuid']}/content?download=1",
        cookies={"openbear_web_session": cookie},
    )
    assert download.status == 200
    assert download.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert download.headers["Content-Disposition"].startswith("attachment;")


async def test_web_assistant_workspace_image_link_rewritten_before_display(web_env, tmp_path):
    row = await web_env.server._create_web_conversation(123, title="assistant artifact")
    workspace = tmp_path / "workspace"
    web_env.server.workspace_dir = str(workspace)
    source = workspace / "artifacts" / "assistant-image.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(
        live=live,
        artifact_rewriter=web_env.server._web_assistant_artifact_rewriter(row),
    )
    await renderer.finalize("生成好了：\n\n![图](workspace/artifacts/assistant-image.png)")
    await renderer.close()

    ops = await web_env.server._web_operations(row["conversation_uuid"])
    assistant_ops = [op for op in ops if op.get("opType") == "assistant_message"]
    assert assistant_ops
    text = assistant_ops[-1]["payload"]["text"]
    assert "workspace/artifacts/assistant-image.png" not in text
    assert "/api/conversations/" in text
    assert "preview=1" in text


async def test_web_artifacts_force_risky_types_to_download(web_env, tmp_path):
    cookies = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="artifacts")
    workspace = tmp_path / "workspace"
    web_env.server.workspace_dir = str(workspace)
    source_dir = workspace / "artifacts" / "openbear-web-artifact-test"
    source_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / "page.html"
    source.write_text("<script>alert(1)</script>", encoding="utf-8")
    artifact = await web_env.server._register_web_artifact_from_path(source, conversation=row)
    assert artifact is not None

    content = await web_env.client.get(
        f"/api/conversations/{row['conversation_uuid']}/artifacts/{artifact['artifactUuid']}/content?preview=1",
        cookies={"openbear_web_session": cookies},
    )
    assert content.status == 200
    assert content.headers["Content-Disposition"].startswith("attachment;")


async def test_web_artifacts_do_not_publish_tool_artifacts(web_env):
    row = await web_env.server._create_web_conversation(123, title="private tool artifacts")
    tool_dir = Path.cwd() / "data" / "tool_artifacts" / "bash-output" / "test-session"
    tool_dir.mkdir(parents=True, exist_ok=True)
    source = tool_dir / "secret-output.txt"
    source.write_text("TOKEN=should-not-be-web-artifact", encoding="utf-8")
    try:
        artifact = await web_env.server._register_web_artifact_from_path(source, conversation=row)
        rewritten = await web_env.server._rewrite_web_artifact_links(f"[日志]({source})", conversation=row)
    finally:
        with contextlib.suppress(OSError):
            source.unlink()
    assert artifact is None
    assert "/api/conversations/" not in rewritten
    assert str(source) in rewritten


async def test_web_conversation_context_actions_rename_pin_duplicate(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="source", model="openai/gpt")
    dao = MessageDAO(web_env.db)
    await dao.add(int(row["internal_chat_id"]), "user", "hello")
    await dao.add_model_call(int(row["internal_chat_id"]), model="openai/gpt", status="ok", model_call_count=2)
    await web_env.server._publish_operation(
        row["conversation_uuid"],
        internal_chat_id=int(row["internal_chat_id"]),
        op_id="msg:test-user",
        op_type="user_message",
        action="create",
        turn_uuid="turn-a",
        payload={"role": "user", "text": "hello", "createdAtMs": 1000},
        status="completed",
        lifecycle="terminal",
    )
    await web_env.db.conn.commit()

    rename = await web_env.client.patch(f"/api/conversations/{row['conversation_uuid']}", cookies=cookie, json={"title": "renamed"})
    assert rename.status == 200
    assert (await rename.json())["conversation"]["title"] == "renamed"

    pin = await web_env.client.post(f"/api/conversations/{row['conversation_uuid']}/pin", cookies=cookie)
    assert pin.status == 200
    assert (await pin.json())["conversation"]["pinned"] is True
    unpin = await web_env.client.post(f"/api/conversations/{row['conversation_uuid']}/unpin", cookies=cookie)
    assert unpin.status == 200
    assert (await unpin.json())["conversation"]["pinned"] is False

    duplicate = await web_env.client.post(f"/api/conversations/{row['conversation_uuid']}/duplicate", cookies=cookie)
    assert duplicate.status == 200
    copied = await duplicate.json()
    copied_uuid = copied["conversation"]["conversationUuid"]
    copied_chat_id = copied["conversation"]["internalChatId"]
    assert copied_uuid != row["conversation_uuid"]
    assert copied_chat_id != int(row["internal_chat_id"])
    assert copied["conversation"]["title"].endswith("副本")
    copied_messages = await dao.recent(copied_chat_id)
    assert [m.content for m in copied_messages] == ["hello"]
    copied_calls = await dao.recent_model_calls(copied_chat_id)
    assert copied_calls[0].model == "openai/gpt"
    ops = await web_env.server._web_operations(copied_uuid)
    assert ops and ops[0]["conversationUuid"] == copied_uuid
    assert ops[0]["internalChatId"] == copied_chat_id


async def test_web_conversation_archive_only_filters_sidebar_listing(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="archive-only-field")
    uuid = str(row["conversation_uuid"])
    cur = await web_env.db.conn.execute(
        "SELECT display_order FROM web_conversations WHERE conversation_uuid=?",
        (uuid,),
    )
    display_order = float((await cur.fetchone())["display_order"])

    archived = await web_env.client.patch(
        f"/api/conversations/{uuid}",
        cookies=cookie,
        json={"archived": True},
    )
    assert archived.status == 200
    archive_payload = await archived.json()
    assert archive_payload["conversation"]["archived"] is True
    assert archive_payload["conversation"]["displayOrder"] == display_order

    # The default sidebar query hides it and must not create a replacement row.
    hidden = await web_env.client.get("/api/conversations", cookies=cookie)
    assert hidden.status == 200
    assert uuid not in {item["conversationUuid"] for item in (await hidden.json())["items"]}
    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM web_conversations WHERE owner_chat_id=?",
        (123,),
    )
    assert int((await cur.fetchone())["n"]) == 1

    # Archive must not make an open/direct conversation inaccessible.
    state = await web_env.client.get(f"/api/conversations/{uuid}/state", cookies=cookie)
    assert state.status == 200
    included = await web_env.client.get("/api/conversations?includeArchived=1", cookies=cookie)
    assert included.status == 200
    included_item = next(item for item in (await included.json())["items"] if item["conversationUuid"] == uuid)
    assert included_item["archived"] is True
    assert included_item["displayOrder"] == display_order

    restored = await web_env.client.patch(
        f"/api/conversations/{uuid}",
        cookies=cookie,
        json={"archived": False},
    )
    assert restored.status == 200
    assert (await restored.json())["conversation"]["archived"] is False
    visible = await web_env.client.get("/api/conversations", cookies=cookie)
    assert uuid in {item["conversationUuid"] for item in (await visible.json())["items"]}


async def test_web_conversation_reorder_is_limited_to_its_pin_group(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    first = await web_env.server._create_web_conversation(123, title="first pinned")
    second = await web_env.server._create_web_conversation(123, title="second pinned")
    ordinary = await web_env.server._create_web_conversation(123, title="ordinary")
    first_uuid = str(first["conversation_uuid"])
    second_uuid = str(second["conversation_uuid"])
    ordinary_uuid = str(ordinary["conversation_uuid"])
    for uuid in (first_uuid, second_uuid):
        response = await web_env.client.post(f"/api/conversations/{uuid}/pin", cookies=cookie)
        assert response.status == 200

    moved = await web_env.client.post(
        f"/api/conversations/{first_uuid}/reorder",
        cookies=cookie,
        json={"beforeConversationUuid": "", "afterConversationUuid": second_uuid},
    )
    assert moved.status == 200
    assert (await moved.json())["conversation"]["displayOrder"] is not None
    cur = await web_env.db.conn.execute(
        "SELECT conversation_uuid, display_order FROM web_conversations WHERE conversation_uuid IN (?, ?)",
        (first_uuid, second_uuid),
    )
    ranks = {str(item["conversation_uuid"]): float(item["display_order"]) for item in await cur.fetchall()}
    assert ranks[first_uuid] < ranks[second_uuid]

    rejected = await web_env.client.post(
        f"/api/conversations/{first_uuid}/reorder",
        cookies=cookie,
        json={"beforeConversationUuid": "", "afterConversationUuid": ordinary_uuid},
    )
    assert rejected.status == 409
    assert (await rejected.json())["error"] == "conversation_reorder_group_mismatch"


async def test_task_memory_duplicate_maps_tasks_isolates_copy_and_hard_delete_cleans_source(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    source = await web_env.server._create_web_conversation(123, title="task memory source")
    source_uuid = str(source["conversation_uuid"])
    source_chat_id = int(source["internal_chat_id"])
    source_task = await web_env.server.rath_dao.create_task(
        chat_id=source_chat_id,
        parent_session_uuid=source_uuid,
        workflow_uuid="wf-task-memory-duplicate",
        title="memory task",
        status="completed",
        task_uuid="source-memory-task",
    )
    memories = TaskMemoryDAO(web_env.db)
    source_conversation_memory, _ = await memories.create(
        conversation_uuid=source_uuid,
        scope_type=SCOPE_CONVERSATION,
        name="conversation fact",
        body="source conversation body",
        idempotency_key="source-conversation-idempotency",
    )
    source_task_memory, _ = await memories.create(
        conversation_uuid=source_uuid,
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=source_task,
        name="task fact",
        body="source task body",
        source_run_uuid=source_task,
        idempotency_key="source-task-idempotency",
    )

    # inheritFromTaskUuid is Plan context only: a newly created task starts with
    # an empty Task Memory scope even when it shares the long-lived Agent session.
    inherited_task = await web_env.server.rath_dao.create_task(
        chat_id=source_chat_id,
        parent_session_uuid=source_uuid,
        workflow_uuid="wf-task-memory-duplicate",
        title="inherited plan only",
        input_data={"inheritFromTaskUuid": source_task},
        agent_session_uuid="same-agent-session",
        task_uuid="new-inherited-task",
        status="completed",
    )
    inherited_listing = await memories.list(
        conversation_uuid=source_uuid,
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=inherited_task,
    )
    assert inherited_listing["total"] == 0

    duplicate = await web_env.client.post(
        f"/api/conversations/{source_uuid}/duplicate",
        cookies=cookie,
    )
    assert duplicate.status == 200
    copied = await duplicate.json()
    copied_uuid = str(copied["conversation"]["conversationUuid"])
    copied_chat_id = int(copied["conversation"]["internalChatId"])
    cur = await web_env.db.conn.execute(
        "SELECT task_uuid,title FROM rath_tasks WHERE chat_id=? ORDER BY id",
        (copied_chat_id,),
    )
    copied_tasks = {str(row["title"]): str(row["task_uuid"]) for row in await cur.fetchall()}
    copied_task = copied_tasks["memory task"]

    copied_conversation = await memories.list(
        conversation_uuid=copied_uuid,
        scope_type=SCOPE_CONVERSATION,
    )
    copied_task_listing = await memories.list(
        conversation_uuid=copied_uuid,
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=copied_task,
    )
    assert copied_conversation["total"] == 1
    assert copied_task_listing["total"] == 1
    copied_conversation_item = copied_conversation["items"][0]
    copied_task_item = copied_task_listing["items"][0]
    assert copied_conversation_item["memoryUuid"] != source_conversation_memory["memoryUuid"]
    assert copied_task_item["memoryUuid"] != source_task_memory["memoryUuid"]
    copied_task_detail = await memories.get(
        copied_task_item["memoryUuid"],
        conversation_uuid=copied_uuid,
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=copied_task,
    )
    assert copied_task_detail["body"] == "source task body"
    assert copied_task_detail["sourceRunUuid"] == copied_task

    await memories.update(
        copied_task_item["memoryUuid"],
        conversation_uuid=copied_uuid,
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=copied_task,
        expected_revision=copied_task_item["revision"],
        changes={"body": "copy-only change"},
    )
    source_detail = await memories.get(
        source_task_memory["memoryUuid"],
        conversation_uuid=source_uuid,
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=source_task,
    )
    assert source_detail["body"] == "source task body"

    deleted = await web_env.client.delete(f"/api/conversations/{source_uuid}", cookies=cookie)
    assert deleted.status == 200
    assert (await deleted.json())["taskMemoriesDeleted"] >= 1
    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM conversation_task_memories WHERE conversation_uuid=?",
        (source_uuid,),
    )
    assert int((await cur.fetchone())["n"] or 0) == 0
    assert (await memories.list(
        conversation_uuid=copied_uuid,
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=copied_task,
    ))["total"] == 1


async def test_web_conversation_duplicate_suppresses_only_copied_terminal_task_notifications(web_env, monkeypatch):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    source = await web_env.server._create_web_conversation(123, title="terminal Rath history")
    source_uuid = str(source["conversation_uuid"])
    source_chat_id = int(source["internal_chat_id"])
    await MessageDAO(web_env.db).add(source_chat_id, "user", "keep terminal history")

    source_task_uuids = []
    for status in ("completed", "failed"):
        task_uuid = await web_env.server.rath_dao.create_task(
            chat_id=source_chat_id,
            parent_session_uuid=source_uuid,
            workflow_uuid="wf-duplicate-terminal-history",
            title=f"historical {status}",
            status=status,
            task_uuid=f"source-{status}-task",
            turn_uuid=f"turn-{status}",
            run_root_turn_uuid=f"turn-{status}",
        )
        source_task_uuids.append(task_uuid)
        await web_env.server.rath_dao.append_event(
            task_uuid,
            f"task_{status}",
            summary=f"historical {status} result",
        )
        await web_env.server._publish_operation(
            source_uuid,
            internal_chat_id=source_chat_id,
            op_id=f"agent:{task_uuid}",
            op_type="agent",
            action="end" if status == "completed" else "error",
            turn_uuid=f"turn-{status}",
            run_root_turn_uuid=f"turn-{status}",
            payload={"taskUuid": task_uuid, "status": status, "title": f"historical {status}"},
            status=status,
            lifecycle="terminal",
        )

    task_placeholders = ",".join("?" for _ in source_task_uuids)
    cur = await web_env.db.conn.execute(
        f"SELECT task_uuid,title,status FROM rath_tasks WHERE task_uuid IN ({task_placeholders}) ORDER BY task_uuid",
        tuple(source_task_uuids),
    )
    source_tasks_before = [tuple(row) for row in await cur.fetchall()]
    cur = await web_env.db.conn.execute(
        f"SELECT notification_uuid,task_uuid,state,claim_token,claimed_at,delivered_at,updated_at FROM web_task_notifications WHERE task_uuid IN ({task_placeholders}) ORDER BY task_uuid,notification_uuid",
        tuple(source_task_uuids),
    )
    source_notifications_before = [tuple(row) for row in await cur.fetchall()]
    assert len(source_notifications_before) == 2
    assert {row[2] for row in source_notifications_before} == {"pending"}

    run_calls = []

    async def unexpected_notification_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(web_env.server, "_run_web_turn", unexpected_notification_run)
    duplicate = await web_env.client.post(f"/api/conversations/{source_uuid}/duplicate", cookies=cookie)
    assert duplicate.status == 200
    copied = await duplicate.json()
    copied_uuid = str(copied["conversation"]["conversationUuid"])
    copied_chat_id = int(copied["conversation"]["internalChatId"])

    cur = await web_env.db.conn.execute(
        "SELECT task_uuid,title,status FROM rath_tasks WHERE chat_id=? ORDER BY task_uuid",
        (copied_chat_id,),
    )
    copied_tasks = await cur.fetchall()
    copied_task_uuids = [str(row["task_uuid"]) for row in copied_tasks]
    assert len(copied_tasks) == 2
    assert {str(row["status"]) for row in copied_tasks} == {"completed", "failed"}
    assert set(copied_task_uuids).isdisjoint(source_task_uuids)

    copied_placeholders = ",".join("?" for _ in copied_task_uuids)
    cur = await web_env.db.conn.execute(
        f"""
        SELECT conversation_uuid,internal_chat_id,task_uuid,state,claim_token,claimed_at,delivered_at,updated_at
        FROM web_task_notifications
        WHERE task_uuid IN ({copied_placeholders})
        ORDER BY task_uuid,notification_uuid
        """,
        tuple(copied_task_uuids),
    )
    copied_notifications = await cur.fetchall()
    assert len(copied_notifications) == 2
    assert {str(row["conversation_uuid"]) for row in copied_notifications} == {copied_uuid}
    assert {int(row["internal_chat_id"]) for row in copied_notifications} == {copied_chat_id}
    assert {str(row["state"]) for row in copied_notifications} == {"suppressed"}
    assert all(str(row["claim_token"] or "") == "" and int(row["claimed_at"] or 0) == 0 for row in copied_notifications)
    assert all(int(row["delivered_at"] or 0) > 0 and int(row["updated_at"] or 0) >= int(row["delivered_at"] or 0) for row in copied_notifications)
    cur = await web_env.db.conn.execute(
        f"SELECT COUNT(*) AS n FROM web_task_notifications WHERE task_uuid IN ({copied_placeholders}) AND state IN ('pending','processing')",
        tuple(copied_task_uuids),
    )
    assert int((await cur.fetchone())["n"] or 0) == 0

    await web_env.server._recover_web_task_notifications(copied_uuid)
    await asyncio.sleep(0)
    assert run_calls == []
    assert copied_uuid not in web_env.server._web_task_notification_workers

    cur = await web_env.db.conn.execute(
        f"SELECT COUNT(*) AS n FROM rath_task_events WHERE task_uuid IN ({copied_placeholders})",
        tuple(copied_task_uuids),
    )
    assert int((await cur.fetchone())["n"] or 0) == 4
    copied_operations = await web_env.server._web_operations(copied_uuid)
    copied_agent_operations = [operation for operation in copied_operations if operation["opType"] == "agent"]
    assert len(copied_agent_operations) == 2
    assert {operation["status"] for operation in copied_agent_operations} == {"completed", "failed"}
    assert [message.content for message in await MessageDAO(web_env.db).recent(copied_chat_id)] == ["keep terminal history"]

    cur = await web_env.db.conn.execute(
        f"SELECT task_uuid,title,status FROM rath_tasks WHERE task_uuid IN ({task_placeholders}) ORDER BY task_uuid",
        tuple(source_task_uuids),
    )
    assert [tuple(row) for row in await cur.fetchall()] == source_tasks_before
    cur = await web_env.db.conn.execute(
        f"SELECT notification_uuid,task_uuid,state,claim_token,claimed_at,delivered_at,updated_at FROM web_task_notifications WHERE task_uuid IN ({task_placeholders}) ORDER BY task_uuid,notification_uuid",
        tuple(source_task_uuids),
    )
    assert [tuple(row) for row in await cur.fetchall()] == source_notifications_before
    assert [message.content for message in await MessageDAO(web_env.db).recent(source_chat_id)] == ["keep terminal history"]


async def test_duplicate_notification_suppression_batches_more_than_999_without_scope_leak(web_env):
    target_uuid = "duplicate-target-conversation"
    target_chat_id = -2001
    source_uuid = "duplicate-source-conversation"
    source_chat_id = -1001
    task_uuids = [f"copied-task-{index:04d}" for index in range(1001)]
    batches = web_env.server._duplicate_notification_task_batches([*task_uuids, "", task_uuids[0]])
    assert [len(batch) for batch in batches] == [500, 500, 1]
    assert [task_uuid for batch in batches for task_uuid in batch] == task_uuids
    assert max(len(batch) + 4 for batch in batches) == 504

    timestamp = now_ts()
    await web_env.db.conn.executemany(
        """
        INSERT INTO web_task_notifications (
          notification_uuid, notification_key, conversation_uuid, internal_chat_id,
          task_uuid, payload_json, state, claim_token, claimed_at, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                f"target-notification-{index}",
                f"target-key-{index}",
                target_uuid,
                target_chat_id,
                task_uuid,
                "{}",
                "processing" if index % 2 else "pending",
                f"claim-{index}",
                timestamp,
                timestamp,
                timestamp,
            )
            for index, task_uuid in enumerate(task_uuids)
        ],
    )
    decoys = [
        ("source-real", "source-real-key", source_uuid, source_chat_id, task_uuids[0], "{}", "pending", "source-claim", timestamp, timestamp, timestamp),
        ("wrong-chat", "wrong-chat-key", target_uuid, target_chat_id - 1, task_uuids[0], "{}", "pending", "wrong-chat-claim", timestamp, timestamp, timestamp),
        ("already-delivered", "already-delivered-key", target_uuid, target_chat_id, task_uuids[1], "{}", "delivered", "delivered-claim", timestamp, timestamp, timestamp),
        ("unrelated-task", "unrelated-task-key", target_uuid, target_chat_id, "unrelated-task", "{}", "pending", "unrelated-claim", timestamp, timestamp, timestamp),
    ]
    await web_env.db.conn.executemany(
        """
        INSERT INTO web_task_notifications (
          notification_uuid, notification_key, conversation_uuid, internal_chat_id,
          task_uuid, payload_json, state, claim_token, claimed_at, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        decoys,
    )

    await web_env.server._suppress_duplicate_task_notifications(
        conversation_uuid=target_uuid,
        internal_chat_id=target_chat_id,
        task_uuids=task_uuids,
    )
    await web_env.db.conn.commit()

    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM web_task_notifications WHERE conversation_uuid=? AND internal_chat_id=? AND state='suppressed'",
        (target_uuid, target_chat_id),
    )
    assert int((await cur.fetchone())["n"] or 0) == len(task_uuids)
    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM web_task_notifications WHERE conversation_uuid=? AND internal_chat_id=? AND task_uuid IN (?,?) AND state IN ('pending','processing')",
        (target_uuid, target_chat_id, task_uuids[0], task_uuids[-1]),
    )
    assert int((await cur.fetchone())["n"] or 0) == 0
    cur = await web_env.db.conn.execute(
        "SELECT notification_uuid,state,claim_token,claimed_at FROM web_task_notifications WHERE notification_uuid IN (?,?,?,?) ORDER BY notification_uuid",
        tuple(row[0] for row in decoys),
    )
    assert [tuple(row) for row in await cur.fetchall()] == [
        ("already-delivered", "delivered", "delivered-claim", timestamp),
        ("source-real", "pending", "source-claim", timestamp),
        ("unrelated-task", "pending", "unrelated-claim", timestamp),
        ("wrong-chat", "pending", "wrong-chat-claim", timestamp),
    ]


@pytest.mark.parametrize("task_status", ["running", "needs_openbear_control"])
async def test_web_conversation_duplicate_rejects_active_or_control_source(web_env, task_status):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    source = await web_env.server._create_web_conversation(123, title=f"active {task_status}")
    await web_env.server.rath_dao.create_task(
        chat_id=int(source["internal_chat_id"]),
        parent_session_uuid=str(source["conversation_uuid"]),
        workflow_uuid="wf-duplicate-active",
        title=f"active {task_status}",
        status=task_status,
        task_uuid=f"duplicate-{task_status}-task",
    )
    if task_status == "running":
        await web_env.server._live_for(source).publish({
            "type": "accepted",
            "turnUuid": "active-duplicate-turn",
            "runUuid": "active-duplicate-run",
        })

    duplicate = await web_env.client.post(
        f"/api/conversations/{source['conversation_uuid']}/duplicate",
        cookies=cookie,
    )

    assert duplicate.status == 409
    assert (await duplicate.json())["error"] == "conversation_is_active"
    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM web_conversations WHERE owner_chat_id=? AND COALESCE(archived_at,0)=0",
        (123,),
    )
    assert int((await cur.fetchone())["n"] or 0) == 1


async def test_web_conversation_delete_refuses_to_clear_data_when_run_does_not_stop(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="busy delete")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    await MessageDAO(web_env.db).add(chat_id, "user", "keep me")

    class StuckRuns:
        def is_running(self, target_chat_id):
            return target_chat_id == chat_id

        async def cancel_and_wait(self, target_chat_id, *, timeout_s=10.0):
            assert target_chat_id == chat_id
            return False

    web_env.server.runs = StuckRuns()
    response = await web_env.client.delete(f"/api/conversations/{conv_uuid}", cookies=cookie)

    assert response.status == 409
    assert (await response.json())["error"] == "conversation_delete_busy"
    assert await web_env.server._conversation_row(123, conv_uuid) is not None
    assert [message.content for message in await MessageDAO(web_env.db).recent(chat_id)] == ["keep me"]


async def test_web_turn_suffix_delete_removes_model_ui_agent_artifact_and_summary_suffix(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="delete suffix")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    messages = MessageDAO(web_env.db)

    async def add_turn(turn_uuid: str, user_text: str, assistant_text: str):
        message_uuid = f"user-{turn_uuid}"
        await live.publish({"type": "accepted", "turnUuid": turn_uuid, "runUuid": turn_uuid})
        await live.publish({"type": "user", "turnUuid": turn_uuid, "messageUuid": message_uuid, "text": user_text})
        user_id = await web_env.server._persist_web_transcript_message(
            messages,
            chat_id,
            "user",
            user_text,
            conversation_uuid=conv_uuid,
            turn_uuid=turn_uuid,
            run_root_turn_uuid=turn_uuid,
            op_ids=[f"msg:{message_uuid}"],
        )
        renderer = _WebStreamRenderer(live)
        await renderer.finalize(assistant_text)
        assistant_id = await web_env.server._persist_web_transcript_message(
            messages,
            chat_id,
            "assistant",
            assistant_text,
            conversation_uuid=conv_uuid,
            turn_uuid=turn_uuid,
            run_root_turn_uuid=turn_uuid,
            op_ids=[f"assistant:{turn_uuid}:0"],
        )
        await renderer.close()
        return user_id, assistant_id

    first_ids = await add_turn("turn-1", "正确前置", "已完成调研")
    second_ids = await add_turn("turn-2", "错误指令", "错误回答")
    third_ids = await add_turn("turn-3", "继续错误方向", "继续错误回答")
    summaries = SummaryDAO(web_env.db)
    await summaries.add(chat_id, "valid summary", first_ids[1], 10)
    await summaries.add(chat_id, "invalid summary", second_ids[1], 20)
    await messages.mark_compacted(chat_id, second_ids[1])

    task_ids = {}
    task_memories = TaskMemoryDAO(web_env.db)
    for turn_uuid in ("turn-1", "turn-2", "turn-3"):
        task_ids[turn_uuid] = await web_env.server.rath_dao.create_task(
            chat_id=chat_id,
            parent_session_uuid=conv_uuid,
            workflow_uuid="wf-delete",
            title=turn_uuid,
            status="completed",
            turn_uuid=turn_uuid,
            parent_turn_uuid=turn_uuid,
            run_root_turn_uuid=turn_uuid,
        )
        await task_memories.create(
            conversation_uuid=conv_uuid,
            scope_type=SCOPE_AGENT_TASK,
            task_uuid=task_ids[turn_uuid],
            name=f"memory-{turn_uuid}",
            body=f"body-{turn_uuid}",
        )
    await web_env.db.conn.executemany(
        """
        INSERT INTO web_artifacts (
          artifact_uuid, conversation_uuid, owner_chat_id, internal_chat_id,
          turn_uuid, message_id, op_id, file_name, mime_type, size_bytes,
          sha256, storage_path, created_at, deleted_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)
        """,
        [
            ("artifact-1", conv_uuid, 123, chat_id, "turn-1", first_ids[1], "assistant:turn-1:0", "one.txt", "text/plain", 1, "sha-1", "/tmp/one", 1),
            ("artifact-2", conv_uuid, 123, chat_id, "turn-2", second_ids[1], "assistant:turn-2:0", "two.txt", "text/plain", 1, "sha-2", "/tmp/two", 2),
            ("artifact-3", conv_uuid, 123, chat_id, "turn-3", third_ids[1], "assistant:turn-3:0", "three.txt", "text/plain", 1, "sha-3", "/tmp/three", 3),
        ],
    )
    await web_env.db.conn.commit()

    response = await web_env.client.delete(
        f"/api/conversations/{conv_uuid}/turns/turn-2/suffix",
        cookies=cookie,
    )
    assert response.status == 200
    body = await response.json()
    assert body["deletedRootTurns"] == ["turn-2", "turn-3"]
    assert body["transcriptDeleted"]["messages"] == 4
    assert body["transcriptDeleted"]["summaries"] == 1

    cur = await web_env.db.conn.execute(
        "SELECT id, content, compacted FROM messages WHERE chat_id=? ORDER BY id",
        (chat_id,),
    )
    remaining_messages = [dict(item) for item in await cur.fetchall()]
    assert [item["id"] for item in remaining_messages] == list(first_ids)
    assert [item["compacted"] for item in remaining_messages] == [1, 1]
    latest_summary = await summaries.latest(chat_id)
    assert latest_summary is not None and latest_summary["summary"] == "valid summary"
    assert {op["turnUuid"] for op in await web_env.server._web_operations(conv_uuid)} == {"turn-1"}
    assert await web_env.server.rath_dao.get_task(task_ids["turn-1"]) is not None
    assert await web_env.server.rath_dao.get_task(task_ids["turn-2"]) is None
    assert await web_env.server.rath_dao.get_task(task_ids["turn-3"]) is None
    assert (await task_memories.list(
        conversation_uuid=conv_uuid,
        scope_type=SCOPE_AGENT_TASK,
        task_uuid=task_ids["turn-1"],
    ))["total"] == 1
    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM conversation_task_memories WHERE task_uuid IN (?,?)",
        (task_ids["turn-2"], task_ids["turn-3"]),
    )
    assert int((await cur.fetchone())["n"] or 0) == 0
    cur = await web_env.db.conn.execute(
        "SELECT artifact_uuid, deleted_at FROM web_artifacts WHERE conversation_uuid=? ORDER BY artifact_uuid",
        (conv_uuid,),
    )
    artifacts = {item["artifact_uuid"]: int(item["deleted_at"] or 0) for item in await cur.fetchall()}
    assert artifacts["artifact-1"] == 0
    assert artifacts["artifact-2"] > 0
    assert artifacts["artifact-3"] > 0


async def test_web_turn_suffix_delete_rejects_active_or_untraceable_turn(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="delete guard")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-old", "runUuid": "turn-old"})
    await live.publish({"type": "user", "turnUuid": "turn-old", "messageUuid": "old-user", "text": "旧历史"})
    await live.publish({"type": "done", "turnUuid": "turn-old"})
    await MessageDAO(web_env.db).add(chat_id, "user", "旧历史")

    class BusyRuns:
        def is_running(self, target_chat_id):
            return target_chat_id == chat_id

    web_env.server.runs = BusyRuns()
    busy = await web_env.client.delete(
        f"/api/conversations/{conv_uuid}/turns/turn-old/suffix",
        cookies=cookie,
    )
    assert busy.status == 409
    assert (await busy.json())["error"] == "conversation_is_active"

    web_env.server.runs = RunRegistry()
    untraceable = await web_env.client.delete(
        f"/api/conversations/{conv_uuid}/turns/turn-old/suffix",
        cookies=cookie,
    )
    assert untraceable.status == 409
    assert (await untraceable.json())["error"] == "turn_not_traceable"
    assert [item.content for item in await MessageDAO(web_env.db).recent(chat_id)] == ["旧历史"]


async def test_web_turn_suffix_delete_rolls_back_all_stores_on_failure(web_env, monkeypatch):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="delete rollback")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    turn_uuid = "turn-rollback"
    await live.publish({"type": "accepted", "turnUuid": turn_uuid, "runUuid": turn_uuid})
    await live.publish({"type": "user", "turnUuid": turn_uuid, "messageUuid": "rollback-user", "text": "不得半删"})
    await web_env.server._persist_web_transcript_message(
        MessageDAO(web_env.db),
        chat_id,
        "user",
        "不得半删",
        conversation_uuid=conv_uuid,
        turn_uuid=turn_uuid,
        run_root_turn_uuid=turn_uuid,
        op_ids=["msg:rollback-user"],
    )
    await live.publish({"type": "done", "turnUuid": turn_uuid})

    async def fail_delete(_task_uuids):
        raise RuntimeError("forced suffix delete failure")

    monkeypatch.setattr(web_env.server.rath_dao, "delete_task_records", fail_delete)
    response = await web_env.client.delete(
        f"/api/conversations/{conv_uuid}/turns/{turn_uuid}/suffix",
        cookies=cookie,
    )
    assert response.status == 500
    assert [item.content for item in await MessageDAO(web_env.db).recent(chat_id)] == ["不得半删"]
    assert any(op["turnUuid"] == turn_uuid for op in await web_env.server._web_operations(conv_uuid))


async def test_web_conversation_json_keeps_explicit_zero_cost(web_env):
    data = web_env.server._web_conversation_json({
        "conversation_uuid": "c1",
        "owner_chat_id": 123,
        "internal_chat_id": -1,
        "title": "t",
        "cost_usd": 0.0,
        "costUsd": 9.99,
    })
    assert data["costUsd"] == 0.0


async def test_conversation_list_ignores_waiting_control_notice_as_runtime(web_env):
    row = await web_env.server._create_web_conversation(123, title="informational notice")
    conv_uuid = str(row["conversation_uuid"])
    await web_env.server._publish_operation(
        conv_uuid,
        internal_chat_id=int(row["internal_chat_id"]),
        op_id="notice:task:old",
        op_type="notice",
        action="create",
        turn_uuid="turn-old",
        payload={"taskUuid": "old", "status": "needs_openbear_control", "text": "等待裁决"},
        status="needs_openbear_control",
        lifecycle="waiting_control",
    )
    await web_env.db.conn.commit()

    facts = await web_env.server._web_operation_facts_for_conversations([conv_uuid])
    assert facts[conv_uuid]["activeCount"] == 0
    assert facts[conv_uuid]["waitingControlCount"] == 0

    listed = await web_env.server._list_web_conversations(123)
    item = next(entry for entry in listed if entry["conversationUuid"] == conv_uuid)
    assert item["running"] is False
    assert item["status"] == "idle"
    public_ops = await web_env.server._web_operations(conv_uuid)
    notice = next(op for op in public_ops if op["opId"] == "notice:task:old")
    assert notice["lifecycle"] == "informational"


async def test_context_compaction_stats_fields(web_env):
    result = RunResult()
    result.last_usage.input_tokens = 267061
    outcome = CompactionOutcome(
        did=True,
        source="turn_epilogue",
        trigger_tokens=267061,
        threshold_tokens=250000,
        keep_recent=8,
        up_to_message_id=12121,
        old_message_count=234,
        kept_message_count=101,
        summary_id=7,
        summary="## Primary Request and Intent\n...",
        summary_tokens=4500,
        compression_model_label="openai/deepseek-v4-flash",
    )

    stats = web_env.server._run_stats_json(
        result,
        cost_usd=0,
        model="openai/gpt",
        think_level="off",
        context_window=400000,
        compactions=[(outcome, 42000)],
    )

    assert stats["contextTokens"] == 267061
    assert stats["contextCompacted"] is True
    assert stats["contextAfterCompactionTokens"] == 42000
    assert stats["contextCompaction"]["source"] == "turn_epilogue"
    assert stats["contextCompaction"]["triggerTokens"] == 267061
    assert stats["contextCompaction"]["afterTokens"] == 42000


async def test_web_build_history_after_compaction_replays_visible_xml_only(web_env):
    row = await web_env.server._create_web_conversation(123, title="visible XML tail", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    messages = MessageDAO(web_env.db)
    await messages.add(chat_id, "user", "用户可见请求")
    await messages.add(
        chat_id, "assistant", "正在执行 AgentWait",
        tool_calls=[ToolCall(id="wait-1", name="AgentWait", arguments="{}")],
    )
    await messages.add(
        chat_id, "tool", '{"notifications":["huge Plan and AgentWait runtime JSON"]}',
        tool_call_id="wait-1", name="AgentWait",
    )
    final_id = await messages.add(chat_id, "assistant", "最终可见结论")
    await SummaryDAO(web_env.db).add(chat_id, "已压缩的历史摘要", final_id, 10)
    await messages.mark_compacted(chat_id, final_id)

    history = await web_env.server._build_history(chat_id)

    assert [message["role"] for message in history] == ["user", "assistant"]
    context = str(history[0]["content"])
    assert "<history_messages>" in context
    assert "用户可见请求" in context
    assert "最终可见结论" in context
    assert "正在执行 AgentWait" not in context
    assert "huge Plan" not in context


async def test_web_context_compaction_gate_preserves_runtime_multimodal_tail():
    runtime_user = {"role": "user", "content": [
        {"type": "text", "text": "请看图"},
        {"type": "image", "path": "/tmp/pic.png", "mime_type": "image/png", "name": "pic.png"},
    ]}
    rebuilt_from_db = [
        {"role": "user", "content": "[此前对话摘要]\nsummary"},
        {"role": "assistant", "content": "好的，我已了解此前的上下文。"},
        {"role": "user", "content": "请看图"},
    ]
    outcome = CompactionOutcome(did=True, source="tool_batch", kept_message_count=1)

    seen_prompt_tokens = []

    class FakeCompactor:
        async def maybe_compact_detail(self, chat_id, prompt_tokens=None, source=""):
            seen_prompt_tokens.append(prompt_tokens)
            return outcome

    class Owner:
        def _estimate_prompt_tokens(self, *, system="", convo=None):
            return 123
        def _make_web_compactor(self, chat_id, *, model_label=""):
            return FakeCompactor()
        async def _build_history(self, chat_id):
            return [dict(m) for m in rebuilt_from_db]
        async def _emit_context_compaction_event(self, renderer, outcome, *, source=""):
            return None

    seen = []
    async def on_compacted(outcome, after_tokens):
        seen.append((outcome, after_tokens))

    gate = _WebContextCompactionGate(Owner(), 1, system="sys", on_compacted=on_compacted)
    new_convo = await gate.maybe_compact_and_rebuild(
        source="agent_result_preflight",
        prompt_tokens=500,
        convo=[{"role": "user", "content": "old"}, runtime_user],
    )

    assert seen_prompt_tokens == [500]
    assert new_convo[-1]["content"] == runtime_user["content"]
    assert any(block.get("type") == "image" for block in new_convo[-1]["content"])
    assert seen and seen[0][1] == 123


async def test_web_agent_result_compaction_preserves_complete_tool_pair_tail():
    full_result = json.dumps({
        "resultOutputTokens": 9000,
        "resultCount": 2,
        "notifications": [{"result": "完整证据" * 20_000}],
    }, ensure_ascii=False)
    tool_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "wait-full-result",
            "type": "function",
            "function": {"name": "AgentWait", "arguments": '{"mode":"event_only"}'},
        }],
        "native_output_items": [{
            "type": "function_call",
            "call_id": "wait-full-result",
            "name": "AgentWait",
            "arguments": '{"mode":"event_only"}',
        }],
    }
    tool_result = {
        "role": "tool",
        "tool_call_id": "wait-full-result",
        "name": "AgentWait",
        "content": full_result,
    }
    outcome = CompactionOutcome(did=True, source="agent_result_preflight", kept_message_count=2)

    class FakeCompactor:
        async def maybe_compact_detail(self, chat_id, prompt_tokens=None, source=""):
            return outcome

    class Owner:
        def _estimate_prompt_tokens(self, *, system="", convo=None):
            return 123

        def _make_web_compactor(self, chat_id, *, model_label=""):
            return FakeCompactor()

        async def _build_history(self, chat_id):
            return [
                {"role": "user", "content": "[此前对话摘要]\nsummary"},
                {"role": "assistant", "content": "db placeholder"},
                {"role": "tool", "tool_call_id": "wait-full-result", "name": "AgentWait", "content": "db placeholder"},
            ]

        async def _emit_context_compaction_event(self, renderer, outcome, *, source=""):
            return None

    epoch_resets = []
    gate = _WebContextCompactionGate(
        Owner(), 1, system="sys", on_cache_epoch_reset=lambda: epoch_resets.append(1),
    )
    rebuilt = await gate.maybe_compact_and_rebuild(
        source="agent_result_preflight",
        prompt_tokens=10_000,
        convo=[
            {"role": "user", "content": "old"},
            _trusted_task_memory_state(),
            tool_call,
            tool_result,
        ],
    )

    assert epoch_resets == [1]
    assert not any(is_task_memory_runtime_message(message) for message in rebuilt)
    assert rebuilt[-2] == {key: value for key, value in tool_call.items() if key != "native_output_items"}
    assert "native_output_items" not in rebuilt[-2]
    assert rebuilt[-1] == tool_result
    assert rebuilt[-1]["content"] == full_result


async def test_web_emergency_compactor_preserves_runtime_multimodal_tail():
    runtime_user = {"role": "user", "content": [{"type": "image", "path": "/tmp/pic.png"}]}
    outcome = CompactionOutcome(did=True, source="emergency", kept_message_count=1)

    class FakeCompactor:
        async def force_compact_detail(self, chat_id, *, source="emergency", keep=None):
            return outcome

    class Owner:
        def _estimate_prompt_tokens(self, *, system="", convo=None):
            return 77
        def _make_web_compactor(self, chat_id, *, model_label=""):
            return FakeCompactor()
        async def _build_history(self, chat_id):
            return [{"role": "user", "content": "[此前对话摘要]\nsummary"}, {"role": "user", "content": "visible"}]
        async def _emit_context_compaction_event(self, renderer, outcome, *, source=""):
            return None

    epoch_resets = []
    compactor = _WebEmergencyCompactor(
        Owner(), 1, system="sys", on_cache_epoch_reset=lambda: epoch_resets.append(1),
    )
    new_convo = await compactor.compact_and_rebuild(convo=[
        {"role": "user", "content": "old"},
        _trusted_task_memory_state(),
        runtime_user,
    ])

    assert epoch_resets == [1]
    assert not any(is_task_memory_runtime_message(message) for message in new_convo)
    assert new_convo[-1]["content"] == runtime_user["content"]


async def test_web_responses_native_context_persists_privately_and_reloads_next_turn(web_env, monkeypatch):
    marker = "test-opaque-web-controller"
    reasoning_item = {
        "type": "reasoning",
        "id": "web-reasoning-1",
        "summary": [{"type": "summary_text", "text": "调用 echo"}],
        "encrypted_content": marker,
    }
    function_item = {
        "type": "function_call",
        "id": "web-function-1",
        "call_id": "web-call-1",
        "name": "echo",
        "arguments": '{"x":"web"}',
    }
    first_backend = FakeStreamBackend([
        [
            StreamEvent(kind="reasoning", text="调用 echo"),
            StreamEvent(kind="native_output_item", native_output_items=[reasoning_item]),
            StreamEvent(kind="native_output_item", native_output_items=[function_item]),
            StreamEvent(
                kind="tool_call",
                tool_calls=[ToolCall(id="web-call-1", name="echo", arguments='{"x":"web"}')],
            ),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(
                kind="native_output_item",
                native_output_items=[{
                    "type": "message",
                    "id": "web-message-1",
                    "content": [{"type": "output_text", "text": "第一轮完成"}],
                }],
            ),
            StreamEvent(kind="content", text="第一轮完成"),
            StreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    first_backend.protocol = "responses"
    web_env.server.llm_factory = FakeRunFactory(first_backend, context_window=128000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    registry = ToolRegistry()

    async def _echo(args):
        return f"echo:{args.get('x', '')}"

    registry.add("echo", "echo", {"type": "object", "properties": {}}, _echo)
    web_env.server.tools = registry

    async def _fake_system_prompt():
        return "stable system"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    row = await web_env.server._create_web_conversation(123, title="native context", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    await TaskMemoryDAO(web_env.db).create(
        conversation_uuid=str(row["conversation_uuid"]),
        scope_type=SCOPE_CONVERSATION,
        name="controller responses state",
        description="private cross-turn state",
        visible_to_agents=True,
    )
    live = _WebLiveStream(str(row["conversation_uuid"]), chat_id)
    await live.publish({"type": "accepted", "turnUuid": "native-turn-1"})

    assert await web_env.server._run_web_turn(
        chat_id,
        "第一轮",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid="native-turn-1",
    ) is True
    assert len(first_backend.seen_opts) == 2
    assert all(options.get("native_continuation") is True for options in first_backend.seen_opts)
    first_request, same_turn_request = first_backend.seen_convos
    assert same_turn_request[:len(first_request)] == first_request
    assert sum(is_task_memory_runtime_message(message) for message in same_turn_request) == 1
    assert first_backend.seen_systems[0] == first_backend.seen_systems[1] == "stable system"
    assert first_backend.seen_tools[0] == first_backend.seen_tools[1]

    cur = await web_env.db.conn.execute(
        "SELECT state_json FROM controller_model_contexts WHERE chat_id=?",
        (chat_id,),
    )
    private_row = await cur.fetchone()
    assert private_row is not None and marker in str(private_row["state_json"])
    assert "openbear-task-memory-state" in str(private_row["state_json"])
    assert "openbear-task-memory-state" not in first_backend.seen_systems[0]
    rows = await MessageDAO(web_env.db).recent(chat_id)
    visible_payload = json.dumps(
        [web_env.server._message_json(item) for item in rows],
        ensure_ascii=False,
        default=str,
    )
    assert marker not in visible_payload
    assert "openbear-task-memory-state" not in visible_payload
    plain_history = await web_env.server._build_history(chat_id)
    assert marker not in json.dumps(plain_history, ensure_ascii=False, default=str)
    assert "openbear-task-memory-state" not in json.dumps(plain_history, ensure_ascii=False, default=str)
    assert not any(message.get("native_output_items") for message in plain_history)
    global_memory_rows = []
    for table in ("memory_entries", "memory_docs", "memory_secrets"):
        cur = await web_env.db.conn.execute(f"SELECT * FROM {table}")
        global_memory_rows.extend(dict(item) for item in await cur.fetchall())
    assert "openbear-task-memory-state" not in json.dumps(
        global_memory_rows, ensure_ascii=False, default=str,
    )

    second_backend = FakeStreamBackend([[
        StreamEvent(
            kind="native_output_item",
            native_output_items=[{
                "type": "message",
                "id": "web-message-2",
                "content": [{"type": "output_text", "text": "第二轮完成"}],
            }],
        ),
        StreamEvent(kind="content", text="第二轮完成"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    second_backend.protocol = "responses"
    # A new backend and a new Agent instance simulate a fresh Web turn/runtime load.
    web_env.server.llm_factory = FakeRunFactory(second_backend, context_window=128000)
    await live.publish({"type": "accepted", "turnUuid": "native-turn-2"})

    assert await web_env.server._run_web_turn(
        chat_id,
        "第二轮",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid="native-turn-2",
    ) is True

    assert second_backend.seen_opts[0].get("native_continuation") is True
    cross_turn_request = second_backend.seen_convos[0]
    assert cross_turn_request[:len(same_turn_request)] == same_turn_request
    assert sum(is_task_memory_runtime_message(message) for message in cross_turn_request) == 1
    assert second_backend.seen_systems[0] == first_backend.seen_systems[-1]
    assert second_backend.seen_tools[0] == first_backend.seen_tools[-1]
    replayed = _to_responses_input(cross_turn_request)
    assert sum(item.get("type") == "reasoning" for item in replayed) == 1
    assert sum(item.get("type") == "function_call" for item in replayed) == 1
    assert sum(item.get("type") == "function_call_output" for item in replayed) == 1
    assert reasoning_item in replayed
    assert function_item in replayed
    assert "第一轮完成" in json.dumps(replayed, ensure_ascii=False)


@pytest.mark.parametrize("protocol", ["chat", "anthropic"])
async def test_web_neutral_model_context_keeps_task_memory_prefix_across_turns(
    web_env, monkeypatch, protocol,
):
    async def _fake_system_prompt():
        return "stable neutral system"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.tools = ToolRegistry()
    row = await web_env.server._create_web_conversation(
        123, title=f"{protocol} private context", model="openai/gpt",
    )
    chat_id = int(row["internal_chat_id"])
    conversation_uuid = str(row["conversation_uuid"])
    await TaskMemoryDAO(web_env.db).create(
        conversation_uuid=conversation_uuid,
        scope_type=SCOPE_CONVERSATION,
        name=f"controller {protocol} state",
        description="private neutral cross-turn state",
        visible_to_agents=True,
    )
    live = _WebLiveStream(conversation_uuid, chat_id)

    first_backend = FakeStreamBackend([[
        StreamEvent(kind="content", text="第一轮完成"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    first_backend.protocol = protocol
    web_env.server.llm_factory = FakeRunFactory(first_backend, context_window=128000)
    await live.publish({"type": "accepted", "turnUuid": f"{protocol}-turn-1"})
    assert await web_env.server._run_web_turn(
        chat_id,
        "第一轮",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid=f"{protocol}-turn-1",
    ) is True

    first_request = first_backend.seen_convos[0]
    assert sum(is_task_memory_runtime_message(message) for message in first_request) == 1
    assert first_backend.seen_opts[0].get("native_continuation") is not True
    session_uuid = await MessageDAO(web_env.db).current_session_uuid(chat_id)
    private_state = await MessageDAO(web_env.db).load_controller_model_context(
        chat_id,
        conversation_uuid=conversation_uuid,
        session_id=session_uuid,
        protocol=protocol,
        model="fake-gpt",
        model_label="openai/gpt",
    )
    assert private_state is not None
    assert "openbear-task-memory-state" in json.dumps(private_state, ensure_ascii=False)
    visible_rows = await MessageDAO(web_env.db).recent(chat_id)
    assert "openbear-task-memory-state" not in json.dumps(
        [web_env.server._message_json(item) for item in visible_rows],
        ensure_ascii=False,
        default=str,
    )

    second_backend = FakeStreamBackend([[
        StreamEvent(kind="content", text="第二轮完成"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    second_backend.protocol = protocol
    web_env.server.llm_factory = FakeRunFactory(second_backend, context_window=128000)
    await live.publish({"type": "accepted", "turnUuid": f"{protocol}-turn-2"})
    assert await web_env.server._run_web_turn(
        chat_id,
        "第二轮",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid=f"{protocol}-turn-2",
    ) is True

    second_request = second_backend.seen_convos[0]
    assert second_request[:len(first_request)] == first_request
    assert sum(is_task_memory_runtime_message(message) for message in second_request) == 1
    assert second_backend.seen_systems[0] == first_backend.seen_systems[0]
    assert second_backend.seen_tools[0] == first_backend.seen_tools[0]


async def test_web_controller_physical_retry_keeps_task_memory_state_prefix(web_env, monkeypatch):
    async def _fake_system_prompt():
        return "stable retry system"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    web_env.server.config.agent.retry_backoff_s = 0
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.tools = ToolRegistry()
    backend = FakeStreamBackend([
        [StreamEvent(kind="error", error="temporary", retryable=True)],
        [
            StreamEvent(kind="content", text="重试完成"),
            StreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    backend.protocol = "chat"
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    row = await web_env.server._create_web_conversation(
        123, title="controller physical retry", model="openai/gpt",
    )
    chat_id = int(row["internal_chat_id"])
    conversation_uuid = str(row["conversation_uuid"])
    await TaskMemoryDAO(web_env.db).create(
        conversation_uuid=conversation_uuid,
        scope_type=SCOPE_CONVERSATION,
        name="controller retry state",
        description="stable during physical retry",
        visible_to_agents=True,
    )
    live = _WebLiveStream(conversation_uuid, chat_id)
    await live.publish({"type": "accepted", "turnUuid": "controller-retry-turn"})

    assert await web_env.server._run_web_turn(
        chat_id,
        "执行重试",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid="controller-retry-turn",
    ) is True

    assert len(backend.seen_convos) == 2
    first_request, retry_request = backend.seen_convos
    assert retry_request[:len(first_request)] == first_request
    assert sum(is_task_memory_runtime_message(message) for message in retry_request) == 1
    assert backend.seen_systems[0] == backend.seen_systems[1]
    assert backend.seen_tools[0] == backend.seen_tools[1]


async def test_web_run_pre_compaction_ignores_stale_previous_usage_and_emits_event(web_env, monkeypatch):
    cfg = _cfg()
    cfg.agent.compact_ratio = 0.5
    cfg.agent.keep_recent_messages = 4
    cfg.models.providers["openai"].models[0].compact_trigger_tokens = 100
    backend = FakeStreamBackend([[StreamEvent(kind="content", text="完成"), StreamEvent(kind="finish", finish_reason="stop")]])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=1000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.tools = ToolRegistry()
    async def _fake_system_prompt():
        return "sys"
    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)

    row = await web_env.server._create_web_conversation(123, title="新对话", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    messages = MessageDAO(web_env.db)
    session_uuid = await messages.current_session_uuid(chat_id)
    await messages.add_model_call(
        chat_id,
        session_uuid=session_uuid,
        model="openai/gpt",
        protocol="fake",
        last_usage=Usage(input_tokens=267061),
    )
    for i in range(8):
        await messages.add(chat_id, "user", f"旧消息{i}", tokens=1)

    events_seen = []
    async def _sink(event):
        payload = dict(event)
        payload["seq"] = len(events_seen) + 1
        payload.setdefault("eventUuid", f"event-{payload['seq']}")
        payload.setdefault("eventId", payload["eventUuid"])
        events_seen.append(payload)
        return payload
    live = _WebLiveStream(str(row["conversation_uuid"]), chat_id, event_sink=_sink)
    renderer = _WebStreamRenderer(live)
    await live.publish({"type": "accepted", "turnUuid": "turn-compact", "ts": 1000})
    await web_env.server._run_web_turn(chat_id, "新问题", renderer, conversation=row)

    events = list(events_seen)
    compaction_starts = [e for e in events if e.get("type") == "tool_start" and e.get("name") == "ContextCompaction"]
    # 如果仍使用 stale previous usage=267061，这里会误触发压缩；修复后估算当前上下文低于阈值，不压缩。
    assert compaction_starts == []
    assert backend.complete_calls == 0


async def test_web_run_restores_anchored_private_context_after_existing_summary(web_env, monkeypatch):
    """A past compaction must not reset every later root turn."""
    cfg = _cfg()
    backend = FakeStreamBackend([[
        StreamEvent(kind="content", text="continued"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.tools = ToolRegistry()

    async def _fake_system_prompt():
        return "stable system"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    row = await web_env.server._create_web_conversation(
        123, title="summary private continuation", model="openai/gpt",
    )
    chat_id = int(row["internal_chat_id"])
    conversation_uuid = str(row["conversation_uuid"])
    messages = MessageDAO(web_env.db)

    compacted_id = await messages.add(chat_id, "user", "old compacted message", tokens=1)
    await SummaryDAO(web_env.db).add(chat_id, "existing summary", compacted_id, 2)
    await messages.mark_compacted(chat_id, compacted_id)
    await messages.add(chat_id, "assistant", "visible tail", tokens=1)

    private_messages = [
        {"role": "user", "content": "[summary projection] existing summary"},
        {"role": "assistant", "content": "summary acknowledged"},
        {"role": "user", "content": "private-after-summary-marker"},
    ]
    session_uuid = await messages.current_session_uuid(chat_id)
    await messages.save_controller_model_context(
        chat_id,
        conversation_uuid=conversation_uuid,
        session_id=session_uuid,
        protocol="fake",
        model="fake-gpt",
        model_label="openai/gpt",
        state={"version": 1, "messages": private_messages},
    )

    live = _WebLiveStream(conversation_uuid, chat_id)
    await live.publish({"type": "accepted", "turnUuid": "turn-after-summary"})
    assert await web_env.server._run_web_turn(
        chat_id,
        "next question",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid="turn-after-summary",
    ) is True

    assert backend.seen_convos
    first_request = backend.seen_convos[0]
    assert first_request[:len(private_messages)] == private_messages
    assert first_request[-1]["role"] == "user"
    assert "next question" in str(first_request[-1]["content"])


async def test_web_run_pre_compaction_uses_provider_snapshot_for_restored_private_context(web_env, monkeypatch):
    """Only a compatible restored private context may reuse its last real prompt size."""
    cfg = _cfg()
    cfg.models.providers["openai"].models[0].compact_trigger_tokens = 100
    backend = FakeStreamBackend([[StreamEvent(kind="content", text="completed"), StreamEvent(kind="finish", finish_reason="stop")]])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=1000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.tools = ToolRegistry()

    async def _fake_system_prompt():
        return "sys"

    def _fake_estimate_prompt_tokens(*, system="", convo=None):
        return 10

    preflight_calls = []

    async def _fake_precompact(chat_id, prompt_tokens, *, model_label, source="pre_model_request"):
        preflight_calls.append((chat_id, prompt_tokens, model_label, source))
        return CompactionOutcome(did=False, source=source, trigger_tokens=prompt_tokens)

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    monkeypatch.setattr(web_env.server, "_estimate_prompt_tokens", _fake_estimate_prompt_tokens)
    monkeypatch.setattr(web_env.server, "_pre_compact_before_web_turn", _fake_precompact)

    row = await web_env.server._create_web_conversation(123, title="restored private context", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    conversation_uuid = str(row["conversation_uuid"])
    messages = MessageDAO(web_env.db)
    session_uuid = await messages.current_session_uuid(chat_id)
    await messages.save_controller_model_context(
        chat_id,
        conversation_uuid=conversation_uuid,
        session_id=session_uuid,
        protocol="fake",
        model="fake-gpt",
        model_label="openai/gpt",
        state={"version": 1, "messages": [{"role": "user", "content": "private checkpoint"}]},
    )
    await messages.add_model_call(
        chat_id,
        session_uuid=session_uuid,
        model="openai/gpt",
        protocol="fake",
        call_kind="controller_request",
        last_usage=Usage(input_tokens=150),
    )
    live = _WebLiveStream(conversation_uuid, chat_id)
    renderer = _WebStreamRenderer(live)
    await live.publish({"type": "accepted", "turnUuid": "turn-restored-context"})

    assert await web_env.server._run_web_turn(
        chat_id,
        "next question",
        renderer,
        conversation=row,
        root_turn_uuid="turn-restored-context",
    ) is True
    assert preflight_calls == [(chat_id, 150, "openai/gpt", "pre_model_request")]


async def test_detached_agent_result_preflight_adds_batch_output_tokens_before_model(web_env, monkeypatch):
    cfg = _cfg()
    backend = FakeStreamBackend([
        [StreamEvent(kind="content", text="已汇总完整 Agent 结论"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.tools = ToolRegistry()

    async def _fake_system_prompt():
        return "sys"

    def _fake_estimate_prompt_tokens(*, system="", convo=None):
        return 1200 if convo else 1000

    preflight_calls = []

    async def _fake_precompact(chat_id, prompt_tokens, *, model_label, source="pre_model_request"):
        preflight_calls.append((chat_id, prompt_tokens, model_label, source))
        return CompactionOutcome(did=False, source=source, trigger_tokens=prompt_tokens)

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    monkeypatch.setattr(web_env.server, "_estimate_prompt_tokens", _fake_estimate_prompt_tokens)
    monkeypatch.setattr(web_env.server, "_pre_compact_before_web_turn", _fake_precompact)

    row = await web_env.server._create_web_conversation(123, title="Agent 回传", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    messages = MessageDAO(web_env.db)
    session_uuid = await messages.current_session_uuid(chat_id)
    await messages.add_model_call(
        chat_id,
        session_uuid=session_uuid,
        model="openai/gpt",
        protocol="fake",
        call_kind="controller_request",
        last_usage=Usage(input_tokens=9000),
    )
    # A newer/larger child request must never masquerade as parent context.
    await messages.add_model_call(
        chat_id,
        session_uuid=session_uuid,
        model="openai/gpt",
        protocol="fake",
        call_kind="agent_request",
        last_usage=Usage(input_tokens=50_000),
    )
    live = _WebLiveStream(str(row["conversation_uuid"]), chat_id)
    renderer = _WebStreamRenderer(live)
    await live.publish({"type": "accepted", "turnUuid": "turn-agent-batch"})

    ok = await web_env.server._run_web_turn(
        chat_id,
        "完整 Agent 结果",
        renderer,
        conversation=row,
        task_notification=True,
        task_notification_payload={
            "kind": "task-notification-batch",
            "status": "completed",
            "resultOutputTokens": 7000,
            "resultCount": 2,
        },
        root_turn_uuid="turn-agent-batch",
    )

    assert ok is True
    assert preflight_calls == [(chat_id, 16_256, "openai/gpt", "agent_result_preflight")]
    assert backend.seen_convos
    assert any("完整 Agent 结果" in str(message.get("content") or "") for message in backend.seen_convos[0])


async def test_web_run_turn_epilogue_compaction_is_sync_visible_and_updates_stats(web_env, monkeypatch):
    cfg = _cfg()
    cfg.agent.compact_ratio = 0.5
    cfg.agent.keep_recent_messages = 4
    cfg.models.providers["openai"].models[0].compact_trigger_tokens = 100
    backend = FakeStreamBackend([[StreamEvent(kind="content", text="完成"), StreamEvent(kind="usage", usage=Usage(input_tokens=150, total_tokens=160)), StreamEvent(kind="finish", finish_reason="stop")]])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=1000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.tools = ToolRegistry()
    async def _fake_system_prompt():
        return "sys"
    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    # Keep pre-model gates below the threshold; provider usage=150 should be the
    # sole trigger so this test isolates the turn-epilogue cache-epoch boundary.
    monkeypatch.setattr(
        web_env.server, "_estimate_prompt_tokens", lambda *, system="", convo=None: 50,
    )

    row = await web_env.server._create_web_conversation(123, title="新对话", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    conversation_uuid = str(row["conversation_uuid"])
    await TaskMemoryDAO(web_env.db).create(
        conversation_uuid=conversation_uuid,
        scope_type=SCOPE_CONVERSATION,
        name="epilogue compaction state",
        description="must remain private",
        visible_to_agents=True,
    )
    messages = MessageDAO(web_env.db)
    for i in range(12):
        await messages.add(chat_id, "user", f"历史{i}", tokens=1)

    events_seen = []
    async def _sink(event):
        payload = dict(event)
        payload["seq"] = len(events_seen) + 1
        payload.setdefault("eventUuid", f"event-{payload['seq']}")
        payload.setdefault("eventId", payload["eventUuid"])
        events_seen.append(payload)
        return payload
    live = _WebLiveStream(str(row["conversation_uuid"]), chat_id, event_sink=_sink)
    renderer = _WebStreamRenderer(live)
    await live.publish({"type": "accepted", "turnUuid": "turn-compact", "ts": 1000})
    await web_env.server._run_web_turn(chat_id, "新问题", renderer, conversation=row)

    events = list(events_seen)
    compaction_starts = [e for e in events if e.get("type") == "tool_start" and e.get("name") == "ContextCompaction"]
    compaction_results = [e for e in events if e.get("type") == "tool_result" and e.get("name") == "ContextCompaction"]
    assert len(compaction_starts) == 1
    assert len(compaction_results) == 1
    assert "## 上下文压缩完成" in compaction_results[0]["result"]
    stats_events = [e for e in events if e.get("type") == "stats"]
    assert stats_events
    final_stats = stats_events[-1]["stats"]
    assert final_stats["contextTokens"] == 150
    ledger_usage = final_stats["ledgerUsage"]
    assert ledger_usage["ledgerRevision"] > 0
    assert ledger_usage["inputTokens"] == 150
    assert ledger_usage["outputTokens"] == 0
    assert ledger_usage["cacheReadTokens"] == 0
    assert ledger_usage["cacheWriteTokens"] == 0
    assert ledger_usage["costUsd"] == final_stats["ledgerCostUsd"]
    durable = await messages.usage_totals(chat_id)
    assert ledger_usage["inputTokens"] == durable.input_tokens
    assert ledger_usage["outputTokens"] == durable.output_tokens
    assert final_stats["contextCompacted"] is True
    assert final_stats["contextCompaction"]["source"] == "turn_epilogue"
    assert final_stats["contextAfterCompactionTokens"] > 0
    session_uuid = await messages.current_session_uuid(chat_id)
    private_state = await messages.load_controller_model_context(
        chat_id,
        conversation_uuid=conversation_uuid,
        session_id=session_uuid,
        protocol="fake",
        model="fake-gpt",
        model_label="openai/gpt",
    )
    assert private_state is not None
    private_runtime_states = [
        message for message in private_state["messages"]
        if is_task_memory_runtime_message(message)
    ]
    assert len(private_runtime_states) == 1
    assert private_runtime_states[0]["_openbear_runtime"]["epoch"] == 1
    summary = await SummaryDAO(web_env.db).latest(chat_id)
    assert summary is not None
    assert "openbear-task-memory-state" not in json.dumps(summary, ensure_ascii=False, default=str)
    assert "openbear-task-memory-state" not in json.dumps(
        await web_env.server._build_history(chat_id), ensure_ascii=False, default=str,
    )


async def test_task_notification_stats_merge_agent_usage(web_env):
    result = RunResult()
    result.model_calls = 1
    result.model_ok = 1
    result.usage.input_tokens = 100
    result.usage.output_tokens = 20
    result.last_usage.input_tokens = 100
    payload = {
        "batch": True,
        "results": [
            {"status": "completed", "task": {"taskUuid": "t1", "status": "completed", "modelCalls": 3, "toolCalls": 7, "tokens": {"input": 1000, "output": 200, "cache": 600}, "costUsd": 1.25}},
            {"status": "completed", "task": {"taskUuid": "t2", "status": "completed", "modelCalls": 2, "toolCalls": 5, "tokens": {"input": 500, "output": 80, "cache": 100}, "costUsd": 0.75}},
        ],
    }

    web_env.server._merge_agent_notification_stats(result, payload)
    web_env.server._merge_agent_notification_stats(result, payload)
    stats = web_env.server._run_stats_json(result, cost_usd=0.01, model="openai/gpt", think_level="off", context_window=4000)

    assert result.expert_tasks == 2
    assert stats["modelCalls"] == 6
    assert stats["toolCalls"] == 12
    assert stats["expertUsage"] == {"inputTokens": 800, "outputTokens": 280, "cacheReadTokens": 700, "cacheWriteTokens": 0, "totalTokens": 1780}
    assert stats["expertTaskUuids"] == ["t1", "t2"]
    assert stats["contextTokens"] == 100
    assert stats["costUsd"] == 2.01
    assert "ledgerCostUsd" not in stats
    assert "ledgerUsage" not in stats
    ledger_stats = web_env.server._run_stats_json(
        result,
        cost_usd=0.01,
        model="openai/gpt",
        think_level="off",
        context_window=4000,
        ledger_usage={
            "ledgerRevision": 42,
            "inputTokens": 1200,
            "outputTokens": 300,
            "cacheReadTokens": 700,
            "cacheWriteTokens": 10,
            "costUsd": 9.876,
        },
    )
    assert ledger_stats["ledgerUsage"] == {
        "ledgerRevision": 42,
        "inputTokens": 1200,
        "outputTokens": 300,
        "cacheReadTokens": 700,
        "cacheWriteTokens": 10,
        "costUsd": 9.876,
    }
    assert ledger_stats["ledgerCostUsd"] == 9.876
    legacy_stats = web_env.server._run_stats_json(
        result,
        cost_usd=0.01,
        model="openai/gpt",
        think_level="off",
        context_window=4000,
        ledger_cost_usd=1.234,
    )
    assert legacy_stats["ledgerCostUsd"] == 1.234


async def test_durable_rath_task_stats_merge_exact_usage_once(web_env):
    result = RunResult()
    task = SimpleNamespace(
        task_uuid="durable-task",
        status="completed",
        model_call_count=3,
        tool_call_count=4,
        input_tokens=55,
        output_tokens=7,
        cache_read_tokens=600,
        cache_write_tokens=11,
        cost_usd=0.42,
        started_at=10,
        finished_at=16,
    )

    assert web_env.server._merge_agent_task_stats(result, task) is True
    assert web_env.server._merge_agent_task_stats(result, task) is False
    assert result.expert_tasks == 1
    assert result.expert_model_calls == 3
    assert result.expert_tool_calls == 4
    assert result.expert_duration_ms == 6000
    assert result.expert_cost_usd == pytest.approx(0.42)
    assert result.expert_usage == Usage(
        input_tokens=55,
        output_tokens=7,
        cache_read_tokens=600,
        cache_write_tokens=11,
        total_tokens=673,
    )
    stats = web_env.server._run_stats_json(
        result,
        cost_usd=0.0,
        model="openai/gpt",
        think_level="off",
        context_window=4000,
    )
    assert stats["expertTaskUuids"] == ["durable-task"]
    assert stats["expertUsage"]["totalTokens"] == 673

    running = SimpleNamespace(task_uuid="running-task", status="running", input_tokens=999)
    assert web_env.server._merge_agent_task_stats(result, running) is False
    assert result.expert_tasks == 1


async def test_web_api_requires_session_and_path_traversal_not_served(web_env):
    resp = await web_env.client.get("/api/auth/session")
    assert resp.status == 401
    data = await resp.json()
    assert data["error"] == "unauthorized"

    resp = await web_env.client.get("/%2e%2e/openbear.json", allow_redirects=False)
    assert resp.status in {302, 404}
    body = await resp.text()
    assert "botToken" not in body


async def test_secret_key_login_requires_telegram_confirmation(web_env):
    key = await web_env.server.get_secret_key()
    resp = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    assert resp.status == 200
    assert web_env.bot.sent
    start_data = await resp.json()
    req_uuid = start_data["requestUuid"]

    wait = await web_env.client.get(f"/api/auth/login/status/{req_uuid}")
    assert (await wait.json())["status"] == "pending"

    pending_consume = await web_env.client.post(f"/api/auth/login/consume/{req_uuid}")
    assert pending_consume.status == 409

    status = await web_env.server.decide_login_request(req_uuid, approved=True, decided_by=123)
    assert status == "approved"
    approved = await web_env.client.post(f"/api/auth/login/consume/{req_uuid}")
    assert approved.status == 200
    assert "openbear_web_session" in approved.cookies

    session_cookie = approved.cookies["openbear_web_session"].value
    me = await web_env.client.get("/api/auth/session", cookies={"openbear_web_session": session_cookie})
    assert me.status == 200
    assert (await me.json())["chatId"] == 123


async def test_auth_api_uses_documented_paths_and_no_legacy_session_alias(web_env):
    key = await web_env.server.get_secret_key()
    start = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    assert start.status == 200
    start_data = await start.json()
    req_uuid = start_data["requestUuid"]
    assert start_data["statusUrl"].endswith(f"/api/auth/login/status/{req_uuid}")

    status = await web_env.client.get(f"/api/auth/login/status/{req_uuid}")
    assert (await status.json())["status"] == "pending"
    await web_env.server.decide_login_request(req_uuid, approved=True, decided_by=123)
    approved = await web_env.client.post(f"/api/auth/login/consume/{req_uuid}")
    cookie = approved.cookies["openbear_web_session"].value

    assert (await web_env.client.get("/api/auth/session", cookies={"openbear_web_session": cookie})).status == 200
    assert (await web_env.client.get("/api/me", cookies={"openbear_web_session": cookie})).status == 404


async def test_reset_secret_revokes_existing_sessions(web_env):
    key = await web_env.server.get_secret_key()
    resp = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    req_uuid = (await resp.json())["requestUuid"]
    await web_env.server.decide_login_request(req_uuid, approved=True, decided_by=123)
    approved = await web_env.client.post(f"/api/auth/login/consume/{req_uuid}")
    session_cookie = approved.cookies["openbear_web_session"].value
    assert (await web_env.client.get("/api/auth/session", cookies={"openbear_web_session": session_cookie})).status == 200

    old_key = key
    new_key = await web_env.server.reset_secret_key(actor="test", chat_id=123)
    assert new_key != old_key
    assert (await web_env.client.get("/api/auth/session", cookies={"openbear_web_session": session_cookie})).status == 401


async def test_bad_secret_rate_limit_and_success_clears_failures(web_env):
    key = await web_env.server.get_secret_key()

    bad = await web_env.client.post("/api/auth/login/start", json={"secret": "bad-1"})
    assert bad.status == 403
    ok = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    assert ok.status == 200
    cur = await web_env.db.conn.execute("SELECT COUNT(*) AS n FROM web_login_failures")
    row = await cur.fetchone()
    assert row["n"] == 0

    for i in range(4):
        resp = await web_env.client.post("/api/auth/login/start", json={"secret": f"bad-{i}"})
        assert resp.status == 403
    limited = await web_env.client.post("/api/auth/login/start", json={"secret": "bad-final"})
    assert limited.status == 429
    assert int(limited.headers["Retry-After"]) > 0
    assert (await limited.json())["error"] == "rate_limited"

    still_limited = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    assert still_limited.status == 429

    audit = await web_env.server._login_rate_limit_status("127.0.0.1")
    assert audit["blocked"] is True


async def test_pending_login_reject_and_expired_status(web_env):
    key = await web_env.server.get_secret_key()
    resp = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    req_uuid = (await resp.json())["requestUuid"]

    status = await web_env.server.decide_login_request(req_uuid, approved=False, decided_by=123)
    assert status == "rejected"
    wait = await web_env.client.get(f"/api/auth/login/status/{req_uuid}")
    assert (await wait.json())["status"] == "rejected"
    rejected = await web_env.client.post(f"/api/auth/login/consume/{req_uuid}")
    assert rejected.status == 403
    assert (await rejected.json())["status"] == "rejected"

    resp2 = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    req_uuid2 = (await resp2.json())["requestUuid"]
    await web_env.db.conn.execute(
        "UPDATE web_login_requests SET expires_at=? WHERE request_uuid=?",
        (now_ts() - 1, req_uuid2),
    )
    await web_env.db.conn.commit()
    assert await web_env.server.login_request_status(req_uuid2) == "expired"
    expired = await web_env.client.post(f"/api/auth/login/consume/{req_uuid2}")
    assert expired.status == 403
    assert (await expired.json())["status"] == "expired"
    assert await web_env.server.decide_login_request(req_uuid2, approved=True, decided_by=123) == "expired"


async def test_logout_revokes_current_web_session(web_env):
    cookie1 = await _login_cookie(web_env)
    cookie2 = await _login_cookie(web_env)

    logout = await web_env.client.post(
        "/api/auth/logout",
        cookies={"openbear_web_session": cookie1},
    )
    assert logout.status == 200
    assert (await web_env.client.get("/api/auth/session", cookies={"openbear_web_session": cookie1})).status == 401
    assert (await web_env.client.get("/api/auth/session", cookies={"openbear_web_session": cookie2})).status == 200


async def _login_cookie(web_env) -> str:
    key = await web_env.server.get_secret_key()
    resp = await web_env.client.post("/api/auth/login/start", json={"secret": key})
    req_uuid = (await resp.json())["requestUuid"]
    await web_env.server.decide_login_request(req_uuid, approved=True, decided_by=123)
    approved = await web_env.client.post(f"/api/auth/login/consume/{req_uuid}")
    return approved.cookies["openbear_web_session"].value


async def test_web_write_api_rejects_cross_origin_when_header_present(web_env):
    cookie = await _login_cookie(web_env)
    resp = await web_env.client.post(
        "/api/auth/logout",
        headers={"Origin": "https://evil.example"},
        cookies={"openbear_web_session": cookie},
    )
    assert resp.status == 403
    assert (await resp.json())["error"] == "csrf_origin_rejected"


async def test_websocket_rejects_cross_origin_when_header_present(web_env):
    cookie = await _login_cookie(web_env)
    created = await web_env.client.post(
        "/api/conversations",
        json={"title": "ws csrf"},
        cookies={"openbear_web_session": cookie},
    )
    assert created.status == 200
    conv_uuid = (await created.json())["conversation"]["conversationUuid"]
    with pytest.raises(WSServerHandshakeError) as exc:
        await web_env.client.ws_connect(
            f"/api/conversations/{conv_uuid}/ws",
            headers={"Origin": "https://evil.example", "Cookie": f"openbear_web_session={cookie}"},
        )
    assert exc.value.status == 403


async def test_web_conversation_internal_chat_id_allocation_is_concurrency_safe(web_env):
    async def one(i: int):
        return await web_env.server._create_web_conversation(123, title=f"c{i}")

    rows = await asyncio.gather(*[one(i) for i in range(20)])
    internal_ids = [int(row["internal_chat_id"]) for row in rows]
    assert len(internal_ids) == len(set(internal_ids))
    assert max(internal_ids) < 0


async def test_web_task_notification_starts_internal_followup_turn(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="notify")
    live = web_env.server._live_for(row)
    calls: list[dict] = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, conversation=None, task_notification=False, task_notification_payload=None, **kwargs):
        calls.append({
            "chat_id": chat_id,
            "user_text": user_text,
            "task_notification": task_notification,
            "conversation_uuid": (conversation or {}).get("conversation_uuid"),
            "root_turn_uuid": kwargs.get("root_turn_uuid"),
            "run_uuid": getattr(renderer.live, "current_run_uuid", ""),
        })
        await renderer.finalize("已根据 Agent 结果汇总")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)

    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-123",
        "status": "completed",
        "summary": "调研 Agent 完成",
        "content": "<task-notification>调研 Agent 完成</task-notification>",
    })

    for _ in range(60):
        if calls and live.status == "idle":
            break
        await asyncio.sleep(0.02)

    assert calls
    assert calls[0]["chat_id"] == row["internal_chat_id"]
    assert calls[0]["task_notification"] is True
    assert "<task-notification>" in calls[0]["user_text"]
    frames = await _web_frames_for(web_env, row["conversation_uuid"])
    event_types = [frame["debug"].get("eventType") for frame in frames]
    assert "accepted" in event_types
    assert "task_notification" in event_types
    assert "final" in event_types
    accepted_frame = next(frame for frame in frames if frame["debug"].get("eventType") == "accepted")
    assert calls[0]["root_turn_uuid"]
    assert calls[0]["run_uuid"]
    assert calls[0]["run_uuid"] != calls[0]["root_turn_uuid"]
    assert accepted_frame["runId"] == calls[0]["run_uuid"]
    assert accepted_frame["runRootTurnId"] == calls[0]["root_turn_uuid"]
    assert accepted_frame["turnUuid"] == calls[0]["root_turn_uuid"]
    note_frame = next(frame for frame in frames if frame["debug"].get("eventType") == "task_notification")
    assert _frame_payload(note_frame)["taskUuid"] == "task-123"
    assert live.status == "idle"


async def test_terminal_web_run_cannot_be_restarted_with_same_execution_id(web_env):
    row = await web_env.server._create_web_conversation(123, title="terminal run guard")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-root", "runUuid": "run-1"})
    await live.publish({"type": "done", "turnUuid": "turn-root"})

    with pytest.raises(RuntimeError, match="terminal Web run cannot be restarted"):
        await live.publish({"type": "accepted", "turnUuid": "turn-root", "runUuid": "run-1", "taskNotification": True})

    await live.publish({"type": "accepted", "turnUuid": "turn-root", "runUuid": "run-2", "taskNotification": True})
    operations = {op["opId"]: op for op in await web_env.server._web_operations(row["conversation_uuid"])}
    assert operations["run:run-1"]["status"] == "completed"
    assert operations["run:run-1"]["lifecycle"] == "terminal"
    assert operations["run:run-2"]["status"] == "running"
    assert operations["run:run-2"]["runRootTurnId"] == "turn-root"
    assert operations["run:run-2"]["turnUuid"] == "turn-root"


async def test_web_ignores_legacy_bash_completion_notification(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="bash notify")
    calls: list[dict] = []

    async def fake_run_web_turn(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)

    await web_env.server._schedule_web_task_notification(row, {
        "kind": "tool-notification",
        "toolName": "Bash",
        "jobId": "bash_test123",
        "status": "completed",
        "summary": "Bash 后台任务已完成",
        "content": "<tool-notification>Bash done</tool-notification>",
        "resultText": "status: completed\noutput:\nok",
    })
    await asyncio.sleep(0.05)

    assert calls == []
    assert web_env.server._web_task_notification_pending.get(row["conversation_uuid"]) is None
    assert await _web_frames_for(web_env, row["conversation_uuid"], event_type="task_notification") == []


async def test_web_task_notification_outbox_recovers_pending_after_restart_boundary(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="durable notification")
    queued = await web_env.server._persist_web_task_notification(row, {
        "taskUuid": "task-durable",
        "status": "completed",
        "summary": "durable result",
        "content": "durable result body",
    })
    assert queued is not None
    notification_uuid = queued["_notificationUuid"]
    cur = await web_env.db.conn.execute(
        "SELECT state FROM web_task_notifications WHERE notification_uuid=?",
        (notification_uuid,),
    )
    assert (await cur.fetchone())["state"] == "pending"

    calls: list[dict] = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, **kwargs):
        calls.append({"chatId": chat_id, "text": user_text, **kwargs})
        await renderer.finalize("最终汇总")
        await renderer.close()
        return True

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    web_env.server._web_task_notification_pending.clear()
    web_env.server._web_task_notification_deferred.clear()
    web_env.server._web_task_notification_workers.clear()
    await web_env.server._recover_web_task_notifications(row["conversation_uuid"])

    for _ in range(100):
        cur = await web_env.db.conn.execute(
            "SELECT state FROM web_task_notifications WHERE notification_uuid=?",
            (notification_uuid,),
        )
        state = str((await cur.fetchone())["state"] or "")
        if state == "delivered":
            break
        await asyncio.sleep(0.02)

    assert state == "delivered"
    assert len(calls) == 1
    assert calls[0]["task_notification"] is True


async def test_plan_notification_outbox_requires_exact_plan_decision_ack(web_env):
    row = await web_env.server._create_web_conversation(123, title="plan outbox ack")
    chat_id = int(row["internal_chat_id"])
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-plan-outbox",
        title="plan approval",
        status="running",
    )
    coordinator = AgentPlanCoordinator(
        web_env.server.rath_dao,
        RathTaskManager(web_env.server.rath_dao),
    )
    plan = {
        "title": "Plan outbox test",
        "objective": "Verify exact durable acknowledgement",
        "scope": {"included": ["outbox"], "excluded": []},
        "assumptions": [],
        "steps": [{
            "id": "s1",
            "title": "Verify",
            "objective": "Verify the outbox",
            "method": "Inspect durable state",
            "dependsOn": [],
            "required": True,
            "criteria": [{"id": "c1", "description": "Outbox acknowledged", "required": True}],
            "expectedEvidence": ["database state"],
        }],
        "finalOutputs": [{
            "id": "o1",
            "title": "Result",
            "description": "Outbox result",
            "supportedBy": ["s1"],
        }],
        "risks": [],
    }
    submitted = await coordinator.submit_plan(
        task_uuid,
        plan,
        request_id="submit-plan-outbox",
        wait_for_decision=False,
    )
    queued = await web_env.server._persist_web_task_notification(row, {
        "kind": "plan-approval-required",
        "notificationKey": f"{row['conversation_uuid']}:{task_uuid}:1:plan-approval-required",
        "requiresDecision": True,
        "taskUuid": task_uuid,
        "status": "awaiting_plan_decision",
        "expectedPlanVersion": submitted["planVersion"],
        "summary": "Plan requires approval",
        "content": "review plan",
    })
    assert queued is not None
    _token, claimed = await web_env.server._claim_web_task_notifications([queued])
    assert claimed == {queued["_notificationUuid"]}
    assert await web_env.server._plan_notification_turn_resolved([queued]) is False

    await coordinator.decide(
        task_uuid,
        expected_version=1,
        action="approve",
        request_id="approve-plan-outbox",
    )
    assert await web_env.server._plan_notification_turn_resolved([queued]) is True
    cur = await web_env.db.conn.execute(
        "SELECT state FROM web_task_notifications WHERE notification_uuid=?",
        (queued["_notificationUuid"],),
    )
    assert (await cur.fetchone())["state"] == "delivered"


async def test_plan_notification_outbox_suppresses_terminal_task(web_env):
    row = await web_env.server._create_web_conversation(123, title="plan outbox suppress")
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=int(row["internal_chat_id"]),
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-plan-suppress",
        title="cancelled plan",
        status="running",
    )
    queued = await web_env.server._persist_web_task_notification(row, {
        "kind": "plan-approval-required",
        "notificationKey": f"{row['conversation_uuid']}:{task_uuid}:1:plan-approval-required",
        "requiresDecision": True,
        "taskUuid": task_uuid,
        "status": "awaiting_plan_decision",
        "expectedPlanVersion": 1,
        "summary": "stale plan",
    })
    assert queued is not None
    _token, claimed = await web_env.server._claim_web_task_notifications([queued])
    assert claimed
    assert await web_env.server.rath_dao.update_task(
        task_uuid,
        status="cancelled",
        finish=True,
        expected_statuses=("running",),
    )
    assert await web_env.server._plan_notification_turn_resolved([queued]) is True
    cur = await web_env.db.conn.execute(
        "SELECT state FROM web_task_notifications WHERE notification_uuid=?",
        (queued["_notificationUuid"],),
    )
    assert (await cur.fetchone())["state"] == "suppressed"


async def test_restart_recovery_suppresses_interrupted_plan_notification(web_env):
    row = await web_env.server._create_web_conversation(123, title="interrupted plan suppress")
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=int(row["internal_chat_id"]),
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-plan-interrupted",
        title="interrupted plan",
        status="running",
    )
    queued = await web_env.server._persist_web_task_notification(row, {
        "kind": "plan-approval-required",
        "notificationKey": f"{row['conversation_uuid']}:{task_uuid}:1:plan-approval-required",
        "requiresDecision": True,
        "taskUuid": task_uuid,
        "status": "awaiting_plan_decision",
        "expectedPlanVersion": 1,
        "summary": "stale after restart",
    })
    assert queued is not None
    assert await web_env.server.rath_dao.mark_interrupted_running() == 1
    web_env.server._web_task_notification_pending.clear()
    web_env.server._web_task_notification_workers.clear()

    await web_env.server._recover_web_task_notifications(row["conversation_uuid"])

    cur = await web_env.db.conn.execute(
        "SELECT state FROM web_task_notifications WHERE notification_uuid=?",
        (queued["_notificationUuid"],),
    )
    assert (await cur.fetchone())["state"] == "suppressed"
    assert web_env.server._web_task_notification_pending.get(row["conversation_uuid"]) in (None, [])


async def test_rath_terminal_trigger_survives_missing_python_notification_callback(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="atomic terminal outbox")
    chat_id = int(row["internal_chat_id"])
    await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-test",
        title="atomic task",
        status="running",
        task_uuid="task-atomic-outbox",
    )
    changed = await web_env.server.rath_dao.update_task(
        "task-atomic-outbox",
        status="completed",
        current_status="任务完成",
        output={"summary": "atomic durable result"},
        last_output_tokens=3210,
        finish=True,
        expected_statuses=("running",),
    )
    assert changed is True
    cur = await web_env.db.conn.execute(
        "SELECT notification_uuid,state,payload_json FROM web_task_notifications WHERE task_uuid='task-atomic-outbox'",
    )
    stored = await cur.fetchone()
    assert stored is not None
    assert stored["state"] == "pending"
    assert json.loads(str(stored["payload_json"] or "{}")).get("durableFallback") == 1

    calls: list[dict] = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, **kwargs):
        calls.append({"chatId": chat_id, "text": user_text, **kwargs})
        await renderer.finalize("已收到原子 outbox 结果")
        await renderer.close()
        return True

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    await web_env.server._recover_web_task_notifications(row["conversation_uuid"])
    for _ in range(100):
        cur = await web_env.db.conn.execute(
            "SELECT state FROM web_task_notifications WHERE notification_uuid=?",
            (stored["notification_uuid"],),
        )
        state = str((await cur.fetchone())["state"] or "")
        if state == "delivered":
            break
        await asyncio.sleep(0.02)
    assert state == "delivered"
    assert len(calls) == 1
    recovered_payload = calls[0]["task_notification_payload"]
    assert recovered_payload["resultOutputTokens"] == 3210
    assert recovered_payload["resultCount"] == 1
    assert recovered_payload["result"]["summary"] == "atomic durable result"
    assert "internal background Agent completion notification" in calls[0]["text"]
    assert "does not end the root task or restrict controller tools" in calls[0]["text"]
    assert "final user-facing answer only when the root objective is complete" in calls[0]["text"]


async def test_web_task_notification_recovery_reclaims_only_expired_processing_leases(web_env):
    row = await web_env.server._create_web_conversation(123, title="notification lease recovery")
    expired = await web_env.server._persist_web_task_notification(row, {
        "taskUuid": "task-expired-lease",
        "status": "completed",
        "summary": "expired",
    })
    active = await web_env.server._persist_web_task_notification(row, {
        "taskUuid": "task-active-lease",
        "status": "completed",
        "summary": "active",
    })
    assert expired is not None and active is not None
    _token, claimed = await web_env.server._claim_web_task_notifications([expired, active])
    assert len(claimed) == 2
    await web_env.db.conn.execute(
        "UPDATE web_task_notifications SET claimed_at=? WHERE notification_uuid=?",
        (now_ts() - 901, expired["_notificationUuid"]),
    )
    await web_env.db.conn.commit()

    conv_uuid = row["conversation_uuid"]
    web_env.server._web_task_notification_workers.add(conv_uuid)
    try:
        await web_env.server._recover_web_task_notifications()
    finally:
        web_env.server._web_task_notification_workers.discard(conv_uuid)
    cur = await web_env.db.conn.execute(
        "SELECT notification_uuid,state FROM web_task_notifications WHERE notification_uuid IN (?,?)",
        (expired["_notificationUuid"], active["_notificationUuid"]),
    )
    states = {str(item["notification_uuid"]): str(item["state"]) for item in await cur.fetchall()}
    assert states[expired["_notificationUuid"]] == "pending"
    assert states[active["_notificationUuid"]] == "processing"


async def test_web_task_notification_dedupes_all_outbox_rows_and_rejects_stale_terminal_status(web_env):
    row = await web_env.server._create_web_conversation(123, title="notification ordering")
    chat_id = int(row["internal_chat_id"])
    await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-test",
        title="ordered task",
        status="completed",
        task_uuid="task-ordered",
    )
    stale = await web_env.server._persist_web_task_notification(row, {
        "taskUuid": "task-ordered",
        "status": "failed",
        "summary": "stale failure",
    })
    assert stale is None

    first = await web_env.server._persist_web_task_notification(row, {
        "taskUuid": "task-ordered",
        "status": "completed",
        "summary": "first callback",
        "recentEvents": [{"seq": 10}],
    })
    second = await web_env.server._persist_web_task_notification(row, {
        "taskUuid": "task-ordered",
        "status": "completed",
        "summary": "newer callback",
        "recentEvents": [{"seq": 11}],
    })
    assert first is not None and second is not None
    cur = await web_env.db.conn.execute(
        "SELECT notification_uuid,payload_json FROM web_task_notifications WHERE task_uuid='task-ordered' ORDER BY id",
    )
    stored_rows = await cur.fetchall()
    all_notification_ids = {str(item["notification_uuid"]) for item in stored_rows}
    assert len(all_notification_ids) == 3
    assert any(json.loads(str(item["payload_json"] or "{}")).get("durableFallback") == 1 for item in stored_rows)
    deduped = web_env.server._dedupe_web_task_notifications([second, first])
    assert len(deduped) == 1
    assert deduped[0]["summary"] == "newer callback"
    assert set(deduped[0]["_notificationUuids"]) == all_notification_ids

    _token, claimed = await web_env.server._claim_web_task_notifications(deduped)
    assert claimed == set(deduped[0]["_notificationUuids"])
    await web_env.server._mark_web_task_notifications_delivered(claimed)
    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM web_task_notifications WHERE task_uuid='task-ordered' AND state='delivered'",
    )
    assert int((await cur.fetchone())["n"]) == 3


async def test_recovered_durable_agent_notification_restores_full_result_and_real_usage(web_env):
    full_result = {
        "summary": "完整恢复结论",
        "evidence": "证据" * 20_000,
    }
    recovered = web_env.server._hydrate_recovered_agent_notification(
        {
            "kind": "task-notification",
            "taskUuid": "task-recovered",
            "status": "completed",
            "summary": "Agent task completed",
            "content": json.dumps(full_result, ensure_ascii=False),
            "durableFallback": 1,
        },
        {"task_last_output_tokens": 4321},
    )

    assert recovered["result"] == full_result
    assert recovered["resultOutputTokens"] == 4321
    assert recovered["resultCount"] == 1
    assert full_result["evidence"] in recovered["content"]
    assert "internal background Agent completion notification" in recovered["content"]
    assert web_env.server._task_notification_result_budget(recovered) == (4321, 1)


def test_agent_notification_budget_estimates_full_result_only_when_real_usage_missing():
    result = {"summary": "x" * 10_000}
    tokens, count = WebAdminServer._task_notification_result_budget({
        "status": "completed",
        "result": result,
    })

    assert tokens == estimate_tokens(json.dumps(result, ensure_ascii=False))
    assert count == 1


async def test_web_task_notification_batch_dedupes_same_completion_before_token_sum(web_env):
    older = {
        "taskUuid": "task-same",
        "status": "completed",
        "summary": "older",
        "content": "old result",
        "resultOutputTokens": 2100,
        "resultCount": 1,
        "recentEvents": [{"seq": 4}],
        "_notificationUuid": "notice-old",
    }
    newer = {
        "taskUuid": "task-same",
        "status": "completed",
        "summary": "newer",
        "content": "new result",
        "resultOutputTokens": 2300,
        "resultCount": 1,
        "recentEvents": [{"seq": 5}],
        "_notificationUuid": "notice-new",
    }
    other = {
        "taskUuid": "task-other",
        "status": "completed",
        "summary": "other",
        "content": "other result",
        "resultOutputTokens": 1700,
        "resultCount": 1,
        "recentEvents": [{"seq": 2}],
        "_notificationUuid": "notice-other",
    }

    batch = web_env.server._coalesce_web_task_notifications([older, newer, other])

    assert batch["batchCount"] == 2
    assert batch["resultOutputTokens"] == 4000
    assert batch["resultCount"] == 2
    same = next(item for item in batch["results"] if item["taskUuid"] == "task-same")
    assert same["summary"] == "newer"
    assert set(same["_notificationUuids"]) == {"notice-old", "notice-new"}


async def test_agent_continuation_generation_has_new_notification_identity_and_budget(web_env):
    conversation_uuid = "conv-generation"
    first = {
        "kind": "task-notification",
        "taskUuid": "task-generation",
        "status": "completed",
        "continued": False,
        "summary": "第一代完成",
        "resultOutputTokens": 2400,
        "resultCount": 1,
        "recentEvents": [{"seq": 10}],
        "_notificationUuid": "notice-generation-1",
    }
    second = {
        "kind": "task-notification",
        "taskUuid": "task-generation",
        "status": "completed",
        "continued": True,
        "summary": "续跑完成",
        "resultOutputTokens": 3600,
        "resultCount": 1,
        "recentEvents": [{"seq": 20}],
        "_notificationUuid": "notice-generation-2",
    }

    assert web_env.server._web_task_notification_key(conversation_uuid, first) != web_env.server._web_task_notification_key(conversation_uuid, second)
    batch = web_env.server._coalesce_web_task_notifications([first, second])

    # The first generation was necessarily consumed before the controller could
    # request continuation. A pending coalesce therefore carries only the new
    # generation result while retaining both durable ids for acknowledgement.
    assert batch["summary"] == "续跑完成"
    assert batch["resultOutputTokens"] == 3600
    assert batch["resultCount"] == 1
    assert set(batch["_notificationUuids"]) == {"notice-generation-1", "notice-generation-2"}


async def test_web_task_notifications_are_batched_into_one_followup_turn(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="batch notify")
    calls: list[dict] = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, conversation=None, task_notification=False, task_notification_payload=None, **kwargs):
        calls.append({
            "chat_id": chat_id,
            "user_text": user_text,
            "task_notification": task_notification,
            "payload": task_notification_payload,
        })
        await renderer.finalize("已合并汇总多个后台任务")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)

    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-a",
        "status": "completed",
        "summary": "Agent A 完成",
        "content": "A done",
        "resultOutputTokens": 3100,
        "resultCount": 1,
    })
    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-b",
        "status": "completed",
        "summary": "Agent B 完成",
        "content": "B done",
        "resultOutputTokens": 4700,
        "resultCount": 1,
    })

    for _ in range(60):
        if calls:
            break
        await asyncio.sleep(0.02)

    assert len(calls) == 1
    assert calls[0]["task_notification"] is True
    assert calls[0]["payload"]["batched"] is True
    assert calls[0]["payload"]["batchCount"] == 2
    assert calls[0]["payload"]["resultOutputTokens"] == 7800
    assert calls[0]["payload"]["resultCount"] == 2
    assert set(calls[0]["payload"]["taskUuids"]) == {"task-a", "task-b"}
    assert "A done" in calls[0]["user_text"]
    assert "B done" in calls[0]["user_text"]
    note_frames = await _web_frames_for(web_env, row["conversation_uuid"], event_type="task_notification")
    assert len(note_frames) == 1
    assert _frame_payload(note_frames[0])["summary"] == "2 个后台任务完成"


async def test_web_task_notification_defers_partial_results_until_related_agents_terminal(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="defer partial notify")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "user", "turnUuid": "turn-root", "messageUuid": "msg-root", "text": "请多 Agent 调查"})
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-root",
        "toolCallId": "call-b-active",
        "name": "Agent",
        "payload": {"status": "running", "detached": True, "task": {"taskUuid": "task-b-active", "status": "running", "currentStatus": "B running"}},
    })
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-other-root",
        "toolCallId": "call-c-unrelated",
        "name": "Agent",
        "payload": {"status": "running", "detached": True, "task": {"taskUuid": "task-c-unrelated", "status": "running", "currentStatus": "C unrelated running"}},
    })
    await live.publish({"type": "final", "turnUuid": "turn-root", "text": "原始回答：我会等全部 Agent 完成后汇总"})
    await live.publish({"type": "done", "turnUuid": "turn-root"})
    await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-test",
        title="Agent B",
        input_data={"agentSnapshot": {"name": "agent-b"}},
        status="running",
        task_uuid="task-b-active",
    )
    await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-other",
        title="Agent C unrelated",
        input_data={"agentSnapshot": {"name": "agent-c"}},
        status="running",
        task_uuid="task-c-unrelated",
    )
    calls: list[dict] = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, conversation=None, task_notification=False, task_notification_payload=None, **kwargs):
        await renderer.finalize("已合并最终结果")
        await renderer.close()
        calls.append({
            "chat_id": chat_id,
            "user_text": user_text,
            "task_notification": task_notification,
            "payload": task_notification_payload,
        })

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)

    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-a-done",
        "status": "completed",
        "summary": "Agent A 完成",
        "content": "A done evidence",
    })
    for _ in range(60):
        if web_env.server._web_task_notification_deferred.get(row["conversation_uuid"]):
            break
        await asyncio.sleep(0.02)
    assert calls == []
    assert len(web_env.server._web_task_notification_deferred[row["conversation_uuid"]]) == 1
    hidden_frames = await _web_frames_for(web_env, row["conversation_uuid"], event_type="task_notification")
    hidden_notes = [_frame_payload(frame) for frame in hidden_frames if _frame_payload(frame).get("deferredUntilRelatedTasksTerminal")]
    assert hidden_notes
    assert hidden_notes[-1]["remainingTaskUuids"] == ["task-b-active"]

    await web_env.server.rath_dao.update_task("task-b-active", status="completed", finish=True)
    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-b-active",
        "status": "completed",
        "summary": "Agent B 完成",
        "content": "B done evidence",
    })
    for _ in range(60):
        if calls:
            break
        await asyncio.sleep(0.02)

    assert len(calls) == 1
    assert calls[0]["task_notification"] is True
    assert calls[0]["payload"]["batched"] is True
    assert calls[0]["payload"]["batchCount"] == 2
    assert set(calls[0]["payload"]["taskUuids"]) == {"task-a-done", "task-b-active"}
    assert "A done evidence" in calls[0]["user_text"]
    assert "B done evidence" in calls[0]["user_text"]
    assert row["conversation_uuid"] not in web_env.server._web_task_notification_deferred
    ops = await web_env.server._web_operations(row["conversation_uuid"])
    original_ops = [op for op in ops if op.get("opId") == "assistant:turn-root:0"]
    assert original_ops and (original_ops[0].get("payload") or {}).get("text") == "原始回答：我会等全部 Agent 完成后汇总"
    final_ops = [op for op in ops if op.get("opId") == "assistant:turn-root:1"]
    assert final_ops and (final_ops[0].get("payload") or {}).get("text") == "已合并最终结果"


async def test_web_task_notification_budget_wait_does_not_finish_batch(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="budget wait notify")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "user", "turnUuid": "turn-root", "messageUuid": "msg-root", "text": "启动三个 Agent"})
    for task_uuid, call_id in [("task-a", "call-a"), ("task-b", "call-b"), ("task-c", "call-c")]:
        await live.publish({
            "type": "tool_progress",
            "turnUuid": "turn-root",
            "toolCallId": call_id,
            "name": "Agent",
            "payload": {"status": "running", "detached": True, "task": {"taskUuid": task_uuid, "status": "running", "currentStatus": "running"}},
        })
        await web_env.server.rath_dao.create_task(
            chat_id=chat_id,
            parent_session_uuid=row["conversation_uuid"],
            workflow_uuid="wf-test",
            title=task_uuid,
            input_data={"agentSnapshot": {"name": task_uuid}},
            status="running",
            task_uuid=task_uuid,
        )
    await live.publish({"type": "final", "turnUuid": "turn-root", "text": "等三个 Agent 完成后汇总"})
    await live.publish({"type": "done", "turnUuid": "turn-root"})
    calls: list[dict] = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, conversation=None, task_notification=False, task_notification_payload=None, **kwargs):
        calls.append({
            "chat_id": chat_id,
            "user_text": user_text,
            "task_notification": task_notification,
            "payload": task_notification_payload,
        })
        await renderer.finalize("我会让等待裁决的 Agent 继续")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)

    for task_uuid, content in [("task-a", "A evidence"), ("task-b", "B evidence")]:
        await web_env.server.rath_dao.update_task(task_uuid, status="completed", finish=True)
        await web_env.server._schedule_web_task_notification(row, {
            "taskUuid": task_uuid,
            "status": "completed",
            "summary": f"{task_uuid} 完成",
            "content": content,
        })
    for _ in range(60):
        if web_env.server._web_task_notification_deferred.get(row["conversation_uuid"]):
            break
        await asyncio.sleep(0.02)
    assert calls == []
    assert {item["taskUuid"] for item in web_env.server._web_task_notification_deferred[row["conversation_uuid"]]} == {"task-a", "task-b"}

    await web_env.server.rath_dao.update_task(
        "task-c",
        status="needs_openbear_control",
        current_status="预算达到上限，等待 OpenBear 裁决",
        finish=True,
    )
    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-c",
        "status": "needs_openbear_control",
        "summary": "task-c 等待 OpenBear 裁决",
        "content": "C budget exhausted",
    })
    for _ in range(60):
        if calls:
            break
        await asyncio.sleep(0.02)

    assert len(calls) == 1
    assert calls[0]["task_notification"] is True
    assert calls[0]["payload"]["status"] == "needs_openbear_control"
    assert "3 个后台任务完成" not in str(calls[0]["payload"].get("summary") or "")
    assert {item["taskUuid"] for item in web_env.server._web_task_notification_deferred[row["conversation_uuid"]]} == {"task-a", "task-b"}
    ops = await web_env.server._web_operations(row["conversation_uuid"])
    agent_c = [op for op in ops if op.get("opId") == "agent:task-c"][-1]
    assert agent_c["lifecycle"] == "waiting_control"

    await web_env.server.rath_dao.update_task("task-c", status="completed", finish=True)
    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-c",
        "status": "completed",
        "summary": "task-c 完成",
        "content": "C evidence",
    })
    for _ in range(60):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.02)

    assert len(calls) == 2
    final_payload = calls[-1]["payload"]
    assert final_payload["batched"] is True
    assert final_payload["batchCount"] == 3
    assert final_payload["summary"] == "3 个后台任务完成"
    assert set(final_payload["taskUuids"]) == {"task-a", "task-b", "task-c"}
    assert "A evidence" in calls[-1]["user_text"]
    assert "B evidence" in calls[-1]["user_text"]
    assert "C evidence" in calls[-1]["user_text"]


async def test_web_task_notification_control_wait_wakes_controller_despite_running_sibling(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="control wait wakes controller")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "user", "turnUuid": "turn-root", "messageUuid": "msg-root", "text": "启动两个 Agent"})
    for task_uuid in ("task-waiting", "task-running"):
        await live.publish({
            "type": "tool_progress",
            "turnUuid": "turn-root",
            "toolCallId": f"call-{task_uuid}",
            "name": "Agent",
            "payload": {"status": "running", "detached": True, "task": {"taskUuid": task_uuid, "status": "running"}},
        })
        await web_env.server.rath_dao.create_task(
            chat_id=chat_id,
            parent_session_uuid=row["conversation_uuid"],
            workflow_uuid="wf-test",
            title=task_uuid,
            input_data={"agentSnapshot": {"name": task_uuid}},
            status="running",
            task_uuid=task_uuid,
        )
    await live.publish({"type": "done", "turnUuid": "turn-root"})
    await web_env.server.rath_dao.update_task("task-waiting", status="needs_openbear_control", finish=True)
    calls = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, task_notification=False, task_notification_payload=None, **kwargs):
        calls.append({"text": user_text, "task_notification": task_notification, "payload": task_notification_payload})
        await renderer.finalize("已立即裁决等待中的 Agent")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-waiting",
        "status": "needs_openbear_control",
        "summary": "等待 OpenBear 裁决",
        "content": "budget boundary",
    })
    for _ in range(80):
        if calls:
            break
        await asyncio.sleep(0.02)

    assert len(calls) == 1
    assert calls[0]["task_notification"] is True
    assert calls[0]["payload"]["status"] == "needs_openbear_control"
    assert calls[0]["payload"]["taskUuid"] == "task-waiting"
    assert "task-running" not in [item.get("taskUuid") for item in web_env.server._web_task_notification_deferred.get(row["conversation_uuid"], [])]


async def test_web_task_notification_two_control_waits_are_batched_without_deadlock(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="two control waits")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "user", "turnUuid": "turn-root", "messageUuid": "msg-root", "text": "启动两个 Agent"})
    for task_uuid in ("task-wait-a", "task-wait-b"):
        await live.publish({
            "type": "tool_progress", "turnUuid": "turn-root", "toolCallId": f"call-{task_uuid}", "name": "Agent",
            "payload": {"status": "running", "detached": True, "task": {"taskUuid": task_uuid, "status": "running"}},
        })
        await web_env.server.rath_dao.create_task(
            chat_id=chat_id, parent_session_uuid=row["conversation_uuid"], workflow_uuid="wf-test",
            title=task_uuid, input_data={"agentSnapshot": {"name": task_uuid}}, status="needs_openbear_control", task_uuid=task_uuid,
        )
    await live.publish({"type": "done", "turnUuid": "turn-root"})
    calls = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, task_notification=False, task_notification_payload=None, **kwargs):
        calls.append(task_notification_payload)
        await renderer.finalize("已批量裁决")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    await asyncio.gather(
        web_env.server._schedule_web_task_notification(row, {"taskUuid": "task-wait-a", "status": "needs_openbear_control", "summary": "A waits", "content": "A"}),
        web_env.server._schedule_web_task_notification(row, {"taskUuid": "task-wait-b", "status": "needs_openbear_control", "summary": "B waits", "content": "B"}),
    )
    for _ in range(80):
        if calls:
            break
        await asyncio.sleep(0.02)

    assert len(calls) == 1
    assert calls[0]["status"] == "needs_openbear_control"
    assert calls[0]["batched"] is True
    assert set(calls[0]["taskUuids"]) == {"task-wait-a", "task-wait-b"}


async def test_active_controller_generation_hands_late_control_to_post_turn_worker(web_env, monkeypatch):
    cfg = _cfg()
    generation_entered = asyncio.Event()
    release_generation = asyncio.Event()

    class BlockingGenerationBackend(FakeStreamBackend):
        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            if self.calls == 1:
                generation_entered.set()
                await release_generation.wait()
            async for event in super().stream(
                messages, model=model, system=system, tools=tools, max_tokens=max_tokens, **opts,
            ):
                yield event

    backend = BlockingGenerationBackend([
        [
            StreamEvent(kind="tool_call", tool_calls=[
                ToolCall(id="capture-notification", name="CaptureTaskNotification", arguments="{}"),
            ]),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [StreamEvent(kind="content", text="A 已处理。"), StreamEvent(kind="finish", finish_reason="stop")],
        [StreamEvent(kind="content", text="已查看 B。"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.runs = RunRegistry()
    callbacks = []
    tools = ToolRegistry()

    async def _capture_notification(_args):
        callback = current_tool_context().task_notification
        assert callback is not None
        callbacks.append(callback)
        changed = await web_env.server.rath_dao.update_task(
            "task-active-a", status="resuming", control_state="continuation_claimed",
            current_status="A resumed", expected_statuses=("needs_openbear_control",),
        )
        assert changed is True
        return json.dumps({"ok": True}, ensure_ascii=False)

    tools.add(
        "CaptureTaskNotification", "capture the active task notification callback",
        {"type": "object", "properties": {}}, _capture_notification,
    )
    web_env.server.tools = tools

    async def _fake_system_prompt():
        return "sys"

    recover_calls = []

    async def _fake_recover(conversation_uuid="", **_kwargs):
        recover_calls.append(conversation_uuid)

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    monkeypatch.setattr(web_env.server, "_recover_web_task_notifications", _fake_recover)
    row = await web_env.server._create_web_conversation(123, title="late active notification", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    await web_env.server.rath_dao.create_task(
        chat_id=chat_id, parent_session_uuid=row["conversation_uuid"], workflow_uuid="wf-late-active",
        title="A waiting control", status="needs_openbear_control", task_uuid="task-active-a",
    )
    await web_env.server.rath_dao.create_task(
        chat_id=chat_id, parent_session_uuid=row["conversation_uuid"], workflow_uuid="wf-late-active",
        title="B waiting control", status="needs_openbear_control", task_uuid="task-active-b",
    )

    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-active-a", "status": "needs_openbear_control", "summary": "A needs a ruling",
        "content": "A control boundary", "recentEvents": [{"seq": 1}],
    })
    await asyncio.wait_for(generation_entered.wait(), timeout=3)
    assert len(callbacks) == 1
    try:
        await callbacks[0]({
            "kind": "task-notification",
            "taskUuid": "task-active-b", "status": "needs_openbear_control",
            "summary": "B needs a ruling", "content": "B control boundary",
            "recentEvents": [{"seq": 2}],
        })
    finally:
        release_generation.set()

    states_by_task = {}
    for _ in range(250):
        cur = await web_env.db.conn.execute(
            "SELECT task_uuid,state FROM web_task_notifications WHERE task_uuid IN ('task-active-a','task-active-b') ORDER BY id",
        )
        states_by_task = {}
        for item in await cur.fetchall():
            states_by_task.setdefault(str(item["task_uuid"]), []).append(str(item["state"]))
        if (
            backend.calls >= 3
            and row["conversation_uuid"] not in web_env.server._web_task_notification_workers
            and states_by_task.get("task-active-a") and states_by_task.get("task-active-b")
        ):
            break
        await asyncio.sleep(0.02)

    assert backend.calls == 3
    assert set(states_by_task["task-active-a"]) == {"delivered"}
    assert set(states_by_task["task-active-b"]) == {"pending"}
    assert recover_calls == []


async def test_control_notification_acknowledges_only_tasks_resolved_by_model_turn(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="partial control resolution")
    chat_id = int(row["internal_chat_id"])
    for task_uuid in ("task-resolved-a", "task-unresolved-b"):
        await web_env.server.rath_dao.create_task(
            chat_id=chat_id, parent_session_uuid=row["conversation_uuid"], workflow_uuid="wf-resolution-aware",
            title=task_uuid, status="needs_openbear_control", task_uuid=task_uuid,
        )
    calls = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, task_notification_payload=None, **kwargs):
        calls.append(task_notification_payload)
        changed = await web_env.server.rath_dao.update_task(
            "task-resolved-a", status="resuming", control_state="continuation_claimed",
            current_status="A resumed", expected_statuses=("needs_openbear_control",),
        )
        assert changed is True
        await renderer.finalize("仅处理了 A")
        await renderer.close()
        return True

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    await asyncio.gather(
        web_env.server._schedule_web_task_notification(row, {
            "taskUuid": "task-resolved-a", "status": "needs_openbear_control",
            "summary": "A waits", "content": "A boundary", "recentEvents": [{"seq": 10}],
        }),
        web_env.server._schedule_web_task_notification(row, {
            "taskUuid": "task-unresolved-b", "status": "needs_openbear_control",
            "summary": "B waits", "content": "B boundary", "recentEvents": [{"seq": 20}],
        }),
    )

    for _ in range(200):
        if calls and row["conversation_uuid"] not in web_env.server._web_task_notification_workers:
            break
        await asyncio.sleep(0.02)
    cur = await web_env.db.conn.execute(
        "SELECT task_uuid,state,attempts FROM web_task_notifications WHERE task_uuid IN ('task-resolved-a','task-unresolved-b') ORDER BY id",
    )
    rows = await cur.fetchall()
    states_by_task = {}
    attempts_by_task = {}
    for item in rows:
        task_uuid = str(item["task_uuid"])
        states_by_task.setdefault(task_uuid, []).append(str(item["state"]))
        attempts_by_task.setdefault(task_uuid, []).append(int(item["attempts"] or 0))

    assert len(calls) == 1
    assert set(states_by_task["task-resolved-a"]) == {"delivered"}
    assert set(states_by_task["task-unresolved-b"]) == {"pending"}
    assert min(attempts_by_task["task-unresolved-b"]) == 1


async def test_task_notification_summary_run_keeps_conversation_running(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="summary running")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, conversation=None, task_notification=False, task_notification_payload=None, **kwargs):
        await renderer.emit({"type": "status", "status": "正在基于 Agent 结果汇总"})
        entered.set()
        await release.wait()
        await renderer.finalize("最终汇总完成")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)

    await web_env.server._schedule_web_task_notification(row, {
        "taskUuid": "task-summary",
        "status": "completed",
        "summary": "Agent 完成",
        "content": "Agent evidence",
    })
    await asyncio.wait_for(entered.wait(), timeout=3)
    state = await web_env.server._chat_payload(int(row["internal_chat_id"]), row)
    assert state["running"] is True
    assert state["conversation"]["running"] is True
    assert state["conversation"]["currentStatus"] in {"正在基于 Agent 结果汇总", "已发送", "Agent 结果已回传", "Agent 结果汇总中"}
    run_ops = [op for op in state["operations"] if op.get("opType") == "run" and op.get("turnUuid")]
    assert run_ops and run_ops[-1]["lifecycle"] == "active"

    release.set()
    for _ in range(60):
        state = await web_env.server._chat_payload(int(row["internal_chat_id"]), row)
        if not state["running"]:
            break
        await asyncio.sleep(0.02)
    assert state["running"] is False


async def test_web_confirm_payload_supports_select_and_prompt_metadata(web_env):
    row = await web_env.server._create_web_conversation(123, title="interactions")
    conv_uuid = row["conversation_uuid"]

    select_task = asyncio.create_task(web_env.server._web_confirm(conv_uuid, {
        "action": "select",
        "title": "选择方向",
        "body": "选一个",
        "options": [{"label": "修复", "value": "fix"}, {"label": "跳过", "value": "skip"}],
        "multiple": False,
        "defaultValues": ["skip"],
    }))
    await asyncio.sleep(0)
    pending = web_env.server._pending_web_confirmations(conv_uuid)
    assert pending[0]["action"] == "select"
    assert pending[0]["options"][0]["label"] == "修复"
    assert pending[0]["defaultValues"] == ["skip"]
    cid = pending[0]["confirmationId"]
    web_env.server._web_confirmations[cid]["future"].set_result({
        "status": "answered",
        "cancelled": False,
        "selectedIndexes": [1],
        "selectedValues": ["skip"],
        "selectedLabels": ["跳过"],
    })
    assert (await select_task)["selectedValues"] == ["skip"]

    prompt_task = asyncio.create_task(web_env.server._web_confirm(conv_uuid, {
        "action": "prompt",
        "title": "输入说明",
        "body": "填一下",
        "defaultValue": "默认",
        "sensitive": True,
    }))
    await asyncio.sleep(0)
    pending = web_env.server._pending_web_confirmations(conv_uuid)
    assert pending[0]["action"] == "prompt"
    assert pending[0]["defaultValue"] == "默认"
    assert pending[0]["sensitive"] is True
    cid = pending[0]["confirmationId"]
    web_env.server._web_confirmations[cid]["future"].set_result({
        "status": "answered",
        "cancelled": False,
        "value": "用户输入",
    })
    assert (await prompt_task)["value"] == "用户输入"


async def test_web_conversation_json_uses_rath_background_fact_without_operation_active(web_env):
    data = web_env.server._web_conversation_json(
        {
            "conversation_uuid": "c-bg",
            "owner_chat_id": 123,
            "internal_chat_id": -1,
            "title": "bg",
            "status": "idle",
            "current_status": "就绪",
            "created_at": 1,
            "updated_at": 1,
        },
        operation_facts={"activeRathTaskCount": 1, "activeRathTaskUuids": ["task-1"]},
    )

    assert data["running"] is True
    assert data["status"] == "running"
    assert data["currentStatus"] == "Agent 后台执行中"


async def test_web_transcript_binding_is_bidirectional_and_hydrates_deferred_operation(web_env):
    row = await web_env.server._create_web_conversation(123, title="transcript binding")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    messages = MessageDAO(web_env.db)
    await live.publish({"type": "accepted", "turnUuid": "turn-bind", "runUuid": "turn-bind"})

    message_id = await web_env.server._persist_web_transcript_message(
        messages,
        chat_id,
        "assistant",
        "延迟 operation 绑定",
        conversation_uuid=conv_uuid,
        turn_uuid="turn-bind",
        run_root_turn_uuid="turn-bind",
        op_ids=["assistant:turn-bind:0"],
        tokens=5,
    )
    assert await web_env.server._web_message_operation_ids(conv_uuid, message_id) == [
        "assistant:turn-bind:0",
        "run:turn-bind",
    ]

    renderer = _WebStreamRenderer(live)
    await renderer.finalize("延迟 operation 绑定")
    await renderer.close()
    operations = await web_env.server._web_operations(conv_uuid)
    assistant = next(op for op in operations if op["opId"] == "assistant:turn-bind:0")
    run = next(op for op in operations if op["opId"] == "run:turn-bind")
    assert assistant["transcriptMessageIds"] == [message_id]
    assert run["transcriptMessageIds"] == [message_id]


async def test_web_transcript_binding_rolls_back_message_when_link_write_fails(web_env, monkeypatch):
    row = await web_env.server._create_web_conversation(123, title="transcript transaction")
    messages = MessageDAO(web_env.db)

    async def fail_link(*args, **kwargs):
        raise RuntimeError("link failed")

    monkeypatch.setattr(web_env.server, "_attach_transcript_message_ids_tx", fail_link)
    with pytest.raises(RuntimeError, match="link failed"):
        await web_env.server._persist_web_transcript_message(
            messages,
            int(row["internal_chat_id"]),
            "user",
            "必须整体回滚",
            conversation_uuid=str(row["conversation_uuid"]),
            turn_uuid="turn-rollback",
            op_ids=["msg:rollback"],
        )
    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS count FROM messages WHERE chat_id=?",
        (int(row["internal_chat_id"]),),
    )
    assert int((await cur.fetchone())["count"] or 0) == 0


async def test_web_merged_steering_message_links_every_source_user_operation(web_env):
    row = await web_env.server._create_web_conversation(123, title="merged steering binding")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-steer", "runUuid": "turn-steer"})
    await live.publish({"type": "user", "turnUuid": "turn-steer", "messageUuid": "steer-1", "text": "第一条"})
    await live.publish({"type": "user", "turnUuid": "turn-steer", "messageUuid": "steer-2", "text": "第二条"})

    message_id = await web_env.server._persist_web_transcript_message(
        MessageDAO(web_env.db),
        chat_id,
        "user",
        "第一条\n\n第二条",
        conversation_uuid=conv_uuid,
        turn_uuid="turn-steer",
        run_root_turn_uuid="turn-steer",
        op_ids=["msg:steer-1", "msg:steer-2"],
    )
    operations = await web_env.server._web_operations(conv_uuid)
    for op_id in ("msg:steer-1", "msg:steer-2", "run:turn-steer"):
        op = next(item for item in operations if item["opId"] == op_id)
        assert op["transcriptMessageIds"] == [message_id]
    assert await web_env.server._web_message_operation_ids(conv_uuid, message_id) == [
        "msg:steer-1",
        "msg:steer-2",
        "run:turn-steer",
    ]



async def test_web_image_upload_renders_as_attachment_not_path_text(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="image upload")
    live = web_env.server._live_for(row)
    media = [InboundMedia(kind="image", upload_type="websocket_upload", file_name="image.png", mime_type="image/png", size=1234, path="/opt/src-space/openbear/data/media/inbound/web/test/image.png")]
    calls: list[dict] = []

    async def fake_register(path, *, conversation, turn_uuid="", op_id="", source_url=""):
        return {
            "artifactUuid": "art-img",
            "contentUrl": "/api/conversations/c/artifacts/art-img/content",
            "previewUrl": "/api/conversations/c/artifacts/art-img/content?preview=1",
            "downloadUrl": "/api/conversations/c/artifacts/art-img/content?download=1",
            "inlinePreview": True,
        }

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, conversation=None, **kwargs):
        calls.append({"user_text": user_text, "media": media})
        await renderer.finalize("ok")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_register_web_artifact_from_path", fake_register)
    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)

    result = await web_env.server._start_or_steer_web_conversation(row, "看图", media, live)
    assert result["ok"] is True
    task = web_env.server.runs.task(int(row["internal_chat_id"]))
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    assert calls and calls[0]["user_text"] == "看图"
    ops = await web_env.server._web_operations(row["conversation_uuid"])
    user_ops = [op for op in ops if op.get("opType") == "user_message"]
    assert user_ops
    payload = user_ops[-1]["payload"]
    assert payload["text"] == "看图"
    assert "[用户附件]" not in payload["text"]
    assert payload["attachments"][0]["kind"] == "image"
    assert payload["attachments"][0]["previewUrl"].endswith("preview=1")


async def test_web_send_serializes_concurrent_starts_into_steering(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="send race")
    live = web_env.server._live_for(row)
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, conversation=None, **kwargs):
        calls.append(user_text)
        await release.wait()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)

    results = await asyncio.gather(
        web_env.server._start_or_steer_web_conversation(row, "第一条", [], live),
        web_env.server._start_or_steer_web_conversation(row, "第二条", [], live),
    )

    assert sum(1 for item in results if item.get("queued")) == 1
    assert sum(1 for item in results if item.get("ok") and not item.get("queued")) == 1
    assert len(calls) == 1
    task = web_env.server.runs.task(int(row["internal_chat_id"]))
    assert task is not None
    release.set()
    await asyncio.wait_for(task, timeout=1.0)


async def test_web_list_does_not_reconcile_run_during_startup_registration_window(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="startup reconcile race")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    accepted_persisted = asyncio.Event()
    release_accepted = asyncio.Event()
    release_run = asyncio.Event()
    original_sink = live._event_sink

    async def blocking_sink(event):
        persisted = await original_sink(event)
        if event.get("type") == "accepted":
            accepted_persisted.set()
            await release_accepted.wait()
        return persisted

    async def fake_run_web_turn(chat_id_arg, user_text, renderer, media=None, *, conversation=None, **kwargs):
        assert chat_id_arg == chat_id
        await release_run.wait()
        await renderer.finalize("完成")
        await renderer.close()

    async def no_active_round(*args, **kwargs):
        return {"active": False, "activeReasons": []}

    live._event_sink = blocking_sink
    monkeypatch.setattr(web_env.server, "_web_active_round_info", no_active_round)
    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    send_task = asyncio.create_task(web_env.server._start_or_steer_web_conversation(row, "开始", [], live))
    controller_task = None
    try:
        await asyncio.wait_for(accepted_persisted.wait(), timeout=1.0)
        assert web_env.server._web_starting_turns.get(conv_uuid)
        assert live.snapshot()["running"] is False
        assert web_env.server.runs.is_running(chat_id) is False

        listed = await web_env.server._list_web_conversations(123)

        item = next(item for item in listed if item["conversationUuid"] == conv_uuid)
        assert item["running"] is True
        operations = await web_env.server._web_operations(conv_uuid)
        run_op = next(op for op in operations if op["opType"] == "run")
        assert run_op["status"] == "running"
        assert run_op["lifecycle"] == "active"

        release_accepted.set()
        result = await asyncio.wait_for(send_task, timeout=1.0)
        assert result == {"ok": True, "queued": False}
        assert conv_uuid not in web_env.server._web_starting_turns
        controller_task = web_env.server.runs.task(chat_id)
        assert controller_task is not None
    finally:
        release_accepted.set()
        release_run.set()
        if not send_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(send_task, timeout=1.0)
        if controller_task is None:
            controller_task = web_env.server.runs.task(chat_id)
        if controller_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(controller_task, timeout=1.0)


async def test_background_agent_interruption_queues_to_same_root_controller_without_steering_agent(web_env, monkeypatch):
    steering.clear(-1)
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="background interrupt")
    chat_id = int(row["internal_chat_id"])
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-test",
        title="agent task",
        input_data={"agentSnapshot": {"name": "general-purpose"}},
        status="running",
        task_uuid="task-bg-main",
    )
    await web_env.server.rath_dao.update_task(task_uuid, current_agent_key="general-purpose", current_status="执行中")
    sleeper = asyncio.create_task(asyncio.sleep(30))
    web_env.server.rath.register(task_uuid, chat_id, sleeper)
    calls = []

    async def fake_run_web_turn(chat_id_arg, user_text, renderer, media=None, *, conversation=None, background_control_payload=None, **kwargs):
        calls.append({
            "chat_id": chat_id_arg,
            "user_text": user_text,
            "background_control_payload": background_control_payload,
            "kwargs": kwargs,
        })
        await renderer.finalize("主会话已收到，我会判断是否需要转告后台 Agent。")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    try:
        live = web_env.server._live_for(row)
        result = await web_env.server._start_or_steer_web_conversation(row, "补充一下：后面只保留最后三行输出", [], live)

        assert result["ok"] is True
        assert result["queued"] is True
        assert result["rootTurnUuid"]
        assert [item["text"] for item in steering.pending_items(chat_id)] == ["补充一下：后面只保留最后三行输出"]
        assert calls == []
        # The main controller decides whether AgentMessage is appropriate; Web
        # input itself must never be routed directly to the child Agent.
        assert await web_env.server.rath_dao.pending_controls(task_uuid) == []
        state = await web_env.server._chat_payload(chat_id, row)
        user_ops = [op for op in state["operations"] if op["opType"] == "user_message"]
        interruption_ops = [op for op in user_ops if (op.get("payload") or {}).get("interruption")]
        assert len(interruption_ops) == 1
        assert (interruption_ops[0].get("payload") or {}).get("queued") is True
        assert [op for op in state["operations"] if op["opType"] == "agent_control"] == []
    finally:
        sleeper.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await sleeper


async def test_background_agent_interruption_with_waiting_control_queues_to_same_root(web_env, monkeypatch):
    steering.clear(-1)
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="background continue")
    chat_id = int(row["internal_chat_id"])
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-test",
        title="waiting agent task",
        input_data={"agentSnapshot": {"name": "general-purpose"}},
        status="needs_openbear_control",
        task_uuid="task-bg-waiting-control",
    )
    await web_env.server.rath_dao.update_task(task_uuid, current_agent_key="general-purpose", current_status="等待 OpenBear 裁决")
    calls = []

    async def fake_run_web_turn(chat_id_arg, user_text, renderer, media=None, *, conversation=None, background_control_payload=None, **kwargs):
        calls.append({"chat_id": chat_id_arg, "user_text": user_text, "background": background_control_payload})
        await renderer.finalize("主会话已收到并会自行裁决。")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    live = web_env.server._live_for(row)
    result = await web_env.server._start_or_steer_web_conversation(row, "继续，但只看最后三行", [], live)

    assert result["ok"] is True
    assert result["queued"] is True
    assert [item["text"] for item in steering.pending_items(chat_id)] == ["继续，但只看最后三行"]
    assert calls == []
    assert await web_env.server.rath_dao.pending_controls(task_uuid) == []


async def test_background_agent_interruption_with_multiple_tasks_queues_to_same_root(web_env, monkeypatch):
    steering.clear(-1)
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="multi background interrupt")
    chat_id = int(row["internal_chat_id"])
    task_uuids = []
    sleepers = []
    for idx in range(2):
        task_uuid = await web_env.server.rath_dao.create_task(
            chat_id=chat_id,
            parent_session_uuid=row["conversation_uuid"],
            workflow_uuid="wf-test",
            title=f"agent task {idx}",
            input_data={"agentSnapshot": {"name": f"agent-{idx}"}},
            status="running",
            task_uuid=f"task-bg-main-{idx}",
        )
        await web_env.server.rath_dao.update_task(task_uuid, current_agent_key=f"agent-{idx}", current_status="执行中")
        sleeper = asyncio.create_task(asyncio.sleep(30))
        web_env.server.rath.register(task_uuid, chat_id, sleeper)
        task_uuids.append(task_uuid)
        sleepers.append(sleeper)
    calls = []

    async def fake_run_web_turn(chat_id_arg, user_text, renderer, media=None, *, conversation=None, background_control_payload=None, **kwargs):
        calls.append({"text": user_text, "background": background_control_payload})
        await renderer.finalize("主会话会判断如何处理多个后台 Agent。")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    try:
        live = web_env.server._live_for(row)
        result = await web_env.server._start_or_steer_web_conversation(row, "让它忽略 amazon", [], live)
        assert result["ok"] is True
        assert result["queued"] is True
        assert [item["text"] for item in steering.pending_items(chat_id)] == ["让它忽略 amazon"]
        assert calls == []
        for task_uuid in task_uuids:
            assert await web_env.server.rath_dao.pending_controls(task_uuid) == []
    finally:
        for sleeper in sleepers:
            sleeper.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await sleeper


async def test_background_agent_status_request_queues_to_same_root_controller(web_env, monkeypatch):
    steering.clear(-1)
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="background status")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "user", "turnUuid": "turn-root", "messageUuid": "msg-root", "text": "请调查"})
    await live.publish({"type": "final", "turnUuid": "turn-root", "text": "原始回答：我会在后台继续查"})
    await live.publish({"type": "done", "turnUuid": "turn-root"})
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-test",
        title="agent status task",
        input_data={"agentSnapshot": {"name": "general-purpose"}},
        status="running",
        task_uuid="task-status-active",
    )
    await web_env.server.rath_dao.update_task(task_uuid, current_agent_key="general-purpose", current_status="正在查代码")
    calls = []

    async def fake_run_web_turn(chat_id_arg, user_text, renderer, media=None, *, conversation=None, background_control_payload=None, **kwargs):
        calls.append({"text": user_text, "background": background_control_payload})
        await renderer.finalize("主会话已读取并解释当前进度。")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    result = await web_env.server._start_or_steer_web_conversation(row, "看下现在进度", [], live)

    assert result["ok"] is True
    assert result["queued"] is True
    assert [item["text"] for item in steering.pending_items(chat_id)] == ["看下现在进度"]
    assert calls == []
    assert await web_env.server.rath_dao.pending_controls(task_uuid) == []
    ops = await web_env.server._web_operations(row["conversation_uuid"])
    assert [op for op in ops if op.get("opType") == "agent_control"] == []


async def test_active_background_process_send_stays_on_backend_root_turn(web_env, monkeypatch):
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="process active round")
    chat_id = int(row["internal_chat_id"])
    steering.clear(chat_id)
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "user", "turnUuid": "turn-root", "messageUuid": "msg-root", "text": "跑后台命令"})
    await live.publish({"type": "done", "turnUuid": "turn-root"})
    monkeypatch.setattr(
        "app.web_console.conversations.processes.active_for_chat",
        lambda cid: [SimpleNamespace(chat_id=cid, session_uuid=row["conversation_uuid"], turn_uuid="turn-root", run_root_turn_uuid="turn-root")],
    )
    try:
        result = await web_env.server._start_or_steer_web_conversation(row, "补充：完成后只汇报最后三行", [], live)
        assert result["ok"] is True
        assert result["queued"] is True
        assert result["rootTurnUuid"] == "turn-root"
        assert [item["text"] for item in steering.pending_items(chat_id)] == ["补充：完成后只汇报最后三行"]
        ops = await web_env.server._web_operations(row["conversation_uuid"])
        user_ops = [op for op in ops if op.get("opType") == "user_message" and (op.get("payload") or {}).get("text") == "补充：完成后只汇报最后三行"]
        assert len(user_ops) == 1
        assert user_ops[0]["turnUuid"] == "turn-root"
        assert user_ops[0]["runRootTurnId"] == "turn-root"
        assert (user_ops[0].get("payload") or {}).get("queued") is True
    finally:
        steering.clear(chat_id)


async def test_needs_openbear_control_agent_send_queues_to_same_root_controller(web_env, monkeypatch):
    steering.clear(-1)
    web_env.server.runs = RunRegistry()
    row = await web_env.server._create_web_conversation(123, title="needs control active round")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "user", "turnUuid": "turn-root", "messageUuid": "msg-root", "text": "请调查"})
    await live.publish({"type": "done", "turnUuid": "turn-root"})
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-test",
        title="agent waits control",
        input_data={"agentSnapshot": {"name": "general-purpose"}},
        status="needs_openbear_control",
        task_uuid="task-needs-control",
    )
    calls = []

    async def fake_run_web_turn(chat_id_arg, user_text, renderer, media=None, *, conversation=None, background_control_payload=None, **kwargs):
        calls.append({"text": user_text, "background": background_control_payload})
        await renderer.finalize("主会话现在可以执行 AgentMessage 裁决。")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    result = await web_env.server._start_or_steer_web_conversation(row, "继续，但只看后端路径", [], live)
    assert result["ok"] is True
    assert result["queued"] is True
    assert [item["text"] for item in steering.pending_items(chat_id)] == ["继续，但只看后端路径"]
    assert calls == []
    assert await web_env.server.rath_dao.pending_controls(task_uuid) == []


async def test_web_operation_target_columns_are_persisted(web_env):
    row = await web_env.server._create_web_conversation(123, title="target columns")
    live = web_env.server._live_for(row)
    await live.publish({
        "type": "agent_control",
        "controlAction": "steer",
        "taskUuid": "task-col-1",
        "controlUuid": "control-col-1",
        "summary": "已追加给后台 Agent",
        "text": "补充",
    })

    cur = await web_env.db.conn.execute(
        "SELECT target_type, target_id, task_uuid, run_id FROM web_operations WHERE conversation_uuid=? AND op_type='agent_control' LIMIT 1",
        (row["conversation_uuid"],),
    )
    op = await cur.fetchone()
    assert op is not None
    assert dict(op)["target_type"] == "task"
    assert dict(op)["target_id"] == "task-col-1"
    assert dict(op)["task_uuid"] == "task-col-1"

    cur = await web_env.db.conn.execute(
        "SELECT target_type, target_id, task_uuid, run_id FROM web_event_frames WHERE conversation_uuid=? AND op_type='agent_control' LIMIT 1",
        (row["conversation_uuid"],),
    )
    frame = await cur.fetchone()
    assert frame is not None
    assert dict(frame)["target_type"] == "task"
    assert dict(frame)["target_id"] == "task-col-1"
    assert dict(frame)["task_uuid"] == "task-col-1"


async def test_web_stop_without_active_work_does_not_append_stopped_event(web_env):
    row = await web_env.server._create_web_conversation(123, title="empty stop")

    result = await web_env.server._stop_web_conversation(row)

    assert result == {"ok": True, "stoppedRun": False, "stoppedTasks": 0, "stoppedProcesses": 0}
    frames = await _web_frames_for(web_env, row["conversation_uuid"], event_type="stopped")
    assert frames == []


async def test_web_stop_attaches_to_current_turn_without_blank_random_turn(web_env):
    row = await web_env.server._create_web_conversation(123, title="stop active")
    live = web_env.server._live_for(row)
    chat_id = int(row["internal_chat_id"])
    web_env.server.control_actions = ControlActionQueue()
    await live.publish({"type": "accepted", "chatId": chat_id, "turnUuid": "turn-active"})
    await live.publish({"type": "user", "turnUuid": "turn-active", "messageUuid": "msg-active", "text": "跑一下"})

    class ConfirmedRuns:
        def __init__(self):
            self.running = True
            self.waited = False

        def is_running(self, target_chat_id):
            assert target_chat_id == chat_id
            return self.running

        async def cancel_and_wait(self, target_chat_id, *, timeout_s=10.0):
            assert target_chat_id == chat_id
            assert timeout_s == 5.0
            self.waited = True
            self.running = False
            return True

    runs = ConfirmedRuns()
    web_env.server.runs = runs

    result = await web_env.server._stop_web_conversation(row)

    assert runs.waited is True
    assert result["stoppedRun"] is True
    assert web_env.server.control_actions.consume_soft_stop(chat_id) == ""
    stopped = await _web_frames_for(web_env, row["conversation_uuid"], event_type="stopped")
    assert stopped
    assert {frame["turnUuid"] for frame in stopped} == {"turn-active"}
    operations = await web_env.server._web_operations(row["conversation_uuid"])
    assert not any(op["opType"] == "user_message" and not (op.get("payload") or {}).get("text") for op in operations)


async def test_web_stop_waits_for_run_cancel_cleanup_before_publishing_stopped(web_env):
    row = await web_env.server._create_web_conversation(123, title="stop waits for cleanup")
    live = web_env.server._live_for(row)
    chat_id = int(row["internal_chat_id"])
    web_env.server.control_actions = ControlActionQueue()
    started = asyncio.Event()
    cleaned = asyncio.Event()
    blocker = asyncio.Event()

    async def controller_run():
        started.set()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            cleaned.set()
            raise

    task = asyncio.create_task(controller_run())
    web_env.server.runs = RunRegistry()
    web_env.server.runs.register(chat_id, task)
    await started.wait()
    await live.publish({"type": "accepted", "chatId": chat_id, "turnUuid": "turn-cleanup"})
    try:
        result = await web_env.server._stop_web_conversation(row)
        assert result["stoppedRun"] is True
        assert cleaned.is_set()
        assert task.done()
        assert web_env.server.control_actions.consume_soft_stop(chat_id) == ""
        stopped = await _web_frames_for(web_env, row["conversation_uuid"], event_type="stopped")
        assert stopped
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_web_stop_does_not_claim_stopped_when_run_remains_alive(web_env):
    row = await web_env.server._create_web_conversation(123, title="stop timeout")
    live = web_env.server._live_for(row)
    chat_id = int(row["internal_chat_id"])
    web_env.server.control_actions = ControlActionQueue()
    await live.publish({"type": "accepted", "chatId": chat_id, "turnUuid": "turn-stuck"})

    class StuckRuns:
        def is_running(self, target_chat_id):
            assert target_chat_id == chat_id
            return True

        async def cancel_and_wait(self, target_chat_id, *, timeout_s=10.0):
            assert target_chat_id == chat_id
            assert timeout_s == 5.0
            return False

    web_env.server.runs = StuckRuns()
    try:
        result = await web_env.server._stop_web_conversation(row)
        assert result["ok"] is False
        assert result["error"] == "conversation_stop_timeout"
        assert result["stoppedRun"] is False
        assert await _web_frames_for(web_env, row["conversation_uuid"], event_type="stopped") == []
        fresh = await web_env.server._conversation_row(123, row["conversation_uuid"])
        assert fresh is not None
        assert fresh["status"] == "running"
        assert fresh["current_status"] == "停止中"
        assert web_env.server.control_actions.consume_soft_stop(chat_id) == "已停止"
    finally:
        web_env.server.control_actions.consume_soft_stop(chat_id)


async def test_finished_web_live_snapshot_uses_db_idle_status(web_env):
    row = await web_env.server._create_web_conversation(123, title="stale live")
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "chatId": row["internal_chat_id"], "turnUuid": "turn-1"})
    await live.publish({"type": "tool_progress", "turnUuid": "turn-1", "toolCallId": "call-1", "name": "Agent", "payload": {"status": "running"}})
    # Simulate a finished run where the durable conversation row is already idle
    # but the in-memory live snapshot still carries the last tool_progress label.
    live.status = "idle"
    live.current_status = "工具执行中"
    await web_env.server._touch_web_conversation(
        row["conversation_uuid"],
        status="idle",
        current_status="就绪",
        last_error="",
    )
    fresh = await web_env.server._conversation_row(123, row["conversation_uuid"])

    state = await web_env.server._chat_payload(int(row["internal_chat_id"]), fresh)

    assert state["running"] is False
    assert state["live"]["currentStatus"] == "就绪"
    assert state["conversation"]["currentStatus"] == "就绪"


async def test_web_event_frames_are_durable_cursor_source(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation")
    conv_uuid = row["conversation_uuid"]
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)

    first = await live.publish({"type": "accepted", "turnUuid": "turn-1"})
    user = await live.publish({"type": "user", "turnUuid": "turn-1", "messageUuid": "msg-user", "text": "你好"})
    final = await live.publish({"type": "final", "turnUuid": "turn-1", "text": "收到"})

    assert [first["frameSeq"], user["frameSeq"], final["frameSeq"]] == [1, 2, 3]
    state = await web_env.server._chat_payload(chat_id, row)
    assert state["frameSeq"] == 3
    frames = await web_env.server._web_frames(conv_uuid)
    assert [frame["debug"]["eventType"] for frame in frames] == ["accepted", "user", "final"]
    ops = {op["opId"]: op for op in state["operations"]}
    assert ops["msg:msg-user"]["payload"]["text"] == "你好"
    assert ops["assistant:turn-1:0"]["payload"]["text"] == "收到"

    # Simulate process/live-bus replacement: frame_seq must continue from SQLite,
    # so browser reconnects can use afterFrameSeq without a legacy event cursor.
    web_env.server._web_live_streams.clear()
    fresh = await web_env.server._conversation_row(123, conv_uuid, require=True)
    live2 = web_env.server._live_for(fresh)
    status = await live2.publish({"type": "status", "turnUuid": "turn-1", "status": "继续"})
    assert status["frameSeq"] == 4
    state2 = await web_env.server._chat_payload(chat_id, fresh)
    assert state2["frameSeq"] >= 4
    frames2 = await web_env.server._web_frames(conv_uuid)
    assert [frame["frameSeq"] for frame in frames2[:4]] == [1, 2, 3, 4]


async def test_web_event_frame_retention_keeps_latest_operation_snapshot_and_reconnect_window(web_env):
    row = await web_env.server._create_web_conversation(123, title="frame retention")
    conv_uuid = row["conversation_uuid"]
    for index in range(110):
        await web_env.server._publish_operation(
            conv_uuid,
            op_id="stats:retention",
            op_type="stats",
            action="patch",
            payload={"index": index},
            status="running",
            lifecycle="active",
        )
    await web_env.db.conn.execute(
        "UPDATE web_event_frames SET updated_at_ms=9999999999999 WHERE conversation_uuid=?",
        (conv_uuid,),
    )
    await web_env.db.conn.commit()

    deleted = await web_env.server._prune_web_event_frames(
        conv_uuid,
        keep_recent=100,
        max_age_days=1,
    )
    frames = await web_env.server._web_frames(conv_uuid, after_frame_seq=0, limit=200)
    operation = {
        op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)
    }["stats:retention"]
    assert deleted == 10
    assert len(frames) == 100
    assert frames[0]["frameSeq"] == 11
    assert frames[-1]["frameSeq"] == 110
    assert operation["payload"]["index"] == 109
    assert operation["revision"] == 110


async def test_web_frames_default_window_returns_latest_frames(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation latest window")
    live = web_env.server._live_for(row)

    for i in range(8):
        await live.publish({"type": "notice", "turnUuid": f"turn-{i}", "text": f"event-{i}"})

    frames = await web_env.server._web_frames(row["conversation_uuid"], limit=100)
    latest = frames[-3:]
    assert [_frame_payload(frame)["text"] for frame in latest] == ["event-5", "event-6", "event-7"]
    assert [frame["frameSeq"] for frame in latest] == [6, 7, 8]

    incremental = await web_env.server._web_frames(row["conversation_uuid"], after_frame_seq=5, limit=3)
    assert [_frame_payload(frame)["text"] for frame in incremental] == ["event-5", "event-6", "event-7"]


async def test_web_operations_mutable_delta_upserts_latest_snapshot(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation delta")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-delta"})
    await live.publish({"type": "user", "turnUuid": "turn-delta", "messageUuid": "msg-user", "text": "写长文"})
    first = await live.publish({"type": "delta", "turnUuid": "turn-delta", "text": "第一帧"})
    second = await live.publish({"type": "delta", "turnUuid": "turn-delta", "text": "第二帧，覆盖第一帧"})

    assert second["opId"] == first["opId"] == "assistant:turn-delta:0"
    assert second["revision"] > first["revision"]
    assistant_ops = [op for op in await web_env.server._web_operations(row["conversation_uuid"]) if op["opType"] == "assistant_message"]
    assert len(assistant_ops) == 1
    assert assistant_ops[0]["payload"]["text"] == "第二帧，覆盖第一帧"
    delta_frames = await _web_frames_for(web_env, row["conversation_uuid"], event_type="delta")
    assert [frame["revision"] for frame in delta_frames] == [1, 2]
    assert live.snapshot()["events"] == []


async def test_web_running_send_keeps_pending_steering_on_backend_root_turn(web_env):
    row = await web_env.server._create_web_conversation(123, title="pending steering")
    live = web_env.server._live_for(row)
    chat_id = int(row["internal_chat_id"])
    steering.clear(chat_id)
    web_env.server.runs = RunRegistry()
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    web_env.server.runs.register(chat_id, task)
    try:
        result = await web_env.server._start_or_steer_web_conversation(row, "你到底在干嘛", [], live)
        assert result["ok"] is True
        assert result["queued"] is True
        assert [item["text"] for item in steering.pending_items(chat_id)] == ["你到底在干嘛"]

        ops = await web_env.server._web_operations(row["conversation_uuid"])
        user_ops = [op for op in ops if op.get("opType") == "user_message"]
        assert len(user_ops) == 1
        assert user_ops[0]["turnUuid"] == result["rootTurnUuid"]
        assert user_ops[0]["runRootTurnId"] == result["rootTurnUuid"]
        assert (user_ops[0].get("payload") or {}).get("queued") is True
        assert (user_ops[0].get("payload") or {}).get("text") == "你到底在干嘛"
        state = await web_env.server._chat_payload(chat_id, row)
        assert [item["text"] for item in state["pendingSteering"]] == ["你到底在干嘛"]
    finally:
        steering.clear(chat_id)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_web_stop_clears_pending_steering_without_injecting_user(web_env, monkeypatch):
    row = await web_env.server._create_web_conversation(123, title="pending steering stop")
    live = web_env.server._live_for(row)
    chat_id = int(row["internal_chat_id"])
    steering.clear(chat_id)
    killed_chats: list[int] = []
    monkeypatch.setattr("app.web_console.chat_api.processes.active", lambda: [SimpleNamespace(chat_id=chat_id, task_uuid="bash-stop-task")])
    monkeypatch.setattr("app.web_console.chat_api.processes.kill_for_chat", lambda cid: killed_chats.append(int(cid)) or 1)
    web_env.server.runs = RunRegistry()
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    web_env.server.runs.register(chat_id, task)
    try:
        result = await web_env.server._start_or_steer_web_conversation(row, "停止前待处理插话", [], live)
        assert result["queued"] is True
        assert steering.pending_items(chat_id)

        stop = await web_env.server._stop_web_conversation(row, message="测试停止")
        assert stop["ok"] is True
        assert stop["stoppedProcesses"] == 1
        assert killed_chats == [chat_id]
        assert "bash-stop-task" in web_env.server._web_stopped_task_uuids[row["conversation_uuid"]]
        assert steering.pending_items(chat_id) == []
        state = await web_env.server._chat_payload(chat_id, row)
        assert state["pendingSteering"] == []
        ops = await web_env.server._web_operations(row["conversation_uuid"])
        queued_ops = [
            op for op in ops
            if op.get("opType") == "user_message"
            and "停止前待处理插话" in str((op.get("payload") or {}).get("text") or "")
        ]
        assert len(queued_ops) == 1
        assert (queued_ops[0].get("payload") or {}).get("queued") is True
        await web_env.server._schedule_web_task_notification(row, {"taskUuid": "bash-stop-task", "status": "failed", "summary": "Bash 后台任务失败"})
        await web_env.server._schedule_web_task_notification(row, {"taskUuid": "bash-untracked-task", "status": "failed", "summary": "Bash 后台任务失败"})
        assert web_env.server._web_task_notification_pending.get(row["conversation_uuid"]) in (None, [])
    finally:
        steering.clear(chat_id)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_web_pending_steering_injects_user_only_at_loop_boundary(web_env):
    row = await web_env.server._create_web_conversation(123, title="pending steering drain")
    live = web_env.server._live_for(row)
    chat_id = int(row["internal_chat_id"])
    steering.clear(chat_id)

    await live.publish({"type": "accepted", "turnUuid": "turn-old"})
    await live.publish({"type": "user", "turnUuid": "turn-old", "messageUuid": "msg-old", "text": "先查"})
    await live.publish({"type": "delta", "text": "我先只做只读排查"})
    await live.publish({"type": "tool_start", "toolCallId": "call-1", "name": "Bash", "arguments": "{}", "line": "Bash: running"})
    await live.publish({"type": "tool_result", "toolCallId": "call-1", "name": "Bash", "arguments": "{}", "result": "ok"})

    steering.enqueue(chat_id, "你到底在干嘛", source="web", turnUuid="turn-new", messageUuid="msg-new")
    # 发送瞬间只是 composer pending 状态，不应进入 transcript/operation。
    assert [op for op in await web_env.server._web_operations(row["conversation_uuid"]) if op.get("opId") == "msg:msg-new"] == []

    await live.publish({"type": "final", "text": "查到了，先不改代码"})
    renderer = _WebStreamRenderer(live=live)
    items = steering.drain_items(chat_id)
    await renderer.on_steers_injected(items, injected_texts=["你到底在干嘛"], cut=True)
    await live.publish({"type": "final", "text": "我查过头了"})

    ops = await web_env.server._web_operations(row["conversation_uuid"])
    positions = {op["opId"]: op["displaySeq"] for op in ops}
    assert positions["assistant:turn-old:1"] > positions["tool:call-1"]
    assert positions["msg:msg-new"] > positions["assistant:turn-old:1"]
    assert positions["assistant:turn-new:0"] > positions["msg:msg-new"]
    injected = next(op for op in ops if op.get("opId") == "msg:msg-new")
    assert (injected.get("payload") or {}).get("interruption") is True
    assert (injected.get("payload") or {}).get("queued") is False
    assert (injected.get("payload") or {}).get("status") == "插话已交给主会话"


async def test_web_cut_closes_the_current_same_turn_assistant_segment(web_env):
    row = await web_env.server._create_web_conversation(123, title="same turn cut")
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    live._assistant_segment = 3
    await live.publish({"type": "delta", "turnUuid": "turn-root", "text": "第三段"})
    cut = await live.publish({"type": "cut", "turnUuid": "turn-root"})

    assert cut["eventKey"] == "assistant:draft:3"
    ops = {op["opId"]: op for op in await web_env.server._web_operations(row["conversation_uuid"])}
    assert ops["assistant:turn-root:3"]["payload"]["complete"] is True
    assert "assistant:turn-root:0" not in ops


async def test_web_operations_tool_result_keeps_original_turn_after_queued_user(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation tool result turn")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-old"})
    await live.publish({"type": "user", "turnUuid": "turn-old", "messageUuid": "msg-old", "text": "跑个工具"})
    start = await live.publish({"type": "tool_start", "toolCallId": "call-1", "name": "Bash", "arguments": "{}", "line": "Bash: running"})
    await live.publish({"type": "user", "turnUuid": "turn-new", "messageUuid": "msg-new", "text": "补充一句"})
    await live.publish({"type": "queued", "turnUuid": "turn-new", "text": "补充一句", "status": "已追加到当前运行"})
    result = await live.publish({"type": "tool_result", "toolCallId": "call-1", "name": "Bash", "arguments": "{}", "result": "ok", "durationMs": 12})

    assert start["turnUuid"] == "turn-old"
    assert result["turnUuid"] == "turn-old"
    frames = await _web_frames_for(web_env, row["conversation_uuid"])
    by_type = {frame["debug"].get("eventType"): frame for frame in frames if frame["debug"].get("eventType") in {"tool_start", "tool_result"}}
    assert by_type["tool_start"]["turnUuid"] == "turn-old"
    assert by_type["tool_result"]["turnUuid"] == "turn-old"


async def test_web_operations_tool_progress_keeps_original_turn_after_queued_user(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation tool progress turn")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-old"})
    await live.publish({"type": "user", "turnUuid": "turn-old", "messageUuid": "msg-old", "text": "跑个工具"})
    await live.publish({"type": "tool_start", "toolCallId": "call-1", "name": "Agent", "arguments": "{}", "line": "Agent: running"})
    await live.publish({"type": "user", "turnUuid": "turn-new", "messageUuid": "msg-new", "text": "补充一句"})
    ledger_usage = {"ledgerRevision": 7, "inputTokens": 70, "outputTokens": 7, "cacheReadTokens": 5, "cacheWriteTokens": 1, "costUsd": 0.7}
    progress = await live.publish({
        "type": "tool_progress",
        "toolCallId": "call-1",
        "name": "Agent",
        "arguments": "{}",
        "payload": {"status": "running", "ledgerUsage": ledger_usage},
    })

    assert progress["turnUuid"] == "turn-old"
    progress_frames = await _web_frames_for(web_env, row["conversation_uuid"], event_type="tool_progress")
    assert len(progress_frames) == 1
    assert progress_frames[0]["turnUuid"] == "turn-old"
    assert _frame_payload(progress_frames[0])["ledgerUsage"] == ledger_usage


async def test_web_renderer_keeps_agent_progress_turn_after_main_run_done(web_env):
    row = await web_env.server._create_web_conversation(123, title="closed renderer agent progress")
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live=live)

    await live.publish({"type": "accepted", "turnUuid": "turn-old"})
    await live.publish({"type": "user", "turnUuid": "turn-old", "messageUuid": "msg-old", "text": "跑 Agent"})
    await renderer.on_tool_start("call-1", "Agent", "{}", "Agent: running")
    await renderer.close()
    assert live.current_turn_uuid == ""

    ledger_usage = {"ledgerRevision": 8, "inputTokens": 80, "outputTokens": 8, "cacheReadTokens": 6, "cacheWriteTokens": 2, "costUsd": 0.8}
    await renderer.on_tool_progress(
        "call-1",
        "Agent",
        "{}",
        {"status": "running", "detached": True, "ledgerUsage": ledger_usage},
    )
    await renderer.on_tool_result("call-1", "Agent", "{}", '{"status":"completed"}', 12)

    followups = await _web_frames_for(web_env, row["conversation_uuid"])
    followups = [frame for frame in followups if frame["debug"].get("eventType") in {"tool_progress", "tool_result"}]
    assert followups
    assert {frame.get("turnUuid") for frame in followups} == {"turn-old"}
    progress_frame = next(frame for frame in followups if frame["debug"].get("eventType") == "tool_progress")
    assert _frame_payload(progress_frame)["ledgerUsage"] == ledger_usage


async def test_web_renderer_close_after_external_stop_does_not_emit_bare_done(web_env):
    row = await web_env.server._create_web_conversation(123, title="external stop close")
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live=live)

    await live.publish({"type": "accepted", "turnUuid": "turn-stop"})
    await live.publish({"type": "user", "turnUuid": "turn-stop", "messageUuid": "msg-stop", "text": "停一下"})
    await live.publish({"type": "stopped", "turnUuid": "turn-stop", "reason": "已停止"})
    await renderer.close()

    stopped = await _web_frames_for(web_env, row["conversation_uuid"], event_type="stopped")
    done = await _web_frames_for(web_env, row["conversation_uuid"], event_type="done")
    assert {(frame.get("turnUuid"), _frame_payload(frame).get("reason")) for frame in stopped} == {("turn-stop", "已停止")}
    assert done == []


async def test_web_renderer_close_after_renderer_stop_does_not_emit_done(web_env):
    row = await web_env.server._create_web_conversation(123, title="renderer stop close")
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live=live)

    await live.publish({"type": "accepted", "turnUuid": "turn-stop"})
    await live.publish({"type": "user", "turnUuid": "turn-stop", "messageUuid": "msg-stop", "text": "停一下"})
    await renderer.emit({"type": "stopped", "reason": "已停止"})
    await renderer.close()

    assert {frame.get("turnUuid") for frame in await _web_frames_for(web_env, row["conversation_uuid"], event_type="stopped")} == {"turn-stop"}
    assert await _web_frames_for(web_env, row["conversation_uuid"], event_type="done") == []


async def test_web_operations_sibling_tool_start_keeps_original_turn_after_queued_user(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation sibling tool turn")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-old"})
    await live.publish({"type": "user", "turnUuid": "turn-old", "messageUuid": "msg-old", "text": "连续跑两个工具"})
    live.pin_tool_batch_turn(["call-1", "call-2"])
    await live.publish({"type": "tool_start", "toolCallId": "call-1", "name": "Bash", "arguments": "{}", "line": "Bash: first"})
    await live.publish({"type": "user", "turnUuid": "turn-new", "messageUuid": "msg-new", "text": "补充一句"})
    queued = await live.publish({"type": "queued", "turnUuid": "turn-new", "text": "补充一句", "status": "已追加到当前运行"})
    await live.publish({"type": "tool_result", "toolCallId": "call-1", "name": "Bash", "arguments": "{}", "result": "one", "durationMs": 12})
    start2 = await live.publish({"type": "tool_start", "toolCallId": "call-2", "name": "Bash", "arguments": "{}", "line": "Bash: second"})
    result2 = await live.publish({"type": "tool_result", "toolCallId": "call-2", "name": "Bash", "arguments": "{}", "result": "two", "durationMs": 8})

    assert queued["turnUuid"] == "turn-new"
    assert live.snapshot()["currentStatus"] == "工具已完成"
    assert start2["turnUuid"] == "turn-old"
    assert result2["turnUuid"] == "turn-old"
    tool_frames = [frame for frame in await _web_frames_for(web_env, row["conversation_uuid"]) if frame.get("opId") in {"tool:call-1", "tool:call-2"}]
    assert tool_frames
    assert {frame["turnUuid"] for frame in tool_frames} == {"turn-old"}


async def test_web_operations_agent_turn_switches_only_after_steer_is_injected(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation steer activation")
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live=live)

    await live.publish({"type": "accepted", "turnUuid": "turn-old"})
    await live.publish({"type": "user", "turnUuid": "turn-old", "messageUuid": "msg-old", "text": "跑工具"})
    await renderer.on_tool_start("call-1", "Bash", "{}", "Bash: running")
    await live.publish({"type": "user", "turnUuid": "turn-new", "messageUuid": "msg-new", "text": "补充一句"})
    queued = await live.publish({"type": "queued", "turnUuid": "turn-new", "text": "补充一句", "status": "已追加到当前运行"})
    still_old = await renderer.live.publish({"type": "tool_result", "toolCallId": "call-1", "name": "Bash", "arguments": "{}", "result": "ok"})
    await renderer.on_steers_injected()
    next_status = await renderer.live.publish({"type": "status", "status": "正在思考 …"})

    assert queued["turnUuid"] == "turn-new"
    assert still_old["turnUuid"] == "turn-old"
    assert next_status["turnUuid"] == "turn-new"


async def test_web_operations_tool_update_carries_call_id_and_keeps_original_turn(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation tool update turn")
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live=live)

    await live.publish({"type": "accepted", "turnUuid": "turn-old"})
    await live.publish({"type": "user", "turnUuid": "turn-old", "messageUuid": "msg-old", "text": "跑 Bash"})
    await renderer.on_tool_start("call-1", "Bash", '{"command":"sleep 1"}', "Bash: running")
    await live.publish({"type": "user", "turnUuid": "turn-new", "messageUuid": "msg-new", "text": "补充一句"})
    await renderer.on_tool_update("Bash: still running", tool_call_id="call-1", name="Bash", arguments='{"command":"sleep 1"}')

    updates = await _web_frames_for(web_env, row["conversation_uuid"], event_type="tool_update")
    assert len(updates) == 1
    assert _frame_payload(updates[0])["toolCallId"] == "call-1"
    assert updates[0]["turnUuid"] == "turn-old"


async def test_web_operations_error_is_persisted_with_turn_and_message(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation error")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-err"})
    await live.publish({"type": "user", "turnUuid": "turn-err", "messageUuid": "msg-err", "text": "会报错"})
    err_event = await live.publish({"type": "error", "error": "❌ 内部错误：boom"})

    errors = await _web_frames_for(web_env, row["conversation_uuid"], event_type="error")
    assistant_error = next(frame for frame in errors if frame["opType"] == "assistant_message")
    assert assistant_error["turnUuid"] == "turn-err"
    assert err_event["messageUuid"]
    assert _frame_payload(assistant_error)["text"] == "❌ 内部错误：boom"


async def test_web_live_error_status_survives_stats_and_done(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation error status")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-err"})
    await live.publish({"type": "user", "turnUuid": "turn-err", "messageUuid": "msg-err", "text": "会报错"})
    await live.publish({"type": "error", "error": "boom"})
    await live.publish({"type": "stats", "stats": {"modelFail": 1}})
    await live.publish({"type": "done"})

    snapshot = live.snapshot()
    assert snapshot["status"] == "error"
    assert snapshot["currentStatus"] == "出错"
    assert snapshot["lastError"] == "boom"


async def test_web_done_completes_pre_steering_active_operations(web_env):
    row = await web_env.server._create_web_conversation(123, title="done after steering")
    live = web_env.server._live_for(row)
    conv_uuid = row["conversation_uuid"]

    await live.publish({"type": "accepted", "turnUuid": "turn-old"})
    await live.publish({"type": "user", "turnUuid": "turn-old", "messageUuid": "msg-old", "text": "先跑"})
    await live.publish({"type": "status", "turnUuid": "turn-old", "status": "正在思考 …"})
    await live.publish({"type": "delta", "turnUuid": "turn-old", "eventKey": "assistant:draft:1", "text": "前半段"})
    await live.publish({"type": "delta", "turnUuid": "turn-old", "eventKey": "assistant:draft:2", "text": "", "reasoning": "旧思考"})

    await live.publish({"type": "user", "turnUuid": "turn-new", "messageUuid": "msg-new", "text": "补充一句"})
    await live.publish({"type": "queued", "turnUuid": "turn-new", "text": "补充一句", "status": "已追加到当前运行"})
    live.activate_latest_user_turn()
    await live.publish({"type": "final", "turnUuid": "turn-new", "eventKey": "assistant:draft:0", "text": "最终回答"})
    await live.publish({"type": "stats", "turnUuid": "turn-new", "stats": {"modelCalls": 1}})
    await live.publish({"type": "done"})

    cur = await web_env.db.conn.execute(
        """
        SELECT op_id FROM web_operations
        WHERE conversation_uuid=?
          AND op_type IN ('run','status','assistant_message','reasoning')
          AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')
        ORDER BY op_id
        """,
        (conv_uuid,),
    )
    assert [row["op_id"] for row in await cur.fetchall()] == []
    ops = {op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)}
    assert ops["run:turn-old"]["status"] == "completed"
    assert ops["status:turn-old"]["status"] == "completed"
    assert ops["assistant:turn-old:1"]["payload"]["complete"] is True
    assert ops["reasoning:turn-old:2"]["payload"]["complete"] is True


async def test_web_operations_stats_are_persisted_on_current_turn(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation stats")
    live = web_env.server._live_for(row)
    stats = {"model": "openai/gpt", "durationMs": 123, "modelCalls": 1, "modelOk": 1}

    await live.publish({"type": "accepted", "turnUuid": "turn-stats"})
    await live.publish({"type": "user", "turnUuid": "turn-stats", "messageUuid": "msg-stats", "text": "统计"})
    stat_event = await live.publish({"type": "stats", "stats": stats})

    assert stat_event["turnUuid"] == "turn-stats"
    stat_frames = await _web_frames_for(web_env, row["conversation_uuid"], event_type="stats")
    assert len(stat_frames) == 1
    assert stat_frames[0]["turnUuid"] == "turn-stats"
    assert {k: _frame_payload(stat_frames[0])[k] for k in stats} == stats


async def test_web_operations_mutable_delta_reasoning_survives_tool_start(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation reasoning")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-reason"})
    await live.publish({"type": "user", "turnUuid": "turn-reason", "messageUuid": "msg-reason", "text": "先想再工具"})
    await live.publish({"type": "delta", "turnUuid": "turn-reason", "text": "", "reasoning": "思考中"})
    await live.publish({"type": "tool_start", "toolCallId": "call-1", "name": "Read", "arguments": "{}", "line": "Read: running"})

    reasoning_ops = [op for op in await web_env.server._web_operations(row["conversation_uuid"]) if op["opType"] == "reasoning"]
    assert len(reasoning_ops) == 1
    assert reasoning_ops[0]["payload"]["text"] == "思考中"


async def test_web_operation_publication_rolls_back_snapshot_when_frame_insert_fails(web_env):
    row = await web_env.server._create_web_conversation(123, title="atomic operation")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    await web_env.server._publish_operation(
        conv_uuid,
        internal_chat_id=chat_id,
        op_id="assistant:atomic:0",
        op_type="assistant_message",
        action="append",
        turn_uuid="atomic",
        payload={"text": "A", "complete": False},
        status="running",
        lifecycle="active",
    )
    await web_env.db.conn.execute(
        """
        CREATE TRIGGER reject_atomic_frame
        BEFORE INSERT ON web_event_frames
        WHEN NEW.op_id='assistant:atomic:0' AND NEW.revision=2
        BEGIN SELECT RAISE(ABORT, 'injected frame failure'); END
        """
    )
    await web_env.db.conn.commit()

    with pytest.raises(Exception, match="injected frame failure"):
        await web_env.server._publish_operation(
            conv_uuid,
            internal_chat_id=chat_id,
            op_id="assistant:atomic:0",
            op_type="assistant_message",
            action="append",
            turn_uuid="atomic",
            payload={"text": "AB", "complete": False},
            status="running",
            lifecycle="active",
        )

    await web_env.db.conn.commit()  # unrelated shared-connection commit cannot expose the failed snapshot
    cur = await web_env.db.conn.execute(
        "SELECT revision, payload_json FROM web_operations WHERE conversation_uuid=? AND op_id=?",
        (conv_uuid, "assistant:atomic:0"),
    )
    operation = await cur.fetchone()
    assert int(operation["revision"]) == 1
    assert json.loads(operation["payload_json"])["text"] == "A"
    cur = await web_env.db.conn.execute(
        "SELECT revision FROM web_event_frames WHERE conversation_uuid=? AND op_id=? ORDER BY revision",
        (conv_uuid, "assistant:atomic:0"),
    )
    assert [int(frame["revision"]) for frame in await cur.fetchall()] == [1]


async def test_live_stats_duration_ticks_broadcast_without_persisting_frames(web_env):
    row = await web_env.server._create_web_conversation(123, title="ephemeral stats timer")
    conv_uuid = str(row["conversation_uuid"])
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-stats", "runUuid": "run-stats"})
    await live.publish({
        "type": "stats",
        "stats": {"live": True, "durationMs": 100, "modelCalls": 1, "toolCalls": 0, "costUsd": 0.01},
    })
    first_frames = await _web_frames_for(web_env, conv_uuid, op_type="stats")
    assert len(first_frames) == 1

    subscriber = live.subscribe()
    try:
        timer_tick = await live.publish({
            "type": "stats",
            "stats": {"live": True, "durationMs": 600, "modelCalls": 1, "toolCalls": 0, "costUsd": 0.01},
        })
        delivered = await asyncio.wait_for(subscriber.get(), timeout=1)
        assert timer_tick["stats"]["durationMs"] == 600
        assert delivered["stats"]["durationMs"] == 600
        assert len(await _web_frames_for(web_env, conv_uuid, op_type="stats")) == 1

        await live.publish({
            "type": "stats",
            "stats": {"live": True, "durationMs": 700, "modelCalls": 2, "toolCalls": 0, "costUsd": 0.02},
        })
        durable_frames = await _web_frames_for(web_env, conv_uuid, op_type="stats")
        assert len(durable_frames) == 2
        assert _frame_payload(durable_frames[-1])["modelCalls"] == 2
    finally:
        live.unsubscribe(subscriber)


async def test_live_delta_frames_are_throttled_but_forced_boundary_is_durable(web_env):
    row = await web_env.server._create_web_conversation(123, title="throttled stream frames")
    conv_uuid = str(row["conversation_uuid"])
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live)

    await live.publish({"type": "accepted", "turnUuid": "turn-delta", "runUuid": "run-delta"})
    await renderer.on_delta("A")
    await renderer.on_delta("AB")
    await renderer._flush_delta()
    assert len(await _web_frames_for(web_env, conv_uuid, op_type="assistant_message")) == 1

    renderer._last_delta_persist_ms -= 300
    await renderer.on_delta("ABC")
    await renderer._flush_delta()
    frames = await _web_frames_for(web_env, conv_uuid, op_type="assistant_message")
    assert len(frames) == 2
    operations = await web_env.server._web_operations(conv_uuid)
    assistant = next(op for op in operations if op["opType"] == "assistant_message")
    assert assistant["payload"]["text"] == "ABC"

    await renderer.on_delta("ABCD")
    await renderer._flush_delta(force_persist=True)
    frames = await _web_frames_for(web_env, conv_uuid, op_type="assistant_message")
    assert len(frames) == 3
    assistant = next(op for op in await web_env.server._web_operations(conv_uuid) if op["opType"] == "assistant_message")
    assert assistant["payload"]["text"] == "ABCD"


async def test_live_delta_broadcast_only_snapshot_is_persisted_at_tool_boundary(web_env):
    row = await web_env.server._create_web_conversation(123, title="broadcast-only delta boundary")
    conv_uuid = str(row["conversation_uuid"])
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live)

    await live.publish({"type": "accepted", "turnUuid": "turn-delta-boundary", "runUuid": "run-delta-boundary"})
    await renderer.on_delta("先")

    # Deterministically exercise a UI-cadence flush inside the 250 ms durable
    # cadence.  The latest snapshot must remain pending after this broadcast.
    renderer._last_delta_emit_ms = 10**30
    await renderer.on_delta("先完整提交当前代码")
    renderer._last_delta_persist_ms = 10**30
    await renderer._flush_delta()

    operations = await web_env.server._web_operations(conv_uuid)
    assistant = next(op for op in operations if op["opType"] == "assistant_message")
    assert assistant["payload"]["text"] == "先"
    assert renderer._pending_delta is not None
    assert renderer._pending_delta["text"] == "先完整提交当前代码"

    await renderer.on_tool_start("call-boundary", "Bash", "{}")

    operations = await web_env.server._web_operations(conv_uuid)
    assistant = next(op for op in operations if op["opType"] == "assistant_message")
    assert assistant["payload"]["text"] == "先完整提交当前代码"
    assert renderer._pending_delta is None


async def test_web_operations_keep_agent_snapshot_fresh(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation agent freshness")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-agent"})
    await live.publish({"type": "user", "turnUuid": "turn-agent", "messageUuid": "msg-agent", "text": "跑 Agent"})
    await live.publish({"type": "tool_start", "toolCallId": "call-agent", "name": "Agent", "arguments": "{}", "line": "Agent: running"})
    first_progress = await live.publish({
        "type": "tool_progress",
        "toolCallId": "call-agent",
        "name": "Agent",
        "arguments": "{}",
        "payload": {"status": "running", "detached": True, "task": {"status": "running", "currentStatus": "检索中"}},
    })
    running_ack = await live.publish({
        "type": "tool_result",
        "toolCallId": "call-agent",
        "name": "Agent",
        "arguments": "{}",
        "result": '{"status":"running","detached":true,"task":{"status":"running","currentStatus":"初始 ACK"}}',
        "durationMs": 1,
    })
    terminal_progress = await live.publish({
        "type": "tool_progress",
        "toolCallId": "call-agent",
        "name": "Agent",
        "arguments": "{}",
        "payload": {"status": "completed", "detached": True, "task": {"status": "completed", "currentStatus": "任务完成"}, "result": {"summary": "done"}},
    })

    assert terminal_progress["opId"] == first_progress["opId"] == "agent:call-agent"
    assert terminal_progress["revision"] > first_progress["revision"]
    assert running_ack["frameSeq"] > first_progress["frameSeq"]
    state = await web_env.server._chat_payload(chat_id, row)
    ops = {op["opId"]: op for op in state["operations"]}
    agent = ops["agent:call-agent"]
    assert agent["status"] == "completed"
    assert agent["lifecycle"] == "terminal"
    assert agent["payload"]["task"]["currentStatus"] == "任务完成"
    assert agent["revision"] >= 4
    assert state["frameSeq"] >= terminal_progress["frameSeq"]
    assert state["facts"]["activeBackgroundAgentOpIds"] == []


async def test_agent_merge_redirect_is_persisted_only_once(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent merge redirect")
    live = web_env.server._live_for(row)
    task_uuid = "task-one-redirect"

    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "tool_start", "turnUuid": "turn-root", "toolCallId": "call-root", "name": "Agent", "arguments": "{}"})
    progress = {
        "type": "tool_progress",
        "turnUuid": "turn-root",
        "toolCallId": "call-root",
        "name": "Agent",
        "arguments": "{}",
        "payload": {"toolName": "Agent", "status": "running", "detached": True, "task": {"taskUuid": task_uuid, "status": "running"}},
    }
    await live.publish(progress)
    await live.publish(progress)

    placeholder_frames = [
        frame for frame in await web_env.server._web_frames(row["conversation_uuid"])
        if frame["opId"] == "agent:call-root" and _frame_payload(frame).get("merged")
    ]
    assert len(placeholder_frames) == 1
    assert placeholder_frames[0]["revision"] == 2
    assert _frame_payload(placeholder_frames[0])["mergedTo"] == f"agent:{task_uuid}"


async def test_agent_merge_redirect_keeps_first_task_target(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent merge redirect conflict")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "tool_start", "turnUuid": "turn-root", "toolCallId": "call-root", "name": "Agent", "arguments": "{}"})
    for task_uuid in ("task-first", "task-conflicting"):
        await live.publish({
            "type": "tool_progress",
            "turnUuid": "turn-root",
            "toolCallId": "call-root",
            "name": "Agent",
            "arguments": "{}",
            "payload": {
                "toolName": "Agent",
                "status": "running",
                "detached": True,
                "task": {"taskUuid": task_uuid, "status": "running"},
            },
        })

    placeholder_frames = [
        frame for frame in await web_env.server._web_frames(row["conversation_uuid"])
        if frame["opId"] == "agent:call-root" and _frame_payload(frame).get("merged")
    ]
    assert len(placeholder_frames) == 1
    assert placeholder_frames[0]["revision"] == 2
    assert _frame_payload(placeholder_frames[0])["mergedTo"] == "agent:task-first"


async def test_agent_merge_redirect_does_not_create_missing_placeholder(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent merge redirect without placeholder")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-root",
        "toolCallId": "call-without-start",
        "name": "Agent",
        "arguments": "{}",
        "payload": {
            "toolName": "Agent",
            "status": "running",
            "detached": True,
            "task": {"taskUuid": "task-without-placeholder", "status": "running"},
        },
    })

    state = await web_env.server._chat_payload(chat_id, row)
    ops = {op["opId"]: op for op in state["operations"]}
    assert "agent:call-without-start" not in ops
    assert ops["agent:task-without-placeholder"]["status"] == "running"


async def test_agent_task_operation_preserves_root_invocation_arguments(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent root invocation")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    original_arguments = '{"prompt":"最初的 Agent 任务","tools":["Read"]}'

    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({
        "type": "tool_start",
        "turnUuid": "turn-root",
        "toolCallId": "call-root",
        "name": "Agent",
        "arguments": original_arguments,
    })
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-root",
        "toolCallId": "call-root",
        "name": "Agent",
        "arguments": original_arguments,
        "payload": {
            "toolName": "Agent",
            "status": "running",
            "detached": True,
            "task": {"taskUuid": "task-root-args", "status": "running"},
        },
    })
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-root",
        "toolCallId": "call-wait",
        "name": "AgentWait",
        "arguments": '{"mode":"event_only","reason":"等待 Agent"}',
        "payload": {
            "toolName": "Agent",
            "status": "running",
            "detached": True,
            "task": {"taskUuid": "task-root-args", "status": "running"},
        },
    })
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-root",
        "toolCallId": "call-conflicting-agent",
        "name": "Agent",
        "arguments": '{"prompt":"错误覆盖参数"}',
        "payload": {
            "toolName": "Agent",
            "status": "running",
            "detached": True,
            "task": {"taskUuid": "task-root-args", "status": "running"},
        },
    })

    state = await web_env.server._chat_payload(chat_id, row)
    ops = {op["opId"]: op for op in state["operations"]}
    payload = ops["agent:task-root-args"]["payload"]
    assert payload["rootToolName"] == "Agent"
    assert payload["rootToolCallId"] == "call-root"
    assert payload["rootArguments"] == original_arguments


async def test_agent_plan_api_returns_immutable_launch_context_and_enforces_conversation_scope(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    owner = await web_env.server._create_web_conversation(123, title="plan launch context", model="openai/gpt")
    other = await web_env.server._create_web_conversation(123, title="other conversation", model="openai/gpt")
    web_env.server.rath.plan_coordinator = AgentPlanCoordinator(web_env.server.rath_dao, web_env.server.rath)
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=int(owner["internal_chat_id"]),
        parent_session_uuid=owner["conversation_uuid"],
        workflow_uuid="wf-plan-launch-context",
        title="durable launch context",
        input_data={
            "instruction": "最初的 Agent prompt",
            "source": "openbear_agent_tool",
            "agentSnapshot": {
                "name": "general-purpose",
                "model": "OpenAI/gpt-5.6-sol",
                "thinkLevel": "xhigh",
                "fastMode": True,
                "toolAllowlist": ["Read"],
            },
        },
        status="running",
    )

    response = await web_env.client.get(
        f"/api/conversations/{owner['conversation_uuid']}/agents/{task_uuid}/plan",
        cookies=cookie,
    )
    assert response.status == 200
    payload = await response.json()
    assert payload["ok"] is True
    assert payload["task"]["task_uuid"] == task_uuid
    assert payload["task"]["input"]["instruction"] == "最初的 Agent prompt"
    assert payload["task"]["input"]["agentSnapshot"]["toolAllowlist"] == ["Read"]

    wrong_conversation = await web_env.client.get(
        f"/api/conversations/{other['conversation_uuid']}/agents/{task_uuid}/plan",
        cookies=cookie,
    )
    assert wrong_conversation.status == 404


async def test_agent_events_api_pages_from_latest_and_enforces_conversation_scope(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    owner = await web_env.server._create_web_conversation(123, title="agent event paging", model="openai/gpt")
    other = await web_env.server._create_web_conversation(123, title="event paging wrong conversation", model="openai/gpt")
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=int(owner["internal_chat_id"]),
        parent_session_uuid=owner["conversation_uuid"],
        workflow_uuid="wf-agent-event-paging",
        title="event paging task",
        status="running",
    )
    await web_env.db.conn.execute("DELETE FROM rath_task_events WHERE task_uuid=?", (task_uuid,))
    await web_env.db.conn.commit()
    for index in range(1, 46):
        is_read = index in {25, 41}
        await web_env.server.rath_dao.append_event(
            task_uuid,
            "tool_call_started" if is_read else "model_call_finished",
            summary="调用工具 Read" if is_read else f"模型调用完成 {index}",
            detail={"name": "Read", "arguments": '{"path":"/tmp/demo"}'} if is_read else {"durationMs": index},
        )

    path = f"/api/conversations/{owner['conversation_uuid']}/agents/{task_uuid}/events"
    latest_response = await web_env.client.get(f"{path}?limit=20", cookies=cookie)
    assert latest_response.status == 200
    latest = await latest_response.json()
    assert latest["total"] == 45
    assert latest["hasMore"] is True
    assert latest["nextBeforeSeq"] == 26
    assert [event["seq"] for event in latest["events"]] == list(range(26, 46))
    assert latest["events"][15]["detail"]["name"] == "Read"

    middle_response = await web_env.client.get(
        f"{path}?beforeSeq={latest['nextBeforeSeq']}&limit=20",
        cookies=cookie,
    )
    assert middle_response.status == 200
    middle = await middle_response.json()
    assert middle["hasMore"] is True
    assert middle["nextBeforeSeq"] == 6
    assert [event["seq"] for event in middle["events"]] == list(range(6, 26))
    assert middle["events"][-1]["summary"] == "调用工具 Read"

    oldest_response = await web_env.client.get(
        f"{path}?beforeSeq={middle['nextBeforeSeq']}&limit=20",
        cookies=cookie,
    )
    oldest = await oldest_response.json()
    assert oldest["hasMore"] is False
    assert oldest["nextBeforeSeq"] == 0
    assert [event["seq"] for event in oldest["events"]] == list(range(1, 6))

    invalid = await web_env.client.get(f"{path}?beforeSeq=nope", cookies=cookie)
    assert invalid.status == 400
    wrong_conversation = await web_env.client.get(
        f"/api/conversations/{other['conversation_uuid']}/agents/{task_uuid}/events",
        cookies=cookie,
    )
    assert wrong_conversation.status == 404


async def test_agent_message_updates_do_not_move_or_rename_root_agent_card(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent continuation stable card")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    task_uuid = "task-stable-card"

    await live.publish({"type": "accepted", "turnUuid": "turn-root"})
    await live.publish({"type": "user", "turnUuid": "turn-root", "messageUuid": "msg-root", "text": "并行审查"})
    await live.publish({"type": "tool_start", "turnUuid": "turn-root", "toolCallId": "call-root", "name": "Agent", "arguments": "{\"prompt\":\"review\"}"})
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-root",
        "toolCallId": "call-root",
        "name": "Agent",
        "arguments": "{\"prompt\":\"review\"}",
        "payload": {"toolName": "Agent", "status": "running", "detached": True, "taskUuid": task_uuid, "task": {"taskUuid": task_uuid, "status": "running", "currentStatus": "执行中"}},
    })

    await live.publish({"type": "accepted", "turnUuid": "turn-notify", "taskNotification": True, "taskNotificationSilent": True, "hidden": True})
    await live.publish({"type": "task_notification", "turnUuid": "turn-notify", "taskUuid": task_uuid, "status": "needs_openbear_control", "summary": "需要继续", "hidden": True})
    await live.publish({"type": "tool_start", "turnUuid": "turn-notify", "toolCallId": "call-follow", "name": "AgentMessage", "arguments": "{\"to\":\"task-stable-card\"}"})
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-notify",
        "toolCallId": "call-follow",
        "name": "AgentMessage",
        "arguments": "{\"to\":\"task-stable-card\"}",
        "payload": {"toolName": "AgentMessage", "status": "completed", "detached": True, "taskUuid": task_uuid, "task": {"taskUuid": task_uuid, "status": "completed", "currentStatus": "任务完成"}},
    })
    await live.publish({"type": "final", "turnUuid": "turn-notify", "text": "内部总结"})

    state = await web_env.server._chat_payload(chat_id, row)
    ops = {op["opId"]: op for op in state["operations"]}
    agent = ops[f"agent:{task_uuid}"]
    assert agent["turnUuid"] == "turn-root"
    assert agent["runId"] == "turn-root"
    assert agent["payload"]["rootToolName"] == "Agent"
    assert agent["payload"]["toolName"] == "Agent"
    assert agent["payload"]["lastControlToolName"] == "AgentMessage"
    assert agent["payload"]["lastControlArguments"] == "{\"to\":\"task-stable-card\"}"
    assert agent["payload"]["task"]["currentStatus"] == "任务完成"
    internal_assistant = next(op for op in state["operations"] if op["opId"].startswith("assistant:turn-notify:"))
    assert internal_assistant["internal"] is True
    assert internal_assistant["payload"]["internal"] is True
    assert internal_assistant["payload"]["hidden"] is True


async def test_web_state_reconciles_terminal_agent_even_when_conversation_row_still_running(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent stale running reconcile")
    chat_id = int(row["internal_chat_id"])
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        workflow_uuid="wf-test",
        title="simple agent",
        input_data={},
        status="running",
        task_uuid="task-reconcile-done",
    )
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-agent-reconcile"})
    await live.publish({"type": "user", "turnUuid": "turn-agent-reconcile", "messageUuid": "msg-agent-reconcile", "text": "跑 Agent"})
    await live.publish({"type": "tool_start", "turnUuid": "turn-agent-reconcile", "toolCallId": "call-agent-reconcile", "name": "Agent", "arguments": "{}"})
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-agent-reconcile",
        "toolCallId": "call-agent-reconcile",
        "name": "Agent",
        "arguments": "{}",
        "payload": {"status": "running", "detached": True, "task": {"taskUuid": task_uuid, "status": "running", "currentStatus": "模型调用中"}},
    })
    await live.publish({"type": "done", "turnUuid": "turn-agent-reconcile"})
    await web_env.server.rath_dao.update_task(
        task_uuid,
        status="completed",
        current_status="任务完成",
        output={"summary": "done"},
        finish=True,
    )
    await web_env.server._touch_web_conversation(
        row["conversation_uuid"],
        status="running",
        current_status="Agent 后台执行中",
        last_error="",
    )
    row = await web_env.server._conversation_row(123, row["conversation_uuid"], require=True)

    state = await web_env.server._chat_payload(chat_id, row)

    agent = {op["opId"]: op for op in state["operations"]}[f"agent:{task_uuid}"]
    assert agent["status"] == "completed"
    assert agent["lifecycle"] == "terminal"
    assert agent["payload"]["task"]["status"] == "completed"
    assert state["facts"]["activeBackgroundAgentOpIds"] == []
    assert state["conversation"]["running"] is False


async def test_web_list_reconciles_stale_running_row_without_live_runtime(web_env):
    row = await web_env.server._create_web_conversation(123, title="stale controller row")
    conv_uuid = row["conversation_uuid"]
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-stale", "runUuid": "exec-stale"})
    await live.publish({"type": "status", "turnUuid": "turn-stale", "status": "正在思考 …"})
    # Simulate a fresh process: no live object/run registry fact survived, only DB.
    web_env.server._web_live_streams.pop(conv_uuid, None)
    await web_env.server._touch_web_conversation(conv_uuid, status="running", current_status="正在思考")

    listed = await web_env.server._list_web_conversations(123)

    item = next(item for item in listed if item["conversationUuid"] == conv_uuid)
    assert item["running"] is False
    assert item["status"] == "idle"
    assert item["currentStatus"] == "已中断（运行状态恢复）"
    operations = {op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)}
    assert operations["run:exec-stale"]["status"] == "interrupted"
    assert operations["run:exec-stale"]["lifecycle"] == "terminal"
    assert operations["status:exec-stale"]["status"] == "interrupted"
    assert operations["status:exec-stale"]["lifecycle"] == "terminal"


async def test_web_list_reconciles_stale_agent_control_operation(web_env):
    row = await web_env.server._create_web_conversation(123, title="stale agent control")
    conv_uuid = row["conversation_uuid"]
    await web_env.server._publish_operation(
        conv_uuid,
        internal_chat_id=int(row["internal_chat_id"]),
        op_id="agent_control:stale-message",
        op_type="agent_control",
        action="start",
        turn_uuid="turn-stale-control",
        payload={"toolName": "AgentMessage", "status": "running", "taskUuid": "task-finished"},
        status="running",
        lifecycle="active",
    )
    await web_env.db.conn.commit()

    listed = await web_env.server._list_web_conversations(123)

    item = next(entry for entry in listed if entry["conversationUuid"] == conv_uuid)
    assert item["running"] is False
    assert item["status"] == "idle"
    operation = {
        op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)
    }["agent_control:stale-message"]
    assert operation["status"] == "completed"
    assert operation["lifecycle"] == "terminal"


async def test_informational_stats_update_does_not_refresh_stale_active_runtime(web_env):
    row = await web_env.server._create_web_conversation(123, title="stale active freshness")
    conv_uuid = row["conversation_uuid"]
    await web_env.server._publish_operation(
        conv_uuid,
        internal_chat_id=int(row["internal_chat_id"]),
        op_id="agent_control:old-active",
        op_type="agent_control",
        action="start",
        turn_uuid="turn-old-active",
        payload={"toolName": "AgentMessage", "status": "running"},
        status="running",
        lifecycle="active",
    )
    await web_env.server._publish_operation(
        conv_uuid,
        internal_chat_id=int(row["internal_chat_id"]),
        op_id="stats:newer",
        op_type="stats",
        action="snapshot",
        turn_uuid="turn-old-active",
        payload={"modelCalls": 1},
        lifecycle="informational",
    )
    await web_env.db.conn.execute(
        "UPDATE web_operations SET updated_at_ms=100000 WHERE conversation_uuid=? AND op_id=?",
        (conv_uuid, "agent_control:old-active"),
    )
    await web_env.db.conn.execute(
        "UPDATE web_operations SET updated_at_ms=300000 WHERE conversation_uuid=? AND op_id=?",
        (conv_uuid, "stats:newer"),
    )
    await web_env.db.conn.execute(
        "UPDATE web_conversations SET status='idle', current_status='就绪', updated_at=200 WHERE conversation_uuid=?",
        (conv_uuid,),
    )
    await web_env.db.conn.commit()

    facts = (await web_env.server._web_operation_facts_for_conversations([conv_uuid]))[conv_uuid]
    rendered = web_env.server._web_conversation_json(
        {**row, "status": "idle", "current_status": "就绪", "updated_at": 200},
        operation_facts=facts,
    )

    assert facts["activeCount"] == 1
    assert facts["latestUpdatedAtMs"] == 300000
    assert facts["latestActiveUpdatedAtMs"] == 100000
    assert rendered["running"] is False
    assert rendered["status"] == "idle"


async def test_web_operations_done_closes_active_status_and_reasoning(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation done cleanup")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-done"})
    await live.publish({"type": "user", "turnUuid": "turn-done", "messageUuid": "msg-done", "text": "多段思考"})
    await live.publish({"type": "status", "turnUuid": "turn-done", "status": "正在思考 …"})
    await live.publish({"type": "delta", "turnUuid": "turn-done", "eventKey": "assistant:draft:0", "reasoning": "第一段思考"})
    await live.publish({"type": "tool_start", "turnUuid": "turn-done", "toolCallId": "call-read", "name": "Read", "arguments": "{}", "line": "Read: running"})
    await live.publish({"type": "tool_result", "turnUuid": "turn-done", "toolCallId": "call-read", "name": "Read", "arguments": "{}", "result": "ok"})
    await live.publish({"type": "delta", "turnUuid": "turn-done", "eventKey": "assistant:draft:1", "reasoning": "第二段思考", "text": "草稿"})
    await live.publish({"type": "final", "turnUuid": "turn-done", "eventKey": "assistant:draft:1", "text": "最终答案"})
    await live.publish({"type": "done", "turnUuid": "turn-done"})

    state = await web_env.server._chat_payload(chat_id, row)
    ops = {op["opId"]: op for op in state["operations"]}
    assert ops["status:turn-done"]["lifecycle"] == "terminal"
    assert ops["status:turn-done"]["payload"]["active"] is False
    assert ops["run:turn-done"]["status"] == "completed"
    reasoning_ops = [op for op in ops.values() if op["opType"] == "reasoning"]
    assert reasoning_ops
    assert all(op["lifecycle"] == "terminal" for op in reasoning_ops)
    assert all(op["payload"].get("complete") is True for op in reasoning_ops)
    assert state["facts"]["activeOperationIds"] == []


async def test_agent_wait_without_active_agents_returns_hidden_terminal_snapshot(web_env, monkeypatch):
    cfg = _cfg()
    backend = FakeStreamBackend([
        [StreamEvent(kind="tool_call", tool_calls=[
            ToolCall(id="wait-empty", name="AgentWait", arguments='{"mode":"event_only"}'),
        ]), StreamEvent(kind="finish", finish_reason="tool_calls")],
        [StreamEvent(kind="content", text="没有待等待的 Agent。"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=1000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    tools = ToolRegistry()

    async def _agent_wait(args):
        ctx = current_tool_context()
        assert ctx.agent_wait is not None
        return await ctx.agent_wait(args)

    tools.add("AgentWait", "wait", {"type": "object", "properties": {}}, _agent_wait)
    web_env.server.tools = tools

    async def _fake_system_prompt():
        return "sys"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    row = await web_env.server._create_web_conversation(123, title="empty agent wait", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live)
    await live.publish({"type": "accepted", "turnUuid": "turn-empty-wait"})

    delivered = await web_env.server._run_web_turn(
        chat_id,
        "确认后台任务",
        renderer,
        conversation=row,
        root_turn_uuid="turn-empty-wait",
    )

    assert delivered is True
    wait_result = next(
        message for message in backend.seen_convos[1]
        if message.get("role") == "tool" and message.get("tool_call_id") == "wait-empty"
    )
    payload = json.loads(wait_result["content"])
    assert payload["alreadyTerminal"] is True
    assert payload["skipped"] is True
    operations = await web_env.server._web_operations(row["conversation_uuid"])
    assert [op for op in operations if op["opType"] == "agent_supervision"] == []


async def test_agent_wait_plan_notification_wakes_immediately_and_requeues_if_undecided(web_env, monkeypatch):
    cfg = _cfg()
    backend = FakeStreamBackend([
        [
            StreamEvent(kind="tool_call", tool_calls=[
                ToolCall(
                    id="wait-plan-review",
                    name="AgentWait",
                    arguments='{"mode":"review_after","reviewAfterSeconds":3600}',
                ),
            ]),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [StreamEvent(kind="content", text="我暂不审批。"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    tools = ToolRegistry()

    async def _agent_wait(args):
        ctx = current_tool_context()
        assert ctx.agent_wait is not None
        return await ctx.agent_wait(args)

    tools.add("AgentWait", "wait", {"type": "object", "properties": {}}, _agent_wait, preserve_result=True)
    web_env.server.tools = tools

    async def _fake_system_prompt():
        return "sys"

    recovered: list[str] = []

    async def _fake_recover(conversation_uuid="", **_kwargs):
        recovered.append(conversation_uuid)

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    monkeypatch.setattr(web_env.server, "_recover_web_task_notifications", _fake_recover)
    row = await web_env.server._create_web_conversation(123, title="plan wait wake", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    root_turn_uuid = "turn-plan-wake"
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-plan-wake",
        title="plan wake task",
        status="running",
        turn_uuid=root_turn_uuid,
        run_root_turn_uuid=root_turn_uuid,
    )
    coordinator = AgentPlanCoordinator(
        web_env.server.rath_dao,
        RathTaskManager(web_env.server.rath_dao),
    )
    plan = {
        "title": "Wake plan",
        "objective": "Wake AgentWait immediately",
        "scope": {"included": ["wake"], "excluded": []},
        "assumptions": [],
        "steps": [{
            "id": "s1",
            "title": "Wake",
            "objective": "Wake controller",
            "method": "Deliver durable notification",
            "dependsOn": [],
            "required": True,
            "criteria": [{"id": "c1", "description": "Controller woke", "required": True}],
            "expectedEvidence": ["AgentWait snapshot"],
        }],
        "finalOutputs": [{
            "id": "o1",
            "title": "Wake result",
            "description": "Wake verified",
            "supportedBy": ["s1"],
        }],
        "risks": [],
    }
    await coordinator.submit_plan(
        task_uuid,
        plan,
        request_id="submit-plan-wake",
        wait_for_decision=False,
    )
    queued = await web_env.server._persist_web_task_notification(row, {
        "kind": "plan-approval-required",
        "notificationKey": f"{row['conversation_uuid']}:{task_uuid}:1:plan-approval-required",
        "requiresDecision": True,
        "taskUuid": task_uuid,
        "status": "awaiting_plan_decision",
        "expectedPlanVersion": 1,
        "summary": "Plan requires approval",
        "content": "review plan",
        "runRootTurnUuid": root_turn_uuid,
    })
    second_task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-plan-wake",
        title="second plan wake task",
        status="running",
        turn_uuid=root_turn_uuid,
        run_root_turn_uuid=root_turn_uuid,
    )
    await coordinator.submit_plan(
        second_task_uuid,
        {**plan, "title": "Second wake plan"},
        request_id="submit-second-plan-wake",
        wait_for_decision=False,
    )
    second_queued = await web_env.server._persist_web_task_notification(row, {
        "kind": "plan-approval-required",
        "notificationKey": (
            f"{row['conversation_uuid']}:{second_task_uuid}:1:plan-approval-required"
        ),
        "requiresDecision": True,
        "taskUuid": second_task_uuid,
        "status": "awaiting_plan_decision",
        "expectedPlanVersion": 1,
        "summary": "Second Plan requires approval",
        "content": "review second plan",
        "runRootTurnUuid": root_turn_uuid,
    })
    assert queued is not None and second_queued is not None
    web_env.server._web_controller_notifications[row["conversation_uuid"]] = [queued, second_queued]
    web_env.server._web_task_notification_pending[row["conversation_uuid"]] = [queued, second_queued]

    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live)
    await live.publish({"type": "accepted", "turnUuid": root_turn_uuid})
    delivered = await web_env.server._run_web_turn(
        chat_id,
        "等待 Plan",
        renderer,
        conversation=row,
        root_turn_uuid=root_turn_uuid,
    )

    assert delivered is True
    wait_result = next(
        message
        for message in backend.seen_convos[1]
        if message.get("role") == "tool" and message.get("tool_call_id") == "wait-plan-review"
    )
    payload = json.loads(str(wait_result["content"]))
    assert payload["wakeReason"] == "task_notification"
    assert len(payload["notifications"]) == 2
    assert all(item["kind"] == "plan-approval-required" for item in payload["notifications"])
    assert len(payload["agents"]) == 2
    plan_runtime = payload["agents"][0]["planRuntime"]
    assert plan_runtime["pendingPlanVersion"] == 1
    assert plan_runtime["plan"]["objective"] == "Wake AgentWait immediately"
    assert plan_runtime["steps"][0]["criteria"][0]["id"] == "c1"
    assert plan_runtime["remainingSteps"] == 1
    assert recovered == []
    cur = await web_env.db.conn.execute(
        "SELECT state,attempts FROM web_task_notifications WHERE notification_uuid IN (?,?) ORDER BY id",
        (queued["_notificationUuid"], second_queued["_notificationUuid"]),
    )
    notification_rows = await cur.fetchall()
    assert [item["state"] for item in notification_rows] == ["pending", "pending"]
    assert [int(item["attempts"] or 0) for item in notification_rows] == [1, 1]
    assert web_env.server._web_task_notification_pending.get(row["conversation_uuid"]) in (None, [])


async def test_agent_wait_user_interruption_returns_stable_instruction_id(web_env, monkeypatch):
    cfg = _cfg()
    backend = FakeStreamBackend([
        [
            StreamEvent(kind="tool_call", tool_calls=[
                ToolCall(id="wait-user-ruling", name="AgentWait", arguments='{"mode":"event_only"}'),
            ]),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [StreamEvent(kind="content", text="已收到用户裁决。"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    tools = ToolRegistry()

    async def _agent_wait(args):
        ctx = current_tool_context()
        assert ctx.agent_wait is not None
        return await ctx.agent_wait(args)

    tools.add("AgentWait", "wait", {"type": "object", "properties": {}}, _agent_wait, preserve_result=True)
    web_env.server.tools = tools

    async def _fake_system_prompt():
        return "sys"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    row = await web_env.server._create_web_conversation(123, title="user plan ruling", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    root_turn_uuid = "turn-user-ruling"
    await web_env.server.rath_dao.create_task(
        chat_id=chat_id,
        parent_session_uuid=row["conversation_uuid"],
        workflow_uuid="wf-user-ruling",
        title="wait user ruling",
        status="running",
        turn_uuid=root_turn_uuid,
        run_root_turn_uuid=root_turn_uuid,
    )
    steering.clear(chat_id)
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live)
    await live.publish({"type": "accepted", "turnUuid": root_turn_uuid})
    run = asyncio.create_task(web_env.server._run_web_turn(
        chat_id,
        "等待用户裁决",
        renderer,
        conversation=row,
        root_turn_uuid=root_turn_uuid,
    ))
    try:
        for _ in range(200):
            wake = web_env.server._web_controller_wake_events.get(row["conversation_uuid"])
            if wake is not None:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("controller AgentWait was not registered")
        item = steering.enqueue(
            chat_id,
            "用户决定继续，但缩小范围",
            source="web",
            turnUuid="turn-new-user-ruling",
            messageUuid="msg-new-user-ruling",
        )
        assert item is not None
        wake.set()
        assert await asyncio.wait_for(run, timeout=3) is True
    finally:
        steering.clear(chat_id)
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    wait_result = next(
        message
        for message in backend.seen_convos[1]
        if message.get("role") == "tool" and message.get("tool_call_id") == "wait-user-ruling"
    )
    payload = json.loads(str(wait_result["content"]))
    assert payload["wakeReason"] == "user_interruption"
    assert payload["userInstructionId"] == item["id"]
    assert payload["userInstructionIds"] == [item["id"]]


async def test_agent_wait_event_only_wakes_when_last_sibling_terminal_notification_is_durable(
    web_env,
    monkeypatch,
):
    cfg = _cfg()
    backend = FakeStreamBackend([
        [
            StreamEvent(kind="tool_call", tool_calls=[
                ToolCall(id="wait-two-results", name="AgentWait", arguments='{"mode":"event_only"}'),
            ]),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [StreamEvent(kind="content", text="两个 Agent 结果已统一汇总。"), StreamEvent(kind="finish", finish_reason="stop")],
    ])
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    tools = ToolRegistry()

    async def _agent_wait(args):
        ctx = current_tool_context()
        assert ctx.agent_wait is not None
        return await ctx.agent_wait(args)

    tools.add("AgentWait", "wait", {"type": "object", "properties": {}}, _agent_wait, preserve_result=True)
    web_env.server.tools = tools

    async def _fake_system_prompt():
        return "sys"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    # The production worker waits on RunRegistry. This test exercises the live
    # same-root controller bridge directly and keeps the fallback worker inert.
    monkeypatch.setattr(web_env.server, "_ensure_web_task_notification_worker", lambda *_a, **_k: None)

    row = await web_env.server._create_web_conversation(123, title="last sibling wakes event-only", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    conversation_uuid = str(row["conversation_uuid"])
    root_turn_uuid = "turn-last-sibling-wake"
    task_uuids = []
    for index in range(2):
        task_uuids.append(await web_env.server.rath_dao.create_task(
            chat_id=chat_id,
            parent_session_uuid=conversation_uuid,
            workflow_uuid="wf-last-sibling-wake",
            title=f"sibling {index + 1}",
            status="running",
            turn_uuid=root_turn_uuid,
            run_root_turn_uuid=root_turn_uuid,
        ))

    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live)
    await live.publish({"type": "accepted", "turnUuid": root_turn_uuid})
    run = asyncio.create_task(web_env.server._run_web_turn(
        chat_id,
        "等待两个 Agent",
        renderer,
        conversation=row,
        root_turn_uuid=root_turn_uuid,
    ))
    try:
        for _ in range(300):
            operations = await web_env.server._web_operations(conversation_uuid)
            if any(
                operation["opType"] == "agent_supervision" and operation["lifecycle"] == "active"
                for operation in operations
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("AgentWait did not enter event_only supervision")

        await web_env.server.rath_dao.update_task(
            task_uuids[0],
            status="completed",
            current_status="任务完成",
            output={"summary": "first result"},
            finish=True,
            expected_statuses=("running",),
        )
        # Simulate the strongest fallback: only the SQLite terminal trigger has
        # persisted the result; the richer Python completion callback is absent.
        await web_env.server._recover_web_task_notifications(conversation_uuid)
        await asyncio.sleep(0.05)
        assert run.done() is False

        await web_env.server.rath_dao.update_task(
            task_uuids[1],
            status="completed",
            current_status="任务完成",
            output={"summary": "second result"},
            finish=True,
            expected_statuses=("running",),
        )
        await web_env.server._recover_web_task_notifications(conversation_uuid)

        assert await asyncio.wait_for(run, timeout=3) is True
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    wait_result = next(
        message
        for message in backend.seen_convos[1]
        if message.get("role") == "tool" and message.get("tool_call_id") == "wait-two-results"
    )
    payload = json.loads(str(wait_result["content"]))
    assert payload["wakeReason"] == "task_notification"
    assert payload["summary"] == {"running": 0, "waitingControl": 0, "terminal": 2, "total": 2}
    assert {item["taskUuid"] for item in payload["notifications"]} == set(task_uuids)
    assert web_env.server._web_task_notification_pending.get(conversation_uuid) in (None, [])

    cur = await web_env.db.conn.execute(
        "SELECT state FROM web_task_notifications WHERE conversation_uuid=? AND task_uuid IN (?,?)",
        (conversation_uuid, *task_uuids),
    )
    notification_states = [str(item["state"] or "") for item in await cur.fetchall()]
    assert len(notification_states) >= 2
    assert set(notification_states) == {"delivered"}
    assert backend.calls == 2


async def test_same_root_agent_notification_model_failure_rearms_durable_worker(web_env, monkeypatch):
    cfg = _cfg()

    class FailAfterWaitBackend(FakeStreamBackend):
        async def stream(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
            self.seen_convos.append([dict(message) for message in messages])
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(kind="tool_call", tool_calls=[
                    ToolCall(id="wait-result", name="AgentWait", arguments='{"mode":"event_only"}'),
                ])
                yield StreamEvent(kind="finish", finish_reason="tool_calls")
                return
            raise OpenBearLLMError("forced controller failure", status=400, retryable=False)
            yield  # pragma: no cover

    backend = FailAfterWaitBackend()
    web_env.server.config = cfg
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    tools = ToolRegistry()

    async def _agent_wait(args):
        ctx = current_tool_context()
        assert ctx.agent_wait is not None
        return await ctx.agent_wait(args)

    tools.add("AgentWait", "wait", {"type": "object", "properties": {}}, _agent_wait, preserve_result=True)
    web_env.server.tools = tools

    async def _fake_system_prompt():
        return "sys"

    recovered = []

    async def _fake_recover(conversation_uuid="", **_kwargs):
        recovered.append(conversation_uuid)

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    monkeypatch.setattr(web_env.server, "_recover_web_task_notifications", _fake_recover)

    row = await web_env.server._create_web_conversation(123, title="same root retry", model="openai/gpt")
    queued = await web_env.server._persist_web_task_notification(row, {
        "kind": "task-notification",
        "taskUuid": "task-same-root-failure",
        "status": "completed",
        "summary": "完整结果待汇总",
        "content": "完整结果正文",
        "resultOutputTokens": 900,
        "resultCount": 1,
    })
    assert queued is not None
    web_env.server._web_controller_notifications[row["conversation_uuid"]] = [queued]

    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live)
    await live.publish({"type": "accepted", "turnUuid": "turn-same-root-failure"})
    delivered = await web_env.server._run_web_turn(
        int(row["internal_chat_id"]),
        "等待 Agent",
        renderer,
        conversation=row,
        root_turn_uuid="turn-same-root-failure",
    )

    assert delivered is False
    assert recovered == [row["conversation_uuid"]]
    cur = await web_env.db.conn.execute(
        "SELECT state FROM web_task_notifications WHERE notification_uuid=?",
        (queued["_notificationUuid"],),
    )
    assert str((await cur.fetchone())["state"]) == "pending"


async def test_agent_supervision_keeps_each_wait_cycle_in_timeline_order(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent supervision op")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "root-supervision"})
    await live.publish({
        "type": "agent_supervision",
        "turnUuid": "root-supervision",
        "runRootTurnUuid": "root-supervision",
        "waitCycleUuid": "wait-180",
        "statusText": "计划 180 秒后统一复查 Agent",
        "preview": "健康推进",
        "mode": "review_after",
        "reviewAfterSeconds": 180,
        "active": True,
        "status": "running",
    })
    await live.publish({
        "type": "agent_supervision",
        "turnUuid": "root-supervision",
        "runRootTurnUuid": "root-supervision",
        "waitCycleUuid": "wait-420",
        "statusText": "计划 420 秒后统一复查 Agent",
        "preview": "继续健康推进",
        "mode": "review_after",
        "reviewAfterSeconds": 420,
        "active": True,
        "status": "running",
    })

    ops = await web_env.server._web_operations(row["conversation_uuid"])
    supervision = [op for op in ops if op["opType"] == "agent_supervision"]
    assert len(supervision) == 2
    assert [op["opId"] for op in supervision] == [
        "agent-supervision:wait-180",
        "agent-supervision:wait-420",
    ]
    assert supervision[0]["payload"]["reviewAfterSeconds"] == 180
    assert supervision[1]["payload"]["reviewAfterSeconds"] == 420
    assert supervision[0]["displaySeq"] < supervision[1]["displaySeq"]
    assert all(op["lifecycle"] == "active" for op in supervision)

    await live.publish({"type": "done", "turnUuid": "root-supervision"})
    ops = await web_env.server._web_operations(row["conversation_uuid"])
    supervision = [op for op in ops if op["opType"] == "agent_supervision"]
    assert all(op["lifecycle"] == "terminal" for op in supervision)
    assert all(op["payload"]["active"] is False for op in supervision)


async def test_agent_wait_after_completed_answer_starts_new_assistant_operation(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent wait answer boundary")
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live=live)

    await live.publish({"type": "accepted", "turnUuid": "turn-answer-wait"})
    await renderer.on_delta("完整正式结论")
    await renderer.finalize("完整正式结论")
    await renderer.on_tool_start("call-wait", "AgentWait", '{"mode":"event_only"}')
    await renderer.on_delta("后台审计已全部收尾")
    await renderer.finalize("后台审计已全部收尾")

    ops = await web_env.server._web_operations(row["conversation_uuid"])
    assistants = [op for op in ops if op["opType"] == "assistant_message"]
    assert [op["opId"] for op in assistants] == [
        "assistant:turn-answer-wait:0",
        "assistant:turn-answer-wait:1",
    ]
    assert assistants[0]["payload"]["text"] == "完整正式结论"
    assert assistants[1]["payload"]["text"] == "后台审计已全部收尾"
    assert all(op["lifecycle"] == "terminal" for op in assistants)


async def test_repeated_agent_wait_cut_cannot_revise_terminal_reasoning(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent wait idempotent cut")
    live = web_env.server._live_for(row)
    renderer = _WebStreamRenderer(live=live)

    await live.publish({"type": "accepted", "turnUuid": "turn-repeated-wait"})
    await renderer.on_delta("", reasoning="已经完成的思考过程")
    await renderer.finalize("", reasoning="已经完成的思考过程")
    before = {
        op["opId"]: op
        for op in await web_env.server._web_operations(row["conversation_uuid"])
    }["reasoning:turn-repeated-wait:0"]

    await renderer.on_tool_start("call-wait-1", "AgentWait", '{"mode":"event_only"}')
    await renderer.on_tool_start("call-wait-2", "AgentWait", '{"mode":"event_only"}')

    after = {
        op["opId"]: op
        for op in await web_env.server._web_operations(row["conversation_uuid"])
    }["reasoning:turn-repeated-wait:0"]
    assert before["terminalAtMs"] == before["updatedAtMs"]
    assert before["payload"]["terminalAtMs"] == before["updatedAtMs"]
    assert after["revision"] == before["revision"]
    assert after["updatedAtMs"] == before["updatedAtMs"]
    assert after["terminalAtMs"] == before["terminalAtMs"]
    assert after["payload"]["text"] == "已经完成的思考过程"
    assert live.draft_reasoning == ""

    await renderer.on_delta("后台 Agent 已全部完成")
    await renderer.finalize("后台 Agent 已全部完成")
    ops = await web_env.server._web_operations(row["conversation_uuid"])
    assert {op["opId"] for op in ops if op["opType"] == "assistant_message"} == {
        "assistant:turn-repeated-wait:1",
    }


async def test_terminal_assistant_operation_rejects_late_append(web_env):
    row = await web_env.server._create_web_conversation(123, title="terminal assistant immutable")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-immutable"})
    await live.publish({
        "type": "final",
        "turnUuid": "turn-immutable",
        "eventKey": "assistant:draft:0",
        "text": "不可覆盖的正式结论",
    })
    before = {
        op["opId"]: op
        for op in await web_env.server._web_operations(row["conversation_uuid"])
    }["assistant:turn-immutable:0"]

    await live.publish({
        "type": "delta",
        "turnUuid": "turn-immutable",
        "eventKey": "assistant:draft:0",
        "text": "迟到消息",
    })

    after = {
        op["opId"]: op
        for op in await web_env.server._web_operations(row["conversation_uuid"])
    }["assistant:turn-immutable:0"]
    assert after["revision"] == before["revision"]
    assert after["payload"]["text"] == "不可覆盖的正式结论"
    assert after["lifecycle"] == "terminal"


async def test_terminal_agent_operation_rejects_late_revival_or_status_change(web_env):
    row = await web_env.server._create_web_conversation(123, title="terminal agent immutable")
    conv_uuid = row["conversation_uuid"]
    await web_env.server._publish_operation(
        conv_uuid,
        op_id="agent:immutable-task",
        op_type="agent",
        action="end",
        payload={"taskUuid": "immutable-task", "status": "completed", "summary": "最终结果"},
        status="completed",
        lifecycle="terminal",
    )
    before = {
        op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)
    }["agent:immutable-task"]

    revived = await web_env.server._publish_operation(
        conv_uuid,
        op_id="agent:immutable-task",
        op_type="agent",
        action="patch",
        payload={"status": "running", "summary": "迟到进度"},
        status="running",
        lifecycle="active",
    )
    cancelled = await web_env.server._publish_operation(
        conv_uuid,
        op_id="agent:immutable-task",
        op_type="agent",
        action="cancel",
        payload={"status": "cancelled"},
        status="cancelled",
        lifecycle="terminal",
    )
    after = {
        op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)
    }["agent:immutable-task"]
    assert revived is None
    assert cancelled is None
    assert after["revision"] == before["revision"]
    assert after["status"] == "completed"
    assert after["payload"]["summary"] == "最终结果"


async def test_agent_control_tool_never_creates_agent_task_card(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent control operation")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-agent-control"})
    await live.publish({
        "type": "tool_start",
        "turnUuid": "turn-agent-control",
        "toolCallId": "call-message",
        "name": "AgentMessage",
        "arguments": '{"to":"61040d2f","message":"收口"}',
    })
    await live.publish({
        "type": "tool_result",
        "turnUuid": "turn-agent-control",
        "toolCallId": "call-message",
        "name": "AgentMessage",
        "arguments": '{"to":"61040d2f","message":"收口"}',
        "result": '{"ok":true,"taskUuid":"61040d2f-14ca-46dc-a7b0-f6854033d40d","status":"running"}',
    })

    ops = await web_env.server._web_operations(row["conversation_uuid"])
    assert not [op for op in ops if op["opType"] == "agent"]
    controls = [op for op in ops if op["opType"] == "agent_control"]
    assert len(controls) == 1
    assert controls[0]["opId"] == "agent_control:call-message"
    assert controls[0]["lifecycle"] == "terminal"


async def test_agent_control_terminal_progress_still_accepts_first_tool_result(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent control result race")
    conv_uuid = row["conversation_uuid"]
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-control-result-race"})
    await live.publish({
        "type": "tool_start",
        "toolCallId": "call-control-result-race",
        "name": "AgentMessage",
        "arguments": '{"to":"task-1","message":"continue"}',
    })
    await live.publish({
        "type": "tool_progress",
        "toolCallId": "call-control-result-race",
        "name": "AgentMessage",
        "arguments": '{"to":"task-1","message":"continue"}',
        "payload": {"toolName": "AgentMessage", "status": "completed", "taskUuid": "task-1"},
    })
    await live.publish({
        "type": "tool_result",
        "toolCallId": "call-control-result-race",
        "name": "AgentMessage",
        "arguments": '{"to":"task-1","message":"continue"}',
        "result": '{"ok":true,"status":"completed","taskUuid":"task-1"}',
    })

    operation = {
        op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)
    }["agent_control:call-control-result-race"]
    assert operation["status"] == "completed"
    assert operation["lifecycle"] == "terminal"
    assert operation["payload"]["transcriptResult"] is True
    assert "resultText" in operation["payload"]


async def test_terminal_agent_control_ignores_late_progress(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent control terminal guard")
    conv_uuid = row["conversation_uuid"]
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-control-guard"})
    await live.publish({
        "type": "tool_start",
        "toolCallId": "call-control-guard",
        "name": "AgentMessage",
        "arguments": '{"to":"task-1","message":"continue"}',
    })
    await live.publish({
        "type": "tool_result",
        "toolCallId": "call-control-guard",
        "name": "AgentMessage",
        "arguments": '{"to":"task-1","message":"continue"}',
        "result": '{"ok":true,"status":"running","detached":true,"taskUuid":"task-1"}',
    })
    before = {
        op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)
    }["agent_control:call-control-guard"]

    await live.publish({
        "type": "tool_progress",
        "toolCallId": "call-control-guard",
        "name": "AgentMessage",
        "arguments": '{"to":"task-1","message":"continue"}',
        "payload": {"toolName": "AgentMessage", "status": "running", "detached": True, "taskUuid": "task-1"},
    })

    after = {
        op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)
    }["agent_control:call-control-guard"]
    assert before["status"] == "completed"
    assert before["lifecycle"] == "terminal"
    assert after["revision"] == before["revision"]
    assert after["status"] == "completed"
    assert after["lifecycle"] == "terminal"


async def test_publish_native_operations_writes_frames_and_operations(web_env):
    row = await web_env.server._create_web_conversation(123, title="native operation direct")
    conv_uuid = row["conversation_uuid"]
    payload = {
        "type": "accepted",
        "conversationUuid": conv_uuid,
        "chatId": row["internal_chat_id"],
        "turnUuid": "turn-direct",
    }

    frames = await web_env.server._publish_native_operations(
        payload,
        internal_chat_id=int(row["internal_chat_id"]),
        owner_chat_id=123,
        debug_source="direct_native_test",
    )
    await web_env.db.conn.commit()

    assert frames
    assert frames[0]["opId"] == "run:turn-direct"
    assert frames[0]["debug"]["source"] == "direct_native_test"
    ops = await web_env.server._web_operations(conv_uuid)
    assert {op["opId"] for op in ops} == {"run:turn-direct"}
    cur = await web_env.db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_timeline_events'")
    assert await cur.fetchone() is None


async def test_agent_tool_result_transcript_does_not_drive_agent_operation_status(web_env):
    row = await web_env.server._create_web_conversation(123, title="agent result transcript only")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-agent-result"})
    await live.publish({"type": "user", "turnUuid": "turn-agent-result", "messageUuid": "msg-agent-result", "text": "跑 Agent"})
    await live.publish({"type": "tool_start", "turnUuid": "turn-agent-result", "toolCallId": "call-agent-result", "name": "Agent", "arguments": "{}"})
    await live.publish({
        "type": "tool_result",
        "turnUuid": "turn-agent-result",
        "toolCallId": "call-agent-result",
        "name": "Agent",
        "arguments": "{}",
        "result": '{"status":"completed","task":{"status":"completed","currentStatus":"不应由 result 决定"}}',
        "durationMs": 5,
    })

    state = await web_env.server._chat_payload(chat_id, row)
    agent = {op["opId"]: op for op in state["operations"]}["agent:call-agent-result"]
    assert agent["status"] == "queued"
    assert agent["lifecycle"] == "active"
    assert agent["payload"]["resultText"].startswith('{"status":"completed"')
    assert "task" not in agent["payload"]


async def test_all_web_live_events_use_source_native_operation_specs(web_env):
    row = await web_env.server._create_web_conversation(123, title="native all specs")
    conv_uuid = row["conversation_uuid"]
    live = web_env.server._live_for(row)

    events = [
        {"type": "accepted", "turnUuid": "turn-all"},
        {"type": "user", "turnUuid": "turn-all", "messageUuid": "msg-all", "text": "问题"},
        {"type": "status", "turnUuid": "turn-all", "status": "正在处理"},
        {"type": "queued", "turnUuid": "turn-queued", "text": "补充", "status": "已追加到当前运行"},
        {"type": "task_notification", "turnUuid": "turn-all", "taskUuid": "task-all", "summary": "Agent 完成", "status": "completed"},
        {"type": "delta", "turnUuid": "turn-all", "eventKey": "assistant:draft:0", "text": "草稿"},
        {"type": "tool_start", "turnUuid": "turn-all", "toolCallId": "call-all", "name": "Read", "arguments": "{}", "line": "Read: running"},
        {"type": "tool_result", "turnUuid": "turn-all", "toolCallId": "call-all", "name": "Read", "arguments": "{}", "result": "ok"},
        {"type": "final", "turnUuid": "turn-all", "eventKey": "assistant:draft:1", "text": "答案"},
        {"type": "stats", "turnUuid": "turn-all", "stats": {"model": "openai/gpt", "costUsd": 0.01}},
        {"type": "notice", "turnUuid": "turn-all", "text": "提示"},
        {"type": "error", "turnUuid": "turn-error", "error": "boom"},
        {"type": "stopped", "turnUuid": "turn-stop", "reason": "已停止"},
        {"type": "done", "turnUuid": "turn-all"},
    ]
    for event in events:
        await live.publish(event)

    frames = await web_env.server._web_frames(conv_uuid)
    by_source = [frame for frame in frames if frame["debug"].get("source") == "source_native_operation_specs"]
    assert by_source
    assert {frame["opId"] for frame in by_source} >= {
        "run:turn-all",
        "msg:msg-all",
        "status:turn-all",
        "notice:task:task-all",
        "assistant:turn-all:0",
        "tool:call-all",
        "assistant:turn-all:1",
        "stats:turn-all",
        "assistant-error:turn-error",
    }
    assert not [frame for frame in by_source if str(frame["opId"]).startswith("turn:")]
    assert any(frame["opType"] == "run_control" and frame["debug"].get("source") == "source_native_operation_specs" for frame in by_source)
    assert all("_webOperationSpecs" not in json.dumps(frame.get("payload") or {}, ensure_ascii=False) for frame in frames)


async def test_assistant_events_use_source_native_operation_specs_without_hidden_payload(web_env):
    row = await web_env.server._create_web_conversation(123, title="native assistant specs")
    chat_id = int(row["internal_chat_id"])
    conv_uuid = row["conversation_uuid"]
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-native-assistant"})
    await live.publish({"type": "user", "turnUuid": "turn-native-assistant", "messageUuid": "msg-native-assistant", "text": "写答案"})
    await live.publish({"type": "delta", "turnUuid": "turn-native-assistant", "eventKey": "assistant:draft:0", "text": "草稿", "reasoning": "先想"})
    await live.publish({"type": "final", "turnUuid": "turn-native-assistant", "eventKey": "assistant:draft:0", "text": "最终", "reasoning": "想完", "footer": "页脚"})

    state = await web_env.server._chat_payload(chat_id, row)
    ops = {op["opId"]: op for op in state["operations"]}
    assert ops["assistant:turn-native-assistant:0"]["status"] == "completed"
    assert ops["assistant:turn-native-assistant:0"]["payload"]["text"] == "最终"
    assert ops["reasoning:turn-native-assistant:0"]["lifecycle"] == "terminal"
    assert ops["reasoning:turn-native-assistant:0"]["payload"]["text"] == "想完"
    frames = await web_env.server._web_frames(conv_uuid)
    assistant_frames = [frame for frame in frames if frame["opId"] == "assistant:turn-native-assistant:0"]
    reasoning_frames = [frame for frame in frames if frame["opId"] == "reasoning:turn-native-assistant:0"]
    assert {frame["debug"].get("source") for frame in assistant_frames + reasoning_frames} == {"source_native_operation_specs"}

    assert all("_webOperationSpecs" not in json.dumps(frame.get("payload") or {}, ensure_ascii=False) for frame in assistant_frames + reasoning_frames)


async def test_detached_agent_progress_does_not_split_foreground_assistant_segment(web_env):
    row = await web_env.server._create_web_conversation(123, title="detached agent progress")
    chat_id = int(row["internal_chat_id"])
    conv_uuid = row["conversation_uuid"]
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-detached-agent"})
    await live.publish({"type": "tool_start", "turnUuid": "turn-detached-agent", "toolCallId": "call-agent", "name": "Agent", "arguments": "{}", "line": "Agent running"})
    await live.publish({"type": "delta", "turnUuid": "turn-detached-agent", "text": "已启动后台 Agent"})
    await live.publish({
        "type": "tool_progress",
        "turnUuid": "turn-detached-agent",
        "toolCallId": "call-agent",
        "name": "Agent",
        "arguments": "{}",
        "payload": {
            "toolName": "Agent",
            "status": "running",
            "detached": True,
            "task": {"taskUuid": "task-1", "status": "running", "detached": True},
        },
    })
    await live.publish({"type": "delta", "turnUuid": "turn-detached-agent", "text": "已启动后台 Agent，等待结果"})
    await live.publish({"type": "final", "turnUuid": "turn-detached-agent", "text": "已启动后台 Agent，等待结果"})

    state = await web_env.server._chat_payload(chat_id, row)
    assistant_ops = [op for op in state["operations"] if str(op["opId"]).startswith("assistant:turn-detached-agent:")]
    assert [op["opId"] for op in assistant_ops] == ["assistant:turn-detached-agent:1"]
    assert assistant_ops[0]["payload"]["text"] == "已启动后台 Agent，等待结果"

    assistant_frames = [frame for frame in await web_env.server._web_frames(conv_uuid) if frame["debug"].get("eventType") in {"delta", "final"} and frame["opType"] == "assistant_message"]
    assert {frame["opId"] for frame in assistant_frames} == {"assistant:turn-detached-agent:1"}


async def test_tool_events_use_source_native_operation_specs_without_hidden_payload(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="native tool specs")
    chat_id = int(row["internal_chat_id"])
    conv_uuid = row["conversation_uuid"]
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-native"})
    await live.publish({"type": "user", "turnUuid": "turn-native", "messageUuid": "msg-native", "text": "跑工具"})
    await live.publish({"type": "tool_start", "turnUuid": "turn-native", "toolCallId": "call-native", "name": "Bash", "arguments": "{}", "line": "Bash: running"})
    await live.publish({"type": "tool_result", "turnUuid": "turn-native", "toolCallId": "call-native", "name": "Bash", "arguments": "{}", "result": "ok", "durationMs": 7})

    state = await web_env.server._chat_payload(chat_id, row)
    tool = {op["opId"]: op for op in state["operations"]}["tool:call-native"]
    assert tool["status"] == "completed"
    assert tool["detailAvailable"] is True
    assert tool["detailLoaded"] is False
    assert tool["payload"]["resultState"] == "ok"
    # `_chat_payload()` adds legacy ContextCompaction rows after `_web_operations()`
    # has already summary-projected persisted tools. The existing Bash summary
    # must not be serialized a second time, otherwise this source disappears.
    assert tool["payload"]["previewArguments"] == "{}"
    assert "args" not in tool["payload"]
    assert "arguments" not in tool["payload"]
    assert "result" not in tool["payload"]
    assert state["messages"] == []

    full = {op["opId"]: op for op in await web_env.server._web_operations(conv_uuid)}["tool:call-native"]
    assert full["detailLoaded"] is True
    assert full["payload"]["result"] == "ok"

    detail_response = await web_env.client.get(
        f"/api/conversations/{conv_uuid}/operations/tool%3Acall-native/detail",
        cookies={"openbear_web_session": cookie},
    )
    assert detail_response.status == 200
    detail = (await detail_response.json())["operation"]
    assert detail["detailLoaded"] is True
    assert detail["payload"]["arguments"] == "{}"
    assert detail["payload"]["result"] == "ok"

    missing_detail = await web_env.client.get(
        f"/api/conversations/{conv_uuid}/operations/tool%3Adoes-not-exist/detail",
        cookies={"openbear_web_session": cookie},
    )
    assert missing_detail.status == 404
    frames = await web_env.server._web_frames(conv_uuid)
    tool_frames = [frame for frame in frames if frame["opId"] == "tool:call-native"]
    assert [frame["action"] for frame in tool_frames] == ["start", "end"]
    assert {frame["debug"].get("source") for frame in tool_frames} == {"source_native_operation_specs"}

    assert all("_webOperationSpecs" not in json.dumps(frame.get("payload") or {}, ensure_ascii=False) for frame in tool_frames)


async def test_web_operations_api_reconciles_stale_active_operations(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="operations reconcile api")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-ops-reconcile"})
    await live.publish({"type": "user", "turnUuid": "turn-ops-reconcile", "messageUuid": "msg-ops-reconcile", "text": "会留下活跃状态"})
    await live.publish({"type": "status", "turnUuid": "turn-ops-reconcile", "status": "正在处理"})
    live.status = "idle"
    await web_env.server._touch_web_conversation(row["conversation_uuid"], status="idle", current_status="就绪")

    resp = await web_env.client.get(
        f"/api/conversations/{row['conversation_uuid']}/operations",
        cookies={"openbear_web_session": cookie},
    )

    assert resp.status == 200
    ops = {op["opId"]: op for op in (await resp.json())["operations"]}
    assert ops["run:turn-ops-reconcile"]["lifecycle"] == "terminal"
    assert ops["status:turn-ops-reconcile"]["lifecycle"] == "terminal"


async def test_web_operation_tail_pages_are_sql_bounded_and_include_active_extras(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="operation tail page")
    conv_uuid = str(row["conversation_uuid"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-active-page"})

    for index in range(6):
        async with web_env.server._web_operation_lock(conv_uuid):
            await web_env.server._publish_operation(
                conv_uuid,
                internal_chat_id=int(row["internal_chat_id"]),
                owner_chat_id=123,
                op_id=f"notice:page-{index}",
                op_type="notice",
                action="create",
                turn_uuid=f"turn-page-{index}",
                payload={"text": f"page {index}"},
                status="completed",
                lifecycle="informational",
                source="test",
            )

    full = await web_env.server._web_operations(conv_uuid, include_tool_details=False)
    page, meta = await web_env.server._web_operations_page(
        conv_uuid,
        limit=2,
        include_tool_details=False,
    )
    active = next(operation for operation in full if operation["opId"] == "run:turn-active-page")
    expected_tail = full[-2:]

    assert [operation["displaySeq"] for operation in page] == sorted(operation["displaySeq"] for operation in page)
    assert {operation["opId"] for operation in expected_tail}.issubset({operation["opId"] for operation in page})
    assert active["opId"] in {operation["opId"] for operation in page}
    assert len({operation["opId"] for operation in page}) == len(page)
    assert meta["hasMoreBefore"] is True
    assert meta["nextBeforeDisplaySeq"] == min(operation["displaySeq"] for operation in expected_tail)

    earlier, earlier_meta = await web_env.server._web_operations_page(
        conv_uuid,
        limit=2,
        before_display_seq=meta["nextBeforeDisplaySeq"],
        include_tool_details=False,
    )
    assert active["opId"] in {operation["opId"] for operation in earlier}
    assert not ({operation["opId"] for operation in expected_tail} & {operation["opId"] for operation in earlier})
    assert earlier_meta["nextBeforeDisplaySeq"] < meta["nextBeforeDisplaySeq"]

    operations_response = await web_env.client.get(
        f"/api/conversations/{conv_uuid}/operations?timelineLimit=2",
        cookies={"openbear_web_session": cookie},
    )
    operations_data = await operations_response.json()
    assert operations_response.status == 200
    assert operations_data["hasMoreBefore"] is True
    assert operations_data["nextBeforeDisplaySeq"] == meta["nextBeforeDisplaySeq"]
    assert active["opId"] in {operation["opId"] for operation in operations_data["operations"]}

    state_response = await web_env.client.get(
        f"/api/conversations/{conv_uuid}/state?timelineLimit=2",
        cookies={"openbear_web_session": cookie},
    )
    state_data = await state_response.json()
    assert state_response.status == 200
    assert state_data["hasMoreBefore"] is True
    assert state_data["nextBeforeDisplaySeq"] == meta["nextBeforeDisplaySeq"]
    assert active["opId"] in {operation["opId"] for operation in state_data["operations"]}

    legacy_response = await web_env.client.get(
        f"/api/conversations/{conv_uuid}/operations",
        cookies={"openbear_web_session": cookie},
    )
    assert [operation["opId"] for operation in (await legacy_response.json())["operations"]] == [operation["opId"] for operation in full]

    # A displaySeq-only cursor must never split and permanently lose a tie.
    await web_env.db.conn.execute(
        "UPDATE web_operations SET display_seq=? WHERE conversation_uuid=? AND op_id=?",
        (full[-1]["displaySeq"], conv_uuid, full[-2]["opId"]),
    )
    await web_env.db.conn.commit()
    tied_page, tied_meta = await web_env.server._web_operations_page(
        conv_uuid,
        limit=1,
        include_tool_details=False,
    )
    assert {full[-2]["opId"], full[-1]["opId"]}.issubset({operation["opId"] for operation in tied_page})
    assert tied_meta["nextBeforeDisplaySeq"] == full[-1]["displaySeq"]


async def test_web_operation_tail_page_completes_boundary_turn_and_tied_turn(web_env):
    row = await web_env.server._create_web_conversation(123, title="complete page turns")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])

    async def publish(op_id: str, op_type: str, turn_uuid: str, text: str):
        async with web_env.server._web_operation_lock(conv_uuid):
            await web_env.server._publish_operation(
                conv_uuid,
                internal_chat_id=chat_id,
                owner_chat_id=123,
                op_id=op_id,
                op_type=op_type,
                action="end" if op_type != "user_message" else "create",
                turn_uuid=turn_uuid,
                run_root_turn_uuid=turn_uuid,
                payload={"text": text, "complete": True},
                status="completed",
                lifecycle="terminal",
                source="test",
            )

    await publish("notice:oldest", "notice", "turn-oldest", "oldest")
    await publish("msg:early", "user_message", "turn-early", "early user")
    await publish("assistant:early", "assistant_message", "turn-early", "early answer")
    await publish("msg:boundary", "user_message", "turn-boundary", "boundary user")
    await publish("tool:boundary", "tool", "turn-boundary", "boundary tool")
    await publish("assistant:boundary", "assistant_message", "turn-boundary", "boundary answer")
    await publish("msg:tail", "user_message", "turn-tail", "tail user")
    await publish("assistant:tail", "assistant_message", "turn-tail", "tail answer")

    # The limit=3 seed lands on the final boundary-turn operation. Make an
    # earlier turn share the expanded boundary displaySeq: both the tie and that
    # tied turn's earlier user row must be included, while the oldest turn stays
    # available through the cursor.
    cur = await web_env.db.conn.execute(
        "SELECT display_seq FROM web_operations WHERE conversation_uuid=? AND op_id='msg:boundary'",
        (conv_uuid,),
    )
    boundary_seq = int((await cur.fetchone())["display_seq"])
    await web_env.db.conn.execute(
        "UPDATE web_operations SET display_seq=? WHERE conversation_uuid=? AND op_id='assistant:early'",
        (boundary_seq, conv_uuid),
    )
    await web_env.db.conn.commit()

    page, meta = await web_env.server._web_operations_page(
        conv_uuid,
        limit=3,
        include_tool_details=False,
    )
    op_ids = [operation["opId"] for operation in page]

    assert op_ids == [
        "msg:early",
        "assistant:early",
        "msg:boundary",
        "tool:boundary",
        "assistant:boundary",
        "msg:tail",
        "assistant:tail",
    ]
    assert "notice:oldest" not in op_ids
    assert meta["hasMoreBefore"] is True
    assert meta["nextBeforeDisplaySeq"] == page[0]["displaySeq"]
    assert page[1]["displaySeq"] == page[2]["displaySeq"] == boundary_seq


async def test_web_operation_page_active_higher_id_tie_includes_complete_turn_in_db_order(web_env):
    row = await web_env.server._create_web_conversation(123, title="active complete turn order")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])

    async def publish(
        op_id: str,
        op_type: str,
        turn_uuid: str,
        *,
        lifecycle: str = "terminal",
    ):
        async with web_env.server._web_operation_lock(conv_uuid):
            await web_env.server._publish_operation(
                conv_uuid,
                internal_chat_id=chat_id,
                owner_chat_id=123,
                op_id=op_id,
                op_type=op_type,
                action="start" if lifecycle == "active" else ("create" if op_type == "user_message" else "end"),
                turn_uuid=turn_uuid,
                run_root_turn_uuid=turn_uuid,
                payload={"text": op_id, "complete": lifecycle != "active"},
                status="running" if lifecycle == "active" else "completed",
                lifecycle=lifecycle,
                source="test",
            )

    await publish("msg:active-turn", "user_message", "turn-active-complete")
    await publish("tool:active-early", "tool", "turn-active-complete")
    await publish("assistant:active-early", "assistant_message", "turn-active-complete")
    for index in range(5):
        await publish(f"notice:tail-{index}", "notice", f"turn-tail-{index}")
    # Insert the active operation last so its row id is higher, then force the
    # exact displaySeq tie that used to reverse during older-page/current merge.
    await publish("status:active-higher-id", "status", "turn-active-complete", lifecycle="active")
    cur = await web_env.db.conn.execute(
        "SELECT display_seq FROM web_operations WHERE conversation_uuid=? AND op_id='msg:active-turn'",
        (conv_uuid,),
    )
    user_seq = int((await cur.fetchone())["display_seq"])
    await web_env.db.conn.execute(
        "UPDATE web_operations SET display_seq=? WHERE conversation_uuid=? AND op_id='status:active-higher-id'",
        (user_seq, conv_uuid),
    )
    await web_env.db.conn.commit()

    page, _meta = await web_env.server._web_operations_page(
        conv_uuid,
        limit=1,
        include_tool_details=False,
    )
    op_ids = [operation["opId"] for operation in page]

    assert {"msg:active-turn", "tool:active-early", "assistant:active-early", "status:active-higher-id"}.issubset(op_ids)
    user = page[op_ids.index("msg:active-turn")]
    active = page[op_ids.index("status:active-higher-id")]
    assert op_ids.index("msg:active-turn") < op_ids.index("status:active-higher-id")
    assert user["displaySeq"] == active["displaySeq"]
    assert user["operationOrder"] < active["operationOrder"]


async def test_native_historical_compaction_is_not_injected_ahead_of_its_page(web_env):
    row = await web_env.server._create_web_conversation(123, title="native paged compaction")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])

    async def publish(
        op_id: str,
        op_type: str,
        turn_uuid: str,
        *,
        payload: dict[str, object],
        source: str,
    ) -> None:
        async with web_env.server._web_operation_lock(conv_uuid):
            await web_env.server._publish_operation(
                conv_uuid,
                internal_chat_id=chat_id,
                owner_chat_id=123,
                op_id=op_id,
                op_type=op_type,
                action="create" if op_type == "user_message" else "end",
                turn_uuid=turn_uuid,
                run_root_turn_uuid=turn_uuid,
                payload=payload,
                status="completed",
                lifecycle="terminal",
                source=source,
                internal=False,
            )

    await publish(
        "msg:old-compaction-turn",
        "user_message",
        "turn-old-compaction",
        payload={"text": "old"},
        source="user",
    )
    await publish(
        "tool:context-compaction:501",
        "tool",
        "turn-old-compaction",
        payload={
            "toolCallId": "context-compaction:501",
            "toolName": "ContextCompaction",
            "compactionId": "context-compaction:501",
            "summaryId": 501,
            "scope": "root",
            "status": "completed",
        },
        source="context_compaction",
    )
    await publish(
        "msg:recent-turn",
        "user_message",
        "turn-recent",
        payload={"text": "recent"},
        source="user",
    )
    await publish(
        "assistant:recent-turn",
        "assistant_message",
        "turn-recent",
        payload={"text": "answer", "complete": True},
        source="assistant",
    )

    initial, initial_meta = await web_env.server._web_operations_page(
        conv_uuid,
        limit=2,
        include_tool_details=False,
    )
    projected_initial = await web_env.server._project_context_compaction_operations(
        chat_id,
        conv_uuid,
        initial,
        include_tool_details=False,
        timeline_page=initial_meta,
    )
    initial_cursor = initial_meta["nextBeforeDisplaySeq"]
    assert [operation["opId"] for operation in initial] == ["msg:recent-turn", "assistant:recent-turn"]
    assert not any(operation["opType"] == "context_compaction" for operation in projected_initial)
    assert initial_meta["pageRowCount"] == 2
    assert initial_meta["hasMoreBefore"] is True
    assert initial_meta["nextBeforeDisplaySeq"] == initial_cursor

    earlier, earlier_meta = await web_env.server._web_operations_page(
        conv_uuid,
        limit=2,
        before_display_seq=initial_cursor,
        include_tool_details=False,
    )
    projected_earlier = await web_env.server._project_context_compaction_operations(
        chat_id,
        conv_uuid,
        earlier,
        include_tool_details=False,
        timeline_page=earlier_meta,
    )
    compactions = [operation for operation in projected_earlier if operation["opType"] == "context_compaction"]
    assert len(compactions) == 1
    assert compactions[0]["opId"] == "tool:context-compaction:501"
    assert compactions[0]["turnUuid"] == compactions[0]["runRootTurnId"] == "turn-old-compaction"
    assert compactions[0]["taskUuid"] == ""
    assert compactions[0]["payload"]["scope"] == "root"
    assert compactions[0]["displaySeq"] == next(
        operation["displaySeq"] for operation in earlier
        if operation["opId"] == "tool:context-compaction:501"
    )


async def test_paged_legacy_compaction_is_local_to_its_loaded_anchor_turn(web_env):
    row = await web_env.server._create_web_conversation(123, title="paged legacy compaction")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    anchor_id = await MessageDAO(web_env.db).add(
        chat_id,
        "user",
        "legacy anchor with a durable turn",
        conversation_uuid=conv_uuid,
        turn_uuid="turn-legacy-anchor",
        run_root_turn_uuid="turn-legacy-anchor",
    )
    summary_id = await SummaryDAO(web_env.db).add(chat_id, "private legacy summary", anchor_id, 20)
    unplaced_anchor_id = await MessageDAO(web_env.db).add(
        chat_id,
        "user",
        "legacy anchor without any operation placement",
        conversation_uuid=conv_uuid,
        turn_uuid="turn-summary-only-unplaced",
        run_root_turn_uuid="turn-summary-only-unplaced",
    )
    unplaced_summary_id = await SummaryDAO(web_env.db).add(
        chat_id,
        "must not be moved to the current tail",
        unplaced_anchor_id,
        10,
    )
    async with web_env.server._web_operation_lock(conv_uuid):
        await web_env.server._publish_operation(
            conv_uuid,
            internal_chat_id=chat_id,
            owner_chat_id=123,
            op_id="msg:legacy-anchor",
            op_type="user_message",
            action="create",
            turn_uuid="turn-legacy-anchor",
            run_root_turn_uuid="turn-legacy-anchor",
            payload={"text": "legacy anchor"},
            status="completed",
            lifecycle="terminal",
            source="user",
        )
    for index in range(3):
        async with web_env.server._web_operation_lock(conv_uuid):
            await web_env.server._publish_operation(
                conv_uuid,
                internal_chat_id=chat_id,
                owner_chat_id=123,
                op_id=f"notice:after-legacy-{index}",
                op_type="notice",
                action="create",
                turn_uuid=f"turn-after-legacy-{index}",
                run_root_turn_uuid=f"turn-after-legacy-{index}",
                payload={"text": str(index)},
                status="completed",
                lifecycle="informational",
                source="test",
            )

    durable = await web_env.server._web_operations(conv_uuid, include_tool_details=False)
    full = await web_env.server._project_context_compaction_operations(
        chat_id,
        conv_uuid,
        durable,
        include_tool_details=False,
    )
    compaction_id = f"tool:context-compaction:{summary_id}"
    unplaced_compaction_id = f"tool:context-compaction:{unplaced_summary_id}"
    full_compaction = next(operation for operation in full if operation["opId"] == compaction_id)
    anchor_operation = next(operation for operation in durable if operation["opId"] == "msg:legacy-anchor")
    assert full_compaction["opType"] == "context_compaction"
    assert full_compaction["displaySeq"] == anchor_operation["displaySeq"]
    assert full_compaction["turnUuid"] == full_compaction["runRootTurnId"] == "turn-legacy-anchor"
    assert unplaced_compaction_id not in {operation["opId"] for operation in full}

    before_display_seq = None
    seen_compactions: list[dict[str, object]] = []
    page_number = 0
    while True:
        page_number += 1
        page_operations, page_meta = await web_env.server._web_operations_page(
            conv_uuid,
            limit=1,
            before_display_seq=before_display_seq,
            include_tool_details=False,
        )
        original_meta = dict(page_meta)
        paged = await web_env.server._project_context_compaction_operations(
            chat_id,
            conv_uuid,
            page_operations,
            include_tool_details=False,
            timeline_page=page_meta,
        )
        assert page_meta == original_meta
        page_compactions = [operation for operation in paged if operation["opId"] == compaction_id]
        if page_number == 1:
            assert page_compactions == []
            assert page_meta["pageRowCount"] == 1
        seen_compactions.extend(page_compactions)
        assert unplaced_compaction_id not in {operation["opId"] for operation in paged}
        if page_compactions:
            assert page_meta["selectedTurnKeys"] == ["turn-legacy-anchor"]
            assert page_compactions[0]["displaySeq"] == anchor_operation["displaySeq"]
            assert page_compactions[0]["detailLoaded"] is False
        if not page_meta["hasMoreBefore"]:
            break
        before_display_seq = page_meta["nextBeforeDisplaySeq"]

    assert len(seen_compactions) == 1
    assert page_number == 4


async def test_web_state_timeline_duration_aggregates_all_stats_beyond_tail_page(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="timeline duration aggregate")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])

    # The first five rows fall outside a 200-row tail page. One valid duration
    # lives there, while malformed/missing/non-numeric/negative values exercise
    # the SQL guards without requiring operation payload deserialization.
    async with web_env.server._web_operation_lock(conv_uuid):
        async with web_env.db.web_operation_transaction() as conn:
            for index in range(205):
                payload: dict[str, object] = {}
                if index == 0:
                    payload = {"durationMs": 123}
                elif index == 2:
                    payload = {"durationMs": -20}
                elif index == 3:
                    payload = {"durationMs": "40"}
                elif index == 4:
                    payload = {"durationMs": True}
                elif index == 5:
                    payload = {"durationMs": 456}
                await web_env.server._publish_operation(
                    conv_uuid,
                    internal_chat_id=chat_id,
                    owner_chat_id=123,
                    op_id=f"stats:aggregate-{index:03d}",
                    op_type="stats",
                    action="snapshot",
                    turn_uuid=f"turn-aggregate-{index:03d}",
                    run_root_turn_uuid=f"turn-aggregate-{index:03d}",
                    payload=payload,
                    status="completed",
                    lifecycle="informational",
                    source="test",
                    conn=conn,
                )
    await web_env.db.conn.execute(
        "UPDATE web_operations SET payload_json='{not-valid-json' "
        "WHERE conversation_uuid=? AND op_id='stats:aggregate-006'",
        (conv_uuid,),
    )
    await web_env.db.conn.commit()

    response = await web_env.client.get(
        f"/api/conversations/{conv_uuid}/state?timelineLimit=200",
        cookies={"openbear_web_session": cookie},
    )
    assert response.status == 200
    state = await response.json()

    assert len(state["operations"]) == 200
    assert state["hasMoreBefore"] is True
    assert "stats:aggregate-000" not in {operation["opId"] for operation in state["operations"]}
    assert sum(
        int(operation.get("payload", {}).get("durationMs") or 0)
        for operation in state["operations"]
    ) == 456
    assert state["timelineTotalDurationMs"] == 579


async def test_web_state_tail_page_metadata_and_legacy_messages_are_not_truncated(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="legacy state page")
    chat_id = int(row["internal_chat_id"])
    messages = MessageDAO(web_env.db)
    for index in range(5):
        await messages.add(chat_id, "user", f"legacy-{index}")

    response = await web_env.client.get(
        f"/api/conversations/{row['conversation_uuid']}/state?timelineLimit=2",
        cookies={"openbear_web_session": cookie},
    )
    assert response.status == 200
    state = await response.json()
    assert state["operations"] == []
    assert [message["content"] for message in state["messages"]] == [f"legacy-{index}" for index in range(5)]
    assert state["hasMoreBefore"] is False
    assert state["nextBeforeDisplaySeq"] is None
    assert state["timelineLimit"] == 2
    assert state["timelineTotalDurationMs"] == 0


async def test_web_frame_api_filters_by_after_frame_seq(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="operation frames api")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-frame"})
    await live.publish({"type": "user", "turnUuid": "turn-frame", "messageUuid": "msg-frame", "text": "看帧"})
    await live.publish({"type": "final", "turnUuid": "turn-frame", "text": "完成"})
    await live.publish({"type": "done", "turnUuid": "turn-frame"})

    all_frames = await web_env.server._web_frames(row["conversation_uuid"])
    assert len(all_frames) >= 4
    after = int(all_frames[1]["frameSeq"])

    resp = await web_env.client.get(
        f"/api/conversations/{row['conversation_uuid']}/frames?afterFrameSeq={after}&limit=2",
        cookies={"openbear_web_session": cookie},
    )
    assert resp.status == 200
    data = await resp.json()
    assert [frame["frameSeq"] for frame in data["frames"]] == [frame["frameSeq"] for frame in all_frames if frame["frameSeq"] > after][:2]
    assert data["frameSeq"] == data["frames"][-1]["frameSeq"]

    empty = await web_env.client.get(
        f"/api/conversations/{row['conversation_uuid']}/frames?afterFrameSeq=999999",
        cookies={"openbear_web_session": cookie},
    )
    assert empty.status == 200
    empty_data = await empty.json()
    assert empty_data["frames"] == []
    assert empty_data["frameSeq"] == 999999


async def test_web_live_stream_serializes_persist_and_broadcast_order():
    first_sink_entered = asyncio.Event()
    persisted: list[str] = []

    async def sink(payload):
        marker = str(payload["marker"])
        persisted.append(marker)
        frame_seq = len(persisted)
        if marker == "first":
            first_sink_entered.set()
            await asyncio.sleep(0.03)
        return {
            **payload,
            "_webFrames": [{
                "frameSeq": frame_seq,
                "opId": f"notice:{marker}",
                "opType": "notice",
                "action": "create",
                "revision": 1,
                "displaySeq": frame_seq,
                "payload": {"marker": marker},
            }],
        }

    live = _WebLiveStream("conv-ordered", -1, event_sink=sink)
    queue = live.subscribe()
    first = asyncio.create_task(live.publish({"type": "notice", "marker": "first"}))
    await first_sink_entered.wait()
    second = asyncio.create_task(live.publish({"type": "notice", "marker": "second"}))
    await asyncio.gather(first, second)

    broadcasts = [await queue.get(), await queue.get()]
    assert persisted == ["first", "second"]
    assert [item["marker"] for item in broadcasts] == ["first", "second"]
    assert [item["_webFrames"][0]["frameSeq"] for item in broadcasts] == [1, 2]


async def test_web_live_stream_overflow_wakes_writer_for_cursor_reconnect():
    live = _WebLiveStream("conv-overflow", -1)
    sub = live.subscribe()

    for i in range(sub.maxsize + 1):
        await live.publish({"type": "notice", "eventUuid": f"event-{i}", "text": str(i)})

    assert sub not in live._subscribers
    assert sub.qsize() == 1
    assert await sub.get() == {
        "_webLiveStreamControl": "overflow",
        "conversationUuid": "conv-overflow",
    }


async def test_websocket_send_refreshes_model_after_http_change(web_env, monkeypatch):
    cookie = await _login_cookie(web_env)
    web_env.server.model_selection = SimpleNamespace(
        current="openai/gpt",
        family_of=lambda label: str(label).split("/", 1)[0],
    )
    row = await web_env.server._create_web_conversation(123, title="websocket model refresh", model="openai/gpt")
    seen_models: list[str] = []
    turn_started = asyncio.Event()

    async def fake_run_web_turn(chat_id, user_text, renderer, media=None, *, conversation=None, **kwargs):
        seen_models.append(str((conversation or {}).get("model") or ""))
        turn_started.set()
        return True

    monkeypatch.setattr(web_env.server, "_run_web_turn", fake_run_web_turn)
    ws = await web_env.client.ws_connect(
        f"/api/conversations/{row['conversation_uuid']}/ws",
        headers={"Cookie": f"openbear_web_session={cookie}"},
    )
    try:
        state = await ws.receive_json(timeout=2)
        assert state["type"] == "state"
        changed = await web_env.client.post(
            f"/api/conversations/{row['conversation_uuid']}/model",
            json={"model": "openai/cheap"},
            cookies={"openbear_web_session": cookie},
        )
        assert changed.status == 200
        assert (await changed.json())["model"] == "openai/cheap"

        # Keep this same socket open: this is the path that previously kept the
        # row captured at WebSocket connection time and sent with openai/gpt.
        await ws.send_json({"type": "send", "text": "use updated model"})
        await asyncio.wait_for(turn_started.wait(), timeout=2)
        assert seen_models == ["openai/cheap"]
    finally:
        await ws.close()


async def test_websocket_state_suppresses_stale_frame_replay_after_frame_seq(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="operation ws replay")
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-ws"})
    await live.publish({"type": "user", "turnUuid": "turn-ws", "messageUuid": "msg-ws", "text": "补帧"})
    await live.publish({"type": "final", "turnUuid": "turn-ws", "text": "补帧完成"})
    await live.publish({"type": "done", "turnUuid": "turn-ws"})

    frames = await web_env.server._web_frames(row["conversation_uuid"])
    assert len(frames) >= 4
    after = int(frames[1]["frameSeq"])

    ws = await web_env.client.ws_connect(
        f"/api/conversations/{row['conversation_uuid']}/ws?afterFrameSeq={after}",
        headers={"Cookie": f"openbear_web_session={cookie}"},
    )
    try:
        state_msg = await ws.receive_json(timeout=2)
        assert state_msg["type"] == "state"
        state_frame_seq = int(state_msg["state"]["frameSeq"])
        assert state_frame_seq == frames[-1]["frameSeq"]

        await live.publish({"type": "accepted", "turnUuid": "turn-ws-next"})
        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "frame"
        assert int(msg["frame"]["frameSeq"]) > state_frame_seq
    finally:
        await ws.close()


async def test_websocket_full_state_resets_cursor_after_suffix_high_water_rollback(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="ws suffix cursor rollback")
    conv_uuid = str(row["conversation_uuid"])
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)
    messages = MessageDAO(web_env.db)

    async def add_turn(turn_uuid: str):
        message_uuid = f"message-{turn_uuid}"
        await live.publish({"type": "accepted", "turnUuid": turn_uuid, "runUuid": turn_uuid})
        await live.publish({"type": "user", "turnUuid": turn_uuid, "messageUuid": message_uuid, "text": turn_uuid})
        await web_env.server._persist_web_transcript_message(
            messages,
            chat_id,
            "user",
            turn_uuid,
            conversation_uuid=conv_uuid,
            turn_uuid=turn_uuid,
            run_root_turn_uuid=turn_uuid,
            op_ids=[f"msg:{message_uuid}"],
        )
        await live.publish({"type": "final", "turnUuid": turn_uuid, "text": f"answer {turn_uuid}"})
        await web_env.server._persist_web_transcript_message(
            messages,
            chat_id,
            "assistant",
            f"answer {turn_uuid}",
            conversation_uuid=conv_uuid,
            turn_uuid=turn_uuid,
            run_root_turn_uuid=turn_uuid,
            op_ids=[f"assistant:{turn_uuid}:0"],
        )
        await live.publish({"type": "done", "turnUuid": turn_uuid})

    await add_turn("turn-keep")
    await add_turn("turn-delete")
    old_frames = await web_env.server._web_frames(conv_uuid)
    stale_after = int(old_frames[-1]["frameSeq"])

    deleted = await web_env.client.delete(
        f"/api/conversations/{conv_uuid}/turns/turn-delete/suffix",
        cookies={"openbear_web_session": cookie},
    )
    assert deleted.status == 200
    remaining_frames = await web_env.server._web_frames(conv_uuid)
    rolled_back_high_water = int(remaining_frames[-1]["frameSeq"])
    assert rolled_back_high_water < stale_after

    ws = await web_env.client.ws_connect(
        f"/api/conversations/{conv_uuid}/ws?afterFrameSeq={stale_after}",
        headers={"Cookie": f"openbear_web_session={cookie}"},
    )
    try:
        state_message = await ws.receive_json(timeout=2)
        assert state_message["type"] == "state"
        assert int(state_message["state"]["frameSeq"]) == rolled_back_high_water

        # MAX(frame_seq)+1 is still below the stale reconnect cursor. It must be
        # delivered because the full state replaced that old cursor.
        await live.publish({"type": "notice", "eventUuid": "after-delete", "text": "after delete"})
        next_message = await ws.receive_json(timeout=2)
        assert next_message["type"] == "frame"
        assert int(next_message["frame"]["frameSeq"]) == rolled_back_high_water + 1
        assert int(next_message["frame"]["frameSeq"]) <= stale_after
    finally:
        await ws.close()


async def test_websocket_incremental_bootstrap_fills_subscribe_race_once(web_env, monkeypatch):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="incremental ws bootstrap")
    conv_uuid = str(row["conversation_uuid"])
    live = web_env.server._live_for(row)
    await live.publish({"type": "notice", "eventUuid": "before-state", "text": "before state"})

    state_response = await web_env.client.get(
        f"/api/conversations/{conv_uuid}/state?timelineLimit=2",
        cookies={"openbear_web_session": cookie},
    )
    state = await state_response.json()
    after = int(state["frameSeq"])

    # Committed after HTTP and before subscribe: SQL catch-up must include it.
    await live.publish({"type": "notice", "eventUuid": "bootstrap-gap", "text": "bootstrap gap"})
    original_web_frames = web_env.server._web_frames
    raced = False

    async def racing_web_frames(*args, **kwargs):
        nonlocal raced
        if kwargs.get("up_to_frame_seq") is not None and not raced:
            raced = True
            # Committed after the WS high-water query: the preinstalled
            # subscription must carry it, while frameSeq dedupe removes overlap.
            await live.publish({"type": "notice", "eventUuid": "bootstrap-race", "text": "bootstrap race"})
        return await original_web_frames(*args, **kwargs)

    monkeypatch.setattr(web_env.server, "_web_frames", racing_web_frames)
    ws = await web_env.client.ws_connect(
        f"/api/conversations/{conv_uuid}/ws?afterFrameSeq={after}&bootstrap=incremental",
        headers={"Cookie": f"openbear_web_session={cookie}"},
    )
    try:
        received = [
            await ws.receive_json(timeout=2),
            await ws.receive_json(timeout=2),
            await ws.receive_json(timeout=2),
        ]
        assert [message["type"] for message in received] == ["frame", "bootstrap", "frame"]
        frame_messages = [message for message in received if message["type"] == "frame"]
        seqs = [int(message["frame"]["frameSeq"]) for message in frame_messages]
        assert seqs == [after + 1, after + 2]
        assert len(set(seqs)) == 2
        assert all("state" not in message and "conversations" not in message for message in received)
        with pytest.raises(asyncio.TimeoutError):
            await ws.receive_json(timeout=0.1)
    finally:
        await ws.close()


async def test_agent_terminal_patch_updates_result_text_status(web_env):
    payload = {
        "toolCallId": "call-patch",
        "status": "running",
        "task": {"taskUuid": "task-patch", "status": "running"},
        "resultText": json.dumps({"ok": True, "status": "running", "task": {"taskUuid": "task-patch", "status": "running"}}, ensure_ascii=False),
    }

    patch = web_env.server._agent_operation_terminal_patch(payload, "interrupted", reason="已停止")

    result = json.loads(patch["resultText"])
    assert result["status"] == "interrupted"
    assert result["ok"] is False
    assert result["error"] == "已停止"
    assert result["task"]["status"] == "interrupted"


async def test_web_operations_stop_cancels_active_agent_snapshot(web_env):
    row = await web_env.server._create_web_conversation(123, title="operation stop")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-stop"})
    await live.publish({"type": "user", "turnUuid": "turn-stop", "messageUuid": "msg-stop", "text": "跑 Agent"})
    await live.publish({"type": "tool_start", "toolCallId": "call-stop", "name": "Agent", "arguments": "{}", "line": "Agent: running"})
    await live.publish({
        "type": "tool_progress",
        "toolCallId": "call-stop",
        "name": "Agent",
        "arguments": "{}",
        "payload": {"status": "running", "results": [{"status": "running", "task": {"status": "running", "currentStatus": "模型调用中"}}]},
    })
    await live.publish({"type": "stopped", "turnUuid": "turn-stop", "reason": "已停止"})

    state = await web_env.server._chat_payload(chat_id, row)
    agent = {op["opId"]: op for op in state["operations"]}["agent:call-stop"]
    assert agent["status"] == "interrupted"
    assert agent["lifecycle"] == "terminal"
    assert agent["payload"]["results"][0]["status"] == "interrupted"
    assert agent["payload"]["results"][0]["task"]["status"] == "interrupted"
    assert state["facts"]["activeBackgroundAgentOpIds"] == []


async def test_web_operations_stop_cancels_single_agent_nested_task(web_env):
    row = await web_env.server._create_web_conversation(123, title="single agent stop")
    chat_id = int(row["internal_chat_id"])
    live = web_env.server._live_for(row)

    await live.publish({"type": "accepted", "turnUuid": "turn-stop-single"})
    await live.publish({"type": "user", "turnUuid": "turn-stop-single", "messageUuid": "msg-stop-single", "text": "跑 Agent"})
    await live.publish({"type": "tool_start", "toolCallId": "call-stop-single", "name": "Agent", "arguments": "{}", "line": "Agent: running"})
    await live.publish({
        "type": "tool_progress",
        "toolCallId": "call-stop-single",
        "name": "Agent",
        "arguments": "{}",
        "payload": {
            "status": "running",
            "task": {"status": "running", "currentStatus": "模型调用中"},
            "result": {"task": {"status": "running", "currentStatus": "工具调用中"}},
        },
    })
    await live.publish({"type": "stopped", "turnUuid": "turn-stop-single", "reason": "已停止"})

    state = await web_env.server._chat_payload(chat_id, row)
    agent = {op["opId"]: op for op in state["operations"]}["agent:call-stop-single"]
    assert agent["status"] == "interrupted"
    assert agent["payload"]["task"]["status"] == "interrupted"
    assert agent["payload"]["task"]["currentStatus"] == "已停止"
    assert agent["payload"]["result"]["task"]["status"] == "interrupted"
    assert agent["payload"]["result"]["task"]["currentStatus"] == "已停止"


async def test_web_model_call_delta_is_immediately_visible_in_channel_and_model_stats(web_env):
    row = await web_env.server._create_web_conversation(123, title="immediate accounting")
    chat_id = int(row["internal_chat_id"])
    messages = MessageDAO(web_env.db)
    session_uuid = await messages.get_or_create_session_uuid(chat_id)
    usage = Usage(input_tokens=100, output_tokens=20, cache_read_tokens=30, cache_write_tokens=5)

    cost = await web_env.server._persist_web_model_call_delta(
        messages,
        chat_id,
        session_uuid=session_uuid,
        call={"status": "ok", "usage": usage, "connectMs": 10, "firstTokenMs": 20, "totalTimeMs": 100, "outputTokens": 20},
        model_cost={"input": 1.0, "output": 2.0, "cacheRead": 0.5, "cacheWrite": 1.5},
        model_label="provider/model-a",
        protocol="responses",
        think_level="high",
    )

    assert cost > 0
    totals = await messages.usage_totals(chat_id)
    assert totals.input_tokens == 100
    assert totals.output_tokens == 20
    rows = await messages.recent_model_calls(chat_id)
    assert len(rows) == 1
    assert rows[0].input_tokens == 100
    provider = await messages.provider_call_summary(chat_id)
    assert provider[0]["provider"] == "provider"
    assert provider[0]["calls"] == 1
    models = await messages.model_detail_summary(chat_id, "provider")
    assert models[0]["model"] == "provider/model-a"
    assert models[0]["calls"] == 1
    state = await web_env.server._chat_payload(chat_id, row)
    assert state["usage"]["ledger_revision"] == rows[0].id
    assert state["usage"]["input_tokens"] == totals.input_tokens
    assert state["usage"]["output_tokens"] == totals.output_tokens
    assert state["usage"]["cache_read_tokens"] == totals.cache_read_tokens
    assert state["usage"]["cache_write_tokens"] == totals.cache_write_tokens
    assert state["usage"]["cost_usd"] == totals.cost_usd


async def test_web_turn_sums_individually_priced_context_tier_calls(web_env):
    # Two 150K requests must each use the base band. Repricing their aggregate
    # 300K Usage would incorrectly select the >200K tier for the whole turn.
    model = web_env.server.config.models.providers["openai"].models[0]
    model.cost = {
        "input": 1.0,
        "output": 0.0,
        "tiers": [{"contextTokens": 200_000, "input": 4.0, "output": 0.0}],
    }
    backend = FakeStreamBackend([
        [
            StreamEvent(kind="usage", usage=Usage(input_tokens=150_000)),
            StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="tier-call", name="echo", arguments="{}")]),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(kind="usage", usage=Usage(input_tokens=150_000)),
            StreamEvent(kind="content", text="完成"),
            StreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=512_000)
    registry = ToolRegistry()

    async def _echo(_args):
        return "ok"

    registry.add("echo", "echo", {"type": "object", "properties": {}}, _echo)
    web_env.server.tools = registry
    row = await web_env.server._create_web_conversation(123, title="tier per call", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    events_seen: list[dict] = []

    async def _sink(event):
        payload = dict(event)
        payload.setdefault("eventUuid", f"event-{len(events_seen) + 1}")
        payload.setdefault("eventId", payload["eventUuid"])
        events_seen.append(payload)
        return payload

    live = _WebLiveStream(str(row["conversation_uuid"]), chat_id, event_sink=_sink)
    await live.publish({"type": "accepted", "turnUuid": "tier-per-call-turn"})
    assert await web_env.server._run_web_turn(
        chat_id,
        "run",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid="tier-per-call-turn",
    ) is True

    stats_events = [event for event in events_seen if event.get("type") == "stats"]
    assert stats_events
    final_stats = stats_events[-1]["stats"]
    assert backend.calls == 2
    assert final_stats["usage"]["inputTokens"] == 300_000
    assert final_stats["costUsd"] == pytest.approx(0.3)
    assert final_stats["ledgerCostUsd"] == pytest.approx(0.3)
    rows = await MessageDAO(web_env.db).recent_model_calls(chat_id)
    controller_rows = [row for row in rows if row.call_kind == "controller_request"]
    assert [row.cost_usd for row in controller_rows] == pytest.approx([0.15, 0.15])


async def test_web_fast_billing_uses_each_response_actual_service_tier(web_env):
    model = web_env.server.config.models.providers["openai"].models[0]
    model.cost = {"input": 2.0, "output": 0.0}
    model.fast_cost = {"input": 4.0, "output": 0.0}
    backend = FakeStreamBackend([
        [
            StreamEvent(
                kind="usage",
                usage=Usage(input_tokens=1_000_000),
                details={"serviceTier": "default"},
            ),
            StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="tier-call", name="echo", arguments="{}")]),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(
                kind="usage",
                usage=Usage(input_tokens=1_000_000),
                details={"serviceTier": "priority"},
            ),
            StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="tier-call-2", name="echo", arguments="{}")]),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(
                kind="usage",
                usage=Usage(input_tokens=1_000_000),
                details={"serviceTier": "priority", "providerCostUsd": 0.125},
            ),
            StreamEvent(kind="content", text="完成"),
            StreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=2_000_000)
    registry = ToolRegistry()

    async def _echo(_args):
        return "ok"

    registry.add("echo", "echo", {"type": "object", "properties": {}}, _echo)
    web_env.server.tools = registry
    row = await web_env.server._create_web_conversation(
        123, title="actual service tier", model="openai/gpt",
    )
    chat_id = int(row["internal_chat_id"])
    await MessageDAO(web_env.db).set_fast_mode(chat_id, True)

    assert await web_env.server._run_web_turn(
        chat_id,
        "run",
        _WebStreamRenderer(),
        conversation=row,
        root_turn_uuid="actual-tier-turn",
    ) is True

    rows = await MessageDAO(web_env.db).recent_model_calls(chat_id)
    controller_rows = [row for row in rows if row.call_kind == "controller_request"]
    assert sorted(row.cost_usd for row in controller_rows) == pytest.approx([0.125, 2.0, 4.0])
    totals = await MessageDAO(web_env.db).usage_totals(chat_id)
    assert totals.cost_usd == pytest.approx(6.125)


async def test_web_model_call_delta_rolls_back_all_ledgers_on_mid_batch_failure(web_env, monkeypatch):
    row = await web_env.server._create_web_conversation(123, title="atomic accounting")
    chat_id = int(row["internal_chat_id"])
    messages = MessageDAO(web_env.db)
    session_uuid = await messages.get_or_create_session_uuid(chat_id)

    async def fail_mid_batch(*_args, **_kwargs):
        raise RuntimeError("forced accounting failure")

    monkeypatch.setattr(MessageDAO, "add_turn_stats", fail_mid_batch)
    with pytest.raises(RuntimeError, match="forced accounting failure"):
        await web_env.server._persist_web_model_call_delta(
            messages,
            chat_id,
            session_uuid=session_uuid,
            call={"status": "ok", "usage": Usage(input_tokens=100, output_tokens=20)},
            model_cost={"input": 1.0, "output": 2.0},
            model_label="provider/model-a",
            protocol="responses",
            think_level="high",
        )

    totals = await messages.usage_totals(chat_id)
    assert totals.input_tokens == 0
    assert totals.output_tokens == 0
    assert await messages.recent_model_calls(chat_id) == []


async def test_cancelled_web_run_persists_partial_usage_and_context(web_env):
    messages = MessageDAO(web_env.db)
    chat_id = 123
    await messages.ensure_session(chat_id)
    session_uuid = await messages.get_or_create_session_uuid(chat_id)
    result = RunResult(
        total_time_ms=9876,
        model_calls=2,
        model_ok=1,
        tools_used=["Read"],
    )
    result.halted_reason = "cancelled"
    result.usage = Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=300, cache_write_tokens=0)
    result.last_usage = Usage(input_tokens=400, output_tokens=50, cache_read_tokens=100, cache_write_tokens=0)
    result.connect_ms_sum = 12
    result.first_token_ms_sum = 34
    result.call_time_ms_sum = 5600
    result.output_tokens_sum = 200

    cost = await web_env.server._persist_web_run_metrics(
        messages,
        chat_id,
        session_uuid=session_uuid,
        result=result,
        model_cost={"input": 2.0, "output": 10.0, "cacheRead": 0.5, "cacheWrite": 2.0},
        model_label="openai/gpt",
        protocol="chat",
        think_level="xhigh",
        status="cancelled",
        error_type="cancelled",
    )

    usage = await messages.usage_totals(chat_id)
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 200
    assert usage.cache_read_tokens == 300
    assert usage.last_input_tokens == 400
    assert usage.last_cache_read_tokens == 100
    assert usage.last_run_total_time_ms == 9876
    assert usage.last_run_model_calls == 2
    assert cost > 0

    rows = await web_env.server._chat_model_calls(chat_id, session_uuid)
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["error_type"] == "cancelled"
    assert rows[0]["model_call_count"] == 2
    assert rows[0]["model_ok_count"] == 1
    assert rows[0]["model_fail_count"] == 0
    assert rows[0]["last_input_tokens"] == 400
    assert rows[0]["last_cache_read_tokens"] == 100


async def test_web_run_metrics_persist_expert_usage_and_counts(web_env):
    messages = MessageDAO(web_env.db)
    chat_id = 123
    await messages.ensure_session(chat_id)
    session_uuid = await messages.get_or_create_session_uuid(chat_id)
    result = RunResult(
        total_time_ms=1200,
        model_calls=1,
        model_ok=1,
        tools_used=["Agent"],
    )
    result.usage = Usage(input_tokens=100, output_tokens=50, cache_read_tokens=10, cache_write_tokens=0)
    result.last_usage = Usage(input_tokens=90, output_tokens=40, cache_read_tokens=5, cache_write_tokens=0)
    result.expert_usage = Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=300, cache_write_tokens=20)
    result.expert_model_calls = 3
    result.expert_tool_calls = 4
    result.expert_cost_usd = 0.5
    result.connect_ms_sum = 10
    result.first_token_ms_sum = 20
    result.call_time_ms_sum = 1000
    result.output_tokens_sum = 50

    main_cost = await web_env.server._persist_web_run_metrics(
        messages,
        chat_id,
        session_uuid=session_uuid,
        result=result,
        model_cost={"input": 1.0, "output": 2.0, "cacheRead": 0.5, "cacheWrite": 1.0},
        model_label="openai/gpt",
        protocol="chat",
        think_level="xhigh",
    )

    usage = await messages.usage_totals(chat_id)
    assert usage.input_tokens == 1100
    assert usage.output_tokens == 250
    assert usage.cache_read_tokens == 310
    assert usage.cache_write_tokens == 20
    assert usage.last_input_tokens == 90
    assert usage.last_cache_read_tokens == 5
    assert usage.last_run_model_calls == 4
    assert usage.last_run_tool_calls == 5
    assert usage.cost_usd > main_cost

    rows = await web_env.server._chat_model_calls(chat_id, session_uuid)
    assert len(rows) == 1
    row = rows[0]
    assert row["input_tokens"] == 100
    assert row["expert_input_tokens"] == 1000
    assert row["expert_cache_read_tokens"] == 300
    assert row["expert_tool_calls"] == 4
    assert row["model_call_count"] == 4
    assert row["model_ok_count"] == 4
    assert row["cost_usd"] == pytest.approx(main_cost + 0.5)


async def test_memory_web_api_crud_preview_and_secret_values(web_env):
    cookie = await _login_cookie(web_env)

    cats = await web_env.client.get("/api/memory/categories", cookies={"openbear_web_session": cookie})
    assert cats.status == 200
    category_keys = {item["key"] for item in (await cats.json())["items"]}
    assert {"identity", "persona", "rule"}.isdisjoint(category_keys)
    assert {"memory", "tools"}.issubset(category_keys)

    removed_entry = await web_env.client.post(
        "/api/memory/entries",
        json={"category": "rule", "name": "旧规则", "body": "不应再创建"},
        cookies={"openbear_web_session": cookie},
    )
    assert removed_entry.status == 400
    assert "category_removed" in (await removed_entry.json())["error"]

    entry = await web_env.client.post(
        "/api/memory/entries",
        json={"category": "memory", "name": "Web记忆", "ref": "web-memory", "grp": "环境", "body": "Web 里可维护。"},
        cookies={"openbear_web_session": cookie},
    )
    assert entry.status == 200
    entry_item = (await entry.json())["item"]
    assert entry_item["ref"] == "web-memory"
    assert entry_item["grp"] == "环境"
    assert entry_item["createdAt"] > 0
    entry_detail = await web_env.client.get(f"/api/memory/entries/{entry_item['id']}", cookies={"openbear_web_session": cookie})
    assert entry_detail.status == 200
    entry_before_reorder = (await entry_detail.json())["item"]
    assert entry_before_reorder["body"] == "Web 里可维护。"
    assert entry_before_reorder["expanded"] is False

    entry_reorder = await web_env.client.post(
        "/api/memory/reorder",
        json={"kind": "entries", "items": [{"id": entry_item["id"], "grp": "环境", "sort": 10, "expanded": 1}]},
        cookies={"openbear_web_session": cookie},
    )
    assert entry_reorder.status == 200
    entry_after_reorder = await web_env.client.get(f"/api/memory/entries/{entry_item['id']}", cookies={"openbear_web_session": cookie})
    expanded_item = (await entry_after_reorder.json())["item"]
    assert expanded_item["expanded"] is True
    assert expanded_item["updatedAt"] == entry_before_reorder["updatedAt"]

    secret = await web_env.client.post(
        "/api/memory/secrets",
        json={"name": "web-secret", "note": "测试", "grp": "开发", "sort": 10, "kvJson": '[{"key":"token","value":"FULL"}]'},
        cookies={"openbear_web_session": cookie},
    )
    assert secret.status == 200
    sec_id = (await secret.json())["item"]["id"]
    secret_detail = await web_env.client.get(f"/api/memory/secrets/{sec_id}", cookies={"openbear_web_session": cookie})
    assert secret_detail.status == 200
    secret_item = (await secret_detail.json())["item"]
    assert secret_item["kv"][0]["value"] == "FULL"
    assert secret_item["grp"] == "开发"
    assert secret_item["created_at"] > 0
    full = await web_env.client.get("/api/memory/secrets?full=1", cookies={"openbear_web_session": cookie})
    full_items = (await full.json())["items"]
    assert any(item["id"] == sec_id and item["kv"][0]["value"] == "FULL" for item in full_items)

    doc = await web_env.client.post(
        "/api/memory/docs",
        json={"name": "web-doc", "title": "Web文档", "summary": "摘要", "grp": "手册", "sort": 10, "content": "全文"},
        cookies={"openbear_web_session": cookie},
    )
    assert doc.status == 200
    doc_item = (await doc.json())["item"]
    assert doc_item["grp"] == "手册"
    assert doc_item["created_at"] > 0

    reorder = await web_env.client.post(
        "/api/memory/reorder",
        json={"kind": "docs", "items": [{"id": doc_item["id"], "grp": "归档资料", "sort": 20}]},
        cookies={"openbear_web_session": cookie},
    )
    assert reorder.status == 200
    reordered_doc = await web_env.client.get(f"/api/memory/docs/{doc_item['id']}", cookies={"openbear_web_session": cookie})
    assert (await reordered_doc.json())["item"]["grp"] == "归档资料"

    tpl = await web_env.client.post(
        "/api/memory/templates",
        json={"name": "exact", "content": "A [[ memory.byCat.memory.length ]] &amp; B", "isActive": True, "isAgentActive": True},
        cookies={"openbear_web_session": cookie},
    )
    assert tpl.status == 200

    templates = await web_env.client.get("/api/memory/templates", cookies={"openbear_web_session": cookie})
    template_data = await templates.json()
    assert templates.status == 200
    assert template_data["promptParams"]["runtimeInfo"]["channel"] == "web"
    assert template_data["promptParams"]["runtimeInfo"]["outputFormat"] == "markdown"
    assert template_data["promptParams"]["tools"]["allowlist"] == template_data["promptParams"]["toolNames"]
    assert template_data["promptParams"]["availableAgents"] == template_data["promptParams"]["agents"]["available"]
    exact_item = next(item for item in template_data["items"] if item["name"] == "exact")
    assert exact_item["is_agent_active"] == 1
    assert "agent_available" not in exact_item

    bad_condition = await web_env.client.post(
        "/api/memory/templates",
        json={"name": "bad-if", "content": "@if missingRoot.flag\nno\n@endif", "isActive": False},
        cookies={"openbear_web_session": cookie},
    )
    assert bad_condition.status == 400
    assert "template_condition_unknown_variable" in (await bad_condition.json())["error"]

    for name, content in [
        ("empty-template", ""),
        ("unclosed-if", "@if skillsPrompt\nmissing"),
        ("orphan-endif", "text\n@endif"),
        ("unclosed-fence", "```json\n{}"),
        ("unclosed-raw", "@raw\n@if ignored"),
    ]:
        invalid_structure = await web_env.client.post(
            "/api/memory/templates",
            json={"name": name, "content": content, "isActive": False},
            cookies={"openbear_web_session": cookie},
        )
        assert invalid_structure.status == 400
        assert "template_structure_invalid" in (await invalid_structure.json())["error"]

    bad_expr = await web_env.client.post(
        "/api/memory/templates",
        json={"name": "bad-expr", "content": "[[ missingRoot.flag ]]", "isActive": False},
        cookies={"openbear_web_session": cookie},
    )
    assert bad_expr.status == 400
    assert "template_render_error_marker" in (await bad_expr.json())["error"]

    tpl_id = (await tpl.json())["id"]
    partial = await web_env.client.put(
        f"/api/memory/templates/{tpl_id}",
        json={"is_active": True},
        cookies={"openbear_web_session": cookie},
    )
    assert partial.status == 200

    preview = await web_env.client.post(
        "/api/memory/preview",
        json={"params": {}},
        cookies={"openbear_web_session": cookie},
    )
    data = await preview.json()
    assert preview.status == 200
    assert "A 1 &amp; B" in data["prompt"]
    assert "FULL" not in data["prompt"]
    logs = await web_env.client.get("/api/memory/render-logs?pageSize=1", cookies={"openbear_web_session": cookie})
    log_item = (await logs.json())["items"][0]
    detail = await web_env.client.get(f"/api/memory/render-logs/{log_item['id']}", cookies={"openbear_web_session": cookie})
    assert (await detail.json())["item"]["output_len"] == len(data["prompt"])

    old_page = await web_env.client.get("/memory/secrets", cookies={"openbear_web_session": cookie})
    body = await old_page.text()
    assert old_page.status == 404
    assert "FULL" not in body


async def test_memory_writes_rejected_while_import_running(web_env):
    cookie = await _login_cookie(web_env)
    await web_env.db.conn.execute(
        "INSERT INTO operations (operation_uuid, chat_id, kind, status, detail_json, started_at) VALUES (?,?,?,?,?,?)",
        ("op-running", 0, "memory_import", "running", "{}", 1),
    )
    await web_env.db.conn.commit()
    resp = await web_env.client.post(
        "/api/memory/entries",
        json={"category": "memory", "name": "Blocked", "body": "blocked"},
        cookies={"openbear_web_session": cookie},
    )
    assert resp.status == 409


async def test_web_dist_dir_points_to_repo_web_dist(web_env):
    assert web_env.server._web_dist_dir() == Path(__file__).resolve().parents[1] / "web" / "dist"


async def test_vue_static_entry_requires_auth_but_login_assets_are_public(tmp_path):
    db = DB(str(tmp_path / "static.db"))
    await db.connect()
    bot = FakeBot()
    server = WebAdminServer(_cfg(), db, bot)  # type: ignore[arg-type]
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id='app'>OpenBear Vue Shell</div>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("window.__openbear=1", encoding="utf-8")
    server._web_dist_dir = lambda: dist  # type: ignore[method-assign]
    await server.ensure_secret_key()
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    try:
        unauth = await client.get("/", allow_redirects=False)
        assert unauth.status == 302
        assert unauth.headers["Location"] == "/login"
        login_page = await client.get("/login", allow_redirects=False)
        assert login_page.status == 200
        assert "OpenBear Vue Shell" in await login_page.text()
        asset_unauth = await client.get("/assets/app.js", allow_redirects=False)
        assert asset_unauth.status == 200
        assert "__openbear" in await asset_unauth.text()

        cookie = await _login_cookie(SimpleNamespace(db=db, bot=bot, server=server, client=client))
        page = await client.get("/", cookies={"openbear_web_session": cookie})
        assert page.status == 200
        assert "OpenBear Vue Shell" in await page.text()

        asset = await client.get("/assets/app.js", cookies={"openbear_web_session": cookie})
        assert asset.status == 200
        assert "__openbear" in await asset.text()
    finally:
        await client.close()
        await db.close()


async def test_memory_writes_rejected_while_memory_candidates_apply_running(web_env):
    cookie = await _login_cookie(web_env)
    await web_env.db.conn.execute(
        "INSERT INTO operations (operation_uuid, chat_id, kind, status, detail_json, started_at) VALUES (?,?,?,?,?,?)",
        ("op-candidates-running", 123, "memory_candidates_apply", "running", "{}", 1),
    )
    await web_env.db.conn.commit()
    resp = await web_env.client.post(
        "/api/memory/docs",
        json={"name": "blocked-doc", "content": "blocked"},
        cookies={"openbear_web_session": cookie},
    )
    assert resp.status == 409



async def test_conversation_defaults_seed_partial_patch_and_failed_request(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    archived = await web_env.server._create_web_conversation(123, title="archive", model="openai/gpt")
    await MessageDAO(web_env.db).set_thinking_level(int(archived["internal_chat_id"]), "high")
    await web_env.db.conn.execute(
        "UPDATE web_conversations SET archived_at=1, updated_at=999 WHERE conversation_uuid=?",
        (archived["conversation_uuid"],),
    )
    recent = await web_env.server._create_web_conversation(123, title="recent", model="openai/cheap")
    recent_chat_id = int(recent["internal_chat_id"])
    await MessageDAO(web_env.db).set_thinking_level(recent_chat_id, "low")
    await web_env.db.conn.execute(
        "UPDATE web_conversations SET agent_model='', agent_think_level='medium', agent_fast_mode=0, updated_at=1000 WHERE conversation_uuid=?",
        (recent["conversation_uuid"],),
    )
    await web_env.db.conn.commit()

    seeded_resp = await web_env.client.get("/api/conversations/defaults", cookies=cookie)
    assert seeded_resp.status == 200
    seeded = (await seeded_resp.json())["defaults"]
    assert seeded == {
        "mainModel": "openai/cheap",
        "mainThinkingLevel": "low",
        "mainFastMode": False,
        "agentModel": "",
        "agentThinkLevel": "medium",
        "agentFastMode": False,
        "revision": 1,
        "updatedAt": seeded["updatedAt"],
    }

    patched_resp = await web_env.client.patch(
        "/api/conversations/defaults",
        json={"mainModel": "openai/gpt"},
        cookies=cookie,
    )
    assert patched_resp.status == 200
    patched = (await patched_resp.json())["defaults"]
    assert patched["mainModel"] == "openai/gpt"
    assert patched["mainThinkingLevel"] == "low"
    assert patched["agentModel"] == ""
    assert patched["agentThinkLevel"] == "medium"
    assert patched["agentFastMode"] is False
    assert patched["revision"] == 2

    failed = await web_env.client.patch(
        "/api/conversations/defaults",
        json={"mainFastMode": "yes"},
        cookies=cookie,
    )
    assert failed.status == 400
    after_failed = (await (await web_env.client.get("/api/conversations/defaults", cookies=cookie)).json())["defaults"]
    assert after_failed == patched


async def test_conversation_defaults_failed_first_patch_does_not_seed(web_env):
    # A direct session for another owner verifies both owner isolation and that
    # validation happens before the first persistent preference write.
    from app.web_console.auth_api import _sha256 as auth_sha256

    token = "owner-456-token"
    ts = now_ts()
    await web_env.db.conn.execute(
        "INSERT INTO web_sessions (session_token_hash, chat_id, created_at, expires_at, last_seen_at) VALUES (?,?,?,?,?)",
        (auth_sha256(token), 456, ts, ts + 3600, ts),
    )
    await web_env.db.conn.commit()
    cookie = {"openbear_web_session": token}
    failed = await web_env.client.patch(
        "/api/conversations/defaults",
        json={"mainModel": "missing/model"},
        cookies=cookie,
    )
    assert failed.status == 404
    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM web_conversation_defaults WHERE owner_chat_id=456"
    )
    assert int((await cur.fetchone())["n"]) == 0

    created = await web_env.client.post(
        "/api/conversations",
        json={
            "title": "完整配置",
            "runConfig": {
                "mainModel": "openai/gpt",
                "mainThinkingLevel": "high",
                "mainFastMode": True,
                "agentModel": "openai/cheap",
                "agentThinkLevel": "medium",
                "agentFastMode": False,
            },
        },
        cookies=cookie,
    )
    assert created.status == 200
    payload = await created.json()
    state = payload["state"]
    assert state["model"] == "openai/gpt"
    assert state["thinkingLevel"] == "high"
    assert state["fastMode"] is True
    assert state["agentRunConfig"]["model"] == "openai/cheap"
    assert state["agentRunConfig"]["thinkLevel"] == "medium"
    assert state["agentRunConfig"]["fastMode"] is False
    internal_chat_id = int(payload["conversation"]["internalChatId"])
    cur = await web_env.db.conn.execute(
        "SELECT thinking_level, fast_mode FROM sessions WHERE chat_id=?",
        (internal_chat_id,),
    )
    session_row = await cur.fetchone()
    assert (session_row["thinking_level"], int(session_row["fast_mode"])) == ("high", 1)
    cur = await web_env.db.conn.execute(
        "SELECT owner_chat_id, main_model, agent_model FROM web_conversation_defaults ORDER BY owner_chat_id"
    )
    rows = [dict(row) for row in await cur.fetchall()]
    assert any(row == {"owner_chat_id": 456, "main_model": "openai/gpt", "agent_model": "openai/cheap"} for row in rows)
    assert all(row["owner_chat_id"] != 123 for row in rows)


async def test_conversation_defaults_normalize_stale_models_and_capabilities(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    await web_env.db.conn.execute(
        """
        INSERT INTO web_conversation_defaults (
          owner_chat_id, main_model, main_thinking_level, main_fast_mode,
          agent_model, agent_think_level, agent_fast_mode, revision, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (123, "missing/main", "max", 1, "missing/agent", "high", 1, 7, 10),
    )
    await web_env.db.conn.commit()
    response = await web_env.client.get("/api/conversations/defaults", cookies=cookie)
    assert response.status == 200
    defaults = (await response.json())["defaults"]
    assert defaults == {
        "mainModel": "openai/gpt",
        "mainThinkingLevel": "medium",
        "mainFastMode": True,
        "agentModel": "",
        "agentThinkLevel": "high",
        "agentFastMode": True,
        "revision": 7,
        "updatedAt": 10,
    }

    await web_env.db.conn.execute(
        "UPDATE web_conversation_defaults SET main_model='openai/cheap', main_fast_mode=1, agent_model='openai/cheap', agent_fast_mode=1 WHERE owner_chat_id=123"
    )
    await web_env.db.conn.commit()
    response = await web_env.client.get("/api/conversations/defaults", cookies=cookie)
    defaults = (await response.json())["defaults"]
    assert defaults["mainModel"] == "openai/cheap"
    assert defaults["mainFastMode"] is False
    assert defaults["agentModel"] == "openai/cheap"
    assert defaults["agentThinkLevel"] == ""
    assert defaults["agentFastMode"] is None


async def test_real_conversation_main_config_success_syncs_defaults(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="sync-defaults", model="openai/gpt")
    uuid = row["conversation_uuid"]
    web_env.server.model_selection = SimpleNamespace(
        current="openai/gpt",
        family_of=lambda label: str(label).split("/", 1)[0],
    )

    thinking = await web_env.client.post(
        f"/api/conversations/{uuid}/thinking",
        json={"level": "high"},
        cookies=cookie,
    )
    assert thinking.status == 200
    fast = await web_env.client.post(
        f"/api/conversations/{uuid}/fast",
        json={"enabled": True},
        cookies=cookie,
    )
    assert fast.status == 200
    chat_id = int(row["internal_chat_id"])
    session_id = await MessageDAO(web_env.db).current_session_uuid(chat_id)
    await MessageDAO(web_env.db).save_controller_model_context(
        chat_id,
        conversation_uuid=uuid,
        session_id=session_id,
        protocol="responses",
        model="gpt",
        model_label="openai/gpt",
        state={"version": 1, "messages": []},
    )
    model = await web_env.client.post(
        f"/api/conversations/{uuid}/model",
        json={"model": "openai/cheap"},
        cookies=cookie,
    )
    assert model.status == 200
    cur = await web_env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM controller_model_contexts WHERE chat_id=?", (chat_id,)
    )
    assert int((await cur.fetchone())["n"]) == 0

    defaults = (await (await web_env.client.get("/api/conversations/defaults", cookies=cookie)).json())["defaults"]
    assert defaults["mainModel"] == "openai/cheap"
    assert defaults["mainThinkingLevel"] == "low"
    assert defaults["mainFastMode"] is False
    state = await (await web_env.client.get(f"/api/conversations/{uuid}/state", cookies=cookie)).json()
    assert state["model"] == "openai/cheap"
    assert state["thinkingLevel"] == "low"
    assert state["fastMode"] is False


async def test_conversation_agent_run_config_api_and_state(web_env):
    cookie = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="agent-run-config", model="openai/gpt")
    uuid = row["conversation_uuid"]

    # Ensure columns exist after migration.
    cur = await web_env.db.conn.execute("PRAGMA table_info(web_conversations)")
    cols = {str(r["name"] if "name" in r.keys() else r[1]) for r in await cur.fetchall()}
    assert "agent_model" in cols
    assert "agent_think_level" in cols
    assert "agent_fast_mode" in cols

    set_resp = await web_env.client.post(
        f"/api/conversations/{uuid}/agent-run-config",
        json={"model": "openai/cheap", "thinkLevel": "medium", "fastMode": False},
        cookies=cookie,
    )
    assert set_resp.status == 200
    body = await set_resp.json()
    assert body["ok"] is True
    assert body["agentRunConfig"]["model"] == "openai/cheap"
    assert body["agentRunConfig"]["thinkLevel"] == "medium"
    assert body["agentRunConfig"]["fastMode"] is False
    assert body["agentRunConfig"]["effective"]["model"] == "openai/cheap"
    assert body["agentRunConfig"]["effective"]["thinkLevel"] == "medium"
    assert body["agentRunConfig"]["effective"]["source"]["model"] == "conversation"
    defaults_resp = await web_env.client.get("/api/conversations/defaults", cookies=cookie)
    synced_defaults = (await defaults_resp.json())["defaults"]
    assert synced_defaults["agentModel"] == "openai/cheap"
    assert synced_defaults["agentThinkLevel"] == "medium"
    assert synced_defaults["agentFastMode"] is False

    state_resp = await web_env.client.get(f"/api/conversations/{uuid}/state", cookies=cookie)
    assert state_resp.status == 200
    state = await state_resp.json()
    assert state["ok"] is True
    assert state["agentRunConfig"]["model"] == "openai/cheap"
    assert state["agentRunConfig"]["effective"]["model"] == "openai/cheap"
    assert state["conversation"]["agentModel"] == "openai/cheap"
    assert state["conversation"]["agentThinkLevel"] == "medium"
    assert state["conversation"]["agentFastMode"] is False

    # Clear overrides back to follow main.
    clear_resp = await web_env.client.post(
        f"/api/conversations/{uuid}/agent-run-config",
        json={"model": "", "thinkLevel": "", "fastMode": None},
        cookies=cookie,
    )
    assert clear_resp.status == 200
    cleared = await clear_resp.json()
    assert cleared["agentRunConfig"]["model"] == ""
    assert cleared["agentRunConfig"]["thinkLevel"] == ""
    assert cleared["agentRunConfig"]["fastMode"] is None
    assert cleared["agentRunConfig"]["effective"]["model"] == "openai/gpt"
    assert cleared["agentRunConfig"]["effective"]["source"]["model"] == "main"


async def test_root_compaction_sources_emit_stable_unified_context_compaction_identity(web_env):
    class CapturingRenderer:
        def __init__(self) -> None:
            self.live = SimpleNamespace(conversation_uuid="conv-compaction")
            self.starts = []
            self.results = []

        async def on_tool_start(self, tool_call_id, name, arguments, line):
            self.starts.append((tool_call_id, name, json.loads(arguments), line))

        async def on_tool_result(self, tool_call_id, name, arguments, result, duration_ms):
            self.results.append((tool_call_id, name, json.loads(arguments), result, duration_ms))

    sources = ("pre_model_request", "tool_batch", "agent_result_preflight", "emergency")
    for summary_id, source in enumerate(sources, start=41):
        renderer = CapturingRenderer()
        summary = f"summary:{source}:" + ("x" * 200)
        outcome = CompactionOutcome(
            did=True,
            source=source,
            trigger_tokens=90_000,
            after_tokens=12_345,
            summary_id=summary_id,
            summary=summary,
            summary_tokens=500,
        )

        await web_env.server._emit_context_compaction_event(renderer, outcome, source=source)

        expected_id = f"context-compaction:{summary_id}"
        assert renderer.starts[0][0] == renderer.results[0][0] == expected_id
        start_metadata = renderer.starts[0][2]
        metadata = renderer.results[0][2]
        assert start_metadata["status"] == "running"
        assert start_metadata["outputAvailable"] is False
        assert metadata["compactionId"] == start_metadata["compactionId"] == expected_id
        assert metadata["summaryId"] == start_metadata["summaryId"] == summary_id
        assert metadata["scope"] == "root"
        assert metadata["source"] == source
        assert metadata["status"] == "completed"
        assert metadata["beforeTokens"] == 90_000
        assert metadata["afterTokens"] == 12_345
        assert metadata["summaryChars"] == len(summary)
        assert metadata["outputAvailable"] is True
        assert metadata["summaryRef"] == f"/api/conversations/conv-compaction/compactions/{summary_id}"
        assert summary in renderer.results[0][3]


async def test_root_compaction_summary_lazy_load_is_full_owned_and_legacy_projected(web_env):
    cookie = await _login_cookie(web_env)
    cookies = {"openbear_web_session": cookie}
    web_env.client.session.cookie_jar.clear()
    first = await web_env.server._create_web_conversation(123, title="compaction-owner")
    second = await web_env.server._create_web_conversation(123, title="compaction-other")
    first_uuid = str(first["conversation_uuid"])
    second_uuid = str(second["conversation_uuid"])
    first_chat = int(first["internal_chat_id"])
    anchor_id = await MessageDAO(web_env.db).add(
        first_chat,
        "user",
        "legacy compaction anchor",
        conversation_uuid=first_uuid,
        turn_uuid="turn-compaction-anchor",
        run_root_turn_uuid="turn-compaction-anchor",
    )
    full_summary = "## full private summary\n" + ("secret-tool-result\n" * 2500)
    assert len(full_summary) > 32_000
    summary_id = await SummaryDAO(web_env.db).add(first_chat, full_summary, anchor_id, 12_345)
    async with web_env.server._web_operation_lock(first_uuid):
        await web_env.server._publish_operation(
            first_uuid,
            internal_chat_id=first_chat,
            owner_chat_id=123,
            op_id="msg:compaction-anchor",
            op_type="user_message",
            action="create",
            turn_uuid="turn-compaction-anchor",
            run_root_turn_uuid="turn-compaction-anchor",
            payload={"text": "legacy compaction anchor"},
            status="completed",
            lifecycle="terminal",
            source="user",
        )

    operations_response = await web_env.client.get(
        f"/api/conversations/{first_uuid}/operations",
        cookies=cookies,
    )
    assert operations_response.status == 200
    operations = (await operations_response.json())["operations"]
    projected = next(op for op in operations if op["opId"] == f"tool:context-compaction:{summary_id}")
    assert projected["opType"] == "context_compaction"
    assert projected["turnUuid"] == "turn-compaction-anchor"
    assert projected["payload"]["compactionId"] == f"context-compaction:{summary_id}"
    assert projected["payload"]["summaryId"] == summary_id
    assert projected["payload"]["scope"] == "root"
    assert projected["payload"]["source"] == "legacy_summary"
    assert projected["payload"]["status"] == "completed"
    assert projected["payload"]["summaryChars"] == len(full_summary)
    assert projected["payload"]["outputAvailable"] is True
    assert projected["detailAvailable"] is True
    assert projected["detailLoaded"] is False
    assert "args" not in projected["payload"]
    assert "result" not in projected["payload"]
    assert len(projected["payload"]["outputPreview"]) <= 512
    repeated_response = await web_env.client.get(
        f"/api/conversations/{first_uuid}/operations",
        cookies=cookies,
    )
    repeated = [
        op for op in (await repeated_response.json())["operations"]
        if op["opId"] == f"tool:context-compaction:{summary_id}"
    ]
    assert len(repeated) == 1
    assert repeated[0]["displaySeq"] == projected["displaySeq"]

    owned = await web_env.client.get(
        f"/api/conversations/{first_uuid}/compactions/{summary_id}",
        cookies=cookies,
    )
    assert owned.status == 200
    body = await owned.json()
    assert body["compactedOutput"] == full_summary
    assert body["summaryChars"] == len(full_summary)
    assert body["outputAvailable"] is True

    await web_env.server._publish_operation(
        first_uuid,
        internal_chat_id=first_chat,
        owner_chat_id=123,
        op_id="manual-compact:test-visible-shape",
        op_type="context_compaction",
        action="end",
        payload={
            "compactionId": f"context-compaction:{summary_id}",
            "summaryId": summary_id,
            "scope": "root",
            "source": "manual",
            "status": "completed",
            "beforeTokens": 212_912,
            "afterTokens": 3_334,
            "summaryChars": len(full_summary),
            "outputAvailable": True,
            "outputPreview": full_summary[:12_000],
        },
        status="completed",
        lifecycle="terminal",
        source="manual",
        internal=False,
    )
    enriched = await web_env.client.get(
        f"/api/conversations/{first_uuid}/compactions/{summary_id}",
        cookies=cookies,
    )
    assert enriched.status == 200
    enriched_body = await enriched.json()
    assert enriched_body["source"] == "manual"
    assert enriched_body["beforeTokens"] == 212_912
    assert enriched_body["afterTokens"] == 3_334

    enriched_operations_response = await web_env.client.get(
        f"/api/conversations/{first_uuid}/operations",
        cookies=cookies,
    )
    enriched_operations = (await enriched_operations_response.json())["operations"]
    assert any(op["opId"] == "manual-compact:test-visible-shape" for op in enriched_operations)
    assert not any(op["opId"] == f"tool:context-compaction:{summary_id}" for op in enriched_operations)
    canonical_detail = await web_env.client.get(
        f"/api/conversations/{first_uuid}/operations/manual-compact%3Atest-visible-shape/detail",
        cookies=cookies,
    )
    assert canonical_detail.status == 200
    assert (await canonical_detail.json())["operation"]["opType"] == "context_compaction"

    cross_conversation = await web_env.client.get(
        f"/api/conversations/{second_uuid}/compactions/{summary_id}",
        cookies=cookies,
    )
    assert cross_conversation.status == 404
    unauthenticated = await web_env.client.get(
        f"/api/conversations/{first_uuid}/compactions/{summary_id}",
    )
    assert unauthenticated.status == 401


async def test_agent_final_compaction_output_stays_task_scoped_and_out_of_root_operations(web_env):
    cookie = await _login_cookie(web_env)
    cookies = {"openbear_web_session": cookie}
    web_env.client.session.cookie_jar.clear()
    owner = await web_env.server._create_web_conversation(123, title="agent compaction owner")
    other = await web_env.server._create_web_conversation(123, title="agent compaction other")
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=int(owner["internal_chat_id"]),
        parent_session_uuid=str(owner["conversation_uuid"]),
        workflow_uuid="wf-agent-compaction-output",
        title="agent compaction output",
        status="running",
    )
    compacted_output = "actual private task summary"
    await web_env.server.rath_dao.append_event(
        task_uuid,
        "model_context_pre_compacted",
        summary="Agent context compacted",
        detail={
            "final": True,
            "compactionId": f"agent-compaction:{task_uuid}:pre_compacted:stable",
            "summaryId": f"agent-compaction:{task_uuid}:pre_compacted:stable",
            "scope": "agent",
            "source": "pre_model_request",
            "status": "pre_compacted",
            "beforeTokens": 50_000,
            "afterTokens": 10_000,
            "summaryChars": len(compacted_output),
            "outputAvailable": True,
            "compactedOutput": compacted_output,
        },
    )
    root_compaction_id = "tool:context-compaction:root-only"
    async with web_env.server._web_operation_lock(str(owner["conversation_uuid"])):
        await web_env.server._publish_operation(
            str(owner["conversation_uuid"]),
            internal_chat_id=int(owner["internal_chat_id"]),
            owner_chat_id=123,
            op_id=root_compaction_id,
            op_type="context_compaction",
            action="end",
            turn_uuid="turn-root-compaction",
            run_root_turn_uuid="turn-root-compaction",
            payload={
                "compactionId": "context-compaction:root-only",
                "scope": "root",
                "status": "completed",
            },
            status="completed",
            lifecycle="terminal",
            source="context_compaction",
            internal=False,
        )

    path = f"/api/conversations/{owner['conversation_uuid']}/agents/{task_uuid}/events"
    response = await web_env.client.get(path, cookies=cookies)
    assert response.status == 200
    body = await response.json()
    assert body["taskUuid"] == task_uuid
    event = [item for item in body["events"] if item["kind"] == "model_context_pre_compacted"][-1]
    assert event["seq"] > 0
    assert event["detail"]["compactedOutput"] == compacted_output
    assert not any(
        item.get("detail", {}).get("compactionId") == "context-compaction:root-only"
        for item in body["events"]
    )

    cross = await web_env.client.get(
        f"/api/conversations/{other['conversation_uuid']}/agents/{task_uuid}/events",
        cookies=cookies,
    )
    assert cross.status == 404
    root = await web_env.client.get(
        f"/api/conversations/{owner['conversation_uuid']}/operations",
        cookies=cookies,
    )
    root_operations = (await root.json())["operations"]
    root_only = next(operation for operation in root_operations if operation["opId"] == root_compaction_id)
    assert root_only["opType"] == "context_compaction"
    assert root_only["taskUuid"] == ""
    assert root_only["payload"]["scope"] == "root"
    assert not any(
        (operation.get("payload") or {}).get("compactionId")
        == f"agent-compaction:{task_uuid}:pre_compacted:stable"
        for operation in root_operations
    )

async def test_manual_compact_exact_50_percent_boundary_and_unknown_usage(web_env, monkeypatch):
    cookies = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="manual boundary", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    url = f"/api/conversations/{row['conversation_uuid']}/compact"
    monkeypatch.setattr(web_env.server, "_model_compact_trigger_tokens", lambda _label: 1000)

    unknown = await web_env.client.post(url, cookies=cookies)
    assert unknown.status == 409
    assert (await unknown.json())["error"] == "context_usage_unknown"

    session_uuid = await MessageDAO(web_env.db).get_or_create_session_uuid(chat_id)
    await web_env.db.conn.execute(
        "INSERT INTO web_controller_context_snapshots(chat_id,session_uuid,summary_id,tokens,updated_at) VALUES(?,?,?,?,?)",
        (chat_id, session_uuid, 0, 499, now_ts()),
    )
    await web_env.db.conn.commit()
    below = await web_env.client.post(url, cookies=cookies)
    assert below.status == 409
    assert (await below.json())["error"] == "below_threshold"

    class FakeCompactor:
        async def _force_compact_unlocked(self, _chat_id, *, source=""):
            assert source == "manual"
            return CompactionOutcome(did=True, source=source)

    monkeypatch.setattr(web_env.server, "_make_web_compactor", lambda *_a, **_k: FakeCompactor())
    await web_env.db.conn.execute("UPDATE web_controller_context_snapshots SET tokens=500 WHERE chat_id=?", (chat_id,))
    await web_env.db.conn.commit()
    equal = await web_env.client.post(url, cookies=cookies)
    assert equal.status == 200
    equal_payload = await equal.json()
    assert equal_payload["outcome"]["did"] is True
    assert equal_payload["state"]["contextUsage"]["known"] is False
    cur = await web_env.db.conn.execute(
        "SELECT known, tokens FROM web_controller_context_snapshots WHERE chat_id=?",
        (chat_id,),
    )
    tombstone = await cur.fetchone()
    assert tombstone is not None
    assert int(tombstone["known"]) == 0
    assert int(tombstone["tokens"]) == 0


async def test_controller_snapshot_exact_provider_usage_isolated_and_invalidated_by_summary(web_env):
    row = await web_env.server._create_web_conversation(123, title="provider snapshot")
    chat_id = int(row["internal_chat_id"])
    dao = MessageDAO(web_env.db)
    session_uuid = await dao.get_or_create_session_uuid(chat_id)
    await web_env.server._persist_web_model_call_delta(
        dao, chat_id, session_uuid=session_uuid,
        call={"status": "ok", "usage": Usage(input_tokens=100, output_tokens=999, cache_read_tokens=30, cache_write_tokens=5), "promptUsageReported": True},
        model_cost={}, model_label="openai/gpt", protocol="chat", think_level="off",
    )
    assert await dao.latest_controller_context_usage(chat_id, session_uuid=session_uuid) == 135

    await web_env.server._persist_web_model_call_delta(
        dao, chat_id, session_uuid=session_uuid,
        call={"status": "ok", "usage": Usage(input_tokens=800, output_tokens=50, cache_read_tokens=20, cache_write_tokens=10), "promptUsageReported": True},
        model_cost={}, model_label="openai/cheap", protocol="chat", think_level="off", call_kind="context_compaction",
    )
    assert await dao.latest_controller_context_usage(chat_id, session_uuid=session_uuid) == 135
    assert (await dao.recent_model_calls(chat_id))[0].call_kind == "context_compaction"

    await SummaryDAO(web_env.db).add(chat_id, "new generation", 0, 1)
    assert await dao.latest_controller_context_usage(chat_id, session_uuid=session_uuid) is None


async def test_unknown_controller_usage_does_not_create_snapshot(web_env):
    row = await web_env.server._create_web_conversation(123, title="unknown provider usage")
    chat_id = int(row["internal_chat_id"])
    dao = MessageDAO(web_env.db)
    session_uuid = await dao.get_or_create_session_uuid(chat_id)
    await web_env.server._persist_web_model_call_delta(
        dao, chat_id, session_uuid=session_uuid,
        call={"status": "ok", "usage": Usage(input_tokens=123), "promptUsageReported": True}, model_cost={},
        model_label="openai/gpt", protocol="chat", think_level="off",
    )
    assert await dao.latest_controller_context_usage(chat_id, session_uuid=session_uuid) == 123
    await web_env.server._persist_web_model_call_delta(
        dao, chat_id, session_uuid=session_uuid,
        call={"status": "ok", "usage": Usage(), "promptUsageReported": False}, model_cost={},
        model_label="openai/gpt", protocol="chat", think_level="off",
    )
    assert await dao.latest_controller_context_usage(chat_id, session_uuid=session_uuid) is None
    cur = await web_env.db.conn.execute(
        "SELECT known, tokens FROM web_controller_context_snapshots WHERE chat_id=?",
        (chat_id,),
    )
    tombstone = await cur.fetchone()
    assert tombstone is not None
    assert int(tombstone["known"]) == 0
    assert int(tombstone["tokens"]) == 0


async def test_send_is_rejected_not_queued_while_chat_lock_is_held(web_env, monkeypatch):
    row = await web_env.server._create_web_conversation(123, title="send busy")
    chat_id = int(row["internal_chat_id"])
    called = False
    async def should_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"ok": True}
    monkeypatch.setattr(web_env.server, "_start_or_steer_web_conversation_locked", should_not_run)
    async with web_env.server.operation_locks.chat(chat_id, "web_manual_compact"):
        result = await asyncio.wait_for(
            web_env.server._start_or_steer_web_conversation(row, "must reject", [], web_env.server._live_for(row)),
            timeout=0.2,
        )
    assert result == {"ok": False, "error": "busy"}
    assert called is False


async def test_manual_compact_rejected_while_chat_lock_is_held(web_env):
    cookies = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="compact busy")
    chat_id = int(row["internal_chat_id"])
    async with web_env.server.operation_locks.chat(chat_id, "web_send"):
        response = await web_env.client.post(f"/api/conversations/{row['conversation_uuid']}/compact", cookies=cookies)
    assert response.status == 409
    assert (await response.json())["error"] == "busy"


def test_memory_reminder_defaults_are_english_and_zero_disables():
    cfg = _cfg()
    assert cfg.agent.memory_reminder_percent == 80
    assert cfg.agent.memory_reminder_prompt.isascii()
    assert "TaskMemory" in cfg.agent.memory_reminder_prompt
    disabled = cfg.model_copy(deep=True)
    disabled.agent.memory_reminder_percent = 0
    assert disabled.agent.memory_reminder_percent == 0

async def test_memory_reminder_delivery_deduplicates_generation_and_resets_after_compaction(web_env):
    row = await web_env.server._create_web_conversation(123, title="reminder generations")
    chat_id = int(row["internal_chat_id"])
    for _ in range(2):
        await web_env.db.conn.execute(
            "INSERT INTO web_memory_reminders(chat_id,session_uuid,summary_id,delivered_at) VALUES(?,?,?,?) ON CONFLICT(chat_id,summary_id) DO NOTHING",
            (chat_id, str(row["conversation_uuid"]), 0, now_ts()),
        )
    await web_env.db.conn.commit()
    cur = await web_env.db.conn.execute("SELECT summary_id FROM web_memory_reminders WHERE chat_id=?", (chat_id,))
    assert [int(r["summary_id"]) for r in await cur.fetchall()] == [0]

    summary_id = await SummaryDAO(web_env.db).add(chat_id, "compacted generation", 0, 1)
    await web_env.db.conn.execute(
        "INSERT INTO web_memory_reminders(chat_id,session_uuid,summary_id,delivered_at) VALUES(?,?,?,?) ON CONFLICT(chat_id,summary_id) DO NOTHING",
        (chat_id, str(row["conversation_uuid"]), summary_id, now_ts()),
    )
    await web_env.db.conn.commit()
    cur = await web_env.db.conn.execute("SELECT summary_id FROM web_memory_reminders WHERE chat_id=? ORDER BY summary_id", (chat_id,))
    assert [int(r["summary_id"]) for r in await cur.fetchall()] == [0, summary_id]


async def test_web_memory_reminder_is_xml_user_overlay_runtime_only_and_deduplicated(web_env, monkeypatch):
    async def _fake_system_prompt():
        return "stable reminder system"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.tools = ToolRegistry()
    row = await web_env.server._create_web_conversation(
        123,
        title="memory reminder overlay",
        model="openai/gpt",
    )
    chat_id = int(row["internal_chat_id"])
    conversation_uuid = str(row["conversation_uuid"])
    dao = MessageDAO(web_env.db)
    session_uuid = await dao.get_or_create_session_uuid(chat_id)
    await TaskMemoryDAO(web_env.db).create(
        conversation_uuid=conversation_uuid,
        scope_type=SCOPE_CONVERSATION,
        name="existing runtime state",
        description="must remain a separate trusted runtime unit",
    )
    await web_env.db.conn.execute(
        """INSERT INTO web_controller_context_snapshots(
               chat_id, session_uuid, summary_id, tokens, updated_at
           ) VALUES(?,?,?,?,?)""",
        (chat_id, session_uuid, 0, 72_000, now_ts()),
    )
    await web_env.db.conn.commit()

    first_backend = FakeStreamBackend([[
        StreamEvent(kind="usage", usage=Usage(input_tokens=72_500, output_tokens=20)),
        StreamEvent(kind="content", text="checkpoint complete"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    first_backend.protocol = "responses"
    web_env.server.llm_factory = FakeRunFactory(first_backend, context_window=128_000)
    live = _WebLiveStream(conversation_uuid, chat_id)
    await live.publish({"type": "accepted", "turnUuid": "reminder-turn-1"})

    assert await web_env.server._run_web_turn(
        chat_id,
        "preserve this request",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid="reminder-turn-1",
    ) is True

    first_request = first_backend.seen_convos[0]
    real_users = [
        message for message in first_request
        if message.get("role") == "user" and not is_task_memory_runtime_message(message)
    ]
    assert real_users
    injected_text = str(real_users[-1]["content"])
    assert '<openbear-memory-checkpoint version="1" runtime-only="true"' in injected_text
    assert 'latest_controller_prompt_tokens="72000"' in injected_text
    assert "Use Memory for stable" in injected_text
    assert "Use TaskMemory for independently useful working state" in injected_text
    runtime_units = [message for message in first_request if is_task_memory_runtime_message(message)]
    assert len(runtime_units) == 1
    assert "openbear-memory-checkpoint" not in str(runtime_units[0]["content"])
    visible_rows = await dao.recent(chat_id)
    assert "openbear-memory-checkpoint" not in json.dumps(
        [web_env.server._message_json(item) for item in visible_rows],
        ensure_ascii=False,
    )
    cur = await web_env.db.conn.execute(
        "SELECT session_uuid, summary_id FROM web_memory_reminders WHERE chat_id=?",
        (chat_id,),
    )
    delivered = await cur.fetchone()
    assert delivered is not None
    assert str(delivered["session_uuid"]) == session_uuid
    assert int(delivered["summary_id"]) == 0

    second_backend = FakeStreamBackend([[
        StreamEvent(kind="usage", usage=Usage(input_tokens=73_000, output_tokens=20)),
        StreamEvent(kind="content", text="second turn"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    second_backend.protocol = "responses"
    web_env.server.llm_factory = FakeRunFactory(second_backend, context_window=128_000)
    await live.publish({"type": "accepted", "turnUuid": "reminder-turn-2"})
    assert await web_env.server._run_web_turn(
        chat_id,
        "continue without duplicate reminder",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid="reminder-turn-2",
    ) is True
    assert "openbear-memory-checkpoint" not in json.dumps(
        second_backend.seen_convos[0],
        ensure_ascii=False,
        default=str,
    )


async def test_controller_context_tracking_is_bound_to_session_and_reset_lifecycle(web_env):
    row = await web_env.server._create_web_conversation(123, title="context session lifecycle")
    chat_id = int(row["internal_chat_id"])
    dao = MessageDAO(web_env.db)
    old_session = await dao.get_or_create_session_uuid(chat_id)
    await web_env.db.conn.execute(
        "INSERT INTO web_controller_context_snapshots(chat_id,session_uuid,summary_id,tokens,updated_at) VALUES(?,?,?,?,?)",
        (chat_id, old_session, 0, 321, now_ts()),
    )
    await web_env.db.conn.execute(
        "INSERT INTO web_memory_reminders(chat_id,session_uuid,summary_id,delivered_at) VALUES(?,?,?,?)",
        (chat_id, old_session, 0, now_ts()),
    )
    await web_env.db.conn.commit()

    await dao.reset_turn_stats(chat_id)
    new_session = await dao.get_or_create_session_uuid(chat_id)
    assert new_session != old_session
    assert await dao.latest_controller_context_usage(chat_id, session_uuid=new_session) is None
    for table in ("web_controller_context_snapshots", "web_memory_reminders"):
        cur = await web_env.db.conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE chat_id=?", (chat_id,))
        assert int((await cur.fetchone())["count"] or 0) == 0


async def test_effective_compact_trigger_falls_back_to_context_window_ratio(web_env):
    resolved = web_env.server.config.models.resolve("openai/gpt")
    assert resolved is not None and int(resolved[1].compact_trigger_tokens or 0) == 0
    assert web_env.server._model_compact_trigger_tokens("openai/gpt") == 89_600


async def test_manual_compaction_broadcasts_standard_lifecycle_and_blocks_delete(web_env, monkeypatch):
    cookies = {"openbear_web_session": await _login_cookie(web_env)}
    row = await web_env.server._create_web_conversation(123, title="manual live lifecycle", model="openai/gpt")
    chat_id = int(row["internal_chat_id"])
    session_uuid = await MessageDAO(web_env.db).get_or_create_session_uuid(chat_id)
    await web_env.db.conn.execute(
        "INSERT INTO web_controller_context_snapshots(chat_id,session_uuid,summary_id,tokens,updated_at) VALUES(?,?,?,?,?)",
        (chat_id, session_uuid, 0, 500, now_ts()),
    )
    await web_env.db.conn.commit()
    monkeypatch.setattr(web_env.server, "_model_compact_trigger_tokens", lambda _label: 1000)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingCompactor:
        async def _force_compact_unlocked(self, _chat_id, *, source=""):
            assert source == "manual"
            started.set()
            await release.wait()
            return CompactionOutcome(did=False, source=source, reason="no_history")

    monkeypatch.setattr(web_env.server, "_make_web_compactor", lambda *_a, **_k: BlockingCompactor())
    live = web_env.server._live_for(row)
    sub = live.subscribe()
    compact_task = asyncio.create_task(
        web_env.client.post(
            f"/api/conversations/{row['conversation_uuid']}/compact",
            cookies=cookies,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        start_event = await asyncio.wait_for(sub.get(), timeout=1)
        start_frames = start_event.get("_webFrames") or []
        assert start_frames and start_frames[-1]["action"] == "start"
        assert start_frames[-1]["opType"] == "context_compaction"
        cur = await web_env.db.conn.execute(
            "SELECT lifecycle, status, internal FROM web_operations WHERE conversation_uuid=? AND op_type='context_compaction'",
            (str(row["conversation_uuid"]),),
        )
        active = await cur.fetchone()
        assert active is not None and str(active["lifecycle"]) == "active"
        assert int(active["internal"] or 0) == 0

        delete_response = await web_env.client.delete(
            f"/api/conversations/{row['conversation_uuid']}",
            cookies=cookies,
        )
        assert delete_response.status == 409
        assert (await delete_response.json())["error"] == "conversation_delete_busy"
    finally:
        release.set()
    response = await asyncio.wait_for(compact_task, timeout=2)
    assert response.status == 200
    end_event = await asyncio.wait_for(sub.get(), timeout=1)
    end_frames = end_event.get("_webFrames") or []
    assert end_frames and end_frames[-1]["action"] == "end"
    cur = await web_env.db.conn.execute(
        "SELECT lifecycle, status FROM web_operations WHERE conversation_uuid=? AND op_type='context_compaction'",
        (str(row["conversation_uuid"]),),
    )
    terminal = await cur.fetchone()
    assert terminal is not None and str(terminal["lifecycle"]) == "terminal"
    live.unsubscribe(sub)


async def test_context_unknown_tombstone_blocks_legacy_fallback_after_suffix_delete(web_env):
    row = await web_env.server._create_web_conversation(123, title="suffix context invalidation")
    chat_id = int(row["internal_chat_id"])
    dao = MessageDAO(web_env.db)
    session_uuid = await dao.get_or_create_session_uuid(chat_id)
    first_message_id = await dao.add(chat_id, "user", "delete this turn")
    await web_env.server._persist_web_model_call_delta(
        dao,
        chat_id,
        session_uuid=session_uuid,
        call={
            "status": "ok",
            "usage": Usage(input_tokens=700),
            "promptUsageReported": True,
        },
        model_cost={},
        model_label="openai/gpt",
        protocol="chat",
        think_level="off",
    )
    assert await dao.latest_controller_context_usage(chat_id, session_uuid=session_uuid) == 700

    deleted = await dao.delete_from_message_id(chat_id, first_message_id)

    assert deleted["messages"] == 1
    assert await dao.latest_controller_context_usage(chat_id, session_uuid=session_uuid) is None
    cur = await web_env.db.conn.execute(
        "SELECT known, summary_id FROM web_controller_context_snapshots WHERE chat_id=?",
        (chat_id,),
    )
    tombstone = await cur.fetchone()
    assert tombstone is not None
    assert int(tombstone["known"]) == 0
    assert int(tombstone["summary_id"]) == 0
    assert len(await dao.recent_model_calls(chat_id)) == 1


async def test_memory_reminder_defers_safe_normal_compaction_until_provider_delivery(web_env, monkeypatch):
    async def _fake_system_prompt():
        return "stable reminder system"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    web_env.server.tools = ToolRegistry()
    row = await web_env.server._create_web_conversation(
        123,
        title="memory before normal compaction",
        model="openai/gpt",
    )
    chat_id = int(row["internal_chat_id"])
    conversation_uuid = str(row["conversation_uuid"])
    session_uuid = await MessageDAO(web_env.db).get_or_create_session_uuid(chat_id)
    await web_env.db.conn.execute(
        """INSERT INTO web_controller_context_snapshots(
               chat_id, session_uuid, summary_id, known, tokens, updated_at
           ) VALUES(?,?,?,?,?,?)""",
        (chat_id, session_uuid, 0, 1, 72_000, now_ts()),
    )
    await web_env.db.conn.commit()

    backend = FakeStreamBackend([[
        StreamEvent(kind="usage", usage=Usage(input_tokens=95_000, output_tokens=20)),
        StreamEvent(kind="content", text="checkpoint delivered before compaction"),
        StreamEvent(kind="finish", finish_reason="stop"),
    ]])
    backend.protocol = "responses"
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128_000)
    monkeypatch.setattr(web_env.server, "_estimate_prompt_tokens", lambda **_kwargs: 95_000)

    async def _unexpected_preflight(*_args, **_kwargs):
        raise AssertionError("safe reminder delivery must defer root preflight compaction")

    async def _no_post_compaction(_chat_id, _tokens, **kwargs):
        return CompactionOutcome(did=False, source=str(kwargs.get("source") or "turn_epilogue"))

    def _unexpected_compactor(*_args, **_kwargs):
        raise AssertionError("Agent pre-model gate must defer normal compaction")

    monkeypatch.setattr(web_env.server, "_pre_compact_before_web_turn", _unexpected_preflight)
    monkeypatch.setattr(web_env.server, "_post_compact_after_web_turn", _no_post_compaction)
    monkeypatch.setattr(web_env.server, "_make_web_compactor", _unexpected_compactor)
    live = _WebLiveStream(conversation_uuid, chat_id)
    await live.publish({"type": "accepted", "turnUuid": "memory-before-compact-turn"})

    assert await web_env.server._run_web_turn(
        chat_id,
        "preserve state first",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid="memory-before-compact-turn",
    ) is True

    outbound = str(backend.seen_convos[0])
    assert '<openbear-memory-checkpoint version="1" runtime-only="true"' in outbound
    cur = await web_env.db.conn.execute(
        "SELECT summary_id FROM web_memory_reminders WHERE chat_id=? AND session_uuid=?",
        (chat_id, session_uuid),
    )
    delivered = await cur.fetchone()
    assert delivered is not None
    assert int(delivered["summary_id"]) == 0


async def test_memory_reminder_arms_inside_tool_loop_before_normal_compaction(web_env, monkeypatch):
    async def _fake_system_prompt():
        return "stable reminder system"

    monkeypatch.setattr(web_env.server, "_build_system_prompt_for_chat", _fake_system_prompt)
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    registry = ToolRegistry()

    async def _echo(args):
        return f"echo:{args.get('value', '')}"

    registry.add("echo", "echo", {"type": "object", "properties": {}}, _echo)
    web_env.server.tools = registry
    row = await web_env.server._create_web_conversation(
        123,
        title="memory threshold crossed in tool loop",
        model="openai/gpt",
    )
    chat_id = int(row["internal_chat_id"])
    conversation_uuid = str(row["conversation_uuid"])
    session_uuid = await MessageDAO(web_env.db).get_or_create_session_uuid(chat_id)
    await web_env.db.conn.execute(
        """INSERT INTO web_controller_context_snapshots(
               chat_id, session_uuid, summary_id, known, tokens, updated_at
           ) VALUES(?,?,?,?,?,?)""",
        (chat_id, session_uuid, 0, 1, 70_000, now_ts()),
    )
    await web_env.db.conn.commit()

    backend = FakeStreamBackend([
        [
            StreamEvent(kind="usage", usage=Usage(input_tokens=72_000, output_tokens=20)),
            StreamEvent(
                kind="tool_call",
                tool_calls=[ToolCall(id="memory-crossing-tool", name="echo", arguments='{"value":"ok"}')],
            ),
            StreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(kind="usage", usage=Usage(input_tokens=95_000, output_tokens=20)),
            StreamEvent(kind="content", text="continued after checkpoint"),
            StreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    backend.protocol = "responses"
    web_env.server.llm_factory = FakeRunFactory(backend, context_window=128_000)

    def _estimate_prompt_tokens(*, system="", convo=None):
        items = list(convo or [])
        return 95_000 if any(item.get("role") == "tool" for item in items) else 70_000

    async def _no_post_compaction(_chat_id, _tokens, **kwargs):
        return CompactionOutcome(did=False, source=str(kwargs.get("source") or "turn_epilogue"))

    monkeypatch.setattr(web_env.server, "_estimate_prompt_tokens", _estimate_prompt_tokens)
    monkeypatch.setattr(web_env.server, "_post_compact_after_web_turn", _no_post_compaction)
    live = _WebLiveStream(conversation_uuid, chat_id)
    await live.publish({"type": "accepted", "turnUuid": "memory-tool-loop-turn"})

    assert await web_env.server._run_web_turn(
        chat_id,
        "cross the reminder threshold",
        _WebStreamRenderer(live),
        conversation=row,
        root_turn_uuid="memory-tool-loop-turn",
    ) is True

    assert len(backend.seen_convos) == 2
    assert "openbear-memory-checkpoint" not in str(backend.seen_convos[0])
    assert '<openbear-memory-checkpoint version="1" runtime-only="true"' in str(backend.seen_convos[1])
    cur = await web_env.db.conn.execute(
        "SELECT summary_id FROM web_memory_reminders WHERE chat_id=? AND session_uuid=?",
        (chat_id, session_uuid),
    )
    delivered = await cur.fetchone()
    assert delivered is not None
    assert int(delivered["summary_id"]) == 0


async def test_questionnaire_rejects_invalid_definitions_without_pending(web_env):
    row = await web_env.server._create_web_conversation(123, title="invalid questionnaire")
    conv_uuid = row["conversation_uuid"]
    invalid_question_sets = [
        [],
        [{"id": "", "type": "open", "question": "Q"}],
        [{"id": "q", "type": "open", "question": "   "}],
        [{"id": "q", "type": "bad", "question": "Q"}],
        [{"id": "q", "type": "choice", "question": "Q", "options": []}],
        [{"id": "q", "type": "open", "question": "Q"}, {"id": "q", "type": "open", "question": "Q2"}],
        [{"id": "q", "type": "choice", "question": "Q", "options": [
            {"label": "A", "value": "x"}, {"label": "B", "value": "x"},
        ]}],
        [{"id": "q", "type": "choice", "question": "Q", "options": [{"label": "A", "value": "a"}],
          "recommendation": {"values": ["missing"], "reason": "why"}}],
    ]
    for questions in invalid_question_sets:
        result = await web_env.server._web_confirm(conv_uuid, {
            "action": "questionnaire", "title": "澄清", "body": "回答", "questions": questions,
        })
        assert result["status"] == "error"
        assert result["error"] == "invalid_questionnaire"
        assert web_env.server._pending_web_confirmations(conv_uuid) == []


async def test_questionnaire_pending_http_answer_is_canonical_and_text_faithful(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="questionnaire")
    conv_uuid = row["conversation_uuid"]
    questions = [
        {"id": "choice", "type": "choice", "question": "方向？", "description": "题目说明", "required": True,
         "multiple": True, "options": [
             {"label": "甲", "value": "a", "description": "选项说明"}, {"label": "乙", "value": "b"}],
         "recommendation": {"values": ["a"], "reason": "只提示"}},
        {"id": "open", "type": "open", "question": "边界？", "required": False},
        {"id": "empty", "type": "open", "question": "其他？", "required": False},
    ]
    task = asyncio.create_task(web_env.server._web_confirm(conv_uuid, {
        "action": "questionnaire", "title": "澄清", "body": "回答", "questions": questions,
    }))
    await asyncio.sleep(0)
    pending = web_env.server._pending_web_confirmations(conv_uuid)
    assert len(pending) == 1
    assert pending[0]["questions"] == questions
    state = await web_env.server._chat_payload(int(row["internal_chat_id"]), row)
    assert state["pendingConfirmations"][0]["confirmationId"] == pending[0]["confirmationId"]
    assert state["pendingConfirmations"][0]["questions"] == questions

    cid = pending[0]["confirmationId"]
    free_text = "  用户原文\n必须逐字保留  "
    response = await web_env.client.post(
        f"/api/conversations/{conv_uuid}/confirmations/{cid}/answer",
        json={"cancelled": False, "answers": [
            {"questionId": "open", "selectedValues": [], "text": "开放答案"},
            {"questionId": "choice", "selectedValues": ["b"], "text": free_text},
            {"questionId": "empty", "selectedValues": [], "text": ""},
        ]}, cookies={"openbear_web_session": cookie},
    )
    assert response.status == 200
    result = await task
    assert [answer["questionId"] for answer in result["answers"]] == ["choice", "open", "empty"]
    assert [answer["answerMode"] for answer in result["answers"]] == ["options_with_text", "text", "unanswered"]
    assert result["answers"][0]["selectedLabels"] == ["乙"]
    assert result["answers"][0]["text"] == free_text


@pytest.mark.parametrize("bad_answer", [
    [{"questionId": "missing", "selectedValues": [], "text": "x"}],
    [{"questionId": "single", "selectedValues": ["a"], "text": ""}, {"questionId": "single", "selectedValues": [], "text": "x"}],
    [{"questionId": "single", "selectedValues": ["missing"], "text": ""}],
    [{"questionId": "single", "selectedValues": ["a", "a"], "text": ""}],
    [{"questionId": "single", "selectedValues": ["a", "b"], "text": ""}],
    [{"questionId": "open", "selectedValues": ["a"], "text": "x"}],
    [{"questionId": "open", "selectedValues": [], "text": ""}],
])
async def test_questionnaire_http_rejects_invalid_answers_and_keeps_pending(web_env, bad_answer):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="bad questionnaire answer")
    conv_uuid = row["conversation_uuid"]
    task = asyncio.create_task(web_env.server._web_confirm(conv_uuid, {
        "action": "questionnaire", "title": "澄清", "body": "回答", "questions": [
            {"id": "single", "type": "choice", "question": "单选", "required": False, "multiple": False,
             "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}]},
            {"id": "open", "type": "open", "question": "必答开放题", "required": True},
        ],
    }))
    await asyncio.sleep(0)
    cid = web_env.server._pending_web_confirmations(conv_uuid)[0]["confirmationId"]
    response = await web_env.client.post(
        f"/api/conversations/{conv_uuid}/confirmations/{cid}/answer",
        json={"cancelled": False, "answers": bad_answer}, cookies={"openbear_web_session": cookie},
    )
    assert response.status == 400
    assert (await response.json())["error"] == "invalid_questionnaire_answer"
    assert web_env.server._pending_web_confirmations(conv_uuid)[0]["confirmationId"] == cid
    web_env.server._web_confirmations[cid]["future"].set_result({
        "status": "cancelled", "cancelled": True, "answers": [], "interactionId": cid,
    })
    assert (await task)["answers"] == []


async def test_questionnaire_text_only_cancel_and_timeout_never_use_recommendation(web_env):
    cookie = await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123, title="questionnaire terminal states")
    conv_uuid = row["conversation_uuid"]
    payload = {"action": "questionnaire", "title": "澄清", "body": "回答", "questions": [{
        "id": "required-choice", "type": "choice", "question": "方向", "required": True,
        "options": [{"label": "推荐项", "value": "recommended"}],
        "recommendation": {"values": ["recommended"], "reason": "仅提示"},
    }]}

    text_task = asyncio.create_task(web_env.server._web_confirm(conv_uuid, payload))
    await asyncio.sleep(0)
    cid = web_env.server._pending_web_confirmations(conv_uuid)[0]["confirmationId"]
    response = await web_env.client.post(
        f"/api/conversations/{conv_uuid}/confirmations/{cid}/answer",
        json={"cancelled": False, "answers": [{"questionId": "required-choice", "selectedValues": [], "text": "只填文字"}]},
        cookies={"openbear_web_session": cookie},
    )
    assert response.status == 200
    text_result = await text_task
    assert text_result["answers"][0]["answerMode"] == "text"
    assert text_result["answers"][0]["selectedValues"] == []

    cancel_task = asyncio.create_task(web_env.server._web_confirm(conv_uuid, payload))
    await asyncio.sleep(0)
    cid = web_env.server._pending_web_confirmations(conv_uuid)[0]["confirmationId"]
    response = await web_env.client.post(
        f"/api/conversations/{conv_uuid}/confirmations/{cid}/answer",
        json={"cancelled": True}, cookies={"openbear_web_session": cookie},
    )
    assert response.status == 200
    assert (await cancel_task)["answers"] == []

    timeout_result = await web_env.server._web_confirm(conv_uuid, {**payload, "timeoutSeconds": 0.01})
    assert timeout_result["status"] == "timeout"
    assert timeout_result["cancelled"] is True
    assert timeout_result["answers"] == []


async def test_user_interaction_live_start_result_is_one_typed_durable_operation_and_lazy_detail(web_env):
    cookie = await _login_cookie(web_env)
    cookies = {"openbear_web_session": cookie}
    row = await web_env.server._create_web_conversation(123, title="retained interaction")
    conv_uuid = str(row["conversation_uuid"])
    live = web_env.server._live_for(row)
    arguments = json.dumps({
        "action": "questionnaire", "title": "当时需求标题", "body": "请逐题回答",
        "questions": [{"id": "scope", "type": "choice", "question": "范围？",
                       "options": [{"label": "甲", "value": "a"}]}],
    }, ensure_ascii=False)
    result = json.dumps({
        "status": "answered", "answers": [{"questionId": "scope", "selectedValues": ["a"], "text": "并补充文字"}],
    }, ensure_ascii=False)

    await live.publish({"type": "accepted", "turnUuid": "turn-ui", "runUuid": "run-ui"})
    started = await live.publish({
        "type": "tool_start", "turnUuid": "turn-ui", "toolCallId": "ui-retained",
        "name": "UserInteraction", "arguments": arguments, "line": "waiting",
    })
    start_ops = [op for op in await web_env.server._web_operations(conv_uuid) if op["opId"] == "tool:ui-retained"]
    assert len(start_ops) == 1
    start = start_ops[0]
    assert (start["opType"], start["source"], start["status"], start["payload"]["interactionStatus"]) == (
        "user_interaction", "user_interaction", "running", "pending",
    )
    display_seq = start["displaySeq"]
    revision = start["revision"]

    terminal = await live.publish({
        "type": "tool_result", "turnUuid": "turn-ui", "toolCallId": "ui-retained",
        "name": "UserInteraction", "arguments": arguments, "result": result,
    })
    assert terminal["frameSeq"] > started["frameSeq"]
    operations = [op for op in await web_env.server._web_operations(conv_uuid) if op["opId"] == "tool:ui-retained"]
    assert len(operations) == 1
    completed = operations[0]
    assert completed["displaySeq"] == display_seq
    assert completed["revision"] > revision
    assert completed["status"] == "completed"
    assert completed["payload"]["interactionStatus"] == "answered"

    frames = [frame for frame in await web_env.server._web_frames(conv_uuid) if frame["opId"] == "tool:ui-retained"]
    assert [frame["action"] for frame in frames] == ["start", "end"]
    assert len({frame["displaySeq"] for frame in frames}) == 1
    assert [frame["revision"] for frame in frames] == sorted({frame["revision"] for frame in frames})

    list_response = await web_env.client.get(f"/api/conversations/{conv_uuid}/operations", cookies=cookies)
    assert list_response.status == 200
    listed = [op for op in (await list_response.json())["operations"] if op["opId"] == "tool:ui-retained"]
    assert len(listed) == 1
    summary = listed[0]
    assert {key: summary["payload"][key] for key in (
        "action", "title", "status", "interactionStatus", "sensitive",
    )} == {
        "action": "questionnaire", "title": "当时需求标题", "status": "completed",
        "interactionStatus": "answered", "sensitive": False,
    }
    assert "confirmed" not in summary["payload"]
    assert not ({"body", "questions", "options", "selected", "value", "result", "preview", "searchText"} & set(summary["payload"]))
    assert summary["detailLoaded"] is False
    detail_response = await web_env.client.get(
        f"/api/conversations/{conv_uuid}/operations/tool:ui-retained/detail", cookies=cookies,
    )
    assert detail_response.status == 200
    detail = (await detail_response.json())["operation"]
    assert detail["payload"]["arguments"] == arguments
    assert detail["payload"]["result"] == result


async def test_sensitive_prompt_answer_audit_and_all_public_operation_surfaces_hide_secret(web_env):
    cookie = await _login_cookie(web_env)
    cookies = {"openbear_web_session": cookie}
    row = await web_env.server._create_web_conversation(123, title="sensitive interaction")
    conv_uuid = str(row["conversation_uuid"])
    secret = "FULL-CHAIN-SECRET-8c12d7"
    arguments = json.dumps({
        "action": "prompt", "title": "凭据", "body": "请输入凭据", "sensitive": True,
        "defaultValue": secret,
    }, ensure_ascii=False)
    redacted_arguments = redact_tool_arguments_for_audit("UserInteraction", arguments)
    raw_result = json.dumps({"status": "answered", "value": secret}, ensure_ascii=False)
    redacted_result = redact_tool_result_for_audit("UserInteraction", raw_result, arguments)
    live = web_env.server._live_for(row)
    await live.publish({"type": "accepted", "turnUuid": "turn-sensitive"})
    await live.publish({"type": "tool_start", "turnUuid": "turn-sensitive", "toolCallId": "ui-sensitive",
                        "name": "UserInteraction", "arguments": redacted_arguments, "line": "[敏感内容已隐藏]"})
    await live.publish({"type": "tool_result", "turnUuid": "turn-sensitive", "toolCallId": "ui-sensitive",
                        "name": "UserInteraction", "arguments": redacted_arguments, "result": redacted_result})

    public_surfaces = []
    public_surfaces.append(await web_env.server._chat_payload(int(row["internal_chat_id"]), row))
    public_surfaces.append(await web_env.server._web_operations(conv_uuid))
    public_surfaces.append(await web_env.server._web_frames(conv_uuid))
    response = await web_env.client.get(f"/api/conversations/{conv_uuid}/operations", cookies=cookies)
    public_surfaces.append(await response.json())
    response = await web_env.client.get(f"/api/conversations/{conv_uuid}/operations/tool:ui-sensitive/detail", cookies=cookies)
    public_surfaces.append(await response.json())
    serialized_surfaces = [json.dumps(surface, ensure_ascii=False) for surface in public_surfaces]
    assert all(secret not in serialized for serialized in serialized_surfaces)
    # Summary APIs carry only the sensitive flag; full state/detail/frame projections
    # carry the placeholder consumed by the frontend hard guard.
    assert sum("[敏感内容已隐藏]" in serialized for serialized in serialized_surfaces) >= 4

    cur = await web_env.db.conn.execute(
        "SELECT payload_json, debug_json FROM web_event_frames WHERE conversation_uuid=? AND op_id=?",
        (conv_uuid, "tool:ui-sensitive"),
    )
    persisted_frames = json.dumps([dict(item) for item in await cur.fetchall()], ensure_ascii=False)
    assert secret not in persisted_frames
    assert "[敏感内容已隐藏]" in persisted_frames
