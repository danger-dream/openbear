from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.dao import MessageDAO
from app.web_console.conversation_prompt import system_prompt_sha256
from tests.test_builtin_template_import import _login_cookie, web_env  # noqa: F401
from tests.test_conversation_tree import _conversation, _folder


@pytest.fixture
async def prompt_env(web_env):  # noqa: F811 - imported pytest fixture
    h = web_env.server
    await _folder(h, "project", "", "Project", "/project", "folder instructions")
    uuid, chat_id = await _conversation(h, 1, "project")
    other_uuid, other_chat = await _conversation(h, 2)
    await h.db.conn.execute("UPDATE web_conversations SET owner_chat_id=123")
    await h.db.conn.execute("UPDATE web_conversation_folders SET owner_chat_id=123")
    await h.db.conn.execute(
        "INSERT INTO memory_templates(name,content,is_active,updated_at) VALUES ('main',?,1,1)",
        ("Policy v2\n[[ folderWorkspaceDir ]]\n[[ folderPrompt ]]",),
    )
    await h.db.conn.execute("INSERT INTO messages(chat_id,role,content) VALUES (?,'user','keep history')", (chat_id,))
    await h.db.conn.execute("INSERT INTO summaries(chat_id,summary) VALUES (?,'keep summary')", (chat_id,))
    await h.db.conn.executemany(
        "INSERT INTO controller_model_contexts(chat_id,conversation_uuid,state_json,revision,created_at,updated_at) VALUES (?,?,'{}',1,1,1)",
        [(chat_id, uuid), (other_chat, other_uuid)],
    )
    await h.db.conn.commit()
    cookies = await _login_cookie(web_env)
    web_env.client.session.cookie_jar.clear()
    return SimpleNamespace(**vars(web_env), uuid=uuid, chat_id=chat_id, other_chat=other_chat,
                           cookies=cookies, url=f"/api/conversations/{uuid}/system-prompt")


async def snapshot(env):
    return await MessageDAO(env.db).get_system_snapshot(env.chat_id)


async def preview(env):
    response = await env.client.post(env.url + "/preview", cookies=env.cookies)
    assert response.status == 200, await response.text()
    return await response.json()


def confirmation(data):
    return {"confirmed": True, "beforeHash": data["beforeHash"], "afterHash": data["afterHash"]}


async def test_refresh_is_authenticated_owned_and_not_a_draft_create(prompt_env):
    e = prompt_env
    assert (await e.client.post(e.url + "/preview")).status == 401
    assert (await e.client.put(e.url, json={})).status == 401
    await e.db.conn.execute("UPDATE web_conversations SET owner_chat_id=456 WHERE conversation_uuid=?", (e.uuid,))
    await e.db.conn.commit()
    assert (await e.client.post(e.url + "/preview", cookies=e.cookies)).status == 404
    assert (await e.client.put(e.url, cookies=e.cookies, json={})).status == 404
    assert (await e.client.post("/api/conversations/local:new/system-prompt/preview", cookies=e.cookies)).status == 404
    assert await snapshot(e) == "old:conversation-1"


async def test_real_render_preview_then_refresh_preserves_history_and_updates_effective_snapshot(prompt_env):
    e = prompt_env
    dao = MessageDAO(e.db)
    candidate = await e.server._build_system_prompt_for_chat(e.uuid, strict=True)
    assert "/project" in candidate and "folder instructions" in candidate
    # A successful live render alone does NOT replace the effective system.
    assert await dao.get_or_set_system_snapshot(e.chat_id, candidate) == "old:conversation-1"
    response = await e.client.post(e.url + "/preview", cookies=e.cookies)
    p = await response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert p["changed"] and "+Policy v2" in p["diff"]
    assert p["afterHash"] == system_prompt_sha256(candidate)
    assert await snapshot(e) == "old:conversation-1"
    response = await e.client.put(e.url, cookies=e.cookies, json=confirmation(p))
    assert response.status == 200
    assert (await response.json())["updated"] is True
    # This is the same DAO selector used by the main-controller run. A later
    # live render cannot undo the newly adopted frozen prompt.
    assert await dao.get_or_set_system_snapshot(e.chat_id, "later candidate") == candidate
    for table, column, expected in [("messages", "content", "keep history"), ("summaries", "summary", "keep summary")]:
        cur = await e.db.conn.execute(f"SELECT {column} FROM {table} WHERE chat_id=?", (e.chat_id,))
        assert (await cur.fetchone())[column] == expected
    cur = await e.db.conn.execute("SELECT chat_id FROM controller_model_contexts")
    assert [r["chat_id"] for r in await cur.fetchall()] == [e.other_chat]
    assert await dao.get_system_snapshot(e.other_chat) == "old:conversation-2"
    cur = await e.db.conn.execute("SELECT folder_uuid FROM web_conversations WHERE conversation_uuid=?", (e.uuid,))
    assert (await cur.fetchone())["folder_uuid"] == "project"


@pytest.mark.parametrize("body", [{}, {"confirmed": False}, {"confirmed": True, "beforeHash": "bad", "afterHash": "bad"}])
async def test_update_requires_explicit_reviewed_hashes(prompt_env, body):
    e = prompt_env
    response = await e.client.put(e.url, cookies=e.cookies, json=body)
    assert response.status == 400
    assert await snapshot(e) == "old:conversation-1"


async def test_changed_template_requires_repreview_not_silent_upgrade(prompt_env):
    e = prompt_env
    p = await preview(e)
    await e.db.conn.execute("UPDATE memory_templates SET content='unreviewed replacement' WHERE is_active=1")
    await e.db.conn.commit()
    response = await e.client.put(e.url, cookies=e.cookies, json=confirmation(p))
    assert response.status == 409
    assert (await response.json())["error"] == "prompt_preview_changed"
    assert await snapshot(e) == "old:conversation-1"


async def test_changed_old_snapshot_requires_repreview(prompt_env):
    e = prompt_env
    p = await preview(e)
    await e.db.conn.execute("UPDATE sessions SET system_snapshot='other editor changed it' WHERE chat_id=?", (e.chat_id,))
    await e.db.conn.commit()
    response = await e.client.put(e.url, cookies=e.cookies, json=confirmation(p))
    assert response.status == 409
    assert await snapshot(e) == "other editor changed it"


async def test_repeat_update_is_noop_and_does_not_clear_new_provider_state(prompt_env):
    e = prompt_env
    p = await preview(e)
    assert (await e.client.put(e.url, cookies=e.cookies, json=confirmation(p))).status == 200
    await e.db.conn.execute(
        "INSERT INTO controller_model_contexts(chat_id,conversation_uuid,state_json,revision,created_at,updated_at) VALUES (?,?,'new-state',1,1,1)",
        (e.chat_id, e.uuid),
    )
    await e.db.conn.commit()
    response = await e.client.put(e.url, cookies=e.cookies, json=confirmation(p))
    assert response.status == 200 and (await response.json())["updated"] is False
    cur = await e.db.conn.execute("SELECT state_json FROM controller_model_contexts WHERE chat_id=?", (e.chat_id,))
    assert (await cur.fetchone())["state_json"] == "new-state"
    assert not (await preview(e))["changed"]


@pytest.mark.parametrize("busy_kind", ["lock", "starting", "runtime"])
async def test_busy_target_rejected_and_never_deferred(prompt_env, busy_kind, monkeypatch):
    e = prompt_env
    p = await preview(e)
    if busy_kind == "starting":
        e.server._web_starting_turns[e.uuid] = {"start"}
    elif busy_kind == "runtime":
        async def busy(_row):
            return True
        monkeypatch.setattr(e.server, "_web_conversation_has_active_runtime", busy)
    async def assert_busy():
        for method, url, body in [(e.client.post, e.url + "/preview", {}), (e.client.put, e.url, confirmation(p))]:
            response = await method(url, cookies=e.cookies, json=body)
            assert response.status == 409
            assert (await response.json())["error"] == "busy"
    if busy_kind == "lock":
        async with e.server.operation_locks.chat(e.chat_id, "test_run"):
            await assert_busy()
    else:
        await assert_busy()
    assert await snapshot(e) == "old:conversation-1"


async def test_runtime_start_during_render_is_rechecked_before_write(prompt_env, monkeypatch):
    e = prompt_env
    p = await preview(e)
    original = e.server._build_system_prompt_for_chat
    async def late_runtime(*args, **kwargs):
        candidate = await original(*args, **kwargs)
        e.server._web_starting_turns[e.uuid] = {"late-start"}
        return candidate
    monkeypatch.setattr(e.server, "_build_system_prompt_for_chat", late_runtime)
    response = await e.client.put(e.url, cookies=e.cookies, json=confirmation(p))
    assert response.status == 409
    assert await snapshot(e) == "old:conversation-1"


@pytest.mark.parametrize("failure", ["exception", "empty", "marker"])
async def test_render_failure_never_installs_fallback_or_exposes_private_error(prompt_env, failure, monkeypatch):
    e = prompt_env
    p = await preview(e)
    async def fail(*args, **kwargs):
        if failure == "exception":
            raise ValueError("private template detail")
        return "" if failure == "empty" else "[[ERROR: private template detail]]"
    monkeypatch.setattr(e.server, "_build_system_prompt_for_chat", fail)
    response = await e.client.put(e.url, cookies=e.cookies, json=confirmation(p))
    assert response.status == 400 and "private template detail" not in await response.text()
    assert await snapshot(e) == "old:conversation-1"


async def test_snapshot_and_continuation_clear_are_atomic(prompt_env):
    e = prompt_env
    p = await preview(e)
    await e.db.conn.execute("CREATE TEMP TRIGGER fail_context_delete BEFORE DELETE ON controller_model_contexts BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
    await e.db.conn.commit()
    response = await e.client.put(e.url, cookies=e.cookies, json=confirmation(p))
    assert response.status == 500
    assert await snapshot(e) == "old:conversation-1"
    cur = await e.db.conn.execute("SELECT 1 FROM controller_model_contexts WHERE chat_id=?", (e.chat_id,))
    assert await cur.fetchone() is not None
