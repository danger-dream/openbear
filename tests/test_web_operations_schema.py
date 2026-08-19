from __future__ import annotations

import json
import sqlite3

import aiosqlite
import pytest

from app.db.engine import DB
from app.web_console.operation_store import WebAdminOperationsMixin
from app.web_operations import (
    frame_public,
    is_context_compaction_operation,
    operation_public,
    tool_event_operation_specs,
    web_event_operation_specs,
)


class _OperationStore(WebAdminOperationsMixin):
    def __init__(self, db: DB) -> None:
        self.db = db


async def _table_columns(db: DB, table: str) -> set[str]:
    cur = await db.conn.execute(f"PRAGMA table_info({table})")
    return {str(row["name"]) for row in await cur.fetchall()}


async def _index_names(db: DB, table: str) -> set[str]:
    cur = await db.conn.execute(f"PRAGMA index_list({table})")
    return {str(row["name"]) for row in await cur.fetchall()}


def test_agent_operation_waiting_control_has_priority_over_partial_results():
    specs = tool_event_operation_specs(
        "tool_progress",
        turn_uuid="turn-1",
        tool_call_id="call-1",
        name="Agent",
        arguments="{}",
        payload={
            "toolName": "Agent",
            "results": [
                {"status": "completed", "task": {"status": "completed"}},
                {"status": "needs_openbear_control", "task": {"status": "needs_openbear_control"}},
            ],
        },
    )

    agent_spec = [spec for spec in specs if spec["op_type"] == "agent"][-1]
    assert agent_spec["status"] == "needs_openbear_control"
    assert agent_spec["lifecycle"] == "waiting_control"


def test_agent_stop_result_is_control_and_only_patches_existing_agent_task():
    specs = tool_event_operation_specs(
        "tool_result",
        turn_uuid="turn-1",
        tool_call_id="stop-call",
        name="AgentStop",
        arguments='{"to":"task-1"}',
        result='{"ok":true,"stopped":true,"status":"cancelled","taskUuid":"task-1","task":{"taskUuid":"task-1","status":"cancelled"}}',
    )

    agent_spec = [spec for spec in specs if spec["op_type"] == "agent"][-1]
    assert agent_spec["op_id"] == "agent:task-1"
    assert agent_spec["action"] == "end"
    assert agent_spec["status"] == "cancelled"
    assert agent_spec["skip_if_missing"] is True
    control_spec = [spec for spec in specs if spec["op_id"] == "agent_control:stop-call"][-1]
    assert control_spec["op_type"] == "agent_control"
    assert control_spec["action"] == "end"
    assert control_spec["status"] == "completed"
    assert control_spec["lifecycle"] == "terminal"
    assert control_spec["payload"]["taskUuid"] == "task-1"
    assert control_spec["payload"]["task"]["status"] == "cancelled"


def test_root_retry_wait_maps_each_attempt_to_stable_model_retry_operation():
    event = {
        "type": "retry_wait",
        "turnUuid": "turn-1",
        "executionRunUuid": "run-1",
        "retry": {
            "active": True,
            "attempt": 2,
            "maxRetries": 10,
            "delayMs": 5000,
            "retryAtMs": 123456,
            "reason": "rate_limit",
            "summary": "请求频率过高，请稍后重试",
            "error": "busy",
            "transportStatus": 503,
            "upstreamStatus": 429,
            "rootCause": {"status": 429, "classification": "rate_limit"},
            "attempts": [{"status": 429}],
            "details": {"summary": "请求频率过高，请稍后重试"},
            "taskUuid": "",
        },
    }

    active = web_event_operation_specs(event)
    resumed = web_event_operation_specs({
        **event,
        "retry": {**event["retry"], "active": False, "delayMs": 0, "retryAtMs": 0, "status": "resumed"},
    })
    next_attempt = web_event_operation_specs({
        **event,
        "retry": {**event["retry"], "attempt": 3},
    })

    assert len(active) == len(resumed) == len(next_attempt) == 1
    start_spec = active[0]
    end_spec = resumed[0]
    assert start_spec["op_id"] == end_spec["op_id"] == "model-retry:run-1:2"
    assert start_spec["op_type"] == end_spec["op_type"] == "model_retry"
    assert start_spec["action"] == "start"
    assert end_spec["action"] == "end"
    assert start_spec["source"] == end_spec["source"] == "model_retry"
    assert start_spec["run_id"] == end_spec["run_id"] == "run-1"
    assert start_spec["turn_uuid"] == end_spec["turn_uuid"] == "turn-1"
    assert start_spec["payload"]["attempt"] == 2
    assert start_spec["payload"]["maxRetries"] == 10
    assert start_spec["payload"]["delayMs"] == 5000
    assert start_spec["payload"]["reason"] == "rate_limit"
    assert start_spec["payload"]["summary"] == "请求频率过高，请稍后重试"
    assert start_spec["payload"]["transportStatus"] == 503
    assert start_spec["payload"]["upstreamStatus"] == 429
    assert start_spec["payload"]["rootCause"]["status"] == 429
    assert start_spec["payload"]["attempts"] == [{"status": 429}]
    assert start_spec["payload"]["details"]["summary"] == "请求频率过高，请稍后重试"
    assert start_spec["payload"]["error"] == "busy"
    assert start_spec["payload"]["active"] is True
    assert end_spec["payload"]["active"] is False
    assert end_spec["payload"]["terminal"] is True
    assert "taskUuid" not in start_spec["payload"]
    assert "taskUuid" not in start_spec["payload"]["retry"]
    assert next_attempt[0]["op_id"] == "model-retry:run-1:3"


def test_agent_retry_wait_is_not_projected_to_root_operations():
    specs = web_event_operation_specs({
        "type": "retry_wait",
        "turnUuid": "root-turn",
        "executionRunUuid": "root-run",
        "retry": {
            "active": True,
            "scope": "model_call",
            "taskUuid": "rath-task-1",
            "attempt": 1,
            "maxRetries": 10,
        },
    })

    assert specs == []


def test_context_compaction_tool_operation_carries_unified_metadata_and_safe_preview():
    metadata = {
        "compactionId": "context-compaction:77",
        "summaryId": 77,
        "scope": "root",
        "source": "tool_batch",
        "status": "completed",
        "beforeTokens": 90_000,
        "afterTokens": 12_000,
        "summaryChars": 45_000,
        "outputAvailable": True,
        "summaryRef": "/api/conversations/conv/compactions/77",
    }
    start_metadata = {**metadata, "status": "running", "outputAvailable": False}

    started = tool_event_operation_specs(
        "tool_start",
        turn_uuid="turn-1",
        tool_call_id="context-compaction:77",
        name="ContextCompaction",
        arguments=json.dumps(start_metadata),
        line="compacting",
    )[-1]
    finished = tool_event_operation_specs(
        "tool_result",
        turn_uuid="turn-1",
        tool_call_id="context-compaction:77",
        name="ContextCompaction",
        arguments=json.dumps(metadata),
        result="summary preview",
    )[-1]

    assert started["op_id"] == finished["op_id"] == "tool:context-compaction:77"
    assert started["op_type"] == finished["op_type"] == "context_compaction"
    assert started["source"] == finished["source"] == "context_compaction"
    assert started["payload"]["status"] == "running"
    assert started["payload"]["outputAvailable"] is False
    for key, value in metadata.items():
        if key not in {"status", "outputAvailable"}:
            assert started["payload"][key] == value
        assert finished["payload"][key] == value
    assert finished["payload"]["outputPreview"] == "summary preview"
    assert finished["payload"]["result"] == "summary preview"


def test_context_compaction_classifier_and_public_projection_are_narrow_and_transport_consistent():
    payload = {
        "toolCallId": "context-compaction:77",
        "toolName": "ContextCompaction",
        "compactionId": "context-compaction:77",
        "summaryId": 77,
        "scope": "root",
        "status": "completed",
        "result": "private summary",
    }
    historical = {
        "conversation_uuid": "conv-1",
        "op_id": "tool:context-compaction:77",
        "op_type": "tool",
        "source": "context_compaction",
        "turn_uuid": "turn-1",
        "run_root_turn_uuid": "turn-1",
        "display_seq": 940,
        "revision": 2,
        "status": "completed",
        "lifecycle": "terminal",
        "payload": payload,
    }
    refill_frame = {
        **historical,
        # Frames have no persisted source column, so compatibility must also
        # recognize the stable identity plus the valid payload shape.
        "source": "",
        "frame_seq": 3,
        "action": "end",
    }

    assert is_context_compaction_operation({"op_type": "context_compaction", "payload": {}}) is True
    assert is_context_compaction_operation(historical) is True
    assert is_context_compaction_operation(refill_frame) is True
    assert operation_public(historical, include_tool_details=False)["opType"] == "context_compaction"
    projected_frame = frame_public(refill_frame)
    assert projected_frame["opType"] == "context_compaction"
    assert projected_frame["displaySeq"] == 940
    assert projected_frame["turnUuid"] == "turn-1"

    ordinary = {
        "op_type": "tool",
        "op_id": "tool:bash-1",
        "source": "tool",
        "internal": True,
        "payload": {"toolName": "Bash", "arguments": "{}"},
    }
    spoofed_id = {
        **ordinary,
        "op_id": "tool:context-compaction:not-valid",
        "payload": {"toolName": "ContextCompaction"},
    }
    assert is_context_compaction_operation(ordinary) is False
    assert is_context_compaction_operation(spoofed_id) is False
    assert operation_public(ordinary)["opType"] == "tool"


async def test_context_compaction_live_start_and_end_update_one_canonical_card(tmp_path):
    db = DB(str(tmp_path / "context-compaction-operation.db"))
    await db.connect()
    store = _OperationStore(db)
    metadata = {
        "compactionId": "context-compaction:91",
        "summaryId": 91,
        "scope": "root",
        "source": "tool_batch",
        "beforeTokens": 90_000,
    }
    base = {
        "conversationUuid": "conv-compact",
        "chatId": -1,
        "turnUuid": "turn-compact",
        "runRootTurnUuid": "turn-compact",
        "toolCallId": "context-compaction:91",
        "name": "ContextCompaction",
    }
    try:
        await store._publish_native_operations({
            **base,
            "type": "tool_start",
            "arguments": json.dumps({**metadata, "status": "running"}),
        })
        first = await (await db.conn.execute(
            "SELECT op_type, display_seq, revision FROM web_operations "
            "WHERE conversation_uuid=? AND op_id=?",
            ("conv-compact", "tool:context-compaction:91"),
        )).fetchone()
        await store._publish_native_operations({
            **base,
            "type": "tool_result",
            "arguments": json.dumps({**metadata, "status": "completed"}),
            "result": "summary preview",
        })
        rows = await (await db.conn.execute(
            "SELECT op_id, op_type, display_seq, revision, status, turn_uuid, run_root_turn_uuid, task_uuid "
            "FROM web_operations WHERE conversation_uuid=?",
            ("conv-compact",),
        )).fetchall()
        frames = await store._web_frames("conv-compact")

        assert first["op_type"] == "context_compaction"
        assert len(rows) == 1
        assert rows[0]["op_id"] == "tool:context-compaction:91"
        assert rows[0]["op_type"] == "context_compaction"
        assert rows[0]["display_seq"] == first["display_seq"]
        assert rows[0]["revision"] == 2
        assert rows[0]["status"] == "completed"
        assert rows[0]["turn_uuid"] == rows[0]["run_root_turn_uuid"] == "turn-compact"
        assert rows[0]["task_uuid"] == ""
        assert [frame["opType"] for frame in frames] == ["context_compaction", "context_compaction"]
        assert [frame["action"] for frame in frames] == ["start", "end"]
        assert {frame["displaySeq"] for frame in frames} == {first["display_seq"]}
    finally:
        await db.close()


async def test_model_retry_upsert_preserves_display_seq_and_partial_assistant_snapshot(tmp_path):
    db = DB(str(tmp_path / "retry-operation.db"))
    await db.connect()
    store = _OperationStore(db)
    base = {
        "conversationUuid": "conv-1",
        "chatId": -1,
        "turnUuid": "turn-1",
        "executionRunUuid": "run-1",
    }
    try:
        await store._publish_native_operations({**base, "type": "delta", "text": "partial "})
        await store._publish_native_operations({
            **base,
            "type": "retry_wait",
            "retry": {
                "active": True,
                "attempt": 1,
                "maxRetries": 10,
                "delayMs": 250,
                "retryAtMs": 123456,
                "reason": "upstream_error",
                "error": "connection reset",
            },
        })
        cur = await db.conn.execute(
            "SELECT display_seq FROM web_operations WHERE conversation_uuid=? AND op_id=?",
            ("conv-1", "model-retry:run-1:1"),
        )
        first_display_seq = int((await cur.fetchone())["display_seq"])

        await store._publish_native_operations({
            **base,
            "type": "retry_wait",
            "retry": {
                "active": False,
                "attempt": 1,
                "maxRetries": 10,
                "delayMs": 0,
                "retryAtMs": 0,
                "reason": "upstream_error",
                "error": "connection reset",
                "status": "resumed",
            },
        })
        await store._publish_native_operations({**base, "type": "delta", "text": "partial continued"})

        cur = await db.conn.execute(
            "SELECT op_id, op_type, turn_uuid, run_id, task_uuid, display_seq, status, lifecycle, source, revision, payload_json "
            "FROM web_operations WHERE conversation_uuid=? ORDER BY display_seq, id",
            ("conv-1",),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        retry_rows = [row for row in rows if row["op_type"] == "model_retry"]
        assistant_rows = [row for row in rows if row["op_type"] == "assistant_message"]
        assert len(retry_rows) == 1
        assert retry_rows[0]["op_id"] == "model-retry:run-1:1"
        assert retry_rows[0]["display_seq"] == first_display_seq
        assert retry_rows[0]["revision"] == 2
        assert retry_rows[0]["status"] == "completed"
        assert retry_rows[0]["lifecycle"] == "terminal"
        assert retry_rows[0]["source"] == "model_retry"
        assert retry_rows[0]["turn_uuid"] == "turn-1"
        assert retry_rows[0]["run_id"] == "run-1"
        assert retry_rows[0]["task_uuid"] == ""
        assert len(assistant_rows) == 1
        assert json.loads(assistant_rows[0]["payload_json"])["text"] == "partial continued"
        assert assistant_rows[0]["display_seq"] < retry_rows[0]["display_seq"]

        cur = await db.conn.execute(
            "SELECT display_seq, action FROM web_event_frames WHERE conversation_uuid=? AND op_id=? ORDER BY revision",
            ("conv-1", "model-retry:run-1:1"),
        )
        retry_frames = await cur.fetchall()
        assert [(row["display_seq"], row["action"]) for row in retry_frames] == [
            (first_display_seq, "start"),
            (first_display_seq, "end"),
        ]
    finally:
        await db.close()


async def test_web_operation_v2_tables_created_on_fresh_db(tmp_path):
    db = DB(str(tmp_path / "fresh.db"))
    await db.connect()
    try:
        frame_columns = await _table_columns(db, "web_event_frames")
        operation_columns = await _table_columns(db, "web_operations")

        assert {
            "conversation_uuid",
            "internal_chat_id",
            "owner_chat_id",
            "frame_seq",
            "op_id",
            "op_type",
            "action",
            "turn_uuid",
            "parent_turn_uuid",
            "run_root_turn_uuid",
            "revision",
            "display_seq",
            "payload_json",
            "debug_json",
            "created_at_ms",
            "updated_at_ms",
        } <= frame_columns
        assert {
            "conversation_uuid",
            "internal_chat_id",
            "op_id",
            "op_type",
            "turn_uuid",
            "parent_turn_uuid",
            "run_root_turn_uuid",
            "display_seq",
            "status",
            "lifecycle",
            "internal",
            "source",
            "transcript_message_ids_json",
            "revision",
            "payload_json",
            "created_at_ms",
            "updated_at_ms",
        } <= operation_columns

        frame_indexes = await _index_names(db, "web_event_frames")
        operation_indexes = await _index_names(db, "web_operations")
        assert "ux_web_event_frames_conv_seq" in frame_indexes
        assert "idx_web_event_frames_conv_op_revision" in frame_indexes
        assert "ux_web_operations_conv_op" in operation_indexes
        assert "idx_web_operations_conv_display" in operation_indexes

        await db.conn.execute(
            """
            INSERT INTO web_event_frames (
              conversation_uuid, internal_chat_id, owner_chat_id, frame_seq,
              op_id, op_type, action, turn_uuid, revision, display_seq,
              payload_json, created_at_ms, updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("conv-1", -1, 123, 1, "agent:call-1", "agent", "start", "turn-1", 1, 10, '{"status":"queued"}', 1000, 1000),
        )
        await db.conn.execute(
            """
            INSERT INTO web_event_frames (
              conversation_uuid, internal_chat_id, owner_chat_id, frame_seq,
              op_id, op_type, action, turn_uuid, revision, display_seq,
              payload_json, created_at_ms, updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("conv-1", -1, 123, 2, "agent:call-1", "agent", "patch", "turn-1", 2, 10, '{"status":"running"}', 1001, 1001),
        )
        await db.conn.execute(
            """
            INSERT INTO web_operations (
              conversation_uuid, internal_chat_id, op_id, op_type, turn_uuid,
              display_seq, status, lifecycle, revision, payload_json,
              created_at_ms, updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("conv-1", -1, "agent:call-1", "agent", "turn-1", 10, "running", "active", 2, '{"status":"running"}', 1000, 1001),
        )
        await db.conn.commit()

        with pytest.raises(aiosqlite.IntegrityError):
            await db.conn.execute(
                """
                INSERT INTO web_event_frames (
                  conversation_uuid, frame_seq, op_id, op_type, action,
                  revision, display_seq, payload_json, created_at_ms, updated_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                ("conv-1", 2, "agent:call-1", "agent", "patch", 3, 10, '{}', 1002, 1002),
            )
    finally:
        await db.close()


async def test_web_operation_snapshot_frame_migration_repairs_revision_drift_once(tmp_path):
    db_path = tmp_path / "drift.db"
    db = DB(str(db_path))
    await db.connect()
    await db.conn.execute(
        """
        INSERT INTO web_conversations (
          conversation_uuid, owner_chat_id, internal_chat_id, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?)
        """,
        ("conv-drift", 123, -1, "drift", 1, 1),
    )
    await db.conn.execute(
        """
        INSERT INTO web_event_frames (
          conversation_uuid, internal_chat_id, owner_chat_id, frame_seq,
          op_id, op_type, action, turn_uuid, revision, display_seq,
          payload_json, created_at_ms, updated_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("conv-drift", -1, 123, 1, "run:r1", "run", "start", "r1", 1, 10, '{"status":"running"}', 1000, 1000),
    )
    await db.conn.execute(
        """
        INSERT INTO web_operations (
          conversation_uuid, internal_chat_id, op_id, op_type, turn_uuid,
          display_seq, status, lifecycle, revision, payload_json,
          created_at_ms, updated_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("conv-drift", -1, "run:r1", "run", "r1", 10, "interrupted", "terminal", 2, '{"status":"interrupted"}', 1000, 2000),
    )
    await db.conn.commit()
    await db.close()

    repaired = DB(str(db_path))
    await repaired.connect()
    try:
        cur = await repaired.conn.execute(
            "SELECT action, revision, payload_json, debug_json FROM web_event_frames WHERE conversation_uuid=? AND op_id=? ORDER BY revision",
            ("conv-drift", "run:r1"),
        )
        frames = await cur.fetchall()
        assert [(row["action"], row["revision"]) for row in frames] == [("start", 1), ("snapshot", 2)]
        assert json.loads(frames[-1]["payload_json"])["status"] == "interrupted"
        assert json.loads(frames[-1]["debug_json"])["source"] == "legacy_snapshot_reconcile"
    finally:
        await repaired.close()

    idempotent = DB(str(db_path))
    await idempotent.connect()
    try:
        cur = await idempotent.conn.execute(
            "SELECT COUNT(*) AS n FROM web_event_frames WHERE conversation_uuid=? AND op_id=?",
            ("conv-drift", "run:r1"),
        )
        assert int((await cur.fetchone())["n"]) == 2
    finally:
        await idempotent.close()


async def test_web_operation_terminal_time_backfill_survives_frame_retention(tmp_path):
    db_path = tmp_path / "terminal-time.db"
    db = DB(str(db_path))
    await db.connect()
    await db.conn.execute(
        """
        INSERT INTO web_event_frames (
          conversation_uuid, frame_seq, op_id, op_type, action,
          revision, display_seq, payload_json, created_at_ms, updated_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?), (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "conv-terminal", 1, "reasoning:turn:0", "reasoning", "append",
            1, 10, '{"delta":"思考","complete":false}', 1000, 1000,
            "conv-terminal", 2, "reasoning:turn:0", "reasoning", "end",
            2, 10, '{"complete":true}', 2500, 2500,
        ),
    )
    await db.conn.execute(
        """
        INSERT INTO web_operations (
          conversation_uuid, op_id, op_type, turn_uuid, display_seq,
          status, lifecycle, revision, payload_json, created_at_ms, updated_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "conv-terminal", "reasoning:turn:0", "reasoning", "turn", 10,
            "completed", "terminal", 2, '{"text":"思考","complete":true}', 1000, 9000,
        ),
    )
    await db.conn.commit()
    await db.close()

    migrated = DB(str(db_path))
    await migrated.connect()
    cur = await migrated.conn.execute(
        "SELECT payload_json FROM web_operations WHERE conversation_uuid=? AND op_id=?",
        ("conv-terminal", "reasoning:turn:0"),
    )
    payload = json.loads((await cur.fetchone())["payload_json"])
    assert payload["terminalAtMs"] == 2500
    await migrated.conn.execute("DELETE FROM web_event_frames WHERE conversation_uuid=?", ("conv-terminal",))
    await migrated.conn.commit()
    await migrated.close()

    retained = DB(str(db_path))
    await retained.connect()
    try:
        cur = await retained.conn.execute(
            "SELECT payload_json FROM web_operations WHERE conversation_uuid=? AND op_id=?",
            ("conv-terminal", "reasoning:turn:0"),
        )
        assert json.loads((await cur.fetchone())["payload_json"])["terminalAtMs"] == 2500
    finally:
        await retained.close()


async def test_web_operation_v2_partial_table_migration_adds_missing_columns(tmp_path):
    db_path = tmp_path / "partial.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE web_event_frames (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conversation_uuid TEXT NOT NULL
            );
            CREATE TABLE web_operations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conversation_uuid TEXT NOT NULL
            );
            """
        )

    db = DB(str(db_path))
    await db.connect()
    try:
        assert "frame_seq" in await _table_columns(db, "web_event_frames")
        assert "updated_at_ms" in await _table_columns(db, "web_event_frames")
        assert "op_id" in await _table_columns(db, "web_operations")
        assert "transcript_message_ids_json" in await _table_columns(db, "web_operations")
        assert "ux_web_event_frames_conv_seq" in await _index_names(db, "web_event_frames")
        assert "ux_web_operations_conv_op" in await _index_names(db, "web_operations")
    finally:
        await db.close()


def test_operation_public_exposes_target_fields_for_agent_control_and_run():
    from app.web_operations import frame_public, operation_public

    agent = operation_public({
        "conversation_uuid": "conv",
        "op_id": "agent-control:c1",
        "op_type": "agent_control",
        "turn_uuid": "legacy-turn",
        "payload_json": '{"taskUuid":"task-1","controlAction":"steer"}',
        "transcript_message_ids_json": "[]",
    })
    assert agent["targetType"] == "task"
    assert agent["targetId"] == "task-1"
    assert agent["taskUuid"] == "task-1"
    assert agent["runId"] == "legacy-turn"

    frame = frame_public({
        "conversation_uuid": "conv",
        "frame_seq": 1,
        "op_id": "assistant:run-1:0",
        "op_type": "assistant_message",
        "turn_uuid": "run-1",
        "payload_json": '{"text":"ok"}',
        "debug_json": "{}",
    })
    assert frame["targetType"] == "run"
    assert frame["targetId"] == "run-1"
    assert frame["runId"] == "run-1"


def test_notice_operation_is_informational_even_with_waiting_control_status():
    from app.web_operations import operation_public, status_lifecycle

    assert status_lifecycle("notice", "needs_openbear_control", {}) == "informational"
    notice = operation_public({
        "conversation_uuid": "conv",
        "op_id": "notice:task:old",
        "op_type": "notice",
        "status": "needs_openbear_control",
        "lifecycle": "waiting_control",
        "payload_json": '{"taskUuid":"old","status":"needs_openbear_control"}',
        "transcript_message_ids_json": "[]",
    })
    assert notice["status"] == "needs_openbear_control"
    assert notice["lifecycle"] == "informational"


def test_operation_public_tool_summary_omits_details_but_preserves_existing_card_preview_source():
    from app.web_operations import operation_public

    payload = {
        "toolCallId": "call-1",
        "name": "Bash",
        "toolName": "Bash",
        "arguments": json.dumps({
            "description": "检查当前服务状态",
            "command": "x" * 4_000,
        }, ensure_ascii=False),
        "args": "x" * 4_000,
        "result": "[错误] command failed",
        # This is a renderer line, not a card-preview payload. It already has
        # the icon and tool name that the browser card renders itself.
        "preview": "💻 Bash: xxxxxxxxxx",
        "durationMs": 12,
        "status": "completed",
    }
    row = {
        "conversation_uuid": "conv",
        "op_id": "tool:call-1",
        "op_type": "tool",
        "status": "completed",
        "revision": 7,
        "payload": payload,
        "transcript_message_ids_json": "[]",
    }

    summary = operation_public(row, include_tool_details=False)
    assert summary["detailAvailable"] is True
    assert summary["detailLoaded"] is False
    assert summary["detailRevision"] == 7
    assert summary["payload"]["toolCallId"] == "call-1"
    assert summary["payload"]["resultState"] == "error"
    assert json.loads(summary["payload"]["previewArguments"]) == {"description": "检查当前服务状态"}
    assert "preview" not in summary["payload"]
    assert "args" not in summary["payload"]
    assert "arguments" not in summary["payload"]
    assert "result" not in summary["payload"]

    no_preview_source = operation_public({
        **row,
        "payload": {**payload, "preview": "💻 Bash: stale line", "arguments": "", "args": ""},
    }, include_tool_details=False)
    assert "preview" not in no_preview_source["payload"]
    assert "previewArguments" not in no_preview_source["payload"]

    detail = operation_public(row)
    assert detail["detailLoaded"] is True
    assert detail["payload"]["arguments"] == payload["arguments"]
    assert detail["payload"]["result"] == payload["result"]


def test_operation_public_task_memory_summary_keeps_action_and_target_for_lazy_card():
    from app.web_operations import operation_public

    payload = {
        "toolCallId": "call-memory",
        "name": "TaskMemory",
        "toolName": "TaskMemory",
        "arguments": json.dumps({
            "action": "delete",
            "memoryUuid": "memory-123",
            "body": "task-memory body " * 100,
        }, ensure_ascii=False),
        "result": json.dumps({
            "memory": {
                "name": "案件要点",
                "description": "记录案件要点",
                "memoryUuid": "memory-123",
                "body": "full result body",
            },
        }, ensure_ascii=False),
        "status": "completed",
    }
    summary = operation_public({
        "conversation_uuid": "conv",
        "op_id": "tool:call-memory",
        "op_type": "tool",
        "status": "completed",
        "payload": payload,
        "transcript_message_ids_json": "[]",
    }, include_tool_details=False)

    # Keep the action and human-readable target available after lazy detail is
    # stripped; bodies remain in the full detail payload, not the card headline.
    assert json.loads(summary["payload"]["previewArguments"]) == {
        "action": "delete",
        "memoryLabel": "记录案件要点",
        "memoryUuid": "memory-123",
    }
    assert "preview" not in summary["payload"]
    assert "arguments" not in summary["payload"]
    assert "result" not in summary["payload"]



def test_user_interaction_native_specs_share_identity_and_map_full_terminal_matrix():
    from app.web_operations import tool_event_operation_specs

    arguments = json.dumps({"action": "prompt", "title": "当时标题", "body": "问题"}, ensure_ascii=False)
    start = tool_event_operation_specs(
        "tool_start", turn_uuid="turn-ui", tool_call_id="ui-call", name="UserInteraction",
        arguments=arguments, ts=100,
    )[-1]
    assert (start["op_id"], start["op_type"], start["source"], start["action"]) == (
        "tool:ui-call", "user_interaction", "user_interaction", "start",
    )
    assert start["status"] == "running"
    assert start["payload"]["interactionStatus"] == "pending"

    expected = {
        "answered": ("completed", "end"),
        "cancelled": ("cancelled", "cancel"),
        "timeout": ("completed", "end"),
        "error": ("failed", "error"),
    }
    for interaction_status, (operation_status, action) in expected.items():
        terminal = tool_event_operation_specs(
            "tool_result", turn_uuid="turn-ui", tool_call_id="ui-call", name="UserInteraction",
            arguments=arguments, result=json.dumps({"status": interaction_status}), ts=200,
        )[-1]
        assert terminal["op_id"] == start["op_id"]
        assert terminal["op_type"] == "user_interaction"
        assert terminal["source"] == "user_interaction"
        assert terminal["status"] == operation_status
        assert terminal["action"] == action
        assert terminal["payload"]["interactionStatus"] == interaction_status


def test_user_interaction_public_canonicalization_summary_detail_and_negative_controls():
    from app.web_operations import frame_public, operation_public

    arguments = json.dumps({
        "action": "questionnaire", "title": "当时标题", "body": "正文不能进摘要",
        "questions": [{"id": "q", "question": "题目", "options": [{"label": "甲", "value": "a"}]}],
    }, ensure_ascii=False)
    result = json.dumps({"status": "answered", "answers": [{"questionId": "q", "selectedValues": ["a"], "text": "开放答案"}]}, ensure_ascii=False)
    legacy = {
        "conversation_uuid": "conv", "op_id": "tool:old-ui", "op_type": "tool", "source": "tool",
        "status": "running", "revision": 4, "display_seq": 9,
        "payload": {"name": "UserInteraction", "arguments": arguments, "result": result,
                    "preview": "答案预览", "searchText": "开放答案"},
        "transcript_message_ids_json": "[]",
    }
    summary = operation_public(legacy, include_tool_details=False)
    assert summary["opType"] == "user_interaction"
    assert summary["source"] == "user_interaction"
    assert summary["status"] == "completed"
    assert summary["payload"] == {
        "action": "questionnaire", "title": "当时标题", "status": "completed",
        "interactionStatus": "answered", "sensitive": False,
    }
    assert summary["detailAvailable"] is True and summary["detailLoaded"] is False
    detail = operation_public(legacy)
    assert detail["payload"]["arguments"] == arguments
    assert detail["payload"]["result"] == result
    frame = frame_public({**legacy, "action": "patch", "frame_seq": 1, "debug": {}})
    assert frame["opType"] == "user_interaction" and frame["action"] == "end"

    for ordinary in [
        {**legacy, "payload": {"name": "Read", "arguments": "{}"}},
        {**legacy, "op_type": "agent_control", "payload": {"name": "UserInteraction"}},
    ]:
        assert operation_public(ordinary, include_tool_details=False)["opType"] == ordinary["op_type"]


def test_user_interaction_confirm_summary_includes_confirmed_without_result_body():
    from app.web_operations import operation_public

    arguments = json.dumps({
        "action": "confirm", "title": "确认 Demo4 的现实验收边界", "body": "正文不能进摘要",
    }, ensure_ascii=False)
    row = {
        "conversation_uuid": "conv", "op_id": "tool:confirm-ui", "op_type": "user_interaction",
        "source": "user_interaction", "status": "completed", "revision": 2,
        "payload": {
            "name": "UserInteraction",
            "arguments": arguments,
            "result": json.dumps({"status": "answered", "confirmed": True, "choice": "confirm"}, ensure_ascii=False),
        },
        "transcript_message_ids_json": "[]",
    }
    summary = operation_public(row, include_tool_details=False)
    assert summary["payload"] == {
        "action": "confirm", "title": "确认 Demo4 的现实验收边界", "status": "completed",
        "interactionStatus": "answered", "sensitive": False, "confirmed": True,
    }
    assert not ({"body", "result", "choice", "label"} & set(summary["payload"]))

    rejected = operation_public({
        **row,
        "payload": {
            **row["payload"],
            "result": json.dumps({"status": "answered", "confirmed": False, "choice": "cancel"}, ensure_ascii=False),
        },
    }, include_tool_details=False)
    assert rejected["payload"]["confirmed"] is False

    choice_only = operation_public({
        **row,
        "payload": {
            **row["payload"],
            "result": json.dumps({"status": "answered", "choice": "confirm"}, ensure_ascii=False),
        },
    }, include_tool_details=False)
    assert choice_only["payload"]["confirmed"] is True

    unanswered = operation_public({
        **row,
        "payload": {k: v for k, v in row["payload"].items() if k != "result"},
    }, include_tool_details=False)
    assert "confirmed" not in unanswered["payload"]


def test_sensitive_user_interaction_public_surfaces_fail_closed_even_for_malformed_marker():
    from app.web_operations import frame_public, operation_public

    secret = "CHAIN-SECRET-4f91ac"
    cases = [
        json.dumps({"action": "prompt", "title": "密码", "body": "输入", "sensitive": True, "defaultValue": secret}, ensure_ascii=False),
        '{"action":"prompt","sensitive":true,"defaultValue":"' + secret + '"',
    ]
    for arguments in cases:
        row = {
            "conversation_uuid": "conv", "op_id": "tool:sensitive", "op_type": "user_interaction",
            "source": "user_interaction", "status": "completed", "revision": 2,
            "payload": {"name": "UserInteraction", "arguments": arguments,
                        "result": json.dumps({"status": "answered", "value": secret}),
                        "preview": secret, "searchText": secret},
            "transcript_message_ids_json": "[]",
        }
        summary = operation_public(row, include_tool_details=False)
        detail = operation_public(row)
        frame = frame_public({**row, "frame_seq": 1, "action": "end", "debug": {"defaultValue": secret, "value": secret, "searchText": secret}})
        for public_value in (summary, detail, frame):
            serialized = json.dumps(public_value, ensure_ascii=False)
            assert secret not in serialized
            assert "[敏感内容已隐藏]" in serialized or public_value is summary
        assert summary["payload"]["sensitive"] is True
        assert "defaultValue" not in summary["payload"]
        assert "searchText" not in frame["debug"]
