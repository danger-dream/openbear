import asyncio
import json

import pytest

from app.llm.events import StreamEvent, ToolCall
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.servers import register_server_tools
from app.webhooks import recovery
from app.webhooks.contracts import WebhookError
from app.webhooks.repository import one
from tests.ddns.harness import OWNER, add_node, base_env, env  # noqa: F401
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend
from tests.test_webhooks_backend import accept, create


@pytest.mark.parametrize('failure,status,effect', [(ConnectionError, 'unknown', 'unknown'), (TimeoutError, 'unknown', 'unknown'), (LookupError, 'failed', 'not_started')])
async def test_write_dispatch_distinguishes_lost_result_from_offline(env, monkeypatch, failure, status, effect):
    sid = await add_node(env)
    async with env.db.write_transaction() as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('review-write',787,123,'t')")
    seen = []

    async def call(server_id, method, params, timeout):
        seen.append((method, params))
        raise failure()

    monkeypatch.setattr(env.servers.hub, 'call', call)
    registry = ToolRegistry()
    register_server_tools(registry, env.servers)
    result = await registry.dispatch_outcome('Servers', json.dumps({'action': 'write_file', 'params': {'serverId': sid, 'path': '/tmp/fixture', 'content': 'fixture'}}),
                                            context=ToolRuntimeContext(chat_id=787, conversation_uuid='review-write'))
    assert result.status == status and result.effect_state == effect
    assert seen[0][0] == 'fs.write' and seen[0][1]['done'] is True
    payload = json.loads(result.content)
    if effect == 'unknown':
        assert payload['effectState'] == 'unknown' and '核实' in payload['message']
    else:
        assert payload['error'] == 'node_offline'


async def test_failed_webhook_report_still_requires_verifying_unknown_server_write(env, monkeypatch):
    sid = await add_node(env)
    env.nodes.connect(sid)
    writes = []
    original = env.servers.hub.call

    async def call(server_id, method, params, timeout):
        if method == 'fs.write':
            writes.append(params)
            raise ConnectionError('reply lost')
        return await original(server_id, method, params, timeout)

    monkeypatch.setattr(env.servers.hub, 'call', call)
    register_server_tools(env.server.tools, env.servers)
    endpoint = await create(env, batching={'enabled': False}, processing={'instructions': 'Fixture write and report.', 'allowServerChanges': True})
    event = await accept(env, endpoint)

    class Backend(FakeStreamBackend):
        async def stream(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                tool = ToolCall('write', 'Servers', json.dumps({'action': 'write_file', 'params': {'serverId': sid, 'path': '/tmp/fixture', 'content': 'fixture'}}))
            elif self.calls == 2:
                tool = ToolCall('report', 'Webhook', json.dumps({'action': 'report', 'params': {'receiptId': 'result', 'results': [{'eventId': event['eventId'], 'outcome': 'failed', 'reason': 'reply lost'}]}}))
            else:
                yield StreamEvent(kind='content', text='Result unknown.')
                yield StreamEvent(kind='finish', finish_reason='stop')
                return
            yield StreamEvent(kind='tool_call', tool_calls=[tool])
            yield StreamEvent(kind='finish', finish_reason='tool_calls')

    env.server.llm_factory = FakeRunFactory(Backend(), context_window=128000)
    await env.worker.tick()
    await asyncio.wait_for(asyncio.gather(*env.server.runs.scheduler.tasks(kind='controller')), 10)
    action = await one(env.db.conn, "SELECT status,outcome_json FROM runtime_actions WHERE name='Servers'")
    assert action['status'] == 'unknown' and json.loads(action['outcome_json'])['effectState'] == 'unknown'
    receipt = await one(env.db.conn, 'SELECT outcome FROM webhook_receipts ORDER BY result_version DESC LIMIT 1')
    assert receipt['outcome'] == 'failed'
    row = await one(env.db.conn, 'SELECT route_version FROM webhook_events WHERE event_id=?', (event['eventId'],))
    with pytest.raises(WebhookError) as error:
        await recovery.retry(env.s, OWNER, event['eventId'], {'stage': 'model', 'requestId': 'retry', 'expectedVersion': row['route_version']})
    assert error.value.payload['code'] == 'verify_unknown_effect_first'
    assert len(writes) == 1
