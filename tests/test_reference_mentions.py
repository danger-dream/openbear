# ruff: noqa: F811
# Imported fixtures are intentionally reused as parameter names.
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.fernet import Fernet

from app.agent.native_continuation import serialize_messages
from app.context.restart import controller_restart_selection
from app.context.window import WindowPolicy
from app.db.dao import MessageDAO
from app.memory.builtin import BuiltinMemoryClient
from app.references import (
    BUNDLE_FIELD,
    MENTION_NOTICE,
    ReferenceError,
    parse_references,
    reference_display_text,
    reference_from_url,
    reference_key,
    reference_occurrences,
)
from app.tools.history import _visible_history_items
from tests.test_builtin_template_import import _login_cookie, web_env  # noqa: F401


def link(kind, identity, *, mode="mention", label="selected", query=""):
    params = ([f"mode={mode}"] if mode is not None else []) + ([query] if query else [])
    return f"[{label}](openbear://ref/{kind}/{identity}{'?' + '&'.join(params) if params else ''})"


async def insert_op(e, conv, op_id, turn, seq, *, text="HISTORY-BODY", payload=None,
                    op_type="user_message", internal=0):
    await e.db.conn.execute(
        "INSERT INTO web_operations(conversation_uuid,op_id,op_type,turn_uuid,display_seq,internal,payload_json,revision,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,1,1,1)",
        (conv, op_id, op_type, turn, seq, internal, json.dumps(payload if payload is not None else {"text": text})),
    )
    await e.db.conn.commit()


@pytest.fixture
async def sources(web_env):
    e = web_env
    memory = BuiltinMemoryClient(e.db)
    mem = (await memory.tool_call("entry", {
        "action": "set", "title": "Mention memory", "ref": "mention-memory",
        "body": "MEMORY-BODY", "fieldsJson": '{"detail":"MEMORY-FIELDS"}',
    }))["item"]
    doc = (await memory.tool_call("doc", {
        "action": "set", "name": "mention-doc", "title": "Mention document", "content": "DOCUMENT-BODY",
    }))["item"]
    secret = (await memory.tool_call("secret", {
        "action": "set", "name": "mention-secret", "kvJson": '[{"key":"token","value":"SECRET-BODY"}]',
    }))["item"]
    row = await e.server._create_web_conversation(123, title="Mention conversation", model="openai/gpt")
    conv = row["conversation_uuid"]
    await insert_op(e, conv, "msg:first", "turn-first", 10)
    return {"mem": str(mem["id"]), "doc": str(doc["id"]), "secret": str(secret["id"]),
            "chat": conv, "turn": conv + "/turn-first", "message": conv + "/msg:first", "row": row}


@asynccontextmanager
async def forbid_body_reads(e, monkeypatch, *, frozen_inspect=False):
    """Fail on Memory get, History materialization, cipher access or body SQL reads."""
    with monkeypatch.context() as patch:
        memory = AsyncMock(side_effect=AssertionError("mention called Memory"))
        history = Mock(side_effect=AssertionError("mention materialized History"))
        cipher = Mock(side_effect=AssertionError("mention loaded encryption key"))
        patch.setattr(BuiltinMemoryClient, "tool_call", memory)
        patch.setattr("app.references.run_history_action", history)
        patch.setattr("app.references._history_material", history)
        patch.setattr("app.web_console.reference_api._visible_history_items", history)
        patch.setattr(e.server._reference_store(), "cipher", cipher)
        patch.setattr(Fernet, "decrypt", Mock(side_effect=AssertionError("mention decrypted")))
        forbidden = {("memory_entries", "body"), ("memory_entries", "fields_json"),
                     ("memory_docs", "content"), ("memory_secrets", "kv_json")}
        if frozen_inspect:
            forbidden |= {("web_reference_bundles", "material_json"),
                          ("web_reference_bundles", "protected_material")}

        def authorize(action, table, column, _db, _trigger):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and (table, column) in forbidden else sqlite3.SQLITE_OK

        connections = [e.db.conn._reader, e.db.conn._writer]
        for conn in connections:
            await conn.set_authorizer(authorize)
        execute = e.db.conn.execute

        async def checked_execute(sql, parameters=()):
            cursor = await execute(sql, parameters)
            if cursor.description and "web_operations" in sql:
                assert not {"payload", "payload_json", "text", "summary"} & {column[0] for column in cursor.description}
            return cursor

        patch.setattr(e.db.conn, "execute", checked_execute)
        try:
            yield
            memory.assert_not_called()
            history.assert_not_called()
            cipher.assert_not_called()
        finally:
            for conn in connections:
                await conn.set_authorizer(None)


@pytest.mark.parametrize("kind,identity", [
    ("mem", "17"), ("doc", "17"), ("secret", "17"), ("chat", "conv"),
    ("turn", "conv/turn-exact"), ("message", "conv/msg:exact"),
])
def test_mode_codec_preserves_legacy_keys_and_separates_deduplication(kind, identity):
    old = parse_references(link(kind, identity, mode=None))[0]
    explicit = parse_references(link(kind, identity, mode="content"))[0]
    mention = parse_references(link(kind, identity))[0]
    assert old == explicit
    assert "mode" not in old
    assert old["key"] == ":".join(str(old.get(k) or "") for k in ["kind", "id", "itemId", "scope", "turns"])
    assert mention == {**old, "mode": "mention", "key": old["key"] + ":mention"}
    assert reference_key(mention) == mention["key"]
    text = " ".join([link(kind, identity, mode=None), link(kind, identity), link(kind, identity), link(kind, identity, mode="content")])
    assert len(reference_occurrences(text)) == 4
    assert parse_references(text) == [old, mention]
    recent = parse_references(link(kind, identity, query="scope=recent&turns=3"))[0]
    assert recent["mode"] == "mention" and recent["turns"] == 3
    assert recent["key"].endswith(":recent:3:mention")
    assert reference_display_text(link(kind, identity)) == "selected"
    assert parse_references("`" + link(kind, identity) + "`") == []


@pytest.mark.parametrize("mode", ["unknown", "", "MENTION", "mention&mode=content", "content&mode=unknown"])
def test_unknown_or_ambiguous_mode_is_rejected_never_content(mode):
    text = link("secret", 17, mode=mode)
    assert reference_from_url(text.split("](", 1)[1][:-1]) is None
    with pytest.raises(ReferenceError, match="invalid_reference_mode"):
        parse_references(text)
    assert parse_references("`" + text + "`") == []


@pytest.mark.parametrize("kind", ["mem", "doc", "secret", "chat", "turn", "message"])
async def test_six_mentions_metadata_only_through_resolve_preview_inspect_save_overlay(web_env, sources, monkeypatch, kind):
    e = web_env
    cookies = await _login_cookie(e)
    store = e.server._reference_store()
    text = link(kind, sources[kind])
    key_file = Path(e.db.path).parent / "reference-materials.key"
    assert not key_file.exists()
    async with forbid_body_reads(e, monkeypatch):
        resolved = await store.resolve(text, owner=123)
        item = resolved["manifest"][0]
        assert item["mode"] == "mention" and item["estimatedTokens"] > 0
        assert "contentHash" not in item and "content" not in resolved["materials"][0]
        assert resolved["materials"][0]["notice"] == MENTION_NOTICE
        locator = item["locator"]
        if kind in {"mem", "doc", "secret"}:
            assert locator["id"] == int(sources[kind]) and locator["tool"] == "Memory"
        else:
            assert locator["conversationUuid"] == sources["chat"]
            if kind != "chat":
                assert locator["turnUuid" if kind == "turn" else "opId"] == sources[kind].split("/", 1)[1]
        for endpoint in ["preview", "inspect"]:
            response = await e.client.post(f"/api/references/{endpoint}", cookies=cookies, json={"text": text})
            assert response.status == 200
            data = await response.json()
            assert (data["items"][0] if endpoint == "preview" else data["item"]) == item
            if endpoint == "inspect":
                assert data["content"] == "" and not data["frozen"] and not data["previewTruncated"]
        bundle, bindings = await e.server._prepare_reference_bundle(sources["row"], text + " " + text, "msg:mention")
        assert len(bindings) == 2 and all(b["mode"] == "mention" for b in bindings)
        cursor = await e.db.conn.execute("SELECT material_json,protected_material FROM web_reference_bundles WHERE bundle_uuid=?", (bundle,))
        frozen = await cursor.fetchone()
        assert frozen["protected_material"] == "" and len(json.loads(frozen["material_json"])) == 1
        durable = [{"role": "user", "content": text, BUNDLE_FIELD: [bundle, bundle]}]
        original = copy.deepcopy(durable)
        overlay = await store.overlay(durable, conversation_uuid=sources["chat"])
        assert MENTION_NOTICE in overlay[0]["content"]
        assert overlay[0]["content"].count(MENTION_NOTICE) == 1
        assert durable == original
        assert "mode=mention" in json.dumps(serialize_messages(durable))
        all_output = json.dumps([resolved, overlay, dict(frozen)], ensure_ascii=False)
        assert not any(body in all_output for body in ["MEMORY-BODY", "MEMORY-FIELDS", "DOCUMENT-BODY", "SECRET-BODY", "HISTORY-BODY"])
    # Source edits exercise FTS triggers, so mutate fixtures outside the read guard.
    await e.db.conn.execute("UPDATE memory_docs SET content=? WHERE id=?", ("HUGE-BODY" * 100000, int(sources["doc"])))
    await e.db.conn.commit()
    async with forbid_body_reads(e, monkeypatch, frozen_inspect=True):
        assert (await store.resolve(text, owner=123))["estimatedTokens"] == resolved["estimatedTokens"]
        response = await e.client.post("/api/references/inspect", cookies=cookies, json={"text": text, "bundleId": bundle})
        data = await response.json()
        assert response.status == 200 and data["frozen"] and data["content"] == ""
        assert data["item"] == item
    assert not key_file.exists()


@pytest.mark.parametrize("kind,table", [("mem", "memory_entries"), ("doc", "memory_docs"), ("secret", "memory_secrets")])
async def test_memory_mentions_check_deleted_disabled_and_archived_as_content_does(web_env, sources, monkeypatch, kind, table):
    e = web_env
    text = link(kind, sources[kind])
    await e.db.conn.execute(f"UPDATE {table} SET enabled=0 WHERE id=?", (int(sources[kind]),))
    await e.db.conn.commit()
    async with forbid_body_reads(e, monkeypatch):
        with pytest.raises(ReferenceError, match="reference_unavailable"):
            await e.server._reference_store().resolve(text, owner=123)
    # Existing full references explicitly permit archived resources.
    await e.db.conn.execute(f"UPDATE {table} SET archived=1 WHERE id=?", (int(sources[kind]),))
    await e.db.conn.commit()
    async with forbid_body_reads(e, monkeypatch):
        assert (await e.server._reference_store().resolve(text, owner=123))["manifest"]
    await e.db.conn.execute(f"DELETE FROM {table} WHERE id=?", (int(sources[kind]),))
    await e.db.conn.commit()
    async with forbid_body_reads(e, monkeypatch):
        with pytest.raises(ReferenceError, match="reference_unavailable"):
            await e.server._reference_store().resolve(text, owner=123)


async def test_history_mentions_enforce_exact_owner_conversation_and_visibility(web_env, sources, monkeypatch):
    e = web_env
    conv = sources["chat"]
    cases = [
        ("hidden", {"text": "BODY", "hidden": True}, "user_message", 0, False),
        ("payload-internal", {"text": "BODY", "internal": True}, "assistant_message", 0, False),
        ("column-internal", {"text": "BODY"}, "user_message", 1, False),
        ("tool", {"text": "BODY"}, "tool", 0, False),
        ("empty", {"text": "\t\n\u3000"}, "user_message", 0, False),
        ("missing-text", {}, "assistant_message", 0, False),
        ("false-text", {"text": False}, "assistant_message", 0, False),
        ("zero-text", {"text": 0, "summary": []}, "assistant_message", 0, False),
        ("empty-object", {"text": {}, "summary": {}}, "assistant_message", 0, False),
        ("empty-array", {"text": [], "summary": "SUMMARY-BODY"}, "assistant_message", 0, True),
        ("text-priority", {"text": " ", "summary": "SUMMARY-BODY"}, "assistant_message", 0, False),
        ("string-zero", {"text": "0"}, "assistant_message", 0, True),
        ("empty-flags", {"text": "BODY", "hidden": {}, "internal": []}, "assistant_message", 0, True),
        ("summary", {"summary": "SUMMARY-BODY"}, "assistant_message", 0, True),
    ]
    for index, (name, payload, op_type, internal, _valid) in enumerate(cases):
        await insert_op(e, conv, name, name, 20 + index, payload=payload, op_type=op_type, internal=internal)
    await insert_op(e, conv, "legacy", "", 999)
    await e.db.conn.execute("INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title) VALUES('other-owner',456,-999,'private')")
    await e.db.conn.commit()
    await insert_op(e, "other-owner", "only-other", "only-other", 1)
    with sqlite3.connect(e.db.path) as conn:
        conn.row_factory = sqlite3.Row
        visible_ids = {item.op_id for item in _visible_history_items(conn, conv)}
    assert all((name in visible_ids) == valid for name, _payload, _op_type, _internal, valid in cases)
    async with forbid_body_reads(e, monkeypatch):
        store = e.server._reference_store()
        for kind in ["chat", "turn", "message"]:
            with pytest.raises(ReferenceError, match="reference_unavailable"):
                await store.resolve(link(kind, sources[kind]), owner=456)
            missing = "missing-conv" if kind == "chat" else conv + "/only-other"
            with pytest.raises(ReferenceError, match="reference_unavailable"):
                await store.resolve(link(kind, missing), owner=123)
        for name, _payload, _op_type, _internal, valid in cases:
            for kind in ["turn", "message"]:
                text = link(kind, conv + "/" + name)
                if valid:
                    assert (await store.resolve(text, owner=123))["manifest"]
                else:
                    with pytest.raises(ReferenceError, match="reference_unavailable"):
                        await store.resolve(text, owner=123)
        assert (await store.resolve(link("turn", conv + "/seq:999"), owner=123))["manifest"][0]["locator"]["turnUuid"] == "seq:999"
        await e.db.conn.execute("DELETE FROM web_operations WHERE conversation_uuid=?", (conv,))
        await e.db.conn.commit()
        for kind in ["turn", "message"]:
            with pytest.raises(ReferenceError, match="reference_unavailable"):
                await store.resolve(link(kind, sources[kind]), owner=123)
        await e.db.conn.execute("DELETE FROM web_conversations WHERE conversation_uuid=?", (conv,))
        await e.db.conn.commit()
        with pytest.raises(ReferenceError, match="reference_unavailable"):
            await store.resolve(link("chat", conv), owner=123)


async def test_invalid_mode_api_and_prepare_never_read_or_save(web_env, sources, monkeypatch):
    e = web_env
    cookies = await _login_cookie(e)
    text = link("secret", sources["secret"], mode="typo")
    async with forbid_body_reads(e, monkeypatch):
        for endpoint in ["preview", "inspect"]:
            response = await e.client.post(f"/api/references/{endpoint}", cookies=cookies, json={"text": text})
            assert response.status == 422 and (await response.json())["error"] == "invalid_reference_mode"
        with pytest.raises(ReferenceError, match="invalid_reference_mode"):
            await e.server._prepare_reference_bundle(sources["row"], text, "msg:invalid")
        cursor = await e.db.conn.execute("SELECT COUNT(*) FROM web_reference_bundles")
        assert (await cursor.fetchone())[0] == 0


async def test_mixed_mode_freezes_only_content_and_keeps_real_credentials_encrypted(web_env, sources, monkeypatch):
    e = web_env
    store = e.server._reference_store()
    cookies = await _login_cookie(e)
    nested = link("secret", 99999)
    await e.db.conn.execute("UPDATE memory_docs SET content=? WHERE id=?", ("FROZEN-DOC " + nested, int(sources["doc"])))
    await e.db.conn.commit()
    text = " ".join(link(kind, sources[kind], mode=mode) for kind, mode in [
        ("doc", None), ("doc", "mention"), ("secret", "mention"), ("secret", None), ("doc", "content"),
    ])
    bundle, bindings = await e.server._prepare_reference_bundle(sources["row"], text, "msg:mixed")
    assert len(bindings) == 5 and bindings[0]["key"] == bindings[-1]["key"]
    cursor = await e.db.conn.execute("SELECT * FROM web_reference_bundles WHERE bundle_uuid=?", (bundle,))
    frozen = dict(await cursor.fetchone())
    manifest = json.loads(frozen["manifest_json"])
    assert len(manifest) == 4 and len({m["key"] for m in manifest}) == 4
    assert [m.get("mode") for m in manifest] == [None, "mention", "mention", None]
    assert "SECRET-BODY" not in json.dumps(frozen)
    assert frozen["protected_material"] and (Path(e.db.path).parent / "reference-materials.key").exists()
    await e.db.conn.execute("UPDATE memory_docs SET content='CHANGED-DOC',title='Changed title'")
    await e.db.conn.execute("UPDATE memory_secrets SET kv_json='[]'")
    await e.db.conn.commit()
    overlay = await store.overlay([{"role": "user", "content": [{"type": "text", "text": text}], BUNDLE_FIELD: [bundle]}], conversation_uuid=sources["chat"])
    actual = json.dumps(overlay, ensure_ascii=False)
    assert "FROZEN-DOC" in actual and "CHANGED-DOC" not in actual and "Changed title" not in actual
    assert "SECRET-BODY" in actual and nested in actual and MENTION_NOTICE in actual
    # Inspect a mention in a mixed bundle must not even SELECT other frozen bodies.
    async with forbid_body_reads(e, monkeypatch, frozen_inspect=True):
        response = await e.client.post("/api/references/inspect", cookies=cookies,
            json={"text": link("doc", sources["doc"]), "bundleId": bundle})
        data = await response.json()
        assert response.status == 200 and data["content"] == "" and data["item"]["sourceLabel"] == "Mention document"
    response = await e.client.post("/api/references/inspect", cookies=cookies,
        json={"text": link("doc", sources["doc"], mode=None), "bundleId": bundle})
    assert (await response.json())["content"] == "FROZEN-DOC " + nested


async def test_multiple_selected_content_turns_inject_only_exact_selection(web_env, sources):
    e = web_env
    conv = sources["chat"]
    for index in range(2, 5):
        await insert_op(e, conv, f"msg:{index}", f"turn-{index}", index * 10, text=f"EXACT-QUESTION-{index}")
        await insert_op(e, conv, f"assistant:{index}", f"turn-{index}", index * 10 + 1,
                        text=f"EXACT-ANSWER-{index}", op_type="assistant_message")
    text = " ".join([link("turn", conv + "/turn-2", mode=None), link("turn", conv + "/turn-4", mode="content"), link("turn", conv + "/turn-2", mode=None)])
    store = e.server._reference_store()
    resolved = await store.resolve(text, owner=123, conversation_uuid=conv)
    assert len(resolved["materials"]) == 2
    bundle = await store.save(resolved, conversation_uuid=conv, op_id="msg:selected")
    actual = json.dumps(await store.overlay([{"role": "user", "content": text, BUNDLE_FIELD: [bundle]}], conversation_uuid=conv))
    for index in [2, 4]:
        assert f"EXACT-QUESTION-{index}" in actual and f"EXACT-ANSWER-{index}" in actual
    assert "EXACT-QUESTION-3" not in actual and "EXACT-ANSWER-3" not in actual and "HISTORY-BODY" not in actual


async def test_history_turns_view_filters_before_paging_and_default_stays_mixed(web_env, sources):
    e = web_env
    cookies = await _login_cookie(e)
    conv = sources["chat"]
    # A large newest turn must not consume the entire turns-only page.
    for index in range(1, 53):
        await insert_op(e, conv, f"q:{index}", f"t:{index}", index * 100, text=f"QUESTION-{index}")
    for index in range(70):
        await insert_op(e, conv, f"a:{index}", "t:52", 5201 + index, op_type="assistant_message")
    endpoint = f"/api/reference-history/{conv}"
    default = await e.client.get(endpoint, cookies=cookies)
    mixed = await default.json()
    assert mixed["items"][0]["kind"] == "turn" and len(mixed["items"]) == 50
    assert all(item["kind"] == "message" for item in mixed["items"][1:])
    first = await (await e.client.get(endpoint + "?view=turns", cookies=cookies)).json()
    second = await (await e.client.get(endpoint + "?view=turns&offset=50", cookies=cookies)).json()
    assert len(first["items"]) == 50 and first["hasMore"] and first["nextOffset"] == 50
    assert len(second["items"]) == 3 and not second["hasMore"]
    items = first["items"] + second["items"]
    assert all(item["kind"] == "turn" and "content" not in item for item in items)
    assert [item["itemId"] for item in items] == [f"t:{index}" for index in range(52, 0, -1)] + ["turn-first"]
    denied = await e.client.get("/api/reference-history/missing?view=turns", cookies=cookies)
    assert denied.status == 404


async def test_duplicate_and_restart_preserve_mention_mode_and_frozen_locators(web_env, sources, monkeypatch):
    e = web_env
    row = sources["row"]
    conv = sources["chat"]
    dao = MessageDAO(e.db)
    # Include a same-conversation turn to verify copied locators are rebound,
    # while Memory identities and all mention modes remain unchanged.
    text = " ".join(link(kind, sources[kind]) for kind in ["doc", "secret", "turn"])
    store = e.server._reference_store()
    bundle, manifest = await e.server._prepare_reference_bundle(row, text, "msg:copy-user")
    await e.server._live_for(row).publish({
        "type": "user", "turnUuid": "copy-turn", "messageUuid": "copy-user", "text": text,
        "referenceBundleId": bundle, "references": manifest,
    })
    await e.server._persist_web_transcript_message(dao, int(row["internal_chat_id"]), "user", text,
        conversation_uuid=conv, turn_uuid="copy-turn", op_ids=["msg:copy-user"])
    selection = await controller_restart_selection(dao, int(row["internal_chat_id"]), 999999,
        policy=WindowPolicy(context_window=128000), reference_store=store, conversation_uuid=conv)
    user = next(message for message in selection if message.get("role") == "user")
    assert user["content"] == text and user[BUNDLE_FIELD] == [bundle]
    assert MENTION_NOTICE not in user["content"]
    copied = await e.server._duplicate_web_conversation_data(row)
    cursor = await e.db.conn.execute("SELECT * FROM web_reference_bundles WHERE conversation_uuid=?", (copied["conversation_uuid"],))
    frozen = dict(await cursor.fetchone())
    assert frozen["bundle_uuid"] != bundle and frozen["protected_material"] == ""
    copied_manifest = json.loads(frozen["manifest_json"])
    assert all(item["mode"] == "mention" and item["key"].endswith(":mention") for item in copied_manifest)
    turn = next(item for item in copied_manifest if item["kind"] == "turn")
    assert turn["id"] == copied["conversation_uuid"] == turn["locator"]["conversationUuid"]
    assert turn["itemId"] == turn["locator"]["turnUuid"]
    ops = await e.server._web_operations(copied["conversation_uuid"])
    copied_user = next(op for op in ops if op["payload"].get("referenceBundleId"))
    assert all(item["mode"] == "mention" for item in copied_user["payload"]["references"])
    assert len(parse_references(copied_user["payload"]["text"])) == 3
    await e.db.conn.execute("DELETE FROM memory_docs")
    await e.db.conn.execute("DELETE FROM memory_secrets")
    await e.db.conn.execute("DELETE FROM web_conversations WHERE conversation_uuid=?", (conv,))
    await e.db.conn.commit()
    async with forbid_body_reads(e, monkeypatch):
        actual = await store.overlay([{ "role": "user", "content": copied_user["payload"]["text"], BUNDLE_FIELD: [frozen["bundle_uuid"]]}], conversation_uuid=copied["conversation_uuid"])
        assert MENTION_NOTICE in actual[0]["content"]
        assert "Mention document" in actual[0]["content"] and "SECRET-BODY" not in actual[0]["content"]
    assert not (Path(e.db.path).parent / "reference-materials.key").exists()
