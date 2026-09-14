"""Cancellation before first prepare must not hide references from manual budgets."""
from __future__ import annotations

import asyncio

import pytest

from app.context.builder import build_controller_history
from app.db.dao import MessageDAO
from app.references import BUNDLE_FIELD
from tests.test_conversation_restart import run_web
from tests.test_web_admin import web_env as shared_web_env
from tests.test_web_context_strategies import setup_manual

web_env = shared_web_env


@pytest.mark.parametrize("oversized", [False, True])
async def test_cancelled_input_rebinds_reference_before_manual_compaction(web_env, monkeypatch, oversized):
    env = web_env
    await env.server.global_realtime.close()
    row, backend, store = await setup_manual(env)
    refs = env.server._reference_store()
    frozen = "FROZEN_ORIGINAL_REFERENCE " * (15000 if oversized else 5)

    async def system():
        return "Isolated policy"

    async def cancelled_overlay(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(env.server, "_build_system_prompt_for_chat", system)
    overlay = refs.overlay
    monkeypatch.setattr(refs, "overlay", cancelled_overlay)
    # This actual runner boundary is after user/bundle persistence but before
    # the first archive/window selection. No model request is sent.
    assert not await run_web(env, row, "cancelled-input", "Read [doc](openbear://ref/doc/1)", ref=frozen)
    assert not backend.calls
    monkeypatch.setattr(refs, "overlay", overlay)
    dao = MessageDAO(env.db)
    chat, conv = row["internal_chat_id"], row["conversation_uuid"]
    before = await store.load()
    originals = await build_controller_history(dao, chat, reference_store=refs)
    assert frozen in str(await overlay(originals, conversation_uuid=conv))
    response = await env.client.post(f"/api/conversations/{conv}/compact")
    after = await store.load()
    if oversized:
        assert response.status == 409, await response.text()
        assert "required_context_too_large" in (await response.json())["message"]
        assert after["state"] == before["state"]
        assert after["state"]["sourceMessageHighWater"] == before["state"]["sourceMessageHighWater"]
        assert (after["window_version"], after["revision"]) == (before["window_version"], before["revision"])
    else:
        assert response.status == 200, await response.text()
        assert after["state"]["sourceMessageHighWater"] > before["state"]["sourceMessageHighWater"]
    retained = await build_controller_history(dao, chat, reference_store=refs)
    assert "openbear://ref/doc/1" in str(retained)
    assert any(m.get(BUNDLE_FIELD) for m in retained)
    assert frozen in str(await overlay(retained, conversation_uuid=conv))
    assert frozen not in str(after["state"])
    assert all("FROZEN_ORIGINAL_REFERENCE" not in str(call) for call in backend.calls)
