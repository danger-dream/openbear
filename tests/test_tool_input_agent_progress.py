from __future__ import annotations

from app.agents.execution import AgentExecutor
from app.llm.events import StreamEvent
from app.tools.base import ToolRegistry
from tests import test_rath_single_agent as agent_fixtures

env = agent_fixtures.env


async def test_agent_parameter_progress_uses_existing_events_and_finishes_cleanly(env):
    dao, task_uuid, agent = env

    class Backend:
        protocol = 'responses'

        async def stream(self, *args, **kwargs):
            for size in (0, 12010):
                yield StreamEvent(kind='tool_input', details={
                    'toolNames': ['Read'], 'receivedBytes': size,
                    'startedAtMs': 1000, 'updatedAtMs': 2000,
                    'elapsedMs': 1000, 'phase': 'generating'})
            yield StreamEvent(kind='content', text='完成')
            yield StreamEvent(kind='finish', finish_reason='stop')

    runner = AgentExecutor(dao, task_uuid, agent=agent, backend=Backend(),
                           model='gpt', max_tokens=2048, tools=ToolRegistry())
    result = await runner.run()
    assert result['summary'] == '完成'
    events = await dao.events(task_uuid)
    progress = [e for e in events if e.kind == 'model_stream_progress' and e.detail.get('toolInput')]
    assert len(progress) == 2
    assert progress[-1].detail['toolInput']['receivedBytes'] == 12010
    assert progress[-1].detail['toolInput']['attemptId']
    assert 'arguments' not in progress[-1].detail['toolInput']
    assert not any(e.kind == 'tool_call_started' for e in events)
    assert (await dao.get_task(task_uuid)).status == 'completed'
