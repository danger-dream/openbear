"""One authenticated global metadata channel, independent of chat streaming."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from app.web_console.core import _WEB_SESSION_KEY
from app.reference_policy import CONVERSATION_CONTENT_LIMIT, catalog_history_sizes


def catalog_preview(value: str) -> str:
    """A bounded, single-line excerpt, never a second copy of a resource body."""
    text = " ".join(str(value or "").split())
    return text[:30] + ("…" if len(text) > 30 else "")


# Project only string field names inside SQLite. Credential values/notes and the
# complete kv_json must never leave the database as part of a catalog row.
_SECRET_KEYS_SQL = """(
    SELECT json_group_array(json_extract(field.value, '$.key'))
    FROM json_each(CASE WHEN json_valid(kv_json) THEN
        CASE WHEN json_type(kv_json)='array' THEN kv_json ELSE '[]' END
        ELSE '[]' END) AS field
    WHERE CASE WHEN field.type='object'
        THEN json_type(field.value, '$.key')='text' ELSE 0 END
)"""


def read_catalog(
    db_path: str, owner: int, include_archived: bool = False, length_cache: dict | None = None
) -> tuple[int, dict[str, dict]]:
    """Read a metadata snapshot and watermark in the SAME SQLite snapshot."""
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        seq = int(
            conn.execute("SELECT COALESCE(MAX(seq),0) FROM web_catalog_changes").fetchone()[0]
        )
        revisions = {
            f"{r['kind']}:{r['entity_id']}": int(r["revision"])
            for r in conn.execute(
                "SELECT kind,entity_id,MAX(seq) AS revision FROM web_catalog_changes GROUP BY kind,entity_id"
            )
        }
        items: dict[str, dict] = {}
        for kind, table, name, title in [
            ("mem", "memory_entries", "ref", "title"),
            ("doc", "memory_docs", "name", "title"),
            ("secret", "memory_secrets", "name", "name"),
        ]:
            # Read a short excerpt for ordinary resources; credential projection
            # is names-only. Never fetch full bodies, values or arbitrary metadata.
            projection = {
                "mem": "substr(COALESCE(NULLIF(trim(body),''),fields_json,''),1,256)",
                "doc": "substr(content,1,256)",
                "secret": _SECRET_KEYS_SQL,
            }[kind]
            rows = conn.execute(
                f"SELECT id,{name} AS name,{title} AS title,grp,enabled,archived,updated_at,"
                f"{projection} AS preview_source FROM {table}"
            )
            for row in rows:
                if (not row["enabled"] and not row["archived"]) or (
                    row["archived"] and not include_archived
                ):
                    continue
                key = f"{kind}:{row['id']}"
                items[key] = {
                    "key": key,
                    "kind": kind,
                    "id": str(row["id"]),
                    "name": row["name"],
                    "label": row["title"] or row["name"],
                    "group": row["grp"] or "",
                    "archived": bool(row["archived"]),
                    "updatedAt": row["updated_at"] or 0,
                    "revision": revisions.get(key, 0),
                }
                if kind == "secret":
                    items[key]["fieldKeys"] = list(
                        dict.fromkeys(
                            key.strip()
                            for key in json.loads(row["preview_source"] or "[]")
                            if key.strip()
                        )
                    )
                else:
                    source = row["preview_source"] or ""
                    items[key]["preview"] = catalog_preview("" if source == "{}" else source)
        folders = {
            str(r["folder_uuid"]): dict(r)
            for r in conn.execute(
                "SELECT folder_uuid,parent_uuid,name FROM web_conversation_folders WHERE owner_chat_id=?",
                (owner,),
            )
        }

        def folder_path(key: str) -> str:
            names, seen = [], set()
            while key and key in folders and key not in seen:
                seen.add(key)
                names.append(str(folders[key]["name"]))
                key = str(folders[key]["parent_uuid"] or "")
            return " / ".join(reversed(names))

        # Finish this metadata cursor before nested length reads register their
        # SQLite function; re-registering while a statement is active fails once
        # the catalog has at least three conversations. Keep the same snapshot.
        for row in conn.execute(
            """SELECT conversation_uuid,title,folder_uuid,archived_at,updated_at
                FROM web_conversations WHERE owner_chat_id=? ORDER BY id DESC""",
            (owner,),
        ).fetchall():
            if row["archived_at"] and not include_archived:
                continue
            key = "chat:" + row["conversation_uuid"]
            sizes = catalog_history_sizes(conn, row["conversation_uuid"], length_cache)
            items[key] = {
                "bodyChars": sizes["bodyChars"],
                "recentTurnChars": sizes["recentTurnChars"],
                "key": key,
                "kind": "chat",
                "id": row["conversation_uuid"],
                "name": row["conversation_uuid"],
                "label": row["title"] or "新会话",
                "group": folder_path(row["folder_uuid"]) or "临时会话",
                "archived": bool(row["archived_at"]),
                "updatedAt": row["updated_at"] or 0,
                "revision": revisions.get(key, 0),
            }
        return seq, items


class GlobalRealtime:
    def __init__(self, owner: Any):
        self.owner = owner
        self.epoch = uuid.uuid4().hex
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.clients: dict[asyncio.Queue, dict] = {}
        self.task: asyncio.Task | None = None
        self._stopping = False
        self.serial = 0
        self.last_cursor = -1
        self.last_calibration = 0.0
        self.reference_length_cache = {}
        self.cached_version: dict = {}
        self.version_at = 0.0

    async def start(self):
        if self.task and not self.task.done():
            return
        self._stopping = False
        listeners = getattr(self.owner.db.conn, "commit_listeners", None)
        if listeners is not None:
            listeners.add(self.wake.set)
        self.task = asyncio.create_task(self.run(), name="web-global-metadata")

    async def close(self):
        listeners = getattr(self.owner.db.conn, "commit_listeners", None)
        if listeners is not None:
            listeners.discard(self.wake.set)
        if not self._stopping:
            # Own the stop transition before cancelling. Repeated close calls
            # must join the task, not interrupt its cancellation cleanup again.
            self._stopping = True
            if self.task:
                self.task.cancel()
        if self.task:
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        for queue in tuple(self.clients):
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait({"type": "reconnect"})
        self.clients.clear()

    def put(self, queue: asyncio.Queue, value: dict):
        if queue.full():
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait({"type": "resync"})
            return
        queue.put_nowait(value)

    async def version(self) -> dict:
        if self.cached_version and time.monotonic() - self.version_at < 2:
            return self.cached_version
        service = getattr(self.owner, "update_service", None)
        running = await self.owner._restart_running_json()
        # Counts belong on the global channel; commands and operation details
        # remain behind the explicit version/detail HTTP request.
        summary = {
            key: value
            for key, value in running.items()
            if key not in {"processes", "operationItems"}
        }
        data = (
            service.snapshot(running=summary)
            if service is not None
            else {"ok": True, "phase": "idle", "running": summary}
        )
        from app import installed_version

        data["version"] = installed_version()
        data["frontend"] = self.owner._frontend_build_info()
        # Do not inspect git/running tasks per keystroke or per streaming token.
        self.cached_version, self.version_at = data, time.monotonic()
        return data

    async def snapshot(self, queue: asyncio.Queue, owner: int, archive: bool):
        async with self.lock:
            cursor, items = await asyncio.to_thread(
                read_catalog, self.owner.db.path, owner, archive, self.reference_length_cache
            )
            status = await self.owner._tree_running_state(owner)
            version = await self.version()
            self.serial += 1
            state = {
                "owner": owner,
                "archive": archive,
                "cursor": cursor,
                "items": items,
                "status": status,
                "version": version,
                "seq": self.serial,
                "overview": self.clients.get(queue, {}).get("overview"),
            }
            if state["overview"]:
                state["overview"]["checkedAt"] = 0.0
                state["overview"]["last"] = None
            self.clients[queue] = state
            self.put(
                queue,
                {
                    "type": "snapshot",
                    "epoch": self.epoch,
                    "seq": self.serial,
                    "items": list(items.values()),
                    "includeArchived": archive,
                    "conversationContentLimit": CONVERSATION_CONTENT_LIMIT,
                    "treeStatus": status,
                    "version": version,
                },
            )

    async def watch_overview(
        self, queue: asyncio.Queue, conversation_uuid: str, subscription_id: str
    ):
        # One visible card per browser; never aggregate every conversation on the
        # tree. Empty id cancels. Ownership is rechecked on every snapshot/update.
        async with self.lock:
            state = self.clients.get(queue)
            if state is None:
                return
            state["overview"] = (
                {
                    "uuid": conversation_uuid,
                    "subscriptionId": subscription_id,
                    "last": None,
                    "checkedAt": 0.0,
                }
                if conversation_uuid
                else None
            )
            await self.refresh_overviews(force=True, only_queue=queue)

    async def refresh_overviews(
        self, *, force: bool = False, only_queue: asyncio.Queue | None = None
    ):
        current = time.monotonic()
        values = {}
        for queue, state in list(self.clients.items()):
            if only_queue is not None and queue is not only_queue:
                continue
            watch = state.get("overview")
            if not watch or (not force and current - watch["checkedAt"] < 1.0):
                continue
            scope = (state["owner"], watch["uuid"])
            if scope not in values:
                try:
                    overview = await self.owner._conversation_overview(*scope)
                    values[scope] = {"overview": overview} if overview else {"error": "not_found"}
                except Exception:
                    # Do not expose exception text, row contents or private data.
                    values[scope] = {"error": "temporarily_unavailable"}
            if self.clients.get(queue) is not state:
                continue
            value = values[scope]
            watch["checkedAt"] = current
            if watch["last"] == value:
                continue
            watch["last"] = value
            self.put(
                queue,
                {
                    "type": "conversation-overview",
                    "conversationUuid": watch["uuid"],
                    "subscriptionId": watch["subscriptionId"],
                    **value,
                },
            )

    async def run(self):
        while not self._stopping:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            # Python 3.11 wait_for can swallow cancellation when Event.wait
            # completes at the same time. The stop state remains authoritative.
            if self._stopping:
                break
            self.wake.clear()
            # Coalesce one transaction/bulk import, not every streamed character.
            await asyncio.sleep(0.06)
            if self._stopping:
                break
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A future wake or the bounded calibration retries; no fabricated
                # empty catalog on a temporary database/read failure.
                for queue in tuple(self.clients):
                    self.put(queue, {"type": "stale"})

    async def refresh(self):
        if not self.clients:
            return
        async with self.lock:
            # Accounting/tool commits wake the existing hub even when the
            # resource-name catalog has not changed. Coalesce only visible cards.
            await self.refresh_overviews()
            cur = await self.owner.db.conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM web_catalog_changes"
            )
            latest = int((await cur.fetchone())[0])
            calibration = time.monotonic() - self.last_calibration >= 2
            if latest == self.last_cursor and not calibration:
                return
            self.last_cursor, self.last_calibration = latest, time.monotonic()
            version = await self.version()
            catalogs, statuses = {}, {}
            for queue, state in list(self.clients.items()):
                owner, archive = state["owner"], state["archive"]
                scope = (owner, archive)
                upserts, removed = [], []
                if latest != state["cursor"] or calibration:
                    if scope not in catalogs:
                        catalogs[scope] = await asyncio.to_thread(
                            read_catalog, self.owner.db.path, owner, archive, self.reference_length_cache
                        )
                    cursor, current = catalogs[scope]
                    upserts = [
                        item for key, item in current.items() if state["items"].get(key) != item
                    ]
                    removed = [key for key in state["items"] if key not in current]
                    state["items"], state["cursor"] = current, cursor
                if owner not in statuses:
                    statuses[owner] = await self.owner._tree_running_state(owner)
                status = statuses[owner]
                # The status endpoint's timestamp is diagnostic, not a change.
                same_status = {
                    k: v
                    for k, v in status.items()
                    if k not in {"ts", "serverTime", "updatedAt", "revision"}
                } == {
                    k: v
                    for k, v in state["status"].items()
                    if k not in {"ts", "serverTime", "updatedAt", "revision"}
                }
                if upserts or removed or not same_status or state["version"] != version:
                    self.serial += 1
                    self.put(
                        queue,
                        {
                            "type": "patch",
                            "epoch": self.epoch,
                            "seq": self.serial,
                            "previousSeq": state["seq"],
                            "upserts": upserts,
                            "conversationContentLimit": CONVERSATION_CONTENT_LIMIT,
                            "removed": removed,
                            **({"treeStatus": status} if not same_status else {}),
                            **({"version": version} if state["version"] != version else {}),
                        },
                    )
                    state.update(seq=self.serial, status=status, version=version)


class WebAdminRealtimeMixin:
    async def _realtime_context(self, app):
        self.global_realtime = GlobalRealtime(self)
        await self.global_realtime.start()
        yield
        await self.global_realtime.close()

    async def _realtime_shutdown(self, app):
        await self.global_realtime.close()

    async def handle_api_reference_catalog(self, request):
        session = request[_WEB_SESSION_KEY]
        archive = request.query.get("archived") == "1"
        cursor, items = await asyncio.to_thread(
            read_catalog, self.db.path, int(session.chat_id), archive
        )
        return web.json_response({"ok": True, "cursor": cursor, "items": list(items.values()), "conversationContentLimit": CONVERSATION_CONTENT_LIMIT})

    async def handle_api_global_ws(self, request):
        session = request[_WEB_SESSION_KEY]
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=8192)
        await ws.prepare(request)
        queue = asyncio.Queue(maxsize=128)
        hub = self.global_realtime
        await hub.snapshot(queue, int(session.chat_id), request.query.get("archived") == "1")

        async def writer():
            while True:
                # Periodic authentication also revokes an already-open channel.
                if await self.session_from_request(request) is None:
                    await ws.close(code=1008, message=b"session expired")
                    return
                try:
                    packet = await asyncio.wait_for(queue.get(), 20)
                except asyncio.TimeoutError:
                    packet = {"type": "ping"}
                await ws.send_json(packet)
                if packet.get("type") == "reconnect":
                    await ws.close(code=1012)
                    return

        task = asyncio.create_task(writer())
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") in {"resync", "scope"}:
                    await hub.snapshot(
                        queue, int(session.chat_id), bool(data.get("includeArchived"))
                    )
                elif data.get("type") == "conversation-overview":
                    conversation_uuid = data.get("conversationUuid", "")
                    subscription_id = data.get("subscriptionId", "")
                    if (
                        isinstance(conversation_uuid, str)
                        and len(conversation_uuid) <= 128
                        and isinstance(subscription_id, str)
                        and len(subscription_id) <= 128
                    ):
                        await hub.watch_overview(queue, conversation_uuid, subscription_id)
                elif data.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
        finally:
            hub.clients.pop(queue, None)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return ws
