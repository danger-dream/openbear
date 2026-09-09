from __future__ import annotations

import json

from app.web_console.conversation_tree import WebAdminConversationTreeMixin
from tests.test_conversation_tree import _conversation, _folder, _Request
from tests.test_conversation_tree import tree_harness as tree_harness


def payload(response):
    assert response.status == 200
    return json.loads(response.text)


async def counts(h):
    folders = await h._tree_folders(7)
    return (await h._tree_counts(7, folders))[1]


async def test_subtree_counts_cover_unloaded_descendants_and_all_tree_entrypoints(tree_harness):
    h = tree_harness
    for folder_id, parent in [("a", ""), ("b", "a"), ("c", "b"), ("d", "a"), ("x", ""), ("empty", "")]:
        await _folder(h, folder_id, parent, f"Project {folder_id}")
    for index, folder_id in enumerate(["a", "b", "c", "c", "d", "x", ""], 1):
        await _conversation(h, index, folder_id)
    await _conversation(h, 8, "c", archived=True)
    foreign, _ = await _conversation(h, 9, "c")
    await h.db.conn.execute("UPDATE web_conversations SET owner_chat_id=8 WHERE conversation_uuid=?", (foreign,))
    await h.db.conn.commit()
    expected = {"a": 5, "b": 3, "c": 2, "d": 1, "x": 1, "empty": 0, "": 1}
    assert await counts(h) == expected

    bootstrap = payload(await h.handle_api_conversation_tree_bootstrap(_Request()))
    assert {row["folderId"]: row["conversationCount"] for row in bootstrap["rootFolders"]} == {"a": 5, "x": 1, "empty": 0}
    assert (bootstrap["activeCount"], bootstrap["archivedCount"], bootstrap["temporaryCount"]) == (7, 1, 1)
    assert bootstrap["running"]["folderConversationCounts"] == expected
    assert bootstrap["running"]["items"] == []  # Idle conversations must still be counted.

    children = payload(await h.handle_api_conversation_tree_children(_Request(query={"parentId": "a"})))
    assert {row["folderId"]: row["conversationCount"] for row in children["items"] if row["kind"] == "folder"} == {"b": 3, "d": 1}
    located = payload(await h.handle_api_conversation_tree_folder_locate(_Request(match_info={"folder_uuid": "c"})))
    assert [(row["folderId"], row["conversationCount"]) for row in located["folderItems"]] == [("a", 5), ("b", 3), ("c", 2)]
    located_chat = payload(await h.handle_api_conversation_tree_locate(_Request(match_info={"conversation_uuid": "conversation-3"})))
    assert located_chat["folderItems"] == located["folderItems"]
    for archive in ["0", "1"]:
        search = payload(await h.handle_api_conversation_tree_search(_Request(query={"q": "Project", "archiveUnlocked": archive})))
        assert {row["folderId"]: row["conversationCount"] for row in search["items"]} == {key: value for key, value in expected.items() if key}
    status = payload(await h.handle_api_conversation_tree_status(_Request()))
    assert status["folderConversationCounts"] == expected


async def test_moves_archive_restore_and_delete_recount_both_ancestor_chains(tree_harness):
    h = tree_harness
    for folder_id, parent in [("a", ""), ("b", "a"), ("c", "b"), ("x", ""), ("y", "x")]:
        await _folder(h, folder_id, parent, folder_id)
    one, _ = await _conversation(h, 1, "c")
    two, _ = await _conversation(h, 2, "c")
    await h.db.conn.commit()

    async def check(expected):
        assert await counts(h) == {"": 0, **expected}
        assert (await h._tree_running_state(7))["folderConversationCounts"] == {"": 0, **expected}

    await check({"a": 2, "b": 2, "c": 2, "x": 0, "y": 0})
    await h.handle_api_conversation_tree_move(_Request(body={"kind": "conversation", "id": one, "targetFolderId": "y"}))
    await check({"a": 1, "b": 1, "c": 1, "x": 1, "y": 1})
    await h.handle_api_conversation_tree_move(_Request(body={"kind": "folder", "id": "b", "targetFolderId": "y"}))
    await check({"a": 0, "b": 1, "c": 1, "x": 2, "y": 2})
    await h.db.conn.execute("UPDATE web_conversations SET archived_at=100 WHERE conversation_uuid=?", (two,))
    await h.db.conn.commit()
    await check({"a": 0, "b": 0, "c": 0, "x": 1, "y": 1})
    await h.db.conn.execute("UPDATE web_conversations SET archived_at=0 WHERE conversation_uuid=?", (two,))
    await h.db.conn.commit()
    await check({"a": 0, "b": 1, "c": 1, "x": 2, "y": 2})
    await h.db.conn.execute("DELETE FROM web_conversations WHERE conversation_uuid=?", (two,))
    await h.db.conn.commit()
    await check({"a": 0, "b": 0, "c": 0, "x": 1, "y": 1})


def test_subtree_counts_have_no_depth_limit_or_recursion_and_do_not_mutate_direct_counts():
    folders = {str(i): {"parent_uuid": str(i - 1) if i else ""} for i in range(1200)}
    direct = {"1199": 2, "0": 1, "": 3}
    result = WebAdminConversationTreeMixin._tree_subtree_counts(direct, folders)
    assert result["0"] == 3 and result["1199"] == 2 and result["600"] == 2
    assert result[""] == 3
    assert direct == {"1199": 2, "0": 1, "": 3}
