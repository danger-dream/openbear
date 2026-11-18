"""Persona prompt templates: which templates can be chosen, where the choice is
inherited from, and that a conversation renders the one it was created with."""
from __future__ import annotations

from app.context import prompt_template
from tests.test_folder_run_defaults import folder, save
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as shared_web_env

web_env = shared_web_env


async def template(env, name, content, **flags):
    response = await env.client.post("/api/memory/templates", json={"name": name, "content": content, **flags})
    assert response.status == 200, await response.text()
    return str((await response.json())["id"])


async def run_config(env):
    defaults = (await (await env.client.get("/api/conversations/defaults")).json())["defaults"]
    return {key: defaults[key] for key in ("mainModel", "mainThinkingLevel", "mainFastMode", "agentModel", "agentThinkLevel", "agentFastMode")}


def test_normalize_keeps_only_template_ids():
    assert prompt_template.normalize(None) == ""
    assert prompt_template.normalize(" 12 ") == "12"
    assert prompt_template.normalize("0") == prompt_template.normalize("abc") == prompt_template.normalize(-3) == ""
    assert prompt_template.patch_error("") == prompt_template.patch_error("7") == ""
    assert prompt_template.patch_error("x") == prompt_template.patch_error(7) == "invalid_prompt_template"


async def test_choices_are_the_active_template_and_persona_templates_except_the_agent_prompt(web_env):
    env = web_env
    await _login_cookie(env)
    main = await template(env, "主提示词", "MAIN", is_active=1, is_persona=1)
    writer = await template(env, "写作人设", "WRITER", is_persona=1)
    await template(env, "草稿", "DRAFT")
    agent = await template(env, "Agent 人设", "AGENT", is_persona=1)
    await env.db.conn.execute("UPDATE memory_templates SET is_agent_active=CASE WHEN id=? THEN 1 ELSE 0 END", (int(agent),))
    await env.db.conn.commit()

    choices = await (await env.client.get("/api/prompt-templates/choices")).json()
    assert {key: choices["active"][key] for key in ("id", "name", "chars")} == {"id": main, "name": "主提示词", "chars": 4}
    assert [{key: item[key] for key in ("id", "name", "chars")} for item in choices["items"]] == [{"id": writer, "name": "写作人设", "chars": 6}]
    assert choices["items"][0]["updatedAt"] > 0

    listed = (await (await env.client.get("/api/memory/templates")).json())["items"]
    assert {str(item["id"]): item["is_persona"] for item in listed}[writer] == 1
    response = await env.client.put(f"/api/memory/templates/{writer}", json={"is_persona": 0})
    assert response.status == 200, await response.text()
    assert (await (await env.client.get("/api/prompt-templates/choices")).json())["items"] == []


async def test_a_conversation_renders_the_persona_inherited_from_its_folder(web_env):
    env = web_env
    await _login_cookie(env)
    await template(env, "主提示词", "MAIN-PROMPT", is_active=1)
    writer = await template(env, "写作人设", "WRITER-PROMPT", is_persona=1)
    parent = await folder(env, "写作", defaults={"promptTemplate": writer})
    child = await folder(env, "短篇", parent)

    created = await (await env.client.post("/api/conversations", json={"folderId": child})).json()
    uuid = created["conversation"]["conversationUuid"]
    assert created["conversation"]["promptTemplate"] == writer
    assert created["state"]["promptTemplate"] == writer and created["state"]["promptTemplateLocked"] is False
    assert "WRITER-PROMPT" in await env.server._build_system_prompt_for_chat(uuid)

    properties = await (await env.client.get(f"/api/conversation-folders/{child}/properties")).json()
    assert properties["runDefaults"]["resolved"]["promptTemplate"] == writer

    plain = await (await env.client.post("/api/conversations", json={})).json()
    assert plain["conversation"]["promptTemplate"] == ""
    assert "MAIN-PROMPT" in await env.server._build_system_prompt_for_chat(plain["conversation"]["conversationUuid"])

    hidden = await template(env, "非人设", "HIDDEN")
    response = await env.client.put(f"/api/conversation-folders/{child}/properties", json={"runDefaults": {"promptTemplate": hidden}})
    assert response.status == 400 and "prompt_template_not_found" in await response.text()


async def test_the_choice_is_made_before_the_first_round_and_then_fixed(web_env):
    env = web_env
    await _login_cookie(env)
    await template(env, "主提示词", "MAIN-PROMPT", is_active=1)
    writer = await template(env, "写作人设", "WRITER-PROMPT", is_persona=1)
    hidden = await template(env, "未标记", "HIDDEN")

    created = await (await env.client.post("/api/conversations", json={"runConfig": {**await run_config(env), "promptTemplate": writer}})).json()
    uuid = created["conversation"]["conversationUuid"]
    assert created["conversation"]["promptTemplate"] == writer

    response = await env.client.post(f"/api/conversations/{uuid}/prompt-template", json={"promptTemplate": hidden})
    assert response.status == 400 and (await response.json())["error"] == "prompt_template_not_found"
    response = await env.client.post(f"/api/conversations/{uuid}/prompt-template", json={"promptTemplate": ""})
    assert response.status == 200, await response.text()
    assert (await response.json())["runConfig"]["promptTemplate"] == ""

    internal = int(created["conversation"]["internalChatId"])
    await env.db.conn.execute("UPDATE sessions SET system_snapshot='frozen' WHERE chat_id=?", (internal,))
    await env.db.conn.commit()
    response = await env.client.post(f"/api/conversations/{uuid}/prompt-template", json={"promptTemplate": writer})
    assert response.status == 409 and (await response.json())["error"] == "prompt_template_locked"
    state = await (await env.client.get(f"/api/conversations/{uuid}/state")).json()
    assert state["promptTemplate"] == "" and state["promptTemplateLocked"] is True

    duplicate = await env.server._duplicate_web_conversation_data(await env.server._conversation_row(123, uuid))
    assert duplicate["prompt_template"] == ""

    response = await env.client.post("/api/conversations", json={"runConfig": {**await run_config(env), "promptTemplate": hidden}})
    assert response.status == 400 and (await response.json())["error"] == "prompt_template_not_found"
    response = await env.client.patch("/api/conversations/defaults", json={"promptTemplate": writer})
    assert response.status == 400 and (await response.json())["error"] == "prompt_template_not_global"


def test_template_ids_reject_unicode_and_out_of_sqlite_range_without_conversion_errors():
    for value in ('²', '١', '１２', '01', '9' * 5000, '9223372036854775808'):
        assert prompt_template.normalize(value) == ''
        assert prompt_template.patch_error(value) == 'invalid_prompt_template'
    assert prompt_template.normalize('9223372036854775807') == '9223372036854775807'


async def test_persona_save_serializes_with_first_render_and_freeze(web_env, monkeypatch):
    import asyncio
    from app.memory.builtin import BuiltinMemoryClient

    env = web_env
    await _login_cookie(env)
    old = await template(env, 'old', 'PERSONA-OLD', is_persona=1)
    new = await template(env, 'new', 'PERSONA-NEW', is_persona=1)
    row = await env.server._create_web_conversation(123, run_config={'prompt_template': old})
    uuid, chat = row['conversation_uuid'], row['internal_chat_id']
    selected, release, saving = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = BuiltinMemoryClient._template_by_id
    check = prompt_template.selectable_error

    async def gated_lookup(self, template_id):
        result = await original(self, template_id)
        selected.set()
        await release.wait()
        return result

    async def checked(db, value):
        result = await check(db, value)
        saving.set()
        return result

    monkeypatch.setattr(BuiltinMemoryClient, '_template_by_id', gated_lookup)
    monkeypatch.setattr(prompt_template, 'selectable_error', checked)

    async def freeze():
        # Production chat_runtime's first-run critical section, with real renderer/DB.
        async with env.server._conversation_tree_lock:
            policy = await env.server._resolved_prompt_policy(uuid)
            rendered = await env.server._build_system_prompt_for_chat(uuid, prompt_policy=policy)
            return await env.server._select_prompt_snapshot(chat, uuid, rendered, policy)

    first = asyncio.create_task(freeze())
    setter = None
    try:
        await asyncio.wait_for(selected.wait(), 5)
        setter = asyncio.ensure_future(env.client.post(f'/api/conversations/{uuid}/prompt-template', json={'promptTemplate': new}))
        await asyncio.wait_for(saving.wait(), 5)
        await asyncio.sleep(0)
        assert not setter.done(), 'save must not claim success while an old persona is being frozen'
    finally:
        release.set()
        await first
    response = await setter
    assert response.status == 409
    assert (await env.server._conversation_row(123, uuid))['prompt_template'] == old
    from app.db.dao import MessageDAO
    assert 'PERSONA-OLD' in await MessageDAO(env.db).get_system_snapshot(chat)


async def test_deleted_persona_keeps_frozen_snapshot_and_rebuilds_only_at_rotation(web_env):
    from app.context.runtime import ContextManager
    from app.context.store import ContextOwner, WindowStore
    from app.context.window import WindowPolicy
    from app.db.dao import MessageDAO
    from tests.test_context_strategies import history

    env = web_env
    await _login_cookie(env)
    await template(env, 'main', 'DEFAULT-SYSTEM', is_active=1)
    selected = await template(env, 'persona', 'FROZEN-PERSONA', is_persona=1)
    row = await env.server._create_web_conversation(123, run_config={'prompt_template': selected})
    uuid, chat = row['conversation_uuid'], row['internal_chat_id']
    dao = MessageDAO(env.db)
    sid = await dao.get_or_create_session_uuid(chat)
    await dao.get_or_set_system_snapshot(chat, 'FROZEN-PERSONA')
    assert (await env.client.delete(f'/api/memory/templates/{selected}')).status == 200
    state = await (await env.client.get(f'/api/conversations/{uuid}/state')).json()
    assert state['promptTemplate'] == selected and state['promptTemplateMissing'] is True
    assert state['promptTemplateLocked'] is True
    config = await env.server._conversation_run_config_public(123, uuid)
    assert config['promptTemplateMissing'] is True
    assert await dao.get_system_snapshot(chat) == 'FROZEN-PERSONA'
    assert 'DEFAULT-SYSTEM' in await env.server._build_system_prompt_for_chat(uuid, strict=True)
    assert await env.server._effective_window_system(chat, 'FROZEN-PERSONA') == 'FROZEN-PERSONA'

    store = WindowStore(env.db, ContextOwner.controller(chat_id=chat, session_uuid=sid, conversation_uuid=uuid))
    calls = []
    async def rebuild(current):
        calls.append(current)
        return await env.server._rebuild_missing_persona_system(uuid, current)
    def manager():
        return ContextManager(store, WindowPolicy(128000, retain_tokens=1200, dialogue_tokens=0),
                              backend=None, model='fake', rebuild_system=rebuild)
    runtime = manager()
    original = history()
    current = await runtime.prepare(original, system='FROZEN-PERSONA', tools=[])
    assert runtime.system == 'FROZEN-PERSONA' and calls == []
    # A failed rebuild must leave the committed window and effective system alone.
    import pytest
    strategy = runtime.strategies['sliding_window']
    class FailedCompression:
        async def compress(self, _request):
            raise RuntimeError('synthetic compression failure')
    runtime.strategies['sliding_window'] = FailedCompression()
    with pytest.raises(RuntimeError, match='synthetic compression failure'):
        await runtime.prepare(current, system='FROZEN-PERSONA', tools=[], force=True)
    assert await env.server._effective_window_system(chat, 'FROZEN-PERSONA') == 'FROZEN-PERSONA'
    assert not (await store.load(fresh=True))['state'].get('systemReplacement')
    calls.clear()
    runtime.strategies['sliding_window'] = strategy
    current = await runtime.prepare(current, system='FROZEN-PERSONA', tools=[], force=True)
    assert runtime.system == 'DEFAULT-SYSTEM' and calls == ['FROZEN-PERSONA']
    await runtime.checkpoint(current)
    restored = manager()
    await restored.prepare(current, system='FROZEN-PERSONA', tools=[])
    assert restored.system == 'DEFAULT-SYSTEM' and len(calls) == 1
    assert await dao.get_system_snapshot(chat) == 'FROZEN-PERSONA'
    assert (await env.server._conversation_row(123, uuid))['prompt_template'] == selected
    assert await env.server._effective_window_system(chat, 'FROZEN-PERSONA') == 'DEFAULT-SYSTEM'
    doc, _ = await env.server._editor_snapshot(row)
    assert doc['system'] == 'DEFAULT-SYSTEM'
    preview = await (await env.client.post(f'/api/conversations/{uuid}/system-prompt/preview')).json()
    from app.web_console.conversation_prompt import system_prompt_sha256
    assert preview['beforeHash'] == system_prompt_sha256('DEFAULT-SYSTEM')
    assert preview['systemChanged'] is False
    # A subsequent explicit source change supersedes the old window replacement.
    await restored.prepare(current, system='EXPLICIT-NEW', tools=[])
    assert restored.system == 'EXPLICIT-NEW'


async def test_deleted_persona_first_assembly_falls_back_and_unmarked_template_is_not_missing(web_env):
    env = web_env
    await _login_cookie(env)
    await template(env, 'main', 'DEFAULT-SYSTEM', is_active=1)
    selected = await template(env, 'persona', 'PERSONA', is_persona=1)
    row = await env.server._create_web_conversation(123, run_config={'prompt_template': selected})
    await env.client.put(f'/api/memory/templates/{selected}', json={'is_persona': 0})
    assert not await prompt_template.missing(env.db, selected)
    assert 'PERSONA' in await env.server._build_system_prompt_for_chat(row['conversation_uuid'])
    assert (await env.client.delete(f'/api/memory/templates/{selected}')).status == 200
    assert 'DEFAULT-SYSTEM' in await env.server._build_system_prompt_for_chat(row['conversation_uuid'], strict=True)
    assert (await env.server._conversation_row(123, row['conversation_uuid']))['prompt_template'] == selected


async def test_invalid_unicode_persona_parameter_is_a_client_error(web_env):
    await _login_cookie(web_env)
    row = await web_env.server._create_web_conversation(123)
    for value in ('²', '9' * 5000):
        response = await web_env.client.post(f"/api/conversations/{row['conversation_uuid']}/prompt-template", json={'promptTemplate': value})
        assert response.status == 400
        assert (await response.json())['error'] == 'invalid_prompt_template'


async def test_missing_persona_never_replaces_an_explicit_system_override(web_env, monkeypatch):
    from app.context.prompt_policy import policy
    from app.context.runtime import ContextManager
    from app.context.store import ContextOwner, WindowStore
    from app.context.window import WindowPolicy
    from tests.test_context_strategies import history

    env = web_env
    await _login_cookie(env)
    row = await env.server._create_web_conversation(123, run_config={'prompt_template': '999'})
    async def overridden(_uuid):
        return policy({'text': 'EXPLICIT', 'overrideSystemPrompt': True})
    monkeypatch.setattr(env.server, '_active_prompt_policy', overridden)
    assert await env.server._rebuild_missing_persona_system(row['conversation_uuid'], 'EXPLICIT') == 'EXPLICIT'
    async def forbidden(_system):
        raise AssertionError('frozen editor system must not be rebuilt')
    manager = ContextManager(WindowStore(env.db, ContextOwner.controller(chat_id=row['internal_chat_id'], session_uuid='frozen-editor')),
                             WindowPolicy(128000, retain_tokens=1200, dialogue_tokens=0), backend=None, model='fake',
                             frozen_system=True, rebuild_system=forbidden)
    await manager.prepare(history(), system='EDITOR-SYSTEM', tools=[], force=True)
    assert manager.system == 'EDITOR-SYSTEM'


async def test_stale_draft_and_folder_persona_ids_are_retained_without_blocking_creation(web_env):
    env = web_env
    await _login_cookie(env)
    await template(env, 'main', 'DEFAULT', is_active=1)
    selected = await template(env, 'deleted', 'PERSONA', is_persona=1)
    parent = await folder(env, 'personas', defaults={'promptTemplate': selected})
    assert (await env.client.delete(f'/api/memory/templates/{selected}')).status == 200
    # This is the complete payload sent by a draft, including an inherited ID.
    created = await env.client.post('/api/conversations', json={'folderId': parent, 'runConfig': {**await run_config(env), 'promptTemplate': selected}})
    assert created.status == 200, await created.text()
    body = await created.json()
    assert body['state']['promptTemplate'] == selected and body['state']['promptTemplateMissing'] is True
    assert await env.server._build_system_prompt_for_chat(body['conversation']['conversationUuid'], strict=True) == 'DEFAULT'
    assert (await env.client.put(f'/api/conversation-folders/{parent}/properties', json={'runDefaults': {'promptTemplate': selected}})).status == 200
