from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.bot.admin import _running_operation_lines
from app.config import Config
from app.db.engine import DB, now_ts
from app.services import Services


def _cfg(tmp_path) -> Config:
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
        "memory": {"provider": "builtin", "identity": "openbear"},
        "storage": {"dbPath": str(tmp_path / "openbear.db")},
        "tools": {"skillsDir": str(tmp_path / "skills")},
        "web": {"enabled": False},
    })


@pytest.fixture
async def db(tmp_path):
    d = DB(str(tmp_path / "openbear.db"))
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


async def test_startup_marks_stale_running_operations_interrupted(tmp_path):
    svc = Services(_cfg(tmp_path), SimpleNamespace())  # type: ignore[arg-type]
    assert svc.web_admin.control_actions is svc.control_actions
    await svc.db.connect()
    try:
        await svc.db.conn.execute(
            "INSERT INTO operations (operation_uuid, chat_id, kind, status, detail_json, started_at) VALUES (?,?,?,?,?,?)",
            ("op-stale", 123, "restore", "running", "{}", now_ts() - 60),
        )
        await svc.db.conn.commit()

        changed = await svc._mark_interrupted_operations()

        assert changed == 1
        cur = await svc.db.conn.execute("SELECT status, finished_at, error FROM operations WHERE operation_uuid=?", ("op-stale",))
        row = await cur.fetchone()
        assert row["status"] == "interrupted"
        assert row["finished_at"] > 0
        assert "startup" in row["error"]
    finally:
        await svc.db.close()
        await svc.http.close()
        await svc.mem.close()


async def test_startup_reconciles_interrupted_rath_agent_web_operation(tmp_path):
    svc = Services(_cfg(tmp_path), SimpleNamespace())  # type: ignore[arg-type]
    await svc.db.connect()
    try:
        task_uuid = "task-zombie"
        payload = {
            "toolCallId": "call-agent",
            "name": "Agent",
            "status": "running",
            "task": {"taskUuid": task_uuid, "status": "running", "currentStatus": "模型调用中"},
            "resultText": json.dumps({"ok": True, "status": "running", "task": {"taskUuid": task_uuid, "status": "running"}}, ensure_ascii=False),
        }
        await svc.db.conn.execute(
            """
            INSERT INTO rath_tasks (
              task_uuid, chat_id, parent_session_uuid, title, status, current_status, error,
              model_call_count, tool_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd,
              started_at, updated_at, finished_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (task_uuid, -1, "conv-zombie", "Zombie Agent", "interrupted", "模型调用中", "interrupted by OpenBear startup", 10, 31, 78681, 2618, 378880, 0, 0.0914946, 100, 200, 200),
        )
        await svc.db.conn.execute(
            """
            INSERT INTO web_operations (
              conversation_uuid, internal_chat_id, op_id, op_type, turn_uuid, display_seq,
              status, lifecycle, revision, payload_json, created_at_ms, updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("conv-zombie", -1, "agent:call-agent", "agent", "turn-1", 10, "running", "active", 1, json.dumps(payload, ensure_ascii=False), 100000, 100000),
        )
        await svc.db.conn.execute(
            """
            INSERT INTO web_operations (
              conversation_uuid, internal_chat_id, op_id, op_type, turn_uuid, display_seq,
              status, lifecycle, revision, payload_json, created_at_ms, updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "conv-zombie", -1, "stats:turn-1", "stats", "turn-1", 20, "", "informational", 1,
                json.dumps({
                    "modelCalls": 5,
                    "modelOk": 5,
                    "toolCalls": 8,
                    "expertModelCalls": 0,
                    "expertToolCalls": 0,
                    "expertTasks": 0,
                    "usage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 0, "cacheWriteTokens": 0, "totalTokens": 120},
                    "expertUsage": {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0, "totalTokens": 0},
                    "costUsd": 0.4,
                }, ensure_ascii=False),
                100001, 100001,
            ),
        )
        await svc.db.conn.commit()

        changed = await svc._mark_interrupted_web_agent_operations()

        assert changed == 2
        cur = await svc.db.conn.execute(
            "SELECT status, lifecycle, revision, payload_json FROM web_operations WHERE conversation_uuid=? AND op_id=?",
            ("conv-zombie", "agent:call-agent"),
        )
        row = await cur.fetchone()
        assert row["status"] == "interrupted"
        assert row["lifecycle"] == "terminal"
        assert row["revision"] == 2
        next_payload = json.loads(row["payload_json"])
        assert next_payload["status"] == "interrupted"
        assert next_payload["task"]["status"] == "interrupted"
        assert next_payload["task"]["currentStatus"] == "interrupted by OpenBear startup"
        assert next_payload["task"]["error"] == "interrupted by OpenBear startup"
        assert json.loads(next_payload["resultText"])["status"] == "interrupted"
        cur = await svc.db.conn.execute(
            "SELECT revision, payload_json FROM web_operations WHERE conversation_uuid=? AND op_id=?",
            ("conv-zombie", "stats:turn-1"),
        )
        stats_row = await cur.fetchone()
        stats_payload = json.loads(stats_row["payload_json"])
        assert stats_row["revision"] == 2
        assert stats_payload["modelCalls"] == 15
        assert stats_payload["modelOk"] == 15
        assert stats_payload["toolCalls"] == 39
        assert stats_payload["expertModelCalls"] == 10
        assert stats_payload["expertToolCalls"] == 31
        assert stats_payload["expertTasks"] == 1
        assert stats_payload["expertUsage"] == {
            "inputTokens": 78681,
            "outputTokens": 2618,
            "cacheReadTokens": 378880,
            "cacheWriteTokens": 0,
            "totalTokens": 460179,
        }
        assert stats_payload["expertTaskUuids"] == [task_uuid]
        assert stats_payload["costUsd"] == pytest.approx(0.4914946)

        assert await svc._mark_interrupted_web_agent_operations() == 0

        # A historical terminal card is immutable even if aggregate status
        # normalization would now call the same failed/cancelled task "partial".
        await svc.db.conn.execute(
            "UPDATE rath_tasks SET status='cancelled', current_status='任务已取消' WHERE task_uuid=?",
            (task_uuid,),
        )
        await svc.db.conn.execute(
            "UPDATE web_operations SET status='cancelled', lifecycle='terminal' WHERE conversation_uuid=? AND op_id=?",
            ("conv-zombie", "agent:call-agent"),
        )
        await svc.db.conn.commit()
        assert await svc._mark_interrupted_web_agent_operations() == 0
        cur = await svc.db.conn.execute(
            "SELECT status, lifecycle, revision FROM web_operations WHERE conversation_uuid=? AND op_id=?",
            ("conv-zombie", "agent:call-agent"),
        )
        immutable_row = await cur.fetchone()
        assert immutable_row["status"] == "cancelled"
        assert immutable_row["lifecycle"] == "terminal"
        assert immutable_row["revision"] == 2
    finally:
        await svc.db.close()
        await svc.http.close()
        await svc.mem.close()


async def test_startup_reconciles_stale_web_controller_runtime_and_conversation(tmp_path):
    svc = Services(_cfg(tmp_path), SimpleNamespace())  # type: ignore[arg-type]
    await svc.db.connect()
    try:
        await svc.db.conn.execute(
            """
            INSERT INTO web_conversations (
              conversation_uuid, owner_chat_id, internal_chat_id, title, model,
              status, current_status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            ("conv-runtime-zombie", 123, -7, "Zombie runtime", "openai/gpt", "running", "正在思考", 100, 100),
        )
        runtime_rows = [
            ("run:exec-2", "run", "running", "active", {"runId": "exec-2", "status": "running"}),
            ("status:exec-2", "status", "running", "active", {"runId": "exec-2", "statusText": "正在思考", "active": True}),
            ("tool:call-running", "tool", "running", "active", {"runId": "exec-2", "toolName": "Read", "status": "running"}),
            ("agent_control:call-message", "agent_control", "running", "active", {"runId": "exec-2", "toolName": "AgentMessage", "status": "running"}),
            ("reasoning:turn-root:1", "reasoning", "running", "active", {"text": "half", "complete": False}),
            ("assistant:turn-root:1", "assistant_message", "running", "active", {"text": "partial", "complete": False}),
        ]
        for index, (op_id, op_type, status, lifecycle, payload) in enumerate(runtime_rows, 1):
            await svc.db.conn.execute(
                """
                INSERT INTO web_operations (
                  conversation_uuid, internal_chat_id, op_id, op_type, turn_uuid,
                  run_root_turn_uuid, run_id, display_seq, status, lifecycle, revision,
                  payload_json, created_at_ms, updated_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("conv-runtime-zombie", -7, op_id, op_type, "turn-root", "turn-root", "exec-2", index * 10, status, lifecycle, 1, json.dumps(payload), 100000, 100000),
            )
        await svc.db.conn.commit()

        changed = await svc._mark_interrupted_web_runtime_operations()

        assert changed == 7
        cur = await svc.db.conn.execute(
            "SELECT status,current_status FROM web_conversations WHERE conversation_uuid='conv-runtime-zombie'"
        )
        conversation = await cur.fetchone()
        assert conversation["status"] == "idle"
        assert conversation["current_status"] == "已中断（服务重启）"
        cur = await svc.db.conn.execute(
            "SELECT op_type,status,lifecycle,payload_json FROM web_operations WHERE conversation_uuid='conv-runtime-zombie' ORDER BY display_seq"
        )
        operations = await cur.fetchall()
        assert all(row["status"] == "interrupted" for row in operations)
        assert all(row["lifecycle"] == "terminal" for row in operations)
        payloads = {row["op_type"]: json.loads(row["payload_json"]) for row in operations}
        assert payloads["run"]["runId"] == "exec-2"
        assert payloads["run"]["interruptedBy"] == "service_restart"
        assert payloads["status"]["active"] is False
        assert payloads["status"]["statusText"] == "已中断（服务重启）"
        assert payloads["tool"]["status"] == "interrupted"
        assert payloads["agent_control"]["status"] == "interrupted"
        assert payloads["agent_control"]["interruptedBy"] == "service_restart"
        assert payloads["reasoning"]["complete"] is True
        assert payloads["assistant_message"]["complete"] is True
        assert await svc._mark_interrupted_web_runtime_operations() == 0
    finally:
        await svc.db.close()
        await svc.http.close()
        await svc.mem.close()


async def test_running_operations_are_visible_for_status_panel(db):
    await db.conn.execute(
        "INSERT INTO operations (operation_uuid, chat_id, kind, status, detail_json, started_at) VALUES (?,?,?,?,?,?)",
        ("op-visible-123", 456, "memory_import", "running", "{}", 1800000000),
    )
    await db.conn.commit()

    lines = await _running_operation_lines(SimpleNamespace(db=db))

    text = "\n".join(lines)
    assert "高风险操作进行中" in text
    assert "memory_import" in text
    assert "op-visib" in text

