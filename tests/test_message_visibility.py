from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.db.engine import DB
from app.web_console.live_stream import _WebLiveStream
from app.web_console.message_visibility import WebAdminMessageVisibilityMixin, visibility_snapshot


class Harness(WebAdminMessageVisibilityMixin):
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()
        self.persisted = []
        self.live = _WebLiveStream('conv', 42, event_sink=self.persist)

    async def persist(self, event):
        self.persisted.append(event)
        return event

    async def _conversation_from_request(self, request):
        if request.match_info['conversation_uuid'] != 'conv':
            raise web.HTTPNotFound()
        return {'conversation_uuid': 'conv', 'internal_chat_id': 42}

    async def _json_body(self, request):
        value = await request.json()
        return value if isinstance(value, dict) else {}

    def _web_operation_lock(self, _):
        return self.lock

    def _live_for(self, _):
        return self.live


@pytest.fixture
async def visibility_api(tmp_path):
    db = DB(str(tmp_path / 'visibility.db'))
    await db.connect()
    await db.conn.execute("INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id) VALUES('conv',1,42)")
    for i, kind in enumerate(('user_message', 'assistant_message', 'tool', 'reasoning', 'run')):
        await db.conn.execute(
            """INSERT INTO web_operations(conversation_uuid,op_id,op_type,turn_uuid,display_seq,revision,payload_json,created_at_ms,updated_at_ms)
               VALUES('conv',?,?, 'turn',?,1,?,1000,1000)""",
            (f'op-{i}', kind, i, json.dumps({'text': f'private original {i}', 'complete': True})),
        )
    await db.conn.execute("INSERT INTO messages(chat_id,role,content) VALUES(42,'user','private original 0')")
    await db.conn.commit()
    harness = Harness(db)
    app = web.Application()
    app.add_routes([
        web.get('/{conversation_uuid}/visibility', harness.handle_api_message_visibility),
        web.put('/{conversation_uuid}/visibility', harness.handle_api_message_visibility_update),
        web.get('/{conversation_uuid}/hidden/{operation_id}', harness.handle_api_hidden_message_preview),
    ])
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, harness
    await client.close()
    await db.close()


async def test_hide_restore_keeps_model_and_operation_facts_and_broadcasts(visibility_api):
    client, h = visibility_api
    cur = await h.db.conn.execute('SELECT * FROM web_operations ORDER BY id')
    before = [dict(r) for r in await cur.fetchall()]
    pc, phone = h.live.subscribe(), h.live.subscribe()
    h.live.status = 'running'
    response = await client.put('/conv/visibility', json={'opIds': ['op-0', 'op-1'], 'hidden': True})
    assert response.status == 200
    state = (await response.json())['visibility']
    assert state['hiddenIds'] == ['op-0', 'op-1']
    assert 'private original' not in json.dumps(state)
    assert pc.get_nowait()['visibility'] == phone.get_nowait()['visibility'] == state
    assert h.persisted == [] and h.live.status == 'running'
    cur = await h.db.conn.execute('SELECT * FROM web_operations ORDER BY id')
    assert before == [dict(r) for r in await cur.fetchall()]
    cur = await h.db.conn.execute('SELECT content FROM messages')
    assert [r['content'] for r in await cur.fetchall()] == ['private original 0']
    detail = await client.get('/conv/hidden/op-0')
    assert (await detail.json())['operation']['payload']['text'] == 'private original 0'
    assert (await visibility_snapshot(h.db, 'conv')) == state  # preview does not restore
    response = await client.put('/conv/visibility', json={'opIds': ['op-0'], 'hidden': False})
    restored = (await response.json())['visibility']
    assert restored['hiddenIds'] == ['op-1'] and restored['revision'] > state['revision']
    assert (await client.get('/conv/hidden/op-0')).status == 404
    response = await client.put('/conv/visibility', json={'restoreAll': True})
    assert (await response.json())['visibility']['hiddenIds'] == []


async def test_stream_updates_reload_and_cleanup(visibility_api):
    client, h = visibility_api
    await client.put('/conv/visibility', json={'opIds': ['op-1'], 'hidden': True})
    await h.db.conn.execute("UPDATE web_operations SET payload_json=?,revision=2 WHERE op_id='op-1'", (json.dumps({'text': 'new streamed text'}),))
    await h.db.conn.commit()
    assert (await (await client.get('/conv/visibility')).json())['visibility']['hiddenIds'] == ['op-1']
    # A fresh connection/startup retains the exact same preference.
    await h.db.close()
    await h.db.connect()
    state = await visibility_snapshot(h.db, 'conv')
    assert state['hiddenIds'] == ['op-1']
    await h.db.conn.execute("DELETE FROM web_operations WHERE op_id='op-1'")
    await h.db.conn.commit()
    next_state = await visibility_snapshot(h.db, 'conv')
    assert next_state['hiddenIds'] == [] and next_state['revision'] > state['revision']


async def test_validation_ownership_atomicity_and_idempotence(visibility_api):
    client, h = visibility_api
    assert (await client.get('/other/visibility')).status == 404
    assert (await client.put('/other/visibility', json={'opIds': ['op-0'], 'hidden': True})).status == 404
    assert (await client.get('/other/hidden/op-0')).status == 404
    for data in ({'opIds': [], 'hidden': True}, {'opIds': [1], 'hidden': True}, {'opIds': ['op-0'], 'hidden': 'yes'}, {'restoreAll': True, 'hidden': True}):
        assert (await client.put('/conv/visibility', json=data)).status == 400
    for ids in (['op-0', 'missing'], ['op-4']):
        assert (await client.put('/conv/visibility', json={'opIds': ids, 'hidden': True})).status == 404
        assert (await visibility_snapshot(h.db, 'conv'))['hiddenIds'] == []
    data = {'opIds': ['op-0'], 'hidden': True}
    a = (await (await client.put('/conv/visibility', json=data)).json())['visibility']
    b = (await (await client.put('/conv/visibility', json=data)).json())['visibility']
    assert a == b
    await h.db.conn.execute("DELETE FROM web_conversations WHERE conversation_uuid='conv'")
    await h.db.conn.commit()
    assert (await visibility_snapshot(h.db, 'conv'))['hiddenIds'] == []
