from __future__ import annotations

import json
import sqlite3

import pytest

from app.db.engine import DB
from tests.test_folder_run_defaults import CHEAP, PAID, baseline, defaults, folder, save
from tests.test_web_admin import web_env as _shared_web_env

web_env = _shared_web_env
TEMP = "__temporary"
URL = f"/api/conversation-folders/{TEMP}/properties"


async def create(env, folder_id="", **fields):
    response = await env.client.post("/api/conversations", json={"folderId": folder_id, **fields})
    assert response.status == 200, await response.text()
    return await response.json()


async def test_temporary_defaults_are_independent_sparse_and_do_not_become_project_ancestors(web_env):
    env = web_env
    fallback = await baseline(env)
    project = await folder(env, "project")
    child = await folder(env, "child", project)
    # A GET does not seed a temporary record or change the organization tree.
    data = await (await env.client.get(URL)).json()
    assert data["systemNode"] == "temporary" and data["name"] == "临时会话"
    assert data["runDefaults"]["local"] == data["runDefaults"]["inherited"] == {}
    assert data["prompt"]["local"] == ""
    assert (await (await env.db.conn.execute("SELECT COUNT(*) AS n FROM web_temporary_conversation_properties")).fetchone())["n"] == 0
    await save(env, TEMP, PAID, promptMarkdown="temporary only")
    assert (await defaults(env))["folderDefaults"] == PAID
    for target in (project, child):
        assert (await defaults(env, target))["defaults"] == fallback
        properties = await (await env.client.get(f"/api/conversation-folders/{target}/properties")).json()
        assert properties["prompt"]["effective"] == ""
        assert properties["runDefaults"]["inherited"] == {}
    await save(env, TEMP, {"mainFastMode": False, "agentModel": "", "agentFastMode": None})
    values = (await (await env.client.get(URL)).json())["runDefaults"]
    assert values["local"] == {"mainFastMode": False, "agentModel": "", "agentFastMode": None}
    assert values["sources"]["mainFastMode"] == {"folderId": TEMP, "path": "临时会话"}
    assert values["resolved"]["mainModel"] == CHEAP["mainModel"]
    assert values["resolved"]["agentFastMode"] is None
    await save(env, TEMP, {})
    assert (await defaults(env))["defaults"] == fallback
    assert (await (await env.client.get(URL)).json())["prompt"]["local"] == "temporary only"
    assert TEMP not in await env.server._tree_folders(123, include_properties=True)
    assert TEMP not in await env.server._tree_run_default_folders(123)


async def test_new_temporary_conversation_uses_all_six_fields_without_mutating_global_or_existing(web_env):
    env = web_env
    fallback = await baseline(env)
    existing = await create(env)
    old_uuid = existing["conversation"]["conversationUuid"]
    old_row = await env.server._conversation_row(123, old_uuid)
    await save(env, TEMP, PAID)
    for body in ({}, {"runConfig": PAID}):
        created = await create(env, **body)
        state = created["state"]
        assert state["model"] == PAID["mainModel"]
        assert state["thinkingLevel"] == "high" and state["fastMode"] is True
        for key, expected in (("model", "openai/gpt"), ("thinkLevel", "high"), ("fastMode", True)):
            assert state["agentRunConfig"][key] == expected
        row = await env.server._conversation_row(123, created["conversation"]["conversationUuid"])
        assert not row["folder_uuid"]
    manual = await create(env, runConfig=CHEAP)
    assert manual["state"]["model"] == "openai/cheap"
    _row, remembered = await env.server._web_run_defaults_candidate(123)
    assert remembered == fallback
    assert await env.server._conversation_row(123, old_uuid) == old_row
    assert (await defaults(env))["folderDefaults"] == PAID


async def test_temporary_prompt_injection_and_optional_snapshot_updates_stay_scoped(web_env):
    env = web_env
    await baseline(env)
    project = await folder(env, "project")
    await save(env, TEMP, {}, promptMarkdown="temporary initial")
    idle, busy, archived, project_conv = [await create(env, target) for target in ("", "", "", project)]
    records = [await env.server._conversation_row(123, item["conversation"]["conversationUuid"]) for item in (idle, busy, archived, project_conv)]
    for index, row in enumerate(records):
        await env.db.conn.execute("UPDATE sessions SET system_snapshot=? WHERE chat_id=?", (f"keep-{index}", row["internal_chat_id"]))
    await env.db.conn.execute("UPDATE web_conversations SET archived_at=123 WHERE conversation_uuid=?", (records[2]["conversation_uuid"],))
    await env.db.conn.commit()
    params = await env.server._prompt_template_params_live(records[0]["conversation_uuid"])
    assert params["folderPrompt"] == "temporary initial"
    assert params["folderWorkspaceDir"] == str(env.server.workspace_dir)
    project_params = await env.server._prompt_template_params_live(records[3]["conversation_uuid"])
    assert project_params["folderPrompt"] == ""
    assert "temporary initial" in await env.server._build_system_prompt_for_chat(records[0]["conversation_uuid"], strict=True)
    assert "temporary initial" not in await env.server._build_system_prompt_for_chat(records[3]["conversation_uuid"], strict=True)
    previous_runs = env.server.runs

    class BusyRuns:
        def is_running(self, chat_id):
            return chat_id == records[1]["internal_chat_id"]

    env.server.runs = BusyRuns()
    try:
        response = await env.client.post(URL + "/impact", json={"promptMarkdown": "temporary next"})
        impact = await response.json()
        assert impact["affectedCount"] == 3 and impact["runningCount"] == 1 and impact["archivedCount"] == 1
        assert (await (await env.client.get(URL)).json())["prompt"]["local"] == "temporary initial"
        # Rendering consumes the proposed context rather than the old stored row.
        async def render(conversation_uuid="", *, folder_values=None, strict=False):
            return f"rendered:{folder_values[1]}"
        env.server._build_system_prompt_for_chat = render
        saved = await env.client.put(URL, json={"promptMarkdown": "temporary next", "updateSnapshots": True})
        result = await saved.json()
        assert result["updatedCount"] == 2 and result["skippedRunningCount"] == 1
        snapshots = []
        for row in records:
            current = await (await env.db.conn.execute("SELECT system_snapshot FROM sessions WHERE chat_id=?", (row["internal_chat_id"],))).fetchone()
            snapshots.append(current["system_snapshot"])
        assert snapshots == ["rendered:temporary next", "keep-1", "rendered:temporary next", "keep-3"]
        # Clearing removes only the temporary injection; no implicit workspace edit.
        saved = await env.client.put(URL, json={"promptMarkdown": "", "updateSnapshots": False})
        assert (await saved.json())["updatedCount"] == 0
        assert await env.server._tree_effective_folder_values(123, "") == (str(env.server.workspace_dir), "")
    finally:
        env.server.runs = previous_runs


async def test_moving_between_project_and_temporary_context_uses_correct_target(web_env):
    env = web_env
    await baseline(env)
    project = await folder(env, "project")
    await save(env, project, {}, workspaceDir="/project", promptMarkdown="project prompt")
    await save(env, TEMP, {}, promptMarkdown="temporary prompt")
    temporary = await create(env)
    project_conv = await create(env, project)
    for item, target, expected in (
        (temporary, project, ("/project", "project prompt")),
        (project_conv, "", (str(env.server.workspace_dir), "temporary prompt")),
    ):
        impact = await env.server._tree_change_impact(123, kind="conversation", item_id=item["conversation"]["conversationUuid"], target_folder_id=target)
        assert impact["affectedCount"] == 1 and impact["rows"][0]["new_values"] == expected


async def test_deleting_folder_into_temporary_group_applies_context_only_to_direct_conversations(web_env):
    env = web_env
    await baseline(env)
    project = await folder(env, "delete me")
    child = await folder(env, "keep child", project)
    await save(env, project, {}, promptMarkdown="project context")
    await save(env, TEMP, {}, promptMarkdown="temporary context")
    direct = await create(env, project)
    nested = await create(env, child)
    url = f"/api/conversation-folders/{project}/delete"
    payload = {"targetFolderId": "", "updateSnapshots": True}
    preview = await env.client.post(url, json={**payload, "impactOnly": True})
    assert (await preview.json())["affectedCount"] == 2
    rendered = {}

    async def render(conversation_uuid="", *, folder_values=None, strict=False):
        rendered[conversation_uuid] = folder_values
        return f"rendered:{folder_values[1]}"

    env.server._build_system_prompt_for_chat = render
    response = await env.client.post(url, json=payload)
    assert response.status == 200, await response.text()
    assert (await response.json())["updatedCount"] == 2
    assert rendered[direct["conversation"]["conversationUuid"]] == (str(env.server.workspace_dir), "temporary context")
    assert rendered[nested["conversation"]["conversationUuid"]] == (str(env.server.workspace_dir), "")
    assert (await env.server._tree_folders(123))[child]["parent_uuid"] == ""


@pytest.mark.parametrize("body", [{"workspaceDir": "/forbidden"}, {"workspaceDir": ""}, {"runDefaults": {"mainFastMode": "false"}}, {"runDefaults": {"unexpected": 1}}])
async def test_invalid_temporary_settings_are_rejected_without_partial_writes(web_env, body):
    env = web_env
    await baseline(env)
    await save(env, TEMP, PAID, promptMarkdown="preserve")
    for url, method in ((URL + "/impact", env.client.post), (URL, env.client.put)):
        response = await method(url, json={"promptMarkdown": "must not save", **body})
        assert response.status == 400, await response.text()
    data = await (await env.client.get(URL)).json()
    assert data["runDefaults"]["local"] == PAID and data["prompt"]["local"] == "preserve"


async def test_temporary_properties_are_owner_scoped_and_not_a_real_folder(web_env):
    env = web_env
    await baseline(env)
    await save(env, TEMP, PAID, promptMarkdown="owner 123")
    await env.db.conn.execute("UPDATE web_temporary_conversation_properties SET owner_chat_id=456 WHERE owner_chat_id=123")
    await env.db.conn.commit()
    data = await (await env.client.get(URL)).json()
    assert data["prompt"]["local"] == "" and data["runDefaults"]["local"] == {}
    assert await env.server._tree_folder_run_defaults(456, "") == PAID
    await save(env, TEMP, CHEAP, promptMarkdown="owner 123 new")
    foreign = await env.server._tree_temporary_properties(456)
    assert foreign["prompt_markdown"] == "owner 123"
    assert not await env.server._tree_folder_owned(123, TEMP)
    assert (await env.client.post("/api/conversations", json={"folderId": TEMP})).status == 404
    assert (await env.client.post("/api/conversation-folders", json={"name": "no child", "parentId": TEMP})).status == 404
    # Clients unable to load models can still save context without erasing defaults.
    response = await env.client.put(URL, json={"promptMarkdown": "updated without models"})
    assert response.status == 200
    assert (await defaults(env))["folderDefaults"] == CHEAP


async def test_schema_adds_temporary_properties_to_existing_database_and_preserves_on_reopen(tmp_path):
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('keep')")
    db = DB(str(path))
    await db.connect()
    try:
        columns = await (await db.conn.execute("PRAGMA table_info(web_temporary_conversation_properties)")).fetchall()
        assert {row["name"] for row in columns} == {"owner_chat_id", "prompt_markdown", "run_defaults_json", "updated_at"}
        await db.conn.execute("INSERT INTO web_temporary_conversation_properties VALUES (123, 'persist', ?, 1)", (json.dumps(PAID),))
        await db.conn.commit()
    finally:
        await db.close()
    await db.connect()
    try:
        row = await (await db.conn.execute("SELECT * FROM web_temporary_conversation_properties")).fetchone()
        assert row["prompt_markdown"] == "persist" and json.loads(row["run_defaults_json"]) == PAID
        assert (await (await db.conn.execute("SELECT value FROM sentinel")).fetchone())["value"] == "keep"
    finally:
        await db.close()
