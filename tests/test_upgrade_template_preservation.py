"""Booting a release must not rewrite any existing prompt-template version."""
from __future__ import annotations

import pytest

from app.context.prompts import REPLACEMENTS, SUMMARY_SYSTEM_POLICY, effective_context_prompt
from app.context.window import WINDOW_SYSTEM_POLICY
from app.db.dao import MessageDAO
from app.memory.builtin import BuiltinMemoryClient
from tests.test_memory_builtin import db as shared_db

db = shared_db


@pytest.mark.parametrize("strategy", ["sliding_window", "model_summary"])
async def test_bootstrap_preserves_old_and_custom_versions_while_request_view_adapts(db, strategy):
    old_paragraphs = "\n\n".join(REPLACEMENTS)
    # Inactive old built-ins and active user variants containing framework text
    # must both remain byte-for-byte unchanged, including identifiers and flags.
    for name, prefix, main, agent in [
        ("Old built-in main", "", 0, 0),
        ("Old built-in Agent", "", 0, 0),
        ("Custom current main", "USER_MAIN_CONSTRAINT\n", 1, 0),
        ("Custom current Agent", "USER_AGENT_CONSTRAINT\n", 0, 1),
    ]:
        await db.conn.execute(
            "INSERT INTO memory_templates(name,content,is_active,is_agent_active,updated_at) VALUES(?,?,?,?,?)",
            (name, prefix + old_paragraphs, main, agent, 123),
        )
    await db.conn.commit()
    dao = MessageDAO(db)
    frozen = "FROZEN_USER_CONSTRAINT\n" + old_paragraphs
    await dao.get_or_set_system_snapshot(123, frozen)

    async def rows():
        cur = await db.conn.execute("SELECT * FROM memory_templates ORDER BY id")
        return [dict(row) for row in await cur.fetchall()]

    before = await rows()
    for _ in range(2):
        # A new client represents another startup, not merely the fast-path flag.
        await BuiltinMemoryClient(db)._bootstrap()
        assert await rows() == before
        assert await dao.get_system_snapshot(123) == frozen
    request = effective_context_prompt(frozen, strategy)
    expected = SUMMARY_SYSTEM_POLICY if strategy == "model_summary" else WINDOW_SYSTEM_POLICY
    assert request.startswith("FROZEN_USER_CONSTRAINT\n")
    assert expected.strip() in request
    assert all(old not in request for old in REPLACEMENTS)
    assert await rows() == before
    assert await dao.get_system_snapshot(123) == frozen
