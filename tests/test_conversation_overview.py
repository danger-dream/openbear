# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json

from app.web_console.conversation_overview import read_conversation_overview
from tests.test_builtin_template_import import _login_cookie, web_env  # noqa: F401
from tests.test_reference_catalog import receive_type


async def seed(e, uuid="overview-a", chat=-901, owner=123, archived=0):
    await e.db.conn.execute(
        """INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title,status,current_status,archived_at)
        VALUES(?,?,?,'概览测试','idle','就绪',?)""",
        (uuid, chat, owner, archived),
    )
    await e.db.conn.execute(
        """INSERT INTO sessions(chat_id,session_uuid,usage_input_tokens,usage_output_tokens,usage_cache_read_tokens,usage_cache_write_tokens,usage_cost_usd,stat_total_time_ms_sum)
        VALUES(?,?,100,20,300,40,1.25,99999)""",
        (chat, uuid + "-session"),
    )
    await e.db.conn.commit()
    return uuid, chat, uuid + "-session"


async def test_configuration_shows_current_model_thinking_and_fast_without_private_config(web_env):
    e = web_env
    uuid, chat, _ = await seed(e)
    model = e.server.config.models.providers["openai"].models[0]
    model.name = "GPT 配置测试"
    model.thinking_levels = ["off", "medium", "xhigh"]
    model.default_thinking_level = "medium"
    model.supports_fast = True
    await e.db.conn.execute(
        "UPDATE sessions SET thinking_level=?,fast_mode=1 WHERE chat_id=?", ("xhigh", chat)
    )
    await e.db.conn.commit()
    config = (await e.server._conversation_overview(123, uuid))["configuration"]
    assert config == {
        "model": "openai/gpt",
        "modelName": "GPT 配置测试",
        "thinkingLevel": "xhigh",
        "supportsThinking": True,
        "fastMode": True,
        "supportsFast": True,
    }
    assert "apiKey" not in json.dumps(config) and "baseUrl" not in json.dumps(config)
    await e.db.conn.execute(
        "UPDATE sessions SET thinking_level=?,fast_mode=0 WHERE chat_id=?",
        ("unsupported-level", chat),
    )
    await e.db.conn.commit()
    config = (await e.server._conversation_overview(123, uuid))["configuration"]
    assert config["thinkingLevel"] == "medium" and config["fastMode"] is False


async def test_configuration_distinguishes_disabled_unsupported_and_unknown(web_env):
    e = web_env
    uuid, chat, _ = await seed(e)
    await e.db.conn.execute("UPDATE sessions SET fast_mode=1 WHERE chat_id=?", (chat,))
    await e.db.conn.commit()
    config = (await e.server._conversation_overview(123, uuid))["configuration"]
    assert config["supportsFast"] is False and config["fastMode"] is False
    assert config["supportsThinking"] is False
    await e.db.conn.execute(
        "UPDATE web_conversations SET model=? WHERE conversation_uuid=?", ("removed/model", uuid)
    )
    await e.db.conn.commit()
    config = (await e.server._conversation_overview(123, uuid))["configuration"]
    assert config["model"] == "removed/model"
    assert config["thinkingLevel"] is None and config["fastMode"] is None
    assert config["supportsFast"] is None


async def test_configuration_is_not_overwritten_by_previous_live_model_stats(web_env):
    e = web_env
    uuid, chat, _ = await seed(e)
    model = e.server.config.models.providers["openai"].models[0]
    model.thinking_levels = ["off", "medium", "xhigh"]
    model.default_thinking_level = "medium"
    live = e.server._live_for(
        {"conversation_uuid": uuid, "internal_chat_id": chat, "status": "idle"}
    )
    live.status = "running"
    live.last_stats = {"model": "old/model", "thinkLevel": "xhigh"}
    assert (await e.server._conversation_overview(123, uuid))["configuration"][
        "thinkingLevel"
    ] == "medium"


async def test_open_overview_receives_thinking_and_fast_changes_without_catalog_rename(web_env):
    e = web_env
    uuid, chat, _ = await seed(e)
    model = e.server.config.models.providers["openai"].models[0]
    model.thinking_levels = ["off", "medium", "xhigh"]
    model.default_thinking_level = "medium"
    model.supports_fast = True
    ws = await socket(e)
    try:
        await ws.send_json(
            {
                "type": "conversation-overview",
                "conversationUuid": uuid,
                "subscriptionId": "settings",
            }
        )
        initial = await receive_type(ws, "conversation-overview")
        assert initial["overview"]["configuration"]["fastMode"] is False
        await e.db.conn.execute(
            "UPDATE sessions SET thinking_level=?,fast_mode=1 WHERE chat_id=?", ("xhigh", chat)
        )
        await e.db.conn.commit()
        next(iter(e.server.global_realtime.clients.values()))["overview"]["checkedAt"] = 0
        await e.server.global_realtime.refresh()
        changed = await receive_type(ws, "conversation-overview")
        assert changed["overview"]["configuration"]["fastMode"] is True
        assert changed["overview"]["configuration"]["thinkingLevel"] == "xhigh"
    finally:
        await ws.close()


async def operation(e, uuid, op_id, kind="stats", payload=None, lifecycle="terminal", created=1000):
    await e.db.conn.execute(
        """INSERT INTO web_operations(conversation_uuid,op_id,op_type,display_seq,revision,payload_json,lifecycle,created_at_ms,updated_at_ms)
        VALUES(?,?,?,1,1,?,?,?,?)""",
        (uuid, op_id, kind, json.dumps(payload or {}), lifecycle, created, created),
    )
    await e.db.conn.commit()


async def socket(e):
    cookies = await _login_cookie(e)
    ws = await e.client.ws_connect(
        "/api/events/ws", headers={"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
    )
    await receive_type(ws, "snapshot")
    return ws


async def test_overview_uses_ledger_counts_without_loading_private_content_or_writing(web_env):
    e = web_env
    uuid, chat, session = await seed(e)
    await e.db.conn.executemany(
        "INSERT INTO model_calls(chat_id,session_uuid,model_call_count,model_retry_count,total_time_ms,expert_tool_calls) VALUES(?,?,?,1,20,?)",
        [(chat, session, 3, 12), (chat, session, 1, 0)],
    )
    await e.db.conn.executemany(
        "INSERT INTO tool_calls(chat_id,session_uuid,tool_name,error_type) VALUES(?,?,?,?)",
        [(chat, session, "private tool", "PRIVATE-CONTENT")] * 5,
    )
    await e.db.conn.execute(
        "INSERT INTO rath_tasks(task_uuid,chat_id,parent_session_uuid,status,model_call_count,tool_call_count,cost_usd,input_json) VALUES(?,?,?,'completed',50,12,900,?)",
        ("overview-agent", chat, session, '{"private":"PRIVATE-CONTENT"}'),
    )
    await e.db.conn.executemany(
        "INSERT INTO messages(chat_id,role,content,compacted) VALUES(?,?,?,?)",
        [
            (chat, "user", "PRIVATE-CONTENT", 1),
            (chat, "assistant", "PRIVATE-CONTENT", 0),
            (chat, "tool", "PRIVATE-CONTENT", 0),
        ],
    )
    await e.db.conn.commit()
    await operation(e, uuid, "stat1", payload={"durationMs": 5000, "secret": "PRIVATE-CONTENT"})
    await operation(e, uuid, "stat2", payload={"durationMs": 7000})
    await operation(
        e,
        uuid,
        "not-a-duration",
        kind="assistant",
        payload={"durationMs": 999999, "text": "PRIVATE-CONTENT"},
    )
    wake = asyncio.Event()
    e.db.conn.commit_listeners.add(wake.set)
    result = await e.server._conversation_overview(123, uuid)
    assert result["usage"]["cost_usd"] == 1.25  # not +task/live copies of same billing
    assert result["usage"]["cache_read_tokens"] == 300
    assert result["messageCount"] == 2
    assert result["calls"] == {"model": 4, "tool": 17, "retry": 2, "failed": 0}
    assert result["duration"] == {"timelineTotalDurationMs": 12000, "modelCallsMs": 40, "liveMs": 0}
    assert result["running"] is False and result["startedAtMs"] is None
    assert not wake.is_set()
    assert "PRIVATE-CONTENT" not in json.dumps(result)
    assert (
        not {"operations", "messages", "lastError", "modelCalls", "toolCalls", "input_json"}
        & result.keys()
    )
    e.db.conn.commit_listeners.discard(wake.set)


async def test_overview_counts_full_history_not_detail_api_limits_and_scopes_session(web_env):
    e = web_env
    uuid, chat, session = await seed(e)
    await e.db.conn.executemany(
        "INSERT INTO model_calls(chat_id,session_uuid,model_call_count,total_time_ms) VALUES(?,?,1,2)",
        [(chat, session)] * 610 + [(chat, "old-session")] * 3,
    )
    await e.db.conn.executemany(
        "INSERT INTO tool_calls(chat_id,session_uuid) VALUES(?,?)",
        [(chat, session)] * 1105 + [(chat, "old-session")] * 2,
    )
    await e.db.conn.commit()
    result = await e.server._conversation_overview(123, uuid)
    assert result["calls"]["model"] == 610 and result["calls"]["tool"] == 1105
    # Same header fallback, not a different sum of overlapping time metrics.
    assert result["duration"]["modelCallsMs"] == 1000


async def test_overview_retains_legacy_expert_tool_fallback_without_task_rows(web_env):
    e = web_env
    uuid, chat, session = await seed(e)
    await e.db.conn.execute(
        "INSERT INTO model_calls(chat_id,session_uuid,model_call_count,expert_tool_calls) VALUES(?,?,7,11)",
        (chat, session),
    )
    await e.db.conn.commit()
    assert (await e.server._conversation_overview(123, uuid))["calls"]["tool"] == 11


async def test_duration_handles_malformed_and_missing_stats_without_throwing(web_env):
    e = web_env
    uuid, _, _ = await seed(e)
    for index, payload in enumerate(
        [{"durationMs": -1}, {"durationMs": "500"}, {}, {"durationMs": 123.5}]
    ):
        await operation(e, uuid, "s" + str(index), payload=payload)
    await e.db.conn.execute("UPDATE web_operations SET payload_json='broken' WHERE op_id='s0'")
    await e.db.conn.commit()
    result = await e.server._conversation_overview(123, uuid)
    assert result["duration"]["timelineTotalDurationMs"] == 123


async def test_read_snapshot_excludes_uncommitted_billing_and_checks_owner_even_for_archive(
    web_env,
):
    e = web_env
    uuid, chat, _ = await seed(e, archived=1)
    assert await e.server._conversation_overview(456, uuid) is None
    assert await e.server._conversation_overview(123, "missing") is None
    assert (await e.server._conversation_overview(123, uuid))["archived"] is True
    await e.db.conn.execute("UPDATE sessions SET usage_cost_usd=99 WHERE chat_id=?", (chat,))
    data = await asyncio.to_thread(read_conversation_overview, e.db.path, 123, uuid)
    assert data["usage"]["cost_usd"] == 1.25
    await e.db.conn.rollback()


async def test_running_uses_real_start_and_stops_timer_after_completion(web_env):
    e = web_env
    uuid, chat, _ = await seed(e)
    live = e.server._live_for(
        {
            "conversation_uuid": uuid,
            "internal_chat_id": chat,
            "status": "idle",
            "current_status": "就绪",
        }
    )
    live.status = "running"
    live.current_status = "等待用户回复"
    live.started_at_ms = 123456
    live.last_stats = {"durationMs": 4500, "private": "PRIVATE-CONTENT"}
    active = await e.server._conversation_overview(123, uuid)
    assert active["running"] and active["startedAtMs"] == 123456
    assert active["status"] == "等待用户回复" and active["duration"]["liveMs"] == 4500
    live.status = "idle"
    idle = await e.server._conversation_overview(123, uuid)
    assert not idle["running"] and idle["startedAtMs"] is None
    # Unknown start remains unknown; updated_at is never used as a stopwatch.
    live.status = "running"
    live.started_at_ms = 0
    assert (await e.server._conversation_overview(123, uuid))["startedAtMs"] is None


async def test_background_only_task_has_its_own_start_not_parent_last_update(web_env):
    e = web_env
    uuid, chat, session = await seed(e)
    await e.db.conn.execute(
        "INSERT INTO rath_tasks(task_uuid,chat_id,parent_session_uuid,status,started_at,updated_at) VALUES('bg',?,?,'running',100,999)",
        (chat, session),
    )
    await e.db.conn.commit()
    data = await e.server._conversation_overview(123, uuid)
    assert data["running"] and data["startedAtMs"] == 100000
    assert data["status"] == "Agent 后台执行中"


async def test_ws_only_reads_subscribed_conversation_and_unsubscribe_stops_work(
    web_env, monkeypatch
):
    e = web_env
    uuid, _, _ = await seed(e)
    calls = []
    original = e.server._conversation_overview

    async def observed(owner, identity):
        calls.append(identity)
        return await original(owner, identity)

    monkeypatch.setattr(e.server, "_conversation_overview", observed)
    ws = await socket(e)
    hub = e.server.global_realtime
    try:
        await hub.refresh()
        assert calls == []
        await ws.send_json(
            {"type": "conversation-overview", "conversationUuid": uuid, "subscriptionId": "a"}
        )
        data = await receive_type(ws, "conversation-overview")
        assert data["subscriptionId"] == "a" and data["overview"]["usage"]["cost_usd"] == 1.25
        assert calls == [uuid]
        state = next(iter(hub.clients.values()))
        # A committed billing change doesn't touch the resource-name catalog.
        await e.db.conn.execute("UPDATE sessions SET usage_cost_usd=2.5 WHERE chat_id=-901")
        await e.db.conn.commit()
        state["overview"]["checkedAt"] = 0
        await hub.refresh()
        patch = await receive_type(ws, "conversation-overview")
        assert patch["overview"]["usage"]["cost_usd"] == 2.5
        await ws.send_json(
            {"type": "conversation-overview", "conversationUuid": "", "subscriptionId": "a"}
        )
        await ws.send_json({"type": "ping"})
        await receive_type(ws, "pong")
        assert state["overview"] is None
        previous = len(calls)
        await hub.refresh()
        assert len(calls) == previous
    finally:
        await ws.close()


async def test_ws_rechecks_access_and_recovers_subscription_during_catalog_resync(web_env):
    e = web_env
    uuid, _, _ = await seed(e)
    await seed(e, uuid="other-owner", chat=-902, owner=456)
    ws = await socket(e)
    try:
        await ws.send_json(
            {
                "type": "conversation-overview",
                "conversationUuid": "other-owner",
                "subscriptionId": "denied",
            }
        )
        assert (await receive_type(ws, "conversation-overview"))["error"] == "not_found"
        await ws.send_json(
            {"type": "conversation-overview", "conversationUuid": uuid, "subscriptionId": "mine"}
        )
        assert "overview" in await receive_type(ws, "conversation-overview")
        await ws.send_json({"type": "resync"})
        await receive_type(ws, "snapshot")
        await e.server.global_realtime.refresh()
        assert (await receive_type(ws, "conversation-overview"))["subscriptionId"] == "mine"
        await e.db.conn.execute("DELETE FROM web_conversations WHERE conversation_uuid=?", (uuid,))
        await e.db.conn.commit()
        next(iter(e.server.global_realtime.clients.values()))["overview"]["checkedAt"] = 0
        await e.server.global_realtime.refresh()
        deleted = await receive_type(ws, "conversation-overview")
        assert deleted["error"] == "not_found" and "overview" not in deleted
    finally:
        await ws.close()
