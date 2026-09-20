# ruff: noqa: F811
"""Reference destination/renderer and frozen locator/real-tool contracts."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
from pathlib import Path

import pytest

from app.memory.builtin import BuiltinMemoryClient
from app.references import BUNDLE_FIELD, ReferenceError
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.history import register_history_tools
from app.tools.memory import register_memory_tools
from tests.test_builtin_template_import import _login_cookie, web_env  # noqa: F401
from tests.test_reference_mentions import sources, insert_op, forbid_body_reads  # noqa: F401


async def render_cases(cases):
    if not shutil.which("node"):
        pytest.skip("Node is required for the actual reference renderer contract")
    script = Path(__file__).resolve().parents[1] / "web/src/references/rendererHarness.mjs"
    process = await asyncio.create_subprocess_exec(
        "node", str(script), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(cases).encode()), 20)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, stderr.decode()
    return json.loads(stdout)


async def test_destination_modes_match_frozen_overlay_and_actual_renderer(web_env, sources, monkeypatch):
    e = web_env
    store = e.server._reference_store()
    cases = []
    for index, (query, mode) in enumerate([
        ("mode=mention", "mention"),
        ("scope=full&amp;mode=mention", "mention"),
        ("scope=full&#38;mode=mention", "mention"),
        ("scope=full&#x26;mode=mention", "mention"),
        (r"scope=full\&mode=mention", "mention"),
        ("mode=&#109;ention", "mention"),
        ("scope=full&amp;amp;mode=mention", "content"),  # Decode exactly once.
        ("scope=full&amp;mode=content", "content"),
    ]):
        text = f"[Credential](openbear://ref/secret/{sources['secret']}?{query})"
        if mode == "mention":
            async with forbid_body_reads(e, monkeypatch):
                resolved = await store.resolve(text, owner=123)
        else:
            resolved = await store.resolve(text, owner=123)
        assert resolved["manifest"][0].get("mode", "content") == mode
        bundle = await store.save(resolved, conversation_uuid=sources["chat"], op_id=f"send:{index}")
        durable = [{"role": "user", "content": text, BUNDLE_FIELD: [bundle]}]
        original = copy.deepcopy(durable)
        overlay = await store.overlay(durable, conversation_uuid=sources["chat"])
        assert durable == original
        assert ("SECRET-BODY" in overlay[0]["content"]) == (mode == "content")
        cursor = await e.db.conn.execute("SELECT * FROM web_reference_bundles WHERE bundle_uuid=?", (bundle,))
        row = dict(await cursor.fetchone())
        assert "SECRET-BODY" not in json.dumps(row)
        assert bool(row["protected_material"]) == (mode == "content")
        cases.append({"text": text, "references": resolved["manifest"], "expectedMode": mode})
    rendered = await render_cases(cases)
    for case, result in zip(cases, rendered, strict=True):
        mode = case["expectedMode"]
        assert result["keys"] == [case["references"][0]["key"]]
        assert result["parsed"][0].get("mode", "content") == mode
        for html in [result["html"], result["frozenHtml"]]:
            assert ("仅提及" in html) == (mode == "mention"), case
            assert ("发送时将向当前模型提供凭证" in html) == (mode == "content"), case
            assert "SECRET-BODY" not in html

    # Simulate a pre-fix frozen bundle: its source looked like mention, but its
    # authoritative content snapshot actually included the credential.
    old_text = cases[1]["text"]
    legacy = await store.resolve(f"[Credential](openbear://ref/secret/{sources['secret']})", owner=123)
    bundle = await store.save(legacy, conversation_uuid=sources["chat"], op_id="legacy")
    actual = await store.overlay([{"role": "user", "content": old_text, BUNDLE_FIELD: [bundle]}], conversation_uuid=sources["chat"])
    assert "SECRET-BODY" in actual[0]["content"]
    legacy_rendered = (await render_cases([{"text": old_text, "references": legacy["manifest"]}]))[0]
    assert "仅提及" not in legacy_rendered["frozenHtml"]
    assert "发送时将向当前模型提供凭证" in legacy_rendered["frozenHtml"]


async def test_encoded_invalid_modes_fail_closed_and_literals_stay_literal(web_env, sources, monkeypatch):
    e = web_env
    cookies = await _login_cookie(e)
    invalid = [
        f"[Credential](openbear://ref/secret/{sources['secret']}?{query})"
        for query in ["mode=mention&amp;mode=content", r"mode=mention\&mode=content", "mode=&#117;nknown"]
    ]
    token = f"[Credential](openbear://ref/secret/{sources['secret']}?scope=full&amp;mode=mention)"
    literals = [f"`{token}`", f"~~~\n{token}\n~~~", "    " + token, "\\" + token]
    async with forbid_body_reads(e, monkeypatch):
        for text in invalid:
            with pytest.raises(ReferenceError, match="invalid_reference_mode"):
                await e.server._prepare_reference_bundle(sources["row"], text, "invalid")
            for endpoint in ["preview", "inspect"]:
                response = await e.client.post(f"/api/references/{endpoint}", cookies=cookies, json={"text": text})
                assert response.status == 422
        for text in literals:
            assert await e.server._prepare_reference_bundle(sources["row"], text, "literal") == ("", [])
    cursor = await e.db.conn.execute("SELECT COUNT(*) FROM web_reference_bundles")
    assert (await cursor.fetchone())[0] == 0
    for result in await render_cases([{"text": text} for text in invalid + literals]):
        assert result["parsed"] == []
        assert 'class="reference-chip ' not in result["html"]


async def test_frozen_message_locator_is_consumed_by_registered_history_exactly(web_env, sources, monkeypatch):
    e = web_env
    conv = sources["chat"]
    await insert_op(e, conv, "same-turn-neighbor", "turn-first", 11, text="DO-NOT-INCLUDE-SAME-TURN", op_type="assistant_message")
    await insert_op(e, conv, "other-turn", "other-turn", 20, text="DO-NOT-INCLUDE-OTHER-TURN")
    text = f"[chosen message](openbear://ref/message/{sources['message']}?mode=mention)"
    async with forbid_body_reads(e, monkeypatch):
        bundle, bindings = await e.server._prepare_reference_bundle(sources["row"], text, "mention-send")
        locator = bindings[0]["locator"]
        assert locator["action"] == "read" and locator["opId"] == "msg:first"
        overlay = await e.server._reference_store().overlay([{"role": "user", "content": text, BUNDLE_FIELD: [bundle]}], conversation_uuid=conv)
        assert "HISTORY-BODY" not in overlay[0]["content"]
    # Read the frozen locator back, rather than assuming it equals fresh metadata.
    cursor = await e.db.conn.execute("SELECT manifest_json FROM web_reference_bundles WHERE bundle_uuid=?", (bundle,))
    locator = json.loads((await cursor.fetchone())[0])[0]["locator"]
    registry = ToolRegistry()
    register_history_tools(registry, e.db)
    schema = next(s["parameters"] for s in registry.schemas() if s["name"] == locator["tool"])
    arguments = {key: value for key, value in locator.items() if key != "tool"}
    assert set(arguments) <= schema["properties"].keys()
    assert all(key in arguments for key in schema["required"])
    assert schema["properties"]["opId"]["type"] == "string"
    context = ToolRuntimeContext(conversation_uuid=conv, turn_uuid="turn-first")
    actual = await registry.dispatch(locator["tool"], json.dumps(arguments), context=context)
    assert "HISTORY-BODY" in actual
    assert "DO-NOT-INCLUDE" not in actual
    assert not actual.startswith("error:")

    # Wrong/missing/invisible IDs and incompatible actions must never fall back
    # to a whole-conversation read. Empty legacy/non-string payloads stay hidden.
    for name, payload, op_type, internal in [
        ("hidden", {"text": "PRIVATE", "hidden": True}, "user_message", 0),
        ("internal", {"text": "PRIVATE", "internal": True}, "assistant_message", 0),
        ("column-internal", {"text": "PRIVATE"}, "user_message", 1),
        ("blank", {"text": " \n"}, "assistant_message", 0),
        ("tool", {"text": "PRIVATE"}, "tool", 0),
    ]:
        await insert_op(e, conv, name, name, 30, payload=payload, op_type=op_type, internal=internal)
    for op_id in ["missing", "hidden", "internal", "column-internal", "blank", "tool", "", None, 7]:
        denied = await registry.dispatch("History", json.dumps({**arguments, "opId": op_id, "includeNotices": True}), context=context)
        assert denied.startswith("error:"), op_id
        assert "PRIVATE" not in denied and "HISTORY-BODY" not in denied
    for wrong in [{"action": "read_turn"}, {"action": "list"}, {"source": "execution"}]:
        denied = await registry.dispatch("History", json.dumps({**arguments, **wrong}), context=context)
        assert denied.startswith("error:")
    other = await e.server._create_web_conversation(456, title="Other owner", model="openai/gpt")
    await insert_op(e, other["conversation_uuid"], "msg:first", "private-turn", 1, text="OTHER-OWNER-PRIVATE")
    denied = await registry.dispatch("History", json.dumps({**arguments, "conversationUuid": other["conversation_uuid"]}), context=context)
    assert "different owner" in denied and "OTHER-OWNER-PRIVATE" not in denied


@pytest.mark.parametrize("kind", ["mem", "doc", "secret", "chat", "turn"])
async def test_frozen_mention_locators_supply_callable_read_arguments(web_env, sources, monkeypatch, kind):
    e = web_env
    conv = sources["chat"]
    current = await e.server._create_web_conversation(123, title="Current discussion", model="openai/gpt")
    await insert_op(e, conv, "same-turn-answer", "turn-first", 11, text="TARGET-ANSWER", op_type="assistant_message")
    for index in range(7):
        await insert_op(e, conv, f"neighbor:{index}", f"neighbor-turn:{index}", 20 + index, text=f"NEIGHBOR-{index}")
    text = f"[selected](openbear://ref/{kind}/{sources[kind]}?mode=mention)"
    # Producing/freeze/inspect stays metadata-only, even for credentials. Reading
    # a body is a separate, explicit tool call made after leaving this guard.
    async with forbid_body_reads(e, monkeypatch):
        bundle, bindings = await e.server._prepare_reference_bundle(current, text, f"mention:{kind}")
        overlay = await e.server._reference_store().overlay(
            [{"role": "user", "content": text, BUNDLE_FIELD: [bundle]}],
            conversation_uuid=current["conversation_uuid"],
        )
        assert all(body not in overlay[0]["content"] for body in ["MEMORY-BODY", "DOCUMENT-BODY", "SECRET-BODY", "HISTORY-BODY", "TARGET-ANSWER", "NEIGHBOR-"])
    cursor = await e.db.conn.execute("SELECT manifest_json FROM web_reference_bundles WHERE bundle_uuid=?", (bundle,))
    locator = json.loads((await cursor.fetchone())[0])[0]["locator"]
    assert locator == bindings[0]["locator"]
    registry = ToolRegistry()
    register_memory_tools(registry, BuiltinMemoryClient(e.db))
    register_history_tools(registry, e.db)
    schema = next(tool["parameters"] for tool in registry.schemas() if tool["name"] == locator["tool"])
    arguments = {key: value for key, value in locator.items() if key != "tool"}
    assert set(arguments) <= schema["properties"].keys()
    assert all(key in arguments for key in schema["required"])
    expected_action = "get" if kind in {"mem", "doc", "secret"} else "read_turn" if kind == "turn" else "read"
    assert arguments["action"] == expected_action
    if kind == "turn":
        assert arguments["before"] == arguments["after"] == 0
    actual = await registry.dispatch(locator["tool"], json.dumps(arguments), context=ToolRuntimeContext(conversation_uuid=current["conversation_uuid"]))
    if kind in {"mem", "doc", "secret"}:
        result = json.loads(actual)
        assert result["ok"] and result["item"]["id"] == int(sources[kind])
        assert {"mem": "MEMORY-BODY", "doc": "DOCUMENT-BODY", "secret": "SECRET-BODY"}[kind] in actual
    elif kind == "turn":
        assert "HISTORY-BODY" in actual and "TARGET-ANSWER" in actual
        assert "NEIGHBOR-" not in actual
    else:
        assert not actual.startswith("error:") and "NEIGHBOR-6" in actual


@pytest.mark.parametrize("mode", ["content", "mention"])
async def test_api_directory_picker_whole_turn_freezes_exact_cross_conversation_material(web_env, monkeypatch, mode):
    if not shutil.which("node"):
        pytest.skip("Node is required for the actual Picker template contract")
    e = web_env
    source = await e.server._create_web_conversation(123, title="Yesterday", model="openai/gpt")
    current = await e.server._create_web_conversation(123, title="Today", model="openai/gpt")
    conv = source["conversation_uuid"]
    for index in range(18):
        await insert_op(e, conv, f"msg:q{index}", f"turn-{index}", index * 10 + 1, text=f"第{index:02d}轮的问题")
        await insert_op(e, conv, f"msg:a{index}", f"turn-{index}", index * 10 + 2, text=f"第{index:02d}轮的回答", op_type="assistant_message")
    cookies = await _login_cookie(e)
    endpoint = f"/api/reference-history/{conv}"
    first = await (await e.client.get(endpoint, cookies=cookies)).json()
    second = await (await e.client.get(endpoint + f"?offset={first['nextOffset']}", cookies=cookies)).json()
    assert len(first["items"]) == 50 and first["hasMore"]
    assert len(second["items"]) == 4 and not second["hasMore"]
    # Actual API page boundary splits turn-1 between its output and input.
    assert first["items"][-1]["itemId"] == "msg:a1"
    assert second["items"][0]["itemId"] == "msg:q1"
    request = {
        "source": {"kind": "chat", "id": conv, "key": f"chat:{conv}", "label": source["title"]},
        "currentConversation": current["conversation_uuid"], "pages": [first, second],
        "mode": mode, "splitTurnId": "turn-1", "filteredTurnId": "turn-0", "filterQuery": "第00*",
    }
    script = Path(__file__).resolve().parents[1] / "web/src/references/turnPickerHarness.mjs"
    process = await asyncio.create_subprocess_exec("node", str(script), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), 20)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, stderr.decode()
    selected = json.loads(stdout)
    for selection, index in [(selected["splitTurn"], 1), (selected["filteredTurn"], 0)]:
        text = selection["token"]
        assert selection["selected"]["kind"] == "turn" and selection["selected"]["id"] == conv
        if mode == "mention":
            async with forbid_body_reads(e, monkeypatch):
                bundle, bindings = await e.server._prepare_reference_bundle(current, text, f"chosen:{index}")
        else:
            bundle, bindings = await e.server._prepare_reference_bundle(current, text, f"chosen:{index}")
        assert len(bindings) == 1 and bindings[0]["itemId"] == f"turn-{index}"
        overlay = await e.server._reference_store().overlay(
            [{"role": "user", "content": text, BUNDLE_FIELD: [bundle]}], conversation_uuid=current["conversation_uuid"],
        )
        response = await e.client.post("/api/references/inspect", cookies=cookies, json={"text": text, "bundleId": bundle})
        inspected = await response.json()
        assert response.status == 200 and inspected["frozen"]
        if mode == "mention":
            assert inspected["content"] == "" and f"第{index:02d}轮的回答" not in overlay[0]["content"]
            locator = inspected["item"]["locator"]
            assert locator["action"] == "read_turn" and locator["before"] == locator["after"] == 0
            registry = ToolRegistry()
            register_history_tools(registry, e.db)
            material = await registry.dispatch("History", json.dumps({key: value for key, value in locator.items() if key != "tool"}), context=ToolRuntimeContext(conversation_uuid=current["conversation_uuid"]))
        else:
            material = inspected["content"]
            assert json.dumps(material, ensure_ascii=False) in overlay[0]["content"]
        assert f"第{index:02d}轮的问题" in material and f"第{index:02d}轮的回答" in material
        assert all(f"第{other:02d}轮的" not in material for other in range(18) if other != index)
