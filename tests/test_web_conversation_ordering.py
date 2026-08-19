from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app.db.engine import DB
from app.web_console.conversations import WebAdminConversationsMixin


class _ConversationHarness(WebAdminConversationsMixin):
    def __init__(self, db: DB) -> None:
        self.db = db
        self._web_starting_turns: dict[str, Any] = {}
        self._web_live_streams: dict[str, Any] = {}
        self.rath = None
        self.runs = None

    async def _ensure_default_web_conversation(self, owner_chat_id: int) -> None:
        return None

    async def _reconcile_inactive_web_conversation_operations(
        self,
        row: dict[str, Any],
        *,
        source: str,
    ) -> list[dict[str, Any]]:
        return []

    async def _web_operation_facts_for_conversations(
        self,
        conversation_uuids: list[str],
    ) -> dict[str, dict[str, Any]]:
        return {}


@pytest.fixture
async def conversation_harness(tmp_path):
    db = DB(str(tmp_path / "ordering.db"))
    await db.connect()
    try:
        yield _ConversationHarness(db)
    finally:
        await db.close()


async def _insert_conversation(
    harness: _ConversationHarness,
    *,
    row_id: int,
    created_at: int | None,
    updated_at: int,
    pinned_at: int = 0,
    owner_chat_id: int = 123,
) -> None:
    await harness.db.conn.execute(
        """
        INSERT INTO web_conversations (
          id, conversation_uuid, owner_chat_id, internal_chat_id, title,
          status, current_status, created_at, updated_at, pinned_at, archived_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,0)
        """,
        (
            row_id,
            f"conversation-{row_id}",
            owner_chat_id,
            -row_id,
            f"conversation {row_id}",
            "idle",
            "就绪",
            created_at,
            updated_at,
            pinned_at,
        ),
    )


def _listed_ids(rows: list[dict[str, Any]]) -> list[int]:
    return [int(str(row["conversationUuid"]).rsplit("-", 1)[-1]) for row in rows]


async def test_list_order_uses_only_created_at_then_id_and_ignores_pin_and_updates(conversation_harness):
    harness = conversation_harness
    await _insert_conversation(harness, row_id=1, created_at=None, updated_at=9_999, pinned_at=9_999)
    await _insert_conversation(harness, row_id=2, created_at=0, updated_at=8_888, pinned_at=8_888)
    await _insert_conversation(harness, row_id=3, created_at=100, updated_at=7_777, pinned_at=7_777)
    await _insert_conversation(harness, row_id=4, created_at=100, updated_at=2)
    await _insert_conversation(harness, row_id=5, created_at=200, updated_at=1)
    await harness.db.conn.commit()

    assert _listed_ids(await harness._list_web_conversations(123)) == [5, 4, 3, 2, 1]

    # Rename/touch and pin/unpin mutate presentation fields, never list position.
    await harness.db.conn.execute(
        "UPDATE web_conversations SET title='renamed', updated_at=20000, pinned_at=20000 WHERE id=3"
    )
    await harness._touch_web_conversation("conversation-2", status="running", current_status="等待 Agent")
    await harness.db.conn.execute("UPDATE web_conversations SET pinned_at=0, updated_at=30000 WHERE id=1")
    await harness.db.conn.commit()

    rows = await harness._list_web_conversations(123)
    assert _listed_ids(rows) == [5, 4, 3, 2, 1]
    assert next(row for row in rows if row["conversationUuid"] == "conversation-3")["pinned"] is True


async def test_recent_activity_cannot_move_old_rows_into_created_at_limited_window(conversation_harness):
    harness = conversation_harness
    for row_id in range(1, 106):
        await _insert_conversation(
            harness,
            row_id=row_id,
            created_at=row_id,
            updated_at=row_id,
        )
    await harness.db.conn.execute(
        "UPDATE web_conversations SET updated_at=999999, pinned_at=999999 WHERE id=1"
    )
    await harness.db.conn.commit()

    rows = await harness._list_web_conversations(123)

    assert len(rows) == 100
    assert _listed_ids(rows) == list(range(105, 5, -1))
    assert 1 not in _listed_ids(rows)


async def test_schema_has_created_at_ordering_index(conversation_harness):
    cur = await conversation_harness.db.conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='web_conversations'"
    )
    indexes = {str(row["name"]): " ".join(str(row["sql"] or "").split()) for row in await cur.fetchall()}

    assert any(
        "(owner_chat_id, archived_at, created_at DESC, id DESC)" in sql
        for name, sql in indexes.items()
        if name != "idx_web_conversations_owner_time"
    )


async def test_legacy_database_gets_display_order_seeded_from_current_sidebar_order(tmp_path):
    path = tmp_path / "legacy-conversations.db"
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript(
            """
            CREATE TABLE web_conversations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conversation_uuid TEXT NOT NULL UNIQUE,
              owner_chat_id INTEGER NOT NULL,
              internal_chat_id INTEGER NOT NULL UNIQUE,
              title TEXT DEFAULT '',
              model TEXT DEFAULT '',
              status TEXT DEFAULT 'idle',
              current_status TEXT DEFAULT '',
              last_error TEXT DEFAULT '',
              created_at INTEGER,
              updated_at INTEGER,
              pinned_at INTEGER DEFAULT 0,
              archived_at INTEGER DEFAULT 0
            );
            INSERT INTO web_conversations (
              conversation_uuid, owner_chat_id, internal_chat_id, created_at, updated_at, pinned_at
            ) VALUES
              ('normal-old', 123, -1, 10, 10, 0),
              ('normal-new', 123, -2, 20, 20, 0),
              ('pinned-old', 123, -3, 5, 5, 1),
              ('pinned-new', 123, -4, 15, 15, 1);
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    db = DB(str(path))
    await db.connect()
    try:
        cur = await db.conn.execute(
            "SELECT conversation_uuid, display_order FROM web_conversations ORDER BY conversation_uuid"
        )
        ranks = {str(row["conversation_uuid"]): float(row["display_order"]) for row in await cur.fetchall()}
    finally:
        await db.close()

    assert ranks["pinned-new"] < ranks["pinned-old"]
    assert ranks["normal-new"] < ranks["normal-old"]
