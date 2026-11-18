"""HTTP/SQLite acceptance for shared conversation strategy, defaults and manual summary."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.context.configuration import conversation_strategy
from app.context.runtime import ContextManager
from app.context.store import ContextOwner, WindowStore
from app.context.window import WindowPolicy
from app.db.dao import MessageDAO, SummaryDAO
from app.llm.events import Usage
from app.tools.base import ToolRegistry
from tests.test_context_strategies import SUMMARY, Backend, history
from tests.test_folder_run_defaults import folder, save
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as shared_web_env

web_env = shared_web_env


async def test_default_inheritance_switch_and_duplicate_are_conversation_scoped(web_env):
    env = web_env
    await _login_cookie(env)
    old = await env.server._create_web_conversation(123)
    assert old["context_strategy"] == "sliding_window"
    env.server.config.context_management.default_strategy = "model_summary"
    fallback = await (await env.client.get("/api/conversations/defaults")).json()
    assert fallback["defaults"]["contextStrategy"] == "model_summary"
    parent = await folder(env, "Summary project", defaults={"contextStrategy": "model_summary"})
    child = await folder(env, "Inherited", parent)
    created = await (await env.client.post("/api/conversations", json={"folderId": child})).json()
    uuid = created["conversation"]["conversationUuid"]
    assert created["conversation"]["contextStrategy"] == "model_summary"
    await save(env, parent, {"contextStrategy": "sliding_window"})
    assert await conversation_strategy(env.db, uuid) == "model_summary"
    response = await env.client.post(f"/api/conversations/{uuid}/context-strategy", json={"strategy": "sliding_window"})
    assert response.status == 200, await response.text()
    result = await response.json()
    assert result["runConfig"]["contextStrategy"] == "sliding_window"
    properties = await (await env.client.get(f"/api/conversation-folders/{parent}/properties")).json()
    assert properties["runDefaults"]["local"] == {"contextStrategy": "sliding_window"}
    assert (await env.server._conversation_row(123, old["conversation_uuid"]))["context_strategy"] == "sliding_window"
    duplicate = await env.server._duplicate_web_conversation_data(await env.server._conversation_row(123, uuid))
    assert duplicate["context_strategy"] == "sliding_window"
    state = await (await env.client.get(f"/api/conversations/{uuid}/state")).json()
    assert state["contextStrategy"] == "sliding_window"
    assert state["manualCompactMinPercent"] == env.server.config.agent.manual_compact_min_percent


@pytest.mark.parametrize("value", ["", "unknown", None, {}, ["model_summary"]])
async def test_invalid_strategy_does_not_mutate_conversation(web_env, value):
    await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123)
    response = await web_env.client.post(f"/api/conversations/{row['conversation_uuid']}/context-strategy", json={"strategy": value})
    assert response.status == 400
    assert await conversation_strategy(web_env.db, row["conversation_uuid"]) == "sliding_window"


async def setup_manual(env):
    await _login_cookie(env)
    cfg = env.server.config
    cfg.agent.keep_recent_messages = 2
    cfg.agent.manual_compact_min_percent = 50
    cfg.agent.compact_max_retries = 0
    cfg.models.compression_models = ["openai/cheap"]
    cfg.models.providers["openai"].models[0].rollover_trigger_tokens = 8000
    backend = Backend()
    env.server.llm_factory = SimpleNamespace(backend_for=lambda label: (backend, label.split("/")[1], 8192), context_window=lambda _: 128000)
    env.server.tools = ToolRegistry()
    row = await env.server._create_web_conversation(123, run_config={"context_strategy": "model_summary"})
    chat, uuid = row["internal_chat_id"], row["conversation_uuid"]
    dao = MessageDAO(env.db)
    await dao.get_or_set_system_snapshot(chat, "Original system")
    sid = await dao.get_or_create_session_uuid(chat)
    store = WindowStore(env.db, ContextOwner.controller(chat_id=chat, session_uuid=sid, conversation_uuid=uuid))
    manager = ContextManager(store, WindowPolicy(128000, trigger_tokens=8000),
                             backend=backend, model="gpt", model_label="openai/gpt")
    await manager.checkpoint(history())
    ticket = await store.begin_request(route="")
    await store.observe_usage(ticket, tokens=6000)
    return row, backend, store


async def test_manual_summary_uses_same_manager_persists_event_and_ledger_without_tool_call(web_env):
    row, backend, store = await setup_manual(web_env)
    uuid, chat = row["conversation_uuid"], row["internal_chat_id"]
    response = await web_env.client.post(f"/api/conversations/{uuid}/compact")
    assert response.status == 200, await response.text()
    result = await response.json()
    assert len(backend.calls) == 1
    assert result["outcome"]["strategy"] == "model_summary"
    assert (await SummaryDAO(web_env.db).latest(chat))["summary"] == SUMMARY
    assert (await store.load())["usage_known"] == 0
    calls = await MessageDAO(web_env.db).recent_model_calls(chat)
    assert len(calls) == 1 and calls[0].call_kind == "context_compaction"
    assert calls[0].model == "openai/cheap"
    cur = await web_env.db.conn.execute("SELECT COUNT(*) n FROM tool_calls WHERE chat_id=?", (chat,))
    assert (await cur.fetchone())["n"] == 0
    events = await web_env.server._web_operations(uuid)
    compactions = [op for op in events if op["opType"] == "context_compaction"]
    assert len(compactions) == 1
    assert compactions[0]["payload"]["strategy"] == "model_summary"
    summary_id = result["outcome"]["summaryId"]
    detail = await (await web_env.client.get(f"/api/conversations/{uuid}/compactions/{summary_id}")).json()
    assert detail["compactedOutput"] == SUMMARY
    # A later click cannot treat summary-model usage as execution-model input.
    assert (await web_env.client.post(f"/api/conversations/{uuid}/compact")).status == 409


async def test_manual_summary_failure_keeps_current_window_and_visible_error(web_env):
    row, backend, store = await setup_manual(web_env)
    before = await store.restore_messages()
    backend.output = RuntimeError("failed upstream")
    uuid = row["conversation_uuid"]
    response = await web_env.client.post(f"/api/conversations/{uuid}/compact")
    assert response.status == 409
    assert await store.restore_messages() == before
    assert await SummaryDAO(web_env.db).latest(row["internal_chat_id"]) is None
    events = await web_env.server._web_operations(uuid)
    failed = [op for op in events if op["opType"] == "context_compaction"]
    assert len(failed) == 1 and failed[0]["status"] == "failed"


async def test_manual_is_summary_only_and_rejects_busy_without_calling_model(web_env):
    row, backend, _ = await setup_manual(web_env)
    uuid = row["conversation_uuid"]
    async with web_env.server.operation_locks.chat(row["internal_chat_id"], "test_running"):
        response = await web_env.client.post(f"/api/conversations/{uuid}/compact")
        assert response.status == 409
    await web_env.client.post(f"/api/conversations/{uuid}/context-strategy", json={"strategy": "sliding_window"})
    response = await web_env.client.post(f"/api/conversations/{uuid}/compact")
    assert response.status == 409
    assert (await response.json())["error"] == "manual_compaction_requires_summary_strategy"
    assert not backend.calls


async def test_conversation_state_reports_completed_compaction_count_across_pagination_and_failures(web_env):
    row, _, _ = await setup_manual(web_env)
    uuid = row["conversation_uuid"]

    before = await (await web_env.client.get(f"/api/conversations/{uuid}/state?timelineLimit=1")).json()
    assert before["contextUsage"]["compactionCount"] == 0

    response = await web_env.client.post(f"/api/conversations/{uuid}/compact")
    assert response.status == 200, await response.text()
    after_one = await (await web_env.client.get(f"/api/conversations/{uuid}/state?timelineLimit=1")).json()
    assert after_one["contextUsage"]["compactionCount"] == 1

    # Persisted sliding_window compaction in an older off-page turn is counted, while failed/running rows are excluded.
    live = web_env.server._live_for(row)
    await live.publish({
        "type": "context_compaction_state",
        "_webOperationSpecs": [
            {
                "op_id": "context-compaction:sw-old",
                "op_type": "context_compaction",
                "action": "end",
                "status": "completed",
                "source": "context_compaction",
                "turn_uuid": "turn-old",
                "payload": {"name": "ContextCompaction", "compactionId": "context-compaction:sw-old", "strategy": "sliding_window", "status": "completed"},
            },
            {
                "op_id": "context-compaction:sw-failed",
                "op_type": "context_compaction",
                "action": "end",
                "status": "failed",
                "source": "context_compaction",
                "turn_uuid": "turn-new",
                "payload": {"name": "ContextCompaction", "compactionId": "context-compaction:sw-failed", "strategy": "sliding_window", "status": "failed"},
            },
            {
                "op_id": "msg:latest-turn",
                "op_type": "user_message",
                "action": "end",
                "status": "completed",
                "turn_uuid": "turn-latest",
                "payload": {"content": "latest question"},
            },
        ],
    })
    paged = await (await web_env.client.get(f"/api/conversations/{uuid}/state?timelineLimit=1")).json()
    assert paged["contextUsage"]["compactionCount"] == 2
    assert paged["timelineLimit"] == 1 and paged["hasMoreBefore"] is True
    assert all(op["opId"] != "context-compaction:sw-old" for op in paged["operations"])


async def test_compaction_count_and_operation_cursor_share_the_lock(web_env, monkeypatch):
    import asyncio

    env = web_env
    await _login_cookie(env)
    row = await env.server._create_web_conversation(123)
    uuid = row['conversation_uuid']
    live = env.server._live_for(row)
    op_id = 'context-compaction:race'
    async def publish(status):
        await live.publish({'type': 'context_compaction_state', '_webOperationSpecs': [{
            'op_id': op_id, 'op_type': 'context_compaction', 'action': 'start' if status == 'running' else 'end',
            'status': status, 'lifecycle': 'active' if status == 'running' else 'terminal',
            'payload': {'name': 'ContextCompaction', 'status': status, 'windowVersion': 2},
        }]})
    await publish('running')
    async def no_reconcile(*args, **kwargs):
        pass
    monkeypatch.setattr(env.server, '_reconcile_inactive_web_conversation_operations', no_reconcile)
    reading, release, publishing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = env.server._count_conversation_compactions
    async def gated_count(*args):
        assert env.server._web_operation_lock(uuid).locked()
        reading.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(env.server, '_count_conversation_compactions', gated_count)
    state_request = asyncio.ensure_future(env.client.get(f'/api/conversations/{uuid}/state?timelineLimit=1'))
    finishing = None
    try:
        await asyncio.wait_for(reading.wait(), 5)
        async def finish():
            publishing.set()
            await publish('completed')
        finishing = asyncio.create_task(finish())
        await publishing.wait()
        await asyncio.sleep(0)
        assert not finishing.done(), 'terminal frame cannot cross the snapshot/count boundary'
    finally:
        release.set()
    before = await (await state_request).json()
    await finishing
    after = await (await env.client.get(f'/api/conversations/{uuid}/state?timelineLimit=1')).json()
    assert before['timelineLimit'] == after['timelineLimit'] == 1
    assert before['contextUsage']['compactionCount'] == 0
    assert next(op for op in before['operations'] if op['opId'] == op_id)['status'] == 'running'
    assert after['contextUsage']['compactionCount'] == 1
    assert after['frameSeq'] > before['frameSeq']
    assert next(op for op in after['operations'] if op['opId'] == op_id)['status'] == 'completed'


async def test_window_summary_waits_for_terminal_frame_instead_of_legacy_projection(web_env):
    import json

    env = web_env
    await _login_cookie(env)
    row = await env.server._create_web_conversation(123)
    chat, uuid = row['internal_chat_id'], row['conversation_uuid']
    dao = MessageDAO(env.db)
    anchor = await dao.add(chat, 'user', 'anchor', conversation_uuid=uuid, turn_uuid='turn', run_root_turn_uuid='turn')
    summary = await SummaryDAO(env.db).add(chat, 'summary', anchor, 10)
    sid = await dao.get_or_create_session_uuid(chat)
    await env.db.conn.execute(
        'INSERT INTO context_window_rotations(rotation_id,owner_key,window_version,reason,detail_json,created_at) VALUES(?,?,?,?,?,?)',
        ('race-rotation', f'controller:{chat}:{sid}', 1, 'pre_model_request', json.dumps({'summaryId': summary}), 1))
    await env.db.conn.commit()
    # The window commit precedes the completed operation. It must not be counted
    # as a legacy summary now and then counted a second time on frame replay.
    assert await env.server._count_conversation_compactions(chat, uuid) == 0
    assert await env.server._project_context_compaction_operations(chat, uuid, []) == []
    await env.server._live_for(row).publish({'type': 'context_compaction_state', '_webOperationSpecs': [{
        'op_id': 'context-compaction:summary', 'op_type': 'context_compaction', 'action': 'end',
        'status': 'completed', 'payload': {'summaryId': summary, 'status': 'completed', 'windowVersion': 1},
    }]})
    assert await env.server._count_conversation_compactions(chat, uuid) == 1


async def test_manual_compaction_uses_default_for_missing_persona_without_editing_snapshot(web_env):
    from tests.test_prompt_template import template

    row, _backend, store = await setup_manual(web_env)
    await template(web_env, 'default', 'DEFAULT-AFTER-DELETION', is_active=1)
    persona = await template(web_env, 'retired', 'PERSONA', is_persona=1)
    await web_env.db.conn.execute('UPDATE web_conversations SET prompt_template=? WHERE conversation_uuid=?', (persona, row['conversation_uuid']))
    await web_env.db.conn.commit()
    assert (await web_env.client.delete(f'/api/memory/templates/{persona}')).status == 200
    response = await web_env.client.post(f"/api/conversations/{row['conversation_uuid']}/compact")
    assert response.status == 200, await response.text()
    saved = await store.load(fresh=True)
    assert saved['state']['systemReplacement']['system'] == 'DEFAULT-AFTER-DELETION'
    assert await MessageDAO(web_env.db).get_system_snapshot(row['internal_chat_id']) == 'Original system'
    assert (await response.json())['state']['promptTemplateMissing'] is True
