from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import Config
from app.db.engine import DB
from app.llm.base import AgentResult
from app.llm.events import Usage
from app.rath.builtin_workflows import ensure_builtin_workflows
from app.web_admin import WebAdminServer, _sha256


def _cfg() -> Config:
    return Config.model_validate({
        "telegram": {"botToken": "t", "whitelistIds": [123]},
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://x",
                    "apiKey": "k",
                    "protocol": "chat",
                    "models": [{"id": "gpt"}],
                }
            },
            "primary": "openai/gpt",
        },
        "memory": {"baseUrl": "http://m", "identity": "openbear", "accessKey": "ak"},
        "web": {"enabled": True, "host": "127.0.0.1", "port": 18961},
    })


class FakeBackend:
    protocol = "chat"

    async def complete(self, messages, *, model, system="", tools=None, max_tokens=8192, **opts):
        return AgentResult(text="试运行完成", usage=Usage(input_tokens=11, output_tokens=7))


class FakeFactory:
    def backend_for(self, model_name: str):
        return FakeBackend(), "gpt", 1024


class FakeBot:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, reply_markup=reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


@pytest.fixture
async def web_env(tmp_path):
    db = DB(str(tmp_path / "web-rath.db"))
    await db.connect()
    bot = FakeBot()
    server = WebAdminServer(_cfg(), db, bot)  # type: ignore[arg-type]
    await server.ensure_secret_key()
    await ensure_builtin_workflows(server.rath_dao)
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    try:
        key = await server.get_secret_key()
        start = await client.post("/api/auth/login/start", json={"secret": key})
        req_uuid = (await start.json())["requestUuid"]
        await server.decide_login_request(req_uuid, approved=True, decided_by=123)
        approved = await client.post(f"/api/auth/login/consume/{req_uuid}")
        cookie = approved.cookies["openbear_web_session"].value
        yield SimpleNamespace(db=db, server=server, client=client, cookie={"openbear_web_session": cookie})
    finally:
        await client.close()
        await db.close()


async def test_rath_web_options(web_env):
    resp = await web_env.client.get("/api/rath/options", cookies=web_env.cookie)
    assert resp.status == 200
    data = await resp.json()
    assert data["models"][0]["key"] == "openai/gpt"
    assert data["primaryModel"] == "openai/gpt"
    assert "off" in data["thinkLevels"]


async def test_rath_web_agent_registry_crud(web_env):
    options = await web_env.client.get("/api/rath/options", cookies=web_env.cookie)
    assert options.status == 200
    assert "baseTemplates" not in await options.json()

    created = await web_env.client.post(
        "/api/rath/agents",
        json={
            "agentKey": "code-reader",
            "name": "代码阅读员",
            "description": "读取项目结构并解释代码",
            "systemPrompt": "你是代码阅读员",
            "model": "openai/gpt",
            "thinkLevel": "high",
            "toolAllowlist": ["Read", "Grep"],
            "enabled": True,
        },
        cookies=web_env.cookie,
    )
    assert created.status == 200
    agent = (await created.json())["item"]
    assert agent["name"] == "代码阅读员"
    assert agent["agent_key"] == "code-reader"
    assert agent["tool_allowlist"] == ["Read"]

    listed = await web_env.client.get("/api/rath/agents?disabled=1", cookies=web_env.cookie)
    assert listed.status == 200
    assert any(a["id"] == agent["id"] for a in (await listed.json())["items"])
    templates = await web_env.client.get("/api/memory/templates", cookies=web_env.cookie)
    prompt_params = (await templates.json())["promptParams"]
    assert any(
        a["id"] == agent["id"]
        and a["scenario"] == "读取项目结构并解释代码"
        and a["allowedTools"] == ["Read"]
        and a["allowedToolsText"] == "Read"
        for a in prompt_params["availableAgents"]
    )

    updated = await web_env.client.put(
        f"/api/rath/agents/{agent['id']}",
        json={
            "enabled": False,
            "model": "openai/other",
            "toolAllowlist": "WebSearch, WebExtract",
        },
        cookies=web_env.cookie,
    )
    assert updated.status == 200
    updated_item = (await updated.json())["item"]
    assert updated_item["enabled"] is False
    assert updated_item["model"] == "openai/other"
    assert updated_item["tool_allowlist"] == ["WebSearch", "WebExtract"]

    deleted = await web_env.client.delete(f"/api/rath/agents/{agent['id']}", cookies=web_env.cookie)
    assert deleted.status == 200
    listed_after_delete = await web_env.client.get("/api/rath/agents?disabled=1", cookies=web_env.cookie)
    assert listed_after_delete.status == 200
    assert not any(a["id"] == agent["id"] for a in (await listed_after_delete.json())["items"])


async def test_rath_web_agent_trial_requires_controller_or_explicit_legacy_mode(web_env):
    web_env.server.llm_factory = FakeFactory()
    web_env.server.model_selection = SimpleNamespace(current="openai/gpt")
    wf = await web_env.server.rath_dao.workflow_by_slug("single-agent")
    assert wf is not None
    agent_id = await web_env.server.rath_dao.create_agent(
        agent_key="trial-agent",
        name="试运行员",
        description="用于试运行",
        system_prompt="你是试运行员",
        model="openai/gpt",
        think_level="off",
        tool_allowlist=[],
        enabled=True,
    )
    resp = await web_env.client.post(
        f"/api/rath/agents/{agent_id}/trial",
        json={"instruction": "介绍一下自己"},
        cookies=web_env.cookie,
    )
    assert resp.status == 409
    assert (await resp.json())["error"] == "controller_runtime_required"

    web_env.server.config.rath.agent_plan_enabled = False
    resp = await web_env.client.post(
        f"/api/rath/agents/{agent_id}/trial",
        json={"instruction": "介绍一下自己"},
        cookies=web_env.cookie,
    )
    assert resp.status == 200
    task_uuid = (await resp.json())["taskUuid"]
    for _ in range(50):
        task = await web_env.server.rath_dao.get_task(task_uuid)
        if task and task.status == "completed":
            break
        await asyncio.sleep(0.02)
    task = await web_env.server.rath_dao.get_task(task_uuid)
    assert task is not None
    assert task.status == "completed"
    assert task.model_call_count == 1

async def test_rath_task_events_support_gap_safe_incremental_and_backward_pagination(web_env):
    workflow = await web_env.server.rath_dao.workflow_by_slug("single-agent")
    assert workflow is not None
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=123,
        workflow_uuid=workflow.workflow_uuid,
        title="event pagination",
        parent_session_uuid="conversation-events",
        status="running",
    )
    event_seqs = []
    for index in range(1, 6):
        event_seqs.append(await web_env.server.rath_dao.append_event(
            task_uuid,
            "tool_result" if index % 2 else "control_requested",
            summary=f"event-{index}",
        ))
    first_seq, _gap_one, _gap_two, fourth_seq, fifth_seq = event_seqs
    await web_env.db.conn.execute(
        "DELETE FROM rath_task_events WHERE task_uuid=? AND seq IN (?, ?)",
        (task_uuid, event_seqs[1], event_seqs[2]),
    )
    await web_env.db.conn.commit()

    first = await web_env.client.get(
        f"/api/conversations/conversation-events/agents/{task_uuid}/events?afterSeq={first_seq}&limit=1",
        cookies=web_env.cookie,
    )
    assert first.status == 200
    first_data = await first.json()
    assert [item["seq"] for item in first_data["events"]] == [fourth_seq]
    assert first_data["total"] == 4
    assert first_data["monitorTotal"] == 2
    assert first_data["hasMore"] is True
    assert first_data["nextAfterSeq"] == fourth_seq

    second = await web_env.client.get(
        f"/api/conversations/conversation-events/agents/{task_uuid}/events?afterSeq={fourth_seq}&limit=10",
        cookies=web_env.cookie,
    )
    second_data = await second.json()
    assert [item["seq"] for item in second_data["events"]] == [fifth_seq]
    assert second_data["total"] == 4
    assert second_data["monitorTotal"] == 2
    assert second_data["hasMore"] is False

    newest = await web_env.client.get(
        f"/api/conversations/conversation-events/agents/{task_uuid}/events?limit=2",
        cookies=web_env.cookie,
    )
    newest_data = await newest.json()
    assert [item["seq"] for item in newest_data["events"]] == [fourth_seq, fifth_seq]
    assert newest_data["total"] == 4
    assert newest_data["monitorTotal"] == 2
    assert newest_data["hasMore"] is True
    assert newest_data["nextBeforeSeq"] == fourth_seq


async def test_rath_task_event_totals_classify_monitor_events_consistently(web_env):
    workflow = await web_env.server.rath_dao.workflow_by_slug("single-agent")
    assert workflow is not None
    task_uuid = await web_env.server.rath_dao.create_task(
        chat_id=123,
        workflow_uuid=workflow.workflow_uuid,
        title="event totals",
        parent_session_uuid="conversation-event-totals",
        status="running",
    )
    for kind in ("plan_progress_started", "plan_approve", "model_context_compaction_finished", "tool_result"):
        await web_env.server.rath_dao.append_event(task_uuid, kind, summary=kind)

    response = await web_env.client.get(
        f"/api/conversations/conversation-event-totals/agents/{task_uuid}/events?limit=1",
        cookies=web_env.cookie,
    )
    assert response.status == 200
    data = await response.json()
    assert data["total"] == 5
    assert data["monitorTotal"] == 4


async def test_web_login_consume_requires_browser_nonce(web_env):
    req_uuid = await web_env.server.create_login_request(
        ip="127.0.0.1",
        user_agent="pytest",
        nonce_hash=_sha256("nonce-value"),
    )
    await web_env.server.decide_login_request(req_uuid, approved=True, decided_by=123)

    missing = await web_env.client.post(f"/api/auth/login/consume/{req_uuid}")
    assert missing.status == 403

    good = await web_env.client.post(
        f"/api/auth/login/consume/{req_uuid}",
        cookies={"openbear_web_login_nonce": "nonce-value"},
    )
    assert good.status == 200


