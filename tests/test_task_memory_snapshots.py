"""Effective note snapshots against temporary databases; never live models."""
from __future__ import annotations

import html
import xml.etree.ElementTree as ET

import pytest

import app.task_memory as tm
from app.task_memory import SCOPE_AGENT_TASK, SCOPE_CONVERSATION, TaskMemoryDAO
from app.utils import estimate_tokens
from tests.test_task_memory import task_memory_db as shared_task_memory_db
from tests.test_task_memory_web_api import task_memory_api as shared_task_memory_api

task_memory_db = shared_task_memory_db
task_memory_api = shared_task_memory_api


@pytest.mark.parametrize("body", ["", " ", "界" * 500, "😀" * 500, "界" * 501, "😀" * 501,
                                  "  macOS & <body>\n\"原文\" ' 😀  ", "LONG-REFERENCE-" * 100])
async def test_short_body_unicode_boundary_is_whole_and_escaped(task_memory_db, body):
    dao = TaskMemoryDAO(task_memory_db)
    item, _ = await dao.create(conversation_uuid="c", scope_type=SCOPE_CONVERSATION,
                               name="note", description="reference locator", body=body)
    snapshot = await tm.task_memory_catalog_snapshot(dao, conversation_uuid="c")
    root = ET.fromstring(snapshot.catalog_xml)
    ET.fromstring(tm.task_memory_runtime_message(snapshot, epoch=0)["content"])
    bodies = root.findall(".//body")
    assert snapshot.short_body_max_chars == 500
    assert snapshot.item_count == snapshot.eligible_count == 1
    assert snapshot.omitted_count == 0
    assert item["memoryUuid"] in snapshot.catalog_xml
    assert "reference locator" in snapshot.catalog_xml
    if 0 < len(body) <= 500:
        assert [node.text for node in bodies] == [body]
        assert html.escape(body, quote=True) in snapshot.catalog_xml
    else:
        assert bodies == []
        if body:
            assert body[:20] not in snapshot.catalog_xml
    # List/search remain body-free even though the snapshot is no longer so.
    for query in ("", body[:3]):
        listing = await dao.list(conversation_uuid="c", scope_type=SCOPE_CONVERSATION, query=query)
        assert listing["items"] and all("body" not in row for row in listing["items"])
    assert (await dao.get(item["memoryUuid"], conversation_uuid="c", scope_type=SCOPE_CONVERSATION))["body"] == body
    assert estimate_tokens(tm.task_memory_runtime_message(snapshot, epoch=0)["content"]) <= 1500


async def test_exact_duplicate_description_not_body_is_deduplicated(task_memory_db):
    dao = TaskMemoryDAO(task_memory_db)
    body = "  原文 & <tag>\n"
    item, _ = await dao.create(conversation_uuid="c", scope_type=SCOPE_CONVERSATION,
                               name="note", description=body.strip(), body=body.strip())
    snapshot = await tm.task_memory_catalog_snapshot(dao, conversation_uuid="c")
    assert snapshot.catalog_xml.count(html.escape(body.strip(), quote=True)) == 1
    original = await dao.get(item["memoryUuid"], conversation_uuid="c", scope_type=SCOPE_CONVERSATION)
    assert original["body"] == original["description"] == body.strip()


async def test_dao_and_global_render_order_ignore_revision_and_scope(task_memory_db):
    dao = TaskMemoryDAO(task_memory_db)
    # Updated DESC, then created DESC, then UUID ASC, with alternating scopes.
    specs = [("z", 9, 1, 1, SCOPE_CONVERSATION),
             ("a", 8, 2, 2, SCOPE_AGENT_TASK),
             ("b", 8, 2, 90, SCOPE_CONVERSATION),
             ("aa", 8, 1, 99, SCOPE_CONVERSATION),
             ("c", 8, 1, 99, SCOPE_AGENT_TASK),
             ("00", 8, 0, 99, SCOPE_CONVERSATION)]
    for name, updated, created, revision, scope in reversed(specs):
        item, _ = await dao.create(conversation_uuid="c", scope_type=scope,
                                   task_uuid="t" if scope == SCOPE_AGENT_TASK else "",
                                   name=name, visible_to_agents=True)
        await task_memory_db.conn.execute(
            "UPDATE conversation_task_memories SET memory_uuid=?, updated_at=?, created_at=?, revision=? WHERE memory_uuid=?",
            ("mem-" + name, updated, created, revision, item["memoryUuid"]),
        )
    await task_memory_db.conn.commit()
    rows = await dao.catalog_rows(conversation_uuid="c", scope_type=SCOPE_CONVERSATION)
    assert [row["memoryUuid"] for row in rows] == ["mem-z", "mem-b", "mem-aa", "mem-00"]
    snapshot = await tm.task_memory_catalog_snapshot(dao, conversation_uuid="c", task_uuid="t", for_agent=True)
    xml = snapshot.catalog_xml
    assert [xml.index("mem-" + name) for name, *_ in specs] == sorted(xml.index("mem-" + name) for name, *_ in specs)
    direct = tm.build_task_memory_catalog_xml([dict(row) for row in reversed(rows)], tag="conversation-memory")
    assert direct.index("mem-z") < direct.index("mem-b")


async def test_all_scopes_counted_before_twenty_item_budget_and_omission_digest(task_memory_db):
    dao = TaskMemoryDAO(task_memory_db)
    shared = []
    for index in range(50):
        item, _ = await dao.create(conversation_uuid="c", scope_type=SCOPE_CONVERSATION,
                                   name=f"s{index}", visible_to_agents=True)
        shared.append(item)
        await dao.create(conversation_uuid="c", scope_type=SCOPE_AGENT_TASK,
                         task_uuid="t", name=f"p{index}")
    # Many revisions on old private notes cannot crowd out a newer shared note.
    await task_memory_db.conn.execute(
        "UPDATE conversation_task_memories SET updated_at=1, created_at=1, revision=99 WHERE scope_type='agent_task'"
    )
    await task_memory_db.conn.execute(
        "UPDATE conversation_task_memories SET updated_at=2, created_at=2 WHERE scope_type='conversation'"
    )
    await task_memory_db.conn.commit()
    rows = await dao.catalog_rows(conversation_uuid="c", scope_type=SCOPE_CONVERSATION)
    assert len(rows) == 50
    assert all("body" not in row for row in rows)  # compatibility DAO projection
    snapshot = await tm.task_memory_catalog_snapshot(dao, conversation_uuid="c", task_uuid="t", for_agent=True)
    assert snapshot.item_count == 20
    assert snapshot.eligible_count == 100
    assert snapshot.omitted_count == 80
    assert 'omittedCount="80"' in snapshot.catalog_xml
    assert "agent-task-memory" not in snapshot.catalog_xml
    assert estimate_tokens(tm.task_memory_runtime_message(snapshot, epoch=0)["content"]) <= 1500
    omitted = next(item for item in shared if item["memoryUuid"] not in snapshot.catalog_xml)
    await dao.delete(omitted["memoryUuid"], conversation_uuid="c", scope_type=SCOPE_CONVERSATION, expected_revision=1)
    changed = await tm.task_memory_catalog_snapshot(dao, conversation_uuid="c", task_uuid="t", for_agent=True)
    assert changed.item_count == 20 and changed.omitted_count == 79
    assert changed.digest != snapshot.digest
    assert changed.catalog_xml.replace('omittedCount="79"', 'omittedCount="80"') == snapshot.catalog_xml
    shared_only = await tm.task_memory_catalog_snapshot(
        dao, conversation_uuid="c", task_uuid="t", for_agent=True, task_memory_available=False,
    )
    assert shared_only.eligible_count == 49  # private records are not even counted
    assert shared_only.item_count + shared_only.omitted_count == 49


async def test_runtime_budget_never_partially_injects_short_bodies(task_memory_db):
    dao = TaskMemoryDAO(task_memory_db)
    bodies = []
    for index in range(6):
        body = f"preference-{index}:" + "界" * 480
        bodies.append(body)
        await dao.create(conversation_uuid="c", scope_type=SCOPE_CONVERSATION, name=f"n{index}", body=body)
    snapshot = await tm.task_memory_catalog_snapshot(dao, conversation_uuid="c", epoch=12345)
    actual_bodies = [node.text for node in ET.fromstring(snapshot.catalog_xml).findall(".//body")]
    assert 0 < len(actual_bodies) == snapshot.item_count < 6
    assert all(body in bodies for body in actual_bodies)
    assert snapshot.omitted_count == 6 - len(actual_bodies)
    assert estimate_tokens(tm.task_memory_runtime_message(snapshot, epoch=12345)["content"]) <= 1500


async def test_all_omitted_is_not_empty_and_clear_snapshot_stays_append_only(task_memory_db, monkeypatch):
    # Exercise the all-omitted branch with a constrained test budget, not a larger
    # production limit. An ordinary legal single record fits the default budget.
    monkeypatch.setattr(tm, "TASK_MEMORY_RUNTIME_MAX_TOKENS", 500)
    dao = TaskMemoryDAO(task_memory_db)
    original = [{"role": "user", "content": "request"}]
    empty = await tm.task_memory_catalog_snapshot(dao, conversation_uuid="c")
    assert empty.eligible_count == empty.item_count == empty.omitted_count == 0
    assert await tm.reconcile_task_memory_runtime_state(original, dao, conversation_uuid="c") == original
    # XML expansion makes this record too large for the constrained budget.
    item, _ = await dao.create(conversation_uuid="c", scope_type=SCOPE_CONVERSATION,
                               name='"' * 80, description='"' * 200, body='"' * 500)
    snapshot = await tm.task_memory_catalog_snapshot(dao, conversation_uuid="c")
    assert snapshot.item_count == 0 and snapshot.omitted_count == snapshot.eligible_count == 1
    assert 'omittedCount="1"' in snapshot.catalog_xml
    assert "<body>" not in snapshot.catalog_xml
    assert snapshot.digest != empty.digest
    first = await tm.reconcile_task_memory_runtime_state(original, dao, conversation_uuid="c")
    assert len(first) == 2 and first[0] is original[0]
    assert first[-1]["_openbear_runtime"]["omittedCount"] == 1
    assert estimate_tokens(first[-1]["content"]) <= 1500
    assert await tm.reconcile_task_memory_runtime_state(first, dao, conversation_uuid="c") == first
    await dao.delete(item["memoryUuid"], conversation_uuid="c", scope_type=SCOPE_CONVERSATION, expected_revision=1)
    cleared = await tm.reconcile_task_memory_runtime_state(first, dao, conversation_uuid="c")
    assert cleared[:len(first)] == first
    assert cleared[-1]["_openbear_runtime"]["digest"] == empty.digest
    assert '<task-memory-catalog empty="true" />' in cleared[-1]["content"]
    assert cleared[-1]["_openbear_runtime"]["omittedCount"] == 0
    epoch = tm.reset_task_memory_runtime_epoch(cleared)
    assert cleared == original
    assert await tm.reconcile_task_memory_runtime_state(cleared, dao, conversation_uuid="c", epoch=epoch) == original


@pytest.mark.parametrize("body,name,description,expected", [
    ("界" * 500, "short", "locator", (1, 0)),
    ("LONG-REFERENCE-" * 100, "long", "locator", (1, 0)),
    ('"' * 500, '"' * 80, '"' * 200, (0, 1)),
], ids=["short", "long", "all-omitted"])
async def test_preview_counts_budget_body_and_empty_transition(task_memory_api, monkeypatch, body, name, description, expected):
    if expected == (0, 1):
        monkeypatch.setattr(tm, "TASK_MEMORY_RUNTIME_MAX_TOKENS", 500)
    client, harness = task_memory_api
    dao = TaskMemoryDAO(harness.db)
    item, _ = await dao.create(conversation_uuid="conv-1", scope_type=SCOPE_CONVERSATION,
                               name=name, description=description, body=body)
    response = await client.get("/api/conversations/conv-1/task-memories/preview")
    payload = await response.json()
    snapshot = await tm.task_memory_catalog_snapshot(dao, conversation_uuid="conv-1")
    assert payload["runtimeSnapshot"] == tm.task_memory_runtime_message(snapshot, epoch=0)["content"]
    assert (payload["includedCount"], payload["omittedCount"]) == expected
    assert payload["eligibleCount"] == 1
    assert payload["shortBodyMaxChars"] == 500
    assert payload["digest"] == snapshot.digest
    assert payload["estimatedRuntimeTokens"] == estimate_tokens(payload["runtimeSnapshot"]) <= 1500
    if name == "short":
        assert body in payload["catalogXml"]
    elif name == "long":
        assert "LONG-REFERENCE-" not in payload["runtimeSnapshot"]
    await dao.delete(item["memoryUuid"], conversation_uuid="conv-1", scope_type=SCOPE_CONVERSATION, expected_revision=1)
    response = await client.get("/api/conversations/conv-1/task-memories/preview")
    cleared = await response.json()
    assert cleared["includedCount"] == cleared["omittedCount"] == cleared["eligibleCount"] == 0
    assert cleared["digest"] != payload["digest"]
    assert cleared["catalogXml"] == ""
    assert '<task-memory-catalog empty="true" />' in cleared["runtimeSnapshot"]
