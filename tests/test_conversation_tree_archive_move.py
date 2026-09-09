from __future__ import annotations

import json

import pytest
from aiohttp import web

from tests.test_conversation_tree import _conversation, _folder, _Request
from tests.test_conversation_tree import tree_harness as tree_harness


async def setup_archived(h):
    await _folder(h, "a", "", "A", "/project", "same prompt")
    await _folder(h, "b", "", "B", "/project", "same prompt")
    uuid, chat = await _conversation(h, 1, "a", archived=True, created=200)
    await h.db.conn.execute("INSERT INTO controller_model_contexts(chat_id,conversation_uuid,state_json) VALUES (?,?,'{}')", (chat, uuid))
    await h.db.conn.execute("INSERT INTO messages(chat_id,role,content) VALUES (?,'user','history remains')", (chat,))
    await h.db.conn.commit()
    return uuid, chat


async def state(h, uuid, chat):
    row = await (await h.db.conn.execute("SELECT folder_uuid,archived_at,pinned_at,created_at FROM web_conversations WHERE conversation_uuid=?", (uuid,))).fetchone()
    snapshot = await (await h.db.conn.execute("SELECT system_snapshot FROM sessions WHERE chat_id=?", (chat,))).fetchone()
    cache = await (await h.db.conn.execute("SELECT COUNT(*) FROM controller_model_contexts WHERE chat_id=?", (chat,))).fetchone()
    return dict(row), snapshot[0], cache[0]


@pytest.mark.parametrize("unarchive", [False, True])
@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("target", ["a", "b"])
async def test_archived_move_options_are_independent_and_refresh_even_with_equal_folder_values(tree_harness, unarchive, update, target):
    h = tree_harness
    uuid, chat = await setup_archived(h)
    response = await h.handle_api_conversation_tree_move(_Request(body={"kind": "conversation", "id": uuid, "targetFolderId": target, "unarchive": unarchive, "updateSnapshots": update}))
    result = json.loads(response.text)
    assert response.status == 200 and result["unarchived"] == unarchive
    row, snapshot, cache = await state(h, uuid, chat)
    assert row == {"folder_uuid": target, "archived_at": 0 if unarchive else 100, "pinned_at": 0, "created_at": 200}
    assert snapshot == (f"system:{uuid}:{('/project', 'same prompt')!r}" if update else f"old:{uuid}")
    assert cache == (0 if update else 1)
    assert result["updatedCount"] == int(update)
    assert (await (await h.db.conn.execute("SELECT content FROM messages WHERE chat_id=?", (chat,))).fetchone())[0] == "history remains"


@pytest.mark.parametrize("same_folder", [False, True])
@pytest.mark.parametrize("pinned", [False, True])
async def test_restored_conversation_uses_active_peers_and_original_creation_rank(tree_harness, same_folder, pinned):
    h = tree_harness
    uuid, chat = await setup_archived(h)
    target = "a" if same_folder else "b"
    newer, _ = await _conversation(h, 2, target, created=300)
    older, _ = await _conversation(h, 3, target, created=100)
    await h.db.conn.execute("UPDATE web_conversations SET display_order=NULL,pinned_at=?", (7 if pinned else 0,))
    await h.db.conn.commit()
    await h.handle_api_conversation_tree_move(_Request(body={"kind": "conversation", "id": uuid, "targetFolderId": target, "unarchive": True}))
    rows = await (await h.db.conn.execute("SELECT conversation_uuid FROM web_conversations WHERE folder_uuid=? AND archived_at=0 ORDER BY display_order", (target,))).fetchall()
    assert [row[0] for row in rows] == [newer, uuid, older]
    row, snapshot, cache = await state(h, uuid, chat)
    assert row["pinned_at"] == (7 if pinned else 0) and row["created_at"] == 200
    assert snapshot == f"old:{uuid}" and cache == 1


@pytest.mark.parametrize("failure", ["render", "reorder"])
async def test_failed_restore_move_is_atomic_including_archive_and_snapshot(tree_harness, failure):
    h = tree_harness
    uuid, chat = await setup_archived(h)
    before = await state(h, uuid, chat)
    if failure == "render":
        async def fail(*args, **kwargs):
            raise RuntimeError("fixture render failure")
        h._build_system_prompt_for_chat = fail
    body = {"kind": "conversation", "id": uuid, "targetFolderId": "b", "unarchive": True, "updateSnapshots": True}
    if failure == "reorder":
        body["afterId"] = "missing-neighbor"
    with pytest.raises(RuntimeError if failure == "render" else web.HTTPConflict):
        await h.handle_api_conversation_tree_move(_Request(body=body))
    assert await state(h, uuid, chat) == before


async def test_archived_move_keeps_busy_snapshot_and_does_not_defer_its_update(tree_harness):
    h = tree_harness
    uuid, chat = await setup_archived(h)
    async with h.operation_locks.chat(chat, "controller_run"):
        response = await h.handle_api_conversation_tree_move(_Request(body={"kind": "conversation", "id": uuid, "targetFolderId": "b", "unarchive": True, "updateSnapshots": True}))
    result = json.loads(response.text)
    assert result["updatedCount"] == 0 and result["skippedRunningCount"] == 1
    row, snapshot, cache = await state(h, uuid, chat)
    assert row["folder_uuid"] == "b" and row["archived_at"] == 0
    assert snapshot == f"old:{uuid}" and cache == 1 and h.rendered == []


async def test_legacy_move_without_options_keeps_archive_and_snapshot(tree_harness):
    h = tree_harness
    uuid, chat = await setup_archived(h)
    await h.handle_api_conversation_tree_move(_Request(body={"kind": "conversation", "id": uuid, "targetFolderId": "b"}))
    row, snapshot, cache = await state(h, uuid, chat)
    assert row["archived_at"] == 100 and row["folder_uuid"] == "b"
    assert snapshot == f"old:{uuid}" and cache == 1
