from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.db.engine import DB
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as _shared_web_env

web_env = _shared_web_env
FIELDS = ("mainModel", "mainThinkingLevel", "mainFastMode", "agentModel", "agentThinkLevel", "agentFastMode")
CHEAP = dict(zip(FIELDS, ("openai/cheap", "low", False, "openai/cheap", "low", False)))
PAID = dict(zip(FIELDS, ("openai/gpt", "high", True, "openai/gpt", "high", True)))


async def folder(env, name, parent="", defaults=None):
    response = await env.client.post("/api/conversation-folders", json={"name": name, "parentId": parent})
    assert response.status == 200, await response.text()
    folder_id = (await response.json())["folder"]["folderId"]
    if defaults is not None:
        await save(env, folder_id, defaults)
    return folder_id


async def save(env, folder_id, defaults, **fields):
    response = await env.client.put(f"/api/conversation-folders/{folder_id}/properties", json={"runDefaults": defaults, **fields})
    assert response.status == 200, await response.text()
    return await response.json()


async def defaults(env, folder_id=""):
    response = await env.client.get("/api/conversations/defaults", params={"folderId": folder_id})
    assert response.status == 200, await response.text()
    return await response.json()


async def baseline(env):
    await _login_cookie(env)
    response = await env.client.patch("/api/conversations/defaults", json=CHEAP)
    assert response.status == 200, await response.text()
    return (await response.json())["defaults"]


async def test_independent_nearest_inheritance_and_explicit_follow_values(web_env):
    env = web_env
    fallback = await baseline(env)
    parent = await folder(env, "paid", defaults=PAID)
    middle = await folder(env, "middle", parent, {"mainThinkingLevel": "medium", "agentThinkLevel": ""})
    child = await folder(env, "child", middle, {"mainFastMode": False, "agentModel": "", "agentFastMode": None})
    data = await defaults(env, child)
    expected = {**PAID, "mainThinkingLevel": "medium", "mainFastMode": False, "agentModel": "", "agentThinkLevel": "", "agentFastMode": None}
    assert {key: data["defaults"][key] for key in FIELDS} == expected
    assert data["folderDefaults"] == expected
    response = await env.client.get(f"/api/conversation-folders/{child}/properties")
    values = (await response.json())["runDefaults"]
    assert values["local"] == {"mainFastMode": False, "agentModel": "", "agentFastMode": None}
    assert values["sources"]["mainModel"] == {"folderId": parent, "path": "paid"}
    assert values["sources"]["mainThinkingLevel"] == {"folderId": middle, "path": "paid / middle"}
    assert values["sources"]["mainFastMode"]["folderId"] == child
    assert values["fallback"] == fallback
    await save(env, child, {})
    cleared = (await defaults(env, child))["defaults"]
    assert cleared["mainFastMode"] is True
    assert cleared["agentModel"] == "openai/gpt"
    assert cleared["agentFastMode"] is True
    assert (await defaults(env))["defaults"] == fallback


async def test_creating_project_conversation_uses_folder_values_without_polluting_temporary_defaults(web_env):
    env = web_env
    fallback = await baseline(env)
    project = await folder(env, "project", defaults=PAID)
    for body in ({"folderId": project}, {"folderId": project, "runConfig": PAID}):
        response = await env.client.post("/api/conversations", json=body)
        assert response.status == 200, await response.text()
        state = (await response.json())["state"]
        assert state["model"] == "openai/gpt"
        assert state["thinkingLevel"] == "high" and state["fastMode"] is True
        assert state["agentRunConfig"]["model"] == "openai/gpt"
        assert state["agentRunConfig"]["thinkLevel"] == "high"
        assert state["agentRunConfig"]["fastMode"] is True
    assert (await defaults(env))["defaults"] == fallback
    temporary = await env.client.post("/api/conversations", json={})
    assert (await temporary.json())["state"]["model"] == "openai/cheap"
    # Defaults are not a policy lock: an explicit draft selection still wins.
    manual = await env.client.post("/api/conversations", json={"folderId": project, "runConfig": CHEAP})
    assert manual.status == 200
    assert (await manual.json())["state"]["model"] == "openai/cheap"
    assert (await defaults(env))["defaults"] == fallback


async def test_first_project_creation_seeds_previous_fallback_not_the_project_model(web_env):
    env = web_env
    env.server.config.models.primary = "openai/cheap"
    await _login_cookie(env)
    project = await folder(env, "first project", defaults=PAID)
    assert (await (await env.db.conn.execute("SELECT COUNT(*) AS n FROM web_conversation_defaults")).fetchone())["n"] == 0
    created = await env.client.post("/api/conversations", json={"folderId": project})
    assert created.status == 200, await created.text()
    assert (await created.json())["state"]["model"] == "openai/gpt"
    assert (await defaults(env))["defaults"]["mainModel"] == "openai/cheap"


async def test_model_only_overrides_normalize_inherited_capabilities_without_freezing_fields(web_env):
    env = web_env
    await baseline(env)
    parent = await folder(env, "parent", defaults=PAID)
    child = await folder(env, "child", parent, {"mainModel": "openai/cheap", "agentModel": ""})
    response = await env.client.get(f"/api/conversation-folders/{child}/properties")
    values = (await response.json())["runDefaults"]
    assert values["local"] == {"mainModel": "openai/cheap", "agentModel": ""}
    assert values["effective"]["mainFastMode"] is True
    assert values["resolved"]["mainFastMode"] is False
    assert values["resolved"]["mainThinkingLevel"] == "low"
    assert values["resolved"]["agentFastMode"] is None
    assert values["resolved"]["agentThinkLevel"] == ""
    # The properties preview must describe the actual new-conversation settings,
    # not the incompatible raw values inherited from the paid parent.
    created = await env.client.post("/api/conversations", json={"folderId": child})
    assert created.status == 200, await created.text()
    state = (await created.json())["state"]
    assert state["model"] == "openai/cheap"
    assert state["thinkingLevel"] == "low" and state["fastMode"] is False
    assert state["agentRunConfig"]["thinkLevel"] == ""
    assert state["agentRunConfig"]["fastMode"] is None


@pytest.mark.parametrize("bad", [
    None, [], {"unexpected": "x"}, {"mainModel": ""}, {"mainModel": "missing/model"},
    {"mainFastMode": None}, {"mainFastMode": "false"}, {"agentFastMode": 1},
    {"mainModel": "openai/cheap", "mainFastMode": True}, {"mainThinkingLevel": "off"},
    {"agentModel": "openai/cheap", "agentThinkLevel": "high"},
])
async def test_invalid_folder_defaults_are_rejected_without_modifying_other_properties(web_env, bad):
    env = web_env
    await baseline(env)
    project = await folder(env, "valid", defaults=PAID)
    await save(env, project, PAID, workspaceDir="/project", promptMarkdown="keep this prompt")
    url = f"/api/conversation-folders/{project}/properties"
    for target in (url + "/impact", url):
        method = env.client.post if target.endswith("/impact") else env.client.put
        response = await method(target, json={"runDefaults": bad})
        assert response.status in {400, 404}, await response.text()
    response = await env.client.get(url)
    data = await response.json()
    assert data["runDefaults"]["local"] == PAID
    assert data["workspace"]["local"] == "/project" and data["prompt"]["local"] == "keep this prompt"


async def test_defaults_changes_only_affect_new_conversations_and_preserve_workspace_prompt(web_env):
    env = web_env
    await baseline(env)
    project = await folder(env, "project")
    await save(env, project, {}, workspaceDir="/project", promptMarkdown="project prompt")
    created = await env.client.post("/api/conversations", json={"folderId": project})
    data = await created.json()
    old = data["conversation"]["conversationUuid"]
    before = await env.server._conversation_row(123, old)
    impact = await env.client.post(f"/api/conversation-folders/{project}/properties/impact", json={"runDefaults": PAID})
    assert (await impact.json())["affectedCount"] == 0
    saved = await save(env, project, PAID, updateSnapshots=True)
    assert saved["affectedCount"] == 0 and saved["updatedCount"] == 0
    after = await env.server._conversation_row(123, old)
    assert after == before
    current = await env.client.get(f"/api/conversation-folders/{project}/properties")
    current = await current.json()
    assert current["workspace"]["local"] == "/project" and current["prompt"]["local"] == "project prompt"
    # Old clients saving workspace/prompt do not erase the newly added settings.
    await env.client.put(f"/api/conversation-folders/{project}/properties", json={"workspaceDir": "/new", "promptMarkdown": "new"})
    assert (await defaults(env, project))["folderDefaults"] == PAID


async def test_unknown_or_foreign_folder_defaults_are_not_exposed_or_used_for_creation(web_env):
    env = web_env
    await baseline(env)
    other = await folder(env, "foreign", defaults=PAID)
    await env.db.conn.execute("UPDATE web_conversation_folders SET owner_chat_id=456 WHERE folder_uuid=?", (other,))
    await env.db.conn.commit()
    for folder_id in (other, "missing"):
        assert (await env.client.get("/api/conversations/defaults", params={"folderId": folder_id})).status == 404
        assert (await env.client.get(f"/api/conversation-folders/{folder_id}/properties")).status == 404
        assert (await env.client.post("/api/conversations", json={"folderId": folder_id})).status == 404


async def test_removed_folder_model_falls_back_to_remembered_model_without_erasing_setting(web_env):
    env = web_env
    await baseline(env)
    project = await folder(env, "project", defaults={"mainModel": "openai/gpt"})
    env.server.config.models.providers["openai"].models = [model for model in env.server.config.models.providers["openai"].models if model.id != "gpt"]
    assert (await defaults(env, project))["defaults"]["mainModel"] == "openai/cheap"
    properties = await env.client.get(f"/api/conversation-folders/{project}/properties")
    assert (await properties.json())["runDefaults"]["local"] == {"mainModel": "openai/gpt"}


async def test_model_without_thinking_accepts_off_and_context_save_preserves_readonly_run_defaults(web_env):
    env = web_env
    await baseline(env)
    model = env.server.config.models.resolve("openai/cheap")[1]
    model.thinking_levels = []
    model.default_thinking_level = ""
    project = await folder(env, "no-thinking", defaults={"mainModel": "openai/cheap", "mainThinkingLevel": "off"})
    assert (await defaults(env, project))["defaults"]["mainThinkingLevel"] == "off"
    # A client with unavailable model options can still save context by omitting
    # runDefaults. Even a since-removed model must remain untouched in storage.
    await save(env, project, PAID)
    env.server.config.models.providers["openai"].models = [model]
    url = f"/api/conversation-folders/{project}/properties"
    payload = {"workspaceDir": "/still-editable", "promptMarkdown": "keep context editing"}
    impact = await env.client.post(url + "/impact", json=payload)
    assert impact.status == 200, await impact.text()
    saved = await env.client.put(url, json=payload)
    assert saved.status == 200, await saved.text()
    data = await (await env.client.get(url)).json()
    assert data["runDefaults"]["local"] == PAID
    assert data["workspace"]["local"] == payload["workspaceDir"]
    assert data["prompt"]["local"] == payload["promptMarkdown"]


async def test_actual_frontend_preview_matches_backend_resolved_defaults(web_env):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend/backend contract test")
    env = web_env
    await baseline(env)
    parent = await folder(env, "parity-parent", defaults=PAID)
    child = await folder(env, "parity-child", parent)
    cases = []

    async def snapshot():
        properties = await (await env.client.get(f"/api/conversation-folders/{child}/properties")).json()
        options = await (await env.client.get("/api/rath/options")).json()
        cases.append({"defaults": properties["runDefaults"], "models": options["models"]})

    for local in ({}, {"mainModel": "openai/cheap", "agentModel": ""},
                  {"mainFastMode": False, "agentModel": "", "agentThinkLevel": "", "agentFastMode": None}):
        await save(env, child, local)
        await snapshot()
    cheap = env.server.config.models.resolve("openai/cheap")[1]
    cheap.thinking_levels = []
    cheap.default_thinking_level = ""
    await save(env, child, {"mainModel": "openai/cheap", "agentModel": ""})
    await snapshot()
    # Removed parent models must resolve identically, without erasing raw overrides.
    await save(env, child, {})
    env.server.config.models.providers["openai"].models = [cheap]
    await snapshot()
    script = """
        import {normalizedRunDefaults} from './web/src/components/folderRunDefaults.js';
        let input = ''; for await (const chunk of process.stdin) input += chunk;
        console.log(JSON.stringify(JSON.parse(input).map(({defaults, models}) => normalizedRunDefaults(defaults, models))));
    """
    process = await asyncio.create_subprocess_exec(
        node, "--input-type=module", "-e", script, cwd=Path(__file__).resolve().parents[1],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(json.dumps(cases).encode()), timeout=15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, error.decode()
    expected = [{key: case["defaults"]["resolved"][key] for key in (*FIELDS, "contextStrategy")} for case in cases]
    assert json.loads(output) == expected


async def test_existing_folder_table_migrates_without_changing_properties(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE web_conversation_folders (
                id INTEGER PRIMARY KEY, folder_uuid TEXT NOT NULL UNIQUE, owner_chat_id INTEGER NOT NULL,
                parent_uuid TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
                workspace_dir TEXT NOT NULL DEFAULT '', prompt_markdown TEXT NOT NULL DEFAULT '',
                pinned_at INTEGER NOT NULL DEFAULT 0, display_order REAL,
                created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO web_conversation_folders(folder_uuid,owner_chat_id,name,workspace_dir,prompt_markdown)
            VALUES ('legacy',123,'Legacy','/keep','keep prompt');
        """)
    db = DB(str(path))
    await db.connect()
    try:
        row = await (await db.conn.execute("SELECT * FROM web_conversation_folders")).fetchone()
        assert json.loads(row["run_defaults_json"]) == {}
        assert row["workspace_dir"] == "/keep" and row["prompt_markdown"] == "keep prompt"
    finally:
        await db.close()
