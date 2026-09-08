from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import pytest

from app.db.engine import DB
from app.operation_locks import ChatOperationLocks
from app.rath.agent_prompt import agent_system_prompt_params
from app.web_console.conversation_tree import WebAdminConversationTreeMixin
from app.web_console.conversations import WebAdminConversationsMixin
from app.web_console.core import _WEB_SESSION_KEY, WebSession


class _TreeHarness(WebAdminConversationTreeMixin, WebAdminConversationsMixin):
    def __init__(self, db: DB) -> None:
        self.db = db
        self.workspace_dir = "/shared/workspace"
        self._web_starting_turns: dict[str, Any] = {}
        self._web_live_streams: dict[str, Any] = {}
        self.runs = None
        self.rath = None
        self.operation_locks = ChatOperationLocks()
        self._conversation_tree_lock = asyncio.Lock()
        self.rendered: list[tuple[str, tuple[str, str] | None]] = []

    async def _json_body(self, request):
        return dict(getattr(request, "body", {}) or {})

    async def audit(self, *args, **kwargs):
        return None

    async def _build_system_prompt_for_chat(
        self,
        conversation_uuid: str = "",
        *,
        folder_values: tuple[str, str] | None = None,
        strict: bool = False,
    ) -> str:
        self.rendered.append((conversation_uuid, folder_values))
        return f"system:{conversation_uuid}:{folder_values!r}"


class _Request:
    def __init__(self, *, chat_id: int = 7, query=None, match_info=None, body=None) -> None:
        self._session = WebSession(chat_id=chat_id, expires_at=9999999999)
        self.query = query or {}
        self.match_info = match_info or {}
        self.body = body or {}
        self.remote = "127.0.0.1"

    def __getitem__(self, key):
        if key is _WEB_SESSION_KEY:
            return self._session
        raise KeyError(key)


@pytest.fixture
async def tree_harness(tmp_path):
    db = DB(str(tmp_path / "tree.db"))
    await db.connect()
    try:
        yield _TreeHarness(db)
    finally:
        await db.close()


async def _folder(harness, folder_id, parent, name, workspace="", prompt=""):
    await harness.db.conn.execute(
        """INSERT INTO web_conversation_folders
           (folder_uuid,owner_chat_id,parent_uuid,name,workspace_dir,prompt_markdown,display_order,created_at,updated_at)
           VALUES (?,?,?,?,?,?,1024,1,1)""",
        (folder_id, 7, parent, name, workspace, prompt),
    )


async def _conversation(harness, index, folder_id="", *, archived=False, created=1):
    uuid = f"conversation-{index}"
    chat_id = -index
    await harness.db.conn.execute(
        """INSERT INTO web_conversations
           (conversation_uuid,owner_chat_id,internal_chat_id,title,status,current_status,created_at,updated_at,display_order,archived_at,folder_uuid)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (uuid, 7, chat_id, f"Conversation {index}", "idle", "就绪", created, created, index * 1024, 100 if archived else 0, folder_id),
    )
    await harness.db.conn.execute(
        "INSERT INTO sessions(chat_id,created_at,updated_at,session_uuid,system_snapshot) VALUES (?,?,?,?,?)",
        (chat_id, 1, 1, uuid, f"old:{uuid}"),
    )
    return uuid, chat_id


async def test_arbitrary_depth_independent_nearest_inheritance_and_override_scope(tree_harness):
    h = tree_harness
    await _folder(h, "a", "", "A", "/project-a", "A prompt")
    await _folder(h, "b", "a", "B", "", "B prompt")
    await _folder(h, "c", "b", "C", "/project-c", "")
    await _folder(h, "d", "c", "D")
    await _conversation(h, 1, "d")
    await h.db.conn.commit()

    assert h._tree_folder_path("d", await h._tree_folders(7)) == ["a", "b", "c", "d"]
    assert await h._tree_effective_folder_values(7, "d") == ("/project-c", "B prompt")

    # A's workspace and prompt are both masked by nearer independent overrides.
    impact = await h._tree_change_impact(
        7, kind="properties", item_id="a", workspace_dir="/changed", prompt_markdown="changed"
    )
    assert impact["affectedCount"] == 0

    # Clearing C's local workspace reveals A's changed *proposed* workspace while
    # B's prompt continues to mask A independently.
    impact = await h._tree_change_impact(
        7, kind="properties", item_id="c", workspace_dir="", prompt_markdown=""
    )
    assert impact["affectedCount"] == 1
    assert impact["rows"][0]["old_values"] == ("/project-c", "B prompt")
    assert impact["rows"][0]["new_values"] == ("/project-a", "B prompt")


async def test_snapshot_update_uses_try_lock_recheck_and_never_defers_busy_target(tree_harness):
    h = tree_harness
    await _folder(h, "a", "", "A", "/old", "old prompt")
    first_uuid, first_chat = await _conversation(h, 1, "a")
    second_uuid, second_chat = await _conversation(h, 2, "a")
    await h.db.conn.executemany(
        """INSERT INTO controller_model_contexts
           (chat_id,conversation_uuid,state_json,revision,created_at,updated_at)
           VALUES (?,?, '{}',1,1,1)""",
        [(first_chat, first_uuid), (second_chat, second_uuid)],
    )
    await h.db.conn.execute(
        "INSERT INTO messages(chat_id,role,content,created_at) VALUES (?,?,?,?)",
        (first_chat, "user", "history remains", 10),
    )
    await h.db.conn.commit()

    impact = await h._tree_change_impact(
        7, kind="properties", item_id="a", workspace_dir="/new", prompt_markdown="new prompt"
    )

    async def mutate(conn):
        await conn.execute(
            "UPDATE web_conversation_folders SET workspace_dir='/new',prompt_markdown='new prompt' WHERE folder_uuid='a'"
        )

    # Holding the second session lock models a run that begins after preflight.
    async with h.operation_locks.chat(second_chat, "controller_run"):
        result = await h._tree_apply_snapshot_updates_locked(impact, update_snapshots=True, mutate=mutate)
    assert result == {"updatedCount": 1, "skippedRunningCount": 1}
    assert h.rendered == [(first_uuid, ("/new", "new prompt"))]

    cur = await h.db.conn.execute("SELECT chat_id,system_snapshot FROM sessions WHERE chat_id IN (?,?)", (first_chat, second_chat))
    snapshots = {int(row["chat_id"]): str(row["system_snapshot"]) for row in await cur.fetchall()}
    assert snapshots[first_chat] == f"system:{first_uuid}:{('/new', 'new prompt')!r}"
    assert snapshots[second_chat] == f"old:{second_uuid}"
    cur = await h.db.conn.execute("SELECT chat_id FROM controller_model_contexts ORDER BY chat_id")
    assert [int(row["chat_id"]) for row in await cur.fetchall()] == [second_chat]
    cur = await h.db.conn.execute("SELECT content FROM messages WHERE chat_id=?", (first_chat,))
    assert str((await cur.fetchone())["content"]) == "history remains"


async def test_property_and_move_handlers_commit_membership_without_snapshot_rewrite(tree_harness):
    h = tree_harness
    await _folder(h, "a", "", "A", "/old", "old")
    await _folder(h, "b", "", "B", "/new", "new")
    uuid, chat_id = await _conversation(h, 1, "a")
    await h.db.conn.commit()

    response = await h.handle_api_conversation_folder_properties_update(_Request(
        match_info={"folder_uuid": "a"},
        body={"workspaceDir": "/old-next", "promptMarkdown": "old-next", "updateSnapshots": False},
    ))
    result = json.loads(response.text)
    assert result["ok"] is True and result["updatedCount"] == 0
    cur = await h.db.conn.execute("SELECT workspace_dir,prompt_markdown FROM web_conversation_folders WHERE folder_uuid='a'")
    assert dict(await cur.fetchone()) == {"workspace_dir": "/old-next", "prompt_markdown": "old-next"}

    response = await h.handle_api_conversation_tree_move(_Request(body={
        "kind": "conversation", "id": uuid, "targetFolderId": "b", "updateSnapshots": False,
    }))
    result = json.loads(response.text)
    assert result["ok"] is True and result["affectedCount"] == 1
    cur = await h.db.conn.execute("SELECT folder_uuid FROM web_conversations WHERE conversation_uuid=?", (uuid,))
    assert str((await cur.fetchone())["folder_uuid"]) == "b"
    cur = await h.db.conn.execute("SELECT system_snapshot FROM sessions WHERE chat_id=?", (chat_id,))
    assert str((await cur.fetchone())["system_snapshot"]) == f"old:{uuid}"


async def test_running_leaf_and_every_ancestor_increment_then_clear_without_loading_branch(tree_harness):
    h = tree_harness
    await _folder(h, "a", "", "A")
    await _folder(h, "b", "a", "B")
    uuid, _chat = await _conversation(h, 1, "b")
    archived_uuid, _archived_chat = await _conversation(h, 2, "b", archived=True)
    await h.db.conn.commit()

    h._web_starting_turns[uuid] = {"turn-1"}
    h._web_starting_turns[archived_uuid] = {"turn-archived"}
    started = await h._tree_running_state(7)
    assert [item["conversationUuid"] for item in started["items"]] == [uuid]
    assert started["folderRunningCounts"] == {"a": 1, "b": 1}

    h._web_starting_turns.clear()
    finished = await h._tree_running_state(7)
    assert finished["items"] == []
    assert finished["folderRunningCounts"] == {}


async def test_children_are_paginated_all_reachable_and_archive_is_opt_in(tree_harness):
    h = tree_harness
    await _folder(h, "a", "", "A", "/secret/path", "LONG SECRET PROMPT")
    for index in range(1, 126):
        await _conversation(h, index, "a")
    await _conversation(h, 999, "a", archived=True)
    await h.db.conn.commit()

    response = await h.handle_api_conversation_tree_children(_Request(query={"parentId": "a", "limit": "50"}))
    first = json.loads(response.text)
    assert len(first["items"]) == 50 and first["hasMore"] is True and first["nextCursor"] == "50"
    response = await h.handle_api_conversation_tree_children(_Request(query={"parentId": "a", "limit": "50", "cursor": "50"}))
    second = json.loads(response.text)
    response = await h.handle_api_conversation_tree_children(_Request(query={"parentId": "a", "limit": "50", "cursor": "100"}))
    third = json.loads(response.text)
    assert len(second["items"]) == 50
    assert len(third["items"]) == 25 and third["hasMore"] is False
    assert "LONG SECRET PROMPT" not in response.text and "/secret/path" not in response.text

    # The archived row is not present in ordinary folder expansion/status; only a
    # manual archive-system request can fetch it.
    all_active = first["items"] + second["items"] + third["items"]
    assert "conversation-999" not in {row["conversationUuid"] for row in all_active}
    archived = json.loads((await h.handle_api_conversation_tree_children(_Request(query={"systemNode": "archive"}))).text)
    assert [row["conversationUuid"] for row in archived["items"]] == ["conversation-999"]
    status = json.loads((await h.handle_api_conversation_tree_status(_Request())).text)
    assert "conversation-999" not in {row["conversationUuid"] for row in status["items"]}


async def test_locate_merges_ancestor_after_first_fifty_siblings_without_paging(tree_harness):
    h = tree_harness
    await _folder(h, "root", "", "Root")
    # Equal sort keys fall back to newest DB row first, so the first inserted target
    # is the 55th child and is deliberately absent from the ordinary first page.
    await _folder(h, "target", "root", "Target after page one")
    for index in range(54):
        await _folder(h, f"sibling-{index:02d}", "root", f"Sibling {index:02d}")
    await _folder(h, "leaf", "target", "Leaf")
    selected_uuid, selected_chat = await _conversation(h, 700, "leaf", created=100)
    await h.db.conn.execute(
        "INSERT INTO messages(chat_id,role,content,created_at) VALUES (?,?,?,?)",
        (selected_chat, "user", "most recent real dialogue", 500),
    )
    await h.db.conn.commit()

    first = json.loads((await h.handle_api_conversation_tree_children(_Request(query={
        "parentId": "root", "limit": "50",
    }))).text)
    assert len(first["items"]) == 50
    assert "target" not in {item.get("folderId") for item in first["items"]}
    assert first["nextCursor"] == "50"

    targeted = json.loads((await h.handle_api_conversation_tree_children(_Request(query={
        "parentId": "root", "limit": "50", "includeFolderId": "target",
    }))).text)
    assert len(targeted["items"]) == 51
    assert targeted["includedFolderId"] == "target"
    assert targeted["includedFolderIds"] == ["target"]
    assert "target" in {item.get("folderId") for item in targeted["items"]}
    assert targeted["nextCursor"] == "50"

    tracked = json.loads((await h.handle_api_conversation_tree_children(_Request(query={
        "parentId": "root", "limit": "50", "includeFolderIds": "target,removed-folder",
    }))).text)
    assert tracked["includedFolderIds"] == ["target"]
    assert "target" in {item.get("folderId") for item in tracked["items"]}

    bootstrap = json.loads((await h.handle_api_conversation_tree_bootstrap(_Request())).text)
    assert bootstrap["selected"]["item"]["conversationUuid"] == selected_uuid
    assert bootstrap["selected"]["folderPath"] == ["root", "target", "leaf"]
    assert [item["folderId"] for item in bootstrap["selected"]["folderItems"]] == ["root", "target", "leaf"]
    assert [item["folderId"] for item in bootstrap["locatedFolders"]] == ["root", "target", "leaf"]

    folder_locate = json.loads((await h.handle_api_conversation_tree_folder_locate(_Request(
        match_info={"folder_uuid": "leaf"},
    ))).text)
    assert folder_locate["folderPath"] == ["root", "target", "leaf"]
    assert [item["folderId"] for item in folder_locate["folderItems"]] == ["root", "target", "leaf"]


async def test_bootstrap_expands_running_temporary_node_in_addition_to_project_selection(tree_harness):
    h = tree_harness
    await _folder(h, "project", "", "Project")
    selected_uuid, selected_chat = await _conversation(h, 1, "project", created=10)
    running_uuid, _running_chat = await _conversation(h, 2, "", created=20)
    await h.db.conn.execute(
        "INSERT INTO messages(chat_id,role,content,created_at) VALUES (?,?,?,?)",
        (selected_chat, "user", "newest real project dialogue", 100),
    )
    await h.db.conn.commit()
    h._web_starting_turns[running_uuid] = {"turn-running"}

    data = json.loads((await h.handle_api_conversation_tree_bootstrap(_Request())).text)
    assert data["selected"]["item"]["conversationUuid"] == selected_uuid
    assert [item["conversationUuid"] for item in data["running"]["items"]] == [running_uuid]
    assert data["initialExpandedPaths"] == [[], ["project"]]

    h._web_starting_turns.clear()
    without_running = json.loads((await h.handle_api_conversation_tree_bootstrap(_Request())).text)
    assert without_running["initialExpandedPaths"] == [["project"]]


async def test_default_selection_uses_visible_message_time_not_updated_at(tree_harness):
    h = tree_harness
    old_uuid, old_chat = await _conversation(h, 1, "", created=100)
    recent_uuid, recent_chat = await _conversation(h, 2, "", created=10)
    await h.db.conn.execute("UPDATE web_conversations SET updated_at=999999 WHERE conversation_uuid=?", (old_uuid,))
    await h.db.conn.executemany(
        "INSERT INTO messages(chat_id,role,content,created_at) VALUES (?,?,?,?)",
        [(old_chat, "user", "older real dialogue", 20), (recent_chat, "assistant", "newer real dialogue", 30)],
    )
    await h.db.conn.execute(
        "INSERT INTO messages(chat_id,role,content,created_at,task_uuid) VALUES (?,?,?,?,?)",
        (old_chat, "assistant", "newer Agent-only text", 999, "agent-task"),
    )
    await h.db.conn.commit()
    data = json.loads((await h.handle_api_conversation_tree_bootstrap(_Request())).text)
    assert data["selected"]["item"]["conversationUuid"] == recent_uuid
    assert data["initialExpandedPaths"] == [[]]


async def test_folder_cycle_rejected_and_agent_params_receive_no_folder_values(tree_harness):
    h = tree_harness
    await _folder(h, "a", "", "A")
    await _folder(h, "b", "a", "B")
    await h.db.conn.commit()
    with pytest.raises(Exception) as caught:
        await h._tree_validate_move_target(7, "folder", "a", "b")
    assert getattr(caught.value, "text", "") == "folder_cycle"

    params = agent_system_prompt_params(None, tool_allowlist=[], workspace_dir="/shared/workspace")
    assert "folderWorkspaceDir" not in params
    assert "folderPrompt" not in params


def test_legacy_database_adds_folder_membership_without_changing_rows(tmp_path):
    path = tmp_path / "legacy-tree.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE web_conversations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conversation_uuid TEXT NOT NULL UNIQUE,
              owner_chat_id INTEGER NOT NULL,
              internal_chat_id INTEGER NOT NULL UNIQUE,
              title TEXT DEFAULT '', model TEXT DEFAULT '', status TEXT DEFAULT 'idle',
              current_status TEXT DEFAULT '', last_error TEXT DEFAULT '', created_at INTEGER,
              updated_at INTEGER, pinned_at INTEGER DEFAULT 0, display_order REAL,
              archived_at INTEGER DEFAULT 0
            );
            INSERT INTO web_conversations
              (conversation_uuid,owner_chat_id,internal_chat_id,title,created_at,updated_at)
            VALUES ('legacy',7,-1,'Keep me',1,2);
            """
        )
        connection.commit()
    finally:
        connection.close()

    async def verify():
        db = DB(str(path))
        await db.connect()
        try:
            cur = await db.conn.execute("SELECT conversation_uuid,title,folder_uuid FROM web_conversations")
            row = await cur.fetchone()
            assert dict(row) == {"conversation_uuid": "legacy", "title": "Keep me", "folder_uuid": ""}
            cur = await db.conn.execute("SELECT 1 FROM web_conversation_folders")
            assert await cur.fetchone() is None
        finally:
            await db.close()

    import asyncio
    asyncio.run(verify())


async def test_move_noop_is_idempotent_for_self_neighbor_and_same_parent(tree_harness):
    h = tree_harness
    await _folder(h, "a", "", "A")
    uuid, _ = await _conversation(h, 1, "a", created=20)
    await h.db.conn.commit()
    for kind, item_id, table, id_col, parent in [
        ("folder", "a", "web_conversation_folders", "folder_uuid", ""),
        ("conversation", uuid, "web_conversations", "conversation_uuid", "a"),
    ]:
        before = dict(await (await h.db.conn.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (item_id,))).fetchone())
        for anchors in ({}, {"beforeId": item_id}, {"afterId": item_id}):
            response = await h.handle_api_conversation_tree_move(_Request(body={"kind": kind, "id": item_id, "targetFolderId": parent, **anchors}))
            assert response.status == 200
            after = dict(await (await h.db.conn.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (item_id,))).fetchone())
            assert after == before


async def test_folder_center_move_uses_original_creation_rank_and_preserves_pin_and_archive(tree_harness):
    h = tree_harness
    await _folder(h, "a", "", "A")
    await _folder(h, "b", "", "B")
    moving, _ = await _conversation(h, 1, "a", created=200)
    newer, _ = await _conversation(h, 2, "b", created=300)
    older, _ = await _conversation(h, 3, "b", created=100)
    pinned, _ = await _conversation(h, 4, "b", created=50)
    archived, _ = await _conversation(h, 5, "b", archived=True, created=400)
    await h.db.conn.execute("UPDATE web_conversations SET pinned_at=77 WHERE conversation_uuid=?", (pinned,))
    await h.db.conn.commit()
    response = await h.handle_api_conversation_tree_move(_Request(body={"kind":"conversation", "id":moving, "targetFolderId":"b"}))
    assert response.status == 200
    children = json.loads((await h.handle_api_conversation_tree_children(_Request(query={"parentId":"b"}))).text)["items"]
    assert [row["id"] for row in children] == [pinned, newer, moving, older]
    row = dict(await (await h.db.conn.execute("SELECT created_at,pinned_at,folder_uuid FROM web_conversations WHERE conversation_uuid=?", (moving,))).fetchone())
    assert row == {"created_at":200, "pinned_at":0, "folder_uuid":"b"}
    row = dict(await (await h.db.conn.execute("SELECT display_order,updated_at,archived_at FROM web_conversations WHERE conversation_uuid=?", (archived,))).fetchone())
    assert row == {"display_order":5120.0, "updated_at":400, "archived_at":100}
    # Explicit placement overrides creation rank, without changing the pin group.
    await h.handle_api_conversation_tree_move(_Request(body={"kind":"conversation", "id":moving, "targetFolderId":"b", "afterId":newer}))
    children = json.loads((await h.handle_api_conversation_tree_children(_Request(query={"parentId":"b"}))).text)["items"]
    assert [row["id"] for row in children] == [pinned, moving, newer, older]


async def test_pinned_and_folder_moves_share_default_and_explicit_position_rules(tree_harness):
    h = tree_harness
    for folder_id, parent, created, order in [("a","",500,1),("b","",400,2),("newer","b",300,1),("moving","a",200,2),("older","b",100,3)]:
        await _folder(h, folder_id, parent, folder_id)
        await h.db.conn.execute("UPDATE web_conversation_folders SET created_at=?,display_order=?,pinned_at=9 WHERE folder_uuid=?", (created,order,folder_id))
    await h.db.conn.commit()
    await h.handle_api_conversation_tree_move(_Request(body={"kind":"folder", "id":"moving", "targetFolderId":"b"}))
    rows = await h._tree_direct_folder_nodes(7,"b")
    assert [row["folderId"] for row in rows] == ["newer","moving","older"]
    assert rows[1]["pinned"] is True and rows[1]["createdAt"] == 200
    await h.handle_api_conversation_tree_move(_Request(body={"kind":"folder", "id":"moving", "targetFolderId":"b", "beforeId":"older"}))
    assert [row["folderId"] for row in await h._tree_direct_folder_nodes(7,"b")] == ["newer","older","moving"]


async def test_invalid_neighbors_and_cycles_do_not_partially_move_entities(tree_harness):
    from aiohttp import web
    h = tree_harness
    await _folder(h,"a","","A")
    await _folder(h,"b","a","B")
    first,_ = await _conversation(h,1,"a")
    pinned,_ = await _conversation(h,2,"b")
    await h.db.conn.execute("UPDATE web_conversations SET pinned_at=1 WHERE conversation_uuid=?",(pinned,))
    await h.db.conn.commit()
    for payload in [
        {"kind":"folder","id":"a","targetFolderId":"b"},
        {"kind":"conversation","id":first,"targetFolderId":"b","beforeId":pinned},
        {"kind":"conversation","id":first,"targetFolderId":"b","afterId":"missing"},
    ]:
        with pytest.raises(web.HTTPConflict):
            await h.handle_api_conversation_tree_move(_Request(body=payload))
    row = await (await h.db.conn.execute("SELECT folder_uuid FROM web_conversations WHERE conversation_uuid=?",(first,))).fetchone()
    assert row["folder_uuid"] == "a"


async def test_archive_successor_is_same_level_and_reaches_next_page(tree_harness):
    h = tree_harness
    await _folder(h,"a","","A")
    await _folder(h,"b","","B")
    for index in range(1,66):
        await _conversation(h,index,"a")
    await _conversation(h,100,"b")
    await _conversation(h,101,"a",archived=True)
    await h.db.conn.execute("UPDATE web_conversations SET archived_at=100 WHERE conversation_uuid='conversation-50'")
    await h.db.conn.commit()
    next_row = await h._tree_archive_successor(7,"conversation-50")
    assert next_row["conversationUuid"] == "conversation-51"
    assert next_row["folderId"] == "a"
    assert (await h._tree_archive_successor(7,"conversation-65"))["conversationUuid"] == "conversation-64"
    assert await h._tree_archive_successor(7,"conversation-100") is None
    # Empty source folder does not pick an archived item or another folder.
    await h.db.conn.execute("UPDATE web_conversations SET archived_at=100 WHERE folder_uuid='a'")
    assert await h._tree_archive_successor(7,"conversation-50") is None


async def test_move_and_archive_update_direct_counts_without_loading_archive(tree_harness):
    h = tree_harness
    await _folder(h,"a","","A")
    await _folder(h,"b","","B")
    uuid,_ = await _conversation(h,1,"a")
    await h.db.conn.commit()
    await h.handle_api_conversation_tree_move(_Request(body={"kind":"conversation","id":uuid,"targetFolderId":"b"}))
    counts = {row["folderId"]: row["conversationCount"] for row in await h._tree_direct_folder_nodes(7)}
    assert counts == {"a":0,"b":1}
    await h.db.conn.execute("UPDATE web_conversations SET archived_at=100 WHERE conversation_uuid=?",(uuid,))
    counts = {row["folderId"]: row["conversationCount"] for row in await h._tree_direct_folder_nodes(7)}
    assert counts == {"a":0,"b":0}
