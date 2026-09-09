# ruff: noqa: F811
# Imported fixtures are registered by pytest and intentionally reused as parameter names.
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.agent.native_continuation import serialize_messages
from app.memory.builtin import BuiltinMemoryClient
from app.references import (
    BUNDLE_FIELD,
    ReferenceError,
    ReferenceMaterials,
    _history_material,
    parse_references,
    reference_display_text,
    reference_occurrences,
)
from tests.test_builtin_template_import import _login_cookie, web_env  # noqa: F401
from tests.test_tools_history import _make_db

SYNTAX_CASES = json.loads((Path(__file__).parent / "fixtures/reference_syntax.json").read_text())


@pytest.mark.parametrize("case", SYNTAX_CASES, ids=lambda case: case["name"])
def test_shared_reference_syntax(case):
    assert [ref["id"] for ref in reference_occurrences(case["text"])] == case["ids"]
    assert [ref["id"] for ref in parse_references(case["text"])] == list(dict.fromkeys(case["ids"]))
    if not case["ids"]:
        assert reference_display_text(case["text"]) == case["text"]


async def test_literal_references_never_read_credentials_or_save_bundles(web_env, monkeypatch):
    e = web_env
    cookies = await _login_cookie(e)
    row = await e.server._create_web_conversation(123, title="literal references", model="openai/gpt")
    # Any resource access is a bug; no actual credential is needed to verify the
    # boundary. Exercise both the preview HTTP handler and the real send helper.
    read = AsyncMock(side_effect=AssertionError("literal example attempted resource access"))
    monkeypatch.setattr(BuiltinMemoryClient, "tool_call", read)
    for case in SYNTAX_CASES:
        if case["ids"]:
            continue
        response = await e.client.post("/api/references/preview", cookies=cookies, json={"text": case["text"]})
        assert response.status == 200, case["name"]
        assert (await response.json())["items"] == [], case["name"]
        assert await e.server._prepare_reference_bundle(row, case["text"], "msg:literal") == ("", [])
    read.assert_not_called()
    cursor = await e.db.conn.execute("SELECT COUNT(*) FROM web_reference_bundles")
    assert (await cursor.fetchone())[0] == 0


def token(kind, identity, label="reference"):
    return f"[{label}](openbear://ref/{kind}/{identity})"


def test_reference_identity_is_typed_id_not_label_and_code_is_not_expanded():
    text = (
        token("doc", 7, "same name")
        + " "
        + token("doc", 8, "same name")
        + " "
        + token("doc", 7, "other label")
    )
    assert [r["id"] for r in parse_references(text)] == ["7", "8"]
    assert parse_references("`" + token("secret", 1) + "`") == []
    assert parse_references("```text\n" + token("secret", 1) + "\n```") == []
    assert parse_references("[x](https://example.invalid/doc/7)") == []
    assert parse_references("[x](openbear://ref/doc/not-an-id)") == []


async def test_selected_materials_resolve_without_search_and_do_not_expand_nested_refs(web_env):
    store = ReferenceMaterials(web_env.db)
    memory = BuiltinMemoryClient(web_env.db)
    result = await memory.tool_call(
        "doc",
        {
            "action": "set",
            "name": "explicit",
            "title": "Exact doc",
            "content": "body " + token("secret", 999),
        },
    )
    text = token("doc", result["item"]["id"])
    resolved = await store.resolve(text, owner=123)
    assert len(resolved["materials"]) == 1
    assert resolved["materials"][0]["content"] == "body " + token("secret", 999)
    assert resolved["manifest"][0]["sourceLabel"] == "Exact doc"
    with pytest.raises(ReferenceError, match="reference_budget_exceeded"):
        await store.resolve(text, owner=123, budget=1)


async def test_secret_snapshot_is_encrypted_and_overlay_never_mutates_checkpoint(web_env):
    e = web_env
    store = e.server._reference_store()
    memory = BuiltinMemoryClient(e.db)
    secret = await memory.tool_call(
        "secret",
        {
            "action": "set",
            "name": "test-secret",
            "kvJson": '[{"key":"token","value":"PRIVATE-REFERENCE-ONLY"}]',
        },
    )
    resolved = await store.resolve(token("secret", secret["item"]["id"]), owner=123)
    assert "PRIVATE-REFERENCE-ONLY" not in json.dumps(resolved["manifest"])
    bundle = await store.save(resolved, conversation_uuid="mine", op_id="msg:one")
    cur = await e.db.conn.execute("SELECT * FROM web_reference_bundles")
    assert "PRIVATE-REFERENCE-ONLY" not in str([dict(row) for row in await cur.fetchall()])
    assert e.db.path and store.cipher()
    durable = [{"role": "user", "content": "use selected credential", BUNDLE_FIELD: [bundle]}]
    before = copy.deepcopy(durable)
    expanded = await store.overlay(durable, conversation_uuid="mine")
    assert "PRIVATE-REFERENCE-ONLY" in expanded[0]["content"]
    assert durable == before
    assert "PRIVATE-REFERENCE-ONLY" not in json.dumps(serialize_messages(durable))
    assert (await store.overlay(durable, conversation_uuid="other")) == durable
    assert (await store.overlay(durable, conversation_uuid="mine")) == expanded


async def test_resolved_document_snapshot_does_not_change_on_source_edit(web_env):
    e = web_env
    memory = BuiltinMemoryClient(e.db)
    store = e.server._reference_store()
    doc = await memory.tool_call(
        "doc", {"action": "set", "name": "versioned", "content": "version one"}
    )
    text = token("doc", doc["item"]["id"])
    material = await store.resolve(text, owner=123)
    bundle = await store.save(material, conversation_uuid="mine", op_id="msg:one")
    await memory.tool_call(
        "doc", {"action": "set", "id": doc["item"]["id"], "content": "version two"}
    )
    message = [{"role": "user", "content": text, BUNDLE_FIELD: [bundle]}]
    old = await store.overlay(message, conversation_uuid="mine")
    assert "version one" in old[0]["content"] and "version two" not in old[0]["content"]
    assert (await store.resolve(text, owner=123))["materials"][0]["content"] == "version two"


def test_history_reference_uses_history_visibility_and_paginates_past_fifty_turns(tmp_path):
    path = tmp_path / "history.db"
    _make_db(path)
    with sqlite3.connect(path) as db:
        for index in range(3, 59):
            db.execute(
                "INSERT INTO web_operations(conversation_uuid,op_id,op_type,turn_uuid,display_seq,internal,payload_json,revision,created_at_ms,updated_at_ms) VALUES('conv-a',?,'user_message',?,?,0,?,1,1,1)",
                (
                    f"msg:{index}",
                    f"turn-{index}",
                    index * 100,
                    json.dumps({"text": f"VISIBLE-TURN-{index}"}, ensure_ascii=False),
                ),
            )
    ref = parse_references(token("chat", "conv-a"))[0]
    output = _history_material(str(path), ref, "")
    assert "VISIBLE-TURN-58" in output and "第一个需求" in output
    assert output.count("VISIBLE-TURN-3\n") == 1
    assert "SECRET TOOL RESULT" not in output and "hidden reasoning" not in output
    assert "Truncated: true" not in output


def test_history_reference_rejects_incomplete_single_huge_turn(tmp_path):
    path = tmp_path / "history.db"
    _make_db(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE web_operations SET payload_json=? WHERE op_id='msg:u1'",
            (json.dumps({"text": "x" * 70000}),),
        )
    with pytest.raises(ReferenceError, match="history_output_limit"):
        _history_material(str(path), parse_references(token("chat", "conv-a"))[0], "")


async def test_reference_preview_is_authorized_metadata_only_and_invalid_reference_is_explicit(
    web_env,
):
    e = web_env
    assert (await e.client.post("/api/references/preview", json={"text": "x"})).status == 401
    cookies = await _login_cookie(e)
    missing = await e.client.post(
        "/api/references/preview", cookies=cookies, json={"text": token("doc", 99999)}
    )
    assert missing.status == 422
    assert (await missing.json())["error"] == "reference_unavailable"
    secret = await BuiltinMemoryClient(e.db).tool_call(
        "secret",
        {
            "action": "set",
            "name": "preview",
            "kvJson": '[{"key":"token","value":"NOT-IN-PREVIEW"}]',
        },
    )
    response = await e.client.post(
        "/api/references/preview",
        cookies=cookies,
        json={"text": token("secret", secret["item"]["id"])},
    )
    assert response.status == 200
    assert "NOT-IN-PREVIEW" not in await response.text()


async def test_running_steers_keep_each_occurrence_bound_to_its_frozen_material(web_env):
    from types import SimpleNamespace

    from app.agent.runs import RunRegistry
    from app.llm.events import StreamEvent
    from app.tools.base import ToolRegistry
    from tests.test_web_admin import FakeRunFactory, FakeStreamBackend

    e = web_env
    entered = asyncio.Event()
    release = asyncio.Event()

    class Backend(FakeStreamBackend):
        async def stream(self, messages, **kwargs):
            if self.calls == 0:
                entered.set()
                await release.wait()
            async for event in super().stream(messages, **kwargs):
                yield event

    backend = Backend(
        [
            [
                StreamEvent(kind="content", text="done"),
                StreamEvent(kind="finish", finish_reason="stop"),
            ]
        ]
    )
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    e.server.model_selection = SimpleNamespace(current="openai/gpt")
    e.server.runs = RunRegistry()
    e.server.tools = ToolRegistry()

    async def system(conversation_uuid=""):
        return "test system"

    e.server._build_system_prompt_for_chat = system
    row = await e.server._create_web_conversation(123, title="steering test", model="openai/gpt")
    live = e.server._live_for(row)
    await e.server._start_or_steer_web_conversation(row, "initial message", [], live)
    task = e.server.runs.task(int(row["internal_chat_id"]))
    await asyncio.wait_for(entered.wait(), 5)
    memory = BuiltinMemoryClient(e.db)
    doc = await memory.tool_call(
        "doc", {"action": "set", "name": "steer-doc", "content": "FROZEN-ONE"}
    )
    text = token("doc", doc["item"]["id"])
    first = await e.server._start_or_steer_web_conversation(row, text + " again " + text, [], live)
    await memory.tool_call(
        "doc", {"action": "set", "id": doc["item"]["id"], "content": "FROZEN-TWO"}
    )
    second = await e.server._start_or_steer_web_conversation(row, text, [], live)
    assert first["queued"] and second["queued"]
    release.set()
    await asyncio.wait_for(task, 10)
    assert backend.calls >= 2
    actual = json.dumps(backend.seen_convos[-1])
    assert "FROZEN-ONE" in actual and "FROZEN-TWO" in actual
    cursor = await e.db.conn.execute(
        "SELECT payload_json FROM web_operations WHERE conversation_uuid=? AND op_type='user_message'",
        (row["conversation_uuid"],),
    )
    payloads = [json.loads(r[0]) for r in await cursor.fetchall()]
    combined = next(p for p in payloads if p.get("interruption") and not p.get("queued"))
    bindings = combined["references"]
    assert len(bindings) == 3
    assert bindings[0]["bundleId"] == bindings[1]["bundleId"]
    assert bindings[2]["bundleId"] != bindings[0]["bundleId"]


async def test_duplicate_conversation_owns_independent_frozen_and_protected_bundles(web_env):
    from app.db.dao import MessageDAO

    e = web_env
    memory = BuiltinMemoryClient(e.db)
    row = await e.server._create_web_conversation(123, title="copy reference", model="openai/gpt")
    content = "KEEP-EXACT " + row["conversation_uuid"]
    doc = await memory.tool_call("doc", {"action": "set", "name": "copy-doc", "content": content})
    secret = await memory.tool_call(
        "secret",
        {
            "action": "set",
            "name": "copy-secret",
            "kvJson": '[{"key":"token","value":"COPY-SECRET"}]',
        },
    )
    text = token("doc", doc["item"]["id"]) + " " + token("secret", secret["item"]["id"])
    bundle, manifest = await e.server._prepare_reference_bundle(row, text, "msg:copy-user")
    await e.server._live_for(row).publish(
        {
            "type": "user",
            "turnUuid": "copy-turn",
            "messageUuid": "copy-user",
            "text": text,
            "referenceBundleId": bundle,
            "references": manifest,
        }
    )
    await e.server._persist_web_transcript_message(
        MessageDAO(e.db),
        int(row["internal_chat_id"]),
        "user",
        text,
        conversation_uuid=row["conversation_uuid"],
        turn_uuid="copy-turn",
        op_ids=["msg:copy-user"],
    )
    copied = await e.server._duplicate_web_conversation_data(row)
    cursor = await e.db.conn.execute(
        "SELECT * FROM web_reference_bundles WHERE conversation_uuid=?",
        (copied["conversation_uuid"],),
    )
    snapshot = dict(await cursor.fetchone())
    assert snapshot["bundle_uuid"] != bundle
    assert json.loads(snapshot["material_json"])[0]["content"] == content
    ops = await e.server._web_operations(copied["conversation_uuid"])
    user = next(op for op in ops if op["opType"] == "user_message")
    assert user["payload"]["referenceBundleId"] == snapshot["bundle_uuid"]
    await e.db.conn.execute(
        "DELETE FROM web_conversations WHERE conversation_uuid=?", (row["conversation_uuid"],)
    )
    await e.db.conn.commit()
    expanded = await e.server._reference_store().overlay(
        [{"role": "user", "content": text, BUNDLE_FIELD: [snapshot["bundle_uuid"]]}],
        conversation_uuid=copied["conversation_uuid"],
    )
    assert content in expanded[0]["content"] and "COPY-SECRET" in expanded[0]["content"]


async def test_chat_reference_cannot_cross_owner(web_env):
    e = web_env
    await e.db.conn.execute(
        "INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title) VALUES('other',456,-123,'private')"
    )
    await e.db.conn.commit()
    with pytest.raises(ReferenceError, match="reference_unavailable"):
        await e.server._reference_store().resolve(token("chat", "other"), owner=123)
