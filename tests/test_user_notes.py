from __future__ import annotations

import json
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.admin.settings import safe_settings_payload, settings_specs_payload
from app.config import UIConfig
from app.db.engine import DB
from app.notes import NoteError, NoteNotFound, UserNotesDAO
from app.settings.specs import get_spec
from app.web_console.core import _WEB_SESSION_KEY, WebSession
from app.web_console.notes_api import WebAdminNotesMixin


@pytest.fixture
async def db(tmp_path):
    database = DB(str(tmp_path / "user-notes.db"))
    await database.connect()
    yield database
    await database.close()


async def seed_conversation(db: DB, owner_id: int, conv_uuid: str, title: str, internal_chat_id: int) -> None:
    async with db.write_transaction(label="seed_conv") as conn:
        await conn.execute(
            """
            INSERT INTO web_conversations (
                conversation_uuid, owner_chat_id, internal_chat_id, title, status, current_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'idle', '就绪', 1000, 1000)
            """,
            (conv_uuid, owner_id, internal_chat_id, title),
        )


def test_notes_ui_settings_defaults_and_specs():
    ui = UIConfig()
    assert ui.notes_enabled is True
    assert ui.notes_selection_popover is True

    enabled_spec = get_spec("ui.notesEnabled")
    popover_spec = get_spec("ui.notesSelectionPopover")
    assert enabled_spec is not None and enabled_spec.group == "notes" and enabled_spec.effect == "立即生效"
    assert popover_spec is not None and popover_spec.group == "notes" and popover_spec.effect == "立即生效"

    for managed in (False, True):
        payload = settings_specs_payload(managed=managed)
        assert "ui.notesEnabled" in payload["specs"]
        assert "ui.notesSelectionPopover" in payload["specs"]

    safe = safe_settings_payload({"ui": ui.model_dump(by_alias=True)})
    assert safe["values"]["ui.notesEnabled"] is True
    assert safe["values"]["ui.notesSelectionPopover"] is True


async def test_user_notes_dao_session_and_global_sheets_lifecycle(db: DB):
    await seed_conversation(db, 1001, "conv-a", "四层透传方案", -101)
    await seed_conversation(db, 2002, "conv-other", "他人会话", -202)
    dao = UserNotesDAO(db)

    initial = await dao.bootstrap(1001, "conv-a")
    assert initial["sessionNotes"] == []
    assert len(initial["globalSheets"]) == 1
    default_sheet = initial["globalSheets"][0]
    assert default_sheet["title"] == "默认便签"

    s_note = await dao.create_note(
        1001,
        scope="session",
        conversation_uuid="conv-a",
        turn_uuid="turn-6",
        turn_number=6,
        op_id="op-ans-6",
        quote="proxy_protocol on;",
        content="本机 1Panel 收敛证书",
    )
    assert s_note["scope"] == "session"
    assert s_note["opId"] == "op-ans-6"
    assert s_note["turnNumber"] == 6

    # Owner isolation: cannot write session note to another user's conversation.
    with pytest.raises(NoteNotFound):
        await dao.create_note(1001, scope="session", conversation_uuid="conv-other", content="越权")

    copied = await dao.copy_to_global(1001, s_note["noteUuid"])
    assert copied["scope"] == "global"
    assert copied["sheetUuid"] == default_sheet["sheetUuid"]
    assert copied["conversationUuid"] == "conv-a"
    assert copied["conversationTitle"] == "四层透传方案"

    sheet2 = await dao.create_sheet(1001, "发版检查")
    renamed = await dao.update_sheet(1001, sheet2["sheetUuid"], "发版备忘")
    assert renamed["title"] == "发版备忘"

    moved = await dao.update_note(
        1001,
        copied["noteUuid"],
        {"content": "跨会话保留配置", "sheetUuid": sheet2["sheetUuid"]},
    )
    assert moved["content"] == "跨会话保留配置"
    assert moved["sheetUuid"] == sheet2["sheetUuid"]

    # Deleting the conversation cleans session notes but preserves global notes!
    removed = await dao.hard_delete_conversation("conv-a")
    assert removed == 1
    async with db.write_transaction(label="del_conv") as conn:
        await conn.execute("DELETE FROM web_conversations WHERE conversation_uuid='conv-a'")

    after_del = await dao.bootstrap(1001, "conv-a")
    assert after_del["sessionNotes"] == []
    sheet2_loaded = next(s for s in after_del["globalSheets"] if s["sheetUuid"] == sheet2["sheetUuid"])
    assert len(sheet2_loaded["notes"]) == 1
    assert sheet2_loaded["notes"][0]["conversationExists"] is False
    assert sheet2_loaded["notes"][0]["conversationTitle"] == "四层透传方案"


class _DummyNotesApp(WebAdminNotesMixin):
    def __init__(self, db: DB) -> None:
        self.db = db

    async def _json_body(self, request: web.Request) -> dict:
        raw = await request.text()
        return json.loads(raw) if raw.strip() else {}


async def test_notes_http_api_routes(db: DB):
    await seed_conversation(db, 42, "conv-http", "API 会话", -420)
    handler = _DummyNotesApp(db)

    @web.middleware
    async def fake_auth(request: web.Request, handler):
        request[_WEB_SESSION_KEY] = WebSession(chat_id=42, expires_at=999999)
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    app.add_routes([
        web.get("/api/notes", handler.handle_api_notes),
        web.post("/api/notes", handler.handle_api_note_create),
        web.patch("/api/notes/{note_uuid}", handler.handle_api_note_update),
        web.post("/api/notes/{note_uuid}/copy-global", handler.handle_api_note_copy_global),
        web.delete("/api/notes/{note_uuid}", handler.handle_api_note_delete),
        web.post("/api/notes/sheets", handler.handle_api_note_sheet_create),
        web.patch("/api/notes/sheets/{sheet_uuid}", handler.handle_api_note_sheet_update),
        web.delete("/api/notes/sheets/{sheet_uuid}", handler.handle_api_note_sheet_delete),
    ])

    async with TestClient(TestServer(app)) as client:
        res = await client.post("/api/notes", json={"scope": "session", "conversationUuid": "conv-http", "content": "第一条"})
        assert res.status == 200
        created = (await res.json())["note"]

        res = await client.post("/api/notes", json={"scope": "session", "conversationUuid": "conv-http", "content": "   "})
        assert res.status == 400

        res = await client.patch(f"/api/notes/{created['noteUuid']}", json={"content": "已更新"})
        assert res.status == 200
        assert (await res.json())["note"]["content"] == "已更新"

        res = await client.get("/api/notes", params={"conversationUuid": "conv-http"})
        data = await res.json()
        assert data["ok"] is True
        assert [n["content"] for n in data["sessionNotes"]] == ["已更新"]

        res = await client.delete(f"/api/notes/{created['noteUuid']}")
        assert res.status == 200
