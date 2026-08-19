from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.db.engine import DB
from app.rath.dao import RathDAO
from app.web_console.core import _WEB_SESSION_KEY, WebSession
from app.web_console.task_memory_api import WebAdminTaskMemoryMixin


class _TaskMemoryApiHarness(WebAdminTaskMemoryMixin):
    def __init__(self, db: DB) -> None:
        self.db = db
        self.rath_dao = RathDAO(db)
        self.audit_rows: list[dict] = []

    async def _conversation_from_request(self, request: web.Request):
        session = request[_WEB_SESSION_KEY]
        conversation_uuid = str(request.match_info.get("conversation_uuid") or "")
        if session.chat_id != 42 or conversation_uuid != "conv-1":
            raise web.HTTPNotFound(text="conversation_not_found")
        return {
            "conversation_uuid": "conv-1",
            "owner_chat_id": 42,
            "internal_chat_id": 4201,
        }

    async def _json_body(self, request: web.Request):
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    async def audit(self, kind: str, **kwargs):
        self.audit_rows.append({"kind": kind, **kwargs})


@pytest.fixture
async def task_memory_api(tmp_path):
    db = DB(str(tmp_path / "task-memory-api.db"))
    await db.connect()
    harness = _TaskMemoryApiHarness(db)

    @web.middleware
    async def session_middleware(request, handler):
        request[_WEB_SESSION_KEY] = WebSession(chat_id=42, expires_at=2**31)
        return await handler(request)

    app = web.Application(middlewares=[session_middleware])
    app.add_routes([
        web.get("/api/conversations/{conversation_uuid}/task-memories", harness.handle_api_task_memories),
        web.get("/api/conversations/{conversation_uuid}/task-memories/tasks", harness.handle_api_task_memory_tasks),
        web.get("/api/conversations/{conversation_uuid}/task-memories/preview", harness.handle_api_task_memory_preview),
        web.get("/api/conversations/{conversation_uuid}/task-memories/{memory_uuid}", harness.handle_api_task_memory_detail),
        web.post("/api/conversations/{conversation_uuid}/task-memories", harness.handle_api_task_memory_create),
        web.patch("/api/conversations/{conversation_uuid}/task-memories/{memory_uuid}", harness.handle_api_task_memory_update),
        web.delete("/api/conversations/{conversation_uuid}/task-memories/{memory_uuid}", harness.handle_api_task_memory_delete),
        web.post("/api/conversations/{conversation_uuid}/task-memories/{memory_uuid}/restore", harness.handle_api_task_memory_restore),
    ])
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, harness
    await client.close()
    await db.close()


async def test_task_memory_web_api_projection_pagination_cas_and_soft_restore(task_memory_api):
    client, harness = task_memory_api
    response = await client.post("/api/conversations/conv-1/task-memories", json={
        "scopeType": "conversation",
        "name": "Deploy constraint",
        "description": "No restart",
        "body": "sensitive-body-not-for-list-or-audit",
        "autoReinjectCatalog": True,
        "visibleToAgents": True,
        "idempotencyKey": "web-create-1",
    })
    assert response.status == 201
    item = (await response.json())["memory"]

    second = await client.post("/api/conversations/conv-1/task-memories", json={
        "scopeType": "conversation", "name": "Second", "body": "second-body",
    })
    assert second.status == 201
    approved_sensitive_value = "password: WEB-USER-APPROVED-VALUE"
    approved = await client.post("/api/conversations/conv-1/task-memories", json={
        "scopeType": "conversation", "name": "User approved credential", "body": approved_sensitive_value,
    })
    assert approved.status == 201
    listing = await client.get(
        "/api/conversations/conv-1/task-memories",
        params={"scopeType": "conversation", "offset": 0, "limit": 1},
    )
    payload = await listing.json()
    assert listing.status == 200
    assert payload["total"] == 3 and len(payload["items"]) == 1
    assert "body" not in payload["items"][0]

    detail = await client.get(
        f"/api/conversations/conv-1/task-memories/{item['memoryUuid']}",
        params={"scopeType": "conversation"},
    )
    assert (await detail.json())["memory"]["body"] == "sensitive-body-not-for-list-or-audit"

    updated_body = "updated-web-body-not-in-audit"
    updated = await client.patch(
        f"/api/conversations/conv-1/task-memories/{item['memoryUuid']}",
        json={
            "scopeType": "conversation", "revision": 1,
            "description": "updated-description-not-in-audit", "body": updated_body,
        },
    )
    assert updated.status == 200
    updated_item = (await updated.json())["memory"]
    assert updated_item["revision"] == 2

    stale = await client.patch(
        f"/api/conversations/conv-1/task-memories/{item['memoryUuid']}",
        json={"scopeType": "conversation", "revision": 1, "body": "stale-value-not-in-audit"},
    )
    assert stale.status == 409
    assert (await stale.json())["error"] == "task_memory_stale_revision"

    deleted = await client.delete(
        f"/api/conversations/conv-1/task-memories/{item['memoryUuid']}",
        json={"scopeType": "conversation", "revision": updated_item["revision"]},
    )
    assert deleted.status == 200
    deleted_item = (await deleted.json())["memory"]
    assert deleted_item["deletedAt"] > 0
    restored = await client.post(
        f"/api/conversations/conv-1/task-memories/{item['memoryUuid']}/restore",
        json={"scopeType": "conversation", "revision": deleted_item["revision"]},
    )
    assert restored.status == 200
    assert (await restored.json())["memory"]["deletedAt"] == 0

    missing = await client.delete(
        "/api/conversations/conv-1/task-memories/mem-missing",
        json={"scopeType": "conversation", "revision": 1},
    )
    assert missing.status == 404

    audit_json = json.dumps(harness.audit_rows, ensure_ascii=False)
    for forbidden in (
        "sensitive-body-not-for-list-or-audit", "No restart", approved_sensitive_value,
        updated_body, "updated-description-not-in-audit", "stale-value-not-in-audit",
        "web-create-1",
    ):
        assert forbidden not in audit_json
    task_audits = [row for row in harness.audit_rows if row["kind"].startswith("web.task_memory.")]
    details = [row["detail"] for row in task_audits]
    assert {detail["action"] for detail in details} == {"create", "update", "delete", "restore"}
    assert {detail["result"] for detail in details} >= {"success", "conflict", "not_found"}
    assert all(detail["actor"] == "web" and detail["conversationUuid"] == "conv-1" for detail in details)
    create_audit = next(
        detail for detail in details
        if detail["action"] == "create" and detail["idempotencyStatus"] == "explicit"
    )
    assert create_audit["changedFields"] == [
        "name", "description", "body", "autoReinjectCatalog", "visibleToAgents",
    ]
    assert len(create_audit["idempotencyIdentifier"]) == 16
    update_success = next(
        detail for detail in details if detail["action"] == "update" and detail["result"] == "success"
    )
    assert update_success["changedFields"] == ["description", "body"]
    assert update_success["revision"] == 2


async def test_task_memory_web_api_agent_task_ownership_parent_session_and_404(task_memory_api):
    client, harness = task_memory_api
    valid = await harness.rath_dao.create_task(
        chat_id=4201,
        workflow_uuid="wf",
        title="Valid task",
        input_data={"agentSnapshot": {"name": "Reviewer"}},
        parent_session_uuid="conv-1",
        agent_session_uuid="shared-session",
        task_uuid="task-valid",
    )
    await harness.rath_dao.create_task(
        chat_id=4201,
        workflow_uuid="wf",
        title="Wrong parent",
        parent_session_uuid="conv-other",
        agent_session_uuid="shared-session",
        task_uuid="task-wrong-parent",
    )
    await harness.rath_dao.create_task(
        chat_id=9999,
        workflow_uuid="wf",
        title="Wrong user",
        parent_session_uuid="conv-1",
        agent_session_uuid="shared-session",
        task_uuid="task-wrong-user",
    )

    created = await client.post("/api/conversations/conv-1/task-memories", json={
        "scopeType": "agent_task", "taskUuid": valid,
        "name": "Task fact", "body": "task-body",
    })
    assert created.status == 201
    item = (await created.json())["memory"]

    for inaccessible_task in ("task-wrong-parent", "task-wrong-user", "missing-task"):
        response = await client.get(
            "/api/conversations/conv-1/task-memories",
            params={"scopeType": "agent_task", "taskUuid": inaccessible_task},
        )
        assert response.status == 404

    cross_task = await client.get(
        f"/api/conversations/conv-1/task-memories/{item['memoryUuid']}",
        params={"scopeType": "agent_task", "taskUuid": "task-wrong-parent"},
    )
    assert cross_task.status == 404
    cross_conversation = await client.get(
        "/api/conversations/conv-other/task-memories",
        params={"scopeType": "conversation"},
    )
    assert cross_conversation.status == 404

    tasks = await client.get("/api/conversations/conv-1/task-memories/tasks")
    task_payload = await tasks.json()
    assert [(row["taskUuid"], row["name"]) for row in task_payload["tasks"]] == [("task-valid", "Reviewer")]


async def test_task_memory_preview_uses_runtime_formatter_scope_acl_budget_and_no_body(task_memory_api):
    client, harness = task_memory_api
    task_uuid = await harness.rath_dao.create_task(
        chat_id=4201,
        workflow_uuid="wf-preview",
        title="Preview task",
        input_data={"agentSnapshot": {"name": "Previewer"}},
        parent_session_uuid="conv-1",
        agent_session_uuid="preview-session",
        task_uuid="task-preview",
    )
    visible = await client.post("/api/conversations/conv-1/task-memories", json={
        "scopeType": "conversation",
        "name": "Visible shared",
        "description": "shown to Agent",
        "body": "VISIBLE-BODY-MUST-NOT-LEAK",
        "visibleToAgents": True,
    })
    hidden = await client.post("/api/conversations/conv-1/task-memories", json={
        "scopeType": "conversation",
        "name": "Hidden shared",
        "description": "main only",
        "body": "HIDDEN-BODY-MUST-NOT-LEAK",
        "visibleToAgents": False,
    })
    own = await client.post("/api/conversations/conv-1/task-memories", json={
        "scopeType": "agent_task",
        "taskUuid": task_uuid,
        "name": "Own task fact",
        "description": "own scope",
        "body": "OWN-BODY-MUST-NOT-LEAK",
    })
    assert visible.status == hidden.status == own.status == 201

    conversation_preview = await client.get(
        "/api/conversations/conv-1/task-memories/preview",
        params={"scopeType": "conversation", "query": "does-not-affect-preview"},
    )
    conversation_payload = await conversation_preview.json()
    conversation_preview_without_search = await client.get(
        "/api/conversations/conv-1/task-memories/preview",
        params={"scopeType": "conversation"},
    )
    conversation_payload_without_search = await conversation_preview_without_search.json()
    assert conversation_preview.status == conversation_preview_without_search.status == 200
    assert conversation_payload["catalogXml"] == conversation_payload_without_search["catalogXml"]
    assert conversation_payload["estimatedRuntimeTokens"] == conversation_payload_without_search["estimatedRuntimeTokens"]
    assert conversation_payload["scopeType"] == "conversation"
    assert conversation_payload["taskUuid"] == ""
    assert "<conversation-memory revision=" in conversation_payload["catalogXml"]
    assert "Visible shared" in conversation_payload["catalogXml"]
    assert "Hidden shared" in conversation_payload["catalogXml"]
    assert "agent-task-memory" not in conversation_payload["catalogXml"]

    agent_preview = await client.get(
        "/api/conversations/conv-1/task-memories/preview",
        params={"scopeType": "agent_task", "taskUuid": task_uuid},
    )
    agent_payload = await agent_preview.json()
    assert agent_preview.status == 200
    assert agent_payload["scopeType"] == "agent_task"
    assert agent_payload["taskUuid"] == task_uuid
    assert "Visible shared" in agent_payload["catalogXml"]
    assert "Hidden shared" not in agent_payload["catalogXml"]
    assert "Own task fact" in agent_payload["catalogXml"]
    assert "<conversation-memory revision=" in agent_payload["catalogXml"]
    assert "<agent-task-memory revision=" in agent_payload["catalogXml"]
    assert 0 < agent_payload["estimatedRuntimeTokens"] <= agent_payload["maxRuntimeTokens"] == 1500

    serialized = json.dumps({"conversation": conversation_payload, "agent": agent_payload})
    assert "VISIBLE-BODY-MUST-NOT-LEAK" not in serialized
    assert "HIDDEN-BODY-MUST-NOT-LEAK" not in serialized
    assert "OWN-BODY-MUST-NOT-LEAK" not in serialized

    inaccessible = await client.get(
        "/api/conversations/conv-1/task-memories/preview",
        params={"scopeType": "agent_task", "taskUuid": "missing-task"},
    )
    assert inaccessible.status == 404
