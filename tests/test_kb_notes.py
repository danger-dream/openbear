from __future__ import annotations

import io
import zipfile

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from app.db.engine import DB
from app.kb_notes import (
    KBNoteConflict,
    KBNoteError,
    KBNoteReadonly,
    KBNotesDAO,
    import_from_markdown_or_zip,
)
from app.web_console.core import _WEB_SESSION_KEY, WebSession
from app.web_console.kb_notes_api import WebAdminKBNotesMixin

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe5'\xde\xfc\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
async def db(tmp_path):
    database = DB(str(tmp_path / "kb-notes.db"))
    await database.connect()
    yield database
    await database.close()


async def test_kb_notes_tree_cas_readonly_move_and_search(db: DB, tmp_path):
    dao = KBNotesDAO(db, asset_root=tmp_path / "assets")
    owner = 1001

    root = await dao.create_note(owner, title="个人", icon="🏠", content="顶层说明正文")
    child_a = await dao.create_note(
        owner,
        parent_uuid=root["noteUuid"],
        title="WWAN模块知识",
        icon="📄",
        content="## FDD 频段\n\n| Band | Duplex |\n| --- | --- |\n| 1 | FDD |",
    )
    child_b = await dao.create_note(
        owner,
        parent_uuid=root["noteUuid"],
        title="RouterOS配置",
        content="```bash\n/ip route print\n```",
    )
    grandchild = await dao.create_note(
        owner,
        parent_uuid=child_a["noteUuid"],
        title="频段附录",
        content="B28 700MHz 补充测试",
    )

    tree = await dao.list_tree(owner)
    assert tree["total"] == 4
    by_uuid = {item["noteUuid"]: item for item in tree["items"]}
    assert by_uuid[root["noteUuid"]]["childCount"] == 2
    assert by_uuid[child_a["noteUuid"]]["childCount"] == 1
    assert by_uuid[grandchild["noteUuid"]]["path"] == "个人 / WWAN模块知识 / 频段附录"
    assert by_uuid[grandchild["noteUuid"]]["depth"] == 2

    detail = await dao.get_note(owner, grandchild["noteUuid"])
    assert [a["title"] for a in detail["ancestors"]] == ["个人", "WWAN模块知识"]

    # CAS revision check
    updated = await dao.update_note(
        owner,
        child_a["noteUuid"],
        {"content": "## FDD 新版\n\n更新内容"},
        expected_revision=child_a["revision"],
    )
    assert updated["revision"] == child_a["revision"] + 1
    with pytest.raises(KBNoteConflict) as conflict_exc:
        await dao.update_note(
            owner,
            child_a["noteUuid"],
            {"content": "过期写入"},
            expected_revision=child_a["revision"],
        )
    assert conflict_exc.value.extra["currentNote"]["revision"] == updated["revision"]

    # Readonly lock check
    locked = await dao.update_note(owner, child_a["noteUuid"], {"isReadonly": True})
    assert locked["isReadonly"] is True
    with pytest.raises(KBNoteReadonly):
        await dao.update_note(owner, child_a["noteUuid"], {"title": "试图改名"})
    with pytest.raises(KBNoteReadonly):
        await dao.delete_note(owner, child_a["noteUuid"])
    unlocked = await dao.update_note(owner, child_a["noteUuid"], {"isReadonly": False, "title": "WWAN模块知识v2"})
    assert unlocked["isReadonly"] is False
    assert unlocked["title"] == "WWAN模块知识v2"

    # Prevent circular hierarchy moves
    with pytest.raises(KBNoteError) as cycle_exc:
        await dao.move_note(owner, root["noteUuid"], target_parent_uuid=grandchild["noteUuid"])
    assert cycle_exc.value.code == "circular_note_hierarchy"

    # Reorder siblings: move child_b before child_a
    moved_tree = await dao.move_note(
        owner,
        child_b["noteUuid"],
        target_parent_uuid=root["noteUuid"],
        before_uuid=child_a["noteUuid"],
    )
    children_under_root = [
        item["noteUuid"]
        for item in moved_tree["items"]
        if item["parentUuid"] == root["noteUuid"]
    ]
    assert children_under_root == [child_b["noteUuid"], child_a["noteUuid"]]

    # Full-text search across title and body
    hits = await dao.search_notes(owner, "WWAN 700MHz")
    assert len(hits) == 1
    assert hits[0]["noteUuid"] == grandchild["noteUuid"]
    assert "700MHz" in hits[0]["snippet"]

    # Delete with promote_children preserves grandchild under root
    await dao.delete_note(owner, child_a["noteUuid"], mode="promote_children")
    after_promote = await dao.get_note(owner, grandchild["noteUuid"])
    assert after_promote["parentUuid"] == root["noteUuid"]



async def test_kb_notes_markdown_zip_import(db: DB, tmp_path):
    dao = KBNotesDAO(db, asset_root=tmp_path / "assets")
    owner = 1001

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("网络笔记.md", "# 网络笔记\n\n总览内容")
        zf.writestr("网络笔记/Clash规则.md", "---\ntags: [net]\n---\n\n# Clash规则\n\n![pic](assets/clash.png)")
        zf.writestr("网络笔记/assets/clash.png", PNG_1X1)
        zf.writestr("../逃逸.md", "不应导入")
    zip_res = await import_from_markdown_or_zip(
        dao,
        owner,
        files=[("bundle.zip", zbuf.getvalue())],
    )
    assert zip_res["createdCount"] == 2
    assert zip_res["importedAssets"] == 1

    items = {i["title"]: i for i in (await dao.list_tree(owner))["items"]}
    assert set(items) == {"网络笔记", "Clash规则"}
    parent = await dao.get_note(owner, items["网络笔记"]["noteUuid"])
    child = await dao.get_note(owner, items["Clash规则"]["noteUuid"])
    assert parent["content"] == "总览内容"
    assert child["parentUuid"] == parent["noteUuid"]
    # Front matter is kept verbatim; only the image path is rewritten.
    assert child["content"].startswith("---\ntags: [net]\n---\n\n![pic](/api/kb-notes/assets/")


async def test_kb_notes_http_api_capture_upload_and_export(db: DB, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBEAR_KB_NOTE_ASSET_DIR", str(tmp_path / "http_assets"))

    class DummyHost(WebAdminKBNotesMixin):
        def __init__(self, database: DB):
            self.db = database

        async def _conversation_row(self, owner_chat_id: int, conv_uuid: str):
            if conv_uuid == "conv-demo":
                return {"conversation_uuid": "conv-demo", "title": "RouterOS 排障会话"}
            return None

    host = DummyHost(db)

    @web.middleware
    async def fake_auth(request, handler):
        request[_WEB_SESSION_KEY] = WebSession(chat_id=1001, expires_at=9999999999)
        return await handler(request)

    # Same request size limit as the real console application.
    app = web.Application(middlewares=[fake_auth])
    app.router.add_get("/api/kb-notes/tree", host.handle_api_kb_notes_tree)
    app.router.add_post("/api/kb-notes/items", host.handle_api_kb_note_create)
    app.router.add_post("/api/kb-notes/capture", host.handle_api_kb_note_capture)
    app.router.add_get("/api/kb-notes/items/{note_uuid}", host.handle_api_kb_note_detail)
    app.router.add_patch("/api/kb-notes/items/{note_uuid}", host.handle_api_kb_note_update)
    app.router.add_get("/api/kb-notes/items/{note_uuid}/export", host.handle_api_kb_note_export)
    app.router.add_post("/api/kb-notes/assets", host.handle_api_kb_note_asset_upload)
    app.router.add_get("/api/kb-notes/assets/{asset_uuid}/{filename:.*}", host.handle_api_kb_note_asset_get)

    async with TestClient(TestServer(app)) as client:
        # Capture from conversation selection into a new note
        cap_resp = await client.post(
            "/api/kb-notes/capture",
            json={
                "mode": "create",
                "title": "排障摘录",
                "quote": "需要将 MTU 调整为 1420",
                "content": "已在测试路由验证通过",
                "conversationUuid": "conv-demo",
                "turnUuid": "turn-9",
                "turnNumber": 3,
            },
        )
        assert cap_resp.status == 200
        cap_data = await cap_resp.json()
        created_uuid = cap_data["note"]["noteUuid"]
        assert "openbear://ref/turn/conv-demo/turn-9?mode=mention" in cap_data["note"]["content"]
        assert "> 需要将 MTU 调整为 1420" in cap_data["note"]["content"]

        # Upload pasted image asset
        form = FormData()
        form.add_field("noteUuid", created_uuid)
        form.add_field("file", PNG_1X1, filename="paste.png", content_type="image/png")
        up_resp = await client.post("/api/kb-notes/assets", data=form)
        assert up_resp.status == 200
        up_data = await up_resp.json()
        asset_url = up_data["asset"]["url"]

        get_asset_resp = await client.get(asset_url)
        assert get_asset_resp.status == 200
        assert await get_asset_resp.read() == PNG_1X1

        # Export Markdown
        exp_resp = await client.get(f"/api/kb-notes/items/{created_uuid}/export")
        assert exp_resp.status == 200
        exported_md = await exp_resp.text()
        assert exported_md.startswith("# 排障摘录")

        # Capture with opId produces exact message traceability link; a malformed
        # turn number is ignored instead of failing the request.
        cap_op_resp = await client.post(
            "/api/kb-notes/capture",
            json={
                "mode": "append",
                "targetNoteUuid": created_uuid,
                "quote": "wg0 接口握手正常",
                "conversationUuid": "conv-demo",
                "opId": "assistant:turn-9:0",
                "turnNumber": "第四轮",
            },
        )
        assert cap_op_resp.status == 200
        cap_op_data = await cap_op_resp.json()
        assert "openbear://ref/message/conv-demo/assistant%3Aturn-9%3A0?mode=mention" in cap_op_data["note"]["content"]

        # A body above aiohttp's 1 MiB default is saved in full.
        current = (await (await client.get(f"/api/kb-notes/items/{created_uuid}")).json())["note"]
        big = "中文正文" * 100_000
        big_resp = await client.patch(
            f"/api/kb-notes/items/{created_uuid}",
            json={"content": big, "expectedRevision": current["revision"]},
        )
        assert big_resp.status == 200
        big_note = (await big_resp.json())["note"]
        assert big_note["revision"] == current["revision"] + 1
        assert (await (await client.get(f"/api/kb-notes/items/{created_uuid}")).json())["note"]["content"] == big

        # Unreadable or oversized bodies fail explicitly and change nothing.
        bad_resp = await client.patch(
            f"/api/kb-notes/items/{created_uuid}",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert bad_resp.status == 400
        assert (await bad_resp.json())["error"] == "invalid_json"
        with monkeypatch.context() as patched:
            patched.setattr("app.web_console.kb_notes_api._JSON_BODY_LIMIT", 1024)
            large_resp = await client.patch(f"/api/kb-notes/items/{created_uuid}", json={"content": "x" * 4096})
        assert large_resp.status == 413
        assert (await large_resp.json())["error"] == "request_too_large"
        after = (await (await client.get(f"/api/kb-notes/items/{created_uuid}")).json())["note"]
        assert after["revision"] == big_note["revision"] and after["content"] == big

        # Folder export keeps every sibling, even with repeated or unsafe titles.
        folder = (await (await client.post("/api/kb-notes/items", json={"title": "目录"})).json())["note"]
        for title, body in [("同名", "一"), ("同名", "二"), ("..", "三")]:
            resp = await client.post(
                "/api/kb-notes/items",
                json={"parentUuid": folder["noteUuid"], "title": title, "content": body},
            )
            assert resp.status == 200
        zip_resp = await client.get(f"/api/kb-notes/items/{folder['noteUuid']}/export?children=1")
        assert zip_resp.status == 200
        with zipfile.ZipFile(io.BytesIO(await zip_resp.read())) as zf:
            names = zf.namelist()
            bodies = sorted(zf.read(name).decode("utf-8").split("\n\n", 1)[1] for name in names[1:])
        assert len(names) == len(set(names)) == 4
        assert not any(".." in part for name in names for part in name.split("/"))
        assert names[0] == "目录.md"
        assert bodies == ["一", "三", "二"]


async def test_kb_notes_reference_resolution_and_catalog(db: DB):
    from app.references import ReferenceError, ReferenceMaterials
    from app.web_console.realtime import read_catalog

    dao = KBNotesDAO(db)
    owner = 1001
    parent = await dao.create_note(owner, title="个人知识库", icon="📚")
    child = await dao.create_note(
        owner,
        parent_uuid=parent["noteUuid"],
        title="WWAN模块频段",
        content="Band 1 / Band 3 / Band 78 频段参数表",
    )
    nuuid = child["noteUuid"]

    seq, catalog_items = read_catalog(db.path, owner, False, {})
    assert seq > 0
    note_entry = catalog_items.get(f"note:{nuuid}")
    assert note_entry is not None
    assert note_entry["kind"] == "note"
    assert note_entry["label"] == "WWAN模块频段"
    assert note_entry["group"] == "个人知识库"
    assert "Band 1" in note_entry["preview"]

    # Another owner never sees private kb_notes in catalog or reference resolution
    _, other_items = read_catalog(db.path, 9999, False, {})
    assert f"note:{nuuid}" not in other_items

    ref_svc = ReferenceMaterials(db)
    resolved = await ref_svc.resolve(f"请参考 [WWAN模块频段](openbear://ref/note/{nuuid})", owner=owner)
    assert len(resolved["materials"]) == 1
    assert resolved["materials"][0]["content"] == "Band 1 / Band 3 / Band 78 频段参数表"

    # The model has no tool to read a note, so a locator-only note is rejected.
    with pytest.raises(ReferenceError) as mention_exc:
        await ref_svc.resolve(f"[目录](openbear://ref/note/{parent['noteUuid']}?mode=mention)", owner=owner)
    assert mention_exc.value.code == "invalid_reference_mode"

    with pytest.raises(ReferenceError):
        await ref_svc.resolve(f"[WWAN模块频段](openbear://ref/note/{nuuid})", owner=9999)
