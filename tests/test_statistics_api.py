from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.web_console.statistics_api import read_statistics

BEIJING = ZoneInfo("Asia/Shanghai")


def _database(tmp_path):
    path = tmp_path / "statistics.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE web_conversation_folders (
          id INTEGER PRIMARY KEY, folder_uuid TEXT, owner_chat_id INTEGER,
          parent_uuid TEXT, name TEXT, display_order REAL
        );
        CREATE TABLE web_conversations (
          internal_chat_id INTEGER, conversation_uuid TEXT, owner_chat_id INTEGER,
          folder_uuid TEXT
        );
        CREATE TABLE messages (
          chat_id INTEGER, created_at INTEGER, role TEXT, task_uuid TEXT
        );
        CREATE TABLE model_calls (
          chat_id INTEGER, created_at INTEGER, model TEXT, call_kind TEXT,
          status TEXT, error_type TEXT, model_call_count INTEGER,
          model_ok_count INTEGER, model_fail_count INTEGER, model_retry_count INTEGER,
          input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
          cache_write_tokens INTEGER, cost_usd REAL, connect_ms INTEGER,
          first_token_ms INTEGER, total_time_ms INTEGER
        );
        CREATE TABLE tool_calls (
          chat_id INTEGER, created_at INTEGER, tool_name TEXT, status TEXT,
          duration_ms INTEGER
        );
        CREATE TABLE web_operations (
          conversation_uuid TEXT, op_type TEXT, created_at_ms INTEGER,
          payload_json TEXT
        );
        """
    )
    return path, connection


def test_statistics_uses_owner_folder_and_bounded_period(tmp_path):
    path, connection = _database(tmp_path)
    owner = 42
    now = datetime.now(BEIJING).replace(hour=12, minute=0, second=0, microsecond=0)
    current = int((now - timedelta(days=1)).timestamp())
    previous = int((now - timedelta(days=10)).timestamp())

    connection.executemany(
        "INSERT INTO web_conversation_folders VALUES (?,?,?,?,?,?)",
        [
            (1, "root", owner, "", "项目", 1),
            (2, "child", owner, "root", "子目录", 1),
            (3, "other", owner, "", "其他", 2),
        ],
    )
    connection.executemany(
        "INSERT INTO web_conversations VALUES (?,?,?,?)",
        [
            (101, "conv-a", owner, "child"),
            (102, "conv-b", owner, "other"),
            (201, "foreign", 99, "root"),
        ],
    )
    connection.executemany(
        "INSERT INTO messages VALUES (?,?,?,?)",
        [
            (101, current, "user", ""),
            (101, previous, "user", ""),
            (102, current, "user", ""),
            (201, current, "user", ""),
            (101, current, "assistant", ""),
            (101, current, "user", "agent-task"),
        ],
    )
    connection.executemany(
        "INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                101,
                current,
                "provider/model-a",
                "controller_request",
                "ok",
                "",
                1,
                1,
                0,
                0,
                100,
                20,
                300,
                10,
                0.5,
                100,
                400,
                1000,
            ),
            (
                101,
                current,
                "provider/model-b",
                "agent_request",
                "error",
                "timeout",
                1,
                0,
                1,
                1,
                50,
                5,
                0,
                0,
                0.1,
                0,
                0,
                6000,
            ),
            (
                101,
                previous,
                "provider/model-a",
                "controller_request",
                "ok",
                "",
                1,
                1,
                0,
                0,
                80,
                10,
                100,
                0,
                0.2,
                80,
                300,
                800,
            ),
            (
                102,
                current,
                "provider/model-c",
                "controller_request",
                "ok",
                "",
                1,
                1,
                0,
                0,
                999,
                99,
                999,
                0,
                9.0,
                50,
                200,
                500,
            ),
            (
                201,
                current,
                "provider/foreign",
                "controller_request",
                "ok",
                "",
                1,
                1,
                0,
                0,
                9999,
                999,
                0,
                0,
                99.0,
                50,
                200,
                500,
            ),
        ],
    )
    connection.executemany(
        "INSERT INTO tool_calls VALUES (?,?,?,?,?)",
        [
            (101, current, "Read", "ok", 20),
            (101, current, "Bash", "error", 1000),
            (102, current, "Write", "ok", 10),
        ],
    )
    connection.execute(
        "INSERT INTO web_operations VALUES (?,?,?,?)",
        ("conv-a", "stats", current * 1000, json.dumps({"reasoningMs": 2500})),
    )
    connection.commit()
    connection.close()

    result = read_statistics(path, owner, days=7, folder_uuid="root")

    summary = result["summary"]["current"]
    assert summary["activeConversations"] == 1
    assert summary["userTurns"] == 1
    assert summary["modelCalls"] == 2
    assert summary["modelOk"] == 1
    assert summary["modelFailed"] == 1
    assert summary["toolCalls"] == 2
    assert summary["inputTokens"] == 150
    assert summary["cacheReadTokens"] == 300
    assert summary["costUsd"] == 0.6
    assert result["summary"]["previous"]["modelCalls"] == 1
    assert {item["model"] for item in result["models"]} == {"provider/model-a", "provider/model-b"}
    assert result["latency"]["main"]["buckets"][0] == 1
    assert result["latency"]["agent"]["buckets"][1] == 1
    # The all-request average includes the Agent request, but the three-stage
    # breakdown must use only the one request with complete timing fields.
    assert result["latency"]["all"]["avgTotalMs"] == 3500
    assert result["latency"]["all"]["avgTimedTotalMs"] == 1000
    assert result["latency"]["all"]["avgConnectMs"] == 100
    assert result["latency"]["all"]["avgFirstTokenMs"] == 400
    assert result["latency"]["all"]["timingSamples"] == 1
    assert result["latency"]["all"]["percentiles"]["total"] == {
        "samples": 2,
        "p50Ms": 3500.0,
        "p90Ms": 5500.0,
        "p95Ms": 5750.0,
        "p99Ms": 5950.0,
        "maxMs": 6000.0,
    }
    assert result["latency"]["all"]["percentiles"]["firstToken"] == {
        "samples": 1,
        "p50Ms": 400.0,
        "p90Ms": 400.0,
        "p95Ms": 400.0,
        "p99Ms": 400.0,
        "maxMs": 400.0,
    }
    assert result["thinking"] == {
        "available": True,
        "totalMs": 2500,
        "observedRuns": 1,
        "daily": [
            {
                "date": (now - timedelta(days=1)).date().isoformat(),
                "reasoningMs": 2500,
                "observedRuns": 1,
            }
        ],
    }
    assert result["errors"] == [{"type": "timeout", "count": 1}]
    assert result["period"]["bucketHours"] == 6
    assert len(result["timeline"]) == 28
    active_bucket = next(
        item
        for item in result["timeline"]
        if item["bucket"].startswith((now - timedelta(days=1)).strftime("%Y-%m-%dT12:"))
    )
    assert active_bucket["modelCalls"] == 2
    assert active_bucket["inputTokens"] == 150
    assert active_bucket["reasoningMs"] == 2500
    assert {item["model"] for item in result["modelTimeline"]} == {
        "provider/model-a",
        "provider/model-b",
    }


def test_statistics_today_and_three_days_use_dense_time_buckets(tmp_path):
    path, connection = _database(tmp_path)
    connection.close()

    today = read_statistics(path, 42, days=1)
    three_days = read_statistics(path, 42, days=3)

    assert today["period"]["days"] == 1
    assert today["period"]["bucketHours"] == 1
    assert len(today["timeline"]) == 24
    assert three_days["period"]["days"] == 3
    assert three_days["period"]["bucketHours"] == 3
    assert len(three_days["timeline"]) == 24


def test_statistics_rejects_an_unknown_folder(tmp_path):
    path, connection = _database(tmp_path)
    connection.close()

    try:
        read_statistics(path, 42, days=30, folder_uuid="missing")
    except ValueError as exc:
        assert str(exc) == "folder_not_found"
    else:
        raise AssertionError("unknown folder must not silently expand to all conversations")


def test_statistics_auxiliary_queries_have_bounded_partial_indexes():
    connection = sqlite3.connect(":memory:")
    schema = Path("app/db/schema.sql").read_text(encoding="utf-8")
    connection.executescript(schema)

    message_plan = connection.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT chat_id, created_at FROM messages
        WHERE chat_id IN (?) AND created_at>=? AND created_at<?
          AND role='user' AND COALESCE(task_uuid,'')=''
        """,
        (1, 2, 3),
    ).fetchall()
    operation_plan = connection.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT created_at_ms FROM web_operations
        WHERE conversation_uuid IN (?) AND op_type='stats'
          AND created_at_ms>=? AND created_at_ms<?
        """,
        ("conversation", 2, 3),
    ).fetchall()
    connection.close()

    assert "idx_messages_user_chat_time" in " ".join(str(row) for row in message_plan)
    assert "idx_web_operations_stats_conversation_time" in " ".join(
        str(row) for row in operation_plan
    )
