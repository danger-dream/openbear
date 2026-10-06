from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.db.engine import DB
from app.web_admin import WebAdminServer
from app.web_console.core import _COOKIE, _sha256
from app.web_console.live_stream import _WebLiveStream
from app.web_push import BrowserPush, b64url, validate_subscription


def subscription(endpoint="https://fcm.googleapis.com/wp/device-one"):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "keys": {"p256dh": b64url(public), "auth": b64url(b"0123456789abcdef")}}


@pytest.fixture
async def db(tmp_path):
    db = DB(str(tmp_path / "push.db"))
    await db.connect()
    await db.conn.execute("INSERT INTO web_sessions(session_token_hash,chat_id,expires_at) VALUES(?,?,?)", (_sha256("token"), 123, int(time.time()) + 3600))
    await db.conn.commit()
    yield db
    await db.close()


async def add_device(db, *, owner=123, endpoint="https://fcm.googleapis.com/wp/device-one"):
    info = subscription(endpoint)
    await db.conn.execute("""INSERT INTO web_push_subscriptions(endpoint,owner_chat_id,session_token_hash,subscription_json,origin,updated_at)
        VALUES(?,?,?,?,?,?)""", (endpoint, owner, _sha256("token"), json.dumps(info), "https://bear.example.test", int(time.time())))
    await db.conn.commit()
    return info


async def add_interaction(db, *, iid="question", conversation="conv", expires=None):
    now = int(time.time() * 1000)
    await db.conn.execute("""INSERT INTO user_interactions
        (interaction_id,owner_chat_id,conversation_uuid,expires_at_ms,created_at_ms,payload_json)
        VALUES(?,123,?,?,?,?)""", (iid, conversation, expires if expires is not None else now + 600000, now, '{"action":"confirm"}'))
    await db.conn.commit()


async def observe(push, kind, **extra):
    await push.observe({"type": kind, "runUuid": "run", "turnUuid": "turn", "conversationUuid": "conv", **extra}, owner_chat_id=123, internal_chat_id=-1)


async def deliveries(db):
    return [dict(r) for r in await (await db.conn.execute("SELECT * FROM web_push_deliveries ORDER BY id")).fetchall()]


@pytest.mark.parametrize("seconds,expected", [(0, "不到1秒"), (9, "9秒"), (192, "3分12秒"), (3723, "1小时02分03秒")])
def test_payload_identifies_task_and_formats_known_elapsed(seconds, expected):
    payload = BrowserPush.payload("completed", "conv", "run:one", task_title="  优化通知\n样式  ", elapsed_seconds=seconds)
    assert payload["body"] == f"任务已完成 · 耗时 {expected}"
    assert payload["title"] == "优化通知 样式" and payload["conversationUuid"] == "conv"
    assert payload["tag"] == "openbear:run:one"


@pytest.mark.parametrize("seconds", [None, -1])
def test_payload_never_invents_unknown_elapsed_and_bounds_long_title(seconds):
    payload = BrowserPush.payload("failed", "conv", "one", task_title="长" * 150, elapsed_seconds=seconds)
    assert payload["title"] == "长" * 79 + "…"
    assert payload["body"].endswith("任务执行失败") and "耗时" not in payload["body"]
    assert BrowserPush.payload("test", "", "test")["body"] == "本设备的通知测试"


@pytest.mark.parametrize("terminal,label", [("done", "任务已完成"), ("error", "任务执行失败")])
async def test_terminal_uses_latest_owned_title_and_durable_start_not_delivery_time(db, terminal, label):
    await add_device(db)
    await db.conn.execute("INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title) VALUES('conv',123,-1,'旧标题')")
    await db.conn.commit()
    push = BrowserPush(db)
    started = int(time.time()) - 200
    await observe(push, "accepted", ts=started * 1000)
    await observe(push, "accepted", ts=(started + 100) * 1000)  # Duplicate/steering cannot reset start.
    await db.conn.execute("UPDATE web_conversations SET title='优化通知样式' WHERE conversation_uuid='conv'")
    await db.conn.commit()
    push = BrowserPush(db)  # Start time survives process recreation.
    await observe(push, terminal, ts=(started + 192) * 1000, text="private answer", stats={"durationMs": 500})
    await observe(push, terminal, ts=(started + 199) * 1000)
    rows = await deliveries(db)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["title"] == "优化通知样式"
    assert payload["body"] == f"{label} · 耗时 3分12秒"
    assert "private" not in rows[0]["payload_json"]
    push.send = AsyncMock(return_value=503)
    await push.deliver_one()
    assert push.send.call_args.args[1] == payload
    await db.conn.execute("UPDATE web_push_deliveries SET next_attempt_at=0")
    await db.conn.commit()
    push.send = AsyncMock(return_value=201)
    await push.deliver_one()
    assert push.send.call_args.args[1] == payload


async def test_interaction_identifies_conversation_without_exposing_question(db):
    await add_device(db)
    await db.conn.execute("INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title) VALUES('conv',123,-1,'上线通知改进')")
    await db.conn.commit()
    await add_interaction(db)
    await BrowserPush(db).on_interaction("created", {"interactionId": "question", "ownerChatId": 123, "conversationUuid": "conv", "sensitive": True, "title": "secret question", "body": "secret body"})
    payload = json.loads((await deliveries(db))[0]["payload_json"])
    assert payload["title"] == "上线通知改进"
    assert payload["body"] == "有一项操作需要你确认"
    assert "secret" not in str(payload) and "耗时" not in payload["body"]


async def test_title_lookup_respects_owner_and_unnamed_fallback(db):
    await add_device(db)
    await db.conn.execute("INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title) VALUES('conv',999,-1,'other owner title'),('empty',123,-2,'')")
    await db.conn.commit()
    push = BrowserPush(db)
    await push.enqueue(123, "conv", "foreign", "completed")
    await push.enqueue(123, "empty", "empty", "completed")
    rows = await deliveries(db)
    assert json.loads(rows[0]["payload_json"])["body"] == "任务已完成"
    assert json.loads(rows[0]["payload_json"])["title"] == "OpenBear"
    assert json.loads(rows[1]["payload_json"])["title"] == "未命名会话"
    assert json.loads(rows[1]["payload_json"])["body"] == "任务已完成"


@pytest.mark.parametrize("status,label", [("completed", "任务已完成"), ("interrupted", "任务已中断")])
async def test_webhook_push_uses_chain_end_not_notification_retry_time(db, status, label):
    from app.webhooks.notifications import deliver
    from app.webhooks.repository import one

    await add_device(db)
    started = int(time.time()) - 600
    await db.conn.execute("INSERT INTO web_conversations(conversation_uuid,owner_chat_id,internal_chat_id,title) VALUES('conv',123,-1,'处理外部消息')")
    payload = {"rootTurnId": "root", "assignmentId": "assignment", "startedAtMs": started * 1000,
               "status": status, "postState": "succeeded", "finalText": "private output"}
    await db.conn.execute("""INSERT INTO web_task_notifications
        (notification_uuid,notification_key,conversation_uuid,internal_chat_id,owner_chat_id,payload_json,created_at,updated_at)
        VALUES('notice','webhook:one','conv',-1,123,?,?,?)""", (json.dumps(payload), started + 192, started + 192))
    await db.conn.commit()
    host = SimpleNamespace(browser_push=BrowserPush(db), _live_for=lambda conv: SimpleNamespace(publish=AsyncMock()))
    service = SimpleNamespace(db=db, host=host, clock=lambda: int(time.time() * 1000))
    notice = await one(db.conn, "SELECT * FROM web_task_notifications WHERE notification_uuid='notice'")
    await deliver(service, notice)
    await deliver(service, notice)
    rows = await deliveries(db)
    assert len(rows) == 1
    assert json.loads(rows[0]['payload_json'])['title'] == '处理外部消息'
    assert json.loads(rows[0]['payload_json'])['body'] == f'{label} · 耗时 3分12秒'
    assert 'private' not in rows[0]['payload_json']


@pytest.mark.parametrize("endpoint", ["http://fcm.googleapis.com/wp/a", "https://127.0.0.1/x", "https://fcm.googleapis.com.evil.test/x", "https://evil.test/x", "https://a:b@web.push.apple.com/x", "https://web.push.apple.com:444/x", "https://web.push.apple.com/x#bad"])
def test_endpoint_validation(endpoint):
    with pytest.raises(ValueError):
        validate_subscription(subscription(endpoint))


@pytest.mark.parametrize("endpoint", ["https://fcm.googleapis.com/wp/a", "https://updates.push.services.mozilla.com/wpush/v2/a", "https://web.push.apple.com/a", "https://wns2-bl2p.notify.windows.com/a"])
def test_supported_push_endpoints(endpoint):
    assert validate_subscription(subscription(endpoint))["endpoint"] == endpoint


async def test_vapid_keys_persist_and_are_unique_per_database(db):
    first, concurrent = await asyncio.gather(BrowserPush(db).key_pair(), BrowserPush(db).key_pair())
    assert first == concurrent == await BrowserPush(db).key_pair()
    assert first[0].startswith("-----BEGIN PRIVATE KEY-----")
    assert len(first[1]) == 87


async def test_terminal_deduplication_no_final_or_retry_notification(db):
    await add_device(db)
    push = BrowserPush(db)
    await observe(push, "accepted")
    for event in ["delta", "final", "retry_wait", "tool_result"]:
        await observe(push, event, text="private answer", reasoning="private reasoning")
    assert not await deliveries(db)
    await observe(push, "error")
    await observe(push, "done")
    await observe(push, "error")
    rows = await deliveries(db)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["kind"] == "failed"
    assert "private" not in rows[0]["payload_json"]


async def test_live_stream_completes_by_run_not_latest_steering_turn(db):
    await add_device(db)
    push = BrowserPush(db)
    async def sink(event):
        await push.observe(event, owner_chat_id=123, internal_chat_id=-1)
        return event
    live = _WebLiveStream("conv", -1, event_sink=sink)
    await live.publish({"type": "accepted", "turnUuid": "turn", "runUuid": "run"})
    await live.publish({"type": "user", "turnUuid": "later-steering-turn"})
    await live.publish({"type": "final", "text": "private result"})
    await live.publish({"type": "done"})
    rows = await deliveries(db)
    assert len(rows) == 1 and rows[0]["event_key"] == "run:run"
    assert json.loads(rows[0]["payload_json"])["kind"] == "completed"


@pytest.mark.parametrize("flag", ["hidden", "internal", "taskNotificationSilent"])
async def test_internal_turns_do_not_notify(db, flag):
    await add_device(db)
    push = BrowserPush(db)
    await observe(push, "accepted", **{flag: True})
    await observe(push, "done")
    assert not await deliveries(db)


async def test_no_subscription_and_explicit_stop_do_not_notify(db):
    push = BrowserPush(db)
    await observe(push, "accepted")
    await observe(push, "done")
    assert not await deliveries(db)
    await add_device(db)
    await observe(push, "accepted")
    await observe(push, "stopped")
    await observe(push, "done")
    assert not await deliveries(db)


async def test_interaction_private_content_never_sent_and_resolved_queue_removed(db):
    await add_device(db)
    push = BrowserPush(db)
    await add_interaction(db)
    item = {"interactionId": "question", "ownerChatId": 123, "conversationUuid": "conv", "sensitive": True, "title": "secret", "body": "secret", "result": {"text": "secret"}}
    await push.on_interaction("created", item)
    await push.on_interaction("created", item)
    rows = await deliveries(db)
    assert len(rows) == 1 and "secret" not in rows[0]["payload_json"]
    await push.on_interaction("resolved", item)
    assert not await deliveries(db)


async def test_worker_restart_retry_and_gone_subscription_cleanup(db):
    await add_device(db)
    push = BrowserPush(db)
    await push.enqueue(123, "conv", "one", "completed")
    push = BrowserPush(db)  # durable queue, not in-memory handoff
    push.send = AsyncMock(return_value=503)
    assert await push.deliver_one()
    row = (await deliveries(db))[0]
    assert row["attempts"] == 1 and row["state"] == "pending"
    await db.conn.execute("UPDATE web_push_deliveries SET next_attempt_at=0")
    await db.conn.commit()
    push.send = AsyncMock(return_value=410)
    assert await push.deliver_one()
    assert not await push.subscriptions(123)
    assert (await deliveries(db))[0]["state"] == "failed"


@pytest.mark.parametrize("revoke", [True, False])
async def test_session_revoke_or_expiry_stops_queued_notifications(db, revoke):
    await add_device(db)
    push = BrowserPush(db)
    await push.enqueue(123, "conv", "one", "completed")
    await db.conn.execute("UPDATE web_sessions SET " + ("revoked_at=1" if revoke else "expires_at=1"))
    await db.conn.commit()
    push.send = AsyncMock(return_value=201)
    await push.deliver_one()
    push.send.assert_not_called()
    assert not await push.subscriptions(123)


async def test_presence_suppresses_only_matching_foreground_device(db):
    await add_device(db)
    await add_device(db, endpoint="https://web.push.apple.com/second")
    push = BrowserPush(db)
    rows = await push.subscriptions(123)
    push.set_presence(rows[0]["id"], "page-a", "conv")
    push.send = AsyncMock(return_value=201)
    await push.enqueue(123, "conv", "one", "completed")
    await push.deliver_one()
    await push.deliver_one()
    assert push.send.await_count == 1
    assert push.send.call_args.args[0]["id"] == rows[1]["id"]


async def test_encryption_vapid_and_no_redirect_transport(db):
    import http_ece
    receiver = ec.generate_private_key(ec.SECP256R1())
    public = receiver.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    info = {"endpoint": "https://web.push.apple.com/device", "keys": {"p256dh": b64url(public), "auth": b64url(b"0123456789abcdef")}}
    push = BrowserPush(db)
    calls = []
    class Response:
        async def __aenter__(self): return SimpleNamespace(status=201)
        async def __aexit__(self, *args): pass
    class HTTP:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    push.http = HTTP()
    payload = push.payload("completed", "conv", "one")
    status = await push.send({"subscription_json": json.dumps(info), "origin": "https://bear.example.test"}, payload)
    assert status == 201
    _, kwargs = calls[0]
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Content-Encoding"] == "aes128gcm"
    assert kwargs["headers"]["Authorization"].startswith("vapid ")
    decrypted = http_ece.decrypt(kwargs["data"], private_key=receiver, auth_secret=b"0123456789abcdef", version="aes128gcm")
    assert json.loads(decrypted) == payload


@pytest.fixture
async def client(db):
    server = WebAdminServer.__new__(WebAdminServer)
    server.db = db
    server.config = SimpleNamespace(web=SimpleNamespace(custom_url=""))
    server.browser_push = BrowserPush(db)
    async def lifecycle(app): yield
    async def shutdown(app): pass
    server._realtime_context = lifecycle
    server._realtime_shutdown = shutdown
    server._web_push_context = lifecycle  # Tests drain the real queue explicitly.
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    client.session.cookie_jar.update_cookies({_COOKIE: "token"})
    yield SimpleNamespace(client=client, server=server)
    await client.close()


async def test_http_subscription_status_test_disable_and_csrf(client, db):
    http = client.client
    data = await (await http.get("/api/push/key")).json()
    assert data["ok"] and len(data["publicKey"]) == 87 and "private" not in str(data).lower()
    info = subscription()
    response = await http.post("/api/push/subscription", json={"subscription": info}, headers={"Origin": "https://evil.test"})
    assert response.status == 403
    response = await http.post("/api/push/subscription", json={"subscription": info})
    assert response.status == 200
    response = await http.post("/api/push/status", json={"endpoint": info["endpoint"]})
    assert (await response.json())["enabled"] is True
    client.server.browser_push.send = AsyncMock(return_value=201)
    response = await http.post("/api/push/test", json={"endpoint": info["endpoint"]})
    assert (await response.json()) == {"ok": True, "accepted": True}
    response = await http.delete("/api/push/subscription", json={"endpoint": info["endpoint"]})
    assert (await response.json())["enabled"] is False
    assert not await client.server.browser_push.subscriptions(123)
    http.session.cookie_jar.clear()
    assert (await http.get("/api/push/key")).status == 401


async def test_http_rejects_invalid_keys_and_another_owner(client, db):
    info = await add_device(db, owner=999)
    response = await client.client.post("/api/push/subscription", json={"subscription": info})
    assert response.status == 409
    info["keys"]["auth"] = "invalid"
    response = await client.client.post("/api/push/subscription", json={"subscription": info})
    assert response.status == 400


async def test_http_presence_is_scoped_to_page_not_whole_subscription(client, db):
    info = await add_device(db)
    http, push = client.client, client.server.browser_push
    for page, conversation in [('a', 'conv'), ('b', ''), ('c', 'other')]:
        response = await http.post('/api/push/presence', json={
            'endpoint': info['endpoint'], 'clientId': page, 'conversationUuid': conversation,
        })
        assert response.status == 200
    push.send = AsyncMock(return_value=201)
    await push.enqueue(123, 'conv', 'viewed', 'completed')
    await push.deliver_one()
    push.send.assert_not_called()
    assert (await deliveries(db))[0]['state'] == 'suppressed'
    await http.post('/api/push/presence', json={'endpoint': info['endpoint'], 'clientId': 'a', 'conversationUuid': ''})
    await push.enqueue(123, 'conv', 'left', 'completed')
    await push.deliver_one()
    push.send.assert_awaited_once()
