"""Rendered prompt contracts for notes/history/task-state separation."""
from pathlib import Path

import pytest

from app.db.engine import DB
from app.memory.builtin import BuiltinMemoryClient, TemplateEngine

ROOT = Path(__file__).resolve().parents[1]


def test_agent_history_survives_without_memory_permission():
    source = (ROOT / 'prompts/openbear-agent.tpl').read_text()
    engine = TemplateEngine()
    common = {'workspaceDir': '/isolated', 'builtinToolSummaries': {}}
    without_memory = engine.render(source, {**common, 'builtinToolNames': ['AgentHistory', 'Read']})
    with_memory = engine.render(source, {**common, 'builtinToolNames': ['AgentHistory', 'Read', 'TaskMemory']})
    assert '[[ERROR:' not in without_memory + with_memory
    assert 'Use AgentHistory for a specific missing original' in without_memory
    assert '## Agent working notes' not in without_memory
    assert '## Agent working notes' in with_memory
    assert 'Shared conversation material is supplied task input, not a grant of a memory tool.' in without_memory
    assert 'one rolling status record' not in with_memory
    assert 'Promptly preserve decisions' not in with_memory
    assert 'Never put credential plaintext in TaskMemory' not in with_memory


@pytest.mark.asyncio
async def test_new_prompt_version_can_render_without_rewriting_existing_snapshot(tmp_path):
    db = DB(str(tmp_path / 'prompt-versions.db'))
    await db.connect()
    try:
        old = 'An existing user-customized template; keep this exact text.'
        await db.conn.execute('INSERT INTO memory_templates (name,content,is_active) VALUES (?,?,1)', ('old', old))
        await db.conn.execute('INSERT INTO sessions (chat_id,system_snapshot) VALUES (?,?)', (91, 'FROZEN ORIGINAL'))
        source = (ROOT / 'prompts/openbear-system.tpl').read_text()
        await db.conn.execute('INSERT INTO memory_templates (name,content,is_active) VALUES (?,?,0)', ('new notes contract', source))
        await db.conn.commit()
        memory = BuiltinMemoryClient(db)
        rendered = await memory.render_system_prompt(
            {'toolNames': ['TaskMemory', 'History', 'AgentInfo', 'Memory'], 'workspaceDir': '/isolated'},
            template_content=source, template_name='candidate', source='test',
        )
        assert '[[ERROR:' not in rendered
        assert 'It is not a project archive, progress ledger, execution report' in rendered
        assert 'use AgentInfo for the current state and History for original exchanges' in rendered
        assert 'An auto-injected short body is usable directly' in rendered
        assert 'Keep at most one rolling status record' not in rendered
        assert 'Promptly record decisions' not in rendered
        current = await (await db.conn.execute('SELECT content,is_active FROM memory_templates WHERE name=?', ('old',))).fetchone()
        assert (current['content'], current['is_active']) == (old, 1)
        snapshot = await (await db.conn.execute('SELECT system_snapshot FROM sessions WHERE chat_id=91')).fetchone()
        assert snapshot[0] == 'FROZEN ORIGINAL'
    finally:
        await db.close()
