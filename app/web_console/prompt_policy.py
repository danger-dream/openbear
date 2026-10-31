"""Shared prompt-policy resolution, frozen selections and editor preview API."""
from __future__ import annotations

import inspect
import json

from aiohttp import web

from app.context.prompt_policy import (FLAGS, conversation_policy, folder_policy, policy,
    policy_tool_names, read_json, snapshot_key, write_json)
from app.db.engine import now_ts
from app.memory.builtin import BuiltinMemoryClient
from app.web_console.core import _WEB_SESSION_KEY


def folder_policy_key(owner, folder_id):
    return f'folder_prompt_policy:{owner}:{folder_id}'


def tool_schema_name(schema):
    return schema.get('name', schema.get('function', {}).get('name'))


def effective_prompt_tools(schemas, value):
    """Schemas a conversation advertises under its frozen prompt policy."""
    if not value['overrideSystemPrompt']:
        return list(schemas)
    allowed = set(policy_tool_names(value))
    return [s for s in schemas if tool_schema_name(s) in allowed]


class PromptPolicyMixin:
    async def _local_conversation_prompt_policy(self, conversation_uuid):
        return conversation_policy(await read_json(self.db.conn, 'conversation_context:' + conversation_uuid))

    async def _resolved_prompt_policy(self, conversation_uuid, *, folders=None, folder_id=None, local=None):
        cur = await self.db.conn.execute('SELECT * FROM web_conversations WHERE conversation_uuid=?', (conversation_uuid,))
        row = await cur.fetchone()
        if row is None:
            return policy()
        if local is None:
            local = await self._local_conversation_prompt_policy(conversation_uuid)
        if local['text'].strip():
            return local
        folders = folders if folders is not None else await self._tree_context_folders(row['owner_chat_id'])
        return folder_policy(row['folder_uuid'] if folder_id is None else folder_id, folders)[0]

    def _folder_prompt_policy_public(self, folder_id, folders):
        row = folders[folder_id]
        local = policy(row.get('prompt_policy'), text=row.get('prompt_markdown') or '')
        parent = str(row.get('parent_uuid') or '')
        inherited, inherited_source = folder_policy(parent, folders) if parent else (policy(), '')
        effective, source = folder_policy(folder_id, folders)
        path = lambda key: self._tree_folder_path_text(key, folders) if key else '未设置'
        return dict(local=local, inherited=inherited, effective=effective, sourceFolderId=source,
                    sourcePath=path(source), inheritedSourcePath=path(inherited_source))

    async def _validate_prompt_policy(self, value, *, previous=None, folder_id='', conversation_uuid='', folder_values=None, folder_path=''):
        registry = getattr(self, 'tools', None)
        available = set(registry.names(scope='main')) if registry else set()
        missing = set(value['toolNames']) - available - set((previous or {}).get('toolNames', []))
        if missing:
            raise ValueError('工具不可用：' + '、'.join(sorted(missing)))
        if value['overrideSystemPrompt'] and value['text'].strip():
            await self._render_policy_template(value, conversation_uuid=conversation_uuid, folder_values=folder_values, folder_path=folder_path)

    async def _folder_policy_from_body(self, owner, folder_id, body, text):
        folders = await self._tree_context_folders(owner)
        if folder_id not in folders:
            raise web.HTTPNotFound(text='folder_not_found')
        previous = policy(folders[folder_id].get('prompt_policy'))
        try:
            raw = body.get('promptPolicy', previous)
            if not isinstance(raw, dict):
                raise ValueError('提示词配置必须是对象')
            value = policy(raw, text=text, strict=True)
            proposed = {key: dict(row) for key, row in folders.items()}
            if 'workspaceDir' in body:
                proposed[folder_id]['workspace_dir'] = body['workspaceDir']
            workspace = self._tree_effective_from_map(folder_id, proposed, str(getattr(self, 'workspace_dir', '') or ''))[0]
            await self._validate_prompt_policy(value, previous=previous, folder_values=(workspace, text),
                folder_path=self._tree_folder_path_text(folder_id, folders))
            return value
        except ValueError as exc:
            raise web.HTTPUnprocessableEntity(text=json.dumps({'error': 'invalid_prompt_policy', 'message': str(exc)}, ensure_ascii=False), content_type='application/json') from exc

    async def _render_policy_template(self, value, *, conversation_uuid='', folder_values=None, folder_path=''):
        params = await self._prompt_template_params_live(conversation_uuid, prompt_policy=value, folder_values=folder_values)
        if folder_path:
            params['conversation']['folderPath'] = folder_path
        output = await BuiltinMemoryClient(self.db, identity=self.config.memory.identity).render_system_prompt(
            params, template_content=value['text'], template_name='Conversation custom system', source='local-prompt-template')
        if '[[ERROR:' in output:
            raise ValueError('提示词模板渲染失败')
        return output, params

    async def _build_policy_prompt(self, conversation_uuid, value, folder_values=None, folder_path=None):
        # Focused integrations may replace the historical builder signature.
        builder = self._build_system_prompt_for_chat
        kwargs = {'folder_values': folder_values, 'strict': True}
        if 'prompt_policy' in inspect.signature(builder).parameters:
            kwargs['prompt_policy'] = value
        if 'folder_path' in inspect.signature(builder).parameters:
            kwargs['folder_path'] = folder_path
        return await builder(conversation_uuid, **kwargs)

    async def _select_prompt_snapshot(self, chat_id, conversation_uuid, candidate, live_policy):
        if not conversation_uuid:
            from app.db.dao import MessageDAO
            return await MessageDAO(self.db).get_or_set_system_snapshot(chat_id, candidate), policy()
        async with self.db.conn.transaction(label='conversation-prompt-selection') as conn:
            cur = await conn.execute('SELECT system_snapshot FROM sessions WHERE chat_id=?', (chat_id,))
            row = await cur.fetchone()
            current = str(row['system_snapshot'] or '') if row else ''
            frozen = await read_json(conn, snapshot_key(conversation_uuid))
            if not current:
                current, frozen = candidate, live_policy
                await conn.execute('UPDATE sessions SET system_snapshot=?,updated_at=? WHERE chat_id=?', (current, now_ts(), chat_id))
                await write_json(conn, snapshot_key(conversation_uuid), frozen)
            elif frozen is None:
                # Existing sessions predate this feature: saving future config
                # alone never changes their active tools or automatic injections.
                frozen = policy()
                await write_json(conn, snapshot_key(conversation_uuid), frozen)
        return current, policy(frozen)

    async def _active_prompt_policy(self, conversation_uuid):
        cur = await self.db.conn.execute('SELECT s.system_snapshot FROM web_conversations w LEFT JOIN sessions s ON s.chat_id=w.internal_chat_id WHERE w.conversation_uuid=?', (conversation_uuid,))
        row = await cur.fetchone()
        if not row or not row['system_snapshot']:
            return await self._resolved_prompt_policy(conversation_uuid)
        stored = await read_json(self.db.conn, snapshot_key(conversation_uuid))
        return policy(stored)

    def _live_main_tool_schemas(self):
        return self.tools.schemas(scope='main') if self.tools else []

    async def _write_prompt_snapshot_policy(self, conn, row, value, rendered):
        await write_json(conn, snapshot_key(row['conversation_uuid']), value)
        cur = await conn.execute('SELECT settings_json FROM context_editor_branches WHERE conversation_uuid=?', (row['conversation_uuid'],))
        branch = await cur.fetchone()
        if branch:
            settings = json.loads(branch['settings_json'])
            settings['system'] = rendered
            # An edited branch advertises frozen schemas; a refresh replaces them
            # with the current definitions, just as it replaces the edited system.
            settings['tools'] = effective_prompt_tools(self._live_main_tool_schemas(), value)
            await conn.execute('UPDATE context_editor_branches SET settings_json=? WHERE conversation_uuid=?',
                               (json.dumps(settings, ensure_ascii=False), row['conversation_uuid']))

    async def handle_api_prompt_tools(self, request):
        registry = self.tools
        summaries = registry.summaries(scope='main') if registry else {}
        builtin = set(registry.names(scope='main', source='builtin')) if registry else set()
        return web.json_response({'items': [{'name': name, 'description': text, 'source': 'builtin' if name in builtin else 'mcp'} for name, text in summaries.items()]}, headers={'Cache-Control': 'no-store'})

    async def handle_api_prompt_template_preview(self, request):
        body = await self._json_body(request)
        owner = request[_WEB_SESSION_KEY].chat_id
        conv, folder_id = str(body.get('conversationId') or ''), str(body.get('folderId') or '')
        try:
            value = policy({k: body[k] for k in ('text', *FLAGS, 'toolNames') if k in body}, strict=True)
            if conv:
                await self._conversation_row(owner, conv, require=True)
            elif folder_id:
                folders = await self._tree_context_folders(owner)
                if folder_id not in folders:
                    raise web.HTTPNotFound(text='folder_not_found')
            params = await self._prompt_template_params_live(conv, prompt_policy=value)
            if folder_id and not conv:
                workspace = self._tree_effective_from_map(folder_id, folders, str(getattr(self, 'workspace_dir', '') or ''))[0]
                params = await self._prompt_template_params_live('', folder_values=(workspace, value['text']), prompt_policy=value)
                params['conversation']['folderPath'] = self._tree_folder_path_text(folder_id, folders)
            output = await BuiltinMemoryClient(self.db, identity=self.config.memory.identity).render_system_prompt(
                params, template_content=value['text'], template_name='Property template preview', source='api-preview')
            if '[[ERROR:' in output:
                raise ValueError('模板渲染失败：' + output)
            return web.json_response({'prompt': output, 'output_len': len(output), 'params': params}, headers={'Cache-Control': 'no-store'})
        except ValueError as exc:
            return web.json_response({'error': 'invalid_prompt_template', 'message': str(exc)}, status=422)
