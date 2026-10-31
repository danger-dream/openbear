"""Event-facing UI projections with real dispatch, transcripts and legacy repair."""
import asyncio
import json

from app.webhooks.repository import one, many
from app.webhooks.presentation import conversation_title, event_summary
from app.web_operations import web_event_operation_specs
from tests.test_webhooks_backend import env, create, accept


def test_names_summaries_and_source_are_not_guessed():
    assert conversation_title('订单', 1791049264000, 'abcdef123') == '10-04 01:41:04 · 订单 · abcdef'
    assert event_summary({'text': '新消息', 'uuid': 'should-not-show'}) == '新消息'
    assert event_summary({'uuid': 'should-not-show'}) == ''
    event = {'type': 'user', 'turnUuid': 'root', 'messageUuid': 'aid', 'text': 'unchanged', 'source': 'webhook', 'eventCard': {'name': '来信'}}
    spec = web_event_operation_specs(event)[0]
    assert spec['source'] == 'webhook' and spec['payload']['eventTriggered']
    assert spec['payload']['text'] == 'unchanged'
    event['source'] = 'user'
    assert 'eventTriggered' not in web_event_operation_specs(event)[0]['payload']


async def dispatch(env, c, key='one'):
    await accept(env, c, '{"title":"订单入库","text":"测试正文"}', key)
    await env.worker.tick()
    await asyncio.gather(*env.server.runs.scheduler.tasks(kind='controller'))
    return await one(env.db.conn, 'SELECT * FROM webhook_assignments ORDER BY created_at_ms DESC LIMIT 1')


async def test_live_card_original_input_and_recent_list(env):
    c = await create(env, batching={'enabled': False})
    a = await dispatch(env, c)
    cid = a['conversation_uuid']
    row = await one(env.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=?', (cid,))
    assert 'Fixture' in row['title'] and ' · ' in row['title'] and row['title'] != 'Webhook'
    ops = await env.server._web_operations(cid)
    user = next(op for op in ops if op['opType'] == 'user_message')
    assert user['source'] == 'webhook'
    assert user['payload']['eventCard']['name'] == 'Fixture'
    assert user['payload']['eventCard']['events'][0]['summary'] == '订单入库 · 测试正文'
    assert 'Handle only assigned fixture events' in user['payload']['text']
    assert 'External event materials' in user['payload']['text']
    answers = [op for op in ops if op['opType'] == 'assistant_message']
    assert answers and all(op['payload']['eventTriggered'] for op in answers)
    await env.server._tree_ensure_interaction_projection(123)
    row = await one(env.db.conn, 'SELECT last_interaction_at_ms FROM web_conversations WHERE conversation_uuid=?', (cid,))
    assert row['last_interaction_at_ms'] == 0
    # A real human can still promote this exact conversation, including an
    # interruption in the event root; subsequent automatic replies cannot bump it.
    live = env.server._live_for(await env.server._conversation_row(123, cid))
    await live.publish({'type': 'user', 'turnUuid': a['root_turn_uuid'], 'messageUuid': 'human', 'text': '我来继续', 'interruption': True})
    before = (await one(env.db.conn, 'SELECT last_interaction_at_ms FROM web_conversations WHERE conversation_uuid=?', (cid,)))['last_interaction_at_ms']
    assert before > 0
    await env.server._publish_operation(cid, op_id='assistant:later', op_type='assistant_message', action='end', turn_uuid=a['root_turn_uuid'], payload={'text':'auto later'}, status='completed', lifecycle='terminal')
    after = (await one(env.db.conn, 'SELECT last_interaction_at_ms FROM web_conversations WHERE conversation_uuid=?', (cid,)))['last_interaction_at_ms']
    assert after == before


async def test_fixed_conversation_keeps_name_and_human_recency(env):
    row = await env.server._create_web_conversation(123, title='用户起的标题', folder_uuid='folder')
    cid = row['conversation_uuid']
    await env.server._live_for(row).publish({'type':'user','turnUuid':'human','messageUuid':'human','text':'原来的交谈'})
    await env.server._tree_ensure_interaction_projection(123)
    before = (await one(env.db.conn, 'SELECT last_interaction_at_ms FROM web_conversations WHERE conversation_uuid=?', (cid,)))['last_interaction_at_ms']
    c = await create(env, batching={'enabled':False}, target={'mode':'fixedConversation','conversationId':cid})
    await dispatch(env, c)
    current = await one(env.db.conn, 'SELECT title,last_interaction_at_ms FROM web_conversations WHERE conversation_uuid=?', (cid,))
    assert current['title'] == '用户起的标题' and current['last_interaction_at_ms'] == before
