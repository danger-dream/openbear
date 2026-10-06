"""Opt-in browser notifications, independent of Telegram and its long-task threshold."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.logging import get_logger

log = get_logger("web.push")
_KEY = "web_push_vapid_private_key"
_ACTIVE = """SELECT p.* FROM web_push_subscriptions p JOIN web_sessions s
ON s.session_token_hash=p.session_token_hash AND s.chat_id=p.owner_chat_id
WHERE s.revoked_at=0 AND s.expires_at>?"""


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def validate_subscription(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("订阅数据无效")
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError("推送地址无效")
    try:
        url = urlsplit(endpoint)
        host = (url.hostname or "").lower()
        # A browser supplies the endpoint, but it is still untrusted HTTP input.
        # Only send to the actual supported browser push providers, never a LAN
        # address or a redirect target. Endpoints/tokens must not enter logs.
        allowed = host in {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
        allowed = allowed or host.endswith(".push.apple.com") or host.endswith(".notify.windows.com")
        if url.scheme != "https" or not allowed or url.port not in {None, 443} or url.username or url.password or url.fragment:
            raise ValueError()
        keys = value.get("keys") or {}
        decoded = {}
        for name, size in (("p256dh", 65), ("auth", 16)):
            text = keys.get(name)
            if not isinstance(text, str) or len(text) > 128:
                raise ValueError()
            raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
            if len(raw) != size:
                raise ValueError()
            decoded[name] = b64url(raw)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.urlsafe_b64decode(decoded["p256dh"] + "="))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("浏览器推送订阅无效或该推送服务暂不支持") from exc
    return {"endpoint": endpoint, "keys": decoded}


class BrowserPush:
    def __init__(self, db):
        self.db = db
        self.wake = asyncio.Event()
        self.task = None
        self.http = None
        self.presence: dict[int, dict[str, tuple[str, float]]] = {}

    async def key_pair(self) -> tuple[str, str]:
        async with self.db.write_transaction(label="browser-push-key") as conn:
            row = await (await conn.execute("SELECT value FROM app_state WHERE key=?", (_KEY,))).fetchone()
            if row:
                pem = str(row["value"])
                private = serialization.load_pem_private_key(pem.encode(), password=None)
            else:
                private = ec.generate_private_key(ec.SECP256R1())
                pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
                await conn.execute("INSERT INTO app_state(key,value,updated_at) VALUES(?,?,?)", (_KEY, pem, int(time.time())))
        public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return pem, b64url(public)

    async def subscriptions(self, owner: int) -> list[dict]:
        rows = await (await self.db.conn.execute(_ACTIVE + " AND p.owner_chat_id=?", (int(time.time()), owner))).fetchall()
        return [dict(row) for row in rows]

    async def start(self):
        # Recover the post-operation / pre-observer gap before opening delivery.
        await self.recover()
        await self.prune()
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.task = asyncio.create_task(self._worker(), name="browser-push-delivery")

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.http:
            await self.http.close()
            self.http = None

    @staticmethod
    def payload(kind: str, conversation: str, key: str, *, task_title: str = "", elapsed_seconds: int | None = None) -> dict:
        labels = {"completed": "任务已完成", "failed": "任务执行失败", "interrupted": "任务已中断", "interaction": "有一项操作需要你确认", "test": "本设备的通知测试"}
        title = " ".join(str(task_title or "").split())
        if len(title) > 80:
            title = title[:79] + "…"
        detail = labels[kind]
        if kind in {"completed", "failed", "interrupted"} and elapsed_seconds is not None and elapsed_seconds >= 0:
            seconds = int(elapsed_seconds)
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            duration = (f"{hours}小时{minutes:02d}分{secs:02d}秒" if hours else
                        f"{minutes}分{secs:02d}秒" if minutes else f"{secs}秒" if secs else "不到1秒")
            detail += " · 耗时 " + duration
        return {"title": title or "OpenBear", "body": detail, "conversationUuid": conversation,
                "tag": "openbear:" + key, "kind": kind, "createdAt": int(time.time())}

    async def enqueue(self, owner: int, conversation: str, key: str, kind: str, *, conn=None,
                      task_title: str = "", elapsed_seconds: int | None = None):
        rows = await self.subscriptions(owner)
        if not rows:
            return
        async def insert(target):
            title = task_title
            # Query after the subscription await, under the same writer transaction
            # as insertion. A delayed created callback cannot resurrect a resolved
            # interaction; the delivery worker also checks expiry before sending.
            if kind == "interaction" and not await self.interaction_pending(target, key, owner, conversation):
                return
            now = int(time.time())
            if not title and conversation:
                row = await (await target.execute("SELECT title FROM web_conversations WHERE conversation_uuid=? AND owner_chat_id=?",
                                                 (conversation, owner))).fetchone()
                if row:
                    title = str(row["title"] or "").strip() or "未命名会话"
            payload = json.dumps(self.payload(kind, conversation, key, task_title=title,
                                             elapsed_seconds=elapsed_seconds), ensure_ascii=False)
            for row in rows:
                await target.execute("""INSERT OR IGNORE INTO web_push_deliveries
                    (subscription_id,event_key,payload_json,next_attempt_at,created_at) VALUES(?,?,?,?,?)""",
                    (row["id"], key, payload, now, now))
        if conn is not None:
            await insert(conn)
        else:
            async with self.db.write_transaction(label="browser-push-enqueue") as target:
                await insert(target)
        self.wake.set()

    async def observe(self, event: dict, *, owner_chat_id: int, internal_chat_id: int):
        kind = str(event.get("type") or "")
        if kind not in {"accepted", "done", "error", "stopped"}:
            return
        root = str(event.get("runUuid") or event.get("rootTurnUuid") or event.get("turnUuid") or "")
        conversation = str(event.get("conversationUuid") or "")
        if not root or not conversation:
            return
        event_ms = int(event.get("ts") or 0)
        event_time = event_ms // 1000 if event_ms > 0 else int(time.time())
        if kind == "accepted":
            if any(event.get(key) for key in ("taskNotificationSilent", "hidden", "internal")) or not await self.subscriptions(owner_chat_id):
                return
            await self.db.conn.execute("""INSERT OR IGNORE INTO web_push_runs
                (root_uuid,conversation_uuid,owner_chat_id,created_at) VALUES(?,?,?,?)""",
                (root, conversation, owner_chat_id, event_time))
            await self.db.conn.commit()
            return
        status = {"done": "completed", "error": "failed", "stopped": "stopped"}[kind]
        async with self.db.write_transaction(label="browser-push-terminal") as conn:
            changed = await conn.execute("""UPDATE web_push_runs SET status=?
                WHERE root_uuid=? AND owner_chat_id=? AND conversation_uuid=? AND status='running'
                RETURNING created_at""", (status, root, owner_chat_id, conversation))
            run = await changed.fetchone()
            if run and status != "stopped":
                # Freeze elapsed time at the terminal event, never at a delivery
                # retry. The durable start survives process restarts/steering.
                elapsed = event_time - run["created_at"] if run["created_at"] > 0 else None
                await self.enqueue(owner_chat_id, conversation, "run:" + root, status, conn=conn,
                                   elapsed_seconds=elapsed)
        self.wake.set()  # Wake after COMMIT as well, so the reader sees the new row.

    async def on_interaction(self, event: str, item: dict):
        conversation = str(item.get("conversationUuid") or "")
        key = "interaction:" + str(item.get("interactionId") or "")
        if event == "created" and conversation:
            await self.enqueue(int(item["ownerChatId"]), conversation, key, "interaction")
        elif event != "created":
            async with self.db.write_transaction(label="browser-push-interaction-resolved") as conn:
                await conn.execute("DELETE FROM web_push_deliveries WHERE event_key=? AND state='pending'", (key,))

    @staticmethod
    async def interaction_pending(conn, key: str, owner: int, conversation: str) -> bool:
        if not key.startswith("interaction:"):
            return False
        row = await (await conn.execute("""SELECT 1 FROM user_interactions
            WHERE interaction_id=? AND owner_chat_id=? AND conversation_uuid=?
            AND status='pending' AND expires_at_ms>?""",
            (key.removeprefix("interaction:"), owner, conversation, int(time.time() * 1000)))).fetchone()
        return row is not None

    def set_presence(self, subscription_id: int, client_id: str, conversation: str):
        self._prune_presence()
        clients = self.presence.setdefault(subscription_id, {})
        if conversation:
            clients[client_id] = (conversation, time.monotonic() + 45)
        else:
            clients.pop(client_id, None)
        if not clients:
            self.presence.pop(subscription_id, None)

    def _prune_presence(self):
        now = time.monotonic()
        for subscription_id, clients in list(self.presence.items()):
            for client_id, (_, until) in list(clients.items()):
                if until <= now:
                    clients.pop(client_id, None)
            if not clients:
                self.presence.pop(subscription_id, None)

    async def recover(self):
        """Reconcile eligible observed starts against durable run operations.

        Never infer success from an orphan/active run. Old terminal events are
        settled without a late alert; recovery does not restart the push TTL.
        """
        while True:
            rows = await (await self.db.conn.execute("""SELECT p.*, o.status AS terminal_status,
                o.payload_json AS operation_payload FROM web_push_runs p
                JOIN web_conversations c ON c.conversation_uuid=p.conversation_uuid AND c.owner_chat_id=p.owner_chat_id
                JOIN web_operations o ON o.conversation_uuid=p.conversation_uuid AND o.op_id='run:'||p.root_uuid
                WHERE p.status='running' AND o.op_type='run' AND o.lifecycle='terminal'
                AND o.status IN ('completed','failed','cancelled','interrupted') LIMIT 200""")).fetchall()
            if not rows:
                return
            for row in rows:
                status = "stopped" if row["terminal_status"] == "cancelled" else row["terminal_status"]
                # terminalAtMs is the immutable first terminal boundary, unlike
                # updated_at_ms which may change with later operation patches.
                payload = json.loads(row["operation_payload"])
                ended = int(payload.get("terminalAtMs") or 0) // 1000
                async with self.db.write_transaction(label="browser-push-recover") as conn:
                    changed = await conn.execute("UPDATE web_push_runs SET status=? WHERE root_uuid=? AND status='running'",
                                                 (status, row["root_uuid"]))
                    if changed.rowcount and status != "stopped" and ended > 0 and 0 <= int(time.time()) - ended < 900:
                        # An interrupted operation may be closed by restart
                        # reconciliation: that timestamp is not execution time.
                        elapsed = ended - row["created_at"] if row["created_at"] > 0 and status != "interrupted" else None
                        await self.enqueue(row["owner_chat_id"], row["conversation_uuid"], "run:" + row["root_uuid"], status,
                                           conn=conn, elapsed_seconds=elapsed)
                        await conn.execute("UPDATE web_push_deliveries SET created_at=? WHERE event_key=? AND state='pending'",
                                           (ended, "run:" + row["root_uuid"]))
            self.wake.set()

    async def prune(self) -> tuple[int, int]:
        totals = [0, 0]
        now = int(time.time())
        for index, (table, predicate, params) in enumerate((
            ("web_push_deliveries", "created_at<?", (now - 86400,)),
            ("web_push_runs", """created_at<? AND (status!='running' OR NOT EXISTS (
                SELECT 1 FROM web_operations o WHERE o.conversation_uuid=web_push_runs.conversation_uuid
                AND o.op_id='run:'||web_push_runs.root_uuid AND o.op_type='run'
                AND o.lifecycle IN ('active','paused','waiting_control')))""", (now - 7 * 86400,)),
        )):
            while True:
                async with self.db.write_transaction(label="browser-push-prune") as conn:
                    cur = await conn.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {predicate} LIMIT 500)", params)
                totals[index] += cur.rowcount
                if cur.rowcount < 500:
                    break
        self._prune_presence()
        return totals[1], totals[0]

    async def send(self, subscription: dict, payload: dict) -> int:
        # Standard RFC 8291 encryption and VAPID signing; no custom crypto.
        from py_vapid import Vapid
        from pywebpush import WebPusher

        info = validate_subscription(json.loads(subscription["subscription_json"]))
        pem, _ = await self.key_pair()
        url = urlsplit(info["endpoint"])
        subject = subscription["origin"]
        if not subject.startswith("https://"):
            subject = "mailto:notifications@openbear.invalid"
        headers = Vapid.from_pem(pem.encode()).sign({"aud": f"{url.scheme}://{url.netloc}", "sub": subject, "exp": int(time.time()) + 3600})
        encoded = WebPusher(info).encode(json.dumps(payload, ensure_ascii=False).encode(), "aes128gcm")
        headers.update({"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "900", "Urgency": "normal"})
        async with self.http.post(info["endpoint"], data=encoded["body"], headers=headers, allow_redirects=False) as response:
            return response.status

    async def deliver_one(self) -> bool:
        now = int(time.time())
        row = await (await self.db.conn.execute("""SELECT * FROM web_push_deliveries
            WHERE state='pending' AND next_attempt_at<=? ORDER BY id LIMIT 1""", (now,))).fetchone()
        if not row:
            return False
        subscription = await (await self.db.conn.execute(_ACTIVE + " AND p.id=?", (now, row["subscription_id"]))).fetchone()
        status = 0
        payload = json.loads(row["payload_json"])
        self._prune_presence()
        quiet = any(conversation == payload.get("conversationUuid") for conversation, _ in self.presence.get(row["subscription_id"], {}).values())
        attempts = row["attempts"]
        error = ""
        state = "failed"
        if not subscription:
            error = "subscription_inactive"
        elif now - row["created_at"] >= 900:
            error = "expired"
        elif payload.get("kind") == "interaction" and not await self.interaction_pending(
                self.db.conn, row["event_key"], subscription["owner_chat_id"], str(payload.get("conversationUuid") or "")):
            state, error = "suppressed", "interaction_inactive"
        elif quiet:
            state, error = "suppressed", "foreground"
        else:
            attempts += 1
            try:
                status = await self.send(dict(subscription), payload)
                if not 200 <= status < 300:
                    error = "provider_http_" + str(status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Store only the exception class, never endpoint/payload content.
                error = type(exc).__name__
                log.warning("浏览器推送投递失败", error_type=error)
            retry = attempts < 3 and (status == 0 or status == 429 or status >= 500)
            state = "pending" if retry else ("accepted" if 200 <= status < 300 else "failed")
        async with self.db.write_transaction(label="browser-push-delivery") as conn:
            await conn.execute("""UPDATE web_push_deliveries SET attempts=?,state=?,next_attempt_at=?,last_status=?,last_error=?
                WHERE id=? AND state='pending'""", (attempts, state, now + 30 * attempts, status, error, row["id"]))
            if status in {404, 410}:
                await conn.execute("DELETE FROM web_push_subscriptions WHERE id=?", (row["subscription_id"],))
        return True

    async def _worker(self):
        while True:
            self.wake.clear()
            try:
                await self.recover()
                while await self.deliver_one():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("浏览器推送队列暂不可用", error_type=type(exc).__name__)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=30)
