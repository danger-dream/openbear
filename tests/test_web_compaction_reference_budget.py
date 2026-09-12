"""Manual summary must measure the same frozen input as automatic requests."""
from __future__ import annotations

import copy
import json

import pytest

from app.context.prompts import effective_context_prompt
from app.context.request_view import expanded_request_view
from app.context.runtime import ContextManager
from app.context.window import WindowPolicy, estimate_request, mark_source, source_of
from app.db.dao import SummaryDAO
from app.memory.builtin import BuiltinMemoryClient
from app.references import BUNDLE_FIELD
from tests.test_reference_materials import token
from tests.test_web_admin import web_env as shared_web_env
from tests.test_web_context_strategies import setup_manual

web_env = shared_web_env


@pytest.mark.parametrize("kind", ["doc", "secret"])
@pytest.mark.parametrize("oversized", [False, True])
async def test_manual_reference_budget_and_checkpoint_privacy(web_env, kind, oversized):
    env = web_env
    row, backend, store = await setup_manual(env)
    uuid = row["conversation_uuid"]
    refs = env.server._reference_store()
    marker = "ISOLATED-REFERENCE-BODY-NEVER-IN-CHECKPOINT"
    content = marker + " documentation" * (4000 if oversized else 120)
    fields = {"action": "set", "name": "budget-reference"}
    if kind == "doc":
        fields["content"] = content
    else:
        fields["kvJson"] = json.dumps([{"key": "token", "value": content}])
    item = await BuiltinMemoryClient(env.db).tool_call(kind, fields)
    text = token(kind, item["item"]["id"])
    resolved = await refs.resolve(text, owner=123)
    bundle = await refs.save(resolved, conversation_uuid=uuid, op_id="msg:reference-budget")
    messages = await store.restore_messages()
    messages[0][BUNDLE_FIELD] = [bundle]
    messages[0]["content"] += "\n" + text
    # New fixture source; previously archived sources are immutable.
    mark_source(messages[0], kind="human", source_id="human-with-frozen-reference")
    manager = ContextManager(store, WindowPolicy(128000, trigger_tokens=8000), backend=backend, model="gpt")
    await manager.checkpoint(messages)
    before = copy.deepcopy(await store.restore_messages())
    window_before = await store.load()
    response = await env.client.post(f"/api/conversations/{uuid}/compact")
    data = await response.json()
    retained = await store.restore_messages()
    expanded = await refs.overlay(retained, conversation_uuid=uuid)
    measured = estimate_request(expanded_request_view(expanded)(retained),
        system=effective_context_prompt("Original system", "model_summary"), tools=[],
        backend=backend, model="gpt", max_tokens=8192)
    events = [op for op in await env.server._web_operations(uuid) if op["opType"] == "context_compaction"]
    assert len(events) == 1
    if oversized:
        assert response.status == 409, data
        assert "required_context_too_large" in data["message"]
        assert measured.tokens > 8000
        assert retained == before
        assert (await store.load())["window_version"] == window_before["window_version"]
        assert await SummaryDAO(env.db).latest(row["internal_chat_id"]) is None
        assert events[0]["status"] == "failed"
    else:
        assert response.status == 200, data
        assert data["outcome"]["afterEstimateTokens"] == measured.tokens
        assert measured.tokens < 8000
        assert events[0]["status"] == "completed"
        assert await SummaryDAO(env.db).latest(row["internal_chat_id"])
    # References remain whole in the executing request, never summarized or
    # copied in plaintext to ordinary source/window/summary persistence.
    assert marker in str(expanded)
    assert marker not in str(retained)
    assert all(marker not in str(call) for call in backend.calls)
    for table, column in [("context_windows", "state_json"), ("context_execution_events", "payload_json"),
                          ("summaries", "summary")]:
        rows = await (await env.db.conn.execute(f"SELECT {column} FROM {table}")).fetchall()
        assert marker not in str([tuple(row) for row in rows])
    assert await refs.overlay(retained, conversation_uuid=uuid) == expanded
    assert env.server.config.models.providers["openai"].models[0].rollover_trigger_tokens == 8000


def test_shared_request_view_handles_regenerated_state_without_overlay_leak_or_mutation():
    raw = mark_source({"role": "user", "content": [{"type": "text", "text": "use reference"}]}, kind="human")
    expanded = [{**raw, "content": [*raw["content"], {"type": "text", "text": "request-only reference"}]}]
    tail = [{"role": "assistant", "content": "partial retry output"}]
    view = expanded_request_view(expanded, retry_tail=tail)
    summary = mark_source({"role": "user", "content": "new summary"}, kind="summary")
    runtime = mark_source({"role": "user", "content": "fresh state"}, kind="runtime")
    unbound = {"role": "user", "content": "unbound generated content"}
    selected = [raw, summary, runtime, unbound]
    before = copy.deepcopy(selected)
    measured = view(selected)
    assert "request-only reference" in str(measured[0])
    assert [m["content"] for m in measured if source_of(m).get("id") == source_of(summary)["id"]] == ["new summary"]
    assert "fresh state" in str(measured) and "unbound generated content" in str(measured)
    assert measured[-1] == tail[0]
    assert selected == before
    assert view(selected) == measured
    assert "request-only reference" not in str(view([summary, runtime]))
