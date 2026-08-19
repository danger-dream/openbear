from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from app.db.engine import DB
from app.memory.builtin import BuiltinMemoryClient


async def test_openbear_v31_is_active_baseline_derived_and_renders_from_database(tmp_path):
    source = Path("data/openbear.db")
    if not source.is_file():
        pytest.skip("requires local data/openbear.db snapshot")
    with sqlite3.connect(source) as conn:
        conn.row_factory = sqlite3.Row
        rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id,name,content,is_active,is_agent_active FROM memory_templates WHERE id IN (27,28,29)"
            )
        }
    if 29 not in rows or rows[29]["name"] != "OpenBear-v3.1" or int(rows[29]["is_active"] or 0) != 1:
        pytest.skip("local db is not the OpenBear-v3.1 active snapshot this test pins")
    assert (rows[27]["name"], rows[27]["is_active"], rows[27]["is_agent_active"]) == ("OpenBear-v3.0", 0, 0)
    assert (rows[28]["is_active"], rows[28]["is_agent_active"]) == (0, 1)
    assert (rows[29]["name"], rows[29]["is_active"], rows[29]["is_agent_active"]) == ("OpenBear-v3.1", 1, 0)
    assert rows[29]["content"].startswith(rows[27]["content"][:1000])

    required_meanings = [
        "actual need", "not to minimize the number of questions", "Investigate facts through safe read-only",
        'UserInteraction(action="questionnaire")', "mutually independent questions", "never a closed answer space",
        "text-only answers and options plus text", "Free text is as authoritative", "Cancellation or timeout supplies no user decision",
        "Clarification does not expand execution authorization", "Agents do not question the user directly",
    ]
    for phrase in required_meanings:
        assert phrase in rows[29]["content"]

    # Render the exact DB content against an isolated database copy; render logs
    # therefore cannot mutate the authoritative DB or its authorized active flags.
    copied = tmp_path / "preview.db"
    shutil.copy2(source, copied)
    db = DB(str(copied))
    await db.connect()
    try:
        rendered = await BuiltinMemoryClient(db).render_system_prompt(
            {
                "toolNames": ["UserInteraction", "Agent"],
                "availableAgents": [],
                "runtimeInfo": {"channel": "web", "outputFormat": "markdown"},
            },
            template_content=rows[29]["content"],
            template_name="OpenBear-v3.1 isolated preview id=29",
            source="test_isolated_preview",
        )
    finally:
        await db.close()
    assert "[[ERROR:" not in rendered
    assert "Questionnaire options are thinking scaffolds, never a closed answer space." in rendered
    assert "Agents do not question the user directly." in rendered
